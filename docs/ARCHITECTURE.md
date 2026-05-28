# AI Video Shorts Creator — 아키텍처 문서

> 이 문서는 실제 코드(`app/`)와 동기화되어 있습니다. 단계 순서·상수·모델 분담은
> [app/pipeline.py](../app/pipeline.py), [app/config.py](../app/config.py),
> [app/modules/gemini_client.py](../app/modules/gemini_client.py) 의 현재 구현 기준입니다.

## 1. 프로젝트 개요

**한 문장**: 한국어 드라마/예능 영상에서 60초 세로 쇼츠를 자동 제작하는 14단계 파이프라인 + 이를 감싸는 멀티테넌트 FastAPI 서비스.

이 저장소는 **두 개의 서브시스템**으로 구성된다.

| 서브시스템 | 진입점 | 역할 |
|------------|--------|------|
| **쇼츠 파이프라인** (코어 엔진) | [app/cli.py](../app/cli.py) | 로컬 CLI 실행 — 영상 → 쇼츠 |
| **웹 서비스 계층** | [app/main.py](../app/main.py) | FastAPI REST API — 채널/인증/업로드/렌더 요청 |

두 진입점 모두 동일한 [app/pipeline.py](../app/pipeline.py) `run_pipeline()` 를 호출한다.

**입력**
- 원본 영상 (mp4) + 자막 (SRT/ASS/VTT/SMI) — 또는 YouTube URL
- 작품명, 주제(선택), 작품 설명(선택), 이전 화 요약(선택)

**출력**
- 9:16 세로 쇼츠 mp4 (1~3개)
- 편집 계획 JSON(`edit_plan.json`), 실행 로그(`run_log.json`), 단계별 체크포인트

**차별점**
- Gemini 멀티모달(영상+자막) 직접 분석 + Flash 스토리 구성 분리
- face_id 사전 인식 인덱스로 인물·크롭·자막 매핑 일관성 확보
- TTS cue를 storyline에 내장해 voice/speed/위치 가변 적용
- 모든 단계가 체크포인트 → `--from-step X --job-id Y` 로 부분 재실행해 비용·시간 최소화

---

## 2. 핵심 가치

| 항목 | 설명 |
|------|------|
| **Hands-off 60초 쇼츠** | 영상 한 편 던지면 후킹·서사·자막·TTS·렌더까지 자동 |
| **재실행 친화** | 단계별 체크포인트로 어느 지점부터든 `--from-step`으로 부분 재실행 |
| **비용 최적화** | Pro는 청크 멀티모달 분석/리서치/관계추출에만, Flash는 스토리 구성·텍스트 단축에 사용 |
| **품질 보강** | face_id 사전 패스로 인물 식별 일관성, 스토리-aware 무음 컷으로 군더더기 제거 |

---

## 3. 기술 스택

| 카테고리 | 기술 |
|----------|------|
| 언어 | Python 3.x |
| LLM | Google Gemini API — `gemini-3.1-pro-preview` (Pro), `gemini-3-flash-preview` (Flash) |
| 웹 프레임워크 | FastAPI + Uvicorn |
| DB / ORM | MySQL (PyMySQL) + SQLAlchemy 2.0 + Alembic 마이그레이션 |
| 설정 | pydantic-settings (`.env`) |
| 영상 처리 | ffmpeg / ffprobe |
| 얼굴 인식 | deepface (ArcFace) + opencv |
| TTS | edge-tts (Edge Neural Voices) |
| 전사 | faster-whisper (SRT 없을 때 폴백) |
| 다운로드 | yt-dlp (YouTube URL 입력 시) |
| 메타데이터 | TMDb (배우 프로필 이미지) |

> **모델 사용 규칙**: [CLAUDE.md](../CLAUDE.md) 에 따라 Pro/Flash 두 모델만 사용하며,
> 환경변수 `GEMINI_MODEL_NAME` / `GEMINI_FLASH_MODEL_NAME` 로 오버라이드 가능.
> 기본값은 코드에 고정되어 있다([gemini_client.py:2027](../app/modules/gemini_client.py)).

---

## 4. 시스템 아키텍처

