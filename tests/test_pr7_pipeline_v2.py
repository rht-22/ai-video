"""PR-7 파이프라인 12단계 단순화 단위 테스트.

검증 대상 (순수 함수):
1. _STEP_ORDER — 12단계 구성, 제거된 단계 부재.
2. _normalize_chunk_boundaries — ≥5분 하한 병합 / max 분할 / 빈 입력 폴백 / 연속 커버리지.
3. _abs_characters_tracking + _build_character_appearances — 청크-상대→절대 오프셋,
   overlap 병합, samples 보존, 좌표 없으면 samples 비움.
4. build_chunks(boundaries=...) — 내용 기반 경계 사용 + overlap prepend + 빈 경계 폴백.
5. face_id.find_target_in_index — hit / character miss / time miss (좌표 lookup).
"""
from __future__ import annotations

from pathlib import Path

from app.modules.chunker import build_chunks
from app.modules.face_id import find_target_in_index
from app.pipeline import (
    _STEP_ORDER,
    _abs_characters_tracking,
    _build_character_appearances,
    _normalize_chunk_boundaries,
)


# ── 1. _STEP_ORDER ───────────────────────────────────────────────
def test_step_order_is_12_steps():
    assert _STEP_ORDER == [
        "init", "research", "probe", "proxy", "video_intent", "chunk",
        "gemini", "story", "av_align", "resources", "render", "validate",
    ]


def test_removed_steps_absent():
    for removed in ("character_index", "chunk_transcribe", "graph", "skeleton"):
        assert removed not in _STEP_ORDER


# ── 2. _normalize_chunk_boundaries ──────────────────────────────
def test_boundaries_empty_returns_empty():
    assert _normalize_chunk_boundaries([], 0.0, 1000.0) == []


def test_boundaries_merge_below_min():
    # 0-120(짧음) 은 다음 구간에 병합되어 ≥300s 가 되어야 한다.
    raw = [{"start_sec": 0, "end_sec": 120}, {"start_sec": 120, "end_sec": 800}]
    out = _normalize_chunk_boundaries(raw, 0.0, 800.0, min_sec=300.0, max_sec=600.0)
    # 모든 구간이 ≥ 300 (마지막 분할 조각 제외 가능성 없도록 max=600 으로 800→2분할 400/400)
    assert out  # 비어있지 않음
    assert out[0][0] == 0.0
    assert out[-1][1] == 800.0
    # 연속 커버리지 (빈틈 없음)
    for a, b in zip(out, out[1:]):
        assert abs(a[1] - b[0]) < 1e-6


def test_boundaries_split_above_max():
    out = _normalize_chunk_boundaries(
        [{"start_sec": 0, "end_sec": 1300}], 0.0, 1300.0, min_sec=300.0, max_sec=600.0
    )
    # 1300 > 600 → 3분할 (각 ~433s, 모두 ≤600 & ≥300)
    assert len(out) == 3
    for s, e in out:
        assert 300.0 <= (e - s) <= 600.0
    assert out[0][0] == 0.0 and out[-1][1] == 1300.0


def test_boundaries_clip_to_content_range():
    out = _normalize_chunk_boundaries(
        [{"start_sec": 0, "end_sec": 2000}], 100.0, 700.0, min_sec=300.0, max_sec=600.0
    )
    assert out[0][0] == 100.0
    assert out[-1][1] == 700.0


# ── 3. characters_tracking → character_appearances ──────────────
def test_abs_characters_tracking_applies_offset():
    ct = [{"character": "민준", "appearances": [
        {"start_sec": 10, "end_sec": 40, "action": "a", "x_norm": 0.4, "y_norm": 0.3}
    ]}]
    out = _abs_characters_tracking(ct, 100.0)
    ap = out[0]["appearances"][0]
    assert ap["start_sec"] == 110.0 and ap["end_sec"] == 140.0
    assert ap["x_norm"] == 0.4 and ap["y_norm"] == 0.3


def test_build_character_appearances_single_coord_midpoint_sample():
    cm = [{"characters_tracking": _abs_characters_tracking(
        [{"character": "민준", "appearances": [
            {"start_sec": 10, "end_sec": 40, "x_norm": 0.4, "y_norm": 0.3}]}],
        0.0,
    )}]
    ca = _build_character_appearances(cm)
    assert len(ca) == 1
    assert ca[0]["character"] == "민준"
    assert len(ca[0]["samples"]) == 1
    assert ca[0]["samples"][0]["t"] == 25.0  # midpoint
    assert ca[0]["samples"][0]["x_norm"] == 0.4


