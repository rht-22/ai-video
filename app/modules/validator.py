from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


@dataclass(frozen=True)
class ValidationResult:
    duration_ok: bool
    audio_peak_ok: bool
    black_frames_ok: bool


def validate_output(video_path: Path, min_sec: float, max_sec: float) -> ValidationResult:
    duration_ok = _check_duration(video_path, min_sec, max_sec)
    audio_peak_ok = _check_audio_peak(video_path)
    black_frames_ok = _check_black_frames(video_path)
    return ValidationResult(
        duration_ok=duration_ok,
        audio_peak_ok=audio_peak_ok,
        black_frames_ok=black_frames_ok,
    )


def _check_duration(video_path: Path, min_sec: float, max_sec: float) -> bool:
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    output = subprocess.check_output(cmd).decode("utf-8").strip()
    duration = float(output)
    return min_sec <= duration <= max_sec


def _check_audio_peak(video_path: Path) -> bool:
    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-i",
        str(video_path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8")
    for line in output.splitlines():
        if "max_volume" in line:
            value = float(line.split(":")[-1].replace(" dB", "").strip())
            return value <= -1.0
    return True


def _check_black_frames(video_path: Path) -> bool:
    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-i",
        str(video_path),
        "-vf",
        "blackdetect=d=0.2:pix_th=0.1",
        "-an",
        "-f",
        "null",
        "-",
    ]
    output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8")
    return "black_start" not in output
