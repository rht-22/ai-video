"""스토리 구성이 조용히 폴백으로 떨어지던 문제 — 잘림 판별 + 폴백 표식·사유 기록.

2026-08-22 실측: 최근 21일 폴백률 약 7%(반려·재생성 run 은 16.7%). 폴백 산출물은
시간축 [0, n/3, 2n/3, n-1] 샘플링이라 서로 무관한 장면이 이어붙고 제목이
'작품명 + 설명 앞 20자' 가 된다(clip 29ad59d0 = 한 입 주막/가왕쇼 사례).
그런데 그게 폴백인지는 selection_reason 문자열을 눈으로 읽어야만 알 수 있었고,
왜 실패했는지는 실행 노드 stdout 에만 있었다.

여기서 고정하는 것:
1. MAX_TOKENS 잘림을 잘림으로 판별하고 재시도한다 (분석 경로 8/4 커밋 80b00aa 와 같은 방식).
2. 폴백이면 반환값에 fallback=true 와 fallback_reason(사유·finish_reason·응답 앞머리)이 실린다.
3. 정상 경로는 fallback=false.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.modules.gemini_client import (
    GeminiClient,
    GeminiConfig,
    _build_fallback_story,
)


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """재시도 백오프(1s+2s)를 테스트에서 실제로 기다릴 이유가 없다."""
    monkeypatch.setattr("app.modules.gemini_client.time.sleep", lambda _s: None)


# ── 가짜 SDK ──────────────────────────────────────────────
class _FakeTypes:
    """types.GenerateContentConfig / ThinkingConfig 자리만 채운다."""

    @staticmethod
    def GenerateContentConfig(**kwargs):  # noqa: N802 — SDK 이름 그대로
        return SimpleNamespace(**kwargs)

    @staticmethod
    def ThinkingConfig(**kwargs):  # noqa: N802
        return SimpleNamespace(**kwargs)


def _response(text: str, finish: str = "STOP", usage=None):
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
        usage_metadata=usage,
    )


class _FakeModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


def _client(responses, *, max_retries: int = 3) -> tuple[GeminiClient, _FakeModels]:
    client = GeminiClient.__new__(GeminiClient)  # __init__ 은 실제 SDK 를 붙는다
    client.config = GeminiConfig(api_key="test", max_retries=max_retries)
    client.types = _FakeTypes
    models = _FakeModels(responses)
    client.client = SimpleNamespace(models=models)
    return client, models


CANDIDATES = [
    {"chunk_index": 0, "candidate_index": i, "start_sec": 100.0 * i,
     "end_sec": 100.0 * i + 12.0, "description": f"장면 {i}"}
    for i in range(6)
]


def _valid_story_json() -> str:
    return json.dumps({
        "storylines": [{
            "shorts_type": "storytelling",
            "sequence_type": "여정몰입형",
            "score": 0.8,
            "storyline": {
                "hook": {"chunk_index": 0, "candidate_index": 0,
                         "start_sec": 0.0, "end_sec": 12.0},
                "build": [],
                "payoff": {"chunk_index": 0, "candidate_index": 1,
                           "start_sec": 100.0, "end_sec": 112.0},
            },
        }],
        "selected_storyline_index": 0,
        "title_line1": "정상 제목",
        "title_line2": "두번째 줄",
    }, ensure_ascii=False)


def _compose(client: GeminiClient):
    return client.compose_story_with_context(CANDIDATES, "가왕쇼", "테스트 주제")


# ── 1. 잘림 판별 ───────────────────────────────────────────
def test_truncated_story_is_recognized_as_truncation_not_json_error():
    """MAX_TOKENS 로 끊긴 조각은 파싱 전에 걸러 kind=max_tokens 로 남는다."""
    usage = SimpleNamespace(prompt_token_count=30000, thoughts_token_count=30000,
                            candidates_token_count=5000, total_token_count=65000)
    truncated = _response('{"storylines": [{"shorts_type": "storyte',
                          finish="MAX_TOKENS", usage=usage)
    client, models = _client([truncated])

    result = _compose(client)

    assert result["fallback"] is True
    assert result["fallback_reason"]["kind"] == "max_tokens"
    assert result["fallback_reason"]["finish_reason"] == "MAX_TOKENS"
    # 잘림도 재시도 대상 — 한 번 보고 바로 폴백하지 않는다
    assert models.calls == 3
    # 진단에 필요한 사용량·응답 앞머리가 남는다
    assert "추론 30000" in result["fallback_reason"]["error"]
    assert result["fallback_reason"]["response_head"].startswith('{"storylines"')
    # 숫자 사용량도 남는다 — 잘림 비율·thinking 예산을 나중에 집계하려면 문자열로는 못 센다
    assert result["fallback_reason"]["usage"] == {
        "prompt_token_count": 30000, "thoughts_token_count": 30000,
        "candidates_token_count": 5000, "total_token_count": 65000,
    }


def test_truncation_recovers_on_retry():
    """첫 응답이 잘려도 재시도가 성공하면 폴백이 아니다."""
    truncated = _response('{"storylines": [', finish="MAX_TOKENS")
    ok = _response(_valid_story_json())
    client, models = _client([truncated, ok])

    result = _compose(client)

    assert result["fallback"] is False
    assert result["title_line1"] == "정상 제목"
    assert models.calls == 2


# ── 2. 폴백 표식·사유 ──────────────────────────────────────
def test_json_error_fallback_records_reason():
    client, models = _client([_response("이건 JSON 이 아니다")])

    result = _compose(client)

    reason = result["fallback_reason"]
    assert result["fallback"] is True
    assert reason["kind"] == "json_decode_error"
    assert reason["finish_reason"] == "STOP"
    assert reason["attempt"] == 3 and reason["max_retries"] == 3
    assert reason["response_head"] == "이건 JSON 이 아니다"
    assert reason["thinking_level"] == client.config.story_thinking_level
    assert models.calls == 3


def test_validation_error_fallback_records_reason():
    """구조 검증 실패(storylines 없음)도 kind 로 구분된다."""
    client, _ = _client([_response(json.dumps({"nope": 1}))])

    result = _compose(client)

    assert result["fallback"] is True
    assert result["fallback_reason"]["kind"] == "validation_error"


def test_empty_response_fallback_records_reason():
    client, _ = _client([_response("")])

    result = _compose(client)

    assert result["fallback"] is True
    assert result["fallback_reason"]["kind"] == "empty_response"


def test_api_exception_fallback_records_reason():
    class _Boom:
        calls = 0

        def generate_content(self, **kwargs):
            _Boom.calls += 1
            raise RuntimeError("503 Service Unavailable")

    client, _ = _client([])
    client.client = SimpleNamespace(models=_Boom())

    result = _compose(client)

    assert result["fallback"] is True
    assert result["fallback_reason"]["kind"] == "exception"
    assert "503" in result["fallback_reason"]["error"]


def test_selection_reason_still_readable_and_now_carries_cause():
    """사람이 읽던 문자열은 유지하되 사유를 덧붙인다(검수 화면 하위 호환)."""
    client, _ = _client([_response("깨진 응답")])

    result = _compose(client)

    assert result["selection_reason"].startswith("폴백: Gemini 응답 실패")
    assert "json_decode_error" in result["selection_reason"]


# ── 3. 정상 경로 ───────────────────────────────────────────
def test_success_marks_fallback_false():
    client, models = _client([_response(_valid_story_json())])

    result = _compose(client)

    assert result["fallback"] is False
    assert "fallback_reason" not in result
    assert models.calls == 1


# ── 4. _build_fallback_story 단독 계약 ─────────────────────
def test_build_fallback_story_without_failure_info():
    """사유를 못 받아도 폴백이라는 사실은 남는다."""
    out = _build_fallback_story(CANDIDATES, "가왕쇼")

    assert out["fallback"] is True
    assert out["fallback_reason"]["kind"] == "unknown"
    assert out["storylines"][0]["fallback"] is True
    assert out["selected_storyline"]["fallback"] is True


def test_build_fallback_story_needs_candidates():
    with pytest.raises(RuntimeError):
        _build_fallback_story([], "가왕쇼")


# ── 5. 체크포인트 복원 시 표식 승계 ────────────────────────
def test_checkpoint_fallback_flag_is_read():
    from app.pipeline import _story_checkpoint_fallback

    is_fb, reason = _story_checkpoint_fallback(
        {"fallback": True, "fallback_reason": {"kind": "max_tokens"}}
    )
    assert is_fb is True and reason["kind"] == "max_tokens"


def test_checkpoint_legacy_selection_reason_still_counts_as_fallback():
    """표식 도입 전 체크포인트(문자열만 있는 편)도 재실행 시 폴백으로 잡힌다."""
    from app.pipeline import _story_checkpoint_fallback

    legacy = {"raw_response": {"selection_reason": "폴백: Gemini 응답 실패 — 상위 moment 자동 조합"}}
    is_fb, reason = _story_checkpoint_fallback(legacy)
    assert is_fb is True
    assert reason["kind"] == "legacy_checkpoint"


def test_checkpoint_normal_story_is_not_fallback():
    from app.pipeline import _story_checkpoint_fallback

    normal = {"fallback": False,
              "raw_response": {"selection_reason": "hook 의 감정 낙차가 가장 크다"}}
    assert _story_checkpoint_fallback(normal) == (False, None)
    assert _story_checkpoint_fallback({}) == (False, None)
