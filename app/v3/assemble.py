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


MUTE_TAIL_MIN_SEC = 0.30      # 이보다 짧은 자투리는 되살리지 않는다(깜빡임 방지)


def narration_windows(story_doc: dict) -> dict[int, list[tuple[float, float]]]:
    """비트 번호 → 내레이션이 실제로 점유하는 소스 구간(병합·정렬). 순수."""
    by_beat: dict[int, list[tuple[float, float]]] = {}
    for cue in story_doc.get("narration_cues") or []:
        a, z = cue.get("source_time_sec"), cue.get("source_end_sec")
        if a is None or z is None or z <= a:
            continue
        by_beat.setdefault(int(cue["beat"]), []).append((float(a), float(z)))
    for bi, wins in by_beat.items():
        wins.sort()
        merged: list[tuple[float, float]] = []
        for a, z in wins:
            if merged and a <= merged[-1][1] + 1e-6:
                merged[-1] = (merged[-1][0], max(merged[-1][1], z))
            else:
                merged.append((a, z))
        by_beat[bi] = merged
    return by_beat


def split_by_windows(t0: float, t1: float,
                     windows: list[tuple[float, float]]) -> list[tuple[float, float, bool]]:
    """[t0,t1] 을 창 안(뮤트)/창 밖(원음)으로 쪼갠다 → [(a, z, use_original_audio)]. 순수.

    창이 없으면 통째로 뮤트(종전 동작). MUTE_TAIL_MIN_SEC 미만의 자투리는 만들지
    않는다 — 0.2초짜리 원음 조각은 살아난 게 아니라 잡음이다."""
    cover = [(max(a, t0), min(z, t1)) for a, z in windows if z > t0 and a < t1]
    cover = [(a, z) for a, z in cover if z > a]
    if not cover:
        return [(t0, t1, False)]
    pieces: list[tuple[float, float, bool]] = []
    cur = t0
    for a, z in cover:
        if a - cur >= MUTE_TAIL_MIN_SEC:
            pieces.append((cur, a, True))
            cur = a
        pieces.append((cur, z, False))
        cur = z
    if t1 - cur >= MUTE_TAIL_MIN_SEC:
        pieces.append((cur, t1, True))
    elif pieces:
        pieces[-1] = (pieces[-1][0], t1, pieces[-1][2])
    return pieces


# ── edit_plan 조립 ──────────────────────────────────────────────────────────

