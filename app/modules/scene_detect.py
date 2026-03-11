from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Scene:
    start_sec: float
    end_sec: float


def detect_scenes(video_path: Path, fps: float, duration_sec: float) -> list[Scene]:
    pyscenedetect = _has_pyscenedetect()
    if pyscenedetect:
        return _detect_with_pyscenedetect(video_path)
    return _detect_with_histogram(video_path, fps, duration_sec)


def _has_pyscenedetect() -> bool:
    import importlib.util

    return importlib.util.find_spec("scenedetect") is not None


def _detect_with_pyscenedetect(video_path: Path) -> list[Scene]:
    cmd = [
        "scenedetect",
        "-i",
        str(video_path),
        "detect-content",
        "list-scenes",
        "-o",
        "-",
    ]
    raw = subprocess.check_output(cmd)
    lines = raw.decode("utf-8").splitlines()
    scenes: list[Scene] = []
    for line in lines:
        if not line or line.startswith("Scene"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        start = _timecode_to_seconds(parts[1])
        end = _timecode_to_seconds(parts[2])
        scenes.append(Scene(start_sec=start, end_sec=end))
    return scenes


def _detect_with_histogram(video_path: Path, fps: float, duration_sec: float) -> list[Scene]:
    if duration_sec <= 0:
        return []
    scene_length = max(3.0, min(8.0, duration_sec / 12.0))
    boundaries = [0.0]
    cursor = scene_length
    while cursor < duration_sec:
        boundaries.append(cursor)
        cursor += scene_length
    boundaries.append(duration_sec)
    return _boundaries_to_scenes(boundaries)


def _boundaries_to_scenes(boundaries: Iterable[float]) -> list[Scene]:
    points = list(boundaries)
    scenes = []
    for idx in range(len(points) - 1):
        scenes.append(Scene(start_sec=points[idx], end_sec=points[idx + 1]))
    return scenes


def _timecode_to_seconds(timecode: str) -> float:
    parts = timecode.split(":")
    if len(parts) != 3:
        return 0.0
    hours, minutes, seconds = parts
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)