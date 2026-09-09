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


def test_cue_inherit_uses_source_end_sec_for_hold_cover():
    """붙잡은 덮개(hold) 위 cue: 편집본 길이(duration_sec)가 소스 창보다 hold_sec 만큼 길다 — start+duration 으로
    끝을 재면 클립 밖이 되어 훅 내레이션이 드랍됐다(2026-09-09 ep8ex01). source_end_sec 이 있으면 그것이 소스 끝."""
    from app.v3 import overrides as ov
    plan = {"timeline": [{"clip_start_sec": 2619.64, "clip_end_sec": 2621.56, "use_original_audio": False,
                          "span_ids": ["a", "b"], "cover": "hold", "hold_sec": 0.032, "role": "hook"},
                         {"clip_start_sec": 2630.0, "clip_end_sec": 2634.0, "use_original_audio": True, "span_ids": ["c"]}],
            "source_fps": 30.0}
    grid = {"span_candidates": [{"id": "a", "t_in": 2619.64, "t_out": 2620.5}, {"id": "b", "t_in": 2620.5, "t_out": 2621.56},
                                {"id": "c", "t_in": 2630.0, "t_out": 2634.0}]}
    cue = {"text": "게릴라 홍보 마감 직전,", "source_time_sec": 2619.74, "source_end_sec": 2621.56,
           "duration_sec": 1.952, "start_sec": 0.1, "end_sec": 2.052}
    res = {"tts_cue_files": [{"cue_index": 0, "path": "x.mp3", "cue": cue}]}
    _p, _s, new_res, rec = ov.apply_overrides_to_plan({"schema": "edit_overrides/v3", "subtitles": []}, plan, grid, [], res)
    assert rec["cues_dropped"] == [] and len(new_res["tts_cue_files"]) == 1
    # source_end_sec 없는 옛 cue: hold 꼬리만큼(≤0.5s) 넘는 건 편집본 길이 보존으로 살린다 · 크게 넘으면 드랍
    old = {**cue}; old.pop("source_end_sec")
    _p, _s, new_res2, rec2 = ov.apply_overrides_to_plan({"schema": "edit_overrides/v3", "subtitles": []}, plan, grid, [],
                                                       {"tts_cue_files": [{"cue_index": 0, "path": "x.mp3", "cue": old}]})
    assert rec2["cues_dropped"] == [] and new_res2["tts_cue_files"][0]["cue"]["duration_sec"] == 1.952
    far = {**old, "duration_sec": 5.0}
    _p, _s, new_res3, rec3 = ov.apply_overrides_to_plan({"schema": "edit_overrides/v3", "subtitles": []}, plan, grid, [],
                                                       {"tts_cue_files": [{"cue_index": 0, "path": "x.mp3", "cue": far}]})
    assert len(rec3["cues_dropped"]) == 1 and new_res3["tts_cue_files"] == []
    # finalize_cues 가 source_end_sec 을 싣는다(resources 사본에 남아야 승계가 정본 경로를 탄다)
    from app.v3 import assemble
    fin = assemble.finalize_cues([{"text": "t", "source_time_sec": 2619.74, "source_end_sec": 2621.56, "beat": 0,
                                  "mode": "cover", "muted_span_ids": []}], plan["timeline"], voice="ko_female", speed="fast",
                                 fps=30.0)
    assert fin[0].get("source_end_sec") == 2621.56


def test_override_subtitles_overlap_is_clamped():
    """손편집 자막이 다음 줄과 0.05s 겹치면 libass 충돌 회피로 줄이 위로 밀린다(ep8ex02 "자막이 위아래로") — 끝을 다음 시작으로."""
    from app.v3 import overrides as ov
    plan = {"timeline": [{"clip_start_sec": 10.0, "clip_end_sec": 20.0, "use_original_audio": True, "span_ids": ["a"]}],
            "source_fps": 30.0}
    grid = {"span_candidates": [{"id": "a", "t_in": 10.0, "t_out": 20.0}]}
    subs = [{"start_sec": 0.0, "end_sec": 1.5, "text": "하나", "source_time_sec": 10.0},
            {"start_sec": 1.45, "end_sec": 3.0, "text": "둘", "source_time_sec": 11.45},
            {"start_sec": 3.0, "end_sec": 4.0, "text": "셋", "source_time_sec": 13.0}]
    _p, segs, _r, _rec = ov.apply_overrides_to_plan({"schema": "edit_overrides/v3", "subtitles": subs}, plan, grid, [], {})
    assert [(s["start_sec"], s["end_sec"]) for s in segs] == [(0.0, 1.45), (1.45, 3.0), (3.0, 4.0)]
