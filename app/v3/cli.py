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
    p.add_argument("--edit-overrides", default=None,
                   help="편집실 수정 JSON(edit_overrides/v1~v3) — --job-id 재개 필수. "
                        "clip 경계는 최근접 grid span 경계로 정착(오차 run_log 기록)")
    p.add_argument("--from-step", default=None,
                   choices=["grid", "seq_analyze", "chunk_split", "chunk_analyze",
                            "story", "resources",
                            "draft_render", "style", "render", "validate"],
                   help="캐시를 무시하고 이 단계부터 재구성")
    p.add_argument("--skip-research", action="store_true")
    p.add_argument("--skip-seq-analyze", action="store_true",
                   help="Stage 1(Pro 호출) 없이 grid 까지만")
    p.add_argument("--skip-stage2", action="store_true",
                   help="M2(chunk_split + Stage 2) 없이 Stage 1 까지만")
    p.add_argument("--skip-stage3", action="store_true",
                   help="M3(story + edit_plan·자막·TTS cue) 없이 Stage 2 까지만")
    p.add_argument("--skip-stage4", action="store_true",
                   help="M4(draft·style·최종 렌더·validate) 없이 M3 까지만")
    p.add_argument("--fix-names", action="store_true",
                   help="자막의 인명 오인식을 리서치 사전으로 자동 교정(기본 경고만)")
    p.add_argument("--hook-variants", type=int, default=None,
                   help="§9-C 훅 변형 N버전(2~3) — 본편 불변·훅 비트/제목/첫 TTS 만 교체")
    p.add_argument("--story-target-sec", type=float, default=None,
                   help="편성 목표 길이(채널 노브 — 기본 53s)")
    p.add_argument("--story-max-sec", type=float, default=None,
                   help="편성 상한(기본 60s) — 초과분은 예산 다듬기가 던다")
    p.add_argument("--max-chunks", type=int, default=None,
                   help="계획 앞에서부터 N개 청크만 재단·분석(스모크용 — Pro 요금 상한)")
    p.add_argument("--scene-threshold", type=float, default=SCENE_THRESHOLD,
                   help=f"ffmpeg scene score 임계(기본 {SCENE_THRESHOLD})")
    # 템플릿 확장(2026-08-31, laeebly 벤치마크) — 미지정 = 종전 그대로(회귀 0)
    p.add_argument("--story-templates", default=None,
                   help="추가로 여는 스토리 템플릿(쉼표 구분 — conflict_payoff,"
                        "chemi_observe). 미지정 = 기본 2종(recap_dialogue·highlight)만")
    p.add_argument("--style-preset", default=None,
                   help="채널 스타일 프리셋(recap|drama_clip). 미지정 = recap")
    # 사람 편집 흐름 체인(2026-09-03) — 미지정(legacy) = 종전 story.run_story(회귀 0)
    p.add_argument("--story-flow", default="legacy", choices=["legacy", "human"],
                   help="Stage 3 편성기: human = 주제→씬→대사→내레이션(즉시 합성)→덮개"
                        "(그 자리 재관찰) 체인. legacy = 종전 단일 프롬프트")
    p.add_argument("--style-tone", default=None,
                   help="채널 톤 프로파일 이름(app/data/style_tones/<이름>.json) — "
                        "Stage 3 스토리 프롬프트에 내레이션 말투·제목 톤·훅 규칙 절을 "
                        "덧붙인다. 미지정 = 프롬프트 종전과 동일")
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
    if args.edit_overrides and not args.job_id:
        raise SystemExit("--edit-overrides 는 --job-id 재개와 함께여야 한다 "
                         "(기존 산출 위에 정착시키는 수정이다 — C4)")
    # 자격 검사 전에 프로젝트 .env 를 로드한다 — pipeline·gemini_client 와 같은
    # 위치. 여기만 안 읽으면 .env 에 키가 있어도 사전검사가 거짓 실패한다
    # (2026-09-02 실사고: shell env 없이 .env 만 있는 실행이 시작도 못 함).
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    except ImportError:
        pass
    # 비싼 단계(전사·프록시) **앞**에서 자격 검사 — E11 규율
    needs_gemini = not (args.skip_seq_analyze and args.skip_research)
    if needs_gemini and not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY 없음 — Stage 1/리서치를 쓰려면 필수. "
                         "grid 만 만들려면 --skip-research --skip-seq-analyze.")

    # 사람 흐름 체인은 인명 교정을 기본으로 켠다(2026-09-03): 받아쓰기가 낸 표기('애지')가
    # 자막에 그대로 나갔다 — 리서치에 정본('예지')이 있는데 경고만 남기고 지나갔다.
    if args.story_flow == "human" and not args.fix_names:
        args.fix_names = True
        print("  [v3] --story-flow human → 인명 교정(--fix-names) 기본 켬")

    from app.v3.pipeline import run_v3
    out = run_v3(video_path=video, work_title=args.work_title,
                 outdir=Path(args.outdir), srt_path=srt, episode=args.episode,
                 job_id=args.job_id, from_step=args.from_step,
                 skip_research=args.skip_research,
                 skip_seq_analyze=args.skip_seq_analyze,
                 skip_stage2=args.skip_stage2, skip_stage3=args.skip_stage3,
                 skip_stage4=args.skip_stage4,
                 edit_overrides_path=(Path(args.edit_overrides)
                                      if args.edit_overrides else None),
                 hook_variants=args.hook_variants,
                 fix_names=args.fix_names,
                 story_target_sec=args.story_target_sec,
                 story_max_sec=args.story_max_sec, max_chunks=args.max_chunks,
                 story_templates=(tuple(
                     s.strip() for s in args.story_templates.split(",") if s.strip())
                     if args.story_templates else None),
                 style_preset=args.style_preset, style_tone=args.style_tone,
                 story_flow=args.story_flow,
                 scene_threshold=args.scene_threshold)
    print(f"[v3] 완료 → {out}")
    return 0
