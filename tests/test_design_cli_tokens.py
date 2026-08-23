"""design_cli_tokens — 재현 렌더(현지화 L4)가 복원할 '명시된 디자인'의 계약.

이 함수가 비면 JP 완성본이 채널 화면비를 잃는다(2026-08-23 SHOTCONE 실측:
편집실·채널 템플릿의 aspect_ratio 13:9 가 완성본에서 엔진 기본값 1:1 로 나갔다).
"""
import argparse

from app.cli import build_parser, design_cli_tokens


def _args(argv):
    return build_parser().parse_args(
        ["create_shorts", "--title", "T", "--video", "/tmp/x.mp4", *argv])


def test_empty_when_nothing_specified():
    """아무 디자인도 안 준 런은 빈 목록 — 재현 렌더도 종전과 같은 명령이 된다(회귀 0)."""
    assert design_cli_tokens(_args([])) == []


def test_value_flags_roundtrip():
    got = design_cli_tokens(_args(["--design-aspect-ratio", "13:9",
                                   "--design-video-y", "440",
                                   "--design-title-color2", "#FF5C8A"]))
    assert got == ["--design-aspect-ratio", "13:9",
                   "--design-title-color2", "#FF5C8A",
                   "--design-video-y", "440"]          # dest 사전순
    # 되먹였을 때 같은 DesignConfig 가 나와야 재현이 성립한다
    from app.cli import _build_design_config
    assert _build_design_config(_args(got)) == _build_design_config(
        _args(["--design-aspect-ratio", "13:9", "--design-video-y", "440",
               "--design-title-color2", "#FF5C8A"]))


def test_store_true_only_when_on():
    assert design_cli_tokens(_args(["--design-title-y-fixed"])) == ["--design-title-y-fixed"]
    assert "--no-reframe" in design_cli_tokens(_args(["--no-reframe"]))
    assert "--design-title-bold" not in design_cli_tokens(_args([]))


def test_unspecified_video_width_stays_unspecified():
    """E10 게이트의 핵심 — '미지정'과 '명시한 1080'은 자막·TTS margin 기하가 다르다.
    asdict(DesignConfig) 를 남겼다면 여기서 1080 이 튀어나와 재현이 원본을 바꾼다."""
    assert "--design-video-width" not in design_cli_tokens(_args([]))
    assert design_cli_tokens(_args(["--design-video-width", "800"])) == \
        ["--design-video-width", "800"]


def test_accepts_plain_dict():
    """순수 함수 — argparse 없이 dict 로도 계약이 성립한다."""
    assert design_cli_tokens({"design_title_y": 160, "design_title_bold": False,
                              "no_reframe": True, "title": "무관"}) == \
        ["--design-title-y", "160", "--no-reframe"]
