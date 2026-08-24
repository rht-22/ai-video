"""E17-2 (2026-08-24) — 소스에 박힌 원본 자막 회피 회귀 가드.

사용자 지시: "영상에 원래 자막이 있으면 그 위치 피해서 자막이 들어가게 해줘.
물론, 자막이 제목과도 겹치면 안되고."

지키는 것 넷:

1. **밴드 기하가 렌더러와 같다** — 검출한 행을 캔버스 y 로 옮기는 유일한 다리다.
2. **판정은 순수 함수** — ffmpeg 없이 합성 프레임으로 검증한다(이 컨테이너에 ffmpeg 가
   없어도 돌아야 한다).
3. **올리기만 하고, 제목 아래를 안 넘는다.** 못 피하면 갈 수 있는 데까지 가고 남긴다.
4. **못 찾거나 꺼져 있으면 아무것도 안 한다**(회귀 0) — 안전장치지 연출이 아니다.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import DesignConfig
from app.modules import subtitle_region as sr


# ──────────────────────────────────────────────────────────────
# 1. 밴드 기하 — 렌더러·파이프라인과 어긋나면 안 된다
# ──────────────────────────────────────────────────────────────

_DESIGNS = [
    DesignConfig(),
    DesignConfig(aspect_ratio="16:9"),
    DesignConfig(aspect_ratio="1:1", video_width=800),
    DesignConfig(aspect_ratio="13:9", video_y=440),
    DesignConfig(aspect_ratio="13:9", video_width=1080, video_y=380),
]


@pytest.mark.parametrize("d", _DESIGNS)
def test_band_geometry_matches_pipeline_within_one_px(d):
    """`pipeline._video_band_bottom` 과 같은 수식이다.

    ⚠ 비교 대상은 `legacy_center=False`, 즉 **실제로 렌더되는 밴드**다. E10 은 자막
    margin 만 'video_width 미지정이면 세로 중앙 가정'으로 계산하는데(픽셀로 튜닝된 기존
    채널의 승인 출력 보존), 그건 자막 위치 규약이지 화면의 밴드가 아니다. 검출은 화면을
    재는 것이라 언제나 실제 밴드를 본다.

    ⚠ 1px 오차를 허용하는 이유: 두 구현의 **짝수 보정 순서**가 다르다. 렌더러는 video_y
    클램프를 홀수 높이로 하고 그 뒤에 짝수 보정하며(이 모듈이 그쪽을 따른다),
    `_video_band_bottom` 은 보정을 먼저 한다. 검출은 밴드를 270행으로 줄여 재므로
    1px 은 판정에 영향이 없다.
    """
    from app.pipeline import _video_band_bottom

    got = sr.band_geometry(d).bottom
    ref = _video_band_bottom(d, legacy_center=False)
    assert abs(got - ref) <= 1


def test_band_geometry_defaults_to_full_width_centered():
    g = sr.band_geometry(DesignConfig())
    assert (g.scaled_w, g.scaled_h) == (1080, 1080)
    assert g.top == 420 and g.bottom == 1500 and g.pad_x == 0


def test_band_geometry_tolerates_garbage():
    """비숫자는 렌더 경계가 즉시 실패시킨다 — 검출은 그 앞이라 폴백만 하면 된다."""
    g = sr.band_geometry(DesignConfig(video_width="wide", video_y="abc"))
    assert g.scaled_w == 1080 and g.top == 420


# ──────────────────────────────────────────────────────────────
# 2. 행 점수·띠 판정 (순수)
# ──────────────────────────────────────────────────────────────


def _frame(width, height, text_rows, *, period=4):
    """text_rows 구간에만 '흰 글자 + 검은 외곽선' 모양(밝기 교대)을 넣은 회색조 프레임."""
    buf = bytearray(b"\x40" * (width * height))       # 균일한 어두운 배경
    for y in text_rows:
        for x in range(width):
            buf[y * width + x] = 250 if (x // period) % 2 == 0 else 10
    return bytes(buf)


def test_row_edge_counts_finds_text_rows_only():
    w, h = 40, 10
    counts = sr.row_edge_counts(_frame(w, h, range(4, 7)), w, h)
    assert all(c == 0 for i, c in enumerate(counts) if i not in (4, 5, 6))
    assert all(counts[i] >= w // 4 - 1 for i in (4, 5, 6))   # 40px/4 주기 = 경계 9개


def test_row_hit_ratios_counts_persistence():
    w, h = 40, 10
    frames = [_frame(w, h, range(4, 7)), _frame(w, h, range(4, 7)), _frame(w, h, [])]
    ratios = sr.row_hit_ratios(frames, w, h)
    assert ratios[5] == pytest.approx(2 / 3)
    assert ratios[0] == 0.0


def test_band_from_ratios_picks_lowest_run_and_merges_gaps():
    h = 100
    ratios = [0.0] * h
    for y in range(55, 62):
        ratios[y] = 0.9                      # 위쪽 텔롭
    for y in list(range(80, 84)) + list(range(88, 93)):
        ratios[y] = 0.8                      # 아래 2줄 자막(사이 4행 빈틈)
    band = sr.band_from_ratios(ratios)
    assert band == (80, 93)                  # 빈틈을 잇고, 더 아래 것을 고른다


def test_band_from_ratios_ignores_upper_half_and_thin_noise():
    h = 100
    ratios = [0.0] * h
    for y in range(10, 20):
        ratios[y] = 1.0                      # 상단 제목권 — 우리 자막과 안 겹친다
    ratios[70] = 1.0                         # 한 행짜리 잡티
    assert sr.band_from_ratios(ratios) is None


def test_band_from_ratios_rejects_scene_sized_run():
    """밴드의 절반을 채우는 것은 자막이 아니라 장면이다."""
    ratios = [0.0] * 100
    for y in range(50, 100):
        ratios[y] = 1.0
    assert sr.band_from_ratios(ratios) is None


# ──────────────────────────────────────────────────────────────
# 3. 배치 — 올리기만, 제목 아래로만
# ──────────────────────────────────────────────────────────────


def test_no_overlap_keeps_margin_exactly():
    """겹치지 않으면 한 픽셀도 안 움직인다(회귀 0)."""
    got, notes = sr.avoid_margin_v(
        430, canvas_height=1920, burned_top=800, burned_bottom=860,
        subtitle_height=160, title_bottom=400, band_top=420)
    assert got == 430 and notes == []


def test_overlap_moves_subtitle_above_the_burned_band():
    # 자막 아래끝 1920-430=1490, 위끝 1330 → 띠(1350~1420)와 겹친다
    got, notes = sr.avoid_margin_v(
        430, canvas_height=1920, burned_top=1350, burned_bottom=1420,
        subtitle_height=160, title_bottom=400, band_top=420)
    assert got == 1920 - (1350 - sr.GAP_PX)          # 아래끝을 띠 위 GAP_PX 로
    assert any("회피" in n for n in notes)
    assert (1920 - got) - 160 >= 400 + sr.GAP_PX     # 제목과도 안 겹친다


def test_title_floor_wins_and_shortfall_is_reported():
    """다 못 피하면 제목을 우선하고 **모자란 양을 남긴다**(조용한 포기 금지)."""
    got, notes = sr.avoid_margin_v(
        430, canvas_height=1920, burned_top=700, burned_bottom=1500,
        subtitle_height=160, title_bottom=600, band_top=420)
    top = (1920 - got) - 160
    assert top == 600 + sr.GAP_PX
    assert any("미달" in n for n in notes)


def test_never_moves_down():
    """띠가 자막보다 아래에서 시작하면 내려가지 않는다 — 로고·작품명 스택이 있다."""
    got, notes = sr.avoid_margin_v(
        430, canvas_height=1920, burned_top=1480, burned_bottom=1500,
        subtitle_height=160, title_bottom=1400, band_top=420)
    assert got == 430
    assert any("그대로" in n for n in notes)


def test_title_bottom_estimate_follows_the_band():
    """제목은 밴드 위 20px 에 동적 배치된다 — 밴드가 내려가면 제목도 내려간다."""
    d = DesignConfig()
    g = sr.band_geometry(d)
    assert sr.estimate_title_bottom(d, g, line_count=2) == g.top - 20
    d2 = DesignConfig(video_y=440)
    g2 = sr.band_geometry(d2)
    assert sr.estimate_title_bottom(d2, g2, line_count=2) == g2.top - 20


def test_title_bottom_uses_title_y_when_pinned():
    """편집실 제목 드래그(title_y_fixed)면 그 절대 좌표가 기준이다."""
    d = DesignConfig(title_y_fixed=True, title_y=300, title_sizes=[70, 90])
    g = sr.band_geometry(d)
    assert sr.estimate_title_bottom(d, g, line_count=2) == 300 + 70 + 90 + 30


def test_title_line_count_counts_wrapped_lines():
    assert sr.estimate_title_line_count("짧은 줄\n둘째 줄") == 2
    assert sr.estimate_title_line_count("가" * 30) >= 3
    assert sr.estimate_title_line_count("") == 1


def test_subtitle_height_uses_two_lines():
    assert sr.estimate_subtitle_height(65) == round(65 * 1.25 * 2)


# ──────────────────────────────────────────────────────────────
# 4. 검출 글루 — 표본 주입으로 ffmpeg 없이
# ──────────────────────────────────────────────────────────────


def _clip(i):
    return SimpleNamespace(role="main", start_sec=10.0 * i, end_sec=10.0 * i + 8.0)


def _sampler_with_band(rows):
    def _s(clip, crop_path):
        return [_frame(sr.PROBE_W, sr.PROBE_H, rows) for _ in range(sr.FRAMES_PER_CLIP)]
    return _s


def test_detect_maps_rows_to_canvas_y():
    clips = [_clip(i) for i in range(3)]
    band = sr.detect_burned_band(Path("x.mp4"), clips, DesignConfig(),
                                 sampler=_sampler_with_band(range(230, 245)))
    assert band is not None
    # 1:1 기본 밴드: top=420, 높이 1080 → 행 하나가 4px
    assert band["top"] == 420 + int(230 * 1080 / sr.PROBE_H)
    assert band["bottom"] == 420 + int(245 * 1080 / sr.PROBE_H)
    assert band["frames"] == 3 * sr.FRAMES_PER_CLIP


def test_detect_returns_none_without_enough_frames():
    band = sr.detect_burned_band(Path("x.mp4"), [_clip(0)], DesignConfig(),
                                 sampler=lambda c, p: [])
    assert band is None


def test_detect_survives_a_broken_sampler():
    """검출 실패가 본편을 막으면 안 된다 — 예외를 삼키고 None."""
    def _boom(clip, crop_path):
        raise RuntimeError("ffmpeg 없음")

    assert sr.detect_burned_band(Path("x.mp4"), [_clip(0), _clip(1)],
                                 DesignConfig(), sampler=_boom) is None


def test_detect_returns_none_when_nothing_persists():
    """장면마다 다른 자리에 밝은 무늬가 있으면 띠가 아니다."""
    seq = [range(200, 210), range(240, 250), range(180, 190)]
    state = {"i": 0}

    def _s(clip, crop_path):
        rows = seq[state["i"] % len(seq)]
        state["i"] += 1
        return [_frame(sr.PROBE_W, sr.PROBE_H, rows) for _ in range(sr.FRAMES_PER_CLIP)]

    assert sr.detect_burned_band(Path("x.mp4"), [_clip(i) for i in range(3)],
                                 DesignConfig(), sampler=_s) is None


# ──────────────────────────────────────────────────────────────
# 5. 파이프라인 배선 — 게이트·캐시
# ──────────────────────────────────────────────────────────────


def test_off_switch_skips_detection(tmp_path, monkeypatch):
    """`off` 면 재지도 않는다 — 사람이 픽셀로 맞춘 채널의 탈출구."""
    from app import pipeline

    def _boom(*a, **k):
        raise AssertionError("off 인데 검출이 돌았다")

    monkeypatch.setattr(sr, "detect_burned_band", _boom)
    payload = SimpleNamespace(design=DesignConfig(subtitle_avoid_burned="off"),
                              video_path=tmp_path / "v.mp4")
    cfg = SimpleNamespace(canvas_width=1080, canvas_height=1920)
    assert pipeline._detect_burned_band_cached(payload, cfg, [_clip(0)], {}, tmp_path) is None


def test_detection_is_cached_and_invalidated_by_clip_change(tmp_path, monkeypatch):
    """재렌더마다 다시 재면 같은 편의 자막 위치가 실행마다 달라진다(E15 규약과 같은 이유)."""
    from app import pipeline

    calls = []

    def _fake(video, clips, design, **kw):
        calls.append(len(clips))
        return {"top": 1300, "bottom": 1380, "frames": 18, "clips": 3, "hit_ratio": 0.9}

    monkeypatch.setattr(sr, "detect_burned_band", _fake)
    payload = SimpleNamespace(design=DesignConfig(), video_path=tmp_path / "v.mp4")
    cfg = SimpleNamespace(canvas_width=1080, canvas_height=1920)
    clips = [_clip(0), _clip(1)]
    a = pipeline._detect_burned_band_cached(payload, cfg, clips, {}, tmp_path)
    b = pipeline._detect_burned_band_cached(payload, cfg, clips, {}, tmp_path)
    assert a == b and len(calls) == 1
    assert json.loads((tmp_path / "checkpoint_burned_subtitle.json")
                      .read_text(encoding="utf-8"))["band"]["top"] == 1300
    pipeline._detect_burned_band_cached(payload, cfg, clips + [_clip(2)], {}, tmp_path)
    assert len(calls) == 2               # 구간이 바뀌면 표본을 뜬 화면 자체가 다르다


def test_no_band_means_no_change(tmp_path):
    """띠가 없으면 margin 은 입력 그대로다 — 검출이 도는 것만으로 화면이 바뀌면 안 된다."""
    from app import pipeline

    cfg = SimpleNamespace(canvas_width=1080, canvas_height=1920)
    assert pipeline._avoid_burned_margin_v(
        430, None, design=DesignConfig(), config=cfg,
        title_text="제목\n두 줄", font_size=65) == 430


def test_pipeline_applies_avoidance_to_variants_too():
    """variant 도 같은 띠를 피한다 — 원본 자막은 소재의 성질이다."""
    src = (Path(__file__).resolve().parents[1] / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert src.count("_avoid_burned_margin_v(") == 3      # 정의 1 + 정본 1 + variant 1
    assert "_burned_band = _detect_burned_band_cached(" in src
