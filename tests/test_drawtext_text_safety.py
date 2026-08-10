"""제목·작품명 drawtext 가 특수문자로 통째로 사라지지 않는지 — 정적 검증.

이 테스트가 있는 이유: 이 결함은 **렌더가 rc=0 으로 성공한다**. ffmpeg 는 drawtext 하나를
조용히 버리고 나머지를 정상 인코딩하므로, 로그에도 산출물 검사에도 안 잡히고 검수함에서
사람 눈으로만 발견된다(2026-08 맥2 실측: "…중식 대가의 50% 할인 전략" 제목 2줄 미렌더).

실측으로 확인된 두 가지(ffmpeg 7.1.5 · 8.1.2):
  - '%' → drawtext 가 확장 문법으로 해석해 필터를 통째 스킵("Stray %" 경고만).
    막는 법은 expansion=none 뿐이다 — '%%' 치환은 동작하지 않는다.
  - "'" → text 를 감싸는 따옴표와 충돌해 필터 파싱이 깨진다(경고조차 없다).
    "\\'"·"'\\''" 는 백슬래시가 찍히거나 따옴표가 사라져, ’(U+2019) 치환만이 글자를 지킨다.
"""
from __future__ import annotations

from app.modules.renderer import _escape_text_for_drawtext


def test_apostrophe_becomes_typographic_quote():
    """작은따옴표는 이스케이프가 아니라 ’ 로 바뀌어야 한다 — 백슬래시가 남으면 줄이 사라진다."""
    out = _escape_text_for_drawtext("중식 '대가'의 50% 할인")
    assert "'" not in out
    assert out.count("’") == 2
    assert "\\’" not in out


def test_percent_is_left_alone_for_expansion_none():
    """'%' 는 이스케이프하지 않는다 — 호출부의 expansion=none 이 정본 방어선이다."""
    assert _escape_text_for_drawtext("50% 할인") == "50% 할인"


def test_other_specials_still_escaped():
    """콜론·대괄호·백슬래시는 종전대로 이스케이프한다(필터 인자 구분자)."""
    out = _escape_text_for_drawtext("대가: [한정] 5\\3")
    assert "\\:" in out and "\\[" in out and "\\]" in out and "\\\\" in out


def test_live_drawtext_filters_declare_expansion_none():
    """제목·작품명 drawtext 는 반드시 expansion=none 을 달고 나가야 한다.

    소스를 읽어 검사하는 이유: 필터 문자열을 만드는 _build_filtergraph 는 렌더 입력 일습이
    있어야 호출되는데, 이 계약은 그 없이도 지켜져야 한다."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "app" / "modules" / "renderer.py"
    live = [ln.strip() for ln in src.read_text(encoding="utf-8").splitlines()
            if "drawtext=" in ln and not ln.lstrip().startswith("#")]
    assert live, "살아있는 drawtext 를 찾지 못했다 — 검사가 무력해졌다"
    assert all("drawtext=expansion=none:" in ln for ln in live), \
        f"expansion=none 없는 drawtext: {[ln[:80] for ln in live if 'expansion=none' not in ln]}"
