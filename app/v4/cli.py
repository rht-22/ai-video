"""pipeline v4 CLI — 계약 `docs/v4/M2-interfaces.md` §6.

    python -m app.v4 --video <원본.mp4> --work-title <작품명> \\
        [--srt 자막.srt] [--episode N] [--outdir outputs] [--job-id <재개>] \\
        [--from-step <단계>] [--stop-after <단계>] [--max-shorts 1..8] \\
        [--winner-detail] [--skip-research] [--scene-threshold 0.3] \\
        [--edit-overrides <json>]

받는 키 집합을 여기서 못박는다 — **모르는 플래그는 argparse 가 거절**한다(기획서 §6).
오케스트레이터가 v1·v3 시절 플래그를 그대로 넘기면 조용히 무시되는 대신 즉시 죽는다.

⚠ v3 CLI 의 `--skip-stage2/3/4` 다섯 플래그는 **만들지 않는다** — `--stop-after` 하나다.
v3 는 스킵 플래그마다 run_log 기록 방식이 달랐고(조사 gotcha 15) 어디까지 돌았는지를
사람이 다섯 곳을 읽어 짜맞춰야 했다. `--stop-after` 는 단계 표 하나로 판정된다.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.modules.ffmpeg_utils import ensure_ffmpeg_supported
from app.modules.grid.scenecut import SCENE_THRESHOLD
from app.v4.approve import MAX_SHORTS_DEFAULT, MAX_SHORTS_LIMIT, clamp_max_shorts
from app.v4.steps import STEP_ORDER, V4_STEPS, parse_from_step

# 이 단계 이상을 실제로 돌면 GEMINI_API_KEY 가 **반드시** 있어야 한다(계약 §6).
# 6단계부터는 업로드한 영상을 모델이 보는 호출이라 키 없이는 아무것도 못 한다.
# ⚠ 2단계 research 도 LLM 을 쓰지만 여기서 막지 않는다 — `--stop-after probe` 는 키
#   없이 돌아야 한다는 것이 계약이고, research 는 전사·인코딩보다 **앞**이라 키가
#   없으면 파이프라인이 그 자리에서(비싼 일 전에) 크게 실패한다(E11 규율의 자리).
GEMINI_REQUIRED_FROM = "candidates"


def needs_gemini_key(stop_after: str | None) -> bool:
    """이 실행이 6단계 이상을 돌게 되는가 → 키 사전검사 여부. 순수.

    `stop_after` 만 본다: `from_step` 은 '어디부터'이지 '어디까지'가 아니라서, 그것으로
    막으면 `--from-step probe` 실행이 6단계까지 흘러가는데도 키 검사를 건너뛴다.
    """
    if stop_after is None:
        return True
    return STEP_ORDER[stop_after] >= STEP_ORDER[GEMINI_REQUIRED_FROM]


def _step_help() -> str:
    return ("정본 id: " + " ".join(V4_STEPS)
            + " (별칭 6=candidates 6b=boundary 6c=verify 7=funnel 8=flags "
              "9=approve 10=flesh 10a=detail 11=11:resources)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="create_shorts_v4",
        description="pipeline v4 (M5: 1~10단계 — 격자·후보·승인·살붙이기) — 병행 신규")
    p.add_argument("--video", required=True, help="원본 영상 경로")
    p.add_argument("--work-title", required=True, help="작품명(job 디렉토리 접두)")
    p.add_argument("--srt", default=None,
                   help="공식 자막(있으면 격자 srt_cues 레이어로 적재 — 전사를 대체하지 "
                        "않는다: 시각 정본은 whisper 단어다)")
    p.add_argument("--episode", type=int, default=None)
    p.add_argument("--outdir", default="outputs")
    p.add_argument("--job-id", default=None, help="기존 job 재개")
    p.add_argument("--from-step", default=None,
                   help="캐시를 무시하고 이 단계부터 재구성 — " + _step_help())
    p.add_argument("--stop-after", default=None,
                   help="이 단계까지만 돌고 정상 종료 — " + _step_help())
    p.add_argument("--max-shorts", type=int, default=None,
                   help=f"승인 편수 상한(1~{MAX_SHORTS_LIMIT} · 기본 {MAX_SHORTS_DEFAULT}) "
                        "— 범위 밖은 즉시 실패(조용한 클램프 금지)")
    p.add_argument("--skip-research", action="store_true")
    # 10a 정밀 청취. **기본 꺼짐**(기획서 §9 N1: "끄고 시작, A/B 뒤").
    # 🛑 꺼져 있으면 화자를 어디서도 못 얻는다 — 자막이 전 줄 흰색으로 나간다
    #   (M5 계약 §0: whisper 전사에 화자가 없고 v4 는 청크 상세 분석을 없앴다).
    #   그 사실은 파이프라인이 stdout·run_log 양쪽에 남긴다.
    p.add_argument("--winner-detail", action="store_true",
                   help="10a 정밀 청취 — 승인 편 구간만 3fps 로 다시 듣는다"
                        "(화자·깨진 전사 보정). 미지정이면 화자별 자막색이 없다")
    p.add_argument("--scene-threshold", type=float, default=SCENE_THRESHOLD,
                   help=f"ffmpeg scene score 임계(기본 {SCENE_THRESHOLD})")
    p.add_argument("--edit-overrides", default=None,
                   help="편집실 수정 JSON — --job-id 재개와 함께여야 한다")
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
    if args.edit_overrides:
        if not args.job_id:
            raise SystemExit("--edit-overrides 는 --job-id 재개와 함께여야 한다 "
                             "(기존 산출 위에 정착시키는 수정이다)")
        if not Path(args.edit_overrides).is_file():
            raise SystemExit(f"편집실 수정 파일 없음: {args.edit_overrides}")

    # 단계 이름·편수는 **파이프라인을 부르기 전에** 정규화한다. 실패 메시지는
    # steps·approve 가 만든 것(허용 목록 전량)을 그대로 보여 준다 — 무인 노드에서
    # 오타 하나가 편을 죽이는데 로그에 허용값이 없으면 사람이 코드를 열어야 한다.
    try:
        from_step = parse_from_step(args.from_step)
        stop_after = parse_from_step(args.stop_after)
        clamp_max_shorts(args.max_shorts)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    if (stop_after is not None and from_step is not None
            and STEP_ORDER[stop_after] < STEP_ORDER[from_step]):
        raise SystemExit(f"--stop-after {stop_after} 가 --from-step {from_step} 보다 "
                         "앞이다 — 그러면 아무 단계도 돌지 않는다")

    # 비싼 단계 앞 자격 검사(E11 규율) — 6단계 이상을 돌 때만 필수다.
    if needs_gemini_key(stop_after) and not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit(
            "GEMINI_API_KEY 없음 — 6단계(후보 편성) 이상은 LLM 호출이다. "
            "격자까지만 만들려면 --stop-after probe --skip-research.")

    from app.v4.pipeline import run_v4
    out = run_v4(video_path=video, work_title=args.work_title,
                 outdir=Path(args.outdir), srt_path=srt, episode=args.episode,
                 job_id=args.job_id, from_step=from_step, stop_after=stop_after,
                 skip_research=args.skip_research,
                 max_shorts=args.max_shorts,
                 winner_detail=args.winner_detail,
                 scene_threshold=args.scene_threshold,
                 edit_overrides_path=(Path(args.edit_overrides)
                                      if args.edit_overrides else None))
    print(f"[v4] 완료 → {out}")
    return 0
