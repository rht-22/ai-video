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
    end_sec: float = None,
    enable_speaker_tracking: bool = True,
    initial_x: float | None = None,
    initial_y: float | None = None,
) -> list[CropKeyframe]:
    if _has_cv2():
        keyframes = _detect_faces(
            clip_path, width, height, sample_interval_sec, start_sec, end_sec,
            enable_speaker_tracking=enable_speaker_tracking,
            initial_x=initial_x,
            initial_y=initial_y,
        )
    else:
        keyframes = _center_crop(width, height, sample_interval_sec)

    # 출력 디렉토리가 존재하는지 확인하고 생성
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(
        json.dumps([kf.__dict__ for kf in keyframes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return keyframes


# ⚠ `build_multi_face_crop_timeline`·`_detect_multi_face_union` 은 2026-08-25 에
#    지웠다(TMDb·deepface 제거, 사용자 결정). 둘 다 배우 사진 레퍼런스가 있어야
#    발동했는데 사진이 붙은 적이 없어 **한 번도 안 돌았다**.


def _has_cv2() -> bool:
    import importlib.util

    return importlib.util.find_spec("cv2") is not None

def _pick_speaker(faces, gray, prev_gray, frame_w: int, frame_h: int, prev_x: float, prev_y: float):
    """area + center + mouth-motion + sticky 가중 점수로 화자 후보 얼굴 선택."""
    import math
    import cv2

    cx_frame, cy_frame = frame_w / 2, frame_h / 2
    max_dist = math.hypot(cx_frame, cy_frame) or 1.0
    frame_area = float(frame_w * frame_h) or 1.0
    diag = math.hypot(frame_w, frame_h) or 1.0

    best_score = -1.0
    best_face = None
    for (x, y, fw, fh) in faces:
        cx, cy = x + fw / 2, y + fh / 2
        area_norm = (fw * fh) / frame_area
        center_norm = 1.0 - (math.hypot(cx - cx_frame, cy - cy_frame) / max_dist)

        my0 = int(y + fh * 0.6); my1 = min(int(y + fh), frame_h)
        mx0 = max(int(x), 0);    mx1 = min(int(x + fw), frame_w)
        if prev_gray is not None and my1 > my0 and mx1 > mx0:
            diff = cv2.absdiff(gray[my0:my1, mx0:mx1], prev_gray[my0:my1, mx0:mx1])
            motion_norm = min(float(diff.mean()) / 30.0, 1.0)
        else:
            motion_norm = 0.0

        sticky_norm = 1.0 - min(math.hypot(cx - prev_x, cy - prev_y) / diag, 1.0)
        score = 0.30 * area_norm + 0.30 * center_norm + 0.30 * motion_norm + 0.10 * sticky_norm
        if score > best_score:
            best_score = score
            best_face = (x, y, fw, fh)
    return best_face


def _detect_faces(
    clip_path: Path,
    width: int,
    height: int,
    sample_interval_sec: float,
    start_sec: float = 0.0,
    end_sec: float = None,
    enable_speaker_tracking: bool = True,
    initial_x: float | None = None,
    initial_y: float | None = None,
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

    # Haar Cascade 로드 (정면 + 프로필 얼굴)
    _tmp_dir = tempfile.mkdtemp()

    cascade_front_tmp = os.path.join(_tmp_dir, "haarcascade_frontalface_default.xml")
    shutil.copy2(cv2.data.haarcascades + "haarcascade_frontalface_default.xml", cascade_front_tmp)
    detector_front = cv2.CascadeClassifier(cascade_front_tmp)

    cascade_alt_tmp = os.path.join(_tmp_dir, "haarcascade_frontalface_alt2.xml")
    shutil.copy2(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml", cascade_alt_tmp)
    detector_alt = cv2.CascadeClassifier(cascade_alt_tmp)

    cascade_profile_tmp = os.path.join(_tmp_dir, "haarcascade_profileface.xml")
    shutil.copy2(cv2.data.haarcascades + "haarcascade_profileface.xml", cascade_profile_tmp)
    detector_profile = cv2.CascadeClassifier(cascade_profile_tmp)

    keyframes = []
    # 라운드 24: 직전 클립의 마지막 위치를 초기값으로 받아 컷 경계 점프 완화.
    smooth_x = float(initial_x) if initial_x is not None else float(width / 2)
    smooth_y = float(initial_y) if initial_y is not None else float(height / 2)
    crop_w, crop_h = _portrait_crop_size(width, height)

    # EMA 스무딩 계수 (0에 가까울수록 부드럽고, 1에 가까울수록 즉각 반응)
    ema_alpha = 0.12  # 0.25→0.12: 작은 변동을 더 부드럽게 흡수해 출렁임 감소
    # ⚠ 라운드 24 의 sticky(같은 인물이 이어지면 초반을 더 굳게 유지)는 2026-08-25 에
    #   지웠다 — 발동 조건이 `target_character` 와 `prev_target_character` 가 **둘 다**
    #   있는 것인데, 그 값은 face_identifier 가 있을 때만 채워졌고 그런 실행이 없었다.
    # dead zone: 변화량이 화면 폭/높이의 5% 이내면 무시 (정적 장면 미세 흔들림 제거)
    dead_zone_ratio = 0.05
    prev_gray = None  # Phase 11: mouth-motion 계산용 직전 프레임

    # 3. 루프를 돌며 타임라인 데이터 생성
    for current_frame in range(start_frame, end_frame, frame_step):
        capture.set(cv2.CAP_PROP_POS_FRAMES, current_frame)
        success, frame = capture.read()
        if not success:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # 히스토그램 평활화로 명암 대비 개선 (어두운 장면 대응)
        gray = cv2.equalizeHist(gray)

        # 다중 Cascade로 얼굴 탐지 (정면 → alt2 → 프로필)
        faces = detector_front.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(40, 40))
        if len(faces) == 0:
            faces = detector_alt.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(40, 40))
        if len(faces) == 0:
            faces = detector_profile.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(40, 40))
            if len(faces) == 0:
                # 좌우 반전으로 반대쪽 프로필도 시도
                flipped = cv2.flip(gray, 1)
                faces_flip = detector_profile.detectMultiScale(flipped, scaleFactor=1.15, minNeighbors=4, minSize=(40, 40))
                if len(faces_flip) > 0:
                    # 좌표를 원본 기준으로 복원
                    fw = gray.shape[1]
                    faces = np.array([[fw - (x + w), y, w, h] for x, y, w, h in faces_flip])

        if len(faces) > 0:
            if enable_speaker_tracking:
                best = _pick_speaker(
                    faces, gray, prev_gray,
                    frame_w=gray.shape[1], frame_h=gray.shape[0],
                    prev_x=smooth_x, prev_y=smooth_y,
                )
            else:
                best = max(faces, key=lambda item: item[2] * item[3])
            x, y, w, h = best
            target_x = float(x + w / 2)
            target_y = float(y + h / 2)

        if len(faces) > 0:
            # EMA + dead zone: 작은 변동(±5% 이내)은 무시, 큰 변동만 부드럽게 추적
            dz_x = gray.shape[1] * dead_zone_ratio
            dz_y = gray.shape[0] * dead_zone_ratio
            effective_alpha = ema_alpha
            if abs(target_x - smooth_x) > dz_x:
                smooth_x = effective_alpha * target_x + (1 - effective_alpha) * smooth_x
            if abs(target_y - smooth_y) > dz_y:
                smooth_y = effective_alpha * target_y + (1 - effective_alpha) * smooth_y
        # 얼굴 못 찾으면 smooth_x/y 유지 (자연스럽게 정체)
        prev_gray = gray

        keyframes.append(
            CropKeyframe(
                time_sec=current_frame / fps,
                x_center=smooth_x,
                y_center=smooth_y,
                crop_w=int(crop_w),
                crop_h=int(crop_h),
            )
        )

    capture.release()
    shutil.rmtree(_tmp_dir, ignore_errors=True)

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

    # 1.6 = 넓은 구도 (인물 주변 환경까지 포함)
    zoom_out_factor = 1.6

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
