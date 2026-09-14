"""N/S/A assembly with ID-only visual selection and bounded cover review."""
from __future__ import annotations

import copy
import math
import time
from pathlib import Path

from app.tikitaka.grid import fingerprint, source_identity
from app.tikitaka.table import (rows_from_items, _tts_cached, speech_end_after, cuts_in_excluded,
                                trim_a_rows_off_dialogue, trim_a_rows_off_black)
from app.tikitaka.guide import in_excluded
from app.v3.story_flow.cover import is_info_screen

MAX_REASKS = 2
MAX_STACK = 4
MIN_CUT = .6
MAX_CUT = 2.0
MAX_HOLD = 2.0
FPS = 30
SCHEMA = "tikitaka_grid_table/v2"


def overlap(a, z, ranges):
    return any(min(z, b) > max(a, s) + 1e-6 for s, b in ranges)


def candidates_for(row, rows, index, grid, exclude, used):
    """Same-scene material only; neither invented IDs nor already-used footage."""
    from app.tikitaka.table import lead_scene_for_row
    k = rows.index(row)
    scene = lead_scene_for_row(rows, k, index["scenes"])
    if scene is None:
        return {}  # missing scene evidence must not open the entire episode
    a, z = scene
    by = {s["id"]: s for s in grid["span_candidates"]}
    out = {}
    for sid, fact in index["grid_facts"].items():
        sp = by[sid]
        s, e = sp["t_in"], sp["t_out"]
        if s < a or e > z or e-s < MIN_CUT or overlap(s, e, exclude + used):
            continue
        out[sid] = {**sp, **fact}
    return out


def assemble_cover(ids, candidates, target, *, opening=False):
    if not isinstance(ids, list) or not ids or len(ids) > MAX_STACK:
        raise ValueError(f"덮개 ID는 1~{MAX_STACK}개")
    if any(not isinstance(s, str) or s not in candidates for s in ids) or len(set(ids)) != len(ids):
        raise ValueError("덮개 ID가 후보 밖이거나 중복")
    if opening and candidates[ids[0]]["importance"] < 4:
        raise ValueError("훅 첫 화면은 중요도 4 이상")
    remaining = math.ceil(target * FPS - 1e-7) / FPS
    cuts = []
    for sid in ids:
        if remaining < 1 / FPS - 1e-7:
            break
        sp = candidates[sid]
        # Full frame windows inside measured source boundaries. Last partial cut
        # is a TTS-derived trim, not a model timestamp.
        a = math.ceil(sp["t_in"] * FPS - 1e-7) / FPS
        end = math.floor(sp["t_out"] * FPS + 1e-7) / FPS
        sec = min(end-a, MAX_CUT, remaining)
        sec = round(sec * FPS) / FPS
        if sec < MIN_CUT and remaining >= MIN_CUT:
            continue
        if sec < 1 / FPS:
            continue
        cuts.append({"src": sid, "span_ids": [sid], "in": a, "out": a+sec,
                     "dur": sec, "desc": sp["scene_script"], "authority": "grid+tts",
                     "subject_pos": sp.get("subject_pos"), "fact": copy.deepcopy(sp)})
        remaining -= sec
    if remaining > 1e-6 and cuts and remaining <= MAX_HOLD:
        fact = cuts[-1]["fact"]
        if is_info_screen([cuts[-1]["src"]], {cuts[-1]["src"]: fact}):
            cuts[-1]["hold_sec"] = remaining
            cuts[-1]["dur"] += remaining
            remaining = 0
    if remaining > 1e-6:
        raise ValueError(f"덮개가 TTS 길이에 {remaining:.2f}s 부족; 같은 씬의 다른 ID를 선택")
    return cuts


def probe_cover(job, gemini, cut, text, *, key):
    from app.tikitaka.probe import cut_proxy_clip
    name = f"grid_cover_probes/{key}.json"
    if job.has(name):
        return job.load(name)
    clip = cut_proxy_clip(job, cut["in"], cut["out"], f"grid_cover_probes/{key}.mp4")
    prompt = ("이 화면이 내레이션의 인물·행동·소품 설명과 일치하는지 확인하라. "
              "화면 글자를 인용하면 원문이 맞아야 한다. 모순이면 text_matches=false. "
              "시각을 제안하지 마라. JSON {text_matches: boolean, seen: string, reason: string}.\n"
              f"내레이션: {text}\n기록: {cut['desc']}")
    raw = gemini.video_json(prompt, clip, kind="grid_cover_probe", fps=10)
    if not isinstance(raw, dict) or type(raw.get("text_matches")) is not bool:
        raise ValueError("덮개 프로브의 일치 판정 없음")
    job.save(name, raw)
    return raw


