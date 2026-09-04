"""내레이션 덮개 구간 원본 볼륨(--narration-original-db) — 2026-09-04 사용자 요청.

종전엔 덮개(뮤트 창)에서 원본을 volume=0 으로 완전히 껐다. dB 를 주면 줄이기만
한다. None = 필터 문자열 종전과 바이트 동일(회귀 0). 덕킹(×0.5)은 뮤트 창 밖에서만.
"""
from __future__ import annotations

from pathlib import Path

from app.config import DesignConfig
from app.modules.renderer import RenderInputs, _build_audio_filter
from app.modules.story_builder import StoryClip


def _inputs(**kw) -> RenderInputs:
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=10.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text="제목", work_title="가왕쇼",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=DesignConfig(), **kw)


_CUE = [{"path": Path("c.mp3"), "cue": {"start_sec": 2.0, "end_sec": 4.0}}]


def test_none_is_byte_identical_mute():
    a = _build_audio_filter(_inputs(muted_windows=[(2.0, 4.0)]), 1, 0)
    b = _build_audio_filter(_inputs(muted_windows=[(2.0, 4.0)], muted_gain_db=None), 1, 0)
    assert a == b and "volume=enable='between(t,2.000,4.000)':volume=0," in a


def test_gain_db_replaces_zero():
    f = _build_audio_filter(_inputs(muted_windows=[(2.0, 4.0)], muted_gain_db=-12), 1, 0)
    assert "volume=enable='between(t,2.000,4.000)':volume=-12dB," in f
    assert ":volume=0," not in f


def test_duck_excludes_mute_window_only_when_gain_set():
    plain = _build_audio_filter(_inputs(muted_windows=[(2.0, 4.0)], tts_cue_files=_CUE), 1, 1)
    assert "volume=enable='between(t,2.000,4.000)':volume=0.5" in plain     # 종전 그대로
    ducked = _build_audio_filter(_inputs(muted_windows=[(2.0, 4.0)], tts_cue_files=_CUE,
                                         muted_gain_db=-10), 1, 1)
    assert "volume=enable='(between(t,2.000,4.000))*not(between(t,2.000,4.000))':volume=0.5" \
        in ducked


def test_cli_and_pipeline_threading():
    from app.v3.cli import build_parser
    args = build_parser().parse_args(["--video", "x", "--work-title", "w",
                                      "--narration-original-db", "-12"])
    assert args.narration_original_db == -12.0
    # 기본값 = -14(사용자 청취 선택) — v3 CLI 만. 렌더러 기본은 여전히 None(무음, v1 회귀 0)
    from app.v3.cli import NARRATION_ORIGINAL_DB
    assert NARRATION_ORIGINAL_DB == -14.0
    assert build_parser().parse_args(["--video", "x", "--work-title", "w"]
                                     ).narration_original_db == -14.0
    from app.modules.renderer import RenderInputs
    import dataclasses
    assert next(f for f in dataclasses.fields(RenderInputs)
                if f.name == "muted_gain_db").default is None
    import inspect
    from app.v3 import finalize, pipeline
    assert "muted_gain_db" in inspect.signature(finalize.render_final).parameters
    assert "narration_original_db" in inspect.signature(pipeline.run_v3).parameters
