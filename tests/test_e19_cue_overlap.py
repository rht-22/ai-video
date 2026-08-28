"""E19-3 내레이션 cue–대사 겹침 검사기 회귀 가드.

발주서: docs/prompts/e19-drama-clip-preset.md §3. 벤치마크의 "끊김 없는 호흡"의 반은
내레이션이 대사와 절대 겹치지 않는 것이다(릴레이). 계약 요점:

- **게이트**: 톤 프로파일 `narration.placement == "dialogue_gaps_only"` 일 때만.
  미지정 채널은 검사 자체가 없다(회귀 0).
- **자리**: 앵커 해석(_resolve_cue_anchors) 직후, resources(비싼 합성) 앞.
- 겹침 > 0.2s(CUE_OVERLAP_TOLERANCE_SEC)면 **가장 가까운 대사 gap 으로 스냅**
  (cue 길이가 들어갈 때만). 들어갈 gap 이 없으면 **옮기지 않고 건별 경고** —
  멀쩡한 내레이션을 지우거나 엉뚱한 자리로 보내는 것이 겹침보다 나쁘다
  (영상 밖 cue 안전망의 규율).
- 순수 — 넘겨받은 cue 를 건드리지 않고 사본을 돌려준다.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.pipeline import CUE_OVERLAP_TOLERANCE_SEC, snap_cues_to_dialogue_gaps

REPO = Path(__file__).resolve().parents[1]


def _seg(s, e):
    return SimpleNamespace(start_sec=s, end_sec=e, text="대사")


def _cue(s, e, text="내레이션"):
    return {"start_sec": s, "end_sec": e, "text": text, "voice": "ko_male"}


# ══════════════════════════════════════════════════════════════════════════
# 판정 — 겹침 없음/허용치 이내는 그대로
# ══════════════════════════════════════════════════════════════════════════
def test_no_overlap_untouched():
    cues = [_cue(5.0, 7.0)]
    out, rep = snap_cues_to_dialogue_gaps(cues, [_seg(0.0, 4.0), _seg(8.0, 12.0)], 20.0)
    assert out[0]["start_sec"] == 5.0 and out[0]["end_sec"] == 7.0
    assert rep == {"of": 1, "cue_snapped": 0, "warned": 0, "details": []}


def test_overlap_within_tolerance_untouched():
    """0.2s 이하 겹침은 정상 — 릴레이는 경계가 맞닿는 문법이라 관용치가 필요하다."""
    cues = [_cue(3.85, 6.0)]                       # 대사(0~4)와 0.15s 겹침
    out, rep = snap_cues_to_dialogue_gaps(cues, [_seg(0.0, 4.0)], 20.0)
    assert out[0]["start_sec"] == 3.85
    assert rep["cue_snapped"] == 0 and rep["warned"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 스냅 — 가장 가까운 들어갈 gap 으로
# ══════════════════════════════════════════════════════════════════════════
def test_overlapping_cue_snaps_to_nearest_gap():
    """대사 위에 얹힌 cue 가 바로 뒤 gap 으로 옮겨진다(릴레이 자리)."""
    cues = [_cue(2.0, 4.0)]                        # 대사(0~5)와 2s 겹침
    out, rep = snap_cues_to_dialogue_gaps(cues, [_seg(0.0, 5.0), _seg(9.0, 12.0)], 20.0)
    assert rep["cue_snapped"] == 1 and rep["warned"] == 0
    assert out[0]["start_sec"] == 5.0              # gap(5~9)의 앞 경계 = 원위치에서 최단
    assert out[0]["end_sec"] == 7.0                # 길이 보존
    assert rep["details"][0]["from_sec"] == 2.0 and rep["details"][0]["to_sec"] == 5.0


def test_snap_prefers_nearest_of_two_gaps():
    cues = [_cue(9.5, 10.5)]                       # 대사(8~13) 안 — 앞 gap(5~8)이 뒤(13~20)보다 가깝다
    out, rep = snap_cues_to_dialogue_gaps(
        cues, [_seg(0.0, 5.0), _seg(8.0, 13.0)], 20.0)
    assert rep["cue_snapped"] == 1
    assert out[0]["start_sec"] == 7.0 and out[0]["end_sec"] == 8.0   # gap 뒤 경계에 붙는다


def test_no_fitting_gap_warns_and_keeps():
    """들어갈 틈이 없으면 옮기지 않는다 — 겹침이 엉뚱한 자리보다 낫다."""
    cues = [_cue(2.0, 6.0)]                        # 길이 4s, gap 은 최대 2s
    out, rep = snap_cues_to_dialogue_gaps(
        cues, [_seg(0.0, 8.0), _seg(10.0, 20.0)], 20.0)
    assert rep["cue_snapped"] == 0 and rep["warned"] == 1
    assert out[0]["start_sec"] == 2.0 and out[0]["end_sec"] == 6.0   # 그대로
    assert rep["details"][0]["to_sec"] is None


def test_second_cue_does_not_land_on_first():
    """스냅이 cue 끼리의 새 겹침을 만들면 안 된다 — 자리 잡은 cue 도 점유물이다."""
    cues = [_cue(1.0, 3.0, "A"), _cue(2.0, 4.0, "B")]     # 둘 다 대사(0~5) 위
    out, rep = snap_cues_to_dialogue_gaps(cues, [_seg(0.0, 5.0)], 30.0)
    assert rep["cue_snapped"] == 2
    a = next(c for c in out if c["text"] == "A")
    b = next(c for c in out if c["text"] == "B")
    assert a["start_sec"] == 5.0 and a["end_sec"] == 7.0
    assert b["start_sec"] >= a["end_sec"]                  # A 뒤로 밀린다
    assert b["end_sec"] - b["start_sec"] == 2.0


def test_result_sorted_and_inputs_not_mutated():
    cues = [_cue(6.0, 7.0, "뒤"), _cue(2.0, 4.0, "앞")]   # "앞"은 대사 위 → 스냅으로 순서 변동 가능
    segs = [_seg(0.0, 5.0)]
    out, _rep = snap_cues_to_dialogue_gaps(cues, segs, 30.0)
    assert [c["start_sec"] for c in out] == sorted(c["start_sec"] for c in out)
    assert cues[1]["start_sec"] == 2.0                     # 원본 무변형(순수)


# ══════════════════════════════════════════════════════════════════════════
# 무해 조건 — 재료가 없으면 아무것도 안 한다
# ══════════════════════════════════════════════════════════════════════════
def test_noop_without_segments_or_cues():
    cues = [_cue(1.0, 2.0)]
    out, rep = snap_cues_to_dialogue_gaps(cues, [], 20.0)
    assert out[0]["start_sec"] == 1.0 and rep["cue_snapped"] == 0
    out2, rep2 = snap_cues_to_dialogue_gaps([], [_seg(0.0, 5.0)], 20.0)
    assert out2 == [] and rep2["of"] == 0
    out3, rep3 = snap_cues_to_dialogue_gaps(cues, [_seg(0.0, 5.0)], 0.0)
    assert out3[0]["start_sec"] == 1.0 and rep3["cue_snapped"] == 0


def test_broken_cue_times_kept_untouched():
    """시간이 깨진 cue 는 판정을 포기하고 그대로 싣는다 — 가드 오작동으로 멀쩡한
    내레이션을 옮기는 것이 겹침보다 나쁘다(영상 밖 cue 안전망과 같은 규율)."""
    cues = [{"text": "깨짐", "start_sec": None, "end_sec": 3.0}]
    out, rep = snap_cues_to_dialogue_gaps(cues, [_seg(0.0, 5.0)], 20.0)
    assert out[0]["text"] == "깨짐" and rep["cue_snapped"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 배선 — 게이트·자리·기록 (E15 방식의 소스 문자열 고정)
# ══════════════════════════════════════════════════════════════════════════
def test_pipeline_wiring_gate_and_order():
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    # 게이트: 톤 프로파일의 placement 가 dialogue_gaps_only 일 때만
    assert 'narration.get("placement") == "dialogue_gaps_only"' in src
    # 자리: 앵커 해석 뒤 · style 블록 앞 (비싼 합성 전)
    gate_at = src.index('narration.get("placement") == "dialogue_gaps_only"')
    assert src.index("[tts cues] 앵커 해석 완료") < gate_at < src.index("[style] AI 연출 구성")
    # 기록: run_log 단계 + 건별 stdout 접두
    assert '"step": "tts_cue_gaps"' in src
    assert "[cue-overlap]" in src


def test_tolerance_constant():
    assert CUE_OVERLAP_TOLERANCE_SEC == 0.2
