"""V4-M3 §1 영상 호출 회귀 가드 — `app/v4/video.py`.

이 파일이 고정하는 것은 계약 `docs/v4/M3-interfaces.md` §1 의 문장들이다:

① **offset 멀티파트** — 같은 핸들을 조각 수만큼 붙이고 **첨부 순서가 곧 편집 순서**다
   (기획서 §2-B 실측). 포맷은 `_offset` **한 함수**에서만 하고 항상 `s` 접미를 붙인다.
   ⚠ 여기서 쓰는 `types` 는 **진짜 google-genai** 다 — 가짜 타입으로 조립하면 필드
   이름·타입이 틀려도 테스트가 통과한다(이 저장소에 선례가 0건인 배선이라 특히 위험).
② **경계 벨트** — `endOffset` 은 소스를 넘어도 조용히 클램프되므로 **보내기 전에** 자른다.
③ **usage 기록** — 계약이 적은 열쇠 전부. 없는 값은 0 이 아니라 None.
④ **MAX_TOKENS 절단은 조용하면 안 된다**(v3-M2 실사고).
⑤ **재시도 분류 E11** — 429·5xx·네트워크만, 그 밖의 4xx 는 즉시 실패.
⑥ **핸들을 삭제하지 않는다** — 6·6b·8·10a 가 공유한다(v3 는 네 곳이 finally 에서 지운다).

🛑 네트워크는 쓰지 않는다(가짜 클라이언트). 실호출로만 알 수 있는 것 — 서버가 파트
순서대로 이어 붙여 보는가 · media_processing 의 실제 효과 — 은 이 파일의 범위 밖이고
모듈 독스트링이 '확인 못 함'으로 적어 두었다.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.v4 import video as V
from app.v4.video import Clip


# ── 가짜 클라이언트 ─────────────────────────────────────────────────────────

def _response(text: str = '{"ok": true}', *, finish: str = "STOP",
              prompt_tok: int | None = 1000, thoughts_tok: int | None = 200,
              out_tok: int | None = 50, cached_tok: int | None = 0,
              total_tok: int | None = 1250,
              model_version: str | None = "gemini-3.7-flash-001",
              usage: bool = True):
    """SDK 응답 흉내 — `_usage_counts`·`_finish_reason`·`_max_tokens_usage` 가 읽는
    표면만 만든다(전부 getattr 기반이라 SimpleNamespace 로 충분하다)."""
    meta = None
    if usage:
        meta = SimpleNamespace(prompt_token_count=prompt_tok,
                               thoughts_token_count=thoughts_tok,
                               candidates_token_count=out_tok,
                               cached_content_token_count=cached_tok,
                               total_token_count=total_tok)
    return SimpleNamespace(
        text=text,
        usage_metadata=meta,
        model_version=model_version,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
    )


class _FakeModels:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.queue.pop(0) if self.queue else _response()
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeFiles:
    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, *, name):
        self.deleted.append(name)


class _FakeGemini:
    """`gemini.types` 는 **진짜 SDK 타입**이다(위 ① 참조)."""

    def __init__(self, queue=()):
        from google.genai import types

        self.types = types
        self.models = _FakeModels(queue)
        self.files = _FakeFiles()
        self.client = SimpleNamespace(models=self.models, files=self.files)
        self.config = SimpleNamespace(flash_model_name="gemini-3.7-flash",
                                      model_name="gemini-3.7-flash")


HANDLE = SimpleNamespace(uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
                         name="files/abc")


def _video_parts(contents):
    return [p for p in contents if getattr(p, "file_data", None) is not None]


def _meta(part):
    """파트의 video_metadata 를 **직렬화된 모양**으로 — 서버가 받는 열쇠 이름 그대로."""
    return part.video_metadata.model_dump(exclude_none=True, by_alias=True)


class _ApiError(Exception):
    """google-genai `APIError` 흉내 — `.code` 에 정수 상태를 담는다(SDK 실측)."""

    def __init__(self, code: int, message: str = "boom"):
        super().__init__(f"{code} {message}")
        self.code = code


# ── ① 파트 조립 · 순서 보존 ─────────────────────────────────────────────────

def test_no_clips_attaches_whole_video_once():
    """조각을 안 주면 파트 하나로 전체 — v3 네 곳과 같은 모양(offset 필드 없음)."""
    g = _FakeGemini()
    parts = V.build_video_parts(g, HANDLE, None, sample_fps=2.0)
    assert len(parts) == 1
    assert _meta(parts[0]) == {"fps": 2.0}
    assert parts[0].file_data.file_uri == HANDLE.uri
    assert parts[0].file_data.mime_type == "video/mp4"


def test_multipart_offsets_are_duration_strings():
    """SDK 실측: start/end_offset 은 **문자열**이고 `s` 접미가 붙는다. 소수점도 산다."""
    g = _FakeGemini()
    parts = V.build_video_parts(
        g, HANDLE, [Clip(10.0, 25.5), Clip(120.25, 130.0)], sample_fps=4.0)
    assert [_meta(p) for p in parts] == [
        {"startOffset": "10.000s", "endOffset": "25.500s", "fps": 4.0},
        {"startOffset": "120.250s", "endOffset": "130.000s", "fps": 4.0},
    ]


def test_offset_is_formatted_in_one_place():
    """`_offset` 이 유일한 포맷 자리다 — 두 곳에서 포맷하면 한쪽이 접미를 빠뜨린다."""
    assert V._offset(0) == "0.000s"
    assert V._offset(7.1234) == "7.123s"
    with pytest.raises(ValueError):
        V._offset(-1.0)
    with pytest.raises(ValueError):
        V._offset(float("inf"))


def test_only_one_place_formats_an_offset():
    """계약 §1: "두 곳에서 포맷하면 한쪽이 접미를 빠뜨리는 날 조용히 다른 구간을 본다."

    `_offset` 의 소스를 통째로 들어낸 나머지에 초→Duration 포맷이 남아 있으면 두 번째
    포맷 자리가 생긴 것이다."""
    import inspect

    src = Path(V.__file__).read_text(encoding="utf-8")
    rest = src.replace(inspect.getsource(V._offset), "")
    assert ':.3f}s"' not in rest


def test_attachment_order_is_edit_order():
    """🛑 기획서 §2-B: 역순으로 넣으면 답도 역순이다. 이 함수는 순서를 절대 안 바꾼다."""
    g = _FakeGemini()
    clips = [Clip(100.0, 102.0), Clip(0.0, 2.0), Clip(40.0, 42.0)]
    parts = V.build_video_parts(g, HANDLE, clips, sample_fps=1.0)
    assert [_meta(p)["startOffset"] for p in parts] == \
        ["100.000s", "0.000s", "40.000s"]


def test_all_parts_reuse_the_same_handle():
    """멀티파트의 요점 — 재단·재업로드 없이 같은 파일을 여러 번 붙인다."""
    g = _FakeGemini()
    parts = V.build_video_parts(g, HANDLE, [Clip(0, 2), Clip(4, 6), Clip(10, 12)],
                                sample_fps=1.0)
    assert {p.file_data.file_uri for p in parts} == {HANDLE.uri}


def test_handle_may_be_a_uri_string():
    """재개 경로는 체크포인트의 문자열만 든다(`checkpoint_upload.json` handle.uri)."""
    g = _FakeGemini()
    parts = V.build_video_parts(g, "https://x/files/abc", None, sample_fps=1.0)
    assert parts[0].file_data.file_uri == "https://x/files/abc"
    with pytest.raises(ValueError):
        V.build_video_parts(g, SimpleNamespace(uri=""), None, sample_fps=1.0)


def test_sample_fps_hard_cap():
    """SDK 필드 설명 · 프로브 실측이 같은 상한(24)을 말한다 — 보내기 전에 죽는다."""
    g = _FakeGemini()
    for bad in (0.0, -1.0, 24.5, float("nan")):
        with pytest.raises(ValueError):
            V.build_video_parts(g, HANDLE, None, sample_fps=bad)
    V.build_video_parts(g, HANDLE, None, sample_fps=24.0)      # 경계는 통과


def test_open_ended_clip_is_refused():
    """끝을 안 주면 서버가 소스 끝까지 읽고 그 길이를 우리가 모른다(조용한 클램프의 입구)."""
    g = _FakeGemini()
    with pytest.raises(ValueError, match="end_sec"):
        V.build_video_parts(g, HANDLE, [Clip(10.0, None)], sample_fps=1.0)


def test_media_processing_default_is_absent():
    """기획서 §12 가 '미확인'으로 둔 필드 — 기본은 안 보낸다."""
    g = _FakeGemini()
    part = V.build_video_parts(g, HANDLE, None, sample_fps=1.0)[0]
    assert part.media_processing is None
    on = V.build_video_parts(g, HANDLE, None, sample_fps=1.0,
                             media_processing="STATIC")[0]
    assert str(getattr(on.media_processing, "value", on.media_processing)) == "STATIC"
    with pytest.raises(ValueError, match="media_processing"):
        V.build_video_parts(g, HANDLE, None, sample_fps=1.0, media_processing="LOW")


def test_media_processing_is_a_part_field_not_a_config_field():
    """이 워크트리 실측 — 기획서 §4 의 'STATIC 명시'가 어느 표면인지 못박는다."""
    from google.genai import types

    assert "media_processing" in types.Part.model_fields
    assert "media_processing" not in types.GenerateContentConfig.model_fields


# ── ② 경계 벨트 (소스 밖 클램프) ────────────────────────────────────────────

def test_clips_within_source_keeps_inside_clips_untouched():
    kept, notes = V.clips_within_source([Clip(10.0, 20.0), Clip(30.0, 40.0)], 100.0)
    assert kept == [Clip(10.0, 20.0), Clip(30.0, 40.0)]
    assert notes == []


def test_clips_within_source_clamps_tail():
    """🛑 endOffset 은 소스를 넘어도 오류 없이 클램프된다 — 우리가 먼저 자르고 기록한다."""
    kept, notes = V.clips_within_source([Clip(90.0, 130.0)], 100.0)
    assert kept == [Clip(90.0, 100.0)]
    assert len(notes) == 1 and notes[0]["action"] == "clamped"
    assert notes[0]["new_end_sec"] == 100.0


def test_clips_within_source_drops_clip_starting_outside():
    """시작이 소스 밖이면 모델은 아무것도 못 본다 — 클램프가 아니라 드롭이다."""
    kept, notes = V.clips_within_source([Clip(10.0, 20.0), Clip(200.0, 226.0)], 100.0)
    assert kept == [Clip(10.0, 20.0)]
    assert [n["action"] for n in notes] == ["dropped"]
    assert "소스 밖" in notes[0]["reason"]


def test_clips_within_source_drops_empty_after_clamp():
    kept, notes = V.clips_within_source([Clip(99.99, 120.0)], 100.0)
    assert kept == []
    assert [n["action"] for n in notes] == ["clamped", "dropped"]


def test_clips_within_source_records_are_not_all_drops():
    """`len(notes)` 는 드롭 수가 아니다 — 클램프도 모델이 보는 것을 바꾸므로 남긴다."""
    kept, notes = V.clips_within_source(
        [Clip(90.0, 130.0), Clip(200.0, 210.0), Clip(0.0, 5.0)], 100.0)
    assert kept == [Clip(90.0, 100.0), Clip(0.0, 5.0)]
    assert [n["action"] for n in notes] == ["clamped", "dropped"]
    assert len([n for n in notes if n["action"] == "dropped"]) == 1


def test_clips_within_source_keeps_whole_clip():
    kept, notes = V.clips_within_source([Clip()], 100.0)
    assert kept == [Clip()] and notes == []


def test_clips_within_source_fails_loud_on_unknown_duration():
    """길이를 모르면 판정하지 않고 **크게 실패**한다 — 전량 통과시키면 벨트가 사라진다."""
    for bad in (0.0, -1.0, None, float("nan")):
        with pytest.raises(ValueError):
            V.clips_within_source([Clip(1.0, 2.0)], bad)


def test_clips_within_source_is_pure():
    """순수 — 넘겨받은 것을 제자리에서 고치지 않고, 같은 입력이면 같은 출력."""
    src = [Clip(90.0, 130.0), Clip(0.0, 5.0)]
    before = list(src)
    a = V.clips_within_source(src, 100.0)
    b = V.clips_within_source(src, 100.0)
    assert src == before
    assert a == b


# ── ③ usage 기록 ────────────────────────────────────────────────────────────

def test_usage_note_has_every_contract_key():
    note = V.usage_note(_response(), elapsed_sec=1.2345, sample_fps=2.0,
                        media_resolution="LOW", parts=3)
    assert note == {
        "prompt": 1000, "thoughts": 200, "candidates": 50, "cached": 0,
        "total": 1250, "finish_reason": "STOP",
        "model_version": "gemini-3.7-flash-001", "elapsed_sec": 1.234,
        "sample_fps": 2.0, "media_resolution": "LOW", "parts": 3,
    }


def test_usage_note_missing_numbers_are_none_not_zero():
    """'안 왔다'와 '0 이었다'가 같아지면 집계가 조용히 틀어진다."""
    note = V.usage_note(_response(usage=False), elapsed_sec=0.0, sample_fps=1.0,
                        media_resolution=None, parts=1)
    assert note["prompt"] is None and note["total"] is None and note["cached"] is None
    assert note["media_resolution"] is None       # 미지정 = 실측상 LOW


def test_usage_note_records_part_windows_when_given_clips():
    note = V.usage_note(_response(), elapsed_sec=0.5, sample_fps=3.0,
                        media_resolution=None, parts=[Clip(1.0, 2.0), Clip(9.0, 12.0)])
    assert note["parts"] == 2
    assert note["part_windows"] == [[1.0, 2.0], [9.0, 12.0]]


def test_usage_note_is_pure():
    r = _response()
    assert V.usage_note(r, elapsed_sec=1.0, sample_fps=2.0, media_resolution=None,
                        parts=1) == V.usage_note(r, elapsed_sec=1.0, sample_fps=2.0,
                                                 media_resolution=None, parts=1)


def test_call_video_returns_usage_with_retries_and_model():
    g = _FakeGemini([_response('{"a": 1}')])
    data, usage = V.call_video(g, HANDLE, "프롬프트", sample_fps=2.0, log=lambda *a: None)
    assert data == {"a": 1}
    assert usage["retries"] == 0
    assert usage["model"] == "gemini-3.7-flash"
    assert usage["prompt"] == 1000 and usage["finish_reason"] == "STOP"


# ── ④ MAX_TOKENS 절단 ───────────────────────────────────────────────────────

def test_max_tokens_truncation_is_logged_loudly():
    """v3-M2 실사고 — 절단이 조용하면 '엉뚱한 JSONDecodeError' 로만 보인다."""
    lines: list[str] = []
    g = _FakeGemini([_response('{"a": 1}', finish="MAX_TOKENS")])
    _data, usage = V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lines.append)
    assert usage["finish_reason"] == "MAX_TOKENS"
    assert any("MAX_TOKENS" in l for l in lines), lines


def test_truncated_parse_failure_says_so_and_carries_usage():
    g = _FakeGemini([_response('{"a": 1', finish="MAX_TOKENS")])
    with pytest.raises(V.VideoParseError) as ei:
        V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert "MAX_TOKENS 절단" in str(ei.value)
    # 계약 §2 "audit 에 시도별 usage 를 전량" — 파싱 실패 시도가 토큰을 제일 많이 먹는다
    assert ei.value.usage["prompt"] == 1000
    assert ei.value.raw_text.startswith('{"a"')


def test_parse_error_is_a_valueerror():
    """v3 재질의 루프가 ValueError 를 반려 재료로 받는다 — 그 어휘를 그대로 쓴다."""
    assert issubclass(V.VideoParseError, ValueError)


# ── ⑤ 재시도 분류 (E11) ─────────────────────────────────────────────────────

def test_classify_error_follows_e11():
    assert V.classify_error(_ApiError(429)) == "transient"
    assert V.classify_error(_ApiError(500)) == "transient"
    assert V.classify_error(_ApiError(503)) == "transient"
    assert V.classify_error(_ApiError(400)) == "permanent"
    assert V.classify_error(_ApiError(401)) == "permanent"
    assert V.classify_error(_ApiError(404)) == "permanent"
    assert V.classify_error(ConnectionError("reset")) == "transient"   # OSError
    assert V.classify_error(TimeoutError("slow")) == "transient"


def test_classify_error_reads_the_real_sdk_error_classes():
    """가짜가 아니라 **진짜** `google.genai.errors` 로 분류를 확인한다.

    `.code` 에 정수가 실린다는 것이 분류의 전제인데, 그것을 가짜로만 확인하면
    SDK 가 필드를 바꾸는 날 전 재시도가 조용히 permanent 가 된다."""
    from google.genai import errors

    quota = errors.ClientError(429, {"error": {"message": "quota"}})
    assert quota.code == 429
    assert V.classify_error(quota) == "transient"
    assert V.classify_error(errors.ServerError(503, {"error": {"message": "x"}})) \
        == "transient"
    assert V.classify_error(errors.ClientError(400, {"error": {"message": "bad"}})) \
        == "permanent"

    import httpx

    assert V.classify_error(httpx.ConnectError("no route")) == "transient"


def test_retry_budget_matches_the_e11_precedent():
    """E11 규약의 자는 `stt_elevenlabs` 가 들고 있다 — 두 값을 묶어 둔다(갈리면 실패)."""
    from app.modules import stt_elevenlabs

    assert V.MAX_RETRIES == stt_elevenlabs._EL_RETRIES == 2


def test_unclassifiable_error_is_permanent():
    """우리 쪽 버그를 transient 로 읽으면 비싼 호출을 세 번 태우고 같은 자리에서 죽는다."""
    assert V.classify_error(TypeError("우리 버그")) == "permanent"


def test_retries_on_429_then_succeeds(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(V.time, "sleep", slept.append)
    g = _FakeGemini([_ApiError(429), _ApiError(503), _response('{"ok": 1}')])
    data, usage = V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert data == {"ok": 1}
    assert usage["retries"] == 2
    assert len(g.models.calls) == 3
    assert slept == [2.0, 4.0]          # stt_elevenlabs 와 같은 백오프


def test_400_fails_immediately(monkeypatch):
    """조용히 다른 모델·설정으로 떨어지지 않는다. 400 을 세 번 보내면 요금만 세 배다."""
    monkeypatch.setattr(V.time, "sleep", lambda *_a: pytest.fail("400 은 재시도 금지"))
    g = _FakeGemini([_ApiError(400, "INVALID_ARGUMENT")])
    with pytest.raises(V.VideoCallError) as ei:
        V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert ei.value.kind == "permanent"
    assert len(g.models.calls) == 1


def test_transient_budget_is_exhausted_loudly(monkeypatch):
    monkeypatch.setattr(V.time, "sleep", lambda *_a: None)
    g = _FakeGemini([_ApiError(500), _ApiError(500), _ApiError(500)])
    with pytest.raises(V.VideoCallError) as ei:
        V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert ei.value.kind == "transient"
    assert len(g.models.calls) == V.MAX_RETRIES + 1 == 3


def test_contract_violations_are_not_retried(monkeypatch):
    """파트·설정 조립은 재시도 루프 **밖**이다 — 계약 위반은 재시도로 안 나아진다."""
    monkeypatch.setattr(V.time, "sleep", lambda *_a: pytest.fail("재시도 금지"))
    g = _FakeGemini([_response()])
    with pytest.raises(ValueError):
        V.call_video(g, HANDLE, "p", sample_fps=99.0, log=lambda *a: None)
    with pytest.raises(ValueError):
        V.call_video(g, HANDLE, "p", sample_fps=1.0, thinking_level="turbo",
                     log=lambda *a: None)
    with pytest.raises(ValueError):
        V.call_video(g, HANDLE, "p", sample_fps=1.0, media_resolution="ULTRA",
                     log=lambda *a: None)
    with pytest.raises(ValueError):
        V.call_video(g, HANDLE, "", sample_fps=1.0, log=lambda *a: None)
    assert g.models.calls == []


# ── ⑥ 파싱 폴백 (v3 가 쓰는 그 경로) ────────────────────────────────────────

def test_parses_markdown_fence():
    g = _FakeGemini([_response('```json\n{"a": 1}\n```')])
    data, _u = V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert data == {"a": 1}


def test_parses_concatenated_json_first_value():
    """2026-08-03 실측: 분석 22회 중 12회 파싱 실패를 이 폴백이 구제했다."""
    g = _FakeGemini([_response('{"a": 1}{"b": 2}')])
    data, _u = V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert data == {"a": 1}


def test_parses_top_level_list():
    """v3 는 폴백을 dict 로만 받았다 — v4 는 배열 계약도 받는다(의도적 확장)."""
    g = _FakeGemini([_response('[{"a": 1}][2]')])
    data, _u = V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert data == [{"a": 1}]


def test_unparseable_raises_with_head_of_text():
    g = _FakeGemini([_response("죄송합니다, JSON 을 못 만들었습니다")])
    with pytest.raises(V.VideoParseError) as ei:
        V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert "죄송합니다" in str(ei.value)


# ── ⑦ 요청 설정 ────────────────────────────────────────────────────────────

def test_request_defaults_match_v3():
    """temperature 0 · JSON mime · 65536 — v3 네 곳이 공유하던 값을 한 곳에 모은 것."""
    g = _FakeGemini([_response()])
    V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    cfg = g.models.calls[0]["config"]
    assert cfg.temperature == 0.0
    assert cfg.response_mime_type == "application/json"
    assert cfg.max_output_tokens == V.DEFAULT_MAX_OUTPUT_TOKENS == 65536
    assert cfg.media_resolution is None          # 미지정 = 실측상 LOW
    assert cfg.thinking_config is None
    assert cfg.http_options is None


def test_prompt_comes_after_the_video_parts():
    """v3 네 곳 모두 `contents=[part, prompt]` 다 — 같은 순서를 유지한다."""
    g = _FakeGemini([_response()])
    V.call_video(g, HANDLE, "프롬프트다", sample_fps=1.0,
                 clips=[Clip(0, 2), Clip(4, 6)], log=lambda *a: None)
    contents = g.models.calls[0]["contents"]
    assert len(contents) == 3 and contents[-1] == "프롬프트다"
    assert len(_video_parts(contents)) == 2


def test_timeout_is_milliseconds():
    """SDK 실측: HttpOptions.timeout 은 **밀리초**. 초로 주면 450초가 0.45초가 된다."""
    g = _FakeGemini([_response()])
    V.call_video(g, HANDLE, "p", sample_fps=1.0, timeout_sec=450.0,
                 log=lambda *a: None)
    assert g.models.calls[0]["config"].http_options.timeout == 450_000


def test_model_defaults_to_flash_slot_and_pro_is_explicit():
    """조용히 슬롯을 고르지 않는다 — Pro 는 부르는 쪽이 명시한다(CLAUDE.md 역할 표)."""
    g = _FakeGemini([_response(), _response()])
    V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert g.models.calls[0]["model"] == "gemini-3.7-flash"
    V.call_video(g, HANDLE, "p", sample_fps=1.0, model="gemini-3.7-pro",
                 log=lambda *a: None)
    assert g.models.calls[1]["model"] == "gemini-3.7-pro"


def test_media_resolution_and_thinking_are_passed_through():
    g = _FakeGemini([_response()])
    V.call_video(g, HANDLE, "p", sample_fps=1.0, media_resolution="HIGH",
                 thinking_level="high", max_output_tokens=1024, log=lambda *a: None)
    cfg = g.models.calls[0]["config"]
    assert cfg.media_resolution.name == "MEDIA_RESOLUTION_HIGH"
    # SDK 가 소문자 라벨을 ThinkingLevel enum 으로 강제 변환한다(2.22.0 실측) —
    # `GeminiConfig.analysis_thinking_level` 이 소문자라 v3 도 같은 길을 지난다.
    level = cfg.thinking_config.thinking_level
    assert str(getattr(level, "value", level)).lower() == "high"
    assert cfg.max_output_tokens == 1024


def test_thinking_level_whitelist_matches_the_sdk_enum():
    """모르는 값을 보내면 400 INVALID_ARGUMENT 다(P4 실런 결함 1). 어휘를 SDK 와 묶는다."""
    from google.genai import types

    sdk = {str(e.value).lower() for e in types.ThinkingLevel
           if "UNSPECIFIED" not in str(e.value)}
    assert set(V.THINKING_LEVELS) == sdk


# ── ⑧ 핸들 수명 ────────────────────────────────────────────────────────────

def test_call_video_never_deletes_the_handle():
    """🛑 6·6b·8·10a 가 같은 핸들을 공유한다 — 단계 안에서 지우면 뒷단계가 죽는다."""
    g = _FakeGemini([_response()])
    V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert g.files.deleted == []


def test_call_video_does_not_delete_even_when_it_fails(monkeypatch):
    monkeypatch.setattr(V.time, "sleep", lambda *_a: None)
    g = _FakeGemini([_ApiError(400)])
    with pytest.raises(V.VideoCallError):
        V.call_video(g, HANDLE, "p", sample_fps=1.0, log=lambda *a: None)
    assert g.files.deleted == []


def test_module_has_no_delete_wiring():
    """v3 는 네 곳이 `finally: files.delete` 를 한다 — v4 는 그 문장이 없어야 한다.

    문자열 가드인 이유: 실패 경로에서만 지우는 코드가 들어오면 위의 두 테스트로는
    안 잡힌다(그 경로를 테스트가 다 밟지 못한다)."""
    src = Path(V.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    body = code.split('"""', 2)[-1]        # 모듈 독스트링(설명에 그 낱말이 나온다) 제외
    assert "files.delete" not in body


# ── ⑨ 결정성 ───────────────────────────────────────────────────────────────

def test_part_assembly_is_deterministic():
    g1, g2 = _FakeGemini(), _FakeGemini()
    clips = [Clip(3.25, 9.5), Clip(100.0, 101.75)]
    a = [_meta(p) for p in V.build_video_parts(g1, HANDLE, clips, sample_fps=2.0)]
    b = [_meta(p) for p in V.build_video_parts(g2, HANDLE, clips, sample_fps=2.0)]
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_clip_is_a_frozen_value():
    c = Clip(1.0, 2.0)
    with pytest.raises(Exception):
        c.start_sec = 5.0           # type: ignore[misc]
    assert Clip(1.0, 2.0) == c and Clip().whole
