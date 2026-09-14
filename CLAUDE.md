# AI Video Shorts Creator

## Gemini 모델 사용 규칙

이 프로젝트에서 Gemini API 모델은 반드시 아래 두 가지만 사용한다:

| 슬롯 | 모델명 | 쓰는 곳 |
|------|--------|---------|
| Pro 슬롯 (`GEMINI_MODEL_NAME`) | `gemini-3.7-flash` | **영상 분석** — `analyze_chunk()`·v3 Stage 1/2 (2026-08-31: 전 호출 Flash 3.7 — A/B 실측 근거, env 로 Pro 복귀 가능) |
| Flash 슬롯 (`GEMINI_FLASH_MODEL_NAME`) | `gemini-3.7-flash` | **그 외 전부** |

`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-3.1-flash-preview` 등 다른 모델은 사용 금지.  
코드 기본값은 위 모델로 고정 — 배포 노드 env 가 있으면 그쪽이 이긴다. 슬롯(역할) 구분은 유지한다(Pro 복귀 시 GEMINI_MODEL_NAME 만 바꾸면 된다).

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
  ⚠ **이 동작은 E18(8/25)이 뒤집었다** — AI 경로는 창 밖을 원래 제목으로 메워 제목이
  사라지는 시간이 없다(편집실 경로는 이 실측 그대로다). 아래 §E18 참조.

⚠ **제목 계약은 E18(2026-08-25)이 바꿨다**: 창의 `text` 는 이제 **아랫줄 한 줄**이고,
윗줄은 편 전체 고정이며, `title_bold(2)` 는 AI 에게 닫혔다. 아래 §E18 이 정본이다.
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

## overlay 현지화 계층 (L-P4, 2026-08-24)

`app/localize/overlay/` — 발주서: ves-orchestrator `docs/LOCALIZE_UNIFY.md` §3-2.
video-localization-project `engine/*` + `src/process_video.py` 를 **충실히 이식**했다.

⚠ **rerender(`app/localize/`)와 입력이 다르다.** 헷갈리면 엉뚱한 것을 고친다.

| | rerender | overlay |
|---|---|---|
| 입력 | ai-video job 디렉토리(체크포인트) | **외부 완성본 mp4 한 개** |
| 화면 속 한글 | 애초에 안 그린다 | 인페인팅으로 지우거나 · 병기하거나 · 그대로 |
| 컷 재현 | gen_flags 복원(프레임 단위) | 해당 없음(원본 시간축) |
| 채널 | 혜미리예채파 · 잔망루피 롱폼 | **잔망루피 쇼츠** |

- 진입점은 `python -m app.cli localize --mode overlay --video <mp4> --video-id <id>
  [--route B]` 다. **`--mode` 미지정 = rerender**(종전 그대로 · 회귀 0).
  `--video`/`--video-id` 를 rerender 에 주거나 그 반대면 **즉시 실패**한다 — 모드마다
  필수 인자가 달라 조용히 섞이면 rerender 가 엉뚱한 경로를 job 으로 읽는다.
- **route 정본은 `data/pipeline.config.yaml` 의 `levels`** 다. 구 '등급'을 이름만
  강등한 것이라 값이 그대로다(A·B·BJ·C·BC). `overlay/__init__.ROUTES` 와 config 가
  갈리면 CLI 가 통과시킨 route 를 엔진이 거절하므로 **테스트가 둘을 묶는다**.
- **더빙(route C·BC)은 이 계층이 하지 않는다** — vlp 규약 그대로다(`process_video`
  머리말). 검수 게이트를 지난 뒤 별도 단계가 맡고, `runner.needs_dub` 이 뒤따를 단계가
  있는지 알린다. ⚠ 이 값은 오케스트레이터 어댑터의 `needs_dub` 과 **같아야 한다** —
  갈리면 더빙이 빠진 편이 더빙된 줄 알고 발행된다(2026-08-12 에 실제로 난 사고).
- **무거운 의존을 requirements 에 안 넣었다.** OCR(rapidocr·paddleocr·easyocr)과
  인페인트(opencv·lama·sttn·propainter)가 **전부 지연 임포트 + 폴백**이라 임포트만으로는
  아무것도 안 끌려온다(회귀 가드가 서브프로세스로 실측한다). 백엔드 선택은 config 몫이고
  없는 백엔드는 다음 후보로 넘어간다(`detect._FALLBACK_ORDER`).
- 🛑 **`propainter` 라이선스 게이트를 그대로 옮겼다** — S-Lab 비상업 라이선스라
  `inpaint.propainter_commercial_ack=true` 없으면 차단이다(회귀 0 계약 §8-7).
  ack 이 있으면 그 다음 벽(가중치 미연동)에 닿는다 — 테스트가 두 벽을 구분해 고정한다.
- ⚠ **이식하며 의도적으로 바꾼 것 셋**(그 외 162개 함수·상수는 vlp 와 AST 동일):
  ① **모델** — `overlay/llm.resolve_model` 이 이 레포 모델 규칙을 강제한다.
     vlp config 의 `gemini-3.5-flash`·`gemini-pro-latest` 는 **사용 금지 모델**이라
     config 값을 읽지 않고 env 기본값(`GEMINI_FLASH_MODEL_NAME`·`GEMINI_MODEL_NAME`)을
     따른다. P1 이 `localize_run` Flash 를 바꾼 것과 같은 규약이다.
     `LLM_MODEL` env 로 못박는 vlp 규약은 유지했다.
  ② **경로 기준** — `common.PROJECT_ROOT` 가 ai-video 레포 루트이고, config 의
     상대경로(persona·font_map·glossary·fonts_dir)를 ai-video 위치로 다시 적었다.
  ③ **진입점** — vlp 의 `_parse_args`/`main` 은 안 옮겼다. 이 레포의 진입점은
     `app.cli` 하나다(rerender 가 세운 규약).
- ⚠ **E14 자막 노출 하한은 이 경로에 없다**(회귀 0 계약 §8-8). overlay 자막은
  `overlay/render.py` 가 조립하고 `merge_subtitle_segments` 를 지나지 않는다 —
  잔망루피 자막 타이밍이 조용히 움직이면 안 되므로 **끄고 시작**하는 것이 계약이다.
  켜려면 A/B 로 확인한 뒤 별건으로 한다.
- 회귀 가드: `tests/test_localize_overlay.py`(39건) + `scripts/overlay_port_diff.py`
  (vlp 와 AST 대조 — **250개 함수·상수 동일**, 의도된 차이 7건만 사유와 함께 통과.
  함수 안 임포트의 패키지 이름 변경은 정규화한다 — 안 하면 멀쩡한 이식이 전부
  '예상 밖 차이'로 떠서 아무도 도구를 안 보게 된다. vlp 동결 시 함께 은퇴).

### 더빙 (route C·BC 뒷단)

`overlay/dub.py` + `refbank.py` + `precheck.py` — vlp `src/` 에서 함께 옮겼다.

- **overlay 파이프라인이 이 단계를 부르지 않는다**(vlp 규약 그대로). 검수 게이트를 지난 뒤
  `python -m app.localize.overlay.dub` 로 따로 돈다.
- 🛑 **route 게이트 정본은 `DUB_ROUTES`(C·BC) 하나다**(2026-08-25). vlp 는 `level != "C"` 로
  **BC 를 거부**했는데(`src/dub.py:31`) 같은 레포의 `DUB_ROUTES`·어댑터 `needs_dub` 은 둘 다
  C·BC 다 — BC 편이 '더빙이 뒤따른다'고 표시된 채 더빙에서 거부당하는 불일치였다. BC 가
  한 번도 안 돌아 안 드러났다. 어댑터도 `--level=C` 하드코딩을 걷고 그 잡의 route 를 싣는다
  (게이트가 볼 재료를 위조하지 않는다). 회귀 가드가 `DUB_ROUTES` 와 게이트를 묶는다.
- ⚠ **self-ref 프로브는 현재 구성에서 도달 불가다** — `dub_backend == "gptsovits"` 일 때만
  지나는데 config 의 `tts_backend` 는 `elevenlabs` 다(2026-08-13 전환). 실측 로그에 흔적이
  없던 이유가 이것이다(refbank 때문이 아니다). 이식이 고친 `_SELF_MODULE` 배선만은 모델
  없이 확인된다: `-m app.localize.overlay.dub --probe-ref=/dev/null --prompt-text=x` 가
  `PROBE_FAIL` + exit 1 이면 정상(`No module named 'src'` 면 vlp 잔재). 회귀 가드가 파이프라인의 임포트를
  AST 로 훑어 **더빙을 부르지 않는 것**을 고정한다 — 부르면 게이트가 없어진다.
- 페이싱·리타이밍 숫자(`pacing_plan` 상한 1.35 · `char_budget` 하한 · `atempo` 체인
  분할)는 **잔망루피 목소리의 정체**라 한 개도 안 바꿨고 값으로 고정했다.
- `refbank` 는 영상마다 '깨끗한 긴 대사'를 은행에 쌓고 그 영상 음향 프로필(F0·밝기)에
  가장 가까운 항목을 고른다 — 짧은 대사의 self-ref 퇴화를 피하는 장치다.
- ⚠ 계획 §3-4 는 ai-video `tts.py`(E11·E12 프리셋·캐시·실패 분류)를 정본으로 삼아
  합치라고 한다. **합치기는 회귀 위험이라 아직 안 했다** — 충실 이식 → 회귀 0 확인 →
  합치기 순서다(P1 이 그렇게 갔다).

#### 🛑 이식 중 잡은 결함 3건 (회귀 가드가 잡았다)

파일 단위 복사는 **함수 안 임포트**를 놓친다. `test_no_stale_vlp_imports_in_the_port`
가 AST 로 훑어 셋을 잡았고, 셋 다 런타임에 죽는 것이었다:

1. `dub.py` 의 `from engine import render` — 재배선 누락
2. `dub.py` 가 self-ref 프로브를 **`-m src.dub`** 로 다시 부르던 것(모델 캐시 오염을
   피하려 자기를 서브프로세스로 부른다). 이 레포에 없는 모듈이라 즉사한다 —
   `_SELF_MODULE` 한 곳에서 만들도록 바꿨다.
3. `src/refbank.py`·`src/precheck.py` 를 **아예 안 옮긴 것** — dub 이 지연 임포트로
   쓰고 있어 문법 검사·임포트만으로는 안 드러났다.

⇒ 지연 임포트가 많은 코드를 옮길 때는 **문법이 통과해도 이식이 끝난 게 아니다.**

### 🛑 아직 이식하지 않은 것 (P4 잔여)
- **`src/autopilot.py` 의 자동 선별·승인** — 폐기다(사용자 결정 2, 사람이 지시한다).
  아카이브·선별은 오케스트레이터가 진다(P3·P3b).
- **실측 회귀 0** — **route BJ·C·BC 각 1편 통과**(아래 §실측). 나머지 편수는 아직이다. 대조 기준은 job_queue 가 아니라 **mm-06 의 autopilot 산출물**이다
  (VES 를 통한 overlay 잡은 3건뿐이고 마지막이 8/13, C·BC 는 한 번도 안 돌았다).

### 실측 (2026-08-24 · mm-06 · `5b2NhVS2h_o` route BJ)

```
산출 목록        6개 동일
원문(OCR·탐지)   18항목 동일          ← paddleocr 결정적, 이식된 detect 가 vlp 와 같은 결과
세그먼트 정렬     최대 오차 0.0s       ← 41개 이벤트가 밀리초까지 같은 자리
최종본 길이       11.245s = 11.245s
라우드니스        -20.00 = -20.00 LUFS
판정             ✅ 회귀 0
```

**돌린 방법**: 신 코드를 **vlp venv 로** 돌렸다(`runner` 임포트 체인이 ai-video 생성
스택을 하나도 안 끌어온다). 그래야 OCR 백엔드·의존성이 양쪽에서 같아 **차이가 나면
코드 차이**다. 운영 노드에 paddlepaddle 1.3GiB 를 설치하지 않아도 된다(계획 §10-1 회피).
⚠ 운영 체크아웃은 updater 가 SHA 로 고정한 detached HEAD 라 `git pull` 이 안 된다 —
별도 워크트리에서 돌린다. 절차: `docs/overlay_ab_runbook.md`.

⚠ **표본 1편이다.** 계획 §8-3 은 10편을 합격선으로 잡았는데, autopilot 산출 중
`ja_events.json` 이 있는 것이 이 한 편뿐이었다(나머지 둘은 이벤트 0). 그리고 이 편의
화면 텍스트는 18건 중 17건이 **기호·단일 문자**(`L` `:` `√` `¶` …)다 — 이식이 vlp 와
같은 것을 만드는지는 보였지만 **번역 품질 회귀를 보기에는 재료가 약하다.**

⚠ **CER 숫자를 비율로 읽지 마라.** CER = 편집거리 / **참조 길이**라 참조가 1자면 1.0 을
쉽게 넘는다(`'L'` → `'エル'` = 2.0). 이 실측의 평균 3.35 는 '335% 틀림'이 아니라 짧은
참조의 성질이다 — `overlay_ab` 가 짧은 참조 건수를 함께 찍는다.

### 실측 2 (2026-08-25 · mm-06 · 같은 소재 route C) — **인페인팅 경로까지 회귀 0**

route C 는 **기준본이 없었다**(그 소재의 autopilot 산출은 BJ 였다) — 구 엔진도 C 로
한 번 돌려 기준을 만들고 대조했다. ⚠ 그때 `--video-id` 를 반드시 바꾼다(같은 id 면
autopilot 운영 산출물을 덮어쓴다).

```
원문(OCR·탐지)   18항목 동일
번역문 CER       평균 0.0 · 동일 18/18      ← 번역까지 글자 단위 동일
세그먼트 정렬     최대 오차 0.0s
최종본 길이       11.245s = 11.245s
라우드니스        -20.10 = -20.10 LUFS
판정             ✅ 회귀 0
```

**`mask` → `lama` → `render(replace)` → FFV1 재조립**이 이번에 처음 돌았다(BJ 는 넷 다
건너뛴다 — 이식본의 절반이 미검증이었다). 구 18분 17초 · 신 18분 09초로 `lama` 337/337
프레임이 같은 시간에 끝났고, **렌더 백체크가 글자까지 같다**(양쪽 `22건 검사, 일치 15,
CER avg 0.318`). 이 값은 인페인팅한 프레임 위에 일본어를 그린 뒤 **다시 OCR 해 자가
검증**한 것이라, `overlay_ab` 가 프레임 픽셀을 안 보는 한계를 상당 부분 메운다.

⚠ **번역 18/18 동일은 재료가 약하다는 증거이기도 하다.** 구는 `gemini-3.5-flash`,
신은 `gemini-3.6-flash` 인데 결과가 같은 이유는 입력이 거의 전부 기호·단일 문자
(`L` `2` `:` `-`)라 두 모델 다 '그대로 유지'로 답했기 때문이다. 번역 품질 회귀는
여전히 못 봤다.

⚠ **아직 안 본 것**: route BC · self-ref 프로브 · 프레임 픽셀 대조 · 나머지 소재.

### 실측 3 (2026-08-25 · mm-06 · `G-mRZ8ncYp8` 더빙) — **구조 동등 · 품질은 사람 몫**

⚠ `5b2NhVS2h_o` 는 **대사 0건**이라 더빙 소재가 못 된다(구·신 **둘 다** `dub_from_video`
같은 자리에서 `받아쓰기된 대사 없음` 으로 실패 — 그 자체가 ASR 경로 일치의 증거다).
대사 18건짜리 `G-mRZ8ncYp8` 로 다시 붙였다.

| | 구 | 신 |
|---|---|---|
| ASR → 옹알이 게이트 | 18 → **16** | 18 → **16** |
| 백체크 | 16세그 · 실패 **1** | 16세그 · 실패 **1** |
| demucs 보컬 제거 → 반주 믹스 | 돔 | 돔 |
| 산출 파일 | 3개 · 31M | 3개 · 31M |
| **`final_dubbed.mp4` 길이** | **37.453991s** | **37.453991s** |
| `dub_ja_draft.wav` | 37.327s | 37.095s |
| 소요 | 2:00 | 2:13 |

`pacing_plan`·백체크 재합성 루프·옹알이 게이트·demucs 가 **같은 규칙으로** 작동했다.
이식된 1,469줄이 실제로 돈다.

#### ⚠ 번역 모델 교체의 하류 효과 — 여기서 처음 보였다

앞의 두 실측은 입력이 기호·단일 문자뿐이라 **번역 품질을 못 봤다.** 더빙에서 처음으로
실제 문장 16개가 번역됐고 차이가 드러났다:

```
구(3.5-flash): 'なんで触るの！'        신(3.6-flash): '大声で言う。触りたい！'
                                       신: '全身ピンクの毛だし髪もだよ'
```

| | 구 | 신 |
|---|---|---|
| 백체크 재합성 | 3회 | **10회** |
| CER avg / max | 0.081 / 0.600 | **0.125 / 0.750** |
| 페이싱 최대 배속 | 1.12x | **1.30x** |

**이식 결함이 아니다** — `gemini-3.6-flash` 가 더 장황하게 번역하고, 긴 문장이 슬롯에
안 맞아 배속이 올라가고 TTS 백체크가 더 틀린다. 하지만 **계획 §8-3 이 CER 을 합격선에
넣은 이유가 이것**이므로, 실운영 전 **사람 청취 검수**가 필요한 항목으로 남긴다.
(모델 규칙을 바꿀 수는 없다 — CLAUDE.md 가 3.5-flash 를 금지한다.)

⚠ **self-ref 프로브는 이번에도 안 돌았다**(양쪽 로그에 흔적 없음). `_SELF_MODULE` 로
고친 그 경로는 **아직 미검증**이다 — refbank 에 쓸 항목이 있으면 self-ref 를 안 만든다.

#### 🛑 실런이 드러낸 운영 성질 2건 (이식 결함 아님 · 개선 후보)

1. **비싼 단계 뒤에 자격증명 검사가 있다.** `ves.env` 의 `GEMINI_API_KEY` 는 수명이
   짧은데(실측: 106자 → 만료 → 갱신 후 53자), **18분짜리 인페인팅을 다 하고 나서**
   번역에서 `401 UNAUTHENTICATED` 로 죽었다. 비싼 단계 **앞**에서 키를 확인하면 그
   18분을 안 버린다. vlp 도 같은 구조라 이식 결함은 아니다.
   ⚠ 재개 시 인페인팅은 다시 돈다 — `inpaint_sequence` 는 기존 결과를 건너뛰지 않는다.
2. ~~**JP 폰트가 양쪽 다 없다.**~~ → **번들해서 닫았다**(2026-08-25). vlp 는 `fonts/` 를
   gitignore 하고 README 의 curl 로 각자 받게 했는데, 그래서 실측 노드에 파일이 없어
   **양쪽 엔진이 조용히 기본 폰트로 떨어졌다** — 기본 폰트에는 일본어 글리프가 없어
   두부(□)가 된다. ai-video 는 한국어 폰트를 이미 번들하므로 같은 규약으로 맞췄다:
   `app/assets/fonts/` 에 Noto Sans JP(Black·Bold·Medium·Regular) + Noto Serif JP(Bold)
   **28 MB**. OFL 이라 재배포에 문제가 없다.
   ⚠ 출처가 Google Fonts 라 **TTF** 이고 확장자도 `.ttf` 로 맞췄다(vlp README 의 noto-cjk
   OTF 와 같은 서체·웨이트이고 PIL·libass 는 둘 다 읽는다). `font_map.yaml` 도 함께 고쳤다.
   회귀 가드 2건: font_map 이 가리키는 파일이 **전부 있는지**, 그리고 **실제로 일본어가
   그려지는지**(확장자만 맞고 글리프가 없으면 두부가 된다).

### 🛑 실런이 잡은 결함 4건 (AST 대조는 넷 다 못 잡는다)

이식 대조가 250개 함수 동일을 확인해도 **실런은 별개다.** 넷 다 실제로 돌려서 나왔다:

1. **모델과 요청 모양은 짝이다** — 모델만 규칙(`gemini-3.6-flash`)에 맞추고 vlp 의
   `ThinkingConfig(thinking_budget=0)`(2.5 시대 인자)를 그대로 뒀다 → `400
   INVALID_ARGUMENT`. 3.x 어휘 `thinking_level="minimal"` 로 고쳤다(본체 `gemini_client`
   와 같은 어휘). **AST 는 vlp 와 '같아서' 통과했다.**
2. **모델이 프롬프트의 번호를 `source` 에 되돌려준다** — `build_user_prompt` 가 각 줄을
   `1. {텍스트}` 로 적는데 `gemini-3.6-flash` 가 `'1. L'` 로 돌려줘 18/18 이 안 붙었다.
   ⚠ **비결정적이다**(같은 입력으로 붙는 판도 있었다). **프롬프트는 안 고친다** — 문구를
   바꾸면 번역이 통째로 달라져 vlp 와 대조가 안 된다. 붙이는 쪽을 견고하게 했다
   (`_index_by_source` — 번호를 뗀 열쇠를 더하되 **정확 일치가 항상 이긴다**).
3. **전량 빈 번역이 조용히 통과하던 것**(vlp 결함, 이식이 노출) — `_transcreate_one` 은
   못 찾은 항목을 빈 문자열로 채운다. 전부 못 찾으면 빈 자막이 렌더까지 간다. 실측에서
   **이벤트 0개짜리 산출**이 나왔고 로그엔 `flagged 18` 만 찍혔다. 이제 크게 실패하고,
   메시지에 **응답 source 키와 입력 텍스트를 나란히** 실어 (2)를 한 번에 짚었다.
