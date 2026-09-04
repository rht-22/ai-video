"""v3 자막/내레이션 블록을 밴드 아래로 — 2026-09-04 가왕쇼 7화(video_y 500 · tts_y_margin 550)
두 줄 내레이션 윗줄이 밴드에 겹친 실사고."""
from __future__ import annotations

from app.config import DesignConfig
from app.modules import subtitle_region as sr
from app.v3.finalize import LOGO_BOX_HEIGHT, LOGO_WIDTH, estimate_work_top, fit_margin_below_band

H = 1920
BAND_BOTTOM = 1246          # 13:9 · video_y 500 (band_geometry 실측)


def test_real_case_moves_tts_down_only_as_needed():
    blk = sr.estimate_subtitle_height(62)                 # 두 줄 기준
    new, note = fit_margin_below_band(550, canvas_height=H, band_bottom=BAND_BOTTOM,
                                      block_height=blk, work_top=1444)
    assert new == H - BAND_BOTTOM - 12 - blk               # 윗변이 밴드+12px
    assert new < 550 and "겹침" in note and "⚠" not in note   # 로고와는 안 부딪힘


def test_unchanged_when_already_clear():
    blk = sr.estimate_subtitle_height(60)
    new, note = fit_margin_below_band(500, canvas_height=H, band_bottom=BAND_BOTTOM,
                                      block_height=blk, work_top=1444)
    assert (new, note) == (500, None)                    # 이미 밴드 아래면 불변


def test_warns_when_logo_would_collide_but_keeps_band_clearance():
    blk = sr.estimate_subtitle_height(62)
    new, note = fit_margin_below_band(550, canvas_height=H, band_bottom=BAND_BOTTOM,
                                      block_height=blk, work_top=1380)
    assert new == H - BAND_BOTTOM - 12 - blk and "⚠" in note


def test_estimate_work_top_matches_renderer_log(tmp_path):
    # 실렌더 로그: [Logo] 2501x1127 → 620x278 @ y=1444 (video_y 500 · center)
    from PIL import Image
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (2501, 1127)).save(logo)
    d = DesignConfig(work_type="image", work_value=str(logo), work_image_width=LOGO_WIDTH,
                     work_image_height=LOGO_BOX_HEIGHT, work_image_align="center")
    assert estimate_work_top(d, band_bottom=BAND_BOTTOM, canvas_height=H) == 1444
    t = DesignConfig(work_type="text", work_title_y=1430)
    assert estimate_work_top(t, band_bottom=BAND_BOTTOM, canvas_height=H) == 1430
    assert estimate_work_top(t, band_bottom=1500, canvas_height=H) == 1520   # 밴드+20 안전선


# ── 밴드 기준 상대 앵커(범용) ──

def test_band_anchored_margin_is_ratio_independent():
    from app.v3.finalize import band_anchored_margin
    one = sr.estimate_subtitle_height(60, lines=1)
    # 같은 offset 이면 밴드가 어디 있든 '밴드 아래 30px' — 화면비마다 margin 을 안 잡는다
    for bb in (1246, 1187, 1477):
        m = band_anchored_margin(band_bottom=bb, offset=30, lines=1, font_size=60, canvas_height=H)
        assert H - m - one == bb + 30
    # 음수 = 밴드 안쪽(recap 처럼 영상 위에 얹는 채널)
    m = band_anchored_margin(band_bottom=1477, offset=-100, lines=1, font_size=60, canvas_height=H)
    assert H - m - one == 1377


def test_tts_cue_margins_follow_line_count():
    from app.v3.finalize import band_anchored_margin, tts_cue_margins
    caps = ["불안 속에 카운트다운이 시작되고", "무더운 날씨에도"]      # 두 줄 · 한 줄
    ms = tts_cue_margins(caps, band_bottom=BAND_BOTTOM, offset=24, font_size=62,
                         width=None, canvas_height=H)
    two = band_anchored_margin(band_bottom=BAND_BOTTOM, offset=24, lines=2, font_size=62, canvas_height=H)
    one = band_anchored_margin(band_bottom=BAND_BOTTOM, offset=24, lines=1, font_size=62, canvas_height=H)
    assert ms == [two, one] and one > two                 # 한 줄은 더 위(윗변이 같은 자리)
    assert tts_cue_margins(caps, band_bottom=BAND_BOTTOM, offset=24, font_size=62,
                           width=None, canvas_height=H, lift=40) == [two + 40, one + 40]


