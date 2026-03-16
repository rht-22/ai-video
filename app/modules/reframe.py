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
    start_sec: float = 0.0, 
    end_sec: float = None    
) -> list[CropKeyframe]:
    if _has_cv2():
        keyframes = _detect_faces(clip_path, width, height, sample_interval_sec, start_sec, end_sec)
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
    start_sec: float = 0.0, 
    end_sec: float = None
) -> list[CropKeyframe]:
    import cv2
    import numpy as np

    video_path_str = str(Path(clip_path).resolve())
    capture = cv2.VideoCapture(video_path_str)
    
    if not capture.isOpened():
        print(f"[ERROR] OpenCV가 영상을 열 수 없습니다: {video_path_str}")
        return _center_crop(width, height, sample_interval_sec)

    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    
    # 1. 실제 분석할 프레임 범위 계산
    start_frame = int(start_sec * fps)
    if end_sec is None:
        end_frame = total_frames
    else:
        end_frame = min(int(end_sec * fps), total_frames)
    
    # 2. 시작 위치로 이동
    capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    
    # 0.5초(sample_interval_sec) 간격으로 프레임을 건너뛰며 분석
    frame_step = max(1, int(fps * sample_interval_sec))
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    
    keyframes = []
    last_x, last_y = width / 2, height / 2
    crop_w, crop_h = _portrait_crop_size(width, height)

    # 3. 루프를 돌며 타임라인 데이터 생성
    for current_frame in range(start_frame, end_frame, frame_step):
        capture.set(cv2.CAP_PROP_POS_FRAMES, current_frame)
        success, frame = capture.read()
        if not success:
            break
            
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5)
        
        if len(faces) > 0:
            # 가장 큰 얼굴 기준 좌표 업데이트
            x, y, w, h = max(faces, key=lambda item: item[2] * item[3])
            last_x = x + w / 2
            last_y = y + h / 2
        
        # 매 스텝마다 keyframe 추가 (얼굴 못 찾아도 last_x 유지)
        keyframes.append(
            CropKeyframe(
                time_sec=round(float(current_frame / fps), 3),
                x_center=float(last_x),
                y_center=float(last_y),
                crop_w=int(crop_w),
                crop_h=int(crop_h),
            )
        )
        
    capture.release()

    # 4. 분석 결과가 비었을 때만 최소한의 중앙 데이터 생성 (방어 코드)
    if not keyframes:
        print(f"[WARN] {start_sec}s ~ {end_sec}s 구간 분석 실패")
        return _center_crop(width, height, sample_interval_sec)

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
