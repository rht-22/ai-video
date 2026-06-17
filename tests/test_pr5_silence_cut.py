"""PR-5 — 스토리-aware 무음 컷 단위 테스트.

검증 대상:
1. _is_gap_safe_to_cut — gap 양쪽 segment + candidate 메타 기반 컷 가능 여부 판별.
   규칙: candidate.visual_essential=False AND description 에 액션 동사 없음 AND 화자 변경 없음.
2. cut_silence_with_story_filter — clip 안 segments 의 gap 중 _is_gap_safe_to_cut 통과한
   것만 컷. visual_essential clip 은 통째 보호.
3. _shift_cues_by_silence_cut — keep_intervals 누적 감소량으로 cue 시간 시프트.
"""
from __future__ import annotations

from dataclasses import replace

from app.modules.silence_cutter import (
    AGGRESSIVE_PROFILE,
    CONSERVATIVE_PROFILE,
    Interval,
    SilenceCutProfile,
    SilenceCutResult,
    _is_gap_safe_to_cut,
    cut_silence_with_story_filter,
    get_silence_profile,
)
from app.modules.speech import SpeechSegment
from app.modules.story_builder import StoryClip
from app.pipeline import _shift_cues_by_silence_cut


def _seg(start, end, text="대사"):
    return SpeechSegment(start_sec=float(start), end_sec=float(end), text=text)


def _clip(start, end, chunk=0, cand=0, visual_essential=False):
    return StoryClip(
        role="hook", start_sec=float(start), end_sec=float(end),
        subtitle="", use_original_audio=True, pacing_note=None,
        chunk_index=chunk, candidate_index=cand,
        character_focus=("A",),
        visual_essential=visual_essential,
    )


def _candidate(visual_essential=False, description="평범한 장면 묘사", chunk=0, cand=0):
    return {
        "chunk_index": chunk, "candidate_index": cand,
        "visual_essential": visual_essential,
        "description": description,
        "start_sec": 0.0, "end_sec": 1000.0,
    }


# ──────────────────────────────────────────────────────────────
# _is_gap_safe_to_cut
# ──────────────────────────────────────────────────────────────


def test_safe_when_no_candidate_meta_is_conservative_false():
    # 메타 없으면 컷 안 함 (보수적)
    assert _is_gap_safe_to_cut(None, _seg(0, 1), _seg(2, 3)) is False


def test_unsafe_when_visual_essential_true():
    cand = _candidate(visual_essential=True)
    assert _is_gap_safe_to_cut(cand, _seg(0, 1), _seg(2, 3)) is False


def test_unsafe_when_description_has_action_verb():
    # 액션 동사 키워드 — 시선 교환 / 키스 / 표정 / 충돌 등
    for desc in [
        "두 사람이 시선을 교환한다",
        "주인공이 키스한다",
        "표정이 굳는다",
        "차가 충돌한다",
        "주먹을 휘두른다",
        "뺨을 때린다",
    ]:
        cand = _candidate(description=desc)
        assert _is_gap_safe_to_cut(cand, _seg(0, 1), _seg(2, 3)) is False, f"desc={desc!r} 가 unsafe 여야 함"


def test_safe_when_description_has_no_action_verb():
    cand = _candidate(description="복도를 천천히 걸으며 대화한다")
    assert _is_gap_safe_to_cut(cand, _seg(0, 1), _seg(2, 3)) is True


def test_unsafe_when_speaker_change_via_narration_prefix():
    # 한쪽만 [내레이션] 접두 → 화자 변경 → unsafe
    cand = _candidate()
    assert _is_gap_safe_to_cut(cand, _seg(0, 1, text="[내레이션] 그날"), _seg(2, 3, text="다음 대사")) is False
    assert _is_gap_safe_to_cut(cand, _seg(0, 1, text="앞 대사"), _seg(2, 3, text="[내레이션] 시작")) is False


def test_safe_when_both_sides_narration():
    cand = _candidate()
    assert _is_gap_safe_to_cut(cand, _seg(0, 1, text="[내레이션] A"), _seg(2, 3, text="[내레이션] B")) is True


def test_safe_when_both_sides_dialog():
    cand = _candidate()
    assert _is_gap_safe_to_cut(cand, _seg(0, 1, text="대사 A"), _seg(2, 3, text="대사 B")) is True


def test_unsafe_when_one_side_missing_segment():
    # 한쪽 segment 가 None (gap 이 clip 경계에 닿음) → 보수적으로 unsafe
    cand = _candidate()
    assert _is_gap_safe_to_cut(cand, None, _seg(2, 3)) is False
    assert _is_gap_safe_to_cut(cand, _seg(0, 1), None) is False


