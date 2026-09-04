"""V4-M3 §4 시각 사고 플래그 회귀 가드 — `app/v4/flags.py`.

이 파일이 고정하는 것은 계약 `docs/v4/M3-interfaces.md` §4 의 문장들이다:

① **이음새 시각은 편집본 좌표다**(조각 길이의 누적합) — 원본 절대초가 아니다. 모델이
   보는 것이 이어 붙인 영상이기 때문이다. 수계산으로 못박는다.
② **점수 금지가 프롬프트와 검증기 양쪽에 있다**(M9 원칙). 한쪽만 있으면 매 편 반려당하거나
   (검증기만) 모델이 계속 점수를 낸다(프롬프트만) — E17-1 판례.
③ **실패는 미채점** — `{status:"failed", …}`. 어휘는 9단계 `approve` 것을 **그대로** 쓴다.
   다른 어휘를 쓰면 멀쩡한 채점이 미채점이 된다.
④ **병렬 결과는 후보 id 정렬 순서**(결정성) · 예산 카운터는 Lock 안 원자적 연산.
⑤ **파트 상한** — 인접(소스 연속) 조각 병합, 그래도 넘치면 미채점 + 사유(조용한 절단 금지).

🛑 네트워크는 쓰지 않는다(가짜 클라이언트). 실호출로만 알 수 있는 것 — 서버가 파트를
첨부 순서대로 이어 붙여 보는가 · 프롬프트 판정 품질(κ) — 은 이 파일의 범위 밖이고
모듈 독스트링이 '확인 못 함'으로 적어 두었다.
"""
from __future__ import annotations

import json
import time
import threading
from types import SimpleNamespace

import pytest

from app.v4 import approve as A
from app.v4 import flags as F
from app.v4 import fps as fps_mod
from app.v4.video import Clip


# ── 가짜 클라이언트 ─────────────────────────────────────────────────────────
# `types` 는 **진짜 google-genai** 다(`tests/test_v4_video.py` 와 같은 규율) — 가짜 타입으로
# 조립하면 offset 필드 이름·타입이 틀려도 테스트가 통과한다.

def _response(payload, *, finish: str = "STOP", total: int | None = 21000):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    meta = SimpleNamespace(prompt_token_count=20800, thoughts_token_count=100,
                           candidates_token_count=100,
                           cached_content_token_count=0, total_token_count=total)
    return SimpleNamespace(text=text, usage_metadata=meta,
                           model_version="gemini-3.7-flash-001",
                           candidates=[SimpleNamespace(
                               finish_reason=SimpleNamespace(name=finish))])


class _FakeModels:
    """후보별 응답을 프롬프트 안의 표식이 아니라 **호출 순서**가 아닌 큐로 준다 —
    병렬이라 호출 순서는 정해지지 않는다. 그래서 `by_marker` 로 프롬프트를 보고 고른다."""

    def __init__(self, by_marker=None, default=None, delay_sec: float = 0.0):
        self.by_marker = dict(by_marker or {})
        self.default = default
        self.calls: list[dict] = []
        self.delay_sec = delay_sec
        self._lock = threading.Lock()
        self.max_in_flight = 0
        self._in_flight = 0

    def generate_content(self, *, model, contents, config):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.calls.append({"model": model, "contents": contents, "config": config})
        try:
            if self.delay_sec:
                time.sleep(self.delay_sec)
            prompt = [c for c in contents if isinstance(c, str)][-1]
            item = self.default
            for marker, resp in self.by_marker.items():
                if marker in prompt:
                    item = resp
                    break
            if item is None:
                item = _response({"seam_jump": False, "hook_weak": False,
                                  "evidence_sec": []})
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            with self._lock:
                self._in_flight -= 1


