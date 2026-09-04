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
    for k in ("title_y", "work_title_y", "work_font_size"):
        if design.get(k) is not None:
            up[k] = int(design[k])
    if design.get("tts_width") is not None:
        up["tts_width"] = float(design["tts_width"])
    if design.get("face_tracking") is not None:
        up["enable_reframe"] = bool(design["face_tracking"])
    for k in ("platform_image", "platform_text", "platform_color", "platform_align"):
        if design.get(k):
            up[k] = str(design[k])
    for k in ("platform_x", "platform_y", "platform_image_width",
              "platform_image_height", "platform_font_size"):
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
TITLE_MAX_WIDTH = 980             # 템플릿 max_width_px — 좌우 여백 각 50px
TITLE_CHAR_W = 1.0                # 한글 1자 폭 ÷ 글자크기 (Jalnan 92px 프레임 실측)
TITLE_SPACE_W = 0.3               # 공백은 좁다 — 1.0 으로 세면 멀쩡한 제목을 줄인다


POP_TO_FX = {"soft": "pop_soft", "strong": "pop_strong"}   # none 은 태그 없음


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


def fit_title_sizes(title_text: str, sizes: list[int]) -> list[int]:
    """제목 줄별 크기를 폭 980px 안으로 줄인다. 순수.

    렌더러의 자동 줄바꿈은 **1줄 크기**로만 폭을 재서(renderer `_max_chars_for(
    base_size)`) 2줄이 더 크면 그 줄이 화면 밖으로 잘린다 — 템플릿 크기(92/112)를
    그대로 넣자 프레임 QC 가 4프레임 전부에서 잡았다. 템플릿 자신의 계약이
    max_width_px 980 이므로 여기서 줄마다 맞춘다(렌더러는 무변경 — 공용이다)."""
    lines = [ln for ln in str(title_text).split("\n") if ln.strip()]
    out = list(sizes)
    for i, size in enumerate(sizes):
        if i >= len(lines):
            continue
        ln = lines[i].strip()
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


