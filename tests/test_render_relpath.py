"""renderer._relpath_or_abs — 상대경로 입력이 cwd=output_dir 인 ffmpeg 에서 깨지지 않는지.

2026-09-03 실사고: `python -m app.v3 --video sources_local/jigeum_EP02.mp4` 로 돌린 편이
분석·초안까지 다 지나고 **최종 렌더에서만** "No such file or directory" 로 죽었다.
renderer 는 Windows 경로 호환을 위해 ffmpeg 를 cwd=output_dir 로 띄우고 입력을 상대경로로
넘기는데, 입력이 애초에 상대경로면 relative_to 가 실패해 그 상대경로가 그대로 나갔다.
"""
from pathlib import Path

from app.modules.renderer import _relpath_or_abs


def test_relative_input_is_anchored_to_process_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sources_local").mkdir()
    src = tmp_path / "sources_local" / "x.mp4"
    src.write_bytes(b"")
    outdir = tmp_path / "outputs" / "job"
    outdir.mkdir(parents=True)

    out = _relpath_or_abs(Path("sources_local/x.mp4"), outdir)
    # cwd 가 outdir 로 바뀌어도 열리는 경로여야 한다
    assert (outdir / out).resolve() == src.resolve()


def test_absolute_input_unchanged(tmp_path):
    outdir = tmp_path / "outputs" / "job"
    outdir.mkdir(parents=True)
    inside = outdir / "cue.mp3"
    assert _relpath_or_abs(inside, outdir) == Path("cue.mp3")          # 종전: 안이면 상대
    outside = tmp_path / "elsewhere.mp4"
    assert _relpath_or_abs(outside, outdir) == outside                  # 종전: 밖이면 절대
