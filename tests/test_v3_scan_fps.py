"""Stage 1 표본 fps 자동 결정 회귀 가드 (2026-09-01).

산식은 API 실측이다(count_tokens · 합성 영상): 토큰 = 재생초 × (fps × 71 + 32).
3시간 실물로 fps 0.85 실호출 성공 / fps 1.0 은 400(상한 초과)까지 확인했다.
이 테스트는 그 실측값과 **회귀 0 조건**(기존 길이 소재는 fps 1.0 그대로)을 못박는다.
"""
import json

import pytest

from app.v3 import schemas
from app.v3 import seq_analyze as s1


# ── 회귀 0 — 지금 도는 소재는 한 프레임도 안 바뀐다 ────────────────────────
@pytest.mark.parametrize("minutes", [1, 10, 45, 67, 90, 120, 150, 161])
def test_existing_lengths_keep_base_fps(minutes):
    fps, note = s1.resolve_scan_fps(minutes * 60)
    assert fps == s1.SCAN_SAMPLE_FPS == 1.0
    assert note["reason"] == "base"


def test_base_boundary_is_162_minutes():
    """161분까지 기본값, 162분부터 낮춘다 — 경계를 값으로 고정한다."""
    assert s1.resolve_scan_fps(161 * 60)[1]["reason"] == "base"
    assert s1.resolve_scan_fps(162 * 60)[1]["reason"] == "reduced_for_duration"
    assert s1.resolve_scan_fps(162 * 60)[0] < 1.0


# ── 실측 대조 ──────────────────────────────────────────────────────────────
def test_three_hours_resolves_to_measured_085():
    """3시간 → 0.85. 그 값은 실제 API 호출로 성공이 확인된 값이다."""
    fps, note = s1.resolve_scan_fps(3 * 3600)
    assert fps == 0.85
    assert note["reason"] == "reduced_for_duration"
    # 산식 997,380 · API count_tokens 실측 997,381 (반올림 차 1)
    assert abs(note["est_tokens"] - 997_381) <= 2


def test_formula_matches_api_measurement():
    """60초 보정본 실측(fps=1.0 → 6,181)과 산식이 맞는지."""
    assert abs(s1.scan_tokens(60, 1.0) - 6_181) <= 1
    assert abs(s1.scan_tokens(60, 6.0) - 27_481) <= 1   # 같은 실측의 다른 점
    assert abs(s1.scan_tokens(60, 0.25) - 2_986) <= 1


def test_fps_1_0_on_three_hours_would_exceed_limit():
    """자동 결정이 없으면 3시간은 상한을 넘는다 — 이 기능의 존재 이유."""
    assert s1.scan_tokens(3 * 3600, 1.0) > s1.SCAN_INPUT_LIMIT


# ── 예산·단조·결정성 ───────────────────────────────────────────────────────
@pytest.mark.parametrize("minutes", [30, 67, 120, 162, 170, 180, 200, 240, 246])
def test_result_always_fits_budget(minutes):
    fps, _ = s1.resolve_scan_fps(minutes * 60)
    budget = s1.SCAN_INPUT_LIMIT - s1.SCAN_PROMPT_RESERVE
    assert s1.scan_tokens(minutes * 60, fps) <= budget
    assert s1.scan_tokens(minutes * 60, fps) <= s1.SCAN_INPUT_LIMIT


def test_fps_is_monotonic_non_increasing_in_duration():
    prev = None
    for minutes in range(10, 247, 7):
        fps, _ = s1.resolve_scan_fps(minutes * 60)
        if prev is not None:
            assert fps <= prev, f"{minutes}분에서 fps 가 되레 올랐다"
        prev = fps