def test_build_character_appearances_merges_overlap_across_chunks():
    # 청크0 abs 10-40, 청크1 abs 38-60 (overlap) → 같은 인물 병합 10-60
    cm = [
        {"characters_tracking": _abs_characters_tracking(
            [{"character": "민준", "appearances": [
                {"start_sec": 10, "end_sec": 40, "samples": [{"t": 20, "x_norm": 0.4, "y_norm": 0.3}]}]}], 0.0)},
        {"characters_tracking": _abs_characters_tracking(
            [{"character": "민준", "appearances": [
                {"start_sec": 38, "end_sec": 60, "samples": [{"t": 50, "x_norm": 0.6, "y_norm": 0.3}]}]}], 0.0)},
    ]
    ca = _build_character_appearances(cm)
    assert len(ca) == 1
    assert ca[0]["start_sec"] == 10.0 and ca[0]["end_sec"] == 60.0
    assert len(ca[0]["samples"]) == 2  # 두 sample 결합


def test_build_character_appearances_no_coords_empty_samples():
    cm = [{"characters_tracking": _abs_characters_tracking(
        [{"character": "민준", "appearances": [{"start_sec": 10, "end_sec": 40, "action": "a"}]}], 0.0)}]
    ca = _build_character_appearances(cm)
    assert len(ca) == 1
    assert ca[0]["samples"] == []  # 좌표 없으면 samples 빈 채로 (reframe opencv 폴백)


def test_build_character_appearances_drops_coordless_samples():
    # sample 에 t 만 있고 x_norm/y_norm 이 없으면 drop — _clamp01(None)=0.5 '가짜 중앙 hit' 방지
    cm = [{"characters_tracking": _abs_characters_tracking(
        [{"character": "민준", "appearances": [
            {"start_sec": 10, "end_sec": 40, "samples": [{"t": 20}, {"t": 30, "x_norm": 0.7, "y_norm": 0.5}]}]}],
        0.0)}]
    ca = _build_character_appearances(cm)
    assert len(ca) == 1
    # 좌표 있는 sample 1개만 남고, 좌표 없는 {"t":20} 은 버려져야 한다
    assert len(ca[0]["samples"]) == 1
    assert ca[0]["samples"][0]["x_norm"] == 0.7


# ── 4. build_chunks(boundaries=...) ─────────────────────────────
def test_build_chunks_uses_boundaries_with_overlap():
    vp = Path("/tmp/x.mp4")
    chunks = build_chunks(vp, 900.0, 600, 30, content_start_sec=0.0, content_end_sec=900.0,
                          boundaries=[(0.0, 400.0), (400.0, 900.0)])
    assert len(chunks) == 2
    assert chunks[0].start_sec == 0.0 and chunks[0].end_sec == 400.0
    # 두 번째 청크는 overlap(30s) 만큼 앞당겨 prepend
    assert chunks[1].start_sec == 370.0 and chunks[1].end_sec == 900.0
    assert [c.index for c in chunks] == [0, 1]


def test_build_chunks_empty_boundaries_fallback_to_fixed():
    vp = Path("/tmp/x.mp4")
    # boundaries=[] → 고정 분할 폴백 (chunk_seconds=600)
    chunks = build_chunks(vp, 1500.0, 600, 180, content_start_sec=0.0, content_end_sec=1500.0,
                          boundaries=[])
    assert len(chunks) >= 2  # 고정 분할 동작


# ── 5. find_target_in_index ─────────────────────────────────────
def _appearances():
    return [{"character": "민준", "start_sec": 0.0, "end_sec": 30.0,
             "samples": [{"t": 10.0, "x_norm": 0.5, "y_norm": 0.4, "similarity": 1.0}]}]


def test_find_target_hit_scales_to_pixels():
    res = find_target_in_index(_appearances(), "민준", 10.0, 1000, 2000)
    assert res == (500.0, 800.0)  # 0.5*1000, 0.4*2000


def test_find_target_character_miss():
    assert find_target_in_index(_appearances(), "__none__", 10.0, 1000, 2000) is None


def test_find_target_time_miss():
    # t=100 은 sample(t=10) 에서 max_dt_sec(3.0) 초과 → None
    assert find_target_in_index(_appearances(), "민준", 100.0, 1000, 2000) is None
