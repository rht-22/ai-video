"""V4-M5 §3 정밀 청취(10a) 회귀 가드 — `app/v4/detail.py`.

이 파일이 고정하는 것은 계약 `docs/v4/M5-interfaces.md` §3 과 §0 의 발견이다:

① **창** — 승인 조각의 합집합(겹침·인접 흡수)이고 180초를 넘으면 등분한다. 등분 경계는
   이어 붙으므로 span 이 두 창에 겹쳐 실리지 않는다(중점 반개구간 타일링).
② **좌표** — 첨부 파트의 0초는 **창 시작**이다. 프롬프트 span 표의 시각이 창 상대초라는
   것을 수계산으로 못박는다. 반대 방향 환산은 존재하지 않는다(모델은 시각을 안 낸다).
③ **v3 기계를 부른다** — 각색 임계는 `chunk_analyze.TRANSCRIPT_DIFF_MAX` **그 상수**여야
   하고, 그 판정이 실제로 돌아 감사에 남아야 한다(베낀 임계면 언젠가 한쪽만 고쳐진다).
④ **실패는 원판정 유지** — 그 창의 span 이 결과에서 빠지고 다른 창은 남는다. 전량 실패도
   예외를 올리지 않는다(10a 는 선택 단계다).
⑤ 🛑 **화자 산출이 `bridge` 가 기대하는 모양** — `build_span_index(detail=…)` 를 실제로
   불러 `speaker_colors` 가 색을 내는 것까지 확인한다. 이 단계가 화자의 유일한 원천이라
   모양이 어긋나면 자막이 조용히 전 줄 흰색이 된다(계약 §0).
⑥ **결정성** — 같은 입력이면 같은 산출(시간 측정값 제외).

🛑 네트워크는 쓰지 않는다(가짜 클라이언트). 실호출로만 알 수 있는 것 — 서버가 offset
파트를 창 그대로 보여 주는가 · 화자 배정 품질 — 은 이 파일의 범위 밖이고 모듈 독스트링이
'확인 못 함'으로 적어 두었다.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.modules.grid import schemas
from app.v3 import assemble, chunk_analyze
from app.v3.seq_analyze import MAX_REASKS
from app.v4 import bridge
from app.v4 import detail as D
from app.v4.video import Clip

DURATION = 600.0

LINE_A = "이건 정말 대단한 순간이었습니다"
LINE_B = "그래서 내가 그때 말했잖아"
LINE_C = "너 진짜 이럴 거야 지금부터"


# ── 격자 ────────────────────────────────────────────────────────────────────

def _span(sid, t_in, t_out, *, audio, text=""):
    return {"id": sid, "t_in": t_in, "t_out": t_out, "is_audio": audio,
            "time_authority": "stt" if audio else "scene", "text": text}


def _words(t0, t1, text, prob, n=3):
    step = (t1 - t0) / n
    return [{"t0": round(t0 + i * step, 3), "t1": round(t0 + (i + 1) * step, 3),
             "text": f"{text}{i}", "prob": prob} for i in range(n)]


def make_grid():
    spans = [
        _span("sp0000", 100.0, 104.0, audio=True, text=LINE_A),
        _span("sp0001", 104.0, 108.0, audio=True, text=LINE_B),
        _span("sp0002", 108.0, 112.0, audio=False),
        _span("sp0003", 300.0, 304.0, audio=True, text=LINE_C),
    ]
    words = (_words(100.0, 104.0, "a", 0.9) + _words(104.0, 108.0, "b", 0.8)
             + _words(300.0, 304.0, "c", 0.7))
    return {"source": {"duration_sec": DURATION},
            "scene_cuts": [100.0, 112.0, 300.0], "silence": [], "arousal": [],
            "words": words, "span_candidates": spans}


def seg(a, b):
    return {"start_sec": a, "end_sec": b}


# ── 가짜 클라이언트 ─────────────────────────────────────────────────────────
# `types` 는 **진짜 google-genai** 다(`tests/test_v4_flags.py` 와 같은 규율) — 가짜 타입으로
# 조립하면 offset 필드 이름·타입이 틀려도 테스트가 통과한다.

def _response(payload, *, finish="STOP", total=44000):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    meta = SimpleNamespace(prompt_token_count=44000, thoughts_token_count=200,
                           candidates_token_count=800,
                           cached_content_token_count=0, total_token_count=total)
    return SimpleNamespace(text=text, usage_metadata=meta,
                           model_version="gemini-3.7-pro-001",
                           candidates=[SimpleNamespace(
                               finish_reason=SimpleNamespace(name=finish))])


class _FakeModels:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.queue.pop(0) if self.queue else _response({"meanings": []})
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeGemini:
    def __init__(self, queue=()):
        from google.genai import types

        self.types = types
        self.models = _FakeModels(queue)
        self.client = SimpleNamespace(models=self.models,
                                      files=SimpleNamespace(delete=self._deleted))
        self.config = SimpleNamespace(flash_model_name="gemini-3.7-flash",
                                      model_name="gemini-3.7-pro",
                                      analysis_thinking_level="medium")
        self.deletes: list[str] = []

    def _deleted(self, *, name):
        self.deletes.append(name)


HANDLE = SimpleNamespace(uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
                         name="files/abc")


def _meaning(first, last, spans, *, content="누가 무엇을 하고 있다", importance=4,
             mood="긴장", characters=("갑",)):
    return {"first_span": first, "last_span": last, "content": content,
            "characters": list(characters), "importance": importance, "mood": mood,
            "spans": spans}


def _sp(sid, *, heard=None, speaker="갑", scene="화면 묘사", importance=3,
        characters=("갑",)):
    out = {"id": sid, "scene_script": scene, "characters": list(characters),
           "importance": importance}
    if heard is not None:
        out["heard"] = [{"speaker": speaker, "line": heard}]
    return out


def _ok_payload():
    """sp0000~sp0002 를 한 meaning 으로 묶은 정상 응답(전사와 같은 말을 들었다)."""
    return {"meanings": [_meaning("sp0000", "sp0002", [
        _sp("sp0000", heard=LINE_A, speaker="갑"),
        _sp("sp0001", heard=LINE_B, speaker="을"),
        _sp("sp0002"),
    ])]}


# ── ① 창 계산 ───────────────────────────────────────────────────────────────

def test_windows_absorb_touching_and_overlapping_segments():
    """두 승인 편이 붙어 있거나 겹치면 **하나의 창**이다 — 같은 구간을 두 번 들려주면
    요금이 두 배이고 두 번의 화자 배정이 갈릴 수 있다."""
    got = D.detail_windows([seg(100.0, 120.0), seg(120.0, 130.0),   # 맞닿음
                            seg(125.0, 128.0),                      # 포함(합집합)
                            seg(300.0, 310.0)])                     # 멀리 — 따로
    assert got == [Clip(100.0, 130.0), Clip(300.0, 310.0)]


def test_windows_do_not_shrink_on_containment():
    """포함 관계에서 창이 **줄지 않는다** — `flags.merge_contiguous_clips` 를 그대로
    썼다면 끝이 뒤 조각의 끝(128)으로 당겨져 2초가 사라졌다."""
    assert D.detail_windows([seg(100.0, 130.0), seg(125.0, 128.0)]) == [Clip(100.0, 130.0)]


def test_windows_are_sorted_and_input_order_does_not_matter():
    a = D.detail_windows([seg(300.0, 310.0), seg(100.0, 120.0)])
    b = D.detail_windows([seg(100.0, 120.0), seg(300.0, 310.0)])
    assert a == b == [Clip(100.0, 120.0), Clip(300.0, 310.0)]


def test_gap_bigger_than_tolerance_stays_two_windows():
    """0.5초 틈은 **진짜 컷**이다 — 창에 넣으면 승인 편에 없는 화면을 들려주게 된다."""
    assert len(D.detail_windows([seg(100.0, 120.0), seg(120.5, 130.0)])) == 2
    # 관용(0.05s) 안이면 하나다 — 부동소수 관용이라는 것을 값으로 못박는다.
    assert len(D.detail_windows([seg(100.0, 120.0), seg(120.04, 130.0)])) == 1


def test_long_window_is_split_evenly_not_greedily():
    """185초 → 92.5 + 92.5. 그리디(180 + 5)로 자르면 마지막이 슬리버가 되고, 더 심하면
    경계 벨트(MIN_CLIP_SEC)에 걸려 통째로 버려진다."""
    got = D.detail_windows([seg(0.0, 185.0)])
    assert got == [Clip(0.0, 92.5), Clip(92.5, 185.0)]
    assert all(w.end_sec - w.start_sec <= D.DETAIL_WINDOW_MAX_SEC for w in got)


def test_split_edges_chain_exactly_so_spans_belong_to_one_window():
    """등분 경계가 이어 붙는다 → 중점 반개구간 규칙으로 span 은 **정확히 한 창**에 든다."""
    windows = D.detail_windows([seg(0.0, 500.0)])
    assert [w.start_sec for w in windows][1:] == [w.end_sec for w in windows][:-1]
    grid = {"span_candidates": [_span("x", 124.9, 125.1, audio=True)]}
    hit = [i for i, w in enumerate(windows)
           if chunk_analyze.spans_for_chunk(grid, w.start_sec, w.end_sec)]
    assert len(hit) == 1


def test_window_max_sec_is_the_contract_value():
    assert D.DETAIL_WINDOW_MAX_SEC == 180.0
    assert D.DETAIL_SAMPLE_FPS == 3.0


def test_windows_reject_broken_segment_shapes():
    """별칭(start/end)·역전은 **크게 실패**한다 — 0.0 으로 떨어지면 엉뚱한 구간의 화자가
    승인 편의 자막색이 된다."""
    with pytest.raises(ValueError, match="숫자가 아니다"):
        D.detail_windows([{"start": 1.0, "end": 2.0}])
    with pytest.raises(ValueError, match="구간 역전"):
        D.detail_windows([seg(20.0, 10.0)])
    with pytest.raises(ValueError):
        D.detail_windows([seg(0.0, 10.0)], max_sec=0)


def test_no_segments_makes_no_windows():
    assert D.detail_windows([]) == []
    assert D.detail_windows(None) == []


# ── ② 좌표 환산 (수계산) ────────────────────────────────────────────────────

def test_window_offset_is_subtraction_from_window_start():
    """창 [100, 112] 의 파트에서 원본 104.0초는 **4.0초**다. 첨부 파트의 0초가 창 시작이다."""
    w = Clip(100.0, 112.0)
    assert D.window_offset_sec(104.0, w) == 4.0
    assert D.window_offset_sec(100.0, w) == 0.0
    assert D.window_offset_sec(112.0, w) == 12.0


def test_window_offset_rejects_outside_and_whole():
    w = Clip(100.0, 112.0)
    with pytest.raises(ValueError, match="창 밖"):
        D.window_offset_sec(99.0, w)
    with pytest.raises(ValueError, match="창 밖"):
        D.window_offset_sec(200.0, w)
    with pytest.raises(ValueError):
        D.window_offset_sec(1.0, Clip())


def test_span_table_times_are_window_relative_not_source_absolute():
    """v3 는 표에 원본 절대초를 적었다(응답에 시각이 없어 무해했다). v4 는 창 상대초다 —
    모델이 파트 안에서 span 을 찾는 것이 이 호출의 일이기 때문이다."""
    grid = make_grid()
    w = Clip(100.0, 112.0)
    spans = chunk_analyze.spans_for_chunk(grid, 100.0, 112.0)
    table = D.span_table(spans, w)
    assert table.splitlines()[0].startswith(
        f"sp0000 | {schemas.format_ts(0.0)}~{schemas.format_ts(4.0)} | 유성 | {LINE_A}")
    assert "sp0001 | 00:00:04.000~00:00:08.000 | 유성 | " + LINE_B in table
    assert "sp0002 | 00:00:08.000~00:00:12.000 | 무성 | —" in table
    # 원본 절대초(00:01:40.000 = 100초)는 어디에도 없다.
    assert schemas.format_ts(100.0) not in table


def test_prompt_names_every_key_the_v3_validator_requires():
    """프롬프트만 고치고 검증기를 안 고치면 매 창 반려당한다(E17-1 판례). 출력 스키마가
    `validate_stage2_response` 가 읽는 열쇠와 같은지 문자열로 묶는다."""
    grid = make_grid()
    prompt = D.build_detail_prompt(Clip(100.0, 112.0),
                                   chunk_analyze.spans_for_chunk(grid, 100.0, 112.0))
    for key in ("meanings", "first_span", "last_span", "content", "characters",
                "importance", "mood", "spans", "scene_script", "heard", "speaker", "line"):
        assert f'"{key}"' in prompt, key
    # 좌표 안내(v4 가 더한 문장)와 화자 일관 지시가 프롬프트에 있어야 한다.
    assert "처음이 0초" in prompt
    assert "같은 사람에게 늘 같은 이름" in prompt


def test_prompt_research_block_is_absent_when_no_material():
    """재료가 없으면 블록 자체가 없다 — 빈 제목만 얹혀 프롬프트 지문이 흔들리면 안 된다."""
    grid = make_grid()
    spans = chunk_analyze.spans_for_chunk(grid, 100.0, 112.0)
    assert "## 작품 배경" not in D.build_detail_prompt(Clip(100.0, 112.0), spans)
    with_ctx = D.build_detail_prompt(Clip(100.0, 112.0), spans,
                                     character_names=["강비호"], research_context="배경")
    assert "## 작품 배경" in with_ctx and "강비호" in with_ctx


# ── ③ v3 기계를 부른다 ──────────────────────────────────────────────────────

def test_transcript_diff_threshold_is_the_v3_constant_object():
    """임계를 베끼지 않았다 — 같은 상수를 부른다(0.35 를 두 곳에 적으면 언젠가 갈린다)."""
    assert D.TRANSCRIPT_DIFF_MAX is chunk_analyze.TRANSCRIPT_DIFF_MAX
    assert D.TRANSCRIPT_DIFF_MAX == 0.35
    assert D.MAX_REASKS is MAX_REASKS


def test_adaptation_beyond_threshold_is_restored_to_transcript():
    """모델이 각색해 오면(편집거리 > 0.35) **전사로 복원**되고 그 사실이 감사에 남는다."""
    grid = make_grid()
    payload = {"meanings": [_meaning("sp0000", "sp0002", [
        _sp("sp0000", heard="완전히 다른 말을 지어냈습니다 정말로", speaker="갑"),
        _sp("sp0001", heard=LINE_B, speaker="을"),
        _sp("sp0002"),
    ])]}
    gem = _FakeGemini([_response(payload)])
    det, audit = run(gem, grid, [Clip(100.0, 112.0)])

    assert chunk_analyze.edit_ratio("완전히 다른 말을 지어냈습니다 정말로",
                                    LINE_A) > D.TRANSCRIPT_DIFF_MAX
    assert det["sp0000"]["audio_script"] == [{"speaker": "갑", "line": LINE_A}]
    guard = audit["windows"][0]["transcript_guard"]
    assert guard["restored"] == 1
    assert guard["picked"]["transcript"] == 2


def test_time_alignment_belt_runs_and_is_all_from_grid():
    grid = make_grid()
    gem = _FakeGemini([_response(_ok_payload())])
    _det, audit = run(gem, grid, [Clip(100.0, 112.0)])
    assert audit["windows"][0]["time_alignment"]["pct"] == 100.0
    assert audit["windows"][0]["time_alignment"]["violations"] == []


def test_reask_loop_uses_v3_validator_and_recovers():
    """1차는 span 빈틈(계약 위반) → 반려 재질의 → 2차 통과. 반려 사유가 다음 프롬프트에
    실린다(검증기만 고치면 모델은 계속 같은 응답을 낸다)."""
    grid = make_grid()
    bad = {"meanings": [_meaning("sp0000", "sp0000", [_sp("sp0000", heard=LINE_A)])]}
    gem = _FakeGemini([_response(bad), _response(_ok_payload())])
    det, audit = run(gem, grid, [Clip(100.0, 112.0)])

    row = audit["windows"][0]
    assert row["status"] == D.STATUS_OK
    assert len(row["attempts"]) == 2
    assert row["attempts"][0]["problems"]
    second_prompt = [c for c in gem.models.calls[1]["contents"] if isinstance(c, str)][-1]
    assert "직전 제안 반려 사유" in second_prompt
    assert set(det) == {"sp0000", "sp0001", "sp0002"}


def test_pro_slot_and_offset_part_and_fps(monkeypatch):
    """영상을 실제로 보는 호출이라 **Pro 슬롯**이고, 붙는 파트는 그 창의 offset 하나다."""
    grid = make_grid()
    gem = _FakeGemini([_response(_ok_payload())])
    run(gem, grid, [Clip(100.0, 112.0)])
    call = gem.models.calls[0]
    assert call["model"] == "gemini-3.7-pro"
    parts = [c for c in call["contents"] if not isinstance(c, str)]
    assert len(parts) == 1
    meta = parts[0].video_metadata
    assert meta.fps == D.DETAIL_SAMPLE_FPS
    assert (meta.start_offset, meta.end_offset) == ("100.000s", "112.000s")
    assert call["config"].max_output_tokens == D.DETAIL_MAX_OUTPUT_TOKENS
    assert call["config"].temperature == 0.0


def test_handle_is_not_deleted():
    """6·6b·8·10a 가 같은 핸들을 공유한다 — 단계 안에서 지우면 뒷단계가 죽은 핸들을 쓴다."""
    grid = make_grid()
    gem = _FakeGemini([_response(_ok_payload())])
    run(gem, grid, [Clip(100.0, 112.0)])
    assert gem.deletes == []


# ── ④ 실패는 원판정 유지 ────────────────────────────────────────────────────

def run(gem, grid, windows, **kw):
    return D.run_detail(gem, HANDLE, windows=windows, grid=grid, log=lambda *a, **k: None,
                        **kw)


class _ApiError(Exception):
    def __init__(self, code: int, message: str = "boom"):
        super().__init__(f"{code} {message}")
        self.code = code


def test_failed_window_keeps_original_judgment_and_others_survive():
    """창 하나가 죽어도 다른 창은 남는다. 죽은 창의 span 은 결과에서 **빠질 뿐**이고,
    `bridge` 가 기본값(전사 채택·화자 없음)으로 채운다."""
    grid = make_grid()
    second = {"meanings": [_meaning("sp0003", "sp0003",
                                    [_sp("sp0003", heard=LINE_C, speaker="병")])]}
    gem = _FakeGemini([_ApiError(400, "bad request"), _response(second)])
    det, audit = run(gem, grid, [Clip(100.0, 112.0), Clip(300.0, 304.0)])

    assert set(det) == {"sp0003"}                      # 실패한 창의 span 은 없다
    assert audit["windows"][0]["status"] == "failed"
    assert audit["windows"][0]["reason"] == D.REASON_CALL_FAILED
    assert audit["windows"][1]["status"] == D.STATUS_OK
    assert audit["ok"] == 1 and audit["failed"] == 1

    span_index, _order = bridge.build_span_index(grid, detail=det)
    assert span_index["sp0000"]["importance"] == bridge.DEFAULT_IMPORTANCE
    assert span_index["sp0000"]["audio_script"] == []
    assert span_index["sp0000"]["text_source"] == "transcript"   # 원판정 유지
    assert span_index["sp0003"]["audio_script"] == [{"speaker": "병", "line": LINE_C}]


def test_total_failure_does_not_raise():
    """10a 는 선택 단계다 — 전량 실패해도 편을 죽이지 않는다(6단계와 다른 자리)."""
    grid = make_grid()
    gem = _FakeGemini([_response("이건 JSON 이 아니다")] * (1 + MAX_REASKS))
    det, audit = run(gem, grid, [Clip(100.0, 112.0)])
    assert det == {}
    assert audit["windows"][0]["reason"] == D.REASON_PARSE_FAILED
    assert len(audit["windows"][0]["attempts"]) == 1 + MAX_REASKS
    assert audit["failed"] == 1
    assert "warning" in audit           # 화자를 못 얻었다는 사실이 소리를 낸다


def test_parse_failure_is_reask_material_not_a_stop():
    """v3-M2 판례: 1차 MAX_TOKENS 절단 → JSON 파싱 실패 → **2차에서 자연 회복**했다.
    여기서 멈추면 그 회복 경로가 사라진다."""
    grid = make_grid()
    gem = _FakeGemini([_response("절단된 {\"mean"), _response(_ok_payload())])
    det, audit = run(gem, grid, [Clip(100.0, 112.0)])
    assert audit["windows"][0]["status"] == D.STATUS_OK
    assert len(audit["windows"][0]["attempts"]) == 2
    assert set(det) == {"sp0000", "sp0001", "sp0002"}


def test_call_failure_is_not_reasked():
    """E11 재시도는 `call_video` 안에서 끝났다 — permanent 4xx 를 되물으면 요금만 두 배다."""
    grid = make_grid()
    gem = _FakeGemini([_ApiError(400, "bad request")])
    _det, audit = run(gem, grid, [Clip(100.0, 112.0)])
    assert audit["windows"][0]["reason"] == D.REASON_CALL_FAILED
    assert len(audit["windows"][0]["attempts"]) == 1
    assert len(gem.models.calls) == 1


def test_reask_exhausted_is_recorded_as_rejected():
    grid = make_grid()
    bad = {"meanings": [_meaning("sp0000", "sp0000", [_sp("sp0000", heard=LINE_A)])]}
    gem = _FakeGemini([_response(bad)] * (1 + MAX_REASKS))
    det, audit = run(gem, grid, [Clip(100.0, 112.0)])
    assert det == {}
    row = audit["windows"][0]
    assert row["reason"] == D.REASON_REJECTED
    assert len(row["attempts"]) == 1 + MAX_REASKS


def test_window_outside_source_is_dropped_by_the_belt():
    """`endOffset` 은 소스를 넘어도 **조용히 클램프**된다 — 보내기 전에 자른다."""
    grid = make_grid()
    gem = _FakeGemini([])
    det, audit = run(gem, grid, [Clip(700.0, 710.0)])
    assert det == {} and gem.models.calls == []
    assert audit["windows"][0]["reason"] == D.REASON_BELT_DROPPED


def test_window_without_spans_is_skipped_without_a_call():
    grid = make_grid()
    gem = _FakeGemini([])
    det, audit = run(gem, grid, [Clip(200.0, 210.0)])
    assert det == {} and gem.models.calls == []
    assert audit["windows"][0]["reason"] == D.REASON_NO_SPANS


def test_reason_vocabulary_is_pinned():
    """`checkpoint_winner_detail.json`·run_log 에 그대로 실리는 문자열이다."""
    assert (D.REASON_NO_SPANS, D.REASON_BELT_DROPPED, D.REASON_CALL_FAILED,
            D.REASON_PARSE_FAILED, D.REASON_REJECTED, D.STATUS_OK) == (
        "no_spans", "belt_dropped", "call_failed", "parse_failed", "rejected", "ok")


def test_our_own_defects_are_not_swallowed():
    """검증기가 보장한 값이 아닌 것은 **올린다** — 배선 사고를 '모델 실패'로 적으면
    원인이 감사에 남지 않는다."""
    grid = make_grid()
    spans = chunk_analyze.spans_for_chunk(grid, 100.0, 112.0)
    norm = [{"first_idx": 0, "last_idx": 0, "content": "x", "characters": [],
             "importance": 3, "mood": "",
             "spans": [{"span_id": "sp0001", "scene_script": "", "characters": [],
                        "importance": 3, "audio": []}]}]
    with pytest.raises(ValueError, match="span 정렬이 어긋났다"):
        D.detail_nodes(norm, spans)
    norm[0]["spans"][0]["span_id"] = "sp0000"
    norm[0]["spans"][0]["importance"] = 9
    with pytest.raises(ValueError, match="importance"):
        D.detail_nodes(norm, spans)


def test_overlapping_windows_fail_loudly():
    """창은 합집합·등분으로 만들어 겹치지 않는다. 겹쳤다면 창 계산이 틀린 것이고, 나중 값이
    조용히 이기면 어느 창의 화자인지 알 수 없게 된다."""
    grid = make_grid()
    gem = _FakeGemini([_response(_ok_payload()), _response(_ok_payload())])
    with pytest.raises(ValueError, match="다른 창과 span 이 겹친다"):
        run(gem, grid, [Clip(100.0, 112.0), Clip(100.0, 112.0)])


def test_grid_without_duration_is_a_wiring_error():
    with pytest.raises(ValueError, match="소스 길이"):
        run(_FakeGemini([]), {"span_candidates": []}, [Clip(0.0, 1.0)])


# ── ⑤ 화자 산출이 bridge 가 기대하는 모양 ───────────────────────────────────

def test_detail_nodes_feed_bridge_and_produce_speaker_colors():
    """🛑 계약 §0 의 발견 — 이 단계가 화자의 유일한 원천이다. `bridge` 를 실제로 태워
    `assemble.speaker_colors` 가 색을 내는 것까지 확인한다(모양이 어긋나면 전 줄 흰색)."""
    grid = make_grid()
    gem = _FakeGemini([_response(_ok_payload())])
    det, audit = run(gem, grid, [Clip(100.0, 112.0)])

    assert det["sp0000"]["audio_script"] == [{"speaker": "갑", "line": LINE_A}]
    assert det["sp0002"]["audio_script"] == []          # 무성 span 은 빈 목록이 정상
    assert audit["speakers"] == ["갑", "을"]

    span_index, _order = bridge.build_span_index(grid, detail=det)
    colors = assemble.speaker_colors(span_index)
    assert set(colors) == {"갑", "을"}
    index_audit = bridge.index_audit(span_index, detail_spans=len(det))
    assert index_audit["speaker_source"] == "detail"
    assert "warning" not in index_audit
    assert index_audit["importance_source"] == bridge.IMPORTANCE_SOURCE_DETAIL


def test_node_keys_are_exactly_what_bridge_reads_plus_characters():
    grid = make_grid()
    gem = _FakeGemini([_response(_ok_payload())])
    det, _audit = run(gem, grid, [Clip(100.0, 112.0)])
    assert set(det["sp0000"]) == {"audio_script", "text_source", "heard_text",
                                  "importance", "scene_script", "characters",
                                  "meaning_content", "mood"}
    # ⚠ `conf` 는 싣지 않는다 — `bridge` 가 격자 단어에서 자기가 잰다(출처 둘 금지).
    assert "conf" not in det["sp0000"]
    span_index, _order = bridge.build_span_index(grid, detail=det)
    assert span_index["sp0000"]["conf"] == pytest.approx(0.9)


def test_unknown_speaker_is_not_counted_as_a_speaker():
    """`speaker_colors` 가 색을 주지 않는 이름을 '화자를 얻었다'로 세면 계기판이 거짓말한다."""
    grid = make_grid()
    payload = {"meanings": [_meaning("sp0000", "sp0002", [
        _sp("sp0000", heard=LINE_A, speaker="미상"),
        _sp("sp0001", heard=LINE_B, speaker="미상"),
        _sp("sp0002"),
    ])]}
    gem = _FakeGemini([_response(payload)])
    det, audit = run(gem, grid, [Clip(100.0, 112.0)])
    assert audit["speakers"] == []
    assert "warning" in audit
    span_index, _order = bridge.build_span_index(grid, detail=det)
    assert assemble.speaker_colors(span_index) == {}


def test_broken_transcript_span_takes_the_heard_text():
    """전사가 기계적으로 깨진 자리만 청취로 바뀐다(M9-C) — 10a 의 나머지 절반이다."""
    grid = make_grid()
    # 반복 환각 서명: 같은 단어 6개 이상 · 길이 퇴화
    grid["span_candidates"][0]["text"] = "육십 육십 육십 육십 육십 육십 육십"
    grid["words"] = [{"t0": 100.0 + i * 0.1, "t1": 100.0 + i * 0.1, "text": "육십",
                      "prob": 0.2} for i in range(8)] + grid["words"][3:]
    payload = {"meanings": [_meaning("sp0000", "sp0002", [
        _sp("sp0000", heard="사실은 이렇게 말했다", speaker="갑"),
        _sp("sp0001", heard=LINE_B, speaker="을"),
        _sp("sp0002"),
    ])]}
    gem = _FakeGemini([_response(payload)])
    det, audit = run(gem, grid, [Clip(100.0, 112.0)])
    assert det["sp0000"]["text_source"] == "heard"
    assert det["sp0000"]["heard_text"] == "사실은 이렇게 말했다"
    assert audit["text_source"]["heard"] == 1


# ── ⑥ 결정성 ────────────────────────────────────────────────────────────────

def _stable(audit):
    """시간 측정값만 걷어낸 감사 — 나머지는 같은 입력이면 바이트까지 같아야 한다."""
    out = json.loads(json.dumps(audit, ensure_ascii=False, sort_keys=True))
    for row in out.get("windows") or []:
        for rec in row.get("attempts") or []:
            rec.pop("elapsed_sec", None)
            if isinstance(rec.get("usage"), dict):
                rec["usage"].pop("elapsed_sec", None)
    return out


def test_same_input_same_output():
    grid = make_grid()
    windows = D.detail_windows([seg(100.0, 112.0), seg(300.0, 304.0)])
    payload2 = {"meanings": [_meaning("sp0003", "sp0003",
                                      [_sp("sp0003", heard=LINE_C, speaker="병")])]}

    runs = []
    for _ in range(2):
        gem = _FakeGemini([_response(_ok_payload()), _response(payload2)])
        det, audit = run(gem, grid, list(windows))
        runs.append((json.dumps(det, ensure_ascii=False, sort_keys=True), _stable(audit)))
    assert runs[0][0] == runs[1][0]
    assert runs[0][1] == runs[1][1]


def test_pure_functions_do_not_mutate_their_input():
    grid = make_grid()
    before = json.dumps(grid, ensure_ascii=False, sort_keys=True)
    segments = [seg(100.0, 112.0)]
    D.detail_windows(segments)
    assert segments == [seg(100.0, 112.0)]
    gem = _FakeGemini([_response(_ok_payload())])
    run(gem, grid, [Clip(100.0, 112.0)])
    # ⚠ `adjudicate_transcript` 는 넘겨받은 norm 을 제자리에서 고치지만 그것은 우리가 만든
    # 사본이다 — **격자**는 어느 경로에서도 변하지 않아야 한다.
    assert json.dumps(grid, ensure_ascii=False, sort_keys=True) == before
