"""잘린 Gemini 응답을 '잘림' 으로 알아보는지 — 2026-07-30·31 생성 실패 3건의 원인.

finish_reason=MAX_TOKENS 로 끊긴 응답을 그대로 파싱하면 JSONDecodeError 가 나서 원인이
'파서 문제' 로 보인다. 잘림은 파싱 전에 잘림이라고 말해야 한다.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.modules.gemini_client import _max_tokens_usage


def _response(finish_name, usage=None):
    finish = SimpleNamespace(name=finish_name) if finish_name else None
    return SimpleNamespace(candidates=[SimpleNamespace(finish_reason=finish)],
                           usage_metadata=usage)


def test_stop_is_not_truncation():
    assert _max_tokens_usage(_response("STOP")) is None


def test_max_tokens_reports_usage():
    usage = SimpleNamespace(prompt_token_count=12000, thoughts_token_count=41000,
                            candidates_token_count=12500, total_token_count=65500)
    out = _max_tokens_usage(_response("MAX_TOKENS", usage))
    assert out is not None
    assert "추론 41000" in out and "합계 65500" in out


def test_max_tokens_without_usage_metadata():
    """사용량이 없어도 잘림 판정 자체는 살아 있어야 한다(진단 정보만 빠진다)."""
    assert _max_tokens_usage(_response("MAX_TOKENS")) == "사용량 정보 없음"


def test_missing_candidates_is_not_truncation():
    assert _max_tokens_usage(SimpleNamespace(candidates=[])) is None
    assert _max_tokens_usage(SimpleNamespace()) is None


def test_enum_without_name_falls_back_to_str():
    """SDK 판에 따라 finish_reason 이 문자열/정수 enum 으로 온다."""
    resp = SimpleNamespace(candidates=[SimpleNamespace(finish_reason="FinishReason.MAX_TOKENS")],
                           usage_metadata=None)
    assert _max_tokens_usage(resp) is not None
