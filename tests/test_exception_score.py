"""V3-M7 — exception 채점기 회귀 가드. 발주서 orders/v3-m7-exception-scorer.md.

두 실사례를 픽스처로 박제한다(발주 합격 기준):
  · 가왕쇼 6화 사고 — Stage 1 이 예고를 50초 늦게 판정(1791 vs 실측 1741.5) →
    fail + miss ≈ 49.5s. 채점기가 이 사고를 영원히 알게 한다.
  · 포핸즈 1회 3층 구조 — credit 카드+오버레이+teaser 를 통짜 teaser 로 판정
    (경계 오차 2.25s) → 정책 3(관용 tol+2.0)·4(aux 중립)로 **pass**.
"""
from __future__ import annotations

import pytest

from app.replay.exception_score import (
    match_job_to_label,
    score_one,
    render_report,
)

# 실측 픽스처 — 레이블(프레임 실측)과 Stage 1 실판정(job 산출 그대로)
GAWANG_LABEL = {
    "source_hint": "가왕쇼_6화.mp4", "duration_sec": 1854.119,
    "labels": {
        "intro": {"start": 5.6, "end": 9.7, "tol": 0.7},
        "recap": None, "credit": None,
        "teaser": {"start": 1741.5, "end": 1836.5, "tol": 1.5},
        "end": {"start": 1836.5, "end": 1854.119, "tol": 1.0},
    },
}
GAWANG_PRED = {  # 2026-08-31 실판정 — 예고를 50초 늦게
    "intro": {"start": "00:00:00.000", "end": "00:00:10.000"},
    "recap": None, "credit": None,
    "teaser": {"start": "00:29:51.000", "end": "00:30:54.100"},
    "end": None,
}

FOURHANDS_LABEL = {
    "source_hint": "1회.mp4", "duration_sec": 4062.848,
    "aux": {"credit_overlay": {"start": 4005.0, "end": 4051.5, "tol": 2.0}},
    "labels": {
        "intro": {"start": 0.0, "end": 43.0, "tol": 1.0},
        "recap": None,
        "credit": {"start": 4027.0, "end": 4029.5, "tol": 1.0},
        "teaser": {"start": 4051.5, "end": 4062.848, "tol": 1.5},
        "end": None,
    },
}
FOURHANDS_PRED = {  # M1 실판정 — 말미를 통짜 teaser 로
    "intro": {"start": "00:00:00.000", "end": "00:00:43.000"},
    "teaser": {"start": "01:07:09.250", "end": "01:07:42.848"},
    "recap": None, "credit": None, "end": None,
}


def test_gawang_incident_is_permanently_failed():
    s = score_one(GAWANG_LABEL, GAWANG_PRED)
    assert s["verdict"] == "fail"
    teaser = next(z for z in s["zones"] if z["zone"] == "teaser")
    assert teaser["pass"] is False
    assert teaser["uncovered_sec"] == pytest.approx(49.5, abs=0.1)   # 1791−1741.5
    assert s["miss_sec"] == pytest.approx(49.5, abs=0.1)             # 오염 위험 총량
    # intro·end 는 관용 안 — intro 판정(0~10)이 레이블(5.6~9.7) 전체를 덮는다
    assert next(z for z in s["zones"] if z["zone"] == "intro")["pass"] is True
    assert next(z for z in s["zones"] if z["zone"] == "end")["pass"] is True


def test_fourhands_three_layer_tolerance_passes():
    s = score_one(FOURHANDS_LABEL, FOURHANDS_PRED)
    assert s["verdict"] == "pass"                                    # 정책 3·4
    credit = next(z for z in s["zones"] if z["zone"] == "credit")
    # 경계 오차 2.25s ≤ 관용 3.0(tol 1.0 + 스냅 2.0)
    assert credit["uncovered_sec"] == pytest.approx(2.25, abs=0.01)
    assert credit["pass"] is True
    # 오버레이 구간(4005~4051.5)은 중립 — overreach 로 계산되지 않는다(정책 4)
    assert s["overreach_sec"] < 1.0
    assert s["aux_neutral_sec"] == pytest.approx(44.0)   # aux − 명시 zone(credit 2.5)


def test_missing_prediction_counts_full_miss():
    s = score_one(GAWANG_LABEL, None)
    assert s["verdict"] == "fail"
    assert s["miss_sec"] == pytest.approx(4.1 + 95.0 + 17.619, abs=0.1)


def test_miss_vs_overreach_asymmetry():
    # overreach(본편을 exception 으로)는 경고일 뿐 fail 이 아니다 — 정책 2
    label = {"labels": {"intro": {"start": 0.0, "end": 10.0, "tol": 1.0}}}
    pred = {"intro": {"start": 0.0, "end": 40.0}}                    # 30s 과잉
    s = score_one(label, pred)
    assert s["verdict"] == "pass"
    assert s["overreach_sec"] == pytest.approx(30.0)
    assert s["overreach_warn"] is True


def test_job_label_matching_by_source_hint():
    labels = {"gw": {"source_hint": "가왕쇼_6화.mp4"}}
    rl = {"input": {"video_path": "/Users/x/Downloads/가왕쇼_6화.mp4"}}
    assert match_job_to_label(rl, labels) == "gw"
    assert match_job_to_label({"input": {"video_path": "/a/딴거.mp4"}}, labels) is None


def test_report_renders_fail_rows():
    rows = [{"name": "gw", **score_one(GAWANG_LABEL, GAWANG_PRED)}]
    md = render_report(rows)
    assert "fail" in md and "teaser ✗" in md
