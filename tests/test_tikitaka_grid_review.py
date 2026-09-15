"""Hybrid contracts: ID authority, speech preservation, per-version review/cache."""
from __future__ import annotations

import copy
import shutil
import subprocess
import json
from pathlib import Path

import pytest

from app.tikitaka import grid as gg, grid_table as gt, finish
from app.tikitaka.common import Job


def fact(sid, **kw):
    return {"span_id": sid, "scene_script": "표정을 바라본다", "characters": ["갑", "을"],
            "importance": 4, "has_text": False, "screen_text": "", "screen_text_kind": "기타",
            "diegesis": "actual", **kw}


def fixtures(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic media identity")
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"synthetic audio identity")
    grid = {"source": {"duration_sec": 12, "width": 640, "height": 360, "fps": 30},
            "span_candidates": [{"id": f"sp{i:04d}", "t_in": i*2., "t_out": i*2.+2,
                                 "is_audio": i in (0, 4), "text": "대사" if i in (0,4) else "",
                                 "time_authority": "stt" if i in (0,4) else "scene"} for i in range(6)],
            "scene_cuts": [2., 4., 6., 8., 10.], "words": [], "arousal": []}
    facts = {s["id"]: fact(s["id"]) for s in grid["span_candidates"]}
    rows = []
    # Spoken line, removable visual insert, narration, final spoken line.
    for i, (mode, start) in enumerate([("S", 0), ("A", 2), ("N", 6), ("S", 8)], 1):
        cut = {"in": float(start), "out": float(start+2), "dur": 2.,
               "src": f"sp{start//2:04d}", "span_ids": [f"sp{start//2:04d}"],
               "authority": "grid+tts" if mode == "N" else "stt", "desc": "표정"}
        row = {"i": i, "mode": mode, "cuts": [cut], "dur": 2., "dur_video": 2.,
               "text": "지금 이 표정", "speaker": "갑", "t0": 2.*(i-1),
               "sub_lines": [{"start": start+.1, "end": start+1.8, "text": "무슨 일이야?"}] if mode == "S" else []}
        if mode == "N":
            row.update(tts=str(tts), plan_sec=2.)
        rows.append(row)
    table = {"version": {"n": 1, "title": "뜻밖의 표정", "strategy": "대사", "issues": []},
             "rows": rows, "total_sec": 8, "voice": "ko_female", "speed": "normal"}
    return Job(source, tmp_path, "테스트"), grid, {"grid_facts": facts}, table


def test_grid_uses_stt_and_shared_carver_without_changing_words():
    transcript = {"words": [{"start": 1., "end": 1.5, "text": "안녕."}], "backend": "elevenlabs"}
    before = copy.deepcopy(transcript)
    grid = gg.build_grid({"duration_sec": 5}, transcript, [2., 4.])
    voiced = [s for s in grid["span_candidates"] if s["is_audio"]]
    assert voiced[0]["t_in"] == 1 and voiced[0]["t_out"] == 1.5
    assert grid["scene_cuts"] == [2, 4]
    assert transcript == before


def test_grid_pads_zero_length_scribe_words_without_touching_transcript():
    transcript = {"words": [{"start": 1., "end": 1., "text": "네,"},
                            {"start": 1., "end": 1.4, "text": "고맙습니다."},
                            {"start": 1.45, "end": 1.45, "text": "어?"},
                            {"start": 1.5, "end": 2., "text": "저희"},
                            {"start": 4.98, "end": 4.98, "text": "컷!"}], "backend": "elevenlabs"}
    before = copy.deepcopy(transcript)
    grid = gg.build_grid({"duration_sec": 5}, transcript, [])
    by_text = {w["text"]: w for w in grid["words"]}
    assert (by_text["네,"]["t0"], by_text["네,"]["t1"]) == (1., 1.1)
    assert by_text["어?"]["t1"] == 1.5            # clamped at the next later-starting word
    assert by_text["컷!"]["t1"] == 5.             # clamped at the source end
    assert all(w["t0"] < w["t1"] for w in grid["words"])
    assert grid["transcript"]["zero_length_words_padded"] == 3
    assert transcript == before
    with pytest.raises(ValueError):
        gg.build_grid({"duration_sec": 5}, {"words": [{"start": 2., "end": 1.9, "text": "x"}]}, [])


def test_observer_cannot_override_clock_or_invent_id():
    spans = [{"id": "sp0000", "t_in": 10., "t_out": 12.}]
    raw = {"spans": [dict(fact("sp0000"), start=933, end=999), fact("invented")],
           "scenes": [{"span_ids": ["sp0000"], "start": 1000}, {"span_ids": ["absent"]}],
           "speakers": {"L-001": "갑", "L-999": "을"}}
    parsed = gg.parse_observations(raw, spans, {"L-001"})
    assert "start" not in parsed["facts"]["sp0000"]
    assert parsed["scenes"][0]["start"] == 10
    assert len(parsed["issues"]) == 2
    assert parsed["speakers"] == {"L-001": "갑"}


