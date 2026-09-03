"""Gemini 호출 원장(app/modules/gemini_usage) 회귀 가드 — SDK·네트워크 없이 돈다.

계기(2026-09-03): 반려 루프가 몇 번 돌았고 Gemini 를 몇 번 불렀는지 run_log 에 합계가
없었다. 호출부가 24군데라 클라이언트 한 곳(CountingClient)에서 가로챈다.

고정하는 것:
  · generate_content / files.upload 호출 수·토큰·소요·실패가 원장에 남는다
  · 예외는 기록 뒤 **그대로 다시 던진다**(삼키지 않는다 — 재시도 루프가 그 예외를 본다)
  · 그 외 속성(files.delete, models 의 다른 메서드)은 손대지 않고 위임한다
  · close_stage() 는 미태그 호출에만 단계를 붙인다(step 이 닫는 규약)
  · GeminiClient 가 실제로 프록시를 끼운다
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.modules.gemini_usage import CountingClient, GeminiUsage


def _resp(prompt=100, thoughts=20, output=30):
    return SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=prompt, thoughts_token_count=thoughts,
        candidates_token_count=output, total_token_count=prompt + thoughts + output),
        text="{}")


class _FakeModels:
    def __init__(self, fail_on: set[int] | None = None):
        self.n = 0
        self.fail_on = fail_on or set()
        self.seen: list[dict] = []

    def generate_content(self, *, model, contents, config=None):
        self.n += 1
        self.seen.append({"model": model})
        if self.n in self.fail_on:
            raise RuntimeError("boom")
        return _resp()

    def other(self):
        return "passthrough"


class _FakeFiles:
    def __init__(self):
        self.uploaded = []
        self.deleted = []

    def upload(self, *, file):
        self.uploaded.append(file)
        return SimpleNamespace(name="files/abc", uri="gs://abc")

    def delete(self, *, name):
        self.deleted.append(name)


def _client(fail_on=None):
    ledger = GeminiUsage()
    real = SimpleNamespace(models=_FakeModels(fail_on), files=_FakeFiles(), vertexai=False)
    return CountingClient(real, ledger), ledger, real


def test_counts_calls_tokens_and_models():
    c, ledger, _ = _client()
    c.models.generate_content(model="gemini-3.7-flash", contents=["a"])
    c.models.generate_content(model="gemini-3.7-flash", contents=["b"])
    c.models.generate_content(model="gemini-3.1-pro-preview", contents=["c"])
    s = ledger.summary()
    assert s["generate_calls"] == 3 and s["upload_calls"] == 0 and s["errors"] == 0
    assert s["tokens"] == {"prompt": 300, "thoughts": 60, "output": 90, "total": 450}
    assert s["by_model"]["gemini-3.7-flash"]["calls"] == 2
    assert s["by_model"]["gemini-3.1-pro-preview"]["total_tokens"] == 150


def test_failed_call_is_counted_and_reraised():
    c, ledger, _ = _client(fail_on={2})
    c.models.generate_content(model="m", contents=["a"])
    with pytest.raises(RuntimeError, match="boom"):
        c.models.generate_content(model="m", contents=["b"])
    c.models.generate_content(model="m", contents=["c"])
    s = ledger.summary()
    assert s["generate_calls"] == 3 and s["errors"] == 1
    assert ledger.calls[1]["ok"] is False and ledger.calls[1]["error"] == "RuntimeError"
    assert ledger.calls[1]["tokens"] == {}


def test_upload_counted_and_other_file_ops_pass_through():
    c, ledger, real = _client()
    c.files.upload(file="/tmp/x.mp4")
    c.files.delete(name="files/abc")
    assert real.files.uploaded == ["/tmp/x.mp4"] and real.files.deleted == ["files/abc"]
    s = ledger.summary()
    assert s["upload_calls"] == 1 and s["generate_calls"] == 0
    # 모델 쪽 다른 메서드·클라이언트 속성도 그대로
    assert c.models.other() == "passthrough"
    assert c.vertexai is False


def test_close_stage_tags_only_untagged_calls():
    c, ledger, _ = _client()
    c.models.generate_content(model="m", contents=["a"])
    c.files.upload(file="f")
    assert ledger.close_stage("seq_analyze") == 2
    c.models.generate_content(model="m", contents=["b"])
    c.models.generate_content(model="m", contents=["c"])
    assert ledger.close_stage("chunk_analyze") == 2
    assert ledger.close_stage("story") == 0          # 남은 미태그 없음
    st = ledger.summary()["by_stage"]
    assert st["seq_analyze"] == {"generate": 1, "upload": 1, "errors": 0, "total_tokens": 150}
    assert st["chunk_analyze"]["generate"] == 2 and "story" not in st


def test_one_line_is_human_readable():
    c, ledger, _ = _client()
    c.models.generate_content(model="m", contents=["a"])
    line = ledger.one_line()
    assert "호출 1회" in line and "토큰 150" in line and "실패 0" in line


def test_gemini_client_installs_the_proxy(monkeypatch):
    """GeminiClient 가 실제로 CountingClient 를 끼운다 — 이게 빠지면 원장이 비어 있다."""
    from google import genai
    monkeypatch.setattr(genai, "Client", lambda api_key: SimpleNamespace(
        models=_FakeModels(), files=_FakeFiles()))
    from app.modules.gemini_client import GeminiClient, GeminiConfig
    gc = GeminiClient(GeminiConfig(api_key="k"))
    assert isinstance(gc.client, CountingClient)
    gc.client.models.generate_content(model="m", contents=["x"])
    assert gc.usage.summary()["generate_calls"] == 1


def test_global_registry_merges_every_client():
    """v1 파이프라인은 클라이언트를 분기마다 따로 만든다 — 변수명에 기대지 않고
    프로세스 안의 모든 원장을 합쳐 run_log 에 남긴다."""
    from app.modules import gemini_usage as gu
    gu.reset_registry()
    a, la, _ = _client(); b, lb, _ = _client()
    a.models.generate_content(model="m", contents=["1"])
    b.models.generate_content(model="m", contents=["2"])
    b.files.upload(file="f")
    s = gu.global_summary()
    assert s["generate_calls"] == 2 and s["upload_calls"] == 1 and s["clients"] == 2
    gu.reset_registry()
    assert gu.global_summary()["generate_calls"] == 0