def test_deterministic_and_quantized():
    """같은 소재는 늘 같은 fps(결정성 조항) · 계단은 SCAN_FPS_QUANTUM."""
    for minutes in (163, 180, 200, 233):
        a = s1.resolve_scan_fps(minutes * 60)[0]
        b = s1.resolve_scan_fps(minutes * 60)[0]
        assert a == b
        q = round(a / s1.SCAN_FPS_QUANTUM)
        assert abs(a - q * s1.SCAN_FPS_QUANTUM) < 1e-9


# ── 하한과 fail-loud ───────────────────────────────────────────────────────
def test_floor_is_derived_from_snap_tolerance():
    """하한은 임의값이 아니라 스냅 관용에서 나온다(표본 간격 ≤ 관용)."""
    assert s1.SCAN_SAMPLE_FPS_MIN == 1.0 / schemas.SNAP_TOLERANCE_SEC == 0.5


def test_too_long_fails_loud_with_actionable_message():
    with pytest.raises(ValueError) as e:
        s1.resolve_scan_fps(5 * 3600)
    msg = str(e.value)
    assert "너무 길어" in msg
    assert "247" in msg or "4.11" in msg      # 하한에서의 최대 길이를 알려준다
    assert "스냅 관용" in msg      # 하한의 유래를 알려준다


def test_floor_boundary():
    """하한 직전은 통과, 직후는 실패."""
    assert s1.resolve_scan_fps(246 * 60)[0] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        s1.resolve_scan_fps(248 * 60)


def test_max_duration_helpers():
    assert s1.scan_max_duration_sec(1.0) / 60 == pytest.approx(162.0, abs=0.5)
    assert s1.scan_max_duration_sec(0.5) / 60 == pytest.approx(247.0, abs=0.5)
    assert s1.scan_max_duration_sec(0.85) / 60 == pytest.approx(180.1, abs=0.5)


# ── 오판 금지 · 불변식 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [0, -1, None])
def test_unknown_duration_keeps_base(bad):
    """길이를 모르면 판정하지 않는다 — 안전장치가 오작동해 멀쩡한 편을 막으면 안 된다."""
    fps, note = s1.resolve_scan_fps(bad if bad is not None else 0)
    assert fps == s1.SCAN_SAMPLE_FPS
    assert note["reason"] == "duration_unknown"


def test_sample_fps_cannot_exceed_proxy_file_fps():
    with pytest.raises(ValueError):
        s1.resolve_scan_fps(600, base_fps=s1.SCAN_PROXY_FILE_FPS + 1)


def test_pure_no_side_effects():
    """순수 함수 — 같은 인자를 두 번 불러도 상수가 안 변한다."""
    before = (s1.SCAN_SAMPLE_FPS, s1.SCAN_PROMPT_RESERVE, s1.SCAN_INPUT_LIMIT)
    s1.resolve_scan_fps(180 * 60)
    assert (s1.SCAN_SAMPLE_FPS, s1.SCAN_PROMPT_RESERVE, s1.SCAN_INPUT_LIMIT) == before


# ── 배선 — 정한 fps 가 실제 호출과 기록에 흐르는가 ─────────────────────────
def _boundaries(duration_sec, step=300.0):
    """격자 눈금 — chunk 경계가 여기에 스냅돼야 Stage 1 이 통과한다."""
    b = [0.0]
    while b[-1] + step < duration_sec:
        b.append(b[-1] + step)
    b.append(float(duration_sec))
    return b


def _grid(duration_sec):
    """실제 grid 모양의 최소본 — span 을 300s 눈금으로 깔아 스냅이 성립하게 한다."""
    bs = _boundaries(duration_sec)
    spans = [{"id": f"sp{i:04d}", "t_in": a, "t_out": b,
              "is_audio": bool(i % 2), "time_authority": "stt" if i % 2 else "scene",
              "text": "말" if i % 2 else ""}
             for i, (a, b) in enumerate(zip(bs, bs[1:]))]
    return {"schema": "v3_grid/v1",
            "source": {"path": "x.mp4", "duration_sec": float(duration_sec),
                       "fps": 24.0, "width": 1920, "height": 1080},
            "transcript": {"backend": "whisper", "model": "m", "word_count": 1,
                           "failed_windows": [], "srt_provided": False},
            "span_candidates": spans, "scene_cuts": bs[1:-1],
            "words": [], "silence": [], "arousal": []}


