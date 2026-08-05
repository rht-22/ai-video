"""텍스트 스타일 CLI 플래그(--design-tts-color 등) + ASS 색상 정규화 테스트."""

import pytest

from app.cli import _build_design_config, build_parser
from app.config import DesignConfig, to_ass_color


def _design(*extra_args: str) -> DesignConfig:
    """create_shorts 필수 인자 + 전달된 --design-* 인자로 DesignConfig 생성."""
    p = build_parser()
    args = p.parse_args([
        "create_shorts", "--title", "T", "--video", "x.mp4", "--subtitle", "x.srt",
        *extra_args,
    ])
    return _build_design_config(args)


# ── to_ass_color ──

def test_hex_converts_to_ass_bgr():
    # #87CEEB (하늘색) → BGR 역순 EB CE 87
    assert to_ass_color("#87CEEB") == "&H00EBCE87"
    assert to_ass_color("87CEEB") == "&H00EBCE87"


def test_ass_value_passes_through():
    assert to_ass_color("&H00EBCE87") == "&H00EBCE87"
    assert to_ass_color("&h00ebce87") == "&h00ebce87"


def test_named_color_converts():
    assert to_ass_color("white") == "&H00FFFFFF"
    assert to_ass_color("yellow") == "&H0000FFFF"  # R,G만 켜짐 → BGR로 00FFFF


def test_invalid_color_raises():
    with pytest.raises(ValueError):
        to_ass_color("#12345")       # 자릿수 부족
    with pytest.raises(ValueError):
        to_ass_color("#GGGGGG")      # 16진수 아님
    with pytest.raises(ValueError):
        to_ass_color("chartreuse")   # 미지원 이름


# ── CLI 배선 ──

def test_tts_color_flag():
    assert _design("--design-tts-color", "#87CEEB").tts_line_color == "&H00EBCE87"


def test_tts_color_accepts_raw_ass():
    assert _design("--design-tts-color", "&H00FF69B4").tts_line_color == "&H00FF69B4"


def test_tts_size_flag():
    assert _design("--design-tts-size", "88").tts_line_font_size == 88


def test_subtitle_color_flag():
    assert _design("--design-subtitle-color", "#00FFFF").subtitle_color == "&H00FFFF00"


def test_subtitle_color_defaults_to_none_for_preset():
    # 미지정 시 None → select_subtitle_style이 프리셋 색을 유지
    assert _design().subtitle_color is None


def test_title_color2_flag():
    d = _design("--design-title-color2", "#00FF00")
    assert d.title_colors == ["white", "#00FF00"]


def test_title_color_sets_first_line():
    d = _design("--design-title-color", "#FF0000")
    # line1만 교체, line2는 기본값 유지
    assert d.title_colors == ["#FF0000", "#FFFF00"]
    assert d.title_color == "#FF0000"


def test_both_title_colors():
    d = _design("--design-title-color", "red", "--design-title-color2", "#00FF00")
    assert d.title_colors == ["red", "#00FF00"]


def test_title_colors_untouched_when_no_flag():
    assert _design().title_colors == DesignConfig().title_colors


def test_defaults_unchanged():
    d = _design()
    assert d.tts_line_color == "&H00EBCE87"
    assert d.tts_line_font_size == 70
