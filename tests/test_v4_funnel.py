"""V4-7 결정적 깔때기 회귀 가드 (2026-09-04).

계약 `docs/v4/M1-interfaces.md` §5 · 기획서 §3-7.

이 파일이 고정하는 것은 **층의 분리**다:
- `hard_problems` 는 사실만 낸다(예고·크레딧 꼬리 겹침 · 소스 밖 · 발화 커버리지 ·
  길이). intro 겹침은 하드가 아니고, 모르는 구역 이름은 조용히 무시되지 않는다.
- `soft_signals` 는 값만 낸다. 근거가 없는 축은 None 이고, **정지 화면 축과 발화 0 run
  축은 따로 기록된다**(대화 고정샷을 오감점하지 않기 위해 — 축 교체 금지).
- 점수는 순위에만 쓰인다. **점수 임계로 탈락하는 후보는 없다**(M9 원칙).
- 발화 커버리지는 반드시 `plausible_speech_intervals` 를 지난다(E20-B4 — 40초짜리
  환각 전사 한 줄이 커버리지를 위장하던 실측).
- 동점은 소스 시각순으로 깬다. 같은 입력이면 같은 순서다(결정성).
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from app.v4 import funnel as F

REPO = Path(__file__).resolve().parents[1]


def _v3_rubric_weights() -> dict[str, float]:
    """`app/v3/story.py` 의 RUBRIC_WEIGHTS 를 소스에서 직접 읽는다(import 없이)."""
    tree = ast.parse((REPO / "app" / "v3" / "story.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "RUBRIC_WEIGHTS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("app/v3/story.py 에서 RUBRIC_WEIGHTS 를 못 찾았다")


# ── 픽스처 헬퍼 ────────────────────────────────────────────────────────────

def _cand(cid: str, pieces: list[tuple[float, float]]) -> dict:
    return {"id": cid, "template": "recap_dialogue",
            "segments": [{"start_sec": float(a), "end_sec": float(b),
                          "quote": "대사"} for a, b in pieces],
            "reason": "사유", "title_draft": "제목 가안"}


def _seg(a: float, b: float) -> list[dict]:
    return [{"start_sec": float(a), "end_sec": float(b)}]


def _iv(a: float, b: float, text: str | None = None) -> dict:
    """텍스트를 든 발화 구간 — 이 모양만 plausible 밀도 판정을 지난다."""
    return {"start_sec": float(a), "end_sec": float(b), "text": text or "정상 대사입니다"}


def _words(*starts: float) -> list[dict]:
    return [{"start_sec": float(s), "end_sec": float(s) + 0.3} for s in starts]


BASE = dict(exception_sectors={}, source_duration_sec=4000.0, scene_cuts=[],
            speech_intervals=[], words=[], min_sec=40.0, max_sec=120.0)


# ── union_iou — 수계산 ─────────────────────────────────────────────────────

def test_union_iou_no_overlap_is_zero():
    assert F.union_iou(_seg(0, 10), _seg(20, 30)) == 0.0


def test_union_iou_identical_is_one():
    assert F.union_iou(_seg(0, 10), _seg(0, 10)) == 1.0


def test_union_iou_partial_hand_calc():
    # 교집합 5 / 합집합 15
    assert F.union_iou(_seg(0, 10), _seg(5, 15)) == pytest.approx(5 / 15)


def test_union_iou_multi_piece_is_not_a_hull():
    """다중 구간의 정체 — v1 단일 IoU 는 (0,110) 껍데기로 읽어 0.09 를 냈다.

    합집합으로 재면 교집합 10 / 합집합 20 = 0.5 다. 이 차이가 dedup 임계 0.5 의
    양쪽을 가르므로 v4 후보 형태에 단일 IoU 를 쓰면 안 된다."""
    a = _seg(0, 10) + _seg(100, 110)
    assert F.union_iou(a, _seg(0, 10)) == pytest.approx(0.5)
    hull = 10 / 110          # 단일 구간으로 읽었을 때의 값(대조용)
    assert abs(F.union_iou(a, _seg(0, 10)) - hull) > 0.3


def test_union_iou_degenerate_inputs_are_zero():
    assert F.union_iou([], _seg(0, 10)) == 0.0
    assert F.union_iou(_seg(10, 10), _seg(0, 10)) == 0.0


# ── 하드 탈락 4종 ──────────────────────────────────────────────────────────

def test_hard_clean_candidate_has_no_problems():
    cand = _cand("c01", [(100, 150)])
    assert F.hard_problems(cand, exception_sectors={}, source_duration_sec=4000.0,
                           speech_intervals=[(100.0, 150.0)],
                           min_sec=40.0, max_sec=120.0) == []


def test_hard_tail_sector_overlap():
    """예고 구역과 겹치면 탈락 — 가왕쇼 사고(예고 50초가 엔딩 오염)의 그 판정."""
    cand = _cand("c01", [(990, 1040)])
    got = F.hard_problems(cand, exception_sectors={"teaser": {"start_sec": 1000.0,
                                                             "end_sec": 1100.0}},
                          source_duration_sec=4000.0, speech_intervals=[(990.0, 1040.0)],
                          min_sec=40.0, max_sec=120.0)
    assert [F.problem_code(r) for r in got] == [F.CODE_SECTOR_OVERLAP]
    assert "40.00s 겹침" in got[0]


def test_hard_intro_overlap_is_not_hard():
    """intro 는 꼬리 구역이 아니다 — 계약이 하드로 두지 않았다(기획서 §3-7)."""
    cand = _cand("c01", [(0, 60)])
    assert F.hard_problems(cand, exception_sectors={"intro": {"start_sec": 0.0,
                                                             "end_sec": 43.0}},
                           source_duration_sec=4000.0, speech_intervals=[(0.0, 60.0)],
                           min_sec=40.0, max_sec=120.0) == []


def test_hard_unknown_sector_name_fails_loud():
    """조용히 무시하면 그 구역과 겹친 후보가 검사 없이 통과한다."""
    with pytest.raises(ValueError) as e:
        F.hard_problems(_cand("c01", [(100, 150)]),
                        exception_sectors={"outro": {"start_sec": 0.0, "end_sec": 5.0}},
                        source_duration_sec=4000.0, speech_intervals=[],
                        min_sec=40.0, max_sec=120.0)
    assert "outro" in str(e.value) and "teaser" in str(e.value)


def test_hard_beyond_source():
    cand = _cand("c01", [(3980, 4030)])
    got = F.hard_problems(cand, exception_sectors={}, source_duration_sec=4000.0,
                          speech_intervals=[(3980.0, 4030.0)],
                          min_sec=40.0, max_sec=120.0)
    assert [F.problem_code(r) for r in got] == [F.CODE_BEYOND_SOURCE]


def test_hard_source_tolerance_absorbs_boundary_rounding():
    """끝이 소수점에서 살짝 넘치는 정상 케이스(실측 0~0.2s)는 통과시킨다."""
    cand = _cand("c01", [(3950, 4000.2)])
    assert F.hard_problems(cand, exception_sectors={}, source_duration_sec=4000.0,
                           speech_intervals=[(3950.0, 4000.2)],
                           min_sec=40.0, max_sec=120.0) == []


def test_hard_low_speech_coverage():
    cand = _cand("c01", [(100, 150)])          # 50초 중 10초만 발화 → 0.2
    got = F.hard_problems(cand, exception_sectors={}, source_duration_sec=4000.0,
                          speech_intervals=[(100.0, 110.0)],
                          min_sec=40.0, max_sec=120.0)
    assert [F.problem_code(r) for r in got] == [F.CODE_LOW_SPEECH_COVERAGE]
    assert "0.200" in got[0]


def test_hard_no_transcript_means_no_coverage_verdict():
    """전사가 없으면 커버리지를 **판정하지 않는다**(모르는 것을 틀렸다고 하지 않는다)."""
    cand = _cand("c01", [(100, 150)])
    assert F.hard_problems(cand, exception_sectors={}, source_duration_sec=4000.0,
                           speech_intervals=[], min_sec=40.0, max_sec=120.0) == []


def test_hard_length_violations():
    short = F.hard_problems(_cand("c01", [(100, 130)]), exception_sectors={},
                            source_duration_sec=4000.0, speech_intervals=[(100.0, 130.0)],
                            min_sec=40.0, max_sec=120.0)
    assert [F.problem_code(r) for r in short] == [F.CODE_TOO_SHORT]
    long = F.hard_problems(_cand("c01", [(100, 300)]), exception_sectors={},
                           source_duration_sec=4000.0, speech_intervals=[(100.0, 300.0)],
                           min_sec=40.0, max_sec=120.0)
    assert [F.problem_code(r) for r in long] == [F.CODE_TOO_LONG]


def test_hard_empty_segments_is_a_fact():
    got = F.hard_problems({"id": "c01", "segments": []}, **{
        k: BASE[k] for k in ("exception_sectors", "source_duration_sec",
                             "speech_intervals", "min_sec", "max_sec")})
    assert [F.problem_code(r) for r in got] == [F.CODE_NO_SEGMENTS]


def test_hard_is_pure():
    cand = _cand("c01", [(300, 250)])          # 뒤집힌 조각
    before = copy.deepcopy(cand)
    got = F.hard_problems(cand, exception_sectors={}, source_duration_sec=4000.0,
                          speech_intervals=[], min_sec=40.0, max_sec=120.0)
    assert F.CODE_INVALID_SEGMENT in [F.problem_code(r) for r in got]
    assert cand == before


# ── 발화 커버리지가 plausible 필터를 지난다 (E20-B4) ────────────────────────

def test_coverage_goes_through_plausible_speech_intervals():
    """환각성 전사(40초·1글자)는 발화로 안 친다 — 안 거르면 커버리지 1.0 으로 통과한다.

    실측 유래: 40.5초짜리 "누가 보냈어?" 한 줄이 hook 전체를 '발화 있음'으로 위장해
    페이싱·커버리지 판정을 전부 오판시켰다(E20-B4)."""
    cand = _cand("c01", [(100, 140)])
    intervals = [_iv(100, 140, "짧"),                  # 40초/1자 = 0.025자/초 → 환각
                 _iv(100, 105, "정상적인 대사 문장입니다")]  # 5초/13자 → 진짜 발화
    got = F.hard_problems(cand, exception_sectors={}, source_duration_sec=4000.0,
                          speech_intervals=intervals, min_sec=40.0, max_sec=120.0)
    assert [F.problem_code(r) for r in got] == [F.CODE_LOW_SPEECH_COVERAGE]
    assert "0.125" in got[0]                       # 5/40 — 필터 없으면 1.000 이다

    kept, rec = F.run_funnel([cand], **{**BASE, "speech_intervals": intervals})
    assert kept == [] and rec["speech_filtered"] == 1   # 몇 건을 걸렀는지 남긴다


def test_plausible_intervals_keeps_textless_tuples():
    """텍스트가 없으면 밀도를 알 수 없다 — 거르지 않는다(오판 금지)."""
    ivs, dropped = F.plausible_intervals([(0.0, 40.0)])
    assert dropped == 0 and len(ivs) == 1
    assert F.plausible_intervals([]) == (None, 0)


# ── 소프트 신호 — 값만 ─────────────────────────────────────────────────────

def test_soft_signal_values_hand_calc():
    cand = _cand("c01", [(100, 120), (200, 215)])
    sig = F.soft_signals(cand, scene_cuts=[105.0, 210.0],
                         speech_intervals=[_iv(100, 110, "어쩌고 저쩌고 대사입니다"),
                                           _iv(205, 215, "또 다른 대사 문장입니다")],
                         words=_words(103.0, 108.0, 206.0))
    assert sig["segment_count"] == 2
    assert sig["cohesion_gap_sec"] == pytest.approx(80.0)   # 200 − 120
    assert sig["stall_max_gap_sec"] == pytest.approx(15.0)  # 105~120
    assert sig["speech_coverage"] == pytest.approx(20 / 35, abs=1e-4)
    assert sig["silent_max_run_sec"] == pytest.approx(10.0)  # 110~120
    assert sig["cut_mid_sentence"] == 0                      # 경계가 구간 끝에 맞닿음
    assert sig["lead_in_sec"] == pytest.approx(3.0)          # 100 → 첫 단어 103


def test_soft_stall_and_silent_run_are_separate_axes():
    """대화 고정샷: 컷이 없어 정지 화면 축은 최악인데 발화 0 run 은 0 이다.

    액션 몽타주는 정반대다. 한 축으로 합치면 두 경우 다 틀린다 — 그래서 따로 기록한다."""
    talk = F.soft_signals(_cand("c01", [(0, 60)]), scene_cuts=[0.5],
                          speech_intervals=[(0.0, 60.0)], words=_words(0.1))
    assert talk["stall_max_gap_sec"] == pytest.approx(59.5)
    assert talk["silent_max_run_sec"] == pytest.approx(0.0)

    action = F.soft_signals(_cand("c02", [(0, 60)]),
                            scene_cuts=[10.0, 20.0, 30.0, 40.0, 50.0],
                            speech_intervals=[(0.0, 1.0)], words=_words(0.1))
    assert action["stall_max_gap_sec"] == pytest.approx(10.0)
    assert action["silent_max_run_sec"] == pytest.approx(59.0)


def test_soft_cut_mid_sentence_counts_inside_boundaries():
    sig = F.soft_signals(_cand("c01", [(100, 120), (200, 210)]), scene_cuts=[],
                         speech_intervals=[(95.0, 105.0), (205.0, 215.0)],
                         words=[])
    # 100 은 (95,105) 안 · 210 은 (205,215) 안 · 120·200 은 어느 구간에도 없다
    assert sig["cut_mid_sentence"] == 2


def test_soft_signals_are_none_without_basis():
    """근거가 없는 축은 None 이다 — 0 으로 두면 재료 없는 후보가 1위가 된다."""
    sig = F.soft_signals(_cand("c01", [(100, 140)]), scene_cuts=[],
                         speech_intervals=[], words=[])
    assert sig["stall_max_gap_sec"] is None
    assert sig["speech_coverage"] is None
    assert sig["silent_max_run_sec"] is None
    assert sig["cut_mid_sentence"] is None
    assert sig["lead_in_sec"] is None
    assert sig["segment_count"] == 1 and sig["cohesion_gap_sec"] == 0.0


def test_soft_lead_in_is_none_when_head_piece_has_no_plausible_word():
    """plausible 단어가 0인 조각은 신호를 내지 않는다(오판 금지).

    단어는 밀도 판정을 걸 수 없어 **환각 구간 안의 단어**가 함께 빠진다."""
    cand = _cand("c01", [(100, 140)])
    sig = F.soft_signals(cand, scene_cuts=[],
                         speech_intervals=[_iv(100, 140, "짧"),        # 환각 → 제거
                                           _iv(300, 310, "정상적인 대사 문장입니다")],
                         words=_words(105.0))                          # 환각 구간 안
    assert sig["lead_in_sec"] is None


def test_soft_signals_is_pure():
    cand = _cand("c01", [(200, 215), (100, 120)])   # 정렬 안 된 입력
    before = copy.deepcopy(cand)
    F.soft_signals(cand, scene_cuts=[105.0], speech_intervals=[(100.0, 110.0)],
                   words=_words(101.0))
    assert cand == before


# ── 점수 — 순위 전용 ───────────────────────────────────────────────────────

def test_score_is_lower_is_better():
    good = F.soft_signals(_cand("g", [(0, 40)]), scene_cuts=[10.0, 20.0, 30.0],
                          speech_intervals=[(0.0, 40.0)], words=_words(0.5))
    bad = F.soft_signals(_cand("b", [(0, 20), (500, 520)]), scene_cuts=[],
                         speech_intervals=[(0.0, 2.0)], words=_words(15.0))
    assert F.score(good) < F.score(bad)


def test_score_missing_axes_are_neutral_not_a_bonus():
    """결측 축이 0 감점이면 재료 없는 후보가 자동 1위가 된다 — 평균으로 채운다."""
    known = {"segment_count": 1, "speech_coverage": 0.5, "stall_max_gap_sec": 6.0,
             "silent_max_run_sec": 6.0, "cut_mid_sentence": 1,
             "cohesion_gap_sec": 0.0, "lead_in_sec": 5.0}
    partial = dict(known, stall_max_gap_sec=None, silent_max_run_sec=None,
                   lead_in_sec=None)
    naive_zero = dict(known, stall_max_gap_sec=0.0, silent_max_run_sec=0.0,
                      lead_in_sec=0.0)          # 결측을 '무감점'으로 읽었을 때
    assert F.score(known) == pytest.approx(3.75)
    assert F.score(partial) == pytest.approx(3.2143)
    assert F.score(naive_zero) == pytest.approx(2.25)
    assert F.score(naive_zero) < F.score(partial) < F.score(known)


def test_every_weighted_axis_has_a_penalty_rule():
    """가중치만 더하고 감점 규칙을 안 더하면 그 축이 조용히 무시된다."""
    base = {"segment_count": 4, "speech_coverage": 0.5, "stall_max_gap_sec": 6.0,
            "silent_max_run_sec": 6.0, "cut_mid_sentence": 2,
            "cohesion_gap_sec": 100.0, "lead_in_sec": 5.0}
    for key in F.DEFAULT_WEIGHTS:
        assert key in base, f"{key} 의 감점 규칙·픽스처가 없다"
        one = {k: (v if k in (key, "segment_count") else None)
               for k, v in base.items()}
        assert F.score(one, weights={key: F.DEFAULT_WEIGHTS[key]}) > 0.0, key


def test_score_all_unknown_is_zero_ties():
    empty = {"segment_count": 0, "speech_coverage": None, "stall_max_gap_sec": None,
             "silent_max_run_sec": None, "cut_mid_sentence": None,
             "cohesion_gap_sec": None, "lead_in_sec": None}
    assert F.score(empty) == 0.0


def test_score_unknown_weight_key_fails_loud():
    with pytest.raises(ValueError):
        F.score({"segment_count": 1}, weights={"viral": 1.0})


def test_score_weights_follow_v3_rubric_spirit():
    """재료 신뢰도(=발화 커버리지) 3.0 · 응집도 1.5 — v3 RUBRIC_WEIGHTS 와 같은 자.

    ⚠ v3 값은 **AST 로 읽는다** — `app.v3.story` 를 import 하면 M1 이 이동 중인
    `app.v3.schemas` 체인을 함께 끌고 와 이 테스트가 남의 작업에 물린다."""
    RUBRIC_WEIGHTS = _v3_rubric_weights()
    assert F.DEFAULT_WEIGHTS["speech_coverage"] == RUBRIC_WEIGHTS["material"]
    assert F.DEFAULT_WEIGHTS["cohesion_gap_sec"] == RUBRIC_WEIGHTS["cohesion"]
    assert F.DEFAULT_WEIGHTS["stall_max_gap_sec"] == RUBRIC_WEIGHTS["progression"]
    assert F.DEFAULT_WEIGHTS["lead_in_sec"] == RUBRIC_WEIGHTS["intro"]


# ── run_funnel ─────────────────────────────────────────────────────────────

def test_funnel_never_drops_on_score_alone():
    """점수 임계 탈락은 없다(M9 원칙) — 최악의 소프트 신호도 하드만 통과하면 남는다."""
    cand = _cand("c01", [(0, 20), (3000, 3030)])   # 점프 3000s · 컷 없음 · 조각 흩어짐
    kept, rec = F.run_funnel([cand], **{**BASE, "speech_intervals": [(0.0, 20.0),
                                                                    (3000.0, 3030.0)]})
    assert [c["id"] for c in kept] == ["c01"] and rec["dropped"] == []
    assert F.score(rec["signals"]["c01"]) > 0.0


def test_funnel_dedup_keeps_the_better_score():
    """겹치면 하나만 — 남는 쪽은 **점수가 나은 쪽**이다(정렬이 먼저 돌기 때문)."""
    a = _cand("c01", [(100, 140)])      # 커버리지 1.0
    b = _cand("c02", [(100, 150)])      # 커버리지 0.8
    kept, rec = F.run_funnel([b, a], **{**BASE, "speech_intervals": [(100.0, 140.0)]})
    assert [c["id"] for c in kept] == ["c01"]
    assert rec["dedup"] == [{"kept": "c01", "dropped": "c02",
                             "iou": pytest.approx(0.8)}]
    assert [d["id"] for d in rec["dropped"]] == ["c02"]
    assert F.problem_code(rec["dropped"][0]["reasons"][0]) == F.CODE_DUPLICATE


def test_funnel_keeps_non_overlapping_below_threshold():
    a = _cand("c01", [(100, 145)])
    b = _cand("c02", [(140, 190)])      # 교집합 5 / 합집합 90 → 0.056 < 0.5
    kept, rec = F.run_funnel([a, b], **{**BASE,
                                        "speech_intervals": [(100.0, 190.0)]})
    assert sorted(c["id"] for c in kept) == ["c01", "c02"] and rec["dedup"] == []


def test_funnel_keep_cap_records_what_it_cut():
    cands = [_cand(f"c{i:02d}", [(i * 200, i * 200 + 50)]) for i in range(1, 11)]
    ivs = [(i * 200, i * 200 + 50) for i in range(1, 11)]
    kept, rec = F.run_funnel(cands, **{**BASE, "source_duration_sec": 4000.0,
                                       "speech_intervals": ivs}, keep=3)
    assert len(kept) == 3 and rec["keep_cap"] == 3 and rec["capped"] == 7
    cap_dropped = [d for d in rec["dropped"]
                   if F.problem_code(d["reasons"][0]) == F.CODE_KEEP_CAP]
    assert len(cap_dropped) == 7
    assert "순위 4위" in cap_dropped[0]["reasons"][0]


def test_funnel_ties_break_by_source_time_not_input_order():
    """동점은 소스 시각순 — 6단계 나열 순서로 깨면 위치 편향이 M9 뒷문으로 샌다."""
    late = _cand("z_late", [(500, 540)])
    early = _cand("a_early", [(100, 140)])
    ivs = [(100.0, 140.0), (500.0, 540.0)]
    kept, _ = F.run_funnel([late, early], **{**BASE, "scene_cuts": [110.0, 510.0],
                                             "speech_intervals": ivs,
                                             "words": _words(103.0, 503.0)})
    assert [c["id"] for c in kept] == ["a_early", "z_late"]


def test_funnel_is_deterministic_under_input_shuffle():
    cands = [_cand(f"c{i:02d}", [(i * 300, i * 300 + 45 + i)]) for i in range(1, 8)]
    ivs = [(i * 300, i * 300 + 40) for i in range(1, 8)]
    kw = {**BASE, "speech_intervals": ivs, "scene_cuts": [i * 300 + 5 for i in range(1, 8)],
          "words": _words(*[i * 300 + 1 for i in range(1, 8)])}
    first, rec1 = F.run_funnel(cands, **kw)
    second, rec2 = F.run_funnel(list(reversed(cands)), **kw)
    assert [c["id"] for c in first] == [c["id"] for c in second]
    assert rec1["kept"] == rec2["kept"] and rec1["scores"] == rec2["scores"]


def test_funnel_every_candidate_is_accounted_for():
    """조용한 증발 금지 — 모든 후보는 kept 나 dropped 중 정확히 한 곳에 한 번 나온다."""
    cands = [_cand("c01", [(100, 140)]),          # 통과
             _cand("c02", [(100, 150)]),          # dedup 탈락
             _cand("c03", [(3990, 4050)]),        # 소스 밖
             _cand("c04", [(100, 120)])]          # 길이 미달
    kept, rec = F.run_funnel(cands, **{**BASE, "speech_intervals": [(100.0, 140.0),
                                                                   (3990.0, 4050.0)]})
    seen = rec["kept"] + [d["id"] for d in rec["dropped"]]
    assert sorted(seen) == ["c01", "c02", "c03", "c04"]
    assert len(seen) == len(set(seen)) and rec["of"] == 4
    assert [c["id"] for c in kept] == rec["kept"]


def test_funnel_records_intro_overlap_without_dropping():
    cand = _cand("c01", [(20, 80)])
    kept, rec = F.run_funnel([cand], **{**BASE,
                                        "exception_sectors": {"intro": {"start_sec": 0.0,
                                                                        "end_sec": 43.0}},
                                        "speech_intervals": [(20.0, 80.0)]})
    assert [c["id"] for c in kept] == ["c01"]
    assert rec["intro_overlap"] == [{"id": "c01", "overlap_sec": 23.0}]


def test_funnel_duplicate_ids_fail_loud():
    with pytest.raises(ValueError):
        F.run_funnel([_cand("c01", [(100, 150)]), _cand("c01", [(900, 950)])], **BASE)


def test_funnel_is_pure():
    cands = [_cand("c01", [(100, 140)]), _cand("c02", [(100, 150)])]
    before = copy.deepcopy(cands)
    kept, _ = F.run_funnel(cands, **{**BASE, "speech_intervals": [(100.0, 140.0)]})
    assert cands == before
    assert kept[0] is cands[0]        # 사본이 아니라 넘겨받은 후보를 그대로 돌려준다


def test_funnel_empty_input():
    kept, rec = F.run_funnel([], **BASE)
    assert kept == [] and rec["kept"] == [] and rec["dropped"] == []
    assert rec["keep_cap"] == F.FUNNEL_KEEP


def test_contract_constants_are_fixed():
    """계약 §5 의 값 — 바뀌면 후보 통과율이 통째로 달라진다."""
    assert F.FUNNEL_KEEP == 8
    assert F.IOU_DEDUP == 0.5
    assert F.MIN_SPEECH_COVERAGE == 0.55
    assert F.STALL_MAX_GAP_SEC == 12.0
    assert F.TAIL_SECTORS == ("teaser", "credit")
