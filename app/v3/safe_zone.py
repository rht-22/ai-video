"""폰 재생 화면 안전 박스(v3, 2026-09-18 사용자 결정).

쇼츠 재생 화면은 영상을 **세로에 맞춰 채우고 좌우를 잘라낸다**. 가왕쇼 8화 어시장 편 아이폰 스크린샷을
같은 편 완성본 프레임에 맞대어 잰 값(캔버스 1080×1920 기준):
  · 좌우 잘림 약 47px(아이폰) — 화면비로 계산한 긴 안드로이드(20:9)는 약 83px
  · 위: 뒤로가기·검색·메뉴 아이콘이 약 200 까지 덮는다
  · 아래: 채널명·설명·댓글 줄이 약 1620 부터 덮는다 · 오른쪽 버튼 열 x 920~ · y 1000~1760
실사고: 가왕쇼 「티빙」 표기(밴드 모서리 24px)의 「빙」이 잘렸고, 제목이 58~1021px 로 거의 끝까지 찼다.

사용자 결정(2026-09-18):
  1. 제목·플랫폼 표기·라벨·자막 등 **글자는 가로 100~980** 안.
  2. 제목은 **글자 크기를 줄이지 않는다 — 글자 수를 줄인다**(스토리 걸음 2 가 폭으로 반려한다).
  3. 영상 밴드는 지금처럼 꽉 채운다. 대신 **인물 얼굴이 잘리는 가장자리에 걸리면 경고**한다
     (배치 주의 — 고치는 건 사람/손편집의 crop_x 몫).
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

SAFE_X0 = 100                 # 글자 요소 왼쪽 한계(캔버스 px)
SAFE_X1 = 980                 # 글자 요소 오른쪽 한계
SAFE_WIDTH = SAFE_X1 - SAFE_X0
SAFE_TOP = 200                # 제목 블록 윗변 하한(상단 아이콘)
SAFE_BOTTOM_CORE = 1600       # 핵심 요소(대사·내레이션 자막) 아랫변 상한(하단 채널명·설명)
TITLE_SPACE_RATIO = 0.28      # 제목 폰트(Jalnan 계열) 공백 폭 ÷ 글자 크기 — 글자 수 안내용
EDGE_FACE_CUT_RATIO = 0.3     # 얼굴 폭의 이만큼 이상이 가장자리 띠(0~100 · 980~1080)에 들어가면 경고
EDGE_FACE_FPS = 1.0           # 완성본 점검 표본 fps
EDGE_FACE_MIN_PX = 40         # 이보다 작은 얼굴(캔버스 px)은 보지 않는다(군중·오검출)
EDGE_FACE_SCORE = 0.8         # YuNet 점수 하한(라벨 회피 0.7 보다 엄격 — 경고 오검출을 줄인다)


def line_width_px(text: str, font_path: str | None, size: int) -> float:
    """제목 한 줄의 실제 폭(px) — 렌더러와 같은 Pillow 실측. 폰트가 파일이 아니면 한글 1em·공백 0.28em 근사."""
    if font_path and Path(str(font_path)).is_file():
        from app.modules.renderer import _measure_title_text_width
        return float(_measure_title_text_width(str(text), str(font_path), int(size)))
    return sum(TITLE_SPACE_RATIO * size if ch.isspace() else size for ch in str(text))


def title_char_budget(size: int) -> int:
    """이 크기에서 안전 폭에 들어가는 한글 글자 수(공백 1개 포함 가정) — 프롬프트 안내용. 판정은 line_width_px."""
    return max(1, int(math.floor((SAFE_WIDTH - TITLE_SPACE_RATIO * size) / float(size))))


def title_overflow(lines: list[str], font_path: str | None, sizes: list[int]) -> list[dict]:
    """줄별로 안전 폭을 넘는 것만 [{line, text, width, size, over_px, cut_chars}] — 순수."""
    out = []
    for i, ln in enumerate(lines):
        if i >= len(sizes) or not str(ln).strip():
            continue
        size = int(sizes[i])
        w = line_width_px(str(ln).strip(), font_path, size)
        if w > SAFE_WIDTH:
            out.append({"line": i + 1, "text": str(ln).strip(), "width": round(w), "size": size,
                        "over_px": round(w - SAFE_WIDTH),
                        "cut_chars": max(1, int(math.ceil((w - SAFE_WIDTH) / float(size))))})
    return out


def edge_cut_ratio(x0: float, x1: float, *, canvas_w: int = 1080) -> float:
    """얼굴 박스 가로 구간 중 가장자리 띠(0~SAFE_X0 · SAFE_X1~canvas_w)에 들어간 비율. 순수."""
    w = max(1e-6, x1 - x0)
    cut = max(0.0, min(x1, SAFE_X0) - max(x0, 0.0)) + max(0.0, min(x1, canvas_w) - max(x0, SAFE_X1))
    return cut / w


def edge_faces_in_video(video_path: Path, band_top: int, band_bottom: int, *,
                        fps: float = EDGE_FACE_FPS, canvas_w: int = 1080, canvas_h: int = 1920,
                        require_success: bool = False
                        ) -> list[dict]:
    """완성본을 fps 로 훑어 **밴드 안 인물 얼굴이 폰 잘림 가장자리에 걸린 구간**을 돌려준다.

    완성본 프레임에서 직접 재므로 크롭 방식(화자 추적·고정·줌·fit)과 무관하게 맞다. 반폭(540×960)으로
    디코드해 YuNet(없으면 Haar)으로 검출. 반환 [{start, end, side, max_cut, box}] — 연속한 초는 합친다.
    cv2·ffmpeg 가 없거나 실패하면 [](점검은 안전장치 — 렌더를 막지 않는다)."""
    try:
        import cv2
        import numpy as np
        from app.v3.label_faces import YUNET_PATH, _detect
        from app.modules.reframe import yunet_plausible
    except ImportError:
        if require_success: raise
        return []

    def _faces(im):
        # YuNet + 랜드마크 타당성(reframe.yunet_plausible — 뒤통수·로고 글자에서 랜드마크가 한 점으로 붕괴한다).
        # 가왕쇼 실측: 좌상단 방송 로고 「가왕쇼」·오른쪽 표기 글자가 점수만으로는 얼굴로 잡혔다. 없으면 Haar.
        h, w = im.shape[:2]
        if YUNET_PATH.is_file():
            try:
                det = cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (w, h), EDGE_FACE_SCORE, 0.3, 5000)
                det.setInputSize((w, h))
                _, rows = det.detect(im)
                out = []
                for r in (rows if rows is not None else []):
                    if yunet_plausible(r) and min(r[2], r[3]) >= max(8, EDGE_FACE_MIN_PX // 2):
                        out.append(tuple(int(v) for v in r[:4]))
                return out
            except Exception:  # noqa: BLE001
                pass
        return _detect(cv2, im, min_size=max(8, EDGE_FACE_MIN_PX // 2))
    hw, hh = canvas_w // 2, canvas_h // 2
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video_path), "-vf", f"fps={fps},scale={hw}:{hh}",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, check=True).stdout
    except Exception:  # noqa: BLE001
        if require_success: raise
        return []
    frame_bytes = hw * hh * 3
    if require_success and (not raw or len(raw) % frame_bytes):
        raise ValueError("얼굴 점검용 프레임을 완전히 디코드하지 못함")
    hits: list[dict] = []
    for k in range(len(raw) // frame_bytes):
        im = np.frombuffer(raw[k * frame_bytes:(k + 1) * frame_bytes], dtype=np.uint8).reshape(hh, hw, 3)
        try:
            faces = _faces(im)
        except Exception:  # noqa: BLE001
            if require_success: raise
            continue
        t = k / float(fps)
        for (x, y, w, h) in faces:
            x0, x1, cy = x * 2.0, (x + w) * 2.0, (y + h / 2.0) * 2.0
            if not (band_top <= cy <= band_bottom):
                continue                                  # 밴드 밖(제목·로고 영역)의 오검출
            r = edge_cut_ratio(x0, x1, canvas_w=canvas_w)
            if r >= EDGE_FACE_CUT_RATIO:
                side = "left" if (x0 + x1) / 2 < canvas_w / 2 else "right"
                hits.append({"t": round(t, 2), "side": side, "cut": round(r, 2),
                             "box": [round(x0), round(y * 2.0), round(x1), round((y + h) * 2.0)]})
    ranges: list[dict] = []
    for h in hits:
        last = ranges[-1] if ranges else None
        if last and last["side"] == h["side"] and h["t"] - last["end"] <= 1.0 / fps + 1e-6:
            last["end"] = h["t"]
            if h["cut"] > last["max_cut"]:
                last["max_cut"], last["box"] = h["cut"], h["box"]
        else:
            ranges.append({"start": h["t"], "end": h["t"], "side": h["side"], "max_cut": h["cut"], "box": h["box"]})
    return ranges


__all__ = ["SAFE_X0", "SAFE_X1", "SAFE_WIDTH", "SAFE_TOP", "SAFE_BOTTOM_CORE", "line_width_px",
           "title_char_budget", "title_overflow", "edge_cut_ratio", "edge_faces_in_video",
           "EDGE_FACE_CUT_RATIO"]
