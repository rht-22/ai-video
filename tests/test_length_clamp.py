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


def test_pinned_no_whole_clip_removal():
    # 2026-08-19 참교육_b32cec9f 실측 재현: 편집실이 보낸 4구간 합계 59.78s(상한 59.7 을
    # 0.08s 초과). 종전엔 score 0.0 인 build(사람이 추가, chunk_index=-1) 19.63s 를
    # **통째로 제거**해 40.15s 로 만들었다 — 사람이 넣은 장면이 사라진다.
    # allow_remove=False 면 부분 trim 만으로 줄이고 4구간이 모두 남아야 한다.
    import dataclasses
    clips = [
        _clip("hook", 809.44, 825.0),
        _clip("build", 929.2, 948.83),
        _clip("build", 957.7, 961.8),
        _clip("payoff", 966.548, 987.038),
    ]
    clips[1:3] = [dataclasses.replace(c, chunk_index=-1, candidate_index=-1)
                  for c in clips[1:3]]
    out, _ = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=59.7,
                                        allow_remove=False)
    assert len(out) == 4                      # 어떤 구간도 통째로 사라지지 않는다
    assert _total(out) <= 59.7 + 1e-6


def test_default_still_removes_low_score_build():
    # 기본(allow_remove=True) 동작 회귀 방지 — 자동 생성 경로는 종전과 같아야 한다.
    clips = [
        _clip("hook", 0.0, 20.0),
        _clip("build", 100.0, 125.0),
        _clip("payoff", 200.0, 235.0),
    ]  # 합계 80s, build 제거 시 55s (in-band)
    out, _ = _fit_storyline_to_duration(clips, {}, target_min=40.0, target_max=59.7)
    assert len(out) == 2
    assert all(c.role != "build" for c in out)
