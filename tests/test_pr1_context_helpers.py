"""PR-1 정리 — _format_work_context / _format_episodes_context / _format_segments_summary 골든 테스트.

목적: 인라인으로 흩어져 있던 블록 빌딩 코드를 헬퍼로 추출했을 때 *기존과 완전히 동일한 문자열* 을
반환해야 동작 변화 0이 보장된다. 헬퍼 출력을 직접 인라인 구현과 비교한다.
"""
from __future__ import annotations

from app.modules.gemini_client import (
    _format_episodes_context,
    _format_segments_summary,
    _format_work_context,
)


# ──────────────────────────────────────────────────────────────
# _format_work_context — analyze_chunk / compose_story / plan_tts_cues 의
# 원본 인라인 구현과 1:1 비교
# ──────────────────────────────────────────────────────────────


def _inline_work_analysis(work_context: str) -> str:
    return (
        f"\n[작품 정보 — 인물 식별·스토리 이해 참고용]\n{work_context}\n"
        "⚠️ 위 정보는 인물명·관계·장르를 정확히 파악하기 위한 참고용입니다.\n"
        "장면 선택 기준은 리서치가 아니라 오직 현재 영상 내 감정 강도·반응·의외성으로 판단하세요.\n"
    )


def _inline_work_story(work_context: str) -> str:
    return (
        f"\n[작품 정보 — 인물 식별·장르 이해 참고용]\n{work_context}\n"
        "⚠️ 위 정보는 인물명·관계·장르를 정확히 파악하기 위한 참고용입니다.\n"
        "스토리라인 구성 기준은 리서치가 아니라 후보 장면들의 감정 강도·반응·의외성입니다.\n"
    )


def _inline_work_tts(work_context: str) -> str:
    return f"\n[작품 정보]\n{work_context}\n"


def test_format_work_context_analysis_matches_inline():
    ctx = "어떤 작품 시놉시스"
    assert _format_work_context(ctx, use_case="analysis") == _inline_work_analysis(ctx)


def test_format_work_context_story_matches_inline():
    ctx = "어떤 작품 시놉시스"
    assert _format_work_context(ctx, use_case="story") == _inline_work_story(ctx)


def test_format_work_context_tts_matches_inline():
    ctx = "어떤 작품 시놉시스"
    assert _format_work_context(ctx, use_case="tts") == _inline_work_tts(ctx)


def test_format_work_context_empty_returns_empty_string():
    assert _format_work_context(None) == ""
    assert _format_work_context("") == ""


# ──────────────────────────────────────────────────────────────
# _format_episodes_context
# ──────────────────────────────────────────────────────────────


def _inline_episodes_analysis(prev: str) -> str:
    return (
        f"\n[이전 에피소드 배경 정보 — 오해 방지 전용]\n{prev}\n"
        "⚠️ 위 정보는 인물명·관계·사건을 올바르게 식별하기 위한 참고용입니다.\n"
        "장면 선택 기준은 오직 현재 첨부 영상 안에서의 재미·흥미도·화제성입니다.\n"
        "이전 화와의 연관성이 높다는 이유만으로 장면을 선택하거나 높게 평가하지 마세요.\n"
        "타임스탬프는 반드시 현재 첨부 영상의 시작(0초) 기준으로 계산하세요.\n"
    )


def _inline_episodes_story(prev: str) -> str:
    return (
        f"\n[이전 에피소드 배경 정보 — 오해 방지 전용]\n{prev}\n"
        "⚠️ 위 정보는 인물명·관계·사건을 올바르게 파악하기 위한 참고용입니다.\n"
        "스토리라인 구성 기준은 이번 화 후보 장면들의 재미·흥미도·화제성입니다.\n"
        "이전 화와의 연관성이 높다는 이유만으로 장면을 선택하거나 높게 평가하지 마세요.\n"
    )


def _inline_episodes_tts(prev: str) -> str:
    return f"\n[이전 에피소드 요약]\n{prev}\n"


def test_format_episodes_context_analysis_matches_inline():
    prev = "지난 화 줄거리"
    assert _format_episodes_context(prev, use_case="analysis") == _inline_episodes_analysis(prev)


