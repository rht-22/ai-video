from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RankedMoment:
    start_sec: float
    end_sec: float
    importance: float
    hook_score: float
    topic_alignment_score: float
    scroll_stop_power: float
    emotional_intensity: float
    rewatch_value: float
    shareability: float
    standalone_value: float
    narrative_core_score: float
    story_role: str  # "hook" | "build" | "payoff"
    description: str
    reason: str
    transcript: str
    viral_titles: list[str]
    suggested_tts_line: str
    pacing_note: str
    points: dict
    final_score: float
    viral_score: float


def rank_moments(moments: list[dict[str, Any]]) -> list[RankedMoment]:
    """하위 호환 래퍼 — 기본 auto 모드로 랭킹."""
    return rank_moments_enhanced(moments, shorts_type="auto")


def rank_moments_enhanced(
    moments: list[dict[str, Any]],
    shorts_type: str = "auto",  # "highlight" | "storytelling" | "auto"
) -> list[RankedMoment]:
    ranked = []
    for moment in moments:
        # 1. points 보너스 계산
        points = moment.get("points") or {}
        points_values = [
            float(v) for v in points.values()
            if v is not None and isinstance(v, (int, float))
        ]
        points_max = max(points_values) if points_values else 0.0
        points_avg = (
            sum(points_values) / len(points_values) if points_values else 0.0
        )

        # 2. 바이럴 서브스코어
        narrative_core = float(moment.get("narrative_core_score", 0.5))
        viral_score = (
            float(moment.get("scroll_stop_power", 0.0)) * 0.20
            + float(moment.get("emotional_intensity", 0.0)) * 0.15
            + float(moment.get("rewatch_value", 0.0)) * 0.15
            + float(moment.get("shareability", 0.0)) * 0.20
            + float(moment.get("standalone_value", 0.0)) * 0.10
            + narrative_core * 0.20
        )

        # 3. 타입별 가중치 적용
        importance = float(moment.get("importance", 0.0))
        hook_score = float(moment.get("hook_score", 0.0))
        topic_alignment = float(moment.get("topic_alignment_score", 0.0))

        if shorts_type == "highlight":
            final_score = (
                importance * 0.15
                + hook_score * 0.10
                + viral_score * 0.30
                + points_max * 0.15
                + narrative_core * 0.30
            )
        elif shorts_type == "storytelling":
            final_score = (
                importance * 0.20
                + hook_score * 0.10
                + topic_alignment * 0.15
                + viral_score * 0.25
                + points_avg * 0.10
                + narrative_core * 0.20
            )
        else:  # auto
            final_score = (
                importance * 0.15
                + hook_score * 0.10
                + topic_alignment * 0.10
                + viral_score * 0.30
                + points_max * 0.10
                + narrative_core * 0.25
            )

        ranked.append(
            RankedMoment(
                start_sec=float(moment["start_sec"]),
                end_sec=float(moment["end_sec"]),
                importance=importance,
                hook_score=hook_score,
                topic_alignment_score=topic_alignment,
                scroll_stop_power=float(moment.get("scroll_stop_power", 0.0)),
                emotional_intensity=float(moment.get("emotional_intensity", 0.0)),
                rewatch_value=float(moment.get("rewatch_value", 0.0)),
                shareability=float(moment.get("shareability", 0.0)),
                standalone_value=float(moment.get("standalone_value", 0.0)),
                narrative_core_score=narrative_core,
                story_role=moment.get("story_role", "build"),
                description=moment["description"],
                reason=moment["reason"],
                transcript=moment["transcript"],
                viral_titles=moment.get("viral_titles", []),
                suggested_tts_line=moment.get("suggested_tts_line", ""),
                pacing_note=moment.get("pacing_note", ""),
                points=moment.get("points") or {},
                final_score=final_score,
                viral_score=viral_score,
            )
        )
    return sorted(ranked, key=lambda item: item.final_score, reverse=True)
