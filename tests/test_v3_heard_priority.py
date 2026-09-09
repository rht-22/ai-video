"""저확신 전사 → Stage 2 청취 우선 (2026-09-08, 가왕쇼 ep7ex02 실사고) — 회귀 가드.

  · 평균 prob < 0.6 · 청취가 다르고 요약이 아닐 때만 청취를 쓴다(건별 기록 kind="heard")
  · 확신이 높거나 · 청취가 같거나 · 청취가 whisper 의 60% 미만(요약)이면 종전 whisper 그대로
  · text_source 가 이미 heard 인 span 은 기록 없이 종전 경로
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v3_stage3 import _mk_grid, _mk_stage2  # noqa: E402

from app.v3 import assemble, story as st  # noqa: E402


def _words(t0, t1, text, prob):
    toks = text.split(); d = (t1 - t0) / len(toks)
    return [{"text": w, "t0": round(t0 + i * d, 3), "t1": round(t0 + (i + 1) * d, 3), "prob": prob}
            for i, w in enumerate(toks)]


def test_prefer_heard_rules():
    lo = _words(0, 3, "아니 모델 2명만 젖었잖아", 0.4)
    assert assemble.prefer_heard(lo, "아니 모델 두 명이면 더 좋잖아")["heard"] == "아니 모델 두 명이면 더 좋잖아"
    assert assemble.prefer_heard(_words(0, 3, "아니 모델 2명만 젖었잖아", 0.9), "아니 모델 두 명이면") is None  # 확신 높음
    assert assemble.prefer_heard(lo, "아니모델 2명만 젖었잖아") is None          # 띄어쓰기만 다름
    assert assemble.prefer_heard(_words(0, 2, "어 예뻐.", 0.4), "어, 예뻐.") is None   # 구두점만 다름
    assert assemble.prefer_heard(_words(0, 3, "약간 놀랐잖아 밥 먹고 왔습니다 네 먹었습니다", 0.5), "밥 먹고 왔어?") is None  # 요약
    assert assemble.prefer_heard(lo, "") is None
    assert assemble.prefer_heard(lo[:1], "다른 문장") is None                      # 단어 1개


def test_word_subtitles_uses_heard_for_low_confidence_span_and_logs():
    grid = _mk_grid([(0.0, 3.0, True, "아니 모델 2명만 젖었잖아"), (3.0, 5.0, True, "고맙습니다")])
    grid["words"] = _words(0, 3, "아니 모델 2명만 젖었잖아", 0.4) + _words(3, 5, "고맙습니다 형", 0.95)
    s2 = _mk_stage2(grid, [(0, 1, 4, "대기실")])
    # Stage 2 청취(heard_text)를 span 문서에 심는다
    for seq in s2["sequences"]:
        for ch in seq["chunks"]:
            for m in ch["meanings"]:
                for sp in m["spans"]:
                    sp["text_source"] = "transcript"
                    sp["heard_text"] = ("아니 모델 두 명이면 더 좋잖아" if sp["span_id"] == "sp0000"
                                        else "고맙습니다 형")
    idx, _ = st.build_span_index(s2, grid)
    tl = [{"clip_start_sec": 0.0, "clip_end_sec": 5.0, "use_original_audio": True,
           "span_ids": ["sp0000", "sp0001"]}]
    log: list[dict] = []
    segs = assemble.word_subtitles(tl, idx, grid["words"], name_fix_log=log)
    text = " ".join(s["text"] for s in segs)
    assert "두 명이면 더 좋잖아" in text and "젖었잖아" not in text
    assert "고맙습니다 형" in text                                   # 확신 높은 span 은 whisper 그대로
    assert [f["kind"] for f in log] == ["heard"] and log[0]["span_id"] == "sp0000"
    assert log[0]["mean_prob"] == 0.4


def test_scene_backed_heard_rules():
    """화면 묘사 증인(2026-09-09, 「꼬무줄」 실사고) — span 단위 폴백. 청취의 다른 어절 어간이 같은 조각의
    Stage 2 화면 묘사에 있으면(whisper 에는 없음) 청취를 쓴다. 문장 유사도 ≥0.5(단어 시비이지 문장 교체 아님)."""
    ws = _words(0, 4, "아무것도 없어요 저희 꼬물들밖에 없습니다 티켓 묶어놓은 꼬물들밖에 없습니다", 0.7)
    heard = "지금 여기 봐 아무것도 없어요 저희 고무줄밖에 없어요 티켓 묶어놓은 고무줄밖에"
    scene = "홍지윤이 가방을 열어보이며 티켓을 묶어놨던 고무줄만 남았다고 보여준다."
    hit = assemble.scene_backed_heard(ws, heard, scene)
    assert hit and hit["heard"] == heard and hit["stem"].startswith("고무") and hit["similarity"] >= 0.5
    # 저확신 우선은 이 span 을 못 잡는다(평균 0.7) — 두 규칙이 서로 다른 구멍을 막는다
    assert assemble.prefer_heard(ws, heard) is None
    # 양쪽에 다 있는 단어(티켓)는 증거가 아니다 · 화면 묘사가 비면 None · 같은 텍스트면 None
    assert assemble.scene_backed_heard(ws, "저희 티켓 묶어놓은 것밖에 없습니다", "티켓을 나눠준다") is None
    assert assemble.scene_backed_heard(ws, heard, "") is None
    assert assemble.scene_backed_heard(ws, " ".join(w["text"] for w in ws), scene) is None
    # 지시어는 어간으로 안 친다 · 요약(길이 60% 미만)은 None
    assert assemble.scene_backed_heard(ws, "지금 여기 봐 아무것도 없어요 저희 꼬물들밖에 없습니다 티켓 묶어놓은 꼬물들밖에 없습니다",
                                       "지금 여기 가방") is None
    assert assemble.scene_backed_heard(ws, "고무줄뿐", scene) is None
    # 전혀 다른 문장(유사도 < 0.5)은 화면 묘사에 단어가 있어도 안 바꾼다(「인천의 아들입니다」→「전유진 많이 투표해 주세요」)
    assert assemble.scene_backed_heard(_words(0, 2, "인천의 아들입니다", 0.8), "전유진 많이 투표해 주세요",
                                       "전유진이 투표를 부탁한다") is None
    # 인물 이름은 어간으로 안 친다(화면 묘사에는 인명이 늘 있다)
    assert assemble.scene_backed_heard(_words(0, 2, "홍지연 씨 보고 싶으세요", 0.8), "홍지윤 씨 보고 싶으세요",
                                       "홍지윤이 무대에 선다", exclude={"홍지윤"}) is None


def test_arbitrate_scene_word_level_keeps_timing():
    """어절 단위 화면 묘사 증인(textcheck.arbitrate_scene): 자모 차이 4 라 aligned 가 거절한 「꼬물들밖에」를
    화면 묘사(고무줄)가 뒤집는다 — 그 어절만 바뀌고 나머지 whisper 타임코드는 산다."""
    from app.v3 import textcheck as tc
    ws = _words(0, 4, "저희 꼬물들밖에 없습니다 티켓 묶어놓은 꼬물들밖에 없습니다", 0.7)
    heard = "저희 고무줄밖에 없어요 티켓 묶어놓은 고무줄밖에"
    scene = "티켓을 묶어놨던 고무줄만 남았다"
    pieces = tc.align_tokens_to_heard([w["text"] for w in ws], heard)
    assert tc.arbitrate_aligned("꼬물들밖에", pieces[1], prob=0.7) is None          # 자모 차이 상한 초과
    assert tc.arbitrate_scene("꼬물들밖에", pieces[1], scene_script=scene, span_text="저희 꼬물들밖에") == ("고무줄밖에", "고무줄")
    assert tc.arbitrate_scene("꼬물들밖에", pieces[1], scene_script="가방을 연다", span_text="저희 꼬물들밖에") is None
    assert tc.arbitrate_scene("꼬물들밖에", pieces[1], scene_script=scene, span_text="고무줄 꼬물들밖에") is None  # whisper 에 이미 있음
    out, fx = tc.fix_span_words(ws, [], heard, scene_script=scene)
    assert [w["text"] for w in out] == ["저희", "고무줄밖에", "없습니다", "티켓", "묶어놓은", "고무줄밖에", "없습니다"]
    assert [(f["kind"], f["stem"]) for f in fx] == [("scene", "고무줄"), ("scene", "고무줄")]
    assert [w["t0"] for w in out] == [w["t0"] for w in ws]                          # 타임코드 불변
    assert tc.fix_span_words(ws, [], heard)[1] == []                                # scene_script 없으면 종전(회귀 0)
    assert tc.scene_stem("고무줄밖에", scene, "꼬물들") == "고무줄" and tc.scene_stem("티켓", "티켓을 준다", "티켓") is None


def test_word_subtitles_uses_scene_witness_word_level_then_span_fallback():
    grid = _mk_grid([(0.0, 4.0, True, "저희 꼬물들밖에 없습니다"), (4.0, 6.0, True, "저희 산맥장 글고 왔어요")])
    grid["words"] = _words(0, 4, "저희 꼬물들밖에 없습니다", 0.7) + _words(4, 6, "저희 산맥장 글고 왔어요", 0.8)
    s2 = _mk_stage2(grid, [(0, 1, 4, "완판")])
    for seq in s2["sequences"]:
        for ch in seq["chunks"]:
            for m in ch["meanings"]:
                for sp in m["spans"]:
                    sp["text_source"] = "transcript"
                    if sp["span_id"] == "sp0000":
                        sp["heard_text"] = "저희 고무줄밖에 없어요"
                        sp["scene_script"] = "티켓을 묶었던 고무줄만 남았다"
                    else:
                        sp["heard_text"] = "저희 300장 들고 왔어요"          # 「산맥장」(3)↔「300장」(4) — 정렬 불가
                        sp["scene_script"] = "홍지윤이 300장을 들고 왔는데 다 나갔다며 감탄한다"
    idx, _ = st.build_span_index(s2, grid)
    tl = [{"clip_start_sec": 0.0, "clip_end_sec": 6.0, "use_original_audio": True,
           "span_ids": ["sp0000", "sp0001"]}]
    log: list[dict] = []
    out = assemble.word_subtitles(tl, idx, grid["words"], name_fix_log=log)
    txt = " ".join(s["text"] for s in out)
    assert "고무줄밖에" in txt and "꼬물들" not in txt and "300장" in txt and "산맥장" not in txt
    kinds = [(f["kind"], f["span_id"]) for f in log]
    assert ("scene", "sp0000") in kinds and ("scene_span", "sp0001") in kinds
    assert not any(k == "scene_span" and s == "sp0000" for k, s in kinds)         # 어절 단위가 잡으면 span 폴백 없음


def test_echo_fragment_dropped_across_clips_but_interjections_live():
    """메아리 조각(2026-09-09, 「들고」 실사고): 단어 1개·확신 <0.2·직전 유성 span 의 어절과 완전 일치·틈 ≤1s 이면
    버린다 — 클립이 갈려도(소스 시각 틈으로 판정). 앞 줄에 없는 감탄사(「와!」)와 확신 높은 조각은 산다."""
    assert assemble.is_echo_fragment(_words(5, 5.4, "들고", 0.1), "저희 300장 들고 왔어요", 0.02)
    assert not assemble.is_echo_fragment(_words(5, 5.4, "들고", 0.5), "저희 300장 들고 왔어요", 0.02)   # 확신
    assert not assemble.is_echo_fragment(_words(5, 5.4, "들고", 0.1), "저희 300장 들고 왔어요", 1.5)    # 틈
    assert not assemble.is_echo_fragment(_words(5, 5.4, "와!", 0.05), "저희 300장 들고 왔어요", 0.02)   # 앞 줄에 없음
    assert not assemble.is_echo_fragment(_words(5, 5.4, "어?", 0.05), "어제 왔어요", 0.02)              # 부분 문자열은 안 침
    grid = _mk_grid([(0.0, 2.0, True, "저희 300장 들고 왔어요"), (2.02, 2.4, True, "들고"), (2.5, 4.0, True, "고맙습니다")])
    grid["words"] = _words(0, 2, "저희 300장 들고 왔어요", 0.9) + _words(2.02, 2.4, "들고", 0.1) + _words(2.5, 4, "고맙습니다", 0.95)
    s2 = _mk_stage2(grid, [(0, 2, 4, "완판")])
    for seq in s2["sequences"]:
        for ch in seq["chunks"]:
            for m in ch["meanings"]:
                for sp in m["spans"]:
                    sp["text_source"] = "transcript"; sp["heard_text"] = sp["audio_script"][0]["line"]
    idx, _ = st.build_span_index(s2, grid)
    # 「들고」가 다른 클립에 있어도 버린다(실사고 그대로: 앞 줄과 0.02s 차이인데 클립이 갈렸다)
    tl = [{"clip_start_sec": 0.0, "clip_end_sec": 2.0, "use_original_audio": True, "span_ids": ["sp0000"]},
          {"clip_start_sec": 2.02, "clip_end_sec": 4.0, "use_original_audio": True, "span_ids": ["sp0001", "sp0002"]}]
    log: list[dict] = []
    out = assemble.word_subtitles(tl, idx, grid["words"], name_fix_log=log)
    assert " ".join(s["text"] for s in out) == "저희 300장 들고 왔어요 고맙습니다"    # 「들고」 조각 없음
    assert [(f["kind"], f["span_id"]) for f in log] == [("echo", "sp0001")]


def test_single_word_span_uses_neighbor_scene_and_extends_into_missed_silent_lead():
    """「왔잖아요?」 실사고: whisper 가 「저희 300장 들고」를 놓쳐 그 자리를 무성으로 잡고 「왔잖아요?」만 남겼다.
    청취 「저희 300장 들고 왔잖아요」의 어간(300장)은 **앞 조각**(무성)의 화면 묘사에 있다 → 이웃 화면으로 채택하고,
    줄은 그 무성 조각 시작부터 편다(창 확장)."""
    grid = _mk_grid([(0.0, 0.9, False, ""), (0.9, 1.5, True, "왔잖아요?"), (1.6, 3.0, True, "한 시간도 안 돼서")])
    grid["words"] = _words(0.9, 1.5, "왔잖아요?", 0.91) + _words(1.6, 3.0, "한 시간도 안 돼서", 0.9)
    s2 = _mk_stage2(grid, [(0, 2, 4, "완판")])
    for seq in s2["sequences"]:
        for ch in seq["chunks"]:
            for m in ch["meanings"]:
                for sp in m["spans"]:
                    sp["text_source"] = "transcript" if sp["is_audio"] else None
                    if sp["span_id"] == "sp0000":
                        sp["scene_script"] = "빈예서가 300장을 다 나눠줬다며 대단하다고 말한다."
                    elif sp["span_id"] == "sp0001":
                        sp["heard_text"] = "저희 300장 들고 왔잖아요"
                        sp["scene_script"] = "홍지윤이 한 시간도 안 돼서 끝났다며 놀라워한다."
                    else:
                        sp["heard_text"] = "한 시간도 안 돼서"
    idx, _ = st.build_span_index(s2, grid)
    tl = [{"clip_start_sec": 0.0, "clip_end_sec": 3.0, "use_original_audio": True,
           "span_ids": ["sp0000", "sp0001", "sp0002"]}]
    log: list[dict] = []
    out = assemble.word_subtitles(tl, idx, grid["words"], name_fix_log=log)
    assert [f["kind"] for f in log] == ["scene_span"] and log[0]["stem"] == "300장"
    first = [s for s in out if "300장" in s["text"]][0]
    assert first["start_sec"] == 0.0 and out[-1]["text"] == "한 시간도 안 돼서"      # 무성 조각 시작(0.0)부터
    # 단어 1개 폴백은 유사도·어간 증거가 가드 — 화면에 근거 없으면 종전(「왔잖아요?」 그대로)
    for seq in s2["sequences"]:
        for ch in seq["chunks"]:
            for m in ch["meanings"]:
                for sp in m["spans"]:
                    if sp["span_id"] == "sp0000":
                        sp["scene_script"] = "시장 골목."
    idx2, _ = st.build_span_index(s2, grid)
    out2 = assemble.word_subtitles(tl, idx2, grid["words"])
    assert out2[0]["text"] == "왔잖아요?"
