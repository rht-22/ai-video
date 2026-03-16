from __future__ import annotations

import json
import mimetypes
import time
import os
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
너는 드라마/예능 톤의 하이라이트 편집 어시스턴트다.
반드시 JSON만 출력한다. 코드블록 금지.
영상에 없는 내용은 절대 창작하지 말 것.
{trend_note}

입력 정보:
- 작품명: {work_title}
- 주제: {topic}
- 청크 범위: {chunk_start_sec} ~ {chunk_end_sec} 초
- 씬 경계(있으면): {scene_boundaries}
- 전체 줄거리(있으면): {full_summary}
- 스토리라인(있으면): {storyline}
{previous_context}

요구 사항:
- candidate_moments 최소 10개
- story_role은 hook/build/payoff 중 하나
- 드라마/예능 톤, 짧은 리액션 문구는 가능하지만 과도 금지
- 최신 쇼츠 트렌드 반영: 자연스러운 톤, 짧은 문장, 강조/리액션 요소
- JSON 스키마 강제

다음 스키마로만 응답:
{{
  "chunk_index": 0,
  "chunk_start_sec": 0,
  "chunk_end_sec": 300,
  "summary": "요약",
  "candidate_moments": [
    {{
      "start_sec": 12.4,
      "end_sec": 25.8,
      "importance": 0.0,
      "hook_score": 0.0,
      "topic_alignment_score": 0.0,
      "story_role": "hook|build|payoff",
      "reason": "선정 이유",
      "subtitle": "자막(짧게)",
      "tts_line": "TTS 한 문장"
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
        import google.generativeai as genai
        try:
            from google.generativeai import types as genai_types
        except ImportError:
            # google-generativeai 패키지의 경우 types 모듈 경로가 다를 수 있음
            try:
                import google.generativeai.types as genai_types
            except ImportError:
                # types를 직접 사용할 수 없는 경우, Part 클래스를 직접 생성
                genai_types = None

        genai.configure(api_key=config.api_key)
        self.model = genai.GenerativeModel(config.model_name)
        self.genai = genai
        self.genai_types = genai_types

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
                        for m in prev["candidate_moments"][:10]  # 상위 3개만
                    ])
                    context_parts.append(f"주요 모멘트:\n{moments_text}")
            if context_parts:
                previous_context = "\n\n이전 청크들의 분석 결과 (전체 흐름 이해용):" + "\n".join(context_parts)
        
     
        trend_note = "\n최신 쇼츠 트렌드: 자연스러운 톤, 짧고 임팩트 있는 문장, 강조/리액션 요소 포함, TTS는 빠른 속도(1.5배)에 맞춘 문구."
        
        prompt = GEMINI_PROMPT_TEMPLATE.format(
            work_title=payload["work_title"],
            topic=payload["topic"],
            chunk_start_sec=payload["chunk_start_sec"],
            chunk_end_sec=payload["chunk_end_sec"],
            scene_boundaries=payload.get("scene_boundaries") or "없음",
            full_summary=payload.get("full_summary") or "없음",
            storyline=payload.get("storyline") or "없음",
            # previous_context=previous_context + previous_transcript,
            previous_context=previous_context,
            trend_note=trend_note,
        )
        
        # 비디오 파일 경로가 있으면 파일을 직접 읽어서 전달
        video_path = payload.get("video_path")
        content_parts = [prompt]
        
        # File API 방식 적용 (Memory-Safe)
        if video_path:
            video_path_obj = Path(video_path) if isinstance(video_path, str) else video_path
            if video_path_obj.exists():
                try:
                    uploaded_file = self.genai.upload_file(path=str(video_path_obj))
                    while uploaded_file.state.name == "PROCESSING":
                        time.sleep(2)
                        uploaded_file = self.genai.get_file(uploaded_file.name)
                    if uploaded_file.state.name == "FAILED":
                        raise RuntimeError("Gemini File API 업로드 실패")
                    content_parts.append(uploaded_file)
                except Exception as upload_err:
                    print(f"    [WARN] 비디오 업로드 중 오류 발생: {upload_err}")

        
        for attempt in range(self.config.max_retries):
            try:
                # JSON 응답 강제 설정 (가능한 경우)
                generation_config = None
                if self.genai_types and hasattr(self.genai_types, 'GenerateContentConfig'):
                    generation_config = self.genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                    )
                
                if generation_config:
                    response = self.model.generate_content(
                        content_parts,
                        generation_config=generation_config,
                    )
                else:
                    response = self.model.generate_content(content_parts)
                
                # 응답이 None이거나 빈 문자열인지 확인
                if not response or not response.text:
                    error_msg = "Gemini API가 빈 응답을 반환했습니다."
                    if hasattr(response, 'prompt_feedback'):
                        error_msg += f" 피드백: {response.prompt_feedback}"
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError(error_msg)
                    print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                    continue
                
                text = response.text.strip()
                
                # 빈 문자열인지 확인
                if not text:
                    error_msg = "Gemini API 응답이 빈 문자열입니다."
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError(error_msg)
                    print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                    continue
                
                try:
                    # 마크다운 코드 블록 제거
                    json_text = _extract_json_from_markdown(text)
                    data = json.loads(json_text)
                except json.JSONDecodeError as json_err:
                    # JSON 파싱 실패 시 응답 내용 일부 출력
                    preview = text[:200] if len(text) > 200 else text
                    error_msg = f"Gemini 응답 JSON 파싱 실패: {json_err}\n응답 미리보기: {preview}..."
                    if attempt == self.config.max_retries - 1:
                        # 마지막 시도에서는 전체 응답을 포함한 에러 메시지
                        raise ValueError(
                            f"Gemini 응답을 JSON으로 파싱할 수 없습니다.\n"
                            f"에러: {json_err}\n"
                            f"전체 응답:\n{text}"
                        )
                    print(f"    [WARN] JSON 파싱 실패. 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                    print(f"    응답 미리보기: {preview}...")
                    continue
                
                _validate_gemini_schema(data)
                return data
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    # 마지막 시도에서 실패하면 상세한 에러 메시지와 함께 예외 발생
                    error_type = type(e).__name__
                    raise RuntimeError(
                        f"Gemini 분석 실패 ({error_type}): {str(e)}\n"
                        f"청크: {payload.get('chunk_start_sec', '?')}초 ~ {payload.get('chunk_end_sec', '?')}초"
                    ) from e
                print(f"    [WARN] 오류 발생: {type(e).__name__}. 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                continue
        raise RuntimeError("Gemini response failed validation after retries")
    
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
                generation_config = None
                if self.genai_types and hasattr(self.genai_types, 'GenerateContentConfig'):
                    generation_config = self.genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                    )
                
                if generation_config:
                    response = self.model.generate_content(
                        content_parts,
                        generation_config=generation_config,
                    )
                else:
                    response = self.model.generate_content(content_parts)
                
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
                generation_config = None
                if self.genai_types and hasattr(self.genai_types, 'GenerateContentConfig'):
                    generation_config = self.genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                    )
                
                if generation_config:
                    response = self.model.generate_content(
                        [prompt],
                        generation_config=generation_config,
                    )
                else:
                    response = self.model.generate_content([prompt])
                
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
    당신은 최고의 숏폼 영상 편집자입니다. 제공된 하이라이트 후보들을 조합하여 영상의 흐름이 자연스럽고 기승전결이 완벽한 60초 이내의 숏츠 스토리를 구성하세요.

    [영상 정보]
    제목: {work_title}
    주제: {topic}

    [하이라이트 후보 목록]
    {candidates_str}

    [작업 지침]
    1. 단순 점수 위주가 아닌, 영상의 '서사 흐름(Narrative Flow)'을 최우선으로 고려하세요.
    2. 반드시 Hook(도입) -> Build(전개) -> Payoff(결정적 장면)의 순서가 논리적이어야 합니다.
    3. 선택한 장면들의 총 합계 시간이 60초를 넘지 않도록 하세요.
    4. 후보들 중 흐름상 불필요한 장면은 과감히 제외하세요.
    5. 출력은 반드시 아래 JSON 형식으로만 하세요.

    [출력 형식]
    {{
    "selected_ids": [선택한 후보 ID 리스트 (순서대로)],
    "story_reasoning": "이 흐름으로 구성한 이유 요약"
    }}
    """
        # Gemini에게 전달 (generate_content 등의 메서드 사용)
        response = self.model.generate_content(prompt)
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
