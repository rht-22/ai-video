# AI Video Shorts Creator — 아키텍처 문서

> PR-7 기준 (12단계). 상세 설계 변경 이력은 [docs/goal-pipeline-v2.md](goal-pipeline-v2.md),
> 가편집 재분석(av_align)은 [docs/goal-av-align.md](goal-av-align.md) 참조.

## 1. 프로젝트 개요

**한 문장**: 한국어 드라마/예능 영상에서 40~60초 세로 쇼츠를 자동 제작하는 12단계 파이프라인.

**입력**
- 원본 영상 (mp4) + 자막 (SRT/ASS/VTT/SMI) — 또는 YouTube URL
- 작품명, 주제(선택), 이전 화 요약(선택)

**출력**
- 9:16 세로 쇼츠 mp4 (1~3개)
- 편집 계획 JSON, 실행 로그, 단계별 체크포인트

**차별점**
- Gemini 멀티모달(영상+전사) 직접 분석 — 인물 식별·구도 좌표·관계까지 Gemini가 직접 산출
- **video_intent**: 사람이 에피소드를 통으로 한 번 보듯 전체 파악 + 내용 기반 청크 경계 + 전사를 1단계로 통합
- **av_align 2-pass**: 1차 가편집본을 Gemini Pro가 다시 시청해 자막·TTS 위치·dead-air를 편집 타임라인 기준으로 교정
- 12단계 모두 체크포인트 → `--from-step X --job-id Y`로 부분 재실행

---

## 2. 핵심 가치

| 항목 | 설명 |
|------|------|
| **Hands-off 쇼츠** | 영상 한 편 던지면 후킹·서사·자막·TTS·렌더까지 자동 |
| **재실행 친화** | 단계별 체크포인트로 어느 단계부터든 `--from-step`로 부분 재실행 |
| **비용 최적화** | Pro는 청크 멀티모달 분석·가편집 재분석에만, Flash는 파악/스토리에 사용 |
| **단일 진실원** | `_STEP_ORDER` 한 곳이 단계·진행 로그 번호를 결정(`_step_tag`가 `[N/12]` 자동 산출) |

---

## 3. 기술 스택

| 카테고리 | 기술 |
|----------|------|
| 언어 | Python 3.10+ |
| LLM | Google Gemini API — `gemini-3.1-pro-preview` (Pro), `gemini-3-flash-preview` (Flash) |
| 영상 처리 | ffmpeg / ffprobe |
| 얼굴 추적 | opencv(Haar) 얼굴 탐지 + Gemini 정규화 좌표로 타깃 식별 (ArcFace/deepface 제거 — PR-7) |
| TTS | edge-tts (Edge Neural Voices, 한국어) |
| 전사 | SRT/ASS/VTT/SMI 파서 (우선) → 없으면 Gemini(Flash) 전사 |
| 다운로드 | yt-dlp (YouTube URL 입력 시) |
| 자막 렌더 | libass(ASS) |

---

## 4. 시스템 아키텍처

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  준비             │   │  파악 → 분석      │   │  콘티 → 후처리     │   ┌──────────┐
│  init·research    │ → │  video_intent     │ → │  story            │ → │ 마무리    │
│  probe·proxy      │   │  chunk → gemini   │   │  av_align         │   │ render    │
│                   │   │                   │   │  resources        │   │ validate  │
└──────────────────┘   └──────────────────┘   └──────────────────┘   └──────────┘
        │                       │                      │                    │
        └──── 단계별 체크포인트 (.json) — 어느 지점부터든 --from-step 재시작 ────┘
