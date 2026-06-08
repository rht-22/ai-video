# GOAL: 가편집 영상 기반 Gemini 재분석 단계 추가 (A/V 정렬 + dead-air 컷)

> 상태: 명세 확정(2026-06-05). 구현은 별도 세션에서 진행.
> 결정사항: ① 기존 Whisper `silence_cut` **완전 대체**, ② 가편집·재분석을 **출력하는 모든 variant**에 적용.

## 한 줄 목표
스토리 구성대로 1차 렌더링한 "가편집 영상"을 Gemini Pro(`gemini-3.1-pro-preview`)에게
다시 보내, (1) TTS 자막의 **정확한 삽입 위치**, (2) 영상 속 실제 발화의 **화자·대사**,
(3) 대사와 대사 사이 **붕뜨는(dead-air) 컷 구간**을 한 번에 추출하고, 이 결과로 최종
영상을 렌더링하는 새 단계를 추가한다.

## 배경 / 현재 문제 (app/pipeline.py 13단계)
현재 흐름: `story` → `silence_cut` → `resources` → `render`
- **story** (`gemini_client.compose_story_with_context`): 스토리라인 클립 + `tts_cues[]`를
  생성. 그러나 `tts_cues[].start_sec`는 LLM이 **영상을 보지 않고 추정한 값** →
  실제 발화/장면과 sync가 안 맞고 각 구간 앞부분에 몰린다.
- **자막**: `chunk_transcribe`의 Whisper(`speech.extract_transcript`) 결과를 슬라이스해
  자막으로 쓴다 → **대사 텍스트·타이밍·화자가 부정확**.
- **silence_cut** (`silence_cutter.cut_silence_with_story_filter`): 위의 부정확한 Whisper
  segment gap에 의존해 무음을 컷한다 → 실제 "대사 사이 붕뜨는 구간"을 정확히 못 잡는다.

핵심 원인: TTS 위치 결정·대사 추출·무음 컷이 모두 **편집된 결과물을 보지 않은 채**,
원본 청크 단위의 Whisper/LLM 추정에 의존한다.

## 해결 방향: "1차 렌더 → Gemini 재분석 → 최종 렌더" 2-pass 구조
선택된 스토리라인을 **먼저 한 번 렌더링**(가편집본)한 뒤, 그 영상 자체를 Gemini Pro가
시청하고 편집 타임라인 기준의 정확한 메타데이터를 산출하게 한다.

신설 단계 이름: **`av_align`**. `step_order`에서 기존 `silence_cut`을 **제거하고 그 자리에
`av_align`을 넣는다**(story → av_align → resources → render). 무음 컷 기능은 `av_align`이
Gemini 기반으로 흡수하므로 Whisper 기반 컷은 더 이상 호출하지 않는다.
※ `silence_cutter`의 보호 규칙(아래)은 개념적으로 계승하되, 컷 판정 주체를 Whisper gap에서
Gemini 시청 판단으로 교체한다.

### 작업 1 — 렌더 기반 Gemini 재분석 단계 신설 (`av_align`)
**출력하는 모든 variant 각각에 대해** 아래를 수행한다(variant당 가편집 렌더 1회 +
Gemini Pro 영상 호출 1회).

1. **가편집 렌더**: variant 스토리라인의 클립들을 편집 순서대로 이어붙인 가편집 mp4 생성.
   - 자막/타이틀/TTS를 **입히지 않은** 깨끗한 컷본(원본 오디오만 포함).
   - 비용 절감을 위해 **프록시(480p)·낮은 fps** 화질로 렌더한다(분석용이므로 화질 무관).
   - 타임라인은 **가편집본 절대시간(0 = 쇼츠 시작)** 기준으로 고정한다.
2. **Gemini Pro 업로드·분석**: 가편집 mp4를 `files.upload`(기존 `analyze_chunk`의
   `_safe_upload_path` + `client.files.upload` 패턴 재사용)로 올리고, 아래를 추출한다.
   응답은 반드시 구조화 스키마(`_validate_gemini_schema` 검증)로 받는다.
   - `dialogue[]`: `{ start_sec, end_sec, speaker, text }`
     - **가편집 타임라인 기준**, 영상에서 실제로 들리는 발화를 누가(speaker) 무슨 말(text)
       했는지. 이것이 **최종 자막의 원천**이 된다(Whisper 자막을 대체).
   - `tts_cues[]`: `{ start_sec, end_sec, text, voice, speed }`
     - 발화가 비어 있는(나레이션을 넣어야 자연스러운) **정확한 위치**에 TTS를 배치.
     - story의 초안 `tts_cues` 텍스트를 **재배치/재타이밍**하는 형태(텍스트는 유지하되
       위치를 영상에 맞게 교정). voice는 "한 쇼츠 = 한 보이스" 규칙 유지.
3. **결과 반영**: 위 `dialogue`로 자막 ASS를 만들고(`subtitle.build_ass_from_segments`),
   위 `tts_cues`로 TTS를 합성(`tts.synthesize_tts_with_fit`)·배치해 **최종 render**한다.
   즉 `resources`/`render`의 입력을 story 추정값이 아니라 **variant별 재분석 결과**로 교체한다.

### 작업 2 — 같은 단계에서 dead-air 컷 구간 동시 산출
작업 1의 Gemini 재분석 응답에 컷 구간을 함께 포함시킨다.
- `dead_air_cuts[]`: `{ start_sec, end_sec, reason }` — 가편집 타임라인 기준,
  **사람의 대사와 대사 사이에 붕뜨는(무의미하게 정적인) 구간**.
