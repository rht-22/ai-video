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
    # E19-4(2026-08-28): 그 표본에서 실제로 검출된 얼굴 박스(원본 픽셀, 스무딩 전 raw).
    # 라벨 얼굴 회피가 재사용한다 — 검출을 두 번 돌리지 않는 발주서 규율. 미검출은
    # face_w=0 이고, **구 캐시 JSON 은 이 키들이 아예 없다** — 읽는 쪽은 .get() 으로
    # 미검출과 동일하게 취급한다(재개 호환·회피 없이 종전 배치).
    face_cx: float = -1.0
    face_cy: float = -1.0
    face_w: int = 0
    face_h: int = 0


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
    detector: str | None = None,
    crop_size: tuple[int, int] | None = None,
    ema_alpha: float | None = None,
    snap_first: bool = False,
    area_relative: bool = False,
    collect: list | None = None,
) -> list[CropKeyframe]:
    """detector(갭 12): "haar"(기본·종전) | "yunet". None 이면 env FACE_DETECTOR(기본 haar).
    crop_size: 크롭 크기(w,h) 명시 — None 이면 종전 9:16 `_portrait_crop_size`(v1 그대로).

    v3 화자 추적 노브(2026-09-08, 「너 바람피니?」 실사고 — 아래 셋 다 기본값이면 v1 과 바이트 동일):
    ema_alpha: EMA 계수(None = 종전 0.12). snap_first: 클립 첫 얼굴에 **즉시** 맞춘다(컷 경계는
    연속성이 없다 — 이전 클립 끝 위치에서 0.12 로 기어가면 5초짜리 클립이 끝나도 못 닿는다).
    area_relative: 화자 점수의 면적 항을 프레임 대비가 아니라 **그 프레임 최대 얼굴 대비**로 —
    프레임 대비면 275px 얼굴도 0.036 이라 면적 가중 0.3 이 사실상 0 이고 중앙 항이 이긴다."""
    if _has_cv2():
        keyframes = _detect_faces(
            clip_path, width, height, sample_interval_sec, start_sec, end_sec,
            enable_speaker_tracking=enable_speaker_tracking,
            initial_x=initial_x,
            initial_y=initial_y,
            detector=detector,
            ema_alpha=ema_alpha, snap_first=snap_first, area_relative=area_relative,
            collect=collect,
        )
        if crop_size is not None:
            import dataclasses
            cw, ch = int(crop_size[0]) & ~1, int(crop_size[1]) & ~1
            keyframes = [dataclasses.replace(
                kf, crop_w=cw, crop_h=ch,
                x_center=min(max(kf.x_center, cw / 2), width - cw / 2),
                y_center=min(max(kf.y_center, ch / 2), height - ch / 2)) for kf in keyframes]
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

def _mouth_motion(gray, prev_gray, x, y, fw, fh) -> float:
    """얼굴 아래 40%(입 주변)의 직전 표본 대비 밝기 변화 평균 → 0~1. 직전 표본이 없으면 0."""
    import cv2
    if prev_gray is None:
        return 0.0
    frame_h, frame_w = gray.shape[:2]
    my0 = int(y + fh * 0.6); my1 = min(int(y + fh), frame_h)
    mx0 = max(int(x), 0);    mx1 = min(int(x + fw), frame_w)
    if my1 <= my0 or mx1 <= mx0:
        return 0.0
    diff = cv2.absdiff(gray[my0:my1, mx0:mx1], prev_gray[my0:my1, mx0:mx1])
    return min(float(diff.mean()) / 30.0, 1.0)


def _pick_speaker(faces, gray, prev_gray, frame_w: int, frame_h: int, prev_x: float, prev_y: float,
                  area_relative: bool = False):
    """area + center + mouth-motion + sticky 가중 점수로 화자 후보 얼굴 선택.

    area_relative(v3): 면적 항 = 얼굴 면적 ÷ 그 프레임 최대 얼굴 면적(가장 큰 얼굴 = 1.0).
    기본 False 는 종전(프레임 면적 대비)과 바이트 동일."""
    import math
    import cv2

    cx_frame, cy_frame = frame_w / 2, frame_h / 2
    max_dist = math.hypot(cx_frame, cy_frame) or 1.0
    frame_area = float(frame_w * frame_h) or 1.0
    if area_relative:
        frame_area = float(max((int(f[2]) * int(f[3]) for f in faces), default=1) or 1)
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


