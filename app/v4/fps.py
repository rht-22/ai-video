"""표본 fps 계단 — 4단계(probe)가 정하는 Gemini 표본 fps 의 **정본**.

계약 `docs/v4/M1-interfaces.md` §3 · 기획 `docs/v4/v4-plan.md` §4(운영자 결정 O2).

v3 는 표본 fps 를 하나(1.0)로 두고 예산을 넘칠 때만 연속 내림했다
(`app/v3/seq_analyze.resolve_scan_fps`). v4 는 그 자리를 **계단 + 예산 상한**으로 바꾼다:
짧은 소재는 후보 편성이 더 잘 보이도록 fps 를 올려 주고(≤40분 4 · ≤60분 3 · ≤90분 2),
그 위는 예산이 허락하는 만큼만 준다. 계단 값 자체는 실측이 아니라 **운영자 결정(O2)**
이고, 첫 30편은 fps 2 로 한 번 더 뽑아(shadow) 계단이 실제로 후보를 넓히는지 잰다
(기획서 §4) — 그 결과가 나오면 `FPS_LADDER` 한 줄만 고친다.

⚠ **산식은 둘이고 쓰는 자리가 다르다** (CLAUDE.md 「산식은 둘이고 쓰는 자리가 다르다」
· 2026-09-01 v4 프로브 `docs/v4/probes/`):

    상한 판정(400 초과) : count = 재생초 × (표본fps × 71 + 32)   ← count_tokens
    과금·예산 집계      : usage = 재생초 × (표본fps × 66 + 25)   ← usageMetadata

- 이 파일의 **예산 판정은 전부 count 쪽**이다. 400 "input token count exceeds the
  maximum" 은 count_tokens 값 기준으로 나기 때문이다(3시간 실물: count 1,112,401
  → 400, count 997,381 → 실호출 성공). 보수적인 쪽으로 계획하는 것이 계약이다.
- `usage_tokens` 는 **과금·예산 집계 전용**이다. 판정에 쓰지 마라 — 88.3% 라 상한을
  넘겨 놓고 통과로 읽는다.
- 🛑 그런데 **멀티파트(offset) 요청의 실과금은 `countTokens` 로 못 잡는다**(3.8배 과소
  계산 — `docs/v4/probes/mrcheck2.py`). 조각을 여러 파트로 붙이는 단계의 과금 집계는
  언제나 `generateContent` 의 `usageMetadata` 여야 한다. 이 파일이 다루는 것은
  **파일 하나를 통째로 첨부하는** 5·6 단계의 예산이다.

⚠ 오디오 몫(초당 32)은 fps 와 무관한 고정값이다 — **fps 를 낮춰도 안 줄어든다**
(3시간이면 오디오만 34.5만 = 상한의 33%). 그래도 빼지 않는다: intro/teaser·훅 판정의
음악·톤 단서다(v3 `build_scan_proxy` 독스트링과 같은 결정).
"""
from __future__ import annotations

import math
from typing import Any

# ── 계단 (운영자 결정 O2) ──────────────────────────────────────────────────
# (길이 상한 초, 그 이하일 때 쓰는 표본 fps). 마지막 계단(90분/2.0)을 넘는 소재는
# 그 마지막 값을 이어받아 예산 상한(fps_cap)이 잇는다 — 계약 §3 ④.
FPS_LADDER: tuple[tuple[float, float], ...] = (
    (40 * 60, 4.0), (60 * 60, 3.0), (90 * 60, 2.0),
)
FPS_QUANTUM = 0.05          # 90분 초과 구간의 내림 계단 — 같은 소재는 늘 같은 fps(결정성)

