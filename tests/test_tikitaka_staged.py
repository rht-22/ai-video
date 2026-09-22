"""staged 대본 흐름(뼈대 → 편별 문장·화면 → 게이트 → 첫 3초 기록) 회귀 가드 — LLM·ffmpeg 없이 돈다."""
import copy

import pytest

from app.tikitaka import staged as st


def material():
    spans = [("sp0", 0.0, 1.0, True, 3), ("sp1", 1.0, 3.0, False, 5), ("sp2", 3.0, 4.5, False, 4), ("sp3", 4.5, 6.0, True, 3),
             ("sp4", 6.0, 9.0, False, 2), ("sp5", 10.0, 12.0, True, 3), ("sp6", 12.0, 15.0, False, 3)]
    desc = {"sp0": "경희가 문을 열며 부른다", "sp1": "재홍이 피 묻은 손을 든다", "sp2": "경희가 입을 막고 뒷걸음친다",
            "sp3": "경희가 재홍을 다그친다", "sp4": "재홍이 고개를 숙인다", "sp5": "수정이 창밖을 본다", "sp6": "수정이 휴대폰을 든다"}
    facts = {sid: {"span_id": sid, "time": {"start": a, "end": b}, "is_audio": v, "importance": imp, "scene_script": desc[sid],
                   "characters": []} for sid, a, b, v, imp in spans}
    moments, k = [], 0
    for sid, a, b, v, imp in spans:
        if not v:
            k += 1
            moments.append({**facts[sid], "id": f"S-{k:03d}", "span_ids": [sid], "start": a, "end": b, "desc": desc[sid], "who": None})
    lines = [{"id": "L-001", "start": 0.1, "end": 0.9, "text": "여보?", "speaker": "경희", "word_i": [0]},
             {"id": "L-002", "start": 4.6, "end": 5.9, "text": "이게 무슨 짓이야!", "speaker": "경희", "word_i": [1, 2, 3]},
             {"id": "L-003", "start": 10.2, "end": 11.8, "text": "다 봤어요.", "speaker": "수정", "word_i": [4, 5]}]
    words = [{"i": 0, "start": 0.1, "end": 0.9, "text": "여보?"}, {"i": 1, "start": 4.6, "end": 5.0, "text": "이게"},
             {"i": 2, "start": 5.0, "end": 5.4, "text": "무슨"}, {"i": 3, "start": 5.4, "end": 5.9, "text": "짓이야!"},
             {"i": 4, "start": 10.2, "end": 11.0, "text": "다"}, {"i": 5, "start": 11.0, "end": 11.8, "text": "봤어요."}]
    index = {"scenes": [{"id": "SC-001", "start": 0.0, "end": 10.0, "summary": "차고", "chars": []},
                        {"id": "SC-002", "start": 10.0, "end": 20.0, "summary": "앞집", "chars": []}],
             "moments": moments, "grid_facts": facts, "cast": []}
    grid = {"span_candidates": [{"id": sid, "t_in": a, "t_out": b, "is_audio": v} for sid, a, b, v, _ in spans],
            "scene_cuts": [1.0, 3.0, 4.5, 6.0, 10.0, 12.0],
            "arousal": [{"t": t / 2, "score": (2.0 if 1.0 <= t / 2 < 3.0 else -1.0)} for t in range(40)]}
    return index, {"lines": lines, "words": words}, grid


def outline_raw():
    return {"versions": [{"n": 1, "strategy": "충격 폭로형", "title": {"line1": "차고 문 열었더니", "line2": "피 묻은 남편"},
                          "structure": "화면 훅 → 대사", "hook": {"with": "A", "why": "피 묻은 손이 3초 안에 보인다"},
                          "items": [{"type": "A", "moment_ids": ["S-001", "S-002"], "effect": "[헉]"},
                                    {"type": "N", "text": "뼈대에 끼어든 내레이션"},
                                    {"type": "S", "line_ids": ["L-002"]},
                                    {"type": "S", "line_ids": ["L-003"]}]}],
            "ranking": [1], "recommended": 1, "reason": "x"}


def outline():
    index, tr, grid = material()
    data = st.normalize_outline(outline_raw(), index, tr)
    return data["versions"][0], index, tr, grid


