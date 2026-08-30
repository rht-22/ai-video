"""V3-M1 Stage 1 회귀 가드 — 정규화·스냅·커버리지·반려 루프·파이프라인 배선.

LLM 없이 돈다(가짜 응답 주입). 고정하는 계약: ① 모든 확정 경계는 격자 눈금
② 스냅 실패(>2s)·커버리지 위반은 반려·재질의(≤2회) 후 크게 실패 ③ sequence 당
chunk ≥1 ④ 기획서 §3 스키마 그대로(HH:MM:SS.mmm · meanings 예약 · exception 5키).
"""
from __future__ import annotations

import json

import pytest

from app.v3 import schemas
from app.v3 import seq_analyze as s1

DUR = 1000.0
GRID = [0.0, 100.0, 250.0, 400.0, 550.0, 700.0, 850.0, 950.0, 1000.0]


def _resp(seqs, exc=None):
    def rng(a, b):
        return {"start": schemas.format_ts(a), "end": schemas.format_ts(b)}
    return {
        "sequences": [
            {"number": i, "time": rng(a, b), "content": f"내용{i}",
             "chunks": [{"number": j, "time": rng(ca, cb)}
                        for j, (ca, cb) in enumerate(chunks)]}
            for i, (a, b, chunks) in enumerate(seqs)],
        "exception_sector": {**{k: None for k in schemas.EXCEPTION_KEYS},
                             **{k: rng(a, b) for k, (a, b) in (exc or {}).items()}},
    }


# ── 정규화·커버리지(순수) ───────────────────────────────────────────────────

def test_normalize_rejects_structural_errors():
    with pytest.raises(ValueError):
        schemas.normalize_stage1_response({"sequences": []})
    with pytest.raises(ValueError):
        schemas.normalize_stage1_response({"sequences": [{"time": {
            "start": "00:00:10.000", "end": "00:00:05.000"}}],
            "exception_sector": {}})                       # 구간 역전
    bad = _resp([(0, 100, [(0, 100)])])
    bad["exception_sector"]["sponsor"] = {"start": "00:00:00.000", "end": "00:00:01.000"}
    with pytest.raises(ValueError):
        schemas.normalize_stage1_response(bad)             # 모르는 exception 키


def test_coverage_gap_overlap_and_chunk_rules():
    norm = schemas.normalize_stage1_response(_resp(
        [(0, 400, [(0, 400)]), (450, 1000, [(450, 1000)])]))   # 400~450 빈틈
    problems = schemas.validate_coverage(norm, DUR)
    assert any("빈틈" in p for p in problems)

    norm = schemas.normalize_stage1_response(_resp(
        [(0, 500, [(0, 500)]), (450, 1000, [(450, 1000)])]))   # 겹침
    assert any("겹침" in p for p in schemas.validate_coverage(norm, DUR))

    norm = schemas.normalize_stage1_response(_resp([(0, 1000, [(0, 1000)])]))
    assert any("10분 상한" in p for p in schemas.validate_coverage(norm, DUR))

    ok = schemas.normalize_stage1_response(_resp(
        [(0, 550, [(0, 400), (400, 550)]), (550, 1000, [(550, 1000)])]))
    assert schemas.validate_coverage(ok, DUR) == []


def test_coverage_with_exceptions_tiling():
    norm = schemas.normalize_stage1_response(_resp(
        [(100, 550, [(100, 550)]), (550, 950, [(550, 950)])],
        exc={"intro": (0, 100), "credit": (950, 1000)}))
    assert schemas.validate_coverage(norm, DUR) == []


# ── 스냅 ────────────────────────────────────────────────────────────────────

def test_snap_stage1_joint_boundaries():
    """인접 구간이 0.8s 어긋나게 낸 공유 경계가 한 눈금으로 정준화-스냅되는지."""
    norm = schemas.normalize_stage1_response(_resp(
        [(0, 549.2, [(0, 249.4), (249.4, 549.2)]), (550.0, 1000, [(550.0, 1000)])]))
    snapped, failures = s1.snap_stage1(norm, GRID, DUR)
    assert failures == []
    assert snapped["sequences"][0]["end"] == 550.0
    assert snapped["sequences"][1]["start"] == 550.0       # 빈틈 소멸
    assert snapped["sequences"][0]["chunks"][0]["end"] == 250.0
    assert schemas.validate_coverage(snapped, DUR) == []


