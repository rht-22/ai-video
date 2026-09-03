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

    ⚠ **클립을 쪼개지 않는다.** timeline 경계는 grid span 경계여야 하고(시간 정본
    벨트) 내레이션 창 끝은 격자 위가 아니다 — 실제로 벨트가 이 시도를 막았다
    (`edit_plan 시각 정합 벨트 위반 … 82.35%`). 그래서 분할은 **소리(뮤트 창)와
    자막**에만 적용한다: 클립은 그대로 두고, 창 밖 구간만 원음·자막을 되살린다.

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

TAIL_PAD_MAX_SEC = 0.45      # 유성 꼬리 연장 상한(무음 시작점 목표일 때)
TAIL_PAD_WORD_SEC = 0.25     # 파형 무음이 안 잡힐 때 다음 단어 직전까지의 상한
# 무음 시작점 뒤 여유(2026-09-03 실사고 "아, 그런가?" 꼬리 잘림): silencedetect 는
# −30dB 아래를 무음으로 보는데 의문형 어미의 감쇠 꼬리는 그 아래에서도 들린다 —
# 종전 +0.05s 는 그 꼬리를 물었다. 상한(TAIL_PAD_MAX·다음 단어)은 그대로.
TAIL_SIL_PAD_SEC = 0.20


def pad_voiced_tails(timeline: list[dict], span_index: dict[str, dict],
                     words: list[dict] | None,
                     silences: list[list[float]] | None) -> int:
    """유성으로 끝나는 클립의 꼬리 클리핑 보정(2026-09-02 실사고: 9클립 중 8개가
    어미 감쇠 꼬리를 문 채 컷 — Whisper 단어 끝 시각이 체계적으로 빡빡하다).

    프리미어 편집자가 파형을 보고 소리가 잦아드는 데서 자르는 것의 기계화(사용자
    지시): 컷 직후 0.5초 안에 silencedetect 무음이 시작하면 **그 지점 + 0.05s** 까지,
    없으면 다음 전사 단어 직전까지(상한 0.25s) 연장한다. 다음 클립이 소스에서 바로
    이어지면 연장하지 않는다(재료 중복 금지). 전부 grid 실측 산술 — tail_pad 로
    벨트에 정식 등록. 반환: 보정 클립 수."""
    if not timeline or (not words and not silences):
        return 0                            # 실측 재료가 없으면 불변(오판 금지)
    word_starts = sorted(float(w["t0"]) for w in words or [])
    word_ends = sorted(float(w["t1"]) for w in words or [])
    sil_starts = sorted(float(s[0]) for s in silences or [])
    import bisect
    n = 0
    for i, c in enumerate(timeline):
        ids = c.get("span_ids") or []
        if c.get("cover"):
            continue                        # 덮개 경계는 내레이션 길이 실측 — 안 건드린다
        # ④ 머리 패드(2026-09-02 실사고 "아동학대라고요?"의 '아' 잘림): 유성으로
        # **시작**하는 클립은 Whisper 단어 시작 시각이 빡빡해 초성이 잘린다 —
        # 직전 단어 끝과의 여유 안에서 시작을 최대 0.15초 당긴다. 앞 클립이 소스에서
        # 바로 붙으면 안 당긴다(재료 중복 금지).
        if ids and ids[0] in span_index and span_index[ids[0]]["is_audio"] \
                and not c.get("head_trimmed"):
            st = float(c["clip_start_sec"])
            prev_end = float(timeline[i - 1]["clip_end_sec"]) if i > 0 else None
            if prev_end is None or st - prev_end >= 0.05:
                k2 = bisect.bisect_left(word_ends, st - 0.01) - 1
                prev_w = word_ends[k2] if k2 >= 0 else None
                room = st - prev_w if prev_w is not None else 0.5
                room = min(room, st)        # 소스 0초 아래로는 못 당긴다
                if prev_end is not None:
                    room = min(room, st - prev_end)
                pull = min(0.15, room - 0.05)
                if pull >= 0.05:
                    c["clip_start_sec"] = round(st - pull, 3)
                    c["head_pad"] = round(pull, 3)
                    n += 1
        if not ids or ids[-1] not in span_index \
                or not span_index[ids[-1]]["is_audio"]:
            continue
        e = float(c["clip_end_sec"])
        nxt_start = float(timeline[i + 1]["clip_start_sec"]) \
            if i + 1 < len(timeline) else None
        if nxt_start is not None and nxt_start - e < 0.05:
            continue                       # 소스에서 바로 이어짐 — 연장 불필요
        limit = e + TAIL_PAD_MAX_SEC
        if nxt_start is not None:
            limit = min(limit, nxt_start - 0.02)
        k = bisect.bisect_left(sil_starts, e - 0.05)
        sil = sil_starts[k] if k < len(sil_starts) and sil_starts[k] <= e + 0.5 \
            else None
        j = bisect.bisect_right(word_starts, e + 0.01)
        nxt_word = word_starts[j] if j < len(word_starts) else None
        if sil is not None:
            target = sil + TAIL_SIL_PAD_SEC
        elif nxt_word is not None and nxt_word - e >= 0.15:
            target = min(e + TAIL_PAD_WORD_SEC, nxt_word - 0.05)
        elif nxt_word is None:
            target = e + TAIL_PAD_WORD_SEC
        else:
            continue                       # 다음 단어가 바로 붙음 — 연장 여지 없음
        if nxt_word is not None:
            target = min(target, nxt_word - 0.05)
        target = min(target, limit)
        if target - e < 0.05:
            continue
        c["clip_end_sec"] = round(target, 3)
        c["tail_pad"] = round(target - e, 3)
        n += 1
    return n


