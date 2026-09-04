"""v3 채널 design 주입(--design-*) + 플랫폼 표기 + 덮개 볼륨 — 2026-09-04.

가왕쇼 7화 v3 실런: '티빙'(채널 design platform_text)이 빠졌다 — v3 CLI 에 --design-*
이 없어 어댑터가 싣지 않았다. 사용자 지시로 플랫폼 표기뿐 아니라 채널 프리셋(밴드·
색·크기·폰트·작품명·face_tracking)까지 v1 과 같은 키로 받는다. 렌더러의 표기 자체는
tests/test_platform_mark.py 가, 어댑터 쪽 화이트리스트는 ves test_aivideo_v3_switch 가 지킨다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DesignConfig
from app.modules.renderer import RenderInputs, _build_filtergraph
from app.modules.story_builder import StoryClip
from app.v3.cli import CHANNEL_DESIGN_ARGS, build_parser, channel_design_from_args
from app.v3.finalize import (CHANNEL_PRESET_KEYS, channel_design_over_ai,
                             design_from_style, merge_channel_preset)
from app.v3.stage4 import RECAP_PRESET, STYLE_ALLOWED

_BASE = ["--video", "x.mp4", "--work-title", "가왕쇼"]


def _cd(*extra: str) -> dict:
    return channel_design_from_args(build_parser().parse_args([*_BASE, *extra]))


def _inputs(design: DesignConfig) -> RenderInputs:
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text="제목", work_title="가왕쇼",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=design)


# ── CLI ──

def test_unspecified_is_empty():
    assert _cd() == {}           # 빈 dict → run_v3 None → 지문·디자인 종전과 동일


def test_hanipjumak_channel_values_round_trip():
    # 한 입 주막 DB 실측 design(2026-09-04) 을 어댑터가 넘기는 모양 그대로
    cd = _cd("--design-video-y", "440", "--design-aspect-ratio", "13:9",
             "--design-title-font", "여기어때 잘난체 고딕 TTF",
             "--design-title-color", "white", "--design-title-color2", "#ff3b30",
             "--design-tts-color", "#F783AC", "--design-tts-y-margin", "550",
             "--design-work-title-y", "1430", "--design-subtitle-color", "#ffffff",
             "--no-reframe", "--design-platform-text", "티빙",
             "--design-platform-align", "right")
    assert cd == {"video_y": 440, "aspect_ratio": "13:9",
                  "title_font": "여기어때 잘난체 고딕 TTF", "title_color": "white",
                  "title_color2": "#ff3b30", "tts_color": "#F783AC", "tts_y_margin": 550,
                  "work_title_y": 1430, "subtitle_color": "#ffffff",
                  "face_tracking": False, "platform_text": "티빙", "platform_align": "right"}


def test_platform_image_resolves_and_missing_fails():
    cd = _cd("--design-platform-image", "RZsv4.png")
    assert Path(cd["platform_image"]).is_absolute()
    with pytest.raises(FileNotFoundError):
        _cd("--design-platform-image", "no_such_logo_zzz.png")


def test_flag_vocabulary_mirrors_v1_cli():
    # v1 app.cli 와 같은 플래그·같은 타입 — 갈리면 같은 채널 키가 두 경로에서 다르게 해석된다
    from app.cli import build_parser as v1_parser
    sub = next(a for a in v1_parser()._actions if a.dest == "command")
    v1 = {a.dest: a for a in sub.choices["create_shorts"]._actions}
    from app.v3.cli import V3_ONLY_DESIGN_KEYS
    for key, kw in CHANNEL_DESIGN_ARGS.items():
        dest = f"design_{key}"
        if key in V3_ONLY_DESIGN_KEYS:
            assert dest not in v1, dest          # v1 엔 없어야 한다(어댑터가 v1 잡엔 안 싣는다)
            continue
        assert dest in v1, dest
        assert v1[dest].type == kw["type"], dest
    assert "no_reframe" in v1


# ── finalize 병합 ──

def test_preset_merge_only_style_keys_and_identity_when_empty():
    assert merge_channel_preset(RECAP_PRESET, None) is RECAP_PRESET     # 회귀 0
    assert merge_channel_preset(RECAP_PRESET, {"platform_text": "티빙"}) is RECAP_PRESET
    merged = merge_channel_preset(RECAP_PRESET, {"video_y": 440, "aspect_ratio": "13:9",
                                                 "title_font": "X", "platform_text": "티빙"})
    assert merged["video_y"] == 440 and merged["aspect_ratio"] == "13:9"
    assert "title_font" not in merged and "platform_text" not in merged
    assert merged["title_color"] == RECAP_PRESET["title_color"]     # 나머지 보존
    # 프리셋에 들어가는 키 = Stage 4 어휘(STYLE_ALLOWED) + 밴드 위치
    assert CHANNEL_PRESET_KEYS == set(STYLE_ALLOWED) | {"video_y", "video_width"}


def test_channel_beats_ai_diff_at_render():
    merged = channel_design_over_ai({"title_color": "#000000", "subtitle_size": 60},
                                    {"title_color": "white"})
    assert merged == {"title_color": "white", "subtitle_size": 60}
    assert channel_design_over_ai({"a": 1}, None) == {"a": 1}


def test_design_from_style_maps_channel_keys():
    d = design_from_style({"title_font": "여기어때 잘난체 고딕 TTF", "subtitle_font": "Griun",
                           "title_y": 100, "work_title_y": 1430, "work_font_size": 44,
                           "tts_width": 0.9, "face_tracking": False,
                           "platform_text": "티빙", "platform_align": "right",
                           "platform_x": 30, "platform_font_size": 44})
    assert (d.title_font, d.subtitle_font) == ("여기어때 잘난체 고딕 TTF", "Griun")
    assert (d.title_y, d.work_title_y, d.work_font_size, d.tts_width) == (100, 1430, 44, 0.9)
    assert d.enable_reframe is False
    assert (d.platform_text, d.platform_align, d.platform_x, d.platform_font_size) \
        == ("티빙", "right", 30, 44)
    base = design_from_style({})
    assert base.enable_reframe is True and base.platform_text is None


def test_platform_text_reaches_filtergraph():
    d = design_from_style({"aspect_ratio": "13:9", "platform_text": "티빙",
                           "platform_align": "right"})
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "text='티빙'" in fg and "[with_pf]" in fg
    assert "[with_pf]" not in _build_filtergraph(
        _inputs(design_from_style({"aspect_ratio": "13:9"})), 1, 0)


# ── 작품명 위 플랫폼 줄(platform_placement=above_work, 2026-09-04) ──

def test_platform_above_work_draws_icon_and_text_above_work_title(tmp_path):
    from PIL import Image
    icon = tmp_path / "icon.png"
    Image.new("RGBA", (174, 175)).save(icon)
    d = design_from_style({"aspect_ratio": "24:23", "video_y": 443,
                           "platform_image": str(icon),
                           "platform_text": "지금 쿠팡플레이에서 시청하세요",
                           "platform_placement": "above_work"})
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "[with_pf]" not in fg                       # 밴드 모서리 표기는 안 그린다
    assert "[pfi]overlay=" in fg and "[with_pfi]" in fg    # 아이콘
    assert "text='지금 쿠팡플레이에서 시청하세요'" in fg and "[with_pft]" in fg
    # 작품명(텍스트 40px → 줄 56)이 줄(56)+여백(16)만큼 내려간다: 1497 → 1569
    assert ":y=1569[with_work]" in fg
    assert "y=1497+(56-text_h)/2[with_pft]" in fg       # 줄 윗변 = 작품명 − 16 − 56


def test_platform_band_default_is_unchanged_by_new_key():
    a = _build_filtergraph(_inputs(design_from_style({"aspect_ratio": "13:9", "platform_text": "티빙"})), 1, 0)
    b = _build_filtergraph(_inputs(design_from_style({"aspect_ratio": "13:9", "platform_text": "티빙",
                                                      "platform_placement": "band"})), 1, 0)
    assert a == b and "[with_pf]" in a and "[with_pft]" not in a


def test_finalize_reserves_platform_line_in_work_block():
    from app.v3.finalize import estimate_work_height, estimate_work_top, platform_line_block
    base = design_from_style({"aspect_ratio": "24:23", "video_y": 443})
    above = design_from_style({"aspect_ratio": "24:23", "video_y": 443, "platform_text": "x",
                               "platform_placement": "above_work"})
    assert platform_line_block(base) == 0 and platform_line_block(above) == 56 + 16
    assert estimate_work_height(above) == estimate_work_height(base) + 72
    # 블록 윗변은 그대로 밴드+20(줄이 그 자리를 차지) — 작품명 자체는 72px 아래
    assert estimate_work_top(base, band_bottom=1477) == 1497
    assert estimate_work_top(above, band_bottom=1477) == 1497
