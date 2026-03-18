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
    """마크다운 코드 블록에서 JSON을 추출합니다.
    
    Args:
        text: 마크다운 코드 블록으로 감싸진 JSON 문자열
    
    Returns:
        순수 JSON 문자열
    """
    # ```json ... ``` 또는 ``` ... ``` 패턴 제거
    text = text.strip()
    
    # ```json으로 시작하고 ```로 끝나는 경우
    if text.startswith("```"):
        # 첫 번째 ``` 제거
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        # 마지막 ``` 제거
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    
    return text.strip()


GEMINI_PROMPT_TEMPLATE = """
[ROLE]
당신은 한국 최고의 숏폼 전문 AI 에디터이자 멀티모달 스토리 분석 전문가다.
텍스트, 영상, 오디오를 통합적으로 이해하여
바이럴 가능성이 높은 쇼츠 콘텐츠를 기획하고 분석한다.
반드시 JSON만 출력한다. 코드블록 금지.
영상에 없는 내용은 절대 창작하지 말 것.

---

입력 정보:

- 작품명: {work_title}
- 주제: {topic}
- 청크 범위: {chunk_start_sec} ~ {chunk_end_sec} 초
- 요약(있으면): {transcript_summary}
- 씬 경계(있으면): {scene_boundaries}
- 전체 줄거리(있으면): {full_summary}
- 스토리라인(있으면): {storyline} {previous_context}

---

[인물 식별 단계]

- 분석 시작 전, 아래 수단을 통해 등장 인물의 이름을 먼저 파악한다:
    - 화면 자막 또는 이름 자막 (예능/드라마 자막 포함)
    - 대사 내 호칭 (예: "야 민준아~")
    - 화면 내 텍스트 (명찰, 이름표 등 OCR 가능한 텍스트)
- 위 수단으로 이름 확인이 불가능한 인물은 "인물A", "인물B" 등 고유 레이블을 부여한다.
- 이후 모든 분석 항목에서 확정된 이름 또는 레이블을 일관되게 사용한다.

---

[멀티모달 분석 필수 출력 항목]

아래 항목을 모두 포함하여 구조화된 형태로 출력하라.

- main_plot: 영상의 핵심 서사를 80자 이내 한국어 요약
- characters_relations: 인물 간 관계 및 권력/감정 역학 설명
- characters_tracking: 인물별 등장 타임스탬프 및 주요 행동/발화 요약
    - 인물명 또는 레이블
    - appearances:
        - start_sec / end_sec: 등장 구간
        - action: 해당 구간 행동/발화 요약
- sub_plots: 영상 내 서브 플롯 목록 (타임스탬프 포함)
- emotion_curve: 타임스탬프별 감정 변화 리스트 (emotion, intensity 0~1)
- tension_score: 평균 긴장도 / 최고 긴장도 / 최고점 타임스탬프
- conflicts_and_twists: 갈등 및 반전 발생 지점과 내용
- humor_points: 유머 발생 지점 및 웃음 강도
- love_points: 인물 간 명확한 플러팅 대사 또는 감정 교환이 발생하는 지점 및 강도
- relatability_points: 많은 시청자가 공감할 수 있는 지점 및 강도
- saida_points: 사이다 전개로 대리만족이 발생하는 포인트 및 강도
- goguma_points: 답답함이나 분노를 유발하는 포인트 및 강도
- audio_tempo: BPM 추정치 / 분위기 변화 키워드
- overall_vibe: 영상 전체 분위기 요약

---

[SHORTS TYPE DEFINITION]

아래 두 가지 쇼츠 유형을 모두 고려하여 candidate_moments를 생성하라.

[유형 1: 하이라이트 쇼츠]

- hook_score 기준:
    - 0.8~1.0: 결과 선공개형 훅으로 적합
    - 0.4~0.7: 여정 몰입형 초반 빌드에 적합
    - 0.0~0.3: 훅보다는 중후반 맥락용 장면
- 하나의 강렬한 장면 중심, 맥락 설명 최소화
- story_role: hook / build / payoff

[유형 2: 서사형 쇼츠]

- 가장 드라마틱한 씬을 중심으로 서사가 자연스럽게 흐르도록 구성
- story_role: hook / build / payoff

---

[TONE & RULES]

- 모든 출력은 한국어 사용
- 최신 쇼츠 트렌드 반영: 자연스러운 톤, 짧은 문장, 강조/리액션 요소
- candidate_moments 최소 10개
- JSON 스키마 강제
- 분석 결과 해당 항목이 없을 경우 빈 배열 대신 null로 출력한다.

다음 스키마로만 응답:
{{
  "chunk_index": 0,
  "chunk_start_sec": 0,
  "chunk_end_sec": 300,
  "summary": "요약",
  "main_plot": "영상의 핵심 서사 80자 이내",
  "characters_relations": "인물 간 관계 및 권력/감정 역학 설명",
  "characters_tracking": [
    {{
      "character": "인물명 또는 레이블",
      "appearances": [
        {{
          "start_sec": 0.0,
          "end_sec": 32.0,
          "action": "해당 구간 행동/발화 요약"
        }}
      ]
    }}
  ],
  "sub_plots": [
    {{
      "start_sec": 0.0,
      "description": "서브플롯 설명"
    }}
  ],
  "emotion_curve": [
    {{
      "start_sec": 0.0,
      "end_sec": 10.0,
      "emotion": "감정",
      "intensity": 0.8
    }}
  ],
  "tension_score": {{
    "average": 0.6,
    "peak": 0.95,
    "peak_start_sec": 0.0,
    "peak_end_sec": 10.0
  }},
  "audio_tempo": {{
    "bpm": 120,
    "vibe_keywords": ["키워드1", "키워드2"]
  }},
  "overall_vibe": "영상 전체 분위기 요약",
  "candidate_moments": [
    {{
      "candidate_index": 0,
      "start_sec": 12.4,
      "end_sec": 25.8,
      "importance": 0.0,
      "hook_score": 0.0,
      "topic_alignment_score": 0.0,
      "story_role": "hook|build|payoff",
      "reason": "선정 이유",
      "subtitle": "자막(짧게)",
      "tts_line": "TTS 한 문장",
      "points": {{
        "humor": {{"description": "유머 내용", "intensity": 0.7}},
        "love": null,
        "relatability": null,
        "saida": {{"description": "사이다 포인트", "intensity": 0.9}},
        "goguma": null,
        "conflict_twist": null
      }}
    }}
  ],
  "title_candidates": ["제목1", "제목2", "제목3"]
}}
""".strip()