4. **첫 가드가 틀린 조건을 봤다** — `not rows` 를 봤는데 실제 증상은 '행은 있는데 하나도
   안 맞음'이었다. 실런이 가드까지 교정했다.

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

⚠ **SHOTCONE 얘기가 아니다.** 실측 — **21개 채널 전부** `style_compose: true` 다
(가왕쇼 런이 그중 하나다). 아래 1·2는 **전 채널 문제**다.

⚠ **켜는 것은 채널 design 키 하나다.** `ops_config.channel_style` 은 *배포 게이트*(허용)일
뿐이고, 실제 `--style-compose` 는 `design.style_compose` 가 참일 때만 붙는다
(`adapters/aivideo.py` 의 `design_for_job` — **키가 없으면 꺼짐**).
⚠ **`channel_design_overrides` 는 통째 교체다** — 대시보드에서 디자인을 저장할 때
`style_compose` 를 같이 안 실으면 그 채널의 AI 연출이 **조용히 꺼진다.** 2026-08-24
11:04 HANIPJUMAK 이 그렇게 꺼졌고(00:00 런은 연출을 탔는데 12:59 런은 안 탔다), 키만
병합해 되살렸다. 채널 설정을 손으로 고친 뒤에는 이 키가 살아 있는지 확인할 것.
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
`base_title` 을 받으면 창 끝을 영상 길이로 클램프하고, 남은 빈 시간을 메운다.

⚠ **메우는 내용은 E19(8/25)가 바꿨다** — 기본 제목이 아니라 **직전 제목**이 잇는다
(기본 제목의 아랫줄은 결말 후킹이라 앞 구간에 붙으면 내용이 어긋난다). '빈 시간이 없다'는
이 절의 계약은 그대로고, 무엇으로 채우는지만 달라졌다. 아래 §E19-3 이 정본이다.

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
- 몇 건을 당겼는지 style 단계 `text_clamp: {clamped, of, y_range}` 로 남긴다(2026-08-24
  후속) — stdout 에만 있으면 '클램프가 돌긴 했나'를 나중에 확인할 길이 없다.

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
- **감사 기록(2026-08-24 후속)**: 판정이 돌았으면 **띠를 못 찾아도**
  `steps[{step:"subtitle_avoid_burned"}]` 를 남긴다(`band: null` · `profile_frames`).
  종전엔 찾은 실행만 남겨서, 단계가 없을 때 '띠가 없었다'인지 '판정이 아예 안 돌았다'인지
  구분이 안 됐다 — 실물 검증에서 실제로 막혔다. 구간별로 올린 줄 수는 ASS 조립이 끝난 뒤
  같은 dict 에 `windowed: [{track, moved, lines, base_margin_v}]` 로 채운다.
  ⚠ `off` 채널·`--no-subtitles` 는 표본을 아예 안 뜨므로 **종전과 같이 아무것도 안 남는다**.
  **조건은 결과가 아니라 `_BURNED_STATE["ran"]`(판정이 돌았는가)이다**(2026-08-25 교정) —
  결과로 걸면 판정이 돌고도 띠·표본을 **둘 다** 못 얻은 실행(ffmpeg 없음·프레임 못 뜸·예외)이
  단계 부재로 남아 가장 알고 싶은 경우가 조용해진다. 그 사유는 `error`·`profiles_error` 로
  함께 남는다. 채널이 끈 경우(`mode != auto`)는 `ran` 이 서기 전에 빠져나가 무기록이다.
- ⚠ **`--no-subtitles` 의 출처는 둘이고, 단계 부재의 대부분은 이것이다**(2026-08-25에
  이걸 못 보고 오진했다 — 기록해 둔다):
  ① 채널 design **`subtitles: false`** → 어댑터 `CHANNEL_DESIGN_FLAGS` 가 `--no-subtitles`
     로 옮긴다. **채널 고정**이라 그 채널은 늘 자막이 없다(예: CAREERDAY·JAEMISHOTS·
     HEUNGHAENG·DARAMJI·NEOGULBANG).
  ② 잡 파라미터 **`no_subtitles`** — planner 가 `sources.has_subtitle=false` 면 true 로
     넣는다. **편마다 다르다**(자막 그리는 채널이라도 그 편 소스에 SRT 가 없으면 안 그린다).
  ⇒ '이 채널이 자막을 그리는가'는 **①과 ②를 함께** 봐야 안다. work_order `has_subtitle`
     하나만 보면 ①을 놓친다. 실측(8/25, 15편): 자막을 실제로 그린 편은 2편(SHOTNOW·
     TETOCHIP)이고 **둘 다 단계가 남았다** — 나머지 13편의 단계 부재는 전부 정상이다.
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

- 회귀 가드: `tests/test_e18_windowed_avoid.py`(22건).

### 남은 별건 (사용자와 합의)

없음 — E17-2 간헐 텔롭 건은 위 E18-6 으로 닫혔다.

회귀 가드: `tests/test_e18_style_placement.py`(15건) · `tests/test_e18_localize_telop.py`(11건)
· `tests/test_e18_windowed_avoid.py`(22건) · `tests/test_e15_style_compose.py` 의 제목 회전 절(4건).

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

## 잔망루피 롱폼 JP 포맷 계약 (2026-08-27, 운영자 결정)

혜미리예채파식 "한국어 원음 + 일본어 자막" 포맷은 **예능 전용**이다. 잔망루피 롱폼
(`shorts_jp_localized` · work "잔망루피 유튜브 숏폼")은 **대사도 일본어 더빙 + 내레이션
없음**이다 — 첫 실전 판(巨大イチゴ, run 439a39a4)으로 왕복 검증됨.

### --no-narration (`PipelineInput.include_narration`)

- 오케스트레이터 채널 design 스위치 `narration:false` → 이 플래그. **미지정 = 종전
  그대로(회귀 0).** `--no-tts-audio`(믹스만 생략)와 다르다 — 이쪽은 **합성 요금도 없다**.
- 개입 지점은 **앵커 해석 직전 한 곳**(`pipeline.py` `[tts cues]` 블록 앞) — 신규·체크포인트
  재개·편집실 오버라이드 모든 경로가 그 지점을 지나고, cue 0 은 이미 정상 상태라 하류
  (합성·TTS 자막·믹스·현지화 L3t)가 전부 자연히 빈다.
- ⚠ 편집실 내레이션 오버라이드보다 **뒤**라 사람이 넣은 cue 도 버려진다(건수 로그) —
  이 채널 편집실엔 내레이션 탭 자체가 안 뜬다(cue 0).
- ⚠ 새 CLI 플래그 — 구 엔진 노드에서 argparse 즉사. 채널 design 에 싣는 것은 엔진 전
  노드 배포 **뒤**다(style_compose 와 같은 롤아웃). 실측: design 키를 넣기 전에 출발한
  잡이 구 포맷으로 나와 반려-재실행했다(잡은 **실행 시점**에 design 을 읽는다 —
  `enrich_params` 가 DB 를 읽으므로 체인 재실행 전에 키가 있으면 된다).

### L4d — 롱폼 대사 더빙 (`app/localize/dub_rerender.py`)

- **게이트**: `locales.json works[작품].ja.dub` — 없으면 단계 자체가 없다(SHOTCONE 회귀 0).
  자리는 runner 의 L4 **뒤**·L5 앞(렌더 산출 위에서만 돈다 — `skip_render` 면 없음).
- **쇼츠 C 루트의 기계를 그대로** 쓴다: `overlay/dub.dub()`(합성·백체크·페이싱 1.35) ·
  `separate_vocals`(demucs) · `common.mux_dub`. 목소리도 쇼츠와 같은 루피 보이스
  (overlay config `dub.voice_id`) — work_cfg `dub.voice_id` 로 편별 교체만 열어 둔다.
- **다른 것 둘뿐**: ① ASR·재번역 없음 — 문구·시각 정본은 L3 의 일본어
  `subtitle_segments.json`(렌더가 구운 자막과 더빙이 **같은 소스**라 어긋날 수 없다).
  ② 자막 안 굽기(`burn_dub_subtitle=False`) — L4 가 이미 구웠다.
- 대사 0줄(노래뿐인 편)은 **원음 유지**(空飛ぶルーピー 실사고 — 보컬 제거가 가사를
  지운다). 그 외 실패는 크게 — 한국어 원음이 조용히 발행되는 것이 실패보다 나쁘다.
- 의존(demucs·faster_whisper) 프리체크를 합성 **앞**에 둔다. 교체 전 본은
  `localize_<locale>/shorts_ja_nodub.mp4` 보존. 산출은 `localize_<locale>/dub/`.
- 회귀 가드: `tests/test_longform_dub.py`(11건).

## 캐릭터 어미 「〜ルプ」 (2026-08-27 확정 — persona §2 의 [채택 대기] 종료)

- **표기와 발음이 다르다.** 표기(자막·번인·SRT·검수 카드)는 「ルプ」 그대로. 발음(더빙
  TTS)만 한국어 "뤂"처럼 받침으로 — **합성 입력에서만** 문말 「ルプ」→「ルプッ」 치환
  (`app/localize/ja_reading.clip_character_ending` 한 곳 · overlay dub `synthesize_segment`
  와 rerender `narration.l3t_tts` 두 배선).
- 문장 끝에만 · 정보성 텍스트(설명란·©) 금지. 규칙 정본은 overlay `persona.md` §2,
  rerender 는 `locales.json` works 항목 context 가 같은 말을 한다 — **세 곳(persona·
  locales·모듈)이 갈리면 가드가 실패한다**(`test_p7_loopy_work_locale` ·
  `test_localize_overlay` 어미 절).
- 문중 「ルプルプ」·이미 치환된 「ルプッ」·어미 없는 작품(SHOTCONE)은 그대로(회귀 0).

## overlay 검수 카드 좌표 계약 (P6-2, 2026-08-27)

- 카드 `ko_ja_pairs` 의 칸은 **`subs`** 고 idx = **translations.json entries 순번** —
  엔진 오버라이드(`_apply_subtitle_overrides` subs{idx})와 같은 좌표라 편집실이 고친 값이
  왕복한다(첫 판 telops 는 화면만 그리고 엔진에서 증발했다). use:false 줄은 빼되 idx 는
  건너뛴 채 보존. 시각·스타일은 `ja_events.json`(entry_idx)에서 얹는다.
