"""라벨 얼굴 회피(app/v3/label_faces, 2026-09-08) — 순수 함수 회귀 가드. cv2·ffmpeg 없이 돈다."""
from __future__ import annotations

from app.modules.subtitle_region import BandGeometry
from app.v3 import label_faces as lf

GEOM = BandGeometry(scaled_w=1080, scaled_h=746, overlay_y=420, pad_x=0)   # 13:9 · video_y 420


def test_draft_box_to_canvas_matches_center_crop_math():
    # 초안 854×480 → 밴드 높이 746 (scale 1.554), 폭 1327 중 가운데 1080 → x 오프셋 −123.7
    x0, y0, x1, y1 = lf.draft_box_to_canvas((404, 72, 118, 118), 854, 480, GEOM, pad_ratio=0.0)
    s = 746 / 480
    assert abs(x0 - (404 * s - (854 * s - 1080) / 2)) < 0.01
    assert abs(y0 - (420 + 72 * s)) < 0.01 and abs(y1 - (420 + 190 * s)) < 0.01


def test_avoid_faces_moves_label_above_face_and_keeps_when_clear():
    face = (400.0, 560.0, 680.0, 900.0)             # 화면 가운데 큰 얼굴(캔버스 px)
    lb = {"text": "(내 파트였는데)", "start_sec": 29.0, "end_sec": 30.6, "x": 0.5, "y": 0.32, "size": 56}
    moved, rec = lf.avoid_faces(lb, [face], GEOM)
    assert rec and rec["moved"] and rec["why"] == "above"
    box = lf.label_box(moved)
    assert box[3] + lf.FACE_GAP_PX <= face[1]                 # 라벨 아랫변이 얼굴 위
    assert box[1] >= GEOM.top                                 # 밴드 안
    clear = {**lb, "y": 0.25}                                 # 이미 얼굴 위
    assert lf.avoid_faces(clear, [face], GEOM) == (clear, None)


def test_avoid_faces_falls_to_side_when_no_room_above_and_records_when_impossible():
    face = (380.0, 432.0, 560.0, 900.0)             # 얼굴이 밴드 꼭대기까지 — 위엔 자리 없음, 옆은 됨
    lb = {"text": "(단호한 거절)", "start_sec": 10.0, "end_sec": 11.0, "x": 0.5, "y": 0.32, "size": 56}
    moved, rec = lf.avoid_faces(lb, [face], GEOM)
    assert rec["moved"] and rec["why"].startswith("side")
    assert not lf._overlaps(lf.label_box(moved), face)
    wall = [(0.0, 432.0, 1080.0, 1154.0)]           # 밴드 전체가 얼굴 — 어디도 안 됨
    same, rec2 = lf.avoid_faces(lb, wall, GEOM)
    assert same == lb and rec2["moved"] is False


def test_avoid_faces_for_labels_without_draft_is_noop(tmp_path):
    labels = [{"text": "x", "start_sec": 0.0, "end_sec": 1.0, "x": 0.5, "y": 0.3}]
    out, rec = lf.avoid_faces_for_labels(labels, tmp_path / "none.mp4", GEOM, log=lambda *a: None)
    assert out == labels and rec == []
