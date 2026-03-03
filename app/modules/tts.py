from __future__ import annotations

import subprocess
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


def synthesize_tts(text: str, output_path: Path, lang: str = "ko") -> Path:
    if _has_gtts():
        return _synthesize_gtts(text, output_path, lang)
    return _synthesize_silence(output_path, duration_sec=1.0)


def _has_gtts() -> bool:
    import importlib.util

    return importlib.util.find_spec("gtts") is not None


def _synthesize_gtts(text: str, output_path: Path, lang: str) -> Path:
    from gtts import gTTS

    tts = gTTS(text=text, lang=lang)
    tts.save(str(output_path))
    return output_path


def _synthesize_silence(output_path: Path, duration_sec: float) -> Path:
    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc=0:d={duration_sec}",
        str(output_path),
    ]
    subprocess.check_call(cmd)
    return output_path
