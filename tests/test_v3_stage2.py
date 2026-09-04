"""V3-M2 회귀 가드 — chunk_split · Stage 2(chunk_analyze).

LLM 없이 돈다(가짜 응답 주입 · ffmpeg 는 합성 5초 소재만). 고정하는 계약:
① 모델은 시각을 출력하지 않는다 — meaning 은 span id 구간, 확정 시각은 grid lookup
② chunk ∩ exception = 0(물리 제거) · 10분 상한 ③ audio_script 는 전사가 정본 —
각색은 복원 ④ 인물 교차는 경고 집계(차단 아님) ⑤ 부분 실패는 커버리지 표기.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.v3 import chunk_analyze as ca
from app.v3 import chunk_split as cs
from app.v3 import schemas


def _stage1(chunks_by_seq, exc=None, dur=1000.0):
    def rng(a, b):
        return {"start": schemas.format_ts(a), "end": schemas.format_ts(b)}
    seqs = []
    for i, (a, b, chunks) in enumerate(chunks_by_seq):
        seqs.append({"number": i, "time": rng(a, b), "content": f"내용{i}",
                     "chunks": [{"number": j, "time": rng(ca_, cb), "meanings": []}
                                for j, (ca_, cb) in enumerate(chunks)]})
    return {"schema": "v3_stage1/v1", "sequences": seqs,
            "exception_sector": {**{k: None for k in schemas.EXCEPTION_KEYS},
                                 **{k: rng(a, b) for k, (a, b) in (exc or {}).items()}}}


def _grid_spans(spans):
    return {"schema": "v3_grid/v1",
            "source": {"path": "x.mp4", "duration_sec": 1000.0, "fps": 24.0,
                       "width": 1920, "height": 1080},
            "span_candidates": [
                {"id": f"sp{i:04d}", "t_in": float(a), "t_out": float(b),
                 "is_audio": bool(voiced), "time_authority": "stt" if voiced else "scene",
                 "text": text}
                for i, (a, b, voiced, text) in enumerate(spans)],
            "scene_cuts": [], "words": [], "silence": [], "arousal": []}


# ── chunk_split ─────────────────────────────────────────────────────────────

def test_plan_chunks_sorted_and_capped():
    doc = _stage1([(43, 600, [(43, 600)]), (600, 1000, [(600, 950)])],
                  exc={"intro": (0, 43), "credit": (950, 1000)})
    chunks, exceptions = cs.plan_chunks(doc)
    assert [(c["seq_number"], c["chunk_number"]) for c in chunks] == [(0, 0), (1, 0)]
    assert chunks[0]["start_sec"] == 43.0
    assert {x["key"] for x in exceptions} == {"intro", "credit"}
    bad = _stage1([(0, 700, [(0, 700)])])
    with pytest.raises(ValueError, match="상한"):
        cs.plan_chunks(bad)


def test_exception_overlap_guard():
    chunks = [{"seq_number": 0, "chunk_number": 0, "start_sec": 0.0, "end_sec": 100.0}]
    with pytest.raises(ValueError, match="exception 유입"):
        cs.assert_no_exception_overlap(
            chunks, [{"key": "recap", "start_sec": 50.0, "end_sec": 60.0}])
    cs.assert_no_exception_overlap(
        chunks, [{"key": "credit", "start_sec": 100.0, "end_sec": 110.0}])  # 접경은 통과


def test_exception_labels_never_enter_chunks():
    """실측 레이블 7편 재사용 — 레이블의 exception 을 뺀 타일링이면 유입 0."""
    label_dir = Path(__file__).parent / "data" / "v3_exception_labels"
    for f in sorted(label_dir.glob("*.json")):
        lab = json.loads(f.read_text(encoding="utf-8"))
        dur = float(lab["duration_sec"])
        exc = {k: (v["start"], v["end"]) for k, v in lab["labels"].items() if v}
        # exception 을 제외한 나머지를 sequence 로 타일링(600s 단위 chunk)
        cuts = sorted({0.0, dur, *(t for a, b in exc.values() for t in (a, b))})
        seqs = []
        for a, b in zip(cuts, cuts[1:]):
            if any(abs(a - x[0]) < 1e-9 for x in exc.values()):
                continue                                   # exception 구간
            n = int((b - a) // schemas.CHUNK_MAX_SEC) + 1
            bounds = [a + (b - a) * j / n for j in range(n)] + [b]
            seqs.append((a, b, list(zip(bounds, bounds[1:]))))
        if not seqs:
            continue
        doc = _stage1(seqs, exc={k: v for k, v in exc.items()}, dur=dur)
        chunks, exceptions = cs.plan_chunks(doc)
        cs.assert_no_exception_overlap(chunks, exceptions)  # 유입 0 — 예외 없이 통과


def test_split_chunks_real_ffmpeg(tmp_path):
    import subprocess

    from app.modules.ffmpeg_utils import find_ffmpeg_command
    src = tmp_path / "src.mp4"
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=12:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    chunks = [{"seq_number": 0, "chunk_number": 0, "start_sec": 1.0, "end_sec": 3.0},
              {"seq_number": 0, "chunk_number": 1, "start_sec": 3.0, "end_sec": 5.0}]
    man = cs.split_chunks(src, chunks, [], tmp_path / "chunks",
                          only=[(0, 0)], log=lambda *a: None)
    assert man["schema"] == cs.SCHEMA_CHUNK_SPLIT
    assert man["chunks"][0]["file"] == "chunk_s00_c00_1.000-3.000.mp4"
    assert man["chunks"][1]["file"] is None                # only 밖 — 미재단(커버리지)
    assert (tmp_path / "chunks" / man["chunks"][0]["file"]).exists()
    assert man["chunks"][0]["duration_sec"] == 2.0
    assert man["proxy"]["fps"] == 10
    # 경계가 바뀌면 파일명이 달라져 옛 재단본을 재사용하지 않는다(리뷰 high 수정)
    chunks2 = [dict(chunks[0], start_sec=2.0, end_sec=5.0)]
    man2 = cs.split_chunks(src, chunks2, [], tmp_path / "chunks",
                           only=[(0, 0)], log=lambda *a: None)
    assert man2["chunks"][0]["file"] == "chunk_s00_c00_2.000-5.000.mp4"
    assert (tmp_path / "chunks" / man2["chunks"][0]["file"]).exists()


# ── Stage 2 순수 검증 ───────────────────────────────────────────────────────

def _spans4():
    return ca.spans_for_chunk(_grid_spans([
        (0, 4, True, "안녕하세요."), (4, 8, False, ""),
        (8, 12, True, "반갑습니다."), (12, 16, True, "저는 강비오입니다."),
    ]), 0.0, 16.0)


def test_spans_for_chunk_overlap_rule():
    grid = _grid_spans([(0, 10, True, "a"), (9, 20, False, ""), (20, 30, True, "b")])
    got = ca.spans_for_chunk(grid, 0.0, 12.0)
    # (9~20)은 겹침 3/11 < 절반 → 이 chunk 소속 아님
    assert [s["id"] for s in got] == ["sp0000"]
    got2 = ca.spans_for_chunk(grid, 12.0, 30.0)
    assert [s["id"] for s in got2] == ["sp0001", "sp0002"]


def test_edit_ratio():
    assert ca.edit_ratio("나는 천재다", "나는천재다") == 0.0     # 공백 무시
    assert ca.edit_ratio("", "") == 0.0
    assert ca.edit_ratio("완전 다른 말", "") == 1.0
    assert ca.edit_ratio("나는 천재다", "나는 천재야") < 0.35     # 한 글자 정정 통과
    assert ca.edit_ratio("나는 천재다", "오늘 날씨가 좋네요") > 0.35


def _resp_ok():
    return {"meanings": [
        {"first_span": "sp0000", "last_span": "sp0001", "content": "인사",
         "characters": ["강비오"], "importance": 3, "mood": "평온",
         "spans": [
             {"id": "sp0000", "scene_script": "인사 장면", "characters": ["강비오"],
              "importance": 3, "audio": [{"speaker": "강비오", "line": "안녕하세요."}]},
             {"id": "sp0001", "scene_script": "정적", "characters": [], "importance": 2},
         ]},
        {"first_span": "sp0002", "last_span": "sp0003", "content": "자기소개",
         "characters": ["강비오"], "importance": 4, "mood": "긴장",
         "spans": [
             {"id": "sp0002", "scene_script": "미소", "characters": ["강비오"],
              "importance": 3, "audio": [{"speaker": "강비오", "line": "반갑습니다."}]},
             {"id": "sp0003", "scene_script": "소개", "characters": ["강비오"],
              "importance": 5, "audio": [{"speaker": "강비오", "line": "저는 강비오입니다."}]},
         ]},
    ]}


def test_validate_happy_path_and_assemble_alignment():
    spans = _spans4()
    norm, problems, notes = ca.validate_stage2_response(
        _resp_ok(), spans, final_attempt=False)
    assert problems == [] and len(norm) == 2
    meanings = ca.assemble_chunk_meanings(norm, spans)
    assert meanings[0]["time"] == {"start": "00:00:00.000", "end": "00:00:08.000"}
    assert meanings[1]["spans"][0]["span_id"] == "sp0002"
    assert meanings[0]["spans"][1]["audio_script"] == []   # 무성
    ta = ca.verify_time_alignment(meanings, _grid_spans([
        (0, 4, True, "x"), (4, 8, False, ""), (8, 12, True, "y"), (12, 16, True, "z")]))
    assert ta["pct"] == 100.0 and ta["checked"] == 12      # 시각 정합 100%


def test_validate_rejects_unknown_gap_overlap():
    spans = _spans4()
    r = _resp_ok()
    r["meanings"][0]["last_span"] = "sp9999"
    _n, p, _ = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert any("모르는/비문자열 span id" in x for x in p)

    r = _resp_ok()
    r["meanings"][0]["last_span"] = "sp0000"               # sp0001 빈틈
    _n, p, _ = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert any("빈틈" in x for x in p)

    r = _resp_ok()
    r["meanings"][1]["first_span"] = "sp0001"              # 겹침
    _n, p, _ = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert any("겹침" in x for x in p)


def test_validate_missing_span_details():
    spans = _spans4()
    r = _resp_ok()
    r["meanings"][1]["spans"] = r["meanings"][1]["spans"][:1]   # sp0003 상세 누락
    _n, p, _ = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert any("상세 누락" in x for x in p)                  # 반려
    norm, p2, notes = ca.validate_stage2_response(r, spans, final_attempt=True)
    assert p2 == [] and any("전사 기본값" in n for n in notes)  # 소진 시 채움+표기
    # M9-C: 누락 span 은 heard 없음(빈 audio)으로 둔다 — 여기서 전사를 채워 넣으면
    # 판정(adjudicate_transcript)이 무의미해진다. 전사 채택은 판정이 한다.
    filled = norm[1]["spans"][1]
    assert filled["audio"] == []


def test_validate_importance_clamp_and_unvoiced_audio_drop():
    spans = _spans4()
    r = _resp_ok()
    r["meanings"][0]["importance"] = 9
    r["meanings"][0]["spans"][1]["audio"] = [{"speaker": "?", "line": "환청"}]
    norm, p, notes = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert p == []
    assert norm[0]["importance"] == 3 and any("보정" in n for n in notes)
    assert norm[0]["spans"][1]["audio"] == [] and any("무성" in n for n in notes)


def test_adjudication_keeps_transcript_when_sound(monkeypatch):
    """M9-C 각색 방어 유지 — 전사가 정상이면 heard 가 뭐라 하든 전사가 이긴다."""
    spans = _spans4()
    r = _resp_ok()
    r["meanings"][0]["spans"][0]["heard"] = [
        {"speaker": "강비오", "line": "여러분 모두 만나서 정말 기쁘고 반갑습니다"}]  # 각색
    r["meanings"][1]["spans"][1]["heard"] = [
        {"speaker": "강비오", "line": "저는 강비호입니다."}]                        # 소정정
    norm, p, _ = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert p == []
    decisions = ca.adjudicate_transcript(norm, spans, words=None)
    assert all(d["decision"] == "transcript" for d in decisions)   # 전부 전사 채택
    assert norm[0]["spans"][0]["audio"] == [
        {"speaker": "강비오", "line": "안녕하세요."}]              # 각색 복원
    assert next(d for d in decisions if d["span_id"] == "sp0000")["restored"] is True
    assert norm[1]["spans"][1]["audio"][0]["line"] == "저는 강비오입니다."  # 전사 채택


def test_character_cross_check_consistency():
    spans = _spans4()
    norm, _, _ = ca.validate_stage2_response(_resp_ok(), spans, final_attempt=False)
    appearances = [
        {"character": "person_0", "start_sec": 0.0, "end_sec": 16.0},   # 전 구간
        {"character": "person_1", "start_sec": 100.0, "end_sec": 120.0},  # 청크 밖
    ]
    cc = ca.character_cross_check(appearances, norm, spans, 0.0, 16.0)
    assert cc["status"] == "ok" and cc["clusters_in_chunk"] == 1
    row = cc["clusters"][0]
    assert row["label"] == "person_0" and row["top_name"] == "강비오"
    assert row["consistency"] == 1.0                        # 전 span 이 강비오
    assert ca.character_cross_check(None, norm, spans, 0, 16)["status"] == "skipped"
    assert ca.character_cross_check([], norm, spans, 0, 16)["status"] == "skipped"


# ── 리뷰(2026-08-31) 재현 수정 가드 ─────────────────────────────────────────

def test_validate_malformed_ids_are_problems_not_crash():
    """리스트형 id·id 없는 상세·중복 상세 — TypeError 크래시가 아니라 반려(리뷰 high)."""
    spans = _spans4()
    r = {"meanings": [{"first_span": ["sp0000"], "last_span": "sp0003",
                       "content": "x", "importance": 3, "mood": "m", "spans": []}]}
    _n, p, _ = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert any("비문자열" in x for x in p)

    r = _resp_ok()
    r["meanings"][0]["spans"].append({"scene_script": "id 없음"})
    r["meanings"][0]["spans"].append({"id": ["sp0000"], "scene_script": "리스트 id"})
    _n, p, _ = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert any("id 없는/비문자열" in x for x in p)

    r = _resp_ok()
    r["meanings"][0]["spans"].append(dict(r["meanings"][0]["spans"][0]))  # 중복 상세
    _n, p, _ = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert any("중복" in x for x in p)


def test_characters_string_not_char_split():
    spans = _spans4()
    r = _resp_ok()
    r["meanings"][0]["characters"] = "강비오"
    r["meanings"][0]["spans"][0]["characters"] = "홍재인"
    norm, p, notes = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert p == []
    assert norm[0]["characters"] == ["강비오"]              # ['강','비','오'] 금지
    assert norm[0]["spans"][0]["characters"] == ["홍재인"]
    assert any("문자열" in n for n in notes)


def test_spans_for_chunk_no_double_assignment_on_tie():
    """경계가 span 을 정확히 반으로 갈라도 소속은 한 chunk 뿐(중점 반개구간)."""
    grid = _grid_spans([(8, 12, True, "a")])
    a = ca.spans_for_chunk(grid, 0.0, 10.0)
    b = ca.spans_for_chunk(grid, 10.0, 24.0)
    assert len(a) + len(b) == 1                             # 정확히 한 쪽
    assert [s["id"] for s in b] == ["sp0000"]               # 중점 10.0 → [10,24)


def test_importance_bool_coerced():
    spans = _spans4()
    r = _resp_ok()
    r["meanings"][0]["importance"] = True
    norm, p, notes = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert p == [] and norm[0]["importance"] == 3 and any("보정" in n for n in notes)


def test_chunk_cache_discarded_on_upstream_change(tmp_path, monkeypatch):
    """상류(stage1/grid) 재구성 시 청크 캐시 지문 불일치 → 폐기·재분석(리뷰 medium)."""
    import subprocess

    from app.modules.ffmpeg_utils import find_ffmpeg_command
    from app.v3 import chunk_analyze as ca_mod
    from app.v3 import pipeline as v3p

    src = tmp_path / "src.mp4"
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=12:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    monkeypatch.setattr(v3p, "transcribe_words",
                        lambda *a, **k: ([{"t0": 1.0, "t1": 1.4, "text": "테스트",
                                           "prob": 0.9}], []))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    calls = {"n": 0}
    monkeypatch.setattr(ca_mod, "run_chunk_analyze",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or
                                         ([], {"chunk": "x", "attempts": []})))

    out = v3p.run_v3(video_path=src, work_title="합성", outdir=tmp_path / "o",
                     skip_research=True, skip_seq_analyze=True, log=lambda *a: None)
    (out / "stage1.json").write_text(json.dumps(
        _stage1([(0.0, 5.0, [(0.0, 5.0)])])), encoding="utf-8")
    v3p.run_v3(video_path=src, work_title="합성", outdir=tmp_path / "o",
               job_id=out.name, skip_research=True, skip_seq_analyze=True,
               log=lambda *a: None)
    assert calls["n"] == 1
    # stage1 경계 변경(재구성 흉내) → 지문 불일치 → 캐시 폐기 → 재분석
    (out / "stage1.json").write_text(json.dumps(
        _stage1([(0.0, 4.0, [(0.0, 4.0)])], exc={"end": (4.0, 6.0)})),
        encoding="utf-8")
    v3p.run_v3(video_path=src, work_title="합성", outdir=tmp_path / "o",
               job_id=out.name, skip_research=True, skip_seq_analyze=True,
               log=lambda *a: None)
    assert calls["n"] == 2


def test_run_m2_chunk_exception_marks_failed_and_continues(tmp_path, monkeypatch):
    """청크 하나의 예외가 run 전체를 죽이지 않는다 — 커버리지 표기 + 계속(리뷰 high)."""
    import subprocess

    from app.modules.ffmpeg_utils import find_ffmpeg_command
    from app.v3 import chunk_analyze as ca_mod
    from app.v3 import pipeline as v3p

    src = tmp_path / "src.mp4"
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=12:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    monkeypatch.setattr(v3p, "transcribe_words",
                        lambda *a, **k: ([{"t0": 1.0, "t1": 1.4, "text": "테스트",
                                           "prob": 0.9}], []))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    seen = []
    def boom_then_ok(gemini, chunk_file, chunk, *a, **k):
        seen.append(chunk["chunk_number"])
        if chunk["chunk_number"] == 0:
            raise TypeError("unhashable type: 'list'")      # 리뷰 재현 모양
        return ([{"number": 0, "time": {"start": "00:00:03.000",
                                        "end": "00:00:05.000"},
                  "content": "m", "characters": [], "importance": 3, "mood": "x",
                  "spans": []}], {"chunk": "s0c1", "attempts": []})
    monkeypatch.setattr(ca_mod, "run_chunk_analyze", boom_then_ok)

    out = v3p.run_v3(video_path=src, work_title="합성", outdir=tmp_path / "o",
                     skip_research=True, skip_seq_analyze=True, log=lambda *a: None)
    (out / "stage1.json").write_text(json.dumps(
        _stage1([(0.0, 5.0, [(0.0, 3.0), (3.0, 5.0)])])), encoding="utf-8")
    v3p.run_v3(video_path=src, work_title="합성", outdir=tmp_path / "o",
               job_id=out.name, skip_research=True, skip_seq_analyze=True,
               log=lambda *a: None)
    assert seen == [0, 1]                                   # 예외 후에도 다음 청크 진행
    s2 = json.loads((out / "stage2.json").read_text(encoding="utf-8"))
    assert s2["coverage"]["failed"] == 1 and s2["coverage"]["analyzed"] == 1
    assert "예외" in s2["coverage"]["failures"][0]["reason"]


# ── 반려 루프 · 파이프라인 배선 ─────────────────────────────────────────────

class _FakeGemini:
    class config:
        model_name = "gemini-3.1-pro-preview"
        analysis_thinking_level = "high"
        max_retries = 3


def test_run_chunk_analyze_reask_then_success(monkeypatch):
    grid = _grid_spans([(0, 4, True, "안녕하세요."), (4, 8, False, ""),
                        (8, 12, True, "반갑습니다."), (12, 16, True, "저는 강비오입니다.")])
    chunk = {"seq_number": 0, "chunk_number": 0, "start_sec": 0.0, "end_sec": 16.0}
    stage1 = _stage1([(0, 16, [(0, 16)])])
    calls = []
    bad = {"meanings": [{"first_span": "sp0000", "last_span": "sp0000",
                         "content": "x", "importance": 3, "mood": "m", "spans": []}]}
    def fake_call(g, u, prompt):
        calls.append(prompt)
        return json.loads(json.dumps(bad if len(calls) == 1 else _resp_ok()))
    monkeypatch.setattr(ca, "_upload_video",
                        lambda g, p, log=print: type("U", (), {"name": "f", "uri": "u"})())
    monkeypatch.setattr(ca, "_call_stage2_model", fake_call)
    _FakeGemini.client = type("C", (), {"files": type("F", (), {
        "delete": staticmethod(lambda name: None)})()})()

    meanings, audit = ca.run_chunk_analyze(
        _FakeGemini(), Path("x.mp4"), chunk, stage1, grid)
    assert meanings and len(audit["attempts"]) == 2
    assert audit["attempts"][0]["problems"]
    assert "반려 사유" in calls[1]
    assert audit["time_alignment"]["pct"] == 100.0
    assert audit["transcript_guard"]["voiced_spans"] == 3


def test_run_chunk_analyze_exhaustion_marks_failed(monkeypatch):
    grid = _grid_spans([(0, 4, True, "안녕하세요."), (4, 8, False, "")])
    chunk = {"seq_number": 0, "chunk_number": 0, "start_sec": 0.0, "end_sec": 8.0}
    monkeypatch.setattr(ca, "_upload_video",
                        lambda g, p, log=print: type("U", (), {"name": "f", "uri": "u"})())
    monkeypatch.setattr(ca, "_call_stage2_model",
                        lambda g, u, p: {"meanings": [{"first_span": "없는거",
                                                       "last_span": "없는거"}]})
    _FakeGemini.client = type("C", (), {"files": type("F", (), {
        "delete": staticmethod(lambda name: None)})()})()
    meanings, audit = ca.run_chunk_analyze(
        _FakeGemini(), Path("x.mp4"), chunk, _stage1([(0, 8, [(0, 8)])]), grid)
    assert meanings is None and "반려 소진" in audit["failed"]   # 커버리지 표기 몫


def test_pipeline_m2_wiring(tmp_path, monkeypatch):
    """합성 소재 — stage1 주입 후 chunk_split 실행·stage2 조립·증분 캐시 확인."""
    import subprocess

    from app.modules.ffmpeg_utils import find_ffmpeg_command
    from app.v3 import chunk_analyze as ca_mod
    from app.v3 import pipeline as v3p

    src = tmp_path / "src.mp4"
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=12:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    words = [{"t0": 1.0, "t1": 1.4, "text": "테스트", "prob": 0.9}]
    monkeypatch.setattr(v3p, "transcribe_words", lambda *a, **k: (words, []))
    # 클라이언트는 생성만 되고 호출은 가짜 run_chunk_analyze 가 가로챈다
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")

    calls = {"n": 0}
    def fake_rca(gemini, chunk_file, chunk, stage1_doc, grid, **kw):
        calls["n"] += 1
        assert chunk_file.exists()                          # 실재 재단 파일
        return ([{"number": 0, "time": chunk and {"start": "00:00:01.000",
                                                  "end": "00:00:05.000"},
                  "content": "m", "characters": [], "importance": 3, "mood": "x",
                  "spans": []}],
                {"chunk": "s0c0", "attempts": [{"attempt": 1, "problems": []}],
                 "time_alignment": {"checked": 2, "from_grid": 2, "pct": 100.0},
                 "transcript_guard": {"voiced_spans": 1, "restored": 0},
                 "character_check": {"status": "skipped"}})
    monkeypatch.setattr(ca_mod, "run_chunk_analyze", fake_rca)

    # 1차: grid 까지 만들고 stage1 을 손으로 주입한 뒤 재개 — M2 만 돈다
    out = v3p.run_v3(video_path=src, work_title="합성", outdir=tmp_path / "o",
                     skip_research=True, skip_seq_analyze=True, log=lambda *a: None)
    (out / "stage1.json").write_text(json.dumps(
        _stage1([(0.0, 5.0, [(0.0, 5.0)])], exc={"end": (5.0, 6.0)})),
        encoding="utf-8")
    v3p.run_v3(video_path=src, work_title="합성", outdir=tmp_path / "o",
               job_id=out.name, skip_research=True, skip_seq_analyze=True,
               log=lambda *a: None)
    assert calls["n"] == 1
    man = json.loads((out / "checkpoint_chunk_split.json").read_text(encoding="utf-8"))
    assert man["chunks"][0]["file"] and (out / "chunks" / man["chunks"][0]["file"]).exists()
    assert man["exceptions_removed"][0]["key"] == "end"
    s2 = json.loads((out / "stage2.json").read_text(encoding="utf-8"))
    assert s2["schema"] == "v3_stage2/v1"
    assert s2["sequences"][0]["chunks"][0]["meanings"], "meanings 가 채워져야 한다"
    assert s2["coverage"]["analyzed"] == 1 and s2["coverage"]["failed"] == 0
    assert s2["validation"]["time_alignment"]["pct"] == 100.0
    # 재실행 — 청크 캐시로 Pro 재호출 0(요금 보호)
    v3p.run_v3(video_path=src, work_title="합성", outdir=tmp_path / "o",
               job_id=out.name, skip_research=True, skip_seq_analyze=True,
               log=lambda *a: None)
    assert calls["n"] == 1
    # 하네스 로더가 job 디렉토리로 인식(diff --b 접점)
    from app.replay import loader
    arc = loader.load_archive(tmp_path / "o")
    assert arc["layout"] == "jobdir"

# ══════════════════════════════════════════════════════════════════════════
# 내용-시각 정합 (2026-09-01) — 좌표 일치 · 단정 금지 · 프레임 대조 벨트
# ══════════════════════════════════════════════════════════════════════════
# 실사고: 모델이 청크 후반에서 내용을 ~14초 이른 span 에 귀속(피 웅덩이를 걷는
# 장면 시각에 기록 — 8/31 원격 분석에도 같은 결함). 원인 셋을 각각 박제한다.

def _mk_chunk_for_prompt():
    chunk = {"seq_number": 6, "chunk_number": 0,
             "start_sec": 2698.25, "end_sec": 2930.75}
    spans = [{"id": "sp1343", "t_in": 2891.4, "t_out": 2893.1,
              "is_audio": False, "text": None},
             {"id": "sp1344", "t_in": 2893.1, "t_out": 2893.7,
              "is_audio": True, "text": "대사"}]
    s1 = {"sequences": [{"number": 6, "time": {"start": "00:44:58.250",
                                               "end": "00:48:50.750"},
                         "content": "s", "chunks": [
                             {"number": 0, "time": {"start": "00:44:58.250",
                                                    "end": "00:48:50.750"}}]}]}
    return chunk, spans, s1


def test_span_table_uses_chunk_relative_clock():
    """span 표는 첨부 영상 자체의 시계 — 절대시각 표 + 0:00 영상의 암산이
    귀속 드리프트의 공범이었다(실측 +14초)."""
    chunk, spans, s1 = _mk_chunk_for_prompt()
    p = ca.build_stage2_prompt(chunk, s1, spans, None)
    assert "00:03:13" in p                      # 2891.4-2698.25=193.15 → 영상 내 시각
    assert "00:48:11" not in p                  # 절대시각은 표에서 사라짐
    assert "첨부 영상 자체의 시계" in p
    assert "00:44:58" in p                      # 0:00 = 원본 어디인지는 알려준다


def test_prompt_forbids_synopsis_assertion():
    """작품 배경은 표기용 — 화면 해석 금지(시놉시스가 '살인 현장'을 프라이밍해
    모호한 화면에 '시신'을 단정 기록한 실사고)."""
    chunk, spans, s1 = _mk_chunk_for_prompt()
    p = ca.build_stage2_prompt(chunk, s1, spans, None, research_context="시놉시스…")
    assert "화면 해석용이 아니다" in p
    assert "보이는 것" in p and "형체" in p
    assert "앞당겨 적지 마라" in p


def test_binding_sampler_prefers_silent_tail():
    """표본은 scene_script 있는 무성 span, 후반 편중(드리프트가 크는 곳)."""
    chunk_spans = [{"id": f"s{i}", "t_in": float(i), "t_out": i + 1.0,
                    "is_audio": (i % 3 == 0)} for i in range(20)]
    # 실전 norm 은 span_id 키다 — id 만 읽던 버그로 벨트가 전 청크 스킵된 실사고
    norm = [{"spans": [{"span_id": f"s{i}", "scene_script": f"장면{i}"}
                       for i in range(20)]}]
    picked = ca.sample_binding_spans(norm, chunk_spans)
    assert 0 < len(picked) <= ca.BINDING_SAMPLE_COUNT
    assert all(not chunk_spans[int(p["id"][1:])]["is_audio"] for p in picked)
    assert picked[-1]["t_in"] >= 15.0           # 마지막 표본은 후반부


def test_binding_problems_carry_evidence():
    """반려 사유에 기록 vs 실제 화면이 나란히 실린다 — 모델이 고칠 근거."""
    details = [{"id": "sp1343", "scene_script": "피 웅덩이가 보인다",
                "match": False, "seen": "걷는 인물 실루엣"},
               {"id": "sp1345", "scene_script": "문", "match": True, "seen": "문"}]
    probs = ca.binding_problems(details)
    assert len(probs) == 1
    assert "피 웅덩이" in probs[0] and "걷는 인물" in probs[0]
    assert "영상 내 시각으로 실제로 이동" in probs[0]


# ── 주 피사체 가로 위치 (2026-09-02 크롭 앵커 재료) ─────────────────────────

def test_subject_pos_kept_invalid_dropped_absent_no_key():
    spans = _spans4()
    r = _resp_ok()
    r["meanings"][0]["spans"][1]["subject_pos"] = "right"       # 무성 span 정상값
    r["meanings"][1]["spans"][0]["subject_pos"] = "upper-left"  # 오값 → 키만 폐기
    norm, problems, notes = ca.validate_stage2_response(r, spans, final_attempt=False)
    assert problems == []
    assert norm[0]["spans"][1]["subject_pos"] == "right"
    assert "subject_pos" not in norm[1]["spans"][0]             # 오값 = 중앙 폴백
    assert any("subject_pos" in n for n in notes)               # 조용한 폐기 금지
    assert "subject_pos" not in norm[0]["spans"][0]             # 미기록 = 키 없음
    meanings = ca.assemble_chunk_meanings(norm, spans)
    assert meanings[0]["spans"][1]["subject_pos"] == "right"    # 문서까지 승계
    assert "subject_pos" not in meanings[0]["spans"][0]


def test_drop_failed_chunks_keeps_successes_only():
    # --retry-failed-chunks(2026-09-04): 실패 기록(meanings 없음)만 캐시에서 빼 재분석한다
    from app.v3.pipeline import drop_failed_chunks
    done = {"s0c0": {"meanings": [{"number": 0}], "audit": {}},
            "s1c0": {"meanings": None, "audit": {"failed": "반려 소진"}},
            "s1c1": {"meanings": [], "audit": {"failed": "예외"}}}
    keep, retry = drop_failed_chunks(done)
    assert list(keep) == ["s0c0"] and retry == ["s1c0", "s1c1"]
    assert drop_failed_chunks({}) == ({}, [])
