"""_validate_gemini_schema 의 candidate_index 결손 보정 검증.

배경: Gemini 가 일부 moment 에 candidate_index 를 빼먹는 케이스가 있어
다운스트림 _dedup_boundary_candidates 의 int(None) 크래시를 막아야 한다.
"""
from __future__ import annotations

import pytest

from app.modules.gemini_client import _validate_gemini_schema


def _base(moments):
    return {
        "chunk_index": 0,
        "chunk_start_sec": 0.0,
        "chunk_end_sec": 600.0,
        "summary": "s",
        "candidate_moments": moments,
    }


def _m(start, end, *, ci=None, desc="d"):
    """min required + optional candidate_index."""
    m = {"start_sec": start, "end_sec": end, "description": desc}
    if ci is not None:
        m["candidate_index"] = ci
    return m


def test_all_have_candidate_index_unchanged():
    d = _base([_m(0, 5, ci=0), _m(5, 10, ci=1), _m(10, 15, ci=2)])
    _validate_gemini_schema(d)
    assert [m["candidate_index"] for m in d["candidate_moments"]] == [0, 1, 2]


def test_missing_candidate_index_backfilled_unique():
    # ci=0 존재, 나머지 둘은 빠짐 → 1, 2 로 채워야 (충돌 없음)
    d = _base([_m(0, 5, ci=0), _m(5, 10), _m(10, 15)])
    _validate_gemini_schema(d)
    cis = [m["candidate_index"] for m in d["candidate_moments"]]
    assert cis[0] == 0
    assert set(cis) == {0, 1, 2}  # 유일성·정수성 보장
    assert all(isinstance(c, int) for c in cis)


def test_none_candidate_index_also_backfilled():
    # 명시적 None 도 결손으로 취급해 채워야 (EP07 케이스)
    d = _base([_m(0, 5, ci=0), {**_m(5, 10), "candidate_index": None},
               {**_m(10, 15), "candidate_index": None}])
    _validate_gemini_schema(d)
    cis = [m["candidate_index"] for m in d["candidate_moments"]]
    assert all(isinstance(c, int) for c in cis)
    assert len(set(cis)) == 3  # 충돌 없음


def test_backfill_avoids_collision_with_existing_high_ci():
    # 기존에 ci=5 가 있으면 결손은 6 부터 채워야 (max+1)
    d = _base([_m(0, 5, ci=5), _m(5, 10), _m(10, 15)])
    _validate_gemini_schema(d)
    cis = [m["candidate_index"] for m in d["candidate_moments"]]
    assert cis[0] == 5
    assert 5 not in cis[1:]
    assert len(set(cis)) == 3
