"""시청 트림 패스 회귀 가드 (2026-09-02) — LLM 없이 순수 로직만.

계약: 모델 제안은 초안 시계 · 코드가 안쪽 축소 스냅(제거분 ⊆ 제안분) · 하드 가드
(내레이션 창·imp≥4 대사·엔딩·총량 상한) · 빼기 전용 · fail-soft.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.v3 import watch_trim as wt

GRID = {"span_candidates": [
    {"id": "sp0000", "t_in": 10.0, "t_out": 12.0, "is_audio": True},
    {"id": "sp0001", "t_in": 12.0, "t_out": 15.0, "is_audio": False},
    {"id": "sp0002", "t_in": 15.0, "t_out": 17.0, "is_audio": True},
    {"id": "sp0003", "t_in": 30.0, "t_out": 34.0, "is_audio": True},
]}
# 클립0: 10~17(3 span) → 편집본 0~7 · 클립1: 30~34 → 7~11
TL = [{"role": "build", "clip_start_sec": 10.0, "clip_end_sec": 17.0,
       "span_ids": ["sp0000", "sp0001", "sp0002"], "subtitle": "(라벨)",
       "use_original_audio": True, "reframe": {"mode": "center"}},
      {"role": "ending", "clip_start_sec": 30.0, "clip_end_sec": 34.0,
       "span_ids": ["sp0003"], "subtitle": "",
       "use_original_audio": True, "reframe": {"mode": "center"}}]


def test_edited_span_edges():
    edges = wt.edited_span_edges(TL, GRID)
    assert edges == [0.0, 2.0, 5.0, 7.0, 11.0]                 # 내부 span 경계 포함


def test_snap_inward_shrinks_and_drops_tiny():
    edges = [0.0, 2.0, 5.0, 7.0]
    assert wt.snap_inward(1.4, 5.6, edges) == (2.0, 5.0)       # 제거분 ⊆ 제안분
    assert wt.snap_inward(2.1, 2.4, edges) is None             # 실익 없음


def test_validate_cuts_guards_and_cap():
    edges = wt.edited_span_edges(TL, GRID)
    res = {"tts_cue_files": [{"cue": {"start_sec": 0.5, "end_sec": 1.8}}]}
    imp = {"sp0000": (True, 5), "sp0001": (False, 3),
           "sp0002": (True, 3), "sp0003": (True, 5)}
    guards = wt.protected_intervals(TL, res, imp, GRID)
    # 내레이션 창·imp5 대사(sp0000 → 0~2)·ending 클립(7~11)이 보호에 있다
    assert any("내레이션" in g[2] for g in guards)
    assert any("핵심 대사" in g[2] for g in guards)
    assert any("ending" in g[2] for g in guards)
    applied, rejected = wt.validate_cuts(
        [{"start_sec": 0.4, "end_sec": 1.9, "reason": "x"},     # 내레이션 창 → 거부
         {"start_sec": 8.0, "end_sec": 10.0, "reason": "x"},    # ending → 거부
         {"start_sec": 1.4, "end_sec": 5.3, "reason": "무음"}], # 보호 차감→2.0~5.0
        duration=20.0, edges=edges, guards=guards)
    assert len(rejected) == 2
    assert [r["reason"][:6] for r in rejected] == ["보호 구간이", "보호 구간이"]
    assert len(applied) == 1
    assert (applied[0]["start"], applied[0]["end"]) == (2.0, 5.0)


def test_validate_cuts_snap_is_inward_only():
    edges = [0.0, 2.0, 5.0, 7.0, 11.0]
    applied, _ = wt.validate_cuts([{"start_sec": 1.9, "end_sec": 5.1}],
                                  duration=20.0, edges=edges, guards=[])
    assert (applied[0]["start"], applied[0]["end"]) == (2.0, 5.0)  # 안쪽 축소
    # free_zones 없이(종전 계약): span 하나를 다 못 덮는 제안은 실익 없음 → 드랍
    applied2, rejected2 = wt.validate_cuts([{"start_sec": 2.3, "end_sec": 4.6}],
                                           duration=11.0, edges=edges, guards=[])
    assert applied2 == [] and "실익" in rejected2[0]["reason"]
    # 무대사 조각(sp0001 → 2~5) 안이면 제안 시각 그대로(2026-09-03 — 4.0s 제안이
    # 0.59s 로 쪼그라들던 실사고). 대사 조각 안이면 여전히 금지.
    # (duration 60 = 총량 상한 12s — 상한이 아니라 스냅을 보는 테스트)
    applied3, _ = wt.validate_cuts([{"start_sec": 2.3, "end_sec": 4.6}], duration=60.0,
                                   edges=edges, guards=[], free_zones=[(2.0, 5.0)])
    assert (applied3[0]["start"], applied3[0]["end"], applied3[0].get("free")) == (2.3, 4.6, True)
    applied4, rejected4 = wt.validate_cuts([{"start_sec": 0.3, "end_sec": 1.6}], duration=60.0,
                                           edges=edges, guards=[], free_zones=[(2.0, 5.0)])
    assert applied4 == [] and "실익" in rejected4[0]["reason"]


def test_validate_cuts_total_cap():
    edges = [0.0, 2.0, 5.0, 7.0, 11.0]
    applied, rejected = wt.validate_cuts(
        [{"start_sec": 0.0, "end_sec": 2.0},
         {"start_sec": 5.0, "end_sec": 7.0}],
        duration=11.0, edges=edges, guards=[])                  # 상한 2.2s
    assert len(applied) == 1 and any("상한" in r["reason"] for r in rejected)


def test_apply_cuts_splits_clip_and_reassigns_spans():
    cuts = [{"start": 2.0, "end": 5.0}]                        # sp0001(무성) 제거
    out = wt.apply_cuts_to_timeline(TL, cuts, GRID, label_anchors={"sp0000"})
    assert len(out) == 3                                       # 클립0 → 두 조각
    p0, p1 = out[0], out[1]
    assert (p0["clip_start_sec"], p0["clip_end_sec"]) == (10.0, 12.0)
    assert (p1["clip_start_sec"], p1["clip_end_sec"]) == (15.0, 17.0)
    assert p0["span_ids"] == ["sp0000"] and p1["span_ids"] == ["sp0002"]
    assert p0["subtitle"] == "(라벨)" and p1["subtitle"] == ""  # 앵커 조각만 유지
    assert out[2]["clip_start_sec"] == 30.0                    # 다른 클립 불변


def test_remap_edited_and_segments_and_resources():
    cuts = [{"start": 2.0, "end": 5.0}]
    assert wt.remap_edited(1.0, cuts) == 1.0
    assert wt.remap_edited(6.0, cuts) == 3.0
    assert wt.remap_edited(3.0, cuts) == 2.0                   # 구간 안 → 시작에 붙음
    segs = [{"start_sec": 0.2, "end_sec": 1.8, "text": "a"},
            {"start_sec": 2.5, "end_sec": 4.5, "text": "b"},   # 통째 제거 → 드랍
            {"start_sec": 5.0, "end_sec": 7.0, "text": "c"}]
    out = wt.remap_segments(segs, cuts, new_total=8.0)
    assert [s["text"] for s in out] == ["a", "c"]
    assert out[1]["start_sec"] == 2.0 and out[1]["end_sec"] == 4.0
    res = {"tts_cue_files": [{"cue": {"start_sec": 5.5, "end_sec": 7.0,
                                      "duration_sec": 1.5, "text": "n"}}]}
    r2 = wt.remap_resources(res, cuts, new_total=8.0)
    cue = r2["tts_cue_files"][0]["cue"]
    assert cue["start_sec"] == 2.5 and cue["end_sec"] == 4.0
    assert res["tts_cue_files"][0]["cue"]["start_sec"] == 5.5  # 원본 불변(순수)


def test_apply_drops_head_trim_mark_on_shifted_piece():
    tl = [dict(TL[0], head_trimmed=True)]
    out = wt.apply_cuts_to_timeline(tl, [{"start": 2.0, "end": 5.0}], GRID, set())
    assert out[0].get("head_trimmed") is True                  # 시작 불변 조각은 유지
    assert "head_trimmed" not in out[1]                        # 옮겨진 조각은 무효


def test_pipeline_wires_watch_trim_between_draft_and_style():
    import app.v3.pipeline as pl
    src = Path(pl.__file__).read_text("utf-8")
    assert "run_watch_trim" in src
    assert src.index("watch_trim (11.5)") < src.index("style (12)")
    assert src.index("draft_render (11)") < src.index("watch_trim (11.5)")


def test_snap_tolerance_catches_rounded_boundaries():
    """모델은 0.1s 단위로 말한다 — 15.664 경계를 15.7 로 불러도 잡혀야 한다
    (실사고: 정확한 지목이 '실익 없음'으로 거부됐다)."""
    edges = [0.0, 15.664, 17.71, 42.5]
    assert wt.snap_inward(15.7, 17.7, edges) == (15.664, 17.71)
    # 확장 상한은 SNAP_TOL_SEC — 그 밖 경계로는 안 넘어간다
    assert wt.snap_inward(16.1, 17.2, edges) is None


# ── 예산 컷 (2026-09-02 사용자 결정: 길이 초과 처리를 watch-trim 으로) ──────

GRID_B = {"span_candidates": [
    {"id": "s0", "t_in": 10.0, "t_out": 13.0, "is_audio": False},
    {"id": "s1", "t_in": 13.0, "t_out": 15.0, "is_audio": True},
    {"id": "s2", "t_in": 15.0, "t_out": 17.0, "is_audio": True},
    {"id": "s3", "t_in": 30.0, "t_out": 34.0, "is_audio": True},
]}
TL_B = [{"role": "build", "clip_start_sec": 10.0, "clip_end_sec": 17.0,
         "span_ids": ["s0", "s1", "s2"], "subtitle": "",
         "use_original_audio": True},
        {"role": "ending", "clip_start_sec": 30.0, "clip_end_sec": 34.0,
         "span_ids": ["s3"], "subtitle": "", "use_original_audio": True}]
SIDX_B = {"s0": {"importance": 2, "is_audio": False},
          "s1": {"importance": 5, "is_audio": True},
          "s2": {"importance": 3, "is_audio": True},
          "s3": {"importance": 4, "is_audio": True}}


def _belt_env():
    imp = {"s1": (True, 5), "s3": (True, 4)}
    guards = wt.protected_intervals(TL_B, {}, imp, GRID_B)
    edges = wt.edited_span_edges(TL_B, GRID_B)
    return guards, edges


def test_budget_fallback_priority_and_stop():
    """중요도 낮은 가장자리 span 부터, deficit 을 채우면 멈춘다(overshoot 허용)."""
    guards, edges = _belt_env()
    cuts = wt.budget_fallback_cuts(TL_B, GRID_B, SIDX_B, 2.5, guards, edges, [])
    assert [round(c["start"], 1) for c in cuts] == [0.0]      # s0(imp2) 하나로 충족
    assert cuts[0]["end"] == pytest.approx(3.0)
    assert cuts[0]["belt"] is True and "s0" in cuts[0]["reason"]
    # 엔딩·마지막 클립(s3)·imp5(s1)는 애초에 후보가 아니다
    big = wt.budget_fallback_cuts(TL_B, GRID_B, SIDX_B, 99.0, guards, edges, [])
    assert all("s1" not in c["reason"] and "s3" not in c["reason"] for c in big)


def test_budget_fallback_pair_protection_and_climax_skip():
    """↪ 짝이 타임라인에 남으면 후보 제외 · climax 클립은 통째 제외."""
    guards, edges = _belt_env()
    sidx = {**SIDX_B, "s0": {**SIDX_B["s0"], "continues_to": "s1"}}
    cuts = wt.budget_fallback_cuts(TL_B, GRID_B, sidx, 2.5, guards, edges, [])
    assert all("s0" not in c["reason"] for c in cuts)          # 반토막 재발 방지
    tl2 = [dict(TL_B[0], role="climax"), TL_B[1]]
    assert wt.budget_fallback_cuts(tl2, GRID_B, SIDX_B, 2.5, guards, edges, []) == []


def test_budget_prompt_gate():
    """deficit 없으면 프롬프트는 종전과 동일 — 예산 절은 초과 판에만 실린다."""
    quality = wt.PROMPT.format(max_cuts=wt.MAX_CUTS, budget_block="",
                               material_block="m")
    assert "예산" not in quality and "반드시" not in quality
    block = wt.BUDGET_BLOCK.format(deficit=7.3)
    assert "7.3초" in block and "빈 배열이 정답이 아니다" in block


def test_run_watch_trim_belt_survives_model_failure():
    """호출 실패도 예산 벨트는 돈다 — 상한 초과 발행 금지(8/24 실사고)."""
    imp = {"s1": (True, 5), "s3": (True, 4)}
    cuts, audit = wt.run_watch_trim(
        object(), Path("/nonexistent.mp4"), timeline=TL_B, grid=GRID_B,
        resources={}, segments=[], importance=imp, span_index=SIDX_B,
        budget_deficit=2.5, log=lambda *a: None)
    assert audit.get("error")
    assert [round(c["start"], 1) for c in cuts] == [0.0]       # 벨트가 s0 을 던다
    assert audit["budget"]["belt_sec"] == pytest.approx(3.0)
    assert audit["budget"]["unmet"] is False
    # 품질 판(무 deficit)의 실패는 종전 그대로 트림 0
    cuts2, audit2 = wt.run_watch_trim(
        object(), Path("/nonexistent.mp4"), timeline=TL_B, grid=GRID_B,
        resources={}, segments=[], importance=imp, log=lambda *a: None)
    assert cuts2 == [] and "budget" not in audit2


def test_free_zones_and_sparse_silent_cut_is_kept():
    """실사고 모양: 3.8초 무대사 조각 하나 안의 4초 제안 — 안쪽 스냅은 0.59s(60% 미만)라
    제안 시각을 그대로 쓴다. 눈금이 촘촘하면 종전대로 안쪽 스냅."""
    grid = {"span_candidates": [
        {"id": "a", "t_in": 100.0, "t_out": 103.08, "is_audio": False},
        {"id": "b", "t_in": 103.08, "t_out": 103.67, "is_audio": False},
        {"id": "c", "t_in": 103.67, "t_out": 107.47, "is_audio": False},
        {"id": "d", "t_in": 107.47, "t_out": 109.0, "is_audio": True}]}
    tl = [{"clip_start_sec": 100.0, "clip_end_sec": 109.0, "span_ids": ["a", "b", "c", "d"],
           "use_original_audio": True}]
    edges = wt.edited_span_edges(tl, grid)
    zones = wt.free_zones(tl, grid)
    assert zones == [(0.0, 3.08), (3.08, 3.67), (3.67, 7.47)]
    applied, _ = wt.validate_cuts([{"start_sec": 1.0, "end_sec": 5.0}], duration=60.0,
                                  edges=edges, guards=[], free_zones=zones)
    assert (applied[0]["start"], applied[0]["end"]) == (1.0, 5.0) and applied[0]["free"]
    # 끝이 대사 조각(d: 7.47~9.0)에 걸치면 그 끝만 눈금(7.47)으로
    applied2, _ = wt.validate_cuts([{"start_sec": 4.0, "end_sec": 8.2}], duration=60.0,
                                   edges=edges, guards=[], free_zones=zones)
    assert (applied2[0]["start"], applied2[0]["end"]) == (4.0, 7.47)
    # 조각 경계가 눈금에 없으면 벨트용 표시가 붙고, 벨트가 100% 로 인정한다
    new = wt.apply_cuts_to_timeline(tl, applied, grid, set())
    assert [(c["clip_start_sec"], c["clip_end_sec"]) for c in new] == [(100.0, 101.0), (105.0, 109.0)]
    assert new[0].get("tail_trim") is True and new[1].get("head_trimmed") is True
    from app.v3 import assemble
    belt = assemble.verify_edit_plan({"timeline": new}, grid)
    assert belt["pct"] == 100.0 and belt["violations"] == []