# 하한은 임의값이 아니라 **스냅 관용에서 파생**된다(v3 SCAN_SAMPLE_FPS_MIN 과 같은 유래):
# 표본 간격(1/fps)이 스냅 관용(±2.0s)을 넘으면 모델이 관용 안으로 경계를 제안할 근거를
# 화면에서 못 본다 — 그 아래로 내리면 어차피 스냅 반려로 재질의를 소진하고 죽는다.
# ⚠ 값의 정본은 격자 쪽(`app/v3/schemas.SNAP_TOLERANCE_SEC`, §7 승격 뒤에는
# `app.modules.grid`)이다. 여기서 import 하지 않는 것은 승격이 진행 중이기 때문이고,
# 대신 **테스트가 두 값을 묶는다**(갈리면 회귀 가드가 실패한다).
SNAP_TOLERANCE_SEC = 2.0
FPS_FLOOR = 1.0 / SNAP_TOLERANCE_SEC        # = 0.5

# ── 예산 ───────────────────────────────────────────────────────────────────
INPUT_LIMIT = 1_048_576     # gemini-3.7-flash 입력 상한(실측: 이 값을 넘기면 400)
# v3 는 프롬프트를 **고정 5만**으로 유보했다. v4 는 전사 전문을 함께 실으므로 고정값이
# 성립하지 않는다 — 텍스트는 **실측(text_tokens)** 으로 받고 그 위에 이만큼만 더 얹는다.
# 90분 소재의 fps 2 가 "전사 텍스트 ≤79k 일 때만"인 것이 이 산식의 직접 귀결이다
# (기획서 §4 표) — 넘으면 조용히 1.95 로 내려가고 사유가 기록된다.
TEXT_RESERVE_MIN = 30_000

TOKENS_PER_FRAME = 71       # count_tokens 단위 — 과금(usage 66)과 **다른 산식**이다
TOKENS_PER_SEC_AUDIO = 32
USAGE_TOKENS_PER_FRAME = 66     # 과금·예산 집계 전용(usageMetadata)
USAGE_TOKENS_PER_SEC_AUDIO = 25
# media_resolution HIGH — 프레임당 ×4(71→284 · 66→264). 미지정 = LOW 이므로
# **해상도를 낮춰 아낄 것은 없다**(실측: 미지정과 LOW 의 토큰이 동일).
HIGH_FRAME_MULTIPLIER = 4.0

# 프록시 파일 fps(운영자 결정 O1: 720p/30fps). 표본은 없는 프레임을 만들 수 없으므로
# **파일 fps 가 표본 fps 의 상한**이다. v3 는 파일이 10fps 라 계단 4 가 위태로웠지만
# v4 는 30fps 라 여유롭다 — 지금 6 을 넘는 호출은 없다(기획서 §4).
PROXY_FILE_FPS = 30.0

__all__ = [
    "FPS_LADDER", "FPS_QUANTUM", "FPS_FLOOR", "SNAP_TOLERANCE_SEC",
    "INPUT_LIMIT", "TEXT_RESERVE_MIN", "PROXY_FILE_FPS",
    "TOKENS_PER_FRAME", "TOKENS_PER_SEC_AUDIO",
    "USAGE_TOKENS_PER_FRAME", "USAGE_TOKENS_PER_SEC_AUDIO",
    "HIGH_FRAME_MULTIPLIER",
    "count_tokens", "usage_tokens", "max_duration_sec", "budget_tokens",
    "resolve_sample_fps",
]


def _tokens_per_sec(fps: float, per_frame: int, per_sec_audio: int, high: bool) -> float:
    """재생 1초의 토큰 — 프레임 몫(fps 비례) + 오디오 몫(고정). 순수."""
    frame = per_frame * (HIGH_FRAME_MULTIPLIER if high else 1.0)
    return fps * frame + per_sec_audio


def count_tokens(duration_sec: float, fps: float, *, high: bool = False) -> int:
    """**상한 판정용** 입력 토큰(count_tokens 단위) — 순수.

    60초 보정본 fps 6점(0.25~6)에서 오차 0의 선형식으로 실측됐다
    (예: 60초·1fps → 60×71 + 60×32 = 6,180, 실측 6,181).
    """
    return round(duration_sec * _tokens_per_sec(
        fps, TOKENS_PER_FRAME, TOKENS_PER_SEC_AUDIO, high))


