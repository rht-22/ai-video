"""E19-8 렌더 후 오디오 QA 지표 회귀 가드.

발주서: docs/prompts/e19-drama-clip-preset.md §8. **렌더를 바꾸지 않는다 — 재기만
한다.** 벤치마크 대비 산출물이 어디쯤인지(−13.5 LUFS 타깃·0.3s+ 무음 ≤2건) 편마다
run_log 에 남아야 프리셋 튜닝이 데이터로 돈다. 계약 요점:

- render 뒤 ffmpeg 한 번(ebur128 + silencedetect 체인 — 디코드 1회)으로
  통합 LUFS·LRA·0.3s+ 무음 수를 잰다.
- 실패해도 편을 막지 않는다 — `audio_qa_error` 로 사유만 남긴다.
- 목표값 판정은 엔진이 하지 않는다(ves evaluate 몫).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.modules.audio_qa import (
    AUDIO_QA_SILENCE_MIN_SEC,
    AUDIO_QA_SILENCE_NOISE_DB,
    measure_audio_qa,
    parse_audio_qa,
)
from app.modules.ffmpeg_utils import find_ffmpeg_command

REPO = Path(__file__).resolve().parents[1]

_SAMPLE_STDERR = """
[silencedetect @ 0x1] silence_start: 1.0
[silencedetect @ 0x1] silence_end: 1.6 | silence_duration: 0.6
[silencedetect @ 0x1] silence_start: 3.0
[silencedetect @ 0x1] silence_end: 3.4 | silence_duration: 0.4
[Parsed_ebur128_0 @ 0x2] Summary:

  Integrated loudness:
    I:         -13.4 LUFS
    Threshold: -23.5 LUFS

  Loudness range:
    LRA:         3.6 LU
    Threshold: -33.5 LUFS
    LRA low:   -15.6 LUFS
    LRA high:  -12.0 LUFS
"""


# ══════════════════════════════════════════════════════════════════════════
# 파서 — 순수
# ══════════════════════════════════════════════════════════════════════════
def test_parse_sample_output():
    qa = parse_audio_qa(_SAMPLE_STDERR)
    assert qa == {"lufs_i": -13.4, "lra": 3.6,
                  "silence_count": 2, "silence_total_sec": 1.0}


def test_parse_garbage_returns_nones():
    qa = parse_audio_qa("no useful lines here")
    assert qa["lufs_i"] is None and qa["lra"] is None
    assert qa["silence_count"] == 0 and qa["silence_total_sec"] == 0.0


# ══════════════════════════════════════════════════════════════════════════
# 실측 — 실제 ffmpeg (합성 소스: 1초 사인 + 1초 무음)
# ══════════════════════════════════════════════════════════════════════════
def test_measure_real_ffmpeg(tmp_path):
    ffmpeg = find_ffmpeg_command("ffmpeg")
    wav = tmp_path / "probe.wav"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi",
         "-i", "aevalsrc=if(lt(t\\,1)\\,0.5*sin(880*2*PI*t)\\,0):d=2",
         str(wav)], capture_output=True, check=True)
    qa = measure_audio_qa(wav)
    assert "error" not in qa
    assert isinstance(qa["lufs_i"], float)
    assert qa["silence_count"] >= 1                       # 뒤 1초 무음이 잡힌다
    assert qa["silence_total_sec"] >= 0.5


def test_measure_missing_file_returns_error_not_raise(tmp_path):
    qa = measure_audio_qa(tmp_path / "no_such.mp4")
    assert "error" in qa                                  # 편을 막지 않는다 — 사유만


def test_thresholds_are_the_benchmark_ones():
    assert AUDIO_QA_SILENCE_NOISE_DB == -30.0             # 벤치마크 실측과 같은 자
    assert AUDIO_QA_SILENCE_MIN_SEC == 0.3


# ══════════════════════════════════════════════════════════════════════════
# 배선 — render 단계에 기록, 실패는 audio_qa_error
# ══════════════════════════════════════════════════════════════════════════
def test_pipeline_wiring():
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert "measure_audio_qa(" in src
    assert '"audio_qa"' in src and '"audio_qa_error"' in src
    # render 단계 dict 에 붙는다 — 별도 step 이 아니라 발주서 §8 의 자리 그대로
    site = src.index("measure_audio_qa(")
    assert '"step": "render"' in src[site - 1500:site + 1500]
