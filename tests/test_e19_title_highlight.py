"""E19-2 제목 단어 단위 색 강조 회귀 가드.

발주서: docs/prompts/e19-drama-clip-preset.md §2. 벤치마크(신병4)의 "가장 **X같은**
질문" — 한 줄 안에서 훅 어절만 색을 바꾸는 장치다. 계약 요점:

- 문법은 제목 문자열 안 `{{어절}}` 하나뿐. **마크업이 없으면 렌더 경로가 종전과
  바이트 동일하다**(회귀 0 — E10 문자열 가드가 함께 지킨다).
- 강조색은 design 키 `title_highlight_color`(기본 #FFE24A).
- 세그먼트 배치는 같은 TTF 를 Pillow 로 재서 절대 x 로 나란히 놓는다(둥근 박스 PNG 폭
  측정과 같은 신뢰 — `_measure_title_text_width`).
- 잔여물(홀짝 불일치·빈 강조)은 **떼고 그리되 건별 경고** — 제목이 깨진 채(중괄호
  노출) 발행되는 것이 최악이다.
"""
from __future__ import annotations

from pathlib import Path

from app.cli import _build_design_config, build_parser
from app.config import DesignConfig
from app.modules.renderer import (
    RenderInputs,
    _build_filtergraph,
    extract_title_highlights,
    strip_title_markup,
)
from app.modules.story_builder import StoryClip


def _design(*extra_args: str) -> DesignConfig:
    p = build_parser()
    args = p.parse_args(["create_shorts", "--title", "T", "--video", "x.mp4",
                         "--subtitle", "x.srt", *extra_args])
    return _build_design_config(args)


def _inputs(design: DesignConfig, title: str = "제목", **over) -> RenderInputs:
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text=title, work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=design, **over)


# ══════════════════════════════════════════════════════════════════════════
# 마크업 파서 — 잔여물은 떼고 경고
# ══════════════════════════════════════════════════════════════════════════
def test_strip_no_markup_identical():
    text = "군 입대하고 들은\n가장 X같은 질문"
    out, warns = strip_title_markup(text)
    assert out == text and warns == []


def test_strip_keeps_balanced_pair():
    out, warns = strip_title_markup("가장 {{X같은}} 질문")
    assert out == "가장 {{X같은}} 질문" and warns == []


def test_strip_unclosed_open_removed_with_warning():
    out, warns = strip_title_markup("가장 {{X같은 질문")
    assert out == "가장 X같은 질문"
    assert len(warns) == 1


def test_strip_stray_close_and_empty_pair():
    out, warns = strip_title_markup("가장}} {{}} 질문")
    assert out == "가장 " + " 질문".strip() + "" or out == "가장  질문"   # 마커만 제거
    assert "{{" not in out and "}}" not in out
    assert len(warns) == 2


def test_extract_segments_single_line():
    lines, hl = extract_title_highlights([(0, "가장 {{X같은}} 질문")])
    assert lines == [(0, "가장 X같은 질문")]
    assert hl == {0: [("가장 ", False), ("X같은", True), (" 질문", False)]}


def test_extract_carry_across_wrapped_lines():
    lines, hl = extract_title_highlights([(0, "{{두"), (0, "단어}} 끝")])
    assert lines == [(0, "두"), (0, "단어 끝")]
    assert hl[0] == [("두", True)]
    assert hl[1] == [("단어", True), (" 끝", False)]


def test_extract_no_markup_passthrough():
    src = [(0, "가장 X같은 질문"), (1, "둘째 줄")]
    lines, hl = extract_title_highlights(src)
    assert lines == src and hl == {}


# ══════════════════════════════════════════════════════════════════════════
# design 키
# ══════════════════════════════════════════════════════════════════════════
def test_design_key_default_and_cli():
    assert DesignConfig().title_highlight_color == "#FFE24A"
    d = _design("--design-title-highlight-color", "#00FF88")
    assert d.title_highlight_color == "#00FF88"


