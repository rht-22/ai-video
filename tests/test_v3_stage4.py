"""V3-M4 — Stage 4 스타일·최종 렌더 어댑터·validate 확장 회귀 가드(LLM·ffmpeg 없이).

발주서 합격 기준의 코드 몫: style 화이트리스트·범위 검증(C5), 프리셋 diff,
design→DesignConfig 매핑(tts 색 ASS 변환 책임 포함), 비트 창 복원, validate 4종
(스냅 벨트·TTS 충돌·exception 유입)과 §9-D(진행감·루프 임계), hard_fail 판정.
"""
from __future__ import annotations

import pytest

from app.v3 import finalize, stage4
from app.v3.schemas import format_ts


# ── style 검증(C5) ──────────────────────────────────────────────────────────

def test_style_whitelist_rejects_unknown_key():
    styled, problems, _ = stage4.validate_style_response(
        {"design": {"background_music": "on"}}, 3)
    assert styled is None
    assert any("어휘 밖" in p for p in problems)


def test_style_range_and_format_validation():
    _, p1, _ = stage4.validate_style_response(
        {"design": {"subtitle_size": 300}}, 3)
    assert any("범위 밖" in x for x in p1)
    _, p2, _ = stage4.validate_style_response(
        {"design": {"subtitle_color": "빨강"}}, 3)
    assert any("형식 위반" in x for x in p2)
    _, p3, _ = stage4.validate_style_response(
        {"design": {"title_bold": "yes"}}, 3)
    assert any("불리언" in x for x in p3)


def test_style_ok_and_beat_normalization():
    styled, problems, notes = stage4.validate_style_response(
        {"design": {"subtitle_color": "#FFEE00", "subtitle_size": 66},
         "beats": [{"number": 1, "crop": "left", "pop": "soft", "sfx": "쿵"},
                   {"number": 2, "crop": "위쪽", "pop": None}],
         "notes": "n"}, 3)
    assert problems == []
    assert styled["design"] == {"subtitle_color": "#FFEE00", "subtitle_size": 66}
    assert styled["beats"][0] == {"number": 1, "crop": "left", "pop": "soft",
                                  "sfx_cue": "쿵"}
    assert styled["beats"][1]["crop"] == "center"          # 어휘 밖 → 보정
    assert any("보정" in n for n in notes)


def test_style_aspect_ratio_band_range():
    # '1:2'(세로 2160px 밴드)는 렌더를 죽인다 — 비율 범위 검증(적대 리뷰 지적)
    _, p1, _ = stage4.validate_style_response({"design": {"aspect_ratio": "1:2"}}, 3)
    assert any("비율 밖" in x for x in p1)
    ok, p2, _ = stage4.validate_style_response({"design": {"aspect_ratio": "16:9"}}, 3)
    assert p2 == [] and ok["design"]["aspect_ratio"] == "16:9"


def test_style_rejects_out_of_range_beat_number():
    styled, problems, _ = stage4.validate_style_response(
        {"design": {}, "beats": [{"number": 9}]}, 3)
    assert styled is None and any("비트 범위" in p for p in problems)


