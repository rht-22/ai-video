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
# 청크 분석 프롬프트 (바이럴 최적화 버전)
# ─────────────────────────────────────────────
GEMINI_PROMPT_TEMPLATE = """
너는 드라마/예능 톤의 쇼츠 편집 어시스턴트다.
현재 단계에서 분석된 내용은 다음 단계에서 쇼츠 제작을 위한 스토리 구성 단계에서 재료로 사용된다.
반드시 JSON만 출력한다. 코드블록 금지.
영상에 없는 내용은 절대 창작하지 말 것.

[입력 정보]
- 작품명: {work_title}
- 주제: {topic}
- 청크 범위: {chunk_start_sec} ~ {chunk_end_sec} 초
{work_context_block}
{narrative_skeleton_block}
{previous_episodes_context_block}
- ⚠️ 모든 start_sec / end_sec는 반드시 첨부된 영상 파일의 시작(0초)을 기준으로 한 상대값으로 반환할 것

- 자막(있으면): {transcript_text}
- 씬 경계(있으면): {scene_boundaries}
- 자막/대사 참고 (화자명 포함): {transcript_hint}
{previous_context}

요구 사항:
- 아래 JSON 스키마 구조를 100% 동일하게 유지하여 출력
- candidate_moments 최소 {min_candidates}개
- 드라마/예능 톤, 짧은 리액션 문구는 가능하지만 과도 금지
- 최신 쇼츠 트렌드 반영: 자연스러운 톤, 짧은 문장, 강조/리액션 요소
- 분석 결과 해당 항목이 없을 경우 빈 배열 대신 null로 출력
- 타이틀 시퀀스, 엔딩 크레딧은 제외하기
- ⚠️ **영상에 실제로 존재하는 장면만** 후보에 포함하라. 추론이나 상상으로 장면을 만들어내지 마라.
- 각 candidate_moment의 'start_sec'~'end_sec' 구간은 반드시 첨부 영상에서 실제 확인 가능해야 한다.
- 영상 전체를 빠짐없이 스캔하라. 앞부분에만 집중하지 말고 중반~후반도 균등하게 분석할 것.
- 확실하지 않은 내용은 넣지 마라.
- 완결되지 않은 부분(엔딩)은 이후 전개에 대한 궁금증을 유발하는 장면일 수 있으므로 확정된 전개로 장담하지 말 것.
- "start_sec"~"end_sec" 내의 상황만 봐도 이해가 가능해야 한다.
- `continues_from`: 이 장면이 다른 candidate_moment와 직접 이어지는 경우 `{{"chunk_index": N, "candidate_index": M}}` 형태로 명시, 독립 장면이면 null
- `highlight_eligible`: 이 클립 하나만으로 완결된 쇼츠가 될 수 있으면 true.
  판단 기준: 클립 자체에 감정적 완결성이 있고, 선행 맥락 없이도 단독으로 이해 가능한 경우
  (웃긴 상황의 시작~반응 등, 특정 상황의 시작과 끝이 클립 하나에 담기는 경우).
- `highlight_reason`: highlight_eligible이 true인 경우에만 작성. 이 클립이 단독으로 완결성을 갖는 이유를 1문장으로 기술. (false면 null)

[description 규칙]
- description은 compose story 단계를 위한 정보이므로 영상과 자막을 기반으로 시간 순서에 맞게 정확하고 객관적으로 최소 5 문장 이상.
- description은 실제 장면 묘사를 유지하고, 앞 장면이 나중 내용에 의해 재해석되는 경우 재해석된 의미는 reason 필드에만 반영할 것. 과대해석 및 과장 금지.
- description에서 행동의 범주를 바꾸지 마라. 장면에서 명확히 관찰된 행동(발화·동작·표정)만 그 종류 그대로 기술하고, 확인되지 않은 의도·감정·결과를 덧씌우지 마라.
- ⚠️ description에서 내레이션·독백·보이스오버(VO)는 반드시 "~의 내레이션", "~가 속으로 독백한다", "VO로 ~가 말한다" 등으로 명시하여 실제 대화와 혼동되지 않도록 할 것.

[Audience Appeal 필드 — 각 candidate_moment에 반드시 포함]
- character_focus: 이 장면의 주요 인물 이름 배열
- scene_location: 이 장면의 배경 장소를 구체적으로 기술 (예: "병원 복도", "카페 야외석", "잠수교 위")
  ※ sequence_id(시간 연속성)와 독립적인 공간 정보다. 같은 장소라도 시간이 달라지면 sequence_id는 달라진다.
  스토리 구성 단계에서 장소 전환의 자연스러움과 공간 대비를 판단하는 데 사용된다.
- timeline_position: 이 장면이 현재 시점 사건인지 여부 (해당 없으면 "현재"로 반환).
  선택값: 현재 | 과거 | 불명
- ⚠️ 대사가 있는 장면에서 인물의 행동/의도를 묘사할 때는 대사 내용을 최우선으로 반영하라.
  캐릭터 설정(질병, 성격 등 배경 지식)이 대사와 충돌하면 대사를 믿어라.


다음 스키마로만 응답:

{{
  "chunk_index": 0,
  "chunk_start_sec": 0,
  "chunk_end_sec": 300,
  "summary": "해당 청크 전체의 핵심 내용 요약",
  "main_plot": "이 구간의 핵심 내용 요약 80자 이내",
  "candidate_moments": [
    {{
      "chunk_index": 0,
      "candidate_index": 0,
      "start_sec": 12.4,
      "end_sec": 25.8,
      "importance": 0.0,
      "hook_score": 0.0,
      "topic_alignment_score": 0.0,
      "characters_in_scene": ["인물명1", "인물명2"],
      "description": "장면 설명(묘사 위주)",
      "transcript": "start_sec~end_sec 구간에서 가장 핵심이 되는 '단 한 명'의 주요 발화. 내레이션/독백/VO인 경우 '[내레이션]' 접두어를 붙일 것",
      "continues_from": {{"chunk_index": 0, "candidate_index": 0}},
      "highlight_eligible": false,
      "highlight_reason": null,
      "character_focus": ["인물명"],
      "scene_location": "장면 배경 장소",
      "timeline_position": "현재|과거|불명"
    }}
  ]
}}
"""

