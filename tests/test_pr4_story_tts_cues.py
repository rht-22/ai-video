"""PR-4(앵커 개정) — storyline.tts_cues 클립 앵커 스키마 검증·정규화 단위 테스트.

검증 대상: _normalize_storyline_tts_cues (gemini_client.py).

2026-08-04 cue 앵커 전환: LLM 은 (clip_index, offset_sec, duration_sec) 만 출력하고
정규화가 source_time_sec(원본 절대시간)를 계산한다. 절대시간 변환은 pipeline 의
_resolve_cue_anchors 담당 (tests/test_cue_anchor_resolve.py).
구 스키마(start_sec/end_sec, story 타임라인 절대시간)는 하위호환 폴백으로 역산된다.
"""
from __future__ import annotations

from app.modules.gemini_client import _normalize_storyline_tts_cues


def _cue(clip_index, offset, dur=3.0, text="x", voice="ko_female", speed="normal", **extra):
    d = {
        "clip_index": clip_index, "offset_sec": float(offset), "duration_sec": float(dur),
        "text": text, "voice": voice, "speed": speed,
    }
    d.update(extra)
    return d


def _legacy_cue(start, end, text="x", voice="ko_female", speed="normal", **extra):
    d = {
        "start_sec": float(start), "end_sec": float(end),
        "text": text, "voice": voice, "speed": speed,
    }
    d.update(extra)
    return d


# 표준 앵커 클립: hook 100~110 (10s), build 200~215 (15s), payoff 300~320 (20s)
ACLIPS = [
    {"start_sec": 100.0, "end_sec": 110.0, "chunk_index": 0, "candidate_index": 1},
    {"start_sec": 200.0, "end_sec": 215.0, "chunk_index": 1, "candidate_index": 2},
    {"start_sec": 300.0, "end_sec": 320.0, "chunk_index": 2, "candidate_index": 0},
]


def _norm(raw, **kw):
    kw.setdefault("anchor_clips", ACLIPS)
    return _normalize_storyline_tts_cues(raw, **kw)


# ──────────────────────────────────────────────────────────────
# voice / speed 라벨 검증
# ──────────────────────────────────────────────────────────────


def test_invalid_voice_falls_back_to_ko_female():
    out = _norm([_cue(0, 1, voice="narrator")])
    assert out[0]["voice"] == "ko_female"


def test_invalid_speed_falls_back_to_normal():
    out = _norm([_cue(0, 1, speed="hyper_fast")])
    assert out[0]["speed"] == "normal"


def test_valid_voice_speed_preserved():
    out = _norm([_cue(0, 1, voice="ko_male_low", speed="slow")])
    assert out[0]["voice"] == "ko_male_low"
    assert out[0]["speed"] == "slow"


def test_all_valid_voice_labels_accepted():
    for v in ("ko_female", "ko_female_high", "ko_male", "ko_male_low",
              "chat_emma", "chat_brian", "chat_seraphina", "chat_florian"):
        out = _norm([_cue(0, 1, voice=v)])
        assert out[0]["voice"] == v, f"voice {v} 가 fallback 됨"


def test_all_valid_speed_labels_accepted():
    for s in ("very_slow", "slow", "normal", "fast", "very_fast"):
        out = _norm([_cue(0, 1, speed=s)])
        assert out[0]["speed"] == s, f"speed {s} 가 fallback 됨"


# ──────────────────────────────────────────────────────────────
# 앵커 검증 — clip_index / offset / duration / source_time
# ──────────────────────────────────────────────────────────────


def test_source_time_is_clip_start_plus_offset():
    out = _norm([_cue(1, 2.5)])
    assert out[0]["source_time_sec"] == 200.0 + 2.5


def test_records_anchor_candidate_key():
    out = _norm([_cue(2, 1.0)])
    assert out[0]["chunk_index"] == 2
    assert out[0]["candidate_index"] == 0


def test_drops_clip_index_out_of_range():
    # clip_index=5 는 환각 → 드롭
    out = _norm([_cue(5, 1.0, text="hallucinated"), _cue(0, 1.0, text="ok")])
    assert [c["text"] for c in out] == ["ok"]


def test_drops_negative_clip_index():
    out = _norm([_cue(-1, 1.0), _cue(0, 1.0, text="ok")])
    assert [c["text"] for c in out] == ["ok"]


def test_offset_clamped_to_clip_length():
    # hook 은 10초 — offset 25 는 10 으로 클램프 → source_time = 110
    out = _norm([_cue(0, 25.0)])
    assert out[0]["offset_sec"] == 10.0
    assert out[0]["source_time_sec"] == 110.0


def test_negative_offset_clamped_to_zero():
    out = _norm([_cue(1, -3.0)])
    assert out[0]["offset_sec"] == 0.0
    assert out[0]["source_time_sec"] == 200.0


def test_drops_nonpositive_duration():
    out = _norm([_cue(0, 1.0, dur=0.0), _cue(0, 2.0, dur=-2.0), _cue(0, 3.0, dur=4.0, text="ok")])
    assert [c["text"] for c in out] == ["ok"]


def test_drops_cue_missing_text():
    out = _norm([
        {"clip_index": 0, "offset_sec": 1.0, "duration_sec": 3.0},  # text 누락
        _cue(0, 2.0, text="ok"),
    ])
    assert [c["text"] for c in out] == ["ok"]


