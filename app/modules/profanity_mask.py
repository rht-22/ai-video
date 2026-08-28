"""E19-7 자막 욕설 마스킹 — 음성은 원음, 자막 텍스트만 가린다.

발주서: `docs/prompts/e19-drama-clip-preset.md` §7. 벤치마크(신병4): 음성은 「새끼야」
그대로 나가고 자막만 「XX끼야」 — 제목의 "X같은" 자체검열과 같은 플랫폼 노출 안전 규율.

- 사전은 코드가 아니라 `app/data/profanity_mask_ko.json` 한 곳(E13 표기 보정과 같은
  규율). 규칙도 같다: **공백 토큰(열) 완전 일치**만 — 부분 문자열 치환은 멀쩡한 말
  (새끼줄·동물 새끼)을 깨뜨린다. 그래서 활용형을 사전에 열거한다.
- 게이트는 파이프라인(design 키 `subtitle_profanity_mask`, 기본 "off") — 이 모듈은
  순수 텍스트 변환만 안다. 적용 대상은 자막(final_segments)뿐이고 전사 원문·TTS 합성
  입력은 호출부가 건드리지 않는다.
- 마스킹 결과는 사전 키와 다시 일치하지 않으므로 **멱등**이다 — 캐시 재개 경로가
  같은 텍스트를 두 번 지나도 안전하다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_MASK_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "profanity_mask_ko.json"
_MASK_CACHE: dict | None = None


def load_mask_rules() -> dict[str, Any]:
    """사전 로드(캐시). 실패는 빈 사전 = 마스킹 없음 — 자막 발행을 막지 않는다."""
    global _MASK_CACHE
    if _MASK_CACHE is not None:
        return _MASK_CACHE
    rules: dict[str, Any] = {"token_map": {}}
    try:
        raw = json.loads(_MASK_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  [자막마스킹] 사전 로드 실패({e}) — 마스킹 없이 진행")
        _MASK_CACHE = rules
        return rules
    token_map = {" ".join(str(k).split()): str(v)
                 for k, v in (raw.get("token_map") or {}).items() if str(k).strip()}
    # 토큰 열이 긴 것부터 — "이런 새끼" 가 "새끼" 보다 먼저 걸려야 한다(E13 과 동일).
    rules["token_map"] = dict(sorted(token_map.items(),
                                     key=lambda kv: -len(kv[0].split(" "))))
    _MASK_CACHE = rules
    return rules


def mask_profanity_text(text: str) -> tuple[str, dict[str, int]]:
    """한 줄 마스킹 → (마스킹된 텍스트, {무엇→무엇: 건수}). 순수 — 테스트 대상."""
    if not text or not text.strip():
        return text, {}
    token_map = load_mask_rules()["token_map"]
    if not token_map:
        return text, {}
    tokens = text.split(" ")
    out_tokens: list[str] = []
    changes: dict[str, int] = {}
    i = 0
    while i < len(tokens):
        matched = False
        for key, val in token_map.items():
            n = len(key.split(" "))
            if n and " ".join(tokens[i:i + n]) == key:
                out_tokens.append(val)
                changes[f"{key}→{val}"] = changes.get(f"{key}→{val}", 0) + 1
                i += n
                matched = True
                break
        if not matched:
            out_tokens.append(tokens[i])
            i += 1
    return " ".join(out_tokens), changes


def apply_mask_to_segments(segments: list) -> tuple[int, list[str]]:
    """자막 줄 목록(start_sec/end_sec/text 속성)에 마스킹 적용 — **text 를 제자리에서
    고친다**(호출부의 subtitle_segments.json 재기록·ASS 조립이 같은 객체를 본다).
    반환: (바뀐 줄 수, 건별 메모 — 조용한 변경 금지)."""
    masked = 0
    details: list[str] = []
    for seg in segments or []:
        new_text, changes = mask_profanity_text(str(getattr(seg, "text", "")))
        if not changes:
            continue
        seg.text = new_text
        masked += 1
        what = " · ".join(f"{k}×{v}" for k, v in changes.items())
        details.append(f"{getattr(seg, 'start_sec', '?')}s: {what}")
    return masked, details
