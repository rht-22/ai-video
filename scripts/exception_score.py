"""V3-M7 — exception 채점 CLI. 발주서 orders/v3-m7-exception-scorer.md.

  python -m scripts.exception_score --labels tests/data/v3_exception_labels \\
      --jobs <job_dir> [<job_dir>...] --out <dir>

job ↔ 레이블 매칭은 run_log.input 파일명 == 레이블 source_hint. stage1.json 이
없는 job 은 건너뛰고 사유를 표에 남긴다(조용한 누락 금지).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.replay.exception_score import (
    load_labels,
    match_job_to_label,
    render_report,
    score_one,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="exception_score")
    p.add_argument("--labels", required=True)
    p.add_argument("--jobs", nargs="+", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    labels = load_labels(args.labels)
    matched: set[str] = set()
    rows = []
    for job in args.jobs:
        jd = Path(job)
        try:
            run_log = json.loads((jd / "run_log.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"[score] ⚠ run_log 없음/깨짐 — 건너뜀: {jd}")
            continue
        name = match_job_to_label(run_log, labels)
        if name is None:
            print(f"[score] ⚠ 레이블 매칭 실패(source_hint 불일치) — 건너뜀: {jd.name}")
            continue
        s1_path = jd / "stage1.json"
        if not s1_path.exists():
            rows.append({"name": name, "verdict": "no-stage1", "miss_sec": None,
                         "overreach_sec": None, "union_iou": None, "zones": []})
            continue
        stage1 = json.loads(s1_path.read_text(encoding="utf-8"))
        score = score_one(labels[name], stage1.get("exception_sector"))
        rows.append({"name": name, **score, "job": jd.name})
        matched.add(name)

    for name in sorted(set(labels) - matched):
        print(f"[score] 미채점 레이블(산출 job 없음): {name}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "score.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(render_report(rows), encoding="utf-8")
    fails = [r for r in rows if r["verdict"] == "fail"]
    print(f"[score] {len(rows)}편 채점 · fail {len(fails)} → {out}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
