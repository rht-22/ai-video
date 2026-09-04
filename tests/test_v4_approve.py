"""V4-M1 — 9단계 승인 게이트 회귀 가드.

계약 `docs/v4/M1-interfaces.md` §6 · 기획 `docs/v4/v4-plan.md` §3(9)·§7 · 운영자 결정 O7.

무엇을 왜 고정하는가:

① **"나가도 되는 영상은 전부 만든다"** — 결함 게이트 전부 통과가 조건이고 점수 임계는
   없다. top-3 를 이 결정이 대체했으므로 승인 5편이 5편으로 나오는 것을 값으로 박제한다
   (v1 의 3-클램프가 새어 들어오면 4번째부터 조용히 사라진다 — `tests/test_v4_m0.py` ③).
② **미채점 ≠ 0점** — 8단계 호출이 실패한 후보는 승인 불가지만 '나쁘다'가 아니다.
   사유 어휘가 `unscored` 로 **구분**되는 것이 계약이다(기획서 §7 실패 3갈래).
③ **승인 0 이면 1위를 경고와 함께** — 무인 노드의 조용한 결번 금지. 폴백 편의 탈락 사유가
   보이는 곳에 남는지, 같은 id 가 승인·탈락 양쪽에 없는지까지 고정한다.
④ **조용한 절단·클램프 금지** — 상한으로 자른 편수는 `capped`, 범위 밖 상한은 ValueError.
⑤ **순수·결정성** — 넘겨받은 자료가 안 바뀌고, 같은 입력이면 같은 출력(순서까지).
"""
from __future__ import annotations

import copy

import pytest

from app.v4.approve import (
    FLAGS_STATUS_OK,
    MAX_SHORTS_DEFAULT,
    MAX_SHORTS_LIMIT,
    MAX_SHORTS_MIN,
    REASON_CAPPED,
    REASON_FUNNEL,
    REASON_HOOK_WEAK,
    REASON_SEAM_JUMP,
    REASON_UNRANKED,
    REASON_UNSCORED,
    REASON_VERIFY,
    approve,
    clamp_max_shorts,
    is_approved,
    scored_flags,
)


def clean(cand_id: str) -> dict:
    """결함 없는 8단계 채점 결과 — 두 플래그 다 False."""
    return {"seam_jump": False, "hook_weak": False, "evidence_sec": []}


def case(ids: list[str], *, flags: dict | None = None,
         funnel_kept: list[str] | None = None,
         verify_kept: list[str] | None = None) -> dict:
    """6c·7·8·순위가 전부 통과한 표준 입력. 개별 테스트가 필요한 절만 바꿔 쓴다."""
    kept = ids if funnel_kept is None else funnel_kept
    vkept = ids if verify_kept is None else verify_kept
    return {
        "funnel": {"kept": list(kept), "dropped": []},
        "verify": {"kept": list(vkept), "dropped": []},
        "flags": {i: clean(i) for i in ids} if flags is None else flags,
        # 순위는 소프트 점수 순(낮을수록 좋다 — funnel.score 계약)
        "ranking": [{"id": i, "score": float(n), "signals": {}}
                    for n, i in enumerate(ids)],
    }


# ── ① 결함 없으면 전부 승인 (O7 · top-K 아님) ───────────────────────────────

def test_all_clean_candidates_are_all_approved():
    """5편이 게이트를 다 통과하면 5편 전부 승인된다 — 순위는 번호용일 뿐이다."""
    ids = ["c01", "c02", "c03", "c04", "c05"]
    out = approve(**case(ids))
    assert out["approved"] == ids            # 순위 순 = 파일 번호 순
    assert out["rejected"] == []
    assert out["fallback"] is False and out["fallback_id"] is None
    assert out["capped"] == 0
    assert out["scoring_unavailable"] is False
    assert out["considered"] == 5


def test_v1_three_clamp_is_not_inherited():
    """🛑 O7 의 실패 모드 — v1 은 max_shorts 를 1~3 으로 조인다(cli.py:632 ·
    pipeline.py:3269 · config.max_shorts_count=3). 그 클램프가 v4 로 새면 4번째
    승인 편부터 **조용히 사라진다**. 기본 상한이 8 이고 8편이 8편으로 나오는 것을 박제한다."""
    assert MAX_SHORTS_DEFAULT == 8 and MAX_SHORTS_LIMIT == 8 and MAX_SHORTS_MIN == 1
    ids = [f"c{n:02d}" for n in range(1, 9)]
    out = approve(**case(ids))
    assert out["approved"] == ids and len(out["approved"]) == 8
    assert out["capped"] == 0 and out["max_shorts"] == 8


