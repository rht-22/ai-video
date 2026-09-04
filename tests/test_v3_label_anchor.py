"""라벨 앵커 + 프로브(2026-09-04) — 지금불륜 EP01 '(영혼 탈곡됨)' 실사고: Stage 4 가 6fps 초안을
보고 절대초로 적은 라벨이, watch_trim 이 잘라낸 리액션 정적 대신 다음 컷의 웃는 아이 얼굴 위에
1.6초 떠 있었다. 시각은 이벤트(L/G/C)에 앵커해 코드가 뽑고, 앵커 창을 다시 보며 프레임을 맞춘다.
"""
from __future__ import annotations

from app.v3 import stage4

TL = [{"clip_start_sec": 100.0, "clip_end_sec": 104.0},          # C0 0~4
      {"clip_start_sec": 200.0, "clip_end_sec": 202.0},          # C1 4~6
      {"clip_start_sec": 300.0, "clip_end_sec": 306.0, "hold_sec": 1.0}]   # C2 6~13
DLG = [{"start_sec": 0.5, "end_sec": 1.5, "text": "민서야 타!"},
       {"start_sec": 3.9, "end_sec": 4.0, "text": "짧게"},           # 다음 줄까지 0.1s → G 없음
       {"start_sec": 4.1, "end_sec": 5.9, "text": "대박 미쳤다"},
       {"start_sec": 7.0, "end_sec": 8.0, "text": "끝 대사"}]


def _lb(**kw):
    base = {"text": "(얼어붙음)", "anchor": "G0", "offset_sec": 0.0, "duration_sec": 1.5,
            "x": 0.3, "y": 0.4, "color": "yellow", "fx": "shake"}
    base.update(kw)
    return base


def test_label_events_lines_gaps_clips():
    ev = stage4.label_events(DLG, TL)
    ids = [e["id"] for e in ev]
    assert ids == ["L0", "G0", "L1", "L2", "L3", "G3", "C0", "C1", "C2"]
    g0 = next(e for e in ev if e["id"] == "G0")
    assert (g0["start"], g0["end"]) == (1.5, 3.9)          # 줄 끝 ~ 다음 줄 시작
    g3 = next(e for e in ev if e["id"] == "G3")
    assert (g3["start"], g3["end"]) == (8.0, 13.0)         # 마지막 줄 → 클립 끝(hold 포함)
    assert stage4.edited_clip_windows(TL)[2] == {"clip": 2, "start": 6.0, "end": 13.0}


def test_anchor_resolves_and_clamps_inside_clip():
    ev, cl = stage4.label_events(DLG, TL), stage4.edited_clip_windows(TL)
    s, e, aid, why = stage4.resolve_label_anchor(_lb(anchor="G0", offset_sec=0.3), ev, cl)
    assert (s, e, aid, why) == (1.8, 3.3, "G0", None)
    # 오프셋이 컷을 넘기면 그 클립 안으로 — C1(4~6) 에 +1.9 → 5.4 시작, 끝은 클립 끝 6.0
    s, e, _, _ = stage4.resolve_label_anchor(_lb(anchor="C1", offset_sec=1.9), ev, cl)
    assert (s, e) == (5.4, 6.0)
    # 오프셋 범위 밖은 클램프(-1.0~2.0)이고, 컷을 넘어도 **앵커 이벤트의 클립** 안에 머문다
    s, e, _, _ = stage4.resolve_label_anchor(_lb(anchor="L2", offset_sec=9.0), ev, cl)
    assert (s, e) == (5.4, 6.0)                          # C1 끝 6.0 − 0.6
    # 모르는 앵커
    s, e, aid, why = stage4.resolve_label_anchor(_lb(anchor="G9"), ev, cl)
    assert s is None and "없다" in why


def test_validate_uses_anchor_and_drops_on_disagreement():
    ev, cl = stage4.label_events(DLG, TL), stage4.edited_clip_windows(TL)
    styled, problems, notes = stage4.validate_style_response(
        {"labels": [_lb(anchor="G0", start_sec=1.6, end_sec=3.0)]},
        n_beats=1, band=(0.2, 0.8), duration=13.0, events=ev, clips=cl)
    assert not problems and styled["labels"][0]["anchor"] == "G0"
    assert (styled["labels"][0]["start_sec"], styled["labels"][0]["end_sec"]) == (1.5, 3.0)
    # 앵커와 절대초가 1.5s 넘게 갈리면 드롭 + 사유
    styled, _, notes = stage4.validate_style_response(
        {"labels": [_lb(anchor="G0", start_sec=9.0, end_sec=10.0)]},
        n_beats=1, band=(0.2, 0.8), duration=13.0, events=ev, clips=cl)
    assert styled["labels"] == [] and any("갈림" in n for n in notes)
    # 앵커 없음 → 절대초 폴백(기록) · 둘 다 없으면 드롭
    styled, _, notes = stage4.validate_style_response(
        {"labels": [{"text": "(정색)", "start_sec": 7.2, "end_sec": 8.0, "x": 0.3, "y": 0.4}]},
        n_beats=1, band=(0.2, 0.8), duration=13.0, events=ev, clips=cl)
    assert styled["labels"][0]["start_sec"] == 7.2 and "anchor" not in styled["labels"][0]
    assert any("폴백" in n for n in notes)


