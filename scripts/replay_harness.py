"""V3-M0 리플레이 하네스 CLI — 저장 아카이브의 구간 분포 지표 산출·비교.

LLM 재실행 없음(비용 0 · 결정적). 로직은 app/replay/* — 이 파일은 배선만.

  # 1) DB → 로컬 아카이브 스냅샷 (읽기 전용 · 번들은 8/13 이후 런에만 존재)
  python -m scripts.replay_harness fetch --out /tmp/replay_archive \\
      --env-file ~/dev/all.env [--since 2026-07-15] [--until 2026-08-25] [--bundles]

  # 2) 지표 리포트 (metrics.json + report.md — §3 재현 검증 포함)
  python -m scripts.replay_harness report --archive /tmp/replay_archive --out /tmp/replay_out \\
      [--since …] [--until …]

  # 3) 두 아카이브 대조 (기준선 ③ — 향후 v3 산출 디렉토리를 --b 로)
  python -m scripts.replay_harness diff --a /tmp/replay_out/metrics.json --b <v3_metrics|아카이브> \\
      --out /tmp/diff.md

  # 4) 골든 케이스 채점 (M0 은 인터페이스까지 — 실소재 채점은 M3)
  python -m scripts.replay_harness golden --plan <edit_plan.json|job_dir> \\
      --golden <golden_cuts.json | fh_captions류> [--gap 0.4] [--tol 0.5]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.replay import fetch as F
from app.replay import golden as G
from app.replay import loader, metrics, report


def _within(created_at: str, since: str | None, until: str | None) -> bool:
    if not created_at:
        return True          # job 디렉토리 레이아웃엔 시각이 없다 — 창 필터 미적용
    if since and created_at < since:
        return False
    if until and created_at >= until:
        return False
    return True


def _metrics_doc_for_archive(path: str, since: str | None, until: str | None) -> dict:
    arc = loader.load_archive(path)
    records = [r for r in arc["records"] if _within(r.get("created_at", ""), since, until)]
    per_run = [metrics.run_metrics(r) for r in records]
    agg = metrics.aggregate(per_run)
    per_run_index = {m["run_id"]: m for m in per_run if m.get("run_id")}
    human = metrics.human_pairs(arc["baselines"], per_run_index) if arc["baselines"] else None
    contamination = (metrics.contamination_check(arc["baselines"], per_run_index)
                     if arc["baselines"] else None)
    meta = {
        "path": str(path),
        "layout": arc["layout"],
        "window": {"since": since or "", "until": until or ""},
        "broken": arc["broken"],
    }
    return report.build_metrics_doc(meta, per_run, agg, human, contamination)


def cmd_fetch(args) -> int:
    env = dict(os.environ)
    if args.env_file:
        env.update(F.load_env_file(args.env_file))
    F.fetch_archive(
        args.out, base_url=env.get("SUPABASE_URL", ""),
        service_key=env.get("SUPABASE_SERVICE_KEY", ""),
        since=args.since, until=args.until, bundles=args.bundles)
    return 0


def cmd_report(args) -> int:
    doc = _metrics_doc_for_archive(args.archive, args.since, args.until)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(report.to_json(doc), encoding="utf-8")
    (out / "report.md").write_text(report.render_markdown(doc), encoding="utf-8")
    n_explain = sum(1 for r in doc["reference_check"] if r["verdict"] == "EXPLAIN")
    print(f"[report] 편 {doc['aggregate']['n_rows']} (집계 {doc['aggregate']['n_usable']}) → "
          f"{out}/metrics.json · report.md | §3 대조 EXPLAIN {n_explain}건")
    return 0


def _load_metrics_or_archive(path: str) -> dict:
    p = Path(path)
    if p.is_file():
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("schema") != "replay_metrics/v1":
            raise SystemExit(f"metrics 문서가 아닙니다: {path}")
        return doc
    return _metrics_doc_for_archive(path, None, None)


def cmd_diff(args) -> int:
    a = _load_metrics_or_archive(args.a)
    b = _load_metrics_or_archive(args.b)
    md = report.diff_metrics(a, b, label_a=args.label_a, label_b=args.label_b)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"[diff] → {args.out}")
    else:
        print(md)
    return 0


def cmd_golden(args) -> int:
    plan_path = Path(args.plan)
    if plan_path.is_dir():
        plan_path = plan_path / "edit_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    clips = plan.get("timeline") or []
    if not clips:
        raise SystemExit(f"edit_plan.timeline 이 비어 있습니다: {plan_path}")

    gdoc = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    if gdoc.get("schema") == G.GOLDEN_SCHEMA:
        golden = gdoc
    else:
        # fh_captions 류 자막 파일 → 근사 정답지(derived) 자동 변환
        golden = G.captions_to_segments(gdoc, gap_sec=args.gap)
        print(f"[golden] 자막 파일에서 근사 정답지 유도(derived): "
              f"{len(golden['segments'])}세그먼트 (gap {args.gap}s)")

    score = G.score_cuts(clips, golden, tol_sec=args.tol)
    print(json.dumps(score, ensure_ascii=False, sort_keys=True, indent=1))
    if score["derived_golden"]:
        print("⚠ 근사 정답지(derived=true) — 컷 리스트 원본이 아니라 자막 cue 뭉치 근사다. "
              "실소재 채점(M3)은 소스 시간 컷 리스트 정답지로 한다.", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="replay_harness", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="DB/Storage → 로컬 아카이브 스냅샷")
    p.add_argument("--out", required=True)
    p.add_argument("--env-file", help="SUPABASE_URL/KEY 를 담은 env 파일(예: ~/dev/all.env)")
    p.add_argument("--since", default="2026-07-15")
    p.add_argument("--until", default="2026-08-25")
    p.add_argument("--bundles", action="store_true",
                   help="ves-runs 번들(자막·후보·무음컷)도 내려받기 — 8/13 이후 런에만 존재")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("report", help="아카이브 → metrics.json + report.md")
    p.add_argument("--archive", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("diff", help="두 아카이브(또는 metrics.json) 대조")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--label-a", default="A")
    p.add_argument("--label-b", default="B")
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("golden", help="산출 컷 리스트 vs 정답지 채점(인터페이스)")
    p.add_argument("--plan", required=True, help="edit_plan.json 또는 job 디렉토리")
    p.add_argument("--golden", required=True,
                   help="golden_cuts/v1 정답지 또는 fh_captions류 자막 파일")
    p.add_argument("--gap", type=float, default=G.DEFAULT_GAP_SEC)
    p.add_argument("--tol", type=float, default=G.DEFAULT_BOUNDARY_TOL)
    p.set_defaults(fn=cmd_golden)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
