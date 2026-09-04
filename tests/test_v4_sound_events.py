"""대사 없는 소리 사건 검출 — 「티키타카 편집점 지침서」 모드 [A] 의 재료.

이 절이 지키는 것은 셋이다:
  ① 규칙이 실측대로 돈다(기준선은 그 소재의 말소리 크기 · 길이 상한이 배경을 거른다)
  ② 순수·결정적 — 같은 격자면 같은 목록(지문·재개의 전제)
  ③ 게이트 미지정이면 프롬프트가 **바이트 동일**(회귀 0)
"""

from __future__ import annotations

import pytest

from app.modules.grid import sound_events as SE


# ── 재료 ────────────────────────────────────────────────────────────────────

def _arousal(points: list[tuple[float, float, float]]) -> list[dict]:
    """(t, energy_db, dynamics) → arousal 표본."""
    return [{"t": t, "energy_db": e, "dynamics": d} for t, e, d in points]


def _grid(spans: list[tuple[float, float, bool]],
          points: list[tuple[float, float, float]]) -> dict:
    return {
        "span_candidates": [{"id": f"sp{i:04d}", "t_in": a, "t_out": b, "is_audio": v}
                            for i, (a, b, v) in enumerate(spans)],
        "arousal": _arousal(points),
    }


# ── 기준선 ─────────────────────────────────────────────────────────────────

def test_baseline_is_the_median_voiced_peak():
    """절대 dB 가 아니라 **그 소재 안의 말소리 크기**를 자로 쓴다.

    마스터링 레벨이 다른 소재에서 절대값을 쓰면 전부 걸리거나 전부 빠진다."""
    g = _grid([(0, 1, True), (1, 2, True), (2, 3, True)],
              [(0.5, -30.0, 0), (1.5, -20.0, 0), (2.5, -25.0, 0)])
    assert SE.voiced_peak_baseline(g) == pytest.approx(-25.0)


def test_baseline_is_none_without_material():
    """잴 재료가 없으면 판정하지 않는다 — 오판 금지."""
    assert SE.voiced_peak_baseline({"span_candidates": [], "arousal": []}) is None
    assert SE.detect_sound_events({"span_candidates": [], "arousal": []}) == []


# ── 검출 규칙 ───────────────────────────────────────────────────────────────

def _material(unvoiced: list[tuple[float, float]], loud_db: float = -20.0) -> dict:
    """유성 기준선 −25dB 짜리 소재에 무성 구간을 얹는다."""
    spans = [(0.0, 1.0, True), (1.0, 2.0, True)]
    pts = [(0.5, -25.0, 0.0), (1.5, -25.0, 0.0)]
    for a, b in unvoiced:
        spans.append((a, b, False))
        t = a + 0.25
        while t < b:
            pts.append((round(t, 3), loud_db, 5.0))
            t += 0.5
    return _grid(spans, pts)


def test_loud_unvoiced_span_is_an_event():
    ev = SE.detect_sound_events(_material([(10.0, 12.0)]))
    assert len(ev) == 1
    assert (ev[0]["start_sec"], ev[0]["end_sec"]) == (10.0, 12.0)
    assert ev[0]["over_db"] == pytest.approx(5.0)      # −20 vs 기준선 −25


def test_quiet_unvoiced_span_is_not_an_event():
    """조용한 무성은 그냥 무성이다 — 사건이 아니다."""
    assert SE.detect_sound_events(_material([(10.0, 12.0)], loud_db=-26.0)) == []


def test_voiced_span_is_never_an_event():
    """대사가 있으면 그건 모드 [S] 다 — 아무리 커도 [A] 가 아니다."""
    g = _grid([(0, 1, True), (1, 2, True), (10, 12, True)],
              [(0.5, -25.0, 0), (1.5, -25.0, 0), (10.5, -5.0, 9), (11.5, -5.0, 9)])
    assert SE.detect_sound_events(g) == []


def test_long_loud_stretch_is_background_not_an_event():
    """🛑 길이 상한이 핵심 판별자다.

    실측(신병4 EPK): 임계를 넘긴 35건 중 13.1초·16.9초짜리는 전부 배경 TV·음악이었고
    (1033초 구간은 일기예보 방송음), 짧은 것만 리액션·타격음이었다. 지침서의 [A] 도
    짧다("컵을 쾅 내려놓는 소리 1.5초")."""
    assert SE.detect_sound_events(_material([(10.0, 30.0)])) == []
    assert len(SE.detect_sound_events(_material([(10.0, 15.0)]))) == 1


def test_too_short_is_skipped():
    """arousal 은 0.5s hop 이다 — 그보다 짧으면 편집점이 못 된다."""
    assert SE.detect_sound_events(_material([(10.0, 10.2)])) == []