def test_approval_order_follows_ranking_not_candidate_id():
    """승인 순서는 **순위**를 따른다(파일 번호·발행 순서가 이것으로 정해진다).
    후보 부여 순서(id)와 순위는 다른 것이라는 §8 규약을 눈에 보이게 고정한다."""
    ids = ["c03", "c01", "c02"]               # 순위 1위가 c03
    out = approve(**case(ids))
    assert out["approved"] == ["c03", "c01", "c02"]


# ── ② 결함 게이트 — 하나만 걸려도 탈락 ──────────────────────────────────────

@pytest.mark.parametrize("flag,reason", [("seam_jump", REASON_SEAM_JUMP),
                                         ("hook_weak", REASON_HOOK_WEAK)])
def test_single_visual_flag_rejects_the_candidate(flag, reason):
    """이음새 튐·훅 약함 중 **하나만** true 여도 못 나간다 — 점수로 상쇄되지 않는다."""
    ids = ["c01", "c02"]
    kw = case(ids)
    kw["flags"]["c02"] = {**clean("c02"), flag: True}
    out = approve(**kw)
    assert out["approved"] == ["c01"]
    assert out["rejected"] == [{"id": "c02", "reasons": [reason]}]
    assert out["fallback"] is False


def test_funnel_and_verify_failures_are_recorded_separately():
    """7단계 하드 탈락과 6c 미통과는 **다른 사유**다. 사유는 첫 하나에서 멈추지 않고
    해당하는 것을 전부 담는다(하나씩 알려주면 재시도를 반복하게 된다)."""
    ids = ["c01", "c02", "c03"]
    kw = case(ids, funnel_kept=["c01", "c03"], verify_kept=["c01"])
    out = approve(**kw)
    assert out["approved"] == ["c01"]
    assert out["rejected"] == [
        {"id": "c02", "reasons": [REASON_FUNNEL, REASON_VERIFY]},
        {"id": "c03", "reasons": [REASON_VERIFY]},
    ]


def test_is_approved_returns_every_applicable_reason_in_fixed_order():
    ok, reasons = is_approved("c09", funnel_kept=set(), verify_ok=set(), flags={})
    assert ok is False
    assert reasons == [REASON_FUNNEL, REASON_VERIFY, REASON_UNSCORED]
    ok2, reasons2 = is_approved(
        "c01", funnel_kept={"c01"}, verify_ok={"c01"},
        flags={"c01": {"seam_jump": True, "hook_weak": True}})
    assert ok2 is False and reasons2 == [REASON_SEAM_JUMP, REASON_HOOK_WEAK]


def test_is_approved_reads_flags_as_an_id_keyed_map():
    """§8 자료 모양 그대로 — `flags` 는 후보 id 로 키가 잡힌 지도다. 다른 후보의
    플래그가 이 후보의 판정에 새면 안 된다."""
    flags = {"c01": clean("c01"), "c02": {"seam_jump": True, "hook_weak": False}}
    assert is_approved("c01", funnel_kept={"c01"}, verify_ok={"c01"},
                       flags=flags) == (True, [])


# ── ③ 미채점은 0점이 아니다 ─────────────────────────────────────────────────

@pytest.mark.parametrize("entry", [
    None,                                                   # 항목 자체가 없다(호출 전)
    {"status": "failed", "reason": "503", "attempts": 3},    # 호출 실패
    {"seam_jump": False},                                    # 응답 절단 — 한 플래그만
    "not-a-dict",                                            # 모양이 깨진 기록
])
def test_unscored_is_rejected_but_with_its_own_reason(entry):
    """8단계가 못 채점한 후보는 승인 불가다. 그러나 사유는 `seam_jump` 가 아니라
    **`unscored`** 여야 한다 — 0점이 아니라 '모른다'이고, 검수가 둘을 구분해야
    재실행으로 살릴 수 있다(기획서 §7 실패 3갈래)."""
    ids = ["c01", "c02"]
    kw = case(ids)
    if entry is None:
        del kw["flags"]["c02"]
    else:
        kw["flags"]["c02"] = entry
    out = approve(**kw)
    assert out["approved"] == ["c01"]
    assert out["rejected"] == [{"id": "c02", "reasons": [REASON_UNSCORED]}]
    # 미채점은 '결함 있음'이 아니다 — 결함 사유가 붙지 않는다
    assert REASON_SEAM_JUMP not in out["rejected"][0]["reasons"]
    assert REASON_HOOK_WEAK not in out["rejected"][0]["reasons"]
    # 한 후보만 미채점이면 전량 실패가 아니다
    assert out["scoring_unavailable"] is False


