# 고품질 자동 쇼츠 제작기 (안 B)

## 개요
이 프로젝트는 입력 영상에서 **60초 내외의 세로 쇼츠(9:16)** 를 자동으로 생성하는 파이프라인입니다.
- 톤: `drama_variety` (몰입 + 리액션 포인트)
- 구조: **Hook(0~3초) → Build(3~45초) → Payoff(45~60초)**
- 결과물: `shorts.mp4` + `edit_plan.json` + `run_log.json`

## 설치
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 실행
```bash
python -m app.cli create_shorts \
  --video "/path/input.mp4" \
  --title "작품명" \
  --topic "한 가지 주제" \
  --outdir "/path/outputs"
```

## 파이프라인 요약
1. ffprobe로 메타데이터 수집
2. 300초 단위 분할 + 3초 오버랩
3. 씬 디텍션
4. Gemini 3 분석(JSON 강제)
5. story_role 기반 Hook/Build/Payoff 구성
6. 씬 경계 스냅(±0.8초)
7. 얼굴 기반 리프레임 타임라인 생성
8. ASS 자막 생성
9. 렌더링 + 품질 검증

## FFmpeg 레이아웃
- 캔버스: 1080x1920
- 중앙: 원본 영상 리프레임/크롭
- 상단: 제목 텍스트
- 하단: 작품명 라벨

## 출력 예시
`outputs/sample_job/` 폴더에 샘플 `edit_plan.json` 및 `run_log.json`을 포함합니다.
