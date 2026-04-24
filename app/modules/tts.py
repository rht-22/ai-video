from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command

VOICE = "ko-KR-SunHiNeural"


def synthesize_tts(
    text: str,
    output_path: Path,
    lang: str = "ko",
) -> Path:
    if _has_edge_tts():
        return _synthesize_edge_tts(text, output_path)
    return _synthesize_silence(output_path, duration_sec=1.0)


def _has_edge_tts() -> bool:
    import importlib.util

    return importlib.util.find_spec("edge_tts") is not None


def _synthesize_edge_tts(text: str, output_path: Path) -> Path:
    import edge_tts

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, VOICE, rate="+0%", pitch="+0Hz")
        await communicate.save(str(output_path))

    try:
        asyncio.run(_run())
    except Exception:
        # 폴백: 프리셋 없이 재시도
        async def _run_plain() -> None:
            communicate = edge_tts.Communicate(text, VOICE)
            await communicate.save(str(output_path))
        asyncio.run(_run_plain())
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
