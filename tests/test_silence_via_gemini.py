"""v3: tts_place 단계의 Gemini 응답 → SilenceCutResult 변환 단위 테스트.

핵심: silence_cutter 의 기존 보호 규칙(visual_essential, action verb)이 후처리에서
강제 적용되는지. Gemini 가 어겨도 무시되어야 한다.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.modules.silence_cutter import _has_action_verb, SilenceCutResult
from app.pipeline import _build_silence_results_from_gemini


def _clip(start, end, *, role="hook", chunk_index=0, candidate_index=0,
          visual_essential=False, subtitle="", use_original_audio=True,
          pacing_note="", character_focus=None, tts_draft=""):
    return SimpleNamespace(
        role=role,
        start_sec=float(start),
        end_sec=float(end),
        subtitle=subtitle,
        use_original_audio=use_original_audio,
        pacing_note=pacing_note,
        chunk_index=chunk_index,
        candidate_index=candidate_index,
        character_focus=character_focus or [],
        visual_essential=visual_essential,
        tts_draft=tts_draft,
    )


def test_visual_essential_no_longer_force_protected():
    """v3.1: visual_essential=True 라도 코드가 컷을 막지 않는다.
    Gemini 가 자르라고 하면 그대로 적용. 보호는 프롬프트 가이드로만 한다."""
    clips = [_clip(10, 30, visual_essential=True)]
    gemini_intervals = [
        {"clip_index": 0, "source_start_sec": 12.0, "source_end_sec": 18.0,
         "reason": "Gemini 가 자르겠다고 판단"},
    ]
    cand_lookup = {(0, 0): {"description": "그냥 평범한 설명"}}
    results = _build_silence_results_from_gemini(clips, gemini_intervals, cand_lookup)
    # 통째 보존이 아니라 실제 컷이 적용됨 — total_removed_sec > 0
    assert results[0].total_removed_sec > 0
    # padding 0.15s 적용된 keep 구조: [10, 12.15], [17.85, 30]
    assert len(results[0].keep_intervals) == 2


def test_action_verb_keyword_no_longer_blocks_cut():
    """v3.1: description 에 키워드("키스") 있어도 코드는 컷을 막지 않는다."""
    clips = [_clip(10, 30, chunk_index=0, candidate_index=0)]
    gemini_intervals = [
        {"clip_index": 0, "source_start_sec": 12.0, "source_end_sec": 18.0,
         "reason": "Gemini 판단"},
    ]
    cand_lookup = {(0, 0): {"description": "두 사람이 키스하는 장면"}}
    results = _build_silence_results_from_gemini(clips, gemini_intervals, cand_lookup)
    # 통째 보존되지 않음 — 키워드 후처리 가드 제거됨
    assert results[0].total_removed_sec > 0
    assert len(results[0].keep_intervals) == 2


def test_normal_clip_cut_applied_with_padding():
    clips = [_clip(10, 30)]
    # 12.0~18.0 cut 요청 → padding 0.15s 적용해서 [12.15, 17.85] 가 잘림 → keep: [10, 12.15], [17.85, 30]
    gemini_intervals = [
        {"clip_index": 0, "source_start_sec": 12.0, "source_end_sec": 18.0,
         "reason": "발화 없음 6s"},
    ]
    cand_lookup = {(0, 0): {"description": "평범한 대화"}}
    results = _build_silence_results_from_gemini(clips, gemini_intervals, cand_lookup)
    keeps = results[0].keep_intervals
    assert len(keeps) == 2
    assert keeps[0].start_sec == pytest.approx(10.0)
    assert keeps[0].end_sec == pytest.approx(12.15)
    assert keeps[1].start_sec == pytest.approx(17.85)
    assert keeps[1].end_sec == pytest.approx(30.0)
    assert results[0].total_removed_sec == pytest.approx(5.7, abs=1e-3)


def test_cut_below_min_gap_dropped():
    # 0.3s 컷 요청 → min_gap=0.4s 미만이라 드롭 → clip 통째 keep
    clips = [_clip(10, 30)]
    gemini_intervals = [
        {"clip_index": 0, "source_start_sec": 12.0, "source_end_sec": 12.3,
         "reason": "짧은 침묵"},
    ]
    results = _build_silence_results_from_gemini(clips, gemini_intervals, {})
    assert results[0].total_removed_sec == 0.0
    assert results[0].keep_intervals[0] == (10.0, 30.0)


def test_cut_outside_clip_range_clamped_or_dropped():
    # clip 범위 밖 요청 → clamp 후 무효면 드롭
    clips = [_clip(10, 30)]
    gemini_intervals = [
        {"clip_index": 0, "source_start_sec": 5.0, "source_end_sec": 8.0,
         "reason": "out of range"},
    ]
    results = _build_silence_results_from_gemini(clips, gemini_intervals, {})
    assert results[0].total_removed_sec == 0.0


def test_multiple_cuts_merged_and_sorted():
    clips = [_clip(0, 60)]
    gemini_intervals = [
        {"clip_index": 0, "source_start_sec": 30.0, "source_end_sec": 35.0, "reason": "second"},
        {"clip_index": 0, "source_start_sec": 10.0, "source_end_sec": 15.0, "reason": "first"},
        # 인접 (32 < 35 + 머지 윈도우)
        {"clip_index": 0, "source_start_sec": 34.5, "source_end_sec": 40.0, "reason": "overlap-merge"},
    ]
    results = _build_silence_results_from_gemini(clips, gemini_intervals, {})
    # 두 개의 별개 컷이 적용되어야 함: ~10~15, ~30~40 (머지됨)
    # padding 0.15 적용 후 keep 의 갯수로 확인 → 3개 keep
    assert len(results[0].keep_intervals) == 3


def test_invalid_clip_index_dropped():
    clips = [_clip(0, 30)]
    gemini_intervals = [
        {"clip_index": 5, "source_start_sec": 10.0, "source_end_sec": 15.0, "reason": "잘못된 인덱스"},
    ]
    results = _build_silence_results_from_gemini(clips, gemini_intervals, {})
    assert results[0].total_removed_sec == 0.0


def test_has_action_verb_keyword_match_legacy_function_still_works():
    """`_has_action_verb` 함수 자체는 silence_cutter.py 에 legacy 로 남아있음.
    v3.1 에서 호출은 안 하지만 함수가 유효한지 확인."""
    assert _has_action_verb("두 사람이 키스하는 장면")
    assert _has_action_verb("주먹을 휘둘렀다")
    assert _has_action_verb("눈물을 흘렸다")
    assert not _has_action_verb("그냥 평범한 대화")
    assert not _has_action_verb("")
    assert not _has_action_verb(None)


def test_clip_meta_passes_visual_essential_and_description_to_gemini():
    """v3.1: visual_essential 와 description 은 코드 가드 대신 clip_meta 로 Gemini 에 전달된다.
    place_tts_cues_with_video 의 prompt 에 그 정보가 들어가는지 확인.
    """
    import json as _json
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.modules.gemini_client import GeminiClient, GeminiConfig

    config = GeminiConfig(api_key="test", max_retries=1)
    client = GeminiClient.__new__(GeminiClient)
    client.config = config

    fake_uploaded = SimpleNamespace(
        state=SimpleNamespace(name="ACTIVE"),
        uri="files/test",
        name="files/test",
    )
    captured_prompt: list[str] = []

    def _capture(model=None, contents=None, config=None, **_):
        # 첫 번째 content 가 prompt 문자열
        if contents and isinstance(contents[0], str):
            captured_prompt.append(contents[0])
        return SimpleNamespace(text=_json.dumps({
            "dialogue_segments": [],
            "silence_cut_intervals": [],
            "placements": [],
        }))

    files_mock = MagicMock()
    files_mock.upload.return_value = fake_uploaded
    files_mock.get.return_value = fake_uploaded
    files_mock.delete = MagicMock()

    models_mock = MagicMock()
    models_mock.generate_content.side_effect = _capture

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

    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.write(b"")
    tmp.close()
    try:
        client.place_tts_cues_with_video(
            Path(tmp.name),
            planned_cues=[{"cue_index": 0, "text": "x", "original_start_sec": 0.0,
                           "fit_actual_sec": 1.0}],
            total_duration_sec=30.0,
            clip_meta=[
                {"clip_index": 0, "rough_start_sec": 0.0, "rough_end_sec": 20.0,
                 "source_start_sec": 10.0, "source_end_sec": 30.0,
                 "visual_essential": True, "description": "키스 장면 — 무언의 교감"},
            ],
        )
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    assert captured_prompt, "Gemini 호출이 일어나지 않았다"
    prompt = captured_prompt[0]
    assert "visual_essential=true" in prompt
    assert "키스 장면" in prompt or "무언의 교감" in prompt
