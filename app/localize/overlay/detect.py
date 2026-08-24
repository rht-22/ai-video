"""[엔진①] 텍스트 탐지·인식·스타일 추출.

프레임을 N개마다 샘플링 → OCR 로 텍스트 영역(bbox)·내용·대략 스타일 추출 → detections.json.
- OCR 백엔드 팩토리: rapidocr(기본) → paddleocr → easyocr.
- ROI 수동 지정 모드: --subtitle-area x1 y1 x2 y2 (스타일화된 밈 폰트로 OCR 실패할 때).
- 신뢰도 낮은 항목은 flagged=True 로 표시(사람 검수 게이트).

무거운 의존성(cv2/numpy/OCR)은 lazy import. 순수 헬퍼(position_bucket 등)는 의존성 없이 동작.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import get_logger, load_config, resolve_path
from app.localize.overlay.schemas import BBox, DetectionDoc, FrameDetections, Region, Style

log = get_logger("detect")

# 정규화된 OCR 결과 한 건: (axis-aligned bbox, text, confidence)
OcrResult = tuple[BBox, str, float]


# ── 순수 헬퍼 (의존성 없음 → 테스트 대상) ─────────────────────────────────
def quad_to_bbox(points: list[list[float]]) -> BBox:
    """4점 폴리곤 → axis-aligned (x1,y1,x2,y2) 정수 bbox."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


def position_bucket(bbox: BBox, width: int, height: int) -> str:
    """bbox 중심 → 'top/center/bottom'-'left/center/right' 위치 버킷."""
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    vy = "top" if cy < height / 3 else ("bottom" if cy > 2 * height / 3 else "center")
    vx = "left" if cx < width / 3 else ("right" if cx > 2 * width / 3 else "center")
    return f"{vy}-{vx}"


def clamp_bbox(bbox: BBox, width: int, height: int) -> BBox:
    x1, y1, x2, y2 = bbox
    return (max(0, min(x1, width)), max(0, min(y1, height)),
            max(0, min(x2, width)), max(0, min(y2, height)))


def korean_ocr_warning(backend: str, languages: Optional[list[str]]) -> str:
    """RapidOCR 기본 모델은 한국어 인식 미지원 → 경고 문구(해당 없으면 빈 문자열)."""
    if backend == "rapidocr" and "korean" in (languages or []):
        return ("RapidOCR 기본 모델은 한국어 인식을 지원하지 않아 텍스트가 깨질 수 있습니다"
                "(영역 탐지는 동작). 한국어 화면 텍스트는 `--backend paddleocr` 또는 "
                "ROI(`--subtitle-area`)+사람 검수 게이트를 권장합니다.")
    return ""


# ── OCR 백엔드 팩토리 ────────────────────────────────────────────────────
class OCRBackend:
    """recognize(frame_bgr) -> list[OcrResult] 계약."""
    name = "base"

    def recognize(self, frame_bgr) -> list[OcrResult]:  # pragma: no cover - 추상
        raise NotImplementedError


class RapidOCRBackend(OCRBackend):
    name = "rapidocr"

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise ImportError("rapidocr 필요: pip install rapidocr-onnxruntime") from e
        self._ocr = RapidOCR()

    def recognize(self, frame_bgr) -> list[OcrResult]:
        result, _ = self._ocr(frame_bgr)
        out: list[OcrResult] = []
        for box, text, score in (result or []):
            out.append((quad_to_bbox(box), text, float(score)))
        return out


class PaddleOCRBackend(OCRBackend):
    name = "paddleocr"

    def __init__(self, languages: Optional[list[str]] = None,
                 det_model: Optional[str] = None, rec_model: Optional[str] = None) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as e:
            raise ImportError("paddleocr 필요: pip install paddleocr (+ paddlepaddle)") from e
        lang = "korean" if not languages or "korean" in languages else languages[0]
        # 3.x: 문서 전처리(방향/언워핑) 끄면 한국어 인식 정확도가 크게 오름.
        #      det/rec 모델명 지정 시 CPU 에서 훨씬 빠름(server det 는 1080p 에서 수백초/프레임).
        #      모델명을 주면 lang 은 무시되므로(rec 모델이 언어 결정) 함께 넘기지 않는다.
        kw: dict = dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
                        use_textline_orientation=False)
        if det_model:
            kw["text_detection_model_name"] = det_model
        if rec_model:
            kw["text_recognition_model_name"] = rec_model
        if not (det_model or rec_model):
            kw["lang"] = lang
        try:
            self._ocr = PaddleOCR(**kw)
        except (TypeError, ValueError):
            self._ocr = PaddleOCR(lang=lang)   # 구버전 폴백

    def recognize(self, frame_bgr) -> list[OcrResult]:
        # PaddleOCR 3.x: predict() → OCRResult(dict-like, rec_texts/rec_scores/rec_polys)
        if hasattr(self._ocr, "predict"):
            out: list[OcrResult] = []
            for page in (self._ocr.predict(frame_bgr) or []):
                texts = page.get("rec_texts", []) or []
                scores = page.get("rec_scores", []) or []
                polys = page.get("rec_polys", None)
                if polys is None:
                    polys = page.get("dt_polys", []) or []
                for i, text in enumerate(texts):
                    if not text:
                        continue
                    box = polys[i] if i < len(polys) else None
                    bbox = quad_to_bbox([[float(p[0]), float(p[1])] for p in box]) \
                        if box is not None else (0, 0, 0, 0)
                    out.append((bbox, text, float(scores[i]) if i < len(scores) else 0.0))
            return out
        # 2.x 폴백
        res = self._ocr.ocr(frame_bgr, cls=True)
        out = []
        for line in (res[0] if res and res[0] else []):
            box, (text, score) = line
            out.append((quad_to_bbox(box), text, float(score)))
        return out


