"""내레이션 시작 효과음 배치 (app/modules/sfx_narration.py).

핵심 계약 네 가지를 지킨다.
  1. 번들 매니페스트가 없으면 아무것도 안 한다 (렌더 종전과 동일)
  2. 배치 시각 = cue.start_sec + (리드인 - 피크) * speed
  3. 같은 **계열**이 연달아 나오지 않는다 (이름이 달라도 같은 소리면 같은 계열)
  4. 선택은 시드에 대해 결정적이다 (재렌더가 같은 소리를 재현한다)
"""
import json

import pytest

from app.modules import sfx_narration as sn


def _bundle(tmp_path, items, *, narration=None):
    """app_root 흉내 — assets/sfx/ 에 매니페스트와 (빈) 음원 파일을 놓는다."""
    d = tmp_path / "assets" / "sfx"
    d.mkdir(parents=True)
    for it in items:
        (d / it["file"]).write_bytes(b"RIFF0000WAVEfmt ")   # 존재만 확인한다
    doc = {"narration": narration if narration is not None
           else {"enabled": True, "gain_db": 0.0, "opening_max_t": 1.5,
                 "no_repeat_window": 3},
           "sfx": items}
    (d / sn.MANIFEST_NAME).write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


ITEMS = [
    {"id": "swoosh-015-tight", "file": "a.wav", "family": "swoosh",
     "opening": [], "mid": ["line_start"], "peak_sec": 0.10},
    {"id": "swoosh-015", "file": "b.wav", "family": "swoosh",
     "opening": [], "mid": ["line_start"], "peak_sec": 0.25},
    {"id": "whoosh-2", "file": "c.wav", "family": "whoosh",
     "opening": [], "mid": ["line_start"], "peak_sec": 0.03},
    {"id": "impact-thud", "file": "d.wav", "family": "impact-thud",
     "opening": ["opening_light"], "mid": ["line_start"], "peak_sec": 0.02},
    {"id": "cinematic-boom", "file": "e.wav", "family": "cinematic-boom",
     "opening": ["opening"], "mid": [], "peak_sec": 0.02},
]


def _cues(starts):
    # path 는 비우지 않는다 — 빈 경로는 리드인 측정을 건너뛰는(0.0) 별개 분기다.
    return [{"cue_index": i, "path": f"/nonexistent/tts_cue_{i}.mp3",
             "cue": {"start_sec": s}} for i, s in enumerate(starts)]


def test_no_manifest_is_noop(tmp_path):
    """번들이 없으면 빈 리스트 — 렌더 필터가 종전과 바이트 동일해야 한다."""
    assert sn.place_narration_sfx(_cues([1.0, 5.0]), app_root=tmp_path,
                                  run_dir=tmp_path / "run", seed="x") == []


def test_disabled_flag_is_noop(tmp_path):
    root = _bundle(tmp_path, ITEMS, narration={"enabled": False})
    assert sn.place_narration_sfx(_cues([1.0]), app_root=root,
                                  run_dir=tmp_path / "run", seed="x") == []


def test_start_subtracts_peak_and_adds_lead_in(tmp_path, monkeypatch):
    """배치 시각 = cue.start_sec + (리드인 - 피크). 피크가 발화 시각에 떨어져야 한다."""
    root = _bundle(tmp_path, ITEMS)
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.12)
    out = sn.place_narration_sfx(_cues([10.0]), app_root=root,
                                 run_dir=tmp_path / "run", seed="s")
    assert len(out) == 1
    peak = out[0]["_narration"]["peak_sec"]
    assert out[0]["start_sec"] == pytest.approx(10.0 + 0.12 - peak, abs=1e-3)


def test_speed_scales_the_correction(tmp_path, monkeypatch):
    """리드인·피크는 출력 시각의 양이므로 편집 좌표로 되돌릴 때 speed 를 곱한다."""
    root = _bundle(tmp_path, ITEMS)
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.20)
    out = sn.place_narration_sfx(_cues([10.0]), app_root=root,
                                 run_dir=tmp_path / "run", seed="s", speed=2.0)
    peak = out[0]["_narration"]["peak_sec"]
    assert out[0]["start_sec"] == pytest.approx(10.0 + (0.20 - peak) * 2.0, abs=1e-3)