def test_snap_stage1_failure_over_tolerance():
    norm = schemas.normalize_stage1_response(_resp(
        [(0, 300, [(0, 300)]), (300, 1000, [(300, 1000)])]))   # 300 은 눈금과 50s 거리
    _snapped, failures = s1.snap_stage1(norm, GRID, DUR)
    assert failures and failures[0]["nearest_err"] > schemas.SNAP_TOLERANCE_SEC


def test_normalize_chunks_fill_short_sequences():
    # 두 sequence 다 600s 이하 — chunk 부재는 반려가 아니라 자동 채움(기획서 §3 명문)
    norm = schemas.normalize_stage1_response(_resp([(0, 550, []), (550, 1000, [])]))
    notes, problems = s1.normalize_chunks(norm, GRID, allow_fallback=False)
    assert problems == []
    assert len(notes) == 2 and all("자동 채움" in n for n in notes)
    assert norm["sequences"][0]["chunks"] == [{"start": 0, "end": 550}]
    assert norm["sequences"][1]["chunks"] == [{"start": 550, "end": 1000}]


def test_normalize_chunks_long_sequence():
    norm = schemas.normalize_stage1_response(_resp([(0, 1000, [])]))
    notes, problems = s1.normalize_chunks(norm, GRID, allow_fallback=False)
    assert problems and "chunk 분할" in problems[0]         # >600s + chunk 부재 → 반려
    notes, problems = s1.normalize_chunks(norm, GRID, allow_fallback=True)
    assert not problems and norm["sequences"][0]["chunks"]
    assert all(c["end"] - c["start"] <= schemas.CHUNK_MAX_SEC + 0.05
               for c in norm["sequences"][0]["chunks"])
    assert any("폴백" in n for n in notes)
    # 폴백 경계도 격자 눈금이어야 한다
    for c in norm["sequences"][0]["chunks"][:-1]:
        assert c["end"] in GRID


def test_to_stage1_doc_schema_shape():
    norm = schemas.normalize_stage1_response(_resp(
        [(100, 550, [(100, 550)])], exc={"intro": (0, 100)}))
    norm["sequences"][0]["chunks"] = [{"start": 100.0, "end": 550.0}]
    doc = schemas.to_stage1_doc(norm)
    assert doc["schema"] == schemas.SCHEMA_STAGE1
    sq = doc["sequences"][0]
    assert sq["number"] == 0 and sq["time"]["start"] == "00:01:40.000"
    assert sq["chunks"][0]["meanings"] == []               # Stage 2 예약
    assert set(doc["exception_sector"]) == set(schemas.EXCEPTION_KEYS)
    assert doc["exception_sector"]["teaser"] is None
    assert doc["exception_sector"]["intro"]["end"] == "00:01:40.000"


# ── 반려 루프(가짜 모델) ────────────────────────────────────────────────────

class _FakeGemini:
    class config:
        model_name = "gemini-3.1-pro-preview"
        analysis_thinking_level = "high"
        max_retries = 3


def _fake_grid():
    spans = [{"id": f"sp{i:04d}", "t_in": float(a), "t_out": float(b),
              "is_audio": bool(i % 2), "time_authority": "stt" if i % 2 else "scene",
              "text": "말" if i % 2 else ""}
             for i, (a, b) in enumerate([(0, 100), (100, 250), (250, 400),
                                         (400, 550), (550, 700), (700, 850),
                                         (850, 950), (950, 1000)])]
    return {
        "schema": "v3_grid/v1",
        "source": {"path": "x.mp4", "duration_sec": DUR, "fps": 24.0,
                   "width": 1920, "height": 1080},
        "transcript": {"backend": "whisper", "model": "m", "word_count": 4,
                       "failed_windows": [], "srt_provided": False},
        "words": [], "scene_cuts": [100.0, 250.0, 400.0, 550.0, 700.0, 850.0, 950.0],
        "silence": [], "arousal": [], "span_candidates": spans,
    }


def test_reask_loop_then_success(monkeypatch):
    grid = _fake_grid()
    prompts: list[str] = []
    responses = [
        _resp([(0, 400, [(0, 400)]), (500, 1000, [(500, 1000)])]),   # 빈틈 100s → 반려
        _resp([(0, 550, [(0, 550)]), (550, 1000, [(550, 1000)])]),   # 정상
    ]
    monkeypatch.setattr(s1, "_upload_video",
                        lambda g, p, log=print: type("U", (), {"name": "f", "uri": "u"})())
    def fake_call(gemini, uploaded, prompt):
        prompts.append(prompt)
        return responses[len(prompts) - 1]
    monkeypatch.setattr(s1, "_call_model", fake_call)

    class _Files:
        def delete(self, name):
            pass
    _FakeGemini.client = type("C", (), {"files": _Files()})()

    doc, audit = s1.run_seq_analyze(_FakeGemini(), None, grid)
    assert len(audit["attempts"]) == 2
    assert audit["attempts"][0]["problems"]
    assert "반려 사유" in prompts[1]                        # 재질의에 사유 첨부
    assert doc["sequences"][0]["time"]["end"] == "00:09:10.000"


