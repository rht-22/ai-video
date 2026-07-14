"""최종 길이 클램프 — _fit_storyline_to_duration 이 target_max 초과분을 역할별로 줄이는지 검증.

배경: snap/extend/fill 후 합계가 60s 를 살짝 넘으면(예: 60.24s) ffmpeg 오버헤드까지 더해져
최종 mp4 가 60s 를 넘는다. 파이프라인 마지막에 target_max(=60-margin)로 클램프한다.
"""
from __future__ import annotations

from app.pipeline import _fit_storyline_to_duration
from app.modules.story_builder import StoryClip


def _clip(role, start, end, ve=False):
    return StoryClip(
        role=role, start_sec=float(start), end_sec=float(end), subtitle="s",
        use_original_audio=True, pacing_note="", chunk_index=0, candidate_index=0,
        character_focus=("A",), visual_essential=ve, tts_draft="t",
    )


def _total(clips):
    return sum(c.end_sec - c.start_sec for c in clips)


def test_single_payoff_highlight_clamped_below_max():
    # 단일 payoff highlight 60.24s → ≤59.7. payoff 는 끝(reveal) 보존 → 시작에서 잘림.
    clips = [_clip("payoff", 960.0, 1020.24)]
    out, msg = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=59.7)
    assert _total(out) <= 59.7 + 1e-6
    assert out[-1].end_sec == 1020.24  # 끝 보존
    assert out[0].start_sec > 960.0     # 시작에서 잘림


def test_visual_essential_preserved_on_clamp():
    clips = [_clip("payoff", 0.0, 60.5, ve=True)]
    out, _ = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=59.7)
    assert _total(out) <= 59.7 + 1e-6
    assert all(c.visual_essential for c in out)  # 부수 수정(visual_essential 보존) 회귀 방지


def test_in_band_unchanged():
    clips = [_clip("hook", 0.0, 25.0), _clip("payoff", 100.0, 130.0)]  # 55s, in-band
    out, msg = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=59.7)
    assert abs(_total(out) - 55.0) < 1e-6
    assert msg == ""


def test_multiclip_over_budget_clamped():
    # hook 28 + payoff 32.5 = 60.5 → ≤59.7
    clips = [_clip("hook", 0.0, 28.0), _clip("payoff", 100.0, 132.5)]
    out, _ = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=59.7)
    assert _total(out) <= 59.7 + 1e-6
