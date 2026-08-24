"""[엔진②-a] 마스크 생성 + temporal smoothing.

detections.json 의 bbox → 프레임별 이진 마스크. 슬라이딩 윈도우로 깜빡임/누락 보정.
detect 는 N프레임마다 샘플링하므로, 마스크는 샘플 구간을 'hold' 해서 전 프레임에 채운다.

순수 기하 헬퍼(iou/dilate/merge/smooth)는 의존성 없이 테스트 가능.
래스터화(rasterize_mask, build_masks)만 numpy/cv2 사용(lazy).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import ensure_dir, get_logger, resolve_path
from app.localize.overlay.schemas import BBox, DetectionDoc

log = get_logger("mask")


# ── 순수 기하 헬퍼 (의존성 없음) ─────────────────────────────────────────
def iou(a: BBox, b: BBox) -> float:
    """두 axis-aligned bbox 의 IoU."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union else 0.0


def dilate_bbox(bbox: BBox, px: int, width: int, height: int) -> BBox:
    """bbox 를 px 만큼 팽창(프레임 경계 클램프)."""
    x1, y1, x2, y2 = bbox
    return (max(0, x1 - px), max(0, y1 - px),
            min(width, x2 + px), min(height, y2 + px))


def merge_boxes(boxes: list[BBox], iou_thresh: float = 0.6) -> list[BBox]:
    """IoU 임계 이상으로 겹치는 박스들을 합집합(bounding box)으로 병합."""
    remaining = list(boxes)
    merged: list[BBox] = []
    while remaining:
        cur = remaining.pop(0)
        changed = True
        while changed:
            changed = False
            keep: list[BBox] = []
            for b in remaining:
                if iou(cur, b) >= iou_thresh:
                    cur = (min(cur[0], b[0]), min(cur[1], b[1]),
                           max(cur[2], b[2]), max(cur[3], b[3]))
                    changed = True
                else:
                    keep.append(b)
            remaining = keep
        merged.append(cur)
    return merged


def smooth_temporal(frames: list[tuple[int, list[BBox]]], window: int,
                    strategy: str = "union", vote_ratio: float = 0.4) -> dict[int, list[BBox]]:
    """샘플 프레임들의 박스를 슬라이딩 윈도우로 평활화.

    frames: [(frame_idx, [bbox,...]), ...] (frame_idx 오름차순)
    union : 윈도우 내 모든 박스 합집합(병합) — 누락/깜빡임 메움(권장 기본).
    vote  : 윈도우 내 vote_ratio 이상 프레임에서 (IoU>0.3) 등장한 박스만 — 오탐 억제.
    반환: {frame_idx: [bbox,...]}
    """
    half = max(0, window // 2)
    out: dict[int, list[BBox]] = {}
    for i, (idx, _boxes) in enumerate(frames):
        lo, hi = max(0, i - half), min(len(frames), i + half + 1)
        neighbor_lists = [frames[j][1] for j in range(lo, hi)]
        pool = [b for lst in neighbor_lists for b in lst]
        if strategy == "vote":
            need = max(1, math.ceil(vote_ratio * len(neighbor_lists)))
            kept: list[BBox] = []
            for b in pool:
                hits = sum(1 for lst in neighbor_lists if any(iou(b, o) > 0.3 for o in lst))
                if hits >= need:
                    kept.append(b)
            out[idx] = merge_boxes(kept)
        else:  # union
            out[idx] = merge_boxes(pool)
    return out


# ── 래스터화 (numpy/cv2 lazy) ────────────────────────────────────────────
def rasterize_mask(boxes: list[BBox], width: int, height: int, dilate_px: int = 0):
    """박스 목록 → uint8 마스크(255=제거영역). numpy ndarray 반환."""
    import numpy as np

    mask = np.zeros((height, width), dtype=np.uint8)
    for b in boxes:
        x1, y1, x2, y2 = dilate_bbox(b, dilate_px, width, height) if dilate_px else b
        mask[y1:y2, x1:x2] = 255
    return mask


def build_masks(doc: DetectionDoc, config: dict[str, Any], out_dir: Optional[str] = None,
                total_frames: Optional[int] = None) -> Path:
    """전 프레임 마스크 PNG 시퀀스 생성. 반환: 마스크 디렉토리."""
    try:
        import cv2  # noqa: F401
    except ImportError as e:
        raise ImportError("opencv 필요: pip install opencv-python") from e
    import cv2

    mcfg = config.get("mask", {})
    window = int(mcfg.get("temporal_window", 5))
    strategy = mcfg.get("temporal_strategy", "union")
    vote_ratio = float(mcfg.get("vote_ratio", 0.4))

    # 샘플 프레임별 박스 수집 → 평활화
    sampled = [(f.frame_idx, [r.bbox for r in f.regions]) for f in doc.frames]
    smoothed = smooth_temporal(sampled, window, strategy, vote_ratio)

    out = ensure_dir(out_dir or resolve_path(
        f"{config['paths']['outputs_dir']}/{doc.video_id}/masks"))

    n = total_frames or ((max((f.frame_idx for f in doc.frames), default=0) + doc.sample_every))
    step = doc.sample_every
    written = 0
    for i in range(n):
        key = (i // step) * step                      # 샘플 구간 hold
        boxes = smoothed.get(key, [])
        mask = rasterize_mask(boxes, doc.width, doc.height, int(mcfg.get("dilate_px", 8)))
        cv2.imwrite(str(out / f"{i:06d}.png"), mask)
        written += 1
    log.info("마스크 %d장 생성 (%s smoothing, window=%d) → %s", written, strategy, window, out)
    return out