def test_prompts_borrow_sections_from_the_single_source():
    secs = st.prompt_sections()
    for head in ("[System Role]", "입력", "[제1원칙", "[제2원칙", "[제3원칙", "[제3-1원칙", "[제4원칙", "스토리 포맷 상자"):
        assert any(k.startswith(head) for k in secs), head
    index, tr, _ = material()
    p = st.outline_prompt(title="작품", episode_label="1화", duration_label="00:20분", script="SCRIPT", guide=None, digest=None,
                          material_note="", seq_hook_rule="", eye=st.eye_block(index), n_versions=14, target_min=45, target_max=70)
    assert "내레이션은 자리도 문장도 정하지 않는다" in p and "눈길 끄는 화면" in p and "15 공감형" in p and "SCRIPT" in p
    assert "대사·현장음 합계는 22~52초" in p and '"intent"' not in p and "{title}" not in p
    from app.tikitaka.rebuild import SHORT_FORM_STRATEGIES
    assert st.SHORT_FORM == SHORT_FORM_STRATEGIES


def test_eye_catchers_merge_touching_moments():
    index, _, _ = material()
    rows = st.eye_catchers(index)
    assert len(rows) == 1 and rows[0].startswith("★5 S-001~S-002 | SC-001 |") and "| 3.5s |" in rows[0]
    assert st.eye_catchers(index, [(0.0, 5.0)]) == []


def test_outline_holds_only_dialogue_and_ambience():
    v, index, tr, _ = outline()
    assert [it["type"] for it in v["items"]] == ["A", "A", "S", "S"]                       # moment_ids → 연속 A · N 은 버린다
    assert [it.get("moment_id") for it in v["items"][:2]] == ["S-001", "S-002"] and v["items"][0]["effect"] == "[헉]"
    assert any("N 항목 — 버림" in i for i in v["issues"])
    assert v["hook"] == {"with": "A", "why": "피 묻은 손이 3초 안에 보인다"} and v["script_flow"] == "staged"
    items, notes = st.expand_moment_runs({"type": "A", "moment_ids": ["S-001", "S-004"]},
                                         {m["id"]: m for m in index["moments"]}, index["scenes"])
    assert [i["moment_id"] for i in items] == ["S-001"] and "맞닿지 않는다" in notes[0]    # 떨어진 화면은 묶지 않는다


def test_bridge_marks_are_for_failure_handling_not_placement():
    v, index, tr, _ = outline()
    marks = st.bridge_marks(v, index, tr)
    assert set(marks) == {3} and marks[3]["kind"] == "jump" and marks[3]["from_scene"] == "SC-001"   # L-002(5.9) → L-003(10.2): 장면이 바뀐다
    back = copy.deepcopy(v)
    back["items"] = [v["items"][3], v["items"][2]]                                         # 결말을 먼저 → 되감기
    assert st.bridge_marks(back, index, tr)[1]["kind"] == "rewind"
    text = st.lineup_block(v, index, tr)
    assert "⚠ 이어야 하는 자리(before: 4)" in text and '3. [S] L-002 경희: "이게 무슨 짓이야!"' in text
    assert "리듬 내레이션" in st.slot_role(None) and "되감는" in st.slot_role({"kind": "rewind", "gap_sec": -40.0})


def table_for(v, index, tr):
    wins = st.item_windows(v, index, tr)
    return st.cover_table(index, tr, [], scenes=set(st.scope_scenes(v, index, tr)),
                          own_dialogue=[(a, b) for a, b, k, _ in wins if k == "S"],
                          forbidden=[(a, b) for a, b, k, _ in wins if k == "A"])


def test_cover_table_is_scoped_marked_and_carries_run_length():
    v, index, tr, _ = outline()
    assert st.scope_scenes(v, index, tr) == ["SC-001", "SC-002"]
    only = copy.deepcopy(v)
    only["items"] = only["items"][:3]
    assert st.scope_scenes(only, index, tr) == ["SC-001"] and st.scope_scenes(only, index, tr, neighbor=1) == ["SC-001", "SC-002"]
    rows, info = table_for(v, index, tr)
    text = "\n".join(rows)
    assert "sp1 | 00:01.0 | 2.0s | - | ★ 무성 S-001 | 재홍이 피 묻은 손을 든다 [현장음 — 고를 수 없음]" in text
    assert "sp3 | 00:04.5 | 1.5s | 묶음 4.5s | 유성 L-002 | 경희가 재홍을 다그친다 (대사: 이게 무슨 짓이야!) [이 편의 대사 화면]" in text
    assert info["sp3"]["run_id"] == info["sp4"]["run_id"] == "sp3" and info["sp4"]["run"] == 4.5   # 현장음(sp1·sp2)이 묶음을 끊는다
    assert info["sp0"]["run"] == 1.0 and not info["sp1"]["usable"]
    assert st.cover_table(index, tr, [], scenes={"SC-002"})[1].keys() == {"sp5", "sp6"}


