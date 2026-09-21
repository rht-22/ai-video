"""ElevenLabs stability env 덮어쓰기(2026-09-18, 로또 clip01 KR Minjun 실측) 회귀 가드.

- 미지정 = 0.5(종전 값 바이트 동일 — 다른 채널 회귀 0) · env 로 바꿀 수 있다 · 오타·범위 밖은 즉시 실패.
- 실제 요청 본문의 voice_settings.stability 가 그 값을 싣는다(가짜 requests 로 확인).
"""
from __future__ import annotations

import sys
import types

import pytest

from app.modules import tts


def test_default_stability_is_unchanged(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_STABILITY", raising=False)
    assert tts.el_stability() == 0.5 == tts.EL_STABILITY_DEFAULT


def test_env_overrides_stability(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_STABILITY", "0.9")
    assert tts.el_stability() == 0.9


@pytest.mark.parametrize("raw", ["abc", "-0.1", "1.5"])
def test_bad_stability_fails_loud(monkeypatch, raw):
    monkeypatch.setenv("ELEVENLABS_STABILITY", raw)
    with pytest.raises(ValueError):
        tts.el_stability()


def test_request_body_carries_stability(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_STABILITY", "0.9")
    seen: dict = {}

    class _Resp:
        status_code = 200
        content = b"ID3fake"
        headers = {"content-type": "audio/mpeg"}
        text = ""

        def raise_for_status(self):
            return None

    def fake_post(url, json=None, headers=None, timeout=None, **_):
        seen["json"] = json
        return _Resp()

    fake_requests = types.SimpleNamespace(post=fake_post, exceptions=types.SimpleNamespace(RequestException=Exception))
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    out = tmp_path / "cue.mp3"
    try:
        tts._synthesize_elevenlabs("테스트", out, voice="elevenlabs:DkAJhrMznCtWh38YrjTv", speed="very_fast")
    except Exception as e:  # noqa: BLE001 — 본문 조립 뒤의 응답 처리 차이는 이 테스트의 관심이 아니다
        if "json" not in seen:
            raise AssertionError(f"요청 전에 실패: {e}")
    assert seen["json"]["voice_settings"]["stability"] == 0.9
    assert seen["json"]["voice_settings"]["speed"] == 1.2