def assemble_edit_plan(story_doc: dict, span_index: dict[str, dict], *,
                       video_path: str, work_title: str,
                       words: list[dict] | None = None,
                       silences: list[list[float]] | None = None,
                       fps: float | None = None) -> dict:
    """비트 편성 → C1 동결 필드의 edit_plan. 클립 = 비트 안 span 병합 단위.

    분할 지점: (a) 소스 시간 불연속(원거리는 비트가 나뉘므로 방어) (b) 뮤트 여부가
    바뀌는 곳 — use_original_audio 는 클립 단위 계약이라 뮤트 span 은 제 클립을 갖는다."""
    timeline: list[dict] = []
    for b in story_doc["beats"]:
        muted = set(b.get("muted_span_ids") or [])
        group: list[str] = []

        # 내레이션 덮개(story_flow · 2026-09-03) — 내레이션 실측 길이에 맞춰 **다시 본**
        # 구간이라 경계가 grid 위가 아닐 수 있다. 제 클립(원음 끔)으로 정식 등록한다
        # (tail_pad·head_trimmed 와 같은 지위 — 벨트가 `cover` 키를 인정한다).
        def emit_cover(cv: dict, beat=b) -> None:
            ids_c = [s for s in (cv.get("span_ids") or []) if s in span_index]
            timeline.append({
                "role": beat["role"],
                "clip_start_sec": round(float(cv["t_in"]), 3),
                "clip_end_sec": round(float(cv["t_out"]), 3),
                "subtitle": "",
                "use_original_audio": False,
                "reframe": {"mode": "center"},
                "span_ids": ids_c,
                "cover": str(cv.get("kind") or "cover"),
                **({"hold_sec": round(float(cv["hold_sec"]), 3)} if cv.get("hold_sec") else {}),
            })

        for cv in b.get("covers") or []:
            if cv.get("position", "before") == "before":
                emit_cover(cv)

        def flush(group: list[str], beat=b, muted=muted) -> None:
            if not group:
                return
            t0 = span_index[group[0]]["t_in"]
            t1 = span_index[group[-1]]["t_out"]
            # 머리 데드에어 컷(2026-09-02) — 창 확정 후 story 가 산출한 트림 지점.
            # span 내부라 시작점만 당긴다. 스냅 벨트에는 head_trimmed 로 정식 등록
            # (기록 없는 비스냅 경계는 여전히 위반).
            head = False
            ht = beat.get("head_trim_sec")
            if ht is not None and group[0] == beat["span_ids"][0] \
                    and t0 < ht < t1 - 0.05:
                t0, head = float(ht), True

            clip = {
                "role": beat["role"],
                "clip_start_sec": round(t0, 3),
                "clip_end_sec": round(t1, 3),
                "subtitle": "",
                "use_original_audio": group[0] not in muted,
                "reframe": {"mode": "center"},
                "span_ids": list(group),
            }
            # 크롭 앵커 재료(2026-09-02, additive) — **전부 무성**인 클립(시각 인서트)
            # 에서 Stage 2 가 주 피사체를 좌/우로 봤으면 클립에 접는다. 판별 신호는
            # 키워드가 아니라 '무성 인서트'라는 구성 자체다(범용 — 케이스 편향 금지).
            # 대사 클립은 건드리지 않는다(중앙 크롭 종전 그대로).
            if all(not span_index[s]["is_audio"] for s in group):
                sides = [span_index[s].get("subject_pos") for s in group]
                sides = [p for p in sides if p in ("left", "right")]
                if sides and all(p == sides[0] for p in sides):
                    clip["subject_pos"] = sides[0]
            if head:
                clip["head_trimmed"] = True
            timeline.append(clip)

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
        for cv in b.get("covers") or []:
            if cv.get("position") == "after":
                emit_cover(cv)
        # 라벨은 **앵커 span 을 담은 클립**의 subtitle 필드에 싣는다(C1 — 편집실
        # 오버레이 재료). M11: 비트 시작 고정이 아니라 대사 순간 앵커.
        for lb in b.get("labels") or ([{"text": b["label"], "span_id": b["span_ids"][0]}]
                                      if b.get("label") else []):
            for c in timeline:
                if lb.get("span_id") in (c.get("span_ids") or []):
                    c["subtitle"] = lb["text"]
                    break

    pad_voiced_tails(timeline, span_index, words, silences)
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
        # 편집본 좌표를 렌더와 같은 프레임 격자로 세기 위한 소스 fps(edited_offsets).
        # None = 미상 → 종전 실수 누적(회귀 0).
        "source_fps": (float(fps) if fps else None),
    }


