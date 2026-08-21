"""제목 줄별 배경 박스(없음/둥근/각진)·굵게 — 2026-08-21.

계약:
· design 키 title_box(2)∈{none,round,rect}, title_box_color(2), title_bold(2)(스위치). 엔진은
  리스트 필드(title_boxes/title_box_colors/title_bolds)만 읽고 CLI 가 조립한다(F-409 교훈).
· 각진 = drawtext 내장 box(boxborderw=pad), 둥근 = Pillow PNG + movie/overlay(글자보다 먼저),
  굵게 = 같은 색 borderw. 여백 0.30em·라운드 0.25em·획 0.025em 은 **최종** 글자 크기 비례.
· 박스 줄의 위아래 pad 는 줄 높이에 포함 → 동적 배치가 박스 기준으로 영상 위 20px 을 지키고
  이웃 박스끼리 안 겹친다. 기본값(none)은 종전 그래프와 바이트 단위로 동일.
"""
from __future__ import annotations

from pathlib import Path

import pytest

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


def _inputs(design: DesignConfig, title_text: str = "머리\n꼬리",
            out: Path = Path("out.mp4"), title_segments=None) -> RenderInputs:
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text=title_text, work_title="작품",
        output_path=out, canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=design,
        title_segments=title_segments)


def _title_filters(fg: str) -> list[str]:
    return [f for f in fg.split(";") if "drawtext" in f and "fontsize=" in f
            and ("fontsize=70" in f or "fontsize=90" in f or "title" in f)]


# ── 기본값: 무영향 ──

def test_defaults_are_neutral_and_graph_unchanged():
    d = DesignConfig()
    assert d.title_boxes == ["none", "none"]
    assert d.title_bolds == [False, False]
    fg = _build_filtergraph(_inputs(_design()), 1, 0)
    assert "box=1" not in fg and "borderw" not in fg and "movie=" not in fg
    assert "y=210" in fg            # 종전 동적 배치 그대로(test_title_override 와 동일 기준)


# ── CLI 조립 ──

def test_cli_assembles_lists_and_keeps_other_line_default():
    d = _design("--design-title-box2", "rect", "--design-title-box-color2", "#FF3E9D",
                "--design-title-bold2")
    assert d.title_boxes == ["none", "rect"]
    assert d.title_box_colors == ["#000000", "#FF3E9D"]
    assert d.title_bolds == [False, True]


def test_cli_rejects_unknown_box_style():
    with pytest.raises(SystemExit):
        _design("--design-title-box", "oval")


def test_renderer_rejects_unknown_box_style_without_cli():
    from dataclasses import replace
    d = replace(DesignConfig(), title_boxes=["blob", "none"])
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(d), 1, 0)


# ── 각진 박스(rect): drawtext box, 그 줄에만 ──

def test_rect_box_only_on_its_line_and_shifts_block_up():
    """2줄(90px) rect → pad=27. 블록 높이 70+(90+54)+30=244 → top=420-244-20=156.
    1줄 y=156(패딩 없음), 2줄 글자 y=156+70+30+27=283."""
    fg = _build_filtergraph(_inputs(_design("--design-title-box2", "rect",
                                             "--design-title-box-color2", "#FF3E9D")), 1, 0)
    l1 = next(f for f in fg.split(";") if "fontsize=70" in f and "drawtext" in f)
    l2 = next(f for f in fg.split(";") if "fontsize=90" in f and "drawtext" in f)
    assert "box=1:boxcolor=#FF3E9D:boxborderw=27" in l2
    assert "box=1" not in l1
    assert "y=156" in l1 and "y=283" in l2
    assert "movie=" not in fg


# ── 굵게: 같은 색 borderw ──

def test_bold_uses_same_color_border_on_its_line_only():
    fg = _build_filtergraph(_inputs(_design("--design-title-bold2")), 1, 0)
    l1 = next(f for f in fg.split(";") if "fontsize=70" in f and "drawtext" in f)
    l2 = next(f for f in fg.split(";") if "fontsize=90" in f and "drawtext" in f)
    assert "borderw=2:bordercolor=#FFFF00" in l2     # round(0.025×90)=2, 2줄 기본색
    assert "borderw" not in l1
    assert "y=210" in l1                              # 굵게는 높이를 안 바꾼다


def test_bold_and_rect_coexist():
    fg = _build_filtergraph(_inputs(_design("--design-title-box", "rect",
                                             "--design-title-bold")), 1, 0)
    l1 = next(f for f in fg.split(";") if "fontsize=70" in f and "drawtext" in f)
    assert "box=1:boxcolor=#000000:boxborderw=21" in l1 and "borderw=2:bordercolor=white" in l1


# ── 둥근 박스(round): PNG + overlay, 글자보다 먼저 ──

