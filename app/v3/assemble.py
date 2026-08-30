"""Stage 3 산출 → 동결 경계면 조립(순수 — LLM 없음).

어댑터 계약(orders/v3-m2-adapter-contract.md · ves docs/CONTRACTS.md 'V3 경계면 동결'):
  C1 edit_plan.timeline[] — clip_start_sec/clip_end_sec **원본 절대초** · role ·
     use_original_audio · subtitle, layout.top_title/bottom_label. additive 만 허용:
     schema="edit_plan/v3" · timeline[].span_ids · grid_marks.
  C2 tts cue — source_time_sec 가 좌표이자 신원. start_sec/end_sec 는 **편집본** 좌표.
     voice/speed 는 E11 라벨 그대로(신규 라벨 금지).
  C6 subtitle_segments.json — 편집본 좌표 {start_sec, end_sec, text}.

어절 자막 규칙(기획 §6): 2~4어절 · 최대 12자 · 어절 경계만 · 문장부호에서 라인 종료 ·
첫 어절 −0.05s 선행(스팬 시작 클램프) · 최소 노출 0.35s.
"""
from __future__ import annotations

CANVAS = "1080x1920"
AUDIO_MIX = {"tts_gain_db": -3, "original_gain_db": -3, "bgm_gain_db": -20}
SUB_LEAD_SEC = 0.05          # 첫 어절 선행
SUB_MIN_SEC = 0.35           # 최소 노출 — 미달이면 다음 라인 시작까지 연장
SUB_MAX_WORDS = 4
SUB_MAX_CHARS = 12           # 공백 포함 표시 길이
SENTENCE_ENDINGS = (".", "?", "!", "…")
CONTIG_EPS = 0.005           # span 이 소스에서 이어져 있다고 볼 잔차


# ── edit_plan 조립 ──────────────────────────────────────────────────────────

def assemble_edit_plan(story_doc: dict, span_index: dict[str, dict], *,
                       video_path: str, work_title: str) -> dict:
    """비트 편성 → C1 동결 필드의 edit_plan. 클립 = 비트 안 span 병합 단위.

    분할 지점: (a) 소스 시간 불연속(원거리는 비트가 나뉘므로 방어) (b) 뮤트 여부가
    바뀌는 곳 — use_original_audio 는 클립 단위 계약이라 뮤트 span 은 제 클립을 갖는다."""
    timeline: list[dict] = []
    for b in story_doc["beats"]:
        muted = set(b.get("muted_span_ids") or [])
        group: list[str] = []

        def flush(group: list[str], beat=b, muted=muted) -> None:
            if not group:
                return
            t0 = span_index[group[0]]["t_in"]
            t1 = span_index[group[-1]]["t_out"]
            timeline.append({
                "role": beat["role"],
                "clip_start_sec": round(t0, 3),
                "clip_end_sec": round(t1, 3),
                "subtitle": "",
                "use_original_audio": group[0] not in muted,
                "reframe": {"mode": "center"},
                "span_ids": list(group),
            })

        for sid in b["span_ids"]:
            if group:
                prev = span_index[group[-1]]
                cur = span_index[sid]
                broken = abs(cur["t_in"] - prev["t_out"]) > CONTIG_EPS
                mute_flip = (sid in muted) != (group[-1] in muted)
                if broken or mute_flip:
                    flush(group)
                    group = []
            group.append(sid)
        flush(group)
        # 라벨은 비트의 첫 클립에 싣는다(C1 subtitle 필드 — 편집실 오버레이 재료)
        if b.get("label"):
            first = len(timeline) - sum(
                1 for c in timeline if c["span_ids"][0] in set(b["span_ids"]))
            timeline[first]["subtitle"] = b["label"]

    grid_marks = sorted({round(span_index[s][k], 3)
                         for c in timeline for s in c["span_ids"]
                         for k in ("t_in", "t_out")})
    return {
        "schema": "edit_plan/v3",
        "input": {"video_path": video_path, "work_title": work_title,
                  "topic": "", "language": "ko"},
        "layout": {"canvas": CANVAS,
                   "top_title": f"{story_doc['title']['line1']}\n"
                                f"{story_doc['title']['line2']}",
                   "bottom_label": work_title,
                   "background_style": "blur", "video_speed": 1.0},
        "timeline": timeline,
        "audio_mix": dict(AUDIO_MIX),
        "grid_marks": grid_marks,
    }


# ── 소스 ↔ 편집본 좌표 ─────────────────────────────────────────────────────

def edited_offsets(timeline: list[dict]) -> list[tuple[float, float, float]]:
    """클립별 (소스 시작, 소스 끝, 편집본 오프셋) — 편성 순서 누적."""
    out = []
    off = 0.0
    for c in timeline:
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        out.append((s, e, off))
        off += e - s
    return out


def to_edited_sec(source_sec: float,
                  offsets: list[tuple[float, float, float]]) -> float | None:
    """원본 절대초 → 편집본 초. 어느 클립에도 없으면 None(조용한 0 금지).

    같은 소스 구간이 두 번 편성될 일은 없지만(“한 span 은 한 비트에만”), 경계
    동률(클립 끝 == 다음 클립 시작인 소스 시각)은 **이른 클립** — 결정성."""
    for s, e, off in offsets:
        if s - 1e-9 <= source_sec <= e + 1e-9:
            return round(off + (min(max(source_sec, s), e) - s), 3)
    return None


# ── 어절 자막 ───────────────────────────────────────────────────────────────

