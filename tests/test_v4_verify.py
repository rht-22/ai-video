"""6c 구간 검증 회귀 가드 — v4 의 시간 방어선(계약 §4 · 운영자 결정 O5).

이 단계가 막는 사고는 실측된 둘이다:
  ① 2026-08-06 샤먼: 미신전 2화 — 후보가 830~887 을 주장했지만 그 대사는 전사의
     518.9~524.2 에 있었다(시간축이 통째로 311초 밀림). 범위 안이라 길이 검사로는
     안 잡히고, 그대로 렌더되면 엉뚱한 장면이 조용히 발행된다.
  ② 2026-08-24 반쪽 쇼츠 5건(4채널 · 18~26.6초 소실) — 소스 밖 구간은 렌더하면
     프레임 0개라 클립이 통째로 사라진다.

그래서 이 파일이 못박는 것은 판정 그 자체가 아니라 **판정의 경계**다:
드롭보다 재배치가 먼저이고, 판정 근거가 없으면(전사 없음 · 짧은 인용) 판정하지 않는다.
과잉 차단은 회차를 말려 버리고, 과소 차단은 엉뚱한 화면을 발행한다.
"""
from __future__ import annotations

import pytest

from app.modules import timestamp_check
from app.v4 import verify as V
from app.v4.verify import (
    QUOTE_FUZZY_MAX_RATIO,
    fuzzy_quote_hits,
    verify_candidate,
)

# 샤먼 실측 그대로 — 대사는 518.9~524.2 에 있다.
SHAMAN_LINE = "딱 봤는데 이거 목매달아 죽은 귀신인데 얘가 장난을 치는구나 라고 봤죠."
LINE_A = "언니가 지금 하는 쏘맥 비율은 코미디 빅리그 녹화 끝나고 먹는 비율이야"
LINE_B = "제작진이 카메라를 정리하는 사이에 우리는 밖으로 나갔어요"

SEGMENTS = [
    {"start_sec": 100.0, "end_sec": 130.0, "text": LINE_A},
    {"start_sec": 200.0, "end_sec": 240.0, "text": LINE_B},
    {"start_sec": 518.9, "end_sec": 524.2, "text": SHAMAN_LINE},
]


def _seg(start, end, quote=None):
    d = {"start_sec": start, "end_sec": end}
    if quote is not None:
        d["quote"] = quote
    return d


def _cand(segments, cid="c01"):
    return {"id": cid, "template": "recap_dialogue", "segments": segments,
            "reason": "사유", "title_draft": "제목 가안"}


def _actions(res, seg_idx):
    return [n["action"] for n in res["notes"] if n["segment"] == seg_idx]


# ── 상수 ────────────────────────────────────────────────────────────────────
def test_constants_match_the_contract():
    """계약 §4 의 값. 관용치는 v1 timestamp_check 와 **같은 자**여야 한다 —
    v1 이 5.0 을 바꾸면 여기서 걸려 두 판정이 갈리는 것을 사람이 먼저 안다."""
    assert V.QUOTE_MATCH_TOLERANCE_SEC == 5.0 == timestamp_check.TOLERANCE_SEC
    assert V.MIN_CANDIDATE_SEC == 40.0
    assert V.MIN_SEGMENT_SEC == 1.0
    # ④의 관용은 격자의 스냅 관용 그대로다(§7 승격된 grid 가 정본 — v3 를 거치지 않는다).
    assert V.SNAP_TOLERANCE_SEC == 2.0


def test_snap_tolerance_comes_from_the_promoted_grid_package():
    """상수를 v4 에 다시 적지 않는다 — 두 벌이 되면 언젠가 한쪽만 고쳐진다."""
    from app.modules.grid import schemas as grid_schemas
    assert V.SNAP_TOLERANCE_SEC is grid_schemas.SNAP_TOLERANCE_SEC


