"""인물 좌표 lookup 모듈 (PR-7).

ArcFace/deepface 기반 사전 인식(FaceIdentifier)은 제거되었다. 인물 등장 좌표(character_index)는
이제 gemini 의 characters_tracking 출력에서 생성되며(app/pipeline.py:_build_character_appearances),
reframe 는 이 함수로 'opencv 가 탐지한 얼굴들 중 어느 것이 타깃 인물인지'를 좌표로 식별한다.

데이터 형식 (character_index):
    [{"character": str, "start_sec": float, "end_sec": float,
      "samples": [{"t": float, "x_norm": 0~1, "y_norm": 0~1, "similarity": float}]}]
"""
from __future__ import annotations

from typing import Any


def find_target_in_index(
    appearances: list[dict[str, Any]],
    target_character: str,
    t_sec: float,
    frame_width: int,
    frame_height: int,
    max_dt_sec: float = 3.0,
) -> tuple[float, float] | None:
    """character_index에서 target_character의 t_sec 시점 좌표를 lookup한다.

    가장 가까운 sample을 사용 (선형 보간 미적용 — reframe의 EMA 스무딩이 부드럽게 만든다).
    sample이 없거나 가장 가까운 sample이 max_dt_sec를 넘으면 None.
    좌표는 정규화된 sample을 frame_width/height로 스케일 업한 픽셀 값.
    """
    best_sample: dict[str, Any] | None = None
    best_dt = float("inf")
    for ap in appearances:
        if ap.get("character") != target_character:
            continue
        if ap["start_sec"] - 1.0 <= t_sec <= ap["end_sec"] + 1.0:
            for smp in ap.get("samples") or []:
                dt = abs(smp["t"] - t_sec)
                if dt < best_dt:
                    best_dt = dt
                    best_sample = smp
    if best_sample is None or best_dt > max_dt_sec:
        return None
    return (
        best_sample["x_norm"] * frame_width,
        best_sample["y_norm"] * frame_height,
    )
