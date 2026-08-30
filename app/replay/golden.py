"""골든 케이스 채점기 — 산출 컷 리스트를 정답지와 대조한다(인터페이스, 순수).

M0 범위는 인터페이스까지다(발주서 §4) — 실소재 채점은 M3 에서 한다.
1호 정답지 재료: ai-premiere-pro `templates/recap_shorts/fh_captions.example.json`
(포핸즈 — 사람이 만든 14세그먼트 몽타주의 자막 파일). 그 파일에는 **편집 타임라인의
cue 시각**만 있고 소스 컷 리스트가 없으므로, captions_to_segments 로 cue 뭉치를
근사 세그먼트로 만들어 쓴다 — 근사임을 정답지 스키마에 명시한다(derived=true).
M3 정답지는 소스 시간 컷 리스트를 직접 싣는다.

정답지 스키마 (golden_cuts/v1):
  {"schema": "golden_cuts/v1", "space": "source"|"edited", "derived": bool,
   "segments": [{"start_sec": float, "end_sec": float}, ...]}
"""
from __future__ import annotations

import json
from pathlib import Path

from app.replay.metrics import clip_span, mean

GOLDEN_SCHEMA = "golden_cuts/v1"
DEFAULT_GAP_SEC = 0.4        # cue 사이 이 이상 비면 다른 세그먼트로 본다(근사 유도용)
DEFAULT_BOUNDARY_TOL = 0.5   # 경계 일치 판정 허용 오차(초)


def load_golden(path: str | Path) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != GOLDEN_SCHEMA:
        raise ValueError(
            f"정답지 스키마가 아닙니다: {doc.get('schema')!r} (기대: {GOLDEN_SCHEMA}) — "
            "fh_captions 류 자막 파일은 captions_to_segments 로 먼저 변환하세요")
    segs = doc.get("segments") or []
    if not segs:
        raise ValueError("정답지 segments 가 비어 있습니다")
    return doc


def captions_to_segments(captions: dict, gap_sec: float = DEFAULT_GAP_SEC) -> dict:
    """fh_captions 류(narration/dialogue/labels 의 s·e cue) → 근사 정답지.

    모든 cue 를 시각순으로 합쳐 gap_sec 이상 빈 곳에서 끊는다. 라벨(labels)은
    대사 위에 얹히는 장식이라 경계 재료에서 뺀다. 산출은 편집 타임라인 좌표다."""
    cues = []
    for k in ("narration", "dialogue"):
        for c in captions.get(k) or []:
            cues.append((float(c["s"]), float(c["e"])))
    if not cues:
        raise ValueError("captions 에 narration/dialogue cue 가 없습니다")
    cues.sort()
    segments: list[dict] = []
    cur_s, cur_e = cues[0]
    for s, e in cues[1:]:
        if s - cur_e > gap_sec:
            segments.append({"start_sec": round(cur_s, 3), "end_sec": round(cur_e, 3)})
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    segments.append({"start_sec": round(cur_s, 3), "end_sec": round(cur_e, 3)})
    return {"schema": GOLDEN_SCHEMA, "space": "edited", "derived": True,
            "gap_sec": gap_sec, "segments": segments}


def _iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def score_cuts(pred_clips: list[dict], golden: dict,
               tol_sec: float = DEFAULT_BOUNDARY_TOL) -> dict:
    """산출 컷 리스트 vs 정답지 — 세그먼트 IoU 매칭 + 경계 정밀/재현율.

    매칭은 IoU 내림차순 그리디 1:1 — 같은 입력이면 항상 같은 결과(결정성).
    경계 히트 = 예측 경계가 정답 경계 tol_sec 안에 있는 것(시작·끝 각각)."""
    gold = [(float(s["start_sec"]), float(s["end_sec"])) for s in golden["segments"]]
    pred = [clip_span(c) for c in pred_clips]

    cand = sorted(
        ((_iou(p, g), pi, gi) for pi, p in enumerate(pred) for gi, g in enumerate(gold)),
        key=lambda t: (-t[0], t[1], t[2]))
    used_p: set[int] = set()
    used_g: set[int] = set()
    matches: list[dict] = []
    for iou, pi, gi in cand:
        if iou <= 0.0 or pi in used_p or gi in used_g:
            continue
        used_p.add(pi)
        used_g.add(gi)
        matches.append({"pred_idx": pi, "gold_idx": gi, "iou": round(iou, 3)})

    gold_bounds = sorted({round(v, 3) for g in gold for v in g})
    pred_bounds = sorted({round(v, 3) for p in pred for v in p})
    hit = sum(1 for pb in pred_bounds if any(abs(pb - gb) <= tol_sec for gb in gold_bounds))
    recall_hit = sum(
        1 for gb in gold_bounds if any(abs(pb - gb) <= tol_sec for pb in pred_bounds))
    precision = hit / len(pred_bounds) if pred_bounds else 0.0
    recall = recall_hit / len(gold_bounds) if gold_bounds else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "schema": "golden_score/v1",
        "space": golden.get("space", "source"),
        "derived_golden": bool(golden.get("derived")),
        "pred_n": len(pred),
        "gold_n": len(gold),
        "count_diff": len(pred) - len(gold),
        "matched_n": len(matches),
        "mean_iou": mean([m["iou"] for m in matches]) or 0.0,
        "boundary_tol_sec": tol_sec,
        "boundary_precision": round(precision, 3),
        "boundary_recall": round(recall, 3),
        "boundary_f1": round(f1, 3),
        "matches": matches,
        "unmatched_pred": sorted(set(range(len(pred))) - used_p),
        "unmatched_gold": sorted(set(range(len(gold))) - used_g),
    }