def test_safe_when_both_sides_none_and_candidate_safe():
    # 양쪽 모두 None — clip 안 transcript 가 아예 없을 때.
    # candidate 가 visual_essential=False 이고 액션 없으면 safe (긴 무음 자체 컷 가능)
    cand = _candidate()
    assert _is_gap_safe_to_cut(cand, None, None) is True


# ──────────────────────────────────────────────────────────────
# cut_silence_with_story_filter — 통합 동작
# ──────────────────────────────────────────────────────────────


def test_story_filter_passes_clip_with_no_segments_unchanged():
    clip = _clip(0, 30)
    candidates_lookup = {(0, 0): _candidate()}
    out = cut_silence_with_story_filter([clip], [], candidates_lookup)
    assert len(out) == 1
    assert out[0].keep_intervals == [Interval(0.0, 30.0)]
    assert out[0].total_removed_sec == 0.0


def test_story_filter_protects_visual_essential_clip():
    clip = _clip(0, 30, visual_essential=True)
    segs = [_seg(0, 1), _seg(10, 11), _seg(20, 21)]  # 큰 gap 있지만 보호
    candidates_lookup = {(0, 0): _candidate(visual_essential=True)}
    out = cut_silence_with_story_filter([clip], segs, candidates_lookup)
    assert out[0].keep_intervals == [Interval(0.0, 30.0)]


def test_story_filter_cuts_safe_gap():
    # gap 8s (1~9), 일반 대화, 컷 가능
    clip = _clip(0, 12)
    segs = [_seg(0.5, 1.0, "안녕"), _seg(9.0, 11.5, "잘 가")]
    candidates_lookup = {(0, 0): _candidate(description="대화 장면")}
    out = cut_silence_with_story_filter([clip], segs, candidates_lookup)
    # padding 0.15 적용 → 2개 keep_intervals
    assert len(out[0].keep_intervals) == 2
    assert out[0].total_removed_sec > 5.0  # 큰 무음 컷됨


def test_story_filter_does_not_cut_when_action_verb():
    # 같은 시간 구간, 액션 동사 있으면 컷 안 함
    clip = _clip(0, 12)
    segs = [_seg(0.5, 1.0, "안녕"), _seg(9.0, 11.5, "잘 가")]
    candidates_lookup = {(0, 0): _candidate(description="두 사람이 키스한다")}
    out = cut_silence_with_story_filter([clip], segs, candidates_lookup)
    # 액션 동사 → unsafe gap → 통째 유지
    assert out[0].keep_intervals == [Interval(0.0, 12.0)]
    assert out[0].total_removed_sec == 0.0


def test_story_filter_does_not_cut_when_speaker_change():
    # 화자 변경 (한쪽만 [내레이션]) → 컷 안 함
    clip = _clip(0, 12)
    segs = [_seg(0.5, 1.0, "[내레이션] 그날"), _seg(9.0, 11.5, "다음 대사")]
    candidates_lookup = {(0, 0): _candidate(description="평범한 장면")}
    out = cut_silence_with_story_filter([clip], segs, candidates_lookup)
    assert out[0].keep_intervals == [Interval(0.0, 12.0)]


def test_story_filter_no_candidate_meta_skips_cut():
    # candidates_lookup 에 없으면 보수적으로 컷 안 함
    clip = _clip(0, 12, chunk=99, cand=99)
    segs = [_seg(0.5, 1.0, "a"), _seg(9.0, 11.5, "b")]
    candidates_lookup = {}  # (99, 99) 없음
    out = cut_silence_with_story_filter([clip], segs, candidates_lookup)
    assert out[0].keep_intervals == [Interval(0.0, 12.0)]


# ──────────────────────────────────────────────────────────────
# _shift_cues_by_silence_cut — cue 시간 보정 (pipeline.py)
# ──────────────────────────────────────────────────────────────


def test_shift_cues_no_cuts_returns_unchanged():
    # silence_cut 결과 keep_intervals 가 원본과 같으면 시프트 없음
    clips = [_clip(0, 10), _clip(20, 30)]
    results = [
        SilenceCutResult(original_clip=clips[0], keep_intervals=[Interval(0, 10)], total_removed_sec=0.0),
        SilenceCutResult(original_clip=clips[1], keep_intervals=[Interval(20, 30)], total_removed_sec=0.0),
    ]
    cues = [{"start_sec": 5.0, "end_sec": 8.0, "text": "x", "voice": "ko_female", "speed": "normal"}]
    out = _shift_cues_by_silence_cut(cues, results, clips)
    assert out[0]["start_sec"] == 5.0
    assert out[0]["end_sec"] == 8.0