- **translate 재실행 캐시**: 원문이 같으면 초벌 재사용(dub 의 번역 캐시와 같은 규약) —
  수정 재렌더에서 **고치지 않은 줄**이 비결정 재번역으로 흔들리면 편집실 계약("고치면
  그 항목만")이 깨진다. 지난 재렌더가 병합한 style·타이밍·use 도 이 경로로 살아남는다.
  원문이 하나라도 다르면 전량 재번역(좌표가 다르다). vlp 와 의도적 차이 —
  `overlay_port_diff` EXPECTED_DIFFS "translate.translate".


## P8 — vlp 동결 (2026-08-27)

- 레거시 검수 카드 15장 일괄 반려(운영자 지시) 후 `ops_config vlp_frozen=on`(ves 0102).
  `zanmang_decision` 어댑터가 실행을 거절한다 — vlp 는 더 이상 어떤 잡도 받지 않는다.
- **`scripts/localize_port_diff.py`·`scripts/overlay_port_diff.py` 는 함께 은퇴했다**
  (위 절들의 "vlp 동결 시 함께 은퇴" 예고 이행). EXPECTED_DIFFS 의 사유들은 각 계약
  절에 이미 옮겨져 있다. vlp 산출물(mm-06 outputs/)은 남는다 — 재작업이 필요하면
  그 mp4 를 overlay 파이프라인 입력으로 쓴다(vlp 를 되살리지 않는다).
- 절차·판정 기록: ves-orchestrator `docs/P8_VLP_FREEZE.md`.

## 채널 톤 프로파일 (E19-1, 2026-08-28)

`app/modules/style_tone.py` · `app/data/style_tones/<이름>.json` · `--style-tone <이름>`.
발주서: `docs/prompts/e19-drama-clip-preset.md` §1 — **E19 의 나머지 항목(2~8)은 미구현**이고
이 플래그 하나가 E19 전체의 유일한 신규 CLI 플래그다(롤아웃 표면적 최소화 설계).
값 정본: `docs/design_presets/drama_clip_kr.preset.json` 의 `status:"prompt"` 항목 —
프로파일과 갈리면 `tests/test_e19_style_tone.py` 가 잡는다.

- **게이트**: `--style-tone` 미지정 = 프롬프트·하드캡이 종전과 완전히 동일(회귀 0).
  블록 함수(`story_prompt_block`/`style_prompt_block`)는 None 에 빈 문자열을 돌려준다.
- **주입은 덧붙임 절 두 개뿐** — story(`compose_story_with_context`)·style(`compose_style`)
  프롬프트 **맨 뒤**. 본문 프롬프트 문자열은 동결(E13 규율 — 문구가 바뀌면 전 채널이 흔들린다).
- `labels.density_max`(상한 `DENSITY_MAX_LIMIT` 20)가 E15 `MAX_TEXTS`(8)를 대체한다 —
  프롬프트의 '상한' 줄(compose_style `max_texts` 인자)과 `validate_plan(max_texts=)` 이
  **같은 숫자**를 봐야 LLM 이 캡까지 쓰고도 잘리지 않는다. 체크포인트 재적용(E15 재개
  계약)은 재검증이 없으므로 캡 변경의 영향도 없다.
- **fail-loud**: 없는 이름·깨진 파일·모르는 enum·범위 밖 값 전부 `StyleToneError` 즉시 실패.
  CLI 사전검사(비싼 청크 분석 **앞**)와 파이프라인 로드 두 곳이 같은 규율 — 조용히 기본
  톤으로 떨어지는 경로가 없다(E11 transcribe-backend 규율).
- **enum 은 고정 화이트리스트**(NARRATION_TONES 등) — 값을 늘리려면 프롬프트 문구 표
  (`_STRUCTURE_TEXT` 등)를 **같이** 늘린다. 문구 없는 enum 은 로드에서 죽는다.
- 기록: `run_log.provenance.style_tone = {name, sha12(파일 바이트)}` — 프로파일을 고치면
  sha 가 갈려 산출물이 어느 판으로 나갔는지 재추적된다.
- ⚠ **신 CLI 플래그** — 구 엔진 노드 argparse 즉사. ves 어댑터 `style_tone` 키 미러는
  엔진 전 노드 배포 **뒤**(style_compose 와 같은 롤아웃, ops 게이트 channel_style 재사용).
- 회귀 가드: `tests/test_e19_style_tone.py`(23건 — 회귀 0·fail-loud·프리셋↔프로파일 값
  대조·배선 문자열 고정).

### E19-3 내레이션 cue–대사 겹침 검사기 (2026-08-28)

`pipeline.snap_cues_to_dialogue_gaps` · `[cue gaps]` 블록(앵커 해석 직후 · style 앞).

- **게이트는 톤 프로파일이다** — `narration.placement == "dialogue_gaps_only"` 일 때만
  돈다. `--style-tone` 미지정 채널은 검사 자체가 없다(회귀 0). 새 플래그 없음.
- 겹침 판정은 cue 창 vs `final_segments`(대사) 합계가 `CUE_OVERLAP_TOLERANCE_SEC`(0.2s)
  초과 — 릴레이는 경계가 맞닿는 문법이라 관용치가 필요하다.
- 스냅은 **cue 길이가 통째로 들어가는 gap** 이 있을 때만, 원위치에서 가장 가까운 배치점
  으로. 이미 자리 잡은 다른 cue 창도 점유물로 본다(스냅이 cue 끼리 새 겹침을 만들면
  안 된다). **들어갈 gap 이 없으면 옮기지 않고 `[cue-overlap]` 경고만** — 멀쩡한
  내레이션을 지우거나 엉뚱한 자리로 보내는 것이 겹침보다 나쁘다(영상 밖 cue 안전망 규율).
  시간이 깨진 cue 도 판정을 포기하고 그대로 싣는다(같은 규율).
- **첫 variant 한정** — `final_segments` 좌표가 그 타임라인이다(variant #2·#3 은 자막
  계열을 안 받는 기존 구멍 — E15 규약과 동일). 자리가 비싼 합성(resources) **앞**인
  것도 계약이다(합성 뒤에 옮기면 요금이 아깝다).
- 기록: run_log `steps[{step:"tts_cue_gaps"}]` = `{of, cue_snapped, warned, details,
  tolerance_sec}` + 건별 stdout. 순수 함수 — 넘겨받은 cue 를 건드리지 않는다.
- 회귀 가드: `tests/test_e19_cue_overlap.py`(11건 — 게이트·자리·순수성·점유 스냅 고정).

### E19-2 제목 단어 단위 색 강조 (2026-08-28)

`renderer.strip_title_markup`·`extract_title_highlights`·`_emit_line_text` ·
design 키 `title_highlight_color`(기본 `#FFE24A`, `--design-title-highlight-color`).

- 문법은 제목 문자열 안 **`{{어절}}`** 하나. 제목(title_text)과 시간대별 제목
  (title_segments 텍스트) 둘 다 같은 문법을 탄다. **마크업이 없으면 렌더 경로가 종전과
  바이트 동일**(회귀 0 — E10/E8/E7 필터그래프 문자열 가드가 함께 지킨다).
- 세그먼트 배치는 같은 TTF 를 **Pillow 로 재서 절대 x** 로 나란히 놓는다(둥근 박스 PNG
  폭 측정 `_measure_title_text_width` 와 같은 신뢰). ffmpeg 와의 수 px 차이는 세그먼트
  경계에서만 나며 실렌더 확인 대상. 폰트가 파일이 아니면 1em/글자 근사(한글 근사 충분).
- 마커는 **줄바꿈 계산에 안 센다**(split_text_smart 의 clean_word) — 마커 때문에 20자
  제목이 두 줄로 갈라지거나 길이 축소가 달라지면 안 된다.
- rect 박스 줄의 강조: 세그먼트별 box 는 조각난 상자가 되므로 **상자색 글자+box 밑그림**
  을 먼저 깔고(ffmpeg 가 전체 폭을 직접 잰다) 글자는 box 없이 그 위에 그린다.
  round 박스·회전 제목(_emit_rotated_title)·bold(세그먼트 색 외곽선)와 전부 직교.
- **잔여물은 떼고 그리되 건별 경고**(`[TitleHighlight] ⚠`) — 홀짝 불일치·중첩·빈 강조
  전부. 제목에 중괄호가 노출된 채 발행되는 것이 최악이다.
- ⚠ **AI 마크업 생성은 아직 닫혀 있다**(사람 경로만 개통 — 편집실·채널이 제목 문자열에
  직접 쓴다). 톤 프로파일로 열려면 story 산출 제목의 마크업이 edit_plan·발행 메타로
  그대로 흘러가는 표면(대시보드·유튜브 제목)의 세척이 선행돼야 한다 — E19 후속 별건.
- 회귀 가드: `tests/test_e19_title_highlight.py`(16건 — 파서·잔여물·회귀 0·절대 x 배치·
  rect 밑그림·회전·tseg 경로).

### E19-4 라벨 얼굴 회피 · 컷 경계 재배치 · 글로우 (2026-08-28)

`style_compose.avoid_faces_for_texts`·`face_box_on_canvas` · `reframe.CropKeyframe`
face_* 필드 · `[face avoid]` 블록(**리소스 3분기 수렴 직후** · 텍스트 ASS 앞) ·
`TEXT_FX += "glow"`.

- ⚠ **자리 교정(울트라리뷰 bug_001, 2026-08-28)**: 처음엔 리소스 생성 elif **안**에
  뒀다가, `--from-step render`(A/B 재렌더 표준 경로)가 캐시 else 로 빠지며 회피를
  건너뛰고 checkpoint_style 원 좌표로 texts.ass 를 덮어쓰는 결함이 리뷰에서 잡혔다
  (E15 재개 계약 위반). 지금은 3분기(캐시·생성·폴백) 수렴 뒤라 재개도 지난다. 회피는
  플랜+크롭 JSON+design 의 **결정적** 순수 함수라 재개마다 재도출해도 같은 좌표다 —
  체크포인트에 따로 저장하지 않는 이유(크롭 JSON 은 run_dir 에 남는다). 가드가 블록
  위치를 문자열 순서로 고정한다(같은 앵커 문구가 파일에 두 번 있어 **rindex** 로 잰다).

- **얼굴 좌표는 리프레임 크롭 타임라인의 것을 재사용한다** — `CropKeyframe` 에 그
  표본의 raw 검출 박스(face_cx/cy/w/h)를 함께 싣는다(검출 비용 0, 발주서 §4 규율).
  ⚠ **구 캐시 JSON 은 face 키가 없다** — `.get()` 으로 미검출 취급 = 회피 없이 종전
  배치(재개 호환). `face_tracking:false`·검출 실패·크롭이 얼굴을 잘라낸 경우도 같다
  (안전장치가 연출을 막으면 안 된다 — E17-2 실패 규율).
- **게이트 셋**: ① AI 라벨만(`_texts_from_style` — 편집실 텍스트는 사람 배치라 안
  건드린다) ② 톤 프로파일 채널만(미지정 = 종전 배치, 회귀 0) ③ 리프레임 켜짐.
- 후보는 **위 → 아래 → 옆(얼굴 반대쪽 먼저)** 첫 성립 — 성립 = 얼굴 비겹침(간격
  12px) + 밴드 y 구간(E18-2 와 같은 글자 반높이 여유 size×0.6) + 캔버스 가로 안.
  어느 후보도 안 되면 **옮기지 않고 기록**(살리고 당긴다 — 겹침이 증발보다 낫다).
- **컷 경계 재배치**: 창이 클립 경계를 넘으면 경계에서 쪼개 조각마다 그 클립의 얼굴로
  재배치(v3 texts 조각당 한 항목 — 렌더 계약 불변). 0.15s 미만 조각은 안 만든다.
- ⚠ 리프레임은 얼굴을 크롭 중앙에 두므로 얼굴의 캔버스 위치는 대개 밴드 중앙 부근이다
  — 회피가 실질을 갖는 것은 EMA 지연·데드존·소스 가장자리 클램프로 크롭 중심과 raw
  얼굴이 어긋나는 순간과 얼굴 크기다(테스트 픽스처 주석에 같은 말이 있다).
- **fx="glow"**(괄호형 상태 라벨 발광): `build_texts_ass` 한 곳 — 외곽선을 글자 자기
  색으로 두껍게(size×0.12) + `\blur`(size×0.08). stroke 지정은 glow 가 대체한다.
  v3 어휘 확장이라 편집실도 쓸 수 있고, 미사용 시 ASS 는 종전과 동일. 톤 프로파일
  style 블록이 괄호형에 glow 를 권한다(기본 프롬프트는 동결 유지).
- 기록: run_log `steps[{step:"style_face_avoid"}]` = `{of, split, moved, kept_overlap,
  no_face}` + 건별 stdout `[face-avoid]`. 순수 함수(사본만).
- 회귀 가드: `tests/test_e19_label_face_avoid.py`(17건 — 변환 수계산·후보 순서·실패
  유지·컷 분할·구 JSON 호환·글로우 ASS·배선).

### E19-5 효과음(SFX) 레이어 (2026-08-28)

`style_compose.load_sfx_manifest`·`stage_sfx`·`sfx_prompt_block`·validate_plan sfx 절 ·
`renderer.RenderInputs.sfx_audio`·`sfx_within_video` · 톤 프로파일 `sfx` 절(선택).

- **스티커 규율 그대로**: `app/assets/sfx/manifest.json` 의 **id 만** AI 에 노출,
  **빈 목록이 정상 상태**(라이선스 확인된 파일만 번들), 없는 id 는 그 항목만 드롭+기록.
  고른 파일은 `style_assets/` 로 스테이징 — 체크포인트 재적용(편집실 재렌더)이 번들
  없이도 같은 소리를 재현한다.
- **게이트 둘**: 톤 프로파일에 `sfx` 절(max_per_episode ≤10·mix_gain_db −30~0·
  target_beats 화이트리스트)이 있고 + 번들이 비어 있지 않을 때만. 닫혀 있으면 기본
  프롬프트에 SFX 절 자체가 없고(동결 유지 — LLM 이 어휘를 모른다), 그래도 플랜에
  실려 오면 **전량 드롭+기록**(unknown 거절은 과하다 — title_rotate 의 '받되 버린다').
- 플랜 어휘 `sfx: [{id, source_time_sec, gain_db?}]` — 좌표는 다른 항목과 같은 원본
  절대초, 배치도 **같은 함수**(place_anchored_images probe — 자막 강조와 동일 규율).
  캡·기본 gain 은 프롬프트와 validate_plan 이 **같은 숫자**를 본다(E19-1 규율).
  gain 범위 밖은 클램프+기록.
- **렌더**: cue 와 같은 방식 — 입력 인덱스 = 클립 수 + cue 수 + si, `volume=<g>dB` +
  `adelay` + amix 합류. **원본 오디오를 덕킹하지 않는다**(짧은 스팅에 덕킹이 걸리면
  원음이 펌핑한다). 영상 밖 시작 sfx 는 `sfx_within_video` 가 cue 안전망을 **재사용**해
  render_video 한 곳에서 거른다(같은 목록 규율). sfx 미지정이면 입력·필터 문자열이
  종전과 동일(회귀 0 — 'sfx' 글자 부재를 테스트가 고정).
- 첫 variant 한정 + `--no-tts-audio`(믹스 끔) 실행은 효과음도 싣지 않는다.
- 기록: run_log style 단계에 `sfx_used`·`sfx_dropped`, 드롭 건별 stdout.
- 회귀 가드: `tests/test_e19_sfx_layer.py`(15건 — 게이트·캡·스테이징·입력 인덱스·
  회귀 0 문자열·late 가드·프리셋↔프로파일 값 대조).

### E19-6 무음 컷 잔여 정적 하한 (2026-08-28)

`silence_cutter.SilenceCutProfile.min_residual_pause_sec` · env
`SILENCE_CUT_MIN_RESIDUAL_SEC` · CLI `--silence-min-residual` ·
A/B 도구 `scripts/e19_silence_residual_ab.py`.

- 벤치마크 실측: 무음은 0~2건으로 밀되 **반전 직후 0.3~0.5s 정적은 남는다**(편 최대
  호흡·웃음 비트). aggressive 가 이것까지 밀면 호흡이 죽는다 — 그래서 안전한 gap 을
  **잘라도** 이만큼의 정적(양쪽 절반씩)은 남기는 하한을 더했다.
- **None(기본) = 산출 불변**(회귀 0). 기존 잔여 정적은 2×padding_sec(aggressive 0.24s)
  이라 그 이하 값도 불변이다. gap 이 하한보다 짧으면 gap 전체가 남는다(없는 정적을
  만들지 않는다). **gap-level(aggressive) 경로에만** 작용 — conservative·보호 gap 의
  cap 분기·`cut_silence_from_clips` 레거시는 안 건드렸다.
- env 값이 숫자가 아니거나 범위(0 초과 2 이하) 밖이면 `get_silence_profile` 이 **즉시
  실패** — 조용히 무시하면 오타가 기본값으로 발행된다(transcribe-backend 규율).
- **캐시 키 확장**: `checkpoint_silence_cut.json` 에 `min_residual` 을 기록하고 프로파일
  이름이 같아도 값이 다르면 재계산한다 — A/B 두 arm 이 같은 output_dir 를 재사용할 때
  stale 방지.
- A/B 는 파이프라인 재실행 없이 잰다 — 도구가 저장된 체크포인트(story 클립·청크 전사·
  후보)에 파라미터만 다시 태워 두 arm 을 대조한다(E14 방식). ⚠ silence_cut 단계만
  본다 — 길이 클램프와의 상호작용은 실런 A/B 몫.
- ⚠ 테스트 함정(기록): `_apply_ab_env` 는 os.environ 을 직접 만지므로 monkeypatch 로
  정리하면 안 된다 — 되돌리기가 중간값을 복원해 다른 테스트를 오염시킨다(실측).
- 회귀 가드: `tests/test_e19_silence_residual.py`(14건 — 회귀 0·기하 수계산·보호 분기
  불변·env/CLI fail-loud·캐시 키·도구 존재).

### E19-7 자막 욕설 마스킹 (2026-08-28)

`app/modules/profanity_mask.py` · `app/data/profanity_mask_ko.json` ·
design 키 `subtitle_profanity_mask`(기본 "off", `--design-subtitle-profanity-mask`).

- **음성은 원음, 자막 텍스트만** 가린다(벤치마크 신병4: 음성 「새끼야」 · 자막 「XX끼야」
  — 제목 "X같은" 자체검열과 같은 플랫폼 노출 안전 규율).
- 사전은 코드가 아니라 데이터 한 곳. 규칙은 **공백 토큰(열) 완전 일치**뿐 — 부분 문자열
  치환 금지(새끼줄·동물 새끼가 깨진다). 활용형은 사전에 열거한다(E13 표기 보정 규율).
- 자리: 자막 신규 생성·캐시 로드 두 경로가 **수렴한 직후, 편집실 자막 오버라이드 앞** —
  사람이 고친 문장은 마스킹이 덮지 않는다. 마스킹되면 `subtitle_segments.json` 도
  재기록한다(화면과 편집실·현지화 자료가 갈리면 안 된다). 전사 원문·TTS cue 텍스트는
  이 길을 안 지나므로 그대로다(소리 원음 유지 계약).
- **멱등** — 마스킹 결과는 사전 키와 다시 안 맞아 캐시 재개가 두 번 지나도 안전하다.
- 기록: 건별 stdout `[자막마스킹]` + run_log `steps[{step:"subtitle_profanity_mask"}]`
  = `{masked, of, details}`. off(기본)면 단계 자체가 없다(회귀 0).
- 회귀 가드: `tests/test_e19_profanity_mask.py`(8건 — 완전 일치·부분 문자열 금지·멱등·
  게이트·배선 자리).

### E19-8 렌더 후 오디오 QA 지표 (2026-08-28)

`app/modules/audio_qa.py` — **렌더를 바꾸지 않는다, 재기만 한다.**

- render 직후 ffmpeg **한 번**(ebur128 → silencedetect 체인, 디코드 1회)으로 통합
  LUFS·LRA·0.3s+ 무음 수·무음 합계를 재서 run_log render 단계 `audio_qa` 에 남긴다.
  임계(−30dB·0.3s)는 벤치마크 분석과 같은 자다 — 바꾸면 실측 수치와 비교가 안 된다.
- 실패는 `audio_qa_error` 로 사유만 — 관측 장치가 본편 발행을 막지 않는다.
- **목표값 판정은 엔진이 하지 않는다**(−13.5 LUFS·무음 ≤2 는 프리셋의 qa 지표) —
  ves evaluate 가 읽고 대시보드에 띄우는 것은 별건.
- 게이트 없음(전 렌더 공통 관측) — run_log 키 추가는 가산적이고 비용은 디코드 1회다.
- 회귀 가드: `tests/test_e19_audio_qa.py`(6건 — 파서 순수·실 ffmpeg 실측(합성 사인+
  무음)·실패 무해·임계 고정·배선).

### E19-9 단일 줄 자막 (2026-08-28, 사용자 지시)

`subtitle.split_segments_single_line` · 톤 프로파일 `subtitle` 절
(`{single_line, max_line_chars}`). 지시: "자막이 한 줄이상 넘어가면 안돼. 오디오
싱크에 맞게 한 줄 씩 나오게 해줘. tts도 마찬가지고, 단어 중간에 끊기면 안돼."

- **게이트는 톤 프로파일 subtitle 절 하나다**(새 CLI 플래그 없음 — E19-1 규약).
  절이 없는 톤·톤 미지정은 종전 2줄 랩 그대로(회귀 0). ⚠ `merge_subtitle_segments`
  는 안 건드렸다(E14 경고 — 전 채널 공유 경로).
- **화면 분할이 아니라 시간축 분할이다.** max 를 넘는 세그먼트를 어절(공백) 경계에서
  잘라 조각마다 글자 수 비례로 시간을 배분한다 — 한 화면에 늘 한 줄, 줄이 오디오
  진행을 따라 돈다. 단어 중간 절단 없음, max 를 넘는 **단일 어절은 통째로**(자르는
  것보다 넘치는 게 낫다는 지시). 경계 단조·양 끝 보존·부가 속성(style·
  low_confidence) 조각마다 복사·멱등.
- **적용은 6곳**: 대사 수렴 지점(E19-7 마스킹 **뒤** — 마스킹 사전은 온전한 문장
  기준 · 편집실 오버라이드 **앞** — 사람이 정한 줄은 사람 것) + **E15 자막 강조 적용
  직후 재분할** + TTS 정본 2분기 + variant 대사·TTS. variant 는 전사에서 새로
  만들므로 정본 분할이 승계 안 된다.
  정본(`subtitle_segments.json`)도 다시 쓴다 — 편집실·현지화가 화면과 같은 줄을 본다.
- E15 강조로 키운 줄(style.size)은 `build_ass_from_segments` 의 size_ratio 와 같은
  수식으로 그 줄만 max 를 줄인다 — 갈리면 강조 줄만 화면에서 두 줄로 넘친다.
  ⚠ 강조는 수렴 분할 **뒤**([style] 블록)에 얹히므로 수렴 지점 분할만으로는 못 잡는다
  — 김부장 v3 풀 루프 실측에서 13자 강조 줄(size 78)이 ASS 조립의 size_ratio 재계산
  (max 11자)에서 \N 두 줄로 나갔고, 강조 적용 직후 재분할(멱등)이 그 수정이다.
- TTS 는 cue mp3 가 한 파일이라 단어별 실측 타이밍이 없다 — 글자 수 비례가 근사다.
- ⚠ E14 노출 하한(merge 단계)보다 뒤라 조각이 0.4초 아래로 갈 수 있다 — 조각은
  문장을 잇는 회전이라 하한을 다시 걸면 오디오 싱크가 깨진다(의도된 예외).
- run_log: `steps[{step:"subtitle_single_line"}]` (before/after/max_line_chars).
- 회귀 가드: `tests/test_e19_single_line.py`(17건).

## E20 — 페이싱·구성 수정 묶음 (2026-08-28, 김부장 v3 프레임 해부 후속)

지시: "영상 길이 상한을 2분으로 변경해주고, 고치는 작업 순서대로 해줘".
근거 실측(v3): 무발화 10.5초·발화 커버리지 75%·첫 컷 8.5초·정지 화면 71% + 확장이
만든 0.8초 초과에 제목의 핵심 build 12.9초가 클램프에 통째로 증발.

### E20-⓪ 길이 상한 120초 (사용자 결정)

- `MAX_DURATION_SEC` 기본 60 → **120**(하한 40 유지). env 로 채널별 예외 가능.
- **전 채널 정책 변경이다** — 길이 정책은 전부 `config.min/max_duration_sec` 로
  흐르므로(스토리 프롬프트·BeatTrim·narrative-ext·length-clamp) 기본값 하나로 움직인다.
- 스토리 프롬프트의 40~60 하드코딩을 `{min_duration_sec}`/`{max_duration_sec}`/
  `{ideal_duration_sec}`(범위 중앙값 파생) 포맷 키로 교체 — 상한을 바꿨는데 프롬프트가
  계속 "40~60초"라고 말하면 LLM 은 60초로 낸다.

### E20-A1 length-clamp 비례 트림

- `_fit_storyline_to_duration`: 초과분이 그 build 길이의 **절반 미만**이면 통째 제거
  대신 끝만 잘라 상한에 맞춘다. 절반 이상이면 종전대로 통째 제거(반토막 꽁다리가
  없느니만 못하다 — 86s→60s 류 대폭 단축은 제거가 맞다).

### E20-A2 narrative-ext 무발화 앞 확장 금지 + 예산 통일

- 앞 확장은 확장 창 안 **가장 이른 발화 - 1.0초(리드인)** 까지만. 창에 발화가 없으면
  앞 확장 0(v3: +7.4초 전부 무발화 → 도입 6.1초 침묵). 전사가 없으면 종전 그대로
  (오판 금지). **뒤 확장은 게이트 밖** — 무발화 리액션 컷(엔딩 비트)이 거기 산다.
- 확장 예산 기준을 클램프 상한(`RENDER_SAFETY_MARGIN_SEC` 0.3 차감)과 통일 — 확장이
  만든 초과를 클램프가 되무는 자기충돌 제거.

### E20-A3 청크 오버랩 중복 전사 dedup

- `speech.dedup_overlapping_transcripts` — 청크가 180초씩 겹치게 잘려 겹침 구간
  대사를 양쪽 청크가 다 전사한다(시각 0.x초 차·문장부호 차로 완전 일치 dedup 통과 →
  같은 자막이 화면에 두 벌 겹침). **시간 겹침(짧은 쪽 50%+) + 정규화 텍스트 동일**만
  제거 — 연달아 말한 진짜 반복 대사(시간 비겹침)·교차 대화(텍스트 다름)는 그대로.
- 배선은 청크 전사 병합 직후 한 곳 — 자막·cue 앵커·snap/ext 가 전부 이 목록을 본다.
  건별 stdout `[transcript-dedup]`.

### E20-B1 발화 갭 페이싱 (dB 무음이 아니라 무발화 기준)

`silence_cutter.apply_speech_gap_pacing` · 톤 프로파일 `pacing` 절
(`{max_speech_gap_sec, gap_residual_sec, head_lead_in_sec, tail_hold_sec}`).

- **왜 별도 패스인가**(v3 실측 셋): ① 앰비언스 −16dB 면 dB 무음 0건이라 커터 무력
  ② visual_essential 클립 통째 보존이라 내부 무발화(payoff 1.1·2.3초)가 그대로 나감
  ③ silence_cut 은 snap·ext·gap-fill **앞**이라 확장이 만든 무발화를 못 보고,
  유일한 컷 1.0초는 gap-fill 이 도로 메웠다.
- **자리: gap-fill 뒤 · length-clamp 앞**(조립 마지막) — 되메움 방지 + 클램프가
  조여진 총량을 본다. `_edit_pinned` 는 그 블록째 건너뛴다(사람 고정).
- **커버리지 = 대사 전사 ∪ 계획된 cue 창**(anchor~+duration+0.2 · 클립 머리 -6s
  앵커는 머리로 클램프 — cue-resolve 와 동일) — 내레이션이 살 자리를 자르면 안 된다
  (E19-3 과 한 몸). 내부 무발화 run > max_gap → 가운데 컷·잔여 residual(양쪽 절반)·
  클립 분할. 머리 lead_in·꼬리 tail_hold 까지만 허용(꼬리 여유 = 리액션 컷 자리).
- 전사 없는 클립은 불변(비주얼 비트 — 오판 금지). visual_essential 이라도 **대사가
  있으면** 상한을 받는다(v3 payoff 유형). 0.8초 미만 조각을 만들 컷은 접는다.
- 게이트: 톤 `pacing` 절 하나(없으면 회귀 0). run_log `steps[{step:"speech_gap_pacing"}]`
  + 건별 stdout `[speech-gap]`. 순수 함수.

### E20-B2·B3 스토리 규칙 (프롬프트 지시 — 강제는 A1·B1 이 한다)

- `narration.fill_gaps`(선택 bool): 대사 없는 1.5초+ 틈**마다** cue 배치 — 오디오가
  비지 않게(v3: max_cues 5 중 3개만 쓰고 무발화 10.5초).
- `story.hook_max_sec`(선택 3~20)·`story.hook_title_scene`(선택 bool): 훅 상한 +
  제목이 약속한 장면을 첫 클립으로 + 첫 2초 안에 사건(v3: 도입 설전 28초).
- drama_clip_kr: fill_gaps true · hook_max_sec 8 · hook_title_scene true ·
  pacing {1.2, 0.4, 1.0, 2.5}. 값 정본은 preset.json(교차 테스트).

### E20-B4 발화 커버리지 가드 (v4 실측 후속)

v4 실런이 드러낸 다음 층: 새 예산(120s)에서 스토리가 **대사 없는 액션 구간**을 골라
21.4초 무발화 구멍이 났다. hook 은 40.5초짜리 환각성 전사 1줄("누가 보냈어?" —
0.17자/초)이 전체를 '발화 있음'으로 위장했고, 25초 build 는 전사 0건이라 페이싱의
비주얼 비트 보호로 빠져나갔다. **선택이 원인, 조립은 벨트다.**

- `speech.plausible_speech_intervals` — 길이 >8s + 밀도 <0.8자/초 세그먼트는 발화로
  안 친다(**판정용** — 화면 자막은 불변). 페이싱 커버리지도 이 필터를 지난다.
- `speech.speech_coverage_ratio` + 스토리라인 검증 루프의 커버리지 가드 — 톤
  `pacing.min_speech_coverage`(선택, 0.1~0.95 · drama_clip_kr 0.55) 미만 스토리라인은
  SKIP + 다음 후보(길이 검증과 같은 규약). 전사가 없으면 판정하지 않는다(오판 금지).
- **전량 탈락 폴백(v5 실측 후속)**: 세 스토리라인이 전부 미달(4%·43%·48%)이면
  selected_storyline 폴백이 가드를 **우회해** 최저(4%) 구성을 되살렸다 — 이제 전량
  탈락 시 탈락분 중 **최고 커버리지**를 경고 + run_log `story_coverage_fallback` 과
  함께 쓴다(cue 정규화를 가드 앞으로 올려 탈락분의 cue 도 보존). 1차 방어로 톤
  story 블록에 재료 제한 지시("대사가 거의 없는 구간을 주 재료로 쓰지 마라")를 얹었다
  — 프롬프트가 먼저 줄이고, 가드가 거르고, 폴백이 최선을 잡고, 페이싱이 벨트다.

### E20-C1 번인 자막 광범위 경고

- E18-6 창별 회피가 대사 트랙 20%+·5줄+ 움직이면 stdout ⚠ + run_log
  `pervasive_burned_hint` — 회피는 겹침만 피하지 **이중 표기**(소스 번인 + 우리 자막)
  자체는 못 푼다. 자막을 끄는 결정은 사람 몫(채널 `subtitles:false`).

### 미이행(별건 — 실렌더 튜닝 필요)

- C2 저조도 보정(eq): 화면 픽셀이 바뀌는 전 채널 변경이라 실렌더 A/B 선행.
- C3 라벨-컷 동기: 장면 전환 검출 인프라가 엔진에 없다(scdet 도입 별건).

회귀 가드: `tests/test_e20_pacing_fixes.py`(15건) · `tests/test_e20_speech_gap.py`(17건)
· `tests/test_e20_story_rules.py`(8건). 전체 1438 통과.
## 제목 두 줄 형식 고정 · 굵게 금지 (E21, 2026-08-25)

> ⚠ 번호 주의: 이 작업은 **8/25 에 만들어졌지만 8/30 에야 푸시됐다**(로컬에만 있던 커밋).
> 그 사이 다른 세션이 E19·E20 을 가져가서, 원래 붙였던 E19 를 **E21 로 바꿔** 실었다.
> 날짜는 E19·E20 보다 앞서지만 번호는 뒤다 — 코드 주석의 E21 도 전부 이 절을 가리킨다.

`style_compose` 의 `STYLE_DESIGN_IGNORED`·`MAX_TITLE_LINE_CHARS`·`split_title_lines`·
`join_title_lines`·`fill_title_gaps`·`title_segments_from_anchors` ·
`pipeline` 의 `[style]` 4) 블록 · `gemini_client.STYLE_COMPOSITION_PROMPT` ·
`localize/style_texts.style_plan_strings`.

