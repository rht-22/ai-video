"""편집실 자유 텍스트(edit_overrides/v3 texts, F-411) — ASS 생성 + 필터그래프 합성 검증.

텍스트는 대사 자막과 별개의 ASS 한 장으로 입힌다: 이벤트마다 \\an5\\pos(중심) ·
\\fs·\\1c·\\3c·\\bord·\\frz(부호 반전) · 등장효과 \\t. 렌더 그래프에서는 대사·TTS 자막
위, layer≥1 이미지 아래다. 배치(place_anchored_texts)는 test_edit_overrides.py 가 본다.
"""
from __future__ import annotations

from pathlib import Path

from app.config import DesignConfig
from app.modules.renderer import RenderInputs, _build_filtergraph
from app.modules.story_builder import StoryClip
from app.modules.subtitle import build_texts_ass


def _placed(**extra):
    base = {"text": "쾅!!", "start_sec": 1.0, "end_sec": 2.2, "x": 0.6, "y": 0.4,
            "size": 96, "color": "#FFDD00", "stroke": "dark", "fx": "none",
            "rotate": 0.0, "font": "Jalnan"}
    base.update(extra)
    return base


def _events(path: Path) -> list[str]:
    body = path.read_text(encoding="utf-8-sig").split("[Events]")[1]
    return [ln for ln in body.splitlines() if ln.startswith("Dialogue:")]


def test_texts_ass_positions_and_styles(tmp_path):
    out = tmp_path / "texts.ass"
    build_texts_ass([_placed(), _placed(text="*생방송 중\n(자막 없음)", x=0.5, y=0.25,
                                        size=40, color="#FFFFFF", stroke="none",
                                        font="mulmaru", rotate=-8)], out)
    ev = _events(out)
    assert len(ev) == 2
    # 중심 좌표 → 캔버스 px(1080×1920), 크기·색(BGR)·테두리(크기 비례)
    assert "\\an5\\pos(648,768)" in ev[0] and "\\fs96" in ev[0]
    assert "\\1c&H00DDFF&" in ev[0] and "\\3c&H00000000" in ev[0] and "\\bord7" in ev[0]
    assert "\\fnJalnan 2 TTF" in ev[0] and "\\frz" not in ev[0]      # rotate 0 → 태그 없음
    assert ev[0].startswith("Dialogue: 2,0:00:01.00,0:00:02.20,Text,")
    # 둘째: 폰트 패밀리명 변환 · 테두리 없음 · 회전 부호 반전(계약 -8 → \frz8) · 줄바꿈 \N
    assert "\\fnMulmaru" in ev[1] and "\\bord0" in ev[1] and "\\frz8" in ev[1]
    assert ev[1].endswith("*생방송 중\\N(자막 없음)")


def test_texts_ass_fx_and_speed_and_escape(tmp_path):
    out = tmp_path / "texts.ass"
    build_texts_ass([_placed(fx="pop"), _placed(fx="shake", rotate=10),
                     _placed(text="{태그}주입")], out, speed=2.0)
    ev = _events(out)
    # 배속 ×1/S — 1.0~2.2 → 0.5~1.1
    assert ev[0].startswith("Dialogue: 2,0:00:00.50,0:00:01.10,")
    assert "\\fscx30\\fscy30\\t(0,140,\\fscx110\\fscy110)" in ev[0]
    # shake 는 \frz 기준각(부호 반전된 -10) 주위로 흔든 뒤 제자리
    assert "\\t(0,70,\\frz-4)" in ev[1] and "\\t(210,280,\\frz-10)" in ev[1]
    # 중괄호는 전각으로 — ASS 태그 주입 차단
    assert ev[2].endswith("｛태그｝주입")


def test_texts_ass_empty_writes_header_only(tmp_path):
    out = tmp_path / "texts.ass"
    build_texts_ass([], out)
    assert _events(out) == [] and "[Events]" in out.read_text(encoding="utf-8-sig")


def _inputs(text_ass, image_overlays=None, subtitle_path=None):
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                     use_original_audio=True)
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[clip], subtitle_path=subtitle_path,
        crop_timeline_map={}, title_text="제목", work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170, design=DesignConfig(),
        image_overlays=image_overlays, text_subtitle_path=text_ass)


def test_no_texts_graph_unchanged(tmp_path):
    fg = _build_filtergraph(_inputs(None), 1, 0)
    assert "[vpretxt]" not in fg and "null[vout]" in fg


def test_text_layer_sits_above_subtitles_below_top_images(tmp_path):
    tass = tmp_path / "texts.ass"; tass.write_text("stub", encoding="utf-8")
    fg = _build_filtergraph(_inputs(tass), 1, 0)
    # 대사·TTS 자막 → [vpretxt] → 텍스트 ASS → [vout]
    assert "null[vpretxt]" in fg and "[vpretxt]ass=" in fg and "texts.ass" in fg
    assert fg.count("[vout]") == 1
    # layer≥1 이미지가 있으면 텍스트 위에 얹는다
    img = {"file": "/run/a.png", "start_sec": 1.0, "end_sec": 3.0, "x": 0.1, "y": 0.2,
           "w": 0.3, "layer": 1}
    fg2 = _build_filtergraph(_inputs(tass, [img]), 1, 0)
    assert "[vpretxt]ass=" in fg2 and "[vpretop][eimgasrc0]overlay" in fg2
    assert "[eimga0]null[vout]" in fg2