- **삭제 목적의 정의**: 대사↔대사 사이의 의미 없는 공백만 제거한다.
- **보호 규칙(반드시 지킬 것)**: 오디오가 없어도 **시각적으로 중요한 장면은 절대 컷 금지**.
  - 기존 `silence_cutter`의 보호 개념을 그대로 계승: `visual_essential=True` 클립,
    표정·시선·키스·충돌·눈물 등 액션/감정 비트(`_ACTION_VERB_KEYWORDS`)가 있는 구간,
    화자 전환 경계는 보호.
  - Gemini에게 "이 구간을 잘라도 스토리·감정·시각 정보 손실이 없는가"를 명시적으로
    판단시키고, 불확실하면 **보호(컷 안 함)** 쪽으로 보수적으로 동작하게 한다.
- 산출된 `dead_air_cuts`를 적용해 클립 타임라인을 재조정하고, 컷으로 당겨진 만큼
  `dialogue`/`tts_cues`의 시간도 함께 보정한다(기존 `_shift_cues_by_silence_cut`와
  `flatten_to_clips`의 시간 보정 패턴 참고).
- **길이 가드**: dead-air 컷 적용 후 `min_duration_sec` 미달이면 해당 variant는 컷 전
  버전으로 롤백한다(기존 silence_cut 롤백 패턴 재사용).

## 제약 / 규칙
- **Gemini 모델 고정**: 영상 분석은 Pro=`gemini-3.1-pro-preview`,
  스크리닝/텍스트 보조는 Flash=`gemini-3-flash-preview`. 그 외 모델 금지(CLAUDE.md).
- **체크포인트/재개 구조 유지**: `av_align`을 `step_order`에 등록하고
  `checkpoint_av_align.json`으로 캐시·`--from-step av_align` 재개가 되게 한다.
  variant별 가편집 렌더 결과 mp4와 Gemini 응답(dialogue/tts_cues/dead_air_cuts)을 캐시한다.
  - 제거되는 `silence_cut`은 `_legacy_step_alias`에 `"silence_cut": "av_align"`으로 등록해
    구버전 `--from-step silence_cut` 입력을 redirect 한다(기존 alias 패턴).
- **비용/시간**: 렌더가 variant당 2회(가편집 + 최종), Gemini Pro 영상 호출이 variant당 1회
  추가됨. 가편집은 프록시 화질·낮은 fps로 최소화한다.

## 부수 정리 (이 작업과 함께 처리)
### 진행 로그 단계 표기 통일
현재 `print`문의 `[N/15]` 표기는 과거 15단계 시절의 하드코딩 잔재로, `step_order`
정의(실행 13단계 + validate)와 어긋나 있다. 제거된 단계(exclusion·transcribe·tts_plan)
자리의 번호가 비어 로그가 점프하고(`[4/15]`→`[7/15]`, `[10/15]`→`[13/15]`), resources가
`[13/15]`/`[14/15]`로 혼재 표기된다. `av_align` 추가로 단계 구성이 바뀌는 김에 함께 정리한다.
- 모든 진행 로그를 **`step_idx` 기반 동적 표기**로 교체한다
  (예: `f"[{step_idx['render']+1}/{len(step_order)}] ..."`). 하드코딩 숫자를 전부 제거.
- 총 단계 수가 **`step_order` 한 곳에서만** 결정되도록 한다(단계 추가/제거 시 자동 반영).

## 수용 기준 (Acceptance)
- [ ] 최종 영상의 **자막 텍스트·타이밍·화자**가 영상 속 실제 발화와 일치(Whisper 오타·
      어긋남 해소).
- [ ] **TTS가 빈 구간의 올바른 위치**에서 재생되고, 발화와 겹치지 않는다.
- [ ] 대사 사이 **붕뜨는 구간이 제거**되어 템포가 빨라진다.
- [ ] 오디오가 없어도 **시각적으로 중요한 장면은 보존**된다(과도 컷 없음).
- [ ] 출력하는 **모든 variant**에 재분석이 적용된다.
- [ ] `--from-step av_align` 재개가 동작하고 체크포인트가 정상 캐시된다.
- [ ] 기존 `silence_cut` 경로가 제거되고, 구버전 `--from-step silence_cut`은 redirect 된다.
- [ ] 진행 로그의 단계 번호가 `step_order` 기준으로 정확·연속적이다(하드코딩 `[N/15]` 전부 제거).
- [ ] Pro/Flash 모델 규칙 위반 없음.

## 구현 시 참고 위치 (앵커)
- 단계 순서/재개: `app/pipeline.py` `step_order`(~L1580), `_legacy_step_alias`(~L1599)
- 기존 무음 컷(제거 대상): `app/modules/silence_cutter.py`,
  `cut_silence_with_story_filter`(~L248), `flatten_to_clips`(~L120),
  파이프라인 호출부 `[silence_cut]`(~L2455), `_shift_cues_by_silence_cut`
- Gemini 영상 업로드 패턴: `app/modules/gemini_client.py`
  `analyze_chunk`(~L1312), `_safe_upload_path`(~L16), `client.files.upload`(~L1411)
- 스토리/cue 산출: `compose_story_with_context`(~L1684), `_normalize_storyline_tts_cues`
- 자막 생성: `app/modules/subtitle.py` `build_ass_from_segments`, `build_tts_ass`
- TTS 합성: `app/modules/tts.py` `synthesize_tts_with_fit`, `VOICE_PRESETS`
- 리소스/렌더: `app/pipeline.py` `[resources]`(~L2704), `[render]`(~L2883), 멀티 variant(~L3034)
- 진행 로그 표기(정리 대상): `app/pipeline.py`의 `[N/15]` print 다수(~L1437 ~ ~L3027) → `step_idx` 동적화
