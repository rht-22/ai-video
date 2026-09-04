"""장면 전환 컷 시각 — ffmpeg scdet 검출(v3·v4 공용 격자 재료).

⚠ 기존 `scene_detect.detect_scenes` 를 그대로 재사용하지 **못한** 사유(2026-08-31 실측):
pyscenedetect 경로는 패키지가 requirements 에 없어 **어느 운영 환경에도 설치돼 있지
않고**, 그때의 폴백 `_detect_with_histogram` 은 픽셀을 전혀 안 보는 등간격 산술이다 —
격자의 눈금(스냅 정본)으로 쓰면 '장면 전환에 스냅'이 거짓말이 된다. 그래서 v3 는
ffmpeg `select='gt(scene,T)'` 하나로 고정한다(의존 추가 0 · 전 환경 동일 산출 =
결정성 합격 기준). pyscenedetect 가 설치된 환경이 생겨도 이 경로를 그대로 쓴다 —
환경에 따라 눈금이 달라지면 같은 소재의 grid 가 노드마다 갈린다.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command

SCENE_THRESHOLD = 0.3        # ffmpeg scene score 관례 기본(0.3~0.4). 낮출수록 민감.
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def parse_showinfo_times(stderr: str) -> list[float]:
    """showinfo 로그 → pts_time 목록(초, 오름차순·중복 제거). 순수 — 테스트 대상."""
    seen: list[float] = []
    for m in _PTS_RE.findall(stderr):
        t = round(float(m), 3)
        if not seen or t > seen[-1]:
            seen.append(t)
    return seen


def detect_scene_cuts(video_path: Path, *,
                      threshold: float = SCENE_THRESHOLD) -> list[float]:
    """영상(프록시 권장) → 장면 전환 시각 목록(초).

    프레임 전체를 한 번 디코드한다 — 480p/4fps 프록시 기준 67분 소재 수십 초 급."""
    ffmpeg = find_ffmpeg_command("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-v", "info", "-i", str(video_path),
         "-vf", f"select='gt(scene,{threshold})',showinfo",
         "-fps_mode", "passthrough", "-f", "null", "-"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"scene 검출 실패: {proc.stderr[-300:]}")
    return parse_showinfo_times(proc.stderr)
