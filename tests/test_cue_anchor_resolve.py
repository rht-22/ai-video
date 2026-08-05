"""_resolve_cue_anchors — 앵커 cue(source_time_sec) → 편집 타임라인 절대시간 변환 테스트.

배경 (2026-08-04, 커리어데이_50 실측): cue 가 story 단계 절대시간으로 적혔는데
그 뒤 silence_cut → snap/extend/fill → 길이 클램프가 클립 경계를 세 번 더 바꿔
전 cue 가 7.28초 일찍 터졌다 (docs/tts_cue_anchor_plan.md).
해석은 원본시간 → 편집시간 조각 매핑이라 클립이 확장·분할·이동해도 cue 가
*화면 내용*에 붙어 따라온다.
"""
from __future__ import annotations

from app.pipeline import _resolve_cue_anchors
from app.modules.story_builder import StoryClip


def _clip(start, end, role="build", chunk=0, cand=0):
    return StoryClip(role=role, start_sec=float(start), end_sec=float(end), subtitle="",
                     use_original_audio=True, chunk_index=chunk, candidate_index=cand)


def _anchor(source_time, dur=4.0, chunk=0, cand=0, text="x", **extra):
    d = {
        "source_time_sec": float(source_time), "duration_sec": float(dur),
        "chunk_index": chunk, "candidate_index": cand,
        "text": text, "voice": "ko_male", "speed": "normal",
    }
    d.update(extra)
    return d


# 기준 시나리오: hook 173.2~182.3 / build 274.8~287.1 / payoff 298.5~319.5 (커리어데이_50 story 단계)
HOOK = dict(start=173.2, end=182.3, chunk=0, cand=0)
BUILD = dict(start=274.8, end=287.1, chunk=1, cand=1)
PAYOFF = dict(start=298.5, end=319.5, chunk=2, cand=2)


def _story_clips():
    return [
        _clip(HOOK["start"], HOOK["end"], "hook", HOOK["chunk"], HOOK["cand"]),
        _clip(BUILD["start"], BUILD["end"], "build", BUILD["chunk"], BUILD["cand"]),
        _clip(PAYOFF["start"], PAYOFF["end"], "payoff", PAYOFF["chunk"], PAYOFF["cand"]),
    ]


# ──────────────────────────────────────────────────────────────
# 1) 경계 변경 없음 → offset 그대로
# ──────────────────────────────────────────────────────────────


def test_no_boundary_change_offset_preserved():
    clips = _story_clips()
    # build 시작 0.9초 지점 (원본 275.7)
    out = _resolve_cue_anchors([_anchor(275.7, dur=4.0, chunk=1, cand=1)], clips)
    hook_dur = HOOK["end"] - HOOK["start"]  # 9.1
    assert abs(out[0]["start_sec"] - (hook_dur + 0.9)) < 1e-6
    assert abs(out[0]["end_sec"] - (hook_dur + 0.9 + 4.0)) < 1e-6


# ──────────────────────────────────────────────────────────────
# 2) ★회귀: 첫 클립이 앞으로 7.28초 확장 → cue 는 화면 내용에 붙어 유지
# ──────────────────────────────────────────────────────────────


def test_first_clip_extended_cue_follows_content():
    # 커리어데이_50 실측: _extend_storyline_for_narrative 가 hook 시작을 173.2 → 165.92 로 확장.
    # 구 방식은 cue 절대시간이 고정돼 7.28초 일찍 터졌다. 앵커 해석은 build 소재 위치를 따라간다.
    clips = [
        _clip(165.92, HOOK["end"], "hook", HOOK["chunk"], HOOK["cand"]),  # +7.28s 확장
        _clip(BUILD["start"], BUILD["end"], "build", BUILD["chunk"], BUILD["cand"]),
        _clip(PAYOFF["start"], PAYOFF["end"], "payoff", PAYOFF["chunk"], PAYOFF["cand"]),
    ]
    out = _resolve_cue_anchors([_anchor(275.7, dur=4.0, chunk=1, cand=1)], clips)
    extended_hook_dur = HOOK["end"] - 165.92  # 16.38
    # build 는 편집 타임라인 16.38 에서 시작 → cue 는 16.38 + 0.9 = 17.28 (구 방식은 10.0)
    assert abs(out[0]["start_sec"] - (extended_hook_dur + 0.9)) < 1e-6


# ──────────────────────────────────────────────────────────────
# 3) 무음 컷으로 앞 클립이 줄어듦 → 뒤 클립 cue 가 당겨짐
# ──────────────────────────────────────────────────────────────


