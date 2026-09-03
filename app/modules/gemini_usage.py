"""Gemini 호출 원장(ledger) — 실행마다 호출 수·토큰을 센다.

계기(2026-09-03, 사용자 지적): 반려 루프가 실제로 몇 번 돌았고 Gemini 를 몇 번 불렀는지
run_log 어디에도 합계가 없었다. 단계별 attempt 만 흩어져 있고 토큰은 0건. 호출부가
12개 파일 24군데(`generate_content(`)라 세는 지점이 없던 것이 원인 — 그래서 클라이언트
**한 곳**에서 가로챈다.

방식: genai.Client 를 감싸는 프록시(CountingClient). SDK 객체를 고치지 않고(버전 종속
회피) `.models.generate_content` 와 `.files.upload` 만 세고 나머지 속성은 그대로 넘긴다.
GeminiClient 를 거치는 호출은 전부 잡힌다 — v1·v3·seq_analyze·refine·chunk_analyze·
story·watch_trim·style·프로브. 자기 client 를 따로 만드는 localize/telop·overlay/llm 은
범위 밖(그쪽은 vlp 규약).

기록: run_log["gemini_usage"] = {generate_calls, upload_calls, errors, elapsed_sec,
tokens{prompt,thoughts,output,total}, by_model{…}, by_stage{…}}.
단계 태그는 파이프라인의 step() 이 닫을 때 붙는다 — 그 step 직전까지의 미태그 호출이
그 단계의 것이다(step 은 단계가 **끝난 뒤** 불리므로).

실패한 호출도 센다 — 재시도는 요금이 나간다. 예외는 기록만 하고 그대로 다시 던진다.
"""
from __future__ import annotations

import time
from typing import Any

_TOKEN_KEYS = (("prompt", "prompt_token_count"), ("thoughts", "thoughts_token_count"),
               ("output", "candidates_token_count"), ("total", "total_token_count"))


def _tokens_of(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    out: dict[str, int] = {}
    if usage is None:
        return out
    for name, attr in _TOKEN_KEYS:
        v = getattr(usage, attr, None)
        if isinstance(v, int):
            out[name] = v
    return out


# 프로세스 전역 레지스트리 — v1 파이프라인은 클라이언트를 분기마다 따로 만들어
# 변수명으로 못 찾는다. 만들어진 모든 원장을 여기 모아 global_summary() 로 합친다.
_REGISTRY: list["GeminiUsage"] = []


def reset_registry() -> None:
    _REGISTRY.clear()


def global_summary() -> dict[str, Any]:
    merged = GeminiUsage(register=False)
    for u in _REGISTRY:
        merged.calls.extend(u.calls)
    out = merged.summary()
    out["clients"] = len(_REGISTRY)
    return out


class GeminiUsage:
    """호출 원장. 순수 자료구조 — 파이프라인이 단계 경계에서 close_stage() 로 태그한다."""

    def __init__(self, *, register: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        if register:
            _REGISTRY.append(self)

    # ── 기록 ────────────────────────────────────────────────────────────
    def record(self, *, kind: str, model: str | None, elapsed: float,
               tokens: dict[str, int] | None = None, ok: bool = True,
               error: str | None = None) -> None:
        self.calls.append({"kind": kind, "model": model or "?", "elapsed": round(elapsed, 3),
                           "tokens": dict(tokens or {}), "ok": ok, "error": error,
                           "stage": None})

    def close_stage(self, name: str) -> int:
        """아직 단계가 안 붙은 호출 전부에 name 을 붙인다. 반환: 붙인 건수."""
        n = 0
        for c in self.calls:
            if c["stage"] is None:
                c["stage"] = name
                n += 1
        return n

    # ── 집계 ────────────────────────────────────────────────────────────
    def summary(self) -> dict[str, Any]:
        gen = [c for c in self.calls if c["kind"] == "generate"]
        up = [c for c in self.calls if c["kind"] == "upload"]
        tok = {k: 0 for k, _ in _TOKEN_KEYS}
        by_model: dict[str, dict[str, int]] = {}
        by_stage: dict[str, dict[str, int]] = {}
        for c in gen:
            for k, v in c["tokens"].items():
                tok[k] += v
            m = by_model.setdefault(c["model"], {"calls": 0, "errors": 0, "total_tokens": 0})
            m["calls"] += 1
            m["errors"] += 0 if c["ok"] else 1
            m["total_tokens"] += c["tokens"].get("total", 0)
        for c in self.calls:
            s = by_stage.setdefault(c["stage"] or "(unassigned)",
                                    {"generate": 0, "upload": 0, "errors": 0, "total_tokens": 0})
            s[c["kind"]] = s.get(c["kind"], 0) + 1
            s["errors"] += 0 if c["ok"] else 1
            s["total_tokens"] += c["tokens"].get("total", 0)
        return {
            "generate_calls": len(gen),
            "upload_calls": len(up),
            "errors": sum(1 for c in self.calls if not c["ok"]),
            "elapsed_sec": round(sum(c["elapsed"] for c in self.calls), 1),
            "tokens": tok,
            "by_model": by_model,
            "by_stage": by_stage,
        }

    def one_line(self) -> str:
        s = self.summary()
        t = s["tokens"]
        return (f"Gemini 호출 {s['generate_calls']}회 (업로드 {s['upload_calls']}, "
                f"실패 {s['errors']}) · 토큰 {t['total']:,} "
                f"(프롬프트 {t['prompt']:,} · 추론 {t['thoughts']:,} · 출력 {t['output']:,}) "
                f"· {s['elapsed_sec']:.0f}s")


# ── 프록시 ──────────────────────────────────────────────────────────────

class _ModelsProxy:
    def __init__(self, real: Any, ledger: GeminiUsage) -> None:
        self._real = real
        self._ledger = ledger

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        if model is None and args:
            model = args[0]
        t0 = time.perf_counter()
        try:
            resp = self._real.generate_content(*args, **kwargs)
        except BaseException as e:   # noqa: BLE001 — 기록만 하고 그대로 던진다
            self._ledger.record(kind="generate", model=str(model) if model else None,
                                elapsed=time.perf_counter() - t0, ok=False,
                                error=type(e).__name__)
            raise
        self._ledger.record(kind="generate", model=str(model) if model else None,
                            elapsed=time.perf_counter() - t0, tokens=_tokens_of(resp))
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _FilesProxy:
    def __init__(self, real: Any, ledger: GeminiUsage) -> None:
        self._real = real
        self._ledger = ledger

    def upload(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            out = self._real.upload(*args, **kwargs)
        except BaseException as e:   # noqa: BLE001
            self._ledger.record(kind="upload", model=None, elapsed=time.perf_counter() - t0,
                                ok=False, error=type(e).__name__)
            raise
        self._ledger.record(kind="upload", model=None, elapsed=time.perf_counter() - t0)
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class CountingClient:
    """genai.Client 대역 — `.models`·`.files` 만 세는 프록시로 바꾸고 나머지는 위임."""

    def __init__(self, real: Any, ledger: GeminiUsage) -> None:
        self._real = real
        self._ledger = ledger

    @property
    def models(self) -> _ModelsProxy:
        return _ModelsProxy(self._real.models, self._ledger)

    @property
    def files(self) -> _FilesProxy:
        return _FilesProxy(self._real.files, self._ledger)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


__all__ = ["GeminiUsage", "CountingClient", "global_summary", "reset_registry"]
