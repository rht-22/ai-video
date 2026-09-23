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


def test_cover_rejects_bad_ids_but_accepts_lower_scored_opening():
    candidates = {"sp0": {**fact("sp0", importance=3), "t_in": 0., "t_out": 2.}}
    for ids in (["fake"], ["sp0", "sp0"]):
        with pytest.raises(ValueError):
            gt.assemble_cover(ids, candidates, 1)
    assert gt.assemble_cover(["sp0"], candidates, 1, opening=True)


def test_cover_never_holds_and_speeds_long_material_up_to_1_2():
    sp = {**fact("sp0", has_text=True, screen_text="방송 자막"), "t_in": 0., "t_out": 1.}
    with pytest.raises(ValueError, match="부족"):
        gt.assemble_cover(["sp0"], {"sp0": sp}, 2)
    sp["screen_text_kind"] = "기사"
    with pytest.raises(ValueError, match="부족"):
        gt.assemble_cover(["sp0"], {"sp0": sp}, 2)
    long = {**sp, "t_out": 2.}
    result = gt.assemble_cover(["sp0"], {"sp0": long}, 5/3)
    assert "hold_sec" not in result[0]
    assert result[0]["playback_speed"] == pytest.approx(1.2)
    assert result[0]["dur"] == pytest.approx(5/3, abs=1/30)


def test_each_cover_cut_stays_at_or_below_speed_cap_after_frame_quantizing():
    candidates = {
        "a": {**fact("a"), "t_in": 0.0, "t_out": 0.6},
        "b": {**fact("b"), "t_in": 1.0, "t_out": 2.2},
    }
    cuts = gt.assemble_cover(["a", "b"], candidates, 1.5)
    assert sum(c["dur"] for c in cuts) >= 1.5
    assert all(c["playback_speed"] <= 1.2 + 1e-9 for c in cuts)
    assert all(c["dur"] * gt.FPS == pytest.approx(round(c["dur"] * gt.FPS)) for c in cuts)


def test_finish_bundle_preserves_grid_output_frame_after_speed_rounding():
    cut = {"in": 0.0, "out": 2.0, "dur": 5/3, "playback_speed": 1.2,
           "span_ids": ["sp0"], "authority": "grid+tts"}
    table = {"version": {"n": 1, "title": "t"}, "rows": [{
        "mode": "N", "text": "x", "dur": 5/3, "tts": "/tmp/x.mp3",
        "cuts": [cut]}]}
    plan, _, _, _ = finish.bundle(table, {}, title="t")
    assert finish.assemble.clip_len(plan["timeline"][0]) == pytest.approx(5/3)


def test_finish_bundle_offsets_follow_renderer_clock_after_sped_up_cover():
    # 로또 v8 행14 (2026-09-23): a hand edit set in/out/dur=2.5 but kept the old
    # 1.176x speed, so the renderer made a 2.133s clip while bundle advanced by
    # 2.5s and every later subtitle/TTS cue was 0.367s late.
    def row(mode, a, z, **kw):
        cut = {"in": a, "out": z, "dur": z - a, "span_ids": ["sp0"],
               "authority": "grid+tts" if mode == "N" else "stt", **kw}
        return {"i": 0, "mode": mode, "text": "x", "tts": "/tmp/x.mp3", "cuts": [cut], "dur": z - a}
    cover = row("N", 0.0, 2.4, playback_speed=1.2)       # renders 2.0s, stale dur 2.4
    cover["dur"] = 1.9                                    # TTS fits the real clip
    spoken = row("S", 3.0, 5.0)
    spoken["sub_lines"] = [{"start": 3.5, "end": 4.5, "text": "자신 있으신 겁니까?"}]
    tail = row("N", 6.0, 7.0)
    table = {"version": {"n": 1, "title": "t"}, "rows": [cover, spoken, tail]}
    plan, _, segments, resources = finish.bundle(table, {}, title="t")
    offsets = finish.assemble.edited_offsets(plan["timeline"], plan["output_fps"])
    def to_source(t):
        s, e, off = max((o for o in offsets if o[2] <= t + 1e-9), key=lambda o: o[2])
        return s + (t - off)
    assert segments[0]["start_sec"] == pytest.approx(2.5)
    for seg in segments:
        assert to_source(seg["start_sec"]) == pytest.approx(seg["source_time_sec"], abs=1e-3)
    cues = [f["cue"] for f in resources["tts_cue_files"]]
    assert [c["start_sec"] for c in cues] == pytest.approx([0.0, 4.0])
    for cue in cues:
        assert to_source(cue["start_sec"]) == pytest.approx(cue["source_time_sec"], abs=1e-3)
    # The stale dur must not hide narration running past the real cover.
    cover["dur"] = 2.3
    with pytest.raises(ValueError, match="TTS audio exceeds"):
        finish.bundle(table, {}, title="t")


