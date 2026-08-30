"""V3-M6 — 훅 변형 + feedback 조인 회귀 가드(LLM·DB 없이).

계약: 변형은 본편 불변(훅 span 만 교체·본편 span 재사용 반려), variant_id 신원.
조인은 정규화 제목 주 키·±14일 창·최근접, 분모 3종(전체/성숙/matchable) 정직 분해,
집계는 합(누계류)+가중 평균(비율류) — perf_studio_daily 증분 규율.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.v3 import variants as va
from app.v3.feedback import aggregate_video, match_clips, variant_groups
from app.v3.schemas import format_ts

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _dt(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


# ── 훅 변형 ─────────────────────────────────────────────────────────────────

SPAN_INDEX = {
    f"sp{i:04d}": {"t_in": i * 5.0, "t_out": i * 5.0 + 5.0, "is_audio": i % 2 == 0,
                   "importance": 3, "pos": i, "audio_script": [], "scene_script": "",
                   "meaning_content": "m", "mood": ""}
    for i in range(8)
}
STORY = {"title": {"line1": "원제", "line2": "원펀치"},
         "beats": [
             {"number": 0, "role": "hook", "span_ids": ["sp0000"],
              "time": {"start": format_ts(0.0), "end": format_ts(5.0)},
              "narration": "원 훅", "label": None, "muted_span_ids": []},
             {"number": 1, "role": "climax", "span_ids": ["sp0004", "sp0005"],
              "time": {"start": format_ts(20.0), "end": format_ts(30.0)},
              "narration": None, "label": None, "muted_span_ids": []},
         ]}


def test_variant_rejects_body_span_reuse_and_noncontiguous():
    bad1 = {"variants": [{"title": {"line1": "a", "line2": "b"},
                          "hook_span_ids": ["sp0004"]}]}       # climax 소속
    v, p = va.validate_variants_response(bad1, SPAN_INDEX, STORY, 2)
    assert v is None and any("재사용" in x for x in p)
    bad2 = {"variants": [{"title": {"line1": "a", "line2": "b"},
                          "hook_span_ids": ["sp0001", "sp0003"]}]}
    v2, p2 = va.validate_variants_response(bad2, SPAN_INDEX, STORY, 2)
    assert v2 is None and any("연속" in x for x in p2)


def test_apply_variant_keeps_body_and_sets_identity():
    ok = {"variants": [{"title": {"line1": "새 훅 제목", "line2": "새 펀치"},
                        "hook_span_ids": ["sp0002", "sp0003"],
                        "narration": "새 훅이었죠"}]}
    v, p = va.validate_variants_response(ok, SPAN_INDEX, STORY, 2)
    assert p == []
    doc = va.apply_hook_variant(STORY, v[0], SPAN_INDEX, "hook_v1")
    assert doc["variant_id"] == "hook_v1"
    assert doc["title"]["line1"] == "새 훅 제목"
    hook = doc["beats"][0]
    assert hook["span_ids"] == ["sp0002", "sp0003"]            # 훅만 교체
    assert doc["beats"][1]["span_ids"] == ["sp0004", "sp0005"]  # 본편 불변
    assert STORY["beats"][0]["span_ids"] == ["sp0000"]          # 원본 무변형(순수)


# ── 조인 ────────────────────────────────────────────────────────────────────

def _perf(title: str, pub: datetime, content_id: str = "c1", **kw):
    return {"content_id": content_id, "channel_id": "ch", "video_title": title,
            "publish_time": pub.isoformat(), **kw}


def test_match_by_normalized_title_and_nearest_time():
    clips = [{"clip_id": "a", "title": "제목  하나", "published_at": _dt(10)}]
    perf = [_perf("제목 하나", _dt(30), "far"), _perf("제목 하나", _dt(9), "near")]
    m = match_clips(clips, perf, now=NOW)
    assert m["counts"]["matched"] == 1
    assert m["matched"][0]["content_id"] == "near"             # 최근접


def test_match_decomposition_buckets():
    perf = [_perf("있는 제목", _dt(10))]                        # 지평선 = 10일 전
    clips = [
        {"clip_id": "m", "title": "있는 제목", "published_at": _dt(11)},   # 매칭
        {"clip_id": "f", "title": "신작", "published_at": _dt(1)},         # immature
        {"clip_id": "h", "title": "지평선밖", "published_at": _dt(5)},     # beyond
        {"clip_id": "w", "title": "있는 제목", "published_at": _dt(40)},   # 창 밖
        {"clip_id": "x", "title": "없는 제목", "published_at": _dt(20)},   # absent
    ]
    m = match_clips(clips, perf, now=NOW)
    c = m["counts"]
    assert (c["matched"], c["immature"], c["beyond_horizon"],
            c["window_miss"], c["absent"]) == (1, 1, 1, 1, 1)
    assert m["rates"]["all"] == pytest.approx(20.0)
    assert m["rates"]["mature"] == pytest.approx(25.0)         # immature 제외
    assert m["rates"]["matchable"] == pytest.approx(100.0 / 3, abs=0.1)


def test_aggregate_sums_and_weighted_rates():
    rows = [
        {"views": 100, "impressions": 1000, "ctr": 5.0, "kept_rate": 80.0,
         "view_pct": 90.0, "watch_hours": 1.0, "likes": 3, "shares": 1, "comments": 0},
        {"views": 300, "impressions": 1000, "ctr": 7.0, "kept_rate": 60.0,
         "view_pct": 70.0, "watch_hours": 2.0, "likes": 5, "shares": 0, "comments": 2},
    ]
    a = aggregate_video(rows)
    assert a["views"] == 400 and a["watch_hours"] == 3.0 and a["likes"] == 8
    assert a["ctr"] == pytest.approx(6.0)                      # 노출 가중
    assert a["kept_rate"] == pytest.approx(65.0)               # 조회 가중(100:300)
    assert a["view_pct"] == pytest.approx(75.0)


def test_variant_groups_skeleton():
    matched = [{"variant_id": "hook_v1"}, {"variant_id": "hook_v1"},
               {"variant_id": None}, {}]
    g = variant_groups(matched)
    assert set(g) == {"hook_v1"} and len(g["hook_v1"]) == 2
