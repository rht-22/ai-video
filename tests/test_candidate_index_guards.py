"""candidate_index 결손 대응 — schema validator + dedup guard 동시 검증.

cut-2 운영 중 발견된 버그: Gemini 가 토큰 한도/format drift 로 일부 모먼트의
`candidate_index` 를 빠뜨리면 `_dedup_boundary_candidates` 가 `int(None)` 으로
크래시 했다. (1) 스키마 검증에서 누락분 자동 보정, (2) dedup 진입 시 가드.
"""
from __future__ import annotations

import pytest

from app.modules.gemini_client import _validate_gemini_schema
from app.pipeline import _dedup_boundary_candidates


# ──────────────────────────────────────────────────────────────
# _validate_gemini_schema — candidate_index 결손/None/충돌 보정
# ──────────────────────────────────────────────────────────────


def _moment(idx=None, start=0.0, end=5.0, text="x"):
    m = {"start_sec": start, "end_sec": end, "description": text}
    if idx is not None:
        m["candidate_index"] = idx
    return m


def _payload(moments):
    return {
        "chunk_index": 0,
        "chunk_start_sec": 0.0,
        "chunk_end_sec": 100.0,
        "summary": "test",
        "candidate_moments": moments,
    }


def test_validator_fills_missing_candidate_index():
    data = _payload([
        _moment(idx=0, start=0, end=5),
        _moment(idx=None, start=10, end=15),  # 누락 → 채워야 함
        _moment(idx=2, start=20, end=25),
    ])
    _validate_gemini_schema(data)
    indices = [m["candidate_index"] for m in data["candidate_moments"]]
    assert indices == [0, 1, 2]


def test_validator_fills_none_candidate_index():
    data = _payload([
        _moment(idx=0, start=0, end=5),
        _moment(idx=None, start=10, end=15),
        _moment(idx=None, start=20, end=25),
    ])
    data["candidate_moments"][1]["candidate_index"] = None  # 명시적 None
    _validate_gemini_schema(data)
    indices = [m["candidate_index"] for m in data["candidate_moments"]]
    assert len(set(indices)) == 3, "모든 index가 유니크해야 함"
    assert all(isinstance(i, int) and i >= 0 for i in indices)


def test_validator_preserves_existing_indices_when_assigning_missing():
    # 0, 1, ?, 5 → 0, 1, 2, 5 (가장 작은 빈 자리)
    data = _payload([
        _moment(idx=0, start=0, end=5),
        _moment(idx=1, start=10, end=15),
        _moment(idx=None, start=20, end=25),
        _moment(idx=5, start=30, end=35),
    ])
    _validate_gemini_schema(data)
    indices = [m["candidate_index"] for m in data["candidate_moments"]]
    assert indices == [0, 1, 2, 5]


def test_validator_reassigns_duplicate_candidate_index():
    data = _payload([
        _moment(idx=0, start=0, end=5),
        _moment(idx=0, start=10, end=15),  # 중복 → 뒤쪽이 재할당
    ])
    _validate_gemini_schema(data)
    indices = [m["candidate_index"] for m in data["candidate_moments"]]
    assert len(set(indices)) == 2


def test_validator_handles_non_int_candidate_index():
    data = _payload([
        _moment(idx=0, start=0, end=5),
        _moment(idx="bad", start=10, end=15),
    ])
    _validate_gemini_schema(data)
    indices = [m["candidate_index"] for m in data["candidate_moments"]]
    assert all(isinstance(i, int) for i in indices)
    assert indices == [0, 1]


# ──────────────────────────────────────────────────────────────
# _dedup_boundary_candidates — candidate_index 누락 시 크래시 안 남
# ──────────────────────────────────────────────────────────────


def _cand(chunk, idx, start, end):
    c = {"chunk_index": chunk, "start_sec": start, "end_sec": end}
    if idx is not None:
        c["candidate_index"] = idx
    return c


def test_dedup_skips_candidate_missing_candidate_index():
    # 옛 버그 재현: candidate_index 없는 candidate 가 dedup 대상에 끼면 int(None) 크래시
    candidates = [
        _cand(0, 0, 100, 200),
        _cand(1, None, 150, 250),  # 결손
        _cand(1, 1, 300, 400),
    ]
    # 크래시 없이 통과해야 한다
    alias = _dedup_boundary_candidates(candidates)
    # 결손 candidate 는 dedup 후보에서 빠지므로 alias 가 생기더라도 그 candidate 는 master 도 slave 도 아니어야 한다
    for slave_key, master_key in alias.items():
        assert None not in slave_key
        assert None not in master_key


def test_dedup_skips_candidate_with_none_chunk_index():
    candidates = [
        _cand(0, 0, 100, 200),
        {"chunk_index": None, "candidate_index": 0, "start_sec": 150, "end_sec": 250},
        _cand(1, 0, 150, 250),
    ]
    alias = _dedup_boundary_candidates(candidates)
    # 정상 candidate 두 개끼리만 dedup 가능
    assert all(None not in k for k in alias.keys())
    assert all(None not in v for v in alias.values())


def test_dedup_skips_candidate_with_string_index():
    candidates = [
        _cand(0, 0, 100, 200),
        {"chunk_index": 1, "candidate_index": "oops", "start_sec": 150, "end_sec": 250},
    ]
    # 크래시 없이 통과 — 이상 값은 그냥 빠짐
    alias = _dedup_boundary_candidates(candidates)
    assert isinstance(alias, dict)
