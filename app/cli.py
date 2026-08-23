from __future__ import annotations

import argparse
from pathlib import Path

from app.config import DesignConfig, get_logo_path, to_ass_color
from app.modules.editorial import merge_editorial, parse_editorial
from app.modules.ffmpeg_utils import ensure_ffmpeg_supported
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


def _parse_loudness(value: str | None) -> float | None:
    """--loudness-lufs 파싱. 'off'/'none'/빈값 → None(정규화 끔, A/B 대조군),
    숫자 → float, 잘못된 값 → 기본 -14.0(렌더 안 깨지게)."""
    s = (value or "").strip().lower()
    if s in ("off", "none", "null", ""):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return -14.0


def _apply_ab_env(args) -> dict:
    """A/B CLI 플래그 → 환경변수 변환. (인터페이스 통일: 모든 A/B 노브를 CLI 로 노출.
    silence/length 내부 플러밍은 config 의 env default_factory 이므로 run_pipeline 전에 env 를 set.)
    설정한 키 dict 반환(테스트용). standard/conservative 는 강제 안 함(config 기본값 사용)."""
    import os
    applied: dict = {}
    sp = getattr(args, "silence_profile", None)
    if sp:
        os.environ["SILENCE_CUT_PROFILE"] = sp
        applied["SILENCE_CUT_PROFILE"] = sp
    if getattr(args, "length_profile", None) == "tight":
        # 시장 승자(~46s) 쪽으로: 목표 45s, 상한 50s, 톨러런스 1.5→1.1(≈55s 상한).
        for k, v in (("TARGET_DURATION_SEC", "45"), ("MAX_DURATION_SEC", "50"),
                     ("MAX_DURATION_TOLERANCE", "1.1")):
            os.environ[k] = v
            applied[k] = v
    return applied


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
    create.add_argument("--editorial-json", dest="editorial_json", default=None,
                        help="(선택) 작품별 편집 지침 JSON — "
                             '{"avoid":[…],"prefer":[…],"tone":"…"}. '
                             "avoid=금지(태깅→하드 필터·문구 금지), prefer=방향(랭킹 편향), "
                             "tone=title·TTS 문체. app/modules/editorial.py 가 계약 정본")
    create.add_argument("--editorial-run-json", dest="editorial_run_json", default=None,
                        help="(선택) 이번 실행에만 얹는 편집 지시 JSON(같은 스키마) — "
                             "prefer·tone 은 추가·교체되지만 avoid 는 완화 불가(합집합)")
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
    create.add_argument(
        "--reject-note", dest="reject_note", default=None,
        help="(선택) 직전 결과가 사람에게 반려된 사유. 영상 분석·스토리 구성 프롬프트에 "
             "'이번엔 이것을 고쳐라'로 주입된다 — VES 검수함의 반려 사유가 여기로 들어온다.",
    )
    create.add_argument(
        "--edit-overrides", dest="edit_overrides", default=None,
        help="(선택) 관제 편집실이 고친 제목·구간 JSON(edit_overrides/v1~v3). 체크포인트보다 "
             "사람 입력이 이긴다 — 구간을 지정하면 자동 보정(대사 경계 스냅·서사 확장·"
             "갭 메우기)을 건너뛰고 그 구간 그대로 렌더한다. --from-step render 와 함께 쓴다.",
    )
    create.add_argument("--previous-context", help="이전 에피소드 요약 텍스트 (직접 입력)")  # 이전 맥락 부여 테스트
    create.add_argument("--previous-context-file", help="이전 에피소드 요약이 담긴 텍스트 파일 경로")  # 이전 맥락 부여 테스트
    create.add_argument("--work-context", help="작품 설명/시놉시스 텍스트 (직접 입력)")
    create.add_argument("--work-context-file", help="작품 설명/시놉시스가 담긴 텍스트 파일 경로")
    create.add_argument("--srt-file", dest="subtitle_file_legacy", help=argparse.SUPPRESS)  # 하위호환
    create.add_argument("--no-subtitles", action="store_true", help="최종 영상에 자막 표시 안 함")
    create.add_argument("--no-tts-subtitles", action="store_true", help="TTS 내레이션 자막을 영상에 표시 안 함 (TTS 음성은 그대로 재생)")
    create.add_argument("--no-title-overlay", action="store_true",
                        help="상단 제목·하단 작품명 오버레이를 그리지 않음 (JP 파이프라인: 텍스트는 현지화 단계가 일본어로 렌더)")
    create.add_argument("--no-tts-audio", action="store_true",
                        help="TTS 내레이션 오디오를 믹스하지 않음 — 원본 오디오만 (JP: 일본어 TTS 는 현지화 단계가 합성). tts_subtitles.ass 는 그대로 생성되어 현지화의 타이밍 원료가 된다")
    create.add_argument("--max-shorts", type=int, default=3, help="생성할 최대 쇼츠 수 (1-3, 기본: 3)")
    create.add_argument("--loudness-lufs", default="-14",
                        help="출력 라우드니스 목표 LUFS (기본 -14, 쇼츠 표준). 'off' 면 정규화 끔(A/B 대조군). "
                             "동일 edit_plan 으로 --from-step render 재렌더 시 ON/OFF 비교 = 깨끗한 A/B.")
    # A/B 노브 통일(모두 CLI). silence/length 는 env(config)로 전달됨.
    create.add_argument("--silence-profile", choices=["conservative", "aggressive"], default=None,
                        help="무음 컷 프로파일 (A/B). aggressive=gap-단위·무음 적극 제거(벤치마크 가설). 미지정=config 기본(conservative).")
    create.add_argument("--length-profile", choices=["standard", "tight"], default=None,
                        help="길이 프로파일 (A/B). tight=목표 45s·상한 톨러런스 1.1(시장 ~46s). 미지정=config 기본(standard).")
    # E11(2026-08-22) 자막 전사 백엔드 선택. 오케스트레이터 어댑터가 채널 design 키
    # transcribe_backend 를 그대로 이 플래그로 넘긴다(ops_config.channel_transcribe 게이트 뒤).
    # 미지정 = 종전 그대로(회귀 0). 허용값 밖은 choices 가 즉시 실패시킨다 — 조용히
    # 기본값으로 떨어지면 사람은 일레븐랩스로 바꿨다고 믿은 채 종전 전사로 발행된다.
    create.add_argument("--transcribe-backend", dest="transcribe_backend",
                        choices=["default", "elevenlabs"], default=None,
                        help="(선택) 대사 자막의 받아쓰기 백엔드. default=내장 전사(미지정과 동일), "
                             "elevenlabs=ElevenLabs Scribe STT(ELEVENLABS_API_KEY 필요). "
                             "자막 파일(--subtitle)이 있으면 전사 자체를 안 하므로 무관하다.")
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
    design.add_argument("--design-title-y", type=int, default=None,
                        help="제목 Y 위치 (기본: 120). 평소엔 동적 배치(영상 위 20px)가 이기고 "
                             "여백 부족 시 폴백으로만 쓰인다 — 항상 이 값을 쓰려면 --design-title-y-fixed")
    design.add_argument("--design-title-y-fixed", action="store_true",
                        help="제목 Y 를 동적 배치 대신 --design-title-y 값에 고정 (F-409). "
                             "편집실 제목 드래그용 — 기본은 종전 동적 배치(기존 채널 무영향)")
    design.add_argument("--design-video-y", type=int, default=None,
                        help="영상영역 상단 Y (기본: 세로 중앙). 위로 올리면 아래 밴드가 넓어져 "
                             "TTS 자막·로고를 영상 아래에 쌓을 수 있다. 제목은 영상 위에 동적 배치라 따라온다")
    design.add_argument("--design-work-title-y", type=int, default=None, help="작품명 Y 위치 (기본: 1560)")
    design.add_argument("--design-aspect-ratio", type=str, default=None, help="비디오 영역 비율 (예: 16:9, 4:3, 1:1)")
    design.add_argument("--design-video-width", type=int, default=None,
                        help="영상 밴드 가로 크기 (캔버스 px, 320~1080, 기본 1080=꽉 찬 폭). "
                             "화면비는 밴드의 모양을, 이 값은 크기를 정한다(높이 = 폭 × 비율). "
                             "가로는 항상 중앙 배치")
    design.add_argument("--design-title-font", type=str, default=None, help="제목 폰트명")
    design.add_argument("--design-subtitle-font", type=str, default=None,
                        help="자막/TTS 자막 폰트명 (기본: 장르 프리셋 폰트). 일본어 등 프리셋 폰트에 없는 글리프가 필요할 때 지정")
    design.add_argument("--design-title-size", type=int, default=None,
                        help="제목 폰트 크기 (1줄 기준). 기본 [70,90]의 줄 간 비율을 유지한 채 "
                             "전체를 스케일한다 — 90/70 배율로 2줄 크기가 함께 커진다")
    design.add_argument("--design-title-color", type=str, default=None, help="제목 1번째 줄 색상 (기본: white)")
    design.add_argument("--design-title-color2", type=str, default=None, help="제목 2번째 줄 색상 (기본: #FFFF00)")
    # 제목 줄별 배경 박스·굵게(2026-08-21) — 값은 렌더러의 title_boxes/title_box_colors/title_bolds
    # 리스트로 조립된다(title_color(2) 와 같은 패턴). 여백·라운드는 글자 크기 비례 고정값.
    design.add_argument("--design-title-box", type=str, default=None, choices=["none", "round", "rect"],
                        help="제목 1번째 줄 배경: none(없음, 기본)·round(둥근네모)·rect(각진네모)")
    design.add_argument("--design-title-box2", type=str, default=None, choices=["none", "round", "rect"],
                        help="제목 2번째 줄 배경: none·round·rect")
    design.add_argument("--design-title-box-color", type=str, default=None,
                        help="제목 1번째 줄 배경 박스 색 (기본: #000000, 예: #FF3E9D · black@0.6)")
    design.add_argument("--design-title-box-color2", type=str, default=None,
                        help="제목 2번째 줄 배경 박스 색 (기본: #000000)")
    design.add_argument("--design-title-bold", action="store_true",
                        help="제목 1번째 줄 굵게(같은 색 외곽선으로 획 두껍게)")
    design.add_argument("--design-title-bold2", action="store_true",
                        help="제목 2번째 줄 굵게")
    design.add_argument("--design-subtitle-size", type=int, default=None, help="자막 폰트 크기")
    design.add_argument("--design-subtitle-color", type=str, default=None,
                        help="메인 자막 색상 (#RRGGBB 또는 &H00BBGGRR). 미지정 시 장르 프리셋 색상 사용.")
    design.add_argument("--design-subtitle-y-margin", type=int, default=None, help="메인 자막 MarginV (기본: 380)")
    design.add_argument("--design-tts-color", type=str, default=None,
                        help="TTS 자막 색상 (#RRGGBB 또는 &H00BBGGRR, 기본: 하늘색 #87CEEB)")
    design.add_argument("--design-tts-size", type=int, default=None, help="TTS 자막 폰트 크기 (기본: 70)")
    design.add_argument("--design-tts-y-margin", type=int, default=None, help="TTS 자막 MarginV (기본: 580)")
    design.add_argument("--design-title-rotate", type=float, default=None,
                        help="제목 블록 회전 (도, -180~180, 시계방향 양수, 기본 0). "
                             "줄 묶음 전체를 블록 중심 기준으로 회전한다")
    design.add_argument("--design-tts-rotate", type=float, default=None,
                        help="TTS 자막 블록 회전 (도, -180~180, 시계방향 양수, 기본 0)")
    design.add_argument("--design-video-speed", type=float, default=None,
                        help="영상 배속 (0.8~2.0, 기본 1). 원본 영상·현장음에만 적용 — "
                             "TTS 내레이션 오디오는 배속하지 않는다")
    design.add_argument("--design-work-font-size", type=int, default=None, help="작품명 폰트 크기")
    design.add_argument("--design-work-color", type=str, default=None, help="작품명 색상")
    design.add_argument("--design-work-image", type=str, default=None,
                        help="작품명 텍스트 대신 이미지(로고) 사용. 이미지 파일 경로.")
    design.add_argument("--design-work-image-width", type=int, default=None,
                        help="로고 이미지 가로 상한(px). 기본 350")
    design.add_argument("--design-work-image-height", type=int, default=None,
                        help="로고 이미지 세로 상한(px). 지정 시 (가로x세로) 박스에 비율 유지로 맞춘다")
    design.add_argument("--design-work-align", type=str, default=None, choices=["top", "center"],
                        help="로고 세로 정렬. top=영상 하단에 붙임(기본) · center=하단 밴드 중앙")
    # 플랫폼 표기 — 권리사 '영상 내 플랫폼 노출' 요구용. 영상영역 왼쪽 상단 기준.
    design.add_argument("--design-platform-image", type=str, default=None,
                        help="영상영역 왼쪽 상단에 얹을 플랫폼 로고 이미지. 이름만 주면 assets/logos 에서 찾는다(작품 로고와 같은 규약)")
    design.add_argument("--design-platform-text", type=str, default=None,
                        help="이미지 대신 텍스트로 표기(예: 티빙). --design-platform-image 와 함께 주면 이미지가 우선")
    design.add_argument("--design-platform-x", type=int, default=None,
                        help="영상영역 왼쪽 상단 기준 가로 오프셋 px (기본 24)")
    design.add_argument("--design-platform-y", type=int, default=None,
                        help="영상영역 왼쪽 상단 기준 세로 오프셋 px (기본 24)")
    design.add_argument("--design-platform-image-width", type=int, default=None,
                        help="플랫폼 로고 가로 상한 px (기본 150, 비율 유지 contain)")
    design.add_argument("--design-platform-image-height", type=int, default=None,
                        help="플랫폼 로고 세로 상한 px (기본 80)")
    design.add_argument("--design-platform-font-size", type=int, default=None,
                        help="플랫폼 텍스트 크기 (기본 40)")
    design.add_argument("--design-platform-color", type=str, default=None,
                        help="플랫폼 텍스트 색 (기본 white)")
    design.add_argument("--design-platform-align", type=str, default=None,
                        choices=["left", "right"],
                        help="플랫폼 표기 가로 앵커 (기본 left). right 면 영상 오른쪽 상단 — "
                             "platform-x 는 오른쪽 가장자리에서 안쪽으로의 오프셋이 된다")
    design.add_argument("--no-reframe", action="store_true",
                        help="얼굴 추종 크롭(리프레이밍) 끄기 — 원본을 가운데 정렬로 넣는다. "
                             "인물이 고정된 인터뷰 소재에 적합하고(확대하면 원본 자막이 잘린다) "
                             "크롭 타임라인 생성을 건너뛰어 생성도 빨라진다")
    # 라운드 10: 자막 스타일 프리셋 (auto = 장르 기반 자동 선택)
    design.add_argument(
        "--design-subtitle-style",
        type=str, default=None,
        choices=["auto", "hormozi", "kvar_yellow", "drama_cine", "thriller_mono", "y2k_pink", "kakao_bubble"],
        help="자막 스타일 프리셋. auto(기본)=장르 기반 자동 선택. 그 외=강제 적용.",
    )

    # ── localize (L 계열) — 완성된 job 을 다른 언어판으로 다시 그린다 ──────────
    # 발주서: ves-orchestrator docs/LOCALIZE_UNIFY.md. 생성 경로(create_shorts)와
    # **완전히 분리된 서브커맨드**라 이 플래그를 안 쓰는 실행은 종전과 한 바이트도 같다.
    loc = subparsers.add_parser(
        "localize", help="완성된 job 디렉토리를 현지화한다 (rerender 모드)")
    loc.add_argument("--job-dir", required=True, help="ai-video job 디렉토리")
    loc.add_argument("--locale", default="ja",
                     help="대상 로케일 (app/localize/data/locales.json 의 키)")
    loc.add_argument("--overrides", default=None,
                     help="검수 반려 수정 JSON(카드 ko_ja_pairs idx 좌표) — L1 번역에 병합해 "
                          "L3+ 를 고친 텍스트로 재실행")
    loc.add_argument("--skip-render", action="store_true",
                     help="L4 재렌더 생략(번역까지만 — 프롬프트·용어집 점검용)")

    return parser


