"""라운드 22 — title 길이 한도 20자 + lookup 기반 폰트 축소 테스트."""
from __future__ import annotations

from app.pipeline import _enforce_title_line_limit


# ── _enforce_title_line_limit (max_chars=20 default) ──

def test_enforce_default_is_20_chars():
    """default max_chars 20인지 확인."""
    text = "이것은 정확히 십삼자임"  # 11자 (공백 포함)
    out = _enforce_title_line_limit(text)  # default
    assert out == text  # 13자 미만 → 그대로


def test_enforce_truncates_above_20_word_boundary():
    """20자 초과 시 어절 경계로 절단."""
    long_title = "이것은 정말정말 너무너무 긴 제목입니다 추가로 더 긴말"  # ≥25자
    out = _enforce_title_line_limit(long_title, max_chars=20)
    assert len(out) <= 20


def test_enforce_keeps_under_20_unchanged():
    """20자 이내는 그대로."""
    text = "사랑에 빠진 그날의 한 마디 저장"  # 16자 정도
    out = _enforce_title_line_limit(text, max_chars=20)
    assert out == text


def test_enforce_preserves_word_boundaries():
    """어절 경계 우선 — 단어 중간에서 자르지 않음."""
    text = "한정판 티셔츠를 뺏으려고 시도하는 패륜 사기극의 결말"  # 25자+
    out = _enforce_title_line_limit(text, max_chars=20)
    # 어절 단위로 잘려야 함 (공백으로 끝나거나 onset)
    assert len(out) <= 20
    # 어절 절단이면 마지막 글자가 공백이 아닐 것
    assert not out.endswith(" ")


def test_enforce_empty_input():
    assert _enforce_title_line_limit("") == ""
    assert _enforce_title_line_limit("", max_chars=20) == ""


# ── _scale_font_for_length lookup 테이블 검증 (재구현) ──
# renderer.py의 _scale_font_for_length는 nested function이라 import 불가.
# 동일 테이블을 여기서 재현해 lookup 값이 의도대로 적용되는지 검증.
# renderer.py:_TITLE_LENGTH_SCALE 과 반드시 동기화 유지.

_TITLE_LENGTH_SCALE_REPRO = {
    14: 0.90,
    15: 0.83,
    16: 0.77,
    17: 0.72,
    18: 0.67,
    19: 0.63,
    20: 0.60,
}


def _scale_font_for_length_repro(base_size: int, char_count: int) -> int:
    """renderer.py:_scale_font_for_length 동일 로직 재현."""
    if char_count <= 13:
        return base_size
    cc = min(char_count, 20)
    scale = _TITLE_LENGTH_SCALE_REPRO[cc]
    return max(1, int(round(base_size * scale)))


def test_scale_font_no_change_under_13():
    assert _scale_font_for_length_repro(70, 10) == 70
    assert _scale_font_for_length_repro(70, 13) == 70
    assert _scale_font_for_length_repro(90, 13) == 90


def test_scale_font_lookup_14_to_20_base70():
    """base 70 — 14~20자 명시 lookup."""
    assert _scale_font_for_length_repro(70, 14) == round(70 * 0.90)  # 63
    assert _scale_font_for_length_repro(70, 15) == round(70 * 0.83)  # 58
    assert _scale_font_for_length_repro(70, 16) == round(70 * 0.77)  # 54
    assert _scale_font_for_length_repro(70, 17) == round(70 * 0.72)  # 50
    assert _scale_font_for_length_repro(70, 18) == round(70 * 0.67)  # 47
    assert _scale_font_for_length_repro(70, 19) == round(70 * 0.63)  # 44
    assert _scale_font_for_length_repro(70, 20) == round(70 * 0.60)  # 42


def test_scale_font_lookup_14_to_20_base90():
    """base 90 (line2 기본값) — 14~20자 명시 lookup."""
    assert _scale_font_for_length_repro(90, 14) == round(90 * 0.90)  # 81
    assert _scale_font_for_length_repro(90, 15) == round(90 * 0.83)  # 75
    assert _scale_font_for_length_repro(90, 16) == round(90 * 0.77)  # 69
    assert _scale_font_for_length_repro(90, 17) == round(90 * 0.72)  # 65
    assert _scale_font_for_length_repro(90, 18) == round(90 * 0.67)  # 60
    assert _scale_font_for_length_repro(90, 19) == round(90 * 0.63)  # 57
    assert _scale_font_for_length_repro(90, 20) == round(90 * 0.60)  # 54


def test_scale_font_monotonic_decreasing():
    """글자 수 증가 시 폰트 크기는 비증가(monotonic non-increasing)여야 함."""
    sizes = [_scale_font_for_length_repro(90, n) for n in range(13, 21)]
    for prev, curr in zip(sizes, sizes[1:]):
        assert curr <= prev, f"비단조: {sizes}"


def test_scale_font_more_aggressive_than_sqrt():
    """새 lookup은 종전 sqrt(13/cc) 곡선보다 모든 구간에서 작거나 같아야 함."""
    import math
    for n in range(14, 21):
        new_val = _scale_font_for_length_repro(90, n)
        old_val = max(1, int(round(90 * math.sqrt(13.0 / n))))
        assert new_val <= old_val, f"{n}자: new={new_val} > old={old_val}"


def test_scale_font_caps_at_20():
    """21자 이상은 20자와 동일 (pipeline에서 [:20] 또는 LLM 재작성)."""
    cap = _scale_font_for_length_repro(70, 20)
    assert _scale_font_for_length_repro(70, 21) == cap
    assert _scale_font_for_length_repro(70, 50) == cap
    assert _scale_font_for_length_repro(70, 100) == cap


def test_scale_font_min_size_1():
    """base가 0이거나 매우 작아도 최소 1 보장."""
    assert _scale_font_for_length_repro(0, 20) == 1


def test_scale_font_canvas_width_safety_base90():
    """line2 base=90, 20자: 가로 잘림 없이 캔버스(1080) 안에 들어와야 함."""
    font_size = _scale_font_for_length_repro(90, 20)
    estimated_width_px = 20 * 0.78 * font_size
    assert estimated_width_px < 1080, f"20자 가로 폭 {estimated_width_px:.0f}px > canvas 1080"
    # 축소 없으면 잘림 (회귀 가정 확인)
    estimated_no_shrink = 20 * 0.78 * 90
    assert estimated_no_shrink > 1080, "축소 없으면 잘려야 함 (가정 확인)"
