"""편집실 수정 적용(app.tikitaka.apply_edit) — 렌더 없이 재료 변환만."""
from pathlib import Path

import pytest

from app.tikitaka import apply_edit as A
from app.tikitaka.common import Job

GRID = {"source": {"duration_sec": 100.0},
        "span_candidates": [{"id": f"sp{i}", "t_in": i * 10.0, "t_out": i * 10.0 + 10} for i in range(10)]}


def plan():
    return {"timeline": [
        {"clip_start_sec": 10.0, "clip_end_sec": 12.0, "span_ids": ["sp1"], "beat": 0, "role": "hook",
         "use_original_audio": False, "cover": True, "narration_framing": {"target": "얼굴"}},
        {"clip_start_sec": 30.0, "clip_end_sec": 33.0, "span_ids": ["sp3"], "beat": 1, "role": "build",
         "use_original_audio": True},
        {"clip_start_sec": 50.0, "clip_end_sec": 52.0, "span_ids": ["sp5"], "beat": 2, "role": "ending",
         "use_original_audio": True},
    ], "layout": {"top_title": "옛 제목", "bottom_label": "작품"}, "output_fps": 30}


def material(tmp_path):
    mp3 = tmp_path / "a.mp3"; mp3.write_bytes(b"x")
    segments = [{"start_sec": 2.5, "end_sec": 3.5, "text": "대사 하나", "source_time_sec": 30.5, "speaker": "갑"},
                {"start_sec": 5.2, "end_sec": 6.0, "text": "대사 둘", "source_time_sec": 50.2}]
    resources = {"tts_cue_files": [{"cue": {"text": "내레이션", "start_sec": 0.0, "end_sec": 1.5, "duration_sec": 1.5,
                                            "source_time_sec": 10.0, "beat": 0, "muted_span_ids": ["sp1"]},
                                    "path": str(mp3)}]}
    story = {"beats": [{"number": n, "span_ids": [], "labels": []} for n in range(3)], "template": "tikitaka"}
    style = {"v3_style": {"labels": [{"text": "(놀람)", "start_sec": 5.5, "end_sec": 6.0, "x": .3, "y": .4, "fx": "pop"}],
                          "emphasis": [{"index": 0, "text": "하나", "start_sec": 3.0, "end_sec": 3.4}],
                          "zooms": [{"clip": 2, "factor": 1.2, "from_sec": 0.0}]}}
    return dict(plan=plan(), segments=segments, resources=resources, story=story, style=style, grid=GRID,
                job=Job(Path("src.mp4"), tmp_path, "작품"))


def test_time_mapping_follows_editor_rules():
    tl = plan()["timeline"]
    assert A.total_sec(tl) == pytest.approx(7.0)
    assert A.src_to_out(tl, 31.0) == pytest.approx(3.0)
    assert A.src_to_out(tl, 40.0) is None
    tl[0]["playback_speed"], tl[0]["hold_sec"] = 1.2, 0.5    # 2/1.2 + .5 = 2.1667 → 30fps 격자 65프레임
    assert A.offsets(tl)[1] == pytest.approx(65 / 30)
    assert A.src_to_out(tl, 11.2) == pytest.approx(1.0)


def test_unsupported_keys_are_refused_not_dropped(tmp_path):
    with pytest.raises(A.ApplyRefused, match="디자인"):
        A.apply_overrides({"design": {"title_color": "#fff"}}, **material(tmp_path))
    with pytest.raises(A.ApplyRefused, match="제목 창"):
        A.apply_overrides({"title": {"top_title": "t", "segments": [{"text": "x"}]}}, **material(tmp_path))


