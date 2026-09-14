"""libx264 출력 level 고정(2026-09-11, 지금불륜 ep02full 실사고).

필터그래프 출력의 프레임레이트를 x264 가 못 읽어 자동 level 이 6.2 로 찍혔고, 애플 VideoToolbox(QuickTime·iOS)가
어두운 아웃포커스 구간 프레임을 -12909(bad data)로 거부해 화면이 깨졌다. ffmpeg 소프트웨어 디코드는 멀쩡해 렌더
검증·프레임 QC 가 못 잡았다. 재인코딩 실측: level 4.0 은 VT 실패 0건 · 6.2 는 83건. 1080×1920 ≤64fps 는 4.2 안.
"""
from __future__ import annotations

from app.modules import renderer


def test_libx264_args_pin_level_for_every_preset():
    assert renderer.H264_LEVEL == "4.2"
    for preset in ("fastest", "balanced", "quality", "unknown"):
        args = renderer._video_encoder_args("libx264", preset)
        assert args[-2:] == ["-level", "4.2"], (preset, args)
    # 하드웨어 인코더 인자는 종전 그대로(level 은 libx264 자동 판정 결함에만 대응)
    assert "-level" not in renderer._video_encoder_args("h264_nvenc", "balanced")
