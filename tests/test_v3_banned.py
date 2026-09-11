"""권리사 활용 불가 구간(app/v3/banned.py, 2026-09-10) 회귀 가드.

  · 입력: mm:ss·초·"end" 해석, 모르는 키·역순·회차 키 오류 즉시 실패, 회차 미지정 즉시 실패
  · 장면 확장: 부분 겹침도 사건 단위 통째(exclude-range 의 50% 규칙과 다르다)
  · 증인: 이웃 단위는 키워드가 있을 때만 · 어디에도 없으면 경고 + 이웃까지 확대(과잉 차단)
  · span: 금지 단위의 span + 커버리지 밖 span(시각 겹침) 전부
  · 벨트: 클립(덮개 포함)이 구간·span 에 닿으면 전부 열거 + ValueError
  · 배선: 템플릿 jigeum.json 로드 · human 흐름 색인 제거 + 제외 집합 · legacy 색인 제거 · CLI
"""
from __future__ import annotations

import json

import pytest

from app.v3 import banned as bn

# 사건 단위 4개(초): m0 0~30 호텔 전 · m1 30~60 호텔 · m2 60~100 복도 · m3 100~130 병원
ROWS = [
    {"idx": 0, "t0": 0.0, "t1": 30.0, "content": "수정이 호텔 로비에 들어선다", "characters": ["수정"],
     "span_ids": ["sp0", "sp1"]},
    {"idx": 1, "t0": 30.0, "t1": 60.0, "content": "객실에서 두 사람이 마주 앉는다", "characters": ["수정", "재홍"],
     "span_ids": ["sp2", "sp3"]},
    {"idx": 2, "t0": 60.0, "t1": 100.0, "content": "복도를 걷는 경희", "characters": ["경희"],
     "span_ids": ["sp4"]},
    {"idx": 3, "t0": 100.0, "t1": 130.0, "content": "병원 진료실", "characters": ["의사"],
     "span_ids": ["sp5"]},
]
SPANS = [{"id": f"sp{i}", "t_in": a, "t_out": z} for i, (a, z) in enumerate(
    [(0, 15), (15, 30), (30, 45), (45, 60), (60, 100), (100, 130), (130, 140)])]   # sp6 = 커버리지 밖


def test_parse_ts_flex_and_doc_fail_loud():
    assert bn.parse_ts_flex("22:06") == 1326.0
    assert bn.parse_ts_flex("1:02:03.5") == 3723.5
    assert bn.parse_ts_flex(12) == 12.0 and bn.parse_ts_flex("7.5") == 7.5
    for bad in ("", "a:b", "1:2:3:4", -1):
        with pytest.raises(bn.BannedError):
            bn.parse_ts_flex(bad)
    doc = bn.parse_banned_doc({"_doc": "x", "2": [{"t0": "22:06", "t1": "end", "what": "호텔 씬"}]})
    assert doc == {"2": [{"t0": 1326.0, "t1": None, "what": "호텔 씬", "keywords": []}]}
    for bad in ({"2": [{"t0": "10:00", "t1": "09:00", "what": "x"}]},           # 역순
                {"2": [{"t0": "1:00", "t1": "2:00", "what": "x", "nope": 1}]},   # 모르는 키
                {"두번째": []},                                                   # 회차 키
                {"2": [{"t0": "1:00", "t1": "2:00"}]},                           # 설명도 키워드도 없음
                {"2": {"t0": 1}}):                                               # 배열 아님
        with pytest.raises(bn.BannedError):
            bn.parse_banned_doc(bad)
    assert bn.parse_banned_doc(None) == {}
    merged = bn.merge_banned(doc, {"2": [{"t0": 1.0, "t1": 2.0, "what": "b", "keywords": []}], "3": []})
    assert len(merged["2"]) == 2 and merged["3"] == []
    assert bn.banned_for_episode(merged, 2) == merged["2"] and bn.banned_for_episode(merged, 4) == []
    with pytest.raises(bn.BannedError):
        bn.banned_for_episode(merged, None)             # 목록이 있는데 회차를 모른다
    assert bn.banned_for_episode({}, None) == []       # 목록이 없으면 회차 없어도 무해


def test_keywords_from_what_drop_generic_words():
    assert bn.keywords_of({"what": "수정&재홍 호텔 씬 일체", "keywords": []}) == ["수정", "재홍", "호텔"]
    assert bn.keywords_of({"what": "x", "keywords": ["USB", "영상"]}) == ["USB", "영상"]


def test_partial_overlap_bans_whole_unit_unlike_exclude_range():
    # 구간 40~50 은 m1(30~60)의 33% — exclude-range 라면 통과하지만 여기는 통째 차단
    res = bn.resolve_banned([{"t0": 40.0, "t1": 50.0, "what": "호텔 씬", "keywords": ["객실"]}],
                            ROWS, SPANS, 140.0)
    assert res["units"] == [1]
    assert set(res["span_ids"]) == {"sp2", "sp3"}
    assert res["intervals"] == [[30.0, 60.0]]          # 차단 구간은 사건 단위로 넓어진다
    assert res["warnings"] == []


