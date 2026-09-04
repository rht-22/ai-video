"""7단계 — 결정적 깔때기. LLM 호출 0 · 전부 순수 함수.

기획서 §3-7 · 인터페이스 계약 `docs/v4/M1-interfaces.md` §5.

후보 결함 판정 다섯 중 **셋은 화면을 안 봐도 결정적으로 계산된다**(소스 밖 · 예고·크레딧
구역 포함 · 발화 없음). 그래서 8단계 LLM 이 볼 자리가 이음새·훅 둘로 좁혀진다. 이 모듈이
그 셋이고, 남은 후보를 ≤FUNNEL_KEEP 으로 줄여 8단계로 넘긴다.

세 층을 **절대 섞지 않는다**(기획서 §1 원칙 1·3):

  ① `hard_problems` — **사실만**. 예고·크레딧 겹침 · 소스 밖 · 발화 커버리지 미달 ·
     길이 위반. 점수가 아니라 사유 목록이고, 하나라도 있으면 탈락이다.
  ② `soft_signals` — **값만**. 판정하지 않는다. 근거가 없으면 `None` 을 낸다
     (모르는 것을 틀렸다고 하지 않는다 — timestamp_check·E20-B4 규율).
  ③ `score` — 값 → 감점 환산. **점수 임계로 탈락시키는 코드는 이 파일에 없다**
     (M9 원칙: 검증자와 피검증자가 편향을 공유하면 안 된다). 점수는 순위에만 쓴다.

동점은 **소스 시각순**으로 깬다(첫 조각 start_sec → id). 6단계가 나열한 순서로 깨면
문서화 안 된 위치 편향이 M9 뒷문으로 샌다(기획서 §5 M3).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.modules.clip_guard import CLIP_LOST_TOLERANCE_SEC, clips_beyond_source
from app.modules.grid.schemas import EXCEPTION_KEYS
from app.modules.speech import plausible_speech_intervals, speech_coverage_ratio

FUNNEL_KEEP = 8               # 8단계로 넘길 상한
IOU_DEDUP = 0.5               # 다중 구간 **합집합** IoU — v3 의 0.7 을 조인다(기획서 §3)
MIN_SPEECH_COVERAGE = 0.55    # E20-B4 스토리라인 커버리지 가드와 같은 자
STALL_MAX_GAP_SEC = 12.0      # 장면 전환 간 최대 간격(소프트 신호)

# 🛑 예외 구역 **어휘의 정본은 격자다**(`grid.schemas.EXCEPTION_KEYS` 5종). 여기서
# 목록을 다시 적었더니 `recap`·`end` 두 종이 빠졌고, 그중 `end` 는 이름 그대로 **꼬리**라
# 하드 게이트에서 조용히 사라졌다 — 예고 오염을 막으려고 만든 게이트가 정작 꼬리 하나를
# 못 보는 것이다(2026-09-03 적대 검증이 잡았다. 가왕쇼 6화가 그 사고다).
# 이제 정본을 import 하고 머리/꼬리로만 나눈다. 둘의 합집합이 정본과 다르면 임포트
# 시점에 죽는다 — 격자에 6번째 종이 생기는 날 여기가 조용히 무시하면 안 된다.
KNOWN_SECTORS = EXCEPTION_KEYS

# 꼬리는 하드다 — 한 프레임도 나가면 안 된다(기획서 §3-7).
TAIL_SECTORS = ("teaser", "credit", "end")
# 머리는 기록만 — 감점 축이 계약에 없다(`run_funnel` 의 `intro_overlap`).
# `recap` 은 지난 회차 줄거리라 intro 와 같은 자리에서 사람이 판단할 몫이다.
HEAD_SECTORS = ("intro", "recap")
if set(TAIL_SECTORS) | set(HEAD_SECTORS) != set(KNOWN_SECTORS):
    raise AssertionError(
        f"예외 구역 어휘가 격자 정본과 갈렸다 — 정본 {sorted(KNOWN_SECTORS)} vs "
        f"여기 {sorted(set(TAIL_SECTORS) | set(HEAD_SECTORS))}. "
        "새 종이 생겼으면 하드(꼬리)인지 기록(머리)인지 여기서 정해야 한다.")

# 겹침 관용은 **부동소수 오차만** 흡수한다. 예고편은 한 프레임도 나가면 안 된다
# (2026-08-24 가왕쇼 사고 — 예고 50초가 쇼츠 엔딩을 오염시켰다). 경계 자체의 오차는
# 6b(경계 정밀)가 앞에서 잡는 몫이지 여기서 관용할 것이 아니다.
SECTOR_OVERLAP_TOLERANCE_SEC = 0.05

# 소스 끝 경계가 소수점에서 어긋나는 정상 케이스(실측 0~0.2s)는 통과시키고 통째로
# 밖에 나간 것만 잡는다. **값을 다시 적지 않고 v1 판정의 것을 그대로 쓴다** —
# 이 레포는 베낀 수식으로 여러 번 다쳤고(E17-2 밴드 기하 · L-P1 환각 클램프),
# 두 곳에 적힌 관용치는 언젠가 한쪽만 고쳐진다. `app/modules/clip_guard.py` 는
# V4-M1 §7 이 모놀리스에서 추출한 것이라 `app.pipeline` 을 끌어오지 않는다.
SOURCE_BOUNDS_TOLERANCE_SEC = CLIP_LOST_TOLERANCE_SEC

LENGTH_TOLERANCE_SEC = 0.05   # 길이 비교의 부동소수 관용

# ── 감점 환산 상수 ─────────────────────────────────────────────────────────
# 전부 '이 값을 넘으면 그 축은 만점 감점'인 포화점이다. 값의 유래를 각 줄에 남긴다.
COHESION_GAP_FREE_SEC = 5.0     # v3 score_story 의 아크 점프 기준(>5.0s 를 점프로 센다)
COHESION_GAP_SAT_SEC = 600.0    # 10분 이상 떨어진 조각 = 원거리 짜집기(최대 감점)
SILENT_RUN_SAT_SEC = 12.0       # STALL_MAX_GAP_SEC 과 같은 자 — '12초 동안 아무 일 없음'
LEAD_IN_FREE_SEC = 2.0          # 훅은 첫 2초 안에 사건(기획서 §3-8 hook_weak 기준)
LEAD_IN_SAT_SEC = 8.0           # E20-B3 hook_max_sec 8 — 훅 전체가 도입이면 최대 감점
SEGMENT_COUNT_FREE = 3          # hook·build·payoff 3조각까지는 무감점
SEGMENT_COUNT_SAT = 8           # v3 PIECES_MAX 8 — 그 이상은 짜집기

# 루브릭 가중치 — v3 `story.RUBRIC_WEIGHTS` 의 정신을 그대로 옮겼다(항목명은 7단계가
# 실제로 가진 재료로 갈아탄다. narration·budget 은 이 단계에 재료가 없다 — 내레이션은
# 10단계, 예산 적합은 길이 하드 게이트가 이미 본다).
#   material 3.0  → speech_coverage      (재료 신뢰도 = 전사가 실재하는가)
#   cohesion 1.5  → cohesion_gap_sec     (원거리 짜집기 억제 — 사용자 지적)
#   progression 1.0 → stall_max_gap_sec  (진행감 = 화면이 멈춰 있지 않은가)
#   intro 1.0     → lead_in_sec          (서론 금지)
# 신설 둘:
#   cut_mid_sentence 1.5 — 문장 중간 절단(기획서 §3-7 소프트 감점 명시 항목)
#   silent_max_run_sec 1.0 — **정지 화면과 분리된 축**(아래 soft_signals 주석 참조)
DEFAULT_WEIGHTS: dict[str, float] = {
    "speech_coverage": 3.0,
    "cohesion_gap_sec": 1.5,
    "cut_mid_sentence": 1.5,
    "stall_max_gap_sec": 1.0,
    "silent_max_run_sec": 1.0,
    "lead_in_sec": 1.0,
    "segment_count": 1.0,
}

# 탈락·드롭 사유 코드. 기록 문자열은 "<code>: <detail>" 이고 detail 은 사람이 읽는 몫이다
# (조용한 드롭 금지 — 몇 건을 왜 버렸는지 전량 남긴다).
CODE_NO_SEGMENTS = "no_segments"
CODE_INVALID_SEGMENT = "invalid_segment"
CODE_SECTOR_OVERLAP = "sector_overlap"
CODE_BEYOND_SOURCE = "beyond_source"
CODE_LOW_SPEECH_COVERAGE = "low_speech_coverage"
CODE_TOO_SHORT = "too_short"
CODE_TOO_LONG = "too_long"
CODE_DUPLICATE = "duplicate"
CODE_KEEP_CAP = "keep_cap"

# 전량 하드 탈락 시 **되살릴 수 있는** 사유 — '품질'로 떨어진 것만이다.
# ⚠ 여기 없는 사유(예고·크레딧 겹침 · 소스 밖 · 조각 없음)는 부활 불가다. 그건 품질이
# 아니라 **오염이거나 물리적으로 불가능**한 것이고(가왕쇼 6화 예고 오염 · 소스 밖 클립이
# 만든 반쪽 쇼츠 5건), 조용한 결번보다 나쁘다.
RESURRECTABLE_CODES = frozenset({CODE_LOW_SPEECH_COVERAGE, CODE_TOO_SHORT, CODE_TOO_LONG})

HARD_CODES = frozenset({CODE_NO_SEGMENTS, CODE_INVALID_SEGMENT, CODE_SECTOR_OVERLAP,
                        CODE_BEYOND_SOURCE, CODE_LOW_SPEECH_COVERAGE,
                        CODE_TOO_SHORT, CODE_TOO_LONG})


def problem_code(reason: str) -> str:
    """사유 문자열에서 코드만 떼어낸다("beyond_source: 조각 2 …" → "beyond_source")."""
    return str(reason).split(":", 1)[0].strip()


# ── 입력 정규화 ────────────────────────────────────────────────────────────
# 후보 조각·발화 구간·단어는 부르는 쪽에 따라 dict 로도 객체로도 온다. 여기서 한 번
# 정규화해 아래 계산은 **한 가지 모양만** 본다(모양 분기가 흩어지면 언젠가 한쪽만 고쳐진다).

def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _field(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, dict):
            if name in item:
                return item[name]
        elif hasattr(item, name):
            return getattr(item, name)
    return default


def _raw_segments_of(cand: Any) -> list[SimpleNamespace]:
    """후보 → **편집 순서 그대로**의 조각 목록(사본). 정렬하지 않는다.

    조각 배열의 순서가 곧 붙는 순서다(8단계 `flags.candidate_clips` 가 같은 규약을
    명시한다). 소스 시간과 다를 수 있으므로 — 결말을 앞으로 빼는 편성 — '첫 화면'을
    묻는 신호는 반드시 이쪽을 봐야 한다."""
    raw = _field(cand, "segments", default=None) or []
    return [SimpleNamespace(start_sec=_num(_field(s, "start_sec", "start")),
                            end_sec=_num(_field(s, "end_sec", "end")),
                            quote=_field(s, "quote", "text"))
            for s in raw]


def _segments_of(cand: Any) -> list[SimpleNamespace]:
    """후보 → **시각순** 조각 목록(사본). 넘겨받은 dict 는 건드리지 않는다.

    ⚠ 정렬본이다. 소스 분포를 재는 신호(커버리지·정적·응집도·문장 절단)는 순서와
    무관하므로 이쪽이 맞지만, **'첫 조각'을 묻는 신호는 `_raw_segments_of`** 를
    써야 한다(2026-09-04: `lead_in_sec` 이 여기를 보고 있어서, 비선형 편성에서
    훅이 아니라 소스상 가장 이른 조각의 서론을 재고 있었다)."""
    return sorted(_raw_segments_of(cand), key=lambda s: (s.start_sec, s.end_sec))


def _normalize_intervals(speech_intervals: Any) -> list[SimpleNamespace] | None:
    """발화 구간 정규화. **비어 있으면 None** — 근거가 없다는 뜻이다.

    ⚠ 계약의 형은 `list[tuple[float, float]]` 지만 튜플에는 텍스트가 없어
    `plausible_speech_intervals`(길이·글자밀도 판정)를 태울 수 없다. 그래서 dict·객체
    (전사 세그먼트 그대로)도 받고, **텍스트를 든 것만** 그 필터를 지난다. 텍스트가 없는
    항목은 밀도를 알 수 없으므로 거르지 않는다(오판 금지).
    """
    if not speech_intervals:
        return None
    out = []
    for it in speech_intervals:
        if isinstance(it, (tuple, list)):
            if len(it) < 2:
                continue
            out.append(SimpleNamespace(start_sec=_num(it[0]), end_sec=_num(it[1]),
                                       text=None))
            continue
        out.append(SimpleNamespace(start_sec=_num(_field(it, "start_sec", "start")),
                                   end_sec=_num(_field(it, "end_sec", "end")),
                                   text=_field(it, "text")))
    return out or None


def plausible_intervals(speech_intervals: Any) -> tuple[list[SimpleNamespace] | None, int]:
    """환각성 전사를 뺀 발화 구간 → (구간 목록|None, 버린 건수).

    E20-B4 그대로 `app.modules.speech.plausible_speech_intervals` 를 **부른다**
    (베끼면 언젠가 한쪽만 고쳐진다). 40.5초짜리 "누가 보냈어?" 한 줄이 hook 전체를
    '발화 있음'으로 위장한 실측이 이 필터의 유래다.
    """
    norm = _normalize_intervals(speech_intervals)
    if norm is None:
        return None, 0
    typed = [it for it in norm if isinstance(it.text, str)]
    untyped = [it for it in norm if not isinstance(it.text, str)]
    kept = plausible_speech_intervals(typed)
    dropped = len(typed) - len(kept)
    merged = sorted(kept + untyped, key=lambda s: (s.start_sec, s.end_sec))
    return (merged or None), dropped


def _plausible_words(words: Any, intervals: list[SimpleNamespace] | None) -> list[SimpleNamespace] | None:
    """단어 목록 → 신뢰할 수 있는 단어만. 근거가 없으면 None.

    단어에는 밀도 판정을 걸 수 없다(한 단어는 늘 짧다). 그래서 **발화 구간 필터가
    남긴 구간 안에 있는 단어만** 신뢰한다 — 환각 세그먼트에서 온 단어는 함께 빠진다.
    구간 자체를 모르면(intervals None) 거르지 않는다(오판 금지).
    """
    if not words:
        return None
    norm = [SimpleNamespace(start_sec=_num(_field(w, "start_sec", "start")),
                            end_sec=_num(_field(w, "end_sec", "end")))
            for w in words]
    if intervals is None:
        return sorted(norm, key=lambda w: (w.start_sec, w.end_sec)) or None
    keep = [w for w in norm
            if any(min(w.end_sec, iv.end_sec) > max(w.start_sec, iv.start_sec)
                   or (iv.start_sec <= w.start_sec <= iv.end_sec)
                   for iv in intervals)]
    return sorted(keep, key=lambda w: (w.start_sec, w.end_sec)) or None


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """길이가 양수인 구간만 병합한 정렬본. 순수."""
    valid = sorted((a, b) for a, b in spans if b > a)
    merged: list[list[float]] = []
    for a, b in valid:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _total(spans: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in spans)


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


# ── 합집합 IoU ─────────────────────────────────────────────────────────────

def union_iou(a: list[dict], b: list[dict]) -> float:
    """다중 구간 두 후보의 **합집합** IoU. 순수·결정적.

    ⚠ v1 `_dedup_overlapping_candidates` 의 IoU 는 `(start_sec, end_sec)` 단일 구간이라
    v4 후보 형태(조각 여러 개)에 맞지 않는다 — 100~120 과 3000~3020 두 조각을 가진
    후보를 100~3020 한 덩어리로 읽으면 안 겹치는 후보가 겹친 것으로 보인다.
    v3 `select_diverse_storylines` 는 chunk_index·emotional_phase 축이라 청크 분할이
    없는 v4 에는 재료 자체가 없다. 그래서 새로 쓴다.

    분자는 두 합집합의 교집합, 분모는 |A ∪ B| = |A| + |B| − 교집합. 빈 쪽이 있으면 0.0.
    """
    ua = _merge([(_num(_field(s, "start_sec", "start")), _num(_field(s, "end_sec", "end")))
                 for s in a or []])
    ub = _merge([(_num(_field(s, "start_sec", "start")), _num(_field(s, "end_sec", "end")))
                 for s in b or []])
    ta, tb = _total(ua), _total(ub)
    if ta <= 0 or tb <= 0:
        return 0.0
    inter = 0.0
    for x0, x1 in ua:
        for y0, y1 in ub:
            lo, hi = max(x0, y0), min(x1, y1)
            if hi > lo:
                inter += hi - lo
    union = ta + tb - inter
    return inter / union if union > 0 else 0.0


# ── ① 하드 — 사실만 ────────────────────────────────────────────────────────

def _sector_span(sector: Any) -> tuple[float, float] | None:
    if not sector:
        return None
    lo = _num(_field(sector, "start_sec", "start"))
    hi = _num(_field(sector, "end_sec", "end"))
    return (lo, hi) if hi > lo else None


def hard_problems(cand: dict, *, exception_sectors: dict, source_duration_sec: float,
                  speech_intervals: list[tuple[float, float]],
                  min_sec: float, max_sec: float) -> list[str]:
    """탈락 사유 목록(빈 리스트 = 통과). **사실만** — 점수가 아니다.

    · 예고·크레딧 **꼬리 구역**과 겹침(intro 는 하드가 아니다 — 기획서 §3-7)
    · 소스 밖 · 발화 커버리지 < MIN_SPEECH_COVERAGE · 길이 위반

    ⚠ 모르는 구역 이름은 **즉시 실패**한다(ValueError). 조용히 무시하면 6b 가 새 이름으로
    신고한 예고 구역이 판정에서 통째로 빠져 그 후보가 그대로 나간다 — 가왕쇼 사고 그대로다.
    ⚠ 전사가 없으면(speech_intervals 가 비면) 커버리지는 **판정하지 않는다**. 소스 범위·
    길이는 전사와 무관하므로 그대로 돈다(timestamp_check 규율).
    """
    unknown = sorted(k for k in (exception_sectors or {}) if k not in KNOWN_SECTORS)
    if unknown:
        raise ValueError(
            f"모르는 예고 구역 이름: {unknown} — 허용: {list(KNOWN_SECTORS)}. "
            "조용히 무시하면 그 구역과 겹친 후보가 검사 없이 통과한다.")

    segments = _segments_of(cand)
    problems: list[str] = []

    if not segments:
        return [f"{CODE_NO_SEGMENTS}: 조각이 하나도 없다"]

    bad = [i for i, s in enumerate(segments) if s.end_sec <= s.start_sec]
    if bad:
        problems.append(f"{CODE_INVALID_SEGMENT}: 조각 {bad} 의 end <= start")

    # ① 꼬리 구역 겹침 — 겹친 초를 함께 남긴다(사람이 6b 경계를 되짚는 근거).
    for name in TAIL_SECTORS:
        span = _sector_span((exception_sectors or {}).get(name))
        if span is None:
            continue
        lo, hi = span
        over = sum(max(0.0, min(s.end_sec, hi) - max(s.start_sec, lo)) for s in segments)
        if over > SECTOR_OVERLAP_TOLERANCE_SEC:
            problems.append(f"{CODE_SECTOR_OVERLAP}: {name} 구역과 {over:.2f}s 겹침 "
                            f"({lo:.1f}~{hi:.1f})")

    # ② 소스 밖 — **판정은 v1 의 것을 그대로 부른다**(`clips_beyond_source`).
    # 렌더가 `-ss start -to end` 로 읽어 소스를 넘으면 조용히 짧아지고 시작 자체가 밖이면
    # 프레임도 오디오도 0개다. 그 판례(2026-08-24 · 185런 중 5건 · 18~26.6초 소실)를 낳은
    # 함수가 이미 있는데 여기서 다시 적으면 언젠가 한쪽만 고쳐진다.
    # 길이를 모르면(≤0) 그 함수가 빈 목록을 돌려준다 — 오판 금지 규율이 거기 들어 있다.
    lost = clips_beyond_source(segments, source_duration_sec or 0.0,
                               SOURCE_BOUNDS_TOLERANCE_SEC)
    # ⚠ 관용치보다 짧으면서 **통째로** 소스 밖인 조각은 위 함수의 `lost > tolerance` 에
    # 안 걸린다(0.5초짜리는 lost 0.5). 그건 '조금 잘림'이 아니라 '한 프레임도 없음'이라
    # 종류가 다르므로 여기서 따로 잡는다. 6c 가 1초 미만 조각을 이미 버리지만, 그 사실에
    # 기대면 6c 상수를 바꾸는 날 이 가드가 조용히 사라진다.
    if source_duration_sec and source_duration_sec > 0:
        empty = [i for i, s in enumerate(segments) if s.start_sec >= source_duration_sec]
        outside = sorted({d["index"] for d in lost} | set(empty))
        if outside:
            problems.append(f"{CODE_BEYOND_SOURCE}: 조각 {outside} 이 소스 끝"
                            f"({source_duration_sec:.1f}s) 밖")

    # ③ 발화 커버리지 — plausible 필터를 지난 구간으로만 잰다(E20-B4).
    intervals, _ = plausible_intervals(speech_intervals)
    if intervals is not None:
        cov = speech_coverage_ratio(segments, intervals)
        if cov < MIN_SPEECH_COVERAGE:
            problems.append(f"{CODE_LOW_SPEECH_COVERAGE}: {cov:.3f} < "
                            f"{MIN_SPEECH_COVERAGE}")

    # ④ 길이 — 조각 합계. 하드인 이유는 길이 정책이 결정적 사실이기 때문이다.
    total = sum(max(0.0, s.end_sec - s.start_sec) for s in segments)
    if total < float(min_sec) - LENGTH_TOLERANCE_SEC:
        problems.append(f"{CODE_TOO_SHORT}: {total:.2f}s < {float(min_sec):.2f}s")
    elif total > float(max_sec) + LENGTH_TOLERANCE_SEC:
        problems.append(f"{CODE_TOO_LONG}: {total:.2f}s > {float(max_sec):.2f}s")

    return problems


# ── ② 소프트 — 값만 ────────────────────────────────────────────────────────

def soft_signals(cand: dict, *, scene_cuts: list[float],
                 speech_intervals: list[tuple[float, float]],
                 words: list[dict]) -> dict:
    """감점 재료. **값만 낸다 — 판정하지 않는다.**

    근거가 없는 축은 `None` 이다(장면 전환 목록이 비었다 · 전사가 없다 · 그 조각에
    신뢰할 단어가 0개다). `score` 가 None 축을 중립으로 다룬다.

    키:
      stall_max_gap_sec  — **정지 화면 축**: 장면 전환 사이 최대 간격(조각 안에서만).
      silent_max_run_sec — **발화 0 run 축**: 발화가 없는 최장 구간.
        ⚠ 둘을 한 축으로 합치지 않는다. 정지 화면 신호는 **대화 고정샷을 오감점한다**
        (두 사람이 앉아 5분 대화하면 컷이 없다) — 그 편은 발화 커버리지가 높아 이
        축에서 살아난다. 반대로 액션 몽타주는 컷이 잦고 발화가 없다. 축을 바꿔치기하면
        두 경우 다 틀린다(E19·E20 규율 — 축을 교체하지 말고 따로 기록한다).
      speech_coverage    — plausible 필터를 지난 발화 커버 비율.
      cut_mid_sentence   — 문장 한복판에서 잘린 조각 경계 수.
      segment_count      — 조각 수(짜집기 억제).
      cohesion_gap_sec   — 조각 사이 소스 시간 최대 점프.
      lead_in_sec        — 첫 조각 시작 → 첫 신뢰 단어까지의 지연(서론 금지).
    """
    segments = _segments_of(cand)
    intervals, _ = plausible_intervals(speech_intervals)
    plaus_words = _plausible_words(words, intervals)

    out: dict[str, Any] = {
        "segment_count": len(segments),
        "stall_max_gap_sec": None,
        "silent_max_run_sec": None,
        "speech_coverage": None,
        "cut_mid_sentence": None,
        "cohesion_gap_sec": None,
        "lead_in_sec": None,
    }
    if not segments:
        return out

    # 조각 사이 최대 점프 — 조각이 하나면 점프가 없다(근거가 있으니 None 이 아니라 0.0).
    gaps = [max(0.0, b.start_sec - a.end_sec)
            for a, b in zip(segments, segments[1:])]
    out["cohesion_gap_sec"] = round(max(gaps), 3) if gaps else 0.0

    # 정지 화면 — 조각 **안**의 전환만 본다(조각 경계는 그 자체가 컷이다).
    if scene_cuts:
        cuts = sorted(_num(c) for c in scene_cuts)
        worst = 0.0
        for s in segments:
            marks = [s.start_sec] + [c for c in cuts if s.start_sec < c < s.end_sec] + [s.end_sec]
            worst = max(worst, max(b - a for a, b in zip(marks, marks[1:])))
        out["stall_max_gap_sec"] = round(worst, 3)

    if intervals is not None:
        out["speech_coverage"] = round(speech_coverage_ratio(segments, intervals), 4)

        # 발화 0 run — 조각 안에서 발화가 덮지 않은 최장 구간.
        spans = _merge([(iv.start_sec, iv.end_sec) for iv in intervals])
        worst = 0.0
        for s in segments:
            cursor = s.start_sec
            for a, b in spans:
                if b <= s.start_sec or a >= s.end_sec:
                    continue
                worst = max(worst, max(0.0, min(a, s.end_sec) - cursor))
                cursor = max(cursor, min(b, s.end_sec))
            worst = max(worst, max(0.0, s.end_sec - cursor))
        out["silent_max_run_sec"] = round(worst, 3)

        # 문장 중간 절단 — 조각 경계가 발화 구간 **안쪽**에 떨어졌는가(경계에 맞닿은
        # 것은 절단이 아니다).
        cuts_mid = 0
        for s in segments:
            for t in (s.start_sec, s.end_sec):
                if any(a < t < b for a, b in spans):
                    cuts_mid += 1
        out["cut_mid_sentence"] = cuts_mid

    # 서론 금지 — 첫 조각에 신뢰할 단어가 하나도 없으면 **판정하지 않는다**(None).
    # ⚠ 여기서 '첫 조각'은 **편집 순서**의 첫 조각이다(정렬본의 첫 항목이 아니다) —
    # 시청자가 0초에 보는 화면이 무엇인지 묻는 신호이기 때문이다. 선형 편성에서는
    # 둘이 같아 값이 변하지 않는다.
    if plaus_words is not None:
        raw_segments = _raw_segments_of(cand)
        head = raw_segments[0] if raw_segments else segments[0]
        inside = [w.start_sec for w in plaus_words
                  if head.start_sec <= w.start_sec < head.end_sec]
        if inside:
            out["lead_in_sec"] = round(max(0.0, min(inside) - head.start_sec), 3)

    return out


# ── ③ 점수 — 값 → 감점 ────────────────────────────────────────────────────

def _penalty(key: str, value: Any) -> float | None:
    """신호 하나 → 0~1 감점. 근거가 없으면(None) None."""
    if value is None:
        return None
    if key == "speech_coverage":
        return _clamp01(1.0 - float(value))          # 커버 1.0 = 무감점
    if key == "stall_max_gap_sec":
        return _clamp01(float(value) / STALL_MAX_GAP_SEC)
    if key == "silent_max_run_sec":
        return _clamp01(float(value) / SILENT_RUN_SAT_SEC)
    if key == "cohesion_gap_sec":
        span = COHESION_GAP_SAT_SEC - COHESION_GAP_FREE_SEC
        return _clamp01((float(value) - COHESION_GAP_FREE_SEC) / span)
    if key == "lead_in_sec":
        span = LEAD_IN_SAT_SEC - LEAD_IN_FREE_SEC
        return _clamp01((float(value) - LEAD_IN_FREE_SEC) / span)
    if key == "segment_count":
        span = SEGMENT_COUNT_SAT - SEGMENT_COUNT_FREE
        return _clamp01((float(value) - SEGMENT_COUNT_FREE) / span)
    if key == "cut_mid_sentence":
        # 조각마다 경계가 둘이라 최대치는 2n 이다 — 개수 자체가 아니라 **비율**로 본다
        # (조각이 많은 후보가 절단 개수만으로 이중 감점되지 않게).
        return None                                   # 분모가 필요 — score 가 채운다
    raise ValueError(f"모르는 소프트 신호: {key!r} — 허용: {sorted(DEFAULT_WEIGHTS)}")


def score(signals: dict, *, weights: dict | None = None) -> float:
    """소프트 신호 → 순위용 점수. **낮을수록 좋다**(감점 합). 순수·결정적.

    ⚠ 이 값으로 탈락시키지 마라 — 순위(파일 번호·발행 순서)에만 쓴다(기획서 §1 원칙 3).

    결측 축(None)은 **가점도 감점도 아니다**. 그냥 0 으로 두면 재료가 없는 후보가
    자동으로 1위가 된다 — 그래서 적용된 축의 가중 평균으로 채우고 전체 가중치로 되곱한다
    (전부 있으면 단순 가중합과 정확히 같다). 아는 축이 하나도 없으면 0.0 이고, 그때는
    전 후보가 동점이라 소스 시각순 타이브레이커가 순서를 정한다.
    """
    w = DEFAULT_WEIGHTS if weights is None else dict(weights)
    unknown = sorted(k for k in w if k not in DEFAULT_WEIGHTS)
    if unknown:
        raise ValueError(f"모르는 가중치 키: {unknown} — 허용: {sorted(DEFAULT_WEIGHTS)}")

    n_seg = signals.get("segment_count") or 0
    applied_w = 0.0
    raw = 0.0
    for key, weight in w.items():
        weight = float(weight)
        if weight <= 0:
            continue
        if key == "cut_mid_sentence":
            value = signals.get(key)
            pen = None if (value is None or n_seg <= 0) else \
                _clamp01(float(value) / (2 * float(n_seg)))
        else:
            pen = _penalty(key, signals.get(key))
        if pen is None:
            continue
        raw += weight * pen
        applied_w += weight
    if applied_w <= 0:
        return 0.0
    total_w = sum(float(v) for v in w.values() if float(v) > 0)
    return round(raw / applied_w * total_w, 4)


# ── 깔때기 ─────────────────────────────────────────────────────────────────

def _cand_id(cand: Any, index: int) -> str:
    cid = _field(cand, "id")
    return str(cid) if cid not in (None, "") else f"#{index}"


def _first_start(cand: Any) -> float:
    segs = _segments_of(cand)
    return segs[0].start_sec if segs else 0.0


def run_funnel(cands: list[dict], *, exception_sectors, source_duration_sec,
               scene_cuts, speech_intervals, words,
               min_sec: float, max_sec: float,
               keep: int = FUNNEL_KEEP) -> tuple[list[dict], dict]:
    """6c 통과분 → (남은 후보 ≤keep, 기록). 순수·결정적.

    순서는 셋을 차례로 건다: ① 하드 탈락 ② 점수 순위(동점은 소스 시각순) ③ 겹침 dedup
    (합집합 IoU ≥ IOU_DEDUP 이면 **점수가 더 나은 쪽**만 남긴다 — 정렬이 앞서므로 먼저
    담긴 쪽이 늘 더 낫다) ④ keep 상한 절단.

    기록의 `kept` 는 **순위 순서**이고, 모든 후보는 `kept` 나 `dropped` 중 정확히 한 곳에
    한 번 나온다(조용한 증발 금지 — 테스트가 이 항등을 고정한다).
    """
    ids = [_cand_id(c, i) for i, c in enumerate(cands or [])]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"후보 id 중복: {dup} — 기록이 id 로 묶이므로 유일해야 한다")

    intervals, filtered = plausible_intervals(speech_intervals)

    survivors: list[tuple[str, Any]] = []
    dropped: list[dict] = []
    intro_overlap: list[dict] = []
    heads = [(n, _sector_span((exception_sectors or {}).get(n))) for n in HEAD_SECTORS]
    heads = [(n, sp) for n, sp in heads if sp is not None]

    for cid, cand in zip(ids, cands or []):
        problems = hard_problems(cand, exception_sectors=exception_sectors,
                                 source_duration_sec=source_duration_sec,
                                 speech_intervals=speech_intervals,
                                 min_sec=min_sec, max_sec=max_sec)
        # intro 겹침은 하드가 아니다(기획서 §3-7 — 꼬리 구역만 하드). 감점 축도 계약에
        # 없으므로 **기록만** 남긴다 — 6b 경계를 되짚을 때 필요한 사실이다.
        for hname, hspan in heads:
            over = sum(max(0.0, min(s.end_sec, hspan[1]) - max(s.start_sec, hspan[0]))
                       for s in _segments_of(cand))
            if over > SECTOR_OVERLAP_TOLERANCE_SEC:
                intro_overlap.append({"id": cid, "sector": hname,
                                      "overlap_sec": round(over, 2)})
        if problems:
            dropped.append({"id": cid, "reasons": problems})
        else:
            survivors.append((cid, cand))

    # 🛑 **전량 하드 탈락 = 조용한 결번.** 이 레포는 '최소 1편 보장'을 네 곳에 깔아 뒀고
    # (`app/pipeline.py` 3446·3586·3598 · `app/v3/story.py` fallback_highlight), E20-B4 가
    # 정확히 같은 상황(스토리라인 셋이 전부 커버리지 미달)에서 정한 답이 **"탈락분 중
    # 최고를 경고와 함께 쓴다"**(run_log `story_coverage_fallback`)다.
    #
    # 여기서 빈 목록을 돌려주면 9단계 폴백도 무력해진다 — 그쪽 폴백은 `ranking` 1위를
    # 집는데 ranking 이 이 함수의 kept 로 만들어지기 때문이다. 통합 스모크 실측
    # (2026-09-03): 후보 2개가 전부 커버리지 미달 → kept=[] → approved=[] · fallback=False.
    # 유닛 테스트는 두 모듈이 각각 옳아서 이걸 못 잡았다. **이음새의 결함이다.**
    funnel_fallback: dict | None = None
    if not survivors and dropped:
        revivable = [d for d in dropped
                     if d.get("reasons")
                     and all(str(r).split(":", 1)[0] in RESURRECTABLE_CODES
                             for r in d["reasons"])]
        if revivable:
            by_id = dict(zip(ids, cands or []))
            # 되살릴 하나는 **소프트 점수가 가장 낮은(= 가장 나은)** 것. 동점은 다른
            # 곳과 같은 규칙으로 깬다(소스 시각 → id) — 여기만 다르면 결정성이 깨진다.
            ranked = sorted(
                ((score(soft_signals(by_id[d["id"]], scene_cuts=scene_cuts,
                                     speech_intervals=speech_intervals, words=words)),
                  _first_start(by_id[d["id"]]), d["id"]) for d in revivable))
            _sc, _st, best_id = ranked[0]
            survivors.append((best_id, by_id[best_id]))
            reasons = next(d["reasons"] for d in revivable if d["id"] == best_id)
            dropped = [d for d in dropped if d["id"] != best_id]
            funnel_fallback = {"id": best_id, "reasons": list(reasons),
                               "of": len(ids), "revivable": len(revivable),
                               "why": "전량 하드 탈락 — 품질 사유로 떨어진 것 중 최고를 "
                                      "경고와 함께 되살린다(E20-B4 판례)"}
        else:
            # 되살릴 수 없는 전량 탈락(전부 예고 오염·소스 밖)은 **살리지 않는다**.
            # 9단계가 `approved=[]` 를 내고 배선이 크게 실패하는 것이 맞다.
            funnel_fallback = {"id": None, "of": len(ids), "revivable": 0,
                               "why": "전량 하드 탈락 · 부활 가능한 사유 없음 "
                                      "(오염·소스 밖) — 발행할 편이 없다"}

    signals = {cid: soft_signals(cand, scene_cuts=scene_cuts,
                                 speech_intervals=speech_intervals, words=words)
               for cid, cand in survivors}
    scores = {cid: score(signals[cid]) for cid, _ in survivors}

    # 동점은 소스 시각순(첫 조각 start_sec → id). 6단계 나열 순서로 깨면 문서화 안 된
    # 위치 편향이 M9 뒷문으로 샌다.
    ordered = sorted(survivors, key=lambda p: (scores[p[0]], _first_start(p[1]), p[0]))

    kept: list[tuple[str, Any]] = []
    dedup: list[dict] = []
    for cid, cand in ordered:
        segs = _field(cand, "segments", default=None) or []
        clash = None
        for kid, kcand in kept:
            iou = union_iou(segs, _field(kcand, "segments", default=None) or [])
            if iou >= IOU_DEDUP:
                clash = (kid, iou)
                break
        if clash is None:
            kept.append((cid, cand))
            continue
        kid, iou = clash
        dedup.append({"kept": kid, "dropped": cid, "iou": round(iou, 4)})
        dropped.append({"id": cid, "reasons": [
            f"{CODE_DUPLICATE}: {kid} 와 합집합 IoU {iou:.3f} ≥ {IOU_DEDUP} "
            f"(점수 {scores[cid]:.3f} vs {scores[kid]:.3f} — 나은 쪽을 남긴다)"]})

    capped = 0
    if len(kept) > keep:
        for rank, (cid, _cand) in enumerate(kept[keep:], start=keep + 1):
            dropped.append({"id": cid, "reasons": [
                f"{CODE_KEEP_CAP}: 상한 {keep} 초과 — 순위 {rank}위"]})
        capped = len(kept) - keep
        kept = kept[:keep]

    record = {
        "kept": [cid for cid, _ in kept],
        "dropped": dropped,
        "signals": {cid: signals[cid] for cid, _ in survivors},
        "scores": {cid: scores[cid] for cid, _ in survivors},
        "dedup": dedup,
        "keep_cap": keep,
        "capped": capped,
        "intro_overlap": intro_overlap,
        "fallback": funnel_fallback,   # 전량 탈락 폴백(없으면 None — 조용한 결번 방지)
        "speech_filtered": filtered,   # plausible 필터가 뺀 환각성 전사 건수
        "of": len(ids),
    }
    return [cand for _cid, cand in kept], record
