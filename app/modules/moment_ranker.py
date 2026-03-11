# AI가 제안한 여러 하이라이트 후보들 중 **"어떤 것이 진짜 쇼츠로 만들기에 가장 좋은가?"**를 수학적으로 결정하는 의사결정 엔진
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RankedMoment:
    start_sec: float
    end_sec: float
    importance: float
    hook_score: float
    topic_alignment_score: float
    story_role: str
    reason: str
    subtitle: str
    tts_line: str
    final_score: float


def rank_moments(moments: list[dict[str, Any]]) -> list[RankedMoment]:
    ranked = []
    for moment in moments:
        # 가중치 기반 스코어링
        final_score = (
            moment["importance"] * 0.45              # 중요도 45%
            + moment["hook_score"] * 0.25            # 후킹 포인트 25%
            + moment["topic_alignment_score"] * 0.3  # 주제 일치도 30%
        )
        ranked.append(
            RankedMoment(
                start_sec=float(moment["start_sec"]),
                end_sec=float(moment["end_sec"]),
                importance=float(moment["importance"]),
                hook_score=float(moment["hook_score"]),
                topic_alignment_score=float(moment["topic_alignment_score"]),
                story_role=moment["story_role"],
                reason=moment["reason"],
                subtitle=moment["subtitle"],
                tts_line=moment["tts_line"],
                final_score=final_score,
            )
        )
    # 정렬 및 필터링
    return sorted(ranked, key=lambda item: item.final_score, reverse=True)
