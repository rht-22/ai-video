# 원격 tikitaka ↔ 현재 v3-human-flow 비교

작성일: 2026-09-14 (KST). `git fetch origin` 완료 후 두 브랜치의 커밋·소스·테스트 정의를 비교했다.

## 1. 핵심 결론

**tikitaka는 대사 중심 쇼츠의 여러 버전을 먼저 펼쳐 고르는 파이프라인이고, human-flow는 한 편을 단계별로 구성하고 초안을 다시 보며 다듬는 파이프라인이다.** 공통 기반에서 서로 다른 기능을 추가한 관계이며, 어느 한쪽이 다른 쪽을 전부 포함하지 않는다.

- **tikitaka**: 14가지 대본 전략 → 재순위 → 선택 버전 영상 확인 → 대사·현장음·내레이션 테이블 → 버전별 렌더. 여러 안을 만들고 고르는 작업에 초점이 있다.
- **human-flow**: 주제 → 씬 → 대사 → 내레이션과 화면 선택 → 덮개 재관찰 → 초안 트림 → 연출 → 최종 렌더. 편집 순서와 중간 검증, 수정 후 재렌더에 초점이 있다.
- 현재 human-flow에는 **권리사 활용 불가 구간 차단, 회차 지도, 여러 편 계획, 화자 추적, 렌더 캐시·이전본 보존**이 있다. 다만 회차 지도·여러 편 계획은 옵션이다.
- 이것은 **코드 구조 비교**다. 같은 영상을 양쪽으로 생성하는 A/B 실험은 하지 않았으므로 화질·재미·조회수·실제 비용·처리 속도의 우열은 판정하지 않는다.

## 2. 비교 기준과 Git 관계

사용자가 지칭한 현재 `human-flow`의 실제 브랜치명은 **`v3-human-flow`**다.

| 항목 | 고정한 기준 |
|---|---|
| 원격 tikitaka | `origin/tikitaka` · `8961e3b8a32db1a306f41bffd6d98439bce6ad1c` |
| 현재 human-flow | `HEAD` / `v3-human-flow` · `00635374fa9a85ff8fd9651329538e26555e5762` |
| 원격 human-flow | `origin/v3-human-flow`는 현재 HEAD와 동일 |
| 공통 조상 | `74df3ce8aa8d237d234b155000033ad7a40ab0da` |
| 공통 조상 이후 고유 커밋 | tikitaka **1개**, human-flow **53개** |
| 작업 트리 | 조사 시작 시 추적 파일 수정 없음. 기존 미추적 파일은 비교에서 제외 |

```text
74df3ce  공통 기반 (기존 v3 포함)
├── 8961e3b              origin/tikitaka
└── 53개 고유 커밋 ── 0063537  v3-human-flow = origin/v3-human-flow
```

| 비교 방향 | 변경 파일 | 추가 줄 | 삭제 줄 |
|---|---:|---:|---:|
| 공통 조상 → tikitaka | 35 | 7,954 | 2 |
| 공통 조상 → human-flow | 154 | 27,912 | 399 |
| tikitaka → human-flow의 최종 트리 차이 | 186 | 27,907 | 8,346 |

줄 수는 Git의 텍스트 diff 집계다. 변경 파일 수에는 바이너리 자산도 포함된다. 최종 트리 차이는 추가 105개·수정 49개·삭제 31개·이름 변경 1개다.

**주의:** 최종 diff에서 `app/tikitaka/*`가 삭제로 표시되는 것은 human-flow가 그 기능을 삭제한 이력이 있다는 뜻이 아니다. 공통 조상 이후 tikitaka 쪽에만 추가되어 현재 브랜치에는 없다는 뜻이다. 또한 tikitaka 브랜치에도 기존 `app/v3/`가 있으나, 이후 human-flow의 53개 커밋은 포함하지 않는다.

## 3. 실행 흐름

### tikitaka

```text
원본 probe / 오디오 / 스캔 프록시 / 샷·암전 검출
  → 전사 (기본 ElevenLabs, Whisper 선택 가능)
  → 전사 글자 교정
  → 영상 인덱스 (장면·화자·대사·순간)
  → 목소리 기반 화자 대조 (ElevenLabs 경로)
  → 작품 이해 문서 digest
  → 14개 전략 대본 생성 + 재순위
  → 선택된 버전별 영상 확인 verify
  → 편집 테이블 (N/S/A, TTS 길이 실측, 컷 탐색)
  → 주인물 프레이밍 + 효과 자막 배치
  → shorts_vN.mp4 + publish_vN.json
```

