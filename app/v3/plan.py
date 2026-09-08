"""plan 단계 — 회차당 N편 기획 (4단계 4-1, 2026-09-08).

회차 지도(episode_map.json) **위에서** 편 N개를 계획한다 — 지도 없는 plan 은 실측대로 같은 사건을
N번 고른다(같은 회차 6회 중 4회가 같은 주제). 편마다 기존 5걸음(topic 은 plan 항목으로 대체)을
돌리는 것은 story_flow 의 `topic_override` 몫이다.

- 입력: 지도(레지스터·사실 장부) + meaning 표(📄 글자·⚠ 연출 층위) + 무대사 구간 + N + 이미 만든
  편의 core_meanings(`--exclude-*` 와 같은 재료).
- 검증기(순수): id 존재 · 편 사이 meaning 중복률 ≤ DUP_MAX_RATIO(코드가 잰다) · contrast/irony 는
  setup·payoff 필수이고 t(setup) < t(payoff), 레지스터 항목에 대응해야 한다(밖이면 note) ·
  시간 거리 규칙은 contrast/irony 에만(event 는 붙어 있어도 된다 — v9 가 최고작).
- 산출 `checkpoint_plan.json` = {schema, fingerprint, n, shorts[]}. run_log `plan` 단계.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from app.v3.story_flow.common import (MAX_REASKS, call_json, fmt_t, meaning_rows, meaning_table,
                                      reject_block)
from app.v3.story_flow.select import (TOPIC_KINDS, excluded_meaning_ids, exclude_block,
                                      parse_meaning_id, silent_block, silent_runs)
from app.v3 import episode_map as em

SCHEMA_PLAN = "v3_plan/v1"
DUP_MAX_RATIO = 0.20            # 편 사이 meaning 중복(작은 쪽 기준) 상한
CONTRAST_MIN_GAP_SEC = 60.0     # contrast/irony 의 setup↔payoff 최소 시간 거리(붙어 있으면 대비가 아니다)
MAX_N = 8

PROMPT = """당신은 리캡 쇼츠 기획자다. 아래는 한 회차의 구조 기록과 **회차 지도**(드라마가 심어 둔 설정↔회수·사실 장부)다. 영상은 볼 수 없다 — 기록이 정본이다.

## 과제 — 이 회차에서 쇼츠 {n}편을 계획하라
편마다 종류를 정한다:
- event: 사건 하나(시작~결과가 한 편에 닫힌다). 대사가 촘촘한 구간이 유리하고, 붙어 있는 사건도 된다.
- contrast: 설정과 회수를 **나란히** 보여주는 편 — 선언(단언·약속·조건) → 경과 → 반전. setup·payoff 는 레지스터의 항목이어야 하고 시간이 떨어져 있어야 한다.
- irony: 인물이 믿는 것과 시청자가 아는 사실이 어긋나는 편(믿음 장부 ↔ 사실 장부) — setup(믿음이 생기는 장면) · payoff(사실이 드러나는 장면).
자기 검증: 편 사이 사건 단위 중복 ≤ {dup_pct}% · 회차 전 구간에 분산 · 회차의 주요 사건(importance 5)을 빠뜨리지 않는다 · 각 편이 작품을 모르는 사람에게 **혼자 서는가**(why_standalone). 무대사 구간·화면 글자(📄)만으로 닫히는 편도 된다(내레이션이 뼈대).