def test_never_negative_start(tmp_path, monkeypatch):
    """피크가 cue 보다 앞서면 0 으로 클램프한다(음수 adelay 는 렌더가 못 받는다)."""
    root = _bundle(tmp_path, ITEMS)
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.0)
    out = sn.place_narration_sfx(_cues([0.01]), app_root=root,
                                 run_dir=tmp_path / "run", seed="s")
    assert out[0]["start_sec"] >= 0.0


def test_no_consecutive_family(tmp_path, monkeypatch):
    """swoosh-015 와 swoosh-015-tight 는 이름이 달라도 같은 소리다 — 연달아 나오면 안 된다."""
    root = _bundle(tmp_path, ITEMS)
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.05)
    for seed in ("a", "b", "c", "d", "e", "f"):
        out = sn.place_narration_sfx(_cues([5.0, 10.0, 15.0, 20.0]), app_root=root,
                                     run_dir=tmp_path / f"run{seed}", seed=seed)
        fams = [o["_narration"]["family"] for o in out]
        assert all(x != y for x, y in zip(fams, fams[1:])), (seed, fams)


def test_deterministic_for_same_seed(tmp_path, monkeypatch):
    root = _bundle(tmp_path, ITEMS)
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.05)
    a = sn.place_narration_sfx(_cues([2.0, 9.0]), app_root=root,
                               run_dir=tmp_path / "r1", seed="same")
    b = sn.place_narration_sfx(_cues([2.0, 9.0]), app_root=root,
                               run_dir=tmp_path / "r2", seed="same")
    assert [x["_narration"]["id"] for x in a] == [x["_narration"]["id"] for x in b]


def test_first_early_cue_takes_opening(tmp_path, monkeypatch):
    """1.5초 이전의 첫 cue 는 도입부 — 무거운 계열에서 고른다."""
    root = _bundle(tmp_path, ITEMS)
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.0)
    out = sn.place_narration_sfx(_cues([0.4, 12.0]), app_root=root,
                                 run_dir=tmp_path / "run", seed="s")
    assert out[0]["_narration"]["tag"] == "opening"
    assert out[1]["_narration"]["tag"] == "line_start"


def test_late_first_cue_is_line_start(tmp_path, monkeypatch):
    root = _bundle(tmp_path, ITEMS)
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.0)
    out = sn.place_narration_sfx(_cues([9.0]), app_root=root,
                                 run_dir=tmp_path / "run", seed="s")
    assert out[0]["_narration"]["tag"] == "line_start"


def test_missing_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    """매니페스트에만 있고 번들에 없는 파일은 건너뛴다 — 편이 죽으면 안 된다."""
    items = [dict(ITEMS[2])] + [{"id": "ghost", "file": "nope.wav", "family": "ghost",
                                 "opening": [], "mid": ["line_start"], "peak_sec": 0.0}]
    root = _bundle(tmp_path, [items[0]])
    d = root / "assets" / "sfx"
    doc = json.loads((d / sn.MANIFEST_NAME).read_text(encoding="utf-8"))
    doc["sfx"] = items                       # ghost 는 파일 없이 등록만 한다
    (d / sn.MANIFEST_NAME).write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.0)
    out = sn.place_narration_sfx(_cues([5.0, 10.0, 15.0]), app_root=root,
                                 run_dir=tmp_path / "run", seed="s")
    assert all(o["_narration"]["id"] != "ghost" for o in out)