class _FakeGemini:
    def __init__(self, by_marker=None, default=None):
        from google.genai import types

        self.types = types
        self.models = _FakeModels(by_marker, default)
        self.client = SimpleNamespace(models=self.models,
                                      files=SimpleNamespace(delete=self._deleted))
        self.config = SimpleNamespace(flash_model_name="gemini-3.7-flash",
                                      model_name="gemini-3.7-pro")
        self.deletes: list[str] = []

    def _deleted(self, *, name):
        self.deletes.append(name)


HANDLE = SimpleNamespace(uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
                         name="files/abc")


class _ApiError(Exception):
    """google-genai `APIError` 흉내 — `.code` 에 정수 상태(SDK 실측)."""

    def __init__(self, code: int, message: str = "boom"):
        super().__init__(f"{code} {message}")
        self.code = code


def _cand(cid: str, segs, **extra):
    return {"id": cid,
            "segments": [{"start_sec": a, "end_sec": b} for a, b in segs],
            **extra}


def _ok(seam=False, hook=False, evidence=()):
    return _response({"seam_jump": seam, "hook_weak": hook,
                      "evidence_sec": list(evidence)})


# ── ① 이음새 시각 = 편집본 좌표 (수계산) ────────────────────────────────────

def test_seam_times_are_edited_coordinates_not_source():
    """조각 [100,120]·[300,315]·[900,905] → 길이 20·15·5.

    이음새는 **누적합** 20.0 · 35.0 이다. 원본 절대초(300·900)가 아니다 — 모델이 보는
    영상에는 300초라는 시각이 없다."""
    clips = [Clip(100.0, 120.0), Clip(300.0, 315.0), Clip(900.0, 905.0)]
    assert F.edited_seam_times(clips) == [20.0, 35.0]
    assert F.edited_total_sec(clips) == 40.0


def test_single_clip_has_no_seam():
    assert F.edited_seam_times([Clip(10.0, 70.0)]) == []


def test_seam_times_reject_whole_attachment():
    """전체 첨부는 길이를 모른다 — 판정하지 않고 크게 실패한다(추측 금지)."""
    with pytest.raises(ValueError, match="전체 첨부"):
        F.edited_seam_times([Clip(), Clip(10.0, 20.0)])


def test_prompt_carries_edited_seam_times():
    """프롬프트에 실린 시각이 편집본 좌표이고, 원본 절대초는 어디에도 없다."""
    cand = _cand("c01", [(100.0, 120.0), (300.0, 315.0)])
    clips = F.candidate_clips(cand)
    seams = F.edited_seam_times(clips)
    prompt = F.build_flags_prompt(cand, seam_times=seams,
                                  total_sec=F.edited_total_sec(clips))
    assert "20.0s" in prompt
    assert "300" not in prompt and "100.0s" not in prompt
    # 좌표계를 말로도 못박는다 — 숫자만 주면 모델이 원본 시각으로 읽을 수 있다.
    assert "이어 붙인 영상의 처음(0초)부터" in prompt


def test_prompt_states_total_of_attached_parts_not_original_segments():
    """벨트·병합으로 붙는 길이가 달라지면 프롬프트의 총 길이도 그것을 따른다."""
    cand = _cand("c01", [(10.0, 40.0), (50.0, 999.0)])   # 뒤 조각이 소스 밖으로 넘친다
    clips, note = F.plan_clips(cand, source_duration_sec=100.0)
    prompt = F.build_flags_prompt(cand, seam_times=note["seam_sec"],
                                  total_sec=note["duration_sec"])
    assert note["duration_sec"] == 80.0        # 30 + (100-50)
    assert "총 80.0초" in prompt


# ── ② 프롬프트가 점수를 금지한다 (M9) ───────────────────────────────────────

