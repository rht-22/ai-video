"""노래(가창) 구간 검출 — 오디오의 **비트 주기성**으로 판정한다(결정적 · numpy 만).

2026-09-07 사용자 지시(가왕쇼 7화): "노래하는 부분은 자막을 빼고 싶다". 무엇이 노래인지
코드가 구분해야 한다. 실측(가왕쇼 7화 16k wav)으로 고른 신호:

  · 자기상관 f0 안정성(지속음) — 반주·관객 소리가 섞인 트랙에서 헛피치가 많아 **무력**
    (노래 0.03 vs 말 0.02~0.11, 구분 없음).
  · 크로마 엔트로피·저역 비율·스펙트럼 평탄도 — 노래 0.88~0.90 vs 말 0.89~0.92, 경계 겹침.
  · **onset 세기 곡선의 자기상관 피크(0.25~1.0s 지연 = 60~240 BPM)** — 반주가 있는 노래는
    박이 규칙적이라 피크가 서고 말은 서지 않는다. 4초 창 기준 노래 구간 0.34~0.44(창의
    81~100% 양성) vs 말 0.13~0.21(0~23%). 노래로 표시된 창이 말에서 최대 23%, 노래에서
    최소 81% — 그 사이에 문턱을 둔다.

판정은 **창 단위 → 연속 구간**이다. 양성 창을 이어 붙이되 짧은 틈(MERGE_GAP_SEC)은 잇고,
MIN_RUN_SEC 미만의 짧은 구간은 버린다(예능 효과음·짧은 BGM 스팅이 대사 자막을 지우면
안 된다 — 노래는 최소 수십 초다). 게이트는 호출부(`--subtitle-skip-singing`) — 드라마
BGM 이 대사 자막을 지우는 오작동을 막으려고 기본은 꺼짐이다.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from app.v3.audio import SAMPLE_RATE

SCHEMA = "v3_singing/v1"
WIN_SEC = 4.0          # 자기상관 창 — 최대 지연 1.0s 의 4배는 있어야 피크가 선다
HOP_SEC = 2.0
N_FFT = 1024
HOP = 160              # 100 fps onset 곡선
LAG_MIN_SEC = 0.25     # 240 BPM
LAG_MAX_SEC = 1.0      # 60 BPM
BEAT_MIN = 0.28        # 창 양성 문턱(피크) — 말 최대 0.21 · 노래 최소 0.27~0.34 사이
H2_MIN = 0.10          # 2배 지연 자기상관(박의 배음) 확인 — 단발 피크 오탐 방지
MIN_RUN_SEC = 8.0      # 이보다 짧은 양성 구간은 버린다(효과음·스팅)
MERGE_GAP_SEC = 4.0    # 양성 구간 사이 이 이하의 틈은 잇는다(브릿지·숨)


def _onset_envelope(pcm: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    """스펙트럼 플럭스(양의 변화 합) — 100 fps. 순수."""
    if len(pcm) < N_FFT + HOP:
        return np.zeros(0, dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(pcm, N_FFT)[::HOP] * np.hanning(N_FFT)
    mag = np.log1p(np.abs(np.fft.rfft(frames, axis=1)) * 100.0)
    return np.maximum(np.diff(mag, axis=0), 0.0).sum(axis=1).astype(np.float32)


def beat_curve(pcm: np.ndarray, sr: int = SAMPLE_RATE, *, win_sec: float = WIN_SEC,
               hop_sec: float = HOP_SEC) -> list[dict]:
    """[{t0, t1, beat, h2}] — 창마다 onset 곡선 자기상관의 (0.25~1.0s) 최대 피크와 그
    2배 지연 값. 반올림 3자리 고정(결정성). 순수."""
    env = _onset_envelope(pcm, sr)
    fps = sr / HOP
    step, hop = int(win_sec * fps), int(hop_sec * fps)
    lo, hi = int(LAG_MIN_SEC * fps), int(LAG_MAX_SEC * fps)
    out: list[dict] = []
    for i in range(0, max(0, len(env) - step) + 1, hop):
        f = env[i:i + step]
        if len(f) < step:
            break
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[len(f) - 1:]
        denom = float(ac[0])
        if denom <= 1e-9:
            peak, h2 = 0.0, 0.0
        else:
            ac = ac / denom
            seg = ac[lo:hi]
            k = int(np.argmax(seg))
            peak = float(seg[k])
            k2 = 2 * (lo + k)
            h2 = float(ac[k2]) if k2 < len(ac) else 0.0
        out.append({"t0": round(i / fps, 3), "t1": round((i + step) / fps, 3),
                    "beat": round(peak, 3), "h2": round(h2, 3)})
    return out


def singing_windows(curve: list[dict], *, beat_min: float = BEAT_MIN, h2_min: float = H2_MIN,
                    min_run_sec: float = MIN_RUN_SEC,
                    merge_gap_sec: float = MERGE_GAP_SEC) -> list[tuple[float, float]]:
    """양성 창(beat ≥ beat_min · h2 ≥ h2_min) → 병합 구간 [(t0, t1)]. 순수."""
    pos = [(float(c["t0"]), float(c["t1"])) for c in curve
           if float(c["beat"]) >= beat_min and float(c["h2"]) >= h2_min]
    merged: list[list[float]] = []
    for a, z in pos:
        if merged and a <= merged[-1][1] + merge_gap_sec:
            merged[-1][1] = max(merged[-1][1], z)
        else:
            merged.append([a, z])
    return [(round(a, 3), round(z, 3)) for a, z in merged if z - a >= min_run_sec]


def detect_singing(audio_path: Path) -> dict:
    """오디오 파일 → {schema, params, windows[[t0,t1]], curve_n}. 파일을 ffmpeg 로 16k 모노
    디코드(`audio.load_pcm`). 52분 소재 실측 수 초."""
    from app.v3.audio import load_pcm
    curve = beat_curve(load_pcm(Path(audio_path)))
    wins = singing_windows(curve)
    return {"schema": SCHEMA,
            "params": {"win_sec": WIN_SEC, "hop_sec": HOP_SEC, "beat_min": BEAT_MIN,
                       "h2_min": H2_MIN, "min_run_sec": MIN_RUN_SEC,
                       "merge_gap_sec": MERGE_GAP_SEC},
            "windows": [[a, z] for a, z in wins], "curve_n": len(curve)}


def in_windows(t: float, windows) -> bool:
    return any(float(a) <= t < float(z) for a, z in windows or ())


# ── 두 번째 증인: Stage 2 사건 단위 문장 ────────────────────────────────────
# 실측(가왕쇼 7화 전편, 41개 음향 양성 창): 노래는 전부 잡히지만 **BGM 이 깔린 대사**
# (오프닝 26~40s · 감사 인사 260~276s · 대기실 2964~3112s)도 8~20초짜리 양성으로 뜬다.
# 단어 밀도는 노래 0.3~0.93/s vs BGM 대사 0.8~1.75/s 로 겹친다(260~276 이 0.81). 그래서
# 음향 창은 **Stage 2 가 그 자리를 노래로 적었을 때만** 확정한다 — 음향이 경계를, 문장이
# 정체를 댄다(textcheck 의 두 증인 규율). 노래 사건 단위는 거의 예외 없이 곡명('…')·
# 열창·부른다·듀엣 을 적는다(실측 25/25 확정 창 전부 적중, BGM 대사 창 0 적중).
SING_HINT = re.compile(r"부르|열창|노래|후렴|코러스|떼창|가창|듀엣|앙코르|곡|'[^']{1,30}'")


def confirm_windows(windows, rows: list[dict]) -> tuple[list[tuple[float, float]], list[dict]]:
    """음향 양성 창 × Stage 2 사건 단위(t0/t1/content) → (확정 창, 기각 기록).
    창과 겹치는 사건 단위 중 하나라도 SING_HINT 에 걸리면 확정. 겹치는 단위가 없으면
    기각(근거 없음). 순수."""
    ok: list[tuple[float, float]] = []
    rejected: list[dict] = []
    for a, z in windows or ():
        a, z = float(a), float(z)
        hit = [r for r in rows if float(r["t1"]) > a and float(r["t0"]) < z]
        if any(SING_HINT.search(str(r.get("content") or "")) for r in hit):
            ok.append((a, z))
        else:
            rejected.append({"t0": a, "t1": z,
                             "why": "Stage 2 문장에 노래 근거 없음" if hit else "겹치는 사건 단위 없음"})
    return ok, rejected


# ── 창 가장자리 보강: 같은 노래 사건 단위 안에서 잇고 경계까지 늘린다 ─────────────────
# 실측(가왕쇼 7화 네 번째 편, 박서진·최수호 듀엣 m033~m034 2006.7~2199.1): 음향 창이
# [2008~2036] [2042~2060] [2084~2120] [2130~2144] 로 끊겨 그 틈(댄스 브레이크·화음 구간)의
# 가사 3줄이 자막으로 새어 나갔고, 노래 첫 구호('가자!' 2006.7)도 창 밖이었다. 비트 검출은
# 4초 창·2초 hop 이라 가장자리에서 ±2~4초, 브레이크에서 수 초씩 빈다. Stage 2 의 사건 단위는
# "듀엣 무대" 같은 노래 사건을 하나로 적으므로 **그 단위 안**의 창은 한 노래다 — 창 사이를 잇고,
# 단위 경계가 창에서 EDGE_EXTEND_MAX_SEC 안이면 경계까지 늘린다(멀면 그 사이는 MC 멘트 —
# 전유진 편 '여러분 기다리셨습니다' 2199~2213 은 창(2216)에서 17s 앞이라 살아남는다).
EDGE_EXTEND_MAX_SEC = 10.0


def bridge_windows(windows, rows: list[dict],
                   *, edge_max_sec: float = EDGE_EXTEND_MAX_SEC) -> list[tuple[float, float]]:
    """확정 창 × Stage 2 사건 단위(t0/t1/content) → 같은 노래 단위 안의 창을 잇고 경계로 늘린
    창 목록(정렬·병합). 노래 근거(SING_HINT)가 없는 단위는 건드리지 않는다. 순수."""
    wins = [(float(a), float(z)) for a, z in windows or ()]
    if not wins:
        return []
    out: list[tuple[float, float]] = []
    used: set[int] = set()
    for r in rows:
        if not SING_HINT.search(str(r.get("content") or "")):
            continue
        t0, t1 = float(r["t0"]), float(r["t1"])
        inside = [i for i, (a, z) in enumerate(wins) if z > t0 and a < t1]
        if not inside:
            continue
        lo = min(wins[i][0] for i in inside)
        hi = max(wins[i][1] for i in inside)
        if 0 <= lo - t0 <= edge_max_sec:
            lo = t0
        if 0 <= t1 - hi <= edge_max_sec:
            hi = t1
        out.append((lo, hi))            # 늘리기만 — 다음 단위로 넘어간 창을 자르지 않는다
        used.update(inside)
    out.extend(w for i, w in enumerate(wins) if i not in used)
    out.sort()
    merged: list[list[float]] = []
    for a, z in out:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], z)
        else:
            merged.append([a, z])
    return [(round(a, 3), round(z, 3)) for a, z in merged]
