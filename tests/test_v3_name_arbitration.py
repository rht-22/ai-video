"""인명 대조 교정(2026-09-03) — whisper 어절 vs 모델 청취, 인물표가 심판. 순수 · LLM 0콜.

실사고(EP01 94a86e4c): whisper '임지영이(0.75)', 모델 heard_text '임재홍, 이 개새끼.',
인물표 [박경희, 임재홍, …] → 화면엔 '임지영이'. 두 증인이 한 이름을 가리킬 때만 뒤집고,
애매하면(후보 0 또는 2+) 손대지 않는다.
"""
from app.v3 import textcheck as tc
from app.v3 import assemble

NAMES = ["박경희", "임재홍", "안수정", "주보성", "임예지", "주민서"]


def test_real_case_is_fixed_with_josa_kept():
    assert tc.arbitrate_name("임지영이", NAMES, "임재홍, 이 개새끼.") == "임재홍이"
    assert tc.split_josa("임지영이") == ("임지영", "이")
    assert tc.split_josa("경희야") == ("경희", "야") or tc.split_josa("경희야") == ("경희야", "")  # 2음절은 안 뗀다


def test_no_fix_without_second_witness_or_when_ambiguous():
    # 모델 청취에 이름이 없으면 손대지 않는다
    assert tc.arbitrate_name("임지영이", NAMES, "당신, 이 개새끼.") is None
    # 청취에 두 후보(임재홍·임예지)가 다 있으면 고르지 않는다
    assert tc.arbitrate_name("임지영이", NAMES, "임재홍이 임예지한테") is None
    # 인물표에 정확히 있는 이름은 건드리지 않는다
    assert tc.arbitrate_name("임재홍이", NAMES, "임재홍") is None
    # 거리가 멀면(3+) 후보가 아니다
    assert tc.arbitrate_name("김철수가", NAMES, "임재홍") is None


def test_fix_span_words_and_word_subtitles_wiring():
    words = [{"t0": 1.0, "t1": 1.5, "text": "임지영이"}, {"t0": 1.5, "t1": 1.8, "text": "너"}]
    fixed, fixes = tc.fix_span_words(words, NAMES, "임재홍, 이 개새끼.")
    assert fixed[0]["text"] == "임재홍이" and fixed[1]["text"] == "너" and words[0]["text"] == "임지영이"
    assert fixes == [{"at": 1.0, "from": "임지영이", "to": "임재홍이"}]

    idx = {"sp1": {"t_in": 1.0, "t_out": 2.0, "is_audio": True, "text_source": "transcript",
                   "heard_text": "임재홍, 이 개새끼.", "audio_script": [{"speaker": "박경희", "line": "x"}],
                   "pos": 0, "importance": 3}}
    tl = [{"clip_start_sec": 1.0, "clip_end_sec": 2.0, "span_ids": ["sp1"], "use_original_audio": True}]
    log: list = []
    segs = assemble.word_subtitles(tl, idx, words, None, cast_names=NAMES, name_fix_log=log)
    assert "임재홍이" in " ".join(s["text"] for s in segs) and log[0]["span_id"] == "sp1"
    # cast_names 없어도 모델 청취가 있으면 정렬 교정(arbitrate_aligned, 2026-09-04)이
    # 같은 답을 낸다 — 청취까지 없으면 종전 그대로
    segs0 = assemble.word_subtitles(tl, idx, words, None)
    assert "임재홍이" in " ".join(s["text"] for s in segs0)
    idx_blind = {"sp1": dict(idx["sp1"], heard_text="")}
    segs1 = assemble.word_subtitles(tl, idx_blind, words, None)
    assert "임지영이" in " ".join(s["text"] for s in segs1)


def test_pipeline_defines_name_arb_before_use():
    """2026-09-03 실런 크래시(UnboundLocalError): step('story') 가 자막 블록보다 먼저 찍히는데
    그 기록에 _name_arb 를 붙였다. 정의 줄이 모든 사용 줄보다 앞서야 한다(AST 고정)."""
    import ast, pathlib
    tree = ast.parse(pathlib.Path("app/v3/pipeline.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_m3")
    stores, loads = [], []
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and n.id == "_name_arb":
            (stores if isinstance(n.ctx, ast.Store) else loads).append(n.lineno)
    assert stores and loads and min(stores) < min(loads)
    # story 기록에는 안 붙는다(자막 전이라 늘 비어 있을 자리)
    src = pathlib.Path("app/v3/pipeline.py").read_text(encoding="utf-8")
    assert 'step("story", elapsed=round(time.time() - t0, 1), name_arbitrations' not in src
    assert 'step("subtitles", lines=len(segments), name_arbitrations=_name_arb)' in src