def test_prompt_forbids_scoring_and_reuses_qc_contract():
    """`finalize.py:636` QC 프롬프트와 **같은 계약 문장**을 쓴다."""
    prompt = F.build_flags_prompt(_cand("c01", [(0.0, 60.0)]), seam_times=[])
    assert "화면 사고만 찾아라 — 취향 평가 금지. 점수를 매기지 마라." in prompt
    assert "true/false 와 근거 시각(초)만 답하라" in prompt
    # 두 항목의 정의도 계약 문구 그대로다(기획서 §8단계 프롬프트 계약).
    assert "seam_jump : 조각 이음새에서 인물·장소가 설명 없이 바뀌는가" in prompt
    assert f"hook_weak : 첫 {F.HOOK_WINDOW_SEC:g}초 안에 사건" in prompt
    # 출력 규칙에서 한 번 더 — 프롬프트만 고치고 검증기를 안 고치면 매 편 반려당한다.
    assert "점수·정도·확신도·설명 열쇠를 넣으면 반려된다" in prompt


def test_prompt_does_not_leak_candidate_intent():
    """제목 가안·사유를 싣지 않는다 — 사람 말로 된 의도를 주면 화면 대신 그것을 채점한다."""
    cand = _cand("c01", [(0.0, 60.0)], title_draft="충격 반전",
                 reason="반전이 강한 아크", template="conflict_payoff")
    prompt = F.build_flags_prompt(cand, seam_times=[])
    assert "충격 반전" not in prompt
    assert "반전이 강한 아크" not in prompt
    assert "conflict_payoff" not in prompt


def test_prompt_is_deterministic():
    cand = _cand("c01", [(0.0, 30.0), (100.0, 130.0)])
    a = F.build_flags_prompt(cand, seam_times=[30.0], total_sec=60.0)
    b = F.build_flags_prompt(cand, seam_times=[30.0], total_sec=60.0)
    assert a == b


# ── ② 검증기가 점수를 반려한다 ──────────────────────────────────────────────

def test_valid_boolean_response_passes():
    got, problems = F.validate_flags_response(
        {"seam_jump": True, "hook_weak": False, "evidence_sec": [12.5, 31]})
    assert problems == []
    assert got == {"seam_jump": True, "hook_weak": False,
                   "evidence_sec": [12.5, 31.0]}


def test_evidence_may_be_absent():
    got, problems = F.validate_flags_response({"seam_jump": False, "hook_weak": False})
    assert problems == [] and got["evidence_sec"] == []


@pytest.mark.parametrize("value", [0.8, 1, 0, "true", "false", None, [True]])
def test_score_or_stringy_flag_is_rejected(value):
    """정도·점수·문자열 불리언은 전부 반려. `"false"` 를 False 로 읽으면 결함 편이
    조용히 나가고 True 로 읽으면 멀쩡한 편이 조용히 죽는다."""
    got, problems = F.validate_flags_response(
        {"seam_jump": value, "hook_weak": False, "evidence_sec": []})
    assert got is None
    assert any("점수를 매기지 마라" in p for p in problems)


def test_extra_score_key_is_rejected():
    """점수 열쇠를 하나 통과시키면 다음 판에 그것을 읽는 코드가 생긴다(M9 뒷문)."""
    got, problems = F.validate_flags_response(
        {"seam_jump": False, "hook_weak": False, "evidence_sec": [],
         "confidence": 0.92, "severity": "high"})
    assert got is None
    assert any("모르는 열쇠" in p and "confidence" in p and "severity" in p
               for p in problems)


def test_missing_flag_is_rejected():
    got, problems = F.validate_flags_response({"seam_jump": False})
    assert got is None
    assert any("hook_weak 가 없다" in p for p in problems)


def test_non_object_response_is_rejected():
    got, problems = F.validate_flags_response([{"seam_jump": False, "hook_weak": False}])
    assert got is None and problems


def test_evidence_must_be_numbers():
    got, problems = F.validate_flags_response(
        {"seam_jump": False, "hook_weak": False, "evidence_sec": ["12.5초"]})
    assert got is None
    assert any("근거 시각은" in p for p in problems)


