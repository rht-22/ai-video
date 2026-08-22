"""E13 (2026-08-22) — E11 전사 백엔드 후속 다듬기의 회귀 가드.

발주서: ves-orchestrator `docs/prompts/e13-transcribe-polish.md`.

지키는 것 다섯 가지:

1. **회귀 0** — 이번 변경은 전부 `--transcribe-backend elevenlabs` 경로 안에서 끝난다.
   미지정·`default` 실행은 전사 텍스트도, `subtitle_segments.json` 의 모양도 종전과
   같아야 한다(auto_update 로 맥미니 6대가 바로 받는다).
2. **keyterms 직렬화** — 같은 이름 폼 필드 반복(`keyterms=A`, `keyterms=B`).
   `json.dumps` 로 배열 하나를 담으면 서버가 400 invalid_keyword_length 를 준다.
3. **언어 이탈만 버린다** — `謝 謝` 는 잡고 멀쩡한 한국어·한자 주석은 안 버린다.
   버린 줄은 전량 기록된다.
4. **저확신은 표시만** — 텍스트도 시간도 안 바꾸고, 줄별 플래그 하나만 얹는다.
5. **표기 보정은 완전 토큰 일치** — 부분 문자열 치환 금지(`아이티` 가 들어간 멀쩡한
   말을 깨뜨리면 안 된다).

네트워크는 타지 않는다 — requests 를 페이크로 갈아끼운다.
"""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from app.modules import stt_elevenlabs as stt
from app.modules.speech import SpeechSegment


@pytest.fixture(autouse=True)
def _clean_usage():
    stt.reset_usage()
    yield
    stt.reset_usage()


def _chunk(index, start, end, split_path=None):
    return SimpleNamespace(index=index, start_sec=float(start), end_sec=float(end),
                           split_path=split_path, actual_start_sec=None)


# ──────────────────────────────────────────────────────────────
# 1. keyterms — 직렬화·요율·검증 (P1-a)
# ──────────────────────────────────────────────────────────────


def test_keyterms_are_repeated_form_fields_not_json_blob():
    """계약: `keyterms=A`, `keyterms=B`.

    배열 하나를 JSON 문자열로 담으면 서버가 그 문자열 전체를 키텀 하나로 보고
    400 invalid_keyword_length 를 준다(elevenlabs-python #819 의 v2.59.0 회귀와 같은 형태).
    """
    fields = stt.build_form_fields(language="ko", keyterms=["SK텔레콤", "NC소프트"],
                                   is_raw=False)
    kt = [v for k, v in fields if k == "keyterms"]
    assert kt == ["SK텔레콤", "NC소프트"]
    assert not any(v.startswith("[") for v in kt), "JSON 배열 문자열로 보내면 안 된다"
    assert ("language_code", "kor") in fields
    assert ("timestamps_granularity", "word") in fields
    assert ("tag_audio_events", "false") in fields


def test_no_keyterms_field_when_empty():
    """보낼 항목이 없으면 필드 자체를 안 실는다 — 그 청크는 기본 요율이다."""
    fields = stt.build_form_fields(language="ko", keyterms=None, is_raw=False)
    assert not [k for k, _ in fields if k == "keyterms"]


def test_keyterms_are_validated_locally(capsys):
    """문서 제한(50자·5단어·금지문자)은 우리가 먼저 거른다.

    서버 400 은 permanent 라, 리서치 결과에 긴 작품명 하나가 섞였다고 전사 전체가
    죽으면 안 된다. 대신 버린 항목은 로그에 남는다(조용히 사라지지 않는다).
    """
    terms = stt._build_keyterms("가" * 60, ["홍길동", "홍길동", "한 두 세 네 다 섯", "a<b", ""])
    assert terms == ["홍길동"]                      # 중복·초과·금지문자 전부 제외
    assert "keyterms 제외" in capsys.readouterr().out