def test_format_episodes_context_story_matches_inline():
    prev = "지난 화 줄거리"
    assert _format_episodes_context(prev, use_case="story") == _inline_episodes_story(prev)


def test_format_episodes_context_tts_matches_inline():
    prev = "지난 화 줄거리"
    assert _format_episodes_context(prev, use_case="tts") == _inline_episodes_tts(prev)


def test_format_episodes_context_empty_returns_empty_string():
    assert _format_episodes_context(None) == ""
    assert _format_episodes_context("") == ""


# ──────────────────────────────────────────────────────────────
# _format_segments_summary
# ──────────────────────────────────────────────────────────────


def _inline_segments(chunk_meta: list[dict], use_case: str) -> str:
    if not chunk_meta:
        return ""
    parts: list[str] = []
    for cm in chunk_meta:
        ci = cm.get("chunk_index", 0)
        summary = (cm.get("summary") or "").strip().replace("\n", " ")
        segs = cm.get("segments") or []
        lines: list[str] = [f"\n[chunk {ci}] 요약: {summary[:120]}"]
        for s in segs:
            ss = float(s.get("start_sec", 0))
            ee = float(s.get("end_sec", 0))
            desc = (s.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 100:
                desc = desc[:100] + "…"
            lines.append(f"  - {ss:>6.1f}~{ee:>6.1f}s  {desc}")
        parts.append("\n".join(lines))
    if not parts:
        return ""
    joined = "\n".join(parts).replace("{", "{{").replace("}", "}}")
    if use_case == "story":
        return (
            "\n\n[전체 장면 흐름 (segments) — 청크별 시간순]\n"
            "(이 요약은 영상 전체 흐름을 빈틈없이 보여준다. candidate_moments는 그 중 가치 있는 일부만 추린 것.)\n"
            + joined
        )
    return (
        "\n\n[현재 회차 전체 흐름 (segments) — 청크별 시간순]\n"
        "(시간은 원본 영상 절대 시간 — 위 [클립 시퀀스]의 '원본 X.X~Y.Y초'와 같은 축. "
        "선정된 클립은 이 전체 흐름의 일부다. 클립 사이의 행간을 메우는 cue를 작성할 때 "
        "이 정보로 *그 사이에 무엇이 있었는지* 파악하라.)\n"
        + joined
    )


def _sample_chunk_meta() -> list[dict]:
    return [
        {
            "chunk_index": 0,
            "summary": "첫 청크 요약 — 짧은 텍스트",
            "segments": [
                {"start_sec": 0.0, "end_sec": 12.5, "description": "도입부 짧은 설명"},
                {"start_sec": 12.5, "end_sec": 45.0, "description": "두 번째 segment 설명"},
            ],
        },
        {
            "chunk_index": 1,
            "summary": "두 번째 청크 요약",
            "segments": [
                {
                    "start_sec": 600.0,
                    "end_sec": 723.4,
                    # 100자 초과 — "…" 자르기 동작 검증
                    "description": "ㄱ" * 150,
                },
            ],
        },
    ]


def test_format_segments_summary_story_matches_inline():
    meta = _sample_chunk_meta()
    assert _format_segments_summary(meta, use_case="story") == _inline_segments(meta, "story")


def test_format_segments_summary_tts_matches_inline():
    meta = _sample_chunk_meta()
    assert _format_segments_summary(meta, use_case="tts") == _inline_segments(meta, "tts")


def test_format_segments_summary_empty_returns_empty_string():
    assert _format_segments_summary(None) == ""
    assert _format_segments_summary([]) == ""


def test_format_segments_summary_braces_are_doubled_for_format():
    # str.format() 안전성: { 와 } 가 들어와도 {{ }} 로 이스케이프 — 인라인 동작과 같아야 함
    meta = [
        {
            "chunk_index": 0,
            "summary": "ok",
            "segments": [{"start_sec": 0.0, "end_sec": 1.0, "description": "with {braces}"}],
        }
    ]
    out = _format_segments_summary(meta, use_case="story")
    # 원본은 .replace("{", "{{").replace("}", "}}") 적용
    assert "{{braces}}" in out
    assert "{braces}" not in out.replace("{{braces}}", "")