def usage_tokens(duration_sec: float, fps: float, *, high: bool = False) -> int:
    """**과금·예산 집계용** 입력 토큰(usageMetadata 단위) — 순수.

    ⚠ 상한 판정에 쓰지 마라(count 의 88.3%라 넘긴 요청을 통과로 읽는다).
    ⚠ 멀티파트(offset) 요청에는 이 근사도 쓰지 마라 — 실과금은 조각 합계이고,
      정본은 응답의 usageMetadata 다(`docs/v4/probes/mrcheck3.py`).
    """
    return round(duration_sec * _tokens_per_sec(
        fps, USAGE_TOKENS_PER_FRAME, USAGE_TOKENS_PER_SEC_AUDIO, high))


def max_duration_sec(fps: float, *, budget: int) -> float:
    """그 표본 fps 로 한 번에 넣을 수 있는 최대 재생초(count 산식·순수)."""
    if fps < 0:
        raise ValueError(f"표본 fps 는 음수일 수 없다: {fps}")
    return budget / _tokens_per_sec(fps, TOKENS_PER_FRAME, TOKENS_PER_SEC_AUDIO, False)


def budget_tokens(text_tokens: int = 0) -> int:
    """영상에 쓸 수 있는 토큰 예산 — 계약 §3 ①. 순수.

    텍스트 토큰은 **실측**이다(전사 + 프롬프트 + 리서치). 음수는 0 으로 본다 —
    측정 실패를 '예산이 늘었다'로 읽으면 안 된다.
    """
    return INPUT_LIMIT - max(int(text_tokens), 0) - TEXT_RESERVE_MIN


def _floor_quantum(value: float) -> float:
    """FPS_QUANTUM 계단 **내림**(결정성). 부동소수 잔차로 한 계단 더 떨어지지 않도록
    작은 epsilon 을 더한다 — 정확히 2.0 인 값이 1.95 가 되면 안 된다."""
    stepped = math.floor(max(value, 0.0) / FPS_QUANTUM + 1e-9) * FPS_QUANTUM
    return round(stepped, 4)


def _validate_ladder(ladder: tuple, file_fps: float) -> tuple[tuple[float, float], ...]:
    """계단 표가 성립하는지 본다 — 깨진 표는 조용히 엉뚱한 계단을 고르므로 즉시 실패."""
    rungs = tuple((float(limit), float(fps)) for limit, fps in ladder)
    if not rungs:
        raise ValueError("표본 fps 계단이 비어 있다 — 고를 값이 없다")
    prev_limit = 0.0
    prev_fps = math.inf
    for limit, fps in rungs:
        if limit <= prev_limit:
            raise ValueError(f"계단의 길이 상한은 증가해야 한다: {rungs}")
        if fps <= 0:
            raise ValueError(f"계단의 표본 fps 는 양수여야 한다: {rungs}")
        if fps > prev_fps:
            raise ValueError(f"긴 소재가 더 높은 fps 를 받는 계단이다: {rungs}")
        if fps > file_fps:
            # 없는 프레임은 만들 수 없다 — v3 가 파일 10fps 에서 걸던 것과 같은 벽이다.
            raise ValueError(
                f"표본 fps {fps:g} 가 프록시 파일 fps {file_fps:g} 를 넘는다")
        prev_limit, prev_fps = limit, fps
    return rungs