# ── 소스 ↔ 편집본 좌표 ─────────────────────────────────────────────────────

# ── 프레임 격자 ────────────────────────────────────────────────────────────
# 렌더는 클립을 따로 잘라 concat 한다. concat 은 세그먼트마다 영상 길이를 **소리 길이에
# 맞추려고 마지막 프레임을 복제**하므로, 실제 세그먼트는 언제나 프레임 정수 개다.
# 2026-09-03 실측(지금불륜이문제가아닙니다_b0ccda99): 계획 55.559s·13조각인데 완성본은
# 1338프레임 = Σceil(길이×fps) — 한 프레임도 안 틀린다.
#
# 그런데 편집본 좌표는 계획값의 실수 누적합이었다. 그래서 조각을 지날 때마다 좌표가
# 밀려, 뒤로 갈수록 벌어진다(실측 최대 0.3초). 덮개 뮤트 창이 화면보다 0.32초 앞서
# 시작하고 앞서 끝나 ① 앞 장면 대사가 잘리고 ② 덮개 꼬리의 대사가 새어나왔다.
# 자막·cue·라벨·효과음이 전부 같은 좌표를 쓰므로 같이 밀렸다.
#
# 고침: 좌표도 렌더와 **같은 격자**로 센다. renderer 가 클립을 정확히 clip_frames()
# 개로 고정하고(trim=end_frame), 여기서 같은 수로 누적한다 — 두 쪽이 같은 식을 쓴다.
# fps 를 모르면 종전 그대로 실수 누적(회귀 0 — 옛 판·비-v3 경로가 안 바뀐다).

def clip_frames(dur_sec: float, fps: float | None) -> int | None:
    """클립 길이 → 렌더가 실제로 내는 프레임 수. fps 미상이면 None."""
    if not fps or float(fps) <= 0:
        return None
    return max(1, round(float(dur_sec) * float(fps)))


