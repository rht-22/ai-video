"""E11 — KR 내레이션 TTS ElevenLabs 전환 (네트워크 불필요).

계약:
· voice/speed **라벨은 E11 이전과 동일** — 편집실(edVoiceSel·edSpeedSel)·
  edit_overrides/v2·체크포인트 cue 에 이미 실린 값이라 라벨 변경은 하위호환 파괴.
  EL_* 매핑이 라벨 전수를 커버해야 한다.
· speed 는 ElevenLabs voice_settings.speed 허용 범위(0.7~1.2, 1.0=기본) 안에서
  라벨 순서대로 단조.
· 백엔드 선택: ELEVENLABS_API_KEY 있으면 elevenlabs, 없으면 edge-tts(stdout 명시
  폴백), 그마저 없으면 silence(개발 폴백).
· API 실패는 429·5xx·네트워크만 재시도, 그 외 4xx 는 즉시 실패 — edge-tts 로
  조용히 넘어가지 않는다(같은 채널 목소리가 편마다 달라진다, fail-loud).
· edge-tts 경로의 '예외 → rate/pitch 빼고 재시도' 무성 폴백 제거 — 속도·피치가
  소리 없이 무시되던 유일한 지점(8/21 '속도가 안 먹힌다' 발주의 직접 원인 후보).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules import tts


REPO = Path(__file__).resolve().parent.parent


# ── 매핑 계약 ─────────────────────────────────────────────────────────────

def test_el_voice_map_covers_all_labels():
    assert set(tts.EL_VOICE_PRESETS) == set(tts.VOICE_PRESETS)
    # premade voice_id 형식(영숫자 20자) — 오타·빈 값 방어
    for label, vid in tts.EL_VOICE_PRESETS.items():
        assert vid and vid.isalnum() and len(vid) == 20, (label, vid)


def test_el_speed_map_covers_all_labels_within_documented_range():
    assert set(tts.EL_SPEED) == set(tts.SPEED_TO_RATE)
    for label, v in tts.EL_SPEED.items():
        assert 0.7 <= v <= 1.2, (label, v)   # 문서 허용 범위 — 밖이면 API 422
    order = ["very_slow", "slow", "normal", "fast", "very_fast"]
    vals = [tts.EL_SPEED[k] for k in order]
    assert vals == sorted(vals) and len(set(vals)) == len(vals), vals   # 단조 증가
    assert tts.EL_SPEED["normal"] == 1.0


# ── 백엔드 선택 ───────────────────────────────────────────────────────────

def test_backend_selection(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert tts.active_backend() == "elevenlabs"
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    monkeypatch.setattr(tts, "_has_edge_tts", lambda: True)
    assert tts.active_backend() == "edge-tts"
    monkeypatch.setattr(tts, "_has_edge_tts", lambda: False)
    assert tts.active_backend() == "silence"


# ── ElevenLabs 호출 계약 (requests 모킹 — 네트워크 없음) ─────────────────

def _mock_post(monkeypatch, responses):
    """responses: 호출마다 돌려줄 (status_code, content) 목록 또는 예외."""
    import requests

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        status, content = r
        return SimpleNamespace(status_code=status, content=content, text="err")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)
    return calls


def test_elevenlabs_success_writes_mp3_and_sends_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    calls = _mock_post(monkeypatch, [(200, b"ID3-fake-mp3")])
    out = tmp_path / "cue.mp3"
    got = tts._synthesize_elevenlabs("안녕하세요", out, voice="ko_male_low", speed="fast")
    assert got == out and out.read_bytes() == b"ID3-fake-mp3"
    assert len(calls) == 1
    c = calls[0]
    assert tts.EL_VOICE_PRESETS["ko_male_low"] in c["url"]
    assert "output_format=mp3_44100_128" in c["url"]
    assert c["headers"]["xi-api-key"] == "test-key"
    assert c["json"]["model_id"] == tts.EL_MODEL_ID
    assert c["json"]["voice_settings"]["speed"] == tts.EL_SPEED["fast"]


def test_legacy_voice_label_maps_to_same_voice_as_before(monkeypatch, tmp_path):
    """구 체크포인트의 레거시 라벨(narrative_female)은 **오늘과 같은 목소리**로.

    E13 에서 모르는 라벨을 fail-loud 로 바꿨지만, 이 값은 pipeline 이 voice 없는 cue 에
    넣는 기본 문자열이라 지금도 실려 온다 — 재개 산출이 달라지면 안 된다(회귀 0).
    """
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    calls = _mock_post(monkeypatch, [(200, b"x")])
    tts._synthesize_elevenlabs("t", tmp_path / "o.mp3",
                               voice="narrative_female", speed="normal")
    assert tts.EL_VOICE_PRESETS[tts.DEFAULT_VOICE] in calls[0]["url"]
    assert calls[0]["json"]["voice_settings"]["speed"] == 1.0


def test_elevenlabs_4xx_fails_immediately(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "bad")
    calls = _mock_post(monkeypatch, [(401, b"")])
    with pytest.raises(RuntimeError, match="ElevenLabs"):
        tts._synthesize_elevenlabs("t", tmp_path / "o.mp3")
    assert len(calls) == 1          # 키·요청 오류는 재시도해도 안 낫는다 — 즉시 실패


def test_elevenlabs_429_and_5xx_retry_then_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    calls = _mock_post(monkeypatch, [(429, b""), (503, b""), (503, b"")])
    with pytest.raises(RuntimeError, match="합성 실패"):
        tts._synthesize_elevenlabs("t", tmp_path / "o.mp3")
    assert len(calls) == tts._EL_RETRIES + 1


def test_elevenlabs_retry_recovers(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    calls = _mock_post(monkeypatch, [(503, b""), (200, b"ok")])
    out = tmp_path / "o.mp3"
    tts._synthesize_elevenlabs("t", out)
    assert out.read_bytes() == b"ok" and len(calls) == 2


def test_synthesize_dispatches_to_elevenlabs(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    seen = {}

    def fake_el(text, output_path, voice, speed):
        seen.update(text=text, voice=voice, speed=speed)
        return output_path

    monkeypatch.setattr(tts, "_synthesize_elevenlabs", fake_el)
    tts.synthesize_tts("본문", tmp_path / "o.mp3", voice="ko_female_high", speed="very_fast")
    assert seen == {"text": "본문", "voice": "ko_female_high", "speed": "very_fast"}


# ── 무성 폴백 제거 + 파이프라인 스탬프 (성분표 고정) ─────────────────────

def test_edge_silent_fallback_removed():
    src = (REPO / "app" / "modules" / "tts.py").read_text(encoding="utf-8")
    assert "_run_plain" not in src          # '예외 → rate/pitch 빼고 재시도' 잔재 금지


def test_pipeline_stamps_tts_backend():
    """run_log·checkpoint_resources 에 어느 백엔드로 합성됐는지 남아야 한다 —
    키 없는 노드의 edge-tts 폴백을 검수함에서 추적하는 근거(조용한 대체 금지)."""
    src = (REPO / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert '"step": "resources", "tts_backend": _tts_backend' in src
    assert '"tts_backend": _tts_backend,' in src
    assert "from app.modules.tts import active_backend" in src
    # E12: cue 합성은 synthesize_cue_cached 를 거친다(유료 경로만 캐시).
    # 그 함수가 tts.synthesize_tts_with_fit 을 그대로 부르므로 합성 계약은 종전과 같다.
    assert "final_text, actual_sec = synthesize_cue_cached(" in src
    # E17: 만료 폴백 산물을 유료 캐시에 넣지 않으려고 elevenlabs_disabled 도 함께 받는다.
    assert "is_elevenlabs_voice,\n        synthesize_tts_with_fit," in src
