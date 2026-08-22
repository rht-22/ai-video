# AI Video Shorts Creator

## Gemini 모델 사용 규칙

이 프로젝트에서 Gemini API 모델은 반드시 아래 두 가지만 사용한다:

| 역할 | 모델명 |
|------|--------|
| 정밀 분석 (Pro) | `gemini-3.1-pro-preview` |
| 스크리닝/스토리 구성 (Flash) | `gemini-3.6-flash` |

`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-3.1-flash-preview` 등 다른 모델은 사용 금지.  
환경변수(`GEMINI_MODEL_NAME`, `GEMINI_FLASH_MODEL_NAME`)와 코드 기본값 모두 위 두 모델로 고정.

> Flash 는 2026-08-03 에 `gemini-3-flash-preview` → `gemini-3.6-flash` 로 올렸다(사용자 지시).
> `models.list()` 로 존재를 확인하고 바꿨다. **이 머신 로컬 변경이며 아직 푸시하지 않았다** —
> 다른 4대는 여전히 `gemini-3-flash-preview` 로 돈다.

## 아키텍처 개요

- **Pro 모델** (`gemini-3.1-pro-preview`): 청크별 영상 분석 (Agent 2, `analyze_chunk()`)
- **Flash 모델** (`gemini-3-flash-preview`): 스크리닝 및 스토리 구성 (Agent 1/3, `screen_candidates()`, `compose_story()`)
- **청크 분할**: 프록시(480p)를 청크별로 물리적 분할 후 각각 독립 업로드 → 10fps 유지 가능

## 영상 밴드 레이아웃 계약 (E10, 2026-08-21)

캔버스 1080×1920 안의 영상 직사각형("밴드")은 세 개의 디자인 키로 정해진다.
수식은 편집실 미리보기·ves-orchestrator 어댑터와 합의된 계약이다 — **바꾸려면
양쪽을 같이 바꿔야 한다**(어긋나면 미리보기가 거짓말을 한다).

- `aspect_ratio` — 밴드의 **모양** (W:H 문자열)
- `video_width` (`--design-video-width`) — 밴드의 **크기** (캔버스 px, 320~1080,
  기본 None = 미지정 = 꽉 찬 폭 + **종전 자막 기하**). 범위 밖·비숫자는 CLI·렌더
  경계 양쪽에서 즉시 실패, 홀수는 짝수 보정
- `video_y` (`--design-video-y`) — 밴드 상단 y (미지정 = 세로 중앙)

렌더 수식 (`renderer._build_filtergraph` [2]~[3]):

- `scaled_w = video_width` · `scaled_h = int(scaled_w * r_h / r_w)` — **밴드 폭 기준**
- `pad_x = (W - scaled_w) // 2` — 가로는 항상 중앙 (video_x 는 없다)
- `overlay_y` = video_y 지정 시 그 위치(`H - scaled_h` 클램프), 미지정 = 세로 중앙
- 클립 체인: `crop_timeline → scale=scaled_w:scaled_h:…increase → setsar=1 →
  crop=scaled_w:scaled_h → pad=W:H:pad_x:overlay_y`

밴드를 따라 움직이는 것들("영상영역 기준" 계약 — 밴드를 바꿔도 위치를 다시 잡지 않는다):

- **플랫폼 표기**: left/right 앵커가 캔버스가 아니라 **밴드 모서리** 기준
- **제목 동적 배치**(영상 위 20px)·**작품명/로고 클램프**(밴드 하단+20px):
  overlay_y·scaled_h 파생 — 실렌더 픽셀 실측으로 확인됨
- **메인 자막 margin_v**: `pipeline._compute_subtitle_margin_v` — 항상 밴드 하단 10px 위
- **TTS 자막 margin_v**: `pipeline._compute_tts_margin_v` — `tts_line_y_margin` 은
  사용자 노브라 절대 재계산이 아니라 **델타 앵커**(종전 기하 대비 밴드 하단이 움직인
  만큼 이동 = 밴드 하단으로부터의 오프셋 상수). 음수는 0 클램프(`build_tts_ass` 가
  음수를 '미지정' 480 폴백으로 해석)
- ⚠ **자막·TTS margin 의 밴드 앵커는 `video_width` 명시 시에만**(1080 포함).
  미지정이면 종전 기하(세로 중앙 가정·절대 tts_line_y_margin) 그대로 — video_y 만
  쓰는 기존 채널(한 입 주막 video_y=440·tts 550, 사람이 실렌더로 픽셀 튜닝)의 승인된
  출력이 조용히 움직이면 안 된다(8/21 발주 검수 교정). video_width 는 오케스트레이터
  editor_e10 게이트 뒤에서만 들어오므로 '명시 = 신규 채널'이 곧 회귀 0 조건이다.
  렌더 기하 자체는 명시 1080 과 미지정이 동일하다(차이는 자막 계열뿐).
- 밴드 기하 단일 소스: `pipeline._video_band_bottom` (렌더러 [2]와 같은 수식)

⚠ 레거시: `DesignConfig.video_width` 는 E10 이전에도 있었지만(기본 800) 렌더러가
읽지 않았다 — 렌더러가 읽기 시작하며 기본값을 None(미지정)으로 바꿨다(videoApi 도
미전달이면 None). **800 이 살아나면 플래그 없는 기존 채널 전부가 800px 로 줄어든다.**

회귀 가드: video_width·video_y 미지정이면 필터그래프·margin 모두 종전과 동일해야
한다 — `tests/test_e10_video_band_width.py` 가 문자열·수치로 고정한다.

