"""인물 인식 게이트 — `--face-recognition` (2026-08-25).

`enable_face_recognition` 은 기본값이 True 였지만 **한 번도 동작한 적이 없다**:
requirements 에 `tf-keras` 가 없어 `DeepFace.represent` 가 늘

    ValueError: You have tensorflow 2.21.0 and this requires tf-keras package.

로 죽었고, `pipeline` 이 그 예외를 인물별로 삼킨 뒤 화자 추적 폴백으로 갔다
(L-P4 2차 관문 실측, mm-06 · 구/신 스택 양쪽 동일).

`tf-keras` 를 requirements 에 넣으면 그날부터 **처음으로** 동작해 `character_focus`
가 있는 편의 크롭이 인물 타겟으로 바뀐다 — 승인된 화면이 움직인다. 그래서 기본을
꺼짐으로 내리고 플래그로만 켠다. 이 파일이 그 계약을 고정한다.
"""
from __future__ import annotations

import argparse

from app.cli import _build_design_config
from app.config import DesignConfig


def _args(**kw):
    base = {k: None for k in (
        "design_title_y", "design_work_title_y", "design_aspect_ratio", "design_title_font",
        "design_title_size", "design_subtitle_size", "design_subtitle_color",
        "design_subtitle_y_margin", "design_tts_color", "design_tts_size",
        "design_tts_y_margin", "design_work_font_size", "design_work_color",
        "design_work_image_width", "design_work_image_height", "design_work_align",
        "design_subtitle_style", "design_title_color", "design_title_color2",
        "design_work_image")}
    base["no_reframe"] = False
    base["face_recognition"] = False
    base.update(kw)
    return argparse.Namespace(**base)


def test_default_is_off_so_todays_output_is_unchanged():
    """오늘 산출이 그대로라는 뜻이다 — 안 돌던 것을 계속 안 돌린다(회귀 0)."""
    assert DesignConfig().enable_face_recognition is False
    assert _build_design_config(_args()).enable_face_recognition is False


def test_flag_turns_it_on():
    assert _build_design_config(_args(face_recognition=True)).enable_face_recognition is True


def test_flag_does_not_touch_the_other_face_axes():
    """'누구를 따라갈지'와 '크롭을 할지'는 별개 축이다."""
    d = _build_design_config(_args(face_recognition=True))
    assert d.enable_reframe is True
    assert d.enable_speaker_tracking is True


def test_requirements_pins_tf_keras_because_deepface_dies_without_it():
    """이 줄이 사라지면 게이트를 켠 채널이 조용히 화자 추적으로 되돌아간다."""
    from pathlib import Path
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    lines = [ln.split("#", 1)[0].strip() for ln in req.splitlines()]
    assert any(ln.startswith("tf-keras") for ln in lines), "requirements.txt 에 tf-keras 가 없다"


def test_pipeline_says_so_when_the_gate_is_on_but_tmdb_key_is_missing():
    """재료가 없는데 켜 놓으면 사람은 도는 줄 안다 — 조용한 폴백 금지(2026-08-25 실측).

    mm-06 에는 TMDB_API_KEY 가 `/etc/ves/node.env`·`/opt/ves/secrets/ves.env`·launchd
    어디에도 없었다. 그 상태로 게이트를 켜면 배우 사진이 0장이라 레퍼런스가 안 만들어지고
    조용히 화자 추적으로 간다."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "pipeline.py").read_text(encoding="utf-8")
    blk = src.split('print("  [TMDb] TMDB_API_KEY 미설정', 1)[1][:800]
    assert "payload.design.enable_face_recognition" in blk
    assert "화자 추적으로 간다" in blk