`N`은 내레이션, `S`는 원본 대사, `A`는 현장음/행동 순간이다. `S`는 전사 줄 ID, `A`는 순간 ID를 참조하고, 코드가 실제 컷 구간을 계산한다. N행의 화면은 테이블 단계에서 탐색한다.

기본 `--count 1`이므로 14개 대본을 모두 렌더하는 것은 아니다. `--count N`, `--version`, `--all-versions`로 대상을 선택한다. 일부 도움말에는 여전히 “10버전”이라고 적혀 있지만 전략 정의는 비선형 10개와 선형 4개다.

### human-flow

```text
원본 / Whisper 어절 시각 / 장면 컷 → grid
  → Stage 1 전체 분석 → 청크 분할 → Stage 2 정독
  → [옵션] 회차 지도 episode_map
  → [옵션] 여러 편 계획 plan + 이번 편 slot 선택
  → Stage 3 human 체인
      주제 → 씬 → 대사 → 내레이션·화면 ID·TTS 실측 → 덮개 재관찰
  → edit_plan / 자막 / TTS / review.json
  → 480p 초안 → watch_trim → 필요 시 초안 재렌더
  → Stage 4 연출 → 최종 렌더 → validate
```

**브랜치에 있다고 자동으로 human 체인이 켜지는 것은 아니다.** `app/v3/cli.py`의 `--story-flow` 기본값은 `legacy`다. 비교한 편성 동작을 사용하려면 **`--story-flow human`**을 명시해야 한다.

## 4. 기능별 차이

| 비교 축 | tikitaka | 현재 human-flow |
|---|---|---|
| 편성 단위 | 여러 전략의 대본을 생성하고 순위로 선택 | 한 편을 단계별로 좁혀 구성. 옵션으로 회차 전체 편성 계획 |
| 여러 편 생성 | 선택 버전을 한 CLI 실행에서 반복 확인·렌더 | `--plan-shorts`로 1~8편 계획, `--plan-slot`으로 이번 실행의 편 선택. 계획만으로 전 슬롯을 일괄 렌더하지 않음 |
| 대사 정본 | STT 단어 시각에 대사 줄을 결합. 기본 Scribe v2 | Whisper 어절 시각과 grid를 사용. SRT가 있어도 어절 시각 정본은 Whisper |
| 영상 관찰 | 360p/1fps 스캔, 480p/10fps 컷 검출 프록시를 분리 | 전체 분석·청크 정독·국소 프로브·초안 시청을 단계별로 수행 |
| 내레이션과 화면 | N 문장을 만든 뒤 테이블에서 화면 탐색 | 내레이션 단계에서 화면 ID도 선택하고 TTS 길이를 즉시 실측 |
| 길이 확정 | N=TTS 실측, S=단어 시각, A=샷 경계로 서로 다르게 결합 | span·대사 경계와 실측 TTS에 맞춰 조립하고 초안에서 불필요 구간 제거 |
| 편성 오류 처리 | 없는 ID·혼합 화자·금지 구간 등의 항목을 제거/축소하고 `issues` 기록 | 단계 검증 실패 시 사유를 담아 최대 2회 재질의. 보조 프로브 실패 등은 별도 처리 |
| 영상 확인 실패 | verify 호출 실패 또는 빈 결과/대사 없는 결과면 로그 후 초안으로 진행 | 편성 검증과 보조 관찰을 구분. 모든 실패가 중단되는 구조는 아님 |
| 완성 전 재관찰 | 원본을 보는 verify는 있으나 편집된 초안을 보는 watch_trim 단계는 없음 | `draft_480.mp4`를 보고 빼기 전용 트림, 트림된 화면으로 연출 구성 |
| 회차 이해 | `digest.json`/`digest.md`로 작품 이해 자료 생성 | Stage 2 화면 글자·연출 층위와 선택적 회차 지도. 사실/믿음·설정/회수 자료 |
| 편 간 중복 | 프롬프트의 다양성 지시와 순위 선택. 순위 번호 중복 제거는 소재 중복 검사와 다름 | plan 검증에서 편 간 핵심 meaning ID 중복률이 작은 집합 기준 20% 초과면 반려 |
| 이미 쓴 소재 제외 | `--range`로 사용할 범위를 지정하고 나머지를 제외 | `--exclude-topic`, `--exclude-range`로 제외. 구간 50% 이상 겹친 사건 단위 반려 |
| 권리사 지침 | Markdown 가이드에서 지양 단어·활용 불가 구간·배우명·로고·카피 등 처리 | `editorial` 지침과 JSON `banned` 구간, 채널 design 프리셋으로 분리 |
| 활용 불가 구간 | 가이드 구간을 대본·컷 후보·테이블 검사에 반영 | 시각이 조금이라도 겹친 사건 단위 + 주변 키워드 증인을 차단하고 조립/렌더/훅 변형에 검사 |
| 화자·프레이밍 | diarization 화자 대조 + Gemini 프레임 판정으로 주인물 크롭 | 기본 YuNet 기반 화자 추적. 표본 재적률·컷 분할·랜드마크·초점 단서·계단식 고정 |
| 자막 교정 | 전사 교정 및 영상 확인의 화자 수정 | 단어 정렬·인명·저확신 청취·화면 묘사와 발음 근거를 이용한 교정 |
| 노래 자막 | 별도의 v3 singing 스위치에 해당하는 CLI 기능 없음 | `--subtitle-skip-singing`으로 가창 구간 판정·가사 자막 제외 |
| 연출·레이아웃 | `fill`/`band`, 버전별 효과 자막·주인물 프레이밍 | 채널 design, 라벨 얼굴/자막 회피, 강조 자막·줌·정보 화면·썸네일 안전 영역 등 |
| 사람 수정 | 산출 JSON 직접 편집, `--tag` 변형, `--redo` 재생성 중심 | `--edit-overrides`, 원본 좌표 앵커 재매핑, `review.json` 검수 항목 |
| 재렌더 | 렌더 함수가 호출되면 출력 생성. 최종 렌더 지문 스킵·이전본 자동 보존 없음 | 렌더 지문 일치 시 재사용. 재렌더 전 `final_prev_<timestamp>.mp4` 보존 |
| 재사용 캐시 | 상류에 존재 여부 캐시, 테이블/agentic 등 하류에는 내용 검사·지문도 있음 | 주요 분석·편성·초안·스타일·렌더에 지문. 전사까지 전부 내용 지문이라고 일반화할 수 없음 |
| 호출 추적 | `llm_usage.json`, 단계 로그 | `run_log`의 `gemini_usage`, 모델 provenance |

