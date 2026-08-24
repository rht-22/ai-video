"""테스트 공통 픽스처.

E17(2026-08-24): `tts` 의 ElevenLabs 만료 판정은 **프로세스 전역**이다(한 번 거절당하면
남은 cue 는 전부 기본 백엔드로 간다 — 한 편 안에서 목소리가 갈리지 않게). 그런데 pytest 는
전 테스트를 한 프로세스에서 돌리므로, 401 을 흉내 내는 테스트 하나가 뒤따르는 모든
합성 테스트를 조용히 폴백 경로로 밀어 버린다. 매 테스트 앞에서 되돌린다.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_elevenlabs_state():
    from app.modules import tts

    tts.reset_elevenlabs_state()
    yield
    tts.reset_elevenlabs_state()
