"""V3-M9 — 자막 텍스트 신뢰 3종 회귀 가드(LLM 없이 · 실측 픽스처).

발주서 orders/v3-m9-transcript-adjudication.md. 세 사례를 박제한다:
  A "박처진"←박서진 (가왕쇼 자막 43.0s 실물) · 오탐 0 대조군
  B "육십!" 34줄 시뮬레이션(환각 구간을 자막 빌더에 태운 실물) · 실제 자막 무경고
  C 전사 깨짐 판정 4분기 — 각색 방어(전사 정상)는 종전 그대로 유지
"""
from __future__ import annotations

import pytest

from app.v3 import chunk_analyze as ca
from app.v3 import textcheck as tc


def _seg(t, text): return {"start_sec": t, "end_sec": t + 1.0, "text": text}


# ── A 인명 대조 ─────────────────────────────────────────────────────────────

NAMES = ["박서진", "전유진", "홍지윤", "빈예서", "윤수현", "최수호"]


def test_name_check_hits_real_case():
    # 가왕쇼 43.0s 실물 자막 — 한 글자 오인식
    hits = tc.check_names([_seg(43.0, "박처진 씨 등장하자마자")], NAMES)
    assert len(hits) == 1
    assert (hits[0]["token"], hits[0]["suggest"]) == ("박처진", "박서진")


def test_name_check_no_false_positive_on_real_subtitles():
    # 이번 편 실제 자막에서 인명이 정확히 나온 줄 + 무관한 줄
    segs = [_seg(31.7, "이거 전유진 티켓은"), _seg(12.5, "이거 이거 티켓 티켓"),
            _seg(35.3, "이건 하나만 가질 수 있어요."), _seg(45.3, "이렇게 힘들게 뿌릴")]
    assert tc.check_names(segs, NAMES) == []


def test_name_check_ignores_length_mismatch():
    # 길이가 다르면 다른 낱말 — "박서진들"·"서진" 은 잡지 않는다(오탐 억제)
    assert tc.check_names([_seg(1, "박서진들 모여")], NAMES) == []
    assert tc.check_names([_seg(1, "서진 씨")], NAMES) == []


def test_name_fix_is_opt_in_and_logged():
    segs = [_seg(43.0, "박처진 씨 등장하자마자")]
    fixed, log = tc.fix_names(segs, NAMES)
    assert fixed[0]["text"] == "박서진 씨 등장하자마자"
    assert log[0]["before"] != log[0]["after"]
    assert segs[0]["text"] == "박처진 씨 등장하자마자"      # 원본 불변(순수)


# ── B 반복 그물 ─────────────────────────────────────────────────────────────

def test_repetition_catches_hallucination_run():
    # 환각 구간 시뮬레이션 실물: "육십!" 이 34줄 연속
    segs = [_seg(i * 0.5, "육십!") for i in range(34)]
    warns = tc.check_repetition(segs)
    assert any(w["kind"] == "run" and w["n"] >= 3 for w in warns)
    kept, w2 = tc.drop_repetition(segs)
    assert kept == [] and w2                                # 전량 제외 + 사유


def test_repetition_window_rule():
    # 사이사이 다른 줄이 껴도 창 점유가 높으면 잡는다
    segs = []
    for i in range(8):
        segs.append(_seg(i * 2.0, "그녀는" if i % 2 == 0 else f"말{i}"))
    assert any(w["kind"] == "window" for w in tc.check_repetition(segs))


def test_repetition_no_false_positive_on_real_dialogue():
    # 이번 편 실제 자막 일부 — 구어 반복("이거 가지세요 이거")은 잡으면 안 된다
    segs = [_seg(12.5, "이거 이거 티켓 티켓"), _seg(14.5, "필요 없고 이거 이거"),
            _seg(15.4, "이거 가지세요 이거"), _seg(16.1, "가지실래요?"),
            _seg(16.7, "아 이거 왜 받으세요?"), _seg(19.4, "왜 받으세요?"),
            _seg(25.1, "가세요 가세요."), _seg(26.5, "어머니 들어가세요.")]
    assert tc.check_repetition(segs) == []


# ── C 전사 판정 ─────────────────────────────────────────────────────────────

def _span(sid, t0, t1, text): return {"id": sid, "t_in": t0, "t_out": t1,
                                      "is_audio": True, "time_authority": "stt",
                                      "text": text}


