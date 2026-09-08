"""라벨 얼굴 회피(v3, 2026-09-08 사용자 지적) — 라벨이 인물 얼굴을 가린다.

Stage 4 는 6fps 저해상 초안을 보고 라벨 `x·y` 를 적고, 라벨 프로브는 표정·타이밍만 검증한다 —
가림은 아무도 보지 않았다(v1 E19-4 는 리프레임 얼굴 박스를 쓰는데 v3 에는 그 박스가 없고,
가왕쇼 채널은 얼굴 추적 자체가 꺼져 있다). 그래서 **라벨 시각의 초안 프레임에서 얼굴을 직접
검출**해(OpenCV Haar, 결정적, 의존 추가 없음 — cv2 는 이미 requirements) 캔버스 좌표로 환산하고,
라벨 박스가 얼굴과 겹치면 **위 → 얼굴 반대쪽 옆 → 같은 쪽 옆 → 아래** 순으로 첫 성립 자리로
옮긴다(E19-4 와 같은 후보 순서). 성립 = 모든 얼굴과 비겹침 + 밴드 안 + 캔버스 가로 안.
어느 후보도 안 되면 **옮기지 않고 기록**(살리고 당긴다 — 겹침이 증발보다 낫다).
검출 실패·cv2 부재·프레임 못 뜸 = 아무것도 안 함(안전장치가 연출을 막지 않는다).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

FACE_GAP_PX = 14          # 라벨 박스와 얼굴 박스 사이 최소 여백(캔버스 px)
FACE_PAD_RATIO = 0.15     # 검출 박스를 이만큼 키워서 본다(Haar 박스는 이마·턱을 조금 자른다)
BAND_PAD_PX = 12          # 밴드 위·아래 안쪽 여백
LABEL_H_RATIO = 0.75      # 라벨 박스 반높이 = size × 이 값(외곽선·pop 확대 포함)
SAMPLE_OFFSETS = (0.05, 0.5)   # 라벨 창 안에서 프레임을 뜨는 지점(비율) — 시작 직후·중간


def face_boxes_at(draft_path: Path, t_sec: float, *, min_size: int = 24) -> list[tuple[int, int, int, int]]:
    """초안 t 초 프레임의 얼굴 박스 [(x, y, w, h)] — 초안 픽셀 좌표. 실패는 []."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t_sec):.3f}", "-i", str(draft_path),
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, check=True)
        arr = np.frombuffer(proc.stdout, dtype=np.uint8)
        im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if im is None:
            return []
        return _detect(cv2, im, min_size=min_size)
    except Exception:  # noqa: BLE001 — 검출은 안전장치, 실패는 무회피
        return []


YUNET_PATH = Path(__file__).resolve().parent.parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
YUNET_SCORE = 0.7


def detector_name() -> str:
    return "yunet" if YUNET_PATH.is_file() else "haar"


def _detect(cv2, im, *, min_size: int) -> list[tuple[int, int, int, int]]:
    """YuNet(ONNX 번들이 있으면 — 옆얼굴·작은 얼굴에 강함) → 없으면 Haar 정면 캐스케이드.
    둘 다 OpenCV 내장 API 라 의존 추가 없음, 결정적."""
    h, w = im.shape[:2]
    if YUNET_PATH.is_file():
        try:
            det = cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (w, h), YUNET_SCORE, 0.3, 5000)
            det.setInputSize((w, h))
            _, faces = det.detect(im)
            out = []
            for f in (faces if faces is not None else []):
                x, y, fw, fh = (int(v) for v in f[:4])
                if fw >= min_size and fh >= min_size:
                    out.append((max(0, x), max(0, y), fw, fh))
            return out
        except Exception:  # noqa: BLE001 — YuNet 실패는 Haar 로
            pass
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = casc.detectMultiScale(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), 1.1, 5,
                                  minSize=(min_size, min_size))
    return [tuple(int(v) for v in f) for f in faces]


def draft_dims(draft_path: Path) -> tuple[int, int] | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0",
                              str(draft_path)], capture_output=True, text=True, check=True).stdout
        w, h = (int(v) for v in out.strip().split(",")[:2])
        return (w, h) if w > 0 and h > 0 else None
    except Exception:  # noqa: BLE001
        return None


def draft_box_to_canvas(box, draft_w: int, draft_h: int, geom, *, pad_ratio: float = FACE_PAD_RATIO
                        ) -> tuple[float, float, float, float]:
    """초안(소스 전체 프레임) 픽셀 박스 → 캔버스 px (x0, y0, x1, y1). 밴드는 소스를 높이
    맞춤으로 키운 뒤 가로 가운데를 잘라낸 것(렌더러 [2]: scale increase → crop 중앙). 순수."""
    x, y, w, h = box
    px, py = w * pad_ratio, h * pad_ratio
    x0, y0, x1, y1 = x - px, y - py, x + w + px, y + h + py
    scale = geom.scaled_h / float(draft_h)
    src_w_scaled = draft_w * scale
    off_x = geom.pad_x - (src_w_scaled - geom.scaled_w) / 2.0
    return (x0 * scale + off_x, y0 * scale + geom.top, x1 * scale + off_x, y1 * scale + geom.top)


