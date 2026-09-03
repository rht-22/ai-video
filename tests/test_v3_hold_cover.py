"""정보 화면 붙잡기(2026-09-03, 사용자 결정) — 덮개 화면이 내레이션보다 짧으면 마지막
프레임을 hold_sec 만큼 붙잡는다. 계기: EP01 카톡 대화 화면이 0.1초만 보이고 내레이션은
얼굴 컷 위에 얹혔다. 판단(글자가 있는 화면인가)은 모델(hold:true), 붙잡기는 코드.

고정하는 것:
  · clip_len 이 hold 를 포함하고, 편집본 좌표·cue 끝·초안/최종 렌더 길이가 전부 같은 값을 본다
  · choose_cover: hold 이고 지정 화면 < L 이면 이웃으로 안 넓히고 kind=hold(프로브 없음)
  · watch_trim 컷이 붙잡은 꼬리에 걸리면 hold_sec 만 줄어든다(소스 구간 불변)
  · hold 가 없으면 전부 종전과 동일
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v3_story_flow import GRID, IDX, ROWS_BY, _beats, _group  # noqa: E402

from app.config import DesignConfig  # noqa: E402
from app.modules.renderer import RenderInputs, _build_filtergraph  # noqa: E402
from app.modules.story_builder import StoryClip  # noqa: E402
from app.v3 import assemble, watch_trim as wt  # noqa: E402
from app.v3.story_flow import cover as cv  # noqa: E402
from app.v3.story_flow import narration as nr  # noqa: E402

FPS = 24000 / 1001


def test_clip_len_and_offsets_include_hold():
    tl = [{"clip_start_sec": 10.0, "clip_end_sec": 12.0, "hold_sec": 1.5, "use_original_audio": False, "span_ids": []},
          {"clip_start_sec": 20.0, "clip_end_sec": 25.0, "use_original_audio": True, "span_ids": []}]
    assert assemble.clip_len(tl[0]) == 3.5 and assemble.clip_len(tl[1]) == 5.0
    offs = assemble.edited_offsets(tl, FPS)
    assert offs[1][2] == pytest.approx(assemble.clip_duration(3.5, FPS))
    # cue 끝이 붙잡은 덮개의 소스 끝에 맞춰져 있으면 꼬리까지 이어진다
    cue = {"beat": 0, "line": 0, "text": "x", "mode": "cover", "speed": "fast",
           "source_time_sec": 10.1, "source_end_sec": 12.0, "muted_span_ids": [],
           "measured_sec": 3.0, "audio_path": None}
    cues = assemble.finalize_cues([cue], tl, voice="ko_female", speed="normal", fps=FPS)
    assert cues[0]["end_sec"] == pytest.approx(assemble.clip_duration(2.0, FPS) + 1.5, abs=0.05) \
        or cues[0]["end_sec"] == pytest.approx(2.0 + 1.5, abs=0.05)


def test_choose_cover_holds_short_designated_screen():
    """지정 화면 sp0003(2.5s) < L(3.0+pad) 이고 hold → 이웃으로 안 넓히고 붙잡는다."""
    g = _group(("before", 1), ["카톡을 열어보는데,"], [3.0], refers="메시지 화면")
    g["cover_ids"] = ["sp0003"]; g["hold"] = True
    cover = cv.choose_cover(("before", 1), g, _beats(), IDX, ROWS_BY, GRID)
    assert cover["kind"] == "hold" and (cover["t_in"], cover["t_out"]) == (6.5, 9.0)
    assert cover["hold_sec"] == pytest.approx(cover["L"] - 2.5, abs=1e-3) and cover["probe"] is None
    # hold 가 아니면 종전대로 지정 창을 넓혀 쓴다(붙잡기 없음)
    g2 = dict(g, hold=False)
    cover2 = cv.choose_cover(("before", 1), g2, _beats(), IDX, ROWS_BY, GRID)
    assert cover2["kind"] != "hold" and "hold_sec" not in cover2


def test_validate_narrations_parses_hold():
    resp = {"narrations": [{"before_beat": 0, "text": "훅.", "cover": ["sp0000"], "hold": True, "closed": True}]}
    obj, pr, _ = nr.validate_narrations(resp, 1, available={"sp0000"})
    assert obj[0]["hold"] is True
    obj, _, _ = nr.validate_narrations({"narrations": [{"before_beat": 0, "text": "훅.", "closed": True}]}, 1)
    assert obj[0]["hold"] is False


def test_watch_trim_edges_and_tail_cut_respect_hold():
    grid = {"span_candidates": [{"id": "a", "t_in": 100.0, "t_out": 102.0, "is_audio": False}]}
    tl = [{"clip_start_sec": 100.0, "clip_end_sec": 102.0, "hold_sec": 2.0, "span_ids": ["a"],
           "use_original_audio": False, "cover": "hold"}]
    assert wt.edited_span_edges(tl, grid)[-1] == 4.0          # 붙잡은 꼬리까지 편집본 길이
    # 꼬리(2.0~4.0) 위의 1초 컷 → hold_sec 만 1.0 으로, 소스 구간 그대로
    new = wt.apply_cuts_to_timeline(tl, [{"start": 3.0, "end": 4.0}], grid, set())
    assert (new[0]["clip_start_sec"], new[0]["clip_end_sec"], new[0]["hold_sec"]) == (100.0, 102.0, 1.0)


def test_renderer_pins_hold_frames():
    clips = [StoryClip(role="hook", start_sec=100.0, end_sec=102.0, subtitle="",
                       use_original_audio=False, hold_sec=1.5)]
    inputs = RenderInputs(video_path=Path("src.mp4"), clips=clips, subtitle_path=None,
                          crop_timeline_map={}, title_text="t", work_title="w",
                          output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
                          top_title_height=250, bottom_label_height=170,
                          design=DesignConfig(), source_fps=FPS)
    fg = _build_filtergraph(inputs, 1, 0)
    n = assemble.clip_frames(3.5, FPS)
    assert f"tpad=stop_mode=clone:stop_duration=2.500,trim=end_frame={n},setpts=PTS-STARTPTS[v0]" in fg
    assert f"apad,atrim=end={n / FPS:.6f}" in fg
    # hold 없는 클립은 종전 문자열 그대로
    clips0 = [StoryClip(role="hook", start_sec=100.0, end_sec=102.0, subtitle="", use_original_audio=True)]
    fg0 = _build_filtergraph(RenderInputs(video_path=Path("src.mp4"), clips=clips0, subtitle_path=None,
                          crop_timeline_map={}, title_text="t", work_title="w", output_path=Path("o.mp4"),
                          canvas_width=1080, canvas_height=1920, top_title_height=250,
                          bottom_label_height=170, design=DesignConfig(), source_fps=FPS), 1, 0)
    assert "stop_duration=1.000,trim=end_frame=" in fg0
