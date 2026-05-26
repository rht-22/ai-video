"""PR-4 — STORY_COMPOSITION_PROMPT 의 storyline.tts_cues 출력 검증·정규화 단위 테스트.

검증 대상: _normalize_storyline_tts_cues 헬퍼 (gemini_client.py 신규).

기존 plan_tts_cues 안의 후처리 로직과 동일 패턴을 별도 함수로 분리해 storyline
응답에서도 사용. PR-5 에서 plan_tts_cues 통째 삭제 시 단일화.
"""
from __future__ import annotations

from app.modules.gemini_client import _normalize_storyline_tts_cues


def _cue(start, end, text="x", voice="ko_female", speed="normal", **extra):
    d = {
        "start_sec": float(start), "end_sec": float(end),
        "text": text, "voice": voice, "speed": speed,
    }
    d.update(extra)
    return d


# ──────────────────────────────────────────────────────────────
# voice / speed 라벨 검증
# ──────────────────────────────────────────────────────────────


def test_invalid_voice_falls_back_to_ko_female():
    out = _normalize_storyline_tts_cues([_cue(0, 3, voice="narrator")])
    assert out[0]["voice"] == "ko_female"


def test_invalid_speed_falls_back_to_normal():
    out = _normalize_storyline_tts_cues([_cue(0, 3, speed="hyper_fast")])
    assert out[0]["speed"] == "normal"


def test_valid_voice_speed_preserved():
    out = _normalize_storyline_tts_cues([_cue(0, 3, voice="ko_male_low", speed="slow")])
    assert out[0]["voice"] == "ko_male_low"
    assert out[0]["speed"] == "slow"


def test_all_valid_voice_labels_accepted():
    for v in ("ko_female", "ko_female_high", "ko_male", "ko_male_low",
              "chat_emma", "chat_brian", "chat_seraphina", "chat_florian"):
        out = _normalize_storyline_tts_cues([_cue(0, 3, voice=v)])
        assert out[0]["voice"] == v, f"voice {v} 가 fallback 됨"


def test_all_valid_speed_labels_accepted():
    for s in ("very_slow", "slow", "normal", "fast", "very_fast"):
        out = _normalize_storyline_tts_cues([_cue(0, 3, speed=s)])
        assert out[0]["speed"] == s, f"speed {s} 가 fallback 됨"


# ──────────────────────────────────────────────────────────────
# 시간 검증
# ──────────────────────────────────────────────────────────────


def test_drops_cue_with_end_lte_start():
    out = _normalize_storyline_tts_cues([_cue(5, 5), _cue(10, 8), _cue(0, 3)])
    # 5==5 와 10>8 둘 다 drop, 0<3 만 남음
    assert len(out) == 1
    assert out[0]["start_sec"] == 0


def test_drops_cue_missing_required_fields():
    out = _normalize_storyline_tts_cues([
        {"start_sec": 0, "end_sec": 3},  # text 누락
        {"start_sec": 0, "text": "x"},   # end_sec 누락
        _cue(5, 8, text="ok"),
    ])
    assert len(out) == 1
    assert out[0]["text"] == "ok"


def test_sorts_by_start_sec():
    out = _normalize_storyline_tts_cues([_cue(20, 23), _cue(0, 3), _cue(10, 13)])
    assert [c["start_sec"] for c in out] == [0, 10, 20]


def test_removes_overlap_by_shifting_later_cue_start():
    # cue1: 0~5, cue2: 3~7 (겹침) → cue2 start 를 cue1.end + 0.05 = 5.05 로
    out = _normalize_storyline_tts_cues([_cue(0, 5, text="a"), _cue(3, 7, text="b")])
    assert len(out) == 2
    assert out[1]["start_sec"] >= out[0]["end_sec"]


def test_drops_cue_if_overlap_fix_makes_invalid():
    # cue1: 0~10, cue2: 5~7 → cue2 start 를 10.05 로 → end 7 < start 10.05 → drop
    out = _normalize_storyline_tts_cues([_cue(0, 10, text="a"), _cue(5, 7, text="b")])
    assert len(out) == 1
    assert out[0]["text"] == "a"


def test_clamps_to_total_duration():
    # total_duration=20 이면 cue.end=25 는 drop (start<-0.5 또는 end>total+0.5 검증)
    out = _normalize_storyline_tts_cues([_cue(0, 5), _cue(22, 25), _cue(10, 15)], total_duration=20.0)
    starts = sorted(c["start_sec"] for c in out)
    assert starts == [0, 10]


def test_no_clamp_when_total_duration_none():
    out = _normalize_storyline_tts_cues([_cue(0, 5), _cue(100, 105)], total_duration=None)
    assert len(out) == 2


# ──────────────────────────────────────────────────────────────
# max_cues 클램프 — 응답 토큰 제어
# ──────────────────────────────────────────────────────────────


def test_max_cues_truncates_from_end():
    raw = [_cue(i, i + 2, text=f"cue{i}") for i in (0, 10, 20, 30, 40, 50, 60)]
    out = _normalize_storyline_tts_cues(raw, max_cues=5)
    assert len(out) == 5
    assert [c["text"] for c in out] == ["cue0", "cue10", "cue20", "cue30", "cue40"]


def test_max_cues_zero_returns_empty():
    out = _normalize_storyline_tts_cues([_cue(0, 3)], max_cues=0)
    assert out == []


def test_max_cues_none_means_no_limit():
    raw = [_cue(i, i + 2) for i in range(0, 100, 10)]
    out = _normalize_storyline_tts_cues(raw, max_cues=None)
    assert len(out) == 10


# ──────────────────────────────────────────────────────────────
# "한 쇼츠 = 한 voice" 통일
# ──────────────────────────────────────────────────────────────


def test_majority_voice_wins_when_mixed():
    raw = [
        _cue(0, 3, voice="ko_male_low"),
        _cue(10, 13, voice="ko_male_low"),
        _cue(20, 23, voice="ko_female"),  # 소수 → ko_male_low 로 통일
    ]
    out = _normalize_storyline_tts_cues(raw)
    assert {c["voice"] for c in out} == {"ko_male_low"}


def test_single_voice_preserved():
    raw = [_cue(0, 3, voice="ko_female_high"), _cue(10, 13, voice="ko_female_high")]
    out = _normalize_storyline_tts_cues(raw)
    assert all(c["voice"] == "ko_female_high" for c in out)


def test_speed_can_differ_across_cues():
    # speed 는 cue 마다 자유
    raw = [_cue(0, 3, voice="ko_male", speed="slow"),
           _cue(10, 13, voice="ko_male", speed="fast")]
    out = _normalize_storyline_tts_cues(raw)
    speeds = [c["speed"] for c in out]
    assert speeds == ["slow", "fast"]


# ──────────────────────────────────────────────────────────────
# 견고성
# ──────────────────────────────────────────────────────────────


def test_empty_input_returns_empty():
    assert _normalize_storyline_tts_cues([]) == []
    assert _normalize_storyline_tts_cues(None) == []


def test_skips_non_dict_entries():
    raw = [_cue(0, 3, text="ok"), "not a dict", None, 42, _cue(10, 13, text="ok2")]
    out = _normalize_storyline_tts_cues(raw)
    assert [c["text"] for c in out] == ["ok", "ok2"]


def test_preserves_rationale_fields():
    raw = [_cue(0, 3, voice_rationale="ko_male_low 묵직", speed_rationale="긴장")]
    out = _normalize_storyline_tts_cues(raw)
    assert out[0].get("voice_rationale") == "ko_male_low 묵직"
    assert out[0].get("speed_rationale") == "긴장"