```
┌──────────────┐        ┌──────────────┐
│  CLI 진입점  │        │ FastAPI 진입점│
│  app/cli.py  │        │  app/main.py │
└──────┬───────┘        └──────┬───────┘
       │                       │ (videoApi → BackgroundTask)
       └───────────┬───────────┘
                   ▼
        ┌─────────────────────┐
        │   app/pipeline.py    │   run_pipeline()
        │  14단계 오케스트레이션 │
        └─────────────────────┘
                   │
   ┌───────────────┼────────────────┐
   ▼               ▼                ▼
입력처리        AI 분석          합성 / 렌더
(1~5)          (6~10)           (11~14)
init           character_index   silence_cut
research       chunk_transcribe  resources
probe          gemini (Pro)      render
proxy          graph (Pro)       validate
chunk          story (Flash)
        │
        └── 단계별 체크포인트(.json) — 어느 지점부터든 재시작 가능 ──┘
```

---

## 5. 디렉토리 / 모듈 구조

```
app/
├─ cli.py                       CLI 진입점, argparse 분기
├─ main.py                      FastAPI 앱 + 라우터 4개 등록
├─ pipeline.py                  14단계 오케스트레이션 (+ 길이/dedup/cue 보정 헬퍼)
├─ config.py                    AppConfig / DesignConfig / 폰트 로더
│
├─ modules/                     ── 쇼츠 파이프라인 코어 ──
│  ├─ gemini_client.py          Pro/Flash 호출 (analyze_chunk / extract_relationships /
│  │                            compose_story_with_context / 텍스트 단축)
│  ├─ work_researcher.py        작품 리서치 (Pro) + TMDb 배우 이미지
│  ├─ tmdb_client.py            TMDb 프로필 이미지 다운로드
│  ├─ media_probe.py            ffprobe 래퍼 (MediaInfo)
│  ├─ chunker.py                proxy 청크 분할 (build_chunks / split_video_chunk)
│  ├─ face_id.py                FaceIdentifier + 사전 인덱스 lookup (ArcFace)
│  ├─ scene_detect.py           씬 경계 감지
│  ├─ speech.py                 Whisper 전사 (SpeechSegment / extract_transcript)
│  ├─ moment_ranker.py          Union-Find sequence_id 부여 (assign_sequence_ids)
│  ├─ story_builder.py          StoryClip / TTSCue dataclass + storyline 선택
│  ├─ silence_cutter.py         스토리-aware 무음 구간 컷
│  ├─ subtitle.py               SRT 파싱, ASS 빌더, 편집 타임라인 리맵
│  ├─ subtitle_styles.py        자막 스타일 프리셋 (hormozi / drama_cine 등)
│  ├─ tts.py                    Edge TTS — voice 8종 / speed 5단계 프리셋
│  ├─ reframe.py                얼굴 추적 크롭 타임라인 (build_crop_timeline)
│  ├─ renderer.py               ffmpeg 합성 (filter_complex, GPU 폴백)
│  ├─ validator.py              산출물 검증 (duration / peak / black frame)
│  ├─ youtube_downloader.py     yt-dlp 영상+자막 다운로드
│  └─ intro_credits_detector.py (⚠️ 미사용 — PR-2의 chunk_intro_credits_ranges로 대체)
│
├─ api/                         ── FastAPI REST 라우터 ──
│  ├─ authApi.py                /auth/verify — 인증/어드민 키 발급
│  ├─ channelApi.py             /channel    — 채널 CRUD + API키 발급/갱신 (어드민)
│  ├─ uploadApi.py              /upload     — 영상 등록 + ffprobe 검증
│  ├─ videoApi.py               /video      — 파이프라인을 BackgroundTask로 실행
│  └─ authApi(token).py         (⚠️ 레거시 JWT 버전 — main.py 미등록)
│
├─ domain/                      SQLAlchemy 모델 (user / channels / authKey / video)
├─ schemas/                     Pydantic DTO (auth / apikeyDto / channelDto)
├─ core/
│  ├─ config.py                 Settings (DATABASE_URL, JWT_* — pydantic-settings)
│  ├─ database.py               엔진 / SessionLocal / Base / get_db
│  ├─ security.py               API Key(SHA256) 인증 + USER/ADMIN 역할 가드
│  └─ exceptions.py             (빈 파일)
└─ assets/fonts/                번들 한글 폰트 (Jalnan / Griun / mulmaru 등)

migrations/                     Alembic 마이그레이션 (versions/)
scripts/                        정적 검증 스크립트 (verify_*.py)
tests/                          PR/라운드별 단위 테스트
docs/                           본 문서 + PPTX/덱
```

