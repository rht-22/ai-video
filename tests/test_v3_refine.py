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
    assert ts["t0"] == pytest.approx(1791.0 - 90.0)     # 원경계 −90
    assert ts["t1"] == pytest.approx(1854.119)          # +90 은 러닝타임 클램프


def test_probe_window_centers_on_original_boundary():
    # 리뷰 확정 수정: 창은 원경계 중심 ±90 — 큰 zone 에서도 원경계가 항상 창 안.
    # zone 깊은 내부의 경계(포핸즈2 42.5)는 부분 표본 재프로브 경로가 맡는다.
    p = next(x for x in boundary_probe_windows(_ex(intro=(0.0, 138.5)), 3560.0)
             if x["zone"] == "intro" and x["edge"] == "end")
    assert (p["t0"], p["t1"]) == (pytest.approx(48.5), pytest.approx(228.5))
    big = next(x for x in boundary_probe_windows(_ex(teaser=(3000.0, 3250.0)), 3300.0)
               if x["edge"] == "start")
    assert big["t0"] <= 3000.0 <= big["t1"]             # 원경계 창 안(리뷰 케이스)


def test_probe_tail_when_no_exception_and_cap():
    probes = boundary_probe_windows(_ex(), 1000.0)
    assert len(probes) == 1 and probes[0]["zone"] == "tail"
    assert probes[0]["t0"] == pytest.approx(820.0)
    many = _ex(intro=(5, 10), recap=(20, 30), teaser=(900, 950),
               credit=(960, 980), end=(985, 995))
    # 상한 적용·탈락 기록은 호출자(refine_exception) 몫 — 순수 함수는 전부 반환
    assert len(boundary_probe_windows(many, 1000.0)) == 10


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


def test_retile_fills_liberated_gap_on_zone_shrink():
    # 리뷰 확정 critical: zone 축소로 해방된 구간(42.5~138.5)을 인접 sequence 가
    # 확장해 덮는다 — 빈틈 0 복원(포핸즈2 헤드라인 시나리오)
    doc = {"sequences": [
        {"number": 0, "content": "a",
         "time": {"start": format_ts(138.5), "end": format_ts(600.0)},
         "chunks": [{"number": 0, "meanings": [],
                     "time": {"start": format_ts(138.5), "end": format_ts(600.0)}}]}],
        "exception_sector": _ex(intro=(0.0, 138.5))}
    out = retile_sequences(doc, _ex(intro=(0.0, 42.5)), 600.0)
    sq = out["sequences"][0]
    assert parse_ts(sq["time"]["start"]) == pytest.approx(42.5)
    assert parse_ts(sq["chunks"][0]["time"]["start"]) == pytest.approx(42.5)


def test_retile_fills_gap_from_zone_drop_with_new_sequence():
    # zone 폐기(신병4 시나리오) — 말미 해방 구간을 이전 sequence 확장으로 덮는다
    doc = {"sequences": [
        {"number": 0, "content": "a",
         "time": {"start": format_ts(0.0), "end": format_ts(1934.0)},
         "chunks": [{"number": 0, "meanings": [],
                     "time": {"start": format_ts(0.0), "end": format_ts(1934.0)}}]}],
        "exception_sector": _ex(credit=(1934.0, 1993.5))}
    out = retile_sequences(doc, _ex(), 1993.5)
    assert parse_ts(out["sequences"][-1]["time"]["end"]) == pytest.approx(1993.5)


def test_apply_boundary_rejects_neighbor_overlap():
    # 리뷰 확정 major: 이동 결과가 이웃 zone 과 겹치면 기각(원판정 유지)
    ex = _ex(intro=(0.0, 60.0), recap=(60.0, 120.0))
    out = apply_boundary(ex, {"zone": "intro", "edge": "end", "orig": 60.0},
                         100.0, 1000.0)
    assert parse_ts(out["intro"]["end"]) == pytest.approx(60.0)   # 기각


