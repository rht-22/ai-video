"""E20 페이싱·구성 수정 회귀 가드 (2026-08-28, 김부장 v3 프레임 해부 후속).

사용자 지시: "영상 길이 상한을 2분으로 변경해주고, 고치는 작업 순서대로 해줘"

⓪ 길이 상한 120초 — config 기본값과 스토리 프롬프트의 하드코딩 수치가 함께 움직인다.
A1 [length-clamp] 비례 트림 — 초과분이 build 길이보다 작으면 통째 제거 대신 끝만 자른다
   (v3 실측: 초과 0.8초에 제목의 핵심 장면 12.9초가 통째로 사라졌다).
A2 [narrative-ext] 무발화 방향 앞 확장 금지 — 전사에서 발화를 못 찾으면 앞으로 늘리지
   않는다(v3 실측: +7.4초 전부 무발화 → 도입 6.1초 침묵). 예산도 클램프 상한과 통일해
   확장이 만든 초과를 클램프가 되무는 자기충돌을 없앤다.
A3 청크 오버랩 중복 전사 dedup — 시간이 겹치고 정규화 텍스트가 같은 두 줄은 한 벌만
   (v3 실측: "이렇게 자기 발로 들어와 주다니"가 39.7s·40.3s 두 벌 → 화면에 겹쳐 그려짐).
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.story_builder import StoryClip

REPO = Path(__file__).resolve().parents[1]


def _clip(role, start, end, chunk=0, cand=0):
    return StoryClip(role=role, start_sec=float(start), end_sec=float(end),
                     subtitle="", use_original_audio=True,
                     chunk_index=chunk, candidate_index=cand)


def _seg(s, e, text="말"):
    return SimpleNamespace(start_sec=float(s), end_sec=float(e), text=text)


# ══════════════════════════════════════════════════════════════════════════
# ⓪ 길이 상한 2분
# ══════════════════════════════════════════════════════════════════════════
def test_max_duration_default_120(monkeypatch):
    for k in ("MIN_DURATION_SEC", "MAX_DURATION_SEC"):
        monkeypatch.delenv(k, raising=False)
    import app.config as cfg
    importlib.reload(cfg)
    c = cfg.AppConfig()
    assert c.max_duration_sec == 120
    assert c.min_duration_sec == 40          # 하한은 그대로 — 지시는 상한이다


def test_story_prompt_totals_follow_config():
    """프롬프트 총길이 수치는 하드코딩이 아니라 min/max 포맷 인자를 따른다 —
    상한을 바꿨는데 프롬프트가 계속 '40~60초'라고 말하면 LLM 은 60초로 낸다."""
    from app.modules.gemini_client import STORY_COMPOSITION_PROMPT
    assert "40~60초" not in STORY_COMPOSITION_PROMPT
    assert "60초 초과" not in STORY_COMPOSITION_PROMPT
    assert "40초 미만" not in STORY_COMPOSITION_PROMPT
    body = STORY_COMPOSITION_PROMPT.replace("{min_duration_sec}", "40").replace(
        "{max_duration_sec}", "120").replace("{ideal_duration_sec}", "80")
    assert "40" in body and "120" in body
    assert "{" not in body.replace("{{", "").replace("}}", "") or True  # 남은 포맷 키는 아래에서
    # 다른 포맷 키가 있어도 총길이 키 세 개는 반드시 소비돼야 한다
    for k in ("{min_duration_sec}", "{max_duration_sec}", "{ideal_duration_sec}"):
        assert k not in body


# ══════════════════════════════════════════════════════════════════════════
# A1 — length-clamp 비례 트림
# ══════════════════════════════════════════════════════════════════════════
def _fit(clips, target_min, target_max, lookup=None):
    from app.pipeline import _fit_storyline_to_duration
    return _fit_storyline_to_duration(clips, lookup or {}, target_min=target_min,
                                      target_max=target_max, allow_remove=True)


def test_small_excess_trims_instead_of_removing():
    """v3 재현: 60.5s vs 상한 59.7s(초과 0.8) — build 12.9s 는 살아남고 끝만 잘린다."""
    clips = [_clip("hook", 3705.0, 3733.0),          # 28.0s
             _clip("build", 3798.0, 3810.9),         # 12.9s ← v3 에서 증발한 그 장면
             _clip("payoff", 3867.9, 3887.5)]        # 19.6s → 합 60.5
    out, msg = _fit(clips, 40.0, 59.7)
    assert len(out) == 3                              # 통째 제거 없음
    assert [c.role for c in out] == ["hook", "build", "payoff"]
    total = sum(c.end_sec - c.start_sec for c in out)
    assert abs(total - 59.7) < 0.01                   # 정확히 상한으로
    assert out[1].start_sec == 3798.0                 # build 시작은 보존, 끝만 단축


def test_large_excess_still_removes_whole_build():
    """종전 동작 보존: 초과분이 build 절반 이상이면 통째 제거가 여전히 맞다(86→60류) —
    반토막 난 장면 꽁다리는 없느니만 못하다."""
    clips = [_clip("hook", 0, 20),
             _clip("build", 100, 120),                # 20s
             _clip("build", 200, 215, cand=1),        # 15s
             _clip("payoff", 300, 331)]               # 합 86
    out, msg = _fit(clips, 40.0, 60.0)
    assert len(out) < 4                               # 적어도 하나는 통째 제거
    total = sum(c.end_sec - c.start_sec for c in out)
    assert total <= 60.0 + 0.01


def test_no_change_within_range():
    clips = [_clip("hook", 0, 20), _clip("payoff", 100, 130)]
    out, msg = _fit(clips, 40.0, 60.0)
    assert [(c.start_sec, c.end_sec) for c in out] == [(0, 20), (100, 130)]


# ══════════════════════════════════════════════════════════════════════════
# A2 — narrative-ext 무발화 앞 확장 금지
# ══════════════════════════════════════════════════════════════════════════
def _ext(variants, lookup, transcript=None, target_max=60.0):
    from app.pipeline import _extend_storyline_for_narrative
    return _extend_storyline_for_narrative(
        variants, lookup, target_max=target_max, max_extend_per_side=8.0,
        transcript_segments=transcript)


def _lookup(chunk=0, cand=0, ext_start=None, ext_end=None):
    ce = {}
    if ext_start is not None:
        ce["extended_start_sec"] = ext_start
    if ext_end is not None:
        ce["extended_end_sec"] = ext_end
    return {(chunk, cand): {"context_extension": ce}}


def test_front_ext_skipped_when_extension_is_speechless():
    """v3 재현: 확장 창(3705~3712.4)에 발화가 없다 → 앞 확장 0 (침묵 도입을 만들지 않는다)."""
    v = [([_clip("hook", 3712.4, 3733.0)], "t", 0.9)]
    lk = _lookup(ext_start=3705.0)
    tr = [_seg(3712.9, 3719.0), _seg(3720.0, 3733.0)]     # 발화는 클립 안에서 시작
    out = _ext(v, lk, transcript=tr)
    assert out[0][0][0].start_sec == 3712.4               # 확장 안 함


def test_front_ext_capped_at_lead_in_before_speech():
    """확장 창 안에 발화가 있으면 그 발화 시작 - 리드인(1.0s)까지만 앞당긴다."""
    v = [([_clip("hook", 3712.4, 3733.0)], "t", 0.9)]
    lk = _lookup(ext_start=3705.0)
    tr = [_seg(3708.0, 3711.0), _seg(3712.9, 3733.0)]     # 창 안 3708 에 발화
    out = _ext(v, lk, transcript=tr)
    assert abs(out[0][0][0].start_sec - 3707.0) < 0.01    # 3708 - 1.0
    assert out[0][0][0].end_sec == 3733.0


def test_front_ext_legacy_without_transcript():
    """전사가 없으면 판정하지 않고 종전 그대로 확장한다(오판 금지 규율)."""
    v = [([_clip("hook", 3712.4, 3733.0)], "t", 0.9)]
    lk = _lookup(ext_start=3705.0)
    out = _ext(v, lk, transcript=None)
    assert out[0][0][0].start_sec < 3712.4                # 종전 동작(확장함)


def test_tail_ext_unaffected_by_speech_gate():
    """뒤 확장은 무발화 게이트 밖이다 — 리액션 컷(무발화 엔딩 비트)이 여기 산다
    (v3 실측: 원수 표정 컷이 +2.5s 확장 구간에 있었다)."""
    v = [([_clip("payoff", 3867.9, 3885.0)], "t", 0.9)]
    lk = _lookup(ext_end=3887.5)
    tr = [_seg(3868.0, 3884.0)]                           # 확장 창(3885~)에 발화 없음
    out = _ext(v, lk, transcript=tr)
    assert out[0][0][-1].end_sec > 3885.0                 # 그래도 뒤로는 늘어난다


def test_ext_budget_uses_clamp_max():
    """확장 예산이 클램프 상한(max-0.3)과 같은 기준을 봐야 확장→클램프 자기충돌이 없다 —
    배선을 소스 문자열로 고정한다."""
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    i = src.index("_extend_storyline_for_narrative(\n            all_storyline_variants")
    window = src[i:i + 400]
    assert "RENDER_SAFETY_MARGIN" in window


# ══════════════════════════════════════════════════════════════════════════
# A3 — 청크 오버랩 중복 전사 dedup
# ══════════════════════════════════════════════════════════════════════════
def test_dedup_drops_overlapping_near_identical():
    from app.modules.speech import dedup_overlapping_transcripts
    segs = [_seg(3879.4, 3882.2, "이렇게 자기 발로 들어와 주다니 말이야"),
            _seg(3880.0, 3882.1, "이렇게 자기 발로 들어와 주다니 말이야."),
            _seg(3883.0, 3885.0, "다음 대사")]
    out, dropped = dedup_overlapping_transcripts(segs)
    assert len(out) == 2 and len(dropped) == 1
    assert out[0].start_sec == 3879.4                     # 먼저 시작한 쪽을 남긴다


def test_dedup_keeps_true_repeats_without_time_overlap():
    """진짜 반복 대사(연달아 두 번 말함)는 시간이 안 겹치므로 남는다 —
    v3 의 '기억하겠습니다'(27.1~28.0 / 28.0~29.1) 케이스."""
    from app.modules.speech import dedup_overlapping_transcripts
    segs = [_seg(27.1, 28.0, "기억하겠습니다"), _seg(28.0, 29.1, "기억하겠습니다.")]
    out, dropped = dedup_overlapping_transcripts(segs)
    assert len(out) == 2 and not dropped


def test_dedup_keeps_different_text_overlap():
    """겹치더라도 텍스트가 다르면(교차 대화) 건드리지 않는다 — E14 겹침 데이터 규율."""
    from app.modules.speech import dedup_overlapping_transcripts
    segs = [_seg(10.0, 12.0, "뭔 개똥 같은 소리"), _seg(11.0, 13.0, "있어요 이 자식 말은")]
    out, dropped = dedup_overlapping_transcripts(segs)
    assert len(out) == 2 and not dropped


def test_dedup_wired_into_chunk_merge():
    """청크 전사 병합 직후에 dedup 이 배선돼 있다 — 자막·cue·후보 인용이 전부 이 목록을 본다."""
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert "dedup_overlapping_transcripts(" in src


# ══════════════════════════════════════════════════════════════════════════
# C1 — 번인 자막 광범위 경고 (배선만 — 관측 장치라 로직은 문자열로 고정)
# ══════════════════════════════════════════════════════════════════════════
def test_pervasive_burned_hint_wired():
    """창별 회피가 대사 트랙의 20%+·5줄+ 이면 이중 자막 경고를 남긴다 — 회피는 겹침만
    피하지 중복 표기 자체는 못 푼다(v3 실측)."""
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert "pervasive_burned_hint" in src
    assert "subtitles:false" in src
