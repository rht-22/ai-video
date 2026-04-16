"""TMDb API 래퍼 — 배우 검색 + 프로필 이미지 다운로드.

환경변수 TMDB_API_KEY가 설정되어 있어야 합니다.
없으면 모든 함수가 graceful하게 빈 결과를 반환합니다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import requests

from app.modules.work_researcher import CharacterInfo

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"


def _get_api_key() -> str | None:
    return os.environ.get("TMDB_API_KEY")


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w가-힣]+", "_", name).strip("_") or "unknown"


def search_actor_image(actor_name: str, api_key: str) -> str | None:
    """TMDb에서 배우명으로 검색 → 프로필 이미지 URL 반환.

    반환: 'https://image.tmdb.org/t/p/w500/...' 또는 None.
    """
    try:
        resp = requests.get(
            f"{TMDB_BASE}/search/person",
            params={"query": actor_name, "language": "ko-KR"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        profile_path = results[0].get("profile_path")
        if not profile_path:
            return None
        return f"{TMDB_IMG_BASE}{profile_path}"
    except Exception as e:
        print(f"  [TMDb] 배우 검색 실패 ({actor_name}): {e}")
        return None


def download_cast_images(
    characters: list[dict[str, Any]],
    out_dir: Path,
    api_key: str | None = None,
) -> list[CharacterInfo]:
    """각 캐릭터의 배우 사진을 TMDb에서 다운로드하고 CharacterInfo 리스트 반환.

    Args:
        characters: Gemini research raw_data['characters'] 리스트.
                   각 항목은 {name, actor, role, relationships} 형태.
        out_dir: 이미지 저장 디렉토리 (예: output_dir/_research/).
        api_key: TMDb API Bearer 토큰. None이면 환경변수에서 조회.

    Returns:
        CharacterInfo 리스트 (이미지 다운로드 성공한 캐릭터는 image_path/image_url 포함).
    """
    if api_key is None:
        api_key = _get_api_key()
    if not api_key:
        print("  [TMDb] TMDB_API_KEY 미설정 — 배우 이미지 없이 진행")
        return [
            CharacterInfo(
                character_name=ch.get("name", ""),
                actor_name=ch.get("actor", ""),
                role_description=ch.get("role", ""),
            )
            for ch in characters
        ]

    out_dir.mkdir(parents=True, exist_ok=True)
    result: list[CharacterInfo] = []

    for ch in characters:
        char_name = ch.get("name", "")
        actor_name = ch.get("actor", "")
        role = ch.get("role", "")

        if not actor_name:
            result.append(CharacterInfo(
                character_name=char_name,
                actor_name=actor_name,
                role_description=role,
            ))
            continue

        image_url = search_actor_image(actor_name, api_key)
        image_path: Path | None = None

        if image_url:
            filename = f"cast_{_safe_filename(actor_name)}.jpg"
            local_path = out_dir / filename
            try:
                img_resp = requests.get(image_url, timeout=15)
                img_resp.raise_for_status()
                local_path.write_bytes(img_resp.content)
                image_path = local_path
                print(f"  [TMDb] {actor_name} ({char_name}) 사진 다운로드 완료")
            except Exception as e:
                print(f"  [TMDb] {actor_name} 이미지 다운로드 실패: {e}")

        result.append(CharacterInfo(
            character_name=char_name,
            actor_name=actor_name,
            role_description=role,
            image_path=image_path,
            image_url=image_url,
        ))

    downloaded = sum(1 for c in result if c.image_path is not None)
    print(f"  [TMDb] 배우 이미지: {downloaded}/{len(result)}명 다운로드 완료")
    return result