**E18 이 나간 뒤에 만들어진 편**(유재석 캠프 8회차 등)의 완성본을 보고 온 지시 2건이다 —
E18-1(빈틈 메우기)·E18-3(회전 차단)은 이미 화면에 반영된 상태였고, 그 화면을 보고 나온
후속 교정이다.

> "1. 제목은 굵게 하기 금지(볼드체 금지) — 애초에 굵은 폰트를 써서 볼드체로 바꾸면 글씨가
> 뭉개져서 안 보임. 2. 제목이 시간대별로 바뀌는 것도 좋은데, 대신 다른 쇼츠들처럼 두 줄
> 형식은 유지하고 위에 큰 제목(불변 제목, 색상 넣고 그 짧은 구절로도 완결성 있게, 고정)은
> 넣고 아랫줄만 바뀌게 하고 싶어. 저렇게 하니까 쇼츠 자체가 눈에 안 띄는 것 같아."

### E21-1 제목 굵게는 AI 에게 닫혔다

- `title_bold`·`title_bold2` 를 `STYLE_DESIGN_ALLOWED` 에서 빼 `STYLE_DESIGN_IGNORED` 로
  옮겼다 — **닫는 방식은 E18-3(회전)과 같은 드롭+메모**다. 모르는 키로 두면 플랜 **전체**가
  거절돼 효과 텍스트·제목 창까지 같이 날아간다.
- 이유는 폰트다: 번들 제목 폰트(Jalnan)는 원래 극굵이라 획을 더 얹으면 글자 속이 메워진다
  (렌더러 주석의 2026-08-21 bold.png 실측과 같은 관찰).
- `design_overrides` 는 `title_bolds` 를 **아예 조립하지 않고**, 닫힌 키가 옛 체크포인트에
  남아 있으면 거기서 한 번 더 메모하고 버린다(E18-3 이 회전에 쓴 이중 방어를 목록으로 일반화).
- **AI 산출만 막는다**(사용자 선택) — 채널·편집실의 `--design-title-bold(2)` 는 그대로다.
  어댑터 `CHANNEL_DESIGN_SWITCHES`·대시보드 스위치·DB `v_allowed` 는 손대지 않았다.

### E21-2 윗줄 고정 · 아랫줄만 교체

- **플랜 계약이 바뀌었다**: `title_segments[].text` 는 **아랫줄 한 줄**(개행 금지 ·
  `MAX_TITLE_LINE_CHARS`=20자)이고, 편 전체에서 안 바뀌는 윗줄은 새 키 **`title_fixed`**
  다(한 줄 · 20자 · 창이 없으면 드롭+메모). 윗줄 문구는 **style 단계 AI 가 짓는다**
  (사용자 선택 — 스토리의 `title_line1` 은 '상황 설명'이라 단독으로 완결되지 않는다).
  `title_fixed` 가 없으면 `title_line1` 로 폴백하므로 구 체크포인트도 그대로 돈다.
- 20자는 스토리의 `_enforce_title_line_limit(max_chars=20)`·렌더러
  `split_text_smart(text, 20)` 과 같은 수치다 — 넘기면 줄이 접혀 제목 블록이 3줄이 되고
  글자가 0.85배로 작아진다(2줄 형식이 깨진다).
- 결합은 `title_segments_from_anchors(fixed_line=…)` 가 한다 — 모든 창의 첫 줄이 같아지므로
  자료 모양(`{text,start_sec,end_sec}`)도, 줄별 색·크기(`title_colors`·`title_sizes`)도
  종전 그대로다. **렌더러·E8 검증기는 한 줄도 안 고쳤다.**
- ⚠ 이미 두 줄인 text(E21 이전 체크포인트)는 **그대로 둔다** — 재개는 재검증 없이
  재적용하므로(E15 계약) 윗줄을 또 붙이면 3줄 제목이 나간다.

### E21-3 빈틈은 기본 제목이 아니라 **직전 제목**이 잇는다 (E18-1 교정)

- E18-1 은 창이 못 덮은 시간을 **기본 제목**으로 메웠다. 그런데 기본 제목의 아랫줄은
  스토리가 만든 **결말 후킹**이라, 아직 그 내용이 아닌 앞 구간에 붙으면 어긋난다.
  사용자 지적(유재석 캠프 편): *"앞에는 바비큐 안 좋아하는 내용이라"* — 그 편은 앞 구간이
  '바비큐 안 좋아한다더니', 맛본 뒤가 '고기 앞에서 무너짐'이었다.
- 그래서 `fill_title_gaps` 의 메우는 내용을 바꿨다: **직전 창의 문구가 그 자리를 잇고**,
  맨 앞 틈은 첫 창을 0초로 당기고, 꼬리는 마지막 창이 문다. **다음 문구를 앞당겨 오지
  않는다** — 아직 화면에 안 나온 내용이 미리 새면 안 된다. 직전 문구는 '방금까지 나온
  내용'이라 항상 맞는다.
- E18-1 이 세운 계약(**빈 시간이 없다**)은 그대로다 — 실측한 60~68% 구멍은 여전히 0 이다.
  `base_title` 인자는 **AI 경로 표시**로 남는다(사람 경로는 안 주므로 종전 그대로 —
  사람이 비운 것은 의도다). `MIN_TITLE_GAP_SEC` 은 '메모를 남길 만한 틈인가'의 기준이 됐다.
- 프롬프트도 같이 고쳤다 — 모델이 "뒤에 올 문구는 그 구간에 가서 쓴다"를 알아야 창을
  촘촘히 낸다(검증기만 고치면 계속 같은 산출이 온다는 E17-1 의 교훈).

### E21-4 현지화 — 고정 윗줄도 번역한다

`style_plan_strings` 의 제목 목록은 **0번이 `title_fixed`**, 그 뒤가 시간대별 아랫줄이다.
빼먹으면 JP 편에 한국어 윗줄이 그대로 번인된다. `apply_style_translation` 의 되돌리기가 이
순서와 한 벌이고, 인덱스가 어긋나면 즉시 실패(E16 규율 그대로).
⚠ 이 지점부터 `style_texts.py` 는 vlp 원본과 **의도적으로 다르다**(vlp 는 `title_fixed` 를
모른다). 대조 스크립트는 P8(vlp 동결)로 은퇴했으므로 이 분기는 이제 이 절이 유일한 기록이다.

- 회귀 가드: `tests/test_e15_style_compose.py`(53건 — 굵게 드롭·한 줄 강제·윗줄 고정·
  다음 문구 미리 새지 않음), `tests/test_e18_style_placement.py`(15건 — 빈틈 규칙 재작성),
  `tests/test_localize_style_texts.py`(21건). 전체 1461 통과.

## V3-M0 — 리플레이 하네스 (분포 저울, 2026-08-30)

`app/replay/` + `python -m scripts.replay_harness {fetch|report|diff|golden}`.
발주: v3(4단계 재구성) 병행 구축 전에 **합격 저울부터** — LLM 재실행 없이(비용 0 ·
결정적) 저장 아카이브만으로 구간 분포 지표를 재계산·대조한다. 근거 문서:
`docs/cut-granularity-problem.md`(문제 정의 §3).

- **아카이브 정본은 오케스트레이터 DB `clip_metadata` 표다**(실물 확인 2026-08-30).
  편(clip_id)당 한 행에 checkpoint_story·edit_plan·run_log(요약)가 jsonb 로 통째로
  있고 2026-07-15 부터 전 기간. §3의 "저장된 614편"이 이 표다 — 창 상한을 측정 시점
  (8/24 06:26Z)으로 좁히면 **614편이 그대로 나오고 §3 전 셀이 MATCH** 로 재현된다
  (`report --until 2026-08-24T06:26` 실측 EXPLAIN 0). Storage `ves-runs` 번들
  (`sha256(run_id)[:16]/bundle/`)은 8/13 이후 런에만 있다 — 문장 절단·맥락 확장
  초 단위는 번들 있는 편만 계산되고 리포트가 커버리지를 함께 찍는다.
- **지표 정의 동결**: 원안 = `checkpoint_story.clips`(조립 끝·무음 컷 앞) · 최종 =
  `edit_plan.timeline`. ⚠ raw_response 의 storyline 구조(hook/build/payoff)로 세면
  §3 분포가 재현되지 않는다(실측 — 그건 raw_storyline_n 보조 신호로만).
- ⚠ **clip_metadata.edit_plan 은 편집 재렌더가 덮는다** — '최종' 쪽 수치는 측정 시점
  종속이다(실측: 기준선 대조 가능 48편 중 24편 덮임). 하네스가 editor_baselines 와
  대조해 오염 편수를 리포트에 찍고(`contamination`), AI↔사람 29쌍 표는 절대값이 아니라
  **방향 판정**으로만 대조한다(§3 스스로 "방향만 참고").
- 사람 기준선: `editor_baselines`(AI 원안 스냅샷) + `job_queue` params.edit_overrides.
  AI 쪽 우선순위 baseline → edit_plan 폴백(오염 플래그).
- `fetch` 는 표준 라이브러리 urllib + PostgREST 만 쓴다(이 레포 venv 에 DB 드라이버가
  없다 — 의존 추가 금지 규율). SUPABASE_URL/SERVICE_KEY 없으면 즉시 실패.
  `report`·`diff`·`golden` 은 네트워크 0 · 순수 — 같은 아카이브면 바이트까지 같은 출력
  (결정성 합격 기준, 테스트 고정). 백분위는 보간 없는 `sorted[int(q*(n-1))]` 하나다.
- 로더는 두 레이아웃 자동 판별: fetch 스냅샷(`runs/*.json`) + **job 디렉토리**(엔진
  산출 그대로 — v3 산출도 이 모양이라 `diff --b <v3_dir>` 가 바로 붙는다).
- 골든 케이스(M0 은 인터페이스까지): `golden_cuts/v1` 정답지 채점(IoU 매칭 + 경계
  정밀/재현율). fh_captions 류 자막 파일은 cue 뭉치 근사(derived=true)로 자동 변환 —
  실소재 채점은 M3 에서 소스 시간 컷 리스트 정답지로.
- 회귀 가드: `tests/test_v3_replay_harness.py`(24건 — 지표 정의·결정성·오염 진단·
  Storage 키 규약·CLI 스모크). 네트워크 없이 돈다.

## V3-M1 — 정본 격자(grid) + Stage 1 전체 간단 분석 (2026-08-31)

`app/v3/` · 엔트리 `python -m app.v3`(create_shorts_v3). 기획 정본: 아티팩트
「리캡 쇼츠 기법 이식안」(8/30 v3) §1·§3·부록A. **병행 신규** — 기존 14단계 코드
무변경(공유 모듈 import 재사용만), app/cli.py 도 안 건드렸다(M5 전환 때 합류 별건).
산출은 기존 job 레이아웃(run_log/checkpoint_*)이라 M0 하네스 로더가 그대로 붙는다
(edit_plan 없는 M1 잡은 기록 0건으로 인식 — loader 가 v3 마커로 판별).

- **시간 정본 원칙(933 방어 계승)**: LLM 은 경계를 제안만, 확정 시각은 grid 스냅.
  스냅 어휘 = span 경계 ∪ 장면 전환 ∪ {0, 러닝타임}. 오차 >2s 는 반려·재질의(≤2회),
  소진 시 크게 실패. 인접 구간이 어긋나게 낸 공유 경계는 1s 클러스터 정준화 후
  스냅(각자 스냅이 만드는 인공 빈틈 방지). 커버리지: 러닝타임 = sequences ∪
  exception, 빈틈·겹침 0(eps 0.05 · 끝 잔차 0.5s 관용).
- **grid 재료 4종과 재사용**: ① 전사 단어 — speech 의 whisper 설정 그대로
  (large-v3-turbo·temp 0·vad 500ms, `_get_whisper_model`/`_build_whisper_prompt`
  재사용), 전체 실패 시 600s 창 재시도 + 실패 창은 scene 폴백·run_log 명시.
  ② 장면 전환 — ⚠ 기존 `detect_scenes` 재사용 **안 함**: pyscenedetect 가
  requirements 에 없어 전 환경이 등간격 산술 폴백(픽셀 안 봄)이라 격자 눈금으로
  부적합 — v3 는 ffmpeg `select=gt(scene,0.3)` 하나로 고정(전 환경 동일 산출).
  ③ 무음 — 신규 silencedetect 래퍼(-30dB=audio_qa 값·0.5s=cue 공백 값).
  ④ arousal — 4피처(energy·pitch_var·dynamics·speech_density) 0.5s hop,
  **생성까지만**(가중 소비·±0.5 보정은 M3 — 기획 §9-B).
- **span 재단**: 유성 = `stt_elevenlabs.words_to_segments` **그 함수**를 호출
  (0.5s·종결부호·44자·6.0s — 발주서의 '40자'는 merge config 값, word→cue 정본은
  44, E14 기록). 무성 = 장면 전환 → 무음 경계 → 등분(≤6.0s) 재단, 0.5s 미만
  슬리버는 흡수. time_authority 는 stt|scene 둘뿐.
- **Stage 1**: scan 프록시(360p/1fps 파일)를 Files API 업로드,
  `video_metadata.fps=0.5` + `media_resolution=LOW` + **temperature=0.0**(결정성
  합격 조항 — GeminiConfig 에 온도 필드가 없는 3.x 규약의 의도적 예외, 이 호출
  한정) + Pro 슬롯(영상을 실제로 보는 호출 규칙). 67분 ≈ 26만 토큰 — 2시간+ 소재가
  넘치면 발주서 멈춤 시점 2. chunk: ≤10분 sequence 는 코드가 1개 자동 채움,
  >10분 chunk 부재는 반려 → 소진 시 격자 눈금 등분 폴백(기록). 휴리스틱
  (`detect_exclusion_zones` — 2-zone 모델·dead code 였음) 후보를 프롬프트 힌트로
  주고 모델 확정과의 불일치를 run_log `heuristic_mismatch` 로 남긴다(검수 신호).
- **character_index 병렬 슬롯**: 기획서는 유지·병렬이라 했지만 face_id 모듈은
  014335e(8/25, 사용자 "필요없어")가 지웠다 — 배선만 유지하고 부재를 run_log
  `character_index.status=module_absent` 로 명시. 복원 여부는 사용자 결정 사안
  (M2 characters 교차 검증·M4 크롭 앵커가 소비 예정).
- **스모크 실측(포핸즈 1회 67분, 2026-08-31)**: 단어 2,402(실패 창 0) · 장면컷
  680 · 무음 855 · arousal 8,124 · span 1,872(유성 609) — grid 359s.
  Stage 1 **1차 시도 통과**: sequence 6·chunk 10, 경계 36/36 격자 오차 0,
  커버리지 위반 0, intro [0,43.0]·teaser [4029.25,끝] — 실물 프레임 대조로 둘 다
  오차 ≤2s(43.0 = 고지문→본편 검은 전환 정확히 그 프레임). exception 레이블
  샘플은 이 1편뿐 — 정확도 합격선은 레이블 5~10편 확보 후(멈춤 시점 3 보고).
- 회귀 가드: `tests/test_v3_grid.py`(15건) · `tests/test_v3_stage1.py`(13건) —
  LLM·whisper 없이 돈다(가짜 응답·주입 전사). 로더 v3 인식은
  `tests/test_v3_replay_harness.py` 에 1건 추가.

## V3-M1 후속 — face_id 복원 (레퍼런스-프리, 2026-08-31)

014335e 가 지운 `app/modules/face_id.py` 를 사용자 지시로 복원하되 **죽은 사슬은
되살리지 않았다** — 구판은 TMDb 배우 사진 레퍼런스가 필수였고(한 번도 없었음),
복원판은 레퍼런스 없이 감지 얼굴을 ArcFace 임베딩 greedy 코사인 클러스터링
(`assign_cluster`, threshold 0.55, running-mean)해 익명 라벨 person_N 의 등장
인덱스를 만든다. 레퍼런스가 있으면(선택) 실명 매칭이 우선. 라벨↔실명 매핑은
Stage 2(M2)의 일 — 여기서 하지 않는다.

- 의존은 `requirements-faceid.txt` 별도 파일(deepface·tf-keras) — 본 requirements 에
  넣지 않는다(014335e 가 걷어낸 tensorflow 무게 유지). 미설치 노드는 v3 슬롯이
  `deps_absent` 로 기록하고 본편 진행(조용한 누락 금지).
- ⚠ find_spec 검사는 부족 — deepface 가 있어도 tf-keras 부재면 import 가
  **ValueError** 로 터진다(이 머신 venv 실측). `FaceIdentifier.__init__` 이 실사용
  import 로 검사해 모든 실패를 ImportError 로 정규화한다(deps_absent 계약의 근거).
- TMDb(`tmdb_client.py`)는 복원하지 않았다 — 레퍼런스-프리 설계라 불필요.
- 회귀 가드: `tests/test_v3_face_id.py`(7건 — 클러스터 순수 로직·deps_absent 경로,
  deepface 없이 돈다). `test_v3_stage1.py` 스모크의 module_absent 기대는
  {ok, deps_absent} 계약으로 갱신. 전체 1528 통과.

## V3-M1 후속 — exception 레이블 7편 (2026-08-31)

`tests/data/v3_exception_labels/*.json` — Stage 1 exception 정확도 합격선(경계 오차
≤2s)의 정답지. 프레임 실측(타일 접촉시트 + 경계 이분탐색)으로 제작, 각 경계에 tol
명시. 포지티브 3편(포핸즈 1·2회, 가왕쇼 6화) + 네거티브 4편(신병 EPK·강레오 2·크리스텔).

- **레이블 정책**: exception = 화면의 주 내용이 본편이 아닌 구간(전용 카드·예고 영상).
  본편/예고 화면 **위 글자 오버레이**(스태프롤·협찬·OST 배너)는 exception 이 아니라
  `aux.credit_overlay` 보조 기록 — Stage 1 채점에 미포함.
- 실측이 드러낸 것: 포핸즈 1회 엔딩은 엔드타이틀 카드(4027~4029.5) + 오버레이
  몽타주(~4051.5) + 예고(4051.5~끝)의 3층인데 M1 Stage 1 은 [4029.25,끝] 통째
  teaser 로 냈다 — 채점 정책(카드/오버레이/예고 구분 관용 수준)은 채점기 구현 때 결정.
  가왕쇼는 예고 오디오가 본편 대사처럼 전사에 섞이는 대표 케이스(화면 없이는 경계 불가).

## V3-M2 — chunk_split + Stage 2 청크 분석 (2026-08-31)

`app/v3/chunk_split.py` · `app/v3/chunk_analyze.py` · pipeline `_run_m2`.
어댑터 계약 정본: ai-premiere-pro `orders/v3-m2-adapter-contract.md`(경계면 동결 —
meaning/span 은 내부 표현, v3 는 additive 만). **"영상을 보는" 마지막 단계** —
이후(M3+)는 기록만 소비한다.

- **chunk_split**: Stage 1 경계대로 **원본에서 직접** 480p/10fps 재단.
  ⚠ 기존 `split_video_chunk` 미재사용 사유 — 4fps 프록시 재재단이라 10fps 불가 +
  output seek 라 뒤쪽 청크마다 앞 전체 디코드. 파일명에 경계 포함
  (`chunk_sNN_cNN_<start>-<end>.mp4`) — 번호만 키면 Stage1 재구성 시 옛 재단본을
  조용히 재사용한다(리뷰 ffprobe 재현). exception 은 물리 제거 + 유입 재검증
  (Stage1 커버리지를 믿지 않고 다시 잰다). 매니페스트(`checkpoint_chunk_split`)가
  시각 환산 정본.
- **Stage 2 = span id 묶음**: 모델은 시각을 **아예 출력하지 않는다** — meaning 은
  first_span~last_span(연속 구간 분할: 빈틈·겹침·전량 커버 검증), 확정 시각은 전부
  grid lookup(시각 정합 100% 는 구조·벨트 검증 별도). 반려·재질의 ≤2회, 소진·예외는
  커버리지 표기(다른 chunk 는 계속 — 실패 기록도 청크 캐시에 남겨 재과금 방지,
  재시도는 `--from-step chunk_analyze` 명시로만). span 소속은 **중점 반개구간**
  규칙(≥절반 겹침은 정확히 반 갈리는 동률에서 두 chunk 중복 소속 — 리뷰 재현).
