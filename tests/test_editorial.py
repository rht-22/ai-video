"""작품별 편집 지침(editorial) — 파싱·병합·프롬프트 렌더링·후보 필터 (2026-08-20).

app/modules/editorial.py 계약 검증. 핵심 위험 두 가지를 테스트로 고정한다:
① 조용한 무시(오타 키·잘못된 값이 기본값으로 발행) — 즉시 실패해야 한다.
② avoid 완화(실행 단위 지시가 권리 제약을 풀어버림) — 병합은 합집합만이어야 한다.
"""
from __future__ import annotations

import pytest

from app.modules.editorial import (
    filter_flagged_candidates,
    filter_log_entry,
    format_editorial_block,
    merge_editorial,
    parse_editorial,
)


# ── parse ──

def test_parse_full_schema():
    ed = parse_editorial('{"avoid": ["경연 결과"], "prefer": ["무대"], "tone": "차분"}')
    assert ed == {"avoid": ["경연 결과"], "prefer": ["무대"], "tone": "차분"}


def test_parse_empty_inputs_are_none():
    assert parse_editorial(None) is None
    assert parse_editorial("") is None
    assert parse_editorial("  ") is None
    assert parse_editorial("{}") is None
    assert parse_editorial('{"avoid": [], "tone": "  "}') is None


def test_parse_underscore_keys_are_docs_and_ignored():
    # works.json 카드의 _note 가 플래그에 실려 와도 계약 위반이 아니어야 한다
    assert parse_editorial('{"avoid": ["x"], "_note": "문서"}') == {"avoid": ["x"]}


def test_parse_unknown_key_fails_loud():
    # 오타(avoids)가 조용히 무시되면 지침 없이 밤새 생성된다 — 즉시 실패
    with pytest.raises(ValueError, match="알 수 없는 키"):
        parse_editorial('{"avoids": ["x"]}')


def test_parse_wrong_types_fail_loud():
    with pytest.raises(ValueError, match="avoid"):
        parse_editorial('{"avoid": "문자열"}')
    with pytest.raises(ValueError, match="tone"):
        parse_editorial('{"tone": ["배열"]}')
    with pytest.raises(ValueError, match="객체"):
        parse_editorial('["배열"]')
    with pytest.raises(ValueError, match="파싱 실패"):
        parse_editorial("{broken")


# ── merge (상시 카드 ⊕ 실행 단위) ──

def test_merge_avoid_is_union_never_relaxed():
    # 실행 단위 지시가 avoid 를 비워 보내도 상시 금지는 그대로 — 권리 제약에 "이번만 예외"는 없다
    base = {"avoid": ["경연 결과"], "prefer": ["무대"]}
    run = {"prefer": ["전유진"], "avoid": []}
    merged = merge_editorial(base, run)
    assert merged["avoid"] == ["경연 결과"]
    assert merged["prefer"] == ["무대", "전유진"]


def test_merge_run_tone_wins_and_avoid_accumulates():
    base = {"avoid": ["A"], "tone": "기본톤"}
    run = {"avoid": ["B", "A"], "tone": "이번톤"}
    merged = merge_editorial(base, run)
    assert merged["avoid"] == ["A", "B"]  # 합집합, 중복 없음
    assert merged["tone"] == "이번톤"


def test_merge_handles_none_sides():
    base = {"avoid": ["A"]}
    assert merge_editorial(base, None) == base
    assert merge_editorial(None, base) == base
    assert merge_editorial(None, None) is None


# ── format (단계별 렌더링) ──

def test_empty_editorial_leaves_prompt_untouched():
    # 지침 없는 실행은 프롬프트가 한 글자도 달라지면 안 된다 (reject_note 규약)
    assert format_editorial_block(None, "analysis") == ""
    assert format_editorial_block(None, "story") == ""


def test_analysis_block_tags_but_never_narrows():
    block = format_editorial_block(
        {"avoid": ["경연 결과"], "prefer": ["무대 하이라이트"]}, "analysis")
    assert "guideline_flags" in block          # 태깅 지시
    assert "제외하지 말라" in block              # 후보 축소 금지
    assert "경연 결과" in block
    assert "무대 하이라이트" in block
    assert "상세히" in block                    # prefer = 기술을 두껍게
    # 청크 단계에 하드 필터·문체 지시가 새면 안 된다
    assert "사용 금지" not in block


def test_analysis_block_tone_not_injected():
    # tone 은 선정 기준으로 오독될 수 있어 청크 분석에는 미주입
    block = format_editorial_block({"tone": "차분한 톤"}, "analysis")
    assert block == ""