def test_adjacent_spans_merge_into_one_event():
    """재단이 나눠 놓은 이웃은 한 사건이다(격자는 6초 등분으로 자른다)."""
    ev = SE.detect_sound_events(_material([(10.0, 12.0), (12.0, 14.0)]))
    assert len(ev) == 1
    assert (ev[0]["start_sec"], ev[0]["end_sec"]) == (10.0, 14.0)


def test_merged_result_still_respects_the_length_cap():
    """붙여 놓고 보니 배경이면 버린다 — 병합이 상한을 우회하면 안 된다."""
    parts = [(10.0 + 2.0 * i, 12.0 + 2.0 * i) for i in range(5)]   # 합쳐서 10초
    assert SE.detect_sound_events(_material(parts)) == []


# ── 순수·결정성 ─────────────────────────────────────────────────────────────

def test_detection_is_deterministic_and_pure():
    g = _material([(10.0, 12.0), (30.0, 31.0)])
    before = repr(g)
    assert SE.detect_sound_events(g) == SE.detect_sound_events(g)
    assert repr(g) == before, "입력 격자를 건드렸다"


def test_limit_keeps_the_strongest_but_returns_time_order():
    """상한은 **센 것부터** 남기고, 돌려줄 때는 다시 시각순이다."""
    spans = [(0.0, 1.0, True), (1.0, 2.0, True)]
    pts = [(0.5, -25.0, 0.0), (1.5, -25.0, 0.0)]
    # 뒤로 갈수록 조용한 사건 셋
    for i, db in enumerate((-15.0, -18.0, -21.0)):
        a = 10.0 + 10.0 * i
        spans.append((a, a + 1.0, False))
        pts.append((a + 0.25, db, 3.0))
        pts.append((a + 0.75, db, 3.0))
    ev = SE.detect_sound_events(_grid(spans, pts), limit=2)
    assert [e["start_sec"] for e in ev] == [10.0, 20.0]      # 시각순으로 돌아온다
    assert [e["over_db"] for e in ev] == [10.0, 7.0]         # 남은 것은 센 둘


# ── 게이트 — 미지정이면 프롬프트·요약이 바이트 동일 ────────────────────────

def test_grid_summary_is_byte_identical_without_events():
    """v3 Stage 1 과 게이트를 안 켠 v4 가 여기 온다 — 한 글자도 움직이면 안 된다."""
    from app.v3.seq_analyze import summarize_grid
    g = {"source": {"duration_sec": 100.0},
         "span_candidates": [{"id": "sp0000", "t_in": 0.0, "t_out": 5.0,
                              "is_audio": True}],
         "scene_cuts": [1.0, 2.0], "arousal": []}
    assert summarize_grid(g) == summarize_grid(g, sound_events=None)
    assert summarize_grid(g) == summarize_grid(g, sound_events=[])
    assert "소리 사건" not in summarize_grid(g)


def test_grid_summary_lists_events_when_given():
    from app.v3.seq_analyze import summarize_grid
    g = {"source": {"duration_sec": 100.0},
         "span_candidates": [{"id": "sp0000", "t_in": 0.0, "t_out": 5.0,
                              "is_audio": True}],
         "scene_cuts": [1.0], "arousal": []}
    out = summarize_grid(g, sound_events=[
        {"start_sec": 10.0, "end_sec": 11.5, "over_db": 4.2}])
    assert "소리 사건 1개" in out and "10.0~11.5(+4)" in out


def test_candidates_prompt_gate_is_byte_identical_when_off():
    from app.v4 import candidates as C
    base = dict(work_title="작품", transcript="0.0 대사", grid_summary="요약",
                templates=("recap_dialogue",), target_sec=50.0, max_sec=60.0)
    assert C.build_prompt(**base) == C.build_prompt(**base, sound_events=False)
    assert C.SOUND_EVENT_CLAUSE not in C.build_prompt(**base)
    on = C.build_prompt(**base, sound_events=True)
    assert C.SOUND_EVENT_CLAUSE in on
    # 절이 두 가지를 못박는다: quote 는 null · 목록 밖을 지어내지 마라
    assert "`quote` 는 `null`" in C.SOUND_EVENT_CLAUSE
    assert "지어내지 마라" in C.SOUND_EVENT_CLAUSE


def test_two_gates_are_independent():
    """`--nonlinear` 와 `--sound-events` 는 서로를 켜지 않는다."""
    from app.v4 import candidates as C
    base = dict(work_title="작품", transcript="0.0 대사", grid_summary="요약",
                templates=("recap_dialogue",), target_sec=50.0, max_sec=60.0)
    only_order = C.build_prompt(**base, nonlinear=True)
    only_sound = C.build_prompt(**base, sound_events=True)
    assert C.SOUND_EVENT_CLAUSE not in only_order
    assert C.ORDER_FREE_CLAUSE not in only_sound
    both = C.build_prompt(**base, nonlinear=True, sound_events=True)
    assert C.ORDER_FREE_CLAUSE in both and C.SOUND_EVENT_CLAUSE in both