def test_earlier_clip_shrunk_pulls_later_cue():
    clips = [
        _clip(HOOK["start"], HOOK["end"] - 3.0, "hook", HOOK["chunk"], HOOK["cand"]),  # 3초 컷
        _clip(BUILD["start"], BUILD["end"], "build", BUILD["chunk"], BUILD["cand"]),
    ]
    out = _resolve_cue_anchors([_anchor(275.7, dur=4.0, chunk=1, cand=1)], clips)
    hook_dur = (HOOK["end"] - 3.0) - HOOK["start"]  # 6.1
    assert abs(out[0]["start_sec"] - (hook_dur + 0.9)) < 1e-6


# ──────────────────────────────────────────────────────────────
# 4) 무음 컷 분할 — cue 소재가 두 번째 kept 조각에 있음
# ──────────────────────────────────────────────────────────────


def test_split_clip_cue_lands_in_second_interval():
    # build(274.8~287.1) 가 [274.8~278.0] + [280.0~287.1] 로 분할 (중간 2초 무음 컷)
    clips = [
        _clip(HOOK["start"], HOOK["end"], "hook", HOOK["chunk"], HOOK["cand"]),
        _clip(274.8, 278.0, "build", BUILD["chunk"], BUILD["cand"]),
        _clip(280.0, 287.1, "build", BUILD["chunk"], BUILD["cand"]),
    ]
    # 원본 281.0 지점 → 두 번째 조각 시작 + 1.0
    out = _resolve_cue_anchors([_anchor(281.0, dur=3.0, chunk=1, cand=1)], clips)
    hook_dur = HOOK["end"] - HOOK["start"]          # 9.1
    first_dur = 278.0 - 274.8                       # 3.2
    assert abs(out[0]["start_sec"] - (hook_dur + first_dur + 1.0)) < 1e-6


def test_cue_in_removed_gap_snaps_to_next_kept():
    # cue 소재(279.0)가 컷된 무음 구간(278.0~280.0) 안 → 같은 앵커 클립의 다음 kept 시작으로 스냅
    clips = [
        _clip(274.8, 278.0, "build", BUILD["chunk"], BUILD["cand"]),
        _clip(280.0, 287.1, "build", BUILD["chunk"], BUILD["cand"]),
    ]
    out = _resolve_cue_anchors([_anchor(279.0, dur=3.0, chunk=1, cand=1)], clips)
    first_dur = 278.0 - 274.8
    assert abs(out[0]["start_sec"] - first_dur) < 1e-6  # 두 번째 조각 시작 = 편집 3.2s


def test_cue_after_all_kept_material_clamps_to_tail():
    # cue 소재(286.0)가 트림으로 빠짐 (클립이 274.8~280.0 만 남음) → 끝 - MIN_CUE_TAIL 로
    clips = [_clip(274.8, 280.0, "build", BUILD["chunk"], BUILD["cand"])]
    out = _resolve_cue_anchors([_anchor(286.0, dur=2.0, chunk=1, cand=1)], clips)
    assert len(out) == 1
    # 원본 279.5 (= 280.0 - 0.5) 지점 → 편집 4.7s
    assert abs(out[0]["start_sec"] - (279.5 - 274.8)) < 1e-6


# ──────────────────────────────────────────────────────────────
# 5) 앵커 클립이 통째로 제거 → 드롭
# ──────────────────────────────────────────────────────────────


def test_anchor_clip_removed_drops_cue():
    clips = [_clip(HOOK["start"], HOOK["end"], "hook", HOOK["chunk"], HOOK["cand"])]
    # build(chunk=1, cand=1) 클립이 최종 타임라인에 없음
    out = _resolve_cue_anchors([_anchor(275.7, dur=4.0, chunk=1, cand=1)], clips)
    assert out == []


# ──────────────────────────────────────────────────────────────
# 6) end 클램프 + 겹침 제거
# ──────────────────────────────────────────────────────────────


def test_end_clamped_to_total_duration():
    clips = [_clip(100.0, 110.0, "hook", 0, 0)]  # 총 10초
    out = _resolve_cue_anchors([_anchor(108.0, dur=5.0, chunk=0, cand=0)], clips)
    assert out[0]["end_sec"] == 10.0  # 8.0 + 5.0 → 10.0 클램프


def test_cue_effectively_outside_dropped():
    clips = [_clip(100.0, 110.0, "hook", 0, 0)]
    # 원본 109.9 → 편집 9.9, 길이 0.1 만 남음 (< 0.3) → 드롭
    out = _resolve_cue_anchors([_anchor(109.9, dur=5.0, chunk=0, cand=0)], clips)
    assert out == []