def test_story_block_hard_filters_scene_and_wording():
    block = format_editorial_block({"avoid": ["경연 결과"]}, "story")
    assert "절대 규칙" in block
    assert "guideline_flags" in block           # 태깅된 후보 사용 금지
    assert "title" in block and "tts_cues" in block  # 문구 스포 차단
    assert "✅" in block                         # 금지가 아닌 것 명시 — 과잉 회피 방지


def test_story_block_prefer_is_bias_not_rule():
    block = format_editorial_block({"prefer": ["무대 하이라이트"]}, "story")
    assert "절대 규칙 아님" in block
    assert "억지로 끼워 맞추지" in block          # 소재 부족 회차 보호


def test_story_block_tone_scoped_to_wording():
    block = format_editorial_block({"tone": "차분한 다큐 톤"}, "story")
    assert "차분한 다큐 톤" in block
    assert "장면 선택 기준이 아니다" in block


def test_unknown_use_case_fails_loud():
    with pytest.raises(ValueError, match="use_case"):
        format_editorial_block({"avoid": ["x"]}, "tts")


# ── filter (스토리 구성 전 코드 레벨 하드 필터) ──

CANDS = [
    {"chunk_index": 0, "start_sec": 1.0, "end_sec": 5.0, "guideline_flags": []},
    {"chunk_index": 0, "start_sec": 10.0, "end_sec": 15.0, "guideline_flags": ["경연 결과"]},
    {"chunk_index": 1, "start_sec": 2.0, "end_sec": 6.0},  # 옛 체크포인트(태깅 없음)
]


def test_filter_drops_only_flagged():
    kept, dropped = filter_flagged_candidates(CANDS, {"avoid": ["경연 결과"]})
    assert len(kept) == 2 and len(dropped) == 1
    assert dropped[0]["guideline_flags"] == ["경연 결과"]


def test_filter_without_avoid_passes_everything():
    # avoid 없으면 태깅 자체가 요청되지 않았다 — 전부 통과 (prefer 만 있는 작품 포함)
    kept, dropped = filter_flagged_candidates(CANDS, {"prefer": ["무대"]})
    assert len(kept) == 3 and dropped == []
    kept, dropped = filter_flagged_candidates(CANDS, None)
    assert len(kept) == 3 and dropped == []


def test_filter_ignores_blank_flags():
    kept, dropped = filter_flagged_candidates(
        [{"guideline_flags": ["", "  "]}], {"avoid": ["x"]})
    assert len(kept) == 1 and dropped == []


# ── run_log 기록 (검수함·편집실이 읽는 provenance) ──

def test_filter_log_entry_shape():
    kept, dropped = filter_flagged_candidates(
        [{"chunk_index": 7, "start_sec": 2841.5, "end_sec": 2893.0,
          "guideline_flags": ["경연 결과"], "description": "순위 발표", "score": 0.9},
         {"chunk_index": 1, "start_sec": 10.0, "end_sec": 20.0, "guideline_flags": []}],
        {"avoid": ["경연 결과"]})
    entry = filter_log_entry(kept, dropped)
    assert entry["step"] == "editorial_filter"
    assert entry["kept"] == 1
    assert entry["dropped"] == [{
        "chunk_index": 7, "start_sec": 2841.5, "end_sec": 2893.0,
        "guideline_flags": ["경연 결과"], "description": "순위 발표",
    }]  # score 등 무관 필드는 싣지 않는다 — 검수 판단에 필요한 것만


def test_filter_log_entry_records_zero_drops():
    # "필터가 돌았는데 걸린 게 없다"와 "필터가 안 돌았다"는 다른 사실이다
    entry = filter_log_entry([{"chunk_index": 0}], [])
    assert entry == {"step": "editorial_filter", "kept": 1, "dropped": []}


# ── CLI 배선 (조용한 무시 방지: 플래그 → PipelineInput) ──

def test_cli_parses_editorial_flags():
    from app.cli import build_parser
    p = build_parser()
    args = p.parse_args([
        "create_shorts", "--title", "T", "--video", "x.mp4",
        "--editorial-json", '{"avoid": ["A"]}',
        "--editorial-run-json", '{"prefer": ["B"]}',
    ])
    merged = merge_editorial(parse_editorial(args.editorial_json),
                             parse_editorial(args.editorial_run_json))
    assert merged == {"avoid": ["A"], "prefer": ["B"]}
