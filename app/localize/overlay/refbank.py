"""레퍼런스 음성 은행 — 더빙 음색을 '자동으로 개선'하는 핵심.

문제(2026-07-08 실측, 커몬2): 짧은 대사("루피" 한 마디)는 self-ref 로 쓰면 퇴화하고,
고정 은행 레퍼런스(adobe) 는 그 영상 목소리보다 밝아서 "느낌이 다른" 더빙이 됨.

해법(자동):
  ① 처리하는 영상마다 '깨끗한 긴 대사'를 은행에 축적(harvest) — 쓸수록 은행이 풍부해짐
  ② 더빙 시 그 영상 자체 목소리의 음향 프로필(F0·밝기)에 **가장 가까운** 은행 항목을 선택
     → 고정 폴백 대신 영상별 최적 매칭. 은행이 클수록 매칭이 좋아진다.

각 클립은 ref_bank/<name>.wav + 사이드카 <name>.json(전사·길이·f0·centroid·source).
무거운 의존(demucs/faster-whisper)은 dub 모듈 함수 재사용(lazy). 순수 로직은 분리해 테스트.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
import sys


from typing import Any, Optional  # noqa: E402

from app.localize.overlay.common import ensure_dir, ffmpeg_bin, get_logger, read_json, resolve_path, write_json  # noqa: E402

log = get_logger("refbank")

_HANGUL_RE = re.compile(r"[가-힣]")


def hangul_chars(text: str) -> int:
    return len(_HANGUL_RE.findall(text or ""))


# ── 순수: 음향 프로필 / 거리 / 선택 ──────────────────────────────────────
def spectral_centroid(audio, sr: int) -> float:
    """스펙트럼 무게중심(Hz) — 음색 '밝기'. 높을수록 또랑또랑/날카로움."""
    import numpy as np
    x = np.asarray(audio, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if not sr or len(x) == 0:
        return 0.0
    X = np.abs(np.fft.rfft(x))
    fr = np.fft.rfftfreq(len(x), 1.0 / sr)
    denom = X.sum()
    return float((fr * X).sum() / denom) if denom > 0 else 0.0


def profile_distance(a: dict[str, float], b: dict[str, float],
                     brightness_weight: float = 0.7) -> float:
    """두 음향 프로필 거리(옥타브 단위 합). 피치 우선 + 밝기 가중.

    측정 불가(0) 성분은 inf 취급 — 비교 불가한 후보를 최악으로 밀어낸다."""
    import math
    for k in ("f0", "centroid"):
        if a.get(k, 0) <= 0 or b.get(k, 0) <= 0:
            return float("inf")
    pitch = abs(math.log2(a["f0"] / b["f0"]))
    bright = abs(math.log2(a["centroid"] / b["centroid"]))
    return pitch + brightness_weight * bright


def choose_best(target: dict[str, float], entries: list[dict[str, Any]],
                brightness_weight: float = 0.7,
                exclude_source: Optional[str] = None) -> Optional[dict[str, Any]]:
    """target 프로필에 가장 가까운 은행 항목. exclude_source 는 후보에서 제외
    (자기 영상에서 harvest 된 짧은 조각으로 자기를 더빙하는 순환 방지)."""
    best, best_d = None, float("inf")
    for e in entries:
        if exclude_source and e.get("source") == exclude_source:
            continue
        d = profile_distance(target, e, brightness_weight)
        if d < best_d:
            best, best_d = e, d
    if best is not None:
        best = {**best, "_distance": round(best_d, 4)}
    return best


# ── 음향 측정 (lazy) ─────────────────────────────────────────────────────
def wav_profile(wav: str) -> dict[str, float]:
    """wav 파일 → {f0, centroid, dur}."""
    import soundfile as sf
    from app.localize.overlay.dub import f0_median
    x, sr = sf.read(wav)
    return {"f0": round(f0_median(x, sr), 1),
            "centroid": round(spectral_centroid(x, sr), 1),
            "dur": round(len(x) / sr, 2) if sr else 0.0}


# ── 정제 컷 ──────────────────────────────────────────────────────────────
def _clean_cut(src_wav: str, start: float, end: float, out_wav: pathlib.Path,
               pad: float = 0.05) -> None:
    """대사 구간 컷 + 저역제거·노이즈감쇠·레벨정규화, mono 32k (build_self_ref 와 동일 체인)."""
    st = max(0.0, start - pad)
    dur = (end + pad) - st
    subprocess.run(
        [ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{st:.3f}", "-t", f"{dur:.3f}",
         "-i", src_wav, "-af", "highpass=f=60,afftdn=nf=-25,dynaudnorm=p=0.7:m=10",
         "-ac", "1", "-ar", "32000", str(out_wav)], check=True)


# ── harvest: 영상/오디오 → 은행 클립 ─────────────────────────────────────
def harvest(media: str, config: dict[str, Any], source_id: str,
            segs: Optional[list[dict[str, Any]]] = None,
            is_vocals: bool = False) -> int:
    """media 에서 '레퍼런스급 깨끗한 대사'를 은행에 축적. 반환: 추가된 클립 수.

    조건: 길이 ≥ min_ref_dur(자연 문장), 한글 ≥ min_hangul, ASR 신뢰(transcribe 필터).
    is_vocals=True 면 이미 보컬 분리된 트랙(demucs 생략)."""
    rb = config.get("dub", {}).get("refbank", {})
    min_dur = float(rb.get("min_ref_dur", 3.0))
    max_dur = float(rb.get("max_ref_dur", 10.0))
    min_hangul = int(rb.get("min_hangul", 6))
    per_source = int(rb.get("max_per_source", 4))
    bank_dir = ensure_dir(resolve_path(rb.get("dir", "outputs/ref_bank")))

    if segs is None:
        from app.localize.overlay.dub import transcribe
        segs = transcribe(media, config, language="ko")

    if is_vocals:
        vocals = media
    else:
        from app.localize.overlay.dub import separate_vocals
        nov = separate_vocals(media, resolve_path(f"outputs/{source_id}/stems"), config)
        vocals = str(pathlib.Path(nov).parent / "vocals.wav")

    added = 0
    for s in segs:
        if added >= per_source:
            break
        dur = float(s.get("end", 0)) - float(s.get("start", 0))
        text = s.get("text", "")
        if not (min_dur <= dur <= max_dur) or hangul_chars(text) < min_hangul:
            continue
        key = hashlib.md5(f"{source_id}:{s['start']:.2f}:{text}".encode()).hexdigest()[:8]
        name = f"{source_id}_{key}"
        wav = bank_dir / f"{name}.wav"
        side = bank_dir / f"{name}.json"
        if side.exists():                              # 이미 축적됨(멱등)
            continue
        try:
            _clean_cut(vocals, float(s["start"]), float(s["end"]), wav)
            prof = wav_profile(str(wav))
            if prof["f0"] <= 0:                        # 유성 성분 없음 → 폐기
                wav.unlink(missing_ok=True)
                continue
            write_json({"wav": str(wav), "transcript": text.strip(), "source": source_id,
                        **prof}, side)
            added += 1
            log.info("은행 축적: %s (%.1fs, f0=%.0f centroid=%.0f) %r",
                     name, prof["dur"], prof["f0"], prof["centroid"], text[:30])
        except Exception as e:                         # 한 클립 실패가 전체를 막지 않게
            log.warning("harvest 클립 실패(%s): %s", name, e)
            wav.unlink(missing_ok=True)
    return added


def load_bank(config: dict[str, Any]) -> list[dict[str, Any]]:
    """ref_bank/*.json 사이드카 → 항목 목록(존재하는 wav 만)."""
    rb = config.get("dub", {}).get("refbank", {})
    bank_dir = resolve_path(rb.get("dir", "outputs/ref_bank"))
    if not bank_dir.exists():
        return []
    out = []
    for j in sorted(bank_dir.glob("*.json")):
        try:
            e = read_json(j)
        except Exception:
            continue
        if e.get("wav") and pathlib.Path(e["wav"]).exists() and "transcript" in e:
            out.append(e)
    return out


def best_ref(target: dict[str, float], config: dict[str, Any],
             exclude_source: Optional[str] = None) -> Optional[dict[str, Any]]:
    """target 음향 프로필에 가장 가까운 은행 레퍼런스 → {ref_wav, prompt_text, aux_refs}.

    은행 비어있으면 None(호출자가 config 고정 ref 로 폴백)."""
    rb = config.get("dub", {}).get("refbank", {})
    entries = load_bank(config)
    if not entries:
        return None
    bw = float(rb.get("brightness_weight", 0.7))
    pick = choose_best(target, entries, bw, exclude_source)
    if pick is None:
        return None
    n_aux = int(rb.get("aux_count", 1))
    same = [e for e in entries if e.get("source") == pick.get("source")
            and e["wav"] != pick["wav"]]
    aux = [e["wav"] for e in same[:n_aux]]
    log.info("은행 최적 매칭: %s (거리 %.3f, f0=%.0f centroid=%.0f) — target f0=%.0f centroid=%.0f",
             pathlib.Path(pick["wav"]).stem, pick["_distance"], pick["f0"], pick["centroid"],
             target.get("f0", 0), target.get("centroid", 0))
    return {"ref_wav": pick["wav"], "prompt_text": pick["transcript"], "aux_refs": aux}


def bank_status(config: dict[str, Any]) -> dict[str, Any]:
    entries = load_bank(config)
    sources = sorted({e.get("source", "?") for e in entries})
    return {"clips": len(entries), "sources": sources}