def test_cover_rejects_unknown_duplicate_and_weak_opening():
    candidates = {"sp0": {**fact("sp0", importance=3), "t_in": 0., "t_out": 2.}}
    for ids in (["fake"], ["sp0", "sp0"]):
        with pytest.raises(ValueError):
            gt.assemble_cover(ids, candidates, 1)
    with pytest.raises(ValueError, match="중요도"):
        gt.assemble_cover(["sp0"], candidates, 1, opening=True)


def test_only_information_screen_can_hold_and_tts_is_not_shortened():
    sp = {**fact("sp0", has_text=True, screen_text="방송 자막"), "t_in": 0., "t_out": 1.}
    with pytest.raises(ValueError, match="부족"):
        gt.assemble_cover(["sp0"], {"sp0": sp}, 2)
    sp["screen_text_kind"] = "기사"
    result = gt.assemble_cover(["sp0"], {"sp0": sp}, 2)
    assert result[0]["hold_sec"] == 1
    assert result[0]["out"] == 1
    assert result[0]["dur"] == 2


def test_same_scene_candidates_do_not_leak_to_middle_scene():
    rows = [{"mode": "S", "cuts": [{"in": 0., "out": 1.}]},
            {"mode": "N", "cuts": []}, {"mode": "S", "cuts": [{"in": 9., "out": 10.}]}]
    spans = [{"id": "sp0", "t_in": 3., "t_out": 4.}, {"id": "sp1", "t_in": 7., "t_out": 8.}]
    index = {"scenes": [{"start": 0., "end": 2.}, {"start": 2., "end": 6.}, {"start": 6., "end": 10.}],
             "grid_facts": {s["id"]: fact(s["id"]) for s in spans}}
    candidates = gt.candidates_for(rows[1], rows, index, {"span_candidates": spans}, [], [])
    assert set(candidates) == {"sp1"}
    assert not gt.candidates_for(rows[1], rows, index, {"span_candidates": spans}, [(7,8)], [])


def test_adapter_keeps_spoken_words_tts_and_source_identity(tmp_path):
    job, grid, index, table = fixtures(tmp_path)
    before = copy.deepcopy(table)
    plan, story, segments, resources = finish.bundle(table, grid, title=job.title)
    assert [s["start_sec"] for s in segments] == pytest.approx([.1, 6.1])
    assert resources["tts_cue_files"][0]["cue"]["start_sec"] == 4
    assert not plan["timeline"][2]["use_original_audio"]
    assert table == before
    assert finish.validate_bundle(plan, grid, segments, resources)["duration_sec"] == 8
    with pytest.raises(ValueError, match="excluded"):
        finish.validate_bundle(plan, grid, segments, resources, exclude=[(6.5, 6.6)])


def test_review_remaps_once_and_reuses_style_render_independently(tmp_path, monkeypatch):
    job, grid, index, table = fixtures(tmp_path)
    index["v3_stage2"] = {"sequences": [], "validation": {"original": True}}
    job.save("picture_area.json", {"x": 0, "y": 0, "w": 640, "h": 360})
    calls = {"watch": 0, "style": 0, "render": 0, "draft": 0}
    def draft(video, tl, out, resources, **kwargs):
        calls["draft"] += 1
        out.write_bytes(b"draft")
    def watch(*args, **kwargs):
        calls["watch"] += 1
        return [{"start": 2.5, "end": 3.5, "reason": "늘어짐"}], {}
    def style(*args, **kwargs):
        calls["style"] += 1
        assert kwargs["dialogue"][-1]["start_sec"] < 6.1
        return {"design": kwargs["preset"], "v3_style": {}}, {"attempts": []}
    def render(**kwargs):
        calls["render"] += 1
        assert kwargs["resources"]["tts_cue_files"][0]["cue"]["start_sec"] < 4
        out = kwargs["output_dir"] / "final_1080x1920.mp4"
        out.write_bytes(b"render")
        return out, {}
    monkeypatch.setattr(finish, "render_draft", draft)
    monkeypatch.setattr(finish.watch_trim, "run_watch_trim", watch)
    monkeypatch.setattr(finish.stage4, "run_style", style)
    monkeypatch.setattr(finish.finalize, "render_final", render)
    monkeypatch.setattr(finish, "render_dependencies", lambda: "rules1")
    monkeypatch.setattr(finish, "validate_media", lambda *a: {"mock": True})
    for _ in range(2):
        result = finish.run(job, table, grid, index, get_gemini=lambda: None)
        assert result.read_bytes() == b"render"
    assert calls["watch"] == calls["style"] == calls["render"] == 1
    assert job.load("review_v1/stage2.json") == index["v3_stage2"]
    first = job.load("review_v1/checkpoint_resources.json")
    monkeypatch.setattr(finish, "render_dependencies", lambda: "rules2")
    finish.run(job, table, grid, index, get_gemini=lambda: None)
    assert calls["watch"] == calls["style"] == 1 and calls["render"] == 2
    assert list(job.path("review_v1").glob("final_prev_*.mp4"))
    assert job.load("review_v1/checkpoint_resources.json") == first
    table["version"]["n"] = 2
    finish.run(job, table, grid, index, get_gemini=lambda: None)
    assert calls["watch"] == 2 and job.has("review_v2/checkpoint_review.json")


