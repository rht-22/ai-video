"""플랫폼 표기(--design-platform-*) — 영상영역 왼쪽 상단 로고/텍스트 (2026-08-19).

권리사 가이드의 '영상 내 플랫폼 노출' 요구(가왕쇼: 티빙, 약한영웅: Wavve 로고 등)용.
종전엔 파이프라인에 화면 삽입 기능이 없어 설명란 표기로 갈음했다(SNL _guide 참조).
위치는 캔버스가 아니라 **영상영역**(overlay_y) 기준 — aspect_ratio 를 바꿔도 표기가
영상을 따라가야 채널마다 위치를 다시 잡지 않는다.
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


def _inputs(design: DesignConfig) -> RenderInputs:
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text="제목", work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=design)


# ── CLI 배선 ──

def test_platform_text_flags():
    d = _design("--design-platform-text", "티빙", "--design-platform-x", "40",
                "--design-platform-y", "32", "--design-platform-font-size", "48",
                "--design-platform-color", "#FF3B30")
    assert (d.platform_text, d.platform_x, d.platform_y) == ("티빙", 40, 32)
    assert (d.platform_font_size, d.platform_color) == (48, "#FF3B30")


def test_platform_image_resolves_from_assets_logos():
    # 작품 로고(get_logo_path)와 같은 규약 — 이름만 주면 assets/logos 에서 절대경로로.
    d = _design("--design-platform-image", "RZsv4.png")
    assert Path(d.platform_image).is_absolute()
    assert Path(d.platform_image).name == "RZsv4.png"


def test_defaults_off():
    d = _design()
    assert d.platform_image is None and d.platform_text is None
    assert (d.platform_x, d.platform_y) == (24, 24)


# ── 필터그래프 ──

def test_no_platform_no_filter():
    fg = _build_filtergraph(_inputs(DesignConfig()), 1, 0)
    assert "[with_pf]" not in fg


def test_text_mark_anchored_to_video_area():
    """13:9 → 영상 상단 y=586(짝수보정 전). 표기는 overlay_y + platform_y 에 있어야 한다."""
    d = DesignConfig(aspect_ratio="13:9", platform_text="티빙", platform_x=24, platform_y=24)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    overlay_y = (1920 - int(1080 * 9 / 13)) // 2          # renderer 와 같은 계산 = 586
    assert f"x=24:y={overlay_y + 24}[with_pf]" in fg
    assert "text='티빙'" in fg


def test_text_mark_follows_aspect_ratio():
    """같은 오프셋이라도 비율이 바뀌면 영상을 따라 움직여야 한다 — 캔버스 고정이면 회귀."""
    def y_of(ratio):
        d = DesignConfig(aspect_ratio=ratio, platform_text="티빙")
        fg = _build_filtergraph(_inputs(d), 1, 0)
        return next(part for part in fg.split(";") if "[with_pf]" in part)
    assert y_of("1:1") != y_of("16:9")


def test_image_mark_contained_in_box(tmp_path):
    """이미지는 (width x height) 박스 contain — 초광폭 로고가 박스 높이를 뚫으면 안 된다."""
    from PIL import Image
    logo = tmp_path / "pf.png"
    Image.new("RGBA", (600, 150), (255, 255, 255, 255)).save(logo)   # 4:1 초광폭
    d = DesignConfig(platform_image=str(logo), platform_image_width=150,
                     platform_image_height=80)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    # 150/600=0.25 vs 80/150=0.53 → 너비가 먼저 닿는다 → 150x36(짝수보정)
    assert "scale=150:36[pfm]" in fg
    assert "overlay=24:" in fg


def test_image_wins_over_text(tmp_path):
    from PIL import Image
    logo = tmp_path / "pf.png"
    Image.new("RGBA", (100, 100), (255, 255, 255, 255)).save(logo)
    d = DesignConfig(platform_image=str(logo), platform_text="티빙")
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "[pfm]" in fg and "text='티빙'" not in fg


# ── video_y (영상 세로 위치 — 같은 2026-08-19 작업분) ──

def test_video_y_default_centers():
    fg = _build_filtergraph(_inputs(DesignConfig(aspect_ratio="13:9")), 1, 0)
    assert "pad=1080:1920:0:586:" in fg          # (1920 - int(1080*9/13)) // 2 = 586


def test_video_y_moves_stack_up():
    d = DesignConfig(aspect_ratio="13:9", video_y=380, platform_text="티빙")
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "pad=1080:1920:0:380:" in fg
    # 플랫폼 표기도 영상을 따라 올라와야 한다 (380+24)
    assert "x=24:y=404[with_pf]" in fg


def test_video_y_clamped_to_canvas():
    d = DesignConfig(aspect_ratio="13:9", video_y=5000)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "pad=1080:1920:0:1173:" in fg         # 1920 - 747(짝수보정 전 높이) 가 하한 클램프


# ── platform_align (오른쪽 상단 앵커 — 2026-08-19 사용자 요청) ──

def test_platform_align_right_text():
    d = DesignConfig(aspect_ratio="13:9", platform_text="티빙", platform_align="right",
                     platform_x=24)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "x=w-text_w-24:" in fg          # 오른쪽 가장자리에서 24px 안쪽


def test_platform_align_right_image(tmp_path):
    from PIL import Image
    logo = tmp_path / "pf.png"
    Image.new("RGBA", (300, 100), (255, 255, 255, 255)).save(logo)
    d = DesignConfig(platform_image=str(logo), platform_align="right",
                     platform_image_width=150, platform_image_height=80, platform_x=24)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    # 150x50 로 contain → x = 1080 - 150 - 24 = 906
    assert "overlay=906:" in fg


def test_platform_align_cli():
    d = _design("--design-platform-text", "티빙", "--design-platform-align", "right")
    assert d.platform_align == "right"


def test_platform_align_default_left():
    assert DesignConfig().platform_align == "left"
    d = DesignConfig(platform_text="티빙", platform_x=24)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "x=24:" in fg
