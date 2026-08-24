"""E17-3 (2026-08-24) — ElevenLabs 토큰이 만료되면 기본 백엔드로 내려간다.

사용자 지시: "일레븐랩스 토큰이 만료되면 일레븐랩스 api 가 아니라 기본으로 사용되도록".

지키는 것 넷:

1. **401·403 만** 폴백이다. 404(없는 voice_id)·429·5xx·네트워크는 E11·E12 계약 그대로
   (재시도 후 크게 실패) — 요청이 잘못된 것은 내려가 봐야 같은 자리에서 또 죽는다.
2. **키가 아예 없는 것은 폴백이 아니다** — 접두사 목소리의 즉시 실패(E12)는 그대로다.
   설정 실수는 사람이 고쳐야 하고, 만료와 달리 시작 전에 알 수 있다.
3. **조용히 안 내려간다** — stdout 한 줄 + run_log/checkpoint 의 백엔드 기록이 바뀐다.
4. **전사도 같은 규칙**: 401 이면 내장 Whisper 로 **청크 전량을 다시** 전사한다
   (섞으면 한 편 안에서 자막 리듬이 갈린다).

네트워크는 타지 않는다 — requests 를 페이크로 갈아끼운다.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules import tts
from app.modules.speech import SpeechSegment
from app.pipeline import transcribe_chunks


class _Resp:
    def __init__(self, status_code=200, content=b"ID3", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def _fake_requests(monkeypatch, responses):
    calls: list[dict] = []

    class _ReqExc(Exception):
        pass

    def _post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    fake = types.ModuleType("requests")
    fake.post = _post
    fake.RequestException = _ReqExc
    monkeypatch.setitem(sys.modules, "requests", fake)
    return calls


@pytest.fixture()
def _keyed(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_expired")
    monkeypatch.setattr(tts, "time", SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None))
    # 이 컨테이너에는 edge_tts 가 없다 — 운영 노드(requirements 에 있다) 조건으로 맞춘다.
    # 없으면 폴백이 silence 로 한 칸 더 내려간다(E11 개발 폴백, 여기 검증 대상 아님).
    monkeypatch.setattr(tts, "_has_edge_tts", lambda: True)
    return tts


# ──────────────────────────────────────────────────────────────
# 1. TTS — 무엇이 폴백이고 무엇이 아닌가
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
def test_auth_status_raises_expired_without_retry(_keyed, monkeypatch, tmp_path, status):
    calls = _fake_requests(monkeypatch, [_Resp(status, text="quota_exceeded")])
    with pytest.raises(tts.ElevenLabsAuthExpired):
        tts._synthesize_elevenlabs("문장", tmp_path / "o.mp3")
    assert len(calls) == 1               # 만료는 재시도 대상이 아니다
    assert tts.elevenlabs_disabled()     # 남은 cue 는 전부 기본 백엔드로 간다


def test_non_auth_4xx_still_fails_loud(_keyed, monkeypatch, tmp_path):
    """404(없는 voice_id)는 E12 계약 그대로 — 폴백하지 않는다."""
    _fake_requests(monkeypatch, [_Resp(404, text="voice not found")])
    with pytest.raises(RuntimeError) as e:
        tts._synthesize_elevenlabs("문장", tmp_path / "o.mp3")
    assert not isinstance(e.value, tts.ElevenLabsAuthExpired)
    assert tts.elevenlabs_disabled() is None


def test_5xx_still_retries_and_fails(_keyed, monkeypatch, tmp_path):
    calls = _fake_requests(monkeypatch, [_Resp(503, text="oops")])
    with pytest.raises(RuntimeError):
        tts._synthesize_elevenlabs("문장", tmp_path / "o.mp3")
    assert len(calls) == tts._EL_RETRIES + 1
    assert tts.elevenlabs_disabled() is None


def test_label_voice_falls_back_to_edge_tts(_keyed, monkeypatch, tmp_path):
    """라벨 목소리: 만료되면 edge-tts 로 합성하고 백엔드 기록도 함께 바뀐다."""
    _fake_requests(monkeypatch, [_Resp(401, text="expired")])
    seen: dict = {}

    def _edge(text, output_path, voice_id, rate, pitch):
        seen.update(text=text, voice_id=voice_id, rate=rate, pitch=pitch)
        return output_path

    monkeypatch.setattr(tts, "_synthesize_edge_tts", _edge)
    assert tts.active_backend() == "elevenlabs"
    tts.synthesize_tts("문장", tmp_path / "o.mp3", voice="ko_male", speed="fast")
    assert seen["voice_id"] == tts.VOICE_PRESETS["ko_male"][0]
    assert seen["rate"] == tts.SPEED_TO_RATE["fast"]
    # run_log(steps[resources].tts_backend)·checkpoint_resources 가 읽는 값이다
    assert tts.active_backend() == "edge-tts"


def test_prefixed_voice_falls_back_to_default_voice(_keyed, monkeypatch, tmp_path):
    """편집실이 고른 voice_id 는 그 계정 자산이라 재현할 수 없다 — 기본 목소리로 간다."""
    _fake_requests(monkeypatch, [_Resp(401, text="expired")])
    seen: dict = {}
    monkeypatch.setattr(tts, "_synthesize_edge_tts",
                        lambda text, output_path, voice_id, rate, pitch:
                        (seen.update(voice_id=voice_id), output_path)[1])
    tts.synthesize_tts("문장", tmp_path / "o.mp3",
                       voice="elevenlabs:abcdefghijklmnop", speed="normal")
    assert seen["voice_id"] == tts.VOICE_PRESETS[tts.DEFAULT_VOICE][0]


def test_missing_key_is_still_a_hard_failure(monkeypatch, tmp_path):
    """E12 그대로 — 키가 **없는** 것은 만료가 아니라 설정 실수다."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        tts.synthesize_tts("문장", tmp_path / "o.mp3", voice="elevenlabs:abcdefghijklmnop")