def test_keyterms_rate_is_charged_only_for_requests_that_sent_them():
    """정산: keyterms 를 실제로 실어 보낸 오디오만 $0.27, 나머지는 $0.22."""
    stt._USAGE["audio_duration_secs"] = 3600.0
    stt._USAGE["audio_duration_secs_keyterms"] = 1800.0
    summary = stt.usage_summary()
    expected = (1800.0 / 3600.0 * stt.EL_STT_USD_PER_AUDIO_HOUR
                + 1800.0 / 3600.0 * stt.EL_STT_KEYTERMS_USD_PER_AUDIO_HOUR)
    assert summary["estimated_usd"] == pytest.approx(round(expected, 6))
    assert stt.EL_STT_KEYTERMS_USD_PER_AUDIO_HOUR == 0.27   # $0.22 + $0.05


def test_keyterm_rejection_fails_loudly_without_retrying_without_keyterms(monkeypatch, tmp_path):
    """서버가 keyterms 를 거절하면 **keyterms 를 빼고 몰래 재시도하지 않는다**.

    조용히 빼고 성공하면 정확도 조건이 소리 없이 달라진다 — 내장 Whisper 는
    initial_prompt 를 늘 받는데 이쪽만 힌트 없이 돌게 된다.
    """
    calls = []

    class _Resp:
        status_code = 400
        text = '{"detail":{"status":"invalid_keyword_length"}}'

    fake = types.SimpleNamespace(
        post=lambda *a, **k: (calls.append(k) or _Resp()),
        RequestException=Exception,
    )
    monkeypatch.setitem(sys.modules, "requests", fake)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    with pytest.raises(stt.ElevenLabsSTTError, match="keyterms"):
        stt._post_speech_to_text(audio, "k", language="ko", keyterms=["가"], is_raw=False)
    assert len(calls) == 1, "permanent — 재시도 없음"


# ──────────────────────────────────────────────────────────────
# 2. 언어 이탈 차단 (P2-a)
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,escaped", [
    ("謝 謝 .", True),                      # 실측 — 한국어 편에 박힌 중국어
    ("アニメ", True),
    ("아저씨 이때 좋아요.", False),          # 실측 저확신이지만 멀쩡한 한국어
    ("마음처럼.", False),                    # 〃
    ("이 회장(李 會長)이 말했다", False),    # 한자 주석이 섞인 정상 한국어
    ("SK텔레콤 CTO 출신", False),
    ("2026", False),                         # 숫자만 — 판정 대상 아님
    ("와 ㅋㅋㅋ 대박", False),               # 호환 자모도 한글이다(예능 자막에 흔하다)
    ("ㅠㅠ", False),
    ("Nothing but the rain", False),         # 영어만 있는 줄(가사 등)도 정상
])
def test_language_escape_judgement(text, escaped):
    """판정은 **문자 구성**만 본다 — logprob 과 무관하다.

    실측 저확신 3건 중 2건이 멀쩡한 한국어였다. 임계로 자르면 실제 대사가 사라진다.
    """
    assert stt.is_language_escape(text, language="ko") is escaped


def test_unknown_language_is_never_judged():
    """문자군을 모르는 언어는 판정하지 않는다 — 모르면서 버리면 자막이 사라진다."""
    assert stt.is_language_escape("謝 謝", language="ja") is False
    assert stt.is_language_escape("謝 謝", language="zh") is False


def test_dropped_escape_lines_are_recorded(capsys):
    """버릴 때 **로그와 run_log 에 남긴다** — 조용히 사라지면 아무도 못 잡는다."""
    segs = [SpeechSegment(0.0, 1.0, "謝 謝 ."), SpeechSegment(1.0, 2.0, "안녕하세요.")]
    kept, conf = stt._drop_language_escapes(segs, [-1.17, -0.1], language="ko")
    assert [s.text for s in kept] == ["안녕하세요."]
    assert conf == [-0.1]                                  # 확신도도 같이 따라간다
    assert "언어이탈 drop" in capsys.readouterr().out
    dropped = stt.usage_summary()["dropped_language_escape"]
    assert len(dropped) == 1 and dropped[0]["text"] == "謝 謝 ."


