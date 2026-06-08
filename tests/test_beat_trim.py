"""beat_trimmer 단위 테스트 — 내용 기반(beats) 길이 단축.

검증 대상:
1. _kept_intervals_from_drops — clip 범위 − 제거 beat 구간. 컷 경계는 beat 경계를 그대로
   사용한다(Whisper 문장경계 스냅 없음). sub-min 조각 폐기.
2. beat_trim_storyline — 폴백 조건, 보호 규칙, target_min ceiling, 분할·메타 보존.

PR-6: beats 는 analyze_chunk 가 발화/의미 단위로 나누고(프롬프트가 "발화 도중 끊지 말 것"을
강제), 컷은 beat 경계에서만 일어나므로 별도 Whisper 스냅을 하지 않는다.
"""
from __future__ import annotations

from app.modules.beat_trimmer import _kept_intervals_from_drops, beat_trim_storyline
from app.modules.silence_cutter import Interval
from app.modules.story_builder import StoryClip


def _beat(s, e, importance="supporting", carries_payoff=False):
    return {
        "start_sec": float(s), "end_sec": float(e),
        "summary": "요약", "mood": "긴장", "location": "loc",
        "characters": ["A"], "dialogue": [],
        "importance": importance, "carries_payoff": carries_payoff,
    }


def _clip(start, end, role="build", chunk=0, cand=0, visual_essential=False, subtitle="sub", tts="tts"):
    return StoryClip(
        role=role, start_sec=float(start), end_sec=float(end), subtitle=subtitle,
        use_original_audio=True, pacing_note="", chunk_index=chunk, candidate_index=cand,
        character_focus=("A",), visual_essential=visual_essential, tts_draft=tts,
    )


def _lookup(beats, chunk=0, cand=0, start=0.0, end=70.0):
    return {(chunk, cand): {
        "chunk_index": chunk, "candidate_index": cand,
        "start_sec": start, "end_sec": end, "beats": beats,
    }}


# ──────────────────────────────────────────────────────────────
# _kept_intervals_from_drops — beat 경계 그대로 사용 (Whisper 스냅 없음)
# ──────────────────────────────────────────────────────────────


def test_kept_no_drops_returns_whole_clip():
    beats = [_beat(0, 10), _beat(10, 20), _beat(20, 30)]
    assert _kept_intervals_from_drops((0, 30), beats, set()) == [Interval(0.0, 30.0)]


def test_kept_middle_drop_splits_into_two():
    beats = [_beat(0, 10), _beat(10, 20), _beat(20, 30)]
    out = _kept_intervals_from_drops((0, 30), beats, {1})
    assert out == [Interval(0.0, 10.0), Interval(20.0, 30.0)]


def test_kept_edge_drop_stays_contiguous():
    beats = [_beat(0, 10), _beat(10, 20), _beat(20, 30)]
    assert _kept_intervals_from_drops((0, 30), beats, {0}) == [Interval(10.0, 30.0)]


def test_kept_preserves_uncovered_gap():
    # beats가 0~5, 20~30 구간을 안 덮음. 5~10 beat 제거 → 0~5 gap·10~30 유지.
    beats = [_beat(5, 10), _beat(15, 20)]
    out = _kept_intervals_from_drops((0, 30), beats, {0})
    assert out == [Interval(0.0, 5.0), Interval(10.0, 30.0)]


def test_kept_cut_boundaries_are_exact_beat_boundaries():
    # 컷 경계는 제거된 beat 의 경계(10, 20)를 그대로 쓴다 — 스냅/보정 없음.
    # (beats 가 이미 발화 단위라 발화 중간이 아니라는 전제)
    beats = [_beat(0, 10), _beat(10, 20), _beat(20, 30)]
    out = _kept_intervals_from_drops((0, 30), beats, {1})
    assert out == [Interval(0.0, 10.0), Interval(20.0, 30.0)]


def test_kept_discards_sub_min_fragment():
    # b1(2~20) 제거 → (0,2) 2s 폐기, (20,30) 유지
    beats = [_beat(0, 2), _beat(2, 20), _beat(20, 30)]
    out = _kept_intervals_from_drops((0, 30), beats, {1}, min_clip_dur=3.0)
    assert out == [Interval(20.0, 30.0)]


def test_kept_all_sub_min_keeps_longest():
    beats = [_beat(0, 2.5), _beat(2.5, 3.0), _beat(3.0, 5.0)]
    out = _kept_intervals_from_drops((0, 5), beats, {1}, min_clip_dur=3.0)
    assert out == [Interval(0.0, 2.5)]  # 둘 다 <3 → 가장 긴 (0,2.5)