def _norm(sid, heard):
    return [{"first_idx": 0, "last_idx": 0, "content": "c", "characters": [],
             "importance": 3, "mood": "",
             "spans": [{"span_id": sid, "scene_script": "", "characters": [],
                        "importance": 3, "audio": heard, "heard": heard}]}]


def test_adjudication_picks_heard_when_transcript_is_loop():
    # 실사고 재현: 격자에 "육십!" ×29 · 모델은 실제 대사를 들었다
    sp = [_span("sp0", 991.0, 1014.0, " ".join(["육십!"] * 29))]
    words = [{"t0": 991 + i * 0.3, "t1": 991 + i * 0.3 + 0.13, "text": "육십!",
              "prob": 0.97} for i in range(29)]
    norm = _norm("sp0", [{"speaker": "박서진", "line": "산악회 60명 온대요"}])
    d = ca.adjudicate_transcript(norm, sp, words)
    assert d[0]["decision"] == "heard" and "반복 환각" in d[0]["broken"]
    assert norm[0]["spans"][0]["audio"][0]["line"] == "산악회 60명 온대요"
    assert norm[0]["spans"][0]["text_source"] == "heard"


def test_adjudication_empty_transcript_takes_heard():
    """전사가 아예 없으면 잃을 게 없다 — heard 채택. (저확신 단독 뒤집기는
    실측 오탐으로 제거됐다: test_low_confidence_alone_never_flips 참조)"""
    d = ca.adjudicate_transcript(_norm("sp2", [{"speaker": "갑", "line": "실제 대사"}]),
                                 [_span("sp2", 10.0, 12.0, "  ")], [])
    assert d[0]["decision"] == "heard" and d[0]["broken"] == "전사 없음"
    assert d[0]["text"] == "실제 대사"


def test_adjudication_drops_span_when_both_unusable():
    # 전사도 깨졌고 모델도 못 들었다 → 자막 제외(조용한 공백 금지 — 사유 기록)
    sp = [_span("sp3", 0.0, 3.0, "")]
    norm = _norm("sp3", [])
    d = ca.adjudicate_transcript(norm, sp, [])
    assert d[0]["decision"] == "none"
    assert norm[0]["spans"][0]["audio"] == []
    assert norm[0]["spans"][0]["text_source"] == "none"


def test_adjudication_keeps_sound_transcript_against_paraphrase():
    # 각색 방어 유지 — 전사가 정상이면 heard 가 달라도 전사가 이긴다
    sp = [_span("sp4", 0.0, 3.0, "안녕하세요.")]
    words = [{"t0": 0.3, "t1": 1.1, "text": "안녕하세요.", "prob": 0.93}]
    norm = _norm("sp4", [{"speaker": "갑", "line": "여러분 만나서 정말 반갑습니다"}])
    d = ca.adjudicate_transcript(norm, sp, words)
    assert d[0]["decision"] == "transcript" and d[0]["restored"] is True
    assert norm[0]["spans"][0]["audio"][0]["line"] == "안녕하세요."


def test_transcript_broken_signals():
    ok = _span("s", 0.0, 3.0, "정상 문장입니다")
    assert ca.transcript_broken(ok, [{"t0": 0.2, "t1": 1.0, "text": "정상",
                                      "prob": 0.9}]) is None
    # 길이 0 단어 8개가 span [0,3) **안에** 있어야 서명 표본이 된다(중점 소속 규칙)
    zero = [{"t0": 0.2 + i * 0.3, "t1": 0.2 + i * 0.3, "text": f"w{i}", "prob": 0.9}
            for i in range(8)]
    assert "길이 퇴화" in (ca.transcript_broken(ok, zero) or "")
    # 단어 없이 텍스트만 반복 — 격자 재료 불일치 경로(표본 ≥6 에서만)
    rep = _span("s", 0.0, 3.0, "네 네 네 네 네 네")
    assert "반복 환각" in (ca.transcript_broken(rep, []) or "")