def test_reviewer_cannot_remove_a_spoken_line(tmp_path, monkeypatch):
    job, grid, index, table = fixtures(tmp_path)
    job.save("picture_area.json", {"x": 0, "y": 0, "w": 640, "h": 360})
    monkeypatch.setattr(finish, "render_draft", lambda v, tl, out, res, **kw: out.write_bytes(b"draft"))
    monkeypatch.setattr(finish.watch_trim, "run_watch_trim", lambda *a, **kw: ([{"start": .5, "end": 1.5}], {}))
    with pytest.raises(ValueError, match="protected"):
        finish.run(job, table, grid, index, get_gemini=lambda: None)


def test_explicit_logo_adapter_uses_shared_design():
    design = finish.finalize.design_from_style({"work_type": "image", "work_value": "/test/logo.png",
                                              "work_image_width": 600, "work_image_height": 240})
    assert design.work_type == "image" and design.work_value == "/test/logo.png"


def test_scribe_diarization_is_opt_in():
    from app.modules.stt_elevenlabs import build_form_fields
    args = dict(language="kor", keyterms=[], is_raw=False)
    assert dict(build_form_fields(**args))["diarize"] == "false"
    assert dict(build_form_fields(**args, diarize=True))["diarize"] == "true"


def test_story_cache_invalidation_is_tag_scoped_and_preserves_edits(tmp_path):
    job = Job(tmp_path / "source.mp4", tmp_path, "작품")
    job.save("rebuild_a.json", {"human_edit": True})
    job.save("verified_v1_a.json", {"human_edit": True})
    job.save("verified_v1_b.json", {"other": True})
    gg.ensure_story_inputs(job, {"grid": "new"}, tag="a")
    assert not job.has("rebuild_a.json") and not job.has("verified_v1_a.json")
    assert job.has("verified_v1_b.json")
    assert list(tmp_path.glob("rebuild_a.json.prev_*"))
    job.save("rebuild_a.json", {"new": True})
    gg.ensure_story_inputs(job, {"grid": "new"}, tag="a")
    assert job.has("rebuild_a.json")


