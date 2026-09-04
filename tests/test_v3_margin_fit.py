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