- **검증 3종**: ① 시각 정합 벨트(`verify_time_alignment`) ② 전사 diff — audio_script
  는 전사 정본, 정규화 편집거리(공백 제거) > 0.35 = 각색 → 전사 복원 + 건별 로그
  (소정정은 통과) ③ ArcFace 클러스터 라벨 ↔ 인물명 배정 일관성(경고 집계 — 차단
  아님, 인덱스 부재는 skipped 커버리지).
- **청크 분석 캐시는 상류 지문에 묶인다** — sha1(chunk 계획 + span id/경계). grid/
  stage1 재구성이면 지문 불일치 → 폐기(옛 격자의 meanings 가 새 문서에 접합되던
  리뷰 재현 수정 — E11/E13 사이드카 무효화 규율).
- **스모크 실측(포핸즈 1회 · 청크 3/10 · 2026-08-31)**: 재단 3(계획 10 · exception
  제거 2) · 분석 3/3 성공 · **시각 정합 1,232/1,232 = 100%** · 전사 복원 7/202
  (각색 3.5% — diff 0.4~1.0 실사례) · **반려 루프 실발동**(s1c0 1차: thinking 이
  출력 예산을 먹어 MAX_TOKENS 절단 → JSON 파싱 실패 반려 → 2차 통과. ⚠ span 많은
  청크에서 thinking+출력이 65,536 을 나눠 쓰는 것이 실측된 절단 원인 — 재질의가
  자연 회복시키지만 상시화되면 thinking level 하향이 다음 손잡이).
  결정성 프로브(s1c1 동일 입력 재호출): meaning 이 6→4로 병합 粒度는 흔들리나
  **경계는 전부 격자 유래·재호출 경계 ⊂ 1차 경계** — "변동은 span 격자 안에서만"
  합격 조항 그대로. 비용: 청크 수(무겹침 10)·호출당 토큰이 현행 gemini 단계와
  동급(프롬프트 ~49k 실측) — +50% 멈춤 기준 미해당.
  인물 교차(deepface 설치 후 실측 — 같은 함수로 오프라인 재계산): 3청크 가중
  일관성 **0.427**(배정 931 · 클러스터 60). ⚠ 이 수치는 Stage 2 이름 배정과
  **클러스터 순도의 곱**이다 — 거대 캐치올 클러스터(person_1, n=150~232 · 점유
  0.19~0.53)가 여러 인물을 섞는 것이 주 하락 요인이고 소형 클러스터는 다수 1.0.
  경고 집계 용도로는 성립하나 순도 개선(검출기·임계) 전에는 합격선으로 쓰지 말 것.
- 회귀 가드: `tests/test_v3_stage2.py`(21건 — 분할·검증·복원·교차·캐시 지문·예외
  벨트·stale 재단 차단). 전체 1549 통과.

## V3-M3 — Stage 3 story + 경계면 조립 + 어절 자막 (2026-08-31)

`app/v3/story.py` · `app/v3/assemble.py` · pipeline `_run_m3`. **시각 비접촉 구간의
시작** — 입력은 Stage 1+2 기록과 grid 뿐, 영상은 다시 보지 않는다. 산출은 동결
계약면 그대로: `edit_plan.json`(C1) · `checkpoint_resources`(C2) ·
`subtitle_segments.json`(C6) — 편집실 수정 0 으로 호환.

- **Stage 3 = Flash 텍스트 온리**(모델 정책: Pro 는 영상 분석만). 모델은 **span id
  연속 범위로만** 비트를 편성 — 시각 무출력, 확정 시각은 전부 grid lookup(Stage 2
  와 같은 구조). 반려·재질의 ≤2회, 소진 시 **highlight 코드 폴백**(최소 1개 보장 —
  meaning 통째가 아니라 최고 importance 중심 **코어 런**만 재단: 통째 편성이 87s
  덩어리를 낳고 트림도 imp5 보호에 막히던 드라이런 재현 수정).
- **길이 예산**: 초과분은 비트 가장자리에서만(통삭제 금지 · climax/imp5 보호),
  arousal 은 §9-B 계약대로 **동점 타이브레이커 ±0.5 상한**. TTS 슬롯 ⓐ무성 런 →
  ⓑimp≤3 유성 뮤트(`use_original_audio=false` 클립 분할) → ⓒ창 없으면 드랍+기록,
  충돌 벨트(importance≥4 겹침)는 AssertionError — 조용한 송출 금지.
- **어절 자막**: 2~4어절 · 12자 · 문장부호 종료 · −0.05s 선행 · 0.35s 최소 —
  미달 꼬리는 **이전 라인 병합**(§6 "미달 병합" — "같아?" 류 span 끝 실측 3건).
  cue 합성은 v1 과 같은 `synthesize_tts_with_fit` + **Flash shorten_fn**(없으면
  단순 절단이 문장을 중간에서 잘라먹는 실측 — 5.198s > 창 4.133s 재현 수정).
- **골든 1호**: `tests/data/golden_cuts/fourhands_ep1_recap.json` — 프리미어 수동
  제작(2026-08-30 세션) v2 최종 14세그먼트, **소스 절대초 직접 채록**(derived=false).
  채점은 M0 `score_cuts` 그대로.
- **하네스 접점**: 로더가 v3 checkpoint_story 의 **비트를 원안으로** 읽는다
  (additive `_v3_story_clips`) — v3 편이 "집계 0"으로 빠지던 것 수정, §3 분포
  대조가 v3 에 처음 붙었다.
- **스모크 실측(포핸즈 · 분석 3청크 재료 · 2026-08-31)**: recap_dialogue 8비트
  풀 구조(hook~ending) · 55.267s(예산 내·트림 0) · 라벨 3개 · 내레이션 2+드랍 1
  기록 · 시각 정합 100% · **반려 루프 실발동**(1차 제목 18자, 2차 span 비연속 2건
  → 3차 클린) · cue fit 실측 4.18s/2.95s(창 4.13/3.47 — 초과 0.05s 는 v1 과 같은
  '잘림 감수') · 자막 19줄 위반 0. **비트 구간 중앙 7.364s — 설계 목표(사람 분포
  ~7.5s) 부합**, 클립 10개 중앙 4.8s(뮤트·불연속 분할 후). 결정성 프로브: 재호출도
  8비트·1발 통과·경계 전부 격자 유래, 편성 교집합 7/20 — "변동은 span 격자
  안에서만" 조항 충족(편성 자체는 스토리 자유도).
  ⚠ 골든 대조 matched 2/14 · mean_iou 0.552 — 모델이 **다른 스토리**(강비오
  트라우마 아크)를 골랐다. 같은 재료의 강비호-홍재인 아크(사람 선택)와 소재가
  갈리면 컷 매칭은 구조적으로 낮다 — 채점은 "같은 스토리를 골랐을 때의 컷 정합"
  지표로 읽어야 하고, 합격선 설정은 스토리 선택 문제와 분리해야 한다(발주서대로
  데이터 쌓인 뒤).
- 회귀 가드: `tests/test_v3_stage3.py`(21건 — 검증·예산·슬롯·조립·자막·좌표·
  결정성·골든 접점). 전체 1568 통과.

## V3-M4·M5·M6 + 적대 리뷰 (2026-08-31)

M4 `app/v3/stage4.py`(draft+Stage4 스타일)·`finalize.py`(렌더 어댑터+validate 확장),
M5 `app/v3/overrides.py`(edit_overrides C4 — 스냅 정착·cue 신원 승계) + ves 어댑터
v3 분기(이중 게이트 off 출고), M6 `app/v3/variants.py`(훅 변형)·`feedback.py`+
`scripts/feedback_report.py`(youtube_studio 미러 조인). 발주서 3부는
ai-premiere-pro `orders/v3-m4~m6-*.md`.

- **2-pass 실측(포핸즈)**: draft 480p 19.6s/4.4MB(최종 4.4s/24MB 대비 — 멈춤 ②
  기준 50% 미만이나 시간은 4.5×: draft 는 스타일 재료로만 정당) · style 1발 통과
  (프리셋 diff 0 — 검정 밴드 위 제목이라 화면 무관 판단) · 최종 1080×1920 60.3s ·
  validate hard_fail 없음 · 프레임 QC(글리프 항목 포함) 0건.
- **폰트 이원화**(스모크 두부 실측 2건): drawtext=파일 경로 · ASS=패밀리명.
  렌더는 진입점 dotenv 로드로 FFMPEG_BIN(ffmpeg 7) 결정 — 8.x 는
  -filter_complex_script 거부.
- **적대 리뷰**(find 4관점→반박 검증 워크플로): 확정 17건 전부 수정(6e2e959) —
  핵심: renderer `muted_windows` additive(기본 None=v1 바이트 동일), M4 캐시
  상류 지문 사이드카, to_edited_sec 용도별 반개구간, 슬롯 소스 갭·과잉 뮤트,
  cue 창 물리 트림. 반박 기각 5건 무변경.
- **M6 조인 실측**: 발행 298편 — 전체 69.1% · matchable 74.1%. **95% 미달의
  실체는 조인 로직이 아니라 발행 데이터**: absent 42(깔때기 미유입 — 비공개
  유지·노출 0)·window_miss 30(private→공개 전환 지연)·미러 8일 지연(perf_sync
  점검 신호). per-video 식별 스탬프는 발행 플로우 변경 — 사용자 결정 대기
  (발주서 멈춤 1). 훅 변형 N=2 실측: 본편 불변·훅만 교체·1발 통과.

## V3-M7 — exception 채점기 실측 (2026-08-31)

`app/replay/exception_score.py` · `python -m scripts.exception_score`. 계기:
가왕쇼 6화 실사고(예고 50초 지각 → 쇼츠 엔딩 오염). 정책: 합집합 이분법 ·
miss(오염)>overreach(손실) · 관용 tol+2.0s · aux 중립(명시 zone 우선).

