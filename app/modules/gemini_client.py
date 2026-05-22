from __future__ import annotations

import json
import mimetypes
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _safe_upload_path(file_path: Path) -> tuple[Path, bool]:
    """한글 등 비-ASCII 경로를 Gemini File API 호환 경로로 변환.

    Returns:
        (사용할 경로, 임시파일 여부). 임시파일이면 사용 후 삭제 필요.
    """
    path_str = str(file_path)
    try:
        path_str.encode("ascii")
        return file_path, False
    except UnicodeEncodeError:
        # 비-ASCII 경로 → 임시 디렉토리에 심볼릭 링크 생성
        suffix = file_path.suffix
        tmp = Path(tempfile.mktemp(suffix=suffix, prefix="gemini_upload_"))
        shutil.copy2(file_path, tmp)
        return tmp, True


def _extract_json_from_markdown(text: str) -> str:
    """마크다운 코드 블록에서 JSON을 추출합니다."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


# ─────────────────────────────────────────────
# 청크 분석 프롬프트 v2 (구조 정리)
# 변경 요약: 인물 식별 통합, P1·P2 묶기, 라운드 주석 제거, 필드 정의 통합
# ─────────────────────────────────────────────
GEMINI_PROMPT_TEMPLATE = """
[ROLE]
당신은 한국 숏폼 전문 AI 에디터이자 멀티모달 스토리 분석 전문가다.
텍스트, 영상, 오디오를 통합적으로 이해해 바이럴 가능성이 높은 쇼츠 후보 장면을 분석한다.
반드시 JSON만 출력한다. 코드블록 금지. 영상에 없는 내용은 절대 창작하지 말 것.

---

[입력 정보]
- 작품명: {work_title}
- 주제: {topic}
- **현재 청크 번호 (chunk_index): {chunk_index}** ← 출력의 모든 chunk_index 필드는 이 값을 사용할 것
- 청크 범위: {chunk_start_sec} ~ {chunk_end_sec} 초
{work_context_block}
{previous_episodes_context_block}
{character_appearances_block}
- ⚠️ 모든 start_sec / end_sec는 첨부된 영상 파일의 시작(0초)을 기준으로 한 상대값으로 반환할 것

- 자막(있으면): {transcript_text}
- 씬 경계(있으면): {scene_boundaries}
{previous_context}

---

[인물 식별]

다음 수단으로 등장 인물의 이름을 먼저 파악한다 (우선순위 순):
1. face_id 사전 인식 결과 (있을 경우): 같은 인물명을 일관되게 사용
2. 화면 자막 또는 이름 자막 (예능/드라마 자막 포함)
3. 대사 내 호칭 (예: "야 민준아~")
4. 화면 내 텍스트 (명찰, 이름표 등 OCR 가능한 텍스트)

이름 확인 불가 → "인물A", "인물B" 등 고유 레이블 부여.
❌ 복장·헤어스타일 기반 레이블링 금지. 같은 인물이 다른 씬에 다른 옷으로 등장해도 같은 이름/레이블 유지.

확정된 이름 또는 레이블은 이후 모든 분석 항목에서 일관되게 사용.

**열린 라벨** — characters_in_scene, character_focus, characters_tracking, event_template 등 모든 인물 필드에서 사용 가능:
- `"엑스트라"` — 이름 없는 단일 행인·조연
- `"엑스트라(다수)"` — 군중·여러 행인이 함께 등장·행동
- `"행인"` — 배경 통행자
- `"불명"` — 화면에 인물은 있으나 누구인지 단정 불가

⚠️ 행위자가 식별된 주요 인물 중 반드시 한 명일 필요는 없다. 확신 부족 시 디폴트는 단정이 아닌 열린 라벨. 잘못된 주요 인물명을 채워넣는 것보다 `"불명"` / `"엑스트라"`가 우선.

---

[핵심 원칙]

**P1 — 시각 단서 근거**

description, transcript 화자 추정, 인물 필드, 장소 등 모든 진술에 적용.
각 진술은 다음 시각 단서 중 하나에 근거해야 한다:

1. **입 모양 동기화** — 화자 결정 시. 입이 보이지 않거나 동기화 확인 불가 → 화자 = `"불명"`. 자막 화자태그가 있으면 그것이 우선.
2. **시선·얼굴 방향** — 두리번거리거나 다른 방향이면 그 인물은 행위자가 아닐 가능성이 높다.
3. **신체 방향·동선** — 접근·이동 등 움직임이 있는 행동은 실제로 움직이는 인물에게 귀속. 정지한 주요 인물에게 이동 행동 귀속 금지.
4. **공간 거리·위치** — 멀리 떨어진 인물 사이에 근접 행동("다가갔다", "속삭였다", "손을 잡았다" 등) 귀속 금지.
5. **카메라 컷·앵글** — 컷이 바뀌면 인물 동일성 가정 금지, 새로 식별. 반응 컷의 인물이 직전 행동·발화의 주체라고 단정 금지.

근거 부재 시:
- 행위자·화자 불명확 → `"불명"` 또는 `"엑스트라"`. 디폴트 주요 인물 채우기 금지.
- 행동 주체 불명확 → 수동태·관찰형으로 표현 (예: "누군가 다가온다", "어디선가 목소리가 들린다").

장소(`scene_location` / `event_template.location`): 서사 맥락이 아닌 화면·대사 단서로 결정.
단서 없으면 `"불명"`, `"실내(불명)"`, `"실외(불명)"` 사용.
- ❌ "외국에서 한국으로 왔다" → "공항 입국장"
- ❌ "의사다" → "병원"

**P2 — 관찰 비약 금지**

두 인물 사이의 관계·상호작용·공동 의도를 표현하려면 **양쪽 인물의 시각 단서가 모두 명시적으로 확인**되어야 한다.

양방 단서가 필요한 표현 (열린 목록):
- **상호 시선**: "서로 본다", "마주본다", "눈이 마주친다" → 양쪽 시선이 서로를 향함 동시 확인
- **상호 발화**: "대화한다", "이야기를 나눈다", "말다툼한다" → 양쪽 발화 단서 확인. 한쪽만 말하면 "A가 B에게 말한다"
- **상호 인지**: "서로 알아본다", "서로를 발견한다" → 양쪽 인지 반응 확인
- **공동 행동**: "함께 걷는다", "동행한다" → 같은 방향·근접 동선 양쪽 확인
- **신체 접촉**: "포옹", "악수", "키스", "손을 잡는다" → 실제 접촉이 화면에 명시적으로 보여야 함
- **감정 상호**: "교감", "서로 의지한다" → 상호 행동(끄덕임·미소 교환·시선 교환 등) 양방 확인

확신 부족 시 — 각 인물의 관찰을 개별 문장으로 분해:
- ❌ "A와 B가 서로 바라본다." → ✅ "A가 멀리서 B를 바라본다. B는 주변을 두리번거리며 누군가를 찾는다."
- ❌ "두 사람이 대화한다." → ✅ "A가 B에게 말을 건다. B는 표정 변화 없이 듣고 있다."
- ❌ "함께 걸어간다." → ✅ "A가 앞장서서 걷는다. B는 뒤에서 천천히 따라간다."

P2는 `reason` 필드에도 동일하게 적용. 드라마가 의도적으로 모호하게 연출한 관계·인지 상태는 한 방향으로 단정하지 말 것. 한쪽 단서만 확인된 상태라면 모호성 자체를 매력 포인트로 기술.
- ❌ "서로를 알면서도 모르는 척해야 하는 두 사람의 엇갈린 관계" (양방 인지 단정)
- ❌ "오랜 연인의 재회" (관계 종류를 단서 없이 확정)
- ✅ "A의 씁쓸한 표정과 B의 사무적 자기소개가 관계를 단정할 수 없게 만드는 긴장"
- ✅ "A의 표정에서 과거 인연이 암시되지만 B 쪽 인지 단서는 비어 있어 관계가 열려 있음"

---

[description 규칙]

- 최소 5문장 이상, 시간 순서에 맞게, 정확하고 객관적으로 작성.
- 실제 장면 묘사만. 재해석된 의미는 `reason` 필드에만 반영. 과대해석·과장 금지.
- 행동의 범주를 바꾸지 마라. 관찰된 행동(발화·동작·표정)만 그 종류 그대로 기술. 확인되지 않은 의도·감정·결과 덧씌우기 금지.
- 내레이션·독백·보이스오버(VO)는 반드시 명시: "~의 내레이션", "VO로 ~가 말한다"
- OST·삽입곡·배경음악 가사는 인물 발화로 기술하지 말 것. 필요 시 "배경음악(OST) 가사"로만 언급.
- 해당 candidate의 `start_sec` ~ `end_sec` 안의 사건만 기술. 범위 밖 사건은 별개 candidate로 분리.
- P1, P2 원칙 적용.

---

[타임스탬프 원칙 — 어기면 해당 후보 폐기]

🚫 가장 중요한 규칙. 어기면 자막·TTS·렌더가 모두 어긋난다.

1. **첨부 영상의 시작 = 0.0초**. 모든 start_sec / end_sec는 영상 0초 기준 상대값. 원본 풀 영상의 절대 시간이 아니다.
2. **영상 길이를 초과하는 시간 출력 금지**. 영상이 600초면 어떤 시간값도 600.0 초과 불가.
3. **transcript 대사는 그 timestamp에 실제로 들리는 대사여야 한다**. 다른 시점의 대사를 베껴 붙이지 마라.
4. **자막의 절대 시간을 그대로 베끼지 마라**. 영상에서 실제로 들리는 시점을 직접 확인하고 기록.
5. **모든 candidate의 start_sec / end_sec는 영상에서 직접 확인 가능한 구간**이어야 한다. 추측 금지.
6. **context_extension의 extended_start/end_sec도 chunk-relative**. [0.0, 영상 길이] 범위 안에 있어야 하며, `extended_start_sec ≤ start_sec ≤ end_sec ≤ extended_end_sec` 유지. 이 조건 미충족 시 needed=false로 강등.

출력 전 체크리스트 (각 값을 쓰기 직전마다 적용):
- 모든 시간값이 [0.0, 영상 길이] 범위 안인가
- segments[i].end_sec == segments[i+1].start_sec (gap/overlap 없는가)
- P1: description·transcript·event_template의 모든 진술에 시각 단서 근거가 있는가
- P2: 상호작용·관계 표현에 양방 단서가 있는가 / description은 단편 관찰 분해 / reason은 모호성 보존
- description의 모든 사건이 해당 candidate의 start_sec ~ end_sec 안에 있는가

---

[세그먼트 분할]

청크 전체(`chunk_start_sec` ~ `chunk_end_sec`)를 **빈틈 없이, 겹침 없이** 시간순으로 분할.

분할 기준 (우선순위):
1. **장면 전환(scene cut)**: 입력 `scene_boundaries`가 있으면 1차 참고
2. **서사 단위(beat)**: 같은 장소·인물이라도 화제/사건이 명확히 바뀌면 분리
3. **대화 단위**: 발화 주체 변경 + 화제 변경이 동시에 일어날 때 분리

길이 가이드:
- 일반적 길이: 10~60초 권장
- 정적·전환 컷이 길면 60초 이상 허용
- 마이크로 컷(1~3초)은 인접 세그먼트에 합침

연속성 제약 (절대 규칙):
- segments[0].start_sec == chunk_start_sec
- segments[-1].end_sec == chunk_end_sec
- 모든 i에 대해 segments[i].end_sec == segments[i+1].start_sec
- segments는 빠짐없이 청크 전체를 덮어야 함 (평범한 구간도 반드시 포함)

---

[후보 추출]

쇼츠 유형 2가지:
- **highlight**: 단일 클립으로 "뭐지?" → 감정반응 → 완결까지 가능한 장면. `highlight_eligible: true` 표기.
- **storytelling**: 다른 장면과 묶여 hook→build→payoff 흐름을 만들 수 있는 장면.

⚠️ hook/build/payoff 역할 결정은 다음 단계(스토리 구성)에서 수행. 여기서는 미리 라벨 붙이지 말 것.
⚠️ 모든 candidate에 context_extension 판단·출력 필수.

추출 규칙:
- segments 중 쇼츠 제작에 가치 있는 장면만 선별.
- candidate의 `segment_index`는 segments 배열 인덱스를 가리킴.
- start_sec/end_sec은 해당 segment 범위 안에서 더 좁게 잡아도 됨 (segment 경계 초과 금지).
- 최소 {min_candidates}개 이상 선별.

---

[tts_draft — 한 컷의 내레이션 초안]

