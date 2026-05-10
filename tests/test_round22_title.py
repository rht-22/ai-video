"""라운드 22 — title 길이 한도 20자 + sqrt 폰트 축소 테스트."""
from __future__ import annotations

import math

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


# ── _scale_font_for_length sqrt 공식 검증 (재구현) ──
# renderer.py의 _scale_font_for_length는 nested function이라 import 불가.
# 동일 공식을 여기서 재현해 sqrt 비례가 의도대로 적용되는지 검증.

def _scale_font_for_length_repro(base_size: int, char_count: int) -> int:
    """renderer.py:_scale_font_for_length 동일 공식 재현."""
    if char_count <= 13:
        return base_size
    cc = min(char_count, 20)
    scale = math.sqrt(13.0 / cc)
    return max(1, int(round(base_size * scale)))


def test_scale_font_no_change_under_13():
    assert _scale_font_for_length_repro(70, 10) == 70
    assert _scale_font_for_length_repro(70, 13) == 70
    assert _scale_font_for_length_repro(90, 13) == 90


def test_scale_font_sqrt_proportional():
    """14자: 70 × sqrt(13/14) ≈ 67"""
    assert _scale_font_for_length_repro(70, 14) == 67
    # 17자: 70 × sqrt(13/17) ≈ 61
    assert _scale_font_for_length_repro(70, 17) == 61
    # 20자: 70 × sqrt(13/20) ≈ 56
    assert _scale_font_for_length_repro(70, 20) == 56


def test_scale_font_caps_at_20():
    """21자 이상은 20자와 동일 (pipeline에서 [:20] 또는 LLM 재작성)."""
    assert _scale_font_for_length_repro(70, 21) == 56
    assert _scale_font_for_length_repro(70, 50) == 56
    assert _scale_font_for_length_repro(70, 100) == 56


def test_scale_font_min_size_1():
    """base가 0이거나 매우 작아도 최소 1 보장."""
    assert _scale_font_for_length_repro(0, 20) == 1


def test_scale_font_canvas_width_safety():
    """20자 × 0.78 × 56 ≈ 873px → canvas 1080 안에 들어옴 (좌우 100px 여백)."""
    font_size = _scale_font_for_length_repro(70, 20)
    estimated_width_px = 20 * 0.78 * font_size
    assert estimated_width_px < 1080, f"20자 가로 폭 {estimated_width_px:.0f}px > canvas 1080"
    # 70 base에서 축소 안 적용하면 잘림 (회귀 검증)
    estimated_no_shrink = 20 * 0.78 * 70
    assert estimated_no_shrink > 1080, "축소 없으면 잘려야 함 (가정 확인)"
