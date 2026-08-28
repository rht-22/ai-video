"""E19-8 렌더 후 오디오 QA 지표 — **렌더를 바꾸지 않는다, 재기만 한다.**

발주서: `docs/prompts/e19-drama-clip-preset.md` §8. 벤치마크 실측(신병4 −13.4 LUFS ·
LRA 3.6 · 무음 2건, 꿀벌무비 −12.3 LUFS · 무음 0건) 대비 산출물이 어디쯤인지 편마다
run_log(render 단계 `audio_qa`)에 남아야 프리셋 튜닝이 데이터로 돈다.

- ffmpeg **한 번**(ebur128 → silencedetect 체인 — 디코드 1회)으로 통합 LUFS·LRA·
  0.3s+ 무음 수·무음 합계를 잰다. 임계는 벤치마크 분석과 같은 자(−30dB · 0.3s).
- 실패해도 편을 막지 않는다 — 호출부가 `audio_qa_error` 로 사유만 남긴다.
- 목표값(−13.5 LUFS · 무음 ≤2 등) 판정은 엔진이 하지 않는다 — ves evaluate 가 읽고
  대시보드에 띄우는 것은 별건이다.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from app.modules.ffmpeg_utils import find_ffmpeg_command

# 벤치마크 분석(보고서 §04·§11)과 같은 자 — 바꾸면 지금까지의 실측 수치와 비교가 안 된다.
AUDIO_QA_SILENCE_NOISE_DB = -30.0
AUDIO_QA_SILENCE_MIN_SEC = 0.3
_QA_TIMEOUT_SEC = 120

_I_RE = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.M)
_LRA_RE = re.compile(r"^\s*LRA:\s*(-?[\d.]+)\s*LU", re.M)
_SIL_DUR_RE = re.compile(r"silence_duration:\s*([\d.]+)")


def parse_audio_qa(stderr_text: str) -> dict[str, Any]:
    """ffmpeg stderr → 지표 dict. 순수(테스트 대상). 못 찾은 값은 None/0 — 파싱이
    반쯤 실패해도 잰 것은 남긴다."""
    i_m = _I_RE.search(stderr_text or "")
    lra_m = _LRA_RE.search(stderr_text or "")
    durs = [float(d) for d in _SIL_DUR_RE.findall(stderr_text or "")]
    return {
        "lufs_i": float(i_m.group(1)) if i_m else None,
        "lra": float(lra_m.group(1)) if lra_m else None,
        "silence_count": len(durs),
        "silence_total_sec": round(sum(durs), 3),
    }


def measure_audio_qa(video_path: Path) -> dict[str, Any]:
    """산출물 오디오를 한 번 디코드해 지표를 잰다. 실패는 {"error": 사유} — 예외를
    올리지 않는다(관측 장치가 본편 발행을 막으면 안 된다)."""
    try:
        ffmpeg = find_ffmpeg_command("ffmpeg")
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", str(video_path),
             "-vn", "-af",
             f"ebur128,silencedetect=noise={AUDIO_QA_SILENCE_NOISE_DB:g}dB"
             f":d={AUDIO_QA_SILENCE_MIN_SEC:g}",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=_QA_TIMEOUT_SEC)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["?"]
            return {"error": f"ffmpeg rc={proc.returncode}: {tail[0][:200]}"}
        return parse_audio_qa(proc.stderr or "")
    except Exception as e:                     # noqa: BLE001 — 관측 실패는 사유만 남긴다
        return {"error": f"{type(e).__name__}: {e}"}