def plan(sid, kind="action", evidence=("S-003",)):
    return {"kind": kind, "evidence_ids": list(evidence), "cover": [{"span_id": sid, "role": "evidence"}]}


def answer(*rows):
    base = [{"before": 3, "text": "고개 숙인 남편.", "effect": None, "production_plan": plan("sp4")},
            {"before": 4, "text": "그 시각 앞집에선,", "effect": None, "production_plan": plan("sp6", "transition", ("S-004",))}]
    return {"narrations": list(rows) if rows else base}


def test_narration_may_sit_between_adjacent_lines_and_is_measured():
    v, index, tr, _ = outline()
    _, info = table_for(v, index, tr)
    out, problems = st.validate_narration(answer(), v, info, index, tr, measure=lambda t: 1.2)
    assert not problems and [x["pos"] for x in out] == [2, 3] and out[0]["sec"] == 1.2       # before 3 = 붙어 있는 A 와 S 사이
    long, _ = st.validate_narration(answer(), v, info, index, tr, measure=lambda t: 9.0), None
    assert any("(합성 실측) > 고른 화면의 묶음 4.5초" in p for p in long[1])                  # 길이는 합성 실측으로 잰다
    missing = st.validate_narration(answer(answer()["narrations"][0]), v, info, index, tr)[1]
    assert any("before 4: ⚠ 이어야 하는 자리" in p for p in missing)                         # 점프 자리는 꼭 잇는다
    bad = st.validate_narration(answer({"before": 3, "text": "고개 숙인 남편.", "production_plan": plan("sp1")}, answer()["narrations"][1]),
                                v, info, index, tr)[1]
    assert any("고를 수 없는 화면 ['sp1']" in p for p in bad)
    twice = st.validate_narration(answer(answer()["narrations"][0], {"before": 4, "text": "이걸 어쩌나.", "production_plan": plan("sp4")}),
                                  v, info, index, tr)[1]
    assert any("이미 쓴 화면" in p for p in twice)


def test_bad_evidence_ids_do_not_kill_a_script():
    v, index, tr, _ = outline()
    _, info = table_for(v, index, tr)
    rows = answer({"before": 3, "text": "고개 숙인 남편.", "production_plan": plan("sp4", evidence=("sp0735",))}, answer()["narrations"][1])
    out, problems = st.validate_narration(rows, v, info, index, tr)
    assert not problems and out[0]["production_plan"]["evidence_ids"] == ["S-003"]          # 오기는 버리고 덮개 화면의 기록으로 채운다


def test_hook_narration_must_take_an_eye_catching_screen():
    v, index, tr, _ = outline()
    v["items"] = v["items"][2:]                                                           # 화면 훅이 없는 편 — 내레이션으로 연다
    _, info = table_for(v, index, tr)
    rows = answer({"before": 1, "text": "고개 숙인 남편.", "production_plan": plan("sp4")},
                  {"before": 2, "text": "그 시각 앞집에선,", "production_plan": plan("sp6", "transition", ("S-004",))})
    probs = st.validate_narration(rows, v, info, index, tr)[1]
    assert any("첫 화면(훅)" in p and "sp1" in p for p in probs)                          # sp4(중요도 2) 대신 ★ 후보를 알려준다


def filled():
    v, index, tr, grid = outline()
    _, info = table_for(v, index, tr)
    st.apply_narration(v, st.validate_narration(answer(), v, info, index, tr)[0], index, tr)
    return v, index, tr, grid


def test_apply_marks_which_narrations_the_story_cannot_lose():
    v, _, _, _ = filled()
    assert [it["type"] for it in v["items"]] == ["A", "A", "N", "S", "N", "S"]
    n1, n2 = (it for it in v["items"] if it["type"] == "N")
    assert (n1["slot"], n1["required"]) == ("N01", False) and "리듬 내레이션" in n1["intent"]   # 붙어 있는 항목 사이 — 빼도 이어진다
    assert (n2["slot"], n2["required"]) == ("N02", True) and "건너뛴 자리" in n2["intent"]      # 장면이 바뀌는 자리 — 뺄 수 없다


class FakeJob:
    def __init__(self, grid):
        self.files, self.logs, self.steps = {"grid.json": grid}, [], []

    def has(self, name):
        return name in self.files

    def load(self, name):
        return self.files[name]

    def save(self, name, data):
        self.files[name] = data

    def log(self, msg):
        self.logs.append(msg)

    def record_step(self, name, **kw):
        self.steps.append((name, kw))


