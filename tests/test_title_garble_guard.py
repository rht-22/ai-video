"""제목 단축 깨짐 가드(_looks_garbled) 단위테스트.
자가개선 루프 R3: shorten_text(LLM)가 가끔 깨진 한국어를 짧게 뱉어도 길이만 보고 채택되던 버그.
가드는 '원문 글자 재사용률 < 0.7'이면 거부 → 어절 절단 폴백.
실행: /Users/gimsewon/rhoonart/ai-video/.venv/bin/python -m pytest tests/test_title_garble_guard.py -q
"""
from app.pipeline import _looks_garbled


def test_real_compression_accepted():
    # 원문 글자를 그대로 재사용한 정상 압축 → 깨짐 아님(False)
    orig = "전사인 줄 알았던 신임 실장님인데"
    short = "전사인 줄 알았던 실장"
    assert _looks_garbled(orig, short) is False


def test_garbled_rewrite_rejected():
    # 관측된 깨짐: 원문과 글자가 거의 안 겹침 → True
    orig = "우량주인 줄 알고 샀는데 결말이 충격"
    assert _looks_garbled(orig, "위늬봐 포겸수승한") is True
    assert _looks_garbled("우아한 매니저가 드레스 내리고", "우아한 애매무가") is True


def test_empty_shortened_is_garbled():
    assert _looks_garbled("아무 제목이든", "") is True


def test_identical_not_garbled():
    assert _looks_garbled("같은 텍스트", "같은 텍스트") is False


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
