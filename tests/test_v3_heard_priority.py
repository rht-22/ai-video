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
