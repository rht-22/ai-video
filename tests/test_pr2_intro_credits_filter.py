"""PR-2 — 인트로/크레딧 청크 분석 통합 단위 테스트.

검증 대상:
1. _filter_candidates_by_chunk_intro_credits — chunk_meta_list 의 intro_credits_ranges 와
   겹치는 candidate 가 overlap_threshold 기준으로 제거되는지.
2. drop 동작의 경계 케이스 (ranges 비어 있음 / chunk_index 불일치 / 부분 겹침).

이 PR 의 또 다른 동작인 "is_intro_credits=true candidate 즉시 drop" 은 analyze_chunk 호출
루프 내부에 인라인으로 들어가므로 함수 단위 분리가 없음 → e2e 검증 영역. 본 파일에서는
필터 헬퍼 로직만 lock-in.
"""
from __future__ import annotations

from app.pipeline import _filter_candidates_by_chunk_intro_credits


def _cand(chunk_index, start, end, **extra):
    d = {"chunk_index": chunk_index, "start_sec": float(start), "end_sec": float(end)}
    d.update(extra)
    return d


def _meta(chunk_index, ranges):
    return {"chunk_index": chunk_index, "intro_credits_ranges": ranges}


def _range(start, end, kind="intro", confidence=0.9):
    return {"start_sec": float(start), "end_sec": float(end), "kind": kind, "confidence": confidence}


# ──────────────────────────────────────────────────────────────
# 통과 케이스
# ──────────────────────────────────────────────────────────────


def test_passes_through_when_no_chunk_meta():
    cands = [_cand(0, 10, 20), _cand(0, 30, 40)]
    assert _filter_candidates_by_chunk_intro_credits(cands, []) == cands


def test_passes_through_when_chunk_meta_has_no_ranges():
    cands = [_cand(0, 10, 20)]
    meta = [_meta(0, [])]
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == cands


def test_passes_through_when_chunk_index_differs():
    # chunk 0 에는 intro range 있지만 candidate 는 chunk 1 소속 → 영향 없음
    cands = [_cand(1, 5, 15)]
    meta = [_meta(0, [_range(0, 60)])]
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == cands


def test_passes_through_when_overlap_below_threshold():
    # candidate 30초, ranges 와 5초 겹침 = 16.7% < 50% → 통과
    cands = [_cand(0, 50, 80)]
    meta = [_meta(0, [_range(0, 55)])]  # overlap 5s of 30s
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == cands


# ──────────────────────────────────────────────────────────────
# Drop 케이스
# ──────────────────────────────────────────────────────────────


def test_drops_candidate_fully_inside_range():
    # candidate (10~20) 완전히 intro (0~60) 안에 포함 → overlap 100% → drop
    cands = [_cand(0, 10, 20)]
    meta = [_meta(0, [_range(0, 60)])]
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == []


def test_drops_candidate_at_exact_50pct_overlap():
    # candidate 10s, ranges 와 5s 겹침 = 50% → 임계값 (≥) 충족 → drop
    cands = [_cand(0, 5, 15)]  # 10s
    meta = [_meta(0, [_range(0, 10)])]  # overlap 5s
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == []


def test_keeps_just_below_threshold():
    # candidate 10s, ranges 와 4.99s 겹침 = 49.9% → 통과
    cands = [_cand(0, 5, 15)]
    meta = [_meta(0, [_range(0, 9.99)])]
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == cands


def test_drops_when_any_range_in_list_overlaps_enough():
    # 두 ranges 중 두 번째에만 70% 겹침 → drop
    cands = [_cand(0, 100, 110)]  # 10s
    meta = [_meta(0, [_range(0, 5), _range(99, 108)])]  # 두 번째와 8s 겹침 = 80%
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == []


def test_mixed_candidates_partially_filtered():
    cands = [
        _cand(0, 0, 30, candidate_index=0),    # intro 안 → drop
        _cand(0, 100, 130, candidate_index=1), # 정상 → 통과
        _cand(1, 0, 30, candidate_index=2),    # chunk 1 의 intro 안 → drop
        _cand(1, 200, 230, candidate_index=3), # 정상 → 통과
    ]
    meta = [
        _meta(0, [_range(0, 60)]),
        _meta(1, [_range(0, 50)]),
    ]
    out = _filter_candidates_by_chunk_intro_credits(cands, meta)
    kept_idx = [c.get("candidate_index") for c in out]
    assert kept_idx == [1, 3]


def test_input_order_preserved():
    cands = [
        _cand(0, 100, 130, tag="a"),
        _cand(0, 0, 30, tag="b-drop"),  # intro 안 → drop
        _cand(0, 200, 230, tag="c"),
        _cand(0, 300, 330, tag="d"),
    ]
    meta = [_meta(0, [_range(0, 60)])]
    out = _filter_candidates_by_chunk_intro_credits(cands, meta)
    tags = [c["tag"] for c in out]
    assert tags == ["a", "c", "d"]


def test_overlap_threshold_param_respected():
    # 30% 겹침 — 기본 0.5 에선 통과, threshold=0.2 면 drop
    cands = [_cand(0, 0, 10)]  # 10s
    meta = [_meta(0, [_range(0, 3)])]  # overlap 3s = 30%
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == cands
    assert _filter_candidates_by_chunk_intro_credits(cands, meta, overlap_threshold=0.2) == []


# ──────────────────────────────────────────────────────────────
# 견고성 (malformed input)
# ──────────────────────────────────────────────────────────────


def test_zero_duration_candidate_passes_through():
    # end == start → cdur=0 → drop 비교 불가, 통과로 안전 처리
    cands = [_cand(0, 50, 50)]
    meta = [_meta(0, [_range(0, 100)])]
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == cands


def test_missing_chunk_index_in_candidate_passes_through():
    # chunk_index 없는 candidate → -1 으로 fallback → cmap 없음 → 통과
    cands = [{"start_sec": 0.0, "end_sec": 10.0}]
    meta = [_meta(0, [_range(0, 60)])]
    assert _filter_candidates_by_chunk_intro_credits(cands, meta) == cands


def test_empty_candidates_returns_empty():
    meta = [_meta(0, [_range(0, 60)])]
    assert _filter_candidates_by_chunk_intro_credits([], meta) == []


def test_none_chunk_meta_list_passes_through():
    cands = [_cand(0, 10, 20)]
    assert _filter_candidates_by_chunk_intro_credits(cands, None) == cands