def test_sorts_by_clip_index_then_offset():
    out = _norm([_cue(2, 1.0, text="c"), _cue(0, 5.0, text="b"), _cue(0, 1.0, text="a")])
    assert [c["text"] for c in out] == ["a", "b", "c"]


def test_preserves_clip_role_field():
    out = _norm([_cue(0, 1.0, clip_role="hook")])
    assert out[0]["clip_role"] == "hook"


# ──────────────────────────────────────────────────────────────
# 구 스키마 하위호환 — start_sec/end_sec(story 타임라인) → 앵커 역산
# ──────────────────────────────────────────────────────────────


def test_legacy_cue_converted_to_anchor():
    # story 타임라인: hook 0~10, build 10~25, payoff 25~45. start=12 → build(idx1) offset 2
    out = _norm([_legacy_cue(12.0, 16.0)])
    assert len(out) == 1
    assert out[0]["clip_index"] == 1
    assert out[0]["offset_sec"] == 2.0
    assert out[0]["duration_sec"] == 4.0
    assert out[0]["source_time_sec"] == 202.0


def test_legacy_cue_in_first_clip():
    out = _norm([_legacy_cue(0.5, 5.0)])
    assert out[0]["clip_index"] == 0
    assert out[0]["offset_sec"] == 0.5
    assert out[0]["source_time_sec"] == 100.5


def test_legacy_cue_beyond_total_dropped():
    # story 합계 45초 — start=50 은 역산 불가 → 드롭
    out = _norm([_legacy_cue(50.0, 55.0)])
    assert out == []


def test_legacy_cue_without_anchor_clips_dropped():
    out = _normalize_storyline_tts_cues([_legacy_cue(0.0, 5.0)], anchor_clips=None)
    assert out == []


def test_legacy_cue_end_lte_start_dropped():
    out = _norm([_legacy_cue(5, 5), _legacy_cue(10, 8), _legacy_cue(0, 3, text="ok")])
    assert [c["text"] for c in out] == ["ok"]


# ──────────────────────────────────────────────────────────────
# max_cues 클램프 — 응답 토큰 제어
# ──────────────────────────────────────────────────────────────


def test_max_cues_truncates_from_end():
    raw = [_cue(0, i, text=f"cue{i}") for i in (0, 1, 2, 3, 4, 5, 6)]
    out = _norm(raw, max_cues=5)
    assert len(out) == 5
    assert [c["text"] for c in out] == ["cue0", "cue1", "cue2", "cue3", "cue4"]


def test_max_cues_zero_returns_empty():
    out = _norm([_cue(0, 1)], max_cues=0)
    assert out == []


def test_max_cues_none_means_no_limit():
    raw = [_cue(0, i * 0.5) for i in range(10)]
    out = _norm(raw, max_cues=None)
    assert len(out) == 10


# ──────────────────────────────────────────────────────────────
# "한 쇼츠 = 한 voice" 통일
# ──────────────────────────────────────────────────────────────


def test_majority_voice_wins_when_mixed():
    raw = [
        _cue(0, 1, voice="ko_male_low"),
        _cue(1, 1, voice="ko_male_low"),
        _cue(2, 1, voice="ko_female"),  # 소수 → ko_male_low 로 통일
    ]
    out = _norm(raw)
    assert {c["voice"] for c in out} == {"ko_male_low"}


def test_single_voice_preserved():
    raw = [_cue(0, 1, voice="ko_female_high"), _cue(1, 1, voice="ko_female_high")]
    out = _norm(raw)
    assert all(c["voice"] == "ko_female_high" for c in out)


def test_speed_can_differ_across_cues():
    # speed 는 cue 마다 자유
    raw = [_cue(0, 1, voice="ko_male", speed="slow"),
           _cue(1, 1, voice="ko_male", speed="fast")]
    out = _norm(raw)
    speeds = [c["speed"] for c in out]
    assert speeds == ["slow", "fast"]


# ──────────────────────────────────────────────────────────────
# 견고성
# ──────────────────────────────────────────────────────────────


def test_empty_input_returns_empty():
    assert _norm([]) == []
    assert _norm(None) == []


def test_skips_non_dict_entries():
    raw = [_cue(0, 1, text="ok"), "not a dict", None, 42, _cue(1, 1, text="ok2")]
    out = _norm(raw)
    assert [c["text"] for c in out] == ["ok", "ok2"]


def test_preserves_rationale_fields():
    raw = [_cue(0, 1, voice_rationale="ko_male_low 묵직", speed_rationale="긴장")]
    out = _norm(raw)
    assert out[0].get("voice_rationale") == "ko_male_low 묵직"
    assert out[0].get("speed_rationale") == "긴장"


def test_anchor_clips_accepts_storyclip_objects():
    from app.modules.story_builder import StoryClip
    clips = [StoryClip(role="hook", start_sec=100.0, end_sec=110.0, subtitle="",
                       use_original_audio=True, chunk_index=3, candidate_index=7)]
    out = _normalize_storyline_tts_cues([_cue(0, 2.0)], anchor_clips=clips)
    assert out[0]["source_time_sec"] == 102.0
    assert out[0]["chunk_index"] == 3
    assert out[0]["candidate_index"] == 7