각 candidate_moment마다 `tts_draft` 필드를 함께 작성한다.
이 라인은 다음 단계(TTS 디렉터)가 voice·speed·cue 위치를 결정할 때 *영상 시각 단서에서 직접 뽑은 내레이션 한 줄*로 참조한다.

⚠️ 너는 이 시점에 영상을 직접 보고 있다. description·transcript·event_template과 함께 *눈에 보이는 사건*을 한 줄로 응축하라.
⚠️ 이 클립이 hook/build/payoff 중 어떤 역할로 쓰일지, 어떤 다른 클립과 묶일지는 *아직 모른다*. 이 한 컷 단독으로 봤을 때 가장 강하게 후킹되는 라인을 적어라. 인접 클립과의 흐름 연결은 다음 단계가 처리한다.

작성 규칙:
1. **영상을 설명하라**. description / transcript / event_template에서 확인 가능한 *실제 사건*만 한 줄로 응축. P1·P2 원칙 그대로 — 시각 단서 없는 추측·해석·외삽 금지.
2. **사실성**: transcript와 충돌하면 transcript가 우선. event_template.mode=="phone_call"이면 '고백/사랑/제안/프러포즈' 금지. 입력에 없는 형용사·관계·결과·시각 명사(흉터·문신·반지·눈물·피 등) 추가 금지.
3. **한 문장, 12~25자**. 너무 짧으면 정보 부족, 너무 길면 후킹력 약함. 한 호흡에 읽히는 단언체.
4. **권장 어투**: 다음 셋 중 하나의 결로 작성하라
   - 명사형 종결 (인물·사건 라벨링): "결국 시장을 통째로 장악한 희로."
   - 단언·상황 설명 (~다/~된다): "조용히 판을 다시 짠다."
   - 궁금증 유발 (~는데?/근데~): "근데 진짜 노림수는 따로 있는데?"
5. **금지 어투**: 격식체("~합니다, 마침내, 새로운 ~의 탄생"), 슬랭·예능톤("~네/~함/~임, 미친 설계, 한 방에 다 뒤집힘"), 시청자 호명("봐봐, 잘 봐, 이거 진짜?").
6. **이모지 금지**. 한 문장 안에 마침표 1개까지만(또는 명사 종결의 `.`).

예시:
- description="현흡이 창밖 선아의 머리 위 붉은 선들을 본다" → tts_draft: "타인의 비밀을 보는 한 사람."
- description="유미가 PD에게 통화로 작가직을 부탁한다" / transcript="피디님, 저예요" → tts_draft: "밤늦게 걸려온 그 한 통의 전화."
- description="희로가 마켓 좌판을 통째로 인수한다" → tts_draft: "결국 시장을 통째로 장악한 희로."

---

[출력 규칙]
- 모든 출력은 한국어
- 최신 쇼츠 트렌드 반영: 자연스러운 톤, 짧은 문장, 강조/리액션 요소
- JSON 스키마 강제. 해당 항목이 없을 경우 빈 배열 대신 null
- 타이틀 시퀀스, 엔딩 크레딧은 candidate_moments 제외 (segments에는 포함)
- start_sec~end_sec 내 상황만으로 이해 가능해야 함
- 대사가 있는 장면에서 인물의 행동/의도 묘사 시 대사 내용 최우선. 캐릭터 배경 지식이 대사와 충돌하면 대사를 신뢰.

---

[출력 스키마]

**characters_tracking** (chunk 레벨)
- `character`: 인물명 또는 열린 라벨 (엑스트라·엑스트라(다수)·행인·불명 포함)
- `appearances[]`:
  - `start_sec` / `end_sec`: 등장 구간
  - `action`: 그 구간 핵심 행동/발화 요약 한 문장. P1 시각 단서 근거 없는 추정 금지.

**segments**
- `segment_index`: 0부터 시작하는 정수
- `start_sec` / `end_sec`: 인접 세그먼트와 정확히 맞물려야 함
- `description`: [description 규칙] + P2 엄수. 평범한 구간 2~3문장, 핵심 구간 5문장 이상.
- `transcript`: 핵심 발화. 내레이션/독백/VO면 `[내레이션]` 접두. 없으면 `""`
- `characters_in_scene`: 화면에 등장하는 인물 배열. 열린 라벨 허용. 화면 안 모든 인물을 주요 인물명으로 자동 채우기 금지.
- `scene_location`: 화면·대사 단서 기반. 단서 없으면 "불명" 계열.
- `timeline_position`: `"현재"` | `"과거"` | `"불명"`

**candidate_moments**
- `segment_index`: 속한 segments 배열 인덱스 (필수)
- `chunk_index`: **반드시 입력의 "현재 청크 번호"와 동일**. 0으로 고정하거나 임의 값 금지.
- `candidate_index`: 청크 내 후보 순서 (0부터 시작)
- `start_sec` / `end_sec`: segment 범위 안의 핵심 구간
- `characters_in_scene`: 열린 라벨 허용. 디폴트로 주요 인물 채우기 금지.
- `character_focus`: 핵심 인물 배열. 행위자가 엑스트라면 `"엑스트라"`, 불명확하면 `"불명"`.
- `description`: segment description 그대로 또는 더 상세하게. P2 엄수.
- `reason`: 선정 이유. 재해석 의미는 여기에만. P2 양방 단서 규칙 적용 — 모호성은 모호성 자체로 기술.
- `transcript`: 해당 장면에 나오는 대사 전부. 발화 도중 잘리면 잘린 곳까지. 내레이션/독백/VO면 `[내레이션]` 접두.
- `tts_draft`: **이 한 컷에 깔릴 내레이션 초안 한 줄** (12~25자). 다음 단계 TTS 디렉터가 voice·cue 배치를 결정할 때 참고한다. 작성 규칙은 아래 [tts_draft] 섹션 참조.
- `scene_location` / `timeline_position`: segment에서 복사
- `continues_from`:
  - 다른 candidate와 이어지면 `{{"chunk_index": N, "candidate_index": M}}`, 독립이면 `null`
  - 같은 청크 참조 → chunk_index = 현재 청크 번호
  - 이전 청크 참조 → [이전 청크들의 분석 결과] 블록에 명시된 chunk_index 값 사용
  - ⚠️ 예시의 좌표를 그대로 베끼지 마라. 실제 참조 대상의 좌표를 정확히 입력.
- `requires_context`: 다른 장면 없이는 이해 안 되면 true
- `highlight_eligible`: requires_context=false + 클립 자체에 감정적 완결성 있으면 true
- `highlight_reason`: highlight_eligible=true인 경우만 1문장 (false면 null)
- `visual_essential`: 핵심 의미가 대사가 아닌 시각 단서(표정·동작·소품·자막·시선·교차편집 등)에 있으면 true. true면 무음 구간도 그대로 유지됨.
- `context_extension`: **모든 candidate에 필수**
  - `needed`: 단독 클립으로 볼 때 앞뒤 맥락이 필요한가
  - `extended_start_sec`: needed=true면 확장 시작점 (보통 핵심 직전 5~25초). false면 start_sec와 동일.
  - `extended_end_sec`: needed=true면 확장 종료점 (보통 핵심 직후 5~25초). false면 end_sec와 동일.
  - `before_summary` / `after_summary`: 확장 부분에 무엇이 있는지 한 문장씩
  - `reason`: 왜 앞뒤가 필수인지 한 줄
  - 강제 조건: `extended_start_sec ≤ start_sec ≤ end_sec ≤ extended_end_sec`
  - 확장 구간 안 컷·장소 변경 >2회 → needed=false로 강등
  - needed=true 적극 적용 신호:
    - candidate 시작 직전 5초 안에 도입·배경 대사·인물 등장이 있음
    - candidate 끝 직후 5초 안에 반응·여운·결과 표정/액션이 있음
    - 앞뒤 1~2 segment 없이 핵심 사건의 의미가 모호해짐
- `event_template`:
  - `subject`: 행동 주체. 열린 라벨 허용. 확신 없으면 `"불명"` 우선. 디폴트로 주요 인물 채우기 금지.
  - `action`: 행동 동사구 (예: "맥주 권유", "병문안", "통화로 업무 위임")
  - `target`: 행동 대상 (캐릭터명·장소·물건). 없으면 null. 불확실하면 `"엑스트라"`/`"불명"` 사용 가능.
  - `mode`:
    - `in_person`: 같은 장소에서 직접 대면
    - `phone_call`: 전화·영상통화. ⚠️ 직접 대면이 아님 — 혼동 시 다음 단계에서 title 오류 발생
    - `narration`: 내레이션·독백·VO
    - `observation`: 한 인물이 다른 인물·상황을 관찰만 함
    - `mixed`: 한 candidate 안에서 둘 이상 모드 혼재
  - `location`: 장면 발생 장소

---

다음 스키마로만 응답 (아래 예시의 숫자는 placeholder. 실제 값은 [입력 정보]의 현재 청크 번호·청크 범위를 사용):
{{
  "chunk_index": <현재 청크 번호와 동일하게>,
  "chunk_start_sec": <청크 범위 시작값>,
  "chunk_end_sec": <청크 범위 종료값>,
  "summary": "해당 청크 전체의 핵심 내용 요약",
  "characters_tracking": [
    {{
      "character": "인물명 또는 레이블",
      "appearances": [
        {{"start_sec": 0.0, "end_sec": 32.0, "action": "해당 구간 행동/발화 요약"}}
      ]
    }}
  ],
  "segments": [
    {{
      "segment_index": 0,
      "start_sec": 0.0,
      "end_sec": 45.3,
      "description": "이 구간 묘사 ([description 규칙] 엄수)",
      "transcript": "해당 장면에 나오는 대사 전부. 발화 도중 잘리면 잘린 곳까지. (없으면 \\"\\")",
      "characters_in_scene": ["인물명1"],
      "scene_location": "장면 배경 장소",
      "timeline_position": "현재|과거|불명"
    }}
  ],
  "candidate_moments": [
    {{
      "segment_index": 0,
      "chunk_index": <현재 청크 번호와 동일하게>,
      "candidate_index": <0부터 시작하는 청크 내 순번>,
      "start_sec": 12.4,
      "end_sec": 25.8,
      "characters_in_scene": ["인물명1", "인물명2"],
      "character_focus": ["인물명"],
      "description": "장면 설명(묘사 위주, 5문장 이상)",
      "reason": "선정 이유",
      "transcript": "단 한 명의 주요 발화 ([내레이션] 접두 가능)",
      "tts_draft": "이 한 컷의 내레이션 초안 한 줄 (12~25자, [tts_draft] 섹션 규칙 준수)",
      "scene_location": "장면 배경 장소",
      "timeline_position": "현재|과거|불명",
      "continues_from": null,
      "requires_context": false,
      "highlight_eligible": false,
      "highlight_reason": null,
      "visual_essential": false,
      "context_extension": {{
        "needed": false,
        "extended_start_sec": 12.4,
        "extended_end_sec": 25.8,
        "before_summary": null,
        "after_summary": null,
        "reason": null
      }},
      "event_template": {{
        "subject": "주체 캐릭터명",
        "action": "행동 동사구",
        "target": "대상 또는 null",
        "mode": "in_person | phone_call | narration | observation | mixed",
        "location": "장면 발생 장소"
      }}
    }}
  ],
  "title_candidates": ["제목1", "제목2", "제목3"]
}}
"""

# ─────────────────────────────────────────────
# 스토리 구성 프롬프트 (레퍼런스 기반 바이럴 최적화)
# ─────────────────────────────────────────────
STORY_COMPOSITION_PROMPT = """
[ROLE]
너는 드라마/영화/예능 기반 유튜브 쇼츠 100만 조회수 전문 편집자다.
시청자가 쇼츠를 보고 작품을 궁금해하게 만든다. 작품을 처음 보는 사람도 한 번 보고 이해할 수 있어야 한다.

영상 분석 데이터(candidate_moments + segments)를 입력받아 **스토리라인 3개**를 구성하고,
그중 바이럴 성공 가능성이 가장 높은 1개를 최종 선정한다. 나머지 2개도 후속 쇼츠로 사용될 수 있다.

JSON만 출력한다. 다른 텍스트 금지.

---

[입력 정보]
- 작품명: {work_title}
- 주제: {topic}
- 클립 길이 제약: {min_duration_sec}초 ~ {max_duration_sec}초 (이상적 50초)
{story_topic_line}
{work_context_block}
{episodes_context_block}
{segments_summary_block}

- 후보 장면 (candidate_moments):
{candidates_str}

---

[핵심 원칙 — 4가지]

이 4원칙으로 모든 결정을 한다. 다른 규칙은 모두 이 원칙의 적용일 뿐이다.

**원칙 1 — topic은 thread다 (실타래)**