def test_staged_into_run_dir(tmp_path, monkeypatch):
    """체크포인트 재렌더가 번들 없이도 같은 소리를 내야 한다 — style_assets 로 복사."""
    root = _bundle(tmp_path, ITEMS)
    monkeypatch.setattr(sn, "lead_in_sec", lambda p: 0.0)
    run = tmp_path / "run"
    out = sn.place_narration_sfx(_cues([5.0]), app_root=root, run_dir=run, seed="s")
    assert out[0]["path"].parent == run / sn.STAGE_SUBDIR
    assert out[0]["path"].is_file()


def test_bundled_manifest_matches_bundled_files():
    """실제 번들: 매니페스트의 모든 file 이 app/assets/sfx 에 있어야 한다."""
    from pathlib import Path
    app_root = Path(__file__).resolve().parents[1] / "app"
    mf = sn.load_narration_manifest(app_root)
    if not mf:
        pytest.skip("번들에 narration_manifest.json 이 없다")
    d = app_root / "assets" / "sfx"
    missing = [it["file"] for it in mf["items"] if not (d / it["file"]).is_file()]
    assert not missing, missing
    # 도입부·문장시작 후보가 각각 하나 이상 있어야 배치가 성립한다
    assert any("opening" in (it.get("opening") or []) for it in mf["items"])
    assert any("line_start" in (it.get("mid") or []) for it in mf["items"])


def test_missing_path_falls_back_to_zero_lead_in(tmp_path):
    """cue 에 path 가 없으면 리드인 측정을 건너뛴다(0.0) — 편이 죽지 않는다."""
    root = _bundle(tmp_path, ITEMS)
    cues = [{"cue_index": 0, "path": "", "cue": {"start_sec": 10.0}}]
    out = sn.place_narration_sfx(cues, app_root=root,
                                 run_dir=tmp_path / "run", seed="s")
    peak = out[0]["_narration"]["peak_sec"]
    assert out[0]["_narration"]["lead_in_sec"] == 0.0
    assert out[0]["start_sec"] == pytest.approx(10.0 - peak, abs=1e-3)


def test_undecodable_file_lead_in_is_zero(tmp_path):
    """디코드 실패한 파일도 0.0 — 예외로 편을 죽이지 않는다."""
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio")
    assert sn.lead_in_sec(bad) == 0.0


def test_v3_render_final_wires_sfx_audio():
    """v3 render_final 이 sfx_audio 를 RenderInputs 로 넘긴다(배선 고정)."""
    import inspect
    from app.v3 import finalize
    src = inspect.getsource(finalize.render_final)
    assert "place_narration_sfx" in src
    assert "sfx_audio=_all_sfx or None" in src   # 내레이션 + 라벨 합본
    # cue_files 확정 뒤여야 리드인 실측이 성립한다
    assert src.index("cue_files = [f for f") < src.index("place_narration_sfx")