## KR 내레이션 TTS 백엔드 계약 (E11, 2026-08-21)

`app/modules/tts.py` — 발주서: ves-orchestrator `docs/prompts/e11-kr-tts-elevenlabs.md`.

- 1순위 **ElevenLabs**: `ELEVENLABS_API_KEY` 가 있으면 무조건 ElevenLabs
  (모델 `eleven_multilingual_v2`, env `ELEVENLABS_MODEL_ID` 로 교체 가능).
  키가 없으면 edge-tts 폴백 — **stdout 에 `[TTS] backend=` 한 줄 명시**(조용한
  대체 금지). API 실패는 429·5xx·네트워크만 재시도(2회), 그 외 4xx 는 즉시 실패 —
  edge-tts 로 조용히 넘어가면 같은 채널 목소리가 편마다 달라진다.
- **voice/speed 라벨은 불변 계약**: `ko_female`·`ko_female_high`·`ko_male`·
  `ko_male_low`·`chat_*` 와 `very_slow`~`very_fast` 는 편집실·edit_overrides/v2·
  체크포인트 cue 에 실려 있는 값이다. ElevenLabs 매핑은 `EL_VOICE_PRESETS`
  (라벨 → premade voice_id, pitch 변형은 목소리 선정으로 재현)·`EL_SPEED`
  (very_slow 0.7 · slow 0.85 · normal 1.0 · fast 1.1 · very_fast 1.2 —
  voice_settings.speed 허용 범위 0.7~1.2) 한 곳에서만 고친다.
- 합성 백엔드는 run_log `steps[{step:"resources"}].tts_backend` 와
  `checkpoint_resources.tts_backend` 에 남는다 — 키 없는 노드의 폴백 추적 근거.
- edge-tts 경로의 '예외 → rate/pitch 빼고 재시도' 무성 폴백은 **제거됐다**(속도·
  피치가 소리 없이 무시되던 지점). 회귀 가드: `tests/test_e11_tts_elevenlabs.py`.

## 자막 전사 백엔드 계약 (E11, 2026-08-22)

`app/modules/stt_elevenlabs.py` · `pipeline.transcribe_chunks` · `--transcribe-backend`.
발주서: ves-orchestrator `docs/prompts/e11-transcribe-backend.md`.

- 플래그: `--transcribe-backend {default|elevenlabs}` — **미지정 = 종전 그대로(회귀 0)**.
  `default` 는 명시해도 미지정과 산출이 같다. 허용값 밖은 argparse `choices` 로 즉시 실패
  (조용히 기본값으로 떨어지면 사람은 일레븐랩스로 바꿨다고 믿은 채 종전 전사로 발행된다).
  오케스트레이터 어댑터가 채널 design 키 `transcribe_backend` 를 그대로 이 플래그로 넘긴다.
- **분기는 `transcribe_chunks` 의 transcriber 선택 한 곳에서 끝난다.** 반환 표현
  (`SpeechSegment` start/end/text)·청크 오프셋 가산·SRT 슬라이스 규칙은 두 백엔드가 공유 —
  `subtitle_segments.json` 이하 downstream 은 백엔드를 모른다(편집실 `source_time_sec`
  역산이 두 경로에서 같이 살려면 좌표계가 하나여야 한다).
- **자막 파일(SRT)이 있으면 전사 자체가 없다** — 백엔드 선택은 STT 엔진 노브라 무관하다.
  이때는 조용히 무시하지 않고 stdout 에 "SRT 를 그대로 씁니다" 한 줄을 남긴다.
- ElevenLabs 경로: `ELEVENLABS_API_KEY` 필수(없으면 CLI 사전검사에서 즉시 실패, 폴백 없음).
  `POST /v1/speech-to-text`, `model_id=scribe_v2`(env `ELEVENLABS_STT_MODEL_ID`),
  `language_code=kor`, `timestamps_granularity=word`, `tag_audio_events=false`, `diarize=false`.
  오디오만 올린다(`-vn -ac 1 -ar 16000 -c:a pcm_s16le`). 응답 `words[]` 는 `type=="word"`
  만 걸러 cue 로 묶는다(`spacing`·`audio_event` 제외). cue 경계 = 0.5s 공백 · 문장 종결부호 ·
  44자 · 6.0s — merge_subtitle_segments 와 같은 수치. `additional_formats`(서버 SRT)는
  **쓰지 않는다**(cue 규칙 정본은 이 파이프라인).
- 실패 분류: 401·403·기타 4xx = permanent(재시도 없음) / 429·5xx·네트워크 = transient(2회
  재시도). 청크 하나가 끝내 실패하면 **크게 실패한다** — 내장 경로의 '빈 결과로 진행'
  무성 폴백은 elevenlabs 에는 적용하지 않는다(유료 백엔드를 골랐는데 자막이 비면 안 된다).
- 기록: 백엔드를 **명시한 실행만** run_log `steps[{step:"chunk_transcribe"}]` 에 남고
  (`transcribe_backend`·`elevenlabs_stt.audio_duration_secs`·`estimated_usd`·
  `low_confidence_lines`), 사이드카 `checkpoint_chunk_transcripts_meta.json` 이 재개 시
  백엔드 전환을 감지해 캐시를 무효화한다. 미지정 실행은 이 파일들을 쓰지도 읽지도 않는다.
- A/B 실측 도구: `python -m scripts.e11_transcribe_ab --video … --ref …`
  (한국어는 WER 아님 — **CER**). 회귀 0 대조는 `--diff JOB_A JOB_B`.
- 회귀 가드: `tests/test_e11_transcribe_backend.py`.
