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
    "title_rotate", "tts_rotate",
    "title_box", "title_box2", "title_box_color", "title_box_color2",
    "title_bold", "title_bold2",
)
TITLE_BOX_KINDS = ("none", "round", "rect")
ROTATE_RANGE_DEG = 180.0

# ── 제목 기울기는 AI 에게 **좁은 범위만** 열려 있다 (E17-1, 2026-08-24) ────────
# 사용자 지시("제목은 왠만하면 회전은 안되게 해주고" → 다시: "돌리는 거는 가능한데,
# 안 돌리게 제약 정도로만 걸어줘"). 처음엔 title_rotate 를 통째로 막았지만, 그건
# '가능하다'는 요구를 어긴다 — 그래서 **완전히 닫지 않고 범위만 좁힌다**.
# tts_rotate 는 그대로 ±180°(design.tts_rotate 는 지시 대상이 아니다).
# ⚠ 사람·채널의 `--design-title-rotate` 는 이 상한과 무관하다(-180~180 그대로) —
#   좁히는 것은 **AI 산출**뿐이고, 사람이 보고 정한 값은 사람 것이다.
AI_TITLE_ROTATE_RANGE_DEG = 15.0

# ── TTS 라벨 ───────────────────────────────────────────────────────────────
# 불변 계약(E11·E12)을 **한 곳에서** 가져온다 — 여기 문자열을 베끼면 tts.py 가 라벨을
# 늘렸을 때 조용히 어긋난다. `elevenlabs:` 접두사는 계정 종속이라 AI 산출에 금지한다
# (E12: 사람이 대시보드에서 고르는 값이고, 계정마다 보이스 라이브러리가 다르다).
STYLE_VOICES = tuple(VOICE_PRESETS)
STYLE_SPEEDS = tuple(SPEED_TO_RATE)

STICKER_DIR_NAME = "stickers"       # app/assets/stickers
STICKER_RUN_SUBDIR = "style_assets"  # <run_dir>/style_assets — v3 images.file 의 기준


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


def validate_plan(
    plan: Any,
    *,
    manifest: dict[str, dict[str, Any]],
    app_root: Path,
    run_dir: Path,
) -> tuple[dict[str, Any], list[str]]:
    """style_plan/v1 → (정규화된 플랜, 기록할 메모들). 계약 위반이면 StylePlanError.

    반환 플랜의 모양은 **v3 자료 그대로**다:
      texts[]           — overrides_texts 가 기본값을 채운 완전한 dict (앵커 좌표 유지)
      images[]          — file 이 run_dir 상대 경로로 바뀐 v3 images
      subtitle_styles[] — {source_time_sec, style{size?,color?}}
      title_segments[]  — {text, from_anchor, to_anchor} (편집본 변환은 배치 단계에서)
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
             "tts", "design", "notes"}
    unknown = [k for k in plan if k not in known]
    if unknown:
        raise StylePlanError(f"모르는 최상위 키 {unknown} — 계약은 {sorted(known - {'schema'})} 뿐입니다")

    out: dict[str, Any] = {}

    # ── texts / images: v3 검증기에 그대로 태운다 ─────────────────────────
    texts = _cap(list(plan.get("texts") or []), MAX_TEXTS, "효과 텍스트", notes)
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
    if images:
        out["images"] = images

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
        a = _num(sg.get("from_anchor"), f"title_segments[{i}]: from_anchor")
        b = _num(sg.get("to_anchor"), f"title_segments[{i}]: to_anchor")
        if a < 0 or b <= a:
            raise StylePlanError(
                f"title_segments[{i}]: 앵커 구간이 잘못됐습니다 (from {a:g} → to {b:g})")
        segs.append({"text": text, "from_anchor": a, "to_anchor": b})
    if segs:
        out["title_segments"] = segs

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
        bad = [k for k in design if k not in STYLE_DESIGN_ALLOWED]
        if bad:
            raise StylePlanError(
                f"design 에 모르는(또는 AI 에게 열리지 않은) 키 {bad} — "
                f"허용: {', '.join(STYLE_DESIGN_ALLOWED)}. 밴드 레이아웃·폰트·자막 색 체계·"
                f"배속은 채널 정체성이라 AI 가 바꾸지 않습니다")
        norm_design: dict[str, Any] = {}
        for k, v in design.items():
            if k == "title_rotate":
                deg = _num(v, f"design.{k}")
                # 좁은 범위(±15°) — 완전히 막지는 않되 자제를 강제한다(위 상수 주석).
                if not (-AI_TITLE_ROTATE_RANGE_DEG <= deg <= AI_TITLE_ROTATE_RANGE_DEG):
                    raise StylePlanError(
                        f"design.{k}: {deg:g} 가 AI 허용 범위 밖입니다 "
                        f"(±{AI_TITLE_ROTATE_RANGE_DEG:g}° — 제목은 크게 기울이지 않습니다. "
                        f"더 큰 각도는 채널·편집실이 --design-title-rotate 로 정합니다)")
                norm_design[k] = deg
            elif k == "tts_rotate":
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
            elif k in ("title_box_color", "title_box_color2"):
                norm_design[k] = str(v)
            else:                                       # title_bold, title_bold2
                if not isinstance(v, bool):
                    raise StylePlanError(f"design.{k}: true/false 여야 합니다({v!r})")
                norm_design[k] = v
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

    if "title_rotate" in plan_design and not _blocked("title_rotate", "title_rotate"):
        kwargs["title_rotate"] = float(plan_design["title_rotate"])
    if "tts_rotate" in plan_design and not _blocked("tts_rotate", "tts_rotate"):
        kwargs["tts_rotate"] = float(plan_design["tts_rotate"])

    # 줄별 리스트 3종은 cli._build_design_config 와 **같은 조립**이다: 기본값에서 시작해
    # 지정한 줄만 치환한다(한 줄만 바꿔도 다른 줄은 그대로). 렌더러는 리스트만 읽는다.
    for keys, field, default in (
        (("title_box", "title_box2"), "title_boxes", list(base_design.title_boxes)),
        (("title_box_color", "title_box_color2"), "title_box_colors",
         list(base_design.title_box_colors)),
        (("title_bold", "title_bold2"), "title_bolds", list(base_design.title_bolds)),
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


def title_segments_from_anchors(segs: list[dict[str, Any]],
                                clips: list) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """앵커 쌍(from/to, 원본 절대초) → E8 `title.segments`(편집본 시간축).

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
    out = [{"text": p["text"], "start_sec": p["start_sec"], "end_sec": p["end_sec"]}
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
    if deduped:
        validate_title_segments(deduped)               # E8 검증기(개수·범위·겹침) 재사용
    return deduped, dropped


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