_CLI_TO_DESIGN_FIELD = {
    "design_title_y": "title_y",
    "design_video_y": "video_y",
    "design_video_width": "video_width",
    "design_work_title_y": "work_title_y",
    "design_aspect_ratio": "aspect_ratio",
    "design_title_font": "title_font",
    "design_subtitle_font": "subtitle_font",
    "design_title_size": "title_size",
    "design_subtitle_size": "subtitle_size",
    "design_subtitle_color": "subtitle_color",
    "design_subtitle_y_margin": "subtitle_y_margin",
    "design_tts_color": "tts_line_color",
    "design_tts_size": "tts_line_font_size",
    "design_tts_y_margin": "tts_line_y_margin",
    "design_title_rotate": "title_rotate",
    "design_tts_rotate": "tts_rotate",
    "design_video_speed": "video_speed",
    "design_work_font_size": "work_font_size",
    "design_work_color": "work_color",
    "design_work_image_width": "work_image_width",
    "design_work_image_height": "work_image_height",
    "design_work_align": "work_image_align",
    "design_subtitle_style": "subtitle_style_preset",
    "design_platform_text": "platform_text",
    "design_platform_x": "platform_x",
    "design_platform_y": "platform_y",
    "design_platform_image_width": "platform_image_width",
    "design_platform_image_height": "platform_image_height",
    "design_platform_font_size": "platform_font_size",
    "design_platform_color": "platform_color",
    "design_platform_align": "platform_align",
}


