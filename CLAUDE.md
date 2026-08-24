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
- ~~**JP(현지화) 채널은 켜지 않는다**~~ — 기획 시점(8/23)에는 연출 텍스트가 한국어로
  번인돼 현지화가 못 지운다고 봤다. **E16(8/24)이 그 구멍을 메웠다** — 아래 §E16 참조.
  공유 함수 시그니처·`subtitle_segments.json`/`edit_plan.json` 모양은 E15 에서 넓히기만
  하고 바꾸지 않았다(vlp 세션 동시 작업 회피).
- 회귀 가드: `tests/test_e15_style_compose.py`.

### 실렌더 실측 (2026-08-23, 합성 180s 소스 · ffmpeg 6.1.1)

F-408/410 과 같은 방식(합성 소스 + 손으로 적은 플랜 — 측정 대상은 '플랜 → 배치 →
렌더'이지 LLM 문장력이 아니다). 편집 타임라인 원본 100~120s + 150~165s → 출력 35s,
플랜은 효과 텍스트 2 · 스티커 1(회전 15°) · 자막 강조 1 · 제목 창 2 · title_rotate −3°.

- **앵커 변환**: 원본 105s → 편집 5.0s, 107s → 7.0s(스티커 창 7.000~9.000),
  155s → 25.0s(2번째 클립 base 20 + 5). 제목 창 원본 100~112 → `between(t,0,12)`,
  150~160 → `between(t,20,30)`. 전부 수계산과 일치, 드롭 0.
- **회전 중심 보존**(F-410 규약): 스티커 w=0.22 → 238×178, 15° 바운딩 박스 276×234,
  `overlay=575:548` — 회전 전 중심 (713,665) 그대로. 수계산과 픽셀 단위 일치.
- **자막 강조**: `{\fs88\1c&H4444FF&}` (size 88 · #FF4444 의 BGR). 강조 없는 줄은
  태그 자체가 없어 off 판과 같은 줄이다.
- **효과 텍스트**: `\an5\pos(778,461)\fs110\frz8` + pop(`\fscx30→110→100`),
  둘째는 shake(`\frz6→-6→3→0`). **rotate −8 계약 → ASS `\frz8`** — 부호 반전이
  엔진 책임이라는 F-410 규약대로 나갔다.
- **시간대별 제목**: 두 창 **사이**(t=13s) 프레임의 제목 영역 글자 픽셀이 **0**
  (off 판은 31,682) — '창 밖 = 제목 없음'이 실제로 화면에서 확인됐다.
- **스티커 알파**: 회전 박스 안 빨강 픽셀 13,339(off 11) — png 반투명이 회전 뒤에도
  배경과 블렌드된다.
- **회귀 0 (핵심)**: pre-E15 코드(7f7c052)와 이 판의 **플래그 미지정** 실행을 같은
  입력으로 렌더 → 필터그래프 문자열 동일(경로 정규화 후) · `subtitles.ass` sha256
  동일 · 디코드 프레임 3지점 픽셀 **완전 동일**(bbox None).
- **렌더 시간**: 3회 중앙값 off 11.87s · on 12.60s → **+0.73s(+6.2%)**. F-408 이
  기준으로 삼은 멈춤 임계 30초 대비 여유.
- ⚠ 이 컨테이너 조건: ffmpeg **6.1.1**(운영 노드는 7.x — 둘 다 `SUPPORTED_FFMPEG_MAJORS`),
  `GEMINI_API_KEY` 없음 → **LLM 호출 경로(compose_style)는 실측 밖**이다. 프롬프트
  `.format()` 은 단위 테스트가 가짜 클라이언트로 덮는다. 실물 1편 on/off A/B(기획서 §12)는
  키·소스가 있는 노드에서 별도로 해야 한다.

## 현지화 계층 계약 (L-P1, 2026-08-23)

`app/localize/` — 발주서: ves-orchestrator `docs/LOCALIZE_UNIFY.md`.
video-localization-project `scripts/localize_run.py`(917줄)를 **충실히 이식**한 것이다.
회귀 0 이 조건이라 프롬프트·임계값·파일 이름·순서를 바꾸지 않았다.

- 진입점은 `python -m app.cli localize --job-dir <job> [--locale ja]` 하나.
  **생성 경로(`create_shorts`)와 완전히 분리된 서브커맨드**라 이 명령을 안 쓰는 실행은
  종전과 한 바이트도 같다(E11·E13 과 같은 게이트 규약).
- 단계: L0 백업 → L2 텔롭 추출 → L2b 타이밍 재보정 → L1 통번역 → L3 적용 →
  L3t 내레이션 재합성 → L4 재렌더+번인 → L5 메타. 모듈이 단계와 1:1 이다.
- **성공 마커·산출은 vlp 규약 그대로** — `<job>/localize_<locale>/metadata.json`,
  `<job>/shorts.mp4` 교체본, 원본은 `localize_backup_ko/`·`shorts_ko.mp4`.
  오케스트레이터 어댑터가 이 이름들을 보므로 바꾸면 컷오버가 깨진다.
- **번역 입력은 언제나 KO 백업본**이다(원본이 아니라) — 재실행해도 이중 번역이 없다.
- **컷 재현이 L4 의 전부다.** 원 생성과 같은 A/B 노브(`run_log.provenance.config.app`)로
  돌지 않으면 컷이 달라져 자막 싱크가 통째로 깨진다(실증: 49.7s→53.3s). 렌더 뒤
  `shorts_ko.mp4` 와 길이를 대조해 0.05초라도 어긋나면 실패시킨다 — **비디오 스트림끼리**
  잰다(아래 §컷 검증 기준 참조).
- **텔롭 인덱스 규약**: `broadcast_telop` 만 추린 목록의 순서가 L1·L2b·L3·검수
  오버라이드의 공통 좌표다(`telop.only_broadcast_telops`).
- **환각 클램프는 함수 하나다**(`apply.clamp_hallucination`) — 렌더(L3)와 검수 화면이
  보는 값(L5 `ko_ja_pairs`)이 같아야 한다. 원본은 같은 수식을 두 곳에 적어 뒀는데,
  베낀 수식은 언젠가 어긋난다(E13 교훈).
- 줄 스타일·타이밍 오버라이드(`styles.py`)는 **편집실과 합의된 계약**이라 동작을 바꾸면
  안 된다. 위반은 조용한 무시가 아니라 `ValueError` — 사람 값이 소리 없이 증발하면
  '고쳤는데 왜 그대로지'가 된다.
- ⚠ **이식하며 의도적으로 바꾼 것 둘**:
  ① **Flash 모델** — vlp 는 `gemini-3-flash-preview` 를 박아 썼지만 이 레포의 모델
     규칙이 금지한다. `GEMINI_FLASH_MODEL_NAME`(기본 `gemini-3.6-flash`)을 따른다.
     Pro 는 양쪽이 같다. Flash 가 쓰이는 곳은 L2b 프레임 판독·제목 축약뿐이고 둘 다
     LLM 판단이라 회귀 0 측정 대상이 아니다(발주서 §8-2).
  ② **진행 상태 파일** — vlp 는 자기 레포 `results/` 에 썼다. 엔진 레포에 런타임 상태를
     쓰지 않으므로 job 디렉토리 안(`localize_<locale>/state.json`)으로 옮겼다.
     읽는 곳이 없어 산출에 영향이 없다.
- 회귀 가드: `tests/test_localize_rerender.py`·`tests/test_localize_pairs.py`(40건) +
  `scripts/localize_port_diff.py`(원본과 **바이트 동일성** 대조 — vlp 동결 시 함께 은퇴).

### 편집실 재렌더 계약 (L-P2b, 2026-08-24)

vlp `1da2a16`·`2f338e3`(147줄)을 따라 이식했다. 이것이 없으면 **사람이 편집실에서 고친
한국어가 일본어판에 한 글자도 반영되지 않는다**(2026-08-23 SHOTCONE 실측 — 새 검수 카드의
`ko_ja_pairs` 가 직전 카드와 바이트 단위로 동일했다).

- **`--rebuild`** (`collect.l0_backup(rebuild=)` · `collect.invalidate_localize_cache`) —
  L0 의 멱등은 이중 번역을 막는 장치인데 편집실 재렌더에서는 **반대로 해롭다**. rebuild 면
  백업을 지금 job 상태로 갱신하고 `shorts_ko.mp4` 를 새 렌더로 갈아끼운다(L2 텔롭 추출·L4
  길이 대조의 기준이다). 캐시는 `REBUILD_STALE` + `refine_frames/` 만 폐기한다 —
  ⚠ **디렉토리째 지우면 안 된다**: L3t 가 같은 곳에 보존한 한국어 mp3 원본이 날아간다.
- **디자인 복원** (`rerender.design_restore_flags` · `read_design_cli_file`) — 종전엔 폰트
  둘만 넘겨 채널·편집실이 정한 화면비·영상 위치·제목 스타일이 전부 엔진 기본값으로
  떨어졌다(SHOTCONE: `aspect_ratio` 13:9 → 완성본 1:1). 출처는 둘이고 **정본은
  `run_log.design_cli`**, 없으면 오케스트레이터가 남긴 `design_cli.json` — 두 배포가 서로를
  기다리지 않게 한 쪽만 있어도 성립한다. 현지화 폰트는 **맨 뒤**에 얹는다(argparse 는 뒤가
  이긴다 — 앞에 두면 원 런의 폰트가 이겨 일본어가 깨진다). 둘 다 없는 옛 런은 폰트만(회귀 0).
- **편집실 겹치기 승계** (`rerender.visual_only_overrides`) — `images`·`texts` **둘만**
  넘긴다. 이 둘은 `edit_overrides.json` 에만 있어(체크포인트에 안 남는다) 안 넘기면 사람이
  올린 이미지·문구가 일본어판에서 조용히 사라진다. 반대로 제목·자막·구간·내레이션까지
  넘기면 L3 가 써 놓은 일본어 위에 **사람이 고친 한국어가 그대로 다시 덮인다.**
- `--rebuild` 미지정 = 종전 그대로다. 첫 현지화(planner 정상 체인)는 이 키를 안 쓴다 —
  무효화할 캐시도 없다. 회귀 가드: `tests/test_localize_rebuild.py`(23건).
- 기계 대조: 새로 옮긴 함수 5개 중 4개가 vlp 와 **AST 동일**, 상수 2개도 동일.
  `l0_backup` 만 다른데 P1 에서 하드코딩 대신 계약 상수(`SOURCE_VIDEO_BACKUP`·
  `RENDER_OUTPUT`)를 쓰기로 한 차이이고 값은 같다.

### 내레이션 창 계약 (L3t, 2026-08-24)

`app/localize/narration.py` — `_trim_to_window` · `trim_args`.

**창을 넘긴 일본어 내레이션이 편 전체를 죽이고 있었다.** SHOTCONE 혜미리예채파 2화가
3회 연속 **같은 숫자**로 dead 였다: `컷 길이 불일치: ko 39.400s vs ja 39.900s`.

- 기전(실측 확인): 렌더는 `amix=inputs=N:duration=longest` 로 섞는데 ffmpeg 에
  **`-shortest` 가 없다**(`renderer._build_audio_filter` · `render_video` 의 argv).
  그래서 마지막 cue 오디오가 창을 넘으면 **컨테이너가 영상 트랙보다 길어진다**
  (합성 2.0s 영상 + 2.5s cue → 출력 2.5s). 그 초과분을 L4 컷 대조(허용 0.05초)가
  잡아 편을 통째로 실패시킨다.
- **비대칭이 정체였다** — 한국어는 `synthesize_cue_cached` 가 fit 재작성(Flash 단축)으로
  창을 지키는데, 일본어 재합성만 rate 3단계(+0/+15/+30%)를 다 쓰고도 안 맞으면
  **경고만 찍고 넘어갔다**. 이제 창 길이로 자른다(끝에 페이드 — 창의 10%·0.15초 중 짧은 쪽).
- **자르는 쪽을 골랐다**: 내레이션 끝 일부를 잃지만 편 전체가 발행되지 못하는 것보다 낫다.
  대신 **건별로 크게 남긴다**(stdout `[L3t] ⚠️ … 창 길이로 잘랐다` + cue `fit_trimmed:true`)
  — 검수에서 짚을 근거다. 창에 드는 cue 는 **파일을 아예 안 건드린다**(회귀 0).
- 잘라내기가 실패하면 원본을 유지하고 죽지 않는다(내레이션을 잃는 것이 더 나쁘다).
- ⚠ **렌더에 `-shortest` 를 넣는 것은 고려했다가 버렸다** — 전 채널 KR 렌더까지
  오디오 꼬리를 자르게 되고 `auto_update=true` 라 맥미니 6대에 즉시 나간다. 원인은
  현지화 재합성 쪽이므로 그쪽에서 막는다.
- ⚠ **vlp 원본에도 같은 버그가 있다**(`localize_run.l3t_tts` — 경고만 찍는 그 코드).
  이식본이 의도적으로 갈라진 지점이라 `localize_port_diff` 대조 대상이 아니다
  (그 스크립트는 render_flags·ass·pairs·l3_apply 만 본다).
- 회귀 가드: `tests/test_localize_narration_window.py`(14건 — 실제 ffmpeg 으로 자름 확인).

### L4 컷 검증 기준 (2026-08-24, 사용자 결정)

`rerender.cut_reproduced` — **비디오 스트림끼리** 비교한다(컨테이너가 아니라).

검사의 의도는 '컷(클립 경계)이 재현됐는가'인데 **컨테이너 길이는 오디오가 좌우한다** —
렌더가 `amix=duration=longest` 를 `-shortest` 없이 섞으므로 오디오가 영상보다 길면
컨테이너만 늘어난다. 컨테이너를 재면 **컷과 무관한 오디오 꼬리 차이로 편을 죽인다.**

실측(SHOTCONE 혜미리예채파 2화, 5회 연속 dead 를 만든 그 숫자):

```
비디오 스트림  ko 25.025s · ja 25.025s   ← 컷은 완벽히 재현됨
컨테이너      ko 39.400s · ja 39.900s   ← 0.5초 차이로 실패 판정
```

- **KO 완성본에도 이미 14.4초 오디오 꼬리가 있었다** — 영상은 25초에서 끝나는데 소리만
  39.4초까지 간다. KR 은 이 검사가 없어 **아무도 몰랐다.**
- 그 마당에 0.5초를 이유로 발행을 막는 것은 앞뒤가 맞지 않는다 → 통과시키되 **stdout 에
  '⚠️ 오디오 꼬리' 로 남겨 보이게** 한다(조용히 넘기지 않는다).

**꼬리의 범위 실측 (2026-08-24, 사용자 요청)** — '꼬리가 KR 전 채널의 상시 결함인가'를
DB 로 재봤다. 성공한 localize 잡 16건의 `result.stdout_tail`·`markers` 에 L4 가 남긴
**ja 컨테이너 길이**가 있고(ko 와 0.05초 이내로 일치함을 L4 가 그 자리에서 확인한다),
`clips.duration_sec` 이 **기획 합계**(= 클립 소실이 없으면 비디오 스트림 길이)다. 둘의 차가 꼬리다:

```
클립 소실 0건 (7편 검증 + 2편 소스 길이 미상)   꼬리 0.068 ~ 0.360s   ← 컨테이너 패딩 수준
클립 소실 26.0s (혜미리예채파 2화, 그 사고)      꼬리 14.375s
```

⇒ **14초짜리 꼬리는 상시 결함이 아니라 소스 밖 클립의 증상이다.** 기획 total 이 렌더되지
않는 클립을 포함해 부풀고, cue 가 그 부푼 total 안에 정상 배치돼 영상 밖에 남는다(아래
§영상 밖 cue 안전망). 상류 차단(`timestamp_check.bounds_problem`)이 원인을 없앤다.
⚠ 표본은 10편·한 채널(SHOTCONE)이고 전부 현지화를 탄 편이다 — TTS fit 초과로 생기는
**0.x초급** 꼬리까지 없다고는 말할 수 없다. 배제된 것은 '초 단위 꼬리가 상시로 있다'는 쪽이다.
- ⚠ 스트림 길이를 못 읽으면 검사를 **건너뛰지 않고 컨테이너로 폴백**한다 — 가드가
  조용히 사라지는 것이 오판보다 나쁘다.
- ⚠ **단위 착오 이력**: 이 함수의 첫 판(`cut_mismatch_hint`)은 ko 의 **컨테이너** 와
  ja 의 **스트림** 을 맞댔다. 두 파일 다 꼬리가 있으면 컷이 멀쩡해도 늘 '재현 실패'로
  읽힌다 — 실제로 그 오진이 조사를 엉뚱한 곳으로 보냈다. 회귀 가드가 이 입력을 고정한다.

### 소스 밖 클립 검증 (2026-08-24, 사용자 결정: 크게 실패시킨다)

`pipeline.clips_beyond_source` — 클램프·dedup 이 끝나 클립이 확정된 지점(`[clip-bounds]`
블록)에서 본다. TTS 합성·렌더 같은 비싼 단계 **전**이다.

**반쪽짜리 쇼츠가 조용히 발행되고 있었다.** 렌더는 클립마다 `-ss start -to end -i <소스>`
로 읽는데, 구간이 소스 끝을 넘으면 그만큼 짧아지고 **시작 자체가 소스 밖이면 프레임도
오디오도 0개**다. concat 은 남은 클립만 이어 붙이므로 경고 한 줄 없이 절반이 사라진다.

**실측(185개 런 중 5건 · 4개 채널 · 전부 클립 2개짜리)** — 하나가 통째로 증발:

```
놀라운 토요일 (08-20)   33.5s 기획 → 15.5s   (-18.0s)
샤먼: 미신전  (08-22)   49.0s 기획 → 28.0s   (-21.0s)
언니네 산지직송(08-22)   49.0s 기획 → 22.4s   (-26.6s)
놀라운 토요일 (08-24)   47.5s 기획 → 29.5s   (-18.0s)
혜미리예채파 2 (08-24)   51.0s 기획 → 25.0s   (-26.0s)  ← 소스 190s, payoff 200~226s
```

혜미리예채파 2화는 내레이션이 "결국 0캐시로 전부 날려버렸다"고 말하는데 **그 장면이
화면에 없다.** 5건 모두 YouTube 발행까지는 가지 않았다(`publish` 잡 0건).

- **원인은 Gemini 가 소스 길이 밖 타임스탬프를 만드는 것**이고, 파이프라인 어디에도
  클립 경계를 소스 길이와 대조하는 코드가 없었다(`total_duration` 은 단순 합산이다).
- **조용한 절반 발행이 실패보다 나쁘다** → `ValueError` 로 멈춘다. 클램프(200~226 을
  176~190 으로 당기기)는 **하지 않는다** — 내용이 사람이 의도한 장면과 달라진다.
- 임계 `CLIP_LOST_TOLERANCE_SEC = 1.0` — 끝 경계가 소스 길이와 소수점에서 어긋나는
  정상 케이스(실측 0~0.2s)는 통과시키고 통째로/대부분 증발하는 것만 잡는다.
- **실제로 렌더될 variant 만 본다**(`[:max_shorts]`) — 안 쓰는 variant 때문에 편을
  죽이지 않는다. 소스 길이를 못 읽으면(0) 판정하지 않는다(오판 금지).
- 걸린 클립은 **전부** 메시지에 적는다 — 하나만 알려주면 재시도를 반복하게 된다.
- ⚠ 이 가드가 켜지면 **이미 만들어진 결함 job 의 재렌더(JP 현지화 포함)도 실패한다.**
  그 job 은 실제로 반쪽이므로 의도된 동작이다.
- 회귀 가드: `tests/test_clip_bounds_guard.py`(9건).

**상류 차단 (`timestamp_check.bounds_problem`, 같은 날 후속)** — 위 가드는 *잡아서 멈추는*
것이라, 걸리면 그 편은 실패한다. 그래서 **후보 단계에서 먼저 뺀다** — 스토리가 고르기
전에 빼야 다른 성한 후보로 대신 만든다.

- 자리는 `timestamp_check.filter_candidates` 다. 이 모듈 맨 위 독스트링이 이미 같은
  현상을 기록하고 있었다 — "같은 응답의 다른 후보는 933~997 을 주장했는데 소스는
  875초까지뿐이었다". 그런데 종전 판정은 **인용 대사가 전사의 다른 곳에서 발견될 때만**
  걸러서, 대사 없는 장면이면 범위 밖이어도 그대로 통과했다.
- **이 수정이 5건을 전부 막는다**: `pipeline.py` 의 스토리 조립부가
  "LLM이 어떻게 출력했든 candidate 정본 시간으로 복원"(`_resolve_clip_times`)하므로,
  소스 밖 클립은 **소스 밖 후보에서 온 것**이다. 후보를 빼면 그 클립이 생기지 않는다.
- 조치는 둘: 시작이 소스 끝을 넘거나 남는 분량이 `MIN_USABLE_SEC`(1초) 미만이면
  **드롭**, 시작은 안이고 끝만 넘치면 **끝을 소스 길이로 클램프**(내용을 더하지 않으므로
  안전하고 쓸 수 있는 소재를 버리지 않는다). 둘 다 사유를 노트로 남긴다.
- **경계 검사는 전사 유무와 무관**하므로 `if not segments` 조기 반환보다 **앞**에서
  돈다 — 전사가 비었다고 건너뛰면 대사 없는 편이 그대로 반쪽이 된다.
- `source_duration_sec` 미지정이면 종전과 완전히 같다(회귀 0). 순수 — 넘겨받은 후보
  dict 를 건드리지 않고 클램프는 사본으로 한다.
- 회귀 가드: `tests/test_timestamp_check.py` 의 소스 밖 후보 절(8건).

### 영상 밖 cue 안전망 (렌더, 2026-08-24)

`renderer.cues_within_video` · `video_out_duration` — `render_video` 가 입력을 만들기
**직전에 한 번** 거른다.

⚠ **여기는 KR 전 채널이 지나는 렌더 경로다**(현지화 전용이 아니다).

- **증상**: 영상이 끝난 뒤 시작하는 TTS cue 가 `amix=duration=longest`(+`-shortest` 없음)
  때문에 **출력 컨테이너를 그만큼 늘린다.** 실측(SHOTCONE 혜미리예채파 2화): 비디오
  25.025s · 컨테이너 39.400s · cue 창 37.0~40.5s. **25초 이후 비디오 패킷이 아예 없다**
  (마지막 비디오 패킷 24.93s / 오디오 38.99s) — 정지 화면 위로 내레이션만 14.4초 흐른다.
- **상류 규명 완료(2026-08-24) — 클램프를 우회한 것이 아니다.** `_resolve_cue_anchors` 는
  `end = min(start+duration, total)` 로 cue 를 가두는데, 그 `total` 은 **기획 클립 합계**다.
  DB 실측(`checkpoint_story`·`edit_plan`): 최종 클립 `[31.5,56.5]`·`[200.0,226.0]` →
  total 51.0s. cue 앵커 `source_time_sec=212.0` 은 두 번째 조각(base 25.0) 안이라
  start = 25.0+12.0 = **37.0**, end = min(40.5, 51.0) = 40.5 — 관측값과 정확히 같다.
  **클램프는 정상 동작했고, 기준 total 이 렌더되지 않는 클립까지 세고 있었을 뿐이다**
  (실제 렌더는 25.025s). 즉 원인은 소스 밖 클립 하나이고 상류에서 차단됐다.
  이 안전망은 그와 무관하게 증상을 끊는다.
- **영상 밖에서 시작하는 것만 버린다.** 영상 안에서 시작해 끝만 넘치는 cue 는 화면
  위에서 들리기 시작하므로 사람이 의도한 소리다 — 안 건드린다(별건).
- 클립 정보가 없으면(길이 0) 아무것도 안 버린다. 시간이 깨진 cue 도 판정을 포기하고
  싣는다 — **가드가 오작동해 멀쩡한 내레이션을 지우는 것이 꼬리보다 나쁘다.**
- 버린 cue 는 건별로 stdout `[cue-late]` 에 남긴다(조용한 드롭 금지).
- ⚠ **거르는 지점은 `render_video` 한 곳이다** — `_build_input_args` 와
  `_build_audio_filter` 가 **같은 목록**을 봐야 cue 입력 인덱스(`num_clip_inputs + ci`)가
  어긋나지 않는다. 필터 안에서만 거르면 엉뚱한 mp3 가 섞인다.
- 판정은 **배속과 무관하다** — cue 시작과 영상 길이가 함께 ÷S 되어 상쇄된다. 그래도
  필터가 나누는 것과 같은 좌표계로 재도록 나눗셈은 남겼다(테스트가 이 사실을 못박는다).
- **범위 실측(2026-08-24)**: `localization_qa` 검수 카드 28건 중 cue 가 영상 밖으로 나간
  편은 **이번 1건뿐**(나머지는 cue 시작이 마지막 자막보다 15~58초 앞).
  ~~`style_compose` 를 켠 유일한 편이라 N=1 로는 구분할 수 없다~~ — **`style_compose` 와
  무관하다**(위 상류 규명). 조건은 '클립이 소스 밖으로 나갔는가' 하나다. 소실 5건 중
  `style` 단계를 탄 편은 **1건뿐이고 나머지 4건은 켜지 않았다**(run_log steps 실측).
- 회귀 가드: `tests/test_render_late_cue_guard.py`(12건 — 마지막 1건이 위 상류 수식을 못박는다).

### 화면 글자 현지화 계약 (E16, 2026-08-24)

`app/localize/style_texts.py` — vlp `0757b68` 이식. 발주서: ves-orchestrator
`docs/prompts/e16-jp-style-texts.md`.

JP 재렌더는 화면에 얹는 **한국어 글자**를 그대로 번인했다. 소스는 둘이다 —
E15 AI 연출(`checkpoint_style.json` 의 `texts[]`·`title_segments[]`)과 편집실 텍스트
(`edit_overrides.json` 의 `texts[]` — `visual_only_overrides` 가 넘기는 그 배열).
E16 이 둘 다 일본어로 바꾼다.

- **손대는 것은 문구와 폰트뿐이다.** 좌표·크기·색·`fx`·`rotate` 는 연출 의도라 불변이고,
  스티커(`images`)·자막 강조(`subtitle_styles`)는 언어 중립이라 아예 안 본다(자막 강조는
  L3 가 이미 일본어로 바꾼 줄에 얹히므로 지금도 정상 동작한다).
- **폰트**: 번들 4종은 전부 한글 전용이다 — `mulmaru` 만 가나가 있고 **한자는 넷 다 없다**
  (fontTools 실측). 그대로 두면 일본어가 두부(□)로 나간다. `locale_cfg.telop_font`
  (ArialUnicode)로 바꾼다. 편집실 texts 는 렌더에서 화이트리스트를 타므로
  `edit_overrides.TEXT_FONTS` 에 같은 이름이 있어야 한다(짝 변경 `c80da45`).
- **멱등의 핵심은 `BACKUP_FILES` 의 `checkpoint_style.json`** 이다. L3 는 언제나 한국어
  백업을 읽어 일본어를 만드는데, 백업에 없으면 두 번째 L3 가 **이미 일본어인 문구를 다시
  번역**한다.
- **1:1 정렬** — `index` 가 좌표다. 개수·범위가 어긋나면 즉시 실패(`ja_by_index`).
  조용히 넘어가면 다른 문구가 다른 자리에 박힌다(자막 정렬과 같은 규율).
- **회귀 0**: 연출이 없는 편은 세 목록을 payload 에 **아예 안 싣는다** — 프롬프트가 한
  글자만 달라져도 그 편의 자막 번역까지 흔들린다. `style_compose` 를 안 켠 채널은
  `checkpoint_style.json` 이 없고 `l0_backup` 이 없는 파일을 건너뛰므로 경로 전체가 no-op.
- 검수 카드는 `ko_ja_pairs.style_texts` 로 대역을 본다(`쿵!` 이 무엇이 됐는지).
- ⚠ 이식본은 함수가 L1·L3·L4·L5 에 걸쳐 쓰이므로 vlp 의 비공개 이름
  (`_load_json_or_none`·`_ja_by_index`)이 **공개 이름**이다. 그 외에는 vlp 와 같은 코드다.
- 회귀 가드: `tests/test_localize_style_texts.py`(19건) + `scripts/localize_port_diff.py`
  의 `[style_texts]` 절(원본과 산출 동일성 대조).

## AI 연출 배치 교정 (E18, 2026-08-24)

`style_compose.py` · `gemini_client.STYLE_COMPOSITION_PROMPT` · `localize/apply.py` ·
`localize/telop.py`. 완성본 프레임 4장을 보고 사용자가 짚은 4건 + 조사 중 나온 1건.

⚠ **SHOTCONE 얘기가 아니다.** `channels_mirror` 실측 — 21개 채널 중 20개가
`style_compose: true` 이고, 키가 없는 HANIPJUMAK 도 전역 게이트(`ops_config.channel_style
= on`)로 style 단계를 탄다(가왕쇼 런이 그 채널이다). 아래 1·2는 **전 채널 문제**다.
다행히 연출이 실제로 들어간 4편은 전부 미발행이고, 발행된 3편은 AI 플랜이 비어 있었다
(`texts 0 · title_segments 0`) — 결함이 나간 적은 없다.

### E18-1 제목은 무조건 있어야 한다 (`fill_title_gaps`)

E8 렌더 계약은 '창 밖 = 제목 없음'이고 그건 **사람이 쓰는 기능**이다(편집실에서 중반부터
제목을 걷어내는 연출). AI 는 그걸 의도가 아니라 부주의로 만든다 — 제목 창이 들어간 3편 실측:

```
김부장_e37253c2    (IGEOBOGOJA)  창 2개 → 55.31s / 55.31s   제목 없음 0s
가왕쇼_b5ec784a    (HANIPJUMAK)  창 1개 → 17.21s / 53.17s   제목 없음 36.0s (68%)
혜미리예채파_2b2b46c6 (SHOTCONE)  창 1개 → 18.50s / 51.00s   제목 없음 32.5s
```

가왕쇼는 AI 가 창을 2개 냈는데 `edit_plan` 에는 1개만 남았다 — 겹침 dedup 이 하나를 버려
구멍이 더 커졌다. 그래서 **두 경로를 한 번에** 막는다: `title_segments_from_anchors` 가
`base_title` 을 받으면 창 끝을 영상 길이로 클램프하고, 남은 빈 시간을 **기본 제목**으로 메운다.

- 틈이 `MIN_TITLE_GAP_SEC`(0.4초) 미만이면 메우지 않고 **앞 창을 늘려** 잇는다 — 0.2초짜리
  기본 제목이 깜빡이면 구멍보다 나쁘다. 어느 쪽이든 **빈 시간은 남지 않는다**.
- 창이 전부 드롭되면 빈 목록을 돌려준다 → 렌더러가 종전대로 제목을 편 내내 그린다.
- ⚠ **사람 경로(`edit_overrides.title.segments`)는 안 건드린다** — `base_title` 을 안 주면
  종전과 완전히 같다. 사람이 비운 것은 의도다.
- 프롬프트도 같이 고쳤다("중반부터 제목을 걷어내 화면을 비운다"가 이 기능의 절반이라고
  **권하고 있었다**). 검증기만 고치면 LLM 은 계속 같은 플랜을 낸다.

### E18-2 효과 텍스트는 영상 밴드 안에만 (`text_y_range` · `clamp_texts_to_band`)

프롬프트가 `y 는 0.15~0.35(위) 또는 0.60~0.72(아래)` 를 **하드코딩**해 권했다. 13:9·꽉 찬
폭·세로 중앙(SHOTCONE)으로 재면 제목 블록은 **y 0.146~0.295**(`dynamic_title_y =
overlay_y − 블록높이 − 20`) — 권장 '위' 구간과 거의 완전히 겹친다. **프롬프트가 제목
자리를 찍어서 권하고 있었다**(실측: 효과 텍스트 `멘붕?!`·`텅-빈` 2건이 제목 두 줄 위에 얹혔다).

- 허용 구간을 **그 편의 실제 기하에서 계산**한다(`subtitle_region.band_geometry` 재사용 —
  수식 복제 금지). 밴드 **위**는 제목 자리, **아래**는 자막·작품명 자리라 밴드로 가두면
  두 충돌이 한꺼번에 사라진다. 밴드가 자막 자리까지 내려오는 채널(1:1·4:5)은
  `SUBTITLE_RESERVE_PX`(520)로 아래를 잘라 준다.
  실측: 13:9 → y 0.318~0.681 · 1:1 → 0.231~0.729 · 16:9 → 0.354~0.645.
  `band_top + 24 > band_top − 20 = 제목 아래끝` 이므로 **화면비와 무관하게** 제목을 피한다.
- 범위 밖은 **드롭이 아니라 클램프**(사용자 결정) — 연출은 살리고 위치만 당긴다. 건별 기록.
  y 는 v3 계약상 **글자 중심**(`\an5\pos`)이라 글자 반높이(size×0.6)를 여유로 뺀다.
- 클램프는 **배치 직전**에 건다 — 체크포인트에서 되살아난 옛 플랜도 같은 길을 지난다.

### E18-3 AI 는 제목을 기울일 수 없다

이 키는 지시가 세 번 바뀌었다: ① 통째로 차단 → ② ±15° 로 완화(E17-1) → ③ **다시 차단**
("제목은 회전하지 않도록 되는지 확인해서 ai가 회전을 못하게 해야돼"). ②로 열어 둔 범위로도
매 편 기울어져 나와서 범위가 아니라 **키를 닫았다**.

- 닫는 방식은 '모르는 키'가 아니라 **드롭+메모**다(`STYLE_DESIGN_IGNORED`). ALLOWED 에서
  빼기만 하면 unknown 검사가 플랜 **전체**를 거절하고, LLM 은 이 키를 계속 낼 테니 그때마다
  효과 텍스트·제목 창까지 통째로 날아간다.
- `design_overrides` 에서 **한 번 더** 막는다 — E17-1 시절(±15°) 체크포인트는 재검증 없이
  재적용된다(E15 재개 계약). 그 경로로 되살아나면 지시가 무력해진다.
- ⚠ 사람·채널의 `--design-title-rotate` 는 그대로다(-180~180). 닫는 것은 **AI 산출**뿐이다.
- `AI_TITLE_ROTATE_RANGE_DEG` 는 사라졌다(프롬프트의 `title_rotate_max` 자리도 함께).

### E18-4 JP 텔롭이 원본 한국어 텔롭을 피한다 (`telop_margin_v`)

`build_telop_ass` 는 **모든** 텔롭을 `MarginV 720` 고정으로 그렸다. 주석대로 우리 대사(430)·
TTS(580)는 피하지만 **원본에 박힌 한국어 텔롭은 고려하지 않는다** — y = 1920−720 = 1200 은
13:9 밴드(586~1332)의 82% 지점이라 하단 방송 텔롭과 정면으로 부딪친다.

L2 는 텔롭마다 `position`(top/middle/bottom)을 **이미 뽑고 있는데** 이 함수가 안 썼다.
그 값으로 원본에서 먼 구역을 고른다(새 검출 비용 0):

- `bottom`·`middle` → `TELOP_MARGIN_V_HIGH`(1120). 글자 아래끝 800px, 두 줄 박스(≈150px)를
  세워도 위끝이 650px 근처라 제목 블록 아래끝(566px) 아래에 남는다.
- `top`·미상 → 이벤트 MarginV 0 = 스타일 기본(720). **종전 출력과 한 글자도 같다.**
- 사람 오버라이드(`style.y`)가 여전히 **최우선**이다.

**실렌더 실측**(ffmpeg 6.1.1, 1080×1920 합성 + 840px 폭 흉내 원본 텔롭 y 1140~1215):

```
position     MarginV   박스 행        원본 잔존
default/top     720   1143~1204   51,713 / 63,000   ← 겹침 재현
bottom/middle  1120    744~ 804   63,000 / 63,000   ← 겹침 0
```

⚠ **박스로 덮는 길은 없다 — 실측으로 닫힌 선택지다.** 사용자 요청("불투명 박스도 넣어줘")
으로 두 가지를 재 봤고 둘 다 기각됐다:

- BackColour 를 반투명(`&H78......`) → 불투명(`&H00......`)으로 바꿔도 **프레임이 픽셀까지
  동일**했다. BorderStyle=3 의 박스는 **OutlineColour**(`&H00000000` — 이미 불투명)로
  그려지고 BackColour 는 그림자 색인데 `Shadow=0` 이라 안 쓰인다. → **되돌렸다**(무효한
  변경을 남기면 있지도 않은 동작을 믿게 된다).
- 박스 여백(Outline)을 5 → 12 → 24 로 넓혀도 박스 폭은 202 → 216 → 242px, 원본은 840px
  (잔존 51.7k → 46.2k). **여백은 마스크가 아니다.**

⇒ 정말 덮으려면 전폭 마스크 도형을 따로 그려야 하는데, 그건 텔롭이 뜰 때마다 영상 한 줄을
검게 지우는 것이라 **사람이 결정할 일**이다. 지금은 자리 이동만 한다.

### E18-5 우리 연출 텍스트를 방송 텔롭으로 다시 뽑지 않는다

L2 `EXTRACT_PROMPT` 의 분류에 **E15 연출 텍스트 칸이 없어서** Gemini 가 우리 글자를
`broadcast_telop` 으로 분류했다. 실측: 효과 텍스트 `멘붕?!`(원본 42.0s)이 검수 카드 텔롭
목록에 `idx5 ko="멘붕?!"`(편집본 9.75~11.25s ≒ 원본 41.25~42.75s)로 다시 들어와 **JP 텔롭
트랙이 같은 말을 한 번 더 그렸다.** `our_style_text` 분류를 넣으면
`only_broadcast_telops` 가 자동으로 걸러 낸다(필터는 그대로).

⚠ 텔롭 인덱스 규약(L1·L2b·L3 공통 좌표)은 `broadcast_telop` 만 추린 순서라, 분류가 바뀌면
**인덱스도 바뀐다.** 한 job 안에서는 L2 결과가 캐시돼 일관되고, `--rebuild` 는 캐시를
무효화한다 — 옛 job 을 새 코드로 재개할 때만 주의하면 된다.

### E18-6 간헐 방송 텔롭도 피한다 — 구간별 재판정 (2026-08-24)

`subtitle_region.detect_burned_profiles`·`band_in_window`·`per_cue_margins` ·
`pipeline._avoid_burned_windowed`. E17-2 의 후속이고 **KR 전 채널 렌더 경로**다.

E17-2 는 *"표본 프레임의 **절반 이상**에서 걸리는 행"* 만 띠로 본다. 편 내내 같은 자리에
있는 번인 대사 자막에는 맞지만 **몇 초씩만 뜨는 방송 텔롭은 못 잡는다.** 증거: SHOTCONE
혜미리예채파 2화는 E17 포함 sha(`6effc4c`)로 돌았는데 run_log 에 `subtitle_avoid_burned`
단계가 **아예 없다**(= 미검출). 완성본에는 한국어 텔롭이 뚜렷하게 박혀 있었다.

- 표본을 **시각과 함께** 모아 두고(`[[편집본 절대초, 글자행 목록], …]`), 자막 줄이 떠 있는
  **그 창 안의 표본만으로** 다시 판정한다. 임계는 같은 비율(절반)이고 창이 짧으면 표본도
  적으므로 최소 표본 수(`WINDOW_MIN_FRAMES` 2)를 따로 둔다 — 1장이면 그 장면의 밝은
  무늬가 자막이 된다.
- **단조 개선이 규율이다.** 전역 판정으로 정한 margin 은 그대로 두고, 창별로 **더 올려야
  하는 줄만** 추가로 올린다. 창 판정이 더 낮은 자리를 가리켜도 **내리지 않는다** — 이미
  승인된 화면이 아래로 내려가면 안 되고, 전역 띠는 편 내내 있는 것이라 창에서 안 잡혀도
  여전히 거기 있다. 그래서 **어떤 줄도 오늘보다 덜 올라가지 않는다.**
- ⚠ 표본은 전역 판정과 **따로** 뜬다(`detect_burned_profiles`). 전역 쪽 표본 구성
  (4클립×6프레임)을 건드리면 이미 승인된 채널의 자막 위치가 움직인다 — E17-2 는 기본이
  켜짐이라 전 채널이 그 값을 쓰고 있다. 비용이 늘지만 회귀 0 이 먼저다.
- **적용은 줄 단위 스타일 `style["y"]`** 로 얹는다 — 편집실(v3)이 쓰는 것과 **같은 통로**라
  ASS 조립부를 새로 만들지 않는다. 사람이 y 를 정한 줄은 **건드리지 않는다**(사람이 이긴다).
  `final_segments` 는 안 바꾸고 **사본**만 만든다 — `subtitle_segments.json` 이 움직이면
  편집실이 보는 자료와 오케스트레이터 왕복이 달라진다.
- `build_tts_ass` 가 줄별 MarginV 를 받게 넓혔다(대사 자막은 `_line_style_overrides` 로
  이미 되고 있었다). style 이 없으면 **종전과 바이트 동일**이고, 기울인 TTS 자막의 회전
  원점(`\org`)도 옮겨진 자리를 따라간다.
- 표본은 `checkpoint_burned_subtitle.json` 의 `profiles` 키에 남아 `--from-step render`
  재개에서 **다시 디코드하지 않고** 창별 재판정이 된다. 클립 구성이 바뀌면 캐시 무효.
- ⚠ `_BURNED_PROFILES` 는 모듈 수준이라 한 프로세스에서 여러 편을 돌리면 앞 편 표본이
  남는다 — `_detect_burned_band_cached` 시작에서 비운다(회귀 가드가 이걸 못박는다).

**실렌더 실측 (2026-08-24, ffmpeg 6.1.1)** — 1080×1080 합성 20초, 원본 텔롭을 **6~10초에만**
띄웠다(편의 20% = 전역 임계 50% 미만):

```
① E17 전역 판정      → None (미검출)          ← 보고된 그 현상을 재현
② E18-2 구간 표본    40프레임 · 글자행 6.0~9.5초 8장
   cue  2.0~ 4.0  → 그대로
   cue  6.5~ 9.5  → margin_v 430 → 582 (+152px)   원본 띠 y=1352~1384
   cue 12.0~14.0  → 그대로
비용: 전역 0.78s + 구간 1.06s (20초·2클립 기준 중앙값 3회)
```

- 회귀 가드: `tests/test_e18_windowed_avoid.py`(17건).

### 남은 별건 (사용자와 합의)

없음 — E17-2 간헐 텔롭 건은 위 E18-6 으로 닫혔다.

회귀 가드: `tests/test_e18_style_placement.py`(15건) · `tests/test_e18_localize_telop.py`(11건)
· `tests/test_e18_windowed_avoid.py`(17건) · `tests/test_e15_style_compose.py` 의 제목 회전 절(4건).

## 원본 자막 회피 · 제목 회전 · 토큰 만료 폴백 (E17, 2026-08-24)

사용자 지시 3건을 한 커밋으로 묶은 것이다.

> "제목은 왠만하면 회전은 안되게 해주고, 영상에 원래 자막이 있으면 그 위치 피해서 자막이
> 들어가게 해줘. 물론, 자막이 제목과도 겹치면 안되고. 그리고 일레븐랩스 토큰이 만료되면
> 일레븐랩스 api 가 아니라 기본으로 사용되도록 해줘."

정정(같은 날): "제목을 돌리는 거는 가능한데, 안돌리게 제약 정도로만 걸어줘. 그리고 tts
자막도 마찬가지로 겹치지 않게 해야돼." → E17-1 은 완전 차단에서 **좁은 범위 제약**으로
바뀌었고, E17-2 회피는 대사 자막뿐 아니라 **TTS(내레이션) 자막에도** 적용됐다(아래 갱신).

### E17-1 제목 기울기는 AI 에게 좁은 범위만 열려 있다

`style_compose.AI_TITLE_ROTATE_RANGE_DEG` · `gemini_client.STYLE_COMPOSITION_PROMPT`.

- 처음엔 `title_rotate` 를 통째로 막았다("회전은 안 되게"). 사용자가 "돌리는 거는
  가능한데, 안 돌리게 **제약 정도로만**"으로 정정해 — **완전히 닫지 않고 AI 산출의
  범위만 좁힌다**(±15°, `AI_TITLE_ROTATE_RANGE_DEG`). `STYLE_DESIGN_ALLOWED` 에 다시
  넣었고, 범위 밖이면(±15° 초과) `tts_rotate` 와 같은 강도로 **플랜 전체를 거절**한다
  (조용히 버리지 않는다 — 범위를 벗어난 값은 LLM 이 계약을 오해했다는 신호라 다른
  항목도 의심해야 한다). 프롬프트도 "웬만하면 기울이지 마라, 필요할 때만 ±15° 이내"로
  맞춰 뒀다 — 검증기만 좁히면 매 편 거절당하는 값을 계속 낸다.
- **사람·채널의 `--design-title-rotate` 는 이 상한과 무관하다**(-180~180 그대로 —
  사람이 보고 정한 값은 사람 것이다). `tts_rotate` 도 ±180 그대로 — 지시는 제목에 대한
  것이다.
- 더 좁히거나 넓히려면 `AI_TITLE_ROTATE_RANGE_DEG` 상수 하나만 고치면 된다. 완전히
  닫으려면(1차 버전으로 되돌리려면) `STYLE_DESIGN_ALLOWED` 에서 다시 빼고 검증기에서
  드롭 로직을 넣는다.
- 구 `checkpoint_style.json` 은 재검증 없이 그대로 재적용된다 — 이미 승인된 화면은 안
  바뀐다(1차 버전에서 범위 밖 값이 저장된 편은 없다 — 그 버전은 title_rotate 를 아예
  냈으면 즉시 드롭했다).

### E17-2 소스에 박힌 원본 자막 회피 — 대사 자막 + TTS 자막

`app/modules/subtitle_region.py` · `pipeline._detect_burned_band_cached` ·
`_avoid_burned_margin_v` · `--design-subtitle-avoid-burned {auto|off}`.

⚠ **E11·E15 와 달리 기본이 켜짐(auto)이다.** 지시가 '그렇게 되게 해 달라'였고, 검출이
못 찾으면 아무것도 안 바뀌기 때문이다. 픽셀로 맞춘 채널의 탈출구는 `off` 한 값이다.

⚠ **TTS(내레이션) 자막도 같은 회피를 탄다**(정정 지시: "tts 자막도 마찬가지로 겹치지
않게"). 대사 자막·TTS 자막은 화면에서 **독립된 두 트랙**이다(서로 다른 margin_v ·
서로 다른 글자 크기 `tts_line_font_size` · 시간상 배타적으로 표시되지만 위치는 각자
정해진다) — 그래서 `_avoid_burned_margin_v` 를 **두 번** 부른다(같은 밴드 검출 결과를
공유하되 자막 종류별로 겹침·이동량을 따로 계산). 정본(대사자막 0건 분기 포함)·variant
전부 다 적용된다 — 실측·회귀 가드는 이 절 끝에 합쳐 적었다.

- **좌표계는 캔버스다.** 검출도 렌더와 **같은 필터 체인**(crop_timeline → scale → crop)을
  태운 프레임을 재므로, 나온 행이 곧 화면의 y 다. 소스 픽셀을 재서 환산하지 않는다 —
  리프레임 크롭이 `t` 에 따라 움직여 언젠가 어긋난다(실측: 확대 크롭이 원본 자막을 아예
  잘라내는 경우도 있고, 그때는 회피할 것도 없다).
- 판정: 밴드를 480×270 회색조로 줄여 **행별 '밝은 획 경계' 개수**를 세고, 표본 프레임의
  절반 이상에서 걸리는 행을 띠로 본다(원본 자막은 편 내내 같은 자리에 있고, 배경의 밝은
  무늬는 장면이 바뀌면 사라진다). 밴드 **아래 절반만** 본다 — 위·가운데 텔롭은 우리 자막과
  겹치지 않으므로 피할 이유가 없다.
- 배치: **올리기만 한다**(아래는 로고·작품명 스택, 시작을 당기면 소리보다 자막이 먼저 뜬다).
  상한은 **제목 블록 아래 + 14px** — 지시의 "제목과도 겹치면 안된다"가 이 클램프다. 다
  못 피하면 제목을 우선하고 **모자란 양을 `[SubtitleAvoid/미달]` 로 남긴다**(조용한 포기 금지).
- **회피는 프리셋이 정해진 뒤**에 계산한다 — 자막 글자 크기(장르 프리셋 56~72px)가 확정돼야
  블록 높이를 잰다. 겹침 판정은 **2줄 기준**이다(`_wrap_for_ass(max_lines=2)`).
- 검출은 **렌더 단계에서 한 번**이고 `checkpoint_burned_subtitle.json` 에 남는다 —
  재렌더마다 다시 재면 승인된 자막 위치가 실행마다 달라진다(E15 체크포인트 규약과 같은 이유).
  클립 구성이 바뀌면(구간 재편집) 캐시는 무효다. variant #2·#3 도 **같은 띠**를 쓴다(재검출
  없음 — 원본 자막은 소재의 성질이다). **띠 검출은 한 번**이지만 대사·TTS 각각의
  margin 보정은 자막 종류·variant 별로 따로 계산된다(정본 대사 1 · 정본 TTS 2개 분기 ·
  variant 대사 1 · variant TTS 1 = `_avoid_burned_margin_v` 호출 6곳, 회귀 가드가 센다).
  로그는 `[대사]`/`[TTS]` 접두로 구분한다.
- 실패는 전부 '아무것도 안 함'이다(ffmpeg 없음·표본 부족·예외). 연출이 아니라 안전장치라
  본편 발행을 막지 않는다. 띠를 찾은 실행만 run_log `steps[{step:"subtitle_avoid_burned"}]`
  에 남는다 — 못 찾은 실행의 run_log 는 종전과 같다.
- ⚠ **밴드 기하는 `subtitle_region.band_geometry` 가 렌더러 [2] 순서를 따른다**(짝수 보정이
  video_y 클램프 **뒤**). `pipeline._video_band_bottom` 은 보정이 앞이라 홀수 높이에서 1px
  다르지만, 그 값으로 낸 자막 margin 이 이미 승인돼 있어(E10) 고치지 않았다 — 검출은 270행
  해상도라 1px 은 판정에 영향이 없다. 회귀 가드가 두 값을 ±1 로 묶는다.
- 임계값은 합성 프레임·규격 추정으로 잡은 **초기값**이다. 실소재로 다시 재는 자:
  `python -m scripts.e17_burned_subtitle_probe --video … --start … --end …`
  (행별 검출률 막대 + 판정된 띠 + **대사 자막·TTS 자막 각각의** margin_v 변화).
  합성 실측(이 컨테이너 ffmpeg 6.1.1 — 운영 노드는 7.x, 둘 다
  `SUPPORTED_FFMPEG_MAJORS`): 하단 번인 자막 1건 → y=1324~1376 검출,
  대사 자막 margin_v 430 → 610 · TTS 자막 margin_v 580 → 610(둘 다 같은 상한 —
  띠 위 14px). testsrc2·SMPTE 바·난수 노이즈에서는 검출 0건.
  비용은 4클립×3초 표본에 **0.94초**(그중 행 분석은 0.23초) — 렌더 앞에 붙는 시간이다.
  `--no-subtitles` 채널은 재지도 않는다.
- 회귀 가드: `tests/test_e17_burned_subtitle.py`(28건).

### E17-3 ElevenLabs 토큰 만료 → 기본 백엔드

`tts._EL_AUTH_STATUSES`·`ElevenLabsAuthExpired` · `stt_elevenlabs.ElevenLabsAuthError` ·
`pipeline.transcribe_chunks`.

E11·E12 는 **모든** 실패를 fail-loud 로 막았다(조용한 목소리 교체 금지). 그 규율을 두고
**401·403 만** 예외로 판다 — 재시도해도 안 낫고, 무인으로 도는 6대에서 키 하나가 만료되면
그날 전 채널이 멈춘다.

- TTS: 401·403 이면 **재시도 없이** `ElevenLabsAuthExpired` → 라벨 목소리는 edge-tts 로,
  편집실 `elevenlabs:{id}` 목소리는 **기본 목소리**로 간다(그 계정 자산은 재현할 수 없다).
  404·429·5xx·네트워크는 E11·E12 계약 그대로. **키가 없는 것은 만료가 아니다** — 접두사
  목소리의 즉시 실패는 그대로다(설정 실수는 시작 전에 안다).
- 판정은 프로세스 전역이고 **거절당한 그 키에 묶인다** — env 의 키가 바뀌면 다시 시도한다.
  `active_backend()` 가 폴백을 반영하므로 run_log `steps[resources].tts_backend` ·
  `checkpoint_resources.tts_backend` 가 실제 백엔드를 가리키고, 사유는 `tts_fallback_reason`.
- **폴백 산물은 유료 캐시에 안 넣는다**(`synthesize_cue_cached`) — 넣으면 키를 갱신한 뒤에도
  그 소리가 되살아난다.
- 전사: 401·403 이면 내장 Whisper 로 **청크 전량을 다시** 전사한다(섞으면 한 편 안에서 자막
  리듬이 갈린다 — E13-0 실측). run_log·`checkpoint_chunk_transcripts_meta.json` 은 **실제로
  쓴** 백엔드를 적어, 키를 갱신한 다음 실행이 백엔드 변경으로 읽고 캐시를 무효화한다.
  그 외 실패는 종전대로 크게 실패한다(유료 백엔드를 골랐는데 자막이 비면 안 된다).
- ⚠ 프로세스 중간에 만료되면 그 앞 cue 는 ElevenLabs, 뒤는 edge-tts 다(한 편 안에서 목소리가
  갈린다). 실행 전 만료가 보통이라 전량 폴백이 정상 경로고, 중간 만료는 로그로 드러난다.
- 테스트 격리: 만료 판정이 전역이라 `tests/conftest.py` 가 매 테스트 앞뒤로 되돌린다.
- 회귀 가드: `tests/test_e17_elevenlabs_fallback.py`(13건).
