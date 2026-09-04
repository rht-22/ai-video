"""V4-M5 다리 회귀 가드 — `app/v4/bridge.py`.

여기가 틀리면 승인된 후보가 **엉뚱한 구간으로 조립된다**. 그래서 이 파일은 값으로
고정한다: 비대칭 스냅의 앞/뒤 관용이 다른 것 · 관용 밖 원값 유지 · 중점 규칙이
`overrides.spans_in_window` 와 같은 답 · 인용 보호가 실제로 뮤트를 막는 것 ·
`span_index` 가 v3 소비자(`assemble.word_subtitles`·`story.plan_narration_slots`)에
**진짜로 먹히는 것**(열쇠 하나가 빠지면 KeyError 다).

LLM·네트워크 0 — 전부 순수 함수다.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.modules import timestamp_check
from app.modules.grid.timegrid import grid_snap_times
from app.v3 import assemble, story
from app.v3.overrides import spans_in_window
from app.v4 import bridge

DURATION = 100.0

# 유성 span 의 대사는 6단계 `transcript_block` 이 보여 주는 그 줄이다(인용의 원본).
QUOTE_A = "이건 정말 대단한 순간이었습니다"
QUOTE_B = "그래서 내가 그때 말했잖아 형"
QUOTE_C = "너 진짜 이럴 거야 지금부터"    # 정규화 12자 — MIN_QUOTE_CHARS 위


def _span(sid, t_in, t_out, *, audio, text=""):
    return {"id": sid, "t_in": t_in, "t_out": t_out, "is_audio": audio,
            "time_authority": "stt" if audio else "scene", "text": text}


def _words(t0, t1, text, prob, n=2):
    """[t0, t1) 을 n 등분한 단어들 — 중점이 span 안에 들도록."""
    step = (t1 - t0) / n
    return [{"t0": round(t0 + i * step, 3), "t1": round(t0 + (i + 1) * step, 3),
             "text": f"{text}{i}", "prob": prob} for i in range(n)]


def make_grid():
    """눈금 = span 경계 ∪ scene_cuts ∪ {0, 100} = [0,5,8,10,12,20,24,30,50,100]."""
    spans = [
        _span("sp0000", 0.0, 5.0, audio=False),
        _span("sp0001", 5.0, 8.0, audio=True, text=QUOTE_A),
        _span("sp0002", 8.0, 12.0, audio=True, text=QUOTE_B),
        _span("sp0003", 12.0, 20.0, audio=False),
        _span("sp0004", 20.0, 24.0, audio=True, text=QUOTE_C),
        _span("sp0005", 24.0, 30.0, audio=False),
    ]
    words = (_words(5.0, 8.0, "a", 0.9) + _words(8.0, 12.0, "b", 0.5)
             + _words(20.0, 24.0, "c", 0.7))
    # sp0001 은 확률 0.9/0.7 로 평균 0.8 이 나오게 한쪽만 바꾼다(평균 검증용)
    words[1]["prob"] = 0.7
    return {"source": {"duration_sec": DURATION},
            "scene_cuts": [10.0, 30.0, 50.0],
            "silence": [], "arousal": [],
            "words": words, "span_candidates": spans}


def seg(start, end, quote=None):
    return {"start_sec": start, "end_sec": end, "quote": quote}


# ── 1) 비대칭 스냅 ─────────────────────────────────────────────────────────

def test_snap_times_come_from_grid_snap_times():
    """눈금 목록을 다시 만들지 않는다 — 계약이 지목한 그 함수의 산출이다."""
    grid = make_grid()
    assert grid_snap_times(grid) == [0.0, 5.0, 8.0, 10.0, 12.0, 20.0, 24.0, 30.0,
                                     50.0, 100.0]


def test_start_snaps_back_far_but_forward_only_a_little():
    """시작 11.4 → **10.0**. 대칭 nearest 였다면 12.0(0.6 뒤)로 갔다 —
    뒤 관용이 0.5 라 12.0 이 창 밖이고, 앞 2.0 안의 10.0 이 남는다."""
    grid = make_grid()
    out, rec = bridge.snap_segments([seg(11.4, 30.0)], grid=grid,
                                    source_duration_sec=DURATION)
    assert out[0]["start_sec"] == 10.0
    assert min(abs(g - 11.4) for g in grid_snap_times(grid)) == pytest.approx(0.6)
    note = [r for r in rec if r["boundary"] == "start"][0]
    assert note["action"] == "snapped" and note["err"] == pytest.approx(1.4)


def test_end_never_moves_backward_even_on_a_tie():
    """끝 11.0 은 10.0 과 12.0 에서 똑같이 1.0 떨어져 있다 → **12.0**.
    끝을 앞으로 당기면 리액션 꼬리가 잘린다(E20-B1 tail_hold 규율)."""
    grid = make_grid()
    out, rec = bridge.snap_segments([seg(0.0, 11.0)], grid=grid,
                                    source_duration_sec=DURATION)
    assert out[0]["end_sec"] == 12.0
    note = [r for r in rec if r["boundary"] == "end"][0]
    assert note["action"] == "snapped" and note["err"] == pytest.approx(1.0)


def test_end_outside_forward_tolerance_keeps_raw_value():
    """끝 12.4 — 뒤 2.0 안에 눈금이 없고(다음은 20.0) 앞으로는 안 간다 → 원값 유지."""
    grid = make_grid()
    out, rec = bridge.snap_segments([seg(0.0, 12.4)], grid=grid,
                                    source_duration_sec=DURATION)
    assert out[0]["end_sec"] == 12.4
    note = [r for r in rec if r["boundary"] == "end"][0]
    assert note["action"] == "kept" and note["nearest_err"] == pytest.approx(0.4)
    assert "원값 유지" in note["why"]


def test_start_outside_tolerance_keeps_raw_value_and_reports_nearest():
    """시작 7.1 — 8.0 은 0.9 뒤(관용 0.5 밖) · 5.0 은 2.1 앞(관용 2.0 밖) → 원값.
    기록의 nearest_err 는 **방향을 안 가린** 최근접(0.9)이다 — 사람이 읽을 사실."""
    grid = make_grid()
    out, rec = bridge.snap_segments([seg(7.1, 30.0)], grid=grid,
                                    source_duration_sec=DURATION)
    assert out[0]["start_sec"] == 7.1
    note = [r for r in rec if r["boundary"] == "start"][0]
    assert note["action"] == "kept" and note["nearest_err"] == pytest.approx(0.9)


def test_boundary_exactly_on_a_grid_mark_is_not_recorded():
    """이미 눈금 위면 기록이 없다 — 기록을 잡음으로 채우지 않는다."""
    _out, rec = bridge.snap_segments([seg(8.0, 24.0)], grid=make_grid(),
                                     source_duration_sec=DURATION)
    assert rec == []


def test_snap_tolerances_are_the_contract_values():
    assert (bridge.SNAP_START_BACK_SEC, bridge.SNAP_START_FWD_SEC) == (2.0, 0.5)
    assert (bridge.SNAP_END_FWD_SEC, bridge.SNAP_END_BACK_SEC) == (2.0, 0.0)


def test_overlap_after_snap_is_reported_not_fixed():
    """스냅이 앞 조각과 겹치게 만들면 **알리고 고치지 않는다**(어느 쪽을 물릴 근거가 없다)."""
    grid = make_grid()
    out, rec = bridge.snap_segments([seg(0.0, 11.0), seg(11.4, 30.0)], grid=grid,
                                    source_duration_sec=DURATION)
    assert (out[0]["end_sec"], out[1]["start_sec"]) == (12.0, 10.0)
    ov = [r for r in rec if r["action"] == "overlap"]
    assert ov and ov[0]["segment"] == 1 and ov[0]["prev_end"] == 12.0


def test_snap_that_would_collapse_a_segment_keeps_the_raw_values():
    """11.6~11.7 은 양 끝이 같은 눈금(12.0)으로 접힌다 → 둘 다 원값 + 기록.

    실전에서는 6c 가 1.0초 미만 조각을 이미 드롭하지만(MIN_SEGMENT_SEC), 접힌 구간을
    그대로 내보내면 렌더가 프레임 0개짜리 클립을 만든다."""
    out, rec = bridge.snap_segments([seg(11.6, 11.7)], grid=make_grid(),
                                    source_duration_sec=DURATION)
    assert (out[0]["start_sec"], out[0]["end_sec"]) == (11.6, 11.7)
    assert [r["action"] for r in rec] == ["collapsed"]
    assert rec[0]["boundary"] == "both"


def test_snap_keeps_other_segment_keys_and_is_pure():
    grid = make_grid()
    segs = [{"start_sec": 11.4, "end_sec": 30.0, "quote": QUOTE_B, "role": "hook"}]
    before = copy.deepcopy(segs), copy.deepcopy(grid)
    out, _rec = bridge.snap_segments(segs, grid=grid, source_duration_sec=DURATION)
    assert out[0]["quote"] == QUOTE_B and out[0]["role"] == "hook"
    assert (segs, grid) == before


def test_snap_rejects_unreadable_or_reversed_segments():
    grid = make_grid()
    with pytest.raises(ValueError, match="구간 역전"):
        bridge.snap_segments([seg(20.0, 20.0)], grid=grid, source_duration_sec=DURATION)
    with pytest.raises(ValueError, match="시각을 읽을 수 없다"):
        bridge.snap_segments([{"start_sec": None, "end_sec": 10.0}], grid=grid,
                             source_duration_sec=DURATION)


# ── 2) 조각 → span (중점 규칙) ─────────────────────────────────────────────

def test_spans_for_matches_overrides_midpoint_rule_exactly():
    """같은 자여야 한다 — `overrides.spans_in_window` 를 직접 불러 대조한다."""
    grid = make_grid()
    segs = [seg(0.0, 12.0), seg(12.0, 24.0), seg(4.0, 9.0)]
    ids, missing = bridge.spans_for(segs, grid)
    assert ids == [spans_in_window(grid, s["start_sec"], s["end_sec"]) for s in segs]
    assert ids[0] == ["sp0000", "sp0001", "sp0002"]      # 중점 2.5·6.5·10.0
    assert ids[1] == ["sp0003", "sp0004"]                # 중점 16.0·22.0
    assert missing == []


def test_segment_shorter_than_the_grid_is_recorded_not_dropped():
    grid = make_grid()
    ids, missing = bridge.spans_for([seg(12.5, 13.0)], grid)
    assert ids == [[]]
    assert missing[0]["segment"] == 0 and missing[0]["window"] == [12.5, 13.0]
    assert "격자보다 짧다" in missing[0]["why"]


# ── 3) 인용 보호 ───────────────────────────────────────────────────────────

def test_quote_marks_only_the_span_that_holds_it():
    grid = make_grid()
    segs = [seg(5.0, 12.0, QUOTE_A)]
    ids, _ = bridge.spans_for(segs, grid)
    quoted, rec = bridge.quoted_span_ids(segs, ids, grid)
    assert quoted == {"sp0001"} and rec == []


def test_short_quote_is_not_judged():
    """짧은 인용은 우연 일치가 잦다 — 6c 와 같은 임계에서 판정하지 않는다."""
    grid = make_grid()
    segs = [seg(5.0, 12.0, "응?")]
    ids, _ = bridge.spans_for(segs, grid)
    quoted, rec = bridge.quoted_span_ids(segs, ids, grid)
    assert quoted == set()
    assert rec[0]["action"] == "skipped"
    assert str(timestamp_check.MIN_QUOTE_CHARS) in rec[0]["why"]


def test_quote_not_found_in_segment_is_recorded():
    grid = make_grid()
    segs = [seg(20.0, 24.0, QUOTE_A)]        # 그 대사는 sp0001(5~8s)에 있다
    ids, _ = bridge.spans_for(segs, grid)
    quoted, rec = bridge.quoted_span_ids(segs, ids, grid)
    assert quoted == set()
    assert rec[0]["action"] == "not_found" and "importance 5" in rec[0]["why"]


def test_null_quote_is_normal_and_silent():
    grid = make_grid()
    segs = [seg(12.0, 20.0, None)]
    ids, _ = bridge.spans_for(segs, grid)
    assert bridge.quoted_span_ids(segs, ids, grid) == (set(), [])


# ── 4) span_index ─────────────────────────────────────────────────────────

def test_index_fills_every_key_the_contract_table_lists():
    index, order = bridge.build_span_index(make_grid())
    assert order == [f"sp{i:04d}" for i in range(6)]
    sp = index["sp0001"]
    for key in ("t_in", "t_out", "is_audio", "pos", "text_source", "heard_text",
                "conf", "importance", "audio_script", "meaning_content", "mood",
                "scene_script"):
        assert key in sp, key
    assert (sp["t_in"], sp["t_out"], sp["is_audio"], sp["pos"]) == (5.0, 8.0, True, 1)
    assert sp["text_source"] == "transcript" and sp["heard_text"] == ""
    assert sp["audio_script"] == []
    assert index["sp0000"]["text_source"] is None       # 무성 span 은 대사 계열이 없다


def test_importance_default_and_quote_protection():
    grid = make_grid()
    index, _ = bridge.build_span_index(grid, quoted_spans={"sp0001"})
    assert index["sp0001"]["importance"] == bridge.QUOTE_IMPORTANCE == 5
    assert index["sp0001"]["importance_source"] == "quote"
    assert index["sp0002"]["importance"] == bridge.DEFAULT_IMPORTANCE == 3
    assert index["sp0002"]["importance_source"] == "default"


def test_quote_importance_must_beat_the_mute_rule():
    """값의 우연이 아니라 계약이다 — `plan_narration_slots` 의 ⓑ 가 이 이하를 뮤트한다."""
    assert bridge.QUOTE_IMPORTANCE > story.MUTE_MAX_IMPORTANCE
    assert bridge.DEFAULT_IMPORTANCE <= story.MUTE_MAX_IMPORTANCE


def test_conf_is_the_mean_word_probability_of_that_span():
    index, _ = bridge.build_span_index(make_grid())
    assert index["sp0001"]["conf"] == pytest.approx(0.8)     # 0.9, 0.7
    assert index["sp0002"]["conf"] == pytest.approx(0.5)
    assert index["sp0000"]["conf"] is None                   # 무성 — 판정하지 않는다


def test_conf_is_none_when_no_words_cover_a_voiced_span():
    grid = make_grid()
    grid["words"] = []
    index, _ = bridge.build_span_index(grid)
    assert index["sp0001"]["conf"] is None


def test_index_is_pure_and_deterministic():
    grid = make_grid()
    before = copy.deepcopy(grid)
    a, _ = bridge.build_span_index(grid, quoted_spans={"sp0001"})
    b, _ = bridge.build_span_index(grid, quoted_spans={"sp0001"})
    assert grid == before
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ── 5) 화자 — 없으면 크게 알린다 ───────────────────────────────────────────

def test_without_detail_there_is_no_speaker_and_the_audit_says_so():
    """🛑 계약 §0 의 발견 — 10a 가 꺼지면 자막이 전 줄 흰색이다."""
    index, _ = bridge.build_span_index(make_grid())
    assert assemble.speaker_colors(index) == {}      # 렌더가 쓰는 바로 그 함수
    audit = bridge.index_audit(index)
    assert audit["speaker_source"] == "none"
    assert audit["speakers"] == []
    assert audit["warning"] == bridge.NO_SPEAKER_WARNING
    assert audit["importance_source"] == bridge.IMPORTANCE_SOURCE


def _detail():
    """10a(정밀 청취) 산출 — `chunk_analyze.assemble_chunk_meanings` 의 span 어휘."""
    return {
        "sp0001": {"audio_script": [{"speaker": "박서진", "line": QUOTE_A}],
                   "importance": 4, "text_source": "transcript", "heard_text": QUOTE_A,
                   "scene_script": "무대 위에서 마이크를 잡는다",
                   "meaning_content": "결심을 밝힌다", "mood": "고조"},
        "sp0002": {"audio_script": [{"speaker": "전유진", "line": QUOTE_B}],
                   "importance": 2},
        "sp0004": {"audio_script": [{"speaker": "박서진", "line": QUOTE_C}],
                   "importance": 5, "text_source": "heard", "heard_text": "다시 들은 대사"},
    }


def test_detail_fills_speaker_and_colors_appear():
    index, _ = bridge.build_span_index(make_grid(), detail=_detail())
    colors = assemble.speaker_colors(index)
    assert colors["박서진"] == assemble.SPEAKER_DEFAULT_COLOR      # 최다 발화자 = 흰색
    assert colors["전유진"] in assemble.SPEAKER_PALETTE
    audit = bridge.index_audit(index, detail_spans=len(_detail()))
    assert audit["speaker_source"] == "detail" and "warning" not in audit
    assert audit["importance_source"] == bridge.IMPORTANCE_SOURCE_DETAIL
    assert audit["detail_spans"] == 3


def test_detail_importance_wins_except_it_cannot_undercut_a_quote():
    index, _ = bridge.build_span_index(make_grid(), quoted_spans={"sp0001"},
                                       detail=_detail())
    assert index["sp0001"]["importance"] == 5                 # 인용 보호가 바닥(10a 는 4)
    assert index["sp0001"]["importance_source"] == "quote"
    assert index["sp0002"]["importance"] == 2                 # 인용 아님 → 10a 그대로
    assert index["sp0002"]["importance_source"] == "detail"
    assert index["sp0004"]["importance_source"] == "detail"
    assert index["sp0001"]["heard_text"] == QUOTE_A
    assert index["sp0004"]["text_source"] == "heard"


def test_detail_from_another_grid_fails_loudly():
    """캐시 stale — 조용히 버리면 그 구간만 화자·대사가 빠진 채 발행된다."""
    with pytest.raises(ValueError, match="격자에 없는 span id"):
        bridge.build_span_index(make_grid(), detail={"sp9999": {"importance": 3}})


def test_detail_importance_out_of_range_fails_loudly():
    with pytest.raises(ValueError, match="1~5"):
        bridge.build_span_index(make_grid(), detail={"sp0001": {"importance": 9}})


# ── 6) 비트 뼈대 ──────────────────────────────────────────────────────────

def test_to_beats_is_one_beat_per_segment_with_empty_flesh():
    grid = make_grid()
    cand = {"id": "c1", "segments": [seg(0.0, 12.0, QUOTE_A), seg(12.0, 24.0)]}
    ids, _ = bridge.spans_for(cand["segments"], grid)
    beats = bridge.to_beats(cand, span_ids=ids, roles=["hook", "climax"])
    assert beats == [
        {"role": "hook", "span_ids": ["sp0000", "sp0001", "sp0002"],
         "narration": [], "labels": []},
        {"role": "climax", "span_ids": ["sp0003", "sp0004"],
         "narration": [], "labels": []}]


def test_to_beats_role_falls_back_to_segment_then_build():
    grid = make_grid()
    cand = {"id": "c1", "segments": [{**seg(0.0, 12.0), "role": "payoff"},
                                     seg(12.0, 24.0)]}
    ids, _ = bridge.spans_for(cand["segments"], grid)
    assert [b["role"] for b in bridge.to_beats(cand, span_ids=ids)] == ["payoff", "build"]


def test_to_beats_refuses_an_empty_span_list():
    """조용히 건너뛰면 비트 번호가 밀려 내레이션·라벨이 다른 구간에 붙는다."""
    cand = {"id": "c1", "segments": [seg(0.0, 12.0), seg(12.5, 13.0)]}
    with pytest.raises(ValueError, match="span 이 없다"):
        bridge.to_beats(cand, span_ids=[["sp0000"], []])


def test_to_beats_length_mismatch_fails():
    with pytest.raises(ValueError, match="조각 수"):
        bridge.to_beats({"id": "c", "segments": [seg(0.0, 1.0)]}, span_ids=[])


# ── 7) 🛑 span_index 가 v3 소비자에게 실제로 먹히는가 ──────────────────────

def test_span_index_feeds_word_subtitles_for_real():
    """열쇠 하나가 빠지면 여기서 KeyError 다 — 가짜가 아니라 진짜로 부른다."""
    grid = make_grid()
    index, _ = bridge.build_span_index(grid, quoted_spans={"sp0001"}, detail=_detail())
    timeline = [{"clip_start_sec": 5.0, "clip_end_sec": 12.0,
                 "span_ids": ["sp0001", "sp0002"], "use_original_audio": True,
                 "role": "hook", "subtitle": ""}]
    segs = assemble.word_subtitles(timeline, index, grid["words"])
    assert segs, "유성 span 이 둘인데 자막이 하나도 안 나왔다"
    assert all(s["start_sec"] >= 0 for s in segs)           # 편집본 좌표
    assert {s["speaker"] for s in segs} == {"박서진", "전유진"}
    assert {s["color"] for s in segs} == {assemble.SPEAKER_DEFAULT_COLOR,
                                          assemble.speaker_colors(index)["전유진"]}


def test_span_index_without_detail_still_draws_white_subtitles():
    grid = make_grid()
    index, _ = bridge.build_span_index(grid)
    timeline = [{"clip_start_sec": 5.0, "clip_end_sec": 12.0,
                 "span_ids": ["sp0001", "sp0002"], "use_original_audio": True,
                 "role": "hook", "subtitle": ""}]
    segs = assemble.word_subtitles(timeline, index, grid["words"])
    assert segs and {s["color"] for s in segs} == {assemble.SPEAKER_DEFAULT_COLOR}


def test_span_index_feeds_narration_slots_and_quote_blocks_the_mute():
    """인용 보호가 **실제로** 뮤트를 막는지 — v3 함수를 그대로 태워 확인한다."""
    grid = make_grid()
    index, _ = bridge.build_span_index(grid, quoted_spans={"sp0001"})
    quoted_beat = [{"role": "hook", "span_ids": ["sp0001"],
                    "narration": ["한 문장 내레이션"], "labels": []}]
    cues, dropped = story.plan_narration_slots(quoted_beat, index)
    assert cues == [] and dropped and "importance≥4" in dropped[0]["reason"]

    plain_beat = [{"role": "hook", "span_ids": ["sp0002"],
                   "narration": ["한 문장 내레이션"], "labels": []}]
    cues2, dropped2 = story.plan_narration_slots(plain_beat, index)
    assert dropped2 == [] and cues2[0]["mode"] == "muted"
    assert cues2[0]["muted_span_ids"] == ["sp0002"]
    assert story.verify_tts_conflicts(cues2, plain_beat, index) == []


def test_span_index_feeds_trim_to_budget_and_story_duration():
    grid = make_grid()
    index, _ = bridge.build_span_index(grid, quoted_spans={"sp0001"})
    beats = [{"role": "hook", "span_ids": ["sp0000", "sp0001", "sp0002"],
              "narration": [], "labels": []}]
    assert story.story_duration(beats, index) == pytest.approx(12.0)
    removed = story.trim_to_budget(beats, index, grid["arousal"], max_sec=8.0)
    # 가장자리부터, importance 5(인용) 는 보호 — sp0000(무성 5s) 이 먼저 나간다
    assert [r["span_id"] for r in removed] == ["sp0000"]
    assert beats[0]["span_ids"] == ["sp0001", "sp0002"]


# ── 8) 다리 건너기(순서가 곧 계약) ─────────────────────────────────────────

def test_cross_runs_snap_then_spans_then_quote_protection():
    grid = make_grid()
    cand = {"id": "c1", "template": "recap_dialogue",
            "segments": [seg(4.6, 11.0, QUOTE_A), seg(20.2, 29.8, QUOTE_C)]}
    out = bridge.cross(cand, grid=grid, source_duration_sec=DURATION)
    # 스냅: 4.6→5.0(앞 관용) · 11.0→12.0(끝은 뒤로만) · 20.2→20.0 · 29.8→30.0
    assert [(s["start_sec"], s["end_sec"]) for s in out["segments"]] == \
        [(5.0, 12.0), (20.0, 30.0)]
    assert out["span_ids"] == [["sp0001", "sp0002"], ["sp0004", "sp0005"]]
    # 인용 보호는 **스냅된** 조각의 span 위에서 걸린다
    assert out["span_index"]["sp0001"]["importance"] == bridge.QUOTE_IMPORTANCE
    assert out["span_index"]["sp0004"]["importance"] == bridge.QUOTE_IMPORTANCE
    assert out["span_index"]["sp0002"]["importance"] == bridge.DEFAULT_IMPORTANCE
    assert [b["span_ids"] for b in out["beats"]] == out["span_ids"]
    assert out["audit"]["index"]["quoted_spans"] == 2
    assert out["audit"]["spans_missing"] == []
    json.dumps(out["audit"])          # run_log 에 그대로 실린다


def test_cross_holds_beats_back_when_a_segment_has_no_span():
    grid = make_grid()
    cand = {"id": "c2", "segments": [seg(0.0, 12.0), seg(12.5, 13.0)]}
    out = bridge.cross(cand, grid=grid, source_duration_sec=DURATION)
    assert out["beats"] is None                      # 호출자가 판단한다
    assert out["audit"]["spans_missing"][0]["segment"] == 1


def test_cross_is_pure_and_deterministic():
    grid = make_grid()
    cand = {"id": "c1", "segments": [seg(4.6, 11.0, QUOTE_A)]}
    before = copy.deepcopy(grid), copy.deepcopy(cand)
    a = bridge.cross(cand, grid=grid, source_duration_sec=DURATION)
    b = bridge.cross(cand, grid=grid, source_duration_sec=DURATION)
    assert (grid, cand) == before
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
