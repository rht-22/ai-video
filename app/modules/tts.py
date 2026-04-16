from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command

VOICE = "ko-KR-SunHiNeural"

# 편집 포커스별 TTS 프리셋 (edge-tts rate/pitch)
_FOCUS_TTS_PRESETS: dict[str, dict[str, str]] = {
    "event":     {"rate": "+15%", "pitch": "+2Hz"},   # 사건 중심 — 긴박, 빠름
    "character": {"rate": "+0%",  "pitch": "+0Hz"},   # 인물 중심 — 중립
    "emotion":   {"rate": "-15%", "pitch": "-2Hz"},   # 감정 중심 — 느림
    "narrative": {"rate": "+5%",  "pitch": "+0Hz"},   # 서사 중심 — 정보 전달
    "reaction":  {"rate": "+20%", "pitch": "+4Hz"},   # 리액션 — 활기
    "auto":      {"rate": "+0%",  "pitch": "+0Hz"},
}


def synthesize_tts(
    text: str,
    output_path: Path,
    lang: str = "ko",
    editing_focus: str = "auto",
) -> Path:
    if _has_edge_tts():
        return _synthesize_edge_tts(text, output_path, editing_focus=editing_focus)
    return _synthesize_silence(output_path, duration_sec=1.0)


def _has_edge_tts() -> bool:
    import importlib.util

    return importlib.util.find_spec("edge_tts") is not None


def _synthesize_edge_tts(text: str, output_path: Path, editing_focus: str = "auto") -> Path:
    import edge_tts

    preset = _FOCUS_TTS_PRESETS.get(editing_focus or "auto", _FOCUS_TTS_PRESETS["auto"])
    rate = preset.get("rate", "+0%")
    pitch = preset.get("pitch", "+0Hz")

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, VOICE, rate=rate, pitch=pitch)
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