def test_cli_default_runs_tikitaka_selection_then_reviews_each_version(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.tikitaka import cli, probe, transcribe, llm, rebuild, report
    from app.tikitaka import research
    from app.tikitaka import v3_analysis
    stages = []
    def research_run(*a, **kw):
        stages.append("research")
        return {"work_context": "박경희는 인플루언서", "cast_images": [
            {"character_name": "박경희", "actor_name": "김혜수"}]}
    monkeypatch.setattr(research, "run", research_run)
    monkeypatch.setattr(cli, "load_dotenv_if_any", lambda: None)
    job, grid, index, table = fixtures(tmp_path)
    index.update(grid_fingerprint="test", scenes=[], cast=[], speakers={}, moments=[])
    transcript = {"lines": [], "words": []}
    versions = [dict(table["version"], n=i, items=[], structure="", analysis={}, plan_sec=8) for i in (1, 2)]
    def indexer(j, *a, **kw):
        assert stages == ["research", "transcribe"]
        assert "박경희는 인플루언서" in kw["research_context"]
        assert "김혜수" in kw["cast"]
        j.save("transcript.json", transcript)
        return index, grid
    def tabler(j, *a, version_n, **kw):
        return {**copy.deepcopy(table), "version": versions[version_n-1]}
    rendered = []
    def render(j, t, *a, **kw):
        rendered.append((t["version"]["n"], kw))
    monkeypatch.setattr(probe, "probe", lambda j: grid["source"])
    monkeypatch.setattr(probe, "build_audio", lambda j: job.source)
    monkeypatch.setattr(probe, "build_scan_proxy", lambda j: job.source)
    monkeypatch.setattr(probe, "build_cut_proxy", lambda j: job.source)
    def transcriber(*a, **kw):
        assert stages == ["research"]
        assert "박경희" in kw["cast"]
        stages.append("transcribe")
        return transcript
    monkeypatch.setattr(transcribe, "transcribe", transcriber)
    from app.tikitaka import scenecut
    monkeypatch.setattr(scenecut, "detect_scene_cuts", lambda *a: [])
    monkeypatch.setattr(scenecut, "detect_black_spans", lambda *a: [])
    monkeypatch.setattr(llm, "Gemini", lambda **kw: SimpleNamespace(usage=SimpleNamespace(calls=[])))
    monkeypatch.setattr(v3_analysis, "build_index", indexer)
    monkeypatch.setattr(rebuild, "rebuild", lambda *a, **kw: {"versions": versions, "recommended": 1,
                        "ranking": [1,2], "rerank": True, "reason": ""})
    monkeypatch.setattr(rebuild, "apply_scene_order_version", lambda *a, **kw: 0)
    monkeypatch.setattr(rebuild, "polish_character_names", lambda *a, **kw: 0)
    monkeypatch.setattr(rebuild, "polish_guide", lambda *a, **kw: 0)
    monkeypatch.setattr(report, "versions_md", lambda *a, **kw: "versions")
    monkeypatch.setattr(gt, "build_table", tabler)
    monkeypatch.setattr(finish, "run", render)
    out = tmp_path / "output"
    assert cli.main(["--source", str(job.source), "--title", "테스트", "--out", str(out), "--count", "2",
                     "--no-verify", "--no-voice-check", "--no-transcript-polish", "--no-digest"]) == 0
    assert [n for n, _ in rendered] == [1,2]
    assert all(not kw["redo"] for _, kw in rendered)
    assert (out / "publish_v2.json").exists()


def test_explicit_render_redo_does_not_reask_watch_or_style(tmp_path, monkeypatch):
    job, grid, index, table = fixtures(tmp_path)
    job.save("picture_area.json", {"x": 0, "y": 0, "w": 640, "h": 360})
    counters = {"watch": 0, "style": 0, "render": 0}
    def watch(*a, **kw):
        counters["watch"] += 1
        return [], {}
    def style(*a, **kw):
        counters["style"] += 1
        return {"design": kw["preset"], "v3_style": {}}, {}
    def render(**kw):
        counters["render"] += 1
        out = kw["output_dir"] / "final_1080x1920.mp4"
        out.write_bytes(b"output")
        return out, {}
    monkeypatch.setattr(finish, "render_draft", lambda v,t,o,r,**kw: o.write_bytes(b"draft"))
    monkeypatch.setattr(finish.watch_trim, "run_watch_trim", watch)
    monkeypatch.setattr(finish.stage4, "run_style", style)
    monkeypatch.setattr(finish.finalize, "render_final", render)
    monkeypatch.setattr(finish, "validate_media", lambda *a: {})
    monkeypatch.setattr(finish, "render_dependencies", lambda: "fixed")
    finish.run(job, table, grid, index, get_gemini=lambda: None)
    finish.run(job, table, grid, index, get_gemini=lambda: None, force_render=True)
    assert counters == {"watch": 1, "style": 1, "render": 2}
    table["version"]["title"] = "바꾼 제목"
    table["rows"][0]["cuts"][0]["reframe"] = {"mode": "fixed", "x": 300}
    finish.run(job, table, grid, index, get_gemini=lambda: None)
    assert counters["watch"] == 1
    saved = job.load("review_v1/edit_plan.json")
    assert saved["layout"]["top_title"] == "바꾼 제목"
    assert saved["timeline"][0]["reframe"] == {"mode": "fixed", "x": 300}


def test_grid_table_reselects_contradictory_cover_then_caches(tmp_path, monkeypatch):
    job, grid, index, table = fixtures(tmp_path)
    index["scenes"] = [{"start": 0., "end": 12.}]
    index["moments"] = []
    transcript = {"words": [{"i": 0, "start": 8.1, "end": 9., "text": "안녕"}],
                  "lines": [{"id": "L-001", "word_i": [0], "text": "안녕", "speaker": "갑"}]}
    version = {"n": 1, "title": "첫 장면", "strategy": "대사", "items": [
        {"type": "N", "text": "표정이 달라지는데"}, {"type": "S", "line_ids": ["L-001"]}]}
    calls = []
    class Model:
        def text_json(self, prompt, **kw):
            calls.append(prompt)
            return {"span_ids": ["sp0000" if len(calls) == 1 else "sp0002"]}
    monkeypatch.setattr(gt, "_tts_cached", lambda *a: (tmp_path/"tts.mp3", 2.))
    monkeypatch.setattr(gt, "probe_cover", lambda j,g,c,t,**kw: {
        "text_matches": c["src"] != "sp0000", "seen": "다른 사람", "reason": "불일치"})
    args = (job, Model(), {"versions": [version]}, index, transcript, [], 12., job.source)
    result = gt.build_table(*args, version_n=1, title=job.title, grid=grid)
    assert len(calls) == 2 and "불일치" in calls[-1]
    assert result["rows"][0]["cuts"][0]["src"] == "sp0002"
    assert result["cover_review"][0]["attempts"] == 2
    gt.build_table(*args, version_n=1, title=job.title, grid=grid)
    assert len(calls) == 2
    gt.build_table(*args, version_n=1, title=job.title, grid=grid, force=True)
    assert len(calls) == 3


def test_ambient_cut_cannot_repeat_later_dialogue(tmp_path, monkeypatch):
    job, grid, index, table = fixtures(tmp_path)
    ambient, spoken = copy.deepcopy(table["rows"][1]), copy.deepcopy(table["rows"][3])
    ambient["cuts"][0].update({"in": 8., "out": 10., "dur": 2.})
    monkeypatch.setattr(gt, "rows_from_items", lambda *a, **kw: [ambient, spoken])
    with pytest.raises(ValueError, match="중복"):
        gt.build_table(job, None, {"versions": [{"n": 1}]}, index, {}, [], 12., job.source,
                       version_n=1, title=job.title, grid=grid)


def test_grid_observation_pipeline_cache_and_binding_review(tmp_path, monkeypatch):
    from app.v3 import audio, arousal, chunk_analyze
    from app.tikitaka import index as legacy_index
    job, _, _, _ = fixtures(tmp_path)
    info = {"duration_sec": 12, "width": 640, "height": 360}
    transcript = {"words": [{"start": 8., "end": 9., "text": "안녕."}],
                  "lines": [{"id": "L-001", "start": 8., "end": 9., "text": "안녕."}]}
    monkeypatch.setattr(audio, "detect_silence_intervals", lambda *a: [(0., 8.), (9., 12.)])
    monkeypatch.setattr(audio, "load_pcm", lambda *a: [])
    monkeypatch.setattr(arousal, "compute_arousal", lambda *a: [])
    monkeypatch.setattr(legacy_index, "_cut_window_clip", lambda *a: job.source)
    checks = []
    def check(*a, **kw):
        checks.append(1)
        return {"status": "ok", "checked": 8, "mismatch": 0}
    monkeypatch.setattr(chunk_analyze, "verify_scene_binding", check)
    spans = gg.build_grid(info, transcript, [4.], silence=[(0.,8.),(9.,12.)])["span_candidates"]
    class Model:
        video_model = "gemini-3.7-flash"
        def video_json(self, *a, **kw):
            assert "인물" in a[0]
            return {"spans": [fact(s["id"]) for s in spans], "speakers": {"L-001": "갑"},
                    "scenes": [{"span_ids": [s["id"] for s in spans], "summary": "대화", "chars": ["갑"]}]}
    index, grid = gg.build_index(job, Model(), transcript, job.source, info, [4.],
                                 title=job.title, cast=["갑"], get_v3=lambda: None)
    assert grid["silence"] == [[0.,8.],[9.,12.]]
    assert index["moments"] and transcript["lines"][0]["speaker"] == "갑"
    job.path("stage2.json").unlink()
    gg.build_index(job, Model(), transcript, job.source, info, [4.], title=job.title, cast=["갑"], get_v3=lambda: None)
    assert len(checks) == 1 and job.has("stage2.json")
    job.save("voice_check.json", {"old": True})
    gg.build_index(job, Model(), transcript, job.source, info, [4.], title=job.title,
                   cast=["박경희"], get_v3=lambda: None, research_context="인물: 박경희=김혜수")
    assert len(checks) == 2
    assert not job.has("voice_check.json")
    assert list(job.out_dir.glob("voice_check.json.prev_*"))


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
@pytest.mark.parametrize("source_rate", ["30", "24000/1001"])
def test_real_render_applies_effects_and_heard_narration_without_api(tmp_path, monkeypatch, source_rate):
    """Real media, model verdicts injected: exercises the actual shared renderer."""
    job, grid, index, table = fixtures(tmp_path)
    # A two-frame grid fragment must survive renderer sanitation, preserving
    # every later subtitle and narration offset.
    head = copy.deepcopy(table["rows"][-1]["cuts"][0])
    tail = copy.deepcopy(head)
    head["out"] = head["in"] + 2/30
    head["dur"] = 2/30
    tail["in"] = head["out"]
    tail["dur"] = tail["out"] - tail["in"]
    table["rows"][-1]["cuts"] = [head, tail]
    ffmpeg = shutil.which("ffmpeg")
    monkeypatch.setenv("FFMPEG_BIN", ffmpeg)
    monkeypatch.setenv("FFPROBE_BIN", shutil.which("ffprobe"))
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={source_rate}",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000", "-t", "12", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(job.source)], check=True, capture_output=True)
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000",
                    "-t", "2", str(tmp_path/"tts.mp3")], check=True, capture_output=True)
    index["grid_facts"]["sp0001"].update(has_text=True, screen_text="화면의 기사", screen_text_kind="기사")
    raw = {"design": {}, "beats": [], "labels": [{"text": "(동공지진)", "anchor": "L0", "person": "을",
            "person_visible": True, "duration_sec": .8, "offset_sec": .2, "color": "white"}],
           "emphasis": [{"line": "L0", "color": "red", "scale": 1.23}], "zooms": [], "fits": [{"clip": "C1"}]}
    monkeypatch.setattr(finish.watch_trim, "run_watch_trim", lambda *a, **kw: ([], {}))
    monkeypatch.setattr(finish.stage4, "_call_style_model", lambda *a, **kw: raw)
    monkeypatch.setattr(finish.stage4, "_default_label_probe", lambda *a, **kw: (
        lambda *a: {"fit": True, "start_sec": .3, "reason": "injected verdict"}))
    monkeypatch.setattr("app.tikitaka.subtitles.narration_captions", lambda *a: [
        {"start_sec": 4., "end_sec": 5., "text": "첫 구절"},
        {"start_sec": 5., "end_sec": 6., "text": "둘째 구절"}])
    output = finish.run(job, table, grid, index, get_gemini=lambda: None, split_narration=True,
                        design={"aspect_ratio": "1:1", "speaker_tracking": "on"})
    caption_ass = job.path("review_v1/v3_tts.ass").read_text()
    assert "첫 구절" in caption_ass and "둘째 구절" in caption_ass
    media = finish.validate_media(output, 8)
    assert (media["width"], media["height"]) == (1080,1920)
    style = job.load("review_v1/checkpoint_style.json")["style"]["v3_style"]
    assert len(style["labels"]) == 1 and len(style["emphasis"]) == 1
    assert style["zooms"] and style["fits"] == [1]
    assert list(job.path("review_v1/style_assets").glob("*.wav"))
    # Draft has the narration at the same location (660Hz), not the muted
    # source's 220Hz. A silent draft would make review of narration misleading.
    import numpy as np
    pcm = subprocess.check_output([ffmpeg, "-v", "error", "-ss", "4.4", "-t", "0.4", "-i",
        str(job.path("review_v1/draft_480.mp4")), "-f", "f32le", "-ac", "1", "-ar", "8000", "pipe:1"])
    samples = np.frombuffer(pcm, dtype="float32")
    spectrum = np.abs(np.fft.rfft(samples))
    peak = np.fft.rfftfreq(len(samples), 1/8000)[spectrum.argmax()]
    assert abs(peak-660) < 10


