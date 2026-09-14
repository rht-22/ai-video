"""쇼츠 구성 규칙 5건(2026-09-11, 지금불륜 4편 ep01x03 검수 → 사용자 교정판) 회귀 가드 — LLM·ffmpeg 없이.

  1. 첫 3초 안에 정지 없음(컷 전환 또는 발화 시작) — `cover.opening_motion` + 걸음 4 재질의 통로
  2. 반응 라벨은 그 대사를 **들은 사람**에게 — Stage 4 앵커 표에 화자, reaction 은 person(청자) 필수·화자 본인 드롭·
     person_visible=false 드롭
  3. 시간 경과 단정 금지 — `narration.TIME_ASSERT_BANNED` 반려 · 되감기 표지는 기간 없는 것만
  4. 내레이션 = 화면 — 전수 프로브(여유 없는 창·정지·쌓기·뮤트도 시작 고정으로 대조) · 손편집 덮개 사후 검증
     (`verify_covers`) · 되돌림 소진 뒤 잔존은 선택 자리 제거 / 필수 자리 실패
  5. 엔딩 질문형은 강제하지 않는다(프롬프트 문구만)
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v3_story_flow import ANSWERS, GRID, IDX, ROWS_BY, S2, _beats, _group  # noqa: E402

from app.v3 import stage4  # noqa: E402
from app.v3 import story_flow as sf  # noqa: E402
from app.v3.story_flow import cover as cv  # noqa: E402
from app.v3.story_flow import narration as nr  # noqa: E402
from app.v3.story_flow import select as sl  # noqa: E402


# ── 3. 시간 경과 단정 금지 ─────────────────────────────────────────────────

def test_time_assertion_rejected_and_rewind_markers_have_no_duration():
    for bad in ("사실 시작은 며칠 전,", "그날 밤 남편이 나갔죠.", "다음 날 아침 다시 만났는데,", "몇 시간 후 폐건물에 도착했죠."):
        obj, pr, _ = nr.validate_narrations({"narrations": [{"before_beat": 0, "text": bad, "closed": True}]}, 1)
        assert obj is None and any("시간 경과" in p for p in pr), bad
    # 기간 없는 표지는 통과하고 되감기 표지로도 인정된다
    ok = {"narrations": [{"before_beat": 0, "text": "훅.", "closed": True},
                         {"before_beat": 1, "text": "사실 시작은, 차 안이었죠.", "closed": True}]}
    obj, pr, _ = nr.validate_narrations(ok, 2, required={0, 1}, rewind={1})
    assert obj is not None and not pr
    assert nr.time_assertion("이야기는 이렇게 시작됐죠,") is None
    # 되감기 표지 목록 자체에 기간 어휘가 없다(둘이 갈리면 표지가 곧 반려 사유가 된다)
    for m in nr.REWIND_MARKERS:
        assert nr.time_assertion(m) is None, m
    assert "시간 경과를 단정하지 마라" in nr.PROMPT and "몇 시간 전" not in nr.PROMPT.split("되감기")[1][:200]


# ── 1. 첫 3초 정지 없음 ────────────────────────────────────────────────────

def _beats_with_cover(cover: dict, head_trim: float | None = None):
    b = _beats()
    b[0]["covers"] = [cover]
    if head_trim is not None:
        b[0]["head_trim_sec"] = head_trim
    return b


def test_opening_motion_hold_cut_speech_cases():
    grid = dict(GRID, scene_cuts=[])
    # 정지(hold)가 3초 안에서 시작 → 위반
    beats = _beats_with_cover({"position": "before", "kind": "hold", "t_in": 6.5, "t_out": 7.0, "hold_sec": 2.0})
    ok, why, _ = cv.opening_motion(beats, IDX, grid)
    assert not ok and "정지 화면" in why
    # 3.5초짜리 무성 덮개(컷 없음) 뒤에 대사 → 3초 안엔 아무 사건도 없다 → 위반
    beats = _beats_with_cover({"position": "before", "kind": "broll_scene", "t_in": 6.5, "t_out": 9.0})
    ok, why, _ = cv.opening_motion(beats, IDX, grid, limit=2.4)
    assert not ok and "컷 전환·발화" in why
    # 같은 덮개라도 grid 장면 전환이 그 안에 있으면 통과(편집본 1.2s)
    ok, _, ev = cv.opening_motion(beats, IDX, dict(GRID, scene_cuts=[7.7]), limit=2.4)
    assert ok and ev[0] == {"at": 1.2, "kind": "cut", "why": "장면 전환"}
    # 짧은 덮개 뒤 유성 조각 시작 = 발화(편집본 2.0s) → 통과. 조각 경계가 원본 불연속이면 컷도 함께 센다
    beats = _beats_with_cover({"position": "before", "kind": "broll_scene", "t_in": 6.5, "t_out": 8.5})
    ok, _, ev = cv.opening_motion(beats, IDX, grid)
    assert ok and {e["kind"] for e in ev} == {"cut", "speech"} and ev[0]["at"] == 2.0
    # 덮개 없이 대사로 열면 0초 발화 → 통과 · 조각 끝 경계의 컷(3.0s)도 '3초 안'
    assert cv.opening_motion(_beats(), IDX, grid)[0]
    beats = _beats_with_cover({"position": "before", "kind": "broll_scene", "t_in": 6.5, "t_out": 9.5})
    ok, _, ev = cv.opening_motion(beats, IDX, dict(GRID, scene_cuts=[9.5]))
    assert ok and ev[0]["at"] == 3.0
    assert cv.opening_motion([], IDX, grid)[0]


def test_lines_prompt_forbids_static_hook():
    assert "첫 3초가 정지면 안 된다" in sl.LINES_PROMPT
    assert "첫 3초 안에 컷 전환이나 발화가 하나는 있어야" in nr.PROMPT


# ── 4-①. 전수 프로브 — 여유 없는 창·정지·쌓기도 시작 고정으로 대조 ─────────

def _fake_probe(calls):
    def fake(gemini, video, out_dir, tag, win, L, text, refers, grid, span_index,
             log=print, focus=None, fixed_start=None):
        calls.append({"tag": tag, "win": dict(win), "fixed_start": fixed_start, "L": L})
        start = fixed_start if fixed_start is not None else win["w0"]
        return {"start": start, "snap": "fixed" if fixed_start is not None else "raw", "raw": start,
                "reason": "r", "confidence": "high", "text_matches": True, "seen": "s",
                "probe_window": [win["w0"], win["w1"]]}
    return fake


def test_designated_window_without_slack_is_still_verified(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(cv, "run_probe", _fake_probe(calls))
    g = _group(("before", 1), ["다리 문장."], [2.0])
    g["cover_ids"] = ["sp0003"]                      # 6.5~9.0 = 2.5s · L = 2.15 → 여유 0.35 < 1.0
    cover = cv.choose_cover(("before", 1), g, _beats(), IDX, ROWS_BY, GRID, gemini=object(),
                            video=Path("v"), out_dir=Path("o"), budget={"left": 5, "used": 0},
                            log=lambda *a: None)
    assert cover["kind"] == "designated" and cover["t_in"] == 6.5
    assert calls and calls[0]["fixed_start"] == 6.5 and cover["probe"]["snap"] == "fixed"
    assert cover["probe"]["text_matches"] is True


def test_hold_cover_gets_verify_probe_and_cache_prevents_recall(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(cv, "run_probe", _fake_probe(calls))
    idx = copy.deepcopy(IDX)
    idx["sp0003"]["screen_text"] = "카톡: 오늘 밤에 봐"      # 자료화면 → hold 경로
    g = _group(("before", 1), ["긴 문장이라 붙잡는다."], [4.0])
    g["cover_ids"] = ["sp0003"]
    g["hold"] = True
    cache: dict = {}
    budget = {"left": 5, "used": 0}
    cover = cv.choose_cover(("before", 1), g, _beats(), idx, ROWS_BY, GRID, gemini=object(),
                            video=Path("v"), out_dir=Path("o"), budget=budget, probe_cache=cache,
                            log=lambda *a: None)
    assert cover["kind"] == "hold" and cover["probe"]["verify_only"] is True
    assert cover["probe"]["start"] == 6.5 and budget["used"] == 1 and len(cache) == 1
    # 같은 창·같은 문장은 캐시 — 재배치에서 재호출 없음
    cover2 = cv.choose_cover(("before", 1), g, _beats(), idx, ROWS_BY, GRID, gemini=object(),
                             video=Path("v"), out_dir=Path("o"), budget=budget, probe_cache=cache,
                             log=lambda *a: None)
    assert cover2["probe"]["verify_only"] is True and budget["used"] == 1 and len(calls) == 1


# ── 4-②. 손편집 덮개 사후 검증 ───────────────────────────────────────────────

def test_verify_covers_probes_unjudged_covers_and_writes_back(monkeypatch):
    calls: list[dict] = []

    def fake(gemini, video, out_dir, tag, win, L, text, refers, grid, span_index,
             log=print, focus=None, fixed_start=None):
        calls.append(tag)
        bad = "골드버튼" in text
        return {"start": fixed_start, "snap": "fixed", "raw": fixed_start, "reason": "r", "confidence": "high",
                "text_matches": not bad, "seen": "소파" if bad else "s", "probe_window": [win["w0"], win["w1"]]}
    monkeypatch.setattr(cv, "run_probe", fake)
    doc = {"beats": [
        {"number": 0, "covers": [{"position": "before", "kind": "designated", "t_in": 6.5, "t_out": 9.0,
                                  "span_ids": ["sp0003"], "probe": None}]},
        {"number": 1, "covers": [{"position": "before", "kind": "hold", "t_in": 0.0, "t_out": 2.0, "hold_sec": 1.0,
                                  "span_ids": ["sp0000"], "probe": {"text_matches": True, "start": 0.0}},
                                 {"position": "after", "kind": "tail", "t_in": 14.0, "t_out": 17.0,
                                  "span_ids": ["sp0006"], "probe": None}]}],
        "narration_cues": [{"beat": 0, "text": "골드버튼을 받는 부부,", "source_time_sec": 6.6},
                           {"beat": 1, "text": "훅.", "source_time_sec": 0.1},
                           {"beat": 1, "text": "여운.", "source_time_sec": 14.1}]}
    bad = cv.verify_covers(doc, IDX, GRID, gemini=object(), video=Path("v"), out_dir=Path("o"), log=lambda *a: None)
    assert calls == ["verify_0_before", "verify_1_after"]          # 판정 있는 hold 는 건너뛴다
    assert len(bad) == 1 and bad[0]["anchor"] == "before0" and bad[0]["seen"] == "소파"
    c0 = doc["beats"][0]["covers"][0]
    assert c0["probe"]["verify_only"] is True and c0["probe"]["text_matches"] is False
    assert doc["beats"][1]["covers"][1]["probe"]["text_matches"] is True
    # 사람 편집은 제거하지 않는다(덮개 그대로) · gemini 없으면 no-op
    assert c0["t_in"] == 6.5
    assert cv.verify_covers(doc, IDX, GRID, gemini=None, video=None, out_dir=None) == []


# ── 4-③. 되돌림 소진 뒤 잔존 — 선택 자리 제거 · 필수 자리 실패 ──────────────

def _run_with_contradiction(tmp_path, monkeypatch, bad_anchor: str):
    answers = [copy.deepcopy(a) for a in ANSWERS] + [copy.deepcopy(ANSWERS[3])] * sf.COVER_REASK_MAX
    prompts: list[str] = []

    def fake_call(g, p):
        prompts.append(p)
        return answers.pop(0)
    monkeypatch.setattr(sf, "call_json", fake_call)

    def fake_probe(gemini, video, out_dir, tag, win, L, text, refers, grid, span_index,
                   log=print, focus=None, fixed_start=None):
        start = fixed_start if fixed_start is not None else cv.arithmetic_start(win, L)
        return {"start": start, "snap": "fixed", "raw": start, "reason": "r", "confidence": "high",
                "text_matches": tag != bad_anchor, "seen": "다른 장면", "probe_window": [win["w0"], win["w1"]]}
    monkeypatch.setattr(cv, "run_probe", fake_probe)

    def synth(text, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return round(0.3 + len("".join(text.split())) / 8.4, 3)

    class G:
        class config:
            flash_model_name = "x"
    doc, audit = sf.run_story_flow(G(), S2, GRID, work_title="T", target_sec=20, max_sec=30,
                                   output_dir=tmp_path, synth_fn=synth, probe=True, video_path=Path("v"),
                                   log=lambda *a: None)
    return doc, audit, prompts


def test_persistent_contradiction_on_optional_narration_is_removed(tmp_path, monkeypatch):
    doc, audit, prompts = _run_with_contradiction(tmp_path, monkeypatch, "after1")
    # 걸음 4 가 COVER_REASK_MAX 번 다시 돌았고(반려 사유에 모순이 실린다) 끝내 남은 after1 은 빠졌다
    assert len(prompts) == 4 + sf.COVER_REASK_MAX
    assert "모순된다" in prompts[4]
    assert [d["anchor"] for d in doc["narration_dropped"]] == ["after1"]
    assert audit["narration_contradictions_dropped"][0]["text"] == "이게 위험한 시작이었죠."
    texts = [c["text"] for c in doc["narration_cues"]]
    assert texts == ["옥상에서 마주친 두 사람,", "말다툼이 시작됐죠.", "그러다 핵심이 나왔는데,"]
    assert not any(c["position"] == "after" for b in doc["beats"] for c in b["covers"])
    # 남은 덮개는 재배치돼도 프로브 판정이 살아 있다(전수 프로브)
    assert all(c["probe"] and c["probe"]["text_matches"] is True for b in doc["beats"] for c in b["covers"])
    assert audit["opening"]["ok"] is True


def test_persistent_contradiction_on_required_narration_fails_loud(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="필수 자리"):
        _run_with_contradiction(tmp_path, monkeypatch, "before0")


# ── 2. 반응 라벨은 들은 사람에게 ──────────────────────────────────────────────

TL = [{"clip_start_sec": 100.0, "clip_end_sec": 104.0}, {"clip_start_sec": 200.0, "clip_end_sec": 206.0}]
DLG = [{"start_sec": 0.5, "end_sec": 1.5, "text": "너 바람피니?", "speaker": "김혜수"},
       {"start_sec": 4.5, "end_sec": 5.5, "text": "무슨 소리야", "speaker": "김지훈"}]


def _lb(**kw):
    base = {"text": "(동공지진)", "anchor": "G0", "offset_sec": 0.0, "duration_sec": 1.5,
            "x": 0.3, "y": 0.4, "color": "yellow", "fx": "shake"}
    base.update(kw)
    return base


def _validate(label):
    ev, cl = stage4.label_events(DLG, TL), stage4.edited_clip_windows(TL)
    return stage4.validate_style_response({"labels": [label]}, n_beats=1, band=(0.2, 0.8),
                                          duration=10.0, events=ev, clips=cl)


def test_label_events_carry_speaker_and_prompt_shows_it():
    ev = stage4.label_events(DLG, TL)
    assert next(e for e in ev if e["id"] == "L0")["speaker"] == "김혜수"
    assert next(e for e in ev if e["id"] == "G0")["speaker"] == "김혜수"
    lines, _ = stage4.label_events_block(ev)
    assert "화자: 김혜수" in lines
    assert "들은 사람에게" in stage4.STYLE_PROMPT and "person_visible" in stage4.STYLE_PROMPT


def test_reaction_label_requires_listener():
    styled, _, notes = _validate(_lb(person="김지훈", person_visible=True))
    assert styled["labels"][0]["person"] == "김지훈" and styled["labels"][0]["anchor"] == "G0"
    styled, _, notes = _validate(_lb(person="김혜수"))            # 화자 본인
    assert styled["labels"] == [] and any("화자 본인" in n for n in notes)
    styled, _, notes = _validate(_lb())                             # person 없음
    assert styled["labels"] == [] and any("person(청자)" in n for n in notes)
    styled, _, notes = _validate(_lb(person="김지훈", person_visible=False))
    assert styled["labels"] == [] and any("얼굴이 안 보인다" in n for n in notes)
    # 대사 없는 컷(C 앵커)은 person 없이도 된다 · identity 는 종전 규칙 그대로
    styled, _, _ = _validate(_lb(anchor="C1", text="(현장 급습)"))
    assert len(styled["labels"]) == 1
    styled, _, _ = _validate(_lb(kind="identity", person="김지훈", text="(남편)"))
    assert len(styled["labels"]) == 1


def test_reaction_person_must_be_in_clip_characters():
    ev, cl = stage4.label_events(DLG, TL), stage4.edited_clip_windows(TL)
    facts = {"screen_clips": set(), "clip_characters": {0: ["김혜수", "김지훈"], 1: ["류지수"]}, "register": []}
    styled, _, notes = stage4.validate_style_response(
        {"labels": [_lb(person="류지수")]}, n_beats=1, band=(0.2, 0.8), duration=10.0,
        events=ev, clips=cl, label_facts=facts)
    assert styled["labels"] == [] and any("그 컷(C0)의 인물" in n for n in notes)


# ── 5. 엔딩 질문형은 강제하지 않는다 ────────────────────────────────────────

def test_ending_question_is_optional_in_prompt_only():
    assert "질문으로 끝낼 수도 있다" in nr.PROMPT and "필수는 아니다" in nr.PROMPT
    ok = {"narrations": [{"before_beat": 0, "text": "훅.", "closed": True},
                         {"after_last": True, "text": "불길함이 시작됐죠.", "closed": True}]}
    assert nr.validate_narrations(ok, 1)[0] is not None


# ── 1-b. 훅 화면은 눈길을 끄는 조각(★, importance ≥4) — 2026-09-11 4편 「옥상 와이드」 지적 ─────

def test_hook_cover_must_be_high_importance():
    idx = {"sp1": {"importance": 2, "scene_script": "서 있다", "is_audio": False},
           "sp2": {"importance": 4, "scene_script": "불길이 솟구친다", "is_audio": False},
           "sp3": {"importance": 3, "scene_script": "걷는다", "is_audio": False}}
    base = lambda ids: {"narrations": [{"before_beat": 0, "text": "훅.", "cover": ids, "closed": True}]}
    obj, pr, _ = nr.validate_narrations(base(["sp1"]), 1, available={"sp1", "sp2", "sp3"}, span_index=idx)
    assert obj is None and any("밋밋" in p and "sp2" in p for p in pr)
    assert nr.validate_narrations(base(["sp2"]), 1, available={"sp1", "sp2", "sp3"}, span_index=idx)[0] is not None
    obj, pr, _ = nr.validate_narrations({"narrations": [{"before_beat": 0, "text": "훅.", "closed": True}]}, 1,
                                        available={"sp1", "sp2"}, span_index=idx)
    assert obj is None and any("짚지 않았다" in p for p in pr)
    # ★ 조각이 아예 없으면 있는 것 중 최고값으로 낮춘다(막히지 않는다)
    assert nr.validate_narrations(base(["sp3"]), 1, available={"sp1", "sp3"}, span_index=idx)[0] is not None
    # span_index 미지정(종전 호출)은 검사 없음
    assert nr.validate_narrations(base(["sp1"]), 1, available={"sp1", "sp2"})[0] is not None
    assert "★ 표시된 조각" in nr.PROMPT
    block = nr.available_block(["sp1", "sp2"], {k: dict(v, t_in=0.0, t_out=1.0) for k, v in idx.items()}, {})
    assert "★ 불길이 솟구친다" in block and "★ 서 있다" not in block


def test_hook_cover_pool_is_limited_to_screens_before_the_hook():
    idx = {"a": {"importance": 2, "t_in": 0.0, "t_out": 1.0, "is_audio": False},
           "b": {"importance": 5, "t_in": 9.0, "t_out": 10.0, "is_audio": False}}      # 훅 첫 조각(5.0) 뒤 — 쓸 수 없다
    r = {"narrations": [{"before_beat": 0, "text": "훅.", "cover": ["a"], "closed": True}]}
    assert nr.validate_narrations(r, 1, available={"a", "b"}, span_index=idx, hook_before_t=5.0)[0] is not None
    assert nr.validate_narrations(r, 1, available={"a", "b"}, span_index=idx)[0] is None       # 제한 없으면 b 가 기준


def test_star_applies_to_first_scene_only_and_label_probe_checks_who_did_what():
    assert "★ 는 **첫 화면에만** 해당한다" in nr.PROMPT and "관련성·자연스러움이 우선" in nr.PROMPT
    assert "재미·시선이 최우선" in sl.LINES_PROMPT
    assert "누가 무엇을 했는지 틀리게" in stage4.LABEL_PROBE_PROMPT
