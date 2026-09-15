"""5단계 — 마스터 편집 테이블. 선택 버전의 항목 → N/S/A 행 + 절대 타임코드(MM:SS.ms).

- S: 전사 단어 타임스탬프에 묶는다(립싱크). A: 순간을 샷 안전 구간으로 스냅.
- N: TTS 를 먼저 합성해 **실측 길이**를 마스터로 삼고, 컷 소스를 정배속으로 쌓아 그 길이를 꽉 채운다.
  컷 소스 선택은 두 경로: `agentic`(Gemini agentic video — 영상 전체를 주고 순간 검색) / `index`(3단계 인덱스에서 텍스트로 고름).
  agentic 이 실패하거나 결과가 부실한 행은 index 로 떨어진다(기록).
산출 `table_v{n}.json` + `master_table_v{n}.md`.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from app.tikitaka.common import Job, fmt_tc, parse_tc, ms3, find_bin
from app.tikitaka.llm import Gemini
from app.tikitaka.prompts import TABLE_PROMPT
from app.tikitaka.guide import in_excluded
from app.tikitaka.timing import (prev_shots_before, bind_dialogue, snap_moment, safe_shot, CutSource, stack_cuts, next_shots_after,
                                 narration_plan_sec, nudge_in, nudge_out, action_window)
from app.tikitaka.common import NARRATION_CHARS_PER_SEC

AGENTIC_PROMPT = """너는 드라마 영상에서 편집 컷을 찾는 '마스터 에디팅 아키텍트'다. 첨부 영상은 작품 「{title}」 원본 전체(360p·1fps 프록시)다.
아래 내레이션 행마다, 내레이션이 흐르는 동안 화면에 깔 **정배속 리액션/상황 컷**을 2~4개 찾아라(슬로우 모션 금지 — 컷을 쌓아 시간을 채운다).

찾는 기준
- [N] 내레이션 행: 각 행의 [문맥 구간] 근처(앞뒤 대사가 있는 **같은 장면**)에서 찾는다. 그 내레이션이 말하는 인물·상황과 맞는 컷: 듣는 표정,
  리액션, 상황 묘사, 소품/공간 인서트. 컷 하나는 1.0~2.5초 분량. 대사 중인 입은 피하고(내레이션과 겹친다) 표정·동작 위주.
  **3~4개**(샷이 짧아 잘릴 수 있으니 여유 있게). 다른 장면의 인물이 갑자기 끼어들면 안 된다.
- [A] 현장음 행: 인덱스가 추정한 구간 ±10초 안에서 **그 소리가 실제로 나는 동작의 정확한 시작·끝**(0.8~3.0초)을 찾는다.
  타격·문·한숨처럼 소리가 터지는 프레임에 시작을 맞춘다(액션 싱크). 컷 1개.
- [피할 구간]에 적힌 시각(대사 행이 쓰는 원본 구간)과 [활용 불가 구간](권리사 지침 — 절대 금지)은 쓰지 않는다.
- 타임코드는 원본 절대 시각 MM:SS.ms. 근사치 금지. 화면에 실제로 있는 것만.

{rows}

출력 JSON 하나(코드블록 금지):
{{"rows": [{{"i": 3, "cuts": [{{"start": "13:41.200", "end": "13:43.000", "who": "홍재인", "desc": "말문 막힌 표정"}}, …]}},
           {{"i": 5, "cuts": [{{"start": "42:55.300", "end": "42:56.900", "who": "음대교수", "desc": "악보 뭉치로 내리치는 순간"}}]}}, …]}}
