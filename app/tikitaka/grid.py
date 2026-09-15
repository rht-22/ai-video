"""Tikitaka material index on the shared, measured v3 grid.

STT remains the dialogue clock. Models return span IDs, never cut timestamps.
The original tikitaka index is kept for --pipeline legacy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time

from app.v3 import timegrid

SCHEMA = "tikitaka_grid/v1"
# Scribe returns some words with start == end (지금불륜 EP01: 73/3182, e.g. 173.19~173.19 「네,」 in the raw
# response). The grid copy gives them this much time, never past the next later-starting word or the source end.
ZERO_WORD_SEC = 0.1


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def source_identity(path) -> dict:
    path = path.resolve()
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def ensure_story_inputs(job, key, *, tag=""):
    """Invalidate only this tag's generated story caches; preserve previous files."""
    suffix = "_" + tag if tag else ""
    name = f"grid_story_inputs{suffix}.json"
    fp = fingerprint(key)
    old = job.load(name) if job.has(name) else {}
    if old.get("fingerprint") != fp:
        patterns = [rf"rebuild{re.escape(suffix)}(?:_raw)?\.json",
                    rf"(?:verified|verify_raw)_v\d+{re.escape(suffix)}\.json"]
        for path in list(job.out_dir.glob("*.json")):
            if any(re.fullmatch(pattern, path.name) for pattern in patterns):
                path.rename(path.with_name(path.name + f".prev_{time.time_ns()}"))
                job.log(f"[grid/cache] 재료 변경 → {path.name} 이전본 보존 후 재생성")
        job.save(name, {"fingerprint": fp})


def build_grid(info: dict, transcript: dict, cuts: list[float], *, silence=None, arousal=None) -> dict:
    duration = float(info["duration_sec"])
    words = [{"t0": float(w["start"]), "t1": float(w["end"]),
              "text": str(w["text"]), "prob": w.get("p", w.get("prob", 0))}
             for w in transcript["words"]]
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("grid: invalid source duration")
    for w in words:
        if not (math.isfinite(w["t0"]) and math.isfinite(w["t1"])
                and 0 <= w["t0"] <= w["t1"] <= duration + .05 and w["t0"] < duration):
            raise ValueError(f"grid: invalid STT word timing: {w}")
    words.sort(key=lambda w: (w["t0"], w["t1"]))
    starts = sorted({w["t0"] for w in words})
    padded = 0
    for w in words:
        if w["t1"] == w["t0"]:
            later = next((t for t in starts if t > w["t0"]), duration)
            w["t1"] = round(min(w["t0"] + ZERO_WORD_SEC, later, duration), 3)
            padded += 1
    cuts = sorted({float(t) for t in cuts if math.isfinite(float(t)) and 0 < float(t) < duration})
    spans = timegrid.carve_spans(words, cuts, silence or [], duration)
    return timegrid.build_grid_doc(
        source=dict(info), words=words, scene_cuts=cuts, silence=silence or [], arousal=arousal or [],
        span_candidates=spans,
        transcript_meta={"backend": transcript.get("backend"), "model": transcript.get("model"),
                         "authority": "tikitaka_stt", "schema": SCHEMA,
                         "zero_length_words_padded": padded})


PROMPT = """작품 {title}의 영상 인덱스를 작성하라. 시각 정본은 아래 grid 표다.
영상은 원본 {start}초에서 잘린 창이다. 표 시각은 이 창 기준이다.
시간을 새로 쓰지 말고 표의 span_id와 L- 대사 ID만 반환하라.
화면에서 실제로 확인한 것만 기록. 화면 밖 화자를 화면에 있는 인물이라고 쓰지 마라.
screen_text는 화면 글자 원문(300자), has_text는 글자 유무,
screen_text_kind는 메시지/기사/댓글/문서/검색/기타,
diegesis는 actual/imagined/recalled/unclear, importance는 1~5,
subject_pos는 left/center/right. 불명확하면 unclear 또는 빈 값.
소품·자료화면·청자의 반응도 빠뜨리지 말 것. 내레이션이 인용할 글자는 원문으로.
출력 JSON:
{{"speakers": {{"L-001":"인물"}},
 "scenes":[{{"span_ids":["sp0000"],"place":"장소","summary":"사건","chars":["인물"]}}],
 "spans":[{{"span_id":"sp0000","scene_script":"보이는 행동·표정·소품",
 "characters":["화면에 보이는 인물"],"importance":4,"kind":"reaction|action|ambience",
 "sound":"현장음","has_text":false,"screen_text":"","screen_text_kind":"기타",
 "diegesis":"actual","is_claim":false,"subject_pos":"center"}}]}}
grid:
{spans}
대사:
{lines}
"""