def test_v1_pipeline_wires_sfx_audio():
    """v1 run_pipeline 도 같은 함수를 쓴다 — 자리는 리소스 3분기 수렴 뒤."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("app/pipeline.py").read_text(
        encoding="utf-8")
    assert "place_narration_sfx" in src
    # 생성 elif 안이 아니라 수렴 뒤 — E19-4 가 겪은 재개 누락 버그 방지
    assert src.index("체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다") \
        < src.index("place_narration_sfx")
    assert src.index("place_narration_sfx") < src.rindex("[face avoid]")


def test_gain_comes_from_manifest(tmp_path):
    """게인은 매니페스트가 정한다 — 코드 기본값이 아니라."""
    root = _bundle(tmp_path, ITEMS, narration={"enabled": True, "gain_db": -6.0})
    out = sn.place_narration_sfx(_cues([5.0]), app_root=root,
                                 run_dir=tmp_path / "run", seed="s")
    assert out[0]["gain_db"] == -6.0


def test_bundled_gain_is_minus_six():
    """번들 기본값 -6dB — 최종 loudnorm 이 트랜지언트에 반응해 밑을 누르는 것을 줄인다
    (0dB 에서 대사 위 -3.7dB · -6dB 에서 -1.2dB, 2026-09-02 실측)."""
    from pathlib import Path
    app_root = Path(__file__).resolve().parents[1] / "app"
    mf = sn.load_narration_manifest(app_root)
    if not mf:
        pytest.skip("번들에 narration_manifest.json 이 없다")
    assert mf["config"]["gain_db"] == -6.0


# ── 라벨 효과음 ──────────────────────────────────────────────────────────────

LABEL_ITEMS = ITEMS + [
    {"id": "pop-a", "file": "pa.wav", "family": "pop-a",
     "opening": [], "mid": ["label"], "peak_sec": 0.01},
    {"id": "pop-b", "file": "pb.wav", "family": "pop-b",
     "opening": [], "mid": ["label"], "peak_sec": 0.02},
    {"id": "pop-c", "file": "pc.wav", "family": "pop-c",
     "opening": [], "mid": ["label"], "peak_sec": 0.03},
    {"id": "pop-up-something", "file": "pu.wav", "family": "pop-up-something",
     "opening": [], "mid": ["label_visual_only"], "peak_sec": 0.04},
]


def _labels(spans):
    return [{"start_sec": a, "end_sec": b, "text": f"L{i}"}
            for i, (a, b) in enumerate(spans)]


def _lbundle(tmp_path, *, mode="all", enabled=True, items=None):
    return _bundle(tmp_path, items if items is not None else LABEL_ITEMS,
                   narration={"enabled": True, "gain_db": -6.0, "opening_max_t": 1.5,
                              "no_repeat_window": 3,
                              "label": {"enabled": enabled, "gain_db": -6.0,
                                        "mode": mode, "no_repeat_window": 3}})


def test_label_off_is_noop(tmp_path):
    """label 절이 꺼져 있으면 빈 리스트 — 렌더 종전과 동일."""
    root = _lbundle(tmp_path, enabled=False)
    assert sn.place_label_sfx(_labels([(5.0, 8.0)]), app_root=root,
                              run_dir=tmp_path / "r", seed="s") == []


def test_label_peak_lands_on_appearance(tmp_path):
    """라벨은 리드인 보정이 없다 — 피크가 등장 시각에 떨어지게 앞당기기만 한다."""
    root = _lbundle(tmp_path)
    out = sn.place_label_sfx(_labels([(10.0, 13.0)]), app_root=root,
                             run_dir=tmp_path / "r", seed="s")
    assert out[0]["start_sec"] == pytest.approx(10.0 - out[0]["_label"]["peak_sec"],
                                                abs=1e-3)


def test_quiet_label_prefers_visual_only_sound(tmp_path):
    """대사·내레이션이 없는 자리면 화면 전용 소리를 먼저 쓴다."""
    root = _lbundle(tmp_path)
    out = sn.place_label_sfx(_labels([(30.0, 33.0)]), app_root=root,
                             run_dir=tmp_path / "r", seed="s",
                             busy_windows=[(0.0, 10.0)])
    assert out[0]["_label"]["quiet"] is True
    assert out[0]["_label"]["id"] == "pop-up-something"


def test_busy_label_never_gets_visual_only_sound(tmp_path):
    """소리가 깔린 자리에는 그 소리를 쓰지 않는다(사용자 분류: 대사 중에는 X)."""
    root = _lbundle(tmp_path)
    out = sn.place_label_sfx(_labels([(5.0, 8.0)]), app_root=root,
                             run_dir=tmp_path / "r", seed="s",
                             busy_windows=[(4.0, 9.0)])
    assert out[0]["_label"]["quiet"] is False
    assert out[0]["_label"]["id"] != "pop-up-something"


def test_no_busy_windows_means_treat_as_busy(tmp_path):
    """창 정보가 없으면 보수적으로 '소리 있음' — 화면 전용 소리를 안 쓴다."""
    root = _lbundle(tmp_path)
    out = sn.place_label_sfx(_labels([(5.0, 8.0)]), app_root=root,
                             run_dir=tmp_path / "r", seed="s")
    assert out[0]["_label"]["quiet"] is False


def test_quiet_only_mode_skips_busy_labels(tmp_path):
    root = _lbundle(tmp_path, mode="quiet_only")
    out = sn.place_label_sfx(_labels([(5.0, 8.0), (30.0, 33.0)]), app_root=root,
                             run_dir=tmp_path / "r", seed="s",
                             busy_windows=[(4.0, 9.0)])
    assert [o["_label"]["at"] for o in out] == [30.0]


def test_all_mode_places_every_label(tmp_path):
    root = _lbundle(tmp_path, mode="all")
    out = sn.place_label_sfx(_labels([(5.0, 8.0), (30.0, 33.0)]), app_root=root,
                             run_dir=tmp_path / "r", seed="s",
                             busy_windows=[(4.0, 9.0)])
    assert len(out) == 2


def test_visual_only_falls_back_when_pool_empty(tmp_path):
    """화면 전용 소리가 번들에 없으면 일반 라벨 소리로 떨어진다(드롭 금지)."""
    items = [i for i in LABEL_ITEMS if i["id"] != "pop-up-something"]
    root = _lbundle(tmp_path, items=items)
    out = sn.place_label_sfx(_labels([(30.0, 33.0)]), app_root=root,
                             run_dir=tmp_path / "r", seed="s", busy_windows=[(0.0, 1.0)])
    assert len(out) == 1 and out[0]["_label"]["quiet"] is True


def test_label_no_consecutive_family(tmp_path):
    root = _lbundle(tmp_path)
    for seed in ("a", "b", "c", "d"):
        out = sn.place_label_sfx(_labels([(5, 6), (10, 11), (15, 16), (20, 21)]),
                                 app_root=root, run_dir=tmp_path / f"r{seed}", seed=seed,
                                 busy_windows=[(0, 100)])
        fams = [o["_label"]["family"] for o in out]
        assert all(x != y for x, y in zip(fams, fams[1:])), (seed, fams)


def test_bundled_label_pool_exists():
    """번들에 라벨용·화면전용 소리가 각각 있어야 배치가 성립한다."""
    from pathlib import Path
    app_root = Path(__file__).resolve().parents[1] / "app"
    mf = sn.load_narration_manifest(app_root)
    if not mf:
        pytest.skip("번들 없음")
    tags = [t for it in mf["items"] for t in (it.get("mid") or [])]
    assert tags.count("label") >= 3
    assert "label_visual_only" in tags


def test_v3_render_final_wires_label_sfx():
    import inspect
    from app.v3 import finalize
    src = inspect.getsource(finalize.render_final)
    assert "place_label_sfx" in src and "sfx_audio=_all_sfx or None" in src


def test_label_sfx_source_is_rendered_labels_not_plan_labels():
    """소스는 실제로 그려지는 `labels` 여야 한다.

    human_flow 는 story 비트의 labels 가 비어 있고 Stage 4 가 직접 쓴
    v3_style.labels 가 화면에 나간다 — plan_labels 를 보면 라벨이 떠 있는데
    효과음은 0개가 된다(2026-09-03 실측 94a86e4c: ASS 2줄 · plan_labels 0).
    """
    import inspect
    from app.v3 import finalize
    src = inspect.getsource(finalize.render_final)
    call = src[src.index("place_label_sfx("):]
    first_arg = call[len("place_label_sfx("):call.index(",")].strip()
    assert first_arg == "labels", first_arg
    # 그리고 labels 조립이 끝난 뒤여야 한다
    assert src.index("texts_path = output_dir") < src.index("place_label_sfx")


def test_visual_only_pool_of_one_does_not_repeat(tmp_path):
    """화면 전용 후보가 하나뿐이어도 연달아 같은 소리를 쓰지 않는다.

    실측(94a86e4c_run1): 라벨 2개가 둘 다 조용해 pop-up-something 이 연속으로
    나갔다 — 전용 소리를 한 번 포기하고 일반 라벨 풀로 간다.
    """
    root = _lbundle(tmp_path)
    out = sn.place_label_sfx(_labels([(30.0, 32.0), (40.0, 42.0)]), app_root=root,
                             run_dir=tmp_path / "r", seed="s", busy_windows=[(0.0, 1.0)])
    ids = [o["_label"]["id"] for o in out]
    assert len(ids) == 2 and ids[0] != ids[1], ids
    assert ids[0] == "pop-up-something"          # 첫 번째는 전용 소리를 쓴다


def test_bundled_peak_sec_is_the_attack_not_the_max():
    """peak_sec 은 최대 진폭이 아니라 **타격**(최대치 70% 첫 도달)이어야 한다.

    몸통이 부푸는 소리는 최대 진폭이 한참 뒤다 — impact-cinematic-boom-02 는
    최대 0.303s · 타격 0.025s 였고, 최대로 정렬하면 타격이 278ms 앞으로 샜다.
    """
    import array
    import subprocess
    from pathlib import Path
    app_root = Path(__file__).resolve().parents[1] / "app"
    mf = sn.load_narration_manifest(app_root)
    if not mf:
        pytest.skip("번들 없음")
    bad = []
    for it in mf["items"]:
        f = app_root / "assets" / "sfx" / it["file"]
        pcm = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(f), "-ac", "1",
                              "-ar", "48000", "-f", "s16le", "-"],
                             capture_output=True).stdout
        a = array.array("h"); a.frombytes(pcm[:len(pcm) // 2 * 2])
        if not a:
            continue
        pk = max(max(a), -min(a)) or 1
        att = next((i for i, v in enumerate(a) if abs(v) >= pk * 0.7), 0) / 48000
        if abs(att - float(it.get("peak_sec", 0))) > 0.01:
            bad.append((it["id"], round(att, 4), it.get("peak_sec")))
    assert not bad, bad


# ── 동시 타격 시 내레이션 우선 (사용자 지시 2026-09-03) ──────────────────────

def _n(start, peak): return {"start_sec": start, "_narration": {"peak_sec": peak}}
def _l(start, peak): return {"start_sec": start, "_label": {"peak_sec": peak, "at": start}}


def test_simultaneous_hit_drops_the_label():
    """같은 순간에 때리면 내레이션만 남는다."""
    keep, dropped = sn.drop_label_collisions([_n(9.9, 0.1)], [_l(9.95, 0.05)])
    assert keep == [] and len(dropped) == 1


def test_separated_hits_both_survive():
    """충분히 벌어지면 둘 다 남는다 — 겹침 아님."""
    keep, dropped = sn.drop_label_collisions([_n(9.9, 0.1)], [_l(12.0, 0.05)])
    assert len(keep) == 1 and dropped == []


def test_collision_compares_hit_not_start():
    """비교는 start_sec 이 아니라 타격(start+peak)이다.

    start 는 0.30 벌어졌지만 타격은 둘 다 10.0 — 시작으로 재면 놓친다.
    """
    keep, dropped = sn.drop_label_collisions([_n(9.70, 0.30)], [_l(9.99, 0.01)])
    assert keep == [] and len(dropped) == 1


def test_different_start_same_hit_is_not_false_positive():
    """반대로 start 가 같아도 타격이 벌어지면 겹침이 아니다."""
    keep, dropped = sn.drop_label_collisions([_n(10.0, 0.30)], [_l(10.0, 0.01)])
    assert len(keep) == 1 and dropped == []


def test_no_narration_keeps_every_label():
    keep, dropped = sn.drop_label_collisions([], [_l(3.0, 0.01), _l(9.0, 0.02)])
    assert len(keep) == 2 and dropped == []


def test_v3_render_final_applies_collision_drop():
    import inspect
    from app.v3 import finalize
    src = inspect.getsource(finalize.render_final)
    assert "drop_label_collisions" in src
    # 드롭 뒤에 합쳐야 한다
    assert src.index("drop_label_collisions") < src.index("_all_sfx = _narr_sfx")
