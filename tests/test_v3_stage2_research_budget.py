"""Stage 2 프롬프트의 리서치 예산(2026-09-08) — 1,200자 절단이 [핵심 갈등/서사]를 잘라먹던 것."""
from app.v3.chunk_analyze import STAGE2_RESEARCH_MAX_CHARS, build_stage2_prompt


def test_stage2_prompt_carries_research_beyond_1200_chars():
    ctx = "시놉시스: " + "가" * 1300 + "\n[핵심 갈등/서사]\n- 가왕 3인 대결"
    chunk = {"seq_number": 0, "chunk_number": 0, "start_sec": 0.0, "end_sec": 10.0}
    p = build_stage2_prompt(chunk, {"sequences": []}, [], None, research_context=ctx)
    assert STAGE2_RESEARCH_MAX_CHARS >= 3000
    assert "[핵심 갈등/서사]" in p and "가왕 3인 대결" in p