class EasyOCRBackend(OCRBackend):
    name = "easyocr"

    def __init__(self, languages: Optional[list[str]] = None) -> None:
        try:
            import easyocr
        except ImportError as e:
            raise ImportError("easyocr 필요: pip install easyocr") from e
        langs = ["ko", "en"]
        if languages:
            langs = ["ko" if x == "korean" else x for x in languages]
        self._ocr = easyocr.Reader(langs)

    def recognize(self, frame_bgr) -> list[OcrResult]:
        out: list[OcrResult] = []
        for box, text, score in self._ocr.readtext(frame_bgr):
            out.append((quad_to_bbox(box), text, float(score)))
        return out


_BACKENDS = {"rapidocr": RapidOCRBackend, "paddleocr": PaddleOCRBackend, "easyocr": EasyOCRBackend}
_FALLBACK_ORDER = ["rapidocr", "paddleocr", "easyocr"]


def make_ocr(name: str, languages: Optional[list[str]] = None,
             paddle_opts: Optional[dict] = None) -> OCRBackend:
    """이름으로 백엔드 생성. 실패 시 폴백 순서대로 시도."""
    if name not in _BACKENDS:
        raise ValueError(f"알 수 없는 OCR 백엔드: {name} (가능: {list(_BACKENDS)})")
    order = [name] + [b for b in _FALLBACK_ORDER if b != name]
    last_err: Optional[Exception] = None
    for cand in order:
        try:
            cls = _BACKENDS[cand]
            if cand == "rapidocr":
                backend = cls()
            elif cand == "paddleocr":
                backend = cls(languages, **{k: v for k, v in (paddle_opts or {}).items() if v})
            else:
                backend = cls(languages)
            if cand != name:
                log.warning("OCR 백엔드 '%s' 사용 불가 → '%s' 폴백", name, cand)
            return backend
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"사용 가능한 OCR 백엔드 없음. 마지막 오류: {last_err}")


# ── 스타일 추정 (numpy lazy) ─────────────────────────────────────────────
def estimate_style(frame_bgr, bbox: BBox, width: int, height: int) -> Style:
    """bbox 크기/위치로 스타일 추정. 색은 영역 밝은 픽셀 중앙값(실패 시 흰색)."""
    x1, y1, x2, y2 = bbox
    font_size = max(8, min(int(y2 - y1), height))
    color = (255, 255, 255)
    try:
        import numpy as np

        crop = frame_bgr[max(0, y1):max(1, y2), max(0, x1):max(1, x2)]
        if crop.size:
            flat = crop.reshape(-1, 3)
            lum = flat.mean(axis=1)
            bright = flat[lum >= np.percentile(lum, 60)]  # 글자는 보통 밝음
            b, g, r = np.median(bright if len(bright) else flat, axis=0)
            color = (int(r), int(g), int(b))
    except Exception:  # noqa: BLE001
        pass
    return Style(color=color, font_size=font_size,
                 position=position_bucket(bbox, width, height))