**7편 전 레이블 실채점**(개선판 프롬프트): fail 1 — 가왕쇼뿐.
- 포핸즈1 **IoU 1.0**(프롬프트 1회전이 credit 시작 4027 정확 포착 — miss 2.25→0),
  강레오×2·크리스텔 무오탐, 포핸즈2·신병4 EPK 는 **과잉 2건**(96s·59s — 프레임
  실측으로 모델 귀책 확인: 본편 드라마 장면을 intro/credit 오탐. "이른 쪽 보수
  판정" 지시의 트레이드오프 — 정책 2대로 경고, 차단 아님).
- **가왕쇼 잔존 원인은 프롬프트가 아니라 입력 신호**(스캔 프록시 실물 진단):
  예고 첫 25초가 본편과 픽셀 구분 불가한 풀스크린 하이라이트 — 장식 프레임은
  1766.5부터, 모델 앵커(텍스트 카드)는 1791. 0.5fps·360p 에서 화면 근거 부재.
  → 멈춤 1 보고: **경계 정밀 2-pass**(말미 3분 10fps 재단 + 예고 시작 컷 전용
  질의 + scene cut 스냅)가 다음 발주 재료. 픽스처 2건(tests/test_exception_score)
  이 사고·관용 정책을 박제한다.

## V3-M8 — 불확실성 트리거식 정밀 재관찰 실측 (2026-08-31)

`app/v3/refine.py`(경계 2-pass·zone 실체 검증·부분 표본 재프로브·재타일링) +
`transcribe.retranscribe_gaps`. 에이전트 방식("의심되면 확대해 다시 본다")의 결정적
이식 — 트리거 명시 · **Flash 예산 강제(FLASH_BUDGET=8, 감사 기록)** · scene cut
스냅 · 실패 시 원판정 유지(오염 방지 비대칭).

- **7편 채점표 fail 0 · miss(오염) 전 편 0**: 가왕쇼 teaser 49.5s 지각 → 정밀
  프로브가 1735.5 로 정착(miss 0·IoU 0.907), 포핸즈2 intro 138.5→47.5(부분 표본
  재프로브 — 진짜 경계가 ±90 창 밖일 때의 경로), 신병4 오탐 credit 폐기(실체
  검증 — 표본이 전체를 덮을 때만), 포핸즈1 IoU 1.0 무회귀.
- **공백 재전사**: 트리거는 단어 공백 ≥6s(에너지 트리거는 실측 기각 — 예능 무성
  −26.9dB). 가왕쇼 전편 28창·**364단어 복원(+22%)** — VAD 가 현장음 속 발화를
  삼킨 것(자막 공백 사고 원인). 본전사와 같은 프롬프트 주입(인명 표기 일관).
- **적대 리뷰 확정 11건 전부 수정**: retile 이 해방 구간을 안 덮던 critical(갭필
  +10분 상한 분할+자가 validate_coverage — 위반 시 원판정 폴백), 인접 zone 침범
  기각, 프로브 창 원경계 중심 ±90(큰 zone 축소-only 강제 해소 — 깊은 내부는
  재프로브 몫), 재단 보호, 상한 탈락·재프로브 생략 기록. 기각 3건 무변경.

## V3 템플릿 확장 — 스토리 2종·스타일 프리셋 1종 (2026-08-31, laeebly 벤치마크 후속)

`app/v3/story.py` `STORY_TEMPLATE_SPECS`·`resolve_story_templates` ·
`app/v3/stage4.py` `STYLE_PRESETS`·`get_style_preset` · CLI `--story-templates`·
`--style-preset`. 근거: laeebly 상위작 6편 프레임 해부(뜨비·빡빡이횽·누나스픽 포함,
아티팩트 「laeebly 상위작 해부」).

- **덧붙임만** — v3 는 다른 세션이 활발히 작업 중인 표면이라 기존 동작 무변경이
  합류 조건이다. 미지정이면: 제공 템플릿 = 종전 2종(recap_dialogue·highlight),
  프롬프트 바이트 동일(`{extra_templates}` 빈 문자열), 프리셋 = RECAP_PRESET,
  story 캐시 지문도 그대로(템플릿 키는 지정 시에만 지문에 들어간다).
- **스토리 템플릿**(추가로 열어야 제공된다 — `--story-templates a,b`):
  · `conflict_payoff`(갈등형) — hook=갈등 정점(설명 금지) → escalate → turn(핵심
    대사 통째 보존 = 펀치라인 감속) → payoff → loop_ending(리액션 직후 즉시 컷·
    해소 금지). 쇼츠몽 40.8만·옥상평상 94% 시청·뜨비 리텐션 105.6% 공통 문법.
  · `chemi_observe`(케미 관찰형) — observe_hook(관전 과제) → member_moment(인물
    역할 **별명** 라벨 재사용) → ensemble(전원 피크) → recap_loop(제목 회수 + 즉시
    컷). 누나스픽 도깨비 여행 22.9만·자매 채널 리텐션 110~120% 문법.
  필수 역할은 레지스트리 `required_roles` 가 강제(recap 의 climax 필수를 일반화 —
  반려 문구 형식 동일). 모르는 이름은 즉시 실패.
- **스타일 프리셋** `drama_clip` — 김부장 v1~v6 실측 채널값(1:1 밴드·video_y 450·
  제목 66/84 흰/#FF4632·자막 58·내레이션 #7DE8D8) + 벤치마크 '전제 흰/결론 액센트'
  2줄 문법. 값은 전부 `STYLE_ALLOWED` 범위 안(테스트가 대조). 밴드 기하와
  run_style 이 **같은 프리셋**을 본다(RECAP 하드코딩 잔재 금지 — 테스트 고정).
- 회귀 가드: `tests/test_v3_templates.py`(17건). 전체 1688 통과.

## 편집실 제목이 AI 제목 창을 이긴다 (2026-09-01, 사용자 지시)

`style_compose.title_windows_owner` · pipeline `[style]` 4) 블록.

**사람이 편집실에서 제목을 고쳐도 화면이 한 글자도 안 바뀌었다.** 실사고
(가왕쇼_9bca673b): 세 라운드에 걸쳐 제목을 고쳤는데 `edit_plan.layout.top_title` 만
조용히 갱신되고 완성본은 그대로였다. 원인은 E18-1 의 빈틈 메우기 —

```
title_segments: [ {0.0 ~ 67.365 : "…표심 잡으러 나선 전유진"} ]   ← 완성본 전체 67.37s
top_title:      "전유진 즉석 열창했더니\n표 싹쓸이 해버린 박서진"   ← 상영 0초
```

엔진은 창이 **못 덮은 시간에만** 기본 제목을 쓴다. 창이 100% 를 덮으면 top_title 은
영구히 화면 밖이다. **창을 지우는 것으로도 못 막는다** — `title.segments` 키가 없으면
파이프라인이 checkpoint_style 의 AI 창을 다시 얹기 때문이다(E15 재개 계약).

- 우선순위는 순수 함수 `title_windows_owner` 한 곳이 정한다(if/elif 안에만 있으면
  조건 하나가 바뀔 때 아무도 못 알아챈다 — 실제로 그렇게 새어 나갔다):
  **사람 창 > 사람 제목 > AI 창**. `editor_top_title` 이 비어 있지 않으면 AI 창 전량 폐기.
- 폐기해도 **제목이 사라지지 않는다** — `_title_segments` 가 None 이면 렌더러가 종전
  (pre-E8)대로 `title_text` 를 편 내내 그린다. E18-1 의 '빈 시간 없음' 계약 유지.
- **사람이 창을 함께 보내면 그 창이 이긴다**(종전 계약) — 구간별 제목을 원한 의도까지
  뭉개지 않는다.
- 0053 라운드 승계가 `title` 을 키 단위로 이어 주므로, 한 번 정한 제목은 다음 라운드에도
  계속 이긴다(AI 창이 되살아나지 않는다).
- 기록: run_log style 단계 `title_segments_dropped_by_editor`(폐기 0 이면 키 없음) +
  stdout `[style] 편집실이 제목을 고쳤습니다 — AI 제목 창 N개 폐기`.
- ⚠ **8/23 이전엔 이 경로가 없었다** — AI 제목 창이 나오기 시작한 것이 8/24 다
  (실측: 8/19~8/23 108편 0건 · 8/24 이후 하루 2~6편). "이전에는 제목 수정이 됐다"는
  관찰이 정확하고, 회귀 구간은 8/24~9/01 이다.
- 회귀 가드: `tests/test_e15_style_compose.py` 의 제목 창 주인 절(5건).

## 티키타카 쇼츠 파이프라인 (`app/tikitaka/`, 2026-09-09)

두 지침서(「티키타카 편집점 지침서 V14」·「티키타카 스크립트 리빌딩 프로토콜 v14」)를 이식한 **독립
파이프라인**이다. v1(14단계)·v3 코드와 무관하게 `python -m app.tikitaka` 로만 돈다 — 공유하는 것은
로직 조각뿐(`ffmpeg_utils.find_ffmpeg_command`·`tts.synthesize_tts`·ffmpeg scene 검출 방식·
`config.to_font_family`). 산출은 `outputs_tikitaka/<제목>_<회차>/`(gitignore).

```
python -m app.tikitaka --source <mp4> --title 포핸즈 --episode 1회 --cast "강비호,홍재인" [--version auto|N] [--cut-search agentic|index]
```

단계(체크포인트가 있으면 건너뛴다 · `--redo index,rebuild,table,render`):

| # | 단계 | 산출 | 시각 정본 |
|---|------|------|-----------|
| 0 | probe | `probe.json` · `scan_360p_1fps.mp4` · `audio_16k.wav` | — |
| 1 | transcribe | `transcript.json` — **ElevenLabs Scribe v2**(기본, `--stt whisper` 로 전환) 단어 타임스탬프 + 줄 L-xxx | S 모드 립싱크 |
| 1.5 | polish | `transcript.json` 줄 `text` 교정(+`text_orig`) · `transcript_polish.json` — Gemini 가 10분 오디오를 듣고 잘못 들은 단어만 교체 | 시각은 STT 그대로 |
| 2 | scenecut | `scenecuts.json` (ffmpeg `gt(scene,0.3)` · **480p/10fps 컷 프록시**) | 컷 안전 마진 ±0.1s |
| 3 | index | `index.json` — 10분 창 × Gemini(정적·1fps·LOW): 화자 배정·장면 SC-xxx·순간 S-xxx | 모델은 **클립 기준** MM:SS.ms, 코드가 창 시작을 더한다 |
| 4 | rebuild | `rebuild.json`·`rebuild_versions.md` — 10가지 바이럴 패턴(텍스트 온리) | 모델은 **ID 만**(L-/S-) 가리킨다 |
| 4.5 | verify | `verified_v{n}.json`·`.md` — **추천 버전만** 인덱스 텍스트 + 영상(agentic)으로 확인하며 다시 짬 | 여전히 ID 만 · 실패 시 초안 그대로 |
| 5 | table | `table_v{n}.json`·`master_table_v{n}.md` — N/S/A 행 + 절대 타임코드 | S=단어 −0.05/+0.15 · A=샷 스냅 · N=TTS 실측 길이 |
| 6 | render | `shorts_v{n}.mp4` (1080×1920 · **`fill` 레이아웃**: 5:6 중앙 크롭 1080×1296 y190 · 제목 겹침 · ASS 자막 / `--layout band` = 종전 16:9 밴드) | — |

- **전사 품질(2026-09-10 사용자 지적 → 방법 1+2)**: whisper large-v3-turbo 는 평균 신뢰도 0.88·5.5% 단어 <0.5 였고
  v3 대사에 동음 오인이 있었다(입시장산·찌리니까·목사판·면접반). ② 1단계 기본을 **Scribe v2** 로(레포 `stt_elevenlabs` 의
  `_post_speech_to_text`·`_build_keyterms` 재사용, 10분 창 업로드 + 창 시작 오프셋, p=exp(logprob)). 키는 `ves.env`
  (`load_dotenv_if_any` 가 .env 다음에 읽는다). 키 없으면 즉시 실패 — whisper 로 조용히 안 떨어진다(`--stt whisper` 명시).
  ① 1.5단계 `transcript_polish` — Gemini 가 창 오디오(aac 48k)를 듣고 줄 ID 별 교정만 돌려준다. 가드 `accept_correction`
  (유사도 ≥0.5 · 길이 비 0.6~1.6, 아니면 기각+기록). 단어 시각·word_i 불변 → 립싱크 바인딩은 그대로. `--redo` 는
  `cli.cascade_redo` 로 뒤 단계를 같이 지운다(옛 줄 ID 위에 새 전사가 얹히는 조용한 불일치 방지). 방법 4(Gemini 단독 전사)는
  E2 실측(끝 0.6s 오차·단어 시각 없음)으로 기각.
- **대사 자막은 줄 단위**(2026-09-10): S 행이 같은 화자의 줄 여러 개를 묶으면 60자+ 가 한 자막에 몰려 화면 밖으로 넘쳤다(Scribe 판
  실측). `rows_from_items` 가 줄마다 단어 시각을 `sub_lines` 로 싣고 `render.build_ass` 가 줄마다 자기 시각에 이벤트를 낸다
  (다음 줄 시작까지 유지 · 줄당 22자 랩). 립싱크 구간(행 in/out)은 그대로.
- **여러 편 출력**(2026-09-10): 리빌딩이 `ranking`(조회수 기대 순 10개 번호)을 함께 내고 `--count N` 이 상위 N개를 각각
  확인 패스·테이블·렌더한다(`rebuild.rank_versions` — 유효 번호만·중복 제거·빠진 번호는 추천→번호순 보충). 프롬프트가
  앞쪽 버전끼리 다른 하이라이트를 쓰도록 요구한다. 첫 사용: 지금 불륜이 문제가 아닙니다 1화, 4편.
- **레이아웃 프리셋(2026-09-11, 레퍼런스 youtube.com/shorts/_GTpP-MUizw 실측)**: `render.LAYOUTS`·`compute_layout`.
  `fill`(기본) = 16:9 소스를 좌우 중앙 크롭해 5:6(1080×1296, y 190~1486)으로 세로를 채우고, 제목 2줄(노랑/#FF4632, 잘난체,
  가장 긴 줄이 1000px 안에 들도록 64~104px 자동)을 영상 위쪽 y320 에 겹치고, 대사·내레이션은 영상 안 하단 y1400(62px·16자 랩),
  효과자막은 영상 위 1/3(y≈618·66px), 작품명(`--title`)을 아래 검정 여백 중앙에 둔다. `band` = 종전(회귀 확인용).
  **2026-09-11 교정(사용자 지시 "제목과 영상이 겹치면 안 된다")**: 제목 블록을 위 검정 영역(y200)에, 영상은 y360~1656.
- **5.5 주인물 크롭(`framing.py`, 2026-09-11 사용자 제안 "얼굴 기준 크롭을 Gemini 에게")**: 컷마다 프레임 3장(20·50·80%, 480p)을
  **정적** 호출로 보내 주인물(말하는 사람/카메라가 따르는 인물) 얼굴 중심을 0~1 로 받고, 5:6 크롭의 x 를 잡는다. 인물이 컷 안에서
  40px 이상 움직이면 `crop=x='clip(x0+(x1-x0)*t/dur,…)'` 선형 팬. Haar(`reframe`)가 아닌 이유: 옆얼굴·뒷모습·다중 얼굴에서
  주인공을 못 고른다. ⚠ 실측 함정: 같은 프롬프트에 **픽셀 좌표(196,331)·혼합(x 0.511, y 484)** 이 섞여 와 40% 가 중앙 폴백됐다 —
  `_norm_coord` 가 이미지 크기로 정규화한다(0~1 · 픽셀 · 천분율 모두 수용). 캐시 `framing_v{n}.json`(컷 키), `--no-framing`.
- **2026-09-11 후속 3건(사용자 지적)**: ① 제목·영상 밀착 — `compute_layout(title=…)` 이 제목 블록 높이(줄수×크기×1.08+6)를
  재서 영상을 그 **바로 아래(간격 0)** 에서 시작, 제목 위 여백 140(`TITLE_TOP/GAP/LINE_H/PAD` — 참고 aZEWYEIPnt8: 제목 바닥이
  영상 윗변에 붙고 위에 검정 여백). ⚠ 공식만으로는 둘째 줄 아래 빈 띠가 26~31px 남았다(사용자 스크린샷) —
  `measure_title_ink_bottom` 이 제목을 검은 프레임에 실제로 그려(ffmpeg ass + PIL) 잉크 바닥을 재고 그 +4px 에서 영상을 시작한다.
  ffmpeg 없으면 공식 폴백. 잉크 측정은 `_INK_CACHE` 로 프로세스 안에서 재사용.
  ⚠ 그래도 남던 "제목 아래 검정"의 진짜 원인은 **소스 레터박스**였다 — 「지금 불륜」은 2:1 화면이 16:9 안에 위아래 60px 띠와
  함께 들어 있다(cropdetect 1920×960@60). `probe.detect_active_area` 가 5지점 표본으로 활성 영역을 재 `probe.json.active` 에
  두고, render 가 모든 컷 앞에 `crop=w:h:x:y` 를 붙인다(이후 크롭·프레이밍 좌표는 활성 영역 기준). 포핸즈는 띠 없음. ② 상황별 화면 잡기 — framing 프롬프트에 행 문맥(내레이션/대사/액션 + 컷 설명)을 주고
  `close|two_shot` 를 받는다. two_shot 은 두 얼굴 중점(둘 사이 폭+0.14 가 크롭 폭 0.469 를 넘으면 **주인물 close**).
  ⚠ wide(흐린 배경 위 전체 프레임)는 구현·실측까지 했지만 **사용자 지시로 제외**(참고 쇼츠는 늘 인물을 채운다) — render 의
  split/boxblur/overlay 체인은 `frame_wide` 가 참일 때만 도는 죽은 경로로 남아 있다. ③ 효과자막 정밀
  배치(`effects.py`, 5.6단계) — S 행은 Gemini(텍스트)가 "반응하는 단어"와 start/end 를 고르고 그 단어 시각에(1/30s 격자),
  위치는 그 순간 컷의 얼굴(framing x/y)을 피한다 — 얼굴이 옆 열이면 반대 열 상단 26%, **가운데 열이면 얼굴 아래 62%**(얼굴이
  낮으면 위 22%; 클로즈업에서 상단 26% 가 이마를 덮던 실측 교정). 글자 폭으로 화면 안 클램프. 유지 1.6s(행이 끝나도 —
  마지막 단어 반응형이 0.8s 만 보이던 교정), 겹치면 `resolve_overlaps`(앞 자막 0.6s 보장 후 끊거나 뒤 자막을 미룸).
  캐시 `effects_v{n}.json`, row["effect_plan"]. 판정 실패면 종전 규칙(마지막 줄 시작·중앙). 와이드 렌더는 실프레임으로 확인됨.
  ⚠ 모델이 좌표를 `606/1000` 분수로 적는 경우가 있어 `llm.extract_json` 이 값 자리의 분수를 소수로 복구한다.
- **5.5 후속 — 보이는 얼굴 우선 · 샷별 프레이밍(2026-09-11, 사용자 지적 "v3 19초에 사람이 안 잡힌다")**: 원인은 오버숄더 샷에서
  프레이밍이 **화자의 뒷머리**를 따라간 것. 프롬프트가 주인물을 "말하는 사람"으로 정하게 해 안수정(뒷모습, x 0.16 · face=false)이
  주인물이 됐고, two_shot 은 컷 안 샷 전환(552.8) 뒤 표본에서 상대가 화면 끝(x 0.94)이라 크롭 폭을 넘어 close 로 떨어져 그 뒷머리
  점을 썼다 → 크롭 x 가 0 으로 클램프돼 얼굴이 보이는 박경희(x 0.57)가 밖으로 나갔다. 4편 66컷 중 같은 패턴 3컷. 교정 셋:
  ① 프롬프트 — 주인물은 **얼굴이 보이는 인물**, 화자가 뒷모습이면 얼굴 보이는 상대를 주인물로(partner 에도 face 플래그).
  ② `parse_points` — 표본별로 주인물 face=false 이고 상대 얼굴이 보이면 그 표본은 상대 좌표(`swapped` 기록, close 일 때 적용 —
  two_shot 중점은 뒷머리+상대라도 오버숄더 구도라 그대로). 캐시된 원응답(`raw`+`img`)은 `_reparse_cached` 가 지금 규칙으로 다시
  해석하므로 해석 규칙 수정은 재호출 없이 반영된다. ③ `split_by_shots` — 컷 안 샷 경계(scenecuts, 가장자리 0.5s 제외)로 구간을
  나눠 **샷별로 따로** 판정하고 컷에 `frame_segs`(컷 상대 t0/t1 + x0/x1/y0/y1)로 싣는다. 렌더는 `crop_x_expr_segs`
  (`if(lt(t,t1),…)` 중첩 — 같은 샷 안에서만 팬, 경계에서 점프)을 쓰고, 효과자막 얼굴 회피는 `frame_at(cut, t)` 로 그 시각의 구간
  얼굴을 본다. 캐시 키가 구간 단위라 분할되지 않은 컷은 종전 캐시를 그대로 쓴다. `--redo framing` 은 전 버전 캐시를 지우므로
  특정 버전만 다시 잡을 때는 `framing_v{n}.json`·`effects_v{n}.json`·`shorts_v{n}.mp4` 를 지우고 `--version n` 으로 돈다.
- **5.5 후속 2 — 얼굴 없는 컷의 우선순위(2026-09-11, 사용자 지적 "v3 30~36초에 사람이 안 잡힌다 · 이전 버전이 좋았다")**:
  '보이는 얼굴 우선' 프롬프트로 바꾸자 얼굴이 아무도 안 보이는 컷(617.67~623.63 — 화자 박경희는 멀리 전신, 전경에 초록 옷
  뒷모습)에서 모델이 **전경 뒷모습**(x 0.74)을 골랐다(종전 프롬프트는 멀리 있는 화자 전신 x 0.27 — 사용자가 맞다고 본 쪽).
  프롬프트에 우선순위를 명시했다: ① 얼굴 보이는 인물(화자 우선, 화자가 뒷모습이면 얼굴 보이는 상대) ② 전신·옆모습이 보이는
  인물(카메라가 따라가는/움직이는 인물, 대개 화자) ③ 전경의 큰 뒷모습·실루엣은 마지막. 응답에 표본별 `view`(face|body|back)를
  받고 `parse_points` 가 `VIEW_RANK`(face 2 > body 1 > back 0)로 표본마다 더 잘 보이는 쪽(주인물 vs 상대)을 쓴다 — view 없는
  옛 응답은 face 플래그로(false=back). 특정 버전만 다시 잡을 때는 `framing_v{n}.json` 에서 `ok:false` 항목만 지우면 그 구간만
  재호출된다(나머지 캐시 유지).
- **제작 가이드 · 로고 · 카피(`guide.py`, 2026-09-11 사용자 요청)**: 작품·회차별 지침 파일을 대본을 쓰는 프롬프트에 넣고 일부 키는
  코드가 강제한다. 위치 `guides/tikitaka/<작품명>.md`(+ `<작품명>/<회차>.md`, 회차가 같은 키를 이긴다) — `--title`·`--episode`
  로 자동 탐색, `--guide` 로 명시. 구조 키(줄 맨 앞 `키: 값`): `지양 단어:`(쉼표 구분) · `로고:`(가이드 파일 기준 상대경로) ·
  `카피:` · `카피 위치: 위|아래`. CLI `--logo/--copy/--copy-pos` 가 가이드 키보다 우선.
  · **프롬프트 주입**: `guide_block` 이 REBUILD_PROMPT(입력 절 뒤 `{guide}`)·VERIFY_PROMPT(규칙 5 뒤)에 본문 + 지양 단어 규칙을
    붙인다. 가이드가 없으면 빈 문자열이라 프롬프트 바이트 동일(회귀 0).
  · **기계 벨트**: `validate_versions(avoid=)` 가 지양 단어가 든 **대사 줄(S)은 드롭**(화면 자막에 그대로 나간다) + 제목·내레이션·
    효과자막의 히트를 `guide_hits`/`[가이드 위반]` 으로 표시 → `polish_guide` 가 걸린 문구만 완곡하게 고쳐 쓴다(GUIDE_POLISH_PROMPT ·
    걸리지 않은 문구는 모델이 손대도 무시 · 고친 뒤에도 남으면 `⚠⚠` 기록). 리빌딩 직후와 확인 패스 직후에 돌고, CLI 가 **캐시된
    대본에도** 한 번 더 건다(멱등 — 첫 적용: v7 효과자막 "[콘돔 발견]"). 리빌딩 캐시가 다른 가이드(`guide_sha`)로 만든 것이면
    경고만 하고 벨트로 처리한다(처음부터 반영은 `--redo rebuild`). A 행의 `text`(순간 desc)는 화면에 안 나가므로 대상 아님.
  · **테이블 캐시**: `table_matches_version` 이 제목·행별 문구·효과자막을 대조해 다르면 테이블을 다시 만든다(agentic 컷 캐시는
    `rows_fingerprint` 에 효과자막이 없어 그대로 재사용).
  · **로고**: `render(logo=)` 가 PNG(알파)를 ffmpeg 입력으로 넣고 `overlay` 로 아래 검정 영역에 얹는다(ASS 앞 · 정지 이미지는
    overlay 기본 eof_action=repeat 로 끝까지). 기하는 `render.bottom_stack` — 로고(≤640×170, 비율 유지) 또는 작품명 텍스트(62px)와
    카피(40px·JalnanGothic)를 `BOTTOM_GAP` 16 으로 쌓아 영역 세로 중앙 정렬, `copy_pos` 로 위/아래. 로고가 있으면 작품명 텍스트는
    안 그린다. 미지정이면 종전 `work_y` 그대로. run_log render 단계에 `logo_box`·`copy_text` 기록.
  · 첫 적용: 「지금 불륜이 문제가 아닙니다」 — 지양 단어 콘돔·피임·섹스·성관계, 로고 `guides/tikitaka/…_logo.png`(4055×1279 흰색),
    카피 "풀 영상은 쿠팡플레이에서 시청하세요" 아래.
- **권리사 가이드 PDF 이식 — 활용 불가 구간·해시태그(2026-09-11, 쿠팡플레이 「지금 불륜이 문제가 아닙니다」 가이드)**: PDF 는 텍스트가
  없는 이미지형이라 pymupdf 로 렌더해 읽었다(pypdf 추출 0자). 내용: 제공 회차 1~4, 회차당 10편·총 40편, **수위 높은 씬 활용 불가**,
  회차별 활용 불가 타임코드(2화 호텔·피부과·44:00~엔딩 반전 대사 / 3화 스킨십·미성년자 운전 / 4화 USB 불륜 영상·난투극 반전),
  필수 카피·해시태그. 가이드 파일: 작품 `guides/tikitaka/지금 불륜이 문제가 아닙니다.md` + 회차 `…/1화.md`~`4화.md`.
  · 새 구조 키 **`활용 불가:`**(`MM:SS~MM:SS (사유) / … / MM:SS~엔딩`, "없음" 허용 · `parse_ranges`) 와 **`해시태그:`**.
  · **세 겹 배제**(원본 절대초 · `excluded_ranges`/`in_excluded`): ① `source_script(exclude)` 가 그 구간의 대사 줄·순간을 **아예
    싣지 않는다**(자리에 "(활용 불가 구간 …)" 표시) — 모델이 고를 ID 자체가 없다. ② `validate_versions(exclude)` 가 걸치는 S(줄)·A(순간)
    항목을 드롭+기록. ③ 테이블 — `drop_excluded_sources` 가 agentic·index 후보와 fill 의 인접 샷을 거르고(깎지 않고 버린다 — 경계에서
    잘라 쓰면 앞뒤가 샌다), agentic 프롬프트에 [활용 불가 구간] 을 함께 주며, 마지막에 `cuts_in_excluded` 가 완성 테이블의 모든 컷을
    대조해 하나라도 걸치면 **RuntimeError** 로 멈춘다(권리사 삭제 요청·저작권 문제라 조용한 송출 금지). 확인 패스도 같은 exclude 로
    소스 스크립트·검증을 돈다.
  · 발행 메모 `publish_v{n}.json`(제목·해시태그·카피·설명란 초안)을 렌더 뒤 저장한다.
  · 1화는 타임코드 없음("없음") — 텍스트 규칙(수위·스포일러·지양 단어)만 프롬프트로 간다. 2~4화는 위 표대로 걸린다.
- **제목–영상 간격(2026-09-11 재지적 "너무 붙어서 겹치는 구간이 있는 것처럼 보인다")**: `TITLE_GAP` 0 → **10**(잉크 바닥 +4 와 합쳐 14px).
- **유튜브 UI 안전 영역(2026-09-11, 사용자 스크린샷 "위아래 유튜브 UI 때문에 가려진다")**: 웹 플레이어 상단 ~160px 에 재생·음량·CC·
  전체화면 컨트롤, 하단 ~250px(모바일은 더 큼)에 채널·구독·제목 오버레이가 얹혀 제목 윗줄과 로고가 가려졌다. `render.SAFE_TOP`=200 ·
  `SAFE_BOTTOM`=360 — 제목 블록은 SAFE_TOP 아래에서 시작(`TITLE_TOP = SAFE_TOP`), **밴드 높이 = min(5:6 높이, (H−SAFE_BOTTOM) − 아래
  스택 필요 높이(`stack_need`: 로고≤130 또는 작품명 62 + 카피 48 + 여백) − band_y)** 로 납작해진다(가로 1080 유지 · 비율은 밴드를 따라
  `aspect=(1080/g, band_h/g)`, crop_w 도 그 비율). 실측(제목 2줄 100px + 로고 + 카피): band 432~1334(902px ≈ 1.2:1), 로고 1350~1480,
  카피 중심 1520 — 전부 안전 영역 안. `bottom_stack(bottom=)` 이 캔버스 끝이 아니라 H−SAFE_BOTTOM 을 하단으로 쓴다. `band` 프리셋은
  안전 영역 없음(종전 그대로). 효과자막 cache(`effects_v{n}.json`)는 밴드 기하에 묶여 있으므로 레이아웃을 바꾸면 지워야 한다.
- **배우 이름 표기 · 크리에이터 톤 내레이션(2026-09-11 사용자 요청)**: 가이드 키 **`배우:`**(`박경희=김혜수, 안수정=조여정 …`)가
  극중 이름→배우 이름 매핑이다. 세 곳에 적용: ① 프롬프트(`guide_block` "인물 표기는 배우 이름으로") ② 텍스트 벨트 `rebuild.apply_name_map`
  — 제목·내레이션·효과자막을 긴 이름부터 치환(부분 이름 '경희' 는 안 건드린다), 리빌딩·확인 패스 직후와 CLI 캐시 벨트에서 돈다.
  **대사(S text)·speaker 키는 그대로**(소리와 화자 색 키) ③ 렌더 `build_ass(name_map=)` 가 화자 라벨만 배우 이름으로 그린다.
  ⚠ 벨트가 N 문장을 바꾸면 `rows_fingerprint` 가 달라져 agentic 컷 캐시가 무효화된다(1화 10편 재적용 실측 — 컷 재탐색 10회).
  내레이션 톤은 REBUILD_PROMPT 제3원칙에 "설명만 하는 해설 금지 — 절반은 크리에이터의 리액션·의견(시청자 말 걸기·1인칭 감상·드립·팩폭·
  다음 장면 예고)" 을, VERIFY_PROMPT 규칙 6 에 같은 요구를 넣었다(제3-1원칙 유지 — 화면에 없는 사실 금지). 프롬프트 변경이라 캐시된
  리빌딩(1화)에는 안 붙는다 — `--redo rebuild` 로만 반영. 2화 실행 스크립트 `scratchpad/run_bulryun_ep2.sh <mp4>` (2화 소스는 2026-09-11
  현재 이 컴퓨터에 없음).
- **N 행 컷 0개 폴백(2026-09-11, 2화 v5 실측)**: 마지막 N 행의 문맥 S 행(2372s)이 활용 불가 구간(2382~2492) 직전이라 같은 장면
  **뒤쪽** 인접 샷·index 순간이 전부 배제돼 컷 0개 → `effects.key_of` 가 `cuts[0]` 에서 IndexError 로 실행 전체가 죽었다.
  `fill_n_rows` ③: ①②를 거치고도 비거나 0.8s+ 모자라면 **같은 장면 앞쪽 샷**(`timing.prev_shots_before`) → 장면 밖 뒤 샷 → 장면 밖 앞 샷
  순으로 보충(전부 used·활용 불가 필터를 지난다). 그래도 0개면 `⚠⚠ 컷 0개` 메모를 남기고 효과자막 단계는 그 행을 건너뛴다(죽지 않는다).
  캐시된 테이블은 문구가 같으면 재사용되므로 이런 행이 든 테이블은 `table_v{n}.json` 을 지워 다시 만든다(agentic 캐시는 지문으로 재사용).
