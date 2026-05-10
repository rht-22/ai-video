from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
import requests
import os

@dataclass(frozen=True)
class DesignConfig:
    # 영상 프레임 (9:16, 1:1 등)
    aspect_ratio: str = "1:1"
    video_width: int = 800
    video_height: int = 1100
    video_y_pos: int = 480

    # 제목 설정 (2줄 구조: line1=맥락, line2=후킹)
    title_font: str = "Jalnan"
    title_size: int = 70
    title_color: str = "white"
    title_colors: list[str] = field(default_factory=lambda: ["white", "#FFFF00"])
    title_sizes: list[int] = field(default_factory=lambda: [70, 90])
    title_y: int = 120

    # 자막 설정 (ASS 스타일 기준)
    subtitle_font: str = "여기어때 잘난체 2 TTF"
    subtitle_size: int = 65
    subtitle_color: str = "&H0000FFFF"
    subtitle_y_margin: int = 380
    # 라운드 10: 자막 스타일 프리셋 (None / "auto" → 장르 기반 자동 선택)
    subtitle_style_preset: str | None = None

    # TTS 자막 설정 (원본 자막과 별도 위치)
    tts_line_font_size: int = 70
    tts_line_color: str = "&H00EBCE87"
    tts_line_y_margin: int = 580

    # 작품명 및 이미지 설정
    work_title_y: int = 1560
    work_font_size: int = 40
    work_color: str = "#FF69B4"
    work_letter_spacing: bool = True

    work_type: str = "text"
    work_value: str | None = None
    # 라운드 23: 200 → 350. 입력 이미지 dimension 무관 항상 W=350px로 scale, 높이는 -1(자동)로 비율 보존.
    work_image_width: int = 350

    overlay_image_path: str | None = None # 필요 시 이미지 경로

    # Phase 11: 화자 우선 얼굴 트래킹 (False면 "가장 큰 얼굴" 폴백)
    enable_speaker_tracking: bool = True

    # Phase 12: 인물 인식 기반 얼굴 추적 (deepface + TMDb 배우 사진)
    enable_face_recognition: bool = True

@dataclass(frozen=True)
class AppConfig:
    chunk_seconds: int = 600
    chunk_overlap: int = 180  # 라운드 19B: 30 → 180. chunks 1+에서 앞 청크 3분 prepend로 경계 사건 손실 방지.
    target_duration_sec: int = 50
    target_duration_tolerance_sec: int = 10
    min_duration_sec: int = 40
    max_duration_sec: int = 60  # 라운드 11: 100 → 60 (요즘 쇼츠 표준 40~60s)
    canvas_width: int = 1080
    canvas_height: int = 1920
    top_title_height: int = 250
    bottom_label_height: int = 200
    subtitle_margin_top: int = 320
    subtitle_margin_bottom: int = 400 
    subtitle_max_chars_per_line: int = 15
    subtitle_max_lines: int = 2
    crop_sample_interval_sec: float = 1.0
    crop_smoothing_window: int = 5
    scene_snap_threshold_sec: float = 0.8
    tts_gain_db: int = -3
    original_gain_db: int = -3
    bgm_gain_db: int = -20
    render_preset: str = "balanced"
    enable_hwaccel: bool = True
    # 바이럴 최적화 설정
    viral_scroll_stop_threshold: float = 0.7
    viral_score_min_threshold: float = 0.4
    max_storyline_candidates: int = 6
    gemini_story_max_retries: int = 3
    max_shorts_count: int = 3


@dataclass(frozen=True)
class Paths:
    app_root: Path

    @property
    def assets_dir(self) -> Path:
        return self.app_root / "assets"

    @property
    def outputs_dir(self) -> Path:
        return self.app_root.parent / "outputs"


def _ensure_ascii_font_path(file_path: Path) -> str:
    """폰트 경로에 비-ASCII 문자가 있으면 시스템 임시 디렉토리로 복사 후 반환."""
    import shutil
    import tempfile
    path_str = str(file_path.resolve())
    try:
        path_str.encode("ascii")
        return path_str.replace("\\", "/")
    except UnicodeEncodeError:
        tmp_dir = Path(tempfile.gettempdir()) / "ai_video_fonts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / file_path.name
        if not tmp_path.exists():
            shutil.copy2(file_path, tmp_path)
        return str(tmp_path).replace("\\", "/")


def get_font_path(name: str, app_root: Path) -> str:
    font_dir = app_root / "assets" / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)

    safe_name = FONT_NAME_MAP.get(name, name.replace(" ", "_"))

    file_path = font_dir / f"{safe_name}.ttf"

    if file_path.exists():
        return _ensure_ascii_font_path(file_path)

    url = REMOTE_FONTS.get(name)
    if url:
        try:
            print(f"폰트 다운로드 시도: {name} -> {safe_name}.ttf")
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            file_path.write_bytes(response.content)
            return _ensure_ascii_font_path(file_path)
        except Exception as e:
            print(f"다운로드 실패: {e}")
            return "Malgun Gothic"

    return name

REMOTE_FONTS = {
    "Jalnan": "https://raw.githubusercontent.com/rht-22/font.zip/main/Jalnan.ttf",
    "JalnanGothic": "https://raw.githubusercontent.com/rht-22/font.zip/main/JalnanGothic.ttf",
    "mulmaru": "https://raw.githubusercontent.com/rht-22/font.zip/main/mulmaru.ttf",
    "Griun": "https://raw.githubusercontent.com/rht-22/font.zip/main/Griun-Polpairness.ttf",
    "Hakgyo": "https://raw.githubusercontent.com/rht-22/font.zip/main/HakgyoansimSamulhamR.ttf"
}

FONT_NAME_MAP = {
    "여기어때 잘난체 2 TTF": "Jalnan",
    "물마루": "mulmaru",
    "여기어때 잘난체 고딕 TTF": "JalnanGothic",
    "그리운 경찰공평체": "Griun"
}