```

사람의 편집 워크플로 대응:
- **준비(init~proxy)**: 원본 소재를 분석 가능한 형태로 준비
- **파악(video_intent~chunk)**: 통으로 한 번 보고 내용 파악 + 의미 단위 구간 분할 + 전사
- **분석(gemini)**: 각 구간을 깊게 정독하며 쇼츠 후보 추출
- **콘티(story)**: 후보로 쇼츠 콘티/순서/편집점/구도 설계
- **후처리(av_align~resources)**: 가편집 검수 + 자막/TTS/크롭 리소스 생성
- **마무리(render~validate)**: 최종 렌더 + 품질 검증

### 모듈 분할

```
app/
├─ cli.py                       CLI 진입점, argparse 분기
├─ pipeline.py                  12단계 오케스트레이션 (_STEP_ORDER 단일 출처)
├─ config.py                    AppConfig / DesignConfig
└─ modules/
   ├─ gemini_client.py          Pro/Flash 호출: analyze_chunk, analyze_video_intent,
   │                            transcribe_proxy, compose_story_with_context, analyze_rendered_cut
   ├─ face_id.py                find_target_in_index — 좌표 lookup (ArcFace 제거됨)
   ├─ media_probe.py            ffprobe 래퍼
   ├─ chunker.py                build_chunks(내용 기반 boundaries 지원) / split_video_chunk
   ├─ moment_ranker.py          assign_sequence_ids — continues_from 기반 Union-Find
   ├─ story_builder.py          StoryClip / TTSCue dataclass
   ├─ subtitle.py               자막 파서(parse_subtitle) + ASS 빌더
   ├─ subtitle_styles.py        장르 기반 자막 스타일 프리셋
   ├─ silence_cutter.py         dead_air 컷 적용(apply_dead_air_cuts) + 시간 remap
   ├─ beat_trimmer.py           내용 기반(beats) 길이 단축
   ├─ scene_detect.py           씬 경계 감지 (분석 보조)
   ├─ speech.py                 Whisper 전사 (현재 파이프라인 미사용 — 헬퍼/폴백 보존)
   ├─ reframe.py                opencv 얼굴 탐지 + Gemini 좌표 크롭 타임라인
   ├─ renderer.py               ffmpeg 합성(render_short) + 가편집(render_rough_cut)
   ├─ validator.py              산출물 검증
   ├─ tmdb_client.py            배우 프로필 이미지 다운로드
   ├─ work_researcher.py        작품 시놉시스/인물 리서치
   └─ youtube_downloader.py     yt-dlp 영상+자막 다운로드
