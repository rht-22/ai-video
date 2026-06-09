# GOAL: 파이프라인 14→12단계 단순화 + 단계별 데이터흐름 정렬 (PR-7)

> 상태: 구현 완료 (feat/cut-2 기반). 베이스: av_align 단계까지 포함된 14단계.
> 핵심: `character_index`·`chunk_transcribe`·`graph` 제거, `video_intent` 신설.

## 한 줄 목표
"사람이 영상을 통으로 한 번 보며 내용을 파악하고(=video_intent), 구간을 나눠 깊게 분석하고,
콘티를 짜고, 가편집을 검수하고, 렌더한다"는 흐름에 맞춰 단계를 12개로 단순화하고, 각 단계가
받는 데이터를 그 단계의 책임에 정확히 맞춘다. Gemini 가 인물·관계·전사를 직접 처리하므로
ArcFace 사전 인덱스·관계 그래프·청크 전사 단계를 없앤다.

## 최종 12단계 (`app/pipeline.py` `_STEP_ORDER`)
```
[준비]   init → research → probe → proxy
[파악]   video_intent → chunk
[분석]   gemini
[콘티]   story
[후처리] av_align → resources
[마무리] render → validate
```
진행 로그 번호는 `_step_tag()` 가 `_STEP_ORDER` 기준으로 `[N/12]` 자동 산출.

## 제거된 단계와 대체

| 제거 | 무엇을 했나 | 무엇이 대체하나 |
|------|-------------|-----------------|
| `character_index` | ArcFace(deepface) 사전 패스로 인물 등장 인덱스(좌표) 생성 | **gemini `characters_tracking`** 에 `x_norm/y_norm`(+samples) 추가 → 파이프라인이 `_build_character_appearances` 로 reframe 호환 `character_appearances` 빌드. deepface 의존 제거 |
| `chunk_transcribe` | 청크별 SRT/Whisper 전사 | **`video_intent`** 가 전체 전사(SRT 우선 → Gemini 폴백)를 flat 절대시간으로 1회 생성 → 청크/클립별 슬라이스 |
| `graph` | `extract_relationships` LLM 호출로 관계 엣지 | **gemini `continues_from`** → `assign_sequence_ids(edges=None)` 가 시퀀스 묶음. LLM 호출 1회 절감 |

## 신설 단계: `video_intent` (proxy 다음, chunk 앞)
- **overview**: `gemini_client.analyze_video_intent`(Flash, 기존 메서드 확장) — 전체 프록시 1회 스캔으로
  intent/emotional_arc/key_characters/story_beats/intro·credits + **`chunk_boundaries`** 산출.
- **chunk 경계**: `chunk` 단계가 `_normalize_chunk_boundaries` 로 ≥300초(5분) 하한 병합 / max 분할 /
  연속 커버리지 정규화 후 `chunker.build_chunks(boundaries=...)` 로 물리 분할. 경계 없으면 고정 길이 폴백.
- **transcript**: SRT 있으면 `subtitle.parse_subtitle`, 없으면 `gemini_client.transcribe_proxy`(Flash,
  경계-윈도우 단위) → flat `list[SpeechSegment]` 절대시간. (최종 자막은 av_align dialogue 가 우선이므로 컨텍스트 등급)
- 캐시: `checkpoint_video_intent.json` (`overview` / `chunk_boundaries` / `transcript`).

## 단계별 Gemini 호출 / 데이터 계약

| 단계 | 모델 | 입력(필요한 것만) | 출력 |
|------|------|-------------------|------|
| video_intent | Flash | 프록시 전체, work_context, cast 이름, (SRT) | overview + chunk_boundaries + transcript |
| gemini | **Pro** | 청크 mp4 + 청크 transcript 슬라이스 + 직전 청크 요약 | segments + candidate_moments(+continues_from) + **characters_tracking(x_norm/y_norm)** + intro_credits_ranges |
| story | Flash | candidates(+continues_from) + chunk_meta + 컨텍스트 (영상 X) | storyline 변형 + tts_cues 초안 |
| av_align | **Pro** | 가편집 mp4 + tts 초안 + 인물명 | dialogue + tts_cues + dead_air_cuts |