def test_validate_is_pure():
    resp = {"seam_jump": True, "hook_weak": False, "evidence_sec": [3.0]}
    snapshot = json.dumps(resp, sort_keys=True)
    F.validate_flags_response(resp)
    assert json.dumps(resp, sort_keys=True) == snapshot


# ── ③ 실패 어휘가 approve 와 같다 ───────────────────────────────────────────

def test_status_vocabulary_is_imported_from_approve():
    """어휘를 새로 짓지 않는다 — 다른 값을 쓰면 멀쩡한 채점이 미채점이 된다."""
    assert F.FLAGS_STATUS_OK is A.FLAGS_STATUS_OK == "ok"
    assert F.FLAG_KEYS is A.FLAG_KEYS == ("seam_jump", "hook_weak")


def test_failed_entry_reads_as_unscored_in_approve():
    """8단계가 남긴 실패 기록을 9단계가 **미채점**으로 읽는다(0점이 아니다)."""
    g = _FakeGemini(default=_ApiError(400, "bad request"))
    out, audit = run_quiet(g, [_cand("c01", [(0.0, 60.0)])], source_duration_sec=600.0)
    assert out["c01"]["status"] == "failed"
    assert out["c01"]["reason"] == F.REASON_CALL_FAILED
    assert A.scored_flags(out, "c01") is None          # ← 9단계가 보는 그 함수
    approved, reasons = A.is_approved("c01", funnel_kept={"c01"},
                                      verify_ok={"c01"}, flags=out)
    assert approved is False and A.REASON_UNSCORED in reasons
    assert audit["scored"] == 0 and audit["unscored"] == 1


def test_ok_entry_reads_as_scored_in_approve():
    g = _FakeGemini(default=_ok())
    out, _audit = run_quiet(g, [_cand("c01", [(0.0, 60.0)])], source_duration_sec=600.0)
    assert out["c01"]["status"] == F.FLAGS_STATUS_OK
    assert A.scored_flags(out, "c01") == {"seam_jump": False, "hook_weak": False}
    approved, reasons = A.is_approved("c01", funnel_kept={"c01"},
                                      verify_ok={"c01"}, flags=out)
    assert approved is True and reasons == []


def test_invalid_response_is_unscored_not_false():
    """점수 응답은 '결함 없음'이 아니라 **모른다** 다."""
    g = _FakeGemini(default=_response({"seam_jump": 0.2, "hook_weak": 0.9}))
    out, _audit = run_quiet(g, [_cand("c01", [(0.0, 60.0)])], source_duration_sec=600.0)
    assert out["c01"]["reason"] == F.REASON_INVALID
    assert "점수를 매기지 마라" in out["c01"]["detail"]
    assert A.scored_flags(out, "c01") is None


def test_parse_failure_is_unscored():
    g = _FakeGemini(default=_response("이건 JSON 이 아니다"))
    out, _audit = run_quiet(g, [_cand("c01", [(0.0, 60.0)])], source_duration_sec=600.0)
    assert out["c01"]["reason"] == F.REASON_PARSE_FAILED
    assert A.scored_flags(out, "c01") is None


def test_one_failure_does_not_take_the_others():
    """후보 단위 증분 — 하나가 죽어도 나머지는 채점된다."""
    g = _FakeGemini(by_marker={"총 20.0초": _ApiError(400)},
                    default=_ok(seam=True, evidence=[7.0]))
    cands = [_cand("c01", [(0.0, 20.0)]), _cand("c02", [(0.0, 30.0)]),
             _cand("c03", [(0.0, 40.0)])]
    out, audit = run_quiet(g, cands, source_duration_sec=600.0)
    assert out["c01"]["status"] == "failed"
    assert out["c02"]["seam_jump"] is True and out["c03"]["seam_jump"] is True
    assert audit["scored"] == 2 and audit["unscored"] == 1


# ── ④ 병렬 · 결정성 · 예산 ──────────────────────────────────────────────────

