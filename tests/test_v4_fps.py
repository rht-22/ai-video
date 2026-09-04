"""v4 표본 fps 계단 회귀 가드 (2026-09-03).

계약 `docs/v4/M1-interfaces.md` §3 · 기획 `docs/v4/v4-plan.md` §4(운영자 결정 O2).

무엇을 왜 고정하나:
- **계단 값과 경계**(≤40분 4 · ≤60분 3 · ≤90분 2)는 운영자 결정이다. 값이 조용히
  움직이면 후보 편성이 보는 프레임 수가 달라지므로 초 단위로 못박는다.
- **예산선이 v3 와 다르다**: 고정 유보 5만이 아니라 `실측 텍스트 + 30,000` 이다.
  90분 fps 2 가 "전사 텍스트 ≤79k 일 때만" 성립하는 것이 그 직접 귀결이고(기획서 §4
  표), 이 파일이 그 경계를 토큰 1개 단위로 고정한다.
- **두 산식은 쓰는 자리가 다르다**(count 71/32 = 상한 판정 · usage 66/25 = 과금).
  섞이면 상한을 넘긴 요청을 통과로 읽는다 — 비율 0.883 을 값으로 박아 둔다.
- **하한 실패는 크게**: 비싼 인코딩·업로드 앞에서 죽는 것이 계약이라, 메시지가 사람이
  결정할 재료(필요 fps · 하한의 유래 · 하한에서의 최대 길이)를 싣는지도 본다.
- **순수·결정성**: 같은 소재는 늘 같은 fps(quantum 내림), 넘겨받은 계단 표 불변.
"""
import math

import pytest

from app.v4 import fps as F

MIN = 60.0
# 계약 §3 끝의 실측 상한이 기준으로 쓴 예산(= INPUT_LIMIT − 50,000). v3 의 고정 유보와
# 같은 값이라 두 세대의 표를 같은 자로 대조할 수 있다.
MEASURED_BUDGET = 998_576


# ── 상수 — 운영자 결정과 실측값 그 자체 ──────────────────────────────────────
def test_ladder_is_the_operator_decision():
    """계단은 O2 결정이다 — 초 단위로 고정(분 표기가 코드에서 바뀌어도 값은 같아야 한다)."""
    assert F.FPS_LADDER == ((2400.0, 4.0), (3600.0, 3.0), (5400.0, 2.0))


def test_two_formulas_are_kept_apart():
    """count(71/32) 는 상한 판정 · usage(66/25) 는 과금 — 상수가 섞이면 안 된다."""
    assert (F.TOKENS_PER_FRAME, F.TOKENS_PER_SEC_AUDIO) == (71, 32)
    assert (F.USAGE_TOKENS_PER_FRAME, F.USAGE_TOKENS_PER_SEC_AUDIO) == (66, 25)
    assert F.HIGH_FRAME_MULTIPLIER == 4.0
    assert F.INPUT_LIMIT == 1_048_576
    assert F.TEXT_RESERVE_MIN == 30_000
    assert F.FPS_QUANTUM == 0.05


def test_floor_comes_from_snap_tolerance():
    """하한 0.5 는 임의값이 아니라 1 / 스냅 관용(2.0)이다 — 유래를 값으로 묶는다."""
    assert F.FPS_FLOOR == 0.5 == 1.0 / F.SNAP_TOLERANCE_SEC


def test_snap_tolerance_matches_the_grid_module():
    """관용의 정본은 격자 쪽이다. 여기 사본이 갈리면 하한이 조용히 어긋난다.

    §7 승격이 진행 중이라 두 위치를 모두 인정한다(둘 중 하나는 언제나 존재한다)."""
    try:
        from app.modules.grid import SNAP_TOLERANCE_SEC as canonical  # type: ignore
    except Exception:
        from app.v3.schemas import SNAP_TOLERANCE_SEC as canonical
    assert F.SNAP_TOLERANCE_SEC == canonical


def test_proxy_file_fps_is_30_not_v3_10():
    """v4 프록시는 720p/30fps(O1) — v3 의 10fps 벽이 계단 4 를 막지 않는다."""
    assert F.PROXY_FILE_FPS == 30.0
    fps, note = F.resolve_sample_fps(10 * MIN)      # 파일 fps 상한에 걸리지 않는다
    assert fps == 4.0 and note["reason"] == "ladder"


