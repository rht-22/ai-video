from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_sec: float
    fps: float
    width: int
    height: int
    has_audio: bool


def probe_media(video_path: Path) -> MediaInfo:
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    raw = subprocess.check_output(cmd)
    payload = json.loads(raw)
    stream = payload["streams"][0]
    duration_sec = float(payload["format"]["duration"])
    width = int(stream["width"])
    height = int(stream["height"])
    fps = _parse_fps(stream.get("r_frame_rate", "30/1"))
    has_audio = _has_audio(video_path)
    return MediaInfo(
        path=video_path,
        duration_sec=duration_sec,
        fps=fps,
        width=width,
        height=height,
        has_audio=has_audio,
    )


def _parse_fps(rate: str) -> float:
    parts = rate.split("/")
    if len(parts) != 2:
        return 30.0
    num = float(parts[0])
    den = float(parts[1])
    if den == 0:
        return 30.0
    return num / den


def _has_audio(video_path: Path) -> bool:
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(video_path),
    ]
    raw = subprocess.check_output(cmd)
    payload = json.loads(raw)
    return bool(payload.get("streams"))
