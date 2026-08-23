# AI Video Shorts Creator

## Gemini 모델 사용 규칙

이 프로젝트에서 Gemini API 모델은 반드시 아래 두 가지만 사용한다:

| 슬롯 | 모델명 | 쓰는 곳 |
|------|--------|---------|
| Pro (`GEMINI_MODEL_NAME`) | `gemini-3.1-pro-preview` | **영상 분석 하나뿐** — `analyze_chunk()` |
| Flash (`GEMINI_FLASH_MODEL_NAME`) | `gemini-3.6-flash` | **그 외 전부** |

`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-3.1-flash-preview` 등 다른 모델은 사용 금지.  
환경변수와 코드 기본값 모두 위 두 모델로 고정.

### 모델 사용 정책 (2026-08-23, 사용자 결정)

**Pro 는 영상을 실제로 보는 호출에만 쓴다.** 텍스트-온리 호출은 전부 Flash 최신이다.

| 호출 | 단계 | 슬롯 |
|------|------|------|
| `analyze_chunk` — 청크 영상 분석 | gemini | **Pro (유일)** |
| `extract_relationships` — 후보 관계 추출 | graph | Flash *(← Pro, 8/23 전환)* |
| `research_work`/`_search_with_grounding` — 작품 리서치 | research | Flash *(← Pro, 8/23 전환)* |
| `analyze_video_intent`·`compose_story_with_context`·`shorten_text`·`choose_beat_drops` | story 계열 | Flash |
| `compose_style` — E15 스타일 구성 | style | Flash |

- 리서치는 **세 갈래(그라운딩 켬/끔/폴백) 전부** 같은 모델이어야 한다 — 하나만 고치면
  폴백에서 조용히 모델이 바뀐다(`work_researcher._search_with_grounding` 의 `model_name` 지역변수).
- `GeminiConfig.model_name` 기본값이 금지 모델(`gemini-3.5-flash`)이던 것을 팩토리 env
  기본값과 같게 고쳤다 — 팩토리가 늘 덮어써서 무해했지만, `GeminiConfig` 를 직접 만드는
  코드·테스트는 그 값을 먹었다.
- 어느 호출이 어느 슬롯을 썼는지는 run_log `provenance.models.roles` 에 남는다. 두 슬롯
  이름(pro/flash)만으로는 전환 전후 산출물을 구분할 수 없어서 역할 표를 함께 남긴다.
- 회귀 가드: `tests/test_e15_style_compose.py` (`model=self.config.model_name` 은 코드
  전체에 **한 번만** 나와야 한다).

## 아키텍처 개요

- **Pro 모델** (`gemini-3.1-pro-preview`): 청크별 영상 분석 (Agent 2, `analyze_chunk()`)
- **Flash 모델** (`gemini-3.6-flash`): 그 외 전 호출 — 스크리닝·스토리 구성·관계 추출·
  리서치·제목 단축·비트 컷·스타일 구성
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

## 편집실 내레이션 목소리 계약 (E12, 2026-08-22)

`app/modules/tts.py` · `pipeline.synthesize_cue_cached`.
발주서: ves-orchestrator `docs/prompts/e12-editor-elevenlabs-voice.md`.

```
voice = "ko_female" | "ko_male" | "chat_*" | …   (지금 그대로)
      | "elevenlabs:{voice_id}"                   (신설 — 그 id 로 바로 합성)
```

- **접두사가 붙은 값만 새 경로.** 나머지는 종전 코드 그대로다(백엔드 선택·프리셋
  매핑·edge-tts 폴백 전부). 편집실 `voice` 는 `edit_overrides` 가 불투명 문자열로
  통과시키고 `_normalize_storyline_tts_cues` 의 화이트리스트는 **LLM 산 cue 에만**
  걸리므로, 접두사 값은 손실 없이 cue 까지 온다.
- **엔진은 목소리 이름표를 들지 않는다.** 사람이 읽는 이름은 대시보드
  (`ops_config.editor_tts_voices`)가 든다 — 계정마다 보이스 라이브러리가 달라
  1:1 미러가 불가능한 어휘다. 엔진은 형태(영숫자 16~32자)만 재확인한다.
- 모델 `eleven_multilingual_v2` 고정(`EL_MODEL_ID`). `eleven_v3` 는 `speed` 를
  안 받아 편집실 속도 프리셋이 죽으므로 쓰지 않는다. `language_code` 는 이 모델에서
  무시되므로 **보내지 않는다**.
