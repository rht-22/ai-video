from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def assign_sequence_ids(
    candidates: list[dict],
    edges: list[dict] | None = None,
) -> list[dict]:
    """continues_from 체인과 관계 그래프 엣지를 바탕으로 연속 장면끼리 같은 sequence_id를 부여한다.

    edges가 제공된 경우:
    - 같은 chunk 내 continues_from만 신뢰하고 cross-chunk continues_from은 무시한다.
    - edges의 continuous 타입 엣지로 cross-chunk 연속성을 보정한다.
    edges가 없으면 기존 로직(cross-chunk 글로벌 폴백 포함)을 그대로 사용한다.
    """
    n = len(candidates)
    if n == 0:
        return candidates

    # (chunk_index, candidate_index) → 목록 내 위치
    idx_map: dict[tuple[int, int], int] = {}
    for i, m in enumerate(candidates):
        key = (m.get("chunk_index", 0), m.get("candidate_index", i))
        idx_map[key] = i

    # Union-Find
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i, m in enumerate(candidates):
        cf = m.get("continues_from")
        if not isinstance(cf, dict):
            continue
        pred_cand_idx = cf.get("candidate_index")
        if pred_cand_idx is None:
            continue

        own_chunk = m.get("chunk_index", 0)
        cf_chunk = cf.get("chunk_index", 0)

        # edges가 있으면 cross-chunk continues_from은 신뢰하지 않음
        if edges is not None and cf_chunk != own_chunk:
            continue

        # 같은 chunk 내 탐색 우선
        pred_i = idx_map.get((own_chunk, pred_cand_idx))
        if pred_i is None and edges is None:
            # edges 없을 때만 글로벌 chunk_index 폴백
            pred_i = idx_map.get((cf_chunk, pred_cand_idx))
        if pred_i is not None and pred_i != i:
            union(i, pred_i)

    # 관계 그래프의 continuous 엣지로 cross-chunk 연속성 보정
    if edges:
        for edge in edges:
            if edge.get("type") != "continuous":
                continue
            f_ref = edge.get("from", {})
            t_ref = edge.get("to", {})
            f_i = idx_map.get((f_ref.get("chunk_index", 0), f_ref.get("candidate_index", 0)))
            t_i = idx_map.get((t_ref.get("chunk_index", 0), t_ref.get("candidate_index", 0)))
            if f_i is not None and t_i is not None and f_i != t_i:
                union(f_i, t_i)

    # 루트별 멤버 수집 후 earliest start_sec 기준으로 sequence_id 번호 부여
    roots: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        roots.setdefault(r, []).append(i)

    root_min_start = {
        r: min(candidates[i]["start_sec"] for i in members)
        for r, members in roots.items()
    }
    sorted_roots = sorted(roots, key=lambda r: root_min_start[r])
    seq_id_map = {r: sid for sid, r in enumerate(sorted_roots)}

    result = []
    for i, m in enumerate(candidates):
        m_copy = dict(m)
        m_copy["sequence_id"] = seq_id_map[find(i)]
        result.append(m_copy)
    return result


@dataclass(frozen=True)
class RankedMoment:
    start_sec: float
    end_sec: float
    importance: float
    hook_score: float
    topic_alignment_score: float
    story_role: str  # "hook" | "build" | "payoff"
    description: str
    reason: str
    transcript: str
    pacing_note: str
    points: dict
    final_score: float
    viral_titles: list[str] = field(default_factory=list)
    suggested_tts_line: str = ""


def rank_moments(moments: list[dict[str, Any]]) -> list[RankedMoment]:
    """하위 호환 래퍼 — 기본 auto 모드로 랭킹."""
    return rank_moments_enhanced(moments, shorts_type="auto")


def rank_moments_enhanced(
    moments: list[dict[str, Any]],
    shorts_type: str = "auto",  # "highlight" | "storytelling" | "auto"
) -> list[RankedMoment]:
    """Rank moments by composite importance."""
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

        importance = float(moment.get("importance", 0.0))
        hook_score = float(moment.get("hook_score", 0.0))
        topic_alignment = float(moment.get("topic_alignment_score", 0.0))

        if shorts_type == "highlight":
            final_score = (
                importance * 0.45
                + hook_score * 0.15
                + points_max * 0.40
            )
        elif shorts_type == "storytelling":
            final_score = (
                importance * 0.40
                + hook_score * 0.15
                + topic_alignment * 0.20
                + points_avg * 0.25
            )
        else:  # auto
            final_score = (
                importance * 0.40
                + hook_score * 0.15
                + topic_alignment * 0.15
                + points_max * 0.30
            )

        ranked.append(
            RankedMoment(
                start_sec=float(moment["start_sec"]),
                end_sec=float(moment["end_sec"]),
                importance=importance,
                hook_score=hook_score,
                topic_alignment_score=topic_alignment,
                story_role=moment.get("story_role", "build"),
                description=moment["description"],
                reason=moment["reason"],
                transcript=moment["transcript"],
                viral_titles=moment.get("viral_titles", []),
                suggested_tts_line=moment.get("suggested_tts_line", ""),
                pacing_note=moment.get("pacing_note", ""),
                points=moment.get("points") or {},
                final_score=final_score,
            )
        )
    return sorted(ranked, key=lambda item: item.final_score, reverse=True)
