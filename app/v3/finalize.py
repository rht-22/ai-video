"""M4 후반부 — 최종 렌더(13) + validate 확장(14) + §9-D 미시문법 검사.

렌더는 기존 모듈 재사용이 원칙(발주서 §C): `renderer.render_short` 가 v1 과 같은
화면 문법(상단 제목 2줄·하단 작품명·자막 밴드·TTS 믹스·덕킹)을 그대로 그린다 —
v3 는 **입력 어댑터만** 짓는다. style.json 의 design 어휘(어댑터 design-* 1:1)를
DesignConfig 로 매핑하고, 어절 자막(C6)·TTS cue(C2)·괄호 라벨을 ASS 3종으로 바꾼다.

validate 확장(경고 모드 — 기획: 차단하지 않는다):
  신규 4종 ① 컷 경계=grid span 경계 100%(벨트 재사용) ② TTS-importance≥4 겹침 0
  ③ exception 구간 유입 0 ④ 프레임 비전 QC(자막 잘림·겹침 — Flash, 경고만).
  §9-D ① 진행감 — 화면 변화 이벤트(컷·자막 등장·cue 시작) 간격 > 3s 경고
       ② 루프 정합 — 첫/끝 프레임 시각 거리(평균 절대 오차) 기록 + 큰 단절 경고
       (서론 금지는 story 프롬프트 규칙 — 편성 시점 방어).
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config import AppConfig, DesignConfig
from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.renderer import RenderInputs, render_short
from app.modules.speech import SpeechSegment
from app.modules.story_builder import StoryClip
from app.modules.subtitle import (
    SubtitleStyle,
    _hex_to_ass_color,
    build_ass_from_segments,
    build_texts_ass,
    build_tts_ass,
)
from app.v3 import assemble, schemas, stage4

PROGRESSION_MAX_GAP_SEC = 3.0     # §9-D 진행감 — 2~3초마다 화면 변화
LOOP_DIFF_WARN = 60.0             # 첫/끝 프레임 평균 절대 오차(0~255) 경고 임계
LABEL_Y_RATIO = stage4.LABEL_Y_FALLBACK   # 괄호 라벨 세로 기본 — 템플릿 1010/1920 실측
LABEL_MAX_SEC = 4.0               # 라벨 표시 상한 — 레퍼런스 실측 3.54~4.00s
# 색 순환은 Stage 4 계약(stage4.LABEL_PALETTE)과 한 곳에서 관리한다
QC_FRAME_COUNT = 4


_CAPTION_TRAIL = ",.、。 \t"


def narration_caption(text: str) -> str:
    """내레이션 **자막**용 문구(순수) — 말투는 그대로 두고 화면 글자에서만 쉼표·온점을 뺀다.

    2026-09-03 사용자 지시: "~데," 말투는 좋은데 자막에서는 쉼표가 안 나오는 게 낫고,
    마지막 온점도 마찬가지. 합성(TTS)은 원문으로 이미 끝났고 story 문서도 그대로다 —
    바뀌는 건 v3_tts.ass 에 그리는 글자뿐. 물음표·느낌표·말줄임표는 남긴다(억양 정보)."""
    t = " ".join(str(text or "").replace(",", " ").replace("、", " ").split())
    return t.rstrip(_CAPTION_TRAIL)


def balance_narration_lines(text: str, *, max_chars: int) -> str:
    """내레이션 자막 두 줄 균형 분할(순수). 한 줄에 들면 그대로.

    종전 `_wrap_for_ass` 는 앞줄을 꽉 채워 '추적기 앱을 보며 뒤를 / 쫓는데' 꽁다리를 남겼다
    (2026-09-08 v10 대조). 어절 경계 하나를 골라 두 줄 길이 차의 제곱이 최소인 배치 — 대사
    자막의 `_balanced_breaks` 와 같은 경계 벌점(의존명사·조사로 줄 시작 · 1음절 부사로 줄 끝).
    동점이면 앞줄이 긴 쪽. 두 줄 모두 max 에 못 넣는 문장은 손대지 않는다(자동 줄바꿈 몫).
    반환에 '\n' 이 있으면 `_lay_out_for_ass` 가 그 줄바꿈을 그대로 쓴다."""
    from app.v3 import assemble as _asm
    t = " ".join(str(text or "").split())
    words = t.split(" ")
    if len(t) <= max_chars or len(words) < 2:
        return t
    best: tuple[float, int, str] | None = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if len(a) > max_chars or len(b) > max_chars:
            continue
        cost = float((len(a) - len(b)) ** 2)
        if _asm._starts_dependent(words[i]):
            cost += _asm.LINE_BOUNDARY_PENALTY
        if words[i - 1].strip(".,!?…'\"") in _asm.LINE_END_PENALTY_WORDS:
            cost += _asm.LINE_BOUNDARY_PENALTY
        if best is None or cost < best[0] or (cost == best[0] and len(a) > len(best[2].split("\n")[0])):
            best = (cost, i, a + "\n" + b)
    return best[2] if best else t


def _style_color(hex_color: str) -> str:
    """hex → ASS Style 블록 표기(&H00BBGGRR). 인라인 태그(&H..&)와 혼동 금지."""
    return f"&H00{_hex_to_ass_color(hex_color).strip('&H&')}"


# ── design 어휘 → DesignConfig 매핑 ─────────────────────────────────────────

def design_from_style(design: dict) -> DesignConfig:
    """style.json design(어댑터 design-* 어휘) → 렌더러 DesignConfig. 순수.

    DesignConfig 는 frozen — dataclasses.replace 로 짓는다. tts 색은 DesignConfig
    가 ASS 표기라 hex → ASS 변환을 여기서 책임진다."""
    import dataclasses
    base = DesignConfig()
    up: dict[str, Any] = {}
    if "aspect_ratio" in design:
        up["aspect_ratio"] = str(design["aspect_ratio"])
    # 밴드 위치·크기는 **채널 프리셋 전용**(STYLE_ALLOWED 에 없어 AI 가 못 준다).
    # 매핑이 없으면 프리셋에 적어도 조용히 무시된다 — 가왕쇼 템플릿 443px 이 그랬다.
    if design.get("video_y") is not None:
        up["video_y"] = int(design["video_y"])
    if design.get("video_width") is not None:
        up["video_width"] = int(design["video_width"])
    for k in ("work_image_width", "work_image_height"):
        if design.get(k) is not None:
            up[k] = int(design[k])
    if design.get("work_image_align"):
        up["work_image_align"] = str(design["work_image_align"])
    up["title_colors"] = [str(design.get("title_color", base.title_colors[0])),
                          str(design.get("title_color2", base.title_colors[1]))]
    up["title_sizes"] = [int(design.get("title_size", base.title_sizes[0])),
                         int(design.get("title_size2", base.title_sizes[1]))]
    # ⚠ title_bolds 는 **조립하지 않는다** — 제목 굵게는 닫혀 있다(E21·stage4
    # STYLE_DESIGN_IGNORED). 옛 체크포인트에 title_bold 가 남아 있어도 되살아나지
    # 않게 여기서 끊는다. 채널·편집실이 정한 base 값은 그대로 간다.
    if design.get("subtitle_color"):
        up["subtitle_color"] = str(design["subtitle_color"])
    if design.get("subtitle_size"):
        up["subtitle_size"] = int(design["subtitle_size"])
    if design.get("subtitle_y_margin"):
        up["subtitle_y_margin"] = int(design["subtitle_y_margin"])
    if design.get("tts_color"):
        # _hex_to_ass_color 는 인라인 태그용(&HBBGGRR&) — DesignConfig.tts_line_color
        # 는 Style 블록 표기(&H00BBGGRR)라 알파 포함으로 재조립한다
        up["tts_line_color"] = _style_color(str(design["tts_color"]))
    if design.get("tts_size"):
        up["tts_line_font_size"] = int(design["tts_size"])
    if design.get("tts_y_margin"):
        up["tts_line_y_margin"] = int(design["tts_y_margin"])
    if design.get("work_color"):
        up["work_color"] = str(design["work_color"])
    # ── 채널 프리셋 주입(2026-09-04, 사용자 지시) — 어댑터 CHANNEL_DESIGN_FLAGS 어휘 중
    # v3 가 받는 키(ves V3_DESIGN_KEYS 와 1:1). 폰트는 이름 그대로(render_final 이
    # get_font_path 로 경로화), 플랫폼 표기는 DesignConfig 필드명이 어휘와 같다.
    for k in ("title_font", "subtitle_font"):
        if design.get(k):
            up[k] = str(design[k])
    if not design.get("subtitle_font"):
        up["subtitle_font"] = V3_TEXT_FONT     # v3 기본 자막 폰트(채널 명시가 이긴다)
        # libass 크기 보정(2026-09-10): ASS Fontsize 는 em 이 아니라 **줄 높이(ascender−descender)** 다 —
        # Noto Sans CJK Black 은 1.448em 이라 60 이 실제 41px 로 찍혔다(v9 수작업은 Pillow 62px em).
        # 기본 폰트 채널만 보정(채널이 폰트를 명시한 가왕쇼 등은 사람이 보고 맞춘 값이라 그대로).
        _sub_em = int(up.get("subtitle_size", base.subtitle_size))
        _tts_em = int(up.get("tts_line_font_size", base.tts_line_font_size))
        up["subtitle_size"] = ass_size_for_em(V3_TEXT_FONT, _sub_em)
        up["tts_line_font_size"] = ass_size_for_em(V3_TEXT_FONT, _tts_em)
    for k in ("title_y", "work_title_y", "work_font_size", "work_band_offset"):
        if design.get(k) is not None:
            up[k] = int(design[k])
    if design.get("tts_width") is not None:
        up["tts_width"] = float(design["tts_width"])
    if design.get("face_tracking") is not None:
        up["enable_reframe"] = bool(design["face_tracking"])
    for k in ("platform_image", "platform_text", "platform_color", "platform_align",
              "platform_placement"):
        if design.get(k):
            up[k] = str(design[k])
    for k in ("platform_x", "platform_y", "platform_image_width",
              "platform_image_height", "platform_font_size"):
        if design.get(k) is not None:
            up[k] = int(design[k])
    # 작품명 아래 캡션(2026-09-07) — 문자열 둘·크기 하나. 없으면 종전과 동일.
    for k in ("work_caption", "work_caption_color"):
        if design.get(k):
            up[k] = str(design[k])
    if design.get("work_caption_font_size") is not None:
        up["work_caption_font_size"] = int(design["work_caption_font_size"])
    # Explicit imported guide logo (tikitaka adapter); absent keys leave v3 unchanged.
    for k in ("work_type", "work_value", "work_image_align"):
        if design.get(k):
            up[k] = str(design[k])
    for k in ("work_image_width", "work_image_height"):
        if design.get(k) is not None:
            up[k] = int(design[k])
    return dataclasses.replace(base, **up)


# 채널 design 키 중 Stage 4 프리셋(프롬프트·밴드 기하)에도 들어가야 하는 것 —
# STYLE_ALLOWED 어휘 + 밴드 위치. 나머지(폰트·플랫폼·작품명 위치·크롭)는 AI 가
# 볼 이유가 없어 렌더에서만 얹는다.
CHANNEL_PRESET_KEYS = frozenset({
    "title_color", "title_color2", "title_size", "title_size2",
    "subtitle_color", "subtitle_size", "subtitle_y_margin",
    "tts_color", "tts_size", "tts_y_margin", "work_color", "aspect_ratio",
    "video_y", "video_width",
})


def merge_channel_preset(preset: dict, channel_design: dict | None) -> dict:
    """채널 명시 키가 프리셋 기본값을 덮는다(v1 규율: 채널 명시 > AI > 기본값). 순수.
    channel_design 이 비면 프리셋 **그 객체**를 돌려준다(캐시 지문 불변 = 회귀 0)."""
    picked = {k: v for k, v in (channel_design or {}).items() if k in CHANNEL_PRESET_KEYS}
    return {**preset, **picked} if picked else preset


def channel_design_over_ai(style_design: dict, channel_design: dict | None) -> dict:
    """렌더 직전 병합 — Stage 4 확정 디자인 위에 채널 design 을 **전량** 얹는다.
    AI diff 가 채널이 못박은 값을 뒤집으면 안 된다(E15 우선순위 표). 순수."""
    return {**(style_design or {}), **(channel_design or {})}


def video_band_ratio(design, *, canvas_height: int = 1920) -> tuple[float, float]:
    """영상 밴드의 세로 범위(캔버스 대비 0~1) — 라벨은 이 **안**에 있어야 한다.

    기하는 subtitle_region.band_geometry 를 **재사용**한다 — 수식을 베끼면 언젠가
    화면과 어긋난다(E17-2 규율). 실제로 처음 베낀 판은 렌더러의 짝수 보정이 빠져
    16:9·11:9 에서 1px 어긋나 있었다(적대 리뷰 M1 확정)."""
    from app.modules.subtitle_region import band_geometry
    g = band_geometry(design, canvas_height=canvas_height)
    return g.top / canvas_height, g.bottom / canvas_height


LOGO_WIDTH = 620                  # 하단 밴드 로고 박스(가로) — 수동 제작본 "가왕쇼" 폭 근사
LOGO_BOX_HEIGHT = 300             # 세로 상한(contain) — 세로형 로고가 밴드를 넘지 않게
from app.v3 import safe_zone as _sz  # noqa: E402 — 폰 재생 화면 안전 박스(2026-09-18)
TITLE_MAX_WIDTH = _sz.SAFE_WIDTH  # 880 — 폰 재생 화면 좌우 잘림(가로 100~980). 종전 980(좌우 50)
TITLE_CHAR_W = 1.0                # 한글 1자 폭 ÷ 글자크기 (Jalnan 92px 프레임 실측)
TITLE_SPACE_W = 0.3               # 공백은 좁다 — 1.0 으로 세면 멀쩡한 제목을 줄인다


# 자막 텍스트 스타일(2026-09-08 사용자 지시 — 참고 쇼츠 v10 build10 실측과 맞춤):
#   폰트 Noto Sans CJK KR Black · 외곽선 8px(62px 글자의 13% — 종전 3px 은 밝은 배경에서 흐릿)
#   · 팝은 '크게 튀어나왔다가 제자리'(pop_snap, 100ms) · 내레이션도 같은 팝 · 내레이션 아랫줄은
#   대사와 같은 y · 내레이션 2줄은 균형 분할. 채널이 subtitle_font/tts_y_margin 을 명시하면 그쪽이 이긴다.
V3_TEXT_FONT = "NotoSansCJKkr-Black"      # config.FONT_NAME_MAP/FONT_FAMILY_MAP 에 등록된 이름

_EM_RATIO_CACHE: dict[str, float] = {}


def libass_line_height_ratio(font_name: str) -> float:
    """폰트의 (ascender − descender) / em — libass 가 ASS Fontsize 를 이 줄 높이에 맞추므로
    실제 em 은 Fontsize ÷ 이 값이다. 실측(2026-09-10, libass vs Pillow 같은 60): Noto Sans CJK KR Black
    0.698 → 1.43 · JalnanGothic 0.725 → 1.38. FreeType 메트릭(PIL getmetrics)으로 잰다. 폰트를 못 찾으면 1.0."""
    if font_name in _EM_RATIO_CACHE:
        return _EM_RATIO_CACHE[font_name]
    ratio = 1.0
    try:
        from PIL import ImageFont
        from app.config import get_font_path
        p = get_font_path(font_name, Path(__file__).resolve().parents[1])
        if p and Path(p).is_file():
            asc, desc = ImageFont.truetype(p, 1000).getmetrics()
            if asc + desc > 0:
                ratio = (asc + desc) / 1000.0
    except Exception:  # noqa: BLE001 — 보정은 편의 장치, 실패하면 종전 크기
        ratio = 1.0
    _EM_RATIO_CACHE[font_name] = ratio
    return ratio


def ass_size_for_em(font_name: str, em_px: int) -> int:
    """원하는 em 픽셀 → libass 에 줄 ASS Fontsize(줄 높이 단위). 비율 1.0 이면 그대로."""
    return int(round(int(em_px) * libass_line_height_ratio(font_name)))
SUB_OUTLINE_PX = 8                        # v10 ow=8(강조 9) — 대사·내레이션 공통
NARRATION_FX = "pop_snap"                 # 내레이션 등장 팝(v10 POP)
POP_TO_FX = {"soft": "pop_snap", "strong": "pop_snap_strong"}   # none 은 태그 없음
EMPHASIS_FX = "pop_snap_strong"           # 강조 자막(v10 EPOP)


def subtitle_fx_windows(story_doc: dict, style_doc: dict,
                        timeline: list[dict]) -> list[tuple[float, float, str]]:
    """비트별 등장 효과 창(편집본 좌표). 순수.

    Stage 4 는 비트마다 `pop`(none/soft/strong)을 **컷 리듬을 보고** 정하는데
    지금까지 아무도 읽지 않는 죽은 출력이었다(사용자 지적) — 라벨 위치와 같은 부류.
    여기서 자막 줄 등장 애니메이션으로 잇는다."""
    pops = {int(b["number"]): str(b.get("pop") or "none")
            for b in ((style_doc or {}).get("v3_style") or {}).get("beats") or []
            if b.get("number") is not None}
    out: list[tuple[float, float, str]] = []
    for w in stage4.edited_beat_windows(story_doc, timeline):
        fx = POP_TO_FX.get(pops.get(int(w["beat"]), "none"))
        if fx:
            out.append((float(w["start"]), float(w["end"]), fx))
    return out


def resolve_work_logo(work_title: str, app_root: Path | None = None) -> Path | None:
    """작품명 → 정규화된 로고 PNG. 순수(파일 조회만)·결정적.

    `assets/logos/<코드>.json` 의 `source_file` 이 작품명으로 **시작**하면 그 작품의
    로고다(scripts/normalize_logo.py 가 남기는 메타). 하단 밴드는 검정이라 흰색판을
    우선하고(`_color` 는 차순위), 동률은 코드 사전순 — 무작위 요소 없음.
    못 찾으면 None → 종전처럼 작품명 **텍스트**로 렌더(회귀 0)."""
    name = str(work_title).strip()
    if not name:
        return None
    root = app_root or Path(__file__).resolve().parent.parent
    logo_dir = root / "assets" / "logos"
    hits: list[tuple[int, str, Path]] = []
    for meta in sorted(logo_dir.glob("*.json")):
        try:
            src = str(json.loads(meta.read_text(encoding="utf-8")).get("source_file") or "")
        except (json.JSONDecodeError, OSError):
            continue
        if not src.startswith(name):
            continue
        png = meta.with_suffix(".png")
        if png.exists():
            hits.append((1 if meta.stem.endswith("_color") else 0, meta.stem, png))
    return min(hits)[2] if hits else None


def fit_title_sizes(title_text: str, sizes: list[int], font_path: str | None = None) -> list[int]:
    """제목 줄별 크기를 폭 980px 안으로 줄인다. 순수.

    렌더러의 자동 줄바꿈은 **1줄 크기**로만 폭을 재서(renderer `_max_chars_for(
    base_size)`) 2줄이 더 크면 그 줄이 화면 밖으로 잘린다 — 템플릿 크기(92/112)를
    그대로 넣자 프레임 QC 가 4프레임 전부에서 잡았다. 템플릿 자신의 계약이
    max_width_px 980 이므로 여기서 줄마다 맞춘다(렌더러는 무변경 — 공용이다).

    font_path 가 실제 TTF 면 **그 폰트로 Pillow 실측**(renderer `_measure_title_text_width`
    와 같은 신뢰)해 폭에 맞는 최대 크기를 이분 탐색한다 — 채널이 제목 폰트를 바꿔도(잘난체
    고딕 등) 글자폭 근사(TITLE_CHAR_W)가 폰트와 어긋나지 않게(2026-09-04). 파일이 아니면
    종전 근사(한글 1em · 공백 0.3em — Jalnan·JalnanGothic 둘 다 실측 일치)."""
    from app.modules.renderer import _measure_title_text_width
    lines = [ln for ln in str(title_text).split("\n") if ln.strip()]
    out = list(sizes)
    measurable = bool(font_path) and Path(str(font_path)).is_file()
    for i, size in enumerate(sizes):
        if i >= len(lines):
            continue
        ln = lines[i].strip()
        if measurable:
            lo, hi = 40, int(size)
            if _measure_title_text_width(ln, str(font_path), hi) <= TITLE_MAX_WIDTH:
                out[i] = hi
                continue
            while lo < hi:                 # 폭 ≤ 980 인 최대 크기
                mid = (lo + hi + 1) // 2
                if _measure_title_text_width(ln, str(font_path), mid) <= TITLE_MAX_WIDTH:
                    lo = mid
                else:
                    hi = mid - 1
            out[i] = lo
            continue
        units = sum(TITLE_SPACE_W if ch.isspace() else TITLE_CHAR_W for ch in ln)
        if units > 0:
            out[i] = max(40, min(int(size), int(TITLE_MAX_WIDTH / units)))
    return out


def _cycle_color(index: int) -> str:
    """Stage 4 가 색을 안 줬을 때의 결정적 순환 — 라벨이 전부 같은 색이 되지 않게."""
    return stage4.LABEL_COLOR_CYCLE[index % len(stage4.LABEL_COLOR_CYCLE)]


def plan_labels(story_doc: dict, plan: dict) -> list[dict]:
    """비트 라벨 → 편집본 시각이 붙은 목록(순수). style·render 공용.

    M11: 앵커 span 시각에 뜬다(비트 시작 고정 아님). 위치(x·y)는 여기서 정하지
    않는다 — Stage 4 가 화면을 보고 채우고, 없으면 렌더가 기본값을 쓴다."""
    offsets = assemble.edited_offsets(plan["timeline"], plan.get("source_fps"))
    span_t = {}
    for c in plan["timeline"]:
        for sid in c.get("span_ids") or []:
            span_t.setdefault(sid, float(c["clip_start_sec"]))
    out: list[dict] = []
    for b in story_doc.get("beats") or []:
        items = b.get("labels") or ([{"text": b["label"],
                                      "span_id": (b.get("span_ids") or [None])[0]}]
                                    if b.get("label") else [])
        for lb in items:
            src = span_t.get(lb.get("span_id"))
            if src is None:
                src = schemas.parse_ts(b["time"]["start"])
            s0 = assemble.to_edited_sec(src, offsets, kind="start")
            s1 = assemble.to_edited_sec(schemas.parse_ts(b["time"]["end"]), offsets,
                                        kind="end")
            if s0 is None or s1 is None or s1 <= s0:   # 역전 = 음수 길이 ASS
                continue
            out.append({"index": len(out), "text": lb["text"],
                        "start_sec": round(s0, 3),
                        "end_sec": round(min(s1, s0 + LABEL_MAX_SEC), 3),
                        "beat": b.get("number"), "span_id": lb.get("span_id")})
    return out


SUB_GAP_PX = 12                   # 원본 자막과 우리 자막 사이 최소 여백
WORK_GAP_BELOW_VIDEO = 20         # renderer 의 _gap_below_video 와 같은 값(로고 안전선)


def platform_line_block(design) -> int:
    """작품명 **위** 플랫폼 표기 줄이 차지하는 높이(줄 + 여백, px) — renderer 의
    `platform_line_geometry` 와 같은 수식. above_work 가 아니거나 표기가 없으면 0. 순수."""
    from app.modules.renderer import PF_LINE_GAP, platform_line_geometry
    if getattr(design, "platform_placement", "band") != "above_work":
        return 0
    if not (getattr(design, "platform_image", None) or getattr(design, "platform_text", None)):
        return 0
    return platform_line_geometry(design)["line_h"] + PF_LINE_GAP


def work_caption_block(design) -> int:
    """작품명/로고 **아래** 캡션 줄(work_caption)의 높이(줄 + 여백) — renderer 와 같은 함수."""
    from app.modules.renderer import work_caption_block as _wcb
    return _wcb(design)


def estimate_work_height(design) -> int:
    """하단 작품명/로고 **블록** 높이(px) 추정 — renderer 와 같은 수식(텍스트 = 폰트×1.4,
    로고 = contain 박스 실측) + 작품명 위 플랫폼 줄(above_work). 로고 PNG 크기를 못 읽으면
    박스 높이(보수적 = 더 큼). 순수."""
    return _work_item_height(design) + platform_line_block(design) + work_caption_block(design)


def _work_item_height(design) -> int:
    if getattr(design, "work_type", "text") != "image" or not getattr(design, "work_value", None):
        return int(int(getattr(design, "work_font_size", 40)) * 1.4)
    box_w = int(getattr(design, "work_image_width", 350) or 350)
    box_h = getattr(design, "work_image_height", None)
    try:
        from PIL import Image
        with Image.open(str(design.work_value)) as im:
            nat_w, nat_h = im.size
        _bh = int(box_h) if box_h else int(nat_h * (box_w / nat_w))
        sc = min(box_w / nat_w, _bh / nat_h)
        return max(2, int(nat_h * sc) // 2 * 2)
    except Exception:  # noqa: BLE001 — 실측 실패 = 박스 높이(보수적)
        return int(box_h) if box_h else max(60, int(box_w * 0.5))


def estimate_work_top(design, *, band_bottom: int, canvas_height: int = 1920,
                      min_top: int | None = None) -> int:
    """하단 작품명/로고의 **윗변 y** 추정 — renderer 의 로고 배치 수식을 그대로 따른다
    (safe_top = max(밴드 하단+20, min_top) · contain 박스 · center 정렬 · 캔버스 하단
    클램프). min_top 은 renderer 의 work_min_top 과 같은 뜻(자막 스택 하한). 순수."""
    H = int(canvas_height)
    _off = getattr(design, "work_band_offset", None)
    safe_top = int(band_bottom) + (int(_off) if _off is not None else WORK_GAP_BELOW_VIDEO)
    if min_top is not None:
        safe_top = max(safe_top, int(min_top))
    # above_work 플랫폼 줄은 작품명 위에 붙는다 — 반환값은 **블록 윗변**(줄이 있으면 줄의 윗변)
    blk = platform_line_block(design)
    safe_top += blk
    # 아래 캡션(work_caption)은 하단 한계를 그만큼 올린다 — renderer 의 _work_bottom 과 같은 수식
    bottom = H - 20 - work_caption_block(design)
    if getattr(design, "work_type", "text") != "image" or not getattr(design, "work_value", None):
        y = max(int(getattr(design, "work_title_y", 1400)), safe_top)
        th = _work_item_height(design)
        if y + th > bottom:
            y = max(safe_top, bottom - th)
        return y - blk
    logo_h = _work_item_height(design)
    y = safe_top
    if getattr(design, "work_image_align", "top") == "center" and _off is None:
        y = safe_top + (bottom - safe_top - logo_h) // 2
    if y + logo_h > bottom:
        y = bottom - logo_h
    return max(safe_top, y) - blk


def stack_work_below_text(*, sub_margin: int, tts_margin: int, work_top: int,
                          work_height: int, canvas_height: int = 1920,
                          gap: int = WORK_GAP_BELOW_VIDEO) -> tuple[int, int, int | None, list[str]]:
    """밴드 아래 스택 — 자막·내레이션 블록 **아래**에 작품명/로고가 오게 한다. 순수.

    2026-09-04 사용자 지시: "무조건 자막이 작품명보다 위 — 이런 경우엔 작품명도 같이
    내려가야 한다". 실사고(지금불륜 EP01 bb71cb7d): recap 프리셋 자막을 밴드 아래로
    내리자(fit_margin_below_band) 자막 아랫변 1639 가 작품명(1497~1553) 밑으로 들어갔다.

    우선순위: ① 자막 > 작품명(하드) ② 작품명이 캔버스 하단 20px 안(하드 — renderer 클램프)
    ③ 자막이 밴드 아래(소프트). 반환 (sub_margin, tts_margin, work_min_top, notes):
      - work_min_top = 두 블록 아랫변 최댓값 + gap(20 — 렌더러 로고 안전선과 같은 값,
        12 는 실렌더에서 내레이션과 작품명이 붙어 보였다). 작품명의 자연 위치(work_top)가 이미
        그 아래면 None(렌더 무변경).
      - 작품명 높이를 더해 캔버스 안에 못 들면 작품명을 하단 한계에 붙이고 자막·내레이션
        margin 을 **올려서**(블록 아랫변 ≤ 작품명 윗변 − gap) ①을 지킨다 — 자막이 밴드
        안쪽으로 되돌아가더라도 작품명 위라는 규칙은 깨지지 않는다(⚠ 기록)."""
    H = int(canvas_height)
    sub_m, tts_m = int(sub_margin), int(tts_margin)
    notes: list[str] = []
    text_bottom = max(H - sub_m, H - tts_m)
    required = text_bottom + int(gap)
    if required <= int(work_top):
        return sub_m, tts_m, None, notes
    limit = H - 20 - int(work_height)
    if required <= limit:
        notes.append(f"작품명 윗변 {int(work_top)} → {required} (자막 아랫변 {text_bottom} + {gap})")
        return sub_m, tts_m, required, notes
    # 못 들어간다 — 작품명은 하단 한계, 자막은 그 위로 올린다(밴드 안쪽 허용)
    new_top = max(limit, int(work_top))
    max_bottom = new_top - int(gap)
    if H - sub_m > max_bottom:
        notes.append(f"⚠ 자막 margin_v {sub_m} → {H - max_bottom} (작품명 하단 한계 {new_top} 위로 올림 — 밴드 안쪽)")
        sub_m = H - max_bottom
    if H - tts_m > max_bottom:
        notes.append(f"⚠ 내레이션 margin_v {tts_m} → {H - max_bottom} (작품명 하단 한계 {new_top} 위로 올림 — 밴드 안쪽)")
        tts_m = H - max_bottom
    notes.append(f"작품명 윗변 {int(work_top)} → {new_top} (캔버스 하단 한계)")
    return sub_m, tts_m, (new_top if new_top > int(work_top) else None), notes


def preset_relative_margin(margin_v: int, *, ref_band_bottom: int, band_bottom: int) -> int:
    """프리셋 절대 margin_v 를 **밴드 하단으로부터의 거리 유지**로 옮긴다. 순수.

    2026-09-04 사용자 지시: "대사도 내레이션도 영상 위에 얹혀도 된다 — 원격(v1) 위치를
    참고". v1 은 대사 자막을 밴드 하단 10px 위(`_compute_subtitle_margin_v`), TTS 를 밴드
    하단 델타 앵커(`_compute_tts_margin_v`)로 둔다 — 둘 다 **밴드 안쪽**이다. v3 프리셋의
    절대값도 프리셋 자신의 밴드에 맞춘 값(recap 518/580 = 24:23·443 밴드 하단 75/137px 위)
    이라, 채널이 밴드만 바꿔도 그 거리를 유지해 따라간다. 밴드가 같으면 값도 같다(회귀 0)."""
    return max(1, int(margin_v) - (int(band_bottom) - int(ref_band_bottom)))


def fit_margin_below_band(margin_v: int, *, canvas_height: int, band_bottom: int,
                          block_height: int, work_top: int | None = None,
                          gap: int = SUB_GAP_PX) -> tuple[int, str | None]:
    """자막 블록(최대 줄 수 기준)이 영상 밴드 **아래**에 오도록 margin_v 를 내린다. 순수.

    2026-09-04 실사고(가왕쇼 7화): 채널 tts_y_margin 550 은 video_y 440 에 손으로 맞춘
    값이라 밴드를 500 으로 내리자 두 줄 내레이션 윗줄이 밴드에 25px 겹쳤다. 값을 채널이
    다시 잡게 하지 않고, 밴드 기하에서 상한을 재서 **내리기만** 한다(이미 아래면 불변).
    로고 윗변(work_top)을 주면 블록 아랫변이 그 위 gap 안에 드는지 확인해 메모만 남긴다
    — 둘 다 못 지키면 영상 겹침 회피가 우선이다(영상 위 글자가 더 나쁘다)."""
    H = int(canvas_height)
    cur = int(margin_v)
    max_margin = H - int(band_bottom) - int(gap) - int(block_height)
    note = None
    new = cur
    if cur > max_margin:
        new = max(1, max_margin)
        overlap = (H - cur - int(block_height)) - int(band_bottom)
        note = f"margin_v {cur} → {new} (두 줄 블록 윗변이 밴드 하단 {band_bottom} 에 {-overlap}px 겹침)"
    if work_top is not None and (H - new) > int(work_top) - int(gap):
        note = (note or f"margin_v {cur} 유지") + \
            f" ⚠ 블록 아랫변 {H - new} 이 작품명/로고 윗변 {work_top} 에 근접 — 로고와 겹칠 수 있음"
    return new, note


def band_anchored_margin(*, band_bottom: int, offset: int, lines: int, font_size: int,
                         canvas_height: int = 1920) -> int:
    """밴드 기준 상대 배치(2026-09-04) — 자막 블록 **윗변**을 `밴드 하단 + offset` 에 건다.
    offset 은 px(음수 = 밴드 안쪽). 채널 키 subtitle_band_offset·tts_band_offset 의 정의.
    ASS 는 하단 앵커라 줄 수만큼 내려 margin_v 로 환산한다. 순수.

    왜 상대값인가: 절대 margin(subtitle_y_margin 등)은 화면비·video_y 마다 손으로 다시
    잡아야 한다(13:9·440 에 맞춘 550 이 500 에서 겹친 실사고). v1 E10 이 같은 이유로
    tts margin 을 '밴드 하단으로부터의 델타 앵커'로 정의했다 — v3 는 그걸 키로 연다."""
    from app.modules.subtitle_region import estimate_subtitle_height
    blk = estimate_subtitle_height(int(font_size), lines=max(1, int(lines)))
    return max(1, int(canvas_height) - (int(band_bottom) + int(offset) + blk))


def tts_cue_margins(captions: list[str], *, band_bottom: int, offset: int, font_size: int,
                    width: float | None, canvas_height: int = 1920, lift: int = 0) -> list[int]:
    """cue 별 margin_v — 그 cue 가 실제로 몇 줄로 접히는지(ASS 조립과 **같은 함수**로
    센다) 보고 윗변을 밴드 아래 같은 자리에 건다. 한 줄 cue 가 두 줄 자리 아래로 처지지
    않게 하는 것이 목적. lift = 번인 회피가 전역 margin 을 올린 만큼(px) — 줄별에도 같이."""
    from app.modules.subtitle import _lay_out_for_ass
    out = []
    for cap in captions:
        n = _lay_out_for_ass(str(cap), width=width).count("\\N") + 1
        out.append(band_anchored_margin(band_bottom=band_bottom, offset=offset, lines=n,
                                        font_size=font_size, canvas_height=canvas_height)
                   + max(0, int(lift)))
    return out


def base_text_margins(design, *, channel_design: dict | None, geom, ref_geom, work_top: int,
                      canvas_height: int = 1920) -> tuple[int, int, int, list[str]]:
    """자막·내레이션의 기준 margin_v — 번인 회피 **전** 값. 순수.

    프리셋 밴드 상대(preset_relative_margin) · 밴드 앵커(subtitle/tts_band_offset) · 내레이션 =
    대사 줄 · 채널 명시 절대값의 밴드 아래 클램프를 이 순서로 건다. render_final 과 썸네일 안전
    구역 판정(layout_extents)이 **같은 함수**를 쓴다 — 둘이 베끼면 판정과 화면이 갈린다.
    반환 (자막, 내레이션, 내레이션 기준값(클램프 전 — 줄별 lift 기준), 로그 줄)."""
    from app.modules.subtitle_region import estimate_subtitle_height
    cd = channel_design or {}
    H = int(canvas_height)
    notes: list[str] = []
    sub_from_channel = cd.get("subtitle_y_margin") is not None
    tts_from_channel = cd.get("tts_y_margin") is not None
    sub_m = int(design.subtitle_y_margin)
    tts_m = int(design.tts_line_y_margin)
    if not sub_from_channel:
        sub_m = preset_relative_margin(sub_m, ref_band_bottom=ref_geom.bottom,
                                       band_bottom=geom.bottom)
    if not tts_from_channel:
        tts_m = preset_relative_margin(tts_m, ref_band_bottom=ref_geom.bottom,
                                       band_bottom=geom.bottom)
    if geom.bottom != ref_geom.bottom:
        notes.append(f"[v3/자막배치] 프리셋 밴드 하단 {ref_geom.bottom} → 이 편 {geom.bottom} — "
                     f"자막 margin_v {sub_m}{'(채널 명시)' if sub_from_channel else ''} · "
                     f"내레이션 {tts_m}{'(채널 명시)' if tts_from_channel else ''}")
    sub_off = cd.get("subtitle_band_offset")
    tts_off = cd.get("tts_band_offset")
    if sub_off is not None:          # 대사 어절 자막은 v3 에서 늘 한 줄(12자)
        sub_m = band_anchored_margin(band_bottom=geom.bottom, offset=int(sub_off),
                                     lines=1, font_size=design.subtitle_size, canvas_height=H)
        notes.append(f"[v3/자막배치] 자막 — 밴드 하단 {geom.bottom} + {sub_off}px 앵커 → margin_v {sub_m}")
    if tts_off is not None:          # 전역값은 두 줄 기준(번인 회피·구 소비자용), 줄별은 render_final
        tts_m = band_anchored_margin(band_bottom=geom.bottom, offset=int(tts_off),
                                     lines=2, font_size=design.tts_line_font_size, canvas_height=H)
        notes.append(f"[v3/자막배치] 내레이션 — 밴드 하단 {geom.bottom} + {tts_off}px 앵커 → margin_v {tts_m}(2줄 기준)")
    if not tts_from_channel and tts_off is None:
        # 내레이션 아랫줄을 대사 자막과 같은 자리에(v10: 둘 다 SUB_Y 앵커 'md'). 대사·내레이션은
        # 시간상 겹치지 않으므로(human 흐름) 한 자리를 나눠 쓴다. 두 줄 내레이션은 위로 쌓인다.
        if tts_m != sub_m:
            notes.append(f"[v3/자막배치] 내레이션 margin_v {tts_m} → {sub_m} (대사 자막과 같은 줄)")
        tts_m = sub_m
    tts_base = tts_m
    # 밴드 아래로 내리기(2026-09-04) — **채널이 명시한 절대 margin** 이 밴드 위치와 안 맞으면
    # 두 줄 블록이 영상에 얹힌다(가왕쇼 7화: tts 550 vs video_y 500). 프리셋·상대 앵커 값은
    # 밴드 안쪽이 의도라 안 건다. 번인 회피(render_final)는 이 값 위에서 **올리기만** 한다.
    for name, size, cur in (("자막", design.subtitle_size, sub_m),
                            ("내레이션", design.tts_line_font_size, tts_m)):
        if name == "자막" and (sub_off is not None or not sub_from_channel):
            continue
        if name == "내레이션" and (tts_off is not None or not tts_from_channel):
            continue
        # 대사 어절 자막은 v3 에서 늘 한 줄(12자) — 두 줄 블록으로 재면 75px 을 더 내린다
        blk = estimate_subtitle_height(size, lines=1 if name == "자막" else 2)
        new, note = fit_margin_below_band(cur, canvas_height=H, band_bottom=geom.bottom,
                                          block_height=blk, work_top=work_top)
        if note:   # 로고 근접 ⚠ 는 자막 스택이 작품명을 내려서 푼다 — 여기선 안 찍는다
            notes.append(f"[v3/자막배치] {name} — {note.split(' ⚠')[0]}")
        if name == "자막":
            sub_m = int(new)
        else:
            tts_m = int(new)
    return sub_m, tts_m, tts_base, notes


# 썸네일 안전 구역(2026-09-11 사용자 지시 "썸네일 안에 제목/영상화면/작품 로고가 다 들어오게").
# 쇼츠 피드·채널 탭 썸네일은 2:3 **가운데 크롭**이라 캔버스 약 148~1770 만 보인다(지금불륜 2화 ·
# 가왕쇼 네 편 썸네일 스크린샷을 완성본 밴드 경계에 맞대어 잰 값 — docs/shorts_thumbnail_safe_zone.md).
# 경계는 사용자가 "맨 위와 맨 아래 경계 자체는 참고할 만 해" 라고 지목한 가왕쇼 템플릿에 맞췄다
# (제목 블록 윗변 187 · 캡션 블록 아랫변 1711 → 둘 다 안). 지금불륜은 로고가 1831 까지 내려가 잘렸다.
SAFE_TOP_TOLERANCE_PX = 2
THUMB_SAFE_TOP = _sz.SAFE_TOP   # 200 — 제목 블록 윗변 하한(재생 화면 상단 아이콘 · 종전 180 = 썸네일만)
THUMB_SAFE_BOTTOM = 1715      # 하단 블록(플랫폼 줄·작품명/로고·캡션) 아랫변 상한
LOGO_MIN_SCALE = 0.7          # 로고 축소 하한(채널 박스에 contain 한 크기 대비)
TITLE_MIN_SCALE = 0.8         # 제목 크기 상한 축소 하한


def layout_extents(design, *, channel_design: dict | None = None, style_preset: str | None = None,
                   line_count: int = 2, canvas_width: int = 1080,
                   canvas_height: int = 1920) -> dict:
    """세로 배치의 위·아래 끝 — 제목 블록 윗변 · 밴드 · 하단 블록(자막 스택 반영) 아랫변. 순수
    (로고 PNG 크기만 읽는다). render_final 과 같은 함수들로 잰다. 번인 자막 회피는 넣지 않는다 —
    회피는 자막을 **올리기만** 해서 작품명을 덜 밀므로, 이 값은 실제보다 같거나 아래(보수적)다."""
    from app.modules import subtitle_region as _sr
    H = int(canvas_height)
    geom = _sr.band_geometry(design, canvas_width=canvas_width, canvas_height=H)
    ref_geom = _sr.band_geometry(design_from_style(stage4.get_style_preset(style_preset)),
                                 canvas_width=canvas_width, canvas_height=H)
    title_top, _title_bottom = _sr.estimate_title_block(design, geom, line_count=line_count)
    work_top = estimate_work_top(design, band_bottom=geom.bottom, canvas_height=H)
    sub_m, tts_m, _base, _notes = base_text_margins(design, channel_design=channel_design,
                                                    geom=geom, ref_geom=ref_geom,
                                                    work_top=work_top, canvas_height=H)
    work_h = estimate_work_height(design)
    _s, _t, min_top, _n = stack_work_below_text(sub_margin=sub_m, tts_margin=tts_m,
                                                work_top=work_top, work_height=work_h,
                                                canvas_height=H)
    top = estimate_work_top(design, band_bottom=geom.bottom, canvas_height=H, min_top=min_top)
    return {"title_top": int(title_top), "band_top": int(geom.top), "band_bottom": int(geom.bottom),
            "work_top": int(top), "bottom": int(top + work_h)}


def fit_thumbnail_safe_zone(design, *, channel_design: dict | None = None,
                            style_preset: str | None = None, line_count: int = 2,
                            canvas_width: int = 1080, canvas_height: int = 1920) -> tuple[Any, dict]:
    """제목 윗변 ≥ THUMB_SAFE_TOP · 하단 블록 아랫변 ≤ THUMB_SAFE_BOTTOM 이 되게 design 을 고친다. 순수.

    이미 안이면 **design 그대로**(회귀 0). 밖이면 이 순서로 — 앞 단계일수록 화면 손실이 없다:
      ① 작품 로고 가운데 정렬의 빈 공간을 걷는다(밴드 +20 에 붙임)
      ② 반대쪽 여유만큼 밴드를 옮긴다(제목·자막·로고가 전부 밴드 상대라 같이 움직인다)
      ③ 로고를 줄인다(contain 박스 높이, 하한 LOGO_MIN_SCALE) — 위가 모자랄 때도 로고를 줄여 아래 여유를 만들고 ②
      ④ 제목 크기 상한을 줄이고(하한 TITLE_MIN_SCALE) 다시 ②
    그래도 밖이면 unmet 에 모자란 px 을 남긴다(렌더는 막지 않는다 — 템플릿을 사람이 고칠 몫).

    ⚠ 제목은 이 편의 실제 크기가 아니라 **채널 상한(design.title_sizes)** 으로 잰다 — 편마다 제목
    길이가 달라도 밴드·로고 자리가 같아야 채널 썸네일 격자가 가지런하다(가왕쇼 네 편처럼).
    실제 제목은 fit_title_sizes 가 상한 이하로만 줄이므로 늘 이 판정보다 안쪽이다."""
    import dataclasses as _dc

    kw = dict(channel_design=channel_design, style_preset=style_preset, line_count=line_count,
              canvas_width=canvas_width, canvas_height=canvas_height)

    def _ext(dd):
        return layout_extents(dd, **kw)

    def _viol(e):
        # 위는 2px 까지 봐준다 — 로고 하한에 걸린 1px 때문에 제목 글자를 줄이지 않게(2026-09-18 "크기는 고정")
        return max(0, e["bottom"] - THUMB_SAFE_BOTTOM), max(0, THUMB_SAFE_TOP - e["title_top"] - SAFE_TOP_TOLERANCE_PX)

    def _shift(dd, e):
        over, short = _viol(e)
        room_top = e["title_top"] - THUMB_SAFE_TOP
        room_bottom = THUMB_SAFE_BOTTOM - e["bottom"]
        delta = 0
        if over and room_top > 0:
            delta = -min(over, room_top)
        elif short and room_bottom > 0:
            delta = min(short, room_bottom)
        if not delta:
            return dd, e
        dd = _dc.replace(dd, video_y=int(e["band_top"]) + delta)
        e2 = _ext(dd)
        actions.append(f"밴드 {'위' if delta < 0 else '아래'}로 {abs(delta)}px (video_y "
                       f"{e['band_top']} → {e2['band_top']})")
        return dd, e2

    e0 = _ext(design)
    info: dict[str, Any] = {"safe": [THUMB_SAFE_TOP, THUMB_SAFE_BOTTOM], "before": e0}
    over, short = _viol(e0)
    if not over and not short:
        info.update(after=e0, actions=[], unmet=None, band_shift=0)
        return design, info
    actions: list[str] = []
    d, e = design, e0
    is_logo = getattr(d, "work_type", "text") == "image" and bool(getattr(d, "work_value", None))
    if over and is_logo and getattr(d, "work_image_align", "top") == "center" \
            and getattr(d, "work_band_offset", None) is None:
        d = _dc.replace(d, work_band_offset=WORK_GAP_BELOW_VIDEO)
        e2 = _ext(d)
        actions.append(f"작품 로고를 밴드 아래 {WORK_GAP_BELOW_VIDEO}px 에 붙임(가운데 정렬 해제) — "
                       f"아랫변 {e['bottom']} → {e2['bottom']}")
        e = e2
    d, e = _shift(d, e)
    over, _short = _viol(e)
    # 위가 모자라도(제목 윗변 < 상한) 로고를 먼저 줄여 아래 여유를 만들고 밴드를 내린다(2026-09-18 사용자
    # 결정 "제목 글자 크기는 줄이지 않는다" — 안전 박스 윗변 180→200 으로 가왕쇼 제목이 92/112→78/95 로
    # 줄던 것). 제목 축소(④)는 로고 하한까지 쓰고도 모자랄 때만.
    _room_bottom = THUMB_SAFE_BOTTOM - e["bottom"]
    _need = over + max(0, _short - max(0, _room_bottom))
    if _need and is_logo:
        cur_h = _work_item_height(d)
        new_h = max(int(math.ceil(cur_h * LOGO_MIN_SCALE)), cur_h - _need)
        new_h -= new_h % 2                     # 렌더러가 짝수로 내린다 — 올리면 1px 넘쳐 ④까지 번진다(실측)
        if new_h < cur_h:
            d = _dc.replace(d, work_image_height=new_h)
            e2 = _ext(d)
            actions.append(f"작품 로고 높이 {cur_h} → {_work_item_height(d)}px — 아랫변 "
                           f"{e['bottom']} → {e2['bottom']}")
            e = e2
            d, e = _shift(d, e)
    over, short = _viol(e)
    if over or short:
        sizes = [int(s) for s in (d.title_sizes or [d.title_size])]
        n = max(1, int(line_count))
        cur = sum(sizes[i] if i < len(sizes) else sizes[-1] for i in range(n))
        k = max(TITLE_MIN_SCALE, (cur - over - short) / float(cur)) if cur else 1.0
        new_sizes = [max(40, int(s * k)) for s in sizes]
        if new_sizes != sizes:
            d = _dc.replace(d, title_sizes=new_sizes, title_size=new_sizes[0])
            e2 = _ext(d)
            actions.append(f"제목 크기 상한 {sizes} → {new_sizes} — 윗변 {e['title_top']} → {e2['title_top']}")
            e = e2
            d, e = _shift(d, e)
    over, short = _viol(e)
    info.update(after=e, actions=actions,
                unmet=({"bottom_over": over, "top_short": short} if over or short else None),
                band_shift=int(e["band_top"]) - int(e0["band_top"]))
    return d, info


def cover_mute_windows(timeline: list[dict],
                       narration_windows_src: list[tuple[float, float]],
                       fps: float | None = None) -> list[tuple[float, float]]:
    """원음을 끌 편집본 좌표 창(순수) — use_original_audio=False 클립 중 내레이션이
    점유하지 **않은** 구간.

    누적은 렌더가 실제로 만드는 프레임 격자로 센다(assemble.clip_duration). 실수
    누적을 쓰면 조각마다 밀려, 덮개 뮤트가 화면보다 먼저 시작하고 먼저 끝난다 —
    ① 앞 장면 대사가 잘리고 ② 덮개 꼬리의 원본 대사가 새어나온다(2026-09-03 실측
    0.32초, 지금불륜이문제가아닙니다_b0ccda99). 클립 끝까지 가는 창은 계획 길이가
    아니라 격자 길이까지 덮어야 반 프레임분도 안 샌다."""
    out: list[tuple[float, float]] = []
    off = 0.0
    for c in timeline:
        cs, ce = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        speed = float(c.get("playback_speed") or 1.0)
        dur = assemble.clip_duration(assemble.clip_len(c), fps)
        if not c.get("use_original_audio"):
            for a, z, on in assemble.split_by_windows(cs, ce, narration_windows_src):
                if on:
                    continue
                r0 = min((a - cs) / speed, dur)
                r1 = dur if z >= ce - 1e-6 else min((z - cs) / speed, dur)
                out.append((round(off + r0, 3), round(off + r1, 3)))
        off += dur
    return out


def with_tts_audible_bounds(cue_files: list[dict], *, bounds_fn=None) -> list[dict]:
    """체크포인트를 바꾸지 않고 렌더용 cue 사본에 실제 발화 끝을 붙인다."""
    if bounds_fn is None:
        from app.modules.sfx_narration import audible_bounds_sec
        bounds_fn = audible_bounds_sec
    out: list[dict] = []
    for cf in cue_files:
        item = dict(cf)
        cue = dict(cf.get("cue") or {})
        item["cue"] = cue
        if cue.get("audible_end_sec") is not None:
            out.append(item)
            continue
        try:
            _lead, audible_end = bounds_fn(Path(cf["path"]))
            if audible_end > 0:
                cue["audible_end_sec"] = round(float(cue["start_sec"]) + audible_end, 3)
        except (KeyError, TypeError, ValueError, OSError):
            pass
        out.append(item)
    return out


def fit_mute_windows_to_tts(muted: list[tuple[float, float]],
                            cue_files: list[dict], *, tail_pad_sec: float = 0.04,
                            bounds_fn=None) -> list[tuple[float, float]]:
    """계획 cue의 빈 꼬리를 원음 뮤트 창에서 제거한다.

    TTS 계획 창은 화면 예산이고 MP3의 실제 발화 길이와 다르다. 계획 끝까지 원음을
    완전 뮤트하면 발화 뒤 무음이 생긴 다음 현장음이 갑자기 복귀한다. 실제 발화 끝에
    짧은 여유만 더한 시각으로 창을 줄인다. cue와 무관한 기존 뮤트 창은 보존한다.
    """
    if not muted or not cue_files:
        return list(muted)
    measured = with_tts_audible_bounds(cue_files, bounds_fn=bounds_fn)
    cues: list[tuple[float, float, float]] = []
    for cf in measured:
        cue = cf.get("cue") or {}
        try:
            start, planned_end = float(cue["start_sec"]), float(cue["end_sec"])
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if planned_end <= start:
            continue
        effective_end = planned_end
        audible_end = cue.get("audible_end_sec")
        if audible_end is not None:
            effective_end = min(planned_end, float(audible_end) + tail_pad_sec)
        cues.append((start, planned_end, effective_end))
    out: list[tuple[float, float]] = []
    for a, z in muted:
        related = [c for c in cues if c[0] < z and a < c[1]]
        if not related:
            out.append((a, z))
            continue
        for start, _planned_end, effective_end in related:
            lo, hi = max(a, start), min(z, effective_end)
            if hi > lo:
                out.append((round(lo, 3), round(hi, 3)))
    # 한 cue가 여러 렌더 클립에 걸리면 창도 잘려 나온다. 조각마다 페이드하면
    # 내레이션 도중 원음이 반복해서 솟으므로 맞닿은 창은 다시 하나로 합친다.
    merged: list[tuple[float, float]] = []
    for a, z in sorted(out):
        if merged and a <= merged[-1][1] + 0.002:
            merged[-1] = (merged[-1][0], max(merged[-1][1], z))
        else:
            merged.append((a, z))
    return merged


def place_above_burned(margin_v: int, burned: list[tuple[int, int]], *,
                       canvas_height: int, subtitle_height: int,
                       floor_top: int, gap: int = SUB_GAP_PX) -> tuple[int, str | None]:
    """원본 자막 띠들을 피한 margin_v 와 메모. 순수·결정적.

    규율은 E17-2 를 따른다 — **올리기만** 하고(내리면 로고와 부딪힌다), 제목 아래
    (floor_top)를 넘지 않는다. 다만 띠를 **하나가 아니라 전부** 본다: 기본 자리에서
    위로 올라가며 우리 자막 상자가 어떤 띠와도 겹치지 않는 첫 자리를 고른다.
    끝까지 자리가 없으면 갈 수 있는 데까지만 가고 **모자란 것을 메모로 남긴다**."""
    H = int(canvas_height)
    box_bottom = H - int(margin_v)

    def hits(bottom: int) -> tuple[int, int] | None:
        top = bottom - int(subtitle_height)
        for a, z in burned:
            if a - gap < bottom and top < z + gap:
                return (a, z)
        return None

    hit = hits(box_bottom)
    if hit is None:
        return int(margin_v), None
    # 겹치는 띠 **위**로 올린다. 위쪽에 또 띠가 있으면 다시 올린다(여러 줄 텔롭).
    bottom = box_bottom
    for _ in range(len(burned) + 1):
        hit = hits(bottom)
        if hit is None:
            break
        bottom = hit[0] - gap
    top = bottom - int(subtitle_height)
    if top < floor_top:                       # 제목을 침범해야만 피할 수 있는 경우
        short = floor_top - top
        bottom = floor_top + int(subtitle_height)
        return (H - bottom,
                f"띠를 다 피하려면 {short}px 더 올려야 하는데 제목에 막혀 여기까지만")
    return H - int(bottom), f"원본 자막 {len(burned)}띠 회피 (margin_v {margin_v} → {H - int(bottom)})"


def detect_burned_subtitles(video_path: Path, clips: list, design, output_dir: Path,
                            log=print) -> tuple[dict | None, list]:
    """소스에 박힌 자막 띠와 구간별 표본. 실패는 (None, []) — 본편을 막지 않는다.

    E17-2/E18-2 는 v1 이 쓰던 안전장치인데 v3 는 밴드 기하 계산만 빌려 쓰고 회피는
    배선하지 않았다(사용자 지적: 원본 '브레이크가 고장' 위에 우리 자막이 깔림).
    검출은 비싸므로 클립 구성 지문으로 사이드카에 캐시한다."""
    import hashlib

    from app.modules import subtitle_region as _sr
    sig = hashlib.sha1((";".join(f"{c.start_sec:.3f}-{c.end_sec:.3f}" for c in clips)
                        + f"|{design.aspect_ratio}|{design.video_y}").encode()).hexdigest()[:16]
    cache = output_dir / "checkpoint_burned_subtitle.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("signature") == sig:
                log("  [v3/자막회피] 검출 결과 재사용")
                return data.get("band"), list(data.get("profiles") or [])
        except (OSError, ValueError):
            pass
    band, profiles = None, []
    try:
        band = _sr.detect_burned_band(video_path, clips, design)
        profiles = _sr.detect_burned_profiles(video_path, clips, design)
    except Exception as e:                        # 안전장치가 발행을 막으면 안 된다
        log(f"  [v3/자막회피] 검출 실패({type(e).__name__}) — 자막 위치는 종전 그대로")
        return None, []
    try:
        cache.write_text(json.dumps({"signature": sig, "band": band,
                                     "profiles": profiles}, ensure_ascii=False),
                         encoding="utf-8")
    except OSError:
        pass
    if band:
        log(f"  [v3/자막회피] 원본 자막 띠 y={band['top']}~{band['bottom']} "
            f"(표본 {band['frames']}프레임/{band['clips']}클립)")
    else:
        log("  [v3/자막회피] 상시 자막 띠 없음")
    if profiles:
        log(f"  [v3/자막회피] 구간별 표본 {len(profiles)}프레임")
    return band, profiles


# ── 최종 렌더 어댑터 ────────────────────────────────────────────────────────

def read_picture_area(output_dir: Path) -> dict | None:
    """`checkpoint_probe.json` 의 그림 영역(`picture`) — 레터박스가 있을 때만 dict.

    probe 단계(`pipeline._load_or_probe_media`)가 additive 로 실은 키다. 파일·키 없음 ·
    실패 기록(`error`) · 그림 영역 = 컨테이너 전체(체크포인트의 width/height 와 대조)는
    전부 None = 종전 경로 — 앵커 없는 편이 불필요한 ffprobe 없이 빈 맵으로 간다."""
    from app.v3 import letterbox

    p = Path(output_dir) / "checkpoint_probe.json"
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return None
    pic = doc.get("picture")
    if not isinstance(pic, dict) or "error" in pic:
        return None
    try:
        out = {"x": int(pic["x"]), "y": int(pic["y"]),
               "w": int(pic["w"]), "h": int(pic["h"])}
        if letterbox.is_full(out, int(doc["width"]), int(doc["height"])):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return out


def fixed_crop_map(timeline: list[dict], *, output_dir: Path, aspect_ratio: str, picture: dict | None,
                   video_path: Path, log=print) -> tuple[dict[str, Path], list[dict]]:
    """클립 `reframe.mode == "fixed"`(x·선택 y, 소스 px) → 키프레임 둘짜리 고정 크롭 맵(2026-09-11).
    사람이 지정한 자리라 검출과 무관하다 — 그림 사각형·밴드 크롭 안으로만 클램프한다. 없으면 빈 dict(회귀 0)."""
    out: dict[str, Path] = {}
    audit: list[dict] = []
    want = [(i, c) for i, c in enumerate(timeline) if (c.get("reframe") or {}).get("mode") == "fixed"]
    if not want:
        return out, audit
    src = None
    pr = Path(output_dir) / "checkpoint_probe.json"
    if pr.exists():
        try:
            pj = json.loads(pr.read_text(encoding="utf-8"))
            if pj.get("width") and pj.get("height"):
                src = (int(pj["width"]), int(pj["height"]))
        except (OSError, ValueError):
            src = None
    if src is None:
        try:
            o = subprocess.run([find_ffmpeg_command("ffprobe"), "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video_path)],
                               capture_output=True, text=True, check=True).stdout.strip()
            w, h = o.split("\n")[0].split(",")[:2]
            src = (int(w), int(h))
        except Exception as e:  # noqa: BLE001
            log(f"  [v3/render] ⚠ 고정 크롭 — 소스 해상도 프로브 실패, 생략: {e}")
            return out, audit
    geo = band_crop_size(aspect_ratio, src, picture)
    if geo is None:
        log("  [v3/render] ⚠ 고정 크롭 — 밴드 비율 크롭 불가, 생략")
        return out, audit
    crop_w, crop_h, pic_x, pic_y, pic_w, pic_h = geo
    for i, c in want:
        rf = c["reframe"]
        x = min(max(float(rf["x"]), pic_x + crop_w / 2), pic_x + pic_w - crop_w / 2)
        y = float(rf.get("y")) if rf.get("y") is not None else pic_y + pic_h / 2
        y = min(max(y, pic_y + crop_h / 2), pic_y + pic_h - crop_h / 2)
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        rows = [{"time_sec": round(s, 3), "x_center": round(x, 1), "y_center": round(y, 1), "crop_w": crop_w, "crop_h": crop_h},
                {"time_sec": round(e, 3), "x_center": round(x, 1), "y_center": round(y, 1), "crop_w": crop_w, "crop_h": crop_h}]
        p = Path(output_dir) / f"v3_crop_fixed_{i}.json"
        p.write_text(json.dumps(rows), encoding="utf-8")
        out[f"{c.get('role') or 'build'}_{i}"] = p
        audit.append({"clip": i, "x": round(x, 1), "y": round(y, 1)})
        log(f"  [v3/render] 고정 크롭 clip{i} {s:.2f}~{e:.2f}s → x {x:.0f} (사람 지정)")
    return out, audit


def subject_crop_map(timeline: list[dict], *, video_path: Path,
                     aspect_ratio: str, output_dir: Path,
                     src_size: tuple[int, int] | None = None,
                     picture: dict | None = None, anchors: bool = True,
                     log=print) -> dict[str, Path]:
    """무성 인서트 클립의 subject_pos → 렌더러 crop_timeline_map (2026-09-02).

    v3 는 얼굴 크롭이 범위 외라 전 클립 고정 **중앙** 크롭이었다 — 구석의 주 피사체
    (실사고: GPS 폰 화면)가 세로 크롭에 통째로 잘려 나갔다. Stage 2 가 영상을 보며
    적어 둔 subject_pos(left/right)를 그 클립의 crop x 앵커로 소비한다. 렌더러는
    이미 crop_timeline_map 을 받게 되어 있으므로(v1 얼굴 추적과 같은 통로) 렌더
    코드는 무변경. 키가 없는 클립 = 종전 중앙(회귀 0). 순수 재료라 재개마다 재도출
    해도 같은 좌표다(E19-4 얼굴 회피와 같은 이유로 체크포인트에 안 남긴다).

    **레터박스(2026-09-07)**: `picture={x,y,w,h}`(probe 단계 검출, 소스 좌표)가
    컨테이너와 다르면 **모든 클립**에 크롭을 내되 그림 사각형 안에서 밴드 비율을
    맞춘다(높이 우선 — crop_w 가 그림 폭을 넘으면 폭 기준). x 앵커 규칙(좌/우
    0.25/0.75 · 아니면 중앙)은 그림 영역 좌표로, y 는 그림 중앙. 그림 영역이
    컨테이너 전체(또는 None)면 종전과 **정확히 같은 맵**(회귀 0).
    `anchors=False` 는 subject_pos 를 무시한다(채널 face_tracking=false — 레터박스
    제거는 앵커와 무관한 소스 성질이라 그 분기에서도 x 중앙으로 적용한다)."""
    from app.v3 import letterbox

    anchored = [(i, c) for i, c in enumerate(timeline)
                if anchors and c.get("subject_pos") in ("left", "right")]
    if src_size is not None:
        has_letterbox = not letterbox.is_full(picture, *src_size)
    else:
        # 소스 크기를 아직 모른다 — picture 가 컨테이너를 실어 오지 않으므로 아래
        # ffprobe 뒤에 다시 판정한다. 앵커도 없고 picture 도 없으면 프로브 자체를 안 한다.
        has_letterbox = bool(picture)
    if not anchored and not has_letterbox:
        return {}
    if src_size is None:
        try:
            out = subprocess.run(
                [find_ffmpeg_command("ffprobe"), "-v", "error",
                 "-select_streams", "v:0", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", str(video_path)],
                capture_output=True, text=True, check=True).stdout.strip()
            w_s, h_s = out.split("\n")[0].split(",")[:2]
            src_size = (int(w_s), int(h_s))
        except Exception as e:  # noqa: BLE001 — 앵커는 부가물, 실패 = 종전 중앙
            log(f"  [v3/render] ⚠ 소스 해상도 프로브 실패 — 피사체 앵커 생략: {e}")
            return {}
    src_w, src_h = src_size
    has_letterbox = not letterbox.is_full(picture, src_w, src_h)
    if not anchored and not has_letterbox:
        return {}
    try:
        r_w, r_h = (int(x) for x in str(aspect_ratio).split(":"))
    except (ValueError, AttributeError):
        return {}
    if r_w <= 0 or r_h <= 0:
        return {}
    out_map: dict[str, Path] = {}

    if not has_letterbox:
        # ── 종전 경로(그대로) — 밴드 비율의 최대 크롭. 렌더러 하류(scale increase →
        # 중앙 crop)가 재크롭으로 앵커를 되물리지 않으려면 **정확히 밴드 비율**로 잘라야 한다
        crop_h = src_h
        crop_w = int(src_h * r_w / r_h) & ~1
        if crop_w >= src_w - 2:          # 가로 여유가 없다(세로 크롭 소재) — 앵커 무의미
            return {}
        for i, c in anchored:
            frac = 0.25 if c["subject_pos"] == "left" else 0.75
            x_center = min(max(frac * src_w, crop_w / 2), src_w - crop_w / 2)
            kf = [{"time_sec": 0.0, "x_center": round(x_center, 1),
                   "y_center": src_h / 2, "crop_w": crop_w, "crop_h": crop_h}]
            p = output_dir / f"v3_crop_subject_{i}.json"
            p.write_text(json.dumps(kf), encoding="utf-8")
            out_map[f"{c.get('role') or 'build'}_{i}"] = p
            log(f"  [v3/render] 피사체 앵커 clip{i} {c['subject_pos']} — "
                f"crop x_center {x_center:.0f}/{src_w}")
        return out_map

    # ── 레터박스 경로 — 그림 사각형 안에서 밴드 비율 최대 크롭, 전 클립 ──────
    pic_x, pic_y = int(picture["x"]), int(picture["y"])
    pic_w, pic_h = int(picture["w"]), int(picture["h"])
    crop_h = pic_h & ~1
    crop_w = int(crop_h * r_w / r_h) & ~1
    if crop_w > pic_w:               # 그림이 밴드보다 좁다(세로 크롭 소재) → 폭 기준
        crop_w = pic_w & ~1
        crop_h = int(crop_w * r_h / r_w) & ~1
    if crop_w <= 0 or crop_h <= 0:
        return {}
    anchor_by_idx = {i: c["subject_pos"] for i, c in anchored}
    y_center = pic_y + pic_h / 2
    log(f"  [v3/render] 레터박스 크롭 — 그림 {pic_w}×{pic_h}@({pic_x},{pic_y}) 안에서 "
        f"{r_w}:{r_h} 최대 {crop_w}×{crop_h}, 클립 {len(timeline)}개"
        + ("" if anchors else " (face_tracking=false — x 중앙)"))
    for i, c in enumerate(timeline):
        pos = anchor_by_idx.get(i)
        frac = 0.25 if pos == "left" else 0.75 if pos == "right" else 0.5
        x_center = min(max(pic_x + frac * pic_w, pic_x + crop_w / 2),
                       pic_x + pic_w - crop_w / 2)
        kf = [{"time_sec": 0.0, "x_center": round(x_center, 1),
               "y_center": y_center, "crop_w": crop_w, "crop_h": crop_h}]
        p = output_dir / f"v3_crop_subject_{i}.json"
        p.write_text(json.dumps(kf), encoding="utf-8")
        out_map[f"{c.get('role') or 'build'}_{i}"] = p
        if pos:
            log(f"  [v3/render] 피사체 앵커 clip{i} {pos} — "
                f"crop x_center {x_center:.0f}/{src_w} (그림 영역 기준)")
    return out_map


def band_crop_size(aspect_ratio: str, src_size: tuple[int, int],
                   picture: dict | None) -> tuple[int, int, int, int, int, int] | None:
    """밴드 비율의 최대 크롭 (crop_w, crop_h, pic_x, pic_y, pic_w, pic_h) — subject_crop_map 과
    같은 수식(그림 사각형 안 · 높이 우선). 비율이 깨졌거나 가로 여유가 없으면 None."""
    from app.v3 import letterbox
    src_w, src_h = src_size
    try:
        r_w, r_h = (int(x) for x in str(aspect_ratio).split(":"))
    except (ValueError, AttributeError):
        return None
    if r_w <= 0 or r_h <= 0:
        return None
    if letterbox.is_full(picture, src_w, src_h):
        pic_x, pic_y, pic_w, pic_h = 0, 0, src_w, src_h
    else:
        pic_x, pic_y = int(picture["x"]), int(picture["y"])
        pic_w, pic_h = int(picture["w"]), int(picture["h"])
    crop_h = pic_h & ~1
    crop_w = int(crop_h * r_w / r_h) & ~1
    if crop_w > pic_w:
        crop_w = pic_w & ~1
        crop_h = int(crop_w * r_h / r_w) & ~1
    if crop_w <= 0 or crop_h <= 0 or crop_w >= pic_w - 2:
        return None
    return crop_w, crop_h, pic_x, pic_y, pic_w, pic_h


SPEAKER_EMA_ALPHA = 0.35     # 0.5s 표본 기준 — 0.12(v1, 연속 프레임용)는 한 클립 안에서 수렴을 못 한다
SPEAKER_TRACKING_DEFAULT = "on"    # v3 기본 켜짐(2026-09-08 사용자 결정) — design speaker_tracking=off 로 끈다
# 화자 고정(hold, 2026-09-08 사용자 지시 "카메라가 왔다갔다 하지 않았으면") — v9 AUTO_CX 방식: 발화 구간마다
# 크롭 x 하나로 고정하고 구간 경계에서 한 프레임에 점프. speaker_tracking=pan 이 종전 EMA 연속 추적.
SPEAKER_HOLD_MIN_SEC = 1.0          # 이보다 짧은 발화 구간("어?")은 앞 구간에 붙인다 — 추임새로 카메라가 튀지 않게
SPEAKER_REHOLD_MIN_RATIO = 0.15     # 새 화자 위치가 그림 폭의 이 비율 미만으로만 다르면 재고정하지 않는다


def utterances_from_segments(segments: list[dict], timeline: list[dict],
                             fps: float | None) -> list[tuple[float, float, str]]:
    """자막 세그먼트(편집본 좌표·speaker) → 소스 좌표 발화 목록 [(t0, t1, speaker)]. 순수.

    편집본→소스 환산은 `assemble.edited_offsets` 와 같은 격자(자막을 만든 자와 동일)."""
    offs = assemble.edited_offsets(timeline, fps)
    out: list[tuple[float, float, str]] = []
    for sg in segments or []:
        a, z = float(sg["start_sec"]), float(sg["end_sec"])
        for (cs, ce, off), c in zip(offs, timeline):
            dur = assemble.clip_duration(assemble.clip_len(c), fps)
            if off <= a < off + dur and c.get("use_original_audio", True):
                t0 = cs + (a - off)
                t1 = min(ce, cs + (z - off))
                if t1 > t0:
                    out.append((round(t0, 3), round(t1, 3), str(sg.get("speaker") or "")))
                break
    return out


SPEAKER_LINK_RATIO = 0.08           # 표본 간 같은 얼굴로 묶는 거리(그림 폭 대비) — v9 autoframe LINK_DIST


SPEAKER_MEMORY_SEC = 60.0           # 같은 화자의 직전 위치 기억 유효 시간(소스 초) — 씬이 바뀌면 무효
SPEAKER_MEMORY_RATIO = 0.12         # 기억 위치에서 이 거리(그림 폭 대비) 안의 얼굴이면 그 얼굴
# run 의 화자 후보는 그 run 표본의 절반 이상에 있어야 한다(2026-09-10, 지금불륜 ep01x01 「대원 여러분」 실사고):
# 와이드 숏(가족 셋 x≈1300 · 7초 내내)에서 마지막 1.5초에만 잡힌 촬영감독 뒤통수 "얼굴"(x≈360, 표본 4/15)이
# talk×√w 로 이겨 크롭이 왼쪽 끝(500)에 붙고 말하는 가족은 오른쪽 가장자리로 밀렸다. 종전 0.3 은 15표본에서
# 4개(=int(4.5)) 를 통과시켰다. 절반을 못 채우는 사람이 하나도 없을 때만 전원으로 폴백한다.
SPEAKER_PRESENCE_RATIO = 0.5
# 초점 단서(2026-09-11): 표본 안 가장 선명한 얼굴 대비 이 비율 미만이면 초점 밖(흐린 전경) — 실측 5.8/10.6 = 0.55.
SPEAKER_FOCUS_RATIO = 0.6


def rank_speaker_x(samples: list[dict], t0: float, t1: float, pic_w: float,
                   link_ratio: float = SPEAKER_LINK_RATIO,
                   prefer_x: float | None = None) -> tuple[float | None, dict]:
    """run 창 [t0,t1] 의 표본(모든 얼굴+입 움직임) → 화자 x. v9 autoframe 순위: 얼굴을 위치로 묶고
    talk(평균 입 움직임) × √(평균 폭) 최대. 움직임이 전부 0 이면 큰 얼굴. 순수."""
    people: list[dict] = []
    n_samples = 0
    for smp in samples:
        t = float(smp["t"])
        if t < t0 - 1e-6 or t > t1 + 1e-6:
            continue
        n_samples += 1
        faces_in = [tuple(f) for f in (smp.get("faces") or [])]
        # 6번째 값(선명도)이 있으면 표본 안 최대값 대비 비율 — 없으면(구 표본) 1.0 = 판정 없음
        sharp_max = max((float(f[5]) for f in faces_in if len(f) >= 6), default=0.0)
        for f in faces_in:
            cx, cy, w, h, mot = f[:5]
            rel = (float(f[5]) / sharp_max) if (len(f) >= 6 and sharp_max > 0) else 1.0
            hit = next((p for p in people if abs(p["cx"] - cx) < pic_w * link_ratio
                        and abs(p["cy"] - cy) < pic_w * link_ratio * 2), None)
            if hit is None:
                people.append({"cx": float(cx), "cy": float(cy), "w": float(w), "n": 1, "score": float(mot),
                               "sharp": rel})
            else:
                k = hit["n"]
                hit["cx"] = (hit["cx"] * k + cx) / (k + 1); hit["cy"] = (hit["cy"] * k + cy) / (k + 1)
                hit["w"] = max(hit["w"], float(w)); hit["n"] = k + 1; hit["score"] += float(mot)
                hit["sharp"] = (hit["sharp"] * k + rel) / (k + 1)
    import math as _math
    solid = [p for p in people if p["n"] >= max(1, _math.ceil(n_samples * SPEAKER_PRESENCE_RATIO))] or people
    # 초점 단서(2026-09-11, 「언제 밥 한 번」 실사고): 숏/리버스숏에서 초점 밖 전경(조여정 뒤통수·흐림)이 크고
    # 입 움직임도 잡혀 talk×√w 와 화자 기억을 다 가져갔다. 초점 안 사람이 하나라도 있으면 흐린 사람은 후보에서 뺀다.
    focused = [p for p in solid if p.get("sharp", 1.0) >= SPEAKER_FOCUS_RATIO]
    if focused and len(focused) < len(solid):
        solid = focused
    if not solid:
        return None, {"people": 0}
    for p in solid:
        p["talk"] = p["score"] / p["n"]
        p["rank"] = p["talk"] * (p["w"] ** 0.5) if p["talk"] > 0 else 0.0
    if prefer_x is not None:
        # 같은 화자가 직전 run 에서 잡힌 자리 근처에 얼굴이 있으면 그 얼굴 — 상대가 크게 리액션하면
        # talk×√w 가 상대로 넘어간다(실측: 「아니, 뭐 갑자기」에서 경희 x1625). 화자 이름은 Stage 2 가 안다.
        near = [p for p in solid if abs(p["cx"] - prefer_x) < pic_w * SPEAKER_MEMORY_RATIO]
        if near:
            best = max(near, key=lambda p: (p["rank"], p["w"]))
            return best["cx"], {"people": len(solid), "talk": round(best["talk"], 3), "w": int(best["w"]),
                                "method": "memory"}
    best = max(solid, key=lambda p: (p["rank"], p["w"]))
    return best["cx"], {"people": len(solid), "talk": round(best["talk"], 3), "w": int(best["w"]),
                        "method": "talk×√w" if best["rank"] > 0 else "largest"}


def hold_keyframes(rows: list[dict], utterances: list[tuple[float, float, str]],
                   *, clip_start: float, clip_end: float, fps: float, pic_w: float,
                   min_hold_sec: float = SPEAKER_HOLD_MIN_SEC,
                   rehold_ratio: float = SPEAKER_REHOLD_MIN_RATIO,
                   samples: list[dict] | None = None,
                   speaker_memory: dict[str, tuple[float, float]] | None = None,
                   scene_cuts: list[float] | None = None) -> tuple[list[dict], list[dict]]:
    """표본 행(face_cx·face_w, 소스 시각) + 발화 구간 → 계단식(hold) 키프레임. 순수.

    scene_cuts(2026-09-10): 클립 안 장면 전환 시각(grid) — 발화 run 이 컷을 넘으면 컷에서 쪼갠다. 카메라 컷은
    새 구도라 한 x 로 붙잡을 수 없다(클로즈업 → 와이드 안에서 같은 화자라도 얼굴 위치가 다르다). 컷에서 나뉜
    조각은 짧아도 앞뒤와 합치지 않는다 — 실제 컷 자리의 한 프레임 점프는 보이지 않는다.

    - 발화 run = 같은 화자가 이어지는 구간(그 클립 안). min_hold_sec 미만 run 은 앞 run(없으면 뒤)에 병합.
    - run 의 x = run 창 안 표본 중 얼굴이 잡힌 행의 face_cx **중앙값**. 표본이 없으면 직전 run 의 x
      (첫 run 이면 클립 전체 중앙값). 새 x 가 직전과 pic_w×rehold_ratio 미만 차이면 재고정하지 않는다.
    - 경계에서는 (t−1/fps, 이전 x)·(t, 새 x) 두 키프레임 = 렌더러 선형 보간 위에서 한 프레임 점프.
    - 대사 없는 앞머리는 첫 run 의 x 로 시작(클립 첫 프레임부터 화자에 맞춰 있다), 뒤꼬리는 마지막 x 유지.
    - 발화가 없는 클립은 얼굴 표본 중앙값 하나로 고정(팬 없음). 얼굴이 하나도 없으면 행의 x_center 중앙값.
    반환 (키프레임, 감사 run 목록)."""
    import statistics
    faced = [r for r in rows if float(r.get("face_w") or 0) > 0]

    def _median_x(sel: list[dict]) -> float | None:
        xs = [float(r.get("face_cx", r["x_center"])) for r in sel if float(r.get("face_w") or 0) > 0]
        return statistics.median(xs) if xs else None

    base = _median_x(faced) if faced else (
        statistics.median(float(r["x_center"]) for r in rows) if rows else None)
    tpl = {k: rows[0][k] for k in ("crop_w", "crop_h", "y_center")} if rows else {"crop_w": 0, "crop_h": 0, "y_center": 0.0}
    # run 만들기(클립 안 발화만, 시간순, 같은 화자 연속 병합)
    runs: list[list] = []
    for t0, t1, spk in sorted(utterances):
        a, z = max(t0, clip_start), min(t1, clip_end)
        if z <= a:
            continue
        if runs and runs[-1][2] == spk:
            runs[-1][1] = max(runs[-1][1], z)
        else:
            runs.append([a, z, spk])
    # 짧은 run 병합 — 앞 run 이 있으면 앞으로, 없으면 뒤로
    merged: list[list] = []
    for r in runs:
        if merged and (r[1] - r[0]) < min_hold_sec:
            merged[-1][1] = max(merged[-1][1], r[1]); continue
        if not merged and len(runs) > 1 and (r[1] - r[0]) < min_hold_sec:
            continue      # 첫 run 이 짧으면 다음 run 이 앞머리까지 맡는다
        merged.append(list(r))
    # 장면 전환에서 run 을 쪼갠다(컷 = 새 구도). 컷이 run 의 안쪽(양 끝 0.2s 제외)에 있을 때만.
    if scene_cuts and merged:
        _cuts = sorted(float(c) for c in scene_cuts if clip_start < float(c) < clip_end)
        split: list[list] = []
        for a, z, spk in merged:
            cur = a
            for c in _cuts:
                if cur + 0.2 < c < z - 0.2:
                    split.append([cur, c, spk]); cur = c
            split.append([cur, z, spk])
        merged = split
    audit: list[dict] = []
    if not merged or base is None:
        x = base if base is not None else (float(rows[0]["x_center"]) if rows else 0.0)
        kfs = [{"time_sec": round(clip_start, 3), "x_center": round(x, 1), **tpl}]
        return kfs, [{"speaker": None, "start": clip_start, "end": clip_end, "x": round(x, 1),
                      "faces": len(faced), "held": True}]
    xs: list[float] = []
    prev: float | None = None
    for a, z, spk in merged:
        sel = [r for r in rows if a - 1e-6 <= float(r["time_sec"]) <= z + 1e-6]
        how: dict = {}
        mx = None
        if samples:
            # 모든 얼굴 + 입 움직임이 있으면 v9 순위(talk×√w)로 run 의 화자를 **다시** 고른다 — 표본별
            # 선택(_pick_speaker, 면적 가중)은 큰 얼굴을 고집해 남편 대사에서도 경희를 잡았다(실측).
            _mem = (speaker_memory or {}).get(spk)
            _pref = _mem[0] if _mem and spk and abs(a - _mem[1]) <= SPEAKER_MEMORY_SEC else None
            mx, how = rank_speaker_x(samples, a, z, pic_w, prefer_x=_pref)
            if mx is not None and spk and speaker_memory is not None:
                speaker_memory[spk] = (float(mx), float(z))
        if mx is None:
            mx = _median_x(sel)
        if mx is None:
            mx = prev if prev is not None else base
        if prev is not None and abs(mx - prev) < pic_w * rehold_ratio:
            mx = prev                                  # 미세 이동 — 재고정 안 함
        xs.append(mx); prev = mx
        audit.append({"speaker": spk, "start": round(a, 3), "end": round(z, 3), "x": round(mx, 1),
                      "faces": sum(1 for r in sel if float(r.get("face_w") or 0) > 0), **how})
    kfs = [{"time_sec": round(clip_start, 3), "x_center": round(xs[0], 1), **tpl}]
    one = 1.0 / float(fps or 24.0)
    for i in range(1, len(merged)):
        tb = merged[i][0]
        if xs[i] == xs[i - 1]:
            continue
        kfs.append({"time_sec": round(tb - one, 3), "x_center": round(xs[i - 1], 1), **tpl})
        kfs.append({"time_sec": round(tb, 3), "x_center": round(xs[i], 1), **tpl})
    kfs.append({"time_sec": round(clip_end, 3), "x_center": round(xs[-1], 1), **tpl})
    return kfs, audit
FACE_DETECTOR_DEFAULT = "yunet"    # v3 기본 YuNet — 기울어진 얼굴(누운 경희)을 Haar 는 못 잡는다


def read_scene_cuts(output_dir: Path) -> list[float]:
    """grid.json 의 장면 전환 시각(ffmpeg select=gt(scene,0.3)) — 없으면 빈 목록(종전 동작)."""
    p = Path(output_dir) / "grid.json"
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8")).get("scene_cuts") or []
    except (OSError, ValueError):
        return []
    out: list[float] = []
    for c in raw:
        v = c if isinstance(c, (int, float)) else (c.get("t") if isinstance(c, dict) else None)
        if isinstance(v, (int, float)):
            out.append(float(v))
    return sorted(out)


def speaker_crop_map(timeline: list[dict], *, video_path: Path, aspect_ratio: str,
                     output_dir: Path, src_size: tuple[int, int], picture: dict | None = None,
                     detector: str | None = None, sample_interval_sec: float = 0.5,
                     build=None, hold: bool = True,
                     utterances: list[tuple[float, float, str]] | None = None,
                     fps: float | None = None, scene_cuts: list[float] | None = None,
                     log=print) -> tuple[dict[str, Path], list[dict]]:
    """갭 12(2026-09-08): 클립별 화자 추적 크롭 — v1 `reframe.build_crop_timeline`(입 움직임 0.3 ·
    0.5s 표본 · y 추적)을 v3 클립에 배선한다. 덮개(cover) 클립은 제외(피사체 앵커 맵이 맡는다).
    밴드 비율 크롭을 그림 사각형 안에 가둔다. Stage 2 `subject_pos` 와 교차 검증 — 모델이 left 라
    했는데 검출 평균이 right 면 경고(맵은 검출을 따른다 · 기록). 반환 (crop_map, 감사)."""
    from app.modules.reframe import build_crop_timeline
    _build = build or build_crop_timeline
    geo = band_crop_size(aspect_ratio, src_size, picture)
    if geo is None:
        log("  [v3/render] 화자 추적 — 밴드 비율 크롭 불가(가로 여유 없음) · 생략")
        return {}, []
    crop_w, crop_h, pic_x, pic_y, pic_w, pic_h = geo
    src_w, src_h = src_size
    out_map: dict[str, Path] = {}
    audit: list[dict] = []
    prev_x = prev_y = None
    _spk_mem: dict[str, tuple[float, float]] = {}     # 화자 → (직전 x, 그 run 끝 소스초) — 클립 사이 승계
    for i, c in enumerate(timeline):
        if c.get("cover"):
            continue
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        p = output_dir / f"v3_crop_speaker_{i}.json"
        rec: dict = {"clip": i, "start": s, "end": e}
        _samples: list[dict] = []
        try:
            # 컷 경계에 연속성은 없다 — 이전 클립 끝(prev_x)에서 EMA 0.12 로 기어가면 5초 클립이 끝나도
            # 얼굴에 못 닿는다(「너 바람피니?」 실사고: 경희 얼굴 x1531, 크롭 중심 1028→971). 첫 검출에
            # 즉시 맞추고(snap_first) 이후는 SPEAKER_EMA_ALPHA 로 따라간다. 면적 항은 프레임 최대 얼굴 대비.
            kfs = _build(Path(video_path), p, src_w, src_h, sample_interval_sec,
                         start_sec=s, end_sec=e, enable_speaker_tracking=True,
                         initial_x=None, initial_y=None, detector=detector,
                         crop_size=(crop_w, crop_h), ema_alpha=SPEAKER_EMA_ALPHA,
                         snap_first=True, area_relative=True, collect=_samples)
        except Exception as ex:  # noqa: BLE001 — 안전장치가 연출을 막지 않는다(E17-2 규율)
            rec["result"] = f"실패 — 이 클립은 종전 맵: {type(ex).__name__}: {str(ex)[:80]}"
            audit.append(rec)
            log(f"  [v3/render] ⚠ 화자 추적 clip{i} 실패 — {rec['result']}")
            continue
        rows = [kf.__dict__ if hasattr(kf, "__dict__") else dict(kf) for kf in kfs]
        if hold:
            # 계단식 고정(2026-09-08) — 발화 구간마다 x 하나, 경계에서 한 프레임 점프(v9 AUTO_CX 방식).
            # 원 표본(팬 경로)은 `v3_crop_speaker_raw_{i}.json` 에 남겨 감사·대조에 쓴다.
            (output_dir / f"v3_crop_speaker_raw_{i}.json").write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            _raw_faces = sum(1 for r in rows if float(r.get("face_w") or 0) > 0)
            rows, _runs = hold_keyframes(rows, utterances or [], clip_start=s, clip_end=e,
                                         fps=float(fps or 24000 / 1001), pic_w=float(pic_w),
                                         samples=_samples or None, speaker_memory=_spk_mem,
                                         scene_cuts=scene_cuts)
            rec["hold_runs"] = _runs
            rec["raw_faces"] = _raw_faces
        # 그림 사각형 안으로(레터박스) — x·y 모두
        for r in rows:
            r["crop_w"], r["crop_h"] = crop_w, crop_h
            r["x_center"] = round(min(max(float(r["x_center"]), pic_x + crop_w / 2), pic_x + pic_w - crop_w / 2), 1)
            r["y_center"] = round(min(max(float(r["y_center"]), pic_y + crop_h / 2), pic_y + pic_h - crop_h / 2), 1)
        p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        out_map[f"{c.get('role') or 'build'}_{i}"] = p
        faces = rec.get("raw_faces", sum(1 for r in rows if float(r.get("face_w") or 0) > 0))
        mean_x = sum(float(r["x_center"]) for r in rows) / max(1, len(rows))
        side = "left" if mean_x < pic_x + pic_w * 0.45 else "right" if mean_x > pic_x + pic_w * 0.55 else "center"
        rec.update({"keyframes": len(rows), "faces": faces, "mean_x": round(mean_x, 1), "side": side})
        sp = c.get("subject_pos")
        if sp in ("left", "right") and side != "center" and side != sp:
            rec["subject_pos_conflict"] = sp
            log(f"  [v3/render] ⚠ 화자 추적 clip{i}: Stage 2 subject_pos={sp} 인데 검출 평균은 {side}"
                f"(x {mean_x:.0f}/{src_w}) — 검출을 따른다(기록)")
        if rows:
            prev_x, prev_y = float(rows[-1]["x_center"]), float(rows[-1]["y_center"])
        audit.append(rec)
    log(f"  [v3/render] 화자 추적 크롭 — 클립 {len(out_map)}개 ({detector or 'haar'}"
        f"{' · 계단식 고정' if hold else ' · 연속 팬'}) · 얼굴 표본 {sum(r.get('faces', 0) for r in audit)}")
    if hold:
        for rec in audit:
            for r in rec.get("hold_runs") or []:
                if r.get("speaker") is not None:
                    log(f"  [v3/render]   clip{rec['clip']} {r['start']:.2f}~{r['end']:.2f}s "
                        f"{r['speaker'] or '?'} → x {r['x']:.0f} ({r.get('method', '중앙값')} · 사람 {r.get('people', '?')})")
    return out_map, audit


# ── 편집 연출 3종(2026-09-08, Stage 4 `emphasis`·`zooms`·`fits` — build12 이식) ──────────────
# 렌더러는 클립마다 crop 창을 잘라 밴드 크기로 키우므로 **crop 창을 1/z 로 줄이면 그게 줌**이다
# (렌더러 무변경). 계단식(from_sec)은 그 지점에서 클립을 둘로 쪼갠다 — 프레임 격자에 맞춰 쪼개
# 총 프레임 수가 안 변한다(자막·cue 편집본 좌표 불변). fit 은 렌더러의 클립 단위 분기(fit_picture).

def apply_zoom_splits(timeline: list[dict], zooms: list[dict], fps: float | None
                      ) -> tuple[list[dict], dict[int, dict], dict[int, list[int]]]:
    """plan 타임라인 → (렌더 타임라인(줌 시작점에서 쪼갠 사본), {렌더 idx: zoom}, {plan idx: [렌더 idx]}).
    순수. hold 는 뒷조각이 갖는다. from_sec 는 fps 를 알면 프레임 격자로 반올림(총 프레임 불변)."""
    by_clip = {int(z["clip"]): z for z in zooms or []}
    out: list[dict] = []
    zmap: dict[int, dict] = {}
    p2r: dict[int, list[int]] = {}
    for i, c in enumerate(timeline):
        z = by_clip.get(i)
        if z is None:
            out.append(dict(c))
            p2r[i] = [len(out) - 1]
            continue
        s0, e0 = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        speed = float(c.get("playback_speed") or 1.0)
        output_len = (e0 - s0) / speed
        stages = list(z.get("stages") or [{"from_sec": z.get("from_sec") or 0.0,
                                           "factor": z.get("factor"), "anchor": z.get("anchor")}])
        # 원본 음성을 쓰는 클립을 발화 중간에서 별도 ffmpeg 입력으로 쪼개면 새 입력의
        # AAC 프리롤/seek 경계에서 음절이 통째로 유실될 수 있다. 줌은 장식이므로 음성을
        # 희생하지 않는다: 처음부터 적용하는 줌만 단일 클립으로 허용하고, 중간 줌은 버린다.
        # (영상/음성 타임라인을 분리하기 전까지의 안전 규칙.)
        if c.get("use_original_audio") and any(float(st.get("from_sec") or 0.0) > 0 for st in stages):
            out.append(dict(c))
            p2r[i] = [len(out) - 1]
            continue
        # 단계 경계(클립 시작 기준) → 프레임 격자 반올림, 조각 하한 미만은 앞 단계에 흡수
        bounds: list[tuple[float, dict]] = []
        for st in stages:
            fs = max(0.0, float(st.get("from_sec") or 0.0))
            if fps:
                fs = round(round(fs * fps) / fps, 3)
            if bounds and (fs - bounds[-1][0] < stage4.ZOOM_MIN_PART_SEC or output_len - fs < stage4.ZOOM_MIN_PART_SEC):
                continue
            if not bounds and fs > 0 and (fs < stage4.ZOOM_MIN_PART_SEC or output_len - fs < stage4.ZOOM_MIN_PART_SEC):
                fs = 0.0
            bounds.append((fs, st))
        if bounds and bounds[0][0] > 0:
            bounds.insert(0, (0.0, {"factor": 1.0, "anchor": bounds[0][1].get("anchor")}))
        idxs: list[int] = []
        for j, (fs, st) in enumerate(bounds):
            a = s0 + fs * speed
            zz = s0 + bounds[j + 1][0] * speed if j + 1 < len(bounds) else e0
            part = {**c, "clip_start_sec": round(a, 3), "clip_end_sec": round(zz, 3)}
            if j > 0:
                part["zoom_part"] = True
                part.pop("subtitle", None)
            if j + 1 < len(bounds):
                part.pop("hold_sec", None)
            out.append(part)
            idxs.append(len(out) - 1)
            if float(st.get("factor") or 1.0) != 1.0:
                zmap[len(out) - 1] = {"clip": i, "factor": float(st["factor"]),
                                      "anchor": st.get("anchor") or "center", "from_sec": fs}
        p2r[i] = idxs
    return out, zmap, p2r


def zoom_crop_rows(base_rows: list[dict] | None, factor: float, anchor: str,
                   geo: tuple[int, int, int, int, int, int]) -> list[dict]:
    """줌 크롭 키프레임 — base(피사체/화자 맵)가 있으면 그 중심을 두고 창만 1/factor,
    없으면 밴드 최대 크롭을 anchor(left/center/right) 쪽으로 당겨 잡는다. 그림 안으로 클램프."""
    crop_w, crop_h, pic_x, pic_y, pic_w, pic_h = geo
    zw, zh = int(crop_w / factor) & ~1, int(crop_h / factor) & ~1
    frac = 0.25 if anchor == "left" else 0.75 if anchor == "right" else 0.5
    def clamp(x, y):
        return (round(min(max(x, pic_x + zw / 2), pic_x + pic_w - zw / 2), 1),
                round(min(max(y, pic_y + zh / 2), pic_y + pic_h - zh / 2), 1))
    if base_rows:
        rows = []
        for r in base_rows:
            x, y = clamp(float(r["x_center"]), float(r["y_center"]))
            rows.append({**r, "x_center": x, "y_center": y, "crop_w": zw, "crop_h": zh})
        return rows
    x, y = clamp(pic_x + frac * pic_w, pic_y + pic_h / 2)
    return [{"time_sec": 0.0, "x_center": x, "y_center": y, "crop_w": zw, "crop_h": zh}]


EMPH_RESOLVE_TOL_SEC = 1.0   # 강조 줄 번호가 어긋났을 때 시각으로 다시 찾는 허용 오차


def resolve_emphasis_index(e: dict, segments: list[dict] | None) -> int | None:
    """강조 항목 → 지금 자막 세그먼트 번호. 순수.

    Stage 4 는 강조를 **자막 줄 번호**(`index`)로 적는데, 자막 오버라이드로 줄을 나누거나 합치면 번호가 밀려
    엉뚱한 줄이 빨개진다(2026-09-11 지금불륜 2화 3편·4편 실사고 두 번). 항목에는 `text`·`start_sec` 가 함께
    실려 있으므로: ① 그 번호의 줄이 같은 글자면 그대로 ② 아니면 같은 글자의 줄 중 시각이 가장 가까운 줄
    ③ 그것도 없으면(사람이 글자를 고침) 시작 시각이 EMPH_RESOLVE_TOL_SEC 안인 가장 가까운 줄
    ④ 그래도 없으면 종전대로 번호. segments 가 없으면 번호 그대로(종전)."""
    try:
        idx = int(e["index"])
    except (KeyError, TypeError, ValueError):
        return None
    if not segments:
        return idx
    text = str(e.get("text") or "").strip()
    if not text:
        return idx
    if 0 <= idx < len(segments) and str(segments[idx].get("text") or "").strip() == text:
        return idx
    try:
        t0 = float(e.get("start_sec")) if e.get("start_sec") is not None else None
    except (TypeError, ValueError):
        t0 = None
    same = [i for i, sg in enumerate(segments) if str(sg.get("text") or "").strip() == text]
    if same:
        if t0 is None or len(same) == 1:
            return same[0]
        return min(same, key=lambda i: abs(float(segments[i]["start_sec"]) - t0))
    if t0 is not None:
        near = min(range(len(segments)), key=lambda i: abs(float(segments[i]["start_sec"]) - t0))
        if abs(float(segments[near]["start_sec"]) - t0) <= EMPH_RESOLVE_TOL_SEC:
            return near
    return idx


def emphasis_styles(v3_style: dict | None, base_size: int,
                    segments: list[dict] | None = None, *,
                    font_path: str | None = None, em_ratio: float = 1.0,
                    notes: list[str] | None = None) -> dict[int, dict]:
    """강조 줄 → {자막 세그먼트 idx: {size, color, fx}}. 강한 팝인(pop_strong)은 build12 EPOP 의 대응.
    segments 를 주면 줄 번호를 글자·시각으로 다시 맞춘다(resolve_emphasis_index).
    font_path 를 주면(2026-09-18 폰 안전 박스) 키운 줄이 가로 안전 폭(880px)을 넘지 않게 **배율만** 낮춘다 —
    기준 자막 크기는 그대로. em_ratio = libass 줄높이 비(ASS 크기 ÷ 실제 em)."""
    out: dict[int, dict] = {}
    for e in (v3_style or {}).get("emphasis") or []:
        idx = resolve_emphasis_index(e, segments)
        if idx is None:
            continue
        scale = float(e.get("scale") or stage4.EMPH_DEFAULT_SCALE)
        if font_path and segments is not None and 0 <= idx < len(segments):
            text = str(segments[idx].get("text") or "")
            em = base_size / max(0.1, float(em_ratio))
            w1 = _sz.line_width_px(text, font_path, int(round(em))) + 2 * SUB_OUTLINE_PX
            if w1 > 0 and w1 * scale > _sz.SAFE_WIDTH:
                new = max(1.0, _sz.SAFE_WIDTH / w1)
                if new < scale:
                    if notes is not None:
                        notes.append(f"강조 「{text}」 배율 {scale:.2f} → {new:.2f}(안전 폭 {_sz.SAFE_WIDTH}px)")
                    scale = new
        out[idx] = {"size": int(round(base_size * scale)),
                    "color": str(e.get("color") or stage4.EMPH_DEFAULT_COLOR), "fx": EMPHASIS_FX}
    return out


def enforce_safe_box(design) -> tuple[Any, list[str]]:
    """가운데 정렬 오버레이를 폰 안전 폭(가로 SAFE_X0~SAFE_X1) 안으로 맞춘다 — 순수(폰트 실측만).

    대상: 작품 로고 이미지 폭 · 로고 아래 캡션 · 작품명 위 플랫폼 줄(아이콘+글자) · 작품명 텍스트.
    넘치면 그 요소의 크기만 줄인다(템플릿 고정 문구라 글자 수를 바꿀 수 없다 — 제목과 다르다).
    밴드 모서리 플랫폼 표기는 render_final 이 오프셋을 끌어올린다(오른쪽 끝 = 마지노선 SAFE_X1).
    반환 (design, 메모). 안이면 design 그대로(회귀 0)."""
    import dataclasses as _dc
    from app.modules.renderer import _measure_title_text_width, platform_line_geometry
    notes: list[str] = []
    font = str(getattr(design, "title_font", "") or "")
    limit = _sz.SAFE_WIDTH

    def _w(text: str, size: int) -> int:
        return int(_measure_title_text_width(str(text), font, int(size)))

    if getattr(design, "work_type", "text") == "image" and int(getattr(design, "work_image_width", 0) or 0) > limit:
        notes.append(f"작품 로고 폭 {design.work_image_width} → {limit}px")
        design = _dc.replace(design, work_image_width=limit)
    cap = str(getattr(design, "work_caption", "") or "")
    if cap:
        fs = int(getattr(design, "work_caption_font_size", 40) or 40)
        w = _w(cap, fs)
        if w > limit:
            nfs = max(20, int(fs * limit / float(w)))
            notes.append(f"로고 아래 문구 「{cap}」 폭 {w}px > {limit} — 글자 {fs} → {nfs}px")
            design = _dc.replace(design, work_caption_font_size=nfs)
    if getattr(design, "platform_placement", "band") == "above_work" and \
            (getattr(design, "platform_text", None) or getattr(design, "platform_image", None)):
        g = platform_line_geometry(design)
        txt = str(getattr(design, "platform_text", "") or "")
        gap = g["font_size"] // 2 if (txt and getattr(design, "platform_image", None)) else 0
        total = g["icon_w"] + gap + (_w(txt, g["font_size"]) if txt else 0)
        if total > limit:
            nfs = max(20, int(g["font_size"] * limit / float(total)))
            notes.append(f"작품명 위 플랫폼 줄 폭 {total}px > {limit} — 글자 {g['font_size']} → {nfs}px")
            design = _dc.replace(design, platform_font_size=nfs)
    if getattr(design, "work_type", "text") != "image":
        wt = str(getattr(design, "work_value", "") or "")
        fs = int(getattr(design, "work_font_size", 40) or 40)
        if wt and "\n" not in wt and _w(wt, fs) > limit:
            nfs = max(20, int(fs * limit / float(_w(wt, fs))))
            notes.append(f"작품명 「{wt}」 폭 > {limit} — 글자 {fs} → {nfs}px")
            design = _dc.replace(design, work_font_size=nfs)
    return design, notes


def render_final(*, video_path: Path, plan: dict, style_doc: dict,
                 segments: list[dict], resources: dict, story_doc: dict,
                 output_dir: Path, out_name: str = "final_1080x1920.mp4",
                 channel_design: dict | None = None,
                 muted_gain_db: float | None = None,
                 style_preset: str | None = None,
                 log=print) -> tuple[Path, dict]:
    """edit_plan + style + 자막/cue → 1080×1920 최종본. 반환: (경로, 실측).

    channel_design: 채널이 CLI(--design-*)로 준 키(어댑터 어휘). Stage 4 가 정한
    디자인보다 **위** — 채널 정체성·권리사 표기는 AI 연출이 아니라 계약이다.
    muted_gain_db: 내레이션 덮개 구간의 원본 볼륨(dB). None = 종전 완전 무음.
    style_preset: 채널 스타일 프리셋 이름(미지정 = recap) — 자막·내레이션 기준 위치의
    **참조 밴드**(프리셋 자신의 밴드)를 재는 데 쓴다(preset_relative_margin)."""
    config = AppConfig()
    if channel_design:
        log(f"  [v3/render] 채널 design 적용 {sorted(channel_design)}")
    design = design_from_style(
        channel_design_over_ai(style_doc.get("design") or {}, channel_design))
    if design.platform_text or design.platform_image:
        log(f"  [v3/render] 플랫폼 표기 "
            f"{design.platform_image or design.platform_text!r} ({design.platform_align})")
    # 폰트 이원화(M4 스모크 프레임 실측 2건의 교훈):
    #   drawtext(제목·작품명) = **파일 경로** — 이름만 주면 fontconfig 폴백으로 한글
    #     글리프가 없어 두부(□)가 된다.
    #   ASS Style(자막·TTS) = **패밀리명** — 경로를 Fontname 에 넣으면 fontsdir 매칭이
    #     실패해 시스템 폴백에 의존한다(맥은 우연히 되지만 프로드 노드에선 두부).
    import dataclasses as _dc

    import app.config as _cfgmod
    from app.config import get_font_path, to_font_family
    _root = Path(_cfgmod.__file__).resolve().parent   # v1 pipeline 과 같은 app_root
    ass_family = to_font_family(design.subtitle_font)
    _text_font_name = design.subtitle_font      # 라벨(texts) 용 — 아래에서 경로로 바뀌기 전 이름
    design = _dc.replace(
        design,
        title_font=get_font_path(design.title_font, _root),
        subtitle_font=get_font_path(design.subtitle_font, _root))

    # 편집 연출 3종(2026-09-08): Stage 4 가 고른 줌 컷은 시작점에서 쪼갠 **렌더 타임라인**을 쓴다.
    # 자막·cue·라벨은 편집본 좌표라 그대로(총 길이 불변). 줌·fit 이 없으면 plan 타임라인과 같다.
    _v3s = (style_doc or {}).get("v3_style") or {}
    render_tl, _zoom_by_idx, _p2r = apply_zoom_splits(plan["timeline"], _v3s.get("zooms") or [],
                                                      plan.get("source_fps"))
    _fit_render_idx: set[int] = set()
    _fill_only = bool((plan.get("visual_policy") or {}).get("fill_only"))
    if _fill_only and _v3s.get("fits"):
        log("  [v3/시각편집] 자료 화면 포함 가로 전체 축소 금지 — 저장된 핵심 영역 크롭 사용")
    for _p in ([] if _fill_only else (_v3s.get("fits") or [])):
        for _ri in _p2r.get(int(_p), []):
            _fit_render_idx.add(_ri)
    if _zoom_by_idx or _fit_render_idx:
        log(f"  [v3/render] 편집 연출 — 줌 {len(_zoom_by_idx)}컷(렌더 클립 {len(render_tl)}개, "
            f"분할 {len(render_tl) - len(plan['timeline'])}) · fit {len(_fit_render_idx)}컷 · "
            f"강조 자막 {len(_v3s.get('emphasis') or [])}줄")
    clips = [StoryClip(role=str(c.get("role") or "build"),
                       start_sec=float(c["clip_start_sec"]),
                       end_sec=float(c["clip_end_sec"]),
                       subtitle=str(c.get("subtitle") or ""),
                       use_original_audio=bool(c.get("use_original_audio", True)),
                       hold_sec=float(c.get("hold_sec") or 0.0),
                       playback_speed=float(c.get("playback_speed") or 1.0),
                       fit_picture=None)
             for c in render_tl]

    # 하단 밴드 — 작품 로고 이미지가 있으면 텍스트 대신 그것을 쓴다(렌더러는 이미
    # contain 배치를 하고 있었고, v3 만 work_type 을 안 넘겨 매 편 텍스트로 나갔다)
    _work_title = (plan.get("layout") or {}).get("bottom_label") or ""
    if design.work_type != "image":
        _logo = resolve_work_logo(_work_title)
        if _logo is not None:
            log(f"  [v3/render] 작품 로고 {_logo.name}")
            design = _dc.replace(design, work_type="image", work_value=str(_logo),
                                 work_image_width=LOGO_WIDTH,
                                 work_image_height=LOGO_BOX_HEIGHT,
                                 work_image_align="center")
    _title_text = (plan.get("layout") or {}).get("top_title") or ""
    # 썸네일 안전 구역(2026-09-11) — 제목 **상한** 크기로 재서 채널 안에서 밴드·로고 자리가 편마다
    # 같게. 안에 들면 design 그대로(회귀 0). 밴드를 옮기면 라벨(캔버스 좌표)도 같이 옮긴다(아래).
    design, _thumb = fit_thumbnail_safe_zone(
        design, channel_design=channel_design, style_preset=style_preset,
        line_count=max(2, len([ln for ln in _title_text.split("\n") if ln.strip()])),
        canvas_width=config.canvas_width, canvas_height=config.canvas_height)
    if _thumb["actions"] or _thumb["unmet"]:
        _b, _a = _thumb["before"], _thumb["after"]
        log(f"  [v3/썸네일] 안전 구역 {THUMB_SAFE_TOP}~{THUMB_SAFE_BOTTOM} — 제목 윗변 "
            f"{_b['title_top']} → {_a['title_top']} · 하단 아랫변 {_b['bottom']} → {_a['bottom']}")
        for _n in _thumb["actions"]:
            log(f"  [v3/썸네일] {_n}")
        if _thumb["unmet"]:
            log(f"  [v3/썸네일] ⚠ 다 못 넣음 {_thumb['unmet']} — 템플릿(화면비·로고·제목 크기) 조정 필요")
    _label_dy = _thumb["band_shift"] / float(config.canvas_height)
    # 제목 줄별 크기를 이 편의 실제 글자수로 맞춘다
    # 안전 폭(2026-09-18 사용자 결정 "글자 크기를 줄일 게 아니라 글자 수를 줄여") — 제목은 스토리 걸음 2 가
    # 폭으로 반려해 상한 크기 그대로 880px 에 들어오게 만든다. 여기서 넘치면 손으로 넣은 제목(오버라이드)이다 —
    # 폰에서 잘리지 않게 크기를 줄여 넣되(최후 수단) ⚠ 로 크게 알린다: 고칠 것은 글자 수다.
    _title_over = _sz.title_overflow([ln for ln in _title_text.split("\n") if ln.strip()],
                                     str(design.title_font), list(design.title_sizes))
    for _o in _title_over:
        log(f"  [v3/render] ⚠ 제목 {_o['line']}줄 「{_o['text']}」 폭 {_o['width']}px > 안전 폭 {_sz.SAFE_WIDTH}px "
            f"({_o['size']}px 기준 약 {_o['cut_chars']}자 초과) — 글자 수를 줄여야 한다(지금은 크기를 줄여 넣음)")
    # 밴드 모서리 플랫폼 표기(「티빙」 등)도 안전 박스 안으로(2026-09-18 실사고 — 밴드 모서리 24px 이라 폰에서
    # 「빙」이 잘렸다). 오프셋은 앵커 쪽 **밴드** 모서리 기준이므로, 캔버스 끝에서 SAFE_X0 이상이 되게 올린다.
    if (design.platform_text or design.platform_image) and design.platform_placement != "above_work":
        from app.modules.subtitle_region import band_geometry as _bg
        _pg = _bg(design, canvas_width=config.canvas_width, canvas_height=config.canvas_height)
        _need_off = max(0, _sz.SAFE_X0 - int(_pg.pad_x))
        if int(design.platform_x) < _need_off:
            log(f"  [v3/안전구역] 플랫폼 표기 오프셋 {design.platform_x} → {_need_off}px(캔버스 끝에서 {_sz.SAFE_X0}px 안쪽)")
            design = _dc.replace(design, platform_x=_need_off)
    # 가로 마지노선(2026-09-18 사용자 지시 "오른쪽 마지노선을 정해두고 다른 로고·글씨들도") — 가운데 정렬
    # 요소(작품 로고·로고 아래 문구·작품명 위 플랫폼 줄·작품명 글자)도 가로 SAFE_X0~SAFE_X1 안으로.
    design, _box_notes = enforce_safe_box(design)
    for _n in _box_notes:
        log(f"  [v3/안전구역] {_n}")
    _fitted = fit_title_sizes(_title_text, list(design.title_sizes),
                              font_path=str(design.title_font))   # 경로화된 제목 폰트로 실측
    if _fitted != list(design.title_sizes):
        log(f"  [v3/render] 제목 크기 폭 맞춤 {design.title_sizes} → {_fitted}")
        design = _dc.replace(design, title_sizes=_fitted, title_size=_fitted[0])

    # 원본에 박힌 자막 회피(E17-2/E18-2) — 우리 자막을 **위로만** 민다
    from app.modules import subtitle_region as _sr
    _band, _profiles = detect_burned_subtitles(Path(video_path), clips, design,
                                               output_dir, log=log)
    _geom = _sr.band_geometry(design, canvas_width=config.canvas_width,
                              canvas_height=config.canvas_height)
    _title_bottom = _sr.estimate_title_bottom(
        design, _geom, line_count=_sr.estimate_title_line_count(_title_text))
    _cd = channel_design or {}
    # 자막·내레이션 기준 위치 — 채널이 절대 margin 을 **명시**하지 않았으면 프리셋 값을 밴드
    # 상대로 옮겨 쓴다(밴드 안쪽 · v1 과 같은 자리). 명시했으면 그 값 그대로 + 아래 밴드 클램프.
    # 실사고(지금불륜 EP01 bb71cb7d): 클램프가 프리셋 값(밴드 안쪽 의도)에 걸려 자막을
    # 밴드 밖 캔버스 바닥(518→281)까지 내렸고 작품명 밑으로 들어갔다.
    _ref_geom = _sr.band_geometry(design_from_style(stage4.get_style_preset(style_preset)),
                                  canvas_width=config.canvas_width,
                                  canvas_height=config.canvas_height)
    _sub_h = _sr.estimate_subtitle_height(design.subtitle_size)
    _floor_top = _title_bottom + SUB_GAP_PX
    _work_top = estimate_work_top(design, band_bottom=_geom.bottom,
                                  canvas_height=config.canvas_height)
    # 기준 margin(프리셋 밴드 상대 · 밴드 앵커 · 내레이션=대사 줄 · 채널 절대값 클램프) — 썸네일
    # 판정(layout_extents)과 같은 함수. 번인 회피(아래 블록)는 이 값 위에서 **올리기만** 한다.
    _sub_margin, _tts_margin, _tts_base, _margin_notes = base_text_margins(
        design, channel_design=channel_design, geom=_geom, ref_geom=_ref_geom,
        work_top=_work_top, canvas_height=config.canvas_height)
    for _n in _margin_notes:
        log(f"  {_n}")
    _tts_off = _cd.get("tts_band_offset")

    def _runs(t0: float, t1: float) -> list[tuple[int, int]]:
        return _sr.runs_in_window(_profiles, t0, t1, _geom) if _profiles else []

    if _band:   # 편 내내 같은 자리에 있는 번인 자막 — 전역으로 한 번 올린다
        for _name, _size, _cur in (("자막", design.subtitle_size, _sub_margin),
                                   ("내레이션", design.tts_line_font_size, _tts_margin)):
            _new, _notes = _sr.avoid_margin_v(
                _cur, canvas_height=config.canvas_height,
                burned_top=int(_band["top"]), burned_bottom=int(_band["bottom"]),
                subtitle_height=_sr.estimate_subtitle_height(_size),
                title_bottom=_title_bottom, band_top=_geom.top)
            for n in _notes:
                log(f"  [v3/자막회피] {_name} — {n}")
            if _name == "자막":
                _sub_margin = int(_new)
            else:
                _tts_margin = int(_new)

    # 내레이션 줄은 스타일이 하나뿐이라 큐 창들의 **합집합**으로 한 번만 정한다
    _cue_wins = [(float(f["cue"]["start_sec"]), float(f["cue"]["end_sec"]))
                 for f in (resources.get("tts_cue_files") or [])
                 if f.get("cue", {}).get("start_sec") is not None
                 and f.get("cue", {}).get("end_sec") is not None]
    _tts_runs = sorted({r for w in _cue_wins for r in _runs(*w)})
    if _tts_runs:
        _tts_margin, _note = place_above_burned(
            _tts_margin, _tts_runs, canvas_height=config.canvas_height,
            subtitle_height=_sr.estimate_subtitle_height(design.tts_line_font_size),
            floor_top=_floor_top)
        if _note:
            log(f"  [v3/자막회피] 내레이션 — {_note}")

    # 자막 스택(2026-09-04) — 자막·내레이션 블록 아래에 작품명/로고. 두 트랙의 최종
    # margin(밴드 아래 내리기·번인 회피 올리기가 끝난 값)이 정해진 이 지점에서 한 번.
    # 줄별 보정(_line_margins · tts_cue_margins lift)은 전부 **올리기만** 하므로 여기
    # 값의 아랫변이 두 트랙의 최저점이다.
    _sub_margin, _tts_margin, _work_min_top, _stack_notes = stack_work_below_text(
        sub_margin=_sub_margin, tts_margin=_tts_margin, work_top=_work_top,
        work_height=estimate_work_height(design), canvas_height=config.canvas_height)
    for _n in _stack_notes:
        log(f"  [v3/자막배치] 작품명 — {_n}")
    _work_top_final = estimate_work_top(design, band_bottom=_geom.bottom,
                                        canvas_height=config.canvas_height,
                                        min_top=_work_min_top)

    # 대사 ASS — C6 세그먼트(편집본 좌표)를 그대로 이벤트로
    # 채널 subtitles:false(2026-09-08, 커리어데이 11회): 소스에 한국어 자막이 번인된 채널은
    # 어절 자막이 같은 말을 한 번 더 그린다(번인 띠 위 618px 에 "부가 축적이 되면" 두 줄).
    # v1 --no-subtitles 와 같은 뜻 — 대사 트랙만 끄고 내레이션 자막·라벨은 그대로.
    draw_subs = (channel_design or {}).get("subtitles") is not False
    sub_path: Path | None = output_dir / "v3_subtitles.ass"
    sub_style = SubtitleStyle(
        font_name=ass_family, font_size=design.subtitle_size,
        primary_color=_style_color(design.subtitle_color or "#FFFFFF"),
        outline=SUB_OUTLINE_PX, margin_v=_sub_margin)
    # 화자별 색은 **줄 단위 style** 통로로 간다(v1 이 쓰는 그 통로 — subtitle.py 의
    # _line_style_overrides). SpeechSegment 는 frozen 3필드라 style 을 못 달아
    # SimpleNamespace 로 짓는다. color 가 없으면 종전과 바이트 동일.
    fx_windows = subtitle_fx_windows(story_doc, style_doc, plan["timeline"])

    # 몇 초씩만 뜨는 방송 텔롭은 전역 띠 판정에 안 걸린다 — 줄이 떠 있는 **그 창의**
    # 표본으로 다시 재서 필요한 줄만 더 올린다(E18-2). 예능은 텔롭이 여러 줄로 쌓이므로
    # 가장 아래 띠만 피하면 그 위 띠에 얹힌다(실측) → 띠를 전부 보고 빈 자리를 고른다.
    _line_margins: list[int | None] = [None] * len(segments or [])
    _moved = 0
    for _i, _s in enumerate(segments or []):
        _rs = _runs(float(_s["start_sec"]), float(_s["end_sec"]))
        if not _rs:
            continue
        _m, _note = place_above_burned(_sub_margin, _rs,
                                       canvas_height=config.canvas_height,
                                       subtitle_height=_sub_h, floor_top=_floor_top)
        if _m > _sub_margin:            # 단조 개선 — 내리지 않는다
            _line_margins[_i] = _m
            _moved += 1
    if _moved:
        log(f"  [v3/자막회피] 자막 {_moved}/{len(segments)}줄을 구간별로 더 올렸습니다")

    _emph_notes: list[str] = []
    _emph = emphasis_styles(_v3s, design.subtitle_size, segments,       # 강조 자막(2026-09-08) — 없으면 빈 dict · 줄 번호는 글자·시각으로 재확인(09-11)
                            font_path=str(design.subtitle_font),
                            em_ratio=libass_line_height_ratio(_text_font_name), notes=_emph_notes)
    for _n in _emph_notes:
        log(f"  [v3/안전구역] {_n}")

    def _seg_style(seg: dict, idx: int = 0) -> dict | None:
        st: dict[str, Any] = {}
        if seg.get("color"):
            st["color"] = str(seg["color"])
        if idx in _emph:
            st.update(_emph[idx])          # 색·크기·강한 팝인 — 화자색·비트 pop 을 덮는다
        m = _line_margins[idx] if idx < len(_line_margins) else None
        if m is not None:
            st["y"] = round((config.canvas_height - int(m)) / float(config.canvas_height), 5)
        t0 = float(seg["start_sec"])
        if "fx" not in st:
            for w0, w1, fx in fx_windows:
                if w0 <= t0 < w1:
                    st["fx"] = fx
                    break
        return st or None

    if draw_subs:
        build_ass_from_segments(
            [SimpleNamespace(start_sec=float(s["start_sec"]), end_sec=float(s["end_sec"]),
                             text=str(s["text"]), style=_seg_style(s, i))
             for i, s in enumerate(segments)],
            sub_path, sub_style)
    else:
        sub_path = None
        _emph = {}                     # 강조 자막·강조 효과음은 대사 트랙의 것 — 함께 끈다
        log(f"  [v3/render] 채널 subtitles=false — 대사 자막 {len(segments)}줄 안 그림"
            "(내레이션 자막·라벨 유지)")

    # TTS 자막 ASS — cue 텍스트(합성 fit 반영본)
    cue_files = [f for f in (resources.get("tts_cue_files") or [])
                 if f.get("cue", {}).get("start_sec") is not None
                 and Path(f.get("path", "")).exists()]
    cue_files = with_tts_audible_bounds(cue_files)
    tts_path = None
    if cue_files:
        tts_path = output_dir / "v3_tts.ass"
        tts_style = SubtitleStyle(
            font_name=ass_family, font_size=design.tts_line_font_size,
            primary_color=design.tts_line_color, outline=SUB_OUTLINE_PX,
            margin_v=_tts_margin)
        from app.modules.subtitle import _width_max_chars
        caption_files = ([{"cue": c} for c in resources["tts_caption_segments"]]
                         if "tts_caption_segments" in resources else cue_files)
        _caps = [balance_narration_lines(narration_caption(str(f["cue"]["text"])),
                                         max_chars=_width_max_chars(getattr(design, "tts_width", None)))
                 for f in caption_files]
        if _tts_off is not None:
            # 줄별 y — 한 줄 cue 도 두 줄 cue 도 윗변이 밴드 아래 같은 자리. 번인 회피가
            # 전역값을 올렸으면 그만큼 같이 올린다(lift). style.y 통로는 E18-2 와 동일.
            _pm = tts_cue_margins(_caps, band_bottom=_geom.bottom, offset=int(_tts_off),
                                  font_size=design.tts_line_font_size,
                                  width=getattr(design, "tts_width", None),
                                  canvas_height=config.canvas_height,
                                  lift=max(0, _tts_margin - _tts_base))
            _tts_segs = [SimpleNamespace(start_sec=float(f["cue"]["start_sec"]),
                                         end_sec=float(f["cue"]["end_sec"]), text=c,
                                         style={"y": 1.0 - m / config.canvas_height,
                                                "fx": NARRATION_FX})
                         for f, c, m in zip(caption_files, _caps, _pm)]
            log(f"  [v3/자막배치] 내레이션 줄별 margin_v {sorted(set(_pm))} (cue {len(_pm)}개)")
        else:
            _tts_segs = [SimpleNamespace(start_sec=float(f["cue"]["start_sec"]),
                                         end_sec=float(f["cue"]["end_sec"]), text=c,
                                         style={"fx": NARRATION_FX})
                         for f, c in zip(caption_files, _caps)]
        build_tts_ass(_tts_segs, tts_path, tts_style)

    # 괄호 라벨 — 편집실 자유 텍스트 레이어 재사용(비트 창 전체에 표시)
    # 라벨 — 계획은 공용(plan_labels), 위치는 Stage 4 가 화면을 보고 정한 값을 쓴다
    # (M12: 가운데 고정이면 인물 얼굴·방송 자막을 덮는다는 사용자 지적).
    style_labels = (style_doc.get("v3_style") or {}).get("labels") or []
    # M16(2026-09-01): text 를 가진 라벨은 Stage 4 가 **직접 쓴** 것 — 문구·시각·
    # 위치가 한 몸이라 그대로 쓴다. index 만 가진 라벨은 구 체크포인트(M12 —
    # Stage 3 문구 + Stage 4 위치)라 종전 병합 경로로 — 재렌더 회귀 0.
    authored = [x for x in style_labels
                if isinstance(x, dict) and isinstance(x.get("text"), str)]
    placed = {int(x["index"]): x for x in style_labels
              if isinstance(x, dict) and x.get("index") is not None
              and x.get("text") is None}
    labels = []
    # 라벨 크기(2026-09-08): 채널 design label_size(v3 전용) > stage4.LABEL_SIZE. 얼굴 회피·x 폭 자도 이 값을 본다.
    _label_size = int((channel_design or {}).get("label_size") or stage4.LABEL_SIZE)
    for lb in authored:
        labels.append({"text": lb["text"], "start_sec": float(lb["start_sec"]),
                       "end_sec": float(lb["end_sec"]),
                       "x": float(lb.get("x", 0.5)),
                       "y": float(lb.get("y", LABEL_Y_RATIO)) + _label_dy,   # 썸네일 맞춤이 옮긴 밴드만큼
                       "rotate": float(lb.get("rotate", 0.0)),
                       "size": _label_size, "stroke": "dark_thick",
                       "fx": str(lb.get("fx") or "pop"), "font": _text_font_name,
                       "color": str(lb.get("color") or _cycle_color(len(labels)))})
    if not authored:
        for lb in plan_labels(story_doc, plan):
            pos = placed.get(lb["index"]) or {}
            labels.append({"text": lb["text"], "start_sec": lb["start_sec"],
                           "end_sec": lb["end_sec"],
                           "x": float(pos.get("x", 0.5)),
                           "y": float(pos.get("y", LABEL_Y_RATIO)) + _label_dy,
                           "rotate": float(pos.get("rotate", 0.0)),
                           "size": _label_size, "stroke": "dark_thick",
                           "fx": str(pos.get("fx") or "pop"), "font": _text_font_name,
                           "color": str(pos.get("color") or _cycle_color(lb["index"]))})
    texts_path = None
    _label_face_records: list[dict] = []
    if labels:
        # 라벨 얼굴 회피(2026-09-08 사용자 지적) — 렌더 직전·결정적. 초안 프레임의 얼굴을 검출해
        # 겹치는 라벨을 비켜 놓는다(app/v3/label_faces). 초안이 없으면 그대로.
        from app.v3 import label_faces as _lf
        # 자막·내레이션 띠도 장애물(2026-09-10 「(눈물의 다짐)」 — '아래' 후보가 강조 자막 위에 얹혔다)
        _obs = _lf.subtitle_obstacles(canvas_w=config.canvas_width, canvas_h=config.canvas_height,
                                      sub_margin_v=_sub_margin, sub_size=design.subtitle_size,
                                      tts_margin_v=_tts_margin, tts_size=design.tts_line_font_size,
                                      emph_scale=stage4.EMPH_SCALE_RANGE[1])
        labels, _label_face_records = _lf.avoid_faces_for_labels(
            labels, output_dir / "draft_480.mp4", _geom,
            canvas_w=config.canvas_width, canvas_h=config.canvas_height, obstacles=_obs, log=log)
        texts_path = output_dir / "v3_labels.ass"
        build_texts_ass(labels, texts_path)

    # 적대 리뷰 확정(critical): renderer 는 use_original_audio 를 읽지 않았다 —
    # 뮤트 창(편집본 좌표)을 additive 필드로 넘겨 원본 트랙에만 volume=0 (cue 는 산다).
    # M15: 창은 **클립 전체가 아니라 내레이션이 실제로 점유한 구간**이다. 클립 전체를
    # 끄면 내레이션이 끝난 뒤가 완전 무음이 된다(실측 도입부 3.57초).
    src_windows = assemble.narration_windows(story_doc)
    all_windows = sorted(w for wins in src_windows.values() for w in wins)
    muted_windows = cover_mute_windows(render_tl, all_windows,
                                       plan.get("source_fps"))
    muted_windows = fit_mute_windows_to_tts(muted_windows, cue_files)

    out_path = output_dir / out_name
    audio_mix = plan.get("audio_mix") or {}
    # 얼굴 크롭은 여전히 범위 외(발주서) — 이 맵은 무성 인서트의 **피사체 앵커**만
    # 싣는다(subject_crop_map 독스트링 참조). 앵커 없는 판은 빈 dict = 종전 그대로.
    # 채널 face_tracking:false(--no-reframe) 는 v3 에서 피사체 앵커 크롭까지 끈다 —
    # v1 과 같은 뜻('원본을 가운데 정렬로 넣는다'). 기본 True = 종전 그대로.
    # 레터박스(2026-09-07): probe 단계가 잰 그림 영역 — 있으면 전 클립을 그 안에서
    # 밴드 비율로 자른다(subject_crop_map 레터박스 경로). 없으면 None = 종전 그대로.
    picture = read_picture_area(output_dir)
    if getattr(design, "enable_reframe", True):
        crop_map = subject_crop_map(render_tl, video_path=Path(video_path),
                                    aspect_ratio=design.aspect_ratio,
                                    output_dir=output_dir, picture=picture, log=log)
    elif picture:
        # 레터박스 제거는 앵커와 무관한 소스 성질 — face_tracking=false 라도 적용(x 중앙)
        log("  [v3/render] 채널 face_tracking=false — 피사체 앵커 끔, 레터박스 크롭만 적용(x 중앙)")
        crop_map = subject_crop_map(render_tl, video_path=Path(video_path),
                                    aspect_ratio=design.aspect_ratio,
                                    output_dir=output_dir, picture=picture,
                                    anchors=False, log=log)
    else:
        crop_map = {}
        log("  [v3/render] 채널 face_tracking=false — 피사체 앵커 크롭 끔(중앙)")
    # 갭 12(2026-09-08): 화자 추적 — **기본 켜짐 · 기본 YuNet**(사용자 결정, 「너 바람피니?」 경희 얼굴 실사고 뒤).
    # 끄려면 design 키 speaker_tracking=off. face_tracking:false(enable_reframe) 는 여전히 통째로 끈다.
    # v1 경로는 이 블록을 안 지나므로 무관(v1 검출기 기본은 그대로 haar).
    speaker_audit: list[dict] = []
    if str(_cd.get("speaker_tracking") or SPEAKER_TRACKING_DEFAULT).lower() in ("on", "pan") and getattr(design, "enable_reframe", True):
        try:
            _src = None
            _pr = output_dir / "checkpoint_probe.json"
            if _pr.exists():
                _pj = json.loads(_pr.read_text(encoding="utf-8"))
                if _pj.get("width") and _pj.get("height"):
                    _src = (int(_pj["width"]), int(_pj["height"]))
            if _src is None:
                _out = subprocess.run(
                    [find_ffmpeg_command("ffprobe"), "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video_path)],
                    capture_output=True, text=True, check=True).stdout.strip()
                _w, _h = _out.split("\n")[0].split(",")[:2]
                _src = (int(_w), int(_h))
            _mode = str(_cd.get("speaker_tracking") or SPEAKER_TRACKING_DEFAULT).lower()
            _smap, speaker_audit = speaker_crop_map(
                render_tl, video_path=Path(video_path), aspect_ratio=design.aspect_ratio,
                output_dir=output_dir, src_size=_src, picture=picture,
                detector=_cd.get("face_detector") or FACE_DETECTOR_DEFAULT,
                hold=(_mode != "pan"),
                utterances=utterances_from_segments(segments, plan["timeline"], plan.get("source_fps")),
                fps=plan.get("source_fps"), scene_cuts=read_scene_cuts(output_dir), log=log)
            crop_map = {**crop_map, **_smap}
        except Exception as e:  # noqa: BLE001
            log(f"  [v3/render] ⚠ 화자 추적 실패 — 종전 맵으로 진행: {e}")
    # 사람 크롭 고정(2026-09-11): 클립 reframe.mode == "fixed" 는 추적·앵커 맵을 덮는다(마지막에 얹어 이긴다).
    _fixed_map, _fixed_audit = fixed_crop_map(render_tl, output_dir=output_dir, aspect_ratio=design.aspect_ratio,
                                              picture=picture, video_path=Path(video_path), log=log)
    crop_map = {**crop_map, **_fixed_map}
    # ── 줌·fit 적용(2026-09-08) — 위 맵들이 확정된 뒤 그 위에 얹는다 ─────────────────
    edit_fx_audit: dict[str, Any] = {}
    if _zoom_by_idx or _fit_render_idx:
        _src = None
        _pr = output_dir / "checkpoint_probe.json"
        if _pr.exists():
            try:
                _pj = json.loads(_pr.read_text(encoding="utf-8"))
                if _pj.get("width") and _pj.get("height"):
                    _src = (int(_pj["width"]), int(_pj["height"]))
            except (OSError, ValueError):
                _src = None
        if _src is None:
            try:
                _out = subprocess.run(
                    [find_ffmpeg_command("ffprobe"), "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video_path)],
                    capture_output=True, text=True, check=True).stdout.strip()
                _w, _h = _out.split("\n")[0].split(",")[:2]
                _src = (int(_w), int(_h))
            except Exception as e:  # noqa: BLE001
                log(f"  [v3/render] ⚠ 소스 해상도 프로브 실패 — 줌·fit 생략: {e}")
        if _src is not None:
            geo = band_crop_size(design.aspect_ratio, _src, picture)
            zoomed = []
            if geo is not None:
                for _ri, z in _zoom_by_idx.items():
                    c = render_tl[_ri]
                    key = f"{c.get('role') or 'build'}_{_ri}"
                    base_rows = None
                    if key in crop_map and Path(crop_map[key]).exists():
                        try:
                            base_rows = json.loads(Path(crop_map[key]).read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            base_rows = None
                    rows = zoom_crop_rows(base_rows, float(z["factor"]), str(z.get("anchor") or "center"), geo)
                    p = output_dir / f"v3_crop_zoom_{_ri}.json"
                    p.write_text(json.dumps(rows), encoding="utf-8")
                    crop_map[key] = p
                    zoomed.append({"render_clip": _ri, "plan_clip": int(z["clip"]), "factor": z["factor"],
                                   "anchor": z.get("anchor"), "from_sec": z.get("from_sec"),
                                   "crop": [rows[0]["crop_w"], rows[0]["crop_h"]], "base": bool(base_rows)})
                    log(f"  [v3/render] 줌 clip{_ri} ×{float(z['factor']):.2f} {z.get('anchor')} "
                        f"→ crop {rows[0]['crop_w']}×{rows[0]['crop_h']}")
            elif _zoom_by_idx:
                log("  [v3/render] ⚠ 밴드 비율 크롭 불가(가로 여유 없음) — 줌 생략")
            fitted = []
            if _fit_render_idx:
                from app.v3 import letterbox as _lb
                if _lb.is_full(picture, *_src):
                    _fp = (0, 0, _src[0], _src[1])
                else:
                    _fp = (int(picture["x"]), int(picture["y"]), int(picture["w"]), int(picture["h"]))
                for _ri in sorted(_fit_render_idx):
                    if 0 <= _ri < len(clips):
                        clips[_ri] = _dc.replace(clips[_ri], fit_picture=_fp)
                        crop_map.pop(f"{clips[_ri].role}_{_ri}", None)   # fit 은 크롭 맵과 배타
                        fitted.append(_ri)
                        log(f"  [v3/render] 정보 화면 전체 맞춤 clip{_ri} — 그림 {_fp[2]}×{_fp[3]}@({_fp[0]},{_fp[1]})")
            edit_fx_audit = {"zooms": zoomed, "fits": fitted,
                             "emphasis": [{"index": e.get("index"), "text": e.get("text"), "scale": e.get("scale"),
                                           "color": e.get("color")} for e in _v3s.get("emphasis") or []]}
    # Narration targets are selected from narration + uncropped source, never mouth motion.
    # Apply after automatic zooms so they cannot crop away the selected action/person.
    _narration_framing_audit = []
    if getattr(design, "enable_reframe", True) and any(c.get("narration_framing") for c in render_tl):
        from app.v3.narration_framing import apply as _apply_narration_framing
        _probe = json.loads((output_dir / "checkpoint_probe.json").read_text(encoding="utf-8"))
        _narration_framing_audit = _apply_narration_framing(
            render_tl, clips, crop_map, output_dir=output_dir,
            src_size=(int(_probe["width"]), int(_probe["height"])), picture=picture, design=design, log=log)
    # 내레이션 시작 효과음 — cue 와 같은 믹스 경로(E19-5 sfx_audio)를 탄다.
    # 번들에 narration_manifest.json 이 없으면 빈 리스트라 RenderInputs 도 필터그래프도
    # 종전과 완전히 같다. 자리는 cue_files 가 확정된 뒤(존재하는 파일만 남은 목록) —
    # 리드인 실측이 그 mp3 를 읽어야 한다.
    try:
        from app.modules.sfx_narration import place_narration_sfx
        _narr_sfx = place_narration_sfx(
            cue_files, app_root=_root, run_dir=output_dir,
            seed=output_dir.name,
            speed=float(getattr(design, "video_speed", 1.0) or 1.0))
    except Exception as _e:                    # 효과음 때문에 편이 죽지 않는다
        log(f"  [sfx-narration] 배치 실패 — 효과음 없이 계속: {_e}")
        _narr_sfx = []
    for _s in _narr_sfx:
        _n = _s["_narration"]
        log(f"  [sfx-narration] cue{_n['cue_index']} {_n['tag']} {_s['start_sec']:.3f}s "
            f"← {_n['id']} (리드인 {_n['lead_in_sec']*1000:.0f}ms, 피크 {_n['peak_sec']*1000:.0f}ms)")
    # 라벨 등장 효과음 — 같은 믹스 경로에 더한다. `busy_windows` 는 소리 있는 구간
    # (cue 창 ∪ 대사 자막)이고, `pop-up-something` 처럼 "화면에 라벨만 있을 때"로
    # 한정된 소리가 그 판정을 쓴다. 라벨이 0개면 빈 리스트라 종전과 같다.
    try:
        from app.modules.sfx_narration import place_label_sfx
        _busy = [(float(f["cue"]["start_sec"]), float(f["cue"]["end_sec"]))
                 for f in cue_files if f.get("cue", {}).get("end_sec") is not None]
        _busy += [(float(s["start_sec"]), float(s["end_sec"])) for s in segments
                  if s.get("start_sec") is not None and s.get("end_sec") is not None]
        # ⚠ 소스는 **실제로 그려지는 `labels`** 다(plan_labels 가 아니라).
        # human_flow 는 story 비트의 labels 가 비어 있고 Stage 4 가 직접 쓴
        # `v3_style.labels`(authored)가 화면에 나간다 — plan_labels 를 보면
        # 라벨이 2개 떠 있는데 효과음은 0개가 된다(2026-09-03 실측).
        _label_sfx = place_label_sfx(
            labels, app_root=_root, run_dir=output_dir,
            seed=output_dir.name,
            speed=float(getattr(design, "video_speed", 1.0) or 1.0),
            busy_windows=_busy)
    except Exception as _e:                    # 효과음 때문에 편이 죽지 않는다
        log(f"  [sfx-label] 배치 실패 — 효과음 없이 계속: {_e}")
        _label_sfx = []
    # 동시에 때리는 쌍은 내레이션만 남긴다(사용자 지시 2026-09-03).
    from app.modules.sfx_narration import drop_label_collisions
    _label_sfx, _collided = drop_label_collisions(_narr_sfx, _label_sfx)
    for _s in _label_sfx:
        _n = _s["_label"]
        log(f"  [sfx-label] {_n['at']:.3f}s 「{_n['text']}」 ← {_n['id']} "
            f"({'화면 전용' if _n['quiet'] else '소리 있음'})")
    for _s in _collided:
        _n = _s["_label"]
        log(f"  [sfx-label] 내레이션과 동시 타격 → 드롭: {_n['at']:.3f}s "
            f"「{_n['text']}」 ({_n['id']})")
    # 강조 자막 타격음(2026-09-08, v9/v10 규칙: 강조 줄 = 줌 = 효과음 한 쌍). 강조가 없으면 빈 리스트.
    # b3bd45a(2026-09-17 01:06)가 '원본 대사 보호'로 전부 생략했던 것을 사용자 지시("강조 효과음 다시 켜고", 같은 날 저녁)로 복원.
    _emph_sfx: list = []
    if _emph:
        try:
            from app.modules.sfx_narration import place_emphasis_sfx
            # `sfx: false` 인 강조 줄은 소리 없이 글자·팝만(2026-09-11 — 한 문장을 여러 줄로 나눠 전부 강조하면
            # 줄마다 타격음이 겹친다. 문장 첫 줄만 소리를 낸다).
            _no_sfx = {i for i in (resolve_emphasis_index(e, segments) for e in (_v3s.get("emphasis") or [])
                                   if e.get("sfx") is False) if i is not None}
            _emph_lines = [{"start_sec": float(segments[i]["start_sec"]), "text": segments[i].get("text")}
                           for i in sorted(_emph) if 0 <= i < len(segments) and i not in _no_sfx]
            _emph_sfx = place_emphasis_sfx(_emph_lines, app_root=_root, run_dir=output_dir,
                                           seed=output_dir.name + ":emph",
                                           speed=float(getattr(design, "video_speed", 1.0) or 1.0))
            _emph_sfx, _c2 = drop_label_collisions(_narr_sfx + _label_sfx, _emph_sfx)
            for _s in _emph_sfx:
                _n = _s["_label"]
                log(f"  [sfx-emphasis] {_n['at']:.3f}s 「{_n['text']}」 ← {_n['id']}")
        except Exception as _e:                # 효과음 때문에 편이 죽지 않는다
            log(f"  [sfx-emphasis] 배치 실패 — 효과음 없이 계속: {_e}")
            _emph_sfx = []
    _all_sfx = _narr_sfx + _label_sfx + _emph_sfx
    inputs = RenderInputs(
        video_path=Path(video_path),
        clips=clips,
        subtitle_path=sub_path,
        crop_timeline_map=crop_map,
        title_text=(plan.get("layout") or {}).get("top_title") or "",
        work_title=(plan.get("layout") or {}).get("bottom_label") or "",
        output_path=out_path,
        canvas_width=config.canvas_width, canvas_height=config.canvas_height,
        top_title_height=config.top_title_height,
        bottom_label_height=config.bottom_label_height,
        design=design,
        tts_subtitle_path=tts_path,
        tts_cue_files=cue_files or None,
        original_audio_gain_db=int(audio_mix.get("original_gain_db", -3)),
        tts_audio_gain_db=int(audio_mix.get("tts_gain_db", -3)),
        text_subtitle_path=texts_path,
        muted_windows=muted_windows or None,
        muted_gain_db=muted_gain_db,
        source_fps=plan.get("source_fps"),
        output_fps=plan.get("output_fps"),
        sfx_audio=_all_sfx or None,
        work_min_top=_work_min_top,
        title_prefit=True,          # 줄별 크기는 위 fit_title_sizes 가 폰트 실측으로 맞췄다
    )
    t0 = time.time()
    render_short(inputs)
    cost = {"elapsed": round(time.time() - t0, 1), "bytes": out_path.stat().st_size,
            "clips": len(clips), "cues": len(cue_files),
            "muted_windows": len(inputs.muted_windows or []), "labels": len(labels),
            "subtitles_drawn": draw_subs,
            "subject_anchor_clips": len(crop_map),
            "text_stack": {"subtitle_margin_v": int(_sub_margin),
                           "tts_margin_v": int(_tts_margin),
                           "work_top": int(_work_top_final),
                           "work_min_top": _work_min_top,
                           "capped": any(n.startswith("⚠") for n in _stack_notes)}}
    # 썸네일 안전 구역 — 이 편의 실제 값(실제 제목 크기 · 번인 회피 뒤 자막 스택)으로 다시 잰 끝
    _tt_actual = _sr.estimate_title_block(
        design, _geom, line_count=max(1, len([ln for ln in _title_text.split("\n") if ln.strip()])))[0]
    _bottom_actual = int(_work_top_final) + estimate_work_height(design)
    if _title_over:
        cost["title_overflow"] = _title_over
    cost["thumb_safe"] = {"safe": [THUMB_SAFE_TOP, THUMB_SAFE_BOTTOM],
                          "title_top": int(_tt_actual), "bottom": int(_bottom_actual),
                          "band": [int(_geom.top), int(_geom.bottom)],
                          "actions": _thumb["actions"], "unmet": _thumb["unmet"],
                          "band_shift": _thumb["band_shift"]}
    if _tt_actual < THUMB_SAFE_TOP - SAFE_TOP_TOLERANCE_PX or _bottom_actual > THUMB_SAFE_BOTTOM:
        log(f"  [v3/썸네일] ⚠ 최종 배치가 안전 구역 밖 — 제목 윗변 {_tt_actual} · 하단 아랫변 {_bottom_actual}")
    # 인물 가장자리 점검(2026-09-18 사용자 결정) — 영상 밴드는 꽉 채운 채 두고, 폰 재생 화면이 잘라내는
    # 좌우 100px 띠에 **인물 얼굴이 걸린 구간**만 알린다. 완성본 프레임에서 직접 재므로 크롭 방식과 무관.
    # 고치는 건 사람 몫(손편집 비트 crop_x · 컷 선택) — 엔진은 자동으로 크롭을 옮기지 않는다.
    try:
        _edges = _sz.edge_faces_in_video(out_path, int(_geom.top), int(_geom.bottom),
                                         canvas_w=config.canvas_width, canvas_h=config.canvas_height,
                                         **({"require_success": True} if _fill_only else {}))
    except Exception as _e:  # noqa: BLE001 — 점검은 안전장치, 렌더 결과는 그대로
        _edges = []
        if _fill_only:
            cost["edge_faces_error"] = str(_e)
        log(f"  [v3/안전구역] 인물 가장자리 점검 실패({_e}) — 건너뜀")
    if _edges:
        cost["edge_faces"] = _edges
        for _r in _edges:
            _side = "왼" if _r["side"] == "left" else "오른"
            log(f"  [v3/안전구역] ⚠ {_r['start']:.0f}~{_r['end']:.0f}s 인물 얼굴이 {_side}쪽 잘림 띠에 걸림 "
                f"(얼굴 x {_r['box'][0]}~{_r['box'][2]} · 잘리는 비율 최대 {int(_r['max_cut'] * 100)}%) — 배치 확인")
    else:
        log(f"  [v3/안전구역] 인물 가장자리 점검 — 걸린 얼굴 없음(잘림 띠 0~{_sz.SAFE_X0} · {_sz.SAFE_X1}~{config.canvas_width})")
    if picture:
        cost["letterbox_crop"] = {**picture, "clips": len(crop_map)}   # 회귀 0: 없으면 키 없음
    if speaker_audit:
        cost["speaker_tracking"] = {"detector": (channel_design or {}).get("face_detector") or FACE_DETECTOR_DEFAULT,
                                    "mode": "pan" if str((channel_design or {}).get("speaker_tracking") or "").lower() == "pan" else "hold",
                                    "clips": speaker_audit}            # 갭 12: 켠 실행만
    if _narration_framing_audit:
        cost["narration_framing"] = _narration_framing_audit
    if edit_fx_audit or _emph:
        cost["edit_fx"] = {**edit_fx_audit, "emphasis_lines": len(_emph)}   # 2026-09-08: 있을 때만
    if _label_face_records:
        cost["label_face_avoid"] = _label_face_records                      # 2026-09-08: 겹친 라벨만
    log(f"  [v3/render] {out_path.name} — {cost['elapsed']}s · "
        f"{cost['bytes'] // (1024 * 1024)}MB")
    return out_path, cost


# ── validate 확장 (14) — 전부 순수 계산 + 프레임 실측 ──────────────────────

def check_exception_overlap(timeline: list[dict], stage1_doc: dict) -> dict:
    """신규 ③ — 최종 컷이 exception 구간과 겹치면 유입(0 이어야 한다)."""
    zones = []
    for key, zone in (stage1_doc.get("exception_sector") or {}).items():
        if isinstance(zone, dict) and zone.get("start") and zone.get("end"):
            zones.append((key, schemas.parse_ts(zone["start"]),
                          schemas.parse_ts(zone["end"])))
    hits = []
    for c in timeline:
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        for key, z0, z1 in zones:
            if min(e, z1) - max(s, z0) > 0.01:
                hits.append({"zone": key, "clip": [s, e],
                             "overlap_sec": round(min(e, z1) - max(s, z0), 3)})
    return {"zones": len(zones), "violations": hits}


def check_tts_conflicts(resources: dict, plan: dict, stage2_doc: dict,
                        grid: dict) -> dict:
    """신규 ② — cue 창(소스 좌표 신원)이 뮤트 안 된 importance≥4 유성 span 과 겹침 0."""
    from app.v3.story import MUTE_MAX_IMPORTANCE, build_span_index
    span_index, _ = build_span_index(stage2_doc, grid)
    selected = [sid for c in plan.get("timeline") or []
                for sid in c.get("span_ids") or []]
    violations = []
    for f in resources.get("tts_cue_files") or []:
        cue = f.get("cue") or {}
        if cue.get("start_sec") is None:
            continue
        c0 = float(cue["source_time_sec"])
        # 계획 창이 아니라 **실측 오디오 길이**가 실제 겹침이다(적대 리뷰 —
        # fit 소진 '잘림 감수' 오디오가 창을 넘어 다음 대사를 밟는 재현)
        c1 = c0 + max(float(cue.get("duration_sec") or 0),
                      float(cue.get("fit_actual_sec") or 0))
        muted = set(cue.get("muted_span_ids") or [])
        for sid in selected:
            sp = span_index.get(sid)
            if not sp or not sp["is_audio"] or sid in muted \
                    or sp["importance"] <= MUTE_MAX_IMPORTANCE:
                continue
            # 허용치는 리소스의 물리 트림 임계(+0.05 '잘림 감수')와 같은 자여야
            # 한다 — 0.01 로 재면 시스템이 스스로 허용한 0.04s 삐짐이 hard_fail 로
            # 승격된다(2026-09-02 EP02 실사고).
            if min(c1, sp["t_out"]) - max(c0, sp["t_in"]) > 0.06:
                violations.append({"cue_beat": cue.get("beat"), "span": sid,
                                   "importance": sp["importance"]})
    return {"checked_cues": len(resources.get("tts_cue_files") or []),
            "violations": violations}


def check_progression(timeline: list[dict], segments: list[dict],
                      resources: dict) -> dict:
    """§9-D ① 진행감 — 화면 변화 이벤트(컷·자막 등장·cue 시작) 간 최대 간격.

    3s 를 넘는 창은 경고로 나열한다 — silent_break 비트는 의도된 호흡이라
    role 표기를 함께 실어 사람이 가려 읽게 한다(차단 아님)."""
    events = [0.0]
    role_at: list[tuple[float, float, str]] = []
    off = 0.0
    for c in timeline:
        dur = assemble.clip_len(c)
        role_at.append((off, off + dur, str(c.get("role") or "")))
        events.append(off)
        off += dur
    total = off
    events += [float(s["start_sec"]) for s in segments]
    for f in resources.get("tts_cue_files") or []:
        cue = f.get("cue") or {}
        if cue.get("start_sec") is not None:
            events.append(float(cue["start_sec"]))
    events = sorted({round(min(max(e, 0.0), total), 3) for e in events}) + [total]
    warnings = []
    for a, b in zip(events, events[1:]):
        if b - a > PROGRESSION_MAX_GAP_SEC:
            role = next((r for s, e, r in role_at if s <= a < e), "")
            warnings.append({"window": [round(a, 2), round(b, 2)],
                            "gap_sec": round(b - a, 2), "role": role})
    return {"events": len(events), "max_gap_allowed": PROGRESSION_MAX_GAP_SEC,
            "warnings": warnings}


def check_loop_continuity(video_path: Path, tmp_dir: Path) -> dict:
    """§9-D ② 루프 정합 — 첫/끝 프레임의 평균 절대 오차(0~255). 경고 모드."""
    ffmpeg = find_ffmpeg_command("ffmpeg")
    first, last = tmp_dir / "loop_first.png", tmp_dir / "loop_last.png"
    try:
        subprocess.run([ffmpeg, "-y", "-i", str(video_path), "-frames:v", "1",
                        str(first)], check=True, capture_output=True)
        subprocess.run([ffmpeg, "-y", "-sseof", "-0.5", "-i", str(video_path),
                        "-frames:v", "1", str(last)], check=True, capture_output=True)
        from PIL import Image
        import numpy as np
        a = np.asarray(Image.open(first).convert("L").resize((90, 160)), dtype=float)
        b = np.asarray(Image.open(last).convert("L").resize((90, 160)), dtype=float)
        diff = float(abs(a - b).mean())
    except Exception as e:  # noqa: BLE001 — 측정 실패는 커버리지 표기(경고 모드)
        return {"status": "skipped", "reason": f"{type(e).__name__}: {e}"}
    return {"status": "ok", "mean_abs_diff": round(diff, 1),
            "warning": diff > LOOP_DIFF_WARN}


QC_PROMPT = """첨부한 프레임들은 세로 쇼츠(1080×1920) 최종본의 샘플이다. 화면 사고만 찾아라 — 취향 평가 금지.
검사 항목: ① 자막/제목이 화면 밖으로 잘림 ② 텍스트 레이어끼리 겹침 ③ 검정 밴드 침범(영상이 제목/로고 밴드를 덮음) ④ 빈 화면(검정/단색) ⑤ 글자 깨짐 — □(두부)·물음표 연속 등 글리프 누락.
출력(JSON만): {"issues": [{"frame": 0, "kind": "clip|overlap|band|blank|glyph", "note": "한 줄"}]} — 문제없으면 빈 배열."""


def frame_vision_qc(gemini, video_path: Path, tmp_dir: Path, *,
                    n_frames: int = QC_FRAME_COUNT, log=print) -> dict:
    """신규 ④ — 최종본 샘플 프레임 Flash 검사(경고 모드 — 차단 아님)."""
    ffmpeg = find_ffmpeg_command("ffmpeg")
    ffprobe = find_ffmpeg_command("ffprobe")
    out = subprocess.run([ffprobe, "-v", "quiet", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(video_path)],
                         capture_output=True, text=True)
    try:
        dur = float(out.stdout.strip())
    except ValueError:
        return {"status": "skipped", "reason": "duration 측정 실패"}
    frames = []
    try:
        for i in range(n_frames):
            t = dur * (i + 0.5) / n_frames
            p = tmp_dir / f"qc_{i}.jpg"
            subprocess.run([ffmpeg, "-y", "-ss", f"{t:.2f}", "-i", str(video_path),
                            "-frames:v", "1", "-q:v", "5", str(p)],
                           check=True, capture_output=True)
            frames.append(p)
    except subprocess.CalledProcessError as e:
        # 경고 모드 — 추출 실패가 파이프라인을 죽이면 안 된다
        return {"status": "skipped",
                "reason": f"프레임 추출 실패: {(e.stderr or b'')[-200:]!r}"}
    types = gemini.types
    parts = [types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg")
             for p in frames]
    parts.append(QC_PROMPT)
    try:
        resp = gemini.client.models.generate_content(
            model=gemini.config.flash_model_name, contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.0, response_mime_type="application/json",
                max_output_tokens=2048))
        from app.modules.gemini_client import _extract_json_from_markdown
        data = json.loads(_extract_json_from_markdown(resp.text or ""))
        issues = data.get("issues") if isinstance(data, dict) else None
        issues = issues if isinstance(issues, list) else []
    except Exception as e:  # noqa: BLE001 — QC 실패가 발행을 막지 않는다(경고 모드)
        return {"status": "skipped", "reason": f"{type(e).__name__}: {e}"}
    if issues:
        log(f"  [v3/validate] ⚠ 프레임 QC 경고 {len(issues)}건")
    return {"status": "ok", "frames": n_frames, "issues": issues}


def run_validate(*, plan: dict, grid: dict, stage1_doc: dict, stage2_doc: dict,
                 segments: list[dict], resources: dict,
                 final_path: Path | None, tmp_dir: Path,
                 cast_names: list[str] | None = None,
                 gemini=None, log=print) -> dict:
    """validate 확장 — 수치 4종 + §9-D. 경고는 차단하지 않는다(기획: 경고 모드)."""
    doc: dict[str, Any] = {"schema": "v3_validate/v1"}
    doc["snap_belt"] = assemble.verify_edit_plan(plan, grid)                    # ①
    doc["tts_conflicts"] = check_tts_conflicts(resources, plan, stage2_doc, grid)  # ②
    doc["exception_ingress"] = check_exception_overlap(plan.get("timeline") or [],
                                                       stage1_doc)             # ③
    doc["progression"] = check_progression(plan.get("timeline") or [],
                                           segments, resources)                # §9-D ①
    # M9-A/B 계기판 — 조립 시점의 예방을 통과한 뒤에도 남은 게 있는지(이중 방어)
    from app.v3 import textcheck
    doc["subtitle_text"] = {
        "repetition": textcheck.check_repetition(segments),
        "name_suspects": textcheck.check_names(segments, cast_names or [])}
    if final_path is not None and final_path.exists():
        doc["loop_continuity"] = check_loop_continuity(final_path, tmp_dir)    # §9-D ②
        if gemini is not None:
            doc["frame_qc"] = frame_vision_qc(gemini, final_path, tmp_dir, log=log)  # ④
        else:
            doc["frame_qc"] = {"status": "skipped", "reason": "gemini 미제공"}
    hard_fail = (
        (doc["snap_belt"]["pct"] is not None and doc["snap_belt"]["pct"] < 100.0)
        or doc["tts_conflicts"]["violations"]
        or doc["exception_ingress"]["violations"])
    doc["hard_fail"] = bool(hard_fail)
    doc["warnings_total"] = (
        len(doc["subtitle_text"]["repetition"])
        + len(doc["subtitle_text"]["name_suspects"])
        + len(doc["progression"]["warnings"])
        + (1 if doc.get("loop_continuity", {}).get("warning") else 0)
        + len(doc.get("frame_qc", {}).get("issues") or []))
    return doc
