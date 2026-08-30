"""리포트 — 집계를 세 기준선과 대조해 JSON + Markdown 으로 만든다(순수).

기준선 ①: 문제 정의 §3 실측(2026-08-24, 614편) — REFERENCE_S3 에 동결.
  재현이 어긋나면 로더·정의가 틀린 것이므로 셀 단위로 대조해 판정을 찍는다.
  ⚠ 알려진 재현 한계 둘(코드가 자동으로 사유를 붙인다):
    · 측정 후 같은 창(7/15~8/24)에 편이 더 들어왔다 — 창 상한을 measured 시점
      (8/24 06:26Z 직후)으로 좁히면 614편이 그대로 나온다(2026-08-30 실측).
    · AI↔사람 29쌍 표의 'AI 최종'은 clip_metadata.edit_plan 이 편집 재렌더로
      덮이기 **전** 상태를 봤다 — 지금은 일부가 사람 값으로 오염돼 있어
      (human.contaminated_n) 그 표는 방향 대조만 한다(문제 정의도 그렇게 못박는다).
기준선 ②: 사람 편집 분포(중앙 7.5초 · 편당 6~8개) — v3 설계 목표.
기준선 ③: 다른 아카이브(향후 v3 산출) — diff_metrics 로 대조.
"""
from __future__ import annotations

import json
from typing import Any

from app.replay import metrics as M

# 문제 정의 §3 (2026-08-24 측정 · 614편) — 이 표가 재현되면 로더·정의가 맞다.
REFERENCE_S3 = {
    "measured_at": "2026-08-24",
    "n": 614,
    "buckets": {
        "1": {"n": 190, "pct": 31, "final_mean": 2.4, "split_n": 57, "shrunk_n": 0},
        "2": {"n": 208, "pct": 34, "final_mean": 3.9, "split_n": 106, "shrunk_n": 10},
        "3": {"n": 195, "pct": 32, "final_mean": 5.0, "split_n": 95, "shrunk_n": 23},
        "4+": {"n": 21, "pct": 3, "final_mean": 5.9, "split_n": 12, "shrunk_n": 2},
    },
    "finding2": {"orig1_n": 190, "solo_n": 133, "solo_pct": 70, "solo_len_mean": 48.8},
    "finding3": {"gap_median": 100, "gap_p90": 736, "multi_chunk_pct": 51},
    "human": {"pairs_n": 29, "ai_n_mean": 3.6, "ai_len_median": 20.4,
              "hu_n_mean": 7.9, "hu_len_median": 7.5,
              "increased_n": 25, "same_n": 2, "decreased_n": 2},
}

# 기준선 ② — 사람 편집 분포(설계 목표). 문제 정의 §3 말미.
HUMAN_TARGET = {"len_median": 7.5, "per_run_min": 6, "per_run_max": 8}

# 재현 판정 관용치 — 편 수는 창 경계(측정 후 유입) 때문에 ±1% 를 허용하고,
# 평균·중앙값은 유입분의 희석을 감안해 상대 5% 를 허용한다. 넘으면 EXPLAIN.
_COUNT_TOL = 0.01
_VALUE_TOL = 0.05


def _verdict(computed, reference, tol: float) -> str:
    if computed is None or reference is None:
        return "N/A"
    if reference == 0:
        return "MATCH" if computed == 0 else "EXPLAIN"
    return "MATCH" if abs(computed - reference) / abs(reference) <= tol else "EXPLAIN"


