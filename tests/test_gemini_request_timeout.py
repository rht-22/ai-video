"""Gemini 요청 타임아웃(2026-09-17, 로또 clip01 실사고) 회귀 가드.

- 미지정 = 600s · env 로 바꿀 수 있다 · 오타·범위 밖은 즉시 실패(조용한 무한 대기 금지).
- tikitaka 클라이언트의 재시도 판정이 httpx ReadTimeout(빈 메시지)·서버 연결 끊김을 일시 오류로 본다.
"""
from __future__ import annotations

import pytest

from app import model_policy
from app.tikitaka import llm


def test_default_timeout_is_600s(monkeypatch):
    monkeypatch.delenv("GEMINI_REQUEST_TIMEOUT_SEC", raising=False)
    assert model_policy.gemini_request_timeout_sec() == 600.0


def test_env_overrides_timeout(monkeypatch):
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_SEC", "900")
    assert model_policy.gemini_request_timeout_sec() == 900.0


@pytest.mark.parametrize("raw", ["abc", "5", "99999"])
def test_bad_timeout_fails_loud(monkeypatch, raw):
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_SEC", raw)
    with pytest.raises(ValueError):
        model_policy.gemini_request_timeout_sec()


def test_transient_classification_covers_timeout_and_disconnect():
    import httpx
    for exc in (httpx.ReadTimeout(""), httpx.RemoteProtocolError("Server disconnected without sending a response.")):
        assert any(k in llm._err_text(exc) for k in llm._TRANSIENT), exc
    # 비일시 오류는 종전대로 재시도 대상이 아니다
    assert not any(k in llm._err_text(ValueError("JSON 파싱 실패")) for k in llm._TRANSIENT)


def test_both_clients_pass_timeout_to_sdk(monkeypatch):
    """두 클라이언트가 같은 값을 SDK http_options 로 넘긴다(ms 단위)."""
    from google.genai import types
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_SEC", "120")
    captured: list[types.HttpOptions] = []

    class _FakeClient:
        def __init__(self, api_key=None, http_options=None, **_):
            captured.append(http_options)
            self.models = object()
            self.files = object()

    import google.genai as genai
    monkeypatch.setattr(genai, "Client", _FakeClient)

    llm.Gemini(api_key="k", log=lambda *_: None)
    from app.modules.gemini_client import GeminiClient, GeminiConfig
    GeminiClient(GeminiConfig(api_key="k"))

    assert len(captured) == 2
    assert all(isinstance(h, types.HttpOptions) and h.timeout == 120_000 for h in captured)
