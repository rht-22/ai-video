"""출력 클립의 무음 비율(silence_ratio)을 측정한다.

벤치마크 기준 — ai-video 클립 ~49% 무음 vs 시장 승자 ~9%. PR-6 무음 컷 aggressive 프로파일
적용 전/후로 이 값을 측정해 ~10-15% 로 떨어지는지 확인하는 A/B 검증 도구.

무음 정의는 task 스펙과 동일: ffmpeg silencedetect=noise=-30dB:d=0.5
(−30dB 미만이 0.5초 이상 지속되면 무음 구간).

사용법:
    python -m scripts.measure_silence_ratio outputs/<job>/final.mp4
    python -m scripts.measure_silence_ratio outputs_ab/conservative outputs_ab/aggressive
    python -m scripts.measure_silence_ratio --json clip1.mp4 clip2.mp4

A/B 비교(두 디렉토리 평균 대조):
    python -m scripts.measure_silence_ratio --compare outputs_ab/conservative outputs_ab/aggressive
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# app 패키지 헬퍼 재사용 (Windows ffmpeg 경로 탐색 포함)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.modules.ffmpeg_utils import find_ffmpeg_command  # noqa: E402

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
_SILENCE_DUR_RE = re.compile(r"silence_duration:\s*([0-9.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")


def _probe_duration_sec(path: Path) -> float:
    """ffprobe 로 전체 길이(초) 반환. 실패 시 0.0."""
    ffprobe = find_ffmpeg_command("ffprobe")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def measure_silence_ratio(
    path: Path, *, noise_db: float = -30.0, min_silence_sec: float = 0.5,
) -> dict:
    """단일 파일의 무음 비율을 측정한다.

    Returns dict: {file, duration_sec, silence_sec, silence_ratio, silence_segments}
    """
    ffmpeg = find_ffmpeg_command("ffmpeg")
    duration = _probe_duration_sec(path)

    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = proc.stderr or ""

    durations = [float(m) for m in _SILENCE_DUR_RE.findall(log)]
    starts = [float(m) for m in _SILENCE_START_RE.findall(log)]
    silence_sec = sum(durations)
    # 파일 끝까지 이어진 무음은 silence_duration 없이 silence_start 만 찍힐 수 있음 → 보정
    if len(starts) > len(durations) and duration > 0:
        silence_sec += max(0.0, duration - starts[-1])

    ratio = (silence_sec / duration) if duration > 0 else 0.0
    return {
        "file": str(path),
        "duration_sec": round(duration, 2),
        "silence_sec": round(silence_sec, 2),
        "silence_ratio": round(ratio, 4),
        "silence_segments": len(starts),
    }


def _expand_inputs(inputs: list[str]) -> list[Path]:
    """파일/디렉토리 인자를 비디오 파일 목록으로 펼친다."""
    files: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in _VIDEO_EXTS))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[warn] 경로 없음: {p}", file=sys.stderr)
    return files


def _summarize(results: list[dict]) -> dict:
    """파일별 결과 → 가중평균(전체 무음/전체 길이) + 단순평균."""
    if not results:
        return {"count": 0, "avg_silence_ratio": 0.0, "weighted_silence_ratio": 0.0}
    total_dur = sum(r["duration_sec"] for r in results)
    total_sil = sum(r["silence_sec"] for r in results)
    return {
        "count": len(results),
        "avg_silence_ratio": round(sum(r["silence_ratio"] for r in results) / len(results), 4),
        "weighted_silence_ratio": round((total_sil / total_dur) if total_dur else 0.0, 4),
        "avg_duration_sec": round(total_dur / len(results), 2),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="출력 클립 무음 비율 측정 (silencedetect -30dB:0.5s)")
    ap.add_argument("inputs", nargs="+", help="비디오 파일 또는 디렉토리")
    ap.add_argument("--noise-db", type=float, default=-30.0, help="무음 임계 dB (기본 -30)")
    ap.add_argument("--min-silence", type=float, default=0.5, help="최소 무음 길이 초 (기본 0.5)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--compare", action="store_true",
                    help="입력을 2개의 arm(디렉토리)으로 보고 평균 무음 비율 대조")
    args = ap.parse_args(argv)

    if args.compare:
        if len(args.inputs) != 2:
            print("--compare 는 정확히 2개의 디렉토리/파일 집합을 받습니다.", file=sys.stderr)
            return 2
        arms = {}
        for label in args.inputs:
            files = _expand_inputs([label])
            results = [measure_silence_ratio(f, noise_db=args.noise_db, min_silence_sec=args.min_silence)
                       for f in files]
            arms[label] = {"summary": _summarize(results), "files": results}
        if args.json:
            print(json.dumps(arms, ensure_ascii=False, indent=2))
        else:
            for label, data in arms.items():
                s = data["summary"]
                print(f"[{label}] n={s['count']} "
                      f"가중무음={s['weighted_silence_ratio']*100:.1f}% "
                      f"단순평균무음={s['avg_silence_ratio']*100:.1f}% "
                      f"평균길이={s.get('avg_duration_sec', 0):.1f}s")
        return 0

    files = _expand_inputs(args.inputs)
    results = [measure_silence_ratio(f, noise_db=args.noise_db, min_silence_sec=args.min_silence)
               for f in files]
    summary = _summarize(results)

    if args.json:
        print(json.dumps({"summary": summary, "files": results}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"  {r['silence_ratio']*100:5.1f}% 무음  "
                  f"({r['silence_sec']:.1f}/{r['duration_sec']:.1f}s, "
                  f"{r['silence_segments']}구간)  {r['file']}")
        print(f"\n[요약] n={summary['count']} "
              f"가중무음={summary['weighted_silence_ratio']*100:.1f}% "
              f"단순평균무음={summary['avg_silence_ratio']*100:.1f}% "
              f"평균길이={summary.get('avg_duration_sec', 0):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