def test_scored_flags_accepts_explicit_ok_status():
    assert scored_flags({"c01": {"status": FLAGS_STATUS_OK, "seam_jump": False,
                                 "hook_weak": True}}, "c01") == {
        "seam_jump": False, "hook_weak": True}
    assert scored_flags({}, "c01") is None


def test_non_bool_flag_fails_loud():
    """`"false"`(문자열)를 False 로 읽으면 결함 편이 조용히 발행되고, True 로 읽으면
    멀쩡한 편이 조용히 죽는다 — 추측으로 정할 값이 아니라 크게 실패시킨다."""
    with pytest.raises(ValueError, match="bool"):
        scored_flags({"c01": {"seam_jump": "false", "hook_weak": False}}, "c01")
    with pytest.raises(ValueError):
        approve(**case(["c01"], flags={"c01": {"seam_jump": 1, "hook_weak": False}}))


def test_scoring_unavailable_only_when_every_ranked_candidate_is_unscored():
    """전량 실패는 별개 신호다(§7 3갈래의 셋째) — 결정적 신호만으로 순위가 정해진
    상태라는 뜻이라 검수가 알아야 한다."""
    ids = ["c01", "c02"]
    out = approve(**case(ids, flags={}))
    assert out["scoring_unavailable"] is True
    # 전량 미채점이면 승인 0 → 폴백이 1위를 살린다
    assert out["approved"] == ["c01"] and out["fallback"] is True
    assert out["fallback_reasons"] == [REASON_UNSCORED]
    # 후보가 아예 없으면 '채점 불가'라고 말할 대상도 없다
    empty = approve(funnel={"kept": []}, verify={"kept": []}, flags={}, ranking=[])
    assert empty["scoring_unavailable"] is False


# ── ③-b 순위 없는 후보는 조용히 사라지지 않는다 ─────────────────────────────

def test_candidate_without_a_rank_is_rejected_as_unranked_not_dropped():
    """게이트를 다 통과했어도 순위가 없으면 파일 번호를 줄 근거가 없다.
    그렇다고 조용히 버리지도 않는다 — 이름이 `unranked` 사유로 남는다."""
    kw = case(["c01"])
    kw["funnel"]["kept"].append("c07")          # 7단계는 남겼는데 순위에 없다
    kw["flags"]["c07"] = clean("c07")
    kw["verify"]["kept"].append("c07")
    out = approve(**kw)
    assert out["approved"] == ["c01"]
    assert out["rejected"] == [{"id": "c07", "reasons": [REASON_UNRANKED]}]
    assert out["considered"] == 2


def test_dropped_candidates_from_upstream_stages_still_appear_in_the_audit():
    """6c·7 에서 탈락해 순위에도 없는 후보까지 기록에 남는다 — 어느 단계가 남긴
    후보든 승인/탈락 어느 한쪽에는 반드시 이름이 있다(조용한 드롭 금지)."""
    kw = case(["c01"])
    kw["funnel"]["dropped"] = [{"id": "c04", "reasons": ["teaser_overlap"]}]
    kw["verify"]["dropped"] = [{"id": "c05", "why": "quote_not_found"}]
    out = approve(**kw)
    assert out["approved"] == ["c01"]
    assert [r["id"] for r in out["rejected"]] == ["c04", "c05"]   # id 정렬(결정성)
    for row in out["rejected"]:
        assert row["reasons"] == [REASON_FUNNEL, REASON_VERIFY,
                                  REASON_UNSCORED, REASON_UNRANKED]


# ── ④ 승인 0 → 1위 폴백 (무인 노드의 조용한 결번 금지) ──────────────────────

