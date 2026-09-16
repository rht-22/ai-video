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
    assert len(out) == 2 and p2r == {0: [0], 1: [1]} and set(zmap) == {1}
    assert out[0]["clip_start_sec"] == 10.0 and out[0]["clip_end_sec"] == 14.0
    assert out[0]["subtitle"] == "x" and not out[0].get("zoom_part")
    assert out[1]["hold_sec"] == 0.5
    total = sum(float(c["clip_end_sec"]) - float(c["clip_start_sec"]) for c in out)
    assert total == pytest.approx(9.0)
    # 줌 없음 = plan 과 동일(사본)
    same, zm, p = finalize.apply_zoom_splits(tl, [], fps)
    assert same == tl and zm == {} and p == {0: [0], 1: [1]}
    # 남는 조각이 짧으면 통째 줌
    whole, zm2, _ = finalize.apply_zoom_splits(tl, [{"clip": 0, "factor": 1.4, "anchor": "center", "from_sec": 3.9}], None)
    assert len(whole) == 2 and 0 not in zm2


def test_mid_clip_zoom_never_splits_original_dialogue_audio():
    tl = [{"role": "hook", "clip_start_sec": 844.63, "clip_end_sec": 849.0,
           "use_original_audio": True, "span_ids": [], "subtitle": "박서진을 뽑아라"}]
    out, zmap, p2r = finalize.apply_zoom_splits(
        tl, [{"clip": 0, "factor": 1.2, "anchor": "center", "from_sec": 1.85}], 30)
    assert out == tl
    assert zmap == {} and p2r == {0: [0]}


