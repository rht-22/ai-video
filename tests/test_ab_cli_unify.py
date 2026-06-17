"""A/B 인터페이스 통일 테스트 — CLI 플래그 → env (병행 세션 config 가 env 로 읽음).

통일 결정: 모든 A/B 노브(loudness/silence/length)를 CLI 플래그로 노출.
silence/length 의 내부 플러밍은 env(config default_factory) 이므로 cli 가 플래그→env 로 변환한다."""
import os
from argparse import Namespace

from app.cli import _apply_ab_env, build_parser


def _restore(keys, saved):
    for k in keys:
        if saved[k] is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = saved[k]


def test_silence_profile_sets_env():
    keys = ["SILENCE_CUT_PROFILE"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        applied = _apply_ab_env(Namespace(silence_profile="aggressive", length_profile="standard"))
        assert os.environ["SILENCE_CUT_PROFILE"] == "aggressive"
        assert applied["SILENCE_CUT_PROFILE"] == "aggressive"
    finally:
        _restore(keys, saved)


def test_length_profile_tight_sets_duration_env():
    keys = ["TARGET_DURATION_SEC", "MAX_DURATION_SEC", "MAX_DURATION_TOLERANCE"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        _apply_ab_env(Namespace(silence_profile="conservative", length_profile="tight"))
        assert os.environ["MAX_DURATION_TOLERANCE"] == "1.1"   # 1.5 → 1.1 (상한 ↓)
        assert int(os.environ["MAX_DURATION_SEC"]) <= 50
        assert int(os.environ["TARGET_DURATION_SEC"]) < 50      # 시장 ~46 쪽으로
    finally:
        _restore(keys, saved)


def test_standard_length_does_not_force_env():
    keys = ["MAX_DURATION_TOLERANCE"]
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.pop("MAX_DURATION_TOLERANCE", None)
    try:
        _apply_ab_env(Namespace(silence_profile="conservative", length_profile="standard"))
        # standard → 강제 설정 안 함(config 기본값 1.5 사용)
        assert "MAX_DURATION_TOLERANCE" not in os.environ
    finally:
        _restore(keys, saved)


def test_cli_exposes_both_profiles():
    p = build_parser()
    a = p.parse_args(["create_shorts", "--title", "T", "--video", "x.mp4", "--subtitle", "x.srt",
                      "--silence-profile", "aggressive", "--length-profile", "tight"])
    assert a.silence_profile == "aggressive" and a.length_profile == "tight"