def test_all_rejected_falls_back_to_rank_one_with_a_warning():
    """레포에 4중으로 깔린 '최소 1편 보장'의 다섯 번째다(pipeline.py:3446·3586·3598 ·
    v3/story.fallback_highlight). 폴백은 조용하지 않다 — 왜 탈락했는지가 함께 남는다."""
    ids = ["c01", "c02"]
    kw = case(ids)
    kw["flags"]["c01"] = {**clean("c01"), "hook_weak": True}
    kw["flags"]["c02"] = {**clean("c02"), "seam_jump": True}
    out = approve(**kw)
    assert out["fallback"] is True
    assert out["approved"] == ["c01"] and out["fallback_id"] == "c01"
    assert out["fallback_reasons"] == [REASON_HOOK_WEAK]     # 사유가 보이는 곳에 남는다
    # 같은 id 가 승인·탈락 양쪽에 있으면 하류가 어느 쪽을 믿을지 정할 수 없다
    assert [r["id"] for r in out["rejected"]] == ["c02"]


def test_fallback_is_impossible_without_a_ranking_and_says_so_quietly():
    """후보가 0 인 것은 6단계의 실패다 — 이 단계가 지어낼 일이 아니다
    (판정 근거가 없으면 판정하지 않는다)."""
    out = approve(funnel={"kept": []}, verify={"kept": []}, flags={}, ranking=[])
    assert out["approved"] == [] and out["fallback"] is False
    assert out["fallback_id"] is None and out["rejected"] == []


def test_fallback_does_not_trigger_when_at_least_one_passes():
    kw = case(["c01", "c02"])
    kw["flags"]["c01"] = {**clean("c01"), "seam_jump": True}
    out = approve(**kw)
    assert out["approved"] == ["c02"] and out["fallback"] is False
    assert out["fallback_reasons"] == []


# ── ⑤ 상한 — 조용한 절단 금지 ───────────────────────────────────────────────

def test_max_shorts_cap_records_how_many_were_cut():
    ids = [f"c{n:02d}" for n in range(1, 6)]
    out = approve(**case(ids), max_shorts=2)
    assert out["approved"] == ["c01", "c02"]
    assert out["capped"] == 3 and out["max_shorts"] == 2
    # 잘린 편은 '결함'이 아니라 상한이라고 적는다
    assert [r["id"] for r in out["rejected"]] == ["c03", "c04", "c05"]
    for row in out["rejected"]:
        assert row["reasons"] == [REASON_CAPPED]


def test_cap_cuts_the_tail_of_the_ranking_not_an_arbitrary_subset():
    """자를 때는 **순위 아래쪽**을 자른다 — 1위가 사라지면 파일 번호가 뒤집힌다."""
    out = approve(**case(["c03", "c01", "c02"]), max_shorts=1)
    assert out["approved"] == ["c03"] and out["capped"] == 2


def test_capped_reason_is_appended_to_existing_reasons():
    """상한에 잘린 편이 원래 결함으로 탈락한 편의 사유를 잃지 않는다."""
    ids = ["c01", "c02", "c03"]
    kw = case(ids)
    kw["flags"]["c02"] = {**clean("c02"), "hook_weak": True}
    out = approve(**kw, max_shorts=1)
    assert out["approved"] == ["c01"] and out["capped"] == 1
    rej = {r["id"]: r["reasons"] for r in out["rejected"]}
    assert rej["c02"] == [REASON_HOOK_WEAK]          # 상한과 무관하게 탈락
    assert rej["c03"] == [REASON_CAPPED]


@pytest.mark.parametrize("value,expected", [(None, MAX_SHORTS_DEFAULT), (1, 1), (8, 8)])
def test_clamp_max_shorts_accepts_the_allowed_range(value, expected):
    assert clamp_max_shorts(value) == expected


@pytest.mark.parametrize("value", [0, -1, 9, 100])
def test_clamp_max_shorts_fails_loud_outside_the_range(value):
    """v1 은 `min(max(v, 1), 3)` 으로 말없이 조여서, 8을 준 사람이 3편만 받고도
    그 사실을 모른다. 여기서는 죽는다 — 설정 실수는 시작 전에 아는 것이 낫다."""
    with pytest.raises(ValueError, match="max_shorts"):
        clamp_max_shorts(value)


@pytest.mark.parametrize("value", [True, 2.0, "3", [3]])
def test_clamp_max_shorts_rejects_non_int(value):
    """bool 은 int 의 하위형이라 True 가 1 로 통과한다 — 넘어온 것이 플래그면 배선 실수다."""
    with pytest.raises(ValueError):
        clamp_max_shorts(value)