def test_span_scale_guard_protects_short_real_speech():
    """리뷰 확정: 창(≥6s 재전사)용 임계를 작은 span 에 쓰면 정상 발화를 오판했다.
    표본이 의미를 갖는 크기(SPAN_SIGNATURE_MIN_WORDS) 아래에서는 서명을 안 본다."""
    # ① 예능 맞장구 "네 네 네" — 3단어, 확신도 정상
    sp = _span("s", 0.0, 1.0, "네 네 네")
    ws = [{"t0": 0.1 + i * 0.3, "t1": 0.1 + i * 0.3 + 0.25, "text": "네", "prob": 0.94}
          for i in range(3)]
    assert ca.transcript_broken(sp, ws) is None
    # ② 한 단어짜리 span — 중앙값이 곧 그 단어 길이라 '길이 퇴화'로 오판되던 경로
    one = _span("s2", 5.2, 5.3, "네.")
    assert ca.transcript_broken(one, [{"t0": 5.20, "t1": 5.25, "text": "네.",
                                       "prob": 0.9}]) is None
    # ③ 표본이 충분하면 여전히 잡는다
    big = _span("s3", 0.0, 10.0, " ".join(["육십!"] * 12))
    bw = [{"t0": i * 0.3, "t1": i * 0.3 + 0.13, "text": "육십!", "prob": 0.97}
          for i in range(12)]
    assert "환각" in (ca.transcript_broken(big, bw) or "")


def test_name_check_skips_exact_dictionary_members():
    """리뷰 확정 major: 편집거리 1 인 출연자 쌍(박서진/박세진)에서 정확한 이름이
    다른 이름의 오인식으로 판정되고, --fix-names 면 엉뚱한 사람으로 바뀌었다."""
    names = ["박서진", "박세진", "전유진"]
    segs = [_seg(10.0, "박세진 씨가 노래합니다")]
    assert tc.check_names(segs, names) == []
    fixed, log = tc.fix_names(segs, names)
    assert fixed[0]["text"] == "박세진 씨가 노래합니다" and log == []
    # 사전에 없는 진짜 오인식은 여전히 잡는다
    assert len(tc.check_names([_seg(1.0, "박처진 씨")], names)) == 1


def test_fix_names_replaces_only_whole_tokens():
    """리뷰 확정: 줄 전체 replace 가 다른 어절의 부분 문자열을 오염시켰다."""
    names = ["홍지윤"]
    segs = [_seg(1.0, "홍지운 씨와 홍지운아빠가")]     # 두 번째는 다른 어절의 접두
    fixed, log = tc.fix_names(segs, names)
    assert fixed[0]["text"] == "홍지윤 씨와 홍지운아빠가"
    assert log[0]["n"] == 1


def test_repetition_window_merges_indexes_once():
    """리뷰 확정: 창마다 경고가 중복 발행되고 뒤 창에만 나온 줄이 새던 결함."""
    segs = [_seg(i * 2.0, "그녀는" if i % 2 == 0 else f"말{i}") for i in range(12)]
    warns = [w for w in tc.check_repetition(segs) if w["kind"] == "window"]
    assert len(warns) == 1                      # 텍스트당 1건
    assert warns[0]["indexes"] == [0, 2, 4, 6, 8, 10]   # 뒤 창의 등장도 포함
    assert warns[0]["at"] == 0.0                # 반복 첫 등장 기준


def test_drop_repetition_keeps_window_only_warnings():
    """리뷰 확정: 창 규칙으로 정상 대사를 제거하면 안 된다 — 경고 전용."""
    segs = [_seg(i * 2.0, "네." if i % 2 == 0 else f"대사{i}") for i in range(8)]
    kept, warns = tc.drop_repetition(segs)
    assert len(kept) == len(segs)               # 한 줄도 안 지운다
    assert any(w["kind"] == "window" for w in warns)   # 경고는 남는다


def test_adjudication_catches_cross_span_loop():
    # 실측 재현(가왕쇼 991~1014s): 환각이 한 단어짜리 span 29개로 흩어져 있어
    # 단일 span 검사로는 안 걸렸다 — 구간 단위 서명이 잡아야 한다
    spans = [_span(f"sp{i}", 991.0 + i * 0.8, 991.0 + i * 0.8 + 0.4, "육십!")
             for i in range(29)]
    assert len(ca.degenerate_span_run(spans)) == 29
    norm = [{"first_idx": 0, "last_idx": 28, "content": "c", "characters": [],
             "importance": 3, "mood": "",
             "spans": [{"span_id": s["id"], "scene_script": "", "characters": [],
                        "importance": 3,
                        "audio": [{"speaker": "박서진", "line": "산악회 60명이요"}],
                        "heard": [{"speaker": "박서진", "line": "산악회 60명이요"}]}
                       for s in spans]}]
    d = ca.adjudicate_transcript(norm, spans, None)
    assert all(x["decision"] == "heard" for x in d)
    assert norm[0]["spans"][0]["audio"][0]["line"] == "산악회 60명이요"


