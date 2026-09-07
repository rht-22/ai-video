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

import copy
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
TRIM_HEADROOM_SEC = 4.0          # watch_trim 이 잘라낼 여유(2026-09-03): 계획을 이만큼 넘치게 짜고
                                 # 초안을 본 뒤 덜어낸다. 상한(max_sec)은 기존 예산 컷 벨트가 지킨다.


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


COVER_REASK_MAX = 1              # 덮개 프로브가 "문장이 화면과 모순"이라 하면 걸음 4·5 를 다시 도는
                                 # 상한(갭 8 첫 삽, 2026-09-07) — 편당 1회, 무한 루프 금지


def _loop(name: str, make_prompt: Callable[[str], str], validate: Callable[[dict], tuple],
          gemini, audit: dict, log=print, *, initial_reject: str = ""):
    """반려·재질의 루프(≤MAX_REASKS). validate → (obj|None, problems, notes).
    initial_reject: 첫 호출부터 싣는 반려 사유(하류가 되돌린 관측 — 갭 8)."""
    rej = initial_reject
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
                   probe: bool = True,
                   exclude_topics: tuple[str, ...] = (),
                   exclude_ranges: tuple[tuple[float, float], ...] = (),
                   log=print) -> tuple[dict, dict]:
    """exclude_topics / exclude_ranges: 이미 만든 쇼츠(주제 문장 · 원본 초 구간) — 걸음
    1·2 프롬프트에 제외 블록으로 싣고, 구간과 겹치는 사건 단위는 검증기가 반려한다
    (2026-09-07). 둘 다 비면 프롬프트·검증 종전과 동일."""
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
    # 무대사 구간 목록(갭 3, 2026-09-07) — 전사 단어 간격으로 코드가 재서 걸음 1·2 재료에
    # 별도 블록으로 싣는다. 없으면 빈 문자열(프롬프트 종전과 동일).
    _dur = grid.get("duration_sec") or (grid.get("source") or {}).get("duration_sec")
    silent = sl.silent_runs(grid.get("words") or [], _dur)
    silent_blk = sl.silent_block(silent, rows)
    audit["silent_runs"] = len(silent)
    if silent:
        log(f"  [v3/flow] 무대사 구간 {len(silent)}개 · {sum(z - a for a, z in silent):.0f}s — 재료 블록으로 싣는다")

    # 제외(이미 만든 쇼츠, 2026-09-07) — 구간 → 사건 단위 idx 는 코드가 정한다
    excluded = sl.excluded_meaning_ids(rows, exclude_ranges)
    exclude_blk = sl.exclude_block(exclude_topics, excluded, rows)
    if exclude_blk:
        audit["excluded"] = {"topics": list(exclude_topics),
                             "ranges": [list(r) for r in exclude_ranges],
                             "meanings": sorted(excluded)}
        log(f"  [v3/flow] 제외 — 주제 {len(exclude_topics)}건 · 구간 {len(exclude_ranges)}개 → "
            f"사건 단위 {len(excluded)}개(" + "/".join(f"m{k:03d}" for k in sorted(excluded)) + ")")
        if exclude_ranges and not excluded:
            log("  [v3/flow] ⚠ 제외 구간이 어떤 사건 단위와도 절반 이상 겹치지 않는다 — 프롬프트 지시로만 막는다")

    # 1 주제
    topic = _loop("topic", lambda rej: sl.TOPIC_PROMPT.format(
        target_sec=target_sec, max_sec=max_sec, work_title=work_title,
        research_block=research_block, sequence_block=seq_block,
        hint_block=_hint_block(stage1_doc), meaning_block=meaning_block,
        silent_block=silent_blk, exclude_block=exclude_blk,
        reject_block=rej), lambda r: sl.validate_topic(r, rows, excluded=excluded),
        gemini, audit, log)
    log(f"  [v3/flow/topic] {topic['topic']} (핵심 m{'/m'.join(f'{i:03d}' for i in topic['core_meanings'])})")

    # 2 씬 + 제목
    scenes_doc = _loop("scenes", lambda rej: sl.SCENES_PROMPT.format(
        topic=topic["topic"], min_scenes=sl.MIN_SCENES, max_scenes=sl.MAX_SCENES,
        target_sec=target_sec, title_max=sl.TITLE_MAX_CHARS, work_title=work_title,
        research_block=research_block, meaning_block=meaning_block,
        silent_block=silent_blk, exclude_block=exclude_blk,
        reject_block=rej), lambda r: sl.validate_scenes(r, rows, excluded=excluded),
        gemini, audit, log)
    scenes, title = scenes_doc["scenes"], scenes_doc["title"]
    log("  [v3/flow/scenes] " + " → ".join(f"m{s['meaning']:03d}[{s['purpose']}]" for s in scenes)
        + f" · 제목 {title['line1']} / {title['line2']}")

    # 3 대사
    material, allowed = sl.lines_material(scenes, rows, span_index)
    n_hint = max(2, len(scenes))
    budget = max(20.0, max_sec + TRIM_HEADROOM_SEC - NARRATION_ALLOWANCE_SEC * (n_hint + 1))
    beats = _loop("lines", lambda rej: sl.LINES_PROMPT.format(
        topic=topic["topic"], title_line1=title["line1"], title_line2=title["line2"],
        budget_sec=budget, material_block=material, reject_block=rej,
        skip_sec=sl.SKIP_MAX_VOICED_SEC, skip_lines=sl.SKIP_MAX_LINES),
        lambda r: sl.validate_beats(r, span_index, allowed, budget_sec=budget,
                                    floor_ratio=sl.BUDGET_FLOOR_RATIO,
                                    material_sec=sum(span_index[x]["t_out"] - span_index[x]["t_in"]
                                                     for x in allowed if x in span_index)),
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
    # 무대사 편(갭 3): 대사가 한 조각도 없으면 내레이션이 뼈대다 — 비트 안 화면을 덮개
    # 후보에 열고(길이가 늘지 않게 그 위에 얹는다), 곳 수 상한을 올리고, 견적 밀도 하한을 건다.
    silent_episode = not any(span_index[x]["is_audio"] for b in beats for x in b["span_ids"])
    audit["silent_episode"] = silent_episode
    own_ids: set[str] = nr.silent_beat_ids(beats, span_index) if silent_episode else set()
    available = nr.available_covers(beats, scene_rows, span_index,
                                    include_silent_beats=silent_episode)
    required = {0} | {j["before_beat"] for j in jumps}
    max_n = max(nr.MAX_NARRATIONS, len(required) + 1)
    min_total: float | None = None
    silent_note = ""
    if silent_episode:
        max_n = max(max_n, len(beats) + 1, nr.SILENT_MAX_NARRATIONS)
        _beat_total = sum(beat_duration(b, span_index) for b in beats)
        min_total = round(nr.SILENT_NARRATION_MIN_RATIO * _beat_total, 1)
        silent_note = nr.silent_note(min_total, len(beats))
        log(f"  [v3/flow/narration] 무대사 편 — 내레이션이 뼈대: 상한 {max_n}곳 · 견적 하한 "
            f"{min_total}s (편 {_beat_total:.1f}s × {nr.SILENT_NARRATION_MIN_RATIO})")
    rhythm = nr.scene_rhythm(scene_rows, grid)
    audit["rhythm"] = rhythm
    if rhythm:
        log(f"  [v3/flow/narration] 호흡 실측 — 발화 {rhythm['speech_cps']}자/초 · "
            f"컷 간격 {rhythm['cut_gap_med']}s · 최장 무발화 {rhythm['longest_silence']}s")
    nar_dir = (output_dir / "narration") if output_dir else Path("narration")
    # 문장 단위 합성 캐시 — 되돌림(갭 8)으로 걸음 4 를 다시 돌아도 같은 문장은 재요금 없이
    # 지난 파일을 복사한다(edge/ElevenLabs 모두 결정적이지 않아 길이도 함께 재사용).
    _nar_cache: dict[str, tuple[float, Path]] = {}
    _synth = synth_fn or nr.default_synth

    def _synth_cached(text: str, path: Path) -> float:
        hit = _nar_cache.get(text)
        if hit and hit[1].exists() and hit[1] != path:
            import shutil
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(hit[1], path)
            return hit[0]
        sec = _synth(text, path)
        _nar_cache[text] = (float(sec), path)
        return sec

    pbudget = {"left": cv.FLASH_BUDGET, "used": 0}
    beats_snapshot = copy.deepcopy(beats)
    reask_note = ""
    contradictions_all: list[dict] = []
    for pass_no in range(1 + COVER_REASK_MAX):
        if pass_no:
            # 되돌림: 덮개가 비트에서 가져간 조각·head_trim 을 원상 복구하고 걸음 4 부터
            beats = copy.deepcopy(beats_snapshot)
            log(f"  [v3/flow] ↩ 덮개 프로브가 문장·화면 모순 {len(contradictions_all)}건을 봤다 — "
                f"걸음 4(내레이션)부터 다시({pass_no}/{COVER_REASK_MAX})")
        groups = _loop("narration" if not pass_no else f"narration_reask{pass_no}",
                       lambda rej: nr.PROMPT.format(
            max_chars=nr.NAR_MAX_CHARS, max_n=max_n,
            work_title=work_title, research_block=research_block, topic=topic["topic"],
            title_line1=title["line1"], title_line2=title["line2"],
            beats_block=nr.beats_block(beats, span_index, rows_by_idx, jumps),
            silent_note=silent_note,
            rhythm_block=nr.rhythm_block(rhythm),
            available_block=nr.available_block(available, span_index, allowed,
                                               beat_ids=own_ids or None),
            tone_block=(f"\n{tone_block}\n" if tone_block else ""), reject_block=rej),
            lambda r: nr.validate_narrations(r, len(beats), required=required,
                                             available=set(available), max_n=max_n,
                                             min_total_sec=min_total),
            gemini, audit, log, initial_reject=reask_note)
        nr.synthesize_groups(groups, nar_dir, _synth_cached, log=log)
        for g in groups:
            log(f"  [v3/flow/narration] {g['anchor'][0]}{g['anchor'][1]}: "
                + " | ".join(f"{t} ({m:.2f}s)" for t, m in zip(g["lines"], g["measured"]))
                + (f"  화면 {g['cover_ids']}" if g.get("cover_ids") else "  화면 미지정"))

        # 5 덮개 — 그 자리를 다시 본다
        covers_audit: list[dict] = []
        cues: list[dict] = []
        placed: list[tuple[float, float]] = []          # 이미 놓인 덮개 — 같은 화면 재사용 금지
        bad: list[dict] = []                            # 프로브가 본 문장·화면 모순(갭 8)
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
                budget=pbudget, placed=placed,
                # 자기 비트 화면 허용은 before 앵커만 — after 덮개가 마지막 비트 머리를 가져가면
                # 나머지 조각이 덮개보다 먼저 재생돼 시간이 되감긴다
                allow_ids=((own_ids & set(beats[k]["span_ids"])) or None) if kind == "before" else None,
                log=log)
            removed = cv.apply_cover_to_beats(cover, beats, span_index, k)
            cover["removed_span_ids"] = removed
            if kind == "before":
                capped = cv.cap_head_gap(beats[k], span_index)
                if capped:
                    cover["head_gap_cap"] = capped
                    log(f"  [v3/flow/cover] 비트 {k} 머리 무발화 {capped['before_sec']:.1f}s → "
                        f"{capped['after_sec']:.1f}s (상한 {cv.HEAD_GAP_MAX_SEC}s · 조각 "
                        f"{len(capped['removed_span_ids'])}개 제거"
                        + (f" · 첫 조각 {capped['head_trim_sec']}s 부터" if capped['head_trim_sec'] else "") + ")")
            placed.append((cover["t_in"], cover["t_out"]))
            beats[k]["covers"].append(cover)
            if cover.get("probe") and cover["probe"].get("text_matches") is False:
                bad.append({"anchor": f"{kind}{k}", "text": " ".join(g["lines"]),
                            "seen": cover["probe"].get("seen", ""),
                            "reason": cover["probe"].get("reason", "")})
            # cue — 묶음의 줄들을 덮개 안에 순서대로(측정 길이 비례가 아니라 실측 그대로,
            # 남는 여유는 마지막 줄 꼬리에)
            t = cover["t_in"] + 0.1
            # 붙잡은 덮개는 소스 끝(t_out)에 cue 끝을 두고, 붙잡은 꼬리는 finalize_cues 가 잇는다
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

        # 갭 8 첫 삽 — 프로브가 "문장이 화면과 모순"이라 한 묶음이 있으면 되돌린다(1회).
        # 프로브 없는 묶음(hold·산술·예산 소진)은 판정이 없어 그대로다(기록으로 드러난다).
        for x in bad:
            log(f"  [v3/flow/cover] ⚠ 문장·화면 모순 {x['anchor']}: 「{x['text'][:30]}」 ↔ 화면 「{x['seen'][:40]}」")
        if not bad or pass_no >= COVER_REASK_MAX:
            if bad:
                log(f"  [v3/flow] ⚠ 모순 {len(bad)}건이 남았지만 되돌림 상한({COVER_REASK_MAX}) — 검수 대상으로 기록")
                audit["narration_contradictions_left"] = bad
            break
        contradictions_all = bad
        audit["narration_contradictions"] = bad
        reask_note = reject_block([
            f"{x['anchor']} 의 문장 「{x['text']}」이 지정한 화면과 모순된다(화면을 다시 본 결과: "
            f"「{x['seen']}」). 화면이 실제로 보여주는 것을 말하거나, 그 행동이 보이는 조각으로 cover 를 바꿔라"
            for x in bad])

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
        d += c["t_out"] - c["t_in"] + float(c.get("hold_sec") or 0.0)
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
