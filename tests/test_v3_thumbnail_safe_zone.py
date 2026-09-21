"""썸네일 안전 구역 맞춤(2026-09-11, 사용자 지시 "썸네일 안에 제목/영상화면/작품 로고가 다 들어오게").

쇼츠 피드·채널 탭 썸네일은 2:3 가운데 크롭(캔버스 약 148~1770)이다. 지금불륜 2화 2편은 작품 로고가
1831 까지 내려가 잘렸다. `finalize.fit_thumbnail_safe_zone` 이 제목 윗변 ≥ 180 · 하단 블록 아랫변
≤ 1715 가 되게 ①로고 빈 공간 걷기 → ②밴드 옮기기 → ③로고 축소 → ④제목 상한 축소 순으로 고친다.
경계는 사용자가 기준으로 지목한 가왕쇼 템플릿(187 · 1711)에 맞췄다.
"""
from __future__ import annotations

import dataclasses as dc
import json
from pathlib import Path

import pytest

from app.config import DesignConfig
from app.v3 import finalize as F
from app.v3 import stage4

ROOT = Path(__file__).resolve().parents[1]


def _logo(tmp_path: Path, w: int = 1240, h: int = 388) -> str:
    from PIL import Image
    p = tmp_path / "logo.png"
    Image.new("RGBA", (w, h), (255, 255, 255, 255)).save(p)
    return str(p)


def _jigeum_like(tmp_path: Path, **kw) -> DesignConfig:
    """24:23 밴드 @443 · 제목 상한 92/112 · 작품명 위 플랫폼 줄 · 620×194 로고 가운데 정렬."""
    d = F.design_from_style(stage4.get_style_preset(None))
    return dc.replace(d, work_type="image", work_value=_logo(tmp_path),
                      work_image_width=F.LOGO_WIDTH, work_image_height=F.LOGO_BOX_HEIGHT,
                      work_image_align="center", platform_placement="above_work",
                      platform_text="지금 쿠팡플레이에서 무료로 시청하세요", **kw)


def test_inside_design_is_returned_untouched(tmp_path):
    # 로고 없음 — 작품명 텍스트. 2026-09-18 윗변 기준이 200 으로 올라 프리셋 그대로(189)는 밖이다 → 밴드를 내려 안에 둔다
    d = dc.replace(F.design_from_style(stage4.get_style_preset(None)), video_y=460)
    e = F.layout_extents(d)
    assert e["title_top"] >= F.THUMB_SAFE_TOP and e["bottom"] <= F.THUMB_SAFE_BOTTOM
    out, info = F.fit_thumbnail_safe_zone(d)
    assert out is d                                                # 회귀 0 — 객체 그대로
    assert info["actions"] == [] and info["unmet"] is None and info["band_shift"] == 0


def test_jigeum_like_logo_is_pulled_in(tmp_path):
    """실사고 재현: 로고가 밴드 아래 남은 공간 가운데(1565~1831)에 앉아 썸네일에서 잘렸다."""
    d = _jigeum_like(tmp_path)
    e0 = F.layout_extents(d)
    assert e0["bottom"] > F.THUMB_SAFE_BOTTOM                      # 1831 — 잘리던 배치
    out, info = F.fit_thumbnail_safe_zone(d)
    a = info["after"]
    assert a["title_top"] >= F.THUMB_SAFE_TOP - F.SAFE_TOP_TOLERANCE_PX and a["bottom"] <= F.THUMB_SAFE_BOTTOM
    assert info["unmet"] is None
    # ① 가운데 정렬 해제(밴드 +20) → ③ 로고 축소 → ② 밴드 이동, 제목은 안 건드린다
    # (2026-09-18: 윗변 200 — 제목 여유가 없어 밴드는 아래로 간다)
    assert out.work_band_offset == F.WORK_GAP_BELOW_VIDEO
    assert info["band_shift"] == out.video_y - 443
    assert out.work_image_height < F.LOGO_BOX_HEIGHT
    assert F._work_item_height(out) >= int(194 * F.LOGO_MIN_SCALE)
    assert list(out.title_sizes) == list(d.title_sizes)
    # 렌더러와 같은 짝수 로고 높이 — 올려 반올림하면 1px 넘쳐 제목까지 줄였다(실측)
    assert F._work_item_height(out) % 2 == 0


def test_top_short_moves_band_down_when_bottom_has_room(tmp_path):
    d = dc.replace(F.design_from_style(stage4.get_style_preset(None)), video_y=300)
    e0 = F.layout_extents(d)
    assert e0["title_top"] < F.THUMB_SAFE_TOP and e0["bottom"] <= F.THUMB_SAFE_BOTTOM
    out, info = F.fit_thumbnail_safe_zone(d)
    assert info["after"]["title_top"] >= F.THUMB_SAFE_TOP - F.SAFE_TOP_TOLERANCE_PX
    assert out.video_y > 300 and list(out.title_sizes) == list(d.title_sizes)


