# from __future__ import annotations

# import json
# from dataclasses import dataclass
# from pathlib import Path


# @dataclass(frozen=True)
# class CropKeyframe:
#     time_sec: float
#     x_center: float
#     y_center: float
#     crop_w: int
#     crop_h: int


# def build_crop_timeline(
#     clip_path: Path,
#     output_path: Path,
#     width: int,
#     height: int,
#     sample_interval_sec: float,
# ) -> list[CropKeyframe]:
#     if _has_cv2():
#         keyframes = _detect_faces(clip_path, width, height, sample_interval_sec)
#     else:
#         keyframes = _center_crop(width, height, sample_interval_sec)

#     # 출력 디렉토리가 존재하는지 확인하고 생성
#     output_path.parent.mkdir(parents=True, exist_ok=True)
    
#     output_path.write_text(
#         json.dumps([kf.__dict__ for kf in keyframes], ensure_ascii=False, indent=2),
#         encoding="utf-8",
#     )
#     return keyframes


# def _has_cv2() -> bool:
#     import importlib.util

#     return importlib.util.find_spec("cv2") is not None


# def _detect_faces(
#     clip_path: Path,
#     width: int,
#     height: int,
#     sample_interval_sec: float,
# ) -> list[CropKeyframe]:
#     import cv2

#     capture = cv2.VideoCapture(str(clip_path))
#     fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
#     frame_interval = max(1, int(fps * sample_interval_sec))
#     detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
#     keyframes = []
#     frame_idx = 0
#     while True:
#         success, frame = capture.read()
#         if not success:
#             break
#         if frame_idx % frame_interval != 0:
#             frame_idx += 1
#             continue
#         gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
#         faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4)
#         if len(faces) == 0:
#             x_center = width / 2
#             y_center = height / 2
#         else:
#             x, y, w, h = max(faces, key=lambda item: item[2] * item[3])
#             x_center = x + w / 2
#             y_center = y + h / 2
#         crop_w, crop_h = _portrait_crop_size(width, height)
#         keyframes.append(
#             CropKeyframe(
#                 time_sec=frame_idx / fps,
#                 x_center=float(x_center),
#                 y_center=float(y_center),
#                 crop_w=crop_w,
#                 crop_h=crop_h,
#             )
#         )
#         frame_idx += 1
#     capture.release()
#     return keyframes


# def _center_crop(width: int, height: int, sample_interval_sec: float) -> list[CropKeyframe]:
#     crop_w, crop_h = _portrait_crop_size(width, height)
#     return [
#         CropKeyframe(
#             time_sec=0.0,
#             x_center=width / 2,
#             y_center=height / 2,
#             crop_w=crop_w,
#             crop_h=crop_h,
#         )
#     ]


# def _portrait_crop_size(width: int, height: int) -> tuple[int, int]:
#     target_ratio = 9 / 16
#     if width / height > target_ratio:
#         crop_h = height
#         crop_w = int(height * target_ratio)
#     else:
#         crop_w = width
#         crop_h = int(width / target_ratio)
#     return crop_w, crop_h

from __future__ import annotations

import json
import os # 파일 경로 확인용 추가
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
    # 1. 파일 존재 여부 및 경로 유효성 검사 추가
    if not clip_path.exists():
        print(f"Error: 파일을 찾을 수 없습니다 -> {clip_path}")
        return _center_crop(width, height, sample_interval_sec)

    if _has_cv2():
        try:
            keyframes = _detect_faces(clip_path, width, height, sample_interval_sec)
        except Exception as e:
            print(f"Face detection failed due to error: {e}. Falling back to center crop.")
            keyframes = _center_crop(width, height, sample_interval_sec)
    else:
        keyframes = _center_crop(width, height, sample_interval_sec)

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
    import numpy as np

    # OpenCV는 한글 경로에 취약하므로 절대 경로 문자열로 확실히 변환
    video_path_str = os.path.abspath(str(clip_path))
    capture = cv2.VideoCapture(video_path_str)

    # 2. 비디오 캡처가 정상적으로 열렸는지 반드시 확인
    if not capture.isOpened():
        print(f"OpenCV: 동영상 파일을 열 수 없습니다. 코덱이나 경로를 확인하세요: {video_path_str}")
        return _center_crop(width, height, sample_interval_sec)

    fps = capture.get(cv2.CAP_PROP_FPS)
    if fps <= 0: fps = 30.0 # FPS를 가져오지 못할 경우 기본값 설정
    
    frame_interval = max(1, int(fps * sample_interval_sec))
    
    # 하스캐스케이드 파일 경로 재확인
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    
    keyframes = []
    frame_idx = 0
    
    try:
        while True:
            # 3. read() 호출 전 예외 방지를 위해 success 체크 강화
            success, frame = capture.read()
            if not success or frame is None:
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
                # 가장 큰 얼굴 선택
                x, y, w, h = max(faces, key=lambda item: item[2] * item[3])
                x_center = x + w / 2
                y_center = y + h / 2
            
            crop_w, crop_h = _portrait_crop_size(width, height)
            keyframes.append(
                CropKeyframe(
                    time_sec=float(frame_idx / fps),
                    x_center=float(x_center),
                    y_center=float(y_center),
                    crop_w=int(crop_w),
                    crop_h=int(crop_h),
                )
            )
            frame_idx += 1
    finally:
        capture.release() # 에러가 나더라도 자원은 해제
        
    # 만약 프레임을 하나도 읽지 못했다면 기본 크롭 반환
    if not keyframes:
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