topic은 라벨이 아니라 hook → build → payoff를 관통하는 **하나의 실타래**다.
실타래는 다음 중 하나로 구체화된다:
- 한 질문 ("그 사람의 정체가 뭘까?")
- 한 인물의 결정·변화 ("주인공이 이걸 받아들이게 되는 과정")
- 한 사건의 인과 ("이 일이 어떻게 시작되어 어떻게 끝나는가")
- 한 감정의 궤적 ("호기심 → 충격 → 체념")

각 clip을 고를 때 자문: **"이 clip이 thread를 전진시키는가, 옆길로 새는가?"**
점수가 높아도 thread와 무관하면 옆길이다. 옆길 clip을 끼우면 시청자에겐 "다른 영상"으로 보인다.

**원칙 2 — 시청자 관점 (음소거 검증)**

너는 candidate.description을 텍스트로 읽지만 시청자는 **영상 자체를 본다**.
- 시청자는 "의미"를 추론하지 않는다. 눈앞의 사건을 본다.
- "이 장면이 X를 상징/대비/복선으로 표현한다"는 너의 해석은 편집된 쇼츠에서는 전달되지 않는다.

자문: **"음소거하고 자막 없이 이 클립 시퀀스만 본 사람이 storyline.topic을 한 줄로 답할 수 있는가?"**
- 답할 수 없으면 의미적 연결이 너의 머릿속에만 있는 것. 다른 candidate로 교체하라.
- "사건 A, B, C가 차례로 일어났다"로만 보이면 thread가 작동하지 않는 것.

**원칙 3 — 입력 데이터 신뢰 순위**

candidate의 정보는 단계별로 신뢰도가 다르다:

| 순위 | 필드 | 성격 | 사용처 |
|------|------|------|--------|
| 1 | `event_template` | 행동·mode·subject/target을 구조화한 라벨 | 행동·mode 단정의 1순위 근거 |
| 2 | `transcript` | Whisper로 전사한 **실제 음성 대사** (정확한 사실) | 통화 상대·대사 인물 등 음성 의존 정보의 1순위 근거 |
| 3 | `description` | LLM의 시각 분석 추정 | 장면 묘사. 통화 상대·등장 인물 등 음성 의존 정보는 부정확 가능 |
| 4 | 너의 해석 | 위 셋에 근거 없으면 출력 금지 | — |

**title·라벨 작성 규칙:**
- 행동·mode가 description과 충돌하면 → event_template을 따른다
- transcript와 description이 충돌하면 → transcript를 따른다 (화자·통화 상대 단정 포함)
- event_template.mode == "phone_call" → 발신자(subject)의 *기능적 행동* 위주로 (위임/통보/알림). '고백/사랑/제안/프러포즈' 금지
- 입력에 없는 형용사·관계·결과·시각 명사(흉터·문신·반지·눈물·피 등) 추가 금지
- "표정이 굳는다 → 분노 폭발", "당황한다 → 충격 대참사" 같은 강도 외삽 금지
- "1→2 추이 → 3대0 압승" 같은 결과 외삽 금지

**원칙 4 — 연결 신호 (인접 clip 쌍이 묶이는 6가지 방법)**

storytelling에서 인접 clip 쌍(hook→build, build→build, build→payoff)은 다음 신호 중 **최소 2개**로 묶여야 한다.
신호 1개뿐이면 시청자는 두 컷을 따로 본다. 2개 이상이면 한 흐름으로 본다.

| 신호 | 판별 방법 | 입력 필드 |
|------|-----------|-----------|
| (a) 인물 연속 | character_focus 교집합 ≥ 1명 | `character_focus` |
| (b) 공간/모티프 연속 | scene_location 일치, 또는 같은 사물·상징 등장 | `scene_location`, `description` |
| (c) 사건 인과 | A의 action·결과가 B의 원인이 됨 | `event_template.action`, `description` |
| (d) 대상-주체 연결 | A.target이 B.subject (또는 그 반대) | `event_template.subject/target` |
| (e) 직접 연결 명시 | candidate가 다른 candidate를 명시적으로 참조 | `continues_from` |
| (f) 키워드 연속 | A의 transcript 명사·고유명사가 B의 transcript·description에 재등장 | `transcript` |

⚠️ (a)뿐인 연결은 약하다 — "같은 인물이 다른 일을 한다"는 thread 보장이 아니다.
   (c)/(d)/(e) 중 하나가 함께 있어야 강한 연결.

⚠️ candidate.continues_from은 분석 단계에서 미리 잡힌 직접 연결 신호다. 적극 활용하라.

---

[구성 절차 — 4단계, 이 순서로]

**Step 1 — thread 후보 3개 도출**

후보 데이터(candidate_moments + segments)를 읽고 thread 후보 3개를 *클립 선택 전에* 먼저 정한다.
각 thread마다:
- 핵심 실타래 (질문/결정/사건/감정 중 하나, 1문장)
- 시청자가 음소거로 봤을 때 따라갈 수 있는지 자문

각 thread마다 **타입 결정**:
- 단일 candidate가 highlight_eligible=true이고 그 자체로 thread를 완결 → **highlight**
- 2개 이상 candidate를 묶어야 thread가 완성 → **storytelling**

타입 분배 비율 가이드 (highlight_eligible:true 비율 기준):
- 90% 이상 → 3개 모두 highlight 허용
- 20% 이하 → storytelling 최소 2개
- 그 외 → 적절히 혼합

storylines 3개는 **서로 다른 사건/장면/코너**에서 추출. 같은 인물·같은 장소·같은 사건의 다른 각도 변형은 다양성 위반.

**Step 2 — clip_selection_plan을 먼저 쓰고, 그 다음 clip 본체를 채운다**

⚠️ **storytelling은 storyline 본체보다 먼저 `clip_selection_plan` 배열을 작성한다.**
plan에서 각 role에 대해 *어떤 candidate를 왜 고르는지*와 *인접 clip과의 연결 신호*를 글로 commit한 뒤,
plan을 따라 storyline 본체(hook/build/payoff)를 옮겨 적는다. 이 순서를 어기면 신호 검증이 사후 합리화가 된다.

**storytelling**:
- 구성: hook (5~8초) + build 1~2개 (각 15~20초) + payoff (15~20초)
- 합계 40~60초 안에 들어오게
- 5개 이상 build로 늘리지 말 것
- plan에서 인접 clip 쌍마다 [원칙 4]의 연결 신호 ≥ 2개 commit
  - (a)만으로는 부족 — (c)/(d)/(e) 중 하나는 함께 있어야 함
  - 신호 2개를 채울 candidate가 없으면 → 다른 candidate로 교체. 교체 불가능하면 이 thread를 storytelling 대신 highlight로 변경하거나 reject
- candidate.continues_from이 가리키는 candidate를 우선 후보로 검토 (= 신호 e의 즉각 충족)

**storytelling sequence_type 결정 (이 순서로)**:
1. 같은 sequence_id candidate 3개 이상이 자체로 hook→발전→결말 → **시퀀스블록형** (build/payoff 구분 없이 통째로)
2. hook/build와 payoff의 결말 방향이 *반대* → **반전형**. title_line2는 반드시 payoff 결말 방향
3. 핵심 결과 장면을 hook에 *시간 역순*으로 미리 노출 → **결과선공개형**. hook은 build/payoff와 같은 인물 ≥ 1명 또는 직접 인과
4. 위 셋 모두 아니면 → **여정몰입형** (디폴트, 시간 순서)

**highlight**:
- highlight_eligible=true인 candidate에서만 선택
- context_extension.needed=true인 candidate를 우선 채택 (setup→핵심→결과 자동 묶음)
- 단일 candidate의 description·transcript·event_template에서만 라벨 추출 — 다른 candidate 모티프 빌리기 금지

**Step 3 — 시간 제약 검증**

**시간 절대 고정 (가장 중요)**:
- candidate의 start_sec, end_sec, context_extension.extended_start_sec/end_sec는 **절대 변형 금지**
- 너의 출력값은 후처리에서 chunk_index/candidate_index 기반 lookup으로 덮어쓰임 → 정확히 인용만 하라
- context_extension 적용은 `"context_extended": true` 플래그로만 표현 (시간은 후처리에서 ext 적용)
- 길이가 max_duration_sec 초과 → 시간을 줄이지 말고 candidate 개수를 줄이거나 더 짧은 candidate로 교체

**시간 순서 (storytelling)**:
- build[0].start_sec < build[1].start_sec < ... < payoff.start_sec 반드시 만족
- 점수가 높다고 시간 순서를 섞지 마라

**hook 시간 위치 (storytelling) — hook_preview 결정**:
- hook.start_sec < build[0].start_sec → hook_preview = null (시간순)
- hook.start_sec > payoff.end_sec → hook_preview = null (결과선공개형 시간 역순)
- build[0].start_sec ≤ hook.start_sec ≤ payoff.end_sec → **hook_preview 필수** (3~7초 발췌). 후처리 시퀀스: hook_preview → build → hook → payoff

**Step 4 — title 작성 (가장 마지막)**

clip이 모두 확정된 후 title을 작성한다. 절대 title을 먼저 정하고 거기에 clip을 끼워넣지 마라.

**구조 (2줄 필수)**:
- title_line1 (위·흰색, 13자 이내 권장): 상황/배경/도입
- title_line2 (아래·노란색 강조, 13자 이내 권장): 캐릭터·반전·핵심 후킹

**line1↔line2 연결 패턴 (다음 중 하나)**:
- 조건절-결과절: line1이 연결어미(...면, ...니, ...만, ...는데, ...자)로 끝 → line2는 동사 종결 결과절 (...된다 / ...한다 / ...버린다)
- 병렬 라벨: line1이 명사구·완결절 → line2도 같은 구조
- 시간순·인과: line1이 시점·도입 → line2는 그 시점에 *벌어진 결과*

**검증**: title_line1과 title_line2를 공백 한 칸으로 이어 읽었을 때 한 호흡으로 자연스러운가.

**title_line2 ↔ payoff 결말 일관성**:
- title_line2는 payoff의 *실제 결말 방향*과 의미가 일치해야 한다
- 반전형은 반드시 payoff 결말을 따른다. hook/build 패턴 외삽 금지

**title 사실성 (원칙 3 재확인)**:
- transcript / event_template / description / characters_in_scene 외 정보로 라벨링 금지
- "서류 건넴 → 사표 제출", "마주 본다 → 사랑 고백" 같은 행동 범주 변경 금지
- 이모지 금지

---

[clip_selection_plan 작성 가이드 — 검증을 *사전에* 박는다]

LLM은 토큰을 왼쪽→오른쪽으로 생성하며 *되돌아가 고치지 않는다.*
따라서 사후 체크리스트는 무의미 — 검증은 plan을 *쓰는 동안* 일어나야 한다.

storytelling storyline마다 본체보다 먼저 `clip_selection_plan`을 작성하며 다음을 한 줄씩 명시:

1. **각 role의 intended_chunk_candidate** — 어떤 (chunk_index, candidate_index)를 그 자리에 둘지
2. **why_this** — 이 candidate가 *thread를 전진시키는* 이유 (옆길이면 다른 candidate로)
3. **linkage_to_prev / linkage_to_next** — 인접 clip과 묶이는 신호 ≥ 2개 (a~f 코드 + 한 줄 근거)
   - (a) 인물 연속 / (b) 공간·모티프 / (c) 사건 인과 / (d) 대상-주체 / (e) continues_from / (f) 키워드
   - (a)만이면 약함. (c)/(d)/(e) 중 하나는 반드시 함께
   - 신호 2개를 채울 candidate를 못 찾으면 → 다른 candidate로 교체. 끝까지 못 찾으면 이 thread를 storytelling 대신 highlight로 변경하거나 reject

plan을 다 적은 후에야 storyline 본체(hook/build/payoff)를 채운다.
**plan과 본체가 어긋나면 plan을 따른다** (본체를 plan에 맞춰 다시 쓴다).

추가 자가 commit (plan 안에서 글로 풀어쓰면 자동 충족):
- thread 한 줄이 있고 모든 clip이 그것을 전진시키는가
- 음소거 검증: 자막 없이 봐도 topic이 한 줄로 답해지는가
- hook_preview 케이스 1·2·3 판별 (Step 3)
- 총 길이 {min_duration_sec}~{max_duration_sec}초
- 3 storylines 서로 다른 사건
- title의 모든 단어가 transcript / event_template / description / characters_in_scene에서 출처 추적 가능
- event_template.mode=="phone_call" clip이 있으면 title에 '고백/사랑/제안/프러포즈' 없음
- title_line2 방향 = payoff 결말 방향 (반전형 특히)
- title_line1+line2가 한 호흡으로 자연스럽게 이어지는가

