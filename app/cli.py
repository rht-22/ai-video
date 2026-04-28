from __future__ import annotations

import argparse
from pathlib import Path

from app.config import DesignConfig
from app.pipeline import PipelineInput, run_pipeline


def _resolve_outdir(outdir: Path) -> Path:
    """출력 디렉토리 경로를 해석합니다.
    
    절대 경로면 그대로 사용하고, 상대 경로면 프로젝트 루트 기준으로 변환합니다.
    """
    if outdir.is_absolute():
        return outdir
    # 프로젝트 루트는 app/cli.py의 부모의 부모
    project_root = Path(__file__).resolve().parent.parent
    return project_root / outdir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="High-quality auto shorts generator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create_shorts", help="Create a 60-second vertical short")
    # ── 필수: 작품명. 입력 소스는 로컬 파일(--video + --subtitle) 또는 --youtube-url 중 하나 ──
    create.add_argument("--video", help="입력 영상 파일 경로 (--youtube-url 사용 시 생략)")
    create.add_argument("--subtitle", dest="subtitle_file",
                        help="자막 파일 경로 SRT/ASS/VTT/SMI (--youtube-url 사용 시 생략)")
    create.add_argument("--youtube-url", dest="youtube_url",
                        help="YouTube 영상 URL — 영상+자막 자동 다운로드")
    create.add_argument("--title", required=True, help="작품명 또는 채널명")
    # ── 선택 입력 (기본값 제공) ──
    create.add_argument("--topic", default="",
                        help="(선택) 집중할 주제 한 줄 — 미지정 시 전체 맥락으로 자동 구성")
    create.add_argument("--outdir", default="outputs",
                        help="(선택) 출력 디렉토리 — 기본: outputs/")
    create.add_argument(
        "--from-step",
        choices=[
            "init", "research", "probe", "proxy", "exclusion", "chunk",
            "skeleton", "character_index", "gemini", "graph", "story",
            "tts_plan", "transcribe", "resources", "render", "validate",
        ],
        help="Start from specific step (requires --job-id)",
    )
    create.add_argument("--job-id", help="Job ID to resume from (for --from-step)")
    create.add_argument("--previous-context", help="이전 에피소드 요약 텍스트 (직접 입력)")  # 이전 맥락 부여 테스트
    create.add_argument("--previous-context-file", help="이전 에피소드 요약이 담긴 텍스트 파일 경로")  # 이전 맥락 부여 테스트
    create.add_argument("--work-context", help="작품 설명/시놉시스 텍스트 (직접 입력)")
    create.add_argument("--work-context-file", help="작품 설명/시놉시스가 담긴 텍스트 파일 경로")
    create.add_argument("--srt-file", dest="subtitle_file_legacy", help=argparse.SUPPRESS)  # 하위호환
    create.add_argument("--no-subtitles", action="store_true", help="최종 영상에 자막 표시 안 함")
    create.add_argument("--no-tts-subtitles", action="store_true", help="TTS 내레이션 자막을 영상에 표시 안 함 (TTS 음성은 그대로 재생)")
    create.add_argument("--max-shorts", type=int, default=3, help="생성할 최대 쇼츠 수 (1-3, 기본: 3)")
    create.add_argument("--no-research", action="store_true",
                        help="작품 자동 리서치를 건너뜁니다")
    create.add_argument("--episode", type=int, default=None,
                        help="에피소드 번호 (리서치 시 스포일러 방지)")
    create.add_argument("--skip-intro", type=float, default=0.0,
                        help="오프닝/인트로 건너뛰기 (초, 예: 90)")
    create.add_argument("--skip-credits", type=float, default=0.0,
                        help="엔딩 크레딧 건너뛰기 (초, 예: 120)")
    # ── 디자인 파라미터 (DesignConfig 오버라이드) ──
    design = create.add_argument_group("design", "영상 디자인/레이아웃 설정 (YouTube Shorts safe zone 기준)")
    design.add_argument("--design-title-y", type=int, default=None, help="제목 Y 위치 (기본: 120)")
    design.add_argument("--design-work-title-y", type=int, default=None, help="작품명 Y 위치 (기본: 1560)")
    design.add_argument("--design-aspect-ratio", type=str, default=None, help="비디오 영역 비율 (예: 16:9, 4:3, 1:1)")
    design.add_argument("--design-title-font", type=str, default=None, help="제목 폰트명")
    design.add_argument("--design-title-size", type=int, default=None, help="제목 폰트 크기")
    design.add_argument("--design-title-color", type=str, default=None, help="제목 색상")
    design.add_argument("--design-subtitle-size", type=int, default=None, help="자막 폰트 크기")
    design.add_argument("--design-subtitle-y-margin", type=int, default=None, help="메인 자막 MarginV (기본: 380)")
    design.add_argument("--design-tts-y-margin", type=int, default=None, help="TTS 자막 MarginV (기본: 580)")
    design.add_argument("--design-work-font-size", type=int, default=None, help="작품명 폰트 크기")
    design.add_argument("--design-work-color", type=str, default=None, help="작품명 색상")

    return parser


