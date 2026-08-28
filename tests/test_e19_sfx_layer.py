"""E19-5 효과음(SFX) 레이어 회귀 가드.

발주서: docs/prompts/e19-drama-clip-preset.md §5. 벤치마크 실측(보고서 §12): SFX 는
편당 1~3건, 큰 비트에만 — 절제가 문법이다. 계약 요점:

- **스티커 규율 그대로**: `assets/sfx/manifest.json` 의 **id 만** AI 에 노출,
  빈 목록이 정상 상태, 없는 id 는 그 항목만 드롭+로그.
- 게이트: style_compose + **톤 프로파일에 sfx 절이 있을 때만**. 미지정이면 입력도
  필터도 종전과 동일(회귀 0 — 필터그래프 문자열로 고정).
- 렌더: cue 와 같은 방식(입력 추가 + volume dB + adelay + amix). 입력 인덱스 =
  클립 수 + cue 수 + si — **같은 목록을 입력과 필터가 봐야** 어긋나지 않는다.
- 영상 밖에서 시작하는 sfx 는 cue 와 같은 안전망으로 드롭.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import DesignConfig
from app.modules import style_compose as sc
from app.modules import style_tone as st
from app.modules.renderer import (
    RenderInputs,
    _build_audio_filter,
    _build_filtergraph,
    sfx_within_video,
)
from app.modules.story_builder import StoryClip

REPO = Path(__file__).resolve().parents[1]


def _clip(start=0.0, end=10.0):
    return StoryClip(role="hook", start_sec=start, end_sec=end, subtitle="s",
                     use_original_audio=True)


def _inputs(design=None, **over):
    return RenderInputs(
        video_path=Path("src.mp4"), clips=[_clip()], subtitle_path=None,
        crop_timeline_map={}, title_text="제목", work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170,
        design=design or DesignConfig(), **over)


def _sfx_root(tmp_path):
    d = tmp_path / "app_root" / "assets" / "sfx"
    d.mkdir(parents=True)
    (d / "bang.mp3").write_bytes(b"ID3" + b"0" * 64)
    manifest = {"sfx": [{"id": "bang", "file": "bang.mp3", "desc": "쾅 — 임팩트"}]}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path / "app_root"


# ══════════════════════════════════════════════════════════════════════════
# 톤 프로파일 sfx 절 — 게이트의 정본
# ══════════════════════════════════════════════════════════════════════════
def test_tone_sfx_section_loaded_and_matches_preset():
    tone = st.load_style_tone("drama_clip_kr")
    assert tone.sfx is not None
    preset = json.loads((REPO / "docs" / "design_presets"
                         / "drama_clip_kr.preset.json").read_text("utf-8"))["sfx"]
    assert tone.sfx["max_per_episode"] == preset["max_per_episode"]["value"]
    assert tone.sfx["mix_gain_db"] == preset["mix_gain_db"]["value"]
    assert tone.sfx["target_beats"] == preset["target_beats"]["value"]


def test_tone_sfx_section_optional_and_validated():
    base = json.loads((REPO / "app" / "data" / "style_tones"
                       / "drama_clip_kr.json").read_text("utf-8"))
    no_sfx = dict(base)
    no_sfx.pop("sfx")
    st.validate_tone_data(no_sfx, "drama_clip_kr")          # sfx 없음 = SFX 닫힘(유효)
    for bad in ({"max_per_episode": 0, "mix_gain_db": -6, "target_beats": ["rage"]},
                {"max_per_episode": 3, "mix_gain_db": 5, "target_beats": ["rage"]},
                {"max_per_episode": 3, "mix_gain_db": -6, "target_beats": ["bgm"]}):
        data = dict(base)
        data["sfx"] = bad
        with pytest.raises(st.StyleToneError):
            st.validate_tone_data(data, "drama_clip_kr")


# ══════════════════════════════════════════════════════════════════════════
# manifest · 프롬프트 블록 — 스티커 규율 그대로
# ══════════════════════════════════════════════════════════════════════════
def test_sfx_manifest_empty_is_normal(tmp_path):
    assert sc.load_sfx_manifest(tmp_path) == {}


def test_sfx_manifest_and_prompt_block(tmp_path):
    root = _sfx_root(tmp_path)
    m = sc.load_sfx_manifest(root)
    assert "bang" in m
    block = sc.sfx_prompt_block(m, max_sfx=3, gain_db=-6.0,
                                target_beats=["rage", "surprise", "action_foley"])
    assert "bang" in block and "3개" in block and "-6" in block
    assert "라벨" in block                                   # 라벨 등장에 소리 금지 규칙


# ══════════════════════════════════════════════════════════════════════════
# validate_plan — id 검증·캡·스테이징·게이트
# ══════════════════════════════════════════════════════════════════════════
def _plan_sfx(items):
    return {"schema": "style_plan/v1", "sfx": items}


def test_validate_sfx_stages_and_normalizes(tmp_path):
    root = _sfx_root(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out, notes = sc.validate_plan(
        _plan_sfx([{"id": "bang", "source_time_sec": 105.0, "gain_db": -8,
                    "reason": "분노 비트"}]),
        manifest={}, app_root=root, run_dir=run_dir,
        sfx_manifest=sc.load_sfx_manifest(root), max_sfx=3, sfx_gain_db=-6.0)
    assert out["sfx"] == [{"file": "style_assets/bang.mp3",
                           "source_time_sec": 105.0, "gain_db": -8.0}]
    assert (run_dir / "style_assets" / "bang.mp3").is_file()


def test_validate_sfx_unknown_id_dropped_and_capped(tmp_path):
    root = _sfx_root(tmp_path)
    items = ([{"id": "bang", "source_time_sec": float(i)} for i in range(5)]
             + [{"id": "no_such", "source_time_sec": 1.0}])
    out, notes = sc.validate_plan(
        _plan_sfx(items), manifest={}, app_root=root, run_dir=tmp_path,
        sfx_manifest=sc.load_sfx_manifest(root), max_sfx=3, sfx_gain_db=-6.0)
    assert len(out["sfx"]) == 3                              # 캡(앞에서부터)
    assert all(s["gain_db"] == -6.0 for s in out["sfx"])     # 기본 gain
    assert any("상한" in n for n in notes)


def test_validate_sfx_closed_channel_drops_all(tmp_path):
    """톤에 sfx 절이 없으면(max_sfx=None) 항목 전량 드롭 + 기록 — 조용한 무시 금지."""
    root = _sfx_root(tmp_path)
    out, notes = sc.validate_plan(
        _plan_sfx([{"id": "bang", "source_time_sec": 1.0}]),
        manifest={}, app_root=root, run_dir=tmp_path)
    assert "sfx" not in out or not out["sfx"]
    assert any("sfx" in n.lower() or "효과음" in n for n in notes)


def test_validate_sfx_gain_clamped(tmp_path):
    root = _sfx_root(tmp_path)
    out, notes = sc.validate_plan(
        _plan_sfx([{"id": "bang", "source_time_sec": 1.0, "gain_db": 20}]),
        manifest={}, app_root=root, run_dir=tmp_path,
        sfx_manifest=sc.load_sfx_manifest(root), max_sfx=3, sfx_gain_db=-6.0)
    assert out["sfx"][0]["gain_db"] == 0.0                   # 상한 0dB 로 클램프+기록
    assert any("gain" in n for n in notes)


# ══════════════════════════════════════════════════════════════════════════
# 렌더 — 입력 인덱스·amix·회귀 0·late 가드
# ══════════════════════════════════════════════════════════════════════════
def _sfx(start=2.0, gain=-6.0):
    return {"path": "style_assets/bang.mp3", "start_sec": start, "gain_db": gain}


def test_audio_filter_sfx_only():
    af = _build_audio_filter(_inputs(sfx_audio=[_sfx()]), 1, 0)
    assert "[1:a]volume=-6dB[sfx0_vol]" in af               # 입력 idx = 클립 1 + cue 0
    assert "adelay=2000|2000" in af
    assert "amix=inputs=2" in af


def test_audio_filter_sfx_after_cues(tmp_path):
    cue = {"path": tmp_path / "c.mp3", "cue": {"start_sec": 1.0, "end_sec": 2.0}}
    af = _build_audio_filter(_inputs(tts_cue_files=[cue], sfx_audio=[_sfx()]), 2, 1)
    assert "[2:a]volume=-4dB[cue0_vol]" in af               # cue idx = 클립 수(2)
    assert "[3:a]volume=-6dB[sfx0_vol]" in af               # sfx idx = 클립 2 + cue 1
    assert "amix=inputs=3" in af


def test_audio_filter_without_sfx_unchanged():
    """sfx 미지정 = 종전 문자열 그대로(회귀 0) — 'sfx' 라는 글자조차 없어야 한다."""
    assert "sfx" not in _build_audio_filter(_inputs(), 1, 0)
    cue = {"path": "c.mp3", "cue": {"start_sec": 1.0, "end_sec": 2.0}}
    assert "sfx" not in _build_audio_filter(_inputs(tts_cue_files=[cue]), 1, 1)


def test_filtergraph_identical_without_sfx():
    fg_a = _build_filtergraph(_inputs(), 1, 0)
    fg_b = _build_filtergraph(_inputs(sfx_audio=None), 1, 0)
    assert fg_a == fg_b and "sfx" not in fg_a


def test_sfx_late_guard_drops_out_of_video():
    kept, dropped = sfx_within_video([_sfx(start=2.0), _sfx(start=99.0)],
                                     [_clip(0.0, 10.0)], 1.0)
    assert len(kept) == 1 and kept[0]["start_sec"] == 2.0
    assert len(dropped) == 1 and dropped[0]["start_sec"] == 99.0


def test_sfx_gain_zero_db():
    af = _build_audio_filter(_inputs(sfx_audio=[_sfx(gain=0.0)]), 1, 0)
    assert "volume=0dB[sfx0_vol]" in af


# ══════════════════════════════════════════════════════════════════════════
# 배선 — 게이트·기록·첫 variant 한정 (소스 문자열 고정)
# ══════════════════════════════════════════════════════════════════════════
def test_pipeline_wiring():
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert "load_sfx_manifest" in src
    assert "sfx_manifest=" in src and "max_sfx=" in src      # validate 와 프롬프트가 같은 캡
    assert '"sfx_used"' in src and '"sfx_dropped"' in src    # run_log style 단계
    # RenderInputs 에 sfx_audio 는 **메인 렌더 한 곳** — variant 렌더는 받지 않는다
    assert src.count("sfx_audio=") == 1
    # 렌더 직전 late 가드가 한 번 돈다
    rsrc = (REPO / "app" / "modules" / "renderer.py").read_text("utf-8")
    assert rsrc.count("sfx_within_video(") >= 2              # 정의 + render_video 호출
