"""라우드니스 정규화 헬퍼 테스트 (_apply_loudnorm).

배경: ai-video 출력 클립이 시장 클립 대비 ~9 LUFS 더 조용함(−32 vs −23 LUFS, 벤치마크).
최종 오디오 라벨 [aout] 에 loudnorm 을 덧붙여 쇼츠 표준(≈−14 LUFS)으로 정규화한다.
target None 이면 무변경(A/B 대조군)."""
from app.cli import _parse_loudness
from app.modules.renderer import _apply_loudnorm


def test_parse_loudness_number():
    assert _parse_loudness("-14") == -14.0
    assert _parse_loudness("-16.5") == -16.5


def test_parse_loudness_off_variants_none():
    for s in ("off", "OFF", "none", "null", "", "  "):
        assert _parse_loudness(s) is None


def test_parse_loudness_invalid_falls_back_to_default():
    # 잘못된 값은 기본값(-14)로 — 렌더가 깨지지 않게
    assert _parse_loudness("garbage") == -14.0


def test_appends_loudnorm_to_final_aout():
    out = _apply_loudnorm("[acat]volume=-10dB[aout]", -14.0)
    assert out == "[acat]volume=-10dB[apremix];[apremix]loudnorm=I=-14.0:TP=-1.5:LRA=11[aout]"


def test_none_target_leaves_unchanged():
    s = "[acat]volume=-10dB[aout]"
    assert _apply_loudnorm(s, None) == s


def test_works_with_amix_tts_chain_single_final_label():
    src = ("[acat]volume=-10dB[orig_vol];[2:a]volume=-4dB[cue0_vol];"
           "[orig_vol][cue0_vol]amix=inputs=2:duration=longest:dropout_transition=2[aout]")
    out = _apply_loudnorm(src, -14.0)
    assert out.endswith("[apremix]loudnorm=I=-14.0:TP=-1.5:LRA=11[aout]")
    assert out.count("[aout]") == 1          # 최종 라벨만 남음
    assert "[apremix]" in out


def test_custom_target_value():
    out = _apply_loudnorm("[acat]x[aout]", -16.0)
    assert "loudnorm=I=-16.0:TP=-1.5:LRA=11[aout]" in out
