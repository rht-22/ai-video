# AI Video Shorts Creator — 아키텍처 문서

## 1. 프로젝트 개요

**한 문장**: 한국어 드라마/예능 영상에서 60초 세로 쇼츠를 자동 제작하는 16단계 파이프라인.

**입력**
- 원본 영상 (mp4) + 자막 (SRT/ASS/VTT/SMI) — 또는 YouTube URL
- 작품명, 주제(선택), 이전 화 요약(선택)

**출력**
- 9:16 세로 쇼츠 mp4 (1~3개)
- 편집 계획 JSON, 실행 로그, 단계별 체크포인트

**차별점**
- Gemini 멀티모달(영상+자막) 직접 분석 + Flash 스토리·TTS 결정 분리
- face_id 사전 인식 인덱스로 인물·크롭·자막 매핑 일관성 확보
- TTS cue를 클립과 분리해 voice/speed/위치 가변 적용
- 16단계 모두 체크포인트 → 단계별 부분 재실행으로 비용·시간 최소화

---

## 2. 핵심 가치

| 항목 | 설명 |
|------|------|
| **Hands-off 60초 쇼츠** | 영상 한 편 던지면 후킹·서사·자막·TTS·렌더까지 자동 |
| **재실행 친화** | 16개 체크포인트로 어느 단계부터든 `--from-step X --job-id Y`로 부분 재실행 |
| **비용 최적화** | Pro는 청크 멀티모달 분석에만, Flash는 가벼운 의사결정에 사용 |
| **품질 보강** | face_id 사전 패스로 인물 식별 일관성, TTS 톤은 작품 분위기 매칭 |

---

## 3. 기술 스택

| 카테고리 | 기술 |
|----------|------|
| 언어 | Python 3.x |
| LLM | Google Gemini API — `gemini-3.1-pro-preview` (Pro), `gemini-3-flash-preview` (Flash) |
| 영상 처리 | ffmpeg / ffprobe |
| 얼굴 인식 | deepface (ArcFace) + opencv |
| TTS | edge-tts (Edge Neural Voices, 한국어 3종) |
| 다운로드 | yt-dlp (YouTube URL 입력 시) |
| 자막 | SRT/ASS/VTT/SMI 파서, libass 렌더 |

---

## 4. 시스템 아키텍처

```
┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│   입력 처리      │   │    AI 분석       │   │   합성 / 렌더    │
│   (1~6단계)      │ → │   (7~12단계)     │ → │   (13~16단계)    │
│ ────────────── │   │ ────────────── │   │ ────────────── │
│ • init / job_id │   │ • skeleton      │   │ • transcribe    │
│ • research      │   │ • face_index    │   │ • resources     │
│ • probe         │   │ • gemini chunk  │   │ • render        │
│ • proxy         │   │ • graph         │   │ • validate      │
│ • exclusion     │   │ • story         │   │                 │
│ • chunk split   │   │ • tts_plan      │   │                 │
└─────────────────┘   └─────────────────┘   └─────────────────┘
        │                     │                     │
        └─── 단계별 체크포인트 (.json) — 어느 지점부터든 재시작 가능 ───┘
```

### 모듈 분할

```
app/
├─ cli.py                       CLI 진입점, argparse 분기
├─ pipeline.py                  16단계 오케스트레이션
├─ config.py                    AppConfig / DesignConfig
└─ modules/
   ├─ gemini_client.py          Pro/Flash 호출, 4개 프롬프트
   ├─ face_id.py                FaceIdentifier + 사전 인덱스 lookup
   ├─ media_probe.py            ffprobe 래퍼
   ├─ chunker.py                proxy 청크 분할
   ├─ intro_credits_detector.py 인트로/크레딧 제외 구간
   ├─ moment_ranker.py          Union-Find sequence_id 부여
   ├─ story_builder.py          StoryClip / TTSCue dataclass
   ├─ subtitle.py               SRT 파싱, ASS 빌더
   ├─ silence_cutter.py         무음 구간 컷
   ├─ tts.py                    Edge TTS, voice 4 / speed 5 프리셋
   ├─ reframe.py                얼굴 추적 크롭 타임라인
   ├─ renderer.py               ffmpeg 합성 (filter_complex)
   ├─ validator.py              산출물 검증
   └─ youtube_downloader.py     yt-dlp 영상+자막 다운로드
```

---

## 5. 16단계 파이프라인 (요약)

