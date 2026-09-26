"""썸네일 후보 단계(app/tikitaka/thumbnail.py) — 순수 함수 회귀 가드(2026-09-26)."""
from pathlib import Path

import pytest

from app.tikitaka import thumbnail as T

FONTS = Path(__file__).resolve().parents[1] / "app" / "assets" / "fonts"


def test_parse_filter_extracts_clip_chains_and_brand():
    fg = ("[0:v]crop=1:1[v0];[1:v]crop=2:2[v1];[v0][v1]concat=n=2[vcat][acat];"
          "[vcat]drawtext=text='제목'[t];[t]overlay[with_cap];[with_cap]ass=x[vsub]")
    chains, brand = T.parse_filter(fg)
    assert chains == {0: "[0:v]crop=1:1[v0]", 1: "[1:v]crop=2:2[v1]"}
    assert brand == "[vcat]drawtext=text='제목'[t];[t]overlay[with_cap]"
    assert T.clean_graph(chains[1], 1, brand, "[with_cap]fps=2[out]") == \
        "[0:v]crop=2:2[vcat];" + brand + ";[with_cap]fps=2[out]"
    with pytest.raises(ValueError):
        T.parse_filter("[0:v]crop[v0]")


def test_band_rect_follows_design():
    assert T.band_rect({"aspect_ratio": "1:1", "video_y": 450}) == (450, 1080)
    assert T.band_rect({"aspect_ratio": "16:9"}) == ((1920 - 607) // 2, 607)
    assert T.band_rect({"aspect_ratio": "1:1", "video_y": 1500}) == (840, 1080)     # 캔버스 밖으로 안 나간다


def test_score_prefers_faces_and_shortlist_spreads_clips():
    assert T.score(300, 0.05, 3) > T.score(3000, 0.0, 5)                           # 얼굴 없는 선명한 컷보다 얼굴
    frames = [{"id": f"c0{c}_{k}", "clip": c, "clip_time_sec": k * 0.5, "score": s}
              for c, k, s in [(1, 0, .9), (1, 1, .89), (1, 3, .8), (1, 5, .7), (2, 0, .5)]]
    got = [f["id"] for f in T.shortlist(frames, n=10, per_clip=2)]
    assert got == ["c01_0", "c01_3", "c02_0"]                                       # 같은 클립 1초 안 표본·3번째는 뺀다


def test_parse_picks_validates_ids_colors_and_split():
    raw = {"picks": [{"id": 2, "style": "split", "parts": ["(긴", "장)"], "color": "sky"},
                     {"id": 2, "label": "(중복)"}, {"id": 9, "label": "(범위 밖)"},
                     {"id": 0, "label": "(아주아주아주아주아주 긴 라벨입니다요)", "color": "gold"},
                     {"id": 1, "style": "split", "parts": ["하나"], "label": "(한 줄)"}]}
    got = T.parse_picks(raw, n_cands=3)
    assert [p["id"] for p in got] == [2, 0, 1]
    assert got[0]["style"] == "split" and got[0]["parts"] == ["(긴", "장)"] and got[0]["label"] == "(긴장)"
    assert got[1]["label"] == "" and got[1]["color"] == "white"                     # 너무 긴 라벨·모르는 색
    assert got[2]["style"] == "line" and got[2]["parts"] == []                      # 반쪽짜리 split 은 한 줄로


def test_label_y_avoids_faces_and_platform_logo():
    band = (450, 1080)
    assert T.label_y([[400, 900, 200, 250]], band, 72) == 900 - T.LABEL_GAP - 36    # 얼굴 위 빈자리
    y = T.label_y([[400, 560, 300, 400]], band, 72)                                  # 위가 모자라면 아래
    assert y == 560 + 400 + T.LABEL_GAP + 36
    assert T.label_y([], band, 72) == 450 + T.LABEL_TOP_CLEAR                        # 얼굴 없으면 로고 아래


def test_split_layout_places_parts_beside_largest_face():
    font = FONTS / T.FONT_FILE
    lay = T.split_layout(["(긴", "장)"], [[440, 700, 200, 250], [100, 700, 50, 60]], font, x0=100, x1=980)
    assert lay and lay["left_x"] < 440 and lay["right_x"] > 640 and lay["y"] == int(700 + 250 * 0.45)
    assert T.split_layout(["(긴", "장)"], [[150, 700, 780, 700]], font, x0=100, x1=980) is None   # 옆자리 없음
    assert T.split_layout(["(긴장)"], [[440, 700, 200, 250]], font, x0=100, x1=980) is None


def test_neon_draws_glow_under_outlined_text():
    got = T.layers("neon", r"\an5\pos(540,700)", "한 잔만~").splitlines()
    assert len(got) == 2 and got[0].startswith("Dialogue: 0,") and got[1].startswith("Dialogue: 1,")
    assert r"\blur" in got[0] and r"\blur" not in got[1] and r"\bord5" in got[1]      # 글자 층은 선명한 테두리
    assert len(T.layers("white", r"\an5", "x").splitlines()) == 1                       # 일반 색은 한 층
