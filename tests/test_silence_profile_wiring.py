"""무음 프로파일 A/B 배선 테스트 — CLI → PipelineInput → cut_silence_with_story_filter(profile=).

배경: PR-6 이 SilenceCutProfile(conservative/aggressive)을 추가했으나 파이프라인 호출이
구(舊) kwargs(max_gap_sec=...)를 넘겨 깨져 있었음. 이 배선이 그걸 고치고 A/B 선택을 노출."""
import inspect

from app.cli import build_parser
from app.modules.silence_cutter import (
    cut_silence_with_story_filter,
    get_silence_profile,
)


def _args(*extra):
    p = build_parser()
    return p.parse_args(["create_shorts", "--title", "T", "--video", "x.mp4",
                         "--subtitle", "x.srt", *extra])


def test_cli_silence_profile_parsed():
    assert _args("--silence-profile", "aggressive").silence_profile == "aggressive"


def test_cli_default_is_conservative():
    assert _args().silence_profile == "conservative"


def test_pipeline_call_signature_accepts_profile_not_old_kwargs():
    sig = inspect.signature(cut_silence_with_story_filter)
    assert "profile" in sig.parameters
    # 파이프라인이 실제로 넘기는 형태 — 바인딩 성공해야(깨졌던 max_gap_sec 형태가 아님)
    sig.bind_partial(None, None, None, profile=get_silence_profile("aggressive"))


def test_profile_resolution():
    assert get_silence_profile("aggressive").gap_level is True
    assert get_silence_profile("conservative").gap_level is False
    assert get_silence_profile(None).name == "conservative"   # 미지정 → 베이스라인
