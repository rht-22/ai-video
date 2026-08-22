# 스토리 구성 폴백 줄이기 — 계측 결과 기반 실행안

작성 2026-08-23. 계기: `compose_story_with_context` 가 조용히 `_build_fallback_story` 로
떨어지던 문제(폴백률 최근 21일 약 7% · 반려 후 재생성 run 16.7% · 일반 4.5%,
2026-08-22 clip_metadata 실측). 사례: clip `29ad59d0-c68d-4bce-b2d6-843fd851cb9a`
(한 입 주막/가왕쇼, run `가왕쇼_240dd2d0`) — hook 420~455s + payoff 2750~2775s 로
원본에서 38분 떨어진 두 덩어리, 제목은 `작품명[:20] + 설명[:20]`.

이 문서는 **폴백을 눈에 보이게 만든 변경(이번 PR)** 다음에 무엇을 어떤 근거로 건드릴지 정리한다.
발주서 4번("출력량·thinking_level 을 임의로 바꾸지 말고 근거를 붙여 제안")에 대한 답이다.

## 0. 이번 PR 이 만든 계측 (선행 조건)

폴백이 나면 아래가 남는다 — 지금까지는 실행 노드 stdout 에만 있었다.

- `run_log.json` → `steps[{step:"story"}]` : `fallback` · `fallback_reason` · `storyline_count` · `elapsed`
- `checkpoint_story.json` 최상위 : `fallback` · `fallback_reason` (격리 파이프라인이 그대로 `clip_metadata` 로 싣는다)
- `fallback_reason` : `kind`(`max_tokens`/`json_decode_error`/`validation_error`/`empty_response`/`empty_text`/`exception`)
  · `finish_reason` · `usage`(prompt/thoughts/candidates/total 토큰 수) · `response_chars` · `response_head`(300자)
  · `attempt` · `model` · `thinking_level`

**아래 어떤 안도 `kind` 분포를 먼저 세고 나서 실행한다.** 표본 없이 프롬프트를 건드리면
"잘림이 문제였다" 는 가설을 검증 없이 굳히게 된다 — 실제로 아래 3번은 잘림이 아니라 파싱 실패다.

집계 기준(제안): 폴백 20건 또는 2주 중 먼저 도달하는 쪽.

## 1. 잘림이 아닌 폴백이 섞여 있다 — `_loads_first_json` 이 스토리 경로에만 없다

**근거(코드).** 분석 경로는 `_loads_first_json`(gemini_client.py:1683)으로 파싱한다.
이 함수는 SDK 의 `response.text` 가 답변 파트를 구분자 없이 이어붙여 `{...}{...}` 가 되는
사고를 구제한다 — 2026-08-03 실측 **분석 호출 22회 중 12회 파싱 실패**가 이 유형이었고
그래서 도입됐다. 스토리 경로는 같은 SDK·같은 `response_mime_type="application/json"` 인데
`json.loads(json_text)`(gemini_client.py:2058) 그대로다. 이 유형이 오면 `Extra data` 로
3회 다 실패하고 폴백이다.

**예상 효과.** `kind=json_decode_error` 중 `response_head` 가 정상 JSON 으로 시작하고
`finish_reason=STOP` 인 건이 이 유형이다. 8/3 분석 경로 비율(55%)이 스토리에도 비슷하게
적용된다면 폴백의 상당수가 여기서 사라진다.

**위험.** 없음에 가깝다 — `raw_decode` 는 `json.loads` 가 성공하는 입력에 대해 같은 값을
돌려주고(꼬리 0자), 실패하던 입력만 살린다. 실패 시 원문 보존(`_dump_gemini_response`)도
분석 경로와 같은 방식으로 붙일 수 있다.

**상태: 미적용.** 이번 PR 범위(발주 1~3번) 밖이라 손대지 않았다. 표본에서 이 유형이 하나라도
보이면 바로 별도 PR 로 올릴 것을 권한다 — 우선순위 1순위.

## 2. 출력량: 버려지는 출력부터 없앤다 (품질 영향 0)

프롬프트가 요구하는 출력 중 **코드가 읽지 않거나 덮어쓰는 것**이 있다. 잘림을 줄이는 데
가장 싼 수단이고, 스토리라인 개수를 줄이는 것과 달리 결과물 품질을 낮추지 않는다.

| 출력 필드 | 근거 | 비용 |
|---|---|---|
| `selected_storyline`("선정된 인덱스의 객체를 **그대로 복사**해서 출력", gemini_client.py:1150) | 코드가 무조건 덮어쓴다: `result["selected_storyline"] = storylines[selected_idx]` (gemini_client.py:2078). 모델이 쓴 복사본은 **파싱 직후 버려진다**. | storyline 1개분 = 전체 출력의 약 1/4 |
| `viral_titles`(storyline 당 3개, 1092·1142행) | 성공 경로 소비처 없음. 유일한 참조가 폴백 빌더(gemini_client.py:2294). | storyline × 3문장 |
| `narrative_plan`(1030행에서 "먼저 작성하라") | 스키마 예시에 없고 읽는 코드도 없다. | 불명 — 모델이 실제로 내보내는지부터 확인 |

**권고.** ① `selected_storyline` 은 "인덱스만 출력하라(`selected_storyline_index`)" 로 바꾼다.
② `viral_titles` 는 제거하거나 1개로 줄인다. ③ `narrative_plan` 은 건드리지 않는다 —
출력 안 CoT 로 작동해 선정 품질을 받치고 있을 수 있고, 지우면 그 효과까지 같이 지운다.

