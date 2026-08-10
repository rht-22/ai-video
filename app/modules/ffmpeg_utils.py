from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 지원하는 ffmpeg 메이저 버전.
# 8.x 는 자막 필터(`ass=…:fontsdir=…`)와 `-filter_complex_script` 문법을 거부해
# 렌더 단계(15단계 중 14번째)에서 실패한다. 생성 1편이 ~68분이라, 다 돌린 뒤
# 마지막에 죽으면 손실이 크다 → 시작 시점에 막는다.
SUPPORTED_FFMPEG_MAJORS = (6, 7)

# 검사를 건너뛰는 탈출구. 새 ffmpeg 를 시험할 때만 쓴다.
ALLOW_UNSUPPORTED_FFMPEG_ENV = "AI_VIDEO_ALLOW_UNSUPPORTED_FFMPEG"


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


def get_ffmpeg_major_version(ffmpeg_path: str) -> int | None:
    """`ffmpeg -version` 첫 줄에서 메이저 버전을 뽑는다.

    Returns:
        메이저 버전 정수. 실행에 실패하거나 형식을 못 읽으면 None
        (git 빌드처럼 `ffmpeg version N-xxxxx` 형태면 버전을 알 수 없다).
    """
    try:
        result = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    match = re.search(r"ffmpeg version\s+n?(\d+)\.", result.stdout or "")
    return int(match.group(1)) if match else None


def ensure_ffmpeg_supported() -> None:
    """지원하지 않는 ffmpeg 면 즉시 중단한다. 긴 생성 전에 호출할 것.

    버전을 읽지 못하면(예: git 빌드) 경고만 남기고 통과시킨다 —
    확인 불가를 실패로 취급하면 정상 환경까지 막힌다.

    Raises:
        RuntimeError: 지원 목록 밖의 메이저 버전일 때.
    """
    if os.getenv(ALLOW_UNSUPPORTED_FFMPEG_ENV):
        return

    ffmpeg_path = find_ffmpeg_command("ffmpeg")
    major = get_ffmpeg_major_version(ffmpeg_path)

    if major is None:
        print(f"  [WARN] ffmpeg 버전을 확인하지 못했습니다 ({ffmpeg_path}) — 검사를 건너뜁니다.")
        return

    if major in SUPPORTED_FFMPEG_MAJORS:
        return

    supported = "/".join(str(v) for v in SUPPORTED_FFMPEG_MAJORS)
    raise RuntimeError(
        f"\n{'='*60}\n"
        f"오류: 지원하지 않는 ffmpeg 버전입니다 — {major}.x\n"
        f"{'='*60}\n"
        f"경로: {ffmpeg_path}\n"
        f"필요: ffmpeg {supported}.x\n\n"
        f"8.x 는 자막 필터(ass=…:fontsdir=…)와 -filter_complex_script 문법을 거부해\n"
        f"렌더 단계에서 실패합니다. 생성을 다 돌린 뒤 마지막에 죽는 것을 막기 위해\n"
        f"시작 시점에 중단했습니다.\n\n"
        f"macOS 해결:\n"
        f"  brew install ffmpeg@7\n"
        f"  brew unlink ffmpeg && brew link --force --overwrite ffmpeg@7\n"
        f"  ffmpeg -version   # 7.x 확인\n\n"
        f"검사를 건너뛰려면(권장하지 않음): {ALLOW_UNSUPPORTED_FFMPEG_ENV}=1\n"
        f"{'='*60}\n"
    )