def estimate_work_top(design, *, band_bottom: int, canvas_height: int = 1920) -> int:
    """하단 작품명/로고의 **윗변 y** 추정 — renderer 의 로고 배치 수식을 그대로 따른다
    (safe_top = 밴드 하단+20 · contain 박스 · center 정렬 · 캔버스 하단 클램프). 순수.
    로고 PNG 크기를 못 읽으면 박스 높이로 본다(보수적 = 더 위)."""
    H = int(canvas_height)
    safe_top = int(band_bottom) + WORK_GAP_BELOW_VIDEO
    if getattr(design, "work_type", "text") != "image" or not getattr(design, "work_value", None):
        return max(int(getattr(design, "work_title_y", 1400)), safe_top)
    box_w = int(getattr(design, "work_image_width", 350) or 350)
    box_h = getattr(design, "work_image_height", None)
    try:
        from PIL import Image
        with Image.open(str(design.work_value)) as im:
            nat_w, nat_h = im.size
        _bh = int(box_h) if box_h else int(nat_h * (box_w / nat_w))
        sc = min(box_w / nat_w, _bh / nat_h)
        logo_h = max(2, int(nat_h * sc) // 2 * 2)
    except Exception:  # noqa: BLE001 — 실측 실패 = 박스 높이(보수적)
        logo_h = int(box_h) if box_h else max(60, int(box_w * 0.5))
    y = safe_top
    if getattr(design, "work_image_align", "top") == "center":
        y = safe_top + (H - 20 - safe_top - logo_h) // 2
    if y + logo_h > H - 20:
        y = H - logo_h - 20
    return max(safe_top, y)


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
        dur = assemble.clip_duration(assemble.clip_len(c), fps)
        if not c.get("use_original_audio"):
            for a, z, on in assemble.split_by_windows(cs, ce, narration_windows_src):
                if on:
                    continue
                r0 = min(a - cs, dur)
                r1 = dur if z >= ce - 1e-6 else min(z - cs, dur)
                out.append((round(off + r0, 3), round(off + r1, 3)))
        off += dur
    return out


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

def subject_crop_map(timeline: list[dict], *, video_path: Path,
                     aspect_ratio: str, output_dir: Path,
                     src_size: tuple[int, int] | None = None,
                     log=print) -> dict[str, Path]:
    """무성 인서트 클립의 subject_pos → 렌더러 crop_timeline_map (2026-09-02).

    v3 는 얼굴 크롭이 범위 외라 전 클립 고정 **중앙** 크롭이었다 — 구석의 주 피사체
    (실사고: GPS 폰 화면)가 세로 크롭에 통째로 잘려 나갔다. Stage 2 가 영상을 보며
    적어 둔 subject_pos(left/right)를 그 클립의 crop x 앵커로 소비한다. 렌더러는
    이미 crop_timeline_map 을 받게 되어 있으므로(v1 얼굴 추적과 같은 통로) 렌더
    코드는 무변경. 키가 없는 클립 = 종전 중앙(회귀 0). 순수 재료라 재개마다 재도출
    해도 같은 좌표다(E19-4 얼굴 회피와 같은 이유로 체크포인트에 안 남긴다)."""
    anchored = [(i, c) for i, c in enumerate(timeline)
                if c.get("subject_pos") in ("left", "right")]
    if not anchored:
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
    try:
        r_w, r_h = (int(x) for x in str(aspect_ratio).split(":"))
    except (ValueError, AttributeError):
        return {}
    # 밴드 비율의 최대 크롭 — 렌더러 하류(scale increase → 중앙 crop)가 재크롭으로
    # 앵커를 되물리지 않으려면 여기서 **정확히 밴드 비율**로 잘라야 한다
    crop_h = src_h
    crop_w = int(src_h * r_w / r_h) & ~1
    if crop_w >= src_w - 2:          # 가로 여유가 없다(세로 크롭 소재) — 앵커 무의미
        return {}
    out_map: dict[str, Path] = {}
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


def render_final(*, video_path: Path, plan: dict, style_doc: dict,
                 segments: list[dict], resources: dict, story_doc: dict,
                 output_dir: Path, out_name: str = "final_1080x1920.mp4",
                 channel_design: dict | None = None,
                 muted_gain_db: float | None = None,
                 log=print) -> tuple[Path, dict]:
    """edit_plan + style + 자막/cue → 1080×1920 최종본. 반환: (경로, 실측).

    channel_design: 채널이 CLI(--design-*)로 준 키(어댑터 어휘). Stage 4 가 정한
    디자인보다 **위** — 채널 정체성·권리사 표기는 AI 연출이 아니라 계약이다.
    muted_gain_db: 내레이션 덮개 구간의 원본 볼륨(dB). None = 종전 완전 무음."""
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
    design = _dc.replace(
        design,
        title_font=get_font_path(design.title_font, _root),
        subtitle_font=get_font_path(design.subtitle_font, _root))

    clips = [StoryClip(role=str(c.get("role") or "build"),
                       start_sec=float(c["clip_start_sec"]),
                       end_sec=float(c["clip_end_sec"]),
                       subtitle=str(c.get("subtitle") or ""),
                       use_original_audio=bool(c.get("use_original_audio", True)),
                       hold_sec=float(c.get("hold_sec") or 0.0))
             for c in plan["timeline"]]

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
    # 제목 줄별 크기를 이 편의 실제 글자수로 맞춘다
    _title_text = (plan.get("layout") or {}).get("top_title") or ""
    _fitted = fit_title_sizes(_title_text, list(design.title_sizes))
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
    _sub_margin = int(design.subtitle_y_margin)
    _tts_margin = int(design.tts_line_y_margin)
    _sub_h = _sr.estimate_subtitle_height(design.subtitle_size)
    _floor_top = _title_bottom + SUB_GAP_PX
    # 밴드 아래로 내리기(2026-09-04) — 채널 margin 이 밴드 위치와 안 맞으면 두 줄 블록이
    # 영상에 얹힌다. 번인 회피(아래 블록)는 이 값 위에서 **올리기만** 하므로 순서가 이렇다.
    _work_top = estimate_work_top(design, band_bottom=_geom.bottom,
                                  canvas_height=config.canvas_height)
    _cd = channel_design or {}
    _sub_off = _cd.get("subtitle_band_offset")
    _tts_off = _cd.get("tts_band_offset")
    if _sub_off is not None:          # 대사 어절 자막은 v3 에서 늘 한 줄(12자)
        _sub_margin = band_anchored_margin(band_bottom=_geom.bottom, offset=int(_sub_off),
                                           lines=1, font_size=design.subtitle_size,
                                           canvas_height=config.canvas_height)
        log(f"  [v3/자막배치] 자막 — 밴드 하단 {_geom.bottom} + {_sub_off}px 앵커 → margin_v {_sub_margin}")
    if _tts_off is not None:          # 전역값은 두 줄 기준(번인 회피·구 소비자용), 줄별은 아래
        _tts_margin = band_anchored_margin(band_bottom=_geom.bottom, offset=int(_tts_off),
                                           lines=2, font_size=design.tts_line_font_size,
                                           canvas_height=config.canvas_height)
        log(f"  [v3/자막배치] 내레이션 — 밴드 하단 {_geom.bottom} + {_tts_off}px 앵커 → margin_v {_tts_margin}(2줄 기준)")
    _tts_base = _tts_margin
    for _name, _size, _cur in (("자막", design.subtitle_size, _sub_margin),
                               ("내레이션", design.tts_line_font_size, _tts_margin)):
        if (_name == "자막" and _sub_off is not None) or (_name == "내레이션" and _tts_off is not None):
            continue                  # 상대 앵커가 정한 자리 — 절대값 클램프는 안 건다
        _new, _note = fit_margin_below_band(
            _cur, canvas_height=config.canvas_height, band_bottom=_geom.bottom,
            block_height=_sr.estimate_subtitle_height(_size), work_top=_work_top)
        if _note:
            log(f"  [v3/자막배치] {_name} — {_note}")
        if _name == "자막":
            _sub_margin = int(_new)
        else:
            _tts_margin = int(_new)

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

    # 대사 ASS — C6 세그먼트(편집본 좌표)를 그대로 이벤트로
    sub_path = output_dir / "v3_subtitles.ass"
    sub_style = SubtitleStyle(
        font_name=ass_family, font_size=design.subtitle_size,
        primary_color=_style_color(design.subtitle_color or "#FFFFFF"),
        outline=3, margin_v=_sub_margin)
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

    def _seg_style(seg: dict, idx: int = 0) -> dict | None:
        st: dict[str, Any] = {}
        if seg.get("color"):
            st["color"] = str(seg["color"])
        m = _line_margins[idx] if idx < len(_line_margins) else None
        if m is not None:
            st["y"] = round((config.canvas_height - int(m)) / float(config.canvas_height), 5)
        t0 = float(seg["start_sec"])
        for w0, w1, fx in fx_windows:
            if w0 <= t0 < w1:
                st["fx"] = fx
                break
        return st or None

    build_ass_from_segments(
        [SimpleNamespace(start_sec=float(s["start_sec"]), end_sec=float(s["end_sec"]),
                         text=str(s["text"]), style=_seg_style(s, i))
         for i, s in enumerate(segments)],
        sub_path, sub_style)

    # TTS 자막 ASS — cue 텍스트(합성 fit 반영본)
    cue_files = [f for f in (resources.get("tts_cue_files") or [])
                 if f.get("cue", {}).get("start_sec") is not None
                 and Path(f.get("path", "")).exists()]
    tts_path = None
    if cue_files:
        tts_path = output_dir / "v3_tts.ass"
        tts_style = SubtitleStyle(
            font_name=ass_family, font_size=design.tts_line_font_size,
            primary_color=design.tts_line_color, outline=3,
            margin_v=_tts_margin)
        _caps = [narration_caption(str(f["cue"]["text"])) for f in cue_files]
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
                                         style={"y": 1.0 - m / config.canvas_height})
                         for f, c, m in zip(cue_files, _caps, _pm)]
            log(f"  [v3/자막배치] 내레이션 줄별 margin_v {sorted(set(_pm))} (cue {len(_pm)}개)")
        else:
            _tts_segs = [SpeechSegment(start_sec=float(f["cue"]["start_sec"]),
                                       end_sec=float(f["cue"]["end_sec"]), text=c)
                         for f, c in zip(cue_files, _caps)]
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
    for lb in authored:
        labels.append({"text": lb["text"], "start_sec": float(lb["start_sec"]),
                       "end_sec": float(lb["end_sec"]),
                       "x": float(lb.get("x", 0.5)),
                       "y": float(lb.get("y", LABEL_Y_RATIO)),
                       "rotate": float(lb.get("rotate", 0.0)),
                       "size": stage4.LABEL_SIZE, "stroke": "dark_thick",
                       "fx": str(lb.get("fx") or "pop"),
                       "color": str(lb.get("color") or _cycle_color(len(labels)))})
    if not authored:
        for lb in plan_labels(story_doc, plan):
            pos = placed.get(lb["index"]) or {}
            labels.append({"text": lb["text"], "start_sec": lb["start_sec"],
                           "end_sec": lb["end_sec"],
                           "x": float(pos.get("x", 0.5)),
                           "y": float(pos.get("y", LABEL_Y_RATIO)),
                           "rotate": float(pos.get("rotate", 0.0)),
                           "size": stage4.LABEL_SIZE, "stroke": "dark_thick",
                           "fx": str(pos.get("fx") or "pop"),
                           "color": str(pos.get("color") or _cycle_color(lb["index"]))})
    texts_path = None
    if labels:
        texts_path = output_dir / "v3_labels.ass"
        build_texts_ass(labels, texts_path)

    # 적대 리뷰 확정(critical): renderer 는 use_original_audio 를 읽지 않았다 —
    # 뮤트 창(편집본 좌표)을 additive 필드로 넘겨 원본 트랙에만 volume=0 (cue 는 산다).
    # M15: 창은 **클립 전체가 아니라 내레이션이 실제로 점유한 구간**이다. 클립 전체를
    # 끄면 내레이션이 끝난 뒤가 완전 무음이 된다(실측 도입부 3.57초).
    src_windows = assemble.narration_windows(story_doc)
    all_windows = sorted(w for wins in src_windows.values() for w in wins)
    muted_windows = cover_mute_windows(plan["timeline"], all_windows,
                                       plan.get("source_fps"))

    out_path = output_dir / out_name
    audio_mix = plan.get("audio_mix") or {}
    # 얼굴 크롭은 여전히 범위 외(발주서) — 이 맵은 무성 인서트의 **피사체 앵커**만
    # 싣는다(subject_crop_map 독스트링 참조). 앵커 없는 판은 빈 dict = 종전 그대로.
    # 채널 face_tracking:false(--no-reframe) 는 v3 에서 피사체 앵커 크롭까지 끈다 —
    # v1 과 같은 뜻('원본을 가운데 정렬로 넣는다'). 기본 True = 종전 그대로.
    if getattr(design, "enable_reframe", True):
        crop_map = subject_crop_map(plan["timeline"], video_path=Path(video_path),
                                    aspect_ratio=design.aspect_ratio,
                                    output_dir=output_dir, log=log)
    else:
        crop_map = {}
        log("  [v3/render] 채널 face_tracking=false — 피사체 앵커 크롭 끔(중앙)")
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
    _all_sfx = _narr_sfx + _label_sfx
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
        sfx_audio=_all_sfx or None,
    )
    t0 = time.time()
    render_short(inputs)
    cost = {"elapsed": round(time.time() - t0, 1), "bytes": out_path.stat().st_size,
            "clips": len(clips), "cues": len(cue_files),
            "muted_windows": len(inputs.muted_windows or []), "labels": len(labels),
            "subject_anchor_clips": len(crop_map)}
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