def _lines_for_span(words: list[dict], t_in: float, t_out: float) -> list[dict]:
    """한 유성 span 의 단어들 → 라인 목록(소스 좌표). 순수.

    끊는 곳: 어절 수 SUB_MAX_WORDS · 표시 길이 SUB_MAX_CHARS(공백 포함 — 넘치면
    그 어절부터 새 라인, 단일 초과 어절은 홀로 간다) · 문장부호로 끝난 어절 뒤."""
    lines: list[dict] = []
    cur: list[dict] = []

    def flush() -> None:
        if not cur:
            return
        text = " ".join(w["text"] for w in cur)
        start = max(t_in, float(cur[0]["t0"]) - SUB_LEAD_SEC)
        end = float(cur[-1]["t1"])
        lines.append({"start": start, "end": end, "text": text})

    for w in words:
        text = str(w.get("text") or "").strip()
        if not text:
            continue
        joined = " ".join([x["text"] for x in cur] + [text])
        if cur and (len(cur) >= SUB_MAX_WORDS or len(joined) > SUB_MAX_CHARS):
            flush()
            cur = []
        cur.append({"t0": w["t0"], "t1": w["t1"], "text": text})
        if text.endswith(SENTENCE_ENDINGS):
            flush()
            cur = []
    flush()

    # 최소 노출 보정 — 다음 라인 시작(없으면 span 끝)까지 연장. 라인 시작이 이전 라인
    # 끝보다 이르면 뒤로 민다(겹침 금지 — 팝인 자막은 한 번에 한 줄).
    for i, ln in enumerate(lines):
        if i > 0 and ln["start"] < lines[i - 1]["end"]:
            ln["start"] = lines[i - 1]["end"]
        limit = lines[i + 1]["start"] if i + 1 < len(lines) else t_out
        if ln["end"] - ln["start"] < SUB_MIN_SEC:
            ln["end"] = min(max(limit, ln["end"]), ln["start"] + SUB_MIN_SEC)
    return lines


def word_subtitles(timeline: list[dict], span_index: dict[str, dict],
                   grid_words: list[dict]) -> list[dict]:
    """채택 유성 span(뮤트 제외) → 어절 자막 세그먼트(**편집본 좌표** — C6).

    단어 소속은 중점 기준(span 재단과 같은 규율). 자막은 원본 오디오 인용에만 —
    내레이션 텍스트는 cue 가 나른다(편집실이 cue.text 로 오버레이)."""
    offsets = edited_offsets(timeline)
    segments: list[dict] = []
    for c in timeline:
        if not c.get("use_original_audio"):
            continue
        for sid in c.get("span_ids") or []:
            sp = span_index[sid]
            if not sp["is_audio"]:
                continue
            in_span = [w for w in grid_words
                       if sp["t_in"] <= (float(w["t0"]) + float(w["t1"])) / 2 < sp["t_out"]]
            for ln in _lines_for_span(in_span, sp["t_in"], sp["t_out"]):
                e0 = to_edited_sec(ln["start"], offsets)
                e1 = to_edited_sec(ln["end"], offsets)
                if e0 is None or e1 is None or e1 <= e0:
                    continue
                segments.append({"start_sec": e0, "end_sec": e1, "text": ln["text"]})
    segments.sort(key=lambda s: (s["start_sec"], s["end_sec"]))
    return segments


# ── TTS cue 좌표 확정 ───────────────────────────────────────────────────────

def finalize_cues(narration_cues: list[dict], timeline: list[dict], *,
                  voice: str, speed: str) -> list[dict]:
    """스토리 cue 계획 → C2 계약 cue(편집본 start/end + source_time_sec 신원)."""
    offsets = edited_offsets(timeline)
    out: list[dict] = []
    for cue in narration_cues:
        e0 = to_edited_sec(cue["source_time_sec"], offsets)
        e1 = to_edited_sec(cue["source_end_sec"], offsets)
        if e0 is None or e1 is None or e1 <= e0:
            # 창이 트리밍으로 사라졌다 — 드랍 기록은 호출자가 남긴다
            out.append({**cue, "start_sec": None, "end_sec": None})
            continue
        out.append({
            "text": cue["text"],
            "source_time_sec": cue["source_time_sec"],
            "start_sec": e0, "end_sec": e1,
            "duration_sec": round(e1 - e0, 3),
            "voice": voice, "speed": speed,
            "beat": cue["beat"], "mode": cue["mode"],
            "muted_span_ids": list(cue.get("muted_span_ids") or []),
        })
    return out


def verify_edit_plan(plan: dict, grid: dict) -> dict:
    """벨트 — timeline 경계가 전부 grid span 경계인가(Stage 2 벨트와 같은 규율)."""
    edges = set()
    for sp in grid.get("span_candidates") or []:
        edges.add(round(float(sp["t_in"]), 3))
        edges.add(round(float(sp["t_out"]), 3))
    checked = ok = 0
    bad: list[float] = []
    for c in plan.get("timeline") or []:
        for v in (c["clip_start_sec"], c["clip_end_sec"]):
            checked += 1
            if round(float(v), 3) in edges:
                ok += 1
            else:
                bad.append(v)
    return {"checked": checked, "from_grid": ok,
            "pct": round(ok / checked * 100, 2) if checked else None,
            "violations": bad[:10]}


def clip_stats(plan: dict) -> dict:
    """분포 지표 — 하네스 §3(구간 중앙 7.5s±·6~8개) 대조용 요약."""
    durs = [round(float(c["clip_end_sec"]) - float(c["clip_start_sec"]), 3)
            for c in plan.get("timeline") or []]
    total = round(sum(durs), 3)
    return {"clips": len(durs), "total_sec": total,
            "median_sec": sorted(durs)[len(durs) // 2] if durs else None,
            "durations": durs}