| # | 단계 | 입력 | 출력 / 체크포인트 |
|---|------|------|-------------------|
| 1 | **init** | PipelineInput | output_dir, run_log.json |
| 2 | **research** | work_title | work_context, episodes_context, cast_images → checkpoint_research.json |
| 3 | **probe** | video_path | MediaInfo (duration, w×h, fps) → checkpoint_probe.json |
| 4 | **proxy** | video_path | 480p fps=4 mp4 (Gemini 분석용) |
| 5 | **exclusion** | duration, srt | 인트로/크레딧 제외 구간 → checkpoint_exclusion.json |
| 6 | **chunk** | proxy + exclusion | 300초 chunk × N (10초 overlap) |
| 7 | **skeleton** | proxy | 작품 전체 의도/serial position → narrative_skeleton.json |
| 8 | **character_index** | proxy + cast_images | face_id 사전 패스로 인물 등장 인덱스 → checkpoint_character_index.json |
| 9 | **gemini** | chunk + skeleton + 인덱스 | candidate_moments 30~50개 → checkpoint_gemini.json |
| 10 | **graph** | candidates | 장면 관계 엣지 → checkpoint_graph.json |
| 11 | **story** | candidates + edges + skeleton | hook/build/payoff storyline 3종 (Flash) → checkpoint_story.json |
| 12 | **tts_plan** | storyline | TTSCue 리스트 (위치 + voice + speed, Flash) → checkpoint_tts_plan.json |
| 13 | **transcribe** | clips + srt | 클립별 전사 + 무음 컷 → full_audio.json |
| 14 | **resources** | clips + cues | 크롭 타임라인 + cue별 mp3 → checkpoint_resources.json |
| 15 | **render** | clips + 자막 + cue mp3 | 최종 9:16 mp4 (ffmpeg filter_complex) |
| 16 | **validate** | output mp4 | duration_ok / audio_peak_ok / black_frame_ok |

---

## 6. 단계별 상세

### 1. init
- 새 job 시작 시 `colocated_marketin_<2hex>` 같은 고유 디렉토리 생성
- `--job-id`로 기존 디렉토리 재개 가능

### 2. research
- Gemini Flash로 작품 시놉시스/인물/회차 줄거리 자동 검색
- TMDb로 배우 프로필 이미지 다운로드
- `--no-research`로 스킵 가능

### 3. probe
- ffprobe로 영상 메타데이터 추출

### 4. proxy
- 원본을 480p / fps=4로 인코딩 (Gemini 분석 비용·시간 최소화)
- 한 번 만들면 모든 후속 분석에서 재사용

### 5. exclusion
- skip_intro/credits 인자 + SRT 끝 부분 자동 감지
- skeleton 분석 결과(intro/credits flag)로 한 번 더 보정

### 6. chunk
- 300초 단위 분할, 10초 overlap
- 각 chunk를 별도 mp4로 split (Gemini File API 업로드용)

### 7. skeleton
- 전체 영상을 Flash가 한 번에 본 뒤 의도/주요 장면/인트로/크레딧 등 메타 추출
- 각 chunk 분석에 컨텍스트로 재사용

### 8. character_index ⭐
- 프록시를 2초당 1프레임 샘플링 → ArcFace로 인물별 등장 구간 + 정규화 좌표 인덱스 생성
- chunk 분석 단계에 chunk 범위로 필터링되어 페이로드에 첨부됨
- 렌더 단계의 크롭 타임라인 생성 시 lookup으로 ArcFace 호출 ~75% 절감

### 9. gemini (Pro 멀티모달)
- 각 chunk 영상 + 자막 + character_appearances + skeleton + 이전 chunk 요약을 받아
- segments[] (전체 타임라인 묘사) + candidate_moments[] (쇼츠 후보) 출력
- 점수·랭킹 없이 description / highlight_eligible / continues_from 등 의미적 평가만
- 인트로/크레딧 후보는 후처리에서 필터링

### 10. graph
- candidate들 사이의 setup_payoff / continuous / consequence / character_arc 등 관계 추출
- assign_sequence_ids로 cross-chunk 연속 장면을 같은 sequence로 묶음

### 11. story (Flash)
- candidates + edges + skeleton + 이전 화 컨텍스트로 storyline 결정
- 두 가지 모드:
  - **storytelling**: hook + build×N + payoff (50~75초)
  - **highlight**: 단일 클립 완결형 (5~30초)
- 3개 storyline을 점수순으로 만들고 1~3위를 max-shorts 안에서 채택

### 12. tts_plan ⭐ (Flash)
- 결정된 storyline의 클립 시퀀스를 받아 편집 타임라인 절대 시간 기준 cue 리스트 생성
- 출력: `[{start_sec, end_sec, text, voice, speed}]`
- voice 4종 (`narrative_female / narrative_male / dramatic_low / bright_high`)
- speed 5단계 (`very_slow / slow / normal / fast / very_fast`)
- 톤 가이드: 격식체·슬랭 모두 금지, 명사형 종결 / 상황 설명 / 궁금증 유발 위주

### 13. transcribe
- 선택된 클립 구간만 전사 (SRT 우선, 없으면 Whisper 폴백)
- 무음 ≥ 0.8s 구간 자동 제거

### 14. resources
- 클립별 얼굴 크롭 타임라인 (build_crop_timeline + character_index lookup)
- TTS cue별 mp3 합성 (voice/speed 적용)

### 15. render
- ffmpeg filter_complex로:
  - 클립 concat → blur 배경 + 9:16 크롭
  - 제목·작품명 텍스트 오버레이
  - 메인 자막 ASS (TTS 구간엔 숨김)
  - TTS 자막 ASS (`--no-tts-subtitles`로 토글)
  - 원본 오디오 덕킹 (TTS 구간 -6dB) + cue별 adelay 절대시간 + amix
- GPU 폴백: d3d11va → cuda → libx264