⚠️ 코드 단(_validate_story_response)에서 다음을 강제 검사하므로, 빠뜨리면 retry로 돌아온다:
   - storytelling이면 clip_selection_plan 길이 ≥ 3
   - 모든 build/payoff의 linkage_signals 길이 ≥ 2

---

[출력 스키마]

⚠️ **필드 작성 순서가 곧 commitment 순서다.** 모델은 토큰을 왼쪽→오른쪽으로 생성하며 되돌아가지 않는다.
따라서 스키마는 *결정에 영향을 주는 필드를 앞쪽에* 배치한다:
1. storyline 단위: `thread` → `clip_selection_plan` → `storyline` 본체 → ... → `title_*` (맨 마지막)
2. clip 단위 (build/payoff): `linkage_signals` → `linkage_in` → `chunk_index`/`candidate_index` → 나머지
   → 신호를 먼저 commit해야 그에 맞는 candidate를 고르게 됨

json
{{
  "storylines": [
    {{
      "storyline_index": 0,
      "shorts_type": "storytelling",
      "sequence_type": "여정몰입형|결과선공개형|반전형|시퀀스블록형",
      "thread": "이 storyline의 실타래 한 줄 (질문/결정/사건/감정)",

      // ▼ storyline 본체보다 먼저 작성 — 여기서 clip 선택을 commit한 뒤 아래 본체를 채운다
      "clip_selection_plan": [
        {{
          "role": "hook",
          "intended_chunk_candidate": [0, 0],
          "why_this": "이 candidate를 hook으로 고르는 이유 (thread 도입 측면에서)",
          "linkage_to_next": "다음 clip과 묶이는 신호 (예: 'a+c — 신현흡 인물 연속 + 능력 사용 인과')"
        }},
        {{
          "role": "build",
          "intended_chunk_candidate": [0, 0],
          "why_this": "thread를 발전시키는 이유",
          "linkage_to_prev": "직전 clip과 묶이는 신호 ≥ 2개 (예: 'a+c+f')",
          "linkage_to_next": "다음 clip과 묶이는 신호 ≥ 2개"
        }},
        {{
          "role": "payoff",
          "intended_chunk_candidate": [0, 0],
          "why_this": "thread를 완결하는 이유",
          "linkage_to_prev": "직전 build와 묶이는 신호 ≥ 2개"
        }}
      ],

      "topic": "주제명 (thread를 시청자 언어로)",
      "topic_reason": "thread 선정 이유",
      "score": 0.0,
      "coherence_score": 0.0,
      "estimated_duration_sec": 0.0,
      "storyline": {{
        "hook": {{
          "chunk_index": 0, "candidate_index": 0,
          "start_sec": 0.0, "end_sec": 0.0,
          "description": "candidate.description 그대로 복사 (재작성·압축·요약 금지)",
          "use_original_audio": true,
          "character_focus": ["인물명"],
          "context_extended": false
        }},
        "hook_preview": null,
        "build": [
          {{
            // ▼ 신호를 먼저 commit한 뒤 candidate를 고른다 (chunk_index/candidate_index보다 앞)
            "linkage_signals": ["a", "c"],
            "linkage_in": "직전 clip(hook 또는 이전 build)과 어떻게 묶이는지 1줄. 예: '같은 인물 신현흡(a), 직전 hook의 능력으로 이 장면을 관찰(c)'",
            "chunk_index": 0, "candidate_index": 0,
            "start_sec": 0.0, "end_sec": 0.0,
            "description": "candidate.description 그대로 복사",
            "use_original_audio": true,
            "character_focus": ["인물명"],
            "context_extended": false
          }}
        ],
        "payoff": {{
          // ▼ 신호 먼저
          "linkage_signals": ["a", "d"],
          "linkage_in": "직전 build와 어떻게 묶이는지 1줄",
          "chunk_index": 0, "candidate_index": 0,
          "start_sec": 0.0, "end_sec": 0.0,
          "description": "candidate.description 그대로 복사",
          "use_original_audio": true,
          "character_focus": ["인물명"],
          "context_extended": false
        }},
        "sequence_block": []
      }},

      // ▼ 모든 clip이 확정된 뒤 마지막에 작성
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/배경 (13자 이내)",
      "title_line2": "캐릭터/사건 후킹 (13자 이내)"
    }},
    {{
      "storyline_index": 1,
      "shorts_type": "highlight",
      "thread": "thread 한 줄",
      "chunk_index": 0, "candidate_index": 0,
      "start_sec": 0.0, "end_sec": 0.0,
      "context_extended": false,
      "topic": "주제명",
      "topic_reason": "단독 선정 이유 (highlight는 단일 candidate라 plan 불필요. 이 reason이 commit 역할)",
      "score": 0.0,
      "coherence_score": 0.0,
      "estimated_duration_sec": 0.0,
      "description": "candidate.description 그대로 복사 또는 더 상세하게 (자막용)",
      "character_focus": ["인물명"],
      "use_original_audio": true,
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/배경 (13자 이내)",
      "title_line2": "캐릭터/사건 후킹 (13자 이내)"
    }}
  ],
  "selected_storyline_index": 0,
  "shorts_type": "storytelling|highlight",
  "selection_reason": "이 storyline을 선택한 이유 — thread의 바이럴 가능성 중심",
  "selected_storyline": {{}},
  "title_line1": "최종 제목 1줄",
  "title_line2": "최종 제목 2줄",
  "title_txt": "title_line1 + title_line2 합친 전체 제목"
}}

"""





TTS_PLANNING_PROMPT = """
# Role
너는 한국 쇼츠/예능 콘텐츠를 위한 내레이션 디렉터다. 결정된 클립 시퀀스를 받아
"어디에 / 무엇을 / 어떤 목소리·속도로" TTS를 얹을지 결정한다.

# 입력
- 작품명: {work_title}
- 쇼츠 총 길이(편집 타임라인 기준): {total_duration:.1f}초 (0초 = 쇼츠 시작점)
{work_context_block}{episodes_context_block}

[클립 시퀀스 — 편집 타임라인 절대 시간]
각 클립에는 세 가지 텍스트 정보가 있다:
- description: LLM의 시각 분석 추정 (인물·동작 추정 — 부정확할 수 있음)
- transcript: Whisper로 전사한 **실제 음성 대사** (정확한 사실)
- tts_draft: 영상 분석 단계(analyze_chunk)에서 *영상을 직접 보면서* 적어둔 **이 한 컷의 내레이션 초안**
  (이 clip 단독 기준으로 작성됨 — 어떤 storyline·역할·인접 clip과 묶일지는 모르고 쓰임)

⚠️ **transcript 우선 원칙 (라운드 16)**: 내레이션을 쓸 때는 transcript를 1순위 사실로 신뢰하라.
description과 transcript가 어긋나면 transcript를 따른다.
- 예: description="유미가 주호에게 전화" / transcript="피디님, 저예요" → 내레이션은
  "유미가 PD에게 전화했다"는 사실을 반영해야 한다 ("주호에게 보낸 문자" 같은 LLM 추정 금지).
- 예: description="문자를 보낸다" / transcript="여보세요? 작가님" → 통화 사실을 반영
  ("보낸 설레는 문자" ❌ → "건넨 한 통의 전화" / "밤늦게 걸려온 그의 한 마디" ✅).

⚠️ **tts_draft 활용 원칙**:
1. tts_draft는 **영상 시각 단서에서 직접 뽑은 한 컷 단독 초안**이다. 사실성(영상에 실제로 보이는 사건)은 신뢰하라.
2. 다만 tts_draft는 *이 컷이 어떤 storyline·역할로 쓰일지 모른 채* 작성된 것이다. 따라서 **storyline 흐름·인접 cue와의 맥락 연결은 너의 책임**이다:
   - 각 컷의 tts_draft를 그대로 베끼지 말고, 직전 cue와 이어지도록(같은 명사·주어 반복, 결과를 받는 종결 등) 다듬어 cue.text로 옮겨라.
   - 첫 cue가 도입을 깔면 다음 cue는 그 도입을 받는 전개, 마지막은 결말이 되도록.
   - tts_draft 시퀀스를 한 줄씩 이어 읽었을 때 한 편의 흐름이 되어야 한다.
3. transcript와 tts_draft가 충돌하면 transcript가 우선 (사실성 원칙).
4. tts_draft가 비어 있거나 이 storyline 흐름에 맞지 않으면 cue를 만들지 않아도 된다 — TTS는 꼭 필요한 곳에만(작성 규칙 1).

{clips_str}

# 사용 가능한 voice 프리셋 (정확히 이 라벨만 사용)

[자연스러운 한국어 — 우선 사용]
- ko_female       : 기본 한국 여성 (차분, 자연스러운 발음)
- ko_female_high  : 밝은 한국 여성 (피치 높음, 트렌드 톤·임팩트)
- ko_male         : 기본 한국 남성 (차분 다큐풍)
- ko_male_low     : 낮은 한국 남성 (피치 낮음, 묵직·진지)

[트렌드 multilingual — 작품 톤이 챗봇/이국·캐주얼/시크 등에 어울릴 때만]
- chat_emma       : 밝고 명료한 챗봇 여성 (en, 트렌드 AI 보이스 느낌)
- chat_brian      : 친근한 캐주얼 남성 (en)
- chat_seraphina  : 차분한 유럽계 여성 (de, 시크·고급)
- chat_florian    : 차분한 유럽계 남성 (de, 진중)

⚠️ multilingual voice는 한국어를 처리할 수 있지만 약간의 외국 억양이 섞일 수 있다. 작품 톤이 한국 드라마 일반(스릴러·로맨스·예능)이면 ko_* 를 우선 선택하라.

# 사용 가능한 speed 라벨 (정확히 이 5개만)
- very_slow / slow / normal / fast / very_fast

# 작성 규칙
1. **TTS는 꼭 필요한 곳에만**. 모든 컷에 다는 것 금지. 보통 클립 1개당 0~2개, 전체 2~5개 cue 정도.
2. cue.start_sec / end_sec 는 **편집 타임라인 절대 시간** (0초 = 쇼츠 시작). 위 [클립 시퀀스]에 명시된 edit_timeline 범위 안에 들어가야 한다.
3. cue 길이(end_sec-start_sec)는 보통 2~6초.
4. cue들끼리 시간이 겹치지 않게 하라(같은 시점에 두 목소리가 동시에 나오면 안 됨).
5. 결정된 클립의 원본 오디오를 죽이지 않게: cue 텍스트가 클립의 핵심 대사와 동시에 충돌하지 않도록 배치.
6. **결과선공개형 타임점프 cue**: hook의 원본 타임라인(원본 start_sec)이 build 클립들보다 뒤에 있으면 결과선공개형이다. 이 경우 build[0] 시작 시점에 타임점프를 알리는 cue를 반드시 배치하라.
   - ❌ 의문형 유도 절대 금지: "대체 무슨 일이?", "어떻게 이렇게 됐을까?", "왜 이런 일이 벌어진 걸까?"
   - ✅ 상황이 시작된 시점·맥락을 단언하라 — 명사형 종결 또는 단언체로
7. **맥락 연속성 (영상 설명 + 흐름)**: 각 cue는 그 시점의 영상에서 *실제로 일어나는 사건*을 짧게 설명하면서, 인접 cue와 한 호흡으로 이어져야 한다. 한 cue가 도입을 깔면 다음 cue는 그 도입을 받는 전개·반전이 되도록 — clip의 tts_draft 시퀀스가 그 기준점이다. cue들을 한 줄씩 이어 읽었을 때 시청자가 "무슨 영상인지" 한 줄로 답할 수 있어야 한다.

# 텍스트 톤 (가장 중요)

**쇼츠 내레이션이다. 뉴스 헤드라인체도, 예능·슬랭 톤도 둘 다 금지.**
방향: **"상황을 짧게 설명해 다음 장면이 궁금해지게 만든다"** — 후킹·여운·인물 명사화.

[금지]
- ❌ 격식체 / 헤드라인체: "~합니다, ~됩니다, ~입니다, 마침내, 비로소, 새로운 ~의 탄생"
- ❌ 가벼운 슬랭·예능톤·반말: "~네, ~함, ~임, ㅋㅋ, 헐, 미친 설계, 통째로 먹었네, 한 방에 다 뒤집힘"
- ❌ 시청자 직접 호명·말 거는 느낌: "봐봐, 잘 봐, 이거 진짜?"

