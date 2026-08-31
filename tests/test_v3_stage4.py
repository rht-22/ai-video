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
    # 굵게는 어휘에서 내려갔다 — 값이 뭐든 **드롭+메모**(별도 테스트에서 전수)


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
        "subtitle_color": "#FFFFFF", "subtitle_size": 62,
        "tts_color": "#FFE94A", "tts_size": 60,
        "aspect_ratio": "5:4",
    })
    assert d.title_colors == ["#FFE94A", "#FF3B2D"]
    assert d.title_sizes == [88, 92]
    assert d.title_bolds == finalize.DesignConfig().title_bolds   # 굵게는 닫혀 있다
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
    assert (styled["labels"][0]["x"], styled["labels"][0]["y"]) == (0.72, 0.36)
    # 밴드·가로 범위 밖은 **보정**한다(라벨을 잃지 않는다) + 노트 기록
    assert styled["labels"][1]["x"] == pytest.approx(0.82)
    assert styled["labels"][1]["y"] == pytest.approx(0.315)
    assert any("위치 보정" in n for n in notes)


def test_malformed_label_entries_never_kill_the_plan():
    """라벨 좌표 한 칸 때문에 비트 crop/pop/sfx·design 까지 잃으면 안 된다(리뷰 H2)."""
    band = (0.275, 0.725)
    styled, problems, notes = stage4.validate_style_response(
        {"design": {"subtitle_color": "#FFFFFF"},
         "labels": [{"index": "0", "x": 0.5, "y": 0.5},      # index 형식 오류
                    {"index": 1}]}, 3, band=band,            # x·y 누락
        labels=[{"index": 0, "text": "(놀람)"}, {"index": 1, "text": "(월척)"}])
    assert problems == [] and styled["design"] == {"subtitle_color": "#FFFFFF"}
    assert styled["labels"] == []                            # 둘 다 기본 위치로
    assert any("2개 미배치" in n for n in notes)


def test_label_index_out_of_range_or_duplicated_is_dropped():
    """1-based 응답이면 전 라벨이 한 칸씩 밀려 **남의 자리**에 렌더된다(리뷰 C3)."""
    band = (0.275, 0.725)
    plan = [{"index": 0, "text": "(놀람)"}, {"index": 1, "text": "(월척)"}]
    styled, problems, notes = stage4.validate_style_response(
        {"design": {}, "labels": [{"index": 1, "x": 0.3, "y": 0.4},
                                  {"index": 2, "x": 0.7, "y": 0.4},
                                  {"index": 1, "x": 0.8, "y": 0.5}]},
        3, band=band, labels=plan)
    assert problems == []
    assert [l["index"] for l in styled["labels"]] == [1]      # 2 는 범위 밖, 중복은 선착순
    assert styled["labels"][0]["x"] == pytest.approx(0.3)
    assert any("밖" in n for n in notes) and any("중복" in n for n in notes)


def test_band_is_recomputed_from_the_design_the_model_actually_chose():
    """모델이 aspect_ratio 를 바꾸면 프리셋 밴드는 렌더 기하와 어긋난다(리뷰 C1)."""
    styled, problems, _ = stage4.validate_style_response(
        {"design": {"aspect_ratio": "16:9"}, "labels": [{"index": 0, "x": 0.5, "y": 0.68}]},
        1, labels=[{"index": 0, "text": "(놀람)"}], preset=stage4.RECAP_PRESET)
    assert problems == []
    # 16:9 밴드는 0.342~0.658 — 프리셋(5:4) 기준이면 0.68 이 그대로 통과해 검정 밴드 침범
    assert styled["labels"][0]["y"] <= 0.658 - stage4.LABEL_BAND_MARGIN + 1e-9


def test_long_label_cannot_be_pushed_off_screen():
    """\an5 는 글자 **중심** 기준 — 13자 라벨은 x=0.82 에서 106px 잘린다(리뷰 H1)."""
    band = (0.275, 0.725)
    styled, _, notes = stage4.validate_style_response(
        {"design": {}, "labels": [{"index": 0, "x": 0.82, "y": 0.4}]}, 1, band=band,
        labels=[{"index": 0, "text": "(여기서 판이 뒤집힌다)"}])
    assert styled["labels"][0]["x"] < 0.70
    assert any("위치 보정" in n for n in notes)


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


def test_style_labels_carry_rotate_color_fx():
    """M12b: 기울기·색·등장효과도 Stage 4 판단 — 렌더러가 이미 굽는 어휘로만."""
    styled, problems, notes = stage4.validate_style_response(
        {"design": {}, "labels": [
            {"index": 0, "x": 0.72, "y": 0.36, "rotate": -4, "color": "yellow", "fx": "pop"},
            {"index": 1, "x": 0.25, "y": 0.62, "rotate": 30, "color": "puce",
             "fx": "wobble"},
            {"index": 2, "x": 0.5, "y": 0.40}]}, 3, band=(0.275, 0.725))
    assert problems == []
    assert styled["labels"][0] == {"index": 0, "x": 0.72, "y": 0.36, "rotate": -4.0,
                                   "color": stage4.LABEL_PALETTE["yellow"], "fx": "pop"}
    # 범위 밖 기울기·미지원 어휘는 **보정**(라벨을 잃지 않는다)
    assert styled["labels"][1]["rotate"] == pytest.approx(stage4.LABEL_ROTATE_LIMIT)
    assert styled["labels"][1]["fx"] == "pop"
    assert sum("labels[1]" in n for n in notes) == 3        # 기울기·색·fx 세 건
    # 미지정은 조용히 순환 기본값 — 라벨이 전부 같은 색이 되지 않게
    assert styled["labels"][2]["color"] == stage4.LABEL_COLOR_CYCLE[2]
    assert styled["labels"][2]["fx"] == "pop"