def build_table(job, gemini, rebuild, index, transcript, cuts, duration, proxy, *,
                version_n, title, grid, voice="ko_female", speed="normal", exclude=None, black=None, tag="", force=False):
    version = next(v for v in rebuild["versions"] if v["n"] == version_n)
    exclude, black = exclude or [], black or []
    fp = fingerprint([SCHEMA, version, index, transcript, grid, voice, speed,
                      exclude, black, source_identity(job.source)])
    name = f"grid_table_v{version_n}{'_'+tag if tag else ''}.json"
    cached = job.load(name) if job.has(name) else {}
    if not force and cached.get("fingerprint") == fp and all(
            Path(r["tts"]).is_file()
            for r in cached["rows"] if r.get("tts")):
        return cached
    nonce = time.time_ns() if force else None
    rows = rows_from_items(version, index, transcript, cuts, duration,
        lambda text: _tts_cached(job, text, voice, speed),
        end_fn=lambda we, limit: speech_end_after(job, we, limit=limit))
    notes = trim_a_rows_off_dialogue(rows) + trim_a_rows_off_black(rows, black)
    spoken = [(c["in"], c["out"]) for r in rows if r["mode"] == "S" for c in r["cuts"]]
    by = {s["id"]: s for s in grid["span_candidates"]}
    used, audit = [], []
    for row in rows:
        if row["mode"] == "N":
            continue
        for cut in row["cuts"]:
            a, z = max(0, cut["in"]), min(duration, cut["out"])
            if z <= a:
                raise ValueError("빈 대사/현장음 컷")
            cut.update({"in": a, "out": z, "dur": z-a, "authority": "stt" if row["mode"] == "S" else "grid+scene",
                        "span_ids": [sid for sid, sp in by.items() if min(z, sp["t_out"]) > max(a, sp["t_in"])]})
            if row["mode"] == "A" and overlap(a, z, used + spoken + black):
                raise ValueError("현장음 컷이 대사·기존 컷·암전과 중복; 대본을 다시 선택해야 함")
            used.append((a, z))
    for row in rows:
        if row["mode"] != "N":
            continue
        candidates = candidates_for(row, rows, index, grid, exclude + black, used)
        rejection = ""
        for attempt in range(MAX_REASKS + 1):
            prompt = ("내레이션 아래 화면을 골라라. 같은 씬의 아래 후보 ID만 1~4개. "
                      "슬로우 금지, 글자 인용은 기록 원문만, 무관한 화면 금지. "
                      "첫 내레이션이 첫 행이면 첫 화면 중요도 4 이상. "
                      "화면을 이어 TTS 길이를 채운다. 자료화면만 최대 2초 정지 가능. "
                      "JSON {span_ids: [string]}. 시간은 쓰지 마라.\n"
                      f"문장: {row['text']} / 길이: {row['dur']:.3f}s\n"
                      f"앞뒤 대본: {[(r['mode'], r['text']) for r in rows]}\n"
                      f"후보: {list(candidates.values())}\n반려 사유: {rejection}")
            request_key = fingerprint([fp, row["i"], prompt, nonce])
            selection_name = f"grid_cover_probes/{request_key}_selection.json"
            try:
                raw = job.load(selection_name) if job.has(selection_name) else gemini.text_json(prompt, kind="grid_cover_select")
                job.save(selection_name, raw)
                selected = assemble_cover(raw.get("span_ids"), candidates, row["dur"], opening=row is rows[0])
                for cut in selected:
                    result = probe_cover(job, gemini, cut, row["text"], key=fingerprint([request_key, cut]))
                    if not result["text_matches"]:
                        raise ValueError(f"{cut['src']} 모순: {result.get('seen')} / {result.get('reason')}")
                row["cuts"] = selected
                audit.append({"row": row["i"], "attempts": attempt+1, "previous_rejection": rejection})
                break
            except (ValueError, TypeError, AttributeError) as e:
                rejection = str(e)
                job.log(f"[grid/cover] 행{row['i']} {attempt+1}/{MAX_REASKS+1} 반려: {e}")
        else:
            job.save(f"review_v{version_n}{'_'+tag if tag else ''}.json",
                     {"items": [{"kind": "cover", "row": row["i"], "error": rejection}], "blocked": True})
            raise ValueError(f"덮개 재검토 소진: 행{row['i']} {rejection}")
        used.extend((c["in"], c["out"]) for c in row["cuts"])
    bad = cuts_in_excluded(rows, exclude)
    if bad:
        raise ValueError(f"활용 불가 구간: {bad}")
    t = 0.0
    for row in rows:
        row["t0"] = t
        row["dur_video"] = sum(c["dur"] for c in row["cuts"])
        t += row["dur_video"]
    result = {"version": copy.deepcopy(version), "rows": rows, "total_sec": t,
              "voice": voice, "speed": speed, "cut_search": "grid", "fingerprint": fp,
              "cover_review": audit, "notes": notes}
    job.save(name, result)
    job.record_step(f"grid_table_v{version_n}{'_'+tag if tag else ''}", total_sec=t, cover_review=audit)
    return result
