"""Gemini 얇은 래퍼 — 텍스트/영상 JSON 호출 · Files API 업로드 · 재시도 · 사용량 기록.

모델 규칙(레포 CLAUDE.md): 영상을 실제로 보는 호출 = `GEMINI_MODEL_NAME`, 그 외 = `GEMINI_FLASH_MODEL_NAME`
(기본 둘 다 gemini-3.7-flash). 샘플링 파라미터는 건드리지 않고 thinking_level 만 쓴다
(`minimal` 은 3.7-flash 가 거부한다 — 실측 메모).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.model_policy import (DEFAULT_FLASH_MODEL, pro_model_name, flash_model_name,
                              gemini_request_timeout_sec)

DEFAULT_MODEL = DEFAULT_FLASH_MODEL
ALLOWED_MODELS = {DEFAULT_MODEL}


def resolve_model(env_value: str | None, *, log=print) -> str:
    """env 가 금지 모델(예: 오래된 .env 의 gemini-3.5-flash)을 가리키면 기본 모델로 바꾸고 **소리 내어** 남긴다."""
    v = (env_value or "").strip()
    if not v:
        return DEFAULT_MODEL
    if v in ALLOWED_MODELS:
        return v
    log(f"  [llm] ⚠ env 모델 {v!r} 은 이 레포의 허용 목록 밖 — {DEFAULT_MODEL} 로 대체")
    return DEFAULT_MODEL


UPLOAD_RETRIES = 5      # 2026-09-11 실측: 6창 동시 업로드에서 FAILED 가 3건 — 2회로는 두 창이 통째로 빠졌다
INLINE_MAX_BYTES = 18 * 1024 * 1024   # generateContent 인라인 요청 상한(20MB) 안쪽 — 10분 360p 창 클립 ≈ 9~12MB

_TRANSIENT = ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE", "timed out", "Timeout",
              # 2026-09-17 로또 clip01 실측: 서버가 응답 없이 연결을 끊는 경우(httpx RemoteProtocolError)와
              # 요청 타임아웃(httpx ReadTimeout — str() 이 비어 있을 수 있어 예외 **타입명**도 함께 본다)
              "Server disconnected", "RemoteProtocolError")


def _err_text(e: BaseException) -> str:
    """재시도 판정용 문자열 — 타입명 + 메시지(httpx.ReadTimeout 은 메시지가 빈 경우가 있다)."""
    return f"{type(e).__name__}: {e}"


def extract_json(text: str, *, repair: bool = False) -> Any:
    """코드펜스 제거 → json.loads. 앞 값만 완결돼 있으면 그것만 취한다(레포 실측 `{...}{...}` 사고).
    repair=True 면 잘린 JSON(출력 상한·런어웨이)을 마지막 완결 객체까지 잘라 괄호를 닫아 살린다."""
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    try:
        obj, _end = json.JSONDecoder().raw_decode(t)
        return obj
    except json.JSONDecodeError:
        pass
    # 숫자 자리의 분수 표기(`"x": 606/1000`) — 2026-09-11 framing 실측. 값 위치(콜론·쉼표·괄호 뒤)에서만 바꾼다
    t2 = re.sub(r'(?<=[:\[,\s])(\d+)/(\d+)(?=\s*[,\]}])', lambda m: repr(int(m.group(1)) / int(m.group(2))) if int(m.group(2)) else m.group(0), t)
    if t2 != t:
        try:
            return json.loads(t2)
        except json.JSONDecodeError:
            pass
    if not repair:
        raise json.JSONDecodeError("JSON 아님", t, 0)
    return json.loads(repair_json(t))


def repair_json(text: str) -> str:
    """잘린 JSON 복구 — 마지막으로 **완결된 객체**(`}`)까지 자르고, 문자열 밖의 열린 괄호를 역순으로 닫는다.
    2026-09-09 실측: agentic 응답이 출력 상한에서 중간 문자열 안에서 끊겼다(`"who": "음대교수, desc: …`).
    손상 부분(마지막 미완 객체)은 버린다 — 반쯤 적힌 컷은 편집 테이블에 올리지 않는다(삼위일체)."""
    cut = text.rfind("}")
    if cut < 0:
        raise json.JSONDecodeError("복구 불가: 완결된 객체가 없다", text, 0)
    t = text[:cut + 1]
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    # 잘린 자리 뒤에 남은 `,` 정리
    t = t.rstrip()
    while t.endswith(","):
        t = t[:-1].rstrip()
    closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
    return t + closers


@dataclass
class Usage:
    calls: list[dict] = field(default_factory=list)

    def add(self, kind: str, model: str, resp: Any, elapsed: float) -> dict:
        um = getattr(resp, "usage_metadata", None)
        rec = {"kind": kind, "model": model, "sec": round(elapsed, 1),
               "prompt_tokens": getattr(um, "prompt_token_count", None),
               "output_tokens": getattr(um, "candidates_token_count", None),
               "thought_tokens": getattr(um, "thoughts_token_count", None),
               "finish": _finish(resp)}
        self.calls.append(rec)
        return rec


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    for attr in ("model_dump", "to_dict", "dict"):
        f = getattr(obj, attr, None)
        if callable(f):
            try:
                return f()
            except Exception:  # noqa: BLE001
                pass
    return str(obj)


def _finish(resp: Any) -> str:
    try:
        r = resp.candidates[0].finish_reason
        return str(getattr(r, "name", r))
    except Exception:
        return "unknown"


class Gemini:
    def __init__(self, api_key: str | None = None, *, log=print):
        from google import genai
        from google.genai import types
        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY 가 없다 (.env 또는 환경변수)")
        # 요청 타임아웃(2026-09-17): SDK 기본은 무한이라 서버가 응답 없이 소켓만 잡고
        # 있으면 파이프라인이 영영 멈춘다(로또 clip01 실측 — 56분·14분·15분 행). 시간이
        # 지나면 httpx ReadTimeout 이 올라오고 아래 _TRANSIENT 가 재시도한다.
        self.client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=int(gemini_request_timeout_sec() * 1000)))
        self.types = types
        self.video_model = pro_model_name()
        self.text_model = flash_model_name()
        self.max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
        self.usage = Usage()
        self.log = log

    # ── 텍스트 → JSON ──────────────────────────────────────────────────────
    def text_json(self, prompt: str, *, kind: str, thinking: str = "medium", max_output_tokens: int = 65536) -> Any:
        types = self.types
        cfg = types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=max_output_tokens,
                                          thinking_config=types.ThinkingConfig(thinking_level=thinking))
        return self._call(kind, self.text_model, [prompt], cfg)

    # ── 영상 + 텍스트 → JSON ───────────────────────────────────────────────
    def video_json(self, prompt: str, video_path: Path, *, kind: str, fps: float | None = 1.0,
                   thinking: str = "low", low_res: bool = True, max_output_tokens: int = 65536) -> Any:
        types = self.types
        # These bounded review clips are tiny. Avoid a separate Files API
        # processing job (and its repeated 500 failures) for each 1–2s clip.
        if kind in {"grid_cover_probe", "opening_review"} and video_path.stat().st_size <= INLINE_MAX_BYTES:
            part = types.Part(inline_data=types.Blob(data=video_path.read_bytes(), mime_type="video/mp4"),
                              video_metadata=types.VideoMetadata(fps=fps) if fps else None)
            kw = dict(response_mime_type="application/json", max_output_tokens=max_output_tokens,
                      thinking_config=types.ThinkingConfig(thinking_level=thinking))
            if low_res:
                kw["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
            return self._call(kind, self.video_model, [part, prompt], types.GenerateContentConfig(**kw))
        try:
            uploaded = self._upload(video_path)
        except Exception as e:  # noqa: BLE001
            # Files API 가 FAILED/500 을 연달아 내는 날이 있다(2026-09-11 실측: 6창 중 4창). 20MB 미만이면 인라인 바이트로 우회한다.
            size = video_path.stat().st_size
            if size > INLINE_MAX_BYTES:
                raise
            self.log(f"  [llm/{kind}] Files API 실패 → 인라인 바이트로 우회({size/1e6:.1f}MB): {str(e)[:80]}")
            part = types.Part(inline_data=types.Blob(data=video_path.read_bytes(), mime_type="video/mp4"),
                              video_metadata=types.VideoMetadata(fps=fps) if fps else None)
            kw: dict[str, Any] = dict(response_mime_type="application/json", max_output_tokens=max_output_tokens,
                                      thinking_config=types.ThinkingConfig(thinking_level=thinking))
            if low_res:
                kw["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
            return self._call(kind, self.video_model, [part, prompt], types.GenerateContentConfig(**kw))
        try:
            part = types.Part(file_data=types.FileData(file_uri=uploaded.uri, mime_type="video/mp4"),
                              video_metadata=types.VideoMetadata(fps=fps) if fps else None)
            kw: dict[str, Any] = dict(response_mime_type="application/json", max_output_tokens=max_output_tokens,
                                      thinking_config=types.ThinkingConfig(thinking_level=thinking))
            if low_res:
                kw["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
            cfg = types.GenerateContentConfig(**kw)
            return self._call(kind, self.video_model, [part, prompt], cfg)
        finally:
            try:
                self.client.files.delete(name=uploaded.name)
            except Exception:
                pass

    # ── 이미지 여러 장(인라인 바이트) + 텍스트 → JSON ───────────────────────
    def images_json(self, prompt: str, image_paths: list[Path], *, kind: str, thinking: str = "low",
                    max_output_tokens: int = 4096) -> Any:
        """작은 JPEG 몇 장은 Files API 없이 인라인으로 보낸다(컷당 3장 · 480p ≈ 1k 토큰) — 주인물 크롭 판정용."""
        types = self.types
        parts = [types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg") for p in image_paths]
        cfg = types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=max_output_tokens,
                                          thinking_config=types.ThinkingConfig(thinking_level=thinking))
        return self._call(kind, self.video_model, parts + [prompt], cfg)

    # ── 임의 미디어(오디오 등) + 텍스트 → JSON ──────────────────────────────
    def media_json(self, prompt: str, media_path: Path, *, mime: str, kind: str, thinking: str = "low",
                   max_output_tokens: int = 65536) -> Any:
        """오디오 파일(예: audio/mp4) 을 Files API 로 올려 텍스트와 함께 보낸다 — 전사 교정 패스용(영상 없이 소리만)."""
        types = self.types
        uploaded = self._upload(media_path)
        try:
            part = types.Part(file_data=types.FileData(file_uri=uploaded.uri, mime_type=mime))
            cfg = types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=max_output_tokens,
                                              thinking_config=types.ThinkingConfig(thinking_level=thinking))
            return self._call(kind, self.video_model, [part, prompt], cfg)
        finally:
            try:
                self.client.files.delete(name=uploaded.name)
            except Exception:
                pass

    # ── Agentic video (Interactions API · processing="agentic") ─────────────
    def agentic_video_json(self, prompt: str, video_path: Path, *, kind: str, schema: dict | None = None,
                           upload_cache: Path | None = None, max_output_tokens: int | None = None) -> tuple[Any, dict]:
        """모델이 영상 타임라인을 스스로 검색·확대해 본다(2026-09-01 출시). Interactions API 전용 —
        fps/media_resolution/클리핑과 양립 불가라 그 옵션을 넣지 않는다. → (JSON, 메타{processing_calls, tokens…})

        ⚠ JSON 은 `response_format={"type":"text","mime_type":"application/json","schema":…}` 로 요청한다 —
        `response_mime_type` 만 주면 400 `responseFormat must be set`(2026-09-09 실측).
        upload_cache 를 주면 Files API 업로드(42MB 프록시 ≈ 2분)를 잡 안에서 재사용한다(48h 보존)."""
        uploaded, cached = self._upload_cached(video_path, upload_cache) if upload_cache else (self._upload(video_path), False)
        # ⚠ JSON 스키마 제약(`mime_type`+`schema`)은 쓰지 않는다 — 2026-09-09 실측 3회 연속 첫 문자열 필드에서 같은 구절을
        # 무한 반복하는 런어웨이(216KB·39KB)가 났고, 스키마 maxLength 도 강제되지 않았다. 일반 텍스트로 받아 프롬프트가
        # 요구한 JSON 을 파싱한다(정적 호출과 같은 경로). `schema` 인자는 호환용으로 남기되 무시한다.
        response_format: dict[str, Any] = {"type": "text"}
        try:
            last: Exception | None = None
            # generation_config 후보를 강한 것부터 — 거절되면 다음 후보로(마지막은 없음).
            # ⚠ max_output_tokens 는 thought 와 **나눠 쓴다**(실측: thought 3,933 + 출력 156 ≈ 4,096 에서 절단) —
            # 상한을 낮게 잡으면 thinking 이 다 먹는다. 그래서 thinking 도 같이 낮춘다.
            gc_candidates: list[dict[str, Any]] = []
            if max_output_tokens:
                gc_candidates.append({"max_output_tokens": int(max_output_tokens), "thinking_config": {"thinking_level": "low"}})
                gc_candidates.append({"max_output_tokens": int(max_output_tokens)})
            gc_candidates.append({})
            gci = 0
            extra: dict[str, Any] = ({"generation_config": gc_candidates[0]} if gc_candidates[0] else {})
            for attempt in range(self.max_retries):
                t0 = time.time()
                try:
                    inter = self.client.interactions.create(
                        model=self.video_model,
                        input=[{"type": "video", "uri": uploaded.uri, "mime_type": "video/mp4", "processing": "agentic"},
                               {"type": "text", "text": prompt}],
                        response_format=response_format, **extra)
                except Exception as e:  # noqa: BLE001
                    last = e
                    msg = _err_text(e)
                    if extra and ("400" in msg or "invalid" in msg.lower() or "Unknown name" in msg) and gci < len(gc_candidates) - 1:
                        gci += 1
                        extra = {"generation_config": gc_candidates[gci]} if gc_candidates[gci] else {}
                        self.log(f"  [llm/{kind}] generation_config 거절 → 다음 후보 {gc_candidates[gci] or '없음'}: {msg[:120]}")
                        continue
                    if any(k in msg for k in _TRANSIENT) and attempt < self.max_retries - 1:
                        time.sleep(5 * (attempt + 1))
                        continue
                    raise
                steps = list(getattr(inter, "steps", None) or [])
                kinds: dict[str, int] = {}
                for s in steps:
                    k = str(getattr(s, "type", None) or (s.get("type") if isinstance(s, dict) else "?"))
                    kinds[k] = kinds.get(k, 0) + 1
                usage = getattr(inter, "usage", None)
                meta = {"sec": round(time.time() - t0, 1), "status": str(getattr(inter, "status", "")),
                        "processing_calls": kinds.get("processing_call", 0), "step_kinds": kinds,
                        "usage": _to_dict(usage), "total_tokens": getattr(usage, "total_tokens", None), "model": self.video_model}
                self.usage.calls.append({"kind": kind, "model": self.video_model, "sec": meta["sec"], "agentic": meta})
                self.log(f"  [llm/{kind}] agentic {self.video_model} steps={kinds} usage={meta['usage']} {meta['sec']}s")
                text = getattr(inter, "output_text", None) or ""
                if not text:
                    for s in reversed(steps):
                        if str(getattr(s, "type", "")) == "text" and getattr(s, "text", None):
                            text = s.text
                            break
                try:
                    return extract_json(text), meta
                except Exception as e:  # noqa: BLE001
                    self._dump(kind, text)
                    try:
                        obj = extract_json(text, repair=True)
                        meta["repaired"] = True
                        self.log(f"  [llm/{kind}] ⚠ agentic JSON 이 잘려 있어 마지막 완결 객체까지 복구해 썼다({len(text)}자)")
                        return obj, meta
                    except Exception as e2:  # noqa: BLE001
                        last = ValueError(f"JSON 파싱 실패: {e} / 복구 실패: {e2}")
                    self.log(f"  [llm/{kind}] ⚠ agentic JSON 파싱 실패 — 재시도 ({attempt+1}/{self.max_retries})")
            raise RuntimeError(f"[llm/{kind}] agentic {self.max_retries}회 실패: {last}")
        finally:
            if not cached:
                try:
                    self.client.files.delete(name=uploaded.name)
                except Exception:
                    pass

    def _upload_cached(self, video_path: Path, cache_file: Path):
        """잡 디렉토리의 업로드 캐시(경로·크기·mtime 키) → 살아 있으면 재사용, 아니면 새로 올리고 기록. → (file, cached)"""
        key = f"{video_path.resolve()}|{video_path.stat().st_size}|{int(video_path.stat().st_mtime)}"
        cache: dict[str, Any] = {}
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cache = {}
        rec = cache.get(key)
        if rec:
            try:
                f = self.client.files.get(name=rec["name"])
                if f.state.name == "ACTIVE":
                    self.log(f"  [llm/upload] 캐시 재사용 {rec['name']}")
                    return f, True
            except Exception:  # noqa: BLE001
                pass
        f = self._upload(video_path)
        cache[key] = {"name": f.name, "uri": f.uri, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return f, True

    # ── 내부 ──────────────────────────────────────────────────────────────
    def _call(self, kind: str, model: str, contents: list, cfg) -> Any:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            t0 = time.time()
            try:
                resp = self.client.models.generate_content(model=model, contents=contents, config=cfg)
            except Exception as e:  # noqa: BLE001
                last = e
                msg = _err_text(e)
                if any(k in msg for k in _TRANSIENT) and attempt < self.max_retries - 1:
                    wait = 5 * (attempt + 1)
                    self.log(f"  [llm/{kind}] 일시 오류 — {wait}s 후 재시도 ({attempt+1}/{self.max_retries}): {msg[:120]}")
                    time.sleep(wait)
                    continue
                raise
            rec = self.usage.add(kind, model, resp, time.time() - t0)
            self.log(f"  [llm/{kind}] {model} in={rec['prompt_tokens']} out={rec['output_tokens']} "
                     f"think={rec['thought_tokens']} finish={rec['finish']} {rec['sec']}s")
            if rec["finish"] == "MAX_TOKENS":
                last = RuntimeError("출력이 max_output_tokens 에서 잘렸다")
                self.log(f"  [llm/{kind}] ⚠ 출력 절단 — 재시도 ({attempt+1}/{self.max_retries})")
                continue
            try:
                return extract_json(resp.text or "")
            except Exception as e:  # noqa: BLE001
                last = ValueError(f"JSON 파싱 실패: {e}")
                self.log(f"  [llm/{kind}] ⚠ JSON 파싱 실패 — 재시도 ({attempt+1}/{self.max_retries})")
                self._dump(kind, resp.text or "")
        raise RuntimeError(f"[llm/{kind}] {self.max_retries}회 실패: {last}")

    def _dump(self, kind: str, text: str) -> None:
        try:
            d = Path(tempfile.gettempdir()) / "tikitaka_llm_failures"
            d.mkdir(exist_ok=True)
            (d / f"{time.strftime('%Y%m%d_%H%M%S')}_{kind}.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass

    def _upload(self, video_path: Path):
        """Files API 업로드(ASCII 임시 이름) → ACTIVE 대기."""
        tmp_dir = Path(tempfile.mkdtemp(prefix="tk_up_"))
        safe = tmp_dir / f"clip_{int(time.time()*1000)}{video_path.suffix or '.mp4'}"
        try:
            os.link(video_path, safe)
        except OSError:
            shutil.copy2(video_path, safe)
        last: Exception | None = None
        try:
            for attempt in range(UPLOAD_RETRIES):          # 업로드는 GEMINI_MAX_RETRIES(2)와 별개 — FAILED 는 서버 쪽 일시 오류가 잦다
                try:
                    up = self.client.files.upload(file=str(safe))
                    while up.state.name == "PROCESSING":
                        time.sleep(2)
                        up = self.client.files.get(name=up.name)
                    if up.state.name == "FAILED":
                        try:
                            self.client.files.delete(name=up.name)
                        except Exception:
                            pass
                        raise RuntimeError("Files API 처리 실패(FAILED)")
                    return up
                except Exception as e:  # noqa: BLE001
                    last = e
                    wait = 3 * (2 ** attempt)
                    self.log(f"  [llm/upload] 실패 — {wait}s 후 재시도 ({attempt+1}/{UPLOAD_RETRIES}): {str(e)[:120]}")
                    time.sleep(wait)
            raise RuntimeError(f"업로드 {UPLOAD_RETRIES}회 실패: {last}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
