from __future__ import annotations

import argparse
from pathlib import Path

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
    create.add_argument("--video", required=True, help="Path to input video")
    create.add_argument("--title", required=True, help="Work title")
    create.add_argument("--topic", required=True, help="One topic to focus on")
    create.add_argument("--outdir", required=True, help="Output directory")
    create.add_argument(
        "--from-step",
        choices=[
            "init",
            "probe",
            "full_audio",
            "storyline",
            "chunk",
            "gemini",
            "story",
            "resources",
            "temp_render",
            "extract_audio",
            "regenerate_subtitles",
            "final_render",
            "validate",
        ],
                       help="Start from specific step (requires --job-id)")
    create.add_argument("--job-id", help="Job ID to resume from (for --from-step)")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "create_shorts":
        if args.from_step and not args.job_id:
            parser.error("--from-step requires --job-id")
            
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
        
        output = run_pipeline(
            PipelineInput(
                video_path=Path(args.video),
                work_title=args.title,
                topic=args.topic,
                outdir=_resolve_outdir(Path(args.outdir))
            ),
            from_step=args.from_step,
            job_id=args.job_id,
        )
        print(f"shorts: {output.output_video}")
        print(f"edit_plan: {output.edit_plan_path}")
        print(f"run_log: {output.run_log_path}")


if __name__ == "__main__":
    main()
