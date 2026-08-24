"""[엔진] 원본-결과 프레임 쌍 비교 QC — '새로 생긴' 결함만 묻는 의미 게이트.

qa.py(PSNR/SSIM)·OCR 백체크는 국소 변화량/문자 정확도를 본다. 이 게이트는 LLM 비전으로
전체 프레임을 **원본과 짝지어** 비교해 두 가지만 판정한다:
1. korean_text_visible — 결과물에 오버레이(자막·타이틀·캡션) 한글 잔존
   (실물 소품에 인쇄된 한글 — 포장지·라벨·명찰 — 은 콘텐츠이므로 무시)
2. content_clipped — 원본에는 없는데 결과물에서 새로 생긴 피사체 잘림
   (원본에도 똑같이 잘려 있으면 연출이므로 통과)

맥6 실증(2026-08-05, 13편): 단독 프레임 검사는 원본 연출(상단 경계 클로즈업)을
잘림으로 오탐(7건). 원본 쌍 비교 전환 후 동일 세트 오탐 0, 실결함(콘텐츠 영역
한글 자막 번인 1건)은 유지 검출.

모션 정적성 헬퍼도 제공: 오버레이(타이틀·로고)는 전 구간 정지, 콘텐츠는 움직인다 →
프레임 샘플의 행별 시간축 변화로 콘텐츠 상단 경계를 측정(밴드 교체·마스크 검증용).

순수(테스트 대상): pair_timestamps / build_compare_prompt / parse_verdict /
row_motion_counts / content_top_from_motion. cv2/ffmpeg/LLM 은 lazy.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import ffmpeg_bin, ffprobe_bin, get_logger, resolve_path, write_json

log = get_logger("qa_compare")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def pair_timestamps(duration: float, n: int = 3, head: float = 1.0,
                    tail_margin: float = 1.5) -> list[float]:
    """비교 프레임 시각 n개 — 도입/중간/말미. 짧은 클립은 중복 없이 클램프."""
    if duration <= 0:
        return [0.0]
    pts = [min(head, duration / 2)]
    if n >= 2:
        pts.append(duration / 2)
    if n >= 3:
        pts.append(max(duration - tail_margin, 0.0))
    extra = max(0, n - 3)
    for i in range(extra):
        pts.append(duration * (i + 1) / (extra + 1))
    uniq = sorted({round(t, 2) for t in pts})
    return uniq


def build_compare_prompt(n_pairs: int) -> tuple[str, str]:
    """(system, user) — 이미지 순서는 [원본, 결과] 쌍 × n_pairs (같은 시점끼리)."""
    system = ("당신은 숏폼 현지화 QA 검사원이다. 반드시 JSON 하나만 출력한다: "
              '{"korean_text_visible": bool, "content_clipped": bool, "notes": str}')
    user = (
        f"이미지는 [원본, 현지화 결과] 쌍이 {n_pairs}세트다(같은 시점끼리).\n"
        "결과물은 원본의 한국어 오버레이를 제거/교체한 것이다. 검사하라:\n"
        "1. korean_text_visible: **결과물에** 오버레이로 새겨진(자막·타이틀·캡션 등 편집으로 "
        "얹힌) 한글 텍스트가 남아 있는가? 실물 소품(포장지·라벨·명찰·간판)에 인쇄된 한글은 "
        "콘텐츠의 일부이므로 무시하라. 일본어 가나·한자·영문도 무관.\n"
        "2. content_clipped: **원본에는 없는데 결과물에서 새로** 캐릭터·음식 등 피사체가 "
        "잘려 나갔는가? 원본에서도 똑같이 잘려 있으면 원래 연출이므로 false.\n"
        "notes 에 발견 사항을 한 줄로."
    )
    return system, user


def parse_verdict(text: str) -> dict[str, Any]:
    """LLM 응답에서 verdict JSON 추출(코드펜스/전후 잡음 허용). 실패 시 ValueError."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"verdict JSON 없음: {text[:200]!r}")
    d = json.loads(m.group(0))
    return {"korean_text_visible": bool(d.get("korean_text_visible")),
            "content_clipped": bool(d.get("content_clipped")),
            "notes": str(d.get("notes", ""))}


