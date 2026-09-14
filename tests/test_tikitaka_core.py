"""티키타카 파이프라인 — 순수 로직 회귀 가드 (ffmpeg·whisper·Gemini 없이 돈다)."""
from __future__ import annotations

import pytest

from app.tikitaka.common import fmt_tc, parse_tc, SAFE_MARGIN_SEC
from app.tikitaka.transcribe import words_to_lines
from app.tikitaka.scenecut import parse_showinfo_times, scene_bounds


# ── 타임코드 서식(제0-2원칙: MM:SS.ms) ─────────────────────────────────────
def test_fmt_tc_is_mm_ss_ms():
    assert fmt_tc(0) == "00:00.000"
    assert fmt_tc(53.5) == "00:53.500"
    assert fmt_tc(65.12) == "01:05.120"
    assert fmt_tc(3599.999) == "59:59.999"
    assert fmt_tc(-1) == "00:00.000"


def test_parse_tc_roundtrip_and_rejects_approximation():
    assert parse_tc("00:55.120") == pytest.approx(55.12)
    assert parse_tc(fmt_tc(1234.567)) == pytest.approx(1234.567)
    for bad in ("00:55", "55.12", "대략 1분", "1:2:3.4"):
        with pytest.raises(ValueError):
            parse_tc(bad)


def test_safe_margin_is_100ms():
    assert SAFE_MARGIN_SEC == 0.1


# ── 전사 단어 → 줄 재단 ───────────────────────────────────────────────────
def _w(i, s, e, t):
    return {"i": i, "start": s, "end": e, "text": t, "p": 0.9}


def test_words_to_lines_splits_on_gap_and_punct():
    words = [_w(0, 0.0, 0.3, "나"), _w(1, 0.35, 0.8, "나가고"), _w(2, 0.85, 1.2, "싶어."),
             _w(3, 1.3, 1.6, "왜?"), _w(4, 3.0, 3.4, "그냥"), _w(5, 3.45, 3.9, "그래")]
    lines = words_to_lines(words)
    assert [l["text"] for l in lines] == ["나 나가고 싶어.", "왜?", "그냥 그래"]
    assert lines[0]["id"] == "L-001" and lines[2]["id"] == "L-003"
    assert lines[0]["start"] == 0.0 and lines[0]["end"] == 1.2
    assert lines[0]["word_i"] == [0, 1, 2]
    assert all(l["speaker"] is None for l in lines)


def test_words_to_lines_empty():
    assert words_to_lines([]) == []


# ── 장면 컷 ────────────────────────────────────────────────────────────────
def test_parse_showinfo_times_dedups_and_sorts():
    stderr = "x pts_time:1.5 y\n pts_time:1.5 \n pts_time:3.25\n pts_time:2.0"
    assert parse_showinfo_times(stderr) == [1.5, 3.25]


def test_scene_bounds():
    cuts = [10.0, 20.0, 30.0]
    assert scene_bounds(cuts, 5.0, 100.0) == (0.0, 10.0)
    assert scene_bounds(cuts, 25.0, 100.0) == (20.0, 30.0)
    assert scene_bounds(cuts, 35.0, 100.0) == (30.0, 100.0)
    assert scene_bounds(cuts, 20.0, 100.0) == (20.0, 30.0)


# ── 타이밍 확정(순수) ───────────────────────────────────────────────────────
from app.tikitaka.timing import (narration_plan_sec, bind_dialogue, safe_shot, snap_moment, CutSource,
                                 stack_cuts, next_shots_after)
from app.tikitaka.index import make_windows, parse_window_result


def test_narration_plan_sec_is_4_chars_per_sec_excluding_spaces():
    # 지침서 예시: "승일이 결국 참지 못하고 폭발합니다." (공백 제외 16자) → 4.0초
    assert narration_plan_sec("승일이 결국 참지 못하고 폭발합니다.") == 4.0
    assert narration_plan_sec("") == 0.0


def test_bind_dialogue_uses_word_timestamps_with_lead_tail():
    words = [_w(0, 10.0, 10.3, "나"), _w(1, 10.35, 10.8, "나가고"), _w(2, 10.85, 11.2, "싶어."), _w(3, 12.0, 12.4, "왜?")]
    lines = {"L-001": {"id": "L-001", "text": "나 나가고 싶어.", "word_i": [0, 1, 2], "speaker": "승일"},
             "L-002": {"id": "L-002", "text": "왜?", "word_i": [3], "speaker": "규현"}}
    b = bind_dialogue(lines, words, ["L-001"])
    assert b["start"] == pytest.approx(9.95) and b["end"] == pytest.approx(11.35)
    assert b["text"] == "나 나가고 싶어." and b["speaker"] == "승일"
    b2 = bind_dialogue(lines, words, ["L-001", "L-002"])
    assert b2["end"] == pytest.approx(12.55) and b2["text"] == "나 나가고 싶어. 왜?"
    with pytest.raises(KeyError):
        bind_dialogue(lines, words, ["L-999"])


