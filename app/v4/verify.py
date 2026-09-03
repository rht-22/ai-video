"""6c 구간 검증 — 후보 조각의 시간이 **전사와 맞는지** 코드로 대조한다(LLM 0콜).

계약 정본 `docs/v4/M1-interfaces.md` §4 · 기획 `docs/v4/v4-plan.md` §6c(운영자 결정 O5).

왜 이 단계가 v4 의 시간 방어 핵심인가 — 조상은 v1 `app/modules/timestamp_check.py` 다.
그 모듈 독스트링의 2026-08-06 샤먼: 미신전 2화 실측이 이 단계가 막는 실패의 원형이다:

    후보가 주장한 구간   830~887
    인용 대사의 실제 위치 518.9~524.2   ← 같은 청크의 시간축이 통째로 311초 밀렸다

내용은 맞고 시간만 밀린 응답은 길이·부등호 검사로는 안 잡힌다(범위 안이니까) — 그대로
렌더돼 **엉뚱한 장면이 조용히 발행된다**. 같은 응답의 다른 후보는 소스(875s) 밖인
933~997 을 주장했고, 그 계열의 사고가 2026-08-24 에 **반쪽 쇼츠 5건**(4채널 · 18~26.6초
소실)으로 실측됐다.

v4 가 넓히는 것은 하나뿐이다: v1 후보는 `{start_sec, end_sec, transcript, beats}` 단일
구간인데 **v4 후보는 조각 배열**(`segments[{start_sec, end_sec, quote}]`)이다. 판정 자체는
`timestamp_check` 를 **부른다**(옮기지도 베끼지도 않는다 — v1 이 계속 쓰고, 베낀 판정은
언젠가 한쪽만 고쳐진다).

판정 셋:
  · **ok**        — quote 가 그 조각 안(±관용)의 전사에서 발견됨
  · **relocated** — quote 가 **다른 시각에서만** 발견됨 → 그 시각으로 조각을 옮긴다(길이 유지).
                    드롭보다 재배치가 먼저다 — 위 실측이 "내용은 맞고 시간축만 밀린다"이므로
                    버리면 멀쩡한 소재를 잃는다.
  · **dropped**   — 어디에도 없음(환각) 또는 소스 밖

그리고 **모르는 것은 틀렸다고 하지 않는다**(timestamp_check 의 규율 승계):
전사가 통째로 비면 ②③ 을 판정하지 않고, 짧은 인용(<10자)은 우연 일치가 잦아 대조 대상이
아니다 — 짧은 quote 를 환각으로 몰면 감탄사·노래 구간이 통째로 날아간다.
"""
from __future__ import annotations

from typing import Any

from app.modules import timestamp_check

# 격자 눈금 관용(④)의 정본은 승격된 격자 패키지다(§7) — v3 를 거치지 않는 것이 승격의
# 이유다(v3 은퇴 시 v4 가 함께 끊기면 안 된다). 패키지가 상수를 아직 올리지 않았으면
# 모듈에서 직접 가져온다. 둘 다 없으면 그대로 터진다 — 값을 여기 적으면 관용치가 두 벌이
# 되고, 베낀 수식은 언젠가 한쪽만 고쳐진다.
try:
    from app.modules.grid import SNAP_TOLERANCE_SEC
except ImportError:                                     # pragma: no cover - 패키지 재수출 전
    from app.modules.grid.schemas import SNAP_TOLERANCE_SEC

# ⚠ 관용치는 v1 과 **같은 자**여야 한다(계약 §4). 값을 여기 다시 적으면 한쪽만 고쳐진다 —
#   그래서 베끼지 않고 가져온다. 값이 5.0 인 것은 테스트가 별도로 못박는다(v1 이 바꾸면 걸린다).
QUOTE_MATCH_TOLERANCE_SEC = timestamp_check.TOLERANCE_SEC   # = 5.0
MIN_CANDIDATE_SEC = 40.0     # 조각을 잃고 이보다 짧아지면 후보 드롭(길이 정책 하한)
MIN_SEGMENT_SEC = 1.0        # 이보다 짧은 조각은 렌더돼도 화면에서 사라진다

# 후보 전체에 대한 노트의 조각 번호. 조각별 노트는 0-based 인덱스를 쓴다.
CANDIDATE_NOTE_INDEX = -1


