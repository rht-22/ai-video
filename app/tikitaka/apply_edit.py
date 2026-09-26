"""편집실 수정 적용 — 제출된 수정 기록(tikitaka_edit/v1)으로 그 편(vN)을 **다시 렌더**한다.

입력은 지금 렌더가 남긴 재료(review_<suffix>/ 의 edit_plan·자막·내레이션·스타일 기록)와 수정 기록 한 건이다.
AI 단계는 다시 돌리지 않는다(2026-09-26 사용자 결정): 재관찰 다듬기(watch_trim)·Stage 4 연출·덮개 구도 판정을
건너뛰고, 사람이 편집실에서 정한 구간·자막·내레이션·보조 자막을 그대로 finalize.render_final 에 넘긴다.
AI 가 사람이 고친 것을 다시 바꾸지 않고, Gemini 비용도 들지 않는다. 얼굴 없는 샷의 구도는 **이전 판정 캐시만** 쓴다
(새로 생긴 샷은 중앙 구도 + 검토 기록).

같은 번호로 교체한다(2026-09-26 사용자 결정): shorts_<suffix>.mp4 를 새 완성본으로 바꾸고, 이전 최종본은
review_<suffix>/final_prev_<ns>.mp4, 이전 묶음은 videos/<suffix>.prev_<ns>/ 로 남는다(bundle.export).

수정 항목의 뜻(편집실 collectOv 와 같은 규약 — 보낸 항목만 바뀌고 나머지는 지금 렌더 그대로):
  clips[]      원본 절대초 · 전량 교체. 편집본 길이 = (끝-시작)/배속 + 붙잡기, 30fps 격자(편집실 clipDur 와 같은 자)
               원음: 내레이션이 나오는 동안만 끈다 — 내레이션을 빼면 다시 켠다(narration_mute)
  subtitles[]  편집본 초 · 전량 교체. source_time_sec 가 있으면 새 구간 위의 그 원본 시각으로 옮긴다(장면 따라가기)
  tts[]        원본 시각(source_time_sec) 기준 · 전량 교체. 문구·목소리·속도로 합성(같은 값이면 TTS 캐시 재사용)
  title        top_title 교체. 제목 창(segments)은 렌더러가 아직 모른다 → 거절
  texts[]      AI 보조 자막·자유 텍스트 · 전량 교체(원본 시각 기준)
  design       이 편 디자인 덮어쓰기(자막 크기·색 등 엔진 디자인 키) — 렌더 당시 디자인 위에 얹고, 다음 적용의 기준이 된다
  images       아직 반영하지 못한다 → 거절(조용히 빼고 렌더하지 않는다)
구간만 바꾸면 손대지 않은 자막·내레이션·보조 자막은 원본 시각으로 새 구간 위에 다시 놓고, 새 구간에 없는 것은 빠진다(기록).

적용 상태는 video_edits/<suffix>/apply/<edit_id>.json 에 남긴다(편집실 목록이 '렌더 중'·'실패'를 보여 준다).
같은 렌더를 보고 낸 이전 제출은 이번 제출에 포함된다(편집실은 매번 그 렌더 대비 전체 차이를 보낸다) → 함께 applied 로 표시.

사용: python -m app.tikitaka.apply_edit <잡 폴더> <suffix> [--edit <edit_id>]   (없으면 가장 최근 대기 수정)
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from app.tikitaka import bundle as BU
from app.tikitaka.common import Job

FPS = BU.FPS
APPLY_DIR = "apply"
SUPPORTED = {"clips", "subtitles", "tts", "title", "texts", "design"}
KEY_NAMES = {"images": "이미지", "design": "디자인", "title.segments": "제목 창"}
LABEL_FIELDS = BU.LABEL_KEYS


class ApplyRefused(Exception):
    """적용할 수 없는 상태 — 사유는 사람이 읽는 한국어."""


# ── 시간 변환 — 편집실 srcToOut/outToSrc 와 같은 규칙 ─────────────────────────────
def clip_sec(c: dict) -> float:
    return BU.clip_edited_sec(c)


def offsets(timeline: list[dict]) -> list[float]:
    out, off = [], 0.0
    for c in timeline:
        out.append(off)
        off += clip_sec(c)
    return out


def total_sec(timeline: list[dict]) -> float:
    return sum(clip_sec(c) for c in timeline)


def src_to_out(timeline: list[dict], t: float) -> float | None:
    """원본 초 → 편집본 초. 그 원본 시각을 담은 **첫** 구간(편성 순서). 없으면 None."""
    for c, off in zip(timeline, offsets(timeline)):
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        if s - 0.001 <= t < e:
            speed = float(c.get("playback_speed") or 1.0)
            return round(off + min(max(0.0, t - s) / speed, clip_sec(c)), 3)
    return None


def out_to_src(timeline: list[dict], t: float) -> float | None:
    return BU.edited_to_source(timeline, t)


def clip_at(timeline: list[dict], t: float) -> int | None:
    for i, (c, off) in enumerate(zip(timeline, offsets(timeline))):
        if off - 1e-6 <= t < off + clip_sec(c) - 1e-9:
            return i
    return None


# ── 적용 ────────────────────────────────────────────────────────────────────
def check_overrides(ov: dict) -> None:
    bad = [k for k in ov if k not in SUPPORTED]
    title = ov.get("title")
    if isinstance(title, dict) and title.get("segments"):
        bad.append("title.segments")
    if bad:
        names = ", ".join(KEY_NAMES.get(k, k) for k in bad)
        raise ApplyRefused(f"아직 다시 렌더에 반영할 수 없는 수정이 들어 있어요: {names}. 이 항목을 되돌린 뒤 다시 제출해 주세요.")


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _spans_in(grid: dict, s: float, e: float) -> list[str]:
    out = []
    for sp in sorted(grid.get("span_candidates") or [], key=lambda x: (float(x["t_in"]), x["id"])):
        a, z = float(sp["t_in"]), float(sp["t_out"])
        if _overlap(s, e, a, z) > 0:
            out.append(sp["id"])
    return out


def new_timeline(clips: list[dict], old: list[dict], grid: dict, log: list[dict]) -> tuple[list[dict], dict[int, int]]:
    """편집실 구간 → 렌더 구간. 가장 많이 겹치는 이전 구간에서 원음 여부·덮개·장면 번호·구도를 물려받는다.
    반환: (새 timeline, 새 index → 같은 원본 범위·배속인 이전 index)."""
    if not clips:
        raise ApplyRefused("구간이 하나도 없어요.")
    out, same = [], {}
    for i, c in enumerate(clips):
        s, e = round(float(c["start_sec"]), 3), round(float(c["end_sec"]), 3)
        speed = float(c.get("playback_speed") or 1.0)
        hold = float(c.get("hold_sec") or 0.0)
        if not e > s:
            raise ApplyRefused(f"{i + 1}번 구간의 끝이 시작보다 앞이에요.")
        if not 1.0 <= speed <= 1.2 + 1e-9:
            raise ApplyRefused(f"{i + 1}번 구간 배속 {speed}배는 렌더할 수 없어요(1.0~1.2배).")
        best, best_ov = None, 0.0
        for j, o in enumerate(old):
            ov = _overlap(s, e, float(o["clip_start_sec"]), float(o["clip_end_sec"]))
            if ov > best_ov:
                best, best_ov = j, ov
        base = old[best] if best is not None else {}
        clip = {"clip_start_sec": s, "clip_end_sec": e, "role": c.get("role") or base.get("role") or "build",
                "beat": base.get("beat", 0), "use_original_audio": bool(base.get("use_original_audio", True)),
                "subtitle": "", "time_authority": "editor"}
        if speed != 1.0:
            clip["playback_speed"] = round(speed, 4)
        if hold:
            clip["hold_sec"] = round(hold, 3)
        if base.get("cover"):
            clip["cover"] = True
        spans = _spans_in(grid, s, e) or list(base.get("span_ids") or [])
        if not spans:
            raise ApplyRefused(f"{i + 1}번 구간({s:.2f}~{e:.2f}초)이 원본 분석 범위 밖이에요.")
        clip["span_ids"] = spans
        # 구도 기록은 거의 같은 화면일 때만 물려받는다(절반 넘게 겹침)
        if best is not None and best_ov >= 0.5 * (e - s):
            for k in ("reframe", "narration_framing", "subject_pos"):
                if base.get(k):
                    clip[k] = copy.deepcopy(base[k])
        if best is not None and abs(float(base["clip_start_sec"]) - s) < 1e-3 and abs(float(base["clip_end_sec"]) - e) < 1e-3 \
                and abs(float(base.get("playback_speed") or 1.0) - speed) < 1e-6:
            same[i] = best
        out.append(clip)
    log.append({"kind": "clips", "count": len(out), "unchanged": len(same)})
    return out, same


def move_items(items: list[dict], old_tl: list[dict], new_tl: list[dict], *, what: str,
               log: list[dict], anchor_key: str | None = "source_time_sec") -> list[dict]:
    """편집본 초 항목(start_sec/end_sec)을 원본 시각 기준으로 새 구간 위에 다시 놓는다. 못 놓으면 뺀다(기록)."""
    out = []
    for it in items:
        start, end = float(it["start_sec"]), float(it["end_sec"])
        src = it.get(anchor_key) if anchor_key else None
        if src is None:
            src = out_to_src(old_tl, start)
        new_start = src_to_out(new_tl, float(src)) if src is not None else None
        if new_start is None:
            log.append({"kind": "dropped", "what": what, "text": it.get("text"), "reason": "새 구간에 그 원본 장면이 없음"})
            continue
        moved = {**it, "start_sec": new_start, "end_sec": round(new_start + (end - start), 3)}
        out.append(moved)
    return out


def fit_inside(items: list[dict], total: float, *, what: str, log: list[dict]) -> list[dict]:
    out = []
    for it in items:
        s, e = float(it["start_sec"]), min(float(it["end_sec"]), total)
        if not 0 <= s < e:
            log.append({"kind": "dropped", "what": what, "text": it.get("text"), "reason": "영상 길이 밖"})
            continue
        out.append({**it, "start_sec": round(s, 3), "end_sec": round(e, 3)})
    return out


def editor_subtitles(subs: list[dict], new_tl: list[dict], old_segments: list[dict]) -> list[dict]:
    out = []
    for su in subs:
        text = " ".join(str(su.get("text") or "").split())
        if not text:
            continue
        start, end = float(su["start_sec"]), float(su["end_sec"])
        src = su.get("source_time_sec")
        if src is not None:
            at = src_to_out(new_tl, float(src))
            if at is not None:
                start, end = at, at + (end - start)
        seg = {"start_sec": round(start, 3), "end_sec": round(end, 3), "text": text}
        src = src if src is not None else out_to_src(new_tl, start)
        if src is not None:
            seg["source_time_sec"] = round(float(src), 3)
        same = next((o for o in old_segments if o.get("text") == text), None)
        if same:
            for k in ("speaker", "emphasis_level", "color"):
                if k in same:
                    seg[k] = same[k]
        if isinstance(su.get("style"), dict) and su["style"].get("color"):
            seg["color"] = su["style"]["color"]
        out.append(seg)
    return sorted(out, key=lambda s: s["start_sec"])


def editor_tts(job: Job, cues: list[dict], new_tl: list[dict], old_files: list[dict], log: list[dict],
               synth=None, voice_default: str = "", speed_default: str = "normal") -> list[dict]:
    """편집실 내레이션 → tts_cue_files. 문구·목소리·속도가 같으면 TTS 캐시(잡 tts/)를 그대로 쓴다.
    목소리·속도가 비었으면 이 편을 렌더한 설정(--voice/--speed)을 쓴다 — 그때 만든 mp3 와 같은 캐시 키다."""
    if synth is None:
        from app.tikitaka.table import _tts_cached as synth
    out = []
    total = total_sec(new_tl)
    for n, t in enumerate(cues, 1):
        text = " ".join(str(t.get("text") or "").split())
        if not text:
            continue
        voice, speed = str(t.get("voice") or voice_default), str(t.get("speed") or speed_default)
        if not voice:
            raise ApplyRefused(f"{n}번째 내레이션의 목소리가 비어 있어요.")
        src = float(t["source_time_sec"])
        start = src_to_out(new_tl, src)
        if start is None:
            log.append({"kind": "dropped", "what": "tts", "text": text, "reason": "새 구간에 그 원본 장면이 없음"})
            continue
        path, dur = synth(job, text, voice, speed)
        end = round(start + dur, 3)
        if end > total + 1e-6:
            raise ApplyRefused(f"내레이션 '{text[:20]}'이 영상 끝을 넘어요({end:.2f}초 > {total:.2f}초). 구간을 늘리거나 문구를 줄여 주세요.")
        i = clip_at(new_tl, start)
        old = next((f["cue"] for f in old_files if abs(float(f["cue"].get("source_time_sec", -1)) - src) < 0.05), {})
        cue = {"text": text, "start_sec": round(start, 3), "end_sec": end, "duration_sec": dur, "fit_actual_sec": dur,
               "beat": new_tl[i].get("beat", 0) if i is not None else 0, "source_time_sec": round(src, 3),
               "source_end_sec": out_to_src(new_tl, max(start, end - 1e-3)) or src,
               "muted_span_ids": list(old.get("muted_span_ids") or (new_tl[i]["span_ids"] if i is not None else [])),
               "voice": voice, "speed": speed}
        out.append({"cue": cue, "path": str(Path(path).resolve())})
    out.sort(key=lambda f: f["cue"]["start_sec"])
    for a, b in zip(out, out[1:]):
        if b["cue"]["start_sec"] < a["cue"]["end_sec"] - 1e-3:
            raise ApplyRefused(f"내레이션 '{a['cue']['text'][:16]}'과 '{b['cue']['text'][:16]}'이 겹쳐요. 한쪽을 옮겨 주세요.")
    return out


def editor_labels(texts: list[dict], new_tl: list[dict], log: list[dict]) -> list[dict]:
    out = []
    for t in texts:
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        start = src_to_out(new_tl, float(t["source_time_sec"]))
        if start is None:
            log.append({"kind": "dropped", "what": "texts", "text": text, "reason": "새 구간에 그 원본 장면이 없음"})
            continue
        lb = {"text": text, "start_sec": start, "end_sec": round(start + float(t.get("duration_sec") or 1.0), 3)}
        for k in LABEL_FIELDS:
            if t.get(k) is not None:
                lb[k] = t[k]
        lb.setdefault("fx", "pop")
        out.append(lb)
    return sorted(out, key=lambda x: x["start_sec"])


def remap_emphasis(emphasis: list[dict], segments: list[dict], old_segments: list[dict], log: list[dict]) -> list[dict]:
    """강조는 자막 줄 하나에 붙어 있다(index·line). 줄이 빠지거나 끼면 번호가 밀리므로 **같은 줄**을 찾아 다시 붙인다:
    이전 줄의 원본 시각(없으면 문구)이 같은 줄 중 그 강조 글자를 아직 담은 줄. 못 찾으면 뺀다(기록)."""
    out = []
    for e in emphasis:
        i = e.get("index")
        was = old_segments[i] if isinstance(i, int) and 0 <= i < len(old_segments) else None
        word = str(e.get("text") or "")
        cands = [(j, s) for j, s in enumerate(segments) if word and word in s["text"]]
        if was is not None:
            src = was.get("source_time_sec")
            same = [(j, s) for j, s in cands if src is not None and s.get("source_time_sec") is not None
                    and abs(float(s["source_time_sec"]) - float(src)) < 0.05]
            cands = same or [(j, s) for j, s in cands if s["text"] == was["text"]] or cands
        if not cands:
            log.append({"kind": "dropped", "what": "emphasis", "text": word, "reason": "그 글자가 든 자막 줄이 없어짐"})
            continue
        j, seg = cands[0]
        base = float(was["start_sec"]) if was is not None else seg["start_sec"]
        shift = seg["start_sec"] - base
        a = float(e.get("start_sec", base)) + shift
        z = float(e.get("end_sec", a + 0.5)) + shift
        a = max(seg["start_sec"], min(a, seg["end_sec"] - 0.05))
        z = min(seg["end_sec"], max(z, a + 0.05))
        moved = {**e, "index": j, "start_sec": round(a, 3), "end_sec": round(z, 3)}
        if isinstance(e.get("line"), str) and e["line"].startswith("L"):
            moved["line"] = f"L{j}"
        out.append(moved)
    return out


def remap_zooms(zooms: list[dict], same: dict[int, int], log: list[dict]) -> list[dict]:
    back = {old: new for new, old in same.items()}
    out = []
    for z in zooms:
        if z.get("clip") in back:
            out.append({**z, "clip": back[z["clip"]]})
        else:
            log.append({"kind": "dropped", "what": "zoom", "clip": z.get("clip"), "reason": "구간이 바뀜"})
    return out


def narration_mute(timeline: list[dict], cue_files: list[dict], log: list[dict]) -> list[dict]:
    """원음은 내레이션이 나오는 동안만 끈다(2026-09-26 사용자 결정 — 내레이션 넣으면 자동 음소거).
    - 내레이션이 걸친 일반 구간: use_original_audio=False → 엔진이 내레이션 창(원본 시각)만 끄고 나머지는 살린다
    - 내레이션이 없는 일반 구간: 원음 켬
    - 덮개(cover) 구간은 원래 통째로 무음이다. 내레이션이 빠졌고 1배속·붙잡기 없음이면 일반 구간으로 되돌려 원음을 켠다.
      배속·붙잡기 덮개는 엔진이 원음을 허용하지 않아 무음으로 남는다(기록)."""
    spans = [(float(f["cue"]["start_sec"]), float(f["cue"]["end_sec"])) for f in cue_files]
    out = []
    for i, (c, off) in enumerate(zip(timeline, offsets(timeline))):
        c = dict(c)
        end = off + clip_sec(c)
        voiced = any(_overlap(off, end, a, z) > 1e-3 for a, z in spans)
        if c.get("cover"):
            if not voiced:
                if float(c.get("playback_speed") or 1.0) == 1.0 and not c.get("hold_sec"):
                    c.pop("cover", None)
                    c["use_original_audio"] = True
                    log.append({"kind": "audio", "clip": i, "result": "내레이션이 빠진 덮개 — 원음 켬"})
                else:
                    log.append({"kind": "audio", "clip": i, "result": "내레이션이 빠졌지만 배속·붙잡기 구간이라 무음 유지"})
        else:
            want = not voiced
            if bool(c.get("use_original_audio", True)) != want:
                log.append({"kind": "audio", "clip": i, "result": "원음 켬" if want else "내레이션 동안 원음 끔"})
            c["use_original_audio"] = want
        out.append(c)
    return out


def rebuild_beats(story: dict, timeline: list[dict]) -> dict:
    story = copy.deepcopy(story)
    for b in story.get("beats") or []:
        ids = [s for c in timeline if c.get("beat") == b["number"] for s in c["span_ids"]]
        if ids:
            b["span_ids"] = list(dict.fromkeys(ids))
    return story


def apply_overrides(ov: dict, *, plan: dict, segments: list[dict], resources: dict, story: dict,
                    style: dict, grid: dict, job: Job, synth=None, captions=None,
                    voice: str = "", speed: str = "normal", design: dict | None = None) -> dict:
    """순수에 가깝게 — 파일은 TTS 합성·구절 자막 캐시만 쓴다. 반환: 새 재료 + 적용 기록."""
    check_overrides(ov)
    log: list[dict] = []
    plan, segments, resources = copy.deepcopy(plan), copy.deepcopy(segments), copy.deepcopy(resources)
    old_segments = copy.deepcopy(segments)
    style = copy.deepcopy(style)
    v3 = style.setdefault("v3_style", {})
    old_tl = plan["timeline"]
    tl, same = old_tl, {i: i for i in range(len(old_tl))}
    if "clips" in ov:
        tl, same = new_timeline(ov["clips"], old_tl, grid, log)
    total = total_sec(tl)
    moved = tl is not old_tl

    if "subtitles" in ov:
        segments = editor_subtitles(ov["subtitles"], tl, segments)
        log.append({"kind": "subtitles", "count": len(segments)})
    elif moved:
        segments = move_items(segments, old_tl, tl, what="subtitles", log=log)
    segments = fit_inside(sorted(segments, key=lambda s: s["start_sec"]), total, what="subtitles", log=log)

    old_files = resources.get("tts_cue_files") or []
    tts_changed = "tts" in ov
    if tts_changed:
        resources["tts_cue_files"] = editor_tts(job, ov["tts"], tl, old_files, log, synth=synth,
                                                   voice_default=voice, speed_default=speed)
        log.append({"kind": "tts", "count": len(resources["tts_cue_files"])})
    elif moved:
        files = []
        for f in old_files:
            cue = f["cue"]
            at = src_to_out(tl, float(cue["source_time_sec"]))
            if at is None:
                log.append({"kind": "dropped", "what": "tts", "text": cue.get("text"), "reason": "새 구간에 그 원본 장면이 없음"})
                continue
            shift = at - float(cue["start_sec"])
            end = round(float(cue["end_sec"]) + shift, 3)
            if end > total + 1e-6:
                raise ApplyRefused(f"내레이션 '{cue['text'][:20]}'이 바뀐 구간에서 영상 끝을 넘어요. 구간을 늘려 주세요.")
            i = clip_at(tl, at)
            files.append({**f, "cue": {**cue, "start_sec": round(at, 3), "end_sec": end,
                                       "beat": tl[i].get("beat", cue.get("beat")) if i is not None else cue.get("beat")}})
        resources["tts_cue_files"] = files
    if "tts_caption_segments" in resources and (tts_changed or moved):
        if tts_changed:
            if captions is None:
                from app.tikitaka.subtitles import narration_captions as captions
            resources["tts_caption_segments"] = captions(job, resources)
        else:
            caps = []
            for f in resources["tts_cue_files"]:
                old = next((o["cue"] for o in old_files if o["cue"].get("source_time_sec") == f["cue"].get("source_time_sec")), None)
                if old is None:
                    continue
                shift = f["cue"]["start_sec"] - float(old["start_sec"])
                caps += [{**c, "start_sec": round(c["start_sec"] + shift, 3), "end_sec": round(c["end_sec"] + shift, 3)}
                         for c in resources["tts_caption_segments"]
                         if float(old["start_sec"]) - 1e-3 <= c["start_sec"] < float(old["end_sec"]) + 1e-3]
            resources["tts_caption_segments"] = caps

    if tts_changed or moved:
        tl = narration_mute(tl, resources.get("tts_cue_files") or [], log)

    if "texts" in ov:
        v3["labels"] = editor_labels(ov["texts"], tl, log)
        log.append({"kind": "texts", "count": len(v3["labels"])})
    elif moved:
        v3["labels"] = move_items(v3.get("labels") or [], old_tl, tl, what="texts", log=log, anchor_key=None)
    v3["labels"] = fit_inside(v3.get("labels") or [], total, what="texts", log=log)

    if "subtitles" in ov or moved:
        v3["emphasis"] = remap_emphasis(v3.get("emphasis") or [], segments, old_segments, log)
    if moved:
        v3["zooms"] = remap_zooms(v3.get("zooms") or [], same, log)

    if "title" in ov:
        top = str((ov["title"] or {}).get("top_title") or "").strip()
        if not top:
            raise ApplyRefused("제목이 비어 있어요.")
        plan.setdefault("layout", {})["top_title"] = top
        log.append({"kind": "title", "top_title": top})

    design = copy.deepcopy(design or {})
    if "design" in ov:
        changes = ov["design"]
        if not isinstance(changes, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float, bool))
                                                     for k, v in changes.items()):
            raise ApplyRefused("디자인 수정 형식이 올바르지 않아요.")
        changed = {k: v for k, v in changes.items() if design.get(k) != v}
        design.update(changes)
        log.append({"kind": "design", "changed": changed})

    plan["timeline"] = tl
    plan["output_fps"] = FPS
    story = rebuild_beats(story, tl)
    story["narration_cues"] = [copy.deepcopy(f["cue"]) for f in resources.get("tts_cue_files") or []]
    return {"plan": plan, "segments": segments, "resources": resources, "story": story, "style": style,
            "design": design, "log": log, "duration_sec": total, "clips_changed": moved}


# ── 실행 ────────────────────────────────────────────────────────────────────
def _load(p: Path) -> Any:
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _write(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def status_path(job_dir: Path, suffix: str, edit_id: str) -> Path:
    return BU.edits_dir(job_dir, suffix) / APPLY_DIR / f"{edit_id}.json"


def pick_edit(job_dir: Path, suffix: str, edit_id: str | None, video: dict) -> tuple[dict, list[str]]:
    edits = BU.list_edits(job_dir, suffix)
    applied = set(((video.get("provenance") or {}).get("render") or {}).get("applied_edits") or [])
    pending = [e for e in edits if e["edit_id"] not in applied]
    if edit_id:
        edit = next((e for e in edits if e["edit_id"] == edit_id), None)
        if edit is None:
            raise ApplyRefused("그 수정 기록을 찾을 수 없어요.")
        if edit_id in applied:
            raise ApplyRefused("이미 반영된 수정이에요.")
    elif pending:
        edit = pending[-1]
    else:
        raise ApplyRefused("반영할 수정이 없어요.")
    if edit["based_on_render_fingerprint"] != video.get("render_fingerprint"):
        raise ApplyRefused("이 수정은 지금 영상이 아니라 이전 판을 보고 한 거예요. 편집실을 새로 열어 다시 제출해 주세요.")
    # 같은 렌더를 보고 낸 이전 제출은 이번 제출에 포함된다(편집실은 렌더 대비 전체 차이를 보낸다)
    covered = [e["edit_id"] for e in pending if e["based_on_render_fingerprint"] == edit["based_on_render_fingerprint"]
               and e["edit_id"] <= edit["edit_id"]]
    return edit, covered


def cache_only_gemini():
    raise RuntimeError("다시 렌더에서는 AI 판정을 새로 하지 않는다(이전 판정 캐시만)")


def run(job_dir: Path, suffix: str, edit_id: str | None = None, *, log=print, render=None) -> dict:
    job_dir = Path(job_dir).resolve()
    video_file = job_dir / BU.VIDEOS_DIR / suffix / "video.json"
    if not video_file.exists():
        raise ApplyRefused("영상 묶음이 없어요.")
    video = _load(video_file)
    if video.get("status") != "ready":
        raise ApplyRefused("이 영상은 낡음 상태예요. 파이프라인으로 다시 렌더한 뒤 편집해 주세요.")
    prov = (video.get("provenance") or {}).get("render") or {}
    if not prov.get("design") and "design" not in prov:
        raise ApplyRefused("이 영상의 렌더 기록(디자인)이 없어 같은 모양으로 다시 렌더할 수 없어요.")
    edit, covered = pick_edit(job_dir, suffix, edit_id, video)
    status_file = status_path(job_dir, suffix, edit["edit_id"])
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _write(status_file, {"edit_id": edit["edit_id"], "state": "running", "pid": os.getpid(), "started_at": started})
    try:
        result = _render(job_dir, suffix, video, prov, edit, covered, log=log, render=render)
    except BaseException as e:
        msg = str(e) if isinstance(e, ApplyRefused) else f"{type(e).__name__}: {str(e)[:400]}"
        _write(status_file, {"edit_id": edit["edit_id"], "state": "failed", "started_at": started,
                             "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "error": msg,
                             "refused": isinstance(e, ApplyRefused)})
        raise
    _write(status_file, {"edit_id": edit["edit_id"], "state": "done", "started_at": started,
                         "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **result})
    return result


def _render(job_dir: Path, suffix: str, video: dict, prov: dict, edit: dict, covered: list[str], *, log, render) -> dict:
    from app.tikitaka.grid import fingerprint
    source = Path(video["source"]["path"])
    if not source.is_file():
        raise ApplyRefused("원본 영상을 찾을 수 없어요.")
    publish = _load(job_dir / f"publish_{suffix}.json")
    job = Job(source, job_dir, publish.get("work") or video.get("work") or "")
    work = Job(source, job_dir / f"review_{suffix}", job.title)
    fp_now = (_load(work.path("render_fingerprint.json")) if work.has("render_fingerprint.json") else {}).get("fingerprint")
    if fp_now != video["render_fingerprint"]:
        raise ApplyRefused("작업 폴더의 렌더가 묶음과 달라요. 파이프라인이 이 편을 다시 렌더하는 중인지 확인해 주세요.")
    style_saved = work.load("checkpoint_style.json")
    got = apply_overrides(edit["overrides"], plan=work.load("edit_plan.json"), segments=work.load("subtitle_segments.json"),
                          resources=work.load("checkpoint_resources.json"), story=work.load("checkpoint_story.json")["story"],
                          style=style_saved["style"], grid=work.load("grid.json"), job=job,
                          voice=prov.get("voice") or "", speed=prov.get("speed") or "normal",
                          design=prov.get("design") or {})
    for item in got["log"]:
        log(f"[수정 적용] {json.dumps(item, ensure_ascii=False)}")
    from app.tikitaka import finish
    from app.v3 import finalize, stage4
    finish.validate_bundle(got["plan"], work.load("grid.json"), got["segments"], got["resources"])
    if got["clips_changed"] or "tts" in edit["overrides"]:
        # 라벨 얼굴 회피가 보는 초안 — 새 구간·내레이션으로 다시 만든다
        finish.render_draft(source, got["plan"]["timeline"], work.path("draft_480.mp4"), got["resources"], log=work.log)
    design = got["design"]
    style_preset = prov.get("style_preset") or "drama_clip"
    shot_ask = None
    if design.get("face_tracking", True):
        from app.tikitaka.cover_framing import shot_asker
        probe = work.load("checkpoint_probe.json")
        preset = finalize.merge_channel_preset(stage4.get_style_preset(style_preset), design)
        geo = finalize.band_crop_size(preset["aspect_ratio"], (probe["width"], probe["height"]), probe.get("picture"))
        shot_ask = shot_asker(work, got["story"], job.load("transcript.json") if job.has("transcript.json") else {},
                              get_gemini=cache_only_gemini,
                              crop_width_ratio=round(geo[0] / probe["width"], 3) if geo else None)
    final = work.path("final_1080x1920.mp4")
    if final.exists():
        final.rename(work.path(f"final_prev_{time.time_ns()}.mp4"))
    render = render or finalize.render_final
    final, audit = render(video_path=source, plan=got["plan"], style_doc=got["style"], segments=got["segments"],
                          resources=got["resources"], story_doc=got["story"], output_dir=work.out_dir,
                          channel_design=design, muted_gain_db=None, style_preset=style_preset,
                          shot_target_ask=shot_ask, log=work.log)
    validation = finish.validate_bundle(got["plan"], work.load("grid.json"), got["segments"], got["resources"])
    validation["media"] = finish.validate_media(final, validation["duration_sec"])
    render_fp = fingerprint([video["render_fingerprint"], edit["edit_id"], edit.get("overrides_sha256")])
    work.save("edit_plan.json", got["plan"]); work.save("subtitle_segments.json", got["segments"])
    work.save("checkpoint_story.json", {"story": got["story"]}); work.save("checkpoint_resources.json", got["resources"])
    if "tts_caption_segments" in got["resources"]:
        work.save("tts_caption_segments.json", got["resources"]["tts_caption_segments"])
    # 지문을 바꿔 둔다 — 파이프라인이 다음에 이 편을 다시 만들 때 사람이 고친 스타일을 AI 결과로 착각해 재사용하지 않게
    work.save("checkpoint_style.json", {**style_saved, "fingerprint": f"edited:{edit['edit_id']}", "style": got["style"]})
    review = work.load("review.json")
    review.update({"output": str(final), "render_fingerprint": render_fp, "validation": validation,
                   "edit_apply": {"edit_id": edit["edit_id"], "covered": covered, "log": got["log"],
                                  "based_on_render_fingerprint": video["render_fingerprint"]}})
    work.save("review.json", review); job.save(f"review_{suffix}.json", review)
    work.save("render_fingerprint.json", {"fingerprint": render_fp})
    work.record_step("edit_apply", edit_id=edit["edit_id"], **{k: v for k, v in audit.items() if isinstance(v, (int, float, str))})
    output = job.path(f"shorts_{suffix}.mp4")
    temp = output.with_suffix(".tmp.mp4")
    shutil.copy2(final, temp); os.replace(temp, output)
    applied = list(dict.fromkeys(list(prov.get("applied_edits") or []) + covered))
    settings = {k: v for k, v in prov.items() if k not in {"git_sha", "git_dirty", "recorded_at", "argv"}}
    new_prov = BU.render_provenance(**{**settings, "design": design, "applied_edits": applied,
                                       "edit_apply": {"edit_id": edit["edit_id"], "created_by": edit.get("created_by"),
                                                      "based_on_render_fingerprint": video["render_fingerprint"],
                                                      "skipped_ai": ["watch_trim", "stage4_style", "cover_framing"]}})
    exported = BU.export(job_dir, suffix, render=new_prov)
    if exported.status == "refused":
        raise ApplyRefused(f"렌더는 끝났지만 묶음을 만들지 못했어요: {exported.detail}")
    return {"render_fingerprint": render_fp, "duration_sec": validation["duration_sec"], "applied_edits": covered,
            "log": got["log"], "bundle": exported.status}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.tikitaka.apply_edit", description="편집실 수정 기록으로 그 편을 다시 렌더")
    ap.add_argument("job_dir", type=Path)
    ap.add_argument("suffix")
    ap.add_argument("--edit", default=None, help="수정 기록 ID(없으면 가장 최근 대기 수정)")
    a = ap.parse_args(argv)
    from app.tikitaka.common import load_dotenv_if_any
    load_dotenv_if_any()
    try:
        result = run(a.job_dir, a.suffix, a.edit, log=lambda m: print(m, file=sys.stderr, flush=True))
    except ApplyRefused as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
