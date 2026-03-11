from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.modules.ffmpeg_utils import find_ffmpeg_command


@dataclass(frozen=True)
class SpeechSegment:
    start_sec: float
    end_sec: float
    text: str


def extract_transcript(audio_path: Path) -> list[SpeechSegment]:
    if _has_whisper():
        return _extract_with_whisper(audio_path)
    return []


def extract_vad_segments(audio_path: Path) -> list[SpeechSegment]:
    return []


def extract_audio_from_video(video_path: Path, output_path: Path | None = None) -> Path:
    """영상에서 오디오를 추출합니다.
    
    Args:
        video_path: 입력 영상 파일 경로
        output_path: 출력 오디오 파일 경로 (None이면 시스템 임시 디렉토리에 생성)
    
    Returns:
        추출된 오디오 파일 경로
    """
    if output_path is None:
        # 시스템 임시 디렉토리에 파일 생성
        temp_dir = Path(tempfile.gettempdir())
        output_path = temp_dir / f"audio_{video_path.stem}.wav"
    
    # 출력 디렉토리 생성
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # ffmpeg 명령 구성
    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-y",  # 기존 파일 덮어쓰기
        "-i", str(video_path),  # 입력 파일
        "-vn",  # 비디오 스트림 제외
        "-acodec", "pcm_s16le",  # PCM 16-bit 오디오 코덱
        "-ar", "16000",  # 샘플 레이트 16kHz (Whisper에 적합)
        "-ac", "1",  # 모노 채널
        str(output_path),
    ]
    
    # ffmpeg 실행
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    return output_path


def _has_whisper() -> bool:
    import importlib.util

    return importlib.util.find_spec("whisper") is not None


def _extract_with_whisper(audio_path: Path) -> list[SpeechSegment]:
    import os
    import warnings
    from subprocess import run
    
    # Whisper가 ffmpeg를 찾을 수 있도록 PATH에 추가 (import 전에 실행)
    try:
        ffmpeg_path = find_ffmpeg_command("ffmpeg")
        ffmpeg_dir = str(Path(ffmpeg_path).parent)
        
        # 현재 PATH에 ffmpeg 디렉토리가 없으면 추가
        current_path = os.environ.get("PATH", "")
        if ffmpeg_dir not in current_path:
            os.environ["PATH"] = ffmpeg_dir + os.pathsep + current_path
    except FileNotFoundError:
        # ffmpeg를 찾지 못하면 에러 발생
        raise RuntimeError(
            "ffmpeg를 찾을 수 없습니다. Whisper가 오디오를 로드하기 위해 필요합니다."
        )
    
    # PATH 설정 후 Whisper 모듈 import
    import whisper
    import whisper.audio as whisper_audio
    import numpy as np
    
    # Whisper의 load_audio 함수를 패치하여 우리가 찾은 ffmpeg 경로 사용
    original_load_audio = whisper_audio.load_audio
    
    def patched_load_audio(file: str | Path, sr: int = 16000):
        # 원본 함수의 로직을 유지하되, ffmpeg 경로를 직접 사용
        cmd = [
            ffmpeg_path,
            "-nostdin",
            "-threads", "0",
            "-i", str(file),
            "-f", "s16le",
            "-ac", "1",
            "-acodec", "pcm_s16le",
            "-ar", str(sr),
            "-"
        ]
        out = run(cmd, capture_output=True, check=True).stdout
        return np.frombuffer(out, np.int16).flatten().astype(np.float32) / 32768.0
    
    # load_audio 함수를 패치
    whisper_audio.load_audio = patched_load_audio
    
    # FP16 경고 필터링 (CPU에서는 FP32를 사용하므로 경고가 불필요함)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="FP16 is not supported on CPU")
        # 명시적으로 CPU를 사용하여 FP32로 실행
        model = whisper.load_model("base", device="cpu")
        result = model.transcribe(str(audio_path))
    
    segments = []
    for seg in result.get("segments", []):
        segments.append(
            SpeechSegment(
                start_sec=float(seg["start"]),
                end_sec=float(seg["end"]),
                text=seg["text"].strip(),
            )
        )
    return segments