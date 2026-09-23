import json

from app.tikitaka import bundle as B


def _w(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_job(tmp_path, suffix="v6", fp="fp-1", video=b"final video"):
    job = tmp_path / "job"
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    st = source.stat()
    _w(job / "pipeline_identity.json", {"pipeline": "grid-review", "stt": "elevenlabs",
       "source": {"path": str(source), "size": str(st.st_size), "mtime_ns": str(st.st_mtime_ns)}})
    tts = job / "tts" / "abc.mp3"
    _w(tts, b"narration")
    work = job / f"review_{suffix}"
    _w(work / "edit_plan.json", {"timeline": [{"clip_start_sec": 1.0, "clip_end_sec": 2.0, "role": "hook"}],
                                 "layout": {"top_title": "제목", "bottom_label": "작품"}})
    _w(work / "subtitle_segments.json", [{"start_sec": 0, "end_sec": 1, "text": "대사"}])
    _w(work / "checkpoint_resources.json", {"tts_cue_files": [
        {"cue": {"text": "내레이션", "source_time_sec": 1.0}, "path": str(tts)},
        {"cue": {"text": "내레이션", "source_time_sec": 5.0}, "path": str(tts)}]})
    _w(work / "review.json", {"items": [], "preview_only": False, "render_fingerprint": fp})
    _w(work / "render_fingerprint.json", {"fingerprint": fp})
    _w(work / "final_1080x1920.mp4", video)
    _w(work / "v3_subtitles.ass", b"[Script Info]")
    _w(job / f"shorts_{suffix}.mp4", video)
    _w(job / f"review_{suffix}.json", {"render_fingerprint": fp})
    _w(job / f"grid_table_{suffix}.json", {"rows": [], "which": "plain"})
    _w(job / f"grid_table_guarded_{suffix}.json", {"rows": [], "which": "guarded"})
    _w(job / f"publish_{suffix}.json", {"title": "후배가 팀장 되자", "work": "로또", "episode": "4화",
                                        "pipeline": "grid-review"})
    return job


def test_bundle_collects_one_video_with_relative_tts_and_source(tmp_path):
    job = make_job(tmp_path)
    before = {p: p.read_bytes() for p in job.rglob("*") if p.is_file()}

    r = B.export(job, "v6")

    assert r.status == "exported"
    d = job / "videos" / "v6"
    assert (d / "shorts.mp4").read_bytes() == b"final video"
    plan = json.loads((d / "edit_plan.json").read_text())
    assert plan["input"]["video_path"] == str(tmp_path / "source.mp4")
    res = json.loads((d / "checkpoint_resources.json").read_text())
    assert [f["path"] for f in res["tts_cue_files"]] == ["tts/abc.mp3", "tts/abc.mp3"]
    assert (d / "tts" / "abc.mp3").read_bytes() == b"narration"
    assert json.loads((d / "grid_table.json").read_text())["which"] == "guarded"
    assert (d / "v3_subtitles.ass").exists()
    v = json.loads((d / "video.json").read_text())
    assert v["schema"] == "tikitaka_video/v1"
    assert (v["version"], v["tag"], v["title"], v["episode"]) == (6, None, "후배가 팀장 되자", "4화")
    assert v["video_key"] == "job/v6"
    assert v["render_fingerprint"] == "fp-1"
    assert v["source"]["matches_now"] is True
    assert v["provenance"]["render"] is None
    assert v["tts_count"] == 1
    assert v["files"]["video"]["sha256"] == B.sha256(job / "shorts_v6.mp4")
    # 원본 잡 파일은 그대로 (edit_plan 에 input 을 넣은 것은 묶음 사본뿐)
    for p, data in before.items():
        assert p.read_bytes() == data, p


def test_rerun_same_render_is_unchanged_and_new_render_archives_old(tmp_path):
    job = make_job(tmp_path)
    assert B.export(job, "v6").status == "exported"
    assert B.export(job, "v6").status == "unchanged"

    make_job(tmp_path, fp="fp-2", video=b"re-rendered")
    r = B.export(job, "v6")

    assert r.status == "exported"
    assert (job / "videos" / "v6" / "shorts.mp4").read_bytes() == b"re-rendered"
    prev = [p for p in (job / "videos").iterdir() if p.name.startswith("v6.prev_")]
    assert len(prev) == 1 and (prev[0] / "shorts.mp4").read_bytes() == b"final video"
    assert not [p for p in (job / "videos").iterdir() if ".tmp-" in p.name]


def test_refuses_when_render_data_was_archived_by_transcript_change(tmp_path):
    job = make_job(tmp_path)
    (job / "review_v6").rename(job / "review_v6.prev_123")

    r = B.export(job, "v6")

    assert r.status == "refused" and "다시 렌더" in r.detail
    assert not (job / "videos").exists()


def test_refuses_pending_invalidation_preview_and_mismatch(tmp_path):
    job = make_job(tmp_path)
    _w(job / "transcript_dependents_dirty.json", {"changed_line_ids": ["L1"]})
    assert B.export(job, "v6").status == "refused"
    (job / "transcript_dependents_dirty.json").unlink()

    _w(job / "review_v6" / "review.json", {"items": [], "preview_only": True, "render_fingerprint": "fp-1"})
    assert "미리보기" in B.export(job, "v6").detail

    _w(job / "review_v6" / "review.json", {"items": [], "preview_only": False, "render_fingerprint": "other"})
    assert "지문" in B.export(job, "v6").detail

    _w(job / "review_v6" / "review.json", {"items": [], "preview_only": False, "render_fingerprint": "fp-1"})
    _w(job / "shorts_v6.mp4", b"someone else's file")
    assert "final_1080x1920" in B.export(job, "v6").detail
    assert not (job / "videos").exists()


def test_refuses_missing_publish_and_missing_tts(tmp_path):
    job = make_job(tmp_path)
    (job / "publish_v6.json").unlink()
    assert "publish" in B.export(job, "v6").detail

    job = make_job(tmp_path / "b")
    (job / "tts" / "abc.mp3").unlink()
    assert "TTS" in B.export(job, "v6").detail


def test_discover_skips_previews_and_archives(tmp_path):
    job = tmp_path / "job"
    for name in ["shorts_v12.mp4", "shorts_v3.mp4", "shorts_v3_visual_preview.mp4",
                 "shorts_v8.mp4.prev_1", "shorts_v1_ab.mp4", "shorts_e3.mp4"]:
        _w(job / name, b"x")
    assert B.discover(job) == ["v1_ab", "v3", "v12"]


def test_pipeline_render_provenance_replaces_manual_bundle(tmp_path):
    job = make_job(tmp_path)
    assert B.export(job, "v6").status == "exported"          # 수동 묶음: 렌더 기록 없음

    render = B.render_provenance(design={"logo": tmp_path / "logo.png"}, voice="ko_female", speed="fast")
    assert B.export(job, "v6", render=render).status == "exported"

    v = json.loads((job / "videos" / "v6" / "video.json").read_text())
    assert v["provenance"]["render"]["voice"] == "ko_female"
    assert v["provenance"]["render"]["design"]["logo"] == str(tmp_path / "logo.png")
    assert "git_sha" in v["provenance"]["render"]
    assert B.export(job, "v6", render=render).status == "unchanged"


def _archive_or_skip():
    # 전사 수정 무효화(cli.archive_transcript_dependents)는 별도 작업과 함께 들어온다 — 없으면 이 연결만 건너뛴다.
    import pytest
    from app.tikitaka import cli
    fn = getattr(cli, "archive_transcript_dependents", None)
    if fn is None:
        pytest.skip("archive_transcript_dependents 없음")
    return fn


def test_transcript_change_marks_bundles_stale_and_rerender_replaces(tmp_path):
    archive_transcript_dependents = _archive_or_skip()
    from app.tikitaka.common import Job

    job = make_job(tmp_path)
    assert B.export(job, "v6").status == "exported"
    _w(job / "transcript_dependents_dirty.json", {"changed_line_ids": ["L-142"]})

    archive_transcript_dependents(Job(source=tmp_path / "source.mp4", out_dir=job, title="t"))

    v = json.loads((job / "videos" / "v6" / "video.json").read_text())
    assert v["status"] == "stale" and v["stale"]["changed_line_ids"] == ["L-142"]
    assert (job / "videos" / "v6" / "shorts.mp4").read_bytes() == b"final video"   # 파일은 유지
    assert B.export(job, "v6").status == "refused"                                # review_v6 보관됨

    make_job(tmp_path, fp="fp-2", video=b"re-rendered")                          # 다시 렌더
    assert B.export(job, "v6").status == "exported"
    assert json.loads((job / "videos" / "v6" / "video.json").read_text())["status"] == "ready"


def test_editor_edits_are_recorded_outside_bundle_and_survive(tmp_path):
    archive_transcript_dependents = _archive_or_skip()
    from app.tikitaka.common import Job
    import pytest

    job = make_job(tmp_path)
    assert B.export(job, "v6").status == "exported"
    e = B.record_edit(job, "v6", {"subtitles": [{"idx": 0, "text": "고친 자막"}]},
                      based_on_render_fingerprint="fp-1", created_by="reviewer@example.com", note="오타")
    assert (job / "video_edits" / "v6" / f"{e['edit_id']}.json").exists()
    for bad in [{}, {"unknown": 1}]:
        with pytest.raises(ValueError):
            B.record_edit(job, "v6", bad, based_on_render_fingerprint="fp-1", created_by="r")
    with pytest.raises(ValueError):
        B.record_edit(job, "v6", {"title": {}}, based_on_render_fingerprint="", created_by="r")

    # 새 수정이 생기면 같은 렌더라도 video.json 대기 목록을 갱신한다
    assert B.export(job, "v6").status == "exported"
    v = json.loads((job / "videos" / "v6" / "video.json").read_text())
    assert [x["edit_id"] for x in v["edits"]["pending"]] == [e["edit_id"]] and v["edits"]["applied"] == []

    # 렌더 기록에 적용했다고 남으면 applied 로 옮긴다
    B.export(job, "v6", render={"applied_edits": [e["edit_id"]]})
    v = json.loads((job / "videos" / "v6" / "video.json").read_text())
    assert [x["edit_id"] for x in v["edits"]["applied"]] == [e["edit_id"]] and v["edits"]["pending"] == []

    # 전사 수정 무효화·재렌더 교체에도 수정 기록은 그대로
    _w(job / "transcript_dependents_dirty.json", {"changed_line_ids": ["L1"]})
    archive_transcript_dependents(Job(source=tmp_path / "source.mp4", out_dir=job, title="t"))
    make_job(tmp_path, fp="fp-2", video=b"re-rendered")
    assert B.export(job, "v6").status == "exported"
    assert [x["edit_id"] for x in B.list_edits(job, "v6")] == [e["edit_id"]]


def test_manual_reexport_keeps_pipeline_render_record(tmp_path):
    job = make_job(tmp_path)
    assert B.export(job, "v6", render={"git_sha": "abc", "voice": "v"}).status == "exported"
    B.record_edit(job, "v6", {"title": {"top_title": "새 제목"}}, based_on_render_fingerprint="fp-1", created_by="r")

    assert B.export(job, "v6").status == "exported"          # 수정 기록이 생겨 다시 묶지만

    v = json.loads((job / "videos" / "v6" / "video.json").read_text())
    assert v["provenance"]["render"] == {"git_sha": "abc", "voice": "v"}   # 렌더 기록은 유지
    assert len(v["edits"]["pending"]) == 1

    make_job(tmp_path, fp="fp-2", video=b"re-rendered")        # 다른 렌더를 수동으로 묶으면 기록 없음
    assert B.export(job, "v6").status == "exported"
    assert json.loads((job / "videos" / "v6" / "video.json").read_text())["provenance"]["render"] is None


def test_record_edit_cli_refuses_outdated_render(tmp_path, monkeypatch, capsys):
    import io
    job = make_job(tmp_path)
    B.export(job, "v6")
    args = ["record-edit", str(job), "v6", "--by", "r@example.com"]

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"texts": [{"text": "보조"}]})))
    assert B.main(args + ["--based-on", "old-fp"]) == 3
    assert "다시 렌더" in capsys.readouterr().out
    assert B.list_edits(job, "v6") == []

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"texts": [{"text": "보조"}]})))
    assert B.main(args + ["--based-on", "fp-1", "--note", "메모"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["edit_id"] for e in B.list_edits(job, "v6")] == [out["edit_id"]]
    (job / "video_edits" / "v6" / "drafts").mkdir()
    (job / "video_edits" / "v6" / "drafts" / "current.json").write_text("{}")
    assert len(B.list_edits(job, "v6")) == 1          # 초안은 수정 기록이 아니다


def test_labels_doc_maps_ai_labels_and_skips_stale_label_ass(tmp_path):
    job = make_job(tmp_path)
    work = job / "review_v6"
    _w(work / "edit_plan.json", {"timeline": [
        {"clip_start_sec": 100.0, "clip_end_sec": 102.0},                                     # 편집본 0~2
        {"clip_start_sec": 200.0, "clip_end_sec": 202.2, "playback_speed": 1.1, "hold_sec": 1.0},  # 2~5
        {"clip_start_sec": 300.0, "clip_end_sec": 310.0}]})                                   # 5~15
    _w(work / "checkpoint_style.json", {"fingerprint": "sf", "style": {"v3_style": {
        "labels": [{"text": "(감동)", "start_sec": 3.1, "end_sec": 4.0, "x": 0.25, "y": 0.35, "rotate": -3,
                    "color": "#FFE94A", "fx": "pop", "person": "이지혜", "anchor": "G27", "probe": {"raw": 1}},
                   {"text": "", "start_sec": 1, "end_sec": 2}],
        "emphasis": [{"index": 2, "text": "진작에", "start_sec": 6, "end_sec": 7, "scale": 1.35, "color": "#FF5540"}]}},
        "audit": {"label_probes": [{"text": "(완벽한 서포트)", "reason": "무표정", "result": "불일치 — 드롭"},
                                   {"text": "(감동)", "result": "유지(±0.05s)"}]}})
    _w(work / "v3_labels.ass", b"old labels")                                  # 옛 렌더가 남긴 파일
    _w(work / "v3_tts.ass", b"tts")
    _w(work / "final_1080x1920.filter.txt", b"ass='v3_subtitles.ass',ass='v3_tts.ass'")

    assert B.export(job, "v6").status == "exported"

    d = job / "videos" / "v6"
    doc = json.loads((d / "labels.json").read_text())
    assert doc["schema"] == "tikitaka_labels/v1" and doc["time_base"] == "edited_sec"
    (lb,) = doc["labels"]
    assert (lb["id"], lb["origin"], lb["text"], lb["person"], lb["duration_sec"]) == ("lb0", "ai", "(감동)", "이지혜", 0.9)
    assert lb["source_time_sec"] == 201.21        # 편집본 3.1 = 둘째 클립 1.1s × 1.1배
    assert "probe" not in lb
    assert [x["text"] for x in doc["dropped"]] == ["(완벽한 서포트)"]
    assert doc["emphasis"][0]["scale"] == 1.35
    assert (d / "v3_tts.ass").exists() and not (d / "v3_labels.ass").exists()
    assert json.loads((d / "video.json").read_text())["files"]["labels"]["count"] == 1


def test_edited_to_source_hold_and_bounds():
    tl = [{"clip_start_sec": 10.0, "clip_end_sec": 11.0, "hold_sec": 0.5}, {"clip_start_sec": 50.0, "clip_end_sec": 52.0}]
    assert B.edited_to_source(tl, 0.5) == 10.5
    assert B.edited_to_source(tl, 1.2) == 11.0        # 붙잡은 꼬리
    assert B.edited_to_source(tl, 2.0) == 50.5
    assert B.edited_to_source(tl, 9.0) is None


def test_editor_scan_is_made_once_per_source_and_bundled(tmp_path, monkeypatch):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "fake-ffmpeg":
            open(cmd[-1], "wb").write(b"proxy")
        return type("P", (), {"returncode": 0})()
    monkeypatch.setattr(B.subprocess, "run", fake_run)
    monkeypatch.setenv("FFMPEG_BIN", "fake-ffmpeg")
    monkeypatch.setattr(B, "_git", lambda repo: {"git_sha": "x", "git_dirty": False})
    monkeypatch.setattr(B, "_place", lambda src, dst: dst.write_bytes(src.read_bytes()))
    job = make_job(tmp_path)
    assert B.export(job, "v6").status == "exported"
    assert (job / "videos" / "v6" / "editor_scan.mp4").read_bytes() == b"proxy"
    enc = [c for c in calls if c[0] == "fake-ffmpeg"]
    assert len(enc) == 1 and "scale=-2:360" in enc[0] and "-g" in enc[0]

    for f, data in [("review_v6/review.json", {"items": [], "preview_only": False, "render_fingerprint": "fp-2"}),
                    ("review_v6/render_fingerprint.json", {"fingerprint": "fp-2"}),
                    ("review_v6.json", {"render_fingerprint": "fp-2"})]:
        _w(job / f, data)                                      # 같은 원본을 다시 렌더
    _w(job / "review_v6" / "final_1080x1920.mp4", b"re-rendered"); _w(job / "shorts_v6.mp4", b"re-rendered")
    assert B.export(job, "v6").status == "exported"
    assert len([c for c in calls if c[0] == "fake-ffmpeg"]) == 1   # 사본은 다시 만들지 않는다

    (tmp_path / "source.mp4").write_bytes(b"new source")           # 원본이 바뀌면 다시 만든다
    st = (tmp_path / "source.mp4").stat()
    _w(job / "pipeline_identity.json", {"source": {"path": str(tmp_path / "source.mp4"), "size": str(st.st_size),
                                                   "mtime_ns": str(st.st_mtime_ns)}})
    assert B.editor_scan(job, B._source_identity(job)) == job / B.EDITOR_SCAN
    assert len([c for c in calls if c[0] == "fake-ffmpeg"]) == 2


def test_editor_scan_failure_does_not_block_bundle(tmp_path, monkeypatch):
    def failing(cmd, **kw):
        if cmd[0] == "fake-ffmpeg":
            raise B.subprocess.CalledProcessError(1, cmd)
        return type("P", (), {"returncode": 0})()
    monkeypatch.setattr(B.subprocess, "run", failing)
    monkeypatch.setenv("FFMPEG_BIN", "fake-ffmpeg")
    monkeypatch.setattr(B, "_git", lambda repo: {"git_sha": "x", "git_dirty": False})
    monkeypatch.setattr(B, "_place", lambda src, dst: dst.write_bytes(src.read_bytes()))
    job = make_job(tmp_path)
    assert B.export(job, "v6").status == "exported"
    assert "editor_scan" not in json.loads((job / "videos" / "v6" / "video.json").read_text())["files"]
    assert not list(job.glob(".*tmp.mp4"))