def test_cover_speed_one_is_canonical_after_float_quantizing():
    # Decimal source timestamps can make the ratio a few ulps smaller than
    # one.  That is not an intentional slow-down and must not trip the v3
    # renderer's [1.0, 1.2] validation.
    candidates = {
        "sp0": {**fact("sp0"), "t_in": 0.0, "t_out": 1.099999999999},
    }
    cuts = gt.assemble_cover(["sp0"], candidates, 1.1)
    assert cuts[0]["playback_speed"] == 1.0
    table = {"version": {"n": 1, "title": "t"}, "rows": [{
        "mode": "N", "text": "x", "dur": cuts[0]["dur"], "tts": "/tmp/x.mp3",
        "cuts": cuts,
    }]}
    plan, _, _, _ = finish.bundle(table, {}, title="t")
    assert "playback_speed" not in plan["timeline"][0]


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


@pytest.mark.parametrize("style_preset", ["drama_clip", "recap"])
def test_review_remaps_once_and_reuses_style_render_independently(tmp_path, monkeypatch, style_preset):
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
        assert kwargs["preset"] == finish.stage4.get_style_preset(style_preset)
        assert kwargs["dialogue"][-1]["start_sec"] < 6.1
        return {"design": kwargs["preset"], "v3_style": {}}, {"attempts": []}
    def render(**kwargs):
        calls["render"] += 1
        assert kwargs["style_preset"] == style_preset
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
        result = finish.run(job, table, grid, index, get_gemini=lambda: None, style_preset=style_preset)
        assert result.read_bytes() == b"render"
    assert calls["watch"] == calls["style"] == calls["render"] == 1
    assert job.load("review_v1/stage2.json") == index["v3_stage2"]
    first = job.load("review_v1/checkpoint_resources.json")
    monkeypatch.setattr(finish, "render_dependencies", lambda: "rules2")
    finish.run(job, table, grid, index, get_gemini=lambda: None, style_preset=style_preset)
    assert calls["watch"] == calls["style"] == 1 and calls["render"] == 2
    assert list(job.path("review_v1").glob("final_prev_*.mp4"))
    assert job.load("review_v1/checkpoint_resources.json") == first
    table["version"]["n"] = 2
    finish.run(job, table, grid, index, get_gemini=lambda: None, style_preset=style_preset)
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
    assert selected[0]['in'] == 6.8 and selected[-1]['out'] == 8.
    assert selected[0]['playback_speed'] == pytest.approx(1.2)
    s['mode'] = 'A'
    assert gt.before_dialogue_candidates(n, rows, index, grid, [6.,8.], []) == {}




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


def test_singing_subtitles_preserve_speech_narration_and_cut_clocks():
    from app.tikitaka.subtitles import filter_singing
    table = {'rows': [
        {'mode': 'S', 'cuts': [{'in': 0, 'out': 6, 'span_ids': ['song', 'speech', 'outside']}],
         'sub_lines': [{'start': 0, 'end': 1, 'text': '노래 가사'},
                       {'start': 2, 'end': 3, 'text': '남은 시간입니다'},
                       {'start': 4, 'end': 5, 'text': '창 밖 가사'}]},
        {'mode': 'N', 'cuts': [{'in': 6, 'out': 8}], 'text': '내레이션', 'tts': 'voice.mp3'},
        {'mode': 'A', 'cuts': [{'in': 8, 'out': 9}], 'text': '박수'}]}
    before = copy.deepcopy(table)
    spans = {'song': {'t_in': 0, 't_out': 2, 'scene_script': '노래를 열창한다'},
             'speech': {'t_in': 2, 't_out': 4, 'scene_script': '남은 시간을 알린다',
                        'meaning_content': '노래 무대'},
             'outside': {'t_in': 4, 't_out': 6, 'scene_script': '노래를 부른다'}}
    result, audit = filter_singing(table, spans, [(0, 4)])
    assert table == before
    assert result['rows'][0]['sub_lines'] == before['rows'][0]['sub_lines'][1:]
    assert result['rows'][0]['cuts'] == before['rows'][0]['cuts']
    assert result['rows'][1:] == before['rows'][1:]
    assert [x['kept'] for x in audit] == [False, True]
    assert filter_singing(table, spans, [])[0] == before
    assert filter_singing(table, {}, [(0, 6)])[0] == before


