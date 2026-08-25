"""L-P4 2차 관문 도구의 순수 함수 — `scripts/reframe_ab.py`.

도구 자체가 틀리면 '회귀 0' 이 거짓말이 된다. 판정을 뒤집는 자리만 고정한다."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reframe_ab import (  # noqa: E402
    compare_keyframes, face_track_clips, resolve_job_dir, stacks_differ, verdict,
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


# ── 판정 ──────────────────────────────────────────────────────────────────
def _crops(*flags):
    return [{"name": f"crop_{i}.json", "cmp": {"identical": f}} for i, f in enumerate(flags)]


def test_verdict_passes_when_every_crop_is_identical():
    ok, reasons = verdict(_crops(True, True))
    assert ok and reasons == []


def test_verdict_fails_on_any_different_crop():
    ok, _ = verdict(_crops(True, False))
    assert not ok


def test_verdict_fails_when_nothing_was_compared():
    """0개 대조를 통과로 읽으면 '아무것도 안 한 A/B' 가 초록으로 나온다."""
    ok, reasons = verdict([])
    assert not ok and any("0개" in r for r in reasons)


# ── 후보를 찾는 명령이 파일 경로를 뱉는다 ────────────────────────────────
def test_resolve_job_dir_accepts_a_file_inside_the_job(tmp_path):
    """`grep -l … outputs/*/edit_plan.json` 출력을 그대로 붙여 넣는 것이 자연스럽다."""
    job = tmp_path / "작품_abc123"
    job.mkdir()
    plan = job / "edit_plan.json"
    plan.write_text("{}", encoding="utf-8")
    assert resolve_job_dir(plan) == job


def test_resolve_job_dir_leaves_a_directory_alone(tmp_path):
    job = tmp_path / "작품_abc123"
    job.mkdir()
    assert resolve_job_dir(job) == job


def test_resolve_job_dir_leaves_a_missing_path_alone(tmp_path):
    """없는 경로를 부모로 바꾸면 엉뚱한 곳을 job 으로 읽는다 — 그대로 두고 아래에서 실패시킨다."""
    missing = tmp_path / "없는것"
    assert resolve_job_dir(missing) == missing


# ── 같은 스택 두 판은 A/B 가 아니다 ──────────────────────────────────────
def test_stacks_differ_detects_the_real_axes():
    old = {"cv2": "4.14.0", "numpy": "2.5.1", "python": "3.12.13"}
    new = {"cv2": "4.10.0", "numpy": "2.3.5", "python": "3.12.13"}
    assert stacks_differ(old, new)


def test_stacks_differ_false_when_only_unrelated_fields_move():
    """파이썬 패치 버전이나 deepface 유무로는 A/B 가 성립하지 않는다."""
    a = {"cv2": "4.10.0", "numpy": "2.3.5", "deepface": "0.0.93"}
    b = {"cv2": "4.10.0", "numpy": "2.3.5", "deepface": "없음(ImportError)"}
    assert not stacks_differ(a, b)


def test_stacks_differ_true_when_one_axis_moves():
    assert stacks_differ({"cv2": "4.14.0", "numpy": "2.3.5"},
                         {"cv2": "4.10.0", "numpy": "2.3.5"})


