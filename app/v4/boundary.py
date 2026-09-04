"""6b 경계 정밀 — 신고된 exception 경계를 국소 창으로 다시 보고 정정한다.

계약 정본 `docs/v4/M3-interfaces.md` §3(운영자 결정 O6) · 기획 `docs/v4/v4-plan.md` §3-6b.

계기는 실사고다. **가왕쇼 6화** — Stage 1 이 다음화 예고 시작을 **49.5초 늦게** 판정해
최종 쇼츠 엔딩이 예고로 오염됐다(V3-M7 채점표 fail 1건 · CLAUDE.md V3-M7 절).
전체 훑기 한 판은 예고 도입부(본편형 풀스크린 하이라이트)를 구분할 화면 근거를 못 본다 —
그래서 **경계 근처만 다시, 촘촘히**(fps 6) 본다.

## v3 `app/v3/refine.py` 에서 가져온 것 / 버린 것 (판정)

v3 는 이 단계의 원본이고 **동결된 라이브러리**다 — 판단 함수는 베끼지 않고 **부른다**
(`tests/test_v4_guards.py` ABSORB_TABLE 의 `app.v3.refine.boundary_probe_windows` 항목이
그 주소를 지킨다).

| v3 | v4 | 판정 |
|---|---|---|
| `scene_cut_candidates` | 그대로 **부른다** | 재사용 — 모델은 후보 id 만 고른다(시각 무출력) |
| `verify_sample_window` | 그대로 **부른다** | 재사용 — 중앙 표본 규약 |
| `validate_verify_response` | 그대로 **부른다** | 재사용 |
| `VERIFY_PROMPT`·`ZONE_DESC` | 그대로 **쓴다** | 재사용 — 문구가 갈리면 판정이 갈린다 |
| `FLASH_BUDGET`·`MAX_PROBES`·`MAX_VERIFIES`·`VERIFY_SAMPLE_SEC`·`TAIL_WINDOW_SEC`·`PROBE_SAMPLE_FPS` | **import** | 재선언 금지(계약 §3) |
| `boundary_probe_windows`(±90 하드코딩) | `probe_windows(window_sec=…)` | **다시 짓는다** — O6 이 ±60 으로 좁혔고 창 크기가 인자가 돼야 한다. 갈린 것이 **창 크기 하나뿐**임은 `tests/test_v4_boundary.py` 의 v3 대조 가드가 못박는다 |
| `apply_boundary(…, duration)` | `apply_boundary(…, grid_times)` | **다시 짓는다** — 격자 스냅(`snap_time`)과 노트 반환이 v4 계약이다 |
| `validate_probe_response` | 창 밖 어휘 2종 추가 | **다시 짓는다**(아래 ±60 의 대가) |
| `PROBE_PROMPT` | `BOUNDARY_PROMPT` | **다시 짓는다** — 출력 어휘가 달라졌다 |
| `_cut_probe_clip` + `_call_probe` | `video.call_video` offset 멀티파트 | **버린다** — 물리 재단이 없다(업로드 1회 · 기획서 §1 행 5) |
| `retile_sequences` | — | **버린다** — v4 에는 `sequences` 가 없다(청크 상세 분석을 없앴다) |
| 긴 zone 부분 표본 + 재프로브 | — | **버린다** — 그 경로의 존재 이유가 "진짜 경계가 ±90 창 밖"이었고(포핸즈2 실측 138.5 → 47.5), v4 는 그 일을 **창 밖 재프로브**가 한다(아래 계산) |

## ±60 으로 좁힌 것의 대가 — 창 밖 재프로브

v3 가 ±90 인 이유는 **가왕쇼 지각 49.5초가 ±30 창 밖**이었기 때문이다(refine 모듈
독스트링). O6 이 ±60 으로 좁힌 것은 창당 토큰을 2/3 로 줄이려는 것이고, 그 대가는
**창 밖 재프로브로 갚는다**(기획서 §9 O6 "창 밖 재프로브 유지").

  · 첫 창 = 원경계 ±`window_sec` (길이 `L = 2×window_sec` = 120s)
  · 모델이 `{"boundary": "earlier"}`/`{"boundary": "later"}` 로 **창 밖을 지목**하면
    그 방향으로 인접한 창(길이 L)을 한 번 더 본다 → 도달 반경 ±180s.

실측 대조(둘 다 이 경로로 잡힌다):

    가왕쇼 teaser  지각 49.5s → 첫 창 ±60 **안**            (재프로브 없이 잡힘)
    포핸즈2 intro  138.5 → 47.5 (91s 밖) → 첫 창 [78.5,198.5] 밖 → 재프로브 창
                   [0, 78.5] **안**                          (v3 가 부분 표본으로 잡던 그 건)

## 실패는 원판정 유지 (오염 방지 비대칭)

호출 실패·재질의 소진·후보 없음·스냅 실패·예산 소진 — 전부 **경계를 안 옮긴다**.
잘못 옮기는 것이 안 옮기는 것보다 나쁘다(V3-M8 계약 그대로). 그리고 zone 을 **줄이는**
이동은 해방 구간이 실제로 본편인지 한 번 더 확인한 뒤에만 적용한다(v3 실사고 2호:
teaser.end 프로브가 예고 속 텍스트 카드를 경계로 오인해 예고 후반 45.5s 를 본편으로
해방 → 채점 FAIL 재현).

🛑 **실호출로만 알 수 있는 것은 여기 없다** — 이 워크트리에 `GEMINI_API_KEY` 가 없다.
프롬프트가 실제로 `earlier`/`later` 를 잘 내는지, offset 멀티파트를 서버가 창 하나로
읽는지는 **키가 있는 노드의 몫**이다(계약 §0 · 기획서 §12 '추정' 목록).
"""
from __future__ import annotations