```
> `intro_credits_detector.py`는 PR-2 이후 미사용(인트로/크레딧은 gemini `chunk_intro_credits_ranges`가 처리).

---

## 5. 12단계 파이프라인 (요약)

`_STEP_ORDER` ([app/pipeline.py](../app/pipeline.py)) 가 단일 출처.

| # | 단계 | 입력 | 출력 / 체크포인트 |
|---|------|------|-------------------|
| 1 | **init** | PipelineInput | output_dir, run_log.json |
| 2 | **research** | work_title, episode | work_context, cast 이름/이미지 → checkpoint_research.json |
| 3 | **probe** | video_path | MediaInfo (duration, w×h, fps) → checkpoint_probe.json |
| 4 | **proxy** | video_path | 480p fps=4 mp4 (분석용, 재사용) |
| 5 | **video_intent** | proxy (+SRT) | overview + chunk_boundaries + transcript → checkpoint_video_intent.json |
| 6 | **chunk** | proxy + boundaries | ≥5분 정규화 후 chunk × N split |
| 7 | **gemini** (Pro) | chunk + transcript 슬라이스 + 이전 chunk 요약 | segments + candidate_moments + characters_tracking(좌표) → checkpoint_gemini.json |
| 8 | **story** (Flash) | candidates + chunk_meta + 컨텍스트 | storyline 변형 + tts_cues 초안 → checkpoint_story.json |
| 9 | **av_align** (Pro) | storyline → 가편집 렌더 | dialogue + tts_cues + dead_air_cuts → checkpoint_av_align.json |
| 10 | **resources** | clips + av_align + gemini 좌표 | 크롭 타임라인 + cue mp3 + 자막 ASS + edit_plan → checkpoint_resources.json |
| 11 | **render** | clips + 자막 + cue mp3 | 최종 9:16 mp4 (ffmpeg filter_complex) |
| 12 | **validate** | output mp4 | duration / audio peak / black frame |

---

## 6. 단계별 상세

### 1. init
- 새 job 시작 시 `<work_slug>_<2hex>` 고유 디렉토리 생성. `--job-id`로 기존 디렉토리 재개.
- 제거된 단계의 옛 체크포인트(exclusion/character_index/chunk_transcripts/graph/tts_plan/…)는 재개 시 자동 삭제(마이그레이션).

### 2. research
- Gemini Flash로 작품 시놉시스/인물/회차 줄거리 자동 검색, TMDb로 배우 프로필 이미지 다운로드.
- cast 이름은 전사 화자 라벨·분석 컨텍스트로 사용. `--no-research`로 스킵.

### 3. probe
- ffprobe로 영상 메타데이터(길이/해상도/fps/오디오) 추출.

### 4. proxy
- 원본을 480p / fps=4로 인코딩 (Gemini 분석 비용·시간 최소화). 한 번 만들면 후속 분석에서 재사용.

### 5. video_intent ⭐ (Flash)
- `analyze_video_intent`로 전체 프록시를 1회 스캔 → intent/emotional_arc/key_characters/story_beats/intro·credits + **chunk_boundaries** 산출.
- **전사**: SRT 있으면 `parse_subtitle`, 없으면 `transcribe_proxy`(Flash, 경계-윈도우 단위) → flat 절대시간 `SpeechSegment[]`.
- 최종 자막은 av_align dialogue가 우선이므로 여기 전사는 분석 컨텍스트 등급.

### 6. chunk
- `_normalize_chunk_boundaries`로 video_intent 경계를 **≥5분(300s) 하한 병합 / max 분할 / 연속 커버리지** 정규화.
- `build_chunks(boundaries=...)` → 각 청크를 `split_video_chunk`로 별도 mp4 분할(Gemini File API 업로드용). 경계 없으면 고정 길이(600s) 폴백.

### 7. gemini ⭐ (Pro 멀티모달)
- 각 chunk 영상 + 해당 청크 transcript 슬라이스 + 이전 chunk 요약을 받아:
  - `segments[]`(타임라인 묘사) + `candidate_moments[]`(쇼츠 후보, `continues_from`/`beats`/`event_template` 포함)
  - `characters_tracking[]`: 인물별 등장 구간 + **정규화 좌표 `x_norm/y_norm`(+samples)** — 크롭 추적 입력
  - `chunk_intro_credits_ranges`: 인트로/크레딧/리캡 구간(후처리 필터)
- 응답은 청크-상대 시간 → 파이프라인이 `actual_cut_offset` 보정으로 절대시간화. characters_tracking은 `_build_character_appearances`로 reframe 호환 `character_appearances`(overlap 병합)로 변환.

### 8. story (Flash)
- `assign_sequence_ids`(continues_from 기반)로 cross-chunk 연속 장면을 같은 sequence로 묶고,
- `compose_story_with_context`로 storyline 결정. 두 모드:
  - **storytelling**: hook + build×N + payoff
  - **highlight**: 단일 클립 완결형
- 변형들을 점수순으로 만들고 max-shorts 안에서 채택. tts_cues 초안 포함, `beat_trim_storyline`로 길이 보정.

### 9. av_align ⭐ (Pro)
- 출력하는 variant마다: `render_rough_cut`(480p/10fps, 타임코드 번인)로 가편집본 렌더 → `analyze_rendered_cut`(Pro)가 시청.
- 산출: `dialogue`(정확한 화자·대사 → 최종 자막 원천), `tts_cues`(영상에 맞춘 재배치), `dead_air_cuts`(대사 사이 정적).
- `apply_dead_air_cuts` + `remap_timed_items`로 컷 적용 및 dialogue/cue 시간 보정. 컷 후 너무 짧으면 롤백.

### 10. resources
- `_build_character_appearances`(gemini 좌표) → `reframe.build_crop_timeline`/`build_multi_face_crop_timeline`로 얼굴 추적 크롭 타임라인 생성(opencv 탐지 + 좌표 식별, ArcFace 미호출).
- av_align `dialogue`로 자막 ASS, `tts_cues`로 cue별 mp3 합성(voice/speed), edit_plan 작성.

### 11. render
- ffmpeg filter_complex: 클립 concat → blur 배경 + 9:16 크롭, 제목·작품명 오버레이, 메인+TTS 자막 ASS, 원본 오디오 덕킹 + cue adelay + amix. GPU 폴백(d3d11va → cuda → libx264).

### 12. validate
- duration / audio peak / black frame 검증, 실패 시 run_log에 경고.

---

## 7. 핵심 데이터 모델

### StoryClip / TTSCue (frozen dataclass, [story_builder.py](../app/modules/story_builder.py))
```python
StoryClip(role, start_sec, end_sec, subtitle, use_original_audio,
          chunk_index, candidate_index, character_focus, visual_essential, tts_draft, ...)
TTSCue(start_sec, end_sec, text, voice="ko_female", speed="normal")
```

### candidate_moment (Gemini 출력, 발췌)
```jsonc
{
  "chunk_index": 0, "candidate_index": 3, "segment_index": 1,
  "start_sec": 12.4, "end_sec": 25.8,
  "characters_in_scene": ["..."], "character_focus": ["..."],
  "description": "객관적 묘사", "transcript": "주요 발화",
  "continues_from": null, "highlight_eligible": false, "visual_essential": false,
  "event_template": {"subject": "...", "action": "...", "mode": "in_person", "location": "..."},
  "beats": [{"start_sec": 12.4, "end_sec": 18.0, "summary": "...", "importance": "core"}]
}
```

### characters_tracking → character_appearance (크롭 좌표, PR-7)
```jsonc
// gemini 출력 (chunk-level, 좌표 추가)
{"character": "최희로", "appearances": [
  {"start_sec": 120.0, "end_sec": 138.0, "action": "...",
   "x_norm": 0.45, "y_norm": 0.30,
   "samples": [{"t": 120.0, "x_norm": 0.4, "y_norm": 0.3}, {"t": 135.0, "x_norm": 0.6, "y_norm": 0.3}]}]}
