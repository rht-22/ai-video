"""V3-M7 — exception 채점기(순수 로직). 발주서 orders/v3-m7-exception-scorer.md.

Stage 1 의 exception_sector 판정을 수동 레이블(tests/data/v3_exception_labels/,
프레임 실측)과 대조한다. 계기는 가왕쇼 6화 실사고 — 예고 시작을 50초 늦게 판정해
최종 쇼츠 엔딩이 다음화 예고로 오염됐다. validate ③은 자기 판정 기준이라 이걸
구조적으로 못 잡는다 — 외부 정답지가 필요하다.

채점 정책(발주서로 확정):
  1) 1차 지표 = 본편/비본편 **이분법** — 소비(chunk_split 물리 제거)는 키 구분 없이
     합집합만 쓴다. 키 혼동(teaser↔credit)은 2차 지표.
  2) miss(레이블 비본편 ∧ 판정 본편 — 오염) > overreach(그 반대 — 재료 손실).
  3) 관용 = zone.tol + SNAP_TOL(2.0s) — 레이블 불확실성과 스냅 격자 오차는 독립 합산.
     zone uncovered ≤ 관용이면 pass(포핸즈1 3층 구조 2.25s 오차가 pass 하는 근거).
  4) aux(credit_overlay 류)는 **중립** — miss 도 overreach 도 아니다(본편 위
     오버레이는 어느 판정도 정답일 수 있다 — 레이블 정책의 '채점 미포함' 그대로).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.v3 import schemas

SNAP_TOL_SEC = 2.0          # 기획 §3 스냅 반려 기준 — 레이블 tol 과 독립 합산
OVERREACH_WARN_SEC = 5.0    # 본편 손실 경고 임계(차단 아님 — 정책 2)

Interval = tuple[float, float]


def _zone_intervals(zones: dict | None) -> list[tuple[str, float, float, float]]:
    """레이블/판정의 {키: {start,end,tol?}} → (키, s, e, tol) 목록. null 은 건너뛴다."""
    out = []
    for key, z in (zones or {}).items():
        if not isinstance(z, dict) or z.get("start") is None or z.get("end") is None:
            continue
        s = z["start"] if isinstance(z["start"], (int, float)) \
            else schemas.parse_ts(z["start"])
        e = z["end"] if isinstance(z["end"], (int, float)) \
            else schemas.parse_ts(z["end"])
        if e > s:
            out.append((key, float(s), float(e), float(z.get("tol") or 0.0)))
    return sorted(out, key=lambda t: t[1])


def _merge(ivs: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1] + 1e-9:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _subtract(ivs: list[Interval], cuts: list[Interval]) -> list[Interval]:
    """ivs − cuts (aux 중립 창 제거용)."""
    out = list(_merge(ivs))
    for c0, c1 in _merge(cuts):
        nxt: list[Interval] = []
        for s, e in out:
            if c1 <= s or e <= c0:
                nxt.append((s, e))
                continue
            if s < c0:
                nxt.append((s, c0))
            if c1 < e:
                nxt.append((c1, e))
        out = nxt
    return out


def _overlap_sec(a: list[Interval], b: list[Interval]) -> float:
    total = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            total += max(0.0, min(e1, e2) - max(s1, s2))
    return total


def _length(ivs: list[Interval]) -> float:
    return sum(e - s for s, e in _merge(ivs))


def score_one(label: dict, predicted: dict | None) -> dict:
    """레이블 1편 × Stage 1 exception_sector 판정 → 채점. 순수.

    predicted 는 stage1.json 의 exception_sector(시각 문자열 또는 초). None 이면
    '판정 없음'(전부 본편 취급)으로 채점한다."""
    lab_zones = _zone_intervals(label.get("labels"))
    aux = [(s, e) for _k, s, e, _t in _zone_intervals(label.get("aux"))]
    pred = [(s, e) for _k, s, e, _t in _zone_intervals(predicted or {})]

    lab_union = _merge([(s, e) for _k, s, e, _t in lab_zones])
    pred_union = _merge(pred)

    # 정책 4 정밀화: **명시 zone 이 aux 보다 우선** — 포핸즈1 credit 카드는 오버레이
    # 창(4005~4051.5) 안에 있지만 명시 레이블이다. aux 를 통째로 중립 처리하면 명시
    # zone 까지 지워져 uncovered 0 이 된다(픽스처 재현) — aux 는 zone 밖 부분만 중립.
    aux_neutral = _subtract(aux, lab_union)

    # 정책 1 — 합집합 이분법. 레이블 쪽은 그대로, 판정 쪽에서 aux 중립 창만 제거
    lab_eff = lab_union
    pred_eff = _subtract(pred_union, aux_neutral)
    inter = _overlap_sec(lab_eff, pred_eff)
    union = _length(lab_eff) + _length(pred_eff) - inter
    miss_sec = round(_length(lab_eff) - inter, 3)          # 오염 위험(정책 2)
    overreach_sec = round(_length(pred_eff) - inter, 3)    # 재료 손실

    # zone 별 — uncovered ≤ tol + SNAP_TOL 이면 pass (정책 3)
    zones_out = []
    all_pass = True
    for key, s, e, tol in lab_zones:
        zone_eff = [(s, e)]                                # 명시 zone 은 aux 우선
        covered = _overlap_sec(zone_eff, pred_union)
        uncovered = round(_length(zone_eff) - covered, 3)
        allow = round(tol + SNAP_TOL_SEC, 3)
        ok = uncovered <= allow
        all_pass = all_pass and ok
        zones_out.append({"zone": key, "span": [round(s, 3), round(e, 3)],
                          "uncovered_sec": uncovered, "allow_sec": allow,
                          "pass": ok})
    return {
        "verdict": "pass" if all_pass else "fail",
        "miss_sec": miss_sec,
        "overreach_sec": overreach_sec,
        "overreach_warn": overreach_sec > OVERREACH_WARN_SEC,
        "union_iou": round(inter / union, 3) if union > 0 else None,
        "zones": zones_out,
        "predicted": [[round(s, 3), round(e, 3)] for s, e in pred_union],
        "aux_neutral_sec": round(_length(aux_neutral), 3),
    }


def match_job_to_label(run_log: dict, labels: dict[str, dict]) -> str | None:
    """job run_log ↔ 레이블 파일 매칭 — input 파일명 == source_hint."""
    video = Path(str((run_log.get("input") or {}).get("video_path") or "")).name
    for name, lab in labels.items():
        if lab.get("source_hint") == video:
            return name
    return None


def load_labels(label_dir: str | Path) -> dict[str, dict]:
    out = {}
    for f in sorted(Path(label_dir).glob("*.json")):
        out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return out


def render_report(rows: list[dict[str, Any]]) -> str:
    """채점표 → markdown. rows: [{name, verdict, miss_sec, …, note?}]"""
    lines = ["# exception 채점표 (Stage 1 vs 수동 레이블)",
             "",
             "정책: 합집합 이분법 · miss(오염)>overreach(손실) · 관용 = tol+2.0s · aux 중립",
             "",
             "| 소재 | 판정 | miss(오염)s | overreach s | 합집합 IoU | zone 상세 |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        zs = " · ".join(
            f"{z['zone']} {'✓' if z['pass'] else '✗ uncovered ' + str(z['uncovered_sec']) + 's'}"
            for z in r.get("zones") or []) or "—"
        mark = "**fail**" if r["verdict"] == "fail" else "pass"
        lines.append(f"| {r['name']} | {mark} | {r['miss_sec']} | "
                     f"{r['overreach_sec']}{'⚠' if r.get('overreach_warn') else ''} | "
                     f"{r['union_iou']} | {zs} |")
    fails = [r for r in rows if r["verdict"] == "fail"]
    lines += ["", f"합계: {len(rows)}편 채점 · fail {len(fails)}편"
              + (" — " + ", ".join(r["name"] for r in fails) if fails else "")]
    return "\n".join(lines) + "\n"
