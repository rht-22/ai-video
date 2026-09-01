#!/usr/bin/env python3
"""폰트 글리프 검사 — 두부(□) 방지 (2026-09-01).

발단: AI 연출 텔롭 "즉석 라이브 폭발 💥" 가 화면에 ⊠ 로 나갔다. 제목에만 있던
이모지 차단이 연출에는 없었고, 애초에 '이모지'는 틀린 기준이었다 — 막아야 하는
것은 **그 폰트에 글리프가 없는 글자**이고 폰트마다 다르다(★♪ 는 Jalnan 에 있고
Griun 에는 없다). 그래서 정규식이 아니라 cmap 을 읽는다.
"""
from pathlib import Path

import pytest

from app.modules.glyphs import (
    ALWAYS_OK,
    charset,
    check_overrides,
    font_charset,
    missing_chars,
    strip_missing,
)

APP_ROOT = Path(__file__).resolve().parent.parent / "app"
FONTS = APP_ROOT / "assets" / "fonts"

# 번들 4종 실측(2026-09-01) — 이 표가 바뀌면 폰트가 교체된 것이다.
EMOJI = "💥🔥⚡😂✨❗⭐"


# ── cmap 파싱 (실파일) ─────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["Jalnan", "JalnanGothic", "mulmaru", "Griun"])
def test_bundled_fonts_have_no_emoji(name):
    """번들 폰트에는 이모지 글리프가 하나도 없다 — 쓰면 무조건 두부다."""
    cs = charset(str(FONTS / f"{name}.ttf"))
    assert cs, f"{name}: cmap 을 못 읽었다(파싱 회귀)"
    assert missing_chars(EMOJI, cs) == list(EMOJI)


@pytest.mark.parametrize("name", ["Jalnan", "JalnanGothic", "mulmaru", "Griun"])
def test_bundled_fonts_draw_hangul(name):
    """한글은 넷 다 그린다 — 이게 깨지면 파서가 틀린 것이다(가장 강한 신호)."""
    cs = charset(str(FONTS / f"{name}.ttf"))
    assert not missing_chars("가나다 한글 힣", cs)


def test_symbol_coverage_differs_by_font():
    """★♪ 는 Jalnan 계열엔 있고 Griun 엔 없다 — 정규식이 못 하는 판정이 이것이다.

    _strip_emoji 의 U+2600–26FF·U+2702–27B0 은 ♪(266A)를 통째로 삼킨다. 폰트를
    보면 Jalnan 에서는 살리고 Griun 에서만 막을 수 있다."""
    jalnan = charset(str(FONTS / "Jalnan.ttf"))
    griun = charset(str(FONTS / "Griun.ttf"))
    assert not missing_chars("★♪‼", jalnan)
    assert missing_chars("★♪", griun) == ["★", "♪"]


def test_font_charset_resolves_like_render():
    """이름 → 파일 해석이 렌더(get_font_path)와 같아야 한다."""
    assert font_charset("Jalnan", APP_ROOT) == charset(str(FONTS / "Jalnan.ttf"))
    assert not missing_chars("한글", font_charset("여기어때 잘난체 2 TTF", APP_ROOT))


# ── 판정·정리 (순수) ──────────────────────────────────────────────────
def test_missing_chars_is_ordered_and_deduped():
    cs = charset(str(FONTS / "Jalnan.ttf"))
    assert missing_chars("🔥가💥나🔥", cs) == ["🔥", "💥"]


def test_whitespace_never_counts_as_missing():
    """줄바꿈은 제목의 2줄 위계다 — 폰트에 없다고 지우면 제목이 한 줄이 된다."""
    cs = charset(str(FONTS / "Jalnan.ttf"))
    assert not missing_chars("윗줄\n아랫줄\t끝", cs)
    assert all(ord(c) in ALWAYS_OK for c in "\n\t\r ")


def test_strip_missing_tidies_the_hole_it_made():
    """"폭발 💥" → "폭발" — 뺀 자리의 공백이 남으면 가운데 정렬이 치우친다."""
    cs = charset(str(FONTS / "Jalnan.ttf"))
    assert strip_missing("즉석 라이브 폭발 💥", cs) == ("즉석 라이브 폭발", ["💥"])
    assert strip_missing("가💥나", cs) == ("가나", ["💥"])


def test_strip_missing_keeps_line_break():
    cs = charset(str(FONTS / "Jalnan.ttf"))
    kept, bad = strip_missing("길거리 즉석 열창했더니\n표심 잡으러 나선 전유진 🔥", cs)
    assert kept == "길거리 즉석 열창했더니\n표심 잡으러 나선 전유진"
    assert bad == ["🔥"]


def test_clean_text_is_untouched():
    """뺄 것이 없으면 원문 그대로 — 공백 정리도 하지 않는다(회귀 0)."""
    cs = charset(str(FONTS / "Jalnan.ttf"))
    assert strip_missing("  띄어  쓰기  ", cs) == ("  띄어  쓰기  ", [])


def test_unreadable_font_fails_open():
    """폰트를 못 읽으면 판정 불가 — 사람 문구를 지우거나 막지 않는다."""
    assert charset("/없는/폰트.ttf") == frozenset()
    assert missing_chars("폭발 💥", frozenset()) == []
    assert strip_missing("폭발 💥", frozenset()) == ("폭발 💥", [])