def assemble_edit_plan(story_doc: dict, span_index: dict[str, dict], *,
                       video_path: str, work_title: str) -> dict:
    """비트 편성 → C1 동결 필드의 edit_plan. 클립 = 비트 안 span 병합 단위.

    분할 지점: (a) 소스 시간 불연속(원거리는 비트가 나뉘므로 방어) (b) 뮤트 여부가
    바뀌는 곳 — use_original_audio 는 클립 단위 계약이라 뮤트 span 은 제 클립을 갖는다."""
    timeline: list[dict] = []
    cue_windows = narration_windows(story_doc)
    for b in story_doc["beats"]:
        muted = set(b.get("muted_span_ids") or [])
        group: list[str] = []

        def flush(group: list[str], beat=b, muted=muted) -> None:
            if not group:
                return
            t0 = span_index[group[0]]["t_in"]
            t1 = span_index[group[-1]]["t_out"]

            def emit(a: float, z: float, audio: bool) -> None:
                timeline.append({
                    "role": beat["role"],
                    "clip_start_sec": round(a, 3),
                    "clip_end_sec": round(z, 3),
                    "subtitle": "",
                    "use_original_audio": audio,
                    "reframe": {"mode": "center"},
                    "span_ids": list(group),
                })

            if group[0] not in muted:
                emit(t0, t1, True)
                return
            # 뮤트 span 이 내레이션 창보다 길면 그 꼬리는 **소리도 자막도 없는**
            # 구간이 된다(실측: 도입부 5.49s 창에 내레이션 1.92s → 무음 3.57s).
            # 창 밖은 원음을 되살린다 — span 은 자를 수 없어도 클립은 자를 수 있다.
            for a, z, audio in split_by_windows(t0, t1, cue_windows.get(beat["number"], [])):
                emit(a, z, audio)

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
        # 라벨은 **앵커 span 을 담은 클립**의 subtitle 필드에 싣는다(C1 — 편집실
        # 오버레이 재료). M11: 비트 시작 고정이 아니라 대사 순간 앵커.
        for lb in b.get("labels") or ([{"text": b["label"], "span_id": b["span_ids"][0]}]
                                      if b.get("label") else []):
            for c in timeline:
                if lb.get("span_id") in (c.get("span_ids") or []):
                    c["subtitle"] = lb["text"]
                    break

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
                  offsets: list[tuple[float, float, float]],
                  *, kind: str = "start") -> float | None:
    """원본 절대초 → 편집본 초. 어느 클립에도 없으면 None(조용한 0 금지).

    경계 동률은 **용도별 반개구간**으로 푼다(적대 리뷰 확정 결함 — 원거리 편성에서
    grid 인접 span 의 공유 경계값이 타임라인상 이른 클립으로 매핑되어 자막 드랍·
    cue 오배치가 재현됐다):
      kind="start"(시작 좌표): [s, e) — 클립 끝과 동률이면 그 클립이 아니다.
      kind="end"(끝 좌표):     (s, e] — 클립 시작과 동률이면 그 클립이 아니다.
    비트 안(클립 신원을 아는) 변환은 이 함수 대신 클립 offset 직접 계산을 쓴다."""
    eps = 1e-9
    for s, e, off in offsets:
        if kind == "end":
            hit = s + eps < source_sec <= e + eps
        else:
            hit = s - eps <= source_sec < e - eps
        if hit:
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
    # 그래도 미달인 꼬리(span 끝이 상한이라 못 늘린 "같아?"류 실측 3건)는 **이전
    # 라인에 병합**한다(기획 §6 "미달 병합" — 12자 규칙보다 노출 규칙이 우선).
    merged: list[dict] = []
    for ln in lines:
        if merged and ln["end"] - ln["start"] < SUB_MIN_SEC - 1e-9:
            merged[-1]["text"] = f"{merged[-1]['text']} {ln['text']}"
            merged[-1]["end"] = max(merged[-1]["end"], ln["end"])
        else:
            merged.append(ln)
    return merged


def _lines_from_text(text: str, t_in: float, t_out: float) -> list[dict]:
    """어절 타임코드 없는 텍스트(M9-C heard) → span 구간 균등 배분 라인. 순수.

    같은 표시 규칙(2~4어절·12자)을 쓰되 타이밍은 균등이다 — 팝인의 발화 동기화는
    포기하지만, 깨진 전사를 그대로 띄우는 것보다 낫다."""
    toks = str(text or "").split()
    if not toks or t_out <= t_in:
        return []
    groups: list[list[str]] = []
    cur: list[str] = []
    for tk in toks:
        joined = " ".join(cur + [tk])
        if cur and (len(cur) >= SUB_MAX_WORDS or len(joined) > SUB_MAX_CHARS):
            groups.append(cur)
            cur = []
        cur.append(tk)
    if cur:
        groups.append(cur)
    step = (t_out - t_in) / len(groups)
    out = []
    for i, g in enumerate(groups):
        a = t_in + step * i
        b = min(t_out, a + step)
        if b - a >= SUB_MIN_SEC - 1e-9 or len(groups) == 1:
            out.append({"start": a, "end": b, "text": " ".join(g)})
        elif out:                       # 너무 짧으면 앞줄에 병합(§6 규칙과 같은 규율)
            out[-1]["text"] += " " + " ".join(g)
            out[-1]["end"] = b
        else:
            out.append({"start": a, "end": b, "text": " ".join(g)})
    return out


# 화자별 자막색 — 정본은 가왕쇼 템플릿(template.json dialogue_captions.colors):
# w 주연/기본 · o 상대역 · y 질문·리액션 · b 썰전달자 · r 강조.
SPEAKER_DEFAULT_COLOR = "#FFFFFF"
SPEAKER_PALETTE = ("#FFB637", "#FFE94A", "#7ED0FF", "#FF5540")
UNKNOWN_SPEAKERS = frozenset({"미상", "?", "unknown", "unknown speaker"})


