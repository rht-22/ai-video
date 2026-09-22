"""관점(`관점:`/--pov) · 확인 패스 규칙(보존·지시어·계획 승계) · 수리 기준 시각 — 2026-09-22 로또 fast11 v6 실사고 후속."""
from app.tikitaka.guide import parse_guide_text, guide_block, pov_rule
from app.tikitaka.staged import neighbor_anchors
from app.tikitaka.verify import inherit_plans, VERIFY_PROMPT
from app.tikitaka.rebuild import RERANK_PROMPT


def test_pov_key_parses_and_reaches_guide_block_only_when_set():
    g = parse_guide_text("# x\n관점: 공은태 — 선을 긋는 팀장\n")
    assert g["pov"] == "공은태 — 선을 긋는 팀장"
    assert parse_guide_text("# x\n카피: a\n")["pov"] is None
    with_pov = guide_block({"text": "본문", "pov": g["pov"]})
    assert pov_rule(g["pov"]) in with_pov and "이 인물의 눈으로" in with_pov
    assert "관점" not in guide_block({"text": "본문"})            # 없으면 종전 — 사건 자체를 보여준다


def test_rerank_prompt_has_pov_slot_but_empty_by_default():
    assert "{pov_note}" in RERANK_PROMPT
    assert "관점" not in RERANK_PROMPT.replace("{pov_note}", "")


def test_verify_prompt_preserves_and_uses_pointer_words():
    assert "글자 그대로 옮긴다" in VERIFY_PROMPT and "드립 · 팩폭" not in VERIFY_PROMPT
    assert "이렇게 말합니다" in VERIFY_PROMPT and "production_plan" in VERIFY_PROMPT


def test_inherit_plans_by_text_then_by_order():
    plan_a, plan_b = {"cover": [{"span_id": "sp0468"}]}, {"cover": [{"span_id": "sp0470"}]}
    draft = {"items": [{"type": "N", "text": "훅 문장", "production_plan": plan_a}, {"type": "S"}, {"type": "N", "text": "둘째", "production_plan": plan_b}]}
    final = {"items": [{"type": "N", "text": "훅 문장 (다듬음)"}, {"type": "S"}, {"type": "N", "text": "둘째"}]}
    assert inherit_plans(draft, final) == 2
    assert final["items"][0]["production_plan"] == plan_a          # 같은 순번(k번째 N) 승계
    assert final["items"][2]["production_plan"] == plan_b          # 같은 문장 승계
    assert final["items"][0]["production_plan"] is not plan_a      # 사본
    kept = {"items": [{"type": "N", "text": "x", "production_plan": {"cover": []}}]}
    assert inherit_plans(draft, kept) == 0                         # 있으면 손대지 않는다


def test_neighbor_anchors_prefer_adjacent_items_not_source_start():
    windows = [(700.0, 705.0, "S", 0), (720.0, 726.0, "S", 2), (730.0, 733.0, "A", 3)]
    assert neighbor_anchors(windows, 1) == [705.0, 720.0]          # 앞 항목 끝 · 뒤 항목 시작
    assert neighbor_anchors(windows, 0) == [720.0]
    assert neighbor_anchors([], 0) == [0.0]                        # S/A 가 없을 때만 종전 폴백
