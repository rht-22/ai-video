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
    _fit_storyline_to_duration,
    _snap_clip_boundaries_to_dialogue,
    _validate_storyline_timeline,
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


# ── 라운드 20: storyline timeline 검증 테스트 ──

def test_validate_rejects_hook_payoff_same_timestamp():
    """hook과 payoff가 같은 timestamp인 환각 케이스 폐기 (SNL EP06 #2 실제 사례)."""
    sl_data = {
        "storyline": {
            "hook": {"start_sec": 2131.0, "end_sec": 2224.0},
            "build": [{"start_sec": 1692.0, "end_sec": 1700.0}],
            "payoff": {"start_sec": 2131.0, "end_sec": 2224.0},
        },
    }
    is_valid, reason = _validate_storyline_timeline(sl_data)
    assert not is_valid
    assert "IoU" in reason


def test_validate_rejects_high_iou_overlap():
    """hook ↔ payoff IoU > 0.3 폐기."""
    sl_data = {
        "storyline": {
            "hook": {"start_sec": 100.0, "end_sec": 130.0},
            "build": [{"start_sec": 50.0, "end_sec": 90.0}],
            "payoff": {"start_sec": 110.0, "end_sec": 140.0},  # IoU≈0.5
        },
    }
    is_valid, reason = _validate_storyline_timeline(sl_data)
    assert not is_valid


def test_validate_accepts_chronological_order():
    """정시간순 (hook < build < payoff) 통과."""
    sl_data = {
        "storyline": {
            "hook": {"start_sec": 100.0, "end_sec": 110.0},
            "build": [{"start_sec": 120.0, "end_sec": 130.0}],
            "payoff": {"start_sec": 140.0, "end_sec": 150.0},
        },
    }
    is_valid, reason = _validate_storyline_timeline(sl_data)
    assert is_valid, f"chronological order should pass: {reason}"


def test_validate_accepts_result_first_pattern():
    """결과선공개 (build < payoff < hook, hook이 끝) 통과."""
    sl_data = {
        "storyline": {
            "hook": {"start_sec": 200.0, "end_sec": 210.0},  # hook이 끝
            "build": [{"start_sec": 50.0, "end_sec": 60.0}],
            "payoff": {"start_sec": 100.0, "end_sec": 110.0},  # 중간
        },
    }
    is_valid, reason = _validate_storyline_timeline(sl_data)
    assert is_valid, f"result-first should pass: {reason}"


def test_validate_rejects_time_reversal():
    """build가 hook보다 7분 앞선 시간 역행 (SNL EP06 #2 실제 사례) 폐기."""
    sl_data = {
        "storyline": {
            "hook": {"start_sec": 2131.0, "end_sec": 2200.0},
            "build": [{"start_sec": 1692.0, "end_sec": 1700.0}],  # hook보다 7분 앞
            "payoff": {"start_sec": 2300.0, "end_sec": 2350.0},
        },
    }
    is_valid, reason = _validate_storyline_timeline(sl_data)
    assert not is_valid
    assert "timeline" in reason or "IoU" in reason


def test_validate_passes_highlight_type():
    """highlight 타입 (storyline 키 없음) 통과."""
    sl_data = {
        "shorts_type": "highlight",
        "start_sec": 100.0,
        "end_sec": 140.0,
    }
    is_valid, _ = _validate_storyline_timeline(sl_data)
    assert is_valid


def test_validate_rejects_invalid_durations():
    """end_sec <= start_sec 폐기."""
    sl_data = {
        "storyline": {
            "hook": {"start_sec": 100.0, "end_sec": 100.0},
            "payoff": {"start_sec": 200.0, "end_sec": 210.0},
        },
    }
    is_valid, _ = _validate_storyline_timeline(sl_data)
    assert not is_valid


# ── 라운드 21: 역할별(role-aware) trim 테스트 ──

def test_role_aware_trim_payoff_keeps_end():
    """payoff: 끝 보존, 시작에서 자름 (reveal/punchline 보존)."""
    # 80s 합계 → 60s로 단축. clips: hook 30s + payoff 50s.
    clips = [
        _mk_clip(0.0, 30.0, role="hook"),
        _mk_clip(100.0, 150.0, role="payoff"),
    ]
    # candidates_lookup 빈 dict (build 제거 로직 안 탐 → 비례 trim 분기로 직접 들어감)
    out, msg = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=60.0)
    # ratio = 60/80 = 0.75, hook_new=22.5, payoff_new=37.5
    # hook(end-trim): start=0, end=22.5
    # payoff(start-trim): start=100+12.5=112.5, end=150
    assert abs(out[0].start_sec - 0.0) < 0.01
    assert abs(out[0].end_sec - 22.5) < 0.01
    assert abs(out[1].start_sec - 112.5) < 0.01
    assert abs(out[1].end_sec - 150.0) < 0.01
    assert "역할별 trim" in msg
    assert "payoff(start)" in msg
    assert "hook(end)" in msg


def test_role_aware_trim_with_build_in_pipeline():
    """hook + build + payoff 시나리오: 현재 로직은 build 먼저 제거 후 비례 trim.

    build branch는 build가 통째로 제거된 후 hook+payoff만 남고 그 합이 여전히 target_max를
    초과하는 케이스에서만 hook/payoff로 적용. build branch 자체는 build가 살아 있는 채
    role-aware 단계에 도달하는 경우의 defensive code.
    """
    clips = [
        _mk_clip(0.0, 50.0, role="hook"),  # 50s
        _mk_clip(100.0, 130.0, role="build"),  # 30s
        _mk_clip(200.0, 250.0, role="payoff"),  # 50s. 합계 130s
    ]
    out, msg = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=60.0)
    # build 제거됨 → hook 50s + payoff 50s = 100s, 여전히 > 60
    # ratio = 60/100 = 0.6, hook→30s (end-trim), payoff→30s (start-trim)
    assert len(out) == 2
    assert out[0].role == "hook"
    assert out[1].role == "payoff"
    assert abs(out[0].start_sec - 0.0) < 0.01
    assert abs(out[0].end_sec - 30.0) < 0.01
    assert abs(out[1].start_sec - 220.0) < 0.01
    assert abs(out[1].end_sec - 250.0) < 0.01
    assert "build" in msg or "hook(end)" in msg or "payoff(start)" in msg


def test_role_aware_trim_hook_keeps_start():
    """hook: 시작 보존, 끝에서 자름 (후킹 모먼트 보존)."""
    clips = [_mk_clip(0.0, 80.0, role="hook")]
    out, msg = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=60.0)
    assert abs(out[0].start_sec - 0.0) < 0.01
    assert abs(out[0].end_sec - 60.0) < 0.01
    assert "hook(end)" in msg


def test_role_aware_trim_no_change_when_under_max():
    """target_max 이하면 변경 없음."""
    clips = [_mk_clip(0.0, 30.0, role="hook"), _mk_clip(100.0, 120.0, role="payoff")]  # 50s
    out, msg = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=60.0)
    assert out[0].end_sec == 30.0
    assert out[1].start_sec == 100.0
    assert msg == ""