중복률 20%는 **meaning ID 집합의 중복**이다. 최종 영상의 초 단위 중복률이나 제목 유사도를 직접 보장하는 수치는 아니다. 두 파이프라인 모두 코드 검증이 의미·연출 품질 전체를 보증하지는 않는다.

## 5. 모델·공유 모듈 차이

양쪽의 기본 Gemini 모델은 `gemini-3.7-flash`지만 **환경변수 처리 정책은 다르다.**

| 항목 | tikitaka | human-flow |
|---|---|---|
| 기본값 위치 | `app/tikitaka/llm.py`의 독립 상수 | `app/model_policy.py`에서 기본값 공통화 |
| 모델 슬롯 | 영상용/텍스트용 환경변수 구분 | Pro/Flash 슬롯과 역할 구분 유지 |
| env 처리 | 허용 목록 검사 후 목록 밖이면 로그와 함께 기본값으로 대체 | `GEMINI_MODEL_NAME`, `GEMINI_FLASH_MODEL_NAME` 값 우선 |
| 현재 소스상 허용 목록 | `3.7-flash` 외에 `3.8-flash`도 포함 | 공통 기본값은 두 슬롯 모두 `3.7-flash` |

원격의 `3.8-flash` 허용은 현재 작업 공간에 제시된 모델 규칙과 다른 지점이다. **원격 코드에서 발견한 차이로만 기록하며, 사용 권장이나 실행을 의미하지 않는다.** 실제 모델 호출은 하지 않았다.

또한 tikitaka는 공유 `app/modules/stt_elevenlabs.py`에 `diarize=False` 선택 인자를 추가해 화자 대조에서 `True`로 호출한다. 현재 human-flow에는 이 변경이 없다. 향후 tikitaka 디렉토리만 복사하면 이 공유 함수 계약이 빠질 수 있다.

## 6. 주요 파일과 산출물

