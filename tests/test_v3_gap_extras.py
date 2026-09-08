"""별건(2026-09-08 · docs/v3_gaps_handoff.md §5) 회귀 가드 — LLM·실렌더 없이 돈다.

갭 10 라벨 확장(종류·판정 동사·지시형/인물/아이러니 검증·컷 경계 클램프) · 갭 12 검출기 선택(haar
기본 = 회귀 0 · yunet 옵트인 · 화자 추적 맵은 design 키로만) · XML 내보내기 · Stage 1 intro 카드 문구.
"""
from __future__ import annotations

import json
import os
import xml.dom.minidom as minidom
from pathlib import Path

import pytest

from app.v3 import finalize, stage4
from app.v3.cli import CHANNEL_DESIGN_ARGS, V3_ONLY_DESIGN_KEYS, build_parser, channel_design_from_args

CLIPS = [{"clip": 0, "start": 0.0, "end": 4.0}, {"clip": 1, "start": 4.0, "end": 9.0}]
FACTS = {"screen_clips": {1}, "clip_characters": {0: ["박경희"], 1: ["임재홍"]},
         "register": [{"id": "r001", "setup": {"quote": "쌩깔 거야"}, "payoff": {"quote": "먼저 말 검"}}]}


def _labels(items):
    return {"design": {}, "beats": [], "labels": items}


def _run(items, facts=FACTS):
    styled, pr, notes = stage4.validate_style_response(
        _labels(items), 0, band=(0.2, 0.8), duration=9.0, clips=CLIPS, label_facts=facts)
    return styled, pr, notes


def test_label_kinds_pointer_identity_irony_and_judgment():
    ok, pr, notes = _run([
        {"text": "[딸 폰에 빨간 하트]", "kind": "pointer", "start_sec": 5.0, "end_sec": 6.5, "x": 0.5, "y": 0.5},
        {"text": "(남편 임재홍)", "kind": "identity", "person": "임재홍", "start_sec": 5.0, "end_sec": 6.0, "x": 0.5, "y": 0.5},
        {"text": "(쌩깐다더니)", "kind": "irony", "register_id": "r001", "start_sec": 1.0, "end_sec": 2.0, "x": 0.5, "y": 0.5},
    ])
    assert pr == [] and [lb["kind"] for lb in ok["labels"]] == ["pointer", "identity", "irony"]
    assert ok["labels"][2]["register_id"] == "r001"
    bad, pr, notes = _run([
        {"text": "[폰 화면]", "kind": "pointer", "start_sec": 1.0, "end_sec": 2.0, "x": 0.5, "y": 0.5},      # C0 엔 글자 없음
        {"text": "(남편 임재홍)", "kind": "identity", "person": "임재홍", "start_sec": 1.0, "end_sec": 2.0, "x": 0.5, "y": 0.5},  # C0 엔 박경희
        {"text": "(불륜 확정)", "start_sec": 1.0, "end_sec": 2.0, "x": 0.5, "y": 0.5},                        # 판정 동사
        {"text": "(아이러니)", "kind": "irony", "register_id": "r999", "start_sec": 1.0, "end_sec": 2.0, "x": 0.5, "y": 0.5},
    ])
    assert pr == [] and bad["labels"] == []
    assert any("화면 글자가 없다" in n for n in notes) and any("인물 지목" in n for n in notes)
    assert any("판정 동사" in n for n in notes) and any("레지스터" in n for n in notes)
    # 재료 없음(None) = 종전: 종류·kind 키 없음, 판정 동사만 막는다
    ok2, pr2, _ = _run([{"text": "(굳은 표정)", "start_sec": 1.0, "end_sec": 2.0, "x": 0.5, "y": 0.5}], facts=None)
    assert pr2 == [] and "kind" not in ok2["labels"][0]


