from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


# Voice 프리셋 라벨 → (Edge TTS voice id, pitch).
# 기본은 자연스러운 한국어 voice (ko_*) 4종, 트렌드 톤이 필요할 때만 multilingual (chat_*) 4종 사용.
VOICE_PRESETS: dict[str, tuple[str, str]] = {
    # ── 자연스러운 한국어 (우선 사용) ──
    "ko_female":      ("ko-KR-SunHiNeural",                  "+0Hz"),    # 기본 한국 여성
    "ko_female_high": ("ko-KR-SunHiNeural",                  "+30Hz"),   # 밝은 한국 여성 (트렌드 톤)
    "ko_male":        ("ko-KR-InJoonNeural",                 "+0Hz"),    # 기본 한국 남성
    "ko_male_low":    ("ko-KR-InJoonNeural",                 "-15Hz"),   # 낮은 한국 남성 (묵직)
    # ── 트렌드 multilingual (작품 톤 어울릴 때만) ──
    "chat_emma":      ("en-US-EmmaMultilingualNeural",       "+0Hz"),    # 밝고 명료한 챗봇 여성 (en)
    "chat_brian":     ("en-US-BrianMultilingualNeural",      "+0Hz"),    # 친근한 캐주얼 남성 (en)
    "chat_seraphina": ("de-DE-SeraphinaMultilingualNeural",  "+0Hz"),    # 차분한 유럽계 여성 (de)
    "chat_florian":   ("de-DE-FlorianMultilingualNeural",    "+0Hz"),    # 차분한 유럽계 남성 (de)
}

# 속도 라벨 → Edge TTS rate 문자열.
SPEED_TO_RATE: dict[str, str] = {
    "very_slow": "-25%",
    "slow":      "-10%",
    "normal":    "+0%",
    "fast":      "+10%",
    "very_fast": "+25%",
}

DEFAULT_VOICE = "ko_female"
DEFAULT_SPEED = "normal"


def synthesize_tts(
    text: str,
    output_path: Path,
    lang: str = "ko",
    voice: str = DEFAULT_VOICE,
    speed: str = DEFAULT_SPEED,
) -> Path:
    """텍스트를 mp3로 합성. voice/speed는 프리셋 라벨.

    voice 라벨이 VOICE_PRESETS에 없으면 DEFAULT_VOICE로, speed 라벨이 SPEED_TO_RATE에
    없으면 DEFAULT_SPEED로 폴백한다.
    """
    voice_id, pitch = VOICE_PRESETS.get(voice, VOICE_PRESETS[DEFAULT_VOICE])
    rate = SPEED_TO_RATE.get(speed, SPEED_TO_RATE[DEFAULT_SPEED])

    if _has_edge_tts():
        return _synthesize_edge_tts(text, output_path, voice_id=voice_id, rate=rate, pitch=pitch)
    return _synthesize_silence(output_path, duration_sec=1.0)


def _has_edge_tts() -> bool:
    import importlib.util

    return importlib.util.find_spec("edge_tts") is not None


def _synthesize_edge_tts(
    text: str,
    output_path: Path,
    voice_id: str = "ko-KR-SunHiNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> Path:
    import edge_tts

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch)
        await communicate.save(str(output_path))

    try:
        asyncio.run(_run())
    except Exception:
        # 폴백: rate/pitch 없이 voice만 재시도 (드물게 SSML 충돌 등)
        async def _run_plain() -> None:
            communicate = edge_tts.Communicate(text, voice_id)
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
