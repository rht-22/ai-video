"""Per-version draft review and shared v3 effects/rendering.

Only this adapter knows N/S/A. The effect, speaker, prop-fit and sound rules
remain in v3; styling cannot change the assembled clock.
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
import shutil
import json
import subprocess
import time
from pathlib import Path

from app.tikitaka.common import Job
from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.tikitaka.grid import fingerprint, source_identity, stage2_document
from app.tikitaka.grid_table import FPS, overlap
from app.v3 import assemble, finalize, stage4, watch_trim

SCHEMA = "tikitaka_review/v1"


def render_draft(video, timeline, out, resources, *, log=print):
    """Review the actual narration as well as the selected original sound."""
    stage4.render_draft(video, timeline, out, log=log)
    cues = resources.get("tts_cue_files") or []
    if not cues:
        return
    cmd = [find_ffmpeg_command("ffmpeg"), "-y", "-v", "error", "-i", str(out)]
    filters, labels = [], ["[0:a]"]
    for i, f in enumerate(cues, 1):
        cmd += ["-i", f["path"]]
        cue = f["cue"]
        delay = round(cue["start_sec"] * 1000)
        filters.append(f"[{i}:a]atrim=duration={cue['duration_sec']:.6f},asetpts=PTS-STARTPTS,"
                       f"adelay={delay}:all=1[t{i}]")
        labels.append(f"[t{i}]")
    filters.append("".join(labels)+f"amix=inputs={len(labels)}:duration=first:normalize=0[a]")
    temp = out.with_name("draft_with_narration.mp4")
    cmd += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(temp)]
    subprocess.run(cmd, check=True, capture_output=True)
    os.replace(temp, out)


def validate_media(path, expected):
    raw = subprocess.check_output([find_ffmpeg_command("ffprobe"), "-v", "error",
        "-show_entries", "stream=codec_type,duration,width,height", "-of", "json", str(path)], text=True)
    streams = json.loads(raw)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    vd, ad = float(video["duration"]), float(audio["duration"])
    if (video["width"], video["height"]) != (1080, 1920) or abs(vd-expected) > .05 + 1e-6 or abs(ad-expected) > .1 + 1e-6:
        raise ValueError(f"render duration/geometry mismatch: expected={expected}, video={video}, audio={ad}")
    return {"video_sec": vd, "audio_sec": ad, "width": video["width"], "height": video["height"]}


def v3_title(title):
    """Adapt existing Tikitaka wording to v3's line1/line2 contract.

    Keep explicit editorial line breaks. For legacy single-line titles, split
    at the most balanced word boundary without dropping or rewriting words;
    shared finalize.fit_title_sizes remains the authority for font sizing.
    """
    if isinstance(title, dict):
        return {k: str(title.get(k) or "").strip() for k in ("line1", "line2")}
    lines = [line.strip() for line in str(title).splitlines() if line.strip()]
    if len(lines) == 2:
        return dict(zip(("line1", "line2"), lines))
    text = " ".join(str(title).split())
    spaces = [i for i, c in enumerate(text) if c == " "]
    if spaces and len(text) > 12:
        split = min(spaces, key=lambda i: abs(len(text[:i]) - len(text[i+1:])))
        return {"line1": text[:split], "line2": text[split+1:]}
    return {"line1": text, "line2": ""}


def bundle(table, grid, *, title):
    """Table → renderer contract. All offsets use the same 30fps clip lengths."""
    timeline, beats, segments, cue_files = [], [], [], []
    offset = 0.0
    for bi, row in enumerate(table["rows"]):
        row_start = offset
        ids = []
        role = "hook" if bi == 0 else "ending" if bi == len(table["rows"])-1 else "build"
        for cut in row["cuts"]:
            s = float(cut["in"])
            hold = float(cut.get("hold_sec") or 0)
            dur = math.floor((float(cut["out"])-s+hold)*FPS + 1e-6)/FPS
            if dur <= 0:
                raise ValueError("grid adapter: sub-frame clip")
            end = s + dur - hold
            spids = list(cut.get("span_ids") or [])
            ids.extend(spids)
            c = {"clip_start_sec": s, "clip_end_sec": end, "span_ids": spids,
                 "role": role, "beat": bi, "use_original_audio": row["mode"] != "N",
                 "subtitle": "", "time_authority": cut["authority"]}
            if hold:
                c["hold_sec"] = hold
            if row["mode"] == "N":
                c["cover"] = True
            if cut.get("subject_pos") in {"left", "right"}:
                c["subject_pos"] = cut["subject_pos"]
            if cut.get("reframe"):
                c["reframe"] = copy.deepcopy(cut["reframe"])
            timeline.append(c)
            if row["mode"] == "S":
                for sub in row.get("sub_lines") or []:
                    a, z = max(s, sub["start"]), min(end, sub["end"])
                    if z > a:
                        segments.append({"start_sec": offset+a-s, "end_sec": offset+z-s,
                                         "source_time_sec": a, "text": sub["text"],
                                         "speaker": row.get("speaker") or "미상"})
            offset += dur
        if not ids:
            raise ValueError(f"row {row['i']}: no grid provenance")
        beats.append({"number": bi, "span_ids": list(dict.fromkeys(ids)), "role": role,
                      "action": row["text"], "time": {"start": row["cuts"][0]["in"],
                                                       "end": row["cuts"][-1]["out"]}, "labels": []})
        if row["mode"] == "N":
            if offset-row_start + 1e-6 < row["dur"]:
                raise ValueError("TTS audio exceeds assembled cover")
            cue = {"text": row["text"], "start_sec": row_start, "end_sec": row_start+row["dur"],
                   "duration_sec": row["dur"], "fit_actual_sec": row["dur"], "beat": bi,
                   "source_time_sec": row["cuts"][0]["in"], "source_end_sec": row["cuts"][-1]["out"],
                   "muted_span_ids": list(dict.fromkeys(ids))}
            cue_files.append({"cue": cue, "path": str(Path(row["tts"]).resolve())})
    headline = v3_title(table["version"]["title"])
    plan = {"timeline": timeline, "source_fps": FPS, "output_fps": FPS,
            "layout": {"top_title": "\n".join(v for v in headline.values() if v), "bottom_label": title},
            "audio_mix": {"original_gain_db": -3, "tts_gain_db": -3}}
    story = {"beats": beats, "title": headline,
             "narration_cues": [f["cue"] for f in cue_files], "template": "tikitaka"}
    return plan, story, segments, {"tts_cue_files": cue_files}


def validate_bundle(plan, grid, segments, resources, *, exclude=()):
    ids = {s["id"] for s in grid["span_candidates"]}
    total = sum(assemble.clip_len(c) for c in plan["timeline"])
    if not plan["timeline"]:
        raise ValueError("review removed all clips")
    for c in plan["timeline"]:
        a, z = c["clip_start_sec"], c["clip_end_sec"]
        if not 0 <= a < z <= grid["source"]["duration_sec"] + 1e-6:
            raise ValueError("source bounds violation")
        if not c.get("span_ids") or not set(c["span_ids"]) <= ids:
            raise ValueError("unknown grid provenance")
        if overlap(a, z, exclude):
            raise ValueError("excluded footage in reviewed output")
    for s in segments:
        if not 0 <= s["start_sec"] < s["end_sec"] <= total + 1e-6:
            raise ValueError("subtitle outside edited timeline")
    for f in resources["tts_cue_files"]:
        c = f["cue"]
        if not 0 <= c["start_sec"] < c["end_sec"] <= total + 1e-6:
            raise ValueError("TTS outside edited timeline")
        if not Path(f["path"]).is_file():
            raise FileNotFoundError(f["path"])
    return {"clips": len(plan["timeline"]), "duration_sec": total, "grid_ids_valid": True,
            "excluded_violations": 0, "subtitle_lines": len(segments), "tts_cues": len(resources["tts_cue_files"])}


def render_dependencies():
    """Include code + bundled assets; changing SFX rules must invalidate render."""
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "v3").rglob("*.py")) + list((root / "modules").glob("*.py"))
    paths += list((root / "assets/sfx").glob("*"))
    paths += [Path(__file__)]
    return fingerprint([(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
                        for p in sorted(paths) if p.is_file()])


def label_facts(timeline, facts):
    return {"screen_clips": {i for i, c in enumerate(timeline)
                             if any(facts.get(s, {}).get("screen_text") for s in c["span_ids"])},
            "clip_characters": {i: sorted({n for s in c["span_ids"] for n in facts.get(s, {}).get("characters", [])})
                                for i, c in enumerate(timeline)}, "register": []}


def run(job, table, grid, index, *, get_gemini, design=None, redo=False, force_render=False,
        force_style=False, exclude=(), tag="", split_narration=False):
    n = table["version"]["n"]
    suffix = f"v{n}{'_'+tag if tag else ''}"
    work = Job(job.source.resolve(), job.path(f"review_{suffix}").resolve(), job.title)
    work.out_dir.mkdir(parents=True, exist_ok=True)
    design = design or {}
    if job.has("transcript.json"):
        from app.tikitaka.subtitles import refresh_table
        transcript = job.load("transcript.json")
        if transcript.get("words") and transcript.get("lines"):
            table = refresh_table(table, transcript)
            job.save(f"subtitle_table_{suffix}.json", table)
    initial = bundle(table, grid, title=job.title)
    review_material = copy.deepcopy(initial)
    review_material[0].pop("layout", None)
    # Draft is already 30fps; this option only fixes the final renderer.
    review_material[0].pop("output_fps", None)
    review_material[1].pop("title", None)
    for clip in review_material[0]["timeline"]:
        clip.pop("reframe", None)
    input_fp = fingerprint([SCHEMA, review_material, grid, index["grid_facts"], source_identity(job.source),
                            [(f["path"], hashlib.sha256(Path(f["path"]).read_bytes()).hexdigest()) for f in initial[3]["tts_cue_files"]]])
    prior = work.load("checkpoint_review.json") if work.has("checkpoint_review.json") else {}
    work.save("grid.json", grid)
    work.save("stage2.json", index.get("v3_stage2") or stage2_document(grid, index["grid_facts"]))
    # Same media identity the existing speaker/letterbox code consumes.
    if not job.has("picture_area.json"):
        from app.v3.letterbox import detect_or_full
        picture = detect_or_full(job.source, width=grid["source"]["width"],
                                 height=grid["source"]["height"], log=job.log)
        job.save("picture_area.json", picture)
    work.save("checkpoint_probe.json", {**grid["source"], "picture": job.load("picture_area.json")})
    if prior.get("fingerprint") == input_fp and not prior.get("audit", {}).get("error") and not redo and work.has("draft_480.mp4"):
        plan, story, segments, resources = prior["bundle"]
        audit = prior["audit"]
        work.log("[review] 같은 편의 재관찰 결과 재사용")
    else:
        plan, story, segments, resources = initial
        validate_bundle(plan, grid, segments, resources, exclude=exclude)
        draft = work.path("draft_480.mp4")
        if prior.get("fingerprint") != input_fp or not draft.exists() or redo:
            render_draft(job.source, plan["timeline"], draft, resources, log=work.log)
        from app.v3.story import build_span_index
        span_index, _ = build_span_index(work.load("stage2.json"), grid)
        # Lip-sync lines are protected. Review can remove pauses and irrelevant
        # visual inserts, never half a spoken line or synthesized narration.
        importance = {s["id"]: (s["is_audio"], 4 if s["is_audio"] else index["grid_facts"].get(s["id"], {}).get("importance", 3))
                      for s in grid["span_candidates"]}
        cuts, audit = watch_trim.run_watch_trim(get_gemini(), draft, timeline=plan["timeline"],
            grid=grid, resources=resources, segments=segments, importance=importance, span_index=span_index, opening_focus=True, log=work.log)
        guards = watch_trim.protected_intervals(plan["timeline"], resources, importance, grid)
        cuts = [{**c, "start": math.ceil(c["start"]*FPS-1e-6)/FPS,
                 "end": math.floor(c["end"]*FPS+1e-6)/FPS} for c in cuts]
        cuts = [c for c in cuts if c["end"] > c["start"]]
        cuts = watch_trim.absorb_slivers(cuts, plan["timeline"], guards)
        # Re-check cached/helper output against protected intervals before mutation.
        for cut in cuts:
            if overlap(cut["start"], cut["end"], [(a,z) for a,z,_ in guards]):
                raise ValueError("review cut overlaps protected dialogue/TTS")
        if cuts:
            plan["timeline"] = watch_trim.apply_cuts_to_timeline(plan["timeline"], cuts, grid, set())
            total = sum(assemble.clip_len(c) for c in plan["timeline"])
            segments = watch_trim.remap_segments(segments, cuts, total)
            resources = watch_trim.remap_resources(resources, cuts, total)
            render_draft(job.source, plan["timeline"], draft, resources, log=work.log)
        audit["applied"] = cuts
        validate_bundle(plan, grid, segments, resources, exclude=exclude)
        work.save("checkpoint_review.json", {"fingerprint": input_fp,
            "bundle": [plan, story, segments, resources], "audit": audit})
    # Appearance-only edits cannot trigger a different trim or resurrect an old
    # title from the reviewed checkpoint. Reapply them after the clock is fixed.
    plan["output_fps"] = FPS
    plan["layout"] = copy.deepcopy(initial[0]["layout"])
    story["title"] = copy.deepcopy(initial[1]["title"])
    for c in plan["timeline"]:
        c.pop("reframe", None)
        original = next((x for x in initial[0]["timeline"] if x["beat"] == c["beat"]
                         and x["clip_start_sec"] <= c["clip_start_sec"]+1e-6
                         and x["clip_end_sec"] >= c["clip_end_sec"]-1e-6), None)
        if original and original.get("reframe"):
            c["reframe"] = copy.deepcopy(original["reframe"])
    if split_narration:
        from app.tikitaka.subtitles import narration_captions
        resources["tts_caption_segments"] = narration_captions(job, resources)
        work.save("tts_caption_segments.json", resources["tts_caption_segments"])
    work.save("edit_plan.json", plan); work.save("subtitle_segments.json", segments)
    work.save("checkpoint_story.json", {"story": story}); work.save("checkpoint_resources.json", resources)
    preset = finalize.merge_channel_preset(stage4.get_style_preset("drama_clip"), design)
    style_fp = fingerprint([input_fp, plan, segments, preset, index["grid_facts"],
                            hashlib.sha256(Path(stage4.__file__).read_bytes()).hexdigest()])
    saved = work.load("checkpoint_style.json") if work.has("checkpoint_style.json") else {}
    if saved.get("fingerprint") == style_fp and not redo and not force_style:
        style_doc = saved["style"]
    else:
        # Use explicit beat ownership: repeated grid spans must not assign a later
        # callback to its first occurrence's beat.
        windows, off = [], 0.0
        for c in plan["timeline"]:
            z = off + assemble.clip_len(c)
            if windows and windows[-1]["beat"] == c["beat"]:
                windows[-1]["end"] = z
            else:
                windows.append({"beat": c["beat"], "start": off, "end": z})
            off = z
        style_doc, style_audit = stage4.run_style(get_gemini(), work.path("draft_480.mp4"), story,
            preset=preset, windows=windows, labels=[], dialogue=segments, duration=off,
            band=finalize.video_band_ratio(finalize.design_from_style(preset)), timeline=plan["timeline"],
            label_facts=label_facts(plan["timeline"], index["grid_facts"]), log=work.log)
        work.save("checkpoint_style.json", {"fingerprint": style_fp, "style": style_doc, "audit": style_audit})
    assets = [(k, hashlib.sha256(Path(v).read_bytes()).hexdigest()) for k,v in design.items()
              if k in {"work_value", "platform_image", "title_font", "subtitle_font"}
              and isinstance(v, str) and Path(v).is_file()]
    render_fp = fingerprint([input_fp, plan, segments, resources, style_doc, design, assets, render_dependencies()])
    saved = work.load("render_fingerprint.json") if work.has("render_fingerprint.json") else {}
    final = work.path("final_1080x1920.mp4")
    if saved.get("fingerprint") != render_fp or not final.exists() or redo or force_render or force_style:
        if final.exists():
            final.rename(work.path(f"final_prev_{time.time_ns()}.mp4"))
        final, render_audit = finalize.render_final(video_path=job.source, plan=plan, style_doc=style_doc,
            segments=segments, resources=resources, story_doc=story, output_dir=work.out_dir,
            channel_design=design, muted_gain_db=None, style_preset="drama_clip", log=work.log)
        work.record_step("render", **render_audit)
    validation = validate_bundle(plan, grid, segments, resources, exclude=exclude)
    validation["media"] = validate_media(final, validation["duration_sec"])
    work.save("render_fingerprint.json", {"fingerprint": render_fp})
    issues = [{"kind": "script", "detail": x} for x in table["version"].get("issues", [])]
    issues += [{"kind": "diegesis", "span": sid, "value": index["grid_facts"][sid]["diegesis"]}
               for sid in sorted({s for c in plan["timeline"] for s in c["span_ids"]})
               if sid in index["grid_facts"] and index["grid_facts"][sid].get("diegesis") != "actual"]
    if audit.get("error"):
        issues.append({"kind": "watch_trim", "detail": audit["error"]})
    review = {"schema": SCHEMA, "items": issues, "watch_trim": audit, "validation": validation,
              "output": str(final), "render_fingerprint": render_fp}
    work.save("review.json", review); job.save(f"review_{suffix}.json", review)
    output = job.path(f"shorts_{suffix}.mp4")
    temp = output.with_suffix(".tmp.mp4")
    shutil.copy2(final, temp); os.replace(temp, output)
    job.record_step(f"review_{suffix}", **validation, review=str(work.path("review.json")))
    return output
