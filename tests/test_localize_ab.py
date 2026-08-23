"""L-P0 — 현지화 A/B 대조 도구의 순수 로직 고정.

이 도구가 틀리면 이관 전체의 판정이 틀린다. ffmpeg 없이 도는 부분만 고정한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.localize_ab import (  # noqa: E402
    diff_segments, flatten_pairs, frame_timestamps, json_equal, norm_json, pair_diff,
    run_evidence, verdict,
)

# 실제 metadata.json 의 모양 — 리스트가 아니라 dict 다(노드 실측에서 이걸로 죽었다).
REAL_PAIRS = {
    "top_title": {"ko": "원제목", "ja": "元タイトル"},
    "subs": [{"idx": 0, "start": 1.0, "end": 2.0, "ko": "안녕", "ja": "こんにちは"},
             {"idx": 1, "start": 3.0, "end": 4.0, "ko": "잘가", "ja": "またね"}],
    "tts": [{"idx": 0, "start": 0.0, "end": 5.0, "ko": "내레이션", "ja": "ナレ"}],
    "telops": [{"idx": 0, "start": 2.0, "end": 3.0, "ko": "텔롭", "ja": "テロップ"}],
}


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


# ── ko_ja_pairs 는 dict 다 (노드 실측 크래시 회귀 가드) ────────────────
def test_flatten_real_metadata_shape():
    rows = flatten_pairs(REAL_PAIRS)
    assert len(rows) == 5                       # top_title 1 + subs 2 + tts 1 + telops 1
    assert {r["_sec"] for r in rows} == {"top_title", "subs", "tts", "telops"}


def test_pair_diff_does_not_crash_on_dict_shape():
    """종전엔 dict 를 순회해 키(문자열)가 나오고 .get 이 없어 죽었다."""
    assert pair_diff(REAL_PAIRS, REAL_PAIRS) == []


def test_pair_diff_dict_shape_flags_ko_change():
    import copy
    other = copy.deepcopy(REAL_PAIRS)
    other["subs"][1]["ko"] = "다른 원문"
    out = pair_diff(REAL_PAIRS, other)
    assert out[0].startswith("⚠ ko 변경 1건")
    assert any("subs:1" in s for s in out)


def test_pair_diff_dict_shape_counts_ja_as_noise():
    import copy
    other = copy.deepcopy(REAL_PAIRS)
    other["telops"][0]["ja"] = "別テロップ"
    out = pair_diff(REAL_PAIRS, other)
    assert out and all(not s.startswith("⚠") for s in out)


def test_flatten_tolerates_legacy_list_and_junk():
    assert flatten_pairs([{"ko": "a", "ja": "b"}]) == [{"ko": "a", "ja": "b"}]
    assert flatten_pairs(None) == [] and flatten_pairs("x") == []
    assert flatten_pairs({"subs": None, "top_title": None}) == []


# ── 거짓 합격 가드 (노드 실측: 아무것도 안 돌았는데 '회귀 0' 이 찍혔다) ──
def test_no_run_is_not_a_pass():
    """스냅샷 이후 B 가 안 갱신됐으면 대조 자체가 무의미하다."""
    ok, why = run_evidence(newest_mtime=100.0, snapshot_at=200.0)
    assert ok is False and "돌지 않았다" in why


def test_run_after_snapshot_is_evidence():
    ok, why = run_evidence(newest_mtime=300.0, snapshot_at=200.0)
    assert ok is True and why == ""


def test_missing_outputs_is_not_a_pass():
    ok, why = run_evidence(newest_mtime=None, snapshot_at=200.0)
    assert ok is False


def test_legacy_snapshot_without_marker_is_allowed_with_note():
    """마커 없는 옛 스냅샷은 막지 않되, 확인 불가라고 알린다."""
    ok, why = run_evidence(newest_mtime=300.0, snapshot_at=None)
    assert ok is True and why


def test_equal_mtime_is_not_evidence():
    """같은 시각 = 갱신 없음. 경계에서 통과시키면 가드가 무의미해진다."""
    assert run_evidence(newest_mtime=200.0, snapshot_at=200.0)[0] is False


def test_started_but_died_is_not_evidence():
    """🛑 2회차 실측: L0 직후 state.json 이 써지므로 '시작'을 '완주'로 읽으면 안 된다.

    성공 마커(metadata.json)만 본다 — 오케스트레이터가 성공 판정에 쓰는 것과 같은 파일."""
    from scripts.localize_ab import _newest_output_mtime
    import tempfile, os, time as _t
    with tempfile.TemporaryDirectory() as t:
        b = Path(t)
        (b / "localize_ja").mkdir()
        (b / "localize_ja" / "state.json").write_text("{}")      # 시작만 한 흔적
        (b / "subtitle_segments.json").write_text("[]")
        assert _newest_output_mtime(b) is None                   # 마커가 없으면 증거 없음
        assert run_evidence(_newest_output_mtime(b), 1.0)[0] is False
        marker = b / "localize_ja" / "metadata.json"
        marker.write_text("{}")
        os.utime(marker, (_t.time() + 10, _t.time() + 10))
        assert run_evidence(_newest_output_mtime(b), 1.0)[0] is True