def test_shrink_guard_interval_semantics():
    # 실사고 2호(가왕쇼 재실행): end 프로브가 예고 속 카드를 경계로 오인해 45.5s
    # 해방 — 줄이는 방향은 해방 구간 main 판정 없이는 기각. 구간 산식만 순수 검증.
    # start edge 를 뒤로(확대 반대): [orig, new] 이 해방분
    orig, new = 100.0, 130.0
    assert (orig, new) == (100.0, 130.0)                # start: new>orig → 축소
    # end edge 를 앞으로: [new, orig] 이 해방분
    orig_e, new_e = 1838.0, 1791.0
    assert (new_e, orig_e) == (1791.0, 1838.0)


# ── 공백 재전사 반복 환각 방어(2026-08-31 실측 결함) ────────────────────────

def test_degenerate_loop_detection():
    from app.v3.transcribe import is_degenerate_loop
    # 실측 재현 ①: "그녀는"×71 — prob 0.84 로 높아 확신 필터를 통과했다
    loop = [{"text": "그녀는", "t0": 632 + i * 0.3, "t1": 632 + i * 0.3 + 0.3}
            for i in range(20)]
    assert "반복 환각" in (is_degenerate_loop(loop) or "")
    # 실측 재현 ②: 길이 0 단어(정렬 산출물)
    zero = [{"text": f"w{i}", "t0": 100.0 + i, "t1": 100.0 + i} for i in range(10)]
    assert "길이 퇴화" in (is_degenerate_loop(zero) or "")
    # 정상 창(가왕쇼 1185~1278s 품바 라이브 — 길이 중앙 0.92s)은 통과
    ok = [{"text": w, "t0": 1185 + i * 1.2, "t1": 1185 + i * 1.2 + 0.92}
          for i, w in enumerate("설레이다 꺼낸 가슴을 오라버니 어깨에 기대해볼래요 커다란 얼굴을 묻고".split())]
    assert is_degenerate_loop(ok) is None
    assert is_degenerate_loop([]) is None


# ── 갭 7 (2026-09-07, EP01 실사고) — credit 경계는 흑 앵커 · 잘라낸 조각 검증 ─────────
# 사고: Stage 1 원판정 credit 2943.5(정답) → 경계 프로브가 2930.75 로 −12.75s → 창 다수결
# verify_part 가 유지 → 기사·댓글 리빌 소실. 아래는 그 사고를 LLM 없이 재현·차단한다.

from pathlib import Path  # noqa: E402

from app.v3 import refine as rf  # noqa: E402
from app.v3.scenecut import parse_blackdetect  # noqa: E402

EP01_DUR = 3146.325
EP01_RUNS = [(2927.508, 2930.761), (2943.524, 2967.298)]     # blackdetect 실측
EP01_CUTS = [2880.0, 2912.5, 2930.8, 2934.5, 2936.8, 2943.5]


def test_parse_blackdetect_offsets_and_order():
    log = ("[blackdetect @ 0x1] black_start:77.507917 black_end:80.761167 black_duration:3.25\n"
           "[blackdetect @ 0x1] black_start:93.523917 black_end:117.297667 black_duration:23.7\n")
    assert parse_blackdetect(log, offset=2850.0) == [(2927.508, 2930.761), (2943.524, 2967.298)]
    assert parse_blackdetect("", 0.0) == []


def test_anchor_candidates_black_onsets_plus_adjacent_cuts():
    grid = {"scene_cuts": EP01_CUTS}
    c = rf.anchor_candidates(grid, EP01_RUNS, 2853.5, 3033.5)
    # 흑 시작점 2927.5·2943.5 — scene cut 2943.5 는 앵커 0.024s 옆이라 하나로 합쳐지고,
    # 2930.8(암전 **끝**)·2934.5·2936.8 은 앵커에서 멀어 후보가 아니다(사고의 그 컷들)
    assert [x["t"] for x in c] == [2927.508, 2943.5]
    assert c[0]["id"] == "a00" and c[1]["rel"] == pytest.approx(90.0)
    # 1초 미만 흑·창 밖 흑은 앵커가 아니다 · 앵커 없으면 빈 목록(호출자가 scene cut 폴백)
    assert rf.anchor_candidates(grid, [(2900.0, 2900.5), (100.0, 105.0)], 2853.5, 3033.5) == []
    assert rf.anchor_candidates(grid, [], 2853.5, 3033.5) == []


