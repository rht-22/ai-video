"""F-412 — 자막 줄바꿈(사람이 정한 경계)과 컨테이너 가로 폭.

편집실에서 "여기서 줄을 끊어라"(텍스트 안 줄바꿈)와 "통을 좌우로 넓혀 줄이 안 접히게
하라"(style.width · design.tts_width)를 사람이 정하면, 엔진이 그대로 존중하는지 본다.

핵심 회귀 방어 두 가지:
  · width 를 안 주면 출력이 **종전과 바이트 동일**해야 한다(안 건드린 편은 안 바뀐다).
  · 폭만 넓히고 자동 줄바꿈 글자 수를 안 늘리면 파이썬이 먼저 잘라 폭이 무용지물이 된다.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.edit_overrides import EditOverrideError, validate_overrides
from app.modules.speech import SpeechSegment
from app.modules.subtitle import (
    SUB_BASE_MAX_CHARS,
    SubtitleStyle,
    _lay_out_for_ass,
    _width_margins,
    _width_max_chars,
    build_ass_from_segments,
    build_tts_ass,
)

STYLE = SubtitleStyle(font_size=65, margin_v=380)


def _dialogues(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8-sig").splitlines()
            if ln.startswith("Dialogue:")]


class TestLayOut:
    def test_manual_break_wins_over_auto_wrap(self) -> None:
        # 자동 규칙이면 15자에서 잘릴 문장을, 사람이 정한 자리에서 끊는다
        out = _lay_out_for_ass("그날 밤\n나는 아무 말도 하지 못했다")
        assert out == "그날 밤\\N나는 아무 말도 하지 못했다"

    def test_manual_single_line_forces_no_wrap_when_wide(self) -> None:
        # 폭을 화면 끝까지 넓히면 종전에 두 줄이던 문장이 한 줄로 들어간다
        text = "이건 한 문장이지만 좀 길어서 두"
        assert "\\N" in _lay_out_for_ass(text)
        assert "\\N" not in _lay_out_for_ass(text, width=1.0)

    def test_no_width_is_byte_identical_to_legacy(self) -> None:
        from app.modules.subtitle import _wrap_for_ass
        text = "이건 한 문장이지만 좀 길어서 두 줄로 나뉘어야 한다"
        assert _lay_out_for_ass(text) == _wrap_for_ass(text, max_chars=15, max_lines=2)

    def test_blank_lines_dropped(self) -> None:
        assert _lay_out_for_ass("앞줄\n\n  \n뒷줄") == "앞줄\\N뒷줄"

    def test_line_cap_applies_to_manual_breaks(self) -> None:
        # 상한(2줄)을 넘는 수동 줄바꿈은 잘린다 — 계약 검증이 애초에 막는다(아래 클래스)
        assert _lay_out_for_ass("하나\n둘\n셋").count("\\N") == 1

    def test_empty(self) -> None:
        assert _lay_out_for_ass("") == ""


class TestWidthMath:
    def test_none_means_style_default(self) -> None:
        assert _width_margins(None) == ("", "")
        assert _width_max_chars(None) == SUB_BASE_MAX_CHARS

    def test_full_width_has_no_side_margin(self) -> None:
        # 1.0 = 화면 끝까지. ASS 규약상 0 은 '스타일 기본값'이라 최소 1 로 클램프
        assert _width_margins(1.0) == ("1", "1")

    def test_margins_are_symmetric_and_shrink_as_width_grows(self) -> None:
        narrow = int(_width_margins(0.6)[0])
        wide = int(_width_margins(0.95)[0])
        assert _width_margins(0.6)[0] == _width_margins(0.6)[1]
        assert narrow > wide

    def test_line_size_override_changes_char_budget(self) -> None:
        # 그 줄만 글자를 키우면 같은 폭에 덜 들어간다(줄이면 더). 안 고쳤으면 그대로.
        assert _width_max_chars(None, 1.0) == SUB_BASE_MAX_CHARS
        assert _width_max_chars(None, 65 / 100) < SUB_BASE_MAX_CHARS
        assert _width_max_chars(None, 65 / 40) > SUB_BASE_MAX_CHARS

    def test_max_chars_scales_with_width(self) -> None:
        # 기본 폭(0.852)에서는 종전 15자 그대로여야 한다 — 회귀 방어
        assert _width_max_chars((1080 - 160) / 1080) == SUB_BASE_MAX_CHARS
        assert _width_max_chars(1.0) > SUB_BASE_MAX_CHARS
        assert _width_max_chars(0.5) < SUB_BASE_MAX_CHARS


class TestDialogueAss:
    def test_width_becomes_event_side_margins(self, tmp_path: Path) -> None:
        seg = SimpleNamespace(start_sec=0.0, end_sec=2.0, text="한 줄로 가고 싶은 긴 자막",
                              style={"width": 1.0})
        out = tmp_path / "s.ass"
        build_ass_from_segments([seg], out, STYLE)
        line = _dialogues(out)[0]
        # Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
        fields = line.split(",", 9)
        assert fields[5] == "1" and fields[6] == "1"
        assert "\\N" not in fields[9]

    def test_untouched_line_keeps_empty_margins(self, tmp_path: Path) -> None:
        seg = SpeechSegment(start_sec=0.0, end_sec=2.0, text="평범한 자막")
        out = tmp_path / "s.ass"
        build_ass_from_segments([seg], out, STYLE)
        fields = _dialogues(out)[0].split(",", 9)
        assert fields[5] == "" and fields[6] == ""

    def test_width_coexists_with_other_line_styles(self, tmp_path: Path) -> None:
        seg = SimpleNamespace(start_sec=0.0, end_sec=2.0, text="색도 크기도 폭도",
                              style={"width": 0.98, "size": 80, "color": "#FF0000",
                                     "y": 0.8})
        out = tmp_path / "s.ass"
        build_ass_from_segments([seg], out, STYLE)
        fields = _dialogues(out)[0].split(",", 9)
        assert fields[5] == fields[6] != ""          # 폭 → 좌우 여백
        assert fields[7] == str(round((1 - 0.8) * 1920))  # y → MarginV
        assert "\\fs80" in fields[9] and "\\1c" in fields[9]

    def test_manual_break_survives_to_ass(self, tmp_path: Path) -> None:
        seg = SimpleNamespace(start_sec=0.0, end_sec=2.0,
                              text="그날 밤\n나는 아무 말도 못했다", style=None)
        out = tmp_path / "s.ass"
        build_ass_from_segments([seg], out, STYLE)
        assert "그날 밤\\N나는 아무 말도 못했다" in _dialogues(out)[0]

    def test_speaker_prefix_still_stripped_on_every_line(self, tmp_path: Path) -> None:
        seg = SimpleNamespace(start_sec=0.0, end_sec=2.0,
                              text="의주: 그날 밤\n우수: 나는 못 갔다", style=None)
        out = tmp_path / "s.ass"
        build_ass_from_segments([seg], out, STYLE)
        body = _dialogues(out)[0]
        assert "의주" not in body and "우수" not in body
        assert "그날 밤\\N나는 못 갔다" in body


class TestTtsAss:
    def test_width_becomes_event_side_margins(self, tmp_path: Path) -> None:
        seg = SpeechSegment(start_sec=0.0, end_sec=2.0, text="내레이션 한 줄로 길게 가고 싶다")
        out = tmp_path / "t.ass"
        build_tts_ass([seg], out, STYLE, width=1.0)
        fields = _dialogues(out)[0].split(",", 9)
        assert fields[5] == "1" and fields[6] == "1"
        assert "\\N" not in fields[9]

    def test_no_width_is_unchanged(self, tmp_path: Path) -> None:
        seg = SpeechSegment(start_sec=0.0, end_sec=2.0, text="내레이션")
        a, b = tmp_path / "a.ass", tmp_path / "b.ass"
        build_tts_ass([seg], a, STYLE)
        build_tts_ass([seg], b, STYLE, width=None)
        assert a.read_bytes() == b.read_bytes()
        assert _dialogues(a)[0].split(",", 9)[5:8] == ["", "", ""]

    def test_manual_break_survives(self, tmp_path: Path) -> None:
        seg = SpeechSegment(start_sec=0.0, end_sec=2.0, text="앞 문장\n뒤 문장")
        out = tmp_path / "t.ass"
        build_tts_ass([seg], out, STYLE)
        assert "앞 문장\\N뒤 문장" in _dialogues(out)[0]

    def test_rotation_origin_unaffected_by_width(self, tmp_path: Path) -> None:
        # 좌우 대칭이라 \org 앵커 x(화면 중앙)는 폭과 무관해야 한다
        seg = SpeechSegment(start_sec=0.0, end_sec=2.0, text="기울인 내레이션")
        a, b = tmp_path / "a.ass", tmp_path / "b.ass"
        build_tts_ass([seg], a, STYLE, rotate_deg=8.0)
        build_tts_ass([seg], b, STYLE, rotate_deg=8.0, width=1.0)
        assert "\\org(540," in a.read_text(encoding="utf-8-sig")
        assert "\\org(540," in b.read_text(encoding="utf-8-sig")


class TestContract:
    def _ov(self, **kw):
        base = {"schema": "edit_overrides/v3",
                "subtitles": [{"start_sec": 0.0, "end_sec": 2.0, "text": "안녕"}]}
        base.update(kw)
        return base

    def test_width_accepted(self) -> None:
        ov = self._ov(subtitles=[{"start_sec": 0.0, "end_sec": 2.0, "text": "안녕",
                                  "style": {"width": 0.95}}])
        assert validate_overrides(ov) is ov

    @pytest.mark.parametrize("bad", [0.1, 1.5, "넓게"])
    def test_width_out_of_range_rejected(self, bad) -> None:
        ov = self._ov(subtitles=[{"start_sec": 0.0, "end_sec": 2.0, "text": "안녕",
                                  "style": {"width": bad}}])
        with pytest.raises(EditOverrideError, match="width"):
            validate_overrides(ov)

    def test_width_is_v3_only(self) -> None:
        ov = self._ov(schema="edit_overrides/v2",
                      subtitles=[{"start_sec": 0.0, "end_sec": 2.0, "text": "안녕",
                                  "style": {"width": 0.95}}])
        with pytest.raises(EditOverrideError):
            validate_overrides(ov)

    def test_two_line_subtitle_ok(self) -> None:
        ov = self._ov(subtitles=[{"start_sec": 0.0, "end_sec": 2.0, "text": "앞\n뒤"}])
        assert validate_overrides(ov) is ov

    def test_three_line_subtitle_rejected(self) -> None:
        ov = self._ov(subtitles=[{"start_sec": 0.0, "end_sec": 2.0, "text": "하나\n둘\n셋"}])
        with pytest.raises(EditOverrideError, match="줄바꿈이 너무 많습니다"):
            validate_overrides(ov)

    def test_three_line_tts_rejected(self) -> None:
        ov = self._ov(tts=[{"source_time_sec": 1.0, "duration_sec": 3.0,
                            "text": "하나\n둘\n셋"}])
        with pytest.raises(EditOverrideError, match="줄바꿈이 너무 많습니다"):
            validate_overrides(ov)

    def test_width_normalized_into_segments(self) -> None:
        from app.modules.edit_overrides import overrides_subtitles
        out = overrides_subtitles({"subtitles": [
            {"start_sec": 0.0, "end_sec": 2.0, "text": "안녕", "style": {"width": "0.95"}}]})
        assert out[0]["style"] == {"width": 0.95}

    def test_manual_break_survives_normalization(self) -> None:
        from app.modules.edit_overrides import overrides_subtitles
        out = overrides_subtitles({"subtitles": [
            {"start_sec": 0.0, "end_sec": 2.0, "text": "  앞\n뒤  "}]})
        assert out[0]["text"] == "앞\n뒤"


class TestTtsSynthesisIgnoresBreaks:
    def test_newline_is_not_sent_to_the_voice(self, monkeypatch, tmp_path: Path) -> None:
        """자막용 줄바꿈이 합성 대본까지 흘러가면 백엔드마다 발음이 갈린다."""
        from app.modules import tts as tts_mod
        seen: list[str] = []
        monkeypatch.setattr(tts_mod, "_announce_backend", lambda: "edge-tts")
        monkeypatch.setattr(tts_mod, "_synthesize_edge_tts",
                            lambda text, path, **kw: seen.append(text) or path)
        tts_mod.synthesize_tts("앞 문장\n뒤 문장", tmp_path / "a.mp3")
        assert seen == ["앞 문장 뒤 문장"]


class TestLocalizeContract:
    """일본어 경로(app.localize)는 같은 style 계약을 fail-loud 로 검증한다 — width 를
    모르면 KR 이 정한 폭을 실은 JP 검수 수정이 통째로 거절돼 재렌더가 죽는다."""

    def test_width_accepted_and_normalized(self) -> None:
        from app.localize.styles import validate_line_style
        assert validate_line_style({"width": "0.95"}) == {"width": 0.95}

    @pytest.mark.parametrize("bad", [0.1, 1.5])
    def test_width_out_of_range_rejected(self, bad) -> None:
        from app.localize.styles import validate_line_style
        with pytest.raises(ValueError, match="width"):
            validate_line_style({"width": bad})

    def test_margin_lr_is_symmetric_and_defaults_to_zero(self) -> None:
        from app.localize.styles import style_margin_lr
        assert style_margin_lr({}, 1080) == 0            # 0 = 스타일 기본값(종전 그대로)
        assert style_margin_lr({"width": 1.0}, 1080) == 1  # 여백 0 은 ASS 가 '기본값'으로 읽는다
        assert style_margin_lr({"width": 0.8}, 1080) == 108

    def test_telop_event_carries_side_margins(self, tmp_path: Path) -> None:
        from app.localize.apply import build_telop_ass
        telops = [{"kind": "broadcast_telop", "index": 0, "start_sec": 1.0, "end_sec": 2.0,
                   "text_ko": "가"}]
        tr = {"telops": [{"index": 0, "use": True, "ja": "ア", "start_sec": 1.0,
                          "end_sec": 2.0, "style": {"width": 1.0}}]}
        out = tmp_path / "t.ass"
        build_telop_ass(telops, tr, "Font", out)
        line = [ln for ln in out.read_text(encoding="utf-8").splitlines()
                if ln.startswith("Dialogue:")][0]
        fields = line.split(",", 9)
        assert fields[5] == "1" and fields[6] == "1"

    def test_telop_without_style_is_unchanged(self, tmp_path: Path) -> None:
        from app.localize.apply import build_telop_ass
        telops = [{"kind": "broadcast_telop", "index": 0, "start_sec": 1.0, "end_sec": 2.0,
                   "text_ko": "가"}]
        tr = {"telops": [{"index": 0, "use": True, "ja": "ア"}]}
        out = tmp_path / "t.ass"
        build_telop_ass(telops, tr, "Font", out)
        line = [ln for ln in out.read_text(encoding="utf-8").splitlines()
                if ln.startswith("Dialogue:")][0]
        assert line.split(",", 9)[5:8] == ["0", "0", "0"]


class TestJpOverrideLineCap:
    """JP 검수 수정(p_edits) 경로의 줄 수 fail-loud — 대사·TTS 자막은 렌더가 상한 넘는
    줄을 조용히 잘라내므로 apply_overrides 가 거절해야 한다. 텔롭은 상한 없음(잘림 없음)."""

    def _tr(self):
        return {"segments": [{"index": 0, "ja": "元"}],
                "tts_cues": [{"index": 0, "ja": "元"}],
                "telops": [{"index": 0, "ja": "元", "use": True}]}

    def test_two_line_sub_accepted_dict_and_string(self) -> None:
        from app.localize.overrides import apply_overrides
        out = apply_overrides(self._tr(), {"subs": {"0": "前\n後"}})
        assert out["segments"][0]["ja"] == "前\n後"
        out = apply_overrides(self._tr(), {"subs": {"0": {"ja": "前\n後"}}})
        assert out["segments"][0]["ja"] == "前\n後"

    def test_three_line_sub_rejected_both_forms(self) -> None:
        from app.localize.overrides import apply_overrides
        with pytest.raises(ValueError, match="줄바꿈이 너무 많습니다"):
            apply_overrides(self._tr(), {"subs": {"0": "一\n二\n三"}})
        with pytest.raises(ValueError, match="줄바꿈이 너무 많습니다"):
            apply_overrides(self._tr(), {"subs": {"0": {"ja": "一\n二\n三"}}})

    def test_three_line_tts_rejected(self) -> None:
        from app.localize.overrides import apply_overrides
        with pytest.raises(ValueError, match="줄바꿈이 너무 많습니다"):
            apply_overrides(self._tr(), {"tts": {"0": "一\n二\n三"}})

    def test_telop_lines_uncapped(self) -> None:
        # build_telop_ass 는 \N 을 그대로 그리고 잘라내지 않는다 — 상한을 걸 이유가 없다
        from app.localize.overrides import apply_overrides
        out = apply_overrides(self._tr(), {"telops": {"0": {"ja": "一\n二\n三"}}})
        assert out["telops"][0]["ja"] == "一\n二\n三"

    def test_sub_width_style_roundtrip(self) -> None:
        # JP 편집실 ⇔ 가 보내는 줄별 폭 — validate_line_style 를 지나 segments 에 얹힌다
        from app.localize.overrides import apply_overrides
        out = apply_overrides(self._tr(),
                              {"subs": {"0": {"ja": "字幕", "style": {"width": 0.95}}}})
        assert out["segments"][0]["style"] == {"width": 0.95}
