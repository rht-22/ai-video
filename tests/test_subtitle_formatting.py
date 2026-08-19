"""subtitle.py 의 출력 정제 헬퍼들에 대한 단위 테스트.

대상:
- `_strip_speaker_prefix`: SRT 파싱 시 부여된 "이름: " prefix 를 ASS 출력 직전에 제거.
- `_wrap_for_ass`: 문장을 max_chars 단위로 분할하되 한 화면(\\N)으로 묶음 — 시간 균등 분할 깜빡임 제거.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.modules.speech import SpeechSegment
from app.modules.subtitle import (
    SubtitleStyle,
    _strip_speaker_prefix,
    _wrap_for_ass,
    build_ass_from_segments,
    merge_subtitle_segments,
)


class TestStripSpeakerPrefix:
    def test_korean_speaker_removed(self) -> None:
        assert _strip_speaker_prefix("의주: 이건 현실이 아닐 거야") == "이건 현실이 아닐 거야"

    def test_english_speaker_removed(self) -> None:
        assert _strip_speaker_prefix("Soyeon: hello") == "hello"

    def test_fullwidth_colon_removed(self) -> None:
        assert _strip_speaker_prefix("우수：무슨 일이지") == "무슨 일이지"

    def test_no_prefix_unchanged(self) -> None:
        assert _strip_speaker_prefix("이건 그냥 문장") == "이건 그냥 문장"

    def test_long_word_not_treated_as_speaker(self) -> None:
        # 9자 이상 + 콜론은 화자 prefix 로 보지 않음 (정규식 max 8자)
        assert _strip_speaker_prefix("길고길고길고길고길긴이름: 무엇") == "길고길고길고길고길긴이름: 무엇"

    def test_empty_text(self) -> None:
        assert _strip_speaker_prefix("") == ""

    def test_only_first_prefix_stripped(self) -> None:
        # 첫 위치 + inline 위치 모두 제거. "안녕:" 는 문장 시작 후 첫 어절이라 _SPEAKER_PREFIX_RE
        # 한 번에 다 못 잡아도 inline 패턴(공백 다음) 이 마저 제거.
        assert _strip_speaker_prefix("의주: 안녕: 잘가") == "잘가"

    def test_inline_colon_in_dialogue_unchanged(self) -> None:
        # 문장 도중 콜론이 어절 안에 있으면 (공백 직후가 아니면) 보존
        assert _strip_speaker_prefix("그건이런뜻이야: 잘 봐") == "그건이런뜻이야: 잘 봐"

    def test_merged_segment_with_two_speakers(self) -> None:
        # 라운드 24 회귀 케이스: merge 로 두 cue 가 합쳐지면 텍스트 중간에 화자 표기가 끼인다.
        # 예) "다시 주시온이 주인공이요 우수: 그래, 지금부터는 시온이가 집착광공"
        assert _strip_speaker_prefix(
            "다시 주시온이 주인공이요 우수: 그래, 지금부터는 시온이가 집착광공"
        ) == "다시 주시온이 주인공이요 그래, 지금부터는 시온이가 집착광공"

    def test_three_speakers_inline(self) -> None:
        assert _strip_speaker_prefix(
            "의주: 안녕 우수: 잘가 시온: 또 보자"
        ) == "안녕 잘가 또 보자"


class TestWrapForAss:
    def test_short_text_single_line(self) -> None:
        # 15자 이하면 한 줄
        assert _wrap_for_ass("짧은 문장", max_chars=15) == "짧은 문장"

    def test_long_text_joined_with_newline(self) -> None:
        # max_chars 초과 시 \N 으로 묶음 (시간 분할 X)
        out = _wrap_for_ass("이건 한 문장이지만 좀 길어서 두 줄로 나뉘어야 한다", max_chars=15)
        assert "\\N" in out
        # \N 으로 합쳐도 원본 단어가 모두 포함
        assert "이건" in out and "두" in out and "한다" in out

    def test_returns_string_not_list(self) -> None:
        # build_ass_from_segments 가 한 Dialogue 이벤트에 그대로 넣을 수 있어야 함
        assert isinstance(_wrap_for_ass("hello world", max_chars=15), str)

    def test_max_lines_cap(self) -> None:
        # max_lines=2 이면 줄 수 2 초과 시 균등 재분할
        long = "한 단어 두 단어 세 단어 네 단어 다섯 단어 여섯 단어 일곱 단어 여덟 단어"
        out = _wrap_for_ass(long, max_chars=10, max_lines=2)
        assert out.count("\\N") <= 1  # 최대 2줄

    def test_empty_text(self) -> None:
        # 빈 문자열도 안전하게 통과
        assert _wrap_for_ass("", max_chars=15) == ""


class TestBuildAssFromSegments:
    """build_ass_from_segments 가 헬퍼들을 올바르게 호출해 ASS 를 만드는지 확인."""

    def test_speaker_prefix_not_in_dialogue(self, tmp_path: Path) -> None:
        seg = SpeechSegment(start_sec=0.0, end_sec=2.0, text="의주: 이건 현실이 아닐 거야")
        out = tmp_path / "sub.ass"
        build_ass_from_segments([seg], out, SubtitleStyle())
        content = out.read_text(encoding="utf-8-sig")
        # Dialogue 라인에 "의주:" 같은 화자 prefix 가 없어야 함
        for line in content.splitlines():
            if line.startswith("Dialogue:"):
                assert "의주:" not in line
                assert "이건 현실이 아닐 거야" in line

    def test_single_event_per_segment(self, tmp_path: Path) -> None:
        # 한 문장이 여러 줄로 분할돼도 Dialogue 이벤트는 1개여야 함 (시간 균등 분할 X)
        seg = SpeechSegment(
            start_sec=0.0, end_sec=3.0,
            text="이건 한 문장이지만 좀 길어서 두 줄로 나뉘어야 한다",
        )
        out = tmp_path / "sub.ass"
        build_ass_from_segments([seg], out, SubtitleStyle())
        content = out.read_text(encoding="utf-8-sig")
        dialogue_lines = [l for l in content.splitlines() if l.startswith("Dialogue:")]
        assert len(dialogue_lines) == 1
        # 줄바꿈 토큰이 들어가 있어야 한다
        assert "\\N" in dialogue_lines[0]

    def test_empty_text_after_strip_skipped(self, tmp_path: Path) -> None:
        # speaker prefix 만 있고 본문이 없는 비정상 입력은 이벤트로 만들지 않음
        seg = SpeechSegment(start_sec=0.0, end_sec=1.0, text="의주: ")
        out = tmp_path / "sub.ass"
        build_ass_from_segments([seg], out, SubtitleStyle())
        content = out.read_text(encoding="utf-8-sig")
        dialogue_lines = [l for l in content.splitlines() if l.startswith("Dialogue:")]
        assert dialogue_lines == []


class TestPerLineStyleOverride:
    """줄 단위 스타일(edit_overrides/v3 subtitles[].style) — 전역 프리셋 위에 그 줄만
    인라인 태그(\\fs·\\1c)와 이벤트 MarginV 로 얹는다."""

    def test_size_color_y_rendered_as_inline_overrides(self, tmp_path: Path) -> None:
        seg = SimpleNamespace(start_sec=0.0, end_sec=2.0, text="스타일 얹은 줄",
                              style={"size": 64, "y": 0.8, "color": "#FFDD00"})
        out = tmp_path / "sub.ass"
        build_ass_from_segments([seg], out, SubtitleStyle())
        line = next(l for l in out.read_text(encoding="utf-8-sig").splitlines()
                    if l.startswith("Dialogue:"))
        assert "\\fs64" in line
        assert "\\1c&H00DDFF&" in line                    # #FFDD00 → BGR 00DDFF
        # y=0.8 → 자막 하단이 1920*0.8 → MarginV = 1920-1536 = 384 (이벤트 필드)
        fields = line.split(",", 9)
        assert fields[7] == "384"                          # MarginV
        assert "스타일 얹은 줄" in fields[9]

    def test_partial_style_only_emits_given_keys(self, tmp_path: Path) -> None:
        seg = SimpleNamespace(start_sec=0.0, end_sec=2.0, text="색만 바꾼 줄",
                              style={"color": "#FF0000"})
        out = tmp_path / "sub.ass"
        build_ass_from_segments([seg], out, SubtitleStyle())
        line = next(l for l in out.read_text(encoding="utf-8-sig").splitlines()
                    if l.startswith("Dialogue:"))
        assert "\\1c&H0000FF&" in line and "\\fs" not in line
        assert line.split(",", 9)[7] == ""                 # MarginV 는 스타일 기본값 유지

    def test_y_bottom_edge_clamps_margin_to_one(self, tmp_path: Path) -> None:
        # ASS 규약상 이벤트 MarginV=0 은 "스타일 기본값 사용" — y=1.0 이 0 으로
        # 떨어지면 오버라이드가 조용히 사라지므로 최소 1 로 클램프해야 한다.
        seg = SimpleNamespace(start_sec=0.0, end_sec=2.0, text="맨 아래 줄",
                              style={"y": 1.0})
        out = tmp_path / "sub.ass"
        build_ass_from_segments([seg], out, SubtitleStyle())
        line = next(l for l in out.read_text(encoding="utf-8-sig").splitlines()
                    if l.startswith("Dialogue:"))
        assert line.split(",", 9)[7] == "1"

    def test_unstyled_segments_unchanged(self, tmp_path: Path) -> None:
        # SpeechSegment(스타일 attr 없음)·style 없는 SimpleNamespace 모두 종전 출력 그대로
        segs = [SpeechSegment(start_sec=0.0, end_sec=2.0, text="옛날 줄"),
                SimpleNamespace(start_sec=2.0, end_sec=4.0, text="캐시 줄")]
        out = tmp_path / "sub.ass"
        build_ass_from_segments(segs, out, SubtitleStyle())
        for line in out.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("Dialogue:"):
                assert ",Default,,,,,, " in line           # 종전 이벤트 형식 불변
                assert "{" not in line


class TestMergeSubtitleSegments:
    """merge_subtitle_segments 의 문장-종결 aware 동작."""

    def test_incomplete_sentence_merges_aggressively(self) -> None:
        # 첫 segment 가 종결어미 없이 끝남 → 다음 segment 가 max_total_chars 한도를 약간 넘어도 merge.
        segs = [
            SpeechSegment(start_sec=0.0, end_sec=1.0, text="이건 한 문장이지만 좀 길어"),  # 17자
            SpeechSegment(start_sec=1.1, end_sec=2.0, text="서 두 줄로 나뉘어야 한다"),  # 13자, 종결
        ]
        merged = merge_subtitle_segments(segs, max_gap_sec=0.5, max_total_chars=30)
        # 종결되지 않은 cur 이라 1.3배 완화(=39자) 적용 → 한 segment 로 묶임 (총 31자)
        assert len(merged) == 1
        assert merged[0].start_sec == 0.0
        assert merged[0].end_sec == 2.0

    def test_complete_sentence_does_not_overmerge(self) -> None:
        # 첫 segment 가 "다." 로 종결 → 다음과 합치면 한도 초과면 합치지 않음.
        segs = [
            SpeechSegment(start_sec=0.0, end_sec=1.0, text="이건 짧은 문장이다."),  # 11자, 종결
            SpeechSegment(start_sec=1.1, end_sec=2.0, text="이건 또 다른 긴 다음 문장이고요."),  # 18자
        ]
        merged = merge_subtitle_segments(segs, max_gap_sec=0.5, max_total_chars=25)
        # 종결이라 1.3배 완화가 적용되지 않음 → 한도 25자 초과해서 분리 유지
        assert len(merged) == 2