# ── 산식 — API 실측 대조 ────────────────────────────────────────────────────
def test_count_formula_matches_measurement():
    """60초·1fps → 60×71 + 60×32 = 6,180(실측 6,181 · 오차 0의 선형식)."""
    assert F.count_tokens(60, 1.0) == 6_180
    assert F.count_tokens(60, 0.5) == round(60 * (0.5 * 71 + 32)) == 4_050
    assert F.count_tokens(10_800, 0.85) == round(10_800 * (0.85 * 71 + 32))


def test_usage_formula_and_ratio():
    """과금은 count 의 88.3% — 관측된 '실호출이 ~88%'가 정확히 이 상수 비율이다."""
    assert F.usage_tokens(60, 1.0) == 5_460
    assert F.usage_tokens(60, 1.0) / F.count_tokens(60, 1.0) == pytest.approx(0.883, abs=5e-4)


def test_high_resolution_multiplies_only_frames():
    """HIGH 는 프레임당 ×4(71→284 · 66→264) · 오디오 몫은 그대로다."""
    assert F.count_tokens(60, 1.0, high=True) == 60 * (284 + 32)
    assert F.usage_tokens(60, 1.0, high=True) == 60 * (264 + 25)


def test_audio_share_is_fps_independent():
    """fps 를 0 으로 내려도 오디오 몫은 남는다 — '낮춰도 안 줄어든다'를 값으로 고정."""
    assert F.count_tokens(3600, 0.0) == 3600 * 32
    assert F.usage_tokens(3600, 0.0) == 3600 * 25


@pytest.mark.parametrize("fps,minutes", [(4, 52.7), (3, 67.9), (2, 95.6),
                                         (1, 161.6), (0.5, 246.6)])
def test_measured_max_duration_table(fps, minutes):
    """계약 §3 끝의 실측 상한표를 재현한다(예산 998,576 기준)."""
    assert round(F.max_duration_sec(fps, budget=MEASURED_BUDGET) / MIN, 1) == minutes


def test_each_ladder_rung_fits_its_own_limit():
    """계단 4/3/2 는 각자 상한 안에 있다 — 계단 자체가 예산을 넘기면 설계가 깨진다."""
    for limit, fps in F.FPS_LADDER:
        assert F.count_tokens(limit, fps) <= MEASURED_BUDGET


def test_max_duration_rejects_negative_fps():
    with pytest.raises(ValueError):
        F.max_duration_sec(-1.0, budget=MEASURED_BUDGET)


def test_budget_is_text_measured_not_fixed_reserve():
    """v3 의 고정 5만 유보와 다르다 — 실측 텍스트 + 30,000. 음수 텍스트는 0 으로."""
    assert F.budget_tokens(0) == 1_048_576 - 30_000
    assert F.budget_tokens(80_000) == 1_048_576 - 80_000 - 30_000
    assert F.budget_tokens(-5) == F.budget_tokens(0)


# ── 계단 경계 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("minutes,expected", [(1, 4.0), (10, 4.0), (39, 4.0), (40, 4.0),
                                              (41, 3.0), (59, 3.0), (60, 3.0),
                                              (61, 2.0), (89, 2.0), (90, 2.0)])
def test_ladder_values(minutes, expected):
    fps, note = F.resolve_sample_fps(minutes * MIN)
    assert fps == expected
    assert note["reason"] == "ladder"
    assert note["ladder_fps"] == expected


@pytest.mark.parametrize("limit_sec,inside,outside", [(2400.0, 4.0, 3.0),
                                                      (3600.0, 3.0, 2.0)])
def test_boundary_is_inclusive_to_the_second(limit_sec, inside, outside):
    """'상한 이하면 그 fps' — 경계 그 값과 +1초를 함께 고정한다(≤ 인지 < 인지)."""
    assert F.resolve_sample_fps(limit_sec)[0] == inside
    assert F.resolve_sample_fps(limit_sec - 1)[0] == inside
    assert F.resolve_sample_fps(limit_sec + 1)[0] == outside


def test_last_rung_carries_past_90_minutes():
    """마지막 계단(2.0)을 넘는 소재가 **더 높은** fps 를 받지 않는다.

    90분 직후의 예산 상한은 아직 2.2 라, 계단을 안 이어받으면 91분 소재가 90분보다
    높은 fps 를 받는다(길수록 촘촘해지는 역전)."""
    fps, note = F.resolve_sample_fps(91 * MIN)
    assert note["fps_cap"] > 2.0          # 예산은 아직 여유가 있다
    assert fps == 2.0 and note["ladder_fps"] == 2.0