def clip_duration(dur_sec: float, fps: float | None) -> float:
    """프레임 격자에 맞춘 클립 길이 = 렌더가 실제로 만드는 길이."""
    n = clip_frames(dur_sec, fps)
    return float(dur_sec) if n is None else n / float(fps)


def clip_len(c: dict) -> float:
    """클립이 편집본에서 차지하는 길이(격자 반올림 전) = 소스 구간 + 붙잡은 시간(hold_sec).

    2026-09-03 '정보 화면 붙잡기': 덮개 화면(메시지·문서)이 내레이션보다 짧으면 마지막
    프레임을 hold_sec 만큼 붙잡는다. 편집본 길이를 더하는 곳은 **전부 이 함수**를 써야
    한다 — 한 곳이라도 (end−start) 를 직접 쓰면 좌표가 밀리고 프레임 격자 정렬이 깨진다."""
    return float(c["clip_end_sec"]) - float(c["clip_start_sec"]) + float(c.get("hold_sec") or 0.0)


def edited_offsets(timeline: list[dict],
                   fps: float | None = None) -> list[tuple[float, float, float]]:
    """클립별 (소스 시작, 소스 끝, 편집본 오프셋) — 편성 순서 누적.

    fps 를 주면 렌더와 같은 프레임 격자로 누적한다(위 주석). 안 주면 종전 실수 누적."""
    out = []
    off = 0.0
    for c in timeline:
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        out.append((s, e, off))
        off += clip_duration(clip_len(c), fps)
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


def _word_speakers(sp: dict, in_span: list[dict]) -> list[str] | None:
    """다화자 span 의 단어별 화자 귀속(2026-09-02 — "빨리 / 자 잠깐만요" 색 실사고).

    Stage 2 audio_script 가 화자별 행으로 나뉘어 있으면, 행 텍스트 길이를 단어열에
    관용 정렬(공백·문장부호 무시)해 단어마다 화자를 단다. 화자가 하나거나 정렬이
    깨지면 None — 종전(대표 화자 한 색)으로 폴백한다(오판 금지)."""
    import re
    _n = lambda t: re.sub(r"[^0-9A-Za-z가-힣]", "", str(t))
    rows = [(str(r.get("speaker") or "").strip(), _n(r.get("line")))
            for r in sp.get("audio_script") or []
            if _n(r.get("line"))]
    if len({r[0] for r in rows if r[0]}) < 2:
        return None
    out: list[str] = []
    wi = 0
    for spk, target in rows:
        got = ""
        start = wi
        while wi < len(in_span) and len(got) < len(target):
            got += _n(in_span[wi]["text"])
            out.append(spk)
            wi += 1
        if wi == start:                      # 행에 배정된 단어 0개 — 정렬 붕괴
            return None
    while len(out) < len(in_span):
        out.append(rows[-1][0])
    return out


