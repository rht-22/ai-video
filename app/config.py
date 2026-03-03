from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    chunk_seconds: int = 300
    chunk_overlap: int = 3
    target_duration_sec: int = 60
    target_duration_tolerance_sec: int = 10  # 50~70초 범위 허용
    min_duration_sec: int = 50  # 최소 길이 50초
    max_duration_sec: int = 60  # 최대 길이 60초
    canvas_width: int = 1080
    canvas_height: int = 1920
    top_title_height: int = 240
    bottom_label_height: int = 220
    subtitle_margin_top: int = 280
    subtitle_margin_bottom: int = 260
    subtitle_max_chars_per_line: int = 22
    subtitle_max_lines: int = 2
    crop_sample_interval_sec: float = 0.5
    crop_smoothing_window: int = 5
    scene_snap_threshold_sec: float = 0.8
    tts_gain_db: int = -4
    original_gain_db: int = -10
    bgm_gain_db: int = -18
    # 렌더링 속도/화질 트레이드오프
    # - fastest: 최대한 빠르게(품질/압축효율 손해 가능)
    # - balanced: 기본값(속도/품질 균형)
    # - quality: 더 느리지만 품질 유지
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
