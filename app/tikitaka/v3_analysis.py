"""Shared v3 visual analysis, adapted to Tikitaka's unchanged transcript IDs."""
from __future__ import annotations

import copy
import time
from pathlib import Path

from app.tikitaka.common import Job
from app.tikitaka.grid import build_grid, fingerprint, source_identity
from app.v3 import schemas

SCHEMA = "tikitaka_v3_analysis/v1"


def overlaps(start, end, ranges):
    return any(start < b and end > a if end > start else a <= start < b for a, b in ranges)


def adapt(stage2: dict, grid: dict, transcript: dict, *, title: str, cast: list[str], fp: str) -> dict:
    """Preserve clocks/text; bind speakers only to unambiguous heard evidence."""
    from app.v3.chunk_split import plan_chunks
    _, exceptions = plan_chunks(stage2)
    excluded = [(x["start_sec"], x["end_sec"]) for x in exceptions]
    excluded += [(schemas.parse_ts(c["time"]["start"]), schemas.parse_ts(c["time"]["end"]))
                 for s in stage2.get("sequences", []) for c in s.get("chunks", [])
                 if not c.get("meanings")]
    by = {s["id"]: s for s in grid["span_candidates"]}
    scenes, windows, facts = [], [], {}
    voice_evidence = []
    for seq in stage2.get("sequences", []):
        for chunk in seq.get("chunks", []):
            start, end = (schemas.parse_ts(chunk["time"][k]) for k in ("start", "end"))
            meanings = chunk.get("meanings") or []
            windows.append({"i": len(windows), "start": start, "end": end,
                            "status": "ok" if meanings else "failed", "synopsis": seq.get("content", "")})
            for meaning in meanings:
                ids = []
                for span in meaning.get("spans", []):
                    sid = span.get("span_id")
                    if sid not in by:
                        raise ValueError(f"v3 adapter: unknown span {sid}")
                    base = by[sid]
                    if overlaps(base["t_in"], base["t_out"], excluded):
                        continue
                    if sid in facts:
                        raise ValueError(f"v3 adapter: duplicate span {sid}")
                    fact = copy.deepcopy(span)
                    # v3 has no reaction/action classification. Do not invent one.
                    fact.update(span_id=sid, kind="ambience", sound="",
                                diegesis=span.get("diegesis", "actual"),
                                has_text=bool(span.get("has_text") or span.get("screen_text")),
                                screen_text_kind=span.get("screen_text_kind", "기타"))
                    facts[sid] = fact
                    ids.append(sid)
                    names = {r.get("speaker") for r in span.get("audio_script", []) if r.get("speaker")}
                    if base["is_audio"]:
                        voice_evidence.append((base["t_in"], base["t_out"], names))
                if ids:
                    scenes.append({"span_ids": ids, "start": min(by[i]["t_in"] for i in ids),
                                   "end": max(by[i]["t_out"] for i in ids), "place": "",
                                   "summary": meaning.get("content", ""), "chars": meaning.get("characters", []),
                                   "mood": meaning.get("mood"), "importance": meaning.get("importance")})
    excluded.sort()
    speakers, ambiguous = {}, []
    for line in transcript["lines"]:
        if overlaps(line["start"], line["end"], excluded):
            continue
        names = set()
        for start, end, heard in voice_evidence:
            if overlaps(line["start"], line["end"], [(start, end)]):
                names.update(heard)
        names.discard("미상")
        if len(names) == 1:
            speakers[line["id"]] = next(iter(names))
        elif len(names) > 1:
            ambiguous.append(line["id"])
    scenes.sort(key=lambda s: s["start"])
    for i, scene in enumerate(scenes, 1):
        scene["id"] = f"SC-{i:03d}"
    moments = []
    for base in grid["span_candidates"]:
        if base["id"] not in facts or base["is_audio"]:
            continue
        fact = facts[base["id"]]
        moments.append({**fact, "id": f"S-{len(moments)+1:03d}", "span_ids": [base["id"]],
                        "start": base["t_in"], "end": base["t_out"], "desc": fact.get("scene_script", ""),
                        "who": ", ".join(fact.get("characters") or []) or None})
    return {"title": title, "cast": cast, "speakers": speakers, "scenes": scenes, "moments": moments,
            "windows": windows, "grid_facts": facts, "grid_fingerprint": fp,
            "analysis_backend": SCHEMA, "analysis_excluded_ranges": excluded,
                            "ambiguous_speaker_lines": ambiguous, "coverage": stage2.get("coverage", {}),
            "v3_stage2": copy.deepcopy(stage2)}


