from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CharacterInfo:
    """리서치에서 얻은 캐릭터 + 배우 정보."""
    character_name: str      # 극중 인물명
    actor_name: str          # 배우/출연자명
    role_description: str    # 역할 설명
    image_path: Path | None = None  # 다운로드된 배우 사진 로컬 경로
    image_url: str | None = None    # TMDb 원본 URL


@dataclass(frozen=True)
class ResearchResult:
    work_context: str
    episodes_context: str
    raw_data: dict[str, Any]
    sources: list[str]
    characters: list[CharacterInfo] = field(default_factory=list)


def _build_research_prompt(work_title: str, episode: int | None) -> str:
    episode_constraint = ""
    if episode is not None:
        episode_constraint = (
            f"\n⚠️ 중요: 반드시 1회~{episode}회까지의 정보만 수집하세요.\n"
            f"{episode + 1}회 이후의 내용은 절대 포함하지 마세요. 스포일러 방지가 필수입니다.\n"
        )

    return f"""당신은 한국 드라마/예능 전문 리서처입니다.
다음 작품에 대해 웹에서 검색하여 정보를 수집하고 정리해 주세요.

작품명: {work_title}
{episode_constraint}
다음 JSON 형식으로 반환하세요:

{{
  "title": "작품명",
  "genre": "장르 (예: 로맨틱코미디, 스릴러, 예능, 드라마 등)",
  "tone": "작품의 전반적 톤/분위기 (예: 잔잔하고 섬세한, 긴장감 넘치는 등)",
  "synopsis": "작품 전체 시놉시스 (3~5문장)",
  "setting": "시대적/공간적 배경",
  "characters": [
    {{
      "name": "등장인물명",
      "actor": "배우/출연자명",
      "role": "역할 설명 (직업, 성격, 위치 등)",
      "relationships": ["다른 인물과의 관계 설명"]
    }}
  ],
  "episode_summaries": [
    {{
      "episode": 1,
      "summary": "줄거리 요약 (2~3문장)",
      "key_events": ["주요 사건 1", "주요 사건 2"]
    }}
  ],
  "key_conflicts": ["핵심 갈등/서사 1", "핵심 갈등/서사 2"],
  "themes": ["주요 테마 1", "주요 테마 2"]
}}

검색 지침:
- 나무위키, 위키백과, 공식 홈페이지, 뉴스 기사 등에서 정보를 수집하세요.
- 등장인물은 주요 인물 위주로 정리하세요 (최대 10명).
- episode_summaries는 각 회차별로 핵심 줄거리와 주요 사건을 정리하세요.
- 영상에 없는 내용을 창작하지 마세요. 검색 결과에 기반한 사실만 포함하세요.
- 반드시 JSON만 출력하세요. 마크다운 코드블록이나 설명 텍스트를 추가하지 마세요.
"""


def _format_work_context(raw_data: dict[str, Any]) -> str:
    parts: list[str] = []

    genre = raw_data.get("genre", "")
    tone = raw_data.get("tone", "")
    synopsis = raw_data.get("synopsis", "")
    setting = raw_data.get("setting", "")

    if genre or tone:
        parts.append(f"장르: {genre} | 톤: {tone}")
    if synopsis:
        parts.append(f"시놉시스: {synopsis}")
    if setting:
        parts.append(f"배경: {setting}")

    characters = raw_data.get("characters", [])
    if characters:
        parts.append("\n[등장인물 및 관계]")
        for ch in characters:
            name = ch.get("name", "")
            actor = ch.get("actor", "")
            role = ch.get("role", "")
            actor_str = f" ({actor})" if actor else ""
            parts.append(f"- {name}{actor_str}: {role}")
            for rel in ch.get("relationships", []):
                parts.append(f"  관계: {rel}")

    conflicts = raw_data.get("key_conflicts", [])
    if conflicts:
        parts.append("\n[핵심 갈등/서사]")
        for c in conflicts:
            parts.append(f"- {c}")

    themes = raw_data.get("themes", [])
    if themes:
        parts.append("\n[주요 테마]")
        for t in themes:
            parts.append(f"- {t}")

    return "\n".join(parts)