def label_box(label: dict, *, canvas_w: int = 1080, canvas_h: int = 1920
              ) -> tuple[float, float, float, float]:
    """라벨(x·y 비율 = 글자 중심) → 캔버스 px 박스. 반폭은 stage4.label_x_range 와 같은 자."""
    from app.v3.stage4 import LABEL_CHAR_W, LABEL_EDGE_PAD, LABEL_FX_OVERSHOOT, LABEL_SIZE
    size = int(label.get("size") or LABEL_SIZE)
    half_w = (len(str(label.get("text") or "")) * size * LABEL_CHAR_W * LABEL_FX_OVERSHOOT) / 2
    half_w += size * 0.07 + LABEL_EDGE_PAD
    half_h = size * LABEL_H_RATIO
    cx, cy = float(label.get("x", 0.5)) * canvas_w, float(label.get("y", 0.5)) * canvas_h
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _overlaps(a, b, gap: float = FACE_GAP_PX) -> bool:
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def avoid_faces(label: dict, faces: list[tuple[float, float, float, float]], geom, *,
                canvas_w: int = 1080, canvas_h: int = 1920) -> tuple[dict, dict | None]:
    """라벨 하나를 얼굴 박스들(캔버스 px)에서 비켜 놓는다. 순수.
    반환 (라벨, 기록|None). 기록 = {hit, moved, from, to, why}."""
    box = label_box(label, canvas_w=canvas_w, canvas_h=canvas_h)
    hit = [f for f in faces if _overlaps(box, f)]
    if not hit:
        return label, None
    half_w, half_h = (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
    lo_y, hi_y = geom.top + BAND_PAD_PX + half_h, geom.bottom - BAND_PAD_PX - half_h
    lo_x, hi_x = half_w, canvas_w - half_w
    cx0, cy0 = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    # 겹친 얼굴들의 합집합 기준으로 후보를 만든다
    fx0, fy0 = min(f[0] for f in hit), min(f[1] for f in hit)
    fx1, fy1 = max(f[2] for f in hit), max(f[3] for f in hit)
    fcx = (fx0 + fx1) / 2
    gap = FACE_GAP_PX + 2
    far_side_x = fx1 + gap + half_w if fcx <= canvas_w / 2 else fx0 - gap - half_w   # 얼굴 반대쪽
    near_side_x = fx0 - gap - half_w if fcx <= canvas_w / 2 else fx1 + gap + half_w
    candidates = [
        ("above", fcx, fy0 - gap - half_h),
        ("above-here", cx0, fy0 - gap - half_h),
        ("side-far", far_side_x, cy0),
        ("side-near", near_side_x, cy0),
        ("below", fcx, fy1 + gap + half_h),
    ]
    for why, cx, cy in candidates:
        cx = min(max(cx, lo_x), hi_x)
        if not (lo_y <= cy <= hi_y):
            continue
        cand = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
        if any(_overlaps(cand, f) for f in faces):
            continue
        moved = {**label, "x": round(cx / canvas_w, 4), "y": round(cy / canvas_h, 4)}
        return moved, {"hit": len(hit), "moved": True, "from": [label.get("x"), label.get("y")],
                       "to": [moved["x"], moved["y"]], "why": why}
    return label, {"hit": len(hit), "moved": False, "from": [label.get("x"), label.get("y")],
                   "to": None, "why": "성립하는 자리 없음 — 그대로 둠(기록)"}


def avoid_faces_for_labels(labels: list[dict], draft_path: Path, geom, *,
                           canvas_w: int = 1080, canvas_h: int = 1920, log=print) -> tuple[list[dict], list[dict]]:
    """렌더 직전 한 번 — 라벨마다 창 안 표본 프레임(시작 직후·중간)의 얼굴 합집합으로 회피.
    결정적(같은 초안·같은 라벨 = 같은 결과)이라 체크포인트에 안 남긴다. 반환 (라벨, 기록)."""
    if not labels:
        return labels, []
    dims = draft_dims(Path(draft_path)) if Path(draft_path).exists() else None
    if not dims:
        log("  [v3/label-face] 초안을 못 읽어 얼굴 회피 생략")
        return labels, []
    dw, dh = dims
    out, records = [], []
    for lb in labels:
        a, z = float(lb["start_sec"]), float(lb["end_sec"])
        faces: list[tuple[float, float, float, float]] = []
        for r in SAMPLE_OFFSETS:
            for f in face_boxes_at(Path(draft_path), a + (z - a) * r):
                faces.append(draft_box_to_canvas(f, dw, dh, geom))
        moved, rec = avoid_faces(lb, faces, geom, canvas_w=canvas_w, canvas_h=canvas_h)
        out.append(moved)
        if rec:
            rec = {**rec, "text": lb.get("text"), "start_sec": a, "faces": len(faces)}
            records.append(rec)
            log(f"  [v3/label-face] {lb.get('text')!r} @{a:.1f}s 얼굴 {rec['hit']}개와 겹침 → "
                + (f"{rec['why']} ({rec['from']} → {rec['to']})" if rec["moved"] else rec["why"]))
    return out, records
