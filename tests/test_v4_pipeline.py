"""V4-M2 배선 회귀 가드 — `app/v4/pipeline.py` · `app/v4/cli.py`.

계약 `docs/v4/M2-interfaces.md` §1(배선)·§2(표본 fps 사전검사)·§6(CLI).

이 파일이 고정하는 것은 **v3 배선이 남긴 사고를 v4 가 반복하지 않는다**는 사실이다:
단계 판정이 하나인지 · run_log 가 단계마다 디스크에 확정되는지 · 캐시 히트와 스킵이
기록으로 남는지 · 미구현 단계가 조용히 아니라 이름과 마일스톤을 남기고 끝나는지 ·
`input.video_path`(M0 채점기가 읽는 열쇠)가 있는지.

전사(whisper)와 업로드(Files API)는 가짜다 — 나머지(ffmpeg 계측·무음·장면 전환·
arousal·span 재단·표본 fps)는 **합성 소재로 진짜 돈다**.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.v4 import cli as v4cli
from app.v4 import pipeline as v4p

WORDS = [{"t0": 0.6, "t1": 1.2, "text": "테스트", "prob": 0.9},
         {"t0": 1.4, "t1": 2.0, "text": "대사다.", "prob": 0.9},
         {"t0": 4.2, "t1": 4.8, "text": "끝.", "prob": 0.9}]


@pytest.fixture(scope="module")
def synth(tmp_path_factory) -> Path:
    """합성 소재 6초(영상+오디오) — test_v3_stage2 의 본보기와 같은 방식."""
    d = tmp_path_factory.mktemp("v4src")
    src = d / "src.mp4"
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=12:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    return src


@pytest.fixture
def fake_transcribe(monkeypatch):
    """whisper 는 이 venv 에 없다(그리고 있어도 느리다) — 호출 횟수를 세는 가짜."""
    calls = {"n": 0}

    def fake_words(audio_path, duration_sec, **kw):
        calls["n"] += 1
        assert Path(audio_path).exists(), "오디오 추출이 선행돼야 한다"
        return [dict(w) for w in WORDS], []

    monkeypatch.setattr(v4p, "transcribe_words", fake_words)
    monkeypatch.setattr(v4p, "retranscribe_gaps",
                        lambda *a, **k: (list(a[1]), {"gaps": 0, "windows": [],
                                                      "recovered_words": 0}))
    return calls


def _run_log(out: Path) -> dict:
    return json.loads((out / "run_log.json").read_text(encoding="utf-8"))


def _steps(out: Path) -> list[dict]:
    return _run_log(out)["steps"]


def _entry(out: Path, name: str) -> dict:
    """그 단계의 **마지막** 기록(재개하면 같은 이름이 여러 번 쌓인다)."""
    got = [s for s in _steps(out) if s["step"] == name]
    assert got, f"{name} 단계 기록이 없다: {[s['step'] for s in _steps(out)]}"
    return got[-1]


# ── 1~4단계가 실제로 돈다 ───────────────────────────────────────────────────

def test_stop_after_probe_runs_for_real(synth, tmp_path, fake_transcribe):
    out = v4p.run_v4(video_path=synth, work_title="합성 소재", outdir=tmp_path / "o",
                     skip_research=True, stop_after="probe", log=lambda *a: None)

    assert (out / "grid.json").exists() and (out / "checkpoint_probe.json").exists()
    assert (out / "checkpoint_grid_words.json").exists()
    grid = json.loads((out / "grid.json").read_text(encoding="utf-8"))
    assert grid["schema"] == "v3_grid/v1"
    assert len(grid["words"]) == len(WORDS) and grid["span_candidates"]
    assert grid["source"]["duration_sec"] > 5.0

    rl = _run_log(out)
    # 마커는 **계약**이라 마일스톤이 올라가도 안 바뀐다 — 현지화·어댑터·로더가 이걸로
    # 분기한다(기획서 §6). 진행 상황은 별도 키(milestone)다. v3 는 마커 자체를 바꿔
    # (v3_m1 → v3_m3) 읽는 쪽이 접두 일치를 해야 했다.
    assert rl["pipeline"] == "v4" and rl["schema"] == "run_log/v1"
    assert rl["milestone"] == v4p.PIPELINE_MILESTONE
    # M0 채점기(app/replay/exception_score.py:137)가 읽는 열쇠 — 빼먹으면 조용히 못 찾는다
    assert rl["input"]["video_path"] == str(synth)
    assert rl["input"]["work_title"] == "합성 소재"

    names = [s["step"] for s in rl["steps"]]
    assert names[:4] == ["init", "research", "transcribe", "probe"]
    assert _entry(out, "research")["skipped"] == "--skip-research"

    probe = _entry(out, "probe")
    assert probe["sample_fps"] == 4.0, "6초 소재는 계단 첫 칸(40분 이하 → 4fps)"
    assert probe["sample_fps_note"]["reason"] == "ladder"
    # 계약 §2 — 텍스트 토큰은 아직 추정이고 그 사실이 값에 명시돼야 한다
    assert probe["text_tokens_note"]["text_tokens_estimated"] is True
    ckpt = json.loads((out / "checkpoint_probe.json").read_text(encoding="utf-8"))
    assert ckpt["sample_fps"] == 4.0 and ckpt["duration_sec"] > 5.0
    assert ckpt["text_tokens_note"]["text_tokens_estimated"] is True

    # --stop-after 는 스킵을 **기록**한다(조용한 스킵 금지) + 남은 단계를 전량 나열
    stop = _entry(out, "upload")
    assert stop["skipped"] == "--stop-after probe"
    assert stop["remaining"][0] == "upload" and stop["remaining"][-1] == "11:validate"
    assert fake_transcribe["n"] == 1


def test_resume_appends_and_records_cache_hits(synth, tmp_path, fake_transcribe):
    o = tmp_path / "o"
    out = v4p.run_v4(video_path=synth, work_title="합성", outdir=o,
                     skip_research=True, stop_after="probe", log=lambda *a: None)
    v4p.run_v4(video_path=synth, work_title="합성", outdir=o, job_id=out.name,
               skip_research=True, stop_after="probe", log=lambda *a: None)

    assert fake_transcribe["n"] == 1, "재개는 전사를 다시 태우지 않는다(가장 비싼 단계)"
    names = [s["step"] for s in _steps(out)]
    assert "resume" in names, "재개는 기존 run_log 에 이어 쓴다(감사 기록 유실 금지)"
    # 🛑 캐시 히트가 기록으로 남는다 — v3 는 무기록이라 '단계 부재'가 '안 돌았다'인지
    #    '캐시였다'인지 구분되지 않았다(gotcha 5).
    assert _entry(out, "transcribe")["cached"] is True
    assert _entry(out, "transcribe")["cache_reason"] == "지문 일치"
    assert _entry(out, "probe")["cached"] is True
    assert _entry(out, "probe")["media_from_cache"] is True
    assert _entry(out, "probe")["sample_fps"] == 4.0, "캐시여도 fps 판정은 돈다"


def test_from_step_probe_invalidates_only_downstream(synth, tmp_path, fake_transcribe):
    o = tmp_path / "o"
    out = v4p.run_v4(video_path=synth, work_title="합성", outdir=o,
                     skip_research=True, stop_after="probe", log=lambda *a: None)
    v4p.run_v4(video_path=synth, work_title="합성", outdir=o, job_id=out.name,
               from_step="probe", stop_after="probe", log=lambda *a: None,
               skip_research=True)

    # 상류(전사)는 캐시, 자기 자신(probe)은 재구성 — 판정은 should_run 하나가 한다
    assert fake_transcribe["n"] == 1
    assert _entry(out, "transcribe")["cached"] is True
    probe = _entry(out, "probe")
    assert probe["cached"] is False and probe["cache_reason"] == "--from-step 무효화"
    assert probe["media_from_cache"] is False


def test_transcribe_cache_survives_scene_threshold_change(synth, tmp_path,
                                                          fake_transcribe):
    """계약 §1 의 의도된 비대칭 — 격자 지문은 장면 임계를 보고, 전사 지문은 안 본다."""
    o = tmp_path / "o"
    out = v4p.run_v4(video_path=synth, work_title="합성", outdir=o,
                     skip_research=True, stop_after="probe", log=lambda *a: None)
    v4p.run_v4(video_path=synth, work_title="합성", outdir=o, job_id=out.name,
               skip_research=True, stop_after="probe", scene_threshold=0.9,
               log=lambda *a: None)

    assert fake_transcribe["n"] == 1, "임계를 바꿔도 전사는 다시 안 돈다"
    probe = _entry(out, "probe")
    assert probe["cached"] is False
    assert probe["cache_reason"].startswith("지문 불일치")


def test_srt_is_a_grid_layer_not_a_transcript_replacement(synth, tmp_path,
                                                          fake_transcribe):
    srt = tmp_path / "sub.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n안녕\n\n", encoding="utf-8")
    lines: list[str] = []
    out = v4p.run_v4(video_path=synth, work_title="합성", outdir=tmp_path / "o",
                     srt_path=srt, skip_research=True, stop_after="probe",
                     log=lambda *a: lines.append(" ".join(str(x) for x in a)))

    grid = json.loads((out / "grid.json").read_text(encoding="utf-8"))
    assert grid["srt_cues"] and grid["transcript"]["backend"] == "whisper"
    assert fake_transcribe["n"] == 1, "SRT 가 있어도 전사는 돈다(시각 정본은 단어다)"
    assert _entry(out, "transcribe")["srt_layer"] is True
    assert any("SRT 제공" in ln for ln in lines), "무엇을 하는지 stdout 에 남긴다"


# ── run_log 는 죽어도 남는다 ────────────────────────────────────────────────

def test_run_log_is_on_disk_before_the_failure(synth, tmp_path, fake_transcribe,
                                               monkeypatch):
    """🛑 v3 는 run_log 를 finally 한 곳에서만 썼다(gotcha 1). v4 는 단계마다 확정한다."""
    def boom(duration_sec, **kw):
        err = ValueError("소재가 너무 길어 한 번에 넣을 수 없다(합성)")
        err.note = {"reason": "floor_failed", "duration_sec": duration_sec}
        raise err

    monkeypatch.setattr(v4p.fps_mod, "resolve_sample_fps", boom)
    o = tmp_path / "o"
    with pytest.raises(ValueError):
        v4p.run_v4(video_path=synth, work_title="합성", outdir=o,
                   skip_research=True, stop_after="probe", log=lambda *a: None)

    out = next(p for p in o.iterdir() if p.is_dir())
    names = [s["step"] for s in _steps(out)]
    assert names[:3] == ["init", "research", "transcribe"]
    probe = _entry(out, "probe")
    assert "error" in probe and probe["sample_fps"] is None
    assert probe["sample_fps_note"]["reason"] == "floor_failed"
    assert _entry(out, "error")["at"] == "probe"
    # 계약 §2 — checkpoint_probe.json 은 "쓰고 죽는다"(왜 죽었는지가 job 에 남는다)
    ckpt = json.loads((out / "checkpoint_probe.json").read_text(encoding="utf-8"))
    assert ckpt["sample_fps"] is None and ckpt["sample_fps_note"]["reason"] == "floor_failed"
    # 프록시·업로드는 시작조차 안 했다 — 사전검사가 그 앞이라는 것이 계약이다
    assert not list(out.glob("*720p*")) and not (out / "checkpoint_upload.json").exists()


def test_research_needs_a_key_and_says_so(synth, tmp_path, fake_transcribe,
                                          monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="--skip-research"):
        v4p.run_v4(video_path=synth, work_title="합성", outdir=tmp_path / "o",
                   stop_after="probe", log=lambda *a: None)


# ── 5단계 + 미구현 단계 ────────────────────────────────────────────────────

def _fake_proxy_module(rec: dict) -> types.ModuleType:
    """`app/v4/proxy.py`(계약 §3)의 가짜 — 그 모듈은 별건이라 시그니처만 맞춘다."""
    m = types.ModuleType("app.v4.proxy")
    m.PROXY_HEIGHT, m.PROXY_FILE_FPS, m.PROXY_CRF = 720, 30.0, 30
    m.proxy_path_for = lambda outdir, *, height, file_fps: (
        Path(outdir) / f"scan_{height}p{int(file_fps)}.mp4")
    m.proxy_fingerprint = lambda video, **kw: "fp-proxy"
    m.upload_checkpoint_doc = lambda *, fingerprint, proxy_path, proxy_meta, handle_meta: {
        "schema": "v4_upload/v1", "fingerprint": fingerprint,
        "proxy": proxy_meta, "handle": handle_meta}
    def build_proxy(video, out_path, **kw):
        rec["built"] = rec.get("built", 0) + 1
        Path(out_path).write_bytes(b"fake-proxy")
        return Path(out_path), {"height": 720, "file_fps": 30.0, "crf": 30,
                                "bytes": 10, "elapsed_sec": 0.0, "reused": False}
    def upload_handle(gemini, proxy, **kw):
        rec["uploaded"] = rec.get("uploaded", 0) + 1
        return object(), {"uri": "files/x", "name": "files/x", "bytes": 10,
                          "elapsed_sec": 0.0}
    m.build_proxy = build_proxy
    m.upload_handle = upload_handle
    m.handle_alive = lambda gemini, ref: rec.get("alive", True)
    return m


@pytest.fixture
def fake_proxy(monkeypatch):
    import app.v4 as v4pkg

    rec: dict = {}
    mod = _fake_proxy_module(rec)
    monkeypatch.setitem(sys.modules, "app.v4.proxy", mod)
    monkeypatch.setattr(v4pkg, "proxy", mod, raising=False)
    monkeypatch.setattr(v4p, "_load_gemini_client", lambda: object())
    return rec


def test_unimplemented_steps_end_cleanly_with_a_milestone(synth, tmp_path,
                                                          fake_transcribe, fake_proxy):
    out = v4p.run_v4(video_path=synth, work_title="합성", outdir=tmp_path / "o",
                     skip_research=True, log=lambda *a: None)

    assert fake_proxy["built"] == 1 and fake_proxy["uploaded"] == 1
    up = _entry(out, "upload")
    assert up["handle"]["uri"] == "files/x"
    assert (out / "checkpoint_upload.json").exists()

    last = _steps(out)[-1]
    assert last["step"] == "candidates" and last["not_implemented"] == "M3"
    assert last["remaining"][0] == "candidates" and last["remaining"][-1] == "11:validate"
    # 미구현이라고 예외로 죽지 않는다 — job 디렉토리를 돌려주고 정상 종료한다
    assert out.is_dir()


def test_dead_upload_handle_is_replaced_not_reused(synth, tmp_path, fake_transcribe,
                                                   fake_proxy):
    o = tmp_path / "o"
    out = v4p.run_v4(video_path=synth, work_title="합성", outdir=o,
                     skip_research=True, stop_after="upload", log=lambda *a: None)
    assert fake_proxy["uploaded"] == 1

    fake_proxy["alive"] = False          # 48h 만료를 흉내
    v4p.run_v4(video_path=synth, work_title="합성", outdir=o, job_id=out.name,
               skip_research=True, stop_after="upload", log=lambda *a: None)
    assert fake_proxy["uploaded"] == 2
    assert "핸들 만료" in _entry(out, "upload")["cache_reason"]

    fake_proxy["alive"] = True
    v4p.run_v4(video_path=synth, work_title="합성", outdir=o, job_id=out.name,
               skip_research=True, stop_after="upload", log=lambda *a: None)
    assert fake_proxy["uploaded"] == 2 and _entry(out, "upload")["cached"] is True


def test_upload_without_the_proxy_module_fails_loudly(synth, tmp_path,
                                                      fake_transcribe, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_proxy(name, *a, **k):
        if name == "app.v4.proxy" or (name == "app.v4" and "proxy" in (a[2] or ())):
            raise ImportError("no proxy module (합성)")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_proxy)
    with pytest.raises(RuntimeError, match="--stop-after probe"):
        v4p.run_v4(video_path=synth, work_title="합성", outdir=tmp_path / "o",
                   skip_research=True, stop_after="upload", log=lambda *a: None)


# ── 인자 정규화(비싼 일 앞) ────────────────────────────────────────────────

def test_bad_arguments_die_before_any_work(synth, tmp_path):
    o = tmp_path / "o"
    with pytest.raises(ValueError, match="--from-step"):
        v4p.run_v4(video_path=synth, work_title="합성", outdir=o, from_step="grid",
                   log=lambda *a: None)
    with pytest.raises(ValueError, match="max_shorts"):
        v4p.run_v4(video_path=synth, work_title="합성", outdir=o, max_shorts=9,
                   log=lambda *a: None)
    with pytest.raises(ValueError, match="아무 단계도 돌지 않는다"):
        v4p.run_v4(video_path=synth, work_title="합성", outdir=o,
                   from_step="probe", stop_after="research", log=lambda *a: None)
    assert not o.exists(), "인자 오류는 job 디렉토리를 만들기 전에 죽는다"


# ── 단계 판정은 should_run 하나다 ──────────────────────────────────────────

def test_no_hand_written_step_membership_checks():
    """🛑 v3 는 손으로 적은 멤버십 검사 7곳이 각각 다른 상류를 봤다(gotcha 4).

    v4 배선에는 그런 검사가 **한 줄도 없어야** 한다 — 단계를 더할 때 고칠 곳이
    `V4_STEPS` 한 줄뿐이어야 아무도 안 놓친다.
    """
    src = Path(v4p.__file__).read_text(encoding="utf-8")
    # 단계 이름을 직접 비교하는 코드가 있는지 **AST** 로 본다(독스트링·주석은 규율을
    # 설명해야 하므로 문자열 검색으로는 못 가린다). 허용되는 비교는 둘뿐이다:
    # "재개 지점이 지정됐는가"(is not None)와 순번 비교(STEP_ORDER).
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Compare):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if "from_step" not in seg and "stop_after" not in seg:
            continue
        assert "is not None" in seg or "STEP_ORDER" in seg, \
            f"손으로 적은 단계 멤버십 비교가 있다: {seg!r}"
    # 판정은 한 곳(_invalidated)이고 should_run 은 그 안에서만 불린다
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert code.count("should_run(") == 1, "should_run 호출이 흩어져 있다"
    assert code.count("_invalidated(") >= 5, "각 단계가 같은 판정 함수를 써야 한다"


def test_every_step_is_wired_or_declared_unimplemented():
    """단계 표에 이름을 더했는데 배선이 모르면 KeyError 로 죽는다 — 표와 배선을 묶는다."""
    from app.v4.steps import V4_STEPS

    handled = {"init", "research", "transcribe", "probe", "upload"}
    for name in V4_STEPS:
        assert name in handled or name in v4p.NOT_IMPLEMENTED_MILESTONE, name


# ── CLI (계약 §6) ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _allow_ffmpeg8(monkeypatch):
    """이 개발 머신엔 ffmpeg 8 뿐이다(운영 노드는 7.x) — CLI 의 버전 관문만 연다."""
    monkeypatch.setenv("AI_VIDEO_ALLOW_UNSUPPORTED_FFMPEG", "1")


def test_cli_rejects_unknown_flags(synth):
    p = v4cli.build_parser()
    with pytest.raises(SystemExit) as e:
        p.parse_args(["--video", str(synth), "--work-title", "x", "--skip-stage2"])
    assert e.value.code == 2
    # v3 의 다섯 스킵 플래그는 만들지 않는다 — --stop-after 하나다
    opts = {a for act in p._actions for a in act.option_strings}
    assert "--stop-after" in opts
    for gone in ("--skip-stage2", "--skip-stage3", "--skip-stage4",
                 "--skip-seq-analyze"):
        assert gone not in opts


def test_cli_rejects_bad_step_and_max_shorts(synth, tmp_path):
    base = ["--video", str(synth), "--work-title", "x", "--outdir", str(tmp_path)]
    with pytest.raises(SystemExit) as e:
        v4cli.main(base + ["--from-step", "grid"])
    assert "정본 id" in str(e.value), "허용 목록 전량을 메시지에 싣는다"
    with pytest.raises(SystemExit) as e:
        v4cli.main(base + ["--max-shorts", "9"])
    assert "max_shorts" in str(e.value)
    with pytest.raises(SystemExit) as e:
        v4cli.main(base + ["--from-step", "probe", "--stop-after", "research"])
    assert "아무 단계도 돌지 않는다" in str(e.value)


def test_cli_gemini_key_gate_is_the_sixth_step(monkeypatch, synth, tmp_path):
    assert v4cli.needs_gemini_key(None) is True
    assert v4cli.needs_gemini_key("probe") is False
    assert v4cli.needs_gemini_key("upload") is False
    assert v4cli.needs_gemini_key("candidates") is True
    assert v4cli.needs_gemini_key("11:validate") is True

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        v4cli.main(["--video", str(synth), "--work-title", "x",
                    "--outdir", str(tmp_path), "--skip-research"])


def test_cli_runs_without_a_key_up_to_probe(synth, tmp_path, fake_transcribe,
                                            monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rc = v4cli.main(["--video", str(synth), "--work-title", "합성",
                     "--outdir", str(tmp_path / "o"), "--skip-research",
                     "--stop-after", "probe", "--max-shorts", "3"])
    assert rc == 0
    out = next(p for p in (tmp_path / "o").iterdir() if p.is_dir())
    assert (out / "grid.json").exists()
    assert _entry(out, "init")["max_shorts"] == 3
    assert "완료" in capsys.readouterr().out


def test_upload_calls_match_the_real_proxy_module():
    """가짜로 테스트한 5단계가 **실물** `app/v4/proxy.py`(계약 §3)와도 맞는지.

    proxy 는 다른 사람이 짓는 모듈이라, 여기서 이름과 시그니처를 묶어 두지 않으면
    가짜만 통과하고 실런에서 TypeError 로 죽는다.
    """
    import inspect

    from app.v4 import proxy

    for name in ("PROXY_HEIGHT", "PROXY_FILE_FPS", "PROXY_CRF"):
        assert isinstance(getattr(proxy, name), (int, float))
    sigs = {
        "proxy_path_for": ("output_dir", "height", "file_fps"),
        "build_proxy": ("video_path", "out_path", "log"),
        "upload_handle": ("gemini", "proxy", "log"),
        "handle_alive": ("gemini", "uri_or_name"),
        "proxy_fingerprint": ("video_path",),
        "upload_checkpoint_doc": ("fingerprint", "proxy_path", "proxy_meta",
                                  "handle_meta"),
    }
    for fn, params in sigs.items():
        got = inspect.signature(getattr(proxy, fn)).parameters
        for pname in params:
            assert pname in got, f"proxy.{fn} 에 {pname} 인자가 없다 — 배선이 깨진다"
