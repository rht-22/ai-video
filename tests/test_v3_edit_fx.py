"""Stage 4 편집 연출 3종(2026-09-08, 사용자 지시: 규칙이 아니라 LLM 이 스타일 단계에서 정한다) 회귀 가드.

강조 자막(emphasis) · 계단식 줌(zooms) · 정보 화면 전체 맞춤(fits) — build12 이식.
고정하는 것: 검증기(항목 단위 드롭·범위 클램프·상한·id 표) · 줌 분할(프레임 격자·총 길이 불변) ·
줌 크롭 수식 · 강조 style 매핑 · 렌더러 fit 분기 · **없으면 종전과 바이트 동일**(회귀 0).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DesignConfig
from app.modules.renderer import RenderInputs, _build_filtergraph
from app.modules.story_builder import StoryClip
from app.v3 import finalize, stage4

CLIPS = [{"clip": 0, "start": 0.0, "end": 4.0}, {"clip": 1, "start": 4.0, "end": 9.0},
         {"clip": 2, "start": 9.0, "end": 9.4}]
EVENTS = [{"id": "L0", "kind": "line", "start": 0.5, "end": 2.0, "text": "너 바람피니?"},
          {"id": "L1", "kind": "line", "start": 5.0, "end": 6.0, "text": "아니야"}]


def _val(resp):
    styled, pr, notes = stage4.validate_style_response(
        {"design": {}, "beats": [], "labels": [], **resp}, 0, band=(0.2, 0.8),
        duration=9.4, clips=CLIPS, events=EVENTS)
    return styled, pr, notes


def test_validate_emphasis_zoom_fit_accept_clamp_drop():
    ok, pr, notes = _val({
        "emphasis": [{"line": "L0", "color": "red", "scale": 1.3}, {"line": "l1", "scale": 9.0},
                     {"line": "L7"}, {"line": "L0", "color": "yellow"}],
        "zooms": [{"clip": "C1", "factor": 1.45, "anchor": "left", "from_sec": 0.9},
                  {"clip": "C2", "factor": 2.5, "from_sec": 0.2}, {"clip": "C9", "factor": 1.2}],
        "fits": [{"clip": "C0"}, {"clip": "C1"}, "C0"]})
    assert pr == []
    e = ok["emphasis"]
    assert [x["line"] for x in e] == ["L0", "L1"] and e[0]["color"] == "#FF5540" and e[0]["scale"] == 1.3
    assert e[1]["scale"] == stage4.EMPH_SCALE_RANGE[1] and e[1]["color"] == stage4.EMPH_DEFAULT_COLOR
    assert e[0]["index"] == 0 and e[0]["start_sec"] == 0.5 and e[0]["text"] == "너 바람피니?"
    z = ok["zooms"]
    assert z[0] == {"clip": 1, "factor": 1.45, "anchor": "left", "from_sec": 0.9,
                    "stages": [{"from_sec": 0.9, "factor": 1.45, "anchor": "left"}]}
    assert z[1]["factor"] == stage4.ZOOM_FACTOR_RANGE[1] and z[1]["from_sec"] == 0.0   # 0.4s 클립 → 통째
    assert ok["fits"] == [0]                                   # C1 은 줌과 겹쳐 드롭 · 중복 무시
    assert any("L7" in n for n in notes) and any("C9" in n for n in notes) and any("줌 우선" in n for n in notes)
    # 상한
    many = {"emphasis": [{"line": f"L{i}"} for i in range(2)] * 5}
    ok2, _, _ = _val(many)
    assert len(ok2["emphasis"]) == 2
    # 표가 없으면(구 호출) 전부 드롭 — 키는 늘 있다
    styled, pr, notes = stage4.validate_style_response(
        {"design": {}, "beats": [], "emphasis": [{"line": "L0"}], "zooms": [{"clip": "C0", "factor": 1.2}]}, 0,
        band=(0.2, 0.8))
    assert styled["emphasis"] == [] and styled["zooms"] == [] and styled["fits"] == []


def test_style_doc_carries_edit_fx_and_fallback_has_empty_lists(monkeypatch, tmp_path):
    calls = {"n": 0}
    def fake_call(g, d, p):
        calls["n"] += 1
        return {"design": {}, "beats": [], "labels": [],
                "emphasis": [{"line": "L0", "color": "red"}], "zooms": [{"clip": "C1", "factor": 1.3}],
                "fits": [{"clip": "C0"}]}
    monkeypatch.setattr(stage4, "_call_style_model", fake_call)
    tl = [{"clip_start_sec": 10.0, "clip_end_sec": 14.0, "span_ids": []},
          {"clip_start_sec": 20.0, "clip_end_sec": 25.0, "span_ids": []},
          {"clip_start_sec": 30.0, "clip_end_sec": 30.4, "span_ids": []}]
    dialogue = [{"start_sec": 0.5, "end_sec": 2.0, "text": "너 바람피니?"}, {"start_sec": 5.0, "end_sec": 6.0, "text": "아니야"}]
    doc, audit = stage4.run_style(object(), tmp_path / "d.mp4", {"beats": []}, timeline=tl, dialogue=dialogue,
                                  duration=9.4, log=lambda *a: None)
    v = doc["v3_style"]
    assert v["emphasis"][0]["index"] == 0 and v["zooms"][0]["clip"] == 1 and v["fits"] == [0]
    monkeypatch.setattr(stage4, "_call_style_model", lambda g, d, p: {"design": {"nope": 1}})
    doc2, audit2 = stage4.run_style(object(), tmp_path / "d.mp4", {"beats": []}, timeline=tl, dialogue=dialogue,
                                    duration=9.4, log=lambda *a: None)
    assert audit2["fallback"] and doc2["v3_style"]["emphasis"] == [] and doc2["v3_style"]["zooms"] == []


def test_apply_zoom_splits_frame_grid_and_total_unchanged():
    tl = [{"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 14.0, "use_original_audio": True, "span_ids": [], "subtitle": "x"},
          {"role": "build", "clip_start_sec": 20.0, "clip_end_sec": 25.0, "use_original_audio": False, "cover": "designated", "hold_sec": 0.5, "span_ids": []}]
    fps = 24000 / 1001
    out, zmap, p2r = finalize.apply_zoom_splits(tl, [{"clip": 0, "factor": 1.4, "anchor": "center", "from_sec": 0.9},
                                                     {"clip": 1, "factor": 1.2, "anchor": "left", "from_sec": 0.0}], fps)
    assert len(out) == 3 and p2r == {0: [0, 1], 1: [2]} and set(zmap) == {1, 2}
    cut = round(round(0.9 * fps) / fps, 3)
    assert out[0]["clip_end_sec"] == pytest.approx(10.0 + cut) and out[1]["clip_start_sec"] == pytest.approx(10.0 + cut)
    assert out[1].get("zoom_part") is True and "subtitle" not in out[1] and out[0]["subtitle"] == "x"
    assert "hold_sec" not in out[0] and out[2]["hold_sec"] == 0.5
    total = sum(float(c["clip_end_sec"]) - float(c["clip_start_sec"]) for c in out)
    assert total == pytest.approx(9.0)
    # 줌 없음 = plan 과 동일(사본)
    same, zm, p = finalize.apply_zoom_splits(tl, [], fps)
    assert same == tl and zm == {} and p == {0: [0], 1: [1]}
    # 남는 조각이 짧으면 통째 줌
    whole, zm2, _ = finalize.apply_zoom_splits(tl, [{"clip": 0, "factor": 1.4, "anchor": "center", "from_sec": 3.9}], None)
    assert len(whole) == 2 and 0 in zm2


def test_zoom_crop_rows_and_emphasis_styles():
    geo = (1000, 960, 0, 60, 1920, 960)          # 24:23 · 레터박스 그림
    rows = finalize.zoom_crop_rows(None, 1.45, "right", geo)
    assert rows[0]["crop_w"] == 688 and rows[0]["crop_h"] == 662
    assert rows[0]["x_center"] == 1440.0 and rows[0]["y_center"] == 540.0
    base = [{"time_sec": 0.0, "x_center": 100.0, "y_center": 100.0, "crop_w": 1000, "crop_h": 960}]
    rows2 = finalize.zoom_crop_rows(base, 2.0, "center", geo)
    assert (rows2[0]["crop_w"], rows2[0]["crop_h"]) == (500, 480)
    assert rows2[0]["x_center"] == 250.0 and rows2[0]["y_center"] == 300.0     # 그림 안으로 클램프
    em = finalize.emphasis_styles({"emphasis": [{"index": 3, "scale": 1.23, "color": "#FF3E3E"}, {"index": "x"}]}, 62)
    assert em == {3: {"size": 76, "color": "#FF3E3E", "fx": "pop_snap_strong"}}
    assert finalize.emphasis_styles(None, 62) == {}


def _inputs(clips):
    return RenderInputs(video_path=Path("src.mp4"), clips=clips, subtitle_path=None, crop_timeline_map={},
                        title_text="제목", work_title="w", output_path=Path("out.mp4"), canvas_width=1080,
                        canvas_height=1920, top_title_height=250, bottom_label_height=170, design=DesignConfig())


def test_renderer_fit_branch_and_default_unchanged():
    plain = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="", use_original_audio=True)
    fit = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="", use_original_audio=True,
                    fit_picture=(0, 60, 1920, 960))
    g0 = _build_filtergraph(_inputs([plain]), 1, 0)
    g1 = _build_filtergraph(_inputs([fit]), 1, 0)
    assert "boxblur" not in g0 and "split=2" not in g0
    assert "[0:v]crop=1920:960:0:60,split=2[bg0][fg0]" in g1 and "boxblur=28:2" in g1
    assert "force_original_aspect_ratio=decrease[f0]" in g1 and "overlay=(W-w)/2:(H-h)/2" in g1
    # fit 이 None 인 클립은 종전 필터 문자열과 완전히 같다
    assert g0 == _build_filtergraph(_inputs([StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="",
                                                       use_original_audio=True, fit_picture=None)]), 1, 0)


def test_style_prompt_mentions_edit_fx_tasks():
    p = stage4.build_style_prompt(stage4.RECAP_PRESET, {"beats": []})
    for k in ("`emphasis`", "`zooms`", "`fits`", "강조 자막", "줌인", "정보 화면 전체 맞춤"):
        assert k in p
    assert f"{stage4.ZOOM_FACTOR_RANGE[1]:.1f}" in p and str(stage4.EMPH_MAX_COUNT) in p


# ── 무관한 인서트(2026-09-08, EP01 42~46s 벽 파쇄 실사고) ─────────────────────

def test_watch_trim_material_marks_silent_inserts_and_prompt_criterion():
    from app.v3 import watch_trim as wt
    tl = [{"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 12.0, "use_original_audio": True},
          {"role": "climax", "clip_start_sec": 20.0, "clip_end_sec": 24.0, "use_original_audio": True},
          {"role": "climax", "clip_start_sec": 30.0, "clip_end_sec": 32.0, "use_original_audio": False, "cover": "designated"}]
    segs = [{"start_sec": 0.5, "end_sec": 1.5, "text": "대사"}]
    res = {"tts_cue_files": [{"cue": {"start_sec": 6.1, "end_sec": 7.5, "text": "내레이션"}}]}
    blk = wt.build_material_block(tl, segs, res)
    lines = blk.splitlines()
    assert "[무성 —" not in lines[0] and "[무성 — 대사·내레이션 없음]" in lines[1]
    assert "[내레이션 덮개]" in lines[2]
    assert "무관한 인서트" in wt.PROMPT and "[무성 — 대사·내레이션 없음]" in wt.PROMPT


def test_binding_sampler_eight_samples_silent_only():
    from app.v3 import chunk_analyze as ca
    assert ca.BINDING_SAMPLE_COUNT == 8 and ca.BINDING_REJECT_MIN == 3 and len(ca.BINDING_QUANTILES) == 8
    chunk_spans = [{"id": f"s{i}", "t_in": float(i), "t_out": i + 1.0, "is_audio": (i % 3 == 0)} for i in range(40)]
    norm = [{"spans": [{"span_id": f"s{i}", "scene_script": f"장면{i}"} for i in range(40)]}]
    picked = ca.sample_binding_spans(norm, chunk_spans)
    assert len(picked) == 8 and all(not chunk_spans[int(p["id"][1:])]["is_audio"] for p in picked)
    assert picked[0]["t_in"] < 5.0 and picked[-1]["t_in"] >= 35.0          # 앞도 뒤도 본다(후반 편중)


def test_lines_material_marks_person_less_silent_spans():
    from app.v3.story_flow.common import span_row
    from app.v3.story_flow import select as sl
    sp = {"t_in": 0.0, "t_out": 2.0, "is_audio": False, "importance": 2, "scene_script": "벽을 깨는 손", "characters": []}
    assert "[무성·인물 없음]" in span_row("sp0001", sp)
    sp2 = {**sp, "characters": ["박경희"]}
    assert "[무성·인물 없음]" not in span_row("sp0001", sp2)
    sp3 = {**sp, "screen_text": "카톡 원문"}
    assert "[무성·인물 없음]" not in span_row("sp0001", sp3)
    assert "[무성·인물 없음]" in sl.LINES_PROMPT and "단서" in sl.LINES_PROMPT


def test_multi_stage_zoom_validate_and_split():
    ok, pr, notes = _val({"zooms": [{"clip": "C1", "stages": [
        {"from_sec": 0.0, "factor": 1.0}, {"from_sec": 1.3, "factor": 1.1, "anchor": "left"},
        {"from_sec": 2.5, "factor": 1.22, "anchor": "left"}, {"from_sec": 2.6, "factor": 1.4}]}]})
    z = ok["zooms"][0]
    assert [st["factor"] for st in z["stages"]] == [1.0, 1.1, 1.22]      # 4단째는 0.3s 미만 → 흡수
    assert any("흡수" in n or "3단" in n for n in notes) and z["factor"] == 1.0
    tl = [{"role": "hook", "clip_start_sec": 0.0, "clip_end_sec": 4.0, "use_original_audio": True, "span_ids": []},
          {"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 15.0, "use_original_audio": True, "span_ids": [], "subtitle": "x"}]
    out, zmap, p2r = finalize.apply_zoom_splits(tl, ok["zooms"], None)
    assert [(c["clip_start_sec"], c["clip_end_sec"]) for c in out[1:]] == [(10.0, 11.3), (11.3, 12.5), (12.5, 15.0)]
    assert set(zmap) == {2, 3} and zmap[2]["factor"] == 1.1 and zmap[3]["factor"] == 1.22 and p2r == {0: [0], 1: [1, 2, 3]}
    assert sum(c["clip_end_sec"] - c["clip_start_sec"] for c in out) == pytest.approx(9.0)
    # 배율 전부 1.0 이면 드롭
    none, _, notes2 = _val({"zooms": [{"clip": "C1", "stages": [{"from_sec": 0.0, "factor": 1.0}]}]})
    assert none["zooms"] == [] and any("전부 1.0" in n for n in notes2)
    assert "`stages`" in stage4.build_style_prompt(stage4.RECAP_PRESET, {"beats": []})



def test_emphasis_pairs_with_zoom_and_sfx(tmp_path):
    # v9/v10 규칙: 강조 줄 = 그 줄 시작의 줌 단계 = 타격음. 모델이 줌을 안 내면 코드가 짝을 채운다
    ok, pr, notes = _val({"emphasis": [{"line": "L1", "color": "red"}]})          # L1 5.0~6.0 → C1(4~9)
    z = ok["zooms"]
    assert len(z) == 1 and z[0]["clip"] == 1 and z[0]["stages"] == [{"from_sec": 1.0, "factor": 1.2, "anchor": "center"}]
    assert any("줌 짝 채움" in n for n in notes)
    # 이미 줌이 있는 컷이면 그 줄 시작에 단계를 더한다(경계가 가까우면 안 더함)
    ok2, _, notes2 = _val({"emphasis": [{"line": "L1"}], "zooms": [{"clip": "C1", "factor": 1.15, "from_sec": 0.0}]})
    assert [st["from_sec"] for st in ok2["zooms"][0]["stages"]] == [0.0, 1.0] and any("줌 단계 추가" in n for n in notes2)
    ok3, _, notes3 = _val({"emphasis": [{"line": "L1"}], "zooms": [{"clip": "C1", "factor": 1.3, "from_sec": 0.9}]})
    assert len(ok3["zooms"][0]["stages"]) == 1                                    # 0.9 vs 1.0 — 같은 자리
    # fit 컷은 줌 짝을 안 채운다
    ok4, _, _ = _val({"emphasis": [{"line": "L1"}], "fits": [{"clip": "C1"}]})
    assert ok4["zooms"] == []
    # 타격음: 강조 줄 시작에 emphasis_soft 태그 소리, 파일 스테이징
    from app.modules.sfx_narration import place_emphasis_sfx
    root = Path(__file__).resolve().parents[1] / "app"
    out = place_emphasis_sfx([{"start_sec": 5.0, "text": "아니야"}, {"start_sec": 20.0, "text": "뭐?"}],
                             app_root=root, run_dir=tmp_path, seed="t")
    assert len(out) == 2 and all(Path(o["path"]).exists() for o in out)
    assert all(o["_label"]["kind"] == "emphasis" and abs(o["_label"]["at"] - o["start_sec"]) < 0.2 for o in out)
    assert "한 쌍이다" in stage4.build_style_prompt(stage4.RECAP_PRESET, {"beats": []})