// → _build_character_appearances → reframe 가 face_identifier=None 으로 소비
```

### av_align 출력
```jsonc
{
  "dialogue":      [{"start_sec": 0.0, "end_sec": 3.2, "speaker": "민준", "text": "..."}],
  "tts_cues":      [{"start_sec": 3.5, "end_sec": 6.0, "text": "...", "voice": "ko_female", "speed": "normal"}],
  "dead_air_cuts": [{"start_sec": 8.0, "end_sec": 9.5, "reason": "대사 사이 정적"}]
}
```

---

## 8. AI 모델 분담

| 모델/도구 | 단계 | 역할 |
|------|------|------|
| `gemini-3.1-pro-preview` (Pro) | 7 gemini, 9 av_align | 청크 멀티모달 분석 / 가편집본 재분석 |
| `gemini-3-flash-preview` (Flash) | 2 research, 5 video_intent, 8 story | 리서치 / 전체 파악·전사 / 스토리 구성 |
| Edge TTS (한국어) | 10 resources | cue별 mp3 합성 |
| opencv(Haar) + Gemini 좌표 | 10 resources | 얼굴 탐지 + 타깃 인물 식별 크롭 |
| SRT 파서 / Gemini 전사 | 5 video_intent | 자막 우선, 없으면 Gemini(Flash) 전사 |

> CLAUDE.md 규칙: Pro/Flash 두 모델만 사용. 다른 모델 금지.

---

## 9. CLI 사용법

```bash
# 기본 (3 쇼츠)
python -m app.cli create_shorts --video /path/video.mp4 --subtitle /path/sub.srt --title "작품명"

# YouTube URL (영상+자막 자동 다운로드)
python -m app.cli create_shorts --youtube-url https://youtu.be/XXXX --title "작품명" --max-shorts 1

# 부분 재실행 (캐시 활용)
python -m app.cli create_shorts --video ... --title "..." --from-step gemini --job-id 작품명_<2hex>

# 자막/인트로 옵션
python -m app.cli create_shorts ... --no-tts-subtitles
python -m app.cli create_shorts ... --skip-intro 90 --skip-credits 120
```

`--from-step` 단계: **init / research / probe / proxy / video_intent / chunk / gemini / story / av_align / resources / render / validate**
(구버전 단계명 `skeleton·exclusion·character_index·chunk_transcribe·graph·tts_plan·transcribe·silence_cut`은 가까운 현행 단계로 자동 redirect.)

---

## 10. 출력 디렉토리 구조

```
outputs/<work_title_slug>_<2hex>/
├─ run_log.json                    실행 로그 (전체 단계 결과)
├─ run_log_gemini.json             청크별 raw Gemini 응답 (진단용)
├─ edit_plan.json                  최종 편집 계획
├─ shorts.mp4 / shorts_2.mp4 / shorts_3.mp4   결과 쇼츠 1~3개
├─ rough_cut_*.mp4                 av_align 가편집본 (variant별)
├─ subtitles*.ass / tts_subtitles*.ass         자막 ASS
├─ tts_cue_*.mp3                   cue별 TTS 음성
├─ crop_*.json                     얼굴 크롭 타임라인
├─ checkpoint_research.json
├─ checkpoint_probe.json
├─ checkpoint_video_intent.json    overview + chunk_boundaries + transcript
├─ checkpoint_gemini.json          all_candidates + chunk_meta(characters_tracking 좌표)
├─ checkpoint_story.json
├─ checkpoint_av_align.json        variant별 dialogue/tts_cues/clips
├─ checkpoint_resources.json
└─ _source/ , _research/           다운로드 영상 / TMDb 배우 이미지
```

---

## 11. 운영 / 검증

- **테스트**: `/Users/.../ai-video/.venv/bin/python -m pytest tests/ -q` (예: `test_pr7_pipeline_v2.py` = 경계 정규화/좌표 빌드/크롭 가드, `test_pr6_av_align.py` = av_align)
- **정적 검증 스크립트**: `scripts/verify_tts_planner.py` — TTS cue 합성/필터그래프 단위 검증
- **회귀 보호**
  - 모든 dataclass는 frozen
  - 단계 추가/제거는 `_STEP_ORDER` 한 곳만 수정 → 진행 로그 번호 자동 반영
  - 모든 단계가 graceful fallback (Gemini 빈 응답·SRT 없음·좌표 없음 등)

---

## 12. 향후 개선

- **프레이밍 품질 검증**: Gemini 좌표 기반 크롭을 실제 영상으로 육안 검증(ArcFace 제거에 따른 수용 게이트). 회귀 시 samples 밀도↑.
- 다국어 voice 풀 확장 / 멀티 트랙 자막
- 톤 프리셋(드라마틱/예능/다큐) 사용자 선택
- no-SRT Gemini 전사 비용 최적화 (윈도우 크기/병렬화 튜닝)
