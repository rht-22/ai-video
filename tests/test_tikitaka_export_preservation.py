def _versioned_job(tmp_path):
    for n in (1, 6, 8, 12):
        (tmp_path / f"shorts_v{n}.mp4").write_bytes(f"final v{n}".encode())
        for kind in ("script", "verified", "verify_raw", "table", "table_agentic_raw", "framing", "effects"):
            (tmp_path / f"{kind}_v{n}.json").write_text("{}")
    (tmp_path / "transcript.json").write_text("{}")
    (tmp_path / "index.json").write_text("{}")


def test_redo_render_for_one_version_keeps_other_finals(tmp_path):
    from app.tikitaka.cli import cascade_redo, clear_redo_outputs, requested_versions
    _versioned_job(tmp_path)

    clear_redo_outputs(tmp_path, cascade_redo({"render"}), versions=requested_versions("6"))

    for n in (1, 8, 12):
        assert (tmp_path / f"shorts_v{n}.mp4").read_bytes() == f"final v{n}".encode()
    assert not (tmp_path / "shorts_v6.mp4").exists()
    backups = list(tmp_path.glob("shorts_v6.mp4.prev_*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"final v6"


def test_redo_table_for_one_version_scopes_versioned_caches(tmp_path):
    from app.tikitaka.cli import cascade_redo, clear_redo_outputs, requested_versions
    _versioned_job(tmp_path)

    clear_redo_outputs(tmp_path, cascade_redo({"verify"}), versions=requested_versions("6"))

    for kind in ("verified", "verify_raw", "table", "table_agentic_raw", "framing", "effects"):
        assert not (tmp_path / f"{kind}_v6.json").exists()
        for n in (1, 8, 12):
            assert (tmp_path / f"{kind}_v{n}.json").exists()
    assert (tmp_path / "script_v6.json").exists()          # rebuild not redone
    assert (tmp_path / "transcript.json").exists()
    assert (tmp_path / "shorts_v1.mp4").exists()


def test_redo_without_version_selection_archives_every_final(tmp_path):
    from app.tikitaka.cli import cascade_redo, clear_redo_outputs, requested_versions
    _versioned_job(tmp_path)
    (tmp_path / "shorts_v6_fast.mp4").write_bytes(b"tagged")

    clear_redo_outputs(tmp_path, cascade_redo({"render"}), versions=requested_versions("auto"))

    # tagged final untouched by an untagged run
    assert (tmp_path / "shorts_v6_fast.mp4").read_bytes() == b"tagged"
    for n in (1, 6, 8, 12):
        assert not (tmp_path / f"shorts_v{n}.mp4").exists()
        assert len(list(tmp_path.glob(f"shorts_v{n}.mp4.prev_*"))) == 1


def test_redo_rebuild_episode_files_unaffected_by_version_scope(tmp_path):
    from app.tikitaka.cli import cascade_redo, clear_redo_outputs, requested_versions
    _versioned_job(tmp_path)
    (tmp_path / "index.json").write_text("{}")

    clear_redo_outputs(tmp_path, cascade_redo({"index"}), versions=requested_versions("6"))

    assert not (tmp_path / "index.json").exists()           # episode-level cache: unchanged behavior
    assert not (tmp_path / "script_v6.json").exists()
    assert (tmp_path / "script_v1.json").exists()
    assert (tmp_path / "shorts_v8.mp4").exists()


def test_range_tag_matches_previous_format():
    from app.tikitaka.cli import range_include, range_tag
    assert range_tag(range_include("03:00~07:00", 1800.0)) == "r0300-0700"
    assert range_tag(range_include("03:00~07:00, 10:00~끝", 750.0)) == "r0300-0700-1000-1230"
    assert range_include("bogus", 100.0) == []


def test_ranged_redo_clears_only_its_derived_tag(tmp_path, monkeypatch):
    from app.tikitaka import cli, probe, transcribe, scenecut
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    out = tmp_path / "job"
    out.mkdir()
    (out / "shorts_v6.mp4").write_bytes(b"untagged v6")
    (out / "shorts_v6_r0300-0700.mp4").write_bytes(b"ranged v6")
    (out / "effects_v6.json").write_text("{}")
    (out / "effects_v6_r0300-0700.json").write_text("{}")
    monkeypatch.setattr(cli, "load_dotenv_if_any", lambda: None)
    monkeypatch.setattr(probe, "probe", lambda j: {"duration_sec": 1800.0})
    for name in ("build_audio", "build_scan_proxy", "build_cut_proxy"):
        monkeypatch.setattr(probe, name, lambda j: source)
    monkeypatch.setattr(scenecut, "detect_scene_cuts", lambda *a: [])
    monkeypatch.setattr(scenecut, "detect_black_spans", lambda *a: [])
    monkeypatch.setattr(transcribe, "transcribe", lambda *a, **kw: {"lines": [], "words": []})

    assert cli.main(["--pipeline", "legacy", "--source", str(source), "--title", "테스트", "--out", str(out),
                     "--range", "03:00~07:00", "--version", "6", "--redo", "effects",
                     "--until", "transcribe"]) == 0

    assert (out / "shorts_v6.mp4").read_bytes() == b"untagged v6"
    assert (out / "effects_v6.json").exists()
    assert not (out / "effects_v6_r0300-0700.json").exists()
    assert not (out / "shorts_v6_r0300-0700.mp4").exists()
    assert len(list(out.glob("shorts_v6_r0300-0700.mp4.prev_*"))) == 1
