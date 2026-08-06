"""타임스탬프 ↔ 내용 대조 — 2026-08-06 샤먼: 미신전 2화 실측 회귀.

청크 분석이 내용은 맞게 보면서 시간축만 밀린 응답을 냈다. 인용 대사의 실제 위치(전사)는
518.9~524.2 인데 후보는 830~887 을 주장했고, 같은 응답의 다른 후보는 소스 길이(875s)를
넘는 933~997 이었다. 길이 검사만으로는 전자를 못 잡는다 — 범위 안이라 그대로 렌더된다.
"""
from __future__ import annotations

from app.modules.timestamp_check import (
    candidate_problem,
    filter_candidates,
    find_quote_times,
    normalize,
    quotes_of,
)

LINE = "딱 봤는데 이거 목매달아 죽은 귀신인데 얘가 장난을 치는구나 라고 봤죠."
SEGMENTS = [
    {"start_sec": 1.78, "end_sec": 3.46, "text": "내 새끼 지키러 왔어요."},
    {"start_sec": 518.9, "end_sec": 524.2, "text": LINE},
    {"start_sec": 830.0, "end_sec": 846.0, "text": "제작진이 카메라를 정리한다."},
]


def _cand(start, end, transcript=LINE, **kw):
    return {"chunk_index": 0, "candidate_index": 4, "start_sec": start, "end_sec": end,
            "transcript": transcript, **kw}


def test_normalize_strips_bracket_times_and_punctuation():
    assert normalize("[518.9~524.2] 딱 봤는데, 이거!") == normalize("딱 봤는데 이거")


def test_quotes_include_transcript_and_beat_dialogue():
    q = quotes_of(_cand(830, 887, beats=[{"dialogue": [{"speaker": "박희정", "line": LINE}]}]))
    assert len(q) == 2


def test_short_quotes_are_ignored():
    # 짧은 인용은 우연 일치가 잦아 대조 대상에서 뺀다
    assert quotes_of(_cand(830, 887, transcript="네?")) == []


def test_find_quote_times_locates_line():
    assert find_quote_times(LINE, SEGMENTS) == [(518.9, 524.2)]


def test_detects_shifted_timestamp():
    # 실측 사례: 대사는 518.9 에 있는데 후보는 830~887 을 주장
    prob = candidate_problem(_cand(830.0, 887.0), SEGMENTS)
    assert prob and "518.9" in prob and "차이 311초" in prob


def test_accepts_matching_timestamp():
    assert candidate_problem(_cand(515.0, 530.0), SEGMENTS) is None


def test_tolerates_boundary_offset():
    # 경계에서 몇 초 어긋난 것은 통과 — 과잉 차단하면 멀쩡한 후보까지 버린다
    assert candidate_problem(_cand(524.0, 560.0), SEGMENTS) is None


def test_unknown_when_quote_not_in_transcript():
    # 대사 없는 장면·전사 실패는 '모름' → 판단하지 않는다
    assert candidate_problem(_cand(100.0, 130.0, transcript="전사에 없는 대사입니다 정말로"),
                             SEGMENTS) is None


def test_filter_drops_only_mismatched():
    good, bad = _cand(515.0, 530.0), _cand(830.0, 887.0)
    keep, notes = filter_candidates([good, bad], [{"segments": SEGMENTS}])
    assert keep == [good] and len(notes) == 1


def test_filter_passes_through_when_no_transcript():
    # 전사가 통째로 없으면 대조 불가 — 후보를 건드리지 않는다
    cands = [_cand(830.0, 887.0)]
    assert filter_candidates(cands, [])[0] == cands


def test_timeline_accepts_speechsegment_objects():
    """🛑 회귀 방지 — 실행 경로의 세그먼트는 dict 가 아니라 SpeechSegment 객체다.
    dict 전용 s.get() 가정이면 AttributeError 로 생성이 rc=1 로 죽는다
    (2026-08-06 맥5 실측: 커리어데이 EP2 수동 생성 즉사)."""
    from app.modules.speech import SpeechSegment
    obj_segments = [SpeechSegment(start_sec=s["start_sec"], end_sec=s["end_sec"], text=s["text"])
                    for s in SEGMENTS]
    good, bad = _cand(515.0, 530.0), _cand(830.0, 887.0)
    keep, notes = filter_candidates([good, bad], [{"segments": obj_segments}])
    assert keep == [good] and len(notes) == 1


def test_filter_candidates_accepts_object_chunks():
    # 청크 묶음이 객체로 와도 죽지 않아야 한다 — 세그먼트와 같은 이유(21d2a18 의 남은 절반)
    from types import SimpleNamespace
    from app.modules.speech import SpeechSegment
    chunk = SimpleNamespace(segments=[SpeechSegment(start_sec=518.9, end_sec=524.2, text=LINE)])
    assert filter_candidates([_cand(515.0, 530.0)], [chunk])[0] != []
    assert filter_candidates([_cand(830.0, 887.0)], [chunk])[0] == []