# ── 메인 탐지 ────────────────────────────────────────────────────────────
def detect(video: str, video_id: str, config: dict[str, Any],
           roi: Optional[BBox] = None, out_path: Optional[str] = None) -> DetectionDoc:
    """영상에서 텍스트 탐지 → DetectionDoc 반환(+ detections.json 저장)."""
    try:
        import cv2
    except ImportError as e:
        raise ImportError("opencv 필요: pip install opencv-python") from e

    dcfg = config.get("detect", {})
    sample_every = max(1, int(dcfg.get("sample_every", 15)))   # 0 이면 idx % sample_every 에서 ZeroDivision
    min_conf = float(dcfg.get("min_confidence", 0.5))
    backend_name = dcfg.get("ocr_backend", "rapidocr")
    languages = dcfg.get("languages", ["korean", "en"])
    roi = roi or (tuple(dcfg["roi"]) if dcfg.get("roi") else None)  # type: ignore[assignment]
    ocr_downscale_w = int(dcfg.get("ocr_downscale_width", 0))       # OCR 입력 가로 폭(0=원본)
    paddle_opts = {"det_model": dcfg.get("paddle_det_model"),
                   "rec_model": dcfg.get("paddle_rec_model")}

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise FileNotFoundError(f"영상 열기 실패: {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    ocr = make_ocr(backend_name, languages, paddle_opts=paddle_opts)
    warn = korean_ocr_warning(ocr.name, languages)
    if warn:
        log.warning(warn)
    log.info("탐지 시작 video_id=%s backend=%s roi=%s sample_every=%d",
             video_id, ocr.name, roi, sample_every)

    frames: list[FrameDetections] = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % sample_every == 0:
            fd = _detect_frame(ocr, frame, idx, idx / fps, width, height, roi, min_conf,
                               ocr_downscale_w)
            if fd.regions:
                frames.append(fd)
        idx += 1
    cap.release()

    doc = DetectionDoc(video_id=video_id, fps=round(fps, 6), width=width, height=height,
                       sample_every=sample_every, ocr_backend=ocr.name, roi=roi, frames=frames)
    flagged = sum(1 for f in frames for r in f.regions if r.flagged)
    log.info("탐지 완료: 샘플프레임 %d, 영역 %d (검수필요 %d)",
             len(frames), sum(len(f.regions) for f in frames), flagged)

    out = Path(out_path) if out_path else resolve_path(
        f"{config['paths']['outputs_dir']}/{video_id}/detections.json")
    doc.save(out)
    log.info("저장: %s", out)
    return doc


def _ocr_scaled(ocr: OCRBackend, img, downscale_w: int):
    """OCR 입력을 가로 downscale_w 로 축소해 인식(속도↑) → bbox 는 원본 좌표로 환원.

    반환: [(bbox_원본좌표, text, conf), ...]. downscale_w<=0 또는 이미 작으면 원본 그대로.
    """
    h, w = img.shape[:2]
    if downscale_w <= 0 or w <= downscale_w:
        return ocr.recognize(img)
    import cv2

    scale = downscale_w / w
    small = cv2.resize(img, (downscale_w, max(1, int(round(h * scale)))))
    out = []
    for bbox, text, conf in ocr.recognize(small):
        bx = tuple(int(round(c / scale)) for c in bbox)  # 원본 해상도로 환원
        out.append((bx, text, conf))
    return out


def _detect_frame(ocr: OCRBackend, frame, idx: int, ts: float, width: int, height: int,
                  roi: Optional[BBox], min_conf: float, downscale_w: int = 0) -> FrameDetections:
    regions: list[Region] = []
    if roi:
        x1, y1, x2, y2 = clamp_bbox(roi, width, height)
        crop = frame[y1:y2, x1:x2]
        text, conf = "", 0.0
        for (_, t, c) in _ocr_scaled(ocr, crop, downscale_w):
            if c >= conf:
                text, conf = t, c
        regions.append(Region(bbox=(x1, y1, x2, y2), text=text, confidence=conf,
                              style=estimate_style(frame, (x1, y1, x2, y2), width, height),
                              flagged=conf < min_conf))  # ROI 는 항상 마스킹 대상
    else:
        for bbox, text, conf in _ocr_scaled(ocr, frame, downscale_w):
            bbox = clamp_bbox(bbox, width, height)
            regions.append(Region(bbox=bbox, text=text, confidence=conf,
                                  style=estimate_style(frame, bbox, width, height),
                                  flagged=conf < min_conf))
    return FrameDetections(frame_idx=idx, timestamp=round(ts, 4), regions=regions)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="화면 텍스트 탐지·스타일 추출 → detections.json")
    p.add_argument("--video", required=True, help="입력 영상 경로")
    p.add_argument("--video-id", required=True)
    p.add_argument("--config", default=None, help="pipeline.config.yaml 경로")
    p.add_argument("--backend", default=None, help="rapidocr|paddleocr|easyocr (config 오버라이드)")
    p.add_argument("--subtitle-area", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"),
                   help="OCR 대신 고정 ROI 사용")
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    config = load_config(args.config)
    if args.backend:
        config.setdefault("detect", {})["ocr_backend"] = args.backend
    roi = tuple(args.subtitle_area) if args.subtitle_area else None
    detect(args.video, args.video_id, config, roi=roi, out_path=args.out)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
