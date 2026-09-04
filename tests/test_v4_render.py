"""V4-M6/M7 §2~§4 회귀 가드 — `app/v4/render.py`.

이 파일이 값으로 고정하는 것은 계약 `docs/v4/M6-interfaces.md` 의 문장들이다:

① **초벌은 720p/30fps 이고 입력 seek 으로 만든다**(운영자 결정 O9 + §2 의 🛑).
   v3 는 `-i 원본` 에 `[0:v]trim` 을 매달아 소스 전체를 디코드한다 — 3시간 소재면
   뒤쪽 클립 하나 때문에 3시간을 읽는다. 구조(argv)와 실제 소요 **둘 다** 잰다:
   argv 만 보면 "인자는 맞는데 결과가 다르다"를, 시간만 보면 "우연히 빨랐다"를 놓친다.
② **스타일 호출은 media_resolution=HIGH 를 싣는다**(O9) — 그리고 **프롬프트·검증기는
   v3 것 그대로**다. 이 둘은 한 몸이다: 호출만 바꾸는 것이 이 모듈의 설계다.
③ **산출 이름** — 최종본은 `shorts.mp4`(현지화 `RENDER_OUTPUT`), 2위↓만 `_{n}`.
④ **검증 어댑터의 왕복** — `story.build_span_index(만든 stage2_doc, grid)` 가 v4 의
   span_index 와 소비되는 열쇠에 한해 같아야 한다. 안 그러면 TTS 충돌 벨트가 조용히
   다른 것을 잰다. exception 구역은 **문자열 시각**이어야 한다(0.0 은 falsy 다).
⑤ **hard_fail 은 예외가 아니다** — 그 편만 실패로 기록하고 배선이 다음 편으로 간다.

🛑 네트워크는 쓰지 않는다(가짜 gemini). 실호출로만 아는 것 — HIGH 가 실제로 어떤
프레임을 모델에 보여 주는가 — 은 이 파일의 범위 밖이다.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.localize import RENDER_OUTPUT
from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.v3 import finalize, stage4
from app.v3 import story as story_mod
from app.v4 import bridge, proxy
from app.v4 import render as R
from app.v4 import video as V


@pytest.fixture(autouse=True)
def _allow_ffmpeg8(monkeypatch):
    """이 머신은 ffmpeg 8.x 뿐이다(운영은 7.x) — 기존 렌더 테스트와 같은 관문."""
    monkeypatch.setenv("AI_VIDEO_ALLOW_UNSUPPORTED_FFMPEG", "1")


# ── 합성 소재 ───────────────────────────────────────────────────────────────

def _synth(path: Path, seconds: float) -> Path:
    """testsrc2 + sine — 다른 v3/v4 테스트와 같은 방식."""
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-f", "lavfi",
         "-i", f"testsrc2=size=320x240:rate=10:duration={seconds:g}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds:g}",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(path)], check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def long_source(tmp_path_factory) -> Path:
    """600초 소재 — 인코딩 3~4초. ① 의 시간 측정이 이 길이를 필요로 한다."""
    return _synth(tmp_path_factory.mktemp("src") / "long.mp4", 600)


def _tl(*windows, muted: tuple[int, ...] = ()) -> list[dict]:
    return [{"role": "build", "clip_start_sec": float(a), "clip_end_sec": float(b),
             "use_original_audio": i not in muted}
            for i, (a, b) in enumerate(windows)]


# ── ① 초벌 — 입력 seek(구조) ────────────────────────────────────────────────

def test_draft_command_opens_the_source_once_per_clip(tmp_path):
    """🛑 계약 §2 의 전부 — 클립마다 `-ss/-t -i` 다(v3 는 `-i` 하나 + trim)."""
    argv = R.draft_command(tmp_path / "src.mp4", _tl((0.0, 1.0), (598.0, 599.0)),
                           tmp_path / "draft.mp4")
    assert argv.count("-i") == 2, "클립 수만큼 입력을 연다"
    assert argv.count("-ss") == 2 and argv.count("-t") == 2
    i0 = argv.index("-ss")
    assert argv[i0:i0 + 4] == ["-ss", "0.000", "-t", "1.000"]
    i1 = argv.index("-ss", i0 + 1)
    # 🛑 두 번째 클립도 **자기 시작점에서** 연다 — 여기가 소재 길이 독립성의 근거다
    assert argv[i1:i1 + 4] == ["-ss", "598.000", "-t", "1.000"]
    # `-to` 는 판본마다 기준이 헷갈리는 자리라 쓰지 않는다(길이는 한 가지 뜻이다)
    assert "-to" not in argv


def test_draft_filtergraph_has_no_trim_and_keeps_v3_vocabulary(tmp_path):
    """어휘는 v3 그대로 — 화면이 달라지면 그 위의 스타일 판정이 달라진다."""
    argv = R.draft_command(tmp_path / "src.mp4",
                           _tl((0.0, 1.0), (10.0, 12.0), muted=(1,)), tmp_path / "d.mp4")
    fg = argv[argv.index("-filter_complex") + 1]
    assert "trim=" not in fg, "🛑 trim 이 있으면 소스 전체를 디코드한다(v3 의 그 자리)"
    assert "[0:v]setpts=PTS-STARTPTS,scale=-2:720,fps=30[v0]" in fg
    assert "[1:v]setpts=PTS-STARTPTS,scale=-2:720,fps=30[v1]" in fg
    assert "[0:a]asetpts=PTS-STARTPTS[a0]" in fg          # 소리 살린 클립
    assert "[1:a]asetpts=PTS-STARTPTS,volume=0[a1]" in fg  # 뮤트 클립(v3 어휘)
    assert fg.endswith("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]")


def test_draft_rejects_unrenderable_timeline(tmp_path):
    """빈 타임라인·물리적으로 못 그리는 구간은 크게 실패한다(조용한 결번 금지)."""
    with pytest.raises(ValueError, match="비어 있다"):
        R.draft_command(tmp_path / "s.mp4", [], tmp_path / "d.mp4")
    with pytest.raises(ValueError, match="렌더 불가"):
        R.draft_command(tmp_path / "s.mp4", _tl((5.0, 5.0)), tmp_path / "d.mp4")
    # 최소 길이는 `video.MIN_CLIP_SEC` 한 곳에서 온다(복제 금지)
    assert V.MIN_CLIP_SEC == 0.05


# ── ① 초벌 — 실제 산출(O9) ─────────────────────────────────────────────────

def test_draft_is_720p30(tmp_path, long_source):
    """운영자 결정 O9 — 인자 문자열이 아니라 **만들어진 파일**을 잰다."""
    out = tmp_path / "draft.mp4"
    cost = R.render_draft(long_source, _tl((0.0, 1.0), (598.0, 599.0)), out,
                          log=lambda *a: None)
    geo = proxy.probe_geometry(out)
    assert geo["height"] == R.DRAFT_HEIGHT == 720
    assert abs(geo["fps"] - R.DRAFT_FPS) <= proxy.FPS_TOLERANCE
    assert R.DRAFT_FPS == 30.0
    assert geo["duration_sec"] == pytest.approx(2.0, abs=0.15), "두 클립이 다 들어간다"
    assert cost["clips"] == 2 and cost["bytes"] > 0
    # 열쇠 이름은 v4 규약(`proxy.build_proxy`) — v3 의 `elapsed` 가 아니다
    assert set(cost) == {"height", "fps", "clips", "bytes", "elapsed_sec", "geometry"}


def test_draft_name_carries_the_height(tmp_path):
    """`draft_720.mp4` — 이름은 기하에서 파생한다(480p 잔재를 재사용하지 않게)."""
    assert R.render_paths(tmp_path)["draft"].name == "draft_720.mp4"
    assert R.render_paths(tmp_path, 3)["draft"].name == "draft_720_3.mp4"


def test_draft_time_is_independent_of_clip_position(tmp_path, long_source):
    """🛑 계약 §2 의 이유 그 자체 — 소요가 **클립 길이**에 비례해야 한다.

    같은 길이(1s×2)의 클립을 소재 **앞**과 **뒤**에서 딴다. 입력 seek 이면 두 소요가
    같고, v3 처럼 전체를 디코드하면 뒤쪽이 소재 길이만큼 비싸진다. 대조군으로 v3
    `stage4.render_draft` 를 같은 입력에 태워 '측정이 차이를 실제로 본다'를 보인다.

    실측(이 머신 · 600초 소재 · ffmpeg 8.1.2):
        v4 앞 0.12s · v4 뒤 0.13s   ← 위치와 무관
        v3 앞 0.08s · v3 뒤 0.51s   ← 뒤쪽 클립 때문에 앞 전체를 디코드
    """
    def timed(fn, out):
        t0 = time.time()
        fn(out)
        return time.time() - t0

    head, tail = _tl((0.0, 1.0), (4.0, 5.0)), _tl((0.0, 1.0), (598.0, 599.0))
    v4_head = timed(lambda o: R.render_draft(long_source, head, o, log=lambda *a: None),
                    tmp_path / "v4_head.mp4")
    v4_tail = timed(lambda o: R.render_draft(long_source, tail, o, log=lambda *a: None),
                    tmp_path / "v4_tail.mp4")
    v3_tail = timed(lambda o: stage4.render_draft(long_source, tail, o,
                                                  height=R.DRAFT_HEIGHT,
                                                  log=lambda *a: None),
                    tmp_path / "v3_tail.mp4")

    assert v4_tail <= v4_head * 3 + 0.5, (
        f"뒤쪽 클립이 앞쪽보다 비싸다 — 입력 seek 이 아니다 "
        f"(앞 {v4_head:.2f}s · 뒤 {v4_tail:.2f}s)")
    assert v4_tail < v3_tail * 0.8, (
        f"v3 전체 디코드보다 빠르지 않다 (v4 {v4_tail:.2f}s · v3 {v3_tail:.2f}s) — "
        f"측정이 차이를 못 보는 것이라면 소재를 더 길게 잡아야 한다")


def test_draft_leaves_no_half_file_when_ffmpeg_dies(tmp_path):
    """원자 교체 — 중단된 인코딩을 다음 실행이 '존재하므로 재사용'하면 안 된다."""
    out = tmp_path / "draft.mp4"
    with pytest.raises(RuntimeError, match="stderr 꼬리"):
        R.render_draft(tmp_path / "없는소재.mp4", _tl((0.0, 1.0)), out,
                       log=lambda *a: None)
    assert not out.exists()
    assert list(tmp_path.glob(".*part*")) == []


def test_draft_fails_loud_when_geometry_is_not_o9(tmp_path, long_source, monkeypatch):
    """만든 것을 다시 잰다 — O9 가 아닌 파일로 스타일을 판정하면 안 된다."""
    monkeypatch.setattr(R.proxy_mod, "probe_geometry",
                        lambda p: {"height": 480, "fps": 30.0, "duration_sec": 2.0})
    with pytest.raises(RuntimeError, match="O9"):
        R.render_draft(long_source, _tl((0.0, 1.0)), tmp_path / "d.mp4",
                       log=lambda *a: None)


# ── ② 스타일 — 가짜 gemini ─────────────────────────────────────────────────

def _response(text: str):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=100, thoughts_token_count=10,
                                       candidates_token_count=20,
                                       cached_content_token_count=0,
                                       total_token_count=130),
        model_version="gemini-3.7-flash-001",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))])


class _FakeModels:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.queue.pop(0) if self.queue else _response('{"design": {}}')
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeFiles:
    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, *, name):
        self.deleted.append(name)


class _FakeGemini:
    def __init__(self, queue=()):
        from google.genai import types
        self.types = types
        self.models = _FakeModels(queue)
        self.files = _FakeFiles()
        self.client = SimpleNamespace(models=self.models, files=self.files)
        self.config = SimpleNamespace(flash_model_name="gemini-3.7-flash",
                                      model_name="gemini-3.7-pro")


@pytest.fixture
def fake_upload(monkeypatch):
    """업로드는 실제 배선(`proxy.upload_handle` → v3 `_upload_video`)을 지나되
    네트워크만 가짜다 — 여기서 `proxy.upload_handle` 을 통째로 가짜로 바꾸면
    '초벌 핸들을 지우는가'를 검사할 수 없다."""
    from app.v3 import seq_analyze
    uploaded: list[Path] = []

    def _fake(gemini, path, log=print):
        uploaded.append(Path(path))
        return SimpleNamespace(
            uri="https://generativelanguage.googleapis.com/v1beta/files/draft1",
            name="files/draft1", size_bytes=123)

    monkeypatch.setattr(seq_analyze, "_upload_video", _fake)
    return uploaded


STORY_DOC = {"beats": [{"number": 0, "role": "hook", "label": "충격",
                        "span_ids": ["sp0001"],
                        "time": {"start": "00:00:05.000", "end": "00:00:08.000"}}]}
WINDOWS = [{"beat": 0, "start": 0.0, "end": 3.0}]
LABELS = [{"index": 0, "text": "충격", "start_sec": 0.2, "end_sec": 3.0}]
GOOD_STYLE = ('{"design": {"subtitle_size": 62}, '
              '"beats": [{"number": 0, "crop": "center", "pop": "soft"}], '
              '"labels": [{"index": 0, "x": 0.5, "y": 0.5, "color": "yellow"}], '
              '"notes": "ok"}')


def _draft_file(tmp_path: Path) -> Path:
    p = tmp_path / "draft_720.mp4"
    p.write_bytes(b"\x00" * 64)
    return p


def test_style_call_carries_high_and_the_v3_sample_fps(tmp_path, fake_upload):
    """🛑 O9 — 이 호출이 존재하는 이유가 `media_resolution=HIGH` 한 줄이다."""
    g = _FakeGemini([_response(GOOD_STYLE)])
    doc, audit = R.run_style(g, _draft_file(tmp_path), STORY_DOC,
                             preset=stage4.RECAP_PRESET, windows=WINDOWS,
                             labels=LABELS, log=lambda *a: None)
    call = g.models.calls[0]
    types = g.types
    assert call["config"].media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
    assert R.STYLE_MEDIA_RESOLUTION == "HIGH"
    # 720p 초벌과 한 세트다(기획서 §2-G) — 한쪽만 되돌리면 480p+HIGH 가 된다
    assert R.DRAFT_HEIGHT == 720
    # 표본 fps 는 v3 상수를 **import** 한 것이다(복제 금지)
    assert R.STYLE_SAMPLE_FPS == stage4.STYLE_SAMPLE_FPS == 6.0
    part = [p for p in call["contents"] if getattr(p, "file_data", None) is not None][0]
    assert part.video_metadata.fps == 6.0
    # 스타일은 연출 판단 — Flash 슬롯이다(CLAUDE.md 역할 표 · v3 와 같다)
    assert call["model"] == g.config.flash_model_name
    assert call["config"].max_output_tokens == R.STYLE_MAX_OUTPUT_TOKENS == 8192
    assert audit["media_resolution"] == "HIGH"
    assert doc["design"]["subtitle_size"] == 62


def test_style_uploads_the_draft_and_deletes_that_handle(tmp_path, fake_upload):
    """초벌 핸들의 소비자는 이 호출뿐이다 — 안 지우면 편마다 서버 파일이 쌓인다."""
    g = _FakeGemini([_response(GOOD_STYLE)])
    draft = _draft_file(tmp_path)
    R.run_style(g, draft, STORY_DOC, windows=WINDOWS, labels=LABELS,
                log=lambda *a: None)
    assert fake_upload == [draft], "올린 것은 초벌 그 파일이다"
    assert g.files.deleted == ["files/draft1"]


def test_style_prompt_and_validator_are_v3(tmp_path, fake_upload):
    """프롬프트는 v3 것 **그대로**고, 반려 판정도 v3 검증기가 한다."""
    bad = _response('{"design": {"title_glow": "#FFFFFF"}}')   # 어휘 밖 키
    g = _FakeGemini([bad, _response(GOOD_STYLE)])
    preset = stage4.get_style_preset("recap")
    band = finalize.video_band_ratio(finalize.design_from_style(preset))
    doc, audit = R.run_style(g, _draft_file(tmp_path), STORY_DOC, preset=preset,
                             windows=WINDOWS, labels=LABELS, band=band,
                             log=lambda *a: None)
    first_prompt = g.models.calls[0]["contents"][-1]
    assert first_prompt == stage4.build_style_prompt(
        preset, STORY_DOC, "", windows=WINDOWS, labels=LABELS, band=band)
    # 반려 사유는 v3 검증기의 문구다(여기서 판정을 다시 짓지 않았다는 증거)
    assert any("어휘 밖 키" in p for p in audit["attempts"][0]["problems"])
    assert "직전 제안 반려" in g.models.calls[1]["contents"][-1]
    assert audit.get("fallback") is None and audit["reasks_used"] == 1
    assert doc["schema"] == "v3_style/v1"     # 렌더 어댑터가 읽는 모양
    assert doc["diff"] == stage4.style_diff(preset, {"subtitle_size": 62})


def test_style_falls_back_to_the_preset_when_reasks_run_out(tmp_path, fake_upload):
    """소진해도 렌더는 간다(v3 규약) — 다만 조용하지 않다."""
    g = _FakeGemini([_response('{"design": {"nope": 1}}') for _ in range(9)])
    preset = stage4.get_style_preset("drama_clip")
    doc, audit = R.run_style(g, _draft_file(tmp_path), STORY_DOC, preset=preset,
                             windows=WINDOWS, labels=LABELS, log=lambda *a: None)
    assert len(g.models.calls) == 1 + stage4.MAX_REASKS
    assert audit["fallback"] is True
    assert doc["design"] == preset and doc["diff"] == {}
    assert doc["v3_style"]["labels"] == []


def test_style_stops_on_permanent_call_error(tmp_path, fake_upload):
    """같은 프롬프트를 세 번 태우지 않는다 — permanent 는 재질의로 안 낫는다."""
    class _ApiError(Exception):
        def __init__(self):
            super().__init__("400 INVALID_ARGUMENT")
            self.code = 400

    g = _FakeGemini([_ApiError(), _ApiError(), _ApiError()])
    doc, audit = R.run_style(g, _draft_file(tmp_path), STORY_DOC, windows=WINDOWS,
                             labels=LABELS, log=lambda *a: None)
    assert len(g.models.calls) == 1, "permanent 면 첫 시도에서 멈춘다"
    assert audit["fallback"] is True
    assert "영상 호출 실패" in audit["attempts"][0]["problems"][0]
    assert doc["design"] == stage4.RECAP_PRESET
    assert g.files.deleted == ["files/draft1"], "실패해도 핸들은 지운다"


# ── ③ 최종 이름 ────────────────────────────────────────────────────────────

def test_final_name_is_the_localization_contract(tmp_path):
    """🛑 `shorts.mp4` — v3 기본값을 쓰면 현지화가 최종본을 못 찾는다(기획서 §6)."""
    assert R.render_paths(tmp_path)["final"].name == RENDER_OUTPUT == "shorts.mp4"
    assert R.render_paths(tmp_path, 2)["final"].name == "shorts_2.mp4"
    assert R.render_paths(tmp_path)["style"].name == "style.json"
    assert R.render_paths(tmp_path, 2)["style"].name == "style_2.json"
    assert R.render_paths(tmp_path)["validation"].name == "validation.json"
    assert R.render_paths(tmp_path, 4)["validation"].name == "validation_4.json"


def test_variant_must_be_a_real_rank():
    """0·음수로 떨어지면 2위 편이 1위의 산출을 덮어쓴다."""
    assert R.variant_suffix(1) == "" and R.variant_suffix(2) == "_2"
    for bad in (0, -1, 1.0, True, "1"):
        with pytest.raises(ValueError):
            R.variant_suffix(bad)


def test_render_final_passes_out_name(tmp_path, monkeypatch):
    """이 함수가 하는 일은 이름 하나다 — v3 기본값이 새면 안 된다."""
    seen: dict = {}

    def _fake_render(**kw):
        seen.update(kw)
        out = Path(kw["output_dir"]) / kw["out_name"]
        out.write_bytes(b"x")
        return out, {"elapsed": 1.0, "bytes": 1}

    monkeypatch.setattr(finalize, "render_final", _fake_render)
    monkeypatch.setattr(R.proxy_mod, "probe_geometry",
                        lambda p: {"height": 1920, "fps": 30.0, "duration_sec": 10.0})
    out, cost = R.render_final(video_path=tmp_path / "src.mp4", plan={"timeline": []},
                               style_doc={}, segments=[], resources={}, story_doc={},
                               output_dir=tmp_path, variant=2, log=lambda *a: None)
    assert seen["out_name"] == "shorts_2.mp4"
    assert "final_1080x1920" not in seen["out_name"]
    assert out.name == "shorts_2.mp4" and cost["variant"] == 2
    assert cost["geometry"]["fps"] == R.FINAL_FPS == 30.0


def test_render_final_fails_loudly_when_fps_is_not_o9(tmp_path, monkeypatch):
    """`output_fps` 를 넘겼는데 결과가 30 이 아니면 **크게 실패한다**.

    종전에는 경고만 남겼다 — 렌더러가 프레임 레이트를 아예 못 받던 시절의 타협이다.
    이제 값을 싣고도 안 먹었다면 그것은 고장이지 감수할 차이가 아니다."""
    monkeypatch.setattr(
        finalize, "render_final",
        lambda **kw: ((Path(kw["output_dir"]) / kw["out_name"]).write_bytes(b"x")
                      or Path(kw["output_dir"]) / kw["out_name"], {}))
    monkeypatch.setattr(R.proxy_mod, "probe_geometry",
                        lambda p: {"height": 1920, "fps": 23.976, "duration_sec": 10.0})
    with pytest.raises(RuntimeError, match="23.976"):
        R.render_final(video_path=tmp_path / "s.mp4", plan={"timeline": []},
                       style_doc={}, segments=[], resources={}, story_doc={},
                       output_dir=tmp_path, log=lambda *_: None)


def test_render_final_passes_output_fps_down_to_the_renderer(tmp_path, monkeypatch):
    """O9 를 실제로 강제하는 통로 — v3 어댑터에 `output_fps` 가 넘어가야 한다.

    이 인자가 빠지면 fps 검사는 통과하는 편(소재가 우연히 30)에서 조용히 넘어가고,
    24fps 소재에서만 실패한다 — 통로 자체를 고정한다."""
    seen: dict = {}

    def _fake(**kw):
        seen.update(kw)
        out = Path(kw["output_dir"]) / kw["out_name"]
        out.write_bytes(b"x")
        return out, {}

    monkeypatch.setattr(finalize, "render_final", _fake)
    monkeypatch.setattr(R.proxy_mod, "probe_geometry",
                        lambda p: {"height": 1920, "fps": 30.0, "duration_sec": 10.0})
    R.render_final(video_path=tmp_path / "s.mp4", plan={"timeline": []}, style_doc={},
                   segments=[], resources={}, story_doc={}, output_dir=tmp_path,
                   log=lambda *_: None)
    assert seen["output_fps"] == R.FINAL_FPS == 30.0


# ── ④ 검증 어댑터 ──────────────────────────────────────────────────────────

def _span(sid, t_in, t_out, *, audio, text=""):
    return {"id": sid, "t_in": t_in, "t_out": t_out, "is_audio": audio,
            "time_authority": "stt" if audio else "scene", "text": text}


def _words(t0, t1, tag, prob, n=2):
    step = (t1 - t0) / n
    return [{"t0": round(t0 + i * step, 3), "t1": round(t0 + (i + 1) * step, 3),
             "text": f"{tag}{i}", "prob": prob} for i in range(n)]


def make_grid() -> dict:
    spans = [
        _span("sp0000", 0.0, 5.0, audio=False),
        _span("sp0001", 5.0, 8.0, audio=True, text="이건 정말 대단한 순간이었습니다"),
        _span("sp0002", 8.0, 12.0, audio=True, text="그래서 내가 그때 말했잖아 형"),
        _span("sp0003", 12.0, 20.0, audio=False),
        _span("sp0004", 20.0, 24.0, audio=True, text="너 진짜 이럴 거야 지금부터"),
    ]
    words = (_words(5.0, 8.0, "a", 0.9) + _words(8.0, 12.0, "b", 0.5)
             + _words(20.0, 24.0, "c", 0.7))
    return {"source": {"duration_sec": 100.0}, "scene_cuts": [10.0, 30.0],
            "silence": [], "arousal": [], "words": words, "span_candidates": spans}


CANDIDATES_DOC = {"exception_sectors": {
    "intro": {"start_sec": 0.0, "end_sec": 5.0},     # 🛑 0.0 — falsy 회귀의 그 자리
    "teaser": {"start_sec": 90.0, "end_sec": 100.0},
    "recap": None, "credit": None, "end": None}}


def test_span_index_survives_the_round_trip():
    """🛑 계약 §4 의 필수 조항 — 되싣은 문서를 v3 색인기가 같게 읽어야 한다.

    소비되는 열쇠(`check_tts_conflicts` 가 보는 t_in·t_out·is_audio·importance)와,
    되싣을 수 있는 나머지 전부를 비교한다. `meaning_content`·`mood` 는 meaning 이
    하나뿐이라 복원할 수 없고 어떤 검사도 보지 않는다(어댑터 독스트링)."""
    grid = make_grid()
    v4_index, v4_order = bridge.build_span_index(
        grid, quoted_spans={"sp0001"},
        detail={"sp0002": {"importance": 5, "audio_script": [{"speaker": "A"}],
                           "text_source": "heard", "heard_text": "들은 대사",
                           "scene_script": "장면"}})
    _s1, stage2 = R.stage_docs_for_validate(CANDIDATES_DOC, v4_index, grid)
    v3_index, v3_order = story_mod.build_span_index(stage2, grid)

    assert v3_order == v4_order, "순서(pos)가 같아야 '연속 범위' 판정이 같다"
    for sid, v3 in v3_index.items():
        v4 = v4_index[sid]
        for key in ("is_audio", "importance", "audio_script", "text_source",
                    "heard_text", "conf", "scene_script", "pos"):
            assert v3[key] == v4[key], f"{sid}.{key} 가 왕복에서 갈렸다"
        # 시각은 v3 문서 방언(HH:MM:SS.mmm)을 지나므로 ms 양자화만 허용한다
        assert v3["t_in"] == pytest.approx(v4["t_in"], abs=5e-4)
        assert v3["t_out"] == pytest.approx(v4["t_out"], abs=5e-4)
    assert v3_index["sp0001"]["importance"] == bridge.QUOTE_IMPORTANCE
    assert v3_index["sp0002"]["importance"] == 5


def test_exception_zone_times_are_strings_so_that_intro_survives():
    """🛑 `check_exception_overlap` 은 `if zone["start"] and …` 로 거른다 —
    0.0 을 숫자로 넣으면 intro 구역이 통째로 사라진다(예고 유입 벨트의 존재 이유)."""
    grid = make_grid()
    index, _ = bridge.build_span_index(grid)
    stage1, _s2 = R.stage_docs_for_validate(CANDIDATES_DOC, index, grid)
    assert stage1["exception_sector"]["intro"] == {"start": "00:00:00.000",
                                                   "end": "00:00:05.000"}
    assert stage1["exception_sector"]["teaser"]["start"] == "00:01:30.000"
    assert "recap" not in stage1["exception_sector"], "신고 없음은 싣지 않는다"

    # 그 구역이 실제로 벨트에 걸린다(문자열이 아니면 이 검사가 0 구역을 본다)
    hit = finalize.check_exception_overlap(
        [{"clip_start_sec": 2.0, "clip_end_sec": 6.0}], stage1)
    assert hit["zones"] == 2
    assert hit["violations"][0]["zone"] == "intro"
    assert hit["violations"][0]["overlap_sec"] == pytest.approx(3.0)


def test_missing_exception_sectors_is_loud():
    """빈 dict 로 넘기면 유입 검사가 '구역 0개 = 항상 통과'가 된다."""
    grid = make_grid()
    index, _ = bridge.build_span_index(grid)
    with pytest.raises(ValueError, match="exception_sectors"):
        R.stage_docs_for_validate({}, index, grid)
    with pytest.raises(ValueError, match="객체가 아니다"):
        R.stage_docs_for_validate({"exception_sectors": []}, index, grid)
    with pytest.raises(ValueError, match="뒤집혔다"):
        R.stage_docs_for_validate(
            {"exception_sectors": {"intro": {"start_sec": 5.0, "end_sec": 5.0}}},
            index, grid)


def test_span_id_outside_the_grid_is_loud():
    """v3 색인기는 격자 밖 span 을 조용히 버린다 — 그 span 이 벨트에서 빠진다."""
    grid = make_grid()
    index, _ = bridge.build_span_index(grid)
    index["sp9999"] = dict(index["sp0001"])
    with pytest.raises(ValueError, match="격자에 없는 span id"):
        R.stage_docs_for_validate(CANDIDATES_DOC, index, grid)


# ── ⑤ run_validate — hard_fail 은 그 편만 죽인다 ───────────────────────────

def _validate_inputs(*, clip: tuple[float, float], cue_at: float | None):
    grid = make_grid()
    index, _ = bridge.build_span_index(grid, detail={"sp0002": {"importance": 5}})
    plan = {"timeline": [{"role": "build", "clip_start_sec": clip[0],
                          "clip_end_sec": clip[1], "span_ids": ["sp0001", "sp0002"],
                          "use_original_audio": True}]}
    segments = [{"start_sec": 0.5, "end_sec": 2.0, "text": "대사 한 줄"}]
    resources: dict = {"tts_cue_files": []}
    if cue_at is not None:
        resources["tts_cue_files"] = [{"cue": {
            "beat": 0, "start_sec": 0.0, "end_sec": 2.0, "source_time_sec": cue_at,
            "duration_sec": 2.0, "muted_span_ids": []}}]
    return grid, index, plan, segments, resources


def test_hard_fail_is_returned_not_raised(tmp_path):
    """🛑 계약 §4 — 한 편의 벨트 위반이 나머지 승인 편의 산출을 막으면 안 된다."""
    grid, index, plan, segments, resources = _validate_inputs(
        clip=(8.0, 12.0), cue_at=9.0)          # importance 5 유성 span 위에 내레이션
    doc = R.run_validate(plan=plan, grid=grid, candidates_doc=CANDIDATES_DOC,
                         span_index=index, segments=segments, resources=resources,
                         final_path=None, tmp_dir=tmp_path / "v", log=lambda *a: None)
    assert doc["hard_fail"] is True
    assert doc["tts_conflicts"]["violations"][0]["span"] == "sp0002"
    assert doc["snap_belt"]["pct"] == 100.0     # 경계는 격자 눈금이다


def test_clean_episode_passes_and_zones_are_measured(tmp_path):
    grid, index, plan, segments, resources = _validate_inputs(
        clip=(8.0, 12.0), cue_at=None)
    doc = R.run_validate(plan=plan, grid=grid, candidates_doc=CANDIDATES_DOC,
                         span_index=index, segments=segments, resources=resources,
                         final_path=None, tmp_dir=tmp_path / "v", log=lambda *a: None)
    assert doc["hard_fail"] is False
    assert doc["exception_ingress"] == {"zones": 2, "violations": []}
    # 최종본이 없으면 프레임 계열 검사는 아예 없다(v3 규약 — 경고 모드)
    assert "frame_qc" not in doc and "loop_continuity" not in doc
    # 경고(§9-D 진행감 — 4초 클립에 이벤트가 드물다)는 **차단하지 않는다**
    assert doc["warnings_total"] >= 1 and doc["progression"]["warnings"]


def test_exception_ingress_fails_the_episode(tmp_path):
    """예고·인트로가 편집본에 들어오면 hard_fail — 가왕쇼 6화 사고의 벨트."""
    grid, index, plan, segments, resources = _validate_inputs(
        clip=(0.0, 5.0), cue_at=None)          # intro 구역 통째
    doc = R.run_validate(plan=plan, grid=grid, candidates_doc=CANDIDATES_DOC,
                         span_index=index, segments=segments, resources=resources,
                         final_path=None, tmp_dir=tmp_path / "v", log=lambda *a: None)
    assert doc["hard_fail"] is True
    assert doc["exception_ingress"]["violations"][0]["zone"] == "intro"


# ── 렌더러 `output_fps` 가산 규약 (V4-M7) ──────────────────────────────────
#
# O9(최종 30fps)를 실제로 강제하는 유일한 지점이다. 종전 argv 에는 `-r` 이 없어
# 출력 fps 가 소재를 따라갔다 — 24fps 소재는 24fps 쇼츠가 됐다. 이 절이 고정하는
# 것은 둘이다: ① 미지정이면 **종전과 바이트 동일** ② 지정하면 실제로 실린다.
# ①이 깨지면 맥미니 6대의 v1 전 채널 출력이 함께 움직인다(`auto_update=true`).

def _argv_with(monkeypatch, tmp_path, **extra):
    """render_short 를 실제 인코딩 없이 argv 까지만 몰아 본다."""
    from app.modules import renderer as R_

    captured: list[list[str]] = []
    monkeypatch.setattr(R_.subprocess, "check_call",
                        lambda cmd, **kw: captured.append(list(cmd)))
    monkeypatch.setattr(R_, "_pick_video_encoder", lambda *_a, **_k: "libx264")
    monkeypatch.setattr(R_, "find_ffmpeg_command", lambda *_a, **_k: "ffmpeg")
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\0" * 16)
    inputs = R_.RenderInputs(
        video_path=src,
        clips=[R_.StoryClip(role="build", start_sec=0.0, end_sec=2.0, subtitle="", use_original_audio=True)],
        subtitle_path=None, crop_timeline_map={}, title_text="t", work_title="w",
        output_path=tmp_path / "out.mp4",
        canvas_width=1080, canvas_height=1920,
        top_title_height=300, bottom_label_height=200, **extra)
    try:
        R_.render_short(inputs)
    except Exception:
        pass                      # argv 만 본다 — 실인코딩은 이 테스트의 관심이 아니다
    return captured[0] if captured else []


def test_renderer_argv_has_no_rate_flag_when_output_fps_is_unset(tmp_path, monkeypatch):
    """미지정 = v1 회귀 0. `-r` 이 argv 에 **한 번도** 나오면 안 된다."""
    argv = _argv_with(monkeypatch, tmp_path)
    assert argv, "argv 를 못 잡았다 — 이 테스트가 통로를 잃었다"
    assert "-r" not in argv


def test_renderer_argv_carries_the_rate_flag_when_output_fps_is_set(tmp_path, monkeypatch):
    """지정하면 출력 옵션으로 실린다 — 값은 `:g` 라 30.0 이 `30` 으로 나간다."""
    argv = _argv_with(monkeypatch, tmp_path, output_fps=30.0)
    assert argv.count("-r") == 1
    assert argv[argv.index("-r") + 1] == "30"
    # 출력단이어야 한다 — 출력 파일명보다 앞, `-map` 보다 뒤.
    assert argv.index("-map") < argv.index("-r") < len(argv) - 1


def test_renderer_rejects_a_nonpositive_output_fps(tmp_path, monkeypatch):
    """0·음수는 조용히 무시하지 않는다 — 오타가 무음 무시로 끝나면 안 된다."""
    from app.modules import renderer as R_
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\0" * 16)
    inputs = R_.RenderInputs(
        video_path=src,
        clips=[R_.StoryClip(role="build", start_sec=0.0, end_sec=2.0, subtitle="", use_original_audio=True)],
        subtitle_path=None, crop_timeline_map={}, title_text="t", work_title="w",
        output_path=tmp_path / "out.mp4",
        canvas_width=1080, canvas_height=1920,
        top_title_height=300, bottom_label_height=200, output_fps=-1.0)
    with pytest.raises(ValueError, match="output_fps"):
        R_.render_short(inputs)