def _format_episodes_context(raw_data: dict[str, Any], episode: int | None) -> str:
    summaries = raw_data.get("episode_summaries", [])
    if not summaries:
        return ""

    if episode is not None:
        summaries = [s for s in summaries if s.get("episode", 0) <= episode]

    summaries.sort(key=lambda s: s.get("episode", 0))

    limit_str = f"1회~{episode}회" if episode else "전체"
    parts = [f"[회차별 줄거리 ({limit_str})]"]

    for s in summaries:
        ep = s.get("episode", "?")
        summary = s.get("summary", "")
        parts.append(f"{ep}회: {summary}")
        for event in s.get("key_events", []):
            parts.append(f"  - {event}")

    return "\n".join(parts)


def _extract_sources(response: Any) -> list[str]:
    """Gemini 응답에서 grounding 출처 URL을 추출합니다."""
    sources: list[str] = []
    try:
        candidates = getattr(response, "candidates", None)
        if not candidates:
            return sources
        metadata = getattr(candidates[0], "grounding_metadata", None)
        if not metadata:
            return sources
        chunks = getattr(metadata, "grounding_chunks", None) or []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            if web:
                uri = getattr(web, "uri", None)
                if uri:
                    sources.append(uri)
    except Exception:
        pass
    return sources


def _build_characters_list(raw_data: dict[str, Any]) -> list[CharacterInfo]:
    """raw_data에서 CharacterInfo 리스트를 생성합니다 (이미지 없는 상태)."""
    characters: list[CharacterInfo] = []
    for ch in raw_data.get("characters", []):
        characters.append(CharacterInfo(
            character_name=ch.get("name", ""),
            actor_name=ch.get("actor", ""),
            role_description=ch.get("role", ""),
        ))
    return characters


def research_work(
    work_title: str,
    episode: int | None,
    gemini_client: Any,
) -> ResearchResult:
    """Gemini Google Search grounding으로 작품 정보를 검색합니다."""
    from app.modules.gemini_client import _extract_json_from_markdown

    prompt = _build_research_prompt(work_title, episode)
    max_retries = gemini_client.config.max_retries

    for attempt in range(max_retries):
        try:
            # Google Search grounding으로 호출
            try:
                response = gemini_client.client.models.generate_content(
                    model=gemini_client.config.model_name,
                    contents=[prompt],
                    config=gemini_client.types.GenerateContentConfig(
                        tools=[gemini_client.types.Tool(
                            google_search=gemini_client.types.GoogleSearch()
                        )],
                    ),
                )
            except (TypeError, AttributeError):
                # GoogleSearch grounding 미지원 시 일반 호출로 폴백
                print("  [WARN] Google Search grounding 미지원 — 일반 Gemini 호출로 폴백")
                response = gemini_client.client.models.generate_content(
                    model=gemini_client.config.model_name,
                    contents=[prompt],
                )

            json_text = _extract_json_from_markdown(response.text)
            raw_data = json.loads(json_text)

            sources = _extract_sources(response)

            work_context = _format_work_context(raw_data)
            episodes_context = _format_episodes_context(raw_data, episode)
            characters = _build_characters_list(raw_data)

            return ResearchResult(
                work_context=work_context,
                episodes_context=episodes_context,
                raw_data=raw_data,
                sources=sources,
                characters=characters,
            )

        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  [WARN] 작품 정보 검색 실패 (시도 {attempt + 1}/{max_retries}): {e}")
                print(f"  {wait}초 후 재시도...")
                time.sleep(wait)
            else:
                print(f"  [WARN] 작품 정보 검색 최종 실패: {e}")
                print("  → 작품 정보 없이 진행합니다.")
                return ResearchResult(
                    work_context="",
                    episodes_context="",
                    raw_data={},
                    sources=[],
                    characters=[],
                )

    # unreachable, but for type safety
    return ResearchResult(work_context="", episodes_context="", raw_data={}, sources=[], characters=[])