FACE_DETECTORS = ("haar", "yunet")
YUNET_MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"


def resolve_face_detector(name: str | None) -> str:
    """검출기 이름 확정 — 인자 > env FACE_DETECTOR > haar. 모르는 값은 즉시 실패(조용한 폴백 금지)."""
    kind = (name or os.environ.get("FACE_DETECTOR") or "haar").strip().lower()
    if kind not in FACE_DETECTORS:
        raise ValueError(f"FACE_DETECTOR 는 {FACE_DETECTORS} 중 하나: {kind!r}")
    if kind == "yunet" and not YUNET_MODEL_PATH.exists():
        raise FileNotFoundError(f"YuNet 모델 없음: {YUNET_MODEL_PATH}")
    return kind


class _HaarDetector:
    """종전 3단 cascade(정면 → alt2 → 프로필 → 반전 프로필) — 동작 동일."""

    def __init__(self, tmp_dir: str):
        import cv2
        self._cv2 = cv2
        dets = {}
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml",
                     "haarcascade_profileface.xml"):
            dst = os.path.join(tmp_dir, name)
            shutil.copy2(cv2.data.haarcascades + name, dst)
            dets[name] = cv2.CascadeClassifier(dst)
        self.front = dets["haarcascade_frontalface_default.xml"]
        self.alt = dets["haarcascade_frontalface_alt2.xml"]
        self.profile = dets["haarcascade_profileface.xml"]

    def detect(self, gray, frame_bgr=None):
        import numpy as np
        cv2 = self._cv2
        kw = dict(scaleFactor=1.15, minNeighbors=4, minSize=(40, 40))
        faces = self.front.detectMultiScale(gray, **kw)
        if len(faces) == 0:
            faces = self.alt.detectMultiScale(gray, **kw)
        if len(faces) == 0:
            faces = self.profile.detectMultiScale(gray, **kw)
            if len(faces) == 0:
                flipped = cv2.flip(gray, 1)
                faces_flip = self.profile.detectMultiScale(flipped, **kw)
                if len(faces_flip) > 0:
                    fw = gray.shape[1]
                    faces = np.array([[fw - (x + w), y, w, h] for x, y, w, h in faces_flip])
        return faces


class _YuNetDetector:
    """OpenCV FaceDetectorYN(ONNX) — 측면·작은 얼굴을 Haar 보다 훨씬 덜 놓친다(autoframe 실측:
    Haar 는 10배 느리고 놓치는 프레임이 4배). 입력 크기가 바뀌면 다시 만든다."""

    def __init__(self, model_path: Path, score_th: float = 0.6, nms_th: float = 0.3, top_k: int = 5000):
        import cv2
        if not hasattr(cv2, "FaceDetectorYN"):
            raise RuntimeError(f"이 OpenCV({cv2.__version__})에는 FaceDetectorYN 이 없다 — opencv 4.5.4+ 필요")
        self._cv2 = cv2
        self._model = str(model_path)
        self._args = (score_th, nms_th, top_k)
        self._det = None
        self._size = None

    def detect(self, gray, frame_bgr=None):
        import numpy as np
        cv2 = self._cv2
        if frame_bgr is None:
            frame_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        h_, w_ = frame_bgr.shape[:2]
        if self._size != (w_, h_):
            self._det = cv2.FaceDetectorYN.create(self._model, "", (w_, h_), *self._args)
            self._size = (w_, h_)
        _, faces = self._det.detect(frame_bgr)
        if faces is None or len(faces) == 0:
            return []
        out = [[int(f[0]), int(f[1]), int(f[2]), int(f[3])] for f in faces if f[2] >= 40 and f[3] >= 40]
        return np.array(out) if out else []


