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
# 청크 분석 프롬프트 (멀티모달 분석 + 핵심 필드 보존)
# ─────────────────────────────────────────────
GEMINI_PROMPT_TEMPLATE = """
[ROLE]
당신은 한국 최고의 숏폼 전문 AI 에디터이자 멀티모달 스토리 분석 전문가다.
텍스트, 영상, 오디오를 통합적으로 이해하여
바이럴 가능성이 높은 쇼츠 콘텐츠를 기획하고 분석한다.
반드시 JSON만 출력한다. 코드블록 금지.
영상에 없는 내용은 절대 창작하지 말 것.

---

[입력 정보]
- 작품명: {work_title}
- 주제: {topic}
- **현재 청크 번호 (chunk_index): {chunk_index}** ← 출력의 모든 chunk_index 필드는 이 값을 사용할 것
- 청크 범위: {chunk_start_sec} ~ {chunk_end_sec} 초
{work_context_block}
{previous_episodes_context_block}
{character_appearances_block}
- ⚠️ 모든 start_sec / end_sec는 반드시 첨부된 영상 파일의 시작(0초)을 기준으로 한 상대값으로 반환할 것

- 자막(있으면): {transcript_text}
- 씬 경계(있으면): {scene_boundaries}
{previous_context}

---

[인물 식별 단계]

- 분석 시작 전, 아래 수단을 통해 등장 인물의 이름을 먼저 파악한다:
    - **face_id 사전 인식 결과** (있을 경우): 위 `[입력 정보]`의 face_id 블록은 외부 얼굴 인식기가 추정한 캐릭터 등장 구간이다. 같은 인물명을 라벨로 일관되게 사용하라.
    - 화면 자막 또는 이름 자막 (예능/드라마 자막 포함)
    - 대사 내 호칭 (예: "야 민준아~")
    - 화면 내 텍스트 (명찰, 이름표 등 OCR 가능한 텍스트)
- 위 수단으로 이름 확인이 불가능한 인물은 "인물A", "인물B" 등 고유 레이블을 부여한다.
- 이후 모든 분석 항목에서 확정된 이름 또는 레이블을 일관되게 사용한다.
- ❌ 복장/헤어스타일 기반 레이블링 금지 (예: "갈색 재킷 남자"). 동일 인물이 다른 씬에 다른 옷으로 등장해도 같은 이름/레이블을 유지하라.
- **자막 화자태그 > 화면에 보이는 인물**: 반응 컷(cutaway)에서는 화면에 보이는 인물과 실제 화자가 다를 수 있다.
- **화자 태그 없는 대사는 단정짓지 마라**: 자막에 `화자명:` 태그가 없는 대사의 화자를 서사적 추론으로 채우지 마라.

---

[description 규칙]
- description은 compose story 단계를 위한 정보이므로 영상과 자막을 기반으로 시간 순서에 맞게 정확하고 객관적으로 최소 5 문장 이상 자세하게.
- description은 실제 장면 묘사를 유지하고, 앞 장면이 나중 내용에 의해 재해석되는 경우 재해석된 의미는 reason 필드에만 반영할 것. 과대해석 및 과장 금지.
- description에서 행동의 범주를 바꾸지 마라. 장면에서 명확히 관찰된 행동(발화·동작·표정)만 그 종류 그대로 기술하고, 확인되지 않은 의도·감정·결과를 덧씌우지 마라.
- ⚠️ description에서 내레이션·독백·보이스오버(VO)는 반드시 "~의 내레이션", "~가 속으로 독백한다", "VO로 ~가 말한다" 등으로 명시하여 실제 대화와 혼동되지 않도록 할 것.

---

[타임스탬프 정확도 — 절대 규칙 / 어기면 그 후보는 폐기됨]

🚫 가장 중요한 규칙. 어기면 다운스트림에서 자막·TTS·렌더가 모두 어긋난다.

1. **첨부 영상의 시작 = 0.0초**. 모든 start_sec / end_sec는 이 영상의 0초 기준 상대값이다. 원본 풀 영상의 절대 시간이 아니다.
2. **첨부 영상 길이를 초과하는 시간은 절대 출력하지 마라.** 영상 길이가 600초이면 어떤 출력 시간도 600.0을 넘으면 안 된다.
3. **transcript에 적은 대사는 그 timestamp 시점에 실제로 들리는 대사여야 한다.** 영상 안 다른 시점의 대사를 베껴서 다른 시간에 붙이지 마라. transcript 시점과 화면 위치를 다시 한 번 일치시킨 뒤 출력하라.
4. **자막에서 본 시간을 그대로 베끼지 마라.** 자막 텍스트가 [12:30~12:35]에 적혀 있어도, 첨부된 영상에서 그 대사가 실제로 어느 시점에 들리는지 영상 본문을 다시 확인하고 그 시점을 적어야 한다. (영상이 원본의 일부 잘라낸 chunk이므로 자막 절대 시간과 영상 0초 기준 상대 시간은 다르다.)
5. **각 candidate_moments의 start_sec / end_sec는 반드시 영상에서 직접 확인 가능한 구간이어야 한다.** 보지 않은 시점을 추측해서 만들지 마라.
6. **context_extension.extended_start_sec / extended_end_sec 도 chunk-relative 시간**이며 동일하게 [0.0, 첨부 영상 길이] 범위 안에 있어야 한다.
   `extended_start_sec ≤ start_sec ≤ end_sec ≤ extended_end_sec`를 만족시키지 못하면 needed=false로 강등하라.
7. 응답 직전 자가 점검:
   - 모든 start_sec / end_sec / extended_start_sec / extended_end_sec 가 [0.0, 첨부 영상 길이] 범위 안인가?
   - 각 candidate의 transcript가 그 timestamp 위치에서 실제로 들리는가?
   - segments[i].end_sec == segments[i+1].start_sec 가 정확히 같은가?
   - context_extension.needed=true면 extended가 start/end를 정확히 감싸는가?
   하나라도 No 면 출력 직전에 보정하라.

---

[세그먼트(segments) 분할 — 청크 전체 커버]

청크 전체 시간(`chunk_start_sec` ~ `chunk_end_sec`)을 **빈틈 없이, 겹침 없이** 시간순 세그먼트로 분할하라.

[분할 기준 — 우선순위]
1. **장면 전환(scene cut)**: 카메라가 새로운 장소/시점/상황으로 바뀌는 지점. 입력으로 주어진 `scene_boundaries`가 있으면 1차 참고.
2. **서사 단위(beat)**: 같은 장소·등장인물이라도 화제/사건이 명확히 바뀌면 분리.
3. **대화 단위**: 한 인물이 길게 이야기하다가 다른 인물로 발화 주체가 바뀌고 화제도 바뀌면 분리.

[세그먼트 길이 가이드]
- 일반적 길이: 10~60초 권장
- 정적·전환 컷이 길게 이어지면 60초 이상도 허용
- 너무 짧은 마이크로 컷(1~3초)은 인접 세그먼트에 합쳐라

[세그먼트 연속성 제약 — 절대 규칙]
- segments[0].start_sec == chunk_start_sec
- segments[-1].end_sec == chunk_end_sec
- 모든 i에 대해 segments[i].end_sec == segments[i+1].start_sec (gap/overlap 금지)
- segments는 빠짐없이 청크 전체를 덮어야 한다 (평범한 구간도 반드시 포함)

[세그먼트 description]
- 모든 세그먼트는 [description 규칙]에 따라 객관적 묘사를 작성하라
- 평범한/조용한 구간도 짧은 묘사(2~3문장)는 작성 (예: "복도를 천천히 걷는 인물A. 별다른 대사는 없고 발걸음 소리만 들린다.")
- 주목할 만한 핵심 세그먼트는 5문장 이상 상세히 작성

---

[SHORTS TYPE DEFINITION]

candidate_moments는 다음 두 가지 유형의 쇼츠 제작에 모두 활용될 수 있도록 추출한다.

[유형 1: 하이라이트 쇼츠]
- 하나의 강렬한 장면 중심, 맥락 설명 최소화
- 단일 클립으로 시청자가 "뭐지?" → 감정반응 → 완결까지 느낄 수 있는 장면
- 이전 맥락이나 이후 결과에 대한 설명이 전혀 필요하지 않은 장면
- → highlight_eligible: true 로 표기

[유형 2: 서사형 쇼츠]
- 가장 드라마틱한 씬을 중심으로 서사가 자연스럽게 흐르도록 구성
- 다른 장면들과 함께 묶여 hook→build→payoff 흐름을 만들 수 있는 장면

⚠️ hook/build/payoff 역할 결정은 다음 단계(스토리 구성)에서 수행하므로 여기서는 미리 라벨을 붙이지 말 것.

⚠️ **모든 candidate**(highlight·서사형 무관)는 context_extension 필드로 앞뒤 맥락 필요 여부를 판단·출력하라.
다음 단계(스토리 구성)에서 highlight는 단독 클립 자연 확장에, storytelling은 hook/build/payoff 클립 사이의 시간 점프를 줄이는 데 활용된다.

---

[후보 모멘트(candidate_moments) 추출 규칙]

segments 중 **쇼츠 제작에 가치 있는 장면만** 선별해 candidate_moments로 추출한다.

- candidate_moment의 `segment_index`는 segments 배열 인덱스를 가리킨다
- candidate_moment의 start_sec/end_sec은 해당 segment 범위 안에서, 더 좁게 잡아도 된다(핵심만 발췌). 단, segment 범위 밖으로 나가면 안 된다
- 후보 최소 {min_candidates}개 이상 선별

---

[TONE & RULES]
- 모든 출력은 한국어 사용
- 최신 쇼츠 트렌드 반영: 자연스러운 톤, 짧은 문장, 강조/리액션 요소
- candidate_moments 최소 {min_candidates}개 (이 청크에서 주목할 만한 모든 장면을 빠짐없이 포함)
- JSON 스키마 강제. 분석 결과 해당 항목이 없을 경우 빈 배열 대신 null로 출력
- 타이틀 시퀀스, 엔딩 크레딧은 candidate_moments에서 제외 (단, segments에는 포함)
- ⚠️ **영상에 실제로 존재하는 장면만** 포함하라. 추론이나 상상으로 장면을 만들어내지 마라.
- 모든 start_sec/end_sec 구간은 반드시 첨부 영상에서 실제 확인 가능해야 한다.
- "start_sec"~"end_sec" 내의 상황만 봐도 이해가 가능해야 한다.
- ⚠️ 대사가 있는 장면에서 인물의 행동/의도를 묘사할 때는 대사 내용을 최우선으로 반영하라.
  캐릭터 설정(질병, 성격 등 배경 지식)이 대사와 충돌하면 대사를 믿어라.

[characters_tracking 필드 정의 (chunk-level)]
- 청크 전체에 등장하는 인물별 등장 타임스탬프 및 주요 행동을 정리한다.
- character: 인물명 또는 레이블 (인물 식별 단계에서 확정한 이름과 일관되게)
- appearances[]: 해당 인물이 등장하는 구간 목록
    - start_sec / end_sec: 등장 구간
    - action: 그 구간에서 인물의 핵심 행동/발화 요약 한 문장

[segment 필드 정의]
- segment_index: 0부터 시작하는 정수 (segments 배열 인덱스)
- start_sec / end_sec: 이 세그먼트의 시간 범위 (인접 세그먼트와 정확히 맞물려야 함)
- description: 장면 설명(묘사 위주). 위 [description 규칙]을 엄수. 평범한 구간은 2~3문장, 핵심 구간은 5문장 이상
- transcript: 이 구간의 핵심 발화. 내레이션/독백/VO인 경우 '[내레이션]' 접두. 발화 없으면 빈 문자열
- characters_in_scene: 화면에 등장하는 인물 이름 배열
- scene_location: 장면 배경 장소
- timeline_position: "현재" | "과거" | "불명"

[candidate_moment 필드 정의]
- segment_index: 이 후보가 속한 segments 배열의 인덱스 (필수)
- chunk_index: **반드시 위 [입력 정보]의 "현재 청크 번호" 값과 동일**해야 한다. 절대 0으로 고정하거나 임의의 값을 쓰지 말 것.
- candidate_index: 이 청크 내 후보 순서 (0부터 시작)
- continues_from의 chunk_index: 같은 청크 내 후보를 참조하면 위 "현재 청크 번호"와 동일. 이전 청크의 후보를 참조할 때만 [이전 청크들의 분석 결과] 블록에 명시된 chunk_index 값을 그대로 사용할 것.
- start_sec / end_sec: 해당 segment 범위 안의 좁은 핵심 구간 (segment 경계를 넘지 말 것)
- characters_in_scene: 화면에 등장하는 인물 이름 배열
- character_focus: 이 장면의 주요(핵심) 인물 이름 배열
- description: 장면 설명. segment의 description을 그대로 복사하거나, 더 상세하게 보강 가능
- reason: 후보 선정 이유 (재해석된 의미는 여기에만)
- transcript: '단 한 명'의 주요 발화. 내레이션/독백/VO인 경우 '[내레이션]' 접두
- scene_location / timeline_position: segment에서 복사
- continues_from: 다른 candidate_moment와 직접 이어지면 {{"chunk_index": N, "candidate_index": M}}, 독립이면 null
  - 같은 청크 내 후보를 참조 → chunk_index = 위 [입력 정보]의 '현재 청크 번호'와 동일하게
  - 이전 청크 후보를 참조 → chunk_index = 위 [이전 청크들의 분석 결과] 블록에 명시된 chunk_index 값을 그대로 사용
  - ⚠️ 응답 예시의 (0, 3) 같은 좌표를 그대로 베껴 쓰지 마라. 실제 참조 대상의 좌표를 정확히 입력하라.
- requires_context: 이 클립을 이해하려면 다른 장면이 필요하면 true
- highlight_eligible: requires_context가 false이고 클립 자체에 감정적 완결성이 있으면 true
- highlight_reason: highlight_eligible이 true인 경우에만 1문장 (false면 null)
- context_extension: **모든 candidate**에 대해 다음을 판단·출력 (highlight 여부와 무관)
    - needed: 이 candidate를 단독 클립으로 보여줬을 때 시청자가 "뭐지?" 없이 흐름을 따라가려면 앞뒤 맥락이 필요한가?
    - extended_start_sec: needed=true면 인접 segment까지 확장한 시작점 (보통 핵심 직전 5~25초). false면 start_sec와 동일
    - extended_end_sec: needed=true면 인접 segment까지 확장한 종료점 (보통 핵심 직후 5~25초). false면 end_sec와 동일
    - before_summary / after_summary: 확장 부분에 무엇이 있는지 한 문장씩
    - reason: 왜 그 앞뒤가 필수인지 한 줄
    - 강제 조건: extended_start_sec ≤ start_sec ≤ end_sec ≤ extended_end_sec (양쪽 확장만 허용)
    - 확장 구간 안에 컷·장소 변경이 너무 많으면(>2회) needed:false 로 강등하라
    - **라운드 19C-1 기준 완화**: 다음 신호 중 하나라도 있으면 needed=true 적극 적용:
      - candidate 시작 직전 5초 안에 *도입·배경 대사·인물 등장*이 있음
      - candidate 끝 직후 5초 안에 *반응·여운 대사·결과 표정/액션*이 있음
      - 핵심 사건의 의미가 앞뒤 1~2개 segment 없이는 모호해짐
    - storytelling의 hook/build/payoff에서도 활용되어 클립 사이 시간 점프를 줄이는 데 사용된다.

- event_template: **라운드 19D — 행동 의미 라벨링 필수**. 이 후보의 *행동 의미*를 (subject, action, target, mode, location)로 명시.
    - subject: 행동의 주체 (캐릭터명; 영상 안 인물 이름과 일치)
    - action: 무슨 행동인지 간결한 동사구 (예: "맥주 권유", "병문안", "통화로 업무 위임", "고백")
    - target: 행동의 대상 (캐릭터명·장소·물건; 없으면 null)
    - mode: 행동 방식 — `in_person` | `phone_call` | `narration` | `observation` | `mixed`
        - in_person: 두 인물이 같은 장소에서 직접 대면
        - phone_call: 전화·영상통화로 *떨어진 두 사람* 간 소통. 직접 대면 아님
        - narration: 내레이션·독백·VO
        - observation: 한 인물이 다른 인물·상황을 *관찰*만 함 (개입 없음)
        - mixed: 한 candidate 안에서 둘 이상 모드가 섞임 (드물게)
    - location: 장면 발생 장소 (예: "극장 로비", "병원", "야외 거리")
    - ⚠️ phone_call vs in_person 구분 매우 중요. 통화 장면을 in_person으로 잘못 라벨하면 다음 단계(스토리 구성)에서 "X가 Y에게 직접 고백" 같은 잘못된 title 생성됨.

다음 스키마로만 응답 (※ 아래 예시의 숫자는 placeholder다. 실제 값은 [입력 정보]의 "현재 청크 번호"·"청크 범위"를 그대로 사용하라):
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
      "transcript": "이 구간 핵심 발화 (없으면 \\"\\")",
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
      "scene_location": "장면 배경 장소",
      "timeline_position": "현재|과거|불명",
      "continues_from": null,
      // 또는 이전 후보를 참조하는 경우:
      // {{"chunk_index": <참조 청크 번호 — 같은 청크면 현재 청크 번호, 이전 청크면 [이전 청크들의 분석 결과]에 명시된 chunk_index>, "candidate_index": <참조 후보의 candidate_index>}}
      "requires_context": false,
      "highlight_eligible": false,
      "highlight_reason": null,
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
        "action": "행동 동사구 (예: '통화로 업무 위임', '맥주 권유', '병문안')",
        "target": "대상 캐릭터명/장소/물건 또는 null",
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
- **title_line1** (위·흰색, 13자 권장, 최대 15자): **상황/배경/도입 설명**.
  인물 자체보다 *무슨 일이 일어났는지* 또는 *어떤 상황인지*를 압축.
- **title_line2** (아래·노란색 강조, 13자 권장, 최대 15자): **캐릭터·반전·핵심 후킹**.
  시청자 시선을 잡는 핵심 문구. 강조 자리.

글자 수 가이드: 13자 이내 가독성 최고, 14~15자 허용(폰트 자동 축소), 16자 이상은 후처리에서 절단되니 가능한 한 15자 이하 권장.

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

## 출력 형식 (필수)

응답은 반드시 **JSON 객체 1개**여야 하며, 최상위 키 `storylines` (배열) 가 **반드시 포함**되어야 한다.
다른 키 이름(예: shorts, proposals, options 등) 사용 금지. 마크다운 ```json 펜스는 허용되나, 텍스트 설명은 금지.

각 storyline의 `narrative_plan`을 해당 storyline의 클립 선택 전에 먼저 작성하라.
점수나 수치가 아니라 "어떤 장면/이야기를 쇼츠로 만들 것인가"를 먼저 결정한 뒤 클립을 찾아라.
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
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/배경 설명 (13자 이내, 초과 금지)",
      "title_line2": "캐릭터/사건 중심 후킹 (13자 이내, 초과 금지)",
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
      }}
    }},
    {{
      "storyline_index": 1,
      "shorts_type": "storytelling",
      "sequence_type": "시퀀스블록형",
      "topic": "시퀀스블록형 예: 한 콩트 통째 사용",
      "topic_reason": "같은 sequence_id의 candidate가 자체 완결",
      "score": 0.0,
      "title_line1": "상황 설명",
      "title_line2": "후킹 강조",
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
      }}
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
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/배경 설명 (13자 이내, 초과 금지)",
      "title_line2": "캐릭터/사건 중심 후킹 (13자 이내, 초과 금지)",
      "use_original_audio": true
    }}
  ],
  "selected_storyline_index": 0,
  "shorts_type":"storytelling"|"highlight",
  "selection_reason": "이 스토리라인을 선택한 이유",
  "title_line1": "최종 제목 1줄 (맥락)",
  "title_line2": "최종 제목 2줄 (후킹)",
  "title_txt": "title_line1 + title_line2 합친 전체 제목",
  "selected_storyline": {{ "선정된 인덱스의 객체를 그대로 복사해서 출력": "" }}
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
각 클립에는 두 가지 텍스트 정보가 있다:
- description: LLM의 시각 분석 추정 (인물·동작 추정 — 부정확할 수 있음)
- transcript: Whisper로 전사한 **실제 음성 대사** (정확한 사실)

⚠️ **transcript 우선 원칙 (라운드 16)**: 내레이션을 쓸 때는 transcript를 1순위 사실로 신뢰하라.
description과 transcript가 어긋나면 transcript를 따른다.
- 예: description="유미가 주호에게 전화" / transcript="피디님, 저예요" → 내레이션은
  "유미가 PD에게 전화했다"는 사실을 반영해야 한다 ("주호에게 보낸 문자" 같은 LLM 추정 금지).
- 예: description="문자를 보낸다" / transcript="여보세요? 작가님" → 통화 사실을 반영
  ("보낸 설레는 문자" ❌ → "건넨 한 통의 전화" / "밤늦게 걸려온 그의 한 마디" ✅).

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
    analysis_temperature: float = 0.5   # analyze_chunk / analyze_video_intent
    story_temperature: float = 1.0      # compose_story_with_context
    research_temperature: float = 0.3   # work_researcher (Google Search grounding)


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
                            temperature=self.config.analysis_temperature,
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
                    time.sleep(2 ** attempt)
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
                            temperature=self.config.analysis_temperature,
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
                        temperature=0.2,
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
                        temperature=self.config.story_temperature,
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
            clips_lines.append(
                f"- clip {i} (role={c.role}): "
                f"edit_timeline {edit_start:.1f}~{edit_end:.1f}s "
                f"(원본 {c.start_sec:.1f}~{c.end_sec:.1f}s), "
                f"chars={chars}\n"
                f"    description={subtitle[:120]!r}\n"
                f"    transcript={transcript_repr!r}"
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

        prompt = TTS_PLANNING_PROMPT.format(
            work_title=work_title,
            total_duration=total_duration,
            clips_str=clips_str,
            work_context_block=work_context_block,
            episodes_context_block=episodes_context_block,
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
                        temperature=0.7,
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
                        temperature=0.4,
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
        "title_line1": work_title[:15],
        "title_line2": best.get("description", "")[:15],
        "ending_hook": "",
        "storyline": storyline_obj,
    }

    return {
        "storylines": [fallback_storyline],
        "selected_storyline_index": 0,
        "selection_reason": "폴백: Gemini 응답 실패 — 상위 moment 자동 조합",
        "title_line1": work_title[:15],
        "title_line2": best.get("description", "")[:15],
        "title_txt": f"{work_title[:15]} {best.get('description', '')[:15]}",
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

    # title_line1/line2 기본값 설정 (하위 호환)
    if "title_line1" not in data:
        title = data.get("title_txt", "")
        data["title_line1"] = title[:15] if len(title) > 15 else title
        data["title_line2"] = title[15:30] if len(title) > 15 else ""
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
    analysis_temperature = float(os.getenv("GEMINI_ANALYSIS_TEMPERATURE", "0.4"))
    story_temperature = float(os.getenv("GEMINI_STORY_TEMPERATURE", "1.0"))
    research_temperature = float(os.getenv("GEMINI_RESEARCH_TEMPERATURE", "0.7"))
    return GeminiClient(GeminiConfig(
        api_key=api_key,
        model_name=model_name,
        flash_model_name=flash_model_name,
        max_retries=max_retries,
        analysis_temperature=analysis_temperature,
        story_temperature=story_temperature,
        research_temperature=research_temperature,
    ))


def _validate_gemini_schema(data: dict[str, Any]) -> None:
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

    for moment in data["candidate_moments"]:
        for key in ["start_sec", "end_sec", "description", "reason", "transcript"]:
            if key not in moment:
                raise ValueError(f"Missing key {key} in candidate moment")
