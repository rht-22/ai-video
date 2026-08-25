"""F-412 — overlay(잔망루피 이식본) 자막 줄바꿈·글자 통 폭 + 재렌더 안정화.

vlp 23648e0 의 tests/test_f412_wrap_width.py 와 같은 계약 — 이식본이 같은 세 버그를
그대로 들고 있었다(P4 컷오버 전에 잡는다).

잔망루피 실측 사고 두 건의 회귀 방어:
  · 편집실 Enter 줄바꿈이 16자 기계 분할 속에 끼어 자막이 부서졌다(구 wrap_text 가
    개행을 모른 채 char-chunk / text.split() 으로 삼켰다).
  · 구 엔진이 모르는 style 키(width)가 route C 의 광역 except 에 삼켜져, 사람이 고친
    y·size 까지 전부 무시된 채 재번역·재합성 전체 재실행이 '성공'으로 끝났다.
"""
from __future__ import annotations

import pytest

from app.localize.overlay.render import (
    LINE_WIDTH_BASE,
    build_ass,
    validate_line_style,
    width_margin_lr,
    width_max_chars,
    wrap_text,
)


class TestWrapText:
    def test_manual_break_wins_over_char_chunking(self) -> None:
        # 잔망루피 실측 문장 — 종전엔 개행이 16자 청크 안에 끼어 ASS 가 부서졌다
        out = wrap_text("チャンネル登録と高評価が\n励みになります！", 16)
        assert out == ["チャンネル登録と高評価が", "励みになります！"]

    def test_manual_break_wins_over_word_wrap(self) -> None:
        out = wrap_text("앞 부분 문장\n뒷 부분 문장", 5)
        assert out == ["앞 부분 문장", "뒷 부분 문장"]

    def test_no_break_keeps_legacy_char_chunking(self) -> None:
        text = "チャンネル登録と高評価お願いします！"      # 18자, 공백·개행 없음
        assert wrap_text(text, 16) == ["チャンネル登録と高評価お願いしま", "す！"]

    def test_no_break_keeps_legacy_word_wrap(self) -> None:
        assert wrap_text("one two three four", 9) == ["one two", "three", "four"]

    def test_blank_manual_lines_dropped(self) -> None:
        assert wrap_text("앞\n\n  \n뒤", 16) == ["앞", "뒤"]

    def test_empty(self) -> None:
        assert wrap_text("", 16) == []


class TestWidth:
    def test_style_width_accepted_and_normalized(self) -> None:
        assert validate_line_style({"width": 0.95}) == {"width": 0.95}

    @pytest.mark.parametrize("bad", [0.1, 1.5, "넓게"])
    def test_width_out_of_range_rejected(self, bad) -> None:
        with pytest.raises(ValueError):
            validate_line_style({"width": bad})

    def test_unknown_key_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="모르는 style 키"):
            validate_line_style({"widht": 0.9})

    def test_char_budget_anchor(self) -> None:
        # 미지정 = 기준 폭(0.852) — 종전 16자 그대로. 1.0 이면 비례해 늘어난다.
        assert width_max_chars(16, None) == 16
        assert width_max_chars(16, {}) == 16
        assert width_max_chars(16, {"width": LINE_WIDTH_BASE}) == 16
        assert width_max_chars(16, {"width": 1.0}) == 19
        assert width_max_chars(16, {"width": 0.5}) < 16

    def test_margin_symmetric_default_zero(self) -> None:
        assert width_margin_lr(None, 1080) == 0        # 0 = 스타일 기본값(종전 그대로)
        assert width_margin_lr({}, 1080) == 0
        assert width_margin_lr({"width": 1.0}, 1080) == 1
        assert width_margin_lr({"width": 0.8}, 1080) == 108

    def test_build_ass_wide_line_fits_one_line(self) -> None:
        # 잔망루피 사용자 시나리오: 18자 문장 + width — 한 줄로 나가야 한다
        ev = [{"start": 0.0, "end": 2.0, "text": "チャンネル登録と高評価お願いします！",
               "style": {"width": 1.0}}]
        out = build_ass(ev, 1920, 1080, line_max_chars=16)
        dlg = [ln for ln in out.splitlines() if ln.startswith("Dialogue:")][0]
        assert "\\N" not in dlg
        fields = dlg.split(",", 9)
        assert fields[5] == fields[6] != "0"           # 이벤트 MarginL/R 대칭

    def test_build_ass_without_style_is_unchanged(self) -> None:
        ev = [{"start": 0.0, "end": 2.0, "text": "チャンネル登録と高評価お願いします！"}]
        dlg = [ln for ln in build_ass(ev, 1920, 1080, 16).splitlines()
               if ln.startswith("Dialogue:")][0]
        assert dlg.split(",", 9)[5:8] == ["0", "0", "0"]
        assert "\\N" in dlg                            # 종전 16자 분할 그대로

    def test_build_ass_manual_break_single_event(self) -> None:
        # 개행이 한 Dialogue 이벤트 안의 \N 이어야 한다 — 물리 줄이 갈라지면 ASS 가 깨진다
        ev = [{"start": 0.0, "end": 2.0,
               "text": "チャンネル登録と高評価が\n励みになります！",
               "style": {"y": 0.8721}}]
        out = build_ass(ev, 1920, 1080, 16)
        dlgs = [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]
        assert len(dlgs) == 1
        assert "チャンネル登録と高評価が\\N励みになります！" in dlgs[0]


class TestDubOverridesFailLoud:
    def test_unknown_style_key_fails_the_merge(self) -> None:
        from app.localize.overlay.dub import apply_dub_overrides
        events = [{"idx": 0, "start": 0.0, "end": 2.0, "text": "元"}]
        with pytest.raises(ValueError):
            apply_dub_overrides(events, {"subs": {"0": {"style": {"widht": 0.9}}}})

    def test_width_style_merges(self) -> None:
        from app.localize.overlay.dub import apply_dub_overrides
        events = [{"idx": 0, "start": 0.0, "end": 2.0, "text": "元"}]
        out, n = apply_dub_overrides(events, {"subs": {"0": {"style": {"width": 0.95}}}})
        assert n == 1 and out[0]["style"] == {"width": 0.95}

    def test_manual_break_survives_merge(self) -> None:
        from app.localize.overlay.dub import apply_dub_overrides
        events = [{"idx": 0, "start": 0.0, "end": 2.0, "text": "元"}]
        out, _ = apply_dub_overrides(events, {"subs": {"0": {"ja": "前\n後"}}})
        assert out[0]["text"] == "前\n後"


class TestSynthFlatten:
    def test_newline_not_sent_to_backend(self, monkeypatch) -> None:
        """자막용 줄바꿈이 합성 대본까지 흘러가면 백엔드가 끊어 읽는다."""
        from app.localize.overlay import dub as dub_mod
        seen = []
        monkeypatch.setattr(dub_mod, "dub_backend", lambda cfg: "gptsovits")
        monkeypatch.setattr(dub_mod, "_detect_lang", lambda t, d: "ja")
        monkeypatch.setattr(dub_mod, "_synthesize_gptsovits",
                            lambda text, lang, cfg: seen.append(text) or b"")
        dub_mod.synthesize_segment("前の行\n後の行", {})
        assert seen == ["前の行 後の行"]