# ── 전사 재료 ────────────────────────────────────────────────────────────────
def _speech_spans(segments: Any) -> list[tuple[float, float]]:
    """전사 세그먼트 → 발화 구간 목록. 순수.

    ⚠ 세그먼트는 dict(체크포인트 JSON)와 SpeechSegment 객체(실행 경로) **둘 다** 온다.
    dict 전용 `s.get()` 만 쓰면 실행 경로에서 AttributeError 로 생성이 통째로 죽는다
    (2026-08-06 맥5 실측 — timestamp_check._timeline 이 같은 자리에서 같은 이유로 고쳐졌다).
    글자가 없는(정규화 후 빈) 세그먼트는 발화로 세지 않는다 — 대조에 쓰이지 않는 재료다."""
    out: list[tuple[float, float]] = []
    for s in segments or []:
        if isinstance(s, dict):
            raw_text, raw_st, raw_en = s.get("text"), s.get("start_sec"), s.get("end_sec")
        else:
            raw_text = getattr(s, "text", None)
            raw_st = getattr(s, "start_sec", None)
            raw_en = getattr(s, "end_sec", None)
        if not timestamp_check.normalize(raw_text):
            continue
        try:
            st, en = float(raw_st), float(raw_en)
        except (TypeError, ValueError):
            continue
        out.append((st, en))
    return out


def _has_speech_in(spans: list[tuple[float, float]], start: float, end: float) -> bool:
    """[start, end) 안에 전사 단어가 하나라도 있는가(③의 재료). 순수."""
    return any(en > start and st < end for st, en in spans)


def _grid_times(grid_times: Any) -> list[float]:
    """눈금 목록 정규화. 값이 숫자가 아니면 **크게 실패**한다 — 눈금은 우리가 만든
    격자(grid.json)에서 오므로 숫자가 아니면 상류 계약 위반이고, 조용히 건너뛰면
    ④ 경고가 통째로 사라진다."""
    if not grid_times:
        return []
    out = []
    for t in grid_times:
        try:
            out.append(float(t))
        except (TypeError, ValueError):
            raise ValueError(f"grid_times 에 숫자가 아닌 값: {t!r}")
    return sorted(out)


def _snap_gap(grid: list[float], t: float) -> float:
    """가장 가까운 눈금까지의 거리. 눈금이 없으면 0.0(판정하지 않는다)."""
    if not grid:
        return 0.0
    return min(abs(g - t) for g in grid)


# ── 조각 판정 ────────────────────────────────────────────────────────────────
def _parse_span(seg: dict) -> tuple[float, float] | None:
    """조각의 (start, end). 읽을 수 없거나 뒤집혀 있으면 None. 순수."""
    try:
        start, end = float(seg.get("start_sec")), float(seg.get("end_sec"))
    except (TypeError, ValueError):
        return None
    if not (end > start):
        return None
    return start, end


def _pick_hit(hits: list[tuple[float, float]], origin: float) -> tuple[float, float]:
    """재배치 목표. 같은 대사가 여러 번 나오면 **원위치에서 가장 가까운 것**을 고르고,
    거리가 같으면 이른 쪽(결정성 — 동점은 명시 규칙으로 깬다)."""
    return min(hits, key=lambda h: (abs(h[0] - origin), h[0]))


def _conflicts(new_span: tuple[float, float], others: list[tuple[int, tuple[float, float]]],
               idx: int) -> str | None:
    """재배치가 다른 조각과 겹치거나 **편집 순서를 뒤집는가** → 사유 또는 None. 순수.

    조각 순서는 곧 편집 순서다. 겹치면 같은 화면이 두 번 나가고, 순서가 뒤집히면
    이야기가 거꾸로 붙는다 — 둘 다 재배치로 얻는 것보다 잃는 것이 크므로 **재배치를
    포기하고 원위치를 유지**한다(드롭도 아니다 — 판정 근거가 없는 쪽으로 기울인다)."""
    ns, ne = new_span
    for j, (os_, oe) in others:
        if ne > os_ and ns < oe:
            return f"재배치 자리가 조각{j}[{os_:.1f}, {oe:.1f}] 와 겹칩니다"
        if j < idx and os_ > ns:
            return f"재배치하면 조각{j} 보다 앞서 편집 순서가 뒤집힙니다"
        if j > idx and os_ < ns:
            return f"재배치하면 조각{j} 보다 뒤로 가 편집 순서가 뒤집힙니다"
    return None


