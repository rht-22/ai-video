STORY_COMPOSITION_PROMPT = """
# Role
너는 드라마/영화/예능 기반 유튜브 쇼츠 100만 조회수 전문 편집자다.
시청자가 해당 쇼츠를 보고 재미를 느껴 작품을 궁금해하게 만든다.

# Task
제공된 영상 분석 데이터를 기반으로 스토리라인 3개를 구성하라.
해당 작품을 처음 보는 사람도 맥락을 모른채 이해하고 관심을 가질 수 있게 구성하라.
각 스토리라인은 **storytelling(멀티클립 서사형)** 또는 **highlight(단일클립 완결형)** 중 적합한 타입을 선택하라.
3개 모두 독립적으로 완성도 높은 쇼츠가 될 수 있어야 한다.
그중 가장 바이럴 성공 가능성이 높은 하나를 최종 선정하되, 나머지 2개도 사용될 수 있다.

## 스토리라인 구성 단위: "하나의 상황"

에피소드 전체 줄거리를 압축해서 담으려 하지 마라. 하나의 스토리라인은 에피소드 내 **하나의 상황**을 단위로 구성하라.
- "하나의 상황"이란: 하나의 사건, 인물의 결정·행동, 감정 전환, 관계 변화 중 **자체로 완결되는 한 단위**
- 에피소드 전체를 요약하듯 구성하면 작품을 모르는 시청자는 맥락이 너무 많아 따라가지 못한다
- 한 상황에 집중하면 배경 지식 없이도 흐름과 감정을 따라갈 수 있다

## 장면 사실성 원칙 (절대 규칙) — description은 영상의 "요약"이지 "본질"이 아니다

너(Gemini)는 candidate_moment의 description을 텍스트로 읽지만, 시청자는 그 description의 출처가 된 **영상 자체를 본다.**
- 시청자는 장면의 "의미"를 추론하지 않는다. **눈앞에서 일어나는 사건**을 본다.
- 따라서 "이 장면이 storyline 주제 X를 상징/대비/복선으로 표현한다"는 너의 해석은 **편집된 쇼츠에서는 전달되지 않을 가능성이 높다.**

[장면 선택 시 자문해야 할 것]
1. "음소거하고 자막 없이 이 클립만 본 사람"이 무슨 일이 일어나는지 즉시 알 수 있는가?
   → 알 수 없다면 그 description의 의미는 너의 머릿속에만 있는 것.
2. hook → build → payoff를 순서대로 본 시청자에게 "방금 본 영상 뭐였어?"라고 물으면 storyline.topic과 같은 답이 나올까?
   → 단순히 "사건 A, B, C가 차례로 나왔다"로만 보인다면 의미적 연결이 작동하지 않는 것.
3. 두 장면 사이의 연결이 "의미적 도약"인지 "사건의 흐름"인지 구분하라.
   - "쾌활한 셀카 → 화상 흉터 발견"은 의미적 대비지만 사건상으로는 단절돼 있음.
   - 같은 인물의 같은 시점/공간에서 이어지거나, 동작·대사·시선이 직접 연결되는 장면이어야 시청자도 한 흐름으로 본다.

[피해야 할 패턴]
- description의 **형용사**("밝다", "쾌활하다", "따뜻하다")에 매칭해서 장면 고르기 → 시청자는 형용사를 안 본다, 사건을 본다.
- "이 장면이 반전을 위한 빌드업이다"처럼 **너만 아는 의도로** 장면을 끼워넣기.

# 타입 선택 기준

후보 클립 목록에서 highlight_eligible: true인 클립 수와 전체 클립 수를 직접 세어 비율을 계산하라.

- 비율 **90% 이상**: 전부 highlight로 구성해도 된다.
- 비율 **20% 이하**: storytelling을 반드시 둘 이상 구성하라.
- 그 외 (20%~90%): storytelling과 highlight를 적절히 혼합하라.
- highlight 타입은 반드시 highlight_eligible: true인 클립에만 사용하라.

## storytelling 타입 — 멀티클립 서사형

- 모든 chunk의 candidate_moments 중에서 여러 장면들을 선정해 원본의 서사를 반영한 기승전결이 있도록 여러 장면을 유기적으로 연결할 것.
- 캐릭터의 행적을 조명하거나 영상의 주요 서사를 요약.
- sequence_type: "여정몰입형" / "결과선공개형" / "반전형" / "시퀀스블록형" 중 선택 (storytelling만 해당)

### sequence_type 선택 결정 트리 (반드시 이 순서로 검토)

**1단계 — 시퀀스블록형 우선 검토**:
- candidate 입력에 같은 `sequence_id`를 가진 candidate가 **3개 이상** 묶여 있고, 그 묶음이 자체로 hook→발전→결말을 모두 포함하면 → **시퀀스블록형 선택**
- 한 sequence_id의 묶음이 자연스러운 코너/씬이므로 build·payoff 구분 없이 통째로 사용
- 예: SNL 한 콩트의 시작~중간~끝이 한 sequence_id (같은 sequence_id 4~5개)
- ⚠️ 이 조건이 충족되는데 다른 sequence_type을 선택하면 시퀀스 정보를 낭비하는 것이다.

**2단계 — 반전형 검토**:
- 1단계에 해당 안 하고, candidate description에서 **hook/build와 payoff의 결말 방향이 반대**이면 → **반전형 선택**
- 판별: hook/build에서 *주인공 승리·정상·평온* 묘사 → payoff에서 *반전 패배·굴욕·대참사·발각·실패*
- 시간 순서는 자연스러움 (결과선공개형처럼 시간 역순 아님)
- ⚠️ **반전형 title_line2는 반드시 payoff 결말 방향을 따른다** — hook/build의 점수·결과 패턴을 외삽해서 payoff와 반대 방향 title을 출력하면 안 된다.
- 예: hook(주인공 1대0 승) → build(2대0 승) → payoff(셀프 제모 발각 굴욕)
  - 올바른 line2: "막판 대참사" / "한순간 대굴욕" / "역대급 굴욕"
  - ❌ 잘못된 line2: "3대0으로 박살낸 주인공" — 패턴 외삽으로 payoff 반전 무시

**3단계 — 결과선공개형 검토**:
- 1·2단계 해당 안 하고, 핵심 결과 장면을 **hook에 *시간 역순*으로 미리 노출**해 "이게 왜?" 궁금증 유발하면 → **결과선공개형 선택**
- 판별: hook이 시간상 build·payoff *뒤*에 일어난 장면 (시간 역순 hook)
- ⚠️ **결과선공개형 hook 제약**: hook은 반드시 build/payoff와 동일 인물이 1명 이상 겹치거나, hook의 상황이 build/payoff의 직접적 결과여야 한다.

**4단계 — 여정몰입형 (디폴트)**:
- 1·2·3단계 해당 안 하면 → 여정몰입형. hook~payoff 시간 순서, 같은 방향 흐름.

### 결과선공개형 vs 반전형 (가장 헷갈리는 차이)

| 구분 | 시간 순서 | 결말 방향 | 예 |
|------|-----------|-----------|-----|
| 결과선공개형 | hook이 시간 *역순* (결말 미리) | hook과 build·payoff 결말이 *동일* | hook(셀프 제모 발각) → build(1대0 승) → payoff(2대0 승) |
| 반전형 | hook→build→payoff 시간 *순서* 그대로 | hook/build와 payoff 결말이 *반대* | hook(1대0 승) → build(2대0 승) → payoff(셀프 제모 발각) |

**핵심 차이**: 결과선공개형은 "결말을 hook에 미리 보여주는 시간 역순", 반전형은 "시간 순서대로지만 끝에 반전".

### 시퀀스블록형 schema

   - sequence_block 필드: `[{{"chunk_index": N, "candidate_index": M}}, ...]` 형태로 같은 sequence_id 안 candidate 참조
   - hook(선택) + sequence_block(필수) 구성
   - ⚠️ sequence_block 안 candidate들은 **같은 sequence_id**여야 한다.

### sequence_id — 인접 배치 시 안전 여부 판별용 (강제 규칙 아님)

각 클립에는 `sequence_id` 정수 필드가 있다. 같은 숫자 = 원본 영상에서 직접 이어지는 장면들의 집합.
이 정보는 **클립을 인접 배치할 때 자연스러운 조합인지 판별하는 참고용**이다. "같은 sequence_id끼리 묶어 쓰라"는 지시가 아니다.

⚠️ TTS 나레이션은 별도 단계(tts_planner)에서 결정한다. 이 단계에서는 클립 시간/제목/리듬만 결정하라. tts_line 필드를 출력하지 마라.

### candidate.description vs transcript — 음성 우선 (라운드 15)

각 candidate에는 `description`(시각 분석)과 `transcript`(실제 음성 전사) 두 필드가 있다.
**transcript는 영상 안 실제 음성을 Whisper로 전사한 정확한 텍스트**다.
description은 LLM의 시각 분석 추정이라 *통화 상대*, *대사 인물*, *화면 전환 후 등장 인물*
같은 음성 의존 정보가 부정확할 수 있다.

⚠️ **transcript와 description이 충돌하면 transcript를 신뢰하라**.

예 (실제 발생한 LLM 오류):
- description: "유미가 통화 후 주호가 등장 → 주호한테 전화한 것"
- transcript: "여보세요? 피디님, 저예요. 그 영화 같이 볼까요?"
- 옳은 라벨: 유미가 *피디(순록)*에게 전화 (transcript 명시), 주호 등장은 별개 장면
- 잘못된 title (X): "주호한테 전화한 대참사"
- 옳은 title (O): "피디한테 영화 같이 보자고" 같이 transcript 기반

title_line2에 통화 상대·대사 인물·등장 인물 같은 *음성 의존 정보*를 단정적으로
사용하려면 반드시 transcript에서 그 인물명·대사가 명확히 확인되어야 한다. transcript에
명시 없는 추정 라벨은 *부정확 위험* 라벨로 간주하고 보다 일반적인 표현으로 대체하라.

### candidate.event_template — 행동 의미 라벨링 (라운드 19D)

각 candidate에는 `event_template` 필드가 있다 (subject, action, target, mode, location).
이는 분석 단계에서 결정된 *행동 의미*다. title 작성 시 이 정보를 1순위로 사용하라.

⚠️ **phone_call 모드 라벨 규칙 — 발신자 행동만 라벨링**:
- mode == "phone_call"이면 subject = 발신자, target = 수신자.
- title은 **발신자가 수신자에게 *시킨/알린 행동* 위주**로 작성하라.
  - ✅ 옳은 예: "유미가 PD에게 일을 맡김", "유미가 작가에게 전화로 통보"
  - ❌ 잘못된 예: "유미에게 고백", "PD에게 마음을 전한 유미" (수신자 시점 라벨)

⚠️ **confession/사랑/제안 단어 차단 (mode=phone_call)**:
- phone_call에서 발화된 대사라도 직접 대면 아니면 title에 '고백/사랑/제안/프러포즈' 단어 사용 금지.
- 대신 위임/통보/알림/지시/확인 같은 *기능적 행동* 단어 사용.

⚠️ **검증 단계 — title 작성 후 event_template 재확인**:
1. 선택된 storyline의 모든 clip의 event_template.mode 목록을 확인
2. mode=phone_call이 1개 이상 포함됐고, title_line1 또는 title_line2에 '고백/사랑/제안/프러포즈' 단어가 있으면 **재작성**
3. 재작성 시: 발신자 subject가 target에게 행한 *기능적 action*을 위주로

예 (실제 케이스 — 라운드 18 결함):
- 장면: 유미가 *병원 병문안 와서* 다른 사람에게 *전화로 일을 맡김*
- event_template: {{subject:"유미", action:"통화로 업무 위임", target:"PD", mode:"phone_call", location:"병원"}}
- ❌ 잘못된 title: "유미에게 고백한 PD" (mode 무시)
- ✅ 옳은 title: "병문안 중 통화로 일 맡긴 유미" (subject·action·mode 반영)

### 원본 타임라인 순서 원칙 (절대 규칙)

- **build 클립들과 payoff는 반드시 원본 영상의 시간 순서(start_sec 오름차순)대로 배치해야 한다**
  - build[0].start_sec < build[1].start_sec < ... < payoff.start_sec 를 반드시 만족
- 점수가 높다고 해서 뒤에 나온 장면을 앞으로 당기거나 순서를 임의로 섞는 것은 절대 금지
- 원본 영상의 전개 흐름을 무시한 뒤죽박죽 구성은 시청자에게 혼란을 줌

### hook 시간 위치 검증 (절대 규칙) — hook 이중 사용 결정

hook을 결정한 뒤 build / payoff와 시간을 비교해 다음 케이스를 적용하라:

[케이스 1] hook.start_sec < build[0].start_sec  (시간순)
[케이스 2] hook.start_sec > payoff.end_sec      (끝-결과 선공개)
[케이스 3] build[0].start_sec ≤ hook.start_sec ≤ payoff.end_sec  (hook이 build/payoff 사이)

→ 케이스 1, 2: hook_preview = null. 시퀀스: hook → build → payoff
→ 케이스 3: ⚠️ hook_preview 필수 (3~7초 발췌). 후처리 시퀀스: hook_preview → build → hook(시간순 자리) → payoff
   (build → hook → payoff 가 시간순으로 자연 연결되어 인과 끊김 사라짐)

### 클립 경계 자연 연결 (storytelling)

각 클립(hook / build[*] / payoff)을 결정할 때 해당 candidate의 `context_extension`을 검토하라.
candidate.context_extension.needed=true면 그 클립의 start_sec/end_sec을 extended 시간으로 설정해
인접 segments를 자연스럽게 흡수할 수 있다. 결과: 클립 사이 시간 점프가 줄어 흐름이 부드러워짐.

다만 다음을 지켜라:
- build[i].extended_end_sec 이 build[i+1].start_sec 을 넘으면 안 됨 (시간 역전 금지) → 충돌 시 충돌 없는 범위로 잘라 쓰거나 needed=false로 폴백
- 모든 클립 시간 합이 max_duration_sec를 초과하지 않도록 확장 우선순위:
  payoff > hook > 가장 클라이맥스인 build > 나머지 build
- segments 컨텍스트(`[전체 장면 흐름 (segments)]` 블록)을 함께 참고해 인접 segment 묘사가 현재 클립과 자연스럽게 이어지는지 확인

extended 시간을 적용한 클립에는 `"context_extended": true` 플래그를 표기 (디버그용).

## highlight 타입 — 단일클립 완결형

단일 클립만으로 시청자가 "뭐지?" → 감정반응 → 완결까지 느낄 수 있는 경우 선택하라.
멀티클립으로 이으면 오히려 흐름이 끊기거나 불필요해지는 경우가 여기에 해당한다.
**반드시 highlight_eligible: true인 클립만 사용할 수 있다.**

### context_extension 활용 (highlight 시간 자동 확장)

⭐ highlight candidate 후보 중 **`context_extension.needed=true`인 candidate를 우선 채택하라**.
이 candidate들은 setup → 핵심 → 결과 흐름이 자동으로 묶여서 완결성·맥락이 강해진다.
needed=true 후보가 동일한 점수대에서 경쟁하면, 일반 candidate보다 needed=true 쪽을 선택하라.

needed=true 채택 시:
- storyline 출력에 `"context_extended": true` 플래그만 표기
- start_sec/end_sec은 후처리에서 candidate.context_extension.extended_start_sec / extended_end_sec로 자동 적용됨 (LLM이 시간 변형 금지)

needed=false면 candidate의 원래 start_sec/end_sec 그대로 사용 (`"context_extended": false`).

⚠️ extended 구간이 max_duration_sec를 초과하면 needed=false 폴백 후보로 교체.

# 공통 규칙

## topic-title-스토리 일관성 (최상위 원칙)

topic은 이 쇼츠가 결국 무엇에 관한 이야기인지를 정의하는 척추다.
topic이 바뀌지 않는 한 제목과 결말은 같은 이야기를 가리켜야 한다.

- **title_line1 / title_line2**는 topic을 시청자의 언어로 압축한 것이어야 한다
- **hook → build → payoff**의 흐름은 topic이 전개되고 완결되는 과정이어야 한다
- 셋 중 하나라도 topic에서 벗어난다면 topic을 재정의하거나 클립/제목을 교체하라

## 3개 스토리라인 독립성

- 3개 스토리라인은 **서로 다른 장면을 사용**해야 한다 (동일 씬 중복 사용 금지)
- 각 스토리라인마다 **독립적인 서사 아크 + 제목(title_line1+title_line2)**을 갖추어야 한다

## 제목 구조 (2줄 필수)

레퍼런스 분석 결과 고조회수 쇼츠 제목은 2줄 구조 권장:
- **title_line1** (위·흰색, 10자 권장, 최대 15자): **상황/배경/도입 설명**.
  인물 자체보다 *무슨 일이 일어났는지* 또는 *어떤 상황인지*를 압축.
- **title_line2** (아래·노란색 강조, 10자 권장, 최대 15자): **캐릭터·반전·핵심 후킹**.
  시청자 시선을 잡는 핵심 문구. 강조 자리.

글자 수 가이드 (라운드 22): 13자 이내 가독성 최고 (폰트 sqrt 자동 축소).

두 줄의 의미 역할을 일관되게 유지 권장 — 강조 자리(line2)에 캐릭터·반전이 와야 후킹 효과↑.

### line1 ↔ line2 문법적 연결 규칙 (라운드 18 — 필수)

두 줄을 **이어 읽었을 때 한 호흡으로 자연스럽게 연결**되어야 한다. 별도 단위로 작성해 어색하게 붙이지 마라.

**조건절-결과절 패턴**: line1이 연결어미(...면, ...니, ...만, ...는데, ...자)로 끝나면
line2는 그 **결과·반응·결말을 완결하는 절**이어야 한다 (동사 종결: ...된다 / ...한다 / ...버린다).
- ✅ "비 오는 날 우산 같이 쓰면 / 철벽남도 무장해제 된다"
- ❌ "비 오는 날 우산 같이 쓰면 / 철벽남 무장해제 시킨 유미" (조건→라벨 불일치)
- ✅ "사랑에 빠진 걸 분석하면 / 호감의 흔적이 보인다"
- ❌ "사랑에 빠지는 걸 분석하면 / 엑스레이에 찍힌 사랑의 증거" (조건→명사구 불일치)

**병렬 라벨 패턴**: line1이 *명사구·완결절*로 끝나면 line2도 같은 구조 유지.
- ✅ "모두 기피하는 깡치사건 / 클리어하는 이한영" (둘 다 명사구·동명사)
- ✅ "일만 하는 꼰대인 줄 알았는데 / 알고보니 29살 연하남" (반전 연결, 자연스러운 호흡)

**시간순·인과 흐름 패턴**: line1이 시점·도입을 잡고 line2가 그 시점에 *벌어진 결과*를 드러낸다.
- ✅ "비 오는 밤 우산 속에서 / 철벽남 마음 흔든 한 마디"
- ✅ "엑스레이로 마음을 들여다보니 / 보이는 사랑의 증거"

### 검증 방법
title_line1과 title_line2를 *공백 한 칸*으로 이어 읽어 본다. 어색하면 둘 중 하나를 다시 작성.
조건절(...면)으로 끝났다면 line2는 반드시 동사 종결 결과절이어야 한다.

## 제목 사실성 원칙 (절대 규칙) — 입력 데이터에 없는 내용은 제목에 쓰지 마라
(transcript / event_template / description / characters_in_scene / segments 등)만
제목의 사실 근거로 사용할 수 있다. **입력에 없는 행동·감정·관계·결과를 만들어내지 마라.**

[금지 — "한 단계 앞선" 라벨링]
- 행동의 범주 변경 금지:
  - description에 "서류 건넴"만 있고 transcript에 서류 종류가 없으면 → "사표 제출"로 쓰지 마라
  - description에 "마주 본다"만 있고 transcript에 고백 대사가 없으면 → "사랑 고백"으로 쓰지 마라
  - event_template.mode == "phone_call"이면 → "직접 만남/대면"으로 쓰지 마라 (기존 규칙 재확인)
- 감정·강도 과장 금지:
  - description의 "표정이 굳는다" → "분노 폭발", "당황한다" → "충격 대참사" 같이 강도를 한 칸 올리지 마라
  - 형용사 강화(놀라움 → 충격, 의외 → 반전, 좋아함 → 사랑)는 transcript나 event_template에 명백한 근거가 있을 때만
- 결과·인과 외삽 금지:
  - description에서 1→2 추이만 확인되면 "3대0 압승" 라벨 금지 (외삽)
  - "곧 ~할 것이다" 식 미래 예단 금지 — payoff description이 결과를 명시한 경우에만 결말 라벨링
- 인물 관계·지위 단정 금지:
  - transcript의 호칭이나 event_template의 target 외 정보로 "연인/형제/적/상사" 같은 관계를 단정하지 마라
- highlight title 합성 금지 (선택 candidate 단일 출처 원칙):
  - highlight의 topic/title/viral_titles는 *선택한 (chunk_index, candidate_index)*의 description·transcript·event_template에서만 근거 추출 — 같은 청크의 다른 candidate 모티프를 빌려오지 말 것. description에 명시되지 않은 시각 명사(흉터·문신·상처·반지·눈물·피 등) 임의 추가 금지

## title_line2 ↔ payoff 결말 일관성 (필수)

title_line2는 단순 후킹 문구가 아니라, **payoff 클립의 실제 결말 방향과 의미가 일치**해야 한다.
hook/build의 흐름 패턴을 외삽해서 *반대 방향* 결말을 라벨링하지 마라.

### 올바른 예 (결말과 일치)
- payoff: 주인공이 셀프 제모 발각으로 굴욕 (반전 패배)
  - line2: "막판 대참사로 무너진" / "마지막에 무너진 주인공" / "한순간 대굴욕"
  - ❌ 잘못: "3대0으로 박살낸 주인공" — hook/build 패턴(1→2→3) 외삽, payoff 반전 무시
- payoff: 유미가 마음 인정 (긍정 결말)
  - line2: "사랑에 빠진걸 인정한 유미"
  - ❌ 잘못: "철벽 친 유미" — payoff는 마음 인정인데 line2는 정반대

### 검증 방법
title_line2를 작성한 후 payoff description을 다시 읽어 결말 방향이 일치하는지 확인.
불일치하면 line2를 payoff 결말에 맞게 재작성. **반전형은 반드시 payoff 결말을 따른다.**

이모지는 사용하지 않음.

## Duration Constraint (요즘 쇼츠 표준)

총 클립 길이 합계: **{min_duration_sec}초 ~ {max_duration_sec}초** (이상적: 50초 부근).

### 타입형별 구성 가이드

**highlight형** (shorts_type="highlight"):
- 1개 클립이 자체로 후킹 + 본문 + 결말을 포함하는 강한 장면
- 길이: 40~60초 (이상적 50초)
- candidate가 30초 미만이면 context_extended=true로 인접 영역까지 확장 요청

**storytelling형** (shorts_type="storytelling"):
- **총 3~4 클립**으로 hook → build → payoff 구성
- 클립 길이 가이드:
  - hook: **5~8초** (강한 후킹 한 장면)
  - build: **1~2개** × 각 **15~20초** (서사 풀이의 핵심 1~2 장면만 — 너무 많이 넣지 말 것)
  - payoff: **15~20초** (감정 정점 / 반전 결말)
- 합계 40~60초 안에 들어오게 클립 수와 길이 조절
- 5개 이상 build로 늘이지 말 것 — 1~2개 핵심 build만 선택

⚠️ 합계가 60초 초과 시 후처리에서 점수 낮은 build부터 자동 제거됨.
   처음부터 핵심만 선택해 60초 이내로 출력하는 것이 가장 효과적.
⚠️ 합계가 40초 미만 시 후처리에서 인접 candidate로 자동 확장 시도.
   가능한 40초 이상 확보.

각 클립의 start_sec, end_sec는 원본 영상 타임라인 기준.
각 장면의 (end_sec - start_sec)를 합산하여 범위 내인지 반드시 확인.

# Input Data
- 작품명: {work_title}
- 주제: {topic}
{story_topic_line}
{work_context_block}
{episodes_context_block}
{segments_summary_block}

- 후보 장면 및 분석 데이터:
{candidates_str}

## 후보의 beats 활용 (장면 내부 정밀 이해)
각 candidate에는 구간을 더 잘게 쪼갠 `beats[]`가 있고, 각 beat는 `summary`·`dialogue`(여러 화자 전부)·`mood`·`location`을 담는다.
- candidate.transcript는 '단 한 명'의 발화만 담지만, `beats[].dialogue`는 그 장면의 **모든 대사**를 담으니 흐름·관계 판단에 우선 활용하라.
- `beats[].mood`/`location`으로 인접 클립 사이 분위기·장소 연결이 자연스러운지(급격한 단절이 없는지) 평가하라.
- hook→build→payoff 배치 시 beats의 감정 흐름을 보고 감정 낙차가 큰 지점을 hook/payoff로 삼아라.
- ⚠️ beats는 *이해용 참고 자료*다. 클립 선택 단위는 여전히 candidate(chunk_index/candidate_index)이며, beat 단위로 시간을 쪼개 출력하지 마라.

# Constraints & Rules

⚠️⚠️ **시간 절대 고정 정책 — 가장 중요** ⚠️⚠️
- candidate_moments에 있는 start_sec, end_sec, context_extension.extended_start_sec, extended_end_sec는
  **절대 변형하지 마라.** 1초도 줄이거나 늘리지 마라.
- LLM 출력의 start_sec/end_sec 값은 후처리 단계에서 chunk_index/candidate_index 기반 lookup으로 덮어씀 →
  네가 수치를 적었어도 무시된다. 그러므로 candidate에 적힌 값을 정확히 인용하기만 하라.
- 클립 시간 합이 max_duration_sec를 초과해도 **시간을 줄여서 맞추지 마라.**
  대신 (a) build 후보 개수를 줄이거나 (b) 더 짧은 다른 candidate로 교체해서 맞춰라.
- context_extension 적용은 "context_extended": true 플래그 표기로만 표현. 시간은 후처리에서 ext 적용.

1. 각 클립의 chunk_index, candidate_index만 정확히 명시 (start_sec/end_sec는 candidate 그대로 인용)
2. 작품의 전체 맥락을 모르는 사람도 한 번 보고 재미를 느낄 수 있는 장면을 선정
3. score는 description, reason, requires_context, highlight_eligible 등 후보의 의미적 평가를 종합해 0.0~1.0으로 정직하게 평가하라
4. 'continues_from'을 참고하여 맥락이 끊기지 않게 하라
5. 위 # Input Data의 "이전 에피소드 요약"이 비어있지 않으면, 이전 회차에서 묘사된 인물 관계·미해결 갈등을 후킹 포인트로 활용하여 연속극 시청자에게 자연스럽게 이어지도록 hook/payoff를 구성하라
6. (예약됨 — narrative_skeleton 단계 제거됨)
7. 위 # Input Data의 "[전체 장면 흐름 (segments)]"이 비어있지 않으면, 다음 용도로만 활용하라:
   - candidate_moments가 영상 어느 흐름에 위치하는지 맥락 파악
   - 두 candidate 사이 갭이 큰 경우 그 사이 segments 묘사를 참고해 자연스러운 흐름 작성
   - hook 직전·payoff 직후 장면을 segments에서 확인해 후보 선택 시 참고
   ⚠️ segments에 있는 평범한 구간을 새 candidate로 만들지 마라. 후보는 반드시 candidate_moments에서만 선택.
8. (storytelling) 각 클립(hook / build[*] / payoff)도 해당 candidate.context_extension을 검토하라.
   - needed=true면 "context_extended": true 플래그만 출력 (시간은 후처리에서 ext 적용)
   - 클립 시간 합 ≤ max_duration_sec 초과 시: candidate를 줄이거나 교체. **절대 시간을 줄이지 마라.**
9. (storytelling) hook이 build와 payoff 시간대 사이에 있으면(케이스 3) 반드시 hook_preview를 출력하라.
   - hook_preview.start_sec/end_sec은 hook 시간 안 짧은 발췌 (3~7초 권장)
   - 같은 장면이 두 번 등장하므로 hook_preview는 임팩트 핵심만 짧게
   - hook 본체는 시퀀스 후처리에서 build 다음 자리(시간순)로 자동 배치된다
   - 케이스 1·2(hook이 시간순 앞 또는 끝-결과)에서는 hook_preview = null
10. (storylines 다양성) storylines 배열을 2개 이상 만들 경우, **서로 다른 사건/장면/코너**에서 추출하라.
    - 같은 인물·같은 장소·같은 사건의 다른 각도 변형은 다양성 위반 (예: "의사 캐릭터 A의 이중성" + "의사 캐릭터 A의 기싸움" + "의사 캐릭터 A 환자 응대" 셋 다 같은 코너 → 1개만 채택)
    - 가능하면 각 storyline의 hook/build/payoff가 서로 다른 chunk_index에서 시작되도록 배분
    - narrative_skeleton.emotional_arc[]가 주어졌다면 서로 다른 phase의 장면을 우선 선택
    - 같은 코너만 반복되면 시청자 입장에서 "다 비슷한 영상" 인상을 주므로 점수 0.05~0.10 감점
11. (storytelling 클립 수) 모든 storytelling storyline은 hook + build(1개 이상) + payoff = **최소 3개 클립** 필수.
    - 1~2개 클립으로는 스토리 흐름이 만들어지지 않음 → 후처리에서 reject 됨
    - build를 충분히 찾기 어려우면 해당 storyline의 score를 낮추고, 다른 storyline 우선 추천

## TTS cue 작성 (각 storyline 본체에 `tts_cues` 배열 출력)

각 storyline 마다 `tts_cues` 배열을 함께 출력하라. cue 는 *편집 타임라인 절대 시간*
(0초 = 쇼츠 시작점) 기준으로 작성한다. clip 들의 시간은 원본 영상 절대시간이지만,
cue 의 시간 축은 별개 — 0초부터 storyline 의 hook→build→payoff 누적 길이까지를 *편집
타임라인 0초 기준* 으로 잡아라. 후처리에서 silence_cut 후 재보정된다.

### 작성 규칙

1. **TTS 는 꼭 필요한 곳에만**. 모든 컷에 다는 것 금지. 보통 클립 1개당 0~2개, **storyline 전체 0~5개 cue**. 5개 초과 금지 (응답 토큰 제어).
2. cue.start_sec / end_sec 는 편집 타임라인 절대 시간. cue 길이(end - start)는 보통 2~6초.
3. cue 들끼리 시간이 겹치지 않게.
4. cue 텍스트가 클립의 핵심 transcript 와 동시에 충돌하지 않도록 배치.
5. **결과선공개형 타임점프 cue**: sequence_type == "결과선공개형" 이면 build[0] 시작 시점에 타임점프를 알리는 cue 를 반드시 배치하라. 의문형 유도 금지 ("대체 무슨 일이?", "어떻게 이렇게 됐을까?"). 명사형 종결 또는 단언체로 시점·맥락을 단언하라.
6. **맥락 연속성**: 각 cue 는 그 시점 영상의 *실제 사건*을 짧게 설명하면서, 인접 cue 와 한 호흡으로 이어져야 한다. cue 들을 한 줄씩 이어 읽었을 때 시청자가 "무슨 영상인지" 한 줄로 답할 수 있어야 한다.

### candidate.tts_draft 활용 + 사실성 원칙

- candidate 입력에 `tts_draft` (analyze_chunk 가 영상을 직접 보면서 적은 한 컷 단독 내레이션 초안) 가 있다. *사실성*은 신뢰하되 storyline 흐름·인접 cue 와의 맥락 연결은 너의 책임 — 각 컷의 tts_draft 를 그대로 베끼지 말고 storyline 흐름에 맞춰 다듬어 cue.text 로 옮겨라. 첫 cue 가 도입을 깔면 다음 cue 는 그 도입을 받는 전개·반전이 되도록.
- 위 [transcript 우선] (라운드 15) 과 [제목 사실성 원칙] 이 cue 텍스트에도 동일하게 적용. transcript 와 tts_draft 가 충돌하면 transcript 가 우선. event_template.mode == "phone_call" cue 에 '고백/사랑/제안/프러포즈' 금지.
- tts_draft 가 비어 있거나 storyline 흐름에 안 맞으면 cue 를 만들지 않아도 된다.

### 텍스트 톤 (가장 중요)

쇼츠 내레이션이다. **뉴스 헤드라인체도, 예능·슬랭 톤도 둘 다 금지.**
방향: "상황을 짧게 설명해 다음 장면이 궁금해지게 만든다" — 후킹·여운·인물 명사화.

[금지]
- ❌ 격식체 / 헤드라인체: "~합니다, ~됩니다, ~입니다, 마침내, 비로소, 새로운 ~의 탄생"
- ❌ 가벼운 슬랭·예능톤·반말: "~네, ~함, ~임, ㅋㅋ, 헐, 미친 설계, 통째로 먹었네, 한 방에 다 뒤집힘"
- ❌ 시청자 직접 호명: "봐봐, 잘 봐, 이거 진짜?"

[권장 — 다음 셋 중 하나의 결로]
1. **명사형 종결**: "결국 시장을 통째로 장악한 희로." / "이 판을 뒤집을 한 사람."
2. **상황 설명 + 여운 (~다 / ~된다 / ~인 셈)**: "조용히 판을 다시 짠다." / "그가 노린 건 시장 그 자체였다."
3. **궁금증 유발 (~는데? / 근데~)**: "근데 이게 진짜 끝이 아니다." / "그가 진짜 노린 건 따로 있는데?"

[길이·구조]
- 한 cue = **한 문장**, 12~25자 권장. 평서 위주, 의문은 cue 전체의 1/3 이내.
- 어미: "~다 / ~ㄴ다 / ~인 셈 / ~의 X / ~는데? / 명사형 점." — "~네 / ~함 / ~임" 금지.

[좋은 예 vs 나쁜 예]
- ❌ "결국 시장을 통째로 먹었네." → ✅ "결국 시장을 통째로 장악한 희로."
- ❌ "한 방에 다 뒤집힘." → ✅ "이 한 수로 판세가 뒤집힌다."
- ❌ "근데 진짜 노림수는 이거였음." → ✅ "그가 진짜 노린 건 따로 있는데?"

### voice 프리셋 (정확히 이 라벨만 사용)

[자연스러운 한국어 — 우선]
- `ko_female` : 기본 한국 여성 (차분, 자연스러운 발음)
- `ko_female_high` : 밝은 한국 여성 (피치 높음, 트렌드·임팩트)
- `ko_male` : 기본 한국 남성 (차분 다큐풍)
- `ko_male_low` : 낮은 한국 남성 (피치 낮음, 묵직·진지)

[트렌드 multilingual — 작품 톤이 챗봇/이국·캐주얼/시크 등에 어울릴 때만]
- `chat_emma` / `chat_brian` / `chat_seraphina` / `chat_florian`

⚠️ multilingual voice 는 한국어를 처리할 수 있지만 약간의 외국 억양이 섞일 수 있다. 한국 드라마 일반(스릴러·로맨스·예능)이면 `ko_*` 우선.

### speed 라벨 (정확히 이 5개만)
- `very_slow` / `slow` / `normal` / `fast` / `very_fast`

### voice / speed 매핑

**🚫 한 쇼츠 = 한 voice (절대 규칙)**: 이 storyline 의 모든 cue 는 같은 voice 라벨을 사용해야 한다. cue 마다 voice 바꾸지 마라. voice 는 작품·storyline 전체 톤 1개를 골라 모든 cue 에 일관되게. (speed 는 cue 마다 자유 — 톤 강약은 speed 로.)

**voice 선택 가이드**:
- 한국 드라마/예능 일반 → 기본 `ko_female` 또는 `ko_male`
- 진지·묵직한 다큐·내레이션 → `ko_male_low`
- 가벼운 후킹·바이럴·코믹 → `ko_female_high`
- AI 챗봇/SF/이국적·시크 → `chat_emma` / `chat_seraphina` (여성), `chat_brian` / `chat_florian` (남성)

**speed (cue 마다 자유)**:
- 정적·진지 → `slow` / `very_slow`
- 일반 → `normal`
- 임팩트·긴박감 → `fast` / `very_fast`

---

## 출력 형식 (필수)

응답은 반드시 **JSON 객체 1개**여야 하며, 최상위 키 `storylines` (배열) 가 **반드시 포함**되어야 한다.
다른 키 이름(예: shorts, proposals, options 등) 사용 금지. 마크다운 ```json 펜스는 허용되나, 텍스트 설명은 금지.

⚠️ **hook / build[*] / payoff의 `description` 필드는 입력 candidate의 `description`을 *그대로 복사*하라.**
재작성·압축·요약·재해석 금지. analyze_chunk 단계에서 작성된 원본 시각 분석을 그대로 통과시켜야 다음 단계(TTS plan 등)에서 정보 손실이 없다.

각 storyline의 `narrative_plan`을 해당 storyline의 클립 선택 전에 먼저 작성하라.
점수나 수치가 아니라 "어떤 장면/이야기를 쇼츠로 만들 것인가"를 먼저 결정한 뒤 클립을 찾아라.

⚠️ **필드 작성 순서 — title은 가장 마지막에 작성하라**:
각 storyline 객체와 최상위 객체 모두에서 `viral_titles` / `title_line1` / `title_line2` / `title_txt` 는
**스키마 말미에 위치**한다. 다른 모든 필드(특히 storyline·description·event_template 정보)를 먼저
작성한 뒤, 그 내용에 *직접 근거*해서 제목을 마지막에 작성하라. 제목을 먼저 결정하고 거기에
맞춰 내용을 끼워 넣지 마라 — 그러면 [제목 사실성 원칙]을 위반하기 쉽다.

{{
  "storylines": [
    {{
      "storyline_index": 0,
      "shorts_type": "storytelling",
      "sequence_type": "여정몰입형|결과선공개형|반전형|시퀀스블록형",
      "topic": "주제명",
      "topic_reason": "서사 구성 이유",
      "score": 0.0,
      "coherence_score": 0.0,
      "estimated_duration_sec": 0.0,
      "storyline": {{
        "hook": {{
          "chunk_index": 0, "candidate_index": 0,
          "start_sec": 0.0, "end_sec": 0.0,
          "description": "장면 설명",
          "use_original_audio": true,
          "character_focus": ["인물명"],
          "context_extended": false
        }},
        "hook_preview": null,
        "build": [
          {{
            "chunk_index": 0, "candidate_index": 0,
            "start_sec": 0.0, "end_sec": 0.0,
            "description": "장면 설명",
            "use_original_audio": true,
            "character_focus": ["인물명"],
            "context_extended": false
          }}
        ],
        "payoff": {{
          "chunk_index": 0, "candidate_index": 0,
          "start_sec": 0.0, "end_sec": 0.0,
          "description": "장면 설명",
          "use_original_audio": true,
          "character_focus": ["인물명"],
          "context_extended": false
        }},
        "sequence_block": []
      }},
      // ▼ TTS cue 는 storyline 구성 결정 후 작성. 0~5개. 한 storyline 안 모든 cue 는 같은 voice 사용 (절대 규칙).
      "tts_cues": [
        {{"start_sec": 0.0, "end_sec": 4.0, "text": "예: 황궁마켓의 유일한 법.",
          "voice": "ko_male_low", "speed": "slow",
          "voice_rationale": "디스토피아·스릴러 톤", "speed_rationale": "긴장 고조 직전이라 천천히"}},
        {{"start_sec": 18.0, "end_sec": 21.5, "text": "근데 진짜 노림수는 따로 있는데?",
          "voice": "ko_male_low", "speed": "fast",
          "voice_rationale": "같은 storyline 이므로 voice 유지", "speed_rationale": "반전 임팩트라 빠르게"}}
      ],
      // ▼ 제목은 위 내용을 모두 작성한 뒤 마지막에 작성 (제목 사실성 원칙 준수)
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/배경 설명 (13자 이내, 초과 금지)",
      "title_line2": "캐릭터/사건 중심 후킹 (13자 이내, 초과 금지)"
    }},
    {{
      "storyline_index": 1,
      "shorts_type": "storytelling",
      "sequence_type": "시퀀스블록형",
      "topic": "시퀀스블록형 예: 한 콩트 통째 사용",
      "topic_reason": "같은 sequence_id의 candidate가 자체 완결",
      "score": 0.0,
      "storyline": {{
        "hook": null,
        "hook_preview": null,
        "build": [],
        "payoff": null,
        "sequence_block": [
          {{"chunk_index": 1, "candidate_index": 0}},
          {{"chunk_index": 1, "candidate_index": 2}},
          {{"chunk_index": 1, "candidate_index": 3}}
        ]
      }},
      "tts_cues": [],
      // ▼ 제목은 마지막
      "title_line1": "상황 설명",
      "title_line2": "후킹 강조"
    }},
    {{
      "storyline_index": 2,
      "shorts_type": "highlight",
      "chunk_index": 0,
      "candidate_index": 0,
      "start_sec": 0.0,
      "end_sec": 0.0,
      "context_extended": false,
      "topic": "주제명",
      "topic_reason": "단독 선정 이유",
      "score": 0.0,
      "coherence_score": 0.0,
      "estimated_duration_sec": 0.0,
      "description": "장면 설명 (candidate.description 그대로 또는 더 상세하게 — 후처리에서 자막으로 사용됨)",
      "character_focus": ["인물명"],
      "use_original_audio": true,
      "tts_cues": [
        {{"start_sec": 1.0, "end_sec": 5.5, "text": "이 한 컷의 후킹.",
          "voice": "ko_female_high", "speed": "normal",
          "voice_rationale": "가벼운 바이럴 톤", "speed_rationale": "기본"}}
      ],
      // ▼ 제목은 위 내용(특히 description)을 작성한 뒤 마지막에 작성
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/배경 설명 (13자 이내, 초과 금지)",
      "title_line2": "캐릭터/사건 중심 후킹 (13자 이내, 초과 금지)"
    }}
  ],
  "selected_storyline_index": 0,
  "shorts_type":"storytelling"|"highlight",
  "selection_reason": "이 스토리라인을 선택한 이유",
  "selected_storyline": {{ "선정된 인덱스의 객체를 그대로 복사해서 출력": "" }},
  "title_line1": "최종 제목 1줄 (맥락)",
  "title_line2": "최종 제목 2줄 (후킹)",
  "title_txt": "title_line1 + title_line2 합친 전체 제목"
}}
"""