"""

A_REFINE_WINDOW_SEC = 10.0      # A 행 agentic 재탐색 허용 반경 — 인덱스 추정에서 이보다 멀면 무시(엉뚱한 장면 방지)


DIALOGUE_TAIL_MAX_SEC = 0.9      # whisper 마지막 단어 끝 뒤로 실제 발성이 이어지는 최대 허용(실측: "나가" 끝이 0.15s+ 앞당겨짐)
_SIL_RE = re.compile(r"silence_start: ([0-9.]+)")


def speech_end_after(job: Job, word_end: float, *, limit: float, noise_db: int = -35, min_sil: float = 0.18) -> float:
    """whisper 마지막 단어 끝(word_end) 뒤 실제 발성 끝 — 그 뒤 첫 무음(≥min_sil)의 시작 + 0.06s. 무음이 없으면 limit.
    2026-09-10 실측: 행 2 "각오해 나가 나가"의 두 번째 '나가'가 whisper 끝 +0.15s 에서 잘렸다."""
    wav = job.path("audio_16k.wav")
    t0 = max(0.0, word_end - 0.05)
    span = max(0.2, limit - t0)
    if not wav.exists() or span <= 0.2:
        return ms3(limit)
    ffmpeg = find_bin("ffmpeg")
    proc = subprocess.run([ffmpeg, "-v", "info", "-ss", f"{t0:.3f}", "-t", f"{span:.3f}", "-i", str(wav),
                           "-af", f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"], capture_output=True, text=True)
    starts = [t0 + float(x) for x in _SIL_RE.findall(proc.stderr)]
    starts = [x for x in starts if x >= word_end - 0.05]
    if starts:
        return ms3(min(limit, starts[0] + 0.06))
    return ms3(limit)


def _tts_cached(job: Job, text: str, voice: str, speed: str) -> tuple[Path, float]:
    from app.modules.tts import synthesize_tts, get_audio_duration
    key = hashlib.sha1(f"{text}|{voice}|{speed}".encode("utf-8")).hexdigest()[:16]
    d = job.path("tts")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{key}.mp3"
    if not p.exists():
        synthesize_tts(text, p, voice=voice, speed=speed)
    return p, ms3(get_audio_duration(p))


def rows_from_items(version: dict, index: dict, transcript: dict, cuts: list[float], duration: float,
                    tts_fn, end_fn=None) -> list[dict]:
    """항목 → 행(타임코드 확정 전 N 은 tts 길이만). 순수 함수에 가깝다(tts_fn·end_fn 주입).
    end_fn(word_end, limit) → 실제 발성 끝(무음 기반). 없으면 whisper 끝 +0.15s 그대로."""
    lines_by_id = {l["id"]: l for l in transcript["lines"]}
    moments_by_id = {m["id"]: m for m in index["moments"]}
    words = transcript["words"]
    rows: list[dict] = []
    for k, it in enumerate(version["items"], 1):
        t = it["type"]
        if t == "S":
            b = bind_dialogue(lines_by_id, words, it["line_ids"])
            if end_fn is not None:
                last_wi = max(i for lid in it["line_ids"] for i in lines_by_id[lid]["word_i"])
                nxt = words[last_wi + 1]["start"] if last_wi + 1 < len(words) else duration
                limit = min(nxt - 0.05, words[last_wi]["end"] + DIALOGUE_TAIL_MAX_SEC)
                if limit > b["end"]:
                    b["end"] = max(b["end"], end_fn(words[last_wi]["end"], limit))
            # 자막은 묶은 행이 아니라 **원래 줄 단위**로 각자의 단어 시각에 띄운다(실측: 3줄을 묶은 60자+ 행이 화면 밖으로 넘쳤다)
            subs = []
            for lid in it["line_ids"]:
                sb = bind_dialogue(lines_by_id, words, [lid])
                subs.append({"start": ms3(max(b["start"], sb["start"])), "end": ms3(min(b["end"], sb["end"])), "text": sb["text"]})
            rows.append({"i": k, "mode": "S", "speaker": b["speaker"] or it.get("speaker") or "미상", "text": b["text"],
                         "label_suffix": (str(it.get("label_suffix") or "").strip() or None),   # 자막 라벨 꼬리("(전화)" — 화면 밖 목소리 표시, 2026-09-13)
                         "effect": it.get("effect"), "src": it["line_ids"], "sub_lines": subs,
                         "cuts": [{"src": "+".join(it["line_ids"]), "in": b["start"], "out": b["end"],
                                   "dur": ms3(b["end"] - b["start"]), "desc": f"[립싱크] {b['speaker'] or ''} 대사"}],
                         "dur": ms3(b["end"] - b["start"])})
        elif t == "A":
            m = moments_by_id[it["moment_id"]]
            a, b_ = action_window(cuts, m["start"], m["end"], duration, min_sec=0.8)
            rows.append({"i": k, "mode": "A", "text": f"(현장음) ({m.get('sound') or m['desc']})", "effect": it.get("effect"),
                         "src": [it["moment_id"]],
                         "cuts": [{"src": it["moment_id"], "in": a, "out": b_, "dur": ms3(b_ - a),
                                   "desc": f"[액션] {(m.get('who') or '')} {m['desc']}".strip()}],
                         "dur": ms3(b_ - a)})
        elif t == "N":
            path, sec = tts_fn(it["text"])
            rows.append({"i": k, "mode": "N", "text": it["text"], "effect": it.get("effect"), "plan_sec": narration_plan_sec(it["text"]),
                         "tts": str(path), "dur": sec, "src": [], "cuts": []})
            if it.get("production_plan"):
                import copy
                rows[-1]["production_plan"] = copy.deepcopy(it["production_plan"])
    return rows


def table_matches_version(table: dict, version: dict, lines_by_id: dict | None = None) -> bool:
    """캐시된 테이블이 지금 대본과 같은 문구인가 — 제목 · 항목 수 · 행별(모드, N 문장, 효과자막). 가이드 벨트가 문구를 고치면
    테이블을 다시 만들어야 화면 문구가 바뀐다(2026-09-11). 순수 — 테스트 대상."""
    if (table.get("version") or {}).get("title") != version.get("title"):
        return False
    rows = table.get("rows") or []
    items = version.get("items") or []
    if len(rows) != len(items):
        return False
    for r, it in zip(rows, items):
        if r.get("mode") != it.get("type") or (r.get("effect") or None) != (it.get("effect") or None):
            return False
        if it.get("type") == "N" and (r.get("text") or "") != (it.get("text") or ""):
            return False
        if it.get("type") == "S" and lines_by_id is not None:     # 화자 교정(voice_check·speaker_fixes) 뒤엔 라벨이 달라진다
            cur = lines_by_id.get((it.get("line_ids") or [None])[0], {}).get("speaker")
            if cur and (r.get("speaker") or None) != cur:
                return False
    return True


def rows_fingerprint(rows: list[dict]) -> str:
    """agentic 제안 캐시의 열쇠 — 행의 모드·문구·S/A 구간이 하나라도 다르면 다른 대본이다(확인 패스가 대본을 바꾸면 자동 무효)."""
    key = [(r["i"], r["mode"], r.get("text", ""), [(c["in"], c["out"]) for c in r["cuts"]] if r["mode"] != "N" else []) for r in rows]
    return hashlib.sha1(json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _context_for(rows: list[dict], k: int) -> tuple[float | None, float | None, str]:
    """N 행 k 의 앞뒤 S/A 행 시각(문맥 구간) → (t_before, t_after, 설명)."""
    before = next((r for r in reversed(rows[:k]) if r["mode"] in ("S", "A")), None)
    after = next((r for r in rows[k+1:] if r["mode"] in ("S", "A")), None)
    tb = before["cuts"][0]["in"] if before else None
    ta = after["cuts"][0]["in"] if after else None
    parts = []
    if before:
        parts.append(f"직전 {before['mode']}행 {fmt_tc(before['cuts'][0]['in'])} {before.get('speaker','')}: {before['text'][:30]}")
    if after:
        parts.append(f"직후 {after['mode']}행 {fmt_tc(after['cuts'][0]['in'])} {after.get('speaker','')}: {after['text'][:30]}")
    return tb, ta, " / ".join(parts)


AGENTIC_SCHEMA = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"},
                       "cuts": {"type": "array", "items": {
                           "type": "object",
                           "properties": {"start": {"type": "string", "maxLength": 12}, "end": {"type": "string", "maxLength": 12},
                                          "who": {"type": "string", "maxLength": 20}, "desc": {"type": "string", "maxLength": 40}},
                           "required": ["start", "end"]},
                                "maxItems": 4}},
        "required": ["i", "cuts"]},
                             "maxItems": 12}},
    "required": ["rows"]}
# ⚠ maxLength/maxItems 는 장식이 아니다 — 2026-09-09 실측에서 제약 없는 스키마로 agentic 을 부르자 `who` 한 필드가
# 같은 구절을 216KB 반복하는 런어웨이가 났다(스키마 제약 디코딩 + agentic 조합). 출력 상한(AGENTIC_MAX_OUTPUT)과 한 벌.
AGENTIC_MAX_OUTPUT = 16384       # thought 와 나눠 쓰는 상한 — 4,096 은 thinking 이 다 먹었다(실측)

NEARBY_PAD_SEC = 180.0


def nearby_moments(index: dict, anchors: list[float], *, pad: float = NEARBY_PAD_SEC, min_count: int = 8) -> list[dict]:
    """index 경로 후보 목록 — 대본이 쓰는 S/A 행 시각(anchors) ±pad 안의 순간만. 너무 적으면 전량(폴백).
    (실측: 전량을 주면 모델이 8분·28분 지점의 무관한 장면을 골랐다 — 리액션 컷은 그 장면 근처여야 한다.)"""
    if not anchors:
        return list(index["moments"])
    lo, hi = min(anchors) - pad, max(anchors) + pad
    near = [m for m in index["moments"] if lo <= m["start"] <= hi]
    return near if len(near) >= min_count else list(index["moments"])


def _moment_sources(ids: list[str], index: dict, cuts: list[float], duration: float) -> list[CutSource]:
    by_id = {m["id"]: m for m in index["moments"]}
    out: list[CutSource] = []
    for mid in ids:
        m = by_id.get(mid)
        if not m:
            continue
        a = nudge_in(cuts, m["start"])
        _lo, hi = safe_shot(cuts, max(a, m["end"] - 0.01), duration)      # 순간 끝이 속한 샷 끝까지 연장 가능(제안 구간은 통째로 포함)
        if hi - a >= 0.5:
            out.append(CutSource(mid, ms3(a), hi, f"[{m['kind']}] {(m.get('who') or '')} {m['desc']}".strip(), prop_out=nudge_out(cuts, m["end"])))
    return out


def choose_sources_index(gemini: Gemini, version: dict, rows: list[dict], index: dict, cuts: list[float], duration: float,
                         log=print) -> dict[int, list[CutSource]]:
    """index 경로 — 텍스트 온리로 순간 ID 를 고른다."""
    row_lines = []
    for r in rows:
        if r["mode"] == "N":
            _tb, _ta, ctx = _context_for(rows, rows.index(r))
            row_lines.append(f"{r['i']}. [N] \"{r['text']}\" (실측 {r['dur']:.1f}s) — 문맥: {ctx}")
        else:
            row_lines.append(f"{r['i']}. [{r['mode']}] {fmt_tc(r['cuts'][0]['in'])}~{fmt_tc(r['cuts'][0]['out'])} {r.get('speaker','')} {r['text'][:40]}")
    anchors = [r["cuts"][0]["in"] for r in rows if r["mode"] in ("S", "A")]
    cand = nearby_moments(index, anchors)
    lo, hi = (min(anchors) - NEARBY_PAD_SEC, max(anchors) + NEARBY_PAD_SEC) if anchors else (0.0, duration)
    idx_lines = [f"{s['id']} [{fmt_tc(s['start'])}~{fmt_tc(s['end'])}] {s.get('place','')} — {s.get('summary','')}"
                 for s in index["scenes"] if s["end"] >= lo and s["start"] <= hi] or \
                [f"{s['id']} [{fmt_tc(s['start'])}~{fmt_tc(s['end'])}] {s.get('place','')} — {s.get('summary','')}" for s in index["scenes"]]
    idx_lines.append("")
    idx_lines += [f"{m['id']} [{fmt_tc(m['start'])}~{fmt_tc(m['end'])}] ({m['kind']}) {(m.get('who') or '')} {m['desc']}" for m in cand]
    prompt = TABLE_PROMPT.format(n=version["n"], strategy=version["strategy"], title=version["title"], cps=int(NARRATION_CHARS_PER_SEC),
                                 rows="\n".join(row_lines), index="\n".join(idx_lines))
    raw = gemini.text_json(prompt, kind="table_index", thinking="medium")
    out: dict[int, list[CutSource]] = {}
    for rr in raw.get("rows") or []:
        try:
            i = int(rr.get("i"))
        except (TypeError, ValueError):
            continue
        out[i] = _moment_sources([str(x) for x in (rr.get("cut_sources") or [])], index, cuts, duration)
    return out


def parse_agentic_rows(raw: dict, cuts: list[float], duration: float, avoid: list[tuple[float, float]]) -> dict[int, list[CutSource]]:
    """agentic 응답 → 행별 CutSource(샷 안전 구간 스냅). 순수 — 테스트 대상."""
    out: dict[int, list[CutSource]] = {}
    for rr in raw.get("rows") or []:
        try:
            i = int(rr.get("i"))
        except (TypeError, ValueError):
            continue
        srcs: list[CutSource] = []
        for k, c in enumerate(rr.get("cuts") or [], 1):
            try:
                a, b = parse_tc(c["start"]), parse_tc(c["end"])
            except (KeyError, ValueError, TypeError):
                continue
            if not (0 <= a < b <= duration + 0.5):
                continue
            if any(a < e and b > s for s, e in avoid):
                continue
            a2 = nudge_in(cuts, a)
            b2 = nudge_out(cuts, b)
            _lo, hi = safe_shot(cuts, max(a2, b2 - 0.01), duration)      # 제안 끝이 속한 샷 끝까지 연장 가능
            if hi - a2 >= 0.5 and b2 > a2:
                srcs.append(CutSource(f"AG-{i}-{k}", a2, hi, f"[agentic] {(c.get('who') or '')} {c.get('desc') or ''}".strip(),
                                      prop_out=b2))
        out[i] = srcs
    return out


def choose_sources_agentic(job: Job, gemini: Gemini, version: dict, rows: list[dict], proxy: Path, cuts: list[float],
                           duration: float, *, title: str, exclude: list[tuple[float, float]] | None = None, tag: str = "") -> tuple[dict[int, list[CutSource]], dict]:
    exclude = exclude or []
    avoid = [(r["cuts"][0]["in"], r["cuts"][0]["out"]) for r in rows if r["mode"] == "S"] + list(exclude)
    blocks = []
    for k, r in enumerate(rows):
        if r["mode"] == "N":
            tb, ta, ctx = _context_for(rows, k)
            anchor = [t for t in (tb, ta) if t is not None]
            win = f"{fmt_tc(min(anchor)-40)}~{fmt_tc(max(anchor)+40)}" if anchor else "(문맥 없음 — 제목이 가리키는 장면)"
            blocks.append(f"행 {r['i']} [N] \"{r['text']}\" — 필요 길이 {r['dur']:.1f}s · [문맥 구간] {win} · {ctx}")
        elif r["mode"] == "A":
            c = r["cuts"][0]
            blocks.append(f"행 {r['i']} [A] 현장음 {r['text']} — {c['desc']} · 인덱스 추정 {fmt_tc(c['in'])}~{fmt_tc(c['out'])}")
    blocks.append("[피할 구간] " + ", ".join(f"{fmt_tc(s)}~{fmt_tc(e)}" for s, e in avoid))
    if exclude:
        blocks.append("[활용 불가 구간] " + ", ".join(f"{fmt_tc(s)}~{fmt_tc(e)}" for s, e in exclude))
    prompt = AGENTIC_PROMPT.format(title=title, rows="\n".join(blocks))
    cache_name = f"table_agentic_raw_v{version['n']}{'_' + tag if tag else ''}.json"
    fp = rows_fingerprint(rows)
    saved = job.load(cache_name) if job.has(cache_name) else None
    if saved and saved.get("fingerprint") == fp:   # 같은 대본이면 같은 제안 재사용(결정성 · 호출 0). 새로 받으려면 --redo agentic
        job.log(f"[table] agentic 제안 캐시 재사용 {cache_name}")
        raw, meta = saved["raw"], dict(saved.get("meta") or {}, cached=True)
    else:
        if saved:
            job.log("[table] 대본이 바뀌어 agentic 제안 캐시 무효 → 재탐색")
        raw, meta = gemini.agentic_video_json(prompt, proxy, kind="table_agentic", schema=AGENTIC_SCHEMA,
                                              upload_cache=job.path("files_cache.json"), max_output_tokens=AGENTIC_MAX_OUTPUT)
        job.save(cache_name, {"meta": meta, "raw": raw, "fingerprint": fp})
    parsed = parse_agentic_rows(raw, cuts, duration, avoid)
    return {i: drop_excluded_sources(v, exclude) for i, v in parsed.items()}, meta


MAX_DRIFT_SEC = 3.0   # 깎인 조각이 제안 끝에서 이만큼 넘게 뒤에서 시작하면 그 소스는 버린다(2026-09-12 — 같은 '샷'이라도 40s 뒤는 딴 내용)


def trim_source(src: CutSource, used: list[tuple[float, float]], *, min_sec: float = 0.5, max_drift: float = MAX_DRIFT_SEC) -> CutSource | None:
    """이미 쓴 구간(used)과 겹치지 않도록 소스의 가용 창을 깎는다. 남는 조각은 **제안 구간(avail_in~prop_out)과 겹치는 쪽**을 먼저,
    같으면 긴 쪽을 취하고 min_sec 미만이면 버린다. (행 간 컷 중복 제거 — 2026-09-10 사용자 지시.)

    ⚠ 2026-09-12 교정(1화 v1 35초 검은 화면): 종전엔 무조건 긴 쪽을 취해, 어두운 장면에서 샷 경계가 안 잡혀 가용 창이 42s 로
    늘어난 소스가 다른 행이 쓴 구간을 피하며 제안(48:08)에서 40s 떨어진 페이드(48:48)까지 밀려났다. 제안 쪽 조각을 우선하고,
    prop_out 이 있는 소스는 조각 시작이 제안 끝 + max_drift 를 넘으면 버린다(호출자가 다른 후보·인접 샷으로 채운다)."""
    a, b = src.avail_in, src.avail_out
    p0, p1 = src.avail_in, (src.prop_out if src.prop_out and src.prop_out > src.avail_in else src.avail_out)

    def _score(w: tuple[float, float]) -> tuple[float, float]:
        return (max(0.0, min(w[1], p1) - max(w[0], p0)), w[1] - w[0])

    for u0, u1 in sorted(used):
        if u1 <= a or u0 >= b:
            continue
        left = (a, min(b, u0))
        right = (max(a, u1), b)
        a, b = max((left, right), key=_score)
        if b - a < min_sec:
            return None
    if b - a < min_sec:
        return None
    if src.prop_out and a > src.prop_out + max_drift:
        return None
    return CutSource(src.id, ms3(a), ms3(b), src.desc, prop_out=src.prop_out)


def _overlap(x: dict, y: dict) -> float:
    return max(0.0, min(x["out"], y["out"]) - max(x["in"], y["in"]))


def drop_excluded_sources(srcs: list[CutSource], exclude: list[tuple[float, float]]) -> list[CutSource]:
    """활용 불가 구간에 조금이라도 걸치는 후보 소스는 버린다(깎지 않는다 — 경계에서 잘라 쓰면 그 장면의 앞뒤가 새어 나간다). 순수."""
    if not exclude:
        return list(srcs)
    return [x for x in srcs if not in_excluded(x.avail_in, x.avail_out, exclude)]


def cuts_in_excluded(rows: list[dict], exclude: list[tuple[float, float]]) -> list[str]:
    """최종 테이블의 모든 컷 대조 — 활용 불가 구간에 걸치는 컷 목록(행·소스·구간). 비어 있어야 정상. 순수 — 테스트 대상."""
    bad: list[str] = []
    for r in rows:
        for c in r["cuts"]:
            if in_excluded(c["in"], c["out"], exclude):
                bad.append(f"행{r['i']}[{r['mode']}] {c.get('src')} {fmt_tc(c['in'])}~{fmt_tc(c['out'])}")
    return bad


BLACK_MARGIN_SEC = 0.2      # 페이드 가장자리는 blackdetect 임계 바로 위라 여전히 검다 — 양쪽으로 이만큼 더 깎는다
BLACK_BELT_MIN_SEC = 0.3    # N 행 컷이 검은 구간과 이만큼 넘게 겹치면 벨트 위반(암전 플래시 0.1~0.2s 는 소스의 연출)


def carve_black(srcs: list[CutSource], black: list[tuple[float, float]], *, margin: float = BLACK_MARGIN_SEC,
                min_sec: float = 0.8) -> list[CutSource]:
    """N/A 컷 후보에서 검은 화면 구간(±margin)을 깎는다 — 활용 불가와 달리 **버리지 않고 깎는다**(검정은 화면 성질이라 앞뒤가 새지
    않는다). 제안 구간 쪽 조각을 남기고(trim_source 규칙) min_sec 미만이면 버린다. 순수 — 테스트 대상."""
    if not black:
        return list(srcs)
    spans = [(max(0.0, a - margin), b + margin) for a, b in black]
    out: list[CutSource] = []
    for x in srcs:
        t = trim_source(x, spans, min_sec=min_sec, max_drift=float("inf"))
        if t:
            out.append(t)
    return out


def cuts_in_black(rows: list[dict], black: list[tuple[float, float]], *, min_overlap: float = BLACK_BELT_MIN_SEC,
                  modes: tuple[str, ...] = ("N",)) -> list[str]:
    """완성 테이블의 N 행 컷 ↔ 검은 구간 대조 — 겹침이 min_overlap 을 넘는 컷 목록. 비어 있어야 정상(S 행은 립싱크라 제외 —
    페이드 위 목소리는 소스의 진실). 순수 — 테스트 대상."""
    bad: list[str] = []
    for r in rows:
        if r.get("mode") not in modes:
            continue
        for c in r.get("cuts") or []:
            ov = sum(max(0.0, min(c["out"], e) - max(c["in"], s)) for s, e in black)
            if ov > min_overlap:
                bad.append(f"행{r['i']}[{r['mode']}] {c.get('src')} {fmt_tc(c['in'])}~{fmt_tc(c['out'])} 검정 {ov:.2f}s")
    return bad


def trim_a_rows_off_black(rows: list[dict], black: list[tuple[float, float]], *, margin: float = BLACK_MARGIN_SEC,
                          min_sec: float = 0.8, min_overlap: float = 0.5) -> list[str]:
    """A 행(현장음) 컷이 검은 화면과 min_overlap 넘게 겹치면 검은 구간(±margin)을 깎아 낸다 — 남는 조각이 min_sec 이상일 때만(행은 안 지운다).
    2026-09-13 3화 v9 실측: 인덱스 순간 S-057(33:18~33:22.5)이 암전 위 소리라 3.4s 가 검은 화면이었다. 못 깎으면 ⚠ 기록. 순수 — 테스트 대상."""
    notes: list[str] = []
    if not black:
        return notes
    spans = [(max(0.0, a - margin), b + margin) for a, b in black]
    for r in rows:
        if r["mode"] != "A" or not r["cuts"]:
            continue
        c = r["cuts"][0]
        ov = sum(max(0.0, min(c["out"], e) - max(c["in"], s)) for s, e in black)
        if ov <= min_overlap:
            continue
        src = trim_source(CutSource(c["src"], c["in"], c["out"], c.get("desc", ""), prop_out=c["out"]), spans, min_sec=min_sec, max_drift=float("inf"))
        if src is None:
            notes.append(f"행{r['i']}: ⚠ A 컷 {fmt_tc(c['in'])}~{fmt_tc(c['out'])} 이 검은 화면 {ov:.1f}s 를 품는데 깎을 여지가 없다(암전 위 소리)")
            continue
        notes.append(f"행{r['i']}: A 컷 {fmt_tc(c['in'])}~{fmt_tc(c['out'])} 검은 화면 {ov:.1f}s → {fmt_tc(src.avail_in)}~{fmt_tc(src.avail_out)} 로 깎음")
        c["in"], c["out"], c["dur"] = ms3(src.avail_in), ms3(src.avail_out), ms3(src.avail_out - src.avail_in)
        r["dur"] = c["dur"]
    return notes


def apply_agentic_a_rows(rows: list[dict], sources: dict[int, list[CutSource]], cuts: list[float], duration: float,
                         *, window: float = A_REFINE_WINDOW_SEC, black: list[tuple[float, float]] | None = None) -> list[str]:
    """A 행: agentic 이 찾은 '소리 나는 동작 구간'으로 인덱스 추정을 교체한다. 인덱스 추정에서 window 초 밖이면 무시(기록).
    순수 — rows 를 제자리에서 갱신하고 메모를 돌려준다."""
    notes: list[str] = []
    for r in rows:
        if r["mode"] != "A":
            continue
        cand = sources.get(r["i"]) or []
        base = r["cuts"][0]
        r["cut_origin"] = "index"
        if not cand:
            continue
        src = cand[0]
        est_mid = (base["in"] + base["out"]) / 2
        if abs(src.avail_in - est_mid) > window:
            notes.append(f"행{r['i']}: agentic A 제안 {fmt_tc(src.avail_in)} 이 인덱스 추정에서 {abs(src.avail_in-est_mid):.1f}s 떨어짐 → 무시")
            continue
        prop_end = src.prop_out if src.prop_out and src.prop_out > src.avail_in else src.avail_in + 1.5
        a, b = action_window(cuts, src.avail_in, min(prop_end, src.avail_in + 3.0), duration, min_sec=0.8)
        s_spans = [(c["in"], c["out"]) for o in rows if o["mode"] == "S" for c in o["cuts"]]
        if any(a < e0 - 0.3 and b > s0 + 0.3 for s0, e0 in s_spans):     # 대사 구간을 다시 보여 주는 제안은 거절(같은 장면 두 번 — 2026-09-11 v14·v2)
            notes.append(f"행{r['i']}: agentic A 제안 {fmt_tc(a)}~{fmt_tc(b)} 이 대사 행 구간과 겹침 → 인덱스 추정 유지")
            continue
        blk = sum(max(0.0, min(b, e0) - max(a, s0)) for s0, e0 in (black or []))
        if blk > BLACK_BELT_MIN_SEC:                                   # 페이드 위 현장음 — 화면이 검다(2026-09-12)
            notes.append(f"행{r['i']}: agentic A 제안 {fmt_tc(a)}~{fmt_tc(b)} 이 검은 화면 {blk:.2f}s 를 품음 → 인덱스 추정 유지")
            continue
        r["cuts"] = [{"src": src.id, "in": a, "out": b, "dur": ms3(b - a), "desc": f"[액션] {src.desc.replace('[agentic] ', '')}"}]
        r["dur"] = ms3(b - a)
        r["cut_origin"] = "agentic"
        notes.append(f"행{r['i']}: A 현장음 {fmt_tc(base['in'])}~{fmt_tc(base['out'])} → agentic {fmt_tc(a)}~{fmt_tc(b)}")
    return notes


def lead_scene_for_row(rows: list[dict], k: int, scenes: list[dict]) -> tuple[float, float] | None:
    """N 행 k 의 리드인 장면 — **다음** S/A 행의 장면(내레이션은 보통 다음 대사를 이끈다), 없으면 앞 S/A 의 장면. 장면 목록이 없으면 None.
    순수 — 테스트 대상."""
    if not scenes:
        return None
    after = next((r for r in rows[k+1:] if r["mode"] in ("S", "A") and r["cuts"]), None)
    before = next((r for r in reversed(rows[:k]) if r["mode"] in ("S", "A") and r["cuts"]), None)
    for r in (after, before):
        if r is None:
            continue
        t = r["cuts"][0]["in"]
        sc = next((s for s in scenes if s["start"] <= t < s["end"]), None)
        if sc:
            return (float(sc["start"]), float(sc["end"]))
    return None


def scene_span_for_row(rows: list[dict], k: int, scenes: list[dict], duration: float) -> tuple[float, float]:
    """N 행 k 의 문맥(앞뒤 S/A 행)이 속한 장면(들)의 [시작, 끝] — 컷 보충은 이 안에서만(다른 장면 인물 난입 방지, 2026-09-10 실측)."""
    tb, ta, _ = _context_for(rows, k)
    anchors = [t for t in (tb, ta) if t is not None]
    if not anchors or not scenes:
        return 0.0, duration
    lo, hi = duration, 0.0
    for t in anchors:
        sc = next((s for s in scenes if s["start"] <= t < s["end"]), None)
        if sc:
            lo, hi = min(lo, sc["start"]), max(hi, sc["end"])
    return (lo, hi) if hi > lo else (min(anchors) - 30.0, max(anchors) + 30.0)


def fill_n_rows(rows: list[dict], sources: dict[int, list[CutSource]], fallback: dict[int, list[CutSource]],
                cuts: list[float], duration: float, log=print, scenes: list[dict] | None = None,
                exclude: list[tuple[float, float]] | None = None, black: list[tuple[float, float]] | None = None) -> list[str]:
    """N 행에 컷을 쌓는다. 부족하면 **같은 장면 안의** 인접 샷 → 같은 장면 안의 fallback 소스 순으로 보충. 미달은 기록.
    **행 간·행 안 컷 시간 겹침 0** — S/A 행 구간과 앞서 채운 N 행 컷을 used 로 두고 소스를 깎는다.
    black = 검은 화면 구간 — 모든 후보(agentic·index·인접 샷)에서 깎는다(2026-09-12)."""
    notes: list[str] = []
    ex = exclude or []
    blk = black or []
    used: list[tuple[float, float]] = [(c["in"], c["out"]) for r in rows if r["mode"] in ("S", "A") for c in r["cuts"]]
    # 같은 샷 재사용 회피(2026-09-12, 2화 실측 "중복 영상이 계속 나온다"): S/A 행이 보여준 샷(장면 컷 경계 사이)과 앞 N 행이 쓴 샷은
    # 뒤 N 행 후보에서 **뒤로 미룬다** — 같은 프레이밍이 대사 앞뒤로 또 나오면 시청자는 같은 장면이 두 번 나온다고 본다.
    # 다른 샷으로 못 채울 때만 같은 샷을 허용(기록).
    # 회피 대상 = **이미 보여준 샷**(출력 순서상 앞 행의 S/A 컷 + 앞서 채운 N 컷). 뒤 행의 S/A 와 같은 샷이라도 그 대사보다 **앞선 구간**은
    # 연속 리드인(통화 중인 모습 → 대사)이라 허용하고, 그 대사 **뒤** 구간(되감기)은 회피한다(2026-09-13 v8 실측 — 옥상 통화 컷이 밀리고 문 앞 샷이 붙었다).
    used_shots: set[int] = set()
    later_shot_start: dict[int, float] = {}
    same_shot_rows: list[int] = []

    lead_scene: list[tuple[float, float] | None] = [None]        # 지금 채우는 N 행의 '리드인 장면'(다음 S/A 의 장면, 없으면 앞 S/A 의 장면)

    def _is_same_shot(x: CutSource) -> bool:
        sh = shot_index(cuts, x.avail_in)
        if sh in used_shots:
            return True
        st = later_shot_start.get(sh)
        return st is not None and x.avail_in >= st                  # 뒤 대사와 같은 샷인데 그 대사 뒤 구간 → 되감기

    def _stack_dedup(srcs: list[CutSource], target: float, allow_out: bool = False) -> tuple[list[dict], float]:
        srcs = [t for t in (trim_source(x, used) for x in carve_black(drop_excluded_sources(srcs, ex), blk)) if t]
        ls = lead_scene[0]
        in_lead = (lambda x: ls is None or (ls[0] <= x.avail_in < ls[1]))
        if not allow_out:                                   # 다른 장면 후보는 ③ 폴백(같은 장면이 다 막혔을 때)에서만 연다
            srcs = [x for x in srcs if in_lead(x)]
        # 우선순위: 리드인 장면 ∧ 새 샷 → 리드인 장면 ∧ 같은 샷 → 다른 장면 ∧ 새 샷 → 나머지 (2026-09-13 v8 실측: "옥상에서 전화로 따지는" 내레이션에
        # 집 계단 컷이 붙었다 — 옥상 컷이 다음 대사와 같은 샷이라 밀리고 다른 장면의 '새 샷'이 이겼다. 장면이 맞는 것이 샷이 다른 것보다 먼저다)
        tiers = [[x for x in srcs if in_lead(x) and not _is_same_shot(x)],
                 [x for x in srcs if in_lead(x) and _is_same_shot(x)],
                 [x for x in srcs if not in_lead(x) and not _is_same_shot(x)],
                 [x for x in srcs if not in_lead(x) and _is_same_shot(x)]]
        pool = list(tiers[0])
        stacked, short = stack_cuts(pool, target)
        for k_t, tier in enumerate(tiers[1:], 1):
            if short <= 0.02 or not tier:
                continue
            cand = pool + tier
            stacked2, short2 = stack_cuts(cand, target)
            if short2 < short:
                stacked, short, pool = stacked2, short2, cand
                if k_t in (1, 3):
                    same_shot_rows.append(1)
        for _ in range(6):                      # 행 안에서 소스끼리 겹치면 뒤 소스를 앞 컷에 맞춰 깎고 다시 쌓는다
            clash = next(((i, j) for i in range(len(stacked)) for j in range(i + 1, len(stacked))
                          if _overlap(stacked[i], stacked[j]) > 0.001), None)
            if clash is None:
                break
            i, j = clash
            by_id = {x.id: x for x in pool}
            later = by_id.get(stacked[j]["src"])
            if later is None:
                break
            trimmed = trim_source(later, [(stacked[i]["in"], stacked[i]["out"])])
            pool = [x for x in pool if x.id != later.id] + ([trimmed] if trimmed else [])
            stacked, short = stack_cuts(list(pool), target)
        return stacked, short

    for k, r in enumerate(rows):
        if r["mode"] != "N":
            continue
        lo, hi = scene_span_for_row(rows, k, scenes or [], duration)
        in_scene = lambda x: lo <= x.avail_in < hi  # noqa: E731
        lead_scene[0] = lead_scene_for_row(rows, k, scenes or [])
        used_shots = {shot_index(cuts, c["in"]) for r2 in rows[:k] if r2["mode"] in ("S", "A") for c in r2["cuts"]} | \
                     {shot_index(cuts, c["in"]) for r2 in rows[:k] if r2["mode"] == "N" for c in r2["cuts"]}
        later_shot_start = {}
        for r2 in rows[k+1:]:
            if r2["mode"] in ("S", "A"):
                for c in r2["cuts"]:
                    sh = shot_index(cuts, c["in"])
                    later_shot_start[sh] = min(later_shot_start.get(sh, c["in"]), c["in"])
        srcs = list(sources.get(r["i"]) or [])
        origin = "agentic" if srcs else "index"
        if not srcs:
            srcs = [x for x in (fallback.get(r["i"]) or []) if in_scene(x)]
        stacked, short = _stack_dedup(srcs, r["dur"])
        if short > 0:                                            # ① 같은 장면 안 인접 샷(앞뒤 대사 근처)
            last_t = stacked[-1]["out"] if stacked else lo
            adj = [CutSource(f"SC@{fmt_tc(a)}", a, min(b, hi), "[인접 샷] 보충")
                   for a, b in next_shots_after(cuts, last_t, min(hi, duration), count=12) if a < hi]
            stacked2, short2 = _stack_dedup(srcs + adj, r["dur"])
            if short2 < short:
                stacked, short = stacked2, short2
                notes.append(f"행{r['i']}: 소스 부족 → 같은 장면 인접 샷 보충")
        if short > 0 and fallback.get(r["i"]):                    # ② 같은 장면 안 index 순간
            extra = [x for x in fallback[r["i"]] if in_scene(x) and x.id not in {c["src"] for c in stacked}]
            stacked3, short3 = _stack_dedup(srcs + extra, r["dur"])
            if short3 < short:
                stacked, short = stacked3, short3
                notes.append(f"행{r['i']}: 같은 장면 index 순간 보충")
        if short >= 0.8 or not stacked:                          # ③ 그래도 비면: 같은 장면 **앞쪽** 샷 → 장면 밖(뒤·앞) 샷 — 활용 불가 구간에
            anchor = stacked[0]["in"] if stacked else (tb if (tb := _context_for(rows, k)[0]) is not None else lo)   #    뒤쪽이 통째로 막힌 실측(2화 v5)
            cands = [CutSource(f"SC<{fmt_tc(a)}", max(a, lo), b, "[앞 샷] 보충") for a, b in prev_shots_before(cuts, anchor, count=12) if b > lo]
            cands += [CutSource(f"SC>{fmt_tc(a)}", a, b, "[장면 밖 뒤 샷] 보충") for a, b in next_shots_after(cuts, hi, duration, count=12)]
            cands += [CutSource(f"SC<<{fmt_tc(a)}", a, b, "[장면 밖 앞 샷] 보충") for a, b in prev_shots_before(cuts, lo, count=12)]
            stacked4, short4 = _stack_dedup(srcs + [CutSource(c["src"], c["in"], c["out"], c.get("desc", "")) for c in stacked] + cands, r["dur"],
                                            allow_out=True)
            if short4 < short:
                stacked, short = stacked4, short4
                notes.append(f"행{r['i']}: 같은 장면 뒤쪽이 막혀(활용 불가/사용됨) 앞 샷·장면 밖 샷으로 보충")
        if not stacked:
            notes.append(f"행{r['i']}: ⚠⚠ 컷 0개 — 내레이션 {r['dur']:.2f}s 동안 화면이 없다(전 후보가 활용 불가/사용됨)")
        elif short > 0.02:
            notes.append(f"행{r['i']}: ⚠ 미달 {short:.2f}s (내레이션 {r['dur']:.2f}s) — 마지막 컷이 그만큼 짧게 끝난다")
        r["cuts"] = stacked
        r["cut_origin"] = origin
        r["video_dur"] = ms3(sum(c["dur"] for c in stacked))
        r["shortfall"] = short
        used += [(c["in"], c["out"]) for c in stacked]
        if same_shot_rows:
            notes.append(f"행{r['i']}: 다른 샷으로 못 채워 이미 보여준 샷을 재사용")
            same_shot_rows.clear()
    return notes


def shot_index(cuts: list[float], t: float) -> int:
    """t 가 속한 샷 번호(장면 컷 경계 목록 기준). 순수 — 테스트 대상."""
    import bisect
    return bisect.bisect_right(cuts, t)


def trim_a_rows_off_dialogue(rows: list[dict], *, min_sec: float = 0.8) -> list[str]:
    """A 행 컷이 S 행(립싱크) 구간과 겹치면 겹치는 쪽을 잘라낸다 — 같은 화면이 두 번 나오지 않게(2026-09-12 2화 v1 실측: 현장음 3.0s 가 대사
    3.0s 와 통째로 겹침). 남는 조각이 min_sec 미만이면 못 자르고 기록만 한다(행은 지우지 않는다 — 행↔항목 1:1). 순수 — 테스트 대상."""
    notes: list[str] = []
    s_spans = [(c["in"], c["out"]) for r in rows if r["mode"] == "S" for c in r["cuts"]]
    for r in rows:
        if r["mode"] != "A" or not r["cuts"]:
            continue
        c = r["cuts"][0]
        a, b = c["in"], c["out"]
        for s0, e0 in s_spans:
            if min(b, e0) - max(a, s0) <= 0.05:
                continue
            left, right = (a, min(b, s0)), (max(a, e0), b)
            na, nb = max((left, right), key=lambda w: w[1] - w[0])
            if nb - na >= min_sec:
                notes.append(f"행{r['i']}: A 컷 {fmt_tc(a)}~{fmt_tc(b)} 이 대사 구간 {fmt_tc(s0)}~{fmt_tc(e0)} 과 겹침 → {fmt_tc(na)}~{fmt_tc(nb)} 로 잘라냄")
                a, b = na, nb
            else:
                notes.append(f"행{r['i']}: ⚠ A 컷 {fmt_tc(a)}~{fmt_tc(b)} 이 대사 구간과 겹치는데 잘라낼 여지가 없다(같은 화면 두 번)")
        if (a, b) != (c["in"], c["out"]):
            c["in"], c["out"], c["dur"] = ms3(a), ms3(b), ms3(b - a)
            r["dur"] = c["dur"]
    return notes


def build_table(job: Job, gemini: Gemini, rebuild: dict, index: dict, transcript: dict, cuts: list[float], duration: float, proxy: Path,
                *, version_n: int, title: str, cut_search: str = "agentic", voice: str = "ko_female", speed: str = "normal",
                exclude: list[tuple[float, float]] | None = None, tag: str = "", black: list[tuple[float, float]] | None = None) -> dict:
    sfx = f"_{tag}" if tag else ""
    name = f"table_v{version_n}{sfx}.json"
    version = next(v for v in rebuild["versions"] if v["n"] == version_n)
    black = black or []
    if job.has(name):
        cached = job.load(name)
        if table_matches_version(cached, version, {l["id"]: l for l in transcript["lines"]}) and cached.get("voice") == voice and cached.get("speed") == speed:
            stale = cuts_in_black(cached.get("rows") or [], black)
            if not stale:
                return cached
            job.log(f"[table] v{version_n} 캐시된 테이블에 검은 화면 위 컷 {len(stale)}개 → 테이블 재생성: " + " · ".join(stale))
        else:
            job.log(f"[table] v{version_n} 캐시된 테이블의 대본·목소리가 지금과 다르다 → 테이블 재생성(컷 탐색 캐시는 지문이 같으면 재사용)")
    job.log(f"[table] v{version_n} {version['strategy']} — \"{version['title']}\" 항목 {len(version['items'])}")
    rows = rows_from_items(version, index, transcript, cuts, duration, lambda text: _tts_cached(job, text, voice, speed),
                           end_fn=lambda we, limit: speech_end_after(job, we, limit=limit))
    n_rows = [r for r in rows if r["mode"] == "N"]
    job.log(f"[table] 행 {len(rows)} (N {len(n_rows)} · S {sum(r['mode']=='S' for r in rows)} · A {sum(r['mode']=='A' for r in rows)}) · "
            f"내레이션 실측 합 {sum(r['dur'] for r in n_rows):.1f}s (계획 {sum(r['plan_sec'] for r in n_rows):.1f}s)")
    # 컷 소스 — index 는 늘 만든다(폴백), agentic 은 옵션
    exclude = exclude or []
    fallback = {i: drop_excluded_sources(v, exclude) for i, v in
                choose_sources_index(gemini, version, rows, index, cuts, duration, log=job.log).items()}
    agentic_meta: dict = {}
    sources: dict[int, list[CutSource]] = {}
    if cut_search == "agentic":
        try:
            sources, agentic_meta = choose_sources_agentic(job, gemini, version, rows, proxy, cuts, duration, title=title, exclude=exclude, tag=tag)
            job.log(f"[table] agentic 컷 탐색 — 행 {len(sources)} · 컷 {sum(len(v) for v in sources.values())} · "
                    f"processing_call {agentic_meta.get('processing_calls')} · tokens {agentic_meta.get('total_tokens')}")
        except Exception as e:  # noqa: BLE001
            job.log(f"[table] ⚠ agentic 컷 탐색 실패 → index 경로로: {type(e).__name__}: {str(e)[:200]}")
            agentic_meta = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    notes = apply_agentic_a_rows(rows, sources, cuts, duration, black=black) if sources else []
    notes += trim_a_rows_off_dialogue(rows)           # 현장음 컷이 대사 컷과 겹치면 잘라낸다(같은 화면 두 번 — 2026-09-12)
    notes += trim_a_rows_off_black(rows, black)       # 현장음 컷이 암전 위면 검은 구간을 깎는다(2026-09-13 3화 v9)
    notes += fill_n_rows(rows, sources, fallback, cuts, duration, log=job.log, scenes=index.get("scenes"), exclude=exclude, black=black)
    for n in notes:
        job.log(f"[table] {n}")
    bad = cuts_in_excluded(rows, exclude)
    if bad:                                    # 권리사 '활용 불가' 장면이 완성본에 들어가면 삭제 요청·저작권 문제 — 조용히 송출하지 않는다
        job.log("[table] ⚠⚠ 활용 불가 구간 컷: " + " · ".join(bad))
        raise RuntimeError(f"활용 불가 구간에 걸치는 컷 {len(bad)}개 — {bad[:3]}")
    dark = cuts_in_black(rows, black)
    if dark:                                   # 내레이션이 검은 화면 위에 나가는 것(2026-09-12 v1 35s) — carve_black 이 막았어야 하므로 여기 걸리면 버그
        job.log("[table] ⚠⚠ 검은 화면 위 N 컷: " + " · ".join(dark))
        raise RuntimeError(f"검은 화면 구간에 걸치는 N 컷 {len(dark)}개 — {dark[:3]}")
    dark_a = cuts_in_black(rows, black, modes=("A",))
    if dark_a:                                 # A 행은 인덱스 추정 그대로일 수 있다 — 기록만(현장음이 우선)
        notes.append("⚠ A 행 컷이 검은 화면에 걸침: " + " · ".join(dark_a))
        job.log("[table] " + notes[-1])
    t = 0.0
    for r in rows:
        r["t0"] = ms3(t)
        r["dur_video"] = ms3(sum(c["dur"] for c in r["cuts"]))
        t += r["dur_video"] if r["mode"] == "N" else r["dur"]
    data = {"version": {k: version[k] for k in ("n", "strategy", "title", "structure", "analysis")}, "rows": rows,
            "total_sec": ms3(t), "voice": voice, "speed": speed, "cut_search": cut_search, "agentic": agentic_meta, "notes": notes}
    job.save(name, data)
    job.log(f"[table] 완료 — 총 {t:.1f}s · 컷 {sum(len(r['cuts']) for r in rows)}")
    job.record_step(f"table_v{version_n}{sfx}", rows=len(rows), total_sec=ms3(t), cut_search=cut_search, voice=voice, speed=speed,
                    agentic_rows=sum(1 for r in rows if r.get("cut_origin") == "agentic"), notes=notes, agentic=agentic_meta)
    return data