[권장 — 다음 셋 중 하나의 결로 작성하라]
1. **명사형 종결 (인물·사건을 라벨링)**
   - "결국 시장을 통째로 장악한 희로."
   - "이 판을 뒤집을 한 사람."
   - "마켓의 진짜 주인이 바뀌는 순간."
2. **상황 설명 + 여운 (~다 / ~된다 / ~인 셈)**
   - "조용히 판을 다시 짠다."
   - "그가 노린 건 시장 그 자체였다."
   - "이 한 수로 판세가 뒤집힌다."
3. **궁금증 유발 (~는데? / ~ㄴ데? / 근데 ~)**
   - "근데 이게 진짜 끝이 아니다."
   - "그가 진짜 노린 건 따로 있는데?"
   - "여기까지가 시작이라면?"

[길이·구조]
- 한 cue = **한 문장**, 12~25자 권장. 너무 짧으면 정보 부족, 너무 길면 후킹력 약해짐.
- 평서 위주, 의문은 cue 전체의 1/3 이내.
- 어미는 "~다 / ~ㄴ다 / ~인 셈 / ~의 X / ~는데? / 명사형 점.". "~네 / ~함 / ~임"은 사용하지 않는다.

[좋은 예 vs 나쁜 예]
- ❌ "결국 시장을 통째로 먹었네." → ✅ "결국 시장을 통째로 장악한 희로."
- ❌ "한 방에 다 뒤집힘." → ✅ "이 한 수로 판세가 뒤집힌다."
- ❌ "근데 진짜 노림수는 이거였음." → ✅ "그가 진짜 노린 건 따로 있는데?"
- ❌ "지옥 같은 마켓에 나타난 천재." → ✅ "지옥 같은 마켓에 들어선 한 사람." (또는 명사 종결 그대로 OK)
- ❌ "중독까지 계산한 미친 설계." → ✅ "중독까지 계산한 한 수의 설계."

# voice / speed 매핑 가이드

**🚫 한 쇼츠 = 한 voice (절대 규칙)**
이 storyline의 **모든 cue는 같은 voice 라벨을 사용해야 한다**. cue마다 voice를 바꾸지 마라.
voice는 작품·storyline 전체 톤 1개를 골라 모든 cue에 일관되게 적용하라.
(speed 라벨은 cue마다 달라도 좋다 — 톤 강약은 speed로 만들어라.)

**voice 선택 가이드 — 작품/storyline 톤 → 라벨**:
- 한국 드라마/예능 일반 (디스토피아·스릴러·로맨스·코미디) → 기본 `ko_female` 또는 `ko_male`
- 진지·묵직한 다큐·내레이션톤 → `ko_male_low`
- 가벼운 후킹·바이럴·코믹 톤 → `ko_female_high`
- AI 챗봇/SF/이국적·시크한 분위기 → `chat_emma` / `chat_seraphina` (여성), `chat_brian` / `chat_florian` (남성)

**speed (cue마다 자유 — 같은 voice 안에서 톤 강약 만들기)**:
- 정적 장면, 진지한 한 마디 → `slow` / `very_slow`
- 일반 내레이션 → `normal`
- 임팩트·긴박감·전환 → `fast` / `very_fast`

