from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CropKeyframe:
    time_sec: float
    x_center: float
    y_center: float
    crop_w: int
    crop_h: int


def build_crop_timeline(
    clip_path: Path,
    output_path: Path,
    width: int,
    height: int,
    sample_interval_sec: float,
) -> list[CropKeyframe]:
    if _has_cv2():
        keyframes = _detect_faces(clip_path, width, height, sample_interval_sec)
    else:
        keyframes = _center_crop(width, height, sample_interval_sec)

    # 출력 디렉토리가 존재하는지 확인하고 생성
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    output_path.write_text(
        json.dumps([kf.__dict__ for kf in keyframes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return keyframes


def _has_cv2() -> bool:
    import importlib.util

    return importlib.util.find_spec("cv2") is not None


def _detect_faces(
    clip_path: Path,
    width: int,
    height: int,
    sample_interval_sec: float,
) -> list[CropKeyframe]:
    import cv2

    capture = cv2.VideoCapture(str(clip_path))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(1, int(fps * sample_interval_sec))
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    keyframes = []
    frame_idx = 0
    while True:
        success, frame = capture.read()
        if not success:
            break
        if frame_idx % frame_interval != 0:
            frame_idx += 1
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4)
        if len(faces) == 0:
            x_center = width / 2
            y_center = height / 2
        else:
            x, y, w, h = max(faces, key=lambda item: item[2] * item[3])
            x_center = x + w / 2
            y_center = y + h / 2
        crop_w, crop_h = _portrait_crop_size(width, height)
        keyframes.append(
            CropKeyframe(
                time_sec=frame_idx / fps,
                x_center=float(x_center),
                y_center=float(y_center),
                crop_w=crop_w,
                crop_h=crop_h,
            )
        )
        frame_idx += 1
    capture.release()
    return keyframes


def _center_crop(width: int, height: int, sample_interval_sec: float) -> list[CropKeyframe]:
    crop_w, crop_h = _portrait_crop_size(width, height)
    return [
        CropKeyframe(
            time_sec=0.0,
            x_center=width / 2,
            y_center=height / 2,
            crop_w=crop_w,
            crop_h=crop_h,
        )
    ]


def _portrait_crop_size(width: int, height: int) -> tuple[int, int]:
    target_ratio = 9 / 16
    if width / height > target_ratio:
        crop_h = height
        crop_w = int(height * target_ratio)
    else:
        crop_w = width
        crop_h = int(width / target_ratio)
    return crop_w, crop_h
