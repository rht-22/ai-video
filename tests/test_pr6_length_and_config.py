"""PR-6 — 클립 길이 상한 튜너블 + A/B config 노브 단위 테스트.

검증 대상:
1. validate_story_clips 의 max_duration_tolerance 파라미터 (레거시 1.5 → 조정 가능).
2. AppConfig 의 env 기반 A/B 노브 (SILENCE_CUT_PROFILE / MAX_DURATION_TOLERANCE / *_DURATION_SEC).
   기본값은 기존 동작을 정확히 보존해야 한다 (베이스라인 회귀 방지).
"""
from __future__ import annotations

import importlib

from app.modules.story_builder import StoryClip, validate_story_clips


def _clip(start, end):
    return StoryClip(role="build", start_sec=float(start), end_sec=float(end),
                     subtitle="", use_original_audio=True)


# ──────────────────────────────────────────────────────────────
# validate_story_clips — max_duration_tolerance
# ──────────────────────────────────────────────────────────────


def test_tolerance_default_is_legacy_1_5():
    # max=60 → 기본 상한 90s. 70s 클립은 통과(레거시 동작 보존).
    clips = [_clip(0, 70)]
    ok, msg = validate_story_clips(clips, 40, 60, min_clip_count=1)
    assert ok is True, msg


def test_tighter_tolerance_rejects_long_clip():
    # tolerance 1.1 → 상한 66s. 70s 클립은 거부 (100s 몬스터 방지).
    clips = [_clip(0, 70)]
    ok, msg = validate_story_clips(clips, 40, 60, min_clip_count=1, max_duration_tolerance=1.1)
    assert ok is False
    assert "너무 김" in msg


def test_tighter_tolerance_still_accepts_in_range():
    # 50s 클립은 어떤 tolerance 든 통과
    clips = [_clip(0, 50)]
    ok, _ = validate_story_clips(clips, 40, 60, min_clip_count=1, max_duration_tolerance=1.1)
    assert ok is True


def test_tolerance_does_not_affect_too_short_check():
    clips = [_clip(0, 30)]  # 30s < min 40
    ok, msg = validate_story_clips(clips, 40, 60, min_clip_count=1, max_duration_tolerance=1.1)
    assert ok is False
    assert "너무 짧음" in msg


# ──────────────────────────────────────────────────────────────
# AppConfig — env A/B 노브 (기본값 = 베이스라인)
# ──────────────────────────────────────────────────────────────


def _fresh_config():
    """env 를 반영한 새 AppConfig 인스턴스 (default_factory 가 인스턴스화 시 env 읽음)."""
    import app.config as cfg
    importlib.reload(cfg)
    return cfg.AppConfig()


def test_config_defaults_preserve_baseline(monkeypatch):
    for k in ("SILENCE_CUT_PROFILE", "MAX_DURATION_TOLERANCE",
              "MIN_DURATION_SEC", "MAX_DURATION_SEC", "TARGET_DURATION_SEC"):
        monkeypatch.delenv(k, raising=False)
    c = _fresh_config()
    assert c.silence_cut_profile == "conservative"
    assert c.max_duration_tolerance == 1.5
    assert c.min_duration_sec == 40
    assert c.max_duration_sec == 120     # E20(2026-08-28): 상한 2분 — 사용자 결정
    assert c.target_duration_sec == 50


def test_config_aggressive_arm_from_env(monkeypatch):
    monkeypatch.setenv("SILENCE_CUT_PROFILE", "aggressive")
    monkeypatch.setenv("MAX_DURATION_TOLERANCE", "1.1")
    monkeypatch.setenv("MIN_DURATION_SEC", "34")
    monkeypatch.setenv("MAX_DURATION_SEC", "48")
    c = _fresh_config()
    assert c.silence_cut_profile == "aggressive"
    assert c.max_duration_tolerance == 1.1
    assert c.min_duration_sec == 34
    assert c.max_duration_sec == 48


def test_config_profile_resolves_to_aggressive(monkeypatch):
    # config.silence_cut_profile 문자열이 get_silence_profile 로 실제 프로파일이 됨
    from app.modules.silence_cutter import get_silence_profile
    monkeypatch.setenv("SILENCE_CUT_PROFILE", "aggressive")
    c = _fresh_config()
    profile = get_silence_profile(c.silence_cut_profile)
    assert profile.name == "aggressive"
    assert profile.gap_level is True
