"""다리 — 승인 후보의 **절대초**를 v3 조립 기계의 **span** 어휘로 옮긴다.

계약 정본 `docs/v4/M5-interfaces.md` §1(+ §0 의 열쇠 조사표). 기획 `docs/v4/v4-plan.md`.

    승인 후보 {id, template, segments[{start_sec, end_sec, quote}], …}
        │  snap_segments   경계를 격자 눈금으로 정착(비대칭)
        │  spans_for       조각 → span_ids (중점 규칙)
        │  build_span_index 격자(+10a) → span_index
        ▼
    v3 story/assemble 기계 (`plan_narration_slots` · `trim_to_budget` · `word_subtitles` …)

🛑 **v4 에서 가장 위험한 조각이다.** 여기가 틀리면 승인된 후보가 엉뚱한 구간으로
조립된다 — 이 파이프라인이 막으려는 사고 그 자체다. 그래서 규율이 셋이다:

1. **수식을 여기서 발명하지 않는다.** 눈금은 `timegrid.grid_snap_times`, span 소속은
   `overrides.spans_in_window`, 단어 확신도는 `candidates._mean_prob`, 화자색은
   `assemble.speaker_colors` 를 **부른다**. 베끼면 한쪽만 고쳐지는 날이 온다.
2. **억지로 당기지 않는다.** 관용 밖 경계는 원값을 그대로 두고 기록한다(모델이 준
   경계가 격자와 2초 넘게 어긋난다는 사실 자체가 검수 재료다).
3. **조용히 버리지 않는다.** span 이 안 잡힌 조각·못 찾은 인용·화자 부재는 전부 기록으로
   나온다. 부르는 쪽(10단계)이 그 기록을 stdout·run_log 에 싣는다.

## 이 판의 두 구멍 — 원천이 v3 와 다르다 (계약 §0)

· **importance** — v3 는 모델의 청크 상세 분석에서 받았다. v4 에는 그 단계가 없다.
  기본 `DEFAULT_IMPORTANCE`(3)에 **인용된 대사가 든 span 만 `QUOTE_IMPORTANCE`(5)**.
  이유는 하나다: 그 대사가 그 후보가 존재하는 이유인데, `story.plan_narration_slots`
  의 ⓑ 규칙이 `importance <= MUTE_MAX_IMPORTANCE`(3) 유성 span 을 내레이션 밑에서
  **뮤트**한다 — 기본값 3 그대로 두면 후보의 근거인 대사가 음소거될 수 있다.
  ⇒ 그래서 `QUOTE_IMPORTANCE > MUTE_MAX_IMPORTANCE` 는 값의 우연이 아니라 계약이다
  (테스트가 두 상수의 관계를 고정한다).
· **화자(audio_script)** — whisper 전사에는 화자가 없고 v4 는 청크 분석을 없앴다.
  10a(정밀 청취)가 **화자의 유일한 원천**이다. 꺼져 있으면 `assemble.speaker_colors`
  가 `{}` 를 내고 자막이 **전 줄 흰색**으로 나간다(가왕쇼 템플릿의 "가장 큰 특징"이
  사라진다 — M13 승계 항목). `index_audit` 이 그 사실을 `speaker_source="none"` +
  `NO_SPEAKER_WARNING` 으로 알린다. 조용히 흰 자막으로 나가면 안 된다.

모든 함수는 **순수**하다 — 넘겨받은 dict/list 를 제자리에서 고치지 않고, 같은 입력이면
같은 출력이다(정렬·동률 규칙까지 결정적).
"""
from __future__ import annotations

from app.modules import timestamp_check
from app.modules.grid.timegrid import grid_snap_times
from app.v3 import assemble
from app.v3.overrides import spans_in_window
from app.v3.story import MUTE_MAX_IMPORTANCE
# ⚠ 밑줄 이름을 건너와 부른다(같은 패키지). 베끼는 것보다 낫다 — 여기서 재는 확신도는
# 6단계 프롬프트가 `[저확신]` 을 붙일 때 쓴 **그 평균**이어야 한다. 두 벌로 두면
# 프롬프트가 저확신이라 표시한 줄이 span_index 에서는 아닌 날이 온다.
from app.v4.candidates import _mean_prob, _word_confidence_index

# ── 계약 상수 (M5 §1) ───────────────────────────────────────────────────────
DEFAULT_IMPORTANCE = 3
QUOTE_IMPORTANCE = 5