def leadin_fixture(tmp_path, monkeypatch, original_sec=3.5):
    job, grid, index, table = fixtures(tmp_path)
    index['scenes'] = [{'start': 0., 'end': 12.}]
    n, s = copy.deepcopy(table['rows'][2:])
    n.update(i=1, dur=original_sec, text='휴대폰 속 메시지를 확인하자', cuts=[])
    s.update(i=2)
    version = {'n': 1, 'title': '제목', 'items': [
        {'type': 'N', 'text': n['text']}, {'type': 'S', 'text': s['text']}]}
    monkeypatch.setattr(gt, 'rows_from_items', lambda *a, **kw: copy.deepcopy([n, s]))
    return job, grid, index, n, s, version


def test_leadin_rewrites_resynthesizes_then_probes_and_updates_all_outputs(tmp_path, monkeypatch):
    job, grid, index, n, s, version = leadin_fixture(tmp_path, monkeypatch)
    calls, synth, probes = [], [], []
    class Model:
        def text_json(self, prompt, *, kind):
            calls.append(kind)
            if kind == 'grid_cover_select':
                return {'placement': 'before_dialogue', 'span_ids': ['sp0003']}
            if kind == 'grid_narration_fit':
                return {'text': '메시지를 확인하자' if calls.count(kind) == 1 else '메시지를 보자'}
            assert kind == 'grid_narration_fit_check'
            return {'meaning_preserved': True, 'reason': '맥락 유지'}
    def tts(j, text, voice, speed):
        synth.append((text, voice, speed))
        return tmp_path/'tts.mp3', 2.4 if len(synth) == 1 else 1.6
    monkeypatch.setattr(gt, '_tts_cached', tts)
    def probe(j, g, cut, text, **kw):
        probes.append((copy.deepcopy(cut), text))
        return {'text_matches': True}
    monkeypatch.setattr(gt, 'probe_cover', probe)
    args = (job, Model(), {'versions': [version]}, index, {}, [6., 8.], 12., job.source)
    result = gt.build_table(*args, version_n=1, title=job.title, grid=grid, voice='ko_male', speed='slow')
    row = result['rows'][0]
    assert row['text'] == result['version']['items'][0]['text'] == '메시지를 보자'
    assert version['items'][0]['text'] == n['text']  # preserve upstream candidate
    assert row['original_text'] == n['text'] and row['dur'] == 1.6
    assert row['cuts'][0]['in'] == pytest.approx(6.4)
    assert row['cuts'][-1]['out'] == 8.  # joins following S exactly
    assert all('hold_sec' not in c for c in row['cuts'])
    assert probes[0][1] == row['text']
    assert synth == [('메시지를 확인하자', 'ko_male', 'slow'), ('메시지를 보자', 'ko_male', 'slow')]
    audit = result['cover_review'][0]['narration_fit']
    assert audit['budget_sec'] == 2 and len(audit['attempts']) == 2
    plan, story, segments, resources = finish.bundle(result, grid, title=job.title)
    assert resources['tts_cue_files'][0]['cue']['text'] == row['text']
    assert resources['tts_cue_files'][0]['cue']['duration_sec'] == 1.6
    assert segments[0]['start_sec'] == pytest.approx(1.7)
    finish.validate_bundle(plan, grid, segments, resources)
    before = len(calls), len(synth), len(probes)
    gt.build_table(*args, version_n=1, title=job.title, grid=grid, voice='ko_male', speed='slow')
    assert before == (len(calls), len(synth), len(probes))  # no repeat charges