def test_clip_edit_moves_untouched_layers_by_source_time(tmp_path):
    ov = {"clips": [{"start_sec": 10.0, "end_sec": 12.0, "role": "hook"},
                    {"start_sec": 50.0, "end_sec": 52.0, "role": "ending"}]}    # 가운데 구간 삭제
    got = A.apply_overrides(ov, **material(tmp_path))
    tl = got["plan"]["timeline"]
    assert [c["span_ids"] for c in tl] == [["sp1"], ["sp5"]]
    assert tl[0]["cover"] and tl[0]["narration_framing"] == {"target": "얼굴"} and tl[0]["use_original_audio"] is False
    assert [s["text"] for s in got["segments"]] == ["대사 둘"]
    assert got["segments"][0]["start_sec"] == pytest.approx(2.2)
    assert got["style"]["v3_style"]["labels"][0]["start_sec"] == pytest.approx(2.5)
    assert got["style"]["v3_style"]["zooms"] == [{"clip": 1, "factor": 1.2, "from_sec": 0.0}]
    assert got["style"]["v3_style"]["emphasis"] == []          # 강조가 붙은 줄이 빠졌다
    assert any(x.get("text") == "대사 하나" for x in got["log"] if x["kind"] == "dropped")
    assert got["story"]["beats"][2]["span_ids"] == ["sp5"]


def test_tts_resynthesizes_with_render_voice_and_refuses_overlap(tmp_path):
    calls = []
    def synth(job, text, voice, speed):
        calls.append((text, voice, speed)); p = tmp_path / f"{len(calls)}.mp3"; p.write_bytes(b"x"); return p, 1.2
    m = material(tmp_path)
    got = A.apply_overrides({"tts": [{"text": "새 문구", "source_time_sec": 30.5, "duration_sec": 1.0}]}, **m,
                            synth=synth, captions=lambda j, r: [], voice="elevenlabs:abc", speed="fast")
    (f,) = got["resources"]["tts_cue_files"]
    assert calls == [("새 문구", "elevenlabs:abc", "fast")]
    assert (f["cue"]["start_sec"], f["cue"]["end_sec"], f["cue"]["beat"]) == (2.5, 3.7, 1)
    assert got["story"]["narration_cues"] == [f["cue"]]
    two = [{"text": "하나", "source_time_sec": 30.0, "voice": "v"}, {"text": "둘", "source_time_sec": 30.5, "voice": "v"}]
    with pytest.raises(A.ApplyRefused, match="겹쳐요"):
        A.apply_overrides({"tts": two}, **material(tmp_path), synth=synth, captions=lambda j, r: [])


def test_editor_texts_become_stage4_labels(tmp_path):
    ov = {"texts": [{"text": "(당황)", "source_time_sec": 31.0, "duration_sec": 0.8, "x": .2, "y": .3, "rotate": -3,
                     "color": "#FFE94A", "origin": "ai", "label_id": "lb0", "size": 56}], "title": {"top_title": "새 제목"}}
    got = A.apply_overrides(ov, **material(tmp_path))
    (lb,) = got["style"]["v3_style"]["labels"]
    assert lb == {"text": "(당황)", "start_sec": 3.0, "end_sec": 3.8, "x": .2, "y": .3, "rotate": -3,
                  "color": "#FFE94A", "fx": "pop"}
    assert got["plan"]["layout"]["top_title"] == "새 제목"


def test_speed_outside_renderer_range_is_refused(tmp_path):
    with pytest.raises(A.ApplyRefused, match="배속"):
        A.apply_overrides({"clips": [{"start_sec": 10, "end_sec": 12, "playback_speed": 1.5}]}, **material(tmp_path))


def test_emphasis_follows_its_line_when_lines_shift(tmp_path):
    m = material(tmp_path)
    m["style"]["v3_style"]["emphasis"] = [{"line": "L1", "index": 1, "text": "둘", "start_sec": 5.3, "end_sec": 5.8}]
    subs = [{"start_sec": 5.2, "end_sec": 6.0, "text": "대사 둘", "source_time_sec": 50.2}]     # 첫 줄 삭제
    got = A.apply_overrides({"subtitles": subs}, **m)
    assert got["style"]["v3_style"]["emphasis"] == [{"line": "L0", "index": 0, "text": "둘", "start_sec": 5.3, "end_sec": 5.8}]
