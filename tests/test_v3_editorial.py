"""v3 편집 지침 입구(2026-09-08) — v1 --editorial-json 과 같은 계약을 걸음 1·2·4 에 넣는다."""
from __future__ import annotations

import argparse

from app.modules.editorial import format_editorial_block, merge_editorial, parse_editorial


def test_v3_story_block_renders_avoid_prefer_tone_and_is_empty_without_editorial():
    ed = {"avoid": ["투표 결과 노출"], "prefer": ["홍지윤이 주인공인 사건"], "tone": "담백하게"}
    b = format_editorial_block(ed, "v3_story")
    assert "금지 요소" in b and "투표 결과 노출" in b and "우선 소재" in b and "홍지윤" in b and "담백하게" in b
    assert "guideline_flags" not in b                       # v1 어휘가 새지 않는다
    assert format_editorial_block(ed, "v3_tone") == "\n## 문구 톤(운영 지시) — 내레이션 문체에만 적용: 담백하게\n"
    assert format_editorial_block({"prefer": ["x"]}, "v3_tone") == ""
    assert format_editorial_block(None, "v3_story") == ""


def test_preset_editorial_merges_with_run_json_avoid_union_prefer_added():
    from app.v3.cli import load_design_preset
    preset = load_design_preset("gawangsho")
    assert preset["editorial"] and any("투표 결과" in s for s in preset["editorial"]["avoid"])
    run = parse_editorial('{"prefer": ["홍지윤이 주인공인 사건"]}')
    merged = merge_editorial(preset["editorial"], run)
    assert merged["avoid"] == preset["editorial"]["avoid"] and merged["prefer"] == ["홍지윤이 주인공인 사건"]


def test_story_flow_appends_editorial_to_topic_and_scene_prompts(monkeypatch):
    """걸음 1·2 프롬프트 뒤에 지침이 붙고, 걸음 4 톤 블록에 톤이 붙는다 — 프롬프트를 가로채 확인."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_v3_stage3 import _mk_grid, _mk_stage2
    from app.v3 import story_flow as sf
    grid = _mk_grid([(0.0, 2.0, True, "왜 여기 있어?"), (2.0, 4.5, True, "말 못 해."), (4.5, 7.0, True, "핵심 대사다.")])
    s2 = _mk_stage2(grid, [(0, 1, 3, "대치"), (2, 2, 5, "핵심")])
    seen: list[str] = []

    class _G:
        pass

    def fake_call(gemini, prompt):
        seen.append(prompt)
        raise ValueError("stop")                          # 걸음 1 첫 호출에서 멈춘다

    monkeypatch.setattr(sf, "call_json", fake_call)
    try:
        sf.run_story_flow(_G(), s2, grid, work_title="t", target_sec=50, max_sec=60,
                          editorial_block="\n## 작품별 편집 지침\n우선 소재: 홍지윤\n",
                          editorial_tone_block="\n## 문구 톤: 담백\n", probe=False, log=lambda *a: None)
    except ValueError:
        pass
    assert seen and "우선 소재: 홍지윤" in seen[0]