def test_shift_cues_after_first_clip_cut():
    # clip0: edit timeline 0~10 → 컷 후 0~5 (removed=5). cue at edit 12 (clip1 안) → shift -5 → 7
    clips = [_clip(0, 10), _clip(20, 30)]  # 원본 clip0 = 10초, clip1 = 10초
    results = [
        SilenceCutResult(original_clip=clips[0], keep_intervals=[Interval(0, 5)], total_removed_sec=5.0),
        SilenceCutResult(original_clip=clips[1], keep_intervals=[Interval(20, 30)], total_removed_sec=0.0),
    ]
    cues = [{"start_sec": 12.0, "end_sec": 15.0, "text": "x", "voice": "ko_female", "speed": "normal"}]
    out = _shift_cues_by_silence_cut(cues, results, clips)
    assert out[0]["start_sec"] == 7.0
    assert out[0]["end_sec"] == 10.0


def test_shift_cues_before_any_cut_unchanged():
    # cue at edit 3 (clip0 안, 컷보다 앞) → shift 없음
    clips = [_clip(0, 10), _clip(20, 30)]
    results = [
        SilenceCutResult(original_clip=clips[0], keep_intervals=[Interval(0, 5)], total_removed_sec=5.0),
        SilenceCutResult(original_clip=clips[1], keep_intervals=[Interval(20, 30)], total_removed_sec=0.0),
    ]
    cues = [{"start_sec": 3.0, "end_sec": 4.5, "text": "x", "voice": "ko_female", "speed": "normal"}]
    out = _shift_cues_by_silence_cut(cues, results, clips)
    # cue 가 clip0 안에 있으므로 clip0 의 removed 영향 없음 (clip0 끝 이전)
    assert out[0]["start_sec"] == 3.0
    assert out[0]["end_sec"] == 4.5


def test_shift_cues_accumulates_removed_across_clips():
    # clip0 = 10s (-3 removed), clip1 = 10s (-2 removed), clip2 = 10s. cue at edit 25 → -5 → 20
    clips = [_clip(0, 10), _clip(20, 30), _clip(50, 60)]
    results = [
        SilenceCutResult(original_clip=clips[0], keep_intervals=[Interval(0, 7)], total_removed_sec=3.0),
        SilenceCutResult(original_clip=clips[1], keep_intervals=[Interval(20, 28)], total_removed_sec=2.0),
        SilenceCutResult(original_clip=clips[2], keep_intervals=[Interval(50, 60)], total_removed_sec=0.0),
    ]
    cues = [{"start_sec": 25.0, "end_sec": 27.0, "text": "x", "voice": "ko_female", "speed": "normal"}]
    out = _shift_cues_by_silence_cut(cues, results, clips)
    assert out[0]["start_sec"] == 20.0
    assert out[0]["end_sec"] == 22.0


def test_shift_cues_handles_empty_input():
    assert _shift_cues_by_silence_cut([], [], []) == []
    # cues 있지만 cut 결과 없음 → 그대로 (defensive)
    cues = [{"start_sec": 5.0, "end_sec": 7.0, "text": "x", "voice": "ko_female", "speed": "normal"}]
    out = _shift_cues_by_silence_cut(cues, [], [])
    assert out == cues


# ──────────────────────────────────────────────────────────────
# PR-6: 프로파일 해석 + gap-단위 무음 컷 (aggressive)
# ──────────────────────────────────────────────────────────────


def test_get_silence_profile_resolves_names():
    assert get_silence_profile("conservative") is CONSERVATIVE_PROFILE
    assert get_silence_profile("aggressive") is AGGRESSIVE_PROFILE
    assert get_silence_profile("AGGRESSIVE") is AGGRESSIVE_PROFILE  # 대소문자 무관
    assert get_silence_profile("  aggressive  ") is AGGRESSIVE_PROFILE  # 공백 무관
    assert get_silence_profile(None) is CONSERVATIVE_PROFILE  # 미지정 → 베이스라인
    assert get_silence_profile("nonexistent") is CONSERVATIVE_PROFILE  # 미상 → 베이스라인


