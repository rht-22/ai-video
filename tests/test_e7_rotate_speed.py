"""E7 — 디자인 레벨 제목·TTS 회전(title_rotate·tts_rotate) + 영상 배속(video_speed).

계약:
· 회전: 도 단위 **시계방향 양수**, -180~180 (images[].rotate·subtitles[].style.rotate 와
  동일). 0 = 종전 경로 그대로(회귀 없음). 원점은 텍스트 블록 중심.
  - 제목: drawtext 는 회전이 없어 투명 캔버스 → drawtext 줄 묶음 → rotate → overlay.
  - TTS 자막: ASS 렌더라 \\frz(부호 반전 = 엔진 책임, F-410) + \\org(블록 중심) 주입.
· 배속: 0.8~2.0. 렌더 그래프의 concat 직후 setpts/atempo 한 번(구현 지점 A) —
  원본·현장음만 배속, TTS 내레이션 오디오는 그대로. 그 뒤의 시간 좌표(ASS 이벤트·
  이미지 enable 창·adelay·덕킹 창)는 전부 출력 시각(×1/S)이다.
· 범위 밖 값은 CLI·엔진 양쪽에서 즉시 실패(조용한 무시 금지).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import _build_design_config, build_parser
from app.config import DesignConfig
from app.modules.renderer import RenderInputs, _build_filtergraph
from app.modules.story_builder import StoryClip
from app.modules.subtitle import SubtitleStyle, build_tts_ass
from app.pipeline import _scale_segments_for_speed


def _design(*extra_args: str) -> DesignConfig:
    p = build_parser()
    args = p.parse_args([
        "create_shorts", "--title", "T", "--video", "x.mp4", "--subtitle", "x.srt",
        *extra_args,
    ])
    return _build_design_config(args)


def _inputs(design=None, image_overlays=None, tts_cue_files=None, title="제목"):
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
        crop_timeline_map={}, title_text=title, work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170,
        design=design or DesignConfig(),
        image_overlays=image_overlays, tts_cue_files=tts_cue_files)


# ── 기본값 ──────────────────────────────────────────────────────────────

def test_defaults_are_neutral():
    d = DesignConfig()
    assert d.title_rotate == 0.0
    assert d.tts_rotate == 0.0
    assert d.video_speed == 1.0


# ── CLI 배선 + 범위 검증 ────────────────────────────────────────────────

def test_cli_rotate_and_speed_flags():
    assert _design("--design-title-rotate", "12.5").title_rotate == 12.5
    assert _design("--design-tts-rotate", "-8").tts_rotate == -8.0
    assert _design("--design-video-speed", "1.25").video_speed == 1.25


def test_cli_rotate_boundaries_ok():
    assert _design("--design-title-rotate", "-180").title_rotate == -180.0
    assert _design("--design-tts-rotate", "180").tts_rotate == 180.0
    assert _design("--design-video-speed", "0.8").video_speed == 0.8
    assert _design("--design-video-speed", "2.0").video_speed == 2.0


def test_cli_out_of_range_fails_loud():
    with pytest.raises(ValueError):
        _design("--design-title-rotate", "180.1")
    with pytest.raises(ValueError):
        _design("--design-tts-rotate", "-181")
    with pytest.raises(ValueError):
        _design("--design-video-speed", "0.5")
    with pytest.raises(ValueError):
        _design("--design-video-speed", "2.01")


def test_renderer_rejects_out_of_range_too():
    # CLI 를 안 거친 호출(파이프라인 재개·프로그램 사용)도 렌더 경계에서 즉시 실패
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(DesignConfig(video_speed=0.5)), 1, 0)
    with pytest.raises(ValueError):
        _build_filtergraph(_inputs(DesignConfig(title_rotate=200.0)), 1, 0)
    with pytest.raises(ValueError):
        build_tts_ass([], Path("/tmp/x.ass"), SubtitleStyle(), rotate_deg=181.0)


# ── E7-1 제목 회전 (투명 캔버스 → rotate → overlay, 중심 고정) ──────────

def test_title_rotate_zero_keeps_legacy_drawtext():
    fg = _build_filtergraph(_inputs(), 1, 0)
    # 종전 경로: 메인 체인 위 drawtext, 동적 배치 y=330 (overlay_y 420 - 70 - 20)
    assert "y=330[title_0]" in fg
    assert "[ttl0]" not in fg and "ttlrot" not in fg


def test_title_rotate_uses_canvas_rotate_overlay_center_fixed():
    fg = _build_filtergraph(_inputs(DesignConfig(title_rotate=90.0)), 1, 0)
    # 제목 "제목"(1줄, 70px): 패딩 35 대칭 → 캔버스 1080x140, 상단 y = 330-35 = 295
    assert "color=c=black@0.0:s=1080x140:d=1,format=rgba[ttl0]" in fg
    assert "y=35[ttl1]" in fg                      # 캔버스 내부 y = 330-295
    # 90°: bb = 140x1080, 중심 (540, 295+70=365) 고정 → overlay (540-70, 365-540)
    assert ":ow=140:oh=1080:c=black@0[ttlrot]" in fg
    assert "[ttlrot]overlay=470:-175[with_title]" in fg
    # 회전 경로에서는 메인 체인 직결 drawtext 가 없어야 한다
    assert "[title_0]" not in fg


def test_title_rotate_clockwise_positive_radians():
    import math
    fg = _build_filtergraph(_inputs(DesignConfig(title_rotate=45.0)), 1, 0)
    # ffmpeg rotate 는 시계방향 양수(F-410 실측) — 계약 부호 그대로 라디안
    assert f"rotate={math.radians(45.0):.10f}:" in fg


def test_title_rotate_multiline_one_canvas():
    # 두 줄 제목도 캔버스 하나에 묶어 그린 뒤 한 번만 회전한다(줄별 개별 회전 아님)
    fg = _build_filtergraph(
        _inputs(DesignConfig(title_rotate=10.0), title="첫줄맥락\n둘째줄후킹"), 1, 0)
    assert fg.count("rotate=") == 1
    assert "[ttl1]drawtext" in fg                  # 두 번째 줄도 캔버스 체인에
    assert "[ttl2]rotate=" in fg


# ── E7-1 TTS 회전 (ASS \frz 부호 반전 + \org 블록 중심) ─────────────────

def _tts_ass(tmp_path, rotate, text="내레이션 한 줄", margin_v=580, font_size=70):
    out = tmp_path / "tts.ass"
    style = SubtitleStyle(font_size=font_size, margin_v=margin_v)
    segs = [SimpleNamespace(start_sec=1.0, end_sec=3.0, text=text)]
    build_tts_ass(segs, out, style, rotate_deg=rotate)
    return out.read_text(encoding="utf-8-sig")


def test_tts_rotate_zero_no_tags(tmp_path):
    content = _tts_ass(tmp_path, 0.0)
    assert "\\frz" not in content and "\\org" not in content


def test_tts_rotate_sign_flip_and_center_org(tmp_path):
    content = _tts_ass(tmp_path, 8.0)
    # 계약 시계방향 양수 → ASS \frz 는 반시계 양수라 부호 반전(엔진 책임)
    # 1줄 70px: 블록 높이 84(×1.2) → 중심 y = 1920-580-42 = 1298, x = 화면 중앙 540
    assert "{\\frz-8\\org(540,1298)}" in content


def test_tts_rotate_negative_flips_positive(tmp_path):
    assert "\\frz8\\org" in _tts_ass(tmp_path, -8.0)


def test_tts_rotate_two_line_block_center(tmp_path):
    # 15자 초과 → \N 두 줄: 블록 높이 168 → 중심 y = 1920-580-84 = 1256
    content = _tts_ass(tmp_path, 8.0, text="열다섯 자를 넘기는 티티에스 내레이션 문장")
    assert "\\N" in content
    assert "\\org(540,1256)}" in content


def test_tts_rotate_zero_output_identical_to_legacy(tmp_path):
    # rotate=0 은 종전 출력과 바이트 동일해야 한다(회귀 없음)
    a = tmp_path / "a.ass"
    b = tmp_path / "b.ass"
    style = SubtitleStyle(font_size=70, margin_v=580)
    segs = [SimpleNamespace(start_sec=0.0, end_sec=2.0, text="확인")]
    build_tts_ass(segs, a, style)
    build_tts_ass(segs, b, style, rotate_deg=0.0)
    assert a.read_bytes() == b.read_bytes()


# ── E7-2 영상 배속 — setpts/atempo 위치와 출력 시각(×1/S) 정합 ──────────

def test_speed_one_no_speed_filters():
    fg = _build_filtergraph(_inputs(), 1, 0)
    assert "setpts" not in fg and "atempo" not in fg
    assert "[vcat]drawtext" in fg                  # 제목이 concat 출력에 직결


def test_speed_setpts_after_concat_atempo_on_original_audio():
    fg = _build_filtergraph(_inputs(DesignConfig(video_speed=1.25)), 1, 0)
    assert "[vcat]setpts=PTS/1.25[vspd]" in fg
    assert "[vspd]drawtext" in fg                  # 이후 체인은 배속된 스트림 위
    assert "atempo=1.25,volume=" in fg             # 원본 오디오만 배속
    assert fg.count("setpts") == 1 and fg.count("atempo") == 1


def test_speed_tts_cue_windows_scaled_not_tempoed():
    cues = [{"cue_index": 0, "path": "cue0.mp3",
             "cue": {"start_sec": 10.0, "end_sec": 15.0, "text": "나레이션"}}]
    fg = _build_filtergraph(_inputs(DesignConfig(video_speed=1.25),
                                    tts_cue_files=cues), 1, 1)
    # 덕킹 창·adelay 는 출력 시각(×1/1.25): 10→8, 15→12
    assert "between(t,8.000,12.000)" in fg
    assert "adelay=8000|8000" in fg
    # 내레이션 입력([1:a] = cue)에는 atempo 가 붙지 않는다 — 원본([acat])에만 1회
    assert "[1:a]volume=" in fg
    assert fg.count("atempo") == 1 and "[acat]atempo=1.25," in fg


def test_speed_image_overlay_windows_scaled():
    img = {"file": "/run/assets/arrow.png", "start_sec": 1.0, "end_sec": 3.0,
           "x": 0.1, "y": 0.2, "w": 0.3}
    fg = _build_filtergraph(_inputs(DesignConfig(video_speed=1.25),
                                    image_overlays=[img]), 1, 0)
    assert "enable='between(t,0.800,2.400)'" in fg


def test_speed_08_slows_expands_times():
    cues = [{"cue_index": 0, "path": "cue0.mp3",
             "cue": {"start_sec": 8.0, "end_sec": 12.0, "text": "n"}}]
    fg = _build_filtergraph(_inputs(DesignConfig(video_speed=0.8),
                                    tts_cue_files=cues), 1, 1)
    assert "[vcat]setpts=PTS/0.8[vspd]" in fg
    assert "atempo=0.8," in fg
    assert "between(t,10.000,15.000)" in fg        # 8/0.8, 12/0.8
    assert "adelay=10000|10000" in fg


# ── E7-2 자막 세그먼트 출력 시각 변환 (순수 헬퍼) ───────────────────────

def test_scale_segments_divides_times_preserves_attrs():
    segs = [SimpleNamespace(start_sec=10.0, end_sec=15.0, text="대사",
                            style={"rotate": 4})]
    out = _scale_segments_for_speed(segs, 2.0)
    assert out[0].start_sec == 5.0 and out[0].end_sec == 7.5
    assert out[0].text == "대사" and out[0].style == {"rotate": 4}
    # 원본(소스 편집 타임라인 — 편집실이 읽는 캐시의 원료)은 불변
    assert segs[0].start_sec == 10.0 and segs[0].end_sec == 15.0


def test_scale_segments_speed_one_passthrough():
    segs = [SimpleNamespace(start_sec=1.0, end_sec=2.0, text="t")]
    out = _scale_segments_for_speed(segs, 1.0)
    assert out[0] is segs[0]
