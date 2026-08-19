"""편집실 이미지 오버레이(edit_overrides/v3 images, F-408) — 필터그래프 합성 검증.

이미지는 ffmpeg overlay 로 얹는다: 좌표는 1080×1920 캔버스 비율→px(폭만 지정,
세로는 원본비 유지 = scale 높이 -2), 시간 창은 편집본 시간축의 enable=between,
layer ≤ 0 은 자막 아래(기본 0) · ≥ 1 은 자막(ASS 두 겹) 위다. 배치 자체
(place_anchored_images)는 test_edit_overrides.py 가 검증한다 — 여기는 배치가 끝난
항목이 필터그래프에 올바르게 박히는지만 본다.
"""
from __future__ import annotations

from pathlib import Path

from app.config import DesignConfig
from app.modules.renderer import RenderInputs, _build_filtergraph
from app.modules.story_builder import StoryClip


def _inputs(image_overlays, subtitle_path=None):
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=subtitle_path,
        crop_timeline_map={}, title_text="제목", work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=DesignConfig(),
        image_overlays=image_overlays)


def _img(**extra):
    base = {"file": "/run/assets/arrow.png", "start_sec": 1.0, "end_sec": 3.0,
            "x": 0.1, "y": 0.2, "w": 0.3}
    base.update(extra)
    return base


def test_no_images_no_overlay_filters():
    fg = _build_filtergraph(_inputs(None), 1, 0)
    assert "eimg" not in fg
    fg2 = _build_filtergraph(_inputs([]), 1, 0)
    assert "eimg" not in fg2


def test_image_ratio_to_px_and_time_window():
    fg = _build_filtergraph(_inputs([_img()]), 1, 0)
    assert "scale=324:-2" in fg                        # w=0.3 → 324px, 세로는 원본비
    assert "overlay=108:384" in fg                     # x=0.1→108px, y=0.2→384px
    assert "enable='between(t,1.000,3.000)'" in fg     # 편집본 시간축 창


def test_default_layer_below_subtitles(tmp_path):
    ass = tmp_path / "sub.ass"
    ass.write_text("stub", encoding="utf-8")
    fg = _build_filtergraph(_inputs([_img()], subtitle_path=ass), 1, 0)
    # 기본(layer 미지정=0) → 자막 아래: 작품명 단계 출력 위에 얹고, 그 출력이 ass 입력이 된다
    assert "[with_work][eimgbsrc0]overlay" in fg
    assert "[eimgb0]ass=" in fg


def test_positive_layer_above_subtitles(tmp_path):
    ass = tmp_path / "sub.ass"
    ass.write_text("stub", encoding="utf-8")
    fg = _build_filtergraph(_inputs([_img(layer=1)], subtitle_path=ass), 1, 0)
    # 자막 위: ASS 두 겹이 [vpretop] 으로 끝나고, 그 위에 얹은 뒤 [vout] 으로 마감
    assert "[with_work]ass=" in fg
    assert "[vpretop][eimgasrc0]overlay" in fg
    assert "[eimga0]null[vout]" in fg


def test_layer_sort_stacks_ascending_below():
    fg = _build_filtergraph(_inputs([_img(layer=0, file="/a.png"),
                                     _img(layer=-1, file="/b.png")]), 1, 0)
    # layer 오름차순 — b(-1) 가 먼저 깔리고 a(0) 가 그 위에 체인으로 쌓인다
    assert fg.index("b.png") < fg.index("a.png")
    assert "[eimgb0][eimgbsrc1]overlay" in fg