def test_render_labels_fall_back_when_style_silent():
    """스타일이 위치를 못 주면 종전 기본값 — 위치 실패가 라벨 소실이 되면 안 된다."""
    assert finalize._cycle_color(0) == stage4.LABEL_COLOR_CYCLE[0]
    assert finalize._cycle_color(5) == stage4.LABEL_COLOR_CYCLE[1]


def test_title_bold_is_closed_to_ai():
    """E21 사용자 지시 — 제목 폰트가 이미 굵어 볼드를 얹으면 글자가 뭉갠다.

    반려가 아니라 **드롭+메모**: 굵게 한 키 때문에 플랜 전체(비트·라벨)를 잃으면 안 된다."""
    assert "title_bold" not in stage4.RECAP_PRESET
    assert "title_bold" not in stage4.STYLE_ALLOWED
    styled, problems, notes = stage4.validate_style_response(
        {"design": {"title_bold": True, "title_bold2": True,
                    "subtitle_color": "#FFFFFF"}}, 1)
    assert problems == []                                  # 플랜은 산다
    assert "title_bold" not in styled["design"]
    assert len([n for n in notes if "무시" in n]) == 2
    # 옛 체크포인트가 되살리지 못한다
    assert finalize.design_from_style({"title_bold": True, "title_bold2": True}) \
        .title_bolds == finalize.DesignConfig().title_bolds


def test_title_sizes_shrink_to_fit_canvas_width():
    """렌더러 줄바꿈은 1줄 크기로만 폭을 재서 큰 2줄이 잘린다 — 프레임 QC 실적발."""
    fit = finalize.fit_title_sizes
    # 가왕쇼 실측: 2줄 '배삯 버리고 따라간 이유'(11자)를 112px 로 그리면 1232px
    # 가왕쇼 실측: 2줄(글자10+공백3 = 10.9단위)을 112px 로 그리면 1221px — 양쪽이 잘렸다
    assert fit("유람선 승선 포기 속출\n배삯 버리고 따라간 이유", [92, 112]) == [92, 89]
    assert fit("짧은 줄\n짧은 줄", [92, 112]) == [92, 112]      # 들어가면 안 건드린다
    assert fit("", [92, 112]) == [92, 112]                      # 제목 없음 = 무변경


def test_resolve_work_logo_prefers_white_and_is_deterministic(tmp_path):
    """하단 밴드는 검정 — 흰색판이 정답이고 _color 는 차순위. 못 찾으면 텍스트 폴백."""
    d = tmp_path / "assets" / "logos"
    d.mkdir(parents=True)
    for stem, src in (("CG0g2", "가왕쇼_logo_final_white.png"),
                      ("CG0g2_color", "가왕쇼_logo_final_color.png"),
                      ("RZsv4", "다른작품_로고.png")):
        (d / f"{stem}.json").write_text('{"source_file": "%s"}' % src, encoding="utf-8")
        (d / f"{stem}.png").write_bytes(b"x")
    got = finalize.resolve_work_logo("가왕쇼", app_root=tmp_path)
    assert got is not None and got.stem == "CG0g2"
    assert finalize.resolve_work_logo("없는작품", app_root=tmp_path) is None
    assert finalize.resolve_work_logo("", app_root=tmp_path) is None
    # png 이 없으면 메타만 보고 고르지 않는다
    (d / "CG0g2.png").unlink()
    assert finalize.resolve_work_logo("가왕쇼", app_root=tmp_path).stem == "CG0g2_color"


def test_beat_pop_reaches_subtitle_lines():
    """Stage 4 의 비트 pop 은 아무도 안 읽는 죽은 출력이었다(사용자 지적)."""
    story = {"beats": [{"number": 0, "role": "hook", "span_ids": ["s1"],
                        "time": {"start": format_ts(100.0), "end": format_ts(104.0)}},
                       {"number": 1, "role": "climax", "span_ids": ["s2"],
                        "time": {"start": format_ts(104.0), "end": format_ts(108.0)}}]}
    tl = [{"clip_start_sec": 100.0, "clip_end_sec": 104.0, "role": "hook",
           "use_original_audio": True, "span_ids": ["s1"], "subtitle": ""},
          {"clip_start_sec": 104.0, "clip_end_sec": 108.0, "role": "climax",
           "use_original_audio": True, "span_ids": ["s2"], "subtitle": ""}]
    style = {"v3_style": {"beats": [{"number": 0, "pop": "none"},
                                    {"number": 1, "pop": "strong"}]}}
    win = finalize.subtitle_fx_windows(story, style, tl)
    assert win == [(4.0, 8.0, "pop_strong")]          # none 은 창을 만들지 않는다
    # 엔진 쪽 통로도 확인 — fx 가 없으면 태그가 붙지 않아야 한다(v1 회귀 0)
    from app.modules.subtitle import _line_style_overrides
    assert "fscx" in _line_style_overrides({"fx": "pop_strong"}, 2)[0]
    assert _line_style_overrides({"color": "#FFFFFF"}, 2)[0] == "{\\1c&HFFFFFF&}"
