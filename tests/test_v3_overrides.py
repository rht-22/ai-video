"""V3-M5 — edit_overrides/v2 수용(C4) 회귀 가드.

계약: clip 경계는 최근접 grid span 경계로 정착(오차 기록·거절 없음), 전량 교체
규약, cue 는 source_time_sec 신원 승계(재합성 없음 — 재과금 방지), 모르는 키는
unhandled 기록(조용한 무시 금지).
"""
from __future__ import annotations

import pytest

from app.modules.edit_overrides import EditOverrideError, validate_overrides
from app.v3.overrides import (
    apply_overrides_to_plan,
    snap_to_span_edges,
    spans_in_window,
)

GRID = {"span_candidates": [
    {"id": "sp0000", "t_in": 100.0, "t_out": 104.0, "is_audio": True},
    {"id": "sp0001", "t_in": 104.0, "t_out": 110.0, "is_audio": False},
    {"id": "sp0002", "t_in": 110.0, "t_out": 115.0, "is_audio": True},
]}

PLAN = {"schema": "edit_plan/v3",
        "layout": {"top_title": "원래 제목\n둘째 줄", "bottom_label": "작품"},
        "timeline": [
            {"role": "hook", "clip_start_sec": 100.0, "clip_end_sec": 104.0,
             "subtitle": "", "use_original_audio": False,
             "reframe": {"mode": "center"}, "span_ids": ["sp0000"]},
            {"role": "build", "clip_start_sec": 110.0, "clip_end_sec": 115.0,
             "subtitle": "", "use_original_audio": True,
             "reframe": {"mode": "center"}, "span_ids": ["sp0002"]},
        ]}

RESOURCES = {"tts_cue_files": [
    {"cue_index": 0, "path": "/tmp/x.mp3",
     "cue": {"text": "나레이션", "source_time_sec": 100.0, "duration_sec": 2.0,
             "start_sec": 0.0, "end_sec": 2.0, "voice": "ko_female",
             "speed": "normal"}}]}


def test_snap_to_nearest_edge_with_error_log():
    snapped, err = snap_to_span_edges(103.4, [100.0, 104.0, 110.0])
    assert snapped == 104.0 and err == pytest.approx(0.6)
    snapped2, err2 = snap_to_span_edges(104.0, [100.0, 104.0, 110.0])
    assert snapped2 == 104.0 and err2 == 0.0


def test_spans_in_window_midpoint_rule():
    assert spans_in_window(GRID, 100.0, 110.0) == ["sp0000", "sp0001"]
    assert spans_in_window(GRID, 102.5, 110.0) == ["sp0001"]   # sp0000 중점 102 밖


def test_clips_replace_snap_and_mute_inherit():
    ov = validate_overrides({"schema": "edit_overrides/v2",
                             "clips": [{"start_sec": 100.3, "end_sec": 104.2,
                                        "role": "hook"}]})
    plan, segs, res, rec = apply_overrides_to_plan(ov, PLAN, GRID, [], RESOURCES)
    tl = plan["timeline"]
    assert len(tl) == 1                                       # 전량 교체
    assert tl[0]["clip_start_sec"] == 100.0                   # 스냅 정착
    assert tl[0]["clip_end_sec"] == 104.0
    assert tl[0]["use_original_audio"] is False               # 겹친 기존 클립 뮤트 승계
    assert tl[0]["span_ids"] == ["sp0000"]
    assert rec["snap_log"][0]["err"] == [pytest.approx(0.3), pytest.approx(0.2)]


def test_title_and_subtitles_replacement():
    ov = validate_overrides({"schema": "edit_overrides/v2",
                             "title": {"top_title": "새 제목\n새 펀치"},
                             "subtitles": [{"start_sec": 1.0, "end_sec": 2.0,
                                            "text": "고친 자막"}]})
    plan, segs, _res, rec = apply_overrides_to_plan(ov, PLAN, GRID, [
        {"start_sec": 0.5, "end_sec": 1.5, "text": "옛 자막"}], RESOURCES)
    assert plan["layout"]["top_title"] == "새 제목\n새 펀치"
    assert segs == [{"start_sec": 1.0, "end_sec": 2.0, "text": "고친 자막"}]
    assert "title.top_title" in rec["applied"]


def test_cue_inherited_by_source_identity_and_dropped_when_window_gone():
    # 유지: cue 소스 창(100~102)이 새 timeline 에 남는 경우 — 편집본 좌표 재계산
    ov = validate_overrides({"schema": "edit_overrides/v2",
                             "clips": [{"start_sec": 100.0, "end_sec": 104.0}]})
    _plan, _s, res, rec = apply_overrides_to_plan(ov, PLAN, GRID, [], RESOURCES)
    assert len(res["tts_cue_files"]) == 1 and not rec["cues_dropped"]
    cue = res["tts_cue_files"][0]["cue"]
    assert cue["source_time_sec"] == 100.0                    # 신원 불변(C2)
    assert cue["start_sec"] == pytest.approx(0.0)
    # 드랍: 소스 창이 편성에서 빠진 경우 — 기록 필수
    ov2 = validate_overrides({"schema": "edit_overrides/v2",
                              "clips": [{"start_sec": 110.0, "end_sec": 115.0}]})
    _p2, _s2, res2, rec2 = apply_overrides_to_plan(ov2, PLAN, GRID, [], RESOURCES)
    assert res2["tts_cue_files"] == []
    assert len(rec2["cues_dropped"]) == 1


def test_unhandled_keys_are_recorded_not_ignored():
    ov = validate_overrides({"schema": "edit_overrides/v2",
                             "title": {"top_title": "x\ny"},
                             "tts": [{"source_time_sec": 100.0, "text": "새 대본"}]})
    _p, _s, _r, rec = apply_overrides_to_plan(ov, PLAN, GRID, [], RESOURCES)
    assert "tts" in rec["unhandled"]


def test_contract_validation_is_reused_verbatim():
    # 같은 JSON 은 v1/v3 어디서든 같은 이유로 거절 — 기존 검증기 그대로
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v2",
                            "clips": [{"start_sec": 5.0, "end_sec": 3.0}]})
