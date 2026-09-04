"""영문 약어 오인식 대조(arbitrate_latin) — 2026-09-04 가왕쇼 7화 'RC가'←'날씨가'."""
from __future__ import annotations

from app.v3.textcheck import arbitrate_latin, fix_span_words

HEARD = "날씨가 이렇게 너무 더운데 이렇게 와주셔서 정말 감사합니다."


def test_real_case_rc_to_nalssi():
    assert arbitrate_latin("RC가", HEARD, prev_word="또", next_word="이렇게") == "날씨가"


def test_no_change_when_model_also_heard_latin():
    assert arbitrate_latin("RC가", "RC가 이렇게 너무 더운데", next_word="이렇게") is None


def test_needs_neighbor_anchor_and_same_josa():
    assert arbitrate_latin("RC가", HEARD) is None                       # 이웃 없음 = 자리 못 잡음
    assert arbitrate_latin("RC를", HEARD, next_word="이렇게") is None    # 조사 불일치
    assert arbitrate_latin("날씨가", HEARD, next_word="이렇게") is None  # 한글 어절은 대상 아님
    assert arbitrate_latin("A가", HEARD, next_word="이렇게") is None     # 1자 라틴 = 대상 아님


def test_tie_returns_none():
    heard = "날씨가 이렇게 좋고 바다가 이렇게 넓고"
    assert arbitrate_latin("RC가", heard, next_word="이렇게") is None


def test_fix_span_words_marks_kind_without_names():
    words = [{"t0": 195.2, "t1": 195.5, "text": "또"},
             {"t0": 195.61, "t1": 196.15, "text": "RC가", "prob": 0.676},
             {"t0": 196.2, "t1": 196.5, "text": "이렇게"}]
    out, fixes = fix_span_words(words, [], HEARD)
    assert [w["text"] for w in out] == ["또", "날씨가", "이렇게"]
    assert fixes == [{"at": 195.61, "from": "RC가", "to": "날씨가", "kind": "latin"}]
    assert words[1]["text"] == "RC가"                                   # 순수(사본)
