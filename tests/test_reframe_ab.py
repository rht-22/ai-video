"""L-P4 2차 관문 도구의 순수 함수 — `scripts/reframe_ab.py`.

도구 자체가 틀리면 '회귀 0' 이 거짓말이 된다. 판정을 뒤집는 자리만 고정한다."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reframe_ab import (  # noqa: E402
    compare_embeddings, compare_keyframes, face_track_clips, verdict,
)


def _plan(*modes):
    return {"timeline": [{"role": "hook" if i == 0 else "body",
                          "clip_start_sec": i * 10.0, "clip_end_sec": i * 10.0 + 5.0,
                          "reframe": {"mode": m}} for i, m in enumerate(modes)]}


# ── 얼굴추적 클립만 고른다 ────────────────────────────────────────────────
def test_face_track_clips_keeps_index_of_the_original_timeline():
    """crop 파일 이름이 `{role}_{원본 idx}` 라 center 클립을 건너뛰어도 idx 는 원본 것이어야 한다."""
    got = face_track_clips(_plan("center", "face_track", "face_track"))
    assert [c["idx"] for c in got] == [1, 2]
    assert [c["role"] for c in got] == ["body", "body"]
    assert got[0]["start_sec"] == 10.0 and got[0]["end_sec"] == 15.0


def test_face_track_clips_empty_when_reframe_off():
    assert face_track_clips(_plan("center", "center")) == []
    assert face_track_clips({}) == []


# ── 키프레임 대조 ─────────────────────────────────────────────────────────
def _kf(t, x, y, w=100, h=200):
    return {"time_sec": t, "x_center": x, "y_center": y, "crop_w": w, "crop_h": h}


def test_compare_keyframes_identical():
    a = [_kf(0, 0.5, 0.5), _kf(1, 0.6, 0.4)]
    r = compare_keyframes(a, [dict(k) for k in a])
    assert r["identical"] and r["same_len"]
    assert all(v == 0.0 for v in r["max_delta"].values())


def test_compare_keyframes_reports_worst_field():
    a = [_kf(0, 0.5, 0.5), _kf(1, 0.6, 0.4)]
    b = [_kf(0, 0.5, 0.5), _kf(1, 0.61, 0.4)]
    r = compare_keyframes(a, b)
    assert not r["identical"]
    assert abs(r["max_delta"]["x_center"] - 0.01) < 1e-12
    assert r["max_delta"]["y_center"] == 0.0


def test_compare_keyframes_length_mismatch_is_not_identical():
    """검출 개수가 갈리면 필드 비교는 자리부터 어긋난다 — 길이부터 실패로 본다."""
    r = compare_keyframes([_kf(0, 0.5, 0.5)], [_kf(0, 0.5, 0.5), _kf(1, 0.5, 0.5)])
    assert not r["same_len"] and not r["identical"]
    assert r["n_a"] == 1 and r["n_b"] == 2


# ── 임베딩 대조 ───────────────────────────────────────────────────────────
def test_compare_embeddings_measures_size_not_equality():
    r = compare_embeddings({"a|A": [1.0, 2.0]}, {"a|A": [1.0, 2.0000001]})
    assert r["same_keys"] and r["n"] == 1
    assert 0 < r["max_abs_delta"] < 1e-6


def test_compare_embeddings_different_dimension_is_infinite():
    r = compare_embeddings({"a|A": [1.0, 2.0]}, {"a|A": [1.0]})
    assert r["max_abs_delta"] == float("inf")


# ── 판정 ──────────────────────────────────────────────────────────────────
def _crops(*flags):
    return [{"name": f"crop_{i}.json", "cmp": {"identical": f}} for i, f in enumerate(flags)]


def test_verdict_pass_when_crops_identical_and_embeddings_close():
    ok, reasons = verdict(_crops(True, True),
                          {"same_keys": True, "max_abs_delta": 1e-7}, emb_tol=1e-4)
    assert ok and reasons == []


def test_verdict_skipped_embeddings_pass_but_say_so():
    """캐스트 사진이 없는 job 은 정상이다 — 다만 '안 봤다'가 보여야 한다."""
    ok, reasons = verdict(_crops(True), {"skipped": "캐스트 사진 0장"}, emb_tol=1e-4)
    assert ok and any("임베딩 대조 없음" in r for r in reasons)


def test_verdict_fails_on_any_different_crop():
    ok, _ = verdict(_crops(True, False), {"skipped": "x"}, emb_tol=1e-4)
    assert not ok


def test_verdict_fails_on_embedding_drift_over_tolerance():
    ok, reasons = verdict(_crops(True), {"same_keys": True, "max_abs_delta": 1e-3},
                          emb_tol=1e-4)
    assert not ok and any("임베딩 최대차" in r for r in reasons)


def test_verdict_fails_when_nothing_was_compared():
    """0개 대조를 통과로 읽으면 '아무것도 안 한 A/B' 가 초록으로 나온다."""
    ok, reasons = verdict([], {"skipped": "x"}, emb_tol=1e-4)
    assert not ok and any("0개" in r for r in reasons)
