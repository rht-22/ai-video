"""N/S/A assembly with ID-only visual selection and bounded cover review."""
from __future__ import annotations

import copy
import math
import time
from pathlib import Path

from app.tikitaka.grid import fingerprint, source_identity
from app.tikitaka.table import (rows_from_items, _tts_cached, speech_end_after, cuts_in_excluded,
                                trim_a_rows_off_dialogue, trim_a_rows_off_black)
from app.tikitaka.timing import narration_plan_sec
from app.v3.story_flow.cover import is_info_screen

MAX_REASKS = 2
MAX_STACK = 4
MIN_CUT = .6
MAX_CUT = 2.0
MAX_HOLD = 2.0
FPS = 30
SCHEMA = "tikitaka_grid_table/v7_joint_plan"
PROBE_SCHEMA = "visual_consistency/v5_shared_semantics"
MAX_NARRATION_REWRITES = 3


class NarrationFitError(ValueError):
    """A chosen lead-in must not silently turn into hold/stack elsewhere."""


def before_dialogue_candidates(row, rows, index, grid, scene_cuts, blocked):
    """The unused, contiguous last shot immediately before the next S row.

    Trim only at measured scene/STT/blocked boundaries, never model timestamps.
    Partial spans are permitted so dialogue head padding cannot hide the lead-in.
    """
    k = rows.index(row)
    if k + 1 >= len(rows) or rows[k+1]["mode"] != "S":
        return {}
    anchor = rows[k+1]["cuts"][0]["in"]
    end = math.floor(anchor * FPS + 1e-7) / FPS
    scene = next((s for s in index.get("scenes", []) if s["start"] < anchor <= s["end"]), None)
    if scene is None:
        return {}
    start = max(float(scene["start"]), end - MAX_STACK * MAX_CUT,
                max((t for t in scene_cuts if t < anchor - 1/FPS), default=0))
    for a, z in blocked:
        if a < end and z > start:
            start = max(start, z)
    start = math.ceil(start * FPS - 1e-7) / FPS
    found = {}
    cursor = end
    for sp in sorted(grid["span_candidates"], key=lambda s: s["t_out"], reverse=True):
        if sp["t_in"] >= cursor or sp["t_out"] <= start:
            continue
        fact = index["grid_facts"].get(sp["id"])
        if fact is None or cursor - sp["t_out"] > 1/FPS + 1e-6:
            break
        a = max(start, math.ceil(sp["t_in"] * FPS - 1e-7) / FPS)
        z = min(cursor, math.floor(sp["t_out"] * FPS + 1e-7) / FPS)
        if z-a < 1/FPS - 1e-7:
            continue
        found[sp["id"]] = {**sp, **fact, "t_in": a, "t_out": z}
        cursor = a
        if a <= start or len(found) == MAX_STACK:
            break
    return dict(reversed(list(found.items())))


def before_dialogue_windows(candidates):
    """The same bounded, backwards windows used for both fit and assembly."""
    windows = []
    for sid, sp in reversed(list(candidates.items())):
        end = sp["t_out"]
        while end - sp["t_in"] >= 1/FPS - 1e-7:
            sec = min(MAX_CUT, end-sp["t_in"])
            windows.append((sid, sp, end-sec, end))
            if len(windows) == MAX_STACK:
                return windows
            end -= sec
    return windows


def assemble_before_dialogue(candidates, target, *, opening=False):
    """Back-fill from the dialogue boundary; no hold, speed change or new shot."""
    remaining = math.ceil(target * FPS - 1e-7) / FPS
    result = []
    for sid, sp, start, end in before_dialogue_windows(candidates):
        if remaining < 1/FPS - 1e-7:
            break
        sec = min(end-start, remaining)
        result.append({"src": sid, "span_ids": [sid], "in": end-sec, "out": end,
                       "dur": sec, "authority": "grid+tts", "desc": sp["scene_script"],
                       "subject_pos": sp.get("subject_pos"), "fact": copy.deepcopy(sp)})
        remaining -= sec
    if remaining > 1e-6 or not result:
        raise NarrationFitError("내레이션이 고정한 대사 앞 장면의 컷 수·길이 한도보다 길다")
    result.reverse()
    if opening and result[0]["fact"]["importance"] < 4:
        raise NarrationFitError("훅 첫 화면은 중요도 4 이상이어야 한다")
    return result


