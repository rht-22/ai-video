"""L-P0 — 현지화 A/B 대조 도구의 순수 로직 고정.

이 도구가 틀리면 이관 전체의 판정이 틀린다. ffmpeg 없이 도는 부분만 고정한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.localize_ab import (  # noqa: E402
    diff_segments, frame_timestamps, json_equal, norm_json, pair_diff, verdict,
)


# ── 정규화 — 의미 없는 차이를 회귀로 잡지 않는다 ────────────────────────
def test_float_noise_is_not_a_regression():
    a = {"start": 1.2340000000001, "text": "가"}
    b = {"start": 1.234, "text": "가"}
    assert json_equal(a, b)


def test_key_order_is_not_a_regression():
    assert json_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})


def test_real_time_shift_is_a_regression():
    assert not json_equal({"start": 1.234}, {"start": 1.240})


def test_norm_json_recurses_into_lists():
    assert norm_json([{"x": 0.10000000001}]) == [{"x": 0.1}]


# ── 자막 줄 단위 차이 ───────────────────────────────────────────────────
def test_diff_segments_reports_count_change():
    out = diff_segments([{"text": "가"}], [{"text": "가"}, {"text": "나"}])
    assert any("줄 수: 1 → 2" in s for s in out)


def test_diff_segments_names_changed_keys():
    out = diff_segments([{"text": "가", "start_sec": 1.0}],
                        [{"text": "가", "start_sec": 2.0}])
    assert out and "start_sec" in out[0]


def test_diff_segments_silent_when_identical():
    segs = [{"text": "가", "start_sec": 1.0}, {"text": "나", "start_sec": 2.0}]
    assert diff_segments(segs, list(segs)) == []


def test_diff_segments_caps_output():
    a = [{"text": f"{i}", "start_sec": i} for i in range(50)]
    b = [{"text": f"{i}", "start_sec": i + 1} for i in range(50)]
    out = diff_segments(a, b, limit=3)
    assert len(out) <= 4 and out[-1].startswith("…")


# ── ko / ja 를 나눠 센다 ────────────────────────────────────────────────
def test_pair_diff_flags_korean_change_first():
    """ko 가 달라지면 번역 이전 단계가 흔들린 것 — 가장 위에 경고로 뜬다."""
    a = [{"ko": "안녕", "ja": "こんにちは"}]
    b = [{"ko": "안녕하세요", "ja": "こんにちは"}]
    out = pair_diff(a, b)
    assert out[0].startswith("⚠ ko 변경 1건")


def test_pair_diff_japanese_only_is_noted_as_llm_noise():
    a = [{"ko": "안녕", "ja": "こんにちは"}]
    b = [{"ko": "안녕", "ja": "どうも"}]
    out = pair_diff(a, b)
    assert out and all(not s.startswith("⚠") for s in out)
    assert any("LLM 비결정성" in s for s in out)


def test_pair_diff_silent_when_identical():
    a = [{"ko": "안녕", "ja": "こんにちは"}]
    assert pair_diff(a, list(a)) == []


# ── 판정 — 번역 차이는 기본 판정에서 빠진다 ─────────────────────────────
def test_translation_diff_alone_is_not_a_regression():
    ok, _ = verdict({"render:shorts.mp4": {"diff": False, "summary": ""},
                     "translation": {"diff": True, "summary": "ja 변경 3건"}})
    assert ok is True


def test_translation_diff_counts_under_strict():
    ok, _ = verdict({"translation": {"diff": True, "summary": "ja 변경 3건"}}, strict=True)
    assert ok is False


def test_render_diff_is_always_a_regression():
    ok, lines = verdict({"render:shorts.mp4": {"diff": True, "summary": "길이 달라짐"}})
    assert ok is False
    assert any(ln.startswith("!! ") for ln in lines)


def test_verdict_marks_translation_distinctly():
    _, lines = verdict({"translation": {"diff": True, "summary": "x"}})
    assert any(ln.startswith("~~ translation") for ln in lines)


# ── 프레임 샘플 시각 ────────────────────────────────────────────────────
def test_frame_timestamps_avoid_both_ends():
    ts = frame_timestamps(60.0, 4)
    assert len(ts) == 4
    assert ts[0] > 0 and ts[-1] < 60.0


def test_frame_timestamps_are_evenly_spaced():
    ts = frame_timestamps(50.0, 4)
    gaps = [round(b - a, 3) for a, b in zip(ts, ts[1:])]
    assert len(set(gaps)) == 1


def test_frame_timestamps_empty_for_zero_duration():
    assert frame_timestamps(0.0) == []
