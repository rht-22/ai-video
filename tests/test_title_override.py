"""제목 크기·위치 오버라이드 실효화(F-409) — 편집실 제목 드래그의 엔진 파트.

오케스트레이터 실측(2026-08-19): --design-title-size 는 title_size 만 바꿨는데
실제 그리기는 title_sizes([70,90] 기본)를 써서 화면에 반영되지 않았고(title_size 는
줄바꿈 계산만), title_y 는 동적 배치(영상 위 20px)가 항상 이겨 폴백으로만 쓰였다.
→ cli 가 title_color→title_colors 와 같은 패턴으로 title_sizes 를 조립하고(줄 간
비율 유지 스케일), --design-title-y-fixed 가 동적 배치를 끈다(기본 false = 무영향).
"""
from __future__ import annotations

from pathlib import Path

from app.cli import _build_design_config, build_parser
from app.config import DesignConfig
from app.modules.renderer import RenderInputs, _build_filtergraph
from app.modules.story_builder import StoryClip


def _design(*extra_args: str) -> DesignConfig:
    p = build_parser()
    args = p.parse_args([
        "create_shorts", "--title", "T", "--video", "x.mp4", "--subtitle", "x.srt",
        *extra_args,
    ])
    return _build_design_config(args)


def _inputs(design: DesignConfig, title_text: str = "머리\n꼬리") -> RenderInputs:
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text=title_text, work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=design)


# ── 제목 크기: title_sizes 조립 ──

def test_title_size_assembles_title_sizes_keeping_ratio():
    """지정 값은 1줄 기준 — 기본 [70,90]의 줄 간 비율(후킹 줄이 더 크다)을 유지한 채
    전체를 스케일한다. 계약: docs/edit_overrides_v3.md 의 F-409 절."""
    d = _design("--design-title-size", "35")
    assert d.title_size == 35            # 줄바꿈 계산용 단일 필드도 종전대로
    assert d.title_sizes == [35, 45]     # 90×(35/70)=45


def test_title_size_reaches_drawn_font_sizes():
    """회귀 방어: 렌더러가 실제 그리는 fontsize 는 title_sizes 에서 나온다 —
    조립이 빠지면 기본 [70,90]이 그려져 지정 크기가 화면에 반영되지 않는다."""
    fg = _build_filtergraph(_inputs(_design("--design-title-size", "35")), 1, 0)
    assert "fontsize=35" in fg and "fontsize=45" in fg
    assert "fontsize=70" not in fg and "fontsize=90" not in fg


def test_title_size_unspecified_keeps_defaults():
    d = _design()
    assert d.title_sizes == [70, 90]
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "fontsize=70" in fg and "fontsize=90" in fg


# ── 제목 Y 고정 ──
# 기본 레이아웃(1:1): scaled_h=1080, overlay_y=(1920-1080)//2=420,
# 제목 2줄 총높이 70+90+행간30=190 → 동적 top = 420-190-20 = 210.

def test_dynamic_title_placement_is_default():
    """기본은 종전 동적 배치(영상 위 20px) — title_y(120)는 폴백일 뿐 안 쓰인다.
    기존 채널 무영향의 근거."""
    fg = _build_filtergraph(_inputs(_design()), 1, 0)
    assert "y=210" in fg
    assert "y=120" not in fg


def test_title_y_fixed_wins_over_dynamic():
    d = _design("--design-title-y", "300", "--design-title-y-fixed")
    assert d.title_y_fixed is True
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "y=300" in fg                 # 1줄이 지정 위치에
    assert "y=210" not in fg             # 동적 배치는 꺼졌다


def test_title_y_without_fixed_flag_stays_dynamic():
    """--design-title-y 만 주면 종전 동작(동적 배치 우선) 그대로 — 플래그 없이
    동작이 바뀌면 기존 채널이 놀란다."""
    fg = _build_filtergraph(_inputs(_design("--design-title-y", "300")), 1, 0)
    assert "y=210" in fg and "y=300" not in fg
