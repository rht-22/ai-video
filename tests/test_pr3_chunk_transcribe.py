"""PR-3 — chunk_transcribe 신규 단계 단위 테스트.

검증 대상:
1. _slice_segments_for_chunk — SRT 세그먼트를 청크 범위와 겹침 기준으로 슬라이스 (input order 보존).
2. _serialize_chunk_transcripts / _deserialize_chunk_transcripts — JSON round-trip.
3. transcribe_chunks — SRT 분기 (parse_subtitle 결과를 청크별 슬라이스로 매핑).

Whisper 분기는 외부 모델 의존이라 e2e 영역 — 본 파일에서는 transcriber 콜러블 DI 로
가짜 함수 주입해서 *호출이 청크별 split_path 로 들어가는지* 만 검증.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.speech import SpeechSegment
from app.pipeline import (
    _deserialize_chunk_transcripts,
    _serialize_chunk_transcripts,
    _slice_segments_for_chunk,
    transcribe_chunks,
)


def _seg(start, end, text="x"):
    return SpeechSegment(start_sec=float(start), end_sec=float(end), text=text)


def _chunk(index, start, end, split_path=None, actual_start_sec=None):
    return SimpleNamespace(
        index=index,
        start_sec=float(start),
        end_sec=float(end),
        split_path=split_path,
        actual_start_sec=actual_start_sec,
    )


# ──────────────────────────────────────────────────────────────
# _slice_segments_for_chunk
# ──────────────────────────────────────────────────────────────


def test_slice_includes_segment_fully_inside():
    segs = [_seg(10, 20, "in")]
    out = _slice_segments_for_chunk(segs, 0.0, 100.0)
    assert [s.text for s in out] == ["in"]


def test_slice_includes_segment_crossing_start_boundary():
    # SRT 세그먼트 (95~105) 가 청크 (100~200) 의 시작 경계를 넘어감 → 포함
    segs = [_seg(95, 105, "cross-start")]
    out = _slice_segments_for_chunk(segs, 100.0, 200.0)
    assert [s.text for s in out] == ["cross-start"]


def test_slice_includes_segment_crossing_end_boundary():
    segs = [_seg(195, 205, "cross-end")]
    out = _slice_segments_for_chunk(segs, 100.0, 200.0)
    assert [s.text for s in out] == ["cross-end"]


def test_slice_excludes_segment_entirely_outside():
    segs = [_seg(0, 50, "before"), _seg(300, 320, "after")]
    out = _slice_segments_for_chunk(segs, 100.0, 200.0)
    assert out == []


def test_slice_excludes_segment_touching_only_at_boundary():
    # SRT 세그먼트가 청크 시작 시점에서 정확히 끝나면 (end == chunk_start) 겹침 0 → 제외
    segs = [_seg(50, 100, "touch-start"), _seg(200, 250, "touch-end")]
    out = _slice_segments_for_chunk(segs, 100.0, 200.0)
    assert out == []


def test_slice_preserves_input_order():
    segs = [_seg(110, 115, "a"), _seg(120, 125, "b"), _seg(130, 135, "c")]
    out = _slice_segments_for_chunk(segs, 100.0, 200.0)
    assert [s.text for s in out] == ["a", "b", "c"]


def test_slice_empty_input_returns_empty():
    assert _slice_segments_for_chunk([], 0.0, 100.0) == []


# ──────────────────────────────────────────────────────────────
# serialize / deserialize round-trip
# ──────────────────────────────────────────────────────────────


def test_serialize_deserialize_round_trip():
    in_mem = [
        {"chunk_index": 0, "segments": [_seg(10, 20, "한국어 텍스트"), _seg(30, 40, "two")]},
        {"chunk_index": 1, "segments": []},
        {"chunk_index": 2, "segments": [_seg(600.5, 605.1, "with quotes \"x\"")]},
    ]
    payload = _serialize_chunk_transcripts(in_mem)
    # JSON-safe 확인
    text = json.dumps(payload, ensure_ascii=False)
    roundtrip = _deserialize_chunk_transcripts(json.loads(text))
    assert len(roundtrip) == 3
    for orig, back in zip(in_mem, roundtrip):
        assert orig["chunk_index"] == back["chunk_index"]
        assert len(orig["segments"]) == len(back["segments"])
        for a, b in zip(orig["segments"], back["segments"]):
            assert isinstance(b, SpeechSegment)
            assert a.start_sec == b.start_sec
            assert a.end_sec == b.end_sec
            assert a.text == b.text


def test_deserialize_empty_input():
    assert _deserialize_chunk_transcripts([]) == []


def test_deserialize_skips_malformed_entries():
    # chunk_index 누락된 entry 는 skip, segments 누락은 빈 리스트 처리
    data = [
        {"chunk_index": 0, "segments": [{"start_sec": 1.0, "end_sec": 2.0, "text": "ok"}]},
        {"segments": []},  # chunk_index 누락 → skip
        {"chunk_index": 1},  # segments 누락 → 빈 리스트
    ]
    out = _deserialize_chunk_transcripts(data)
    assert len(out) == 2
    assert out[0]["chunk_index"] == 0
    assert len(out[0]["segments"]) == 1
    assert out[1]["chunk_index"] == 1
    assert out[1]["segments"] == []


# ──────────────────────────────────────────────────────────────
# transcribe_chunks — SRT 분기
# ──────────────────────────────────────────────────────────────


def test_transcribe_chunks_srt_branch(tmp_path: Path):
    # SRT 파일 작성
    srt = tmp_path / "test.srt"
    srt.write_text(
        "1\n00:00:10,000 --> 00:00:15,000\nin-chunk-0\n\n"
        "2\n00:01:30,000 --> 00:01:35,000\nstraddles-0-1\n\n"
        "3\n00:03:00,000 --> 00:03:05,000\nin-chunk-1\n\n"
        "4\n00:05:00,000 --> 00:05:05,000\noutside\n\n",
        encoding="utf-8",
    )
    chunks = [
        _chunk(0, 0.0, 100.0),     # 0~100s
        _chunk(1, 90.0, 250.0),    # 90~250s (overlap 90~100)
    ]
    out = transcribe_chunks(
        chunks=chunks, srt_path=srt,
        audio_workdir=tmp_path,
    )
    assert len(out) == 2
    # chunk 0: in-chunk-0 (10~15), straddles-0-1 (90~95)
    chunk0_texts = [s.text for s in out[0]["segments"]]
    assert chunk0_texts == ["in-chunk-0", "straddles-0-1"]
    # chunk 1: straddles-0-1 (90~95, 청크 진입부와 겹침), in-chunk-1 (180~185)
    chunk1_texts = [s.text for s in out[1]["segments"]]
    assert chunk1_texts == ["straddles-0-1", "in-chunk-1"]


def test_transcribe_chunks_srt_missing_returns_whisper_branch(tmp_path: Path):
    # SRT 없음 → transcriber DI 콜러블이 청크별로 호출되는지 검증
    calls: list[Path] = []

    def fake_transcriber(audio_path: Path) -> list[SpeechSegment]:
        calls.append(audio_path)
        # 항상 chunk-relative 한 1개 세그먼트 반환
        return [SpeechSegment(start_sec=5.0, end_sec=10.0, text=f"whisper:{audio_path.name}")]

    split0 = tmp_path / "chunk_0.mp4"
    split0.write_bytes(b"fake")
    split1 = tmp_path / "chunk_1.mp4"
    split1.write_bytes(b"fake")
    chunks = [
        _chunk(0, 0.0, 100.0, split_path=split0, actual_start_sec=0.0),
        _chunk(1, 100.0, 200.0, split_path=split1, actual_start_sec=0.0),
    ]
    out = transcribe_chunks(
        chunks=chunks, srt_path=None,
        audio_workdir=tmp_path,
        transcriber=fake_transcriber,
    )
    assert len(out) == 2
    assert len(calls) == 2
    # chunk-relative (5~10s) 를 절대시간(+ chunk.start_sec)으로 변환했는지
    assert out[0]["segments"][0].start_sec == 5.0   # chunk 0 시작 0s + 5s = 5s
    assert out[1]["segments"][0].start_sec == 105.0 # chunk 1 시작 100s + 5s = 105s


def test_transcribe_chunks_srt_missing_no_split_path(tmp_path: Path):
    # SRT 없고 split_path 도 없으면 Whisper 분기에서 빈 segments 반환 (안전)
    chunks = [_chunk(0, 0.0, 100.0)]
    out = transcribe_chunks(
        chunks=chunks, srt_path=None,
        audio_workdir=tmp_path,
        transcriber=lambda p: pytest.fail("should not be called"),  # 호출되면 실패
    )
    assert out == [{"chunk_index": 0, "segments": []}]