def test_legacy_path_without_timeline_is_unchanged():
    styled, problems, _ = stage4.validate_style_response(
        {"labels": [{"text": "(정색)", "start_sec": 3.0, "end_sec": 4.5, "x": 0.3, "y": 0.4}]},
        n_beats=1, band=(0.2, 0.8), duration=60.0)
    assert not problems and (styled["labels"][0]["start_sec"], styled["labels"][0]["end_sec"]) == (3.0, 4.5)


def test_probe_moves_drops_and_keeps():
    cl = stage4.edited_clip_windows(TL)
    labels = [{"text": "(A)", "anchor": "G0", "start_sec": 1.5, "end_sec": 3.0},
              {"text": "(B)", "anchor": "C1", "start_sec": 4.0, "end_sec": 5.5},
              {"text": "(C)", "anchor": "L3", "start_sec": 7.0, "end_sec": 8.5}]
    asked = []

    def ask(t0, t1, lb):
        asked.append((lb["text"], round(t0, 2), round(t1, 2)))
        if lb["text"] == "(A)":
            return {"fit": True, "start_sec": 1.2, "reason": "표정 꺾임"}   # 창 0.5~4.0 → 1.7
        if lb["text"] == "(B)":
            return {"fit": False, "reason": "웃는 얼굴"}
        raise RuntimeError("네트워크")
    out, audit = stage4.probe_labels(labels, cl, ask=ask, log=lambda *a: None)
    assert asked[0] == ("(A)", 0.5, 4.0)                      # 앵커 −1.0 ~ +3.0, 클립 안(0~4)
    assert [lb["text"] for lb in out] == ["(A)", "(C)"]        # (B) 드롭
    assert (out[0]["start_sec"], out[0]["end_sec"]) == (1.7, 3.2) and out[0]["probe"]["moved"] == 0.2
    assert (out[1]["start_sec"], out[1]["end_sec"]) == (7.0, 8.5) and "error" in audit[2]
    assert labels[0]["start_sec"] == 1.5                       # 입력 불변(사본)


def test_probe_budget_and_clip_clamp():
    cl = stage4.edited_clip_windows(TL)
    labels = [{"text": f"({i})", "anchor": "C2", "start_sec": 6.5, "end_sec": 8.0} for i in range(4)]
    calls = []

    def ask(t0, t1, lb):
        calls.append(1)
        return {"fit": True, "start_sec": 99.0}                # 창 밖 → 앵커값 유지
    out, audit = stage4.probe_labels(labels, cl, ask=ask, log=lambda *a: None, budget=2)
    assert len(calls) == 2 and len(out) == 4
    assert all(lb["start_sec"] == 6.5 for lb in out)
    assert "예산" in audit[2]["result"] and "창 밖" in audit[0]["result"]


def test_prompt_lists_events_and_anchor_rule():
    ev = stage4.label_events(DLG, TL)
    p = stage4.build_style_prompt(stage4.RECAP_PRESET, {"beats": []}, dialogue=DLG,
                                  events=ev, band=(0.2, 0.8))
    assert "- L0 0.5~1.5s 「민서야 타!」" in p and "· G0 정적 1.5~3.9s" in p
    assert "C0 0.0~4.0s" in p and '"anchor": "G7"' in p and "절대초를 세지 마라" in p
    legacy = stage4.build_style_prompt(stage4.RECAP_PRESET, {"beats": []}, dialogue=DLG,
                                       band=(0.2, 0.8))
    assert "- 0.5~1.5s 「민서야 타!」" in legacy


def test_run_style_probes_anchored_labels(tmp_path):
    class _G:  # run_style 은 _call_style_model 만 gemini 를 쓴다 — 여기선 monkeypatch
        pass
    resp = {"design": {}, "beats": [], "notes": "x",
            "labels": [_lb(anchor="G0", offset_sec=0.0)]}
    stage4_call = stage4._call_style_model
    stage4._call_style_model = lambda g, d, p: resp
    try:
        doc, audit = stage4.run_style(
            _G(), tmp_path / "draft.mp4", {"beats": []}, preset=stage4.RECAP_PRESET,
            dialogue=DLG, duration=13.0, band=(0.2, 0.8), timeline=TL,
            probe_ask=lambda t0, t1, lb: {"fit": True, "start_sec": 0.5, "reason": "ok"},
            log=lambda *a: None)
    finally:
        stage4._call_style_model = stage4_call
    lb = doc["v3_style"]["labels"][0]
    assert lb["anchor"] == "G0" and lb["start_sec"] == 1.0 and lb["probe"]["moved"] == -0.5
    assert audit["label_events"] == 9 and audit["label_probes"][0]["result"].startswith("이동")