def verify_candidate(cand: dict, *, segments: list[dict], source_duration_sec: float,
                     grid_times: list[float] | None = None) -> dict:
    """후보 하나 → 판정. 순수 — 넘겨받은 dict 를 절대 제자리에서 고치지 않는다.

    조각마다 넷을 본다(계약 §4):
      ① 소스 범위   — `timestamp_check.bounds_problem` 재사용(drop | clamp)
      ② 인용 실재   — quote 가 있으면 `timestamp_check.find_quote_times` 로 찾는다
      ③ 발화 커버리지 — quote 가 있는데 그 조각에 전사 단어가 하나도 없으면 dropped
      ④ 경계 눈금   — ±SNAP_TOLERANCE_SEC 안에 눈금이 없으면 **경고만**(드롭 아님)

    판정 순서의 규율:
      · ②가 관용(±QUOTE_MATCH_TOLERANCE_SEC) 안에서 대사를 확인하면 ③은 보지 않는다.
        관용은 경계 오차를 위해 있는 것인데, 대사가 조각 끝 2초 뒤에 있다고 그 조각을
        버리면 관용을 둔 이유가 없어진다.
      · ③은 ②가 확인하지 못한 조각의 **드롭 사유를 가른다**: 무성 조각에 대사를 붙인
        것인가(③), 전사 어디에도 없는 환각인가(②).
      · 전사가 통째로 비면 ②③ 을 건너뛴다. ①④ 는 전사와 무관하므로 그대로 돈다 —
        `timestamp_check.filter_candidates` 가 bounds 를 전사 조기 반환 **앞**에 둔 것과
        같은 규율이다(전사가 비었다고 건너뛰면 대사 없는 편이 그대로 반쪽이 된다).

    반환 `{"id", "verdict", "segments", "total_sec", "notes"}`.
    notes 의 `segment` 는 0-based 조각 번호이고, 후보 전체에 대한 노트는
    CANDIDATE_NOTE_INDEX(-1)다. action 어휘는 ok|relocated|clamped|dropped|unsnapped."""
    cand_id = cand.get("id")
    if not isinstance(cand_id, str) or not cand_id:
        # id 는 checkpoint_candidates.json 의 좌표다(§8) — 없으면 verify·funnel·approve 가
        # 서로를 못 가리킨다. 조용히 만들어 붙이면 그 좌표가 단계마다 달라진다.
        raise ValueError(f"후보에 id 가 없습니다: {cand!r}")

    raw_segments = cand.get("segments")
    if raw_segments is None:
        raw_segments = []
    if not isinstance(raw_segments, list):
        raise ValueError(f"후보 {cand_id}: segments 가 목록이 아닙니다({type(raw_segments).__name__})")

    grid = _grid_times(grid_times)
    speech = _speech_spans(segments)
    has_transcript = bool(speech)

    # 원본 조각의 자리 — 재배치 충돌 검사가 '아직 안 본 뒤쪽 조각'의 자리로 쓴다.
    originals: list[tuple[float, float] | None] = []
    for seg in raw_segments:
        originals.append(_parse_span(seg) if isinstance(seg, dict) else None)

    kept: list[dict] = []
    finals: list[tuple[int, tuple[float, float]]] = []   # 확정된 앞쪽 조각들의 자리
    notes: list[dict] = []
    relocated_any = False

    for idx, seg in enumerate(raw_segments):
        if not isinstance(seg, dict):
            raise ValueError(f"후보 {cand_id} 조각{idx}: dict 가 아닙니다({type(seg).__name__})")

        span = originals[idx]
        if span is None:
            notes.append({"segment": idx, "action": "dropped",
                          "why": f"조각 시각을 읽을 수 없습니다: "
                                 f"start_sec={seg.get('start_sec')!r} end_sec={seg.get('end_sec')!r}"})
            continue
        start, end = span

        # ① 소스 범위 — 렌더는 `-ss start -to end` 로 읽으므로 소스 밖 조각은 프레임 0개다.
        act = timestamp_check.bounds_problem({"start_sec": start, "end_sec": end},
                                             source_duration_sec)
        if act is not None:
            action, why = act
            if action == "drop":
                notes.append({"segment": idx, "action": "dropped", "why": why})
                continue
            end = float(source_duration_sec)
            notes.append({"segment": idx, "action": "clamped", "why": why,
                          "from": [start, span[1]], "to": [start, end]})

        quote = seg.get("quote")
        quote_text = str(quote) if quote else ""
        judgeable = (has_transcript and
                     len(timestamp_check.normalize(quote_text)) >= timestamp_check.MIN_QUOTE_CHARS)

        if quote_text and not judgeable:
            # 짧은 인용은 우연 일치가 잦아 v1 도 대조하지 않는다 — 여기서 환각으로 몰면
            # 감탄사·노래 구간이 통째로 날아간다. 전사가 비었을 때도 같다(모르는 것).
            why = ("전사가 없어 대조하지 않았습니다" if not has_transcript else
                   f"인용이 짧아(정규화 {len(timestamp_check.normalize(quote_text))}자 "
                   f"< {timestamp_check.MIN_QUOTE_CHARS}) 판정하지 않았습니다")
            notes.append({"segment": idx, "action": "ok", "why": why})

        if judgeable:
            hits = timestamp_check.find_quote_times(quote_text, segments)
            lo, hi = start - QUOTE_MATCH_TOLERANCE_SEC, end + QUOTE_MATCH_TOLERANCE_SEC
            inside = [h for h in hits if lo <= h[0] <= hi or lo <= h[1] <= hi]

            if hits and not inside:
                # ② relocated — 시간축만 밀린 전형. 길이를 유지한 채 실제 발화 자리로 옮긴다.
                hs, _he = _pick_hit(hits, start)
                dur = end - start
                new_span = (hs, hs + dur)

                others = [(j, sp) for j, sp in finals]
                others += [(j, sp) for j, sp in enumerate(originals)
                           if j > idx and sp is not None]
                clash = _conflicts(new_span, others, idx)

                if clash:
                    notes.append({"segment": idx, "action": "ok",
                                  "why": f"재배치 포기 — {clash} (원위치 유지)",
                                  "declined_relocation": True,
                                  "found_at": [hs, hs + dur]})
                else:
                    rb = timestamp_check.bounds_problem(
                        {"start_sec": new_span[0], "end_sec": new_span[1]}, source_duration_sec)
                    if rb is not None and rb[0] == "drop":
                        notes.append({"segment": idx, "action": "dropped",
                                      "why": f"재배치 자리가 소스 밖입니다 — {rb[1]}"})
                        continue
                    if rb is not None:                       # clamp
                        notes.append({"segment": idx, "action": "clamped", "why": rb[1],
                                      "from": list(new_span),
                                      "to": [new_span[0], float(source_duration_sec)]})
                        new_span = (new_span[0], float(source_duration_sec))
                    if new_span[1] - new_span[0] < MIN_SEGMENT_SEC:
                        notes.append({"segment": idx, "action": "dropped",
                                      "why": f"재배치·클램프 뒤 길이 "
                                             f"{new_span[1] - new_span[0]:.2f}초 "
                                             f"< {MIN_SEGMENT_SEC}초"})
                        continue
                    notes.append({"segment": idx, "action": "relocated",
                                  "why": f"인용 대사가 [{start:.1f}, {end:.1f}] 이 아니라 "
                                         f"[{new_span[0]:.1f}, {new_span[1]:.1f}] 에 있습니다 "
                                         f"(차이 {abs(hs - start):.0f}초) — "
                                         f"\"{quote_text[:24]}…\"",
                                  "from": [start, end], "to": list(new_span)})
                    start, end = new_span
                    relocated_any = True

            elif not hits:
                # ③ 무성 조각인가, ② 환각인가 — 드롭 사유를 가른다(둘 다 드롭).
                if not _has_speech_in(speech, start, end):
                    notes.append({"segment": idx, "action": "dropped",
                                  "why": f"조각 [{start:.1f}, {end:.1f}] 에 전사 단어가 하나도 "
                                         f"없는데 대사를 주장합니다 — \"{quote_text[:24]}…\""})
                else:
                    notes.append({"segment": idx, "action": "dropped",
                                  "why": f"인용 대사가 전사 어디에도 없습니다(환각) — "
                                         f"\"{quote_text[:24]}…\""})
                continue

        # 렌더 가능 하한 — 클램프·재배치를 안 거친 조각에도 건다. 0.5초짜리 조각은
        # 모델이 그렇게 냈든 우리가 잘랐든 화면에서 보이지 않는다.
        if end - start < MIN_SEGMENT_SEC:
            notes.append({"segment": idx, "action": "dropped",
                          "why": f"조각 길이 {end - start:.2f}초 < {MIN_SEGMENT_SEC}초"})
            continue

        # 여기까지 아무 노트도 없는 조각은 조용히 통과한 것이다 — 그래도 한 줄 남긴다.
        # "전부 기록한다"(기획서 §6c)가 이 단계의 계약이고, 노트가 비면 검증이 돌았는지
        # 조각이 판정 대상이 아니었는지 나중에 구분할 수 없다(E18-6 감사 기록의 교훈).
        if not any(n["segment"] == idx for n in notes):
            notes.append({"segment": idx, "action": "ok", "why": "판정 통과"})

        # ④ 경계 눈금 — 경고만. 드롭하지 않는 이유: 눈금은 스냅의 재료이지 진위의 근거가
        #    아니다(재배치된 조각은 전사 시각을 그대로 쓰므로 눈금과 어긋나는 게 정상이다).
        if grid:
            for label, t in (("start", start), ("end", end)):
                gap = _snap_gap(grid, t)
                if gap > SNAP_TOLERANCE_SEC:
                    notes.append({"segment": idx, "action": "unsnapped",
                                  "why": f"{label} {t:.2f} 에서 가장 가까운 눈금이 "
                                         f"{gap:.2f}초 떨어져 있습니다 "
                                         f"(±{SNAP_TOLERANCE_SEC:g}s 밖) — 경고",
                                  "boundary": label, "gap_sec": round(gap, 3)})

        seg2 = dict(seg)
        seg2["start_sec"], seg2["end_sec"] = start, end
        kept.append(seg2)
        finals.append((idx, (start, end)))

    total_sec = round(sum(e - s for _i, (s, e) in finals), 6)

    if not kept:
        verdict = "dropped"
        notes.append({"segment": CANDIDATE_NOTE_INDEX, "action": "dropped",
                      "why": f"남은 조각이 없습니다(원래 {len(raw_segments)}개)"})
    elif total_sec < MIN_CANDIDATE_SEC:
        verdict = "dropped"
        notes.append({"segment": CANDIDATE_NOTE_INDEX, "action": "dropped",
                      "why": f"남은 길이 {total_sec:.1f}초 < {MIN_CANDIDATE_SEC:g}초 "
                             f"(조각 {len(kept)}/{len(raw_segments)}개)"})
    else:
        verdict = "relocated" if relocated_any else "ok"

    return {"id": cand_id, "verdict": verdict, "segments": kept,
            "total_sec": total_sec, "notes": notes}