### 16. validate
- duration / audio peak / black frame 검증
- 실패 시 run_log에 경고 기록

---

## 7. 핵심 데이터 모델

### StoryClip (frozen dataclass)
```python
StoryClip(role, start_sec, end_sec, subtitle, use_original_audio,
          chunk_index, candidate_index, character_focus, bridges_from_previous)
```

### TTSCue (frozen dataclass)
```python
TTSCue(start_sec, end_sec, text,
       voice="narrative_female", speed="normal")
```

### candidate_moment (Gemini 출력)
```jsonc
{
  "chunk_index": 0, "candidate_index": 3,
  "start_sec": 12.4, "end_sec": 25.8,
  "characters_in_scene": ["..."], "character_focus": ["..."],
  "description": "5문장 이상 객관적 묘사",
  "transcript": "단 한 명의 주요 발화",
  "scene_location": "...", "timeline_position": "현재|과거|불명",
  "continues_from": null,
  "requires_context": false,
  "highlight_eligible": false, "highlight_reason": null
}
```

### character_appearance (face_id 인덱스)
```jsonc
{
  "character": "최희로",
  "start_sec": 120.0, "end_sec": 138.0,
  "samples": [{"t": 120.0, "x_norm": 0.45, "y_norm": 0.30, "similarity": 0.62}, ...]
}
```

---

## 8. AI 모델 분담

| 모델 | 단계 | 역할 |
|------|------|------|
| `gemini-3.1-pro-preview` (Pro) | 9 (chunk 멀티모달) | 영상 + 자막 직접 보고 candidate_moments 추출 |
| `gemini-3-flash-preview` (Flash) | 2, 7, 10, 11, 12 | research / skeleton / graph / story / tts_plan |
| Edge TTS (한국어 3 voice × pitch) | 14 | cue별 mp3 합성 |
| ArcFace (deepface) | 8, 14 | 인물 등장 인덱스 + 크롭 타겟 lookup |
| Whisper (선택, SRT 없을 때) | 13 | 전사 폴백 |

---

## 9. CLI 사용법

```bash
# 기본 (3 쇼츠)
python -m app.cli create_shorts \
  --video /path/to/video.mp4 \
  --subtitle /path/to/subtitle.srt \
  --title "작품명"

# YouTube URL
python -m app.cli create_shorts \
  --youtube-url https://www.youtube.com/watch?v=XXXX \
  --title "작품명" --max-shorts 1

# 부분 재실행 (캐시 활용)
python -m app.cli create_shorts \
  --video ... --title "..." \
  --from-step tts_plan --job-id 작품명_<2hex>

# TTS 자막 끄기 (음성은 그대로)
python -m app.cli create_shorts ... --no-tts-subtitles

# 메인 자막까지 모두 끄기
python -m app.cli create_shorts ... --no-subtitles --no-tts-subtitles

# 인트로/엔딩 강제 스킵
python -m app.cli create_shorts ... --skip-intro 90 --skip-credits 120
```

`--from-step` 가능 단계: init / research / probe / proxy / exclusion / chunk / skeleton / character_index / gemini / graph / story / tts_plan / transcribe / resources / render / validate

---

## 10. 출력 디렉토리 구조

```
outputs/<work_title_slug>_<2hex>/
├─ run_log.json                    실행 로그 (전체 단계 결과)
├─ edit_plan.json                  최종 편집 계획
├─ shorts.mp4 / shorts_2.mp4 / shorts_3.mp4   결과 쇼츠 1~3개
├─ subtitles*.ass / tts_subtitles*.ass         자막 ASS (TTS 자막은 토글 시 미사용)
├─ tts_cue_*.mp3                   cue별 TTS 음성
├─ crop_*.json                     얼굴 크롭 타임라인
├─ checkpoint_research.json
├─ checkpoint_probe.json
├─ checkpoint_exclusion.json
├─ narrative_skeleton.json
├─ checkpoint_character_index.json
├─ checkpoint_gemini.json
├─ checkpoint_graph.json
├─ checkpoint_story.json
├─ checkpoint_tts_plan.json
├─ checkpoint_resources.json
└─ _research/                      TMDb 배우 프로필 이미지
```

---

## 11. 운영 / 검증

- **정적 검증 스크립트**
  - `scripts/verify_character_index.py` — face_id 사전 인덱스 단위 검증 (Phase A)
  - `scripts/verify_tts_planner.py` — TTS cue 합성/필터그래프 단위 검증

- **회귀 보호**
  - 모든 dataclass는 frozen
  - 모든 LLM 입력은 `prompt.format(**payload)` 정합성을 정적 스크립트로 검증
  - 모든 단계가 graceful fallback (deepface/yt-dlp 미설치, SRT 없음 등)

---

## 12. 향후 개선

- 다국어 voice 풀 확장 (현재 한국어 3 voice)
- character_index 커버리지 향상 (현재 ~8%, sample interval/threshold 조정)
- chunk-level 메타(summary/title_candidates) 다운스트림 활용
- 멀티 트랙 자막 (다국어 동시 출력)
- 톤 프리셋 (드라마틱/예능/다큐 등) 사용자 선택 옵션