def _make_detector(name: str | None, tmp_dir: str):
    kind = resolve_face_detector(name)
    if kind == "yunet":
        return _YuNetDetector(YUNET_MODEL_PATH)
    return _HaarDetector(tmp_dir)


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
    detector: str | None = None,
    ema_alpha: float | None = None,
    snap_first: bool = False,
    area_relative: bool = False,
    collect: list | None = None,
) -> list[CropKeyframe]:
    """collect(v3 hold, 2026-09-08): 리스트를 주면 표본마다 {"t", "faces": [(cx, cy, w, h, motion), …]} 를
    담는다 — 선택된 얼굴 하나가 아니라 **모든** 얼굴과 입 움직임. 계단식 고정이 run 단위로 화자를
    다시 고르는 재료(v9 autoframe 의 talk×√w 순위)."""
    import cv2

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

    # 얼굴 검출기(2026-09-08, 갭 12): 기본 Haar(종전 그대로 — 회귀 0) · `FACE_DETECTOR=yunet`
    # 또는 detector="yunet" 이면 YuNet(ONNX, app/assets/models). 선택은 한 곳(_make_detector).
    _tmp_dir = tempfile.mkdtemp()
    _det = _make_detector(detector, _tmp_dir)

    keyframes = []
    # 라운드 24: 직전 클립의 마지막 위치를 초기값으로 받아 컷 경계 점프 완화.
    smooth_x = float(initial_x) if initial_x is not None else float(width / 2)
    smooth_y = float(initial_y) if initial_y is not None else float(height / 2)
    crop_w, crop_h = _portrait_crop_size(width, height)

    # EMA 스무딩 계수 (0에 가까울수록 부드럽고, 1에 가까울수록 즉각 반응)
    ema_alpha = 0.12 if ema_alpha is None else float(ema_alpha)  # 0.25→0.12: 작은 변동을 더 부드럽게 흡수해 출렁임 감소
    _snapped = not snap_first          # snap_first 면 첫 검출에서 EMA 없이 바로 맞춘다
    # ⚠ 라운드 24 의 sticky(같은 인물이 이어지면 초반을 더 굳게 유지)는 2026-08-25 에
    #   지웠다 — 발동 조건이 `target_character` 와 `prev_target_character` 가 **둘 다**
    #   있는 것인데, 그 값은 face_identifier 가 있을 때만 채워졌고 그런 실행이 없었다.
    # dead zone: 변화량이 화면 폭/높이의 5% 이내면 무시 (정적 장면 미세 흔들림 제거)
    dead_zone_ratio = 0.05
    prev_gray = None  # Phase 11: mouth-motion 계산용 직전 프레임

    # 3. 루프를 돌며 타임라인 데이터 생성
    # 표본마다 `set(CAP_PROP_POS_FRAMES)` 로 다시 seek 하지 않는다(2026-09-08) — 시작 위치로 한 번
    #   seek 한 뒤 grab() 으로 건너뛴다. 같은 프레임 번호를 읽으므로 산출은 종전과 같고(EP01 실측:
    #   read 위치 56728·56739·… 동일), 긴 H.264 소스에서 표본마다 키프레임부터 다시 디코드하던 비용이 없다.
    _pos = start_frame
    for current_frame in range(start_frame, end_frame, frame_step):
        while _pos < current_frame:
            if not capture.grab():
                break
            _pos += 1
        if _pos < current_frame:
            break
        success, frame = capture.read()
        _pos += 1
        if not success:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # 히스토그램 평활화로 명암 대비 개선 (어두운 장면 대응)
        gray = cv2.equalizeHist(gray)

        faces = _det.detect(gray, frame)

        _det_face: tuple[float, float, int, int] | None = None   # E19-4: 이 표본의 raw 얼굴 박스
        if collect is not None:
            collect.append({"t": current_frame / fps,
                            "faces": [(float(x + w / 2), float(y + h / 2), int(w), int(h),
                                       _mouth_motion(gray, prev_gray, x, y, w, h))
                                      for (x, y, w, h) in faces]})
        if len(faces) > 0:
            if enable_speaker_tracking:
                best = _pick_speaker(
                    faces, gray, prev_gray,
                    frame_w=gray.shape[1], frame_h=gray.shape[0],
                    prev_x=smooth_x, prev_y=smooth_y, area_relative=area_relative,
                )
            else:
                best = max(faces, key=lambda item: item[2] * item[3])
            x, y, w, h = best
            target_x = float(x + w / 2)
            target_y = float(y + h / 2)
            _det_face = (target_x, target_y, int(w), int(h))

        if len(faces) > 0:
            # EMA + dead zone: 작은 변동(±5% 이내)은 무시, 큰 변동만 부드럽게 추적
            dz_x = gray.shape[1] * dead_zone_ratio
            dz_y = gray.shape[0] * dead_zone_ratio
            effective_alpha = ema_alpha
            if not _snapped:
                smooth_x, smooth_y, _snapped = target_x, target_y, True
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
                face_cx=_det_face[0] if _det_face else -1.0,
                face_cy=_det_face[1] if _det_face else -1.0,
                face_w=_det_face[2] if _det_face else 0,
                face_h=_det_face[3] if _det_face else 0,
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