def speaker_colors(span_index: dict[str, dict]) -> dict[str, str]:
    """화자 → 자막색. 순수·결정적.

    최다 발화자(= 주연)와 미상은 흰색, 나머지는 **첫 등장 순서**로 팔레트를 돈다.
    사람이 만든 템플릿도 주연만 흰색이고 상대역·리액션에 색을 줬다(gw 실측 w12/y4/o4).
    동률은 먼저 나온 화자가 주연 — 무작위 요소 없음."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for sp in sorted(span_index.values(), key=lambda s: (s["t_in"], s["t_out"])):
        for line in sp.get("audio_script") or []:
            name = str(line.get("speaker") or "").strip()
            if not name or name.lower() in UNKNOWN_SPEAKERS:
                continue
            if name not in counts:
                order.append(name)
            counts[name] = counts.get(name, 0) + 1
    if not order:
        return {}
    lead = min(order, key=lambda n: (-counts[n], order.index(n)))
    out = {lead: SPEAKER_DEFAULT_COLOR}
    for i, name in enumerate(n for n in order if n != lead):
        out[name] = SPEAKER_PALETTE[i % len(SPEAKER_PALETTE)]
    return out


def span_speaker(sp: dict) -> str:
    """그 span 의 대표 화자(첫 발화자). 없으면 빈 문자열."""
    for line in sp.get("audio_script") or []:
        name = str(line.get("speaker") or "").strip()
        if name:
            return name
    return ""


def word_subtitles(timeline: list[dict], span_index: dict[str, dict],
                   grid_words: list[dict]) -> list[dict]:
    """채택 유성 span(뮤트 제외) → 어절 자막 세그먼트(**편집본 좌표** — C6).

    단어 소속은 중점 기준(span 재단과 같은 규율). 자막은 원본 오디오 인용에만 —
    내레이션 텍스트는 cue 가 나른다(편집실이 cue.text 로 오버레이)."""
    segments: list[dict] = []
    colors = speaker_colors(span_index)
    off = 0.0
    for c in timeline:
        c0, c1 = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        if not c.get("use_original_audio"):
            off += c1 - c0
            continue
        for sid in c.get("span_ids") or []:
            sp = span_index[sid]
            if not sp["is_audio"]:
                continue
            # M9-C 전사 판정을 자막에 반영(리뷰 확정 critical — 판정이 stage2
            # 기록에만 남고 화면에는 깨진 전사가 그대로 나가던 결함):
            #   none  → 대사 확보 실패, 자막 없음(로그·docstring 의 약속 이행)
            #   heard → grid 단어는 깨진 전사다. 모델이 들은 문장을 span 구간에
            #           균등 배치한다(어절 타임코드가 없으므로 팝인 대신 균등).
            src = sp.get("text_source")
            if src == "none":
                continue
            if src == "heard":
                lines = _lines_from_text(str(sp.get("heard_text") or ""),
                                         sp["t_in"], sp["t_out"])
            else:
                in_span = [w for w in grid_words
                           if sp["t_in"] <= (float(w["t0"]) + float(w["t1"])) / 2
                           < sp["t_out"]]
                lines = _lines_for_span(in_span, sp["t_in"], sp["t_out"])
            speaker = span_speaker(sp)
            color = colors.get(speaker, SPEAKER_DEFAULT_COLOR)
            for ln in lines:
                # 소속 클립을 이미 안다 — offset 직접 계산(동률 스캔 매핑 금지)
                e0 = round(off + (max(ln["start"], c0) - c0), 3)
                e1 = round(off + (min(ln["end"], c1) - c0), 3)
                if e1 <= e0:
                    continue
                # speaker·color 는 additive — 옛 소비자는 세 키만 읽는다(C6)
                segments.append({"start_sec": e0, "end_sec": e1, "text": ln["text"],
                                 "speaker": speaker, "color": color})
        off += c1 - c0
    segments.sort(key=lambda s: (s["start_sec"], s["end_sec"]))
    return segments


# ── TTS cue 좌표 확정 ───────────────────────────────────────────────────────

def finalize_cues(narration_cues: list[dict], timeline: list[dict], *,
                  voice: str, speed: str) -> list[dict]:
    """스토리 cue 계획 → C2 계약 cue(편집본 start/end + source_time_sec 신원)."""
    offsets = edited_offsets(timeline)
    out: list[dict] = []
    for cue in narration_cues:
        e0 = to_edited_sec(cue["source_time_sec"], offsets, kind="start")
        e1 = to_edited_sec(cue["source_end_sec"], offsets, kind="end")
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