def test_span_run_ignores_normal_dialogue():
    # 실제 대사에서 짧은 맞장구가 몇 번 나오는 정도는 잡으면 안 된다
    spans = [_span("a", 0, 2, "안녕하세요."), _span("b", 2, 4, "네."),
             _span("c", 4, 6, "반갑습니다."), _span("d", 6, 8, "네."),
             _span("e", 8, 10, "그러시군요.")]
    assert ca.degenerate_span_run(spans) == {}


def test_span_judgment_requires_repetition_and_duration():
    """M9 완주 실측 결함: 실제 반복 발화가 비율만으로 뒤집혀 자막이 사람이 하지 않은
    말로 바뀌었다("이거 이거 티켓 티켓…" → 모델 추정). span 레벨은 반복 ∧ 길이 이상을
    **동시에** 요구한다 — 실측 구분자는 단어 길이(실제 0.260s vs 환각 0.000~0.130s)."""
    # ① 실제 반복 발화 — 길이 정상(0.26s) → 전사 유지
    real = _span("s", 300.1, 304.4, "이거 이거 티켓 티켓 필요 없고 이거 이거")
    rw = [{"t0": 300.1 + i * 0.33, "t1": 300.1 + i * 0.33 + 0.26,
           "text": ["이거", "이거", "티켓", "티켓", "필요", "없고", "이거", "이거"][i],
           "prob": 0.78} for i in range(8)]
    assert ca.transcript_broken(real, rw) is None
    # ② 환각 루프 — 같은 반복 비율인데 길이가 무너졌다 → 적발
    fake = _span("s2", 632.0, 657.0, " ".join(["그녀는"] * 8))
    fw = [{"t0": 632.0 + i * 0.5, "t1": 632.0 + i * 0.5, "text": "그녀는",
           "prob": 0.85} for i in range(8)]
    assert "반복 환각" in (ca.transcript_broken(fake, fw) or "")


def test_low_confidence_alone_never_flips():
    """실측: 1단어 span "안녕하세요."(prob 0.04)의 전사가 정답이었다 — 저확신
    단독으로 모델 추정을 화면에 올리면 안 된다(계기판의 몫)."""
    sp = _span("s", 297.6, 298.0, "안녕하세요.")
    ws = [{"t0": 297.65, "t1": 297.95, "text": "안녕하세요.", "prob": 0.04}]
    assert ca.transcript_broken(sp, ws) is None
    norm = _norm("s", [{"speaker": "갑", "line": "완전히 다른 말"}])
    d = ca.adjudicate_transcript(norm, [sp], ws)
    assert d[0]["decision"] == "transcript"


def test_name_check_initials_rule_catches_two_edits():
    """M9 완주 실측 미탐: '정유지!'(←전유진)가 화면에 나갔다 — 3음절 이름은 거리 2
    오인식이 흔한데(자음 유지·모음/받침 흔들림) 거리 1 규칙이 통째로 놓쳤다."""
    names = ["전유진", "박서진", "빈예서"]
    hits = tc.check_names([_seg(28.1, "정유지!")], names)
    assert len(hits) == 1 and hits[0]["suggest"] == "전유진"
    # 초성이 다르면 거리 2 는 안 잡는다(오탐 억제) — '박처진'은 거리 1 규칙으로 잡힘
    assert tc.check_names([_seg(1.0, "박처진 씨")], names)[0]["suggest"] == "박서진"
    assert tc.check_names([_seg(1.0, "가나다 라마바")], names) == []


def test_name_check_no_false_positive_on_measured_corpora():
    """실측 회귀 — 가왕쇼·포핸즈 자막 어휘에서 오탐 0(임계 확장의 조건)."""
    gw = ["전유진", "박서진", "홍지윤", "김수찬", "별사랑", "김태연", "구수경",
          "윤수현", "최수호", "빈예서"]
    fh = ["강비오", "최정요", "홍재인", "강승운", "강지혜", "윤선주", "도현수", "최기택"]
    real = ["어머니 나가시는 거예요?", "홍보 종료하겠습니다.", "이거 전유진 티켓은",
            "반주는 됐고,", "그냥 너랑 나랑 결혼하자!", "저리 가, 제발",
            "이건 하나만 가질 수 있어요.", "연예인 된 기분입니다, 지금."]
    segs = [_seg(i, s) for i, s in enumerate(real)]
    assert tc.check_names(segs, gw) == []
    assert tc.check_names(segs, fh) == []


def test_initials_helper():
    assert tc.initials("전유진") == tc.initials("정유지") == "ㅈㅇㅈ"
    assert tc.initials("박서진") != tc.initials("박처진")
    assert tc.initials("abc") == "abc"
