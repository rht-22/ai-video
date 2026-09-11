"""권리사 활용 불가 구간 드라이런 — 회차 잡의 Stage 2 기록에 템플릿 `banned` 를 태워 본다 (2026-09-10).

    python -m scripts.banned_dryrun --job outputs/지금불륜이문제가아닙니다_fb76f307 --design-preset jigeum --episode 2
    python -m scripts.banned_dryrun --job <job> --banned-json '{"2": [{"t0": "22:06", "t1": "22:50", "what": "호텔"}]}' --episode 2

LLM 0콜 · 결정적. 항목마다 직접 겹침·증인·확대 사건 단위와 차단 구간을 찍고, 키워드가 어디에도 없는 항목
(시간축 불일치 의심 → 이웃 전부 차단)을 ⚠ 로 보여준다. 가이드 어휘(「호텔」)와 Stage 2 어휘(「침실·침대」)가
다른 경우가 실제로 있어(EP02), keywords 를 기록 어휘로 맞춰 **경고 0** 을 만든 뒤 실행하는 것이 절차다.
edit_plan.json 이 있으면 그 완성본 클립을 벨트에 대조해 위반 수를 함께 찍는다(옛 편 폐기 판정).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", required=True, help="stage2.json·grid.json 이 있는 잡 디렉토리")
    ap.add_argument("--design-preset", default=None)
    ap.add_argument("--banned-json", default=None)
    ap.add_argument("--episode", type=int, required=True)
    ap.add_argument("--pad", type=float, default=None, help="증인 탐색 이웃 창(초, 기본 BANNED_SEARCH_PAD_SEC)")
    a = ap.parse_args(argv)

    from app.v3 import banned as bn
    from app.v3.cli import load_design_preset
    from app.v3.story import build_span_index
    from app.v3.story_flow.common import meaning_rows

    job = Path(a.job)
    stage2 = json.loads((job / "stage2.json").read_text(encoding="utf-8"))
    grid = json.loads((job / "grid.json").read_text(encoding="utf-8"))
    docs = []
    if a.design_preset:
        docs.append(load_design_preset(a.design_preset)["banned"])
    if a.banned_json:
        docs.append(bn.parse_banned_doc(json.loads(a.banned_json)))
    items = bn.banned_for_episode(bn.merge_banned(*docs), a.episode)
    if not items:
        print(f"회차 {a.episode}: 금지 항목 없음")
        return 0
    rows = meaning_rows(stage2)
    span_index, _ = build_span_index(stage2, grid)
    runtime = grid.get("duration_sec") or (grid.get("source") or {}).get("duration_sec")
    kw = {"span_index": span_index}
    if a.pad is not None:
        kw["pad"] = a.pad
    res = bn.resolve_banned(items, rows, grid.get("span_candidates") or [], runtime, **kw)
    by_idx = {r["idx"]: r for r in rows}

    def _t(x: float) -> str:
        return f"{int(x // 60):02d}:{x % 60:05.2f}"

    print(f"회차 {a.episode} · 사건 단위 {len(rows)}개 · 금지 항목 {len(items)}건")
    for it in res["items"]:
        a0, z0 = it["interval"]
        print(f"\n[{_t(it['t0'])}~{_t(it['t1'])}] {it['what'] or '/'.join(it['keywords'])}")
        print(f"   keywords {it['keywords']} → 차단 [{_t(a0)}~{_t(z0)}]")
        for tag, ids in (("직접", it["direct"]), ("증인", it["witness"]), ("확대⚠", it["widened"])):
            for k in ids:
                r = by_idx.get(k)
                if r:
                    print(f"   {tag:<4} m{k:03d} [{_t(r['t0'])}~{_t(r['t1'])}] {r['content'][:70]}")
    for w in res["warnings"]:
        print(f"\n⚠ {w}")
    total = sum(z - a0 for a0, z in res["intervals"])
    raw = sum((it["t1"] if it["t1"] is not None else (runtime or 0)) - it["t0"] for it in items)
    if runtime:
        print(f"\n원문 금지 {raw:.0f}s → 차단 {total:.0f}s / 러닝타임 {runtime:.0f}s ({100 * total / runtime:.0f}%) · "
              f"사건 단위 {len(res['units'])} · span {len(res['span_ids'])} · 경고 {len(res['warnings'])}")
    ep = job / "edit_plan.json"
    if ep.exists():
        plan = json.loads(ep.read_text(encoding="utf-8"))
        bad = bn.violations(plan.get("timeline") or [], res)
        print(f"이 잡의 edit_plan: 클립 {len(plan.get('timeline') or [])}개 중 위반 {len(bad)}건"
              + (" → 이 편은 폐기 대상" if bad else " → 통과"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
