"""3단계 회차 지도(app/v3/episode_map) 회귀 가드 — LLM 없이 돈다.

고정하는 것: 검증기(id 존재·kind 화이트리스트·setup<payoff·시퀀스 소속) · 정방향 병합(id 부여·
payoff_of 연결·diegesis 기록) · 역방향 diff 적용 · 상태 압축 상한 · 지문 · 사람 교정 병합
(fail-loud) · 게이트(미지정이면 파일도 프롬프트 변화도 없음) · story_flow 소비(레지스터 블록·
diegesis_final 덮기·갭 8 corrections 스키마).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v3_stage3 import _mk_grid, _mk_stage2  # noqa: E402

from app.v3 import episode_map as em  # noqa: E402
from app.v3 import story as st  # noqa: E402
from app.v3.story_flow import select as sl  # noqa: E402
from app.v3.story_flow.common import meaning_rows  # noqa: E402

GRID = _mk_grid([
    (0.0, 2.0, True, "먼저 인사해도 쌩깔 거야."),   # sp0000  m0 (seq0)
    (2.0, 4.0, False, ""),                        # sp0001  m0
    (10.0, 13.0, True, "너 바람피니?"),           # sp0002  m1 (seq1)
    (13.0, 16.0, False, ""),                      # sp0003  m1  카페(상상)
    (20.0, 23.0, True, "나 그때 일터에 있었어."),  # sp0004  m2 (seq1)
])
S2 = _mk_stage2(GRID, [(0, 1, 4, "선언"), (2, 3, 5, "추궁"), (4, 4, 3, "알리바이")])
# 두 시퀀스로 나눈다(m0 | m1·m2) + 2단계 필드
_ch = S2["sequences"][0]["chunks"][0]
_ms = _ch["meanings"]
S2["schema"] = "v3_stage2/v2"
S2["sequences"] = [
    {"number": 0, "content": "선언", "time": _ms[0]["time"],
     "chunks": [{"number": 0, "time": _ms[0]["time"], "meanings": [_ms[0]]}]},
    {"number": 1, "content": "추궁", "time": {"start": _ms[1]["time"]["start"], "end": _ms[2]["time"]["end"]},
     "chunks": [{"number": 0, "time": {"start": _ms[1]["time"]["start"], "end": _ms[2]["time"]["end"]},
                 "meanings": [_ms[1], _ms[2]]}]},
]
_ms[0]["spans"][0]["is_claim"] = True
_ms[1]["spans"][1]["diegesis"] = "recalled"
_ms[1]["spans"][1]["scene_script"] = "카페에서 마주 앉은 두 사람"
_ms[1]["spans"][0]["screen_text"] = "오빠랑 같이 해서 너무 좋았어"
_ms[1]["screen_texts"] = ["오빠랑 같이 해서 너무 좋았어"]
ROWS = meaning_rows(S2)
BY = {r["idx"]: r for r in ROWS}
SPANS = em.span_table(S2)


def _fwd0():
    return {"facts": [{"text": "경희가 쌩깔 선언", "source": "dialogue", "meanings": ["m000"]}],
            "beliefs": [], "characters": {"박경희": {"knows": [], "believes": ["이웃이 무례하다"], "wants": []}},
            "open_questions": ["정말 쌩깔까?"],
            "register": [{"kind": "claim", "setup": {"meaning": "m000", "quote": "먼저 인사해도 쌩깔 거야"},
                          "payoff": None, "what_flipped": ""}],
            "diegesis": {}}


def _fwd1():
    return {"facts": [{"text": "카톡 원문이 화면에", "source": "screen_text", "meanings": ["m001"]},
                      {"text": "남편이 일터에 있었다고 말함", "source": "dialogue", "meanings": ["m002"]}],
            "beliefs": [{"holder": "박경희", "text": "남편이 바람을 핀다", "meanings": ["m001"]}],
            "characters": {"박경희": {"knows": ["카톡"], "believes": ["불륜"], "wants": ["확인"]}},
            "open_questions": ["카페 장면은 실제인가"],
            "register": [{"kind": "claim", "payoff_of": "r001", "payoff": {"meaning": "m001", "quote": "먼저 말을 건다"},
                          "what_flipped": "쌩깐다더니 먼저 말을 걸었다"},
                         {"kind": "symmetry", "setup": {"meaning": "m001", "quote": "추궁"}, "payoff": None}],
            "diegesis": {"sp0003": "unclear"}}


def test_span_table_and_fingerprint_change_with_content():
    assert SPANS["sp0003"]["seq"] == 1 and SPANS["sp0003"]["diegesis"] == "recalled"
    assert SPANS["sp0000"]["is_claim"] and SPANS["sp0002"]["screen_text"].startswith("오빠랑")
    fp = em.fingerprint_of(S2)
    import copy
    other = copy.deepcopy(S2)
    other["sequences"][1]["chunks"][0]["meanings"][0]["spans"][0]["screen_text"] = "다른 글자"
    assert em.fingerprint_of(other) != fp and em.fingerprint_of(copy.deepcopy(S2)) == fp


def test_validate_forward_accepts_and_rejects():
    obj, pr, notes = em.validate_forward(_fwd0(), seq=0, rows_by_idx=BY, spans=SPANS, register_ids=set())
    assert pr == [] and obj["facts"][0]["t"] == 0.0 and obj["register"][0]["setup"]["meaning"] == 0
    bad = _fwd0()
    bad["register"][0]["kind"] = "foreshadow"
    assert em.validate_forward(bad, seq=0, rows_by_idx=BY, spans=SPANS, register_ids=set())[0] is None
    bad = _fwd0()
    bad["facts"][0]["meanings"] = ["m099"]
    _, pr, _ = em.validate_forward(bad, seq=0, rows_by_idx=BY, spans=SPANS, register_ids=set())
    assert any("모르는 meaning" in p for p in pr)
    # payoff_of 는 존재하는 레지스터 id 여야 · payoff 는 이 시퀀스의 meaning 이어야
    _, pr, _ = em.validate_forward(_fwd1(), seq=1, rows_by_idx=BY, spans=SPANS, register_ids=set())
    assert any("payoff_of" in p for p in pr)
    obj, pr, notes = em.validate_forward(_fwd1(), seq=1, rows_by_idx=BY, spans=SPANS, register_ids={"r001"})
    assert pr == [] and obj["diegesis"] == {"sp0003": "unclear"}
    wrong_seq = _fwd1()
    wrong_seq["register"][1]["payoff"] = {"meaning": "m000", "quote": "x"}   # setup m001 보다 앞
    _, pr, _ = em.validate_forward(wrong_seq, seq=1, rows_by_idx=BY, spans=SPANS, register_ids={"r001"})
    assert any("다른 시퀀스" in p or "보다 뒤다" in p for p in pr)
    # 다른 시퀀스 span 의 diegesis 는 무시(note) · 오값은 폐기(note)
    d = _fwd0()
    d["diegesis"] = {"sp0003": "imagined", "sp0000": "dream", "sp9999": "unclear"}
    obj, pr, notes = em.validate_forward(d, seq=0, rows_by_idx=BY, spans=SPANS, register_ids=set())
    assert obj is None and any("없는 span" in p for p in pr)
    d["diegesis"].pop("sp9999")
    obj, pr, notes = em.validate_forward(d, seq=0, rows_by_idx=BY, spans=SPANS, register_ids=set())
    assert pr == [] and obj["diegesis"] == {} and len(notes) == 2


def test_merge_forward_assigns_ids_links_payoff_and_records_diegesis():
    m = em.empty_map("fp")
    o0, _, _ = em.validate_forward(_fwd0(), seq=0, rows_by_idx=BY, spans=SPANS, register_ids=set())
    em.merge_forward(m, 0, o0, SPANS)
    assert m["facts_ledger"][0]["id"] == "f001" and m["register"][0]["id"] == "r001"
    o1, _, _ = em.validate_forward(_fwd1(), seq=1, rows_by_idx=BY, spans=SPANS, register_ids={"r001"})
    em.merge_forward(m, 1, o1, SPANS)
    assert m["register"][0]["payoff"]["meaning"] == 1 and m["register"][0]["payoff_seq"] == 1
    assert [r["id"] for r in m["register"]] == ["r001", "r002"]
    assert m["beliefs_ledger"][0]["id"] == "b001" and m["beliefs_ledger"][0]["confirmed"] is None
    assert m["diegesis_final"]["sp0003"] == {"value": "unclear", "evidence": ["forward seq 1"],
                                             "changed_from": "recalled", "stage2": "recalled"}
    assert [s["seq"] for s in m["state_by_sequence"]] == [0, 1]
    state = em.compress_state(m)
    assert "r001" in state and len(state) <= em.STATE_MAX_CHARS
    # 상한 초과 → 사실 본문 생략
    m2 = json.loads(json.dumps(m))
    m2["facts_ledger"] += [{"id": f"f{i:03d}", "text": "가" * 100, "source": "action", "meanings": [0], "t": 0, "seq": 0}
                           for i in range(3, 80)]
    s2 = em.compress_state(m2)
    assert len(s2) <= em.STATE_MAX_CHARS and "생략" in s2


def _merged_map():
    m = em.empty_map("fp")
    o0, _, _ = em.validate_forward(_fwd0(), seq=0, rows_by_idx=BY, spans=SPANS, register_ids=set())
    em.merge_forward(m, 0, o0, SPANS)
    o1, _, _ = em.validate_forward(_fwd1(), seq=1, rows_by_idx=BY, spans=SPANS, register_ids={"r001"})
    em.merge_forward(m, 1, o1, SPANS)
    return m


def test_backward_validate_and_apply():
    m = _merged_map()
    resp = {"diegesis_changes": {"sp0003": {"value": "imagined", "evidence": ["m002: 남편은 일터에 있었다"]}},
            "beliefs_confirmed": {"b001": False},
            "register_payoffs": [{"id": "r002", "payoff": {"meaning": "m002", "quote": "일터"}, "what_flipped": "대칭"}]}
    obj, pr, notes = em.validate_backward(resp, seq=1, rows_by_idx=BY, spans=SPANS, map_doc=m)
    assert pr == []
    em.apply_backward(m, 1, obj, SPANS)
    assert m["diegesis_final"]["sp0003"]["value"] == "imagined"
    assert m["diegesis_final"]["sp0003"]["changed_from"] == "unclear"
    assert m["beliefs_ledger"][0]["confirmed"] is False
    assert m["register"][1]["payoff"]["meaning"] == 2 and m["register"][1]["payoff_pass"] == "backward"
    # 다른 시퀀스의 믿음·레지스터는 반려 · 이미 payoff 있는 항목은 무시(note)
    bad = {"beliefs_confirmed": {"b001": True}}
    assert em.validate_backward(bad, seq=0, rows_by_idx=BY, spans=SPANS, map_doc=m)[0] is None
    dup = {"register_payoffs": [{"id": "r002", "payoff": {"meaning": "m002"}}]}
    obj, pr, notes = em.validate_backward(dup, seq=1, rows_by_idx=BY, spans=SPANS, map_doc=m)
    assert pr == [] and obj["register_payoffs"] == [] and any("이미 payoff" in n for n in notes)
    rv = em.build_review(m, SPANS)
    assert rv[0]["kind"] == "diegesis" and rv[0]["span_ids"] == ["sp0003"] and rv[0]["value"] == "imagined"


def test_overrides_merge_fail_loud():
    m = em.empty_map("fp")
    notes = em.apply_overrides(m, {"diegesis": {"sp0003": "imagined"}, "names": {"유지수": "류지수"},
                                   "register": [{"kind": "object", "setup": {"meaning": "m001", "t": 10.0, "quote": "지갑"}}]},
                               SPANS)
    assert len(notes) == 3 and m["diegesis_final"]["sp0003"]["human"] is True
    assert m["name_overrides"] == {"유지수": "류지수"} and m["register"][0]["id"] == "h001"
    with pytest.raises(ValueError, match="없는 span"):
        em.apply_overrides(em.empty_map("x"), {"diegesis": {"sp9999": "imagined"}}, SPANS)
    with pytest.raises(ValueError, match="값"):
        em.apply_overrides(em.empty_map("x"), {"diegesis": {"sp0003": "dream"}}, SPANS)
    with pytest.raises(ValueError, match="kind"):
        em.apply_overrides(em.empty_map("x"), {"register": [{"kind": "x"}]}, SPANS)
    assert em.apply_overrides(m, None, SPANS) == []


def test_run_episode_map_forward_then_backward_with_reask():
    answers = []
    def call(g, prompt):
        answers.append(prompt)
        if "이야기 상태를 되짚는" in prompt:                      # 역방향
            if "시퀀스 1 의 판정" in prompt:
                return {"diegesis_changes": {"sp0003": {"value": "imagined", "evidence": ["m002"]}},
                        "beliefs_confirmed": {}, "register_payoffs": []}
            return {"diegesis_changes": {}, "beliefs_confirmed": {}, "register_payoffs": []}
        if "이 시퀀스 0 " in prompt:
            if "반려 사유" not in prompt:
                bad = _fwd0(); bad["register"][0]["kind"] = "foreshadow"     # 1차 반려
                return bad
            return _fwd0()
        return _fwd1()
    doc, audit = em.run_episode_map(object(), S2, GRID, work_title="T", research_context="시놉",
                                    character_names=["박경희"], call=call, log=lambda *a: None)
    assert doc["schema"] == em.SCHEMA_MAP and doc["fingerprint"] == em.fingerprint_of(S2)
    assert audit["calls"] == 5 and audit["sequences"] == 2       # fwd 3(반려 1) + bwd 2
    assert doc["register"][0]["payoff"]["meaning"] == 1 and audit["register_paid"] == 1
    assert doc["diegesis_final"]["sp0003"]["value"] == "imagined"
    assert doc["review"][0]["kind"] == "diegesis"
    # 프롬프트 재료: 📄 글자 · ⚠claim · 초벌 판정이 실린다 · 직전 상태가 seq1 에 들어간다
    fwd1 = next(p for p in answers if "이 시퀀스 1 " in p)
    assert '📄 "오빠랑' in fwd1 and "⚠claim 「" not in fwd1 and "r001" in fwd1
    fwd0 = next(p for p in answers if "이 시퀀스 0 " in p)
    assert "⚠claim 갑: 「먼저 인사해도" in fwd0 and "첫 시퀀스" in fwd0
    # 재질의 소진은 크게 실패
    with pytest.raises(ValueError, match="재질의 소진"):
        em.run_episode_map(object(), S2, GRID, work_title="T", call=lambda g, p: {"facts": [{"text": "x", "meanings": ["m099"]}]},
                           log=lambda *a: None)


def test_story_flow_consumption_blocks_and_diegesis_override():
    assert em.register_block(None) == "" and em.facts_block({}) == ""
    m = em.empty_map("fp")
    o0, _, _ = em.validate_forward(_fwd0(), seq=0, rows_by_idx=BY, spans=SPANS, register_ids=set())
    em.merge_forward(m, 0, o0, SPANS)
    blk = em.register_block(m)
    assert "r001 claim" in blk and "회수 없음" in blk and "설정↔회수" in blk
    assert "f001" in em.facts_block(m)
    idx, _ = st.build_span_index(S2, GRID)
    assert idx["sp0003"]["diegesis"] == "recalled"
    m["diegesis_final"]["sp0003"] = {"value": "imagined", "evidence": [], "changed_from": "recalled"}
    m["diegesis_final"]["sp0001"] = {"value": "actual", "evidence": [], "changed_from": None}
    assert em.apply_diegesis_final(idx, m) == 1
    assert idx["sp0003"]["diegesis"] == "imagined" and idx["sp0001"]["diegesis"] is None
    # 게이트: 지도 없음 = 프롬프트에 블록 없음(placeholder 만)
    p = sl.TOPIC_PROMPT.format(target_sec=50, max_sec=60, work_title="T", research_block="", sequence_block="",
                               hint_block="", meaning_block="", silent_block="", exclude_block="",
                               map_block="", strategy_block="", reject_block="")
    assert "레지스터" not in p and "{map_block}" in sl.TOPIC_PROMPT and "{map_block}" in sl.SCENES_PROMPT
    c = em.correction_entry("narration", "문장·화면 모순", t=12.345, span_id="sp0003", frame_path=None)
    assert c == {"stage": "narration", "reason": "문장·화면 모순",
                 "evidence": {"t": 12.345, "span_id": "sp0003", "frame_path": None}}


def test_pipeline_gate_and_cli_flag(tmp_path):
    from app.v3 import cli, pipeline as v3p
    assert "episode_map" in v3p.V3_STEPS and v3p.V3_STEPS.index("chunk_analyze") < v3p.V3_STEPS.index("episode_map") < v3p.V3_STEPS.index("story")
    a = cli.build_parser().parse_args(["--video", "v.mp4", "--work-title", "t", "--episode-map",
                                       "--from-step", "episode_map"])
    assert a.episode_map is True and a.from_step == "episode_map"
    assert cli.build_parser().parse_args(["--video", "v.mp4", "--work-title", "t"]).episode_map is False
    # 미지정 = 파일 없음: _run_m3 의 지도 로드는 use_episode_map 이 False 면 None
    assert v3p.load_episode_map(tmp_path) is None
    src = (Path(__file__).resolve().parents[1] / "app" / "v3" / "pipeline.py").read_text(encoding="utf-8")
    assert "if episode_map and stage2_path.exists():" in src
    assert 'if (use_episode_map and story_flow == "human")' in src
