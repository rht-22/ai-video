from __future__ import annotations

from enum import Enum
from typing import Any


class GenreType(str, Enum):
    # 드라마
    DRAMA_ROMANCE = "drama_romance"
    DRAMA_THRILLER = "drama_thriller"
    DRAMA_MAKJANG = "drama_makjang"
    DRAMA_LEGAL = "drama_legal"
    DRAMA_SAGEUK = "drama_sageuk"
    # 예능
    VARIETY_TALK = "variety_talk"
    VARIETY_OBSERVATION = "variety_observation"
    VARIETY_SURVIVAL = "variety_survival"
    VARIETY_MUKBANG = "variety_mukbang"
    # 영화
    MOVIE_GENERAL = "movie_general"
    # 기본 (기존 호환)
    AUTO = "auto"


class ContentFormat(str, Enum):
    """콘텐츠 형식: 연속극/독립/시즌제/예능."""
    EPISODIC = "episodic"
    STANDALONE = "standalone"
    SEASON_BASED = "season_based"
    VARIETY_EPISODE = "variety_episode"
    AUTO = "auto"


class EditingFocus(str, Enum):
    """편집 포커스: 사건/인물/감정/서사/리액션."""
    EVENT = "event"
    CHARACTER = "character"
    EMOTION = "emotion"
    NARRATIVE = "narrative"
    REACTION = "reaction"
    AUTO = "auto"


# ─────────────────────────────────────────────
# 장르 → 기본 ContentFormat / EditingFocus 매핑
# ─────────────────────────────────────────────
_GENRE_DEFAULT_FORMAT: dict[str, str] = {
    "drama_romance": "episodic",
    "drama_thriller": "episodic",
    "drama_makjang": "episodic",
    "drama_legal": "episodic",
    "drama_sageuk": "episodic",
    "variety_talk": "variety_episode",
    "variety_observation": "variety_episode",
    "variety_survival": "variety_episode",
    "variety_mukbang": "variety_episode",
    "movie_general": "standalone",
}

_GENRE_DEFAULT_FOCUS: dict[str, str] = {
    "drama_romance": "emotion",
    "drama_thriller": "event",
    "drama_makjang": "reaction",
    "drama_legal": "narrative",
    "drama_sageuk": "narrative",
    "variety_talk": "reaction",
    "variety_observation": "character",
    "variety_survival": "event",
    "variety_mukbang": "emotion",
    "movie_general": "narrative",
}


def resolve_auto_format(genre: str) -> str:
    """장르 기반 기본 ContentFormat을 반환합니다."""
    return _GENRE_DEFAULT_FORMAT.get(genre, "episodic")


def resolve_auto_focus(genre: str) -> str:
    """장르 기반 기본 EditingFocus를 반환합니다."""
    return _GENRE_DEFAULT_FOCUS.get(genre, "narrative")


# ─────────────────────────────────────────────
# 장르별 모듈 레지스트리
# ─────────────────────────────────────────────
_GENRE_MODULES: dict[GenreType, Any] = {}


def _load_genre_module(genre: GenreType) -> Any:
    """장르별 모듈을 lazy import합니다."""
    if genre in _GENRE_MODULES:
        return _GENRE_MODULES[genre]

    module = None
    if genre == GenreType.DRAMA_ROMANCE:
        from app.prompts import drama_romance as module
    elif genre == GenreType.DRAMA_THRILLER:
        from app.prompts import drama_thriller as module
    elif genre == GenreType.DRAMA_MAKJANG:
        from app.prompts import drama_makjang as module
    elif genre == GenreType.DRAMA_LEGAL:
        from app.prompts import drama_legal as module
    elif genre == GenreType.DRAMA_SAGEUK:
        from app.prompts import drama_sageuk as module
    elif genre == GenreType.VARIETY_TALK:
        from app.prompts import variety_talk as module
    elif genre == GenreType.VARIETY_OBSERVATION:
        from app.prompts import variety_observation as module
    elif genre == GenreType.VARIETY_SURVIVAL:
        from app.prompts import variety_survival as module
    elif genre == GenreType.VARIETY_MUKBANG:
        from app.prompts import variety_mukbang as module
    elif genre == GenreType.MOVIE_GENERAL:
        from app.prompts import movie_general as module

    if module is not None:
        _GENRE_MODULES[genre] = module
    return module