def test_overlap_shifts_later_cue():
    clips = [_clip(100.0, 130.0, "hook", 0, 0)]  # 총 30초
    out = _resolve_cue_anchors([
        _anchor(101.0, dur=5.0, chunk=0, cand=0, text="a"),   # 편집 1.0~6.0
        _anchor(104.0, dur=4.0, chunk=0, cand=0, text="b"),   # 편집 4.0~8.0 (겹침)
    ], clips)
    assert len(out) == 2
    assert out[1]["start_sec"] >= out[0]["end_sec"]
    assert abs((out[1]["end_sec"] - out[1]["start_sec"]) - 4.0) < 1e-6  # duration 유지


def test_overlap_shift_pushed_outside_dropped():
    clips = [_clip(100.0, 106.0, "hook", 0, 0)]  # 총 6초
    out = _resolve_cue_anchors([
        _anchor(100.5, dur=5.0, chunk=0, cand=0, text="a"),   # 0.5~5.5
        _anchor(101.0, dur=4.0, chunk=0, cand=0, text="b"),   # 밀리면 5.55~6.0 → 0.45 < 0.3? 아님 → 남음
        _anchor(101.2, dur=4.0, chunk=0, cand=0, text="c"),   # 그 뒤로 밀리면 영상 밖 → 드롭
    ], clips)
    texts = [c["text"] for c in out]
    assert "a" in texts and "c" not in texts


# ──────────────────────────────────────────────────────────────
# 7) 구 스키마(절대시간) 통과 경로 — 옛 체크포인트 재개
# ──────────────────────────────────────────────────────────────


def test_legacy_absolute_cue_passthrough_with_clamp():
    clips = [_clip(100.0, 120.0, "hook", 0, 0)]  # 총 20초
    legacy = {"start_sec": 5.0, "end_sec": 25.0, "text": "x", "voice": "ko_male", "speed": "normal"}
    out = _resolve_cue_anchors([legacy], clips)
    assert out[0]["start_sec"] == 5.0
    assert out[0]["end_sec"] == 20.0  # 영상 길이 클램프


def test_legacy_absolute_cue_outside_dropped():
    clips = [_clip(100.0, 110.0, "hook", 0, 0)]  # 총 10초
    legacy = {"start_sec": 12.0, "end_sec": 15.0, "text": "x", "voice": "ko_male", "speed": "normal"}
    assert _resolve_cue_anchors([legacy], clips) == []


# ──────────────────────────────────────────────────────────────
# 8) 견고성
# ──────────────────────────────────────────────────────────────


def test_empty_inputs():
    assert _resolve_cue_anchors([], _story_clips()) == []
    assert _resolve_cue_anchors([_anchor(275.7)], []) == []


def test_resolved_cue_keeps_anchor_fields_for_debugging():
    clips = _story_clips()
    out = _resolve_cue_anchors([_anchor(275.7, dur=4.0, chunk=1, cand=1)], clips)
    assert out[0]["source_time_sec"] == 275.7
    assert out[0]["chunk_index"] == 1


def test_multiple_cues_all_shift_by_same_extension():
    # ★커리어데이_50 종합 재현: hook 확장 −7.28s, cue 3개 전부 내용 기준 유지
    clips = [
        _clip(165.92, HOOK["end"], "hook", HOOK["chunk"], HOOK["cand"]),
        _clip(BUILD["start"], BUILD["end"], "build", BUILD["chunk"], BUILD["cand"]),
        _clip(PAYOFF["start"], PAYOFF["end"], "payoff", PAYOFF["chunk"], PAYOFF["cand"]),
    ]
    cues = [
        _anchor(HOOK["start"] + 0.5, dur=4.5, chunk=HOOK["chunk"], cand=HOOK["cand"], text="c1"),
        _anchor(BUILD["start"] + 0.9, dur=5.0, chunk=BUILD["chunk"], cand=BUILD["cand"], text="c2"),
        _anchor(PAYOFF["start"] + 0.6, dur=5.5, chunk=PAYOFF["chunk"], cand=PAYOFF["cand"], text="c3"),
    ]
    out = _resolve_cue_anchors(cues, clips)
    assert len(out) == 3
    ext = HOOK["start"] - 165.92  # 7.28
    hook_dur = HOOK["end"] - HOOK["start"]      # 9.1
    build_dur = BUILD["end"] - BUILD["start"]   # 12.3
    # hook 안 cue: 확장분만큼 뒤로 (0.5 → 7.78)
    assert abs(out[0]["start_sec"] - (ext + 0.5)) < 1e-6
    # build 안 cue: hook 이 길어진 만큼 뒤로 (9.1+0.9=10.0 → 16.38+0.9=17.28)
    assert abs(out[1]["start_sec"] - (hook_dur + ext + 0.9)) < 1e-6
    # payoff 안 cue 도 동일 시프트
    assert abs(out[2]["start_sec"] - (hook_dur + ext + build_dur + 0.6)) < 1e-6
