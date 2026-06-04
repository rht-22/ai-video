"""place_tts_cues_with_video 단위 테스트.

google-genai 클라이언트는 mock 하고, 응답 처리/검증/SRT 모드/폴백 동작만 확인.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.modules.gemini_client import (
    GeminiClient,
    GeminiConfig,
    _validate_dialogue_segments,
    _validate_place_decisions,
    _validate_silence_intervals,
)


# ──────────────────────────────────────────────────────────────
# _validate_place_decisions
# ──────────────────────────────────────────────────────────────


def _planned(ci, start=10.0, dur=3.0, text=""):
    return {
        "cue_index": ci,
        "text": text,
        "original_start_sec": start,
        "fit_actual_sec": dur,
        "voice_rationale": "",
        "speed_rationale": "",
    }


def test_validate_place_decisions_clamps_end_to_duration():
    data = {"placements": [
        {"cue_index": 0, "start_sec": 58.0, "rationale": "끝부분"},
    ]}
    out = _validate_place_decisions(
        data, [_planned(0, start=58.0, dur=5.0)], total_duration_sec=60.0,
    )
    assert len(out) == 1
    # 60s 영상에 5s cue → max_start = 55
    assert out[0]["start_sec"] == pytest.approx(55.0)
    assert out[0]["end_sec"] == pytest.approx(60.0)


def test_validate_place_decisions_fills_missing_with_original():
    data = {"placements": [
        {"cue_index": 1, "start_sec": 20.0, "rationale": "두번째만"},
    ]}
    planned = [_planned(0, start=5.0, dur=2.0), _planned(1, start=10.0, dur=2.0)]
    out = _validate_place_decisions(data, planned, total_duration_sec=60.0)
    assert len(out) == 2
    assert out[0]["cue_index"] == 0
    assert out[0]["start_sec"] == pytest.approx(5.0)
    assert out[0]["rationale"] == "missing_in_response_fallback_to_original"
    assert out[1]["start_sec"] == pytest.approx(20.0)


def test_validate_place_decisions_preserves_cue_index_order():
    data = {"placements": [
        {"cue_index": 2, "start_sec": 30.0},
        {"cue_index": 0, "start_sec": 5.0},
        {"cue_index": 1, "start_sec": 15.0},
    ]}
    planned = [_planned(0, 1.0, 1.0), _planned(1, 1.0, 1.0), _planned(2, 1.0, 1.0)]
    out = _validate_place_decisions(data, planned, total_duration_sec=60.0)
    assert [p["cue_index"] for p in out] == [0, 1, 2]


def test_validate_place_decisions_handles_non_list_response():
    out = _validate_place_decisions(
        {"placements": "bad"}, [_planned(0)], total_duration_sec=60.0,
    )
    assert len(out) == 1
    assert out[0]["rationale"] == "missing_in_response_fallback_to_original"


# ──────────────────────────────────────────────────────────────
# place_tts_cues_with_video (mocked client)
# ──────────────────────────────────────────────────────────────


def _make_client(response_text: str) -> tuple[GeminiClient, MagicMock]:
    config = GeminiConfig(api_key="test", max_retries=2)
    client = GeminiClient.__new__(GeminiClient)
    client.config = config

    fake_uploaded = SimpleNamespace(
        state=SimpleNamespace(name="ACTIVE"),
        uri="files/test",
        name="files/test",
    )
    files_mock = MagicMock()
    files_mock.upload.return_value = fake_uploaded
    files_mock.get.return_value = fake_uploaded
    files_mock.delete = MagicMock()

    models_mock = MagicMock()
    models_mock.generate_content.return_value = SimpleNamespace(text=response_text)

    fake_client = MagicMock()
    fake_client.files = files_mock
    fake_client.models = models_mock
    client.client = fake_client

    types_mock = MagicMock()
    types_mock.Part = lambda **kw: SimpleNamespace(**kw)
    types_mock.FileData = lambda **kw: SimpleNamespace(**kw)
    types_mock.GenerateContentConfig = lambda **kw: SimpleNamespace(**kw)
    types_mock.ThinkingConfig = lambda **kw: SimpleNamespace(**kw)
    client.types = types_mock
    return client, fake_client


def test_place_returns_dialogue_and_placements_non_srt(tmp_path: Path):
    video = tmp_path / "rough.mp4"
    video.write_bytes(b"")
    payload = {
        "dialogue_segments": [
            {"start_sec": 1.0, "end_sec": 2.0, "speaker": "이수", "text": "안녕"},
        ],
        "placements": [
            {"cue_index": 0, "start_sec": 5.0, "rationale": "분노 직전"},
        ],
    }
    client, fake = _make_client(json.dumps(payload))
    out = client.place_tts_cues_with_video(
        video,
        planned_cues=[_planned(0, start=10.0, dur=3.0, text="선생님 화남")],
        total_duration_sec=60.0,
        character_names=["이수"],
    )
    assert len(out["dialogue_segments"]) == 1
    assert out["dialogue_segments"][0]["speaker"] == "이수"
    assert len(out["placements"]) == 1
    assert out["placements"][0]["start_sec"] == pytest.approx(5.0)
    assert out["placements"][0]["end_sec"] == pytest.approx(8.0)
    # Flash 모델 호출
    call_args = fake.models.generate_content.call_args
    assert call_args.kwargs["model"] == client.config.flash_model_name
    fake.files.delete.assert_called_once()


def test_place_srt_mode_overrides_gemini_dialogue(tmp_path: Path):
    video = tmp_path / "rough.mp4"
    video.write_bytes(b"")
    # Gemini 가 dialogue 를 다르게 줘도 known_dialogue 가 우선
    payload = {
        "dialogue_segments": [
            {"start_sec": 100.0, "end_sec": 200.0, "speaker": "wrong", "text": "잘못된 응답"},
        ],
        "placements": [
            {"cue_index": 0, "start_sec": 12.0, "rationale": "ok"},
        ],
    }
    client, _ = _make_client(json.dumps(payload))
    known = [{"start_sec": 1.0, "end_sec": 2.0, "speaker": "여의주", "text": "정답"}]
    out = client.place_tts_cues_with_video(
        video,
        planned_cues=[_planned(0, start=15.0, dur=2.0)],
        total_duration_sec=60.0,
        known_dialogue_segments=known,
    )
    assert out["dialogue_segments"] == known
    assert out["placements"][0]["start_sec"] == pytest.approx(12.0)


def test_place_missing_video_raises(tmp_path: Path):
    client, _ = _make_client("{}")
    with pytest.raises(FileNotFoundError):
        client.place_tts_cues_with_video(
            tmp_path / "nope.mp4",
            planned_cues=[_planned(0)],
            total_duration_sec=60.0,
        )


def test_place_retries_on_empty_then_raises(tmp_path: Path):
    video = tmp_path / "rough.mp4"
    video.write_bytes(b"")
    client, fake = _make_client("")
    fake.models.generate_content.return_value = SimpleNamespace(text=None)
    with pytest.raises(Exception):
        client.place_tts_cues_with_video(
            video,
            planned_cues=[_planned(0)],
            total_duration_sec=60.0,
        )
    assert fake.models.generate_content.call_count == client.config.max_retries
    fake.files.delete.assert_called_once()


def test_place_strips_markdown_fences(tmp_path: Path):
    video = tmp_path / "rough.mp4"
    video.write_bytes(b"")
    payload = {
        "dialogue_segments": [],
        "placements": [{"cue_index": 0, "start_sec": 4.0, "rationale": "x"}],
    }
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    client, _ = _make_client(fenced)
    out = client.place_tts_cues_with_video(
        video,
        planned_cues=[_planned(0, dur=2.0)],
        total_duration_sec=60.0,
    )
    assert out["placements"][0]["start_sec"] == pytest.approx(4.0)


def test_place_array_response_wrapped_as_placements(tmp_path: Path):
    """모델이 가끔 placements 키 없이 list 만 반환하는 경우."""
    video = tmp_path / "rough.mp4"
    video.write_bytes(b"")
    raw_list = [{"cue_index": 0, "start_sec": 7.0, "rationale": "raw"}]
    client, _ = _make_client(json.dumps(raw_list))
    out = client.place_tts_cues_with_video(
        video,
        planned_cues=[_planned(0, dur=1.0)],
        total_duration_sec=60.0,
        known_dialogue_segments=[],
    )
    assert out["placements"][0]["start_sec"] == pytest.approx(7.0)


# ──────────────────────────────────────────────────────────────
# _validate_dialogue_segments — 기존 유지 (sanity)
# ──────────────────────────────────────────────────────────────


def test_dialogue_validator_still_works():
    data = {
        "segments": [
            {"start_sec": 10.0, "end_sec": 12.0, "speaker": "A", "text": "ok"},
        ]
    }
    out = _validate_dialogue_segments(data, total_duration_sec=20.0)
    assert len(out) == 1
    assert out[0]["text"] == "ok"


# ──────────────────────────────────────────────────────────────
# _validate_silence_intervals (v3)
# ──────────────────────────────────────────────────────────────


def _meta(ci, src_s, src_e):
    return {
        "clip_index": ci,
        "rough_start_sec": 0.0,
        "rough_end_sec": src_e - src_s,
        "source_start_sec": src_s,
        "source_end_sec": src_e,
        "visual_essential": False,
        "description": "",
    }


def test_silence_validator_clamps_to_source_range():
    data = {"silence_cut_intervals": [
        {"clip_index": 0, "source_start_sec": 5.0, "source_end_sec": 25.0, "reason": "out"},
    ]}
    out = _validate_silence_intervals(data, [_meta(0, 10.0, 20.0)])
    assert len(out) == 1
    # 5.0~25.0 → clamp 10.0~20.0
    assert out[0]["source_start_sec"] == pytest.approx(10.0)
    assert out[0]["source_end_sec"] == pytest.approx(20.0)


def test_silence_validator_drops_unknown_clip():
    data = {"silence_cut_intervals": [
        {"clip_index": 5, "source_start_sec": 1.0, "source_end_sec": 2.0, "reason": "x"},
    ]}
    out = _validate_silence_intervals(data, [_meta(0, 0.0, 10.0)])
    assert out == []


def test_silence_validator_merges_overlapping():
    data = {"silence_cut_intervals": [
        {"clip_index": 0, "source_start_sec": 3.0, "source_end_sec": 5.0, "reason": "a"},
        {"clip_index": 0, "source_start_sec": 4.0, "source_end_sec": 7.0, "reason": "b"},
    ]}
    out = _validate_silence_intervals(data, [_meta(0, 0.0, 10.0)])
    assert len(out) == 1
    assert out[0]["source_start_sec"] == pytest.approx(3.0)
    assert out[0]["source_end_sec"] == pytest.approx(7.0)


def test_silence_validator_handles_non_list():
    assert _validate_silence_intervals({"silence_cut_intervals": "bad"}, []) == []
    assert _validate_silence_intervals({}, []) == []
    assert _validate_silence_intervals(None, []) == []


def test_place_returns_silence_intervals(tmp_path: Path):
    video = tmp_path / "rough.mp4"
    video.write_bytes(b"")
    payload = {
        "dialogue_segments": [],
        "silence_cut_intervals": [
            {"clip_index": 0, "source_start_sec": 12.0, "source_end_sec": 13.0, "reason": "발화 없음 1s"}
        ],
        "placements": [
            {"cue_index": 0, "start_sec": 5.0, "rationale": "ok"},
        ],
    }
    client, _ = _make_client(json.dumps(payload))
    out = client.place_tts_cues_with_video(
        video,
        planned_cues=[_planned(0, start=5.0, dur=2.0)],
        total_duration_sec=60.0,
        clip_meta=[_meta(0, 10.0, 30.0)],
    )
    assert "silence_cut_intervals" in out
    assert len(out["silence_cut_intervals"]) == 1
    assert out["silence_cut_intervals"][0]["clip_index"] == 0