import math
from typing import Any

from app.modules.grid.schemas import EXCEPTION_KEYS, SNAP_TOLERANCE_SEC, snap_time
from app.modules.grid.timegrid import grid_snap_times

# v3 는 동결된 라이브러리다 — 판단 함수와 상수를 **부르고 가져온다**(베끼지 않는다).
from app.v3.refine import (
    FLASH_BUDGET,
    MAX_PROBES,
    MAX_VERIFIES,
    PROBE_SAMPLE_FPS,
    TAIL_WINDOW_SEC,
    VERIFY_PROMPT,
    VERIFY_SAMPLE_SEC,
    ZONE_DESC,
    scene_cut_candidates,
    validate_verify_response,
    verify_sample_window,
)
from app.v3.seq_analyze import MAX_REASKS
from app.v4.funnel import TAIL_SECTORS
from app.v4.video import Clip, VideoCallError, VideoParseError, call_video, clips_within_source

__all__ = [
    "PROBE_WINDOW_SEC", "TAIL_PROBE_SEC", "PROBE_SAMPLE_FPS", "FLASH_BUDGET",
    "MAX_PROBES", "MAX_VERIFIES", "MAX_WINDOW_EXTENSIONS", "BOUNDARY_TOKEN_BUDGET",
    "OUT_OF_WINDOW", "NO_BOUNDARY", "BOUNDARY_PROMPT",
    "probe_windows", "extend_window", "validate_probe_response", "apply_boundary",
    "build_probe_prompt", "run_boundary_probe",
]

# ── 상수 ────────────────────────────────────────────────────────────────────

PROBE_WINDOW_SEC = 60.0        # 운영자 결정 O6 — v3 는 90.0. 대가는 창 밖 재프로브가 갚는다
TAIL_PROBE_SEC = TAIL_WINDOW_SEC   # = 180.0 — exception 신고가 **없는** 편의 의무 확인(기획서 §3)

# 창 밖 재프로브 횟수. 1 이면 도달 반경이 ±3×window_sec 이고, 그것이 v3 ±90 을 덮는다
# (위 실측 표). 늘리려면 예산(FLASH_BUDGET)을 함께 봐야 한다 — 한 경계에 창을 더 쓰면
# 다른 경계가 미검사로 남는다.
MAX_WINDOW_EXTENSIONS = 1

# 러닝타임 양 끝에 붙은 경계는 검사 대상이 아니다(움직일 곳이 없다) — v3 와 같은 자.
EDGE_MARGIN_SEC = 0.5

# 이보다 짧은 zone 은 실체 검증을 하지 않는다 — 짧은 카드류는 표본 판정이 더 위험하다(v3).
MIN_VERIFY_ZONE_SEC = 8.0
# 이보다 짧은 해방 구간은 축소 검증을 하지 않는다(호출 값어치가 없다) — v3 와 같은 자.
MIN_SHRINK_VERIFY_SEC = 8.0

# v3 refine 은 1024 였다. 올린 이유는 V3-M2 실측이다 — thinking 이 **출력 예산을 나눠 써서**
# JSON 이 MAX_TOKENS 로 잘렸고, 잘린 조각은 엉뚱한 JSONDecodeError 로 보였다. 답 자체는
# 한 줄이라 4096 은 넉넉하고(8단계 FLAG_MAX_OUTPUT_TOKENS 와 같은 자), 안 쓰면 과금되지 않는다.
PROBE_MAX_OUTPUT_TOKENS = 4096

# 누적 토큰 상한(계약 §3 "호출 수와 누적 토큰 둘 다 강제"). 기획서 §4 의 6b 추정은
# 50,000~230,000 이고 그 상한의 1.7배를 안전판으로 잡는다 — **실측 전이라 정상 실행을
# 죽이지 않는 쪽**으로 넉넉히 둔다(M8 실측 라운드에서 조인다).
BOUNDARY_TOKEN_BUDGET = 400_000

# 모델 출력 어휘. 후보 id · 경계 없음 · 창 밖 두 방향.
NO_BOUNDARY = "none"
OUT_OF_WINDOW = ("earlier", "later")


class _BudgetExhausted(RuntimeError):
    """예산(호출 수·누적 토큰)이 바닥났다 — 남은 전부 원판정 유지."""


# ── zone 표기 ───────────────────────────────────────────────────────────────
#
# ⚠ 열쇠 이름이 두 벌이다: v4 체크포인트는 `start_sec`/`end_sec`(계약 M1 §8)이고
#   v3 산출·M0 채점기(`app/replay/exception_score.py`)는 `start`/`end` 를 읽는다.
#   **읽을 때는 둘 다 받고, 쓸 때는 그 zone 이 쓰던 이름을 그대로 유지한다** — 여기서
#   조용히 이름을 바꾸면 읽는 쪽 하나가 그 zone 을 통째로 못 본다(= 신고가 사라진다).

_START_KEYS = ("start_sec", "start")
_END_KEYS = ("end_sec", "end")


