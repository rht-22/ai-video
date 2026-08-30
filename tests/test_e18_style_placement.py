"""E18 — AI 연출이 제목을 지우거나 제목 위에 글자를 얹지 못하게 한다 (2026-08-24).

사용자 지시 두 개가 이 파일의 계약이다:

  · "ai가 작업할 때는 제목은 무조건 있어야 해"
  · "제목은 회전하지 않도록 되는지 확인해서 ai가 회전을 못하게 해야돼"
    (회전 차단은 tests/test_e15_style_compose.py 에 있다 — 같은 계약의 다른 절)

실측 근거(2026-08-24, DB) — 제목 창이 실제로 들어간 3편:

    김부장_e37253c2    (IGEOBOGOJA)  창 2개 → 55.31s / 55.31s   제목 없음 0s
    가왕쇼_b5ec784a    (HANIPJUMAK)  창 1개 → 17.21s / 53.17s   제목 없음 36.0s
    혜미리예채파_2b2b46c6 (SHOTCONE)  창 1개 → 18.50s / 51.00s   제목 없음 32.5s

가왕쇼는 SHOTCONE 이 아니다 — 이건 현지화 문제가 아니라 **전 채널 문제**다
(21개 채널 중 20개가 style_compose=true, 나머지 하나도 전역 게이트로 style 을 탄다).

효과 텍스트 쪽 근거: 13:9·꽉 찬 폭·세로 중앙이면 제목 블록은 y 0.146~0.295 인데
종전 프롬프트는 `y 0.15~0.35(위)` 를 권했다 — **프롬프트가 제목 자리를 찍어서
권하고 있었다**(같은 편에서 효과 텍스트 2건이 제목 두 줄 위에 얹혔다).
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DesignConfig  # noqa: E402
from app.modules import style_compose as sc  # noqa: E402


@dataclass
class _Clip:
    start_sec: float
    end_sec: float


# ══════════════════════════════════════════════════════════════════════════
# 제목은 무조건 있어야 한다
# ══════════════════════════════════════════════════════════════════════════
def test_the_real_incident_gap_is_filled():
    """혜미리예채파 2화 — 창 하나가 18.5s 에서 끝나고 51.0s 까지 제목이 없었다.

    ⚠ E21(8/25)에서 **메우는 내용이 바뀌었다** — 기본 제목이 아니라 **직전 제목**이
    잇는다(기본 제목의 아랫줄은 결말 후킹이라 앞 구간에 붙으면 내용이 어긋난다).
    빈 시간이 없다는 계약은 그대로다."""
    segs = [{"text": "AI 제목", "start_sec": 0.0, "end_sec": 18.5}]
    out, notes = sc.fill_title_gaps(segs, "기본 제목", 51.0)
    assert [(s["start_sec"], s["end_sec"]) for s in out] == [(0.0, 51.0)]
    assert out[0]["text"] == "AI 제목"              # 기본 제목이 끼어들지 않는다
    assert any("이어짐" in n for n in notes)        # 조용한 보정 금지


def test_coverage_is_total_for_gaps_at_both_ends_and_in_between():
    """앞머리는 첫 창을 당기고, 사이와 꼬리는 직전 창이 문다 — 0~끝이 빈틈없이 이어진다.

    **다음 문구를 앞당겨 오지 않는다**(E21): 아직 화면에 안 나온 내용이 미리 새면
    안 된다 — 사용자 지적("앞에는 바비큐 안 좋아하는 내용이라")이 이 규칙의 이유다."""
    segs = [{"text": "A", "start_sec": 5.0, "end_sec": 10.0},
            {"text": "B", "start_sec": 20.0, "end_sec": 25.0}]
    out, _ = sc.fill_title_gaps(segs, "기본", 30.0)
    spans = [(s["start_sec"], s["end_sec"]) for s in out]
    assert spans == [(0.0, 20.0), (20.0, 30.0)]
    assert [s["text"] for s in out] == ["A", "B"]


def test_a_fully_covered_plan_is_untouched():
    """김부장 편처럼 이미 끝까지 덮은 플랜은 한 글자도 안 바뀐다."""
    segs = [{"text": "A", "start_sec": 0.0, "end_sec": 30.0},
            {"text": "B", "start_sec": 30.0, "end_sec": 55.31}]
    out, notes = sc.fill_title_gaps([dict(s) for s in segs], "기본", 55.31)
    assert out == segs and notes == []


def test_a_tiny_gap_is_absorbed_without_a_note():
    """0.2초짜리 틈은 직전 제목이 그대로 물고 지나간다 — 메모조차 남기지 않는다
    (MIN_TITLE_GAP_SEC 은 E21 부터 '메모를 남길 만한 틈인가'의 기준이다)."""
    segs = [{"text": "A", "start_sec": 0.0, "end_sec": 10.0},
            {"text": "B", "start_sec": 10.2, "end_sec": 20.0}]
    out, notes = sc.fill_title_gaps(segs, "기본", 20.1)
    assert [(s["start_sec"], s["end_sec"]) for s in out] == [(0.0, 10.2), (10.2, 20.1)]
    assert [s["text"] for s in out] == ["A", "B"]     # 기본 제목이 끼어들지 않는다
    assert notes == []


def test_a_window_past_the_video_is_dropped_and_the_rest_still_covers():
    segs = [{"text": "A", "start_sec": 0.0, "end_sec": 10.0},
            {"text": "밖", "start_sec": 60.0, "end_sec": 70.0}]
    out, notes = sc.fill_title_gaps(segs, "기본", 40.0)
    assert [(s["start_sec"], s["end_sec"]) for s in out] == [(0.0, 40.0)]
    assert out[0]["text"] == "A"
    assert any("밖" in n for n in notes)


def test_an_overrunning_window_end_is_clamped_to_the_video():
    out, _ = sc.fill_title_gaps([{"text": "A", "start_sec": 0.0, "end_sec": 99.0}], "기본", 40.0)
    assert out == [{"text": "A", "start_sec": 0.0, "end_sec": 40.0}]


def test_without_a_base_title_nothing_changes():
    """회귀 0 — 사람 경로(edit_overrides)는 base_title 을 주지 않는다. 사람이 비운 건 의도다."""
    segs = [{"text": "A", "start_sec": 0.0, "end_sec": 5.0}]
    assert sc.fill_title_gaps(segs, "", 40.0) == (segs, [])
    assert sc.fill_title_gaps(segs, "기본", 0.0) == (segs, [])


def test_placement_fills_gaps_when_a_base_title_is_given():
    """배치 함수를 통해서도 같은 계약이 걸린다(파이프라인이 부르는 경로)."""
    clips = [_Clip(100.0, 130.0)]                    # 편집본 0~30s
    plan = [{"text": "AI 제목", "from_anchor": 100.0, "to_anchor": 110.0}]
    segs, _ = sc.title_segments_from_anchors(plan, clips, base_title="기본 제목")
    assert [(s["start_sec"], s["end_sec"]) for s in segs] == [(0.0, 30.0)]
    plain, _ = sc.title_segments_from_anchors(plan, clips)      # base 없으면 종전 그대로
    assert [(s["start_sec"], s["end_sec"]) for s in plain] == [(0.0, 10.0)]


# ══════════════════════════════════════════════════════════════════════════
# 효과 텍스트는 밴드 안에만
# ══════════════════════════════════════════════════════════════════════════
def test_the_band_y_range_excludes_the_title_block():
    """13:9·꽉 찬 폭·세로 중앙 = SHOTCONE 기하. 제목 블록(≈0.146~0.295)이 범위 밖이어야 한다."""
    lo, hi = sc.text_y_range(DesignConfig(aspect_ratio="13:9"))
    assert lo > 0.30 and hi < 0.70               # 밴드 586~1334px → 0.31~0.66
    assert lo > 0.295                            # 제목 블록 아래에서 시작한다


def test_the_band_y_range_also_keeps_clear_of_the_subtitle_zone():
    """1:1 채널은 밴드가 자막 자리(margin_v 430)까지 내려온다 — 거기서 잘라야 한다."""
    lo, hi = sc.text_y_range(DesignConfig(aspect_ratio="1:1"))
    assert hi <= (1920 - sc.SUBTITLE_RESERVE_PX) / 1920 + 1e-9
    assert lo < hi


def test_a_text_in_the_title_zone_is_pulled_into_the_band():
    """드롭이 아니라 클램프다(사용자 결정) — 연출은 살고 위치만 움직인다."""
    lo, hi = sc.text_y_range(DesignConfig(aspect_ratio="13:9"))
    out, notes = sc.clamp_texts_to_band(
        [{"text": "멘붕?!", "y": 0.22, "size": 96}], lo, hi)
    assert out[0]["y"] > 0.30 and out[0]["text"] == "멘붕?!"
    assert len(notes) == 1 and "멘붕?!" in notes[0]


def test_a_text_already_inside_is_untouched():
    lo, hi = sc.text_y_range(DesignConfig(aspect_ratio="13:9"))
    item = {"text": "쿵!", "y": 0.5, "size": 96}
    out, notes = sc.clamp_texts_to_band([item], lo, hi)
    assert out == [item] and notes == []


def test_the_glyph_height_is_kept_inside_not_just_its_centre():
    """y 는 글자 **중심**이라 중심만 넣으면 큰 글자는 위아래로 삐져나온다."""
    lo, hi = sc.text_y_range(DesignConfig(aspect_ratio="13:9"))
    out, _ = sc.clamp_texts_to_band([{"text": "크다", "y": 0.0, "size": 160}], lo, hi)
    assert out[0]["y"] > lo                       # 정확히 lo 가 아니라 반높이만큼 더 아래


def test_a_clamp_never_crashes_on_broken_values():
    """v3 검증기가 먼저 걸러 주지만, 여기서 죽으면 연출 하나로 본편이 멈춘다."""
    bad = [{"text": "x", "y": None, "size": 90}, {"text": "y", "y": 0.5, "size": "??"}]
    out, notes = sc.clamp_texts_to_band(bad, 0.3, 0.6)
    assert out == bad and notes == []
    assert sc.clamp_texts_to_band([], 0.3, 0.6) == ([], [])


def test_a_giant_glyph_lands_in_the_band_centre():
    """글자가 밴드보다 크면 클램프 구간이 뒤집힌다 — 중앙에 놓고 죽지 않는다."""
    out, _ = sc.clamp_texts_to_band([{"text": "거대", "y": 0.05, "size": 400}], 0.45, 0.5)
    assert 0.45 <= out[0]["y"] <= 0.5