def test_safe_shot_applies_100ms_margins():
    cuts = [10.0, 20.0]
    assert safe_shot(cuts, 15.0, 100.0) == (10.1, 19.9)
    assert safe_shot([10.0, 10.2], 10.1, 100.0) == (10.0, 10.2)      # 0.3s 미만 샷은 마진 없음


def test_snap_moment_clamps_into_shot_and_keeps_min_len():
    cuts = [10.0, 20.0]
    assert snap_moment(cuts, 9.5, 12.0, 100.0) == (10.1, 12.0)        # 샷 시작을 넘은 앞부분을 자른다
    assert snap_moment(cuts, 19.7, 19.95, 100.0) == (19.4, 19.9)     # 0.5s 확보(샷 안에서 앞으로)


def test_stack_cuts_fills_target_with_1_to_2s_cuts():
    srcs = [CutSource("S-1", 100.0, 103.0, "a"), CutSource("S-2", 200.0, 201.5, "b"), CutSource("S-3", 300.0, 305.0, "c")]
    cuts, short = stack_cuts(srcs, 4.0)
    assert short == 0.0
    assert sum(c["dur"] for c in cuts) == pytest.approx(4.0)
    # S-1 2.0 + S-2 1.5(샷 끝) = 3.5 → 0.5 부족은 0.5s 플래시 컷을 만들지 않고 S-1 을 같은 샷 안에서 2.5 로 늘린다
    assert [c["src"] for c in cuts] == ["S-1", "S-2"] and all(c["dur"] >= 0.8 for c in cuts)
    assert cuts[0]["in"] == 100.0 and cuts[0]["out"] == 102.5 and cuts[1]["dur"] == 1.5


def test_stack_cuts_adds_third_source_when_remaining_is_a_real_cut():
    srcs = [CutSource("S-1", 100.0, 102.0, "a"), CutSource("S-2", 200.0, 202.0, "b"), CutSource("S-3", 300.0, 305.0, "c")]
    cuts, short = stack_cuts(srcs, 5.5)     # n=3 · base 1.83 → 1.83+1.83+1.83
    assert short == 0.0 and [c["src"] for c in cuts] == ["S-1", "S-2", "S-3"]
    assert sum(c["dur"] for c in cuts) == pytest.approx(5.5, abs=0.01)


def test_stack_cuts_extends_within_shot_when_sources_short():
    srcs = [CutSource("S-1", 100.0, 106.0, "a")]
    cuts, short = stack_cuts(srcs, 5.0)
    assert short == 0.0 and len(cuts) == 1 and cuts[0]["dur"] == 5.0     # 2.0 상한보다 슬로우모션 금지가 위


def test_stack_cuts_reports_shortfall():
    srcs = [CutSource("S-1", 100.0, 101.0, "a"), CutSource("S-2", 200.0, 200.8, "b")]
    cuts, short = stack_cuts(srcs, 5.0)
    assert short == pytest.approx(3.2) and sum(c["dur"] for c in cuts) == pytest.approx(1.8)


def test_next_shots_after():
    shots = next_shots_after([10.0, 12.0, 15.0], 10.0, 100.0, count=2)
    assert shots == [(10.1, 11.9), (12.1, 14.9)]


# ── 인덱스 창·응답 파싱 ────────────────────────────────────────────────────
def test_make_windows_merges_short_tail():
    assert make_windows(4062.8) == [(0.0, 600.0), (600.0, 1200.0), (1200.0, 1800.0), (1800.0, 2400.0),
                                    (2400.0, 3000.0), (3000.0, 3600.0), (3600.0, 4062.8)]
    assert make_windows(650.0) == [(0.0, 650.0)]


def test_parse_window_result_offsets_and_validates():
    raw = {"speakers": {"L-001": "강비호", "L-999": "x", "L-002": ""},
           "scenes": [{"start": "00:00.000", "end": "01:00.000", "place": "카페", "summary": "s", "chars": ["강비호"]},
                      {"start": "bad", "end": "00:10.000"}],
           "moments": [{"start": "00:05.000", "end": "00:06.500", "kind": "reaction", "who": "홍재인", "desc": "d", "sound": None},
                       {"start": "00:07.000", "end": "00:07.000", "kind": "action", "who": None, "desc": "문 쾅", "sound": "쾅"},
                       {"start": "00:08.000", "end": "00:09.000", "kind": "weird"}],
           "synopsis": "요약"}
    r = parse_window_result(raw, 600.0, 1200.0, {"L-001", "L-002"})
    assert r["speakers"] == {"L-001": "강비호"}
    assert r["scenes"] == [{"start": 600.0, "end": 660.0, "place": "카페", "summary": "s", "chars": ["강비호"]}]
    assert r["moments"][0]["start"] == 605.0 and r["moments"][0]["end"] == 606.5
    assert r["moments"][1]["end"] == 608.0 and r["moments"][1]["sound"] == "쾅"     # end<=start → +1.0
    assert len(r["moments"]) == 2 and r["dropped"] == 2