def test_neighbor_unit_joins_only_with_keyword_witness():
    # 가이드 시각이 30초 늦게 적혔다(실제 호텔 장면은 m1 30~60, 구간은 60~70 = m2 만 직접 겹침)
    res = bn.resolve_banned([{"t0": 60.0, "t1": 70.0, "what": "호텔 씬", "keywords": ["호텔", "객실"]}],
                            ROWS, SPANS, 140.0)
    assert 2 in res["units"]                            # 직접 겹침
    assert 0 in res["units"] and 1 in res["units"]      # 이웃 중 '호텔'·'객실'이 있는 단위만 증인으로 합류
    assert 3 not in res["units"]                        # 병원(키워드 없음)은 안 잡힌다
    assert res["items"][0]["witness"] == [0, 1] and res["items"][0]["widened"] == []
    assert res["warnings"] == []


def test_no_witness_anywhere_warns_and_widens_to_neighbors():
    res = bn.resolve_banned([{"t0": 60.0, "t1": 70.0, "what": "USB 영상", "keywords": ["USB"]}],
                            ROWS, SPANS, 140.0, pad=45.0)
    assert 2 in res["units"]
    assert res["items"][0]["widened"] == [0, 1, 3]      # pad 45 → 창 15~115 안의 이웃(m0·m1·m3) 전부
    assert len(res["warnings"]) == 1 and "시간축 불일치" in res["warnings"][0]


def test_end_and_coverage_gap_spans_are_banned_by_time():
    res = bn.resolve_banned([{"t0": 125.0, "t1": None, "what": "엔딩 반전", "keywords": ["병원"]}],
                            ROWS, SPANS, 140.0)
    assert res["units"] == [3]
    assert "sp6" in res["span_ids"]                     # 사건 단위 밖(커버리지 밖) span 도 시각으로 차단
    assert res["intervals"] == [[100.0, 140.0]]


def test_belt_lists_every_offending_clip_including_covers():
    res = bn.resolve_banned([{"t0": 40.0, "t1": 50.0, "what": "호텔 씬", "keywords": ["객실"]}],
                            ROWS, SPANS, 140.0)
    timeline = [
        {"role": "hook", "clip_start_sec": 5.0, "clip_end_sec": 12.0, "span_ids": ["sp0"]},          # 안전
        {"role": "build", "clip_start_sec": 58.0, "clip_end_sec": 62.0, "span_ids": ["sp3", "sp4"]},  # 구간 접촉
        {"role": "build", "clip_start_sec": 61.0, "clip_end_sec": 64.0, "span_ids": ["sp3"],         # span 만 걸림
         "cover": "designated"},
        {"role": "end", "clip_start_sec": 60.0, "clip_end_sec": 70.0, "span_ids": ["sp4"]},           # 경계 접촉 = 무해
    ]
    bad = bn.violations(timeline, res)
    assert [b["clip"] for b in bad] == [1, 2]
    assert bad[1]["cover"] == "designated" and bad[1]["span_ids"] == ["sp3"]
    logs: list[str] = []
    with pytest.raises(ValueError, match="위반 2건"):
        bn.enforce(timeline, res, where="t", log=logs.append)
    assert len(logs) == 2
    bn.enforce(timeline[:1], res, where="t")           # 위반 없음 = 조용
    assert bn.violations(timeline, None) == []


def test_banned_block_prompt_and_empty():
    assert bn.banned_block(None) == "" and bn.banned_block({"items": []}) == ""
    res = bn.resolve_banned([{"t0": 40.0, "t1": 50.0, "what": "호텔 씬", "keywords": ["객실"]}],
                            ROWS, SPANS, 140.0)
    blk = bn.banned_block(res)
    assert "권리사 활용 불가" in blk and "호텔 씬" in blk and "00:30.00~01:00.00" in blk


# ── 배선 ─────────────────────────────────────────────────────────────────────

def test_jigeum_template_carries_guideline_banned_ranges():
    from app.v3.cli import load_design_preset
    preset = load_design_preset("jigeum")
    b = preset["banned"]
    assert set(b) == {"2", "3", "4"} and "1" not in b       # 1화는 시각 구간 없음
    assert len(b["2"]) == 5 and len(b["3"]) == 5 and len(b["4"]) == 2
    ep2 = b["2"]
    assert ep2[0]["t0"] == 22 * 60 + 6 and ep2[0]["t1"] == 22 * 60 + 50
    assert ep2[-1]["t1"] is None and "아저씨" in ep2[-1]["keywords"]   # 44:00~엔딩
    assert b["4"][0]["t0"] == 37 * 60 + 6 and "USB" in b["4"][0]["keywords"]


