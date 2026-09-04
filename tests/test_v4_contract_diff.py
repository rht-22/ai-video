"""V4-M1 합격선 — 바깥 계약 대조 도구 회귀 가드.

이 파일이 못박는 것은 판정 자체가 아니라 **판정의 경계** 셋이다:

  ① 없는 파일 ≠ 모양이 틀린 파일 — 전자는 skipped(그 단계를 안 돌았을 수 있다),
     후자는 violation. 섞으면 `--stop-after probe` 로 멈춘 잡이 전부 빨갛게 되고
     사람이 이 표를 안 보게 된다.
  ② 빈 목록 ≠ 키 없는 항목 — 후보 0인 잡·연출 없는 편을 위반으로 세지 않는다.
  ③ **v3 모양 checkpoint_story 는 반드시 잡힌다** — 이 도구가 존재하는 이유다
     (app/localize/apply.py:182~186 이 읽는 `variants[].title_text` 를 v3 는 안 낸다).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import v4_contract_diff as M


# ── 정상 잡 픽스처 ──────────────────────────────────────────────────────────

def _clip(start=10.0, end=25.0, role="hook"):
    # StoryClip(app/modules/story_builder.py:7~12)의 필수 5필드.
    return {"role": role, "start_sec": start, "end_sec": end,
            "subtitle": "대사", "use_original_audio": True}


def _timeline_clip(start=10.0, end=25.0, role="hook"):
    return {"role": role, "clip_start_sec": start, "clip_end_sec": end,
            "subtitle": "대사", "use_original_audio": True,
            "reframe": {"mode": "center"}}


def good_docs() -> dict:
    """모든 계약을 만족하는 문서 지도(실제 v1 산출 모양을 줄인 것)."""
    return {
        "run_log.json": {
            "schema": "run_log/v1", "pipeline": "v4", "job_id": "작품_abcd1234",
            "provenance": {"git_sha": "deadbee", "config": {"app": {}}},
            "input": {"video_path": "/src/ep01.mp4", "work_title": "작품"},
            "steps": [{"step": "init"}],
        },
        "checkpoint_story.json": {
            "variants": [
                {"title_text": "윗줄\n아랫줄", "clips": [_clip()], "score": 1.0},
                {"title_text": "2위 제목", "clips": [_clip(60.0, 90.0, "payoff")]},
            ],
            "title_text": "윗줄\n아랫줄",
            "clips": [_clip()],
        },
        "edit_plan.json": {
            "layout": {"top_title": "윗줄\n아랫줄", "bottom_label": "작품",
                       "canvas": "1080x1920"},
            "timeline": [_timeline_clip()],
        },
        "subtitle_segments.json": [
            {"start_sec": 0.0, "end_sec": 1.2, "text": "안녕"},
            {"start_sec": 1.2, "end_sec": 2.0, "text": "하세요"},
        ],
        "checkpoint_resources.json": {
            "tts_cue_files": [
                {"cue_index": 0, "path": "/job/tts_0.mp3",
                 "cue": {"text": "내레이션", "start_sec": 3.0, "end_sec": 6.0,
                         "voice": "ko_female", "speed": "normal"}},
            ],
            "video_speed": 1.0,
        },
        # v4 가 실제로 쓰는 연출 문서(v3 Stage 4 어휘) — `checkpoint_style.json` 과
        # 이름도 모양도 다른 별개 파일이다.
        "style.json": {"design": {"aspect_ratio": "1:1", "subtitle_size": 58},
                       "beats": [], "labels": [], "diff": {}, "notes": ""},
        "checkpoint_style.json": {
            "schema": "style_plan/v1",
            "texts": [{"text": "쿵!", "source_time_sec": 42.0}],
            "title_segments": [{"text": "아랫줄", "start_sec": 0.0, "end_sec": 20.0}],
            "title_fixed": "윗줄",
        },
        "grid.json": {
            "schema": "grid/v1",
            "source": {"duration_sec": 4020.0},
            "scene_cuts": [0.0, 12.5],
            "silence": [[3.0, 3.6]],
            "arousal": [],
            "span_candidates": [
                {"id": "sp0000", "t_in": 0.0, "t_out": 3.0, "is_audio": True,
                 "time_authority": "stt", "text": "안녕"},
            ],
            "words": [{"t0": 0.1, "t1": 0.4, "text": "안녕", "prob": 0.9}],
        },
        "checkpoint_candidates.json": {
            "schema": "v4_candidates/v1",
            "candidates": [
                {"id": "c01", "segments": [{"start_sec": 10.0, "end_sec": 55.0,
                                            "quote": "대사"}]},
            ],
            "approved": ["c01"],
        },
    }


def write_job(tmp_path: Path, docs: dict, name: str = "job") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    for fn, doc in docs.items():
        (d / fn).write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return d


# ── 계약 표 자체의 건전성 ───────────────────────────────────────────────────

def test_every_contract_key_parses():
    """계약 표의 오타가 조용히 '검사 안 함'이 되면 이 도구가 있는 이유가 사라진다."""
    for name, keys in M.CONTRACTS.items():
        assert keys, f"{name}: 빈 계약"
        for k in keys:
            optional, steps = M.parse_key_path(k)
            assert steps, f"{name}/{k}"
            assert isinstance(optional, bool)


def test_contract_files_match_the_plan_section_6():
    """기획서 §6 표가 정본이다 — 표에 없는 파일을 계약으로 들고 있으면 근거가 없다."""
    plan = Path(__file__).resolve().parents[1] / "docs" / "v4" / "v4-plan.md"
    text = plan.read_text(encoding="utf-8")
    body = text.split("## 6. 바깥 계약", 1)[1].split("## 7.", 1)[0]
    for name in M.CONTRACTS:
        stem = name[: -len(".json")]
        assert f"`{stem}.json`" in body or f"`{stem}" in body, \
            f"{name} 이 기획서 §6 표에 없다 — 근거 없는 계약"


def test_numbered_siblings_share_the_base_contract():
    """v4 는 승인 편을 여럿 낸다(O7) — 2위 이하가 다른 모양이면 그 편만 조용히 깨진다."""
    assert M.contract_keys_for("edit_plan_2.json") == M.CONTRACTS["edit_plan.json"]
    assert M.contract_keys_for("checkpoint_style_3.json") == M.CONTRACTS["checkpoint_style.json"]
    # v3 훅 변형은 승인 편이 아니다(app/replay/loader.py 와 같은 판별).
    assert M.contract_keys_for("edit_plan_variant_2.json") is None
    assert M.contract_keys_for("checkpoint_gemini.json") is None


# ── 키 경로 문법 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("layout.top_title", (False, (("field", "layout"), ("field", "top_title")))),
    ("variants[].title_text", (False, (("each", "variants"), ("field", "title_text")))),
    ("variants[].clips[].start_sec",
     (False, (("each", "variants"), ("each", "clips"), ("field", "start_sec")))),
    ("tts_cue_files[].cue.text",
     (False, (("each", "tts_cue_files"), ("field", "cue"), ("field", "text")))),
    ("[].start_sec", (False, (("each", ""), ("field", "start_sec")))),
    ("?texts[].text", (True, (("each", "texts"), ("field", "text")))),
])
def test_parse_key_path(path, expected):
    assert M.parse_key_path(path) == expected


@pytest.mark.parametrize("bad", ["", "?", "a[0].b", "[]x", "a.[]b"])
def test_parse_key_path_rejects_garbage(bad):
    with pytest.raises(ValueError):
        M.parse_key_path(bad)


# ── 판정 경계 ──────────────────────────────────────────────────────────────

def test_good_job_passes(tmp_path):
    job = write_job(tmp_path, good_docs())
    docs, present, unread = M.load_job(job)
    res = M.check_job(docs, present=present, unreadable=unread, job=str(job))
    assert res["ok"], [f for f in res["files"] if f["violations"]]
    assert res["counts"]["skipped"] == 0
    assert res["counts"]["violation"] == 0


def test_missing_key_fails_and_names_the_key(tmp_path):
    docs = good_docs()
    del docs["edit_plan.json"]["layout"]["top_title"]
    job = write_job(tmp_path, docs)
    d, p, u = M.load_job(job)
    res = M.check_job(d, present=p, unreadable=u, job=str(job))

    assert not res["ok"]
    ep = next(f for f in res["files"] if f["file"] == "edit_plan.json")
    assert ep["status"] == "violation"
    assert [v["key"] for v in ep["violations"]] == ["layout.top_title"]
    # 보고에 **키 이름**이 나와야 한다 — 개수만 적으면 못 고친다.
    report = M.render_report(res)
    assert "layout.top_title" in report


def test_missing_key_inside_a_list_item_reports_the_index(tmp_path):
    docs = good_docs()
    del docs["checkpoint_story.json"]["variants"][1]["title_text"]
    job = write_job(tmp_path, docs)
    d, p, u = M.load_job(job)
    res = M.check_job(d, present=p, unreadable=u, job=str(job))
    cs = next(f for f in res["files"] if f["file"] == "checkpoint_story.json")
    v = next(v for v in cs["violations"] if v["key"] == "variants[].title_text")
    assert v["where"] == "variants[1].title_text"
    assert "variants[1]" in M.render_report(res)


def test_absent_file_is_skipped_not_a_violation(tmp_path):
    """`--stop-after probe` 로 멈춘 잡 — 렌더 산출이 아직 없는 것은 결함이 아니다."""
    docs = good_docs()
    partial = {k: docs[k] for k in ("run_log.json", "grid.json")}
    job = write_job(tmp_path, partial)
    d, p, u = M.load_job(job)
    res = M.check_job(d, present=p, unreadable=u, job=str(job))

    assert res["ok"]
    statuses = {f["file"]: f["status"] for f in res["files"]}
    assert statuses["checkpoint_story.json"] == "skipped"
    assert statuses["edit_plan.json"] == "skipped"
    assert statuses["run_log.json"] == "ok"
    ep = next(f for f in res["files"] if f["file"] == "edit_plan.json")
    assert "파일 없음" in ep["reason"]


def test_broken_json_is_unreadable_not_skipped(tmp_path):
    """파일이 있는데 못 읽는 것은 '안 돌았다'와 완전히 다른 사건이다."""
    job = write_job(tmp_path, good_docs())
    (job / "edit_plan.json").write_text("{ 이건 JSON 이 아니다", encoding="utf-8")
    d, p, u = M.load_job(job)
    res = M.check_job(d, present=p, unreadable=u, job=str(job))

    assert not res["ok"]
    ep = next(f for f in res["files"] if f["file"] == "edit_plan.json")
    assert ep["status"] == "unreadable"
    assert ep["status"] != "skipped"
    assert "읽기 실패" in M.render_report(res)


def test_empty_list_passes(tmp_path):
    """항목이 없는 것과 항목의 키가 없는 것은 다르다 — 후보 0인 잡을 빨갛게 만들지 않는다."""
    docs = good_docs()
    docs["checkpoint_story.json"]["variants"] = []
    docs["checkpoint_story.json"]["clips"] = []
    docs["edit_plan.json"]["timeline"] = []
    docs["subtitle_segments.json"] = []
    docs["checkpoint_resources.json"]["tts_cue_files"] = []
    docs["grid.json"]["words"] = []
    docs["grid.json"]["span_candidates"] = []
    docs["checkpoint_candidates.json"]["candidates"] = []
    job = write_job(tmp_path, docs)
    d, p, u = M.load_job(job)
    assert M.check_job(d, present=p, unreadable=u)["ok"]


def test_optional_container_is_only_optional_when_absent():
    """연출에 효과 텍스트가 없는 편은 통과하되, **있는데 문구가 없으면** 잡는다."""
    base = {"schema": "style_plan/v1"}
    assert M.check_document("checkpoint_style.json", base)["status"] == "ok"
    bad = {"schema": "style_plan/v1", "texts": [{"x": 0.5}]}
    r = M.check_document("checkpoint_style.json", bad)
    assert r["status"] == "violation"
    assert [v["key"] for v in r["violations"]] == ["?texts[].text"]


def test_wrong_type_is_distinguished_from_missing():
    doc = {"variants": {"0": {"title_text": "a"}}}
    r = M.check_key(doc, "variants[].title_text")
    assert r["verdict"] == "not_list"
    r2 = M.check_key({"layout": "문자열"}, "layout.top_title")
    assert r2["verdict"] == "not_object"


def test_check_functions_do_not_mutate_input():
    docs = good_docs()
    snapshot = json.dumps(docs, ensure_ascii=False, sort_keys=True)
    M.check_job(docs)
    M.diff_key_sets(docs, docs)
    assert json.dumps(docs, ensure_ascii=False, sort_keys=True) == snapshot


def test_report_is_deterministic(tmp_path):
    job = write_job(tmp_path, good_docs())
    d, p, u = M.load_job(job)
    a = M.render_report(M.check_job(d, present=p, unreadable=u, job="x"))
    d2, p2, u2 = M.load_job(job)
    b = M.render_report(M.check_job(d2, present=p2, unreadable=u2, job="x"))
    assert a == b


# ── 🛑 이 도구의 존재 이유: v3 모양 checkpoint_story ────────────────────────

V3_STORY_DOC = {
    # app/v3/pipeline.py:637 이 실제로 쓰는 모양 — 지문 래퍼 + story 안에 title{line1,line2}.
    "fingerprint": "0123456789abcdef",
    "story": {
        "schema": "v3_story/v1",
        "template": "recap_dialogue",
        "title": {"line1": "상황 한 줄", "line2": "펀치 한 줄"},
        "beats": [{"role": "hook", "time": {"start": "00:00:10.000",
                                            "end": "00:00:25.000"},
                   "span_ids": ["sp0001"]}],
        "narration_cues": [],
    },
}


def test_v3_shaped_checkpoint_story_is_caught(tmp_path):
    """v3 가 현지화를 깨뜨린 그 모양 — `variants[].title_text` 도 `clips` 도 없다.

    app/localize/apply.py:182~186 은 `variants` 가 없으면 최상위 `title_text` 를 쓰는데
    v3 문서에는 그것도 없어서 JP 판에 한국어 제목이 그대로 번인된다. 파일 이름이 같으니
    아무도 죽지 않는다 — 그래서 **기계가 키를 봐야** 한다.
    """
    docs = good_docs()
    docs["checkpoint_story.json"] = V3_STORY_DOC
    job = write_job(tmp_path, docs)
    d, p, u = M.load_job(job)
    res = M.check_job(d, present=p, unreadable=u, job=str(job))

    assert not res["ok"]
    cs = next(f for f in res["files"] if f["file"] == "checkpoint_story.json")
    assert cs["status"] == "violation"        # skipped 도 unreadable 도 아니다
    missing = {v["key"] for v in cs["violations"]}
    assert "variants[].title_text" in missing
    assert "variants[].clips" in missing
    assert "title_text" in missing
    assert "clips" in missing

    report = M.render_report(res)
    assert "variants[].title_text" in report


def test_v3_shape_is_not_rescued_by_the_v3_only_keys(tmp_path):
    """`title.line1` 이 있다고 계약을 만족하지 않는다 — 이름이 다르면 아무도 못 읽는다."""
    r = M.check_document("checkpoint_story.json", V3_STORY_DOC["story"])
    assert r["status"] == "violation"
    assert "title.line1" not in {v["key"] for v in r["violations"]}


# ── --against 키 집합 diff ─────────────────────────────────────────────────

def test_observed_key_paths_uses_the_contract_syntax():
    doc = {"layout": {"top_title": "a"}, "timeline": [{"role": "hook"},
                                                     {"reframe": {"mode": "center"}}]}
    got = M.observed_key_paths(doc)
    # 목록 항목의 키는 **합집합**이다 — 첫 항목만 보면 diff 가 실행마다 달라진다.
    assert {"layout", "layout.top_title", "timeline",
            "timeline[].role", "timeline[].reframe",
            "timeline[].reframe.mode"} <= got


def test_observed_key_paths_of_root_list():
    got = M.observed_key_paths([{"start_sec": 0.0, "text": "a"}])
    assert got == {"[].start_sec", "[].text"}


def test_diff_key_sets_reports_both_directions(tmp_path):
    a_docs = good_docs()
    b_docs = good_docs()
    # v1 잡에만 있는 키(reframe) ↔ v4 잡이 더한 키(span_ids) — 둘 다 정상이다.
    b_docs["edit_plan.json"]["timeline"][0].pop("reframe")
    b_docs["edit_plan.json"]["timeline"][0]["span_ids"] = ["sp0001"]
    diff = M.diff_key_sets(a_docs, b_docs, label_a="v1", label_b="v4")

    row = next(r for r in diff["files"] if r["file"] == "edit_plan.json")
    assert "timeline[].reframe" in row["only_in_v1"]
    assert "timeline[].span_ids" in row["only_in_v4"]
    assert row["only_in_v1"] == sorted(row["only_in_v1"])      # 결정성
    # 계약 필수 키는 양쪽 다 있으므로 이 칸은 비어야 한다.
    assert row["contract_only_in_a"] == []


def test_diff_flags_contract_keys_the_other_job_lacks():
    a_docs = good_docs()
    b_docs = good_docs()
    b_docs["checkpoint_story.json"] = V3_STORY_DOC
    diff = M.diff_key_sets(a_docs, b_docs, label_a="v1", label_b="v3")
    row = next(r for r in diff["files"] if r["file"] == "checkpoint_story.json")
    assert "variants[].title_text" in row["contract_only_in_a"]
    assert "clips" in row["contract_only_in_a"]


def test_diff_covers_files_present_in_only_one_job():
    a_docs = good_docs()
    b_docs = {k: v for k, v in good_docs().items() if k != "checkpoint_style.json"}
    diff = M.diff_key_sets(a_docs, b_docs, label_a="A", label_b="B")
    row = next(r for r in diff["files"] if r["file"] == "checkpoint_style.json")
    assert row["present"] == {"A": True, "B": False}
    assert row["only_in_B"] == []


# ── CLI · 종료 코드 ────────────────────────────────────────────────────────

def test_cli_exit_code_zero_on_clean_job(tmp_path, capsys):
    job = write_job(tmp_path, good_docs())
    assert M.main(["--job", str(job)]) == 0
    assert "v4 계약 대조" in capsys.readouterr().out


def test_cli_exit_code_one_on_violation(tmp_path, capsys):
    docs = good_docs()
    docs["checkpoint_story.json"] = V3_STORY_DOC
    job = write_job(tmp_path, docs)
    assert M.main(["--job", str(job)]) == 1
    assert "variants[].title_text" in capsys.readouterr().out


def test_cli_json_output_is_machine_readable(tmp_path, capsys):
    job = write_job(tmp_path, good_docs())
    assert M.main(["--job", str(job), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["job"]["ok"] is True
    assert {f["file"] for f in doc["job"]["files"]} == set(M.CONTRACTS)


def test_cli_against_runs_both_jobs_and_diffs(tmp_path, capsys):
    a = write_job(tmp_path, good_docs(), name="v1job")
    bad = good_docs()
    bad["checkpoint_story.json"] = V3_STORY_DOC
    b = write_job(tmp_path, bad, name="v3job")

    # --against 잡의 위반도 종료 코드를 올린다(어느 쪽이든 계약 위반은 위반이다).
    assert M.main(["--job", str(a), "--against", str(b), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["job"]["ok"] is True
    assert doc["against"]["ok"] is False
    row = next(r for r in doc["diff"]["files"] if r["file"] == "checkpoint_story.json")
    assert "variants[].title_text" in row["contract_only_in_a"]


def test_cli_against_diff_alone_does_not_fail(tmp_path):
    """키를 더하는 것은 정상이다(§6 '새 정보는 새 키로만') — diff 는 판정기가 아니다."""
    a = write_job(tmp_path, good_docs(), name="a")
    b_docs = good_docs()
    b_docs["edit_plan.json"]["timeline"][0]["span_ids"] = ["sp0001"]
    b_docs["run_log.json"]["v4_extra"] = {"note": "새 키"}
    b = write_job(tmp_path, b_docs, name="b")
    assert M.main(["--job", str(a), "--against", str(b)]) == 0


def test_cli_rejects_a_path_that_is_not_a_job_dir(tmp_path):
    with pytest.raises(NotADirectoryError):
        M.main(["--job", str(tmp_path / "없는디렉토리")])


def test_numbered_sibling_is_checked(tmp_path):
    """승인 2위(`edit_plan_2.json`)가 1위와 다른 모양이면 그 편만 조용히 깨진다."""
    docs = good_docs()
    job = write_job(tmp_path, docs)
    second = json.loads(json.dumps(docs["edit_plan.json"]))
    del second["layout"]["bottom_label"]
    (job / "edit_plan_2.json").write_text(json.dumps(second, ensure_ascii=False),
                                          encoding="utf-8")
    d, p, u = M.load_job(job)
    res = M.check_job(d, present=p, unreadable=u)
    assert not res["ok"]
    row = next(f for f in res["files"] if f["file"] == "edit_plan_2.json")
    assert [v["key"] for v in row["violations"]] == ["layout.bottom_label"]


def test_style_json_and_checkpoint_style_are_different_contracts():
    """한 이름에 두 모양을 얹지 않는다 — 두 파일은 서로의 키를 받지 않는다.

    v4 연출은 v3 Stage 4 어휘(`design`…)이고 E15 어휘(`schema`·`texts`…)가 아니다.
    이 둘이 같은 표를 공유하면, 모양이 다른 문서가 계약 이름으로 나가는 그 사고가
    도구를 통과한다(실제로 M7 에서 그렇게 나갈 뻔했고 이 도구가 잡았다)."""
    v3_shape = {"design": {}, "beats": [], "labels": [], "notes": ""}
    e15_shape = {"schema": "style_plan/v1"}
    assert M.check_document("style.json", v3_shape)["status"] == "ok"
    assert M.check_document("checkpoint_style.json", e15_shape)["status"] == "ok"
    # 서로 바꿔 넣으면 잡힌다 — 이것이 이 분리의 존재 이유다.
    assert M.check_document("checkpoint_style.json", v3_shape)["status"] == "violation"
    assert M.check_document("style.json", e15_shape)["status"] == "violation"
    # 2위 편도 같은 계약을 탄다(승인 편이 여럿이다 — O7).
    assert M.contract_keys_for("style_2.json") == M.CONTRACTS["style.json"]
