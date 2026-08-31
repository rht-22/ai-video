"""V3-M8 — 불확실성 트리거식 정밀 재관찰 회귀 가드(LLM·whisper 없이).

계약: 트리거 명시(경계별 창 + exception 전무 말미), 호출 상한, 후보 id 검증,
경계 이동 시 재타일링(커버리지 유지·역전 방어), 공백 재전사의 트리거·무음 제외·
경계 중복 방지. 발주서 orders/v3-m8-uncertainty-refine.md.
"""
from __future__ import annotations

import pytest

from app.v3.refine import (
    MAX_PROBES,
    apply_boundary,
    boundary_probe_windows,
    retile_sequences,
    scene_cut_candidates,
    validate_probe_response,
)
from app.v3.schemas import format_ts, parse_ts


def _ex(**zones):
    base = {"intro": None, "recap": None, "teaser": None, "credit": None, "end": None}
    for k, (s, e) in zones.items():
        base[k] = {"start": format_ts(s), "end": format_ts(e)}
    return base


# ── 트리거(창 계산) ─────────────────────────────────────────────────────────

def test_probe_windows_per_edge_and_clamp():
    ex = _ex(intro=(0.0, 10.0), teaser=(1791.0, 1854.1))
    probes = boundary_probe_windows(ex, 1854.119)
    kinds = {(p["zone"], p["edge"]) for p in probes}
    # intro 시작(0)·teaser 끝(러닝타임 끝)은 검사 대상 아님
    assert kinds == {("intro", "end"), ("teaser", "start")}
    ts = next(p for p in probes if p["zone"] == "teaser")
    assert ts["t0"] == pytest.approx(1791.0 - 90.0)     # 본편 쪽 여유
    assert ts["t1"] == pytest.approx(1854.119)          # zone 전체(러닝타임 클램프)


def test_probe_window_covers_head_anchored_zone():
    # 포핸즈2 실측: intro[0,138.5] 과잉 — 진짜 경계 42.5 가 종전 창(48.5~) 밖.
    # 머리 고정 zone 의 end 프로브 창은 zone 전체를 덮어야 한다.
    ex = _ex(intro=(0.0, 138.5))
    p = next(x for x in boundary_probe_windows(ex, 3560.0)
             if x["zone"] == "intro" and x["edge"] == "end")
    assert p["t0"] == pytest.approx(0.0)
    assert p["t1"] == pytest.approx(180.0)              # 창 상한 180s(t0 기준)


def test_probe_tail_when_no_exception_and_cap():
    probes = boundary_probe_windows(_ex(), 1000.0)
    assert len(probes) == 1 and probes[0]["zone"] == "tail"
    assert probes[0]["t0"] == pytest.approx(820.0)
    many = _ex(intro=(5, 10), recap=(20, 30), teaser=(900, 950),
               credit=(960, 980), end=(985, 995))
    assert len(boundary_probe_windows(many, 1000.0)) == MAX_PROBES  # 상한


def test_scene_cut_candidates_ids_and_margin():
    grid = {"scene_cuts": [100.0, 150.5, 199.9, 220.0]}
    c = scene_cut_candidates(grid, 100.0, 200.0)
    assert [x["t"] for x in c] == [150.5, 199.9][:len(c)]
    assert c[0]["id"] == "c00" and c[0]["rel"] == pytest.approx(50.5)
    # 창 가장자리 0.2s 안 컷은 제외(창 경계 자체가 아님)
    assert 100.0 not in [x["t"] for x in c]


# ── 응답 검증·경계 적용 ─────────────────────────────────────────────────────

def test_probe_response_validation():
    cands = [{"id": "c00", "t": 1.0, "rel": 1.0}]
    assert validate_probe_response({"boundary": "c00"}, cands)[0] == "c00"
    assert validate_probe_response({"boundary": "none"}, cands)[0] == "none"
    bad, problems = validate_probe_response({"boundary": "c99"}, cands)
    assert bad is None and problems


def test_apply_boundary_moves_and_guards_inversion():
    ex = _ex(teaser=(1791.0, 1854.1))
    moved = apply_boundary(ex, {"zone": "teaser", "edge": "start", "orig": 1791.0},
                           1740.8, 1854.119)
    assert parse_ts(moved["teaser"]["start"]) == pytest.approx(1740.8)
    # 역전(시작을 끝 뒤로) — 기각, 원판정 유지
    same = apply_boundary(ex, {"zone": "teaser", "edge": "start", "orig": 1791.0},
                          1860.0, 1900.0)
    assert parse_ts(same["teaser"]["start"]) == pytest.approx(1791.0)