원칙: gemini 청크엔 전체 transcript가 아닌 슬라이스만, story엔 영상 대신 candidate 메타만 전달.
Pro/Flash 역할분담(CLAUDE.md) 준수 — Pro=`gemini-3.1-pro-preview`, Flash=`gemini-3-flash-preview`.

## 크롭(reframe) 데이터 흐름 (character_index 대체)
- gemini `characters_tracking[].appearances[]` 에 `x_norm/y_norm`(+선택 `samples`) 추가.
- 파이프라인이 청크 루프에서 `_abs_characters_tracking`(actual_cut_offset 보정) 으로 chunk_meta 에 **절대시간**
  저장 → gemini 블록 직후 `_build_character_appearances`(overlap 병합) 로 `character_appearances` 생성.
- `reframe` 는 opencv(Haar)로 얼굴을 **탐지**하고, `face_id.find_target_in_index` 가 좌표로 **어느 얼굴이
  타깃인지 식별**(`face_identifier=None` 으로 동작). 좌표 없으면 최대 얼굴/센터 폴백.
- resources 크롭 가드는 `(face_identifier or character_appearances)` 로 확대(좌표만 있어도 추적).

## 하위호환 / 재개
- `_legacy_step_alias`: `skeleton→video_intent`, `character_index→gemini`, `chunk_transcribe→video_intent`,
  `graph→story` (+ 기존 exclusion/transcribe/tts_plan/silence_cut). `--from-step skeleton` 크래시 해소.
- `cli.py --from-step` choices = 12단계 + 레거시 별칭(argparse 가 alias 처리 전 거부하지 않도록 유지).
- 제거 단계 옛 체크포인트(`checkpoint_character_index/chunk_transcripts/graph.json`) 자동 삭제 마이그레이션.

## 제거된 코드
- `gemini_client`: `extract_relationships`, `RELATIONSHIP_EXTRACTION_PROMPT`, `compose_story_with_context` 의
  `relationship_edges` 파라미터/블록.
- `face_id.py`: `FaceIdentifier` 클래스 전체(+죽은 `find_target_in_frame`) → `find_target_in_index` 만 유지.
- `scripts/verify_character_index.py` 삭제(ArcFace 경로 검증 — 무의미해짐).

## 수용 기준 (현재 상태)
- [x] `_STEP_ORDER` 12단계, 로그 `[N/12]` 연속.
- [x] character_index/chunk_transcribe/graph 단계·체크포인트 제거 + 데이터 소비처 새 출처로 대체.
- [x] video_intent overview + ≥5분 경계 + 전사 산출, chunk 가 경계로 분할.
- [x] gemini characters_tracking 좌표 → character_appearances → reframe (ArcFace 미호출).
- [x] graph 제거 → continues_from 기반 시퀀스.
- [x] Pro/Flash 모델 규칙 위반 0.
- [x] 기존 190 + 신규 15 = 205 테스트 green.
- [ ] (운영 게이트) 실제 영상으로 인물 크롭 품질이 ArcFace 대비 허용 수준인지 육안 검증.

## 알려진 리스크
1. **프레이밍 품질**: Gemini `x_norm/y_norm` 은 ArcFace 프레임별보다 시공간 밀도가 낮다. opencv 박스가
   실제 중심을 보정하지만, sample 간격이 `max_dt_sec=3.0` 보다 벌어지면 센터/최대얼굴 폴백. 프롬프트가
   appearance당 좌표(+긴 구간 samples)를 요청하도록 설계됨 — 실제 푸티지로 검증 필요.
2. **시퀀싱**: `edges=None` 경로는 cross-chunk `continues_from` 를 다시 신뢰하므로 gemini 정확도에 의존.
3. **전사 비용**: no-SRT 시 경계-윈도우별 Gemini 호출 추가. SRT 우선이 일반 케이스 완화.
