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


def extract_transcript(
    audio_path: Path,
    *,
    work_title: str | None = None,
    character_names: list[str] | None = None,
    work_context: str | None = None,
) -> list[SpeechSegment]:
    if _has_faster_whisper():
        return _extract_with_faster_whisper(
            audio_path,
            work_title=work_title,
            character_names=character_names,
            work_context=work_context,
        )
    return []


def extract_vad_segments(audio_path: Path) -> list[SpeechSegment]:
    return []


def extract_audio_segment(
    video_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    padding_sec: float = 2.0,
) -> tuple[Path, float]:
    """영상에서 특정 구간의 오디오만 추출합니다.

    Whisper 정확도를 위해 앞뒤 padding_sec만큼 여유를 두고 추출합니다.

    Args:
        video_path: 입력 영상 파일 경로
        output_path: 출력 오디오 파일 경로
        start_sec: 추출 시작 시간(초)
        end_sec: 추출 종료 시간(초)
        padding_sec: Whisper 컨텍스트 확보용 앞뒤 패딩(초)

    Returns:
        (추출된 오디오 경로, 실제 추출 시작 시간) 튜플.
        실제 시작 시간은 패딩이 적용된 값이며 전사 결과 오프셋 보정에 사용.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    actual_start = max(0.0, start_sec - padding_sec)
    duration = (end_sec + padding_sec) - actual_start

    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd, "-y",
        "-ss", str(actual_start),
        "-i", str(video_path),
        "-t", str(duration),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(output_path),
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path, actual_start


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


# ── Whisper 모델 캐싱 (클립마다 재로드 방지) ──
_cached_model = None
_cached_model_key: str | None = None


def _detect_device_and_compute() -> tuple[str, str]:
    """GPU 가용성을 확인하고 최적 device/compute_type을 반환."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", "float16"
    except ImportError:
        pass
    return "cpu", "int8"


def _get_whisper_model(model_name: str, device: str, compute_type: str):
    """Whisper 모델을 캐싱하여 반환 (동일 설정이면 재사용)."""
    global _cached_model, _cached_model_key
    key = f"{model_name}:{device}:{compute_type}"
    if _cached_model_key != key:
        from faster_whisper import WhisperModel
        print(f"  [Whisper] 모델 로드 중... ({model_name}, {device}, {compute_type})")
        _cached_model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _cached_model_key = key
    return _cached_model


def _build_whisper_prompt(
    work_title: str | None = None,
    character_names: list[str] | None = None,
    work_context: str | None = None,
) -> str:
    """리서치 결과를 반영한 Whisper initial_prompt를 생성."""
    parts = ["한국어 드라마, 예능 프로그램입니다."]
    if work_title:
        parts.append(f"작품명: {work_title}.")
    if character_names:
        # Whisper initial_prompt ~224 토큰 제한 → 인물명 15명으로 제한
        names_str = ", ".join(character_names[:15])
        parts.append(f"등장인물: {names_str}.")
    if work_context:
        # 시놉시스는 100자만 (너무 길면 역효과)
        parts.append(f"내용: {work_context[:100].rstrip()}")
    parts.append("한국어와 외국어가 혼용될 수 있습니다.")
    return " ".join(parts)


def _extract_with_faster_whisper(
    audio_path: Path,
    *,
    work_title: str | None = None,
    character_names: list[str] | None = None,
    work_context: str | None = None,
) -> list[SpeechSegment]:
    """faster-whisper large-v3-turbo로 음성을 텍스트로 변환합니다.

    - large-v3-turbo: base 대비 한국어 정확도 대폭 향상
    - 리서치 결과(인물명, 작품명)를 initial_prompt에 주입하여 고유명사 인식 강화
    - 모델 캐싱으로 다중 클립 전사 시 재로드 방지
    - GPU 자동 감지 (CUDA 가용 시 float16 사용)
    """
    device, compute_type = _detect_device_and_compute()
    model = _get_whisper_model("large-v3-turbo", device, compute_type)

    initial_prompt = _build_whisper_prompt(
        work_title=work_title,
        character_names=character_names,
        work_context=work_context,
    )
    print(f"  [Whisper] 프롬프트: {initial_prompt[:80]}...")

    print("  [Whisper] 전사 시작...")
    segments_generator, info = model.transcribe(
        str(audio_path),
        language="ko",
        initial_prompt=initial_prompt,
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