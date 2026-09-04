"""정렬 기반 맞춤법 교정(arbitrate_aligned) — 2026-09-04 지금불륜 EP01 실사고.

쇼츠 자막 오타 6건 중 arbitrate_spelling 이 1건만 잡았다: 어절 1:1 대조라 whisper 와
모델의 띄어쓰기가 다르면 후보가 없고(이삿덕이요↔이사 떡이요 · 청년녀 중이구나↔청남여중이구나),
초성이 다르면 제외(들이러↔드리러). 모델 청취는 6/6 정답이었다. 드라이런 4편 1,532 span
6,312 어절: 적중 95 · 멀쩡한 어절 깨짐 0.
"""
from __future__ import annotations

from app.v3.textcheck import align_tokens_to_heard, arbitrate_aligned, fix_span_words


def _fix(words, heard):
    toks = [w["text"] for w in words]
    pieces = align_tokens_to_heard(toks, heard)
    return [arbitrate_aligned(w["text"], pc, prob=w.get("prob")) for w, pc in zip(words, pieces)]


def _w(*pairs):
    return [{"text": t, "prob": p, "t0": i, "t1": i + 1} for i, (t, p) in enumerate(pairs)]


def test_spacing_mismatch_one_token_vs_two():
    # whisper 한 어절 ↔ 청취 두 어절 (이삿덕이요 ↔ 이사 떡이요)
    assert _fix(_w(("이삿덕이요", 0.62)), "이사 떡이요.") == ["이사떡이요"]


def test_spacing_mismatch_two_tokens_vs_one_and_initial_change():
    # whisper 두 어절 ↔ 청취 한 어절 + 초성 ㄴ/ㅇ 차이(청년녀→청남여) — spelling 규칙은 못 잡는다
    out = _fix(_w(("어머,", 0.18), ("너도", 0.99), ("청년녀", 0.77), ("중이구나.", 0.87)),
               "어머, 너도 청남여중이구나!")
    assert out == [None, None, "청남여", None]


def test_initial_consonant_change_with_same_length():
    assert _fix(_w(("들이러", 0.87), ("갔었는데", 0.98)), "드리러 갔었는데") == ["드리러", None]
    assert _fix(_w(("무실하잖아", 0.87)), "무시하잖아.") == ["무시하잖아"]
    assert _fix(_w(("빙그레샹", 0.81)), "빙그레 썅...") == ["빙그레썅"]


def test_prob_ceiling_and_punctuation_kept():
    assert _fix(_w(("들이러", 0.95)), "드리러") == [None]          # 0.90 이상은 안 건드린다
    assert _fix(_w(("역시.", 0.28)), "혹시...") == ["혹시."]         # 구두점은 whisper 것


def test_paraphrase_far_in_jamo_is_left_alone():
    # 각색(다른 말) — 자모 차이가 크다: 이제↔내가 4 · 왔다↔어디 3(2음절 상한 2)
    assert _fix(_w(("이제", 0.75), ("연락할게.", 0.9)), "내가 연락할게.") == [None, None]
    assert _fix(_w(("왔다", 0.61), ("봐.", 0.9)), "어디 봐.") == [None, None]


def test_demonstratives_and_single_syllables_are_never_touched():
    assert _fix(_w(("여기", 0.09), ("좀", 0.5)), "이거 좀") == [None, None]
    assert _fix(_w(("이건", 0.32), ("알", 0.5)), "그건 알") == [None, None]
    assert _fix(_w(("이", 0.6), ("브랜드", 0.9)), "그 브랜드") == [None, None]


def test_last_syllable_only_initial_consonant():
    assert _fix(_w(("로컴", 0.81)), "로펌") == ["로펌"]              # 초성 ㅋ→ㅍ 허용
    assert _fix(_w(("버려.", 0.31)), "버렸...") == [None]             # 종성 추가 = 어미 각색
    assert _fix(_w(("방이", 0.80)), "방에") == [None]                 # 모음 = 조사 각색
    assert _fix(_w(("찍으시게", 0.77)), "찍으시겠다잖아") == [None]


def test_piece_must_start_at_heard_token_boundary():
    # '가운이 보니까' ↔ '가운 입으니까' — 으니까 는 청취 어절 가운데서 시작하는 조각
    out = _fix(_w(("가운이", 0.64), ("보니까", 0.75)), "가운 입으니까")
    assert out == [None, None]


def test_three_syllable_allows_three_jamo_but_not_whole_syllable():
    assert _fix(_w(("개첩한", 0.54)), "개차반") == ["개차반"]
    assert _fix(_w(("하연아", 0.60)), "하여간") == ["하여간"]
    assert _fix(_w(("차지하는데", 0.49)), "차야되는데") == [None]    # 4 자모


def test_fix_span_words_records_kind_aligned_after_other_passes():
    words = _w(("호요를", 0.81), ("이삿덕이요", 0.62))
    out, fixes = fix_span_words(words, [], "호의를 이사 떡이요")
    assert [w["text"] for w in out] == ["호의를", "이사떡이요"]
    assert [f["kind"] for f in fixes] == ["spelling", "aligned"]    # spelling 이 먼저 잡는다


# ── whisper 가 끊은 단어 병합(merge_split_words, 2026-09-04) ──