def verify_candidates(cands: list[dict], *, segments: list[dict], source_duration_sec: float,
                      grid_times: list[float] | None = None) -> tuple[list[dict], dict]:
    """전량 판정 → (살아남은 후보 목록, 기록). 순수·결정적.

    살아남은 후보는 **사본**이고 `segments` 만 판정 결과로 갈아 끼운다 — 재배치·클램프가
    여기서 반영되지 않으면 6c 가 한 일이 하류로 전달되지 않는다.

    기록 = {"results", "kept", "dropped", "relocated", "clamped"} —
    `relocated`·`clamped` 는 **조각 수**다(후보 수가 아니다).

    ⚠ 전량 드롭이면 kept=[] 를 그대로 돌려준다 — 재질의 판단은 부르는 쪽(6단계)의 일이다.
      여기서 '최소 하나는 살린다'를 하면 환각 후보가 그 자리로 올라온다."""
    results: list[dict] = []
    kept: list[dict] = []
    record: dict = {"results": results, "kept": [], "dropped": [],
                    "relocated": 0, "clamped": 0}

    seen: set[str] = set()
    for cand in cands or []:
        res = verify_candidate(cand, segments=segments,
                               source_duration_sec=source_duration_sec,
                               grid_times=grid_times)
        cid = res["id"]
        if cid in seen:
            # id 는 §8 의 좌표다 — 중복이면 funnel·flags·approval 이 누구를 가리키는지 모른다.
            raise ValueError(f"후보 id 가 중복입니다: {cid}")
        seen.add(cid)

        results.append(res)
        record["relocated"] += sum(1 for n in res["notes"] if n["action"] == "relocated")
        record["clamped"] += sum(1 for n in res["notes"] if n["action"] == "clamped")

        if res["verdict"] == "dropped":
            why = next((n["why"] for n in res["notes"]
                        if n["segment"] == CANDIDATE_NOTE_INDEX), "드롭")
            record["dropped"].append({"id": cid, "why": why})
            continue

        cand2 = dict(cand)
        cand2["segments"] = res["segments"]
        kept.append(cand2)
        record["kept"].append(cid)

    return kept, record
