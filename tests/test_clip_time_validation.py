"""렌더 전 컷 시간 검증 — 2026-08-05 하루에 두 번 터진 실패의 회귀 방지.

두 실패 모두 **분석이 끝난 뒤 렌더 단계**에서야 드러나 30~90분과 Gemini 비용을 버렸다:
  · 샤먼: 미신전 2화 — 컷 [948, 997] / 소스 875초 → 비디오 0프레임 → 후속 검사가 exit 234
  · 약한영웅 Class 1 — 컷 [2348.68, 2348.52] (시작>끝) → ffmpeg 'Invalid argument'
"""
from __future__ import annotations

from app.modules.renderer import sanitize_clips
from app.modules.story_builder import StoryClip


def _clip(start, end, role="build"):
    return StoryClip(role=role, start_sec=start, end_sec=end, subtitle="", use_original_audio=True)


def test_keeps_valid_clips_untouched():
    clips = [_clip(10.0, 20.0), _clip(30.0, 45.5)]
    clean, notes = sanitize_clips(clips, 100.0)
    assert clean == clips and notes == []


def test_drops_inverted_clip():
    # 약한영웅 실측: -ss 2348.68 -to 2348.52 → ffmpeg 가 입력 자체를 거부한다
    clean, notes = sanitize_clips([_clip(2348.68, 2348.52), _clip(2393.1, 2440.0)], 3000.0)
    assert [(c.start_sec, c.end_sec) for c in clean] == [(2393.1, 2440.0)]
    assert any("2348" in n for n in notes)


def test_clamps_small_overshoot():
    # 소수점 몇 초는 경계 반올림 — 잘라서 살린다
    clean, notes = sanitize_clips([_clip(860.0, 876.5)], 875.0)
    assert (clean[0].start_sec, clean[0].end_sec) == (860.0, 875.0)
    assert any("경계 오차" in n for n in notes)


def test_drops_large_overshoot_instead_of_clamping():
    # 🛑 수십 초 초과는 시간축이 밀렸다는 신호 — 잘라 렌더하면 엉뚱한 장면이 조용히 나간다
    clean, notes = sanitize_clips([_clip(860.0, 997.0)], 875.0)
    assert clean == []
    assert any("시간축이 밀린" in n for n in notes)


def test_drops_clip_entirely_beyond_source():
    # 샤먼 2화 실측: 소스 875초인데 컷이 948~997 — 시작조차 소스 밖이다
    clean, notes = sanitize_clips([_clip(948.0, 997.0)], 875.0)
    assert clean == []
    assert any("소스 길이" in n for n in notes)


def test_clamps_negative_start():
    clean, _ = sanitize_clips([_clip(-3.0, 12.0)], 100.0)
    assert (clean[0].start_sec, clean[0].end_sec) == (0.0, 12.0)


def test_drops_too_short_after_clamp():
    # 소스 끝에 0.05초만 걸치는 컷은 남겨도 프레임이 안 나온다
    clean, _ = sanitize_clips([_clip(874.95, 900.0)], 875.0)
    assert clean == []


def test_unknown_duration_still_catches_inversion():
    # 프로브 실패(길이 None)여도 시작>끝 은 잡아야 한다 — 길이 검사만 건너뛴다
    clean, _ = sanitize_clips([_clip(50.0, 40.0), _clip(10.0, 20.0)], None)
    assert [(c.start_sec, c.end_sec) for c in clean] == [(10.0, 20.0)]


def test_nan_and_non_numeric_are_dropped():
    assert sanitize_clips([_clip(float("nan"), 10.0)], 100.0)[0] == []
    assert sanitize_clips([_clip("어디쯤", 10.0)], 100.0)[0] == []