def test_label_absolute_fallback_clamped_to_clip():
    ok, pr, notes = _run([{"text": "(굳은 표정)", "start_sec": 3.0, "end_sec": 6.0, "x": 0.5, "y": 0.5}])
    assert ok["labels"][0]["end_sec"] == 4.0 and any("컷 경계로 자름" in n for n in notes)
    drop, _, notes = _run([{"text": "(굳은 표정)", "start_sec": 3.7, "end_sec": 6.0, "x": 0.5, "y": 0.5}])
    assert drop["labels"] == [] and any("컷 끝에 걸림" in n for n in notes)


def test_style_prompt_lists_pointer_irony_only_with_material():
    p0 = stage4.build_style_prompt(stage4.RECAP_PRESET, {"beats": []}, label_facts=None)
    assert "`pointer`" not in p0 and "`irony`" not in p0 and "판정 동사 금지" in p0
    p1 = stage4.build_style_prompt(stage4.RECAP_PRESET, {"beats": []}, label_facts=FACTS)
    assert "`pointer`" in p1 and "C1" in p1 and "`irony`" in p1 and "r001" in p1


def test_face_detector_resolution_default_haar(monkeypatch):
    from app.modules import reframe as rf
    monkeypatch.delenv("FACE_DETECTOR", raising=False)
    assert rf.resolve_face_detector(None) == "haar"
    assert rf.YUNET_MODEL_PATH.exists() and rf.resolve_face_detector("yunet") == "yunet"
    monkeypatch.setenv("FACE_DETECTOR", "yunet")
    assert rf.resolve_face_detector(None) == "yunet"
    with pytest.raises(ValueError):
        rf.resolve_face_detector("dlib")


def test_yunet_detector_runs_on_blank_frame():
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from app.modules import reframe as rf
    if not hasattr(cv2, "FaceDetectorYN"):
        pytest.skip("FaceDetectorYN 없음")
    det = rf._YuNetDetector(rf.YUNET_MODEL_PATH)
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    assert len(det.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), frame)) == 0


def test_build_crop_timeline_crop_size_override(monkeypatch, tmp_path):
    from app.modules import reframe as rf
    kf = [rf.CropKeyframe(time_sec=0.0, x_center=100.0, y_center=100.0, crop_w=600, crop_h=1080)]
    monkeypatch.setattr(rf, "_has_cv2", lambda: True)
    monkeypatch.setattr(rf, "_detect_faces", lambda *a, **k: list(kf))
    out = rf.build_crop_timeline(Path("v.mp4"), tmp_path / "c.json", 1920, 1080, 0.5,
                                 crop_size=(1004, 962))
    assert (out[0].crop_w, out[0].crop_h) == (1004, 962)
    assert out[0].x_center == 502.0 and out[0].y_center == 481.0      # 프레임 안으로 클램프
    assert json.loads((tmp_path / "c.json").read_text())[0]["crop_w"] == 1004


