from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


@dataclass(frozen=True)
class Chunk:
    index: int
    start_sec: float
    end_sec: float
    path: Path
    split_path: Path | None = None  # 분할된 파일 경로 (optional)
    actual_start_sec: float | None = None  # ffprobe로 확인한 실제 시작 시간


def build_chunks(video_path: Path, duration_sec: float, chunk_seconds: int, overlap_sec: int) -> list[Chunk]:
    if duration_sec <= chunk_seconds:
        return [Chunk(index=0, start_sec=0.0, end_sec=duration_sec, path=video_path)]

    chunks = []
    index = 0
    start = 0.0
    while start < duration_sec:
        end = min(start + chunk_seconds, duration_sec)
        chunks.append(
            Chunk(
                index=index,
                start_sec=start,
                end_sec=end,
                path=video_path,
            )
        )
        if end >= duration_sec:
            break
        index += 1
        start = end - overlap_sec
    return chunks


def get_actual_start_time(video_path: Path) -> float:
    """ffprobe로 영상의 실제 시작 타임스탬프를 읽습니다."""
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd,
        "-v", "quiet",
        "-show_entries", "format=start_time",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def split_video_chunk(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    output_path: Path | None = None,
) -> tuple[Path, float]:
    """ffmpeg를 사용해 영상의 특정 구간을 분할합니다.
    
    Args:
        video_path: 원본 영상 파일 경로
        start_sec: 시작 시간 (초)
        end_sec: 종료 시간 (초)
        output_path: 출력 파일 경로 (None이면 시스템 임시 디렉토리에 생성)
    
    Returns:
        분할된 파일 경로
    """
    if output_path is None:
        # 시스템 임시 디렉토리에 파일 생성
        temp_dir = Path(tempfile.gettempdir())
        output_path = temp_dir / f"chunk_{start_sec:.1f}_{end_sec:.1f}.mp4"
    
    # 출력 디렉토리 생성
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 분할 길이 계산
    duration_sec = end_sec - start_sec
    
    # ffmpeg 명령 구성
    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-y",  # 기존 파일 덮어쓰기
        "-ss", str(start_sec),  # 시작 시간
        "-i", str(video_path),  # 입력 파일
        "-t", str(duration_sec),  # 길이
        "-c", "copy",  # 재인코딩 없이 복사 (빠름)
        "-avoid_negative_ts", "make_zero",  # 타임스탬프 문제 방지
        str(output_path),
    ]
    
    # ffmpeg 실행
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 실제로 잘린 시작 시간 확인 (키프레임 정렬로 인해 start_sec와 다를 수 있음)
    actual_start = get_actual_start_time(output_path)

    return output_path, actual_start
