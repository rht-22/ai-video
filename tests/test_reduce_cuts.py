"""reduce 단계 순수 로직 테스트.

- map_draft_cuts_to_results: 드래프트(이어붙인) 타임라인 컷 → clip별 소스 keep_intervals
- _normalize_cut_segments: Gemini 컷 응답 검증/clamp
파이프라인·LLM 의존부는 e2e로 검증.
"""
from __future__ import annotations

from app.modules.story_builder import StoryClip
from app.modules.silence_cutter import map_draft_cuts_to_results, flatten_to_clips
from app.modules.gemini_client import _normalize_cut_segments
from app.pipeline import _shift_cues_by_draft_cuts


def _clip(start, end, role="build"):
    return StoryClip(
        role=role, start_sec=start, end_sec=end,
        subtitle="", use_original_audio=True,
    )


def test_map_cut_inside_single_clip_splits_into_two():
    # clip0: src[10,20] -> draft[0,10] / clip1: src[100,120] -> draft[10,30]
    clips = [_clip(10.0, 20.0), _clip(100.0, 120.0)]
    # draft[5,8] -> clip0 src[15,18] 제거
    results = map_draft_cuts_to_results(clips, [{"start_sec": 5.0, "end_sec": 8.0}])
    assert results[0].keep_intervals == [(10.0, 15.0), (18.0, 20.0)]
    assert abs(results[0].total_removed_sec - 3.0) < 1e-6
    # clip1 변화 없음
    assert results[1].keep_intervals == [(100.0, 120.0)]
    assert results[1].total_removed_sec == 0.0


def test_map_cut_in_second_clip_uses_offset():
    clips = [_clip(10.0, 20.0), _clip(100.0, 120.0)]
    # draft[12,16] -> clip1 offset10 -> src[102,106]
    results = map_draft_cuts_to_results(clips, [{"start_sec": 12.0, "end_sec": 16.0}])
    assert results[0].keep_intervals == [(10.0, 20.0)]
    assert results[1].keep_intervals == [(100.0, 102.0), (106.0, 120.0)]
    assert abs(results[1].total_removed_sec - 4.0) < 1e-6


def test_map_cut_spanning_clip_boundary():
    clips = [_clip(10.0, 20.0), _clip(100.0, 120.0)]
    # draft[8,14] -> clip0 [8,10]->src[18,20], clip1 [10,14]->src[100,104]
    results = map_draft_cuts_to_results(clips, [{"start_sec": 8.0, "end_sec": 14.0}])
    assert results[0].keep_intervals == [(10.0, 18.0)]
    assert results[1].keep_intervals == [(104.0, 120.0)]


def test_map_full_clip_cut_drops_clip_on_flatten():
    clips = [_clip(10.0, 20.0), _clip(100.0, 120.0)]
    # draft[0,10] -> clip0 전체 제거
    results = map_draft_cuts_to_results(clips, [{"start_sec": 0.0, "end_sec": 10.0}])
    assert results[0].keep_intervals == []
    new_clips = flatten_to_clips(results)
    # clip0 사라지고 clip1만 남음
    assert len(new_clips) == 1
    assert new_clips[0].start_sec == 100.0 and new_clips[0].end_sec == 120.0


def test_map_drops_tiny_keep_fragments():
    clips = [_clip(0.0, 10.0)]
    # draft[0.1, 10] 제거 → 남는 [0,0.1] 는 min_keep_sec(0.3) 미만 → 버림
    results = map_draft_cuts_to_results(clips, [{"start_sec": 0.1, "end_sec": 10.0}])
    assert results[0].keep_intervals == []


def test_normalize_clamps_and_filters():
    raw = [
        {"priority": 2, "start_sec": -5, "end_sec": 8},   # start clamp → 0
        {"priority": 1, "start_sec": 50, "end_sec": 200},  # end clamp → 100
        {"priority": 1, "start_sec": 30, "end_sec": 30},   # zero-length → drop
        {"start_sec": 10, "end_sec": 12},                  # priority 없음 → 큰 값
        "bad",                                              # dict 아님 → skip
    ]
    out = _normalize_cut_segments(raw, duration_sec=100.0)
    assert len(out) == 3
    assert out[0]["start_sec"] == 0.0 and out[0]["end_sec"] == 8.0
    assert out[1]["end_sec"] == 100.0
    assert out[2]["priority"] == 9999


def test_normalize_non_list_returns_empty():
    assert _normalize_cut_segments(None, 100.0) == []
    assert _normalize_cut_segments({"x": 1}, 100.0) == []


# ── _shift_cues_by_draft_cuts: 단일 clip 내부 컷도 cue 시간 보정 ──

def test_shift_cue_after_cuts_moves_earlier():
    # 90s 단일 타임라인에서 [8,40] (32s) 제거 → 75s 의 cue 는 75-32=43 으로 이동
    cues = [{"start_sec": 75.0, "end_sec": 80.0, "text": "payoff"}]
    cuts = [{"start_sec": 8.0, "end_sec": 40.0}]
    out = _shift_cues_by_draft_cuts(cues, cuts)
    assert len(out) == 1
    assert abs(out[0]["start_sec"] - 43.0) < 1e-6
    assert abs(out[0]["end_sec"] - 48.0) < 1e-6


def test_shift_cue_before_cuts_unchanged():
    cues = [{"start_sec": 2.0, "end_sec": 5.0}]
    cuts = [{"start_sec": 8.0, "end_sec": 40.0}]
    out = _shift_cues_by_draft_cuts(cues, cuts)
    assert out[0]["start_sec"] == 2.0 and out[0]["end_sec"] == 5.0


def test_shift_cue_inside_cut_is_dropped():
    # cue 중심(중앙 20s)이 제거 구간 [8,40] 안 → cue 버림
    cues = [{"start_sec": 15.0, "end_sec": 25.0}]
    cuts = [{"start_sec": 8.0, "end_sec": 40.0}]
    out = _shift_cues_by_draft_cuts(cues, cuts)
    assert out == []


def test_shift_multiple_cuts_accumulate():
    # 컷 [10,20](10s) + [50,60](10s). cue@70 → 70-20=50
    cues = [{"start_sec": 70.0, "end_sec": 72.0}]
    cuts = [{"start_sec": 10.0, "end_sec": 20.0}, {"start_sec": 50.0, "end_sec": 60.0}]
    out = _shift_cues_by_draft_cuts(cues, cuts)
    assert abs(out[0]["start_sec"] - 50.0) < 1e-6
    assert abs(out[0]["end_sec"] - 52.0) < 1e-6


def test_shift_no_cuts_returns_same():
    cues = [{"start_sec": 5.0, "end_sec": 9.0}]
    assert _shift_cues_by_draft_cuts(cues, []) == cues
