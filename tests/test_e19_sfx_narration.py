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
    assert "sfx_audio=_narr_sfx or None" in src
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
