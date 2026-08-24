"""L-P2b — 편집실 재렌더(rebuild)·디자인 복원·겹치기 승계 이식 고정.

이식 원본은 vlp `1da2a16`·`2f338e3`(scripts/localize_run.py, 147줄). 이것이 없으면
**사람이 편집실에서 고친 한국어가 일본어판에 한 글자도 반영되지 않는다**
(2026-08-23 SHOTCONE 실측: 새 검수 카드의 ko_ja_pairs 가 직전 카드와 바이트 단위 동일).

이 파일이 지키는 것 셋:
  ① rebuild=False 는 종전과 **완전히 같다** (회귀 0 — 첫 현지화가 안 흔들린다)
  ② rebuild=True 는 낡은 것만 지운다 (한국어 mp3 원본을 같이 날리면 L3t 가 죽는다)
  ③ 승계는 images·texts 만 (자막·제목까지 넘기면 한국어가 일본어를 덮는다)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.localize.collect import (  # noqa: E402
    REBUILD_STALE, invalidate_localize_cache, l0_backup,
)
from app.localize.rerender import (  # noqa: E402
    VISUAL_OVERRIDE_KEYS, design_restore_flags, read_design_cli_file, render_argv,
    visual_only_overrides,
)

LOCALE = {"title_font": "ArialUnicode", "subtitle_font": "ArialUnicode"}


def _job(tmp_path, **files):
    job = tmp_path / "job"
    job.mkdir()
    (job / "shorts.mp4").write_text("렌더본")
    for name, body in files.items():
        (job / name).write_text(body, encoding="utf-8")
    return job


# ── ① rebuild=False 는 종전 그대로 ──────────────────────────────────────
def test_first_run_creates_backup_as_before(tmp_path):
    job = _job(tmp_path, **{"title.txt": "원제목"})
    backup = l0_backup(job)
    assert (backup / "title.txt").read_text(encoding="utf-8") == "원제목"
    assert (job / "shorts_ko.mp4").exists()


def test_rerun_without_rebuild_keeps_the_old_backup(tmp_path):
    """이중 번역 방지의 핵심 — 여기가 흔들리면 일본어를 또 번역한다."""
    job = _job(tmp_path, **{"title.txt": "원제목"})
    l0_backup(job)
    (job / "title.txt").write_text("편집실에서 고친 제목", encoding="utf-8")
    backup = l0_backup(job)                       # rebuild 없이 재실행
    assert (backup / "title.txt").read_text(encoding="utf-8") == "원제목"


def test_rerun_without_rebuild_keeps_the_korean_video(tmp_path):
    job = _job(tmp_path, **{"title.txt": "원제목"})
    l0_backup(job)
    (job / "shorts_ko.mp4").write_text("최초 한국어판")
    l0_backup(job)
    assert (job / "shorts_ko.mp4").read_text() == "최초 한국어판"


# ── ② rebuild=True 는 지금 상태로 갱신한다 ──────────────────────────────
def test_rebuild_refreshes_backup_from_current_job(tmp_path):
    """이 한 줄이 실사고의 수정이다 — 고친 한국어가 번역 입력이 돼야 한다."""
    job = _job(tmp_path, **{"title.txt": "원제목"})
    l0_backup(job)
    (job / "title.txt").write_text("편집실에서 고친 제목", encoding="utf-8")
    backup = l0_backup(job, rebuild=True)
    assert (backup / "title.txt").read_text(encoding="utf-8") == "편집실에서 고친 제목"


def test_rebuild_replaces_the_korean_video(tmp_path):
    """shorts_ko.mp4 는 L2 텔롭 추출·L4 길이 대조의 기준이다 — 낡으면 둘 다 틀린다."""
    job = _job(tmp_path, **{"title.txt": "t"})
    l0_backup(job)
    (job / "shorts_ko.mp4").write_text("낡은 한국어판")
    (job / "shorts.mp4").write_text("편집실 새 렌더")
    l0_backup(job, rebuild=True)
    assert (job / "shorts_ko.mp4").read_text() == "편집실 새 렌더"


def test_rebuild_does_not_wipe_the_backup_directory(tmp_path):
    """🛑 디렉토리째 지우면 L3t 가 보존한 한국어 mp3 원본이 날아간다(덮어쓰기여야 한다)."""
    job = _job(tmp_path, **{"title.txt": "t"})
    backup = l0_backup(job)
    (backup / "cue_0.mp3").write_bytes("한국어 내레이션 원본".encode())
    l0_backup(job, rebuild=True)
    assert (backup / "cue_0.mp3").read_bytes() == "한국어 내레이션 원본".encode()


def test_missing_source_files_are_skipped_not_fatal(tmp_path):
    job = _job(tmp_path)                          # BACKUP_FILES 가 하나도 없다
    backup = l0_backup(job, rebuild=True)
    assert backup.exists()


def test_l0_fails_loudly_without_a_render(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    try:
        l0_backup(job)
    except SystemExit as e:
        assert "shorts.mp4" in str(e)
    else:
        raise AssertionError("렌더본이 없는데 조용히 넘어갔다")


# ── 캐시 폐기 — 낡은 것만, 전부 ─────────────────────────────────────────
def test_invalidate_removes_every_stale_artifact(tmp_path):
    out = tmp_path / "localize_ja"
    out.mkdir()
    for n in REBUILD_STALE:
        (out / n).write_text("{}")
    frames = out / "refine_frames"
    frames.mkdir()
    (frames / "f0.jpg").write_bytes(b"x")
    removed = invalidate_localize_cache(out)
    assert set(removed) == set(REBUILD_STALE) | {"refine_frames/"}
    assert not frames.exists()
    assert not any((out / n).exists() for n in REBUILD_STALE)


def test_invalidate_keeps_what_l3_l5_rewrite(tmp_path):
    """telops.ass·metadata.json 은 매번 다시 쓰인다 — 지울 이유가 없다."""
    out = tmp_path / "localize_ja"
    out.mkdir()
    (out / "telops.ass").write_text("[Events]")
    (out / "metadata.json").write_text("{}")
    invalidate_localize_cache(out)
    assert (out / "telops.ass").exists() and (out / "metadata.json").exists()


def test_invalidate_on_a_clean_dir_is_quiet(tmp_path):
    out = tmp_path / "localize_ja"
    out.mkdir()
    assert invalidate_localize_cache(out) == []


# ── ③ 겹치기 승계는 images·texts 만 ────────────────────────────────────
def test_visual_overrides_drop_korean_text_fields():
    """🛑 자막·제목까지 넘기면 사람이 고친 **한국어**가 일본어판 위에 다시 덮인다."""
    got = visual_only_overrides({
        "schema": "edit_overrides/v3",
        "subtitles": [{"idx": 0, "text": "한국어 자막"}],
        "top_title": "한국어 제목",
        "tts": [{"idx": 0, "text": "한국어 내레이션"}],
        "images": [{"src": "a.png"}],
        "texts": [{"text": "효과음"}],
    })
    assert set(got) == {"schema", "images", "texts"}
    assert got["schema"] == "edit_overrides/v3"


def test_visual_overrides_none_when_nothing_to_carry():
    """빈 승계 파일을 만들어 --edit-overrides 로 넘기면 안 된다."""
    assert visual_only_overrides({"subtitles": [{"idx": 0}]}) is None
    assert visual_only_overrides({"images": [], "texts": []}) is None
    assert visual_only_overrides({}) is None
    assert visual_only_overrides(None) is None


def test_visual_override_keys_are_exactly_two():
    assert VISUAL_OVERRIDE_KEYS == ("images", "texts")


# ── 디자인 복원 ─────────────────────────────────────────────────────────
def test_design_restore_puts_locale_fonts_last():
    """argparse 는 뒤가 이긴다 — 폰트가 앞에 오면 원 런의 폰트가 이겨 일본어가 깨진다."""
    flags = design_restore_flags(
        {"design_cli": ["--design-aspect-ratio", "13:9", "--design-title-font", "Pretendard"]},
        LOCALE)
    assert flags[-4:] == ["--design-title-font", "ArialUnicode",
                          "--design-subtitle-font", "ArialUnicode"]
    assert flags[:2] == ["--design-aspect-ratio", "13:9"]


def test_design_restore_falls_back_to_orchestrator_file():
    """엔진 배포와 오케스트레이터 배포가 서로를 기다리지 않게 — 한 쪽만 있어도 복원된다."""
    flags = design_restore_flags({}, LOCALE, ["--design-video-y", "440"])
    assert "--design-video-y" in flags and "440" in flags


def test_run_log_wins_over_the_fallback_file():
    flags = design_restore_flags({"design_cli": ["--design-video-y", "100"]}, LOCALE,
                                 ["--design-video-y", "999"])
    assert "100" in flags and "999" not in flags


def test_old_runs_get_fonts_only():
    """design_cli 가 없던 옛 런은 종전과 똑같이 폰트 둘 — 회귀 0."""
    assert design_restore_flags({}, LOCALE, []) == [
        "--design-title-font", "ArialUnicode", "--design-subtitle-font", "ArialUnicode"]
    assert design_restore_flags(None, LOCALE) == design_restore_flags({}, LOCALE)


def test_design_cli_file_missing_or_broken_is_not_fatal(tmp_path):
    """깨진 파일에 잡을 걸지 않는다 — 경고를 남기고 종전처럼 그린다."""
    job = tmp_path / "job"
    job.mkdir()
    assert read_design_cli_file(job) == []
    (job / "design_cli.json").write_text("{깨짐")
    assert read_design_cli_file(job) == []
    (job / "design_cli.json").write_text('{"not": "a list"}')
    assert read_design_cli_file(job) == []


def test_design_cli_file_reads_a_list(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "design_cli.json").write_text(json.dumps(["--design-video-y", 440]))
    assert read_design_cli_file(job) == ["--design-video-y", "440"]   # 전부 문자열로


# ── argv — 종전 호출은 종전 그대로 ─────────────────────────────────────
def test_render_argv_without_design_flags_is_unchanged(tmp_path):
    """P1 이 고정한 모양 그대로 — 옛 호출자가 있으면 산출이 안 바뀐다."""
    argv = render_argv("/py", tmp_path / "j", "작품", "/v.mp4", LOCALE, ["--fast"])
    assert argv[-3:] == ["--max-shorts", "1", "--fast"]
    assert "--design-title-font" in argv and "--edit-overrides" not in argv


def test_render_argv_appends_overrides_after_gen_flags(tmp_path):
    argv = render_argv("/py", tmp_path / "j", "작품", "/v.mp4", LOCALE, ["--fast"],
                       ["--design-video-y", "440",
                        "--design-title-font", "ArialUnicode",
                        "--design-subtitle-font", "ArialUnicode"],
                       ["--edit-overrides", "/ov.json"])
    assert argv[-2:] == ["--edit-overrides", "/ov.json"]
    assert argv[argv.index("--design-video-y") + 1] == "440"
    # gen_flags 는 여전히 --max-shorts 뒤 (컷 재현이 그 순서에 달려 있다)
    assert argv[argv.index("--max-shorts") + 2] == "--fast"


def test_render_argv_keeps_job_id_pinning(tmp_path):
    """--job-id 가 빠지면 제목이 다를 때 디렉토리가 새로 생겨 재렌더가 딴 데 떨어진다."""
    argv = render_argv("/py", tmp_path / "myjob", "작품", "/v.mp4", LOCALE, [])
    assert argv[argv.index("--job-id") + 1] == "myjob"
    assert argv[argv.index("--from-step") + 1] == "render"