---

## 6. 14단계 파이프라인

`run_pipeline()` 의 `step_order` 정의([pipeline.py:1575](../app/pipeline.py)). 사용자에게는 `init` 제외 `[N/13]` 으로 표시된다.

| # | 단계 | 모델/도구 | 출력 / 체크포인트 |
|---|------|-----------|-------------------|
| 0 | **init** | — | output_dir, run_log.json |
| 1 | **research** | Gemini Pro + TMDb | work_context, 인물, 회차 → checkpoint_research.json |
| 2 | **probe** | ffprobe | MediaInfo (duration, w×h, fps) → checkpoint_probe.json |
| 3 | **proxy** | ffmpeg | 480p / fps=4 mp4 (`<title>_480.mp4`) |
| 4 | **chunk** | ffmpeg | 600초 chunk × N (180초 overlap), 각 chunk mp4 split |
| 5 | **character_index** | ArcFace | 인물 등장 인덱스 → checkpoint_character_index.json |
| 6 | **chunk_transcribe** | SRT / Whisper | 청크별 전사 segments → checkpoint_chunk_transcripts.json |
| 7 | **gemini** | Gemini Pro (멀티모달) | candidate_moments + chunk 메타 → checkpoint_gemini.json |
| 8 | **graph** | Gemini Pro (텍스트) | 장면 관계 엣지 → checkpoint_graph.json |
| 9 | **story** | Gemini Flash | storyline 3종 + 내장 tts_cues → checkpoint_story.json |
| 10 | **silence_cut** | ffmpeg/분석 | 스토리-aware 무음 컷 + cue 시프트 → checkpoint_silence_cut.json |
| 11 | **resources** | ArcFace + Edge TTS | 크롭 타임라인(crop_*.json) + cue별 mp3 → checkpoint_resources.json |
| 12 | **render** | ffmpeg filter_complex | 최종 9:16 mp4 |
| 13 | **validate** | ffprobe | duration_ok / audio_peak_ok / black_frames_ok |

> **제거된 단계(레거시 from_step alias로 redirect)**: `exclusion`→`chunk`, `transcribe`/`tts_plan`→`silence_cut`.
> `skeleton` 단계는 라운드 6b에서 완전 제거(함수는 [gemini_client.py:1479](../app/modules/gemini_client.py)에 남아 있으나 파이프라인 미호출).
> CLI의 `--from-step` 선택지 목록([cli.py:40](../app/cli.py))은 옛 단계명을 포함하지만, 위 alias로 자동 redirect된다.

---

## 7. 단계별 상세

### 1. research (Pro)
- Gemini Pro로 작품 시놉시스/인물/회차 줄거리 검색([work_researcher.py](../app/modules/work_researcher.py))
- TMDb로 배우 프로필 이미지 다운로드
- `--no-research` 로 스킵, `--episode` 로 스포일러 방지

### 2. probe
- ffprobe로 duration / 해상도 / fps / 오디오 유무 추출

### 3. proxy
- 원본을 `scale=-2:480, fps=4` 로 인코딩(libx264 ultrafast). 모든 후속 분석에서 재사용

### 4. chunk
- `chunk_seconds=600`, `chunk_overlap=180` ([config.py](../app/config.py)) — 청크 1+에는 앞 청크 3분을 prepend해 경계 사건 손실 방지(라운드 19B)
- 사용자가 `--skip-intro` / `--skip-credits` 로 명시한 구간만 잘라냄(자동 인트로/크레딧 감지는 gemini 단계가 직접 처리)
- 각 chunk를 별도 mp4로 split (Gemini File API 업로드용, PTS 0 정규화)

### 5. character_index ⭐
- 프록시를 샘플링 → ArcFace로 인물별 등장 구간 + 정규화 좌표 인덱스 생성
- gemini 단계에 chunk 범위로 필터링되어 첨부, render 크롭 타임라인 생성 시 lookup으로 ArcFace 호출 절감

### 6. chunk_transcribe (PR-3)
- 청크 분할 직후 청크별 transcript를 **원본 절대시간 기준**으로 일괄 생성·캐시
- SRT 있으면 [parse_subtitle](../app/modules/subtitle.py) 후 청크별 슬라이스, 없으면 Whisper(`faster-whisper`)로 청크 split을 전사 후 시프트
- gemini 단계가 SRT/Whisper 구분 없이 이 결과를 사용