# ──────────────────────────────────────────────────────────────
# 3. 표기 보정 (P1-b)
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("src,want", [
    ("에스케이텔레콤 에서 일했다", "SK텔레콤 에서 일했다"),
    ("씨티오 출신", "CTO 출신"),
    ("엔씨소프트 대표", "NC소프트 대표"),
    ("아이티 업계 흐름", "IT업계 흐름"),
    ("삼십년 만이다", "30년 만이다"),
    ("삼십년을 버텼다", "30년을 버텼다"),      # 수사는 조사까지 허용(모호하지 않다)
    ("이십사시간 영업", "24시간 영업"),
])
def test_notation_is_restored(src, want):
    assert stt.normalize_notation_text(src)[0] == want


@pytest.mark.parametrize("src", [
    "아이티 는 카리브해 국가다",     # '아이티' 단독은 안 건드린다 (Haiti)
    "아이티오 회사",                 # 부분 문자열 치환 금지
    "만원짜리 물건",                 # 백·천·만은 한글 표기가 정상
    "삼십 이라고 했다",              # 단위 없는 수사는 그대로
    "에스케이텔레콤이",              # 완전 토큰 일치만 — 조사 붙은 형태는 사전 대상 아님
])
def test_notation_does_not_touch_innocent_text(src):
    """부분 문자열 치환 금지 — 멀쩡한 말을 깨뜨리느니 못 고치는 게 낫다."""
    assert stt.normalize_notation_text(src)[0] == src


def test_notation_changes_are_reported():
    """무엇을 왜 바꿨는지 남는다 — 사람이 안 보는 사이에 전사를 고쳐 쓰는 일이다."""
    _, changes = stt.normalize_notation_text("씨티오 와 씨티오")
    assert changes == {"씨티오→CTO": 2}


def test_notation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(stt, "EL_STT_NORMALIZE_ENABLED", False)
    segs = [SpeechSegment(0.0, 1.0, "씨티오 출신")]
    assert stt._normalize_notation(segs)[0].text == "씨티오 출신"


def test_notation_never_moves_cue_boundaries():
    """보정은 cue 를 다 묶은 **뒤**다 — 시간도 cue 수도 안 바뀐다(E13-3 ⑤ 대조군)."""
    segs = [SpeechSegment(1.0, 2.5, "에스케이텔레콤 삼십년")]
    out = stt._normalize_notation(segs)
    assert len(out) == 1
    assert (out[0].start_sec, out[0].end_sec) == (1.0, 2.5)


# ──────────────────────────────────────────────────────────────
# 4. 저확신 줄은 버리지 않고 표시만 (P2-b)
# ──────────────────────────────────────────────────────────────


def test_low_confidence_lines_are_kept_and_spans_exposed():
    """저확신 cue 는 결과에 그대로 남고, 구간만 옆으로 흘러 나간다."""
    segs = [SpeechSegment(0.0, 1.0, "확실한 줄"), SpeechSegment(2.0, 3.0, "애매한 줄")]
    stt._log_result({"language_code": "kor"}, segs, [-0.1, -1.17], 3.0, language="ko")
    spans = stt.take_low_confidence_spans()
    assert len(spans) == 1 and spans[0]["start_sec"] == 2.0
    assert stt.take_low_confidence_spans() == [], "한 번 꺼내면 비워진다"
    assert stt.usage_summary()["low_confidence_count"] == 1