@pytest.mark.parametrize('failure', ['long', 'meaning'])
def test_leadin_rewrite_exhaustion_never_holds_or_falls_back(tmp_path, monkeypatch, failure):
    job, grid, index, n, s, version = leadin_fixture(tmp_path, monkeypatch)
    calls = []
    class Model:
        def text_json(self, prompt, *, kind):
            calls.append(kind)
            if kind == 'grid_cover_select':
                return {'span_ids': ['sp0003']}  # old schema must also honor fit
            if kind == 'grid_narration_fit':
                return {'text': '그가 범인이었다'}
            return {'meaning_preserved': False, 'reason': '새 사실'}
    monkeypatch.setattr(gt, '_tts_cached', lambda *a: (tmp_path/'tts.mp3', 2.4 if failure == 'long' else 1.))
    # Info screens must also shorten, never hold, in before-dialogue placement.
    index['grid_facts']['sp0003'].update(has_text=True, screen_text_kind='기사', screen_text='기사 원문')
    with pytest.raises(gt.NarrationFitError, match='재작성 3회 소진'):
        gt.build_table(job, Model(), {'versions': [version]}, index, {}, [6.,8.], 12., job.source,
                       version_n=1, title=job.title, grid=grid)
    assert calls.count('grid_narration_fit') == 3
    assert calls.count('grid_cover_select') == 1
    assert job.load('review_v1.json')['blocked']
    assert not job.has('grid_table_v1.json')