### 7. gemini (Pro 멀티모달) ⭐
- 각 chunk 영상 + 자막 + character_appearances + 이전 chunk 요약을 받아
- `segments[]`(전체 타임라인 묘사) + `candidate_moments[]`(쇼츠 후보) 출력
- 점수 없이 description / highlight_eligible / continues_from 등 의미적 평가
- `chunk_intro_credits_ranges`(영상 직접 분석)로 인트로/크레딧 후보를 후처리 필터링([pipeline.py](../app/pipeline.py) `_filter_candidates_by_chunk_intro_credits`)
- 청크 오버랩(180s) 중복은 IoU dedup + 경계 candidate alias 병합으로 정리

### 8. graph (Pro, 텍스트 전용)
- candidate들 사이의 setup_payoff / continuous / consequence / character_arc 등 관계 추출(영상 업로드 없음)
- `assign_sequence_ids` 로 cross-chunk 연속 장면을 같은 sequence로 묶음([moment_ranker.py](../app/modules/moment_ranker.py))

### 9. story (Flash)
- candidates + edges + 이전 화 컨텍스트로 storyline 결정([gemini_client.py](../app/modules/gemini_client.py) `compose_story_with_context`)
- 두 가지 모드:
  - **storytelling**: hook + build×N + payoff (40~60초)
  - **highlight**: 단일 클립 완결형
- storyline에 **tts_cues가 내장**(PR-4)되어 별도 tts_plan 단계 불필요. 비어 있으면 `plan_tts_cues` 폴백
- `select_diverse_storylines` 로 다양성 정렬 후 `--max-shorts` 안에서 채택
- 시간 일관성 검증(`_validate_storyline_timeline`: hook↔payoff IoU·시간 역행)으로 환각 storyline 폐기
- 길이 보정(`_fit_storyline_to_duration`)으로 40~60초 fitting

### 10. silence_cut (PR-5)
- `cut_silence_with_story_filter` 로 각 variant clip의 무음 구간 제거([silence_cutter.py](../app/modules/silence_cutter.py))
- 컷으로 줄어든 누적량만큼 tts cue 시간을 시프트(`_shift_cues_by_silence_cut`)
- 컷 후 길이가 40초 미만이면 컷 전으로 롤백 + 길이 재보정

### 11. resources
- 클립별 얼굴 크롭 타임라인(`build_crop_timeline` + character_index lookup)
- TTS cue별 mp3 합성(voice/speed 적용, `synthesize_tts`). 자막이 cue에 비해 길면 Flash로 텍스트 단축

### 12. render
- ffmpeg `filter_complex` 로:
  - 클립 concat → blur 배경 + 9:16 크롭/리프레임
  - 제목(2줄: line1 맥락 / line2 후킹)·작품명 텍스트(또는 로고 이미지) 오버레이
  - 메인 자막 ASS (TTS 구간엔 숨김) + TTS 자막 ASS (`--no-tts-subtitles` 토글)
  - 원본 오디오 덕킹 + cue별 adelay 절대시간 + amix
- GPU 폴백: d3d11va → cuda → libx264 ([renderer.py](../app/modules/renderer.py))

### 13. validate
- duration / audio peak / black frame 검증([validator.py](../app/modules/validator.py)). 실패 시 run_log에 경고 기록

---

## 8. AI 모델 분담 (실제 호출 기준)

| 모델 / 도구 | 단계 | 역할 |
|-------------|------|------|
| `gemini-3.1-pro-preview` (Pro) | 1 research, 7 gemini, 8 graph | 리서치 / 청크 멀티모달 분석 / 관계 추출 |
| `gemini-3-flash-preview` (Flash) | 9 story, (11 텍스트 단축) | storyline 구성 / TTS 자막 길이 단축 |
| Edge TTS | 11 resources | cue별 mp3 합성 |
| ArcFace (deepface) | 5 character_index, 11 resources | 인물 등장 인덱스 + 크롭 타겟 lookup |
| Whisper (faster-whisper) | 6 chunk_transcribe | SRT 없을 때 전사 폴백 |
| TMDb | 1 research | 배우 프로필 이미지 |

---

## 9. 핵심 데이터 모델

### StoryClip (frozen dataclass — [story_builder.py](../app/modules/story_builder.py))
```python
StoryClip(
    role, start_sec, end_sec, subtitle, use_original_audio,
    pacing_note="", chunk_index=-1, candidate_index=-1,
    character_focus=(), visual_essential=False, tts_draft="",
)
```