def test_apply_boundary_tail_creates_teaser():
    out = apply_boundary(_ex(), {"zone": "tail", "edge": "start", "orig": None},
                         900.0, 1000.0)
    assert parse_ts(out["teaser"]["start"]) == pytest.approx(900.0)
    assert parse_ts(out["teaser"]["end"]) == pytest.approx(1000.0)


# ── 재타일링 ────────────────────────────────────────────────────────────────

def _doc():
    return {"sequences": [
        {"number": 0, "content": "a",
         "time": {"start": format_ts(10.0), "end": format_ts(1000.0)},
         "chunks": [{"number": 0, "meanings": [],
                     "time": {"start": format_ts(10.0), "end": format_ts(600.0)}},
                    {"number": 1, "meanings": [],
                     "time": {"start": format_ts(600.0), "end": format_ts(1000.0)}}]},
        {"number": 1, "content": "b",
         "time": {"start": format_ts(1000.0), "end": format_ts(1791.0)},
         "chunks": [{"number": 0, "meanings": [],
                     "time": {"start": format_ts(1000.0), "end": format_ts(1791.0)}}]},
    ], "exception_sector": _ex(intro=(0, 10), teaser=(1791.0, 1854.1))}


def test_retile_shrinks_adjacent_sequence():
    new_ex = _ex(intro=(0, 10), teaser=(1740.8, 1854.1))     # 가왕쇼 수정 시나리오
    doc = retile_sequences(_doc(), new_ex, 1854.119)
    last = doc["sequences"][-1]
    assert parse_ts(last["time"]["end"]) == pytest.approx(1740.8)
    assert parse_ts(last["chunks"][-1]["time"]["end"]) == pytest.approx(1740.8)
    # 커버리지: sequences ∪ exception = 러닝타임(빈틈 0)
    total = sum(parse_ts(s["time"]["end"]) - parse_ts(s["time"]["start"])
                for s in doc["sequences"])
    ex_total = (10 - 0) + (1854.1 - 1740.8)
    assert total + ex_total == pytest.approx(1854.1, abs=0.05)


def test_retile_drops_swallowed_sequence_and_renumbers():
    # exception 이 sequence 하나를 통째로 삼키면 그 sequence 는 사라진다
    new_ex = _ex(intro=(0, 10), teaser=(1000.0, 1854.1))
    doc = retile_sequences(_doc(), new_ex, 1854.119)
    assert len(doc["sequences"]) == 1
    assert doc["sequences"][0]["number"] == 0
    assert parse_ts(doc["sequences"][0]["time"]["end"]) == pytest.approx(1000.0)


# ── 공백 재전사(트리거·무음 제외) ───────────────────────────────────────────

def test_gap_trigger_and_silence_skip(monkeypatch):
    from app.v3 import transcribe as tr
    calls = []

    class _FakeModel:
        def transcribe(self, *a, **kw):
            calls.append(kw["clip_timestamps"])
            return [], None
    monkeypatch.setattr(tr, "_get_whisper_model", lambda *a: _FakeModel())
    monkeypatch.setattr(tr, "_detect_device_and_compute", lambda: ("cpu", "int8"))
    words = [{"t0": 0.0, "t1": 2.0, "text": "a", "prob": 0.9},
             {"t0": 12.0, "t1": 13.0, "text": "b", "prob": 0.9},   # 공백 10s → 재전사
             {"t0": 16.0, "t1": 17.0, "text": "c", "prob": 0.9}]   # 공백 3s → 미달
    merged, audit = tr.retranscribe_gaps(
        # 꼬리 공백(17~40 = 23s)은 silencedetect 무음 — 건너뛴다
        __import__("pathlib").Path("/x.wav"), words, 40.0,
        silence=[(17.0, 40.0)], log=lambda *a: None)
    assert audit["gaps"] == 2 and audit["skipped_silence"] == 1
    assert len(calls) == 1                                          # 10s 공백만
    assert calls[0] == [max(0.0, 2.0 - 1.0), 12.0 + 1.0]
    assert merged == sorted(words, key=lambda w: (w["t0"], w["t1"]))