def test_title_caps_shrink_only_when_band_cannot_move(tmp_path):
    """위아래 둘 다 꽉 차면 제목 **상한**을 줄이고 그 여유만큼 밴드를 올린다."""
    d = _jigeum_like(tmp_path, video_width=1080)
    d = dc.replace(d, aspect_ratio="1:1")                          # 밴드 1080 — 로고 하한까지 줄여도 모자람
    out, info = F.fit_thumbnail_safe_zone(d)
    assert any("제목 크기 상한" in a for a in info["actions"])
    assert all(s >= int(o * F.TITLE_MIN_SCALE) for s, o in zip(out.title_sizes, d.title_sizes))
    assert out.title_size == out.title_sizes[0]


def test_impossible_layout_reports_unmet_instead_of_failing(tmp_path):
    d = _jigeum_like(tmp_path)
    d = dc.replace(d, aspect_ratio="9:12", work_image_height=None)   # 밴드 1440 — 들어갈 수 없다
    out, info = F.fit_thumbnail_safe_zone(d)
    assert info["unmet"] and (info["unmet"]["bottom_over"] or info["unmet"]["top_short"])


def test_title_block_top_matches_renderer_formula():
    """제목 윗변 = 밴드 윗변 − 20 − (줄 크기 합 + 줄 간격 30) — renderer _dynamic_title_top."""
    from app.modules import subtitle_region as sr
    d = dc.replace(F.design_from_style(stage4.get_style_preset(None)), title_sizes=[92, 112])
    g = sr.band_geometry(d)
    top, bottom = sr.estimate_title_block(d, g, line_count=2)
    assert top == g.overlay_y - 20 - (92 + 112 + 30)
    assert bottom == sr.estimate_title_bottom(d, g, line_count=2) == g.overlay_y - 20


def test_base_text_margins_keeps_render_final_rules():
    """render_final 에서 떼어 낸 기준 margin 규칙 — 프리셋 밴드 상대 · 내레이션 = 대사 줄 · 밴드 앵커."""
    from app.modules import subtitle_region as sr
    d = F.design_from_style(stage4.get_style_preset(None))
    g = sr.band_geometry(d)
    sub, tts, base, _ = F.base_text_margins(d, channel_design=None, geom=g, ref_geom=g, work_top=1565)
    assert sub == int(d.subtitle_y_margin) and tts == sub and base == sub
    moved = dc.replace(d, video_y=int(g.overlay_y) - 9)             # 밴드가 9px 위로 → 자막도 9px 위로
    g2 = sr.band_geometry(moved)
    sub2, _t, _b, notes = F.base_text_margins(moved, channel_design=None, geom=g2, ref_geom=g,
                                              work_top=1565)
    assert sub2 == sub + 9 and any("프리셋 밴드 하단" in n for n in notes)
    sub3, tts3, _b3, _n3 = F.base_text_margins(
        d, channel_design={"subtitle_band_offset": 30, "tts_band_offset": 24}, geom=g, ref_geom=g,
        work_top=1565)
    assert sub3 == F.band_anchored_margin(band_bottom=g.bottom, offset=30, lines=1,
                                          font_size=d.subtitle_size)
    assert tts3 == F.band_anchored_margin(band_bottom=g.bottom, offset=24, lines=2,
                                          font_size=d.tts_line_font_size)


def test_gawangsho_reference_bottom_is_inside():
    """사용자 기준(가왕쇼 템플릿) — 하단 캡션 블록 아랫변 1711 이 경계 안이어야 한다."""
    logo = F.resolve_work_logo("가왕쇼")
    if logo is None:
        pytest.skip("가왕쇼 로고 에셋 없음")
    cd = json.loads((ROOT / "app/data/channel_designs/gawangsho.json").read_text(encoding="utf-8"))["design"]
    d = F.design_from_style(F.channel_design_over_ai(
        F.merge_channel_preset(stage4.get_style_preset(None), cd), cd))
    d = dc.replace(d, work_type="image", work_value=str(logo), work_image_width=F.LOGO_WIDTH,
                   work_image_height=F.LOGO_BOX_HEIGHT, work_image_align="center")
    e = F.layout_extents(d, channel_design=cd)
    assert e["band_top"] == 420 and e["bottom"] <= F.THUMB_SAFE_BOTTOM


def test_render_wiring():
    """render_final 이 맞춤을 부르고 라벨을 밴드와 같이 옮기며, 렌더 지문에 경계가 들어간다."""
    src = (ROOT / "app/v3/finalize.py").read_text(encoding="utf-8")
    body = src[src.index("def render_final("):]
    assert body.index("fit_thumbnail_safe_zone(") < body.index("fit_title_sizes(")   # 상한으로 먼저 잰다
    assert body.count("+ _label_dy") == 2
    assert '"thumb_safe"' in body
    pipe = (ROOT / "app/v3/pipeline.py").read_text(encoding="utf-8")
    assert "finalize.THUMB_SAFE_TOP" in pipe and "finalize.THUMB_SAFE_BOTTOM" in pipe