def run_quiet(g, cands, **kw):
    return F.run_flags(g, HANDLE, cands, log=lambda *_a, **_k: None, **kw)


def test_results_are_in_sorted_candidate_id_order():
    """병렬이라 완료 순서는 정해지지 않는다 — 담는 순서는 **id 정렬**이다(결정성)."""
    g = _FakeGemini(default=_ok())
    cands = [_cand(cid, [(0.0, 10.0 + i)])
             for i, cid in enumerate(["c05", "c01", "c12", "c03"])]
    out, audit = run_quiet(g, cands, source_duration_sec=600.0)
    assert list(out) == ["c01", "c03", "c05", "c12"]
    assert [row["id"] for row in audit["candidates"]] == ["c01", "c03", "c05", "c12"]


def test_run_is_deterministic_across_runs():
    def once():
        # ⚠ 재시도가 도는 5xx 가 아니라 즉시 실패하는 4xx 를 쓴다 — E11 백오프(2s·4s)를
        # 세 판 태우면 이 테스트만 18초다(재시도 자체는 아래 attempts 테스트가 본다).
        g = _FakeGemini(by_marker={"총 20.0초": _ApiError(400)},
                        default=_ok(hook=True, evidence=[1.0]))
        out, audit = run_quiet(
            g, [_cand("c03", [(0.0, 20.0)]), _cand("c01", [(0.0, 30.0)]),
                _cand("c02", [(0.0, 40.0)])], source_duration_sec=600.0)
        return json.dumps(out, ensure_ascii=False, sort_keys=False), audit["scored"]

    first = once()
    assert first == once() == once()


def test_calls_are_parallel_up_to_concurrency():
    """동시 실행이 실제로 일어난다 — 가짜 클라이언트가 최대 동시 진입 수를 센다."""
    g = _FakeGemini(default=_ok())
    g.models = _FakeModels(default=_ok(), delay_sec=0.05)
    g.client = SimpleNamespace(models=g.models, files=SimpleNamespace(delete=None))
    cands = [_cand(f"c{i:02d}", [(0.0, 10.0 + i)]) for i in range(1, 9)]
    out, audit = run_quiet(g, cands, source_duration_sec=600.0, concurrency=4)
    assert len(out) == 8 and audit["calls"] == 8
    assert g.models.max_in_flight > 1                 # 순차가 아니다
    assert g.models.max_in_flight <= 4                # 상한을 넘지 않는다


def test_token_budget_check_and_increment_is_atomic():
    """`refine.py:359` 식 단순 int 는 병렬에서 샌다 — Lock 안 원자 연산이라 안 샌다.

    스레드 32개가 동시에 10짜리 자리를 잡으려 하면 예산 100 에서 **정확히 10개**만
    잡아야 한다."""
    budget = F.TokenBudget(100)
    got: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(32)

    def worker():
        barrier.wait()
        ok = budget.reserve(10)
        with lock:
            got.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(got) == 10
    assert budget.committed == 100


def test_budget_settles_actual_usage():
    """예약분을 풀고 실제 소비를 계상한다. 실제값을 모르면 예약분을 그대로 문다 —
    실패한 호출도 입력 토큰은 나갔다."""
    b = F.TokenBudget(1000)
    assert b.reserve(300) is True
    b.settle(300, 250)
    assert b.committed == 250
    assert b.reserve(300) is True
    b.settle(300, None)
    assert b.committed == 550


