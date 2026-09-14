"""2단계 — 장면 전환 컷 경계(ffmpeg scene score). v3 와 같은 방식(의존 추가 0 · 전 환경 동일 산출).

산출 `scenecuts.json`: {threshold, cuts:[초…]} — 편집 테이블의 안전 마진(±0.1s) 기준점.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.tikitaka.common import Job, find_bin

SCENE_THRESHOLD = 0.3
SCENE_FALLBACK_THRESHOLDS = (0.2, 0.15, 0.1)   # 어두운 그레이딩·레터박스 소스는 0.3 에서 컷이 거의 안 잡힌다(2026-09-13 3화 실측: 50개/50분)
MIN_CUTS_PER_MIN = 4.0                         # 이 밑이면 임계를 낮춰 다시 잰다 — 빠진 경계는 '45초짜리 가짜 샷'(드리프트·같은 샷 재사용의 뿌리)
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def pick_threshold(counts: dict[float, int], duration_sec: float, *, requested: float = SCENE_THRESHOLD,
                   fallbacks: tuple[float, ...] = SCENE_FALLBACK_THRESHOLDS, min_per_min: float = MIN_CUTS_PER_MIN) -> float:
    """임계별 컷 수 → 쓸 임계. 요청 임계가 분당 min_per_min 이상이면 그대로, 아니면 폴백 순서대로 처음 기준을 넘는 값, 다 못 넘으면 가장 낮은 것.
    순수 — 테스트 대상."""
    minutes = max(1e-6, float(duration_sec) / 60.0)
    if counts.get(requested, 0) / minutes >= min_per_min:
        return requested
    last = requested
    for th in fallbacks:
        if th not in counts:
            continue
        last = th
        if counts[th] / minutes >= min_per_min:
            return th
    return last


def parse_showinfo_times(stderr: str) -> list[float]:
    seen: list[float] = []
    for m in _PTS_RE.findall(stderr):
        t = round(float(m), 3)
        if not seen or t > seen[-1]:
            seen.append(t)
    return seen


def _run_scene(ffmpeg: str, video_path: Path, threshold: float) -> list[float]:
    proc = subprocess.run([ffmpeg, "-v", "info", "-i", str(video_path),
                           "-vf", f"select='gt(scene,{threshold})',showinfo",
                           "-fps_mode", "passthrough", "-f", "null", "-"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"scene 검출 실패: {proc.stderr[-300:]}")
    return parse_showinfo_times(proc.stderr)


def detect_scene_cuts(job: Job, video_path: Path, *, threshold: float = SCENE_THRESHOLD, duration: float | None = None) -> list[float]:
    """샷 경계. 요청 임계(0.3)에서 분당 컷이 MIN_CUTS_PER_MIN 미만이면 SCENE_FALLBACK_THRESHOLDS 로 낮춰 다시 잰다(적응형).
    캐시 키는 소스 이름 + **요청** 임계(실제 쓴 임계는 `threshold`, 시도 내역은 `trials` 에 남는다)."""
    if job.has("scenecuts.json"):
        cached = job.load("scenecuts.json")
        # 검출 소스가 바뀌면(1fps 스캔 프록시 → 10fps 컷 프록시) 캐시를 버린다 — 정밀도가 다른 경계를 섞으면 안 된다
        if cached.get("source") == video_path.name and cached.get("requested", cached.get("threshold")) == threshold:
            return cached["cuts"]
        job.log(f"[scenecut] 캐시 소스 {cached.get('source')!r} ≠ {video_path.name!r} → 재검출")
    ffmpeg = find_bin("ffmpeg")
    if duration is None:
        duration = float(job.load("probe.json").get("duration_sec") or 0.0) if job.has("probe.json") else 0.0
    counts: dict[float, int] = {}
    cuts_by: dict[float, list[float]] = {}
    for th in (threshold,) + tuple(SCENE_FALLBACK_THRESHOLDS):
        cuts_by[th] = _run_scene(ffmpeg, video_path, th)
        counts[th] = len(cuts_by[th])
        chosen = pick_threshold(counts, duration, requested=threshold)
        if chosen == th and (not duration or counts[th] / max(1e-6, duration / 60.0) >= MIN_CUTS_PER_MIN):
            break
    chosen = pick_threshold(counts, duration, requested=threshold)
    cuts = cuts_by[chosen]
    job.save("scenecuts.json", {"threshold": chosen, "requested": threshold, "source": video_path.name, "cuts": cuts,
                                "trials": {str(k): v for k, v in counts.items()}})
    per_min = (len(cuts) / max(1e-6, duration / 60.0)) if duration else 0.0
    job.log(f"[scenecut] 컷 경계 {len(cuts)}개 (threshold {chosen}" + (f" ← 요청 {threshold} 에서 분당 {counts[threshold] / max(1e-6, duration / 60.0):.1f}개뿐이라 낮춤" if chosen != threshold else "")
            + (f" · 분당 {per_min:.1f}개)" if duration else ")"))
    job.record_step("scenecut", cuts=len(cuts), threshold=chosen, requested=threshold, trials=counts)
    return cuts


# ── 검은 화면 구간 (2026-09-12, 사용자 지적 "v1 35초에 검은 화면이 있어서 tts 와 안 맞아") ──────────────────────────────
# 장면 전환 페이드(검정 1~4초)는 scene score 로는 경계가 안 잡힌다(어두운 밤 장면은 프레임 차이가 작아 2885~2930.8 이 한 샷으로
# 잡혔다). 그 '가짜 긴 샷' 안에서 컷 소스가 검은 꼬리로 밀려 내레이션 1.7s 가 검은 화면 위에 나갔다. 컷 프록시(10fps)에
# blackdetect 를 한 번 돌려 캐시하고, N/A 행 컷 후보에서 그 구간을 깎는다(table.carve_black). S 행(립싱크)은 대상이 아니다.
BLACK_MIN_SEC = 0.3
BLACK_PIX_TH = 0.04   # 0.10 은 어두운 밤 차 안(실루엣·계기판 불빛이 보이는 장면)까지 검정으로 잡았다(2026-09-13 3화 v9 실측: 33:18~33:22).
                      # 0.04 는 진짜 페이드(1화 2927.5~2930.8)·오프닝 암전은 그대로 잡고 어두운 장면은 통과시킨다 — 3화 35구간 89s → 7구간 43s.
BLACK_PIC_TH = 0.98
_BLACK_RE = re.compile(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)")


def parse_blackdetect(stderr: str) -> list[tuple[float, float]]:
    """blackdetect 로그 → [(start, end)] 오름차순. 순수 — 테스트 대상."""
    out = sorted((round(float(a), 3), round(float(b), 3)) for a, b in _BLACK_RE.findall(stderr))
    return [(a, b) for a, b in out if b > a]


def detect_black_spans(job: Job, video_path: Path, *, min_sec: float = BLACK_MIN_SEC, pix_th: float = BLACK_PIX_TH,
                       pic_th: float = BLACK_PIC_TH) -> list[tuple[float, float]]:
    """소스의 검은 화면 구간(페이드·암전) — `blackspans.json` 캐시(검출 소스 이름·파라미터가 다르면 재검출). 실패는 빈 목록 + 기록
    (안전장치가 본편을 막지 않는다 — 대신 벨트가 조용히 사라지지 않게 로그를 남긴다)."""
    params = {"min_sec": min_sec, "pix_th": pix_th, "pic_th": pic_th}
    if job.has("blackspans.json"):
        cached = job.load("blackspans.json")
        if cached.get("source") == video_path.name and cached.get("params") == params:
            return [tuple(x) for x in cached["spans"]]
        job.log(f"[scenecut] 검은 구간 캐시 소스 {cached.get('source')!r}/{cached.get('params')} ≠ 지금 → 재검출")
    ffmpeg = find_bin("ffmpeg")
    proc = subprocess.run([ffmpeg, "-v", "info", "-i", str(video_path),
                           "-vf", f"blackdetect=d={min_sec}:pix_th={pix_th}:pic_th={pic_th}", "-an", "-f", "null", "-"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        job.log(f"[scenecut] ⚠ 검은 구간 검출 실패(컷 후보 필터 없이 진행): {proc.stderr[-200:]}")
        return []
    spans = parse_blackdetect(proc.stderr)
    job.save("blackspans.json", {"source": video_path.name, "params": params, "spans": spans})
    job.log(f"[scenecut] 검은 화면 구간 {len(spans)}개 · 합 {sum(b - a for a, b in spans):.1f}s")
    job.record_step("blackdetect", spans=len(spans), total_sec=round(sum(b - a for a, b in spans), 3))
    return spans


def scene_bounds(cuts: list[float], t: float, duration: float) -> tuple[float, float]:
    """t 를 품는 샷의 [raw_in, raw_out]. 컷 목록은 오름차순."""
    lo, hi = 0.0, duration
    for c in cuts:
        if c <= t:
            lo = c
        else:
            hi = c
            break
    return lo, hi
