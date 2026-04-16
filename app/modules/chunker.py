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


def build_chunks(
    video_path: Path,
    duration_sec: float,
    chunk_seconds: int,
    overlap_sec: int,
    content_start_sec: float = 0.0,
    content_end_sec: float | None = None,
) -> list[Chunk]:
    """영상을 청크로 분할합니다.

    Args:
        content_start_sec: 콘텐츠 시작 시간 (인트로 이후). 기본 0.0.
        content_end_sec: 콘텐츠 종료 시간 (크레딧 이전). None이면 duration_sec.
    """
    if content_end_sec is None:
        content_end_sec = duration_sec
    effective_duration = content_end_sec - content_start_sec

    if effective_duration <= chunk_seconds:
        return [Chunk(index=0, start_sec=content_start_sec, end_sec=content_end_sec, path=video_path)]

    chunks = []
    index = 0
    start = content_start_sec
    while start < content_end_sec:
        end = min(start + chunk_seconds, content_end_sec)
        chunks.append(
            Chunk(
                index=index,
                start_sec=start,
                end_sec=end,
                path=video_path,
            )
        )
        if end >= content_end_sec:
            break
        index += 1
        start = end - overlap_sec
    return chunks


def get_actual_start_time(video_path: Path) -> float:
    """ffprobe로 영상의 실제 시작 타임스탬프를 읽습니다.

    format=start_time이 0을 반환하면 첫 번째 패킷의 PTS로 재시도합니다.
    (MP4 -c copy 청크에서 컨테이너 start_time이 0으로 기록되는 경우 대비)
    """
    ffprobe_cmd = find_ffmpeg_command("ffprobe")

    # 1차: 컨테이너 start_time
    cmd = [
        ffprobe_cmd,
        "-v", "quiet",
        "-show_entries", "format=start_time",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        val = float(result.stdout.strip())
        if val > 0:
            return val
    except (ValueError, AttributeError):
        pass

    # 2차: 첫 번째 비디오 패킷의 PTS
    cmd2 = [
        ffprobe_cmd,
        "-v", "quiet",
        "-read_intervals", "%+#1",
        "-show_entries", "packet=pts_time",
        "-select_streams", "v:0",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result2 = subprocess.run(cmd2, capture_output=True, text=True)
    try:
        return float(result2.stdout.strip())
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
        # -avoid_negative_ts make_zero 제거: 원본 PTS를 보존해야 ffprobe로 실제 시작 시간을 정확히 읽을 수 있음
        str(output_path),
    ]
    
    # ffmpeg 실행
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 실제로 잘린 시작 시간 확인 (키프레임 정렬로 인해 start_sec와 다를 수 있음)
    actual_start = get_actual_start_time(output_path)

    return output_path, actual_start