| 영역 | tikitaka의 핵심 파일 | human-flow의 핵심 파일 |
|---|---|---|
| 진입점 | `app/tikitaka/cli.py` | `app/v3/cli.py`, `app/v3/pipeline.py` |
| 전사·화자 | `transcribe.py`, `transcript_polish.py`, `voice_check.py` | `app/v3/transcribe.py`, `textcheck.py`, `assemble.py` |
| 분석·이해 | `index.py`, `digest.py` | `chunk_analyze.py`, `screen_text.py`, `episode_map.py` |
| 편성 | `rebuild.py`, `verify.py` | `story_flow/{select,narration,cover}.py`, `plan.py` |
| 시간·조립 | `timing.py`, `table.py` | `assemble.py`, `story.py` |
| 렌더·연출 | `render.py`, `framing.py`, `effects.py` | `finalize.py`, `stage4.py`, `label_faces.py`, `watch_trim.py` |
| 지침 | `guide.py`, `guides/tikitaka/` | `banned.py`, `app/modules/editorial.py`, `app/data/channel_designs/` |

표에서 접두사를 생략한 Python 파일은 해당 열의 `app/tikitaka/` 또는 `app/v3/` 아래에 있다.

| 산출 목적 | tikitaka | human-flow |
|---|---|---|
| 분석 재료 | `transcript.json`, `index.json`, `digest.json` | `grid.json`, Stage 1/2 체크포인트, 선택적 `episode_map.json` |
| 대본·계획 | `rebuild.json`, `rebuild_versions.md`, `verified_vN.json`, `table_vN.json` | `checkpoint_story.json`, 선택적 `checkpoint_plan.json`, `edit_plan.json` |
| 초안·검수 | `verify_raw_vN.json`, 버전의 `issues`/`changes` | `draft_480.mp4`, `checkpoint_watch_trim.json`, `review.json` |
| 최종본 | `shorts_vN.mp4`, `publish_vN.json` | `final_1080x1920.mp4`, `render_fingerprint.json`, 이전 최종본 |

JSON 어휘가 N/S/A 행과 beat/span/timeline으로 다르고 산출 파일명도 다르므로, **기존 잡 디렉토리나 체크포인트를 그대로 바꿔 끼울 수 있는 관계는 아니다.**

## 7. 변경 규모와 테스트 근거

| 소스 정의 집계 | origin/tikitaka | 현재 HEAD |
|---|---:|---:|
| `app/tikitaka/` Python 파일 | 23 | 0 |
| `app/v3/` Python 파일 | 22 | 35 |
| `tests/test_tikitaka*` + `tests/test_v3_*` 파일 | 14 | 41 |
| 위 파일의 `test_*` 함수 정의 수 | 342 | 685 |

테스트 숫자는 Python AST로 센 **함수 정의 수**다. parametrization이 적용된 실행 케이스 수나 통과 수가 아니며, 전체 저장소 테스트 수도 아니다. 문서 작성 작업이므로 테스트 스위트·유료 API·실영상 렌더는 실행하지 않았다.

대표 회귀 검증 파일:

- tikitaka: `tests/test_tikitaka_core.py`, `tests/test_tikitaka_stages.py`.
- human-flow: `tests/test_v3_story_flow.py`, `tests/test_v3_watch_trim.py`, `tests/test_v3_banned.py`, `tests/test_v3_plan.py`, `tests/test_v3_overrides_anchor.py`, `tests/test_v3_text_style.py`, `tests/test_v3_composition_rules.py`.

human-flow의 주요 추가 영역은 story_flow 체인, 회차 지도·여러 편 계획, 화면 글자 정독, 자막 교정, 노래 제외, 금지 구간, 화자 추적, 레이아웃·효과음·재렌더 안정화다. 파일 수에는 폰트·로고·효과음·YuNet 모델 등 자산 추가도 포함되므로 코드 복잡도와 동일시하면 안 된다.

## 8. 함께 사용할 때의 검토 지점

두 경로를 함께 유지하는 것은 구조상 검토 가능하다. tikitaka가 독립 패키지이고 기존 v3를 교체하지 않기 때문이다. 다만 병합 가능성과 실행 호환성은 별도 검증이 필요하다. **이번 작업에서 merge/cherry-pick은 하지 않았다.**

