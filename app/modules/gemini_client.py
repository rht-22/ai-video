from __future__ import annotations

import json
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import re


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
# 청크 분석 프롬프트 (바이럴 최적화 버전)
# ─────────────────────────────────────────────
GEMINI_PROMPT_TEMPLATE = """
너는 유튜브 쇼츠 영상 분석 및 편집 전문가다.
반드시 JSON만 출력한다. 코드블록 금지.
영상에 없는 내용은 절대 창작하지 말 것.

[입력 정보]
- 작품명: {work_title}
- 주제: {topic}
- 청크 범위: {chunk_start_sec} ~ {chunk_end_sec} 초
{work_context_block}
{previous_episodes_context_block}
- ⚠️ 모든 start_sec / end_sec는 반드시 첨부된 영상 파일의 시작(0초)을 기준으로 한 상대값으로 반환할 것

- 자막(있으면): {transcript_text}
- 씬 경계(있으면): {scene_boundaries}
- 자막/대사 참고 (화자명 포함): {transcript_hint}
{previous_context}

[핵심 장면 선별 원칙 — ⚠️ 가장 중요]
단순히 자극적이거나 감정적인 장면이 아니라, **이 영상에서 시청자가 꼭 봐야 할 핵심 장면**을 찾아라.
- 이 에피소드의 스토리를 이해하는 데 필수적인 장면
- 캐릭터의 성격/매력이 가장 잘 드러나는 장면
- 시청자가 "이 드라마/예능 재밌다"고 느끼게 만드는 결정적 장면
- 반전, 갈등의 정점, 감정의 클라이맥스, 핵심 대사가 있는 장면
- 맥락 없이도 "뭔데 이거?" 하고 궁금해지는 장면

⚠️ 다음은 피하라:
- 단순 이동/대기/배경 장면 (서사적으로 무의미)
- 감정은 강하지만 맥락이 없으면 이해 불가능한 장면
- 엔딩 크레딧, 타이틀 시퀀스, 예고편

[바이럴 콘텐츠 분석 기준]
다음 기준으로 각 장면의 바이럴 잠재력을 평가하라:

1. **scroll_stop_power** (0.0~1.0): 스크롤 멈춤 파워
   - 첫 3초 안에 시청자의 스크롤을 멈출 수 있는 힘
   - 충격, 호기심, 감정 폭발, 예상치 못한 상황 등

2. **emotional_intensity** (0.0~1.0): 감정 강도
   - 웃음, 눈물, 분노, 소름 등 강한 감정 반응 유발 정도

3. **rewatch_value** (0.0~1.0): 재시청 가치
   - 반복 시청하고 싶은 정도 (반전, 디테일, 중독성)

4. **shareability** (0.0~1.0): 공유 가능성
   - "이거 봐봐" 하고 남에게 공유하고 싶은 정도

5. **standalone_value** (0.0~1.0): 맥락 독립성
   - 전체 작품을 모르는 사람도 재미를 느낄 수 있는 정도

6. **narrative_core_score** (0.0~1.0): 서사적 핵심도 — ⚠️ 가장 중요한 지표
   - 이 에피소드의 핵심 스토리/갈등/반전을 담고 있는 정도
   - 이 장면이 빠지면 에피소드의 재미가 크게 줄어드는가?
   - 캐릭터의 매력/결정적 행동이 드러나는가?
   - 0.8 이상: 이 에피소드의 "명장면" — 반드시 쇼츠에 포함해야 할 장면
   - 0.5 이하: 있으면 좋지만 없어도 되는 부가적 장면

[points 평가 기준 — 해당 항목만 0.0~1.0 점수, 해당 없으면 null]
- humor: 웃긴 정도 (리액션, 상황 코미디, 말실수 등)
- love: 설렘/달달함 (스킨십, 고백, 케미 등)
- relatability: 공감도 (시청자가 "나도 저래" 할 만한 장면)
- saida: 시원함 (속이 뻥 뚫리는 통쾌한 장면)
- goguma: 답답함 (시청자가 답답해하며 댓글 달 만한 장면)
- conflict_twist: 갈등/반전 (예상 못한 전개, 충격적 반전)

[인물 식별 원칙 — 복장/헤어스타일 변화에 강건하게]
- 인물 동일성 판단 시 **절대 복장이나 헤어스타일에 의존하지 마라** (씬마다 바뀔 수 있음)
- 대신 다음 우선순위로 식별하라:
  1. 자막의 화자명 (가장 신뢰도 높음)
  2. 얼굴 특징 (얼굴형, 연령대, 성별, 쌍꺼풀 유무 등)
  3. 체형/키/목소리 톤
  4. 극 중 역할과 다른 인물과의 관계
- 이름을 알 수 없는 인물은 **얼굴/체형 기반**으로 레이블링 (예: "30대 각진 턱 남성", "둥근 얼굴 여성")
  ❌ "갈색 재킷 남자" (복장 기반 — 금지)
- 동일 인물이 다른 씬에 다른 옷으로 등장해도 같은 이름/레이블을 유지

[규칙]
- 아래 JSON 스키마 구조를 100% 동일하게 유지하여 출력
- 분석 결과 해당 항목이 없을 경우 빈 배열 대신 null로 출력
- candidate_moments 최소 {min_candidates}개 (이 청크에서 주목할 만한 모든 장면을 빠짐없이 포함)
- 타이틀 시퀀스, 엔딩 크레딧은 제외하기
- description은 실제 장면 묘사를 유지하고, 앞 장면이 나중 내용에 의해 재해석되는 경우 재해석된 의미는 reason 필드에만 반영할 것
- ⚠️ **영상에 실제로 존재하는 장면만** 후보에 포함하라. 추론이나 상상으로 장면을 만들어내지 마라.
- 각 candidate_moment의 start_sec~end_sec 구간은 반드시 첨부 영상에서 실제 확인 가능해야 한다.
- 영상 전체를 빠짐없이 스캔하라. 앞부분에만 집중하지 말고 중반~후반도 균등하게 분석할 것.
- 확실하지 않은 내용은 넣지 마라
- 완결되지 않은 부분(엔딩)은 이후 전개에 대한 궁금증을 유발하는 장면일 수 있으므로 확정된 전개로 장담하지 말 것
- 각 moment에 바이럴 제목 후보 2개 포함 (30자 이내, 이모지 포함)
- 각 moment의 story_role을 반드시 지정: "hook" | "build" | "payoff"
- hook으로 적합한 moment에는 hook_description 필드 추가 (0-3초 후킹 전략)

{{
  "chunk_index": 0,
  "chunk_start_sec": 0,
  "chunk_end_sec": 300,
  "summary": "해당 청크 전체의 핵심 내용 요약",
  "main_plot": "영상의 핵심 서사 80자 이내",
  "characters_relations": "인물 간 관계 및 감정 역학",
  "characters_tracking": [
    {{
      "character": "인물명 (자막 화자명 우선, 없으면 얼굴 기반 레이블)",
      "identity_anchor": "이 인물을 다른 씬에서도 알아볼 수 있는 불변 특징 (얼굴형, 연령대, 성별, 체형, 목소리)",
      "appearances": [
        {{"start_sec": 0.0, "end_sec": 32.0, "action": "행동 요약", "current_look": "이 씬에서의 복장/헤어 (참고용)"}}
      ]
    }}
  ],
  "emotion_curve": [{{"start_sec": 0.0, "end_sec": 10.0, "emotion": "감정", "intensity": 0.8}}],
  "tension_score": {{"average": 0.6, "peak": 0.95, "peak_start_sec": 0.0, "peak_end_sec": 10.0}},
  "overall_vibe": "영상 전체 분위기 요약",
  "candidate_moments": [
    {{
      "chunk_index": 0,
      "candidate_index": 0,
      "start_sec": 12.4,
      "end_sec": 25.8,
      "importance": 0.0,
      "hook_score": 0.0,
      "topic_alignment_score": 0.0,
      "scroll_stop_power": 0.0,
      "emotional_intensity": 0.0,
      "rewatch_value": 0.0,
      "shareability": 0.0,
      "standalone_value": 0.0,
      "narrative_core_score": 0.0,
      "story_role": "hook|build|payoff",
      "characters_in_scene": ["인물명1", "인물명2"],
      "description": "장면 설명(묘사 위주)",
      "reason": "선정 이유",
      "hook_description": "이 장면을 hook으로 사용할 경우 0-3초 전략 (hook role일 때만, 아니면 null)",
      "transcript": "start_sec~end_sec 구간에서 가장 핵심이 되는 '단 한 명'의 주요 대사",
      "viral_titles": ["제목 후보 1 🔥", "제목 후보 2 😱"],
      "suggested_tts_line": "이 장면에 어울리는 나레이션 TTS 한 줄",
      "pacing_note": "빠른 컷|느린 텐션|점진적 고조",
      "points": {{
        "humor": null,
        "love": null,
        "relatability": null,
        "saida": null,
        "goguma": null,
        "conflict_twist": null
      }}
    }}
  ]
}}
"""