def test_style_diff_records_changes_only():
    diff = stage4.style_diff({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert diff == {"b": {"preset": 2, "styled": 3}}


# ── design → DesignConfig 매핑 ─────────────────────────────────────────────

def test_design_mapping_and_ass_color_conversion():
    d = finalize.design_from_style({
        "title_color": "#FFE94A", "title_color2": "#FF3B2D",
        "title_size": 88, "title_size2": 92,
        "title_bold": True, "title_bold2": True,
        "subtitle_color": "#FFFFFF", "subtitle_size": 62,
        "tts_color": "#FFE94A", "tts_size": 60,
        "aspect_ratio": "5:4",
    })
    assert d.title_colors == ["#FFE94A", "#FF3B2D"]
    assert d.title_sizes == [88, 92]
    assert d.title_bolds == [True, True]
    assert d.aspect_ratio == "5:4"
    # tts 색은 DesignConfig 가 ASS 표기 — hex 그대로 새면 렌더가 색을 못 읽는다
    assert d.tts_line_color.startswith("&H")
    assert "FFE94A" not in d.tts_line_color                # BGR 뒤집힘까지 확인
    assert d.tts_line_color == "&H004AE9FF"


def test_design_mapping_defaults_untouched():
    base = finalize.DesignConfig()
    d = finalize.design_from_style({})
    assert d.subtitle_size == base.subtitle_size
    assert d.tts_line_color == base.tts_line_color


# ── 비트 창 복원 ────────────────────────────────────────────────────────────

def _plan_timeline():
    return [
        {"clip_start_sec": 100.0, "clip_end_sec": 105.0, "role": "hook",
         "use_original_audio": True, "span_ids": ["sp0001"], "subtitle": ""},
        {"clip_start_sec": 200.0, "clip_end_sec": 203.0, "role": "climax",
         "use_original_audio": True, "span_ids": ["sp0005"], "subtitle": ""},
        {"clip_start_sec": 203.0, "clip_end_sec": 206.0, "role": "climax",
         "use_original_audio": False, "span_ids": ["sp0006"], "subtitle": ""},
    ]


def _story_doc():
    return {"beats": [
        {"number": 0, "role": "hook", "span_ids": ["sp0001"],
         "time": {"start": format_ts(100.0), "end": format_ts(105.0)}},
        {"number": 1, "role": "climax", "span_ids": ["sp0005", "sp0006"],
         "time": {"start": format_ts(200.0), "end": format_ts(206.0)}},
    ]}


def test_edited_beat_windows_spans_clips():
    w = stage4.edited_beat_windows(_story_doc(), _plan_timeline())
    assert w == [
        {"beat": 0, "start": 0.0, "end": 5.0, "role": "hook"},
        {"beat": 1, "start": 5.0, "end": 11.0, "role": "climax"},
    ]


# ── validate 4종 + §9-D ────────────────────────────────────────────────────

def test_exception_ingress_detects_overlap():
    stage1 = {"exception_sector": {
        "intro": {"start": format_ts(0.0), "end": format_ts(43.0)},
        "teaser": None}}
    clean = finalize.check_exception_overlap(
        [{"clip_start_sec": 100.0, "clip_end_sec": 105.0}], stage1)
    assert clean["violations"] == []
    dirty = finalize.check_exception_overlap(
        [{"clip_start_sec": 40.0, "clip_end_sec": 45.0}], stage1)
    assert len(dirty["violations"]) == 1
    assert dirty["violations"][0]["zone"] == "intro"
    assert dirty["violations"][0]["overlap_sec"] == pytest.approx(3.0)


def test_tts_conflict_check_via_source_identity():
    grid = {"span_candidates": [
        {"id": "sp0000", "t_in": 10.0, "t_out": 14.0, "is_audio": True,
         "time_authority": "stt", "text": "핵심"}]}
    stage2 = {"sequences": [{"number": 0, "chunks": [{"number": 0, "meanings": [
        {"number": 0, "content": "m", "characters": [], "importance": 5,
         "mood": "", "time": {"start": format_ts(10.0), "end": format_ts(14.0)},
         "spans": [{"number": 0, "span_id": "sp0000",
                    "time": {"start": format_ts(10.0), "end": format_ts(14.0)},
                    "is_audio": True, "importance": 5, "audio_script": [],
                    "scene_script": "", "characters": [],
                    "time_authority": "stt"}]}]}]}]}
    plan = {"timeline": [{"clip_start_sec": 10.0, "clip_end_sec": 14.0,
                          "span_ids": ["sp0000"]}]}
    bad = {"tts_cue_files": [{"cue": {
        "start_sec": 0.0, "end_sec": 2.0, "source_time_sec": 11.0,
        "duration_sec": 2.0, "muted_span_ids": [], "beat": 0}}]}
    res = finalize.check_tts_conflicts(bad, plan, stage2, grid)
    assert len(res["violations"]) == 1 and res["violations"][0]["span"] == "sp0000"
    ok = {"tts_cue_files": [{"cue": {
        "start_sec": 0.0, "end_sec": 2.0, "source_time_sec": 20.0,
        "duration_sec": 2.0, "muted_span_ids": [], "beat": 0}}]}
    assert finalize.check_tts_conflicts(ok, plan, stage2, grid)["violations"] == []


def test_progression_warns_on_static_window():
    timeline = [{"clip_start_sec": 0.0, "clip_end_sec": 10.0, "role": "silent_break",
                 "use_original_audio": True, "span_ids": []}]
    res = finalize.check_progression(timeline, [], {})
    assert len(res["warnings"]) == 1
    assert res["warnings"][0]["gap_sec"] == pytest.approx(10.0)
    assert res["warnings"][0]["role"] == "silent_break"    # 의도된 호흡 표기
    # 자막 이벤트가 3s 간격을 메우면 경고가 사라진다
    segs = [{"start_sec": t, "end_sec": t + 1, "text": "x"}
            for t in (2.5, 5.0, 7.5)]
    assert finalize.check_progression(timeline, segs, {})["warnings"] == []


def test_run_validate_hard_fail_logic(tmp_path):
    grid = {"span_candidates": [
        {"id": "sp0001", "t_in": 100.0, "t_out": 105.0, "is_audio": False,
         "time_authority": "scene"}]}
    plan = {"timeline": [{"clip_start_sec": 100.0, "clip_end_sec": 105.0,
                          "role": "hook", "use_original_audio": True,
                          "span_ids": ["sp0001"], "subtitle": ""}]}
    stage1 = {"exception_sector": {}}
    stage2 = {"sequences": []}
    doc = finalize.run_validate(
        plan=plan, grid=grid, stage1_doc=stage1, stage2_doc=stage2,
        segments=[], resources={}, final_path=None, tmp_dir=tmp_path,
        gemini=None, log=lambda *a: None)
    assert doc["hard_fail"] is False
    assert doc["snap_belt"]["pct"] == 100.0
    # 격자 밖 경계 → hard_fail
    plan_bad = {"timeline": [{"clip_start_sec": 101.5, "clip_end_sec": 105.0,
                              "role": "hook", "use_original_audio": True,
                              "span_ids": ["sp0001"], "subtitle": ""}]}
    doc2 = finalize.run_validate(
        plan=plan_bad, grid=grid, stage1_doc=stage1, stage2_doc=stage2,
        segments=[], resources={}, final_path=None, tmp_dir=tmp_path,
        gemini=None, log=lambda *a: None)
    assert doc2["hard_fail"] is True


def test_renderer_muted_windows_additive():
    # 적대 리뷰 확정(critical) 수정: use_original_audio 는 renderer 가 안 읽었다 —
    # muted_windows additive 필드가 원본 트랙에만 volume=0 (미지정 = 종전 바이트 동일)
    from pathlib import Path

    from app.modules.renderer import RenderInputs, _build_audio_filter
    from app.modules.story_builder import StoryClip
    base = dict(video_path=Path("/v.mp4"),
                clips=[StoryClip("hook", 0, 5, "", True)],
                subtitle_path=None, crop_timeline_map={}, title_text="",
                work_title="", output_path=Path("/o.mp4"), canvas_width=1080,
                canvas_height=1920, top_title_height=250, bottom_label_height=200)
    assert "volume=enable" not in _build_audio_filter(RenderInputs(**base), 1, 0)
    muted = _build_audio_filter(
        RenderInputs(**base, muted_windows=[(5.0, 10.0)]), 1, 0)
    assert "volume=enable='between(t,5.000,10.000)':volume=0," in muted


# ── M12: 라벨 위치를 Stage 4 가 화면 보고 정한다 ──────────────────────────

def test_video_band_ratio_matches_renderer_geometry():
    """라벨은 영상 밴드 **안**에 있어야 한다 — 검정 밴드는 제목·로고 자리."""
    import dataclasses
    from app.config import DesignConfig
    lo, hi = finalize.video_band_ratio(
        dataclasses.replace(DesignConfig(), aspect_ratio="5:4"))
    assert (round(lo * 1920), round(hi * 1920)) == (528, 1392)   # renderer 와 동일
    lo2, hi2 = finalize.video_band_ratio(
        dataclasses.replace(DesignConfig(), aspect_ratio="16:9"))
    assert hi2 - lo2 < hi - lo                                   # 납작할수록 좁다


def test_style_validates_label_positions_and_clamps():
    band = (0.275, 0.725)
    styled, problems, notes = stage4.validate_style_response(
        {"design": {}, "labels": [{"index": 0, "x": 0.72, "y": 0.36},
                                  {"index": 1, "x": 0.99, "y": 0.05}]}, 3, band=band)
    assert problems == []
    assert styled["labels"][0] == {"index": 0, "x": 0.72, "y": 0.36}
    # 밴드·가로 범위 밖은 **보정**한다(라벨을 잃지 않는다) + 노트 기록
    assert styled["labels"][1]["x"] == pytest.approx(0.82)
    assert styled["labels"][1]["y"] == pytest.approx(0.315)
    assert any("위치 보정" in n for n in notes)


def test_style_rejects_malformed_label_entries():
    _, problems, _ = stage4.validate_style_response(
        {"design": {}, "labels": [{"index": "0", "x": 0.5, "y": 0.5}]}, 3)
    assert any("index" in p for p in problems)
    _, p2, _ = stage4.validate_style_response(
        {"design": {}, "labels": [{"index": 0}]}, 3)
    assert any("x·y" in p for p in p2)


def test_plan_labels_uses_anchor_and_is_shared():
    """style 과 render 가 **같은 목록**을 봐야 index 가 맞는다(M12 계약)."""
    story = {"beats": [{"number": 0, "role": "climax",
                        "span_ids": ["s1", "s2"],
                        "labels": [{"text": "(놀람)", "span_id": "s2"}],
                        "time": {"start": format_ts(100.0), "end": format_ts(110.0)}}]}
    plan = {"timeline": [
        {"clip_start_sec": 100.0, "clip_end_sec": 105.0, "role": "climax",
         "use_original_audio": True, "subtitle": "", "span_ids": ["s1"]},
        {"clip_start_sec": 105.0, "clip_end_sec": 110.0, "role": "climax",
         "use_original_audio": True, "subtitle": "", "span_ids": ["s2"]}]}
    out = finalize.plan_labels(story, plan)
    assert len(out) == 1 and out[0]["index"] == 0
    assert out[0]["start_sec"] == pytest.approx(5.0)     # 앵커 s2 의 편집본 시각
    assert out[0]["end_sec"] == pytest.approx(9.0)       # +4s 상한
