"""현지화 대상 판정 — 노이즈를 지우지도 그리지도 않는다 (2026-08-26 실사고).

🛑 잔망루피 `a6wO8o91Oi0`(route B) 완성본에 인형 몸통 한가운데 얼룩이 남았다.
   `ja_events.json` 13건이 **전부 글자가 아니었다**:

     '-' '・' 'ATAL' '2' 'U' 'AI' '・' '-' '2' '：' "'9" 'L' '：'

   route B 는 지우고 그 자리에 일본어를 그리는 길이라, 그 노이즈 자리를 인페인팅으로
   지우고 같은 노이즈를 다시 그렸다. 이 파일은 그 13건이 다시 통과하지 못하게 한다.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.localize.overlay.detect import filter_localizable, localizable  # noqa: E402
from app.localize.overlay.schemas import (DetectionDoc, FrameDetections,  # noqa: E402
                                          Region, Style)

# 실측 그대로 — 이 목록이 회귀 가드의 재료다.
REAL_NOISE = ["-", "・", "ATAL", "2", "U", "AI", "：", "'9", "L"]
KW = {"min_conf": 0.5, "min_area_px": 400, "source_lang": "ko"}


def test_the_thirteen_that_shipped_are_all_rejected():
    for t in REAL_NOISE:
        assert not localizable(t, 0.9, (0, 0, 120, 60), **KW), t


def test_korean_passes():
    for t in ["루피야", "맛있뤂", "이니미니", "청하 or 루피"]:
        assert localizable(t, 0.9, (0, 0, 120, 60), **KW), t


def test_korean_mixed_with_latin_and_digits_passes():
    """`청하 or 루피`·`2화` 처럼 섞인 것은 한글이 하나라도 있으면 대상이다."""
    assert localizable("2화", 0.9, (0, 0, 120, 60), **KW)
    assert localizable("TOP3 루피", 0.9, (0, 0, 120, 60), **KW)


def test_low_confidence_is_rejected():
    assert not localizable("루피야", 0.2, (0, 0, 120, 60), **KW)


def test_tiny_boxes_are_rejected():
    """실측 노이즈 중 11x11 짜리가 있었다(`L`) — 그 크기는 글자로 보지 않는다."""
    assert not localizable("루피야", 0.9, (155, 773, 166, 784), **KW)


def test_broken_bbox_does_not_drop_the_region():
    """상자가 깨졌다고 멀쩡한 자막을 버리면 안 된다 — 오판이 얼룩보다 나쁘다."""
    assert localizable("루피야", 0.9, None, **KW)
    assert localizable("루피야", 0.9, ("a", "b", "c", "d"), **KW)


def test_unknown_source_language_skips_the_script_check():
    """문자군을 모르는 언어에서 전량을 버리면 그 채널이 통째로 멈춘다."""
    assert localizable("ATAL", 0.9, (0, 0, 120, 60), min_conf=0.5, min_area_px=400,
                       source_lang="xx")


def _doc(texts, roi=None):
    regions = [Region(bbox=(0, 0, 120, 60), text=t, confidence=0.9, style=Style())
               for t in texts]
    return DetectionDoc(video_id="v", fps=30.0, width=1080, height=1920, sample_every=15,
                        ocr_backend="paddleocr", roi=roi,
                        frames=[FrameDetections(frame_idx=0, timestamp=0.0, regions=regions)])


def test_filter_keeps_only_targets_and_records_the_rest():
    doc, dropped = filter_localizable(_doc(["루피야"] + REAL_NOISE), {})
    kept = [r.text for f in doc.frames for r in f.regions]
    assert kept == ["루피야"]
    assert len(dropped) == len(REAL_NOISE)          # 조용히 사라지지 않는다
    assert {d["text"] for d in dropped} == set(REAL_NOISE)


def test_frames_that_become_empty_are_dropped():
    doc, _ = filter_localizable(_doc(REAL_NOISE), {})
    assert doc.frames == []


def test_switch_off_restores_the_old_behaviour():
    """회귀 0 탈출구 — 켠 적 없는 채널이 생기면 이 값으로 종전과 같아진다."""
    doc, dropped = filter_localizable(_doc(REAL_NOISE),
                                      {"detect": {"localizable_only": False}})
    assert dropped == [] and len(doc.frames[0].regions) == len(REAL_NOISE)


def test_human_roi_is_never_filtered():
    """사람이 `--subtitle-area` 로 '여기가 자막이다' 라고 말한 실행은 거르지 않는다."""
    doc, dropped = filter_localizable(_doc(REAL_NOISE, roi=(0, 0, 100, 100)), {})
    assert dropped == [] and len(doc.frames[0].regions) == len(REAL_NOISE)


def test_the_filter_runs_before_detections_are_saved():
    """🛑 마스크·렌더·검수 카드가 **같은 목록**을 봐야 한다 — 저장 전에 거른다.

    저장 뒤에 거르면 마스크(인페인팅)는 걸러지지 않은 목록을 쓴다(그것이 이 사고다)."""
    src = pathlib.Path("app/localize/overlay/detect.py").read_text(encoding="utf-8")
    body = src.split("def detect(", 1)[1]
    assert body.index("filter_localizable(doc, config)") < body.index("doc.save(out)")
    assert "detections_dropped.json" in body
