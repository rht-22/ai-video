"""v3 edit_overrides — 자막 앵커 재배치 · cue 끝 동률 · 화자 색 승계 (2026-09-07 가왕쇼 실사고).

  · 자막 source_time_sec 가 있으면 편집본 좌표는 **현재 타임라인**으로 다시 계산한다
    (다른 실행의 좌표로 보낸 자막이 1.18s 일찍 나가던 것) · 앵커가 타임라인 밖이면 드랍+기록
  · cue 끝이 클립 끝과 동률(덮개 클립)이어도 드랍되지 않는다(kind="end")
  · speaker·color 는 additive 로 살아남는다
"""
from __future__ import annotations

from app.v3.overrides import apply_overrides_to_plan

PLAN = {"timeline": [
    {"role": "hook", "clip_start_sec": 100.0, "clip_end_sec": 110.0, "use_original_audio": True,
     "span_ids": []},
    {"role": "ending", "clip_start_sec": 200.0, "clip_end_sec": 202.0, "use_original_audio": False,
     "span_ids": [], "cover": "designated"},
]}
GRID = {"span_candidates": []}


def _ov(subs):
    return {"schema": "edit_overrides/v3", "subtitles": subs}


def test_subtitle_source_anchor_remaps_to_current_timeline_and_keeps_color():
    subs = [{"start_sec": 3.0, "end_sec": 4.0, "text": "앵커 줄", "source_time_sec": 105.5,
             "speaker": "박서진", "color": "#FFFFFF"},
            {"start_sec": 1.0, "end_sec": 2.0, "text": "앵커 없음"}]
    _, segs, _, rec = apply_overrides_to_plan(_ov(subs), PLAN, GRID, [], {"tts_cue_files": []})
    assert [s["text"] for s in segs] == ["앵커 없음", "앵커 줄"]
    anchored = segs[1]
    assert anchored["start_sec"] == 5.5 and anchored["end_sec"] == 6.5    # 105.5 → 100 오프셋 0
    assert anchored["speaker"] == "박서진" and anchored["color"] == "#FFFFFF"
    assert "speaker" not in segs[0]
    assert any("앵커 재배치 1줄" in a for a in rec["applied"])


def test_subtitle_anchor_outside_timeline_is_dropped_and_recorded():
    subs = [{"start_sec": 0.0, "end_sec": 1.0, "text": "밖", "source_time_sec": 150.0}]
    _, segs, _, rec = apply_overrides_to_plan(_ov(subs), PLAN, GRID, [], {"tts_cue_files": []})
    assert segs == [] and rec["dropped_subtitles"] == ["밖@150.00s"]


def test_cue_ending_exactly_at_clip_end_survives():
    res = {"tts_cue_files": [
        {"path": "a.mp3", "cue": {"text": "엔딩", "source_time_sec": 200.1, "duration_sec": 1.9}}]}
    _, _, new_res, rec = apply_overrides_to_plan(_ov([{"start_sec": 0, "end_sec": 1, "text": "x"}]),
                                                 PLAN, GRID, [], res)
    assert rec["cues_dropped"] == []
    cue = new_res["tts_cue_files"][0]["cue"]
    assert cue["start_sec"] == 10.1 and cue["end_sec"] == 12.0


def test_subtitle_anchor_in_small_gap_before_clip_snaps_to_clip_head():
    plan = {"timeline": [
        {"role": "hook", "clip_start_sec": 100.0, "clip_end_sec": 101.0, "use_original_audio": True, "span_ids": []},
        {"role": "hook", "clip_start_sec": 101.04, "clip_end_sec": 103.0, "use_original_audio": True, "span_ids": []}]}
    subs = [{"start_sec": 0.95, "end_sec": 1.6, "text": "선행 자막", "source_time_sec": 101.0}]   # 틈 0.04s
    _, segs, _, rec = apply_overrides_to_plan(_ov(subs), plan, GRID, [], {"tts_cue_files": []})
    assert [s["text"] for s in segs] == ["선행 자막"] and segs[0]["start_sec"] == 1.0
    assert "dropped_subtitles" not in rec
    far = [{"start_sec": 0.0, "end_sec": 1.0, "text": "먼 앵커", "source_time_sec": 99.0}]      # 1.0s 앞 — 드랍
    _, segs, _, rec = apply_overrides_to_plan(_ov(far), plan, GRID, [], {"tts_cue_files": []})
    assert segs == [] and rec["dropped_subtitles"]