def test_zoom_split_uses_output_seconds_for_sped_cover():
    tl = [{"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 12.4,
           "playback_speed": 1.2, "use_original_audio": False, "span_ids": []}]
    out, _, _ = finalize.apply_zoom_splits(
        tl, [{"clip": 0, "factor": 1.2, "anchor": "center", "from_sec": 1.0}], 30)
    assert out[0]["clip_end_sec"] == pytest.approx(11.2)
    assert sum(finalize.assemble.clip_len(c) for c in out) == pytest.approx(2.0)


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
    assert "바로 그 구간" in p and "뒤에 나올 내레이션·가사·반전" in p
    assert "이 구간에 실제로 들리는 말" in stage4.LABEL_PROBE_PROMPT


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
          {"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 15.0, "use_original_audio": False, "span_ids": [], "subtitle": "x"}]
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
    assert "원본 대사가 묻히지 않도록 타격음은 얹지 않는다" in stage4.build_style_prompt(
        stage4.RECAP_PRESET, {"beats": []})


def test_fixed_crop_from_beat_crop_x(tmp_path):
    """2026-09-11 ep01x03: 훅 직후 남편(먼 인물, 검출 0)에게 컷을 맞추려 비트 crop_x(소스 px) → 클립 reframe
    mode=fixed → finalize.fixed_crop_map 이 고정 키프레임 맵을 낸다(그림 사각형·밴드 크롭 안 클램프)."""
    import json
    from app.v3 import finalize
    tl = [{"role": "build", "clip_start_sec": 10.0, "clip_end_sec": 12.0, "reframe": {"mode": "fixed", "x": 570.0}},
          {"role": "build", "clip_start_sec": 12.0, "clip_end_sec": 14.0, "reframe": {"mode": "center"}}]
    (tmp_path / "checkpoint_probe.json").write_text(json.dumps({"width": 1920, "height": 1080}), encoding="utf-8")
    m, audit = finalize.fixed_crop_map(tl, output_dir=tmp_path, aspect_ratio="24:23",
                                       picture={"x": 0, "y": 60, "w": 1920, "h": 960}, video_path=tmp_path / "x.mp4", log=lambda *a: None)
    assert list(m) == ["build_0"] and audit[0]["x"] == 570.0
    rows = json.loads(m["build_0"].read_text(encoding="utf-8"))
    assert [r["time_sec"] for r in rows] == [10.0, 12.0] and rows[0]["x_center"] == 570.0
    assert rows[0]["crop_w"] / rows[0]["crop_h"] == pytest.approx(24 / 23, rel=0.01)
    # x 가 그림 밖이면 크롭 반폭으로 클램프
    tl2 = [{"role": "hook", "clip_start_sec": 0.0, "clip_end_sec": 1.0, "reframe": {"mode": "fixed", "x": 10.0}}]
    m2, a2 = finalize.fixed_crop_map(tl2, output_dir=tmp_path, aspect_ratio="24:23",
                                     picture={"x": 0, "y": 60, "w": 1920, "h": 960}, video_path=tmp_path / "x.mp4", log=lambda *a: None)
    assert a2[0]["x"] == json.loads(m2["hook_0"].read_text())[0]["crop_w"] / 2
    assert finalize.fixed_crop_map([{"role": "b", "clip_start_sec": 0, "clip_end_sec": 1}], output_dir=tmp_path,
                                   aspect_ratio="24:23", picture=None, video_path=tmp_path / "x.mp4") == ({}, [])


def test_emphasis_sfx_never_overlays_original_dialogue():
    """강조는 원본 대사 위에 있으므로 타격음으로 음절을 가리지 않는다."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("app/v3/finalize.py").read_text()
    assert "원본 대사 보호" in src and "_emph_sfx: list = []" in src


def test_adjacent_emphasis_sfx_use_different_sounds(tmp_path):
    """붙어 있는 강조 줄(3초 안)은 서로 다른 효과음 — 매니페스트 emphasis_followup(punch)과 번갈아(2026-09-11)."""
    from pathlib import Path
    import app.config as c
    from app.modules.sfx_narration import place_emphasis_sfx
    root = Path(c.__file__).resolve().parent
    lines = [{"start_sec": 0.15, "text": "a"}, {"start_sec": 29.56, "text": "b"}, {"start_sec": 30.01, "text": "c"},
             {"start_sec": 31.12, "text": "d"}, {"start_sec": 46.54, "text": "e"}, {"start_sec": 47.54, "text": "f"}]
    out = place_emphasis_sfx(lines, app_root=root, run_dir=tmp_path, seed="s")
    fam = [o["_label"]["family"] or o["_label"]["id"] for o in out]
    assert len(fam) == 6
    for k, (a, b) in enumerate(zip(lines, lines[1:])):
        if b["start_sec"] - a["start_sec"] <= 3.0:
            assert fam[k] != fam[k + 1], (k, fam)
    assert fam[0] == "hit"                      # 떨어진 강조는 종전(soft 풀) 그대로
    assert all(o["gain_db"] == -12.0 for o in out)  # 최종 loudnorm을 흔들지 않는 상한


def test_adjacent_emphasis_chain_stays_in_hit_family(tmp_path):
    """세 줄 연속 강조는 이웃끼리 서로 다르고 강조 풀(hit3 · 연이어 강조 punch/gunshot) 안에서만 고른다(2026-09-11)."""
    from pathlib import Path
    import app.config as c
    from app.modules.sfx_narration import place_emphasis_sfx
    root = Path(c.__file__).resolve().parent
    lines = [{"start_sec": 29.56, "text": "a"}, {"start_sec": 30.01, "text": "b"}, {"start_sec": 32.11, "text": "c"}]
    fam = [o["_label"]["id"] for o in place_emphasis_sfx(lines, app_root=root, run_dir=tmp_path, seed="s")]
    assert fam[0] == "hit3" and all(a != b for a, b in zip(fam, fam[1:])), fam
    assert set(fam) <= {"hit3", "punch", "gunshot"}, fam


def test_emphasis_index_is_resolved_by_text_and_time_after_subtitle_edits():
    """자막 오버라이드로 줄이 합쳐져 번호가 밀려도 강조는 원래 글자의 줄에 붙는다(2026-09-11 3편·4편 실사고)."""
    from app.v3.finalize import emphasis_styles, resolve_emphasis_index
    segs = [{"start_sec": 0.0, "text": "a"}, {"start_sec": 1.0, "text": "이걸로, 이거 불 끌 때"},
            {"start_sec": 2.0, "text": "으아악!"}, {"start_sec": 3.0, "text": "불을 어디다"}, {"start_sec": 3.9, "text": "끄는 거예요?"}]
    v = {"emphasis": [{"index": 3, "text": "으아악!", "start_sec": 2.0},             # 번호가 한 줄 밀림 → 글자로
                      {"index": 5, "text": "끄는 거예요?", "start_sec": 3.9},        # 범위 밖 번호 → 글자로
                      {"index": 0, "text": "원래 문구", "start_sec": 3.05},          # 글자가 고쳐짐 → 시각으로
                      {"index": 1, "text": "이걸로, 이거 불 끌 때"}]}                 # 번호·글자 일치 → 그대로
    assert sorted(emphasis_styles(v, 60, segs)) == [1, 2, 3, 4]
    assert resolve_emphasis_index({"index": 2, "text": "x"}, None) == 2              # 세그먼트 없으면 종전(번호)
    assert resolve_emphasis_index({"index": 0, "text": "없는 글자", "start_sec": 9.0}, segs) == 0   # 못 찾으면 번호
