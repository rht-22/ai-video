"""E19-9 단일 줄 자막 회귀 가드 (2026-08-28 사용자 지시).

지시: "자막이 한 줄이상 넘어가면 안돼. 오디오 싱크에 맞게 한 줄 씩 나오게 해줘.
tts도 마찬가지고, 그렇다고 자막이 단어 중간에 끊기면 안돼. 띄어쓰기가 들어가는
곳으로 잘라야해."

세 가지를 못박는다:

1. **분할 규칙** — 어절(공백) 경계에서만 자른다. 단어 중간 절단 금지. max 를 넘는
   단일 어절은 통째로 남는다(자르지 않는다). 시간은 글자 수 비례로 나눠 오디오와
   근사 동기화하고, 경계는 단조·양 끝은 원본 그대로다.
2. **회귀 0** — 톤 프로파일에 subtitle 절이 없으면(기존 톤·톤 미지정) 파이프라인은
   분할을 아예 안 태운다. max 이하 세그먼트는 **같은 객체**가 그대로 나온다.
3. **fail-loud 스키마** — subtitle 절이 있으면 single_line(bool)·max_line_chars
   (int, 범위) 둘 다 검증한다. 값 정본은 preset.json 과 대조한다(E19-1 규약).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules import style_tone as st
from app.modules.subtitle import split_segments_single_line

REPO = Path(__file__).resolve().parents[1]


def _seg(start, end, text, **extra):
    return SimpleNamespace(start_sec=start, end_sec=end, text=text, **extra)


# ══════════════════════════════════════════════════════════════════════════
# 분할 규칙 — 어절 경계·시간 비례·단조
# ══════════════════════════════════════════════════════════════════════════
def test_short_segment_is_same_object():
    """max 이하는 분할도 복사도 없다 — 같은 객체(부가 속성·시간 완전 불변)."""
    s = _seg(1.0, 2.0, "짧은 줄", style={"size": 88}, low_confidence=True)
    out = split_segments_single_line([s], 15)
    assert len(out) == 1 and out[0] is s


def test_split_at_word_boundaries_only():
    s = _seg(0.0, 3.0, "부장님이 갑자기 소리를 지르기 시작했다")
    out = split_segments_single_line([s], 10)
    # 조각을 도로 이으면 원문과 같다(공백 경계 절단 — 글자 손실·중간 절단 없음)
    assert " ".join(p.text for p in out) == s.text
    assert all(len(p.text) <= 10 for p in out)
    assert len(out) >= 2


def test_long_single_word_stays_whole():
    """max 를 넘는 단일 어절은 자르지 않는다 — 단어 중간 절단 금지가 우선이다."""
    s = _seg(0.0, 1.0, "아아아아아아아아아아아아아아아아아")
    out = split_segments_single_line([s], 10)
    assert len(out) == 1 and out[0].text == s.text


def test_timing_proportional_and_monotonic():
    s = _seg(10.0, 14.0, "가나다라 마바사아 자차카타")   # 4+4+4자, 공백 포함 시간 비례
    out = split_segments_single_line([s], 5)
    assert len(out) == 3
    assert out[0].start_sec == 10.0 and out[-1].end_sec == 14.0   # 양 끝 보존
    for a, b in zip(out, out[1:]):
        assert a.end_sec == b.start_sec                            # 빈틈·겹침 없음
        assert a.end_sec > a.start_sec
    # 글자 수가 같으니 대략 균등(반올림 오차 허용)
    durs = [p.end_sec - p.start_sec for p in out]
    assert max(durs) - min(durs) < 0.05


def test_extra_attrs_carried_to_pieces():
    """style·low_confidence 같은 부가 속성이 조각마다 산다 — E15 강조·E13 표시가
    분할로 증발하면 안 된다."""
    s = _seg(0.0, 2.0, "강조된 자막이 길어서 넘어간다", style={"color": "#FF4632"},
             low_confidence=True)
    out = split_segments_single_line([s], 8)
    assert len(out) >= 2
    for p in out:
        assert p.style == {"color": "#FF4632"}
        assert p.low_confidence is True


def test_emphasized_line_uses_scaled_max():
    """style.size 로 키운 줄은 같은 폭에 덜 들어간다 — build_ass_from_segments 의
    size_ratio 재조정과 같은 수식으로 max 를 줄여야 화면에서 두 줄로 안 넘친다."""
    text = "강조된 자막 줄이 넘친다"            # 13자 — 기본 15면 한 조각
    plain = split_segments_single_line([_seg(0, 2, text)], 15, base_font_size=58)
    emph = split_segments_single_line(
        [_seg(0, 2, text, style={"size": 116})], 15, base_font_size=58)  # 폭 2배 → max 7
    assert len(plain) == 1
    assert len(emph) == 2
    assert all(len(p.text) <= 7 for p in emph)


def test_empty_and_whitespace_text_passthrough():
    s1, s2 = _seg(0, 1, ""), _seg(1, 2, "   ")
    out = split_segments_single_line([s1, s2], 10)
    assert out == [s1, s2]


def test_idempotent():
    """이미 분할된 조각을 다시 태워도 그대로 — 캐시 재개가 두 번 지나도 안전(E19-7 규약)."""
    s = _seg(0.0, 3.0, "부장님이 갑자기 소리를 지르기 시작했다")
    once = split_segments_single_line([s], 10)
    twice = split_segments_single_line(once, 10)
    assert [(p.start_sec, p.end_sec, p.text) for p in once] == \
           [(p.start_sec, p.end_sec, p.text) for p in twice]


# ══════════════════════════════════════════════════════════════════════════
# 톤 스키마 — subtitle 절
# ══════════════════════════════════════════════════════════════════════════
def _data(**over):
    base = json.loads(
        (REPO / "app" / "data" / "style_tones" / "drama_clip_kr.json").read_text("utf-8"))
    for path, value in over.items():
        cur = base
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur[k]
        if value is _DEL:
            cur.pop(keys[-1], None)
        else:
            cur[keys[-1]] = value
    return base


_DEL = object()


def test_drama_clip_kr_has_single_line():
    tone = st.load_style_tone("drama_clip_kr")
    assert tone.subtitle is not None
    assert tone.subtitle["single_line"] is True
    assert tone.subtitle["max_line_chars"] == 15


def test_subtitle_section_optional():
    """절이 없으면 None — 기존 톤·다른 톤은 종전 그대로(회귀 0)."""
    data = _data(subtitle=_DEL)
    st.validate_tone_data(data, "drama_clip_kr")
    tone = st.StyleTone(name="t", sha12="x" * 12, narration=data["narration"],
                        labels=data["labels"], story=data["story"])
    assert tone.subtitle is None


@pytest.mark.parametrize("path,value", [
    ("subtitle.single_line", "yes"),               # bool 아님
    ("subtitle.max_line_chars", 3),                # 범위 밖(아래)
    ("subtitle.max_line_chars", 41),               # 범위 밖(위)
    ("subtitle.max_line_chars", 12.5),             # int 아님
    ("subtitle", []),                              # dict 아님
])
def test_invalid_subtitle_section_fails(path, value):
    with pytest.raises(st.StyleToneError):
        st.validate_tone_data(_data(**{path: value}), "drama_clip_kr")


def test_profile_matches_preset_subtitle():
    """값 정본 대조(E19-1 규약) — preset.json subtitle_dialogue 의 두 항목."""
    preset = json.loads((REPO / "docs" / "design_presets"
                         / "drama_clip_kr.preset.json").read_text("utf-8"))
    tone = st.load_style_tone("drama_clip_kr")
    sd = preset["subtitle_dialogue"]
    assert tone.subtitle["single_line"] is sd["single_line"]["value"]
    assert tone.subtitle["max_line_chars"] == sd["max_line_chars"]["value"]


# ══════════════════════════════════════════════════════════════════════════
# 배선 — 대사(수렴 지점)·TTS(정본 2분기 + variant 2곳)
# ══════════════════════════════════════════════════════════════════════════
def test_pipeline_wires_single_line():
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    # 대사: E19-7 마스킹 **뒤**(마스킹 사전은 온전한 문장 기준) · 편집실 오버라이드 **앞**
    mask = src.index('"step": "subtitle_profanity_mask"')
    split = src.index("split_segments_single_line(")
    override = src.index("_sub_override = overrides_subtitles(_edit_overrides)")
    assert mask < split < override
    # TTS: 정본 두 분기 + variant 대사·TTS — 대사 수렴 1곳과 합쳐 총 5곳
    assert src.count("split_segments_single_line(") == 5
    # 게이트는 톤 프로파일 subtitle 절 하나다(새 CLI 플래그 없음 — E19-1 규약)
    assert 'get("single_line")' in src
