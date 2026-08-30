"""V3-M1 grid 회귀 가드 — 시각 표기·스냅·span 재단·arousal·무음 파서(전부 순수).

핵심 고정: ① cue 묶음 규칙은 stt_elevenlabs 정본 **그 함수**를 태운다(수치 복제
금지) ② 같은 입력 → 바이트까지 같은 grid(결정성) ③ 전사 실패 창은 무성 취급.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.modules.stt_elevenlabs import (
    _CUE_GAP_SEC,
    _CUE_MAX_CHARS,
    _CUE_MAX_DURATION_SEC,
)
from app.v3 import schemas, timegrid
from app.v3.arousal import HOP_SEC, compute_arousal
from app.v3.audio import parse_silence_log
from app.v3.scenecut import parse_showinfo_times


# ── 시각 표기 ───────────────────────────────────────────────────────────────

def test_ts_roundtrip():
    assert schemas.format_ts(0) == "00:00:00.000"
    assert schemas.format_ts(4062.848) == "01:07:42.848"
    assert schemas.parse_ts("01:07:42.848") == pytest.approx(4062.848)
    assert schemas.parse_ts("00:01:30,500") == pytest.approx(90.5)   # 콤마 밀리초 관용
    assert schemas.parse_ts("12:30") == pytest.approx(750.0)          # MM:SS 관용
    assert schemas.parse_ts(42) == 42.0
    with pytest.raises(ValueError):
        schemas.parse_ts("한시간")
    with pytest.raises(ValueError):
        schemas.parse_ts(-1)
    with pytest.raises(ValueError):
        schemas.format_ts(-0.5)


def test_snap_time_tolerance_and_tie():
    grid = [0.0, 10.0, 20.0]
    assert schemas.snap_time(11.3, grid) == (10.0, pytest.approx(1.3))
    s, err = schemas.snap_time(14.9, grid)
    assert s is None and err == pytest.approx(4.9)      # >2s → 스냅 실패
    assert schemas.snap_time(15.0, grid, tolerance=5.0)[0] == 10.0  # 동률은 이른 쪽
    assert schemas.snap_time(1.0, [])[0] is None


# ── span 재단 ───────────────────────────────────────────────────────────────

def _w(t0, t1, text):
    return {"t0": t0, "t1": t1, "text": text, "prob": 0.9}


def test_voiced_spans_use_el_cue_rules():
    """cue 경계 4규칙이 stt_elevenlabs 정본 함수로 집행되는지 — 0.5s 공백 분리."""
    words = [_w(0.0, 0.4, "안녕"), _w(0.5, 0.9, "하세요"),
             _w(2.0, 2.4, "반갑다")]                     # 0.9→2.0 공백 1.1s ≥ 0.5
    spans = timegrid.carve_spans(words, [], [], 10.0)
    voiced = [s for s in spans if s["is_audio"]]
    assert len(voiced) == 2
    assert voiced[0]["text"] == "안녕 하세요"
    assert voiced[0]["time_authority"] == "stt"
    assert (voiced[0]["t_in"], voiced[0]["t_out"]) == (0.0, 0.9)


def test_voiced_spans_sentence_end_split():
    words = [_w(0.0, 0.4, "끝났다."), _w(0.6, 0.9, "다음")]  # 공백 0.2s < 0.5 지만 종결부호
    voiced = [s for s in timegrid.carve_spans(words, [], [], 5.0) if s["is_audio"]]
    assert len(voiced) == 2


def test_voiced_span_max_duration_reused():
    # 6.0s 상한 — 상수를 복제하지 않고 정본과 같은 값으로 동작하는지
    words = [_w(i * 0.7, i * 0.7 + 0.5, f"말{i}") for i in range(12)]  # 총 ~8.2s 연속
    voiced = [s for s in timegrid.carve_spans(words, [], [], 20.0) if s["is_audio"]]
    assert len(voiced) >= 2
    assert all(s["t_out"] - s["t_in"] <= _CUE_MAX_DURATION_SEC + 1e-6 for s in voiced)
    assert (_CUE_GAP_SEC, _CUE_MAX_CHARS, _CUE_MAX_DURATION_SEC) == (0.5, 44, 6.0)


def test_unvoiced_carved_by_scene_cuts():
    words = [_w(0.0, 1.0, "앞"), _w(20.0, 21.0, "뒤")]
    spans = timegrid.carve_spans(words, [5.0, 12.0], [], 21.0)
    unvoiced = [s for s in spans if not s["is_audio"]]
    # 1.0~20.0 구간이 장면 컷 5.0·12.0 에서 재단되고, 6s 넘는 조각(5~12 · 12~20)은
    # 무음 경계 부재 → 등분 폴백으로 더 쪼개진다
    assert [(s["t_in"], s["t_out"]) for s in unvoiced] == [
        (1.0, 5.0), (5.0, 8.5), (8.5, 12.0), (12.0, 16.0), (16.0, 20.0)]
    assert all(s["time_authority"] == "scene" for s in unvoiced)
    assert all(not s["text"] for s in unvoiced)


def test_unvoiced_fallback_silence_then_equal_split():
    # 장면 컷 없음 · 20s 무성 → 무음 경계(8.0)로 1차, 넘치는 조각은 등분
    spans = timegrid.carve_spans([], [], [(7.5, 8.0)], 20.0)
    unvoiced = [s for s in spans if not s["is_audio"]]
    assert unvoiced, "무성 구간이 조각나야 한다"
    assert all(s["t_out"] - s["t_in"] <= timegrid.MAX_UNVOICED_SEC + 1e-6
               for s in unvoiced)
    # 경계 8.0(무음 끝)이 절단점으로 쓰였는지
    assert any(abs(s["t_in"] - 8.0) < 1e-6 or abs(s["t_out"] - 8.0) < 1e-6
               for s in unvoiced)


def test_sliver_gaps_ignored():
    words = [_w(0.0, 1.0, "가"), _w(1.2, 2.0, "나")]     # 틈 0.2s < 0.5 → 무성 span 없음
    spans = timegrid.carve_spans(words, [1.1], [], 2.0)
    assert all(s["is_audio"] for s in spans)


def test_transcribe_failed_window_becomes_scene_spans():
    """전사 실패 창 = 단어 부재 → scene 폴백 무성 재단(발주 원칙)."""
    words = [_w(0.0, 1.0, "시작")]                        # 1.0 이후 전사 없음(실패 창 가정)
    spans = timegrid.carve_spans(words, [4.0, 9.0], [], 12.0)
    unvoiced = [s for s in spans if not s["is_audio"]]
    assert unvoiced and unvoiced[0]["t_in"] == 1.0
    assert unvoiced[-1]["t_out"] == 12.0


def test_span_ids_sequential_and_sorted():
    words = [_w(5.0, 6.0, "중간")]
    spans = timegrid.carve_spans(words, [2.0], [], 10.0)
    assert [s["id"] for s in spans] == [f"sp{i:04d}" for i in range(len(spans))]
    assert spans == sorted(spans, key=lambda s: s["t_in"])


def test_grid_doc_deterministic_and_snap_vocab():
    words = [_w(1.0, 2.0, "말")]
    spans = timegrid.carve_spans(words, [5.0], [], 10.0)
    kw = dict(source={"path": "x.mp4", "duration_sec": 10.0, "fps": 24.0,
                      "width": 1920, "height": 1080},
              words=words, scene_cuts=[5.0], silence=[(3.0, 4.0)],
              arousal=[], span_candidates=spans,
              transcript_meta={"backend": "whisper", "model": "m", "word_count": 1,
                               "failed_windows": [], "srt_provided": False})
    a = json.dumps(timegrid.build_grid_doc(**kw), ensure_ascii=False)
    b = json.dumps(timegrid.build_grid_doc(**kw), ensure_ascii=False)
    assert a == b
    grid = timegrid.build_grid_doc(**kw)
    times = timegrid.grid_snap_times(grid)
    assert 0.0 in times and 10.0 in times and 5.0 in times
    assert times == sorted(times)


# ── arousal ────────────────────────────────────────────────────────────────

def test_arousal_synthetic_deterministic():
    sr = 16000
    t = np.arange(sr * 6) / sr
    pcm = np.zeros(sr * 6, dtype=np.float32)
    pcm[sr * 2:sr * 4] = (0.5 * np.sin(2 * np.pi * 150 * t[sr * 2:sr * 4])).astype(
        np.float32)                                       # 가운데 2초만 150Hz 톤
    words = [{"t0": 2.2, "t1": 2.5, "text": "말"}]
    a1 = compute_arousal(pcm, 6.0, words)
    a2 = compute_arousal(pcm.copy(), 6.0, list(words))
    assert a1 == a2                                       # 결정성
    assert a1[0]["t"] == pytest.approx(0.5) and len(a1) >= 10
    mid = [p for p in a1 if 2.5 <= p["t"] <= 3.5]
    edge = [p for p in a1 if p["t"] <= 1.0]
    assert min(p["energy_db"] for p in mid) > max(p["energy_db"] for p in edge)
    assert all(set(p) == {"t", "energy_db", "pitch_var", "dynamics",
                          "speech_density", "score"} for p in a1)
    assert any(p["speech_density"] > 0 for p in mid)


def test_arousal_hop_grid():
    pcm = np.zeros(16000 * 3, dtype=np.float32)
    a = compute_arousal(pcm, 3.0, [])
    assert [p["t"] for p in a[:3]] == [pytest.approx(0.5), pytest.approx(0.5 + HOP_SEC),
                                       pytest.approx(0.5 + 2 * HOP_SEC)]


# ── 파서(무음·장면) ─────────────────────────────────────────────────────────

def test_parse_silence_log_pairs_and_tail():
    err = ("[silencedetect @ 0x1] silence_start: 1.5\n"
           "[silencedetect @ 0x1] silence_end: 3.25 | silence_duration: 1.75\n"
           "[silencedetect @ 0x1] silence_start: 8.0\n")
    assert parse_silence_log(err, 10.0) == [(1.5, 3.25), (8.0, 10.0)]
    assert parse_silence_log("", 10.0) == []


def test_parse_showinfo_times_dedup_sorted():
    err = ("[Parsed_showinfo] n:0 pts_time:1.2345 x\n"
           "[Parsed_showinfo] n:1 pts_time:1.2345 x\n"
           "[Parsed_showinfo] n:2 pts_time:7.5 x\n")
    assert parse_showinfo_times(err) == [1.234, 7.5]
