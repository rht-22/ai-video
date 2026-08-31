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
    assert lines[0]["text"] == "웬만하면 이런 부탁"       # 12자 규칙(공백 포함)
    assert lines[0]["start"] == pytest.approx(0.45)        # −0.05 선행
    assert lines[1]["text"] == "안하는데."
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
    # recap_dialogue 인데 내레이션을 아예 안 쓴 안도 규약 위반으로 0점
    none_planned = _cand([{"role": "hook", "span_ids": ["v1"],
                           "narration": None, "label": None}])
    assert score_story(none_planned, idx, target_sec=6.0)["parts"]["narration"] == 0.0


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
    assert score_story(mid, idx, target_sec=5.0)["parts"]["intro"] == 1.0


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
    # 창 1.5s — 첫 문장(견적 1.07s) 뒤 남는 0.43s 는 최소 노출(1.0s)에 못 미친다
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
    """남은 창이 최소 노출 이상이면 뒤 문장도 fit 으로 넣는다(버리기 전에 살린다)."""
    grid2 = _mk_grid([(0.0, 2.2, False, "")])
    s2 = _mk_stage2(grid2, [(0, 0, 3, "m")])
    idx, _ = st.build_span_index(s2, grid2)
    beats = [{"role": "hook", "span_ids": ["sp0000"],
              "narration": ["첫문장은들어간다", "둘째문장은자리가없다"], "labels": []}]
    cues, dropped = st.plan_narration_slots(beats, idx)
    assert len(cues) == 2 and not dropped
    assert cues[1]["mode"] == "fit"
    assert cues[1]["source_end_sec"] <= 2.2 + 1e-6


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
