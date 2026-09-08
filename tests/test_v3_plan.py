"""4단계(plan N편 + contrast + 갭 9·11) 회귀 가드 — LLM 없이 돈다.

고정하는 것: plan 검증기(kind·id·중복률·contrast 시간 규칙·레지스터 밖 note·N 절단) ·
topic kind(event 기본·contrast 는 setup<payoff 필수) · 씬 쓰임 축(event 문구 종전과 동일) ·
hook_return(훅 조각 1회 재사용 · 길이 ≤ 훅 · 맨 뒤 정렬 · 다른 재사용은 반려) · cue 의 클립 신원(beat) ·
plan_item_as_topic · CLI/파이프라인 배선.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v3_story_flow import GRID, IDX, ROWS, _allowed  # noqa: E402

from app.v3 import assemble, plan as pl  # noqa: E402
from app.v3.story_flow import select as sl  # noqa: E402

REG = [{"id": "r001", "kind": "claim", "setup": {"meaning": 0, "t": 0.0, "quote": "x"},
        "payoff": {"meaning": 2, "t": 14.0, "quote": "y"}}]
ROWS_FAR = [dict(r) for r in ROWS]
ROWS_FAR[2] = {**ROWS_FAR[2], "t0": 100.0, "t1": 120.0}      # contrast 시간 거리용


def test_validate_plan_kinds_ids_dup_and_contrast_rules():
    resp = {"shorts": [
        {"no": 1, "kind": "event", "topic": "대치", "core_meanings": ["m000"], "purpose": "p", "why_standalone": "w"},
        {"no": 2, "kind": "contrast", "topic": "선언↔반전", "core_meanings": ["m000", "m002"],
         "setup": "m000", "payoff": "m002", "purpose": "p", "why_standalone": "w"},
    ]}
    out, pr, notes = pl.validate_plan(resp, ROWS_FAR, n=2, register=REG)
    # 편1(m000) 과 편2(m000,m002) 중복 = 1/1 = 100% → 반려
    assert out is None and any("중복" in p for p in pr)
    resp["shorts"][0]["core_meanings"] = ["m001"]
    out, pr, notes = pl.validate_plan(resp, ROWS_FAR, n=2, register=REG)
    assert pr == [] and [it["kind"] for it in out] == ["event", "contrast"]
    assert out[1]["setup"] == 0 and out[1]["payoff"] == 2 and out[1]["core_meanings"] == [0, 2]
    assert not any("레지스터 밖" in n for n in notes)
    # 레지스터 밖 쌍은 note · 시간 거리 미달은 반려 · setup 이 뒤면 반려
    resp2 = {"shorts": [{"kind": "irony", "topic": "t", "core_meanings": [], "setup": "m001", "payoff": "m002"}]}
    out, pr, notes = pl.validate_plan(resp2, ROWS_FAR, n=1, register=REG)
    assert pr == [] and any("레지스터 밖" in n for n in notes)
    out, pr, _ = pl.validate_plan(resp2, ROWS, n=1, register=REG)          # 붙어 있는 사건
    assert out is None and any("떨어져야" in p for p in pr)
    resp3 = {"shorts": [{"kind": "contrast", "topic": "t", "core_meanings": [], "setup": "m002", "payoff": "m000"}]}
    assert pl.validate_plan(resp3, ROWS_FAR, n=1)[0] is None
    assert pl.validate_plan({"shorts": [{"kind": "x", "topic": "t", "core_meanings": ["m000"]}]}, ROWS, n=1)[0] is None
    assert pl.validate_plan({"shorts": [{"kind": "event", "topic": "t", "core_meanings": ["m099"]}]}, ROWS, n=1)[0] is None
    # 이미 만든 편의 사건은 반려 · N 초과는 절단 note
    out, pr, _ = pl.validate_plan({"shorts": [{"kind": "event", "topic": "t", "core_meanings": ["m000"]}]},
                                  ROWS, n=1, excluded={0})
    assert out is None and any("이미 만든" in p for p in pr)
    many = {"shorts": [{"kind": "event", "topic": f"t{i}", "core_meanings": [f"m{i:03d}"]} for i in range(3)]}
    out, pr, notes = pl.validate_plan(many, ROWS, n=2)
    assert pr == [] and [it["no"] for it in out] == [1, 2] and any("앞 2개" in n for n in notes)


def test_run_plan_requires_map_and_uses_register_block():
    with pytest.raises(ValueError, match="회차 지도"):
        pl.run_plan(object(), {"sequences": []}, GRID, n=2, map_doc=None, work_title="T")
    from test_v3_story_flow import S2
    map_doc = {"fingerprint": "fp", "register": REG, "facts_ledger": [], "diegesis_final": {}}
    prompts = []
    def call(g, p):
        prompts.append(p)
        return {"shorts": [{"kind": "event", "topic": "a", "core_meanings": ["m001"]},
                           {"kind": "event", "topic": "b", "core_meanings": ["m002"]}]}
    doc, audit = pl.run_plan(object(), S2, GRID, n=2, map_doc=map_doc, work_title="T", call=call,
                             log=lambda *a: None)
    assert doc["schema"] == pl.SCHEMA_PLAN and len(doc["shorts"]) == 2 and audit["calls"] == 1
    assert "r001 claim" in prompts[0] and "쇼츠 2편" in prompts[0]
    t = pl.plan_item_as_topic(doc["shorts"][0])
    assert t == {"topic": "a", "why": "", "core_meanings": ["m001"], "kind": "event"}
    with pytest.raises(ValueError, match="1~"):
        pl.run_plan(object(), S2, GRID, n=0, map_doc=map_doc, work_title="T", call=call)


def test_topic_kind_default_event_and_contrast_requires_setup_payoff():
    obj, pr = sl.validate_topic({"topic": "t", "core_meanings": ["m001"]}, ROWS)
    assert obj["kind"] == "event" and "setup" not in obj
    obj, pr = sl.validate_topic({"topic": "t", "core_meanings": ["m001"], "kind": "contrast"}, ROWS)
    assert obj is None and any("setup·payoff" in p for p in pr)
    obj, pr = sl.validate_topic({"topic": "t", "core_meanings": [], "kind": "contrast",
                                 "setup": "m000", "payoff": "m002"}, ROWS)
    assert pr == [] and obj["setup"] == 0 and obj["payoff"] == 2 and obj["core_meanings"] == [0, 2]
    obj, pr = sl.validate_topic({"topic": "t", "core_meanings": [], "kind": "irony",
                                 "setup": "m002", "payoff": "m000"}, ROWS)
    assert obj is None and any("앞이어야" in p for p in pr)
    assert sl.validate_topic({"topic": "t", "core_meanings": ["m001"], "kind": "x"}, ROWS)[0] is None


def test_purpose_axis_event_unchanged_and_contrast_axis():
    purposes, axis, choices = sl.purpose_axis("event")
    assert purposes is sl.PURPOSES and choices == "배경|맥락|과정|결과|반응"
    assert axis == "배경(왜 이 상황인지) · 맥락(인물 관계·무엇이 걸렸는지) · 과정(사건 진행) · 결과(정점·반전) · 반응(리액션·여운)"
    p2, a2, c2 = sl.purpose_axis("contrast")
    assert p2 == sl.CONTRAST_PURPOSES and c2 == "선언|경과|반전"
    resp = {"scenes": [{"meaning": "m000", "purpose": "선언"}, {"meaning": "m002", "purpose": "결과"}],
            "title": {"line1": "a", "line2": "b"}}
    obj, pr, notes = sl.validate_scenes(resp, ROWS, purposes=p2)
    assert pr == [] and [s["purpose"] for s in obj["scenes"]] == ["선언", "경과"]
    assert any("→ 경과" in n for n in notes)
    obj, pr, notes = sl.validate_scenes(resp, ROWS)
    assert [s["purpose"] for s in obj["scenes"]] == ["과정", "결과"]     # event 축 종전 그대로


def test_hook_return_reuses_hook_once_and_sorts_last():
    allowed = _allowed()
    resp = {"beats": [{"first": "sp0001", "last": "sp0002", "role": "hook"},
                      {"first": "sp0004", "last": "sp0005", "role": "climax"},
                      {"first": "sp0001", "last": "sp0001", "role": "hook_return"}]}
    beats, pr, notes = sl.validate_beats(resp, IDX, allowed, budget_sec=30)
    assert pr == [] and [b["role"] for b in beats] == ["hook", "climax", "hook_return"]
    assert beats[-1]["reuse_of"] == "hook" and beats[-1]["span_ids"] == ["sp0001"]
    # 훅보다 길면 · 훅 밖 조각이면 · 두 번째면 · 다른 role 의 재사용이면 반려
    resp["beats"][2] = {"first": "sp0004", "last": "sp0004", "role": "hook_return"}
    assert sl.validate_beats(resp, IDX, allowed, budget_sec=30)[0] is None
    resp["beats"][2] = {"first": "sp0001", "last": "sp0002", "role": "build"}
    b2, pr2, _ = sl.validate_beats(resp, IDX, allowed, budget_sec=30)
    assert b2 is None and any("겹친다" in p for p in pr2)
    resp["beats"] = [{"first": "sp0001", "last": "sp0002", "role": "hook"},
                     {"first": "sp0001", "last": "sp0001", "role": "hook_return"},
                     {"first": "sp0002", "last": "sp0002", "role": "hook_return"}]
    b3, pr3, _ = sl.validate_beats(resp, IDX, allowed, budget_sec=30)
    assert b3 is None and any("편당 하나" in p for p in pr3)
    # hook_return 이 아닌데 role 만 hook_return 이면 build 로(note)
    resp["beats"] = [{"first": "sp0001", "last": "sp0002", "role": "hook"},
                     {"first": "sp0004", "last": "sp0005", "role": "hook_return"}]
    b4, pr4, n4 = sl.validate_beats(resp, IDX, allowed, budget_sec=30)
    assert pr4 == [] and b4[1]["role"] == "build" and any("훅 조각 재사용이 아니다" in n for n in n4)


def test_cue_prefers_own_beat_clip_when_source_repeats():
    story = {"beats": [
        {"number": 0, "role": "hook", "span_ids": ["sp0001", "sp0002"], "muted_span_ids": []},
        {"number": 1, "role": "climax", "span_ids": ["sp0004"], "muted_span_ids": []},
        {"number": 2, "role": "hook_return", "span_ids": ["sp0001"], "muted_span_ids": []},
    ], "narration_cues": [], "title": {"line1": "a", "line2": "b"}}
    plan = assemble.assemble_edit_plan(story, IDX, video_path="v.mp4", work_title="T")
    assert [c["beat"] for c in plan["timeline"]] == [0, 1, 2]
    assert plan["timeline"][2]["clip_start_sec"] == 2.0 == plan["timeline"][0]["clip_start_sec"]
    cues = [{"beat": 2, "line": 0, "text": "회수", "mode": "cover", "source_time_sec": 2.0,
             "source_end_sec": 3.5, "muted_span_ids": []},
            {"beat": 0, "line": 0, "text": "훅", "mode": "cover", "source_time_sec": 2.0,
             "source_end_sec": 3.5, "muted_span_ids": []}]
    fin = assemble.finalize_cues(cues, plan["timeline"], voice="v", speed="s")
    assert fin[0]["start_sec"] == pytest.approx(4.5 + 3.0)     # 훅(4.5s) + 클라이맥스(3.0s) 뒤 = 두 번째 등장
    assert fin[1]["start_sec"] == pytest.approx(0.0)           # 첫 등장
    # beat 키 없는 옛 타임라인은 종전(첫 등장) 그대로
    for c in plan["timeline"]:
        c.pop("beat")
    fin2 = assemble.finalize_cues(cues, plan["timeline"], voice="v", speed="s")
    assert fin2[0]["start_sec"] == pytest.approx(0.0)


def test_cli_and_pipeline_wiring():
    from app.v3 import cli, pipeline as v3p
    assert v3p.V3_STEPS.index("plan") == v3p.V3_STEPS.index("episode_map") + 1
    a = cli.build_parser().parse_args(["--video", "v.mp4", "--work-title", "t", "--episode-map",
                                       "--plan-shorts", "3", "--plan-slot", "2", "--from-step", "plan"])
    assert a.plan_shorts == 3 and a.plan_slot == 2 and a.from_step == "plan"
    src = (Path(__file__).resolve().parents[1] / "app" / "v3" / "pipeline.py").read_text(encoding="utf-8")
    assert "topic_override=_topic_override" in src and "--plan-slot 은 --story-flow human 전용" in src