def build_index(job, gemini, transcript, proxy, info, cuts, *, title, cast, get_v3,
                research_context="", force=False, retry_failed=False,
                skip_broadcast_text=False, reuse_analysis=False):
    from app.v3 import seq_analyze, chunk_analyze, refine, chunk_split, text_policy, screen_text
    from app.v3.audio import detect_silence_intervals, load_pcm
    from app.v3.arousal import compute_arousal
    from app.v3.pipeline import _run_m2

    audio_fp = fingerprint([source_identity(job.source), transcript["words"]])
    measured = job.load("grid_audio.json") if job.has("grid_audio.json") else {}
    if measured.get("fingerprint") != audio_fp:
        audio = job.path("audio_16k.wav")
        words = build_grid(info, transcript, cuts)["words"]
        measured = {"fingerprint": audio_fp, "silence": detect_silence_intervals(audio, info["duration_sec"]),
                    "arousal": compute_arousal(load_pcm(audio), info["duration_sec"], words)}
        job.save("grid_audio.json", measured)
    grid = build_grid(info, transcript, cuts, silence=measured["silence"], arousal=measured["arousal"])
    if reuse_analysis:
        if force or retry_failed:
            raise ValueError("--reuse-analysis는 분석 재생성·실패 재시도와 함께 사용할 수 없습니다")
        if not job.has("grid.json") or not job.has("index.json") or job.load("grid.json") != grid:
            raise ValueError("저장된 분석의 시간 격자·전사가 현재 입력과 다릅니다")
        cached_index = job.load("index.json")
        if cached_index.get("analysis_backend") != SCHEMA or cached_index.get("title") != title or cached_index.get("cast") != cast:
            raise ValueError("저장된 분석의 작품·인물·백엔드가 다릅니다")
        job.record_step("analysis_reuse", fingerprint=cached_index["grid_fingerprint"],
                        mode="explicit_saved_analysis", grid_verified=True)
        job.log("[v3/index] 저장된 영상 분석 재사용 — 시간 격자·전사 일치, 영상 분석 호출 0")
        return cached_index, grid
    client = get_v3()
    fp = fingerprint([SCHEMA, grid, source_identity(job.source), research_context, cast,
                      [(l["id"], l["text"]) for l in transcript["lines"]],
                      client.config.model_name, client.config.flash_model_name,
                      client.config.analysis_thinking_level,
                      *(["essential_text", Path(text_policy.__file__).read_text(),
                         Path(screen_text.__file__).read_text()] if skip_broadcast_text else []),
                      [Path(m.__file__).read_text() for m in (seq_analyze, chunk_analyze, chunk_split, refine)]])
    work = job.path(f"v3_analysis/{fp}")
    if force and work.exists():
        work.rename(work.with_name(work.name + f".prev_{time.time_ns()}"))
    work.mkdir(parents=True, exist_ok=True)
    cache = Job(job.source, work, title)
    saved = job.load("grid_index.json") if job.has("grid_index.json") else {}
    if not force and not retry_failed and saved.get("fingerprint") == fp and cache.has("stage2.json"):
        index = saved["index"]
        if job.has("index.json") and job.load("index.json").get("grid_fingerprint") == fp:
            index = job.load("index.json")
    else:
        if job.has("voice_check.json"):
            job.path("voice_check.json").rename(job.path(f"voice_check.json.prev_{time.time_ns()}"))
        def step(name, **kwargs):
            job.record_step(name, **kwargs)
        scan = job.path("v3_scan_480p.mp4")
        # Source identity is part of the CLI job identity and the analysis fingerprint.
        if not cache.has("stage1.json"):
            seq_analyze.build_scan_proxy(job.source, scan, log=job.log)
            doc, audit = seq_analyze.run_seq_analyze(client, scan, grid, research_context=research_context, log=job.log)
            doc, ra = refine.refine_exception(client, doc, grid, job.source, work / "refine_probes", log=job.log)
            doc, ha = refine.describe_zone_heads(client, doc, job.source, work / "refine_probes",
                                                used_calls=int(ra.get("flash_calls") or 0), log=job.log)
            cache.save("stage1.json", doc)
            cache.save("stage1_audit.json", {"analysis": audit, "refine": ra, "heads": ha})
            step("seq_analyze", audit=audit, refine=ra, heads=ha)
        _run_m2(output_dir=work, video_path=job.source, stage1_path=work / "stage1.json",
                grid=grid, research={"work_context": research_context,
                                    "cast_images": [{"character_name": n} for n in cast]},
                from_step=None, max_chunks=None, get_gemini=get_v3, step=step, log=job.log,
                retry_failed=retry_failed,
                **({"skip_broadcast_text": True} if skip_broadcast_text else {}))
        index = adapt(cache.load("stage2.json"), grid, transcript, title=title, cast=cast, fp=fp)
        if not index["scenes"]:
            raise ValueError("v3 analysis: 성공한 분석 청크가 없습니다. --retry-failed-chunks로 재시도하세요")
        job.save("grid_index.json", {"fingerprint": fp, "index": index})
        job.record_step("grid", backend=SCHEMA, fingerprint=fp, observed=len(index["grid_facts"]),
                        coverage=index["coverage"], ambiguous_speaker_lines=index["ambiguous_speaker_lines"])
    for name in ("stage1.json", "stage2.json", "checkpoint_chunk_split.json", "checkpoint_chunk_analyze.json"):
        job.save(name, cache.load(name))
    for line in transcript["lines"]:
        line["speaker"] = index["speakers"].get(line["id"])
    job.save("transcript.json", transcript)
    job.save("grid.json", grid)
    job.save("index.json", index)
    job.log(f"[v3/index] 장면 {len(index['scenes'])} · 순간 {len(index['moments'])} · 화자 {len(index['speakers'])} · 제외 창 {len(index['analysis_excluded_ranges'])}")
    return index, grid
