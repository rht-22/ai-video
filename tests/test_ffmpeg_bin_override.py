"""FFMPEG_BIN/FFPROBE_BIN 환경변수 오버라이드 — find_ffmpeg_command 우선순위.

운영 머신의 PATH ffmpeg(homebrew 8.x)가 libass 없이 빌드되어 ass 필터가 없고,
자막 번인(renderer)이 그 빌드로는 실패한다. FFMPEG_BIN 으로 libass 포함 빌드를
지정하면 PATH 보다 먼저 잡혀야 하고, 환경변수가 없으면 기존 PATH 검색 그대로다.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.modules.ffmpeg_utils import (
    FFMPEG_BIN_ENV,
    FFPROBE_BIN_ENV,
    find_ffmpeg_command,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """머신/셸의 FFMPEG_BIN·FFPROBE_BIN 이 테스트에 새지 않게 제거."""
    monkeypatch.delenv(FFMPEG_BIN_ENV, raising=False)
    monkeypatch.delenv(FFPROBE_BIN_ENV, raising=False)


def _make_exe(directory: Path, name: str) -> Path:
    p = directory / name
    p.write_text("#!/bin/sh\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_ffmpeg_bin_overrides_path(tmp_path, monkeypatch):
    fake = _make_exe(tmp_path, "ffmpeg")
    monkeypatch.setenv(FFMPEG_BIN_ENV, str(fake))
    assert find_ffmpeg_command("ffmpeg") == str(fake)


def test_ffprobe_bin_explicit(tmp_path, monkeypatch):
    fake = _make_exe(tmp_path, "ffprobe")
    monkeypatch.setenv(FFPROBE_BIN_ENV, str(fake))
    # FFMPEG_BIN 형제에 다른 ffprobe 가 있어도 명시 지정이 이긴다
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    _make_exe(other_dir, "ffmpeg")
    _make_exe(other_dir, "ffprobe")
    monkeypatch.setenv(FFMPEG_BIN_ENV, str(other_dir / "ffmpeg"))
    assert find_ffmpeg_command("ffprobe") == str(fake)


def test_ffprobe_inferred_from_ffmpeg_bin_sibling(tmp_path, monkeypatch):
    ffmpeg = _make_exe(tmp_path, "ffmpeg")
    sibling = _make_exe(tmp_path, "ffprobe")
    monkeypatch.setenv(FFMPEG_BIN_ENV, str(ffmpeg))
    assert find_ffmpeg_command("ffprobe") == str(sibling)


def test_ffprobe_falls_back_to_path_without_sibling(tmp_path, monkeypatch):
    # FFMPEG_BIN 옆에 ffprobe 가 없으면 PATH 검색으로 폴백한다
    ffmpeg = _make_exe(tmp_path, "ffmpeg")
    monkeypatch.setenv(FFMPEG_BIN_ENV, str(ffmpeg))
    path_dir = tmp_path / "on_path"
    path_dir.mkdir()
    path_probe = _make_exe(path_dir, "ffprobe")
    monkeypatch.setenv("PATH", str(path_dir))
    assert find_ffmpeg_command("ffprobe") == str(path_probe)


def test_bad_explicit_override_fails_fast(tmp_path, monkeypatch):
    # 오타 난 오버라이드가 PATH 의 (libass 없는) 빌드로 조용히 폴백하면
    # 렌더 마지막 단계에서야 죽는다 → 즉시 실패해야 한다
    monkeypatch.setenv(FFMPEG_BIN_ENV, str(tmp_path / "no_such_ffmpeg"))
    with pytest.raises(FileNotFoundError, match=FFMPEG_BIN_ENV):
        find_ffmpeg_command("ffmpeg")


def test_bad_explicit_ffprobe_override_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setenv(FFPROBE_BIN_ENV, str(tmp_path / "no_such_ffprobe"))
    with pytest.raises(FileNotFoundError, match=FFPROBE_BIN_ENV):
        find_ffmpeg_command("ffprobe")


def test_no_env_uses_path(tmp_path, monkeypatch):
    path_dir = tmp_path / "on_path"
    path_dir.mkdir()
    path_ffmpeg = _make_exe(path_dir, "ffmpeg")
    monkeypatch.setenv("PATH", str(path_dir))
    assert find_ffmpeg_command("ffmpeg") == str(path_ffmpeg)
