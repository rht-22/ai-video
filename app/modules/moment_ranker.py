from __future__ import annotations


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