def parse_observations(raw: dict, spans: list[dict], line_ids: set[str]) -> dict:
    """Bind only known IDs; unknown/malformed observations have an audit exit."""
    if not isinstance(raw, dict):
        raise ValueError("grid index: response must be an object")
    by = {s["id"]: s for s in spans}
    facts, issues = {}, []
    for row in raw.get("spans") or []:
        if not isinstance(row, dict) or not isinstance(row.get("span_id"), str) or row["span_id"] not in by:
            issues.append("unknown span observation dropped")
            continue
        sid = row["span_id"]
        if sid in facts:
            issues.append(f"{sid}: duplicate observation dropped")
            continue
        fact = {"span_id": sid, "scene_script": str(row.get("scene_script") or "")[:500],
                "characters": [str(x) for x in row.get("characters", []) if isinstance(x, str)]
                if isinstance(row.get("characters"), list) else [],
                "importance": row.get("importance") if type(row.get("importance")) is int
                and 1 <= row["importance"] <= 5 else 3,
                "screen_text": str(row.get("screen_text") or "")[:300],
                "has_text": row.get("has_text") is True,
                "screen_text_kind": row.get("screen_text_kind") if row.get("screen_text_kind")
                in {"메시지", "기사", "댓글", "문서", "검색", "기타"} else "기타",
                "diegesis": row.get("diegesis") if row.get("diegesis")
                in {"actual", "imagined", "recalled", "unclear"} else "unclear",
                "kind": row.get("kind") if row.get("kind") in {"reaction", "action", "ambience"} else "ambience",
                "sound": str(row.get("sound") or ""), "is_claim": row.get("is_claim") is True}
        if row.get("subject_pos") in {"left", "center", "right"}:
            fact["subject_pos"] = row["subject_pos"]
        facts[sid] = fact
    scenes = []
    for row in raw.get("scenes") or []:
        ids = row.get("span_ids") if isinstance(row, dict) else None
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or x not in by for x in ids):
            issues.append("scene with unknown/empty span IDs dropped")
            continue
        scenes.append({"start": min(by[x]["t_in"] for x in ids),
                       "end": max(by[x]["t_out"] for x in ids), "span_ids": list(dict.fromkeys(ids)),
                       "place": str(row.get("place") or ""), "summary": str(row.get("summary") or ""),
                       "chars": row.get("chars") if isinstance(row.get("chars"), list) else []})
    speakers = {k: v.strip() for k, v in (raw.get("speakers") or {}).items()
                if k in line_ids and isinstance(v, str) and v.strip()}
    return {"facts": facts, "scenes": scenes, "speakers": speakers, "issues": issues}


def stage2_document(grid: dict, facts: dict) -> dict:
    """Shared observation schema, honestly tagged as tikitaka grid observations."""
    meanings = []
    for s in grid["span_candidates"]:
        f = facts.get(s["id"])
        if f is None:
            continue
        span = {**copy.deepcopy(f), "time": {"start": s["t_in"], "end": s["t_out"]},
                "is_audio": s["is_audio"], "time_authority": s["time_authority"],
                "audio_script": [], "heard_text": ""}
        meanings.append({"number": len(meanings), "time": span["time"],
                         "content": f["scene_script"], "characters": f["characters"],
                         "importance": f["importance"], "spans": [span]})
    return {"observation_source": SCHEMA,
            "sequences": [{"chunks": [{"meanings": meanings}]}]}