def fit_before_dialogue(job, gemini, row, candidates, rows, *, voice, speed, key, guide=None,
                        budget_sec=None, placement="before_dialogue"):
    """Rewrite then re-synthesize within a fixed source window, transactionally."""
    from app.tikitaka.production import SEMANTIC_RULES, narration_context
    context = narration_context(rows, row)
    budget = (sum(z-a for _, _, a, z in before_dialogue_windows(candidates))
              if budget_sec is None else budget_sec)
    if not math.isfinite(budget) or budget < 1 / FPS:
        raise NarrationFitError("내레이션을 넣을 유효한 화면 길이 없음")
    original = row["text"]
    candidate_text, measured = original, float(row["dur"])
    if not math.isfinite(measured) or measured <= 0:
        raise NarrationFitError("잘못된 TTS 실측 길이")
    audit = {"kind": placement, "budget_sec": budget, "original_text": original,
             "original_sec": measured, "span_ids": list(candidates), "attempts": []}
    attempt_file = f"narration_fit/{key}.json"
    if math.ceil(measured * FPS - 1e-7) / FPS <= budget + 1e-6:
        return audit
    for attempt in range(MAX_NARRATION_REWRITES):
        # Character count is guidance only; actual synthesized duration decides.
        hint = max(1, int(len(candidate_text.replace(" ", "")) * budget / measured * .85))
        prompt = ("선택된 고정 화면에 넣을 한국어 내레이션을 다시 써라. "
                  "장면 길이가 우선이다. 핵심 인물·행동·다음 대사로 이어지는 의미를 유지하고 군더더기를 빼라. "
                  "새 사실·인물·시간 단정은 추가하지 말고, 화면 글자는 기록된 원문만 인용하라. "
                  "문장을 기계적으로 잘라내지 말고 자연스러운 짧은 문장으로 다시 써라. "
                  "속도 변경·정지·다른 장면 추가로 길이를 늘릴 수 없다. "
                  'JSON {"text": "새 문장"}.\n'
                  f"원문: {original}\n직전 시도: {candidate_text} ({measured:.3f}초)\n"
                  f"허용 길이: {budget:.3f}초 / 공백 제외 약 {hint}자 이내 권장\n"
                  f"문장 역할·인접 대본: {context}\n" + SEMANTIC_RULES + "\n"
                  f"고정 화면: {list(candidates.values())}\n제작 가이드: {guide or {}}")
        if audit["attempts"]:
            prompt += f"\n직전 검수: {audit['attempts'][-1]}"
        name = f"narration_fit/{fingerprint([key, attempt, prompt])}_rewrite.json"
        raw = job.load(name) if job.has(name) else gemini.text_json(prompt, kind="grid_narration_fit")
        job.save(name, raw)
        text = raw.get("text") if isinstance(raw, dict) else None
        if not isinstance(text, str) or not text.strip():
            audit["attempts"].append({"error": "재작성 문장 없음"})
            job.save(attempt_file, audit)
            continue
        candidate_text = text.strip()
        path, measured = _tts_cached(job, candidate_text, voice, speed)
        measured = float(measured)
        if not math.isfinite(measured) or measured <= 0:
            raise NarrationFitError("재합성 TTS 길이가 유효하지 않다")
        entry = {"text": candidate_text, "measured_sec": measured, "tts": str(path)}
        audit["attempts"].append(entry)
        if math.ceil(measured * FPS - 1e-7) / FPS > budget + 1e-6:
            entry["error"] = "고정 화면 길이 초과"
            job.save(attempt_file, audit)
            continue
        check_prompt = ("내레이션 재작성 검수. 짧아진 문장이 원문의 핵심 인물·행동·다음 대사로의 연결을 "
                        "보존하고 새 사실·시간 단정·가이드 위반을 추가하지 않았는지 판단하라. "
                        'JSON {"meaning_preserved": boolean, "reason": string}.\n'
                        f"원문: {original}\n재작성: {candidate_text}\n"
                        f"문장 역할·인접 대본: {context}\n" + SEMANTIC_RULES + "\n"
                        f"화면: {list(candidates.values())}\n제작 가이드: {guide or {}}")
        check_name = f"narration_fit/{fingerprint([key, check_prompt])}_meaning.json"
        verdict = job.load(check_name) if job.has(check_name) else gemini.text_json(check_prompt, kind="grid_narration_fit_check")
        job.save(check_name, verdict)
        entry["meaning_review"] = verdict
        if not isinstance(verdict, dict) or verdict.get("meaning_preserved") is not True:
            entry["error"] = "핵심 의미 보존 실패"
            job.save(attempt_file, audit)
            continue
        row.update(text=candidate_text, tts=str(path), dur=measured,
                   plan_sec=narration_plan_sec(candidate_text), original_text=row.get("original_text", original))
        audit.update(final_text=candidate_text, final_sec=measured)
        job.save(attempt_file, audit)
        job.log(f"[grid/narration_fit] {original!r} → {candidate_text!r} ({measured:.2f}/{budget:.2f}초)")
        return audit
    audit["blocked"] = True
    job.save(attempt_file, audit)
    raise NarrationFitError(f"내레이션 재작성 {MAX_NARRATION_REWRITES}회 소진: {budget:.2f}초 장면에 맞출 수 없음")


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


