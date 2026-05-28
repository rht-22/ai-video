"""파이프라인 결과 폴더에서 sequence_id를 사후 재구성해 출력한다.

사용법:
    python scripts/print_sequence_ids.py <output_dir>

필요 파일:
    <output_dir>/checkpoint_gemini.json
    <output_dir>/checkpoint_graph.json (선택 — 없으면 edges 없이 계산)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 프로젝트 루트를 import path에 추가
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.modules.moment_ranker import assign_sequence_ids


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1

    out = Path(sys.argv[1])
    if not out.exists():
        print(f"[ERROR] 폴더를 찾을 수 없습니다: {out}")
        return 1

    gemini_path = out / "checkpoint_gemini.json"
    graph_path = out / "checkpoint_graph.json"

    if not gemini_path.exists():
        print(f"[ERROR] {gemini_path.name} 없음 — 파이프라인 step 8까지 완료해야 합니다")
        return 1

    candidates = json.loads(gemini_path.read_text(encoding="utf-8"))["all_candidates"]

    edges = None
    if graph_path.exists():
        edges = json.loads(graph_path.read_text(encoding="utf-8")).get("edges", [])
        print(f"[INFO] {len(edges)}개 관계 엣지 로드")
    else:
        print(f"[WARN] {graph_path.name} 없음 — edges 없이 계산 (cross-chunk 폴백 동작)")

    assigned = assign_sequence_ids(candidates, edges=edges)

    # sequence_id, start_sec 순으로 정렬
    assigned.sort(key=lambda c: (c["sequence_id"], c["start_sec"]))

    print(f"\n총 {len(assigned)}개 후보, "
          f"{len({c['sequence_id'] for c in assigned})}개 sequence")
    print("=" * 100)

    current_seq = -1
    for c in assigned:
        seq = c["sequence_id"]
        if seq != current_seq:
            print(f"\n── sequence {seq} ──")
            current_seq = seq
        chunk = c.get("chunk_index", "?")
        cand = c.get("candidate_index", "?")
        start = c.get("start_sec", 0.0)
        end = c.get("end_sec", 0.0)
        desc = (c.get("description") or "").replace("\n", " ")[:70]
        print(f"  chunk={chunk:>2} cand={cand:>2} | {start:>6.1f}~{end:>6.1f}s | {desc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