class FakeGemini:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def text_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def gated(monkeypatch, verdicts, answers):
    """verdicts: 프로브가 차례로 돌려줄 (일치?, 본 것) 목록, 또는 (cut, text) → (일치?, 본 것) 함수."""
    from app.tikitaka import grid_table
    v, index, tr, grid = filled()
    it = iter(verdicts) if isinstance(verdicts, list) else None
    contexts = []

    def probe(job, gemini, cut, text, *, key, context=None):
        contexts.append(context)
        ok, what = next(it) if it else verdicts(cut, text)
        return {"text_matches": ok, "record_matches": True, "seen": what, "reason": "r"}
    monkeypatch.setattr(grid_table, "probe_cover", probe)
    return v, index, tr, FakeJob(grid), FakeGemini(answers), contexts


def test_gate_passes_untouched_when_the_screens_match(monkeypatch):
    v, index, tr, job, gem, contexts = gated(monkeypatch, [(True, "고개 숙인 남자"), (True, "휴대폰을 든 여자")], [])
    job.files["staged_names.json"] = {"박경희": "김혜수"}
    st.enforce_staged_plans(job, gem, v, index, tr)
    assert all(it["production_plan"]["gate_verified"] for it in v["items"] if it["type"] == "N")
    assert not gem.prompts and "joint_plan_repairs" not in v
    assert "박경희=김혜수" in contexts[0]["인물 표기 규칙"]                                  # 검사관이 배우 이름 표기를 안다


def test_gate_swaps_screen_then_rewrites_from_what_the_inspector_saw(monkeypatch):
    swap = {"text": "무시됨", "production_plan": plan("sp0")}
    rewrite = {"text": "경희다.", "production_plan": plan("sp0", evidence=("L-001",))}   # sp0 은 1.0초 — 그 안에 드는 문장
    seen = {6.0: "천장을 보는 남자", 0.0: "문을 여는 여자", 4.5: "다그치는 여자", 12.0: "휴대폰을 든 여자"}
    v, index, tr, job, gem, _ = gated(monkeypatch, lambda cut, text: (text == "경희다." or cut["in"] == 12.0, seen[cut["in"]]), [swap, rewrite])
    st.enforce_staged_plans(job, gem, v, index, tr)
    assert "문장은 한 글자도 바꾸지 말고" in gem.prompts[0]                                  # ① 화면 교체 — 문장 고정
    assert "검사관이 실제로 본 화면" in gem.prompts[1] and "천장을 보는 남자" in gem.prompts[1] and "문을 여는 여자" in gem.prompts[1]
    assert "리듬 내레이션" in gem.prompts[1]                                                 # ② 자리의 역할을 이어받는다
    n = next(it for it in v["items"] if it["type"] == "N")
    assert n["text"] == "경희다." and n["production_plan"]["gate_verified"] and v["joint_plan_repairs"][0]["rewritten"]


def test_gate_drops_a_rhythm_narration_and_records_it(monkeypatch):
    same = {"text": "x", "production_plan": plan("sp4")}
    escape = {"text": "창밖을 보는 수정.", "production_plan": plan("sp5", evidence=("L-003",))}   # 본 적 없는 화면으로 도망 → 거절
    v, index, tr, job, gem, _ = gated(monkeypatch, [(False, "천장"), (False, "천장"), (True, "휴대폰을 든 여자")], [same, escape])
    st.enforce_staged_plans(job, gem, v, index, tr)
    assert [it["type"] for it in v["items"]] == ["A", "A", "S", "N", "S"]                    # 그 내레이션만 빠진다
    d = v["narration_dropped"][0]
    assert d["stage"] == "gate" and d["slot"] == "N01" and d["text"] == "고개 숙인 남편." and d["tried"]
    assert any("[staged/뺌]" in m for m in job.logs) and job.steps[0][0] == "staged_narration_dropped"   # 빠질 때마다 기록


def test_gate_fails_loudly_where_the_story_must_be_bridged(monkeypatch):
    same = {"text": "x", "production_plan": plan("sp6", "transition", ("S-004",))}
    verdicts = [(True, "고개 숙인 남자"), (False, "빈 방"), (False, "빈 방"), (False, "빈 방")]
    v, index, tr, job, gem, _ = gated(monkeypatch, verdicts, [same, dict(same, text="빈 방.")])
    with pytest.raises(ValueError, match="이어야 하는 자리"):
        st.enforce_staged_plans(job, gem, v, index, tr)


