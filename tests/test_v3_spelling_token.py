"""맞춤법 오인식 대조(arbitrate_spelling) — 2026-09-04 가왕쇼 7화 '때양볕이라서'←'뙤약볕이라'.
사례는 전부 두 편(가왕쇼 7화·지금불륜 EP01) 드라이런 실측에서 가져왔다."""
from __future__ import annotations

from app.v3.textcheck import arbitrate_spelling, fix_span_words


def test_real_case_keeps_whisper_tail():
    # 몸통(뙤약볕)만 모델 표기, 어미(이라서)는 whisper 것
    assert arbitrate_spelling("때양볕이라서", "오늘 너무 덥고 뙤약볕이라", prob=0.644) == "뙤약볕이라서"


def test_more_real_hits():
    assert arbitrate_spelling("꿈쳤거든요", "몇 장 내가 꿍쳤거든요.", prob=0.84) == "꿍쳤거든요"
    assert arbitrate_spelling("히끔히끔", "힐끔힐끔 바라보는 그대", prob=0.66) == "힐끔힐끔"
    assert arbitrate_spelling("커플레이를", "예지님의 카플레이를 연결합니다", prob=0.72) == "카플레이를"
    assert arbitrate_spelling("연세현입니다", "윤수현입니다.", prob=0.67) == "윤수현입니다"


def test_last_syllable_diff_is_grammar_not_spelling():
    # 어미·조사·시제 차이는 각색 — 첫 판에서 깨진 출력을 내던 실사례들
    assert arbitrate_spelling("벗어주시고", "다 같이 안대를 벗어주실 거", prob=0.78) is None
    assert arbitrate_spelling("계시고", "한 분 계시거든요.", prob=0.83) is None
    assert arbitrate_spelling("신발이", "형이 신발을 제일", prob=0.72) is None
    assert arbitrate_spelling("올라오는", "올라온 김에", prob=0.76) is None
    assert arbitrate_spelling("되세요", "이런 게 대세예요.", prob=0.46) is None


def test_gates():
    assert arbitrate_spelling("때양볕이라서", "덥고 뙤약볕이라", prob=0.9) is None    # 확신 어절
    assert arbitrate_spelling("때양볕이라서", "덥고 때양볕이라서") is None          # 모델도 같은 표기
    assert arbitrate_spelling("이거", "이게 뭐야") is None                          # 몸통 2음절
    assert arbitrate_spelling("RC가", "날씨가 더운데") is None                       # 한글 아님(latin 몫)
    assert arbitrate_spelling("먹었어", "밥을 먹었지") is None                       # 초성 다름
    assert arbitrate_spelling("때양볕", "뙤약볕 또는 뙈약볕") is None                # 후보 둘 = 동률


def test_fix_span_words_order_and_kind():
    words = [{"t0": 90.8, "t1": 91.88, "text": "때양볕이라서", "prob": 0.644}]
    out, fixes = fix_span_words(words, [], "오늘 너무 덥고 뙤약볕이라")
    assert out[0]["text"] == "뙤약볕이라서"
    assert fixes == [{"at": 90.8, "from": "때양볕이라서", "to": "뙤약볕이라서", "kind": "spelling"}]
    assert words[0]["text"] == "때양볕이라서"
