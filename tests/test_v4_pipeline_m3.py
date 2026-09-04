"""V4-M3 배선 회귀 가드 — `app/v4/pipeline.py` 의 6~9단계(후보 깔때기).

계약 `docs/v4/M3-interfaces.md` §5(배선). 이 파일이 고정하는 것은 **이음새**다 —
6·6b·8 이 부르는 LLM 쪽 로직과 6c·7·9 의 순수 함수는 각자 자기 테스트가 있고
(`test_v4_candidates.py`·`test_v4_boundary.py`·`test_v4_verify.py`·`test_v4_funnel.py`·
`test_v4_flags.py`·`test_v4_approve.py`), 여기서 보는 것은 그 여섯이 한 파일에
**증분으로** 쌓이는가·재개가 각 절을 캐시로 읽는가·8단계 부분 실패가 나머지를 안
지우는가·`no_publishable` 이 조용하지 않은가다.

🛑 v3-M2 의 교훈: "유닛 테스트는 두 모듈이 각각 옳아서 이음새의 결함을 못 잡았다"
(`app/v4/funnel.py` 의 전량 탈락 폴백 주석). 그래서 여기서는 여섯 단계를 **끝까지**
돌린다.

⚠ **네트워크는 쓰지 않는다.** `GEMINI_API_KEY` 가 없는 워크트리라 실호출 검증은 불가하고
(계약 §0), 가짜 클라이언트가 진짜 `call_video` 를 통과시킨다 — `gemini.types` 는 **진짜
google-genai** 라 파트 조립·offset 포맷이 실제로 검증된다. 실호출로만 알 수 있는 것
(프롬프트 품질 · 서버가 파트 순서대로 이어 붙여 보는가 · 토큰 실측)은 범위 밖이다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import types as pytypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.v4 import approve as approve_mod
from app.v4 import boundary as boundary_mod
from app.v4 import candidates as cand_mod
from app.v4 import flags as flags_mod
from app.v4 import pipeline as v4p

MODEL = "gemini-3.7-flash"
SRC_SEC = 200.0            # 후보 길이 정책(40~120초)이 성립하는 최소한의 소재

# ── 합성 재료 ───────────────────────────────────────────────────────────────
# 3초마다 2초짜리 발화. 발화 커버리지 2/3 ≈ 0.667 로 7단계 하한(0.55)을 넘긴다.
# 글자를 전부 다르게 두는 것은 6c 의 인용 대조 때문이다 — 같은 문장이 여러 곳에 있으면
# `find_quote_times` 가 여러 히트를 내고 어느 것이 골라졌는지가 테스트의 관심 밖으로 샌다.
WORDS = [{"t0": t, "t1": t + 2.0, "prob": 0.9,
          "text": f"{int(t)}초 지점에서 벌어진 결정적인 장면의 대사"}
         for t in [float(x) for x in range(6, 196, 3)]]


def _quote_at(t: float) -> str:
    """그 시각에 실제로 있는 대사 한 줄 — 모델이 '전사에서 그대로 옮긴' 것을 흉내낸다."""
    return next(w["text"] for w in WORDS if w["t0"] >= t)


def _cand(cid: str, template: str, spans: list[tuple[float, float]]) -> dict:
    return {"id": cid, "template": template,
            "reason": f"{cid} 선택 사유 한 문장",
            "title_draft": {"line1": "윗줄 가안", "line2": "아랫줄 가안"},
            "segments": [{"start_sec": a, "end_sec": b, "quote": _quote_at(a)}
                         for a, b in spans]}


# 서로 다른 아크 5개. 합집합 IoU 는 전부 0.5 미만이라 7단계 dedup 에 걸리지 않는다
# (c05 대 c01 = 0.32 · c05 대 c03 = 0.34 — 손으로 계산해 고른 값이다).
CANDIDATE_SPANS: list[tuple[str, str, list[tuple[float, float]]]] = [
    ("c01", "recap_dialogue", [(10.0, 55.0)]),
    ("c02", "highlight", [(55.0, 100.0)]),
    ("c03", "conflict_payoff", [(100.0, 145.0)]),
    ("c04", "chemi_observe", [(145.0, 190.0)]),
    ("c05", "recap_dialogue", [(20.0, 42.0), (120.0, 143.0)]),   # 조각 2개 = 이음새 1개
]

CANDIDATES_JSON = json.dumps({
    "candidates": [_cand(cid, tpl, spans) for cid, tpl, spans in CANDIDATE_SPANS],
    "exception_sector": {"intro": {"start_sec": 0.0, "end_sec": 5.0},
                         "recap": None, "teaser": None, "credit": None, "end": None},
}, ensure_ascii=False)

# 6c 가 전량 드롭하는 판 — 조각이 길이 하한(40초)에 한참 못 미친다.
TINY_JSON = json.dumps({
    "candidates": [_cand(cid, tpl, [(a, a + 5.0)])
                   for cid, tpl, (a, _b) in
                   [(c, t, s[0]) for c, t, s in CANDIDATE_SPANS]],
    "exception_sector": {"intro": None, "recap": None, "teaser": None,
                         "credit": None, "end": None},
}, ensure_ascii=False)

FLAGS_JSON = '{"seam_jump": false, "hook_weak": false, "evidence_sec": []}'


# ── 가짜 Gemini ─────────────────────────────────────────────────────────────

class _ApiError(Exception):
    """google-genai `APIError` 흉내 — `.code` 에 정수 상태(SDK 실측 · video._status_of)."""

    def __init__(self, code: int, message: str = "boom"):
        super().__init__(f"{code} {message}")
        self.code = code


def _response(text: str):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1000, thoughts_token_count=10,
                                       candidates_token_count=50,
                                       cached_content_token_count=0,
                                       total_token_count=1060),
        model_version=MODEL,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))])


def _kind(prompt: str) -> str:
    """프롬프트 첫 문장으로 어느 단계의 호출인지 — 세 프롬프트가 각자 다른 말로 시작한다."""
    if prompt.startswith("당신은 쇼츠 편집 후보를 고르는"):
        return "candidates"
    if prompt.startswith("당신은 방송 편집 검수자다"):
        return "boundary"
    if prompt.startswith("첨부한 영상은 쇼츠 후보의 조각들을"):
        return "flags"
    return "unknown"


class FakeGemini:
    """진짜 `call_video` 를 통과시키는 가짜. `types` 는 **진짜 SDK** 다."""

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
        parts = contents[:-1]
        kind = _kind(prompt)
        with self._lock:
            self.calls.append({"kind": kind, "model": model, "parts": len(parts),
                               "prompt": prompt})
        out = self._router(kind, prompt, len(parts))
        if isinstance(out, BaseException):
            raise out
        return _response(out)

    def kinds(self) -> list[str]:
        with self._lock:
            return [c["kind"] for c in self.calls]

    def count(self, kind: str) -> int:
        return sum(1 for k in self.kinds() if k == kind)


def default_router(cands_json: str = CANDIDATES_JSON):
    def route(kind, prompt, n_parts):
        if kind == "candidates":
            return cands_json
        if kind == "boundary":
            # 어느 창이 잡히든 '이 창에 경계 없음' — 원판정 유지가 계약이다.
            return '{"boundary": "none"}'
        if kind == "flags":
            return FLAGS_JSON
        raise AssertionError(f"모르는 프롬프트로 호출됐다: {prompt[:60]!r}")
    return route


# ── 소재·가짜 앞단 ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def synth(tmp_path_factory) -> Path:
    """합성 소재 200초. 후보 길이 정책(40~120초)이 성립하려면 이만큼은 있어야 한다."""
    d = tmp_path_factory.mktemp("v4m3src")
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
    """whisper 는 이 venv 에 없다 — 격자 재료를 주입한다(시각 정본은 이 단어들이다)."""
    monkeypatch.setattr(v4p, "transcribe_words",
                        lambda audio, dur, **kw: ([dict(w) for w in WORDS], []))
    monkeypatch.setattr(v4p, "retranscribe_gaps",
                        lambda *a, **k: (list(a[1]), {"gaps": 0, "windows": [],
                                                      "recovered_words": 0}))


@pytest.fixture
def fake_proxy(monkeypatch):
    """5단계는 별건 모듈이다(M2 계약 §3) — 시그니처만 맞춘 가짜."""
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
        rec["uploaded"] = rec.get("uploaded", 0) + 1
        return SimpleNamespace(uri="files/fake-uri", name="files/fake"), {
            "uri": "files/fake-uri", "name": "files/fake", "bytes": 10,
            "elapsed_sec": 0.0}

    m.build_proxy = build_proxy
    m.upload_handle = upload_handle
    m.handle_alive = lambda gemini, ref: rec["alive"]
    # ⚠ `sys.modules` 만 갈아끼우면 부족하다 — 다른 테스트가 이미 `app.v4.proxy` 를
    #   임포트했으면 `from app.v4 import proxy` 는 **패키지 속성**을 집어 진짜 모듈이
    #   온다(전체 스위트에서만 나는 실패다). 둘 다 건다.
    import app.v4 as v4pkg
    monkeypatch.setitem(sys.modules, "app.v4.proxy", m)
    monkeypatch.setattr(v4pkg, "proxy", m, raising=False)
    return rec


@pytest.fixture
def gem(monkeypatch):
    """진행 중 테스트가 갈아끼울 수 있는 가짜 클라이언트 홀더."""
    holder: dict = {"router": default_router()}
    client = FakeGemini(lambda *a: holder["router"](*a))
    holder["client"] = client
    monkeypatch.setattr(v4p, "_load_gemini_client", lambda: client)
    return holder


@pytest.fixture
def no_boundary_calls(monkeypatch):
    """6b 를 기록기로 갈아끼운다 — 이 파일의 관심은 **배선**이고, 프로브 로직 자체는
    `tests/test_v4_boundary.py` 가 값으로 고정한다. 정정본이 7단계에 전달되는지를
    보려면 결정적으로 '움직인' 결과가 필요하다."""
    rec: dict = {"calls": 0, "seen": []}

    def fake(gemini, handle, *, exception_sector, grid, duration_sec, log=print, **kw):
        rec["calls"] += 1
        rec["seen"].append(exception_sector)
        corrected = {**exception_sector,
                     "teaser": {"start_sec": 190.0, "end_sec": SRC_SEC}}
        return corrected, {"moved": 1, "flash_calls": 1, "tokens": 100,
                           "stopped": None, "probes": [], "notes": []}

    monkeypatch.setattr(boundary_mod, "run_boundary_probe", fake)
    return rec


# ── 읽기 도우미 ─────────────────────────────────────────────────────────────

def _run_log(out: Path) -> dict:
    return json.loads((out / "run_log.json").read_text(encoding="utf-8"))


def _steps(out: Path, name: str) -> list[dict]:
    return [s for s in _run_log(out)["steps"] if s["step"] == name]


def _entry(out: Path, name: str) -> dict:
    got = _steps(out, name)
    assert got, f"{name} 단계 기록이 없다: {[s['step'] for s in _run_log(out)['steps']]}"
    return got[-1]


def _doc(out: Path) -> dict:
    return json.loads((out / v4p.CANDIDATES_CKPT).read_text(encoding="utf-8"))


def _meta(out: Path) -> dict:
    p = out / v4p.CANDIDATES_CKPT
    return json.loads(v4p._meta_path(p).read_text(encoding="utf-8"))


def _run(synth, outdir, **kw):
    kw.setdefault("skip_research", True)
    kw.setdefault("log", lambda *a: None)
    return v4p.run_v4(video_path=synth, work_title="합성", outdir=outdir, **kw)


# ═══════════════════════════════════════════════════════════════════════════
# ① 여섯 단계가 한 파일에 순서대로 쌓인다
# ═══════════════════════════════════════════════════════════════════════════

def test_six_to_nine_stack_sections_in_order(synth, tmp_path, fake_transcribe,
                                             fake_proxy, gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o")

    doc = _doc(out)
    assert doc["schema"] == cand_mod.SCHEMA_CANDIDATES
    # 절이 **쌓인 순서**가 곧 단계 순서다 — 뒤 단계가 앞 절을 덮으면(다시 쓰면) 그 절이
    # 목록 끝으로 밀려 여기서 걸린다.
    owned = {k for keys in v4p.STEP_SECTIONS.values() for k in keys} | {"candidates"}
    assert [k for k in doc if k in owned] == [
        "candidates", "boundary", "verify", "funnel", "rank", "flags", "approval"]
    assert len(doc["candidates"]) == len(CANDIDATE_SPANS)
    assert doc["sample_fps"] == 4.0 and doc["source_duration_sec"] > 199.0

    names = [s["step"] for s in _run_log(out)["steps"]]
    for a, b in zip(["candidates", "boundary", "verify", "funnel", "flags", "approve"],
                    ["boundary", "verify", "funnel", "flags", "approve", "flesh"]):
        assert names.index(a) < names.index(b), f"{a} 가 {b} 뒤에 돌았다: {names}"

    # LLM 콜은 6(1회) + 8(후보 수) 뿐이다 — 6b 는 기록기로 갈아끼웠고 6c·7·9 는 0콜이다.
    assert gem["client"].count("candidates") == 1
    assert gem["client"].count("flags") == len(doc["funnel"]["kept"])
    assert gem["client"].count("boundary") == 0 and no_boundary_calls["calls"] == 1

    # 9단계가 실제로 승인했다 — 배선이 끝까지 흘렀다는 뜻이다.
    assert doc["approval"]["approved"], doc["approval"]
    assert doc["approval"]["no_publishable"] is False
    assert doc["approval"]["fallback"] is False
    # 승인 순서 = 순위 순서(파일 번호의 근거)
    ranked = [r["id"] for r in doc["rank"]]
    assert doc["approval"]["approved"] == [c for c in ranked
                                           if c in doc["approval"]["approved"]]


def test_verify_and_funnel_run_without_any_llm_call(synth, tmp_path, fake_transcribe,
                                                    fake_proxy, gem, no_boundary_calls):
    """6c·7·9 는 **이미 있는 순수 함수를 부르기만** 한다(계약 §5)."""
    out = _run(synth, tmp_path / "o")
    doc = _doc(out)

    v = doc["verify"]
    assert v["kept"] and not v["dropped"], v
    assert set(v) >= {"results", "kept", "dropped", "relocated", "clamped",
                      "candidates"}
    f = doc["funnel"]
    assert f["kept"] and f["of"] == len(CANDIDATE_SPANS)
    # 모든 후보는 kept 나 dropped 중 정확히 한 곳에 한 번 나온다(조용한 증발 금지)
    assert set(f["kept"]) | {d["id"] for d in f["dropped"]} == \
        {c[0] for c in CANDIDATE_SPANS}
    # 6c·7 은 0콜이다 — 이 두 단계의 기록에는 usage 도 audit 도 없다(LLM 을 안 부른다)
    for name in ("verify", "funnel"):
        entry = _entry(out, name)
        assert entry["elapsed"] >= 0 and entry["fingerprint"]
        assert "usage" not in entry and "audit" not in entry, entry


def test_step_six_records_the_measured_transcript_against_the_probe_estimate(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, no_boundary_calls):
    """계약 §5 — 4단계 추정치와 6단계 실측 블록 길이를 나란히 남긴다(M8 재료)."""
    out = _run(synth, tmp_path / "o", stop_after="candidates")
    check = _entry(out, "candidates")["text_tokens_check"]

    assert check["estimated_at_probe"] == _entry(out, "probe")["text_tokens"]
    assert check["transcript_chars"] == len(cand_mod.transcript_block(
        json.loads((out / "grid.json").read_text(encoding="utf-8"))))
    assert check["transcript_lines"] == len(WORDS)
    assert check["tokens_per_char"] == v4p.TEXT_TOKENS_PER_CHAR


# ═══════════════════════════════════════════════════════════════════════════
# ② 재개 — 각 절이 캐시로 읽힌다
# ═══════════════════════════════════════════════════════════════════════════

def test_resume_reads_every_section_from_cache(synth, tmp_path, fake_transcribe,
                                               fake_proxy, gem, no_boundary_calls):
    o = tmp_path / "o"
    out = _run(synth, o)
    before = list(gem["client"].kinds())
    boundary_before = no_boundary_calls["calls"]

    _run(synth, o, job_id=out.name)

    assert gem["client"].kinds() == before, "재개가 LLM 을 다시 불렀다(요금이 샌다)"
    assert no_boundary_calls["calls"] == boundary_before
    for name in ("candidates", "boundary", "verify", "funnel", "flags", "approve"):
        entry = _entry(out, name)
        assert entry.get("cached") is True, (name, entry)
        assert entry["cache_reason"] == "지문 일치", (name, entry)


def test_from_step_flags_rebuilds_only_flags_and_downstream(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, no_boundary_calls):
    o = tmp_path / "o"
    out = _run(synth, o)
    n_flags = gem["client"].count("flags")
    approval_before = _doc(out)["approval"]

    _run(synth, o, job_id=out.name, from_step="flags")

    # 상류 넷은 캐시, 8단계만 다시 채점된다
    for name in ("candidates", "boundary", "verify", "funnel"):
        assert _entry(out, name)["cached"] is True, name
    assert gem["client"].count("candidates") == 1
    assert gem["client"].count("flags") == n_flags * 2
    assert _entry(out, "flags")["cache_reason"] == "--from-step 무효화"
    assert _entry(out, "flags")["reused"] == 0
    # 하류(9)는 폐기 후 다시 만들어진다 — 값은 같지만 '캐시였다'가 아니다
    assert _entry(out, "flags")["purged"] == ["approval"]
    assert _entry(out, "approve").get("cached") is not True
    assert _doc(out)["approval"]["approved"] == approval_before["approved"]


def test_rerunning_step_six_purges_every_downstream_section(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, no_boundary_calls):
    """6단계는 파일의 몸통을 새로 쓴다 — 옛 후보의 승인 결과가 남아 있으면 안 된다."""
    o = tmp_path / "o"
    out = _run(synth, o, stop_after="approve")
    assert set(_doc(out)) >= {"verify", "funnel", "rank", "flags", "approval"}

    _run(synth, o, job_id=out.name, from_step="candidates", stop_after="candidates")

    doc = _doc(out)
    assert set(doc) & {"boundary", "verify", "funnel", "rank", "flags",
                       "approval"} == set(), doc
    assert set(_meta(out)["sections"]) == {"candidates"}


# ═══════════════════════════════════════════════════════════════════════════
# ③ 8단계 — 후보 단위 증분
# ═══════════════════════════════════════════════════════════════════════════

def _fail_two_part_candidate():
    """조각 2개짜리 후보(c05)만 permanent 400 으로 죽인다 — 파트 수로 식별한다."""
    base = default_router()

    def route(kind, prompt, n_parts):
        if kind == "flags" and n_parts == 2:
            return _ApiError(400, "합성 permanent 실패")
        return base(kind, prompt, n_parts)
    return route


def test_one_failed_candidate_does_not_erase_the_other_scores(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, no_boundary_calls):
    gem["router"] = _fail_two_part_candidate()
    out = _run(synth, tmp_path / "o")

    flags = _doc(out)["flags"]
    ok = {cid for cid, e in flags.items() if e["status"] == approve_mod.FLAGS_STATUS_OK}
    bad = {cid for cid, e in flags.items() if e["status"] != approve_mod.FLAGS_STATUS_OK}
    assert bad == {"c05"} and len(ok) >= 3, flags
    assert flags["c05"]["reason"] == flags_mod.REASON_CALL_FAILED
    # 미채점은 **'모른다'** 지 0점이 아니다 — 9단계가 그 어휘로 탈락시킨다
    rejected = {r["id"]: r["reasons"] for r in _doc(out)["approval"]["rejected"]}
    assert approve_mod.REASON_UNSCORED in rejected["c05"], rejected
    assert set(_doc(out)["approval"]["approved"]) == ok & set(
        _doc(out)["funnel"]["kept"])
    # 저장 순서는 id 순(결정성)
    assert list(flags) == sorted(flags)


def test_failed_entry_is_rescored_on_resume_but_the_scored_ones_are_not(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, no_boundary_calls):
    o = tmp_path / "o"
    gem["router"] = _fail_two_part_candidate()
    out = _run(synth, o)
    n_first = gem["client"].count("flags")
    assert n_first >= 4

    gem["router"] = default_router()          # 이번엔 그 후보도 답한다
    _run(synth, o, job_id=out.name)

    # 재개는 **미채점 하나만** 다시 묻는다(채점된 것은 재사용 — 계약 §5 증분)
    assert gem["client"].count("flags") == n_first + 1
    entry = _entry(out, "flags")
    assert entry["reused"] == n_first - 1 and entry["recomputed"] == 1
    assert _doc(out)["flags"]["c05"]["status"] == approve_mod.FLAGS_STATUS_OK
    assert "c05" in _doc(out)["approval"]["approved"]


def test_flag_fingerprints_are_per_candidate(synth, tmp_path, fake_transcribe,
                                             fake_proxy, gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o")
    per_id = _meta(out)["sections"]["flags"]["per_id"]

    assert set(per_id) == set(_doc(out)["funnel"]["kept"])
    assert len(set(per_id.values())) == len(per_id), "후보마다 다른 지문이어야 한다"


# ═══════════════════════════════════════════════════════════════════════════
# ④ 6b — 정정본이 7단계가 보는 값이다
# ═══════════════════════════════════════════════════════════════════════════

def test_boundary_correction_is_what_the_funnel_sees(synth, tmp_path, fake_transcribe,
                                                     fake_proxy, gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o")
    doc = _doc(out)

    # 6b 는 자기 절에 정정본을 두고 **6단계의 신고를 덮지 않는다**(재프로브 기준 보존)
    assert doc["exception_sectors"]["teaser"] is None
    assert doc["boundary"]["exception_sectors"]["teaser"]["start_sec"] == 190.0
    assert doc["boundary"]["before"]["teaser"] is None
    assert doc["funnel"]["sectors_from"] == "boundary"
    # 정정된 teaser(190~200)와 겹치는 c04(145~190)는 경계가 맞닿을 뿐이라 살아남는다.
    # 그 사실이 아니라 **7단계가 정정본을 봤다**는 것이 이 테스트의 관심이다.
    assert no_boundary_calls["seen"][0] == doc["exception_sectors"]


def test_real_boundary_probe_keeps_the_verdict_when_the_model_says_none(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, monkeypatch):
    """진짜 6b 를 태운다 — 모델이 'none' 이면 원판정 유지(오염 방지 비대칭).

    ⚠ 장면 전환을 주입한다. 합성 소재(testsrc2)는 장면 전환이 **0개**라(실측) 그대로
    두면 `scene_cut_candidates` 가 비어 프로브가 모델을 아예 안 부른다 — 그러면 이
    테스트는 '경계가 안 움직였다'만 보고 정작 호출 경로를 안 지난다.
    """
    monkeypatch.setattr(v4p, "detect_scene_cuts",
                        lambda video, threshold=0.3: [12.0, 33.0, 77.0, 150.0])
    out = _run(synth, tmp_path / "o", stop_after="boundary")
    doc = _doc(out)

    assert gem["client"].count("boundary") >= 1, "프로브가 모델을 부르지 않았다"
    assert doc["boundary"]["moved"] == 0
    assert doc["boundary"]["exception_sectors"] == doc["exception_sectors"]
    assert _entry(out, "boundary")["moved"] == 0
    assert _entry(out, "boundary")["flash_calls"] == gem["client"].count("boundary")


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ 조용한 결번 금지
# ═══════════════════════════════════════════════════════════════════════════

def test_no_publishable_fails_loudly_and_leaves_the_audit(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, no_boundary_calls):
    o = tmp_path / "o"
    gem["router"] = default_router(TINY_JSON)

    with pytest.raises(ValueError, match="조용한 결번 금지"):
        _run(synth, o)

    out = next(p for p in o.iterdir() if p.is_dir())
    # 크게 죽었어도 감사 기록은 디스크에 있다(단계마다 원자적 기록)
    assert _entry(out, "verify")["kept"] == 0
    assert _entry(out, "approve")["no_publishable"] is True
    assert _entry(out, "error")["at"] == "approve"
    assert _doc(out)["approval"]["approved"] == []
    # 8단계는 채점할 후보가 없었다 — 콜도 없다
    assert gem["client"].count("flags") == 0


# ═══════════════════════════════════════════════════════════════════════════
# ⑥ --stop-after · 미구현 단계 · AST 가드
# ═══════════════════════════════════════════════════════════════════════════

def test_stop_after_candidates_stops_before_the_probe_call(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, no_boundary_calls):
    out = _run(synth, tmp_path / "o", stop_after="candidates")

    assert gem["client"].count("candidates") == 1
    assert no_boundary_calls["calls"] == 0
    doc = _doc(out)
    assert "candidates" in doc and "boundary" not in doc
    stop = _entry(out, "boundary")
    assert stop["skipped"] == "--stop-after candidates"
    assert stop["remaining"][0] == "boundary" and stop["remaining"][-1] == "11:validate"


def test_pipeline_ends_cleanly_at_the_first_unbuilt_step(
        synth, tmp_path, fake_transcribe, fake_proxy, gem, no_boundary_calls):
    """9단계까지 흐르고 **10(살붙이기)** 에서 이름과 마일스톤을 남기고 정상 종료한다."""
    out = _run(synth, tmp_path / "o")

    last = _run_log(out)["steps"][-1]
    assert last["step"] == "flesh" and last["not_implemented"] == "M5"
    assert last["remaining"][0] == "flesh" and last["remaining"][-1] == "11:validate"
    assert out.is_dir()


def test_step_tables_stay_in_sync():
    """배선 표·미구현 표가 단계 표를 정확히 덮는가(임포트 시점 검사의 재확인)."""
    from app.v4.steps import V4_STEPS

    assert v4p.IMPLEMENTED_STEPS.isdisjoint(v4p.NOT_IMPLEMENTED_MILESTONE)
    assert set(V4_STEPS) == v4p.IMPLEMENTED_STEPS | set(v4p.NOT_IMPLEMENTED_MILESTONE)
    # `STEP_SECTIONS` 는 6~9 를 정확히 덮는다(누가 무엇을 쓰는가의 정본)
    assert set(v4p.STEP_SECTIONS) == {"candidates", "boundary", "verify", "funnel",
                                      "flags", "approve"}
    assert v4p._purge_after("candidates") == ("approval", "boundary", "flags",
                                              "funnel", "rank", "verify")
    assert v4p._purge_after("approve") == ()


def test_ast_guards_still_pass_on_the_extended_wiring():
    """M3 배선이 v3 gotcha 4 를 재발시키지 않았는가 — 가드의 탐지기를 그대로 부른다."""
    from test_v4_guards import _v4_sources, from_step_comparisons, imported_modules

    src = Path(v4p.__file__).read_text(encoding="utf-8")
    assert from_step_comparisons(src) == [], "손으로 적은 from_step 판정이 생겼다"
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert code.count("should_run(") == 1, "should_run 호출이 흩어졌다"
    assert _v4_sources(), "가드가 훑을 파일 목록이 비었다"
    forbidden = {"app.pipeline", "app.v3.pipeline"}
    got = {name for name, _ in imported_modules(src, package="app.v4")}
    assert not (got & forbidden), got & forbidden


# ═══════════════════════════════════════════════════════════════════════════
# ⑦ 순수 변환기 — 안 옮기면 조용히 틀리는 것들
# ═══════════════════════════════════════════════════════════════════════════

def test_words_for_funnel_translates_the_time_keys():
    """🛑 `t0` 를 그대로 넘기면 funnel 이 전 단어를 **0.0 초**로 읽는다(예외도 안 난다)."""
    got = v4p.words_for_funnel({"words": [{"t0": 12.5, "t1": 13.0, "text": "가"}]})
    assert got == [{"start_sec": 12.5, "end_sec": 13.0}]
    with pytest.raises(ValueError, match="숫자가 아니다"):
        v4p.words_for_funnel({"words": [{"t0": "열둘", "t1": 13.0}]})


def test_speech_segments_come_from_the_grid_cues_not_a_new_grouping():
    grid = {"span_candidates": [
        {"t_in": 1.0, "t_out": 3.0, "is_audio": True, "text": "대사 한 줄"},
        {"t_in": 3.0, "t_out": 5.0, "is_audio": False, "text": ""},
        {"t_in": 5.0, "t_out": 7.0, "is_audio": True, "text": "  "}]}
    assert v4p.speech_segments_from_grid(grid) == [
        {"start_sec": 1.0, "end_sec": 3.0, "text": "대사 한 줄"}]


def test_build_ranking_keeps_the_funnel_order_and_refuses_to_invent_scores():
    rec = {"kept": ["c02", "c01"], "scores": {"c01": 1.5, "c02": 0.5},
           "signals": {"c01": {"a": 1}, "c02": {"a": 2}}}
    assert v4p.build_ranking(rec) == [
        {"id": "c02", "score": 0.5, "signals": {"a": 2}},
        {"id": "c01", "score": 1.5, "signals": {"a": 1}}]
    with pytest.raises(ValueError, match="점수가 없다"):
        v4p.build_ranking({"kept": ["c09"], "scores": {}, "signals": {}})