def test_new_key_lifts_the_disable(_keyed, monkeypatch, tmp_path):
    """키를 갈아 끼우면 다시 시도한다 — 거절당한 것은 '그 키'다."""
    _fake_requests(monkeypatch, [_Resp(401, text="expired")])
    with pytest.raises(tts.ElevenLabsAuthExpired):
        tts._synthesize_elevenlabs("문장", tmp_path / "o.mp3")
    assert tts.elevenlabs_disabled()
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_renewed")
    assert tts.elevenlabs_disabled() is None
    assert tts.active_backend() == "elevenlabs"


def test_fallback_audio_is_not_cached_under_the_paid_key(_keyed, monkeypatch, tmp_path):
    """edge-tts 산물이 ElevenLabs 캐시 키에 들어가면 키를 갱신해도 그 소리가 되살아난다."""
    from app.pipeline import synthesize_cue_cached

    def _fit(text, output_path, *, target_sec, voice, speed, shorten_fn=None, **kw):
        Path(output_path).write_bytes(b"mp3")
        return text, 1.0

    monkeypatch.setattr(tts, "synthesize_tts_with_fit", _fit)
    monkeypatch.setattr(tts, "_el_auth_failed", "HTTP 401")
    monkeypatch.setattr(tts, "_el_auth_failed_key", "sk_expired")
    cache = tmp_path / "tts_cache"
    synthesize_cue_cached("문장", tmp_path / "o.mp3", target_sec=2.0,
                          voice="elevenlabs:abcdefghijklmnop", speed="normal",
                          cache_dir=cache)
    assert not (cache.exists() and list(cache.glob("*.mp3")))


# ──────────────────────────────────────────────────────────────
# 2. 전사(STT) — 청크 전량 재전사
# ──────────────────────────────────────────────────────────────


def test_stt_auth_status_is_its_own_error(monkeypatch, tmp_path):
    from app.modules import stt_elevenlabs as stt

    calls = _fake_requests(monkeypatch, [_Resp(401, text="expired")])
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(stt.ElevenLabsAuthError):
        stt._post_speech_to_text(audio, "k", language="ko", keyterms=None, is_raw=False)
    assert len(calls) == 1
    assert issubclass(stt.ElevenLabsAuthError, stt.ElevenLabsSTTError)


def _chunk(index, start, end, split_path):
    return SimpleNamespace(index=index, start_sec=float(start), end_sec=float(end),
                           split_path=split_path, actual_start_sec=None)


def test_transcribe_falls_back_and_redoes_every_chunk(tmp_path, monkeypatch):
    """만료 시점 앞 청크까지 **전부 다시** — 백엔드가 섞인 자막을 내보내지 않는다."""
    import app.modules.speech as speech
    from app.modules.stt_elevenlabs import ElevenLabsAuthError

    a, b = tmp_path / "c0.mp4", tmp_path / "c1.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    el_calls: list[Path] = []
    wh_calls: list[Path] = []

    def _el(path):
        el_calls.append(Path(path))
        if len(el_calls) == 1:
            return [SpeechSegment(0.0, 1.0, "일레븐")]
        raise ElevenLabsAuthError("401 인증 거절")

    monkeypatch.setattr(speech, "extract_transcript",
                        lambda p, **kw: (wh_calls.append(Path(p)),
                                         [SpeechSegment(0.0, 1.0, "내장")])[1])
    used: list[str] = []
    low: list[dict] = [{"start_sec": 1.0, "end_sec": 2.0, "logprob": -3.0}]
    out = transcribe_chunks(
        chunks=[_chunk(0, 10.0, 20.0, a), _chunk(1, 20.0, 30.0, b)],
        srt_path=None, audio_workdir=tmp_path, backend="elevenlabs",
        transcriber=_el, effective_backend_out=used, low_confidence_out=low,
    )
    assert [s.text for c in out for s in c["segments"]] == ["내장", "내장"]
    assert wh_calls == [a, b]              # 처음부터 다시
    assert used == ["elevenlabs", "default"]
    assert low == []                       # 없는 전사의 좌표는 남기지 않는다


def test_transcribe_non_auth_failure_still_raises(tmp_path):
    """E11 그대로 — 유료 백엔드를 골랐는데 자막이 비면 안 된다."""
    from app.modules.stt_elevenlabs import ElevenLabsSTTError

    c = tmp_path / "c.mp4"
    c.write_bytes(b"x")

    def _boom(path):
        raise ElevenLabsSTTError("500 서버 오류")

    with pytest.raises(ElevenLabsSTTError):
        transcribe_chunks(chunks=[_chunk(0, 0.0, 5.0, c)], srt_path=None,
                          audio_workdir=tmp_path, backend="elevenlabs", transcriber=_boom)


def test_pipeline_records_effective_backend():
    """run_log·사이드카는 **실제로 쓴** 백엔드를 적는다 — 다음 실행의 캐시 무효화 근거."""
    src = Path(__file__).resolve().parents[1] / "app" / "pipeline.py"
    s = src.read_text(encoding="utf-8")
    assert '"transcribe_backend": _tr_effective' in s
    assert '_meta: dict[str, Any] = {"transcribe_backend": _tr_effective}' in s
    assert '_res_entry["tts_fallback_reason"] = "elevenlabs_auth_expired"' in s