def test_leadin_short_narration_reuses_audio_and_excludes_used_or_banned(tmp_path, monkeypatch):
    job, grid, index, n, s, _ = leadin_fixture(tmp_path, monkeypatch, original_sec=1.)
    rows = [n,s]
    before = gt.before_dialogue_candidates(n, rows, index, grid, [6.,8.], [(7.5,8.)])
    assert before == {}  # cannot bridge blocked tail to dialogue
    before = gt.before_dialogue_candidates(n, rows, index, grid, [6.,8.], [(6.,6.7)])
    assert before['sp0003']['t_in'] == pytest.approx(6.7)
    monkeypatch.setattr(gt, '_tts_cached', lambda *a: pytest.fail('should reuse TTS'))
    audit = gt.fit_before_dialogue(job, None, n, before, rows, voice='ko_female', speed='normal', key='short')
    assert audit['attempts'] == [] and n['text'] == '휴대폰 속 메시지를 확인하자'
    selected = gt.assemble_before_dialogue(before, n['dur'], opening=True)
    assert selected[0]['in'] == 7. and selected[-1]['out'] == 8.
    s['mode'] = 'A'
    assert gt.before_dialogue_candidates(n, rows, index, grid, [6.,8.], []) == {}


def test_same_scene_short_cover_fits_measured_audio(tmp_path, monkeypatch):
    job, grid, index, n, s, version = leadin_fixture(tmp_path, monkeypatch, original_sec=2.93)
    calls, probes = [], []
    class Model:
        def text_json(self, prompt, *, kind):
            calls.append(kind)
            if kind == 'grid_cover_select':
                assert 'usable_sec' in prompt
                return {'placement': 'same_scene', 'span_ids': ['sp0000']}
            if kind == 'grid_narration_fit':
                return {'text': '메시지를 보자'}
            return {'meaning_preserved': True, 'reason': '의미 유지'}
    monkeypatch.setattr(gt, '_tts_cached', lambda *a: (tmp_path/'tts.mp3', 1.8))
    def probe(j, g, cut, text, **kw):
        probes.append(text)
        return {'text_matches': True}
    monkeypatch.setattr(gt, 'probe_cover', probe)
    result = gt.build_table(job, Model(), {'versions': [version]}, index, {}, [6.,8.], 12., job.source,
                            version_n=1, title=job.title, grid=grid)
    row = result['rows'][0]
    assert row['text'] == '메시지를 보자' and row['dur'] == 1.8
    assert probes == [row['text']]
    assert result['version']['items'][0]['text'] == row['text']
    assert result['cover_review'][0]['narration_fit']['budget_sec'] == 2.
    assert result['cover_review'][0]['narration_fit']['kind'] == 'same_scene'
    assert calls.count('grid_cover_select') == 1
    assert all('hold_sec' not in c for c in row['cuts'])


