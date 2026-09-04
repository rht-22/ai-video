"""V4-M5 배선 회귀 가드 — `app/v4/pipeline.py` 의 10·10a(살붙이기·정밀 청취).

계약 `docs/v4/M5-interfaces.md` §4(배선). 단계 안쪽 로직은 각자 자기 테스트가 있고
(`test_v4_bridge.py`·`test_v4_flesh.py`·`test_v4_detail.py`), 여기서 고정하는 것은
**이음새와 산출 파일**이다:

① `checkpoint_story.json` 이 **v1 모양**인가 — 특히 `variants[*].title_text`.
   🛑 v3 는 같은 이름의 파일에 다른 모양(`{fingerprint, story}`)을 썼고, 그래서
   `app/localize/apply.py:182~` 의 제목 갈아끼우기가 **조용히 아무 일도 안 했다**
   (JP 판에 한국어 제목이 그대로 번인). 파일이 있으니 아무도 안 죽는 종류의 사고다.
② 10a 가 10 **앞에서** 도는가 — 단계 표는 flesh(10) → detail(10a) 순인데 자료는 반대로
   흐른다(10a 산출을 10 이 먹는다). 배선이 그 어긋남을 감당하는지 순서로 본다.
③ 10a 가 꺼져 화자를 못 얻은 사실이 **소리를 내는가** — 조용하면 자막이 전 줄 흰색으로
   나가고 아무도 모른다(계약 §0 의 발견).
④ 한 편의 실패가 그 편에서 끝나는가 · 전량 실패는 크게 죽는가(조용한 결번 금지).

⚠ **네트워크 0.** `GEMINI_API_KEY` 가 없는 워크트리라 실호출 검증은 불가하고, 가짜
클라이언트가 진짜 `call_video`·`_call_text` 를 통과한다(`gemini.types` 는 진짜
google-genai). 실호출로만 아는 것(모델이 규칙을 지키는가·화자 품질·실 과금)은 범위 밖이다.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import types as pytypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.v4 import boundary as boundary_mod
from app.v4 import detail as detail_mod
from app.v4 import flesh as flesh_mod
from app.v4 import pipeline as v4p

MODEL = "gemini-3.7-flash"
SRC_SEC = 200.0

# 재료는 M3 배선 테스트와 같은 모양이다 — 6~9 를 실제로 통과시켜야 10 에 승인 편이
# 오기 때문이다(여기서 다시 고르는 것은 10·10a 뿐이다).
WORDS = [{"t0": t, "t1": t + 2.0, "prob": 0.9,
          "text": f"{int(t)}초 지점에서 벌어진 결정적인 장면의 대사"}
         for t in [float(x) for x in range(6, 196, 3)]]


def _quote_at(t: float) -> str:
    return next(w["text"] for w in WORDS if w["t0"] >= t)


def _cand(cid: str, template: str, spans: list[tuple[float, float]]) -> dict:
    return {"id": cid, "template": template,
            "reason": f"{cid} 선택 사유 한 문장",
            "title_draft": {"line1": "윗줄 가안", "line2": "아랫줄 가안"},
            "segments": [{"start_sec": a, "end_sec": b, "quote": _quote_at(a)}
                         for a, b in spans]}


CANDIDATE_SPANS: list[tuple[str, str, list[tuple[float, float]]]] = [
    ("c01", "recap_dialogue", [(10.0, 55.0)]),
    ("c02", "highlight", [(55.0, 100.0)]),
    # ⚠ 조각 **둘**이다. `conflict_payoff` 는 turn·payoff 두 역할을 요구하는데
    #   비트는 조각 하나당 하나라(`bridge.to_beats`), 조각이 하나면 그 편은 10단계에서
    #   영원히 반려당한다(보고서 notes — 6단계가 막아야 할 조합이다).
    ("c03", "conflict_payoff", [(100.0, 122.0), (126.0, 145.0)]),
    ("c04", "chemi_observe", [(145.0, 190.0)]),
    # 조각 2개 = 비트 2개. 살붙이기가 비트를 두 개 받는 편이 하나는 있어야 라벨 앵커
    # 이동·클립 분할이 실제로 돈다(합집합 IoU 는 전부 0.5 미만이라 7단계 dedup 밖이다).
    ("c05", "recap_dialogue", [(20.0, 42.0), (120.0, 143.0)]),
]

CANDIDATES_JSON = json.dumps({
    "candidates": [_cand(cid, tpl, spans) for cid, tpl, spans in CANDIDATE_SPANS],
    "exception_sector": {"intro": {"start_sec": 0.0, "end_sec": 5.0},
                         "recap": None, "teaser": None, "credit": None, "end": None},
}, ensure_ascii=False)

FLAGS_JSON = '{"seam_jump": false, "hook_weak": false, "evidence_sec": []}'


# ── 프롬프트 → 응답 (가짜 모델) ────────────────────────────────────────────
# 응답을 **프롬프트에서 만들어 낸다.** 고정 JSON 을 돌려주면 후보가 하나만 바뀌어도
# 검증기가 전량 반려하고, 그러면 테스트가 배선이 아니라 픽스처를 재게 된다.

def flesh_reply(prompt: str) -> dict:
    """살붙이기 프롬프트 → 계약을 지키는 응답. 재료 표에서 비트·span 을 읽어 짓는다."""
    template = re.search(r"템플릿 (\S+) ·", prompt).group(1)
    need = list((flesh_mod.STORY_TEMPLATE_SPECS[template].get("required_roles") or ()))
    beats: list[dict] = []
    # "### 비트 0 — …" 다음의 "spXXXX | …" 줄들이 그 비트의 재료다.
    for block in prompt.split("### 비트 ")[1:]:
        number = int(block.split(" ", 1)[0])
        ids = re.findall(r"^(sp\d+) \|", block, re.M)
        beats.append({"number": number, "role": "build", "narration": [], "labels": [],
                      "_ids": ids})
    # 필수 역할은 **뒤 비트부터** 채운다(절정이 뒤에 오는 것이 이 템플릿들의 문법이다).
    for i, role in enumerate(reversed(need)):
        beats[max(0, len(beats) - 1 - i)]["role"] = role
    beats[0]["narration"] = ["형이 먼저 말했어요", "동생은 굳었죠"]
    if beats[-1]["_ids"]:
        beats[-1]["labels"] = [{"text": "(팩폭 시전)", "span_id": beats[-1]["_ids"][0]},
                               {"text": "(정적)", "span_id": beats[-1]["_ids"][-1]}]
    return {"title": {"line1": "형이 던진 한마디", "line2": "동생은 무너졌다"},
            "reason": "형제 갈등이 한 대사에서 뒤집힌다",
            "beats": [{k: v for k, v in b.items() if k != "_ids"} for b in beats]}


def description_reply(_prompt: str) -> dict:
    return {"description": "형제의 말싸움이 한 대사로 뒤집히는 순간.",
            "hashtags": ["#형제", "#말싸움", "#리액션"]}


def detail_reply(prompt: str) -> dict:
    """10a 프롬프트 → 창의 span 전량을 한 meaning 으로 묶은 응답(화자 둘)."""
    rows = re.findall(r"^(sp\d+) \| \S+ \| (유성|무성) \| (.*)$", prompt, re.M)
    spans = []
    for i, (sid, kind, text) in enumerate(rows):
        node = {"id": sid, "scene_script": "화면 묘사", "characters": ["갑"],
                "importance": 3}
        if kind == "유성":
            # 전사와 같은 말을 들었다고 답한다 — 각색 판정(diff 0.35)에 걸리지 않는다.
            node["heard"] = [{"speaker": "갑" if i % 2 == 0 else "을", "line": text}]
        spans.append(node)
    return {"meanings": [{"first_span": rows[0][0], "last_span": rows[-1][0],
                          "content": "누가 무엇을 하고 있다", "characters": ["갑", "을"],
                          "importance": 4, "mood": "긴장", "spans": spans}]}


def _kind(prompt: str) -> str:
    if prompt.startswith("당신은 쇼츠 편집 후보를 고르는"):
        return "candidates"
    if prompt.startswith("당신은 방송 편집 검수자다"):
        return "boundary"
    if prompt.startswith("첨부한 영상은 쇼츠 후보의 조각들을"):
        return "flags"
    if prompt.startswith("당신은 리캡 쇼츠 구성작가다"):
        return "flesh"
    if prompt.startswith("아래 쇼츠 한 편의"):
        return "description"
    if prompt.startswith("당신은 방송 영상의 장면 기록가다"):
        return "detail"
    return "unknown"


def _response(text: str):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1000, thoughts_token_count=10,
                                       candidates_token_count=50,
                                       cached_content_token_count=0,
                                       total_token_count=1060),
        model_version=MODEL,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))])


class FakeGemini:
    """진짜 `call_video`·`_call_text` 를 통과시키는 가짜. `types` 는 **진짜 SDK**."""

    def __init__(self, router):
        from google.genai import types

        self.types = types
        self._router = router
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self.client = SimpleNamespace(
            models=SimpleNamespace(generate_content=self._generate),
            files=SimpleNamespace())
        self.config = SimpleNamespace(flash_model_name=MODEL, model_name=MODEL,
                                      analysis_thinking_level="minimal")

    def _generate(self, *, model, contents, config):
        prompt = contents[-1]
        kind = _kind(prompt)
        with self._lock:
            self.calls.append({"kind": kind, "model": model,
                               "parts": len(contents) - 1, "prompt": prompt})
        out = self._router(kind, prompt)
        if isinstance(out, BaseException):
            raise out
        return _response(out if isinstance(out, str)
                         else json.dumps(out, ensure_ascii=False))

    def kinds(self) -> list[str]:
        with self._lock:
            return [c["kind"] for c in self.calls]

    def count(self, kind: str) -> int:
        return sum(1 for k in self.kinds() if k == kind)

    def prompts(self, kind: str) -> list[str]:
        with self._lock:
            return [c["prompt"] for c in self.calls if c["kind"] == kind]


def default_router(*, flesh=flesh_reply):
    def route(kind, prompt):
        if kind == "candidates":
            return CANDIDATES_JSON
        if kind == "boundary":
            return '{"boundary": "none"}'
        if kind == "flags":
            return FLAGS_JSON
        if kind == "flesh":
            return flesh(prompt)
        if kind == "description":
            return description_reply(prompt)
        if kind == "detail":
            return detail_reply(prompt)
        raise AssertionError(f"모르는 프롬프트로 호출됐다: {prompt[:60]!r}")
    return route


# ── 소재·가짜 앞단 (M3 배선 테스트와 같은 구성) ────────────────────────────

@pytest.fixture(scope="module")
def synth(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("v4m5src")
    src = d / "src.mp4"
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-f", "lavfi",
         "-i", f"testsrc2=size=160x120:rate=6:duration={SRC_SEC:.0f}",
         "-f", "lavfi", "-i", f"sine=frequency=330:duration={SRC_SEC:.0f}",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    return src


@pytest.fixture
def fake_transcribe(monkeypatch):
    monkeypatch.setattr(v4p, "transcribe_words",
                        lambda audio, dur, **kw: ([dict(w) for w in WORDS], []))
    monkeypatch.setattr(v4p, "retranscribe_gaps",
                        lambda *a, **k: (list(a[1]), {"gaps": 0, "windows": [],
                                                      "recovered_words": 0}))


@pytest.fixture
def fake_proxy(monkeypatch):
    rec: dict = {"alive": True}
    m = pytypes.ModuleType("app.v4.proxy")
    m.PROXY_HEIGHT, m.PROXY_FILE_FPS, m.PROXY_CRF = 720, 30.0, 30
    m.proxy_path_for = lambda outdir, *, height, file_fps: (
        Path(outdir) / f"scan_{height}p{int(file_fps)}.mp4")
    m.proxy_fingerprint = lambda video, **kw: "fp-proxy"
    m.upload_checkpoint_doc = lambda *, fingerprint, proxy_path, proxy_meta, handle_meta: {
        "schema": "v4_upload/v1", "fingerprint": fingerprint,
        "proxy": proxy_meta, "handle": handle_meta}

    def build_proxy(video, out_path, **kw):
        Path(out_path).write_bytes(b"fake-proxy")
        return Path(out_path), {"height": 720, "file_fps": 30.0, "crf": 30,
                                "bytes": 10, "elapsed_sec": 0.0, "reused": False}

    def upload_handle(gemini, proxy, **kw):
        return SimpleNamespace(uri="files/fake-uri", name="files/fake"), {
            "uri": "files/fake-uri", "name": "files/fake", "bytes": 10,
            "elapsed_sec": 0.0}

    m.build_proxy = build_proxy
    m.upload_handle = upload_handle
    m.handle_alive = lambda gemini, ref: rec["alive"]
    import app.v4 as v4pkg
    monkeypatch.setitem(sys.modules, "app.v4.proxy", m)
    monkeypatch.setattr(v4pkg, "proxy", m, raising=False)
    return rec


@pytest.fixture
def gem(monkeypatch):
    holder: dict = {"router": default_router()}
    client = FakeGemini(lambda *a: holder["router"](*a))
    holder["client"] = client
    monkeypatch.setattr(v4p, "_load_gemini_client", lambda: client)
    return holder


@pytest.fixture
def no_boundary_calls(monkeypatch):
    """6b 는 이 파일의 관심 밖이다 — 원판정 유지로 고정한다."""
    def fake(gemini, handle, *, exception_sector, grid, duration_sec, log=print, **kw):
        return dict(exception_sector), {"moved": 0, "flash_calls": 0, "tokens": 0,
                                        "stopped": None, "probes": [], "notes": []}

    monkeypatch.setattr(boundary_mod, "run_boundary_probe", fake)


# ── 읽기 도우미 ─────────────────────────────────────────────────────────────

def _run_log(out: Path) -> dict:
    return json.loads((out / "run_log.json").read_text(encoding="utf-8"))


def _steps(out: Path, name: str) -> list[dict]:
    return [s for s in _run_log(out)["steps"] if s["step"] == name]


def _entry(out: Path, name: str) -> dict:
    got = _steps(out, name)
    assert got, f"{name} 단계 기록이 없다: {[s['step'] for s in _run_log(out)['steps']]}"
    return got[-1]


def _story(out: Path) -> dict:
    return json.loads((out / "checkpoint_story.json").read_text(encoding="utf-8"))


def _run(synth, outdir, **kw):
    kw.setdefault("skip_research", True)
    kw.setdefault("log", lambda *a: None)
    # 🛑 **10a 까지만 돈다.** 이 파일의 관심은 10·10a 이고, M6 가 11단계(편집 재료~
    # 렌더)를 배선한 뒤로는 여기서 멈추지 않으면 그 다섯 조각까지 함께 돈다 — 이
    # 파일의 가짜 클라이언트는 스타일 호출(영상 업로드·재시도 설정)을 흉내 내지
    # 않으므로 11 이 그 자리에서 죽는다. 11 의 배선 가드는
    # `tests/test_v4_pipeline_m6.py` 다(거기 가짜가 그 호출까지 덮는다).
    kw.setdefault("stop_after", "detail")
    return v4p.run_v4(video_path=synth, work_title="합성", outdir=outdir, **kw)


# ═══════════════════════════════════════════════════════════════════════════
# ① checkpoint_story.json 이 v1 모양인가 — v3 가 깨뜨린 그 파일
# ═══════════════════════════════════════════════════════════════════════════

def test_story_checkpoint_is_the_v1_shape(synth, tmp_path, fake_transcribe,
                                          fake_proxy, gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o")
    doc = _story(out)

    assert doc["variants"], "승인 편이 variants 로 실리지 않았다"
    for v in doc["variants"]:
        # 🛑 현지화 L3(app/localize/apply.py:183)가 이 열쇠에 일본어 제목을 덮어쓴다.
        assert isinstance(v["title_text"], str) and "\n" in v["title_text"]
        assert v["clips"], "클립이 비었다 — v1 재개가 빈 타임라인을 만든다"
        for c in v["clips"]:
            # v1 재개는 `StoryClip(**c)` 다(app/pipeline.py:3470) — 열쇠가 하나라도
            # 더 있으면 그 자리에서 TypeError.
            assert set(c) == set(v4p.STORY_CLIP_FIELDS), sorted(c)
            assert c["end_sec"] > c["start_sec"]
        assert isinstance(v["score"], float)
        assert isinstance(v["tts_cues"], list)
        # v4 가산 키 — M6 가 읽는 재료
        assert v["candidate_id"] and v["beats"]

    # 하위 호환(v1 은 variants 와 **함께** 쓴다)
    assert doc["title_text"] == doc["variants"][0]["title_text"]
    assert doc["clips"] == doc["variants"][0]["clips"]
    assert doc["pipeline"] == "v4"
    assert doc["fallback"] is False and doc["fallback_reason"] is None


def test_story_checkpoint_passes_the_contract_tool(synth, tmp_path, fake_transcribe,
                                                   fake_proxy, gem, no_boundary_calls):
    """계약 대조 도구(`scripts/v4_contract_diff.py`)가 이 job 을 통과시키는가.

    v3 사고의 재발을 막는 기계가 그것 하나다 — 테스트가 직접 키를 세는 것과 별개로
    **그 도구로** 봐야 한다(도구가 보는 키 목록이 정본이다).

    ⚠ **선행 위반 1건이 있다(M5 범위 밖 · 보고서 contract_issues).** 도구는
    `checkpoint_candidates.json` 의 **뿌리**에 `approved` 를 요구하는데(기획서 §6 — 편집실·
    성과 조인이 읽는 순위 순 승인 목록) 배선은 그것을 `approval.approved` 절 안에만 쓴다.
    고치려면 `STEP_SECTIONS["approve"]` 에 절을 하나 더해야 하고 그건 M4 가 세운 절
    소유표·하류 폐기 표(그리고 그것을 값으로 고정한 M3 테스트)를 함께 바꾸는 일이라
    여기서 손대지 않는다. 그래서 이 테스트는 **위반이 늘지 않았는가**를 본다."""
    from scripts.v4_contract_diff import check_job, load_job

    out = _run(synth, tmp_path / "o")
    docs, present, unreadable = load_job(out)
    result = check_job(docs, present=present, unreadable=unreadable, job=str(out))
    story = [f for f in result["files"] if f["file"] == "checkpoint_story.json"]
    assert story and story[0]["status"] == "ok", story
    assert not [f for f in result["files"] if f["status"] == "unreadable"]
    bad = {f["file"]: [v["where"] for v in f["violations"]]
           for f in result["files"] if f["status"] == "violation"}
    assert bad == {"checkpoint_candidates.json": ["approved"]}, bad


def test_localization_can_rewrite_every_variant_title(synth, tmp_path, fake_transcribe,
                                                      fake_proxy, gem,
                                                      no_boundary_calls):
    """현지화 L3 의 실제 코드 모양을 그대로 흉내 낸다(app/localize/apply.py:182~186).

    v3 는 이 자리에서 **아무 일도 안 했다** — `variants` 도 `title_text` 도 없어서
    else 가지의 `story["title_text"]` 만 세팅되고 렌더는 원본 제목을 그대로 구웠다."""
    out = _run(synth, tmp_path / "o")
    story = _story(out)
    assert "variants" in story
    for v in story["variants"]:
        v["title_text"] = "日本語タイトル"
    assert all(v["title_text"] == "日本語タイトル" for v in story["variants"])


# ═══════════════════════════════════════════════════════════════════════════
# ② 10a 는 10 **앞에서** 돈다 (표 순서와 자료 흐름이 어긋나는 자리)
# ═══════════════════════════════════════════════════════════════════════════

def test_detail_runs_before_flesh_and_feeds_it(synth, tmp_path, fake_transcribe,
                                               fake_proxy, gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o", winner_detail=True, max_shorts=1)
    kinds = gem["client"].kinds()
    assert "detail" in kinds and "flesh" in kinds
    assert kinds.index("detail") < kinds.index("flesh"), \
        f"10a 가 10 뒤에 돌았다 — 살붙이기가 화자를 못 받는다: {kinds}"

    # 산출과 기록
    dd = json.loads((out / "checkpoint_winner_detail.json").read_text(encoding="utf-8"))
    assert dd["detail"] and dd["audit"]["speakers"]
    entry = _entry(out, "detail")
    assert entry["spans_detailed"] > 0 and entry["speakers"]
    # 기록은 **한 줄**이다 — 표의 detail 자리가 다시 돌면 감사가 거짓말을 한다.
    assert len(_steps(out, "detail")) == 1

    # 화자가 실제로 살붙이기까지 갔는가(다리가 낸 값 그대로)
    flesh_entry = _entry(out, "flesh")
    assert flesh_entry["speakers"], "10a 를 켰는데 화자가 flesh 기록에 없다"
    assert flesh_entry["speaker_warning"] is None
    assert flesh_entry["detail_spans"] == len(dd["detail"])


def test_detail_off_says_the_subtitles_will_be_white(synth, tmp_path, fake_transcribe,
                                                     fake_proxy, gem,
                                                     no_boundary_calls):
    """🛑 계약 §0 의 발견 — 10a 가 꺼지면 화자가 **어디에도 없다**.

    조용히 넘어가면 '왜 자막이 전 줄 흰색이지'가 되고, 그 답을 코드에서 찾아야 한다."""
    logs: list[str] = []
    out = _run(synth, tmp_path / "o", log=logs.append)
    assert gem["client"].count("detail") == 0

    from app.v4.bridge import NO_SPEAKER_WARNING

    entry = _entry(out, "detail")
    assert entry["skipped"] == "--winner-detail 미지정"
    assert entry["warning"] == NO_SPEAKER_WARNING
    assert entry["speaker_source"] == "none"
    flesh_entry = _entry(out, "flesh")
    assert flesh_entry["speakers"] == []
    assert flesh_entry["speaker_warning"] == NO_SPEAKER_WARNING
    assert any(NO_SPEAKER_WARNING in line for line in logs), \
        "stdout 에 화자 없음 경고가 없다(조용한 흰 자막)"
    assert not (out / "checkpoint_winner_detail.json").exists()


def test_cli_carries_the_winner_detail_flag(synth, tmp_path, monkeypatch):
    """플래그가 배선까지 닿는가 — 기본은 꺼짐(기획서 §9 N1)."""
    from app.v4 import cli

    # 이 개발 머신엔 ffmpeg 8 뿐이다(운영 노드는 7.x) — CLI 의 버전 관문만 연다
    # (`tests/test_v4_pipeline.py::_allow_ffmpeg8` 와 같은 이유).
    monkeypatch.setenv("AI_VIDEO_ALLOW_UNSUPPORTED_FFMPEG", "1")
    seen: dict = {}
    monkeypatch.setattr("app.v4.pipeline.run_v4",
                        lambda **kw: (seen.update(kw), tmp_path)[1])
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    base = ["--video", str(synth), "--work-title", "합성", "--outdir", str(tmp_path)]
    cli.main(base)
    assert seen["winner_detail"] is False
    cli.main(base + ["--winner-detail"])
    assert seen["winner_detail"] is True


# ═══════════════════════════════════════════════════════════════════════════
# ③ 편별 실패 격리 · 전량 실패는 크게
# ═══════════════════════════════════════════════════════════════════════════

def _flesh_but_break(cid: str):
    """그 후보의 프롬프트에만 계약 위반 응답을 준다(제목이 상한을 넘는다)."""
    def make(prompt: str):
        if f"{cid} 선택 사유" in prompt:
            return {"title": {"line1": "가" * (flesh_mod.TITLE_MAX_CHARS + 5),
                              "line2": "나"},
                    "reason": "일부러 반려당하는 제목", "beats": []}
        return flesh_reply(prompt)
    return make


def test_one_failed_episode_does_not_take_the_others(synth, tmp_path, fake_transcribe,
                                                     fake_proxy, gem,
                                                     no_boundary_calls):
    gem["router"] = default_router(flesh=_flesh_but_break("c01"))
    out = _run(synth, tmp_path / "o")
    doc = _story(out)
    ids = [v["candidate_id"] for v in doc["variants"]]
    assert "c01" not in ids and ids, ids
    entry = _entry(out, "flesh")
    assert entry["failed"] == 1 and entry["ok"] == len(ids)
    assert [r["id"] for r in entry["failed_episodes"]] == ["c01"]
    assert entry["failed_episodes"][0]["reason"] == flesh_mod.REASON_REASK_EXHAUSTED


def test_every_episode_failing_dies_loudly(synth, tmp_path, fake_transcribe,
                                           fake_proxy, gem, no_boundary_calls):
    """🛑 승인이 있었는데 낼 것이 없다 = 조용한 결번. 이 레포에서 가장 나쁜 실패다."""
    def all_bad(prompt: str):
        return {"title": {"line1": "", "line2": ""}, "reason": "", "beats": []}

    gem["router"] = default_router(flesh=all_bad)
    with pytest.raises(ValueError, match="전량 실패"):
        _run(synth, tmp_path / "o")
    out = next((tmp_path / "o").glob("합성_*"))
    err = _entry(out, "error")
    assert err["at"] == "flesh"
    assert not (out / "checkpoint_story.json").exists(), \
        "전량 실패인데 빈 story 파일을 남겼다 — 하류가 그것을 정상으로 읽는다"


# ═══════════════════════════════════════════════════════════════════════════
# ④ 캐시·재개
# ═══════════════════════════════════════════════════════════════════════════

def test_resume_reads_the_story_from_cache(synth, tmp_path, fake_transcribe,
                                           fake_proxy, gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o", winner_detail=True, max_shorts=1)
    before = (_story(out), gem["client"].count("flesh"), gem["client"].count("detail"))

    again = _run(synth, tmp_path / "o", job_id=out.name, winner_detail=True,
                 max_shorts=1)
    assert again == out
    assert gem["client"].count("flesh") == before[1], "재개가 살붙이기를 다시 불렀다"
    assert gem["client"].count("detail") == before[2], "재개가 10a 를 다시 불렀다"
    assert _entry(out, "flesh")["cached"] is True
    assert _entry(out, "detail")["cached"] is True
    assert _story(out) == before[0]


def test_from_step_flesh_rebuilds_the_story(synth, tmp_path, fake_transcribe,
                                            fake_proxy, gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o", max_shorts=1)
    n = gem["client"].count("flesh")
    _run(synth, tmp_path / "o", job_id=out.name, from_step="flesh", max_shorts=1)
    assert gem["client"].count("flesh") == n + 1
    assert _entry(out, "flesh")["cache_reason"] == "--from-step 무효화"


def test_winner_detail_toggle_invalidates_the_story(synth, tmp_path, fake_transcribe,
                                                    fake_proxy, gem, no_boundary_calls):
    """10a 를 켜면 카피 재료(화자·대사)가 바뀐다 — 지문이 그것을 봐야 한다."""
    out = _run(synth, tmp_path / "o", max_shorts=1)
    fp_off = _entry(out, "flesh")["fingerprint"]
    _run(synth, tmp_path / "o", job_id=out.name, winner_detail=True, max_shorts=1)
    entry = _entry(out, "flesh")
    assert entry.get("cached") is not True
    assert entry["fingerprint"] != fp_off
    assert entry["winner_detail"] is True


def test_story_cache_is_not_reused_without_a_fingerprint(synth, tmp_path,
                                                         fake_transcribe, fake_proxy,
                                                         gem, no_boundary_calls):
    """이름이 v1·v3 와 같은 파일이다 — 남이 쓴 것을 '있으니 쓴다'로 집으면 안 된다."""
    out = _run(synth, tmp_path / "o", max_shorts=1)
    v4p._meta_path(out / "checkpoint_story.json").unlink()
    n = gem["client"].count("flesh")
    _run(synth, tmp_path / "o", job_id=out.name, max_shorts=1)
    assert gem["client"].count("flesh") == n + 1
    assert "지문 미기록" in _entry(out, "flesh")["cache_reason"]


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ --max-shorts 로 잘린 편은 살붙이기도 안 한다(요금)
# ═══════════════════════════════════════════════════════════════════════════

def test_capped_episodes_cost_nothing(synth, tmp_path, fake_transcribe, fake_proxy,
                                      gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o", max_shorts=1, winner_detail=True)
    approved = _entry(out, "approve")["approved"]
    assert len(approved) == 1
    assert gem["client"].count("flesh") == 1
    assert gem["client"].count("description") == 1
    assert len(_story(out)["variants"]) == 1
    # 10a 창도 그 한 편의 구간만 본다
    prompts = gem["client"].prompts("detail")
    assert prompts and len(prompts) <= 2


# ═══════════════════════════════════════════════════════════════════════════
# ⑥ 단계 표·AST 가드
# ═══════════════════════════════════════════════════════════════════════════

def test_step_tables_stay_in_sync():
    assert {"flesh", "detail"} <= v4p.IMPLEMENTED_STEPS
    assert "flesh" not in v4p.NOT_IMPLEMENTED_MILESTONE
    assert "detail" not in v4p.NOT_IMPLEMENTED_MILESTONE
    # M6 이 11단계 다섯 조각을 배선하면서 미구현 표는 **비었다**(그 사실이 곧
    # "단계 표가 전부 배선됐다"이고, `_check_step_coverage` 가 임포트 시점에 본다).
    assert v4p.NOT_IMPLEMENTED_MILESTONE == {}
    assert {"11:resources", "11:draft", "11:style", "11:render",
            "11:validate"} <= v4p.IMPLEMENTED_STEPS
    # 10·10a 는 `checkpoint_candidates.json` 의 절을 쓰지 않는다(자기 파일을 쓴다).
    assert "flesh" not in v4p.STEP_SECTIONS and "detail" not in v4p.STEP_SECTIONS


def test_stop_after_detail_leaves_eleven_unrun(synth, tmp_path, fake_transcribe,
                                              fake_proxy, gem, no_boundary_calls):
    """`--stop-after detail` 은 11단계를 **기록을 남기고** 건너뛴다(조용한 스킵 금지).

    ⚠ M6 이전에는 같은 자리가 `not_implemented` 였다 — 이제 11 은 배선돼 있고, 이
    파일이 거기까지 돌지 않는 것은 `--stop-after` 때문이다(`_run` 의 기본값)."""
    out = _run(synth, tmp_path / "o", max_shorts=1)
    entry = _entry(out, "11:resources")
    assert entry["skipped"] == "--stop-after detail"
    assert entry["remaining"][0] == "11:resources"
    assert not (out / "edit_plan.json").exists()


def test_ast_guards_still_pass_on_the_m5_wiring():
    from test_v4_guards import _v4_sources, from_step_comparisons, imported_modules

    src = Path(v4p.__file__).read_text(encoding="utf-8")
    assert from_step_comparisons(src) == [], "손으로 적은 from_step 판정이 생겼다"
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert code.count("should_run(") == 1, "should_run 호출이 흩어졌다"
    assert _v4_sources(), "가드가 훑을 파일 목록이 비었다"
    got = {name for name, _ in imported_modules(src, package="app.v4")}
    assert not (got & {"app.pipeline", "app.v3.pipeline"})


# ═══════════════════════════════════════════════════════════════════════════
# ⑦ 순수 변환기 — 여기서 틀리면 나는 소리가 없다
# ═══════════════════════════════════════════════════════════════════════════

SPAN_INDEX = {
    "sp0000": {"t_in": 0.0, "t_out": 4.0, "is_audio": True},
    "sp0001": {"t_in": 4.0, "t_out": 8.0, "is_audio": True},
    "sp0002": {"t_in": 20.0, "t_out": 24.0, "is_audio": True},
}

STORY_DOC = {
    "candidate_id": "c01", "template": "recap_dialogue", "reason": "사유",
    "title": {"line1": "윗줄", "line2": "아랫줄"},
    "beats": [{"number": 0, "role": "hook", "span_ids": ["sp0000", "sp0001"],
               "narration": ["한 줄"], "labels": [], "muted_span_ids": ["sp0001"]},
              {"number": 1, "role": "climax", "span_ids": ["sp0002"],
               "narration": [], "labels": [{"text": "(정적)", "span_id": "sp0002"}],
               "muted_span_ids": []}],
    "narration_cues": [{"beat": 0, "line": 0, "text": "한 줄", "mode": "muted",
                        "source_time_sec": 4.0, "source_end_sec": 6.5,
                        "muted_span_ids": ["sp0001"]}],
    "narration_dropped": [], "segments": [], "span_ids": [],
    "budget": {"target_sec": 80.0, "max_sec": 120.0},
}


def test_story_clips_reuse_the_assemble_grouping():
    """묶는 규칙은 `assemble_edit_plan` 것이다 — 뮤트 전환·소스 불연속에서 잘린다."""
    clips = v4p.story_clips(STORY_DOC, SPAN_INDEX, video_path="/x.mp4",
                            work_title="작품")
    assert [(c["start_sec"], c["end_sec"], c["use_original_audio"]) for c in clips] == [
        (0.0, 4.0, True),      # sp0000 — 뮤트 아님
        (4.0, 8.0, False),     # sp0001 — 뮤트라 제 클립을 갖는다
        (20.0, 24.0, True)]    # 소스 불연속(8 → 20)
    assert clips[-1]["subtitle"] == "(정적)", "라벨이 앵커 클립에 안 붙었다"
    assert clips[0]["role"] == "hook" and clips[-1]["role"] == "climax"
    for c in clips:
        assert set(c) == set(v4p.STORY_CLIP_FIELDS)


def test_story_tts_cues_keep_the_source_coordinates():
    cues = v4p.story_tts_cues(STORY_DOC)
    assert cues == [{"text": "한 줄", "source_time_sec": 4.0, "source_end_sec": 6.5,
                     "duration_sec": 2.5, "beat": 0, "line": 0, "mode": "muted",
                     "muted_span_ids": ["sp0001"]}]
    # 목소리는 채널·편집실의 것이다(E11·E12) — 여기서 박아 두면 계약처럼 굳는다.
    assert "voice" not in cues[0] and "speed" not in cues[0]


def test_story_checkpoint_doc_fallback_reason_is_a_dict():
    """v1 `_story_checkpoint_fallback`(app/pipeline.py:110)은 문자열 사유를 **버린다**."""
    doc = v4p.story_checkpoint_doc(
        [STORY_DOC], SPAN_INDEX, video_path="/x.mp4", work_title="작품",
        scores={"c01": 1.25}, fallback=True,
        fallback_reason={"kind": "v4_approve_fallback", "id": "c01", "reasons": ["a"]})
    assert doc["variants"][0]["score"] == 1.25
    assert isinstance(doc["fallback_reason"], dict)

    from app.pipeline import _story_checkpoint_fallback

    assert _story_checkpoint_fallback(doc) == (
        True, {"kind": "v4_approve_fallback", "id": "c01", "reasons": ["a"]})


def test_story_checkpoint_doc_survives_an_empty_variant_list():
    """빈 목록은 배선이 먼저 막지만(NO_APPROVED_MESSAGE), 모양은 무너지지 않아야 한다."""
    doc = v4p.story_checkpoint_doc([], {}, video_path="/x.mp4", work_title="작품")
    assert doc["variants"] == [] and doc["clips"] == [] and doc["title_text"] == ""


def test_detail_windows_carry_the_snap_slack():
    """🛑 스냅은 flesh 입구에서 일어난다(다리는 한 곳) — 10a 창이 그만큼 넓지 않으면
    스냅으로 들어온 가장자리 span 만 화자가 없다(그리고 아무 소리도 안 난다)."""
    from app.v4.bridge import SNAP_END_FWD_SEC, SNAP_START_BACK_SEC

    got = v4p.detail_windows_for([{"start_sec": 30.0, "end_sec": 40.0}],
                                 source_duration_sec=100.0)
    assert [(w.start_sec, w.end_sec) for w in got] == [
        (30.0 - SNAP_START_BACK_SEC, 40.0 + SNAP_END_FWD_SEC)]

    # 소스 경계 밖으로는 안 나간다
    got = v4p.detail_windows_for([{"start_sec": 0.5, "end_sec": 99.0}],
                                 source_duration_sec=100.0)
    assert got[0].start_sec == 0.0 and got[-1].end_sec == 100.0

    # 상한을 넘으면 등분(그리디로 자르면 마지막이 슬리버가 된다)
    got = v4p.detail_windows_for([{"start_sec": 0.0, "end_sec": 400.0}],
                                 source_duration_sec=400.0,
                                 max_sec=detail_mod.DETAIL_WINDOW_MAX_SEC)
    assert len(got) == 3 and got[0].end_sec == got[1].start_sec

    with pytest.raises(ValueError, match="시각을 읽을 수 없다"):
        v4p.detail_windows_for([{"start_sec": "가"}], source_duration_sec=100.0)


def test_detail_windows_settle_on_span_boundaries():
    """🛑 실측 크래시의 회귀 가드.

    창 안 span 은 **중점 규칙**으로 정해지는데(`spans_for_chunk`) 그 규칙은 경계에 걸친
    span 을 창 안으로 넣는다. 그런데 `detail.span_table` 은 창 밖 시각을 만나면 크게
    실패한다 — 합성 소재 실행에서 `t_in 117.0 ∉ [118.0, 145.0]` 로 실제로 죽었다.
    조각 경계는 모델이 부른 임의의 초라 이 충돌은 정상 입력에서 난다."""
    grid = {"source": {"duration_sec": 100.0},
            "span_candidates": [
                {"id": "sp0000", "t_in": 0.0, "t_out": 30.0, "is_audio": False},
                {"id": "sp0001", "t_in": 30.0, "t_out": 34.0, "is_audio": True},
                {"id": "sp0002", "t_in": 34.0, "t_out": 48.0, "is_audio": True},
                {"id": "sp0003", "t_in": 48.0, "t_out": 100.0, "is_audio": False}]}
    # 창 [31, 45] → 중점이 안에 드는 span 은 sp0001(32)·sp0002(41) 둘. 정착하면 [30, 48].
    got = v4p.detail_windows_for([{"start_sec": 33.0, "end_sec": 43.0}],
                                 source_duration_sec=100.0, grid=grid, max_sec=180.0)
    assert [(w.start_sec, w.end_sec) for w in got] == [(30.0, 48.0)]

    for w in got:
        # 정착한 창으로는 표를 만들 수 있다(= 크래시가 안 난다)
        spans = detail_mod.spans_for_chunk(grid, w.start_sec, w.end_sec)
        assert detail_mod.span_table(spans, w)