### TTSCue (frozen dataclass)
```python
TTSCue(start_sec, end_sec, text, voice="ko_female", speed="normal")
```

### TTS 프리셋 ([tts.py](../app/modules/tts.py))
- **voice 8종**: `ko_female` / `ko_female_high` / `ko_male` / `ko_male_low` (한국어 우선)
  + `chat_emma` / `chat_brian` / `chat_seraphina` / `chat_florian` (multilingual, 트렌드 톤)
- **speed 5단계**: `very_slow(-25%)` / `slow(-10%)` / `normal(+0%)` / `fast(+10%)` / `very_fast(+25%)`

### candidate_moment (Gemini 출력)
```jsonc
{
  "chunk_index": 0, "candidate_index": 3,
  "start_sec": 12.4, "end_sec": 25.8,
  "characters_in_scene": ["..."], "character_focus": ["..."],
  "description": "객관적 묘사",
  "transcript": "주요 발화",
  "scene_location": "...", "timeline_position": "현재|과거|불명",
  "continues_from": null, "requires_context": false,
  "highlight_eligible": false, "highlight_reason": null
}
```

### DB 모델 ([app/domain](../app/domain))
```
User(id, role)                                         role: USER | ADMIN
Channel(channel_id, user_id→user.id, channel_name,     status: ACTIVE|INACTIVE|DELETED
        created_at, updated_at, status)
AuthApiKey(id, user_id→user.id, channel_id→channels,   role: USER|ADMIN
           api_key_hash(SHA256, indexed), role,         status: ACTIVE|INACTIVE|DELETED
           status, created_at, last_used_at)
Video(id, video_id, channel_id→channels.channel_id,    status: READY|PROCESSING|COMPLETED|FAILED
      status, source_type, title, thumbnail_url, topic, source_type: FILE|YOUTUBE
      format, duration, size, resolution, storage_path, expired_at: +30일
      log, created_at, expired_at)
```

---

## 10. 웹 서비스 계층 (FastAPI)

[app/main.py](../app/main.py) 가 4개 라우터를 등록한다. 인증은 `Authorization: Bearer <plain_api_key>` 헤더 →
서버가 SHA256 해시 후 `auth_api_keys` 조회([security.py](../app/core/security.py)).

| Prefix | 메서드 / 경로 | 권한 | 동작 |
|--------|---------------|------|------|
| `/auth/verify` | GET `/user-check` | API Key | 인증 확인 |
| | GET `/admin-check` | ADMIN | 어드민 인증 확인 |
| | POST `/admin/initial-key?user_id=` | (User.role=admin 검증) | 최초 어드민 키 발급 |
| `/channel` | POST `/create` | ADMIN | 채널 + 유저 API키 생성 |
| | GET `/list` | ADMIN | 채널 목록(상태/이름 필터) |
| | GET `/{channel_id}` | ADMIN | 채널 단건 조회 |
| | PATCH `/{channel_id}/update` | ADMIN | 채널명 변경 / API키 갱신 |
| | DELETE `/{channel_id}` | ADMIN | 채널 정지/삭제 + 키 연동 처리 |
| `/upload` | POST `/upload/file` | API Key | 영상 등록 + ffprobe 검증 (전체 경로 `/upload/upload/file`) |
| `/video` | POST `/generate/shorts` | (예시 코드) | DesignConfig 구성 → `run_pipeline` BackgroundTask |

핵심 연결점: [videoApi.py](../app/api/videoApi.py) 가 HTTP 요청의 디자인/자막 설정을 `DesignConfig` 로 변환,
`PipelineInput` 을 구성해 `run_pipeline` 을 백그라운드로 실행한다.

---

## 11. CLI 사용법

```bash
# 기본 (최대 3 쇼츠)
python -m app.cli create_shorts \
  --video /path/to/video.mp4 \
  --subtitle /path/to/subtitle.srt \
  --title "작품명"

# YouTube URL (영상+자막 자동 다운로드)
python -m app.cli create_shorts \
  --youtube-url https://www.youtube.com/watch?v=XXXX \
  --title "작품명" --max-shorts 1

# 부분 재실행 (캐시 활용)
python -m app.cli create_shorts \
  --video ... --title "..." \
  --from-step story --job-id 작품명_<2hex>

# TTS 자막만 끄기 (음성은 그대로) / 모든 자막 끄기
python -m app.cli create_shorts ... --no-tts-subtitles
python -m app.cli create_shorts ... --no-subtitles --no-tts-subtitles

# 인트로/엔딩 강제 스킵
python -m app.cli create_shorts ... --skip-intro 90 --skip-credits 120
```

