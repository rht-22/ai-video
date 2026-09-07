"""v3 소스 레터박스(그림 영역) 검출 + 크롭 회귀 가드 (2026-09-07).

배경: EP01 소스는 1920×1080 컨테이너에 그림이 위아래 59px 검은 띠 사이 1920×962 —
v3 크롭이 `crop_h = src_h` 라 완성본 밴드 안에 띠가 그대로 들어갔다
(docs/v3_gaps_from_manual_shorts.md「자산 레터박스」).

고정하는 것:
  · analyze_rows 순수 규칙 — 띠 없음=전체 · 59px 대칭(→ 짝수 60/960) · 비대칭은 작은
    쪽으로 대칭 · 밝은 프레임이 하나라도 섞이면 띠 아님(cropdetect 실패 모드의 반대)
  · 합성 mp4 실측(ffmpeg color + pad) · 실패는 사유 기록 + 전체 영역
  · finalize 크롭 맵 — 그림 영역 안에서 밴드 비율 · 그림=컨테이너면 종전 맵과 동일(회귀 0)
  · probe 재개 — 옛 체크포인트(picture 없음)는 다시 재서 채운다
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.v3 import letterbox
from app.v3.letterbox import analyze_rows

W, H = 1920, 1080


def _rows(total: int, top: int, bottom: int, val: int = 200) -> list[int]:
    arr = [0] * total
    for i in range(top, total - bottom):
        arr[i] = val
    return arr


def _ffmpeg() -> str | None:
    try:
        from app.modules.ffmpeg_utils import find_ffmpeg_command
        return find_ffmpeg_command("ffmpeg")
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def _synth_letterboxed(path: Path, *, w: int, h: int, pad_top: int, seconds: int = 2):
    """흰 그림 w×h 를 위아래 pad_top 검은 띠로 감싼 (w × h+2·pad_top) 합성 mp4."""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg 없음")
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c=white:s={w}x{h}:d={seconds}:r=10",
         "-vf", f"pad={w}:{h + 2 * pad_top}:0:{pad_top}:black",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path)],
        check=True, capture_output=True)
    return path


# ── analyze_rows 순수 규칙 ────────────────────────────────────────────────

def test_analyze_rows_no_band_is_full():
    assert analyze_rows([200] * H, [200] * W, W, H) == {"x": 0, "y": 0, "w": W, "h": H}


def test_analyze_rows_ep01_symmetric_59px():
    """EP01 실측 모양 — 위아래 59px 검정 → 경계 1줄 제외 + 짝수 = y 60 · h 960."""
    pic = analyze_rows(_rows(H, 59, 59), [200] * W, W, H)
    assert pic == {"x": 0, "y": 60, "w": W, "h": 960}
    assert pic["y"] % 2 == 0 and pic["h"] % 2 == 0


def test_analyze_rows_asymmetric_uses_smaller_pad():
    """위 59 · 아래 30 → 작은 쪽(30+1=31 → 짝수 올림 32)으로 대칭 — 중심이 안 틀어진다."""
    pic = analyze_rows(_rows(H, 59, 30), [200] * W, W, H)
    assert pic["y"] == 32 and pic["h"] == H - 64
    assert pic["x"] == 0 and pic["w"] == W


def test_analyze_rows_pillarbox_and_letterbox_both():
    pic = analyze_rows(_rows(H, 59, 59), _rows(W, 240, 240), W, H)
    assert pic == {"x": 242, "y": 60, "w": W - 484, "h": 960}


def test_bright_frame_breaks_band():
    """어두운 장면 하나(위아래 검정)에 밝은 장면 하나가 섞이면 누적 최대는 띠가 아니다 —
    ffmpeg cropdetect 가 어두운 장면 하나로 그림까지 잘라내던 것의 반대 규칙."""
    dark = _rows(H, 59, 59, val=180)
    bright = [255] * H
    acc = [max(a, b) for a, b in zip(dark, bright)]
    assert analyze_rows(acc, [200] * W, W, H) == {"x": 0, "y": 0, "w": W, "h": H}
    # 반대로 표본 전부가 검은 행을 공유하면(누적해도 어둡다) 띠다
    acc2 = [max(a, b) for a, b in zip(dark, _rows(H, 59, 59, val=90))]
    assert analyze_rows(acc2, [200] * W, W, H)["y"] == 60


def test_noise_below_thresh_is_black():
    rows = _rows(H, 59, 59)
    for i in list(range(0, 59)) + list(range(H - 59, H)):
        rows[i] = letterbox.THRESH                     # 압축 잡음 여유 — 12 이하는 검정
    assert analyze_rows(rows, [200] * W, W, H)["y"] == 60
    rows[3] = letterbox.THRESH + 1                     # 한 줄이라도 넘으면 그림
    assert analyze_rows(rows, [200] * W, W, H)["y"] == 4   # 3+1 경계 = 4


def test_all_black_is_undecidable_full():
    assert analyze_rows([0] * H, [0] * W, W, H) == {"x": 0, "y": 0, "w": W, "h": H}


def test_analyze_rows_length_mismatch_fails_loud():
    with pytest.raises(ValueError):
        analyze_rows([0] * (H - 1), [0] * W, W, H)


def test_is_full():
    assert letterbox.is_full(None, W, H)
    assert letterbox.is_full({"x": 0, "y": 0, "w": W, "h": H}, W, H)
    assert not letterbox.is_full({"x": 0, "y": 60, "w": W, "h": 960}, W, H)
    assert letterbox.is_full({"x": "bad"}, W, H)       # 깨진 값 = 종전 경로


# ── 실측(합성 mp4) ───────────────────────────────────────────────────────

def test_detect_picture_area_synthetic(tmp_path):
    """흰 640×300 + 위아래 30px 띠 → pad 30+1=31 → 짝수 32, h 296. 표본 4장 전부 읽힘."""
    v = _synth_letterboxed(tmp_path / "lb.mp4", w=640, h=300, pad_top=30)
    pic = letterbox.detect_picture_area(v, samples=4, log=lambda *a: None)
    assert (pic["x"], pic["y"], pic["w"], pic["h"]) == (0, 32, 640, 296)
    assert pic["samples"] == 4 and pic["width"] == 640 and pic["height"] == 360


def test_detect_picture_area_no_band_synthetic(tmp_path):
    v = _synth_letterboxed(tmp_path / "full.mp4", w=640, h=360, pad_top=0)
    pic = letterbox.detect_picture_area(v, samples=3, log=lambda *a: None)
    assert (pic["x"], pic["y"], pic["w"], pic["h"]) == (0, 0, 640, 360)


def test_detect_or_full_failure_is_recorded_not_silent(tmp_path):
    logs: list[str] = []
    pic = letterbox.detect_or_full(tmp_path / "missing.mp4", width=W, height=H,
                                   log=logs.append)
    assert (pic["x"], pic["y"], pic["w"], pic["h"]) == (0, 0, W, H)
    assert pic["samples"] == 0 and "error" in pic and "LetterboxProbeError" in pic["error"]
    assert any("레터박스 검출 실패" in m for m in logs)


def test_detect_picture_area_missing_raises(tmp_path):
    with pytest.raises(letterbox.LetterboxProbeError):
        letterbox.detect_picture_area(tmp_path / "missing.mp4")


# ── finalize 크롭 맵 ─────────────────────────────────────────────────────

def _kf(m, key):
    return json.loads(m[key].read_text())[0]


def test_crop_map_letterbox_all_clips_in_picture_band_ratio(tmp_path):
    """그림 1920×960@(0,60) · 1:1 밴드 → 전 클립 960×960, y 540, x 는 그림 좌표의 앵커."""
    from app.v3.finalize import subject_crop_map
    tl = [{"role": "build", "subject_pos": "left"}, {"role": "build"},
          {"role": "ending", "subject_pos": "right"}]
    m = subject_crop_map(tl, video_path=tmp_path / "v.mp4", aspect_ratio="1:1",
                         output_dir=tmp_path, src_size=(W, H),
                         picture={"x": 0, "y": 60, "w": 1920, "h": 960},
                         log=lambda *a: None)
    assert set(m) == {"build_0", "build_1", "ending_2"}
    for k in m:
        kf = _kf(m, k)
        assert (kf["crop_w"], kf["crop_h"]) == (960, 960)
        assert kf["y_center"] == 540.0
        # 크롭 상단이 그림 상단(60) 아래·하단이 그림 하단(1020) 위 — 검은 띠가 안 들어온다
        assert kf["y_center"] - kf["crop_h"] / 2 >= 60
        assert kf["y_center"] + kf["crop_h"] / 2 <= 1020
    assert _kf(m, "build_0")["x_center"] == 480.0     # 0.25×1920 = 480 ≥ 960/2 → 그대로
    assert _kf(m, "build_1")["x_center"] == 960.0     # 앵커 없음 = 그림 중앙
    assert _kf(m, "ending_2")["x_center"] == 1440.0   # 0.75×1920 = 1440 ≤ 1920−480


def test_crop_map_pillarbox_width_bound(tmp_path):
    """그림 1120×1080@(400,0) 에 16:9 → 폭 기준 1120×630, 앵커 좌는 클램프로 그림 중앙."""
    from app.v3.finalize import subject_crop_map
    tl = [{"role": "build", "subject_pos": "left"}, {"role": "build"}]
    m = subject_crop_map(tl, video_path=tmp_path / "v.mp4", aspect_ratio="16:9",
                         output_dir=tmp_path, src_size=(W, H),
                         picture={"x": 400, "y": 0, "w": 1120, "h": 1080},
                         log=lambda *a: None)
    kf0, kf1 = _kf(m, "build_0"), _kf(m, "build_1")
    assert (kf0["crop_w"], kf0["crop_h"]) == (1120, 630)
    assert abs(kf0["crop_w"] / kf0["crop_h"] - 16 / 9) < 0.01
    assert kf0["x_center"] == 960.0 == kf1["x_center"]   # 400+280=680 → 클램프 960
    assert kf0["y_center"] == 540.0
    assert kf0["x_center"] - kf0["crop_w"] / 2 >= 400   # 필러박스 안 들어옴


def test_crop_map_full_picture_equals_legacy(tmp_path):
    """그림 = 컨테이너 → picture 를 줘도 종전과 **정확히 같은 맵**(같은 키·같은 바이트)."""
    from app.v3.finalize import subject_crop_map
    tl = [{"role": "build", "subject_pos": "left"}, {"role": "build"},
          {"role": "ending", "subject_pos": "right"}]
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(); b_dir.mkdir()
    legacy = subject_crop_map(tl, video_path=tmp_path / "v.mp4", aspect_ratio="24:23",
                              output_dir=a_dir, src_size=(W, H), log=lambda *a: None)
    same = subject_crop_map(tl, video_path=tmp_path / "v.mp4", aspect_ratio="24:23",
                            output_dir=b_dir, src_size=(W, H),
                            picture={"x": 0, "y": 0, "w": W, "h": H},
                            log=lambda *a: None)
    assert set(legacy) == set(same) == {"build_0", "ending_2"}   # 앵커 클립만
    for k in legacy:
        assert legacy[k].read_bytes() == same[k].read_bytes()
    assert _kf(legacy, "build_0") == {"time_sec": 0.0, "x_center": 563.0,
                                      "y_center": 540.0, "crop_w": 1126, "crop_h": 1080}
    # 앵커 0 + 전체 영역 = 빈 맵(종전)
    assert subject_crop_map([{"role": "build"}], video_path=tmp_path / "v.mp4",
                            aspect_ratio="24:23", output_dir=tmp_path, src_size=(W, H),
                            picture={"x": 0, "y": 0, "w": W, "h": H},
                            log=lambda *a: None) == {}


def test_crop_map_anchors_false_centers_but_still_crops_letterbox(tmp_path):
    """face_tracking=false — subject_pos 무시(x 중앙)하되 레터박스 크롭은 전 클립 적용."""
    from app.v3.finalize import subject_crop_map
    tl = [{"role": "build", "subject_pos": "left"}, {"role": "build"}]
    logs: list[str] = []
    m = subject_crop_map(tl, video_path=tmp_path / "v.mp4", aspect_ratio="1:1",
                         output_dir=tmp_path, src_size=(W, H), anchors=False,
                         picture={"x": 0, "y": 60, "w": 1920, "h": 960}, log=logs.append)
    assert set(m) == {"build_0", "build_1"}
    assert _kf(m, "build_0")["x_center"] == 960.0 == _kf(m, "build_1")["x_center"]
    assert any("face_tracking=false" in s for s in logs)
    # anchors=False 인데 레터박스도 없으면 종전과 같이 빈 맵
    assert subject_crop_map(tl, video_path=tmp_path / "v.mp4", aspect_ratio="1:1",
                            output_dir=tmp_path, src_size=(W, H), anchors=False,
                            log=lambda *a: None) == {}


def test_read_picture_area_from_checkpoint(tmp_path):
    from app.v3.finalize import read_picture_area
    assert read_picture_area(tmp_path) is None                  # 파일 없음
    ck = tmp_path / "checkpoint_probe.json"
    base = {"path": "x.mp4", "duration_sec": 10.0, "fps": 30.0,
            "width": W, "height": H, "has_audio": True}
    ck.write_text(json.dumps(base))
    assert read_picture_area(tmp_path) is None                  # 옛 체크포인트(키 없음)
    ck.write_text(json.dumps({**base, "picture": {"x": 0, "y": 0, "w": W, "h": H,
                                                  "samples": 16}}))
    assert read_picture_area(tmp_path) is None                  # 전체 영역 = 종전 경로
    ck.write_text(json.dumps({**base, "picture": {"x": 0, "y": 0, "w": W, "h": H,
                                                  "samples": 0, "error": "boom"}}))
    assert read_picture_area(tmp_path) is None                  # 실패 기록 = 종전 경로
    ck.write_text(json.dumps({**base, "picture": {"x": 0, "y": 60, "w": W, "h": 960,
                                                  "samples": 16}}))
    assert read_picture_area(tmp_path) == {"x": 0, "y": 60, "w": W, "h": 960}


# ── probe 단계 배선 ──────────────────────────────────────────────────────

def test_load_or_probe_media_refills_old_checkpoint(tmp_path):
    """옛 checkpoint_probe(picture 없음)는 다시 재서 채우고, 그 다음 재개는 그대로 읽는다."""
    from app.modules.media_probe import MediaInfo
    from app.v3.pipeline import _load_or_probe_media

    v = _synth_letterboxed(tmp_path / "lb.mp4", w=640, h=300, pad_top=30)
    ck = tmp_path / "checkpoint_probe.json"
    ck.write_text(json.dumps({"path": str(v), "duration_sec": 2.0, "fps": 10.0,
                              "width": 640, "height": 360, "has_audio": False}))
    logs: list[str] = []
    info, pic, refilled = _load_or_probe_media(ck, v, log=logs.append)
    assert isinstance(info, MediaInfo) and info.width == 640
    assert refilled is True
    assert (pic["x"], pic["y"], pic["w"], pic["h"]) == (0, 32, 640, 296)
    # 2초·10fps 합성은 마지막 표본(t=1.94)이 마지막 프레임(1.9) 뒤라 15장 — `samples` 는
    # 요청 수가 아니라 **실제 읽은 장수**다
    assert 8 <= pic["samples"] <= letterbox.DEFAULT_SAMPLES
    assert any("다시 측정" in s for s in logs) and any("레터박스 위아래 32px" in s for s in logs)
    saved = json.loads(ck.read_text())
    assert saved["picture"] == pic and saved["width"] == 640       # additive
    # 두 번째 재개: picture 가 있으니 재측정 없음 · MediaInfo 도 추가 키에 안 죽는다
    info2, pic2, refilled2 = _load_or_probe_media(ck, v, log=lambda *a: None)
    assert refilled2 is False and pic2 == pic and info2 == info


def test_load_or_probe_media_error_record_is_retried(tmp_path):
    """지난 실행이 실패(error)를 기록했으면 재개에서 다시 잰다 — 폴백이 굳어지지 않는다."""
    from app.v3.pipeline import _load_or_probe_media

    v = _synth_letterboxed(tmp_path / "lb.mp4", w=640, h=300, pad_top=30)
    ck = tmp_path / "checkpoint_probe.json"
    ck.write_text(json.dumps({"path": str(v), "duration_sec": 2.0, "fps": 10.0,
                              "width": 640, "height": 360, "has_audio": False,
                              "picture": {"x": 0, "y": 0, "w": 640, "h": 360,
                                          "samples": 0, "error": "ffmpeg 없음"}}))
    _, pic, refilled = _load_or_probe_media(ck, v, log=lambda *a: None)
    assert refilled is True and "error" not in pic and pic["y"] == 32


def test_wiring_strings_fixed():
    """배선 고정 — render_final 이 checkpoint 의 picture 를 읽고, --no-reframe 분기도
    레터박스 크롭을 적용하며, 렌더 지문이 레터박스일 때만 picture 를 싣는다."""
    import app.v3.finalize as _fin
    import app.v3.pipeline as _pipe
    fin = Path(_fin.__file__).read_text("utf-8")
    pipe = Path(_pipe.__file__).read_text("utf-8")
    assert "picture = read_picture_area(output_dir)" in fin
    assert "picture=picture,\n                                    anchors=False" in fin
    assert 'cost["letterbox_crop"]' in fin
    assert "_load_or_probe_media(" in pipe
    assert '"picture": picture' in pipe                         # run_log probe step
    assert '{"letterbox_picture": _picture}' in pipe            # 렌더 지문(레터박스 한정)
