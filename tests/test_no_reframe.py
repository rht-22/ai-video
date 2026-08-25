"""--no-reframe — 얼굴 추종 크롭 끄기 (2026-08-10).

인물이 고정된 인터뷰 소재(커리어데이 실측)는 얼굴로 확대하면 **원본에 박힌 자막이 잘린다**.
`enable_speaker_tracking` 은 '누구를 따라갈지'만 고르므로 꺼도 크롭은 계속 일어난다 —
크롭 자체를 끄는 스위치가 따로 필요했다.
(인물 인식 축은 2026-08-25 에 사라졌다 — TMDb·deepface 제거.)
"""
from __future__ import annotations

import argparse

from app.cli import _build_design_config
from app.config import DesignConfig


def _args(**kw):
    base = {k: None for k in (
        "design_title_y", "design_work_title_y", "design_aspect_ratio", "design_title_font",
        "design_title_size", "design_subtitle_size", "design_subtitle_color",
        "design_subtitle_y_margin", "design_tts_color", "design_tts_size",
        "design_tts_y_margin", "design_work_font_size", "design_work_color",
        "design_work_image_width", "design_work_image_height", "design_work_align",
        "design_subtitle_style", "design_title_color", "design_title_color2",
        "design_work_image")}
    base["no_reframe"] = False
    base.update(kw)
    return argparse.Namespace(**base)


def test_default_keeps_reframe_on():
    assert DesignConfig().enable_reframe is True
    assert _build_design_config(_args()).enable_reframe is True


def test_no_reframe_flag_turns_it_off():
    assert _build_design_config(_args(no_reframe=True)).enable_reframe is False


def test_no_reframe_is_independent_of_speaker_tracking():
    # 화자 추적은 '누구를' 고르는 것 — 리프레이밍 on/off 와 별개 축이다.
    base = _build_design_config(_args())
    d = _build_design_config(_args(no_reframe=True))
    assert d.enable_speaker_tracking == base.enable_speaker_tracking


def test_edit_plan_center_mode_when_crop_missing():
    """crop_map 이 비면 edit_plan 이 KeyError 대신 mode=center 를 낸다 (회귀 방지)."""
    from app.pipeline import PipelineInput, _build_edit_plan
    from app.modules.story_builder import StoryClip
    from app.config import AppConfig
    from pathlib import Path

    clips = [StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="",
                       use_original_audio=True)]
    payload = PipelineInput(video_path=Path("/x.mp4"), work_title="작품", topic="",
                            outdir=Path("/tmp"), design=DesignConfig(enable_reframe=False))
    plan = _build_edit_plan(payload, "제목", clips, {}, AppConfig())
    assert plan["timeline"][0]["reframe"] == {"mode": "center"}