# ── 예산 상한이 계단을 내리는 지점 ─────────────────────────────────────────
def test_90min_fps2_requires_text_under_79k():
    """기획서 §4: 90분 fps 2 는 '전사 텍스트 ≤79k 일 때만' — 토큰 1개 단위로 고정."""
    assert F.resolve_sample_fps(5400, text_tokens=78_976)[0] == 2.0
    fps, note = F.resolve_sample_fps(5400, text_tokens=78_977)
    assert fps == 1.95                       # 기획서의 '넘으면 1.95 로 자동 하향'
    assert note["reason"] == "budget_capped"
    assert note["ladder_fps"] == 2.0         # 무엇이 내려갔는지가 기록에 남는다


def test_text_tokens_shrink_a_short_material_too():
    """계단이 짧은 소재에서도 예산에 진다 — 텍스트가 예산을 다 먹으면 4 를 못 준다."""
    fps, note = F.resolve_sample_fps(40 * MIN, text_tokens=600_000)
    assert fps < 4.0 and note["reason"] == "budget_capped"
    assert F.count_tokens(40 * MIN, fps) <= note["budget_tokens"]


def test_beyond_ladder_is_continuous_and_monotone():
    """90분 초과 구간은 예산 상한이 잇는다 — 단조 비증가 + 항상 예산 안."""
    prev = 4.0
    for minutes in range(90, 250, 5):
        fps, note = F.resolve_sample_fps(minutes * MIN)
        assert fps <= prev + 1e-9, f"{minutes}분에서 fps 가 올라갔다"
        assert F.count_tokens(minutes * MIN, fps) <= note["budget_tokens"]
        assert note["est_count_tokens"] <= note["budget_tokens"]
        prev = fps


def test_three_hour_material_lands_on_the_measured_085():
    """3시간 실물 실측(fps 0.85 실호출 성공 · 1.0 은 400)과 같은 값을 준다."""
    fps, note = F.resolve_sample_fps(3 * 3600)
    assert fps == 0.85
    assert note["reason"] == "budget_capped"
    assert note["est_count_tokens"] < F.INPUT_LIMIT


def test_resolved_fps_is_always_on_the_quantum():
    """결정성 — 값은 늘 0.05 계단 위에 있다(부동소수 잔차로 새는 값 금지)."""
    for minutes in range(90, 250):
        fps = F.resolve_sample_fps(minutes * MIN)[0]
        assert abs(fps / F.FPS_QUANTUM - round(fps / F.FPS_QUANTUM)) < 1e-9


# ── 하한 실패 ──────────────────────────────────────────────────────────────
def test_floor_failure_boundary():
    """251분은 통과·252분은 실패(텍스트 0 기준) — 실패 경계를 값으로 고정한다."""
    assert F.resolve_sample_fps(251 * MIN)[0] == F.FPS_FLOOR
    with pytest.raises(ValueError):
        F.resolve_sample_fps(252 * MIN)


def test_floor_failure_message_carries_the_decision_material():
    """조용히 더 내리지 않고 크게 실패한다 — 사람이 결정할 재료가 메시지에 있어야 한다."""
    with pytest.raises(ValueError) as ei:
        F.resolve_sample_fps(300 * MIN)
    msg = str(ei.value)
    assert "300.0분" in msg                      # 무엇이 들어왔나
    assert "0.5" in msg                          # 하한
    assert "±2s" in msg                          # 하한의 유래(스냅 관용)
    # 하한에서의 최대 길이는 **그 실행의 예산**으로 잰다(텍스트 0 → 251.5분).
    # 계약 §3 끝 표의 246.6분은 예산 998,576(고정 유보 5만) 기준이라 다른 자다.
    assert "251.5분" in msg
    note = ei.value.note                         # 실패도 run_log 에 남을 수 있어야 한다
    assert note["reason"] == "floor_failed"
    assert note["fps"] < F.FPS_FLOOR


def test_text_tokens_alone_can_trigger_the_floor():
    """예산이 텍스트에 다 먹히면 짧은 소재도 실패한다(예산 0 이하 포함)."""
    with pytest.raises(ValueError):
        F.resolve_sample_fps(60 * MIN, text_tokens=1_040_000)
    with pytest.raises(ValueError):
        F.resolve_sample_fps(60 * MIN, text_tokens=2_000_000)   # 예산 음수