def _num(value: Any) -> float | None:
    """숫자로 읽히면 float. bool 은 숫자가 아니다."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _zone_key_names(zone: dict) -> tuple[str, str]:
    """그 zone 이 쓰던 열쇠 이름 → (시작 열쇠, 끝 열쇠). 없으면 v4 정본 이름."""
    sk = next((k for k in _START_KEYS if k in zone), _START_KEYS[0])
    ek = next((k for k in _END_KEYS if k in zone), _END_KEYS[0])
    return sk, ek


def _zone_span(zone: Any) -> tuple[float, float] | None:
    """zone dict → (시작, 끝). 시각이 없거나 못 읽으면 None(= 신고 없음)."""
    if not isinstance(zone, dict):
        return None
    s = next((_num(zone[k]) for k in _START_KEYS if k in zone), None)
    e = next((_num(zone[k]) for k in _END_KEYS if k in zone), None)
    if s is None or e is None:
        return None
    return s, e


def _copy_sector(sector: Any) -> dict:
    """한 겹 사본 — 순수 함수가 넘겨받은 것을 제자리에서 고치지 않는다(규율 5)."""
    return {k: (dict(v) if isinstance(v, dict) else v)
            for k, v in (sector or {}).items()}


def _known_keys_or_die(sector: Any) -> None:
    """모르는 구역 이름은 **즉시 실패**한다.

    `funnel.hard_problems` 와 같은 규율이다 — 조용히 무시하면 그 구역이 판정에서
    통째로 빠지고, 그 후보가 검사 없이 나간다(가왕쇼 사고 그대로)."""
    unknown = sorted(k for k in (sector or {}) if k not in EXCEPTION_KEYS)
    if unknown:
        raise ValueError(
            f"모르는 예고 구역 이름: {unknown} — 허용: {list(EXCEPTION_KEYS)}. "
            "조용히 무시하면 그 구역이 경계 정밀에서 빠진 채 발행된다.")


# ── 순수 로직 ───────────────────────────────────────────────────────────────

def probe_windows(exception_sector: dict, duration_sec: float, *,
                  window_sec: float = PROBE_WINDOW_SEC,
                  tail_sec: float = TAIL_PROBE_SEC) -> list[dict]:
    """트리거 목록 — 신고된 경계마다 ±window 창, 신고가 없으면 꼬리 의무 창. 순수·결정적.

    · 창은 **원경계 중심 ±window_sec** 이다(v3 와 같은 모양 · 크기만 O6 의 60).
      원경계가 항상 창 안이라야 '동일 컷 재확인'이 성립한다.
    · 러닝타임 양 끝(±`EDGE_MARGIN_SEC`)에 붙은 경계는 내지 않는다 — 움직일 곳이 없다.
    · exception 신고가 **하나도 없으면** 말미 `tail_sec` 를 의무로 본다(기획서 §3).
      신고가 하나라도 있으면 꼬리 창은 없다 — 그 편은 이미 모델이 봤다.
    · 순회는 dict 순서가 아니라 `EXCEPTION_KEYS` 순서다(같은 입력 = 같은 순서 · 결정성.
      JSON 열쇠 순서가 달라도 감사 기록이 흔들리지 않는다).
    · 상한(`MAX_PROBES`)은 **여기서 걸지 않는다** — 자르는 쪽이 무엇을 안 봤는지
      기록해야 하므로 호출자의 감사 의무다(v3 와 같은 규약).

    ⚠ 반환하는 probe 에 `duration_sec` 을 싣는다. `apply_boundary` 가 꼬리 프로브로
      새 teaser 를 만들 때 그 끝이 러닝타임이기 때문이다 — 계약 시그니처에 duration 이
      없으므로 probe 가 들고 다닌다.
    """
    dur = _num(duration_sec)
    if dur is None or dur <= 0:
        raise ValueError(
            f"러닝타임이 유효하지 않다: {duration_sec!r} — 창은 격자의 duration_sec 을 "
            f"기준으로 만든다(배선 오류일 때 창을 지어내지 않는다)")
    w = _num(window_sec)
    if w is None or w <= 0:
        raise ValueError(f"window_sec 이 양수가 아니다: {window_sec!r}")
    tail = _num(tail_sec)
    if tail is None or tail <= 0:
        raise ValueError(f"tail_sec 이 양수가 아니다: {tail_sec!r}")
    _known_keys_or_die(exception_sector)

    def _window(center: float) -> tuple[float, float]:
        return max(0.0, center - w), min(dur, center + w)

    probes: list[dict] = []
    zones = 0
    for key in EXCEPTION_KEYS:
        span = _zone_span((exception_sector or {}).get(key))
        if span is None:
            continue
        zones += 1
        s, e = span
        if s > EDGE_MARGIN_SEC:
            t0, t1 = _window(s)
            probes.append({"zone": key, "edge": "start", "t0": t0, "t1": t1,
                           "orig": s, "duration_sec": dur, "extension": 0})
        if e < dur - EDGE_MARGIN_SEC:
            t0, t1 = _window(e)
            probes.append({"zone": key, "edge": "end", "t0": t0, "t1": t1,
                           "orig": e, "duration_sec": dur, "extension": 0})

    # 🛑 **꼬리 구역 신고가 없으면** 말미를 의무로 본다 — '아무 신고도 없으면'이 아니다.
    #
    # 기획서 §3 의 문구는 "exception 전무 편은 tail 의무 프로브"이고 처음엔 그대로
    # `zones == 0` 이었다. 그런데 그러면 **머리만 신고하고 예고를 통째로 놓친 편**의
    # 꼬리가 아무도 안 본다 — 6b 가 막으라고 존재하는 바로 그 사고다(가왕쇼 6화: 예고
    # 50초가 쇼츠 엔딩을 오염시켰다). 스모크에서 실제로 그 구멍이 열렸다(2026-09-03:
    # intro 만 신고된 편 → 꼬리 프로브 0콜).
    #
    # 대가는 편당 **최대 1콜**(≈76k 토큰 · 편 예산의 8% 남짓)이고, 그것도 모델이 꼬리를
    # 하나도 신고하지 않은 편에만 든다. 조용한 예고 오염보다 싸다.
    # ⚠ 기획서 문구보다 넓은 판정이다 — 되돌리려면 이 조건 한 줄만 `zones == 0` 으로.
    tail_zones = sum(1 for k in TAIL_SECTORS
                     if _zone_span((exception_sector or {}).get(k)) is not None)
    # 창이 편의 절반을 넘으면 '말미'가 아니므로 내지 않는다(v3 와 같은 자) — 짧은
    # 소재에서 편 전체를 예고로 볼 위험을 만들지 않는다.
    if tail_zones == 0 and dur > tail / 2:
        probes.append({"zone": "tail", "edge": "start",
                       "t0": max(0.0, dur - tail), "t1": dur,
                       "orig": None, "duration_sec": dur, "extension": 0})
    return probes


def extend_window(probe: dict, direction: str, *,
                  window_sec: float = PROBE_WINDOW_SEC) -> dict | None:
    """창 밖 지목 → 그 방향으로 인접한 창 하나. 못 넓히면 None. 순수.

    창 길이는 `2×window_sec`(첫 창이 ±window 이므로) — 같은 크기로 이어 붙이면 도달
    반경이 `±(1 + MAX_WINDOW_EXTENSIONS)×2×window_sec − window_sec` 다. 기본값에서
    ±180s 이고 그것이 v3 ±90 을 덮는다(모듈 독스트링의 실측 표).

    ⚠ 소스 밖으로는 안 나간다. 이미 끝에 붙어 있으면(넓힐 폭 없음) None 이고, 호출자는
    그때 **원판정을 유지**한다 — 여기서 억지 창을 만들면 모델이 못 본 곳을 봤다고 답한다.
    """
    if direction not in OUT_OF_WINDOW:
        raise ValueError(f"방향은 {list(OUT_OF_WINDOW)} 중 하나: {direction!r}")
    w = _num(window_sec)
    if w is None or w <= 0:
        raise ValueError(f"window_sec 이 양수가 아니다: {window_sec!r}")
    length = 2.0 * w
    dur = _num(probe.get("duration_sec"))
    if dur is None or dur <= 0:
        raise ValueError("probe 에 duration_sec 이 없다 — probe_windows 산출이어야 한다")

    t0, t1 = float(probe["t0"]), float(probe["t1"])
    if direction == "earlier":
        n1 = t0
        n0 = max(0.0, t0 - length)
    else:
        n0 = t1
        n1 = min(dur, t1 + length)
    if n1 - n0 < EDGE_MARGIN_SEC:
        return None
    return {**probe, "t0": n0, "t1": n1,
            "extension": int(probe.get("extension") or 0) + 1,
            "extended": direction}


def validate_probe_response(resp: Any, candidates: list[dict]) \
        -> tuple[str | None, list[str]]:
    """모델 응답 → (선택 id | 'none' | 'earlier' | 'later', 반려 사유). 순수.

    v3 `validate_probe_response` 에 **창 밖 어휘 두 개**를 더한 것이다. 그 둘이 ±60 의
    대가를 갚는 유일한 통로라, 모르는 값으로 취급해 반려하면 좁힌 창이 그대로 벽이 된다.
    """
    if not isinstance(resp, dict):
        return None, [f"응답이 객체가 아니다: {type(resp).__name__}"]
    b = resp.get("boundary")
    if b == NO_BOUNDARY or b in OUT_OF_WINDOW:
        return b, []
    known = {c["id"] for c in candidates or []}
    if not isinstance(b, str) or b not in known:
        return None, [
            f"boundary 는 후보 id 또는 {[NO_BOUNDARY, *OUT_OF_WINDOW]}: {b!r} "
            f"(후보: {sorted(known)[:8]})"]
    return b, []


def apply_boundary(exception_sector: dict, probe: dict, new_t: float,
                   grid_times: list[float]) -> tuple[dict, list[str]]:
    """모델이 제안한 경계 → 격자 스냅 후 반영 → (새 sector, 노트). 순수·결정적.

    · 스냅은 `grid.schemas.snap_time` 을 **부른다**(수식 복제 금지 · 계약 §3).
      관용(`SNAP_TOLERANCE_SEC` 2.0s) 밖이면 **안 옮긴다** — 시간 정본 원칙(LLM 은 제안만,
      확정 시각은 격자 눈금)의 마지막 관문이다.
    · 꼬리(`zone == "tail"`) 프로브는 새 teaser 를 만든다(발견 컷 ~ 러닝타임 끝).
    · 역전(끝 ≤ 시작)·이웃 zone 침범은 **기각**한다 — 창이 이웃 위로 뻗을 수 있어서
      이동 결과가 겹치면 커버리지 계약(겹침 0)이 깨진다(v3 리뷰 확정 방어 그대로).
    · 안 옮긴 경우에도 **왜 안 옮겼는지 노트로 돌려준다**(조용한 드롭 금지 · 규율 3).

    ⚠ 입력을 제자리에서 고치지 않는다. 반환 dict 의 zone 은 **그 zone 이 쓰던 열쇠
      이름**을 유지하고, 새로 만드는 zone 만 v4 정본(`start_sec`/`end_sec`)을 쓴다.
    """
    _known_keys_or_die(exception_sector)
    zone_key = probe.get("zone")
    if zone_key != "tail" and zone_key not in EXCEPTION_KEYS:
        raise ValueError(f"모르는 zone: {zone_key!r} — 허용: {['tail', *EXCEPTION_KEYS]}")

    original = _copy_sector(exception_sector)
    t = _num(new_t)
    if t is None:
        return original, [f"제안 시각이 숫자가 아니다({new_t!r}) — 원판정 유지"]

    snapped, err = snap_time(t, list(grid_times or []))
    if snapped is None:
        return original, [
            f"격자 스냅 실패(가장 가까운 눈금까지 {err:.3f}s > "
            f"{SNAP_TOLERANCE_SEC}s) — 원판정 유지"]
    notes: list[str] = []
    if abs(snapped - t) > 1e-9:
        notes.append(f"격자 스냅: {t:.3f} → {snapped:.3f} (오차 {err:.3f}s)")

    out = _copy_sector(exception_sector)
    if zone_key == "tail":
        dur = _num(probe.get("duration_sec"))
        if dur is None or dur <= 0:
            raise ValueError("꼬리 프로브에 duration_sec 이 없다")
        if snapped >= dur - EDGE_MARGIN_SEC:
            return original, notes + [
                f"꼬리 경계가 러닝타임에 붙었다({snapped:.3f} ≥ {dur - EDGE_MARGIN_SEC:.3f}) "
                f"— 만들 구간이 없다, 원판정 유지"]
        out["teaser"] = {"start_sec": round(snapped, 3), "end_sec": round(dur, 3)}
        notes.append(f"꼬리 의무 확인에서 teaser 신설: {snapped:.3f}~{dur:.3f}")
        return out, notes

    span = _zone_span(out.get(zone_key))
    if span is None:
        return original, notes + [f"{zone_key} 구역이 없다(이미 폐기됐거나 시각 없음) — 원판정 유지"]
    zone = out[zone_key]
    sk, ek = _zone_key_names(zone)
    s, e = span
    if probe.get("edge") == "start":
        s = snapped
    elif probe.get("edge") == "end":
        e = snapped
    else:
        raise ValueError(f"모르는 edge: {probe.get('edge')!r} — 'start' 또는 'end'")

    if e <= s:
        return original, notes + [
            f"{zone_key} 구간 역전({s:.3f} ≥ {e:.3f}) — 이동 기각, 원판정 유지"]
    zone[sk], zone[ek] = round(s, 3), round(e, 3)

    spans = sorted(sp for sp in (_zone_span(z) for z in out.values()) if sp is not None)
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        if b0 < a1 - 1e-9:
            return original, notes + [
                f"{zone_key} 이동이 이웃 구역과 겹친다({a0:.3f}~{a1:.3f} ∩ "
                f"{b0:.3f}~{b1:.3f}) — 이동 기각, 원판정 유지"]
    return out, notes


# ── 프롬프트 ────────────────────────────────────────────────────────────────
#
# v3 `refine.PROBE_PROMPT` 의 판별 신호 문장은 실측이 밴 자산이라 그대로 옮겼고, **출력
# 어휘만** 넓혔다(창 밖 두 방향). v3 를 그대로 쓸 수 없는 이유가 이 두 값이고, 그래서
# import 가 아니라 여기 다시 적는다 — 문구를 고치면 두 파이프라인의 판정이 갈리므로
# **v3 를 따라 고치지 말 것**(v3 는 동결이다).

BOUNDARY_PROMPT = """당신은 방송 편집 검수자다. 첨부한 영상은 원본의 {t0}~{t1} 구간(창 안 0초 = 원본 {t0})이다. 이 창 어딘가에 **{desc}와 본편의 경계**가 있는지 정밀하게 찾아라.