def resolve_sample_fps(duration_sec: float, *, text_tokens: int = 0,
                       ladder: tuple = FPS_LADDER,
                       file_fps: float = PROXY_FILE_FPS) -> tuple[float, dict]:
    """소재 길이 → (표본 fps, 기록 dict). 순수·결정적 — 계약 §3.

    ① 예산 = INPUT_LIMIT − max(text_tokens, 0) − TEXT_RESERVE_MIN
    ② 계단: 길이가 ladder 의 상한 이하면 그 fps. 마지막 계단을 넘는 소재는 그 마지막
       값을 이어받고, 그 위는 ④ 가 잇는다(긴 소재가 더 높은 fps 를 받지 않는다).
    ③ 예산 상한 fps_cap = floor_quantum((예산 − 32·D) / (71·D))
    ④ fps = min(계단, fps_cap) — 계단이 예산에 안 들면 내리고 **사유를 남긴다**
    ⑤ fps < FPS_FLOOR 면 **크게 실패**(ValueError). 비싼 인코딩·업로드 **앞**에서 죽는
       것이 계약이다 — 조용히 더 내리면 스냅 반려로 재질의를 소진하고 어차피 죽는데,
       그때는 이미 프록시 인코딩(수 분)과 업로드(3시간 소재 실측 364초)를 태운 뒤다.

    ⚠ 길이를 모르면(≤0) **판정하지 않고** 계단 첫 값을 그대로 준다(오판 금지 — 모르는
      것을 틀렸다고 하지 않는다). 그 사실은 reason="duration_unknown" 으로 남는다.

    ValueError 에는 같은 모양의 기록 dict 가 `.note`(reason="floor_failed")로 붙는다 —
    실패한 실행도 run_log 에 왜 죽었는지 남길 수 있어야 한다.
    """
    rungs = _validate_ladder(ladder, float(file_fps))
    dur = float(duration_sec or 0.0)
    budget = budget_tokens(text_tokens)
    note: dict[str, Any] = {
        "duration_sec": round(dur, 3),
        "text_tokens": max(int(text_tokens), 0),
        "budget_tokens": budget,
        "tokens_per_frame": TOKENS_PER_FRAME,
        "tokens_per_sec_audio": TOKENS_PER_SEC_AUDIO,
    }

    if dur <= 0:
        first = rungs[0][1]
        return first, {**note, "ladder_fps": first, "fps_cap": None, "fps": first,
                       "est_count_tokens": 0, "est_usage_tokens": 0,
                       "reason": "duration_unknown"}

    ladder_fps = rungs[-1][1]
    for limit, fps_at in rungs:
        if dur <= limit:
            ladder_fps = fps_at
            break

    cap_exact = (budget - TOKENS_PER_SEC_AUDIO * dur) / (TOKENS_PER_FRAME * dur)
    fps_cap = _floor_quantum(cap_exact)
    fps = min(ladder_fps, fps_cap)
    note = {**note, "ladder_fps": ladder_fps, "fps_cap": fps_cap,
            "fps_cap_exact": round(cap_exact, 6), "fps": fps,
            "est_count_tokens": count_tokens(dur, fps),
            "est_usage_tokens": usage_tokens(dur, fps),
            "reason": "ladder" if fps >= ladder_fps else "budget_capped"}

    if fps < FPS_FLOOR - 1e-9:
        floor_max = max_duration_sec(FPS_FLOOR, budget=budget)
        note = {**note, "reason": "floor_failed"}
        err = ValueError(
            f"소재가 너무 길어 한 번에 넣을 수 없다 — {dur / 60:.1f}분 · 텍스트 "
            f"{note['text_tokens']:,} 토큰(예산 {budget:,}). 필요 표본 fps "
            f"{cap_exact:.3f} 가 하한 {FPS_FLOOR:g} 아래다. 하한에서의 최대 길이는 "
            f"{floor_max / 60:.1f}분({floor_max / 3600:.2f}시간)이다. "
            f"하한은 스냅 관용 ±{SNAP_TOLERANCE_SEC:g}s 에서 온 값이다(표본 간격이 "
            "관용을 넘으면 모델이 관용 안의 경계를 제안할 근거를 화면에서 못 본다). "
            "소재를 나눌지, 전사·프롬프트 텍스트를 줄일지는 사람이 결정해야 한다.")
        err.note = note        # type: ignore[attr-defined]
        raise err
    return fps, note