RELATIONSHIP_EXTRACTION_PROMPT = """
# Role
너는 영상 편집 전문가다. 쇼츠 편집을 위해 후보 장면들 사이의 관계를 분석한다.

# Task
아래 후보 장면 목록을 읽고, 장면들 사이에 존재하는 관계(엣지)를 추출하라.

각 후보에는 `continues_from` 필드가 있다. 이 값은 Pro 모델이 청크별로 개별 분석할 때 기록한 것이다.
- **같은 chunk 내** `continues_from`은 대체로 정확하다.
- **다른 chunk를 가리키는** `continues_from`은 틀린 경우가 많다. 반드시 재검증하라.

# 관계 타입 정의

| type | 의미 |
|---|---|
| `continuous` | 원본 영상에서 1~5초 이내로 물리적으로 이어지는 장면 |
| `setup_payoff` | A의 맥락이 없으면 B의 의미가 반감되는 인과 관계 |
| `consequence` | A의 결과로 B가 발생 (시간 간격이 있어도 인과 명확) |
| `sequence` | 같은 서사 라인의 순차적 단계 (반드시 같이 쓸 필요는 없음) |
| `duplicate` | 같은 장면이나 개그를 중복으로 포함한 경우 — 하나만 선택 |
| `character_arc` | 같은 인물의 감정/상태 변화 흐름 (편집 순서 지켜야 함) |
| `contrast` | 두 장면의 감정 낙차가 바이럴 포인트를 만드는 대조 관계 |

# 출력 규칙

- 명확한 관계만 출력하라. 추측성 관계는 제외.
- `continuous`는 start_sec 차이가 5초 이내이고 narrative가 이어지는 경우만 표시.
- `required` 필드: 두 클립을 반드시 함께 사용해야 하면 true, 아니면 false.
- 같은 chunk 내 `continues_from`이 올바르다고 판단되면 별도 엣지 출력 불필요.
- cross-chunk `continues_from`이 **오류**라고 판단되면 note에 명시하라.

# 후보 목록
{candidates_str}

# 출력 JSON 형식 (다른 내용 없이 JSON만 출력)
{{
  "edges": [
    {{
      "from": {{"chunk_index": 0, "candidate_index": 0}},
      "to": {{"chunk_index": 0, "candidate_index": 1}},
      "type": "continuous|setup_payoff|consequence|sequence|duplicate|character_arc|contrast",
      "required": false,
      "note": "관계 설명 한 문장"
    }}
  ]
}}
"""


# ─────────────────────────────────────────────
