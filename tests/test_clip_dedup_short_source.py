"""같은 원본 구간이 두 번 들어간 storyline 복구 + 짧은 소재 최소 길이 완화.

배경(2026-08-12 스트릿 레스토랑 파이터 EP1, https://www.youtube.com/watch?v=cYfKHWfNNqs):
66s 소스에서 candidate 가 2개(32~46 · 46~60)만 나왔는데 compose_story 가 3슬롯
(hook/build/payoff)을 채우려고 cand0 을 hook 과 build 에 그대로 두 번 배치했다.
_validate_storyline_timeline 은 hook↔payoff IoU 만 보므로 통과했고, context_extension 이
둘을 똑같이 32~50s 로 늘려 완성본에서 같은 화면이 두 번(38~50s 는 세 번) 재생됐다.
"""
from __future__ import annotations

from app.pipeline import (
    _candidate_coverage_sec,
    _dedup_storyline_clips,
    _remap_cue_clip_indexes,
)
from app.modules.story_builder import StoryClip


def _clip(role, start, end, cand=0):
    return StoryClip(
        role=role, start_sec=float(start), end_sec=float(end), subtitle="s",
        use_original_audio=True, pacing_note="", chunk_index=0, candidate_index=cand,
        character_focus=("셰프A",), visual_essential=True, tts_draft="t",
    )


# 실제 사고 재현 데이터 — edit_plan.json 타임라인 그대로
_INCIDENT_CLIPS = [
    _clip("hook", 32.0, 50.0, cand=0),
    _clip("build", 32.0, 50.0, cand=0),   # hook 과 완전히 동일
    _clip("payoff", 38.0, 60.0, cand=1),  # 38~50 이 앞의 둘과 겹침
]


def test_incident_timeline_is_repaired_to_distinct_ranges():
    out, index_map, msg = _dedup_storyline_clips(_INCIDENT_CLIPS)

    # hook 은 그대로, build 는 통째 중복이라 제거, payoff 는 앞이 잘려 reveal 만 남는다
    assert [(c.role, c.start_sec, c.end_sec) for c in out] == [
        ("hook", 32.0, 50.0),
        ("payoff", 50.0, 60.0),
    ]
    assert index_map == {0: 0, 2: 1}  # build(1) 은 사라지고 payoff 가 1번으로 당겨짐
    assert "build" in msg

    # 어떤 두 clip 도 원본 시간이 겹치지 않는다
    for i in range(len(out)):
        for j in range(i + 1, len(out)):
            a, b = out[i], out[j]
            assert not (a.end_sec > b.start_sec and b.end_sec > a.start_sec)


def test_distinct_clips_pass_through_unchanged():
    clips = [_clip("hook", 10.0, 25.0), _clip("payoff", 40.0, 60.0, cand=1)]
    out, index_map, msg = _dedup_storyline_clips(clips)
    assert out == clips
    assert index_map == {0: 0, 1: 1}
    assert msg == ""


def test_tiny_leftover_clip_is_dropped():
    # 잔여 1.0s(< min_keep_sec 1.5s)면 남기지 않는다 — 한 프레임짜리 잔상 방지
    clips = [_clip("hook", 30.0, 50.0), _clip("build", 49.0, 50.0)]
    out, index_map, _ = _dedup_storyline_clips(clips)
    assert len(out) == 1
    assert index_map == {0: 0}


def test_cue_indexes_follow_dropped_clip():
    # 사고 당시 cue 는 clip_index 0(hook) 과 2(payoff) 를 가리켰다
    cues = [{"clip_index": 0, "text": "a"}, {"clip_index": 2, "text": "b"}]
    _, index_map, _ = _dedup_storyline_clips(_INCIDENT_CLIPS)
    out = _remap_cue_clip_indexes(cues, index_map)
    assert [c["clip_index"] for c in out] == [0, 1]
    assert [c["text"] for c in out] == ["a", "b"]


def test_cue_pointing_at_removed_clip_is_dropped():
    cues = [{"clip_index": 1, "text": "build 내레이션"}]
    _, index_map, _ = _dedup_storyline_clips(_INCIDENT_CLIPS)
    assert _remap_cue_clip_indexes(cues, index_map) == []