# ─────────────────────────────────────────────
# 편집 포커스/콘텐츠 형식 모듈 레지스트리
# ─────────────────────────────────────────────
_FOCUS_MODULES: dict[str, Any] = {}
_FORMAT_MODULES: dict[str, Any] = {}


def _load_focus_module(focus: str) -> Any:
    """편집 포커스 모듈을 lazy import합니다."""
    if focus in _FOCUS_MODULES:
        return _FOCUS_MODULES[focus]

    module = None
    if focus == "event":
        from app.prompts import focus_event as module
    elif focus == "character":
        from app.prompts import focus_character as module
    elif focus == "emotion":
        from app.prompts import focus_emotion as module
    elif focus == "narrative":
        from app.prompts import focus_narrative as module
    elif focus == "reaction":
        from app.prompts import focus_reaction as module

    if module is not None:
        _FOCUS_MODULES[focus] = module
    return module


def _load_format_module(fmt: str) -> Any:
    """콘텐츠 형식 모듈을 lazy import합니다."""
    if fmt in _FORMAT_MODULES:
        return _FORMAT_MODULES[fmt]

    module = None
    if fmt == "episodic":
        from app.prompts import format_episodic as module
    elif fmt == "standalone":
        from app.prompts import format_standalone as module
    elif fmt == "season_based":
        from app.prompts import format_season_based as module
    elif fmt == "variety_episode":
        from app.prompts import format_variety_episode as module

    if module is not None:
        _FORMAT_MODULES[fmt] = module
    return module


class PromptRegistry:
    """장르/포커스/형식별 프롬프트 블록과 설정을 제공하는 레지스트리."""

    # ── 장르 ──

    @staticmethod
    def get_analysis_genre_block(genre: GenreType) -> str:
        if genre == GenreType.AUTO:
            return ""
        module = _load_genre_module(genre)
        if module is None:
            return ""
        return getattr(module, "GENRE_BLOCK", "")

    @staticmethod
    def get_story_genre_block(genre: GenreType) -> str:
        if genre == GenreType.AUTO:
            return ""
        module = _load_genre_module(genre)
        if module is None:
            return ""
        return getattr(module, "STORY_GENRE_BLOCK", "")

    @staticmethod
    def get_scoring_weights(genre: GenreType) -> dict[str, float] | None:
        if genre == GenreType.AUTO:
            return None
        module = _load_genre_module(genre)
        if module is None:
            return None
        return getattr(module, "SCORING_WEIGHTS", None)

    @staticmethod
    def get_duration_range(genre: GenreType) -> tuple[int, int] | None:
        if genre == GenreType.AUTO:
            return None
        module = _load_genre_module(genre)
        if module is None:
            return None
        return getattr(module, "DURATION_RANGE", None)

    # ── 편집 포커스 ──

    @staticmethod
    def get_focus_analysis_block(focus: str) -> str:
        """편집 포커스별 분석 프롬프트 블록."""
        if focus in ("auto", ""):
            return ""
        module = _load_focus_module(focus)
        if module is None:
            return ""
        return getattr(module, "ANALYSIS_BLOCK", "")

    @staticmethod
    def get_focus_story_block(focus: str) -> str:
        """편집 포커스별 스토리 구성 프롬프트 블록."""
        if focus in ("auto", ""):
            return ""
        module = _load_focus_module(focus)
        if module is None:
            return ""
        return getattr(module, "STORY_BLOCK", "")

    @staticmethod
    def get_focus_weight_modifiers(focus: str) -> dict[str, float] | None:
        """편집 포커스별 스코어링 가중치 조정값."""
        if focus in ("auto", ""):
            return None
        module = _load_focus_module(focus)
        if module is None:
            return None
        return getattr(module, "WEIGHT_MODIFIERS", None)

    # ── 콘텐츠 형식 ──

    @staticmethod
    def get_format_analysis_block(fmt: str) -> str:
        """콘텐츠 형식별 분석 프롬프트 블록."""
        if fmt in ("auto", ""):
            return ""
        module = _load_format_module(fmt)
        if module is None:
            return ""
        return getattr(module, "ANALYSIS_BLOCK", "")

    @staticmethod
    def get_format_story_block(fmt: str) -> str:
        """콘텐츠 형식별 스토리 구성 프롬프트 블록."""
        if fmt in ("auto", ""):
            return ""
        module = _load_format_module(fmt)
        if module is None:
            return ""
        return getattr(module, "STORY_BLOCK", "")
