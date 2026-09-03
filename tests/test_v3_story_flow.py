"""사람 편집 흐름 체인(app/v3/story_flow) 회귀 가드 — LLM·ffmpeg 없이 돈다.

고정하는 것:
  · 걸음별 검증기(주제·씬·대사·내레이션)의 반려/보정 규칙
  · 내레이션 문장 분할·불변(축약 경로 없음)
  · 덮개 후보 순서(lead_in → B-roll(같은 씬→고른 씬) → spill → mute) · 덮개끼리
    화면 재사용 금지 · 대사로 고른 조각은 B-roll 로 안 쓴다
  · 프로브 답 검증·스냅
  · 산출 계약: assemble 이 cover 클립을 내고 벨트가 100% · finalize_cues 가 audio_path 를 넘긴다
  · legacy 게이트: story_flow 미지정이면 기존 경로 무변경(assemble 은 covers 없는 beats 를 종전처럼)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v3_stage3 import _mk_grid, _mk_stage2  # noqa: E402

from app.v3 import assemble, story as st  # noqa: E402
from app.v3 import story_flow as sf  # noqa: E402
from app.v3.story_flow import cover as cv  # noqa: E402
from app.v3.story_flow import narration as nr  # noqa: E402
from app.v3.story_flow import select as sl  # noqa: E402
from app.v3.story_flow.common import meaning_rows, parse_meaning_id  # noqa: E402

GRID = _mk_grid([
    (0.0, 2.0, False, ""),                 # sp0000 씬 머리 무성
    (2.0, 4.0, True, "왜 여기 있어?"),      # sp0001
    (4.0, 6.5, True, "말 못 해."),          # sp0002
    (6.5, 9.0, False, ""),                 # sp0003 무성
    (9.0, 12.0, True, "이게 핵심 대사다."), # sp0004
    (12.0, 14.0, True, "떡밥 대사."),      # sp0005
    (14.0, 17.0, False, ""),               # sp0006 여운 무성
])
GRID["scene_cuts"] = [1.0, 6.5, 14.0]
S2 = _mk_stage2(GRID, [(0, 2, 3, "대치"), (3, 5, 5, "핵심"), (6, 6, 2, "여운")])
IDX, ORDER = st.build_span_index(S2, GRID)
ROWS = meaning_rows(S2)
ROWS_BY = {r["idx"]: r for r in ROWS}


def _beats():
    return [{"scene": 0, "role": "hook", "span_ids": ["sp0001", "sp0002"],
             "lines": ["sp0001", "sp0002"], "visual": [], "action": "대치"},
            {"scene": 1, "role": "climax", "span_ids": ["sp0004", "sp0005"],
             "lines": ["sp0004", "sp0005"], "visual": [], "action": "핵심"}]


def _group(anchor, texts, secs, refers="장면"):
    return {"anchor": anchor, "lines": list(texts), "measured": list(secs),
            "audio_paths": [None] * len(texts), "refers_to": refers}


# ── 공용 ────────────────────────────────────────────────────────────────────

def test_meaning_rows_match_span_index_meaning_idx():
    for r in ROWS:
        for sid in r["span_ids"]:
            assert IDX[sid]["meaning_idx"] == r["idx"]


@pytest.mark.parametrize("v,exp", [("m012", 12), (3, 3), ("7", 7), ("x", None), (True, None)])
def test_parse_meaning_id(v, exp):
    assert parse_meaning_id(v) == exp


# ── 걸음 1~3 검증 ──────────────────────────────────────────────────────────

def test_topic_rejects_unknown_meanings_and_empty_topic():
    obj, pr = sl.validate_topic({"topic": "", "core_meanings": ["m099"]}, ROWS)
    assert obj is None and len(pr) == 2
    obj, pr = sl.validate_topic({"topic": "t", "core_meanings": ["m001", 1, "m000"]}, ROWS)
    assert obj["core_meanings"] == [0, 1]


def test_scenes_sorted_dedup_purpose_coerced_title_limits():
    resp = {"scenes": [{"meaning": "m001", "purpose": "결과"},
                       {"meaning": "m000", "purpose": "이상한"},
                       {"meaning": "m001", "purpose": "결과"}],
            "title": {"line1": "짧은 제목", "line2": "x" * 17}}
    obj, pr, notes = sl.validate_scenes(resp, ROWS)
    assert obj is None and any("line2" in p for p in pr)
    resp["title"]["line2"] = "후킹"
    obj, pr, notes = sl.validate_scenes(resp, ROWS)
    assert [s["meaning"] for s in obj["scenes"]] == [0, 1]
    assert obj["scenes"][0]["purpose"] == "과정"
    assert any("중복" in n for n in notes)


def test_scenes_min_count():
    obj, pr, _ = sl.validate_scenes({"scenes": [{"meaning": "m000", "purpose": "배경"}],
                                     "title": {"line1": "a", "line2": "b"}}, ROWS)
    assert obj is None and any("최소" in p for p in pr)


def _allowed():
    _mat, allowed = sl.lines_material(
        [{"meaning": 0, "purpose": "배경"}, {"meaning": 1, "purpose": "결과"}], ROWS, IDX)
    return allowed


def test_lines_material_only_chosen_scenes():
    allowed = _allowed()
    assert set(allowed) == {"sp0000", "sp0001", "sp0002", "sp0003", "sp0004", "sp0005"}
    assert "sp0006" not in allowed


def test_beats_reject_reuse_and_half_sentence():
    grid = _mk_grid([(0.0, 2.0, True, "받아야"), (2.05, 4.0, True, "사업도 확장하고."),
                     (4.0, 6.0, True, "끝.")])
    s2 = _mk_stage2(grid, [(0, 2, 4, "x")])
    idx, _ = st.build_span_index(s2, grid)
    assert idx["sp0000"]["continues_to"] == "sp0001"
    allowed = {k: 0 for k in idx}
    obj, pr, _ = sl.validate_beats(
        {"beats": [{"role": "hook", "first": "sp0000", "last": "sp0000"}]}, idx, allowed,
        budget_sec=30)
    assert obj is None and any("반토막" in p for p in pr)
    obj, pr, _ = sl.validate_beats(
        {"beats": [{"role": "hook", "first": "sp0000", "last": "sp0001"},
                   {"role": "ending", "first": "sp0001", "last": "sp0002"}]},
        idx, allowed, budget_sec=30)
    assert obj is None and any("겹친다" in p for p in pr)


def test_beats_budget_tolerance_and_ordering():
    allowed = _allowed()
    resp = {"beats": [{"scene": "m001", "role": "climax", "first": "sp0004", "last": "sp0005"},
                      {"scene": "m000", "role": "hook", "first": "sp0000", "last": "sp0002",
                       "skip": ["sp0099"]}]}
    obj, pr, notes = sl.validate_beats(resp, IDX, allowed, budget_sec=30)
    assert obj is not None
    assert [b["role"] for b in obj] == ["hook", "climax"]        # 시간순 정렬
    assert obj[0]["span_ids"] == ["sp0000", "sp0001", "sp0002"]   # 구간 = 사이 조각 전부
    assert any("구간 밖 skip" in n for n in notes)
    obj, pr, _ = sl.validate_beats(resp, IDX, allowed, budget_sec=5)   # 11.5s > 5×1.3
    assert obj is None and any("예산" in p for p in pr)
    # 구간이 다른 씬에 걸치면 반려
    obj, pr, _ = sl.validate_beats({"beats": [{"role": "hook", "first": "sp0001", "last": "sp0004"}]},
                                   IDX, allowed, budget_sec=30)
    assert obj is None and any("다른 씬" in p for p in pr)


def test_beats_require_some_dialogue():
    obj, pr, _ = sl.validate_beats({"beats": [{"role": "hook", "first": "sp0000", "last": "sp0000"}]},
                                   IDX, _allowed(), budget_sec=30)
    assert obj is None and any("대사" in p for p in pr)


def test_short_skip_is_cut_but_long_hole_splits_beat_and_marks_jump():
    grid = _mk_grid([(0.0, 2.0, True, "질문."), (2.0, 2.6, True, "아이고."),
                     (2.6, 5.0, True, "답."), (5.0, 9.0, True, "긴 딴 얘기 하나."),
                     (9.0, 13.0, True, "긴 딴 얘기 둘."), (13.0, 15.0, True, "마무리.")])
    s2 = _mk_stage2(grid, [(0, 5, 4, "대화")])
    idx, _ = st.build_span_index(s2, grid)
    allowed = {k: 0 for k in idx}
    # 짧은 추임새(0.6s) 는 뺀 채 한 비트
    obj, pr, notes = sl.validate_beats(
        {"beats": [{"role": "hook", "first": "sp0000", "last": "sp0002", "skip": ["sp0001"]}]},
        idx, allowed, budget_sec=30)
    assert len(obj) == 1 and obj[0]["span_ids"] == ["sp0000", "sp0002"]
    assert obj[0]["skipped"] == ["sp0001"]
    assert sl.compute_jumps(obj, idx) == []
    # 8초·2줄을 빼면 그 자리가 비트 경계 — 반려가 아니라 분할 + 점프 표시
    obj, pr, notes = sl.validate_beats(
        {"beats": [{"role": "hook", "first": "sp0000", "last": "sp0005",
                    "skip": ["sp0003", "sp0004"]}]}, idx, allowed, budget_sec=30)
    assert pr == [] and len(obj) == 2
    assert obj[0]["span_ids"] == ["sp0000", "sp0001", "sp0002"]
    assert obj[1]["span_ids"] == ["sp0005"] and obj[1]["hole_before"] == ["sp0003", "sp0004"]
    assert any("나눔" in n for n in notes)
    jumps = sl.compute_jumps(obj, idx)
    assert len(jumps) == 1 and jumps[0]["before_beat"] == 1
    assert jumps[0]["skipped_text"] == ["갑: 긴 딴 얘기 하나.", "갑: 긴 딴 얘기 둘."]


def test_compute_jumps_by_source_gap():
    beats = _beats()                                  # 6.5 → 9.0 간격 2.5s: 점프 아님
    assert sl.compute_jumps(beats, IDX) == []
    assert sl.compute_jumps(beats, IDX, gap_sec=2.0)[0]["before_beat"] == 1


# ── 걸음 4 내레이션 ────────────────────────────────────────────────────────

def test_split_sentences_by_punctuation_then_comma():
    assert nr.split_sentences("짧은 문장.") == ["짧은 문장."]
    out = nr.split_sentences("옥상에서 마주친 두 사람, 말다툼이 시작됐죠.")
    assert out == ["옥상에서 마주친 두 사람,", "말다툼이 시작됐죠."]
    out = nr.split_sentences("첫 문장이다. 둘째 문장이다!")
    assert out == ["첫 문장이다.", "둘째 문장이다!"]


def test_validate_narrations_requires_hook_and_bounds():
    obj, pr, _ = nr.validate_narrations({"narrations": [{"before_beat": 1, "text": "x"}]}, 2)
    assert obj is None and any("도입" in p for p in pr)
    obj, pr, _ = nr.validate_narrations({"narrations": [{"before_beat": 5, "text": "x"}]}, 2)
    assert obj is None and any("밖" in p for p in pr)
    # 점프 자리는 필수 빈칸
    obj, pr, _ = nr.validate_narrations({"narrations": [{"before_beat": 0, "text": "훅."}]}, 3,
                                        required={0, 2})
    assert obj is None and any("before_beat: 2" in p and "다리" in p for p in pr)


def test_validate_narrations_cover_ids_must_be_available():
    resp = {"narrations": [{"before_beat": 0, "text": "훅.", "cover": ["sp0000", "sp0099"]},
                           {"after_last": True, "text": "끝.", "cover": ["sp0001"]}]}
    obj, pr, notes = nr.validate_narrations(resp, 2, available={"sp0000", "sp0003"})
    assert obj is None and any("쓸 수 있는 화면" in p for p in pr)     # sp0001 은 대사 조각
    resp["narrations"][1]["cover"] = ["sp0003"]
    obj, pr, notes = nr.validate_narrations(resp, 2, available={"sp0000", "sp0003"})
    assert obj[0]["cover_ids"] == ["sp0000"] and any("무시" in n for n in notes)
    assert obj[1]["cover_ids"] == ["sp0003"]


def test_available_covers_excludes_beat_spans():
    av = nr.available_covers(_beats(), {0: ROWS_BY[0], 1: ROWS_BY[1]}, IDX)
    assert av == ["sp0000", "sp0003"]
    block = nr.available_block(av, IDX, {"sp0000": 0, "sp0003": 1})
    assert "sp0000 | 00:00.0 | 2.0s" in block and "### 씬 m001" in block


def test_validate_narrations_splits_long_and_rejects_hard_max():
    long = "이건 아주 길어서 도저히 한 줄로는 못 넣는 문장인데 쉼표도 없다"
    obj, pr, _ = nr.validate_narrations({"narrations": [{"before_beat": 0, "text": long}]}, 2)
    assert obj is None and any("나눠" in p for p in pr)
    obj, pr, notes = nr.validate_narrations(
        {"narrations": [{"before_beat": 0, "text": "옥상에서 마주친 두 사람, 말다툼이 시작됐죠."},
                        {"after_last": True, "text": "떡밥."}]}, 2)
    assert obj[0]["anchor"] == ("before", 0) and obj[0]["lines"] == [
        "옥상에서 마주친 두 사람,", "말다툼이 시작됐죠."]
    assert obj[1]["anchor"] == ("after", 1)


def test_synthesize_groups_keeps_text_and_falls_back_to_estimate(tmp_path):
    g = [_group(("before", 0), ["문장 하나.", "문장 둘."], [])]
    calls = []

    def fn(text, path):
        calls.append(text)
        if "둘" in text:
            raise RuntimeError("boom")
        path.write_bytes(b"x")
        return 1.5
    nr.synthesize_groups(g, tmp_path, fn, log=lambda *a: None)
    assert g[0]["lines"] == ["문장 하나.", "문장 둘."]          # 문장 불변
    assert g[0]["measured"][0] == 1.5 and g[0]["audio_paths"][0].endswith("nar_00_0.mp3")
    assert g[0]["measured"][1] == nr.estimate_sec("문장 둘.") and g[0]["audio_paths"][1] is None


# ── 걸음 5 덮개 ────────────────────────────────────────────────────────────

def test_subtract_and_used_intervals():
    used = cv.used_intervals(_beats(), IDX)
    assert used == [(2.0, 6.5), (9.0, 14.0)]
    assert cv.subtract((0.0, 17.0), used) == [(0.0, 2.0), (6.5, 9.0), (14.0, 17.0)]


def test_candidate_order_lead_in_first_then_scene_broll():
    wins = cv.candidate_windows(("before", 1), _beats(), IDX, ROWS_BY, L=1.5)
    kinds = [w["kind"] for w in wins]
    assert kinds[0] == "lead_in" and wins[0]["w0"] == 6.5 and wins[0]["w1"] == 9.0
    assert "mute_head" == kinds[-1]
    # 같은 씬의 빈 구간(6.5~9.0)은 lead_in 과 같은 창이라 중복 없이 한 번만, 다른 씬
    # B-roll(0~2 · 14~17)은 그 뒤에
    assert kinds.count("lead_in") == 1 and "broll_scene" not in kinds
    assert "broll_any" in kinds and kinds.index("broll_any") > 0
    ivs = [(w["w0"], w["w1"]) for w in wins if w["kind"] == "broll_any"]
    assert (0.0, 2.0) in ivs and (14.0, 17.0) in ivs
    # 앵커 바로 앞에 이미 덮개(8~9)가 놓였으면 lead_in 은 없고, 남은 같은 씬 구간
    # (6.5~8.0)이 B-roll 로 다른 씬보다 앞선다
    wins2 = cv.candidate_windows(("before", 1), _beats(), IDX, ROWS_BY, L=1.0,
                                 extra_used=[(8.0, 9.0)])
    k2 = [w["kind"] for w in wins2]
    assert "lead_in" not in k2
    assert wins2[0] == {"kind": "broll_scene", "w0": 6.5, "w1": 8.0, "attach": "start"}
    assert k2.index("broll_scene") < k2.index("broll_any")


def test_lead_in_short_becomes_spill_before_mute():
    wins = cv.candidate_windows(("before", 0), _beats(), IDX, ROWS_BY, L=3.2)
    kinds = [w["kind"] for w in wins]
    assert "lead_in" not in kinds                      # 2.0s < 3.2s
    assert kinds.index("lead_spill") < kinds.index("mute_head")
    sp = next(w for w in wins if w["kind"] == "lead_spill")
    assert (sp["w0"], sp["w1"]) == (0.0, 3.2)


def test_broll_never_reuses_chosen_lines_or_placed_covers():
    wins = cv.candidate_windows(("after", 1), _beats(), IDX, ROWS_BY, L=1.5,
                                extra_used=[(14.0, 17.0)])
    for w in wins:
        if w["kind"].startswith("broll") or w["kind"] == "tail":
            assert not (w["w0"] < 14.0 < w["w1"] or (w["w0"] >= 14.0 and w["w1"] <= 17.0))
            assert not (w["w0"] < 12.0 and w["w1"] > 9.0)       # 대사 sp0004~5 위 금지


def test_choose_cover_lead_in_joins_small_gap_and_trims_beat_head():
    beats = _beats()
    cover = cv.choose_cover(("before", 1), _group(("before", 1), ["다리."], [1.0]),
                            beats, IDX, ROWS_BY, GRID, log=lambda *a: None)
    assert cover["kind"] == "lead_in" and cover["t_out"] == 9.0      # 앵커까지 잇는다
    removed = cv.apply_cover_to_beats(cover, beats, IDX, 1)
    assert removed == [] and beats[1]["span_ids"] == ["sp0004", "sp0005"]


def test_choose_cover_spill_trims_line_head():
    beats = _beats()
    cover = cv.choose_cover(("before", 0), _group(("before", 0), ["긴 도입 문장이다."], [2.95]),
                            beats, IDX, ROWS_BY, GRID, log=lambda *a: None)
    assert cover["kind"] == "lead_spill" and cover["t_in"] == 0.0
    cv.apply_cover_to_beats(cover, beats, IDX, 0)
    assert beats[0]["head_trim_sec"] == cover["t_out"]
    assert beats[0]["span_ids"] == ["sp0001", "sp0002"]


def test_choose_cover_uses_probe_and_snaps_to_scene_cut(monkeypatch):
    beats = _beats()
    seen = {}

    def fake_probe(gemini, video, out_dir, tag, win, L, text, refers, grid, span_index,
                   log=print, focus=None):
        seen["win"] = win
        return {"start": 6.6, "snap": "scene_cut", "raw": 6.5, "reason": "r",
                "confidence": "high", "probe_window": [6.5, 9.0]}
    monkeypatch.setattr(cv, "run_probe", fake_probe)
    budget = {"left": 1, "used": 0}
    cover = cv.choose_cover(("before", 1), _group(("before", 1), ["다리."], [0.8]),
                            beats, IDX, ROWS_BY, GRID, gemini=object(), video=Path("v"),
                            out_dir=Path("o"), budget=budget, log=lambda *a: None)
    assert seen["win"]["kind"] == "lead_in" and budget["used"] == 1
    assert cover["t_in"] == 6.6 and cover["probe"]["snap"] == "scene_cut"
    # 예산 0 이면 프로브 없이 산술
    cover2 = cv.choose_cover(("before", 1), _group(("before", 1), ["다리."], [0.8]),
                             _beats(), IDX, ROWS_BY, GRID, gemini=object(), video=Path("v"),
                             out_dir=Path("o"), budget={"left": 0, "used": 1},
                             log=lambda *a: None)
    assert cover2["probe"] is None and cover2["t_out"] == 9.0


def test_validate_probe_and_snap():
    assert cv.validate_probe({"start_sec": 1.2}, L=2.0, clip_len=5.0) == (1.2, "")
    assert cv.validate_probe({"start_sec": 4.0}, L=2.0, clip_len=5.0)[0] is None
    assert cv.validate_probe({"start_sec": 3.2}, L=2.0, clip_len=5.0) == (3.0, "")  # 0.5 관용 클램프
    assert cv.validate_probe("x", 1, 1)[0] is None
    assert cv.snap_time(6.6, GRID) == (6.5, "scene_cut")
    assert cv.snap_time(2.2, GRID) == (2.0, "grid_edge")
    assert cv.snap_time(3.0, GRID) == (3.0, "raw")


def test_probe_prompt_mentions_constraint_and_cuts():
    p = cv.build_probe_prompt(0.0, 9.0, 2.0, "다리 문장", "장면", GRID, IDX, attach="end")
    assert "start_sec ≤ 7.0" in p and "1.0s, 6.5s" in p and "다리 문장" in p


# ── 오케스트레이터 → 산출 계약 ────────────────────────────────────────────

ANSWERS = [
    {"topic": "대치 끝에 핵심이 드러난다", "why": "w", "core_meanings": ["m000", "m001"],
     "title_draft": {"line1": "a", "line2": "b"}},
    {"scenes": [{"meaning": "m000", "purpose": "배경", "why": "x"},
                {"meaning": "m001", "purpose": "결과", "why": "y"},
                {"meaning": "m002", "purpose": "반응", "why": "z"}],
     "title": {"line1": "왜 여기 있냐 물었더니", "line2": "돌아온 핵심 대사"}},
    {"beats": [{"scene": "m000", "role": "hook", "first": "sp0001", "last": "sp0002", "action": "대치"},
               {"scene": "m001", "role": "climax", "first": "sp0004", "last": "sp0005", "action": "핵심"}]},
    {"narrations": [{"before_beat": 0, "text": "옥상에서 마주친 두 사람, 말다툼이 시작됐죠.",
                     "cover": ["sp0000"]},
                    {"before_beat": 1, "text": "그러다 핵심이 나왔는데,", "cover": ["sp0003"]},
                    {"after_last": True, "text": "이게 위험한 시작이었죠.", "cover": ["sp0006"]}]},
]


def _run(tmp_path, monkeypatch, answers=None):
    answers = list(answers or ANSWERS)
    monkeypatch.setattr(sf, "call_json", lambda g, p: answers.pop(0))

    def synth(text, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return round(0.3 + len("".join(text.split())) / 8.4, 3)

    class G:
        class config:
            flash_model_name = "x"
    return sf.run_story_flow(G(), S2, GRID, work_title="T", target_sec=20, max_sec=30,
                             output_dir=tmp_path, synth_fn=synth, probe=False,
                             log=lambda *a: None)


def test_flow_doc_contract_and_assemble_belt(tmp_path, monkeypatch):
    doc, audit = _run(tmp_path, monkeypatch)
    assert doc["schema"] == st.SCHEMA_STORY and doc["template"] == "human_flow"
    assert doc["title"] == {"line1": "왜 여기 있냐 물었더니", "line2": "돌아온 핵심 대사"}
    kinds = [c["kind"] for b in doc["beats"] for c in b["covers"]]
    # before0: 지정 화면 sp0000(2.0s) < L 3.23 이고 넓힐 인접 미사용 조각이 없어 후보 탐색
    # → lead_spill. 나머지 둘은 모델이 지정한 화면 그대로(designated).
    assert kinds == ["lead_spill", "designated", "designated"]
    d1 = doc["beats"][1]["covers"][0]
    assert d1["t_in"] == 6.5 and d1["t_out"] == 6.5 + d1["L"]
    # 덮개끼리 화면 재사용 없음
    ivs = [(c["t_in"], c["t_out"]) for b in doc["beats"] for c in b["covers"]]
    for i, (a, z) in enumerate(ivs):
        for a2, z2 in ivs[i + 1:]:
            assert z <= a2 or z2 <= a
    # 문장 불변 · 모든 줄이 cue 로
    texts = [c["text"] for c in doc["narration_cues"]]
    assert texts == ["옥상에서 마주친 두 사람,", "말다툼이 시작됐죠.",
                     "그러다 핵심이 나왔는데,", "이게 위험한 시작이었죠."]
    assert all(c["speed"] == nr.NAR_SPEED and c["audio_path"] for c in doc["narration_cues"])
    assert doc["narration_dropped"] == []
    # cue 창은 자기 덮개 안
    for c in doc["narration_cues"]:
        cov = [x for x in doc["beats"][c["beat"]]["covers"]
               if x["t_in"] <= c["source_time_sec"] and c["source_end_sec"] <= x["t_out"] + 1e-6]
        assert cov, c
        assert c["source_end_sec"] - c["source_time_sec"] >= c["measured_sec"] - 1e-6
    plan = assemble.assemble_edit_plan(doc, IDX, video_path="v", work_title="T",
                                       words=[], silences=[])
    covers = [c for c in plan["timeline"] if c.get("cover")]
    assert len(covers) == 3 and all(not c["use_original_audio"] for c in covers)
    assert plan["timeline"][0]["cover"] == "lead_spill" and plan["timeline"][0]["clip_start_sec"] == 0.0
    assert plan["timeline"][1]["head_trimmed"] and plan["timeline"][1]["clip_start_sec"] == covers[0]["clip_end_sec"]
    assert plan["timeline"][-1]["cover"] == "designated"         # after 덮개는 비트 뒤
    belt = assemble.verify_edit_plan(plan, GRID)
    assert belt["pct"] == 100.0 and belt["cover_edges"] == 3
    fin = assemble.finalize_cues(doc["narration_cues"], plan["timeline"],
                                 voice="ko_female", speed="normal")
    assert all(c["start_sec"] is not None and c["audio_path"] and c["speed"] == nr.NAR_SPEED
               for c in fin)
    assert abs(sum(assemble.clip_stats(plan)["durations"]) - doc["budget"]["total_after_sec"]) < 1e-6
    assert audit["probe_calls"] == 0 and audit["pieces"] == 2


def test_flow_reask_then_fail_loud(tmp_path, monkeypatch):
    bad = [{"topic": "", "core_meanings": []}] * 3
    with pytest.raises(ValueError, match="topic"):
        _run(tmp_path, monkeypatch, bad)


def test_pad_voiced_tails_skips_cover_clips():
    timeline = [{"clip_start_sec": 0.0, "clip_end_sec": 3.2, "span_ids": ["sp0001"],
                 "cover": "lead_spill", "use_original_audio": False},
                {"clip_start_sec": 9.0, "clip_end_sec": 14.0, "span_ids": ["sp0004", "sp0005"],
                 "use_original_audio": True}]
    n = assemble.pad_voiced_tails(timeline, IDX, words=[{"t0": 14.6, "t1": 15.0, "text": "x"}],
                                  silences=[[14.1, 14.5]])
    assert timeline[0]["clip_end_sec"] == 3.2 and "tail_pad" not in timeline[0]
    assert "head_pad" not in timeline[0] and timeline[0]["clip_start_sec"] == 0.0
    assert n >= 1 and timeline[1].get("tail_pad")


def test_legacy_beats_without_covers_unchanged():
    doc = {"title": {"line1": "a", "line2": "b"},
           "beats": [{"role": "hook", "span_ids": ["sp0001", "sp0002"], "muted_span_ids": []}]}
    plan = assemble.assemble_edit_plan(doc, IDX, video_path="v", work_title="T")
    assert len(plan["timeline"]) == 1 and "cover" not in plan["timeline"][0]
    assert assemble.verify_edit_plan(plan, GRID)["cover_edges"] == 0


def test_designated_window_grows_over_unused_neighbors_only():
    beats = _beats()
    # sp0003(6.5~9.0, 2.5s) 지정 · L 4.0 → 앞 sp0002 는 대사(사용) · 뒤 sp0004 도 대사 → 못 넓힘
    dw = cv.designated_window(["sp0003"], beats, IDX, L=4.0)
    assert (dw["w0"], dw["w1"]) == (6.5, 9.0) and dw["kind"] == "designated"
    # 사용 중인 조각을 지정하면 None(후보 탐색으로)
    assert cv.designated_window(["sp0004"], beats, IDX, L=1.0) is None
    # 지정 조각이 짧아도 인접 미사용 조각으로 넓힌다: sp0000(0~2) + 이어지는 사용 조각 없음
    beats2 = [{"scene": 1, "role": "climax", "span_ids": ["sp0004"], "action": ""}]
    dw2 = cv.designated_window(["sp0001"], beats2, IDX, L=5.0)
    assert dw2["w0"] == 0.0 and dw2["w1"] >= 5.0 and dw2["focus0"] == 2.0


def test_probe_window_keeps_whole_window_and_centers_focus():
    win = {"kind": "designated", "w0": 0.0, "w1": 20.0, "attach": "start"}
    assert cv.probe_window(win, L=2.0) == (0.0, 20.0)            # 상한 안 — 통째로
    win = {"kind": "designated", "w0": 0.0, "w1": 60.0, "attach": "start"}
    t0, t1 = cv.probe_window(win, L=2.0, focus=(40.0, 42.0))
    assert t1 - t0 == cv.PROBE_MAX_WINDOW_SEC and t0 <= 40.0 <= t1
    assert cv.probe_window(win, L=2.0) == (0.0, cv.PROBE_MAX_WINDOW_SEC)