def word_subtitles(timeline: list[dict], span_index: dict[str, dict],
                   grid_words: list[dict],
                   mute_windows: list[tuple[float, float]] | None = None,
                   cast_names: list[str] | None = None,
                   name_fix_log: list[dict] | None = None) -> list[dict]:
    """채택 유성 span(뮤트 제외) → 어절 자막 세그먼트(**편집본 좌표** — C6).

    단어 소속은 중점 기준(span 재단과 같은 규율). 자막은 원본 오디오 인용에만 —
    내레이션 텍스트는 cue 가 나른다(편집실이 cue.text 로 오버레이)."""
    segments: list[dict] = []
    colors = speaker_colors(span_index)
    # 뮤트 클립이라도 내레이션 창 **밖**은 원음이 살아 있다(finalize 의 muted_windows
    # 와 같은 계산) — 그 구간 대사는 자막이 있어야 한다. 창을 모르면 종전대로 전부 제외.
    mw = list(mute_windows or [])
    off = 0.0
    for c in timeline:
        c0, c1 = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        audible: list[tuple[float, float]] = []
        if c.get("use_original_audio"):
            audible = [(c0, c1)]
        elif mw:
            audible = [(a, z) for a, z, on in split_by_windows(c0, c1, mw) if on]
        if not audible:
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
                # 인명 대조(2026-09-03): whisper 어절이 인물표 이름과 가깝고 모델
                # 청취(heard_text)에 그 이름이 정확히 있으면 그 이름으로 — 두 증인
                # 일치 시에만. cast_names 없으면 종전과 동일.
                if cast_names:
                    from app.v3.textcheck import fix_span_words
                    in_span, _fx = fix_span_words(in_span, cast_names,
                                                  str(sp.get("heard_text") or ""))
                    if _fx and name_fix_log is not None:
                        name_fix_log.extend(dict(f, span_id=sid) for f in _fx)
                lines = _lines_for_span(in_span, sp["t_in"], sp["t_out"])
            speaker = span_speaker(sp)
            color = colors.get(speaker, SPEAKER_DEFAULT_COLOR)
            wspk = None if src == "heard" else _word_speakers(sp, in_span)
            for ln in lines:
                # 소속 클립을 이미 안다 — offset 직접 계산(동률 스캔 매핑 금지)
                e0 = round(off + (max(ln["start"], c0) - c0), 3)
                e1 = round(off + (min(ln["end"], c1) - c0), 3)
                if e1 <= e0:
                    continue
                mid = (ln["start"] + ln["end"]) / 2      # 소속은 중점 기준(span 재단과 같은 규율)
                if not any(a <= mid < z for a, z in audible):
                    continue                      # 뮤트 창 안 — 소리가 없으니 자막도 없다
                # speaker·color 는 additive — 옛 소비자는 세 키만 읽는다(C6).
                # 다화자 span 이면 이 줄의 첫 단어가 속한 화자의 색을 쓴다.
                l_spk, l_color = speaker, color
                if wspk:
                    for wi2, w2 in enumerate(in_span):
                        if float(w2["t0"]) >= ln["start"] - 0.06:
                            l_spk = wspk[wi2] or speaker
                            l_color = colors.get(l_spk, SPEAKER_DEFAULT_COLOR)
                            break
                segments.append({"start_sec": e0, "end_sec": e1, "text": ln["text"],
                                 "speaker": l_spk, "color": l_color})
        off += c1 - c0
    segments.sort(key=lambda s: (s["start_sec"], s["end_sec"]))
    return segments


# ── TTS cue 좌표 확정 ───────────────────────────────────────────────────────

