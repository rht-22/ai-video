# 이 모듈은 영상 편집의 가장 첫 단계인 **"영상 스펙 파악"**과 분석의 기초가 되는 **"씬(Scene) 탐지"**를 담당합니다. 
# 사람이 영상을 편집하기 전 "이 영상은 몇 분짜리고, 장면이 어디서 바뀌지?"를 먼저 파악하는 것과 같습니다.
# 이 코드는 영상을 가공하기 전, 해당 영상이 어떤 상태인지 수치로 확인하는 역할을 합니다. 길이, 프레임 레이트, 해상도, 오디오 유무를 파악하여 이후 편집 과정(자르기, 크롭 등)에서 필요한 기준 데이터를 생성합니다.

# ffprobe: 영상의 길이, 해상도, 프레임 레이트(FPS) 등 기술적 스펙을 추출합니다.

# 이 코드는 영상 편집 파이프라인에서 "입력값 검증 및 데이터 수집" 단계에 해당합니다.

# 전체 흐름 요약
# find_ffmpeg_command: ffprobe 실행 파일의 위치를 찾습니다.

# ffprobe 실행: 영상의 메타데이터를 JSON 형태로 받아옵니다.

# 데이터 정제: 분수 형태의 FPS를 숫자로 바꾸고, 오디오가 있는지 별도로 체크합니다.

# 결과 반환: 모든 정보를 담은 MediaInfo 객체를 시스템에 넘겨줍니다.


# 주목해야 할 기술적 포인트
# ① ffprobe를 활용한 정밀 메타데이터 추출
# 단순히 "길이가 얼마인가"만 묻지 않고, JSON 포맷으로 영상의 모든 정보를 가져옵니다.

# -select_streams v:0: 여러 스트림(오디오, 자막 등) 중 첫 번째 비디오 스트림 정보만 정밀하게 타격합니다.

# avg_frame_rate 파싱: 30000/1001 같은 분수 형태의 FPS를 실수(29.97)로 변환하는 디테일이 포함되어 있을 것입니다.
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_sec: float
    fps: float
    width: int
    height: int
    has_audio: bool

# 영상의 비디오 스트림과 포맷 정보를 한꺼번에 요청
def probe_media(video_path: Path) -> MediaInfo:
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd,
        "-v",
        "error",
        # 비디오 정보 추출: -select_streams v:0을 사용하여 첫 번째 비디오 트랙만 선택하고, 너비(width), 높이(height), 프레임 레이트(r_frame_rate)를 가져옵니다.
        "-select_streams",
        "v:0",
        # 재생 시간 추출: -show_entries format=duration을 통해 영상의 총 길이를 초(sec) 단위로 가져옵니다.
        "-show_entries",
        "stream=width,height,r_frame_rate",
        "-show_entries",
        "format=duration",
        # JSON 결과 파싱: 모든 결과는 -of json 옵션 덕분에 구조화된 데이터로 수신되며, 이를 MediaInfo라는 객체로 변환
        "-of",
        "json",
        str(video_path),
    ]
    raw = subprocess.check_output(cmd)
    payload = json.loads(raw)
    stream = payload["streams"][0]
    duration_sec = float(payload["format"]["duration"])
    width = int(stream["width"])
    height = int(stream["height"])
    fps = _parse_fps(stream.get("r_frame_rate", "30/1"))
    has_audio = _has_audio(video_path)
    return MediaInfo(
        path=video_path,
        duration_sec=duration_sec,
        fps=fps,
        width=width,
        height=height,
        has_audio=has_audio,
    )

# 프레임 계산
# FFmpeg은 프레임 레이트를 보통 "30/1" 또는 "30000/1001" 같은 분수 문자열로 줍니다.
# 이 함수는 문자열을 / 기준으로 나누어 분자(num)를 분모(den)로 나누는 계산을 수행하여 30.0 또는 29.97 같은 실수값으로 변환합니다.
# 분모가 0이거나 형식이 잘못된 경우 기본값(30.0)을 반환하는 안전장치가 포함되어 있습니다.
def _parse_fps(rate: str) -> float:
    parts = rate.split("/")
    if len(parts) != 2:
        return 30.0
    num = float(parts[0])
    den = float(parts[1])
    if den == 0:
        return 30.0
    return num / den

# 오디오 확인
# AI 분석(STT)이나 쇼츠 제작 시 소리가 있는지 확인하는 것은 필수입니다.
# -select_streams a:0을 사용하여 오디오 스트림이 존재하는지 검사합니다.
# 결과값의 streams 리스트가 비어있지 않으면 True를 반환하여 소리가 있는 영상임을 확인합니다.
def _has_audio(video_path: Path) -> bool:
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(video_path),
    ]
    raw = subprocess.check_output(cmd)
    payload = json.loads(raw)
    return bool(payload.get("streams"))
