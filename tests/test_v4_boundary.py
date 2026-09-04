"""V4-M3 §3 경계 정밀(6b) 회귀 가드 — `app/v4/boundary.py`.

이 파일이 값으로 못박는 것:

① **창 계산** — 신고된 경계마다 ±`PROBE_WINDOW_SEC`(O6 = 60), 러닝타임 양 끝에 붙은
   경계는 검사 없음, 신고가 **전무한 편만** 꼬리 180초 의무 창(기획서 §3).
② **v3 대조** — `window_sec=90` 으로 부르면 v3 `refine.boundary_probe_windows` 와
   **같은 창**이 나온다. 갈린 것이 창 크기 하나뿐임을 기계가 지킨다(v3 는 동결이므로
   저쪽이 움직이면 여기서 잡힌다).
③ **±60 의 대가** — 제안 경계가 창 밖이면 그 방향으로 한 창 더. 없으면 가왕쇼 사고가
   돌아온다. 두 실측(가왕쇼 49.5s · 포핸즈2 91s)의 도달 여부를 값으로 고정한다.
④ **원판정 유지 비대칭** — 실패·예산 소진·스냅 실패·축소 미검증은 전부 경계를 안 옮긴다.
⑤ **격자 스냅** — 확정 시각은 `grid.schemas.snap_time` 을 지난 눈금이다.
⑥ **순수·결정성** — 순수 함수는 입력을 안 고치고, 같은 입력이면 같은 산출이다.

🛑 네트워크 0. `gemini` 는 가짜지만 `types` 는 **진짜 google-genai** 다 —
`app/v4/video.py` 의 실제 배선(offset 멀티파트·재시도·usage)을 그대로 태운다.
실호출로만 알 수 있는 것(모델이 `earlier`/`later` 를 잘 내는가 · 서버가 offset 창을
어떻게 읽는가)은 이 파일의 범위 밖이고 모듈 독스트링이 그렇게 적어 두었다.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.v3 import refine as v3refine
from app.v4 import boundary as B


# ── 가짜 클라이언트 ─────────────────────────────────────────────────────────

def _resp(payload, *, finish: str = "STOP", prompt_tok: int = 1000,
          out_tok: int = 20, total: int | None = None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tok, thoughts_token_count=0,
            candidates_token_count=out_tok, cached_content_token_count=0,
            total_token_count=total if total is not None else prompt_tok + out_tok),
        model_version="gemini-3.7-flash-001",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
    )


class _FakeModels:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.queue.pop(0) if self.queue else _resp({"boundary": "none"})
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
                                      model_name="gemini-3.7-flash")
        self.deleted: list[str] = []

    def _deleted(self, *, name):
        self.deleted.append(name)


HANDLE = SimpleNamespace(uri="https://generativelanguage.googleapis.com/v1beta/files/x",
                         name="files/x")


def _grid(duration: float = 3600.0, cuts=()):
    return {"source": {"duration_sec": duration},
            "scene_cuts": sorted(round(float(c), 3) for c in cuts),
            "span_candidates": []}


def _windows(gem):
    """가짜 모델이 실제로 받은 창 목록 → [(start_offset, end_offset), …].

    ⚠ 여기를 읽는 것이 요점이다 — 재프로브가 '두 번째 창'을 실제로 보냈는지는
    audit 기록이 아니라 **요청 파트**로 확인해야 한다(기록만 보면 배선이 빠져도 통과한다)."""
    out = []
    for call in gem.models.calls:
        part = call["contents"][0]
        meta = part.video_metadata
        out.append((meta.start_offset, meta.end_offset))
    return out


def _sector(**zones):
    return {k: {"start_sec": v[0], "end_sec": v[1]} for k, v in zones.items()}


# ═══════════════════════════════════════════════════════════════════════════
# ① 창 계산
# ═══════════════════════════════════════════════════════════════════════════

def test_windows_are_centered_on_the_reported_edges():
    probes = B.probe_windows(_sector(intro=(143.0, 1200.0), teaser=(3500.0, 3600.0)),
                             3600.0)
    edges = [(p["zone"], p["edge"], p["t0"], p["t1"], p["orig"]) for p in probes
             if p["zone"] != "tail"]
    assert edges[:2] == [
        ("intro", "start", 143.0 - 60.0, 143.0 + 60.0, 143.0),
        ("intro", "end", 1200.0 - 60.0, 1200.0 + 60.0, 1200.0),
    ]
    assert all(p["duration_sec"] == 3600.0 and p["extension"] == 0 for p in probes)
    # 꼬리(teaser)가 신고됐으므로 의무 창은 없다
    assert not [p for p in probes if p["zone"] == "tail"]


def test_window_size_is_the_operator_decision_o6():
    """O6 = ±60. v3 는 ±90 이었다 — 이 값이 조용히 되돌아가면 토큰 예산이 달라진다."""
    assert B.PROBE_WINDOW_SEC == 60.0
    assert v3refine.WINDOW_BACK_SEC == 90.0


def test_edges_pinned_to_the_running_time_are_not_probed():
    """0 에 붙은 시작·끝에 붙은 끝은 움직일 곳이 없다 — 창을 내면 호출만 태운다."""
    probes = B.probe_windows(_sector(intro=(0.0, 47.5), teaser=(3500.0, 3600.0)), 3600.0)
    assert [(p["zone"], p["edge"]) for p in probes] == [("intro", "end"),
                                                       ("teaser", "start")]


def test_tail_window_when_no_tail_zone_was_reported():
    """🛑 말미 의무 창의 조건은 **꼬리 구역 신고가 없을 때**다 — '아무 신고도 없을 때'가
    아니다.

    기획서 §3 문구("exception 전무 편")를 글자대로 읽으면 **머리만 신고하고 예고를 통째로
    놓친 편**의 꼬리를 아무도 안 본다 — 6b 가 막으라고 존재하는 바로 그 사고다(가왕쇼
    6화: 예고 50초가 쇼츠 엔딩을 오염시켰다). 2026-09-03 스모크에서 실제로 그 구멍이
    열렸다(intro 만 신고된 편 → 꼬리 프로브 0콜).

    대가는 편당 최대 1콜이고, 그것도 모델이 꼬리를 하나도 신고하지 않은 편에만 든다."""
    probes = B.probe_windows({}, 3600.0)
    assert len(probes) == 1
    p = probes[0]
    assert (p["zone"], p["edge"], p["t0"], p["t1"], p["orig"]) == \
        ("tail", "start", 3600.0 - 180.0, 3600.0, None)
    assert B.TAIL_PROBE_SEC == 180.0 == v3refine.TAIL_WINDOW_SEC

    # ⚠ 머리만 신고된 편도 꼬리를 본다(종전에는 안 봤다 — 그것이 이 수정의 이유다)
    head_only = B.probe_windows(_sector(intro=(0.0, 40.0)), 3600.0)
    assert [q for q in head_only if q["zone"] == "tail"]

    # 꼬리가 하나라도 신고되면 의무 창은 없다 — 그 자리는 이미 모델이 봤다
    for key, span in (("teaser", (3400.0, 3600.0)), ("credit", (3500.0, 3600.0)),
                      ("end", (3550.0, 3600.0))):
        got = B.probe_windows(_sector(**{key: span}), 3600.0)
        assert not [q for q in got if q["zone"] == "tail"], key


def test_tail_trigger_uses_the_shared_tail_vocabulary():
    """꼬리 어휘는 깔때기와 **같은 것**을 쓴다 — 두 벌이면 한쪽만 고쳐지는 날
    `end` 같은 종이 여기서만 빠진다(2026-09-03 적대 검증이 깔때기에서 잡은 그 결함)."""
    from app.v4.funnel import TAIL_SECTORS
    assert B.TAIL_SECTORS is TAIL_SECTORS


def test_tail_window_skipped_on_a_short_source():
    """창이 편의 절반을 넘으면 '말미'가 아니다 — 편 전체를 예고로 볼 위험을 안 만든다."""
    assert B.probe_windows({}, 80.0) == []


def test_windows_are_clamped_to_the_source():
    probes = B.probe_windows(_sector(intro=(20.0, 3580.0)), 3600.0)
    assert probes[0]["t0"] == 0.0
    assert probes[1]["t1"] == 3600.0


def test_probe_order_follows_exception_keys_not_dict_order():
    """JSON 열쇠 순서가 달라도 감사 기록이 흔들리면 안 된다(결정성)."""
    a = B.probe_windows(_sector(teaser=(3000.0, 3400.0), intro=(43.0, 120.0)), 3600.0)
    b = B.probe_windows(_sector(intro=(43.0, 120.0), teaser=(3000.0, 3400.0)), 3600.0)
    assert a == b
    assert [p["zone"] for p in a] == ["intro", "intro", "teaser", "teaser"]


def test_unknown_sector_name_fails_loud():
    """조용히 무시하면 그 구역이 경계 정밀에서 빠진 채 발행된다(funnel 과 같은 규율)."""
    with pytest.raises(ValueError, match="모르는 예고 구역"):
        B.probe_windows({"outro": {"start_sec": 1.0, "end_sec": 2.0}}, 3600.0)


def test_bad_duration_fails_loud():
    for bad in (0.0, -1.0, None, float("nan")):
        with pytest.raises(ValueError):
            B.probe_windows({}, bad)


def test_probe_windows_is_pure():
    sector = _sector(intro=(43.0, 120.0))
    snapshot = json.dumps(sector, sort_keys=True)
    B.probe_windows(sector, 3600.0)
    assert json.dumps(sector, sort_keys=True) == snapshot


# ═══════════════════════════════════════════════════════════════════════════
# ② v3 대조 — 갈린 것은 창 크기 하나뿐이다
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sector,duration", [
    ({"intro": {"start": 43.0, "end": 120.0},
      "teaser": {"start": 3500.0, "end": 3600.0}}, 3600.0),
    ({"intro": {"start": 0.0, "end": 138.5},
      "teaser": {"start": 3400.0, "end": 3600.0}}, 3600.0),
    ({}, 3600.0),
    ({}, 80.0),
])
def test_windows_match_v3_when_the_size_matches(sector, duration):
    """v3 `boundary_probe_windows` 와 같은 창을 낸다(크기를 90 으로 맞췄을 때).

    v3 는 동결이지만 **라이브러리로 남는다** — 저쪽이 움직이면 v4 가 조용히 갈라지는
    대신 여기서 잡힌다. 갈라도 되는 것은 `window_sec` 하나이고, 그것이 O6 이다."""
    mine = B.probe_windows(sector, duration, window_sec=v3refine.WINDOW_BACK_SEC)
    theirs = v3refine.boundary_probe_windows(sector, duration)
    assert [(p["zone"], p["edge"], round(p["t0"], 6), round(p["t1"], 6), p["orig"])
            for p in mine] == \
           [(p["zone"], p["edge"], round(p["t0"], 6), round(p["t1"], 6), p["orig"])
            for p in theirs]


def test_reused_v3_helpers_are_the_same_objects():
    """판단 함수는 베끼지 않고 **부른다**(계약 §3 · ABSORB_TABLE 의 취지)."""
    assert B.scene_cut_candidates is v3refine.scene_cut_candidates
    assert B.verify_sample_window is v3refine.verify_sample_window
    assert B.validate_verify_response is v3refine.validate_verify_response
    assert B.FLASH_BUDGET is v3refine.FLASH_BUDGET
    assert B.PROBE_SAMPLE_FPS == v3refine.PROBE_SAMPLE_FPS == 6.0


# ═══════════════════════════════════════════════════════════════════════════
# ③ ±60 의 대가 — 창 밖 재프로브
# ═══════════════════════════════════════════════════════════════════════════

def test_gawangsho_late_teaser_is_inside_the_60s_window():
    """🛑 실사고 재현 — 가왕쇼 6화: 예고 시작을 49.5초 늦게 판정했다.

    ±30 이면 창 **밖**이라 v3 가 ±90 을 골랐고, O6 은 ±60 으로 좁혔다. 49.5 < 60 이므로
    **첫 창 안**이다 — 이 부등식이 깨지면 그 사고가 재프로브 없이는 안 잡힌다."""
    reported, truth = 1785.0, 1735.5
    late = reported - truth
    assert late == 49.5

    probe = B.probe_windows(_sector(teaser=(reported, 3600.0)), 3600.0)[0]
    assert (probe["zone"], probe["edge"]) == ("teaser", "start")
    assert probe["t0"] <= truth <= probe["t1"], "±60 창이 가왕쇼 지각을 못 덮는다"

    # v3 가 ±90 을 고른 이유(±30 이면 밖)도 같은 자리에 박아 둔다 — 창을 더 좁히려는
    # 다음 사람이 이 줄을 본다.
    narrow = B.probe_windows(_sector(teaser=(reported, 3600.0)), 3600.0, window_sec=30.0)[0]
    assert not (narrow["t0"] <= truth <= narrow["t1"])


def test_fourhands2_needs_the_out_of_window_reprobe():
    """포핸즈2 intro 과잉(138.5 → 47.5, 91초 밖) — 첫 창 밖이고 재프로브 창 안이다.

    v3 는 이 건을 '긴 zone 부분 표본 + 재프로브'로 잡았다. v4 는 그 경로를 버리고
    창 밖 재프로브 하나로 잡는다 — 그 대체가 성립하는지가 이 계산이다."""
    probe = B.probe_windows(_sector(intro=(0.0, 138.5)), 3600.0)[0]
    assert (probe["edge"], probe["t0"], probe["t1"]) == ("end", 78.5, 198.5)
    assert not (probe["t0"] <= 47.5 <= probe["t1"])

    nxt = B.extend_window(probe, "earlier")
    assert (nxt["t0"], nxt["t1"], nxt["extension"]) == (0.0, 78.5, 1)
    assert nxt["t0"] <= 47.5 <= nxt["t1"]


def test_extend_window_moves_one_window_in_that_direction():
    probe = {"zone": "teaser", "edge": "start", "t0": 1000.0, "t1": 1120.0,
             "orig": 1060.0, "duration_sec": 3600.0, "extension": 0}
    earlier = B.extend_window(probe, "earlier")
    later = B.extend_window(probe, "later")
    assert (earlier["t0"], earlier["t1"]) == (880.0, 1000.0)
    assert (later["t0"], later["t1"]) == (1120.0, 1240.0)
    assert earlier["extended"] == "earlier" and later["extended"] == "later"
    # 원래 창의 나머지 재료는 그대로 따라간다(zone·edge·orig)
    assert (earlier["zone"], earlier["edge"], earlier["orig"]) == ("teaser", "start", 1060.0)


def test_extend_window_never_leaves_the_source():
    at_head = {"zone": "intro", "edge": "end", "t0": 0.0, "t1": 78.5,
               "orig": 40.0, "duration_sec": 3600.0, "extension": 0}
    assert B.extend_window(at_head, "earlier") is None       # 넓힐 폭이 없다
    at_tail = {"zone": "teaser", "edge": "start", "t0": 3540.0, "t1": 3600.0,
               "orig": 3570.0, "duration_sec": 3600.0, "extension": 0}
    assert B.extend_window(at_tail, "later") is None


def test_extend_window_rejects_unknown_direction():
    probe = {"zone": "intro", "edge": "end", "t0": 10.0, "t1": 130.0,
             "orig": 70.0, "duration_sec": 3600.0, "extension": 0}
    with pytest.raises(ValueError, match="방향"):
        B.extend_window(probe, "sideways")


def test_extend_window_is_pure():
    probe = {"zone": "intro", "edge": "end", "t0": 10.0, "t1": 130.0,
             "orig": 70.0, "duration_sec": 3600.0, "extension": 0}
    snapshot = dict(probe)
    B.extend_window(probe, "later")
    assert probe == snapshot


# ═══════════════════════════════════════════════════════════════════════════
# 응답 검증
# ═══════════════════════════════════════════════════════════════════════════

CANDS = [{"id": "c00", "t": 100.0, "rel": 10.0}, {"id": "c01", "t": 120.0, "rel": 30.0}]


@pytest.mark.parametrize("value", ["c00", "c01", "none", "earlier", "later"])
def test_probe_response_vocabulary(value):
    """창 밖 어휘 둘은 ±60 의 대가를 갚는 유일한 통로다 — 모르는 값으로 반려하면 벽이 된다."""
    chosen, problems = B.validate_probe_response({"boundary": value}, CANDS)
    assert (chosen, problems) == (value, [])


@pytest.mark.parametrize("resp", [
    {"boundary": "c99"},            # 후보에 없는 id
    {"boundary": 3},                # 숫자
    {"boundary": None},
    {},                             # 열쇠 자체가 없음
    "c00",                          # 객체가 아님
    None,
])
def test_probe_response_rejections_say_why(resp):
    chosen, problems = B.validate_probe_response(resp, CANDS)
    assert chosen is None and problems and isinstance(problems[0], str)


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ 격자 스냅 · 경계 반영
# ═══════════════════════════════════════════════════════════════════════════

GRID_TIMES = [0.0, 47.5, 120.0, 1735.5, 3600.0]


def test_apply_boundary_snaps_to_the_grid():
    """확정 시각은 모델이 부른 값이 아니라 **격자 눈금**이다(시간 정본 원칙)."""
    sector = _sector(intro=(0.0, 138.5))
    probe = {"zone": "intro", "edge": "end", "orig": 138.5, "duration_sec": 3600.0}
    out, notes = B.apply_boundary(sector, probe, 121.2, GRID_TIMES)
    assert out["intro"]["end_sec"] == 120.0
    assert any("스냅" in n for n in notes)


def test_apply_boundary_keeps_the_original_when_the_snap_fails():
    """관용(2.0s) 밖이면 안 옮긴다 — 잘못 옮기는 것이 안 옮기는 것보다 나쁘다."""
    sector = _sector(intro=(0.0, 138.5))
    probe = {"zone": "intro", "edge": "end", "orig": 138.5, "duration_sec": 3600.0}
    out, notes = B.apply_boundary(sector, probe, 200.0, GRID_TIMES)
    assert out == sector
    assert notes and "스냅 실패" in notes[0]


def test_apply_boundary_rejects_inverted_and_overlapping_moves():
    sector = _sector(intro=(0.0, 120.0), recap=(120.0, 200.0))
    times = [0.0, 47.5, 120.0, 200.0, 3600.0]

    # ① 역전 — intro.start 를 끝 뒤로
    inverted, notes = B.apply_boundary(
        sector, {"zone": "intro", "edge": "start", "orig": 0.0, "duration_sec": 3600.0},
        200.0, times)
    assert inverted == sector and any("역전" in n for n in notes)

    # ② 이웃 침범 — intro.end 를 recap 안으로(창이 이웃 위로 뻗을 수 있다)
    overlapped, notes = B.apply_boundary(
        sector, {"zone": "intro", "edge": "end", "orig": 120.0, "duration_sec": 3600.0},
        200.0, times)
    assert overlapped == sector and any("겹친다" in n for n in notes)


def test_apply_boundary_creates_a_teaser_for_the_tail_probe():
    probe = {"zone": "tail", "edge": "start", "orig": None, "duration_sec": 3600.0}
    out, notes = B.apply_boundary({}, probe, 1735.5, GRID_TIMES)
    assert out == {"teaser": {"start_sec": 1735.5, "end_sec": 3600.0}}
    assert any("teaser 신설" in n for n in notes)


def test_apply_boundary_keeps_the_key_naming_of_the_zone():
    """v3 산출·M0 채점기는 `start`/`end` 를 읽는다 — 여기서 이름을 바꾸면 그 zone 이
    읽는 쪽 하나에서 통째로 사라진다(= 신고가 없어진다)."""
    sector = {"intro": {"start": 0.0, "end": 138.5}}
    probe = {"zone": "intro", "edge": "end", "orig": 138.5, "duration_sec": 3600.0}
    out, _notes = B.apply_boundary(sector, probe, 120.0, GRID_TIMES)
    assert out == {"intro": {"start": 0.0, "end": 120.0}}


def test_apply_boundary_is_pure_and_deterministic():
    sector = _sector(intro=(0.0, 138.5))
    snapshot = json.dumps(sector, sort_keys=True)
    probe = {"zone": "intro", "edge": "end", "orig": 138.5, "duration_sec": 3600.0}
    a = B.apply_boundary(sector, probe, 121.2, GRID_TIMES)
    b = B.apply_boundary(sector, probe, 121.2, GRID_TIMES)
    assert a == b
    assert json.dumps(sector, sort_keys=True) == snapshot


def test_apply_boundary_rejects_unknown_zone():
    with pytest.raises(ValueError, match="모르는 zone"):
        B.apply_boundary({}, {"zone": "outro", "edge": "start", "duration_sec": 10.0},
                         5.0, [0.0, 5.0, 10.0])


# ═══════════════════════════════════════════════════════════════════════════
# 실행 — 가짜 모델
# ═══════════════════════════════════════════════════════════════════════════

def test_run_moves_the_gawangsho_teaser_to_the_true_cut():
    """🛑 사고 재현 end-to-end — 신고 1785.0, 진짜 1735.5. 한 콜로 잡힌다."""
    grid = _grid(3600.0, cuts=[1700.0, 1735.5, 1800.0])
    gem = _FakeGemini([_resp({"boundary": "c00"})])       # 창 [1725,1845] 안 후보 c00 = 1735.5
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 1735.5
    assert audit["moved"] == 1 and audit["flash_calls"] == 1
    assert audit["tokens"] == 1020
    # 창은 offset 멀티파트로 붙는다 — 기록이 아니라 요청을 본다
    assert _windows(gem) == [("1725.000s", "1845.000s")]


def test_run_reprobes_the_next_window_when_the_model_points_outside():
    """포핸즈2 — 첫 창 밖 지목 → 그 방향 한 창 더 → 축소 검증 → 반영."""
    grid = _grid(3600.0, cuts=[47.5, 100.0, 138.5])
    gem = _FakeGemini([
        _resp({"boundary": "earlier"}),      # 창 [78.5,198.5] — 경계는 더 앞이다
        _resp({"boundary": "c00"}),          # 재프로브 창 [0,78.5] 안 후보 = 47.5
        _resp({"kind": "main"}),             # 해방 구간 [47.5,138.5] 실체 = 본편
        _resp({"kind": "exception"}),        # 남은 intro [0,47.5] 실체 검증
    ])
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(intro=(0.0, 138.5)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["intro"] == {"start_sec": 0.0, "end_sec": 47.5}
    assert _windows(gem)[:3] == [("78.500s", "198.500s"),
                                 ("0.000s", "78.500s"),
                                 ("47.500s", "138.500s")]
    win = audit["probes"][0]["windows"]
    assert len(win) == 2 and win[0]["extension"] == 0 and win[1]["extension"] == 1
    assert win[1]["shrink_check"]["kind"] == "main"


def test_run_stops_extending_at_the_cap_and_keeps_the_original():
    """확장 상한을 넘어서까지 쫓지 않는다 — 예산은 다른 경계의 몫이다."""
    grid = _grid(3600.0, cuts=[500.0, 900.0, 1000.0, 1100.0, 1500.0])
    gem = _FakeGemini([_resp({"boundary": "earlier"}), _resp({"boundary": "earlier"})])
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(1000.0, 3600.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 1000.0          # 원판정 유지
    assert audit["moved"] == 0
    assert len(gem.models.calls) == 1 + B.MAX_WINDOW_EXTENSIONS
    assert "창 밖 지목" in audit["probes"][0]["result"]


def test_run_keeps_the_original_when_the_shrink_check_says_exception():
    """오염 방지 비대칭(v3 실사고 2호) — 해방 구간이 본편이 아니면 축소를 기각한다."""
    grid = _grid(3600.0, cuts=[3400.0, 3450.0, 3500.0])
    gem = _FakeGemini([_resp({"boundary": "c01"}),          # teaser.start 를 3450 으로(축소)
                       _resp({"kind": "exception"})])       # 해방 [3400,3450] 은 예고다
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(3400.0, 3600.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 3400.0
    assert audit["moved"] == 0
    assert "축소 기각" in audit["probes"][0]["result"]


def test_run_applies_a_widening_move_without_spending_a_verify_call():
    """확대 방향은 손실 위험뿐이라 그냥 간다(v3 규약) — 검증 콜을 안 태운다."""
    grid = _grid(3600.0, cuts=[3350.0, 3400.0, 3450.0])
    gem = _FakeGemini([_resp({"boundary": "c00"})])         # 3350 — teaser 를 넓힌다
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(3400.0, 3600.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 3350.0
    assert len(gem.models.calls) == 1


def test_run_keeps_the_original_when_the_budget_runs_out():
    """예산 소진 = 원판정 유지. 왜 멈췄는지 감사에 남는다."""
    grid = _grid(3600.0, cuts=[100.0, 140.0, 3300.0, 3400.0])
    gem = _FakeGemini([_resp({"boundary": "none"})])
    sector, audit = B.run_boundary_probe(
        gem, HANDLE,
        exception_sector=_sector(intro=(0.0, 138.5), teaser=(3400.0, 3600.0)),
        grid=grid, duration_sec=3600.0, budget=1, log=lambda *a: None)

    assert sector == _sector(intro=(0.0, 138.5), teaser=(3400.0, 3600.0))
    assert len(gem.models.calls) == 1
    assert audit["stopped"] and "호출 상한" in audit["stopped"]
    assert audit["flash_calls"] == 1


def test_run_enforces_the_token_budget_too():
    """계약 §3 — 호출 수와 **누적 토큰 둘 다** 강제한다."""
    grid = _grid(3600.0, cuts=[100.0, 140.0, 3300.0, 3400.0])
    gem = _FakeGemini([_resp({"boundary": "none"}, prompt_tok=5000, total=5000)])
    _sec, audit = B.run_boundary_probe(
        gem, HANDLE,
        exception_sector=_sector(intro=(0.0, 138.5), teaser=(3400.0, 3600.0)),
        grid=grid, duration_sec=3600.0, budget_tokens=4000, log=lambda *a: None)

    assert len(gem.models.calls) == 1                 # 두 번째 창은 못 나간다
    assert audit["stopped"] and "누적 토큰" in audit["stopped"]
    assert audit["tokens"] == 5000


def test_run_does_not_reask_a_permanent_failure():
    """E11 규약 — 400 류는 다시 보내도 같은 답이다. 예산을 태우지 않는다."""
    grid = _grid(3600.0, cuts=[1700.0, 1735.5, 1800.0])

    class _Boom(_FakeModels):
        def generate_content(self, **kw):
            super().generate_content(**kw)
            raise ValueError("400 INVALID_ARGUMENT")

    gem = _FakeGemini()
    gem.models = _Boom([])
    gem.client = SimpleNamespace(models=gem.models, files=SimpleNamespace(delete=None))
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 1785.0        # 원판정 유지
    assert len(gem.models.calls) == 1                     # 재질의 없음
    problems = audit["probes"][0]["windows"][0]["attempts"][0]["problems"]
    assert "permanent" in problems[0], problems           # 왜 안 봤는지가 기록에 남는다


def test_run_reasks_a_rejected_answer_then_accepts():
    """모르는 id 는 반려 사유와 함께 재질의한다(≤MAX_REASKS)."""
    grid = _grid(3600.0, cuts=[1700.0, 1735.5, 1800.0])
    gem = _FakeGemini([_resp({"boundary": "c99"}), _resp({"boundary": "c00"})])
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 1735.5
    attempts = audit["probes"][0]["windows"][0]["attempts"]
    assert len(attempts) == 2 and attempts[0]["problems"]
    # 반려 사유가 두 번째 프롬프트에 실린다 — 안 실으면 같은 답이 온다
    assert "직전 반려" in gem.models.calls[1]["contents"][-1]


def test_run_gives_up_after_max_reasks():
    grid = _grid(3600.0, cuts=[1700.0, 1735.5, 1800.0])
    gem = _FakeGemini([_resp({"boundary": "c99"})] * (1 + B.MAX_REASKS))
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 1785.0
    assert len(gem.models.calls) == 1 + B.MAX_REASKS
    assert "재질의 소진" in audit["probes"][0]["result"]


def test_run_keeps_the_original_when_there_are_no_scene_cuts():
    """후보가 없으면 부르지 않는다 — 모델에게 고를 것이 없다."""
    gem = _FakeGemini()
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
        grid=_grid(3600.0, cuts=[10.0]), duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 1785.0
    assert gem.models.calls == []
    assert "후보 없음" in audit["probes"][0]["result"]


def test_run_returns_the_same_object_when_nothing_moved():
    """항등 — 아무것도 안 움직였으면 넘겨받은 것을 그대로 돌려준다."""
    sector_in = _sector(teaser=(1785.0, 3600.0))
    gem = _FakeGemini([_resp({"boundary": "none"})])
    sector_out, _audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=sector_in,
        grid=_grid(3600.0, cuts=[1700.0, 1735.5, 1800.0]), duration_sec=3600.0,
        log=lambda *a: None)
    assert sector_out is sector_in


def test_run_discards_a_zone_the_verify_call_says_is_the_main_show():
    """신병4 유형 — 본편 장면을 credit 으로 판정한 zone 은 경계가 아니라 zone 이 틀렸다."""
    grid = _grid(3600.0, cuts=[1000.0, 1030.0])
    gem = _FakeGemini([_resp({"boundary": "none"}),      # 경계 프로브(start)
                       _resp({"boundary": "none"}),      # 경계 프로브(end)
                       _resp({"kind": "main"})])         # 실체 검증
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(credit=(1000.0, 1030.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["credit"] is None                      # 폐기를 **명시**한다
    assert audit["moved"] == 1
    assert any("구역 폐기" in str(p.get("result")) for p in audit["probes"])


def test_run_does_not_discard_a_long_zone_from_one_middle_sample():
    """긴 zone 은 중앙 60s 표본 하나로 폐기하지 않는다(v3 와 같은 보수)."""
    grid = _grid(3600.0, cuts=[1000.0, 1400.0])
    gem = _FakeGemini([_resp({"boundary": "none"}), _resp({"boundary": "none"})])
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(credit=(1000.0, 1400.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["credit"] == {"start_sec": 1000.0, "end_sec": 1400.0}
    assert len(gem.models.calls) == 2                    # 실체 검증 콜 없음
    assert any("긴 구역" in str(p.get("result")) for p in audit["probes"])


def test_run_skips_the_verify_for_a_very_short_zone():
    grid = _grid(3600.0, cuts=[1000.0, 1004.0])
    gem = _FakeGemini([_resp({"boundary": "none"}), _resp({"boundary": "none"})])
    _sector_out, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(credit=(1000.0, 1004.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)
    assert len(gem.models.calls) == 2
    assert any("짧은 구역" in str(p.get("result")) for p in audit["probes"])


def test_run_records_probes_it_never_looked_at():
    """상한 초과분을 조용히 자르지 않는다 — 무엇을 안 봤는지가 감사의 절반이다."""
    zones = {"intro": (100.0, 200.0), "recap": (300.0, 400.0),
             "teaser": (500.0, 600.0)}                   # 경계 6개 > MAX_PROBES(5)
    grid = _grid(3600.0, cuts=[])
    gem = _FakeGemini()
    _s, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(**zones), grid=grid,
        duration_sec=3600.0, log=lambda *a: None)
    skipped = [p for p in audit["probes"] if "미검사" in str(p.get("result"))]
    assert len(skipped) == 6 - B.MAX_PROBES


def test_run_uses_the_flash_slot_and_the_probe_sample_fps():
    """국소 창은 Flash 로 충분하다(v3 와 같은 슬롯) · 표본 fps 는 6.0."""
    grid = _grid(3600.0, cuts=[1700.0, 1735.5, 1800.0])
    gem = _FakeGemini([_resp({"boundary": "none"})])
    B.run_boundary_probe(gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
                         grid=grid, duration_sec=3600.0, log=lambda *a: None)
    call = gem.models.calls[0]
    assert call["model"] == "gemini-3.7-flash"
    assert call["contents"][0].video_metadata.fps == 6.0


def test_run_is_deterministic_for_the_same_answers():
    """같은 입력·같은 응답이면 같은 산출(감사의 판정 문자열까지)."""
    def once():
        grid = _grid(3600.0, cuts=[1700.0, 1735.5, 1800.0])
        gem = _FakeGemini([_resp({"boundary": "c00"})])
        sector, audit = B.run_boundary_probe(
            gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
            grid=grid, duration_sec=3600.0, log=lambda *a: None)
        results = [str(p.get("result")) for p in audit["probes"]]
        return sector, results, audit["moved"], audit["flash_calls"], audit["tokens"]

    assert once() == once()


def test_run_fails_loud_on_an_unknown_sector_name():
    with pytest.raises(ValueError, match="모르는 예고 구역"):
        B.run_boundary_probe(_FakeGemini(), HANDLE,
                             exception_sector={"outro": {"start_sec": 1.0, "end_sec": 9.0}},
                             grid=_grid(), duration_sec=3600.0, log=lambda *a: None)


def test_run_never_deletes_the_shared_handle():
    """🛑 6·6b·8·10a 가 같은 핸들을 쓴다 — 여기서 지우면 뒷단계가 죽은 핸들을 쓴다."""
    grid = _grid(3600.0, cuts=[1700.0, 1735.5, 1800.0])
    gem = _FakeGemini([_resp({"boundary": "c00"})])
    B.run_boundary_probe(gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
                         grid=grid, duration_sec=3600.0, log=lambda *a: None)
    assert gem.deleted == []


def test_run_creates_a_teaser_from_the_mandatory_tail_window():
    """신고가 전무한 편 — 말미 180초를 의무로 보고, 찾으면 teaser 를 신설한다."""
    grid = _grid(3600.0, cuts=[3300.0, 3450.0, 3500.0])
    gem = _FakeGemini([_resp({"boundary": "c01"}),      # 꼬리 창 [3420,3600] 안 c01 = 3500
                       _resp({"kind": "exception"})])   # 신설된 teaser 실체 검증
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector={}, grid=grid, duration_sec=3600.0,
        log=lambda *a: None)

    assert sector == {"teaser": {"start_sec": 3500.0, "end_sec": 3600.0}}
    assert _windows(gem)[0] == ("3420.000s", "3600.000s")
    assert audit["moved"] == 1


def test_run_keeps_the_original_when_the_response_is_not_json():
    """파싱 실패도 원판정 유지다 — 그리고 그 시도의 토큰은 예산에 **더해진다**
    (MAX_TOKENS 절단 시도가 제일 비싸다 · video.VideoParseError 계약)."""
    grid = _grid(3600.0, cuts=[1700.0, 1735.5, 1800.0])
    gem = _FakeGemini([_resp("이건 JSON 이 아니다", finish="MAX_TOKENS")] * (1 + B.MAX_REASKS))
    sector, audit = B.run_boundary_probe(
        gem, HANDLE, exception_sector=_sector(teaser=(1785.0, 3600.0)),
        grid=grid, duration_sec=3600.0, log=lambda *a: None)

    assert sector["teaser"]["start_sec"] == 1785.0
    assert audit["tokens"] == (1 + B.MAX_REASKS) * 1020
    assert "재질의 소진" in audit["probes"][0]["result"]
