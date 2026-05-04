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
    target_character: str | None = None,
    face_identifier: object | None = None,
    character_index: list[dict] | None = None,
) -> list[CropKeyframe]:
    if _has_cv2():
        keyframes = _detect_faces(
            clip_path, width, height, sample_interval_sec, start_sec, end_sec,
            enable_speaker_tracking=enable_speaker_tracking,
            target_character=target_character,
            face_identifier=face_identifier,
            character_index=character_index,
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


def build_multi_face_crop_timeline(
    clip_path: Path,
    output_path: Path,
    width: int,
    height: int,
    sample_interval_sec: float,
    start_sec: float = 0.0,
    end_sec: float = None,
    target_characters: list[str] | None = None,
    face_identifier: object | None = None,
    character_index: list[dict] | None = None,
    union_max_screen_ratio: float = 0.7,
    margin_ratio: float = 0.10,
) -> list[CropKeyframe]:
    """라운드 19A: ≥2명 캐릭터 동시 추적용 와이드 프레이밍 크롭 타임라인.

    매 샘플 프레임에서 face_identifier/character_index로 target_characters에 속하는 얼굴을 식별하고
    *bbox union + margin*으로 두 명 모두 화면에 들어오는 크롭을 생성. union이 화면 70% 이상 차지하면
    가장 큰 1명 single-track 폴백 (build_crop_timeline 결과와 동일 효과).

    target_characters는 ≥2명을 받지만 1명만 찾히는 프레임은 그 1명 + margin으로 single-track 처리.
    """
    if not _has_cv2() or not target_characters:
        return build_crop_timeline(
            clip_path, output_path, width, height, sample_interval_sec,
            start_sec, end_sec,
            target_character=(target_characters[0] if target_characters else None),
            face_identifier=face_identifier,
            character_index=character_index,
        )
    keyframes = _detect_multi_face_union(
        clip_path, width, height, sample_interval_sec, start_sec, end_sec,
        target_characters=target_characters,
        face_identifier=face_identifier,
        character_index=character_index,
        union_max_screen_ratio=union_max_screen_ratio,
        margin_ratio=margin_ratio,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([kf.__dict__ for kf in keyframes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return keyframes


def _detect_multi_face_union(
    clip_path: Path,
    width: int,
    height: int,
    sample_interval_sec: float,
    start_sec: float,
    end_sec: float,
    target_characters: list[str],
    face_identifier: object | None,
    character_index: list[dict] | None,
    union_max_screen_ratio: float,
    margin_ratio: float,
) -> list[CropKeyframe]:
    import cv2
    import numpy as np

    video_path_str = str(Path(clip_path).resolve())
    capture = cv2.VideoCapture(video_path_str)
    if not capture.isOpened():
        return _center_crop(width, height, sample_interval_sec)

    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    start_frame = int(start_sec * fps)
    end_frame = total_frames if end_sec is None else min(int(end_sec * fps), total_frames)
    capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    frame_step = max(1, int(fps * sample_interval_sec))

    # Haar cascade
    _tmp_dir = tempfile.mkdtemp()
    cascade_front_tmp = os.path.join(_tmp_dir, "haarcascade_frontalface_default.xml")
    shutil.copy2(cv2.data.haarcascades + "haarcascade_frontalface_default.xml", cascade_front_tmp)
    detector_front = cv2.CascadeClassifier(cascade_front_tmp)
    cascade_alt_tmp = os.path.join(_tmp_dir, "haarcascade_frontalface_alt2.xml")
    shutil.copy2(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml", cascade_alt_tmp)
    detector_alt = cv2.CascadeClassifier(cascade_alt_tmp)

    keyframes: list[CropKeyframe] = []
    smooth_x, smooth_y = float(width / 2), float(height / 2)
    smooth_w, smooth_h = float(width / 2), float(height / 2)
    crop_w_default, crop_h_default = _portrait_crop_size(width, height)
    ema_alpha = 0.18  # multi 모드는 약간 빠른 반응 (인물 이동 추적)

    for current_frame in range(start_frame, end_frame, frame_step):
        capture.set(cv2.CAP_PROP_POS_FRAMES, current_frame)
        success, frame = capture.read()
        if not success:
            break
        frame_w, frame_h = frame.shape[1], frame.shape[0]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)

        faces = detector_front.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(40, 40))
        if len(faces) == 0:
            faces = detector_alt.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(40, 40))

        # 각 target character에 대해 위치 식별
        target_boxes: list[tuple[float, float, float, float]] = []  # (x, y, w, h) center 기반
        t_sec = current_frame / fps
        for char_name in target_characters:
            cx, cy = None, None
            if character_index:
                try:
                    from app.modules.face_id import find_target_in_index
                    result = find_target_in_index(character_index, char_name, t_sec, frame_w, frame_h)
                    if result is not None:
                        cx, cy = result
                except Exception:
                    pass
            if (cx is None) and face_identifier:
                try:
                    result = face_identifier.find_target_in_frame(frame, char_name)
                    if result is not None:
                        cx, cy = result
                except Exception:
                    pass
            if cx is not None:
                # face bbox는 detect 결과 중 center에 가장 가까운 것을 매칭
                best_box = None
                best_dist = float("inf")
                for (fx, fy, fw, fh) in faces:
                    fcx, fcy = fx + fw / 2, fy + fh / 2
                    d = (fcx - cx) ** 2 + (fcy - cy) ** 2
                    if d < best_dist:
                        best_dist = d
                        best_box = (fx, fy, fw, fh)
                if best_box is not None:
                    target_boxes.append(best_box)

        # union 계산
        if len(target_boxes) >= 2:
            xs = [b[0] for b in target_boxes] + [b[0] + b[2] for b in target_boxes]
            ys = [b[1] for b in target_boxes] + [b[1] + b[3] for b in target_boxes]
            ux0, uy0 = max(0, min(xs)), max(0, min(ys))
            ux1, uy1 = min(frame_w, max(xs)), min(frame_h, max(ys))
            uw, uh = ux1 - ux0, uy1 - uy0
            # margin 적용
            mx = uw * margin_ratio
            my = uh * margin_ratio
            ux0 = max(0, ux0 - mx); uy0 = max(0, uy0 - my)
            ux1 = min(frame_w, ux1 + mx); uy1 = min(frame_h, uy1 + my)
            target_x = (ux0 + ux1) / 2
            target_y = (uy0 + uy1) / 2
            union_w = ux1 - ux0
            union_h = uy1 - uy0
            screen_ratio = (union_w * union_h) / (frame_w * frame_h)
            if screen_ratio > union_max_screen_ratio:
                # 너무 넓음 → 가장 큰 1명 single-track 폴백
                biggest = max(target_boxes, key=lambda b: b[2] * b[3])
                target_x = biggest[0] + biggest[2] / 2
                target_y = biggest[1] + biggest[3] / 2
                union_w = biggest[2] * (1 + 2 * margin_ratio)
                union_h = biggest[3] * (1 + 2 * margin_ratio)
        elif len(target_boxes) == 1:
            (fx, fy, fw, fh) = target_boxes[0]
            target_x = fx + fw / 2
            target_y = fy + fh / 2
            union_w = fw * (1 + 2 * margin_ratio)
            union_h = fh * (1 + 2 * margin_ratio)
        else:
            # 식별 실패 → 기본 portrait 크롭 유지 (smooth 그대로)
            target_x = smooth_x
            target_y = smooth_y
            union_w = smooth_w
            union_h = smooth_h

        # EMA 스무딩
        smooth_x = ema_alpha * target_x + (1 - ema_alpha) * smooth_x
        smooth_y = ema_alpha * target_y + (1 - ema_alpha) * smooth_y
        smooth_w = ema_alpha * union_w + (1 - ema_alpha) * smooth_w
        smooth_h = ema_alpha * union_h + (1 - ema_alpha) * smooth_h

        # crop_w/h: union을 portrait aspect로 감쌀 수 있는 크기로 확장
        target_aspect = crop_w_default / crop_h_default if crop_h_default else 9.0 / 16.0
        if smooth_w / max(smooth_h, 1) > target_aspect:
            crop_w_use = int(smooth_w)
            crop_h_use = int(smooth_w / target_aspect)
        else:
            crop_h_use = int(smooth_h)
            crop_w_use = int(smooth_h * target_aspect)
        # 최소 default 크기 보장
        crop_w_use = max(crop_w_use, crop_w_default)
        crop_h_use = max(crop_h_use, crop_h_default)
        # 영상 크기 초과 방지
        crop_w_use = min(crop_w_use, frame_w)
        crop_h_use = min(crop_h_use, frame_h)

        keyframes.append(CropKeyframe(
            time_sec=current_frame / fps,
            x_center=smooth_x,
            y_center=smooth_y,
            crop_w=crop_w_use,
            crop_h=crop_h_use,
        ))

    capture.release()
    shutil.rmtree(_tmp_dir, ignore_errors=True)
    if not keyframes:
        return _center_crop(width, height, sample_interval_sec)
    return keyframes


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
    target_character: str | None = None,
    face_identifier: object | None = None,
    character_index: list[dict] | None = None,
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
    smooth_x, smooth_y = float(width / 2), float(height / 2)
    crop_w, crop_h = _portrait_crop_size(width, height)

    # EMA 스무딩 계수 (0에 가까울수록 부드럽고, 1에 가까울수록 즉각 반응)
    ema_alpha = 0.12  # 0.25→0.12: 작은 변동을 더 부드럽게 흡수해 출렁임 감소
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

        # Phase 12: 인덱스 lookup 우선, 미스 시 face_identifier(ArcFace) 폴백
        face_id_hit = False
        if target_character:
            t_sec = current_frame / fps
            if character_index:
                try:
                    from app.modules.face_id import find_target_in_index
                    result = find_target_in_index(
                        character_index, target_character, t_sec,
                        frame.shape[1], frame.shape[0],
                    )
                    if result is not None:
                        target_x, target_y = result
                        face_id_hit = True
                except Exception:
                    pass
            if not face_id_hit and face_identifier:
                try:
                    result = face_identifier.find_target_in_frame(frame, target_character)
                    if result is not None:
                        target_x, target_y = result
                        face_id_hit = True
                except Exception:
                    pass  # deepface 오류 시 기존 로직 폴백

        if not face_id_hit and len(faces) > 0:
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

        if face_id_hit or len(faces) > 0:
            # EMA + dead zone: 작은 변동(±5% 이내)은 무시, 큰 변동만 부드럽게 추적
            dz_x = gray.shape[1] * dead_zone_ratio
            dz_y = gray.shape[0] * dead_zone_ratio
            if abs(target_x - smooth_x) > dz_x:
                smooth_x = ema_alpha * target_x + (1 - ema_alpha) * smooth_x
            if abs(target_y - smooth_y) > dz_y:
                smooth_y = ema_alpha * target_y + (1 - ema_alpha) * smooth_y
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
