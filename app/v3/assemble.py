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

import re

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
    for bi, b in enumerate(story_doc["beats"]):
        muted = set(b.get("muted_span_ids") or [])
        group: list[str] = []
        _beat_no = int(b.get("number", bi))

        # 내레이션 덮개(story_flow · 2026-09-03) — 내레이션 실측 길이에 맞춰 **다시 본**
        # 구간이라 경계가 grid 위가 아닐 수 있다. 제 클립(원음 끔)으로 정식 등록한다
        # (tail_pad·head_trimmed 와 같은 지위 — 벨트가 `cover` 키를 인정한다).
        def emit_cover(cv: dict, beat=b) -> None:
            # 컷 쌓기(stack, 2026-09-08): parts 마다 제 클립 — 정지 대신 정배속 컷 여러 개
            parts = [(float(a), float(z)) for a, z in (cv.get("parts") or [(cv["t_in"], cv["t_out"])])]
            for a, z in parts:
                ids_c = [s for s in (cv.get("span_ids") or []) if s in span_index
                         and span_index[s]["t_in"] < z - 1e-6 and span_index[s]["t_out"] > a + 1e-6]
                timeline.append({
                    "role": beat["role"],
                    "clip_start_sec": round(a, 3),
                    "clip_end_sec": round(z, 3),
                    "subtitle": "",
                    "use_original_audio": False,
                    "reframe": {"mode": "center"},
                    "span_ids": ids_c,
                    "cover": str(cv.get("kind") or "cover"),
                    **({"hold_sec": round(float(cv["hold_sec"]), 3)} if cv.get("hold_sec") and len(parts) == 1 else {}),
                    "beat": _beat_no,      # additive(4단계) — 같은 소스 구간이 두 번 나올 때(훅 회수) cue 의 클립 신원
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
                # 사람 크롭 고정(2026-09-11): 비트 `crop_x`(소스 px, 선택 `crop_y`)가 있으면 그 비트의 클립은
                # 화자 추적·피사체 앵커 대신 그 자리를 본다(finalize 가 mode=fixed 를 소비). 검출이 못 잡는
                # 멀리 있는 작은 인물(ep01x03 「너 바람피니?」 직후 남편, x≈570·얼굴 20px)을 사람이 지정하는 통로.
                "reframe": ({"mode": "fixed", "x": float(beat["crop_x"]),
                             **({"y": float(beat["crop_y"])} if beat.get("crop_y") is not None else {})}
                            if beat.get("crop_x") is not None else {"mode": "center"}),
                "span_ids": list(group),
                "beat": _beat_no,      # additive(4단계) — 훅 회수 시 cue·자막 좌표의 클립 신원
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

# 줄 경계 벌점(2026-09-04) — 길이만 고르게 하면 '여기서도 하실 / 건 아니죠' · '이런 거 잘 안 /
# 하는데' 처럼 붙어 읽히는 두 어절이 갈린다. 1음절 의존명사·조사류는 앞 어절에, 1음절 부사는
# 뒤 어절에 붙으므로 그 경계로 줄을 끊는 배치에 벌점을 준다(남는 칸 5~6자 분량).
# '봐·줘·해' 는 보조용언(싸울까 봐 · 해 줘 · 하게 해) — 문장 첫머리 "봐, 이거" 는 문장 덩어리의
# 첫 어절(i == 0)이라 벌점 대상이 아니다(줄 경계 벌점은 문장 **안** 경계에만 붙는다).
LINE_START_PENALTY_WORDS = frozenset("건 거 것 게 수 줄 데 때 뿐 등 중 채 듯 놈 분 편 시 봐 줘 해".split())
LINE_START_PENALTY_JOSA = ("이", "가", "을", "를", "은", "는", "에", "에는", "에도", "도", "의", "로",
                           "으로", "만", "야", "지", "요", "죠", "다", "까", "만큼", "처럼", "부터",
                           "까지", "이야", "이지", "이라", "이다", "이면", "이고")
LINE_END_PENALTY_WORDS = frozenset("안 못 잘 더 다 또 왜 꼭 참 막 딱 젤 늘 꽤".split())
LINE_BOUNDARY_PENALTY = 30


def _starts_dependent(tok: str) -> bool:
    """줄 첫 어절이 '의존명사(+조사)' 인가 — '건'·'거' 뿐 아니라 '시에'·'때는'·'수가'·'것을'
    (2026-09-04 신병4: '35도를 넘은 / 시에 오침을'). 순수."""
    t = tok.strip(".,!?…'\"")
    if not t:
        return False
    if t in LINE_START_PENALTY_WORDS:
        return True
    return t[0] in LINE_START_PENALTY_WORDS and t[1:] in LINE_START_PENALTY_JOSA


def _balanced_breaks(lens: list[int], texts: list[str] | None = None) -> list[int]:
    """어절 표시 길이 목록 → 줄 끝 인덱스(각 줄의 마지막 어절 index) 목록. 순수·결정적.

    한 문장 안에서 줄 길이를 **고르게**(SUB_MAX_CHARS 대비 남는 칸의 제곱합 최소) 나눈다 —
    그리디(앞부터 채우기)는 '어디다 두고 남의 / 호의를 함부로 쓰레기 / 취급해' 처럼 마지막
    줄에 꽁다리를 남긴다(2026-09-04 사용자 지적). 제약은 종전과 같다: 줄당 어절
    ≤ SUB_MAX_WORDS · 표시 길이(공백 포함) ≤ SUB_MAX_CHARS · 단일 초과 어절은 홀로.
    경계 벌점(LINE_*_PENALTY_WORDS)을 더하고, **동점이면 앞 줄을 길게**(종전 그리디와 같은
    쪽)로 푼다 — 두 배치가 똑같이 고르면 승인된 종전 모양을 지킨다."""
    n = len(lens)
    texts = texts or [""] * n
    INF = float("inf")
    best = [INF] * (n + 1)
    prev = [-1] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        for i in range(max(0, j - SUB_MAX_WORDS), j):
            width = sum(lens[i:j]) + (j - i - 1)
            if width > SUB_MAX_CHARS and j - i > 1:
                continue
            cost = best[i] + max(0, SUB_MAX_CHARS - width) ** 2
            if i > 0 and _starts_dependent(texts[i]):
                cost += LINE_BOUNDARY_PENALTY
            if j < n and texts[j - 1].strip(".,!?…'\"") in LINE_END_PENALTY_WORDS:
                cost += LINE_BOUNDARY_PENALTY
            if cost <= best[j]:            # 동점 → i 가 큰 쪽(= 앞 줄이 긴 배치)
                best[j], prev[j] = cost, i
    out, j = [], n
    while j > 0:
        out.append(j - 1)
        j = prev[j]
    return out[::-1]


def _lines_for_span(words: list[dict], t_in: float, t_out: float) -> list[dict]:
    """한 유성 span 의 단어들 → 라인 목록(소스 좌표). 순수.

    끊는 곳: ① 문장 끝 — whisper 어절의 문장부호 **또는** 모델 청취가 문장부호로 끝난 자리
    (`sent_end` 힌트, textcheck.align_tokens_to_heard — '안녕하세요 / 저희 앞집 이사왔어요').
    문장 경계는 **소프트**다: 앞뒤 문장을 합쳐도 한 줄(12자·4어절)에 들면 안 끊는다
    ('대박. 미쳤다.' 가 2자 줄 둘로 깜빡이지 않게) ② 문장 안에서는 `_balanced_breaks`."""
    toks = [dict(t0=w["t0"], t1=w["t1"], text=str(w.get("text") or "").strip(),
                 hard=bool(w.get("sent_end")))
            for w in words if str(w.get("text") or "").strip()]
    # 문장 덩어리로 자른 뒤, 한 줄에 같이 드는 이웃 덩어리는 다시 합친다
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for t in toks:
        cur.append(t)
        if t["hard"] or t["text"].endswith(SENTENCE_ENDINGS):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    merged_chunks: list[list[dict]] = []
    for ch in chunks:
        if merged_chunks:
            prev_ch = merged_chunks[-1]
            joined = " ".join(t["text"] for t in prev_ch + ch)
            if len(prev_ch) + len(ch) <= SUB_MAX_WORDS and len(joined) <= SUB_MAX_CHARS:
                merged_chunks[-1] = prev_ch + ch
                continue
        merged_chunks.append(ch)

    lines: list[dict] = []
    for sentence in merged_chunks:
        ends = _balanced_breaks([len(t["text"]) for t in sentence],
                                [t["text"] for t in sentence])
        i0 = 0
        for e in ends:
            cur = sentence[i0:e + 1]
            lines.append({"start": max(t_in, float(cur[0]["t0"]) - SUB_LEAD_SEC),
                          "end": float(cur[-1]["t1"]),
                          "text": " ".join(t["text"] for t in cur)})
            i0 = e + 1

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


# 저확신 전사 → 청취 우선(2026-09-08 사용자 지시, 가왕쇼 ep7ex02 실사고): whisper 가 발음이 뭉개진
# 대사를 "2명만 젖었잖아 … 손도 쪼꼬 방향이" 로 냈고 Stage 2 는 "두 명이면 더 좋잖아 … 손붙잡고
# 가면 좋잖아" 로 정확히 들었는데, 어절 정렬 교정은 자모 차이 ≤2~3 만 뒤집어 못 잡았다. 그 span 의
# whisper 단어 확신도는 0.09~0.5. 실측(2편 937 span): 평균 확신 p10 0.63~0.72 · p50 0.86 — 0.6 미만은
# 하위 ~8%. 그 구간에서 청취가 whisper 와 다르면 대체로 청취가 맞지만, 청취가 문장을 **요약**한 경우
# (whisper 20자 → 청취 7자)도 있어 길이 비율 하한을 함께 건다(요약은 대사를 지운다).
HEARD_PREFER_MAX_PROB = 0.6     # span 의 whisper 평균 확신이 이 미만이면 저확신
HEARD_PREFER_MIN_LEN_RATIO = 0.6  # 청취 글자수(공백 제외) ≥ whisper 의 이 비율일 때만(요약 방지)
_HEARD_CMP_STRIP = re.compile(r"[\s,.!?…~\"'·\-]+")   # 비교용 정규화(구두점·공백 제거)


def prefer_heard(words: list[dict], heard: str, *, max_prob: float = HEARD_PREFER_MAX_PROB,
                 min_len_ratio: float = HEARD_PREFER_MIN_LEN_RATIO) -> dict | None:
    """저확신 span 판정 — 청취를 쓸 거면 {mean_prob, whisper, heard} 를, 아니면 None. 순수.
    조건: whisper 단어 ≥2 · 평균 prob < max_prob · 청취 비어 있지 않음 · 공백 제거 텍스트가
    다름 · 청취 길이 ≥ whisper 길이 × min_len_ratio."""
    ws = [w for w in words or [] if str(w.get("text") or "").strip()]
    heard = str(heard or "").strip()
    if len(ws) < 2 or not heard:
        return None
    probs = [float(w.get("prob", 1.0)) for w in ws]
    mean = sum(probs) / len(probs)
    if mean >= max_prob:
        return None
    wtxt = " ".join(str(w["text"]).strip() for w in ws)
    # 구두점·공백 차이만이면 뒤집지 않는다 — 청취 경로는 어절 타임코드가 없어(균등 배분)
    # 쉼표 하나 얻자고 단어 싱크를 버리게 된다(실측: 채택 23건 중 8건이 구두점 차이뿐).
    a, b = _HEARD_CMP_STRIP.sub("", wtxt), _HEARD_CMP_STRIP.sub("", heard)
    if a == b or len(b) < len(a) * min_len_ratio:
        return None
    return {"mean_prob": round(mean, 2), "whisper": wtxt, "heard": heard}


# ── 화면 묘사 증인(2026-09-09, 가왕쇼 8화 「꼬무줄」 실사고) ──────────────────────────────
# whisper 「꼬물들밖에」 · Stage 2 청취 「고무줄밖에」 · Stage 2 화면 묘사 「티켓을 묶어놨던 고무줄만
# 남았다」. 청취가 맞았는데 세 안전장치가 전부 놓쳤다: 각색 복원은 차이(0.545)가 커서 whisper 편,
# 어절 정렬은 자모 차이 4 로 상한 초과, 저확신 우선은 평균 prob 0.70 으로 임계 미달.
# Stage 2 가 영상을 보며 적어 둔 화면 묘사(`scene_script`)를 세 번째 증인으로 쓴다 — 청취에서
# whisper 와 다른 어절의 어간이 **같은 조각의 화면 묘사**에 있으면 각색이 아니라 화면이 뒷받침하는
# 단어다(모델이 문장을 지어냈다면 화면 묘사에 그 단어가 같이 들어갈 확률은 낮다). 영상을 다시 보지
# 않는다 — 이미 있는 텍스트 두 칸의 대조다. 어간 = 어절 앞부분(길이 ≥2, 긴 쪽부터), whisper 텍스트에
# 없는 것만(「티켓」처럼 양쪽에 다 있는 단어는 증거가 아니다). 지시어·감탄사는 textcheck 와 같은 목록으로 뺀다.
# 어간 규칙(길이·지시어·인명 제외)은 `textcheck.scene_stem` 한 곳 — 어절 단위(arbitrate_scene)와
# span 단위(여기)가 같은 자로 잰다.
# 문장 유사도 하한(공백 제거 difflib ratio) — 이 규칙은 **단어 하나의 시비**를 화면으로 가리는 것이지
# 문장 전체를 갈아끼우는 것이 아니다. 드라이런(가왕쇼 8화 808 span)에서 유사도 가드 없이 150건이 걸렸고
# 그중 「인천의 아들입니다」→「전유진 많이 투표해 주세요」처럼 전혀 다른 문장(청취가 옆 조각을 들었거나
# 요약)이 섞여 있었다. 0.5 는 「꼬물들」 span(0.68)은 지나고 위 사례(0.1대)는 막는 자리.
SCENE_SIM_MIN = 0.5
SCENE_SIM_MIN_MULTI = 0.35   # 화면 어간 2개 이상 · whisper ≤3어절 · 청취 ≤2배 일 때


PHONETIC_ANCHOR_MIN = 0.5    # 청취 어간 어절 ↔ whisper 어절 자모 유사도 하한(발음 닻, 3음절+)
PHONETIC_ANCHOR_MIN_SHORT = 0.6   # 2음절 이하 — 짧은 어절은 한 음절만 겹쳐도 0.5 를 넘는다(「시간」↔「세계란에」 0.57 · 「남은」↔「나왔습니다」 0.44)


def scene_backed_heard(words: list[dict], heard: str, scene_script: str, *,
                       min_len_ratio: float = HEARD_PREFER_MIN_LEN_RATIO,
                       min_similarity: float = SCENE_SIM_MIN,
                       exclude: set[str] | frozenset[str] | None = None,
                       missed_words: bool = False) -> dict | None:
    """화면 묘사가 뒷받침하는 청취 판정 — 채택이면 {whisper, heard, stem, similarity}, 아니면 None. 순수.
    조건: whisper 단어 ≥2 · 청취·화면 묘사 비어 있지 않음 · 공백 제거 텍스트가 다르되 유사도 ≥
    min_similarity(단어 시비이지 문장 교체가 아니다) · 청취가 요약이 아님(길이 비율) · 청취 어절 중
    어간(≥SCENE_STEM_MIN_CHARS)이 화면 묘사에는 있고 whisper 에는 없는 것이 하나 이상.
    exclude: 어간으로 치지 않을 단어(인물 이름 — 화면 묘사에는 인명이 거의 늘 있어 증거가 못 된다.
    인명은 별도 인명 대조 규칙의 몫)."""
    import difflib
    ws = [w for w in words or [] if str(w.get("text") or "").strip()]
    heard = str(heard or "").strip()
    scene = str(scene_script or "").strip()
    # 단어 1개 span 도 본다(prefer_heard 와 다른 점 — 「왔잖아요?」 하나만 남은 span 의 청취
    # 「저희 300장 들고 왔잖아요」: whisper 가 앞 단어들을 놓쳤다. 유사도·어간 증거가 가드다)
    if len(ws) < 1 or not heard or not scene:
        return None
    wtxt = " ".join(str(w["text"]).strip() for w in ws)
    a, b = _HEARD_CMP_STRIP.sub("", wtxt), _HEARD_CMP_STRIP.sub("", heard)
    if a == b or len(b) < len(a) * min_len_ratio:
        return None
    sim = difflib.SequenceMatcher(None, a, b).ratio()
    from app.v3.textcheck import phonetic_sim, read_digits_ko, scene_stem
    # 발음 닻(2026-09-09 ep8ex01 「15분」 실사고): 청취 「남은 홍보 시간 15분 남았습니다」는 실제 발화 「자 여러분 이제
    # 15분 남았습니다 15분 안에」의 **요약**인데, 화면 묘사가 같은 모델의 같은 요약(「남은 홍보 시간이 15분이라고
    # 알리자」)이라 어간 남은·홍보·시간이 전부 '증거'로 잡혔다 — 청취와 화면 묘사는 같은 호출의 산물이라 서술어에서는
    # 독립 증인이 아니다. 화면 증인은 whisper 가 **잘못 들은** 단어를 바로잡는 것이지 모델의 다른 문장을 들이는 게
    # 아니므로, 어간을 든 청취 어절은 whisper 어절 중 **발음이 닮은 것**(자모 유사도 ≥ PHONETIC_ANCHOR_MIN)이 있어야
    # 한다(꼬물들↔고무줄 · 산맥장↔삼백장 · 오호↔홍보). 예외는 missed_words(whisper 가 말을 통째로 놓쳐 바로 앞이
    # 무성 조각 — 「저희 300장 들고」): 닮은 어절이 있을 리 없다.
    stems: list[str] = []
    wtoks = [str(w["text"]).strip() for w in ws]
    for tok in heard.split():
        stem = scene_stem(_HEARD_CMP_STRIP.sub("", tok), scene, wtxt, exclude)
        if not stem or stem in stems:
            continue
        _syl = len(read_digits_ko(_HEARD_CMP_STRIP.sub("", tok)))
        _need = PHONETIC_ANCHOR_MIN if _syl >= 3 else PHONETIC_ANCHOR_MIN_SHORT
        if not missed_words and max((phonetic_sim(tok, wt) for wt in wtoks), default=0.0) < _need:
            continue
        stems.append(stem)
    if not stems:
        return None
    # 짧은 span(whisper ≤3어절)은 단어 둘만 달라도 유사도가 0.4 대로 떨어진다(「5호 30초입니다」↔「홍보 30분
    # 남았습니다」 0.44). 어간이 **둘 이상** 화면에 있고 청취가 whisper 의 2배를 안 넘으면(긴 문장을 짧은
    # 조각에 욱여넣는 「50표!」→8어절 방지) 하한을 SCENE_SIM_MIN_MULTI 로 낮춘다. 드라이런: 3건만 추가로 걸림.
    multi = len(stems) >= 2 and len(ws) <= 3 and len(b) <= 2 * len(a)
    if sim < (SCENE_SIM_MIN_MULTI if multi else min_similarity):
        return None
    return {"whisper": wtxt, "heard": heard, "stem": stems[0], "stems": stems, "similarity": round(sim, 2)}


# 메아리 조각(2026-09-09, 가왕쇼 8화 「들고」 실사고): 「저희 300장 들고 왔어요」 바로 뒤에 whisper 가
# 확신 0.10 짜리 단어 하나(「들고」)를 따로 span 으로 냈다 — 앞 줄의 꼬리를 되풀이한 조각이고 화자 색까지
# 달라(빈예서) 화면에 노란 「들고」가 혼자 떴다. 단어 1개 · 극저확신 · 직전 유성 span 텍스트에 이미 있는
# 단어 · 틈 ≤1s 일 때만 버린다(감탄사 「와!」「어?」는 앞 줄에 없으니 산다 — E14 규율).
from app.v3.story_flow.common import nospace_len  # noqa: E402  (창 확장 판정)

ECHO_MAX_PROB = 0.2
ECHO_GAP_SEC = 1.0


def is_echo_fragment(words: list[dict], prev_text: str, gap_sec: float, *,
                     max_prob: float = ECHO_MAX_PROB, max_gap: float = ECHO_GAP_SEC) -> bool:
    """단어 1개짜리 span 이 직전 유성 span 의 메아리인가. 순수."""
    ws = [w for w in words or [] if str(w.get("text") or "").strip()]
    if len(ws) != 1 or not prev_text or gap_sec is None or gap_sec > max_gap:
        return False
    if float(ws[0].get("prob", 1.0)) >= max_prob:
        return False
    t = _HEARD_CMP_STRIP.sub("", str(ws[0]["text"]))
    # 어절 단위 완전 일치(부분 문자열이면 「어?」가 '…어…' 아무 데나 붙어 감탄사가 죽는다 — 드라이런 2건)
    prev_toks = {_HEARD_CMP_STRIP.sub("", x) for x in str(prev_text).split()}
    return len(t) >= 1 and t in prev_toks


import re as _re
SPEECH_HINT = _re.compile(r"말한|말하|말을|알린|알리|묻는|물어|대답|답한|설명|이야기|소리친|외친|외치|권한|안내|고지|제안|부탁|요청|인사|"
                          r"한숨|중얼|연호|유도|당부|응원|감사|호소|질문|멘트|진행")
# 화면 묘사의 노래 근거는 **단어**만(따옴표 패턴 제외): 「관객들이 '최수호 최고다'를 연호」·「'전유진'을 외친다」의 따옴표가
# 곡명으로 잡혀 무대 뒤 MC 멘트 8줄이 가사로 버려졌다(2026-09-09 ep8ex02). 곡명 패턴은 사건 단위 문장(창 확정)에서만.
SCENE_SING_WORDS = _re.compile(r"부르|열창|노래|후렴|코러스|떼창|가창|듀엣|앙코르|곡|라이브|버스킹|가사")


def span_sings(sp: dict) -> bool:
    """이 조각이 노래인가 — Stage 2 의 두 기록(사건 단위 문장 · 조각 화면 묘사) 중 하나라도 노래
    근거(`singing.SING_HINT`)를 말하면 참. 순수.

    2026-09-09 가왕쇼 8화 실사고: 음향 창(29:04~29:32)이 「촉이 와요」 사건 단위와 겹쳐 창 전체가
    확정됐는데, 노래는 29:09 에 끝났고 나머지 22초는 완판 사건 단위였다 — 「솔드아웃!」「300장 들고
    왔어요!」 4줄이 가사로 분류돼 사라졌다. 창은 창대로 두고(전유진 편에서 창을 단위 경계에서 자르다
    노래를 잘라먹은 이력) **줄 단위**로 두 번째 증인을 본다: 조각이 속한 사건 단위 문장이나 조각의
    화면 묘사에 노래 근거가 있어야 버린다. 관객 컷 위의 가사는 사건 단위 문장이 잡고, 단위 경계가
    어긋나 옆 단위로 넘어간 가사는 화면 묘사(「…를 열창한다」)가 잡는다."""
    from app.v3.singing import SING_HINT
    scene = str(sp.get("scene_script") or "")
    if SCENE_SING_WORDS.search(scene):
        return True
    # 조각 화면 묘사가 발화(알린다·말한다·묻는다…)를 적고 노래 말이 없으면 노래 위 대사다 — 단위 문장이
    # 노래(즉석 라이브)라도 그 줄은 산다(ep8ex01 「남은 홍보 시간 15분 남았습니다」 제작진 고지가 무대 단위 안).
    if scene and SPEECH_HINT.search(scene):
        return False
    return bool(SING_HINT.search(str(sp.get("meaning_content") or "")))


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
# 노랑(#FFE94A)은 내레이션 자막 색이라 화자색에서 뺐다(2026-09-08 사용자 지시 — 대사와
# 내레이션 색이 겹치면 안 된다). 빨강도 강조 자막 색이라 마지막 순위.
SPEAKER_PALETTE = ("#FFB637", "#7ED0FF", "#FF5540")
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
                   name_fix_log: list[dict] | None = None,
                   skip_windows: list[tuple[float, float]] | None = None,
                   skip_log: list[dict] | None = None,
                   fps: float | None = None) -> list[dict]:
    """채택 유성 span(뮤트 제외) → 어절 자막 세그먼트(**편집본 좌표** — C6).

    ⚠ 편집본 오프셋 누적은 `clip_duration(clip_len(c), fps)` — 다른 좌표 소비자(cue·뮤트 창·
    라벨)와 **같은 자**다. 종전 `c1 - c0` 누적은 덮개의 hold_sec 과 프레임 격자를 빼먹어, 붙잡은
    덮개(hold 1.835s) 뒤의 **모든 대사 자막이 1.8초 일찍** 나갔다(2026-09-08 ep01full 실측 —
    "대사 자막 타이밍이 다 어긋나있어"). fps 를 안 주면 격자 없이 hold_sec 만 더한다.

    skip_windows(2026-09-07): 자막을 내지 않을 **소스** 구간(노래 — `singing`). 줄의
    소스 중점이 창 안이면 그 줄을 버리고 skip_log 에 건별 기록한다. None/빈 = 종전 동일.

    단어 소속은 중점 기준(span 재단과 같은 규율). 자막은 원본 오디오 인용에만 —
    내레이션 텍스트는 cue 가 나른다(편집실이 cue.text 로 오버레이)."""
    segments: list[dict] = []
    colors = speaker_colors(span_index)
    # 뮤트 클립이라도 내레이션 창 **밖**은 원음이 살아 있다(finalize 의 muted_windows
    # 와 같은 계산) — 그 구간 대사는 자막이 있어야 한다. 창을 모르면 종전대로 전부 제외.
    mw = list(mute_windows or [])
    # 이웃 화면 묘사(2026-09-09): span 단위 화면 증인은 같은 사건 단위의 앞뒤 조각 화면 묘사도 본다 —
    # whisper 가 놓친 말("저희 300장 들고")은 대개 **앞 조각**(무성으로 잡힌 자리)의 화면에 적혀 있다.
    _by_pos = {sp.get("pos"): sid for sid, sp in span_index.items() if sp.get("pos") is not None}

    def _neighbor_scene(sid: str) -> str:
        sp = span_index[sid]
        parts = [str(sp.get("scene_script") or "")]
        for d in (-1, 1):
            nb = _by_pos.get((sp.get("pos") or 0) + d)
            if nb and span_index[nb].get("meaning_idx") == sp.get("meaning_idx"):
                parts.append(str(span_index[nb].get("scene_script") or ""))
        return " ".join(p for p in parts if p)
    off = 0.0
    # 직전 유성 span(텍스트, t_out) — 메아리 판정. 클립을 넘어도 유지한다: 편성이 같은 대화를 클립 둘로
    # 재단하는 일이 흔하고(「들고」는 앞 줄과 0.02s 차이인데 다른 클립이었다), 판정은 **소스 시각** 틈으로
    # 하므로 점프 뒤에는 틈이 커서 안 걸린다.
    _prev_audio: tuple[str, float] | None = None
    for c in timeline:
        c0, c1 = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        audible: list[tuple[float, float]] = []
        if c.get("use_original_audio"):
            audible = [(c0, c1)]
        elif mw:
            audible = [(a, z) for a, z, on in split_by_windows(c0, c1, mw) if on]
        if not audible:
            off += clip_duration(clip_len(c), fps)
            continue
        _silent_lead: float | None = None                # 직전 연속 무성 조각의 시작 — 놓친 말의 자리
        for sid in c.get("span_ids") or []:
            sp = span_index[sid]
            if not sp["is_audio"]:
                if _silent_lead is None:
                    _silent_lead = sp["t_in"]
                continue
            # M9-C 전사 판정을 자막에 반영(리뷰 확정 critical — 판정이 stage2
            # 기록에만 남고 화면에는 깨진 전사가 그대로 나가던 결함):
            #   none  → 대사 확보 실패, 자막 없음(로그·docstring 의 약속 이행)
            #   heard → grid 단어는 깨진 전사다. 모델이 들은 문장을 span 구간에
            #           균등 배치한다(어절 타임코드가 없으므로 팝인 대신 균등).
            src = sp.get("text_source")
            if src == "none":
                _silent_lead = None
                continue
            in_span = [w for w in grid_words
                       if sp["t_in"] <= (float(w["t0"]) + float(w["t1"])) / 2
                       < sp["t_out"]]
            if _prev_audio is not None and is_echo_fragment(in_span, _prev_audio[0], sp["t_in"] - _prev_audio[1]):
                if name_fix_log is not None:
                    name_fix_log.append({"kind": "echo", "span_id": sid,
                                         "from": " ".join(str(w["text"]) for w in in_span), "to": ""})
                _prev_audio = (" ".join(str(w.get("text") or "") for w in in_span), sp["t_out"])
                _silent_lead = None
                continue
            _lead_in = _silent_lead if (_silent_lead is not None and sp["t_in"] - _silent_lead <= 3.0) else None
            _silent_lead = None
            # 저확신 전사 → 청취 우선(위 prefer_heard). 판정은 span 단위·순수, 건별 기록.
            _ph = None if src == "heard" else prefer_heard(in_span, sp.get("heard_text"))
            if _ph is not None:
                src = "heard"
                if name_fix_log is not None:
                    name_fix_log.append({"kind": "heard", "span_id": sid, "from": _ph["whisper"],
                                         "to": _ph["heard"], "mean_prob": _ph["mean_prob"]})
            _scene_excl = (set(sp.get("characters") or []) | set(sp.get("meaning_characters") or [])
                           | set(cast_names or []))
            if src != "heard":
                # 화면 묘사 증인(2026-09-09, 「꼬물들」→「고무줄」): ① 어절 단위 — 정렬된 청취 조각의
                # 어간이 화면 묘사에 있으면 그 어절만 뒤집는다(whisper 타임코드 보존, fix_span_words
                # 안 arbitrate_scene). ② 정렬로 못 잡은 경우(whisper 가 어절을 빠뜨리거나 길이가 다른
                # 「산맥장」→「300장」)만 span 단위 폴백(scene_backed_heard) — 청취 문장을 균등 배분.
                _heard = str(sp.get("heard_text") or "")
                if cast_names or _heard:
                    from app.v3.textcheck import fix_span_words
                    _raw_span = list(in_span)
                    in_span, _fx = fix_span_words(in_span, cast_names or [], _heard,
                                                  scene_script=str(sp.get("scene_script") or ""),
                                                  exclude=_scene_excl)
                    if _fx and name_fix_log is not None:
                        name_fix_log.extend(dict(f, span_id=sid) for f in _fx)
                    # span 폴백은 **교정 전** whisper 로 잰다(어절 교정이 「홍보」를 넣고 나면 그 어간이 증거에서
                    # 빠져 「홍보 30초입니다」 반쪽 교정으로 끝난다). 어절 교정이 있었으면 어간 2개 이상일 때만
                    # span 채택이 이긴다(하나면 타임코드 보존 쪽이 낫다).
                    _n_word_scene = sum(1 for f in _fx if f.get("kind") == "scene")
                    _sb = scene_backed_heard(_raw_span, _heard, _neighbor_scene(sid), exclude=_scene_excl,
                                             missed_words=(_lead_in is not None and nospace_len(_heard)
                                                           > nospace_len(" ".join(w["text"] for w in _raw_span))))
                    # 어절 교정이 있었으면: 짧은 span(≤3어절)에서 화면 증거가 둘 이상(어절 교정 + span 어간 합산 —
                    # 「홍보」는 어절 단위가, 「30분」은 span 단위가 잡는다)일 때만 span 채택이 이긴다.
                    if _sb is not None and _n_word_scene and not (
                            len(_raw_span) <= 3 and _n_word_scene + len(_sb.get("stems") or []) >= 2):
                        _sb = None            # 긴 span 은 어절 교정(타임코드 보존)이 이긴다
                    if True:
                        if _sb is not None:
                            src = "heard"
                            if name_fix_log is not None:
                                name_fix_log.append({"kind": "scene_span", "span_id": sid,
                                                     "from": _sb["whisper"], "to": _sb["heard"],
                                                     "stem": _sb["stem"], "stems": _sb.get("stems"),
                                                     "similarity": _sb["similarity"]})
            if src == "heard":
                # 창 확장(2026-09-09): 청취가 whisper 보다 길고 바로 앞이 무성으로 잡힌 조각이면 whisper 가
                # 거기서 말을 놓친 것(「저희 300장 들고」 = 앞 조각 0.86s) — 청취 문장을 그 조각 시작부터 편다.
                _t_in = sp["t_in"]
                if _lead_in is not None and nospace_len(str(sp.get("heard_text") or "")) > nospace_len(
                        " ".join(str(w.get("text") or "") for w in in_span)):
                    _t_in = max(c0, _lead_in)
                lines = _lines_from_text(str(sp.get("heard_text") or ""), _t_in, sp["t_out"])
            else:
                # 인명·영문·맞춤법·정렬·화면 묘사 대조는 위 블록(fix_span_words)이 이미 돌았다 —
                # 어절 타임코드 보존 경로. (2026-09-03 인명 대조 · 09-04 영문 오인식 · 09-09 화면 증인)
                lines = _lines_for_span(in_span, sp["t_in"], sp["t_out"])
            _prev_audio = (" ".join(str(w.get("text") or "") for w in in_span), sp["t_out"])
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
                if skip_windows and any(a <= mid < z for a, z in skip_windows):
                    _sings = span_sings(sp)
                    if skip_log is not None:
                        skip_log.append({"span_id": sid, "src_start": round(ln["start"], 3),
                                         "src_end": round(ln["end"], 3), "edit_start": e0,
                                         "text": ln["text"],
                                         **({} if _sings else {"kept": True})})
                    if _sings:
                        continue                  # 노래 구간 — 가사 자막을 내지 않는다
                    # 창 안이지만 이 조각의 Stage 2 기록에 노래 근거가 없다(위 span_sings) —
                    # 창이 옆 사건 단위로 흘러든 대사다. 자막을 살리고 기록만 남긴다.
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
        off += clip_duration(clip_len(c), fps)
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
        # 훅 회수(4단계): 같은 소스 구간이 두 클립에 있으면 cue 의 beat 와 같은 클립을 먼저
        # 찾는다(첫 등장으로 새지 않게). 클립에 beat 가 없거나(옛 plan) 못 찾으면 종전 전체 탐색.
        _own = [o for o, c in zip(offsets, timeline)
                if cue.get("beat") is not None and c.get("beat") == cue.get("beat")]
        e0 = to_edited_sec(cue["source_time_sec"], _own, kind="start") if _own else None
        e1 = to_edited_sec(cue["source_end_sec"], _own, kind="end") if _own else None
        if e0 is None:
            e0 = to_edited_sec(cue["source_time_sec"], offsets, kind="start")
        if e1 is None:
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
            # 소스 창 끝도 신원의 일부(2026-09-09, additive): 오버라이드 cue 승계가 hold 덮개 위 cue 의 끝을
            # start+duration 으로 재면 클립 밖이 되어 드랍된다 — 계획의 창 끝을 그대로 싣는다.
            "source_end_sec": cue.get("source_end_sec"),
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