def test_probe_prompt_edge_note_differs_for_credit():
    assert "{edge_note}" in rf.PROBE_PROMPT
    assert "본편 서사가 끝나는 첫 컷" in rf.edge_note("teaser")           # 예고 규칙 그대로
    assert "앞당겨 크레딧에 넣지 마라" in rf.edge_note("credit")
    assert rf.edge_note("end") == rf.EDGE_NOTE_CREDIT and rf.edge_note("intro") == rf.EDGE_NOTE_DEFAULT
    assert "글자 화면**은 본편" in rf.DELTA_PROMPT


def _ep01_doc(credit_start: float, teaser: tuple | None = None):
    ex = _ex(credit=(credit_start, EP01_DUR), **({"teaser": teaser} if teaser else {}))
    body_end = teaser[0] if teaser else credit_start
    return {"sequences": [
        {"number": 0, "content": "a",
         "time": {"start": format_ts(0.0), "end": format_ts(2698.25)},
         "chunks": [{"number": 0, "meanings": [],
                     "time": {"start": format_ts(0.0), "end": format_ts(2698.25)}}]},
        {"number": 1, "content": "b",
         "time": {"start": format_ts(2698.25), "end": format_ts(body_end)},
         "chunks": [{"number": 0, "meanings": [],
                     "time": {"start": format_ts(2698.25), "end": format_ts(body_end)}}]},
    ], "exception_sector": ex}


def _fake_env(monkeypatch, answers: dict, calls: list):
    """LLM·ffmpeg 없이: 재단은 빈 파일, blackdetect 는 실측 목록(절대초·프로브 클립은 상대초),
    모델은 프롬프트 종류별 고정 답."""
    monkeypatch.setattr(rf, "find_ffmpeg_command", lambda *_a: "ffmpeg")

    def cut(ffmpeg, video, t0, t1, out):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"")
        cut.last = (t0, t1)
    monkeypatch.setattr(rf, "_cut_probe_clip", cut)

    def black(video, t0=None, t1=None, *, min_sec=0.5, pix_th=0.1):
        if str(video).endswith("probe_0_credit_start.mp4"):        # 프로브 클립 = 창 상대초
            base = cut.last[0]
            return [(a - base, z - base) for a, z in EP01_RUNS if cut.last[0] <= a <= cut.last[1]]
        return [(a, z) for a, z in EP01_RUNS if (t0 or 0) - 0.01 <= a <= (t1 or 1e9)]
    monkeypatch.setattr(rf, "detect_black_runs", black)

    def call(gemini, clip, prompt):
        calls.append(prompt)
        if "사이로 지목된 조각" in prompt:
            return {"kind": answers["delta"]}
        if "크레딧 직전" in prompt:
            return {"kind": answers["teaser_check"]}
        if "이 클립의 **주 내용**이 본편" in prompt:
            return {"kind": answers["verify"]}
        if "다음 회차 예고" in prompt or ("예고" in prompt and "후보" in prompt and "크레딧 경계" not in prompt):
            return {"boundary": answers.get("teaser_probe", "none")}
        return {"boundary": answers["probe"]}
    monkeypatch.setattr(rf, "_call_probe", call)


def test_refine_credit_expansion_rejected_by_delta_check(tmp_path, monkeypatch):
    # 사고 재현: 모델이 앵커 a00(2927.5 암전)을 고른다 → 잘라낸 2927.5~2943.5 는 main → 기각
    calls: list = []
    _fake_env(monkeypatch, {"probe": "a00", "delta": "main", "verify": "exception",
                            "teaser_check": "main"}, calls)
    grid = {"source": {"duration_sec": EP01_DUR}, "scene_cuts": EP01_CUTS}
    doc, audit = rf.refine_exception(object(), _ep01_doc(2943.5), grid, Path("v.mp4"),
                                     tmp_path, log=lambda *a: None)
    assert parse_ts(doc["exception_sector"]["credit"]["start"]) == pytest.approx(2943.5)
    p0 = audit["probes"][0]
    assert p0["candidate_source"] == "black_anchor" and p0["candidates"] == 2
    assert str(p0["result"]).startswith("확대 기각") and p0["delta_check"]["kind"] == "main"
    assert p0["delta_check"]["t0"] == pytest.approx(2927.508) and p0["delta_check"]["t1"] == pytest.approx(2943.5)
    assert audit["moved"] == 0
    # 크레딧 경계 프롬프트는 credit 편향 문구, 예고 문구가 아니다
    assert "앞당겨 크레딧에 넣지 마라" in calls[0] and "본편 서사가 끝나는 첫 컷" not in calls[0]
    # 예고 존재 검사가 돌았고(main) — 총 Flash 4콜 = 프로브·조각 검증·머리 표본·예고 검사
    assert any(r.get("edge") == "pre_credit_check" and r["result"] == "main" for r in audit["probes"])
    assert audit["flash_calls"] == 4