def test_reask_exhaustion_raises(monkeypatch):
    grid = _fake_grid()
    bad = _resp([(0, 400, [(0, 400)]), (500, 1000, [(500, 1000)])])
    monkeypatch.setattr(s1, "_upload_video",
                        lambda g, p, log=print: type("U", (), {"name": "f", "uri": "u"})())
    monkeypatch.setattr(s1, "_call_model", lambda g, u, p: json.loads(json.dumps(bad)))

    class _Files:
        def delete(self, name):
            pass
    _FakeGemini.client = type("C", (), {"files": _Files()})()
    with pytest.raises(RuntimeError, match="반려"):
        s1.run_seq_analyze(_FakeGemini(), None, grid)


def test_prompt_contains_grid_vocab_and_hints():
    grid = _fake_grid()
    hints = {"intro": {"start": 0.0, "end": 100.0}, "method": "srt", "confidence": 0.7}
    p = s1.build_prompt(grid, research_context="장르: 드라마", hints=hints)
    assert "00:16:40.000" in p                             # 러닝타임
    assert "100.0" in p and "550.0" in p                   # 장면 전환 어휘
    assert "intro 후보" in p and "장르: 드라마" in p
    assert "HH:MM:SS.mmm" in p


def test_hint_mismatch():
    hints = {"intro": {"start": 0.0, "end": 100.0}, "method": "srt", "confidence": 0.7}
    final = {"intro": {"start": 0.0, "end": 250.0}, "recap": None, "teaser": None,
             "credit": None, "end": None}
    m = s1.hint_mismatch(hints, final)
    assert len(m) == 1 and m[0]["key"] == "intro" and m[0]["kind"] == "경계 불일치"
    assert s1.hint_mismatch({}, {k: None for k in schemas.EXCEPTION_KEYS}) == []


# ── 파이프라인 배선(네트워크·whisper 없이) ──────────────────────────────────

def test_pipeline_grid_smoke(tmp_path, monkeypatch):
    """5초 합성 소재로 job 레이아웃·run_log·grid 산출 배선 확인(전사는 주입)."""
    import subprocess

    from app.modules.ffmpeg_utils import find_ffmpeg_command
    from app.v3 import pipeline as v3p

    src = tmp_path / "src.mp4"
    ffmpeg = find_ffmpeg_command("ffmpeg")
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=12:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)

    words = [{"t0": 1.0, "t1": 1.4, "text": "테스트", "prob": 0.9}]
    monkeypatch.setattr(v3p, "transcribe_words", lambda *a, **k: (words, [(3.0, 4.0)]))

    out = v3p.run_v3(video_path=src, work_title="합성 소재", outdir=tmp_path / "out",
                     skip_research=True, skip_seq_analyze=True, log=lambda *a: None)
    grid = json.loads((out / "grid.json").read_text(encoding="utf-8"))
    assert grid["schema"] == "v3_grid/v1"
    assert grid["words"] == words
    assert grid["transcript"]["failed_windows"] == [[3.0, 4.0]]
    assert grid["span_candidates"], "span 이 나와야 한다"
    rl = json.loads((out / "run_log.json").read_text(encoding="utf-8"))
    assert rl["pipeline"] == "v3_m1"
    steps = {s["step"] for s in rl["steps"]}
    assert {"probe", "proxy", "grid", "character_index"} <= steps
    ci = next(s for s in rl["steps"] if s["step"] == "character_index")
    assert ci["status"] == "module_absent"                 # 부재가 명시된다(조용한 누락 금지)
    assert (out / "checkpoint_probe.json").exists()
    assert (out / "checkpoint_grid_words.json").exists()
    assert (out / "run_log.json").exists()
    # 재실행(캐시) — grid 바이트 동일(결정성)
    g1 = (out / "grid.json").read_bytes()
    v3p.run_v3(video_path=src, work_title="합성 소재", outdir=tmp_path / "out",
               job_id=out.name, skip_research=True, skip_seq_analyze=True,
               log=lambda *a: None)
    assert (out / "grid.json").read_bytes() == g1
