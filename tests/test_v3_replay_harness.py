"""V3-M0 리플레이 하네스 회귀 가드.

전부 합성 데이터(tmp_path) — 네트워크·DB 없이 돈다. 지표 정의(원안=checkpoint_story.clips
· 최종=edit_plan.timeline · 쪼갬/줄어듦)와 결정성(같은 입력 → 바이트 동일)을 고정한다.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.replay import golden as G
from app.replay import loader, metrics, report
from app.replay.fetch import storage_bundle_prefix
from scripts.replay_harness import main as cli_main


# ── 합성 아카이브 재료 ──────────────────────────────────────────────────────

def _story_clip(s, e, chunk=0, cand=0):
    return {"role": "build", "start_sec": s, "end_sec": e,
            "chunk_index": chunk, "candidate_index": cand}


def _tl(spans):
    return [{"role": "build", "clip_start_sec": s, "clip_end_sec": e,
             "subtitle": "", "use_original_audio": True} for s, e in spans]


def _run_record(key, story_spans, final_spans, chunks=None, raw=None):
    chunks = chunks or [0] * len(story_spans)
    return {
        "key": key, "run_id": key, "created_at": "2026-08-01T00:00:00+00:00",
        "git_sha": "deadbeef",
        "story_clips": [_story_clip(s, e, chunk=c)
                        for (s, e), c in zip(story_spans, chunks)],
        "raw_response": raw or {},
        "timeline": _tl(final_spans),
    }


# ── 기본 지표 ───────────────────────────────────────────────────────────────

def test_clip_span_three_shapes():
    assert metrics.clip_span({"start_sec": 1.0, "end_sec": 3.5}) == (1.0, 3.5)
    assert metrics.clip_span({"clip_start_sec": 2.0, "clip_end_sec": 4.0}) == (2.0, 4.0)
    # editor_timeline/v1 — dur_sec 만 있는 행
    assert metrics.clip_span({"start_sec": 10.0, "end_sec": None, "dur_sec": 5.0}) == (10.0, 15.0)
    assert metrics.clip_len({"start_sec": 5.0, "end_sec": 4.0}) == 0.0  # 역전은 0


def test_sentence_final():
    assert metrics.sentence_final("끝났다.")
    assert metrics.sentence_final("정말?!")
    assert metrics.sentence_final('"…했다."')      # 닫는 따옴표 뒤 판정
    assert not metrics.sentence_final("말하는 중에")
    assert not metrics.sentence_final("")


def test_percentile_no_interpolation():
    vals = [float(v) for v in range(1, 11)]        # 1..10
    assert metrics.percentile(vals, 0.9) == 9.0    # sorted[int(0.9*9)] = idx8
    assert metrics.percentile([], 0.9) is None


def test_run_metrics_split_and_gaps():
    m = metrics.run_metrics(_run_record(
        "r1", [(100, 120), (300, 330)], [(100, 110), (112, 120), (300, 330)],
        chunks=[0, 1]))
    assert (m["orig_n"], m["final_n"]) == (2, 3)
    assert m["silence_split"] and not m["shrunk"]
    assert m["orig_gaps"] == [180.0]
    assert m["spans_multiple_chunks"]
    assert m["orig_total"] == 50.0 and m["final_total"] == 48.0
    assert m["usable"]


def test_run_metrics_shrunk_and_unusable():
    m = metrics.run_metrics(_run_record("r2", [(0, 10), (20, 30), (40, 50)], [(0, 10)]))
    assert m["shrunk"] and not m["silence_split"]
    empty = metrics.run_metrics(_run_record("r3", [], [(0, 10)]))
    assert not empty["usable"]


def test_raw_storyline_count_and_ext():
    raw = {"selected_storyline": {"storyline": {
        "hook": {"context_extended": True},
        "build": [{"context_extended": False}, {}],
        "payoff": {"context_extended": True}}}}
    assert metrics.raw_storyline_clip_count(raw) == 4
    assert metrics.raw_storyline_ext_requested(raw) == 2
    assert metrics.raw_storyline_clip_count({"shorts_type": "highlight"}) == 1


# ── 번들 지표 — 문장 절단 · 맥락 확장 ───────────────────────────────────────

def test_bundle_boundary_cut_and_ext():
    rec = _run_record("r4", [(100, 120)], [(100, 110), (112, 120)])
    rec["story_clips"][0]["candidate_index"] = 7
    rec["bundle"] = {
        # 편집 타임라인 좌표: 첫 클립 경계는 10.0 — 9.5~10.5 cue 가 경계에 걸친다
        "subtitle_segments": [
            {"start_sec": 0.0, "end_sec": 2.0, "text": "안녕하세요."},
            {"start_sec": 9.5, "end_sec": 10.5, "text": "말이 잘리는 중"},
            {"start_sec": 15.0, "end_sec": 17.0, "text": "끝맺지 못한 말"},
        ],
        "all_candidates": [{
            "chunk_index": 0, "candidate_index": 7,
            "start_sec": 102.0, "end_sec": 118.0,
            "context_extension": {"needed": True,
                                  "extended_start_sec": 95.0, "extended_end_sec": 125.0},
        }],
    }
    m = metrics.run_metrics(rec)
    assert m["boundary_n"] == 1 and m["boundary_cut_n"] == 1
    assert m["last_cue_sentence_final"] is False
    # 제안 = (102-95)+(125-118)=14 · 실적용 = (102-100)+(120-118)=4
    assert m["ext_matched_clips"] == 1
    assert m["ext_proposed_sec"] == 14.0
    assert m["ext_applied_sec"] == 4.0


# ── 집계 — §3 분포 표 · 발견 2/3 ───────────────────────────────────────────

def _mini_population():
    rows = [
        _run_record("a1", [(0, 48)], [(0, 48)]),                       # 원안1 통짜 48s
        _run_record("a2", [(0, 50)], [(0, 20), (25, 50)]),             # 원안1 쪼개짐
        _run_record("b1", [(0, 10), (110, 130)], [(0, 10), (110, 130)], chunks=[0, 1]),
        _run_record("c1", [(0, 10), (20, 30), (40, 55)], [(0, 10)]),   # 줄어듦
        _run_record("z_skip", [], []),                                  # 집계 불가
    ]
    return [metrics.run_metrics(r) for r in rows]


def test_aggregate_buckets_and_findings():
    agg = metrics.aggregate(_mini_population())
    assert (agg["n_rows"], agg["n_usable"], agg["n_skipped"]) == (5, 4, 1)
    assert agg["skipped_keys"] == ["z_skip"]
    b1 = agg["buckets"]["1"]
    assert (b1["n"], b1["split_n"], b1["shrunk_n"]) == (2, 1, 0)
    assert agg["buckets"]["2"]["n"] == 1
    assert agg["buckets"]["3"]["shrunk_n"] == 1
    # 발견2 — 원안1 2편 중 통짜 1편(48s)
    f2 = agg["finding2"]
    assert (f2["orig1_n"], f2["solo_n"], f2["solo_len_mean"]) == (2, 1, 48.0)
    # 발견3 — 갭: b1 [100] · c1 [10, 10] → 중앙 10, 여러 청크 1/2
    f3 = agg["finding3"]
    assert f3["gap_median"] == 10.0
    assert (f3["multi_chunk_n"], f3["multi_run_n"]) == (1, 2)


# ── 사람 기준선 쌍 — baseline 우선 · edit_plan 폴백 오염 판정 ───────────────

def test_human_pairs_baseline_and_contamination():
    baselines = [
        {"run_id": "r_base", "ai_clips": [{"start_sec": 0, "end_sec": 20}],
         "human_edits": [{"created_at": "t1",
                          "clips": [{"start_sec": 0, "end_sec": 8},
                                    {"start_sec": 10, "end_sec": 18}]}]},
        # baseline 없음 → run 의 edit_plan(final_lens)로 폴백. 사람 clips 와 같으면 오염.
        {"run_id": "r_dirty", "ai_clips": [],
         "human_edits": [{"created_at": "t2",
                          "clips": [{"start_sec": 5, "end_sec": 10},
                                    {"start_sec": 20, "end_sec": 30}]}]},
        {"run_id": "r_noedit", "ai_clips": [{"start_sec": 0, "end_sec": 5}],
         "human_edits": []},                                  # 편집 없음 → 쌍 아님
    ]
    idx = {"r_dirty": metrics.run_metrics(
        _run_record("r_dirty", [(0, 15)], [(5, 10), (20, 30)]))}
    h = metrics.human_pairs(baselines, idx)
    assert h["pairs_n"] == 2
    assert h["contaminated_n"] == 1
    assert h["increased_n"] == 1 and h["same_n"] == 1
    assert h["ai_len_median"] is not None


def test_contamination_check():
    """edit_plan 은 편집 재렌더가 덮는다 — baseline 클립 수와 다르면 오염으로 센다."""
    baselines = [
        {"run_id": "clean", "ai_clips": [{"start_sec": 0, "end_sec": 5},
                                         {"start_sec": 6, "end_sec": 9}], "human_edits": []},
        {"run_id": "dirty", "ai_clips": [{"start_sec": 0, "end_sec": 5}], "human_edits": []},
        {"run_id": "no_run", "ai_clips": [{"start_sec": 0, "end_sec": 5}], "human_edits": []},
    ]
    idx = {
        "clean": metrics.run_metrics(_run_record("clean", [(0, 9)], [(0, 5), (6, 9)])),
        "dirty": metrics.run_metrics(_run_record("dirty", [(0, 9)], [(0, 3), (4, 9)])),
    }
    c = metrics.contamination_check(baselines, idx)
    assert (c["checked"], c["final_overwritten"], c["runs"]) == (2, 1, ["dirty"])


def test_human_direction_verdicts():
    """사람 대조는 방향 판정 — §3 스스로 '방향만 참고'로 못박은 표다."""
    agg = metrics.aggregate(_mini_population())
    human = {"pairs_n": 5, "ai_n_mean": 3.0, "hu_n_mean": 6.0,
             "ai_len_median": 20.0, "hu_len_median": 7.0,
             "increased_n": 4, "same_n": 1, "decreased_n": 0,
             "ai_total": 100.0, "hu_total": 120.0, "contaminated_n": 0}
    rows = [r for r in report.compare_reference(agg, human) if r["cell"].startswith("사람대조")]
    assert len(rows) == 4 and all(r["verdict"] == "MATCH" for r in rows)
    human_bad = dict(human, hu_len_median=30.0)      # 길이 단축 방향이 깨지면 EXPLAIN
    rows = [r for r in report.compare_reference(agg, human_bad)
            if r["cell"] == "사람대조 방향: 구간 길이 단축"]
    assert rows[0]["verdict"] == "EXPLAIN" and rows[0]["note"]


# ── 로더 — 두 레이아웃 · 깨진 파일 관용 ─────────────────────────────────────

def _write_snapshot(root, key="clip-1", human_edited=False):
    (root / "runs").mkdir(parents=True, exist_ok=True)
    doc = {
        "clip_id": key, "run_id": f"run_{key}", "created_at": "2026-08-01T00:00:00+00:00",
        "git_sha": "cafebabe", "human_edited": human_edited,
        "checkpoint_story": {"clips": [_story_clip(0, 20)], "raw_response": {}},
        "edit_plan": {"timeline": _tl([(0, 10), (12, 20)])},
        "run_log": {"job_id": f"run_{key}",
                    "provenance": {"git_sha": "cafebabe", "config": {"app": {}}}},
    }
    (root / "runs" / f"{key}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_loader_snapshot_layout(tmp_path):
    _write_snapshot(tmp_path, "clip-1")
    _write_snapshot(tmp_path, "clip-2", human_edited=True)
    (tmp_path / "runs" / "broken.json").write_text("{잘림", encoding="utf-8")
    (tmp_path / "baselines.json").write_text("[]", encoding="utf-8")
    arc = loader.load_archive(tmp_path)
    assert arc["layout"] == "snapshot"
    assert [r["key"] for r in arc["records"]] == ["clip-1", "clip-2"]
    assert arc["records"][1]["human_edited"] is True
    assert len(arc["broken"]) == 1 and "broken.json" in arc["broken"][0]


def _write_jobdir(root, name="가왕쇼_abcd1234", with_bundle=False):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "edit_plan.json").write_text(
        json.dumps({"timeline": _tl([(0, 10), (12, 20), (30, 40)])}), encoding="utf-8")
    (d / "checkpoint_story.json").write_text(
        json.dumps({"clips": [_story_clip(0, 20), _story_clip(30, 40)],
                    "raw_response": {}}), encoding="utf-8")
    (d / "run_log.json").write_text(
        json.dumps({"job_id": name, "provenance": {"git_sha": "beefbeef"}}),
        encoding="utf-8")
    if with_bundle:
        (d / "subtitle_segments.json").write_text(json.dumps(
            [{"start_sec": 0.0, "end_sec": 2.0, "text": "끝났다."}]), encoding="utf-8")
    return d


def test_loader_jobdir_layout(tmp_path):
    _write_jobdir(tmp_path, "잡A", with_bundle=True)
    _write_jobdir(tmp_path, "잡B")
    arc = loader.load_archive(tmp_path)
    assert arc["layout"] == "jobdir"
    assert [r["key"] for r in arc["records"]] == ["잡A", "잡B"]
    assert arc["records"][0]["bundle"]["subtitle_segments"]
    m = metrics.run_metrics(arc["records"][0])
    assert m["last_cue_sentence_final"] is True
    # 루트 자체가 job 디렉토리 하나여도 된다
    solo = loader.load_archive(tmp_path / "잡B")
    assert len(solo["records"]) == 1


def test_loader_unknown_layout(tmp_path):
    (tmp_path / "빈방").mkdir()
    with pytest.raises(FileNotFoundError):
        loader.load_archive(tmp_path)


# ── 리포트 — 결정성 · §3 대조 ───────────────────────────────────────────────

def test_report_deterministic_and_reference_table():
    per_run = _mini_population()
    agg = metrics.aggregate(per_run)
    meta = {"path": "X", "layout": "snapshot", "window": {}, "broken": []}
    doc1 = report.build_metrics_doc(meta, per_run, agg, None)
    doc2 = report.build_metrics_doc(meta, list(reversed(per_run)), agg, None)
    assert report.to_json(doc1) == report.to_json(doc2)          # 입력 순서 무관
    md = report.render_markdown(doc1)
    assert "원안 구간 수 분포" in md and "§3 재현 검증" in md
    # 합성 5편은 §3(614편)와 다르므로 EXPLAIN 이 떠야 하고, 사유가 비면 안 된다
    explains = [r for r in doc1["reference_check"] if r["verdict"] == "EXPLAIN"]
    assert explains and all(r["note"] for r in explains)


def test_reference_match_verdict():
    # §3 값을 그대로 흉내낸 집계는 전 셀 MATCH 여야 한다(관용치 검증)
    agg = metrics.aggregate(_mini_population())
    agg["n_usable"] = 614
    for b, ref in report.REFERENCE_S3["buckets"].items():
        agg["buckets"][b] = dict(ref)
    agg["finding2"] = dict(report.REFERENCE_S3["finding2"])
    agg["finding3"] = {"gap_median": 100.0, "gap_p90": 736.0, "multi_chunk_pct": 51.0,
                       "multi_run_n": 423, "multi_chunk_n": 217}
    rows = report.compare_reference(agg, None)
    assert all(r["verdict"] == "MATCH" for r in rows), \
        [r for r in rows if r["verdict"] != "MATCH"]


def test_diff_metrics_fake_v3(tmp_path):
    """합격 기준: v3 산출 디렉토리를 가짜 데이터로 넣으면 diff 리포트가 나온다."""
    _write_jobdir(tmp_path / "v3_out", "v3잡1")
    per_run = _mini_population()
    agg = metrics.aggregate(per_run)
    a = report.build_metrics_doc({"path": "cur", "layout": "snapshot",
                                  "window": {}, "broken": []}, per_run, agg, None)
    arc = loader.load_archive(tmp_path / "v3_out")
    pr_b = [metrics.run_metrics(r) for r in arc["records"]]
    b = report.build_metrics_doc({"path": "v3", "layout": "jobdir",
                                  "window": {}, "broken": []}, pr_b, metrics.aggregate(pr_b), None)
    md = report.diff_metrics(a, b, label_a="현행", label_b="v3")
    assert "현행" in md and "v3" in md and "편당 최종 구간 평균" in md
    assert "설계 목표" in md


# ── 골든 케이스 ─────────────────────────────────────────────────────────────

def test_captions_to_segments_gap_split():
    caps = {"narration": [{"s": 0.0, "e": 2.0, "text": "a"}],
            "dialogue": [{"s": 2.2, "e": 4.0, "text": "b"},     # 갭 0.2 → 붙는다
                         {"s": 5.0, "e": 7.0, "text": "c"}],     # 갭 1.0 → 끊긴다
            "labels": [{"s": 0.0, "e": 9.0, "text": "(장식)"}]}  # 경계 재료 아님
    g = G.captions_to_segments(caps, gap_sec=0.4)
    assert g["schema"] == G.GOLDEN_SCHEMA and g["derived"] is True
    assert g["segments"] == [{"start_sec": 0.0, "end_sec": 4.0},
                             {"start_sec": 5.0, "end_sec": 7.0}]


def test_score_cuts_hand_computed():
    golden = {"schema": G.GOLDEN_SCHEMA, "space": "edited", "derived": False,
              "segments": [{"start_sec": 0, "end_sec": 10},
                           {"start_sec": 20, "end_sec": 30}]}
    pred = _tl([(0, 10), (40, 50)])
    s = G.score_cuts(pred, golden, tol_sec=0.5)
    assert (s["pred_n"], s["gold_n"], s["matched_n"]) == (2, 2, 1)
    assert s["mean_iou"] == 1.0                       # (0,10) 완전 일치 1쌍
    # 경계: pred {0,10,40,50} 중 정답 {0,10,20,30} 근접 2개 → precision 0.5
    assert s["boundary_precision"] == 0.5 and s["boundary_recall"] == 0.5
    assert s["unmatched_gold"] == [1] and s["unmatched_pred"] == [1]


def test_load_golden_rejects_wrong_schema(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schema": "다른것", "segments": [{}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        G.load_golden(p)


def test_fh_captions_example_derives(tmp_path):
    """1호 정답지 재료(포핸즈 자막 파일)와 같은 모양이 그대로 변환되는지."""
    caps = {"title": ["x", "y"],
            "narration": [{"s": 0.2, "e": 2.2, "text": "n1"}],
            "dialogue": [{"s": 4.78, "e": 7.18, "text": "d1"},
                         {"s": 7.18, "e": 9.76, "text": "d2"}]}
    g = G.captions_to_segments(caps)
    assert len(g["segments"]) == 2                    # 2.2→4.78 갭에서 끊김


# ── 배선 — Storage 키 규약 · CLI 스모크 ─────────────────────────────────────

def test_storage_bundle_prefix_pinned():
    # ves 어댑터 base.storage_key 와 같은 규칙(sha256 16자) — 갈리면 번들을 못 찾는다
    rid = "가왕쇼_58adfd02"
    assert storage_bundle_prefix(rid) == hashlib.sha256(
        rid.encode("utf-8")).hexdigest()[:16]
    assert len(storage_bundle_prefix("x")) == 16


def test_cli_report_and_diff_smoke(tmp_path, capsys):
    _write_jobdir(tmp_path / "arc", "잡1", with_bundle=True)
    out = tmp_path / "out"
    assert cli_main(["report", "--archive", str(tmp_path / "arc"),
                     "--out", str(out)]) == 0
    m1 = (out / "metrics.json").read_bytes()
    assert cli_main(["report", "--archive", str(tmp_path / "arc"),
                     "--out", str(out)]) == 0
    assert (out / "metrics.json").read_bytes() == m1   # 결정성 — 반복 실행 바이트 동일
    assert (out / "report.md").exists()
    assert cli_main(["diff", "--a", str(out / "metrics.json"),
                     "--b", str(tmp_path / "arc"), "--out", str(tmp_path / "d.md")]) == 0
    assert "편수" in (tmp_path / "d.md").read_text(encoding="utf-8")


def test_cli_golden_smoke(tmp_path, capsys):
    d = _write_jobdir(tmp_path, "잡G")
    gpath = tmp_path / "golden.json"
    gpath.write_text(json.dumps({
        "schema": G.GOLDEN_SCHEMA, "space": "edited",
        "segments": [{"start_sec": 0, "end_sec": 10}]}), encoding="utf-8")
    assert cli_main(["golden", "--plan", str(d), "--golden", str(gpath)]) == 0
    out = capsys.readouterr().out
    assert '"matched_n"' in out
