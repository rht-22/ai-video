"""V4-M5 살붙이기 회귀 가드 — `app/v4/flesh.py`.

이 단계는 **승인이 끝난 편에 카피를 입히는 마지막 LLM 자리**다. 여기서 새는 것 셋을
값으로 고정한다:

① **v3 규칙 4·5·6 이 실제로 프롬프트에 실렸는가** — 6단계가 일부러 안 실은 그것들이
   여기 오지 않으면 아무 데도 없다. 실측 인용 문장은 v3 `story.PROMPT_TEMPLATE` 에서
   **런타임에 뽑아 대조**한다(v3 문구가 바뀌면 이 테스트가 먼저 깨진다).
② **재료가 채택 span 만인가** — v3 `build_material_block` 은 전량을 싣는다(`only` 인자가
   없다). 67분 소재에서 입력이 두 배가 되는 자리라 '안 실린 것'을 값으로 센다.
③ **한 편의 실패가 그 편에서 끝나는가 · 전량 실패는 크게 죽는가** — 조용한 결번이 이
   레포에서 가장 나쁜 실패다.

LLM·네트워크 0 — 모델은 가짜 클라이언트이고 나머지는 순수 함수다.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest

from app.v3 import story as story_mod
from app.v4 import bridge, flesh
from app.v4 import video as video_mod

DURATION = 100.0

QUOTE_A = "이건 정말 대단한 순간이었습니다"
QUOTE_B = "그래서 내가 그때 말했잖아 형"
QUOTE_C = "너 진짜 이럴 거야 지금부터"
QUOTE_D = "그 말을 여기서 다시 들을 줄이야"


# ── 격자·후보 ───────────────────────────────────────────────────────────────

def _span(sid, t_in, t_out, *, audio, text=""):
    return {"id": sid, "t_in": t_in, "t_out": t_out, "is_audio": audio,
            "time_authority": "stt" if audio else "scene", "text": text}


def _words(t0, t1, tag, prob, n=2):
    step = (t1 - t0) / n
    return [{"t0": round(t0 + i * step, 3), "t1": round(t0 + (i + 1) * step, 3),
             "text": f"{tag}{i}", "prob": prob} for i in range(n)]


def make_grid():
    spans = [
        _span("sp0000", 0.0, 5.0, audio=False),
        _span("sp0001", 5.0, 8.0, audio=True, text=QUOTE_A),
        _span("sp0002", 8.0, 12.0, audio=True, text=QUOTE_B),
        _span("sp0003", 12.0, 20.0, audio=False),
        _span("sp0004", 20.0, 24.0, audio=True, text=QUOTE_C),
        _span("sp0005", 24.0, 30.0, audio=False),
        _span("sp0006", 30.0, 40.0, audio=True, text=QUOTE_D),
    ]
    words = (_words(5.0, 8.0, "a", 0.9) + _words(8.0, 12.0, "b", 0.9)
             + _words(20.0, 24.0, "c", 0.9) + _words(30.0, 40.0, "d", 0.9))
    return {"source": {"duration_sec": DURATION},
            "scene_cuts": [10.0, 30.0, 50.0], "silence": [], "arousal": [],
            "words": words, "span_candidates": spans}


def make_candidate(cid="c01", template="recap_dialogue"):
    """조각 둘 = 비트 둘. 경계는 전부 눈금 위라 스냅이 값을 안 바꾼다."""
    return {"id": cid, "template": template, "reason": "형제의 말싸움이 뒤집히는 편",
            "title_draft": {"line1": "형제 말싸움", "line2": "결국 사과"},
            "segments": [{"start_sec": 5.0, "end_sec": 12.0, "quote": QUOTE_A},
                         {"start_sec": 20.0, "end_sec": 30.0, "quote": QUOTE_C}]}


def crossed(cand=None, grid=None):
    return bridge.cross(cand or make_candidate(), grid=grid or make_grid(),
                        source_duration_sec=DURATION)


# ── 가짜 클라이언트 ─────────────────────────────────────────────────────────
# `types` 는 **진짜 google-genai** 다(`tests/test_v4_flags.py` 와 같은 규율) — 가짜 타입
# 으로 조립하면 config 필드 이름이 틀려도 테스트가 통과한다.

FLESH_MARKER = "카피를 입히는 것"          # 살붙이기 프롬프트에만 있는 문구
DESC_MARKER = "유튜브 발행 메타"           # 설명 프롬프트에만 있는 문구


def _response(payload, *, finish: str = "STOP", total: int | None = 9000):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    meta = SimpleNamespace(prompt_token_count=8000, thoughts_token_count=500,
                           candidates_token_count=500,
                           cached_content_token_count=0, total_token_count=total)
    return SimpleNamespace(text=text, usage_metadata=meta,
                           model_version="gemini-3.7-flash-001",
                           candidates=[SimpleNamespace(
                               finish_reason=SimpleNamespace(name=finish))])


def ok_flesh(*, line1="형이 던진 한마디", line2="동생은 무너졌다",
             beats=None):
    return {"title": {"line1": line1, "line2": line2},
            "reason": "형제 갈등이 한 대사에서 뒤집힌다",
            "beats": beats if beats is not None else [
                {"number": 0, "role": "hook",
                 "narration": ["형이 먼저 말했어요", "동생은 굳었죠"], "labels": []},
                {"number": 1, "role": "climax", "narration": [],
                 "labels": [{"text": "(팩폭 시전)", "span_id": "sp0004"},
                            {"text": "(정적)", "span_id": "sp0004"}]}]}


def ok_desc():
    return {"description": "형제의 말싸움이 한 대사로 뒤집히는 순간.",
            "hashtags": ["#형제", "#말싸움", "#리액션"]}


class _FakeModels:
    """프롬프트 안의 표식으로 응답을 고른다 — 병렬이라 **호출 순서는 정해지지 않는다**."""

    def __init__(self, by_marker=None, default=None, delay_sec: float = 0.0):
        self.by_marker = dict(by_marker or {})
        self.default = default
        self.calls: list[dict] = []
        self.delay_sec = delay_sec
        self._lock = threading.Lock()
        self.max_in_flight = 0
        self._in_flight = 0

    def generate_content(self, *, model, contents, config):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.calls.append({"model": model, "contents": contents, "config": config})
            n_for_marker = {m: sum(1 for c in self.calls
                                   if m in [x for x in c["contents"] if isinstance(x, str)][-1])
                            for m in self.by_marker}
        try:
            if self.delay_sec:
                time.sleep(self.delay_sec)
            prompt = [c for c in contents if isinstance(c, str)][-1]
            item = self.default
            for marker, resp in self.by_marker.items():
                if marker in prompt:
                    item = resp
                    break
            if callable(item):
                item = item(prompt, n_for_marker)
            if item is None:
                item = _response(ok_desc() if DESC_MARKER in prompt else ok_flesh())
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            with self._lock:
                self._in_flight -= 1

    def prompts(self, marker: str = "") -> list[str]:
        out = []
        for c in self.calls:
            prompt = [x for x in c["contents"] if isinstance(x, str)][-1]
            if not marker or marker in prompt:
                out.append(prompt)
        return out


class _FakeGemini:
    def __init__(self, by_marker=None, default=None, delay_sec: float = 0.0):
        from google.genai import types

        self.types = types
        self.models = _FakeModels(by_marker, default, delay_sec)
        self.client = SimpleNamespace(models=self.models)
        self.config = SimpleNamespace(flash_model_name="gemini-3.7-flash",
                                      model_name="gemini-3.7-pro")


class _ApiError(Exception):
    """google-genai `APIError` 흉내 — `.code` 에 정수 상태(SDK 실측)."""

    def __init__(self, code: int, message: str = "boom"):
        super().__init__(f"{code} {message}")
        self.code = code


# ══════════════════════════════════════════════════════════════════════════
# 1) 프롬프트 — v3 규칙 4·5·6 이 여기 왔는가
# ══════════════════════════════════════════════════════════════════════════

def _v3_rule(n: int) -> str:
    """v3 `story.PROMPT_TEMPLATE` 에서 규칙 n 의 본문을 뽑는다(런타임 대조).

    v3 문구가 바뀌면 이 테스트가 **먼저** 깨진다 — 그것이 목적이다(규칙의 정본은 v3 이고
    10단계는 그 규칙을 옮겨 든 자리다)."""
    body = story_mod.PROMPT_TEMPLATE.split("## 편성 규칙", 1)[1]
    m = re.search(rf"^{n}\. (.+?)$", body, re.M)
    assert m, f"v3 PROMPT_TEMPLATE 에서 규칙 {n} 을 못 찾았다"
    return m.group(1)


def _prompt(**kw) -> str:
    c = crossed()
    args = dict(work_title="형제 예능 3화", candidate=make_candidate(),
                span_index=c["span_index"], span_ids=c["span_ids"],
                research_context="", template="recap_dialogue",
                target_sec=53.0, max_sec=60.0)
    args.update(kw)
    return flesh.build_flesh_prompt(**args)


def test_rule4_measured_quotes_are_carried_verbatim_from_v3():
    """규칙 4 의 **실측 인용 2건**이 v3 문장 그대로 실린다 — 그 인용이 12~16자 규칙의
    근거다(요약하면 근거가 사라진다)."""
    prompt = _prompt()
    rule4 = _v3_rule(4)
    for quote in ('"천재에게 반주를 부탁한 여학생" 15자·2.0s',
                  '"돌아온 답은 최악이었죠" 12자·2.15s'):
        assert quote in rule4, "v3 규칙 4 의 인용이 바뀌었다 — 정본을 먼저 보라"
        assert quote in prompt
    # 17자 초과의 결과(잘려 나간 실측)도 함께 온다 — 규칙이 아니라 사고 기록이다.
    assert "관광 접고 전유진" in rule4 and "관광 접고 전유진" in prompt
    assert f"{flesh.NARRATION_SENT_CHARS[0]}~{flesh.NARRATION_SENT_CHARS[1]}자" in prompt


def test_rule5_labels_range_and_span_anchor_are_in_the_prompt():
    rule5 = _v3_rule(5)
    prompt = _prompt()
    assert "(팩폭 시전)" in rule5 and "(팩폭 시전)" in prompt
    assert f"편 전체에서 {flesh.LABELS_PER_EPISODE[0]}~{flesh.LABELS_PER_EPISODE[1]}개" in prompt
    assert '"span_id": "sp0123"' in prompt          # 앵커 형식 예시


def test_rule6_title_is_strict_here_not_a_draft():
    """제목 규칙은 v3 와 같은 두 줄·같은 자수지만, **여기서는 확정**이라고 말한다."""
    prompt = _prompt()
    assert "line1=상황" in _v3_rule(6)
    assert "line1=상황" in prompt
    assert f"각 {flesh.TITLE_MAX_CHARS}자 이내" in prompt
    assert "가안이 아니라 확정이다" in prompt


def test_prompt_forbids_moving_the_cut_and_asks_for_span_ids_only():
    prompt = _prompt()
    assert "구간은 바꿀 수 없다" in prompt and "시각을 쓰지 마라" in prompt


def test_prompt_carries_required_roles_of_the_template():
    """필수 역할 목록의 정본은 레지스트리다 — 프롬프트가 그것을 그대로 말한다."""
    assert "climax 비트는 반드시" in _prompt(template="recap_dialogue")
    p = _prompt(template="conflict_payoff")
    assert "turn · payoff 비트는 반드시" in p
    assert tuple(story_mod.STORY_TEMPLATE_SPECS["conflict_payoff"]["required_roles"]) \
        == ("turn", "payoff")


def test_output_example_opens_with_a_hook_not_with_the_climax():
    """예시의 첫 비트에 절정 역할을 박아 두면 모델이 그 순서로 편성한다(v3 예시도
    hook → climax 다)."""
    example = _prompt(template="conflict_payoff").split("## 출력", 1)[1]
    assert '"number": 0, "role": "hook"' in example
    assert '"number": 1, "role": "payoff"' in example


def test_trust_rule_only_mentions_tags_that_are_actually_in_the_material():
    """6단계가 배운 것(`candidates.py` 독스트링): 없는 표지를 적으면 모델이 못 찾고
    **규칙 자체를 무시한다**. `[청취]`·`[대사없음]` 은 10a 가 붙이는 표기다."""
    plain = _prompt()                                   # 10a 꺼짐 · 저확신 없음
    assert "신뢰 표기가 붙은 줄이 없다" in plain
    for tag in ("[저확신", "[청취]", "[대사없음]"):
        assert tag not in plain.split("## 규칙")[1].split("4. 내레이션")[0]

    grid = make_grid()
    for w in grid["words"]:
        if w["text"].startswith("a"):
            w["prob"] = story_mod.LOW_CONF - 0.1
    c = crossed(grid=grid)
    low = _prompt(span_index=c["span_index"], span_ids=c["span_ids"])
    assert "[저확신 …]" in low and "[청취]" not in low

    detail = {"sp0001": {"text_source": "heard", "heard_text": QUOTE_A,
                         "audio_script": [{"speaker": "형", "line": QUOTE_A}]}}
    c2 = bridge.cross(make_candidate(), grid=make_grid(),
                      source_duration_sec=DURATION, detail=detail)
    heard = _prompt(span_index=c2["span_index"], span_ids=c2["span_ids"])
    assert "[청취]" in heard


def test_prompt_is_pure_and_reject_note_is_the_only_difference():
    a, b = _prompt(), _prompt()
    assert a == b                                   # 같은 인자 = 같은 문자열(지문의 전제)
    c = _prompt(reject_note="- title.line1 이 24자")
    assert c != a and "직전 제안 반려 사유" in c and "24자" in c


# ══════════════════════════════════════════════════════════════════════════
# 2) 재료 — 채택된 span 만
# ══════════════════════════════════════════════════════════════════════════

def test_material_block_carries_only_adopted_spans():
    """격자 span 7개 중 후보가 채택한 것은 4개다. 나머지 3개는 **한 번도** 안 나온다 —
    v3 `build_material_block` 은 전량을 싣는다(`only` 인자가 없다)."""
    c = crossed()
    adopted = [sid for ids in c["span_ids"] for sid in ids]
    assert adopted == ["sp0001", "sp0002", "sp0004", "sp0005"]

    block = flesh.material_block(c["span_index"], c["span_ids"])
    for sid in adopted:
        assert sid in block
    for sid in ("sp0000", "sp0003", "sp0006"):
        assert sid not in block, f"{sid} 는 이 편이 안 쓰는 span 이다"
    assert QUOTE_D not in block                     # 그 span 의 대사도 안 실린다
    # 프롬프트 전체로도 같다(재료 블록 밖에서 새지 않는다).
    assert "sp0006" not in _prompt()


def test_material_block_groups_by_beat_and_marks_voiced_or_silent():
    block = flesh.material_block(crossed()["span_index"], crossed()["span_ids"])
    assert "### 비트 0" in block and "### 비트 1" in block
    assert "sp0001 | 유성" in block and QUOTE_A in block
    assert "sp0005 | 무성" in block


def test_material_block_uses_the_same_low_conf_threshold_as_stage6():
    """`[저확신]` 임계는 6단계 전사 블록과 **같은 자**여야 한다 — 6단계가 피하라고 한 줄이
    여기서 멀쩡해 보이면 모델이 그 대사를 인용해 자막이 깨진다."""
    grid = make_grid()
    for w in grid["words"]:
        if w["text"].startswith("a"):
            w["prob"] = story_mod.LOW_CONF - 0.1
    c = crossed(grid=grid)
    block = flesh.material_block(c["span_index"], c["span_ids"])
    assert "[저확신 0.40]" in block
    assert "sp0002 | 유성" in block and "[저확신" not in block.split("sp0002")[1].split("\n")[0]


def test_material_block_shows_speaker_only_when_detail_gave_one():
    """화자의 유일한 원천은 10a 다(bridge §0) — 없으면 이름 없이 전사만 나온다."""
    plain = flesh.material_block(crossed()["span_index"], crossed()["span_ids"])
    assert f"| {QUOTE_A}" in plain

    detail = {"sp0001": {"audio_script": [{"speaker": "형", "line": QUOTE_A}]}}
    c = bridge.cross(make_candidate(), grid=make_grid(),
                     source_duration_sec=DURATION, detail=detail)
    assert f"형: {QUOTE_A}" in flesh.material_block(c["span_index"], c["span_ids"])


# ══════════════════════════════════════════════════════════════════════════
# 3) 검증기
# ══════════════════════════════════════════════════════════════════════════

SPANS = {"sp0001", "sp0002", "sp0004", "sp0005"}


def _validate(resp, **kw):
    args = dict(span_ids=SPANS, n_beats=2, required_roles=("climax",))
    args.update(kw)
    return flesh.validate_flesh_response(resp, **args)


def test_title_over_limit_is_rejected_here_strictly():
    """6단계는 같은 위반을 **잘라내고 노트**로 넘긴다(가안). 여기서는 확정이라 반려다."""
    long_line = "가" * (flesh.TITLE_MAX_CHARS + 1)
    got, problems = _validate(ok_flesh(line1=long_line))
    assert got is None
    assert any(f"{flesh.TITLE_MAX_CHARS}자 이내" in p and "line1" in p for p in problems)
    # 경계는 통과한다(넘는 것만 막는다).
    ok, problems = _validate(ok_flesh(line1="가" * flesh.TITLE_MAX_CHARS))
    assert ok is not None and problems == []


def test_title_must_be_two_non_empty_lines():
    got, problems = _validate(ok_flesh(line2="  "))
    assert got is None and any("line2" in p and "비었다" in p for p in problems)


def test_narration_must_be_an_array_of_sentences():
    beats = [{"number": 0, "role": "hook", "narration": {"a": 1}, "labels": []},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert got is None and any("narration 은 짧은 문장의 배열" in p for p in problems)


def test_narration_single_string_is_accepted_but_recorded():
    """v3 하위호환(조용히 감쌌다) — 여기서는 감싸되 **노트를 남긴다**."""
    beats = [{"number": 0, "role": "hook", "narration": "형이 먼저 말했어요", "labels": []},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert problems == []
    assert got["beats"][0]["narration"] == ["형이 먼저 말했어요"]
    assert any("배열이 아니라 문자열" in n for n in got["notes"])


def test_long_narration_is_a_note_not_a_rejection():
    """자수 초과의 결과는 이미 기계가 처리한다(`plan_narration_slots` 가 fit·드랍하고
    기록한다) — 카피 한 문장 때문에 승인된 편을 버리지 않는다."""
    long_sentence = "놀랍게도 승객들은 유람선 관광까지 포기하고 돌아갔어요"
    beats = [{"number": 0, "role": "hook", "narration": [long_sentence], "labels": []},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert problems == [] and got["beats"][0]["narration"] == [long_sentence]
    assert any("규칙 4 상한" in n for n in got["notes"])


def test_label_anchor_outside_this_episode_is_rejected():
    beats = [{"number": 0, "role": "hook", "narration": [],
              "labels": [{"text": "(정적)", "span_id": "sp0006"}]},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert got is None
    assert any("sp0006" in p and "이 편의 span 이 아니다" in p for p in problems)


def test_label_without_anchor_is_rejected_not_guessed():
    """v3 는 문자열 라벨을 첫 span 에 붙였다 — 여기서는 자리를 지어내지 않는다."""
    beats = [{"number": 0, "role": "hook", "narration": [], "labels": ["(정적)"]},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert got is None and any("앵커 없는 라벨은 받지 않는다" in p for p in problems)


def test_label_bracket_is_repaired_with_a_note():
    beats = [{"number": 0, "role": "hook", "narration": [],
              "labels": [{"text": "팩폭 시전", "span_id": "sp0001"}]},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert problems == [] and got["beats"][0]["labels"][0]["text"] == "(팩폭 시전)"
    assert any("괄호 보정" in n for n in got["notes"])


def test_labels_over_the_cap_are_trimmed_from_the_front_and_recorded():
    """E15 하드캡 규율 — 넘으면 앞에서부터 남기고 **자른 사실을 반드시 기록**한다."""
    many = [{"text": f"(라벨{i})", "span_id": "sp0001"} for i in range(6)]
    beats = [{"number": 0, "role": "hook", "narration": [], "labels": many},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert problems == []
    kept = [x["text"] for b in got["beats"] for x in b["labels"]]
    assert kept == ["(라벨0)", "(라벨1)", "(라벨2)", "(라벨3)"]
    assert sum("상한 초과로 버렸다" in n for n in got["notes"]) == 2


def test_too_few_labels_is_only_a_note():
    beats = [{"number": 0, "role": "hook", "narration": [], "labels": []},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert problems == [] and any("하한" in n for n in got["notes"])


@pytest.mark.parametrize("resp,needle", [
    ({**ok_flesh(), "effect_texts": [{"text": "쿵!"}]}, "effect_texts"),
    ({**ok_flesh(), "title_rotate": -3}, "title_rotate"),
])
def test_unknown_top_level_keys_are_rejected(resp, needle):
    """효과 문구·스티커·제목 기울기는 11:style 의 일이다 — 여기서 받으면 그 값을 읽는
    코드가 다음 판에 생긴다(8단계 `ALLOWED_RESPONSE_KEYS` 와 같은 규율)."""
    got, problems = _validate(resp)
    assert got is None and any(needle in p and "모르는 열쇠" in p for p in problems)


def test_unknown_beat_and_label_keys_are_rejected():
    beats = [{"number": 0, "role": "hook", "narration": [], "labels": [],
              "confidence": 0.9},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert got is None and any("confidence" in p for p in problems)

    beats = [{"number": 0, "role": "hook", "narration": [],
              "labels": [{"text": "(정적)", "span_id": "sp0001", "y": 0.4}]},
             {"number": 1, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert got is None and any("'y'" in p or "y" in p for p in problems)


def test_beat_number_must_be_inside_this_episode_and_unique():
    beats = [{"number": 5, "role": "hook", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert got is None and any("비트 번호" in p for p in problems)

    beats = [{"number": 0, "role": "hook", "narration": [], "labels": []},
             {"number": 0, "role": "climax", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert got is None and any("두 번 나왔다" in p for p in problems)


def test_required_role_of_the_template_is_enforced():
    beats = [{"number": 0, "role": "hook", "narration": [], "labels": []},
             {"number": 1, "role": "build", "narration": [], "labels": []}]
    got, problems = _validate(ok_flesh(beats=beats))
    assert got is None and any("climax 비트가 하나 필요하다" in p for p in problems)


def test_validator_is_pure_and_orders_beats_by_number():
    resp = ok_flesh(beats=[
        {"number": 1, "role": "climax", "narration": [], "labels": []},
        {"number": 0, "role": "hook", "narration": ["형이 먼저 말했어요"], "labels": []}])
    before = copy.deepcopy(resp)
    got, problems = _validate(resp)
    assert problems == [] and resp == before                 # 인자를 안 고친다
    assert [b["number"] for b in got["beats"]] == [0, 1]     # 결정적 순서


# ══════════════════════════════════════════════════════════════════════════
# 4) 예산 다듬기 — 역할 보호를 템플릿에 맞춘다
# ══════════════════════════════════════════════════════════════════════════

def _index(*specs):
    """(id, t_in, t_out, importance) → span_index 조각(trim 이 읽는 열쇠만)."""
    return {sid: {"t_in": t0, "t_out": t1, "is_audio": True, "importance": imp,
                  "pos": i, "audio_script": [], "text_source": "transcript",
                  "heard_text": "", "conf": None, "scene_script": "",
                  "meaning_content": "", "mood": "", "text": ""}
            for i, (sid, t0, t1, imp) in enumerate(specs)}


def test_protected_roles_come_from_the_template_registry():
    assert flesh.protected_roles("recap_dialogue") == ("climax",)
    assert flesh.protected_roles("conflict_payoff") == ("climax", "turn", "payoff")
    assert flesh.protected_roles("chemi_observe") == ("climax", "ensemble")
    with pytest.raises(ValueError, match="모르는 스토리 템플릿"):
        flesh.protected_roles("no_such_template")


def test_v3_trim_alone_would_eat_the_payoff_beat():
    """🛑 계약 §2 의 경고를 값으로 재현한다 — v3 는 `role == "climax"` 만 보호한다."""
    index = _index(("sp0", 0.0, 10.0, 3), ("sp1", 10.0, 20.0, 3),
                   ("sp2", 20.0, 30.0, 3), ("sp3", 30.0, 40.0, 3))
    beats = [{"role": "build", "span_ids": ["sp0", "sp1"], "narration": [], "labels": []},
             {"role": "payoff", "span_ids": ["sp2", "sp3"], "narration": [], "labels": []}]
    removed = story_mod.trim_to_budget(copy.deepcopy(beats), index, [], 25.0)
    assert any(r["span_id"] in ("sp2", "sp3") for r in removed), \
        "v3 단독은 payoff 를 깎는다 — 이 사실이 `trim_to_budget_by_role` 의 존재 이유다"


def test_role_view_protects_the_templates_climax_equivalent():
    index = _index(("sp0", 0.0, 10.0, 3), ("sp1", 10.0, 20.0, 3),
                   ("sp2", 20.0, 30.0, 3), ("sp3", 30.0, 40.0, 3))
    beats = [{"role": "build", "span_ids": ["sp0", "sp1"], "narration": [], "labels": []},
             {"role": "payoff", "span_ids": ["sp2", "sp3"], "narration": [], "labels": []}]
    out, removed, renamed = flesh.trim_to_budget_by_role(
        beats, index, [], 25.0, template="conflict_payoff")
    assert [r["span_id"] for r in removed] == ["sp0"]        # build 의 가장자리만
    assert out[1]["span_ids"] == ["sp2", "sp3"]              # payoff 는 통째로 남는다
    assert renamed == ["payoff"]                             # 감사 기록에 남는다


def test_role_view_restores_the_original_role_names_and_is_pure():
    index = _index(("sp0", 0.0, 10.0, 3), ("sp1", 10.0, 20.0, 3),
                   ("sp2", 20.0, 30.0, 3), ("sp3", 30.0, 40.0, 3))
    beats = [{"role": "member_moment", "span_ids": ["sp0", "sp1"],
              "narration": [], "labels": []},
             {"role": "ensemble", "span_ids": ["sp2", "sp3"], "narration": [], "labels": []}]
    before = copy.deepcopy(beats)
    out, _removed, _renamed = flesh.trim_to_budget_by_role(
        beats, index, [], 25.0, template="chemi_observe")
    assert beats == before                                   # 인자를 제자리에서 안 고친다
    assert [b["role"] for b in out] == ["member_moment", "ensemble"]


def test_recap_dialogue_needs_no_rename():
    index = _index(("sp0", 0.0, 10.0, 3), ("sp1", 10.0, 20.0, 3),
                   ("sp2", 20.0, 30.0, 3), ("sp3", 30.0, 40.0, 3))
    beats = [{"role": "build", "span_ids": ["sp0", "sp1"], "narration": [], "labels": []},
             {"role": "climax", "span_ids": ["sp2", "sp3"], "narration": [], "labels": []}]
    out, removed, renamed = flesh.trim_to_budget_by_role(
        beats, index, [], 25.0, template="recap_dialogue")
    assert renamed == [] and out[1]["span_ids"] == ["sp2", "sp3"]
    assert [r["span_id"] for r in removed] == ["sp0"]


# ══════════════════════════════════════════════════════════════════════════
# 5) 실행 — 배선·격리·결정성
# ══════════════════════════════════════════════════════════════════════════

def _run(g, cands=None, **kw):
    args = dict(grid=make_grid(), work_title="형제 예능 3화", log=lambda *a: None)
    args.update(kw)
    return flesh.run_flesh(g, None, cands or [make_candidate()], **args)


def test_happy_path_builds_a_story_doc_per_episode():
    g = _FakeGemini()
    docs, audit = _run(g)
    assert list(docs) == ["c01"]
    doc = docs["c01"]
    assert doc["schema"] == flesh.SCHEMA_FLESH
    assert doc["candidate_id"] == "c01" and doc["template"] == "recap_dialogue"
    assert doc["title"] == {"line1": "형이 던진 한마디", "line2": "동생은 무너졌다"}
    assert [b["role"] for b in doc["beats"]] == ["hook", "climax"]
    # 시각은 **격자 조회**다 — 모델은 시각을 아예 답하지 않았다.
    assert doc["beats"][0]["time"] == {"start": "00:00:05.000", "end": "00:00:12.000"}
    assert audit["ok"] == 1 and audit["failed"] == 0 and audit["of"] == 1


def test_two_calls_per_episode_flesh_then_description():
    """설명은 **별도 텍스트 호출**이다(기획서 §3) — 한 응답에 합치면 메타 형식 하나에
    카피 전체가 반려된다."""
    g = _FakeGemini()
    docs, audit = _run(g)
    assert len(g.models.calls) == 2
    assert len(g.models.prompts(FLESH_MARKER)) == 1
    assert len(g.models.prompts(DESC_MARKER)) == 1
    assert docs["c01"]["description"].startswith("형제의 말싸움")
    assert docs["c01"]["hashtags"] == ["#형제", "#말싸움", "#리액션"]
    assert audit["episodes"][0]["description"]["status"] == "ok"


def test_description_prompt_does_not_repeat_the_dialogue_material():
    g = _FakeGemini()
    _run(g)
    desc_prompt = g.models.prompts(DESC_MARKER)[0]
    assert "형이 던진 한마디" in desc_prompt          # 확정 제목은 재료다
    assert QUOTE_A not in desc_prompt                # 대사 전문은 두 번 싣지 않는다
    assert "sp0001" not in desc_prompt


def test_description_failure_does_not_drop_the_episode():
    """발행 메타가 없는 편은 사람이 채울 수 있지만, 영상이 없는 편은 결번이다."""
    g = _FakeGemini(by_marker={DESC_MARKER: _ApiError(400, "bad request")})
    docs, audit = _run(g)
    assert list(docs) == ["c01"]
    assert "description" not in docs["c01"] and "hashtags" not in docs["c01"]
    assert audit["episodes"][0]["description"]["status"] == "failed"
    assert audit["episodes"][0]["description"]["reason"] == flesh.REASON_CALL_FAILED


def test_flesh_uses_the_flash_slot_and_temperature_zero():
    """모델 정책(CLAUDE.md): Pro 는 영상을 실제로 보는 호출에만. 10단계는 텍스트다."""
    g = _FakeGemini()
    _run(g)
    for call in g.models.calls:
        assert call["model"] == "gemini-3.7-flash"
        assert call["config"].temperature == video_mod.TEMPERATURE == 0.0
        assert call["config"].max_output_tokens == flesh.FLESH_MAX_OUTPUT_TOKENS
        # 영상 파트가 없다 — 텍스트 온리(핸들을 안 쓴다).
        assert all(isinstance(c, str) for c in call["contents"])


def test_slot_planning_calls_the_v3_functions(monkeypatch):
    """슬롯 배치·예산·충돌 벨트를 여기서 다시 짜지 않는다(계약 §2) — v3 함수를 부른다."""
    seen: dict[str, int] = {}

    def spy(name, fn):
        def wrapped(*a, **kw):
            seen[name] = seen.get(name, 0) + 1
            return fn(*a, **kw)
        return wrapped

    for name in ("plan_narration_slots", "verify_tts_conflicts", "trim_to_budget"):
        monkeypatch.setattr(story_mod, name, spy(name, getattr(story_mod, name)))
    docs, _audit = _run(_FakeGemini())
    assert seen == {"plan_narration_slots": 1, "verify_tts_conflicts": 1,
                    "trim_to_budget": 1}
    # 배치 결과가 문서에 실린다(cue 시각은 원본 절대초 — C2 신원 규약).
    cues = docs["c01"]["narration_cues"]
    assert cues and cues[0]["source_time_sec"] >= 5.0


def test_narration_lands_in_a_window_that_does_not_bury_the_quoted_line():
    """인용 span 은 `QUOTE_IMPORTANCE`(5) 라 내레이션 밑에서 뮤트되지 않는다 —
    다리의 인용 보호가 여기까지 이어지는지 값으로 본다."""
    docs, _ = _run(_FakeGemini())
    doc = docs["c01"]
    muted = {sid for cue in doc["narration_cues"] for sid in cue["muted_span_ids"]}
    assert "sp0001" not in muted                   # QUOTE_A 가 실린 span
    assert "sp0004" not in muted                   # QUOTE_C 가 실린 span


def test_labels_move_to_the_beat_that_owns_the_anchor():
    """라벨의 자리는 앵커가 정한다 — 모델이 다른 비트에 적어 냈어도 옮기고 기록한다."""
    resp = ok_flesh(beats=[
        {"number": 0, "role": "hook", "narration": [],
         "labels": [{"text": "(정적)", "span_id": "sp0004"}]},
        {"number": 1, "role": "climax", "narration": [], "labels": []}])
    g = _FakeGemini(by_marker={FLESH_MARKER: _response(resp)})
    docs, audit = _run(g)
    beats = docs["c01"]["beats"]
    assert beats[0]["labels"] == [] and beats[1]["labels"][0]["text"] == "(정적)"
    assert any("옮겼다" in n for n in audit["episodes"][0]["notes"])


def test_label_whose_anchor_was_trimmed_is_removed_and_recorded():
    """라벨은 예산 다듬기 **전**에 붙는다 — 앵커가 깎여 나가면 화면에 없는 시각을
    가리키게 된다. 조용히 두면 M6 가 타임라인 밖에 라벨을 그린다."""
    resp = ok_flesh(beats=[
        {"number": 0, "role": "hook", "narration": [],
         "labels": [{"text": "(정적)", "span_id": "sp0002"},
                    {"text": "(한숨)", "span_id": "sp0001"}]},
        {"number": 1, "role": "climax", "narration": [], "labels": []}])
    g = _FakeGemini(by_marker={FLESH_MARKER: _response(resp)})
    docs, audit = _run(g, max_sec=12.0)               # sp0002(4s)가 예산에 깎인다
    doc = docs["c01"]
    assert [r["span_id"] for r in doc["budget"]["removed"]] == ["sp0002"]
    assert doc["beats"][0]["span_ids"] == ["sp0001"]
    assert [x["text"] for x in doc["beats"][0]["labels"]] == ["(한숨)"]
    assert any("sp0002" in n and "뗐다" in n for n in audit["episodes"][0]["notes"])


def test_climax_beat_survives_the_budget_trim():
    """보호가 실제로 걸린다 — 예산이 모자라도 climax 비트는 통째로 남는다."""
    docs, _ = _run(_FakeGemini(), max_sec=12.0)
    doc = docs["c01"]
    assert doc["beats"][1]["role"] == "climax"
    assert doc["beats"][1]["span_ids"] == ["sp0004", "sp0005"]
    assert doc["budget"]["unmet"] is True            # 더 깎을 것이 없다 — 기록으로 남는다


def test_bridge_is_crossed_here_and_the_audit_travels():
    g = _FakeGemini()
    _docs, audit = _run(g)
    index_audit = audit["episodes"][0]["bridge"]["index"]
    assert index_audit["quoted_spans"] == 2               # 인용 두 줄이 보호를 받았다
    assert index_audit["speaker_source"] == "none"        # 10a 가 꺼져 있다
    assert bridge.NO_SPEAKER_WARNING in index_audit["warning"]


def test_episode_whose_segments_have_no_span_fails_alone():
    """조각이 격자 눈금보다 짧으면 빈 비트가 된다 — 그 편만 탈락하고 사유가 남는다."""
    bad = make_candidate("c02")
    bad["segments"] = [{"start_sec": 12.05, "end_sec": 12.1, "quote": None}]
    docs, audit = _run(_FakeGemini(), [make_candidate("c01"), bad])
    assert list(docs) == ["c01"]
    row = [r for r in audit["episodes"] if r["id"] == "c02"][0]
    assert row["status"] == "failed" and row["reason"] == flesh.REASON_NO_SPANS


def test_unknown_template_drops_only_that_episode():
    """6단계가 검증한 값이라 정상 경로에는 없다 — 구 체크포인트·편집실 재개로 들어오면
    프롬프트 조립이 크게 실패하는데, 그러면 **다른 편까지 죽는다**."""
    bad = make_candidate("c02", template="no_such_template")
    docs, audit = _run(_FakeGemini(), [make_candidate("c01"), bad])
    assert list(docs) == ["c01"]
    row = [r for r in audit["episodes"] if r["id"] == "c02"][0]
    assert row["reason"] == flesh.REASON_UNKNOWN_TEMPLATE
    assert "사용 가능" in row["detail"]


def test_one_episode_failing_does_not_touch_the_others():
    """c02 만 400 을 받는다(프롬프트에 그 편의 제목 가안이 실려 갈린다)."""
    g = _FakeGemini(by_marker={"둘째 편 가안": _ApiError(400, "bad request")})
    c2 = make_candidate("c02")
    c2["title_draft"] = {"line1": "둘째 편 가안", "line2": "펀치"}
    docs, audit = _run(g, [make_candidate("c01"), c2])
    assert list(docs) == ["c01"]
    assert audit["ok"] == 1 and audit["failed"] == 1
    row = [r for r in audit["episodes"] if r["id"] == "c02"][0]
    assert row["reason"] == flesh.REASON_CALL_FAILED
    # 400 은 permanent 다 — 재질의를 태우지 않는다(요금만 세 배).
    assert len(row["attempts"]) == 1


def test_reask_loop_then_success():
    """1차 반려(제목 24자) → 반려 사유를 실어 재질의 → 통과."""
    long_title = ok_flesh(line1="가" * 24)

    def pick(prompt, _n):
        return _response(long_title if "직전 제안 반려 사유" not in prompt else ok_flesh())

    g = _FakeGemini(by_marker={FLESH_MARKER: pick})
    docs, audit = _run(g)
    assert list(docs) == ["c01"]
    attempts = audit["episodes"][0]["attempts"]
    assert len(attempts) == 2 and attempts[0]["accepted"] is False
    assert any("20자 이내" in p for p in attempts[0]["problems"])


def test_reask_exhausted_drops_only_that_episode():
    """c02 만 매번 24자 제목을 낸다 → 재질의 소진 → **그 편만** 탈락한다."""
    g = _FakeGemini(by_marker={"둘째 편 가안": _response(ok_flesh(line1="가" * 24))})
    c2 = make_candidate("c02")
    c2["title_draft"] = {"line1": "둘째 편 가안", "line2": "펀치"}
    docs, audit = _run(g, [make_candidate("c01"), c2])
    assert list(docs) == ["c01"]
    row = [r for r in audit["episodes"] if r["id"] == "c02"][0]
    assert row["status"] == "failed" and row["reason"] == flesh.REASON_REASK_EXHAUSTED
    assert len(row["attempts"]) == 1 + story_mod.MAX_REASKS   # 1차 + 재질의 2회
    assert "20자 이내" in row["detail"]


def test_total_failure_is_loud_because_a_silent_gap_is_the_worst_failure():
    """승인이 있었는데 낼 것이 없다 = 조용한 결번. 크게 죽고 사유를 전부 싣는다."""
    g = _FakeGemini(by_marker={FLESH_MARKER: _response(ok_flesh(line1="가" * 24))})
    with pytest.raises(ValueError) as e:
        _run(g, [make_candidate("c01"), make_candidate("c02")])
    msg = str(e.value)
    assert "전량 실패" in msg and flesh.REASON_REASK_EXHAUSTED in msg
    assert "c01" in msg and "c02" in msg


def test_tts_conflict_belt_is_our_defect_and_propagates(monkeypatch):
    """벨트 위반은 **우리 코드의 결함**이다 — 편별 격리로 삼키지 않는다(v3 규율 계승)."""
    monkeypatch.setattr(story_mod, "verify_tts_conflicts",
                        lambda *a, **kw: ["cue(beat 0) ↔ sp0004"])
    with pytest.raises(AssertionError, match="충돌 벨트 위반"):
        _run(_FakeGemini())


def test_results_are_ordered_by_candidate_id_not_by_completion():
    """병렬이라 완료 순서는 정해지지 않는다 — 저장 파일이 실행마다 달라지면 안 된다."""
    g = _FakeGemini(delay_sec=0.02)
    cands = [make_candidate(cid) for cid in ("c03", "c01", "c02")]
    docs, audit = _run(g, cands, concurrency=3)
    assert list(docs) == ["c01", "c02", "c03"]
    assert [r["id"] for r in audit["episodes"]] == ["c01", "c02", "c03"]


def test_episodes_run_in_parallel():
    g = _FakeGemini(delay_sec=0.05)
    _run(g, [make_candidate(f"c{i:02d}") for i in range(4)], concurrency=4)
    assert g.models.max_in_flight > 1


def test_duplicate_ids_fail_loudly():
    with pytest.raises(ValueError, match="중복"):
        _run(_FakeGemini(), [make_candidate("c01"), make_candidate("c01")])


def test_deterministic_same_input_same_docs():
    a, _ = _run(_FakeGemini(), [make_candidate("c01"), make_candidate("c02")])
    b, _ = _run(_FakeGemini(), [make_candidate("c01"), make_candidate("c02")])
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(b, ensure_ascii=False, sort_keys=True)


def test_source_duration_comes_from_the_grid_when_not_given():
    grid = make_grid()
    del grid["source"]["duration_sec"]
    with pytest.raises(ValueError, match="소스 길이를 알 수 없다"):
        _run(_FakeGemini(), grid=grid)


# ══════════════════════════════════════════════════════════════════════════
# 6) 설명·해시태그 검증기
# ══════════════════════════════════════════════════════════════════════════

def test_description_hashtags_are_normalised_with_notes():
    got, problems = flesh.validate_description_response(
        {"description": "형제의 말싸움.", "hashtags": ["형제", "#형제", "#말 싸움", ""]})
    assert problems == []
    assert got["hashtags"] == ["#형제", "#말싸움"]
    assert any("'#' 보정" in n for n in got["notes"])
    assert any("중복 제거" in n for n in got["notes"])


def test_description_over_the_limit_is_rejected_not_cut():
    got, problems = flesh.validate_description_response(
        {"description": "가" * (flesh.DESCRIPTION_MAX_CHARS + 1), "hashtags": ["#a"]})
    assert got is None and any("이내로" in p for p in problems)


def test_description_unknown_keys_are_rejected():
    got, problems = flesh.validate_description_response(
        {"description": "x", "hashtags": [], "title": "덧붙임"})
    assert got is None and any("모르는 열쇠" in p and "title" in p for p in problems)


def test_description_reask_then_success():
    calls = {"n": 0}

    def pick(prompt, _n):
        calls["n"] += 1
        if calls["n"] == 1:
            return _response({"description": "", "hashtags": []})
        return _response(ok_desc())

    g = _FakeGemini(by_marker={DESC_MARKER: pick})
    docs, audit = _run(g)
    assert docs["c01"]["hashtags"] == ["#형제", "#말싸움", "#리액션"]
    assert len(audit["episodes"][0]["description"]["attempts"]) == 2


# ══════════════════════════════════════════════════════════════════════════
# 7) 계약 상수
# ══════════════════════════════════════════════════════════════════════════

def test_title_limit_is_the_renderer_constraint_not_a_local_number():
    from app.modules.style_compose import MAX_TITLE_LINE_CHARS

    assert flesh.TITLE_MAX_CHARS == MAX_TITLE_LINE_CHARS == 20


def test_contract_constants_are_pinned():
    assert flesh.FLESH_MAX_OUTPUT_TOKENS == 16384
    assert flesh.NARRATION_SENT_CHARS == (12, 16)
    assert flesh.LABELS_PER_EPISODE == (2, 4)
    assert flesh.FLESH_CONCURRENCY == 4
    assert flesh.REASON_NO_SPANS == "bridge_no_spans"
    assert flesh.REASON_CALL_FAILED == "call_failed"
    assert flesh.REASON_REASK_EXHAUSTED == "reask_exhausted"