# ── 사람이 보낸 값은 거절 ──────────────────────────────────────────────
def test_check_overrides_rejects_tofu_in_texts():
    from app.modules.edit_overrides import EditOverrideError

    ov = {"texts": [{"text": "폭발 💥", "font": "Jalnan"}]}
    with pytest.raises(EditOverrideError) as e:
        check_overrides(ov, APP_ROOT)
    assert "💥" in str(e.value) and "두부" in str(e.value)


def test_check_overrides_rejects_tofu_in_title():
    from app.modules.edit_overrides import EditOverrideError

    ov = {"title": {"top_title": "윗줄\n아랫줄 🔥",
                    "segments": [{"text": "창 ⭐", "start_sec": 0, "end_sec": 1}]}}
    with pytest.raises(EditOverrideError) as e:
        check_overrides(ov, APP_ROOT, title_font="Jalnan")
    assert "🔥" in str(e.value) and "⭐" in str(e.value)


def test_check_overrides_font_is_per_item():
    """폰트가 항목마다 다르다 — ★ 는 Jalnan 에선 통과, Griun 에선 거절."""
    from app.modules.edit_overrides import EditOverrideError

    check_overrides({"texts": [{"text": "★", "font": "Jalnan"}]}, APP_ROOT)
    with pytest.raises(EditOverrideError):
        check_overrides({"texts": [{"text": "★", "font": "Griun"}]}, APP_ROOT)


def test_check_overrides_passes_clean_and_empty():
    check_overrides(None, APP_ROOT)
    check_overrides({}, APP_ROOT)
    check_overrides({"texts": [{"text": "즉석 라이브 폭발"}]}, APP_ROOT)
    # 제목 폰트를 모르면 제목은 판정하지 않는다(fail-open)
    check_overrides({"title": {"top_title": "폭발 💥"}}, APP_ROOT)


# ── AI 산출은 제거 + note (거절하지 않는다) ────────────────────────────
def _plan(**kw):
    base = {"schema": "style_plan/v1"}
    base.update(kw)
    return base


def _validate(plan, tmp_path, **kw):
    from app.modules.style_compose import validate_plan
    return validate_plan(plan, manifest={}, app_root=APP_ROOT, run_dir=tmp_path, **kw)


def test_ai_text_tofu_is_stripped_not_rejected(tmp_path):
    """연출 하나 때문에 렌더 전체를 죽이지 않는다 — 빼고 남긴다."""
    out, notes = _validate(_plan(texts=[
        {"text": "즉석 라이브 폭발 💥", "source_time_sec": 10.0, "duration_sec": 1.2,
         "x": 0.5, "y": 0.5, "size": 96}]), tmp_path)
    assert out["texts"][0]["text"] == "즉석 라이브 폭발"
    assert any("💥" in n and "두부" in n for n in notes)


def test_ai_text_uses_its_own_font(tmp_path):
    """★ 는 Jalnan 에선 남고 Griun 에선 빠진다."""
    item = {"source_time_sec": 10.0, "duration_sec": 1.2, "x": 0.5, "y": 0.5, "size": 96}
    keep, _ = _validate(_plan(texts=[{**item, "text": "쿵 ★", "font": "Jalnan"}]), tmp_path)
    drop, notes = _validate(_plan(texts=[{**item, "text": "쿵 ★", "font": "Griun"}]), tmp_path)
    assert keep["texts"][0]["text"] == "쿵 ★"
    assert drop["texts"][0]["text"] == "쿵"
    assert any("★" in n for n in notes)


def test_ai_title_window_tofu_is_stripped(tmp_path):
    out, notes = _validate(_plan(
        title_fixed="길거리 즉석 열창했더니 🔥",
        title_segments=[{"text": "표심 잡으러 나선 전유진 ⭐",
                         "from_anchor": 0.0, "to_anchor": 30.0}]),
        tmp_path, title_font="Jalnan")
    assert out["title_fixed"] == "길거리 즉석 열창했더니"
    assert out["title_segments"][0]["text"] == "표심 잡으러 나선 전유진"
    assert any("🔥" in n for n in notes) and any("⭐" in n for n in notes)


def test_ai_title_window_of_only_tofu_is_dropped(tmp_path):
    """빈 창을 남기면 그 시간에 제목이 사라진다 — 창을 버리고 직전 문구가 잇는다."""
    out, notes = _validate(_plan(
        title_fixed="윗줄",
        title_segments=[{"text": "💥⭐", "from_anchor": 0.0, "to_anchor": 30.0},
                        {"text": "살아남는 창", "from_anchor": 30.0, "to_anchor": 60.0}]),
        tmp_path, title_font="Jalnan")
    assert [s["text"] for s in out["title_segments"]] == ["살아남는 창"]
    assert any("창을 버린다" in n for n in notes)


def test_no_title_font_means_no_title_judgement(tmp_path):
    """제목 폰트를 모르면 제목은 종전 그대로다(회귀 0 · fail-open)."""
    out, _ = _validate(_plan(
        title_fixed="윗줄 🔥",
        title_segments=[{"text": "아랫줄 ⭐", "from_anchor": 0.0, "to_anchor": 30.0}]),
        tmp_path)
    assert out["title_fixed"] == "윗줄 🔥"
    assert out["title_segments"][0]["text"] == "아랫줄 ⭐"


def test_clean_plan_gets_no_tofu_notes(tmp_path):
    out, notes = _validate(_plan(texts=[
        {"text": "즉석 라이브 폭발", "source_time_sec": 10.0, "duration_sec": 1.2,
         "x": 0.5, "y": 0.5, "size": 96}]), tmp_path)
    assert out["texts"][0]["text"] == "즉석 라이브 폭발"
    assert not [n for n in notes if "두부" in n]