def _build_design_config(args: argparse.Namespace) -> DesignConfig:
    """CLI의 --design-* 인자에서 DesignConfig를 생성합니다."""
    overrides: dict = {}
    for cli_name, field_name in _CLI_TO_DESIGN_FIELD.items():
        value = getattr(args, cli_name, None)
        if value is not None:
            overrides[field_name] = value

    # E7: 회전·배속 범위 검증 — 밖이면 즉시 실패(조용한 무시 금지, v3 검증과 동일 원칙).
    # 비숫자는 argparse type=float 가 이미 즉시 실패시킨다.
    _E7_RANGES = {
        "title_rotate": (-180.0, 180.0, "--design-title-rotate"),
        "tts_rotate": (-180.0, 180.0, "--design-tts-rotate"),
        "video_speed": (0.8, 2.0, "--design-video-speed"),
    }
    for _field, (_lo, _hi, _flag) in _E7_RANGES.items():
        if _field in overrides:
            _v = float(overrides[_field])
            if not (_lo <= _v <= _hi):
                raise ValueError(
                    f"{_flag} 값 {overrides[_field]} 이 범위 밖입니다 ({_lo:g}~{_hi:g})")
            overrides[_field] = _v

    # E10: 영상 밴드 가로 크기 — 같은 원칙(범위 밖 즉시 실패). 비숫자는 argparse type=int
    # 가 이미 즉시 실패시킨다. 홀수는 렌더러가 짝수 보정한다(scaled_w -= scaled_w % 2).
    if "video_width" in overrides:
        _vw = int(overrides["video_width"])
        if not (320 <= _vw <= 1080):
            raise ValueError(
                f"--design-video-width 값 {_vw} 이 범위 밖입니다 (320~1080)")

    # 리프레이밍 스위치 — store_true 라 None 이 아니어서 위 루프에 못 태운다(끄는 쪽만 의미 있음).
    if getattr(args, "no_reframe", False):
        overrides["enable_reframe"] = False

    # 제목 Y 고정(F-409) — 같은 이유로 store_true 는 켜는 쪽만 의미 있다.
    if getattr(args, "design_title_y_fixed", False):
        overrides["title_y_fixed"] = True

    # 제목 크기(F-409): 렌더러가 실제 그리는 건 title_sizes 리스트다 — title_size 단일
    # 필드는 줄바꿈 계산에만 쓰여, 여기서 조립하지 않으면 --design-title-size 가 화면에
    # 반영되지 않는다(2026-08-19 오케스트레이터 실측). title_color→title_colors 와 같은
    # 패턴: 기본 [70,90]에서 시작하되, 지정 값을 1줄 기준으로 삼아 **줄 간 비율을 유지한
    # 채** 전체를 스케일한다(90/70 배율). 편집실 제목 드래그 = 블록 전체 확대/축소라
    # 두 줄의 위계(후킹 줄이 더 크다)가 유지돼야 한다 — 계약: docs/edit_overrides_v3.md.
    title_size = getattr(args, "design_title_size", None)
    if title_size:
        base_sizes = list(DesignConfig().title_sizes)
        overrides["title_sizes"] = [
            max(1, round(sz * title_size / base_sizes[0])) for sz in base_sizes]

    # 자막/TTS 색상은 ASS 형식(&HAABBGGRR) — 사용자가 #RRGGBB로 줘도 받도록 정규화.
    # (제목·작품명 색은 ffmpeg drawtext라 hex를 그대로 쓰므로 변환하지 않는다)
    for field_name in ("subtitle_color", "tts_line_color"):
        if field_name in overrides:
            overrides[field_name] = to_ass_color(overrides[field_name])

    # 제목 색: 렌더러가 읽는 건 title_colors 리스트뿐이므로 여기서 조립한다.
    # 기본값에서 시작해 지정된 줄만 치환 → 한 줄만 바꿔도 나머지 줄은 기본색 유지.
    title_color1 = getattr(args, "design_title_color", None)
    title_color2 = getattr(args, "design_title_color2", None)
    if title_color1 or title_color2:
        title_colors = list(DesignConfig().title_colors)
        if title_color1:
            title_colors[0] = title_color1
            overrides["title_color"] = title_color1  # 단일 필드 하위호환
        if title_color2:
            title_colors[1] = title_color2
        overrides["title_colors"] = title_colors

    # 제목 배경 박스·굵게: 렌더러는 리스트 필드만 읽는다 — title_colors 와 같은 조립.
    # 기본값에서 시작해 지정한 줄만 치환(한 줄만 바꿔도 다른 줄은 기본 유지). 스위치(bold)는
    # store_true 라 켜는 쪽만 의미 있다(title_y_fixed 와 같은 이유).
    box1 = getattr(args, "design_title_box", None)
    box2 = getattr(args, "design_title_box2", None)
    if box1 or box2:
        boxes = list(DesignConfig().title_boxes)
        if box1: boxes[0] = box1
        if box2: boxes[1] = box2
        overrides["title_boxes"] = boxes
    bc1 = getattr(args, "design_title_box_color", None)
    bc2 = getattr(args, "design_title_box_color2", None)
    if bc1 or bc2:
        bcs = list(DesignConfig().title_box_colors)
        if bc1: bcs[0] = bc1
        if bc2: bcs[1] = bc2
        overrides["title_box_colors"] = bcs
    bold1 = bool(getattr(args, "design_title_bold", False))
    bold2 = bool(getattr(args, "design_title_bold2", False))
    if bold1 or bold2:
        overrides["title_bolds"] = [bold1, bold2]

    # --design-work-image 지정 시 work_type="image" + work_value=경로 자동 설정.
    # 이름만 준 경우(예: RZsv4.png) assets/logos 에서 찾는다 — 루프·작품 카드가 레포 경로를
    # 몰라도 되게 하려는 것으로, 폰트(get_font_path)와 같은 규약이다.
    work_image = getattr(args, "design_work_image", None)
    if work_image:
        overrides["work_type"] = "image"
        overrides["work_value"] = get_logo_path(
            work_image, Path(__file__).resolve().parent)

    # 플랫폼 로고도 같은 규약 — 이름만 주면 assets/logos 에서 찾아 절대경로로.
    platform_image = getattr(args, "design_platform_image", None)
    if platform_image:
        overrides["platform_image"] = get_logo_path(
            platform_image, Path(__file__).resolve().parent)

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

    if args.command == "localize":
        from app.localize.runner import run_localize
        # 렌더가 자막·텔롭을 번인하므로 ffmpeg 를 먼저 검증한다(create_shorts 와 같은 이유).
        if not args.skip_render:
            ensure_ffmpeg_supported()
        marker = run_localize(args.job_dir, args.locale,
                              overrides_path=args.overrides,
                              skip_render=args.skip_render)
        print(f"[localize] 완료 마커: {marker}")
        return

    if args.command == "create_shorts":
        if args.from_step and not args.job_id:
            parser.error("--from-step requires --job-id")

        # 긴 생성(~68분)을 시작하기 전에 렌더 단계가 쓸 ffmpeg 를 먼저 검증한다.
        ensure_ffmpeg_supported()

        # E11: 전사 백엔드가 elevenlabs 인데 키가 없으면 68분을 태우기 전에 즉시 실패.
        # (파이프라인 안에도 같은 검사가 있지만, 사람이 제일 빨리 알아야 하는 자리는 여기다.)
        if getattr(args, "transcribe_backend", None) == "elevenlabs":
            from app.modules.stt_elevenlabs import ElevenLabsSTTError, ensure_api_key
            try:
                ensure_api_key()
            except ElevenLabsSTTError as e:
                parser.error(str(e))

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
            from app.modules.youtube_downloader import download_youtube_assets, video_id_of

            def _slugify(s: str) -> str:
                return re.sub(r"[^\w가-힣]+", "_", s).strip("_") or "untitled"

            # 영상 ID 로 경로를 가른다 — 제목만으로 가르면 같은 작품의 다른 영상이
            # 기존 source.mp4 를 재사용한다(yt-dlp 는 파일이 있으면 건너뛴다)
            dl_dir = (_resolve_outdir(Path(args.outdir)) / _slugify(args.title)
                      / "_source" / video_id_of(args.youtube_url))
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

        # A/B 노브(silence/length) CLI 플래그 → env (config 가 env 로 읽음). run_pipeline 전에 적용.
        _apply_ab_env(args)

        # 작품별 편집 지침 — 잘못된 JSON·모르는 키는 여기서 즉시 실패(밤중 생성에서
        # 조용히 무시되면 "지침이 적용되고 있다"는 착각이 제일 위험하다).
        try:
            editorial_run = parse_editorial(getattr(args, "editorial_run_json", None))
            editorial = merge_editorial(
                parse_editorial(getattr(args, "editorial_json", None)), editorial_run)
        except ValueError as e:
            parser.error(str(e))

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
                show_title_overlay=not getattr(args, "no_title_overlay", False),
                include_tts_audio=not getattr(args, "no_tts_audio", False),
                max_shorts=max_shorts,
                skip_research=getattr(args, "no_research", False),
                episode=getattr(args, "episode", None),
                skip_intro_sec=getattr(args, "skip_intro", 0.0),
                skip_credits_sec=getattr(args, "skip_credits", 0.0),
                loudness_target_lufs=_parse_loudness(getattr(args, "loudness_lufs", "-14")),
                reject_note=getattr(args, "reject_note", None),
                editorial=editorial,
                editorial_run=editorial_run,
                edit_overrides_path=(Path(args.edit_overrides)
                                     if getattr(args, "edit_overrides", None) else None),
                transcribe_backend=getattr(args, "transcribe_backend", None),
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