def test_boundary_within_snap_tolerance_raises_no_warning():
    """±2초 안에 눈금이 있으면 경고가 없다 — 경고가 늘 뜨면 아무도 안 본다."""
    cand = _cand([_seg(100.0, 145.0, LINE_A)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0,
                             grid_times=[98.5, 146.9])
    assert [n["action"] for n in res["notes"] if n["action"] == "unsnapped"] == []


# ── 정상 통과 ────────────────────────────────────────────────────────────────
def test_ok_when_quotes_are_where_the_candidate_says():
    cand = _cand([_seg(100.0, 128.0, LINE_A), _seg(205.0, 230.0, LINE_B)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "ok"
    assert res["id"] == "c01"
    assert len(res["segments"]) == 2
    assert res["total_sec"] == pytest.approx(28.0 + 25.0)


def test_quote_inside_tolerance_is_ok_not_dropped():
    """대사가 조각 끝 몇 초 뒤에 있는 것은 경계 오차다 — 관용을 둔 이유가 그것이다.

    ⚠ 이 조각에는 전사 단어가 하나도 없다(무성). ②가 관용 안에서 대사를 확인하면
    ③으로 다시 드롭하지 않는다는 순서 규율을 여기서 못박는다."""
    cand = _cand([_seg(60.0, 98.0, LINE_A), _seg(205.0, 250.0, LINE_B)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "ok"
    assert len(res["segments"]) == 2


def test_segment_without_quote_is_not_judged():
    """quote 가 없는 조각은 비주얼 비트다 — 대조할 근거가 없으므로 판정하지 않는다."""
    cand = _cand([_seg(100.0, 128.0, LINE_A), _seg(900.0, 930.0)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "ok" and len(res["segments"]) == 2
    assert _actions(res, 1) == ["ok"]


# ── ② 시간축 밀림 → 재배치 (샤먼 실측) ───────────────────────────────────────
def test_shifted_segment_is_relocated_keeping_length():
    """실측 그대로: 후보는 830~887 을 주장하고 대사는 518.9 에 있다.

    드롭보다 재배치가 먼저다 — 내용은 맞고 시간축만 밀린 것이 그 사고의 정체였다."""
    cand = _cand([_seg(830.0, 887.0, SHAMAN_LINE)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "relocated"
    seg = res["segments"][0]
    assert seg["start_sec"] == pytest.approx(518.9)
    assert seg["end_sec"] == pytest.approx(518.9 + 57.0)     # 길이 유지
    note = next(n for n in res["notes"] if n["action"] == "relocated")
    assert "518.9" in note["why"] and "차이 311초" in note["why"]
    assert note["from"] == [830.0, 887.0]


def test_relocation_is_declined_when_it_would_overlap_another_segment():
    """재배치가 다른 조각을 밟으면 같은 화면이 두 번 나간다 — 재배치를 포기하고 원위치를
    유지한다(드롭도 아니다 — 사유는 노트에 남는다).

    두 조각이 같은 대사를 주장하고, 뒤 조각의 시간축만 밀린 모양이다."""
    cand = _cand([_seg(100.0, 130.0, LINE_A), _seg(830.0, 880.0, LINE_A)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "ok"
    assert [s["start_sec"] for s in res["segments"]] == [100.0, 830.0]
    declined = [n for n in res["notes"] if n.get("declined_relocation")]
    assert len(declined) == 1
    assert "재배치 포기" in declined[0]["why"] and "겹칩니다" in declined[0]["why"]


def test_relocation_is_declined_when_it_would_invert_the_edit_order():
    """조각 순서는 편집 순서다 — 뒤 조각이 앞 조각보다 앞으로 가면 이야기가 거꾸로 붙는다.

    조각0 은 정상 재배치(→200), 조각1 은 100 으로 가려다 순서가 뒤집혀 포기한다."""
    cand = _cand([_seg(100.0, 145.0, LINE_B), _seg(300.0, 350.0, LINE_A)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "relocated"
    assert [s["start_sec"] for s in res["segments"]] == [200.0, 300.0]
    declined = [n for n in res["notes"] if n.get("declined_relocation")]
    assert len(declined) == 1 and declined[0]["segment"] == 1
    assert "순서가 뒤집" in declined[0]["why"]


def test_relocation_picks_the_occurrence_closest_to_the_original_start():
    """같은 대사가 두 번 나오면 원위치에서 가까운 쪽 — 동점은 이른 쪽(결정성)."""
    segs = [{"start_sec": 100.0, "end_sec": 130.0, "text": LINE_A},
            {"start_sec": 900.0, "end_sec": 930.0, "text": LINE_A}]
    cand = _cand([_seg(820.0, 870.0, LINE_A)])
    res = V.verify_candidate(cand, segments=segs, source_duration_sec=4000.0)
    assert res["segments"][0]["start_sec"] == pytest.approx(900.0)


def test_relocation_that_lands_past_the_source_is_dropped():
    """재배치 뒤에도 소스 밖이면 드롭한다 — 옮겨 놓고 렌더 0프레임이면 의미가 없다."""
    segs = [{"start_sec": 3990.0, "end_sec": 3995.0, "text": LINE_A}]
    cand = _cand([_seg(100.0, 150.0, LINE_A)])
    res = V.verify_candidate(cand, segments=segs, source_duration_sec=3991.0)
    assert res["verdict"] == "dropped"
    assert any("재배치 자리가 소스 밖" in n["why"] for n in res["notes"])


# ── ② 환각 → 드롭 ───────────────────────────────────────────────────────────
def test_hallucinated_quote_is_dropped():
    """전사 어디에도 없는 대사는 환각이다 — 전사가 **있는데** 못 찾은 것이 근거다."""
    # ⚠ 조각1 은 전사(200~240)와 겹친다 — 말이 **있는데** 그 대사가 아닌 경우라야
    #    환각 판정이고, 무성 조각이면 아래 ③ 테스트의 사유가 된다.
    cand = _cand([_seg(100.0, 128.0, LINE_A),
                  _seg(205.0, 245.0, "제가 그때 사실은 회사를 그만두려고 마음먹었습니다")])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "dropped"          # 남은 28초 < 40초
    assert _actions(res, 1) == ["dropped"]
    assert "환각" in next(n["why"] for n in res["notes"] if n["segment"] == 1)


def test_quote_on_a_silent_segment_is_dropped_with_its_own_reason():
    """③ — 대사 없는 조각에 대사를 붙인 것과 환각은 사유가 다르다(둘 다 드롭).
    사유를 가르지 않으면 6단계 재질의가 무엇을 고쳐야 하는지 모른다."""
    cand = _cand([_seg(1000.0, 1045.0, "화면에는 아무 말도 없는 구간의 대사 주장입니다")])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "dropped"
    why = next(n["why"] for n in res["notes"] if n["segment"] == 0)
    assert "전사 단어가 하나도" in why and "환각" not in why


# ── ① 소스 밖 (2026-08-24 반쪽 쇼츠) ────────────────────────────────────────
def test_segment_past_the_source_is_dropped():
    """혜미리예채파 2화 계열 — 시작이 소스 밖이면 그 클립은 통째로 사라진다."""
    cand = _cand([_seg(100.0, 145.0, LINE_A), _seg(200.0, 226.0, LINE_B)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=189.99)
    assert _actions(res, 1) == ["dropped"]
    assert "소스" in next(n["why"] for n in res["notes"] if n["segment"] == 1)


def test_segment_that_only_overruns_the_end_is_clamped():
    """끝만 넘치면 당긴다 — 내용을 더하지 않으므로 안전하고 소재를 버리지 않는다."""
    cand = _cand([_seg(100.0, 145.0, LINE_A), _seg(150.0, 200.0)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=190.0)
    assert res["verdict"] == "ok"
    assert res["segments"][1]["end_sec"] == pytest.approx(190.0)
    assert "clamped" in _actions(res, 1)


def test_unknown_source_duration_is_not_judged():
    """소스 길이를 모르면(0) 범위 판정을 하지 않는다 — 오판 금지."""
    cand = _cand([_seg(100.0, 145.0, LINE_A)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=0.0)
    assert res["verdict"] == "ok" and len(res["segments"]) == 1


# ── 판정 불가는 통과 ─────────────────────────────────────────────────────────
def test_short_quote_is_unjudgeable_not_hallucination():
    """짧은 인용은 우연 일치가 잦아 v1 도 대조하지 않는다(MIN_QUOTE_CHARS=10).
    이걸 환각으로 몰면 감탄사·노래 구간이 통째로 날아간다."""
    cand = _cand([_seg(100.0, 145.0, "네?"), _seg(300.0, 345.0, "박수!")])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "ok" and len(res["segments"]) == 2
    assert all("판정하지 않았습니다" in n["why"] for n in res["notes"]
               if n["segment"] in (0, 1))


def test_no_transcript_skips_quote_checks_but_keeps_bounds_and_grid():
    """전사가 통째로 없으면 ②③ 을 판정하지 않는다. ①④ 는 전사와 무관하므로 그대로 돈다 —
    timestamp_check.filter_candidates 가 bounds 를 전사 조기 반환 **앞**에 둔 그 규율이다."""
    cand = _cand([_seg(100.0, 145.0, SHAMAN_LINE),      # 환각처럼 보이지만 판정 불가
                  _seg(150.0, 200.0, LINE_A)])          # 끝이 소스 밖 → 클램프
    res = V.verify_candidate(cand, segments=[], source_duration_sec=190.0,
                             grid_times=[100.0, 145.0])
    assert res["verdict"] == "ok"
    assert res["segments"][1]["end_sec"] == pytest.approx(190.0)   # ① 살아 있다
    assert "clamped" in _actions(res, 1)
    assert "unsnapped" in _actions(res, 1)                          # ④ 살아 있다
    assert "dropped" not in _actions(res, 0)


def test_transcript_of_only_blank_text_counts_as_no_transcript():
    """글자가 없는 세그먼트만 있는 전사는 '없음'과 같다 — 대조할 재료가 아니다."""
    blank = [{"start_sec": 10.0, "end_sec": 20.0, "text": "  ...  "}]
    cand = _cand([_seg(100.0, 145.0, SHAMAN_LINE)])
    res = V.verify_candidate(cand, segments=blank, source_duration_sec=4000.0)
    assert res["verdict"] == "ok"


# ── ④ 눈금은 경고만 ──────────────────────────────────────────────────────────
def test_unsnapped_boundary_is_a_warning_only():
    """눈금은 스냅의 재료이지 진위의 근거가 아니다 — 어긋나도 드롭하지 않는다."""
    cand = _cand([_seg(100.0, 145.0, LINE_A)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0,
                             grid_times=[0.0, 99.5, 300.0])
    assert res["verdict"] == "ok" and len(res["segments"]) == 1
    warn = [n for n in res["notes"] if n["action"] == "unsnapped"]
    assert len(warn) == 1 and warn[0]["boundary"] == "end"
    assert warn[0]["gap_sec"] == pytest.approx(145.0 - 99.5)


def test_grid_times_with_a_non_number_fails_loudly():
    """눈금은 우리가 만든 격자에서 온다 — 숫자가 아니면 상류 계약 위반이고,
    조용히 건너뛰면 ④ 경고가 통째로 사라진다."""
    with pytest.raises(ValueError, match="grid_times"):
        V.verify_candidate(_cand([_seg(100.0, 145.0)]), segments=SEGMENTS,
                           source_duration_sec=4000.0, grid_times=[0.0, "열"])


# ── 길이 하한 ────────────────────────────────────────────────────────────────
def test_candidate_dropped_when_losses_take_it_under_the_floor():
    """조각을 잃어 40초 미만이 되면 후보 드롭. 사유에 몇 개가 남았는지 함께 적는다."""
    cand = _cand([_seg(100.0, 130.0, LINE_A),
                  _seg(300.0, 360.0, "전사에 없는 문장을 길게 적어 환각으로 판정되게 한다")])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "dropped"
    tail = next(n for n in res["notes"] if n["segment"] == V.CANDIDATE_NOTE_INDEX)
    assert "40" in tail["why"] and "1/2" in tail["why"]


def test_segment_shorter_than_the_floor_is_dropped():
    """1초 미만 조각은 렌더돼도 화면에서 사라진다 — 모델이 그렇게 냈어도 버린다."""
    cand = _cand([_seg(100.0, 145.0, LINE_A), _seg(300.0, 300.4)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert len(res["segments"]) == 1
    assert _actions(res, 1) == ["dropped"]


def test_unreadable_times_are_dropped_not_crashed():
    """시각을 읽을 수 없는 조각은 드롭한다(사유 기록) — NaN 을 렌더까지 보내지 않는다."""
    cand = _cand([_seg(100.0, 145.0, LINE_A), {"start_sec": "가", "end_sec": None},
                  _seg(300.0, 300.0)])
    res = V.verify_candidate(cand, segments=SEGMENTS, source_duration_sec=4000.0)
    assert len(res["segments"]) == 1
    assert _actions(res, 1) == ["dropped"] and _actions(res, 2) == ["dropped"]


def test_candidate_without_segments_is_dropped():
    res = V.verify_candidate(_cand([]), segments=SEGMENTS, source_duration_sec=4000.0)
    assert res["verdict"] == "dropped" and res["segments"] == []
    assert res["total_sec"] == 0.0


# ── 계약 위반은 크게 실패 ────────────────────────────────────────────────────
def test_missing_id_fails_loudly():
    """id 는 checkpoint_candidates.json 의 좌표다 — 없으면 단계끼리 서로를 못 가리킨다."""
    with pytest.raises(ValueError, match="id"):
        V.verify_candidate({"segments": [_seg(100.0, 145.0)]}, segments=SEGMENTS,
                           source_duration_sec=4000.0)


def test_segments_of_wrong_shape_fail_loudly():
    with pytest.raises(ValueError, match="목록이 아닙니다"):
        V.verify_candidate({"id": "c01", "segments": {"start_sec": 1}}, segments=SEGMENTS,
                           source_duration_sec=4000.0)
    with pytest.raises(ValueError, match="dict 가 아닙니다"):
        V.verify_candidate({"id": "c01", "segments": [[100.0, 145.0]]}, segments=SEGMENTS,
                           source_duration_sec=4000.0)


def test_duplicate_ids_fail_loudly():
    cand = _cand([_seg(100.0, 145.0, LINE_A)])
    with pytest.raises(ValueError, match="중복"):
        V.verify_candidates([cand, dict(cand)], segments=SEGMENTS, source_duration_sec=4000.0)


# ── 전량 판정 ────────────────────────────────────────────────────────────────
def test_verify_candidates_records_everything():
    good = _cand([_seg(100.0, 128.0, LINE_A), _seg(205.0, 230.0, LINE_B)], cid="c01")
    shifted = _cand([_seg(830.0, 887.0, SHAMAN_LINE)], cid="c02")
    halluc = _cand([_seg(700.0, 745.0, "전사에 전혀 없는 긴 문장을 여기에 적는다")], cid="c03")

    kept, rec = V.verify_candidates([good, shifted, halluc], segments=SEGMENTS,
                                    source_duration_sec=4000.0)
    assert [c["id"] for c in kept] == ["c01", "c02"]
    assert rec["kept"] == ["c01", "c02"]
    assert [d["id"] for d in rec["dropped"]] == ["c03"]
    assert rec["relocated"] == 1 and rec["clamped"] == 0
    assert [r["verdict"] for r in rec["results"]] == ["ok", "relocated", "dropped"]
    # 살아남은 후보는 **판정 결과**를 싣는다 — 재배치가 하류로 전달되지 않으면 6c 가 한
    # 일이 사라진다.
    assert kept[1]["segments"][0]["start_sec"] == pytest.approx(518.9)


def test_record_counts_clamped_segments():
    """`clamped`·`relocated` 는 **조각 수**다 — 후보 수로 세면 조각 여럿이 잘린 편이
    한 건으로 보인다(조용한 절단 금지의 계량 쪽)."""
    cand = _cand([_seg(100.0, 145.0, LINE_A), _seg(150.0, 250.0)], cid="c05")
    _kept, rec = V.verify_candidates([cand], segments=SEGMENTS, source_duration_sec=190.0)
    assert rec["clamped"] == 1 and rec["relocated"] == 0
    assert rec["kept"] == ["c05"]


def test_all_dropped_returns_empty_kept():
    """전량 드롭이면 kept=[] 를 그대로 돌려준다 — '최소 하나는 살린다'를 여기서 하면
    환각 후보가 그 자리로 올라온다. 재질의 판단은 6단계의 일이다."""
    bad = _cand([_seg(700.0, 745.0, "전사에 전혀 없는 긴 문장을 여기에 적는다")], cid="c09")
    kept, rec = V.verify_candidates([bad], segments=SEGMENTS, source_duration_sec=4000.0)
    assert kept == [] and rec["kept"] == [] and len(rec["dropped"]) == 1
    assert rec["dropped"][0]["why"]


def test_empty_candidate_list_is_fine():
    kept, rec = V.verify_candidates([], segments=SEGMENTS, source_duration_sec=4000.0)
    assert kept == [] and rec["results"] == [] and rec["relocated"] == 0


# ── 순수성·결정성 ────────────────────────────────────────────────────────────
def test_input_candidates_are_never_mutated():
    """순수 — 넘겨받은 dict·조각을 제자리에서 고치지 않는다(재배치가 원본을 밀면
    체크포인트의 원 후보를 되짚을 수 없다)."""
    import copy
    cands = [_cand([_seg(830.0, 887.0, SHAMAN_LINE)], cid="c01"),
             _cand([_seg(150.0, 200.0, LINE_A)], cid="c02")]
    snapshot = copy.deepcopy(cands)
    kept, _rec = V.verify_candidates(cands, segments=SEGMENTS, source_duration_sec=190.0)
    assert cands == snapshot
    assert kept[0]["segments"] is not cands[0]["segments"]


def test_deterministic_for_the_same_input():
    """같은 입력이면 같은 출력 — 6c 는 순수 함수다(재개·A/B 대조의 조건)."""
    cands = [_cand([_seg(830.0, 887.0, SHAMAN_LINE), _seg(100.0, 130.0, LINE_A)], cid="c01"),
             _cand([_seg(150.0, 200.0, LINE_B)], cid="c02")]
    a = V.verify_candidates(cands, segments=SEGMENTS, source_duration_sec=4000.0,
                            grid_times=[0.0, 100.0, 130.0, 518.9])
    b = V.verify_candidates(cands, segments=SEGMENTS, source_duration_sec=4000.0,
                            grid_times=[518.9, 130.0, 100.0, 0.0])   # 정렬은 우리가 한다
    assert a == b


def test_speech_segment_objects_are_accepted_like_dicts():
    """🛑 실행 경로의 전사 세그먼트는 dict 가 아니라 SpeechSegment 객체다.
    dict 전용 가정은 2026-08-06 맥5 에서 생성을 rc=1 로 죽였다 — 두 모양 다 받아야 한다."""
    from app.modules.speech import SpeechSegment
    objs = [SpeechSegment(start_sec=s["start_sec"], end_sec=s["end_sec"], text=s["text"])
            for s in SEGMENTS]
    res = V.verify_candidate(_cand([_seg(830.0, 887.0, SHAMAN_LINE)]),
                             segments=objs, source_duration_sec=4000.0)
    assert res["verdict"] == "relocated"
    assert res["segments"][0]["start_sec"] == pytest.approx(518.9)


# ── 관용 인용 대조 (2026-09-03 적대 검증 critical) ──────────────────────────

_FUZZY_SEGS = [{"start_sec": 120.0, "end_sec": 124.0,
                "text": "이건 정말 대단한 순간이었습니다"},
               {"start_sec": 300.0, "end_sec": 305.0,
                "text": "그때 제가 완전히 무너졌어요 정말로"}]


def _one(quote: str, span=(110.0, 180.0)):
    cand = {"id": "c1", "segments": [{"start_sec": span[0], "end_sec": span[1],
                                      "quote": quote}]}
    return verify_candidate(cand, segments=_FUZZY_SEGS, source_duration_sec=1200.0)


def test_paraphrased_quote_is_not_a_hallucination():
    """🛑 `find_quote_times` 는 앞 max(10, 60%) 글자의 **정확 부분문자열**을 찾는다.
    모델이 전사를 한 글자라도 다듬어 인용하면(조사·군더더기 제거·오탈자 교정) 그
    후보가 '전사 어디에도 없음' = 환각으로 드롭됐다.

    여기는 v4 의 시간 방어선이고 **거짓 드롭이 거짓 통과보다 비싸다** — 통과한 것은
    소스 범위·발화 커버리지(7)·시각 플래그(8)가 다시 보지만, 드롭된 것은 아무도
    되살리지 않는다. 기획서 §3 6c ② 가 정한 임계(편집거리 0.35)를 이제 실제로 건다."""
    got = _one("이건 정말 대단한 순간입니다")          # '이었습' → '입'
    assert got["verdict"] == "ok"
    assert [n["action"] for n in got["notes"]] == ["fuzzy_match"]


def test_real_hallucination_is_still_dropped():
    """관용이 환각까지 통과시키면 6c 가 무의미해진다."""
    got = _one("저는 화성에서 왔고 우주선을 타고 왔습니다")
    assert got["verdict"] == "dropped"


def test_fuzzy_match_also_relocates():
    """다듬은 인용이 **다른 자리**에서 발견되면 그 자리로 옮긴다 — 정확 대조와 같은 규칙."""
    got = _one("그때 제가 완전히 무너졌어요 정말루")     # '정말로' → '정말루'
    assert got["verdict"] == "relocated"
    assert got["segments"][0]["start_sec"] == pytest.approx(300.0, abs=1.0)


def test_fuzzy_does_not_match_a_short_segment_to_a_long_quote():
    """⚠ `timestamp_check._timeline` 이 경고한 착시(0.8초 조각이 30초 인용에 들어맞음)는
    **부분문자열 포함** 판정의 문제다. 여기는 max(len) 으로 나눈 **비율**이라 길이가
    크게 다르면 자동으로 탈락한다 — 같은 함정이 아님을 값으로 고정한다."""
    tiny = [{"start_sec": 10.0, "end_sec": 10.8, "text": "네"},
            {"start_sec": 20.0, "end_sec": 21.0, "text": "그렇죠"}]
    assert fuzzy_quote_hits("이것은 아주 긴 인용문이고 전사에는 없습니다", tiny) == []


def test_fuzzy_skips_short_quotes_like_v1_does():
    """짧은 인용은 우연 일치가 잦아 v1 도 대조하지 않는다 — 관용 경로도 같은 규율."""
    assert fuzzy_quote_hits("네", _FUZZY_SEGS) == []


def test_fuzzy_threshold_is_the_planned_one():
    assert QUOTE_FUZZY_MAX_RATIO == 0.35


def test_speech_span_extraction_is_one_rule():
    """텍스트를 함께 내는 쪽과 안 내는 쪽이 **같은 추출 규칙**을 쓴다(두 벌이면
    언젠가 한쪽만 고쳐진다 — 이 레포가 여러 번 다친 그 방식)."""
    from app.v4.verify import _speech_spans, _speech_spans_texted
    segs = [{"start_sec": 1.0, "end_sec": 2.0, "text": "가나다"},
            {"start_sec": 3.0, "end_sec": 4.0, "text": "   "},      # 빈 텍스트 → 제외
            {"start_sec": None, "end_sec": 6.0, "text": "라마바"}]   # 시각 깨짐 → 제외
    assert _speech_spans(segs) == [(1.0, 2.0)]
    assert [(a, b) for a, b, _t in _speech_spans_texted(segs)] == _speech_spans(segs)