def test_opening_record_measures_instead_of_rejecting():
    v, index, tr, grid = filled()
    rec = st.opening_record(v, index, tr, grid)
    f = rec["facts"]
    assert f["first_item"] == "A" and f["cover_importance"] == 5 and f["first_cut_sec"] == 2.0 and f["first_dialogue_sec"] is None
    assert f["arousal_pct"] is not None and 0 <= rec["strength"] <= 100 and rec["writer_why"].startswith("피 묻은 손")
    assert "말 없는 화면" in rec["reason"] and rec["basis"] == "plan"
    weak = copy.deepcopy(v)
    weak["items"] = [{"type": "N", "text": "고개 숙인 남편.", "plan_sec": 4.0, "slot": "N01",
                      "production_plan": {"cover": [{"span_id": "sp4", "role": "support"}]}}] + weak["items"][3:]
    assert st.opening_record(weak, index, tr, grid)["strength"] < rec["strength"]                     # 서 있는 화면 위 내레이션은 약하다


def test_carry_slots_recomputes_from_position_after_verify():
    v, index, tr, _ = filled()
    final = {"items": [{"type": "S", "line_ids": ["L-002"]}, {"type": "N", "text": "확인 패스가 고친 문장"},
                       {"type": "S", "line_ids": ["L-003"]}, {"type": "N", "text": "끝."}]}
    st.carry_slots(v, final, index, tr)
    n1, n2 = (it for it in final["items"] if it["type"] == "N")
    assert n1["required"] is True and n1["slot"] == "N01" and n2["required"] is False and "마지막" in n2["intent"]
    assert final["script_flow"] == "staged"


def test_rebuild_refuses_a_cache_made_by_the_other_flow():
    from app.tikitaka.rebuild import rebuild
    index, tr, grid = material()
    job = FakeJob(grid)
    job.files["rebuild.json"] = {"versions": [], "script_flow": "staged"}
    with pytest.raises(ValueError, match="--script-flow staged"):
        rebuild(job, None, index, tr, title="t", episode_label="1", duration=20.0, script_flow="single")


# ── 캐시 지문은 화자 표기에 흔들리지 않는다 (2026-09-22 실사고: 확인 패스 화자 교정 4줄에 뼈대 14편 재생성) ──
def test_fingerprints_ignore_speaker_labels():
    from app.tikitaka.staged import outline_fingerprint, script_fingerprint, speaker_neutral
    from app.tikitaka.digest import digest_sha
    index = {"scenes": [{"id": "SC-001", "start": 0.0, "end": 10.0, "place": "", "summary": "", "chars": []}],
             "moments": [], "grid_facts": {}}
    tr_a = {"lines": [{"id": "L-001", "start": 1.0, "end": 2.0, "speaker": "양준호", "text": "야, 뭐해?"}]}
    tr_b = {"lines": [{"id": "L-001", "start": 1.0, "end": 2.0, "speaker": "정선혁", "text": "야, 뭐해?"}]}
    tr_c = {"lines": [{"id": "L-001", "start": 1.0, "end": 2.0, "speaker": "정선혁", "text": "야, 뭐 해?"}]}
    kw = dict(eye="", guide=None, digest=None, material_note="", seq_hook=True, n_versions=14)
    assert outline_fingerprint(index, tr_a, [], **kw) == outline_fingerprint(index, tr_b, [], **kw)     # 화자만 다름 → 같은 대본
    assert outline_fingerprint(index, tr_a, [], **kw) != outline_fingerprint(index, tr_c, [], **kw)     # 대사가 다르면 다른 재료
    va = {"items": [{"type": "S", "line_ids": ["L-001"], "speaker": "양준호", "text": "야, 뭐해?"}], "title": "t"}
    vb = {"items": [{"type": "S", "line_ids": ["L-001"], "speaker": "정선혁", "text": "야, 뭐해?"}], "title": "t"}
    assert script_fingerprint(va, [], {}, None, None, 60) == script_fingerprint(vb, [], {}, None, None, 60)
    assert digest_sha(index, tr_a, None) == digest_sha(index, tr_b, None) and digest_sha(index, tr_a, None) != digest_sha(index, tr_c, None)
    assert "speaker" not in speaker_neutral(tr_a)["lines"][0] and tr_a["lines"][0]["speaker"] == "양준호"   # 원본 불변
