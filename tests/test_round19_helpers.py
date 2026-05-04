"""라운드 19 helper functions 단위 테스트.

순수 로직 함수만 테스트. 파이프라인·외부 LLM 의존 함수는 e2e로 검증.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.modules.story_builder import StoryClip
from app.pipeline import (
    _dedup_overlapping_candidates,
    _extend_storyline_for_narrative,
    _fill_intra_storyline_gaps,
    _snap_clip_boundaries_to_dialogue,
)


def _mk_clip(start, end, chars=("A",), chunk=0, cand=0, role="hook"):
    return StoryClip(
        role=role, start_sec=start, end_sec=end,
        subtitle="", use_original_audio=True, pacing_note=None,
        chunk_index=chunk, candidate_index=cand,
        character_focus=tuple(chars),
    )


def _mk_seg(start, end, text="x"):
    return SimpleNamespace(start_sec=start, end_sec=end, text=text)


def test_dedup_keeps_higher_score_on_overlap():
    """IoU >= 0.7 중복 발견 시 score 높은 쪽 유지."""
    cands = [
        {"start_sec": 550.0, "end_sec": 610.0, "score": 0.8, "chunk_index": 0},
        {"start_sec": 545.0, "end_sec": 605.0, "score": 0.7, "chunk_index": 1},
    ]
    out = _dedup_overlapping_candidates(cands, iou_threshold=0.7)
    assert len(out) == 1
    assert out[0]["score"] == 0.8


def test_dedup_keeps_distinct_candidates():
    """IoU < 0.7 → 두 candidate 모두 유지."""
    cands = [
        {"start_sec": 100.0, "end_sec": 130.0, "score": 0.8},
        {"start_sec": 200.0, "end_sec": 230.0, "score": 0.7},
    ]
    out = _dedup_overlapping_candidates(cands, iou_threshold=0.7)
    assert len(out) == 2


def test_dedup_handles_viral_score_fallback():
    """score 없으면 viral_score 사용."""
    cands = [
        {"start_sec": 550.0, "end_sec": 610.0, "viral_score": 0.9},
        {"start_sec": 545.0, "end_sec": 605.0, "viral_score": 0.6},
    ]
    out = _dedup_overlapping_candidates(cands, iou_threshold=0.7)
    assert len(out) == 1
    assert out[0]["viral_score"] == 0.9


def test_dedup_empty_input():
    assert _dedup_overlapping_candidates([]) == []


def test_dedup_invalid_durations_zero_iou():
    """end <= start인 잘못된 candidate는 IoU 0으로 처리 → 유지."""
    cands = [
        {"start_sec": 100.0, "end_sec": 100.0, "score": 0.5},
        {"start_sec": 100.0, "end_sec": 130.0, "score": 0.5},
    ]
    out = _dedup_overlapping_candidates(cands, iou_threshold=0.7)
    assert len(out) == 2  # IoU=0이므로 둘 다 유지


def test_snap_extends_first_start_to_segment_start():
    """첫 clip start가 가까운 transcript segment start - 0.2s로 스냅."""
    clips = [_mk_clip(11.0, 16.5)]
    variants = [(clips, "T", 1.0)]
    segs = [_mk_seg(10.0, 13.5), _mk_seg(15.0, 18.0)]
    out = _snap_clip_boundaries_to_dialogue(variants, segs)
    new_clips = out[0][0]
    # first.start_sec=11.0, 가까운 candidate: 10-0.2=9.8 (거리 1.2) 또는 15-0.2=14.8 (범위 밖, +1까지만)
    assert new_clips[0].start_sec == 9.8


def test_snap_extends_last_end_to_segment_end():
    """마지막 clip end가 가까운 segment end + 0.3s로 스냅."""
    clips = [_mk_clip(11.0, 16.5)]
    variants = [(clips, "T", 1.0)]
    segs = [_mk_seg(10.0, 13.5), _mk_seg(15.0, 18.0)]
    out = _snap_clip_boundaries_to_dialogue(variants, segs)
    new_clips = out[0][0]
    # last.end=16.5, candidates: 13.5+0.3=13.8 (범위 밖), 18+0.3=18.3 (범위 -1~+5 안)
    assert new_clips[0].end_sec == 18.3


def test_snap_no_change_when_no_segments_in_range():
    clips = [_mk_clip(100.0, 105.0)]
    variants = [(clips, "T", 1.0)]
    segs = [_mk_seg(10.0, 13.5)]
    out = _snap_clip_boundaries_to_dialogue(variants, segs)
    assert out[0][0][0].start_sec == 100.0
    assert out[0][0][0].end_sec == 105.0


def test_extend_narrative_uses_context_extension():
    """길이 < target_max + candidate.context_extension 있으면 양쪽 확장."""
    clips = [_mk_clip(300.0, 320.0, chunk=0, cand=0)]
    variants = [(clips, "T", 1.0)]
    cands_lookup = {(0, 0): {
        "start_sec": 300.0, "end_sec": 320.0,
        "context_extension": {"needed": True, "extended_start_sec": 290.0, "extended_end_sec": 328.0},
    }}
    out = _extend_storyline_for_narrative(variants, cands_lookup, target_max=60.0, max_extend_per_side=8.0)
    new_clip = out[0][0][0]
    assert new_clip.start_sec == 292.0  # 300 - 8 (max_extend_per_side cap)
    assert new_clip.end_sec == 328.0  # 320 + 8


def test_extend_narrative_skipped_when_at_target_max():
    """길이 >= target_max면 확장 안 함."""
    clips = [_mk_clip(0.0, 60.0, chunk=0, cand=0)]
    variants = [(clips, "T", 1.0)]
    cands_lookup = {(0, 0): {
        "start_sec": 0.0, "end_sec": 60.0,
        "context_extension": {"needed": True, "extended_start_sec": -10.0, "extended_end_sec": 70.0},
    }}
    out = _extend_storyline_for_narrative(variants, cands_lookup, target_max=60.0)
    new_clip = out[0][0][0]
    assert new_clip.start_sec == 0.0
    assert new_clip.end_sec == 60.0


def test_fill_gap_combines_adjacent_clips_with_common_character():
    """갭 ≤ 3s + character 공통 → pre clip의 end를 next clip start까지 확장."""
    clips = [_mk_clip(100.0, 110.0, chars=("A", "B")), _mk_clip(112.0, 125.0, chars=("B", "C"))]
    variants = [(clips, "T", 1.0)]
    out = _fill_intra_storyline_gaps(variants, max_gap_sec=3.0)
    new_clips = out[0][0]
    assert new_clips[0].end_sec == 112.0  # 110 → 112 (갭 채움)
    assert new_clips[1].start_sec == 112.0


def test_fill_gap_skipped_when_no_common_character():
    """공통 character 없으면 갭 그대로."""
    clips = [_mk_clip(100.0, 110.0, chars=("A",)), _mk_clip(112.0, 125.0, chars=("B",))]
    variants = [(clips, "T", 1.0)]
    out = _fill_intra_storyline_gaps(variants, max_gap_sec=3.0)
    assert out[0][0][0].end_sec == 110.0


def test_fill_gap_skipped_when_gap_too_large():
    """갭 > max_gap_sec이면 그대로."""
    clips = [_mk_clip(100.0, 110.0, chars=("A",)), _mk_clip(120.0, 130.0, chars=("A",))]
    variants = [(clips, "T", 1.0)]
    out = _fill_intra_storyline_gaps(variants, max_gap_sec=3.0)
    assert out[0][0][0].end_sec == 110.0  # 갭 10s > 3s
