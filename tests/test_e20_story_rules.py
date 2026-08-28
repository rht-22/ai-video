"""E20-B2·B3 스토리 규칙 회귀 가드 (2026-08-28).

B2 내레이션 갭 채움 — narration.fill_gaps(선택 bool): 대사 없는 틈마다 cue 를 배치해
   오디오가 비지 않게 하라는 프롬프트 지시(v3: max_cues 5 중 3개만 쓰고 무발화 10.5초).
B3 훅 규칙 — story.hook_max_sec(선택 3~20초)·story.hook_title_scene(선택 bool):
   v3 는 도입 설전 21초(확장 후 28초) 뒤에야 제목이 약속한 장면이 왔고, 그 장면은
   클램프에 잘려 아예 없었다. 첫 2초 사건 + 제목 장면 훅 우선 + 훅 상한.

둘 다 프롬프트 지시(LLM 가이드)다 — 강제는 A1(클램프)·B1(페이싱)이 하고, 여기는
스토리가 애초에 그렇게 고르게 만든다. 절이 없으면 블록이 종전과 같다(회귀 0).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules import style_tone as st

REPO = Path(__file__).resolve().parents[1]


def _data(**over):
    base = json.loads(
        (REPO / "app" / "data" / "style_tones" / "drama_clip_kr.json").read_text("utf-8"))
    for path, value in over.items():
        cur = base
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur[k]
        cur[keys[-1]] = value
    return base


def test_drama_clip_kr_values():
    tone = st.load_style_tone("drama_clip_kr")
    assert tone.narration["fill_gaps"] is True
    assert tone.story["hook_max_sec"] == 8
    assert tone.story["hook_title_scene"] is True


@pytest.mark.parametrize("path,value", [
    ("narration.fill_gaps", "yes"),        # bool 아님
    ("story.hook_max_sec", 1),             # 범위 밖(아래)
    ("story.hook_max_sec", 60),            # 범위 밖(위)
    ("story.hook_title_scene", 1),         # bool 아님
])
def test_invalid_values_fail(path, value):
    with pytest.raises(st.StyleToneError):
        st.validate_tone_data(_data(**{path: value}), "drama_clip_kr")


def test_optional_absence_ok():
    """세 키 모두 선택이다 — 없는 프로파일은 종전과 같은 블록을 낸다(회귀 0)."""
    data = _data()
    data["narration"].pop("fill_gaps", None)
    data["story"].pop("hook_max_sec", None)
    data["story"].pop("hook_title_scene", None)
    st.validate_tone_data(data, "drama_clip_kr")
    tone = st.StyleTone(name="t", sha12="x" * 12, narration=data["narration"],
                        labels=data["labels"], story=data["story"])
    block = st.story_prompt_block(tone)
    assert "오디오가 비지 않게" not in block
    assert "제목이 약속하는" not in block
    assert "훅 상한" not in block


def test_story_block_carries_rules():
    b = st.story_prompt_block(st.load_style_tone("drama_clip_kr"))
    assert "오디오가 비지 않게" in b                      # B2 fill_gaps
    assert "8초 이내" in b                                # B3 hook_max_sec
    assert "제목이 약속하는" in b and "첫 2초" in b       # B3 hook_title_scene


def test_profile_matches_preset():
    preset = json.loads((REPO / "docs" / "design_presets"
                         / "drama_clip_kr.preset.json").read_text("utf-8"))
    tone = st.load_style_tone("drama_clip_kr")
    assert tone.narration["fill_gaps"] is preset["narration_tts"]["fill_gaps"]["value"]
    assert tone.story["hook_max_sec"] == preset["story"]["hook_max_sec"]["value"]
    assert tone.story["hook_title_scene"] is preset["story"]["hook_title_scene"]["value"]
