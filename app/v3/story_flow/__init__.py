"""사람 편집 흐름 체인 — 오케스트레이터.

    1 주제(topic) → 2 씬(scenes) → 3 대사(lines) → 4 내레이션(narration, 즉시 합성)
    → 5 덮개(cover, 그 자리를 다시 본다)

산출은 v3 `checkpoint_story` 계약 그대로(schema v3_story/v1 · beats · narration_cues ·
budget)라 조립 이후(assemble → watch_trim → stage4 → finalize)는 무변경이다. 덧붙인
것: beats[].covers(덮개 클립 — assemble 이 `cover` 클립으로 정식 등록), cue 의
audio_path/measured_sec(resources 가 재합성 대신 그대로 쓴다), doc.flow(감사 기록).
게이트: `--story-flow human`. 미지정(legacy) = 종전 story.run_story 그대로(회귀 0).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from app.v3 import schemas
from app.v3.story import SCHEMA_STORY, build_span_index
from app.v3.story_flow import cover as cv
from app.v3.story_flow import narration as nr
from app.v3.story_flow import select as sl
from app.v3.story_flow.common import (
    MAX_REASKS,
    call_json,
    fmt_t,
    meaning_rows,
    meaning_table,
    reject_block,
    sequence_rows,
)

TEMPLATE = "human_flow"
NARRATION_ALLOWANCE_SEC = 2.5    # 대사 예산에서 내레이션 자리로 미리 비워 두는 몫(곳당)


def _research_block(research_context: str) -> str:
    rc = (research_context or "").strip()
    return f"\n\n### 작품 정보(리서치)\n{rc[:3000]}" if rc else ""


def _hint_block(stage1_doc: dict | None) -> str:
    hints = (stage1_doc or {}).get("shorts_candidates") or []
    if not hints:
        return ""
    rows = [f"- {h.get('approx_time', '?')} {h.get('situation', '')} — {h.get('why', '')}"
            for h in hints[:8] if isinstance(h, dict)]
    return "\n## 본 눈의 추천(참고 — 영상을 본 단계가 지목한 쇼츠감)\n" + "\n".join(rows) + "\n"


def _loop(name: str, make_prompt: Callable[[str], str], validate: Callable[[dict], tuple],
          gemini, audit: dict, log=print):
    """반려·재질의 루프(≤MAX_REASKS). validate → (obj|None, problems, notes)."""
    rej = ""
    rec: list[dict] = []
    for attempt in range(1 + MAX_REASKS):
        t0 = time.time()
        try:
            resp = call_json(gemini, make_prompt(rej))
            out = validate(resp)
        except ValueError as e:
            out = (None, [f"응답 오류: {e}"], [])
        obj, problems, notes = (out + ([],))[:3] if len(out) == 2 else out
        rec.append({"attempt": attempt + 1, "elapsed": round(time.time() - t0, 1),
                    "problems": list(problems), "notes": list(notes)})
        for n in notes:
            log(f"  [v3/flow/{name}] · {n}")
        if obj is not None:
            audit[name] = rec
            return obj
        log(f"  [v3/flow/{name}] 반려 {len(problems)}건 — 재질의")
        rej = reject_block(problems)
    audit[name] = rec
    raise ValueError(f"[{name}] 재질의 소진 — 마지막 사유: {rec[-1]['problems'][:3]}")


def run_story_flow(gemini, stage2_doc: dict, grid: dict, *, work_title: str,
                   research_context: str = "", target_sec: float, max_sec: float,
                   video_path: Path | None = None, output_dir: Path | None = None,
                   stage1_doc: dict | None = None, tone_block: str = "",
                   synth_fn: Callable[[str, Path], float] | None = None,
                   probe: bool = True, log=print) -> tuple[dict, dict]:
    span_index, span_order = build_span_index(stage2_doc, grid)
    if not span_index:
        raise ValueError("분석된 span 이 없다 — Stage 2 가 선행돼야 한다")
    rows = meaning_rows(stage2_doc)
    rows = [r for r in rows if any(s in span_index for s in r["span_ids"])]
    if not rows:
        raise ValueError("분석된 사건 단위가 없다")
    rows_by_idx = {r["idx"]: r for r in rows}
    audit: dict[str, Any] = {"flow": TEMPLATE, "spans_available": len(span_index),
                             "meanings": len(rows)}
    research_block = _research_block(research_context)
    seq_block = "\n".join(f"- 시퀀스 {s['number']} [{fmt_t(s['t0'])}~{fmt_t(s['t1'])}] {s['content']}"
                          for s in sequence_rows(stage2_doc))
    meaning_block = meaning_table(rows)

    # 1 주제
    topic = _loop("topic", lambda rej: sl.TOPIC_PROMPT.format(
        target_sec=target_sec, max_sec=max_sec, work_title=work_title,
        research_block=research_block, sequence_block=seq_block,
        hint_block=_hint_block(stage1_doc), meaning_block=meaning_block,
        reject_block=rej), lambda r: sl.validate_topic(r, rows), gemini, audit, log)
    log(f"  [v3/flow/topic] {topic['topic']} (핵심 m{'/m'.join(f'{i:03d}' for i in topic['core_meanings'])})")

    # 2 씬 + 제목
    scenes_doc = _loop("scenes", lambda rej: sl.SCENES_PROMPT.format(
        topic=topic["topic"], min_scenes=sl.MIN_SCENES, max_scenes=sl.MAX_SCENES,
        target_sec=target_sec, title_max=sl.TITLE_MAX_CHARS, work_title=work_title,
        research_block=research_block, meaning_block=meaning_block,
        reject_block=rej), lambda r: sl.validate_scenes(r, rows), gemini, audit, log)
    scenes, title = scenes_doc["scenes"], scenes_doc["title"]
    log("  [v3/flow/scenes] " + " → ".join(f"m{s['meaning']:03d}[{s['purpose']}]" for s in scenes)
        + f" · 제목 {title['line1']} / {title['line2']}")

    # 3 대사
    material, allowed = sl.lines_material(scenes, rows, span_index)
    n_hint = max(2, len(scenes))
    budget = max(20.0, max_sec - NARRATION_ALLOWANCE_SEC * (n_hint + 1))
    beats = _loop("lines", lambda rej: sl.LINES_PROMPT.format(
        topic=topic["topic"], title_line1=title["line1"], title_line2=title["line2"],
        budget_sec=budget, material_block=material, reject_block=rej,
        skip_sec=sl.SKIP_MAX_VOICED_SEC, skip_lines=sl.SKIP_MAX_LINES),
        lambda r: sl.validate_beats(r, span_index, allowed, budget_sec=budget),
        gemini, audit, log)
    _dial = sum(span_index[x]["t_out"] - span_index[x]["t_in"]
                for b in beats for x in b["span_ids"])
    jumps = sl.compute_jumps(beats, span_index)
    log(f"  [v3/flow/lines] 비트 {len(beats)}개 · 조각 {sum(len(b['span_ids']) for b in beats)}개 "
        f"· {_dial:.1f}s · 점프 {len(jumps)}곳"
        + (" (" + ", ".join(f"[{j['before_beat'] - 1}]→[{j['before_beat']}] {j['gap_sec']:.0f}s"
                            for j in jumps) + ")" if jumps else ""))
    audit["jumps"] = jumps

    # 4 내레이션(화면 id 지정) + 합성
    scene_rows = {s["meaning"]: rows_by_idx[s["meaning"]] for s in scenes}
    available = nr.available_covers(beats, scene_rows, span_index)
    required = {0} | {j["before_beat"] for j in jumps}
    groups = _loop("narration", lambda rej: nr.PROMPT.format(
        max_chars=nr.NAR_MAX_CHARS, max_n=max(nr.MAX_NARRATIONS, len(required) + 1),
        work_title=work_title, research_block=research_block, topic=topic["topic"],
        title_line1=title["line1"], title_line2=title["line2"],
        beats_block=nr.beats_block(beats, span_index, rows_by_idx, jumps),
        available_block=nr.available_block(available, span_index, allowed),
        tone_block=(f"\n{tone_block}\n" if tone_block else ""), reject_block=rej),
        lambda r: nr.validate_narrations(r, len(beats), required=required,
                                         available=set(available)),
        gemini, audit, log)
    nar_dir = (output_dir / "narration") if output_dir else Path("narration")
    nr.synthesize_groups(groups, nar_dir, synth_fn, log=log)
    for g in groups:
        log(f"  [v3/flow/narration] {g['anchor'][0]}{g['anchor'][1]}: "
            + " | ".join(f"{t} ({m:.2f}s)" for t, m in zip(g["lines"], g["measured"]))
            + (f"  화면 {g['cover_ids']}" if g.get("cover_ids") else "  화면 미지정"))

    # 5 덮개 — 그 자리를 다시 본다
    pbudget = {"left": cv.FLASH_BUDGET, "used": 0}
    covers_audit: list[dict] = []
    cues: list[dict] = []
    placed: list[tuple[float, float]] = []          # 이미 놓인 덮개 — 같은 화면 재사용 금지
    # 앵커별 순서: before 는 비트 순, after 는 마지막
    groups.sort(key=lambda g: (g["anchor"][1], 0 if g["anchor"][0] == "before" else 1))
    for b in beats:
        b.setdefault("covers", [])
    for gi, g in enumerate(groups):
        kind, k = g["anchor"]
        if not beats[k]["span_ids"]:
            log(f"  [v3/flow/cover] ⚠ 비트 {k} 가 비어 덮개를 못 만든다 — 내레이션 드랍 아님, "
                "다음 비트로 앵커 이동")
            k = next((i for i in range(k, len(beats)) if beats[i]["span_ids"]), None)
            if k is None:
                raise ValueError("덮개를 붙일 비트가 없다")
            g["anchor"] = (kind, k)
        cover = cv.choose_cover(
            (kind, k), g, beats, span_index, scene_rows, grid,
            gemini=gemini if probe else None, video=video_path,
            out_dir=(output_dir / "cover_probes") if (output_dir and probe) else None,
            budget=pbudget, placed=placed, log=log)
        removed = cv.apply_cover_to_beats(cover, beats, span_index, k)
        cover["removed_span_ids"] = removed
        placed.append((cover["t_in"], cover["t_out"]))
        beats[k]["covers"].append(cover)
        # cue — 묶음의 줄들을 덮개 안에 순서대로(측정 길이 비례가 아니라 실측 그대로,
        # 남는 여유는 마지막 줄 꼬리에)
        t = cover["t_in"] + 0.1
        for li, (text, m, path) in enumerate(zip(g["lines"], g["measured"], g["audio_paths"])):
            end = cover["t_out"] if li == len(g["lines"]) - 1 else min(cover["t_out"], t + m)
            cue = {"beat": k, "line": li, "text": text, "mode": "cover",
                   "speed": nr.NAR_SPEED, "source_time_sec": round(t, 3),
                   "source_end_sec": round(end, 3),
                   "muted_span_ids": [x for x in cover["span_ids"] if span_index[x]["is_audio"]],
                   "measured_sec": m}
            if path:
                cue["audio_path"] = path
            cues.append(cue)
            t = end
        beats[k]["narration"] = (beats[k].get("narration") or []) + list(g["lines"])
        covers_audit.append({"anchor": f"{kind}{k}", "kind": cover["kind"],
                             "t_in": cover["t_in"], "t_out": cover["t_out"], "L": cover["L"],
                             "probe": cover.get("probe"), "note": cover.get("note"),
                             "removed": removed, "cover_ids": g.get("cover_ids") or []})
        log(f"  [v3/flow/cover] {kind}{k} {cover['kind']} {fmt_t(cover['t_in'])}~{fmt_t(cover['t_out'])} "
            f"({cover['L']:.1f}s)" + (f" · 프로브 {cover['probe']['snap']} {cover['probe']['reason'][:40]}"
                                     if cover.get("probe") else " · 산술"))
    audit["covers"] = covers_audit
    audit["probe_calls"] = pbudget["used"]

    # 덮개가 통째로 삼킨(재료 0) 비트는 빠진다 — cue.beat 는 새 번호로 옮긴다
    keep_idx = [i for i, b in enumerate(beats) if b["span_ids"] or b.get("covers")]
    renum = {old: new for new, old in enumerate(keep_idx)}
    for c in cues:
        c["beat"] = renum.get(c["beat"], c["beat"])
    beats = [beats[i] for i in keep_idx]
    doc = build_story_doc(beats, span_index, cues, topic=topic, title=title,
                          scenes=scenes, target_sec=target_sec, max_sec=max_sec,
                          jumps=jumps)
    audit["pieces"] = len(beats)
    audit["attempts"] = [{"attempt": 1, "steps": {k: len(v) for k, v in audit.items()
                                                  if isinstance(v, list) and k in
                                                  ("topic", "scenes", "lines", "narration")}}]
    log(f"  [v3/flow] 완료 — 비트 {len(beats)}개 · 내레이션 {len(cues)}줄 · "
        f"{doc['budget']['total_after_sec']}s (프로브 {pbudget['used']}회)")
    return doc, audit


def beat_duration(b: dict, span_index: dict[str, dict]) -> float:
    ids = b["span_ids"]
    d = 0.0
    if ids:
        d = sum(span_index[x]["t_out"] - span_index[x]["t_in"] for x in ids)
        ht = b.get("head_trim_sec")
        if ht is not None:
            first = span_index[ids[0]]
            if first["t_in"] < ht < first["t_out"]:
                d -= ht - first["t_in"]
    for c in b.get("covers") or []:
        d += c["t_out"] - c["t_in"]
    return d


def build_story_doc(beats: list[dict], span_index: dict[str, dict], cues: list[dict], *,
                    topic: dict, title: dict, scenes: list[dict],
                    target_sec: float, max_sec: float,
                    jumps: list[dict] | None = None) -> dict:
    out_beats = []
    for i, b in enumerate(beats):
        ids = b["span_ids"]
        covers = b.get("covers") or []
        if ids:
            t0 = min([span_index[ids[0]]["t_in"]] + [c["t_in"] for c in covers if c["position"] == "before"])
            t1 = max([span_index[ids[-1]]["t_out"]] + [c["t_out"] for c in covers if c["position"] == "after"])
        else:
            t0 = min(c["t_in"] for c in covers)
            t1 = max(c["t_out"] for c in covers)
        out_beats.append({
            "number": i, "role": b["role"], "span_ids": list(ids),
            "time": {"start": schemas.format_ts(t0), "end": schemas.format_ts(t1)},
            "narration": b.get("narration") or None,
            "labels": [], "muted_span_ids": [],
            "why": b.get("action") or None, "link": None,
            "scene": b.get("scene"),
            "skipped": list(b.get("skipped") or []),
            "hole_before": list(b.get("hole_before") or []) or None,
            "covers": [dict(c) for c in covers],
            **({"head_trim_sec": b["head_trim_sec"]} if b.get("head_trim_sec") is not None else {}),
        })
    total = round(sum(beat_duration(b, span_index) for b in beats), 3)
    deficit = round(max(0.0, total - max_sec), 3)
    return {
        "schema": SCHEMA_STORY,
        "template": TEMPLATE,
        "reason": topic.get("why", ""),
        "topic": topic["topic"],
        "title": {"line1": title["line1"], "line2": title["line2"]},
        "beats": out_beats,
        "narration_cues": cues,
        "narration_dropped": [],
        "budget": {"target_sec": target_sec, "max_sec": max_sec,
                   "total_before_sec": total, "total_after_sec": total,
                   "removed": [], "unmet": deficit > 0, "deficit_sec": deficit},
        "flow": {"scenes": scenes, "core_meanings": topic.get("core_meanings"),
                 "jumps": list(jumps or [])},
    }


__all__ = ["run_story_flow", "build_story_doc", "beat_duration", "TEMPLATE"]
