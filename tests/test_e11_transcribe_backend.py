"""E11 (2026-08-22) — 자막 전사 백엔드 선택(`--transcribe-backend`) 회귀 가드.

지키는 것 세 가지:

1. **회귀 0** — 플래그 없는 실행은 종전과 동일하다. transcribe_chunks 가 backend 인자
   없이 불리면 내장 Whisper(extract_transcript) 를 그대로 쓰고, PipelineInput 기본값은
   None 이며, 청크 오프셋·SRT 분기 규칙은 두 백엔드가 공유한다.
2. **허용값 밖은 즉시 실패** — argparse choices. 조용히 기본값으로 떨어지면 사람은
   일레븐랩스로 바꿨다고 믿은 채 종전 전사로 발행된다.
3. **Scribe 어댑터 계약** — words[] 의 spacing·audio_event 를 걸러내고, 실패 분류가
   permanent/transient 로 갈리며, 키가 없으면 조용한 폴백 없이 즉시 실패한다.

네트워크는 타지 않는다 — requests 를 페이크로 갈아끼운다.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.speech import SpeechSegment
from app.pipeline import PipelineInput, transcribe_chunks


def _chunk(index, start, end, split_path=None):
    return SimpleNamespace(index=index, start_sec=float(start), end_sec=float(end),
                           split_path=split_path, actual_start_sec=None)


# ──────────────────────────────────────────────────────────────
# 1. 회귀 0 — 플래그 없는 실행
# ──────────────────────────────────────────────────────────────


def test_pipeline_input_default_backend_is_none():
    """미지정이 기본 — 이 값이 바뀌면 플래그 없는 채널 전부가 새 경로로 넘어간다."""
    payload = PipelineInput(video_path=Path("x.mp4"), work_title="t", topic="",
                            outdir=Path("out"))
    assert payload.transcribe_backend is None


def test_no_backend_uses_builtin_extract_transcript(tmp_path, monkeypatch):
    """backend 인자 없이 부르면 내장 Whisper(extract_transcript) 가 호출된다."""
    import app.modules.speech as speech

    calls: list[Path] = []

    def _fake_extract(audio_path, **kwargs):
        calls.append(Path(audio_path))
        return [SpeechSegment(start_sec=1.0, end_sec=2.0, text="내장")]

    monkeypatch.setattr(speech, "extract_transcript", _fake_extract)

    split = tmp_path / "chunk0.mp4"
    split.write_bytes(b"x")
    out = transcribe_chunks(
        chunks=[_chunk(0, 100.0, 200.0, split)],
        srt_path=None,
        audio_workdir=tmp_path,
    )
    assert calls == [split]
    # 청크 오프셋 가산 규칙(=편집실 앵커 좌표계)은 종전 그대로
    assert out[0]["segments"][0].start_sec == 101.0
    assert out[0]["segments"][0].end_sec == 102.0


def test_explicit_default_matches_unspecified(tmp_path, monkeypatch):
    """`default` 를 명시해도 미지정과 결과가 같다(발주 계약)."""
    import app.modules.speech as speech
    monkeypatch.setattr(speech, "extract_transcript",
                        lambda p, **kw: [SpeechSegment(0.5, 1.5, "같다")])
    split = tmp_path / "c.mp4"
    split.write_bytes(b"x")
    chunks = [_chunk(3, 10.0, 20.0, split)]
    a = transcribe_chunks(chunks=chunks, srt_path=None, audio_workdir=tmp_path)
    b = transcribe_chunks(chunks=chunks, srt_path=None, audio_workdir=tmp_path,
                          backend="default")
    assert [(s.start_sec, s.end_sec, s.text) for s in a[0]["segments"]] == \
           [(s.start_sec, s.end_sec, s.text) for s in b[0]["segments"]]


def test_builtin_branch_keeps_swallowing_chunk_failure(tmp_path):
    """내장 경로의 '한 청크 실패 → 빈 결과로 진행' 은 종전 그대로 유지된다."""
    split = tmp_path / "c.mp4"
    split.write_bytes(b"x")

    def _boom(_p):
        raise RuntimeError("whisper down")

    out = transcribe_chunks(chunks=[_chunk(0, 0.0, 10.0, split)], srt_path=None,
                            audio_workdir=tmp_path, transcriber=_boom)
    assert out == [{"chunk_index": 0, "segments": []}]


def test_elevenlabs_branch_fails_loud_on_chunk_failure(tmp_path):
    """반대로 elevenlabs 는 빈 자막으로 조용히 발행하지 않고 크게 실패한다."""
    split = tmp_path / "c.mp4"
    split.write_bytes(b"x")

    def _boom(_p):
        raise RuntimeError("scribe 500")

    with pytest.raises(RuntimeError, match="scribe 500"):
        transcribe_chunks(chunks=[_chunk(0, 0.0, 10.0, split)], srt_path=None,
                          audio_workdir=tmp_path, transcriber=_boom,
                          backend="elevenlabs")


def test_srt_wins_over_backend_and_says_so(tmp_path, capsys):
    """자막 파일이 있으면 전사 자체가 없다 — 조용히 무시하지 않고 로그로 알린다."""
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n사람이 준 자막\n\n", encoding="utf-8")
    out = transcribe_chunks(chunks=[_chunk(0, 0.0, 10.0)], srt_path=srt,
                            audio_workdir=tmp_path, backend="elevenlabs")
    assert [s.text for s in out[0]["segments"]] == ["사람이 준 자막"]
    assert "backend=elevenlabs" in capsys.readouterr().out


def test_backend_branch_ends_at_transcriber_selection(tmp_path):
    """두 백엔드가 같은 좌표계로 돌아온다 — 같은 raw 를 주면 결과가 동일하다."""
    split = tmp_path / "c.mp4"
    split.write_bytes(b"x")
    raw = [SpeechSegment(2.0, 4.0, "동일 좌표계")]
    chunks = [_chunk(2, 300.0, 400.0, split)]
    a = transcribe_chunks(chunks=chunks, srt_path=None, audio_workdir=tmp_path,
                          transcriber=lambda p: raw, backend="default")
    b = transcribe_chunks(chunks=chunks, srt_path=None, audio_workdir=tmp_path,
                          transcriber=lambda p: raw, backend="elevenlabs")
    assert [(s.start_sec, s.end_sec, s.text) for s in a[0]["segments"]] == [(302.0, 304.0, "동일 좌표계")]
    assert a == b


# ──────────────────────────────────────────────────────────────
# 2. CLI 플래그 — 이름·choices 는 오케스트레이터와의 계약
# ──────────────────────────────────────────────────────────────


def _parse(argv):
    from app.cli import build_parser
    return build_parser().parse_args(argv)


_BASE = ["create_shorts", "--video", "v.mp4", "--title", "t"]


def test_flag_absent_is_none():
    assert _parse(_BASE).transcribe_backend is None


@pytest.mark.parametrize("value", ["default", "elevenlabs"])
def test_flag_accepts_contract_values(value):
    assert _parse(_BASE + ["--transcribe-backend", value]).transcribe_backend == value


@pytest.mark.parametrize("value", ["eleven", "ElevenLabs", "whisper", ""])
def test_flag_rejects_everything_else(value):
    """조용히 기본값으로 떨어지지 않고 argparse 가 즉시 죽는다."""
    with pytest.raises(SystemExit):
        _parse(_BASE + ["--transcribe-backend", value])


# ──────────────────────────────────────────────────────────────
# 3. ElevenLabs Scribe 어댑터
# ──────────────────────────────────────────────────────────────


def _words(*items):
    """(text, start, end, type) 튜플 → 응답 words[] dict."""
    return [{"text": t, "start": s, "end": e, "type": ty, "logprob": lp}
            for t, s, e, ty, lp in items]


def test_words_filter_spacing_and_audio_events():
    """type=='word' 만 cue 로 묶는다 — (laughter) 가 자막에 새면 안 된다."""
    from app.modules.stt_elevenlabs import words_to_segments

    segs, _ = words_to_segments(_words(
        ("안녕", 0.0, 0.4, "word", -0.05),
        (" ", 0.4, 0.4, "spacing", 0.0),
        ("(laughter)", 0.4, 0.7, "audio_event", -0.2),
        ("하세요", 0.7, 1.4, "word", -0.08),
    ))
    assert len(segs) == 1
    assert segs[0].text == "안녕 하세요"
    assert "laughter" not in segs[0].text
    assert (segs[0].start_sec, segs[0].end_sec) == (0.0, 1.4)


def test_words_split_on_silence_gap():
    """0.5s 이상 공백이면 새 cue (내장 Whisper VAD min_silence 500ms 와 같은 기준)."""
    from app.modules.stt_elevenlabs import words_to_segments

    segs, _ = words_to_segments(_words(
        ("첫", 0.0, 0.3, "word", -0.1),
        ("줄", 0.3, 0.6, "word", -0.1),
        ("둘째", 2.0, 2.4, "word", -0.1),
    ))
    assert [s.text for s in segs] == ["첫 줄", "둘째"]
    assert segs[1].start_sec == 2.0


def test_words_split_on_sentence_end():
    from app.modules.stt_elevenlabs import words_to_segments

    segs, _ = words_to_segments(_words(
        ("갔어요.", 0.0, 0.5, "word", -0.1),
        ("그리고", 0.6, 0.9, "word", -0.1),
    ))
    assert [s.text for s in segs] == ["갔어요.", "그리고"]


def test_words_split_on_char_and_duration_caps():
    """44자·6.0s 상한 — merge_subtitle_segments 와 같은 수치로 묶는다."""
    from app.modules.stt_elevenlabs import words_to_segments

    long_words = [(f"단어{i}", i * 0.4, i * 0.4 + 0.3, "word", -0.1) for i in range(20)]
    segs, _ = words_to_segments(_words(*long_words))
    assert len(segs) > 1
    for s in segs:
        assert len(s.text) <= 44
        assert s.end_sec - s.start_sec <= 6.0 + 1e-6


def test_words_mean_logprob_per_cue():
    """cue 별 평균 logprob — 검수자가 어디를 볼지 알려 주는 값(결과는 안 바꾼다)."""
    from app.modules.stt_elevenlabs import words_to_segments

    segs, conf = words_to_segments(_words(
        ("가", 0.0, 0.2, "word", -1.0),
        ("나", 0.2, 0.4, "word", -0.0),
    ))
    assert len(segs) == 1
    assert conf[0] == pytest.approx(-0.5)


def test_missing_key_fails_immediately_with_instructions(monkeypatch):
    from app.modules.stt_elevenlabs import ElevenLabsSTTError, build_transcriber

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(ElevenLabsSTTError) as e:
        build_transcriber(language="ko")
    assert "ELEVENLABS_API_KEY" in str(e.value)


# ── 실패 분류: permanent(4xx) 즉시 / transient(429·5xx·네트워크) 재시도 ──────


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _install_fake_requests(monkeypatch, responses):
    """requests.post 를 응답 큐로 대체. (호출 기록, 마지막 kwargs) 반환."""
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
    return calls, _ReqExc


@pytest.fixture()
def _stt_env(monkeypatch, tmp_path):
    """키를 심고 ffmpeg 오디오 추출을 페이크로 대체(네트워크·ffmpeg 무의존)."""
    import app.modules.stt_elevenlabs as stt

    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test")
    monkeypatch.setattr(stt, "time", SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None))
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(stt, "_extract_audio", lambda p: (audio, False))
    stt.reset_usage()
    return stt


def test_permanent_4xx_is_not_retried(_stt_env, monkeypatch):
    stt = _stt_env
    calls, _ = _install_fake_requests(monkeypatch, [_FakeResp(401, text="bad key")])
    with pytest.raises(stt.ElevenLabsSTTError, match="401"):
        stt.transcribe(Path("x.mp4"))
    assert len(calls) == 1          # 재시도 없음


def test_transient_5xx_is_retried_then_fails(_stt_env, monkeypatch):
    stt = _stt_env
    calls, _ = _install_fake_requests(monkeypatch, [_FakeResp(503, text="upstream")])
    with pytest.raises(stt.ElevenLabsSTTError):
        stt.transcribe(Path("x.mp4"))
    assert len(calls) == 3          # 첫 시도 + 2회 재시도


def test_transient_429_retries_and_can_succeed(_stt_env, monkeypatch):
    stt = _stt_env
    ok = _FakeResp(200, {"language_code": "kor", "audio_duration_secs": 12.5,
                         "words": _words(("성공", 0.0, 0.5, "word", -0.1))})
    calls, _ = _install_fake_requests(monkeypatch, [_FakeResp(429, text="rate"), ok])
    segs = stt.transcribe(Path("x.mp4"))
    assert [s.text for s in segs] == ["성공"]
    assert len(calls) == 2
    usage = stt.usage_summary()
    assert usage["audio_duration_secs"] == 12.5
    # 분 단위 과금 — 정산 근거가 run_log 에 남는다
    assert usage["estimated_usd"] == pytest.approx(12.5 / 3600 * 0.22, rel=1e-3)


def test_request_body_matches_contract(_stt_env, monkeypatch):
    """model_id·language_code·tag_audio_events=false·timestamps_granularity 계약."""
    stt = _stt_env
    ok = _FakeResp(200, {"words": _words(("가", 0.0, 0.3, "word", -0.1)),
                         "audio_duration_secs": 1.0})
    calls, _ = _install_fake_requests(monkeypatch, [ok])
    stt.transcribe(Path("x.mp4"), language="ko")

    sent = calls[0]
    assert sent["url"] == "https://api.elevenlabs.io/v1/speech-to-text"
    assert sent["headers"]["xi-api-key"] == "sk_test"
    data = sent["data"]
    assert data["model_id"] == "scribe_v2"        # scribe_v1 은 폐기됨
    assert data["language_code"] == "kor"         # ISO-639-3
    assert data["tag_audio_events"] == "false"    # 기본 true — (laughter) 유입 차단
    assert data["timestamps_granularity"] == "word"
    assert data["diarize"] == "false"
    # additional_formats 는 쓰지 않는다 — cue 규칙은 파이프라인이 정본
    assert "additional_formats" not in data
    # keyterms 는 기본 off(요율 상승 + 폼 직렬화 미검증)
    assert "keyterms" not in data
    assert "file" in sent["files"]


def test_unspecified_backend_prints_nothing_new(tmp_path, capsys):
    """플래그 없는 실행의 stdout 은 종전 그대로 — 새 로그 한 줄도 안 붙는다(회귀 0)."""
    split = tmp_path / "c.mp4"
    split.write_bytes(b"x")
    transcribe_chunks(chunks=[_chunk(0, 0.0, 10.0, split)], srt_path=None,
                      audio_workdir=tmp_path, transcriber=lambda p: [])
    assert "[transcribe] backend=" not in capsys.readouterr().out


def test_cli_fails_before_long_run_when_key_missing(monkeypatch, capsys):
    """68분을 태우기 전에 죽는다 — 메시지가 '무엇을 어디에 넣어야 하는지' 말한다."""
    import app.cli as cli

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(cli, "ensure_ffmpeg_supported", lambda: None)
    monkeypatch.setattr(sys, "argv",
                        ["cli"] + _BASE + ["--transcribe-backend", "elevenlabs"])
    with pytest.raises(SystemExit):
        cli.main()
    err = capsys.readouterr().err
    assert "ELEVENLABS_API_KEY" in err


def test_wordless_response_fails_loud(_stt_env, monkeypatch):
    """text 만 오고 word 타임코드가 없으면(스키마 변동) 빈 자막 대신 실패한다."""
    stt = _stt_env
    _install_fake_requests(monkeypatch, [_FakeResp(200, {"text": "말은 있는데", "words": []})])
    with pytest.raises(stt.ElevenLabsSTTError, match="words"):
        stt.transcribe(Path("x.mp4"))
