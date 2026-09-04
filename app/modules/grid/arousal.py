"""arousal 곡선 — 전 장르 공통 오디오 피처만(기획서 §9-B). M1 은 **데이터 생성까지**다.

피처 4종(웃음 감지 등 장르 특화 금지 — 기획 확정):
  energy_db       음성 에너지(RMS, dBFS)
  pitch_var       피치 변화율 — 이웃 창 f0(자기상관 추정)의 절대 변화, 무성 창은 0
  dynamics        음악 다이내믹스/급격한 레벨 전환 — 이웃 창 energy_db 절대 변화
  speech_density  발화 밀도 — ±HALF_DENSITY_WIN 창의 전사 단어 수/초

score 는 네 피처의 z-점수 평균(전 채널 동일 계수 — 장르·채널별 튜닝 금지, §9-B).
⚠ 가중치 **소비** 로직(importance 타이브레이커 등)은 M3 의 일이다 — 여기서는
계약된 상한(±0.5 보정)조차 구현하지 않는다. 산출은 결정적: 같은 PCM·같은 단어
목록이면 바이트까지 같은 목록이 나온다(반올림 3자리 고정).
"""
from __future__ import annotations

import numpy as np

from app.modules.grid.audio import SAMPLE_RATE

HOP_SEC = 0.5            # 곡선 해상도 — span(중앙 3s급)보다 촘촘하면 충분
WIN_SEC = 1.0
F0_MIN_HZ = 75.0         # 자기상관 f0 탐색 대역(사람 음성 기본 주파수)
F0_MAX_HZ = 400.0
VOICED_RMS_DB = -45.0    # 이보다 조용한 창은 f0 추정을 하지 않는다(무성)
HALF_DENSITY_WIN = 2.5   # 발화 밀도 창 반폭(초)


def _frame_view(pcm: np.ndarray, hop: int, win: int) -> np.ndarray:
    n = max(0, 1 + (len(pcm) - win) // hop) if len(pcm) >= win else 0
    if n == 0:
        return np.empty((0, win), dtype=pcm.dtype)
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return pcm[idx]


def _f0_autocorr(frame: np.ndarray, sr: int) -> float:
    """창 하나의 f0(Hz) — FFT 자기상관. 못 찾으면 0."""
    x = frame - frame.mean()
    if not np.any(x):
        return 0.0
    n = len(x)
    size = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(x, size)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    if ac[0] <= 0:
        return 0.0
    lag_min = int(sr / F0_MAX_HZ)
    lag_max = min(int(sr / F0_MIN_HZ), n - 1)
    if lag_max <= lag_min:
        return 0.0
    seg = ac[lag_min:lag_max + 1] / ac[0]
    peak = int(np.argmax(seg))
    if seg[peak] < 0.3:              # 주기성 미약 — 음악/잡음 창에서 헛피치 방지
        return 0.0
    return sr / (lag_min + peak)


def compute_arousal(pcm: np.ndarray, duration_sec: float,
                    words: list[dict]) -> list[dict]:
    """PCM + 전사 단어 → arousal 곡선 [{t, energy_db, pitch_var, dynamics,
    speech_density, score}] (t = 창 중심, HOP_SEC 간격)."""
    hop, win = int(HOP_SEC * SAMPLE_RATE), int(WIN_SEC * SAMPLE_RATE)
    frames = _frame_view(pcm, hop, win)
    if len(frames) == 0:
        return []
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    energy_db = 20.0 * np.log10(np.maximum(rms, 1e-6))

    f0 = np.zeros(len(frames))
    for i, fr in enumerate(frames):
        if energy_db[i] >= VOICED_RMS_DB:
            f0[i] = _f0_autocorr(fr, SAMPLE_RATE)
    pitch_var = np.zeros(len(frames))
    both = (f0[1:] > 0) & (f0[:-1] > 0)
    pitch_var[1:][both] = np.abs(np.diff(f0))[both]

    dynamics = np.zeros(len(frames))
    dynamics[1:] = np.abs(np.diff(energy_db))

    starts = np.array([float(w["t0"]) for w in words]) if words else np.empty(0)
    density = np.zeros(len(frames))
    centers = HOP_SEC * np.arange(len(frames)) + WIN_SEC / 2
    for i, c in enumerate(centers):
        if len(starts):
            density[i] = np.count_nonzero(
                (starts >= c - HALF_DENSITY_WIN) & (starts < c + HALF_DENSITY_WIN)
            ) / (2 * HALF_DENSITY_WIN)

    def z(a: np.ndarray) -> np.ndarray:
        sd = float(a.std())
        return (a - a.mean()) / sd if sd > 1e-9 else np.zeros_like(a)

    score = (z(energy_db) + z(pitch_var) + z(dynamics) + z(density)) / 4.0

    out = []
    for i, c in enumerate(centers):
        if c > duration_sec:
            break
        out.append({
            "t": round(float(c), 3),
            "energy_db": round(float(energy_db[i]), 3),
            "pitch_var": round(float(pitch_var[i]), 3),
            "dynamics": round(float(dynamics[i]), 3),
            "speech_density": round(float(density[i]), 3),
            "score": round(float(score[i]), 3),
        })
    return out