- speed 는 `EL_SPEED` 한 곳(E11 표 그대로): very_slow 0.7 · slow 0.85 · normal 1.0 ·
  fast 1.1 · very_fast 1.2. ⚠ 편집실 길이 게이지가 가정하는 0.75/0.9/1/1.1/1.25 와
  **양 끝단이 다르다** — very_fast 상한이 1.2(문서 허용 최대)라 edge-tts(+25%)만큼
  안 벌어진다. 게이지 배율은 오케스트레이터에서 맞춘다.
- **캐시(요금)**: `synthesize_cue_cached` 가 키 `sha1(문구·voice·speed·fit창)` 로
  `<job>/tts_cache/{key}.mp3` 를 재사용한다 — 편집실 재렌더(`from_step=resources`)에서
  한 줄만 고쳐도 전 cue 가 다시 도는 구조라 캐시가 없으면 요금이 그대로 곱해진다.
  **유료 경로(접두사)만** 캐시한다 — edge-tts 는 아낄 요금이 없고, 캐시를 끼우면
  fit 재작성(Flash 단축) 결과가 달라져 지금 도는 내레이션이 변한다(회귀 0 조건).
- 실패 분류: 키 없음·voice_id 형태 오류·401·403·404(없는 voice_id) = permanent 즉시 실패 /
  429·5xx·네트워크 = transient(2회 재시도). **어느 경우도 기본 목소리로 안 떨어진다.**
- `checkpoint_resources.tts_cue_files` 모양은 그대로(오케스트레이터가 편집실 미리듣기로 올린다).
- 회귀 가드: `tests/test_e12_editor_elevenlabs_voice.py`.

## 전사 다듬기 계약 (E13, 2026-08-22)

`app/modules/stt_elevenlabs.py` · `app/data/transcribe_normalize_ko.json`.
발주서: ves-orchestrator `docs/prompts/e13-transcribe-polish.md`. E11 의 후속이고
**손대는 범위는 `--transcribe-backend elevenlabs` 경로 안**이다 — 미지정·`default`
실행은 전사 텍스트도 `subtitle_segments.json` 모양도 한 글자 안 바뀐다(회귀 0).
`speech.py`·`merge_subtitle_segments` 는 안 건드렸다.