def test_approve_validates_max_shorts_before_judging():
    with pytest.raises(ValueError):
        approve(**case(["c01"]), max_shorts=0)


# ── ⑥ 계약 위반은 크게 실패한다 ─────────────────────────────────────────────

def test_duplicate_rank_entry_fails_loud():
    """같은 후보가 순위에 두 번 있으면 어느 편이 shorts_2.mp4 인지 실행마다 달라진다."""
    kw = case(["c01"])
    kw["ranking"].append({"id": "c01", "score": 9.0})
    with pytest.raises(ValueError, match="중복"):
        approve(**kw)


def test_unknown_kept_shape_fails_loud():
    """`kept` 원소는 id 문자열 또는 'id' 키를 가진 dict 다. 모르는 모양을 추측으로
    통과시키면 후보가 조용히 증발한다."""
    with pytest.raises(TypeError):
        approve(funnel={"kept": [42]}, verify={"kept": []}, flags={}, ranking=[])
    with pytest.raises(ValueError, match="id"):
        approve(funnel={"kept": [{"cand": "c01"}]}, verify={"kept": []},
                flags={}, ranking=[])


def test_non_string_flags_key_fails_loud():
    """`flags` 는 후보 id 로 키가 잡힌 지도다 — 키가 문자열이 아니면 배선 실수이고,
    조용히 건너뛰면 그 후보가 감사 기록에서 통째로 증발한다."""
    with pytest.raises(ValueError, match="flags"):
        approve(funnel={"kept": []}, verify={"kept": []}, flags={7: clean("c07")},
                ranking=[])


def test_kept_accepts_both_id_strings_and_candidate_dicts():
    """계약이 verify 쪽은 `[id...]`, funnel 쪽은 `[...]` 로만 적어 놓아 둘 다 받는다."""
    out = approve(funnel={"kept": [{"id": "c01", "segments": []}]},
                  verify={"kept": ["c01"]},
                  flags={"c01": clean("c01")},
                  ranking=[{"id": "c01", "score": 0.0}])
    assert out["approved"] == ["c01"]


# ── ⑦ 순수성·결정성 ────────────────────────────────────────────────────────

def test_approve_does_not_mutate_its_inputs():
    kw = case(["c01", "c02", "c03"], funnel_kept=["c01", "c02"])
    kw["funnel"]["dropped"] = [{"id": "c09", "reasons": ["out_of_source"]}]
    before = copy.deepcopy(kw)
    approve(**kw, max_shorts=1)
    assert kw == before


def test_approve_is_deterministic():
    """같은 입력이면 순서까지 같은 출력 — 재개마다 파일 번호가 흔들리면 안 된다
    (E15 재개 계약과 같은 이유)."""
    kw = case(["c05", "c02", "c09"], funnel_kept=["c05", "c02"])
    kw["verify"]["dropped"] = [{"id": "c11", "why": "hallucination"}]
    first = approve(**copy.deepcopy(kw), max_shorts=3)
    for _ in range(3):
        assert approve(**copy.deepcopy(kw), max_shorts=3) == first


def test_reason_vocabulary_is_frozen():
    """이 문자열은 run_log·checkpoint_candidates·검수 카드에 그대로 실린다 —
    바꾸면 저장된 잡의 감사 기록과 대조가 끊긴다."""
    assert (REASON_FUNNEL, REASON_VERIFY, REASON_UNSCORED, REASON_SEAM_JUMP,
            REASON_HOOK_WEAK, REASON_UNRANKED, REASON_CAPPED) == (
        "funnel_dropped", "verify_failed", "unscored",
        "seam_jump", "hook_weak", "unranked", "capped")


def test_approval_record_always_carries_the_same_keys():
    """감사 기록의 모양이 실행마다 달라지면 run_log 를 읽는 쪽이 키 유무를 추측해야 한다."""
    keys = {"approved", "rejected", "fallback", "fallback_id", "fallback_reasons",
            "capped", "max_shorts", "scoring_unavailable", "considered",
            # 승인 0 이고 폴백조차 못 세운 상태 — 배선이 크게 실패할 근거다.
            "no_publishable"}
    assert set(approve(**case(["c01"])).keys()) == keys
    assert set(approve(funnel={}, verify={}, flags={}, ranking=[]).keys()) == keys
