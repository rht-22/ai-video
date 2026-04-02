from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


@dataclass(frozen=True)
class SpeechSegment:
    start_sec: float
    end_sec: float
    text: str


def extract_transcript(audio_path: Path) -> list[SpeechSegment]:
    if _has_faster_whisper():
        return _extract_with_faster_whisper(audio_path)
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
        temp_dir = Path(tempfile.gettempdir())
        output_path = temp_dir / f"audio_{video_path.stem}.wav"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",  # Whisper에 적합한 샘플레이트
        "-ac", "1",      # 모노 채널
        str(output_path),
    ]

    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return output_path


def _has_faster_whisper() -> bool:
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def _extract_with_faster_whisper(audio_path: Path) -> list[SpeechSegment]:
    """faster-whisper로 음성을 텍스트로 변환합니다.

    기존 openai-whisper 대비:
    - CPU에서 약 4배 빠름
    - 동일 모델 크기 대비 메모리 사용량 절반
    - int8 양자화로 CPU 최적화
    """
    from faster_whisper import WhisperModel

    print("  [Whisper] 모델 로드 중...")
    model = WhisperModel(
        "base",
        device="cpu",
        compute_type="int8",  # CPU에서 속도/정확도 균형을 위한 int8 양자화
    )

    print("  [Whisper] 전사 시작...")
    segments_generator, info = model.transcribe(
    str(audio_path),
    # language 제거 → 자동 감지
    initial_prompt="한국어 드라마, 예능 프로그램입니다. 한국어와 외국어가 혼용될 수 있습니다.",
    no_speech_threshold=0.6,
    temperature=0.0,
    condition_on_previous_text=False,
    vad_filter=True,
    vad_parameters=dict(
        min_silence_duration_ms=500,
    ),
)

    print(f"  [Whisper] 감지된 언어: {info.language} (확률: {info.language_probability:.2f})")

    segments = []
    for seg in segments_generator:
        text = seg.text.strip()
        if not text:
            continue
        segments.append(
            SpeechSegment(
                start_sec=float(seg.start),
                end_sec=float(seg.end),
                text=text,
            )
        )

    print(f"  [Whisper] 전사 완료: {len(segments)}개 세그먼트")
    return segments