※ `_validate_story_response` 9-B(`selected_storyline` 만 온 응답을 `storylines` 로 wrap,
2344행)는 폴백의 폴백이라 ①의 영향을 받지 않는다.

## 3. `max_output_tokens` 비대칭 — 스토리 경로만 안 정해져 있다

**근거(코드).** 분석 경로는 `max_output_tokens=65536`(gemini_client.py:1639)을 명시한다.
주석에 "기본 한도에서 JSON이 잘리는 문제 방지" 라고 도입 이유가 적혀 있다.
스토리 경로(gemini_client.py:2008~2018)는 이 값을 안 준다 — 모델 기본값을 그대로 쓴다.
스토리 프롬프트는 고정부만 23,720자(585줄)이고 여기에 후보 전량 + 관계 그래프 + editorial
블록이 더 붙는다.

**확인 방법(이번 PR 계측으로 자동).** 첫 `kind=max_tokens` 표본의
`fallback_reason.usage.total_token_count` 를 본다.
- 65,000 근처 → 이미 모델 최대치. 이 안은 무효, 2번·5번으로 간다.
- 그보다 한참 낮은 값(8k/16k/32k대)에서 끊겼다 → **기본값이 낮다는 뜻이므로 분석 경로와
  동일하게 명시**한다. 한 줄 변경으로 그 유형이 사라진다.

지금 이 워크트리엔 API 키가 없어 모델 기본값을 조회하지 못했다 — 그래서 값을 넣지 않고
판별 기준만 남긴다.

## 4. `max_storyline_candidates` 는 지금 아무 일도 하지 않는다 (죽은 노브)

**근거(코드).** `app/config.py:161` 에 `max_storyline_candidates: int = 6` 이 있지만
레포 전체에서 이 이름을 읽는 코드가 없다(참조: 정의 1곳 + `provenance.py:86` 주석 1곳).
프롬프트는 개수를 **하드코딩**한다 — "스토리라인 3개를 구성하라"(gemini_client.py:576),
"3개 모두 독립적으로"(579), "3개 스토리라인 독립성"(780).
`compose_story_with_context` 의 독스트링 "하이라이트형 3개 + 서사형 3개"(=6)도 실제
프롬프트와 어긋난 잔재다.

**따라서 발주 4번의 "6 → 낮춤" 은 지금 상태로는 아무 효과가 없다.** 실제 출력량은 3이다.

**권고.** 둘 중 하나. ⓐ 노브를 프롬프트에 실배선해서 진짜 레버로 만든다(3→2 가 유효해짐).
단 `provenance` 가 이 값을 "핵심 레버" 로 검수 화면에 노출하므로, 배선 전까지는
운영자가 없는 레버를 돌리고 있는 셈이다. ⓑ 지운다. 어느 쪽이든 독스트링(1924행)은 고친다.
같은 이유로 `gemini_story_max_retries`(config.py:162)도 죽은 값이다 — 스토리 경로는
`GeminiConfig.max_retries`(env `GEMINI_MAX_RETRIES`)를 쓴다.

## 5. `story_thinking_level` — **먼저 내리지 말 것**

현재 `medium`(dataclass 1468행 · env 기본 2429행 동일).

**내리지 말자는 근거.**
1. 이 프롬프트는 585줄짜리 제약 만족 문제다 — `sequence_type` 4단계 결정 트리,
   transcript↔description 충돌 시 transcript 우선, `mode=phone_call` 일 때 제목 단어 금지와
   사후 재검증 단계까지 요구한다. thinking 을 깎으면 가장 먼저 무너지는 종류의 지시다.
   폴백률 7% 를 줄이려다 나머지 93% 의 품질을 깎으면 손해다(검수 반려는 폴백에서만 나오지 않는다).
2. 같은 문제를 먼저 만난 분석 경로(2026-07-30·31 생성 실패 3건)의 처방은 thinking 인하가
   아니라 **잘림 판별 가드 + 출력 예산 확보**였다(커밋 80b00aa). 그쪽은 지금도 `high` 다.

**내려도 되는 조건(둘 다 충족).** ① 표본에서 `kind=max_tokens` 가 폴백의 과반이고,
② `usage.thoughts_token_count` 가 `candidates_token_count` 의 2배 이상 —
즉 잘림의 주범이 출력이 아니라 추론일 때. 그때 `medium → low` 를 A/B 로 검증한다
(같은 소재 재실행 · 폴백률과 검수 반려율을 함께 본다. env `GEMINI_STORY_THINKING_LEVEL`
로 코드 변경 없이 노드 단위 비교 가능).

## 실행 순서 요약

0. (이번 PR) 계측 → `kind` 분포 표본 20건 또는 2주.
1. `kind=json_decode_error` 가 보이면 → `_loads_first_json` 을 스토리 경로에 (1번).
2. 출력 스키마에서 버려지는 필드 제거 (2번) — 표본과 무관하게 안전.
3. `usage.total_token_count` 가 낮은 값에서 끊겼으면 → `max_output_tokens` 명시 (3번).
4. 노브 정리: `max_storyline_candidates` 배선 또는 제거 (4번).
5. 위를 다 하고도 `max_tokens` 가 남으면 → thinking_level A/B (5번).