def test_transcribe_chunks_offsets_low_confidence_spans(tmp_path):
    """저확신 구간은 자막과 **같은 오프셋**을 받는다 — 좌표계가 하나여야 표시가 맞는다."""
    from app.pipeline import transcribe_chunks

    split = tmp_path / "c0.mp4"
    split.write_bytes(b"x")

    def _fake(_path):
        stt._LAST_LOW_CONF_SPANS.append({"start_sec": 5.0, "end_sec": 6.0, "logprob": -1.2})
        return [SpeechSegment(5.0, 6.0, "애매")]

    out: list = []
    got = transcribe_chunks(chunks=[_chunk(0, 100.0, 200.0, split)], srt_path=None,
                            audio_workdir=tmp_path, transcriber=_fake,
                            backend="elevenlabs", low_confidence_out=out)
    assert got[0]["segments"][0].start_sec == 105.0     # 자막 좌표계
    assert out == [{"start_sec": 105.0, "end_sec": 106.0, "logprob": -1.2}]


def test_default_backend_never_produces_low_confidence_spans(tmp_path, monkeypatch):
    """회귀 0 — 내장 경로는 저확신 표시를 만들지 않는다(Whisper 는 줄별 확신도를 안 준다)."""
    import app.modules.speech as speech
    from app.pipeline import transcribe_chunks

    monkeypatch.setattr(speech, "extract_transcript",
                        lambda p, **k: [SpeechSegment(1.0, 2.0, "내장")])
    split = tmp_path / "c0.mp4"
    split.write_bytes(b"x")
    stt._LAST_LOW_CONF_SPANS.append({"start_sec": 5.0, "end_sec": 6.0, "logprob": -1.2})

    out: list = []
    transcribe_chunks(chunks=[_chunk(0, 10.0, 20.0, split)], srt_path=None,
                      audio_workdir=tmp_path, low_confidence_out=out)
    assert out == []


# ──────────────────────────────────────────────────────────────
# 5. subtitle_segments.json 스키마 — 표시된 줄에만 키가 붙는다
# ──────────────────────────────────────────────────────────────


def _seg(start, end, text="t"):
    return SimpleNamespace(start_sec=start, end_sec=end, text=text)


def test_subtitle_json_shape_unchanged_without_flag():
    """회귀 0 — 표시 없는 줄은 종전 3필드 그대로다."""
    from app.pipeline import _subtitle_segment_json

    assert _subtitle_segment_json(_seg(0.0, 1.0)) == {
        "start_sec": 0.0, "end_sec": 1.0, "text": "t"}


def test_subtitle_json_gains_one_flag_when_marked():
    from app.pipeline import _subtitle_segment_json

    s = _seg(0.0, 1.0)
    s.low_confidence = True
    out = _subtitle_segment_json(s)
    assert out["low_confidence"] is True
    assert out["text"] == "t", "텍스트는 그대로 — 표시만 얹는다"


def test_flagging_is_skipped_for_default_backend(tmp_path):
    """회귀 0 — elevenlabs 를 **명시한 실행에서만** 표시가 붙는다."""
    from app.pipeline import _flag_low_confidence_segments

    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"transcribe_backend": "elevenlabs",
                                "low_confidence_spans": [{"start_sec": 0.0, "end_sec": 9.0}]}),
                    encoding="utf-8")
    clips = [SimpleNamespace(start_sec=0.0, end_sec=10.0, use_original_audio=True, subtitle="")]
    segs = [_seg(0.0, 2.0)]
    for backend in (None, "default"):
        assert _flag_low_confidence_segments(segs, clips, meta, backend=backend) == 0
        assert not hasattr(segs[0], "low_confidence")


def test_flagging_maps_source_time_to_edited_timeline(tmp_path):
    """표시는 **편집본 타임라인**의 자막 줄에 붙는다 — 자막과 같은 remap 함수를 탄다.

    클립 2개(원본 100~110, 200~210)를 이어 붙이면 원본 205s 는 편집본 15s 다.
    """
    from app.pipeline import _flag_low_confidence_segments

    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({
        "transcribe_backend": "elevenlabs",
        "low_confidence_spans": [{"start_sec": 205.0, "end_sec": 206.0, "logprob": -1.2}],
    }), encoding="utf-8")
    clips = [
        SimpleNamespace(start_sec=100.0, end_sec=110.0, use_original_audio=True, subtitle=""),
        SimpleNamespace(start_sec=200.0, end_sec=210.0, use_original_audio=True, subtitle=""),
    ]
    early, late = _seg(2.0, 3.0), _seg(15.0, 16.0)
    assert _flag_low_confidence_segments([early, late], clips, meta,
                                         backend="elevenlabs") == 1
    assert getattr(late, "low_confidence", False) is True
    assert not hasattr(early, "low_confidence")


