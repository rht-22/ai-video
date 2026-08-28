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
    seg = _seg(3.0, 16.0)                                 # 머리 3s·꼬리 4s 무발화
    seg.text = "십삼 초 동안 정상 밀도로 이어지는 대사 텍스트라 환각 필터에 안 걸린다"
    out, stats = apply_speech_gap_pacing(clips, [seg], [], PARAMS)
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


# ── B4 발화 커버리지 가드 (v4 실측 후속) ──────────────────────────────────
# v4 실측: 새 스토리가 대사 없는 액션 구간을 골라 21.4초 무발화 구멍이 났다.
# hook 은 40.5초짜리 환각성 전사 1줄("누가 보냈어?" — 0.17자/초)이 전체를 덮어
# 페이싱이 커버리지로 오인했고, 25초 build 는 전사 0건이라 비주얼 비트 보호로
# 빠져나갔다. 그래서 ① 환각성 세그먼트는 판정 커버리지에서 빼고 ② 스토리라인
# 선택 단계에서 커버리지 하한으로 거른다(선택이 원인, 조립은 벨트).
def test_plausible_filter_drops_hallucination_segment():
    from app.modules.speech import plausible_speech_intervals
    fake = _seg(534.5, 575.0)                             # 40.5s
    fake.text = "누가 보냈어?"                            # 7자 / 40.5s = 0.17자/초
    real = _seg(574.0, 575.0)
    real.text = "내가 보냈어?"
    out = plausible_speech_intervals([fake, real])
    assert out == [real]


def test_plausible_filter_keeps_long_dense_segment():
    from app.modules.speech import plausible_speech_intervals
    s = _seg(0, 12.0)
    s.text = "긴 문장이지만 글자 밀도가 정상인 대사가 계속 이어지는 세그먼트다 정상"
    assert plausible_speech_intervals([s]) == [s]


def test_coverage_ratio():
    from app.modules.speech import speech_coverage_ratio
    clips = [_clip(0, 40), _clip(100, 130)]               # 총 70s
    segs = [_seg(0, 20), _seg(100, 114)]                  # 커버 34s
    assert abs(speech_coverage_ratio(clips, segs) - 34 / 70) < 0.01
    assert speech_coverage_ratio(clips, []) == 0.0


def test_pacing_ignores_hallucination_coverage():
    """페이싱 커버리지도 환각 필터를 지난다 — 40s 가짜 한 줄이 클립을 '전부 발화'로
    위장하면 안 된다. 가짜만 있는 클립은 유효 전사 0 = 통째 유지(오판 금지 규율)."""
    clips = [_clip(0, 40)]
    fake = _seg(0, 40)
    fake.text = "누가?"
    out, stats = apply_speech_gap_pacing(clips, [fake], [], PARAMS)
    assert _spans(out) == [(0, 40)] and stats["gaps_cut"] == 0


def test_min_speech_coverage_schema():
    tone = st.load_style_tone("drama_clip_kr")
    assert tone.pacing["min_speech_coverage"] == 0.55
    with pytest.raises(st.StyleToneError):
        st.validate_tone_data(_data(**{"pacing.min_speech_coverage": 1.5}), "drama_clip_kr")
    data = _data()
    data["pacing"].pop("min_speech_coverage", None)       # 선택 키 — 없어도 유효
    st.validate_tone_data(data, "drama_clip_kr")


def test_coverage_guard_wired_into_storyline_selection():
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert "min_speech_coverage" in src
    assert "발화 커버리지" in src
    # 자리: validate_story_clips(스토리라인 검증) 절 안 — 탈락은 SKIP + 다음 후보
    guard = src.index("발화 커버리지")
    assert src.index("validate_story_clips(\n                sl_clips") < guard


def test_all_rejected_falls_back_to_best_coverage():
    """v5 실측: 세 스토리라인이 전부 커버리지 미달(4%·43%·48%)로 탈락하자
    selected_storyline 폴백이 **가드를 우회해** 최저 커버리지(4%) 구성을 그대로
    렌더했다. 전량 탈락이면 탈락분 중 **최고 커버리지**를 경고와 함께 쓴다 —
    48% 가 4% 보다 낫고, 무인 노드에서 편이 통째로 죽는 것보다도 낫다."""
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert "_cov_rejected" in src
    best = src.index("전량 커버리지 미달")
    fallback = src.index("폴백: 유효한 스토리가 없으면")
    assert best < fallback                    # 최고 커버리지 채택이 폴백보다 먼저


def test_story_block_warns_against_speechless_material():
    """톤 story 블록의 1차 방어 — 대사 빈약 후보를 재료로 쓰지 말라는 지시.
    pacing.min_speech_coverage 가 있는 톤에만 붙는다."""
    tone = st.load_style_tone("drama_clip_kr")
    b = st.story_prompt_block(tone)
    assert "transcript" in b and "대사가 거의 없는" in b


def test_pipeline_wires_pacing_after_gap_fill_before_clamp():
    """자리 고정: gap-fill **뒤**(되메움 방지) · length-clamp **앞**(클램프가 조여진
    총량을 본다). 게이트는 톤 pacing 절이다."""
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    fill = src.index("_fill_intra_storyline_gaps(\n            all_storyline_variants")
    pacing = src.index("apply_speech_gap_pacing(")
    clamp = src.rindex("_fit_storyline_to_duration(\n            _vc")
    assert fill < pacing < clamp
    assert "style_tone_profile.pacing" in src or 'getattr(style_tone_profile, "pacing"' in src
