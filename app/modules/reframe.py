from __future__ import annotations

import json
import os
import shutil
import tempfile
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

    # 한글 경로 문제 우회: ASCII 임시 경로로 복사 후 로드
    _tmp_dir = tempfile.mkdtemp()
    cascade_tmp = os.path.join(_tmp_dir, "haarcascade_frontalface_default.xml")
    shutil.copy2(cv2.data.haarcascades + "haarcascade_frontalface_default.xml", cascade_tmp)
    detector = cv2.CascadeClassifier(cascade_tmp)

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
                # time_sec=round(float(current_frame / fps), 3),
                time_sec=current_frame / fps,
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
    
    # 1.0 = 기존 (타이트함, 화질 저하 위험)
    # 1.3 ~ 1.5 = 수정 (여유로운 구도, 고화질 유지)
    zoom_out_factor = 1.4 

    if width / height > target_ratio:
        # 가로형 영상
        base_h = height
        base_w = int(height * target_ratio)
    else:
        # 세로형 영상
        base_w = width
        base_h = int(width / target_ratio)

    # 더 넓은 영역을 크롭 범위로 설정
    crop_w = int(base_w * zoom_out_factor)
    crop_h = int(base_h * zoom_out_factor)

    # 원본 해상도를 넘지 않도록 방어 코드
    crop_w = min(crop_w, width)
    crop_h = min(crop_h, height)

    return crop_w, crop_h

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
#     start_sec: float = 0.0, 
#     end_sec: float = None 
# ) -> list[CropKeyframe]:
#     if _has_cv2():
#         keyframes = _detect_faces(clip_path, width, height, sample_interval_sec,start_sec,end_sec)
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
#     start_sec: float = 0.0, 
#     end_sec: float = None
# ) -> list[CropKeyframe]:
#     import cv2

#     capture = cv2.VideoCapture(str(clip_path))
#     fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
#     total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

#     # 분석 시작 프레임과 종료 프레임 계산
#     start_frame = int(start_sec * fps)
#     if end_sec is None:
#         end_frame = total_frames
#     else:
#         end_frame = min(int(end_sec * fps), total_frames)

#     # 시작 위치로 비디오 포인터 이동
#     capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

#     frame_interval = max(1, int(fps * sample_interval_sec))
#     detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
#     keyframes = []
    
#     # 현재 위치(frame_idx)를 start_frame으로 초기화
#     frame_idx = start_frame

#     while frame_idx < end_frame:
#         success, frame = capture.read()
#         if not success:
#             break
        
#         # 지정된 간격(sample_interval_sec)마다만 분석 실행
#         if (frame_idx - start_frame) % frame_interval == 0:
#             gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
#             faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4)
            
#             if len(faces) == 0:
#                 x_center = width / 2
#                 y_center = height / 2
#             else:
#                 x, y, w, h = max(faces, key=lambda item: item[2] * item[3])
#                 x_center = x + w / 2
#                 y_center = y + h / 2
            
#             crop_w, crop_h = _portrait_crop_size(width, height)
#             keyframes.append(
#                 CropKeyframe(
#                     time_sec=frame_idx / fps,
#                     x_center=float(x_center),
#                     y_center=float(y_center),
#                     crop_w=crop_w,
#                     crop_h=crop_h,
#                 )
#             )
#         frame_idx += 1
        
#     capture.release()
#     return keyframes