def test_scenecut_cache_is_keyed_by_source_name(tmp_path, monkeypatch):
    """1fps 스캔 프록시로 만든 캐시를 10fps 컷 프록시 검출이 재사용하면 안 된다(2026-09-10)."""
    import json
    from app.tikitaka.common import Job
    from app.tikitaka import scenecut as C
    job = Job(source=tmp_path / "src.mp4", out_dir=tmp_path, title="t")
    job.save("scenecuts.json", {"threshold": 0.3, "source": "scan_360p_1fps.mp4", "cuts": [4.0, 13.0]})
    assert C.detect_scene_cuts(job, tmp_path / "scan_360p_1fps.mp4") == [4.0, 13.0]          # 같은 소스 → 재사용
    calls = []
    monkeypatch.setattr(C, "find_bin", lambda n: "ffmpeg")
    class R:  # ffmpeg 대역
        returncode = 0
        stderr = "pts_time:4.3\npts_time:12.7"
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: calls.append(a) or R())
    assert C.detect_scene_cuts(job, tmp_path / "cut_480p_10fps.mp4") == [4.3, 12.7]         # 다른 소스 → 재검출
    assert json.load(open(tmp_path / "scenecuts.json"))["source"] == "cut_480p_10fps.mp4"


def test_nudge_and_action_window_keep_span_but_avoid_boundaries():
    from app.tikitaka.timing import nudge_in, nudge_out, action_window
    cuts = [2592.2, 2592.9, 2593.7]
    assert nudge_in(cuts, 2592.15) == 2592.3 and nudge_in(cuts, 2592.5) == 2592.5
    assert nudge_out(cuts, 2593.65) == 2593.6 and nudge_out(cuts, 2593.4) == 2593.4
    # 악보 던지기: 제안 2592.1~2593.4 는 컷 2개를 가로지른다 — 한 샷에 가두지 않고 양 끝만 뗀다
    assert action_window(cuts, 2592.1, 2593.4, 4000.0) == (2592.3, 2593.4)
    # 너무 짧으면 뒤로 늘리되 경계는 피한다
    assert action_window(cuts, 2592.3, 2592.5, 4000.0, min_sec=0.8) == (2592.3, 2593.1)


def test_scene_threshold_adapts_when_cuts_are_too_sparse(tmp_path, monkeypatch):
    """어두운 소스는 0.3 에서 컷이 거의 안 잡힌다(2026-09-13 3화 50개/50분) → 분당 4개 미만이면 0.2·0.15·0.1 로 낮춰 다시 잰다."""
    import json
    from app.tikitaka.common import Job
    from app.tikitaka import scenecut as C
    assert C.pick_threshold({0.3: 50, 0.2: 105, 0.15: 184, 0.1: 287}, 2990.0) == 0.1       # 3화 실측: 0.15 는 3.7/분 → 0.1
    assert C.pick_threshold({0.3: 162}, 2871.0) == 0.3 or C.pick_threshold({0.3: 200}, 2871.0) == 0.3
    assert C.pick_threshold({0.3: 300}, 3000.0) == 0.3                                        # 6/분 → 그대로
    assert C.pick_threshold({0.3: 10, 0.2: 20, 0.15: 30, 0.1: 40}, 3000.0) == 0.1              # 다 못 넘으면 가장 낮은 것
    job = Job(source=tmp_path / "src.mp4", out_dir=tmp_path, title="t")
    job.save("probe.json", {"duration_sec": 600.0})
    monkeypatch.setattr(C, "find_bin", lambda n: "ffmpeg")
    calls = []

    def fake_run(argv, **k):
        th = float(argv[argv.index("-vf") + 1].split("gt(scene,")[1].split(")")[0])
        calls.append(th)
        n = {0.3: 5, 0.2: 12, 0.15: 30, 0.1: 60}[th]
        class R:
            returncode = 0
            stderr = "\n".join(f"pts_time:{i * (600.0 / n):.1f}" for i in range(1, n + 1))
        return R()
    monkeypatch.setattr(C.subprocess, "run", fake_run)
    cuts = C.detect_scene_cuts(job, tmp_path / "cut_480p_10fps.mp4")
    assert len(cuts) == 60 and calls == [0.3, 0.2, 0.15, 0.1]                                  # 0.15 는 3/분 → 0.1(6/분)에서 멈춤
    saved = json.load(open(tmp_path / "scenecuts.json"))
    assert saved["threshold"] == 0.1 and saved["requested"] == 0.3 and saved["trials"]["0.3"] == 5
    assert C.detect_scene_cuts(job, tmp_path / "cut_480p_10fps.mp4") == cuts and len(calls) == 4   # 캐시(요청 임계 기준) 재사용