판별 신호: 콜라주/장식 프레임 테두리, 스태프롤·제작진 자막, "다음 이야기" 문구, 본편 서사와 단절된 빠른 몽타주(장소·의상이 컷마다 바뀜), 전용 카드.
⚠ 예고/크레딧 몽타주는 종종 **본편처럼 보이는 풀스크린 하이라이트 컷으로 문을 연다** — 경계는 장식이 뜨는 순간이 아니라 본편 서사가 끝나는 첫 컷이다.

## 경계 후보 (클립 내 상대초 | id) — 이 중에서만 고른다
{cands}

## 출력 (JSON 만)
{{"boundary": "c03"}}                  — 경계가 이 창 안에 있고 그 컷이 경계일 때
{{"boundary": "none"}}                 — 이 창 전체가 한 종류(전부 본편이거나 전부 {desc})라 경계가 없을 때
{{"boundary": "earlier"}}              — 경계가 이 창보다 **앞**에 있을 때(창 시작 시점에 이미 넘어가 있다)
{{"boundary": "later"}}                — 경계가 이 창보다 **뒤**에 있을 때(창 끝까지도 아직 안 넘어갔다)"""


def build_probe_prompt(probe: dict, candidates: list[dict], *,
                       reject_note: str = "") -> str:
    """경계 프로브 프롬프트. 순수.

    ⚠ 시각은 **창 상대초**로 준다 — 모델이 보는 것은 offset 으로 잘라 붙인 창 하나이고
    그 안의 0초는 창 시작이다. 절대초로 말하면 모델이 본 적 없는 좌표계다."""
    desc = ZONE_DESC.get(probe.get("zone"), "예고/크레딧")
    lines = "\n".join(f"- {c['rel']:.1f}s | {c['id']}" for c in candidates)
    prompt = BOUNDARY_PROMPT.format(t0=f"{float(probe['t0']):.1f}s",
                                    t1=f"{float(probe['t1']):.1f}s",
                                    desc=desc, cands=lines)
    if reject_note:
        prompt += f"\n\n⚠ 직전 반려: {reject_note}"
    return prompt


# ── 실행 ────────────────────────────────────────────────────────────────────

def _usage_tokens(usage: Any) -> int:
    """usage 기록 → 이 호출이 쓴 토큰. `total` 이 없으면 조각 합으로 센다.

    ⚠ 0 으로 때우지 않는다 — 예산을 못 세면 예산이 없는 것과 같다. 다만 usage 자체가
    안 온 호출(응답에 usage_metadata 부재)은 셀 재료가 없으므로 0 이고, 그 사실은
    감사 기록의 `usages` 에 None 으로 남는다."""
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total")
    if isinstance(total, int):
        return total
    return sum(v for k in ("prompt", "thoughts", "candidates")
               if isinstance(v := usage.get(k), int))


class _Budget:
    """호출 수·누적 토큰 **둘 다** 강제한다(계약 §3 · 기획서 §7).

    ⚠ 6b 는 **순차**다(8단계 flags 와 다르다) — 그래서 Lock 이 없다. 병렬로 바꾸는
    날에는 check-and-increment 를 Lock 안으로 옮겨야 한다(기획서 §8: v3 식 int 는 샌다).
    ⚠ 토큰은 **쓰고 나서** 더해진다 — 한 호출이 상한을 넘길 수는 있고, 그 다음 호출이
    막힌다. 요청 전에 그 호출의 비용을 알 방법이 없다(countTokens 는 멀티파트에서
    3.8배 과소 — 기획서 §2-C)."""

    def __init__(self, calls: int, tokens: int | None) -> None:
        self.max_calls = int(calls)
        self.max_tokens = tokens
        self.calls = 0
        self.tokens = 0

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise _BudgetExhausted(f"호출 상한 {self.max_calls}회 소진")
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            raise _BudgetExhausted(
                f"누적 토큰 상한 소진({self.tokens} ≥ {self.max_tokens})")

    def spend(self, usage: Any) -> None:
        self.calls += 1
        self.tokens += _usage_tokens(usage)


def _ask(gemini, handle, probe: dict, prompt: str, *, duration_sec: float,
         budget: _Budget, audit: dict,
         log) -> tuple[Any, dict | None, str | None, bool]:
    """창 하나를 붙여 1회 호출 → (파싱된 응답|None, usage|None, 실패 사유|None, permanent).

    창은 `clips_within_source` 를 **먼저** 지난다 — endOffset 은 소스를 넘어도 오류 없이
    조용히 클램프되므로(기획서 §7) 보내기 전에 자르지 않으면 모델이 무엇을 봤는지 모른다."""
    budget.check()
    clips, records = clips_within_source(
        [Clip(start_sec=float(probe["t0"]), end_sec=float(probe["t1"]))], duration_sec)
    for rec in records:                      # 조용한 클램프·드롭 금지(규율 3)
        audit["clip_belt"].append({"zone": probe.get("zone"), "edge": probe.get("edge"),
                                   **rec})
    if not clips:
        # 재질의해도 같은 창이라 같은 결과다 — permanent 로 표시해 예산을 태우지 않는다.
        return None, None, "경계 벨트가 창을 통째로 버렸다(소스 밖) — 원판정 유지", True

    try:
        resp, usage = call_video(gemini, handle, prompt,
                                 sample_fps=PROBE_SAMPLE_FPS, clips=clips,
                                 max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
                                 log=log)
    except VideoParseError as e:
        # 파싱 실패는 **토큰을 제일 많이 먹은 시도**다(MAX_TOKENS 절단) — 그 숫자를 버리면
        # 예산이 새므로 usage 를 들고 온 것을 그대로 센다(video.VideoParseError 계약).
        budget.spend(e.usage)
        audit["usages"].append(e.usage)
        return None, e.usage, f"응답 파싱 실패: {e}", False
    except VideoCallError as e:
        budget.spend(e.usage)
        if e.usage is not None:
            audit["usages"].append(e.usage)
        return None, e.usage, f"호출 실패({e.kind or '분류 없음'}): {e}", e.kind == "permanent"

    budget.spend(usage)
    audit["usages"].append(usage)
    return resp, usage, None, False


def _probe_once(gemini, handle, probe: dict, cands: list[dict], *, duration_sec: float,
                budget: _Budget, audit: dict, log) -> tuple[str | None, list[dict]]:
    """재질의 ≤MAX_REASKS 를 포함한 창 하나의 판정 → (선택 | None, 시도 기록).

    ⚠ permanent 실패(4xx)는 **재질의하지 않는다** — 같은 요청을 다시 보내도 같은 답이고
    예산만 태운다(E11 규약을 부르는 쪽에서 한 번 더 지킨다)."""
    attempts: list[dict] = []
    reject = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = build_probe_prompt(probe, cands, reject_note=reject)
        resp, usage, failure, permanent = _ask(gemini, handle, probe, prompt,
                                               duration_sec=duration_sec, budget=budget,
                                               audit=audit, log=log)
        if failure is not None:
            attempts.append({"attempt": attempt, "problems": [failure], "usage": usage})
            if permanent:
                break
            reject = failure
            continue
        chosen, problems = validate_probe_response(resp, cands)
        attempts.append({"attempt": attempt, "problems": problems, "usage": usage})
        if chosen is not None:
            return chosen, attempts
        reject = "; ".join(problems[:3])
    return None, attempts


def _verify_window(gemini, handle, zone_key: str, v0: float, v1: float, *,
                   duration_sec: float, budget: _Budget, audit: dict,
                   log) -> tuple[str | None, str | None]:
    """구간 하나가 본편인지 exception 인지 → (kind | None, 실패 사유 | None).

    프롬프트·판정은 v3 것을 그대로 부른다(`VERIFY_PROMPT`·`validate_verify_response`) —
    문구가 갈리면 같은 화면에 다른 판정이 난다."""
    probe = {"zone": zone_key, "edge": "verify", "t0": v0, "t1": v1,
             "duration_sec": duration_sec}
    prompt = VERIFY_PROMPT.format(t0=f"{v0:.1f}s", t1=f"{v1:.1f}s",
                                  desc=ZONE_DESC.get(zone_key, "예고/크레딧"))
    resp, _usage, failure, _permanent = _ask(gemini, handle, probe, prompt,
                                             duration_sec=duration_sec, budget=budget,
                                             audit=audit, log=log)
    if failure is not None:
        return None, failure
    kind, problems = validate_verify_response(resp)
    if kind is None:
        return None, "; ".join(problems[:2])
    return kind, None


def _shrink_interval(probe: dict, new_t: float) -> tuple[float, float] | None:
    """이동이 zone 을 **줄인다면** 해방되는 구간. 확대·동일이면 None. 순수."""
    orig = _num(probe.get("orig"))
    if orig is None:
        return None                       # 꼬리 프로브 — 만들기만 하므로 축소가 없다
    if probe.get("edge") == "start" and new_t > orig + 0.01:
        return orig, new_t
    if probe.get("edge") == "end" and new_t < orig - 0.01:
        return new_t, orig
    return None


def run_boundary_probe(gemini, handle, *, exception_sector: dict, grid: dict,
                       duration_sec: float, budget: int = FLASH_BUDGET,
                       budget_tokens: int | None = BOUNDARY_TOKEN_BUDGET,
                       window_sec: float = PROBE_WINDOW_SEC,
                       log=print) -> tuple[dict, dict]:
    """6b 실행 → (정정된 exception_sector, audit).

    순서는 셋이다:
      ① 경계 프로브 — 창 하나당 1콜. 모델이 창 밖을 지목하면 그 방향으로 한 창 더
         (`MAX_WINDOW_EXTENSIONS`). 축소 이동은 해방 구간 실체 검증을 통과해야 적용된다.
      ② zone 실체 검증 — 중앙 표본을 보고 '본편'이면 그 zone 을 폐기(≤`MAX_VERIFIES`).
      ③ 아무것도 안 움직였으면 **넘겨받은 sector 를 그대로** 돌려준다(항등).

    **실패·예산 소진은 원판정 유지**다. 이미 적용된 이동은 남는다 — 그 이동은 각자
    자기 근거(스냅·이웃 검사·축소 검증)를 통과한 것이고, 뒤가 막혔다고 되돌리면 확인된
    사실을 버리는 것이 된다.

    ⚠ 순수하지 않다(모델을 부른다). 순수한 부분은 `probe_windows`·`apply_boundary`·
      `validate_probe_response`·`extend_window` 이고 테스트가 그쪽을 값으로 고정한다.
    """
    _known_keys_or_die(exception_sector)
    grid_times = grid_snap_times(grid)
    all_probes = probe_windows(exception_sector, duration_sec, window_sec=window_sec)
    probes = all_probes[:MAX_PROBES]

    audit: dict[str, Any] = {
        "window_sec": float(window_sec),
        "budget": {"calls": int(budget), "tokens": budget_tokens},
        "flash_calls": 0, "tokens": 0, "moved": 0,
        "probes": [], "usages": [], "clip_belt": [], "notes": [], "stopped": None,
    }
    # 조용한 절단 금지 — 무엇을 안 봤는지가 감사의 절반이다(v3 리뷰 확정).
    for dropped in all_probes[MAX_PROBES:]:
        audit["probes"].append({k: dropped[k] for k in ("zone", "edge", "orig")}
                               | {"result": f"프로브 상한 {MAX_PROBES} 초과 — 미검사"})
    if not probes:
        audit["notes"].append("검사할 경계가 없다(신고 없음 + 꼬리 창 없음)")
        return exception_sector, audit

    bud = _Budget(budget, budget_tokens)
    sector = exception_sector
    moved = 0

    def _finish() -> tuple[dict, dict]:
        audit["flash_calls"] = bud.calls
        audit["tokens"] = bud.tokens
        audit["moved"] = moved
        return (sector if moved else exception_sector), audit

    for probe in probes:
        rec: dict[str, Any] = {k: probe[k] for k in ("zone", "edge", "t0", "t1", "orig")}
        rec["windows"] = []
        chain = probe
        try:
            while True:
                cands = scene_cut_candidates(grid, chain["t0"], chain["t1"])
                win: dict[str, Any] = {"t0": round(float(chain["t0"]), 3),
                                       "t1": round(float(chain["t1"]), 3),
                                       "extension": chain.get("extension", 0),
                                       "candidates": len(cands)}
                rec["windows"].append(win)
                if not cands:
                    win["result"] = "후보 없음 — 원판정 유지"
                    rec["result"] = win["result"]
                    break

                chosen, attempts = _probe_once(gemini, handle, chain, cands,
                                               duration_sec=duration_sec,
                                               budget=bud, audit=audit, log=log)
                win["attempts"] = attempts
                if chosen is None:
                    win["result"] = "재질의 소진 — 원판정 유지"
                    rec["result"] = win["result"]
                    break

                if chosen in OUT_OF_WINDOW:
                    nxt = (extend_window(chain, chosen, window_sec=window_sec)
                           if int(chain.get("extension") or 0) < MAX_WINDOW_EXTENSIONS
                           else None)
                    if nxt is None:
                        win["result"] = (f"창 밖 지목({chosen}) — 더 볼 창이 없다"
                                         f"(확장 상한 {MAX_WINDOW_EXTENSIONS} 또는 소스 끝), "
                                         f"원판정 유지")
                        rec["result"] = win["result"]
                        break
                    win["result"] = f"창 밖 지목({chosen}) — 그 방향으로 한 창 더"
                    log(f"  [v4/boundary] {chain['zone']}.{chain['edge']} 창 밖 "
                        f"({chosen}) → {nxt['t0']:.1f}~{nxt['t1']:.1f}s 재프로브")
                    chain = nxt
                    continue

                if chosen == NO_BOUNDARY:
                    win["result"] = "none(이 창에 경계 없음) — 원판정 유지"
                    rec["result"] = win["result"]
                    break

                new_t = next(c["t"] for c in cands if c["id"] == chosen)
                win["chosen"] = {"id": chosen, "t": new_t}

                shrink = _shrink_interval(chain, new_t)
                if shrink and shrink[1] - shrink[0] >= MIN_SHRINK_VERIFY_SEC:
                    # 오염 방지 비대칭(v3 실사고 2호) — zone 을 줄이는 이동은 해방 구간이
                    # 정말 본편일 때만 적용한다. 확대 방향은 손실 위험뿐이라 그냥 간다.
                    kind, failure = _verify_window(gemini, handle, chain["zone"],
                                                   shrink[0], shrink[1],
                                                   duration_sec=duration_sec,
                                                   budget=bud, audit=audit, log=log)
                    win["shrink_check"] = {"t0": round(shrink[0], 3),
                                           "t1": round(shrink[1], 3),
                                           "kind": kind, "failure": failure}
                    if kind != "main":
                        win["result"] = (f"축소 기각(해방 구간 실체="
                                         f"{kind or failure or '판정 불가'}) — 원판정 유지")
                        rec["result"] = win["result"]
                        break

                applied, notes = apply_boundary(sector, chain, new_t, grid_times)
                win["notes"] = notes
                orig = _num(chain.get("orig"))
                if applied == sector:
                    win["result"] = ("원판정 확인(같은 컷)"
                                     if orig is not None and abs(new_t - orig) < 0.01
                                     else f"이동 기각: {'; '.join(notes) or '변화 없음'}")
                else:
                    sector = applied
                    moved += 1
                    win["result"] = {"chosen": chosen, "new_t": new_t,
                                     "moved_sec": (None if orig is None
                                                   else round(new_t - orig, 3))}
                    log(f"  [v4/boundary] {chain['zone']}.{chain['edge']} "
                        f"{chain.get('orig')} → {new_t} "
                        f"(창 확장 {chain.get('extension', 0)}회)")
                rec["result"] = win["result"]
                break
        except _BudgetExhausted as e:
            rec.setdefault("result", f"예산 소진 — 원판정 유지: {e}")
            audit["probes"].append(rec)
            audit["stopped"] = str(e)
            log(f"  [v4/boundary] ⚠ {e} — 남은 경계는 검사하지 않는다(원판정 유지)")
            return _finish()
        audit["probes"].append(rec)

    # ── zone 실체 검증 — 경계 프로브가 오탐을 '확인'해버리는 유형 방어 ─────────
    # (v3 실측: 신병4 는 본편 장면을 credit 으로 판정했고 경계 프로브는 none 을 냈다 —
    #  경계가 없는 것이 맞다, zone 자체가 틀렸으니까.)
    verified = 0
    for key in EXCEPTION_KEYS:
        if verified >= MAX_VERIFIES:
            break
        span = _zone_span(sector.get(key))
        if span is None:
            continue
        zs, ze = span
        rec = {"zone": key, "edge": "verify"}
        if ze - zs < MIN_VERIFY_ZONE_SEC:
            rec["result"] = f"짧은 구역({ze - zs:.1f}s) — 표본 판정이 더 위험, 유지"
            audit["probes"].append(rec)
            continue
        if ze - zs > VERIFY_SAMPLE_SEC + 1.0:
            # 긴 zone 을 중앙 60s 표본 하나로 폐기하지 않는다(v3 와 같은 보수). v3 는
            # 여기서 부분 표본 + 재프로브를 했는데, 그 경로의 존재 이유가 "진짜 경계가
            # ±90 창 밖"이었다 — v4 는 그 일을 위 ①의 창 밖 재프로브가 한다.
            rec["result"] = (f"긴 구역({ze - zs:.1f}s > {VERIFY_SAMPLE_SEC}s) — 중앙 표본으로 "
                             f"폐기 불가, 유지(경계는 ①이 본다)")
            audit["probes"].append(rec)
            continue
        v0, v1 = verify_sample_window(zs, ze)
        rec["t0"], rec["t1"] = round(v0, 3), round(v1, 3)
        try:
            kind, failure = _verify_window(gemini, handle, key, v0, v1,
                                           duration_sec=duration_sec,
                                           budget=bud, audit=audit, log=log)
        except _BudgetExhausted as e:
            rec["result"] = f"예산 소진 — 유지: {e}"
            audit["probes"].append(rec)
            audit["stopped"] = str(e)
            log(f"  [v4/boundary] ⚠ {e} — 남은 실체 검증은 하지 않는다(유지)")
            return _finish()
        verified += 1
        if kind == "main":
            sector = _copy_sector(sector)
            sector[key] = None            # 폐기를 **명시**한다(열쇠를 지우면 '신고 없음'과 같아진다)
            moved += 1
            rec["result"] = f"본편 판정 — 구역 폐기({zs:.1f}~{ze:.1f})"
            log(f"  [v4/boundary] {key} 실체 검증: 본편 — 폐기({zs:.1f}~{ze:.1f})")
        else:
            rec["result"] = f"유지({kind or failure or '판정 불가'})"
        audit["probes"].append(rec)

    return _finish()
