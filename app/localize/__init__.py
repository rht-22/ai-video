"""현지화 계층 — ai-video job 디렉토리를 다른 언어판으로 다시 그린다.

발주서: ves-orchestrator `docs/LOCALIZE_UNIFY.md`. 이 패키지는 P1(rerender 계층)에서
video-localization-project `scripts/localize_run.py` 를 **충실히 이식**한 것이다.
회귀 0 이 조건이라 프롬프트·임계값·파일 이름·순서를 바꾸지 않았다.

파이프라인 (기획서 §3-1):

    L0 collect    한국어 원본을 localize_backup_ko/ 에 보존 (멱등 — 재실행해도 이중 번역 없음)
    L2 telop      화면에 박힌 텍스트 추출 (Gemini Pro 영상 패스)
    L2b telop     텔롭 타이밍을 프레임 대조로 재보정 (1장=1콜)
    L1 translate  문맥 통번역 + 용어집 강제 + ASR 교정
    L3 apply      체크포인트 교체 (subtitle_segments·checkpoint_story·edit_plan·resources)
    L3t narration 내레이션 TTS 재합성
    L4 rerender   같은 gen_flags 로 재렌더 + 텔롭 번인
    L5 meta       유튜브 메타 + ko_ja_pairs(검수 대역)

⚠ **모드는 rerender 하나뿐이다.** 완성본 후처리(overlay — 잔망루피 쇼츠)는 P4 다.
"""
from __future__ import annotations

# 성공 마커·산출 규약 — 오케스트레이터 어댑터가 이 이름들을 본다. 바꾸면 컷오버가 깨진다.
BACKUP_DIR_FMT = "localize_backup_{src}"
OUT_DIR_FMT = "localize_{locale}"
META_NAME = "metadata.json"
SOURCE_VIDEO_BACKUP = "shorts_ko.mp4"
RENDER_OUTPUT = "shorts.mp4"

# L0 가 백업하는 파일들 — 번역 입력은 **항상 이 백업본**이다(원본이 아니라).
BACKUP_FILES = [
    "subtitle_segments.json", "edit_plan.json", "checkpoint_story.json",
    "checkpoint_resources.json", "title.txt", "work_title.txt",
]

__all__ = ["BACKUP_DIR_FMT", "OUT_DIR_FMT", "META_NAME", "SOURCE_VIDEO_BACKUP",
           "RENDER_OUTPUT", "BACKUP_FILES"]