# ──────────────────────────────────────────────────────────────
# beat_trim_storyline
# ──────────────────────────────────────────────────────────────


def test_trim_under_budget_returns_empty():
    clips = [_clip(0, 30)]
    out, msg = beat_trim_storyline(
        clips, _lookup([_beat(0, 30)], end=30),
        target_min=40, target_max=60, flash_drop_fn=None,
    )
    assert out == [] and msg == ""


def test_trim_missing_beats_returns_empty():
    clips = [_clip(0, 70)]
    lookup = {(0, 0): {"chunk_index": 0, "candidate_index": 0, "start_sec": 0, "end_sec": 70}}  # no beats
    out, msg = beat_trim_storyline(
        clips, lookup, target_min=40, target_max=60, flash_drop_fn=None,
    )
    assert out == [] and msg == ""


def test_trim_deterministic_drops_least_important():
    # 70s 단일 clip, 7 beats(10s). 첫/마지막 보호. droppable 먼저 제거.
    beats = [
        _beat(0, 10, "core"), _beat(10, 20, "droppable"), _beat(20, 30, "supporting"),
        _beat(30, 40, "core"), _beat(40, 50, "droppable"), _beat(50, 60, "supporting"),
        _beat(60, 70, "core"),
    ]
    clips = [_clip(0, 70, role="build", visual_essential=True)]
    out, msg = beat_trim_storyline(
        clips, _lookup(beats, end=70),
        target_min=40, target_max=60, flash_drop_fn=None,
    )
    # must_remove=10 → 첫 droppable(10~20) 하나만 제거 → 2 클립
    assert len(out) == 2
    assert out[0].start_sec == 0.0 and out[0].end_sec == 10.0
    assert out[1].start_sec == 20.0 and out[1].end_sec == 70.0
    assert abs(sum(c.end_sec - c.start_sec for c in out) - 60.0) < 1e-6
    assert all(c.visual_essential for c in out)  # 보존
    assert out[0].subtitle == "sub" and out[1].subtitle == ""  # 첫 sub-clip에만


def test_trim_protects_carries_payoff_beat():
    # 유일한 droppable 이 carries_payoff → 보호 → 제거 불가 → ([], "")
    beats = [_beat(0, 30, "core"), _beat(30, 40, "droppable", carries_payoff=True), _beat(40, 70, "core")]
    clips = [_clip(0, 70)]
    out, msg = beat_trim_storyline(
        clips, _lookup(beats, end=70),
        target_min=40, target_max=60, flash_drop_fn=None,
    )
    assert out == [] and msg == ""


def test_trim_flash_guided_drops():
    beats = [_beat(i * 10, i * 10 + 10, "supporting") for i in range(7)]  # 0~70
    beats[0]["importance"] = "core"
    beats[6]["importance"] = "core"
    clips = [_clip(0, 70)]

    def fake_flash(payload):
        return {"drops": [{"clip_idx": 0, "beat_idx": 2}], "reason": "test"}

    out, msg = beat_trim_storyline(
        clips, _lookup(beats, end=70),
        target_min=40, target_max=60, flash_drop_fn=fake_flash,
    )
    # beat2(20~30) 제거 → (0,20)+(30,70)
    assert len(out) == 2
    assert out[0].start_sec == 0.0 and out[0].end_sec == 20.0
    assert out[1].start_sec == 30.0 and out[1].end_sec == 70.0


def test_trim_ceiling_blocks_over_large_drop():
    # 100s clip, target_min=40 → ceiling=60. 유일 droppable 이 65s(>ceiling) → 제거 불가 → ([], "")
    beats = [_beat(0, 10, "core"), _beat(10, 75, "droppable"), _beat(75, 100, "core")]
    clips = [_clip(0, 100)]
    out, msg = beat_trim_storyline(
        clips, _lookup(beats, end=100),
        target_min=40, target_max=60, flash_drop_fn=None,
    )
    assert out == [] and msg == ""


def test_trim_flash_failure_falls_back_to_deterministic():
    beats = [
        _beat(0, 10, "core"), _beat(10, 20, "droppable"), _beat(20, 30, "supporting"),
        _beat(30, 40, "core"), _beat(40, 50, "droppable"), _beat(50, 60, "supporting"),
        _beat(60, 70, "core"),
    ]
    clips = [_clip(0, 70)]

    def boom(payload):
        raise RuntimeError("flash down")

    out, msg = beat_trim_storyline(
        clips, _lookup(beats, end=70),
        target_min=40, target_max=60, flash_drop_fn=boom,
    )
    # Flash 예외 → 결정적 폴백 → droppable(10~20) 제거
    assert len(out) == 2
    assert out[0].end_sec == 10.0 and out[1].start_sec == 20.0
