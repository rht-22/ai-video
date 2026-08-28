"""E20-B1 발화 갭 페이싱 회귀 가드 (2026-08-28).

"오디오가 비지 않고 영상의 호흡이 빠르게" — dB 무음이 아니라 **무발화** 기준의 컷.
김부장 v3 실측: 빗소리 바닥 −16dB 라 silencedetect 무음 0건인데 무발화는 10.5초/4곳.
gap-level 커터는 전사 기반이지만 ① visual_essential 클립을 통째 보존하고(payoff 의
1.1·2.3초 구멍이 그대로 나감) ② silence_cut 이 스냅·확장·갭메움 **앞**에 돌아서 확장이
만든 무발화를 못 보고, 유일한 컷 1.0초는 [gap-fill] 이 도로 메웠다.

그래서 B1 은 **조립 마지막**(snap→ext→gap-fill 뒤·length-clamp 앞)에 도는 별도 패스다:
- 커버리지 = 대사 전사 ∪ 계획된 내레이션 cue 창(앵커+duration) — 내레이션이 살
  자리를 잘라 버리면 안 된다(E19-3 dialogue_gaps_only 와 한 몸).
- 내부 무발화 run > max_speech_gap_sec → 가운데를 잘라 gap_residual_sec(양쪽 절반)만
  남기고 클립을 쪼갠다. 머리는 head_lead_in_sec, 꼬리는 tail_hold_sec 까지만 허용
  (꼬리 여유가 리액션 컷 자리다 — v3 엔딩 원수 표정이 거기 살았다).
- 전사가 하나도 없는 클립은 건드리지 않는다(비주얼 비트 — 오판 금지).
- 게이트는 톤 프로파일 pacing 절 하나(없으면 회귀 0).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.silence_cutter import apply_speech_gap_pacing
from app.modules.story_builder import StoryClip
from app.modules import style_tone as st

REPO = Path(__file__).resolve().parents[1]

PARAMS = {"max_speech_gap_sec": 1.2, "gap_residual_sec": 0.4,
          "head_lead_in_sec": 1.0, "tail_hold_sec": 2.5}


def _clip(start, end, role="build", visual=False):
    return StoryClip(role=role, start_sec=float(start), end_sec=float(end),
                     subtitle="s", use_original_audio=True, visual_essential=visual)


def _seg(s, e):
    return SimpleNamespace(start_sec=float(s), end_sec=float(e), text="말")


def _spans(clips):
    return [(round(c.start_sec, 2), round(c.end_sec, 2)) for c in clips]


def test_internal_gap_cut_to_residual():
    clips = [_clip(0, 20)]
    segs = [_seg(0, 5), _seg(15, 20)]
    out, stats = apply_speech_gap_pacing(clips, segs, [], PARAMS)
    assert _spans(out) == [(0, 5.2), (14.8, 20.0)]        # 잔여 0.4 = 양쪽 0.2
    assert stats["gaps_cut"] == 1 and abs(stats["removed_sec"] - 9.6) < 0.01


def test_small_gap_untouched():
    clips = [_clip(0, 11)]
    segs = [_seg(0, 5), _seg(6, 11)]                      # gap 1.0 ≤ 1.2
    out, stats = apply_speech_gap_pacing(clips, segs, [], PARAMS)
    assert _spans(out) == [(0, 11)] and stats["gaps_cut"] == 0


def test_cue_window_counts_as_coverage():
    """계획된 내레이션 창은 발화로 친다 — cue 가 살 자리를 잘라 버리면 안 된다."""
    clips = [_clip(0, 20)]
    segs = [_seg(0, 5), _seg(15, 20)]
    cues = [{"source_time_sec": 7.0, "duration_sec": 1.5}]
    out, stats = apply_speech_gap_pacing(clips, segs, cues, PARAMS)
    # 커버리지 [0,5]·[7,8.5]·[15,20] → 두 무발화 run(5~7, 8.5~15)이 각각 잘린다
    assert len(out) == 3
    assert _spans(out)[1] == (6.8, 8.9)                   # cue 창(7~8.7)이 통째로 산다
    assert stats["gaps_cut"] == 2


def test_head_trim_and_tail_hold():
    clips = [_clip(0, 20)]
    segs = [_seg(3.0, 16.0)]                              # 머리 3s·꼬리 4s 무발화
    out, stats = apply_speech_gap_pacing(clips, segs, [], PARAMS)
    assert _spans(out) == [(2.0, 18.5)]                   # 머리 리드인 1.0 · 꼬리 2.5 유지


def test_clip_without_transcript_kept_whole():
    """전사 없는 비주얼 비트는 판정하지 않는다 — 컷하면 비트 자체가 소멸한다."""
    clips = [_clip(100, 110)]
    out, stats = apply_speech_gap_pacing(clips, [_seg(0, 5)], [], PARAMS)
    assert _spans(out) == [(100, 110)] and stats["gaps_cut"] == 0


def test_visual_essential_with_dialogue_is_processed():
    """대사가 있는 visual_essential 클립도 무발화 상한은 받는다 — v3 payoff 가 바로
    이 유형이라(2.3초 구멍) 여기를 빼면 B1 이 존재 이유를 잃는다."""
    clips = [_clip(0, 20, role="payoff", visual=True)]
    segs = [_seg(0, 5), _seg(15, 20)]
    out, stats = apply_speech_gap_pacing(clips, segs, [], PARAMS)
    assert stats["gaps_cut"] == 1


def test_tiny_piece_not_created():
    """분할이 0.8초 미만 조각을 만들면 그 컷은 접는다 — 조각 난사가 더 나쁘다."""
    clips = [_clip(0, 7)]
    segs = [_seg(0, 0.5), _seg(5, 7)]                     # 앞 조각이 0.5+0.2 = 0.7 < 0.8
    out, stats = apply_speech_gap_pacing(clips, segs, [], PARAMS)
    assert len(out) == 1                                  # 분할 안 함(현상 유지)


def test_pure_function_and_sub_clip_metadata():
    clips = [_clip(0, 20, role="hook")]
    segs = [_seg(0, 5), _seg(15, 20)]
    out, _ = apply_speech_gap_pacing(clips, segs, [], PARAMS)
    assert clips[0].start_sec == 0 and clips[0].end_sec == 20   # 원본 불변
    assert all(c.role == "hook" for c in out)                   # 메타 승계
    assert all(c.use_original_audio for c in out)


# ── 톤 스키마·배선 ─────────────────────────────────────────────────────────
def _data(**over):
    base = json.loads(
        (REPO / "app" / "data" / "style_tones" / "drama_clip_kr.json").read_text("utf-8"))
    for path, value in over.items():
        cur = base
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur[k]
        cur[keys[-1]] = value
    return base


def test_drama_clip_kr_has_pacing():
    tone = st.load_style_tone("drama_clip_kr")
    assert tone.pacing is not None
    assert tone.pacing["max_speech_gap_sec"] == 1.2
    assert tone.pacing["gap_residual_sec"] == 0.4
    assert tone.pacing["head_lead_in_sec"] == 1.0
    assert tone.pacing["tail_hold_sec"] == 2.5


@pytest.mark.parametrize("path,value", [
    ("pacing.max_speech_gap_sec", 0.1),           # 범위 밖
    ("pacing.gap_residual_sec", 3.0),             # 범위 밖
    ("pacing.gap_residual_sec", 1.5),             # residual ≥ max_gap 은 모순
    ("pacing.head_lead_in_sec", -1),
    ("pacing", []),                               # dict 아님
])
def test_invalid_pacing_fails(path, value):
    with pytest.raises(st.StyleToneError):
        st.validate_tone_data(_data(**{path: value}), "drama_clip_kr")


def test_pacing_section_optional():
    data = _data()
    data.pop("pacing", None)
    st.validate_tone_data(data, "drama_clip_kr")          # 없어도 유효(회귀 0)


def test_profile_matches_preset_pacing():
    preset = json.loads((REPO / "docs" / "design_presets"
                         / "drama_clip_kr.preset.json").read_text("utf-8"))
    tone = st.load_style_tone("drama_clip_kr")
    ap = preset["audio_pacing"]
    assert tone.pacing["max_speech_gap_sec"] == ap["max_speech_gap_sec"]["value"]
    assert tone.pacing["gap_residual_sec"] == ap["gap_residual_sec"]["value"]


def test_pipeline_wires_pacing_after_gap_fill_before_clamp():
    """자리 고정: gap-fill **뒤**(되메움 방지) · length-clamp **앞**(클램프가 조여진
    총량을 본다). 게이트는 톤 pacing 절이다."""
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    fill = src.index("_fill_intra_storyline_gaps(\n            all_storyline_variants")
    pacing = src.index("apply_speech_gap_pacing(")
    clamp = src.rindex("_fit_storyline_to_duration(\n            _vc")
    assert fill < pacing < clamp
    assert "style_tone_profile.pacing" in src or 'getattr(style_tone_profile, "pacing"' in src