1. **공유 변경:** 공통 조상 이후 양쪽이 모두 변경한 경로는 `.gitignore`, `CLAUDE.md`다. 실제 충돌 여부는 병합을 시험하지 않았으므로 확정하지 않는다. tikitaka 전용 공유 STT 변경도 함께 고려해야 한다.
2. **모델 규칙:** 독립 `llm.py`의 허용 목록과 현재 공통 모델 정책을 맞춰야 한다.
3. **잡·어댑터 계약:** 입력 플래그, JSON 스키마, 최종 파일명이 다르므로 명시적인 어댑터가 필요하다.
4. **재사용 후보:** human-flow의 렌더 지문·이전본 보존·검수 항목 수집은 tikitaka에 적용할 후보이고, tikitaka의 여러 전략 생성·재순위·가이드 입력은 human-flow에 적용할 후보다. 이미 구현됐다는 뜻은 아니다.
5. **대본 다양성과 편 간 중복:** 버전 수를 늘리는 기능과 중복을 측정하는 기능은 별개다. 여러 편 발행을 묶으려면 두 개념을 함께 다뤄야 한다.

## 9. 기존 비교 문서와 달리 바로잡은 부분

원격 tikitaka에는 같은 경로의 기존 비교 문서가 있었다. 그 문서는 이식 제안과 과거 실측 기록을 포함하므로 참고하되, 현재 두 HEAD의 코드 확인을 우선했다.

- **“human-flow에는 권리사 계약 입력이 없다”**: 현재는 `editorial`과 `banned`, 채널 프리셋이 있다. Markdown 가이드를 직접 읽는 방식과는 다르다.
- **“영상 정독 이후에는 국소 창만 본다”**: tikitaka verify는 원본 전체 스캔 프록시를 `agentic_video_json`에 전달한다. 이후 관찰을 모두 국소 창으로 단정할 수 없다.
- **“human-flow의 전 단계 캐시가 내용 지문이다”**: 주요 단계의 지문은 확인되지만 전사까지 동일하다고 단정할 수 없다.
- **“tikitaka는 10버전”**: 현재 전략 목록은 14개다. 기본 렌더 수는 1개다.
- 기존 문서의 비용·시간·중복률 실측 숫자는 이번에 재현하지 않았으므로 이 문서의 성능 근거로 사용하지 않았다.

## 10. 소스 근거와 재현 명령

아래 링크는 비교 당시 커밋에 고정되어 있다.

- tikitaka: [CLI](https://github.com/rht-22/ai-video/blob/8961e3b8a32db1a306f41bffd6d98439bce6ad1c/app/tikitaka/cli.py), [대본 검증·재순위](https://github.com/rht-22/ai-video/blob/8961e3b8a32db1a306f41bffd6d98439bce6ad1c/app/tikitaka/rebuild.py), [영상 확인](https://github.com/rht-22/ai-video/blob/8961e3b8a32db1a306f41bffd6d98439bce6ad1c/app/tikitaka/verify.py), [테이블](https://github.com/rht-22/ai-video/blob/8961e3b8a32db1a306f41bffd6d98439bce6ad1c/app/tikitaka/table.py), [모델 처리](https://github.com/rht-22/ai-video/blob/8961e3b8a32db1a306f41bffd6d98439bce6ad1c/app/tikitaka/llm.py).
- human-flow: [CLI](https://github.com/rht-22/ai-video/blob/00635374fa9a85ff8fd9651329538e26555e5762/app/v3/cli.py), [파이프라인·캐시·산출](https://github.com/rht-22/ai-video/blob/00635374fa9a85ff8fd9651329538e26555e5762/app/v3/pipeline.py), [human 체인](https://github.com/rht-22/ai-video/blob/00635374fa9a85ff8fd9651329538e26555e5762/app/v3/story_flow/__init__.py), [여러 편 계획](https://github.com/rht-22/ai-video/blob/00635374fa9a85ff8fd9651329538e26555e5762/app/v3/plan.py), [활용 불가 구간](https://github.com/rht-22/ai-video/blob/00635374fa9a85ff8fd9651329538e26555e5762/app/v3/banned.py).

```bash
# 로컬에 fetch된 커밋으로 같은 비교 재현
git rev-list --left-right --count 8961e3b...0063537
git merge-base 8961e3b 0063537
git diff --stat 8961e3b 0063537
git diff --name-status 8961e3b 0063537
git log --left-right --oneline 8961e3b...0063537

# 각 브랜치가 공통 기반에 추가한 변경을 별도로 확인
git diff --stat 74df3ce 8961e3b
git diff --stat 74df3ce 0063537
git show 8961e3b:app/tikitaka/cli.py
git show 0063537:app/v3/cli.py
```

위 수치와 설명은 문서 파일을 추가하기 전의 고정 커밋을 기준으로 한다. 기존 미추적 파일과 이 문서는 브랜치 diff 집계에 포함되지 않는다.