class CoverDurationError(ValueError):
    def __init__(self, remaining, available):
        super().__init__(f"덮개가 TTS 길이에 {remaining:.2f}s 부족; 같은 씬의 다른 ID를 선택")
        self.available = available


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
        raise CoverDurationError(remaining, sum(c["dur"] for c in cuts))
    return cuts


def probe_cover(job, gemini, cut, text, *, key, context=None):
    from app.tikitaka.probe import cut_proxy_clip
    from app.tikitaka.production import SEMANTIC_RULES
    name = f"grid_cover_probes/{fingerprint([PROBE_SCHEMA, key, context])}.json"
    if job.has(name):
        return job.load(name)
    clip = cut_proxy_clip(job, cut["in"], cut["out"], f"grid_cover_probes/{key}.mp4")
    prompt = ("이 화면이 내레이션의 인물·행동·소품 설명과 일치하는지 확인하라. "
              "화면 글자를 인용하면 원문이 맞아야 한다. "
              "관찰 가능한 인물·행동·소품이 설명과 실제로 충돌하거나 무관한 장면이면 text_matches=false. "
              "단순히 화면에서 입증되지 않는다는 이유로 모순으로 판정하지 마라. "
              "관점 전환·진행 안내(예: 누구 시점 보시죠)·의견은 화면 속 글자나 행동으로 재현될 필요가 없다. "
              "기록에는 발언·청취 정보도 포함된다. 발언이 자막이나 소품으로 보이지 않는 것은 시각적 모순이 아니다. "
              "인물 신원이 불확실하면 그 불확실성을 reason에 남기고 일치가 확인됐다고 단정하지 마라. "
              "기록은 후보를 찾기 위한 참고이며 오기나 더 긴 구간의 설명일 수 있다. "
              "text_matches는 내레이션과 실제 선택 화면이 맞는지 판단한다. 기록과의 불일치는 record_matches와 reason에 별도로 남기고 그것만으로 text_matches=false를 주지 마라. "
              "구체 행동을 말하는 내레이션은 그 행동의 직접 장면이어야 한다. 단순히 같은 인물이 등장하는 다른 행동은 불일치다. "
              + SEMANTIC_RULES + "\n"
              "시각을 제안하지 마라. JSON {text_matches: boolean, record_matches: boolean, narration_kind: string, seen: string, reason: string}.\n"
              f"내레이션: {text}\n기록: {cut['desc']}\n인접 대본(S=원본 대사, A=현장음 설명, N=내레이션): {context or []}")
    raw = gemini.video_json(prompt, clip, kind="grid_cover_probe", fps=10)
    if not isinstance(raw, dict) or type(raw.get("text_matches")) is not bool:
        raise ValueError("덮개 프로브의 일치 판정 없음")
    job.save(name, raw)
    return raw