def test_budget_exhausted_is_unscored_not_rejected():
    """예산 소진은 **탈락이 아니라 미채점**이다(refine 의 '원판정 유지'와 같은 규율)."""
    g = _FakeGemini(default=_ok())
    cands = [_cand("c01", [(0.0, 60.0)]), _cand("c02", [(0.0, 60.0)])]
    # 한 콜 예상치보다 조금 큰 예산 — 첫 후보만 자리를 잡는다.
    one = fps_mod.usage_tokens(60.0, F.FLAG_SAMPLE_FPS)
    out, audit = run_quiet(g, cands, source_duration_sec=600.0,
                           budget_tokens=one + 10)
    assert out["c01"]["status"] == F.FLAGS_STATUS_OK
    assert out["c02"] == {"status": "failed", "reason": F.REASON_BUDGET,
                          "detail": out["c02"]["detail"], "attempts": 0}
    assert "예상" in out["c02"]["detail"]              # 조용한 드롭 금지
    assert audit["calls"] == 1
    assert A.scored_flags(out, "c02") is None


def test_budget_estimate_reuses_fps_module():
    """예상 토큰은 `fps.usage_tokens` 를 부른다 — 산식을 여기서 다시 적지 않는다."""
    assert F._estimate_tokens(60.0) == fps_mod.usage_tokens(60.0, F.FLAG_SAMPLE_FPS)


def test_budget_none_still_counts():
    """한도가 없어도 세기는 한다 — 다음 판의 예산(p99×2)이 이 숫자를 먹는다."""
    g = _FakeGemini(default=_ok())
    _out, audit = run_quiet(g, [_cand("c01", [(0.0, 60.0)])], source_duration_sec=600.0)
    assert audit["budget_tokens"] is None
    assert audit["budget_used"] == 21000        # 가짜 응답의 total_token_count


# ── ⑤ 파트 상한 · 인접 병합 ─────────────────────────────────────────────────

def test_contiguous_clips_are_merged():
    """`[10,20]`·`[20,30]` 은 이어 붙여도 컷이 아니다 — 거짓 이음새를 만들지 않는다."""
    merged, records = F.merge_contiguous_clips(
        [Clip(10.0, 20.0), Clip(20.0, 30.0), Clip(100.0, 110.0)])
    assert merged == [Clip(10.0, 30.0), Clip(100.0, 110.0)]
    assert len(records) == 1 and records[0]["action"] == "merged"


def test_merge_preserves_total_and_remaining_seams():
    """병합은 총 길이도 남은 이음새 시각도 바꾸지 않는다 — 경계 하나가 빠질 뿐이다."""
    raw = [Clip(10.0, 20.0), Clip(20.0, 30.0), Clip(100.0, 115.0)]
    merged, _ = F.merge_contiguous_clips(raw)
    assert F.edited_total_sec(raw) == F.edited_total_sec(merged) == 35.0
    assert F.edited_seam_times(raw) == [10.0, 20.0]
    assert F.edited_seam_times(merged) == [20.0]        # 거짓 이음새 10.0 이 사라졌다


def test_merge_does_not_join_across_a_real_cut():
    """0.5초 틈은 실제 컷이다 — 관용은 부동소수 수준이지 '가까우니 붙이자'가 아니다."""
    merged, records = F.merge_contiguous_clips([Clip(10.0, 20.0), Clip(20.5, 30.0)])
    assert len(merged) == 2 and records == []


def test_merge_absorbs_float_noise():
    merged, _ = F.merge_contiguous_clips([Clip(10.0, 20.0), Clip(20.02, 30.0)])
    assert merged == [Clip(10.0, 30.0)]


def test_part_limit_merges_then_gives_up_loudly():
    """병합으로 상한 안에 들면 채점하고, 그래도 넘치면 **미채점 + 사유**다.
    앞 몇 개만 보내고 '이음새 없음'을 받는 것이 가장 나쁘다(거짓 통과)."""
    # 연속 22조각 → 병합하면 1파트.
    contiguous = _cand("c01", [(float(i), float(i + 1)) for i in range(22)])
    clips, note = F.plan_clips(contiguous, source_duration_sec=600.0)
    assert len(clips) == 1 and "problem" not in note

    # 불연속 12조각 → 병합 불가 → 상한 10 초과.
    scattered = _cand("c02", [(i * 10.0, i * 10.0 + 3.0) for i in range(12)])
    _clips, note2 = F.plan_clips(scattered, source_duration_sec=600.0)
    assert note2["problem"][0] == F.REASON_PART_LIMIT
    assert "12개 > 상한 10" in note2["problem"][1]


