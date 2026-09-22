"""tikitaka 제목 — 줄당 글자 수를 프롬프트에 처음부터 싣는다(2026-09-21 사용자 결정). 템플릿 없는 실행은 종전과 바이트 동일."""
from app.tikitaka.title import TITLE_PROMPT, title_prompt
from app.v3.safe_zone import title_char_budget


def test_no_title_fit_is_byte_identical():
    assert title_prompt(None) == TITLE_PROMPT
    assert title_prompt({}) == TITLE_PROMPT


def test_title_fit_states_per_line_budget_up_front():
    text = title_prompt({"font": None, "sizes": [66, 84]})
    a, b = title_char_budget(66), title_char_budget(84)
    assert f"**{a}자 이내**" in text and f"**{b}자 이내**" in text
    assert "총 24자 안팎" not in text                      # 옛 총량 기준은 사라진다
    assert "각 줄이 위 상한 안이면" in text
    assert text.index("길이(필수") < text.index("[말투]")   # 예시·출력 연결보다 앞 — 구상 단계에서 읽힌다
    assert text.count("[좋은 예시") == 1 and text.endswith(TITLE_PROMPT[-40:])


def test_three_prompt_sites_use_the_budgeted_prompt():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "tikitaka"
    for name in ("rebuild.py", "staged.py", "verify.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert 'title_prompt((guide or {}).get("title_fit"))' in src and "TITLE_PROMPT" not in src, name