def build_index(job, gemini, transcript, proxy, info, cuts, *, title, cast, get_v3,
                research_context: str = ""):
    from app.tikitaka.index import make_windows, _cut_window_clip, _lines_block
    from app.v3.screen_text import run_screen_text_pass
    from app.v3.audio import detect_silence_intervals, load_pcm
    from app.v3.arousal import compute_arousal
    audio_fp = fingerprint([source_identity(job.source), transcript["words"]])
    measured = job.load("grid_audio.json") if job.has("grid_audio.json") else {}
    grid = build_grid(info, transcript, cuts)
    if measured.get("fingerprint") != audio_fp:
        audio = job.path("audio_16k.wav")
        measured = {"fingerprint": audio_fp,
                    "silence": detect_silence_intervals(audio, info["duration_sec"]),
                    "arousal": compute_arousal(load_pcm(audio), info["duration_sec"], grid["words"])}
        job.save("grid_audio.json", measured)
    grid = build_grid(info, transcript, cuts, silence=measured["silence"], arousal=measured["arousal"])
    fp = fingerprint([SCHEMA, PROMPT, research_context, cast, getattr(gemini, "video_model", None), source_identity(job.source), grid,
                      [(l["id"], l["text"]) for l in transcript["lines"]]])
    saved = job.load("grid_index.json") if job.has("grid_index.json") else {}
    if saved.get("fingerprint") == fp:
        data = saved["index"]
        if job.has("index.json") and job.load("index.json").get("grid_fingerprint") == fp:
            data = job.load("index.json")  # retain verified/diarized speaker corrections
        job.save("index.json", data)
        job.save("grid.json", grid)
        job.save("stage2.json", stage2_document(grid, data["grid_facts"]))
        for line in transcript["lines"]:
            line["speaker"] = data["speakers"].get(line["id"])
        job.save("transcript.json", transcript)
        return data, grid
    # 새 인물 정보로 관찰을 만들 때, 옛 관찰에 대한 화자 다수결을 재사용하지 않는다.
    if job.has("voice_check.json"):
        job.path("voice_check.json").rename(job.path(f"voice_check.json.prev_{time.time_ns()}"))
    facts, scenes, speakers, issues = {}, [], {}, []
    for i, (ws, we) in enumerate(make_windows(info["duration_sec"])):
        spans = [s for s in grid["span_candidates"] if ws <= s["t_in"] < we]
        lines = [l for l in transcript["lines"] if ws <= l["start"] < we]
        if not spans:
            continue
        we = min(info["duration_sec"], max(we, max(s["t_out"] for s in spans)))
        # Reuse clip extraction, with source-dependent names (no stale video windows).
        from app.tikitaka.common import Job
        cache_job = Job(job.source, job.path(f"grid_cache/{fp}"), job.title)
        clip = _cut_window_clip(cache_job, proxy, ws, we, i)
        prompt = PROMPT.format(title=title, start=ws,
            spans="\n".join(f"{s['id']} {s['t_in']-ws:.3f}~{s['t_out']-ws:.3f} {s['text']}" for s in spans),
            lines=_lines_block(lines, ws))
        if research_context:
            prompt += "\n\n" + research_context
        elif cast:
            prompt += "\n등장인물 후보: " + ", ".join(cast) + "\n불확실한 인물은 미상으로 기록하라."
        from app.v3.chunk_analyze import verify_scene_binding, BINDING_REJECT_MIN, binding_problems
        rejection = ""
        for attempt in range(3):
            name = f"grid_cache/{fp}/observations_{i}_{attempt}.json"
            raw = job.load(name) if job.has(name) else gemini.video_json(
                prompt + "\n반려: " + rejection, clip, kind=f"grid_index{i}", fps=1)
            parsed = parse_observations(raw, spans, {l["id"] for l in lines})
            job.save(name, raw)
            if not parsed["facts"] or not parsed["scenes"]:
                rejection = "grid ID에 귀속된 관찰·씬이 없다. 표의 ID를 사용하라."
                continue
            norm = stage2_document(grid, parsed["facts"])["sequences"][0]["chunks"][0]["meanings"]
            binding_name = f"grid_cache/{fp}/binding_{i}_{attempt}.json"
            binding = job.load(binding_name) if job.has(binding_name) else verify_scene_binding(
                get_v3(), job.source, {"start_sec": 0}, norm, spans, log=job.log)
            job.save(binding_name, binding)
            if binding.get("status") == "ok" and binding.get("mismatch", 0) >= BINDING_REJECT_MIN:
                rejection = "\n".join(binding_problems(binding["details"]))
                job.log(f"[grid] 창{i} 사진 대조 반려 {attempt+1}/3: {rejection}")
                continue
            if binding.get("status") != "ok" or binding.get("mismatch"):
                issues.append(f"window {i} binding: {binding}")
            break
        else:
            raise ValueError(f"grid index window {i}: observation review exhausted: {rejection}")
        facts.update(parsed["facts"]); scenes.extend(parsed["scenes"]); speakers.update(parsed["speakers"])
        issues.extend(parsed["issues"])
        job.log(f"[grid] 창{i}: {len(parsed['facts'])}/{len(spans)}개 관찰")
    s2 = stage2_document(grid, facts)
    audit = run_screen_text_pass(get_v3(), s2, job.source, job.out_dir,
                                duration_sec=info["duration_sec"], fingerprint=fp, log=job.log)
    for sq in s2["sequences"]:
        for ch in sq["chunks"]:
            for m in ch["meanings"]:
                for s in m["spans"]:
                    facts[s["span_id"]].update({k: v for k, v in s.items() if k.startswith("screen_text") or k == "has_text"})
    moments = []
    for s in grid["span_candidates"]:
        if s["id"] not in facts:
            issues.append(f"{s['id']}: unobserved; unavailable for visual selection")
            continue
        f = facts[s["id"]]
        moments.append({**f, "id": f"S-{len(moments)+1:03d}", "span_ids": [s["id"]],
                        "start": s["t_in"], "end": s["t_out"], "desc": f["scene_script"],
                        "who": ", ".join(f["characters"]) or None})
    for i, scene in enumerate(scenes, 1):
        scene["id"] = f"SC-{i:03d}"
    data = {"title": title, "cast": cast, "speakers": speakers, "scenes": scenes,
            "moments": moments, "windows": [], "grid_facts": facts, "grid_fingerprint": fp}
    for l in transcript["lines"]:
        l["speaker"] = speakers.get(l["id"])
    job.save("transcript.json", transcript); job.save("index.json", data)
    job.save("stage2.json", s2); job.save("grid.json", grid)
    job.save("grid_index.json", {"fingerprint": fp, "index": data})
    job.record_step("grid", fingerprint=fp, issues=issues, screen_text=audit,
                    spans=len(grid["span_candidates"]), observed=len(facts))
    for issue in issues:
        job.log(f"[grid] {issue}")
    return data, grid
