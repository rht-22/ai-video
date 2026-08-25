"""줄 스타일·타이밍 오버라이드 계약 — **편집실과 합의된 규약이라 동작을 바꾸면 안 된다.**

원본: video-localization-project `engine/render.py`(E5 JP-2, docs/subtitle-style-overrides.md).
관제 편집실이 보낸 값을 검증하고 ASS 인라인 태그로 옮긴다. 두 경로(SHOTCONE·잔망루피)가
같이 쓰는 계약이라 여기만 고치면 양쪽이 함께 움직인다.

⚠ 조용한 무시는 금지다 — 위반은 ValueError. 사람이 정한 값이 소리 없이 증발하면
'고쳤는데 왜 그대로지'가 된다.
"""
from __future__ import annotations

import re
from typing import Any, Optional

LINE_STYLE_KEYS = {"size", "y", "color", "rotate", "width"}
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def validate_line_style(style: Any) -> dict[str, Any]:
    """style 오버라이드 검증 → 정규화 사본. 위반은 ValueError(조용한 무시 = 사람 값 증발).

    size: 양수(1080×1920 캔버스 px) · y: 0~1(자막 하단, 하단=1) · color: #RRGGBB ·
    rotate: -180~180(도, **시계방향 양수** — images 와 동일 규약) ·
    width: 0.3~1.0(그 줄이 쓸 가로 폭, 캔버스 대비 비율 — F-412). 모르는 키 즉시 거절.

    width 를 여기서도 받는 이유: KR 편집실이 정한 폭이 subtitle_segments.json 을 타고
    일본어판까지 따라온다(l3_apply 가 KO 백업 세그먼트 위에 번역만 얹는다). 이 목록에
    없으면 그 style 을 되돌려 보내는 JP 검수 수정이 통째로 거절돼 재렌더가 죽는다."""
    if not isinstance(style, dict):
        raise ValueError(f"style 은 객체여야 합니다: {style!r}")
    unknown = set(style) - LINE_STYLE_KEYS
    if unknown:
        raise ValueError(f"모르는 style 키: {sorted(unknown)} (허용: {sorted(LINE_STYLE_KEYS)})")
    out: dict[str, Any] = {}
    if style.get("size") is not None:
        size = float(style["size"])
        if size <= 0:
            raise ValueError(f"style.size 는 양수(px): {style['size']!r}")
        out["size"] = size
    if style.get("y") is not None:
        y = float(style["y"])
        if not 0.0 <= y <= 1.0:
            raise ValueError(f"style.y 는 0~1 비율: {style['y']!r}")
        out["y"] = y
    if style.get("color") is not None:
        c = str(style["color"])
        if not _COLOR_RE.match(c):
            raise ValueError(f"style.color 는 #RRGGBB: {style['color']!r}")
        out["color"] = c.upper()
    if style.get("rotate") is not None:
        rot = float(style["rotate"])
        if not -180.0 <= rot <= 180.0:
            raise ValueError(f"style.rotate 는 -180~180 도: {style['rotate']!r}")
        out["rotate"] = rot
    if style.get("width") is not None:
        w = float(style["width"])
        if not 0.3 <= w <= 1.0:
            raise ValueError(f"style.width 는 0.3~1.0 비율: {style['width']!r}")
        out["width"] = w
    return out


def validate_line_timing(item: Any) -> tuple[Optional[float], Optional[float]]:
    """start_sec/end_sec 오버라이드 검증 → (start, end). 없는 키는 None.

    편집본 시간축 초, 0 이상. 둘 다 있으면 end > start."""
    if not isinstance(item, dict):
        raise ValueError(f"오버라이드 항목은 객체여야 합니다: {item!r}")
    vals: list[Optional[float]] = []
    for key in ("start_sec", "end_sec"):
        v = item.get(key)
        if v is None:
            vals.append(None)
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{key} 는 숫자(초): {v!r}")
        if float(v) < 0:
            raise ValueError(f"{key} 는 0 이상: {v!r}")
        vals.append(float(v))
    start, end = vals
    if start is not None and end is not None and end <= start:
        raise ValueError(f"end_sec({end}) 는 start_sec({start}) 보다 커야 합니다")
    return start, end


def hex_to_ass_color(color: str) -> str:
    """#RRGGBB → ASS &HBBGGRR& (BGR)."""
    rr, gg, bb = color[1:3], color[3:5], color[5:7]
    return f"&H{bb}{gg}{rr}&".upper()


def style_ass_tags(style: dict[str, Any], play_res_y: int = 1920) -> str:
    """style → 인라인 ASS 태그(\\fs·\\1c·\\frz). y 는 별도(MarginV — style_margin_v).

    size 는 1080×1920 캔버스 px 계약 → PlayResY 가 다르면 비율 환산.
    rotate 는 계약이 시계방향 양수, ASS \\frz 는 반시계 양수 — **부호 반전은 엔진(여기)
    책임**(ai-video subtitle.py 와 동일 규약). 0 은 태그를 안 박는다."""
    tags = []
    if style.get("size") is not None:
        tags.append(f"\\fs{max(1, round(float(style['size']) * play_res_y / 1920))}")
    if style.get("color"):
        tags.append(f"\\1c{hex_to_ass_color(str(style['color']))}")
    if style.get("rotate") is not None and float(style["rotate"]) != 0.0:
        tags.append(f"\\frz{-float(style['rotate']):g}")
    return "".join(tags)


def style_margin_lr(style: dict[str, Any], play_res_x: int) -> int:
    """width(그 줄의 가로 폭 비율) → 이벤트 MarginL/MarginR(px, 좌우 대칭).
    미지정 0(=스타일 기본값 사용) — style_margin_v 와 같은 규약.

    글자 크기는 건드리지 않는다(F-412): 통만 좌우로 넓혀 줄이 접히는 것을 막는 값이다.
    최소 1 — 이벤트 여백 0 은 ASS 규약상 '스타일 기본값'이라 width=1(화면 끝까지)이
    증발한다."""
    if style.get("width") is None:
        return 0
    return max(1, round(play_res_x * (1.0 - float(style["width"])) / 2))


def style_margin_v(style: dict[str, Any], play_res_y: int) -> int:
    """y(자막 하단 비율) → 이벤트 MarginV(px). 미지정 0(=스타일 기본값 사용).

    최소 1 — ASS 규약상 이벤트 MarginV=0 은 '스타일 기본값'이라 y=1(맨 하단)이 증발한다."""
    if style.get("y") is None:
        return 0
    return max(1, round((1.0 - float(style["y"])) * play_res_y))