def test_profile_defaults_are_conservative_legacy():
    # 기본 프로파일은 기존 하드코딩 값과 정확히 일치해야 (베이스라인 보존)
    p = CONSERVATIVE_PROFILE
    assert p.gap_level is False
    assert p.max_gap_sec == 0.4 and p.padding_sec == 0.15 and p.min_interval_sec == 0.3
    assert p.protect_missing_candidate and p.protect_action_verb and p.protect_speaker_change
    assert p.protected_gap_max_sec is None


# ── _is_gap_safe_to_cut 가 프로파일 토글을 따른다 ──


def test_aggressive_cuts_missing_candidate():
    # conservative 보호, aggressive 컷 허용
    assert _is_gap_safe_to_cut(None, _seg(0, 1), _seg(2, 3), CONSERVATIVE_PROFILE) is False
    assert _is_gap_safe_to_cut(None, _seg(0, 1), _seg(2, 3), AGGRESSIVE_PROFILE) is True


def test_aggressive_ignores_lone_action_verb():
    cand = _candidate(description="두 사람이 키스한다")
    assert _is_gap_safe_to_cut(cand, _seg(0, 1), _seg(2, 3), CONSERVATIVE_PROFILE) is False
    assert _is_gap_safe_to_cut(cand, _seg(0, 1), _seg(2, 3), AGGRESSIVE_PROFILE) is True


def test_aggressive_cuts_clip_boundary_gap():
    cand = _candidate()
    assert _is_gap_safe_to_cut(cand, None, _seg(2, 3), CONSERVATIVE_PROFILE) is False
    assert _is_gap_safe_to_cut(cand, None, _seg(2, 3), AGGRESSIVE_PROFILE) is True


def test_aggressive_still_protects_speaker_change_and_visual_essential():
    # 화자 변경은 aggressive 도 보호 (점프컷 방지) — cap 으로 상한만 건다
    cand = _candidate()
    assert _is_gap_safe_to_cut(
        cand, _seg(0, 1, "[내레이션] A"), _seg(2, 3, "대사"), AGGRESSIVE_PROFILE
    ) is False
    # visual_essential 은 프로파일 무관 항상 보호
    cand_ve = _candidate(visual_essential=True)
    assert _is_gap_safe_to_cut(cand_ve, _seg(0, 1), _seg(2, 3), AGGRESSIVE_PROFILE) is False


# ── gap-단위 통합 동작 ──


def test_gap_level_cuts_safe_gap_despite_one_protected_gap():
    """핵심 회귀 방지: 보호 대상 gap 1개가 다른 안전한 gap 의 컷을 막지 않는다."""
    clip = _clip(0, 30)
    segs = [
        _seg(0.5, 1.0, "대사 A"),
        _seg(12.0, 12.5, "대사 B"),        # A→B: 둘 다 대사 → 안전 → 컷
        _seg(24.0, 24.5, "[내레이션] 끝"),  # B→C: 화자 변경 → 보호 (cap 적용)
    ]
    lookup = {(0, 0): _candidate(description="평범한 장면")}

    # conservative: 보호 gap 하나라도 있으면 clip 통째 유지
    cons = cut_silence_with_story_filter([clip], segs, lookup, profile=CONSERVATIVE_PROFILE)
    assert cons[0].keep_intervals == [Interval(0.0, 30.0)]
    assert cons[0].total_removed_sec == 0.0

    # aggressive: A→B 컷됨(섬 분리) + B→C 보호하되 cap=1.0 으로 dead-air 상한
    aggr = cut_silence_with_story_filter([clip], segs, lookup, profile=AGGRESSIVE_PROFILE)
    ivs = aggr[0].keep_intervals
    assert len(ivs) == 3                      # 보호 gap 도 cap 으로 잘려 섬이 분리됨
    assert aggr[0].total_removed_sec > 25.0   # 대부분의 무음 제거
    # A→B 사이 무음이 실제로 제거됨 (섬1 끝 << 섬2 시작)
    assert ivs[0].end_sec < 2.0 and ivs[1].start_sec > 11.0
    # B→C 보호 gap 의 가운데 dead-air 가 잘려나감 (전체 10.5s 중 cap=1.0s 만 유지)
    cut_middle_gap = ivs[2].start_sec - ivs[1].end_sec
    assert cut_middle_gap > 9.0