def test_round_box_png_overlay_precedes_text(tmp_path):
    """1줄(70px) round → pad=21, 라운드 18. PNG 폭 = 2글자×70(폴백 측정)+42 = 182, 높이 112.
    블록 (70+42)+90+30=232 → top=168. 박스 y=168, 글자 y=189, x=(1080-182)/2=449."""
    d = _design("--design-title-box", "round", "--design-title-box-color", "#FF3E9D")
    fg = _build_filtergraph(_inputs(d, out=tmp_path / "out.mp4"), 1, 0)
    png = tmp_path / "title_box_title_0.png"
    assert png.exists()
    from PIL import Image
    im = Image.open(png)
    assert im.size == (182, 112) and im.mode == "RGBA"
    assert im.getpixel((91, 56)) == (255, 62, 157, 255)   # 가운데는 채움색
    assert im.getpixel((0, 0))[3] == 0                     # 모서리는 투명(둥글다)
    ov = fg.index("overlay=449:168")
    txt = fg.index("fontsize=70")
    assert ov < txt                                        # 박스가 글자 아래
    assert "y=189" in fg
    assert "box=1" not in fg                               # round 는 drawtext box 를 안 쓴다


def test_round_box_color_alpha_suffix(tmp_path):
    d = _design("--design-title-box2", "round", "--design-title-box-color2", "black@0.5")
    _build_filtergraph(_inputs(d, out=tmp_path / "o.mp4"), 1, 0)
    from PIL import Image
    im = Image.open(tmp_path / "title_box_title_1.png")
    cx, cy = im.size[0] // 2, im.size[1] // 2
    assert im.getpixel((cx, cy)) == (0, 0, 0, 128)


# ── 길이 축소와 연동 ──

def test_pad_follows_length_scaled_font_size():
    """16자 1줄 → 70×0.77=54 → pad=round(16.2)=16."""
    d = _design("--design-title-box", "rect")
    fg = _build_filtergraph(_inputs(d, title_text="가나다라마바사아자차카타파하거너\n꼬리"), 1, 0)
    l1 = next(f for f in fg.split(";") if "fontsize=54" in f and "drawtext" in f)
    assert "boxborderw=16" in l1


def test_wrapped_line_keeps_its_origin_line_box():
    """줄바꿈으로 1줄이 두 시각 줄이 되면 둘 다 1줄 박스, 2줄은 없음."""
    d = _design("--design-title-box", "rect", "--design-title-box-color", "#123456")
    long1 = "하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나"   # 20자 초과 → wrap
    fg = _build_filtergraph(_inputs(d, title_text=f"{long1}\n꼬리"), 1, 0)
    ls = [f for f in fg.split(";") if "drawtext" in f and "fontfile" in f and "#123456" in f]
    assert len(ls) >= 2
    l2 = next(f for f in fg.split(";") if "fontsize=90" in f and "drawtext" in f)
    assert "box=1" not in l2


# ── E7 회전: 박스가 캔버스 안에서 회전 전에 ──

def test_rotate_puts_box_inside_canvas_before_rotate(tmp_path):
    d = _design("--design-title-rotate", "10", "--design-title-box", "round",
                "--design-title-box2", "rect", "--design-title-bold2")
    fg = _build_filtergraph(_inputs(d, out=tmp_path / "o.mp4"), 1, 0)
    canvas = fg.index("color=c=black@0.0")
    ov = fg.index("movie=")
    rot = fg.index("rotate=")
    assert canvas < ov < rot                          # 캔버스 → 박스 overlay → (글자) → rotate
    l2 = next(f for f in fg.split(";") if "fontsize=90" in f and "drawtext" in f)
    assert "box=1" in l2 and "borderw=2" in l2
    # 블록 높이에 pad 가 들어간다: 1줄 pad 21×2 + 2줄 pad 27×2 = 96 만큼 커진 캔버스
    assert "s=1080x" in fg


# ── E8 시간대별 제목: 세그먼트 줄에도 같은 스타일, enable 창 유지 ──

def test_segments_apply_box_with_enable_window(tmp_path):
    d = _design("--design-title-box2", "rect", "--design-title-box-color2", "#FF3E9D")
    segs = [{"start_sec": 0.0, "end_sec": 2.0, "text": "앞\n뒤"},
            {"start_sec": 3.0, "end_sec": 5.0, "text": "둘째\n줄"}]
    fg = _build_filtergraph(_inputs(d, out=tmp_path / "o.mp4", title_segments=segs), 1, 0)
    boxed = [f for f in fg.split(";") if "box=1:boxcolor=#FF3E9D" in f]
    assert len(boxed) == 2 and all("enable='between(t," in f for f in boxed)