- **효과자막 타이밍 규칙 v2 · 산출 태그(2026-09-11, 사용자 지적 "효과 자막 구간이 안 맞는다")**: 실측 원인은 겹침 해소였다 —
  S 행 반응 자막(마지막 단어 끝 ≈ 행 끝)과 바로 뒤 N 행 자막(행 시작)이 0.6s 차로 겹치면 종전 규칙이 **앞 자막을 0.55s 로 잘라**
  [극대노]·[팩트폭행]·[태세전환]이 깜빡였다(1화 v3 7개 중 3개). 지금은 `effects.MIN_SHOW`=1.0 을 모든 자막에 보장하고,
  겹치면 **뒤 자막을 앞 자막이 끝난 뒤로 미룬다**(뒤 창에 1.0s 남을 때 — N 행 자막은 행 안에서 늦게 떠도 된다), 미룰 자리가 없으면 앞을
  끊되 시작을 앞당긴다. 허용 창은 `earliest`(행 시작)~`latest`(`_latest_end`: 다음 행 첫 컷이 이 행 마지막 컷에서 `SAME_SCENE_GAP` 45s
  안이면 같은 장면으로 보고 행 끝 + HOLD, 아니면 행 끝 — 다른 장면으로 자막이 넘어가지 않는다). `fit_span` 이 창 안에서 시작을 당긴다.
  캐시 `effects_v{n}.json` 은 `_rules`(RULES_VERSION) 가 다르면 버린다 — 규칙을 고칠 때 이 숫자를 올릴 것.
  · **`--tag`**: table/effects/shorts/publish/master_table 을 `v{n}_{tag}` 로 따로 만든다(전사·인덱스·대본·확인 패스·agentic 컷 캐시·
    프레이밍 캐시는 공유). 테이블 캐시는 대본 문구 외에 **voice/speed 도 대조**한다(목소리를 바꾸면 TTS 실측 길이가 달라 N 행 컷 스택이
    바뀐다). 첫 사용: 1화 v3·v2·v9·v5 를 `--voice ko_female_high --speed fast --tag fast` 로(Laura · 1.1×).
- **효과자막 규칙 v3·v4(2026-09-11, 사용자 지적 "효과자막이 영상 내용과 약간씩 안 맞는다")**: 실측 원인 셋.
  ① **캐시 키가 행 시작 t0 를 몰랐다** — 배우 표기 벨트가 N 문장을 바꿔 TTS 길이가 달라지자 뒤 행 출력 시각이 밀렸는데 종전 키
  (행·문구·소스 in)는 옛 절대 시각을 재사용해 v3 행8·10 이 +1.4s 늦게 떴다 → 키에 `t0` 추가(RULES_VERSION 3). 전 버전 대조 스크립트
  로 확인: 10편 중 8편에 0.1~1.4s 어긋난 플랜이 있었다. ② 반응 자막이 **다음 행이 새 대사(S)** 여도 HOLD 만큼 넘어가 다른 대사 위에
  남았다 → `_latest_end`: 다음 행이 S 면 `NEXT_LINE_GRACE` 0.3 만, N·A(같은 장면 리액션 컷)면 HOLD, 다른 장면이면 행 끝(v4).
  ③ 반응형 앵커가 단어 **끝**이라 항상 조금 늦게 보였다 → `REACTION_LEAD` 0.15 앞당김. 또 안전 영역 밴드(≈950px)에서 얼굴 가운데
  배치(62%)가 대사 자막 블록과 겹쳐 `place(sub_top=)` 로 대사 블록 윗변(`layout.sub_top` = sub_y − 라벨 38 − 2줄) 위로 올린다.
  규칙을 고칠 때마다 RULES_VERSION 을 올리면 `effects_v{n}*.json` 이 전부 재계산된다(테이블·컷은 그대로).
  v6: 겹침을 풀고도 `MIN_DROP` 0.8s 미만으로만 보일 자막은 **생략**하고(캐시에 None 으로 남아 렌더도 폴백 창으로 안 떨어진다) 남은 자막을
  원래 창으로 되돌려 다시 편다 — 0.3~0.9s 대사 두 개가 연달아 자막을 달면 둘 다 1.0s 를 못 채운다(실측 1화 v2·v10, 2화 v6).
  ⚠ 효과자막 배치는 CLI 가 렌더와 **같은 인자**(logo_size·copy_text)로 `compute_layout` 을 불러야 한다 — 종전엔 로고 없이 계산해 밴드가
  1034 vs 실제 948 로 어긋나 자막이 대사 블록에 걸렸다. `canonical_strategy` 가 모델의 전략 이름 꼬리("(슬로우 번)")를 정본으로 맞춘다.
  ⚠ 남은 어긋남은 **N 행 컷 내용**(agentic 이 고른 리액션 컷이 내레이션과 안 맞는 경우 — v3 행3 "[혈압 상승]" 밑에 조여정·김지훈
  포옹 컷)이지 자막 시각이 아니다 — 컷 탐색 품질은 별건.
- **선형 서사 계열 v11~14 · 재료 구간 `--range`(2026-09-11 사용자 요청)**: 리빌딩이 14버전을 낸다 — 1~10 은 종전 비선형 바이럴, 11~14 는
  원본 순서를 지키는 계열: **11 구간 순차형**(한 구간을 순서대로 압축 · `--seq-hook on|off` 로 첫 3초 콜드오픈 여부, 기본 on) ·
  **12 루프형**(끝이 처음으로 이어짐 — 마지막 항목은 첫 항목 직전 상황이거나 첫 항목이 답이 되는 질문, 결말 미해소) ·
  **13 장면 통째 압축형**(한 장면·한 대화를 처음부터 끝까지, N 최소·S 촘촘) · **14 점층 빌드업형**(작은 균열→폭발, 최고조를 맨 끝에).
  프롬프트 제2원칙에 11~14 예외를 명시. `rebuild.LINEAR_STRATEGIES`/`HOOK_STRATEGIES`.
  · **기계 벨트 `enforce_linear_order`**: 선형 계열은 S/A 원본 시각이 단조 증가해야 한다 — 어긋나면 안정 정렬로 재배열(N 은 바로 뒤 S/A 에
    붙는다), 콜드오픈 첫 항목(hook)과 루프 꼬리(첫 항목 앞으로 되돌아온 마지막 S/A)는 고정. `[순차] 재배열 k항목`·`[루프] 끝→처음` 기록.
    확인 패스 프롬프트에 규칙 7(선형 유지)이 붙고 검증도 같은 벨트를 탄다.
  · **`--range MM:SS~MM:SS[,…]`**: 재료를 그 구간(들)으로 좁힌다 — 여집합(`guide.ranges_complement`)을 활용 불가 구간과 합쳐 **같은 배제
    기계**(소스 스크립트·검증·컷 탐색·테이블 벨트)를 탄다. 산출은 자동 태그 `r0300-0700`(rebuild/verified/agentic raw/table/effects/
    shorts 전부 `_tag`) — 본 리빌딩 캐시를 건드리지 않는다. 한 구간만 골라 짜기 = `--range` 하나, 여러 구간 = 쉼표.
  · `--version 11,12,13,14` 처럼 쉼표로 여러 버전. 기존 잡의 rebuild.json 은 10버전이라 11~14 를 얻으려면 `--redo rebuild`(전 버전 재생성)
    이거나 `--range` 로 별도 리빌딩. 첫 사용: 1화 03:00~07:00 v11~14 — v11 콜드오픈(L-087 "아, 왜 눈물이…") 뒤 223→308s 단조,
    v13·v14 단조 확인. v12 는 모델이 되돌아오지 않고(끝 354s > 처음 181s) 마지막 N 에 **카피 문구를 그대로 읽는** 내레이션을 넣었다 →
    벨트 둘 추가: `loop_tail`(끝이 처음 앞이 아니면 **첫 S 대사 바로 앞 줄**을 꼬리 S 로 부착 — 같은 장면 20s 안·활용 불가 밖·마지막 N 앞에)
    + `validate_versions(copy_text=)` 가 카피 문구를 읽는 N 을 드롭하고 `guide_block` 이 "다시 쓰지 마라" 를 프롬프트에 넣는다.
  · ⚠ 순위 예시 함정(2026-09-11 실측): 출력 스펙에 적어 둔 예시 `ranking: [3, 1, 7, 9, 13, …]` 을 모델이 **글자 그대로 베꼈다**(1화 14버전
    재생성). 예시를 `<… 실제로 비교해 정한다>` 자리표시로 바꿨다 — 구체 숫자 예시는 모델이 복사한다.
  · **재순위 단계 `rebuild.rerank`/`apply_rerank`(2026-09-11)**: 리빌딩 호출의 ranking 은 예시를 베끼고 추천은 늘 v3(예시의 추천 번호)였다 —
    예시 없는 별도 텍스트 호출(RERANK_PROMPT · 14개 대본을 `version_block` 으로 주고 "번호는 의미 없다")로 다시 매긴다. 리빌딩 직후
    자동(basis=draft), 재순위 이전 캐시는 CLI 가 한 번 붙인다(멱등), `--rerank verified` 는 확인 패스 최종본으로 렌더 뒤 다시 매긴다.
    원래 값은 `model_ranking` 에 보존, 버전별 한 줄 평은 `rerank.notes`. 실패하면 종전 값 유지(기록). 1화 14버전 실측: 모델 순위
    [3,1,7,9,13,…](예시 그대로) → 재순위 [1,3,8,7,4,14,2,11,10,13,12,5,6,9] 추천 v1.
  · `--redo` 는 **태그를 안다**: 무태그 실행은 `_v{n}_{tag}` 파일을 보존하고, 태그 실행은 그 태그 파일(+ `rebuild_{tag}.json`)만 지운다 —
    14버전 `--redo rebuild --all-versions`(1화, 2026-09-11) 앞에서 3~7분 데모·fast 판이 딸려 지워지는 것을 막았다.
- **14버전 검수 후속(2026-09-11, 사용자 QA: v1·v3·v4·v11·v9 합격)** — 지적 8건의 원인과 수정:
  ① **자막이 길어 잘림**: `wrap_lines` 는 글자 수(16자)로 접고 2줄을 넘기면 둘째 줄에 몰아넣어, 잘난고딕 62px 에서 최대 1646px(26줄이 1080
     초과)이 나갔다 → `render.fit_subtitle`: **실제 폰트 픽셀 폭**(PIL `getlength`)으로 어절 줄바꿈, 2줄에 안 들어가면 글자 크기를 46px 까지
     2px 씩 줄이고, 그래도 안 되면 3줄. 대사·내레이션 둘 다. `\\fs` 태그로 줄별 크기.
  ② **효과자막이 느리다**(v3 12초): 앵커가 단어 **끝**이었다 → 단어 **시작**(입에서 나오는 순간)으로. TIMING_PROMPT 는 "터져야 하는 순간의
     단어(펀치라인 첫 단어)"를 고르게 하고 `at` 필드를 없앴다. RULES_VERSION 7.
  ③ **중복 구간**(v14 행4·5, v2 행15·16): A 현장음이 S 대사와 같은 원본 구간 → `validate_versions` 가 대사 구간과 겹치는 A 항목을 드롭
     (`[중복]`), `apply_agentic_a_rows` 는 대사 행과 겹치는 제안을 거절(인덱스 추정 유지). ⚠ 행을 지우거나 재번호하면 안 된다 — 테이블 캐시
     대조(행↔항목 1:1)와 소스 키(행 번호)가 깨진다. 그래서 대본 단계에서 뺀다.
  ④ **화자 오표기**(v13 47초 "조심 좀 하지." 임재홍→실제 박경희): 인덱스 창 분석의 화자 배정 오류. 확인 패스가 영상을 보므로 `speaker_fixes`
     (줄 ID → 이름)를 출력하게 하고 `apply_speaker_fixes` 가 등장인물 목록에 있는 이름만 transcript.json·index.json 에 반영한다(파일 저장 —
     이후 모든 버전에 적용). L-606 은 사용자 확인으로 직접 고쳤다.
  ⑤ **구성 이상**(v3 32초: 내레이션 "무시하기로 합의!" 뒤에 그 내용의 대사 · 0.8s 조각 "싹 무시해."): VERIFY 규칙 7(뒤 대사 선요약 금지) ·
     9(1.2s 미만 조각은 묶거나 뺌, 단독 훅은 예외). ⑥ **제목 과장**(v10 "…전말"): 규칙 8(제목이 약속한 전말·이유·정체·결말이 대본에
     있어야, 없으면 제목을 고친다). ⑦ **루프형 34초**(v12 "그리고 다시—" + 맥락 없는 꼬리): 패턴 12 문구 — 꼬리는 첫 대사를 직접 유발하는
     바로 앞 대사, 혼자 들어도 뜻이 통하는 말, 메타 내레이션 금지·S 로 끝. ⑧ v6(내용 불일치)은 재순위 13위 — 재확인만.
  재적용: v3·v6·v10·v12·v14·v2 확인 패스 재실행(캐시 삭제), v13 테이블 재생성, 14편 재렌더 + `--rerank verified`. 원본은 `keep_r9/`.
  실측: 자막 폭 초과 0/14편, A/S 중복 0(v11 의 행1↔행14 는 콜드오픈 재사용 — 의도), v3 는 "싹 무시해" 조각이 앞 줄과 묶이고 선요약
  내레이션이 리액션형으로 교체, v10 제목에서 "전말" 제거. ⚠ 루프형(v12)은 확인 패스가 "루프 유지" 메모만 보고 초안의 "그리고 다시—"
  + 조각 꼬리를 그대로 뒀다 → verify strategy_note 에 루프 조건(직접 유발하는 앞 대사·단독 의미·메타 내레이션 금지·S 로 끝)을 명시하고
  `_is_meta_loop_narration` 벨트("그리고 다시—" 류 N 드롭)를 추가해 v12 만 재확인.
- **3.5 목소리 기준 화자 대조 `voice_check.py` · 장면 안 순서 벨트(2026-09-11, 사용자 지적 v1 "자막-배우 매핑 오류 · 32초에 마지막 장면")**:
  · 원인 ①: 인덱스(10분 창·1fps)가 폐건물 장면(SC-032) 5줄을 통째로 안수정으로 적었다(실제 박경희 — 긴 웨이브 머리·"남편 잡으러" 서사).
    해결: 같은 16k 오디오를 Scribe **diarize=true 로 파일 전체 한 요청**(`diarize_windows(window=None)`, 100MB·1분, 캐시 `diarize_windows/
    full.json`) → 줄마다 겹침 다수 목소리 ID(`assign_voices`) → 클러스터별 인덱스 이름 다수결로 교정(`speaker_consistency`). 규칙: 미상 제외
    다수 ≥ 0.7 · 이름 붙은 줄 ≥ 5 · 고칠 줄의 이름 비율 ≤ 0.25 · **그 이름의 홈 클러스터가 따로 있을 때만**(화자 분리가 그 목소리를 구분한다는
    증거 — 홈 없는 조연(주민서·하실장)은 합쳐졌을 수 있어 보류) · 미상은 조건만 맞으면 이름 부여. ⚠ 10분 창별 diarize 는 ID 가 창마다 새로
    매겨져 창 안 다수결이 인덱스 오류에 진다(창4: 박경희 9 vs 오표기 21) — 반드시 전체 한 요청. 실측 1화: 전역 화자 11 · 교정 177줄
    (미상→이름 77 · 안수정→박경희 40 · 부부 대사 뒤바뀜 23 · 강태한→임재홍 12). 교정은 transcript.json(`speaker_index` 에 원값)·index.json 에
    저장, 테이블 캐시는 S 행 화자가 현재 줄 화자와 다르면 재생성(`table_matches_version(lines_by_id)`). `--no-voice-check` 로 끔.
    `stt_elevenlabs.build_form_fields(diarize=)` 는 additive(기본 false — 자막 경로 회귀 0).
  · 원인 ②: 결말 선공개형이 **한 장면 안에서** 순서를 섞었다(피 → "살인 현장" 내레이션 → "내가 잘못했네" → 쿵). `rebuild.enforce_scene_order`:
    비선형 전략도 연속 S/A 가 같은 index 장면이면 그 묶음 안을 시간순 안정 정렬(N 은 다음 S/A 에 붙음), 첫 항목(훅)과 장면을 넘는 배치는
    그대로. 검증 단계 + 캐시된 버전 후처리(`apply_scene_order_version`, CLI 멱등). ⚠ 블록 안 **N 은 직전 S/A 뒤**에 붙인다(반응) —
    처음엔 다음 S/A 앞에 붙였더니 v1 의 "살인 현장이었다니" 내레이션이 피 웅덩이 앞으로 밀려 반전을 먼저 말했다. 검증 규칙을 고친 뒤
    재호출 없이 다시 적용하려면 `verify.finalize_from_raw`(캐시된 verify_raw 로 후처리만 재실행)를 쓴다 — 확인 패스 재호출(편당 10~26만
    토큰)을 아낀다. 후처리 결과가 이미 verified 파일에 저장돼 있으면 벨트는 순서가 단조라 다시 안 움직이므로 원응답에서 다시 만들어야 한다.
- **검은 화면 위 컷 · 컷 소스 드리프트(2026-09-12, 사용자 지적 "v1 35초에 검은 화면이 있어서 tts 와 안 맞아")**: 1화 v1 14행(내레이션
  "불륜 현장이 아니라 살인 현장이었다니") 첫 컷 1.7s 와 2행 둘째 컷 1.0s 가 장면 전환 페이드(원본 2927.5~2930.8) 위에 나갔다. 기전 둘:
  ① 어두운 밤 장면은 프레임 차가 작아 scene score 경계가 2885~2930.8 사이에 **하나도 없었다** — 코드가 45s 를 한 샷으로 보고 agentic 제안
  (48:08.5~48:11)의 가용 창을 2930.7 까지 늘렸다. ② `trim_source` 가 다른 행이 쓴 구간을 피하며 남는 조각 중 **긴 쪽**을 고르다 제안에서
  40s 떨어진 검은 꼬리(2928.5~)까지 밀렸다(설명 "핸드폰 쥐고 둘러봄"은 그대로인데 화면은 검정). 교정 셋:
  · `scenecut.detect_black_spans` — 10fps 컷 프록시에 `blackdetect=d=0.3:pix_th=0.04:pic_th=0.98` 한 번(⚠ 0.10 은 어두운 밤 차 안 장면을 검정으로
    오탐 — 2026-09-13 3화 v9 실측, 0.04 로 낮춤: 페이드·오프닝은 그대로 잡힌다), 캐시 `blackspans.json`(소스 이름·
    파라미터 키). 실패는 빈 목록 + 로그(안전장치가 본편을 막지 않는다). 1화 실측 20구간(오프닝 8s·페이드 3~4s·엔딩 크레딧), 2화 22구간.
  · `table.carve_black` — N/A 컷 후보(agentic·index·인접 샷 전부, `fill_n_rows._stack_dedup` 입구)에서 검은 구간 ±0.2s 를 **깎는다**
    (활용 불가와 달리 버리지 않는다 — 검정은 화면 성질이라 앞뒤가 새지 않는다). agentic A 제안이 검정 0.3s+ 를 품으면 인덱스 추정 유지.
    S 행(립싱크)은 대상 아님 — 페이드 위 목소리는 소스의 진실.
  · `trim_source` — 남는 조각은 **제안 구간(avail_in~prop_out)과 겹치는 쪽** 우선, 같으면 긴 쪽(prop_out 없는 인접 샷은 종전 그대로).
    prop_out 이 있는데 조각 시작이 제안 끝 + `MAX_DRIFT_SEC` 3.0 을 넘으면 그 소스는 버린다(호출자가 다른 후보·인접 샷으로 채운다).
    깎은 소스도 `prop_out` 을 승계한다(종전엔 잃어서 두 번째 깎기가 제안을 몰랐다).
  · 벨트 `cuts_in_black`(N 행, 겹침 > 0.3s → RuntimeError — carve 가 막았어야 하므로 걸리면 버그 · A 행은 메모만) + 캐시된 테이블도
    같은 검사로 재생성(`build_table(black=)`, CLI 가 `detect_black_spans` 결과를 넘긴다). 1화 14편 캐시 대조: v1 만 걸렸다(v5_fast A 행 0.3s
    암전 플래시는 소스 연출 — 임계 아래).
  ⚠ 근본 원인(어두운 장면의 샷 경계 미검출)은 안 고쳤다 — scene 임계를 낮추면 `scenecuts.json` 이 통째로 바뀌어 전 버전의 컷 스냅이 움직인다.
  검은 구간 깎기 + 드리프트 상한이 그 구멍을 좁힌다. 회귀 가드: `tests/test_tikitaka_stages.py` 검은 화면 절(5건).
  ⚠ **드리프트는 검정보다 넓은 결함이다** — v1 재생성에서 4행(내레이션 "국민 부부에게 닥친 불륜 찌라시")도 바뀌었다: 제안 38:48(김혜수 거울
  앞 핸드폰)이 39:28.9(김지훈 침대) 로 40s 밀려 있었는데 검정이 아니라 아무 벨트에도 안 걸렸다. 캐시된 테이블 전수 대조(agentic N 컷 시작 −
  제안 끝 > 3s): **1화 14편 중 13편·2화 10편 중 8편**에 드리프트 컷이 있다(최대 +138s v4 행11 · +114s 2화 v6 행6). 이 컷들은 설명("혈압 상승"
  리액션)과 화면(포옹)이 다른 바로 그 유형이다. 캐시된 테이블은 자동 재생성되지 않는다 — 고치려면 `table_v{n}.json` 을 지우고 `--version n`
  (agentic 제안 캐시는 지문이 같아 호출 0 · 새 컷의 프레이밍·효과자막만 재호출). **사용자 승인으로 전량 재생성했다(2026-09-12)**: 1화 12편
  (v3 는 드리프트 없음) + 2화 8편(`--no-voice-check` — 2화는 목소리 대조를 아직 안 탔다), 편당 ~30s, 원본은 `keep_r11/`·2화 `keep_drift/`.
  재감사(`scratchpad/v1_black/audit.py`): 24편 전부 드리프트 0·검정 0. 부수 효과 — 2화 v3(SC-010)·v4(SC-003)는 장면 안 순서 벨트가
  처음 적용돼 2항목씩 재배열됐다. 같은 지시로 1화 v10 은 마지막 A 항목(S-062, 40.19s 이후)을 대본에서 빼 40.2s 로 끝난다
  (`verified_v10.json` items 11개 · issues 에 사유).
