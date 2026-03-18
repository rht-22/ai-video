from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
import requests
import os

@dataclass(frozen=True)
class DesignConfig:
    # 영상 프레임 (9:16, 1:1 등)
    aspect_ratio: str = "9:16" 
    video_width: int = 800
    video_height: int = 1100
    video_y_pos: int = 480
    
    # 제목 설정
    title_font: str = "Jalnan"
    title_size: int = 90
    title_color: str = "white"
    title_colors: list[str] = field(default_factory=lambda: ["white", "white"])
    title_y: int = 150
    
    # 자막 설정 (ASS 스타일 기준)
    subtitle_font: str = "NanumGothicBold"
    subtitle_size: int = 65
    subtitle_color: str = "&H0000FFFF" 
    subtitle_y_margin: int = 450
    
    # 작품명 및 이미지 설정
    work_title_y: int = 1500
    work_font_size: int = 35 
    work_color: str = "white"

    work_type: str = "text"             
    work_value: str | None = None        
    work_image_width: int = 200         

    overlay_image_path: str | None = None # 필요 시 이미지 경로

@dataclass(frozen=True)
class AppConfig:
    chunk_seconds: int = 1800
    chunk_overlap: int = 0
    target_duration_sec: int = 60
    target_duration_tolerance_sec: int = 10 
    min_duration_sec: int = 50  
    max_duration_sec: int = 60  
    # 유튜브 숏츠 표준 규격 (9:16)
    canvas_width: int = 1080
    canvas_height: int = 1920
    # 제목 및 레이블 높이 최적화
    top_title_height: int = 550
    bottom_label_height: int = 450
    # 자막 위치 조정 (알고리즘을 방해하지 않는 위치)
    subtitle_margin_top: int = 320
    subtitle_margin_bottom: int = 400 
    # 가독성을 위해 한 줄당 글자 수 제한 (줄바꿈 유도)
    subtitle_max_chars_per_line: int = 15
    subtitle_max_lines: int = 2
    crop_sample_interval_sec: float = 1.0
    crop_smoothing_window: int = 5
    scene_snap_threshold_sec: float = 0.8
    tts_gain_db: int = -2 # TTS를 더 또렷하게
    original_gain_db: int = -12
    bgm_gain_db: int = -20
    render_preset: str = "balanced"
    enable_hwaccel: bool = True


@dataclass(frozen=True)
class Paths:
    app_root: Path

    @property
    def assets_dir(self) -> Path:
        return self.app_root / "assets"

    @property
    def outputs_dir(self) -> Path:
        return self.app_root.parent / "outputs"


def get_font_path(name: str, app_root: Path) -> str:
    """
    폰트 이름을 받아 로컬 경로를 반환합니다. 
    REMOTE_FONTS에 있으면 다운로드 후 경로를 반환하고, 없으면 시스템 폰트로 간주합니다.
    """
    url = REMOTE_FONTS.get(name)
    
    # 1. URL이 없는 경우 (시스템 폰트 이름으로 간주)
    if not url:
        return name

    # 2. 로컬 저장 폴더 설정 (assets/fonts)
    font_dir = app_root / "assets" / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)
    file_path = font_dir / f"{name}.ttf"

    # 3. 파일이 없으면 다운로드 진행
    if not file_path.exists():
        try:
            print(f"다운로드 중인 폰트: {name}...")
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            file_path.write_bytes(response.content)
            print(f"폰트 다운로드 완료: {file_path}")
        except Exception as e:
            print(f"폰트 다운로드 실패 ({name}): {e}")
            return "Malgun Gothic"  # 실패 시 기본 폰트 반환

    # 4. FFmpeg 호환을 위해 절대 경로의 역슬래시(\)를 슬래시(/)로 변환
    return str(file_path.absolute()).replace("\\", "/")

REMOTE_FONTS = {

    "Jalnan": "https://raw.githubusercontent.com/rht-22/font.zip/main/Jalnan.ttf",

    "JalnanGothic": "https://raw.githubusercontent.com/rht-22/font.zip/main/JalnanGothic.ttf",

    "Griun": "https://raw.githubusercontent.com/rht-22/font.zip/main/Griun-Polpairness.ttf",

    "Hakgyo": "https://raw.githubusercontent.com/rht-22/font.zip/main/HakgyoansimSamulhamR.ttf",

    "Mulmaru": "https://raw.githubusercontent.com/rht-22/font.zip/main/mulmaru.ttf"

}