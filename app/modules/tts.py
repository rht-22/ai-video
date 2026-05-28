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


def get_audio_duration(path: Path) -> float:
    """ffprobe로 mp3/audio 파일의 재생 시간(초)을 반환."""
    try:
        ffprobe = find_ffmpeg_command("ffprobe")
    except Exception:
        return 0.0
    cmd = [
        ffprobe, "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except (ValueError, AttributeError, subprocess.SubprocessError):
        return 0.0


def synthesize_tts_with_fit(
    text: str,
    output_path: Path,
    target_sec: float,
    *,
    voice: str = DEFAULT_VOICE,
    speed: str = DEFAULT_SPEED,
    shorten_fn=None,
    max_retries: int = 2,
    safety_margin: float = 0.95,
) -> tuple[str, float]:
    """target_sec 안에 들어가도록 TTS 합성. 초과 시 shorten_fn으로 텍스트를 줄여 재합성.

    shorten_fn: callable(text: str, target_chars: int) -> str
        제공되지 않으면 단순 절단 (의미 손상 위험) 폴백.

    반환: (최종 사용된 텍스트, 실제 합성 길이 초)
    """
    current_text = text
    last_actual = 0.0
    for attempt in range(max_retries + 1):
        synthesize_tts(current_text, output_path, voice=voice, speed=speed)
        actual = get_audio_duration(output_path)
        last_actual = actual
        if actual <= target_sec or actual <= 0.0:
            return current_text, actual
        if attempt == max_retries:
            print(f"  [TTS-fit-WARN] {len(current_text)}자 → {actual:.1f}s > target {target_sec:.1f}s — 잘림 감수")
            return current_text, actual
        # 줄여야 하는 비율 계산 (안전 마진 5% 추가 단축)
        shrink_ratio = (target_sec / actual) * safety_margin
        target_chars = max(8, int(len(current_text) * shrink_ratio))
        if shorten_fn:
            try:
                shorter = shorten_fn(current_text, target_chars=target_chars)
                shorter = (shorter or "").strip()
                if shorter and shorter != current_text:
                    print(f"  [TTS-fit] 재합성 시도 {attempt + 1}: {len(current_text)}자 → {len(shorter)}자 (target {target_chars}자)")
                    current_text = shorter
                    continue
            except Exception as e:
                print(f"  [TTS-fit] shorten 실패 ({e}) — 단순 절단 폴백")
        # shorten_fn 미제공 또는 실패 → 단순 절단
        current_text = current_text[:target_chars].rstrip("., \t\n") + "."
        print(f"  [TTS-fit] 재합성 시도 {attempt + 1} (단순 절단): {len(current_text)}자")
    return current_text, last_actual


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