## 작품
{work_title}{research_block}
{register_block}{facts_block}
## 사건 단위 (id | 시각 | 길이 | importance | 분위기 | 인물 | 내용 · 📄 화면 글자 · ⚠ 연출 층위)
{meaning_block}
{silent_block}{exclude_block}{reject_block}
## 출력 (JSON 만)
{{"shorts": [
  {{"no": 1, "kind": "event", "topic": "한 문장", "core_meanings": ["m012", "m013"], "purpose": "이 편이 하는 일", "why_standalone": "혼자 서는 이유"}},
  {{"no": 2, "kind": "contrast", "topic": "…", "core_meanings": ["m020", "m037"], "setup": "m020", "payoff": "m037", "purpose": "…", "why_standalone": "…"}}
]}}"""


def _overlap_ratio(a: list[int], b: list[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def validate_plan(resp: Any, rows: list[dict], *, n: int, register: list[dict] | None = None,
                  excluded: set[int] | None = None,
                  dup_max: float = DUP_MAX_RATIO) -> tuple[list[dict] | None, list[str], list[str]]:
    """편 목록 정규화. id 부재·kind 밖·중복률 초과·contrast 시간 규칙은 반려, 레지스터 밖 쌍은 note."""
    if not isinstance(resp, dict) or not isinstance(resp.get("shorts"), list):
        return None, ["shorts 배열이 없다"], []
    problems: list[str] = []
    notes: list[str] = []
    by_idx = {r["idx"]: r for r in rows}
    reg_pairs = {(r["setup"]["meaning"], (r.get("payoff") or {}).get("meaning"))
                 for r in (register or []) if r.get("setup")}
    out: list[dict] = []
    for k, it in enumerate(resp["shorts"]):
        if not isinstance(it, dict):
            problems.append(f"shorts[{k}] 가 객체가 아님")
            continue
        kind = str(it.get("kind") or "event").strip()
        if kind not in TOPIC_KINDS:
            problems.append(f"shorts[{k}] kind {kind!r} — {'/'.join(TOPIC_KINDS)} 중 하나")
            continue
        topic = str(it.get("topic") or "").strip()
        if not topic:
            problems.append(f"shorts[{k}] topic 이 비었다")
            continue
        core: list[int] = []
        bad: list[str] = []
        for v in it.get("core_meanings") or []:
            m = parse_meaning_id(v)
            if m is None or m not in by_idx:
                bad.append(str(v))
            elif m not in core:
                core.append(m)
        if bad:
            problems.append(f"shorts[{k}] 모르는 사건 단위 {bad[:3]}")
            continue
        setup = parse_meaning_id(it.get("setup")) if it.get("setup") is not None else None
        payoff = parse_meaning_id(it.get("payoff")) if it.get("payoff") is not None else None
        if kind in ("contrast", "irony"):
            if setup is None or setup not in by_idx or payoff is None or payoff not in by_idx:
                problems.append(f"shorts[{k}] {kind} 는 setup·payoff 사건 단위가 둘 다 필요하다")
                continue
            if not by_idx[setup]["t0"] < by_idx[payoff]["t0"]:
                problems.append(f"shorts[{k}] setup(m{setup:03d}) 이 payoff(m{payoff:03d}) 보다 앞이어야 한다")
                continue
            if by_idx[payoff]["t0"] - by_idx[setup]["t1"] < CONTRAST_MIN_GAP_SEC:
                problems.append(f"shorts[{k}] {kind} 의 setup↔payoff 가 {CONTRAST_MIN_GAP_SEC:.0f}초 이상 떨어져야 한다"
                                f"(붙어 있으면 event 로 내라)")
                continue
            if (setup, payoff) not in reg_pairs:
                notes.append(f"shorts[{k}] setup↔payoff(m{setup:03d}→m{payoff:03d}) 가 레지스터 밖 쌍이다 — 검수 대상")
            for m in (setup, payoff):
                if m not in core:
                    core.append(m)
        elif setup is not None or payoff is not None:
            notes.append(f"shorts[{k}] event 의 setup/payoff 는 무시")
            setup = payoff = None
        if not core:
            problems.append(f"shorts[{k}] core_meanings 가 비었다")
            continue
        core.sort()
        hit = [m for m in core if excluded and m in excluded]
        if hit:
            problems.append(f"shorts[{k}] 이미 만든 편의 사건 단위({'/'.join(f'm{m:03d}' for m in hit)}) — 다른 사건을 골라라")
            continue
        out.append({"no": len(out) + 1, "kind": kind, "topic": topic, "core_meanings": core,
                    **({"setup": setup, "payoff": payoff} if setup is not None else {}),
                    "purpose": str(it.get("purpose") or "").strip()[:120],
                    "why_standalone": str(it.get("why_standalone") or "").strip()[:160],
                    "t0": round(min(by_idx[m]["t0"] for m in core), 3),
                    "t1": round(max(by_idx[m]["t1"] for m in core), 3)})
    if not out:
        return None, problems + ["shorts 가 비었다"], notes
    for i in range(len(out)):
        for j in range(i + 1, len(out)):
            r = _overlap_ratio(out[i]["core_meanings"], out[j]["core_meanings"])
            if r > dup_max + 1e-9:
                problems.append(f"편 {out[i]['no']} 와 편 {out[j]['no']} 의 사건 단위 중복 {r:.0%} > {dup_max:.0%} — 다른 사건으로")
    if len(out) < n:
        notes.append(f"편 {len(out)}개 — 요청 {n}개보다 적다(재료가 얇으면 그대로 간다)")
    if len(out) > n:
        notes.append(f"편 {len(out)}개 → 앞 {n}개만(요청 N)")
        out = out[:n]
        for i, it in enumerate(out):
            it["no"] = i + 1
    # 분산 진단(note — 반려 아님): 편들의 중심 시각
    if len(out) >= 2:
        span = max(r["t1"] for r in rows) - min(r["t0"] for r in rows)
        centers = sorted((it["t0"] + it["t1"]) / 2 for it in out)
        gaps = [b - a for a, b in zip(centers, centers[1:])]
        if span > 0 and min(gaps) < span * 0.05:
            notes.append(f"편 중심 시각이 몰려 있다(최소 간격 {min(gaps):.0f}s / 회차 {span:.0f}s) — 검수 참고")
    if problems:
        return None, problems, notes
    return out, [], notes


def plan_fingerprint(map_doc: dict | None, stage2_doc: dict, n: int, excluded: set[int]) -> str:
    return hashlib.sha1(json.dumps(
        {"map": (map_doc or {}).get("fingerprint"), "stage2": em.fingerprint_of(stage2_doc),
         "n": n, "excluded": sorted(excluded)}, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def run_plan(gemini, stage2_doc: dict, grid: dict, *, n: int, map_doc: dict | None,
             work_title: str, research_context: str = "",
             exclude_topics: tuple[str, ...] = (), exclude_ranges=(),
             call: Callable[[Any, str], dict] | None = None, log=print) -> tuple[dict, dict]:
    if map_doc is None:
        raise ValueError("plan 단계는 회차 지도(episode_map.json)가 필요하다 — --episode-map 을 먼저(지도 없는 plan 은 같은 사건을 N번 고른다)")
    if not 1 <= int(n) <= MAX_N:
        raise ValueError(f"--plan-shorts 는 1~{MAX_N}: {n}")
    from app.v3.story_flow import _loop
    import app.v3.story_flow as _sf
    rows = meaning_rows(stage2_doc)
    if not rows:
        raise ValueError("분석된 사건 단위가 없다")
    _dur = grid.get("duration_sec") or (grid.get("source") or {}).get("duration_sec")
    silent = silent_runs(grid.get("words") or [], _dur)
    excluded = excluded_meaning_ids(rows, exclude_ranges)
    ex_blk = exclude_block(exclude_topics, excluded, rows)
    rc = (research_context or "").strip()
    research_block = f"\n\n### 작품 정보(리서치)\n{rc[:3000]}" if rc else ""
    audit: dict[str, Any] = {"n": n, "meanings": len(rows), "excluded": sorted(excluded)}
    _call = call or call_json
    _orig = _sf.call_json
    _sf.call_json = lambda g, p: _call(gemini, p)      # noqa: E731
    try:
        shorts = _loop("plan", lambda rej: PROMPT.format(
            n=n, dup_pct=int(DUP_MAX_RATIO * 100), work_title=work_title, research_block=research_block,
            register_block=em.register_block(map_doc), facts_block=em.facts_block(map_doc),
            meaning_block=meaning_table(rows), silent_block=silent_block(silent, rows),
            exclude_block=ex_blk, reject_block=rej),
            lambda r: validate_plan(r, rows, n=n, register=map_doc.get("register"), excluded=excluded),
            gemini, audit, log)
    finally:
        _sf.call_json = _orig
    audit["calls"] = len(audit["plan"])
    doc = {"schema": SCHEMA_PLAN, "fingerprint": plan_fingerprint(map_doc, stage2_doc, n, excluded),
           "n": n, "shorts": shorts}
    for it in shorts:
        log(f"  [v3/plan] 편 {it['no']} {it['kind']} {fmt_t(it['t0'])}~{fmt_t(it['t1'])} — {it['topic'][:50]}"
            f" (핵심 {'/'.join(f'm{m:03d}' for m in it['core_meanings'])})")
    return doc, audit


def plan_item_as_topic(item: dict) -> dict:
    """plan 항목 → story_flow `topic_override` (validate_topic 이 받는 모양)."""
    out = {"topic": item["topic"], "why": item.get("why_standalone") or item.get("purpose") or "",
           "core_meanings": [f"m{m:03d}" for m in item["core_meanings"]], "kind": item.get("kind") or "event"}
    if item.get("setup") is not None:
        out["setup"] = f"m{item['setup']:03d}"
        out["payoff"] = f"m{item['payoff']:03d}"
    return out


__all__ = ["SCHEMA_PLAN", "validate_plan", "run_plan", "plan_item_as_topic", "plan_fingerprint",
           "DUP_MAX_RATIO", "CONTRAST_MIN_GAP_SEC"]