def test_cli_requires_episode_when_banned_present_and_merges_inline(tmp_path, monkeypatch):
    from app.v3 import cli
    (tmp_path / "t.json").write_text(json.dumps(
        {"design": {}, "banned": {"2": [{"t0": "1:00", "t1": "2:00", "what": "호텔"}]}}), encoding="utf-8")
    p = cli.load_design_preset("t", base_dir=tmp_path)
    assert p["banned"]["2"][0]["t0"] == 60.0
    (tmp_path / "bad.json").write_text(json.dumps({"banned": {"2": [{"t0": "2:00", "t1": "1:00", "what": "x"}]}}),
                                       encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.load_design_preset("bad", base_dir=tmp_path)
    merged = bn.merge_banned(p["banned"], bn.parse_banned_doc({"2": [{"t0": 5, "t1": 6, "what": "y"}]}))
    assert len(merged["2"]) == 2
    with pytest.raises(bn.BannedError):
        bn.banned_for_episode(merged, None)


def _stage2_and_grid():
    def span(i, a, z, heard="", scene=""):
        return {"number": i, "span_id": f"sp{i}", "time": {"start": _ts(a), "end": _ts(z)},
                "is_audio": bool(heard), "audio_script": [], "heard_text": heard, "conf": None,
                "text_source": None, "scene_script": scene, "characters": [], "importance": 3,
                "time_authority": "stt"}
    meanings = [
        {"number": 0, "time": {"start": _ts(0), "end": _ts(30)}, "content": "수정이 호텔 로비에 들어선다",
         "characters": ["수정"], "importance": 3, "mood": "", "spans": [span(0, 0, 15, "가요"), span(1, 15, 30, "", "로비")]},
        {"number": 1, "time": {"start": _ts(30), "end": _ts(60)}, "content": "객실에서 마주 앉는다",
         "characters": ["수정", "재홍"], "importance": 4, "mood": "", "spans": [span(2, 30, 45, "앉아"), span(3, 45, 60, "왜")]},
        {"number": 2, "time": {"start": _ts(60), "end": _ts(100)}, "content": "복도를 걷는 경희",
         "characters": ["경희"], "importance": 3, "mood": "", "spans": [span(4, 60, 100, "어디 가")]},
    ]
    stage2 = {"schema": "v3.stage2/1", "sequences": [{"number": 1, "time": {"start": _ts(0), "end": _ts(100)},
                                                       "content": "s", "chunks": [{"number": 0, "time": {"start": _ts(0), "end": _ts(100)},
                                                                                   "meanings": meanings}]}]}
    grid = {"span_candidates": [{"id": f"sp{i}", "t_in": a, "t_out": z, "is_audio": True}
                                for i, (a, z) in enumerate([(0, 15), (15, 30), (30, 45), (45, 60), (60, 100)])],
            "words": [], "silence": [], "duration_sec": 100.0, "source": {"fps": 24.0}}
    return stage2, grid


def _ts(s: float) -> str:
    m = int(s // 60)
    return f"00:{m:02d}:{s - m * 60:06.3f}"


def test_pipeline_resolver_uses_stage2_witness_text():
    from app.v3.pipeline import _resolve_banned_for_job
    stage2, grid = _stage2_and_grid()
    logs: list[str] = []
    res = _resolve_banned_for_job(stage2, grid, [{"t0": 62.0, "t1": 65.0, "what": "호텔 씬", "keywords": ["호텔", "객실"]}],
                                  log=logs.append)
    assert res["units"] == [0, 1, 2] and set(res["span_ids"]) == {"sp0", "sp1", "sp2", "sp3", "sp4"}
    assert any("차단" in l for l in logs)


def test_legacy_run_story_and_human_flow_drop_banned_spans_from_index():
    from app.v3 import story as st
    stage2, grid = _stage2_and_grid()
    idx, order = st.build_span_index(stage2, grid)
    assert "sp2" in idx
    # legacy: 색인에서 빠진 span 은 프롬프트 재료·폴백 어디에도 없다 — build_material_block 이 색인만 본다
    from app.v3.story import build_material_block
    full = build_material_block(stage2, idx)
    kept = build_material_block(stage2, {k: v for k, v in idx.items() if k not in {"sp2", "sp3"}})
    assert "sp2" in full and "sp2" not in kept and "sp0" in kept
    # human: banned 가 있으면 excluded 집합과 프롬프트 블록에 합류(검증기가 형식으로 반려)
    from app.v3.story_flow import select as sl
    rows = [{"idx": 0, "t0": 0, "t1": 30, "content": "a", "characters": [], "span_ids": ["sp0"]},
            {"idx": 1, "t0": 30, "t1": 60, "content": "b", "characters": [], "span_ids": ["sp2"]}]
    obj, pr = sl.validate_topic({"topic": "t", "core_meanings": ["m001"]}, rows, excluded={1})
    assert obj is None and any("제외" in p for p in pr)


def test_run_story_signature_accepts_banned_span_ids_and_flow_kwarg():
    import inspect
    from app.v3 import story as st
    from app.v3.story_flow import run_story_flow
    assert "banned_span_ids" in inspect.signature(st.run_story).parameters
    assert "banned" in inspect.signature(run_story_flow).parameters
    from app.v3 import plan as pl
    assert "banned" in inspect.signature(pl.run_plan).parameters
