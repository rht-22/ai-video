"""E10 — 영상 밴드 가로 크기(design.video_width, --design-video-width).

계약(편집실 미리보기와 합의 — ves-orchestrator e10 발주서):
· scaled_w = video_width (캔버스 px, 320~1080, 기본 1080 = 종전과 동일한 꽉 찬 폭)
· scaled_h = int(scaled_w * r_h / r_w) — **밴드 폭 기준**. 화면비는 밴드 직사각형의
  모양을, 크기는 video_width 하나로 정한다.
· pad_x = (W - scaled_w) // 2 — 가로는 항상 중앙(video_x 는 범위 밖).
· overlay_y 클램프·세로 중앙 기본값은 종전 그대로(scaled_h 만 작아진다).
· 홀수는 짝수 보정(scaled_w -= scaled_w % 2). 범위 밖·비숫자는 CLI·렌더 경계
  양쪽에서 즉시 실패(E7 패턴).
· 플랫폼 표기(left/right)는 캔버스가 아니라 **밴드 모서리**를 따른다.
· ⚠ video_width 는 E10 이전에도 DesignConfig 에 있었지만(기본 800) 렌더러가 읽지
  않았다 — 렌더러가 읽기 시작하면서 기본값을 1080 으로 올렸다(아무 플래그도 안 준
  기존 채널이 800px 로 줄어드는 회귀 방지).
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


def _inputs(design: DesignConfig) -> RenderInputs:
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text="제목", work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=design)


# ── 기본값 — 회귀 0 (아무 플래그도 안 준 기존 채널 전부) ──────────────────

def test_default_video_width_is_full_canvas():
    # 레거시 기본 800 이 살아나면 기존 채널 전부가 800px 로 줄어든다 — 1080 고정 가드.
    assert DesignConfig().video_width == 1080
    assert _design().video_width == 1080


def test_unspecified_keeps_legacy_filtergraph():
    """미지정 = 종전과 동일한 필터그래프(1:1 기본, 꽉 찬 폭, pad_x=0)."""
    fg = _build_filtergraph(_inputs(DesignConfig()), 1, 0)
    assert "scale=1080:1080:force_original_aspect_ratio=increase," in fg
    assert "crop=1080:1080," in fg
    assert "pad=1080:1920:0:420:color=#0D0011" in fg     # overlay_y = (1920-1080)//2


def test_unspecified_13_9_keeps_legacy_strings():
    # test_platform_mark.test_video_y_default_centers 와 같은 값 — 13:9 회귀 가드
    fg = _build_filtergraph(_inputs(DesignConfig(aspect_ratio="13:9")), 1, 0)
    assert "crop=1080:746," in fg                        # int(1080*9/13)=747 → 짝수보정
    assert "pad=1080:1920:0:586:" in fg


def test_explicit_1080_same_as_unspecified():
    a = _build_filtergraph(_inputs(DesignConfig()), 1, 0)
    b = _build_filtergraph(_inputs(DesignConfig(video_width=1080)), 1, 0)
    assert a == b


# ── 렌더 수식 — scale/crop/pad 문자열, pad_x 중앙, overlay_y ─────────────

def test_800_16_9_band():
    fg = _build_filtergraph(_inputs(DesignConfig(aspect_ratio="16:9", video_width=800)), 1, 0)
    # scaled_h = int(800*9/16) = 450, overlay_y = (1920-450)//2 = 735, pad_x = 140
    assert "scale=800:450:force_original_aspect_ratio=increase," in fg
    assert "crop=800:450," in fg
    assert "pad=1080:1920:140:735:" in fg


def test_800_1_1_band():
    fg = _build_filtergraph(_inputs(DesignConfig(aspect_ratio="1:1", video_width=800)), 1, 0)
    # scaled_h = 800, overlay_y = (1920-800)//2 = 560, pad_x = 140
    assert "scale=800:800:force_original_aspect_ratio=increase," in fg
    assert "crop=800:800," in fg
    assert "pad=1080:1920:140:560:" in fg


def test_320_lower_bound():
    fg = _build_filtergraph(_inputs(DesignConfig(aspect_ratio="1:1", video_width=320)), 1, 0)
    assert "scale=320:320:" in fg
    assert "crop=320:320," in fg
    assert "pad=1080:1920:380:800:" in fg                # pad_x=(1080-320)//2, y=(1920-320)//2


def test_odd_801_corrected_to_800():
    fg = _build_filtergraph(_inputs(DesignConfig(aspect_ratio="1:1", video_width=801)), 1, 0)
    # 801 → scaled_h=801, overlay_y=(1920-801)//2=559(종전 흐름: 보정 전 계산), 짝수보정 → 800x800
    assert "scale=800:800:" in fg
    assert "crop=800:800," in fg
    assert "pad=1080:1920:140:559:" in fg


def test_video_y_respected_with_narrow_band():
    d = DesignConfig(aspect_ratio="1:1", video_width=800, video_y=380)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "pad=1080:1920:140:380:" in fg


def test_video_y_clamp_uses_band_height():
    # 클램프 상한 = H - scaled_h — 밴드가 작아지면 더 아래까지 내려갈 수 있어야 한다
    d = DesignConfig(aspect_ratio="1:1", video_width=800, video_y=5000)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "pad=1080:1920:140:1120:" in fg               # 1920 - 800


# ── 영상영역 기준 계약 — 플랫폼 표기·제목 동적 배치·작품명 클램프 ─────────

def test_platform_left_follows_pad_x():
    d = DesignConfig(aspect_ratio="16:9", video_width=800, platform_text="티빙",
                     platform_x=24, platform_y=24)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    # 밴드 왼쪽 상단 = (pad_x=140, overlay_y=735) 기준 오프셋
    assert "x=164:y=759[with_pf]" in fg


def test_platform_right_text_follows_band_edge():
    d = DesignConfig(aspect_ratio="16:9", video_width=800, platform_text="티빙",
                     platform_align="right", platform_x=24)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    # 밴드 오른쪽 가장자리 = 140+800 → 캔버스 오른쪽에서 (1080-940)+24 = 164 안쪽
    assert "x=w-text_w-164:" in fg


def test_platform_right_text_default_width_unchanged():
    # 꽉 찬 폭이면 종전 문자열 그대로 — 회귀 가드
    d = DesignConfig(platform_text="티빙", platform_align="right", platform_x=24)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "x=w-text_w-24:" in fg


def test_platform_right_image_follows_band_edge(tmp_path):
    from PIL import Image
    logo = tmp_path / "pf.png"
    Image.new("RGBA", (300, 100), (255, 255, 255, 255)).save(logo)
    d = DesignConfig(aspect_ratio="16:9", video_width=800, platform_image=str(logo),
                     platform_align="right", platform_image_width=150,
                     platform_image_height=80, platform_x=24)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    # 150x50 contain → x = 140 + 800 - 150 - 24 = 766
    assert "overlay=766:" in fg


def test_title_dynamic_placement_follows_overlay_y():
    # overlay_y 파생이라 수식 변경 없음 — 밴드가 작아지면 제목이 따라 내려오는지만 확인
    d = DesignConfig(aspect_ratio="16:9", video_width=800)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    assert "y=645[title_0]" in fg                        # 735 - 70(제목 1줄) - 20(gap)


def test_work_clamp_follows_band_bottom():
    # 작품명 클램프 = overlay_y + scaled_h + 20 파생 — 밴드 하단을 따라오는지 확인
    d = DesignConfig(aspect_ratio="9:16", video_width=800)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    # scaled_h=int(800*16/9)=1422, overlay_y=249 → safe_top=1691 > work_title_y(1400)
    assert "y=1691[with_work]" in fg


# ── 자막 margin_v — 밴드 하단 추종 (E10 후속) ───────────────────────────

def test_subtitle_margin_default_unchanged():
    from app.pipeline import _compute_subtitle_margin_v
    # 1:1 꽉 찬 폭: scaled_h=1080, overlay_y=420, 밴드 하단 1500 → 1920-1500+10
    assert _compute_subtitle_margin_v(DesignConfig()) == 430
    # 16:9 꽉 찬 폭: scaled_h=607→606, overlay_y=657, 하단 1263 → 667 (종전 값 그대로)
    assert _compute_subtitle_margin_v(DesignConfig(aspect_ratio="16:9")) == 667
    assert _compute_subtitle_margin_v(DesignConfig(video_width=1080)) == 430


def test_subtitle_margin_follows_band_width():
    from app.pipeline import _compute_subtitle_margin_v
    # 밴드가 좁아지면 하단이 올라가고 자막도 따라 올라와야 한다(캔버스 기준이면 회귀)
    # 800×1:1: scaled_h=800, overlay_y=560, 하단 1360 → 1920-1360+10 = 570
    assert _compute_subtitle_margin_v(DesignConfig(aspect_ratio="1:1", video_width=800)) == 570
    # 800×16:9: scaled_h=450, overlay_y=735, 하단 1185 → 745 (렌더러 실측 밴드와 일치)
    assert _compute_subtitle_margin_v(DesignConfig(aspect_ratio="16:9", video_width=800)) == 745


def test_subtitle_margin_non_numeric_width_falls_back():
    from app.pipeline import _compute_subtitle_margin_v
    # 비숫자는 비율 파싱과 같은 관용으로 W 폴백 — 범위 검증은 렌더 경계가 즉시 실패시킨다
    assert _compute_subtitle_margin_v(DesignConfig(video_width="wide")) == 430


def test_subtitle_margin_follows_video_y():
    from app.pipeline import _compute_subtitle_margin_v
    # 렌더러와 같은 규약: 지정 위치 그대로 — 밴드를 올리면 자막도 밴드 하단을 따라 올라온다
    # 13:9, video_y=380: scaled_h=747→746, 하단 380+746=1126 → 1920-1126+10 = 804
    # (렌더러 test_video_y_moves_stack_up 의 pad y=380·짝수보정 746 과 같은 밴드)
    d = DesignConfig(aspect_ratio="13:9", video_y=380)
    assert _compute_subtitle_margin_v(d) == 804
    # video_width 와 결합: 800×1:1, video_y=380 → 하단 1180 → 750
    d = DesignConfig(aspect_ratio="1:1", video_width=800, video_y=380)
    assert _compute_subtitle_margin_v(d) == 750


def test_subtitle_margin_video_y_clamped():
    from app.pipeline import _compute_subtitle_margin_v
    # 클램프 상한 = H - scaled_h (렌더러와 동일): 밴드 하단이 캔버스 끝 → padding 하한
    d = DesignConfig(aspect_ratio="1:1", video_width=800, video_y=5000)
    assert _compute_subtitle_margin_v(d) == 10


def test_subtitle_margin_non_numeric_video_y_falls_back_to_center():
    from app.pipeline import _compute_subtitle_margin_v
    assert _compute_subtitle_margin_v(DesignConfig(video_y="abc")) == 430


# ── TTS 자막 margin_v — 밴드 앵커 델타 (E10 후속 3) ─────────────────────
# tts_line_y_margin 은 사용자 노브라 절대 재계산이 아니라 **종전 기하(꽉 찬 폭·세로
# 중앙) 대비 밴드 하단이 움직인 델타**로 따른다 — 밴드 하단으로부터의 오프셋 상수 유지.

def test_tts_margin_default_unchanged():
    from app.pipeline import _compute_tts_margin_v
    # video_width·video_y 미지정 = 델타 0 → 종전 값 그대로 (aspect_ratio 무관 — 회귀 0)
    assert _compute_tts_margin_v(DesignConfig()) == 580
    assert _compute_tts_margin_v(DesignConfig(aspect_ratio="16:9")) == 580
    assert _compute_tts_margin_v(DesignConfig(aspect_ratio="9:16")) == 580  # 꽉 찬 캔버스
    assert _compute_tts_margin_v(DesignConfig(video_width=1080)) == 580
    assert _compute_tts_margin_v(DesignConfig(tts_line_y_margin=400)) == 400


def test_tts_margin_follows_band_width():
    from app.pipeline import _compute_tts_margin_v
    # 800×1:1: 밴드 하단 1500 → 1360, 델타 140 → 580+140 = 720 (오프셋 상수 유지)
    assert _compute_tts_margin_v(DesignConfig(aspect_ratio="1:1", video_width=800)) == 720
    # 사용자 노브 유지: base 400 도 같은 델타로 이동
    assert _compute_tts_margin_v(
        DesignConfig(aspect_ratio="1:1", video_width=800, tts_line_y_margin=400)) == 540
    # 종전 꽉 찬 캔버스(9:16, 하단 1920=H 클램프) → 800 폭 밴드 하단 1671, 델타 249
    assert _compute_tts_margin_v(DesignConfig(aspect_ratio="9:16", video_width=800)) == 829


def test_tts_margin_follows_video_y():
    from app.pipeline import _compute_tts_margin_v
    # 13:9 + video_y=380: 종전 하단 587+746=1333 → 380+746=1126, 델타 207 → 787
    assert _compute_tts_margin_v(DesignConfig(aspect_ratio="13:9", video_y=380)) == 787


def test_tts_margin_clamped_at_zero():
    from app.pipeline import _compute_tts_margin_v
    # 밴드를 아래로 내리면 델타 음수 — 0 하한(build_tts_ass 가 음수를 '미지정' 480 으로
    # 해석하므로 음수를 내보내면 안 된다)
    d = DesignConfig(aspect_ratio="1:1", video_width=800, video_y=5000,
                     tts_line_y_margin=100)
    assert _compute_tts_margin_v(d) == 0  # 100 + (1500-1920) = -320 → 0


# ── 범위 검증 — CLI·렌더 경계 양쪽 즉시 실패 ────────────────────────────

def test_cli_flag_wired():
    assert _design("--design-video-width", "800").video_width == 800
    assert _design("--design-video-width", "320").video_width == 320
    assert _design("--design-video-width", "1080").video_width == 1080


def test_cli_out_of_range_fails_loud():
    with pytest.raises(ValueError):
        _design("--design-video-width", "319")
    with pytest.raises(ValueError):
        _design("--design-video-width", "1081")


def test_cli_non_numeric_fails_loud():
    with pytest.raises(SystemExit):                      # argparse type=int 즉시 실패
        _design("--design-video-width", "wide")


def test_renderer_rejects_out_of_range_too():
    # CLI 를 안 거친 호출(파이프라인 재개·videoApi·테스트)도 렌더 경계에서 즉시 실패
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(DesignConfig(video_width=319)), 1, 0)
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(DesignConfig(video_width=1081)), 1, 0)
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(DesignConfig(video_width="wide")), 1, 0)
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(DesignConfig(video_width=800.5)), 1, 0)