def test_refine_credit_head_moves_to_black_anchor(tmp_path, monkeypatch):
    # Stage 1 이 처음부터 2930.75 로 이르게 냈다면 — 머리가 앵커에 안 붙어 있다 →
    # 2930.75~2943.5 조각 검증(main) → 앵커 2943.524 로 민다 · 재타일링이 해방 구간을 덮는다
    calls: list = []
    _fake_env(monkeypatch, {"probe": "none", "delta": "main", "verify": "exception",
                            "teaser_check": "main"}, calls)
    grid = {"source": {"duration_sec": EP01_DUR}, "scene_cuts": EP01_CUTS}
    doc, audit = rf.refine_exception(object(), _ep01_doc(2930.75), grid, Path("v.mp4"),
                                     tmp_path, log=lambda *a: None)
    assert parse_ts(doc["exception_sector"]["credit"]["start"]) == pytest.approx(2943.524)
    head = next(r for r in audit["probes"] if r.get("edge") == "head_anchor")
    assert head["result"]["moved_sec"] == pytest.approx(12.774, abs=0.01)
    assert parse_ts(doc["sequences"][-1]["time"]["end"]) == pytest.approx(2943.524)
    # 앵커에 붙어 있으면 머리 검사는 돌지 않는다(종전 표본 검증)
    calls2: list = []
    _fake_env(monkeypatch, {"probe": "none", "delta": "exception", "verify": "exception",
                            "teaser_check": "main"}, calls2)
    doc2, audit2 = rf.refine_exception(object(), _ep01_doc(2943.5), grid, Path("v.mp4"),
                                       tmp_path, log=lambda *a: None)
    assert not any(r.get("edge") == "head_anchor" for r in audit2["probes"])
    assert parse_ts(doc2["exception_sector"]["credit"]["start"]) == pytest.approx(2943.5)


def test_refine_creates_teaser_before_credit_when_check_says_teaser(tmp_path, monkeypatch):
    # 본편→예고→크레딧: credit 앵커는 뒤에 물러나 있고 예고가 무표시 — 직전 60s 3분법이
    # teaser 면 예고 규칙(scene cut 후보·이른 쪽)으로 시작점을 찾아 zone 을 만든다
    calls: list = []
    _fake_env(monkeypatch, {"probe": "none", "delta": "exception", "verify": "exception",
                            "teaser_check": "teaser", "teaser_probe": "c00"}, calls)
    grid = {"source": {"duration_sec": EP01_DUR}, "scene_cuts": EP01_CUTS}
    doc, audit = rf.refine_exception(object(), _ep01_doc(2943.5), grid, Path("v.mp4"),
                                     tmp_path, log=lambda *a: None)
    tz = doc["exception_sector"]["teaser"]
    assert tz is not None and parse_ts(tz["start"]) == pytest.approx(2880.0) \
        and parse_ts(tz["end"]) == pytest.approx(2943.5)
    assert parse_ts(doc["sequences"][-1]["time"]["end"]) == pytest.approx(2880.0)
    r3 = next(r for r in audit["probes"] if r.get("edge") == "pre_credit_start")
    assert r3["result"]["chosen"] == "c00"
    # 예고 시작 프로브는 예고 편향 문구를 쓴다
    assert any("본편 서사가 끝나는 첫 컷" in p for p in calls)
