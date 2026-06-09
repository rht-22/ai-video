"""_slice_segments_for_chunk 단위 테스트.

청크/클립 범위와 *겹치는* segment 만 input order 보존하며 슬라이스한다.
(PR-7: chunk_transcribe 단계 제거 — 전사는 video_intent_transcript 가 담당하고, gemini/자막
단계는 이 함수로 청크 범위를 슬라이스한다.)
"""
from __future__ import annotations

from app.modules.speech import SpeechSegment
from app.pipeline import _slice_segments_for_chunk


def _seg(start, end, text="x"):
    return SpeechSegment(start_sec=float(start), end_sec=float(end), text=text)


def test_slice_includes_segment_fully_inside():
    out = _slice_segments_for_chunk([_seg(10, 20, "in")], 0.0, 100.0)
    assert [s.text for s in out] == ["in"]


def test_slice_includes_segment_crossing_start_boundary():
    out = _slice_segments_for_chunk([_seg(95, 105, "cross-start")], 100.0, 200.0)
    assert [s.text for s in out] == ["cross-start"]


def test_slice_includes_segment_crossing_end_boundary():
    out = _slice_segments_for_chunk([_seg(195, 205, "cross-end")], 100.0, 200.0)
    assert [s.text for s in out] == ["cross-end"]


def test_slice_excludes_segment_entirely_outside():
    out = _slice_segments_for_chunk([_seg(0, 50, "before"), _seg(300, 320, "after")], 100.0, 200.0)
    assert out == []


def test_slice_excludes_segment_touching_only_at_boundary():
    # end == chunk_start 또는 start == chunk_end → 겹침 0 → 제외
    out = _slice_segments_for_chunk([_seg(50, 100, "touch-start"), _seg(200, 250, "touch-end")], 100.0, 200.0)
    assert out == []


def test_slice_preserves_input_order():
    segs = [_seg(110, 115, "a"), _seg(120, 125, "b"), _seg(130, 135, "c")]
    out = _slice_segments_for_chunk(segs, 100.0, 200.0)
    assert [s.text for s in out] == ["a", "b", "c"]


def test_slice_empty_input_returns_empty():
    assert _slice_segments_for_chunk([], 0.0, 100.0) == []
