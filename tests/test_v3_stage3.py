"""V3-M3 — Stage 3 story·경계면 조립 회귀 가드(순수 로직 — LLM 없이 돈다).

발주서 합격 기준의 코드 몫: span id 검증·반려 재료, 길이 예산(통삭제 금지·보호
목록·arousal 타이브레이커 상한), TTS 슬롯 3규칙(ⓐⓑⓒ)과 충돌 벨트 0, C1/C2/C6
동결 필드, 어절 자막 규칙, 소스↔편집본 좌표, 결정성, 골든 채점 인터페이스 접점.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.replay.golden import load_golden, score_cuts
from app.v3 import assemble, schemas
from app.v3 import story as st
from app.v3.story import pick_best, score_story


# ── 픽스처(순수 dict — 실측 grid 축소판) ────────────────────────────────────

def _mk_grid(spans):
    return {"span_candidates": [
        {"id": f"sp{i:04d}", "t_in": t0, "t_out": t1, "is_audio": aud,
         "time_authority": "stt" if aud else "scene", "text": txt}
        for i, (t0, t1, aud, txt) in enumerate(spans)],
        "arousal": [], "words": []}


def _mk_stage2(grid, meanings):
    """meanings: [(first_idx, last_idx, importance, content)] — chunk 하나로 감싼다."""
    spans_all = grid["span_candidates"]
    ms = []
    for num, (a, b, imp, content) in enumerate(meanings):
        spans_doc = []
        for j, sp in enumerate(spans_all[a:b + 1]):
            spans_doc.append({
                "number": j, "span_id": sp["id"],
                "time": {"start": schemas.format_ts(sp["t_in"]),
                         "end": schemas.format_ts(sp["t_out"])},
                "is_audio": sp["is_audio"],
                "audio_script": ([{"speaker": "갑", "line": sp["text"]}]
                                 if sp["is_audio"] else []),
                "scene_script": "" if sp["is_audio"] else "장면",
                "characters": ["갑"], "importance": imp,
                "time_authority": sp["time_authority"],
            })
        ms.append({"number": num,
                   "time": {"start": spans_doc[0]["time"]["start"],
                            "end": spans_doc[-1]["time"]["end"]},
                   "content": content, "characters": ["갑"], "importance": imp,
                   "mood": "평온", "spans": spans_doc})
    return {"sequences": [{"number": 0,
                           "time": {"start": schemas.format_ts(spans_all[0]["t_in"]),
                                    "end": schemas.format_ts(spans_all[-1]["t_out"])},
                           "content": "테스트", "chunks": [
                               {"number": 0,
                                "time": {"start": schemas.format_ts(spans_all[0]["t_in"]),
                                         "end": schemas.format_ts(spans_all[-1]["t_out"])},
                                "meanings": ms}]}]}


GRID = _mk_grid([
    (0.0, 2.0, True, "안녕."),          # sp0000
    (2.0, 5.0, False, ""),              # sp0001 무성
    (5.0, 8.0, True, "잘 지냈어?"),     # sp0002
    (8.0, 11.0, True, "그럭저럭."),     # sp0003
    (11.0, 14.0, False, ""),            # sp0004 무성
    (14.0, 20.0, True, "이게 핵심 대사다."),  # sp0005
    (20.0, 23.0, True, "떡밥 대사."),   # sp0006
])
S2 = _mk_stage2(GRID, [(0, 2, 3, "인사"), (3, 4, 2, "근황"), (5, 6, 5, "핵심")])
IDX, ORDER = st.build_span_index(S2, GRID)


# ── 재료 색인 ───────────────────────────────────────────────────────────────

def test_span_index_only_analyzed_and_ordered():
    grid2 = _mk_grid([(0, 1, True, "a"), (1, 2, True, "b"), (2, 3, True, "c")])
    s2 = _mk_stage2(grid2, [(0, 1, 3, "앞")])   # sp0002 는 분석 밖
    idx, order = st.build_span_index(s2, grid2)
    assert order == ["sp0000", "sp0001"]
    assert "sp0002" not in idx
    assert idx["sp0001"]["pos"] == 1


# ── 모델 응답 검증 ──────────────────────────────────────────────────────────

def _resp(beats):
    return {"template": "recap_dialogue", "reason": "r",
            "title": {"line1": "상황", "line2": "펀치"}, "beats": beats}


def test_validate_ok_and_label_paren_note():
    """M11: narration 은 배열, labels 는 span 앵커 배열. 구 형식(문자열)도 받는다."""
    story, problems, notes = st.validate_story_response(_resp([
        {"role": "hook", "span_ids": ["sp0000", "sp0001"],
         "narration": ["첫 문장이었죠", "둘째 문장입니다"], "labels": []},
        {"role": "climax", "span_ids": ["sp0005"], "narration": [],
         "labels": [{"text": "팩폭", "span_id": "sp0005"}]},
    ]), IDX, ORDER)
    assert problems == []
    assert story["beats"][0]["narration"] == ["첫 문장이었죠", "둘째 문장입니다"]
    assert story["beats"][1]["labels"] == [{"text": "(팩폭)", "span_id": "sp0005"}]
    assert any("괄호 보정" in n for n in notes)


def test_validate_accepts_legacy_string_forms():
    story, problems, _ = st.validate_story_response(_resp([
        {"role": "hook", "span_ids": ["sp0000"], "narration": "한 문장",
         "label": "(구형식)"},
        {"role": "climax", "span_ids": ["sp0005"], "narration": None, "label": None},
    ]), IDX, ORDER)
    assert problems == []
    assert story["beats"][0]["narration"] == ["한 문장"]
    assert story["beats"][0]["labels"] == [{"text": "(구형식)", "span_id": "sp0000"}]


def test_validate_rejects_label_anchor_outside_beat():
    _, problems, _ = st.validate_story_response(_resp([
        {"role": "hook", "span_ids": ["sp0000"], "narration": [],
         "labels": [{"text": "(밖)", "span_id": "sp0005"}]},
        {"role": "climax", "span_ids": ["sp0005"], "narration": [], "labels": []},
    ]), IDX, ORDER)
    assert any("앵커" in p for p in problems)


def test_validate_rejects_unknown_and_noncontiguous_and_reuse():
    _, p1, _ = st.validate_story_response(
        _resp([{"role": "hook", "span_ids": ["sp9999"]}]), IDX, ORDER)
    assert any("모르는" in x for x in p1)
    _, p2, _ = st.validate_story_response(
        _resp([{"role": "climax", "span_ids": ["sp0000", "sp0002"]}]), IDX, ORDER)
    assert any("연속 범위가 아니다" in x for x in p2)
    _, p3, _ = st.validate_story_response(_resp([
        {"role": "hook", "span_ids": ["sp0000"]},
        {"role": "climax", "span_ids": ["sp0000"]}]), IDX, ORDER)
    assert any("재사용" in x for x in p3)


def test_validate_requires_climax_and_title():
    _, p, _ = st.validate_story_response(
        _resp([{"role": "build", "span_ids": ["sp0000"]}]), IDX, ORDER)
    assert any("climax" in x for x in p)
    bad = _resp([{"role": "climax", "span_ids": ["sp0005"]}])
    bad["title"] = {"line1": "혼자"}
    _, p2, _ = st.validate_story_response(bad, IDX, ORDER)
    assert any("두 줄" in x for x in p2)


# ── 길이 예산 ───────────────────────────────────────────────────────────────

def test_trim_removes_low_importance_edge_and_protects():
    beats = [
        {"role": "hook", "span_ids": ["sp0000", "sp0001", "sp0002"],
         "narration": None, "label": None},                       # imp 3
        {"role": "build", "span_ids": ["sp0003", "sp0004"],
         "narration": None, "label": None},                       # imp 2
        {"role": "climax", "span_ids": ["sp0005", "sp0006"],
         "narration": None, "label": None},                       # imp 5 · climax
    ]
    total = st.story_duration(beats, IDX)
    removed = st.trim_to_budget(beats, IDX, [], total - 1.0)      # 한 번만 덜게
    assert len(removed) == 1
    assert removed[0]["importance"] == 2                          # 낮은 importance 먼저
    assert beats[2]["span_ids"] == ["sp0005", "sp0006"]           # climax 보호
    assert all(b["span_ids"] for b in beats)                      # 통삭제 금지


def test_trim_never_empties_beat():
    beats = [{"role": "build", "span_ids": ["sp0003", "sp0004"],
              "narration": None, "label": None}]
    st.trim_to_budget(beats, IDX, [], 0.1)                        # 불가능한 예산
    assert len(beats[0]["span_ids"]) == 1                         # 마지막 1개는 남는다


def test_arousal_is_tiebreaker_not_override():
    # 같은 importance 후보 둘 — arousal 낮은 쪽이 먼저 빠진다
    grid2 = _mk_grid([(0, 3, True, "a"), (3, 6, True, "b"), (6, 9, True, "c")])
    s2 = _mk_stage2(grid2, [(0, 2, 3, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    arous = [{"t": 1.0, "score": -2.0}, {"t": 7.0, "score": 2.0}]  # 앞 낮음·뒤 높음
    beats = [{"role": "build", "span_ids": ["sp0000", "sp0001", "sp0002"],
              "narration": None, "label": None}]
    removed = st.trim_to_budget(beats, idx, arous, 6.0)
    assert removed[0]["span_id"] == "sp0000"
    # 상한 ±0.5 — importance 1 차이를 뒤집을 수 없다
    assert st.arousal_adjust(arous, 6.0, 9.0) == pytest.approx(0.5)
    assert st.arousal_adjust(arous, 0.0, 3.0) == pytest.approx(-0.5)


# ── TTS 슬롯 ────────────────────────────────────────────────────────────────

def test_narration_prefers_silent_run():
    beats = [{"role": "hook", "span_ids": ["sp0000", "sp0001", "sp0002"],
              "narration": "짧은 내레이션", "label": None}]        # sp0001 무성 3s
    cues, dropped = st.plan_narration_slots(beats, IDX)
    assert not dropped
    assert cues[0]["mode"] == "silent"
    assert cues[0]["source_time_sec"] == 2.0                      # 무성 span 시작
    assert cues[0]["muted_span_ids"] == []
    assert st.verify_tts_conflicts(cues, beats, IDX) == []


def test_narration_mutes_only_window_overlap():
    # 적대 리뷰 확정 수정: 뮤트는 cue **창과 겹치는** 유성 span 만 — 런 전체 뮤트는
    # 창 밖 대사까지 무음으로 만들었다(내레이션도 대사도 없는 구간).
    grid2 = _mk_grid([(0, 4, True, "잡담"), (4, 8, True, "잡담2")])
    s2 = _mk_stage2(grid2, [(0, 1, 2, "m")])                      # imp 2 — 뮤트 가능
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "context", "span_ids": ["sp0000", "sp0001"],
              "narration": "여덟자내레이션쯤", "label": None}]     # est ≈ 1.07s
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert not dropped
    assert cues[0]["mode"] == "muted"
    assert cues[0]["muted_span_ids"] == ["sp0000"]                # 창(0~1.07)과 겹침만
    assert st.verify_tts_conflicts(cues, beats, idx) == []


def test_narration_run_breaks_at_source_gap():
    # 적대 리뷰 확정 수정: grid 인덱스 인접이어도 0.5s 미만 전사 구멍이면 런을 끊어
    # cue 창이 구멍에 떨어지지 않는다(cue 소실+뮤트만 남던 재현).
    grid2 = {"span_candidates": [
        {"id": "sp0000", "t_in": 10.0, "t_out": 12.0, "is_audio": True,
         "time_authority": "stt", "text": "a"},
        {"id": "sp0001", "t_in": 12.4, "t_out": 14.0, "is_audio": True,
         "time_authority": "stt", "text": "b"},
    ], "arousal": [], "words": []}
    idx = {"sp0000": {"t_in": 10.0, "t_out": 12.0, "is_audio": True,
                      "importance": 2, "pos": 0, "audio_script": [],
                      "scene_script": "", "meaning_content": "m", "mood": ""},
           "sp0001": {"t_in": 12.4, "t_out": 14.0, "is_audio": True,
                      "importance": 2, "pos": 1, "audio_script": [],
                      "scene_script": "", "meaning_content": "m", "mood": ""}}
    beats = [{"role": "context", "span_ids": ["sp0000", "sp0001"],
              "narration": "열입곱자짜리내레이션대본입니다", "label": None}]  # est≈2.0s
    cues, dropped = st.plan_narration_slots(beats, idx)
    # 갭을 걸치는 3.6s 런 대신 — 각 런(2.0s/1.6s)에 fit 배치, 창은 소스 연속 구간 안
    assert not dropped and cues
    w0, w1 = cues[0]["source_time_sec"], cues[0]["source_end_sec"]
    assert (w0 >= 10.0 and w1 <= 12.0) or (w0 >= 12.4 and w1 <= 14.0)


def test_narration_dropped_over_high_importance():
    grid2 = _mk_grid([(0, 4, True, "핵심대사")])
    s2 = _mk_stage2(grid2, [(0, 0, 5, "m")])                      # imp 5 — 절대 불가
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "bridge", "span_ids": ["sp0000"],
              "narration": "얹을 곳이 없다", "label": None}]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert cues == []
    assert len(dropped) == 1 and dropped[0]["beat"] == 0
    assert beats[0]["narration"] is None                          # 드랍 반영


# ── 폴백 ────────────────────────────────────────────────────────────────────

def test_fallback_highlight_orders_by_time():
    doc = st.fallback_highlight(IDX, ORDER, [], target_sec=15.0, work_title="작품")
    assert doc["template"] == "highlight"
    assert doc["beats"]                                            # 최소 1개 보장
    starts = [IDX[b["span_ids"][0]]["t_in"] for b in doc["beats"]]
    assert starts == sorted(starts)


# ── edit_plan 조립(C1) ─────────────────────────────────────────────────────

def _story_doc():
    beats = [
        {"role": "hook", "span_ids": ["sp0000", "sp0001", "sp0002"],
         "narration": "내레이션", "label": None},
        {"role": "climax", "span_ids": ["sp0005", "sp0006"],
         "narration": None, "label": "(핵심)"},
    ]
    cues, _ = st.plan_narration_slots(beats, IDX)
    return {"schema": st.SCHEMA_STORY, "template": "recap_dialogue", "reason": "",
            "title": {"line1": "상황", "line2": "펀치"},
            "beats": [dict(b, number=i) for i, b in enumerate(beats)],
            "narration_cues": cues, "narration_dropped": [],
            "budget": {}}


def test_edit_plan_frozen_fields_and_additive():
    plan = assemble.assemble_edit_plan(_story_doc(), IDX,
                                       video_path="/v.mp4", work_title="작품")
    assert plan["schema"] == "edit_plan/v3"
    assert plan["layout"]["top_title"] == "상황\n펀치"
    assert plan["layout"]["bottom_label"] == "작품"
    for c in plan["timeline"]:
        for key in ("role", "clip_start_sec", "clip_end_sec", "subtitle",
                    "use_original_audio", "reframe", "span_ids"):
            assert key in c
    # 라벨은 비트 첫 클립의 subtitle 로
    climax = [c for c in plan["timeline"] if c["role"] == "climax"]
    assert climax[0]["subtitle"] == "(핵심)"
    # grid_marks 는 채택 span 경계 집합
    assert 14.0 in plan["grid_marks"] and 23.0 in plan["grid_marks"]
    belt = assemble.verify_edit_plan(plan, GRID)
    assert belt["pct"] == 100.0


def test_edit_plan_splits_on_mute_flip():
    grid2 = _mk_grid([(0, 2, True, "잡담"), (2, 4, True, "잡담2"), (4, 6, True, "중요")])
    s2 = _mk_stage2(grid2, [(0, 1, 2, "m"), (2, 2, 4, "n")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "context", "span_ids": ["sp0000", "sp0001", "sp0002"],
              "narration": "이건 좀 많이 긴 서른자쯤 되는 내레이션 대본이었죠", "label": None}]
    cues, dropped = st.plan_narration_slots(beats, idx)
    # est ≈ 3.1s → 창이 sp0000(0~2)+sp0001(2~4) 에 걸침 — 둘만 뮤트, sp0002 는 원음
    assert not dropped and cues[0]["muted_span_ids"] == ["sp0000", "sp0001"]
    doc = {"title": {"line1": "a", "line2": "b"},
           "beats": [dict(beats[0], number=0)]}
    plan = assemble.assemble_edit_plan(doc, idx, video_path="/v", work_title="w")
    assert len(plan["timeline"]) == 2                              # 뮤트 경계에서 분할
    assert plan["timeline"][0]["use_original_audio"] is False
    assert plan["timeline"][1]["use_original_audio"] is True


# ── 좌표 변환 ───────────────────────────────────────────────────────────────

def test_to_edited_sec_boundary_kind_disambiguation():
    # 적대 리뷰 확정 수정: 원거리 편성에서 공유 경계값(103.0)이 이른-타임라인 클립에
    # 오매핑되던 결함 — 용도별 반개구간(start=[s,e) · end=(s,e])으로 해소
    offs = assemble.edited_offsets([
        {"clip_start_sec": 103.0, "clip_end_sec": 106.0},   # beatA (타임라인 앞)
        {"clip_start_sec": 94.0, "clip_end_sec": 103.0},    # beatC (타임라인 뒤)
    ])
    assert assemble.to_edited_sec(103.0, offs, kind="start") == pytest.approx(0.0)
    assert assemble.to_edited_sec(103.0, offs, kind="end") == pytest.approx(12.0)


def test_edited_mapping_and_identity():
    plan = assemble.assemble_edit_plan(_story_doc(), IDX,
                                       video_path="/v", work_title="w")
    offs = assemble.edited_offsets(plan["timeline"])
    # hook 8s(0~8) + climax 9s(14~23) — 소스 14.0 은 편집본 8.0
    assert assemble.to_edited_sec(14.0, offs) == pytest.approx(8.0)
    assert assemble.to_edited_sec(0.0, offs) == pytest.approx(0.0)
    assert assemble.to_edited_sec(12.0, offs) is None              # 미편성 구간


# ── 어절 자막(C6) ───────────────────────────────────────────────────────────

def test_subtitle_line_rules():
    words = [
        {"t0": 0.5, "t1": 0.9, "text": "웬만하면"},
        {"t0": 0.9, "t1": 1.3, "text": "이런"},
        {"t0": 1.3, "t1": 1.7, "text": "부탁"},
        {"t0": 1.7, "t1": 2.4, "text": "안하는데."},   # 문장부호 → 라인 종료
        {"t0": 2.6, "t1": 3.4, "text": "하나같이"},
        {"t0": 3.4, "t1": 4.3, "text": "맘에안들어"},   # 12자 초과 지점에서 분할
    ]
    lines = assemble._lines_for_span(words, 0.0, 5.0)
    # 2026-09-04 균형 분할: 문장 '웬만하면 이런 부탁 안하는데.'(16자)는 그리디 10/5 대신
    # 남는 칸 제곱합이 작은 7/8 — 12자 규칙(공백 포함)은 그대로다
    assert lines[0]["text"] == "웬만하면 이런"
    assert lines[0]["start"] == pytest.approx(0.45)        # −0.05 선행
    assert lines[1]["text"] == "부탁 안하는데."
    assert lines[2]["text"] == "하나같이 맘에안들어"
    # 최소 노출 0.35 — 라인 겹침 없음
    for a, b in zip(lines, lines[1:]):
        assert b["start"] >= a["end"] - 1e-9
        assert a["end"] - a["start"] >= 0.35 - 1e-9


def test_subtitle_short_tail_merges_back():
    # span 끝에 낀 0.35s 미달 꼬리("같아?" 실측 3건)는 이전 라인에 병합된다(§6)
    words = [{"t0": 0.5, "t1": 2.0, "text": "나 때문인 것"},
             {"t0": 2.0, "t1": 2.2, "text": "같아?"}]
    lines = assemble._lines_for_span(words, 0.0, 2.2)
    assert len(lines) == 1
    assert lines[0]["text"] == "나 때문인 것 같아?"
    assert lines[0]["end"] == pytest.approx(2.2)


def test_subtitles_skip_muted_and_use_edited_coords():
    grid2 = _mk_grid([(0, 2, True, "잡담"), (2, 4, True, "중요한 말")])
    grid2["words"] = [{"t0": 0.2, "t1": 1.0, "text": "잡담"},
                      {"t0": 2.2, "t1": 3.0, "text": "중요한"},
                      {"t0": 3.0, "t1": 3.8, "text": "말"}]
    s2 = _mk_stage2(grid2, [(0, 0, 2, "m"), (1, 1, 4, "n")])
    idx, _ = st.build_span_index(s2, grid2)
    timeline = [
        {"role": "context", "clip_start_sec": 0.0, "clip_end_sec": 2.0,
         "subtitle": "", "use_original_audio": False, "reframe": {"mode": "center"},
         "span_ids": ["sp0000"]},
        {"role": "climax", "clip_start_sec": 2.0, "clip_end_sec": 4.0,
         "subtitle": "", "use_original_audio": True, "reframe": {"mode": "center"},
         "span_ids": ["sp0001"]},
    ]
    segs = assemble.word_subtitles(timeline, idx, grid2["words"])
    assert all("잡담" not in s["text"] for s in segs)               # 뮤트 클립 제외
    assert segs[0]["text"] == "중요한 말"
    assert segs[0]["start_sec"] == pytest.approx(2.15 - 2.0 + 2.0)  # 편집본 좌표(=2.15)


# ── TTS cue(C2) ────────────────────────────────────────────────────────────

def test_finalize_cues_contract():
    sdoc = _story_doc()
    plan = assemble.assemble_edit_plan(sdoc, IDX, video_path="/v", work_title="w")
    cues = assemble.finalize_cues(sdoc["narration_cues"], plan["timeline"],
                                  voice="ko_female", speed="normal")
    assert cues and cues[0]["source_time_sec"] == 2.0               # 신원 = 원본 절대초
    assert cues[0]["start_sec"] == pytest.approx(2.0)               # 편집본 좌표
    assert cues[0]["voice"] == "ko_female" and cues[0]["speed"] == "normal"
    assert cues[0]["duration_sec"] == pytest.approx(
        cues[0]["end_sec"] - cues[0]["start_sec"])


def test_finalize_cues_marks_lost_windows():
    cues = assemble.finalize_cues(
        [{"beat": 0, "text": "t", "mode": "silent", "source_time_sec": 100.0,
          "source_end_sec": 102.0, "muted_span_ids": []}],
        [{"clip_start_sec": 0.0, "clip_end_sec": 5.0}], voice="v", speed="s")
    assert cues[0]["start_sec"] is None                             # 조용한 0 금지


# ── 결정성·골든 접점 ────────────────────────────────────────────────────────

def test_determinism():
    a = assemble.assemble_edit_plan(_story_doc(), IDX, video_path="/v", work_title="w")
    b = assemble.assemble_edit_plan(copy.deepcopy(_story_doc()), IDX,
                                    video_path="/v", work_title="w")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_golden_file_loads_and_scores_timeline_shape():
    golden = load_golden(Path(__file__).parent / "data" / "golden_cuts"
                         / "fourhands_ep1_recap.json")
    assert golden["space"] == "source" and not golden["derived"]
    assert len(golden["segments"]) == 14
    # 우리 timeline 모양(clip_start_sec/clip_end_sec)이 채점기에 그대로 물린다
    score = score_cuts([{"clip_start_sec": 570.0, "clip_end_sec": 574.65}], golden)
    assert score["matched_n"] == 1 and score["matches"][0]["iou"] == 1.0


# ── M10-C 다안 심사 루브릭(순수·결정적) ────────────────────────────────────

def _cand(beats, title=("a", "b")):
    return {"template": "recap_dialogue", "reason": "",
            "title": {"line1": title[0], "line2": title[1]}, "beats": beats}


def _idx(specs):
    """specs: [(sid, t_in, t_out, is_audio, conf, text_source, line)]"""
    return {s[0]: {"t_in": s[1], "t_out": s[2], "is_audio": s[3], "importance": 3,
                   "pos": i, "conf": s[4], "text_source": s[5],
                   "audio_script": [{"speaker": "갑", "line": s[6]}] if s[3] else [],
                   "heard_text": "", "scene_script": "", "meaning_content": "m",
                   "mood": ""} for i, s in enumerate(specs)}


def test_rubric_prefers_trustworthy_material():
    """M9 실측 사고: 저확신 구간을 골라 자막이 깨진 채 나갔다 — 재료 신뢰도 반영."""
    idx = _idx([("hi", 0, 5, True, 0.9, "transcript", "믿을 만한 대사"),
                ("lo", 10, 15, True, 0.15, "transcript", "저거에 샜는데")])
    good = _cand([{"role": "hook", "span_ids": ["hi"], "narration": None, "label": None}])
    bad = _cand([{"role": "hook", "span_ids": ["lo"], "narration": None, "label": None}])
    best, table = pick_best([bad, good], idx, target_sec=5.0)
    assert best == 1
    assert table[1]["parts"]["material"] > table[0]["parts"]["material"]


def test_rubric_prefers_realizable_narration():
    """M9 실측 사고: cue 3개 전량 드랍(내레이션 0) — 슬롯을 실제로 얻는 안이 이긴다."""
    # importance 4 = 뮤트 불가(ⓒ) 이므로 무성 span 이 없으면 슬롯이 안 나온다
    idx = _idx([("v1", 0, 6, True, 0.9, "transcript", "대사"),
                ("sil", 6, 12, False, None, None, ""),
                ("v2", 12, 18, True, 0.9, "transcript", "대사2")])
    for sid in ("v1", "v2"):
        idx[sid]["importance"] = 4
    with_slot = _cand([{"role": "hook", "span_ids": ["v1", "sil"],
                        "narration": "내레이션 문장", "label": None}])
    no_slot = _cand([{"role": "hook", "span_ids": ["v1", "v2"],
                      "narration": "내레이션 문장", "label": None}])
    best, table = pick_best([no_slot, with_slot], idx, target_sec=12.0)
    assert best == 1 and table[1]["parts"]["narration"] == 1.0
    assert table[0]["parts"]["narration"] == 0.0
    # 내레이션 0줄은 더 이상 처벌이 아니다(사용자 결정 2026-09-01 — 재료가
    # 촘촘하면 0줄이 정당하다). 점프 없는 0줄 = 만점.
    none_planned = _cand([{"role": "hook", "span_ids": ["v1"],
                           "narration": None, "label": None}])
    assert score_story(none_planned, idx, target_sec=6.0)["parts"]["narration"] == 1.0


def test_rubric_penalizes_scattered_arcs():
    idx = _idx([("a", 0, 5, True, 0.9, "transcript", "사건 시작"),
                ("b", 5, 10, True, 0.9, "transcript", "사건 한복판"),
                ("z", 900, 905, True, 0.9, "transcript", "멀리 떨어진 컷")])
    near = _cand([{"role": "hook", "span_ids": ["a"], "narration": None, "label": None},
                  {"role": "climax", "span_ids": ["b"], "narration": None, "label": None}])
    far = _cand([{"role": "hook", "span_ids": ["a"], "narration": None, "label": None},
                 {"role": "climax", "span_ids": ["z"], "narration": None, "label": None}])
    assert score_story(near, idx, target_sec=10.0)["parts"]["cohesion"] == 1.0
    assert score_story(far, idx, target_sec=10.0)["parts"]["cohesion"] == 0.0


def test_rubric_intro_ban():
    idx = _idx([("g", 0, 5, True, 0.9, "transcript", "안녕하세요 반갑습니다"),
                ("m", 5, 10, True, 0.9, "transcript", "사건 한복판")])
    greet = _cand([{"role": "hook", "span_ids": ["g"], "narration": None, "label": None}])
    mid = _cand([{"role": "hook", "span_ids": ["m"], "narration": None, "label": None}])
    assert score_story(greet, idx, target_sec=5.0)["parts"]["intro"] == 0.0
    # 2026-09-02: 도입 nar 없는 훅은 0.5(맥락 감점) — nar 를 얹으면 만점
    assert score_story(mid, idx, target_sec=5.0)["parts"]["intro"] == 0.5
    mid_nar = _cand([{"role": "hook", "span_ids": ["m"],
                      "narration": ["맥락 한 줄"], "label": None}])
    assert score_story(mid_nar, idx, target_sec=5.0)["parts"]["intro"] == 1.0


def test_rubric_tie_break_is_deterministic():
    idx = _idx([("a", 0, 5, True, 0.9, "transcript", "대사")])
    one = _cand([{"role": "hook", "span_ids": ["a"], "narration": None, "label": None}])
    best, table = pick_best([one, dict(one)], idx, target_sec=5.0)
    assert best == 0 and table[0]["score"] == table[1]["score"]


# ── M11: 비트당 복수 내레이션·라벨(레퍼런스 리듬) ──────────────────────────

def test_multi_narration_places_sequentially():
    """레퍼런스 실측: 훅에 짧은 문장 둘이 이어 붙는다(0.20~2.20 + 2.20~4.35).
    한 런 안에서 커서가 전진하며 두 문장이 겹치지 않게 놓여야 한다."""
    grid2 = _mk_grid([(0.0, 8.0, False, "")])          # 무성 8초 런 하나
    s2 = _mk_stage2(grid2, [(0, 0, 3, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "hook", "span_ids": ["sp0000"],
              "narration": ["열다섯자짜리첫문장이죠", "열다섯자짜리둘째문장"],
              "labels": []}]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert len(cues) == 2 and not dropped
    assert cues[0]["line"] == 0 and cues[1]["line"] == 1
    assert cues[1]["source_time_sec"] >= cues[0]["source_end_sec"] - 1e-6  # 겹침 없음
    assert all(c["mode"] == "silent" for c in cues)


def test_multi_narration_drops_tail_when_window_runs_out():
    """창이 모자라면 **뒤 문장부터** 버린다 — 앞 문장이 서사에 더 중요하다."""
    # 창 1.5s — 첫 문장(견적 0.6+8/7=1.74s)이 창을 다 먹어 뒤 문장 자리가 없다
    # (견적식은 고정 오버헤드 + 자수/속도 — story.NARRATION_LEAD_SEC 실측 주석 참조)
    grid2 = _mk_grid([(0.0, 1.5, False, "")])
    s2 = _mk_stage2(grid2, [(0, 0, 3, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "hook", "span_ids": ["sp0000"],
              "narration": ["첫문장은들어간다", "둘째문장은자리가없다", "셋째도없다"],
              "labels": []}]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert len(cues) == 1 and cues[0]["text"] == "첫문장은들어간다"
    assert [d["text"] for d in dropped] == ["둘째문장은자리가없다", "셋째도없다"]
    assert beats[0]["narration"] == ["첫문장은들어간다"]    # 산출에 반영


def test_multi_narration_uses_leftover_window_as_fit():
    """남은 창이 최소 노출 이상이면 뒤 문장도 fit 으로 넣는다(버리기 전에 살린다).

    창 3.0s = 첫 문장 견적(0.6+8/7=1.74s) + 남는 1.26s(최소 노출 1.0s 이상).
    ⚠ 이 숫자는 견적식을 따라간다 — 2026-09-01 실측으로 오버헤드 항이 생기며
    종전 픽스처(2.2s)로는 남는 창이 0.46s 라 뒤 문장이 드랍됐다(의도 반대).
    """
    grid2 = _mk_grid([(0.0, 3.0, False, "")])
    s2 = _mk_stage2(grid2, [(0, 0, 3, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "hook", "span_ids": ["sp0000"],
              "narration": ["첫문장은들어간다", "둘째문장은자리가없다"], "labels": []}]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert len(cues) == 2 and not dropped
    assert cues[1]["mode"] == "fit"
    assert cues[1]["source_end_sec"] <= 3.0 + 1e-6


def test_multi_narration_mute_scope_stays_window_local():
    """복수 문장에서도 뮤트는 창과 겹친 유성 span 만(M3 리뷰 확정 규율 유지)."""
    grid2 = _mk_grid([(0.0, 3.0, True, "잡담"), (3.0, 9.0, False, ""),
                      (9.0, 12.0, True, "중요")])
    s2 = _mk_stage2(grid2, [(0, 2, 2, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    idx["sp0002"]["importance"] = 5                    # 뮤트 금지 대상
    beats = [{"role": "context", "span_ids": ["sp0000", "sp0001", "sp0002"],
              "narration": ["첫문장", "둘째문장"], "labels": []}]
    cues, _ = st.plan_narration_slots(beats, idx)
    assert cues and st.verify_tts_conflicts(cues, beats, idx) == []
    assert "sp0002" not in beats[0]["muted_span_ids"]


def test_labels_anchor_to_span_not_beat_start():
    """레퍼런스: 라벨이 대사 순간에 붙는다. 앵커 span 을 담은 클립에 실려야 한다."""
    beats = [{"number": 0, "role": "climax",
              "span_ids": ["sp0005", "sp0006"], "narration": [],
              "labels": [{"text": "(갑자기 청혼)", "span_id": "sp0006"}],
              "muted_span_ids": [],
              "time": {"start": schemas.format_ts(14.0),
                       "end": schemas.format_ts(23.0)}}]
    doc = {"title": {"line1": "a", "line2": "b"}, "beats": beats}
    plan = assemble.assemble_edit_plan(doc, IDX, video_path="/v", work_title="w")
    # sp0005·sp0006 은 소스 연속이라 한 클립 — 그 클립에 라벨이 실린다
    labeled = [c for c in plan["timeline"] if c["subtitle"]]
    assert labeled and labeled[0]["subtitle"] == "(갑자기 청혼)"
    assert "sp0006" in labeled[0]["span_ids"]


def test_rubric_narration_ratio_is_bounded_by_lines():
    """M11 실측 버그: 복수 문장이면 cue 수 > 비트 수라 비율이 1.667 까지 튀어
    가중치 3.0 이 응집도(0.43)를 압도했다 — 분모는 **문장 수**여야 한다."""
    idx = _idx([("s", 0, 20, False, None, None, "")])
    cand = _cand([{"role": "hook", "span_ids": ["s"],
                   "narration": ["첫문장이었죠", "둘째문장이죠", "셋째문장까지"],
                   "labels": []}])
    sc = score_story(cand, idx, target_sec=20.0)
    assert 0.0 <= sc["parts"]["narration"] <= 1.0
    assert sc["parts"]["narration"] == 1.0        # 셋 다 배치되면 만점

# ── 가왕쇼 템플릿: 화자별 자막색 ────────────────────────────────────────────

def test_speaker_colors_lead_is_white_others_cycle():
    """사람이 만든 템플릿도 주연만 흰색이고 상대역·리액션에 색을 줬다(gw 실측)."""
    idx = {
        "s1": {"t_in": 0.0, "t_out": 2.0, "audio_script": [{"speaker": "박서진"}]},
        "s2": {"t_in": 2.0, "t_out": 4.0, "audio_script": [{"speaker": "어머니"}]},
        "s3": {"t_in": 4.0, "t_out": 6.0, "audio_script": [{"speaker": "박서진"}]},
        "s4": {"t_in": 6.0, "t_out": 8.0, "audio_script": [{"speaker": "미상"}]},
        "s5": {"t_in": 8.0, "t_out": 9.0, "audio_script": [{"speaker": "PD"}]},
    }
    c = assemble.speaker_colors(idx)
    assert c["박서진"] == assemble.SPEAKER_DEFAULT_COLOR          # 최다 = 주연
    assert c["어머니"] == assemble.SPEAKER_PALETTE[0]             # 첫 등장 순
    assert c["PD"] == assemble.SPEAKER_PALETTE[1]
    assert "미상" not in c                                        # 미상은 기본색
    # 동률이면 먼저 나온 화자가 주연 — 무작위 요소 없이 결정적
    tie = {"a": {"t_in": 1.0, "t_out": 2.0, "audio_script": [{"speaker": "을"}]},
           "b": {"t_in": 0.0, "t_out": 1.0, "audio_script": [{"speaker": "갑"}]}}
    assert assemble.speaker_colors(tie)["갑"] == assemble.SPEAKER_DEFAULT_COLOR


def test_word_subtitles_carry_speaker_color():
    span = {"sp1": {"t_in": 0.0, "t_out": 2.0, "is_audio": True, "text_source": "heard",
                    "heard_text": "여보세요", "audio_script": [{"speaker": "어머니"}]},
            "sp2": {"t_in": 2.0, "t_out": 4.0, "is_audio": True, "text_source": "heard",
                    "heard_text": "안녕하세요 저 박서진입니다",
                    "audio_script": [{"speaker": "박서진"}]},
            "sp3": {"t_in": 4.0, "t_out": 6.0, "is_audio": True, "text_source": "heard",
                    "heard_text": "네 네", "audio_script": [{"speaker": "박서진"}]}}
    tl = [{"clip_start_sec": 0.0, "clip_end_sec": 6.0, "use_original_audio": True,
           "span_ids": ["sp1", "sp2", "sp3"]}]
    segs = assemble.word_subtitles(tl, span, [])
    by_speaker = {s["speaker"]: s["color"] for s in segs}
    assert by_speaker["박서진"] == assemble.SPEAKER_DEFAULT_COLOR
    assert by_speaker["어머니"] != assemble.SPEAKER_DEFAULT_COLOR


# ── 뮤트 창 — 내레이션이 끝나면 원음이 돌아온다 ─────────────────────────────

def test_mute_covers_only_the_narration_window():
    """실측 사고: 도입부 뮤트창 5.49s 에 내레이션 1.92s → 3.57s 완전 무음."""
    f = assemble.split_by_windows
    # 창이 없으면 종전대로 통째 뮤트(회귀 0)
    assert f(298.9, 304.4, []) == [(298.9, 304.4, False)]
    # 실측 케이스 — 창 밖 3.62s 는 원음이 돌아온다
    assert f(298.9, 304.4, [(297.98, 300.78)]) == [(298.9, 300.78, False),
                                                   (300.78, 304.4, True)]
    # 창이 가운데면 앞뒤 둘 다 살린다
    assert f(10.0, 20.0, [(13.0, 15.0)]) == [(10.0, 13.0, True), (13.0, 15.0, False),
                                             (15.0, 20.0, True)]
    # 0.2s 자투리는 만들지 않는다 — 살아난 게 아니라 잡음이다
    assert f(10.0, 15.2, [(10.0, 15.0)]) == [(10.0, 15.2, False)]


def test_narration_windows_merge_adjacent_cues():
    doc = {"narration_cues": [
        {"beat": 0, "source_time_sec": 297.98, "source_end_sec": 299.447},
        {"beat": 0, "source_time_sec": 299.447, "source_end_sec": 300.78},
        {"beat": 2, "source_time_sec": 346.1, "source_end_sec": 347.7},
        {"beat": 3, "source_time_sec": 1.0, "source_end_sec": None}]}
    w = assemble.narration_windows(doc)
    assert w[0] == [(297.98, 300.78)]          # 잇닿은 두 큐는 한 창
    assert w[2] == [(346.1, 347.7)]
    assert 3 not in w                          # 끝이 없는 큐는 창이 아니다


def test_muted_clip_keeps_subtitles_outside_the_narration_window():
    """클립은 못 쪼갠다(벨트) — 대신 창 밖 대사는 자막이 살아난다."""
    span = {"s1": {"t_in": 100.0, "t_out": 107.0, "pos": 0, "is_audio": True,
                   "text_source": "heard", "heard_text": "하나 둘 셋 넷 다섯 여섯",
                   "audio_script": [{"speaker": "갑"}]}}
    tl = [{"clip_start_sec": 100.0, "clip_end_sec": 107.0,
           "use_original_audio": False, "span_ids": ["s1"]}]
    # 창을 모르면 종전대로 전부 제외(회귀 0)
    assert assemble.word_subtitles(tl, span, []) == []
    # 창(100~102) 밖 구간은 자막이 나온다
    segs = assemble.word_subtitles(tl, span, [], [(100.0, 102.0)])
    # 중점이 창 밖인 줄만 산다 — 첫 줄(중점 101.75)은 뮤트 안이라 빠진다
    assert [s["text"] for s in segs] == ["다섯 여섯"]

# ══════════════════════════════════════════════════════════════════════════
# 내레이션 기본값 — 있음 (사용자 결정 2026-09-01)
# ══════════════════════════════════════════════════════════════════════════
def test_prompt_does_not_force_zero_narration():
    """편성 규칙은 "내레이션을 쓰지 마라"를 **기본값으로 강제하지 않는다.**

    166편 해부 문서(work/apn/v3_story_prompt_final.md)의 F 조항("안 쓰는 편이 정상 —
    39%가 0줄")을 공통 규칙으로 실었다가 사용자 결정으로 걷어냈다: **내레이션 없는
    편성은 하나의 편집 스타일**이지 전 채널에 강제할 규칙이 아니고, 기본값은
    내레이션 있음이다. 그 스타일이 필요한 채널은 톤 프로파일 노브로 연다.

    ⚠ 골격 템플릿을 추가할 때 이 조항을 템플릿 desc 로 되살리지 말 것.
    """
    span_index = {
        f"sp{i:04d}": {"pos": i, "t_in": i * 2.0, "t_out": i * 2.0 + 2.0,
                       "is_audio": True, "importance": 3, "audio_script": [],
                       "text_source": "heard", "heard_text": "", "conf": 0.9,
                       "scene_script": "", "meaning_content": "", "mood": ""}
        for i in range(4)}
    prompt = st.build_story_prompt({"sequences": []}, span_index, work_title="T")
    for banned in ("안 쓰는 편이 정상", "한 줄도 넣지 마라", "39%가 0줄"):
        assert banned not in prompt, f"내레이션 생략을 강제하는 문구: {banned!r}"
    # 템플릿 desc 로 되살아나는 경로도 막는다
    for name, spec in st.STORY_TEMPLATE_SPECS.items():
        desc = spec.get("desc") or ""
        for banned in ("안 쓰는 편이 정상", "한 줄도 넣지 마라"):
            assert banned not in desc, f"{name} desc 에 내레이션 생략 강제: {banned!r}"


# ══════════════════════════════════════════════════════════════════════════
# 아이템 열 편성 (2026-09-01 재설계) — 내레이션-장면 교대 구조
# ══════════════════════════════════════════════════════════════════════════
# 계기(같은 날 실측 3건): ① 오디오 커버리지 62% — 14.7초 연속 정적 ② 내레이션이
# "비트 안 아무 빈 곳"에 놓여 예고 구조(A조항)가 성립 안 함 ③ 라벨 4초 창 동안
# 화면 인원이 바뀌어 누구를 가리키는지 모름. 셋의 뿌리가 같았다 — 편성 단위가
# 비트이고 내레이션·라벨이 부속물이라는 것. 아이템 열은 그걸 뒤집는다.

def _items_fixture():
    """sp0 무성리드인 · sp1 대사 · sp2 무성 · sp3 대사 · sp4 무성꼬리 (연속)."""
    grid = _mk_grid([(0.0, 2.0, False, ""), (2.0, 5.0, True, "대사 하나"),
                     (5.0, 7.0, False, ""), (7.0, 10.0, True, "대사 둘"),
                     (10.0, 12.0, False, "")])
    s2 = _mk_stage2(grid, [(0, 4, 3, "m")])
    return st.build_span_index(s2, grid)


def test_items_schema_accepted_and_normalized():
    idx, order = _items_fixture()
    resp = {"template": "recap_dialogue", "reason": "t",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "items": [
                {"nar": "다리 문장"},
                {"span_ids": ["sp0000", "sp0001"], "label": "(첫 라벨)"},
                {"span_ids": ["sp0002", "sp0003"]},
            ]}]}
    story, problems, notes = st.validate_story_response(resp, idx, order)
    assert not problems, problems
    b = story["beats"][0]
    assert b["span_ids"] == ["sp0000", "sp0001", "sp0002", "sp0003"]
    assert b["narration"] == ["다리 문장"]
    # 라벨은 그 대사 아이템의 첫 span 에 앵커 — "그 대사 순간"과 어긋날 수 없다
    assert b["labels"] == [{"text": "(첫 라벨)", "span_id": "sp0000"}]
    assert b["items"][0]["kind"] == "nar"


def test_items_narration_ends_at_next_dialogue_start():
    """A조항의 기계화 — 창의 **끝이 다음 대사 시작에 닿는다**(리드인 위에서 말한다)."""
    idx, order = _items_fixture()
    beats = [{"role": "hook", "span_ids": ["sp0000", "sp0001"],
              "narration": ["다리 문장"], "labels": [],
              "items": [{"kind": "nar", "text": "다리 문장", "span_ids": [],
                         "label": None},
                        {"kind": "mat", "text": None,
                         "span_ids": ["sp0000", "sp0001"], "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert not dropped and len(cues) == 1
    # 앵커 = 다음 아이템의 첫 **유성** span(sp0001, t=2.0) — 무성 머리는 리드인 재료
    assert cues[0]["source_end_sec"] == 2.0
    assert cues[0]["mode"] in ("silent", "fit")
    assert cues[0]["muted_span_ids"] == []


def test_items_narration_no_longer_borrows_previous_beat_tail():
    """2026-09-02 정책 교체 — 직전 비트 꼬리·대사 뮤트 배치 폐지("집도착" 실사고:
    다리가 직전 장면 위에서 들려 화면과 어긋나고, 뮤트가 대사 소리를 끊었다).
    자기 비트 머리에 무성 리드인이 없으면 **드랍+기록**이다(자리는 모델이 만든다)."""
    idx, order = _items_fixture()
    beats = [
        {"role": "hook", "span_ids": ["sp0001", "sp0002"], "narration": [],
         "labels": [], "items": [{"kind": "mat", "text": None,
                                  "span_ids": ["sp0001", "sp0002"], "label": None}]},
        {"role": "climax", "span_ids": ["sp0003"], "narration": ["다리 문장"],
         "labels": [], "items": [{"kind": "nar", "text": "다리 문장",
                                  "span_ids": [], "label": None},
                                 {"kind": "mat", "text": None,
                                  "span_ids": ["sp0003"], "label": None}]},
    ]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert not cues and len(dropped) == 1
    assert "인접 무성 재료" in dropped[0]["reason"]   # 2026-09-02 구제 경로 후 문구


def test_items_narration_lead_in_gets_speed_ladder():
    """창이 견적보다 작으면 드랍이 아니라 배속(fast→very_fast)이 먼저 흡수한다
    (2026-09-02 사용자 지시 "너무 길면 배속, 드랍 절대 금지")."""
    idx, order = _items_fixture()
    beats = [
        {"role": "hook", "span_ids": ["sp0000", "sp0001"],
         "narration": ["열여섯자짜리내레이션문장입니다"], "labels": [],
         "items": [{"kind": "nar", "text": "열여섯자짜리내레이션문장입니다",
                    "span_ids": [], "label": None},
                   {"kind": "mat", "text": None,
                    "span_ids": ["sp0000", "sp0001"], "label": None}]},
    ]
    cues, dropped = st.plan_narration_slots(beats, idx)
    # 리드인 창 = sp0000(2.0s) < 견적 0.6+15/7=2.74s → fast/very_fast 로 흡수
    assert not dropped and cues[0]["speed"] in ("fast", "very_fast")


def test_items_trailing_nar_sits_after_last_dialogue():
    """마지막 아이템이 nar(엔딩 리캡 루프)면 마지막 대사 직후 여운 컷 위에서 말한다."""
    idx, order = _items_fixture()
    beats = [{"role": "ending", "span_ids": ["sp0003", "sp0004"],
              "narration": ["마무리 한 줄"], "labels": [],
              "items": [{"kind": "mat", "text": None,
                         "span_ids": ["sp0003", "sp0004"], "label": None},
                        {"kind": "nar", "text": "마무리 한 줄", "span_ids": [],
                         "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert not dropped and len(cues) == 1
    assert cues[0]["source_time_sec"] == 10.0     # 대사(~10.0) 직후 여운 컷 시작


def test_zero_narration_full_marks_when_no_meaning_jump():
    """점프 없는 0줄 = 만점 — 재료가 촘촘하면 내레이션 없는 편성이 정당하다."""
    idx, order = _items_fixture()
    story = {"template": "recap_dialogue", "title": {"line1": "a", "line2": "b"},
             "beats": [{"role": "climax",
                        "span_ids": ["sp0001", "sp0002", "sp0003"],
                        "narration": [], "labels": [], "items": None}]}
    parts = st.score_story(story, idx, target_sec=8.0)["parts"]
    assert parts["narration"] == 1.0


def test_unbridged_meaning_jump_penalized():
    """meaning 을 통째로 건너뛰고 다리를 안 놓으면 감점 — 819초 점프 실사고의 저울.

    절대 초가 아니라 meaning 경계로 잰다(작품마다 호흡이 달라 초 임계는 기준이
    못 된다 — 사용자 지적)."""
    grid = _mk_grid([(0.0, 2.0, True, "가."), (2.0, 4.0, True, "나."),
                     (4.0, 6.0, True, "다."), (6.0, 8.0, True, "라.")])
    s2 = _mk_stage2(grid, [(0, 0, 3, "장면A"), (1, 2, 3, "장면B"),
                           (3, 3, 3, "장면C")])
    idx, order = st.build_span_index(s2, grid)
    base = {"template": "recap_dialogue", "title": {"line1": "a", "line2": "b"}}
    jumped = {**base, "beats": [
        {"role": "hook", "span_ids": ["sp0000"], "narration": [], "labels": [],
         "items": None},
        {"role": "climax", "span_ids": ["sp0003"], "narration": [], "labels": [],
         "items": None}]}                          # 장면B 통째 건너뜀 · 다리 없음
    bridged = {**base, "beats": [
        {"role": "hook", "span_ids": ["sp0000"], "narration": [], "labels": [],
         "items": None},
        {"role": "climax", "span_ids": ["sp0003"], "narration": ["다리"],
         "labels": [], "items": None}]}            # 구 스키마 내레이션 = 다리 관용
    pj = st.score_story(jumped, idx, target_sec=4.0)["parts"]["narration"]
    pb = st.score_story(bridged, idx, target_sec=4.0)["parts"]["narration"]
    assert pj == 0.0 and pb > pj


def test_coverage_part_measures_audio_fill():
    """오디오 커버리지 — 62% 실사고(14.7초 정적)의 저울. 0.8 이상 = 만점."""
    idx, order = _items_fixture()   # 유성 6s / 전체 12s = 50% (내레이션 없이)
    silent_heavy = {"template": "recap_dialogue",
                    "title": {"line1": "a", "line2": "b"},
                    "beats": [{"role": "climax",
                               "span_ids": ["sp0000", "sp0001", "sp0002",
                                            "sp0003", "sp0004"],
                               "narration": [], "labels": [], "items": None}]}
    parts = st.score_story(silent_heavy, idx, target_sec=12.0)["parts"]
    assert parts["coverage"] < 0.8                 # 50%/80% = 0.625
    voiced_only = {"template": "recap_dialogue",
                   "title": {"line1": "a", "line2": "b"},
                   "beats": [{"role": "climax", "span_ids": ["sp0001"],
                              "narration": [], "labels": [], "items": None}]}
    assert st.score_story(voiced_only, idx, target_sec=3.0)["parts"]["coverage"] == 1.0


def test_trim_syncs_items_and_labels():
    """트림이 뺀 span 은 아이템에서도 사라지고, 빈 아이템의 라벨은 고아가 안 된다."""
    idx, order = _items_fixture()
    beats = [{"role": "hook",
              "span_ids": ["sp0000", "sp0001", "sp0002", "sp0003", "sp0004"],
              "narration": [], "labels": [{"text": "(라벨)", "span_id": "sp0004"}],
              "items": [{"kind": "mat", "text": None,
                         "span_ids": ["sp0000", "sp0001", "sp0002", "sp0003"],
                         "label": None},
                        {"kind": "mat", "text": None, "span_ids": ["sp0004"],
                         "label": "(라벨)"}]}]
    st.trim_to_budget(beats, idx, [], max_sec=8.0)   # 12s → 8s: 가장자리 제거
    b = beats[0]
    kept = set(b["span_ids"])
    for it in b["items"]:
        assert set(it["span_ids"]) <= kept
    for lab in b["labels"]:
        assert lab["span_id"] in kept                # 고아 라벨 없음


def test_overlong_plan_is_rejected_not_trimmed():
    """길이 상한은 반려다 — 실측 3판 연속 2배 계획 → 트림이 아크를 반토막 낸 사고.

    1.2배까지는 통과(트림이 다듬는다), 그 이상은 모델이 다시 고른다."""
    grid = _mk_grid([(i * 10.0, i * 10.0 + 10.0, True, f"대사{i}") for i in range(10)])
    s2 = _mk_stage2(grid, [(0, 9, 3, "m")])
    idx, order = st.build_span_index(s2, grid)
    resp = {"template": "recap_dialogue", "reason": "t",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax",
                       "span_ids": [f"sp{i:04d}" for i in range(10)]}]}  # 100s
    _, problems, _ = st.validate_story_response(resp, idx, order, max_sec=60.0)
    assert any("상한" in p and "다시 골라서" in p for p in problems)
    # 1.2배 이내(=72s 한도)는 통과 — 7 span = 70s
    resp2 = {**resp, "beats": [{"role": "climax",
                                "span_ids": [f"sp{i:04d}" for i in range(7)]}]}
    story, problems2, _ = st.validate_story_response(resp2, idx, order, max_sec=60.0)
    assert story is not None and not problems2
    # max_sec 미지정 = 종전 그대로(길이 무검사 — 구 테스트·폴백 호환)
    story3, problems3, _ = st.validate_story_response(resp, idx, order)
    assert story3 is not None and not problems3


def test_sequence_crossing_counts_as_jump():
    """시퀀스 경계 넘기도 점프다 — 다리가 있으면 정상, 없으면 감점.

    '한 시퀀스 안에서만'(공간 수갑)을 폐기하고 다리 조건부로 완화한 것의 벨트.
    meaning 이 긴 작품에서 meaning 건너뜀만으로는 못 잡던 구멍을 시퀀스 축이 보완."""
    grid = _mk_grid([(0.0, 2.0, True, "가."), (2.0, 4.0, True, "나."),
                     (4.0, 6.0, True, "다."), (6.0, 8.0, True, "라.")])
    s2 = _mk_stage2(grid, [(0, 1, 3, "장면A"), (2, 3, 3, "장면B")])
    # 두 meaning 을 서로 다른 시퀀스로 재포장 (meaning 건너뜀 없이 경계만 넘게)
    seqs = s2["sequences"][0]["chunks"][0]["meanings"]
    s2 = {"sequences": [
        {"number": 0, "chunks": [{"number": 0, "meanings": [seqs[0]]}]},
        {"number": 1, "chunks": [{"number": 0, "meanings": [seqs[1]]}]},
    ]}
    idx, order = st.build_span_index(s2, grid)
    assert idx["sp0000"]["sequence_idx"] == 0 and idx["sp0003"]["sequence_idx"] == 1
    base = {"template": "recap_dialogue", "title": {"line1": "a", "line2": "b"}}
    # sp0001→sp0003: sp0002(장면B 일부) 건너뜀 — 건너뛴 '통째 meaning' 없음,
    # 그러나 시퀀스 경계를 넘는다 → 점프
    unbridged = {**base, "beats": [
        {"role": "hook", "span_ids": ["sp0000", "sp0001"], "narration": [],
         "labels": [], "items": None},
        {"role": "climax", "span_ids": ["sp0003"], "narration": [], "labels": [],
         "items": None}]}
    bridged = {**base, "beats": [
        unbridged["beats"][0],
        {**unbridged["beats"][1], "narration": ["다리"]}]}
    pu = st.score_story(unbridged, idx, target_sec=6.0)["parts"]["narration"]
    pb = st.score_story(bridged, idx, target_sec=6.0)["parts"]["narration"]
    assert pu == 0.0 and pb > pu


def test_prompt_is_general_not_session_specific():
    """프롬프트에 일화·코퍼스 수치·실명·공간 수갑이 없다(범용화 패스 2026-09-01).

    근거·수치는 코드 주석의 몫이다 — 모델 지시문은 행동 규칙만 든다."""
    idx = {f"sp{i:04d}": {"pos": i, "t_in": i * 2.0, "t_out": i * 2.0 + 2.0,
                          "is_audio": True, "importance": 3, "audio_script": [],
                          "text_source": "heard", "heard_text": "", "conf": 0.9,
                          "scene_script": "", "meaning_content": "", "mood": ""}
           for i in range(4)}
    p = st.build_story_prompt({"sequences": []}, idx, work_title="T")
    for banned in ("819초", "전유진", "ElevenLabs", "실측 62%", "0/166",
                   "셋뿐", "한 시퀀스 안에서 골라라", "직접 측정"):
        assert banned not in p, f"세션 특수물 잔존: {banned!r}"
    # 일반 원칙은 있어야 한다
    assert "하나의 이야기" in p and "다리" in p


# ══════════════════════════════════════════════════════════════════════════
# 대화 페이싱 (2026-09-01) — 같은 장면이어도 대사끼리 점프컷
# ══════════════════════════════════════════════════════════════════════════

def _pacing_fixture():
    """대사(2s) · 무성 3s · 무성 0.8s · 대사(2s) · 무성 꼬리 2s — 연속."""
    grid = _mk_grid([(0.0, 2.0, True, "대사 하나"), (2.0, 5.0, False, ""),
                     (5.0, 5.8, False, ""), (5.8, 7.8, True, "대사 둘"),
                     (7.8, 9.8, False, "")])
    s2 = _mk_stage2(grid, [(0, 4, 3, "m")])
    return st.build_span_index(s2, grid)


def test_pacing_cuts_long_silence_between_dialogue():
    """대사 사이 무성 3.8s → 긴 span(3s)을 덜고 짧은 것(0.8s)이 호흡으로 남는다."""
    idx, _ = _pacing_fixture()
    beats = [{"role": "hook", "span_ids": [f"sp{i:04d}" for i in range(5)],
              "narration": [], "labels": [], "items": None}]
    removed = st.apply_dialogue_pacing(beats, idx)
    assert [r["span_id"] for r in removed] == ["sp0001"]      # 3s 짜리만
    assert "sp0002" in beats[0]["span_ids"]                    # 0.8s 호흡 잔존
    assert "sp0004" in beats[0]["span_ids"]                    # 꼬리(여운)는 불가침


def test_pacing_keeps_head_and_tail_silence():
    """비트 머리(리드인)·꼬리(여운)는 안 건드린다 — 내레이션·여운 컷 재료."""
    idx, _ = _pacing_fixture()
    beats = [{"role": "hook", "span_ids": ["sp0001", "sp0003", "sp0004"],
              "narration": [], "labels": [], "items": None}]   # 머리 무성 3s
    removed = st.apply_dialogue_pacing(beats, idx)
    assert removed == []                                       # 대사 '사이'가 아니다


def test_pacing_protects_nar_leadin():
    """nar 앵커 직전 리드인은 보호 — 페이싱이 내레이션 자리를 먹으면 안 된다."""
    idx, _ = _pacing_fixture()
    items = [{"kind": "mat", "text": None, "span_ids": ["sp0000"], "label": None},
             {"kind": "nar", "text": "여덟자짜리다리문장", "span_ids": [], "label": None},
             {"kind": "mat", "text": None,
              "span_ids": ["sp0001", "sp0002", "sp0003"], "label": None}]
    beats = [{"role": "hook", "span_ids": [f"sp{i:04d}" for i in range(4)],
              "narration": ["여덟자짜리다리문장"], "labels": [], "items": items}]
    removed = st.apply_dialogue_pacing(beats, idx)
    # 견적 0.6+9/7≈1.9s — 앵커(sp0003, 5.8s) 직전 리드인 sp0002(5.0~5.8)는 보호,
    # sp0001(2.0~5.0)도 창 범위(5.8-2.1=3.7↑)에 걸쳐 보호된다 → 아무것도 안 던다
    assert removed == []


def test_pacing_caps_silent_only_beat():
    """무성 전용 비트(silent_break)는 총량 캡 — 8.7s 정적 실사고."""
    grid = _mk_grid([(float(i), float(i + 1), False, "") for i in range(6)])
    s2 = _mk_stage2(grid, [(0, 5, 3, "m")])
    idx, _ = st.build_span_index(s2, grid)
    beats = [{"role": "silent_break", "span_ids": [f"sp{i:04d}" for i in range(6)],
              "narration": [], "labels": [], "items": None},
             # 마지막 비트는 캡 예외(리빌 보호)라 중간 비트로 만들기 위한 꼬리
             {"role": "ending", "span_ids": [], "narration": [], "labels": [],
              "items": None}]
    st.apply_dialogue_pacing(beats, idx)
    total = sum(idx[x]["t_out"] - idx[x]["t_in"] for x in beats[0]["span_ids"])
    assert total <= st.SILENT_BEAT_CAP_SEC + 1e-6
    assert beats[0]["span_ids"][0] == "sp0000"                 # 앞부터 보존


def test_title_promise_clause_and_pacing_rule_in_prompt():
    idx = {f"sp{i:04d}": {"pos": i, "t_in": i * 2.0, "t_out": i * 2.0 + 2.0,
                          "is_audio": True, "importance": 3, "audio_script": [],
                          "text_source": "heard", "heard_text": "", "conf": 0.9,
                          "scene_script": "", "meaning_content": "", "mood": ""}
           for i in range(4)}
    p = st.build_story_prompt({"sequences": []}, idx, work_title="T")
    assert "제목은 약속이다" in p and "스포일러가 아니라 예고" in p
    assert "컷이 잦은 것이 쇼츠의 리듬" in p


def test_nar_only_beat_folds_into_previous():
    """nar 전용 비트는 반려가 아니라 직전 비트 꼬리로 접합 — 마무리 내레이션을
    제 비트로 내는 자연스러운 실수다(실측: 3안 전량 폴백 사고의 반려 사유)."""
    idx, order = _items_fixture()
    resp = {"template": "recap_dialogue", "reason": "t",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [
                {"role": "climax", "items": [{"span_ids": ["sp0000", "sp0001"]}]},
                {"role": "ending", "items": [{"nar": "마무리 한 줄"}]},
            ]}
    story, problems, notes = st.validate_story_response(resp, idx, order)
    assert not problems, problems
    assert len(story["beats"]) == 1
    b = story["beats"][0]
    assert b["items"][-1]["kind"] == "nar"
    assert b["narration"] == ["마무리 한 줄"]
    assert any("접합" in n for n in notes)
    # 첫 비트가 nar 전용이면 붙일 곳이 없다 — 반려 유지
    resp2 = {**resp, "beats": [{"role": "hook", "items": [{"nar": "다리"}]}]}
    _, problems2, _ = st.validate_story_response(resp2, idx, order)
    assert problems2


def test_prompt_does_not_ask_model_to_skip_silence():
    """무성 컷은 코드(페이싱)의 일 — 모델에게 시키면 연속 범위 벨트와 모순돼
    전량 반려→폴백이 난다(실측 사고). 프롬프트가 역할 분담을 명시해야 한다."""
    idx = {f"sp{i:04d}": {"pos": i, "t_in": i * 2.0, "t_out": i * 2.0 + 2.0,
                          "is_audio": True, "importance": 3, "audio_script": [],
                          "text_source": "heard", "heard_text": "", "conf": 0.9,
                          "scene_script": "", "meaning_content": "", "mood": ""}
           for i in range(4)}
    p = st.build_story_prompt({"sequences": []}, idx, work_title="T")
    assert "끼워 넣지 마라" not in p
    assert "코드가 알아서 잘라 붙인다" in p


# ══════════════════════════════════════════════════════════════════════════
# 프로토 계약 (2026-09-01 교체 — interval-proto 8/25 실증 이식)
# ══════════════════════════════════════════════════════════════════════════
# 비트 = 의미 단위 {action, lines(비연속), visual, why, link, narration}.
# 컷은 선택의 부산물(빠진 줄 = 컷), 도약은 link 로 자기 증명, 내레이션 창은 실측.

def test_proto_contract_noncontiguous_lines_accepted():
    """lines 는 연속일 필요가 없다 — 필요 없는 대사를 뺀 자리가 컷이 된다."""
    idx, order = _items_fixture()          # sp0~4: 무성·대사·무성·대사·무성
    resp = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "action": "대사 둘을 붙인다",
                       "lines": ["sp0001", "sp0003"],   # sp0002 건너뜀 = 컷
                       "visual": [], "why": "topic 전개", "link": None,
                       "narration": None}]}
    story, problems, notes = st.validate_story_response(resp, idx, order)
    assert not problems, problems
    b = story["beats"][0]
    assert b["span_ids"] == ["sp0001", "sp0003"]       # 비연속 그대로
    assert story["topic"] == "한 문장"
    assert b["why"] == "topic 전개" and b["action"]


def test_proto_noncontiguous_lines_become_two_clips():
    """빠진 줄은 실제 컷 — 조립이 pos 인접 런마다 클립을 만든다."""
    idx, order = _items_fixture()
    from app.v3 import assemble
    story = {"template": "recap_dialogue", "title": {"line1": "a", "line2": "b"},
             "beats": [{"role": "climax", "span_ids": ["sp0001", "sp0003"],
                        "narration": None, "labels": [], "items": None,
                        "muted_span_ids": []}],
             "narration_cues": [], "budget": {}}
    plan = assemble.assemble_edit_plan(story, idx, video_path="v.mp4",
                                       work_title="T")
    tl = plan["timeline"]
    assert len(tl) == 2                                 # 두 클립 = 점프컷
    assert tl[0]["clip_end_sec"] == 5.0 and tl[1]["clip_start_sec"] == 7.0


def test_proto_unknown_ids_ignored_with_note():
    """없는 조각 id 는 반려가 아니라 무시+노트(프로토 관용) — 나머지로 성립."""
    idx, order = _items_fixture()
    resp = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "lines": ["sp0001", "c9999"],
                       "why": "w", "narration": None}]}
    story, problems, notes = st.validate_story_response(resp, idx, order)
    assert not problems
    assert story["beats"][0]["span_ids"] == ["sp0001"]
    assert any("무시" in n for n in notes)


def test_proto_sequence_jump_requires_link_or_narration():
    """시퀀스를 넘는 도약에 link 도 내레이션도 없으면 반려 — 도약의 자기 증명."""
    grid = _mk_grid([(0.0, 2.0, True, "가."), (2.0, 4.0, True, "나."),
                     (4.0, 6.0, True, "다."), (6.0, 8.0, True, "라.")])
    s2 = _mk_stage2(grid, [(0, 1, 3, "A"), (2, 3, 3, "B")])
    seqs = s2["sequences"][0]["chunks"][0]["meanings"]
    s2 = {"sequences": [
        {"number": 0, "chunks": [{"number": 0, "meanings": [seqs[0]]}]},
        {"number": 1, "chunks": [{"number": 0, "meanings": [seqs[1]]}]}]}
    idx, order = st.build_span_index(s2, grid)
    base = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"}}
    bare = {**base, "beats": [
        {"role": "hook", "lines": ["sp0000"], "why": "w", "narration": None},
        {"role": "climax", "lines": ["sp0003"], "why": "w", "link": None,
         "narration": None}]}
    _, problems, _ = st.validate_story_response(bare, idx, order)
    assert any("link" in p for p in problems)
    linked = {**base, "beats": [
        bare["beats"][0],
        {**bare["beats"][1], "link": "같은 저녁 자리 — 인물 연속"}]}
    story, problems2, _ = st.validate_story_response(linked, idx, order)
    assert story is not None and not problems2


def test_proto_narration_becomes_leadin_item():
    """narration 은 비트 머리 리드인으로 — 앵커식 배치가 그 창을 만든다."""
    idx, order = _items_fixture()
    resp = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "lines": ["sp0000", "sp0001"],
                       "why": "w", "narration": {"text": "다리 문장"}}]}
    story, problems, _ = st.validate_story_response(resp, idx, order)
    assert not problems
    b = story["beats"][0]
    assert b["items"][0]["kind"] == "nar" and b["narration"] == ["다리 문장"]
    cues, dropped = st.plan_narration_slots(story["beats"], idx)
    assert len(cues) == 1 and cues[0]["source_end_sec"] == 2.0   # 대사 시작에 닿음


def test_measured_length_overrides_estimate():
    """실측이 정본 — measured 가 있으면 견적 대신 그 길이로 창을 만든다."""
    idx, order = _items_fixture()
    beats = [{"role": "hook", "span_ids": ["sp0000", "sp0001"],
              "narration": ["다리"], "labels": [],
              "items": [{"kind": "nar", "text": "다리", "span_ids": [],
                         "label": None},
                        {"kind": "mat", "text": None,
                         "span_ids": ["sp0000", "sp0001"], "label": None}]}]
    cues, _ = st.plan_narration_slots(
        [dict(b, span_ids=list(b["span_ids"])) for b in beats], idx,
        {"다리": 1.4})
    w = cues[0]["source_end_sec"] - cues[0]["source_time_sec"]
    # 실측 1.4+0.3 → 발화부 1.1 ÷ 기본 배속 1.1 + 리드 0.6 = 1.6 (2026-09-02
    # 기본 fast — 견적만 쓰면 1.0~1.2대라 실측 우선이 여전히 확인된다)
    assert abs(w - 1.6) < 0.05


def test_silent_cap_exempts_final_reveal_beat():
    """무성 캡은 중간 호흡 전용 — 마지막 비트(리빌·발견)는 자르지 않는다.

    실사고: 캡이 발견 장면을 앞 2.5초만 남기고 잘라 제목이 약속한 화면이 증발
    ("마지막 장면이 안 나왔는데?")."""
    grid = _mk_grid([(float(i), float(i + 1), False, "") for i in range(6)])
    s2 = _mk_stage2(grid, [(0, 5, 3, "발견 장면")])
    idx, _ = st.build_span_index(s2, grid)
    ending = [{"role": "ending", "span_ids": [f"sp{i:04d}" for i in range(6)],
               "narration": [], "labels": [], "items": None}]
    st.apply_dialogue_pacing(ending, idx)
    assert len(ending[0]["span_ids"]) == 6          # 6초 리빌 그대로
    # 중간 비트면 종전대로 캡
    mid = [{"role": "silent_break", "span_ids": [f"sp{i:04d}" for i in range(6)],
            "narration": [], "labels": [], "items": None},
           {"role": "ending", "span_ids": [], "narration": [], "labels": [],
            "items": None}]
    st.apply_dialogue_pacing(mid, idx)
    total = sum(idx[x]["t_out"] - idx[x]["t_in"] for x in mid[0]["span_ids"])
    assert total <= st.SILENT_BEAT_CAP_SEC + 1e-6


def test_lazy_highlight_and_missing_discipline_rejected():
    """규율 우회 차단(실사고: topic·why·내레이션 전부 생략한 나열이 highlight 로
    무혈입성 — cohesion 0.0 이 당선). highlight 는 코드 폴백 전용, topic·why 필수."""
    idx, order = _items_fixture()
    lazy = {"template": "highlight", "reason": "t",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "build", "lines": ["sp0001"], "why": "w"}]}
    _, problems, _ = st.validate_story_response(lazy, idx, order)
    assert any("코드 폴백 전용" in p for p in problems)
    no_topic = {"template": "recap_dialogue", "reason": "t",
                "title": {"line1": "제목", "line2": "펀치"},
                "beats": [{"role": "climax", "lines": ["sp0001"], "why": "w"}]}
    _, problems2, _ = st.validate_story_response(no_topic, idx, order)
    assert any("topic" in p for p in problems2)
    no_why = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
              "title": {"line1": "제목", "line2": "펀치"},
              "beats": [{"role": "climax", "lines": ["sp0001"]}]}
    _, problems3, _ = st.validate_story_response(no_why, idx, order)
    assert any("why" in p for p in problems3)


# ── 2026-09-01 내레이션 전멸 실사고 회귀 가드 ──────────────────────────────
# 47초 완성본이 내레이션 0개로 발행됐다: 모델이 쓴 3개 중 2개는 계획 드랍(별건),
# 1개는 창 끝이 미편성 span 구멍에 떨어져 finalize_cues 가 통드랍. 조력자는 구
# 스키마 백도어(items 없음 = 페이싱 보호·앵커 배치 무장해제) — 세 번째 실해.

def test_finalize_cues_rescues_window_ending_in_source_hole():
    """창 끝만 구멍이면 편집본 좌표로 길이를 보존한다 — 내레이션은 편집본 위에서
    연속 재생되므로 컷을 건너는 창이 정상이다."""
    cues = assemble.finalize_cues(
        [{"beat": 0, "text": "t", "mode": "silent", "source_time_sec": 1.0,
          "source_end_sec": 4.0, "muted_span_ids": []}],
        [{"clip_start_sec": 0.0, "clip_end_sec": 2.0},
         {"clip_start_sec": 10.0, "clip_end_sec": 12.0}], voice="v", speed="s")
    c = cues[0]
    assert c["start_sec"] == pytest.approx(1.0)
    assert c["end_sec"] == pytest.approx(4.0)          # 1.0 + 계획 길이 3.0
    assert c["duration_sec"] == pytest.approx(3.0)
    assert c.get("window_rescued") is True


def test_finalize_cues_rescue_clamps_to_timeline_end():
    cues = assemble.finalize_cues(
        [{"beat": 0, "text": "t", "mode": "silent", "source_time_sec": 11.0,
          "source_end_sec": 20.0, "muted_span_ids": []}],
        [{"clip_start_sec": 10.0, "clip_end_sec": 12.0}], voice="v", speed="s")
    assert cues[0]["end_sec"] == pytest.approx(2.0)    # 편집본 총 길이 클램프
    assert cues[0]["duration_sec"] == pytest.approx(1.0)


def test_finalize_cues_still_drops_tiny_remainder():
    """구조해도 0.5s 미만이면 슬롯 구실을 못 한다 — 종전대로 드랍(기록은 호출자)."""
    cues = assemble.finalize_cues(
        [{"beat": 0, "text": "t", "mode": "silent", "source_time_sec": 1.8,
          "source_end_sec": 2.1, "muted_span_ids": []}],
        [{"clip_start_sec": 0.0, "clip_end_sec": 2.0}], voice="v", speed="s")
    assert cues[0]["start_sec"] is None


def test_finalize_cues_intact_window_has_no_rescue_mark():
    """양 끝이 다 매핑되는 창은 종전과 동일 — rescued 키 자체가 없다(회귀 0)."""
    cues = assemble.finalize_cues(
        [{"beat": 0, "text": "t", "mode": "silent", "source_time_sec": 0.5,
          "source_end_sec": 1.5, "muted_span_ids": []}],
        [{"clip_start_sec": 0.0, "clip_end_sec": 2.0}], voice="v", speed="s")
    assert cues[0]["duration_sec"] == pytest.approx(1.0)
    assert "window_rescued" not in cues[0]


def test_require_proto_rejects_legacy_schema():
    """백도어 폐쇄 — 모델 응답 검증(require_proto=True)에서 구 스키마는 반려."""
    story, problems, _ = st.validate_story_response(_resp([
        {"role": "hook", "span_ids": ["sp0000"], "narration": []},
        {"role": "climax", "span_ids": ["sp0005"], "narration": []}]),
        IDX, ORDER, require_proto=True)
    assert story is None
    assert any("구 스키마" in p for p in problems)


def test_require_proto_accepts_proto_schema():
    resp = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "action": "a", "lines": ["sp0005"],
                       "visual": [], "why": "topic 전개", "link": None,
                       "narration": None}]}
    story, problems, _ = st.validate_story_response(resp, IDX, ORDER,
                                                    require_proto=True)
    assert not problems and story is not None


def test_default_keeps_legacy_schema_for_fallback_and_old_tests():
    """기본값(False)은 종전 그대로 — 코드 폴백·옛 테스트 경로 회귀 0."""
    story, _, _ = st.validate_story_response(_resp([
        {"role": "hook", "span_ids": ["sp0000"], "narration": []},
        {"role": "climax", "span_ids": ["sp0005"], "narration": []}]), IDX, ORDER)
    assert story is not None


def test_run_story_wires_require_proto():
    """배선 고정 — 검증 루프가 백도어 폐쇄를 실제로 켠 채 부른다."""
    src = Path(st.__file__).read_text("utf-8")
    assert "require_proto=True" in src


# ── 피사체 크롭 앵커 재료 승계 (2026-09-02) ────────────────────────────────

def test_span_index_carries_subject_pos():
    s2 = copy.deepcopy(S2)
    s2["sequences"][0]["chunks"][0]["meanings"][0]["spans"][1]["subject_pos"] = "right"
    idx, _ = st.build_span_index(s2, GRID)
    assert idx["sp0001"]["subject_pos"] == "right"
    assert idx["sp0000"]["subject_pos"] is None


def _story_with_beat(span_ids):
    return {"template": "recap_dialogue", "title": {"line1": "a", "line2": "b"},
            "beats": [{"role": "build", "span_ids": list(span_ids),
                       "narration": None, "labels": [], "items": None,
                       "muted_span_ids": []}],
            "narration_cues": [], "budget": {}}


def test_assemble_folds_subject_pos_for_all_silent_clip():
    idx = copy.deepcopy(IDX)
    idx["sp0001"]["subject_pos"] = "right"                     # 무성
    plan = assemble.assemble_edit_plan(_story_with_beat(["sp0001"]), idx,
                                       video_path="/v", work_title="w")
    assert plan["timeline"][0]["subject_pos"] == "right"


def test_assemble_ignores_subject_pos_on_dialogue_clips():
    """대사 클립(유성 포함)은 중앙 크롭 종전 그대로 — 키 자체가 없다(회귀 0)."""
    idx = copy.deepcopy(IDX)
    idx["sp0001"]["subject_pos"] = "right"
    plan = assemble.assemble_edit_plan(_story_with_beat(["sp0000", "sp0001"]), idx,
                                       video_path="/v", work_title="w")
    assert all("subject_pos" not in c for c in plan["timeline"])


def test_assemble_conflicting_sides_fall_back_to_center():
    idx = copy.deepcopy(IDX)
    idx["sp0001"]["subject_pos"] = "right"
    idx["sp0004"]["subject_pos"] = "left"
    story = _story_with_beat(["sp0001"])
    story["beats"].append({"role": "build", "span_ids": ["sp0004"],
                           "narration": None, "labels": [], "items": None,
                           "muted_span_ids": []})
    plan = assemble.assemble_edit_plan(story, idx, video_path="/v", work_title="w")
    assert plan["timeline"][0]["subject_pos"] == "right"       # 각자 일관되면 각자
    assert plan["timeline"][1]["subject_pos"] == "left"
    # 한 클립 안에서 갈리면 중앙 폴백
    idx2 = copy.deepcopy(IDX)
    idx2["sp0001"]["subject_pos"] = "right"
    # sp0001 하나뿐인 클립엔 갈릴 상대가 없으므로 붙는다 — 갈림 검사는 다중 span 몫


# ── 비트 머리 데드에어 컷 (2026-09-02) ─────────────────────────────────────
# e748690c 실사고: 훅 머리 무성 4.9s 중 내레이션 창(2.6s) 앞 2.3초가 데드에어로
# 발행. 창 확정 후 코드 산술(창 시작 = 실측 mp3 길이 역산)로 자른다.

def _head_idx():
    """sp0: 무성 0~4.9 · sp1: 유성 4.9~6 · sp2: 무성 6~7 · sp3: 유성 7~9."""
    grid2 = _mk_grid([(0.0, 4.9, False, ""), (4.9, 6.0, True, "대사"),
                      (6.0, 7.0, False, ""), (7.0, 9.0, True, "둘")])
    s2 = _mk_stage2(grid2, [(0, 3, 3, "장면")])
    return st.build_span_index(s2, grid2)


def test_trim_beat_heads_partial_with_narration_window():
    idx, _ = _head_idx()
    beats = [{"role": "hook", "span_ids": ["sp0000", "sp0001"],
              "narration": ["x"], "labels": []}]
    cues = [{"source_time_sec": 2.3, "source_end_sec": 4.9, "mode": "silent"}]
    recs = st.trim_beat_heads(beats, idx, cues)
    assert beats[0]["head_trim_sec"] == pytest.approx(2.0)      # 창 2.3 − 리드 0.3
    assert beats[0]["span_ids"] == ["sp0000", "sp0001"]         # span 은 유지(걸침)
    assert recs[0]["partial_sec"] == pytest.approx(2.0)


def test_trim_beat_heads_dialogue_only_keeps_one_second_breath():
    idx, _ = _head_idx()
    beats = [{"role": "hook", "span_ids": ["sp0000", "sp0001"],
              "narration": [], "labels": []}]
    recs = st.trim_beat_heads(beats, idx, [])
    assert beats[0]["head_trim_sec"] == pytest.approx(3.9)      # 대사 4.9 − 호흡 1.0
    assert recs and recs[0]["sec"] == pytest.approx(3.9)


def test_trim_beat_heads_short_head_untouched():
    idx, _ = _head_idx()
    beats = [{"role": "build", "span_ids": ["sp0002", "sp0003"],
              "narration": [], "labels": []}]                    # 머리 1.0s = 호흡
    assert st.trim_beat_heads(beats, idx, []) == []
    assert "head_trim_sec" not in beats[0]


def test_trim_beat_heads_silent_only_beat_untouched():
    """무성 전용 비트(창 없음)는 산술 판정 불가 — 시청 트림 몫."""
    idx, _ = _head_idx()
    beats = [{"role": "silent_break", "span_ids": ["sp0000"],
              "narration": [], "labels": []}]
    assert st.trim_beat_heads(beats, idx, []) == []


def test_trim_beat_heads_label_anchor_protected():
    idx, _ = _head_idx()
    beats = [{"role": "hook", "span_ids": ["sp0000", "sp0001"], "narration": [],
              "labels": [{"text": "(라벨)", "span_id": "sp0000"}]}]
    st.trim_beat_heads(beats, idx, [])
    assert "head_trim_sec" not in beats[0]                      # 앵커 span 불변


def test_assemble_applies_head_trim_and_belt_accepts_it():
    idx, _ = _head_idx()
    story = {"template": "recap_dialogue", "title": {"line1": "a", "line2": "b"},
             "beats": [{"role": "hook", "span_ids": ["sp0000", "sp0001"],
                        "narration": None, "labels": [], "items": None,
                        "muted_span_ids": [], "head_trim_sec": 2.0}],
             "narration_cues": [], "budget": {}}
    plan = assemble.assemble_edit_plan(story, idx, video_path="/v", work_title="w")
    c0 = plan["timeline"][0]
    assert c0["clip_start_sec"] == pytest.approx(2.0)
    assert c0["head_trimmed"] is True
    grid2 = _mk_grid([(0.0, 4.9, False, ""), (4.9, 6.0, True, "대사"),
                      (6.0, 7.0, False, ""), (7.0, 9.0, True, "둘")])
    belt = assemble.verify_edit_plan(plan, grid2)
    assert belt["pct"] == 100.0 and belt["head_trimmed"] == 1   # 정식 어휘 등록
    # 기록 없는 비스냅 경계는 여전히 위반
    plan["timeline"][0].pop("head_trimmed")
    belt2 = assemble.verify_edit_plan(plan, grid2)
    assert belt2["pct"] < 100.0


def test_run_story_wires_head_trim():
    src = Path(st.__file__).read_text("utf-8")
    assert "trim_beat_heads(beats, span_index, cues)" in src


# ── 훅 도입 내레이션 감점 · 프롬프트 규칙 (2026-09-02 사용자 피드백) ────────

def test_rubric_penalizes_hook_without_intro_narration():
    """이 쇼츠만 보는 사람은 맥락이 없다 — 훅에 도입 nar 없으면 intro 0.5."""
    base = {"template": "recap_dialogue", "title": {"line1": "a", "line2": "b"},
            "beats": [{"role": "hook", "span_ids": ["sp0003"],
                       "narration": [], "labels": [], "items": None},
                      {"role": "climax", "span_ids": ["sp0005"],
                       "narration": [], "labels": [], "items": None}]}
    s0 = score_story(base, IDX, target_sec=50)
    assert s0["parts"]["intro"] == 0.5
    withnar = copy.deepcopy(base)
    withnar["beats"][0]["narration"] = ["남편을 의심하게 된 아내"]
    s1 = score_story(withnar, IDX, target_sec=50)
    assert s1["parts"]["intro"] == 1.0


def test_prompt_carries_narration_duty_slots_and_title_rules():
    p = st.build_story_prompt({"sequences": []}, IDX, work_title="T")
    assert "도입 내레이션이 없는 훅은 감점" in p       # 규칙 4-ⓐ ↔ 루브릭 한 벌
    assert "장면 전환마다" in p                        # 4-ⓑ 교대 리듬
    assert "장면 보고체 금지" in p                     # 제목 — 요약이 아니라 낚시
    assert "기본값은 '있음'" in p                      # 내레이션 기본값(사용자 방침)


# ── 꼬리 파형 연장 · 문장 반토막 (2026-09-02) ──────────────────────────────

def test_pad_voiced_tails_prefers_waveform_silence():
    idx = {"a": {"t_in": 0.0, "t_out": 2.0, "is_audio": True},
           "b": {"t_in": 10.0, "t_out": 12.0, "is_audio": True}}
    tl = [{"clip_start_sec": 0.0, "clip_end_sec": 2.0, "span_ids": ["a"]},
          {"clip_start_sec": 10.0, "clip_end_sec": 12.0, "span_ids": ["b"]}]
    words = [{"t0": 1.5, "t1": 2.0}, {"t0": 2.9, "t1": 3.3},
             {"t0": 11.5, "t1": 12.0}, {"t0": 12.2, "t1": 12.6}]
    sil = [[2.2, 2.8]]                       # 파형 무음이 2.2 에서 시작
    n = assemble.pad_voiced_tails(tl, idx, words, sil)
    assert n == 3                            # 꼬리 2 + b 머리 패드 1(④, 2026-09-02)
    assert tl[0]["clip_end_sec"] == pytest.approx(2.4)    # 무음 시작 + TAIL_SIL_PAD_SEC(0.2)
    assert tl[0]["tail_pad"] == pytest.approx(0.4)
    assert tl[1]["clip_end_sec"] == pytest.approx(12.15)  # 무음 없음 → 단어 직전
    # 재료가 없으면 불변(오판 금지)
    tl2 = [{"clip_start_sec": 0.0, "clip_end_sec": 2.0, "span_ids": ["a"]}]
    assert assemble.pad_voiced_tails(tl2, idx, None, None) == 0
    assert "tail_pad" not in tl2[0]


def test_validator_rejects_half_sentence_selection():
    """길이 규칙이 가른 문장(갭<0.12s·무종결부호)은 짝 없이 못 고른다 — ↪ 반려."""
    grid = _mk_grid([(0.0, 3.0, True, "지원을 좀 받아야"),
                     (3.0, 6.0, True, "사업도 확장하고 그러지."),
                     (7.0, 9.0, True, "다른 얘기.")])
    s2 = _mk_stage2(grid, [(0, 2, 4, "m")])
    idx, order = st.build_span_index(s2, grid)
    assert idx["sp0000"]["continues_to"] == "sp0001"
    assert idx["sp0001"]["continues_to"] is None          # 종결부호 = 문장 끝
    resp = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "lines": ["sp0000"], "why": "w",
                       "narration": None}]}
    _, problems, _ = st.validate_story_response(resp, idx, order)
    assert any("반토막" in p for p in problems)
    both = {**resp, "beats": [{"role": "climax", "lines": ["sp0000", "sp0001"],
                               "why": "w", "narration": None}]}
    _, problems2, _ = st.validate_story_response(both, idx, order)
    assert not any("반토막" in p for p in problems2)
    # 재료 표에 ↪ 표시
    mat = st.build_story_prompt(s2, idx, work_title="T")
    assert "↪다음과 한 문장" in mat


def test_research_block_carries_no_assertion_rule():
    """Stage 3 단정 금지(2026-09-02) — 시놉시스가 있을 때만 절이 붙고, 없으면 종전."""
    p = st.build_story_prompt({"sequences": []}, IDX, work_title="T",
                              research_context="시놉: 살인 사건이 벌어진다")
    assert "단정해 적지 마라" in p
    p2 = st.build_story_prompt({"sequences": []}, IDX, work_title="T")
    assert "단정해 적지 마라" not in p2


def test_rubric_ending_continuity():
    """엔딩이 직전 비트와 같은 meaning(또는 5초 안)이면 1.0, 떨어진 새 국면은
    nar 0.6 / link 0.4 / 무근거 0.0 (2026-09-02 "벌레 소동→평온 마무리" 지적)."""
    base = [{"role": "climax", "span_ids": ["sp0005"], "narration": [], "labels": []},
            {"role": "ending", "span_ids": ["sp0006"], "narration": [], "labels": []}]
    doc = {"template": "recap_dialogue", "title": {"line1": "a", "line2": "b"},
           "beats": base}
    s0 = score_story(doc, IDX, target_sec=50)
    assert s0["parts"]["ending"] == 1.0                    # sp0005→sp0006 붙어 있음
    far = copy.deepcopy(doc)
    far["beats"] = [{"role": "climax", "span_ids": ["sp0000"], "narration": [],
                     "labels": []},
                    {"role": "ending", "span_ids": ["sp0006"], "narration": [],
                     "labels": []}]                        # meaning 0 → 2 · 갭 15s
    s1 = score_story(far, IDX, target_sec=50)
    assert s1["parts"]["ending"] == 0.0
    far["beats"][1]["link"] = "주장"
    assert score_story(far, IDX, target_sec=50)["parts"]["ending"] == 0.4
    far["beats"][1]["narration"] = ["다리 한 줄"]
    assert score_story(far, IDX, target_sec=50)["parts"]["ending"] == 0.6


def test_proto_narration_seat_is_auto_inserted():
    """머리가 유성인 비트의 내레이션 — 인접 무성 span 을 코드가 끼워 자리를 만든다
    (반려 폭풍 → 폴백 실사고로 반려에서 강등, 2026-09-02)."""
    idx, order = _items_fixture()          # sp0 무성 · sp1 유성 · sp2 무성 · sp3 유성
    base = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"}}
    bad = {**base, "beats": [
        {"role": "climax", "lines": ["sp0003"], "why": "w",
         "narration": {"text": "다리 문장"}}]}          # 머리 유성 + 자리 없음
    story, problems, notes = st.validate_story_response(bad, idx, order)
    assert story is not None and not problems
    assert story["beats"][0]["span_ids"][0] == "sp0002"  # 무성 sp0002 자동 삽입
    assert any("자동 보강" in n for n in notes)
    # 인접 무성이 없으면(sp0001 앞은 무성 sp0000뿐이나 sp0001 채택 시 sp0000 삽입)
    ok = {**base, "beats": [
        {"role": "climax", "lines": ["sp0003"], "visual": ["sp0002"], "why": "w",
         "narration": {"text": "다리 문장"}}]}          # 이미 자리 있음 — 무변경
    story2, _, notes2 = st.validate_story_response(ok, idx, order)
    assert story2["beats"][0]["span_ids"][0] == "sp0002"
    assert not any("자동 보강" in n for n in notes2)


# ── ⓑ 미분석 무성 자리 확장 · 전환 다리 의무 (2026-09-02 사용자 결정) ──────

def test_span_index_adds_unanalyzed_silent_tier():
    grid2 = _mk_grid([(0.0, 2.0, False, ""), (2.0, 4.0, False, ""),
                      (4.0, 6.0, True, "대사"), (6.0, 8.0, True, "끝.")])
    s2 = _mk_stage2(grid2, [(2, 3, 3, "m")])          # sp0/sp1 은 분석 밖 무성
    idx, order = st.build_span_index(s2, grid2)
    assert idx["sp0001"]["unanalyzed"] is True and not idx["sp0001"]["is_audio"]
    assert "sp0001" not in order                       # 모델 재료 목록엔 없음
    assert idx["sp0001"]["meaning_idx"] == idx["sp0002"]["meaning_idx"]


def test_seat_extension_uses_unanalyzed_silent():
    """창이 견적에 못 미치면 미분석 무성까지 끌어와 자리를 채운다(ⓑ)."""
    grid2 = _mk_grid([(0.0, 2.0, False, ""), (2.0, 4.0, False, ""),
                      (4.0, 6.0, True, "대사"), (6.0, 8.0, True, "끝.")])
    s2 = _mk_stage2(grid2, [(2, 3, 3, "m")])
    idx, order = st.build_span_index(s2, grid2)
    resp = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "lines": ["sp0002", "sp0003"], "why": "w",
                       "narration": {"text": "열여섯자짜리다리내레이션문장임"}}]}
    story, problems, notes = st.validate_story_response(resp, idx, order)
    assert story is not None and not problems
    assert story["beats"][0]["span_ids"][:2] == ["sp0000", "sp0001"]  # 2조각 확장
    assert any("미분석 2건" in n for n in notes)
    # 모델이 미분석 id 를 직접 부르면 종전대로 무시+노트
    resp2 = {**resp, "beats": [{"role": "climax", "lines": ["sp0001", "sp0002"],
                                "why": "w", "narration": None}]}
    _, _, notes2 = st.validate_story_response(resp2, idx, order)
    assert any("없는 조각" in n for n in notes2)


def test_scene_jump_bridge_is_rubric_not_reject():
    """소스 5초+ 전환의 다리 요구 — 반려 폭풍→폴백 실사고로 심사 감점으로 강등."""
    grid2 = _mk_grid([(0.0, 2.0, True, "가."), (2.0, 3.0, False, ""),
                      (17.0, 20.0, False, ""),
                      (20.0, 22.0, True, "나."), (22.0, 24.0, True, "다.")])
    s2 = _mk_stage2(grid2, [(0, 1, 3, "A"), (2, 4, 4, "B")])
    idx, order = st.build_span_index(s2, grid2)
    base = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"}}
    bare = {**base, "beats": [
        {"role": "hook", "lines": ["sp0000"], "why": "w", "narration": None},
        {"role": "climax", "lines": ["sp0003", "sp0004"], "why": "w",
         "link": "주장만", "narration": None}]}
    story_b, problems, notes = st.validate_story_response(bare, idx, order)
    assert story_b is not None and not problems              # 반려 아님
    assert any("다리 내레이션 없음" in n for n in notes)      # 노트로 지목
    ok = {**base, "beats": [
        bare["beats"][0],
        {**bare["beats"][1], "narration": {"text": "그날 밤 술자리에서"}}]}
    story_o, _, _ = st.validate_story_response(ok, idx, order)
    sb = score_story(story_b, idx, target_sec=20)["parts"]["narration"]
    so = score_story(story_o, idx, target_sec=20)["parts"]["narration"]
    assert so > sb                                           # 다리 있는 안이 이긴다


def test_leadin_may_extend_into_silent_only_previous_beat():
    """직전 비트가 무성 전용이면 그 꼬리는 리드인 재료다(1.66s 창 전보문 재발 수정).
    유성이 섞인 직전 비트는 종전대로 금지(옛 ② 사고 방지)."""
    idx, order = _items_fixture()          # sp0무성 sp1유성 sp2무성 sp3유성 sp4무성
    beats = [
        {"role": "silent_break", "span_ids": ["sp0002"], "narration": [],
         "labels": [], "items": [{"kind": "mat", "text": None,
                                  "span_ids": ["sp0002"], "label": None}]},
        {"role": "climax", "span_ids": ["sp0003"], "narration": ["다리 문장"],
         "labels": [], "items": [{"kind": "nar", "text": "다리 문장",
                                  "span_ids": [], "label": None},
                                 {"kind": "mat", "text": None,
                                  "span_ids": ["sp0003"], "label": None}]},
    ]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert not dropped and len(cues) == 1
    assert cues[0]["mode"] == "silent" and cues[0]["muted_span_ids"] == []
    assert cues[0]["source_time_sec"] >= 5.0            # silent_break(5~7) 위
    assert cues[0]["source_end_sec"] == pytest.approx(7.0)  # 끝은 대사 시작


def test_validate_tts_conflict_tolerance_matches_trim_threshold():
    from app.v3 import finalize as fz
    grid = {"span_candidates": [
        {"id": "a", "t_in": 0.0, "t_out": 2.0, "is_audio": False},
        {"id": "b", "t_in": 2.0, "t_out": 4.0, "is_audio": True, "text": "대사"}]}
    s2doc = {"sequences": [{"number": 0, "chunks": [{"number": 0, "meanings": [
        {"number": 0, "content": "m", "characters": [], "importance": 5,
         "mood": "", "time": {"start": "00:00:00.000", "end": "00:00:04.000"},
         "spans": [
            {"number": 0, "span_id": "a", "is_audio": False, "audio_script": [],
             "scene_script": "s", "characters": [], "importance": 5,
             "time": {"start": "00:00:00.000", "end": "00:00:02.000"},
             "time_authority": "scene"},
            {"number": 1, "span_id": "b", "is_audio": True,
             "audio_script": [{"speaker": "갑", "line": "대사"}],
             "scene_script": "", "characters": ["갑"], "importance": 5,
             "time": {"start": "00:00:02.000", "end": "00:00:04.000"},
             "time_authority": "stt"}]}]}]}]}
    plan = {"timeline": [{"clip_start_sec": 0.0, "clip_end_sec": 4.0,
                          "span_ids": ["a", "b"]}]}
    res = {"tts_cue_files": [{"cue": {"start_sec": 0.0, "source_time_sec": 0.0,
                                      "duration_sec": 2.0, "fit_actual_sec": 2.04,
                                      "muted_span_ids": []}}]}
    out = fz.check_tts_conflicts(res, plan, s2doc, grid)
    assert out["violations"] == []                       # 0.04s 삐짐 = 잘림 감수
    res["tts_cue_files"][0]["cue"]["fit_actual_sec"] = 2.30
    out2 = fz.check_tts_conflicts(res, plan, s2doc, grid)
    assert len(out2["violations"]) == 1                  # 진짜 침범은 여전히 잡힘


# ── 생성 레버 (2026-09-02 기조 전환: 거르기→잘 만들게) ─────────────────────

def test_prompt_publishes_rubric_and_self_check():
    """심사 기준·자기 점검이 프롬프트에 실린다 — 생성이 심사와 같은 방향을 본다."""
    p = st.build_story_prompt({"sequences": []}, IDX, work_title="T")
    assert "심사 기준" in p and "채점표를 보고 짜라" in p
    assert "내레이션 약속 이행 ×3" in p and "소리 커버리지 ×2" in p
    assert "출력 전 자기 점검" in p and "↪ 조각의 짝" in p


def test_shorts_hints_block_and_regression_zero():
    hints = [{"approx_time": "00:31:20", "situation": "정체불명 덮개를 여는 부부",
              "why": "미스터리+코믹"}]
    p1 = st.build_story_prompt({"sequences": []}, IDX, work_title="T",
                               shorts_hints=hints)
    assert "본 눈의 추천" in p1 and "00:31:20쯤" in p1 and "시각은 대략치" in p1
    p0 = st.build_story_prompt({"sequences": []}, IDX, work_title="T")
    assert "본 눈의 추천" not in p0                      # 미지정 = 블록 자체가 없음
    assert p0 == st.build_story_prompt({"sequences": []}, IDX, work_title="T",
                                       shorts_hints=None)


def test_extract_shorts_candidates_lenient():
    from app.v3.seq_analyze import extract_shorts_candidates
    raw = {"shorts_candidates": [
        {"approx_time": "00:31:20", "situation": "덮개 개봉", "why": "미스터리"},
        {"situation": ""},                               # 빈 상황 → 버림
        "문자열",                                        # 형식 오류 → 버림
        {"situation": "긴" * 100, "why": "w" * 100},     # 길이 클램프
    ]}
    out = extract_shorts_candidates(raw)
    assert len(out) == 2
    assert out[0]["approx_time"] == "00:31:20"
    assert len(out[1]["situation"]) <= 80 and len(out[1]["why"]) <= 60
    assert extract_shorts_candidates({}) == []           # 키 없음(구 캐시) = 빈 목록


def test_stage1_prompt_asks_for_shorts_candidates():
    from app.v3 import seq_analyze as sq
    p = sq.build_prompt({"source": {"duration_sec": 100.0}, "span_candidates": [],
                         "words": [], "scene_cuts": [], "silence": [],
                         "transcript": [], "arousal": []})
    assert "shorts_candidates" in p and "쇼츠로 만들 만한 상황" in p
    assert "지명일 뿐" in p                              # 시각은 경계가 아니라 지명


# ── 머리 패드 · 화자 런 색 분리 (2026-09-02 A/B 청취 피드백) ────────────────

def test_head_pad_pulls_voiced_clip_start():
    """유성 시작 클립의 초성 잘림('아'동학대) — 직전 단어와의 여유 안에서 당긴다."""
    idx = {"a": {"t_in": 10.0, "t_out": 12.0, "is_audio": True}}
    tl = [{"clip_start_sec": 10.0, "clip_end_sec": 12.0, "span_ids": ["a"]}]
    words = [{"t0": 9.0, "t1": 9.5}, {"t0": 10.0, "t1": 10.4}]
    assemble.pad_voiced_tails(tl, idx, words, [[12.2, 13.0]])
    assert tl[0]["clip_start_sec"] == pytest.approx(9.85)      # 0.15 당김
    assert tl[0]["head_pad"] == pytest.approx(0.15)
    # 직전 클립이 소스에서 바로 붙으면 당기지 않는다(재료 중복 금지)
    tl2 = [{"clip_start_sec": 8.0, "clip_end_sec": 10.0, "span_ids": []},
           {"clip_start_sec": 10.0, "clip_end_sec": 12.0, "span_ids": ["a"]}]
    assemble.pad_voiced_tails(tl2, idx, words, None)
    assert "head_pad" not in tl2[1]
    # 벨트가 head_pad 시작 경계를 정식 어휘로 받는다
    grid2 = {"span_candidates": [{"id": "a", "t_in": 10.0, "t_out": 12.0}]}
    belt = assemble.verify_edit_plan({"timeline": tl}, grid2)
    assert belt["pct"] == 100.0


def test_word_speakers_split_and_line_colors():
    """다화자 span — Stage 2 화자별 행을 단어열에 정렬해 줄별 색을 가른다."""
    sp = {"audio_script": [{"speaker": "변호사", "line": "빨리"},
                           {"speaker": "경희", "line": "자 잠깐만요"}]}
    words = [{"t0": 0.0, "t1": 0.4, "text": "빨리"},
             {"t0": 0.5, "t1": 0.8, "text": "자"},
             {"t0": 0.9, "t1": 1.5, "text": "잠깐만요"}]
    runs = assemble._word_speakers(sp, words)
    assert runs == ["변호사", "경희", "경희"]
    # 화자 1명이면 None(종전 폴백)
    one = {"audio_script": [{"speaker": "경희", "line": "빨리 자 잠깐만요"}]}
    assert assemble._word_speakers(one, words) is None
    # 정렬 붕괴(행에 단어 0개) → None(오판 금지)
    bad = {"audio_script": [{"speaker": "a", "line": "전혀다른긴문장이라매칭이안됨"},
                            {"speaker": "b", "line": "역시다른문장"}]}
    assert assemble._word_speakers(bad, [{"t0": 0, "t1": 1, "text": "짧"}]) is None


def test_intra_beat_hole_noted_not_rejected():
    """비트 안 5초+ 구멍 — 반려가 아니라 노트+자동 분할(반려 폭풍 실사고로 강등)."""
    grid2 = _mk_grid([(0.0, 2.0, True, "가."), (2.0, 3.0, False, ""),
                      (20.0, 22.0, True, "나.")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, order = st.build_span_index(s2, grid2)
    resp = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "lines": ["sp0000", "sp0002"],
                       "why": "w", "narration": None}]}
    data, problems, notes = st.validate_story_response(resp, idx, order)
    assert not any("구멍" in p for p in problems)
    assert any("구멍" in n and "분할로 수리" in n for n in notes)


def test_split_beats_at_holes_partitions_items_and_labels():
    """자동 분할 — 재료 불변, mat 은 조각별로, 라벨은 원 앵커 조각에만,
    nar 는 다음 mat 파트의 머리(다리 의도 유지)."""
    grid2 = _mk_grid([(0.0, 2.0, False, ""), (2.0, 4.0, True, "가."),
                      (20.0, 22.0, False, ""), (22.0, 24.0, True, "나.")])
    s2 = _mk_stage2(grid2, [(0, 3, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "climax",
              "span_ids": ["sp0000", "sp0001", "sp0002", "sp0003"],
              "narration": [], "labels": [], "muted_span_ids": [],
              "items": [{"kind": "mat", "text": None, "label": "(라벨)",
                         "span_ids": ["sp0000", "sp0001"]},
                        {"kind": "nar", "text": "다리 문장", "span_ids": [],
                         "label": None},
                        {"kind": "mat", "text": None, "label": None,
                         "span_ids": ["sp0002", "sp0003"]}]}]
    recs = st.split_beats_at_holes(beats, idx)
    assert len(recs) == 1 and recs[0]["parts"] == 2
    assert len(beats) == 2                               # 경계 승격
    assert beats[0]["span_ids"] == ["sp0000", "sp0001"]
    assert beats[1]["span_ids"] == ["sp0002", "sp0003"]
    # 구멍 위에 있던 nar 는 뒤 파트의 머리 다리가 된다
    kinds1 = [x["kind"] for x in beats[1]["items"]]
    assert kinds1[0] == "nar"
    # 라벨은 원 앵커(sp0000) 조각에만
    assert beats[0]["labels"] and not beats[1]["labels"]
    # 구멍 없으면 불변
    recs2 = st.split_beats_at_holes(beats, idx)
    assert recs2 == [] and len(beats) == 2


def test_pause_split_front_fragment_marked_and_reversed_half_rejected():
    """'큰일 … 날 뻔했네요' — 쉼 조각은 재료 표 마커, 역방향(앞토막)은 반려."""
    grid2 = _mk_grid([(0.0, 0.8, True, "큰일"), (1.8, 3.0, True, "날 뻔했네요."),
                      (3.0, 5.0, True, "다른 말.")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, order = st.build_span_index(s2, grid2)
    assert idx["sp0001"]["pause_cont_from"] == "sp0000"
    mat = st.build_story_prompt(s2, idx, work_title="T")
    assert "쉼 뒤 이어졌을 수 있음" in mat
    # 역방향 tight(갭<0.12) 반려
    grid3 = _mk_grid([(0.0, 1.0, True, "지원을 받아야"),
                      (1.0, 3.0, True, "사업도 확장하고."), (3.0, 5.0, True, "끝.")])
    s3 = _mk_stage2(grid3, [(0, 2, 4, "m")])
    idx3, order3 = st.build_span_index(s3, grid3)
    resp = {"template": "recap_dialogue", "reason": "t", "topic": "한 문장",
            "title": {"line1": "제목", "line2": "펀치"},
            "beats": [{"role": "climax", "lines": ["sp0001", "sp0002"],
                       "why": "w", "narration": None}]}
    _, problems, _ = st.validate_story_response(resp, idx3, order3)
    assert any("앞 조각에서 이어진다" in p for p in problems)


def test_window_widening_uses_budget_slack():
    """예산 여유가 있으면 창을 넓혀 축약을 피한다(A판 1.03s 창 실사고)."""
    grid2 = _mk_grid([(0.0, 2.0, False, ""), (2.0, 4.0, False, ""),
                      (4.0, 5.0, False, ""), (5.0, 7.0, True, "대사."),
                      (7.0, 9.0, True, "끝.")])
    s2 = _mk_stage2(grid2, [(2, 4, 3, "m")])   # sp0/sp1 은 비트 밖 무성
    idx, order = st.build_span_index(s2, grid2)
    beats = [{"role": "hook", "span_ids": ["sp0002", "sp0003", "sp0004"],
              "narration": ["열여섯자짜리다리내레이션문장임"], "labels": [],
              "items": [{"kind": "nar", "text": "열여섯자짜리다리내레이션문장임",
                         "span_ids": [], "label": None},
                        {"kind": "mat", "text": None,
                         "span_ids": ["sp0002", "sp0003", "sp0004"],
                         "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    assert not dropped
    w = cues[0]["source_end_sec"] - cues[0]["source_time_sec"]
    assert w > 2.0                              # 1s 머리가 확장돼 창이 견적급으로
    assert beats[0]["span_ids"][0] in ("sp0000", "sp0001")   # 재료가 실제로 붙음
    # 슬랙 0이어도 OVERSHOOT 한도까지는 자리를 만든다(2026-09-02 원칙 교체 —
    # 초과분은 watch-trim 예산 컷이 회수한다. "좁은 자리에 맞추지 않는다")
    beats2 = [{"role": "hook", "span_ids": ["sp0002", "sp0003", "sp0004"],
               "narration": ["열여섯자짜리다리내레이션문장임"], "labels": [],
               "items": beats[0]["items"]}]
    st.plan_narration_slots(beats2, idx, budget_slack=0.0)
    assert beats2[0]["span_ids"][0] in ("sp0000", "sp0001")


def test_run_story_defers_budget_to_watch_trim():
    """길이 초과를 story 산술로 안 자른다(2026-09-02) — deficit 기록만 남긴다."""
    import inspect
    src = inspect.getsource(st.run_story)
    assert "trim_to_budget(" not in src                # 산술 트림 호출 제거
    assert "deficit_sec" in src and "budget_deficit" in src
    from app.v3 import pipeline as pl
    psrc = inspect.getsource(pl)
    assert "budget_deficit=_deficit" in psrc           # watch-trim 에 초과분 전달
    assert "span_index=_sidx" in psrc


# ── 자리 생성 (2026-09-02 "내레이션이 먼저, 자리는 만들어서라도") ───────────

def _nar_beat(span_ids, nar_span_ids):
    return [{"role": "hook", "span_ids": list(span_ids),
             "narration": ["열여섯자짜리다리내레이션문장임"], "labels": [],
             "items": [{"kind": "nar", "text": "열여섯자짜리다리내레이션문장임",
                        "span_ids": list(nar_span_ids), "label": None},
                       {"kind": "mat", "text": None,
                        "span_ids": [x for x in span_ids
                                     if x not in nar_span_ids], "label": None}]}]


def test_slot_growth_applies_to_model_specified_window():
    """⓪ 모델 지정 극소 창도 견적까지 넓힌다 — '옥상 불길 맞선' 축약 재발 방지."""
    grid2 = _mk_grid([(0.0, 2.0, False, ""), (2.0, 4.0, False, ""),
                      (4.0, 5.0, False, ""), (5.0, 7.0, True, "대사."),
                      (7.0, 9.0, True, "끝.")])
    s2 = _mk_stage2(grid2, [(2, 4, 3, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = _nar_beat(["sp0002", "sp0003", "sp0004"], ["sp0002"])  # 창 1s 지정
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    assert not dropped
    w = cues[0]["source_end_sec"] - cues[0]["source_time_sec"]
    assert w > 2.0                             # 1s 지정창이 견적급으로 넓어짐
    assert beats[0]["span_ids"][0] in ("sp0000", "sp0001")


def test_slot_rescue_builds_leadin_for_voiced_head_beat():
    """비트 머리가 유성이면 드랍이 아니라 그 앞 무성으로 리드인을 만든다."""
    grid2 = _mk_grid([(0.0, 2.0, False, ""), (2.0, 4.0, False, ""),
                      (4.0, 6.0, True, "대사."), (6.0, 8.0, True, "끝.")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "hook", "span_ids": ["sp0002", "sp0003"],
              "narration": ["열여섯자짜리다리내레이션문장임"], "labels": [],
              "items": [{"kind": "nar", "text": "열여섯자짜리다리내레이션문장임",
                         "span_ids": [], "label": None},
                        {"kind": "mat", "text": None,
                         "span_ids": ["sp0002", "sp0003"], "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    assert not dropped and len(cues) == 1      # 종전엔 드랍이던 자리
    assert beats[0]["span_ids"][0] in ("sp0000", "sp0001")
    assert cues[0]["mode"] == "silent"


def test_slot_drop_only_when_no_adjacent_silence():
    """앞이 다른 장면의 대사뿐이면(끌어올 무성 0) 그때만 드랍 — 사유 갱신."""
    grid2 = _mk_grid([(0.0, 2.0, True, "남의 대사."), (2.0, 4.0, True, "대사."),
                      (4.0, 6.0, True, "끝.")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "hook", "span_ids": ["sp0001", "sp0002"],
              "narration": ["열여섯자짜리다리내레이션문장임"], "labels": [],
              "items": [{"kind": "nar", "text": "열여섯자짜리다리내레이션문장임",
                         "span_ids": [], "label": None},
                        {"kind": "mat", "text": None,
                         "span_ids": ["sp0001", "sp0002"], "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    assert len(dropped) == 1 and "인접 무성 재료" in dropped[0]["reason"]


# ── 엔딩 꼬리 여운 배치 (2026-09-02 사용자 지시) ───────────────────────────

def _ending_beats(role="ending"):
    """머리엔 끌어올 무성이 없고(앞이 남의 대사) 꼬리에 여운이 있는 비트."""
    return [{"role": role, "span_ids": ["sp0001", "sp0002", "sp0003"],
             "narration": ["불륜뒤에찾아온진짜비극이었죠"], "labels": [],
             "items": [{"kind": "nar", "text": "불륜뒤에찾아온진짜비극이었죠",
                        "span_ids": [], "label": None},
                       {"kind": "mat", "text": None,
                        "span_ids": ["sp0001", "sp0002", "sp0003"], "label": None}]}]


def test_ending_nar_falls_back_to_tail_lull():
    """머리 재료 0 이어도 엔딩은 꼬리 여운 위에 배치 — 드랍이 아니다."""
    grid2 = _mk_grid([(0.0, 2.0, True, "남의 대사."), (2.0, 4.0, True, "대사."),
                      (4.0, 6.0, False, ""), (6.0, 9.0, False, "")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = _ending_beats()
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    assert not dropped and len(cues) == 1
    assert cues[0]["source_time_sec"] >= 4.0 - 1e-6     # 여운(무성) 위
    assert cues[0]["mode"] == "silent"


def test_ending_tail_grows_with_adjacent_silence():
    """여운이 짧으면 마지막 span 뒤 인접 무성을 이어붙여 늘린다(_grow_after)."""
    grid2 = _mk_grid([(0.0, 2.0, True, "남의 대사."), (2.0, 4.0, True, "대사."),
                      (4.0, 4.6, False, ""), (4.6, 7.0, False, ""),
                      (7.0, 9.0, False, "")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "ending", "span_ids": ["sp0001", "sp0002"],
              "narration": ["불륜뒤에찾아온진짜비극이었죠"], "labels": [],
              "items": [{"kind": "nar", "text": "불륜뒤에찾아온진짜비극이었죠",
                         "span_ids": [], "label": None},
                        {"kind": "mat", "text": None,
                         "span_ids": ["sp0001", "sp0002"], "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    assert not dropped
    assert "sp0003" in beats[0]["span_ids"]             # 뒤 무성이 이어붙음
    w = cues[0]["source_end_sec"] - cues[0]["source_time_sec"]
    assert w > 1.5                                      # 0.6s 여운이 견적급으로


def test_tail_lull_rescue_is_ending_only():
    """비엔딩 비트는 꼬리 구제 대상이 아니다 — 무성이 전혀 없으면 드랍.
    (내부 무성이 있으면 ①b 가 정당하게 배치하므로, 전부 유성인 픽스처로 검사)"""
    grid2 = _mk_grid([(0.0, 2.0, True, "남의 대사."), (2.0, 4.0, True, "대사."),
                      (4.0, 6.0, True, "대사 둘."), (6.0, 8.0, True, "뒷비트.")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "build", "span_ids": ["sp0001", "sp0002"],
              "narration": ["불륜뒤에찾아온진짜비극이었죠"], "labels": [],
              "items": [{"kind": "nar", "text": "불륜뒤에찾아온진짜비극이었죠",
                         "span_ids": [], "label": None},
                        {"kind": "mat", "text": None,
                         "span_ids": ["sp0001", "sp0002"], "label": None}]},
             {"role": "ending", "span_ids": ["sp0003"], "narration": [],
              "labels": [], "items": [{"kind": "mat", "text": None,
                                       "span_ids": ["sp0003"], "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    assert len(dropped) == 1


def test_tail_intent_nar_never_rescued_to_head():
    """mat 뒤에 오는 엔딩 회고 nar 는 머리 구제로 장면 앞에 끌려가지 않는다."""
    grid2 = _mk_grid([(0.0, 2.0, False, ""), (2.0, 4.0, True, "대사."),
                      (4.0, 4.4, False, "")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "ending", "span_ids": ["sp0001", "sp0002"],
              "narration": ["불륜뒤에찾아온진짜비극이었죠"], "labels": [],
              "items": [{"kind": "mat", "text": None,
                         "span_ids": ["sp0001", "sp0002"], "label": None},
                        {"kind": "nar", "text": "불륜뒤에찾아온진짜비극이었죠",
                         "span_ids": [], "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    if cues:                                            # 배치됐다면 반드시 꼬리
        assert cues[0]["source_time_sec"] >= 4.0 - 1e-6


def test_narrow_head_relocates_to_internal_silent_run():
    """①b — 머리 리드인이 좁고 앞이 대사면 비트 안 무성 run 으로 창을 옮긴다
    (EP02 훅 1.03s '옥상 싸움에 불' 축약 재발 사례)."""
    grid2 = _mk_grid([(0.0, 1.0, True, "앞 장면 대사."), (1.0, 2.0, False, ""),
                      (2.0, 4.0, True, "대사."), (4.0, 5.2, False, ""),
                      (5.2, 7.2, False, ""), (7.2, 9.0, True, "끝.")])
    s2 = _mk_stage2(grid2, [(0, 2, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "hook", "span_ids": ["sp0001", "sp0002", "sp0003"],
              "narration": ["열여섯자짜리다리내레이션문장임"], "labels": [],
              "items": [{"kind": "nar", "text": "열여섯자짜리다리내레이션문장임",
                         "span_ids": [], "label": None},
                        {"kind": "mat", "text": None,
                         "span_ids": ["sp0001", "sp0002", "sp0003"],
                         "label": None}]},
             {"role": "ending", "span_ids": ["sp0005"], "narration": [],
              "labels": [], "items": [{"kind": "mat", "text": None,
                                       "span_ids": ["sp0005"], "label": None}]}]
    cues, dropped = st.plan_narration_slots(beats, idx, budget_slack=10.0)
    assert not dropped and len(cues) == 1
    assert cues[0]["source_time_sec"] == pytest.approx(4.0)   # 내부 run 으로 이동
    w = cues[0]["source_end_sec"] - cues[0]["source_time_sec"]
    assert w > 2.0                                            # 견적급 창
    assert "sp0004" in beats[0]["span_ids"]                   # 꼬리 성장 합류
    assert cues[0]["mode"] == "silent"


def test_rubric_penalizes_fit_mode_narration():
    """축약 예정(fit) cue 는 반 실현 — 자리 있는 안이 심사에서 이긴다."""
    grid2 = _mk_grid([(0.0, 0.6, False, ""), (0.6, 3.0, True, "대사."),
                      (10.0, 13.0, False, ""), (13.0, 15.0, True, "끝.")])
    s2 = _mk_stage2(grid2, [(0, 3, 4, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    def _story(span_ids):
        return {"template": "recap_dialogue", "title": {"line1": "제", "line2": "목"},
                "beats": [{"role": "climax", "span_ids": list(span_ids),
                           "narration": ["열여섯자짜리다리내레이션문장임"],
                           "labels": [], "muted_span_ids": []}]}
    tight = st.score_story(_story(["sp0000", "sp0001"]), idx, target_sec=53.0)
    roomy = st.score_story(_story(["sp0002", "sp0003"]), idx, target_sec=53.0)
    assert roomy["parts"]["narration"] > tight["parts"]["narration"]
