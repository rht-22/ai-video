"""create_shorts_v3 CLI — v3 병행 파이프라인의 신규 엔트리포인트.

기존 `app.cli`(단일 진입점 규약)는 **한 줄도 고치지 않는다** — 발주서가 v3 를
신규 엔트리포인트로 지시했고, M5(채널 전환) 때 app.cli 합류를 별건으로 한다.

  python -m app.v3 --video <원본.mp4> --work-title <작품명> \\
      [--srt 자막.srt] [--episode N] [--outdir outputs] [--job-id <재개>] \\
      [--from-step grid|seq_analyze] [--skip-research] [--skip-seq-analyze] \\
      [--scene-threshold 0.3]
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.modules.ffmpeg_utils import ensure_ffmpeg_supported
from app.v3.scenecut import SCENE_THRESHOLD


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="create_shorts_v3",
        description="pipeline v3 (M1: grid + Stage 1) — 기존 파이프라인과 병행 신규")
    p.add_argument("--video", required=True, help="원본 영상 경로")
    p.add_argument("--work-title", required=True, help="작품명(job 디렉토리 접두)")
    p.add_argument("--srt", default=None, help="공식 자막 파일(있으면 grid 레이어로 적재)")
    p.add_argument("--episode", type=int, default=None)
    p.add_argument("--outdir", default="outputs")
    p.add_argument("--job-id", default=None, help="기존 job 재개")
    p.add_argument("--from-step", default=None,
                   choices=["grid", "seq_analyze", "chunk_split", "chunk_analyze",
                            "story", "resources"],
                   help="캐시를 무시하고 이 단계부터 재구성")
    p.add_argument("--skip-research", action="store_true")
    p.add_argument("--skip-seq-analyze", action="store_true",
                   help="Stage 1(Pro 호출) 없이 grid 까지만")
    p.add_argument("--skip-stage2", action="store_true",
                   help="M2(chunk_split + Stage 2) 없이 Stage 1 까지만")
    p.add_argument("--skip-stage3", action="store_true",
                   help="M3(story + edit_plan·자막·TTS cue) 없이 Stage 2 까지만")
    p.add_argument("--story-target-sec", type=float, default=None,
                   help="편성 목표 길이(채널 노브 — 기본 53s)")
    p.add_argument("--story-max-sec", type=float, default=None,
                   help="편성 상한(기본 60s) — 초과분은 예산 다듬기가 던다")
    p.add_argument("--max-chunks", type=int, default=None,
                   help="계획 앞에서부터 N개 청크만 재단·분석(스모크용 — Pro 요금 상한)")
    p.add_argument("--scene-threshold", type=float, default=SCENE_THRESHOLD,
                   help=f"ffmpeg scene score 임계(기본 {SCENE_THRESHOLD})")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_ffmpeg_supported()

    video = Path(args.video)
    if not video.is_file():
        raise SystemExit(f"영상 파일 없음: {video}")
    srt = Path(args.srt) if args.srt else None
    if srt and not srt.is_file():
        raise SystemExit(f"자막 파일 없음: {srt}")
    # 비싼 단계(전사·프록시) **앞**에서 자격 검사 — E11 규율
    needs_gemini = not (args.skip_seq_analyze and args.skip_research)
    if needs_gemini and not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY 없음 — Stage 1/리서치를 쓰려면 필수. "
                         "grid 만 만들려면 --skip-research --skip-seq-analyze.")

    from app.v3.pipeline import run_v3
    out = run_v3(video_path=video, work_title=args.work_title,
                 outdir=Path(args.outdir), srt_path=srt, episode=args.episode,
                 job_id=args.job_id, from_step=args.from_step,
                 skip_research=args.skip_research,
                 skip_seq_analyze=args.skip_seq_analyze,
                 skip_stage2=args.skip_stage2, skip_stage3=args.skip_stage3,
                 story_target_sec=args.story_target_sec,
                 story_max_sec=args.story_max_sec, max_chunks=args.max_chunks,
                 scene_threshold=args.scene_threshold)
    print(f"[v3] 완료 → {out}")
    return 0
