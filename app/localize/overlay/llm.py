"""LLM 프로바이더 추상화 — gemini | anthropic. translate/metadata/thumbnail 공용.

config.translate.provider 로 백엔드 선택. 키는 LLM_API_KEY(공용) 우선, 프로바이더별
폴백(ANTHROPIC_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY)도 인식. SDK 는 lazy import.
"""
from __future__ import annotations

import os
from typing import Optional

from app.localize.overlay.common import get_secret, get_logger

log = get_logger("llm")


def provider(config: dict) -> str:
    return str(config.get("translate", {}).get("provider", "gemini")).lower()


def resolve_model(config: dict, hero: bool = False) -> Optional[str]:
    """이 레포의 **모델 규칙**을 강제한다 — vlp 와 의도적으로 갈라지는 지점.

    ⚠ vlp config 는 `gemini-3.5-flash`·`gemini-pro-latest` 를 쓰는데 둘 다 이 레포에서
    **사용 금지**다(CLAUDE.md 모델 규칙: Pro 는 `gemini-3.1-pro-preview`, 그 외 전부
    `gemini-3.6-flash`). config 값을 읽지 않고 env 기본값을 따른다 — P1 이 `localize_run`
    Flash 를 바꾼 것과 같은 규약이고, 같은 이유로 여기서만 갈린다.

    hero=True 는 '고품질' 요청이라 Pro 로 보낸다. 다만 이 계층에서 Pro 를 쓰는 것은
    **영상/이미지를 실제로 보는 호출**뿐이어야 한다(모델 정책표) — 텍스트-온리
    트랜스크리에이션은 hero 여도 Flash 가 맞다. 그래서 hero 는 vision 경로에서만 켠다.
    """
    from app.localize.spec import model_flash, model_pro
    return os.environ.get("LLM_MODEL") or (model_pro() if hero else model_flash())



def complete(system: str, user: str, config: dict, *, model: Optional[str] = None,
             max_tokens: int = 1024, hero: bool = False) -> str:
    """system+user 단일 턴 호출 → 응답 텍스트. provider 에 따라 분기."""
    prov = provider(config)
    model = model or resolve_model(config, hero=hero)
    if not model:
        raise ValueError("LLM 모델 미지정. config.translate.model 또는 LLM_MODEL 설정 필요.")
    if prov == "gemini":
        return _gemini(system, user, model, max_tokens)
    if prov == "anthropic":
        return _anthropic(system, user, model, max_tokens)
    raise ValueError(f"알 수 없는 LLM provider: {prov} (gemini|anthropic)")


def complete_vision(system: str, user: str, images: list[tuple[bytes, str]], config: dict,
                    *, model: Optional[str] = None, max_tokens: int = 1024) -> str:
    """이미지+텍스트 멀티모달 단일 턴 → 응답 텍스트. images = [(bytes, mime), …].

    qa_compare(원본-결과 프레임 쌍 비교) 등 비전 검사용. provider 분기는 complete 와 동일.
    """
    prov = provider(config)
    model = model or resolve_model(config)
    if not model:
        raise ValueError("LLM 모델 미지정. config.translate.model 또는 LLM_MODEL 설정 필요.")
    if prov == "gemini":
        return _gemini_vision(system, user, images, model, max_tokens)
    if prov == "anthropic":
        return _anthropic_vision(system, user, images, model, max_tokens)
    raise ValueError(f"알 수 없는 LLM provider: {prov} (gemini|anthropic)")


def _gemini(system: str, user: str, model: str, max_tokens: int) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise ImportError("google-genai 필요: pip install google-genai") from e
    client = genai.Client(api_key=get_secret("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                                             required=True))
    cfg_kwargs = {"system_instruction": system, "max_output_tokens": max_tokens}
    if "flash" in model:    # flash 2.5: thinking 비활성화(출력 토큰 절약·잘림 방지)
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    resp = client.models.generate_content(
        model=model, contents=user, config=types.GenerateContentConfig(**cfg_kwargs))
    return resp.text or ""


def _gemini_vision(system: str, user: str, images: list[tuple[bytes, str]],
                   model: str, max_tokens: int) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise ImportError("google-genai 필요: pip install google-genai") from e
    client = genai.Client(api_key=get_secret("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                                             required=True))
    parts = [types.Part.from_bytes(data=b, mime_type=m) for b, m in images]
    cfg_kwargs = {"system_instruction": system, "max_output_tokens": max_tokens}
    if "flash" in model:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    resp = client.models.generate_content(
        model=model, contents=[*parts, user], config=types.GenerateContentConfig(**cfg_kwargs))
    return resp.text or ""


def _anthropic_vision(system: str, user: str, images: list[tuple[bytes, str]],
                      model: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise ImportError("anthropic 필요: pip install anthropic") from e
    import base64

    client = anthropic.Anthropic(api_key=get_secret("LLM_API_KEY", "ANTHROPIC_API_KEY",
                                                    required=True))
    content: list[dict] = [
        {"type": "image", "source": {"type": "base64", "media_type": m,
                                     "data": base64.b64encode(b).decode()}}
        for b, m in images
    ]
    content.append({"type": "text", "text": user})
    resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise ImportError("anthropic 필요: pip install anthropic") from e
    client = anthropic.Anthropic(api_key=get_secret("LLM_API_KEY", "ANTHROPIC_API_KEY",
                                                    required=True))
    resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
