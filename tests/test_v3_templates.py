"""V3 스토리·스타일 템플릿 확장 회귀 가드 (2026-08-31, laeebly 벤치마크 후속).

laeebly 상위작 6편 프레임 해부(뜨비·빡빡이횽·누나스픽 포함)에서 나온 두 문법을
v3 에 **덧붙임으로만** 싣는다:

- 스토리 템플릿 2종 — conflict_payoff(갈등형: 훅=갈등 정점·펀치라인 감속·루프 엔딩),
  chemi_observe(케미 관찰형: 관전 과제 훅·인물 역할 별명·전원 피크·리캡 루프).
- 스타일 프리셋 1종 — drama_clip(김부장 v1~v6 실측 채널값: 1:1 밴드·흰/빨 제목·
  민트 내레이션).

셋 다 **미지정 = 종전과 바이트 동일**(회귀 0): 기본 제공 템플릿은 recap_dialogue·
highlight 그대로, 기본 프리셋은 recap 그대로, 프롬프트도 추가 템플릿을 안 열면
한 글자도 안 바뀐다. v3 는 다른 세션이 활발히 작업 중인 표면이라 기존 동작 무변경이
합류 조건이다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.v3 import stage4
from app.v3 import story as st

REPO = Path(__file__).resolve().parents[1]


def _resp(template, roles, n_spans=4):
    """유효한 응답 뼈대 — span sp0000..sp000N 연속 사용."""
    beats = []
    i = 0
    for r in roles:
        beats.append({"role": r, "span_ids": [f"sp{i:04d}"],
                      "narration": [], "labels": []})
        i += 1
    return {"template": template, "reason": "t",
            "title": {"line1": "제목 한 줄", "line2": "펀치 한 줄"}, "beats": beats}


def _index(n=8):
    idx = {f"sp{i:04d}": {"pos": i, "t_in": i * 2.0, "t_out": i * 2.0 + 2.0,
                          "is_audio": True, "importance": 3, "audio_script": [],
                          "text_source": "heard", "heard_text": "", "conf": 0.9,
                          "scene_script": "", "meaning_content": "", "mood": ""}
           for i in range(n)}
    return idx, sorted(idx)


# ══════════════════════════════════════════════════════════════════════════
# 회귀 0 — 기본 제공은 종전 그대로
# ══════════════════════════════════════════════════════════════════════════
def test_default_templates_unchanged():
    assert st.TEMPLATES == ("recap_dialogue", "highlight")
    assert set(st.STORY_TEMPLATE_SPECS) >= set(st.TEMPLATES)


def test_default_prompt_has_no_extra_templates():
    """추가 템플릿을 안 열면 프롬프트에 새 문구가 한 글자도 없다."""
    stage2 = {"sequences": []}
    idx, order = _index()
    p = st.build_story_prompt(stage2, idx, work_title="T")
    assert "conflict_payoff" not in p and "chemi_observe" not in p


def test_recap_required_role_message_unchanged():
    """recap_dialogue 의 climax 필수 검증은 레지스트리로 일반화한 뒤에도 종전
    반려 문구 그대로다(재질의 프롬프트에 실리는 문자열)."""
    idx, order = _index()
    _, problems, _ = st.validate_story_response(
        _resp("recap_dialogue", ["hook", "build"]), idx, order)
    assert "recap_dialogue 는 climax 비트가 하나 필요하다" in problems


def test_default_style_preset_is_recap():
    assert stage4.STYLE_PRESETS["recap"] is stage4.RECAP_PRESET
    assert stage4.get_style_preset(None) is stage4.RECAP_PRESET
    assert stage4.get_style_preset("recap") is stage4.RECAP_PRESET


# ══════════════════════════════════════════════════════════════════════════
# 신규 스토리 템플릿 2종
# ══════════════════════════════════════════════════════════════════════════
def test_new_templates_registered():
    for name, need in (("conflict_payoff", ("turn", "payoff")),
                       ("chemi_observe", ("ensemble",))):
        spec = st.STORY_TEMPLATE_SPECS[name]
        assert spec["required_roles"] == need
        assert spec["extra"] is True and spec["desc"].strip()


def test_extra_template_rejected_unless_offered():
    """열지 않은 채널에서 모델이 새 템플릿을 내면 종전처럼 반려된다(회귀 0)."""
    idx, order = _index()
    _, problems, _ = st.validate_story_response(
        _resp("conflict_payoff", ["hook", "turn", "payoff"]), idx, order)
    assert any("template" in p for p in problems)


def test_extra_template_accepted_when_offered():
    idx, order = _index()
    allowed = st.TEMPLATES + ("conflict_payoff",)
    story, problems, _ = st.validate_story_response(
        _resp("conflict_payoff", ["hook", "escalate", "turn", "payoff"]),
        idx, order, allowed_templates=allowed)
    assert not problems and story["template"] == "conflict_payoff"


@pytest.mark.parametrize("template,roles,missing", [
    ("conflict_payoff", ["hook", "escalate", "payoff"], "turn"),
    ("conflict_payoff", ["hook", "turn"], "payoff"),
    ("chemi_observe", ["observe_hook", "member_moment"], "ensemble"),
])
def test_new_template_required_roles_enforced(template, roles, missing):
    idx, order = _index()
    allowed = st.TEMPLATES + ("conflict_payoff", "chemi_observe")
    _, problems, _ = st.validate_story_response(
        _resp(template, roles), idx, order, allowed_templates=allowed)
    assert any(missing in p for p in problems)


def test_prompt_carries_offered_template_rules():
    """열린 템플릿의 문구가 프롬프트 템플릿 절에 실린다 — 벤치마크 규칙
    (루프 엔딩·펀치라인 보호·역할 별명)이 지시로 존재해야 모델이 따른다."""
    stage2 = {"sequences": []}
    idx, order = _index()
    p = st.build_story_prompt(stage2, idx, work_title="T",
                              story_templates=("conflict_payoff", "chemi_observe"))
    assert "conflict_payoff" in p and "chemi_observe" in p
    assert "자르지 말고 통째로" in p          # 펀치라인 감속(벤치마크 습관 5)
    assert "즉시 컷" in p                     # 루프 엔딩(습관 4)
    assert "별명" in p                        # 도깨비 케미 관찰 실측


def test_unknown_extra_template_fails_loud():
    stage2 = {"sequences": []}
    idx, order = _index()
    with pytest.raises(ValueError):
        st.build_story_prompt(stage2, idx, work_title="T",
                              story_templates=("no_such_template",))


# ══════════════════════════════════════════════════════════════════════════
# 신규 스타일 프리셋 — drama_clip
# ══════════════════════════════════════════════════════════════════════════
def test_drama_clip_preset_values():
    p = stage4.get_style_preset("drama_clip")
    assert p["aspect_ratio"] == "1:1" and p["video_y"] == 450   # 김부장 실측 밴드
    assert p["title_color2"] == "#FF4632"                       # 결론 줄 = 빨강
    assert p["tts_color"] == "#7DE8D8"                          # 내레이션 = 민트


def test_drama_clip_preset_within_style_allowed():
    """프리셋 값이 Stage 4 화이트리스트 범위를 전부 지킨다 — 어긋나면 모델 diff
    검증과 프리셋이 서로 딴 어휘를 쓰게 된다."""
    p = stage4.get_style_preset("drama_clip")
    for key, rule in stage4.STYLE_ALLOWED.items():
        if key not in p:
            continue
        v = p[key]
        if isinstance(rule, tuple):
            lo, hi = rule
            assert lo <= v <= hi, f"{key}={v} 범위 밖 {rule}"
        else:
            assert rule.match(str(v)), f"{key}={v!r} 형식 위반"


def test_unknown_preset_fails_loud_with_list():
    with pytest.raises(ValueError) as e:
        stage4.get_style_preset("no_such_preset")
    assert "drama_clip" in str(e.value)       # 오타를 한 번에 잡게 목록을 싣는다


# ══════════════════════════════════════════════════════════════════════════
# 배선 — CLI·pipeline
# ══════════════════════════════════════════════════════════════════════════
def test_cli_flags_exist():
    from app.v3.cli import build_parser
    p = build_parser()
    a = p.parse_args(["--video", "x.mp4", "--work-title", "T",
                      "--style-preset", "drama_clip",
                      "--story-templates", "conflict_payoff,chemi_observe"])
    assert a.style_preset == "drama_clip"
    assert a.story_templates == "conflict_payoff,chemi_observe"
    d = p.parse_args(["--video", "x.mp4", "--work-title", "T"])
    assert d.style_preset is None and d.story_templates is None


def test_pipeline_wires_preset_and_templates():
    src = (REPO / "app" / "v3" / "pipeline.py").read_text("utf-8")
    assert "get_style_preset(" in src
    assert "story_templates" in src
    # 밴드 기하와 run_style 이 같은 프리셋을 봐야 한다 — RECAP 하드코딩 잔재 금지
    assert "stage4.RECAP_PRESET" not in src
