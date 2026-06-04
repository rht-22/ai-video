from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path


def probe_audio_duration(path: Path) -> float:
    """ffprobe로 오디오/비디오 파일의 재생 시간(초)을 읽는다. 실패 시 0.0.

    tts_place 단계가 합성된 mp3 길이를 조회할 때 사용.
    """
    if not path.exists():
        return 0.0
    try:
        ffprobe = find_ffmpeg_command("ffprobe")
    except FileNotFoundError:
        return 0.0
    cmd = [
        ffprobe, "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def _get_windows_common_paths() -> list[Path]:
    """Windows에서 FFmpeg가 일반적으로 설치되는 경로 목록을 반환합니다."""
    paths = []
    
    # 환경 변수에서 경로 가져오기
    local_appdata = os.getenv("LOCALAPPDATA")
    program_files = os.getenv("ProgramFiles")
    program_files_x86 = os.getenv("ProgramFiles(x86)")
    userprofile = os.getenv("USERPROFILE")
    
    # 일반적인 설치 경로들
    if program_files:
        paths.append(Path(program_files) / "ffmpeg" / "bin")
    if program_files_x86:
        paths.append(Path(program_files_x86) / "ffmpeg" / "bin")
    
    # winget으로 설치된 경우 (Gyan.FFmpeg)
    if local_appdata:
        winget_base = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
        if winget_base.exists():
            # Gyan.FFmpeg 패키지 찾기
            for pkg_dir in winget_base.glob("Gyan.FFmpeg*"):
                # bin 폴더 찾기
                for bin_path in pkg_dir.rglob("bin"):
                    if bin_path.is_dir():
                        paths.append(bin_path)
    
    # 사용자 홈 디렉토리
    if userprofile:
        paths.append(Path(userprofile) / "ffmpeg" / "bin")
        paths.append(Path(userprofile) / "AppData" / "Local" / "ffmpeg" / "bin")
    
    # C 드라이브 루트
    paths.append(Path("C:/ffmpeg/bin"))
    
    return paths


def find_ffmpeg_command(cmd_name: str) -> str:
    """ffmpeg/ffprobe 명령을 찾아서 반환합니다.
    
    Windows에서는 .exe 확장자를 자동으로 추가하고,
    PATH에서 찾지 못하면 일반적인 설치 경로에서도 검색합니다.
    
    Args:
        cmd_name: 찾을 명령 이름 ('ffmpeg' 또는 'ffprobe')
    
    Returns:
        찾은 명령 경로 (문자열)
    
    Raises:
        FileNotFoundError: 명령을 찾을 수 없을 때
    """
    # Windows에서는 .exe 확장자도 시도
    candidates = [cmd_name]
    exe_name = f"{cmd_name}.exe"
    if sys.platform == "win32":
        candidates.append(exe_name)
    
    # 1. PATH에서 찾기
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            # 경로가 실제로 존재하는지 확인
            if os.path.exists(found) and os.access(found, os.X_OK):
                return found
    
    # 2. Windows에서 일반적인 설치 경로에서 찾기
    if sys.platform == "win32":
        for common_path in _get_windows_common_paths():
            exe_path = common_path / exe_name
            if exe_path.exists() and os.access(exe_path, os.X_OK):
                return str(exe_path)
    
    # 찾지 못한 경우 에러 메시지
    error_msg = (
        f"\n{'='*60}\n"
        f"오류: '{cmd_name}' 명령을 찾을 수 없습니다.\n"
        f"{'='*60}\n"
        f"FFmpeg가 설치되어 있고 PATH에 추가되어 있는지 확인하세요.\n\n"
        f"Windows 설치 방법:\n"
        f"  1. https://www.gyan.dev/ffmpeg/builds/ 에서 다운로드\n"
        f"  2. 압축 해제 후 bin 폴더를 PATH에 추가\n"
        f"  3. 또는 패키지 매니저 사용:\n"
        f"     - choco install ffmpeg (Chocolatey)\n"
        f"     - winget install ffmpeg (Windows Package Manager)\n\n"
        f"설치 후 새 터미널을 열고 'ffprobe -version' 명령으로 확인하세요.\n"
        f"또는 FFmpeg bin 폴더를 시스템 PATH 환경 변수에 추가하세요.\n"
        f"{'='*60}\n"
    )
    raise FileNotFoundError(error_msg)