def finalize_cues(narration_cues: list[dict], timeline: list[dict], *,
                  voice: str, speed: str, fps: float | None = None) -> list[dict]:
    """스토리 cue 계획 → C2 계약 cue(편집본 start/end + source_time_sec 신원)."""
    offsets = edited_offsets(timeline, fps)
    total = round(sum(clip_duration(clip_len(c), fps) for c in timeline), 3)
    out: list[dict] = []
    for cue in narration_cues:
        e0 = to_edited_sec(cue["source_time_sec"], offsets, kind="start")
        e1 = to_edited_sec(cue["source_end_sec"], offsets, kind="end")
        # 붙잡은 덮개(hold_sec): 소스 끝에 맞춘 cue 끝은 붙잡은 꼬리까지 이어진다
        if e1 is not None:
            for c in timeline:
                h = float(c.get("hold_sec") or 0.0)
                if h > 0 and abs(float(cue["source_end_sec"]) - float(c["clip_end_sec"])) < 0.02:
                    e1 = round(e1 + h, 3)
                    break
        rescued = False
        if e0 is not None and (e1 is None or e1 <= e0):
            # 창 끝만 소스 구멍(미편성 span·트림)에 떨어졌다 — 내레이션은 **편집본**
            # 위에서 연속 재생되므로 편집본 좌표로 길이를 보존한다(2026-09-01 실사고:
            # 배치기는 선택 span 길이 합으로 창을 재고 소스 연속 시각으로 적어, 비트가
            # 건너뛴 span 위에 창 끝이 얹히면 여기서 통째 드랍 → 내레이션 0개 발행).
            want = float(cue["source_end_sec"]) - float(cue["source_time_sec"])
            e1 = round(min(e0 + want, total), 3)
            rescued = True
        if e0 is None or e1 is None or e1 - e0 < 0.5:
            # 창 시작이 사라졌거나 남은 길이가 슬롯 구실을 못 한다 — 기록은 호출자가
            out.append({**cue, "start_sec": None, "end_sec": None})
            continue
        fin = {
            "text": cue["text"],
            "source_time_sec": cue["source_time_sec"],
            "start_sec": e0, "end_sec": e1,
            "duration_sec": round(e1 - e0, 3),
            # 배속 사다리(2026-09-02) — 계획이 cue 별로 고른 speed 가 기본값을 이긴다
            "voice": voice, "speed": cue.get("speed") or speed,
            "beat": cue["beat"], "mode": cue["mode"],
            "muted_span_ids": list(cue.get("muted_span_ids") or []),
        }
        if rescued:
            fin["window_rescued"] = True
        # story_flow(2026-09-03) — 걸음 4 에서 이미 합성한 mp3 와 실측 길이. resources
        # 가 재합성 대신 그대로 쓴다(additive — 없으면 종전 경로).
        for k_add in ("audio_path", "measured_sec"):
            if cue.get(k_add) is not None:
                fin[k_add] = cue[k_add]
        out.append(fin)
    return out


def verify_edit_plan(plan: dict, grid: dict) -> dict:
    """벨트 — timeline 경계가 전부 grid span 경계인가(Stage 2 벨트와 같은 규율)."""
    edges = set()
    for sp in grid.get("span_candidates") or []:
        edges.add(round(float(sp["t_in"]), 3))
        edges.add(round(float(sp["t_out"]), 3))
    checked = ok = head_ok = tail_ok = cover_ok = 0
    bad: list[float] = []
    for c in plan.get("timeline") or []:
        for v, is_start in ((c["clip_start_sec"], True),
                            (c["clip_end_sec"], False)):
            checked += 1
            if round(float(v), 3) in edges:
                ok += 1
            elif c.get("cover"):
                # 내레이션 덮개(story_flow) — 경계 출처가 합성 실측 길이 + 국소 재관찰
                # (장면 전환 스냅)이라 시각 환각 방어 위반이 아니다. 클립에 `cover`
                # 로 기록된 것만 허용(기록 없는 비스냅 경계는 여전히 위반).
                ok += 1
                cover_ok += 1
            elif is_start and (c.get("head_trimmed") or c.get("head_pad")):
                # 머리 데드에어 컷의 산술 경계(창 시작−리드) — 기록된 트림만 허용.
                # 출처가 실측 mp3 길이라 시각 환각 방어 위반이 아니다(2026-09-02).
                ok += 1
                head_ok += 1
            elif not is_start and (c.get("tail_pad") or c.get("tail_trim")):
                # 유성 꼬리 파형 연장(silencedetect·단어 시각 산술) — 같은 지위.
                # tail_trim: watch_trim 이 무대사 구간에서 눈금 밖으로 자른 끝(2026-09-03)
                ok += 1
                tail_ok += 1
            else:
                bad.append(v)
    return {"checked": checked, "from_grid": ok,
            "pct": round(ok / checked * 100, 2) if checked else None,
            "head_trimmed": head_ok, "tail_padded": tail_ok,
            "cover_edges": cover_ok,
            "violations": bad[:10]}


def clip_stats(plan: dict) -> dict:
    """분포 지표 — 하네스 §3(구간 중앙 7.5s±·6~8개) 대조용 요약."""
    durs = [round(clip_len(c), 3) for c in plan.get("timeline") or []]
    total = round(sum(durs), 3)
    return {"clips": len(durs), "total_sec": total,
            "median_sec": sorted(durs)[len(durs) // 2] if durs else None,
            "durations": durs}