def compare_reference(agg: dict, human: dict | None) -> list[dict]:
    """집계 vs §3 — 셀 단위 대조 행 목록. 어긋난 셀은 EXPLAIN + 사유."""
    rows: list[dict] = []

    def add(name, computed, reference, tol, note=""):
        v = _verdict(computed, reference, tol)
        rows.append({"cell": name, "computed": computed, "reference": reference,
                     "verdict": v, "note": note if v != "MATCH" else ""})

    window_note = ("측정(8/24 오후) 후 같은 창에 편이 추가됨 — 창 상한을 8/24 06:26Z 로 "
                   "좁히면 614편이 재현된다")
    add("전체 편수", agg["n_usable"], REFERENCE_S3["n"], _COUNT_TOL, window_note)
    for b, ref in REFERENCE_S3["buckets"].items():
        cur = agg["buckets"].get(b) or {}
        add(f"원안 {b} 편수", cur.get("n"), ref["n"], _COUNT_TOL, window_note)
        add(f"원안 {b} 최종평균", cur.get("final_mean"), ref["final_mean"], _VALUE_TOL,
            window_note)
        add(f"원안 {b} 쪼갬", cur.get("split_n"), ref["split_n"], _COUNT_TOL + 0.03,
            window_note)
        add(f"원안 {b} 줄어듦", cur.get("shrunk_n"), ref["shrunk_n"], _COUNT_TOL + 0.03,
            window_note)
    f2, ref2 = agg["finding2"], REFERENCE_S3["finding2"]
    add("발견2 통짜 편수", f2.get("solo_n"), ref2["solo_n"], _COUNT_TOL, window_note)
    add("발견2 통짜 평균길이", f2.get("solo_len_mean"), ref2["solo_len_mean"], _VALUE_TOL,
        window_note)
    f3, ref3 = agg["finding3"], REFERENCE_S3["finding3"]
    add("발견3 갭 중앙", f3.get("gap_median"), ref3["gap_median"], _VALUE_TOL, window_note)
    add("발견3 갭 p90", f3.get("gap_p90"), ref3["gap_p90"], _VALUE_TOL, window_note)
    add("발견3 여러청크 %", f3.get("multi_chunk_pct"), ref3["multi_chunk_pct"], _VALUE_TOL,
        window_note)

    if human and human.get("pairs_n"):
        # 사람 대조표는 **방향만** 잰다 — 문제 정의 §3 스스로 "방향만 참고"로 못박았고,
        # 당시 AI 쪽 정본(clip_metadata.edit_plan)이 이후 편집 재렌더로 덮여
        # (contamination.final_overwritten) 절대값은 측정 시점 종속이라 재현 대상이 아니다.
        note = "방향 판정 — 절대값은 재렌더 오염으로 측정 시점 종속(§3 도 '방향만 참고')"
        def add_dir(name, computed_bool):
            rows.append({"cell": name, "computed": bool(computed_bool), "reference": True,
                         "verdict": "MATCH" if computed_bool else "EXPLAIN",
                         "note": "" if computed_bool else note})
        an, hn = human.get("ai_n_mean"), human.get("hu_n_mean")
        al, hl = human.get("ai_len_median"), human.get("hu_len_median")
        add_dir("사람대조 방향: 편당 구간 수 증가", an is not None and hn is not None and hn > an)
        add_dir("사람대조 방향: 구간 길이 단축", al is not None and hl is not None and hl < al)
        add_dir("사람대조 방향: 늘린 편이 우세",
                human.get("increased_n", 0) > human.get("decreased_n", 0))
        add_dir("사람대조 방향: 총 길이 순증",
                (human.get("hu_total") or 0) > (human.get("ai_total") or 0))
    return rows


def build_metrics_doc(archive_meta: dict, per_run: list[dict], agg: dict,
                      human: dict | None, contamination: dict | None = None) -> dict:
    """metrics.json 본문 — 정렬·반올림이 끝난 결정적 산출."""
    doc = {
        "schema": "replay_metrics/v1",
        "archive": archive_meta,
        "aggregate": agg,
        "human": human or {},
        "contamination": contamination or {},
        "reference_check": compare_reference(agg, human),
        "human_target": HUMAN_TARGET,
        "per_run": sorted(per_run, key=lambda m: str(m.get("key", ""))),
    }
    return doc


