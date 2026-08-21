"""E8 — 시간대별 제목(타임드 제목): edit_overrides title.segments.

계약:
· `title.segments` (v3 전용): [{text, start_sec, end_sec}] — 좌표계는 subtitles 와
  동일(**편집본 시간축**, 쇼츠 0초 시작, 초). text 는 top_title 규약(줄바꿈 = 2줄
  위계·title_colors/title_sizes lookup).
· segments 가 있으면 **그 창들만 그린다** — 창 밖 시간은 제목 없음(빈 화면이 유효값).
  top_title 은 세그먼트가 없을 때의 종전 경로(전체 상영) 그대로 — 회귀 없음.
· 검증(즉시 실패): text 비면 거절 · start_sec≥0 · end_sec>start_sec · 창끼리 겹침
  거절(제목은 한 벌 자리라 겹치면 포개진다) · 최대 20개. 렌더 경계에서도 재검증.
· 자동 배치 기준선은 전 세그먼트 공통 한 벌 — **최대 줄 수(=최대 블록 높이) 기준**.
· title_y_fixed/title_y·title_size·title_rotate(E7)는 전 세그먼트 공통(디자인 레벨).
· 배속(E7-2 지점 A) 정합: 제목은 setpts 뒤에 얹히므로 enable 창만 출력 시각(×1/S) —
  이미지 오버레이([6.5])와 같은 지점(렌더러)에서 나눈다.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import AppConfig, DesignConfig
from app.modules.edit_overrides import (
    EditOverrideError,
    overrides_title_segments,
    validate_overrides,
    validate_title_segments,
)
from app.modules.renderer import RenderInputs, _build_filtergraph
from app.modules.story_builder import StoryClip
from app.pipeline import _build_edit_plan


def _ov(segments, schema="edit_overrides/v3", **title_extra):
    return {"schema": schema, "title": {"segments": segments, **title_extra}}


def _segs2():
    return [{"text": "첫 제목", "start_sec": 0, "end_sec": 12.5},
            {"text": "반전!", "start_sec": 15.5, "end_sec": 30}]


def _inputs(design=None, title_segments=None, title="제목"):
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text=title, work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170,
        design=design or DesignConfig(),
        title_segments=title_segments)


# ── 계약 검증 (validate_overrides / validate_title_segments) ────────────

def test_valid_segments_accepted_v3():
    data = validate_overrides(_ov(_segs2(), top_title="기본 제목"))
    assert data["title"]["segments"][0]["text"] == "첫 제목"


def test_segments_rejected_on_old_schema():
    # v3 전용 — 구 스키마에 얹으면 구 엔진 노드가 조용히 무시하므로 즉시 거절
    for schema in ("edit_overrides/v1", "edit_overrides/v2"):
        with pytest.raises(EditOverrideError, match="v3 전용"):
            validate_overrides(_ov(_segs2(), schema=schema))


def test_segments_must_be_nonempty_list():
    with pytest.raises(EditOverrideError):
        validate_overrides(_ov([]))
    with pytest.raises(EditOverrideError):
        validate_overrides(_ov({"text": "객체는 안 된다"}))


def test_segments_max_20():
    segs = [{"text": f"제목{i}", "start_sec": i * 2.0, "end_sec": i * 2.0 + 1.0}
            for i in range(21)]
    with pytest.raises(EditOverrideError, match="최대 20"):
        validate_title_segments(segs)
    validate_title_segments(segs[:20])   # 20개는 통과


def test_segment_empty_text_rejected():
    with pytest.raises(EditOverrideError, match="text 가 비어"):
        validate_title_segments([{"text": "  ", "start_sec": 0, "end_sec": 1}])


def test_segment_bad_window_rejected():
    with pytest.raises(EditOverrideError, match="음수"):
        validate_title_segments([{"text": "t", "start_sec": -0.1, "end_sec": 1}])
    with pytest.raises(EditOverrideError, match="뒤집혔거나"):
        validate_title_segments([{"text": "t", "start_sec": 3, "end_sec": 3}])
    with pytest.raises(EditOverrideError, match="start_sec·end_sec"):
        validate_title_segments([{"text": "t", "start_sec": 0}])


def test_segment_overlap_rejected_touching_ok():
    # 제목은 한 벌 자리 — 겹치면 포개지므로 거절. 경계가 맞닿는 것(=연속)은 유효하다.
    with pytest.raises(EditOverrideError, match="겹칩니다"):
        validate_title_segments([{"text": "a", "start_sec": 0, "end_sec": 10},
                                 {"text": "b", "start_sec": 9.9, "end_sec": 20}])
    validate_title_segments([{"text": "a", "start_sec": 0, "end_sec": 10},
                             {"text": "b", "start_sec": 10, "end_sec": 20}])


def test_segment_unknown_key_rejected():
    # 세그먼트별 스타일은 이번 판 범위 밖(후속) — 조용한 무시 대신 즉시 거절
    with pytest.raises(EditOverrideError, match="모르는 키"):
        validate_title_segments([{"text": "t", "start_sec": 0, "end_sec": 1,
                                  "size": 90}])


def test_overrides_title_segments_normalizes_and_sorts():
    ov = _ov([{"text": "둘째", "start_sec": "15.5", "end_sec": 30},
              {"text": "첫째", "start_sec": 0, "end_sec": "12.5"}])
    out = overrides_title_segments(ov)
    assert out == [{"text": "첫째", "start_sec": 0.0, "end_sec": 12.5},
                   {"text": "둘째", "start_sec": 15.5, "end_sec": 30.0}]
    assert overrides_title_segments({"schema": "edit_overrides/v3"}) is None
    assert overrides_title_segments(None) is None


# ── 렌더 그래프: enable 창 + 기준선 고정 ────────────────────────────────

def test_segments_draw_only_windows_ignore_top_title():
    fg = _build_filtergraph(_inputs(title_segments=_segs2()), 1, 0)
    # 세그먼트 창만 그린다 — 종전 전체 상영 경로([title_0]) 없음
    assert "[title_0]" not in fg
    assert ":enable='between(t,0.000,12.500)'[tseg0_0]" in fg
    assert ":enable='between(t,15.500,30.000)'[tseg1_0]" in fg


def test_no_segments_keeps_legacy_full_run_path():
    fg = _build_filtergraph(_inputs(), 1, 0)
    assert "y=330[title_0]" in fg          # E7 과 동일한 종전 경로 그대로
    assert "tseg" not in fg and "enable" not in fg.split("[6.5]")[0].split("drawtext")[0]


def test_baseline_fixed_by_max_line_count():
    # 세그 0 = 1줄(70px, 블록 70), 세그 1 = 2줄(70+30+90 = 블록 190).
    # 자동 배치 기준선은 **최대 블록** 기준 한 벌: top = overlay_y 420 - 190 - 20 = 210.
    # 두 세그먼트의 첫 줄이 같은 y(210)에서 시작한다 — 세그먼트 전환 때 튐 없음.
    segs = [{"text": "하나", "start_sec": 0, "end_sec": 5},
            {"text": "첫줄\n둘째", "start_sec": 5, "end_sec": 10}]
    fg = _build_filtergraph(_inputs(title_segments=segs), 1, 0)
    assert "y=210:enable='between(t,0.000,5.000)'[tseg0_0]" in fg
    assert "y=210:enable='between(t,5.000,10.000)'[tseg1_0]" in fg
    assert "y=310:enable='between(t,5.000,10.000)'[tseg1_1]" in fg   # 210+70+30


def test_title_y_fixed_common_to_all_segments():
    d = DesignConfig(title_y_fixed=True, title_y=150)
    fg = _build_filtergraph(_inputs(design=d, title_segments=_segs2()), 1, 0)
    assert "y=150:enable='between(t,0.000,12.500)'[tseg0_0]" in fg
    assert "y=150:enable='between(t,15.500,30.000)'[tseg1_0]" in fg


# ── E7 회전 정합: 세그먼트별 캔버스 → rotate → overlay(enable) ──────────

def test_segments_with_rotate_per_segment_canvas():
    fg = _build_filtergraph(
        _inputs(DesignConfig(title_rotate=8.0), title_segments=_segs2()), 1, 0)
    assert fg.count("rotate=") == 2                    # 세그먼트별 캔버스 회전
    assert "[tsg0_rot]overlay=" in fg and "[tsg1_rot]overlay=" in fg
    # 창은 overlay 의 enable 파라미터로 단다
    assert "overlay=" in fg
    assert ":enable='between(t,0.000,12.500)'[tsg0out]" in fg
    assert ":enable='between(t,15.500,30.000)'[tsg1out]" in fg
    # 종전 단일 회전 경로 라벨은 없다
    assert "[ttlrot]" not in fg and "[with_title]" not in fg


def test_rotate_canvas_height_is_per_segment_block():
    # 세그 0(1줄 70px): 패딩 35 → 캔버스 1080x140. 세그 1(2줄 70+90, 패딩 45):
    # 70+30+90+90 = 280. 회전 원점(블록 중심)이 세그먼트별 제 블록에 있다.
    segs = [{"text": "하나", "start_sec": 0, "end_sec": 5},
            {"text": "첫줄\n둘째", "start_sec": 5, "end_sec": 10}]
    fg = _build_filtergraph(
        _inputs(DesignConfig(title_rotate=8.0), title_segments=segs), 1, 0)
    assert "color=c=black@0.0:s=1080x140:d=1,format=rgba[tsg0_0]" in fg
    assert "color=c=black@0.0:s=1080x280:d=1,format=rgba[tsg1_0]" in fg


def test_legacy_rotate_path_unchanged_without_segments():
    fg = _build_filtergraph(_inputs(DesignConfig(title_rotate=90.0)), 1, 0)
    assert "[ttlrot]overlay=470:-175[with_title]" in fg   # E7 실측값 그대로


# ── E7-2 배속 정합: 창은 출력 시각(×1/S) ────────────────────────────────

def test_segment_windows_scaled_by_speed():
    fg = _build_filtergraph(
        _inputs(DesignConfig(video_speed=1.25), title_segments=_segs2()), 1, 0)
    # 12.5/1.25=10, 15.5/1.25=12.4, 30/1.25=24 — 이미지 오버레이와 같은 규약
    assert ":enable='between(t,0.000,10.000)'[tseg0_0]" in fg
    assert ":enable='between(t,12.400,24.000)'[tseg1_0]" in fg
    assert "[vspd]drawtext" in fg                      # setpts 뒤(출력 시각) 체인


def test_segment_windows_scaled_with_rotate_too():
    fg = _build_filtergraph(
        _inputs(DesignConfig(video_speed=1.25, title_rotate=8.0),
                title_segments=_segs2()), 1, 0)
    assert ":enable='between(t,0.000,10.000)'[tsg0out]" in fg
    assert ":enable='between(t,12.400,24.000)'[tsg1out]" in fg


# ── 렌더 경계 재검증 (E7 패턴 — 조용한 무시 금지) ───────────────────────

def test_renderer_rejects_bad_segments_too():
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(title_segments=[
            {"text": "t", "start_sec": 5, "end_sec": 3}]), 1, 0)
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(title_segments=[
            {"text": "a", "start_sec": 0, "end_sec": 10},
            {"text": "b", "start_sec": 5, "end_sec": 15}]), 1, 0)


# ── edit_plan.layout 기록 (하류 검산·검수 노출용) ───────────────────────

def _payload():
    return SimpleNamespace(video_path=Path("src.mp4"), work_title="작품",
                           topic="주제", language="ko", design=DesignConfig())


def _clip():
    return StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)


def test_edit_plan_records_segments():
    segs = [{"text": "첫 제목", "start_sec": 0.0, "end_sec": 12.5}]
    plan = _build_edit_plan(_payload(), "기본 제목", [_clip()], {}, AppConfig(),
                            title_segments=segs)
    assert plan["layout"]["title_segments"] == segs
    assert plan["layout"]["top_title"] == "기본 제목"


def test_edit_plan_omits_key_when_no_segments():
    plan = _build_edit_plan(_payload(), "기본 제목", [_clip()], {}, AppConfig())
    assert "title_segments" not in plan["layout"]