# ── 자막 스택 (2026-09-04 사용자 지시 "자막은 무조건 작품명보다 위 — 작품명도 같이 내려간다") ──
# 실사고: 지금불륜 EP01 bb71cb7d — recap 자막을 밴드 아래로 내리자(518→281) 자막 아랫변
# 1639 가 작품명 텍스트(1497~1553) 밑으로 들어갔다.

from app.v3.finalize import estimate_work_height, stack_work_below_text  # noqa: E402


def test_stack_pushes_work_title_below_text_real_case():
    sub, tts, top, notes = stack_work_below_text(
        sub_margin=281, tts_margin=276, work_top=1497, work_height=56, canvas_height=H)
    assert (sub, tts) == (281, 276)                     # 자막은 안 움직인다
    assert top == (H - 276) + 20 == 1664                # 내레이션 아랫변 1644 + 20
    assert notes and not any(n.startswith("⚠") for n in notes)


def test_stack_noop_when_work_already_below():
    sub, tts, top, notes = stack_work_below_text(
        sub_margin=518, tts_margin=580, work_top=1497, work_height=56, canvas_height=H)
    assert (sub, tts, top, notes) == (518, 580, None, [])   # 렌더 무변경


def test_stack_caps_text_when_no_room_for_logo():
    # 로고 300px: 자막 아랫변 1644 + 20 = 1664 > 한계 1920-20-300 = 1600
    sub, tts, top, notes = stack_work_below_text(
        sub_margin=281, tts_margin=276, work_top=1497, work_height=300, canvas_height=H)
    assert top == 1600                                  # 작품명은 하단 한계
    assert H - sub <= 1600 - 20 and H - tts <= 1600 - 20   # 자막·내레이션이 그 위로
    assert (sub, tts) == (340, 340)
    assert any(n.startswith("⚠") for n in notes)        # 밴드 안쪽으로 되돌아간 것은 ⚠


def test_estimate_work_top_honors_min_top_for_centered_logo(tmp_path):
    from PIL import Image
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (2501, 1127)).save(logo)
    d = DesignConfig(work_type="image", work_value=str(logo), work_image_width=LOGO_WIDTH,
                     work_image_height=LOGO_BOX_HEIGHT, work_image_align="center")
    assert estimate_work_height(d) == 278
    base = estimate_work_top(d, band_bottom=BAND_BOTTOM, canvas_height=H)      # 1444
    # 스택이 보장하는 범위 안의 하한(1600 + 278 ≤ 1900)에서 center 정렬이 그 아래 남은
    # 공간의 중앙에 앉는다 — 하한이 캔버스를 넘는 값은 stack_work_below_text 가 캡한다
    pushed = estimate_work_top(d, band_bottom=BAND_BOTTOM, canvas_height=H, min_top=1600)
    assert pushed >= 1600 > base and pushed + 278 <= H - 20
    t = DesignConfig(work_type="text", work_font_size=40)
    assert estimate_work_height(t) == 56
    assert estimate_work_top(t, band_bottom=1477, canvas_height=H, min_top=1656) == 1656


def test_renderer_work_min_top_none_is_byte_identical():
    from pathlib import Path
    from app.modules.renderer import RenderInputs, _build_filtergraph
    from app.modules.story_builder import StoryClip

    def _inputs(**kw):
        clip = StoryClip(role="hook", start_sec=0.0, end_sec=10.0, subtitle="s",
                         use_original_audio=True)
        return RenderInputs(
            video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
            crop_timeline_map={}, title_text="제목", work_title="지금불륜이문제가아닙니다",
            output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
            top_title_height=250, bottom_label_height=170,
            design=DesignConfig(aspect_ratio="24:23", video_y=443), **kw)

    a = _build_filtergraph(_inputs(), 1, 0)
    b = _build_filtergraph(_inputs(work_min_top=None), 1, 0)
    assert a == b and ":y=1497[with_work]" in a          # 밴드 하단 1477 + 20
    c = _build_filtergraph(_inputs(work_min_top=1664), 1, 0)
    assert ":y=1664[with_work]" in c
    assert c.replace(":y=1664[with_work]", ":y=1497[with_work]") == a   # 그 외 바이트 동일