def test_band_crop_size_and_speaker_map(tmp_path):
    assert finalize.band_crop_size("24:23", (1920, 1080), None) == (1126, 1080, 0, 0, 1920, 1080)
    geo = finalize.band_crop_size("24:23", (1920, 1080), {"x": 0, "y": 60, "w": 1920, "h": 960})
    assert geo == (1000, 960, 0, 60, 1920, 960)
    assert finalize.band_crop_size("9:16", (1080, 1920), None) is None       # 가로 여유 없음
    tl = [{"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 12.0, "span_ids": [], "subject_pos": "left"},
          {"role": "hook", "clip_start_sec": 12.0, "clip_end_sec": 13.0, "span_ids": [], "cover": "designated"},
          {"role": "build", "clip_start_sec": 20.0, "clip_end_sec": 21.0, "span_ids": []}]
    calls = []
    def build(video, out, w, h, step, **kw):
        calls.append(kw)
        return [{"time_sec": kw["start_sec"], "x_center": 1700.0, "y_center": 20.0, "crop_w": 0, "crop_h": 0, "face_w": 100}]
    m, audit = finalize.speaker_crop_map(tl, video_path=Path("v.mp4"), aspect_ratio="24:23",
                                         output_dir=tmp_path, src_size=(1920, 1080),
                                         picture={"x": 0, "y": 60, "w": 1920, "h": 960},
                                         detector="yunet", build=build, log=lambda *a: None)
    assert set(m) == {"hook_0", "build_2"} and len(calls) == 2            # 덮개 클립 제외
    assert calls[0]["detector"] == "yunet" and calls[0]["crop_size"] == (1000, 960)
    rows = json.loads(m["hook_0"].read_text())
    assert rows[0]["x_center"] == 1420.0 and rows[0]["y_center"] == 540.0   # 그림 안으로 클램프
    assert audit[0]["subject_pos_conflict"] == "left" and audit[0]["side"] == "right"
    # 2026-09-08: 컷 경계에 연속성은 없다 — 직전 클립 위치를 승계하지 않고 첫 얼굴에 즉시 맞춘다
    assert calls[1]["initial_x"] is None and calls[1]["snap_first"] is True
    assert calls[1]["area_relative"] is True and calls[1]["ema_alpha"] == finalize.SPEAKER_EMA_ALPHA


def test_speaker_tracking_gate_is_design_key_only():
    cd = channel_design_from_args(build_parser().parse_args(
        ["--video", "x.mp4", "--work-title", "t", "--design-speaker-tracking", "on",
         "--design-face-detector", "yunet"]))
    assert cd == {"speaker_tracking": "on", "face_detector": "yunet"}
    assert {"speaker_tracking", "face_detector"} <= V3_ONLY_DESIGN_KEYS <= set(CHANNEL_DESIGN_ARGS)
    src = (Path(__file__).resolve().parents[1] / "app" / "v3" / "finalize.py").read_text(encoding="utf-8")
    # 2026-09-08 사용자 결정: 기본 켜짐·YuNet — 끄는 길(off)은 남긴다
    assert 'str(_cd.get("speaker_tracking") or SPEAKER_TRACKING_DEFAULT).lower() in ("on", "pan")' in src
    assert finalize.SPEAKER_TRACKING_DEFAULT == "on" and finalize.FACE_DETECTOR_DEFAULT == "yunet"
    assert 'detector=_cd.get("face_detector") or FACE_DETECTOR_DEFAULT' in src


def test_edit_plan_to_xml_builds_parseable_sequence(tmp_path):
    from scripts.edit_plan_to_xml import build_srt, build_xml
    plan = {"layout": {"canvas": "1080x1920"}, "timeline": [
        {"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 12.5, "use_original_audio": True, "span_ids": []},
        {"role": "hook", "clip_start_sec": 12.5, "clip_end_sec": 14.0, "use_original_audio": False, "span_ids": [],
         "cover": "designated", "hold_sec": 0.7}]}
    cues = [{"path": str(tmp_path / "tts_cue_0.mp3"), "cue": {"start_sec": 2.5, "duration_sec": 1.2, "text": "내레이션"}}]
    x = build_xml(plan, cues, video_path=tmp_path / "EP01.mp4", fps=24, src_duration_sec=100.0, seq_name="s")
    doc = minidom.parseString(x)
    items = doc.getElementsByTagName("clipitem")
    assert len(items) == 5                                                   # 비디오 2 + 오디오 2 + cue 1
    assert x.count("<name>Audio Levels</name>") == 1 and "hold=0.7s" in x    # 원음 끔 클립만 Level 0
    assert "<start>60</start><end>96</end>" in x and "<in>240</in><out>300</out>" in x
    srt = build_srt([{"start_sec": 1.0, "end_sec": 2.5, "text": "안녕"}])
    assert srt.startswith("1\n00:00:01,000 --> 00:00:02,500\n안녕\n")


def test_stage1_prompt_counts_provider_cards_as_intro():
    from app.v3.seq_analyze import PROMPT_TEMPLATE
    from app.v3.seq_analyze import INTRO_MAX_SEC, build_prompt
    assert "제공사·제작지원" in PROMPT_TEMPLATE and "콜드오픈·프롤로그·회상·몽타주는 본편이다" in PROMPT_TEMPLATE
    assert INTRO_MAX_SEC == 90.0 and "{intro_max" in PROMPT_TEMPLATE