def share_adjacent_action_boundary(rows, transcript):
    """Partition contiguous S→A footage without dropping audio or repeating it.

    Only reclaim the optional speech tail, retaining the STT words and standard
    padding. The reclaimed tail plays with original audio in the following A row.
    Actual speech/action conflicts and non-adjacent repeats still fail validation.
    """
    from app.tikitaka.timing import bind_dialogue
    lines = {line["id"]: line for line in transcript.get("lines", [])}
    notes = []
    for speech, action in zip(rows, rows[1:]):
        if speech["mode"] != "S" or action["mode"] != "A":
            continue
        if len(speech["cuts"]) != 1 or len(action["cuts"]) != 1:
            continue
        s, a = speech["cuts"][0], action["cuts"][0]
        if not s["in"] < a["in"] < s["out"] < a["out"]:
            continue
        protected_end = bind_dialogue(lines, transcript["words"], speech["src"])["end"]
        boundary = max(protected_end, a["in"])
        if boundary >= s["out"] or a["out"] - boundary < 0.8:
            continue
        old_end = s["out"]
        s["out"] = a["in"] = boundary
        s["dur"] = speech["dur"] = round(boundary - s["in"], 3)
        a["dur"] = action["dur"] = round(a["out"] - boundary, 3)
        notes.append(f"행{speech['i']}→{action['i']}: 대사 뒤 여유 구간을 인접 현장음으로 연속 재생 "
                     f"(경계 {old_end:.3f}→{boundary:.3f}s, 원본 화면·소리 보존)")
    return notes