# 응답 형식 (JSON, 다른 텍스트 금지)
{{
  "tts_cues": [
    {{
      "start_sec": 0.0,
      "end_sec": 4.0,
      "text": "예: 황궁마켓의 유일한 법.",
      "voice": "ko_male_low",
      "speed": "slow",
      "voice_rationale": "디스토피아·스릴러 톤 — 한국 남성 묵직 voice 선택",
      "speed_rationale": "긴장 고조 직전이라 천천히 깔아둠"
    }},
    {{
      "start_sec": 18.0,
      "end_sec": 21.5,
      "text": "근데 진짜 노림수는 따로 있는데?",
      "voice": "ko_male_low",
      "speed": "fast",
      "voice_rationale": "같은 storyline이므로 같은 voice 유지 (절대 규칙)",
      "speed_rationale": "반전 임팩트라 빠르게"
    }}
  ]
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


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model_name: str = "gemini-3.1-pro-preview"
    flash_model_name: str = "gemini-3-flash-preview"
    max_retries: int = 3
    # Google 공식 가이드(Gemini 3.x): temperature/top_p/top_k 같은 샘플링 매개변수는
    # 설정하지 말고 기본값을 따르도록 권장. 카테고리별 thinking_level만 제어한다.
    analysis_thinking_level: str = "high"         # "minimal" | "low" | "medium" | "high"
    relationship_thinking_level: str = "medium"     # "minimal" | "low" | "medium" | "high"
    story_thinking_level: str = "high"            # "minimal" | "low" | "medium" | "high"
    tts_cues_thinking_level: str = "medium"         # "minimal" | "low" | "medium" | "high"
    shorten_thinking_level: str = "medium"          # "minimal" | "low" | "medium" | "high"
    research_thinking_level: str = "medium"         # "minimal" | "low" | "medium" | "high"


class GeminiClient:
    def __init__(self, config: GeminiConfig) -> None:
        self.config = config
        from google import genai
        from google.genai import types
        self.client = genai.Client(api_key=config.api_key)
        self.types = types

    # ─────────────────────────────────────────
    # 청크 분석
    # ─────────────────────────────────────────
    def analyze_chunk(self, payload: dict[str, Any]) -> dict[str, Any]:
        previous_episodes_context_block = ""
        if payload.get("previous_episodes_context"):
            ctx = payload["previous_episodes_context"]
            previous_episodes_context_block = (
                f"\n[이전 에피소드 배경 정보 — 오해 방지 전용]\n{ctx}\n"
                "⚠️ 위 정보는 인물명·관계·사건을 올바르게 식별하기 위한 참고용입니다.\n"
                "장면 선택 기준은 오직 현재 첨부 영상 안에서의 재미·흥미도·화제성입니다.\n"
                "이전 화와의 연관성이 높다는 이유만으로 장면을 선택하거나 높게 평가하지 마세요.\n"
                "타임스탬프는 반드시 현재 첨부 영상의 시작(0초) 기준으로 계산하세요.\n"
            )

        previous_context = ""
        if payload.get("previous_analyses"):
            prev_analyses = payload["previous_analyses"]
            context_parts = []
            for prev in prev_analyses:
                prev_chunk_idx = prev.get("chunk_index", "?")
                context_parts.append(f"\n[이전 청크 분석 결과 — chunk_index={prev_chunk_idx}]")
                if prev.get("summary"):
                    context_parts.append(f"요약: {prev['summary']}")
                if prev.get("candidate_moments"):
                    moments_text = "\n".join([
                        f"  - candidate_index={m.get('candidate_index', '?')}, "
                        f"{m.get('start_sec', 0)}~{m.get('end_sec', 0)}초: {m.get('description', '')}"
                        for m in prev["candidate_moments"][:3]
                    ])
                    context_parts.append(f"주요 모멘트:\n{moments_text}")
                if prev.get("segments"):
                    seg_lines = [
                        f"  - {s.get('start_sec', 0):.1f}~{s.get('end_sec', 0):.1f}초: {s.get('description', '')[:120]}"
                        for s in prev["segments"][:8]
                    ]
                    context_parts.append("전체 타임라인 묘사 (segments):\n" + "\n".join(seg_lines))
            if context_parts:
                previous_context = (
                    "\n\n이전 청크들의 분석 결과 (전체 흐름 이해용 — continues_from 참조 시 위 chunk_index/candidate_index 값을 그대로 사용할 것):"
                    + "\n".join(context_parts)
                )

        chunk_start = payload["chunk_start_sec"]
        chunk_end = payload["chunk_end_sec"]

        # 자막 텍스트 (origin/test: transcript_text)
        raw_segments = payload.get("transcript_segments") or []
        if raw_segments:
            filtered = [
                s for s in raw_segments
                if hasattr(s, "start_sec") and s.start_sec >= chunk_start and s.end_sec <= chunk_end
            ]
            transcript_text_str = "\n".join(
                f"[{s.start_sec:.1f}~{s.end_sec:.1f}] {s.text}" for s in filtered
            ) or "없음"
            transcript_text_str = transcript_text_str.replace("{", "{{").replace("}", "}}")
        else:
            transcript_text_str = "없음"

        # 작품 컨텍스트 (시놉시스/장르/핵심 요소)
        work_context_block = ""
        if payload.get("work_context"):
            work_context_block = (
                f"\n[작품 정보 — 인물 식별·스토리 이해 참고용]\n{payload['work_context']}\n"
                "⚠️ 위 정보는 인물명·관계·장르를 정확히 파악하기 위한 참고용입니다.\n"
                "장면 선택 기준은 리서치가 아니라 오직 현재 영상 내 감정 강도·반응·의외성으로 판단하세요.\n"
            )

        # 청크 길이에 비례한 최소 후보 수 (1분당 1개, 올림, 최소 1개)
        chunk_duration = chunk_end - chunk_start
        min_candidates = max(1, -(-int(chunk_duration) // 60))

        # narrative_skeleton 블록 — 라운드 6a에서 단계 자체 제거. payload의 skeleton은 무시.

        # face_id 사전 인식 결과 블록 (선택적 — 인덱스가 없으면 빈 문자열)
        character_appearances_block = ""
        _appearances = payload.get("character_appearances") or []
        if _appearances:
            ap_lines = [
                f"- {a.get('character', '?')}: {float(a.get('start_sec', 0)):.1f}~{float(a.get('end_sec', 0)):.1f}초"
                for a in _appearances
            ]
            character_appearances_block = (
                "\n[face_id 사전 인식 결과 — 참고용]\n"
                "외부 얼굴 인식기가 추정한 캐릭터 등장 구간이다. 같은 인물명을 라벨로 일관되게 사용하되, "
                "픽셀에서 명백히 다르게 보이는 경우 영상 분석 결과를 우선한다.\n"
                + "\n".join(ap_lines)
            )

        prompt = GEMINI_PROMPT_TEMPLATE.format(
            work_title=payload["work_title"],
            topic=payload["topic"],
            chunk_index=payload.get("chunk_index", 0),
            chunk_start_sec=chunk_start,
            chunk_end_sec=chunk_end,
            transcript_text=transcript_text_str,
            scene_boundaries=payload.get("scene_boundaries") or "없음",
            previous_context=previous_context,
            previous_episodes_context_block=previous_episodes_context_block,
            work_context_block=work_context_block,
            character_appearances_block=character_appearances_block,
            min_candidates=min_candidates,
        )

        video_path = payload.get("video_path")
        content_parts = [prompt]
        uploaded_file = None

        if video_path:
            video_path_obj = Path(video_path) if isinstance(video_path, str) else video_path
            if video_path_obj.exists():
                safe_path, is_tmp = _safe_upload_path(video_path_obj)
                for upload_attempt in range(self.config.max_retries):
                    try:
                        uploaded_file = self.client.files.upload(file=str(safe_path))
                        while uploaded_file.state.name == "PROCESSING":
                            time.sleep(2)
                            uploaded_file = self.client.files.get(name=uploaded_file.name)
                        if uploaded_file.state.name == "FAILED":
                            raise RuntimeError("Gemini File API 업로드 실패")
                        content_parts.append(self.types.Part(
                            file_data=self.types.FileData(
                                file_uri=uploaded_file.uri,
                                mime_type="video/mp4",
                            ),
                        ))
                        break
                    except Exception as upload_err:
                        if upload_attempt == self.config.max_retries - 1:
                            raise RuntimeError(
                                f"비디오 업로드 {self.config.max_retries}회 모두 실패 — 분석을 중단합니다.\n"
                                f"원인: {upload_err}"
                            )
                        wait = 2 ** upload_attempt
                        print(f" [WARN] 업로드 오류 (시도 {upload_attempt + 1}/{self.config.max_retries}), {wait}초 후 재시도: {upload_err}")
                        time.sleep(wait)
                if is_tmp and safe_path.exists():
                    safe_path.unlink()

        try:
            for attempt in range(self.config.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=self.config.model_name,
                        contents=content_parts,
                        config=self.types.GenerateContentConfig(
                            response_mime_type="application/json",
                            thinking_config=self.types.ThinkingConfig(
                                thinking_level=self.config.analysis_thinking_level,
                            ),
                        ),
                    )

                    if not response or not response.text:
                        error_msg = "Gemini API가 빈 응답을 반환했습니다."
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError(error_msg)
                        print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                        continue

                    text = response.text.strip()
                    if not text:
                        error_msg = "Gemini API 응답이 빈 문자열입니다."
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError(error_msg)
                        print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                        continue

                    json_text = _extract_json_from_markdown(text)
                    if not json_text:
                        error_msg = f"Gemini 응답에서 JSON을 추출할 수 없습니다. (원문: {text[:200]!r})"
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError(error_msg)
                        print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                        continue
                    data = json.loads(json_text)
                    # Gemini가 JSON 배열로 응답하는 경우 첫 번째 요소 사용
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    data["chunk_start_sec"] = payload["chunk_start_sec"]
                    data["chunk_end_sec"] = payload["chunk_end_sec"]
                    _validate_gemini_schema(data)
                    return data

                except Exception as e:
                    if attempt == self.config.max_retries - 1:
                        raise e
                    wait = 2 ** attempt
                    print(
                        f"    [WARN] Gemini 응답 검증 실패 "
                        f"(시도 {attempt + 1}/{self.config.max_retries}), "
                        f"{wait}초 후 재시도: {type(e).__name__}: {e}"
                    )
                    time.sleep(wait)
                    continue

            raise RuntimeError("Gemini 분석 시도 횟수 초과")

        finally:
            if uploaded_file:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                    print(f" [INFO] Gemini File API 서버 파일 삭제 완료: {uploaded_file.name}")
                except Exception as del_err:
                    print(f" [WARN] Gemini File API 서버 파일 삭제 실패: {del_err}")

    # ─────────────────────────────────────────
    # 영상 의도 사전 분석 (Flash 모델, 전체 프록시 1회 스캔)
    # ─────────────────────────────────────────
    def analyze_video_intent(
        self,
        proxy_path: Path,
        work_title: str,
        topic: str,
        work_context: str | None = None,
        previous_episodes_context: str | None = None,
    ) -> dict[str, Any]:
        """전체 프록시 영상을 1회 스캔해 narrative_skeleton을 생성합니다.

        Flash 모델로 빠르게 영상의 핵심 의도/감정 아크/핵심 인물/콘텐츠 구조를 파악하고,
        이후 청크 분석 및 스토리 구성 단계에 주입되어 청크가 '전체 그림 안에서 이 장면은 뭐야?'
        를 알 수 있게 합니다.
        """
        work_context_line = f"\n[작품 정보]\n{work_context}\n" if work_context else ""
        episodes_context_line = (
            f"\n[이전 에피소드 배경 정보 — 오해 방지 전용]\n{previous_episodes_context}\n"
            "⚠️ 위 정보는 인물명·관계·사건을 올바르게 식별하기 위한 참고용입니다.\n"
        ) if previous_episodes_context else ""
        prompt = (
            "너는 영상 분석 전문가다. 아래 첨부된 전체 영상을 빠르게 스캔해 "
            "narrative_skeleton JSON을 생성하라. 반드시 JSON만 출력, 코드블록 금지.\n\n"
            f"[입력]\n- 작품명: {work_title}\n- 주제: {topic}\n"
            f"{work_context_line}"
            f"{episodes_context_line}\n"
            "[공통 출력 필드 — 포맷 무관하게 반드시 포함]\n"
            "{\n"
            '  "intent": "이 영상이 시청자에게 전달하려는 핵심 의도 (1-2문장)",\n'
            '  "emotional_arc": [\n'
            '    {"phase": "오프닝", "emotion": "호기심", "time_range": "0-120"}\n'
            "  ],\n"
            '  "key_characters": [\n'
            '    {\n'
            '      "name": "인물A (자막/대사에서 들린 실제 이름 우선, 없으면 얼굴 기반 묘사)",\n'
            '      "role": "주인공",\n'
            '      "arc": "불신 → 신뢰",\n'
            '      "name_sources": ["자막에서 직접 언급", "다른 인물이 부름", "추정"]\n'
            '    }\n'
            "  ],\n"
            "⚠️ key_characters 수집 원칙:\n"
            "  -'work_context'에 없더라도 자막/대사에서 불린 이름이 있으면 반드시 그 이름을 사용\n"
            "  - 한 번이라도 이름이 언급된 인물은 모두 포함 (주연뿐 아니라 조연/게스트 포함)\n"
            "  - 이름을 전혀 알 수 없는 경우에만 '30대 여성 주인공' 형태 사용\n"
            "  - name_sources: 이름을 어떻게 알았는지 명시 (이름 신뢰도 판단용)\n"
            '  "tone_keywords": ["묵직함", "반전 서스펜스"],\n'
            '  "story_beats": [\n'
            '    {"time_range": "0-180", "beat": "무슨 일이 벌어지는지 객관적으로 1문장"}\n'
            "  ],\n"
            '  "has_intro": false,\n'
            '  "intro_end_sec": 0,\n'
            '  "intro_description": "오프닝 타이틀/크레딧/로고 시퀀스가 있으면 true, 끝나는 시간(초). 없으면 false, 0",\n'
            '  "has_credits": false,\n'
            '  "credits_start_sec": 0,\n'
            '  "credits_description": "엔딩 크레딧/스태프롤/예고편이 시작되면 true, 시작 시간(초). 없으면 false, 0"\n'
            "}\n\n"
            "⚠️ 영상에 실제로 존재하는 정보만 담아라. 추측 금지.\n"
            "⚠️ story_beats: 영상 전체를 시간 순서대로 5~10개 구간으로 나눠 각 구간에서 실제로 벌어지는 일을 1문장으로 기술하라. "
            "감정 평가나 해석 없이 객관적 사실만 묘사한다 (예: 'A가 B에게 편지를 건네고 B는 읽지 않고 돌아선다'). "
            "행동의 범주를 바꾸지 마라 — 질문은 질문으로, 대화는 대화로, 침묵은 침묵으로 기술하고, 확인되지 않은 의도·결과를 덧씌우지 마라.\n"
            "⚠️ 인트로/크레딧 감지: 실제 타이틀 시퀀스, 스태프롤, 출연진 자막 등이 보이는 경우에만 true로 표기.\n"
        )

        uploaded_file = None
        content_parts: list = [prompt]
        proxy_path_obj = Path(proxy_path)
        if proxy_path_obj.exists():
            safe_path, is_tmp = _safe_upload_path(proxy_path_obj)
            for upload_attempt in range(self.config.max_retries):
                try:
                    uploaded_file = self.client.files.upload(file=str(safe_path))
                    while uploaded_file.state.name == "PROCESSING":
                        time.sleep(2)
                        uploaded_file = self.client.files.get(name=uploaded_file.name)
                    if uploaded_file.state.name == "FAILED":
                        raise RuntimeError("Gemini File API 업로드 실패")
                    content_parts.append(self.types.Part(
                        file_data=self.types.FileData(
                            file_uri=uploaded_file.uri,
                            mime_type="video/mp4",
                        ),
                    ))
                    break
                except Exception as upload_err:
                    if upload_attempt == self.config.max_retries - 1:
                        raise RuntimeError(
                            f"프록시 업로드 {self.config.max_retries}회 모두 실패: {upload_err}"
                        )
                    time.sleep(2 ** upload_attempt)
            if is_tmp and safe_path.exists():
                safe_path.unlink()

        try:
            for attempt in range(self.config.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=self.config.flash_model_name,
                        contents=content_parts,
                        config=self.types.GenerateContentConfig(
                            response_mime_type="application/json",
                            thinking_config=self.types.ThinkingConfig(
                                thinking_level=self.config.analysis_thinking_level,
                            ),
                        ),
                    )
                    if not response or not response.text:
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError("Gemini Flash가 빈 응답을 반환했습니다.")
                        continue
                    text = response.text.strip()
                    if not text:
                        continue
                    json_text = _extract_json_from_markdown(text)
                    data = json.loads(json_text)
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    return data
                except Exception as e:
                    if attempt == self.config.max_retries - 1:
                        print(f"    [WARN] analyze_video_intent 실패: {e} — 빈 skeleton 반환")
                        return {}
                    time.sleep(2 ** attempt)
            return {}
        finally:
            if uploaded_file:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass

    # ─────────────────────────────────────────
    # 후보 장면 관계 그래프 추출
    # ─────────────────────────────────────────
    def extract_relationships(self, all_candidates: list) -> list[dict]:
        """후보 장면들 사이의 관계 엣지를 Pro 모델로 추출한다 (텍스트 전용, 영상 업로드 없음)."""
        slim_fields = (
            "chunk_index", "candidate_index", "start_sec", "end_sec",
            "description", "characters_in_scene",
            "requires_context", "continues_from", "transcript",
        )
        candidates_str = ""
        for m in all_candidates:
            slim = {k: m[k] for k in slim_fields if k in m}
            candidates_str += f"- {json.dumps(slim, ensure_ascii=False)}\n"

        prompt = RELATIONSHIP_EXTRACTION_PROMPT.format(candidates_str=candidates_str)

        for attempt in range(self.config.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.config.model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.relationship_thinking_level,
                        ),
                    ),
                )
                text = (response.text or "").strip()
                if not text:
                    raise RuntimeError("빈 응답")
                data = json.loads(_extract_json_from_markdown(text))
                edges = data.get("edges", [])
                if not isinstance(edges, list):
                    raise ValueError("edges 필드가 리스트가 아님")
                return edges
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    print(f"    [WARN] 관계 그래프 추출 실패: {e} — 빈 엣지 반환")
                    return []
                time.sleep(2 ** attempt)
        return []

    # ─────────────────────────────────────────
    # 스토리 구성 v2 (바이럴 최적화)
    # ─────────────────────────────────────────
    def compose_story_with_context(
        self,
        all_candidates: list,
        work_title: str,
        topic: str,
        min_duration_sec: float = 50.0,
        max_duration_sec: float = 60.0,
        work_context: str | None = None,
        narrative_skeleton: dict | None = None,
        previous_episodes_context: str | None = None,
        relationship_edges: list[dict] | None = None,
        chunk_meta: list[dict] | None = None,
    ) -> dict[str, Any]:
        """후보 장면들로 바이럴 최적화 스토리라인을 구성합니다.

        하이라이트형 3개 + 서사형 3개를 생성하고 최적 1개를 선정합니다.
        JSON 강제 응답 + 구조 검증 + 3회 재시도 + 폴백을 적용합니다.
        """
        candidates_str = ""
        for m in all_candidates:
            candidates_str += f"- {json.dumps(m, ensure_ascii=False)}\n"

        work_context_block = ""
        if work_context:
            work_context_block = (
                f"\n[작품 정보 — 인물 식별·장르 이해 참고용]\n{work_context}\n"
                "⚠️ 위 정보는 인물명·관계·장르를 정확히 파악하기 위한 참고용입니다.\n"
                "스토리라인 구성 기준은 리서치가 아니라 후보 장면들의 감정 강도·반응·의외성입니다.\n"
            )

        episodes_context_block = ""
        if previous_episodes_context:
            episodes_context_block = (
                f"\n[이전 에피소드 배경 정보 — 오해 방지 전용]\n{previous_episodes_context}\n"
                "⚠️ 위 정보는 인물명·관계·사건을 올바르게 파악하기 위한 참고용입니다.\n"
                "스토리라인 구성 기준은 이번 화 후보 장면들의 재미·흥미도·화제성입니다.\n"
                "이전 화와의 연관성이 높다는 이유만으로 장면을 선택하거나 높게 평가하지 마세요.\n"
            )

        story_topic_line = f"[핵심 주제] {topic}" if topic else ""

        # narrative_skeleton 블록 — 라운드 6a에서 단계 자체 제거. 인자는 호환성 유지용.

        # ── segments 요약 블록 (청크별 시간순 — 영상 전체 흐름 빈틈없이 보여주기) ──
        segments_summary_block = ""
        if chunk_meta:
            parts: list[str] = []
            for cm in chunk_meta:
                ci = cm.get("chunk_index", 0)
                summary = (cm.get("summary") or "").strip().replace("\n", " ")
                segs = cm.get("segments") or []
                lines: list[str] = [f"\n[chunk {ci}] 요약: {summary[:120]}"]
                for s in segs:
                    ss = float(s.get("start_sec", 0))
                    ee = float(s.get("end_sec", 0))
                    desc = (s.get("description") or "").strip().replace("\n", " ")
                    if len(desc) > 100:
                        desc = desc[:100] + "…"
                    lines.append(f"  - {ss:>6.1f}~{ee:>6.1f}s  {desc}")
                parts.append("\n".join(lines))
            if parts:
                joined = "\n".join(parts).replace("{", "{{").replace("}", "}}")
                segments_summary_block = (
                    "\n\n[전체 장면 흐름 (segments) — 청크별 시간순]\n"
                    "(이 요약은 영상 전체 흐름을 빈틈없이 보여준다. candidate_moments는 그 중 가치 있는 일부만 추린 것.)\n"
                    + joined
                )

        prompt = STORY_COMPOSITION_PROMPT.format(
            work_title=work_title,
            topic=topic,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            candidates_str=candidates_str,
            work_context_block=work_context_block,
            episodes_context_block=episodes_context_block,
            segments_summary_block=segments_summary_block,
            story_topic_line=story_topic_line,
        )

        # ── 관계 그래프 블록 (프롬프트 뒤에 추가) ──
        if relationship_edges:
            type_rules = {
                "setup_payoff": "required=true이면 두 클립을 반드시 함께 사용하거나 둘 다 제외",
                "continuous": "sequence_id에 이미 반영됨. 인접 배치 권장",
                "duplicate": "둘 중 하나만 선택. 동일 스토리라인에 중복 사용 금지",
                "character_arc": "from → to 순서 유지 필수",
                "sequence": "같은 서사 라인. 같이 쓰면 효과적이나 필수는 아님",
                "consequence": "from이 to의 감정적 원인. 인접하지 않아도 to 앞에 from이 선행돼야 함",
                "contrast": "인접 배치 시 감정 낙차 극대화",
            }
            lines = ["\n[장면 관계 그래프 — 반드시 준수]"]
            lines.append("아래 관계를 스토리라인 구성 시 반드시 참고하라.\n")
            for edge in relationship_edges:
                f = edge.get("from", {})
                t = edge.get("to", {})
                etype = edge.get("type", "")
                req = edge.get("required", False)
                note = edge.get("note", "")
                rule = type_rules.get(etype, "")
                req_str = " [REQUIRED]" if req else ""
                lines.append(
                    f"- [{f.get('chunk_index')},{f.get('candidate_index')}]"
                    f" → [{t.get('chunk_index')},{t.get('candidate_index')}]"
                    f" | {etype}{req_str} | {note}"
                )
                if rule:
                    lines.append(f"  ↳ 규칙: {rule}")
            prompt += "\n".join(lines)

        for attempt in range(self.config.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.config.flash_model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.story_thinking_level,
                        ),
                    ),
                )

                if not response or not response.text:
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError("Gemini API가 빈 응답을 반환했습니다.")
                    print(f"    [WARN] 빈 응답, 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                    continue

                text = response.text.strip()
                if not text:
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError("Gemini API 응답이 빈 문자열입니다.")
                    continue

                json_text = _extract_json_from_markdown(text)
                result = json.loads(json_text)

                # 구조 검증
                _validate_story_response(result)

                # selected_storyline 자동 채우기
                selected_idx = result.get("selected_storyline_index", 0)
                storylines = result.get("storylines", [])
                if not isinstance(selected_idx, int) or selected_idx < 0 or selected_idx >= len(storylines):
                    # 최고 score로 자동 선택
                    best_idx = max(range(len(storylines)), key=lambda i: storylines[i].get("score", 0))
                    result["selected_storyline_index"] = best_idx
                    selected_idx = best_idx

                # 복합 점수 = viral*0.6 + coherence*0.4
                for _sl in storylines:
                    _v = float(_sl.get("score", 0) or 0)
                    _c = float(_sl.get("coherence_score", 0) or 0)
                    _sl["composite_score"] = _v * 0.6 + _c * 0.4

                result["selected_storyline"] = storylines[selected_idx]
                # 전체 storylines를 복합 점수 순으로 정렬 (멀티쇼츠용)
                result["ranked_storylines"] = sorted(
                    storylines, key=lambda s: s.get("composite_score", s.get("score", 0)), reverse=True
                )
                return result

            except json.JSONDecodeError as json_err:
                if attempt == self.config.max_retries - 1:
                    print(f"    [ERROR] JSON 파싱 실패: {json_err}")
                    break
                print(f"    [WARN] JSON 파싱 실패, 재시도... ({attempt + 1}/{self.config.max_retries})")
                time.sleep(2 ** attempt)
            except ValueError as val_err:
                if attempt == self.config.max_retries - 1:
                    print(f"    [ERROR] 응답 검증 실패: {val_err}")
                    break
                print(f"    [WARN] 응답 검증 실패, 재시도... ({attempt + 1}/{self.config.max_retries})")
                time.sleep(2 ** attempt)
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    print(f"    [ERROR] 스토리 구성 실패: {e}")
                    break
                time.sleep(2 ** attempt)

        # 폴백: 최고 점수 moment를 highlight 클립으로 사용
        print("    [FALLBACK] Gemini 스토리 구성 실패 — 최고 점수 moment로 하이라이트 클립 생성")
        return _build_fallback_story(all_candidates, work_title)

    def plan_tts_cues(
        self,
        clips: list,
        work_title: str,
        narrative_skeleton: dict | None = None,
        work_context: str | None = None,
        previous_episodes_context: str | None = None,
        transcript_segments: list | None = None,
        chunk_meta: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """결정된 storyline의 클립 시퀀스를 받아 TTS cue 리스트를 반환한다.

        cue.start_sec / end_sec은 편집 타임라인 절대 시간(0초 = 쇼츠 시작) 기준.
        voice/speed는 app.modules.tts의 프리셋 라벨.

        라운드 16: transcript_segments(원본 시간 기준 Whisper 전사)를 받아
        각 clip의 실제 대사를 프롬프트에 포함 → LLM 시각 추정 오류 방지.
        """
        # 클립을 편집 타임라인 절대 시간 기준으로 직렬화
        cum = 0.0
        clips_lines: list[str] = []
        # 각 clip 원본 영역에 겹치는 transcript 모음
        for i, c in enumerate(clips):
            dur = float(c.end_sec - c.start_sec)
            edit_start = cum
            edit_end = cum + dur
            cum = edit_end
            chars = list(getattr(c, "character_focus", ()) or [])
            subtitle = getattr(c, "subtitle", "") or ""
            # 라운드 16: clip 원본 영역과 겹치는 Whisper 전사 텍스트 수집
            clip_transcript_parts: list[str] = []
            if transcript_segments:
                _segs_in_clip = sorted(
                    [s for s in transcript_segments
                     if float(getattr(s, "end_sec", 0)) > c.start_sec
                     and float(getattr(s, "start_sec", 0)) < c.end_sec],
                    key=lambda s: float(getattr(s, "start_sec", 0)),
                )
                for s in _segs_in_clip:
                    t = (getattr(s, "text", "") or "").strip()
                    if t:
                        clip_transcript_parts.append(t)
            transcript_text = " ".join(clip_transcript_parts).strip()
            transcript_repr = transcript_text[:200] if transcript_text else "(전사 없음)"
            tts_draft_text = (getattr(c, "tts_draft", "") or "").strip()
            tts_draft_repr = tts_draft_text[:200] if tts_draft_text else "(없음)"
            clips_lines.append(
                f"- clip {i} (role={c.role}): "
                f"edit_timeline {edit_start:.1f}~{edit_end:.1f}s "
                f"(원본 {c.start_sec:.1f}~{c.end_sec:.1f}s), "
                f"chars={chars}\n"
                f"    description={subtitle[:400]!r}\n"
                f"    transcript={transcript_repr!r}\n"
                f"    tts_draft={tts_draft_repr!r}"
            )
        clips_str = "\n".join(clips_lines) if clips_lines else "(없음)"
        total_duration = cum

        # 컨텍스트 블록
        work_context_block = ""
        if work_context:
            work_context_block = f"\n[작품 정보]\n{work_context}\n"
        episodes_context_block = ""
        if previous_episodes_context:
            episodes_context_block = f"\n[이전 에피소드 요약]\n{previous_episodes_context}\n"
        # narrative_skeleton 블록 — 라운드 6a에서 단계 자체 제거. 인자는 호환성 유지용.

        # ── segments 요약 블록 (회차 전체 흐름 — 클립 사이 행간 보강) ──
        # compose_story와 동일 패턴. 시간 단위는 원본 영상 절대 시간이므로
        # 위 [클립 시퀀스]의 '원본 X.X~Y.Y초'와 같은 축.
        segments_summary_block = ""
        if chunk_meta:
            parts: list[str] = []
            for cm in chunk_meta:
                ci = cm.get("chunk_index", 0)
                summary = (cm.get("summary") or "").strip().replace("\n", " ")
                segs = cm.get("segments") or []
                lines: list[str] = [f"\n[chunk {ci}] 요약: {summary[:120]}"]
                for s in segs:
                    ss = float(s.get("start_sec", 0))
                    ee = float(s.get("end_sec", 0))
                    desc = (s.get("description") or "").strip().replace("\n", " ")
                    if len(desc) > 100:
                        desc = desc[:100] + "…"
                    lines.append(f"  - {ss:>6.1f}~{ee:>6.1f}s  {desc}")
                parts.append("\n".join(lines))
            if parts:
                joined = "\n".join(parts).replace("{", "{{").replace("}", "}}")
                segments_summary_block = (
                    "\n\n[현재 회차 전체 흐름 (segments) — 청크별 시간순]\n"
                    "(시간은 원본 영상 절대 시간 — 위 [클립 시퀀스]의 '원본 X.X~Y.Y초'와 같은 축. "
                    "선정된 클립은 이 전체 흐름의 일부다. 클립 사이의 행간을 메우는 cue를 작성할 때 "
                    "이 정보로 *그 사이에 무엇이 있었는지* 파악하라.)\n"
                    + joined
                )

        prompt = TTS_PLANNING_PROMPT.format(
            work_title=work_title,
            total_duration=total_duration,
            clips_str=clips_str,
            work_context_block=work_context_block,
            episodes_context_block=episodes_context_block,
            segments_summary_block=segments_summary_block,
        )

        valid_voices = {
            "ko_female", "ko_female_high", "ko_male", "ko_male_low",
            "chat_emma", "chat_brian", "chat_seraphina", "chat_florian",
        }
        valid_speeds = {"very_slow", "slow", "normal", "fast", "very_fast"}

        for attempt in range(self.config.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.config.flash_model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.tts_cues_thinking_level,
                        ),
                    ),
                )
                if not response or not response.text:
                    if attempt == self.config.max_retries - 1:
                        return []
                    time.sleep(2 ** attempt)
                    continue
                text = _extract_json_from_markdown(response.text)
                data = json.loads(text)
                raw_cues = data.get("tts_cues", []) if isinstance(data, dict) else []
                cues: list[dict[str, Any]] = []
                for c in raw_cues:
                    if not isinstance(c, dict):
                        continue
                    if "start_sec" not in c or "end_sec" not in c or "text" not in c:
                        continue
                    s = float(c["start_sec"])
                    e = float(c["end_sec"])
                    if e <= s:
                        continue
                    # 편집 타임라인 범위 안 (약간의 여유 허용)
                    if s < -0.5 or e > total_duration + 0.5:
                        continue
                    voice = str(c.get("voice", "ko_female"))
                    if voice not in valid_voices:
                        voice = "ko_female"
                    speed = str(c.get("speed", "normal"))
                    if speed not in valid_speeds:
                        speed = "normal"
                    cues.append({
                        "start_sec": s,
                        "end_sec": e,
                        "text": str(c["text"]).strip(),
                        "voice": voice,
                        "speed": speed,
                    })
                # 시간순 정렬 + 겹침 제거 (뒤 cue가 앞 cue와 겹치면 뒤 cue 시작을 앞 cue 종료 후로 이동)
                cues.sort(key=lambda x: x["start_sec"])
                for i in range(1, len(cues)):
                    if cues[i]["start_sec"] < cues[i - 1]["end_sec"]:
                        cues[i]["start_sec"] = cues[i - 1]["end_sec"] + 0.05
                # 보정으로 end_sec ≤ start_sec가 됐다면 제거
                cues = [c for c in cues if c["end_sec"] > c["start_sec"]]

                # ── 한 쇼츠 = 한 voice 강제 (LLM이 가이드를 어겼을 때 후처리로 통일) ──
                if cues:
                    voice_count: dict[str, int] = {}
                    for c in cues:
                        voice_count[c["voice"]] = voice_count.get(c["voice"], 0) + 1
                    # 다수 등장 voice를 채택, 동률이면 첫 cue voice
                    majority = max(voice_count.items(), key=lambda kv: (kv[1], -list(c["voice"] for c in cues).index(kv[0])))[0]
                    if len(voice_count) > 1:
                        for c in cues:
                            c["voice"] = majority

                return cues
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    print(f"    [ERROR] TTS cue 계획 실패: {e}")
                    return []
                time.sleep(2 ** attempt)
        return []

    # ─────────────────────────────────────────
    # 텍스트 단축 (TTS fit 용도, Flash 모델)
    # ─────────────────────────────────────────
    def shorten_text(self, text: str, *, target_chars: int) -> str:
        """target_chars 이내로 의미를 보존하며 한국어 문장 단축. Flash 모델 사용.

        TTS 합성 결과가 cue 시간 초과 시 호출되어 텍스트를 줄인다.
        실패 시 입력 그대로 반환 (호출부가 단순 절단으로 폴백).
        """
        if not text or target_chars <= 0:
            return text
        if len(text) <= target_chars:
            return text
        prompt = (
            f"다음 한국어 쇼츠 나레이션 문장을 의미·뉘앙스를 보존하며 {target_chars}자 이내로 줄여라. "
            "결과 텍스트만 출력. 따옴표·설명·접두사 금지.\n\n"
            f"{text}"
        )
        for attempt in range(2):
            try:
                response = self.client.models.generate_content(
                    model=self.config.flash_model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.shorten_thinking_level,
                        ),
                    ),
                )
                if response and response.text:
                    out = response.text.strip().strip('"').strip("'")
                    # 첫 줄만 사용 (Flash가 가끔 설명 추가)
                    if "\n" in out:
                        out = out.split("\n", 1)[0].strip()
                    if out and len(out) <= len(text):
                        return out
            except Exception as e:
                if attempt == 1:
                    print(f"    [WARN] shorten_text 실패: {e}")
                time.sleep(1)
        return text