def test_adjacent_action_reclaims_only_optional_tail_without_losing_audio():
    transcript = {'lines': [{'id': 'L-941', 'word_i': [0], 'text': '넣었다가.'}],
                  'words': [{'start': 3246.06, 'end': 3246.58}]}
    rows = [
        {'i': 1, 'mode': 'S', 'src': ['L-941'], 'dur': 1.47,
         'sub_lines': [{'start': 3246.01, 'end': 3246.73, 'text': '넣었다가.'}],
         'cuts': [{'in': 3246.01, 'out': 3247.48, 'dur': 1.47}]},
        {'i': 2, 'mode': 'A', 'dur': 1.32,
         'cuts': [{'in': 3246.58, 'out': 3247.9, 'dur': 1.32}]}]
    before = copy.deepcopy(rows)
    assert len(gt.share_adjacent_action_boundary(rows, transcript)) == 1
    s, a = [r['cuts'][0] for r in rows]
    assert s['out'] == a['in'] == 3246.73
    assert s['in'] == before[0]['cuts'][0]['in']
    assert a['out'] == before[1]['cuts'][0]['out']
    assert rows[0]['sub_lines'] == before[0]['sub_lines']
    assert sum(r['dur'] for r in rows) == pytest.approx(a['out'] - s['in'])
    assert gt.share_adjacent_action_boundary(rows, transcript) == []


@pytest.mark.parametrize('word_end,action_end', [(3247.2, 3247.9), (3246.58, 3247.0)])
def test_action_boundary_joins_overlapping_reaction_after_protected_words(word_end, action_end):
    """2026-09-17 사용자 결정: 앞 대사와 겹친다고 반응 장면이 잘리거나 편이 실패하면 안 된다(종전 판은 이 두 경우를
    '진짜 충돌'로 남겨 조립을 실패시켰다). 대사의 STT 단어 구간(protected_end)은 그대로 두고 그 뒤부터 A 가 이어받는다 —
    꼬리가 짧아도, 반응이 발성 안에 통째로 들어 있어도(발성 끝 뒤 0.3s 이어 줌)."""
    transcript = {'lines': [{'id': 'L', 'word_i': [0], 'text': '대사'}],
                  'words': [{'start': 3246.06, 'end': word_end}]}
    rows = [{'i': 1, 'mode': 'S', 'src': ['L'], 'cuts': [{'in': 3246.01, 'out': 3247.48}]},
            {'i': 2, 'mode': 'A', 'cuts': [{'in': 3246.58, 'out': action_end}]}]
    from app.tikitaka.timing import bind_dialogue
    protected_end = bind_dialogue({'L': transcript['lines'][0]}, transcript['words'], ['L'])['end']
    notes = gt.share_adjacent_action_boundary(rows, transcript)
    assert len(notes) == 1
    s, a = rows[0]['cuts'][0], rows[1]['cuts'][0]
    assert s['out'] == a['in'] >= protected_end                      # 대사 단어는 한 글자도 안 잘린다
    assert a['out'] - a['in'] >= 1 / gt.FPS - 1e-7                   # 반응 행은 사라지지 않는다


def test_failed_narration_fit_reselects_and_preserves_original_row(tmp_path, monkeypatch):
    job, grid, index, n, s, version = leadin_fixture(tmp_path, monkeypatch)
    calls, probes = [], []
    class Model:
        def text_json(self, prompt, *, kind):
            calls.append(kind)
            if kind == 'grid_cover_select':
                if calls.count(kind) == 1:
                    return {'placement': 'before_dialogue', 'span_ids': ['sp0003']}
                assert '내레이션은 유지' in prompt
                return {'placement': 'same_scene', 'span_ids': ['sp0000', 'sp0001']}
            return {'text': '여전히 너무 긴 문장'}
    monkeypatch.setattr(gt, '_tts_cached', lambda *a: (tmp_path/'tts.mp3', 3.))
    def probe(j, g, cut, text, **kw):
        probes.append(text)
        return {'text_matches': True}
    monkeypatch.setattr(gt, 'probe_cover', probe)
    result = gt.build_table(job, Model(), {'versions': [version]}, index, {}, [6.,8.], 12., job.source,
                            version_n=1, title=job.title, grid=grid)
    row = result['rows'][0]
    assert row['text'] == n['text'] and row['dur'] == n['dur'] and row['tts'] == n['tts']
    assert probes == [n['text'], n['text']]
    assert calls.count('grid_cover_select') == 2
    assert calls.count('grid_narration_fit') == 0
    assert all('hold_sec' not in c for c in row['cuts'])