def verdict_pass(v: dict[str, Any]) -> bool:
    return not v["korean_text_visible"] and not v["content_clipped"]


def row_motion_counts(frames: list[list[int]], width: int, height: int,
                      col_stride: int = 2, diff_thresh: int = 24) -> list[int]:
    """프레임(그레이스케일 평면 배열) N장 → 행별 '움직인 픽셀' 수.

    오버레이·빈 배경은 시간축 변화가 없다(정지) → 변화가 나타나는 행이 콘텐츠다.
    """
    counts = []
    for row in range(height):
        motion = 0
        for col in range(0, width, col_stride):
            idx = row * width + col
            vals = [f[idx] for f in frames]
            if max(vals) - min(vals) > diff_thresh:
                motion += 1
        counts.append(motion)
    return counts


def content_top_from_motion(counts: list[int], min_hits: int = 5,
                            min_row: int = 0) -> Optional[int]:
    """행별 모션 수 → 콘텐츠가 시작하는 첫 행(없으면 None). min_row 아래부터 탐색."""
    for row in range(min_row, len(counts)):
        if counts[row] >= min_hits:
            return row
    return None


# ── 외부 의존 (ffmpeg / cv2 / LLM) ────────────────────────────────────────
def _duration(path: str) -> float:
    import subprocess

    r = subprocess.run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60)
    return float(r.stdout.strip() or 0)


def _frame_png(video: str, t: float, scale_w: int = 540) -> bytes:
    import subprocess
    import tempfile

    out = tempfile.mktemp(suffix=".png")
    r = subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
                        "-i", video, "-frames:v", "1", "-vf", f"scale={scale_w}:-1", out],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"프레임 추출 실패 t={t}: {r.stderr[-300:]}")
    return Path(out).read_bytes()


def compare(src_video: str, out_video: str, config: dict[str, Any],
            report_path: Optional[str] = None) -> dict[str, Any]:
    """원본-결과 프레임 쌍 비교 → verdict dict (+선택적 리포트 저장)."""
    from app.localize.overlay import llm

    qcfg = config.get("qa_compare", {})
    n = int(qcfg.get("pairs", 3))
    scale_w = int(qcfg.get("scale_w", 540))
    pts = pair_timestamps(_duration(out_video), n)
    images: list[tuple[bytes, str]] = []
    for t in pts:
        images.append((_frame_png(src_video, t, scale_w), "image/png"))
        images.append((_frame_png(out_video, t, scale_w), "image/png"))
    system, user = build_compare_prompt(len(pts))
    raw = llm.complete_vision(system, user, images, config, max_tokens=512)
    verdict = parse_verdict(raw)
    verdict["pass"] = verdict_pass(verdict)
    verdict["timestamps"] = pts
    if report_path:
        write_json(resolve_path(report_path), verdict)
    log.info("qa_compare: pass=%s (%s)", verdict["pass"], verdict["notes"][:80])
    return verdict


def measure_content_top(video: str, samples: int = 9, scale: tuple[int, int] = (135, 240),
                        min_hits: int = 5, margin_px: int = 8) -> Optional[int]:
    """모션 기반 콘텐츠 상단 경계(원본 해상도 y). 밴드 레이아웃 아니면 None 가능."""
    import subprocess
    import tempfile

    try:
        import cv2
    except ImportError as e:
        raise ImportError("opencv-python 필요: pip install opencv-python") from e

    w, h = scale
    dur = _duration(video)
    frames: list[list[int]] = []
    for i in range(samples):
        t = dur * (i + 0.5) / samples
        out = tempfile.mktemp(suffix=".png")
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video,
                        "-frames:v", "1", "-vf", f"scale={w}:{h}", out],
                       capture_output=True, timeout=120)
        img = cv2.imread(out, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        frames.append(img.flatten().tolist())
    if len(frames) < 3:
        raise RuntimeError("프레임 샘플 부족 — 영상 확인")
    counts = row_motion_counts(frames, w, h)
    row = content_top_from_motion(counts, min_hits=min_hits)
    if row is None:
        return None
    # 스케일 좌표 → 원본(세로 1920 기준) 좌표로 환산 후 여유 margin
    src_h = 1920
    return max(0, int(row / h * src_h) - margin_px)
