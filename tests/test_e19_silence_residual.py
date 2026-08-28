"""E19-6 무음 컷 잔여 정적 하한 회귀 가드.

발주서: docs/prompts/e19-drama-clip-preset.md §6. 벤치마크 실측: 무음은 0~2건으로
밀되 **반전 직후 0.3~0.5s 정적은 남는다**(20.95s 의 0.53s — 편 최대 호흡이자 웃음
비트). aggressive 프로파일이 이런 정적까지 밀면 호흡이 죽는다. 계약 요점:

- `SilenceCutProfile.min_residual_pause_sec`(기본 None) — 안전한 gap 을 **잘라도**
  이만큼의 정적은 남긴다(양쪽 절반씩). **None(기본) = 산출 불변이 회귀 0 조건.**
- gap-level(aggressive) 경로에만 작용한다 — conservative 는 손대지 않는다.
- env `SILENCE_CUT_MIN_RESIDUAL_SEC` / CLI `--silence-min-residual` 로 켠다.
  잘못된 값은 즉시 실패(조용한 무시 = 오타가 기본값으로 발행).
- 프로파일 이름이 같아도 residual 이 다르면 silence_cut 캐시를 무효화한다
  (A/B 두 arm 이 같은 output_dir 를 재사용할 때 stale 방지 — 기존 프로파일 규약 확장).
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.modules.silence_cutter import (
    AGGRESSIVE_PROFILE,
    CONSERVATIVE_PROFILE,
    cut_silence_with_story_filter,
    get_silence_profile,
)
from app.modules.speech import SpeechSegment
from app.modules.story_builder import StoryClip

REPO = Path(__file__).resolve().parents[1]


def _clip(start=0.0, end=10.0):
    return StoryClip(role="hook", start_sec=start, end_sec=end, subtitle="s",
                     use_original_audio=True)


def _cut(clips, segs, profile):
    return cut_silence_with_story_filter(clips, segs, {}, profile=profile)


def _intervals(results):
    return [(round(iv.start_sec, 3), round(iv.end_sec, 3))
            for r in results for iv in r.keep_intervals]


SEGS = [SpeechSegment(0.0, 2.0, "가"), SpeechSegment(5.0, 7.0, "나")]   # gap 3.0s


# ══════════════════════════════════════════════════════════════════════════
# 회귀 0 — 기본값(None)이면 산출 불변
# ══════════════════════════════════════════════════════════════════════════
def test_default_is_none():
    assert AGGRESSIVE_PROFILE.min_residual_pause_sec is None
    assert CONSERVATIVE_PROFILE.min_residual_pause_sec is None


def test_none_and_small_residual_identical_to_baseline():
    base = _intervals(_cut([_clip()], SEGS, AGGRESSIVE_PROFILE))
    # None = 기본, 그리고 2×padding(0.24) 이하의 값도 기존 패딩이 이미 그만큼 남기므로 불변
    for resid in (None, 0.2):
        prof = replace(AGGRESSIVE_PROFILE, min_residual_pause_sec=resid)
        assert _intervals(_cut([_clip()], SEGS, prof)) == base
    # 베이스라인 자체 확인: 컷 뒤 남는 정적 = 2×padding = 0.24s
    assert base == [(0.0, 2.12), (4.88, 7.12)]


# ══════════════════════════════════════════════════════════════════════════
# 잔여 정적 — 잘라도 이만큼은 남는다 (양쪽 절반씩)
# ══════════════════════════════════════════════════════════════════════════
def test_residual_keeps_pause_around_cut():
    prof = replace(AGGRESSIVE_PROFILE, min_residual_pause_sec=0.5)
    out = _intervals(_cut([_clip()], SEGS, prof))
    assert out == [(0.0, 2.25), (4.75, 7.12)]              # 정적 0.5s = 양쪽 0.25s
    # 여전히 잘리긴 한다 — 3.0s 무음이 0.5s 로 준다
    assert (4.75 - 2.25) == 2.5


def test_residual_larger_than_gap_keeps_whole_gap():
    """gap 이 하한보다 짧으면 gap 전체가 남는다 — 없는 정적을 만들어내지 않는다."""
    segs = [SpeechSegment(0.0, 2.0, "가"), SpeechSegment(2.4, 4.0, "나")]   # gap 0.4
    prof = replace(AGGRESSIVE_PROFILE, min_residual_pause_sec=1.0)
    results = _cut([_clip(0.0, 4.0)], segs, prof)
    kept = sum(iv.end_sec - iv.start_sec for r in results for iv in r.keep_intervals)
    assert kept == pytest.approx(4.0, abs=1e-6)             # gap 포함 전체 유지


def test_conservative_untouched_by_residual():
    """conservative 는 gap-level 이 아니라 residual 이 아예 작용하지 않는다."""
    base = _intervals(_cut([_clip()], SEGS, CONSERVATIVE_PROFILE))
    prof = replace(CONSERVATIVE_PROFILE, min_residual_pause_sec=0.5)
    assert _intervals(_cut([_clip()], SEGS, prof)) == base


def test_protected_gap_cap_branch_unchanged():
    """보호 gap 의 dead-air 상한(cap) 분기는 residual 과 무관하게 종전 그대로다.

    ⚠ 화자 보호는 candidate 메타가 있어야 도달한다 — 메타 없음(None)은 aggressive 에서
    첫 검사(protect_missing_candidate=False)로 바로 '컷 가능'이 된다(기존 동작)."""
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=10.0, subtitle="s",
                     use_original_audio=True, chunk_index=0, candidate_index=0)
    lookup = {(0, 0): {"description": "조용한 장면"}}
    segs = [SpeechSegment(0.0, 2.0, "[내레이션] 가"), SpeechSegment(5.0, 7.0, "나")]
    base = [(round(iv.start_sec, 3), round(iv.end_sec, 3))
            for r in cut_silence_with_story_filter([clip], segs, lookup,
                                                   profile=AGGRESSIVE_PROFILE)
            for iv in r.keep_intervals]
    prof = replace(AGGRESSIVE_PROFILE, min_residual_pause_sec=0.5)
    out = [(round(iv.start_sec, 3), round(iv.end_sec, 3))
           for r in cut_silence_with_story_filter([clip], segs, lookup, profile=prof)
           for iv in r.keep_intervals]
    assert out == base
    # 실제로 cap 분기를 탔는지(보호 gap 이 1.0s 로 잘렸는지)도 못박는다 — 픽스처가
    # 조용히 컷 분기로 새면 이 테스트는 아무것도 검증하지 않는다.
    assert base == [(0.0, 2.5), (4.5, 7.12)]                # cap 1.0 = 양쪽 0.5s


# ══════════════════════════════════════════════════════════════════════════
# env / CLI 노브 — 잘못된 값은 즉시 실패
# ══════════════════════════════════════════════════════════════════════════
def test_env_knob_applies_to_aggressive_only(monkeypatch):
    monkeypatch.setenv("SILENCE_CUT_MIN_RESIDUAL_SEC", "0.35")
    assert get_silence_profile("aggressive").min_residual_pause_sec == 0.35
    assert get_silence_profile("conservative").min_residual_pause_sec is None
    monkeypatch.delenv("SILENCE_CUT_MIN_RESIDUAL_SEC")
    assert get_silence_profile("aggressive").min_residual_pause_sec is None


@pytest.mark.parametrize("bad", ["abc", "-0.5", "0", "9"])
def test_env_knob_bad_value_fails(monkeypatch, bad):
    monkeypatch.setenv("SILENCE_CUT_MIN_RESIDUAL_SEC", bad)
    with pytest.raises(ValueError):
        get_silence_profile("aggressive")


def test_cli_flag_sets_env():
    """⚠ _apply_ab_env 는 os.environ 을 직접 만지므로 monkeypatch 로 정리하면 안 된다 —
    monkeypatch 의 되돌리기가 테스트 중간값을 '복원'해 다른 테스트를 오염시킨다
    (실측: test_pr5 의 프로파일 동일성 검사가 이 값에 깨졌다). try/finally 로 지운다."""
    from app.cli import _apply_ab_env, build_parser
    p = build_parser()
    args = p.parse_args(["create_shorts", "--title", "T", "--video", "x.mp4",
                         "--subtitle", "x.srt", "--silence-min-residual", "0.35"])
    try:
        applied = _apply_ab_env(args)
        assert applied["SILENCE_CUT_MIN_RESIDUAL_SEC"] == "0.35"
        assert os.environ["SILENCE_CUT_MIN_RESIDUAL_SEC"] == "0.35"
    finally:
        os.environ.pop("SILENCE_CUT_MIN_RESIDUAL_SEC", None)


# ══════════════════════════════════════════════════════════════════════════
# 배선 — 캐시 무효화·A/B 도구
# ══════════════════════════════════════════════════════════════════════════
def test_pipeline_cache_invalidation_includes_residual():
    """프로파일 이름이 같아도 residual 이 다르면 캐시를 다시 계산해야 한다."""
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert '"min_residual"' in src                          # 체크포인트에 기록
    assert src.count("min_residual") >= 2                   # 기록 + 비교


def test_ab_tool_exists_and_imports():
    tool = REPO / "scripts" / "e19_silence_residual_ab.py"
    assert tool.is_file()
    src = tool.read_text("utf-8")
    assert "cut_silence_with_story_filter" in src           # 같은 함수를 재현에 쓴다
    assert "checkpoint_story" in src and "checkpoint_chunk_transcripts" in src
