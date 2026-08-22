"""E12 (2026-08-22) — 편집실 내레이션에 ElevenLabs voice_id 를 여는 접두사 어휘.

지키는 것:

1. **회귀 0** — 접두사 `elevenlabs:` 가 없는 값은 종전 경로 그대로다(백엔드 선택·
   프리셋 매핑·edge-tts 폴백·캐시 미적용까지). 지금 도는 모든 내레이션이 그 경로다.
2. **조용한 대체 금지** — 접두사 목소리인데 키가 없거나 voice_id 형태가 깨졌으면
   기본 목소리로 안 떨어지고 즉시 실패한다.
3. **캐시** — 같은 (문구, voice, speed, fit창) 은 API 를 다시 안 부른다(요금 0).
   단 유료 경로만 — edge-tts cue 는 종전대로 매번 합성한다.

네트워크는 타지 않는다 — requests 를 페이크로 갈아끼운다.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import app.modules.tts as tts
from app.pipeline import _tts_cue_cache_key, synthesize_cue_cached

_VOICE_ID = "AbC123dEf456GhI789"          # 영숫자 18자 — RPC 가 거르는 형태와 같다
_PREFIXED = f"elevenlabs:{_VOICE_ID}"


# ──────────────────────────────────────────────────────────────
# 1. 접두사 판별 · voice_id 해석
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("voice", ["ko_female", "ko_male_low", "chat_emma",
                                   "narrative_female", "", None])
def test_legacy_labels_are_not_prefixed(voice):
    """종전 라벨은 전부 접두사 경로가 아니다 — 한 글자도 안 바뀐다."""
    assert tts.is_elevenlabs_voice(voice) is False


def test_prefixed_voice_detected_and_unwrapped():
    assert tts.is_elevenlabs_voice(_PREFIXED) is True
    assert tts.elevenlabs_voice_id(_PREFIXED) == _VOICE_ID


@pytest.mark.parametrize("bad", ["elevenlabs:", "elevenlabs:짧다", "elevenlabs:abc-123-def-456",
                                 "elevenlabs:" + "a" * 15, "elevenlabs:" + "a" * 33])
def test_malformed_voice_id_fails_loud(bad):
    """형태가 깨졌으면 기본 목소리로 안 떨어지고 즉시 실패."""
    with pytest.raises(RuntimeError, match="voice_id"):
        tts.elevenlabs_voice_id(bad)


def test_missing_key_fails_with_instructions(monkeypatch, tmp_path):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as e:
        tts.synthesize_tts("안녕하세요", tmp_path / "o.mp3", voice=_PREFIXED)
    msg = str(e.value)
    assert "ELEVENLABS_API_KEY" in msg
    assert "조용히" in msg          # 폴백하지 않는다는 사실이 메시지에 있다


# ──────────────────────────────────────────────────────────────
# 2. 합성 요청 본문 — 모델·speed·voice_id
# ──────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code=200, content=b"ID3mp3", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def _install_fake_requests(monkeypatch, responses):
    calls: list[dict] = []

    class _ReqExc(Exception):
        pass

    def _post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    fake = types.ModuleType("requests")
    fake.post = _post
    fake.RequestException = _ReqExc
    monkeypatch.setitem(sys.modules, "requests", fake)
    return calls


@pytest.fixture()
def _keyed(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test")
    monkeypatch.setattr(tts, "time", types.SimpleNamespace(time=lambda: 0.0,
                                                           sleep=lambda s: None))
    return tts


def test_prefixed_voice_id_goes_into_url(_keyed, monkeypatch, tmp_path):
    calls = _install_fake_requests(monkeypatch, [_FakeResp()])
    out = tmp_path / "o.mp3"
    tts.synthesize_tts("한국어 내레이션", out, voice=_PREFIXED, speed="fast")

    assert out.read_bytes() == b"ID3mp3"
    sent = calls[0]
    assert f"/v1/text-to-speech/{_VOICE_ID}" in sent["url"]
    assert sent["headers"]["xi-api-key"] == "sk_test"
    body = sent["json"]
    assert body["model_id"] == "eleven_multilingual_v2"
    # eleven_v3 는 speed 를 안 받는다 — 편집실 속도 프리셋이 죽으므로 쓰지 않는다
    assert body["voice_settings"]["speed"] == tts.EL_SPEED["fast"]
    # language_code 는 multilingual_v2 에서 무시된다 — 보내지 않는다
    assert "language_code" not in body


@pytest.mark.parametrize("speed,expected", [
    ("very_slow", 0.7), ("slow", 0.85), ("normal", 1.0), ("fast", 1.1), ("very_fast", 1.2),
])
def test_speed_labels_map_into_allowed_range(_keyed, monkeypatch, tmp_path, speed, expected):
    """다섯 단이 전부 0.7~1.2(문서 허용 범위) 안에 눌러 담긴다 — E11 표 그대로."""
    calls = _install_fake_requests(monkeypatch, [_FakeResp()])
    tts.synthesize_tts("문장", tmp_path / "o.mp3", voice=_PREFIXED, speed=speed)
    sent_speed = calls[0]["json"]["voice_settings"]["speed"]
    assert sent_speed == expected
    assert 0.7 <= sent_speed <= 1.2


def test_unknown_voice_id_is_permanent_failure(_keyed, monkeypatch, tmp_path):
    """없는 voice_id(404)는 재시도 없이 즉시 실패 — 기본 목소리 폴백 없음."""
    calls = _install_fake_requests(monkeypatch, [_FakeResp(404, text="voice not found")])
    with pytest.raises(RuntimeError, match="ElevenLabs"):
        tts.synthesize_tts("문장", tmp_path / "o.mp3", voice=_PREFIXED)
    assert len(calls) == 1


def test_rate_limit_is_transient(_keyed, monkeypatch, tmp_path):
    calls = _install_fake_requests(monkeypatch, [_FakeResp(429, text="rate")])
    with pytest.raises(RuntimeError):
        tts.synthesize_tts("문장", tmp_path / "o.mp3", voice=_PREFIXED)
    assert len(calls) == 3          # 첫 시도 + 2회 재시도


def test_prefixed_never_falls_back_to_edge_tts(_keyed, monkeypatch, tmp_path):
    """키가 있어도 edge-tts 경로는 접두사 목소리를 절대 안 받는다."""
    _install_fake_requests(monkeypatch, [_FakeResp()])

    def _boom(*a, **k):
        raise AssertionError("edge-tts 가 불렸다 — 접두사 목소리는 오면 안 된다")

    monkeypatch.setattr(tts, "_synthesize_edge_tts", _boom)
    tts.synthesize_tts("문장", tmp_path / "o.mp3", voice=_PREFIXED)


def test_legacy_label_still_uses_preset_id(_keyed, monkeypatch, tmp_path):
    """종전 라벨은 E11 프리셋 매핑 그대로 — 접두사 도입이 여기 손대지 않았다."""
    calls = _install_fake_requests(monkeypatch, [_FakeResp()])
    tts.synthesize_tts("문장", tmp_path / "o.mp3", voice="ko_male")
    assert tts.EL_VOICE_PRESETS["ko_male"] in calls[0]["url"]


# ──────────────────────────────────────────────────────────────
# 3. 캐시 — 유료 경로만
# ──────────────────────────────────────────────────────────────


def _fake_fit(monkeypatch, counter: list):
    """synthesize_tts_with_fit 대체 — 호출 횟수를 세고 mp3 를 만든다."""
    def _fit(text, output_path, *, target_sec, voice, speed, shorten_fn=None, **kw):
        counter.append((text, voice, speed, target_sec))
        Path(output_path).write_bytes(b"mp3-" + text.encode("utf-8"))
        return text, 2.0
    monkeypatch.setattr(tts, "synthesize_tts_with_fit", _fit)


def test_cache_prevents_second_synthesis(monkeypatch, tmp_path):
    calls: list = []
    _fake_fit(monkeypatch, calls)
    cache = tmp_path / "tts_cache"

    a = synthesize_cue_cached("같은 문구", tmp_path / "cue0.mp3", target_sec=3.0,
                              voice=_PREFIXED, speed="normal", cache_dir=cache)
    b = synthesize_cue_cached("같은 문구", tmp_path / "cue1.mp3", target_sec=3.0,
                              voice=_PREFIXED, speed="normal", cache_dir=cache)

    assert len(calls) == 1                     # 두 번째는 API 를 안 탄다(요금 0)
    assert a == b
    assert (tmp_path / "cue1.mp3").read_bytes() == (tmp_path / "cue0.mp3").read_bytes()


@pytest.mark.parametrize("second", [
    {"text": "다른 문구"},
    {"speed": "fast"},
    {"voice": "elevenlabs:ZzZ999yYy888xXx77"},
    {"target_sec": 4.0},
])
def test_cache_key_covers_all_four_inputs(monkeypatch, tmp_path, second):
    """넷 중 하나만 달라져도 다른 소리 — 캐시가 잘못 재사용하면 안 된다."""
    calls: list = []
    _fake_fit(monkeypatch, calls)
    cache = tmp_path / "tts_cache"
    base = {"text": "문구", "voice": _PREFIXED, "speed": "normal", "target_sec": 3.0}

    synthesize_cue_cached(base["text"], tmp_path / "a.mp3", target_sec=base["target_sec"],
                          voice=base["voice"], speed=base["speed"], cache_dir=cache)
    merged = {**base, **second}
    synthesize_cue_cached(merged["text"], tmp_path / "b.mp3", target_sec=merged["target_sec"],
                          voice=merged["voice"], speed=merged["speed"], cache_dir=cache)
    assert len(calls) == 2


def test_legacy_voice_is_not_cached(monkeypatch, tmp_path):
    """edge-tts cue 는 종전대로 매번 합성한다 — 캐시를 끼우면 fit 재작성 결과가 달라진다."""
    calls: list = []
    _fake_fit(monkeypatch, calls)
    cache = tmp_path / "tts_cache"
    for i in range(2):
        synthesize_cue_cached("같은 문구", tmp_path / f"c{i}.mp3", target_sec=3.0,
                              voice="ko_female", speed="normal", cache_dir=cache)
    assert len(calls) == 2
    assert not cache.exists()


def test_cache_key_is_stable_and_distinct():
    k1 = _tts_cue_cache_key("문구", _PREFIXED, "normal", 3.0)
    k2 = _tts_cue_cache_key("문구", _PREFIXED, "normal", 3.004)   # 소수 2자리 반올림 동일
    k3 = _tts_cue_cache_key("문구", _PREFIXED, "slow", 3.0)
    assert k1 == k2 != k3
