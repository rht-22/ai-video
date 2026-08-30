"""M5 — edit_overrides/v2 수용 (어댑터 계약 C4).

편집실 수정(JSON)은 **v2 키·의미 무변경**으로 받고, v3 는 해석만 다르다:
사람이 찍은 clip 경계가 격자 밖이면 **최근접 grid span 경계로 정착**(스냅)하고
오차를 기록한다(C4 문구 그대로 — 관용 한계 없음: 사람 의도를 거절하지 않고
격자로 데려온다). 계약 검증은 기존 `edit_overrides.validate_overrides` 재사용 —
새 검증 발명 금지(같은 JSON 이 v1/v3 어디서든 같은 이유로 거절돼야 한다).

처리 범위(발주서 v3-m5 §B):
  clips[](원본 절대초·전량 교체) → 스냅 정착 + span_ids 재계산 + 뮤트 승계
  title.top_title → layout 교체 · subtitles[](편집본 좌표) → 전량 교체(C6 그대로)
  design → style.json design 위에 얹기(소비는 아는 키만 — C5)
  그 밖의 키(tts·images·texts·title.segments) → **unhandled 로 기록**(조용한
  무시 금지 — v3 후속 마일스톤 재료).

cue 는 source_time_sec 신원(C2)으로 승계한다 — timeline 이 바뀌어도 같은 소스
창의 cue 는 재합성 없이 편집본 좌표만 재계산한다(재과금 방지 — 신원 규약의 존재
이유). 새 timeline 이 cue 의 소스 창을 더는 담지 않으면 그 cue 는 드랍 + 기록.
"""
from __future__ import annotations

from typing import Any

from app.v3 import assemble

HANDLED_KEYS = ("schema", "clips", "title", "subtitles", "design")


def snap_to_span_edges(sec: float, edges: list[float]) -> tuple[float, float]:
    """최근접 span 경계로 정착 → (스냅 시각, 오차). 동률은 이른 쪽(결정성)."""
    if not edges:
        raise ValueError("grid 에 span 경계가 없다")
    best = min(edges, key=lambda g: (abs(g - sec), g))
    return best, round(abs(best - sec), 3)


def spans_in_window(grid: dict, t0: float, t1: float) -> list[str]:
    """[t0, t1) 창에 중점이 드는 span id 목록 — chunk 소속과 같은 중점 규칙."""
    out = []
    for sp in sorted(grid.get("span_candidates") or [],
                     key=lambda s: (float(s["t_in"]), s["id"])):
        mid = (float(sp["t_in"]) + float(sp["t_out"])) / 2.0
        if t0 <= mid < t1:
            out.append(sp["id"])
    return out


def _inherit_mute(old_timeline: list[dict], s: float, e: float) -> bool:
    """새 구간과 가장 많이 겹치는 기존 클립의 use_original_audio 승계(없으면 원음)."""
    best_ov, best = 0.0, True
    for c in old_timeline:
        ov = min(e, float(c["clip_end_sec"])) - max(s, float(c["clip_start_sec"]))
        if ov > best_ov:
            best_ov, best = ov, bool(c.get("use_original_audio", True))
    return best


def apply_overrides_to_plan(ov: dict, plan: dict, grid: dict,
                            segments: list[dict], resources: dict) \
        -> tuple[dict, list[dict], dict, dict]:
    """검증 통과한 overrides → (새 plan, 새 segments, 새 resources, 적용 기록).

    순수 — 파일 입출력 없음. resources 의 cue 는 source_time_sec 신원으로 승계."""
    record: dict[str, Any] = {"applied": [], "unhandled": [], "snap_log": [],
                              "cues_dropped": []}
    for k in ov:
        if k not in HANDLED_KEYS:
            record["unhandled"].append(k)

    new_plan = {**plan, "timeline": [dict(c) for c in plan.get("timeline") or []]}

    clips = ov.get("clips")
    if clips:
        edges = sorted({round(float(sp[k]), 3)
                        for sp in grid.get("span_candidates") or []
                        for k in ("t_in", "t_out")})
        old_timeline = plan.get("timeline") or []
        timeline = []
        for i, c in enumerate(clips):
            s_raw, e_raw = float(c["start_sec"]), float(c["end_sec"])
            s, err_s = snap_to_span_edges(s_raw, edges)
            e, err_e = snap_to_span_edges(e_raw, edges)
            if e <= s:                       # 짧은 구간이 같은 경계로 접힌 극단 케이스
                idx = edges.index(s)
                e = edges[min(idx + 1, len(edges) - 1)]
            if err_s > 0 or err_e > 0:
                record["snap_log"].append(
                    {"clip": i, "raw": [s_raw, e_raw], "snapped": [round(s, 3), round(e, 3)],
                     "err": [err_s, err_e]})
            timeline.append({
                "role": str(c.get("role") or "build"),
                "clip_start_sec": round(s, 3), "clip_end_sec": round(e, 3),
                "subtitle": "",
                "use_original_audio": _inherit_mute(old_timeline, s, e),
                "reframe": {"mode": "center"},
                "span_ids": spans_in_window(grid, s, e),
            })
        new_plan["timeline"] = timeline
        record["applied"].append(f"clips({len(timeline)}개 · 전량 교체)")

    title = ov.get("title") or {}
    if title.get("top_title") is not None:
        layout = dict(new_plan.get("layout") or {})
        layout["top_title"] = str(title["top_title"])
        new_plan["layout"] = layout
        record["applied"].append("title.top_title")

    new_segments = segments
    if ov.get("subtitles") is not None:
        new_segments = [{"start_sec": round(float(s["start_sec"]), 3),
                         "end_sec": round(float(s["end_sec"]), 3),
                         "text": str(s["text"])} for s in ov["subtitles"]]
        record["applied"].append(f"subtitles({len(new_segments)}줄 · 전량 교체)")

    # cue 승계 — source_time_sec 신원(C2). 편집본 좌표만 새 timeline 으로 재계산.
    offsets = assemble.edited_offsets(new_plan["timeline"])
    new_files = []
    for f in resources.get("tts_cue_files") or []:
        cue = dict(f.get("cue") or {})
        if cue.get("source_time_sec") is None:
            continue
        e0 = assemble.to_edited_sec(float(cue["source_time_sec"]), offsets)
        src_end = float(cue["source_time_sec"]) + float(cue.get("duration_sec") or 0)
        e1 = assemble.to_edited_sec(src_end, offsets)
        if e0 is None or e1 is None or e1 <= e0:
            record["cues_dropped"].append(
                {"source_time_sec": cue["source_time_sec"],
                 "reason": "새 timeline 이 cue 소스 창을 담지 않는다"})
            continue
        cue["start_sec"], cue["end_sec"] = e0, e1
        cue["duration_sec"] = round(e1 - e0, 3)
        new_files.append({**f, "cue": cue})
    new_resources = {**resources, "tts_cue_files": new_files}

    return new_plan, new_segments, new_resources, record