# ─────────────────────────────────────────────
# 스토리 구성 프롬프트 (레퍼런스 기반 바이럴 최적화)
# ─────────────────────────────────────────────
STORY_COMPOSITION_PROMPT = """
# Role
너는 유튜브 쇼츠 100만 조회수 전문 편집자다.

# Task
제공된 영상 분석 데이터를 기반으로 **반드시 서사형(storytelling) 스토리라인** 3개를 구성하라.
3개 모두 독립적으로 완성도 높은 쇼츠가 될 수 있어야 한다.
그중 가장 바이럴 성공 가능성이 높은 하나를 최종 선정하되, 나머지 2개도 사용될 수 있다.

⚠️ **3개 스토리라인 독립성 원칙:**
- 3개 스토리라인은 **서로 다른 장면을 사용**해야 한다 (동일 씬 중복 사용 금지)
- 각 스토리라인마다 **독립적인 서사 아크, 제목(title_line1+title_line2), TTS 나레이션**을 갖추어야 한다
- score는 바이럴 가능성을 0.0~1.0으로 **정직하게** 평가하라 (모두 비슷한 점수 금지)

# 핵심 원칙: 멀티클립 편집

⚠️ **단일 연속 클립은 절대 금지.** 반드시 3개 이상의 서로 다른 씬을 조합하여 서사 아크를 구성하라.
실제 고조회수 쇼츠 레퍼런스를 분석한 결과:
- 조회수 100만+ 쇼츠는 예외 없이 3~7개의 서로 다른 씬을 편집하여 하나의 서사를 만듬
- 각 씬 사이에 TTS 나레이션이 맥락을 연결함
- 캐릭터 중심의 서사 아크(hook→build→payoff)가 필수

# 바이럴 쇼츠 필수 구조

## Hook (0-5초): 스크롤 멈춤
- 충격적 반전, 감정 폭발, 의외의 상황으로 시작
- scroll_stop_power 0.7 이상인 moment 우선 선택
- TTS로 궁금증 유발: "그런데 이때 예상치 못한 일이 벌어지는데..."

## Build (5-45초): 몰입 유지 (최소 2개 씬)
- 감정 강도가 점진적으로 상승
- 씬 전환마다 TTS 나레이션이 맥락을 이어줌
- 핵심 대사가 포함된 씬 우선 선택

## Payoff (45-60초): 감정 폭발 + 엔딩
- 클라이맥스 또는 예상치 못한 반전
- 여운이 남거나 다음 편 궁금증 유도

# 제목 구조 (2줄 필수)

레퍼런스 분석 결과 고조회수 쇼츠 제목은 반드시 2줄 구조:
- **title_line1**: 상황/맥락 설명 (15자 이내, 흰색으로 표시됨)
- **title_line2**: 캐릭터 중심 후킹 문구 (15자 이내, 노란색 강조로 표시됨)

예시:
- "되찾은 구역과 신뢰" / "기분 째지는 태진이"
- "멍청한 아줌마 연기" / "천재가 들통나는 순간"
- "위기를 기회로 삼아" / "회장 눈에 든 희로"

⚠️ 이모지 금지. 캐릭터명(인물명) 반드시 포함.

# TTS 나레이션 규칙

각 클립(hook, build 각 항목, payoff)마다 반드시 tts_line을 작성:
- 해설 톤으로 상황을 요약하고 다음 씬으로 전환 유도
- 예: "그런데 이때", "바로 그 순간", "하지만 반전이 있었으니"
- 각 TTS는 1-2문장, 자연스럽게 이전 씬에서 다음 씬으로 연결
- payoff의 마지막에는 궁금증을 유발하는 엔딩 훅 가능

# Duration Constraint
- 총 클립 길이 합계: {min_duration_sec}초 ~ {max_duration_sec}초 범위
- 각 클립의 start_sec, end_sec는 반드시 원본 영상 타임라인 기준
- 각 장면의 (end_sec - start_sec)를 합산하여 범위 내인지 반드시 확인할 것

# Input Data
- 작품명: {work_title}
- 주제: {topic}
{work_context_block}
- 후보 장면 및 분석 데이터:
{candidates_str}

# 인물 일관성 원칙 (⚠️ 매우 중요)

서로 다른 인물을 같은 캐릭터로 혼동하지 마라.
인물의 복장이나 헤어스타일은 씬마다 바뀔 수 있으므로, 인물 동일성은 반드시 다음으로 판단:
1. characters_tracking의 character 이름 (자막 화자명 기반)
2. identity_anchor (얼굴, 체형, 연령대 등 불변 특징)

검증 체크리스트:
- 스토리라인의 주인공이 모든 클립에 실제로 등장하는가? (characters_in_scene 확인)
- 서로 다른 identity_anchor를 가진 인물을 같은 캐릭터로 취급하고 있지 않은가?
- 복장만 다르고 같은 인물인 경우: identity_anchor가 일치하면 동일 인물로 처리

# 핵심 장면 우선 원칙 (⚠️ 매우 중요)
- **narrative_core_score가 0.8 이상인 후보 장면을 반드시 우선 사용하라**
- 이 에피소드의 "명장면"을 빠뜨리고 부차적인 장면만으로 쇼츠를 구성하지 마라
- 시청자가 이 쇼츠를 보고 "이 드라마/예능 재밌겠다, 본편 보고 싶다"고 느껴야 한다
- 핵심 갈등, 반전, 캐릭터의 결정적 행동/대사가 포함된 장면을 중심으로 구성

# Constraints & Rules
1. **storytelling만 생성** (highlight 타입 금지)
2. 각 storyline은 반드시 3개 이상의 서로 다른 씬(hook 1개 + build 1개 이상 + payoff 1개)으로 구성
3. hook이 전개상 앞부분이면 "여정몰입형", 전개상 뒷부분이면 "결과선공개형"
4. 각 클립의 start_sec, end_sec 명시
5. 모든 클립에 tts_line 필수
6. ending_hook: 영상 마지막에 시청자 궁금증을 유발하는 질문 1줄
7. 작품의 전체 맥락을 모르는 사람도 한 번 보고 재미를 느낄 수 있는 장면을 선정
8. score 평가 시 narrative_core_score가 높은 장면을 많이 포함한 스토리라인에 높은 점수를 부여

다음 JSON 스키마로만 응답:
{{
  "storylines": [
    {{
      "storyline_index": 0,
      "shorts_type": "storytelling",
      "sequence_type": "여정몰입형|결과선공개형",
      "topic": "주제명",
      "topic_reason": "서사 구성 이유",
      "score": 0.0,
      "estimated_duration_sec": 0.0,
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/맥락 설명 (15자 이내)",
      "title_line2": "캐릭터 중심 후킹 (15자 이내)",
      "ending_hook": "과연 그의 선택은 옳았을까?",
      "storyline": {{
        "hook": {{
          "chunk_index": 0, "candidate_index": 0,
          "start_sec": 0.0, "end_sec": 0.0,
          "description": "장면 설명",
          "tts_line": "궁금증 유발 나레이션 1-2문장",
          "use_original_audio": true
        }},
        "build": [
          {{
            "chunk_index": 0, "candidate_index": 0,
            "start_sec": 0.0, "end_sec": 0.0,
            "description": "장면 설명",
            "tts_line": "맥락 연결 나레이션 1-2문장",
            "use_original_audio": true
          }}
        ],
        "payoff": {{
          "chunk_index": 0, "candidate_index": 0,
          "start_sec": 0.0, "end_sec": 0.0,
          "description": "장면 설명",
          "tts_line": "클라이맥스 나레이션",
          "use_original_audio": true
        }}
      }}
    }}
  ],
  "selected_storyline_index": 0,
  "selection_reason": "이 스토리라인을 선택한 이유",
  "title_line1": "최종 제목 1줄 (맥락)",
  "title_line2": "최종 제목 2줄 (후킹)",
  "title_txt": "title_line1 + title_line2 합친 전체 제목",
  "ending_hook": "시청자 궁금증 유발 질문",
  "selected_storyline": {{ "선정된 인덱스의 객체를 그대로 복사해서 출력": "" }}
}}
"""


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model_name: str = "gemini-1.5-pro"
    max_retries: int = 3


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
            for idx, prev in enumerate(prev_analyses, 1):
                context_parts.append(f"\n[이전 청크 {idx} 분석 결과]")
                if prev.get("summary"):
                    context_parts.append(f"요약: {prev['summary']}")
                if prev.get("candidate_moments"):
                    moments_text = "\n".join([
                        f"  - {m.get('start_sec', 0)}~{m.get('end_sec', 0)}초: {m.get('description', '')}"
                        for m in prev["candidate_moments"][:3]
                    ])
                    context_parts.append(f"주요 모멘트:\n{moments_text}")
            if context_parts:
                previous_context = "\n\n이전 청크들의 분석 결과 (전체 흐름 이해용):" + "\n".join(context_parts)

        chunk_start = payload["chunk_start_sec"]
        chunk_end = payload["chunk_end_sec"]

        # 자막 텍스트 (origin/test: transcript_text)
        raw_segments = payload.get("transcript_text") or []
        if raw_segments:
            filtered = [
                s for s in raw_segments
                if hasattr(s, "start_sec") and s.start_sec >= chunk_start and s.end_sec <= chunk_end
            ]
            transcript_text_str = "\n".join(
                f"[{s.start_sec:.1f}~{s.end_sec:.1f}] {s.text}" for s in filtered
            ) or "없음"
        else:
            transcript_text_str = "없음"

        # 자막 텍스트(화자명 포함)를 Gemini에 전달
        transcript_hint = "없음"
        if payload.get("transcript_segments"):
            segs = payload["transcript_segments"]
            lines = [f"  {s.start_sec:.0f}~{s.end_sec:.0f}초: {s.text}" for s in segs[:50]]
            transcript_hint = "\n".join(lines)

        # 작품 컨텍스트 (시놉시스/장르/핵심 요소)
        work_context_block = ""
        if payload.get("work_context"):
            work_context_block = (
                f"\n[작품 정보 — 핵심 장면 판단의 근거]\n{payload['work_context']}\n"
                "⚠️ 위 정보를 바탕으로 이 에피소드에서 가장 핵심적인 장면을 정확히 식별하라.\n"
                "작품의 장르, 톤, 핵심 갈등을 이해하고, 그에 맞는 명장면을 선별하라.\n"
            )

        # 청크 길이에 비례한 최소 후보 수 (1분당 1개, 최소 5개)
        chunk_duration = chunk_end - chunk_start
        min_candidates = max(5, int(chunk_duration / 60))

        prompt = GEMINI_PROMPT_TEMPLATE.format(
            work_title=payload["work_title"],
            topic=payload["topic"],
            chunk_start_sec=chunk_start,
            chunk_end_sec=chunk_end,
            transcript_text=transcript_text_str,
            scene_boundaries=payload.get("scene_boundaries") or "없음",
            transcript_hint=transcript_hint,
            previous_context=previous_context,
            previous_episodes_context_block=previous_episodes_context_block,
            work_context_block=work_context_block,
            min_candidates=min_candidates,
        )

        video_path = payload.get("video_path")
        content_parts = [prompt]
        uploaded_file = None

        if video_path:
            video_path_obj = Path(video_path) if isinstance(video_path, str) else video_path
            if video_path_obj.exists():
                for upload_attempt in range(self.config.max_retries):
                    try:
                        uploaded_file = self.client.files.upload(file=str(video_path_obj))
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

        try:
            for attempt in range(self.config.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=self.config.model_name,
                        contents=content_parts,
                        config=self.types.GenerateContentConfig(
                            response_mime_type="application/json",
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
    ) -> dict[str, Any]:
        """후보 장면들로 바이럴 최적화 스토리라인을 구성합니다.

        하이라이트형 3개 + 서사형 3개를 생성하고 최적 1개를 선정합니다.
        JSON 강제 응답 + 구조 검증 + 3회 재시도 + 폴백을 적용합니다.
        """
        candidates_str = ""
        for i, m in enumerate(all_candidates):
            m_copy = dict(m)
            m_copy["candidate_index"] = i
            candidates_str += f"- {json.dumps(m_copy, ensure_ascii=False)}\n"

        work_context_block = ""
        if work_context:
            work_context_block = (
                f"\n[작품 정보]\n{work_context}\n"
                "⚠️ 이 정보를 바탕으로 작품의 핵심 서사와 매력을 잘 살리는 스토리라인을 구성하라.\n"
            )

        prompt = STORY_COMPOSITION_PROMPT.format(
            work_title=work_title,
            topic=topic,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            candidates_str=candidates_str,
            work_context_block=work_context_block,
        )

        for attempt in range(self.config.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.config.model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
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

                result["selected_storyline"] = storylines[selected_idx]
                # 전체 storylines를 score 순으로 정렬하여 반환 (멀티쇼츠용)
                result["ranked_storylines"] = sorted(
                    storylines, key=lambda s: s.get("score", 0), reverse=True
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


def _build_fallback_story(all_candidates: list, work_title: str) -> dict[str, Any]:
    """Gemini 스토리 구성 실패 시 상위 3-4개 moment를 조합하여 서사형 폴백 생성."""
    if not all_candidates:
        raise RuntimeError("후보 장면이 없어 폴백도 불가능합니다.")

    # final_score 기준 정렬
    sorted_candidates = sorted(
        all_candidates,
        key=lambda m: m.get("final_score", m.get("importance", 0)),
        reverse=True,
    )

    # 상위 4개 moment 선택 (최소 3개)
    top_moments = sorted_candidates[:min(4, len(sorted_candidates))]

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
            "tts_line": m.get("suggested_tts_line", ""),
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
            "tts_line": hook_m.get("suggested_tts_line", ""),
            "use_original_audio": True,
        },
        "build": build_clips,
        "payoff": {
            "chunk_index": payoff_m.get("chunk_index", 0),
            "candidate_index": payoff_m.get("candidate_index", 0),
            "start_sec": payoff_m["start_sec"],
            "end_sec": payoff_m["end_sec"],
            "description": payoff_m.get("description", ""),
            "tts_line": payoff_m.get("suggested_tts_line", ""),
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
        "score": best.get("final_score", best.get("importance", 0)),
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
    """스토리 구성 응답의 필수 구조를 검증합니다."""
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

    for idx, sl in enumerate(data["storylines"]):
        if "shorts_type" not in sl:
            raise ValueError(f"storyline[{idx}]에 'shorts_type'이 없습니다.")
        if "score" not in sl:
            raise ValueError(f"storyline[{idx}]에 'score'가 없습니다.")

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
    model_name = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-pro-preview-06-05")
    max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
    return GeminiClient(GeminiConfig(api_key=api_key, model_name=model_name, max_retries=max_retries))


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
        # 기존 필수 필드
        for key in [
            "start_sec", "end_sec", "importance", "hook_score",
            "topic_alignment_score", "description", "reason", "transcript",
        ]:
            if key not in moment:
                raise ValueError(f"Missing key {key} in candidate moment")

        # 바이럴 필드 — 없으면 기본값 적용 (하위 호환)
        moment.setdefault("scroll_stop_power", 0.5)
        moment.setdefault("emotional_intensity", 0.5)
        moment.setdefault("rewatch_value", 0.5)
        moment.setdefault("shareability", 0.5)
        moment.setdefault("standalone_value", 0.5)
        moment.setdefault("narrative_core_score", 0.5)
        moment.setdefault("viral_titles", [])
        moment.setdefault("suggested_tts_line", "")
        moment.setdefault("pacing_note", "")
        moment.setdefault("points", {})

        # story_role 기본값
        if moment.get("story_role") not in ("hook", "build", "payoff"):
            moment["story_role"] = "build"