# ─────────────────────────────────────────────
# 스토리 구성 프롬프트 (레퍼런스 기반 바이럴 최적화)
# ─────────────────────────────────────────────
STORY_COMPOSITION_PROMPT = """
# Role
너는 쇼츠 영상 편집 전문가다. 전체 영상 분석 결과를 바탕으로 여러 개의 쇼츠 스토리라인을 생성해라.

# Task
제공된 영상 분석 데이터를 기반으로 시청자의 몰입을 극대화할 수 있는 2가지 타입의 스토리라인(하이라이트형, 서사형)을 3개씩 구성하고,
그중 가장 성공 가능성이 높은 하나를 최종 선정하여 출력하라.
원본 영상에 대해 아무 정보가 없는 사람도 만들어진 쇼츠에 대해 흥미를 느낄 수 있어야 한다.

# Input Data
- 제목: {work_title}
- 주제: {topic}
- 후보 장면 및 분석 데이터:
{candidates_str}

# Constraints & Rules
1. Highlight Type:
- candidate_moments들 중, `highlight_eligible`이 true인 것만 선별. 
- 맥락 설명 없이 시청 지속을 유도할 수 있는 하나를 중심으로 구성할 것.

2. Storytelling Type:
- 모든 chunk의 candidate_moments 중에서 여러 장면들을 선정해 완결성이 있도록 여러 장면을 유기적으로 연결할 것.
- 캐릭터의 행적을 조명하거나 영상의 특정 사건을 요약.
- 각 storyline 내에서는 가장 눈길을 끄는 장면을 hook으로 사용하고, hook이 전개상 앞부분이면 "여정몰입형", 전개상 뒷부분이면 "결과선공개형"으로 분류.

3. 연속성 (Storytelling 전용):
각 후보 장면에는 `sequence_id`(정수)와 `continues_from` 필드가 있다.
- `sequence_id`가 같은 장면끼리 = 원본에서 직접 이어지는 연속 장면 → 인접 배치 시 자연스럽게 연결됨.
- `sequence_id`가 다른 장면끼리 = 다른 시간·상황의 장면 → 인접 배치 시 `tts_line`으로 맥락 전환을 반드시 설명할 것.
- `continues_from`이 있는 장면은 앞 장면의 맥락을 이어받으므로, 선행 장면 없이 단독 배치 시 이해가 어려울 수 있음.
- build·payoff는 반드시 원본 시간 순(start_sec 오름차순)으로 배치할 것.

4. 제목 구조 (2줄 필수):
각 storyline마다 반드시 2줄 제목을 작성할 것.
- title_line1: 상황/맥락 설명 (15자 이내, 흰색으로 표시됨)
- title_line2: 캐릭터/주제 중심 후킹 문구 (15자 이내, 노란색 강조로 표시됨)

예시:
- "모두 기피하는 깡치사건" / "클리어하는 이한영"
- "차기 회장을 지명한" / "준수의 놀라운 선택"
- "교우 불화의 원인이" / "부모님 재력인 현실"

⚠️ 이모지 금지.

5. Common:
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
    "topic_reason": "선정 이유",
    "score": 0.0,
    "title_line1": "상황/맥락 (15자 이내)",
    "title_line2": "캐릭터/후킹 (15자 이내)",
    "start_sec": 0.0,
    "end_sec": 0.0
    }},
    {{
    "storyline_index": 1,
    "shorts_type": "storytelling",
    "sequence_type": "여정몰입형|결과선공개형",
    "topic": "주제명",
    "topic_reason": "서사 구성 이유",
    "score": 0.0,
    "title_line1": "상황/맥락 (15자 이내)",
    "title_line2": "캐릭터/후킹 (15자 이내)",
    "storyline": {{
        "hook": {{ "chunk_index": 0, "candidate_index": 0, "start_sec": 0.0, "end_sec": 0.0, "description": "장면 설명", "tts_line": "", "use_original_audio": true }},
        "build": [ {{ "chunk_index": 0, "candidate_index": 0, "start_sec": 0.0, "end_sec": 0.0, "description": "장면 설명", "tts_line": "", "use_original_audio": true }} ],
        "payoff": {{ "chunk_index": 0, "candidate_index": 0, "start_sec": 0.0, "end_sec": 0.0, "description": "장면 설명", "tts_line": "", "use_original_audio": true }}
    }}
    }}
],
"selected_storyline_index": 0,
"shorts_type": "storytelling|highlight",
"selection_reason": "이 스토리라인을 선택한 이유",
"title_line1": "최종 제목 1줄 (맥락, 15자 이내)",
"title_line2": "최종 제목 2줄 (후킹, 15자 이내)",
"title_txt": "title_line1 + title_line2 합친 전체 제목",
"selected_storyline": {{ "이곳에 선정된 인덱스의 객체를 그대로 복사해서 출력": "" }}
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
                f"\n[작품 정보 — 인물 식별·스토리 이해 참고용]\n{payload['work_context']}\n"
                "⚠️ 위 정보는 인물명·관계·장르를 정확히 파악하기 위한 참고용입니다.\n"
                "장면 선택 기준은 리서치가 아니라 오직 현재 영상 내 감정 강도·반응·의외성으로 판단하세요.\n"
            )

        # 청크 길이에 비례한 최소 후보 수 (1분당 1개, 올림, 최소 1개)
        chunk_duration = chunk_end - chunk_start
        min_candidates = max(1, -(-int(chunk_duration) // 60))

        # ── 전체 영상 skeleton 블록 주입 ──
        narrative_skeleton_block = ""
        sk = payload.get("narrative_skeleton")
        if sk and isinstance(sk, dict):
            sk_json = json.dumps(sk, ensure_ascii=False, indent=2)
            sk_json_escaped = sk_json.replace("{", "{{").replace("}", "}}")
            narrative_skeleton_block = (
                "\n[전체 영상 배경 정보 — 인물 식별 및 맥락 파악용]\n"
                f"{sk_json_escaped}\n"
                "⚠️ 위 정보는 인물 이름을 일관되게 식별하고 현재 청크가 영상 전체에서 어느 위치인지 파악하기 위한 참고용이다.\n"
                "⚠️ 장면 선택 기준은 이 skeleton이 아니라 오직 현재 첨부 영상 안에서의 재미·흥미도·반응성이다.\n"
                "⚠️ skeleton에 등장하지 않은 장면이라도 자체적으로 흥미롭거나 반응이 강하면 candidate로 포함하라.\n"
            )

            # skeleton의 key_characters를 work_context_block에 보완 주입
            # (SRT 없이 리서치도 부실한 경우 영상 스캔 단계에서 추출한 이름이 유일한 단서)
            skeleton_chars = sk.get("key_characters", [])
            if skeleton_chars:
                reliable = [
                    c for c in skeleton_chars
                    if c.get("name") and "추정" not in (c.get("name_sources") or [])
                ]
                all_chars = skeleton_chars  # 추정 포함 전체도 참고용으로 전달
                char_lines = []
                for c in all_chars:
                    name = c.get("name", "")
                    role = c.get("role", "")
                    sources = c.get("name_sources", [])
                    src_note = f" [출처: {', '.join(sources)}]" if sources else ""
                    char_lines.append(f"- {name}: {role}{src_note}")
                skeleton_char_block = (
                    "\n[영상 스캔에서 감지된 인물 — 이름 식별에 활용]\n"
                    + "\n".join(char_lines)
                    + "\n⚠️ '출처: 자막에서 직접 언급' 또는 '다른 인물이 부름'인 이름은 신뢰도 높음. "
                    "'추정'인 이름은 참고만 하고 실제 영상 내 발화로 재확인하라.\n"
                )
                work_context_block = work_context_block + skeleton_char_block

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
            narrative_skeleton_block=narrative_skeleton_block,
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
            "  - 자막/대사에서 불린 이름이 있으면 반드시 그 이름을 사용\n"
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
            "continues_from", "transcript",
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

        # ── narrative_skeleton 블록 ──
        narrative_skeleton_json_block = ""
        if narrative_skeleton and isinstance(narrative_skeleton, dict):
            sk_json = json.dumps(narrative_skeleton, ensure_ascii=False, indent=2)
            sk_escaped = sk_json.replace("{", "{{").replace("}", "}}")
            narrative_skeleton_json_block = (
                "\n[전체 영상 배경 정보 — 인물·맥락 참고용]\n" + sk_escaped +
                "\n⚠️ 위 정보는 인물 이름 일관성과 전체 영상 분위기 파악을 위한 참고용이다.\n"
                "⚠️ 스토리라인은 후보 장면들의 재미·반응·흥미도만을 기준으로 자유롭게 구성하라. "
                "skeleton에 없는 장면이어도 매력적이면 우선 선택하라.\n"
            )

        prompt = STORY_COMPOSITION_PROMPT.format(
            work_title=work_title,
            topic=topic,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            candidates_str=candidates_str,
            work_context_block=work_context_block,
            episodes_context_block=episodes_context_block,
            story_topic_line=story_topic_line,
            narrative_skeleton_json_block=narrative_skeleton_json_block,
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

                for _sl in storylines:
                    _sl["composite_score"] = float(_sl.get("score", 0) or 0)

                result["selected_storyline"] = storylines[selected_idx]
                # 선정된 storyline의 shorts_type을 top-level에 자동 반영
                result.setdefault("shorts_type", storylines[selected_idx].get("shorts_type", "storytelling"))
                result.setdefault("selection_reason", "")
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
        for key in [
            "start_sec", "end_sec", "importance", "hook_score",
            "topic_alignment_score", "description", "transcript",
        ]:
            if key not in moment:
                raise ValueError(f"Missing key {key} in candidate moment")
