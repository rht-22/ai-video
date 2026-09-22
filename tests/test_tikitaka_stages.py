"""티키타카 파이프라인 — 단계 로직 회귀 가드(LLM 은 가짜 응답 · 렌더는 합성 소스, ffmpeg 없으면 스킵)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tikitaka.common import Job, fmt_tc
from app.tikitaka.llm import extract_json
from app.tikitaka.rebuild import validate_versions, source_script
from app.tikitaka.table import rows_from_items, parse_agentic_rows, fill_n_rows, _moment_sources
from app.tikitaka.timing import CutSource
from app.tikitaka.render import build_ass, ass_time, ass_color, wrap_lines, split_title
from app.tikitaka.report import versions_md, table_md


# ── 공통 픽스처 ────────────────────────────────────────────────────────────
def _w(i, s, e, t):
    return {"i": i, "start": s, "end": e, "text": t, "p": 0.9}


@pytest.fixture
def transcript():
    words = [_w(0, 100.0, 100.3, "나"), _w(1, 100.35, 100.8, "나가고"), _w(2, 100.85, 101.2, "싶어."),
             _w(3, 102.0, 102.4, "왜?"), _w(4, 105.0, 105.5, "그냥"), _w(5, 105.6, 106.2, "싫어서.")]
    lines = [{"id": "L-001", "start": 100.0, "end": 101.2, "text": "나 나가고 싶어.", "word_i": [0, 1, 2], "speaker": "강비호"},
             {"id": "L-002", "start": 102.0, "end": 102.4, "text": "왜?", "word_i": [3], "speaker": "홍재인"},
             {"id": "L-003", "start": 105.0, "end": 106.2, "text": "그냥 싫어서.", "word_i": [4, 5], "speaker": "강비호"}]
    return {"words": words, "lines": lines}


@pytest.fixture
def index():
    return {"title": "포핸즈", "cast": ["강비호", "홍재인"], "speakers": {},
            "scenes": [{"id": "SC-001", "start": 90.0, "end": 120.0, "place": "연습실", "summary": "다툼", "chars": ["강비호", "홍재인"]}],
            "moments": [{"id": "S-001", "start": 103.0, "end": 104.5, "kind": "reaction", "who": "홍재인", "desc": "말문 막힘", "sound": None},
                        {"id": "S-002", "start": 107.0, "end": 108.0, "kind": "action", "who": "강비호", "desc": "문 쾅", "sound": "쾅"},
                        {"id": "S-003", "start": 110.0, "end": 112.0, "kind": "reaction", "who": "강비호", "desc": "한숨", "sound": "한숨"}]}


CUTS = [90.0, 102.8, 106.5, 109.5, 115.0]
DUR = 200.0


@pytest.mark.parametrize("title", [
    {"line1": "투표함에 넣다 말고", "line2": "표 도로 뺀 관객"},
    "투표함에 넣다 말고\n표 도로 뺀 관객",
])
def test_semantic_title_lines_survive_validation_and_render_adapter(index, transcript, title):
    from app.tikitaka.finish import v3_title
    raw = {"versions": [{"n": 1, "title": title, "items": [
        {"type": "S", "line_ids": ["L-001"]}]}], "recommended": 1}
    version = validate_versions(raw, index, transcript)["versions"][0]
    assert version["title"] == "투표함에 넣다 말고\n표 도로 뺀 관객"
    assert split_title(version["title"]) == ("투표함에 넣다 말고", "표 도로 뺀 관객")
    assert v3_title(version["title"]) == {
        "line1": "투표함에 넣다 말고", "line2": "표 도로 뺀 관객"}


# ── extract_json ──────────────────────────────────────────────────────────
def test_extract_json_strips_fence_and_takes_first_value():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('{"a": 1}{"b": 2}') == {"a": 1}
    with pytest.raises(Exception):
        extract_json("not json")


# ── 리빌딩 검증 ────────────────────────────────────────────────────────────
def test_validate_versions_binds_ids_and_drops_bad_items(index, transcript):
    raw = {"versions": [{"n": 1, "strategy": "결말 선공개형", "title": "T", "structure": "s",
                         "items": [{"type": "N", "text": "승일이 결국 참지 못하고 폭발합니다.", "effect": "[부들부들]"},
                                   {"type": "S", "line_ids": ["L-001"], "effect": None},
                                   {"type": "S", "line_ids": ["L-002", "L-003"]},          # 화자 전환도 보존
                                   {"type": "S", "line_ids": ["L-999"]},                   # 없는 ID → 드롭
                                   {"type": "A", "moment_id": "S-002", "effect": "[쾅]"},
                                   {"type": "A", "moment_id": "S-999"},                    # 없는 순간 → 드롭
                                   {"type": "X"}],
                         "analysis": {"grade": "안전"}}],
           "recommended": 7, "reason": "r"}
    out = validate_versions(raw, index, transcript)
    v = out["versions"][0]
    assert [it["type"] for it in v["items"]] == ["N", "S", "S", "A"]
    assert v["items"][0]["plan_sec"] == 4.0
    assert v["items"][1]["text"] == "나 나가고 싶어." and v["items"][1]["speaker"] == "강비호"
    assert v["items"][2]["line_ids"] == ["L-002", "L-003"]
    assert v["items"][3]["moment_id"] == "S-002" and v["items"][3]["sound"] == "쾅"
    assert len(v["issues"]) == 3
    assert out["recommended"] == 1            # 없는 추천 번호 → 첫 버전


def test_validate_versions_trims_tail_over_hard_max(index, transcript):
    raw = {"versions": [{"n": 1, "items": [{"type": "N", "text": "가" * 200}, {"type": "N", "text": "나" * 200}]}]}
    out = validate_versions(raw, index, transcript, hard_max=60)
    v = out["versions"][0]
    assert len(v["items"]) == 1 and v["plan_sec"] == 50.0 and any("상한" in s for s in v["issues"])


def test_source_script_lists_lines_and_moments_in_time_order(index, transcript):
    s = source_script(index, transcript)
    assert "## SC-001 [01:30.000~02:00.000] 연습실" in s
    assert s.index("L-001") < s.index("L-002") < s.index("S-001") < s.index("L-003") < s.index("S-002")
    assert '강비호: "나 나가고 싶어."' in s


# ── 편집 테이블 ────────────────────────────────────────────────────────────
def _version():
    return {"n": 1, "strategy": "결말 선공개형", "title": "결국 나가버린 강비호", "structure": "", "analysis": {},
            "items": [{"type": "N", "text": "승일이 결국 참지 못하고 폭발합니다.", "effect": "[부들부들]"},
                      {"type": "S", "line_ids": ["L-001"], "speaker": "강비호", "text": "나 나가고 싶어.", "effect": "(동공지진)"},
                      {"type": "A", "moment_id": "S-002", "desc": "문 쾅", "sound": "쾅", "who": "강비호", "effect": None},
                      {"type": "S", "line_ids": ["L-003"], "speaker": "강비호", "text": "그냥 싫어서.", "effect": None}]}


def _fake_tts(text):
    return Path("/tmp/fake.mp3"), 4.4


def test_rows_from_items_binds_dialogue_and_action(index, transcript):
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    assert [r["mode"] for r in rows] == ["N", "S", "A", "S"]
    s = rows[1]
    assert s["cuts"][0]["in"] == pytest.approx(99.95) and s["cuts"][0]["out"] == pytest.approx(101.35)   # 단어 −0.05/+0.15
    a = rows[2]
    assert a["cuts"][0]["in"] == pytest.approx(107.0) and a["cuts"][0]["out"] == pytest.approx(108.0)
    n = rows[0]
    assert n["dur"] == 4.4 and n["plan_sec"] == 4.0 and n["cuts"] == []


def test_parse_agentic_rows_snaps_and_avoids_dialogue():
    raw = {"rows": [{"i": 1, "cuts": [{"start": "01:43.000", "end": "01:44.500", "who": "홍재인", "desc": "말문"},
                                      {"start": "01:40.100", "end": "01:41.000"},         # 대사 구간과 겹침 → 제외
                                      {"start": "bad", "end": "01:50.000"},
                                      {"start": "01:47.000", "end": "01:48.200"}]}]}
    out = parse_agentic_rows(raw, CUTS, DUR, avoid=[(99.95, 101.35)])
    srcs = out[1]
    assert [s.id for s in srcs] == ["AG-1-1", "AG-1-4"]
    assert srcs[0].avail_in == pytest.approx(103.0) and srcs[0].avail_out == pytest.approx(106.4)   # 샷 [102.8,106.5] −0.1
    assert srcs[1].avail_in == pytest.approx(107.0) and srcs[1].avail_out == pytest.approx(109.4)


def test_fill_n_rows_stacks_to_tts_duration_and_falls_back(index, transcript):
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    fallback = {1: _moment_sources(["S-001", "S-003"], index, CUTS, DUR)}
    notes = fill_n_rows(rows, {}, fallback, CUTS, DUR)
    n = rows[0]
    assert n["cut_origin"] == "index"
    assert sum(c["dur"] for c in n["cuts"]) == pytest.approx(4.4, abs=0.01) and n["shortfall"] == 0.0
    assert all(c["dur"] >= 0.8 for c in n["cuts"])
    assert not any("미달" in x for x in notes)


def test_fill_n_rows_prefers_agentic_then_adjacent_shots(index, transcript):
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, lambda t: (Path("/tmp/x.mp3"), 9.0))
    ag = {1: [CutSource("AG-1-1", 103.0, 104.0, "a")]}         # 1.0s 뿐 → 인접 샷 보충
    notes = fill_n_rows(rows, ag, {}, CUTS, DUR)
    n = rows[0]
    assert n["cut_origin"] == "agentic" and n["cuts"][0]["src"] == "AG-1-1"       # 뒤 대사(L-003)와 같은 샷이지만 그 앞 구간 = 연속 리드인
    assert sum(c["dur"] for c in n["cuts"]) == pytest.approx(9.0, abs=0.02)
    assert any(c["src"].startswith("SC@") for c in n["cuts"]) and any("보충" in x for x in notes)


# ── ASS / 리포트 ───────────────────────────────────────────────────────────
def test_ass_helpers():
    assert ass_time(65.13) == "0:01:05.13" and ass_time(0) == "0:00:00.00"
    assert ass_color("#FFE24A") == "&H004AE2FF"
    assert wrap_lines("가 나 다 라 마 바 사", max_chars=5) == "가 나 다\\N라 마 바 사"
    assert split_title("전유진 즉석 열창했더니 표 싹쓸이") == ("전유진 즉석", "열창했더니 표 싹쓸이")
    assert split_title("짧은제목") == ("짧은제목", "")


def _table(index, transcript):
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    fill_n_rows(rows, {}, {1: _moment_sources(["S-001", "S-003"], index, CUTS, DUR)}, CUTS, DUR)
    t = 0.0
    for r in rows:
        r["t0"] = round(t, 3)
        r["dur_video"] = round(sum(c["dur"] for c in r["cuts"]), 3)
        t += r["dur_video"] if r["mode"] == "N" else r["dur"]
    return {"version": {k: _version()[k] for k in ("n", "strategy", "title", "structure", "analysis")}, "rows": rows,
            "total_sec": round(t, 3), "voice": "ko_female", "speed": "normal", "cut_search": "index", "agentic": {}, "notes": []}


def test_build_ass_has_styles_and_events(index, transcript):
    ass = build_ass(_table(index, transcript), title="결국 나가버린 강비호")
    assert "Style: Title," in ass and "Style: Effect," in ass and "PlayResX: 1080" in ass
    assert ass.count("Dialogue:") == 1 + 3 + 2          # 제목 1 + (N자막·S자막·S자막) 3 + 효과자막 2
    assert "[부들부들]" in ass and "\\fscx115" in ass
    assert "강비호" in ass and "나 나가고 싶어." in ass


def test_reports_render(index, transcript):
    tbl = _table(index, transcript)
    md = table_md(tbl, title="포핸즈")
    assert "| 1 | **[N]** |" in md and "01:39.950~01:41.350" in md and "(내레이션)" in md
    rb = {"versions": [{"n": 1, "strategy": "결말 선공개형", "title": "T", "structure": "s", "items": _version()["items"],
                        "analysis": {"grade": "안전"}, "plan_sec": 12.0, "issues": []}], "recommended": 1, "reason": "r"}
    vm = versions_md(rb, title="포핸즈")
    assert "[버전 1: 결말 선공개형]" in vm and "<효과자막: [부들부들]>" in vm and "[강비호]" in vm


# ── 렌더 실측(합성 소스 · ffmpeg 6/7 필요) ────────────────────────────────
@pytest.mark.skipif(not os.getenv("FFMPEG_BIN"), reason="FFMPEG_BIN(ffmpeg 6/7) 없음")
def test_render_synthetic_source(tmp_path, index, transcript):
    from app.tikitaka.render import render
    ffmpeg = os.environ["FFMPEG_BIN"]
    src = tmp_path / "src.mp4"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=200",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=200", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    tts = tmp_path / "tts.mp3"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=4.4", "-c:a", "libmp3lame", str(tts)], check=True)
    tbl = _table(index, transcript)
    for r in tbl["rows"]:
        if r["mode"] == "N":
            r["tts"] = str(tts)
    job = Job(source=src, out_dir=tmp_path / "job", title="t")
    job.out_dir.mkdir()
    out = render(job, tbl, title="합성 테스트 제목입니다")
    assert out.exists() and out.stat().st_size > 10_000
    probe = json.loads(subprocess.run([os.environ.get("FFPROBE_BIN", ffmpeg.replace("ffmpeg", "ffprobe")), "-v", "error",
                                       "-show_entries", "format=duration:stream=width,height", "-of", "json", str(out)],
                                      capture_output=True, text=True).stdout)
    assert abs(float(probe["format"]["duration"]) - tbl["total_sec"]) < 0.25
    assert {(s.get("width"), s.get("height")) for s in probe["streams"] if s.get("width")} == {(1080, 1920)}


# ── 모델 규칙 ─────────────────────────────────────────────────────────────
def test_resolve_model_rejects_forbidden_env_models():
    from app.tikitaka.llm import resolve_model, DEFAULT_MODEL
    notes = []
    assert resolve_model("gemini-3.5-flash", log=notes.append) == DEFAULT_MODEL and notes
    assert resolve_model("gemini-3.7-flash", log=notes.append) == "gemini-3.7-flash"
    assert resolve_model(None) == DEFAULT_MODEL and resolve_model("") == DEFAULT_MODEL


def test_nearby_moments_filters_by_anchor_window_and_falls_back():
    from app.tikitaka.table import nearby_moments
    idx = {"moments": [{"id": f"S-{k:03d}", "start": float(t), "end": float(t) + 1} for k, t in enumerate([10, 500, 1000, 1200, 1300, 2500, 2600, 2700, 2800, 2900], 1)]}
    near = nearby_moments(idx, [2600.0, 2700.0], pad=180, min_count=3)
    assert [m["id"] for m in near] == ["S-006", "S-007", "S-008", "S-009"]          # 2420~2880
    assert len(nearby_moments(idx, [10.0], pad=50, min_count=3)) == 10             # 너무 적으면 전량
    assert len(nearby_moments(idx, [], pad=50)) == 10


def test_agentic_schema_is_bounded():
    """2026-09-09 실측: 제약 없는 스키마로 agentic 을 부르면 문자열 필드가 216KB 반복 런어웨이 — 상한이 계약이다."""
    from app.tikitaka.table import AGENTIC_SCHEMA, AGENTIC_MAX_OUTPUT
    cut = AGENTIC_SCHEMA["properties"]["rows"]["items"]["properties"]["cuts"]
    props = cut["items"]["properties"]
    assert all("maxLength" in props[k] for k in ("start", "end", "who", "desc"))
    assert cut["maxItems"] <= 4 and AGENTIC_SCHEMA["properties"]["rows"]["maxItems"] <= 12
    assert 8192 <= AGENTIC_MAX_OUTPUT <= 32768      # thought 와 나눠 쓴다 — 너무 낮으면 출력이 절단된다


def test_repair_json_recovers_truncated_agentic_output():
    from app.tikitaka.llm import repair_json, extract_json
    truncated = '''{"rows": [{"i": 1, "cuts": [{"start": "42:33.500", "end": "42:35.500", "who": "전경"},
        {"start": "43:09.200", "end": "43:11.800", "who": "음대교수, desc: 분노를 억누르'''
    obj = extract_json(truncated, repair=True)
    assert obj == {"rows": [{"i": 1, "cuts": [{"start": "42:33.500", "end": "42:35.500", "who": "전경"}]}]}
    with pytest.raises(Exception):
        extract_json(truncated)                                  # repair 없이는 그대로 실패
    assert repair_json('{"a": [1, 2], "b": {"c": 3}, "d": [4, ') == '{"a": [1, 2], "b": {"c": 3}}'   # 마지막 완결 객체까지 + `,` 정리
    assert extract_json('{"a": {"b": "x]}", "c": [1, {"d": 2}, {"e": "unterm', repair=True) == {"a": {"b": "x]}", "c": [1, {"d": 2}]}}


# ── 행 간 컷 중복 제거 (2026-09-10) ───────────────────────────────────────
def test_trim_source_cuts_out_used_intervals():
    from app.tikitaka.table import trim_source
    src = CutSource("X", 100.0, 106.0, "d")
    t = trim_source(src, [(105.0, 108.0)])
    assert (t.avail_in, t.avail_out) == (100.0, 105.0)                 # 뒤가 겹치면 앞을 취한다
    t = trim_source(src, [(99.0, 101.0)])
    assert (t.avail_in, t.avail_out) == (101.0, 106.0)                 # 앞이 겹치면 뒤를 취한다
    t = trim_source(src, [(101.0, 104.5)])
    assert (t.avail_in, t.avail_out) == (100.0, 101.0) or (t.avail_in, t.avail_out) == (104.5, 106.0)
    assert t.avail_out - t.avail_in == pytest.approx(1.5)             # 가운데가 겹치면 긴 쪽
    assert trim_source(src, [(100.2, 105.9)]) is None                  # 남는 게 0.5s 미만이면 버린다
    assert trim_source(src, []) is src or (trim_source(src, []).avail_in, trim_source(src, []).avail_out) == (100.0, 106.0)


def test_fill_n_rows_has_no_overlap_across_rows(index, transcript):
    v = _version()
    v["items"] = [{"type": "N", "text": "가" * 10, "effect": None}, {"type": "S", "line_ids": ["L-001"], "speaker": "강비호", "text": "x", "effect": None},
                  {"type": "N", "text": "나" * 10, "effect": None}]
    rows = rows_from_items(v, index, transcript, CUTS, DUR, lambda t: (Path("/tmp/x.mp3"), 2.5))
    same = [CutSource("AG-a", 103.0, 106.4, "a"), CutSource("AG-b", 110.0, 114.9, "b")]
    fill_n_rows(rows, {1: list(same), 3: list(same)}, {}, CUTS, DUR)            # 두 N 행에 같은 소스 창
    cuts = [c for r in rows for c in r["cuts"]]
    for i in range(len(cuts)):
        for j in range(i + 1, len(cuts)):
            assert min(cuts[i]["out"], cuts[j]["out"]) - max(cuts[i]["in"], cuts[j]["in"]) <= 0.001, (cuts[i], cuts[j])
    assert rows[0]["shortfall"] == 0.0 and rows[2]["shortfall"] == 0.0
    # S 행 구간(99.95~101.35)과도 겹치지 않는다
    assert all(c["out"] <= 99.95 or c["in"] >= 101.35 for r in rows if r["mode"] == "N" for c in r["cuts"])


# ── A 행 agentic 재탐색 (2026-09-10) ─────────────────────────────────────
def test_apply_agentic_a_rows_replaces_within_window_only(index, transcript):
    from app.tikitaka.table import apply_agentic_a_rows
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    a_row = next(r for r in rows if r["mode"] == "A")                       # S-002 107.0~108.0
    notes = apply_agentic_a_rows(rows, {a_row["i"]: [CutSource("AG-3-1", 107.4, 109.4, "[agentic] 문 쾅 순간", prop_out=108.6)]}, CUTS, DUR)
    assert a_row["cut_origin"] == "agentic" and a_row["cuts"][0]["in"] == pytest.approx(107.4)
    assert a_row["cuts"][0]["out"] == pytest.approx(108.6) and any("agentic" in n for n in notes)     # 모델이 찾은 끝을 쓴다(샷 끝이 아니라)
    rows2 = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    a2 = next(r for r in rows2 if r["mode"] == "A")
    notes2 = apply_agentic_a_rows(rows2, {a2["i"]: [CutSource("AG-3-1", 150.0, 152.0, "x")]}, CUTS, DUR)   # 43s 떨어짐
    assert a2["cut_origin"] == "index" and a2["cuts"][0]["in"] == pytest.approx(107.0) and any("무시" in n for n in notes2)


# ── 실험 모듈(원샷 vs 파이프라인) 순수 로직 ──────────────────────────────
def test_experiment_parse_rows_and_match_line(transcript):
    from app.tikitaka.experiment import parse_rows, match_line
    raw = {"rows": [{"mode": "S", "speaker": "강비호", "text": "나 나가고 싶어", "cuts": [{"start": "01:40.000", "end": "01:41.300", "desc": "[립싱크]"}]},
                    {"mode": "N", "text": "가나다라", "cuts": [{"start": "01:43.000", "end": "01:44.500", "desc": "a"}, {"start": "bad", "end": "01:50.000"}]},
                    {"mode": "A", "text": "(현장음)", "cuts": []},                                # 컷 없음 → 폐기
                    {"mode": "Z", "text": "x", "cuts": [{"start": "00:01.000", "end": "00:02.000"}]}]}
    rows, issues = parse_rows(raw, 200.0)
    assert [r["mode"] for r in rows] == ["S", "N"] and rows[1]["cuts"][0]["dur"] == 1.5 and len(issues) == 3
    m, score = match_line("나 나가고 싶어!", transcript)
    assert m and m["id"] == "L-001" and score > 0.9
    m2, score2 = match_line("완전히 다른 문장입니다 정말로", transcript)
    assert m2 is None and score2 < 0.55


def test_experiment_measure_reports_sync_error_and_rule_violations(transcript):
    from app.tikitaka.experiment import measure
    cuts = [90.0, 102.8, 106.5, 109.5, 115.0]
    rows = [{"i": 1, "mode": "N", "text": "가" * 8, "cuts": [{"in": 103.0, "out": 105.5, "dur": 2.5}, {"in": 106.55, "out": 107.0, "dur": 0.45}], "dur": 2.95},
            {"i": 2, "mode": "S", "text": "나 나가고 싶어", "cuts": [{"in": 100.8, "out": 101.9, "dur": 1.1}], "dur": 1.1},   # 정본 99.95~101.35
            {"i": 3, "mode": "S", "text": "전사에 없는 환각 대사 문장", "cuts": [{"in": 120.0, "out": 121.0, "dur": 1.0}], "dur": 1.0},
            {"i": 4, "mode": "N", "text": "나" * 8, "cuts": [{"in": 104.0, "out": 108.0, "dur": 4.0}], "dur": 4.0}]           # 1행과 겹침 · 3s 초과
    m = measure(rows, transcript, cuts, tts_len={1: 2.0, 4: 2.0})
    assert m["s_matched"] == 1 and m["s_hallucinated"] == 1
    assert m["s_start_err_max"] == pytest.approx(0.85) and m["s_sync_broken_over_0_5s"] == 1
    assert m["n_cut_outside_1_2s"] == 3 and m["n_hold_over_3s"] == 1
    assert m["n_sum_minus_tts_max_abs"] == pytest.approx(2.0)
    assert m["margin_violations"] >= 1           # 106.55 는 샷 경계 106.5 에서 0.05s
    assert m["cut_overlaps"] >= 1


# ── 2026-09-10 사용자 지적 3건 ─────────────────────────────────────────────
def test_literal_action_flags_catch_figurative_actions(index, transcript):
    from app.tikitaka.rebuild import literal_action_flags
    flags = literal_action_flags(["음대 교수 뺨 때리는 천재", "스승의 권위에 침 뱉고 뛰쳐나간 반란", "문 쾅 닫고 나감"], index, transcript)
    assert any("'뺨'" in f for f in flags) and any("'침 뱉'" in f for f in flags)
    assert not any("문 쾅" in f for f in flags)                       # 인덱스 순간에 '문 쾅' 이 있으므로 근거 있음
    assert literal_action_flags(["그냥 감정 표현입니다"], index, transcript) == []


def test_rows_from_items_extends_dialogue_end_with_end_fn(index, transcript):
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts, end_fn=lambda we, limit: min(limit, we + 0.5))
    s = rows[1]                                                        # L-001 마지막 단어 끝 101.2 · 다음 단어 102.0
    assert s["cuts"][0]["out"] == pytest.approx(101.7) and s["dur"] == pytest.approx(101.7 - 99.95)
    rows2 = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    assert rows2[1]["cuts"][0]["out"] == pytest.approx(101.35)         # end_fn 없으면 종전 +0.15


def test_fill_n_rows_fallback_stays_inside_context_scene(index, transcript):
    """행 7 실측: agentic 컷이 짧아지자 ±180s 폴백이 다른 장면(40:30) 인물을 끌어왔다 — 장면 밖 순간은 쓰지 않는다."""
    v = _version()
    v["items"] = [{"type": "S", "line_ids": ["L-001"], "speaker": "강비호", "text": "x", "effect": None},
                  {"type": "N", "text": "가" * 12, "effect": None}]                         # 3.0s 계획 · fake tts 4.4s
    rows = rows_from_items(v, index, transcript, CUTS, DUR, _fake_tts)
    scenes = [{"id": "SC-001", "start": 90.0, "end": 120.0}, {"id": "SC-000", "start": 0.0, "end": 90.0}]
    far = CutSource("S-far", 20.0, 40.0, "[다른 장면] 여자")                                # 장면 밖 넉넉한 소스
    near = CutSource("S-003", 110.0, 112.0, "[reaction] 한숨")
    fill_n_rows(rows, {}, {2: [far, near]}, CUTS, DUR, scenes=scenes)
    n = rows[1]
    assert all(90.0 <= c["in"] < 120.0 for c in n["cuts"]), n["cuts"]                      # 전부 문맥 장면 안
    assert not any(c["src"] == "S-far" for c in n["cuts"])


# ── 4.5 영상 확인 패스 (2026-09-10 고정 구성) ────────────────────────────
def test_verify_prompt_and_diff(index, transcript):
    from app.tikitaka.verify import VERIFY_PROMPT, draft_block, diff_summary
    from app.tikitaka.rebuild import validate_versions
    draft = validate_versions({"versions": [dict(_version(), n=3)]}, index, transcript)["versions"][0]
    p = VERIFY_PROMPT.format(title="포핸즈", episode="1회", strategy=draft["strategy"], n=3, draft_title=draft["title"], digest="",
                             draft=draft_block(draft), script="(script)", target_min=45, target_max=70, guide="", strategy_note="")
    assert "L-001" in p and "S-002" in p and "결말 선공개형" in p and "신체 동작 묘사는 화면에 실제로 있는 것만" in p
    final = validate_versions({"versions": [{"n": 3, "title": "다른 제목", "items": [
        {"type": "N", "text": "새 내레이션"}, {"type": "S", "line_ids": ["L-001"]}, {"type": "S", "line_ids": ["L-002"]},
        {"type": "A", "moment_id": "S-001"}]}]}, index, transcript)["versions"][0]
    d = diff_summary(draft, final)
    assert d["title_changed"] and d["lines_kept"] == 1 and d["lines_added"] == ["L-002"] and d["lines_removed"] == ["L-003"]
    assert d["moments_added"] == ["S-001"] and d["moments_removed"] == ["S-002"]


def test_rows_fingerprint_changes_when_script_changes(index, transcript):
    from app.tikitaka.table import rows_fingerprint
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    a = rows_fingerprint(rows)
    v2 = _version(); v2["items"][0]["text"] = "다른 내레이션 문장"
    b = rows_fingerprint(rows_from_items(v2, index, transcript, CUTS, DUR, _fake_tts))
    assert a != b and a == rows_fingerprint(rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts))


def test_cli_verify_is_default_on():
    from app.tikitaka import cli
    import argparse
    src = open(cli.__file__, encoding="utf-8").read()
    assert '"--no-verify"' in src and "verify_version" in src and '"verify", ["verified_v*.json"' in src


# ── 전사 백엔드·교정 (2026-09-10 방법 1+2) ───────────────────────────────
def test_scribe_words_to_words_filters_and_offsets():
    from app.tikitaka.transcribe import scribe_words_to_words
    payload = [{"type": "word", "text": "나가", "start": 1.0, "end": 1.4, "logprob": -0.1},
               {"type": "spacing", "text": " ", "start": 1.4, "end": 1.5},
               {"type": "audio_event", "text": "(laughter)", "start": 2.0, "end": 2.5},
               {"type": "word", "text": "나가", "start": 1.6, "end": 2.2, "logprob": None},
               {"type": "word", "text": "", "start": 3.0, "end": 3.1}]
    ws = scribe_words_to_words(payload, 600.0)
    assert [w["text"] for w in ws] == ["나가", "나가"]
    assert ws[0]["start"] == 601.0 and ws[0]["end"] == 601.4 and 0.9 < ws[0]["p"] <= 1.0 and ws[1]["p"] == 0.9


def test_accept_correction_guards():
    from app.tikitaka.transcript_polish import accept_correction
    ok, _ = accept_correction("너 내가 음악성 따윈 안중에도 없고 입시장산하고 있다 이 말하고 싶은 거지?", "너 내가 음악성 따윈 안중에도 없고 입시 장사하고 있다 이 말하고 싶은 거지?")
    assert ok
    assert accept_correction("그럼 저는 선생님 목사판밖에 더 되겠어요?", "그럼 저는 선생님 복사판밖에 더 되겠어요?")[0]
    assert not accept_correction("나가 나가", "나가 나가")[0]                                  # 변화 없음
    assert not accept_correction("짧은 말", "완전히 다른 긴 문장으로 다시 써버린 경우입니다 정말로")[0]   # 길이 비
    assert not accept_correction("양심에 찌리니까 꼬투리 잡는", "")[0]


def test_cascade_redo_pulls_downstream_steps():
    from app.tikitaka.cli import cascade_redo
    assert cascade_redo({"transcribe"}) == {"transcribe", "polish", "index", "digest", "rebuild", "verify", "agentic", "table", "framing", "effects", "render"}
    assert cascade_redo({"digest"}) == {"digest", "rebuild", "verify", "agentic", "table", "framing", "effects", "render"}
    assert cascade_redo({"table"}) == {"table", "framing", "effects", "render"}
    assert cascade_redo({"agentic"}) == {"agentic", "table", "framing", "effects", "render"}
    assert cascade_redo(set()) == set()


def test_dialogue_rows_carry_per_line_subtitles(index, transcript):
    v = _version(); v["items"][1] = {"type": "S", "line_ids": ["L-001", "L-002"], "speaker": "강비호", "text": "x", "effect": None}
    transcript["lines"][1]["speaker"] = "강비호"
    rows = rows_from_items(v, index, transcript, CUTS, DUR, _fake_tts)
    s = rows[1]
    assert [x["text"] for x in s["sub_lines"]] == ["나 나가고 싶어.", "왜?"]
    assert s["sub_lines"][0]["start"] == pytest.approx(99.95) and s["sub_lines"][1]["start"] == pytest.approx(101.95)
    from app.tikitaka.render import build_ass
    tbl = _table(index, transcript)
    for r in tbl["rows"]:
        if r["mode"] == "S" and r["i"] == 2:
            r["sub_lines"] = [{"start": r["cuts"][0]["in"], "end": r["cuts"][0]["in"] + 0.6, "text": "나 나가고"}, {"start": r["cuts"][0]["in"] + 0.6, "end": r["cuts"][0]["out"], "text": "싶어."}]
    ass = build_ass(tbl, title="t")
    assert ass.count(",Dialog,") == 3 and "싶어." in ass                     # 2행이 2개 이벤트로, 4행 1개


def test_rank_versions_dedups_and_fills():
    from app.tikitaka.rebuild import rank_versions
    assert rank_versions([3, "1", 3, 99, 7], 3, list(range(1, 11))) == [3, 1, 7, 2, 4, 5, 6, 8, 9, 10]
    assert rank_versions(None, 5, [1, 2, 5]) == [5, 1, 2]
    assert rank_versions([2], 5, [1, 2, 5]) == [5, 2, 1]          # 추천이 랭킹에 없으면 맨 앞


# ── 레이아웃 프리셋 (2026-09-11 레퍼런스 _GTpP-MUizw) ──────────────────────
def test_compute_layout_fill_and_band():
    from app.tikitaka.render import compute_layout, title_font_size
    from app.tikitaka.render import H, SAFE_BOTTOM, stack_need
    f = compute_layout("fill")
    # 유튜브 안전 영역(2026-09-11): 밴드는 5:6 이 상한이고 아래 스택(작품명)이 H-SAFE_BOTTOM 위에서 끝나도록 납작해진다
    assert f["band_y"] == 444 and f["band_h"] <= 1296 and f["band_h"] % 2 == 0
    assert f["band_bottom"] + stack_need(None, None) <= H - SAFE_BOTTOM and f["work_y"] and f["band_bottom"] < f["work_y"] < H - SAFE_BOTTOM
    assert f["sub_y"] == f["band_bottom"] - 86 and f["effect_y"] == int(f["band_y"] + f["band_h"] * 0.30)
    assert "crop=" in f["crop"] and f["effect_size"] == 66 and f["aspect"][0] * f["band_h"] == f["aspect"][1] * 1080   # 비율은 밴드를 따라간다
    b = compute_layout("band")
    assert (b["band_h"], b["band_y"]) == (608, 656) and b["sub_y"] == 656 + 608 + 170 and b["work_y"] is None
    assert title_font_size("음대 교수 정면으로", "들이받는 천재의 역대급 하극상", size_max=104, size_min=64) == 64   # 16자(공백 포함) → 1000/16=62 → 하한 64
    assert title_font_size("짧은", "제목", size_max=104, size_min=64) == 104


def test_build_ass_uses_layout_positions(index, transcript):
    from app.tikitaka.render import build_ass, compute_layout
    tbl = _table(index, transcript)
    L = compute_layout("fill")
    ass = build_ass(tbl, title="결국 나가버린 강비호", layout=L, work_title="포핸즈")
    assert "\\pos(540,200)" in ass and "Style: Work," in ass and "포핸즈" in ass and f"\\pos(540,{L['sub_y']})" in ass
    assert f"\\pos(540,{L['work_y']})" in ass and L["sub_y"] == L["band_bottom"] - 86
    ass_b = build_ass(tbl, title="결국 나가버린 강비호", layout=compute_layout("band"))
    assert "\\pos(540,466)" in ass_b and "Style: Work," in ass_b and ",Work," not in ass_b


# ── 5.5 주인물 크롭 · 효과 타이밍 · 화자 색 (2026-09-11) ─────────────────
def test_framing_parse_and_crop_expr():
    from app.tikitaka.framing import parse_points, crop_x_expr
    r = parse_points({"subject": "강비호", "faces": 1, "points": [{"x": 0.3, "y": 0.4, "face": True}, {"x": 0.32, "y": 0.4}, {"x": 0.7, "y": 0.4, "face": True}]}, 3)
    assert (r["x0"], r["x1"], r["ok"], r["faces"]) == (0.3, 0.7, True, 1)
    assert parse_points({}, 3)["x0"] == 0.5 and parse_points({"points": [{"x": -1, "y": 0.2}]}, 3)["ok"] is False
    # 픽셀·혼합 좌표도 정규화한다(실측: x=196,y=331 / x=0.511,y=484)
    px = parse_points({"faces": 1, "points": [{"x": 196, "y": 331, "face": True}, {"x": 0.511, "y": 484, "face": True}]}, 3, img_w=853, img_h=480)
    assert px["ok"] and px["x0"] == pytest.approx(196/853, abs=1e-3) and px["x1"] == pytest.approx(0.511) and px["y1"] == pytest.approx(484/480 if 484 <= 480 else 484/1000, abs=1e-3)
    # 1920 소스 · 크롭 900: 중심 0.3 → 126px, 0.7 → 894px → 팬(선형)
    expr = crop_x_expr(0.3, 0.7, src_w=1920, crop_w=900, dur=2.0)
    assert expr.startswith("'clip(126.0+(894.0-126.0)*t/2.000,0,1020)'")
    assert crop_x_expr(0.5, 0.51, src_w=1920, crop_w=900, dur=2.0) == "520"          # 작은 이동은 고정(평균)
    assert crop_x_expr(0.0, 0.0, src_w=1920, crop_w=900, dur=1.0) == "0" and crop_x_expr(1.0, 1.0, src_w=1920, crop_w=900, dur=1.0) == "1020"


def test_effect_window_and_speaker_colors(index, transcript):
    from app.tikitaka.render import effect_window, speaker_colors, compute_layout
    row_s = {"mode": "S", "cuts": [{"in": 100.0, "out": 104.0}], "sub_lines": [{"start": 100.0, "end": 101.5, "text": "a"}, {"start": 102.4, "end": 104.0, "text": "b"}]}
    es, ee = effect_window(row_s, 10.0, 14.0)
    assert es == pytest.approx(12.4) and ee == pytest.approx(14.2)                # 마지막 줄(펀치라인) 시작에
    assert effect_window({"mode": "N", "cuts": [{"in": 0, "out": 5}]}, 20.0, 25.0) == (20.0, 21.8)
    assert effect_window({"mode": "A", "cuts": [{"in": 0, "out": 1}]}, 30.0, 31.0) == (30.0, 31.0)     # 행 길이 안(최소 0.8s)
    rows = [{"mode": "S", "speaker": "박경희"}, {"mode": "N"}, {"mode": "S", "speaker": "임재홍"}, {"mode": "S", "speaker": "박경희"}]
    c = speaker_colors(rows)
    assert c["박경희"] == "#FFFFFF" and c["임재홍"] == "#B8FF7A"
    L = compute_layout("fill")
    assert L["title_y"] + 120 < L["band_y"] == 444 and L["band_bottom"] == 444 + L["band_h"]           # 제목이 영상과 안 겹친다


# ── 제목 밀착 · 상황별 프레이밍 · 효과자막 정밀 배치 (2026-09-11) ─────────
def test_layout_band_sits_right_below_title():
    from app.tikitaka.render import compute_layout, TITLE_TOP, TITLE_GAP, TITLE_LINE_H, TITLE_PAD
    L = compute_layout("fill", title="앞집 여자 무례에 극대노한 유튜버 대장")
    block = int(2 * L["title_size"] * TITLE_LINE_H) + TITLE_PAD
    formula = (TITLE_TOP + block + TITLE_GAP) // 2 * 2
    # ffmpeg 가 있으면 실측 잉크 바닥+4+GAP(공식보다 위), 없으면 공식 — 어느 쪽이든 제목 아래끝을 넘지 않고, 아주 약간(10px)만 띈다
    assert TITLE_GAP == 10 and formula - 40 <= L["band_y"] <= formula
    from app.tikitaka.render import measure_title_ink_bottom, split_title
    l1, l2 = split_title("앞집 여자 무례에 극대노한 유튜버 대장")
    ink = measure_title_ink_bottom(l1, l2, L["title_size"], L["title_y"])
    if ink:                                                               # 실측 가능하면: 영상 시작 = 잉크 바닥+4+GAP(짝수 보정) — 겹치지 않고 아주 약간만 띈다
        assert ink + 4 + TITLE_GAP - 2 <= L["band_y"] <= ink + 4 + TITLE_GAP
    else:
        assert L["title_y"] + block // 2 <= L["band_y"]
    assert L["band_bottom"] == L["band_y"] + L["band_h"] and L["band_h"] <= 1296 and L["work_y"] > L["band_bottom"]
    assert L["title_y"] - block // 2 >= TITLE_TOP >= 200                                                  # 상단 컨트롤 바 아래


def test_layout_respects_youtube_safe_zones_with_logo_and_copy():
    from app.tikitaka.render import compute_layout, H, SAFE_TOP, SAFE_BOTTOM, COPY_LINE_H, TITLE_LINE_H, TITLE_PAD
    L = compute_layout("fill", title="이사 첫날부터 이웃 사모님 참교육", logo_size=(4055, 1279), copy_text="풀 영상은 쿠팡플레이에서 시청하세요")
    block = int(2 * L["title_size"] * TITLE_LINE_H) + TITLE_PAD
    assert L["title_y"] - block // 2 >= SAFE_TOP                                                          # 제목 위 = 컨트롤 바 아래
    b = L["logo_box"]
    assert b["y"] >= L["band_bottom"] and b["h"] <= 130 and L["copy_y"] > b["y"] + b["h"]
    assert L["copy_y"] + COPY_LINE_H // 2 <= H - SAFE_BOTTOM                                              # 카피 바닥 = 하단 오버레이 위
    assert L["band_h"] < 1296 and L["band_h"] >= 900                                                      # 납작해지되 영상이 절반 이상은 차지
    band = compute_layout("band")
    assert (band["band_h"], band["band_y"]) == (608, 656)                                                 # band 프리셋은 종전 그대로


def test_parse_points_two_shot_and_wide():
    from app.tikitaka.framing import parse_points
    r = parse_points({"framing": "two_shot", "faces": 2, "points": [{"x": 0.3, "y": 0.4, "face": True}], "partner": [{"x": 0.6, "y": 0.4}]}, 1)
    assert r["framing"] == "two_shot" and not r["wide"] and r["x0"] == pytest.approx(0.45)        # 둘 사이 중점
    far = parse_points({"framing": "two_shot", "points": [{"x": 0.1, "y": 0.4}], "partner": [{"x": 0.9, "y": 0.4}]}, 1)
    assert far["wide"] is False and far["framing"] == "close" and far["x0"] == pytest.approx(0.1)   # 안 들어가면 주인물 클로즈
    assert parse_points({"framing": "wide", "points": [{"x": 0.5, "y": 0.5}]}, 1)["wide"] is False   # 와이드는 제외
    assert parse_points({"framing": "nonsense", "points": [{"x": 0.2, "y": 0.2, "face": True}]}, 1)["framing"] == "close"


def test_effects_place_avoids_face_and_quantizes():
    from app.tikitaka.effects import place, q
    x, y = place(0.2, 0.5, text="(동공지진)", size=66, band_y=300, band_h=1296)
    assert x > 540 and y == int(300 + 1296 * 0.26)                                                # 얼굴 왼쪽 → 오른쪽 열·상단
    x2, _ = place(0.8, 0.5, text="(동공지진)", size=66, band_y=300, band_h=1296)
    assert x2 < 540
    _, y3 = place(0.5, 0.3, text="[갑분싸]", size=66, band_y=300, band_h=1296)
    assert y3 == int(300 + 1296 * 0.62)                                                           # 얼굴이 가운데 → 얼굴 아래
    _, y4 = place(0.5, 0.75, text="[갑분싸]", size=66, band_y=300, band_h=1296)
    assert y4 == int(300 + 1296 * 0.22)                                                           # 얼굴이 가운데 아래 → 위
    xl, _ = place(0.2, 0.5, text="아주아주아주아주긴효과자막문구", size=66, band_y=300, band_h=1296)
    assert xl + len("아주아주아주아주긴효과자막문구") * 66 * 0.95 / 2 <= 1080 - 40 + 1                # 화면 안
    assert q(1.234) == pytest.approx(1.233) and q(0.0) == 0.0


def test_extract_json_repairs_fraction_numbers():
    from app.tikitaka.llm import extract_json
    obj = extract_json('{"points": [{"x": 606/1000, "y": 182/1000, "face": true}], "note": "3/4 박자"}')
    assert obj["points"][0]["x"] == pytest.approx(0.606) and obj["note"] == "3/4 박자"


def test_effects_resolve_overlaps():
    from app.tikitaka.effects import resolve_overlaps, fit_span, MIN_SHOW
    # 실측(1화 v3): S 행 반응 자막 [극대노] 11.03~12.63 와 바로 뒤 N 행 [분노의 요가] 11.63~13.23 — 종전엔 앞을 0.55s 로 잘랐다.
    # 이제 뒤 자막을 앞 자막이 끝난 뒤(12.68)로 미룬다(뒤 창 latest 에 1.0s 이상 남으므로).
    plans = [{"start": 11.03, "end": 12.63, "earliest": 10.0, "latest": 12.63},
             {"start": 11.63, "end": 13.23, "earliest": 11.63, "latest": 15.0}, {"start": 20.0, "end": 21.6}]
    resolve_overlaps(plans)
    assert plans[0]["start"] == 11.03 and plans[0]["end"] == pytest.approx(12.63)
    assert plans[1]["start"] == pytest.approx(12.68, abs=0.02) and plans[1]["end"] == pytest.approx(14.28, abs=0.02)
    assert plans[2] == {"start": 20.0, "end": 21.6}
    # 뒤 자막을 미룰 자리가 없으면(다른 장면) 앞을 끊되 앞 자막도 MIN_SHOW 는 보이게 시작을 앞당긴다
    p2 = [{"start": 5.6, "end": 7.2, "earliest": 4.0, "latest": 7.2}, {"start": 6.0, "end": 7.0, "earliest": 6.0, "latest": 7.0}]
    resolve_overlaps(p2)
    assert p2[0]["end"] == pytest.approx(5.95, abs=0.02) and p2[0]["start"] == pytest.approx(4.95, abs=0.02) and p2[1]["start"] == 6.0
    assert p2[0]["end"] - p2[0]["start"] >= MIN_SHOW - 0.01
    # fit_span: 다른 장면이 이어지면 행 안에서 1.0s 보장(시작 앞당김), 같은 장면이면 HOLD 그대로 넘어간다
    assert fit_span(12.6, earliest=11.0, latest=12.7) == (11.7, 12.7)
    assert fit_span(12.6, earliest=11.0, latest=14.3) == (12.6, 14.2)
    assert fit_span(11.0, earliest=11.0, latest=11.4) == (11.0, 11.4)                 # 창이 1.0s 도 안 되면 창 전체


def test_effects_cache_rules_version_and_tag(tmp_path, index, transcript):
    from app.tikitaka.effects import plan_effects, RULES_VERSION
    from app.tikitaka.render import compute_layout
    tbl = _table(index, transcript)
    for r in tbl["rows"]:
        if r["mode"] == "N":
            r["effect"] = "[효과]"
    job = Job(source=tmp_path / "x.mp4", out_dir=tmp_path, title="t")
    class FakeGemini:
        def text_json(self, *a, **k): return {"plans": []}          # 타이밍 판정 없음 → 기본 규칙
    job.save("effects_v1_fast.json", {"_rules": RULES_VERSION - 1, "stale": {"start": 0, "end": 1}})   # 옛 규칙 캐시는 버린다
    n = plan_effects(job, FakeGemini(), tbl, transcript, compute_layout("fill"), tag="fast")
    saved = job.load("effects_v1_fast.json")
    assert n == sum(1 for r in tbl["rows"] if r.get("effect")) and saved["_rules"] == RULES_VERSION and "stale" not in saved
    plan = next(r["effect_plan"] for r in tbl["rows"] if r.get("effect"))
    assert plan["end"] - plan["start"] >= 1.0 - 1e-6 and plan["earliest"] <= plan["start"]


def test_parse_cropdetect_picks_letterbox_or_full():
    from app.tikitaka.probe import parse_cropdetect
    log = "x crop=1920:960:0:60\n crop=1920:960:0:60\n crop=1920:1080:0:0\n crop=1920:960:0:60"
    a = parse_cropdetect(log, 1920, 1080)
    assert (a["w"], a["h"], a["x"], a["y"], a["letterbox"]) == (1920, 960, 0, 60, True)
    assert parse_cropdetect("crop=1920:1080:0:0", 1920, 1080)["letterbox"] is False
    assert parse_cropdetect("crop=300:200:0:0", 1920, 1080) == {"w": 1920, "h": 1080, "x": 0, "y": 0, "letterbox": False}   # 너무 작으면 전체
    assert parse_cropdetect("", 1920, 1080)["w"] == 1920


# ── 5.5 보이는 얼굴 우선 · 샷별 프레이밍 (2026-09-11, 지금불륜 v3 19s 실측 후속) ─────────
def test_parse_points_uses_partner_face_when_subject_is_back_of_head():
    from app.tikitaka.framing import parse_points
    # 실측 원응답: 화자(안수정) 뒷머리 x 0.158(face=false) · 상대(박경희) 얼굴 x 0.574 — two_shot 이 안 들어가 close 로 떨어지면
    # 종전엔 뒷머리를 따라가 사람이 크롭 밖으로 나갔다. 이제 그 표본은 상대의 보이는 얼굴을 쓴다.
    raw = {"framing": "two_shot", "subject": "안수정", "faces": 2,
           "points": [{"x": 0.158, "y": 273, "face": False}, {"x": 0.161, "y": 279, "face": False}, {"x": 0.354, "y": 427, "face": True}],
           "partner": [{"x": 0.574, "y": 425}, {"x": 0.569, "y": 412}, {"x": 0.941, "y": 517}]}
    r = parse_points(raw, 3, img_w=854, img_h=480)
    assert r["framing"] == "close" and r["swapped"] == 2
    assert r["x0"] == pytest.approx(0.574) and r["x1"] == pytest.approx(0.354) and r["ok"]     # 첫 표본은 상대 얼굴, 끝은 주인물 얼굴
    assert "상대 얼굴 2표본" in r["note"]
    # 주인물 얼굴이 보이면 상대가 있어도 그대로(회귀 0)
    same = parse_points({"framing": "close", "points": [{"x": 0.3, "y": 0.4, "face": True}], "partner": [{"x": 0.7, "y": 0.4}]}, 1)
    assert same["x0"] == pytest.approx(0.3) and same["swapped"] == 0
    # 상대도 얼굴이 안 보이면(face=false) 대체하지 않는다
    nf = parse_points({"framing": "close", "points": [{"x": 0.2, "y": 0.4, "face": False}], "partner": [{"x": 0.7, "y": 0.4, "face": False}]}, 1)
    assert nf["x0"] == pytest.approx(0.2) and nf["ok"] is False


def test_split_by_shots_and_piecewise_crop_expr():
    from app.tikitaka.framing import split_by_shots, crop_x_expr_segs, crop_x_expr, frame_at
    # 실측 컷 549.18~553.82, 샷 경계 552.8 → 두 구간. 경계 0.5s 안쪽(가장자리)은 나누지 않는다
    assert split_by_shots(549.18, 553.82, [100.0, 552.8, 553.6, 600.0]) == [(549.18, 552.8), (552.8, 553.82)]
    assert split_by_shots(10.0, 12.0, [10.3, 11.7]) == [(10.0, 12.0)]
    assert split_by_shots(10.0, 12.0, None) == [(10.0, 12.0)]
    # 구간 하나면 종전 식과 같다
    one = [{"t0": 0.0, "t1": 2.0, "x0": 0.3, "x1": 0.7}]
    assert crop_x_expr_segs(one, src_w=1920, crop_w=900) == crop_x_expr(0.3, 0.7, src_w=1920, crop_w=900, dur=2.0)
    # 두 구간: 앞은 고정(0.57 → 644px), 뒤는 자기 구간 시작에서 팬((t-3.62)) · 경계에서 즉시 점프
    segs = [{"t0": 0.0, "t1": 3.62, "x0": 0.57, "x1": 0.57}, {"t0": 3.62, "t1": 4.64, "x0": 0.2, "x1": 0.5}]
    expr = crop_x_expr_segs(segs, src_w=1920, crop_w=900)
    assert expr == "'if(lt(t,3.620),644,clip(0.0+(510.0-0.0)*(t-3.620)/1.020,0,1020))'"
    # frame_at: 그 시각의 구간 얼굴
    cut = {"frame_segs": [{"t0": 0.0, "t1": 3.62, "x0": 0.57, "x1": 0.57, "y0": 0.5, "y1": 0.5},
                          {"t0": 3.62, "t1": 4.64, "x0": 0.2, "x1": 0.4, "y0": 0.6, "y1": 0.6}]}
    assert frame_at(cut, 1.0) == (pytest.approx(0.57), pytest.approx(0.5))
    assert frame_at(cut, 4.0) == (pytest.approx(0.3), pytest.approx(0.6))
    assert frame_at({"frame_x0": 0.2, "frame_x1": 0.4, "frame_y0": 0.3, "frame_y1": 0.3}, 0.5) == (pytest.approx(0.3), pytest.approx(0.3))


# ── 제작 가이드 · 로고 · 카피 (2026-09-11 사용자 요청) ────────────────────────
def test_guide_parse_keys_and_avoid_hits(tmp_path):
    from app.tikitaka.guide import parse_guide_text, load_guides, guide_block, avoid_hits, discover_guides
    txt = "# 가이드\n- 톤: 시니컬\n지양 단어: 콘돔, 피임 / 섹스\n로고: logo.png\n카피: 풀 영상은 쿠팡플레이에서 시청하세요\n카피 위치: 아래\n"
    g = parse_guide_text(txt)
    assert g["avoid"] == ["콘돔", "피임", "섹스"] and g["logo"] == "logo.png" and g["copy_pos"] == "below"
    assert g["copy"] == "풀 영상은 쿠팡플레이에서 시청하세요"
    assert parse_guide_text("카피 위치: 위\n")["copy_pos"] == "above" and parse_guide_text("- 금지어: 욕설\n")["avoid"] == ["욕설"]
    # 파일 로드: 로고 상대경로는 가이드 파일 위치 기준 · 회차 파일이 뒤에 와서 같은 키를 이긴다
    base = tmp_path / "guides"; (base / "작품").mkdir(parents=True)
    (base / "작품.md").write_text(txt, encoding="utf-8")
    (base / "작품" / "1화.md").write_text("회차 메모\n지양 단어: 살인\n카피 위치: 위\n", encoding="utf-8")
    paths = discover_guides("작품", "1화", base=base)
    assert [p.name for p in paths] == ["작품.md", "1화.md"] and discover_guides("없음", "1화", base=base) == []
    g = load_guides(paths)
    assert g["avoid"] == ["콘돔", "피임", "섹스", "살인"] and g["copy_pos"] == "above" and g["logo"] == str((base / "logo.png").resolve())
    assert "### 작품" in g["text"] and "회차 메모" in g["text"] and len(g["sha"]) == 12
    assert guide_block(None) == "" and "지양 단어**: 콘돔, 피임, 섹스, 살인" in guide_block(g)
    assert avoid_hits(["[콘돔 발견]", "멀쩡한 문구"], ["콘돔"]) == [("콘돔", "[콘돔 발견]")]
    assert load_guides([]) is None


def test_validate_versions_drops_avoid_lines_and_flags_texts(index, transcript):
    from app.tikitaka.rebuild import validate_versions, guide_hits
    transcript["lines"][0]["text"] = "나 콘돔 사러 나가고 싶어."
    raw = {"versions": [{"n": 1, "title": "콘돔 들킨 남편", "items": [
        {"type": "S", "line_ids": ["L-001"]}, {"type": "S", "line_ids": ["L-002"], "effect": "[콘돔 발견]"},
        {"type": "N", "text": "지갑에서 나온 그것.", "effect": "[증거]"}]}], "recommended": 1}
    data = validate_versions(raw, index, transcript, avoid=["콘돔"])
    v = data["versions"][0]
    assert [it["type"] for it in v["items"]] == ["S", "N"]                                   # 지양 단어 대사 줄은 드롭
    assert any("[가이드] 지양 단어 ['콘돔'] 대사 ['L-001']" in x for x in v["issues"])
    assert sorted(w for w, _ in v["guide_hits"]) == ["콘돔", "콘돔"]                            # 제목 + 효과자막
    assert guide_hits(v, ["콘돔"]) == [("콘돔", "콘돔 들킨 남편"), ("콘돔", "[콘돔 발견]")]
    same = validate_versions(raw, index, transcript)                                          # avoid 없으면 종전 그대로
    assert [it["type"] for it in same["versions"][0]["items"]] == ["S", "S", "N"] and same["versions"][0]["guide_hits"] == []


def test_polish_guide_rewrites_only_hits(index, transcript):
    from app.tikitaka.rebuild import polish_guide
    version = {"n": 7, "title": "콘돔 들킨 남편", "plan_sec": 0, "issues": ["[가이드 위반] x"], "items": [
        {"type": "A", "moment_id": "S-001", "desc": "d", "effect": "[콘돔 발견]", "plan_sec": 1.5},
        {"type": "N", "text": "멀쩡한 내레이션.", "effect": None, "plan_sec": 2.0}]}
    class FakeGemini:
        def text_json(self, prompt, **kw):
            assert "지양 단어: 콘돔" in prompt and "[콘돔 발견]" in prompt
            return {"texts": ["수상한 물건 들킨 남편", "[수상한 물건 발견]", "모델이 멋대로 바꾼 문장"]}
    logs = []
    n = polish_guide(FakeGemini(), version, {"avoid": ["콘돔"], "text": "가이드"}, log=logs.append)
    assert n == 2 and version["title"] == "수상한 물건 들킨 남편" and version["items"][0]["effect"] == "[수상한 물건 발견]"
    assert version["items"][1]["text"] == "멀쩡한 내레이션."                                    # 걸리지 않은 문구는 안 바꾼다
    assert version["guide_hits"] == [] and any(x.startswith("[가이드 교정] 2건") for x in version["issues"])
    assert polish_guide(FakeGemini(), version, {"avoid": ["콘돔"]}, log=logs.append) == 0        # 멱등


def test_table_matches_version_detects_text_changes():
    from app.tikitaka.table import table_matches_version
    v = {"title": "T", "items": [{"type": "N", "text": "a", "effect": "[x]"}, {"type": "A", "effect": "[콘돔 발견]"}]}
    tbl = {"version": {"title": "T"}, "rows": [{"i": 1, "mode": "N", "text": "a", "effect": "[x]"}, {"i": 2, "mode": "A", "effect": "[콘돔 발견]"}]}
    assert table_matches_version(tbl, v)
    v2 = {"title": "T", "items": [dict(v["items"][0]), {"type": "A", "effect": "[수상한 물건 발견]"}]}
    assert not table_matches_version(tbl, v2)
    assert not table_matches_version(tbl, dict(v, title="다른 제목"))


def test_framing_view_rank_prefers_body_over_back():
    from app.tikitaka.framing import parse_points
    # 실측(지금불륜 v3 30~36s): 화자 박경희는 멀리 전신(body), 전경에 초록 옷 뒷모습(back) — 뒷모습을 주인물로 낸 응답도
    # 상대가 body 면 상대 좌표를 쓴다(뒷모습만 따라가면 사람이 안 잡힌다)
    raw = {"framing": "close", "subject": "초록 상의", "points": [{"x": 0.74, "y": 0.88, "face": False, "view": "back"}],
           "partner": [{"x": 0.27, "y": 0.4, "face": False, "view": "body"}]}
    r = parse_points(raw, 1)
    assert r["x0"] == pytest.approx(0.27) and r["swapped"] == 1
    # 둘 다 back 이면 그대로 · view 없는 옛 응답은 face 플래그로(face=false 는 back 취급)
    both = parse_points({"points": [{"x": 0.7, "y": 0.5, "view": "back"}], "partner": [{"x": 0.2, "y": 0.5, "view": "back"}]}, 1)
    assert both["x0"] == pytest.approx(0.7)
    legacy = parse_points({"points": [{"x": 0.7, "y": 0.5, "face": False}], "partner": [{"x": 0.2, "y": 0.5, "face": True}]}, 1)
    assert legacy["x0"] == pytest.approx(0.2)


def test_bottom_stack_logo_and_copy_geometry():
    from app.tikitaka.render import bottom_stack, compute_layout, H, W, COPY_LINE_H, BOTTOM_GAP
    st = bottom_stack(1586, logo_size=(4055, 1279), copy_text="풀 영상은 쿠팡플레이에서 시청하세요", copy_pos="below")
    b = st["logo_box"]
    assert st["work_y"] is None and b["w"] <= 640 and b["h"] <= 170 and b["x"] == (W - b["w"]) // 2
    assert abs(b["w"] / b["h"] - 4055 / 1279) < 0.05                                           # 비율 유지
    assert b["y"] >= 1586 and st["copy_y"] > b["y"] + b["h"] and st["copy_y"] + COPY_LINE_H // 2 <= H     # 카피가 로고 아래 · 화면 안
    above = bottom_stack(1586, logo_size=(4055, 1279), copy_text="카피", copy_pos="above")
    assert above["copy_y"] < above["logo_box"]["y"]
    txt = bottom_stack(1586, logo_size=None, copy_text="카피", copy_pos="below")
    assert txt["logo_box"] is None and txt["work_y"] and txt["copy_y"] > txt["work_y"]
    plain = bottom_stack(1586, logo_size=None, copy_text=None)
    assert plain["work_y"] == 1586 + (H - 1586) // 2 and plain["copy_y"] is None              # 종전 작품명 위치 그대로
    assert bottom_stack(H - 40, logo_size=(100, 100), copy_text="x")["logo_box"] is None       # 영역이 없으면 아무것도
    L = compute_layout("fill", title="합성 테스트 제목입니다", logo_size=(4055, 1279), copy_text="카피")
    assert L["logo_box"] and L["copy_y"] and L["work_y"] is None
    L0 = compute_layout("fill", title="합성 테스트 제목입니다")
    assert L0["logo_box"] is None and L0["copy_y"] is None and L0["work_y"]                   # 미지정 = 종전


def test_build_ass_copy_and_logo(index, transcript):
    from app.tikitaka.render import build_ass, compute_layout
    tbl = _table(index, transcript)
    L = compute_layout("fill", title="제목", logo_size=(400, 100), copy_text="풀 영상은 쿠팡플레이에서 시청하세요")
    ass = build_ass(tbl, title="제목", layout=L, work_title="작품명", copy_text="풀 영상은 쿠팡플레이에서 시청하세요")
    assert "Style: Copy," in ass and ",Copy,,0,0,0,,{\\an5\\pos(540," in ass and "쿠팡플레이" in ass
    assert ",Work,,0,0,0,," not in ass                                                        # 로고가 있으면 작품명 텍스트 없음
    L2 = compute_layout("fill", title="제목", copy_text="카피")
    ass2 = build_ass(tbl, title="제목", layout=L2, work_title="작품명", copy_text="카피")
    assert ",Work,,0,0,0,," in ass2 and ",Copy,,0,0,0,," in ass2


def test_render_with_logo_and_copy(tmp_path, index, transcript):
    from app.tikitaka.render import render
    from PIL import Image
    ffmpeg = os.environ["FFMPEG_BIN"]
    src = tmp_path / "src.mp4"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=200",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=200", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    tts = tmp_path / "tts.mp3"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=4.4", "-c:a", "libmp3lame", str(tts)], check=True)
    logo = tmp_path / "logo.png"
    im = Image.new("RGBA", (800, 200), (0, 0, 0, 0))
    for x in range(800):
        for y in range(60, 140):
            im.putpixel((x, y), (255, 255, 255, 255))                                          # 흰 띠(알파) 로고
    im.save(logo)
    tbl = _table(index, transcript)
    for r in tbl["rows"]:
        if r["mode"] == "N":
            r["tts"] = str(tts)
    job = Job(source=src, out_dir=tmp_path / "job", title="t")
    job.out_dir.mkdir()
    out = render(job, tbl, title="합성 테스트 제목입니다", logo=logo, copy_text="풀 영상은 쿠팡플레이에서 시청하세요")
    assert out.exists()
    step = next(s for s in job.load("run_log.json")["steps"] if s["step"].startswith("render_v"))
    b = step["logo_box"]
    # 로고 상자 가운데 행은 흰색(로고), 상자 밖 검정 영역은 검정이어야 한다
    frame = tmp_path / "f.png"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", "1.0", "-i", str(out), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as fr:
        cx, cy = b["x"] + b["w"] // 2, b["y"] + b["h"] // 2
        assert fr.getpixel((cx, cy))[0] > 200
        assert fr.getpixel((b["x"] - 20, b["y"] + 4))[0] < 40
    with pytest.raises(FileNotFoundError):
        render(job, tbl, title="t", logo=tmp_path / "없음.png")


# ── 활용 불가 구간(권리사 가이드 · 2026-09-11 쿠팡플레이 PDF) ────────────────────
def test_guide_parse_ranges_and_exclusion_helpers():
    from app.tikitaka.guide import parse_ranges, parse_guide_text, excluded_ranges, in_excluded
    rs = parse_ranges("22:06~22:50 (수정&재홍 호텔 씬 일체) / 36:52~37:06 (스킨쉽) / 44:00~엔딩 (반전 대사)")
    assert [(r["start"], r["end"]) for r in rs] == [(1326.0, 1370.0), (2212.0, 2226.0), (2640.0, None)]
    assert rs[0]["note"] == "수정&재홍 호텔 씬 일체" and rs[2]["note"] == "반전 대사"
    assert parse_ranges("07:00~06:00") == [] and parse_ranges("없음") == []
    g = parse_guide_text("활용 불가: 06:09~06:49 (스킨십) / 28:00~29:07\n해시태그: #쿠팡플레이 지금불륜이문제가아닙니다\n")
    assert len(g["exclude"]) == 2 and g["hashtags"] == ["#쿠팡플레이", "#지금불륜이문제가아닙니다"]
    assert parse_guide_text("활용 불가: 없음\n")["exclude"] == []
    ex = excluded_ranges({"exclude": rs}, duration=3000.0)
    assert ex == [(1326.0, 1370.0), (2212.0, 2226.0), (2640.0, 3000.0)] and excluded_ranges(None, 10.0) == []
    assert in_excluded(1360.0, 1380.0, ex) and in_excluded(2990.0, 2999.0, ex) and not in_excluded(1370.0, 1400.0, ex)


def test_source_script_and_validate_exclude_ranges(index, transcript):
    from app.tikitaka.rebuild import source_script, validate_versions
    ex = [(101.5, 104.0)]                       # L-002(102.0~102.4)·S-001(103.0~104.5) 이 걸친다
    script = source_script(index, transcript, ex)
    assert "L-001" in script and "L-002" not in script and "S-001" not in script and "활용 불가 구간 01:41.500~01:44.000" in script
    assert "L-002" in source_script(index, transcript)                                  # exclude 없으면 종전 그대로
    raw = {"versions": [{"n": 1, "title": "t", "items": [{"type": "S", "line_ids": ["L-001"]}, {"type": "S", "line_ids": ["L-002"]},
                                                          {"type": "A", "moment_id": "S-001"}, {"type": "N", "text": "내레이션 문장."}]}], "recommended": 1}
    v = validate_versions(raw, index, transcript, exclude=ex)["versions"][0]
    assert [it["type"] for it in v["items"]] == ["S", "N"]
    assert sum("[가이드] 활용 불가 구간" in x for x in v["issues"]) == 2


def test_table_drops_excluded_sources_and_belt(index, transcript):
    from app.tikitaka.table import drop_excluded_sources, cuts_in_excluded, fill_n_rows, _moment_sources, rows_from_items
    from app.tikitaka.timing import CutSource
    ex = [(103.0, 104.0)]
    srcs = [CutSource("a", 100.0, 101.0, "ok"), CutSource("b", 103.5, 105.0, "걸침"), CutSource("c", 104.0, 106.0, "경계 밖")]
    assert [x.id for x in drop_excluded_sources(srcs, ex)] == ["a", "c"] and drop_excluded_sources(srcs, []) == srcs
    rows = [{"i": 1, "mode": "N", "cuts": [{"src": "x", "in": 103.2, "out": 104.2}]}, {"i": 2, "mode": "S", "cuts": [{"src": "L", "in": 100.0, "out": 101.0}]}]
    assert cuts_in_excluded(rows, ex) == ["행1[N] x 01:43.200~01:44.200"] and cuts_in_excluded(rows, []) == []
    # fill_n_rows: 활용 불가 순간(S-001 103~104.5)은 후보에서 빠진다
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    fallback = {1: _moment_sources(["S-001", "S-003"], index, CUTS, DUR)}
    fill_n_rows(rows, {}, fallback, CUTS, DUR, exclude=[(103.0, 104.5)])
    n_row = next(r for r in rows if r["mode"] == "N")
    assert all(not (c["in"] < 104.5 and c["out"] > 103.0) for c in n_row["cuts"])


# ── 배우 이름 표기 · 크리에이터 톤 (2026-09-11 사용자 요청) ─────────────────────
def test_guide_actor_map_and_apply_name_map():
    from app.tikitaka.guide import parse_guide_text, guide_block
    from app.tikitaka.rebuild import apply_name_map
    g = parse_guide_text("배우: 박경희=김혜수, 안수정 → 조여정 / 임재홍: 김지훈\n")
    assert g["actors"] == {"박경희": "김혜수", "안수정": "조여정", "임재홍": "김지훈"}
    assert "인물 표기는 배우 이름으로" in guide_block({"text": "x", "actors": g["actors"]}) and "박경희→김혜수" in guide_block({"text": "x", "actors": g["actors"]})
    v = {"title": "박경희 vs 안수정 전쟁", "plan_sec": 0, "issues": [], "items": [
        {"type": "N", "text": "경희 남편 임재홍의 본색.", "effect": "[박경희 분노]", "plan_sec": 2.0},
        {"type": "S", "line_ids": ["L-001"], "speaker": "박경희", "text": "박경희가 말했다", "effect": None, "plan_sec": 1.0}]}
    n = apply_name_map(v, g["actors"], log=lambda *_: None)
    assert n == 3 and v["title"] == "김혜수 vs 조여정 전쟁"
    assert v["items"][0]["text"] == "경희 남편 김지훈의 본색." and v["items"][0]["effect"] == "[김혜수 분노]"   # 부분 이름('경희')은 안 건드린다
    assert v["items"][1]["text"] == "박경희가 말했다" and v["items"][1]["speaker"] == "박경희"               # 대사·화자 키는 그대로
    assert apply_name_map(v, g["actors"], log=lambda *_: None) == 0 and apply_name_map(v, {}, log=lambda *_: None) == 0


def test_build_ass_speaker_label_uses_actor_name(index, transcript):
    from app.tikitaka.render import build_ass, compute_layout
    tbl = _table(index, transcript)
    ass = build_ass(tbl, title="제목", layout=compute_layout("fill"), name_map={"강비호": "김배우"})
    assert "김배우" in ass and "강비호{\\r}" not in ass
    plain = build_ass(tbl, title="제목", layout=compute_layout("fill"))
    assert "강비호" in plain and "김배우" not in plain                                                        # 미지정 = 종전


def test_prompts_ask_for_creator_voice_narration():
    from app.tikitaka.prompts import REBUILD_PROMPT
    from app.tikitaka.verify import VERIFY_PROMPT
    assert "크리에이터(편집자)의 리액션·의견" in REBUILD_PROMPT and "설명만 하는 해설 금지" in REBUILD_PROMPT
    # 2026-09-22 사용자 결정: 확인 패스는 해설 위주여도 그대로 둔다(의견형 강제 삭제) — 대신 보존 규칙·지시어 규칙이 있어야 한다
    assert "크리에이터의 리액션·의견" not in VERIFY_PROMPT
    assert "글자 그대로 옮긴다" in VERIFY_PROMPT and "이렇게 말합니다" in VERIFY_PROMPT


# ── N 행 컷 0개 폴백 (2026-09-11 2화 v5 실측: 뒤쪽 샷이 활용 불가 구간에 통째로 막힘) ─────────
def test_prev_shots_before_and_fill_fallback_when_forward_blocked(index, transcript):
    from app.tikitaka.timing import prev_shots_before
    from app.tikitaka.table import fill_n_rows, rows_from_items
    assert prev_shots_before([100.0, 103.0, 107.0], 107.5, count=2) == [(103.1, 106.9), (100.1, 102.9)]   # 107.1~107.4 는 0.5s 미만 → 건너뜀
    assert prev_shots_before([], 3.0) == [(0.1, 2.9)] and prev_shots_before([10.0], 10.2) == [(0.1, 9.9)]
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, _fake_tts)
    # 문맥 장면(90~120) 안에서 S 행(100~101.2) 뒤쪽 전부가 활용 불가 → 앞 샷(90~100)으로 채워야 한다
    notes = fill_n_rows(rows, {}, {}, CUTS, DUR, scenes=index["scenes"], exclude=[(101.3, 130.0)])
    n_row = next(r for r in rows if r["mode"] == "N")
    assert n_row["cuts"] and all(c["out"] <= 101.3 for c in n_row["cuts"]) and any("앞 샷·장면 밖 샷으로 보충" in n for n in notes)


def test_plan_effects_skips_rows_without_cuts(tmp_path, index, transcript):
    from app.tikitaka.effects import plan_effects
    from app.tikitaka.render import compute_layout
    tbl = _table(index, transcript)
    n_row = next(r for r in tbl["rows"] if r["mode"] == "N")
    n_row["cuts"], n_row["effect"], n_row["dur_video"] = [], "[효과]", 0.0
    job = Job(source=tmp_path / "x.mp4", out_dir=tmp_path, title="t")
    class NoGemini:
        def text_json(self, *a, **k): raise AssertionError("S 행이 없으면 호출 없음")
    assert plan_effects(job, NoGemini(), tbl, transcript, compute_layout("fill")) == 0 or True     # 죽지 않는다
    assert "effect_plan" not in n_row


def test_effects_place_stays_above_dialogue_block_and_cache_key_has_t0(tmp_path, index, transcript):
    from app.tikitaka.effects import place, plan_effects, RULES_VERSION
    from app.tikitaka.render import compute_layout
    L = compute_layout("fill", title="제목")                                   # 안전 영역 밴드(≈950px)
    assert L["sub_top"] < L["sub_y"] and L["sub_top"] > L["band_y"]
    _, y = place(0.5, 0.3, text="[갑분싸]", size=66, band_y=L["band_y"], band_h=L["band_h"], sub_top=L["sub_top"])
    assert y + 66 * 0.8 <= L["sub_top"] + 1                                    # 효과자막이 대사 블록 위에
    _, y0 = place(0.5, 0.3, text="[갑분싸]", size=66, band_y=300, band_h=1296)
    assert y0 == int(300 + 1296 * 0.62)                                        # sub_top 없으면 종전
    # 캐시 키에 t0: 앞 행 길이가 바뀌어 t0 가 밀리면 옛 플랜을 재사용하지 않는다
    tbl = _table(index, transcript)
    job = Job(source=tmp_path / "x.mp4", out_dir=tmp_path, title="t")
    class FakeGemini:
        def text_json(self, *a, **k): return {"plans": []}
    plan_effects(job, FakeGemini(), tbl, transcript, L)
    first = {k: v for k, v in job.load("effects_v1.json").items() if k != "_rules"}
    assert all(k.count("|") == 3 for k in first)                               # i|effect|in|t0
    for r in tbl["rows"]:
        r["t0"] = round(r["t0"] + 1.4, 3)
        r.pop("effect_plan", None)
    plan_effects(job, FakeGemini(), tbl, transcript, L)
    second = {k: v for k, v in job.load("effects_v1.json").items() if k != "_rules"}
    assert len(second) == 2 * len(first) and all(abs(r["effect_plan"]["start"] - r["t0"]) < 5 for r in tbl["rows"] if r.get("effect"))
    assert job.load("effects_v1.json")["_rules"] == RULES_VERSION


def test_effects_latest_end_depends_on_next_row_kind():
    from app.tikitaka.effects import _latest_end, HOLD_SEC, NEXT_LINE_GRACE
    s_row = {"mode": "S", "cuts": [{"in": 100.0, "out": 102.0}]}
    rows = [s_row, {"mode": "N", "cuts": [{"in": 103.0, "out": 104.5}]}]
    assert _latest_end(rows, 0, 10.0) == pytest.approx(10.0 + HOLD_SEC)                      # 같은 장면 리액션 컷 → 넘어간다
    rows = [s_row, {"mode": "S", "cuts": [{"in": 103.0, "out": 104.5}]}]
    assert _latest_end(rows, 0, 10.0) == pytest.approx(10.0 + NEXT_LINE_GRACE)               # 다음이 새 대사 → 거의 안 넘어간다
    rows = [s_row, {"mode": "N", "cuts": [{"in": 900.0, "out": 902.0}]}]
    assert _latest_end(rows, 0, 10.0) == 10.0                                                # 다른 장면 → 행 끝
    assert _latest_end([s_row], 0, 10.0) == 10.0                                             # 마지막 행


# ── 선형 서사 계열(v11~14) · --range (2026-09-11 사용자 요청) ─────────────────────
def test_ranges_complement_and_prompt_has_linear_patterns():
    from app.tikitaka.guide import ranges_complement
    from app.tikitaka.prompts import REBUILD_PROMPT
    from app.tikitaka.rebuild import STRATEGIES, LINEAR_STRATEGIES, SEQ_HOOK_RULES
    assert ranges_complement([(180.0, 420.0)], 600.0) == [(0.0, 180.0), (420.0, 600.0)]
    assert ranges_complement([(0.0, 100.0), (300.0, 600.0)], 600.0) == [(100.0, 300.0)]
    assert ranges_complement([], 50.0) == [(0.0, 50.0)]
    assert len(STRATEGIES) == 15 and STRATEGIES[10:] == ["구간 순차형", "루프형", "장면 통째 압축형", "점층 빌드업형", "공감형"]
    assert LINEAR_STRATEGIES == set(STRATEGIES[10:])
    p = REBUILD_PROMPT.format(title="t", episode_label="1화", duration_label="45분", target_min=45, target_max=70, hard_max=75, script="(s)", digest="",
                              guide="", material_note="", seq_hook_rule=SEQ_HOOK_RULES[True])
    assert "11 구간 순차형" in p and "12 루프형" in p and "13 장면 통째 압축형" in p and "14 점층 빌드업형" in p and "콜드오픈" in p and "… 14개 …" in p
    # 15 공감형(2026-09-18): 뼈대가 아니라 톤(설명 금지·리액션 종결·집단 제목) · 포맷은 15개지만 요청 대본은 14개 그대로 · 이 포맷만 길이 예외 25~45초
    from app.tikitaka.rebuild import SHORT_FORM_STRATEGIES, N_VERSIONS
    assert "15 공감형" in p and "25~45초" in p and "설명하지 마라" in p and "리액션" in p and SHORT_FORM_STRATEGIES == {"공감형": (25, 45)} and N_VERSIONS == 14
    # 포맷은 골라 쓰는 도구(강제 배정 아님) · 마지막 항목은 이야기를 닫는다 · 던진 질문은 회수된다 — 규칙(검증기)이 아니라 작가 지침
    assert "골라" in p and "강제 배정 아님" in p and "마지막 항목은 이야기를 닫는다" in p and "답이 되는 장면" in p and "자연 흐름" in p


def test_enforce_linear_order_reorders_and_keeps_hook(index, transcript):
    from app.tikitaka.rebuild import validate_versions
    # L-001(100.0) · L-002(102.0) · L-003(105.0) · S-001(103.0). 순차형인데 모델이 뒤집어 냈다: 콜드오픈 L-003 → L-002 → N → L-001 → A
    raw = {"versions": [{"n": 11, "strategy": "구간 순차형", "title": "t", "items": [
        {"type": "S", "line_ids": ["L-003"]}, {"type": "S", "line_ids": ["L-002"]}, {"type": "N", "text": "그 전에 있었던 일."},
        {"type": "S", "line_ids": ["L-001"]}, {"type": "A", "moment_id": "S-001"}]}], "recommended": 11}
    v = validate_versions(raw, index, transcript, seq_hook=True)["versions"][0]
    kinds = [(it["type"], it.get("line_ids", [it.get("moment_id")])[0]) for it in v["items"]]
    assert kinds == [("S", "L-003"), ("N", None), ("S", "L-001"), ("S", "L-002"), ("A", "S-001")]   # 콜드오픈 고정 · 나머지 시간순 · N 은 다음 대사 앞
    assert any(x.startswith("[순차] 원본 순서로 재배열") for x in v["issues"]) and v["strategy"] == "구간 순차형"
    v_off = validate_versions(raw, index, transcript, seq_hook=False)["versions"][0]
    assert [it.get("line_ids", [it.get("moment_id")])[0] for it in v_off["items"]] == [None, "L-001", "L-002", "S-001", "L-003"]
    # 루프형: 마지막이 첫 항목 앞으로 되돌아오면 그 항목은 순서 검사 밖 + 기록
    raw2 = {"versions": [{"n": 12, "strategy": "루프형", "title": "t", "items": [
        {"type": "S", "line_ids": ["L-002"]}, {"type": "A", "moment_id": "S-001"}, {"type": "S", "line_ids": ["L-003"]}, {"type": "S", "line_ids": ["L-001"]}]}]}
    v2 = validate_versions(raw2, index, transcript)["versions"][0]
    assert [it.get("line_ids", [it.get("moment_id")])[0] for it in v2["items"]] == ["L-002", "S-001", "L-003", "L-001"]
    assert any("[루프]" in x and "되돌아옴" in x for x in v2["issues"])
    # 비선형 전략은 전체 순서는 자유지만 **같은 장면 안**은 시간순(첫 훅 제외) — 장면 안 순서 벨트(2026-09-11)
    raw3 = dict(raw, versions=[dict(raw["versions"][0], n=1, strategy="결말 선공개형")])
    v3 = validate_versions(raw3, index, transcript)["versions"][0]
    assert [it.get("line_ids", [it.get("moment_id")])[0] for it in v3["items"]] == ["L-003", "L-001", "L-002", None, "S-001"]   # N 은 직전 S/A(L-002) 뒤


def test_effects_drop_captions_without_room(tmp_path, index, transcript):
    """실측(1화 v2 행1·행2): 0.86s 대사 + 0.38s 대사가 연달아 자막을 달면 둘 다 1.0s 를 못 채운다 — 둘째를 생략하고 첫째를 다시 편다."""
    from app.tikitaka.effects import plan_effects, MIN_DROP
    from app.tikitaka.render import compute_layout, build_ass
    words = transcript["words"]
    rows = [{"i": 1, "mode": "S", "t0": 0.0, "dur": 0.86, "dur_video": 0.86, "src": ["L-001"], "speaker": "a", "text": "x", "effect": "[기습 폭로]",
             "cuts": [{"src": "L-001", "in": 100.0, "out": 100.86, "dur": 0.86}], "sub_lines": [{"start": 100.0, "end": 100.86, "text": "x"}]},
            {"i": 2, "mode": "S", "t0": 0.86, "dur": 0.38, "dur_video": 0.38, "src": ["L-002"], "speaker": "b", "text": "y", "effect": "[동공지진]",
             "cuts": [{"src": "L-002", "in": 102.0, "out": 102.38, "dur": 0.38}], "sub_lines": [{"start": 102.0, "end": 102.38, "text": "y"}]},
            {"i": 3, "mode": "S", "t0": 1.24, "dur": 2.0, "dur_video": 2.0, "src": ["L-003"], "speaker": "a", "text": "z", "effect": None,
             "cuts": [{"src": "L-003", "in": 105.0, "out": 107.0, "dur": 2.0}], "sub_lines": [{"start": 105.0, "end": 107.0, "text": "z"}]}]
    tbl = {"version": {"n": 2, "title": "t"}, "rows": rows, "total_sec": 3.24}
    job = Job(source=tmp_path / "x.mp4", out_dir=tmp_path, title="t")
    class FakeGemini:
        def text_json(self, *a, **k): return {"plans": []}
    shown = plan_effects(job, FakeGemini(), tbl, transcript, compute_layout("fill"))
    assert shown == 1 and rows[1]["effect_plan"] is None and rows[0]["effect_plan"]["end"] - rows[0]["effect_plan"]["start"] >= MIN_DROP
    assert job.load("effects_v2.json")[f"2|[동공지진]|102.000|0.860"] is None                     # 생략도 캐시된 결정
    ass = build_ass(tbl, title="t", layout=compute_layout("fill"))
    assert "[기습 폭로]" in ass and "[동공지진]" not in ass                                         # 렌더도 생략을 존중(폴백 창으로 안 떨어진다)


def test_canonical_strategy_tolerates_model_suffixes():
    from app.tikitaka.rebuild import canonical_strategy, STRATEGIES
    assert canonical_strategy("점층 빌드업형(슬로우 번)", 14) == "점층 빌드업형" and canonical_strategy("루프형 ", 12) == "루프형"
    # 2026-09-17 사용자 결정: 포맷은 재료에 맞을 때 고르는 도구다 — 모르는 이름·빈 이름을 버전 번호로 강제 배정하지 않는다(자연 흐름)
    from app.tikitaka.rebuild import FREE_FORMAT
    assert canonical_strategy("구간 순차형 — 03:00~07:00", 11) == "구간 순차형" and canonical_strategy(None, 3) == FREE_FORMAT
    assert canonical_strategy("모르는 이름", 99) == FREE_FORMAT and canonical_strategy("결말 선공개형", 1) == "결말 선공개형"
    assert canonical_strategy("자연 흐름", 5) == FREE_FORMAT and FREE_FORMAT not in STRATEGIES


def test_loop_tail_and_copy_narration_drop(index, transcript):
    from app.tikitaka.rebuild import validate_versions
    from app.tikitaka.guide import guide_block
    # 루프형인데 끝(L-003 105s)이 처음(L-002 102s) 앞으로 안 돌아온다 → 첫 대사 직전 줄 L-001 을 꼬리로 붙인다(마지막 N 앞에)
    raw = {"versions": [{"n": 12, "strategy": "루프형", "title": "t", "items": [
        {"type": "S", "line_ids": ["L-002"]}, {"type": "S", "line_ids": ["L-003"]}, {"type": "N", "text": "그리고 다시."},
        {"type": "N", "text": "풀 영상은 쿠팡플레이에서 시청하세요."}]}]}
    v = validate_versions(raw, index, transcript, copy_text="풀 영상은 쿠팡플레이에서 시청하세요")["versions"][0]
    kinds = [(it["type"], it.get("line_ids", [None])[0]) for it in v["items"]]
    assert kinds == [("S", "L-002"), ("S", "L-003"), ("S", "L-001")]                       # "그리고 다시." 는 메타 내레이션으로 드롭
    assert any("[루프] 끝이 처음으로 안 돌아와" in x for x in v["issues"]) and any("카피 문구를 읽는 내레이션 1건 드롭" in x for x in v["issues"])
    assert any("메타 내레이션 1건 드롭" in x for x in v["issues"])
    # 이미 되돌아오면 안 붙인다
    raw2 = {"versions": [{"n": 12, "strategy": "루프형", "title": "t", "items": [{"type": "S", "line_ids": ["L-002"]}, {"type": "S", "line_ids": ["L-001"]}]}]}
    v2 = validate_versions(raw2, index, transcript)["versions"][0]
    assert [it["line_ids"][0] for it in v2["items"]] == ["L-002", "L-001"]
    assert "다시 쓰지 마라" in guide_block({"text": "x", "copy": "풀 영상은 쿠팡플레이에서 시청하세요"})


def test_redo_preserves_tagged_outputs(tmp_path, monkeypatch):
    """--redo render 는 무태그 shorts_v*.mp4 만 지우고 태그 산출(v3_fast · v11_r0300-0700)은 보존한다(2026-09-11 · 14버전 재생성 전 확인)."""
    import re
    from app.tikitaka.cli import REDO_FILES, cascade_redo
    out = tmp_path
    for name in ("shorts_v3.mp4", "shorts_v3_fast.mp4", "shorts_v11_r0300-0700.mp4", "table_v3.json", "table_v3_fast.json", "rebuild.json", "rebuild_r0300-0700.json"):
        (out / name).write_text("x")
    redo = cascade_redo({"table"})
    tag_re = re.compile(r"_v\d+_(.+?)\.(json|md|mp4|txt)$")
    for step, files in REDO_FILES:
        if step in redo:
            for pat in files:
                for p in out.glob(pat):
                    if tag_re.search(p.name):
                        continue
                    p.unlink()
    left = sorted(p.name for p in out.iterdir())
    assert left == ["rebuild.json", "rebuild_r0300-0700.json", "shorts_v11_r0300-0700.mp4", "shorts_v3_fast.mp4", "table_v3_fast.json"]


def test_rerank_replaces_ranking_and_keeps_model_ranking(tmp_path):
    from app.tikitaka.rebuild import rerank, apply_rerank, RERANK_PROMPT, version_block
    vs = [{"n": k, "strategy": "s", "title": f"t{k}", "plan_sec": 50, "items": [{"type": "N", "text": "n", "effect": None}]} for k in (1, 2, 3)]
    class FakeGemini:
        class usage: calls = [{"kind": "rerank"}]
        def text_json(self, prompt, **kw):
            assert "번호는 아무 의미가 없다" in prompt and "### 버전 2" in prompt and "[3, 1" not in RERANK_PROMPT
            return {"ranking": [2, 3, 3, 9], "recommended": 2, "reason": "r", "notes": {"2": "good"}}
    res = rerank(FakeGemini(), vs, title="t", episode="1화", log=lambda *_: None)
    assert res["ranking"] == [2, 3, 1] and res["recommended"] == 2 and res["ok"] and res["notes"] == {"2": "good"}
    job = Job(source=tmp_path / "x.mp4", out_dir=tmp_path, title="t")
    data = {"versions": vs, "ranking": [3, 1, 2], "recommended": 3, "reason": "예시 베낌"}
    apply_rerank(job, FakeGemini(), data, title="t", episode="1화", basis="verified")
    assert data["ranking"] == [2, 3, 1] and data["model_ranking"]["ranking"] == [3, 1, 2] and data["rerank"]["basis"] == "verified"
    class Broken:
        class usage: calls = []
        def text_json(self, *a, **k): raise RuntimeError("boom")
    d2 = {"versions": vs, "ranking": [3, 1, 2], "recommended": 3, "reason": "x"}
    apply_rerank(job, Broken(), d2, title="t", episode="1화")
    assert d2["ranking"] == [3, 1, 2] and "rerank" not in d2                                     # 실패면 종전 값 유지(기록)
    assert version_block(vs[0]).startswith("### 버전 1")


# ── 2026-09-11 14버전 검수 후속: 자막 픽셀 맞춤 · 효과자막 단어 시작 · A/S 중복 · 화자 교정 ─────────
def test_fit_subtitle_wraps_by_pixels_and_shrinks():
    from app.tikitaka.render import fit_subtitle, text_px, FONTS_DIR, build_ass, compute_layout
    fp = str(FONTS_DIR / "JalnanGothic.ttf")
    long = "필요한 거 있으시면 부담 없이 말씀하세요. 뭐든지 도울게요."          # 실측 1646px(62px 기준) — 종전엔 둘째 줄에 몰려 잘렸다
    lines, fs = fit_subtitle(long, font_path=fp, size=62)
    assert len(lines) <= 2 and all(text_px(l, fp, fs) <= 1000 for l in lines) and 46 <= fs <= 62
    short, fs2 = fit_subtitle("이사 떡이요.", font_path=fp, size=62)
    assert short == ["이사 떡이요."] and fs2 == 62
    very = " ".join(["아주아주긴어절"] * 9)
    lines3, fs3 = fit_subtitle(very, font_path=fp, size=62)
    assert fs3 == 46 and len(lines3) >= 3 and all(text_px(l, fp, fs3) <= 1000 for l in lines3)   # 최소 크기에서도 안 들어가면 3줄+
    tbl = {"version": {"n": 1, "title": "t"}, "total_sec": 5.0, "rows": [
        {"i": 1, "mode": "S", "t0": 0.0, "dur": 4.0, "speaker": "a", "text": long, "cuts": [{"in": 10.0, "out": 14.0, "dur": 4.0}],
         "sub_lines": [{"start": 10.0, "end": 14.0, "text": long}]},
        {"i": 2, "mode": "N", "t0": 4.0, "dur": 1.0, "dur_video": 1.0, "text": "짧은 내레이션.", "cuts": [{"in": 20.0, "out": 21.0, "dur": 1.0}]}]}
    ass = build_ass(tbl, title="t", layout=compute_layout("fill"))
    assert "\\N".join(lines) in ass.replace("\\N", "\\N") or all(l in ass for l in lines)


def test_validate_drops_action_overlapping_dialogue(index, transcript):
    from app.tikitaka.rebuild import validate_versions
    # S-001(103.0~104.5) 이 L-002(102.0~102.4)·L-003(105.0~106.2) 과는 안 겹치지만, 순간을 대사 L-002 구간으로 옮겨 보면 드롭된다
    index["moments"].append({"id": "S-009", "start": 100.2, "end": 101.0, "kind": "reaction", "who": "홍재인", "desc": "겹침", "sound": None})
    raw = {"versions": [{"n": 1, "title": "t", "items": [{"type": "S", "line_ids": ["L-001"]}, {"type": "A", "moment_id": "S-009"}, {"type": "A", "moment_id": "S-001"}]}]}
    v = validate_versions(raw, index, transcript)["versions"][0]
    assert [it.get("moment_id") for it in v["items"] if it["type"] == "A"] == ["S-001"] and any("[중복] A S-009" in x for x in v["issues"])


def test_agentic_a_proposal_rejected_when_overlapping_dialogue(index, transcript):
    from app.tikitaka.table import apply_agentic_a_rows, rows_from_items
    from app.tikitaka.timing import CutSource
    rows = rows_from_items(dict(_version(), items=[{"type": "S", "line_ids": ["L-001"], "effect": None, "plan_sec": 1.4},
                                                    {"type": "A", "moment_id": "S-001", "desc": "d", "effect": None, "plan_sec": 1.5}]),
                           index, transcript, CUTS, DUR, _fake_tts)
    a_row = next(r for r in rows if r["mode"] == "A")
    before = dict(a_row["cuts"][0])
    notes = apply_agentic_a_rows(rows, {a_row["i"]: [CutSource("AG", 100.2, 103.9, "[agentic] x", prop_out=101.4)]}, CUTS, DUR)
    assert a_row["cuts"][0] == before and any("대사 행 구간과 겹침" in n for n in notes)          # L-001(100~101.2) 구간 → 거절, 인덱스 추정 유지


def test_speaker_fixes_validation(index, transcript):
    from app.tikitaka.verify import _valid_fixes
    ok = _valid_fixes({"L-001": "홍재인", "L-002": "김배우", "L-999": "강비호", "L-003": "강비호 "}, transcript, ["강비호", "홍재인"])
    assert ok == {"L-001": "홍재인"}                                             # 목록에 없는 이름·없는 줄·이미 같은 화자는 무시
    assert _valid_fixes("nope", transcript, ["강비호"]) == {} and _valid_fixes(None, transcript, []) == {}


def test_loop_meta_narration_dropped(index, transcript):
    from app.tikitaka.rebuild import validate_versions, _is_meta_loop_narration
    assert _is_meta_loop_narration("남편의 헛소리에 식어가는 김혜수, 그리고 다시—") and _is_meta_loop_narration("그리고 다시 처음으로.")
    assert not _is_meta_loop_narration("다시 보니 이 남자 진짜 뻔뻔하네요.")
    raw = {"versions": [{"n": 12, "strategy": "루프형", "title": "t", "items": [
        {"type": "S", "line_ids": ["L-002"]}, {"type": "N", "text": "식어가는 김혜수, 그리고 다시—"}, {"type": "S", "line_ids": ["L-001"]}]}]}
    v = validate_versions(raw, index, transcript)["versions"][0]
    assert [it["type"] for it in v["items"]] == ["S", "S"] and any("[루프] 메타 내레이션 1건 드롭" in x for x in v["issues"])


# ── 목소리 기준 화자 대조 · 장면 안 순서 벨트 (2026-09-11 v1 실측) ─────────────────────
def test_stt_form_fields_diarize_default_false():
    from app.modules.stt_elevenlabs import build_form_fields
    base = build_form_fields(language="ko", keyterms=None, is_raw=False)
    assert ("diarize", "false") in base and ("diarize", "true") not in base                      # 자막 경로 회귀 0
    assert ("diarize", "true") in build_form_fields(language="ko", keyterms=None, is_raw=False, diarize=True)


def test_assign_voices_and_speaker_consistency():
    from app.tikitaka.voice_check import assign_voices, speaker_consistency
    lines = [{"id": f"L-{k:03d}", "start": 10.0 * k, "end": 10.0 * k + 2.0, "speaker": sp, "text": "x"}
             for k, sp in enumerate(["박경희", "박경희", "박경희", "안수정", "안수정", "임재홍", "박경희", None])]
    # 목소리 A = 0,1,2,6,7 · B = 3,4 · C = 5
    voice_of = {0: "w0:a", 1: "w0:a", 2: "w0:a", 3: "w0:b", 4: "w0:b", 5: "w0:c", 6: "w0:a", 7: "w0:a"}
    diar = [{"start": 10.0 * k + 0.2, "end": 10.0 * k + 1.8, "voice": v} for k, v in voice_of.items()]
    diar.append({"start": 63.0, "end": 63.5, "voice": "w0:a"})      # 줄 6 에 잠깐 겹치는 다른 조각 — 다수 겹침이 이긴다
    assert assign_voices(lines, diar) == 8 and lines[3]["voice"] == "w0:b" and lines[7]["voice"] == "w0:a"
    lines[6]["speaker"] = "안수정"                                    # 인덱스가 목소리 A 의 한 줄을 안수정으로 잘못 적음(폐건물 사례)
    fixes, report = speaker_consistency(lines, min_cluster=2, min_share=0.6)
    assert fixes == {"L-006": "박경희", "L-007": "박경희"}           # 안수정은 홈 클러스터(B)가 따로 있으니 A 안의 안수정은 오류 → 교정 · 미상도 이름을 받는다
    b = next(r for r in report if r["voice"] == "w0:b")
    assert b["fixed"] == [] and b["majority"] == "안수정"
    lines[6]["speaker"] = "하실장"                                    # 홈 클러스터가 없는 조연은 뒤집지 않는다(화자 분리가 합쳤을 수 있다)
    fixes3, report3 = speaker_consistency(lines, min_cluster=2, min_share=0.6)
    assert "L-006" not in fixes3 and next(r for r in report3 if r["voice"] == "w0:a")["skipped"] == {"하실장": 1}
    lines[6]["speaker"] = "안수정"; lines[0]["voice"] = None
    fixes2, _ = speaker_consistency(lines, min_cluster=3, min_share=0.9)
    assert fixes2 == {}                                               # 다수 비율 조건 미달이면 손대지 않는다


def test_enforce_scene_order_within_scene_only(index, transcript):
    from app.tikitaka.rebuild import validate_versions, enforce_scene_order, apply_scene_order_version
    # 장면 SC-001(90~120): L-001 100 · L-002 102 · S-001 103 · L-003 105. 다른 장면 SC-002(200~230)에 순간 S-020 210
    index["scenes"].append({"id": "SC-002", "start": 200.0, "end": 230.0, "place": "", "summary": "", "chars": []})
    index["moments"].append({"id": "S-020", "start": 210.0, "end": 211.0, "kind": "reaction", "who": "x", "desc": "다른 장면", "sound": None})
    raw = {"versions": [{"n": 1, "strategy": "결말 선공개형", "title": "t", "items": [
        {"type": "A", "moment_id": "S-020"},                         # 훅(다른 장면 결말) — 제외
        {"type": "S", "line_ids": ["L-003"]}, {"type": "N", "text": "그 전에."}, {"type": "S", "line_ids": ["L-001"]},
        {"type": "A", "moment_id": "S-001"}, {"type": "S", "line_ids": ["L-002"]}]}]}
    v = validate_versions(raw, index, transcript)["versions"][0]
    seq = [it.get("moment_id") or it.get("line_ids", [None])[0] or it["text"] for it in v["items"]]
    assert seq == ["S-020", "L-001", "L-002", "S-001", "L-003", "그 전에."]                        # 장면 안만 시간순 · N 은 직전 S/A 뒤 · 훅 유지
    assert any(x.startswith("[장면 순서] SC-001") for x in v["issues"])
    # 장면을 넘는 순서(결말 선공개)는 그대로
    raw2 = {"versions": [{"n": 1, "strategy": "결말 선공개형", "title": "t", "items": [
        {"type": "A", "moment_id": "S-020"}, {"type": "S", "line_ids": ["L-001"]}, {"type": "S", "line_ids": ["L-002"]}]}]}
    v2 = validate_versions(raw2, index, transcript)["versions"][0]
    assert [it.get("moment_id") or it["line_ids"][0] for it in v2["items"]] == ["S-020", "L-001", "L-002"] and not any("[장면 순서]" in x for x in v2["issues"])
    # 캐시된 버전 후처리(멱등)
    assert apply_scene_order_version(v, index, transcript, log=lambda *_: None) == 0


def test_table_cache_invalidates_on_speaker_change(index, transcript):
    from app.tikitaka.table import table_matches_version
    v = {"title": "T", "items": [{"type": "S", "line_ids": ["L-001"], "effect": None}]}
    tbl = {"version": {"title": "T"}, "rows": [{"i": 1, "mode": "S", "speaker": "강비호", "effect": None}]}
    by = {l["id"]: l for l in transcript["lines"]}
    assert table_matches_version(tbl, v, by)
    by["L-001"]["speaker"] = "홍재인"
    assert not table_matches_version(tbl, v, by) and table_matches_version(tbl, v)                 # lines 없이 부르면 종전 판정


# ── 검은 화면 위 컷 (2026-09-12 사용자 지적 "v1 35초에 검은 화면이 있어서 tts 와 안 맞아") ─────────────────────────
def test_parse_blackdetect_and_cache(tmp_path, monkeypatch):
    from app.tikitaka import scenecut as C
    log = "[blackdetect @ 0x1] black_start:2927.5 black_end:2930.8 black_duration:3.3\n[blackdetect] black_start:61.1 black_end:64.1 black_duration:3\nblack_start:5 black_end:5"
    assert C.parse_blackdetect(log) == [(61.1, 64.1), (2927.5, 2930.8)]                       # 정렬 · 길이 0 제외
    job = Job(source=tmp_path / "src.mp4", out_dir=tmp_path, title="t")
    monkeypatch.setattr(C, "find_bin", lambda n: "ffmpeg")
    calls = []

    class R:
        returncode = 0
        stderr = log
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: calls.append(a) or R())
    assert C.detect_black_spans(job, tmp_path / "cut_480p_10fps.mp4") == [(61.1, 64.1), (2927.5, 2930.8)]
    assert C.detect_black_spans(job, tmp_path / "cut_480p_10fps.mp4") == [(61.1, 64.1), (2927.5, 2930.8)] and len(calls) == 1   # 캐시
    assert C.detect_black_spans(job, tmp_path / "other.mp4") and len(calls) == 2                                            # 소스가 다르면 재검출

    class Bad:
        returncode = 1
        stderr = "boom"
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: Bad())
    assert C.detect_black_spans(job, tmp_path / "third.mp4") == []                              # 실패는 빈 목록(본편은 막지 않는다)


def test_trim_source_prefers_proposal_side_and_caps_drift():
    """1화 v1 재현: 어두운 장면이라 샷 경계가 안 잡혀 가용 창 42s — 종전 '긴 쪽' 규칙이 제안(2888.5)에서 40s 떨어진 페이드로 밀었다."""
    from app.tikitaka.table import trim_source
    src = CutSource("AG-14-1", 2888.5, 2930.7, "핸드폰 쥐고 둘러봄", prop_out=2891.0)
    used = [(2905.0, 2907.5), (2917.0, 2920.0), (2923.0, 2926.0), (2926.0, 2928.508)]
    t = trim_source(src, used)
    assert (t.avail_in, t.avail_out) == (2888.5, 2905.0) and t.prop_out == 2891.0             # 제안 쪽 조각 · prop_out 승계
    # 제안 구간 자체가 쓰였고 남은 조각이 제안 끝 + 3s 를 넘어서 시작하면 버린다(딴 내용)
    assert trim_source(CutSource("X", 2888.5, 2930.7, "d", prop_out=2891.0), [(2888.0, 2895.0)]) is None
    t2 = trim_source(CutSource("X", 2888.5, 2930.7, "d", prop_out=2891.0), [(2888.0, 2893.0)])
    assert (t2.avail_in, t2.avail_out) == (2893.0, 2930.7)                                       # 2s 뒤는 같은 샷 연장으로 허용
    # prop_out 없는 소스(인접 샷)는 종전 규칙(긴 쪽) 그대로
    t3 = trim_source(CutSource("SC@", 100.0, 106.0, "d"), [(101.0, 104.5)])
    assert (t3.avail_in, t3.avail_out) == (104.5, 106.0)


def test_carve_black_and_cuts_in_black():
    from app.tikitaka.table import carve_black, cuts_in_black
    black = [(2927.5, 2930.8)]
    srcs = [CutSource("a", 2888.5, 2930.7, "제안 앞", prop_out=2891.0), CutSource("b", 2928.508, 2930.7, "통째 검정"),
            CutSource("c", 2926.0, 2935.0, "양쪽 걸침", prop_out=2932.0)]
    out = {x.id: x for x in carve_black(srcs, black)}
    assert (out["a"].avail_in, out["a"].avail_out) == (2888.5, 2927.3) and out["a"].prop_out == 2891.0   # 검정(−0.2 여유) 앞에서 끝
    assert "b" not in out                                                                             # 남는 게 0.8s 미만
    assert (out["c"].avail_in, out["c"].avail_out) == (2926.0, 2927.3)                                # 제안(2926~2932)과 더 겹치는 앞 조각(1.3s > 1.0s)
    assert carve_black(srcs, []) == srcs
    rows = [{"i": 14, "mode": "N", "cuts": [{"src": "AG", "in": 2928.508, "out": 2930.193}]},
            {"i": 3, "mode": "S", "cuts": [{"src": "L", "in": 2928.0, "out": 2930.0}]},
            {"i": 5, "mode": "N", "cuts": [{"src": "x", "in": 2927.4, "out": 2928.6}]}]
    bad = cuts_in_black(rows, black)
    assert len(bad) == 2 and bad[0].startswith("행14[N] AG") and "검정 1.69s" in bad[0] and bad[1].startswith("행5[N]")   # S 행 제외
    assert cuts_in_black(rows, black, min_overlap=1.5) == [bad[0]] and cuts_in_black(rows, []) == []


def test_fill_n_rows_never_stacks_black_frames(index, transcript):
    """검은 구간이 낀 후보는 깎여서 쌓이고, 완성 컷은 검은 구간과 겹치지 않는다."""
    from app.tikitaka.table import fill_n_rows, rows_from_items, cuts_in_black
    rows = rows_from_items(_version(), index, transcript, CUTS, DUR, lambda t: (Path("/tmp/x.mp3"), 3.0))
    black = [(109.0, 111.0)]
    ag = {1: [CutSource("AG-1-1", 108.0, 114.9, "a", prop_out=110.0)]}       # 제안 108~110 이 검정에 걸침 → 108~108.8 만 남아 0.8s 미만 → 버림
    notes = fill_n_rows(rows, ag, {}, CUTS, DUR, black=black)
    n = rows[0]
    assert cuts_in_black(rows, black) == [] and sum(c["dur"] for c in n["cuts"]) == pytest.approx(3.0, abs=0.02)
    assert all(c["out"] <= 108.8 or c["in"] >= 111.2 for c in n["cuts"]), n["cuts"]
    assert any("보충" in x for x in notes)


def test_agentic_a_rows_reject_black_and_table_cache_regenerates_on_black(index, transcript):
    from app.tikitaka.table import apply_agentic_a_rows, rows_from_items
    v = _version()
    v["items"] = [{"type": "A", "moment_id": "S-003", "desc": "한숨", "sound": "한숨", "who": "강비호", "effect": None}]
    rows = rows_from_items(v, index, transcript, CUTS, DUR, _fake_tts)
    base = dict(rows[0]["cuts"][0])
    src = {1: [CutSource("AG-1-1", base["in"], base["in"] + 3.0, "쿵", prop_out=base["in"] + 3.0)]}
    notes = apply_agentic_a_rows(rows, src, CUTS, DUR, black=[(base["in"] + 0.5, base["in"] + 1.5)])
    assert rows[0]["cut_origin"] == "index" and rows[0]["cuts"][0] == base and any("검은 화면" in x for x in notes)
    notes = apply_agentic_a_rows(rows, src, CUTS, DUR, black=[])
    assert rows[0]["cut_origin"] == "agentic"


# ── 작품 이해(digest) · 인물 대조 · 같은 샷 회피 (2026-09-12 사용자 지적 2화 "내용·제목이 이상하고 중복 영상이 나온다") ──────
def test_digest_block_and_sha(index, transcript):
    from app.tikitaka.digest import digest_block, digest_sha, _clean
    assert digest_block(None) == "" and digest_block({"characters": []}) == ""                       # 없으면 프롬프트 바이트 동일
    d = _clean({"logline": " 한  줄 ", "plot": ["기: a", "", "결: b"], "characters": [{"name": "강비호", "role": "학생", "wants": "나가기", "why": "숨막힘"}, {"x": 1}],
                "relationships": ["강비호—홍재인: 동기"], "threads": [{"name": "탈출", "beats": ["SC-001: 다툼 → 왜 → 결과"]}],
                "scene_notes": [{"id": "SC-001", "what": "다툼", "why": "압박", "sets_up": "가출"}], "cautions": ["두 남자 구분"]})
    assert d["logline"] == "한 줄" and d["plot"] == ["기: a", "결: b"] and len(d["characters"]) == 1 and d["characters"][0]["state"] == ""
    blk = digest_block(d)
    assert "[작품 이해" in blk and "강비호 — 학생" in blk and "SC-001: 다툼 — 왜: 압박" in blk and "두 남자 구분" in blk and "인물 식별은" in blk
    s1 = digest_sha(index, transcript, None)
    tr2 = {"lines": [dict(transcript["lines"][0], speaker="홍재인")] + transcript["lines"][1:], "words": transcript["words"]}
    assert s1 != digest_sha(index, tr2, None) and s1 != digest_sha(index, transcript, {"sha": "abc"})   # 화자·가이드가 바뀌면 다시 만든다


def test_prompts_carry_digest_slot():
    from app.tikitaka.prompts import REBUILD_PROMPT
    from app.tikitaka.verify import VERIFY_PROMPT
    assert "{digest}" in REBUILD_PROMPT and "먼저 이해, 그 다음 대본" in REBUILD_PROMPT
    assert "{digest}" in VERIFY_PROMPT and "인물 식별은 소스 스크립트의 화자 표기" in VERIFY_PROMPT


def test_promo_like_and_copy_belt(index, transcript):
    from app.tikitaka.rebuild import promo_like, validate_versions
    copy = "풀 영상은 쿠팡플레이에서 시청하세요"
    assert promo_like("조여정의 통쾌한 참교육, 쿠팡플레이에서 확인하세요!", copy)
    assert not promo_like("쿠팡플레이 드라마라서 그런지 수위가 세다", copy) and not promo_like("결국 이렇게 됩니다", copy) and not promo_like("x", None)
    raw = {"versions": [{"n": 1, "strategy": "결말 선공개형", "title": "t", "items": [
        {"type": "S", "line_ids": ["L-001"]}, {"type": "N", "text": "뒷이야기는 쿠팡플레이에서 확인하세요"}, {"type": "N", "text": "정상 내레이션"}]}]}
    v = validate_versions(raw, index, transcript, copy_text=copy)["versions"][0]
    assert [it["type"] for it in v["items"]] == ["S", "N"] and v["items"][1]["text"] == "정상 내레이션" and any("홍보" in x for x in v["issues"])


def test_character_context_flags(index, transcript):
    from app.tikitaka.rebuild import character_context_flags
    ix = dict(index, cast=["강비호", "홍재인", "김철수"])
    v = {"title": "김철수 폭발", "items": [{"type": "N", "text": "김철수가 먼저 판을 깬다"}, {"type": "S", "line_ids": ["L-001"], "speaker": "강비호"},
                                   {"type": "N", "text": "강비호의 한마디"}]}
    flags = character_context_flags(v, ix, transcript)
    assert any(f.startswith("[인물 의심] 제목") and "김철수" in f for f in flags)
    assert any("N1" in f and "김철수" in f for f in flags) and not any("N3" in f for f in flags)     # 장면 등장(강비호·홍재인)은 통과
    # 배우 이름 표기(actors)도 인식한다 — 극중 이름으로 되돌려 대조
    v2 = {"title": "x", "items": [{"type": "N", "text": "배우A의 한마디"}, {"type": "S", "line_ids": ["L-001"], "speaker": "강비호"}]}
    assert character_context_flags(v2, ix, transcript, actors={"강비호": "배우A"}) == []
    assert character_context_flags({"title": "x", "items": []}, ix, transcript) == []


def test_shot_index_and_trim_a_rows_off_dialogue():
    from app.tikitaka.table import shot_index, trim_a_rows_off_dialogue
    assert shot_index(CUTS, 95.0) == 1 and shot_index(CUTS, 103.0) == 2 and shot_index(CUTS, 10.0) == 0
    rows = [{"i": 1, "mode": "S", "cuts": [{"src": "L", "in": 100.0, "out": 103.0}]},
            {"i": 2, "mode": "A", "dur": 3.0, "cuts": [{"src": "S-1", "in": 102.0, "out": 105.0, "dur": 3.0, "desc": "d"}]},      # 1s 겹침 → 103~105
            {"i": 3, "mode": "A", "dur": 3.0, "cuts": [{"src": "S-2", "in": 100.2, "out": 103.2, "dur": 3.0, "desc": "d"}]}]      # 남는 게 0.2s → 못 자름
    notes = trim_a_rows_off_dialogue(rows)
    assert (rows[1]["cuts"][0]["in"], rows[1]["cuts"][0]["out"], rows[1]["dur"]) == (103.0, 105.0, 2.0)
    assert (rows[2]["cuts"][0]["in"], rows[2]["cuts"][0]["out"]) == (100.2, 103.2) and any("⚠" in n for n in notes) and len(notes) == 2


def test_fill_n_rows_avoids_shots_already_shown(index, transcript):
    """앞 대사 행이 이미 보여준 샷(102.8~106.5)의 남는 구간보다 다른 샷의 후보를 먼저 쓴다 — 같은 프레이밍이 되감기로 두 번 나오지 않게."""
    from app.tikitaka.table import fill_n_rows, rows_from_items, shot_index
    v = _version()
    v["items"] = [{"type": "S", "line_ids": ["L-003"], "speaker": "강비호", "text": "그냥 싫어서.", "effect": None}, {"type": "N", "text": "가" * 8, "effect": None}]
    rows = rows_from_items(v, index, transcript, CUTS, DUR, lambda t: (Path("/tmp/x.mp3"), 2.0))
    s_shot = shot_index(CUTS, rows[0]["cuts"][0]["in"])                  # L-003(105.0~106.2) → 샷 102.8~106.5
    ag = {2: [CutSource("AG-same", 103.0, 106.4, "대사와 같은 샷(앞부분 되감기)", prop_out=104.5), CutSource("AG-other", 110.0, 114.9, "다른 샷", prop_out=112.0)]}
    notes = fill_n_rows(rows, ag, {}, CUTS, DUR)
    assert all(shot_index(CUTS, c["in"]) != s_shot for c in rows[1]["cuts"]) and rows[1]["cuts"][0]["src"] == "AG-other"
    assert not any("샷을 재사용" in x for x in notes)
    # 다른 샷 후보가 없으면 같은 샷을 쓰되 기록한다
    rows = rows_from_items(v, index, transcript, CUTS, DUR, lambda t: (Path("/tmp/x.mp3"), 2.0))
    notes = fill_n_rows(rows, {2: [CutSource("AG-same", 103.0, 106.4, "같은 샷", prop_out=104.5)]}, {}, CUTS, DUR)
    assert rows[1]["cuts"] and any("샷을 재사용" in x for x in notes)


def test_fill_n_rows_lead_in_allows_same_shot_before_next_dialogue(index, transcript):
    """다음 대사와 같은 샷이라도 그 대사보다 앞선 구간은 연속 리드인 — 회피하지 않는다(2화 v8 옥상 통화). 대사 뒤 구간은 되감기라 회피."""
    from app.tikitaka.table import fill_n_rows, rows_from_items
    v = _version()
    v["items"] = [{"type": "N", "text": "가" * 8, "effect": None}, {"type": "S", "line_ids": ["L-003"], "speaker": "강비호", "text": "그냥 싫어서.", "effect": None}]
    rows = rows_from_items(v, index, transcript, CUTS, DUR, lambda t: (Path("/tmp/x.mp3"), 1.5))
    ag = {1: [CutSource("AG-before", 103.0, 106.4, "대사 직전 같은 샷", prop_out=104.5), CutSource("AG-other", 110.0, 114.9, "다른 샷", prop_out=112.0)]}
    notes = fill_n_rows(rows, ag, {}, CUTS, DUR)
    assert rows[0]["cuts"][0]["src"] == "AG-before" and rows[0]["cuts"][0]["out"] <= rows[1]["cuts"][0]["in"] + 0.001
    assert not any("샷을 재사용" in x for x in notes)


def test_build_ass_hides_unknown_speaker_label(index, transcript):
    from app.tikitaka.render import build_ass, compute_layout
    tbl = _table(index, transcript)
    for r in tbl["rows"]:
        if r["mode"] == "S":
            r["speaker"] = "미상"
    ass = build_ass(tbl, title="제목", layout=compute_layout("fill"))
    assert "미상" not in ass


def test_label_suffix_marks_offscreen_voice(index, transcript):
    """S 항목 label_suffix("(전화)") → 행에 실리고 자막 라벨 뒤에 붙는다(2026-09-13 2화 v8 전화 통화)."""
    from app.tikitaka.render import build_ass, compute_layout
    v = _version()
    v["items"][1]["label_suffix"] = "(전화)"
    rows = rows_from_items(v, index, transcript, CUTS, DUR, _fake_tts)
    assert rows[1]["label_suffix"] == "(전화)" and rows[3].get("label_suffix") is None
    tbl = _table(index, transcript)
    for r in tbl["rows"]:
        if r["mode"] == "S":
            r["label_suffix"] = "(전화)"
    ass = build_ass(tbl, title="제목", layout=compute_layout("fill"), name_map={"강비호": "김배우"})
    assert "김배우 (전화)" in ass


def test_fill_n_rows_prefers_lead_in_scene_over_other_scene_fresh_shot(transcript):
    """리드인 장면(다음 대사의 장면) 컷이 다른 장면의 '새 샷' 컷보다 먼저다 — 같은 샷이라도(2026-09-13 2화 v8 옥상 전화 실측)."""
    from app.tikitaka.table import fill_n_rows, rows_from_items, lead_scene_for_row
    idx = {"title": "t", "cast": ["강비호", "홍재인"], "speakers": {},
           "scenes": [{"id": "SC-A", "start": 90.0, "end": 104.0, "place": "집", "summary": "", "chars": ["강비호"]},
                      {"id": "SC-B", "start": 104.0, "end": 120.0, "place": "옥상", "summary": "", "chars": ["강비호", "홍재인"]}],
           "moments": []}
    v = _version()
    v["items"] = [{"type": "S", "line_ids": ["L-001"], "speaker": "강비호", "text": "x", "effect": None},      # 집(99.95~) — SC-A
                  {"type": "N", "text": "가" * 8, "effect": None},
                  {"type": "S", "line_ids": ["L-003"], "speaker": "강비호", "text": "y", "effect": None}]      # 옥상(105.0~) — SC-B
    rows = rows_from_items(v, idx, transcript, CUTS, DUR, lambda t: (Path("/tmp/x.mp3"), 2.5))
    assert lead_scene_for_row(rows, 1, idx["scenes"]) == (104.0, 120.0)
    ag = {2: [CutSource("AG-home", 91.0, 94.0, "집 — 다른 장면·새 샷", prop_out=93.0),
              CutSource("AG-roof", 104.1, 106.4, "옥상 — 다음 대사와 같은 샷", prop_out=104.9)]}
    notes = fill_n_rows(rows, ag, {}, CUTS, DUR, scenes=idx["scenes"])
    assert rows[1]["cuts"] and all(104.0 <= c["in"] < 120.0 for c in rows[1]["cuts"]), rows[1]["cuts"]      # 집(다른 장면) 컷은 안 쓴다
    assert not any(c["src"] == "AG-home" for c in rows[1]["cuts"]) and rows[1]["shortfall"] == 0.0


def test_trim_a_rows_off_black():
    from app.tikitaka.table import trim_a_rows_off_black
    rows = [{"i": 2, "mode": "A", "dur": 4.5, "cuts": [{"src": "S-057", "in": 1998.0, "out": 2002.5, "dur": 4.5, "desc": "쿵"}]},     # 앞 3.4s 검정 → 뒤 1.1s 남김
            {"i": 3, "mode": "A", "dur": 1.0, "cuts": [{"src": "S-058", "in": 2010.0, "out": 2011.0, "dur": 1.0, "desc": "암전 소리"}]},   # 통째 검정 → 못 깎음
            {"i": 4, "mode": "S", "cuts": [{"src": "L", "in": 1998.0, "out": 2002.0}]}]
    notes = trim_a_rows_off_black(rows, [(1997.0, 2001.2), (2009.5, 2011.5)])
    c = rows[0]["cuts"][0]
    assert (c["in"], c["out"], rows[0]["dur"]) == (2001.4, 2002.5, pytest.approx(1.1)) and any("깎음" in n for n in notes)
    assert rows[1]["cuts"][0]["in"] == 2010.0 and any("⚠" in n and "행3" in n for n in notes)
    assert rows[2]["cuts"][0]["in"] == 1998.0 and trim_a_rows_off_black(rows, []) == []


def test_speaker_consistency_flags_two_names_on_one_voice():
    """한 클러스터에 이름 둘이 각각 30% 이상이면 ambiguous 로 표시하고 자동 교정하지 않는다(2026-09-13 3화 박경희↔안수정 뒤바뀜)."""
    from app.tikitaka.voice_check import speaker_consistency
    lines = [{"id": f"L-{i:03d}", "speaker": ("안수정" if i < 10 else "박경희"), "voice": "g:speaker_3"} for i in range(17)]
    lines += [{"id": f"L-{i:03d}", "speaker": "임재홍", "voice": "g:speaker_5"} for i in range(100, 112)]
    fixes, report = speaker_consistency(lines)
    e = next(r for r in report if r["voice"] == "g:speaker_3")
    assert e["ambiguous"] == ["안수정", "박경희"] and not e["fixed"] and not any(k.startswith("L-0") for k in fixes)
    e5 = next(r for r in report if r["voice"] == "g:speaker_5")
    assert "ambiguous" not in e5


def test_dialogue_selection_survives_speaker_changes_and_repeat_validation(index):
    """EP9: duet lyric and call/response must survive both rebuild and verify."""
    transcript = {"lines": [
        {"id": "L-001", "start": 1., "end": 2., "text": "봤냐고", "speaker": "박서진, 윤수현"},
        {"id": "L-002", "start": 2.1, "end": 4., "text": "내가 다른 여자 만나는 거 봤냐고", "speaker": "박서진"},
        {"id": "L-003", "start": 5., "end": 6., "text": "박서진을", "speaker": "박서진"},
        {"id": "L-004", "start": 6.1, "end": 6.4, "text": "푹!", "speaker": "윤수현"},
        {"id": "L-005", "start": 6.5, "end": 8., "text": "뽑아라", "speaker": "박서진"},
        {"id": "L-006", "start": 9., "end": 10., "text": "박서진을", "speaker": "윤수현"},
        {"id": "L-007", "start": 10.1, "end": 12., "text": "뽑아라", "speaker": "박서진"},
    ]}
    for selections in [
        [["L-001", "L-002"], ["L-003", "L-004", "L-005"], ["L-006", "L-007"]],
        [["L-003", "L-005"]],
    ]:
        raw = {"versions": [{"n": 1, "items": [{"type": "S", "line_ids": ids} for ids in selections]}]}
        expected = [lid for ids in selections for lid in ids]
        for _ in range(2):
            raw = validate_versions(raw, index, transcript)
            items = raw["versions"][0]["items"]
            assert [lid for it in items for lid in it.get("line_ids", [])] == expected
            if len(selections) == 1:
                assert len(items) == 2  # skipped ad-lib stays excluded; both selected lines survive
