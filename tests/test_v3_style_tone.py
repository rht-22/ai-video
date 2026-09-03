"""v3 채널 톤 후크 회귀 가드 (2026-09-01).

E19-1 톤 프로파일은 구 엔진 story/style 프롬프트에만 배선돼 있었다 — `app/v3/` 어디에도
`style_tone` import 가 없어서, 채널이 `pipeline_v3:true` 로 넘어가면 프로파일이 정한
내레이션 말투가 **조용히 사라졌다**(에러도 경고도 없이 v3 기본 서술체로). 이 후크가 그
구멍을 막는다.

규율은 `--story-templates`(V3 템플릿 확장)와 같다:

- **미지정 = 프롬프트 바이트 동일**(회귀 0). `tone_block=""` 이면 종전과 한 글자도 같다.
- **없는 이름은 즉시 실패**(StyleToneError) — 조용히 기본 톤으로 떨어지면 채널은 프리셋을
  켰다고 믿은 채 종전 산출을 받는다.
- **v3 에 안 실리는 노브는 말한다**(`v3_unapplied_knobs`) — 같은 이유.
- 지문(캐시 키)에 톤 sha 가 들어가되 **미지정이면 키를 안 넣는다** — 기존 잡의 story
  캐시가 그대로 산다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.modules import style_tone as stn
from app.v3 import story as st

REPO = Path(__file__).resolve().parents[1]


def _index(n=8):
    idx = {f"sp{i:04d}": {"pos": i, "t_in": i * 2.0, "t_out": i * 2.0 + 2.0,
                          "is_audio": True, "importance": 3, "audio_script": [],
                          "text_source": "heard", "heard_text": "", "conf": 0.9,
                          "scene_script": "", "meaning_content": "", "mood": ""}
           for i in range(n)}
    return idx, sorted(idx)


def _tone():
    return stn.load_style_tone("drama_clip_kr")


# ══════════════════════════════════════════════════════════════════════════
# 회귀 0
# ══════════════════════════════════════════════════════════════════════════
def test_no_tone_means_identical_prompt():
    """톤을 안 주면 프롬프트가 종전과 바이트 동일하다."""
    idx, _ = _index()
    a = st.build_story_prompt({"sequences": []}, idx, work_title="T")
    b = st.build_story_prompt({"sequences": []}, idx, work_title="T", tone_block="")
    assert a == b
    assert "채널 톤 프로파일" not in a


def test_block_is_empty_string_for_none():
    assert stn.v3_story_prompt_block(None) == ""
    assert stn.v3_unapplied_knobs(None) == []


def test_legacy_tone_text_unchanged():
    """구 엔진이 쓰는 합친 문구는 한 글자도 안 바뀐다(문체 문구를 본체/꼬리로 쪼갠 뒤에도).

    이 문자열이 흔들리면 --style-tone 을 쓰는 기존 채널의 스토리 프롬프트가 함께 흔들린다.
    """
    txt = stn._NARRATION_TONE_TEXT["recall_first_person"]
    assert txt.startswith("- 문체: **1인칭 회상체 초단문**.")
    assert txt.endswith("위 [텍스트 톤] 절의 권장 결(명사형 종결 등)보다 **이 문체가 이긴다**.")
    assert stn.story_prompt_block(_tone()).count("[텍스트 톤]") == 1


# ══════════════════════════════════════════════════════════════════════════
# 절의 내용 — v3 어휘로 번역됐는가
# ══════════════════════════════════════════════════════════════════════════
def test_block_carries_narration_tone():
    blk = stn.v3_story_prompt_block(_tone())
    assert "1인칭 회상체 초단문" in blk
    assert "이 절이 이긴다" in blk


def test_block_speaks_v3_vocabulary_not_legacy():
    """구 엔진 어휘(cue·clip·start_sec·[텍스트 톤])가 새면 모델이 없는 것을 찾는다."""
    blk = stn.v3_story_prompt_block(_tone())
    assert "[텍스트 톤]" not in blk
    # 규칙 줄(- 로 시작)에 구 엔진 어휘가 없어야 한다. 프로파일 이름(drama_clip_kr)과
    # 문체 문구 안의 예시("접착제형 cue")는 본체에 박힌 문자열이라 예외다.
    rules = [ln for ln in blk.splitlines()
             if ln.startswith("- ") and not ln.startswith("- 문체")]
    for line in rules:
        for legacy in ("cue", "clip", "start_sec", "storyline"):
            assert legacy not in line, f"구 엔진 어휘 {legacy!r}: {line}"
    assert "hook 비트" in blk and "span" in blk


def test_block_overrides_rule_four_length():
    """길이 노브는 규칙 4 의 12~16자를 대체한다고 명시해야 한다(둘 다 살면 모델이 헷갈린다)."""
    blk = stn.v3_story_prompt_block(_tone())
    assert "10~18자" in blk and "규칙 4" in blk


def test_hook_knobs_are_carried():
    blk = stn.v3_story_prompt_block(_tone())
    assert "8초 이내" in blk          # story.hook_max_sec
    assert "제목-훅 일치" in blk      # story.hook_title_scene


def test_prompt_places_tone_before_reject_note():
    """톤 절은 재료 목록 앞에 온다 — 규칙을 다 읽은 뒤, 재료를 고르기 전."""
    idx, _ = _index()
    p = st.build_story_prompt({"sequences": []}, idx, work_title="T",
                              tone_block=stn.v3_story_prompt_block(_tone()))
    assert p.index("채널 톤 프로파일") < p.index("## 재료")
    assert p.index("8. 대사 신뢰") < p.index("채널 톤 프로파일")


# ══════════════════════════════════════════════════════════════════════════
# 조용한 누락 금지
# ══════════════════════════════════════════════════════════════════════════
def test_unapplied_knobs_are_named():
    """v3 가 안 싣는 노브는 사유와 함께 이름이 나와야 한다."""
    knobs = stn.v3_unapplied_knobs(_tone())
    joined = " ".join(knobs)
    for name in ("narration.placement", "narration.fill_gaps", "story.structure",
                 "labels.", "pacing."):
        assert name in joined, f"{name} 가 비적용 목록에 없다"
    assert all("(" in k for k in knobs), "사유 없는 항목이 있다"


def test_applied_and_unapplied_do_not_overlap():
    """한 노브가 '실렸다'와 '안 실렸다'에 동시에 있으면 안 된다."""
    unapplied = {k.split("(")[0] for k in stn.v3_unapplied_knobs(_tone())}
    assert not (set(stn.V3_APPLIED_KNOBS) & unapplied)


def test_unknown_tone_name_fails_loudly():
    with pytest.raises(stn.StyleToneError):
        stn.load_style_tone("no_such_tone_profile")


# ══════════════════════════════════════════════════════════════════════════
# 배선 — 파이프라인·CLI
# ══════════════════════════════════════════════════════════════════════════
def test_run_story_accepts_tone_block():
    import inspect
    assert "tone_block" in inspect.signature(st.run_story).parameters
    assert "tone_block" in inspect.signature(st.build_story_prompt).parameters


def test_pipeline_wires_tone_into_fingerprint_and_run_story():
    src = (REPO / "app" / "v3" / "pipeline.py").read_text("utf-8")
    assert "v3_story_prompt_block" in src
    assert 'style_tone"] = _tone.sha12' in src or '_fp_payload["style_tone"]' in src
    assert "tone_block=tone_block" in src
    assert "v3_unapplied_knobs" in src, "비적용 노브를 로그로 알리지 않는다"
    # 미지정이면 지문에 키가 안 들어간다(기존 잡 캐시 보존)
    assert "if style_tone:" in src


def test_cli_exposes_style_tone_flag():
    src = (REPO / "app" / "v3" / "cli.py").read_text("utf-8")
    assert '"--style-tone"' in src
    assert "style_tone=args.style_tone" in src