_CLI_TO_DESIGN_FIELD = {
    "design_title_y": "title_y",
    "design_work_title_y": "work_title_y",
    "design_aspect_ratio": "aspect_ratio",
    "design_title_font": "title_font",
    "design_title_size": "title_size",
    "design_title_color": "title_color",
    "design_subtitle_size": "subtitle_size",
    "design_subtitle_y_margin": "subtitle_y_margin",
    "design_tts_y_margin": "tts_line_y_margin",
    "design_work_font_size": "work_font_size",
    "design_work_color": "work_color",
}


def _build_design_config(args: argparse.Namespace) -> DesignConfig:
    """CLI의 --design-* 인자에서 DesignConfig를 생성합니다."""
    overrides: dict = {}
    for cli_name, field_name in _CLI_TO_DESIGN_FIELD.items():
        value = getattr(args, cli_name, None)
        if value is not None:
            overrides[field_name] = value

    # aspect_ratio 형식 검증
    if "aspect_ratio" in overrides:
        ratio_str = overrides["aspect_ratio"]
        try:
            rw, rh = map(int, ratio_str.split(":"))
            if rw <= 0 or rh <= 0:
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError(
                f"잘못된 비율 형식: '{ratio_str}'. W:H 형식을 사용하세요 (예: 16:9, 4:3, 1:1)"
            )

    return DesignConfig(**overrides)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "create_shorts":
        if args.from_step and not args.job_id:
            parser.error("--from-step requires --job-id")

        # 이전 맥락 부여 테스트: --previous-context 또는 --previous-context-file 처리
        previous_episodes_context: str | None = None
        if getattr(args, "previous_context_file", None):
            previous_episodes_context = Path(args.previous_context_file).read_text(encoding="utf-8").strip()
        elif getattr(args, "previous_context", None):
            previous_episodes_context = args.previous_context

        # 작품 컨텍스트: --work-context 또는 --work-context-file 처리
        work_context: str | None = None
        if getattr(args, "work_context_file", None):
            work_context = Path(args.work_context_file).read_text(encoding="utf-8").strip()
        elif getattr(args, "work_context", None):
            work_context = args.work_context

        # # 명령어 실행 직후(영상을 올리고 나서) 사용자에게 톤(장르)을 인터랙티브하게 물어봅니다.
        # print("\n🎬 어떤 스타일(톤)의 쇼츠를 만드시겠습니까?")
        # print("  1) 드라마 (묵직한 서사, 갈등, 반전, 긴장감 위주)")
        # print("  2) 예능 (티키타카, 빵 터지는 리액션, 유머 위주)")
        # while True:
        #     choice = input("원하는 번호를 선택하세요 (1 또는 2): ").strip()
        #     if choice == "1":
        #         selected_tone = "drama"
        #         print(">> [드라마] 버전으로 분석을 시작합니다!\n")
        #         break
        #     elif choice == "2":
        #         selected_tone = "variety"
        #         print(">> [예능] 버전으로 분석을 시작합니다!\n")
        #         break
        #     else:
        #         print("❌ 잘못된 입력입니다. 1 또는 2를 입력해 주세요.")
        
        # 입력 소스 해소: YouTube URL이면 자동 다운로드, 아니면 로컬 파일 사용
        if getattr(args, "youtube_url", None):
            import re
            from app.modules.youtube_downloader import download_youtube_assets

            def _slugify(s: str) -> str:
                return re.sub(r"[^\w가-힣]+", "_", s).strip("_") or "untitled"

            dl_dir = _resolve_outdir(Path(args.outdir)) / _slugify(args.title) / "_source"
            print(f"[YouTube] 다운로드 중: {args.youtube_url}")
            assets = download_youtube_assets(args.youtube_url, dl_dir, lang="ko")
            print(f"  영상: {assets.video_path}")
            print(f"  자막: {assets.subtitle_path or '(없음 — Gemini 전사로 폴백)'}")
            video_path = assets.video_path
            srt_path = assets.subtitle_path
        else:
            if not args.video:
                parser.error("--video 또는 --youtube-url 중 하나는 필수입니다")
            video_path = Path(args.video)
            # 자막 파일: --subtitle 우선, --srt-file(레거시) 폴백
            sub_file = getattr(args, "subtitle_file", None) or getattr(args, "subtitle_file_legacy", None)
            srt_path = Path(sub_file) if sub_file else None

        max_shorts = min(max(getattr(args, "max_shorts", 3), 1), 3)

        output = run_pipeline(
            PipelineInput(
                video_path=video_path,
                work_title=args.title,
                topic=args.topic,
                outdir=_resolve_outdir(Path(args.outdir)),
                design=_build_design_config(args),
                previous_episodes_context=previous_episodes_context,
                work_context=work_context,
                srt_path=srt_path,
                show_subtitles=not getattr(args, "no_subtitles", False),
                show_tts_subtitles=not getattr(args, "no_tts_subtitles", False),
                max_shorts=max_shorts,
                skip_research=getattr(args, "no_research", False),
                episode=getattr(args, "episode", None),
                skip_intro_sec=getattr(args, "skip_intro", 0.0),
                skip_credits_sec=getattr(args, "skip_credits", 0.0),
            ),
            from_step=args.from_step,
            job_id=args.job_id,
        )
        for idx, vp in enumerate(output.output_videos, 1):
            print(f"shorts_{idx}: {vp}")
        print(f"edit_plan: {output.edit_plan_path}")
        print(f"run_log: {output.run_log_path}")


if __name__ == "__main__":
    main()
