"""[엔진①] 텍스트 탐지·인식·스타일 추출.

프레임을 N개마다 샘플링 → OCR 로 텍스트 영역(bbox)·내용·대략 스타일 추출 → detections.json.
- OCR 백엔드 팩토리: rapidocr(기본) → paddleocr → easyocr.
- ROI 수동 지정 모드: --subtitle-area x1 y1 x2 y2 (스타일화된 밈 폰트로 OCR 실패할 때).
- 신뢰도 낮은 항목은 flagged=True 로 표시(사람 검수 게이트).

무거운 의존성(cv2/numpy/OCR)은 lazy import. 순수 헬퍼(position_bucket 등)는 의존성 없이 동작.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import (get_logger, load_config, resolve_path,
                                         write_json)
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
# ── 현지화 대상 판정 (2026-08-26 실측으로 신설 — vlp 에 없다) ────────────────
#
# 🛑 실사고: 잔망루피 `a6wO8o91Oi0`(route B) 완성본에 인형 몸통 한가운데 얼룩이 남았다.
#    `ja_events.json` 13건이 **전부 글자가 아니었다**: `-` `・` `ATAL` `2` `U` `AI`
#    `：` `'9` `L`. 인형 표면의 무늬·경계·반사를 OCR 이 글자로 잡은 것이다.
#
#    route B 는 "지우고 그 자리에 일본어를 그린다"라서, 그 노이즈 자리를 **인페인팅으로
#    지우고** 같은 노이즈를 다시 그렸다. 화면에 남은 것이 그 얼룩이다.
#
#    구조적 원인: 파이프라인이 마스크·인페인팅을 번역보다 **먼저** 하고, 마스크는
#    탐지된 모든 상자를 쓴다(`mask.build_masks`). 즉 '지울지'를 '번역해서 그릴 것인가'와
#    무관하게 정한다. 그래서 판정을 **탐지가 끝나는 이 지점 한 곳**에서 한다 —
#    여기서 걸러야 마스크·렌더·검수 카드가 **같은 목록**을 본다.
#
# 규율: 버린 것은 조용히 사라지지 않는다(`detections_dropped.json` + 로그).
_SCRIPT_RE = {
    "ko": re.compile(r"[가-힣ㄱ-ㆎ]"),
    "ja": re.compile(r"[぀-ゟ゠-ヿ㐀-䶿一-鿿]"),
    "en": re.compile(r"[A-Za-z]"),
}


def localizable(text: str, confidence: float, bbox, *, min_conf: float = 0.0,
                min_area_px: int = 0, source_lang: str = "ko") -> bool:
    """이 탐지가 **현지화 대상**인가 — 지우고 그 자리에 번역을 그릴 것인가. 순수.

    셋을 본다: ① 소스 언어 문자가 실제로 들어 있는가 ② 신뢰도 ③ 상자 넓이.
    ①이 핵심이다 — 우리가 지우는 이유는 그 자리에 번역을 그리려는 것이고, 소스 문자가
    없으면 번역할 것이 없다(그대로 다시 그리게 된다). 모르는 언어면 ①은 건너뛴다.
    """
    txt = str(text or "").strip()
    if not txt:
        return False
    if float(confidence or 0.0) < float(min_conf):
        return False
    try:
        x1, y1, x2, y2 = (int(v) for v in bbox)
        if (x2 - x1) * (y2 - y1) < int(min_area_px):
            return False
    except (TypeError, ValueError):
        pass                     # 상자가 깨졌으면 넓이로 버리지 않는다(오판 금지)
    rx = _SCRIPT_RE.get(str(source_lang or "").lower())
    return bool(rx.search(txt)) if rx else True


def text_persistence(doc: DetectionDoc) -> dict:
    """텍스트 → 그것이 나타난 **샘플 프레임 수**. 순수.

    같은 문구가 여러 위치에 잡혀도 한 프레임에서는 1로 센다(같은 자막이다)."""
    seen: dict = {}
    for f in doc.frames:
        for t in {str(r.text or "").strip() for r in f.regions}:
            if t:
                seen[t] = seen.get(t, 0) + 1
    return seen


def filter_localizable(doc: DetectionDoc, config: dict[str, Any]) -> tuple:
    """현지화 대상만 남긴 DetectionDoc + 버린 목록. 순수(사본을 만든다).

    ⚠ ROI(`--subtitle-area`)를 사람이 지정한 실행은 **거르지 않는다** — 사람이 그
    사각형을 자막이라고 말한 것이다(detect 의 'ROI 는 항상 마스킹 대상' 규약)."""
    dcfg = config.get("detect", {})
    if not bool(dcfg.get("localizable_only", True)) or doc.roi:
        return doc, []
    kw = {"min_conf": float(dcfg.get("mask_min_confidence",
                                     dcfg.get("min_confidence", 0.5))),
          "min_area_px": int(dcfg.get("min_area_px", 400)),
          "source_lang": str(dcfg.get("source_lang", "ko"))}
    # 🛑 두 번째 축: **한 샘플에만 나타난 탐지는 버린다.**
    #    실측 2026-08-26 — 걸러야 했던 탐지가 **전부** 정확히 한 샘플(0.50초)짜리였다
    #    (13건 + 재실행의 `'은` 1건). 화면에 실제로 박힌 글자는 여러 샘플에 걸쳐 남는다.
    #    문자 종류·크기·신뢰도로는 못 잡는 것이 이것이다: `'은` 은 한글이고 상자도
    #    211×169 로 컸다 — 다만 **딱 한 번 보였다**. 사용자 확인: 그런 말은 영상에 없다.
    #    ⚠ 대가: 0.5초만 스치는 진짜 자막은 함께 버려진다. `min_frames: 1` 로 끈다.
    min_frames = max(1, int(dcfg.get("min_frames", 2)))
    persist = text_persistence(doc) if min_frames > 1 else {}
    dropped, frames = [], []
    for f in doc.frames:
        keep = []
        for r in f.regions:
            txt = str(r.text or "").strip()
            n_seen = persist.get(txt, 0) if min_frames > 1 else min_frames
            if localizable(r.text, r.confidence, r.bbox, **kw) and n_seen >= min_frames:
                keep.append(r)
            else:
                why = ("한 샘플만 보임" if n_seen and n_seen < min_frames
                       else "현지화 대상 아님")
                dropped.append({"frame_idx": f.frame_idx, "timestamp": f.timestamp,
                                "bbox": list(r.bbox), "text": r.text, "why": why,
                                "frames_seen": n_seen,
                                "confidence": round(float(r.confidence or 0.0), 3)})
        if keep:
            frames.append(replace(f, regions=keep))
    return replace(doc, frames=frames), dropped


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
    doc, dropped = filter_localizable(doc, config)
    if dropped:
        # 조용한 드롭 금지 — 무엇을 왜 버렸는지 파일과 로그 양쪽에 남긴다.
        write_json({"video_id": video_id, "reason": "현지화 대상 아님(문자·신뢰도·크기)",
                    "dropped": dropped}, out.parent / "detections_dropped.json")
        sample = " · ".join(repr(d["text"]) for d in dropped[:8])
        log.warning("현지화 대상 아님으로 %d건 제외(지우지도 그리지도 않는다): %s",
                    len(dropped), sample)
        log.info("남은 영역 %d (샘플프레임 %d)",
                 sum(len(f.regions) for f in doc.frames), len(doc.frames))
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
