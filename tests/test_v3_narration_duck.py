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


def test_none_uses_soft_mute_boundaries():
    a = _build_audio_filter(_inputs(muted_windows=[(2.0, 4.0)]), 1, 0)
    b = _build_audio_filter(_inputs(muted_windows=[(2.0, 4.0)], muted_gain_db=None), 1, 0)
    assert a == b
    assert "volume='if(between(t,1.880,4.120)" in a
    assert "(2.000-t)/0.12" in a and "(t-4.000)/0.12" in a
    assert "eval=frame" in a


def test_zero_fade_preserves_hard_mute_for_explicit_legacy_use():
    f = _build_audio_filter(_inputs(muted_windows=[(2.0, 4.0)], mute_fade_sec=0), 1, 0)
    assert "volume=enable='between(t,2.000,4.000)':volume=0," in f


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


def test_amix_never_renormalizes_when_cues_end():
    f = _build_audio_filter(_inputs(tts_cue_files=_CUE), 1, 1)
    assert "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0" in f
    assert "dropout_transition=2" not in f


def test_duck_ends_at_measured_speech_end():
    cue = [{"path": Path("c.mp3"), "cue": {
        "start_sec": 2.0, "end_sec": 4.0, "audible_end_sec": 3.72}}]
    f = _build_audio_filter(_inputs(tts_cue_files=cue), 1, 1)
    assert "between(t,2.000,3.720)" in f
    assert "between(t,2.000,4.000)" not in f


def test_non_contiguous_source_cuts_get_short_audio_fades():
    clips = [
        StoryClip(role="hook", start_sec=10, end_sec=12, subtitle="", use_original_audio=True),
        StoryClip(role="build", start_sec=40, end_sec=42, subtitle="", use_original_audio=True),
    ]
    inputs = _inputs()
    import dataclasses
    from app.modules.renderer import _build_filtergraph
    fg = _build_filtergraph(dataclasses.replace(inputs, clips=clips, source_fps=30), 2, 0)
    assert "afade=t=out:st=1.920:d=0.080" in fg
    assert "afade=t=in:st=0:d=0.080" in fg


def test_contiguous_source_cuts_do_not_fade_dialogue_edges():
    clips = [
        StoryClip(role="hook", start_sec=10, end_sec=12, subtitle="", use_original_audio=True),
        StoryClip(role="build", start_sec=12.03, end_sec=14, subtitle="", use_original_audio=True),
    ]
    inputs = _inputs()
    import dataclasses
    from app.modules.renderer import _build_filtergraph
    fg = _build_filtergraph(dataclasses.replace(inputs, clips=clips, source_fps=30), 2, 0)
    assert "afade=" not in fg


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