def test_gap_level_protected_gap_without_cap_bridges_fully():
    # protected_gap_max_sec=None 이면 보호 gap 은 통째 유지(bridge)
    profile = replace(AGGRESSIVE_PROFILE, protected_gap_max_sec=None)
    clip = _clip(0, 30)
    segs = [
        _seg(0.5, 1.0, "대사 A"),
        _seg(12.0, 12.5, "대사 B"),
        _seg(24.0, 24.5, "[내레이션] 끝"),  # 보호 gap, cap 없음 → bridge
    ]
    lookup = {(0, 0): _candidate(description="평범한 장면")}
    out = cut_silence_with_story_filter([clip], segs, lookup, profile=profile)
    # A→B 만 컷 → 섬2 개: [A] [B..C 통째]
    assert len(out[0].keep_intervals) == 2


def test_gap_level_cuts_when_missing_candidate():
    # 메타 없음 — aggressive 는 긴 무음 컷
    clip = _clip(0, 12, chunk=99, cand=99)
    segs = [_seg(0.5, 1.0, "a"), _seg(9.0, 11.5, "b")]
    out = cut_silence_with_story_filter([clip], segs, {}, profile=AGGRESSIVE_PROFILE)
    assert len(out[0].keep_intervals) == 2
    assert out[0].total_removed_sec > 5.0


def test_gap_level_cuts_when_action_verb():
    # 단일 액션 동사 — aggressive 는 보호하지 않음
    clip = _clip(0, 12)
    segs = [_seg(0.5, 1.0, "안녕"), _seg(9.0, 11.5, "잘 가")]
    lookup = {(0, 0): _candidate(description="두 사람이 키스한다")}
    out = cut_silence_with_story_filter([clip], segs, lookup, profile=AGGRESSIVE_PROFILE)
    assert len(out[0].keep_intervals) == 2
    assert out[0].total_removed_sec > 5.0


def test_gap_level_respects_max_gap_threshold():
    # max_gap_sec(0.3) 미만 gap 은 유지(bridge), 이상 gap 만 컷
    clip = _clip(0, 10)
    segs = [
        _seg(0.5, 1.0, "A"),
        _seg(1.25, 1.75, "B"),   # A→B gap 0.25 < 0.3 → 유지
        _seg(5.0, 5.5, "C"),     # B→C gap 3.25 ≥ 0.3 → 컷
    ]
    lookup = {(0, 0): _candidate()}
    out = cut_silence_with_story_filter([clip], segs, lookup, profile=AGGRESSIVE_PROFILE)
    ivs = out[0].keep_intervals
    assert len(ivs) == 2          # A·B 한 섬, C 별도 섬
    assert ivs[0].start_sec < 0.5 and ivs[0].end_sec > 1.75  # A·B 가 한 구간으로 병합


def test_gap_level_still_protects_visual_essential_clip():
    clip = _clip(0, 30, visual_essential=True)
    segs = [_seg(0, 1), _seg(10, 11), _seg(20, 21)]
    lookup = {(0, 0): _candidate(visual_essential=True)}
    out = cut_silence_with_story_filter([clip], segs, lookup, profile=AGGRESSIVE_PROFILE)
    assert out[0].keep_intervals == [Interval(0.0, 30.0)]


def test_gap_level_keeps_no_segment_clip_whole():
    # 대사 없는 비주얼 비트는 aggressive 도 통째 유지 (컷하면 비트 소멸)
    clip = _clip(0, 8)
    lookup = {(0, 0): _candidate()}
    out = cut_silence_with_story_filter([clip], [], lookup, profile=AGGRESSIVE_PROFILE)
    assert out[0].keep_intervals == [Interval(0.0, 8.0)]
    assert out[0].total_removed_sec == 0.0


def test_aggressive_removes_more_silence_than_conservative():
    """동일 입력에서 aggressive 무음 제거량 ≥ conservative (가설의 방향성 보장)."""
    clips = [
        _clip(0, 30, chunk=0, cand=0),
        _clip(40, 70, chunk=1, cand=0),
    ]
    segs = [
        _seg(1.0, 2.0, "대사"), _seg(15.0, 16.0, "[내레이션] 전환"),  # clip0: 화자 변경 gap
        _seg(41.0, 42.0, "x"), _seg(55.0, 56.0, "y"),               # clip1: 메타 없는 내부 gap
    ]
    lookup = {(0, 0): _candidate(description="평범")}  # (1,0) 누락 → 메타 없음
    cons = cut_silence_with_story_filter(clips, segs, lookup, profile=CONSERVATIVE_PROFILE)
    aggr = cut_silence_with_story_filter(clips, segs, lookup, profile=AGGRESSIVE_PROFILE)
    cons_removed = sum(r.total_removed_sec for r in cons)
    aggr_removed = sum(r.total_removed_sec for r in aggr)
    assert aggr_removed > cons_removed
