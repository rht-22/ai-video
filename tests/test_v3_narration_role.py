"""내레이션의 역할(2026-09-03, 사용자 지적) — 편성표에 인물이 실리고, 전환이 표시되고,
마지막 줄 닫힘·제목 스포는 **모델의 판정**을 코드가 되돌려 보낸다(코드 판정 없음).

계기: 두 집의 딸이 교차되는 편에서 '그 시각 ○○ 집' 류 설명이 없었다 — 편성표에 인물이
아예 없어 모델이 알 길이 없었다. 마지막 내레이션이 "…는데,"로 끝나 편이 안 끝난 느낌,
제목 아랫줄이 클라이맥스 대사를 그대로 인용해 결말을 미리 말했다.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v3_story_flow import GRID, IDX, ROWS_BY, S2, _beats  # noqa: E402

from app.v3 import story as st  # noqa: E402
from app.v3.story_flow import narration as nr  # noqa: E402
from app.v3.story_flow import select as sl  # noqa: E402


def _rows_with_chars():
    rows = {k: dict(v) for k, v in ROWS_BY.items()}
    rows[0]["characters"] = ["A", "B"]
    rows[1]["characters"] = ["C"]
    return rows


def test_beats_block_lists_characters_and_marks_transition():
    idx = {k: dict(v) for k, v in IDX.items()}
    idx["sp0001"]["characters"] = ["A"]
    block = nr.beats_block(_beats(), idx, _rows_with_chars())
    assert "인물: A, B" in block and "인물: C" in block
    assert "대사 sp0001 (A):" in block                      # 화자 표시
    assert "⚠ 인물 전환: 비트 [0] → [1]" in block and "A, B → C" in block


def test_no_transition_marker_when_same_people_or_unknown():
    rows = _rows_with_chars()
    rows[1]["characters"] = ["A", "B"]
    assert "인물 전환" not in nr.beats_block(_beats(), IDX, rows)
    assert "인물 전환" not in nr.beats_block(_beats(), IDX, ROWS_BY)   # 인물 정보 없음 = 종전


def test_span_index_carries_characters():
    s2 = copy.deepcopy(S2)
    m = s2["sequences"][0]["chunks"][0]["meanings"][0]
    m["characters"] = ["A", "B"]
    m["spans"][0]["characters"] = ["A"]
    idx, _ = st.build_span_index(s2, GRID)
    sid = m["spans"][0]["span_id"]
    assert idx[sid]["characters"] == ["A"] and idx[sid]["meaning_characters"] == ["A", "B"]


def test_last_narration_closed_false_is_rejected_open_or_true_passes():
    base = [{"before_beat": 0, "text": "밤늦게 돌아왔는데,", "closed": False}]
    # 마지막이 after_last 이고 모델이 '안 닫힘'이라 판정 → 반려
    resp = {"narrations": base + [{"after_last": True, "text": "발버둥치는데,", "closed": False}]}
    obj, pr, _ = nr.validate_narrations(resp, 2)
    assert obj is None and any("마지막 내레이션" in p for p in pr)
    # 닫혔다고 판정 → 통과
    resp = {"narrations": base + [{"after_last": True, "text": "파국이 시작됐죠.", "closed": True}]}
    obj, pr, _ = nr.validate_narrations(resp, 2)
    assert obj is not None and pr == []
    # 칸이 없으면(구 응답) 종전과 같이 통과 — 코드가 어미를 판정하지 않는다
    resp = {"narrations": [{"before_beat": 0, "text": "…는데,"}]}
    obj, pr, _ = nr.validate_narrations(resp, 1)
    assert obj is not None


def test_title_self_review_spoiler_is_bounced_back():
    scenes = [{"meaning": "m000", "purpose": "배경"}, {"meaning": "m001", "purpose": "결과"}]
    ok = {"scenes": scenes, "title": {"line1": "상황", "line2": "후킹"},
          "title_review": {"line2_reveals_ending": False}}
    obj, pr, _ = sl.validate_scenes(ok, list(ROWS_BY.values()))
    assert obj is not None and pr == []
    bad = dict(ok, title_review={"line2_reveals_ending": True})
    obj, pr, _ = sl.validate_scenes(bad, list(ROWS_BY.values()))
    assert obj is None and any("결말을 말한다" in p for p in pr)
    # 칸 없음(구 응답) = 종전 통과
    obj, pr, _ = sl.validate_scenes({"scenes": scenes, "title": ok["title"]}, list(ROWS_BY.values()))
    assert obj is not None


def test_scene_rhythm_numbers_and_block():
    grid = {"words": [{"t0": 1.0, "t1": 1.4, "text": "가나"}, {"t0": 2.0, "t1": 2.5, "text": "다라마"},
                      {"t0": 50.0, "t1": 50.5, "text": "밖"}],
            "scene_cuts": [0.5, 3.5, 6.0, 9.5]}
    rows = {0: {"t0": 0.0, "t1": 10.0}}
    r = nr.scene_rhythm(rows, grid)
    assert r["words"] == 2 and r["speech_cps"] == 0.5          # 5자 / 10초 · 범위 밖 단어 제외
    assert r["cut_gap_med"] == 3.0 and r["longest_silence"] == 7.5   # 2.5→10.0
    block = nr.rhythm_block(r)
    assert "발화 밀도 0.5자/초" in block and "최장 무발화 7.5초" in block
    assert nr.rhythm_block(None) == "" and nr.scene_rhythm({}, grid) is None


def test_cap_head_gap_trims_silent_head_only():
    """덮개 뒤 머리 무발화 6.5초 → 무성 조각 제거 + 첫 조각 안 트림으로 1.5초. 유성은 불변."""
    from app.v3.story_flow import cover as cv
    idx = {"s0": {"t_in": 0.0, "t_out": 3.0, "is_audio": False},
           "s1": {"t_in": 3.0, "t_out": 6.5, "is_audio": False},
           "s2": {"t_in": 6.5, "t_out": 9.0, "is_audio": True},
           "s3": {"t_in": 9.0, "t_out": 10.0, "is_audio": True}}
    b = {"span_ids": ["s0", "s1", "s2", "s3"], "lines": ["s2", "s3"], "visual": ["s0", "s1"]}
    rec = cv.cap_head_gap(b, idx, cap=1.5)
    assert rec["before_sec"] == 6.5 and rec["after_sec"] == 1.5
    assert rec["removed_span_ids"] == ["s0"] and rec["head_trim_sec"] == 5.0
    assert b["span_ids"] == ["s1", "s2", "s3"] and b["visual"] == ["s1"] and b["head_trim_sec"] == 5.0
    # 이미 짧으면 무변경 · 유성으로 시작하면 무변경
    assert cv.cap_head_gap({"span_ids": ["s2", "s3"]}, idx) is None
    short = {"span_ids": ["s1", "s2"]}
    assert cv.cap_head_gap(short, idx, cap=4.0) is None and short["span_ids"] == ["s1", "s2"]


def test_watch_trim_prompt_asks_pacing_and_keeps_cuts_schema():
    from app.v3 import watch_trim as wt
    assert '"pacing"' in wt.PROMPT and '"cuts"' in wt.PROMPT
    assert "되돌리지는 않는다" in wt.PROMPT          # 기록만 — 자동 되돌리기 아님