def test_merge_split_word_when_model_heard_one_token():
    # '청년녀 / 중이구나.' ↔ '청남여중이구나!' — 교정된 앞 어절과 뒤 어절을 한 어절로
    words = _w(("어머,", 0.18), ("너도", 0.99), ("청년녀", 0.77), ("중이구나.", 0.87))
    out, fixes = fix_span_words(words, [], "어머, 너도 청남여중이구나!")
    assert [w["text"] for w in out] == ["어머,", "너도", "청남여중이구나."]
    assert out[2]["t0"] == 2 and out[2]["t1"] == 4 and out[2]["prob"] == 0.77
    assert [(f["kind"], f["to"]) for f in fixes] == [("aligned", "청남여"), ("merge", "청남여중이구나.")]
    assert all("_fixed" not in w for w in out)


def test_no_merge_on_plain_spacing_disagreement():
    # 둘 다 교정 안 된 쌍은 whisper 띄어쓰기 편 — '안 계시더라구요' vs '안계시더라고요'
    # (뒤 어절은 청취 어절 가운데서 시작하는 조각이라 aligned 교정도 안 된다)
    words = _w(("안", 0.97), ("계시더라구요", 0.86))
    out, fixes = fix_span_words(words, [], "안계시더라고요")
    assert [w["text"] for w in out] == ["안", "계시더라구요"] and fixes == []


# ── 문장 힌트 · 앞 음절 복원 · 균형 분할 (2026-09-04 지금불륜 EP01 줄 나눔 지적) ──

def test_sentence_end_hint_from_model_punctuation_splits_lines():
    from app.v3 import assemble
    words = _w(("안녕하세요", 0.9), ("저희", 0.14), ("앞집", 0.9), ("이사왔어요", 0.9))
    out, fixes = fix_span_words(words, [], "안녕하세요. 저희 앞집 이사 왔어요.")
    assert out[0].get("sent_end") is True and not out[1].get("sent_end")
    assert [w["text"] for w in out] == ["안녕하세요", "저희", "앞집", "이사왔어요"]   # 글자 불변
    lines = assemble._lines_for_span(out, 0.0, 6.0)
    assert [l["text"] for l in lines] == ["안녕하세요", "저희 앞집 이사왔어요"]


def test_missing_prefix_recovered_only_when_unmapped():
    words = _w(("한", 0.58), ("정성도", 0.88), ("좀", 0.92), ("부담이거든요", 0.99))
    out, fixes = fix_span_words(words, [], "'과한 정성도 좀 부담이거든요'? 이런 빙그레 썅")
    assert out[0]["text"] == "과한" and fixes[0]["kind"] == "prefix"
    assert out[3].get("sent_end") is True                    # 부담이거든요'? → 문장 끝
    # 앞 whisper 어절이 그 글자에 대응하면 띄어쓰기 차이 — 복원 아님
    words2 = _w(("안", 0.97), ("계시더라구요", 0.86))
    out2, fixes2 = fix_span_words(words2, [], "안계시더라고요")
    assert [w["text"] for w in out2] == ["안", "계시더라구요"] and fixes2 == []


def test_balanced_breaks_avoid_orphan_tail():
    from app.v3.assemble import _balanced_breaks, _lines_for_span
    lens = [len(x) for x in "어디다 두고 남의 호의를 함부로 쓰레기 취급해".split()]
    ends = _balanced_breaks(lens)
    assert ends == [2, 4, 6]        # 어디다 두고 남의 / 호의를 함부로 / 쓰레기 취급해
    assert _balanced_breaks([15]) == [0]                    # 단일 초과 어절은 홀로
    assert _balanced_breaks([2, 2, 2, 2, 2]) == [2, 4]      # 3+2 와 2+3 이 동점 → 앞 줄 긴 쪽(그리디)
    words = _w(("어디다", 0.4), ("두고", 0.8), ("남의", 0.9), ("호의를", 0.8), ("함부로", 0.99),
               ("쓰레기", 0.98), ("취급해", 1.0), ("그러니까", 0.86), ("말이야", 0.86))
    out, _ = fix_span_words(words, [], "어디다 두고 남의 호의를 함부로 쓰레기 취급해? 어, 그러니까 말이야.")
    lines = _lines_for_span(out, 0.0, 12.0)
    assert [l["text"] for l in lines] == ["어디다 두고 남의", "호의를 함부로", "쓰레기 취급해",
                                          "그러니까 말이야"]


def test_soft_sentence_hint_merges_short_neighbors_and_boundary_penalties():
    from app.v3.assemble import _lines_for_span
    # '대박. 미쳤다.' — 합쳐서 한 줄에 들면 문장 힌트로 안 끊는다
    words = _w(("대박", 0.95), ("미쳤다", 0.95))
    out, _ = fix_span_words(words, [], "대박. 미쳤다.")
    assert [l["text"] for l in _lines_for_span(out, 0.0, 3.0)] == ["대박 미쳤다"]
    # 의존명사 '건' 으로 줄이 시작하는 배치는 벌점 → 종전 모양 유지
    words = _w(("여기서도", 0.9), ("하실", 0.9), ("건", 0.9), ("아니죠.", 0.9))
    assert [l["text"] for l in _lines_for_span(words, 0.0, 3.0)] == ["여기서도 하실 건", "아니죠."]
    # 부사 '안' 으로 줄이 끝나는 배치는 벌점 → '잘 안 하는데' 가 한 줄
    words = _w(("이쪽", 0.9), ("동네", 0.9), ("사람들은", 0.9), ("이런", 0.9), ("거", 0.5),
               ("잘", 0.9), ("안", 0.9), ("하는데", 0.9))
    assert [l["text"] for l in _lines_for_span(words, 0.0, 5.0)] == \
        ["이쪽 동네", "사람들은 이런 거", "잘 안 하는데"]
