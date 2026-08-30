"""v3 오디오 계층 — 16kHz 모노 PCM 로드·silencedetect 구간 목록.

기존 재사용: 오디오 추출은 `speech.extract_audio_from_video`(-vn -ac 1 -ar 16000
pcm_s16le)를 그대로 쓴다(호출은 pipeline 쪽). 무음 '구간 목록'을 반환하는 코드는
기존에 없어(audio_qa 는 집계만, silence_cutter 는 전사 gap 기반 — 2026-08-31 실측)
여기서 만든다 — 임계는 audio_qa 의 noise(-30dB)와 cue 공백 규칙의 0.5s 를 그대로
물려받는다(새 숫자 발명 금지 원칙).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

from app.modules.audio_qa import AUDIO_QA_SILENCE_NOISE_DB
from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.stt_elevenlabs import _CUE_GAP_SEC

SAMPLE_RATE = 16_000
SILENCE_NOISE_DB = AUDIO_QA_SILENCE_NOISE_DB      # -30.0 — audio_qa(E19-8)와 같은 자
SILENCE_MIN_SEC = _CUE_GAP_SEC                    # 0.5 — cue 공백 규칙과 같은 자

_SIL_START = re.compile(r"silence_start:\s*([0-9.+\-eE]+)")
_SIL_END = re.compile(r"silence_end:\s*([0-9.+\-eE]+)")


def load_pcm(audio_path: Path) -> np.ndarray:
    """오디오(wav/영상 무엇이든) → 16kHz 모노 float32 [-1, 1] 파형.

    ffmpeg s16le 파이프 — soundfile 같은 의존을 더하지 않는다(레포에 없다)."""
    ffmpeg = find_ffmpeg_command("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(audio_path),
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def parse_silence_log(stderr: str, duration_sec: float) -> list[tuple[float, float]]:
    """silencedetect 로그 → [(t0, t1), …]. 짝 없는 꼬리 silence_start 는 duration 으로
    닫는다(파일 끝까지 무음). 순수 — 테스트 대상."""
    starts = [float(m) for m in _SIL_START.findall(stderr)]
    ends = [float(m) for m in _SIL_END.findall(stderr)]
    out: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration_sec
        if e > s >= 0:
            out.append((round(s, 3), round(min(e, duration_sec), 3)))
    return out


def detect_silence_intervals(audio_path: Path, duration_sec: float, *,
                             noise_db: float = SILENCE_NOISE_DB,
                             min_sec: float = SILENCE_MIN_SEC) -> list[tuple[float, float]]:
    """ffmpeg silencedetect 1회 디코드 → 무음 구간 목록(초, 오름차순)."""
    ffmpeg = find_ffmpeg_command("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-v", "info", "-i", str(audio_path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_sec}", "-f", "null", "-"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        # 조용한 빈 목록은 '무음 0건'과 구분이 안 된다 — 격자 재료는 크게 실패한다.
        raise RuntimeError(f"silencedetect 실패(rc={proc.returncode}): "
                           f"{proc.stderr[-300:]}")
    return parse_silence_log(proc.stderr, duration_sec)