def test_cover_probe_context_is_transmitted_and_invalidates_cache(tmp_path, monkeypatch):
    job, _, _, _ = fixtures(tmp_path)
    monkeypatch.setattr('app.tikitaka.probe.cut_proxy_clip', lambda *a: job.source)
    prompts = []
    class Model:
        def video_json(self, prompt, clip, **kw):
            prompts.append(prompt)
            return {'text_matches': True, 'record_matches': True, 'seen': '규칙 설명', 'reason': '같은 상황'}
    cut = {'in': 0., 'out': 1., 'desc': '진행자가 규칙을 설명한다'}
    ctx = [{'mode': 'S', 'text': '다른 팀 티켓은 두 표예요.'}]
    gt.probe_cover(job, Model(), cut, '점수가 두 배죠.', key='fixed', context=ctx)
    gt.probe_cover(job, Model(), cut, '점수가 두 배죠.', key='fixed', context=ctx)
    assert len(prompts) == 1 and ctx[0]['text'] in prompts[0]
    gt.probe_cover(job, Model(), cut, '점수가 두 배죠.', key='fixed', context=[])
    assert len(prompts) == 2




def test_joint_reselection_rejects_new_fact_before_tts_and_keeps_good_row(tmp_path, monkeypatch):
    job, grid, index, n, s, version = leadin_fixture(tmp_path, monkeypatch)
    selections = []
    class Model:
        def text_json(self, prompt, *, kind):
            if kind == 'grid_cover_select':
                selections.append(prompt)
                return {'text': '범인이 잡혔다' if len(selections)==1 else n['text'],
                        'placement':'same_scene', 'span_ids':['sp0000','sp0001']}
            return {'meaning_preserved':False, 'reason':'원문에 없는 범인 체포 사실'}
    monkeypatch.setattr(gt, '_tts_cached', lambda *a: pytest.fail('rejected text must not be synthesized'))
    monkeypatch.setattr(gt, 'probe_cover', lambda *a,**kw: {'text_matches':True})
    result = gt.build_table(job, Model(), {'versions':[version]}, index, {}, [6.,8.], 12., job.source,
                            version_n=1, title=job.title, grid=grid)
    row = result['rows'][0]
    assert row['text'] == n['text'] and row['tts'] == n['tts']
    assert 'original_text' not in row
    assert len(selections)==2 and '문구를 변경할 수 없음' in selections[1]


def test_adjacent_reaction_keeps_short_usable_tail_after_complete_lyric():
    transcript = {'lines': [{'id': 'L-266', 'word_i': [0], 'text': '뽑아라'}],
                  'words': [{'start': 859.69, 'end': 861.68}]}
    rows = [{'i': 1, 'mode': 'S', 'src': ['L-266'],
             'cuts': [{'in': 857.909, 'out': 862.0}]},
            {'i': 2, 'mode': 'A', 'cuts': [{'in': 861.68, 'out': 862.48}]}]
    assert gt.share_adjacent_action_boundary(rows, transcript)
    assert rows[0]['cuts'][0]['out'] == pytest.approx(861.83)
    assert rows[1]['cuts'][0]['in'] == pytest.approx(861.83)
    assert rows[1]['cuts'][0]['dur'] == pytest.approx(.65)


def test_grid_action_does_not_expand_into_unobserved_chunk():
    from app.tikitaka.table import rows_from_items, cuts_in_excluded
    index = {'moments': [{'id': 'S-236', 'start': 894.14, 'end': 894.66,
                         'desc': '두 사람이 환호한다'}]}
    rows = rows_from_items({'items': [{'type': 'A', 'moment_id': 'S-236'}]},
        index, {'lines': [], 'words': []}, [894.14, 894.66], 3270,
        lambda _: None, strict_action_bounds=True)
    assert rows[0]['cuts'][0]['in'] == 894.14
    assert rows[0]['cuts'][0]['out'] == 894.66
    assert not cuts_in_excluded(rows, [(894.66, 1387.8)])


@pytest.mark.parametrize('placement', ['before_dialogue', 'same_scene'])
def test_short_cover_never_rewrites_or_resynthesizes_narration(tmp_path, monkeypatch, placement):
    job, grid, index, n, s, version = leadin_fixture(tmp_path, monkeypatch)
    original = copy.deepcopy(n)
    class Model:
        def text_json(self, *a, **kw):
            pytest.fail('No narration rewrite is permitted')
    monkeypatch.setattr(gt, '_tts_cached', lambda *a: pytest.fail('No replacement TTS'))
    with pytest.raises(gt.NarrationFitError, match='내레이션은 유지'):
        gt.fit_before_dialogue(job, Model(), n, {}, [], voice='ko_female', speed='normal',
                               key='no-shortening', budget_sec=.5, placement=placement)
    assert n == original