FULL_VIDEO_ANALYSIS_PROMPT = """
너는 영상 분석 전문가다. 주어진 영상과 전사 결과를 바탕으로 전체 줄거리와 주요 장면을 분석해라.

입력 정보:
- 작품명: {work_title}
- 주제: {topic}
- 영상 길이: {duration_sec}초
- 전사 결과: {transcript}

요구 사항:
1. 전체 줄거리를 요약해라 (3-5문단)
2. 주요 장면들을 시간대와 함께 나열해라 (최소 10개)
3. 감정/분위기 변화를 분석해라
4. 핵심 인물과 사건을 파악해라

다음 JSON 스키마로만 응답:
{{
  "summary": "전체 줄거리 요약 (3-5문단)",
  "key_scenes": [
    {{
      "start_sec": 123.4,
      "end_sec": 145.6,
      "description": "장면 설명",
      "importance": 0.9,
      "emotion": "감정/분위기"
    }}
  ],
  "emotion_arc": "감정 변화 흐름 설명",
  "key_characters": ["인물1", "인물2"],
  "key_events": ["사건1", "사건2"]
}}
""".strip()

STORYLINE_GENERATION_PROMPT = """
너는 쇼츠 영상 편집 전문가다. 전체 영상 분석 결과를 바탕으로 여러 개의 쇼츠 스토리라인을 생성해라.

입력 정보:
- 작품명: {work_title}
- 전체 줄거리: {full_summary}
- 주요 장면: {key_scenes}
- 감정 흐름: {emotion_arc}

요구 사항:
1. 전체 줄거리를 바탕으로 여러 개의 서로 다른 주제를 찾아라 (영상 내용에 따라 3~10개 정도)
2. 각 주제마다 쇼츠 스토리라인을 생성해라:
   - Hook (0-3초): 시청자의 관심을 끄는 장면
   - Build (3-45초): 긴장감을 상승시키는 장면들
   - Payoff (45-60초): 클라이맥스 장면
3. 각 스토리라인에 흥미도 점수(interest_score)를 부여해라 (0.0~1.0)
4. 가장 흥미로운 스토리라인을 자동으로 선택해라

다음 JSON 스키마로만 응답:
{{
  "storylines": [
    {{
      "topic": "주제1",
      "topic_reason": "주제 선택 이유",
      "interest_score": 0.9,
      "storyline": {{
        "hook": {{
          "description": "0-3초: 어필 포인트 설명",
          "recommended_scenes": [123.4, 145.6]
        }},
        "build": {{
          "description": "3-45초: 긴장감 상승 설명",
          "recommended_scenes": [234.5, 345.6, 456.7]
        }},
        "payoff": {{
          "description": "45-60초: 클라이맥스 설명",
          "recommended_scenes": [567.8]
        }}
      }},
      "key_moments": [
        {{
          "time": 123.4,
          "description": "주요 장면 설명",
          "role": "hook|build|payoff"
        }}
      ]
    }},
    {{
      "topic": "주제2",
      "topic_reason": "주제 선택 이유",
      "interest_score": 0.8,
      "storyline": {{...}},
      "key_moments": [...]
    }}
  ],
  "selected_storyline_index": 0
}}
""".strip()


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

    def analyze_chunk(self, payload: dict[str, Any]) -> dict[str, Any]:
        # 이전 분석 결과와 전사를 컨텍스트로 준비
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
                        f"  - {m.get('start_sec', 0)}~{m.get('end_sec', 0)}초: {m.get('subtitle', '')} ({m.get('story_role', 'unknown')})"
                        for m in prev["candidate_moments"][:3]  # 상위 3개만
                    ])
                    context_parts.append(f"주요 모멘트:\n{moments_text}")
            if context_parts:
                previous_context = "\n\n이전 청크들의 분석 결과 (전체 흐름 이해용):" + "\n".join(context_parts)
        
     
        prompt = GEMINI_PROMPT_TEMPLATE.format(
            work_title=payload["work_title"],
            topic=payload["topic"],
            chunk_start_sec=payload["chunk_start_sec"],
            chunk_end_sec=payload["chunk_end_sec"],
            transcript_summary=payload.get("transcript_summary") or "없음",
            scene_boundaries=payload.get("scene_boundaries") or "없음",
            full_summary=payload.get("full_summary") or "없음",
            storyline=payload.get("storyline") or "없음",
            previous_context=previous_context,
        )
        
        # 비디오 파일 경로가 있으면 파일을 직접 읽어서 전달
        video_path = payload.get("video_path")
        content_parts = [prompt]
        uploaded_file = None
        
        # File API 방식 적용 (Memory-Safe)
        if video_path:
            video_path_obj = Path(video_path) if isinstance(video_path, str) else video_path
            if video_path_obj.exists():
                try:
                    uploaded_file = self.client.files.upload(path=str(video_path_obj))
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
                        video_metadata=self.types.VideoMetadata(fps=30),
                    ))
                except Exception as upload_err:
                    print(f" [WARN] 비디오 업로드 중 오류 발생: {upload_err}")


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
    
    def analyze_full_video(
        self,
        video_path: Path,
        transcript: str,
        work_title: str,
        topic: str,
        duration_sec: float,
    ) -> dict[str, Any]:
        """전체 영상의 줄거리와 주요 장면을 분석합니다.
        
        Args:
            video_path: 영상 파일 경로
            transcript: Whisper 전사 결과 (전체 텍스트)
            work_title: 작품명
            topic: 주제
            duration_sec: 영상 길이 (초)
        
        Returns:
            분석 결과 딕셔너리 (summary, key_scenes, emotion_arc 등)
        """
        prompt = FULL_VIDEO_ANALYSIS_PROMPT.format(
            work_title=work_title,
            topic=topic,
            duration_sec=duration_sec,
            transcript=transcript,
        )
        
        # 전체 영상 분석은 전사 결과만 사용 (영상 파일은 너무 크거나 전송 오류가 발생할 수 있음)
        # 전사 결과만으로도 충분히 전체 줄거리와 주요 장면을 분석할 수 있음
        content_parts = [prompt]

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
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError("Gemini API가 빈 응답을 반환했습니다.")
                    continue
                
                text = response.text.strip()
                if not text:
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError("Gemini API 응답이 빈 문자열입니다.")
                    continue
                
                try:
                    # 마크다운 코드 블록 제거
                    json_text = _extract_json_from_markdown(text)
                    data = json.loads(json_text)
                except json.JSONDecodeError as json_err:
                    if attempt == self.config.max_retries - 1:
                        raise ValueError(
                            f"Gemini 응답을 JSON으로 파싱할 수 없습니다.\n"
                            f"에러: {json_err}\n"
                            f"전체 응답:\n{text}"
                        )
                    continue
                
                return data
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    raise RuntimeError(f"전체 영상 분석 실패: {str(e)}") from e
                continue
        raise RuntimeError("전체 영상 분석 실패: 최대 재시도 횟수 초과")
    
    def generate_shorts_storyline(
        self,
        full_summary: str,
        key_scenes: list[dict[str, Any]],
        emotion_arc: str,
        work_title: str,
    ) -> dict[str, Any]:
        """쇼츠 영상의 여러 스토리라인을 생성하고 가장 흥미로운 것을 선택합니다.
        
        Args:
            full_summary: 전체 줄거리 요약
            key_scenes: 주요 장면 리스트
            emotion_arc: 감정 흐름 설명
            work_title: 작품명
        
        Returns:
            스토리라인 딕셔너리 (storylines, selected_storyline_index, selected_storyline 등)
        """
        # key_scenes를 문자열로 변환
        key_scenes_str = "\n".join(
            f"- {scene.get('start_sec', 0)}초~{scene.get('end_sec', 0)}초: {scene.get('description', '')}"
            for scene in key_scenes
        )
        
        prompt = STORYLINE_GENERATION_PROMPT.format(
            work_title=work_title,
            full_summary=full_summary,
            key_scenes=key_scenes_str,
            emotion_arc=emotion_arc,
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
                    continue
                
                text = response.text.strip()
                if not text:
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError("Gemini API 응답이 빈 문자열입니다.")
                    continue
                
                try:
                    # 마크다운 코드 블록 제거
                    json_text = _extract_json_from_markdown(text)
                    data = json.loads(json_text)
                except json.JSONDecodeError as json_err:
                    if attempt == self.config.max_retries - 1:
                        raise ValueError(
                            f"Gemini 응답을 JSON으로 파싱할 수 없습니다.\n"
                            f"에러: {json_err}\n"
                            f"전체 응답:\n{text}"
                        )
                    continue
                
                # 스키마 검증 및 선택 로직
                if "storylines" not in data or not isinstance(data["storylines"], list):
                    raise ValueError("응답에 'storylines' 배열이 없습니다.")
                
                if len(data["storylines"]) == 0:
                    raise ValueError("생성된 스토리라인이 없습니다.")
                
                # selected_storyline_index가 없거나 유효하지 않으면 interest_score로 자동 선택
                selected_idx = data.get("selected_storyline_index")
                if selected_idx is None or not isinstance(selected_idx, int) or selected_idx < 0 or selected_idx >= len(data["storylines"]):
                    # interest_score가 가장 높은 스토리라인 선택
                    best_idx = 0
                    best_score = -1.0
                    for idx, storyline in enumerate(data["storylines"]):
                        score = storyline.get("interest_score", 0.0)
                        if score > best_score:
                            best_score = score
                            best_idx = idx
                    selected_idx = best_idx
                    data["selected_storyline_index"] = selected_idx
                
                # 선택된 스토리라인을 별도 필드로 추가 (편의를 위해)
                selected_storyline = data["storylines"][selected_idx]
                data["selected_storyline"] = selected_storyline
                data["selected_topic"] = selected_storyline.get("topic", "")
                
                return data
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    raise RuntimeError(f"스토리라인 생성 실패: {str(e)}") from e
                continue
        raise RuntimeError("스토리라인 생성 실패: 최대 재시도 횟수 초과")

    def compose_story_with_context(self, all_candidates: list, work_title: str, topic: str):
            """
            Gemini가 후보 모멘트들을 보고 전체 흐름에 맞는 스토리 구성을 다시 수행합니다.
            """
            # 후보 데이터를 텍스트로 정리
            candidates_str = ""
            for i, m in enumerate(all_candidates):
                candidates_str += f"ID: {i}, 시간: {m['start_sec']}~{m['end_sec']}s, 역할: {m['story_role']}, 내용: {m['subtitle']}, 중요도: {m['importance']}\n"

            prompt = f"""
    # Role
    너는 쇼츠 영상 편집 전문가다. 전체 영상 분석 결과를 바탕으로 여러 개의 쇼츠 스토리라인을 생성해라.

    # Task
    제공된 영상 분석 데이터를 기반으로 
    시청자의 몰입을 극대화할 수 있는 2가지 타입의 스토리라인(하이라이트형, 서사형)을 3개씩 구성하고, 
    그중 가장 성공 가능성이 높은 하나를 최종 선정하여 출력하라.

    # Input Data
    - 제목: {work_title}
    - 주제: {topic}
    - 후보 장면 및 분석 데이터:
    {candidates_str}

    # Constraints & Rules
    1. Highlight Type: 
    - 모든 chunk의 candidate_moments들 중, 맥락 설명 없이 가장 자극적이고 강렬한 장면(Peak Tension) 하나를 중심으로 구성할 것.
    - viral_type: 해당되는 것 모두 기재.

    2. Storytelling Type: 
    - 모든 chunk의 candidate_moments 중에서 여러 장면들을 선정해 기승전결이 있도록 여러 장면을 유기적으로 연결할 것.
    - 캐릭터의 행적을 조명하거나 영상의 주요 서사를 요약.
    - 각 storyline 내에서는 가장 눈길을 끄는 장면을 hook으로 사용하고, hook이 전개상 앞부분이면 "여정몰입형", 전개상 뒷부분이면 "결과선공개형"으로 분류.
    - key_source 작성 규칙: 서사 구성에 실제로 활용된 재료만 선택적으로 포함할 것. (전부 다 쓸 필요 없음)

    3. Common: 
    각 장면의 `start_sec`, `end_sec`를 명시하고, 영상의 '후킹'을 위한 TTS 라인을 반드시 포함할 것.

    다음 JSON 스키마로만 응답:
    {{
    "storylines": [
        {{
        "storyline_index": 0,
        "chunk_index": 0,
        "candidate_index": 0,
        "shorts_type": "highlight",
        "topic": "주제명",
        "viral_type": "conflicts_and_twists|humor_points|love_points|relatability_points|saida_points|goguma_points",
        "topic_reason": "선정 이유",
        "score": 0.0,
        "start_sec": 0.0,
        "end_sec": 0.0
        }},
        {{
        "storyline_index": 1,
        "shorts_type": "storytelling",
        "sequence_type": "여정몰입형|결과선공개형",
        "topic": "주제명",
        "key_source": {{
            "main_plot": "선택적: 활용한 경우만 요약 작성",
            "sub_plots": "선택적: 활용한 경우만 요약 작성",
            "focus_characters": ["선택적: 캐릭터 이름 리스트"]
        }},
        "topic_reason": "서사 구성 이유",
        "score": 0.0,
        "storyline": {{
            "hook": {{ "chunk_index": 0, "candidate_index": 0, "start_sec": 0.0, "end_sec": 0.0, "description": "장면 설명", "tts_line": "", "use_original_audio": true }},
            "build": [ {{ "chunk_index": 0, "candidate_index": 0, "start_sec": 0.0, "end_sec": 0.0, "description": "장면 설명", "tts_line": "", "use_original_audio": true }} ],
            "payoff": {{ "chunk_index": 0, "candidate_index": 0, "start_sec": 0.0, "end_sec": 0.0, "description": "장면 설명", "tts_line": "", "use_original_audio": true }}
        }}
        }}
    ],
    "selected_storyline_index": 0,
    "title_txt":"적절한 제목 정하기",
    "selected_storyline": {{ "이곳에 선정된 인덱스의 객체를 그대로 복사해서 출력": "" }}
    }}
    """
            # Gemini에게 전달 (generate_content 등의 메서드 사용)
            response = self.client.models.generate_content(
                model=self.config.model_name,
                contents=prompt,
            )
            try:
                # JSON 파싱 로직 (기존 클라이언트 내 json_loader 활용)
                import json
                result = json.loads(response.text.replace('```json', '').replace('```', ''))
                return result
            except:
                return None


def load_gemini_client() -> GeminiClient:
    # .env 파일 로드 (프로젝트 루트에서 찾기)
    # gemini_client.py는 app/modules/에 있으므로 프로젝트 루트는 parent.parent
    project_root = Path(__file__).resolve().parent.parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        # 프로젝트 루트에 없으면 현재 디렉토리에서도 시도
        load_dotenv()
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is required. "
            "Please set it in .env file or as an environment variable."
        )
    model_name = os.getenv("GEMINI_MODEL_NAME", "gemini-3-pro-preview")
    max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
    return GeminiClient(GeminiConfig(api_key=api_key, model_name=model_name, max_retries=max_retries))


def _validate_gemini_schema(data: dict[str, Any]) -> None:
    required_keys = {
        "chunk_index",
        "chunk_start_sec",
        "chunk_end_sec",
        "summary",
        "candidate_moments",
        "title_candidates",
    }
    missing = required_keys - data.keys()
    if missing:
        raise ValueError(f"Missing keys in Gemini response: {missing}")
    if not isinstance(data["candidate_moments"], list) or len(data["candidate_moments"]) < 5:
        raise ValueError("candidate_moments must be a list of at least 5 items")
    for moment in data["candidate_moments"]:
        for key in [
            "start_sec",
            "end_sec",
            "importance",
            "hook_score",
            "topic_alignment_score",
            "story_role",
            "reason",
            "subtitle",
            "tts_line",
        ]:
            if key not in moment:
                raise ValueError(f"Missing key {key} in candidate moment")
        if moment["story_role"] not in {"hook", "build", "payoff"}:
            raise ValueError("story_role must be hook/build/payoff")