"""소스 레터박스(그림 영역) 검출 — v3 probe 단계 (2026-09-07).

배경(docs/v3_gaps_from_manual_shorts.md「자산 레터박스」): EP01 소스는 1920×1080
컨테이너에 그림이 **위아래 59px 검은 띠** 사이 1920×962 다. v3 크롭은 `crop_h = src_h`
로 잘라 완성본 밴드 안에 그 띠가 그대로 들어갔다(실측: 그림이 450+59 행부터).

판정 규칙(참고 구현 ~/premiere_claude/probe_source.py 그대로):
  · 영상 전체에 고르게 뿌린 표본 프레임(`dur*(i+0.5)/n`)의 행/열 **최대 밝기**를
    프레임에 걸쳐 누적한다 → "모든 프레임에서 항상 검은 행/열"만 띠다. 밝은 장면이
    하나라도 섞이면 그 행은 그림이다(ffmpeg cropdetect 는 어두운 장면 하나로 그림까지
    잘라내서 못 쓴다).
  · THRESH 12 이하 = 검정(압축 잡음 여유), 띠와 그림 사이 번지는 경계 EDGE_SKIP 줄은
    그림에서 제외, 위아래(좌우)는 **작은 쪽으로 대칭**(중심이 틀어지면 팬이 어긋난다).
  · x·y·w·h 는 짝수 보정(ffmpeg crop/yuv420 제약) — pad 는 올림(검은 줄이 남지 않게).

실패(ffmpeg 없음·프레임 0장)는 예외로 올린다 — 안전장치가 본편을 막으면 안 되므로
호출자(`detect_or_full`)가 사유를 기록하고 **전체 영역**으로 진행한다(조용한 폴백 금지).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command

THRESH = 12            # 이 밝기 이하를 '검정'으로 본다 (압축 잡음 여유)
EDGE_SKIP = 1          # 띠와 그림 사이 번지는 경계 1줄은 그림에서 제외
DEFAULT_SAMPLES = 16   # 영상 전체에 고르게 뿌리는 표본 프레임 수


class LetterboxProbeError(RuntimeError):
    """표본 프레임을 한 장도 못 읽었거나 ffmpeg/ffprobe 가 없다."""


def full_area(width: int, height: int) -> dict:
    """띠 없음 = 컨테이너 전체."""
    return {"x": 0, "y": 0, "w": int(width), "h": int(height)}


def is_full(picture: dict | None, width: int, height: int) -> bool:
    """그림 영역이 컨테이너 전체인가(= 종전 경로, 회귀 0 조건)."""
    if not picture:
        return True
    try:
        return (int(picture["x"]) == 0 and int(picture["y"]) == 0
                and int(picture["w"]) == int(width) and int(picture["h"]) == int(height))
    except (KeyError, TypeError, ValueError):
        return True


def _span(arr: list[int], total: int, thresh: int, edge_skip: int) -> tuple[int, int]:
    """한 축의 누적 최대 밝기 → (pad, 길이). 순수."""
    a = 0
    while a < total and arr[a] <= thresh:
        a += 1
    b = total - 1
    while b > a and arr[b] <= thresh:
        b -= 1
    if a >= total:                     # 전부 검정(판정 불가) — 띠 없음으로 본다
        return 0, total
    if a > 0:
        a += edge_skip                 # 번짐 경계 한 줄 버리기
    if b < total - 1:
        b -= edge_skip
    pad = max(0, min(a, total - 1 - b))   # 위아래(좌우) 대칭 — 작은 쪽
    pad += pad % 2                     # 짝수 올림(검은 줄이 남지 않게)
    length = (total - 2 * pad) & ~1
    if length <= 0:
        return 0, total
    return pad, length


def analyze_rows(rows_max: list[int], cols_max: list[int], W: int, H: int,
                 *, thresh: int = THRESH, edge_skip: int = EDGE_SKIP) -> dict:
    """행/열 누적 최대 밝기 → 그림 영역 `{x, y, w, h}`. 순수.

    rows_max[y] = 모든 표본 프레임에 걸친 y 행의 최대 밝기(0~255), cols_max 동형.
    띠가 없으면 `x=0, y=0, w=W, h=H`. 전부 짝수."""
    if len(rows_max) != H or len(cols_max) != W:
        raise ValueError(f"프로필 길이 불일치: rows {len(rows_max)}≠H {H} / "
                         f"cols {len(cols_max)}≠W {W}")
    y, h = _span(list(rows_max), H, thresh, edge_skip)
    x, w = _span(list(cols_max), W, thresh, edge_skip)
    return {"x": x, "y": y, "w": w, "h": h}


def _probe_geometry(video: Path) -> tuple[int, int, float]:
    ffprobe = find_ffmpeg_command("ffprobe")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-show_entries", "format=duration", "-of", "json", str(video)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise LetterboxProbeError(f"ffprobe 실패: {out.stderr.strip()[:200]}")
    meta = json.loads(out.stdout or "{}")
    streams = meta.get("streams") or []
    if not streams:
        raise LetterboxProbeError("비디오 스트림 없음")
    W, H = int(streams[0]["width"]), int(streams[0]["height"])
    dur = float((meta.get("format") or {}).get("duration") or 0.0)
    if W <= 0 or H <= 0:
        raise LetterboxProbeError(f"해상도 이상 {W}x{H}")
    return W, H, dur


def _accumulate(im, rows_max: list[int], cols_max: list[int]) -> None:
    """PIL 회색조 프레임의 행/열 최대 밝기를 누적한다(C-level getextrema — 전열 전수)."""
    W, H = im.size
    for y in range(H):
        m = im.crop((0, y, W, y + 1)).getextrema()[1]
        if m > rows_max[y]:
            rows_max[y] = m
    for x in range(W):
        m = im.crop((x, 0, x + 1, H)).getextrema()[1]
        if m > cols_max[x]:
            cols_max[x] = m


def detect_picture_area(video: Path, *, samples: int = DEFAULT_SAMPLES,
                        log=print) -> dict:
    """소스의 그림 영역 검출 → `{x, y, w, h, samples, width, height}`.

    samples 는 요청 장수, 반환의 `samples` 는 **실제로 읽은** 장수. 한 장도 못 읽으면
    LetterboxProbeError — 삼키지 않는다(호출자가 기록하고 전체 영역으로 간다)."""
    from PIL import Image

    video = Path(video)
    if not video.exists():
        raise LetterboxProbeError(f"소스 없음: {video}")
    W, H, dur = _probe_geometry(video)
    ffmpeg = find_ffmpeg_command("ffmpeg")
    n = max(1, int(samples))
    rows_max = [0] * H
    cols_max = [0] * W
    got = 0
    with tempfile.TemporaryDirectory(prefix="v3_letterbox_") as tmp:
        for i in range(n):
            t = dur * (i + 0.5) / n if dur > 0 else 0.0
            fn = os.path.join(tmp, f"p{i}.png")
            r = subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                 "-frames:v", "1", fn], capture_output=True)
            if r.returncode or not os.path.exists(fn):
                continue
            try:
                with Image.open(fn) as im:
                    g = im.convert("L")
                    if g.size != (W, H):        # 회전 메타 등으로 크기가 갈리면 표본 제외
                        continue
                    _accumulate(g, rows_max, cols_max)
            finally:
                try:
                    os.unlink(fn)
                except OSError:
                    pass
            got += 1
    if got == 0:
        raise LetterboxProbeError(f"표본 프레임을 한 장도 못 읽었다(요청 {n}장)")
    area = analyze_rows(rows_max, cols_max, W, H)
    return {**area, "samples": got, "width": W, "height": H}


def describe(picture: dict, width: int, height: int) -> str:
    """stdout 한 줄 — `레터박스 위아래 60px / 좌우 0px → 그림 1920×960`."""
    return (f"레터박스 위아래 {picture['y']}px / 좌우 {picture['x']}px → "
            f"그림 {picture['w']}×{picture['h']} (컨테이너 {width}×{height}, "
            f"표본 {picture.get('samples', '?')}장)")


def detect_or_full(video: Path, *, width: int, height: int,
                   samples: int = DEFAULT_SAMPLES, log=print) -> dict:
    """probe 단계용 — 실패하면 **사유를 남기고** 전체 영역으로 진행한다.

    반환은 늘 `{x, y, w, h, samples}` 이고, 실패면 `error` 키가 추가된다(run_log ·
    checkpoint 에 그대로 실린다 — 재개는 error 가 있으면 다시 잰다)."""
    try:
        pic = detect_picture_area(Path(video), samples=samples, log=log)
    except Exception as e:  # noqa: BLE001 — 안전장치가 본편을 막지 않는다(기록은 남긴다)
        reason = f"{type(e).__name__}: {e}"
        log(f"  [v3/probe] ⚠ 레터박스 검출 실패 — 전체 영역으로 진행: {reason}")
        return {**full_area(width, height), "samples": 0, "error": reason}
    out = {"x": pic["x"], "y": pic["y"], "w": pic["w"], "h": pic["h"],
           "samples": pic["samples"]}
    if is_full(out, width, height):
        log(f"  [v3/probe] 레터박스 없음 (표본 {out['samples']}장)")
    else:
        log(f"  [v3/probe] {describe(out, width, height)}")
    return out