def test_flagging_is_noop_without_spans(tmp_path):
    from app.pipeline import _flag_low_confidence_segments

    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"transcribe_backend": "elevenlabs"}), encoding="utf-8")
    clips = [SimpleNamespace(start_sec=0.0, end_sec=10.0, use_original_audio=True, subtitle="")]
    assert _flag_low_confidence_segments([_seg(0.0, 1.0)], clips, meta,
                                         backend="elevenlabs") == 0


def test_extra_key_survives_speed_scaling_and_is_ignored_by_ass_builder(tmp_path):
    """downstream 이 모르는 필드에 안 깨지는지 — 스키마를 늘렸으니 확인해야 한다."""
    from app.modules.subtitle import SubtitleStyle, build_ass_from_segments
    from app.pipeline import _scale_segments_for_speed

    s = _seg(0.0, 2.0, "안녕하세요")
    s.low_confidence = True
    scaled = _scale_segments_for_speed([s], 2.0)
    assert scaled[0].start_sec == 0.0 and scaled[0].end_sec == 1.0
    assert scaled[0].low_confidence is True          # 배속 조정이 표시를 안 떨어뜨린다

    out = tmp_path / "s.ass"
    build_ass_from_segments(scaled, out, SubtitleStyle())
    assert out.exists() and "안녕하세요" in out.read_text(encoding="utf-8")


def test_editor_override_normalization_drops_unknown_key():
    """편집실이 그대로 되돌려 보내도 거절하지 않고, 자막 정본 3필드로 정규화된다."""
    from app.modules.edit_overrides import overrides_subtitles

    out = overrides_subtitles({"subtitles": [
        {"start_sec": 0.0, "end_sec": 1.0, "text": "고친 문장", "low_confidence": True},
    ]})
    assert out == [{"start_sec": 0.0, "end_sec": 1.0, "text": "고친 문장"}]


# ──────────────────────────────────────────────────────────────
# 6. 곁다리 — 모르는 TTS 라벨은 조용히 기본값으로 안 떨어진다
# ──────────────────────────────────────────────────────────────


def test_unknown_voice_label_fails_loudly(monkeypatch, tmp_path):
    from app.modules import tts

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    with pytest.raises(RuntimeError, match="모르는 voice 라벨"):
        tts._synthesize_elevenlabs("t", tmp_path / "o.mp3", voice="ko_femalee")


def test_unknown_speed_label_fails_loudly(monkeypatch, tmp_path):
    from app.modules import tts

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    with pytest.raises(RuntimeError, match="모르는 speed 라벨"):
        tts._synthesize_elevenlabs("t", tmp_path / "o.mp3", speed="warp9")


def test_legacy_voice_label_still_resolves():
    """구 체크포인트가 싣는 narrative_female 은 **오늘과 같은 목소리**로 — 재개 회귀 0."""
    from app.modules import tts

    assert tts._resolve_label("voice", "narrative_female", tts.EL_VOICE_PRESETS,
                              tts.DEFAULT_VOICE) == tts.DEFAULT_VOICE


def test_ffmpeg_not_found_message_matches_host_os(monkeypatch):
    """운영 노드 6대는 macOS 다 — 윈도우 설치법만 뜨면 사람이 매번 걸린다."""
    import app.modules.ffmpeg_utils as fu

    monkeypatch.setattr(fu.sys, "platform", "darwin")
    monkeypatch.setattr(fu.shutil, "which", lambda _c: None)
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.delenv("FFPROBE_BIN", raising=False)
    with pytest.raises(FileNotFoundError, match="brew install ffmpeg"):
        fu.find_ffmpeg_command("ffmpeg")