def _build_fallback_story(all_candidates: list, work_title: str) -> dict[str, Any]:
    """Gemini 스토리 구성 실패 시 상위 3-4개 moment를 조합하여 서사형 폴백 생성."""
    if not all_candidates:
        raise RuntimeError("후보 장면이 없어 폴백도 불가능합니다.")

    # 점수 필드가 더 이상 없으므로 시간순 안정 정렬을 폴백 기준으로 사용
    sorted_candidates = sorted(
        all_candidates,
        key=lambda m: m.get("start_sec", 0),
    )

    # 상위 4개 moment 선택 (최소 3개) — 시간순으로 균등하게 샘플링
    if len(sorted_candidates) <= 4:
        top_moments = list(sorted_candidates)
    else:
        n = len(sorted_candidates)
        idxs = [0, n // 3, (2 * n) // 3, n - 1]
        top_moments = [sorted_candidates[i] for i in idxs]

    # 시간순 정렬 (서사 흐름)
    top_moments.sort(key=lambda m: m.get("start_sec", 0))

    if len(top_moments) < 3:
        # moment가 3개 미만이면 있는 것만 사용
        pass

    # hook: 첫 번째, payoff: 마지막, build: 나머지
    hook_m = top_moments[0]
    payoff_m = top_moments[-1]
    build_moments = top_moments[1:-1] if len(top_moments) > 2 else []

    # 총 길이 계산
    total_dur = sum(m.get("end_sec", 0) - m.get("start_sec", 0) for m in top_moments)

    build_clips = [
        {
            "chunk_index": m.get("chunk_index", 0),
            "candidate_index": m.get("candidate_index", 0),
            "start_sec": m["start_sec"],
            "end_sec": m["end_sec"],
            "description": m.get("description", ""),
            "use_original_audio": True,
        }
        for m in build_moments
    ]

    storyline_obj = {
        "hook": {
            "chunk_index": hook_m.get("chunk_index", 0),
            "candidate_index": hook_m.get("candidate_index", 0),
            "start_sec": hook_m["start_sec"],
            "end_sec": hook_m["end_sec"],
            "description": hook_m.get("description", ""),
            "use_original_audio": True,
        },
        "build": build_clips,
        "payoff": {
            "chunk_index": payoff_m.get("chunk_index", 0),
            "candidate_index": payoff_m.get("candidate_index", 0),
            "start_sec": payoff_m["start_sec"],
            "end_sec": payoff_m["end_sec"],
            "description": payoff_m.get("description", ""),
            "use_original_audio": True,
        },
    }

    best = sorted_candidates[0]
    fallback_storyline = {
        "storyline_index": 0,
        "shorts_type": "storytelling",
        "sequence_type": "여정몰입형",
        "topic": best.get("description", work_title)[:30],
        "topic_reason": "Gemini 스토리 구성 실패로 상위 moment 자동 조합",
        "score": 0.5,
        "estimated_duration_sec": total_dur,
        "viral_titles": best.get("viral_titles", [work_title]),
        "title_line1": work_title[:20],
        "title_line2": best.get("description", "")[:20],
        "ending_hook": "",
        "storyline": storyline_obj,
    }

    return {
        "storylines": [fallback_storyline],
        "selected_storyline_index": 0,
        "selection_reason": "폴백: Gemini 응답 실패 — 상위 moment 자동 조합",
        "title_line1": work_title[:20],
        "title_line2": best.get("description", "")[:20],
        "title_txt": f"{work_title[:20]} {best.get('description', '')[:20]}",
        "ending_hook": "",
        "selected_storyline": fallback_storyline,
    }


def _validate_story_response(data: dict[str, Any]) -> None:
    """스토리 구성 응답의 필수 구조를 검증합니다.

    라운드 9: LLM이 'storylines' 외 다른 키로 응답하는 케이스를 자동 정규화
    하여 폴백 빈도를 낮춘다.
    """
    # 라운드 9-A: alias 매핑 (LLM이 다른 키 이름으로 출력해도 정상 처리).
    if "storylines" not in data or not isinstance(data.get("storylines"), list):
        for alias in (
            "shorts", "proposals", "storyline_options", "options",
            "story_proposals", "stories", "story_list", "results", "output"
        ):
            if alias in data and isinstance(data[alias], list) and data[alias]:
                data["storylines"] = data[alias]
                break

    # 라운드 9-B: 단일 selected_storyline만 있는 경우 storylines로 wrap.
    if "storylines" not in data or not isinstance(data.get("storylines"), list):
        sel = data.get("selected_storyline")
        if isinstance(sel, dict) and sel:
            data["storylines"] = [sel]

    # 라운드 9-C: 응답 자체가 단일 storyline dict (배열 누락) 인 경우 wrap.
    # `shorts_type` 키 존재로 단일 storyline 형태 추정.
    if "storylines" not in data or not isinstance(data.get("storylines"), list):
        if "shorts_type" in data and ("storyline" in data or "start_sec" in data):
            data["storylines"] = [dict(data)]

    if "storylines" not in data or not isinstance(data["storylines"], list):
        raise ValueError("응답에 'storylines' 배열이 없습니다.")
    if len(data["storylines"]) == 0:
        raise ValueError("생성된 스토리라인이 없습니다.")

    # title_txt 또는 title_line1+title_line2 필수
    if "title_txt" not in data and "title_line1" not in data:
        raise ValueError("응답에 'title_txt' 또는 'title_line1'이 없습니다.")

    # title_line1/line2 기본값 설정 (하위 호환) — 라운드 22: 15→20자
    if "title_line1" not in data:
        title = data.get("title_txt", "")
        data["title_line1"] = title[:20] if len(title) > 20 else title
        data["title_line2"] = title[20:40] if len(title) > 20 else ""
    if "title_line2" not in data:
        data["title_line2"] = ""
    if "title_txt" not in data:
        data["title_txt"] = f"{data['title_line1']} {data['title_line2']}".strip()

    # ending_hook 기본값
    data.setdefault("ending_hook", "")

    # 라운드 12: sequence_type 미지의 값이면 "여정몰입형"으로 fallback (LLM 출력 안정성)
    _ALLOWED_SEQUENCE_TYPES = {"여정몰입형", "결과선공개형", "반전형", "시퀀스블록형"}

    for idx, sl in enumerate(data["storylines"]):
        if "shorts_type" not in sl:
            raise ValueError(f"storyline[{idx}]에 'shorts_type'이 없습니다.")
        if "score" not in sl:
            raise ValueError(f"storyline[{idx}]에 'score'가 없습니다.")

        # 라운드 12: sequence_type 검증·정규화
        if sl.get("shorts_type") == "storytelling":
            seq_type = sl.get("sequence_type")
            if seq_type not in _ALLOWED_SEQUENCE_TYPES:
                sl["sequence_type"] = "여정몰입형"

        # storyline별 title_line1/line2 기본값
        sl.setdefault("title_line1", data.get("title_line1", ""))
        sl.setdefault("title_line2", data.get("title_line2", ""))
        sl.setdefault("ending_hook", "")

        if sl["shorts_type"] == "highlight":
            for key in ("start_sec", "end_sec"):
                if key not in sl:
                    raise ValueError(f"highlight storyline[{idx}]에 '{key}'가 없습니다.")
        elif sl["shorts_type"] == "storytelling":
            storyline = sl.get("storyline")
            if not storyline:
                raise ValueError(f"storytelling storyline[{idx}]에 'storyline' 객체가 없습니다.")
            for role in ("hook", "payoff"):
                if role not in storyline:
                    raise ValueError(f"storyline[{idx}].storyline에 '{role}'이 없습니다.")
                for key in ("start_sec", "end_sec"):
                    if key not in storyline[role]:
                        raise ValueError(f"storyline[{idx}].{role}에 '{key}'가 없습니다.")


def load_gemini_client() -> GeminiClient:
    project_root = Path(__file__).resolve().parent.parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is required. "
            "Please set it in .env file or as an environment variable."
        )
    model_name = os.getenv("GEMINI_MODEL_NAME", "gemini-3.1-pro-preview")
    flash_model_name = os.getenv("GEMINI_FLASH_MODEL_NAME", "gemini-3-flash-preview")
    max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
    analysis_thinking_level = os.getenv("GEMINI_ANALYSIS_THINKING_LEVEL", "medium")
    relationship_thinking_level = os.getenv("GEMINI_RELATIONSHIP_THINKING_LEVEL", "medium")
    story_thinking_level = os.getenv("GEMINI_STORY_THINKING_LEVEL", "medium")
    tts_cues_thinking_level = os.getenv("GEMINI_TTS_CUES_THINKING_LEVEL", "medium")
    shorten_thinking_level = os.getenv("GEMINI_SHORTEN_THINKING_LEVEL", "medium")
    research_thinking_level = os.getenv("GEMINI_RESEARCH_THINKING_LEVEL", "medium")
    return GeminiClient(GeminiConfig(
        api_key=api_key,
        model_name=model_name,
        flash_model_name=flash_model_name,
        max_retries=max_retries,
        analysis_thinking_level=analysis_thinking_level,
        relationship_thinking_level=relationship_thinking_level,
        story_thinking_level=story_thinking_level,
        tts_cues_thinking_level=tts_cues_thinking_level,
        shorten_thinking_level=shorten_thinking_level,
        research_thinking_level=research_thinking_level,
    ))


