"""render_rough_concat ffmpeg argv 수준 검증.

자막/오버레이/크롭 없이 concat 필터만 사용한 mp4를 만드는지 확인.
실제 ffmpeg 실행은 mock으로 차단 (CI 환경 무관).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.modules.renderer import render_rough_concat


def _clip(start, end, role="hook"):
    return SimpleNamespace(
        start_sec=float(start),
        end_sec=float(end),
        role=role,
        character_focus=[],
    )


def test_argv_has_no_subtitle_or_overlay_filters(tmp_path: Path):
    clips = [_clip(10.0, 15.0), _clip(20.0, 25.0)]
    video = tmp_path / "src.mp4"
    video.write_bytes(b"")
    out = tmp_path / "rough_1.mp4"

    captured: list[list[str]] = []

    def _fake_check_call(cmd, **_kwargs):
        captured.append(list(cmd))
        out.write_bytes(b"")  # 출력 파일 존재한 척

    with patch("app.modules.renderer.subprocess.check_call", _fake_check_call):
        render_rough_concat(video, clips, out, enable_hwaccel=False)

    assert captured, "ffmpeg 호출이 한 번도 일어나지 않았다"
    argv = captured[0]
    joined = " ".join(argv)
    assert "subtitles=" not in joined, "rough 렌더에 자막 필터가 들어가면 안 됨"
    assert "drawtext" not in joined, "rough 렌더에 drawtext 오버레이가 들어가면 안 됨"
    assert "ass=" not in joined, "rough 렌더에 ass 자막이 들어가면 안 됨"


def test_argv_has_per_clip_ss_to_inputs(tmp_path: Path):
    clips = [_clip(10.0, 15.0), _clip(20.0, 25.0), _clip(30.0, 32.0)]
    video = tmp_path / "src.mp4"
    video.write_bytes(b"")
    out = tmp_path / "rough_1.mp4"

    captured: list[list[str]] = []

    def _fake_check_call(cmd, **_kwargs):
        captured.append(list(cmd))
        out.write_bytes(b"")

    with patch("app.modules.renderer.subprocess.check_call", _fake_check_call):
        render_rough_concat(video, clips, out, enable_hwaccel=False)

    argv = captured[0]
    # -ss 가 clip 개수만큼 나와야 한다
    ss_positions = [i for i, t in enumerate(argv) if t == "-ss"]
    to_positions = [i for i, t in enumerate(argv) if t == "-to"]
    assert len(ss_positions) == len(clips)
    assert len(to_positions) == len(clips)
    # 각 -ss/-to 값이 clip 시간과 일치
    for pos, clip in zip(ss_positions, clips):
        assert argv[pos + 1] == f"{clip.start_sec}"
    for pos, clip in zip(to_positions, clips):
        assert argv[pos + 1] == f"{clip.end_sec}"


def test_filter_script_uses_concat(tmp_path: Path):
    clips = [_clip(0.0, 5.0), _clip(5.0, 10.0)]
    video = tmp_path / "src.mp4"
    video.write_bytes(b"")
    out = tmp_path / "rough_1.mp4"

    def _fake_check_call(cmd, **_kwargs):
        out.write_bytes(b"")

    with patch("app.modules.renderer.subprocess.check_call", _fake_check_call):
        render_rough_concat(video, clips, out, enable_hwaccel=False)

    filter_path = out.with_suffix(".rough.filter.txt")
    assert filter_path.exists()
    content = filter_path.read_text(encoding="utf-8")
    assert "concat=n=2:v=1:a=1" in content
    assert "[vout]" in content and "[aout]" in content


def test_single_clip_uses_null_filter(tmp_path: Path):
    clips = [_clip(0.0, 5.0)]
    video = tmp_path / "src.mp4"
    video.write_bytes(b"")
    out = tmp_path / "rough_1.mp4"

    def _fake_check_call(cmd, **_kwargs):
        out.write_bytes(b"")

    with patch("app.modules.renderer.subprocess.check_call", _fake_check_call):
        render_rough_concat(video, clips, out, enable_hwaccel=False)

    filter_path = out.with_suffix(".rough.filter.txt")
    content = filter_path.read_text(encoding="utf-8")
    assert "concat" not in content
    assert "null" in content and "anull" in content


def test_empty_clips_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        render_rough_concat(tmp_path / "src.mp4", [], tmp_path / "out.mp4")


def test_encoder_fallback_to_libx264(tmp_path: Path):
    clips = [_clip(0.0, 5.0)]
    video = tmp_path / "src.mp4"
    video.write_bytes(b"")
    out = tmp_path / "rough_1.mp4"

    calls: list[list[str]] = []

    def _fake_check_call(cmd, **_kwargs):
        calls.append(list(cmd))
        # 첫 시도는 항상 실패 → libx264 폴백 경로가 결국 성공
        if "libx264" not in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        out.write_bytes(b"")

    with patch("app.modules.renderer.subprocess.check_call", _fake_check_call):
        with patch("app.modules.renderer._pick_video_encoder", return_value="h264_nvenc"):
            render_rough_concat(video, clips, out, enable_hwaccel=False)

    # 첫 시도는 nvenc, 마지막 성공 호출은 libx264
    assert "h264_nvenc" in calls[0]
    assert "libx264" in calls[-1]
