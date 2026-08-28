"""E19-1 스타일 톤 프로파일 회귀 가드.

발주서: docs/prompts/e19-drama-clip-preset.md §1. 세 가지를 못박는다:

1. **회귀 0** — 톤 미지정이면 프롬프트 블록은 빈 문자열이고, E15 하드캡(MAX_TEXTS 8)이
   그대로다. PipelineInput·CLI 기본값도 None 이다.
2. **fail-loud** — 없는 이름·깨진 파일·모르는 enum·범위 밖 값은 StyleToneError.
   조용히 기본 톤으로 떨어지는 경로가 없다.
3. **값 정본 일치** — app/data/style_tones/drama_clip_kr.json 은
   docs/design_presets/drama_clip_kr.preset.json(status:"prompt" 항목)의 사본이다.
   두 파일이 갈리면 여기서 잡는다(발주서 §1 "테스트가 두 파일을 대조한다").
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import build_parser
from app.modules import style_compose as sc
from app.modules import style_tone as st

REPO = Path(__file__).resolve().parents[1]


def _load():
    return st.load_style_tone("drama_clip_kr")


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


# ══════════════════════════════════════════════════════════════════════════
# 회귀 0 — 미지정이면 아무 일도 없다
# ══════════════════════════════════════════════════════════════════════════
def test_blocks_empty_when_no_tone():
    """None 톤의 블록은 빈 문자열 — 프롬프트에 += 해도 한 글자도 안 바뀐다."""
    assert st.story_prompt_block(None) == ""
    assert st.style_prompt_block(None) == ""


def test_defaults_are_off():
    from app.pipeline import PipelineInput
    assert PipelineInput.__dataclass_fields__["style_tone"].default is None
    p = build_parser()
    args = p.parse_args(["create_shorts", "--title", "T", "--video", "x.mp4",
                         "--subtitle", "x.srt"])
    assert getattr(args, "style_tone", None) is None


def test_cap_default_unchanged_without_tone(tmp_path):
    """validate_plan(max_texts 미지정) = 종전 MAX_TEXTS(8) 그대로."""
    many = [{"text": f"t{i}", "source_time_sec": 105.0, "duration_sec": 0.5,
             "x": 0.5, "y": 0.3} for i in range(sc.MAX_TEXTS + 6)]
    out, notes = sc.validate_plan({"schema": "style_plan/v1", "texts": many},
                                  manifest={}, app_root=tmp_path, run_dir=tmp_path)
    assert len(out["texts"]) == sc.MAX_TEXTS


# ══════════════════════════════════════════════════════════════════════════
# 로드·검증 — fail-loud
# ══════════════════════════════════════════════════════════════════════════
def test_load_drama_clip_kr():
    tone = _load()
    assert tone.name == "drama_clip_kr"
    assert len(tone.sha12) == 12
    assert tone.density_max == 14
    assert tone.narration["tone"] == "recall_first_person"


def test_unknown_name_fails_and_lists_available():
    with pytest.raises(st.StyleToneError) as e:
        st.load_style_tone("no_such_tone")
    assert "drama_clip_kr" in str(e.value)        # 오타를 한 번에 잡게 목록을 싣는다


def test_bad_name_format_fails():
    with pytest.raises(st.StyleToneError):
        st.load_style_tone("../etc/passwd")


@pytest.mark.parametrize("path,value", [
    ("schema", "style_tone/v2"),
    ("name", "copied_from_somewhere"),           # 파일명·name 불일치 = 복사 후 갱신 누락
    ("narration.tone", "explainer"),             # 모르는 enum
    ("narration.max_cues", 0),
    ("narration.cue_len_chars", [12, 3]),        # 최소>최대
    ("labels.density_max", st.DENSITY_MAX_LIMIT + 1),
    ("labels.categories", ["state_paren", "bgm"]),
    ("labels.color_map", {"state": "#FF9EC4"}),  # 키 부족
    ("labels.duration_sec", [0.6, 99.0]),
    ("story.ending", "fade_out"),
])
def test_invalid_values_fail(path, value):
    with pytest.raises(st.StyleToneError):
        st.validate_tone_data(_data(**{path: value}), "drama_clip_kr")


def test_color_map_requires_hex():
    with pytest.raises(st.StyleToneError):
        st.validate_tone_data(_data(**{"labels.color_map": {
            "state": "pink", "comment": "#FFE24A", "tsukkomi": "#FF4632",
            "laugh": "#FFFFFF", "positive": "#7DFF9E"}}), "drama_clip_kr")


# ══════════════════════════════════════════════════════════════════════════
# 값 정본 일치 — 프리셋(preset.json) ↔ 프로파일(style_tones/*.json)
# ══════════════════════════════════════════════════════════════════════════
def test_profile_matches_preset():
    """프리셋의 status:"prompt" 값이 정본이다 — 옮겨 적다 갈리면 여기서 잡는다."""
    preset = json.loads((REPO / "docs" / "design_presets"
                         / "drama_clip_kr.preset.json").read_text("utf-8"))
    tone = _load()

    n = preset["narration_tts"]
    assert tone.narration["tone"] == n["tone"]["value"]
    assert tone.narration["max_cues"] == n["max_cues"]["value"]
    assert tone.narration["cue_len_chars"] == n["cue_len_chars"]["value"]
    assert tone.narration["relay_rule"] is n["relay_rule"]["value"]
    assert tone.narration["placement"] == n["placement"]["value"]

    lf = preset["labels_fx"]
    assert tone.labels["categories"] == lf["categories"]["value"]
    assert tone.labels["color_map"] == lf["color_map"]["value"]
    assert tone.labels["density_max"] == lf["density_max"]["value"]
    assert tone.labels["duration_sec"] == lf["duration_sec"]["value"]
    for k in ("sync_to_speech", "fill_gaps", "payoff_clean", "running_gag"):
        assert tone.labels[k] is lf[k]["value"]

    s, ap = preset["story"], preset["audio_pacing"]
    assert tone.story["structure"] == s["structure"]["value"]
    assert tone.story["title_tone"] == s["title_tone"]["value"]
    assert tone.story["ending"] == ap["ending"]["value"]
    assert tone.story["payoff_longtake"] is ap["payoff_longtake"]["value"]


# ══════════════════════════════════════════════════════════════════════════
# 프롬프트 블록 — 값이 실제로 문구에 실리는지
# ══════════════════════════════════════════════════════════════════════════
def test_story_block_carries_values():
    b = st.story_prompt_block(_load())
    assert "drama_clip_kr" in b
    assert "1인칭 회상체" in b and "고민하는데" in b        # tone=recall_first_person
    assert "3~12자" in b                                    # cue_len_chars
    assert "5개 이하" in b                                  # max_cues
    assert "대사가 없는 틈" in b                            # placement
    assert "받아치는" in b                                  # relay_rule
    assert "즉시" in b and "rule of three" in b             # ending·structure


def test_style_block_carries_values():
    b = st.style_prompt_block(_load())
    assert "상한은 14개" in b                               # density_max
    assert "#FF9EC4" in b and "#FFE24A" in b                # color_map
    assert "0.6~2.5초" in b                                 # duration_sec
    assert "웃참 2222" in b and "(폐급 선임)" in b          # categories 예시 어휘
    assert "러닝 개그" in b and "페이오프" in b             # running_gag·payoff_clean


def test_cap_override_applies(tmp_path):
    """톤의 density_max 가 E15 하드캡을 대체한다 — 프롬프트 상한(compose_style
    max_texts 인자)과 검증기(validate_plan max_texts)가 같은 숫자를 본다."""
    tone = _load()
    many = [{"text": f"t{i}", "source_time_sec": 105.0, "duration_sec": 0.5,
             "x": 0.5, "y": 0.3} for i in range(tone.density_max + 2)]
    out, notes = sc.validate_plan(
        {"schema": "style_plan/v1", "texts": many},
        manifest={}, app_root=tmp_path, run_dir=tmp_path, max_texts=tone.density_max)
    assert len(out["texts"]) == tone.density_max            # 8 이 아니라 14
    assert any("상한" in n for n in notes)


# ══════════════════════════════════════════════════════════════════════════
# 배선 — CLI 플래그·파이프라인 인자
# ══════════════════════════════════════════════════════════════════════════
def test_cli_flag_parses():
    p = build_parser()
    args = p.parse_args(["create_shorts", "--title", "T", "--video", "x.mp4",
                         "--subtitle", "x.srt", "--style-tone", "drama_clip_kr"])
    assert args.style_tone == "drama_clip_kr"


def test_pipeline_wires_tone_blocks():
    """파이프라인이 story·style 호출에 톤 블록·캡을 실제로 넘기는지 소스로 못박는다
    (E15 의 문자열 가드와 같은 방식 — 배선이 빠지면 톤이 조용히 무시된다)."""
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert "style_tone_block=(_tonemod.story_prompt_block(style_tone_profile)" in src
    # style 쪽 블록은 E19-5 부터 SFX 절이 뒤에 이어진다 — 톤 블록 호출 자체를 고정한다.
    assert "_tonemod.style_prompt_block(style_tone_profile)" in src
    assert src.count("max_texts=(style_tone_profile.density_max") == 2   # compose+validate
    assert 'run_log["provenance"]["style_tone"]' in src
