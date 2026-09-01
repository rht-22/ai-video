"""E15 스타일 구성 — AI 연출 플랜(style_plan/v1)의 검증·정규화·배치.

발주 기획: ves-orchestrator `docs/prompts/e15-style-compose.md`.

스토리 구성이 끝난 편에 **연출 레이어**를 얹는다. 이 모듈은 LLM 이 낸 플랜을
받아 계약을 강제하고, 편집실(edit_overrides/v3)이 쓰는 것과 **같은 자료 모양**으로
바꿔 놓는 것까지만 한다 — 배치(place_anchored_*)·렌더는 v3 코드를 그대로 탄다.

왜 v3 어휘를 재사용하는가: texts·images·자막 style·title.segments 는 이미 사람이
편집실에서 만드는 것과 **완전히 같은 것**이다. 새 렌더 경로를 만들면 사람 손과 AI 손이
다른 코드로 그려지고, 언젠가 한쪽만 고쳐진다. 그래서 검증도 v3 검증기
(`edit_overrides.validate_overrides`)를 그대로 통과시킨다 — 이 모듈은 그 앞에서
**AI 에게만 적용되는 좁은 규칙**(하드캡·스티커 화이트리스트·라벨 제한)을 건다.

우선순위(엔진이 강제): 편집실 오버라이드 > 채널 design 명시 키 > AI 플랜 > 기본값.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.modules.edit_overrides import (
    SCHEMA_V3,
    EditOverrideError,
    overrides_texts,
    validate_overrides,
    validate_title_segments,
)
from app.modules.tts import SPEED_TO_RATE, VOICE_PRESETS

SCHEMA = "style_plan/v1"

# ── 하드캡 (과연출 방지) ────────────────────────────────────────────────────
# LLM 은 "더 넣을수록 좋다"로 흐른다. 상한을 넘으면 **앞에서부터 자르고 로그**를 남긴다
# (조용한 절단 금지 — 사람이 '왜 8개만 나왔지'를 알아야 한다).
MAX_TEXTS = 8
MAX_IMAGES = 4
MAX_SUBTITLE_STYLES = 10
MAX_TITLE_SEGMENTS = 5

# ── 제목 한 줄 글자수 (E21, 2026-08-25) ────────────────────────────────────
# 스토리 단계의 `_enforce_title_line_limit(max_chars=20)` 과 같은 수치다. 렌더러는 제목을
# 20자에서 접으므로(`split_text_smart(text, 20)`) 이보다 길면 한 줄이 두 줄이 되고, 제목
# 블록이 3줄이 되면 글자가 0.85배로 작아진다 — **2줄 형식을 지키려면** 여기서 막아야 한다
# (사용자 지시: "다른 쇼츠들처럼 두 줄 형식은 유지").
MAX_TITLE_LINE_CHARS = 20

# ── AI 에게 여는 자막 강조 키 ───────────────────────────────────────────────
# v3 의 줄 스타일은 넷(size·y·color·rotate)이지만 AI 에게는 **강조 둘만** 연다.
# 위치(y)·회전(rotate)은 자막 가독성을 직접 깨뜨리는 축이라 사람 전용으로 남긴다
# (기획서 §13-4 — 파일럿에서 열지 말지 결정). 열려면 이 튜플 한 줄만 고치면 된다.
STYLE_SUBTITLE_KEYS = ("size", "color")
# 강조 크기 상한 — v3 는 상한이 없지만(사람이 보고 정한다) AI 산출은 화면을 덮는 값을
# 낼 수 있다. 기본 자막(65px) 대비 2배 남짓까지만.
SUBTITLE_SIZE_RANGE = (30.0, 140.0)

# ── AI 에게 여는 디자인 키 ─────────────────────────────────────────────────
# 값은 "CLI 단수 플래그 이름 → DesignConfig 필드" (cli._build_design_config 와 같은 조립).
#
# ⚠ `video_speed` 는 **일부러 뺐다**(기획서 §4 는 열려고 했다 — 코드 실측으로 뒤집었다).
#    배속은 렌더 효과가 아니라 **길이 예산**이다: pipeline 이 `_speed` 를 스토리 길이
#    클램프·확장 상한에 ×S 로 곱해 쓰고(3378·3389·3417~3427), style 단계는 그 클램프가
#    **끝난 뒤**에 돈다. 여기서 배속을 바꾸면 40~60초 정책이 이미 적용된 편의 출력 길이만
#    조용히 달라진다(1.1배면 50초 편이 45초로 나간다). 배속을 편 단위로 열려면 style 을
#    클램프 앞으로 옮겨야 하는데, 그러면 앵커 좌표의 기준인 최종 클립이 아직 없다.
#    → 배속은 채널 플래그로 남긴다.
STYLE_DESIGN_ALLOWED = (
    "tts_rotate",
    "title_box", "title_box2", "title_box_color", "title_box_color2",
)
TITLE_BOX_KINDS = ("none", "round", "rect")
ROTATE_RANGE_DEG = 180.0

# ── 제목 기울기는 AI 에게 **닫혀 있다** (E18, 2026-08-24) ────────────────────
# 이 키는 세 번 바뀌었다. 지시를 그대로 적어 둔다 — 다음 사람이 되돌리지 않게:
#   ① "제목은 왠만하면 회전은 안되게 해주고"            → 통째로 차단
#   ② "돌리는 거는 가능한데, 안 돌리게 제약 정도로만"    → ±15° 로 완화(E17-1)
#   ③ "제목은 회전하지 않도록 … ai가 회전을 못하게"      → **다시 차단(현재)**
# ②로 열어 둔 ±15° 로도 매 편 기울어져 나왔다(실측: SHOTCONE 2화 title_rotate 적용).
# 그래서 범위가 아니라 **키 자체**를 닫는다.
#
# ⚠ 닫는 방식이 '모르는 키'가 아니라 **드롭+메모**인 이유: STYLE_DESIGN_ALLOWED 에서
#   빼기만 하면 아래 unknown 검사가 플랜 **전체**를 거절한다. LLM 은 이 키를 계속 낼
#   테고, 그때마다 효과 텍스트·제목 창까지 통째로 날아간다. 그래서 '받되 버린다'.
# ⚠ 사람·채널의 `--design-title-rotate` 는 그대로다(-180~180) — 닫는 것은 **AI 산출**
#   뿐이고, 사람이 화면을 보고 정한 값은 사람 것이다(E17-1 에서 정한 규율 유지).
# ── 제목 굵게도 AI 에게 **닫혀 있다** (E21, 2026-08-25) ─────────────────────
# 사용자 지시: "제목은 굵게 하기 금지(볼드체 금지) — 애초에 굵은 폰트를 써서 볼드체로
# 바꾸면 글씨가 뭉개져서 안 보임". 번들 제목 폰트(Jalnan)는 원래 극굵이라 획을 더 얹으면
# 글자 속이 메워진다(렌더러 주석의 2026-08-21 bold.png 실측과 같은 관찰).
# 닫는 방식은 title_rotate 와 같다 — **드롭+메모**(플랜 전체를 거절하면 효과 텍스트·제목
# 창까지 같이 날아간다). 막는 것은 **AI 산출뿐**이고 채널·편집실의 `--design-title-bold(2)`
# 는 그대로다(사람이 화면을 보고 정한 값은 사람 것 — E17-1 에서 정한 규율).
STYLE_DESIGN_IGNORED = ("title_rotate", "title_bold", "title_bold2")

# 닫힌 키마다 '누가 대신 정하는가'를 한 줄로 — 메모가 그 자리에서 길을 알려 준다.
STYLE_DESIGN_IGNORED_WHY = {
    "title_rotate": "제목 기울기는 채널·편집실이 정합니다",
    "title_bold": "제목 굵게는 폰트가 이미 굵어 글자가 뭉갭니다 — 채널·편집실이 정합니다",
    "title_bold2": "제목 굵게는 폰트가 이미 굵어 글자가 뭉갭니다 — 채널·편집실이 정합니다",
}

# ── TTS 라벨 ───────────────────────────────────────────────────────────────
# 불변 계약(E11·E12)을 **한 곳에서** 가져온다 — 여기 문자열을 베끼면 tts.py 가 라벨을
# 늘렸을 때 조용히 어긋난다. `elevenlabs:` 접두사는 계정 종속이라 AI 산출에 금지한다
# (E12: 사람이 대시보드에서 고르는 값이고, 계정마다 보이스 라이브러리가 다르다).
STYLE_VOICES = tuple(VOICE_PRESETS)
STYLE_SPEEDS = tuple(SPEED_TO_RATE)

STICKER_DIR_NAME = "stickers"       # app/assets/stickers
STICKER_RUN_SUBDIR = "style_assets"  # <run_dir>/style_assets — v3 images.file 의 기준

# ── E19-5 효과음(SFX) ──────────────────────────────────────────────────────
# 스티커와 같은 규율: 라이선스 확인된 파일만 assets/sfx 에 번들하고, AI 는 manifest 의
# **id 만** 고른다. **빈 목록이 정상 상태다.** 게이트는 톤 프로파일의 sfx 절 —
# 절이 없는 채널은 프롬프트에도 안 실리고 플랜에 실려 와도 전량 드롭+기록이다.
SFX_DIR_NAME = "sfx"                 # app/assets/sfx
SFX_GAIN_RANGE = (-30.0, 0.0)
SFX_BEAT_TEXT = {
    "rage": "분노 폭발·고함",
    "surprise": "서프라이즈·반전 순간",
    "action_foley": "액션 폴리(부딪힘·쏟아짐 등 화면 속 동작 강조)",
}


class StylePlanError(ValueError):
    """AI 스타일 플랜이 계약을 위반 — 조용히 무시하지 않는다.

    ⚠ 다만 **호출부의 처리는 edit_overrides 와 다르다**: 사람이 고친 편집은 실패해야
    맞지만(사람 입력이 증발하면 안 된다), AI 연출은 부가물이라 본편 발행을 막지 않는다.
    pipeline 은 이 예외를 잡아 '스타일 없이 진행'하고 stdout·run_log 에 크게 남긴다.
    """


# ─────────────────────────────────────────────────────────────────────────
# 스티커 라이브러리
# ─────────────────────────────────────────────────────────────────────────
def load_sticker_manifest(app_root: Path) -> dict[str, dict[str, Any]]:
    """`app/assets/stickers/manifest.json` → {id: {file, tags, desc, w}}.

    없거나 비어 있으면 **빈 dict**(스티커 없는 운용이 정상 상태다 — 라이선스가 확인된
    자산만 번들한다는 규율이라, 초기 배포에는 목록이 비어 있을 수 있다).
    프롬프트에는 이 요약만 들어가고 산출은 **id 만** 받는다 — 엔진이 파일 시스템을
    AI 에게 열지 않는 현행 규율(이미지는 오케스트레이터가 run_dir 로 내려놓는다)의 연장.
    """
    path = Path(app_root) / "assets" / STICKER_DIR_NAME / "manifest.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("stickers") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "").strip()
        fname = str(it.get("file") or "").strip()
        if not sid or not fname:
            continue
        out[sid] = {"file": fname, "tags": it.get("tags") or [],
                    "desc": str(it.get("desc") or ""), "w": it.get("w")}
    return out


def sticker_catalog_for_prompt(manifest: dict[str, dict[str, Any]]) -> str:
    """프롬프트에 넣을 스티커 목록 한 덩어리. 비면 빈 문자열(= 스티커 금지 안내)."""
    if not manifest:
        return ""
    lines = [f"- {sid}: {m['desc'] or sid}" + (f" (권장 w={m['w']})" if m.get("w") else "")
             for sid, m in sorted(manifest.items())]
    return "\n".join(lines)


def stage_sticker(sticker_id: str, manifest: dict[str, dict[str, Any]],
                  app_root: Path, run_dir: Path) -> str:
    """스티커 id → run_dir 상대 경로(`style_assets/<파일명>`). 파일을 복사해 둔다.

    v3 images 계약이 **run_dir 상대 경로**라 그렇다 — 여기서 한 번 복사해 두면 배치·
    렌더·편집실 재렌더가 편집실이 올린 이미지와 **완전히 같은 코드**를 탄다(경로 탈출
    검사·용량 상한·overlay 합성 전부). 없는 id 는 KeyError 로, 호출부가 드롭+로그 한다.
    """
    meta = manifest[sticker_id]                       # KeyError = 없는 id (호출부가 처리)
    src = Path(app_root) / "assets" / STICKER_DIR_NAME / meta["file"]
    if not src.is_file():
        raise KeyError(sticker_id)                    # manifest 에 있는데 파일이 없다
    dest_dir = Path(run_dir) / STICKER_RUN_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if not dest.exists():
        shutil.copy2(src, dest)
    return f"{STICKER_RUN_SUBDIR}/{src.name}"


# ─────────────────────────────────────────────────────────────────────────
# E19-5: SFX 라이브러리 (스티커 로더와 같은 모양 — 규율이 같으니 코드도 같다)
# ─────────────────────────────────────────────────────────────────────────
def load_sfx_manifest(app_root: Path) -> dict[str, dict[str, Any]]:
    """`app/assets/sfx/manifest.json` → {id: {file, desc}}. 없거나 비면 빈 dict(정상)."""
    path = Path(app_root) / "assets" / SFX_DIR_NAME / "manifest.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("sfx") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "").strip()
        fname = str(it.get("file") or "").strip()
        if not sid or not fname:
            continue
        out[sid] = {"file": fname, "desc": str(it.get("desc") or "")}
    return out


def stage_sfx(sfx_id: str, manifest: dict[str, dict[str, Any]],
              app_root: Path, run_dir: Path) -> str:
    """SFX id → run_dir 상대 경로(`style_assets/<파일명>`). 스티커와 같은 스테이징 —
    체크포인트 재적용(편집실 재렌더)이 번들 없이도 같은 소리를 재현한다."""
    meta = manifest[sfx_id]                       # KeyError = 없는 id (호출부가 드롭+로그)
    src = Path(app_root) / "assets" / SFX_DIR_NAME / meta["file"]
    if not src.is_file():
        raise KeyError(sfx_id)
    dest_dir = Path(run_dir) / STICKER_RUN_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if not dest.exists():
        shutil.copy2(src, dest)
    return f"{STICKER_RUN_SUBDIR}/{src.name}"


def sfx_prompt_block(manifest: dict[str, dict[str, Any]], *, max_sfx: int,
                     gain_db: float, target_beats: list[str]) -> str:
    """compose_style 프롬프트에 덧붙는 SFX 절. 목록이 비면 빈 문자열(= SFX 언급 없음 —
    기본 프롬프트는 동결이라 이 절이 없으면 LLM 은 sfx 어휘 자체를 모른다)."""
    if not manifest:
        return ""
    beats = " · ".join(SFX_BEAT_TEXT.get(b, b) for b in target_beats)
    lines = [
        "",
        "[효과음 (SFX) — 아래 목록의 id 만]",
        '출력에 "sfx" 배열을 더할 수 있다: '
        '[{"id": "...", "source_time_sec": 743.2, "gain_db": -6, "reason": "한 줄"}]',
        f"- **큰 비트에만**({beats}) — 편당 {max_sfx}개 이하. 벤치마크 문법은 절제다: "
        f"넣을 이유가 없으면 배열을 비워라.",
        "- ⚠ 라벨(효과 텍스트) 등장에는 소리를 붙이지 마라 — 라벨은 무음으로 뜬다.",
        f"- 좌표는 다른 항목과 같은 **원본 절대초**. gain_db 는 "
        f"{SFX_GAIN_RANGE[0]:g}~{SFX_GAIN_RANGE[1]:g}(기본 {gain_db:g} — 대사보다 작게).",
        "[번들 SFX 목록]",
        *[f"- {sid}: {m['desc'] or sid}" for sid, m in sorted(manifest.items())],
    ]
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────
# 검증·정규화
# ─────────────────────────────────────────────────────────────────────────
def _cap(items: list, limit: int, what: str, notes: list[str]) -> list:
    """하드캡 — 넘으면 앞에서부터 자르고 **반드시 기록**한다."""
    if len(items) <= limit:
        return items
    notes.append(f"{what} {len(items)}건 중 상한 {limit}건까지만 씀(앞에서부터)")
    return items[:limit]


def _num(value: Any, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise StylePlanError(f"{where}: 숫자가 아닙니다({value!r})") from None


# ── 두부 방지 (2026-09-01) — 폰트에 글리프가 없는 글자를 AI 산출에서 뺀다 ───────
# 제목에는 이중 차단이 있었는데(스토리 프롬프트 + _strip_emoji) **연출에는 한 겹도**
# 없었다: AI 가 "즉석 라이브 폭발 💥" 를 냈고 화면에 ⊠ 로 나갔다(가왕쇼_9bca673b).
# 정규식이 아니라 cmap 을 보는 이유는 glyphs.py 머리말 — 폰트마다 되는 기호가 다르다
# (Jalnan 은 ★♪‼ 를 그리고 Griun 은 못 그린다).
# AI 산출은 **제거 + note**, 사람이 보낸 값은 **거절**이다(edit_overrides 쪽). 사람 문구를
# 조용히 지우면 '고친 값이 반영 안 된 채 나간다'는 최악을 우리 손으로 만드는 셈이다.
def _tofu_strip(text: Any, font: Any, app_root: Path) -> tuple[str, list[str]]:
    """(문구, 뺀 글자들). 폰트를 못 읽으면 원문 그대로(fail-open)."""
    from app.modules.glyphs import font_charset, strip_missing
    if not font:
        return str(text or ""), []
    return strip_missing(text, font_charset(str(font), app_root))


def _strip_tofu_texts(texts: list[dict[str, Any]], app_root: Path,
                      notes: list[str]) -> None:
    """효과 텍스트에서 그 항목의 **자기 폰트**에 없는 글자를 뺀다. 항목마다 폰트가 다르다.

    빈 문구만 남으면 그 항목을 지우는 대신 **남긴다** — 여기서 지우면 뒤따르는 배치·
    클램프 기록과 개수가 어긋나고, 빈 텍스트는 어차피 화면에 아무것도 안 그린다."""
    for t in texts:
        if not isinstance(t, dict):
            continue
        kept, tofu = _tofu_strip(t.get("text"), t.get("font"), app_root)
        if tofu:
            t["text"] = kept
            notes.append(f"효과 텍스트 {str(t.get('text'))[:12]!r}: 폰트"
                         f"({t.get('font')})에 없는 글자 {''.join(tofu)} 제거 — "
                         f"그대로 두면 화면에 두부(□)로 나간다")


def _strip_tofu_titles(segs: list[dict[str, Any]], title_font: Any, app_root: Path,
                       notes: list[str]) -> None:
    """시간대별 제목 창의 문구에서 제목 폰트에 없는 글자를 뺀다.

    창 문구가 통째로 사라지면 그 창을 버린다 — 빈 창을 남기면 그 시간에 제목이 없는
    화면이 되고, 그건 '제목은 편 내내 떠 있어야 한다'(2026-08-24 지시)를 어긴다.
    창이 빠진 시간은 배치 단계의 빈틈 잇기(fill_title_gaps)가 직전 문구로 메운다."""
    if not title_font:
        return
    keep = []
    for sg in segs:
        kept, tofu = _tofu_strip(sg.get("text"), title_font, app_root)
        if not tofu:
            keep.append(sg)
            continue
        if kept.strip():
            sg["text"] = kept.strip()
            keep.append(sg)
            notes.append(f"제목 창에서 제목 폰트에 없는 글자 {''.join(tofu)} 제거")
        else:
            notes.append(f"제목 창 {str(sg.get('text'))[:12]!r} 이 통째로 폰트에 없는 "
                         f"글자뿐이라 창을 버린다 — 그 시간은 직전 문구가 잇는다")
    segs[:] = keep


# ── 제목 창의 주인은 누구인가 (2026-09-01) ──────────────────────────────────
# 우선순위를 **한 곳에서** 정한다. 파이프라인 if/elif 안에만 있으면 조건 하나가 바뀔 때
# 아무도 못 알아채고, 실제로 그렇게 새어 나간 것이 아래 'editor_title' 경우다.
TITLE_OWNER_EDITOR_SEGMENTS = "editor_segments"   # 사람이 창을 직접 보냈다
TITLE_OWNER_EDITOR_TITLE = "editor_title"         # 사람이 제목만 고쳤다 → AI 창 폐기
TITLE_OWNER_AI = "ai"                             # AI 창을 얹는다(종전)
TITLE_OWNER_NONE = "none"                         # 창 없음 — 기본 제목이 편 전체


def title_windows_owner(ai_segments: Any, editor_segments: Any,
                        editor_top_title: Any) -> str:
    """시간대별 제목을 누가 정하는가. 순수 — 테스트 대상.

    ① 사람이 창을 보냈으면 그 창(종전 계약 — 구간별 제목을 원한 의도).
    ② 사람이 **제목만** 고쳤으면 AI 창을 버린다(2026-09-01 사용자 지시).
       E18-1 의 빈틈 메우기가 창을 편 전체로 늘려 top_title 의 상영 시간이 0초가 됐고,
       사람이 제목을 몇 번을 고쳐도 화면이 안 바뀌었다(가왕쇼_9bca673b 실사고).
       창을 **지우는** 것으로는 못 막는다 — 키가 없으면 엔진이 AI 창을 다시 얹는다.
    ③ 그 외에는 종전대로 AI 창.
    """
    if editor_segments:
        return TITLE_OWNER_EDITOR_SEGMENTS
    if not ai_segments:
        return TITLE_OWNER_NONE
    if str(editor_top_title or "").strip():
        return TITLE_OWNER_EDITOR_TITLE
    return TITLE_OWNER_AI


def validate_plan(
    plan: Any,
    *,
    manifest: dict[str, dict[str, Any]],
    app_root: Path,
    run_dir: Path,
    max_texts: int | None = None,
    sfx_manifest: dict[str, dict[str, Any]] | None = None,
    max_sfx: int | None = None,
    sfx_gain_db: float = -6.0,
    title_font: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """style_plan/v1 → (정규화된 플랜, 기록할 메모들). 계약 위반이면 StylePlanError.

    반환 플랜의 모양은 **v3 자료 그대로**다:
      texts[]           — overrides_texts 가 기본값을 채운 완전한 dict (앵커 좌표 유지)
      images[]          — file 이 run_dir 상대 경로로 바뀐 v3 images
      subtitle_styles[] — {source_time_sec, style{size?,color?}}
      title_fixed       — 편 전체에서 안 바뀌는 **윗줄**(없으면 스토리 title_line1)
      title_segments[]  — {text, from_anchor, to_anchor} — text 는 **아랫줄 한 줄뿐**
                          (윗줄 결합·빈틈 잇기는 배치 단계에서)
      tts[]             — {source_time_sec, voice?, speed?}
      design{}          — CLI 단수 키 (파생은 design_overrides 가 한다)

    항목 단위 실패(없는 스티커 id 등)는 드롭+메모이고, **구조가 깨진 플랜**(스키마 오류·
    범위 밖 숫자·모르는 키)은 예외다 — 후자는 프롬프트가 계약을 못 지킨 것이라 재시도해야
    하고, 전자는 재시도해도 같은 결과다.
    """
    notes: list[str] = []
    if not isinstance(plan, dict):
        raise StylePlanError(f"플랜은 JSON 객체여야 합니다({type(plan).__name__})")
    schema = str(plan.get("schema") or "")
    if schema != SCHEMA:
        raise StylePlanError(f"알 수 없는 스키마: {schema!r} (기대: {SCHEMA})")

    known = {"schema", "texts", "subtitle_styles", "images", "title_segments",
             "title_fixed", "tts", "design", "notes", "sfx"}
    unknown = [k for k in plan if k not in known]
    if unknown:
        raise StylePlanError(f"모르는 최상위 키 {unknown} — 계약은 {sorted(known - {'schema'})} 뿐입니다")

    out: dict[str, Any] = {}

    # ── texts / images: v3 검증기에 그대로 태운다 ─────────────────────────
    # 효과 텍스트 캡은 톤 프로파일(E19-1)이 키울 수 있다 — 상한은 프로파일 로더가
    # 이미 검증했다(style_tone.DENSITY_MAX_LIMIT). None(톤 미지정) = 종전 8 그대로.
    texts = _cap(list(plan.get("texts") or []),
                 max_texts if max_texts is not None else MAX_TEXTS, "효과 텍스트", notes)
    texts = [{k: v for k, v in t.items() if k != "reason"} if isinstance(t, dict) else t
             for t in texts]

    images: list[dict[str, Any]] = []
    for i, im in enumerate(_cap(list(plan.get("images") or []), MAX_IMAGES, "스티커", notes)):
        if not isinstance(im, dict):
            raise StylePlanError(f"images[{i}]: 객체여야 합니다")
        item = {k: v for k, v in im.items() if k not in ("reason", "sticker")}
        sid = str(im.get("sticker") or "").strip()
        if not sid:
            raise StylePlanError(f"images[{i}]: sticker(스티커 id)가 필요합니다 — "
                                 f"엔진은 AI 에게 파일 경로를 받지 않습니다")
        try:
            item["file"] = stage_sticker(sid, manifest, app_root, run_dir)
        except KeyError:
            notes.append(f"없는 스티커 id {sid!r} → 그 항목 드롭")
            continue
        images.append(item)

    # v3 검증기 전면 재사용 — 모르는 키·범위 밖·폰트 화이트리스트·경로 탈출이 여기서 걸린다.
    # (v3 는 스탬프가 있어야 검증을 돌려 준다 — 이 문서는 검증만 받고 버린다)
    if texts or images:
        probe: dict[str, Any] = {"schema": SCHEMA_V3}
        if texts:
            probe["texts"] = texts
        if images:
            probe["images"] = images
        try:
            validate_overrides(probe)
        except EditOverrideError as e:
            raise StylePlanError(str(e)) from None
    if texts:
        out["texts"] = overrides_texts({"texts": texts}) or []
        _strip_tofu_texts(out["texts"], app_root, notes)
    if images:
        out["images"] = images

    # ── 효과음 (E19-5) ───────────────────────────────────────────────────
    # 게이트: 톤 프로파일 sfx 절(max_sfx)이 있고 번들(manifest)이 비어 있지 않을 때만.
    # 닫힌 채널에 sfx 가 실려 오면 전량 드롭+기록 — 프롬프트에 없던 어휘를 LLM 이
    # 지어냈다는 신호라 조용히 무시하면 안 된다(unknown 거절은 과하다: 다른 연출까지
    # 통째로 날아간다 — title_rotate 의 '받되 버린다' 규율).
    _sfx_raw = list(plan.get("sfx") or [])
    if _sfx_raw and (max_sfx is None or not sfx_manifest):
        notes.append(f"효과음 {len(_sfx_raw)}건 — 이 채널은 SFX 가 닫혀 있어(톤 프로파일 "
                     f"sfx 절/번들 없음) 전량 버립니다")
        _sfx_raw = []
    sfx_items: list[dict[str, Any]] = []
    for i, sf in enumerate(_cap(_sfx_raw, max_sfx or 0, "효과음", notes)):
        if not isinstance(sf, dict):
            raise StylePlanError(f"sfx[{i}]: 객체여야 합니다")
        t = _num(sf.get("source_time_sec"), f"sfx[{i}]: source_time_sec")
        if t < 0:
            raise StylePlanError(f"sfx[{i}]: source_time_sec 은 0 이상이어야 합니다")
        sid = str(sf.get("id") or "").strip()
        if not sid:
            raise StylePlanError(f"sfx[{i}]: id 가 필요합니다 — 엔진은 AI 에게 파일 "
                                 f"경로를 받지 않습니다(스티커와 같은 규율)")
        g = sf.get("gain_db")
        gain = float(g) if isinstance(g, (int, float)) and not isinstance(g, bool) \
            else float(sfx_gain_db)
        lo, hi = SFX_GAIN_RANGE
        if not (lo <= gain <= hi):
            clamped = min(max(gain, lo), hi)
            notes.append(f"sfx[{i}] {sid!r}: gain_db {gain:g} 이 범위 밖 → {clamped:g} 로 클램프")
            gain = clamped
        try:
            rel = stage_sfx(sid, sfx_manifest or {}, app_root, run_dir)
        except KeyError:
            notes.append(f"없는 SFX id {sid!r} → 그 항목 드롭")
            continue
        sfx_items.append({"file": rel, "source_time_sec": t, "gain_db": gain})
    if sfx_items:
        out["sfx"] = sfx_items

    # ── 자막 강조 ────────────────────────────────────────────────────────
    subs: list[dict[str, Any]] = []
    for i, s in enumerate(_cap(list(plan.get("subtitle_styles") or []),
                               MAX_SUBTITLE_STYLES, "자막 강조", notes)):
        if not isinstance(s, dict):
            raise StylePlanError(f"subtitle_styles[{i}]: 객체여야 합니다")
        t = _num(s.get("source_time_sec"), f"subtitle_styles[{i}]: source_time_sec")
        if t < 0:
            raise StylePlanError(f"subtitle_styles[{i}]: source_time_sec 은 0 이상이어야 합니다")
        style = s.get("style")
        if not isinstance(style, dict) or not style:
            raise StylePlanError(f"subtitle_styles[{i}]: style 은 비어 있지 않은 객체여야 합니다")
        bad = [k for k in style if k not in STYLE_SUBTITLE_KEYS]
        if bad:
            raise StylePlanError(
                f"subtitle_styles[{i}]: style 에 모르는 키 {bad} — AI 가 쓸 수 있는 것은 "
                f"{'/'.join(STYLE_SUBTITLE_KEYS)} 뿐입니다(위치·회전은 사람 전용)")
        norm: dict[str, Any] = {}
        if style.get("size") is not None:
            sz = _num(style["size"], f"subtitle_styles[{i}]: size")
            lo, hi = SUBTITLE_SIZE_RANGE
            if not (lo <= sz <= hi):
                raise StylePlanError(
                    f"subtitle_styles[{i}]: size {sz:g} 가 범위 밖입니다 ({lo:g}~{hi:g})")
            norm["size"] = sz
        if style.get("color") is not None:
            col = str(style["color"])
            if not (len(col) == 7 and col.startswith("#")
                    and all(c in "0123456789abcdefABCDEF" for c in col[1:])):
                raise StylePlanError(f"subtitle_styles[{i}]: color 는 '#RRGGBB' 여야 합니다({col!r})")
            norm["color"] = col.upper()
        subs.append({"source_time_sec": t, "style": norm})
    if subs:
        out["subtitle_styles"] = subs

    # ── 시간대별 제목 (앵커 쌍) ───────────────────────────────────────────
    segs: list[dict[str, Any]] = []
    for i, sg in enumerate(_cap(list(plan.get("title_segments") or []),
                                MAX_TITLE_SEGMENTS, "시간대별 제목", notes)):
        if not isinstance(sg, dict):
            raise StylePlanError(f"title_segments[{i}]: 객체여야 합니다")
        text = str(sg.get("text") or "").strip()
        if not text:
            raise StylePlanError(f"title_segments[{i}]: text 가 비었습니다")
        # 2줄 형식 고정(E21, 2026-08-25) — 창이 바꾸는 것은 **아랫줄 한 줄뿐**이고 윗줄은
        # 편 전체에서 고정이다. 두 줄을 통째로 보내던 종전 산출은 여기서 걸린다(조용히
        # 이어 붙이면 3줄 제목이 나간다).
        if "\n" in text:
            raise StylePlanError(
                f"title_segments[{i}]: text 는 한 줄이어야 합니다 — 창은 아랫줄만 "
                f"바꾸고 윗줄(title_fixed)은 편 전체에서 고정입니다")
        if len(text) > MAX_TITLE_LINE_CHARS:
            raise StylePlanError(
                f"title_segments[{i}]: text 가 {len(text)}자입니다 — "
                f"{MAX_TITLE_LINE_CHARS}자 이내여야 합니다(넘으면 줄이 접혀 3줄이 됩니다)")
        a = _num(sg.get("from_anchor"), f"title_segments[{i}]: from_anchor")
        b = _num(sg.get("to_anchor"), f"title_segments[{i}]: to_anchor")
        if a < 0 or b <= a:
            raise StylePlanError(
                f"title_segments[{i}]: 앵커 구간이 잘못됐습니다 (from {a:g} → to {b:g})")
        segs.append({"text": text, "from_anchor": a, "to_anchor": b})
    if segs:
        _strip_tofu_titles(segs, title_font, app_root, notes)
        out["title_segments"] = segs

    # ── 고정 윗줄 (제목 창과 한 벌) ───────────────────────────────────────
    if plan.get("title_fixed") is not None:
        fixed, _tofu = _tofu_strip(plan["title_fixed"], title_font, app_root)
        fixed = fixed.strip()
        if _tofu:
            notes.append(f"title_fixed 에서 제목 폰트에 없는 글자 {''.join(_tofu)} 제거 "
                         f"— 그대로 두면 화면에 두부(□)로 나간다")
        if not fixed:
            raise StylePlanError("title_fixed 가 비었습니다 (안 쓸 거면 키를 빼세요)")
        if "\n" in fixed:
            raise StylePlanError("title_fixed 는 한 줄이어야 합니다 (제목 윗줄 하나)")
        if len(fixed) > MAX_TITLE_LINE_CHARS:
            raise StylePlanError(
                f"title_fixed 가 {len(fixed)}자입니다 — {MAX_TITLE_LINE_CHARS}자 이내여야 "
                f"합니다(넘으면 줄이 접혀 제목이 3줄이 됩니다)")
        if segs:
            out["title_fixed"] = fixed
        else:
            # 창이 없으면 붙일 자리가 없다 — 제목 전체를 갈아 끼우는 값이 아니다.
            notes.append("title_fixed 는 제목 창과 같이 쓰는 값입니다 — 창이 없어 무시")

    # ── 내레이션 톤 ──────────────────────────────────────────────────────
    tts: list[dict[str, Any]] = []
    for i, c in enumerate(list(plan.get("tts") or [])):
        if not isinstance(c, dict):
            raise StylePlanError(f"tts[{i}]: 객체여야 합니다")
        item: dict[str, Any] = {
            "source_time_sec": _num(c.get("source_time_sec"), f"tts[{i}]: source_time_sec")}
        if c.get("voice") is not None:
            v = str(c["voice"])
            if v not in STYLE_VOICES:
                raise StylePlanError(
                    f"tts[{i}]: 모르는 voice {v!r} — 계약 라벨은 {', '.join(STYLE_VOICES)} 뿐입니다"
                    f"(elevenlabs: 접두사는 계정 종속이라 AI 산출에 쓰지 않습니다)")
            item["voice"] = v
        if c.get("speed") is not None:
            sp = str(c["speed"])
            if sp not in STYLE_SPEEDS:
                raise StylePlanError(
                    f"tts[{i}]: 모르는 speed {sp!r} — {', '.join(STYLE_SPEEDS)} 뿐입니다")
            item["speed"] = sp
        if len(item) > 1:                              # voice·speed 둘 다 없으면 무의미
            tts.append(item)
    if tts:
        out["tts"] = tts

    # ── 디자인 ───────────────────────────────────────────────────────────
    design = plan.get("design") or {}
    if design:
        if not isinstance(design, dict):
            raise StylePlanError("design 은 객체여야 합니다")
        bad = [k for k in design
               if k not in STYLE_DESIGN_ALLOWED and k not in STYLE_DESIGN_IGNORED]
        if bad:
            raise StylePlanError(
                f"design 에 모르는(또는 AI 에게 열리지 않은) 키 {bad} — "
                f"허용: {', '.join(STYLE_DESIGN_ALLOWED)}. 밴드 레이아웃·폰트·자막 색 체계·"
                f"배속은 채널 정체성이라 AI 가 바꾸지 않습니다")
        norm_design: dict[str, Any] = {}
        for k, v in design.items():
            if k in STYLE_DESIGN_IGNORED:
                # 받되 버린다(위 상수 주석) — 조용히는 아니고 건별로 남긴다.
                notes.append(f"design.{k} 는 AI 에게 닫힌 키입니다 — 값 {v!r} 무시"
                             f"({STYLE_DESIGN_IGNORED_WHY.get(k, '사람이 정합니다')})")
                continue
            if k == "tts_rotate":
                deg = _num(v, f"design.{k}")
                if not (-ROTATE_RANGE_DEG <= deg <= ROTATE_RANGE_DEG):
                    raise StylePlanError(
                        f"design.{k}: {deg:g} 가 범위 밖입니다 (-180~180)")
                norm_design[k] = deg
            elif k in ("title_box", "title_box2"):
                if str(v) not in TITLE_BOX_KINDS:
                    raise StylePlanError(
                        f"design.{k}: {v!r} — {'/'.join(TITLE_BOX_KINDS)} 중 하나여야 합니다")
                norm_design[k] = str(v)
            else:                                       # title_box_color, title_box_color2
                norm_design[k] = str(v)
        if norm_design:
            out["design"] = norm_design

    if plan.get("notes"):
        out["notes"] = str(plan["notes"])[:300]
    return out, notes


# ─────────────────────────────────────────────────────────────────────────
# 적용 (우선순위 · 파생)
# ─────────────────────────────────────────────────────────────────────────
def design_overrides(plan_design: dict[str, Any] | None,
                     explicit: set[str] | None,
                     base_design: Any) -> tuple[dict[str, Any], list[str]]:
    """AI design 키 → `dataclasses.replace` 인자. **채널이 명시한 키는 AI 가 못 덮는다**.

    `explicit` 은 CLI 가 실제로 받은 design 필드 이름들(`--design-*` 로 명시된 것).
    비교를 '기본값과 다른가'로 하면 안 된다 — 채널이 기본값과 같은 값을 명시한 경우와
    아무것도 안 준 경우를 구분할 수 없고, 그러면 사람이 정한 값이 AI 에게 덮인다.

    반환: (replace 인자, 사람이 읽을 메모들)
    """
    if not plan_design:
        return {}, []
    explicit = explicit or set()
    notes: list[str] = []
    kwargs: dict[str, Any] = {}

    def _blocked(field: str, key: str) -> bool:
        if field in explicit:
            notes.append(f"design.{key} 는 채널이 명시한 값이 이깁니다 — AI 값 무시")
            return True
        return False

    # ⚠ title_rotate 는 여기서 **한 번 더** 막는다 — validate_plan 이 이미 버리지만,
    #   E17-1 시절(±15° 허용)에 저장된 checkpoint_style.json 은 재검증 없이 그대로
    #   재적용된다(E15 재개 계약). 그 경로로 되살아나면 지시가 무력해진다.
    for _closed in STYLE_DESIGN_IGNORED:
        if _closed in plan_design:
            notes.append(f"design.{_closed} 는 AI 에게 닫힌 키입니다 — 값 "
                         f"{plan_design[_closed]!r} 무시(옛 체크포인트 포함)")
    if "tts_rotate" in plan_design and not _blocked("tts_rotate", "tts_rotate"):
        kwargs["tts_rotate"] = float(plan_design["tts_rotate"])

    # 줄별 리스트 2종은 cli._build_design_config 와 **같은 조립**이다: 기본값에서 시작해
    # 지정한 줄만 치환한다(한 줄만 바꿔도 다른 줄은 그대로). 렌더러는 리스트만 읽는다.
    # ⚠ `title_bolds` 는 여기 없다 — 굵게는 AI 에게 닫혔다(STYLE_DESIGN_IGNORED, E21-1).
    #   채널이 `--design-title-bold` 로 준 값은 base_design 에 이미 있어 그대로 간다.
    for keys, field, default in (
        (("title_box", "title_box2"), "title_boxes", list(base_design.title_boxes)),
        (("title_box_color", "title_box_color2"), "title_box_colors",
         list(base_design.title_box_colors)),
    ):
        if not any(k in plan_design for k in keys):
            continue
        if _blocked(field, keys[0]):
            continue
        values = list(default)
        for idx, k in enumerate(keys):
            if k in plan_design:
                values[idx] = plan_design[k]
        kwargs[field] = values
    return kwargs, notes


# 제목 창 사이의 빈 구간 처리 기준 (E18, 2026-08-24 → **E21 에서 규칙이 바뀌었다**).
# E18 은 이보다 긴 틈을 '기본 제목'으로 메웠지만, E21 부터는 길이와 무관하게 **직전 창을
# 늘려** 잇는다(아래 fill_title_gaps 주석). 상수는 '이보다 짧은 틈은 메모조차 남기지
# 않는다'는 잡음 기준으로만 남는다 — 어느 쪽이든 결과는 같다: **빈 시간이 남지 않는다.**
MIN_TITLE_GAP_SEC = 0.4


def split_title_lines(title_text: str) -> tuple[str, str]:
    """편 제목("윗줄\n아랫줄") → (윗줄, 아랫줄). 줄이 하나뿐이면 아랫줄은 빈 문자열.

    스토리 단계가 만든 `title_line1\ntitle_line2` 가 정본이고(폴백은 topic 한 줄), 여기서는
    **읽기만** 한다 — 제목 문구를 정하는 것은 스토리 단계다.
    """
    parts = [ln.strip() for ln in str(title_text or "").split("\n")]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def join_title_lines(fixed: str, second: str) -> str:
    """(윗줄, 아랫줄) → 렌더러가 받는 제목 문자열. 한쪽이 비면 한 줄짜리."""
    fixed, second = str(fixed or "").strip(), str(second or "").strip()
    if fixed and second:
        return f"{fixed}\n{second}"
    return fixed or second


def fill_title_gaps(segs: list[dict[str, Any]], base_title: str,
                    total_sec: float) -> tuple[list[dict[str, Any]], list[str]]:
    """제목 창 목록의 빈 시간을 없앤다 → (창 목록, 메모). 순수(테스트 대상).

    ⚠ **E21(2026-08-25)에서 메우는 내용이 바뀌었다** — 빈틈은 이제 `base_title` 이 아니라
    **직전 창의 문구**가 잇는다. E18 은 틈마다 기본 제목을 끼웠는데, 기본 제목의 아랫줄은
    스토리가 만든 **결말 후킹**이라 아직 그 내용이 아닌 앞 구간에 붙으면 어긋난다.
    사용자 지적(유재석 캠프 편): "앞에는 바비큐 안 좋아하는 내용이라" — 그 편은 앞 구간이
    '바비큐 안 좋아한다더니', 맛본 뒤가 '고기 앞에서 무너짐'이었다. **아직 안 나온 내용이
    미리 새면 안 된다**가 규칙의 이유다. 직전 문구는 '방금까지 나온 내용'이라 항상 맞는다.
    `base_title` 은 **AI 경로 표시**로 남는다(사람 경로는 안 주므로 종전 그대로).

    **사용자 지시(2026-08-24): "ai가 작업할 때는 제목은 무조건 있어야 해".**

    E8 렌더 계약은 '창 밖 = 제목 없음'이고 그건 **사람이 쓰는 기능**이다(편집실에서
    중반부터 제목을 걷어내는 연출). 그런데 AI 는 그걸 의도가 아니라 부주의로 만든다 —
    실측(2026-08-24, 제목 창이 실제로 들어간 3편):

        김부장_e37253c2    창 2개 → 55.31s / 55.31s   제목 없음 0s      정상
        가왕쇼_b5ec784a    창 1개 → 17.21s / 53.17s   제목 없음 36.0s
        혜미리예채파_2b2b46c6 창 1개 → 18.50s / 51.00s   제목 없음 32.5s

    2/3 편이 대부분의 시간 동안 제목 없이 나갔다(가왕쇼는 SHOTCONE 이 아니라 KR 채널 —
    이건 현지화 문제가 아니라 **전 채널 문제**다). 그래서 **AI 경로에서만** 구멍을 메운다.

    ⚠ 사람 경로(`edit_overrides.title.segments`)는 **손대지 않는다** — 사람이 비운 것은
      의도다. 이 함수는 style_compose 에서만 불린다.

    규칙:
      · 창 끝은 total 로 클램프하고, total 밖에서 시작하는 창은 버린다(메모).
      · 틈이 MIN_TITLE_GAP_SEC 이상이면 **기본 제목**으로 메운다(내용을 지어내지 않는다).
      · 그보다 짧으면 **앞 창을 늘려** 잇는다(깜빡임 방지). 맨 앞 틈은 첫 창을 0 으로 당긴다.
    """
    notes: list[str] = []
    total = float(total_sec or 0.0)
    if total <= 0 or not base_title:
        return list(segs), notes

    kept: list[dict[str, Any]] = []
    for sg in sorted(segs, key=lambda x: (float(x["start_sec"]), float(x["end_sec"]))):
        st, en = float(sg["start_sec"]), min(float(sg["end_sec"]), total)
        if st >= total - 0.05 or en <= st:
            notes.append(f"제목 창이 영상({total:.1f}초) 밖이라 드롭: "
                         f"[{sg['start_sec']}, {sg['end_sec']}] {str(sg.get('text',''))[:16]!r}")
            continue
        item = dict(sg)
        item["start_sec"], item["end_sec"] = st, en
        kept.append(item)
    if not kept:
        notes.append("남은 제목 창이 없어 기본 제목으로 되돌립니다(제목은 항상 있어야 합니다)")
        return [], notes

    out: list[dict[str, Any]] = []
    cursor = 0.0
    for item in kept:
        gap = item["start_sec"] - cursor
        if gap > 0:
            if out:
                out[-1]["end_sec"] = item["start_sec"]   # 직전 제목이 그 자리를 잇는다
                if gap >= MIN_TITLE_GAP_SEC:
                    notes.append(f"제목 빈 구간 {cursor:.2f}~{item['start_sec']:.2f}초 "
                                 f"→ 직전 제목이 이어짐")
            else:
                item["start_sec"] = 0.0                  # 맨 앞 틈은 첫 창을 당긴다
                if gap >= MIN_TITLE_GAP_SEC:
                    notes.append(f"제목 시작 전 {gap:.2f}초 → 첫 제목을 0초로 당김")
        out.append(item)
        cursor = item["end_sec"]
    if total > cursor:
        if total - cursor >= MIN_TITLE_GAP_SEC:
            notes.append(f"제목 빈 구간 {cursor:.2f}~{total:.2f}초 → 마지막 제목이 이어짐")
        out[-1]["end_sec"] = total                       # 꼬리는 마지막 창이 문다
    return out, notes


def title_segments_from_anchors(segs: list[dict[str, Any]], clips: list,
                                *, base_title: str = "", fixed_line: str = "",
                                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """앵커 쌍(from/to, 원본 절대초) → E8 `title.segments`(편집본 시간축).

    `base_title` 을 주면 창 사이의 빈 시간을 없앤다(`fill_title_gaps`) —
    **AI 가 만든 창은 제목을 없애지 못한다**(2026-08-24 사용자 지시). 안 주면 종전대로다.

    `fixed_line` 을 주면 창의 text 를 **아랫줄로 보고 그 위에 윗줄을 붙인다**(E21,
    2026-08-25 사용자 지시: "다른 쇼츠들처럼 두 줄 형식은 유지하고 위에 큰 제목은 고정,
    아랫줄만 바뀌게"). 모든 창의 첫 줄이 같아지므로 화면에서는 윗줄이 고정돼 보이고,
    줄별 색·크기(`title_colors`·`title_sizes`)도 종전 그대로 적용된다.
    ⚠ 이미 두 줄인 text(E21 이전 체크포인트)는 **그대로 둔다** — 재개는 플랜을 재검증
      없이 재적용하므로(E15 계약) 윗줄을 또 붙이면 3줄 제목이 나간다.

    변환은 자막·이미지 앵커와 **같은 규칙**(담은 클립의 편집 오프셋 + 클립 내 상대시각)을
    쓴다 — 배치 함수(place_anchored_images)를 그대로 재사용해 수식을 베끼지 않는다.
    창 밖 앵커·0.1초 미만은 드롭(고아 규칙). 겹침·개수 상한은 E8 검증기가 본다.

    ⚠ 창 길이는 `to_anchor - from_anchor`(원본 기준)를 **편집본 길이로 그대로** 쓴다 —
    v3 images/texts 의 duration 규약과 같다. 두 앵커가 서로 다른 클립에 걸치고 그
    사이가 잘려 나갔다면 창이 의도보다 길어질 수 있다(한 클립 안이면 정확하다).
    길어진 창이 다음 창을 침범하면 아래 겹침 규칙이 뒤엣것을 버린다.
    """
    from app.modules.edit_overrides import place_anchored_images

    if not segs:
        return [], []
    # place_anchored_images 의 입력 모양(source_time_sec + duration_sec)으로 바꿔 태운다.
    probe = [{"source_time_sec": s["from_anchor"],
              "duration_sec": max(0.001, s["to_anchor"] - s["from_anchor"]),
              "text": s["text"]} for s in segs]
    placed, dropped = place_anchored_images(probe, clips)
    out = [{"text": (p["text"] if "\n" in p["text"]
                     else join_title_lines(fixed_line, p["text"])),
            "start_sec": p["start_sec"], "end_sec": p["end_sec"]}
           for p in placed]
    out.sort(key=lambda x: (x["start_sec"], x["end_sec"]))
    # 겹침은 E8 계약상 거절이다(제목은 한 벌 자리라 포개진다). AI 산출이 겹치면 뒤엣것을
    # 버린다 — 전량 실패시키면 연출 하나 때문에 제목이 통째로 사라진다.
    deduped: list[dict[str, Any]] = []
    for seg in out:
        if deduped and seg["start_sec"] < deduped[-1]["end_sec"] - 1e-9:
            dropped.append({"source_time_sec": None, "text": seg["text"],
                            "why": "앞 제목 창과 겹침"})
            continue
        deduped.append(seg)
    if base_title:
        total = sum(max(0.0, float(c.end_sec) - float(c.start_sec)) for c in clips)
        deduped, fill_notes = fill_title_gaps(deduped, base_title, total)
        for why in fill_notes:
            dropped.append({"source_time_sec": None, "text": "", "why": why, "note": True})
    if deduped:
        validate_title_segments(deduped)               # E8 검증기(개수·범위·겹침) 재사용
    return deduped, dropped


# ─────────────────────────────────────────────────────────────────────────
# 효과 텍스트는 **영상 밴드 안**에만 놓는다 (E18, 2026-08-24)
# ─────────────────────────────────────────────────────────────────────────
# 밴드 아래로 이만큼은 대사 자막 구역이라 비워 둔다(margin_v 430 + 2줄 블록 여유).
SUBTITLE_RESERVE_PX = 520
# 밴드 모서리에 딱 붙이지 않는다 — 글자가 영상 경계에 걸치면 읽기 어렵다.
TEXT_BAND_PAD_PX = 24


def text_y_range(design: Any, *, canvas_height: int = 1920) -> tuple[float, float]:
    """효과 텍스트가 놓일 수 있는 y 비율 범위(글자 **중심** 기준). 순수.

    왜 밴드 안인가: 효과 텍스트는 '영상 위에 얹는 연출'이다. 밴드 **위**는 제목 블록
    자리이고(`dynamic_title_y = overlay_y − 블록높이 − 20`), 밴드 **아래**는 자막·
    작품명 자리다. 밴드로 가두면 두 충돌이 한꺼번에 사라진다.

    ⚠ 종전 프롬프트는 `y 0.15~0.35(위) 또는 0.60~0.72(아래)` 를 **하드코딩**해서 권했다.
      13:9·꽉 찬 폭·세로 중앙(SHOTCONE) 기하로 재면 제목 블록은 y 0.146~0.295 라
      권장 '위' 구간과 거의 완전히 겹친다 — 프롬프트가 제목 자리를 찍어서 권하고
      있었다(2026-08-24 실측: 효과 텍스트 2건이 제목 두 줄 위에 얹혔다).
      그래서 범위를 **그 편의 실제 기하에서 계산**해 프롬프트에 끼우고, 검증기도 같은
      범위로 클램프한다(프롬프트만 고치면 LLM 이 계속 벗어난다).

    밴드 기하는 `subtitle_region.band_geometry`(렌더러 [2]와 같은 순서)를 재사용한다 —
    수식을 베끼면 언젠가 화면과 어긋난다(E17-2 에서 정한 규율).
    """
    from app.modules.subtitle_region import band_geometry

    band = band_geometry(design, canvas_height=canvas_height)
    top = band.overlay_y + TEXT_BAND_PAD_PX
    bottom = min(band.overlay_y + band.scaled_h - TEXT_BAND_PAD_PX,
                 canvas_height - SUBTITLE_RESERVE_PX)
    if bottom <= top:            # 밴드가 아주 얕은 채널 — 밴드 중앙 한 점으로 접는다
        mid = band.overlay_y + band.scaled_h / 2.0
        top = bottom = mid
    return top / canvas_height, bottom / canvas_height


def clamp_texts_to_band(texts: list[dict[str, Any]], y_lo: float, y_hi: float,
                        *, canvas_height: int = 1920,
                        ) -> tuple[list[dict[str, Any]], list[str]]:
    r"""효과 텍스트의 y 를 밴드 안으로 당긴다 → (사본 목록, 메모). 순수(테스트 대상).

    **드롭이 아니라 클램프다**(2026-08-24 사용자 결정) — 연출 의도를 살리고 위치만
    조금 옮긴다. 옮긴 것은 건별로 남긴다(조용한 이동 금지).

    y 는 v3 계약상 **글자 중심**(`\an5\pos`)이라 글자 높이의 절반을 여유로 뺀다 —
    중심만 밴드 안에 넣으면 큰 글자는 위아래로 삐져나온다.
    """
    notes: list[str] = []
    if not texts:
        return list(texts or []), notes
    out: list[dict[str, Any]] = []
    for t in texts:
        item = dict(t)
        try:
            y = float(item.get("y"))
            size = float(item.get("size") or 0.0)
        except (TypeError, ValueError):
            out.append(item)                     # 값이 깨졌으면 v3 검증기가 이미 걸렀다
            continue
        half = (size * 0.6) / canvas_height      # 어센더·디센더까지 감안한 반높이
        lo, hi = y_lo + half, y_hi - half
        if lo > hi:                              # 글자가 밴드보다 크다 — 중앙에 놓는다
            lo = hi = (y_lo + y_hi) / 2.0
        new_y = min(max(y, lo), hi)
        if abs(new_y - y) > 1e-6:
            item["y"] = round(new_y, 4)
            notes.append(f"효과 텍스트 {str(item.get('text', ''))[:12]!r} 가 영상 밖"
                         f"(제목·자막 구역)이라 y {y:g} → {item['y']:g} 로 당겼습니다")
        out.append(item)
    return out, notes


def apply_subtitle_styles(final_segments: list, styles: list[dict[str, Any]],
                          clips: list) -> tuple[int, list[dict[str, Any]]]:
    """자막 강조를 `final_segments` 에 얹는다. 반환: (적용 건수, 드롭된 항목).

    매칭: 앵커(원본 절대초)를 편집본 시각으로 바꾼 뒤 **그 시각을 품은 자막 줄**을 찾는다.
    좌표 변환은 다른 앵커와 같은 함수를 태운다(수식 복제 금지 — 언젠가 어긋난다).
    이미 style 이 있는 줄(편집실이 보낸 것)은 **건드리지 않는다**(사람이 이긴다).
    """
    from app.modules.edit_overrides import place_anchored_images

    if not styles or not final_segments:
        return 0, list(styles or [])
    # ⚠ duration 은 **0.1초보다 커야 한다** — place_anchored_images 는 변환 후 길이가
    # 0.1초 미만이면 고아로 보고 드롭한다(v3 규칙). 여기서는 시작 시각만 쓰지만, 짧은
    # 프로브를 넣으면 멀쩡한 앵커가 전부 드롭된다(구현 중 실측으로 잡은 함정).
    probe = [{"source_time_sec": s["source_time_sec"], "duration_sec": 0.5,
              "_style": s["style"]} for s in styles]
    placed, dropped = place_anchored_images(probe, clips)
    applied = 0
    for p in placed:
        t = float(p["start_sec"])
        hit = next((seg for seg in final_segments
                    if float(seg.start_sec) - 1e-9 <= t < float(seg.end_sec) + 1e-9), None)
        if hit is None:
            dropped.append({"source_time_sec": None, "why": "그 시각에 자막 줄이 없음"})
            continue
        if getattr(hit, "style", None):
            continue                                    # 편집실이 이미 정한 줄 — 사람이 이긴다
        hit.style = dict(p["_style"])
        applied += 1
    return applied, dropped


# ─────────────────────────────────────────────────────────────
# E19-4: 라벨 얼굴 회피 · 컷 경계 재배치 (2026-08-28)
# ─────────────────────────────────────────────────────────────
# 발주서: docs/prompts/e19-drama-clip-preset.md §4. 벤치마크 실측(보고서 §12): 라벨은
# 인물 얼굴 옆 빈 공간에 붙고, 컷이 바뀌면 새 구도에 맞춰 재배치된다.
#
# 얼굴 좌표는 **리프레임 크롭 타임라인의 얼굴 박스를 재사용**한다(reframe.CropKeyframe
# 의 face_* 필드 — 검출을 두 번 돌리지 않는다). face_tracking 을 끈 채널·검출 실패
# 클립·구 캐시 JSON(face 키 없음)은 전부 '얼굴 없음' = 회피 없이 종전 배치 —
# 안전장치가 연출을 막으면 안 된다(E17-2 실패 규율).

# 라벨-얼굴 최소 간격(캔버스 px)과 얼굴 표본 허용 시차. 표본은 crop_sample_interval
# (기본 0.5s 안팎)로 찍히므로 0.75s 면 이웃 표본 하나는 반드시 잡힌다.
FACE_AVOID_GAP_PX = 12
FACE_SAMPLE_TOLERANCE_SEC = 0.75
# 조각이 이보다 짧으면 쪼개지 않는다(깜빡이는 라벨 조각 방지 — E18-1 의 틈 규율과 동류).
FACE_SPLIT_MIN_SEC = 0.15


def face_box_on_canvas(keyframes: list[dict[str, Any]], t_rel: float,
                       geom: Any, *, canvas_width: int = 1080,
                       ) -> tuple[float, float, float, float] | None:
    """크롭 키프레임 → 그 시각 얼굴의 **캔버스** 박스 (cx, cy, w, h). 없으면 None.

    변환은 렌더 체인과 같은 순서다: crop(cw×ch, 중심 x/y_center) → scale
    force_original_aspect_ratio=increase → 중앙 crop(scaled_w×scaled_h) → pad.
    키프레임 time_sec 는 원본 절대초이고 렌더는 첫 키프레임 기준으로 정규화하므로
    (renderer._build_crop_expr 의 t0), 여기도 같은 기준을 쓴다. 얼굴이 크롭 밖으로
    잘려 나갔으면 None — 화면에 없는 얼굴은 피할 것도 없다."""
    if not keyframes:
        return None
    t0 = float(keyframes[0].get("time_sec", 0.0))
    best = None
    best_d = FACE_SAMPLE_TOLERANCE_SEC
    for kf in keyframes:
        if float(kf.get("face_w", 0) or 0) <= 0:
            continue                       # 미검출 표본 · 구 캐시(face 키 없음)
        d = abs((float(kf.get("time_sec", 0.0)) - t0) - t_rel)
        if d <= best_d:
            best, best_d = kf, d
    if best is None:
        return None
    cw = float(best.get("crop_w", 0) or 0)
    ch = float(best.get("crop_h", 0) or 0)
    if cw <= 0 or ch <= 0:
        return None
    s = max(geom.scaled_w / cw, geom.scaled_h / ch)
    ox = (cw * s - geom.scaled_w) / 2.0
    oy = (ch * s - geom.scaled_h) / 2.0
    crop_left = float(best["x_center"]) - cw / 2.0
    crop_top = float(best["y_center"]) - ch / 2.0
    cx = geom.pad_x + (float(best["face_cx"]) - crop_left) * s - ox
    cy = geom.overlay_y + (float(best["face_cy"]) - crop_top) * s - oy
    w = float(best["face_w"]) * s
    h = float(best["face_h"]) * s
    band_right = geom.pad_x + geom.scaled_w
    band_bottom = geom.overlay_y + geom.scaled_h
    if cx < geom.pad_x or cx > band_right or cy < geom.overlay_y or cy > band_bottom:
        return None                        # 리프레임 크롭이 얼굴을 잘라낸 경우
    return (cx, cy, w, h)


def _label_box(item: dict[str, Any], canvas_w: int, canvas_h: int,
               ) -> tuple[float, float, float, float] | None:
    """라벨의 캔버스 박스 (cx, cy, w, h). 폭은 1em/글자 근사 — 한글·짧은 라벨에 충분
    (제목 강조 E19-2 의 폰트 폴백과 같은 근사)."""
    try:
        cx = float(item["x"]) * canvas_w
        cy = float(item["y"]) * canvas_h
        size = float(item.get("size") or 72.0)
    except (TypeError, ValueError, KeyError):
        return None
    lines = str(item.get("text", "")).split("\n")
    w = size * max(1, max(len(ln) for ln in lines))
    h = size * len(lines)
    return (cx, cy, w, h)


def _boxes_overlap(a: tuple[float, float, float, float],
                   b: tuple[float, float, float, float], gap: float) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (abs(ax - bx) < (aw + bw) / 2.0 + gap
            and abs(ay - by) < (ah + bh) / 2.0 + gap)


def avoid_faces_for_texts(
    texts: list[dict[str, Any]],
    clips: list,
    crop_map: dict[str, Path],
    design: Any,
    y_lo: float,
    y_hi: float,
    *,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """배치된(편집본 시간축) 효과 텍스트를 얼굴 비가림 자리로 옮긴다. 순수(사본만).

    - 후보는 **위 → 아래 → 옆(원위치에서 먼 쪽이 아니라 얼굴 반대쪽 먼저)** 순서로
      첫 성립 — 성립 조건은 얼굴 박스 비겹침 + 밴드 y 구간(E18-2 와 같은 글자 반높이
      여유) + 캔버스 가로 안. 어느 후보도 성립 못 하면 **옮기지 않고 기록**한다
      (E18-2 '살리고 당긴다' — 겹침이 증발보다 낫다).
    - 창이 클립 경계를 넘으면 경계에서 쪼개 조각마다 그 클립의 얼굴로 다시 배치한다
      (v3 texts 는 조각당 한 항목 — 렌더 계약 변경 없음). FACE_SPLIT_MIN_SEC 미만
      조각은 만들지 않는다.
    - 얼굴 정보가 없으면(face_tracking 끔·검출 실패·구 캐시) 아무것도 안 한다.

    반환: (새 목록, 건별 메모, {"of","split","moved","kept_overlap","no_face"}).
    """
    from app.modules.subtitle_region import band_geometry

    report = {"of": len(texts or []), "split": 0, "moved": 0,
              "kept_overlap": 0, "no_face": 0}
    notes: list[str] = []
    if not texts or not clips:
        return [dict(t) for t in (texts or [])], notes, report

    geom = band_geometry(design, canvas_width=canvas_width, canvas_height=canvas_height)
    # 편집본 클립 구간표 + 크롭 키프레임 캐시(클립당 한 번만 읽는다)
    spans: list[tuple[float, float, int]] = []
    base = 0.0
    for i, c in enumerate(clips):
        dur = max(0.0, float(c.end_sec) - float(c.start_sec))
        spans.append((base, base + dur, i))
        base += dur
    kf_cache: dict[int, list[dict[str, Any]]] = {}

    def _kfs_for(clip_idx: int) -> list[dict[str, Any]]:
        if clip_idx not in kf_cache:
            key = f"{clips[clip_idx].role}_{clip_idx}"
            path = (crop_map or {}).get(key)
            data: list[dict[str, Any]] = []
            if path is not None:
                try:
                    raw = json.loads(Path(path).read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        data = [k for k in raw if isinstance(k, dict)]
                except (OSError, ValueError):
                    data = []                # 못 읽으면 얼굴 없음 — 회피 없이 종전 배치
            kf_cache[clip_idx] = data
        return kf_cache[clip_idx]

    def _adjust(piece: dict[str, Any]) -> None:
        try:
            p_s, p_e = float(piece["start_sec"]), float(piece["end_sec"])
        except (TypeError, ValueError, KeyError):
            return
        t_mid = (p_s + p_e) / 2.0
        span = next((sp for sp in spans if sp[0] - 1e-9 <= t_mid < sp[1] + 1e-9), None)
        if span is None:
            report["no_face"] += 1
            return
        face = face_box_on_canvas(_kfs_for(span[2]), t_mid - span[0], geom,
                                  canvas_width=canvas_width)
        if face is None:
            report["no_face"] += 1
            return
        label = _label_box(piece, canvas_width, canvas_height)
        if label is None:
            return
        if not _boxes_overlap(label, face, FACE_AVOID_GAP_PX):
            return
        lx, ly, lw, lh = label
        fx, fy, fw, fh = face
        half_y = float(piece.get("size") or 72.0) * 0.6      # E18-2 클램프와 같은 여유
        y_min = y_lo * canvas_height + half_y
        y_max = y_hi * canvas_height - half_y
        x_min = 24 + lw / 2.0
        x_max = canvas_width - 24 - lw / 2.0
        gap = FACE_AVOID_GAP_PX
        cands: list[tuple[float, float, str]] = [
            (lx, fy - fh / 2.0 - gap - lh / 2.0, "위"),
            (lx, fy + fh / 2.0 + gap + lh / 2.0, "아래"),
        ]
        side_far = (fx - fw / 2.0 - gap - lw / 2.0 if lx <= fx
                    else fx + fw / 2.0 + gap + lw / 2.0)
        side_near = (fx + fw / 2.0 + gap + lw / 2.0 if lx <= fx
                     else fx - fw / 2.0 - gap - lw / 2.0)
        cands += [(side_far, ly, "옆"), (side_near, ly, "옆")]
        for nx, ny, kind in cands:
            if not (x_min <= nx <= x_max and y_min <= ny <= y_max):
                continue
            if _boxes_overlap((nx, ny, lw, lh), face, gap - 1):
                continue
            piece["x"] = round(nx / canvas_width, 4)
            piece["y"] = round(ny / canvas_height, 4)
            report["moved"] += 1
            notes.append(f"{str(piece.get('text', ''))[:12]!r} 얼굴 가림 → {kind}로 이동 "
                         f"({lx / canvas_width:.2f},{ly / canvas_height:.2f} → "
                         f"{piece['x']:g},{piece['y']:g})")
            return
        report["kept_overlap"] += 1
        notes.append(f"{str(piece.get('text', ''))[:12]!r} 얼굴을 다 못 피함 — 그대로 둔다"
                     f"(겹침이 증발보다 낫다)")

    out: list[dict[str, Any]] = []
    for t in texts:
        item = dict(t)
        try:
            t_s, t_e = float(item["start_sec"]), float(item["end_sec"])
        except (TypeError, ValueError, KeyError):
            out.append(item)
            continue
        cut_points = [sp[1] for sp in spans[:-1] if t_s + FACE_SPLIT_MIN_SEC <= sp[1] <= t_e - FACE_SPLIT_MIN_SEC]
        if cut_points:
            report["split"] += len(cut_points)
            bounds = [t_s] + cut_points + [t_e]
            pieces = []
            for b_s, b_e in zip(bounds, bounds[1:]):
                p = dict(item)
                p["start_sec"] = round(b_s, 3)
                p["end_sec"] = round(b_e, 3)
                pieces.append(p)
            notes.append(f"{str(item.get('text', ''))[:12]!r} 창이 컷 경계를 넘음 → "
                         f"{len(pieces)}조각으로 나눠 각 클립 기준 재배치")
        else:
            pieces = [item]
        for p in pieces:
            _adjust(p)
            out.append(p)
    return out, notes, report