# ══════════════════════════════════════════════════════════════════════════
# 필터그래프 — 회귀 0 + 세그먼트 배치
# ══════════════════════════════════════════════════════════════════════════
def test_fg_without_markup_unchanged_by_highlight_color():
    """마크업 없는 제목은 강조색 값과 무관하게 필터그래프가 바이트 동일 — 회귀 0."""
    fg_a = _build_filtergraph(_inputs(DesignConfig(), "가장 X같은 질문"), 1, 0)
    fg_b = _build_filtergraph(
        _inputs(DesignConfig(title_highlight_color="#00FF00"), "가장 X같은 질문"), 1, 0)
    assert fg_a == fg_b
    assert "x=(w-text_w)/2" in fg_a          # 종전 중앙 정렬 경로 그대로


def test_fg_markup_draws_segments_with_absolute_x():
    """세그먼트 3개가 절대 x 로 나란히 — 기본 폰트(파일 아님)는 1em/글자 근사라
    폭이 결정적이다: '가장 '(3자)·'X같은'(3자)·' 질문'(3자) × 70px."""
    fg = _build_filtergraph(_inputs(DesignConfig(), "가장 {{X같은}} 질문"), 1, 0)
    assert "{{" not in fg and "}}" not in fg
    x0 = (1080 - 70 * 9) // 2                # 225
    assert f"text='가장 ':fontcolor=white:fontsize=70:x={x0}:" in fg
    assert f"text='X같은':fontcolor=#FFE24A:fontsize=70:x={x0 + 210}:" in fg
    assert f"text=' 질문':fontcolor=white:fontsize=70:x={x0 + 420}:" in fg


def test_fg_markup_uses_design_highlight_color():
    fg = _build_filtergraph(
        _inputs(DesignConfig(title_highlight_color="#00FF88"), "가장 {{X같은}} 질문"), 1, 0)
    assert "fontcolor=#00FF88" in fg and "#FFE24A" not in fg


def test_fg_residue_stripped_and_legacy_path():
    """홀짝 불일치는 떼고 종전 중앙 정렬로 — 중괄호가 화면에 나가지 않는다."""
    fg = _build_filtergraph(_inputs(DesignConfig(), "가장 {{X같은 질문"), 1, 0)
    assert "{{" not in fg and "}}" not in fg
    assert "text='가장 X같은 질문':fontcolor=white" in fg
    assert "x=(w-text_w)/2" in fg


def test_fg_markup_wrapping_counts_clean_length():
    """마커가 줄바꿈 계산을 부풀리면 안 된다 — 20자 이내 제목이 마커 때문에 두 줄로
    갈라지는 회귀 방지. 클린 13자는 한 줄(축소 없이 70px)이어야 한다."""
    fg = _build_filtergraph(
        _inputs(DesignConfig(), "가장 {{X같은}} 질문입니다요"), 1, 0)   # 클린 13자
    assert "fontsize=70" in fg                                          # 축소 없음 = 한 줄


def test_fg_rect_box_underlay_then_segments():
    """rect 박스 줄의 강조: 세그먼트별 box 는 조각난 상자가 되므로, 상자색 글자+box
    밑그림 한 장을 먼저 깔고 세그먼트는 box 없이 그 위에 그린다."""
    d = _design("--design-title-box", "rect", "--design-title-box-color", "#112233")
    fg = _build_filtergraph(_inputs(d, "가장 {{X같은}} 질문"), 1, 0)
    assert "fontcolor=#112233" in fg and "box=1:boxcolor=#112233" in fg
    assert "fontcolor=#FFE24A" in fg
    # 세그먼트 drawtext 에는 box 파라미터가 없다
    seg = fg[fg.index("fontcolor=#FFE24A"):]
    assert "box=1" not in seg.split("[")[0]


def test_fg_rotated_title_supports_markup():
    fg = _build_filtergraph(
        _inputs(DesignConfig(title_rotate=5.0), "가장 {{X같은}} 질문"), 1, 0)
    assert "rotate=" in fg and "fontcolor=#FFE24A" in fg and "{{" not in fg


def test_fg_title_segments_markup():
    """E8 시간대별 제목 텍스트도 같은 문법을 탄다 — enable 창과 함께."""
    fg = _build_filtergraph(
        _inputs(DesignConfig(), "기본 제목",
                title_segments=[{"text": "A {{B}}", "start_sec": 0.0, "end_sec": 2.0}]),
        1, 0)
    assert "fontcolor=#FFE24A" in fg
    assert "enable='between(t,0.000,2.000)'" in fg
    assert "{{" not in fg