def test_part_limit_candidate_is_unscored_without_a_call():
    g = _FakeGemini(default=_ok())
    scattered = _cand("c01", [(i * 10.0, i * 10.0 + 3.0) for i in range(12)])
    out, audit = run_quiet(g, [scattered], source_duration_sec=600.0)
    assert out["c01"]["reason"] == F.REASON_PART_LIMIT
    assert audit["calls"] == 0 and g.models.calls == []     # 돈을 쓰지 않는다
    assert A.scored_flags(out, "c01") is None


def test_part_limit_is_documented_ten():
    """문서 상한 10 vs 실측 25 통과(§10) — 미문서 동작에 기대지 않는다."""
    assert F.PART_LIMIT == 10


# ── 경계 벨트 · 배선 ────────────────────────────────────────────────────────

def test_boundary_belt_runs_before_the_call():
    """`endOffset` 은 소스를 넘어도 조용히 클램프된다 — **보내기 전에** 자른다."""
    g = _FakeGemini(default=_ok())
    cand = _cand("c01", [(10.0, 40.0), (90.0, 500.0)])
    out, audit = run_quiet(g, [cand], source_duration_sec=100.0)
    assert out["c01"]["status"] == F.FLAGS_STATUS_OK
    row = audit["candidates"][0]
    assert row["belt"][0]["action"] == "clamped"        # 조용히 넘기지 않는다
    parts = [p for p in g.models.calls[0]["contents"] if getattr(p, "file_data", None)]
    meta = [p.video_metadata.model_dump(exclude_none=True, by_alias=True) for p in parts]
    assert meta[1]["endOffset"] == "100.000s"


def test_all_clips_dropped_is_unscored():
    g = _FakeGemini(default=_ok())
    cand = _cand("c01", [(500.0, 560.0)])               # 통째로 소스 밖
    out, audit = run_quiet(g, [cand], source_duration_sec=100.0)
    assert out["c01"]["reason"] == F.REASON_NO_CLIPS
    assert audit["calls"] == 0


def test_attachment_order_is_the_edit_order():
    """첨부 순서 = 편집 순서(기획서 §2-B). 시각순으로 정렬하지 않는다."""
    g = _FakeGemini(default=_ok())
    cand = _cand("c01", [(300.0, 320.0), (10.0, 30.0)])   # 뒤 → 앞 편성
    run_quiet(g, [cand], source_duration_sec=600.0)
    parts = [p for p in g.models.calls[0]["contents"] if getattr(p, "file_data", None)]
    starts = [p.video_metadata.start_offset for p in parts]
    assert starts == ["300.000s", "10.000s"]


def test_call_uses_flash_slot_and_contract_settings():
    """Flash 슬롯 고정(기획서 §6) · fps 5 · media_resolution 미지정 · 출력 4096."""
    g = _FakeGemini(default=_ok())
    run_quiet(g, [_cand("c01", [(0.0, 60.0)])], source_duration_sec=600.0)
    call = g.models.calls[0]
    assert call["model"] == "gemini-3.7-flash"
    assert call["config"].max_output_tokens == F.FLAG_MAX_OUTPUT_TOKENS == 4096
    assert call["config"].media_resolution is None
    assert call["config"].temperature == 0.0
    parts = [p for p in call["contents"] if getattr(p, "file_data", None)]
    assert all(p.video_metadata.fps == F.FLAG_SAMPLE_FPS == 5.0 for p in parts)


