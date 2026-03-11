# 코드의 핵심 역할
# 렌더링된 영상 파일(.mp4)을 다시 읽어 들여, 길이, 오디오 레벨, 화면 깨짐(블랙 프레임) 등 세 가지 관점에서 영상의 품질을 검증합니다.
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


@dataclass(frozen=True)
class ValidationResult:
    duration_ok: bool
    audio_peak_ok: bool
    black_frames_ok: bool


def validate_output(video_path: Path, min_sec: float, max_sec: float) -> ValidationResult:
    duration_ok = _check_duration(video_path, min_sec, max_sec)
    audio_peak_ok = _check_audio_peak(video_path)
    black_frames_ok = _check_black_frames(video_path)
    return ValidationResult(
        duration_ok=duration_ok,
        audio_peak_ok=audio_peak_ok,
        black_frames_ok=black_frames_ok,
    )


# 길이 검증
# 목적: 쇼츠 플랫폼(YouTube Shorts, TikTok 등)의 규격에 맞는지 확인합니다.
# 방법: ffprobe를 사용해 영상의 정확한 재생 시간을 추출하고, 설정된 최소/최대 시간 범위 내에 있는지 판단합니다.
def _check_duration(video_path: Path, min_sec: float, max_sec: float) -> bool:
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    output = subprocess.check_output(cmd).decode("utf-8").strip()
    duration = float(output)
    return min_sec <= duration <= max_sec

# 오디오 피크 검증 (_check_audio_peak)
# 목적: 소리가 너무 커서 깨지는 '클리핑' 현상이 있는지 확인합니다.
# 방법: FFmpeg의 volumedetect 필터를 사용합니다. max_volume이 -1.0 dB 이하인지를 체크하여, 소리가 최대치에 너무 근접해 왜곡될 위험이 없는지 검사합니다.
def _check_audio_peak(video_path: Path) -> bool:
    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-i",
        str(video_path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8")
    for line in output.splitlines():
        if "max_volume" in line:
            value = float(line.split(":")[-1].replace(" dB", "").strip())
            return value <= -1.0
    return True

# 블랙 프레임 검증 (_check_black_frames)
# 목적: 렌더링 오류로 인해 화면이 검게 나오는 구간이 있는지 확인합니다.
# 방법: blackdetect 필터를 사용합니다. 0.2초 이상의 검은 화면(d=0.2)이 감지되면 불합격 처리를 합니다. 이는 자막이나 TTS는 나오는데 영상만 안 나오는 치명적인 오류를 잡아내기 위함입니다.
def _check_black_frames(video_path: Path) -> bool:
    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    # 경로에 공백이나 특수문자가 있을 경우를 대비해 절대 경로 및 문자열 변환 확실히 처리
    video_str = str(video_path.resolve())
    
    cmd = [
        ffmpeg_cmd,
        "-v", "error",            # 에러 메시지만 출력해서 가독성 확보
        "-i", video_str,
        "-vf", "blackdetect=d=0.2:pix_th=0.1",
        "-an",
        "-f", "null",
        "-"
    ]
    
    try:
        # 에러 발생 시 로그를 확인하기 위해 stderr를 통합하고 decode 에러 방지 처리
        process = subprocess.run(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True, 
            encoding="utf-8",
            errors="replace", # 혹시 모를 인코딩 깨짐 방지
            check=True
        )
        output = process.stdout
        # "black_start"가 출력에 포함되어 있다면 검은 화면이 감지된 것임
        return "black_start" not in output

    except subprocess.CalledProcessError as e:
        # 여기서 에러가 난다면 영상 파일 자체가 손상되었거나 FFmpeg이 접근을 못하는 상태입니다.
        print(f"\n[!] 검증 프로세스 실행 실패: {e}")
        print(f"[!] FFmpeg 출력 로그:\n{e.output}")
        # 파일이 실제로 존재하는지 한 번 더 확인
        if not video_path.exists():
            print(f"[!] 원인: {video_path} 파일이 존재하지 않습니다.")
        return False # 검증할 수 없으므로 안전하게 실패(False) 처리
