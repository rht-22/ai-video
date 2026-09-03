"""E19-4 라벨 얼굴 회피·컷 경계 재배치·괄호형 글로우 회귀 가드.

발주서: docs/prompts/e19-drama-clip-preset.md §4. 벤치마크 실측(§12): 라벨은 인물
얼굴 옆 빈 공간에 붙고, 컷이 바뀌면 새 구도에 맞춰 재배치된다(「난가???」 3위치).

- **얼굴 좌표는 새로 검출하지 않는다** — 리프레임 크롭 타임라인(CropKeyframe)에
  검출된 얼굴 박스를 함께 실어 재사용한다(검출 비용 0). 구 캐시 JSON(face 키 없음)은
  미검출로 읽혀 회피 없이 종전 배치다(안전장치가 연출을 막으면 안 된다).
- 배치 후보는 위 → 아래 → 옆 순서로 첫 성립. 성립 후보가 없으면 **옮기지 않고 기록**
  (E18-2 "살리고 당긴다" 규율).
- 라벨 창이 클립 경계를 넘으면 경계에서 쪼개 각 조각을 그 클립의 얼굴 기준으로
  재배치한다(v3 texts 는 조각당 한 항목 — 렌더 계약 변경 없음).
- fx="glow"(괄호형 라벨 전용 발광 테두리) — TEXT_FX 확장 + build_texts_ass 한 곳.
  미사용 시 종전과 동일.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.config import DesignConfig
from app.modules.edit_overrides import TEXT_FX
from app.modules.reframe import CropKeyframe
from app.modules.style_compose import (
    avoid_faces_for_texts,
    face_box_on_canvas,
    text_y_range,
)
from app.modules.subtitle import build_texts_ass
from app.modules.subtitle_region import band_geometry
from app.modules.story_builder import StoryClip

REPO = Path(__file__).resolve().parents[1]


def _kf(t, cx=500.0, cy=400.0, fw=200, fh=260, **over):
    d = {"time_sec": t, "x_center": cx, "y_center": cy, "crop_w": 540, "crop_h": 960,
         "face_cx": cx, "face_cy": cy, "face_w": fw, "face_h": fh}
    d.update(over)
    return d


def _clip(start, end, role="hook"):
    return StoryClip(role=role, start_sec=start, end_sec=end, subtitle="s",
                     use_original_audio=True)


def _crop_map(tmp_path, per_clip_kfs):
    m = {}
    for key, kfs in per_clip_kfs.items():
        p = tmp_path / f"crop_{key}.json"
        p.write_text(json.dumps(kfs), encoding="utf-8")
        m[key] = p
    return m


def _text(x=0.5, y=0.5, size=80, text="쫌", start=1.0, end=2.5, **over):
    d = {"text": text, "x": x, "y": y, "size": size,
         "start_sec": start, "end_sec": end}
    d.update(over)
    return d


# ══════════════════════════════════════════════════════════════════════════
# CropKeyframe — 얼굴 박스 필드 (구 JSON 하위 호환)
# ══════════════════════════════════════════════════════════════════════════
def test_cropkeyframe_face_fields_default():
    """face 인자 없이 만든 옛 호출(_center_crop 등)이 그대로 성립하고, 직렬화에
    미검출(face_w=0)로 실린다 — 구 캐시 JSON 은 키 자체가 없어도 .get 으로 0."""
    kf = CropKeyframe(time_sec=0.0, x_center=1.0, y_center=1.0, crop_w=10, crop_h=10)
    assert asdict(kf)["face_w"] == 0 and asdict(kf)["face_h"] == 0


# ══════════════════════════════════════════════════════════════════════════
# face_box_on_canvas — 렌더 체인과 같은 변환
# ══════════════════════════════════════════════════════════════════════════
def test_face_box_canvas_transform():
    """1:1 밴드(1080x1080, overlay_y 420) · 크롭 540x960 → s=2.0, 세로 중앙 크롭.
    얼굴(500,400) 200x260 → 캔버스 (540,960) 400x520 — 수계산과 일치해야 한다."""
    geom = band_geometry(DesignConfig())
    box = face_box_on_canvas([_kf(100.0)], 0.0, geom)
    cx, cy, w, h = box
    assert (round(cx), round(cy)) == (540, 960)
    assert (round(w), round(h)) == (400, 520)


def test_face_box_none_without_detection():
    assert face_box_on_canvas([_kf(100.0, fw=0, fh=0)], 0.0, band_geometry(DesignConfig())) is None
    assert face_box_on_canvas([], 0.0, band_geometry(DesignConfig())) is None


def test_face_box_old_json_without_keys():
    kf = {"time_sec": 100.0, "x_center": 500, "y_center": 400, "crop_w": 540, "crop_h": 960}
    assert face_box_on_canvas([kf], 0.0, band_geometry(DesignConfig())) is None


# ══════════════════════════════════════════════════════════════════════════
# avoid_faces_for_texts — 위 → 아래 → 옆, 실패 시 그대로
# ══════════════════════════════════════════════════════════════════════════
def _series(t0, n=11, step=0.5, **over):
    """실제 크롭 타임라인처럼 촘촘한(기본 0.5s) 표본열 — 표본 허용 시차(0.75s) 안."""
    return [_kf(t0 + step * i, **over) for i in range(n)]


def _run(tmp_path, texts, clips=None, kfs=None):
    clips = clips or [_clip(100.0, 105.0)]
    kfs = kfs if kfs is not None else {"hook_0": _series(100.0)}
    crop_map = _crop_map(tmp_path, kfs)
    y_lo, y_hi = text_y_range(DesignConfig())
    return avoid_faces_for_texts(texts, clips, crop_map, DesignConfig(), y_lo, y_hi)


def test_overlapping_label_moves_above_face(tmp_path):
    out, notes, rep = _run(tmp_path, [_text()])          # 라벨이 얼굴 중심(540,960) 위
    assert rep["moved"] == 1
    y_px = out[0]["y"] * 1920
    assert y_px < 960 - 260                              # 얼굴 위(face_top=700)보다 위
    assert out[0]["x"] == 0.5                            # 위 후보는 x 를 안 움직인다


def test_no_overlap_untouched(tmp_path):
    out, notes, rep = _run(tmp_path, [_text(x=0.15, y=0.25)])   # 얼굴에서 멀다
    assert out[0]["x"] == 0.15 and out[0]["y"] == 0.25
    assert rep["moved"] == 0


def test_no_face_untouched(tmp_path):
    out, _n, rep = _run(tmp_path, [_text()], kfs={"hook_0": [_kf(100.0, fw=0, fh=0)]})
    assert out[0]["y"] == 0.5 and rep["moved"] == 0 and rep["no_face"] == 1


def test_missing_crop_json_untouched(tmp_path):
    out, _n, rep = _run(tmp_path, [_text()], kfs={})     # 크롭 파일 자체가 없다
    assert out[0]["y"] == 0.5 and rep["moved"] == 0


def test_side_candidate_when_vertical_blocked(tmp_path):
    """위·아래가 밴드 밖이면 옆으로 — 세로로 아주 큰 얼굴을 만들어 강제한다."""
    kfs = {"hook_0": _series(100.0, fw=200, fh=2000)}
    out, _n, rep = _run(tmp_path, [_text()], kfs=kfs)
    assert rep["moved"] == 1
    assert out[0]["x"] != 0.5                            # 옆 후보 = x 이동
    assert out[0]["y"] == 0.5


def test_impossible_keeps_position_with_note(tmp_path):
    """어느 후보도 성립 못 하면 옮기지 않는다 — 화면을 덮는 얼굴 + 화면만 한 라벨."""
    kfs = {"hook_0": _series(100.0, fw=2000, fh=2000)}
    out, notes, rep = _run(tmp_path, [_text(size=400, text="아주아주긴라벨텍스트")], kfs=kfs)
    assert out[0]["x"] == 0.5 and out[0]["y"] == 0.5
    assert rep["kept_overlap"] == 1
    assert any("그대로" in n for n in notes)


def test_split_at_clip_boundary(tmp_path):
    """창이 클립 경계를 넘으면 조각내 각 클립의 얼굴 기준으로 재배치 — 「난가???」."""
    clips = [_clip(100.0, 105.0, "hook"), _clip(200.0, 205.0, "build")]
    # ⚠ 리프레임은 얼굴을 크롭 중앙에 두므로 face 캔버스 위치는 대개 밴드 중앙이다 —
    # 조각별 차이는 (EMA 지연·데드존으로 실데이터에 늘 있는) 크롭 중심과 raw 얼굴의
    # 어긋남 + 얼굴 크기 차이로 만든다. build 클립은 얼굴이 더 커서 '위' 후보가 다르다.
    kfs = {
        "hook_0": _series(100.0),                                  # 얼굴 캔버스 (540,960) h520
        "build_1": _series(200.0, fh=400),                         # 얼굴 캔버스 h800
    }
    texts = [_text(start=4.0, end=6.5)]                           # 5.0s 경계를 넘는다
    out, _n, rep = _run(tmp_path, texts, clips=clips, kfs=kfs)
    assert len(out) == 2 and rep["split"] == 1
    assert out[0]["end_sec"] == 5.0 and out[1]["start_sec"] == 5.0
    assert out[0]["text"] == out[1]["text"] == "쫌"
    assert out[0]["y"] != out[1]["y"] or out[0]["x"] != out[1]["x"]   # 조각별 재배치


def test_inputs_not_mutated(tmp_path):
    src = [_text()]
    _run(tmp_path, src)
    assert src[0]["y"] == 0.5                             # 순수 — 사본만


# ══════════════════════════════════════════════════════════════════════════
# fx="glow" — 괄호형 라벨 발광 테두리
# ══════════════════════════════════════════════════════════════════════════
def test_glow_in_fx_vocab():
    assert "glow" in TEXT_FX


def test_glow_ass_tags(tmp_path):
    p = tmp_path / "texts.ass"
    build_texts_ass([{"text": "(폐급 선임)", "x": 0.5, "y": 0.4, "size": 60,
                      "color": "#FF4632", "fx": "glow",
                      "start_sec": 1.0, "end_sec": 2.0}], p)
    ass = p.read_text(encoding="utf-8-sig")
    assert "\\blur" in ass
    assert ass.count("3246FF") >= 2                       # 아래층: 글자색 = 테두리색(후광)
    # 2026-09-03 D 안: 후광(Layer 1) + 흰 글자/검정 외곽(Layer 2) 두 줄
    dl = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(dl) == 2 and dl[0].startswith("Dialogue: 1,") and dl[1].startswith("Dialogue: 2,")
    assert "\\1c&HFFFFFF&" in dl[1] and "\\3c&H000000&" in dl[1] and "\\blur" not in dl[1]


def test_non_glow_has_no_blur(tmp_path):
    p = tmp_path / "texts.ass"
    build_texts_ass([{"text": "쿵", "x": 0.5, "y": 0.4, "size": 60,
                      "color": "#FFDD00", "fx": "pop",
                      "start_sec": 1.0, "end_sec": 2.0}], p)
    ass = p.read_text(encoding="utf-8-sig")
    assert "\\blur" not in ass and "\\fscx30" in ass       # pop 은 종전 그대로


def test_tone_style_block_mentions_glow():
    from app.modules.style_tone import load_style_tone, style_prompt_block
    assert "glow" in style_prompt_block(load_style_tone("drama_clip_kr"))


# ══════════════════════════════════════════════════════════════════════════
# 배선 — 게이트·자리 (소스 문자열 고정)
# ══════════════════════════════════════════════════════════════════════════
def test_pipeline_wiring():
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    assert "_texts_from_style" in src                     # 편집실 텍스트는 안 건드린다
    gate = src.index("avoid_faces_for_texts(")
    # 자리: 크롭 타임라인 생성 뒤 · 텍스트 ASS 생성 앞
    assert src.index("build_crop_timeline(") < gate < src.index("build_texts_ass(")
    assert '"step": "style_face_avoid"' in src
    # 톤 게이트 — 미지정 채널은 종전 배치(회귀 0)
    assert "style_tone_profile is not None" in src[gate - 1200:gate]
    # ⚠ 울트라리뷰 bug_001(2026-08-28): 리소스 **생성 elif 안**에 두면 --from-step render
    # (A/B 재렌더의 표준 경로)가 캐시 else 로 빠지며 회피를 건너뛰고, 체크포인트의 원
    # 좌표로 texts.ass 를 덮어쓴다 — 승인 화면이 조용히 되돌아간다(E15 재개 계약 위반).
    # 블록은 리소스 3분기(캐시 로드·생성·재개 폴백)가 **수렴한 뒤**에 있어야 한다.
    # rindex — 이 문구는 파일에 두 번 있다(3653 의 다른 재개 블록이 먼저 걸리면 헛가드).
    assert src.rindex("체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다") < gate
    assert src.index("[OK] 리소스 로드 완료 (체크포인트에서)") < gate