**유효 `--from-step` (현재 단계)**: init / research / probe / proxy / chunk / character_index /
chunk_transcribe / gemini / graph / story / silence_cut / resources / render / validate
(레거시 exclusion / transcribe / tts_plan / skeleton 입력은 가까운 단계로 자동 redirect)

디자인 오버라이드(`--design-*`): title/work 위치·폰트·색, aspect_ratio, 자막 크기/마진,
작품명 이미지(로고), 자막 스타일 프리셋(auto/hormozi/drama_cine 등) — [cli.py](../app/cli.py) 참고.

---

## 12. 출력 디렉토리 구조

```
outputs/<work_title_slug>_<2hex>/
├─ run_log.json                       실행 로그 (전체 단계 결과)
├─ run_log_gemini.json                gemini 원본 응답 로그 (재실행 보존)
├─ edit_plan.json                     최종 편집 계획
├─ <title>_480.mp4                    분석용 프록시
├─ shorts.mp4 / shorts_2.mp4 / shorts_3.mp4   결과 쇼츠 1~3개
├─ subtitles*.ass / tts_subtitles*.ass         자막 ASS
├─ tts_cue_*.mp3                      cue별 TTS 음성
├─ crop_*.json                       얼굴 크롭 타임라인
├─ checkpoint_research.json
├─ checkpoint_probe.json
├─ checkpoint_character_index.json
├─ checkpoint_chunk_transcripts.json
├─ checkpoint_gemini.json
├─ checkpoint_graph.json
├─ checkpoint_story.json
├─ checkpoint_silence_cut.json
├─ checkpoint_resources.json
├─ subtitle_segments.json
└─ _research/                        TMDb 배우 프로필 이미지
```

> 제거된 단계의 옛 체크포인트(`checkpoint_exclusion.json`, `checkpoint_tts_plan.json`,
> `full_audio.json`)는 재실행 시 자동 삭제된다([pipeline.py:1463](../app/pipeline.py)).

---

## 13. 운영 / 검증

- **정적 검증 스크립트** ([scripts/](../scripts))
  - `verify_character_index.py` — face_id 사전 인덱스 단위 검증
  - `verify_tts_planner.py` — TTS cue 합성/필터그래프 단위 검증
  - `print_sequence_ids.py` — sequence_id 부여 점검
- **테스트** ([tests/](../tests)) — PR/라운드별 단위 테스트 (silence_cut, context helpers, intro/credits 필터, chunk transcribe, story tts cues, subtitle formatting, title 등)
- **DB 마이그레이션** — Alembic (`migrations/versions/`)
- **회귀 보호**
  - 모든 핵심 dataclass는 frozen
  - 모든 단계가 graceful fallback (deepface/yt-dlp 미설치, SRT 없음 등)

---

## 14. 알려진 정리 대상 / 향후 개선

**정리 대상 (코드-문서 동기화 중 발견)**
- `app/modules/intro_credits_detector.py` — 어디서도 import되지 않음 (PR-2 `chunk_intro_credits_ranges`로 대체)
- `app/api/authApi(token).py` — 레거시 JWT 버전, `main.py` 미등록
- `app/core/exceptions.py` — 빈 파일
- `gemini_client.py` 의 skeleton(`analyze_full…`) 함수 — 단계 제거 후 미호출 잔재
- [security.py:38](../app/core/security.py) — 평문 API 키/해시를 콘솔에 출력하는 디버그 로그
- `/upload` 라우터 경로 중복(`/upload/upload/file`)
- 진행 표시 카운터 불일치 (`[N/13]` vs `[N/15]` 혼재)
- README 파일명이 `README.me` (→ `README.md` 권장)

**향후 개선**
- 다국어 voice 풀 확장
- character_index 커버리지 향상 (sample interval/threshold 조정)
- chunk-level 메타(summary/title_candidates) 다운스트림 활용
- 멀티 트랙 자막 (다국어 동시 출력)
- 톤 프리셋(드라마틱/예능/다큐) 사용자 선택 옵션
```