# ── 프리셋 margin 은 밴드 상대(2026-09-04 "대사도 내레이션도 영상 위에 얹혀도 된다 — v1 참고") ──

from app.v3.finalize import design_from_style, preset_relative_margin  # noqa: E402
from app.v3 import stage4  # noqa: E402


def test_preset_relative_margin_identity_on_same_band():
    assert preset_relative_margin(518, ref_band_bottom=1477, band_bottom=1477) == 518


def test_preset_relative_margin_follows_band():
    # 밴드가 100px 올라가면(하단 1377) 자막도 100px 올라간다 — 밴드 하단 75px 위 유지
    assert preset_relative_margin(518, ref_band_bottom=1477, band_bottom=1377) == 618
    assert preset_relative_margin(518, ref_band_bottom=1477, band_bottom=1577) == 418
    assert preset_relative_margin(50, ref_band_bottom=1000, band_bottom=1900) == 1   # 하한


def test_recap_preset_margins_sit_inside_band():
    d = design_from_style(stage4.get_style_preset(None))
    g = sr.band_geometry(d, canvas_width=1080, canvas_height=1920)
    assert g.bottom == 1477                                  # 24:23 · video_y 443
    assert H - d.subtitle_y_margin == 1402 < g.bottom        # 대사 아랫변: 밴드 하단 75px 위
    assert H - d.tts_line_y_margin == 1340 < g.bottom        # 내레이션 아랫변: 137px 위


# ── work_band_offset — 작품명/로고 블록 윗변 = 밴드 하단 + N (2026-09-04 사용자 요청) ──

def test_work_band_offset_anchors_text_and_logo_to_band(tmp_path):
    from PIL import Image
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (4052, 1279)).save(logo)
    d = DesignConfig(work_type="image", work_value=str(logo), work_image_width=LOGO_WIDTH,
                     work_image_height=LOGO_BOX_HEIGHT, work_image_align="center",
                     work_band_offset=60)
    assert estimate_work_top(d, band_bottom=1477, canvas_height=H) == 1477 + 60   # center 무시
    t = DesignConfig(work_type="text", work_band_offset=60)
    assert estimate_work_top(t, band_bottom=1477, canvas_height=H) == 1537
    # 미지정 = 종전(밴드+20 · center)
    d0 = DesignConfig(work_type="image", work_value=str(logo), work_image_width=LOGO_WIDTH,
                      work_image_height=LOGO_BOX_HEIGHT, work_image_align="center")
    assert estimate_work_top(d0, band_bottom=1477, canvas_height=H) > 1497


def test_renderer_work_band_offset_moves_text_work_title():
    from pathlib import Path
    from app.modules.renderer import RenderInputs, _build_filtergraph
    from app.modules.story_builder import StoryClip

    def _inputs(d):
        clip = StoryClip(role="hook", start_sec=0.0, end_sec=10.0, subtitle="s",
                         use_original_audio=True)
        return RenderInputs(
            video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
            crop_timeline_map={}, title_text="제목", work_title="지금불륜이문제가아닙니다",
            output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
            top_title_height=250, bottom_label_height=170, design=d)
    a = _build_filtergraph(_inputs(DesignConfig(aspect_ratio="24:23", video_y=443)), 1, 0)
    b = _build_filtergraph(_inputs(DesignConfig(aspect_ratio="24:23", video_y=443,
                                                work_band_offset=60)), 1, 0)
    assert ":y=1497[with_work]" in a and ":y=1537[with_work]" in b
    assert b.replace(":y=1537[with_work]", ":y=1497[with_work]") == a