def _validate_gemini_schema(data: dict[str, Any]) -> None:
    """Gemini 응답의 최소 스키마를 검증한다.

    - 응답 레벨 필수 키: chunk_index, chunk_start_sec, chunk_end_sec, summary, candidate_moments
    - 모먼트 레벨 필수 키: start_sec, end_sec, description (clip 생성에 직접 필요)
    - 모먼트 레벨 옵셔널 키: reason, transcript (LLM self-explanation/참고용 — 누락 시 ""로 채움)
    - 필수 키가 빠진 모먼트는 drop. 단 candidate_moments 전체가 비면 ValueError 발생 (재시도 트리거).
    """
    required_keys = {
        "chunk_index",
        "chunk_start_sec",
        "chunk_end_sec",
        "summary",
        "candidate_moments",
    }
    missing = required_keys - data.keys()
    if missing:
        raise ValueError(f"Missing keys in Gemini response: {missing}")
    if not isinstance(data["candidate_moments"], list):
        raise ValueError("candidate_moments must be a list")

    # 필수 키 / 옵셔널 키 분리
    moment_required = ("start_sec", "end_sec", "description")
    moment_optional_defaults = {"reason": "", "transcript": ""}

    cleaned: list = []
    dropped: list[str] = []
    for idx, moment in enumerate(data["candidate_moments"]):
        if not isinstance(moment, dict):
            dropped.append(f"#{idx}(not-a-dict)")
            continue
        missing_required = [k for k in moment_required if k not in moment]
        if missing_required:
            dropped.append(f"#{idx}(missing={','.join(missing_required)})")
            continue
        for k, default in moment_optional_defaults.items():
            moment.setdefault(k, default)
        cleaned.append(moment)

    if dropped:
        print(
            f"    [WARN] candidate_moments 중 {len(dropped)}개 결손으로 drop: "
            + ", ".join(dropped[:6])
            + (" …" if len(dropped) > 6 else "")
        )

    data["candidate_moments"] = cleaned
    if not cleaned:
        # 모든 모먼트가 결손이면 응답 자체가 무의미 → 재시도 트리거
        raise ValueError("candidate_moments가 모두 결손되어 유효한 모먼트가 없습니다.")