- **3.7 작품 이해(digest) · 인물 대조 · 같은 샷 회피(2026-09-12, 사용자 지적 2화 "내용·제목이 이상하고 중복 영상이 계속 나온다 — 전체를 관통하는
  내용과 인물이 왜 그렇게 행동하는지 파악하고 대본을 짜야지")**: 진단 — ① 리빌딩·확인 패스 프롬프트에는 장면별 60자 요약과 대사만 있었다. v10 은
  "의뢰 → 납치 → 사고" 인과를 지어냈고, v7 은 초안(김재철)이 맞았는데 **확인 패스가 얼굴 인상으로 김지훈으로 바꿔 적었다**. ② 10편 전부 N 행 컷이
  옆 대사 행과 **같은 샷**(편당 2~14곳)이라 같은 프레이밍이 두 번 나왔고, A 행이 S 행과 통째로 겹친 편도 6편(2화 verified 는 A/S 벨트 이전 산출).
  ③ 화자 '미상' 줄이 자막 라벨로 찍혔다. 수정:
  · `digest.py` — index(+voice_check) 뒤 **3.7단계**: 전체 소스 스크립트(활용 불가 구간 포함 — 스포일러를 알아야 지킨다) + 창별 줄거리를 주고
    logline·기승전결·인물(역할·원함·왜)·관계·줄기(장면→왜→결과)·장면별 동기·전환점·훅 후보·주의(닮은 인물 구분·말하면 안 되는 반전)를 JSON 으로.
    캐시 `digest.json`(sha = 장면·순간·줄별 화자/텍스트·가이드 — 화자 교정 뒤 자동 갱신) · `digest.md`. `digest_block` 이 REBUILD_PROMPT(입력 절 뒤
    `{digest}`)·VERIFY_PROMPT(규칙 13 뒤)에 같은 절을 넣는다 — 없으면 빈 문자열(회귀 0). `--no-digest` · `--redo digest`(rebuild 이하 연쇄) · `--until digest`.
    프롬프트 규칙: "먼저 이해, 그 다음 대본" — 인과·관계는 문서에 있는 것만, 인물 식별은 화자 표기·관계표, 얼굴 인상으로 바꾸지 마라(verify 규칙 13).
  · `rebuild.character_context_flags` + `polish_character_names` — 내레이션이 부르는 인물이 **앞뒤 S/A 의 화자·행위자·장면 등장 인물**에 없으면 `[인물 의심]`
    → 그 문장만 고쳐 쓰는 텍스트 호출(확실치 않으면 관계·역할로 대체). 리빌딩·확인 패스 직후 + CLI 캐시 벨트(멱등). 배우 이름 매핑도 인식.
  · `rebuild.promo_like` — 카피의 "<플랫폼>에서" 어절 + 시청 유도 어휘가 든 N 은 카피 벨트가 드롭(v8 "…쿠팡플레이에서 확인하세요!").
  · `table.fill_n_rows` **같은 샷 회피** — 회피 대상은 **이미 보여준 샷**(출력 순서상 앞 행의 S/A 컷 + 앞서 채운 N 컷, `shot_index`)과 **되감기**
    (뒤 대사 행과 같은 샷인데 그 대사 **뒤** 구간). 뒤 대사와 같은 샷이라도 그 대사보다 앞선 구간은 연속 리드인(통화 중 → 대사)이라 허용한다
    (`_is_same_shot`, 2026-09-13 v8 실측 — 처음엔 뒤 행 샷까지 피해서 옥상 통화 컷이 밀리고 문 앞 김지훈 샷이 붙었다). 못 채울 때만 재사용 + 기록.
  · `table.trim_a_rows_off_dialogue` — A 컷이 S 컷과 겹치면 겹치는 쪽을 잘라낸다(남는 조각 ≥0.8s · 행은 안 지운다), 못 자르면 ⚠ 기록.
  · `render.UNKNOWN_SPEAKERS` — 화자 미상 줄은 라벨 줄을 아예 안 그린다.
  · **리드인 장면 우선(2026-09-13, 2화 v8 실측)**: 같은 샷 회피가 "옥상에서 전화로 따지는" 내레이션에 집 계단 컷(다른 장면·새 샷)을 붙였다 — 옥상 컷은
    다음 대사와 같은 샷이라 밀렸다. `fill_n_rows` 후보 우선순위 = **리드인 장면**(`lead_scene_for_row`: 다음 S/A 의 장면, 없으면 앞 S/A 의 장면) 안의
    새 샷 → 같은 샷 → 같은 장면 인접 샷 보충(①②) → 그래도 모자랄 때만 ③ 폴백에서 다른 장면(`allow_out=True`). 장면이 맞는 것이 샷이 다른 것보다 먼저다.
  · S 항목 `label_suffix`(예: "(전화)") — 화면에 없는 목소리(전화 상대)를 자막 라벨에 표시한다(`rows_from_items` → `build_ass`). 2화 v8 11행
    "김재철 (전화)" — 통화 장면에서 옆에 선 김지훈을 김재철로 오인하는 것을 막는다(사용자 지적). 사람이 verified_v{n}.json 에 적는다.
  회귀 가드: `tests/test_tikitaka_stages.py` digest·인물·같은 샷 절(7건). 2화는 목소리 대조 + digest + `--redo rebuild` 로 전량 재생성(keep_r2 백업).
  ⚠ **권리사 타임코드는 근사치다**(2026-09-13 실측): 2화 가이드의 "44:00~엔딩(민서 '지금 다시 가면 그 아저씨…')" 대사가 실제로는 43:33(L-784)에 있어
  배제 기계(구간 기준)를 통과했고 첫 재생성의 추천 v5 초안이 그 대사를 썼다(확인 패스는 이해 문서의 '주의' 덕에 스스로 뺐다). 가이드가 **대사를 인용**해
  막는 구간은 전사에서 그 대사의 위치를 찾아 장면 시작(SC-038 43:14)까지 구간을 앞당겨 적는다 — `guides/…/2화.md` 43:14~엔딩.
- **적응형 샷 경계 임계(2026-09-13, 3화 실측)**: `scenecut.detect_scene_cuts` 가 요청 임계 0.3 에서 **분당 컷 수 < 4** 면 0.2 → 0.15 → 0.1 로 낮춰
  다시 잰다(`pick_threshold` 순수 함수, `scenecuts.json` 에 `threshold`(실제)·`requested`·`trials`). 실측: 이 작품은 어두운 그레이딩 + 레터박스라
  0.3 에서 1화 162 · 2화 163 · **3화 50개**(분당 1.0 — 크롭해도 62)였다. 1·2화 캐시는 그대로(요청 임계 0.3 기준 캐시 재사용 — 산출 회귀 0),
  3화는 0.1 → 287개(분당 5.8). 빠진 경계는 45초짜리 가짜 샷을 만들어 드리프트·같은 샷 재사용의 뿌리가 된다(2026-09-12 v1 35초 검은 화면의 원인도
  이것). ⚠ CLAUDE.md 이전 기록의 "10fps 검출 779경계"는 포핸즈 값이고 이 작품은 아니다. 회귀 가드: `tests/test_tikitaka_core.py` 적응형 임계 절.
- **배우 뒤바뀜 — 인덱스가 두 여배우를 양방향으로 바꿔 적은 경우(2026-09-13, 3화 사용자 지적 "김혜수와 조여정이 바뀌었는데?")**: 3화 인덱스(10분 창)가
  박경희↔안수정을 창마다 다르게 적어, 목소리 대조의 한 클러스터(speaker_3, 177줄)에 안수정 99·박경희 69 가 섞였다(다수 0.56 < 0.7 → 보류). 다른
  클러스터(speaker_0)는 조여정 목소리와 임예지 목소리가 합쳐져 있었다. 프레임 실측: 1100s·2302s·2690s = 김혜수(긴 웨이브 머리), 1878s·2153s = 조여정(단발).
  → speaker_3 의 안수정 표기 99줄을 박경희로, speaker_0 의 박경희 표기 38줄을 안수정으로 **클러스터 기준 수동 교정**(transcript.json `voice_relabel` 기록,
  `speaker_prev` 보존, index.json speakers 동기화) 후 `--redo digest` 로 이해 문서부터 재생성(digest sha 가 화자를 포함하므로 자동 무효화).
  ⚠ 내가 v2 제목을 "촬영자 = 김혜수"로 고친 것은 뒤바뀐 표기를 믿은 오판이었다 — 촬영자는 조여정(안수정)이 맞다. 대사 내용("여보야", "우리 예지 부모잖아")이
  표기와 어긋나면 표기를 의심할 것. `voice_check` 는 이제 한 클러스터에 이름 둘이 각각 30% 이상이면 `⚠⚠ 한 목소리에 두 이름` 을 크게 남긴다(자동 교정은 안 함 —
  어느 이름이 맞는지는 화면을 봐야 안다).
- **효과자막 타이밍·자막 스타일(참고 쇼츠 dtjASrRSXFc·GPDY61vwaGY·aZEWYEIPnt8 실측)**: `render.effect_window` — S 행은
  **펀치라인(마지막 줄) 시작**에, A 행은 동작 시작, N 행은 문장 시작에 1.8s. 화자별 자막 색(`SPEAKER_COLORS`, 등장 순 —
  첫 화자 흰색·연두·보라…), 5자 이하 감탄 줄은 1.35배 크게(`SHORT_LINE_*`). 참고 영상의 작품명 하단 배치는 `fill` 에 이미 있다.
- **고정 구성(2026-09-10 사용자 결정)**: 텍스트 리빌딩으로 10버전을 싸게 펼쳐 추천을 고르고 → **그 버전만 4.5 영상 확인 패스**
  (`verify.verify_version`, 실험 E3 의 정식화: 초안 + 소스 스크립트 + 영상을 agentic 으로 주고 장면을 확인해 없는 장면은 빼고
  시각 비트는 A/N 으로 보강, 산출은 ID 만) → 5단계 agentic 컷 검색 → 렌더. `--no-verify` 로 종전 P 경로. 확인 패스가 대본을
  바꾸면 `table.rows_fingerprint` 가 agentic 컷 캐시를 자동 무효화한다. 10버전 전부를 영상으로 돌리지 않는 이유는 비용
  (회당 12만 토큰대)이고, 후보 선정에는 텍스트로 충분하다는 판단.
- **삼위일체(제0-1원칙)의 기계적 보장**: LLM 은 어느 단계에서도 최종 타임코드를 짓지 않는다. 리빌딩은
  줄/순간 ID 로만 가리키고(`validate_versions` 가 없는 ID·비연속·화자 섞임을 드롭+기록), 테이블은
  코드가 전사 단어·샷 경계에서 붙인다(`timing.py` 순수 함수).
- **제1원칙(물리적 시간)**: 계획값은 공백 제외 4글자/초, **마스터는 TTS 실측**(edge-tts 실측 16자 → 3.7s).
  N 행의 비디오는 그 길이를 정배속 컷(1.0~2.0s)으로 채운다(`stack_cuts`) — 슬로우 모션 없음. 소스가
  모자라면 ① 쓴 컷을 2.0s 까지 ② 남은 소스 0.8s+ ③ 같은 샷 안에서 2.0s 초과 연장 ④ 인접 샷 보충
  (`SC@MM:SS.ms`) 순. 그래도 모자라면 `[미달]` 로 기록(조용한 포기 금지).
- **컷 소스 탐색 두 경로**(`--cut-search`): `agentic` = Gemini agentic video(Interactions API ·
  `processing:"agentic"`, 2026-09-01 출시 — 영상 전체를 주고 문맥 구간 근처의 리액션 컷을 순간 검색) /
  `index` = 3단계 인덱스에서 텍스트로 고름. agentic 은 fps·media_resolution·클리핑과 양립 불가라 그
  옵션 없이 부르고, 실패·부실 행은 index 로 떨어진다(기록 `cut_origin`). 인덱스 단계는 전수 채록이라
  agentic 이득이 없어 정적 처리다(문서 권고와 같다).
- **Interactions API 실측 함정(2026-09-09)**: `response_mime_type` 만 주면 400 `responseFormat must be set` —
  JSON 은 `response_format={"type":"text","mime_type":"application/json","schema":…}` 로 요청한다
  (`table.AGENTIC_SCHEMA`). 첫 실행이 이 400 으로 index 경로에 폴백해 렌더됐다(설계대로 본편은 막히지 않았다).
  Files API 업로드(42MB 스캔 프록시 ≈ 2분)는 `files_cache.json` 으로 잡 안에서 재사용한다.
  ⚠ 같은 날 실측 셋 더: ① 제약 없는 스키마로 부르자 `who` 한 필드가 같은 구절을 **216KB 반복**(런어웨이)
  — 스키마 `maxLength` 는 서버가 **강제하지 않는다**(20자 제한을 넘긴 값이 그대로 왔다). ② 그래서 건 출력 상한
  `generation_config.max_output_tokens` 는 **thought 와 나눠 쓴다**(thought 3,933 + 출력 156 ≈ 4,096 에서 절단)
  — 상한 16,384 + `thinking_config.thinking_level=low` 를 같이 보내고 거절되면 단계적으로 뺀다. ③ 잘린 JSON 은
  `llm.repair_json` 이 마지막 완결 객체까지 잘라 괄호를 닫아 살린다(반쯤 적힌 컷은 버린다 — 삼위일체).
- **index 경로 후보는 문맥 근처만**(`table.nearby_moments`, ±180s): 전량을 주면 모델이 8분·28분 지점의 무관한
  장면을 골랐다(첫 실행 실측). 근처 순간이 8개 미만이면 전량 폴백.
- ⚠ **샷 경계는 10fps 프록시에서 검출한다**(2026-09-10 교정): 첫 판은 1fps 스캔 프록시로 검출해 경계가 전부 정수 초였다
  (4.0·13.0·43.0…) — ±0.1s 마진이 실제로는 ±1s 였다. `probe.build_cut_proxy`(480p/10fps 무음)로 갈랐고
  `scenecuts.json` 캐시는 검출 소스 이름·임계로 키를 잡아 소스가 바뀌면 재검출한다(정밀도 다른 경계를 섞지 않는다).
- **경계 정책(2026-09-10 교정)**: 제0-2원칙은 "경계에 걸치지 마라"이지 "한 샷을 넘지 마라"가 아니다. 10fps 검출(779경계)
  뒤 A 행을 한 샷에 가두자 악보 던지기(컷 2개를 가로지름)가 0.5s 로 잘렸다. 지금은 `timing.action_window` — 제안 구간을
  그대로 쓰고 양 끝만 `nudge_in/out`(경계 ±0.1s 안이면 밖으로). N 소스도 제안 시작을 nudge 하고 가용 끝은 **제안 끝이 속한
  샷** 끝까지(제안 구간은 통째 포함). `stack_cuts` 는 0.8s 미만 소스를 안 쓴다(0.6s 플래시 컷 실측).
- **완성본 검수에서 나온 결함 3건(2026-09-10, 사용자 지적)**:
  ① **대사 꼬리 잘림** — whisper 마지막 단어 끝(신뢰도 0.79 "나가")이 실제 발성보다 앞서 +0.15s 로는 부족했다.
     `table.speech_end_after` 가 마지막 단어 뒤 첫 무음(silencedetect −35dB·0.18s) 시작 +0.06s 까지 늘린다(상한 +0.9s ·
     다음 단어 −0.05s). S 행만, 시작은 그대로.
  ② **다른 장면 인물 난입** — N 행 컷이 부족하자 ±180s 폴백이 2.5분 전 다른 장면(SC-033)의 순간을 끌어왔다. 이제 보충은
     **문맥 장면 안**(`scene_span_for_row`)에서만: 같은 장면 인접 샷 → 같은 장면 index 순간. agentic 요청도 행당 3~4컷.
  ③ **화면에 없는 동작어** — 제목 "뺨 때리는"·내레이션 "침 뱉고" 는 리빌딩 모델의 비유. 프롬프트 제3-1원칙(동작 묘사는
     화면 근거 있을 때만) + `rebuild.literal_action_flags`(동작어 어간이 인덱스·전사에 없으면 `[비유 의심]`) +
     `polish_literal_actions`(걸린 문구만 감정·태도 표현으로 고쳐 쓰는 텍스트 호출). 리빌딩 직후 자동, 캐시된 대본은 별도 실행.
- **A 행(현장음)도 agentic 이 찾는다**(2026-09-10): 같은 호출에 A 행을 실어 "인덱스 추정 ±10s 안에서 소리가 나는 동작의
  정확한 시작·끝"을 받고(`apply_agentic_a_rows`), 추정에서 10s 밖이면 무시+기록. 제안 끝 시각은 `CutSource.prop_out`
  으로 싣는다 — N 행처럼 샷 끝을 가용 끝으로 쓰면 A 행이 전부 '시작+3.0s' 가 된다(실측 재현). agentic 제안은
  `table_agentic_raw_v{n}.json` 에 캐시돼 테이블 규칙만 고칠 때는 호출 0 으로 재생성된다(`--redo agentic` 으로 갱신).
- **행 간 컷 중복 제거**(`trim_source` · `fill_n_rows` 의 used 목록): S/A 행 구간과 앞 N 행 컷을 사용됨으로 두고 뒤 행
  소스를 깎는다. 남는 쪽 0.5s 미만이면 버림. 컷 17~20개 기준 겹침 0 을 테스트가 고정한다.
- **실험 "원샷 agentic vs 단계형"**(`app/tikitaka/experiment.py`, 2026-09-10 · 포핸즈 v3 · 보고서
  `experiment/report_v3.md`): E1 완전 원샷은 S 행 시작 오차 평균 52s(최대 298s — 엉뚱한 장면)·환각 대사 1·싱크 깨짐 6/6
  · N 컷 1~2s 밖 9/10. E2(같은 대본, 시각만 모델)는 S 시작 오차 평균 0.14s(최대 0.22s)로 근접하지만 끝이 0.5~0.6s
  흔들려 2/5 싱크 깨짐 · 행 간 겹침 6 · N 컷이 대사 구간 침범 5 · 토큰 17.8만(파이프라인 agentic 1.6만의 11배).
  ⇒ agentic 은 **좁은 검색**(N 리액션·A 현장음)에 두고 시각 확정은 코드가 한다는 설계를 실측이 뒷받침한다.
  **E3 절충안**(같은 날 후속 — 리빌딩에 인덱스 텍스트 + 영상을 함께 주고 후보 장면을 agentic 으로 확인, 산출은 ID 로만,
  5·6단계는 P 와 같은 코드): 지표는 P 와 동급(립싱크 오차 0 · 겹침 0 · 대사 침범 0)이면서 **다른 이야기**를 골랐다 —
  피 흘리는 손가락 연주(시각 비트)를 A 행 6개로 살린 「손가락이 찢겨도 멈출 수 없는 이유」 73s. 호출 1회 12.6만 토큰
  (텍스트 리빌딩의 수십 배)·확인 2회. 텍스트만 본 P 가 60자 요약에 묻힌 시각 비트를 놓친다는 가설의 첫 실증이다.
  실험 코드: `experiment.run_e3` · 산출 `experiment/e3/`. 본 파이프라인 4단계에 넣을지는 사용자 결정(비용·결정성 대가).
  ⚠ 지표의 '샷 경계 ±0.1s 위반' 은 S 행도 세므로 파이프라인에 2건이 찍힌다 — 단어 경계가 우연히 컷 0.08s 옆인
  대사 행이고, 립싱크가 마진보다 우선이라 의도된 값이다.
- **모델 규칙 강제**: `llm.resolve_model` 이 env 의 금지 모델(오래된 `.env` 의 `gemini-3.5-flash` — 실제로
  9/9 첫 실행에서 잡혔다)을 3.7-flash 로 바꾸고 소리 내어 남긴다. 허용 목록 `ALLOWED_MODELS`.
- **오디오 정책(제3원칙)**: N = 원본 MUTE + 내레이션 / S·A = 원본 ON. TTS 는 행 비디오 길이로 `atrim`.
- 회귀 가드: `tests/test_tikitaka_core.py`(18) · `tests/test_tikitaka_stages.py`(13 — 합성 소스 실렌더 포함,
  `FFMPEG_BIN` 없으면 그 1건 스킵). LLM·whisper 없이 돈다.