def test_handle_is_not_deleted():
    """6·6b·8·10a 가 같은 핸들을 공유한다 — 여기서 지우면 뒷단계가 죽은 핸들을 쓴다."""
    g = _FakeGemini(default=_ok())
    run_quiet(g, [_cand("c01", [(0.0, 60.0)])], source_duration_sec=600.0)
    assert g.deletes == []


def test_missing_candidate_id_fails_loud():
    with pytest.raises(ValueError, match="id 가 없다"):
        F.candidate_clips({"segments": [{"start_sec": 0.0, "end_sec": 10.0}]})


def test_duplicate_candidate_ids_fail_loud():
    g = _FakeGemini(default=_ok())
    with pytest.raises(ValueError, match="중복"):
        run_quiet(g, [_cand("c01", [(0.0, 10.0)]), _cand("c01", [(0.0, 20.0)])],
                  source_duration_sec=600.0)


def test_segment_alias_keys_are_not_guessed():
    """`start`/`end` 별칭을 추측으로 받지 않는다 — 0.0 으로 떨어지면 모델에게 엉뚱한
    구간을 보여주고 승인 게이트가 그 판정을 믿는다."""
    with pytest.raises(ValueError, match="start_sec"):
        F.candidate_clips({"id": "c01", "segments": [{"start": 0.0, "end": 10.0}]})


def test_empty_candidate_list_is_harmless():
    g = _FakeGemini(default=_ok())
    out, audit = run_quiet(g, [], source_duration_sec=600.0)
    assert out == {} and audit["of"] == 0 and audit["calls"] == 0


def test_all_unscored_is_logged_loudly():
    """전량 미채점은 크게 남긴다 — 9단계가 `scoring_unavailable` 로 읽는다."""
    lines: list[str] = []
    g = _FakeGemini(default=_ApiError(400))
    F.run_flags(g, HANDLE, [_cand("c01", [(0.0, 60.0)])],
                source_duration_sec=600.0, log=lines.append)
    assert any("전량 미채점" in ln for ln in lines)


def test_plan_clips_is_pure():
    cand = _cand("c01", [(10.0, 40.0), (90.0, 500.0)])
    snapshot = json.dumps(cand, sort_keys=True)
    F.plan_clips(cand, source_duration_sec=100.0)
    assert json.dumps(cand, sort_keys=True) == snapshot


def test_transient_retry_is_counted_as_attempts_not_reasks():
    """`attempts` 는 E11 재시도 횟수다 — 8단계에는 재질의가 없다."""
    g = _FakeGemini()
    g.models = _FakeModels(default=_ok())
    calls = {"n": 0}
    real = g.models.generate_content

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ApiError(503, "unavailable")
        return real(**kw)

    g.models.generate_content = flaky
    g.client = SimpleNamespace(models=g.models, files=SimpleNamespace(delete=None))
    import app.v4.video as V

    orig_sleep = V.time.sleep
    V.time.sleep = lambda _s: None
    try:
        out, _audit = run_quiet(g, [_cand("c01", [(0.0, 60.0)])],
                                source_duration_sec=600.0)
    finally:
        V.time.sleep = orig_sleep
    assert out["c01"]["status"] == F.FLAGS_STATUS_OK
    assert out["c01"]["attempts"] == 2


def test_part_limit_covers_the_upstream_segment_cap():
    """6단계 `SEGMENTS_MAX` 와 이 파일의 `PART_LIMIT` 이 어긋나면 안 된다.

    상류 상한을 올리면(8 → 12) 정상 후보가 여기서 **미채점**이 된다 — 계약이 둘을
    "한 몸"이라고 적어 둔 이유다(`candidates.SEGMENTS_MAX` 주석). 값을 가져다 쓰지는
    않지만(각자 근거가 다르다) 부등식은 묶어 둔다."""
    candidates = pytest.importorskip(
        "app.v4.candidates", reason="6단계 모듈이 아직 이 워크트리에 없다")
    assert candidates.SEGMENTS_MAX <= F.PART_LIMIT
