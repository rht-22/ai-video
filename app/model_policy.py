"""Gemini 모델 기본값의 **단일 정본** (CLAUDE.md 「Gemini 모델 사용 규칙」).

2026-09-08: 현지화 계층(`app/localize/spec.py`)과 provenance 기록이 옛 기본값
(`gemini-3.1-pro-preview` / `gemini-3.6-flash`)을 따로 들고 있었다 — env 가 없는 노드에서는
현지화가 금지 모델을 부르고, run_log `provenance.models` 가 실제 호출(3.7-flash)과 다른
이름을 남겼다. 기본값을 세 곳에 베껴 두면 규칙이 바뀔 때 한 곳만 고쳐진다(E13 교훈).

- 의존이 없다(표준 라이브러리만) — overlay 계층은 생성 스택(gemini_client)을 끌어오지
  않는 것이 계약이라, 기본값을 gemini_client 에서 import 할 수 없어 따로 뗐다.
- 슬롯(Pro/Flash) 구분은 유지한다 — Pro 복귀 시 `GEMINI_MODEL_NAME` 만 바꾸면 된다.
- 배포 노드 env 가 있으면 그쪽이 이긴다. 이 값은 **env 부재 시**의 기본값이다.
"""
from __future__ import annotations

import os

DEFAULT_PRO_MODEL = "gemini-3.7-flash"    # 영상을 실제로 보는 호출 (Pro 슬롯)
DEFAULT_FLASH_MODEL = "gemini-3.7-flash"  # 그 외 전부 (Flash 슬롯)


def pro_model_name() -> str:
    return os.getenv("GEMINI_MODEL_NAME", DEFAULT_PRO_MODEL)


def flash_model_name() -> str:
    return os.getenv("GEMINI_FLASH_MODEL_NAME", DEFAULT_FLASH_MODEL)