# ── 판정하지 않는 경우 · 계단 표 검증 ──────────────────────────────────────
@pytest.mark.parametrize("dur", [0, 0.0, -1, -3600, None])
def test_unknown_duration_is_not_judged(dur):
    """길이를 모르면 판정하지 않고 첫 계단을 준다 — 모르는 것을 틀렸다고 하지 않는다."""
    fps, note = F.resolve_sample_fps(dur)
    assert fps == F.FPS_LADDER[0][1] == 4.0
    assert note["reason"] == "duration_unknown"
    assert note["fps_cap"] is None


@pytest.mark.parametrize("bad", [
    (),                                   # 빈 표 — 고를 값이 없다
    ((3600, 3.0), (2400, 4.0)),           # 길이 상한 역순
    ((2400, 4.0), (3600, 5.0)),           # 긴 소재가 더 높은 fps
    ((2400, 0.0),),                       # 0 fps
    ((2400, -1.0),),                      # 음수 fps
])
def test_broken_ladder_fails_loud(bad):
    """깨진 계단 표는 조용히 엉뚱한 값을 고르므로 즉시 실패."""
    with pytest.raises(ValueError):
        F.resolve_sample_fps(600, ladder=bad)


def test_sample_fps_cannot_exceed_file_fps():
    """없는 프레임은 만들 수 없다 — v3 가 파일 10fps 에 걸던 것과 같은 벽."""
    with pytest.raises(ValueError, match="파일 fps"):
        F.resolve_sample_fps(600, file_fps=3.0)          # 기본 계단(4.0)이 파일을 넘는다
    with pytest.raises(ValueError, match="파일 fps"):
        F.resolve_sample_fps(600, ladder=((3600, 40.0),))
    assert F.resolve_sample_fps(600, file_fps=4.0)[0] == 4.0   # 같으면 통과


def test_custom_ladder_is_honoured_and_not_mutated():
    """계단은 인자로 갈아끼울 수 있다(shadow 실험) · 넘겨받은 표를 건드리지 않는다."""
    ladder = ((10 * MIN, 6.0), (30 * MIN, 2.0))
    snapshot = tuple(ladder)
    assert F.resolve_sample_fps(9 * MIN, ladder=ladder)[0] == 6.0
    assert F.resolve_sample_fps(11 * MIN, ladder=ladder)[0] == 2.0
    assert F.resolve_sample_fps(200 * MIN, ladder=ladder)[0] < 2.0   # 마지막 계단 이어받기
    assert ladder == snapshot


# ── 순수·결정성 ────────────────────────────────────────────────────────────
def test_deterministic_and_note_is_independent():
    """같은 입력이면 같은 출력(값·기록 전량) · 반환 dict 를 고쳐도 다음 호출에 안 샌다."""
    a_fps, a = F.resolve_sample_fps(120 * MIN, text_tokens=40_000)
    b_fps, b = F.resolve_sample_fps(120 * MIN, text_tokens=40_000)
    assert a_fps == b_fps and a == b
    a["fps"] = 99.0
    assert F.resolve_sample_fps(120 * MIN, text_tokens=40_000)[1]["fps"] == b_fps


def test_note_keys_are_the_contract_ones():
    """계약 §3 이 못박은 키 전량 — run_log·테스트가 이 이름으로 읽는다."""
    required = {"duration_sec", "text_tokens", "budget_tokens", "ladder_fps",
                "fps_cap", "fps", "est_count_tokens", "est_usage_tokens", "reason"}
    for fps, note in (F.resolve_sample_fps(30 * MIN),
                      F.resolve_sample_fps(120 * MIN, text_tokens=90_000),
                      F.resolve_sample_fps(0)):
        assert required <= set(note)
        assert note["fps"] == fps
        assert note["reason"] in {"ladder", "budget_capped", "duration_unknown"}


def test_estimates_use_the_resolved_fps():
    """기록의 추정 토큰은 **실제로 쓸 fps** 로 잰 값이다(계단 값이 아니라)."""
    fps, note = F.resolve_sample_fps(120 * MIN, text_tokens=100_000)
    assert note["est_count_tokens"] == F.count_tokens(120 * MIN, fps)
    assert note["est_usage_tokens"] == F.usage_tokens(120 * MIN, fps)
    assert note["est_usage_tokens"] < note["est_count_tokens"]   # 과금 < 상한 판정
    assert not math.isnan(note["fps_cap_exact"])
