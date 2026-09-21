"""폰 재생 화면 안전 박스(2026-09-18 사용자 결정) — 회귀 가드.

가왕쇼 8화 어시장 편 아이폰 스크린샷 ↔ 완성본 실측: 재생 화면이 좌우 약 47px 을 잘라 「티빙」의 「빙」이 잘렸다.
결정: 글자는 가로 100~980 · 제목은 **글자 크기 고정, 글자 수를 줄인다** · 영상 밴드는 그대로, 인물이 가장자리에
걸리면 경고. 윗변 200(상단 아이콘) — 로고를 먼저 줄여 제목 크기를 지킨다.
"""
from __future__ import annotations

import dataclasses as dc
from pathlib import Path

from app.v3 import finalize as F
from app.v3 import safe_zone as sz
from app.v3 import stage4
from app.v3.story_flow import select as sl

FONT = str(Path(__file__).resolve().parents[1] / "app" / "assets" / "fonts" / "JalnanGothic.ttf")
FIT = {"font": FONT, "sizes": [92, 112]}


def test_safe_box_numbers():
    assert (sz.SAFE_X0, sz.SAFE_X1, sz.SAFE_TOP) == (100, 980, 200)
    assert F.TITLE_MAX_WIDTH == 880 and F.THUMB_SAFE_TOP == 200


def test_title_overflow_measures_at_fixed_size():
    over = sz.title_overflow(["인천 어시장 찾은 전유진", "통로 한복판 뒤집어놓은 사연"], FONT, [92, 112])
    assert [o["line"] for o in over] == [1, 2]                    # 둘 다 넘친다(998 · 1439px)
    assert over[1]["cut_chars"] >= 5
    assert sz.title_overflow(["돈가방에 추적기 심고", "택배로 위장 잠입"], FONT, [92, 112]) == []
    assert sz.title_char_budget(92) == 9 and sz.title_char_budget(112) == 7


def test_scene_validator_rejects_wide_title_only_with_fit():
    rows = [{"idx": i, "content": "x", "importance": 3, "t_in": i * 10.0, "t_out": i * 10.0 + 9} for i in range(4)]
    resp = {"scenes": [{"meaning": "m000", "purpose": "배경"}, {"meaning": "m001", "purpose": "과정"}],
            "title": {"line1": "협박범이 부른 값 5억", "line2": "뺄 수 있는 건 3억뿐"}}
    ok, probs, _ = sl.validate_scenes(resp, rows)                  # 폭 판정 없음 = 종전(글자 수만)
    assert ok is not None
    bad, probs, _ = sl.validate_scenes(resp, rows, title_fit=FIT)
    assert bad is None and any("안전 폭 880px" in p and "line2" in p for p in probs)


def test_title_rule_prompt_unchanged_without_fit():
    assert sl.title_len_rule(12, None) == "각 줄 7~12자(공백 포함) — 짧을수록 좋다."
    r = sl.title_len_rule(12, FIT)
    assert "9자" in r and "7자" in r and "글자 크기는 고정" in r
    assert "{title_len_rule}" in sl.SCENES_PROMPT and "{title_max}" not in sl.SCENES_PROMPT


def test_top_edge_shrinks_logo_before_title(tmp_path):
    """윗변 200 으로 제목이 모자라면 로고를 먼저 줄이고 밴드를 내린다 — 제목 크기는 그대로."""
    from PIL import Image
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (1240, 600), (255, 255, 255, 255)).save(logo)
    d = dc.replace(F.design_from_style(stage4.get_style_preset(None)), video_y=420, aspect_ratio="13:9",
                   work_type="image", work_value=str(logo), work_image_width=F.LOGO_WIDTH,
                   work_image_height=F.LOGO_BOX_HEIGHT, work_band_offset=40)
    out, info = F.fit_thumbnail_safe_zone(d)
    assert list(out.title_sizes) == list(d.title_sizes)
    assert info["after"]["title_top"] >= F.THUMB_SAFE_TOP - F.SAFE_TOP_TOLERANCE_PX
    assert info["after"]["bottom"] <= F.THUMB_SAFE_BOTTOM and info["unmet"] is None


def test_label_x_range_keeps_text_inside_safe_box():
    for text in ("(경악)", "(단돈 천만원 입금)", "(단호한 거절)"):
        lo, hi = stage4.label_x_range(text)
        half = (len(text) * stage4.LABEL_SIZE * stage4.LABEL_CHAR_W * stage4.LABEL_FX_OVERSHOOT) / 2 \
            + stage4.LABEL_SIZE * 0.07
        assert lo * 1080 - half >= sz.SAFE_X0 - 0.5 and hi * 1080 + half <= sz.SAFE_X1 + 0.5


def test_emphasis_scale_clamped_to_safe_width():
    notes: list[str] = []
    segs = [{"text": "은행에서 3억밖에 인출 안 될 줄", "start_sec": 0.0, "end_sec": 1.0}]
    style = {"emphasis": [{"index": 0, "line": "L0", "scale": 1.45, "text": segs[0]["text"], "start_sec": 0.0}]}
    out = F.emphasis_styles(style, 90, segs, font_path=FONT, em_ratio=1.45, notes=notes)
    assert out[0]["size"] < round(90 * 1.45) and notes
    plain = F.emphasis_styles(style, 90, segs)                     # 폰트 미지정 = 종전
    assert plain[0]["size"] == round(90 * 1.45)


def test_edge_cut_ratio():
    assert sz.edge_cut_ratio(20, 120) == 0.8                       # 왼쪽 띠에 80px 들어감
    assert sz.edge_cut_ratio(400, 600) == 0.0
    assert abs(sz.edge_cut_ratio(950, 1050) - 0.7) < 1e-9


def test_centered_overlays_are_held_inside_safe_width():
    """오른쪽 마지노선(2026-09-18 "다른 로고·글씨들도") — 가운데 정렬 요소도 가로 880px 안."""
    from app.config import DesignConfig
    d = dc.replace(DesignConfig(), title_font=FONT, work_type="image", work_value="x.png",
                   work_image_width=1000, work_caption="아주 아주 긴 로고 아래 문구가 화면 끝까지 넘친다니까요 정말",
                   work_caption_font_size=48)
    out, notes = F.enforce_safe_box(d)
    assert out.work_image_width == sz.SAFE_WIDTH
    from app.modules.renderer import _measure_title_text_width as W
    assert W(out.work_caption, FONT, out.work_caption_font_size) <= sz.SAFE_WIDTH and len(notes) == 2
    same, n2 = F.enforce_safe_box(dc.replace(DesignConfig(), title_font=FONT))
    assert n2 == []                                                # 넘치는 게 없으면 그대로


def test_gawangsho_template_uses_tving_logo_inside_right_limit():
    from app.v3.cli import load_design_preset
    des = load_design_preset("gawangsho")["design"]
    assert des["platform_image"] == "tving_logo" and "platform_text" not in des
    assert (Path(__file__).resolve().parents[1] / "app" / "assets" / "logos" / "tving_logo.png").is_file()
    assert des["platform_image_width"] <= sz.SAFE_WIDTH