def build_table(job, gemini, rebuild, index, transcript, cuts, duration, proxy, *,
                version_n, title, grid, voice="ko_female", speed="normal", exclude=None, black=None, tag="", force=False, guide=None):
    version = next(v for v in rebuild["versions"] if v["n"] == version_n)
    exclude, black = exclude or [], black or []
    from app.tikitaka.production import SEMANTIC_RULES, narration_context, preflight
    fp = fingerprint([SCHEMA, SEMANTIC_RULES, version, index, transcript, grid, voice, speed,
                      exclude, black, guide, source_identity(job.source)])
    name = f"grid_table_v{version_n}{'_'+tag if tag else ''}.json"
    cached = job.load(name) if job.has(name) else {}
    if not force and cached.get("fingerprint") == fp and all(
            Path(r["tts"]).is_file()
            for r in cached["rows"] if r.get("tts")):
        return cached
    nonce = time.time_ns() if force else None
    preflight_result = preflight(version, index, transcript, exclude + black) if index.get("moments") else None
    if preflight_result:
        job.save(f"production_preflight_v{version_n}{'_'+tag if tag else ''}.json", preflight_result)
    rows = rows_from_items(version, index, transcript, cuts, duration,
        lambda text: _tts_cached(job, text, voice, speed),
        end_fn=lambda we, limit: speech_end_after(job, we, limit=limit))
    notes = share_adjacent_action_boundary(rows, transcript)
    notes += trim_a_rows_off_dialogue(rows) + trim_a_rows_off_black(rows, black)
    for note in notes:
        job.log(f"[table] {note}")
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
    from app.tikitaka.cover_evidence import retrieve, tighten_opening
    opening_audit = tighten_opening(job, gemini, rows, grid)
    evidence = retrieve(job, gemini, rows, index, grid, exclude + black, guide)
    for row in rows:
        if row["mode"] != "N":
            continue
        candidates = candidates_for(row, rows, index, grid, exclude + black, used)
        before = before_dialogue_candidates(row, rows, index, grid, cuts, exclude + black + used)
        proof = {sid: sp for sid, sp in evidence.get(row["i"], {}).get("candidates", {}).items()
                 if not overlap(sp["t_in"], sp["t_out"], used)}
        candidates.update(proof)
        context = narration_context(rows, row, index, transcript)
        moments = {m["id"]: m for m in index.get("moments", [])}
        planned_ids = list(dict.fromkeys(sid for mid in (row.get("production_plan") or {}).get("cover_moment_ids", [])
                                        for sid in moments.get(mid, {}).get("span_ids", [])))
        for sid in planned_ids:
            sp, fact = by.get(sid), index.get("grid_facts", {}).get(sid)
            if sp is None or fact is None or overlap(sp["t_in"], sp["t_out"], exclude + black + used):
                continue
            if any(word in fact.get("scene_script", "") for word in (guide or {}).get("avoid", []) if word):
                continue
            candidates[sid] = {**sp, **fact}
        rejection = ""
        failed_selections = []
        for attempt in range(MAX_REASKS + 1):
            prompt = ("내레이션 아래 화면을 골라라. 아래 후보 ID만 1~4개. "
                      "슬로우 금지, 글자 인용은 기록 원문만, 무관한 화면 금지. "
                      "첫 내레이션이 첫 행이면 첫 화면 중요도 4 이상. "
                      "우선순위: 설명한 행동/장면을 직접 보여주는 화면 > 관련 상황 > 대사 직전 연결 화면. "
                      "실제 행동 후보가 있으면 그 행동을 보여줘야 한다. 지도나 운전으로 추적기 심는 행동을 대체하지 마라. "
                      "같은 인물이라는 이유만으로 다른 행동을 고르지 마라. 앞선 실제 사건의 자료화면은 허용한다. "
                      "before_dialogue는 직접 행동 화면이 없고 문장에 맞을 때만 선택한다. "
                      "이 배치는 아래 고정 앞 장면 전체를 선택하며, 짧으면 내레이션을 다시 쓴다. "
                      "앞 장면에 자료화면 정지·슬로우·다른 씬 추가로 시간을 늘리지 않는다. "
                      "그 외 same_scene 배치는 화면을 이어 TTS 길이를 채우고 자료화면만 최대 2초 정지 가능. "
                      "문장과 화면을 함께 결정하라. 계획한 화면을 우선 검토하되 사용할 수 없거나 의미가 맞지 않으면 새 조합을 골라라. "
                      "다른 화면에 맞추려 새 사실을 만들지 마라. 문장은 필요할 때만 고치고 핵심 의미·연결을 보존하라. "
                      "JSON {text: string, placement: before_dialogue|same_scene, span_ids: [string]}. 시간은 쓰지 마라.\n"
                      + SEMANTIC_RULES + "\n"
                      f"문장: {row['text']} / 길이: {row['dur']:.3f}s\n"
                      f"문장 역할·근거·인접 대본: {context}\n"
                      f"작성 단계의 계획 화면(현재 후보에 있는 ID만 가능): {planned_ids}\n"
                      f"행동/장면 검색 근거: {evidence.get(row['i'], {}).get('reason', '')}\n"
                      f"행동/장면 후보 ID: {list(proof)}\n"
                      f"일반 후보: {[dict(v, usable_sec=max(0, min(MAX_CUT, math.floor(v['t_out']*FPS+1e-7)/FPS-math.ceil(v['t_in']*FPS-1e-7)/FPS))) for v in candidates.values()]}\n"
                      f"이미 반려된 선택(재사용 금지): {failed_selections}\n"
                      f"고정 앞 장면(선택 시 ID 전부): {list(before.values())}\n반려 사유: {rejection}")
            request_key = fingerprint([SCHEMA, prompt, voice, speed, source_identity(job.source), nonce])
            selection_name = f"grid_cover_probes/{request_key}_selection.json"
            raw = None
            try:
                raw = job.load(selection_name) if job.has(selection_name) else gemini.text_json(prompt, kind="grid_cover_select")
                job.save(selection_name, raw)
                if raw in failed_selections:
                    raise ValueError("이미 반려된 동일 화면 조합; 다른 ID 또는 배치를 선택")
                working = copy.deepcopy(row)
                fit_audit = None
                ids = raw.get("span_ids")
                revised = raw.get("text", row["text"])
                if not isinstance(revised, str) or not revised.strip():
                    raise ValueError("빈 내레이션 재선택")
                if revised.strip() != row["text"]:
                    rewrite_check = ("내레이션·화면 재선택의 문장 수정 검수. 핵심 의미와 앞뒤 연결을 유지하고 "
                                     "새 사실·인물·시간 단정을 만들지 않았는지 판단하라. "
                                     'JSON {meaning_preserved: boolean, reason: string}.\n' + SEMANTIC_RULES +
                                     f"\n원문: {row['text']}\n수정: {revised.strip()}\n맥락: {context}\n가이드: {guide or {}}")
                    check_name = f"narration_fit/{fingerprint([SCHEMA, rewrite_check])}_joint_meaning.json"
                    verdict = job.load(check_name) if job.has(check_name) else gemini.text_json(rewrite_check, kind="grid_narration_revision_check")
                    job.save(check_name, verdict)
                    if not isinstance(verdict, dict) or verdict.get("meaning_preserved") is not True:
                        raise ValueError(f"내레이션 재선택의 핵심 의미 보존 실패: {verdict}")
                    path, measured = _tts_cached(job, revised.strip(), voice, speed)
                    if not math.isfinite(measured) or measured <= 0:
                        raise ValueError("내레이션 재합성 길이가 유효하지 않음")
                    working.update(text=revised.strip(), original_text=row["text"], tts=str(path), dur=measured,
                                   plan_sec=narration_plan_sec(revised.strip()))
                placement = raw.get("placement", "same_scene")
                if placement not in {"before_dialogue", "same_scene"}:
                    raise ValueError("알 수 없는 덮개 배치")
                # Even an older response cannot use the immediate lead-in and
                # quietly hold it instead of fitting the narration to its length.
                before_ids = list(before)
                is_tail = isinstance(ids, list) and bool(ids) and before_ids[-len(ids):] == ids
                is_before = placement == "before_dialogue" or is_tail
                if is_before:
                    if not before or not is_tail:
                        raise ValueError("대사 앞 고정 장면의 연속 ID를 대사 직전까지 순서대로 선택해야 함")
                    fixed_before = {sid: before[sid] for sid in ids}
                    fit_audit = fit_before_dialogue(job, gemini, working, fixed_before, rows,
                        voice=voice, speed=speed, key=request_key, guide=guide)
                    selected = assemble_before_dialogue(fixed_before, working["dur"], opening=row is rows[0])
                else:
                    try:
                        selected = assemble_cover(ids, candidates, working["dur"], opening=row is rows[0])
                    except CoverDurationError as e:
                        if e.available < 1 / FPS:
                            raise
                        fit_audit = fit_before_dialogue(job, gemini, working,
                            {sid: candidates[sid] for sid in ids}, rows, voice=voice, speed=speed,
                            key=request_key, guide=guide, budget_sec=e.available, placement="same_scene")
                        selected = assemble_cover(ids, candidates, working["dur"], opening=row is rows[0])
                for cut in selected:
                    result = probe_cover(job, gemini, cut, working["text"],
                        key=fingerprint([request_key, cut, working["text"]]), context=context)
                    if not result["text_matches"]:
                        raise ValueError(f"{cut['src']} 모순: {result.get('seen')} / {result.get('reason')}")
                working["cuts"] = selected
                working["cover_placement"] = "before_dialogue" if is_before else "same_scene"
                row.update(working)
                audit.append({"row": row["i"], "attempts": attempt+1, "previous_rejection": rejection,
                              "narration_fit": fit_audit, "context": context,
                              "original_text": row.get("original_text"), "final_text": row["text"]})
                break
            except (ValueError, TypeError, AttributeError) as e:
                # A failed fit rejects this selection, not the entire script.
                # The next bounded selection must choose different footage and
                # pass the same fit/visual checks; the original row is untouched.
                if isinstance(raw, dict) and raw not in failed_selections:
                    failed_selections.append(copy.deepcopy(raw))
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
    final_version = copy.deepcopy(version)
    for row in rows:
        if row.get("original_text"):
            item = final_version["items"][row["i"]-1]
            item.update(text=row["text"], original_text=row["original_text"], plan_sec=narration_plan_sec(row["text"]))
    final_version["plan_sec"] = round(sum(it.get("plan_sec", 0) for it in final_version["items"]), 3)
    result = {"version": final_version, "rows": rows, "total_sec": t,
              "voice": voice, "speed": speed, "cut_search": "grid", "fingerprint": fp,
              "cover_review": audit, "opening_review": opening_audit, "notes": notes}
    job.save(name, result)
    job.record_step(f"grid_table_v{version_n}{'_'+tag if tag else ''}", total_sec=t, cover_review=audit)
    return result
