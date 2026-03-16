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
        final_score = (
            moment["importance"] * 0.45
            + moment["hook_score"] * 0.25
            + moment["topic_alignment_score"] * 0.3
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
    return sorted(ranked, key=lambda item: item.final_score, reverse=True)