# 비대칭 스냅(기획서 §2 · rev.7 M2). **대칭 nearest 는 리액션 꼬리를 자른다** —
# E20-B1 이 `tail_hold_sec` 로 지킨 그 꼬리다(웃음·리액션은 대사 뒤에 온다).
SNAP_START_BACK_SEC = 2.0     # 시작은 앞으로 넉넉히(대사 첫 글자를 자르지 않는다)
SNAP_START_FWD_SEC = 0.5      # 뒤로는 조금만(시작을 늦추면 대사가 잘린다)
SNAP_END_FWD_SEC = 2.0        # 끝은 뒤로만 — 꼬리를 남긴다
SNAP_END_BACK_SEC = 0.0       # 끝을 앞으로 당기는 스냅은 없다(위와 같은 이유)

if QUOTE_IMPORTANCE <= MUTE_MAX_IMPORTANCE:
    # 두 상수의 관계가 인용 보호의 전부다(모듈 독스트링). 누가 한쪽을 고치면 후보의
    # 근거인 대사가 내레이션 밑에서 조용히 음소거된다 — 그 전에 크게 실패한다.
    raise ValueError(
        f"QUOTE_IMPORTANCE({QUOTE_IMPORTANCE}) 는 story.MUTE_MAX_IMPORTANCE"
        f"({MUTE_MAX_IMPORTANCE}) 보다 커야 한다 — 인용 span 이 뮤트 후보가 된다")

# importance 의 출처 — v3 는 "chunk_analysis" 였다. run_log 가 이 값을 싣는다(계약 §1).
IMPORTANCE_SOURCE = "v4_quote_heuristic"
IMPORTANCE_SOURCE_DETAIL = "v4_winner_detail"

NO_SPEAKER_WARNING = (
    "화자 원천이 없다 — 10a(--winner-detail)가 꺼져 있어 화자별 자막색이 없다"
    "(전 줄 흰색). 색을 쓰려면 10a 를 켜라.")

_EPS = 1e-9        # 부동소수 경계 비교용(값 판단이 아니라 '같은 눈금인가' 판정에만)


# ── 1) 경계 정착 ────────────────────────────────────────────────────────────

def _nearest(t: float, times: list[float]) -> float | None:
    """가장 가까운 눈금(관용 무시) — 기록에 '얼마나 떨어졌나'를 적기 위한 값."""
    if not times:
        return None
    return min(times, key=lambda g: (abs(g - t), g))


def _snap_one(t: float, times: list[float], *, back: float, fwd: float
              ) -> tuple[float, float | None]:
    """[t-back, t+fwd] 안의 눈금 중 최근접 → (시각, 오차). 없으면 (t, None) = 원값 유지.

    동률(양쪽 눈금이 정확히 같은 거리)은 **이른 쪽** — `schemas.snap_time` 과 같은
    결정성 규약이다."""
    lo, hi = t - back - _EPS, t + fwd + _EPS
    window = [g for g in times if lo <= g <= hi]
    if not window:
        return t, None
    best = min(window, key=lambda g: (abs(g - t), g))
    return best, abs(best - t)