- **keyterms 기본 on** — 내장 Whisper 가 `_build_whisper_prompt` 로 리서치 결과를 늘
  받는데 Scribe 쪽만 꺼져 있어 두 백엔드가 비대칭 조건으로 붙고 있었다.
  ⚠ 직렬화는 **같은 이름 폼 필드 반복**(`keyterms=A`, `keyterms=B`)이다. 배열 하나를
  `json.dumps` 로 담으면 서버가 그 문자열 전체를 키텀 하나로 보고 400
  `invalid_keyword_length` 를 준다(elevenlabs-python #819 의 v2.59.0 회귀와 같은 형태).
  요청 형태를 만드는 곳은 `build_form_fields` 한 곳. 제한(50자·5단어·`<>{}[]\` 불가·
  1000개)은 `_build_keyterms` 가 먼저 거른다 — 서버 400 은 permanent 라 리서치 결과에
  긴 항목 하나가 섞였다고 전사 전체가 죽으면 안 된다.
- **요율**: 기본 $0.22/오디오시간, keyterms 를 **실제로 실어 보낸 요청**만 $0.27.
  켜 놓고 리서치가 비어 폼에 안 실린 청크는 기본 요율이다 — `usage_summary` 가 오디오
  길이를 두 갈래로 나눠 센다(안 나누면 정산이 틀린다).
- **언어 이탈 cue 차단** — `language_code` 요청 언어의 문자군 + 라틴 + 숫자·문장부호
  밖 문자가 **cue 텍스트의 절반 이상**이면 그 cue 를 버린다(`謝 謝`). 한글 문장 안의
  한자 주석은 비율이 낮아 통과한다. 문자군을 모르는 언어(`ja`·`zh` 등)는 판정 자체를
  건너뛴다. **버린 줄은 전량 stdout·run_log 에 남는다.**
  ⚠ logprob 임계로 줄을 버리지 않는다 — 실측 저확신 3건 중 2건이 멀쩡한 한국어였다.
- **저확신 줄은 표시만** — 텍스트·시간 그대로, `subtitle_segments.json` 의 그 줄에만
  `"low_confidence": true` 한 키. 표시가 없으면 파일은 종전과 바이트까지 같다.
  좌표 변환은 자막이 쓴 것과 **같은 함수**(`remap_transcript_to_edited_timeline`)를 다시
  태워 얻는다(`pipeline._flag_low_confidence_segments`) — 누적 오프셋 수식을 베끼면
  언젠가 어긋난다. 구간은 `checkpoint_chunk_transcripts_meta.json` 에 원본 절대초로
  남아 `--from-step resources` 재개에서도 표시가 유지된다. 대시보드 노출은 별건(ves 몫).
- **표기 보정** — keyterms 로 못 잡는 일반 어휘(`CTO`·`IT업계`·`30년`)만 되돌린다.
  사전은 코드가 아니라 `app/data/transcribe_normalize_ko.json` 한 곳. 규칙은 둘뿐이고
  **둘 다 공백 토큰 완전 일치**다: (1) 토큰(열) 사전, (2) 십의 자리 수사(10~99)+단위.
  부분 문자열 치환은 안 한다 — `아이티` 단독 치환은 Haiti·아이티오를 깨뜨리므로
  `아이티 업계` 토큰 **열**로만 건다. 백·천·만은 '만 원'처럼 한글 표기가 정상이라 제외.
  cue 를 다 묶은 **뒤** 적용한다(경계 규칙은 원문 기준 — cue 분할이 안 흔들린다).
  바꾼 내역은 건별로 stdout·run_log 에 남는다.
- env 스위치: `ELEVENLABS_STT_KEYTERMS`·`ELEVENLABS_STT_NORMALIZE` (기본 on, `off` 로 끔).
- **E13-0 조사 결과(수정 대상 아님)**: 두 백엔드의 cue 수가 갈리는 주범은
  `_SENTENCE_END`(`[.!?…]$` flush)다. Scribe 는 한국어에 문장부호를 찍어 주고 Whisper 는
  거의 안 찍어서 같은 규칙이 Scribe 에서만 발동한다(가왕쇼 EP1 898→1468, 커리어데이
  EP8 206→180). 한쪽이 일관되게 낫지 않고(가왕쇼에서 Whisper 는 24.7초 cue 를 냈다)
  백엔드 선택은 채널 단위라 채널마다 자막 리듬이 다른 건 설계상 허용 범위다.
- A/B 도구: `python -m scripts.e11_transcribe_ab --video … --keyterm SK텔레콤 …`
  (기본 keyterms on/off 두 판). 회귀 0 대조는 `--diff JOB_A JOB_B`.
- 회귀 가드: `tests/test_e13_transcribe_polish.py`.

### 곁다리 2건 (같은 커밋)

- `ffmpeg_utils.find_ffmpeg_command` 미검출 메시지가 **실행 중인 OS 기준**으로 바뀌었다
  (운영 노드 6대는 macOS 인데 윈도우 설치법만 떴다). 비대화형 SSH 의 PATH 안내 포함.
- `tts.py` 의 voice/speed **라벨**이 모르는 값이면 즉시 실패한다(`_resolve_label`).
  E12 가 `elevenlabs:` 접두사 경로만 고쳤고 라벨 경로는 fail-silent 로 남아 있었다.
  ⚠ 구 체크포인트가 싣는 `narrative_female`(pipeline 의 voice 없는 cue 기본값)은
  `LEGACY_VOICE_ALIASES` 가 **오늘과 같은 목소리**로 보낸다 — 재개 산출이 달라지면 안 된다.

## 자막 최소 노출 시간 계약 (E14, 2026-08-23)

`app/modules/subtitle.py` — `_enforce_min_exposure` · `merge_subtitle_segments` 꼬리.
발주서: ves-orchestrator `docs/prompts/e14-subtitle-min-exposure.md`.

⚠ **E11·E13 과 달리 게이트가 없다.** `merge_subtitle_segments` 는 모든 생성이 지나는
길이라(elevenlabs·whisper·SRT 파싱 전부) 여기 손대면 전 채널 자막 타이밍이 함께 움직인다.
`deployments.auto_update=true` 라 main 머지 = 맥미니 6대 자동 갱신이다.

- **하한은 시간이 아니라 읽기 속도다.** `min_duration_sec=0.6` 은 "짧으면 되도록 다음 것과
  합쳐라"는 **병합 힌트**이고 `gap <= max_gap_sec` 일 때만 발동한다 — cue 를 늘려 주는
  코드는 없었다. 그래서 고립된 0.07초 cue 도, 0.72초/18자(25자/초)도 그대로 나갔다.
- 필요 노출 = `max(SUBTITLE_MIN_EXPOSURE_SEC 0.4, 글자수 / SUBTITLE_MIN_CHARS_PER_SEC 12)`,
  `max_duration_sec`(6초) 상한이 이긴다. 12자/초는 규격 원문이 egress 차단이라 2차 자료
  (한 줄 16~18자·1.5초 → 11~12자/초) + 실측으로 잡았고, 상수 하나만 고치면 바뀐다.
  절대 하한 0.4 는 **감탄사를 살리려고 낮게** 잡았다 — `박수!`(0.52초·3자)는 정상이라
  건드리면 안 된다(3자면 12자/초로 0.25초).
- **늘리는 것은 `end_sec` 뿐이고 빈 구간으로만.** `start_sec` 를 당기면 소리보다 자막이
  먼저 뜬다. 다음 cue 와 **이미 겹친 쌍은 손대지 않고**, 비어 있어도 다음 cue 시작까지만
  늘린다 — 겹침은 고칠 대상이 아니라 있는 그대로의 데이터다(`edit_overrides.py:236~`
  의 실측 기록: merge 가 겹치는 cue 를 만들고 ASS 도 그대로 그린다). 실측 45편에서
  겹침쌍 188 → 188, 신규 0.
- **늘려도 모자라면 건별로 stdout 에 남긴다**(`[SubtitleFloor/미달]` + 사유). 조용히
  포기하면 '고쳤는데 왜 그대로지'가 된다. run_log 에는 안 쓴다 — merge 는 순수 함수다.
- **편집실 경로는 범위 밖이다.** `edit_overrides` 자막은 재매핑 **뒤**에 `final_segments`
  를 통째로 갈아끼우므로(`pipeline.py:3510~`) merge 를 지나지 않는다. 사람이 0.3초로
  정했으면 그게 의도라 렌더 단계(`build_ass`)에 하한을 넣지 않았다.
- **순수 후처리**라 병합 결과 자체(줄 수·start·텍스트)는 한 글자도 안 바뀐다. 그래서
  저장된 실렌더 타임라인에 후처리만 다시 태우면 수정 전후 대조가 정확히 된다.
- 실측(editor_baselines/assets 45편 639줄, 전부 whisper 경로): 76줄(11.9%) 연장, 총
  +27.7초(편당 +0.62초), 길이 p50 2.87→3.02 · p90 5.51→5.51 · max 20.80→20.80,
  0.4초 미만 14→2줄, 12자/초 초과 129→102줄.
- 곁다리(E14-2): `stt_elevenlabs._CUE_MAX_CHARS` 주석이 실제(파이프라인은 40·config 값으로
  다시 부른다)와 달랐다 — 값이 아니라 **주석**을 고쳤다(44 를 40 으로 맞추면 E11·E13 전사가
  바뀐다).
- 회귀 가드: `tests/test_e14_subtitle_min_exposure.py`.

## 스타일 구성 단계 계약 (E15, 2026-08-23)

`app/modules/style_compose.py` · `pipeline.run_pipeline` 의 `[style]` 블록 ·
`gemini_client.compose_style`. 기획: ves-orchestrator `docs/prompts/e15-style-compose.md`.

스토리 구성이 끝난 편에 **연출 레이어**를 얹는다 — 효과 텍스트·자막 강조·스티커·
시간대별 제목·제목 기울기·내레이션 톤. 편집(장면·문구)은 안 건드린다.

- **게이트**: `--style-compose` — **미지정이면 단계가 통째로 없다**(회귀 0).
  체크포인트를 쓰지도 읽지도 않고 필터그래프·`subtitle_segments.json`·cue 가 종전과
  같다. 오케스트레이터는 채널 design 스위치 `style_compose` 를 이 플래그로 넘긴다
  (ves 0075 · ops_config `channel_style` 게이트).
- **자리**: `silence_cut → 【style】 → resources`. 앵커 좌표의 기준인 최종 클립이
  snap·클램프·중복제거까지 끝나야 확정되고(그 블록 주석: "이후 어떤 단계도 클립 경계를
  바꾸지 않는다"), TTS 톤은 resources 의 합성보다 앞서야 소리에 반영된다.
  **이 블록은 clips 를 바꾸지 않는다** — 바꾸면 자막·cue 좌표가 전부 어긋난다.
- **어휘는 전부 v3 재사용**: `texts`·`images` 는 `edit_overrides.validate_overrides`
  (v3 스탬프 문서를 만들어 통과시킨다)가 검증하고, 배치는 `place_anchored_*`,
  제목 창은 `validate_title_segments` 가 본다. 별도 검증기를 만들지 않았다 —
  사람 손과 AI 손이 다른 코드로 그려지면 언젠가 한쪽만 고쳐진다.
- **우선순위(파이프라인 블록이 강제)**: 편집실 오버라이드 > 채널 design **명시** 키 >
  AI 플랜 > 기본값. 편집실이 어떤 카테고리를 보내면 그 카테고리의 AI 항목은 **전량
  진다**(항목 병합 안 함 — 사람이 지운 연출이 살아 돌아오면 안 된다).
  채널 명시 여부는 `PipelineInput.design_explicit_fields`(cli 가 걷어 넘긴다)로 본다 —
  '기본값과 다른가'로 판정하면 채널이 기본값과 같은 값을 일부러 준 경우를 놓친다.
- **AI 에게만 좁은 규칙**(v3 보다 좁다):
  - 자막 강조는 `size`·`color` **둘만**(v3 는 y·rotate 도 있다). 위치·회전은 자막
    가독성을 직접 깨뜨려 사람 전용으로 남겼다 — `STYLE_SUBTITLE_KEYS` 한 줄로 연다.
    size 는 30~140 상한(v3 는 상한 없음 — 사람은 보고 정한다).
  - ⚠ **`video_speed` 는 안 연다.** 배속은 렌더 효과가 아니라 **길이 예산**이다:
    `_speed` 가 스토리 길이 클램프·확장 상한에 ×S 로 곱해지는데(pipeline 3389·3417~3427)
    style 은 그 클램프가 **끝난 뒤** 돈다. 여기서 바꾸면 40~60초 정책이 적용된 편의
    출력 길이만 조용히 달라진다(기획서 §4 가 열려고 했던 것을 코드 실측으로 뒤집었다).
  - voice/speed 는 불변 라벨만(`tts.py` 의 `VOICE_PRESETS`·`SPEED_TO_RATE` 에서 직접
    가져온다). `elevenlabs:` 접두사는 계정 종속이라 AI 산출에 금지(E12 — 사람이 고른다).
  - 하드캡: 텍스트 8 · 스티커 4 · 자막 강조 10 · 제목 창 5. 넘으면 앞에서부터 자르고
    **반드시 기록**한다(조용한 절단 금지).
- **스티커**: AI 는 `assets/stickers/manifest.json` 의 **id 만** 고른다(엔진이 AI 에게
  파일 경로를 열지 않는 규율 — 편집실 이미지도 오케스트레이터가 run_dir 로 내려놓는다).
  고른 파일은 `<run_dir>/style_assets/` 로 복사돼 v3 images 계약을 그대로 탄다.
  **목록이 비어 있는 것이 정상 상태다** — 권리사 콘텐츠 위에 얹는 자산이라 상용 가능
  라이선스가 확인된 파일만 번들한다. 없는 id 는 그 항목만 드롭 + 로그.
- **실패는 크게 남기고 본편은 막지 않는다**: 계약 위반·호출 실패면 `[style]` 로 사유를
  stdout 에 남기고 **연출 없이 진행**한다(연출은 부가물이라 발행을 막지 않는다).
  드롭된 앵커는 tts 고아 규칙과 같은 형식으로 건별 기록.
- **재개**: `checkpoint_style.json` 에 검증까지 끝난 플랜을 저장하고, 편집실 재렌더
  (`--from-step resources|render`)는 **재호출 없이 그대로 재적용**한다 — 재렌더마다
  다른 연출이 나오면 사람이 승인한 화면이 달라진다. `--from-step style` 은 재구성.
  자막 강조는 `subtitle_segments.json` 에 v3 와 **같은 style 키**로 저장돼 재개에서도
  유지된다(강조가 없으면 파일은 종전과 바이트까지 같다).
- **첫 variant 한정**(다른 오버라이드와 같은 규약): variant #2·#3 의 `RenderInputs` 는
  `image_overlays`·`title_segments`·`text_subtitle_path` 를 애초에 받지 않는다(기존 구멍).
  variant 확대는 그 렌더 경로를 함께 넓혀야 하는 별건.
- **JP(현지화) 채널은 켜지 않는다** — 연출 텍스트가 한국어로 번인돼 vlp 가 못 지운다
  (기획서 §9-1). 공유 함수 시그니처·`subtitle_segments.json`/`edit_plan.json` 모양은
  이번 변경에서 넓히기만 하고 바꾸지 않았다(vlp 세션 동시 작업 회피).
- 회귀 가드: `tests/test_e15_style_compose.py`.
