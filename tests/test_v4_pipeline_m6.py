"""V4-M6/M7 배선 회귀 가드 — `app/v4/pipeline.py` 의 11단계 다섯 조각.

계약 `docs/v4/M6-interfaces.md` §6(배선). 조각 안쪽은 각자 자기 테스트가 있고
(`test_v4_resources.py`·`test_v4_render.py`), 여기서 고정하는 것은 **이음새와 산출**이다:

① **승인 편마다** 다섯 조각이 돌고 편별 산출 이름이 규약대로인가 — 1위는 v1 이름
   그대로(`shorts.mp4`·`edit_plan.json`…), 2위↓ 는 `_{n}`. 현지화·편집실이 1위 이름을
   읽으므로(기획서 §6) 이름이 어긋나면 **아무도 안 죽고** 컷오버가 조용히 깨진다.
② **편별 증분** — 한 편이 실패해도 나머지 편의 산출은 남는다. 전량 실패는 크게 죽는다
   (조용한 결번이 이 레포에서 가장 나쁜 실패다).
③ **지문** — 특히 style 지문이 **편집실이 고치는 필드에 묶이지 않는가**(계약 §6 의 🛑 ·
   E15: "편집실 라운드에서 스타일을 재호출하지 않는다"). 반대로 컷·라벨·내레이션이
   바뀌면 하류 캐시가 폐기되는가(v3 적대 리뷰 C2: 옛 라벨이 든 final 납품).
④ 재개가 캐시를 쓰는가 · `--stop-after 11:draft` 로 조각 단위 정지가 되는가.
⑤ 계약 대조 도구(`scripts/v4_contract_diff.py`)와 AST 가드가 이 판을 통과하는가.

🛑 **네트워크 0.** 가짜 gemini 가 진짜 `call_video`·`_call_text` 를 통과하고(`types` 는
진짜 SDK), 업로드·TTS 합성은 가짜다.

🛑 **최종 렌더만은 실물이 아니다.** 이 머신의 ffmpeg 는 8.x 이고 **libass·drawtext 가
없다**(`ffmpeg -filters` 실측: `ass`·`subtitles`·`drawtext` 전무 · `-filter_complex_script`
도 8.x 가 거절한다). `finalize.render_final` 의 필터그래프는 그 셋을 전부 쓰므로 여기서는
돌 수 없다 — 그래서 그 함수만 **진짜 ffmpeg 로 1080×1920/30fps 를 굽는 대역**으로
바꿔 끼운다(계획 타임라인을 실제로 읽어 굽는다). 그 결과 이 파일이 실물로 확인하는 것은
**초벌(720p/30fps, 진짜 v4 렌더)·산출 이름·편별 격리·캐시**이고, v3 필터그래프 자체는
운영 노드(ffmpeg 7 + libass)의 몫이다(`docs/v4/UNVERIFIED.md`).

⚠ 픽스처는 M5 배선 테스트에서 **가져다 쓴다**(재료를 두 벌 적으면 언젠가 갈린다) —
6~10단계를 실제로 통과시켜야 11 에 승인 편이 온다.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.v3 import finalize as finalize_mod
from app.v3 import seq_analyze, stage4
from app.v4 import pipeline as v4p
from app.v4 import render as render_mod
from app.v4 import resources as resources_mod

# M5 배선 테스트의 재료·가짜를 그대로 쓴다(픽스처는 import 만으로 이 모듈의 것이 된다).
from test_v4_pipeline_m5 import (  # noqa: F401 — fixture 재사용
    FakeGemini,
    default_router,
    fake_proxy,
    fake_transcribe,
    no_boundary_calls,
    synth,
)
import test_v4_pipeline_m5 as m5


# ── 가짜 응답 ───────────────────────────────────────────────────────────────
# 스타일 응답은 v3 검증기(`stage4.validate_style_response`)를 **실제로** 통과해야 한다 —
# 통과 못 하면 배선이 프리셋 폴백으로 떨어져, 이 파일이 재는 것이 배선이 아니라 폴백이 된다.
STYLE_JSON = json.dumps({
    "design": {"subtitle_size": 62},
    "beats": [{"number": 0, "crop": "center", "pop": "soft"}],
    "labels": [{"index": 0, "x": 0.5, "y": 0.5, "color": "yellow"}],
    "notes": "합성 소재 — 미세 조정만",
}, ensure_ascii=False)

QC_JSON = '{"issues": []}'


# ⚠ **원본을 import 시점에 붙든다.** 아래에서 `m5._kind` 를 이 함수로 갈아끼우므로
# 그때 이름으로 다시 찾으면 자기 자신을 부른다(RecursionError — 실제로 났다).
_M5_KIND = m5._kind


def _kind_m6(prompt: str) -> str:
    """M5 분류기 + 11단계가 새로 내는 두 호출(스타일·프레임 QC)."""
    if prompt.startswith("당신은 쇼츠 아트디렉터다"):
        return "style"
    if prompt.startswith("첨부한 프레임들은 세로 쇼츠"):
        return "frame_qc"
    return _M5_KIND(prompt)


def _router(base=None, *, style=None):
    base = base or default_router()

    def route(kind, prompt):
        if kind == "style":
            return style(prompt) if style else STYLE_JSON
        if kind == "frame_qc":
            return QC_JSON
        return base(kind, prompt)
    return route


@pytest.fixture
def gem(monkeypatch):
    """가짜 gemini + 11단계 호출 분류. 반환 dict 로 라우터를 갈아끼울 수 있다."""
    monkeypatch.setattr(m5, "_kind", _kind_m6)
    holder: dict = {"router": _router()}
    client = FakeGemini(lambda *a: holder["router"](*a))
    # `release_handle` 이 부르는 자리 — M5 가짜는 files 가 비어 있어 WARN 으로 새는데,
    # 초벌 핸들이 실제로 삭제되는지가 이 단계의 계약이라(편마다 서버 파일이 쌓인다)
    # 지운 이름을 받아 둔다.
    deleted: list[str] = []
    client.client.files.delete = lambda name: deleted.append(name)
    holder["client"] = client
    holder["deleted"] = deleted
    monkeypatch.setattr(v4p, "_load_gemini_client", lambda: client)
    return holder


@pytest.fixture
def fake_upload(monkeypatch):
    """초벌 업로드(Files API)만 가짜 — `run_style` 이 올리는 그 파일을 받아 둔다."""
    uploaded: list[Path] = []

    def _fake(gemini, video_path: Path, *, log=print):
        uploaded.append(Path(video_path))
        return SimpleNamespace(uri=f"files/draft{len(uploaded)}",
                               name=f"files/draft{len(uploaded)}", size_bytes=123)

    monkeypatch.setattr(seq_analyze, "_upload_video", _fake)
    return uploaded


@pytest.fixture
def fake_tts(monkeypatch):
    """cue 합성 가짜 — 실호출(edge-tts·ElevenLabs)은 이 파일의 범위 밖이다."""
    made: list[str] = []

    def fake(text, output_path, target_sec, *, voice, speed, shorten_fn=None, **kw):
        Path(output_path).write_bytes(b"ID3-fake-mp3")
        made.append(Path(output_path).name)
        # 창 안에 들어온 것으로 답한다 — 물리 트림(ffmpeg)은 resources 테스트의 몫이다.
        return text, min(float(target_sec), 1.5)

    monkeypatch.setattr("app.modules.tts.synthesize_tts_with_fit", fake)
    return made


@pytest.fixture
def fake_final(monkeypatch):
    """`finalize.render_final` 대역 — **진짜 ffmpeg 로** 1080×1920/30fps 를 굽는다.

    모듈 독스트링의 🛑: 이 머신의 ffmpeg 8 에는 libass·drawtext 가 없어 v3 필터그래프가
    돌지 않는다. 그래도 파일이 실물이어야 산출 이름·기하를 ffprobe 로 잴 수 있으므로,
    **계획 타임라인을 실제로 읽어** 클립을 이어 굽는다(입력 seek). 자막·제목·라벨은
    그리지 않는다 — 그것을 재는 것은 운영 노드의 몫이다.
    """
    calls: list[dict] = []

    def fake(*, video_path, plan, style_doc, segments, resources, story_doc,
             output_dir, out_name="final_1080x1920.mp4", output_fps=None,
             span_times=None, log=print):
        # ⚠ `output_fps` 를 받아야 한다 — v4 가 O9(30fps)를 이 인자로 강제한다.
        # 대역이 이것을 안 받으면 실패가 "렌더 실패"로 뭉뚱그려져 원인이 안 보인다.
        calls.append({"out_name": out_name, "plan": plan, "style_doc": style_doc,
                      "output_fps": output_fps, "span_times": span_times,
                      "segments": segments, "resources": resources,
                      "story_doc": story_doc})
        out = Path(output_dir) / out_name
        timeline = plan["timeline"]
        cmd = [find_ffmpeg_command("ffmpeg"), "-y"]
        filters, parts = [], []
        for i, c in enumerate(timeline):
            s0, e0 = float(c["clip_start_sec"]), float(c["clip_end_sec"])
            cmd += ["-ss", f"{s0:.3f}", "-t", f"{e0 - s0:.3f}", "-i", str(video_path)]
            filters.append(
                f"[{i}:v]setpts=PTS-STARTPTS,scale=1080:-2,"
                f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1[v{i}]")
            filters.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
            parts.append(f"[v{i}][a{i}]")
        filters.append("".join(parts) + f"concat=n={len(timeline)}:v=1:a=1[vout][aout]")
        cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                "-fps_mode", "cfr", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "34", "-c:a", "aac", "-ac", "1", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        return out, {"elapsed": 0.0, "bytes": out.stat().st_size,
                     "clips": len(timeline),
                     "cues": len(resources.get("tts_cue_files") or []),
                     "muted_windows": 0, "labels": 0}

    monkeypatch.setattr(finalize_mod, "render_final", fake)
    return calls


@pytest.fixture(autouse=True)
def _allow_ffmpeg8(monkeypatch):
    """이 머신은 ffmpeg 8.x 뿐이다(운영은 7.x) — 다른 렌더 테스트와 같은 관문."""
    monkeypatch.setenv("AI_VIDEO_ALLOW_UNSUPPORTED_FFMPEG", "1")


# ── 읽기 도우미 ─────────────────────────────────────────────────────────────

def _run_log(out: Path) -> dict:
    return json.loads((out / "run_log.json").read_text(encoding="utf-8"))


def _entry(out: Path, name: str) -> dict:
    got = [s for s in _run_log(out)["steps"] if s["step"] == name]
    assert got, f"{name} 기록이 없다: {[s['step'] for s in _run_log(out)['steps']]}"
    return got[-1]


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [find_ffmpeg_command("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate", "-of", "json",
         str(path)], capture_output=True, text=True, check=True)
    st = json.loads(out.stdout)["streams"][0]
    num, den = (float(x) for x in st["avg_frame_rate"].split("/"))
    return {"width": int(st["width"]), "height": int(st["height"]),
            "fps": num / den if den else 0.0}


def _run(synth, outdir, **kw):
    kw.setdefault("skip_research", True)
    kw.setdefault("max_shorts", 2)
    kw.setdefault("log", lambda *a: None)
    return v4p.run_v4(video_path=synth, work_title="합성", outdir=outdir, **kw)


@pytest.fixture
def wired(fake_transcribe, fake_proxy, gem, no_boundary_calls, fake_upload, fake_tts,
          fake_final):
    """11단계까지 실제로 도는 한 벌(가짜는 LLM·업로드·TTS·최종 렌더 넷뿐)."""
    return SimpleNamespace(gem=gem, uploaded=fake_upload, tts=fake_tts,
                           finals=fake_final)


# ═══════════════════════════════════════════════════════════════════════════
# ① 승인 2편이 끝까지 간다 — 산출 이름과 기하
# ═══════════════════════════════════════════════════════════════════════════

def test_two_approved_episodes_render_to_shorts_and_shorts_2(synth, tmp_path, wired):
    out = _run(synth, tmp_path / "o")

    # 1위는 **v1 이름 그대로**(현지화 `RENDER_OUTPUT`·편집실이 그 이름을 읽는다),
    # 2위↓ 만 `_{n}`. 이름이 어긋나면 아무도 안 죽고 컷오버가 조용히 깨진다.
    names = {p.name for p in out.iterdir()}
    assert {"edit_plan.json", "edit_plan_2.json",
            "subtitle_segments.json", "subtitle_segments_2.json",
            "checkpoint_resources.json", "checkpoint_resources_2.json",
            "draft_720.mp4", "draft_720_2.mp4",
            "style.json", "style_2.json",
            "shorts.mp4", "shorts_2.mp4",
            "validation.json", "validation_2.json"} <= names, sorted(names)
    # v3 기본 이름으로 새어 나가면 현지화가 최종본을 못 찾는다
    assert "final_1080x1920.mp4" not in names

    for name in ("shorts.mp4", "shorts_2.mp4"):
        geo = _probe(out / name)
        assert (geo["width"], geo["height"]) == (1080, 1920), (name, geo)
        assert abs(geo["fps"] - render_mod.FINAL_FPS) < 0.01, (name, geo)

    # 초벌은 **진짜 v4 렌더**다(O9: 720p/30fps · 입력 seek)
    for name in ("draft_720.mp4", "draft_720_2.mp4"):
        geo = _probe(out / name)
        assert geo["height"] == render_mod.DRAFT_HEIGHT
        assert abs(geo["fps"] - render_mod.DRAFT_FPS) < 0.01

    # cue 오디오도 편마다 이름이 갈린다(2위 실패가 1위 오디오를 지우면 안 된다)
    assert any(n.startswith("tts_cue_") for n in names), sorted(names)
    assert any(n.startswith("tts_2_cue_") for n in names), sorted(names)

    # 순서 = 승인 순위 = 파일 번호
    story = json.loads((out / "checkpoint_story.json").read_text(encoding="utf-8"))
    rows = _entry(out, "11:render")["episodes"]
    assert [r["id"] for r in rows] == [v["candidate_id"] for v in story["variants"]]
    assert [r["final"] for r in rows] == ["shorts.mp4", "shorts_2.mp4"]
    assert _entry(out, "11:validate")["failed"] == 0


def test_every_eleven_step_is_recorded_once_with_per_episode_rows(synth, tmp_path,
                                                                  wired):
    """단계당 기록 한 줄 + 그 안에 편별 내역(같은 단계가 여러 줄이면 감사가 거짓말한다)."""
    out = _run(synth, tmp_path / "o")
    steps = _run_log(out)["steps"]
    for name in ("11:resources", "11:draft", "11:style", "11:render", "11:validate"):
        rows = [s for s in steps if s["step"] == name]
        assert len(rows) == 1, f"{name} 기록이 {len(rows)}줄이다"
        entry = rows[0]
        assert entry["of"] == 2 and entry["ok"] == 2 and entry["failed"] == 0
        assert [e["variant"] for e in entry["episodes"]] == [1, 2]
    # 초벌 핸들은 편마다 지운다 — 안 지우면 서버 파일이 쌓여 할당량을 먹는다
    assert len(wired.uploaded) == 2
    assert wired.gem["deleted"] == ["files/draft1", "files/draft2"]


def test_style_gets_the_same_preset_as_the_band_and_the_render_labels(synth, tmp_path,
                                                                      wired,
                                                                      monkeypatch):
    """프리셋·라벨 계획은 렌더가 쓰는 그것과 **같은 것**이어야 한다(v3 `_run_m4` 규율)."""
    seen: list[dict] = []
    real = render_mod.run_style

    def spy(gemini, draft_path, story_doc, **kw):
        seen.append({"draft": Path(draft_path), **kw})
        return real(gemini, draft_path, story_doc, **kw)

    monkeypatch.setattr(render_mod, "run_style", spy)
    out = _run(synth, tmp_path / "o")

    assert [s["draft"].name for s in seen] == ["draft_720.mp4", "draft_720_2.mp4"]
    assert all(s["preset"] == stage4.get_style_preset(None) for s in seen)
    plan = json.loads((out / "edit_plan.json").read_text(encoding="utf-8"))
    story = json.loads((out / "checkpoint_story.json").read_text(encoding="utf-8"))
    # ⚠ **격자를 같이 넘겨서** 만든다. 라벨은 앵커 span 의 실제 시각에 떠야 하고
    # (2026-09-04 교정), 스타일 단계와 렌더 단계가 **같은 인자**로 부르지 않으면
    # 두 목록의 index 가 어긋나 Stage 4 가 정한 좌표가 남의 라벨에 붙는다.
    grid = json.loads((out / "grid.json").read_text(encoding="utf-8"))
    span_times = finalize_mod.span_start_times(grid)
    assert span_times, "격자에 span 시각이 없다 — 이 검사가 무의미해진다"
    expect = finalize_mod.plan_labels(v4p.episode_story_doc(story["variants"][0]),
                                      plan, span_times)
    assert seen[0]["labels"] == expect
    # 접기 폴백이 아니라 진짜 span 시각을 썼는지 — 폴백이면 라벨이 클립 시작에 몰린다.
    starts = [lb["start_sec"] for lb in expect]
    assert len(set(starts)) == len(starts), f"라벨이 같은 시각에 몰렸다: {starts}"
    # 확정 스타일 문서는 **감싸지 않고** 그대로 쓴다(현지화 E16·계약 도구가 뿌리를 읽는다)
    doc = json.loads((out / "style.json").read_text(encoding="utf-8"))
    assert doc["schema"] == "v3_style/v1" and "design" in doc and "v3_style" in doc
    assert doc["design"]["subtitle_size"] == 62      # 모델이 낸 미세 조정이 실렸다


def test_contract_diff_tool_passes_the_finished_job(synth, tmp_path, wired):
    """계약 대조 도구(`scripts/v4_contract_diff.py`)로 네 산출을 본다.

    ⚠ **선행 위반 1건은 그대로다**(M5 보고서 contract_issues · M6 범위 밖):
    도구는 `checkpoint_candidates.json` **뿌리**의 `approved` 를 요구하는데 배선은
    그것을 `approval.approved` 절 안에 쓴다. 고치려면 절 소유표(M4)를 바꿔야 한다 —
    그래서 이 테스트는 **위반이 늘지 않았는가**를 본다."""
    from scripts.v4_contract_diff import check_job, load_job

    out = _run(synth, tmp_path / "o")
    docs, present, unreadable = load_job(out)
    result = check_job(docs, present=present, unreadable=unreadable, job=str(out))
    bad = {f["file"]: [v["where"] for v in f["violations"]]
           for f in result["files"] if f["status"] == "violation"}
    # 위반 0 이 합격선이다. 종전에는 여기 `checkpoint_candidates.json: [approved]`
    # 를 **기대값으로 박아 뒀는데**, 그것은 계약 표가 키 자리를 틀리게 적은 것이지
    # 잡의 결함이 아니었다(실잡 대조로 드러났다). 아는 위반을 기대값에 박으면
    # 도구가 그 자리에서 영구히 눈을 감는다.
    assert bad == {}, bad
    assert not [f for f in result["files"] if f["status"] == "unreadable"]
    # 2위 편의 형제 파일도 같은 계약으로 본다(도구가 숫자 접미를 함께 읽는다)
    checked = {f["file"] for f in result["files"] if f["status"] == "ok"}
    assert {"edit_plan.json", "edit_plan_2.json", "style.json",
            "style_2.json", "subtitle_segments.json",
            "checkpoint_resources.json"} <= checked, sorted(checked)


# ═══════════════════════════════════════════════════════════════════════════
# ② 편별 격리 — 한 편이 죽어도 나머지는 나간다 / 전량 실패는 크게
# ═══════════════════════════════════════════════════════════════════════════

def test_one_episode_failure_does_not_take_the_others(synth, tmp_path, wired,
                                                      monkeypatch):
    real = render_mod.run_style
    calls: list[int] = []

    def flaky(gemini, draft_path, story_doc, **kw):
        calls.append(1)
        if len(calls) == 2:                     # 2위 편만 죽인다
            raise RuntimeError("가짜 스타일 실패")
        return real(gemini, draft_path, story_doc, **kw)

    monkeypatch.setattr(render_mod, "run_style", flaky)
    out = _run(synth, tmp_path / "o")

    # 1위는 끝까지 갔고 2위는 style 에서 멈췄다
    assert (out / "shorts.mp4").exists()
    assert not (out / "shorts_2.mp4").exists()
    # 그래도 2위의 **앞 단계 산출은 남는다**(재개가 그 위에서 다시 시작한다)
    assert (out / "edit_plan_2.json").exists() and (out / "draft_720_2.mp4").exists()

    style_rows = _entry(out, "11:style")["episodes"]
    assert style_rows[0].get("error") is None
    assert "가짜 스타일 실패" in style_rows[1]["error"]
    for name in ("11:render", "11:validate"):
        entry = _entry(out, name)
        assert entry["ok"] == 1 and entry["failed"] == 1
        assert entry["episodes"][1]["skipped"] == "앞 단계 실패(11:style)"


def test_all_episodes_failing_is_loud(synth, tmp_path, wired, monkeypatch):
    """🛑 조용한 결번이 이 레포에서 가장 나쁜 실패다(9단계 `no_publishable` 과 같은 규율)."""
    def dead(*a, **kw):
        raise RuntimeError("가짜 스타일 전량 실패")

    monkeypatch.setattr(render_mod, "run_style", dead)
    with pytest.raises(RuntimeError, match="전부 실패했다"):
        _run(synth, tmp_path / "o")


def test_hard_fail_marks_only_that_episode(synth, tmp_path, wired, monkeypatch):
    """계약 §4 — hard_fail 은 예외가 아니다. 그 편만 실패로 적고 다음 편으로 간다."""
    real = finalize_mod.run_validate
    seen: list[int] = []

    def flip(**kw):
        doc = real(**kw)
        seen.append(1)
        if len(seen) == 1:                      # 1위 편만 hard_fail 로 만든다
            doc["hard_fail"] = True
        return doc

    monkeypatch.setattr(finalize_mod, "run_validate", flip)
    out = _run(synth, tmp_path / "o")

    rows = _entry(out, "11:validate")["episodes"]
    assert rows[0]["hard_fail"] is True and rows[1]["hard_fail"] is False
    assert _entry(out, "11:validate")["failed"] == 1
    # 판정 문서는 **양쪽 다** 남는다 — 실패한 편도 사람이 사유를 읽어야 한다
    for name in ("validation.json", "validation_2.json"):
        doc = json.loads((out / name).read_text(encoding="utf-8"))
        assert "snap_belt" in doc and "exception_ingress" in doc
    # 최종본은 두 편 다 이미 구워졌다(hard_fail 은 발행 판정이지 렌더 실패가 아니다)
    assert (out / "shorts.mp4").exists() and (out / "shorts_2.mp4").exists()


# ═══════════════════════════════════════════════════════════════════════════
# ③ 지문 — 편집실 필드에 묶이지 않는다 / 바뀌면 폐기된다
# ═══════════════════════════════════════════════════════════════════════════

TIMELINE = [{"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 20.0,
             "subtitle": "(팩폭)", "use_original_audio": True,
             "span_ids": ["sp0001", "sp0002"]},
            {"role": "climax", "clip_start_sec": 30.0, "clip_end_sec": 41.0,
             "subtitle": "", "use_original_audio": False, "span_ids": ["sp0009"]}]
LABEL_PLAN = [{"index": 0, "text": "(팩폭)", "start_sec": 0.5, "end_sec": 9.0}]


_MISSING = object()


def _style_fp(timeline=_MISSING, labels=_MISSING, preset=_MISSING):
    # ⚠ `or` 로 기본값을 주면 **빈 목록이 기본값으로 되살아난다** — 라벨 0개를 재는
    #   단언이 조용히 통과했다(실제로 났다). 센티널로 '안 준 것'과 '비운 것'을 가른다.
    return v4p.style_fingerprint(
        TIMELINE if timeline is _MISSING else timeline,
        LABEL_PLAN if labels is _MISSING else labels,
        stage4.RECAP_PRESET if preset is _MISSING else preset,
        model="gemini-3.7-flash")


def test_style_fingerprint_ignores_the_fields_the_editor_edits():
    """🛑 E15 — 편집실 라운드에서 스타일을 재호출하면 승인된 화면이 라운드마다 달라진다.

    편집실이 고치는 것은 `clips`·`title`·`subtitles`·`design` 넷이다
    (`app/v3/overrides.py:HANDLED_KEYS`). 그중 **컷(clips)만** 화면을 바꾸므로 지문
    재료이고, 나머지 셋은 지문에 닿지 않아야 한다."""
    base = _style_fp()

    # 제목(layout.top_title)은 계획의 다른 자리라 애초에 재료가 아니다 — 타임라인의
    # `subtitle`(라벨 문구 사본)·`role` 도 지문을 움직이지 않는다.
    edited = [dict(c, subtitle="사람이 고친 문구", role="build") for c in TIMELINE]
    assert _style_fp(timeline=edited) == base

    # 자막 문구·design 은 지문 인자에 아예 없다 — 재료 표가 그것을 못 받는다
    assert v4p.style_fingerprint.__doc__ and "편집실" in v4p.style_fingerprint.__doc__

    # 반대로 컷이 바뀌면 화면이 달라지므로 다시 물어야 한다
    moved = [dict(TIMELINE[0], clip_end_sec=21.0), TIMELINE[1]]
    assert _style_fp(timeline=moved) != base
    # 라벨 문구·개수도 재료다(v3 적대 리뷰 C2: 옛 라벨이 든 final 이 납품됐다)
    assert _style_fp(labels=[{**LABEL_PLAN[0], "text": "(정적)"}]) != base
    assert _style_fp(labels=[]) != base
    # 프리셋이 바뀌면 밴드·기본값이 달라진다
    assert _style_fp(preset=stage4.get_style_preset("drama_clip")) != base


def test_render_fingerprint_moves_when_the_sound_or_the_style_moves():
    """최종본은 자막·cue 도 굽는다 — style 지문만 보면 옛 소리가 든 판이 그대로 나간다."""
    doc = {"design": {"subtitle_size": 62}, "v3_style": {"labels": []}}
    base = v4p.render_fingerprint("style-fp", doc, "res-fp")
    assert v4p.render_fingerprint("style-fp2", doc, "res-fp") != base
    assert v4p.render_fingerprint("style-fp", {**doc, "design": {}}, "res-fp") != base
    # 내레이션 문구만 고친 편 — 컷도 라벨도 그대로라 style 지문은 안 움직인다
    assert v4p.render_fingerprint("style-fp", doc, "res-fp2") != base


def test_resources_fingerprint_watches_the_name_dictionary_and_the_grid():
    story = {"title": {"line1": "가", "line2": "나"}, "beats": []}
    base = v4p.resources_fingerprint(story, [["sp1"]], ["강비호"], "grid-1")
    assert v4p.resources_fingerprint(story, [["sp1"]], ["강비호", "홍재인"],
                                     "grid-1") != base
    # 같은 span **id** 라도 격자가 다시 만들어졌으면 그 id 의 시각이 다르다
    assert v4p.resources_fingerprint(story, [["sp1"]], ["강비호"], "grid-2") != base


def test_draft_fingerprint_is_the_cut_and_the_o9_geometry():
    base = v4p.draft_fingerprint(TIMELINE)
    assert v4p.draft_fingerprint([dict(c, subtitle="다른 문구") for c in TIMELINE]) == base
    assert v4p.draft_fingerprint([dict(TIMELINE[0], use_original_audio=False),
                                  TIMELINE[1]]) != base
    # 기하가 곧 그 파일이 무엇인가다 — O9 를 되돌리면 스타일이 다른 화면을 본다
    assert v4p.timeline_signature(TIMELINE)[0][:2] == [10.0, 20.0]


# ═══════════════════════════════════════════════════════════════════════════
# ④ 재개 — 캐시를 쓴다 / 조각 단위로 멈춘다
# ═══════════════════════════════════════════════════════════════════════════

def test_resume_reuses_every_eleven_cache(synth, tmp_path, wired):
    out = _run(synth, tmp_path / "o")
    style_calls = wired.gem["client"].count("style")
    draft_mtime = (out / "draft_720.mp4").stat().st_mtime_ns
    cues = list(wired.tts)

    again = _run(synth, tmp_path / "o", job_id=out.name)
    assert again == out
    # 🛑 E15 — 재개는 스타일을 **다시 부르지 않는다**(승인된 화면이 달라지면 안 된다)
    assert wired.gem["client"].count("style") == style_calls
    # 요금 드는 것들도 그대로다
    assert wired.tts == cues, "재개가 TTS 를 다시 합성했다"
    assert (out / "draft_720.mp4").stat().st_mtime_ns == draft_mtime
    for name in ("11:resources", "11:draft", "11:style", "11:render"):
        rows = _entry(out, name)["episodes"]
        assert all(r["cached"] for r in rows), (name, rows)
    # 검증만은 **매번 다시 잰다**(산출이 아니라 판정이다 — v3 규약)
    assert all(r["cached"] is False for r in _entry(out, "11:validate")["episodes"])


def test_from_step_eleven_style_recomputes_only_downstream(synth, tmp_path, wired):
    out = _run(synth, tmp_path / "o")
    style_calls = wired.gem["client"].count("style")
    cues = list(wired.tts)

    _run(synth, tmp_path / "o", job_id=out.name, from_step="11:style")
    # 상류(재료·초벌)는 캐시, 자기와 하류는 다시
    assert all(r["cached"] for r in _entry(out, "11:resources")["episodes"])
    assert all(r["cached"] for r in _entry(out, "11:draft")["episodes"])
    assert wired.gem["client"].count("style") == style_calls + 2
    assert wired.tts == cues
    assert all(r["cached"] is False for r in _entry(out, "11:render")["episodes"])


def test_from_step_eleven_resources_rebuilds_the_bridge_after_a_cached_flesh(
        synth, tmp_path, wired):
    """10단계를 캐시로 건너뛴 재개에서도 span 색인(다리)이 다시 만들어지는가.

    11 은 클립 묶기·자막·검증에 그 색인이 필요한데, 그것을 만든 10단계는 캐시로
    지나간다 — 여기서 다시 만들지 않으면 재개가 `KeyError` 로 죽는다."""
    out = _run(synth, tmp_path / "o")
    cues = list(wired.tts)
    style_calls = wired.gem["client"].count("style")

    _run(synth, tmp_path / "o", job_id=out.name, from_step="11:resources")
    assert _entry(out, "flesh")["cached"] is True
    assert all(r["cached"] is False for r in _entry(out, "11:resources")["episodes"])
    assert len(wired.tts) == len(cues) * 2, "재료를 다시 만들었는데 TTS 가 안 돌았다"
    # 하류도 전부 다시 — 초벌 지문은 그대로여도 순번이 무효화한다
    assert wired.gem["client"].count("style") == style_calls + 2
    assert (out / "shorts.mp4").exists() and (out / "shorts_2.mp4").exists()


def test_stop_after_eleven_draft(synth, tmp_path, wired):
    out = _run(synth, tmp_path / "o", stop_after="11:draft")
    assert (out / "draft_720.mp4").exists() and (out / "draft_720_2.mp4").exists()
    assert not (out / "style.json").exists()
    assert not (out / "shorts.mp4").exists()
    entry = _entry(out, "11:style")
    assert entry["skipped"] == "--stop-after 11:draft"
    assert entry["remaining"] == ["11:style", "11:render", "11:validate"]
    assert wired.gem["client"].count("style") == 0


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ 순수 변환기 · 단계 표 · AST 가드
# ═══════════════════════════════════════════════════════════════════════════

def test_episode_story_doc_drops_the_v1_mirror_keys():
    variant = {"clips": [{"role": "hook"}], "title_text": "가\n나", "score": 1.0,
               "tts_cues": [], "candidate_id": "c01", "template": "recap_dialogue",
               "reason": "왜", "title": {"line1": "가", "line2": "나"},
               "beats": [], "narration_cues": [], "segments": [], "span_ids": [[]],
               "budget": {}, "narration_dropped": []}
    doc = v4p.episode_story_doc(variant)
    assert set(doc) & {"clips", "title_text", "score", "tts_cues"} == set()
    assert doc["title"] == {"line1": "가", "line2": "나"}
    assert doc["candidate_id"] == "c01"
    # 순수 — 넘겨받은 dict 를 고치지 않는다
    assert "clips" in variant


def test_episode_story_doc_is_loud_about_a_foreign_document():
    """v1·v3 가 남긴 같은 이름의 파일을 집어 들면 그 자리에서 죽어야 한다."""
    with pytest.raises(RuntimeError, match="10단계"):
        v4p.episode_story_doc({"clips": [], "title_text": "가"})


def test_step_tables_are_complete():
    assert v4p.NOT_IMPLEMENTED_MILESTONE == {}
    assert {"11:resources", "11:draft", "11:style", "11:render",
            "11:validate"} <= v4p.IMPLEMENTED_STEPS
    # 11 은 `checkpoint_candidates.json` 의 절을 쓰지 않는다(자기 파일들을 쓴다)
    assert not ({"11:resources", "11:draft", "11:style", "11:render", "11:validate"}
                & set(v4p.STEP_SECTIONS))
    # 편별 산출 경로의 정본은 각 모듈이다 — 배선이 이름을 다시 적지 않는다
    src = Path(v4p.__file__).read_text(encoding="utf-8")
    for literal in ("shorts.mp4", "draft_720", "subtitle_segments_"):
        assert f'"{literal}' not in src, f"배선이 산출 이름 {literal!r} 를 다시 적었다"


def test_ast_guards_still_pass_on_the_m6_wiring():
    from test_v4_guards import from_step_comparisons, imported_modules

    src = Path(v4p.__file__).read_text(encoding="utf-8")
    assert from_step_comparisons(src) == [], "손으로 적은 from_step 판정이 생겼다"
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert code.count("should_run(") == 1, "should_run 호출이 흩어졌다"
    dotted = {m for m, _ in imported_modules(src, package="app.v4")}
    assert not {m for m in dotted
                if m == "app.pipeline" or m.startswith("app.pipeline.")
                or m == "app.v3.pipeline" or m.startswith("app.v3.pipeline.")}
    # 11 은 v3 를 **라이브러리로** 부른다(배선은 v4 것이다 — 계약 §0)
    assert {"app.v3.finalize", "app.v3.stage4"} <= dotted


def test_resource_and_render_paths_agree_on_the_variant_suffix():
    """두 모듈이 편 번호를 다르게 붙이면 1위·2위 산출이 섞인다."""
    for n, sfx in ((1, ""), (2, "_2"), (3, "_3")):
        res = resources_mod.resource_paths(Path("/x"), n)
        ren = render_mod.render_paths(Path("/x"), n)
        assert res["edit_plan"].name == f"edit_plan{sfx}.json"
        assert ren["final"].name == f"shorts{sfx}.mp4"
        assert ren["draft"].name == f"draft_720{sfx}.mp4"
