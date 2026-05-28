# AI Video Shorts Creator

입력 영상에서 **60초 내외의 세로 쇼츠(9:16)** 를 자동 생성하는 14단계 파이프라인 +
이를 감싸는 FastAPI 서비스입니다.

- 구조: **Hook → Build×N → Payoff** (storytelling) 또는 단일 클립(highlight)
- 결과물: `shorts.mp4` + `edit_plan.json` + `run_log.json` + 단계별 체크포인트
- 상세 설계: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 설치

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

별도 의존: `ffmpeg` / `ffprobe` (PATH), Gemini API 키(환경변수). YouTube 입력 시 `yt-dlp`,
얼굴 인식에 `deepface`, 전사 폴백에 `faster-whisper` 사용.

## 실행 (CLI)

```bash
# 로컬 영상 + 자막
python -m app.cli create_shorts \
  --video "/path/input.mp4" \
  --subtitle "/path/input.srt" \
  --title "작품명" \
  --topic "한 가지 주제" \
  --outdir "outputs"

# YouTube URL (영상+자막 자동 다운로드)
python -m app.cli create_shorts \
  --youtube-url "https://www.youtube.com/watch?v=XXXX" \
  --title "작품명" --max-shorts 1

# 부분 재실행 (캐시 활용)
python -m app.cli create_shorts \
  --video ... --title "..." --from-step story --job-id 작품명_<2hex>
```

주요 옵션: `--max-shorts`(1~3), `--no-research`, `--episode`,
`--skip-intro/--skip-credits`(초), `--no-subtitles`/`--no-tts-subtitles`,
`--design-*`(레이아웃/폰트/색/자막 스타일). 전체 목록은 [app/cli.py](app/cli.py).

## 실행 (웹 API)

```bash
uvicorn app.main:app --reload
```

REST 엔드포인트: `/auth/verify`, `/channel`, `/upload`, `/video/generate/shorts`
(인증: `Authorization: Bearer <api_key>`). 상세는 [ARCHITECTURE.md §10](docs/ARCHITECTURE.md#10-웹-서비스-계층-fastapi).

## 파이프라인 요약 (14단계)

| 단계 | 내용 |
|------|------|
| research | Gemini Pro + TMDb로 작품/인물 리서치 |
| probe / proxy | ffprobe 메타데이터 → 480p·fps=4 프록시 인코딩 |
| chunk | 600초 단위 분할 + 180초 오버랩 |
| character_index | ArcFace 인물 등장 인덱스 |
| chunk_transcribe | 청크별 전사 (SRT 우선, 없으면 Whisper) |
| gemini | Gemini Pro 멀티모달 분석 → 후보 장면 추출 |
| graph | Gemini Pro 장면 관계 추출 + sequence 묶기 |
| story | Gemini Flash로 storyline(+TTS cue) 구성 |
| silence_cut | 스토리-aware 무음 컷 |
| resources | 얼굴 크롭 타임라인 + cue별 TTS mp3 |
| render | ffmpeg filter_complex 합성 (9:16) |
| validate | duration / audio peak / black frame 검증 |

## FFmpeg 레이아웃

- 캔버스: 1080x1920
- 중앙: 원본 영상 리프레임/크롭(얼굴 추적) + blur 배경
- 상단: 제목 텍스트 (2줄: 맥락 / 후킹)
- 하단: 작품명 라벨(텍스트 또는 로고 이미지)
- 자막: 메인 ASS + TTS 자막 ASS (각각 토글 가능)

## 모델 규칙

Gemini는 `gemini-3.1-pro-preview`(Pro) / `gemini-3-flash-preview`(Flash) 두 모델만 사용합니다.
자세한 분담은 [CLAUDE.md](CLAUDE.md) 및 [ARCHITECTURE.md §8](docs/ARCHITECTURE.md#8-ai-모델-분담-실제-호출-기준).