def to_json(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def render_markdown(doc: dict) -> str:
    """사람이 읽는 리포트. metrics.json 과 같은 숫자만 쓴다(이중 계산 금지)."""
    agg = doc["aggregate"]
    human = doc.get("human") or {}
    meta = doc.get("archive") or {}
    L: list[str] = []
    L.append("# 리플레이 하네스 리포트 (V3-M0)")
    L.append("")
    L.append(f"- 아카이브: `{meta.get('path', '?')}` (레이아웃 {meta.get('layout', '?')})")
    if meta.get("window"):
        L.append(f"- 창: {meta['window']}")
    L.append(f"- 편수: {agg['n_rows']} (집계 가능 {agg['n_usable']} · 제외 {agg['n_skipped']}"
             f" · 사람 손 탄 편 {agg['n_human_edited']})")
    if agg["n_skipped"]:
        L.append(f"  - 제외 사유: 원안/최종 클립이 빈 편 — {', '.join(agg['skipped_keys'][:10])}"
                 + (" …" if agg["n_skipped"] > 10 else ""))
    if meta.get("broken"):
        L.append(f"- ⚠ 읽기 실패 {len(meta['broken'])}건: " + "; ".join(meta["broken"][:5]))
    cont = doc.get("contamination") or {}
    if cont.get("checked"):
        L.append(f"- ⚠ '최종' 오염 진단: 기준선 대조 가능 {cont['checked']}편 중 "
                 f"{cont['final_overwritten']}편의 edit_plan 이 편집 재렌더로 덮임 — "
                 "최종 쪽 수치는 측정 시점에 따라 이만큼 흔들린다")
    L.append("")

    L.append("## 원안 구간 수 분포 (§3 재현 표)")
    L.append("")
    L.append("| 원안 구간 수 | 편수 | % | 최종 구간 수(평균) | 무음컷이 쪼갬 | 오히려 줄어듦 |")
    L.append("|---|---|---|---|---|---|")
    for b in ("1", "2", "3", "4+"):
        c = agg["buckets"][b]
        L.append(f"| {b}개 | {_fmt(c['n'])} | {_fmt(c['pct'])}% | {_fmt(c['final_mean'])} "
                 f"| {_fmt(c['split_n'])} | {_fmt(c['shrunk_n'])} |")
    L.append("")

    f2, f3, ln = agg["finding2"], agg["finding3"], agg["lengths"]
    L.append("## 주요 수치")
    L.append("")
    L.append(f"- 원안 1구간 {_fmt(f2['orig1_n'])}편 중 끝까지 통짜 {_fmt(f2['solo_n'])}편 "
             f"({_fmt(f2['solo_pct'])}%) · 통짜 평균 {_fmt(f2['solo_len_mean'])}초")
    L.append(f"- 원안 인접 갭 중앙 {_fmt(f3['gap_median'])}초 · p90 {_fmt(f3['gap_p90'])}초 "
             f"· 여러 청크 스팬 {_fmt(f3['multi_chunk_n'])}/{_fmt(f3['multi_run_n'])}편 "
             f"({_fmt(f3['multi_chunk_pct'])}%)")
    L.append(f"- 구간 길이 중앙(원안 {_fmt(ln['orig_len_median'])}s · 최종 "
             f"{_fmt(ln['final_len_median'])}s) · p90(원안 {_fmt(ln['orig_len_p90'])}s · "
             f"최종 {_fmt(ln['final_len_p90'])}s)")
    L.append(f"- 편당 구간 수: 원안 평균 {_fmt(ln['orig_n_mean'])} · 최종 평균 "
             f"{_fmt(ln['final_n_mean'])} (중앙 {_fmt(ln['final_n_median'])}) · "
             f"편 길이 중앙 {_fmt(ln['final_total_median'])}s")
    ext = agg["ext"]
    L.append(f"- 맥락 확장: 요청 {_fmt(ext['requested_runs'])}편/{_fmt(ext['requested_clips'])}클립"
             + (f" · (번들 {_fmt(ext['tierb_runs'])}편) 제안 {_fmt(ext['proposed_sec_total'])}s "
                f"→ 실적용 {_fmt(ext['applied_sec_total'])}s" if ext["tierb_runs"] else
                " · 제안/실적용 초 단위는 번들(checkpoint_gemini) 필요 — 이 아카이브엔 없음"))
    sc = agg["sentence_cut"]
    if sc["coverage_n"]:
        L.append(f"- 문장 절단(번들 {_fmt(sc['coverage_n'])}편): 마지막 cue 종결부호 "
                 f"{_fmt(sc['last_cue_final_n'])}편 ({_fmt(sc['last_cue_final_pct'])}%) · "
                 f"구간 경계 절단 {_fmt(sc['boundary_cut_total'])}/{_fmt(sc['boundary_total'])}")
    else:
        L.append("- 문장 절단: 번들(subtitle_segments) 없는 아카이브 — 측정 불가(커버리지 0)")
    L.append("")

    if human and human.get("pairs_n"):
        L.append("## AI 최종 → 사람 편집 (기준선 ②)")
        L.append("")
        L.append("| 항목 | AI | 사람 |")
        L.append("|---|---|---|")
        L.append(f"| 편당 구간 수 | {_fmt(human['ai_n_mean'])} | {_fmt(human['hu_n_mean'])} |")
        L.append(f"| 구간 길이 중앙 | {_fmt(human['ai_len_median'])}초 "
                 f"| {_fmt(human['hu_len_median'])}초 |")
        L.append(f"| 총 길이 | {_fmt(human['ai_total'])}초 | {_fmt(human['hu_total'])}초 |")
        L.append(f"- 쌍 {_fmt(human['pairs_n'])}편: 사람이 늘림 {_fmt(human['increased_n'])} · "
                 f"같음 {_fmt(human['same_n'])} · 줄임 {_fmt(human['decreased_n'])}"
                 f" · ⚠ AI쪽 오염(재렌더로 덮임) {_fmt(human['contaminated_n'])}쌍")
        tgt = doc.get("human_target") or HUMAN_TARGET
        L.append(f"- 설계 목표(사람 분포): 구간 길이 중앙 ~{tgt['len_median']}초 · "
                 f"편당 {tgt['per_run_min']}~{tgt['per_run_max']}개")
        L.append("")

    L.append("## §3 재현 검증 (기준선 ①)")
    L.append("")
    L.append("| 셀 | 계산값 | §3 값 | 판정 | 사유 |")
    L.append("|---|---|---|---|---|")
    for r in doc["reference_check"]:
        L.append(f"| {r['cell']} | {_fmt(r['computed'])} | {_fmt(r['reference'])} "
                 f"| {r['verdict']} | {r['note']} |")
    n_explain = sum(1 for r in doc["reference_check"] if r["verdict"] == "EXPLAIN")
    L.append("")
    L.append(f"MATCH {sum(1 for r in doc['reference_check'] if r['verdict'] == 'MATCH')} · "
             f"EXPLAIN {n_explain} · N/A "
             f"{sum(1 for r in doc['reference_check'] if r['verdict'] == 'N/A')}")
    L.append("")
    return "\n".join(L) + "\n"


# ── 기준선 ③ — 두 아카이브(예: 현행 vs v3) diff ─────────────────────────────

_DIFF_KEYS = [
    ("편수(집계 가능)", ("aggregate", "n_usable")),
    ("편당 최종 구간 평균", ("aggregate", "lengths", "final_n_mean")),
    ("편당 최종 구간 중앙", ("aggregate", "lengths", "final_n_median")),
    ("최종 구간 길이 중앙", ("aggregate", "lengths", "final_len_median")),
    ("최종 구간 길이 p90", ("aggregate", "lengths", "final_len_p90")),
    ("편 길이 중앙", ("aggregate", "lengths", "final_total_median")),
    ("원안 구간 길이 중앙", ("aggregate", "lengths", "orig_len_median")),
    ("통짜 편수(원안1→최종1)", ("aggregate", "finding2", "solo_n")),
    ("문장절단 커버리지", ("aggregate", "sentence_cut", "coverage_n")),
    ("마지막 cue 종결 %", ("aggregate", "sentence_cut", "last_cue_final_pct")),
]


def _dig(doc: dict, path: tuple) -> Any:
    cur: Any = doc
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def diff_metrics(a: dict, b: dict, label_a: str = "A", label_b: str = "B") -> str:
    """두 metrics 문서의 핵심 집계 대조 Markdown. v3 합격 판정의 입구.

    사람 분포 목표(HUMAN_TARGET) 열을 함께 찍는다 — v3 가 어디까지 왔는지가
    diff 한 장에서 보여야 한다."""
    L = [f"# 아카이브 대조: {label_a} vs {label_b}", ""]
    L.append(f"| 지표 | {label_a} | {label_b} | Δ |")
    L.append("|---|---|---|---|")
    for name, path in _DIFF_KEYS:
        va, vb = _dig(a, path), _dig(b, path)
        delta = (round(vb - va, 3)
                 if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else None)
        L.append(f"| {name} | {_fmt(va)} | {_fmt(vb)} | {_fmt(delta)} |")
    L.append("")
    L.append("## 원안 구간 수 분포")
    L.append("")
    L.append(f"| 버킷 | {label_a} 편수 | {label_b} 편수 | {label_a} 최종평균 | {label_b} 최종평균 |")
    L.append("|---|---|---|---|---|")
    for bkt in ("1", "2", "3", "4+"):
        ca = _dig(a, ("aggregate", "buckets", bkt)) or {}
        cb = _dig(b, ("aggregate", "buckets", bkt)) or {}
        L.append(f"| {bkt} | {_fmt(ca.get('n'))} | {_fmt(cb.get('n'))} "
                 f"| {_fmt(ca.get('final_mean'))} | {_fmt(cb.get('final_mean'))} |")
    tgt = HUMAN_TARGET
    L.append("")
    L.append(f"설계 목표(사람 분포): 구간 길이 중앙 ~{tgt['len_median']}초 · "
             f"편당 {tgt['per_run_min']}~{tgt['per_run_max']}개")
    L.append("")
    return "\n".join(L) + "\n"