def test_assemble_cover_joins_adjacent_short_pieces_into_one_shot():
    """가왕쇼 10화(2026-09-17): 0.5s 조각도 이웃과 이어지면 한 샷. 홀로 짧은 조각만 버린다."""
    cands = {'sp0': {'t_in': 0.0, 't_out': 0.5, 'scene_script': 'a'},
             'sp1': {'t_in': 0.5, 't_out': 1.0, 'scene_script': 'b'},
             'sp9': {'t_in': 7.0, 't_out': 7.4, 'scene_script': 'z'}}
    cuts = gt.assemble_cover(['sp0', 'sp1'], cands, 0.9)
    assert sum(c['dur'] for c in cuts) >= 0.9 - 1e-6
    assert cuts[0]['span_ids'] == ['sp0', 'sp1'] and cuts[0]['src'] == 'sp0'
    with pytest.raises(gt.CoverDurationError):
        gt.assemble_cover(['sp9'], cands, 0.3)


def test_visual_preview_is_separate_and_strict_blocks_before_final_render(tmp_path,monkeypatch):
    from app.tikitaka import visual_edit as ve
    job,grid,index,table=fixtures(tmp_path)
    job.save('picture_area.json',{'x':0,'y':0,'w':640,'h':360})
    monkeypatch.setattr(finish,'render_draft',lambda video,tl,out,resources,**kw:out.write_bytes(b'draft'))
    monkeypatch.setattr(finish.watch_trim,'run_watch_trim',lambda *a,**kw:([],{}))
    monkeypatch.setattr(finish.stage4,'run_style',lambda *a,**kw:({'design':kw['preset'],'v3_style':{}},{}))
    monkeypatch.setattr(ve,'scan',lambda *a:[.2])
    calls=[]
    def render(**kw):
        calls.append(1);out=kw['output_dir']/'final_1080x1920.mp4';out.write_bytes(b'preview');return out,{}
    monkeypatch.setattr(finish.finalize,'render_final',render)
    monkeypatch.setattr(finish,'validate_media',lambda *a:{'mock':True})
    # Original export remains untouched, and absent crop analysis is also pending.
    job.path('shorts_v1.mp4').write_bytes(b'approved original')
    preview=finish.run(job,table,grid,index,get_gemini=lambda:None,visual_edit='preview')
    assert preview.name=='shorts_v1_visual_preview.mp4' and len(calls)==1
    assert job.path('shorts_v1.mp4').read_bytes()==b'approved original'
    assert job.load('review_v1.json')['preview_only']
    with pytest.raises(ValueError,match='시각 편집 검토 필요'):
        finish.run(job,table,grid,index,get_gemini=lambda:None,visual_edit='strict')
    assert len(calls)==1 and job.has('review_v1/visual_review.md')


def test_strict_blocks_export_on_rendered_face_warning(tmp_path,monkeypatch):
    from app.tikitaka import visual_edit as ve
    job,grid,index,table=fixtures(tmp_path)
    table['rows'][2]['cuts'][0]['reframe']={'mode':'fixed','x':320}
    job.save('picture_area.json',{'x':0,'y':0,'w':640,'h':360})
    monkeypatch.setattr(finish,'render_draft',lambda video,tl,out,resources,**kw:out.write_bytes(b'draft'))
    monkeypatch.setattr(finish.watch_trim,'run_watch_trim',lambda *a,**kw:([],{}))
    monkeypatch.setattr(finish.stage4,'run_style',lambda *a,**kw:({'design':kw['preset'],'v3_style':{}},{}))
    monkeypatch.setattr(ve,'scan',lambda *a:[])
    def render(**kw):
        out=kw['output_dir']/'final_1080x1920.mp4';out.write_bytes(b'inspection')
        return out,{'edge_faces':[{'start':3,'end':3,'side':'left','max_cut':.5,'box':[0,400,200,600]}]}
    monkeypatch.setattr(finish.finalize,'render_final',render)
    monkeypatch.setattr(finish,'validate_media',lambda *a:{'mock':True})
    with pytest.raises(ValueError,match='시각 편집 검토 필요'):
        finish.run(job,table,grid,index,get_gemini=lambda:None,visual_edit='strict')
    assert not job.has('shorts_v1.mp4')
    report=job.load('review_v1/visual_edit.json')
    assert report['review_items'][0]['kind']=='face_safe' and report['blocked']
