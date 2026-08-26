"""일본어 내레이션을 ElevenLabs 목소리로 (2026-08-26, 사용자 지시).

> "혜미리예채파 일본어 나레이션도 일레븐랩스 일본어 목소리로 변경해줘."

지금까지 이 경로는 **edge-tts 를 직접 불렀다**(`ja-JP-NanamiNeural`). E11·E12 로 만든
ElevenLabs 층(키 검사·실패 분류·토큰 만료 폴백·캐시)을 지나지 않아, 목소리 id 만
바꿔 넣을 수가 없었다. 이 파일은 그 배선을 고정한다.

⚠ 회귀 0: 매핑이 종전처럼 `ja-JP-*` 면 edge-tts 그대로다 — 한 글자도 안 바뀐다.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.localize import narration  # noqa: E402
from app.modules.tts import EL_SPEED, is_elevenlabs_voice  # noqa: E402

SRC = pathlib.Path("app/localize/narration.py").read_text(encoding="utf-8")
BODY = SRC.split("def l3t_tts", 1)[-1] if "def l3t_tts" in SRC else SRC


def test_prefixed_voices_go_through_the_shared_tts_layer():
    """🛑 편집실이 쓰는 것과 **같은 통로**여야 한다 — 키 검사·실패 분류가 거기 있다."""
    assert "from app.modules.tts import is_elevenlabs_voice, synthesize_tts" in SRC
    assert 'is_elevenlabs_voice(prof["voice_id"])' in SRC


def test_edge_tts_path_is_untouched():
    """회귀 0 — ja-JP-* 매핑은 종전 코드 그대로 돈다."""
    assert "edge_tts.Communicate(" in SRC
    assert "rate=rate, pitch=prof.get(\"pitch\", \"+0Hz\")" in SRC


def test_window_fitting_uses_speed_labels_on_the_paid_path():
    """ElevenLabs 는 rate(%) 를 안 받는다 — 눈금이 speed 라벨이다."""
    assert narration.EL_SPEED_BUMPS == ("normal", "fast", "very_fast")
    assert len(narration.EL_SPEED_BUMPS) == len(narration.RATE_BUMPS)
    # 그 라벨이 실제로 tts 계약에 있는 값이어야 한다(오타면 합성이 즉시 실패한다).
    for label in narration.EL_SPEED_BUMPS:
        assert label in EL_SPEED


def test_the_two_scales_are_not_the_same_and_we_say_so():
    """⚠ edge +30% 와 ElevenLabs 1.2 는 다르다 — 주석이 그 사실을 들고 있어야 한다."""
    assert "끝이 다르다" in SRC


def test_trimming_still_backs_it_up():
    """상한까지 올려도 창을 넘으면 잘라야 한다 — L3t 계약(편이 통째로 실패하는 것보다 낫다)."""
    assert "_trim_to_window(mp3, window)" in SRC


def test_prefix_detection_contract():
    assert is_elevenlabs_voice("elevenlabs:abcdefghijklmnop")
    assert not is_elevenlabs_voice("ja-JP-NanamiNeural")


def test_voice_map_is_data_not_code():
    """목소리를 바꾸는 것은 코드 수정이 아니라 locales.json 한 줄이어야 한다."""
    import json
    cfg = json.loads(pathlib.Path("app/localize/data/locales.json").read_text(encoding="utf-8"))
    ja = ((cfg.get("locales") or {}).get("ja") or {}).get("tts_voice_map") or {}
    assert ja.get("_default", {}).get("voice_id")
    for label, prof in ja.items():
        if label.startswith("_"):
            continue
        assert "voice_id" in prof, label
