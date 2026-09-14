"""0단계 — 소스 프로브 · 스캔 프록시 · 전사용 오디오."""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from app.tikitaka.common import Job, find_bin

SCAN_HEIGHT = 360
SCAN_FPS = 1          # Gemini 스캔 프록시는 파일 fps 1 — 토큰 = 길이×66(+오디오 25)
AUDIO_SR = 16000


def probe(job: Job) -> dict:
    if job.has("probe.json"):
        return job.load("probe.json")
    ffprobe = find_bin("ffprobe")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-show_entries", "stream=codec_type,width,height,r_frame_rate,sample_rate,channels",
         "-of", "json", str(job.source)],
        capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), {})
    num, den = (v.get("r_frame_rate") or "0/1").split("/")
    info = {
        "duration_sec": float(d["format"]["duration"]),
        "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
        "fps": (float(num) / float(den)) if float(den) else 0.0,
        "audio": bool(a), "sample_rate": int(a.get("sample_rate") or 0),
    }
    job.save("probe.json", info)
    job.log(f"[probe] {info['width']}x{info['height']} {info['fps']:.3f}fps {info['duration_sec']:.1f}s audio={info['audio']}")
    return info


def build_audio(job: Job) -> Path:
    """전사용 16k 모노 wav."""
    out = job.path("audio_16k.wav")
    if out.exists():
        return out
    ffmpeg = find_bin("ffmpeg")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(job.source), "-vn",
                    "-ac", "1", "-ar", str(AUDIO_SR), "-c:a", "pcm_s16le", str(out)], check=True)
    job.log(f"[probe] 오디오 추출 → {out.name}")
    return out


def build_scan_proxy(job: Job) -> Path:
    """Gemini 인덱싱용 360p/1fps 프록시(오디오 포함 — 화자 배정에 소리가 필요하다)."""
    out = job.path("scan_360p_1fps.mp4")
    if out.exists():
        return out
    ffmpeg = find_bin("ffmpeg")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(job.source),
                    "-vf", f"scale=-2:{SCAN_HEIGHT},fps={SCAN_FPS}", "-fps_mode", "cfr",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                    "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", str(out)], check=True)
    job.log(f"[probe] 스캔 프록시 → {out.name} ({out.stat().st_size/1e6:.1f}MB)")
    return out


CUT_HEIGHT = 480
CUT_FPS = 10          # 샷 경계 검출용 — 1fps 스캔 프록시로 검출하면 경계가 정수 초가 돼 ±0.1s 마진이 ±1s 가 된다(2026-09-10 실측)


def build_cut_proxy(job: Job) -> Path:
    """샷 경계 검출용 480p/10fps 무음 프록시(0.1s 정밀도). 스캔 프록시(1fps)와 별개."""
    out = job.path(f"cut_{CUT_HEIGHT}p_{CUT_FPS}fps.mp4")
    if out.exists():
        return out
    ffmpeg = find_bin("ffmpeg")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(job.source), "-an",
                    "-vf", f"scale=-2:{CUT_HEIGHT},fps={CUT_FPS}", "-fps_mode", "cfr",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-movflags", "+faststart", str(out)], check=True)
    job.log(f"[probe] 컷 검출 프록시 → {out.name} ({out.stat().st_size/1e6:.1f}MB)")
    return out


def cut_proxy_clip(job: Job, start: float, end: float, name: str, *, height: int = 480, fps: int = 10) -> Path:
    """편집 테이블 단계에서 창 하나를 10fps/480p 로 재단(입력 시크 — 앞 전체 디코드 없음)."""
    out = job.path("windows") / name
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_bin("ffmpeg")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(job.source),
                    "-vf", f"scale=-2:{height},fps={fps}", "-fps_mode", "cfr",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                    "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", str(out)], check=True)
    return out


_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def parse_cropdetect(stderr: str, width: int, height: int) -> dict:
    """cropdetect 로그 → 최빈 활성 영역 {w,h,x,y}. 너무 작거나(절반 미만) 없으면 전체 프레임. 순수 — 테스트 대상."""
    cands = Counter(_CROP_RE.findall(stderr))
    for (w, h, x, y), _n in cands.most_common():
        w, h, x, y = int(w), int(h), int(x), int(y)
        if w >= width * 0.5 and h >= height * 0.5:
            return {"w": w, "h": h, "x": x, "y": y, "letterbox": (h < height or w < width)}
    return {"w": width, "h": height, "x": 0, "y": 0, "letterbox": False}


def detect_active_area(job: Job) -> dict:
    """소스의 실제 화면 영역(레터박스 검은 띠 제외) — 5지점 × 6s 표본. probe.json `active` 에 저장.
    2026-09-11 실측: 「지금 불륜」 소스는 1920×960(위아래 60px 띠) — 그 띠가 제목 아래 검정 여백으로 보였다."""
    info = job.load("probe.json")
    if info.get("active"):
        return info["active"]
    ffmpeg = find_bin("ffmpeg")
    dur = float(info["duration_sec"])
    logs = []
    for frac in (0.1, 0.3, 0.5, 0.7, 0.9):
        proc = subprocess.run([ffmpeg, "-v", "info", "-ss", f"{dur*frac:.1f}", "-t", "6", "-i", str(job.source),
                               "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"], capture_output=True, text=True)
        logs.append(proc.stderr)
    active = parse_cropdetect("\n".join(logs), int(info["width"]), int(info["height"]))
    info["active"] = active
    job.save("probe.json", info)
    job.log(f"[probe] 활성 화면 영역 {active['w']}×{active['h']} @({active['x']},{active['y']})" + (" — 레터박스 제거" if active["letterbox"] else ""))
    return active
