"""소스 밖 클립 가드 — 렌더되지 않는 구간을 비싼 단계 **앞**에서 잡는다.

v1 모놀리스(`app/pipeline.py`)에 있던 것을 V4-M1 §7 규약대로 물리 이동한 것이다.
⚠ **옮긴 이유**: v3 는 v1 모놀리스를 한 줄도 import 하지 못해(비공개·모놀리스 내부)
이 가드를 승계하지 못했다 — 같은 판정을 두 번 적으면 언젠가 한쪽만 고쳐진다.
v4 는 **이 함수를 부른다**. 모놀리스는 `app.pipeline` 에서 같은 이름으로 재수출하므로
v1 의 호출 지점(`pipeline.py` 의 `[clip-bounds]` 블록)은 그대로 돈다.

동작·상수는 이동 전과 **한 글자도 다르지 않다**(회귀 0 이 이동의 조건 —
`tests/test_clip_bounds_guard.py` 가 값으로 고정한다).
"""

from __future__ import annotations

# 소스 밖으로 나가 실제로 렌더되지 않는 분량이 이보다 크면 실패시킨다.
# 1.0s: 끝 경계가 소스 길이와 소수점에서 어긋나는 정상 케이스(실측 0~0.2s)는 통과시키고,
# 클립이 통째로/대부분 증발하는 경우만 잡는다.
CLIP_LOST_TOLERANCE_SEC = 1.0


def clips_beyond_source(clips, source_duration_sec: float,
                        tolerance: float = CLIP_LOST_TOLERANCE_SEC) -> list[dict]:
    """소스 영상 밖이라 **실제로 렌더되지 않는** 클립을 찾는다. 순수(테스트 대상).

    ⚠ **왜 필요한가**: 렌더는 클립마다 `-ss start -to end -i <소스>` 로 읽는데, 구간이
    소스 끝을 넘으면 그만큼 **조용히 짧아지고**, 시작 자체가 소스 밖이면 **프레임도
    오디오도 0개**가 나온다. concat 은 남은 클립만 이어 붙이므로 **반쪽짜리 쇼츠가
    아무 경고 없이 발행된다.**

    2026-08-24 실측(185개 런 중 5건, 4개 채널): 전부 클립 2개짜리에서 하나가 통째로
    증발해 18~26.6초(기획 분량의 절반 가까이)를 잃었다. 예: 혜미리예채파 2화 —
    소스 189.99s 인데 payoff 클립이 200.0~226.0s. 내레이션은 "결국 0캐시로 전부
    날려버렸다"고 말하는데 **그 장면이 화면에 없다.**

    Gemini 가 소스 길이 밖 타임스탬프를 만들어내는 것이 원인이고, 파이프라인 어디에도
    클립 경계를 소스 길이와 대조하는 코드가 없었다.

    Returns: 소실이 tolerance 를 넘는 클립별 dict
             (index·start·end·planned·rendered·lost) — 비어 있으면 정상."""
    src = float(source_duration_sec or 0.0)
    if src <= 0:
        return []                      # 소스 길이를 모르면 판정하지 않는다(오판 금지)
    out: list[dict] = []
    for i, c in enumerate(clips or []):
        start = float(getattr(c, "start_sec", 0.0))
        end = float(getattr(c, "end_sec", 0.0))
        planned = max(0.0, end - start)
        rendered = max(0.0, min(end, src) - start)
        lost = planned - rendered
        if lost > tolerance:
            out.append({"index": i, "start": start, "end": end,
                        "planned": planned, "rendered": rendered, "lost": lost})
    return out