def test_rejected_identical_selection_is_not_probed_again(tmp_path, monkeypatch):
    job, grid, index, n, s, version = leadin_fixture(tmp_path, monkeypatch, original_sec=1.)
    prompts, probes = [], []
    class Model:
        def text_json(self, prompt, *, kind):
            prompts.append(prompt)
            return {'placement': 'same_scene', 'span_ids': ['sp0000']}
    def probe(j, g, cut, text, **kw):
        probes.append(cut)
        return {'text_matches': False, 'seen': '다른 사람', 'reason': '인물 모순'}
    monkeypatch.setattr(gt, 'probe_cover', probe)
    with pytest.raises(ValueError, match='동일 화면 조합'):
        gt.build_table(job, Model(), {'versions': [version]}, index, {}, [6.,8.], 12., job.source,
                       version_n=1, title=job.title, grid=grid)
    assert len(probes) == 1 and len(prompts) == 3
    assert "'span_ids': ['sp0000']" in prompts[1]
    assert job.load('review_v1.json')['blocked']


def test_leadin_fit_budget_obeys_split_cut_count(tmp_path, monkeypatch):
    job, grid, index, table = fixtures(tmp_path)
    fact = copy.deepcopy(index['grid_facts']['sp0000'])
    candidates = {f'sp{i}': dict(fact, t_in=a, t_out=z) for i,(a,z) in enumerate(
        [(0., 2.), (2., 4.), (4., 6.), (6., 7.)])}
    # First three backwards windows are 1+2+2; the fourth adds 2.
    assert sum(z-a for _,_,a,z in gt.before_dialogue_windows(candidates)) == 7.
    candidates['sp3']['t_out'] = 9.  # splitting the last span uses two of four cuts
    row = {'text': '원문', 'dur': 8.}
    class Model:
        def text_json(self, prompt, *, kind):
            return {'text': '축약'} if kind == 'grid_narration_fit' else {'meaning_preserved': True}
    monkeypatch.setattr(gt, '_tts_cached', lambda *a: (tmp_path/'tts.mp3', 6.8))
    audit = gt.fit_before_dialogue(job, Model(), row, candidates, [], voice='ko_female', speed='normal', key='bounded')
    assert audit['budget_sec'] == 7. and row['dur'] == 6.8
    selected = gt.assemble_before_dialogue(candidates, row['dur'])
    assert len(selected) == 4 and selected[-1]['out'] == 9.
    assert sum(c['dur'] for c in selected) == pytest.approx(6.8)


def test_title_uses_v3_lines_without_rewriting_existing_words(tmp_path):
    title = '불륜 잡으러 갔다가 시체 마주친 김혜수'
    assert finish.v3_title(title) == {'line1': '불륜 잡으러 갔다가', 'line2': '시체 마주친 김혜수'}
    assert finish.v3_title('원하는 윗줄\n원하는 아랫줄') == {
        'line1': '원하는 윗줄', 'line2': '원하는 아랫줄'}
    job, grid, index, table = fixtures(tmp_path)
    table['version']['title'] = title
    plan, story, _, _ = finish.bundle(table, grid, title=job.title)
    assert plan['layout']['top_title'] == '불륜 잡으러 갔다가\n시체 마주친 김혜수'
    assert story['title'] == finish.v3_title(title)


def test_media_tolerance_includes_exact_audio_boundary(monkeypatch):
    import json
    streams = {'streams': [
        {'codec_type': 'video', 'width': 1080, 'height': 1920, 'duration': '39.466668'},
        {'codec_type': 'audio', 'duration': '39.6'}]}
    monkeypatch.setattr(finish.subprocess, 'check_output', lambda *a, **kw: json.dumps(streams))
    assert finish.validate_media(Path('x.mp4'), 39.5)['audio_sec'] == 39.6
    streams['streams'][1]['duration'] = '39.601'
    with pytest.raises(ValueError, match='mismatch'):
        finish.validate_media(Path('x.mp4'), 39.5)