def test_coverage_of_incident_candidates_is_below_min_duration():
    # 사고 소재의 후보 2개 — context_extension 까지 합쳐도 32~60s(28s)뿐이라
    # 40s 를 채우려면 같은 구간을 다시 쓰는 수밖에 없다. 완화 트리거가 이 값이다.
    candidates = [
        {"start_sec": 32.0, "end_sec": 46.0,
         "context_extension": {"needed": True, "extended_start_sec": 32.0, "extended_end_sec": 50.0}},
        {"start_sec": 46.0, "end_sec": 60.0,
         "context_extension": {"needed": True, "extended_start_sec": 38.0, "extended_end_sec": 60.0}},
    ]
    assert _candidate_coverage_sec(candidates) == 28.0


def test_coverage_unions_disjoint_and_ignores_broken_rows():
    candidates = [
        {"start_sec": 0.0, "end_sec": 30.0},
        {"start_sec": 100.0, "end_sec": 130.0},
        {"start_sec": 10.0, "end_sec": 20.0},   # 첫 구간에 포함 → 더해지지 않음
        {"start_sec": "x", "end_sec": None},    # 깨진 행은 무시
        {"start_sec": 50.0, "end_sec": 50.0},   # 길이 0
    ]
    assert _candidate_coverage_sec(candidates) == 60.0
    assert _candidate_coverage_sec([]) == 0.0


# ── 선공개형 훅(케이스 3) — 의도된 반복은 보존 ──────────────────────────────
# hook_preview 로 페이오프의 핵심 몇 초를 앞에 보여주고, 뒤에서 같은 장면을
# 온전히 다시 재생하는 편집 문법. gemini_client 스토리 프롬프트가 명시적으로
# 지시하는 정식 기능이므로 dedup 이 이 반복을 잘라내면 안 된다.

def test_preview_hook_pattern_is_preserved():
    # 훅: 페이오프의 제일 웃긴 2초 맛보기 (100~102) / 빌드: 상황 설명 (50~70)
    # 페이오프: 그 장면 전체 (90~110) — 100~102 는 의도적으로 두 번 나온다
    clips = [
        _clip("hook", 100.0, 102.0, cand=1),
        _clip("build", 50.0, 70.0, cand=0),
        _clip("payoff", 90.0, 110.0, cand=1),
    ]
    out, index_map, msg = _dedup_storyline_clips(clips)
    # 세 clip 모두 원형 그대로 — payoff 가 쪼개지거나 잘리면 안 된다
    assert [(c.role, c.start_sec, c.end_sec) for c in out] == [
        ("hook", 100.0, 102.0),
        ("build", 50.0, 70.0),
        ("payoff", 90.0, 110.0),
    ]
    assert index_map == {0: 0, 1: 1, 2: 2}
    assert "선공개" in msg


def test_preview_absorbed_payoff_case3_overlap_branch():
    # 케이스 3 overlap 분기: hook 본체가 payoff 에 흡수된 형태.
    # preview 구간 == hook 전체 구간(라운드 8c same_cand 케이스)이라도
    # 흡수된 payoff 가 더 길면(진부분집합) 반복이 유지된다.
    clips = [
        _clip("hook", 1194.0, 1215.0, cand=0),     # preview (resolve 로 본체 시간 복원됨)
        _clip("build", 900.0, 930.0, cand=2),
        _clip("payoff", 1180.0, 1240.0, cand=0),   # hook 본체 흡수 → preview ⊂ payoff
    ]
    out, _, msg = _dedup_storyline_clips(clips)
    assert [(c.start_sec, c.end_sec) for c in out] == [
        (1194.0, 1215.0), (900.0, 930.0), (1180.0, 1240.0)]
    assert "선공개" in msg


def test_identical_spans_are_not_treated_as_preview():
    # 사고 케이스 회귀: 완전 동일 구간(진부분집합 아님)은 선공개가 아니라 중복이다
    out, _, msg = _dedup_storyline_clips(_INCIDENT_CLIPS)
    assert [(c.role, c.start_sec, c.end_sec) for c in out] == [
        ("hook", 32.0, 50.0),
        ("payoff", 50.0, 60.0),
    ]
    assert "선공개" not in msg


def test_preview_does_not_shield_a_third_duplicate():
    # preview(2s) + 본편(20s) 반복은 허용하되, 본편이 *또* 반복되면 그건 중복이다
    clips = [
        _clip("hook", 100.0, 102.0, cand=1),
        _clip("payoff", 90.0, 110.0, cand=1),
        _clip("build", 90.0, 110.0, cand=1),   # 세 번째 등장 — 제거 대상
    ]
    out, index_map, _ = _dedup_storyline_clips(clips)
    assert [(c.role, c.start_sec, c.end_sec) for c in out] == [
        ("hook", 100.0, 102.0),
        ("payoff", 90.0, 110.0),
    ]
    assert index_map == {0: 0, 1: 1}
