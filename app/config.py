from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

# @dataclass(frozen=True)
# class DesignConfig:
#     # 영상 프레임 (9:16, 1:1 등)
#     aspect_ratio: str = "9:16" 
    
#     # 제목 설정
#     title_font: str = "Malgun Gothic"
#     title_size: int = 70
#     title_color: str = "yellow"
#     title_y: int = 120
    
#     # 자막 설정 (ASS 스타일 기준)
#     subtitle_font: str = "Malgun Gothic"
#     subtitle_size: int = 65
#     subtitle_color: str = "&H0000FFFF" # 노란색
#     subtitle_outline_color: str = "&H00000000"
#     subtitle_y_margin: int = 400
    
#     # 작품명 및 이미지 설정
#     work_title_y: int = 1700
#     overlay_image_path: str | None = None # 필요 시 이미지 경로

    # @classmethod
    # def from_dict(cls, data: dict):
    #     return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class AppConfig:
    chunk_seconds: int = 600
    # chunk_overlap: int = 5
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
    crop_sample_interval_sec: float = 0.5
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