def _run(monkeypatch, duration_sec):
    """Stage 1 을 가짜 응답으로 한 바퀴 돌리고 (호출에 실린 fps, 감사) 를 돌려준다."""
    seen = {}

    def rng(a, b):
        return {"start": schemas.format_ts(a), "end": schemas.format_ts(b)}

    # 격자 눈금(0 · 중점 · 끝)에 맞춘 경계라 스냅이 통과한다. 10분 초과 sequence 라
    # chunk 를 명시한다(부재는 반려 사유).
    half = duration_sec / 2
    bs = _boundaries(duration_sec)
    chunks = [{"number": i, "time": rng(a, b)} for i, (a, b) in enumerate(zip(bs, bs[1:]))]

    def fake_call(gemini, uploaded, prompt, *, sample_fps=None):
        seen["fps"] = sample_fps
        return {"sequences": [{"number": 0, "time": rng(0.0, float(duration_sec)),
                               "content": "본편", "chunks": chunks}],
                "exception_sector": {k: None for k in schemas.EXCEPTION_KEYS}}

    monkeypatch.setattr(s1, "_upload_video", lambda *a, **k: object())
    monkeypatch.setattr(s1, "_call_model", fake_call)
    monkeypatch.setattr(s1, "heuristic_hints", lambda grid: {})
    doc, audit = s1.run_seq_analyze(object(), None, _grid(duration_sec),
                                    log=lambda *a, **k: None)
    assert doc["sequences"], "가짜 응답이 통과해야 배선을 잰다"
    return seen, audit


def test_chosen_fps_reaches_the_api_call_and_audit(monkeypatch):
    """3시간이면 0.85 가 실제 호출 인자로 가고 감사 기록에도 남는다."""
    seen, audit = _run(monkeypatch, 3 * 3600)
    assert seen["fps"] == 0.85
    assert audit["sample_fps"] == 0.85
    assert audit["sample_fps_note"]["reason"] == "reduced_for_duration"


def test_short_source_still_calls_with_base_fps(monkeypatch):
    """회귀 0 — 기존 길이는 종전과 같은 1.0 으로 호출된다."""
    seen, audit = _run(monkeypatch, 67 * 60)
    assert seen["fps"] == s1.SCAN_SAMPLE_FPS == 1.0
    assert audit["sample_fps_note"]["reason"] == "base"


def test_precheck_runs_before_expensive_encode():
    """사전검사는 스캔 프록시 인코딩 **앞**이어야 한다(3시간 업로드 실측 364초)."""
    src = (__import__("pathlib").Path("app/v3/pipeline.py")).read_text(encoding="utf-8")
    i_check = src.index("s1.resolve_scan_fps(duration)")
    i_build = src.index("s1.build_scan_proxy(")
    assert i_check < i_build, "fps 사전검사가 프록시 인코딩 뒤로 밀렸다"


def test_precheck_only_when_stage1_will_run():
    """재개 계약 — stage1.json 캐시를 쓰는 실행은 이 가드가 새로 죽이면 안 된다."""
    src = (__import__("pathlib").Path("app/v3/pipeline.py")).read_text(encoding="utf-8")
    head = src[:src.index("s1.resolve_scan_fps(duration)")]
    tail = head[head.rindex("if not skip_seq_analyze:"):]
    assert 'stage1.json").exists()' in tail
    assert 'from_step in ("grid", "seq_analyze")' in tail


def test_run_log_records_sample_fps():
    src = (__import__("pathlib").Path("app/v3/pipeline.py")).read_text(encoding="utf-8")
    assert "sample_fps=audit.get(\"sample_fps\")" in src
    assert "sample_fps=scan_fps" in src