def snap_segments(segments: list[dict], *, grid: dict,
                  source_duration_sec: float) -> tuple[list[dict], list[dict]]:
    """승인 후보의 조각 경계를 눈금으로 정착 → (스냅된 조각, 기록). 순수.

    눈금은 `timegrid.grid_snap_times` 하나다(목록을 다시 만들지 않는다 — span 경계
    ∪ 장면 전환 ∪ {0, 러닝타임}). 스냅은 **비대칭**이다: 시작은 앞 2.0s·뒤 0.5s,
    끝은 뒤 2.0s 로만. 관용 밖이면 **원값 유지 + 기록**(억지로 당기지 않는다).

    기록 항목(전부 JSON 직렬화 가능):
      {"segment", "boundary": "start"|"end", "action": "snapped"|"kept",
       "from", "to", "err"|"nearest_err", "why"}
      · action="collapsed" — 스냅이 구간을 접었다(둘 다 원값으로 되돌린다)
      · action="overlap"   — 스냅 뒤 앞 조각과 겹친다(**고치지 않고 알린다** —
        어느 쪽을 물릴지는 근거가 없다. 판정할 근거가 없으면 판정하지 않는다)

    ⚠ 조각의 시각을 못 읽거나 구간이 역전이면 **크게 실패한다** — 6c(`verify`)를 지난
    조각은 그럴 수 없으므로, 그런 값이 오면 배선 사고다(조용히 고치면 사고가 숨는다)."""
    duration = float(source_duration_sec)
    times = [t for t in grid_snap_times(grid) if -_EPS <= t <= duration + _EPS]

    out: list[dict] = []
    record: list[dict] = []
    prev_end: float | None = None
    for i, seg in enumerate(segments or []):
        if not isinstance(seg, dict):
            raise ValueError(f"조각{i}: dict 가 아니다({type(seg).__name__})")
        try:
            s_raw, e_raw = float(seg["start_sec"]), float(seg["end_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"조각{i}: 시각을 읽을 수 없다 "
                             f"(start_sec={seg.get('start_sec')!r} "
                             f"end_sec={seg.get('end_sec')!r})") from exc
        if not e_raw > s_raw:
            raise ValueError(f"조각{i}: 구간 역전 {s_raw}~{e_raw}")

        s, err_s = _snap_one(s_raw, times, back=SNAP_START_BACK_SEC, fwd=SNAP_START_FWD_SEC)
        e, err_e = _snap_one(e_raw, times, back=SNAP_END_BACK_SEC, fwd=SNAP_END_FWD_SEC)

        collapsed = not e > s
        if collapsed:
            # 스냅이 구간을 접었다(격자가 성긴 극단) — 원값이 낫다. 둘 다 되돌린다.
            record.append({"segment": i, "boundary": "both", "action": "collapsed",
                           "from": [round(s_raw, 3), round(e_raw, 3)],
                           "to": [round(s_raw, 3), round(e_raw, 3)],
                           "why": f"스냅이 구간을 접었다({s:.3f} ≥ {e:.3f}) — 원값 유지"})
            s, e = s_raw, e_raw

        for label, raw, snapped, err, back, fwd in (() if collapsed else (
                ("start", s_raw, s, err_s, SNAP_START_BACK_SEC, SNAP_START_FWD_SEC),
                ("end", e_raw, e, err_e, SNAP_END_BACK_SEC, SNAP_END_FWD_SEC))):
            if err is None:
                near = _nearest(raw, times)
                record.append({
                    "segment": i, "boundary": label, "action": "kept",
                    "from": round(raw, 3), "to": round(raw, 3),
                    "nearest_err": None if near is None else round(abs(near - raw), 3),
                    "why": f"관용(앞 {back:g}s · 뒤 {fwd:g}s) 안에 눈금이 없다 — 원값 유지"})
            elif err > _EPS:
                record.append({"segment": i, "boundary": label, "action": "snapped",
                               "from": round(raw, 3), "to": round(snapped, 3),
                               "err": round(err, 3)})
            # err == 0 : 이미 눈금 위 — 적을 것이 없다(기록을 잡음으로 채우지 않는다)

        s, e = round(s, 3), round(e, 3)
        if prev_end is not None and s < prev_end - _EPS:
            record.append({"segment": i, "boundary": "start", "action": "overlap",
                           "from": s, "to": s, "prev_end": prev_end,
                           "why": "스냅 뒤 앞 조각과 겹친다 — 고치지 않고 알린다"})
        prev_end = e
        out.append({**seg, "start_sec": s, "end_sec": e})
    return out, record


# ── 2) 조각 → span ─────────────────────────────────────────────────────────

def spans_for(segments: list[dict], grid: dict) -> tuple[list[list[str]], list[dict]]:
    """조각 → span_ids. 순수.

    소속 규칙은 `app/v3/overrides.spans_in_window` 를 **그대로 부른다** — 중점이 창에
    드는 span(반개구간 [t0, t1)). chunk 소속·편집실 스냅과 같은 자다(수식 복제 금지).

    ⚠ span 이 하나도 안 잡히는 조각은 **버리지 않고 기록**한다 — 그 조각이 격자 눈금
    보다 짧다는 뜻이고, 빈 클립을 만들지 않도록 **호출자가 판단한다**(빈 조각을 그대로
    `to_beats` 에 넘기면 크게 실패한다). 기록: {"segment", "window", "why"}."""
    ids: list[list[str]] = []
    record: list[dict] = []
    for i, seg in enumerate(segments or []):
        t0, t1 = float(seg["start_sec"]), float(seg["end_sec"])
        got = spans_in_window(grid, t0, t1)
        ids.append(got)
        if not got:
            record.append({"segment": i, "window": [round(t0, 3), round(t1, 3)],
                           "why": "중점이 이 창에 드는 span 이 없다 — 조각이 격자보다 짧다"})
    return ids, record


def quoted_span_ids(segments: list[dict], span_ids: list[list[str]], grid: dict
                    ) -> tuple[set[str], list[dict]]:
    """조각의 `quote` 가 실린 span 들 → (span id 집합, 기록). 순수.

    이 집합이 `build_span_index` 의 `quoted_spans` 다 — 그 span 이 `QUOTE_IMPORTANCE`
    로 올라가 내레이션 뮤트에서 보호된다(모듈 독스트링).

    대조는 `timestamp_check.normalize`(구두점·공백 제거) 위에서 한다. 인용은 전사
    한 줄을 그대로 옮긴 것이고 전사 한 줄이 곧 유성 span 이므로(6단계 `transcript_block`
    이 span 텍스트로 만든 표를 보여 준다), **포함 관계 어느 쪽이든** 같은 대사로 본다.
    · 짧은 인용(`MIN_QUOTE_CHARS` 미만)은 우연 일치가 잦아 **판정하지 않는다** —
      6c 가 같은 이유로 대조를 건너뛰는 그 임계다.
    · 조각 안에서 못 찾으면 그 조각만 기록하고 넘어간다(전량 보호 실패는 호출자가 본다)."""
    if len(span_ids) != len(segments or []):
        raise ValueError(f"조각 수({len(segments or [])})와 span 목록 수({len(span_ids)})가 다르다")
    text_of = {str(sp["id"]): timestamp_check.normalize(sp.get("text"))
               for sp in grid.get("span_candidates") or []}

    quoted: set[str] = set()
    record: list[dict] = []
    for i, seg in enumerate(segments or []):
        quote = seg.get("quote")
        if not quote:
            continue                       # 무성 조각의 quote=null 은 정상이다
        q = timestamp_check.normalize(quote)
        if len(q) < timestamp_check.MIN_QUOTE_CHARS:
            record.append({"segment": i, "quote": str(quote), "action": "skipped",
                           "why": f"인용이 짧아(정규화 {len(q)}자 < "
                                  f"{timestamp_check.MIN_QUOTE_CHARS}) 판정하지 않았다"})
            continue
        hits = [sid for sid in span_ids[i]
                if text_of.get(sid) and (text_of[sid] in q or q in text_of[sid])]
        if hits:
            quoted.update(hits)
        else:
            record.append({"segment": i, "quote": str(quote), "action": "not_found",
                           "why": "그 조각의 span 텍스트 어디에도 없다 — "
                                  "인용 보호(importance 5)를 걸지 못했다"})
    return quoted, record


# ── 3) span_index ──────────────────────────────────────────────────────────

def _detail_for(detail: dict | None, sid: str) -> dict:
    node = (detail or {}).get(sid)
    return node if isinstance(node, dict) else {}


def build_span_index(grid: dict, *, quoted_spans: set[str] | None = None,
                     detail: dict | None = None) -> tuple[dict[str, dict], list[str]]:
    """격자(+선택적으로 10a 산출) → `span_index` + grid 순서 목록. 순수.

    계약 §0 표의 열쇠를 전부 채운다 — `t_in`·`t_out`·`is_audio`·`pos` 는 격자 그대로,
    `text_source`="transcript"·`heard_text`=""·`conf`=단어 확률 평균,
    `importance`·`audio_script`·`meaning_content`·`mood`·`scene_script` 는 10a 가 있으면
    그것, 없으면 기본값. 순서는 v3 와 같은 grid t_in 순(`pos` 가 그 자리다 —
    `story.validate_story_response` 의 '연속 범위' 검사가 이 값을 본다).

    additive 두 키: `text`(격자 대사 — 10단계 프롬프트 재료) ·
    `importance_source`("default"|"quote"|"detail" — 왜 그 값인지 되짚을 근거).

    · `conf` 는 **유성 span 만** 잰다(v3 `assemble_chunk_meanings` 와 같은 규약).
      단어가 없으면 None — 모르는 것을 0 으로 채우지 않는다.
    · `importance` 는 인용 보호를 **바닥**으로 쓴다: 10a 가 더 높게 보면 그 값을 쓰되
      인용 span 이 `QUOTE_IMPORTANCE` 아래로 내려가지는 않는다(내려가면 그 대사가
      내레이션 밑에서 뮤트될 수 있다 — 보호의 이유가 사라진다).
    · 🛑 `detail` 에 격자에 없는 span id 가 있으면 **크게 실패한다** — 10a 산출이
      다른 격자의 것이라는 뜻이고(캐시 stale), 조용히 버리면 그 구간만 화자·대사가
      빠진 채 발행된다(E11·E13 사이드카 무효화와 같은 규율)."""
    spans = sorted((grid.get("span_candidates") or []),
                   key=lambda s: (float(s["t_in"]), str(s["id"])))
    quoted = set(quoted_spans or ())
    mids, probs = _word_confidence_index(grid.get("words"))

    unknown = sorted(set(detail or ()) - {str(sp["id"]) for sp in spans})
    if unknown:
        raise ValueError(
            f"10a 산출에 격자에 없는 span id 가 있다: {unknown[:5]} "
            f"(총 {len(unknown)}개) — 다른 격자의 산출이다(캐시 무효화 필요)")

    index: dict[str, dict] = {}
    order: list[str] = []
    for pos, sp in enumerate(spans):
        sid = str(sp["id"])
        t_in, t_out = float(sp["t_in"]), float(sp["t_out"])
        is_audio = bool(sp.get("is_audio"))
        d = _detail_for(detail, sid)

        d_imp = d.get("importance")
        if d_imp is not None:
            if not isinstance(d_imp, int) or isinstance(d_imp, bool) \
                    or not 1 <= d_imp <= 5:
                # 10a 는 `chunk_analyze` 검증을 지난 산출이다 — 범위 밖 값이 오면 배선
                # 사고이지 '모르는 값'이 아니다. 조용히 무시하면 그 span 만 다른 규칙으로
                # 편집된다(뮤트 여부가 갈린다).
                raise ValueError(f"10a importance 가 1~5 정수가 아니다: {sid} → {d_imp!r}")
        if sid in quoted:
            # 인용 보호는 **바닥**이다 — 10a 가 더 높게 보면 그 값을 쓴다.
            imp = max(QUOTE_IMPORTANCE, d_imp or QUOTE_IMPORTANCE)
            imp_src = "detail" if (d_imp or 0) > QUOTE_IMPORTANCE else "quote"
        elif d_imp is not None:
            imp, imp_src = d_imp, "detail"
        else:
            imp, imp_src = DEFAULT_IMPORTANCE, "default"

        index[sid] = {
            "t_in": t_in, "t_out": t_out, "is_audio": is_audio,
            "importance": imp,
            "importance_source": imp_src,
            # 화자는 10a 만이 원천이다(모듈 독스트링) — 없으면 빈 목록 = 전 줄 흰색.
            # 대사 계열 세 키는 **유성 span 만** 채운다(v3 `assemble_chunk_meanings` 와
            # 같은 규약 — 무성 span 의 text_source 가 서면 `word_subtitles` 가 없는
            # 대사를 그리려 든다).
            "audio_script": list(d.get("audio_script") or []) if is_audio else [],
            "text_source": ((d.get("text_source") or "transcript") if is_audio else None),
            "heard_text": (str(d.get("heard_text") or "") if is_audio else ""),
            "conf": (_mean_prob(mids, probs, t_in, t_out) if is_audio else None),
            "scene_script": str(d.get("scene_script") or ""),
            "meaning_content": str(d.get("meaning_content") or ""),
            "mood": str(d.get("mood") or ""),
            "pos": pos,
            "text": str(sp.get("text") or ""),
        }
        order.append(sid)
    return index, order


def index_audit(span_index: dict[str, dict], *, detail_spans: int = 0) -> dict:
    """`span_index` → run_log 에 실을 감사 기록. 순수·JSON 직렬화 가능.

    🛑 이 함수가 §0 의 발견을 소리 내어 말한다: 화자가 없으면 자막이 전 줄 흰색이다.
    화자 판정은 렌더가 쓰는 **바로 그 함수**(`assemble.speaker_colors`)로 낸다 —
    여기서 따로 세면 '색이 있다고 적혀 있는데 화면은 흰색'이 된다."""
    colors = assemble.speaker_colors(span_index)
    voiced = [sp for sp in span_index.values() if sp["is_audio"]]
    audit = {
        "spans": len(span_index),
        "voiced_spans": len(voiced),
        "quoted_spans": sum(1 for sp in span_index.values()
                            if sp.get("importance_source") == "quote"),
        "detail_spans": int(detail_spans),
        "importance_source": (IMPORTANCE_SOURCE_DETAIL if detail_spans
                              else IMPORTANCE_SOURCE),
        "conf_missing": sum(1 for sp in voiced if sp.get("conf") is None),
        "speakers": sorted(colors),
        "speaker_source": "detail" if colors else "none",
    }
    if not colors:
        audit["warning"] = NO_SPEAKER_WARNING
    return audit


# ── 4) 비트 뼈대 ───────────────────────────────────────────────────────────

def to_beats(candidate: dict, *, span_ids: list[list[str]],
             roles: list[str] | None = None) -> list[dict]:
    """승인 후보 → v3 `story_doc["beats"]` 뼈대. 순수.

    조각 하나 = 비트 하나. `narration`·`labels` 는 **비어 있다** — 10단계(살붙이기)가
    채운다. `role` 은 인자 > 조각의 `role` > `"build"` 순.

    ⚠ 빈 span 목록은 **크게 실패한다**. `spans_for` 가 그 조각을 이미 기록해 놨으므로,
    호출자가 그 기록을 보고 걸렀어야 한다. 여기서 조용히 건너뛰면 비트 번호가 밀려
    내레이션·라벨이 다른 구간에 붙는다.

    ⚠ **`trim_to_budget` 은 `role == "climax"` 를 하드코딩으로 보호한다.** v4 템플릿 중
    그 역할을 쓰는 것은 `recap_dialogue` 뿐이고 `conflict_payoff`(turn/payoff)·
    `chemi_observe`(ensemble)는 **보호를 못 받는다** — 예산 초과 시 절정 비트의
    가장자리 span 이 깎일 수 있다. 이 판은 역할 이름을 바꾸지 않고 사실만 남긴다
    (`docs/v4/UNVERIFIED.md` 재료 · M5 §2 가 같은 경고를 적어 뒀다)."""
    segments = candidate.get("segments") or []
    if len(span_ids) != len(segments):
        raise ValueError(f"조각 수({len(segments)})와 span 목록 수({len(span_ids)})가 다르다")
    if roles is not None and len(roles) != len(segments):
        raise ValueError(f"조각 수({len(segments)})와 role 수({len(roles)})가 다르다")

    beats: list[dict] = []
    for i, seg in enumerate(segments):
        ids = span_ids[i]
        if not ids:
            raise ValueError(
                f"후보 {candidate.get('id')!r} 조각{i}: span 이 없다 — "
                "`spans_for` 기록을 보고 호출자가 먼저 걸러라(빈 비트 금지)")
        role = (roles[i] if roles is not None else seg.get("role")) or "build"
        beats.append({"role": str(role).strip() or "build", "span_ids": list(ids),
                      "narration": [], "labels": []})
    return beats


# ── 다리 건너기(편의) ───────────────────────────────────────────────────────

def cross(candidate: dict, *, grid: dict, source_duration_sec: float,
          detail: dict | None = None, roles: list[str] | None = None) -> dict:
    """스냅 → span → 인용 보호 → span_index → 비트 를 **정해진 순서로** 한 번에. 순수.

    계약 §4 는 "다리는 10단계 입구 한 곳"이라고 못박는다. 순서가 곧 계약이라(인용 보호는
    **스냅된** 조각의 span 위에서 걸린다) 그 순서를 함수 하나에 담아 둔다 — 부르는 쪽이
    순서를 다시 적으면 언젠가 어긋난다.

    반환 `{"segments", "span_ids", "span_index", "span_order", "beats", "audit"}`.
    `audit` 은 그대로 run_log 에 싣는다: `{"snap", "spans_missing", "quotes",
    "index": index_audit(...)}`. **빈 조각이 있으면 `beats` 는 None** 이고 사유가
    `audit["spans_missing"]` 에 있다 — 호출자가 그 후보를 어떻게 할지 정한다."""
    segments, snap_rec = snap_segments(candidate.get("segments") or [], grid=grid,
                                       source_duration_sec=source_duration_sec)
    span_ids, missing = spans_for(segments, grid)
    quoted, quote_rec = quoted_span_ids(segments, span_ids, grid)
    span_index, span_order = build_span_index(grid, quoted_spans=quoted, detail=detail)
    beats = None if missing else to_beats({**candidate, "segments": segments},
                                          span_ids=span_ids, roles=roles)
    return {"segments": segments, "span_ids": span_ids,
            "span_index": span_index, "span_order": span_order, "beats": beats,
            "audit": {"snap": snap_rec, "spans_missing": missing, "quotes": quote_rec,
                      "index": index_audit(span_index,
                                           detail_spans=len(detail or {}))}}
