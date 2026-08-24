"""소스 영상 밖 클립을 잡는다 — 반쪽짜리 쇼츠가 조용히 나가던 것 (2026-08-24).

렌더는 클립마다 `-ss start -to end -i <소스>` 로 읽는다. 구간이 소스 끝을 넘으면
그만큼 **조용히 짧아지고**, 시작 자체가 소스 밖이면 **프레임도 오디오도 0개**다.
concat 은 남은 클립만 이어 붙이므로 경고 한 줄 없이 절반짜리가 발행된다.

실측(185개 런 중 5건 · 4개 채널) — 전부 클립 2개짜리에서 하나가 통째로 증발:

    놀라운 토요일     33.5s 기획 → 15.5s   (-18.0s)
    샤먼: 미신전       49.0s 기획 → 28.0s   (-21.0s)
    언니네 산지직송     49.0s 기획 → 22.4s   (-26.6s)
    놀라운 토요일     47.5s 기획 → 29.5s   (-18.0s)
    혜미리예채파 2화   51.0s 기획 → 25.0s   (-26.0s)  ← 소스 190s, payoff 클립 200~226s

⚠ 원인은 Gemini 가 소스 길이 밖 타임스탬프를 만드는 것이고, 파이프라인 어디에도
클립 경계를 소스 길이와 대조하는 코드가 없었다.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import CLIP_LOST_TOLERANCE_SEC, clips_beyond_source  # noqa: E402


@dataclass
class _Clip:
    start_sec: float
    end_sec: float


def test_the_real_incident_is_caught():
    """혜미리예채파 2화 — 소스 189.99s 인데 payoff 가 200.0~226.0s (통째로 증발)."""
    clips = [_Clip(31.5, 56.5), _Clip(200.0, 226.0)]
    bad = clips_beyond_source(clips, 189.9898)
    assert len(bad) == 1
    b = bad[0]
    assert b["index"] == 1
    assert b["rendered"] == 0.0                 # 프레임 0개
    assert b["lost"] == 26.0                    # 기획 26초를 통째로 잃는다


def test_a_partially_truncated_clip_is_caught_too():
    """시작은 안이지만 끝이 크게 넘치면 그만큼 조용히 짧아진다 — 그것도 잡는다."""
    bad = clips_beyond_source([_Clip(170.0, 200.0)], 190.0)
    assert len(bad) == 1
    assert bad[0]["rendered"] == 20.0 and bad[0]["lost"] == 10.0


def test_a_clean_storyline_passes():
    """회귀 0 — 정상 편은 한 건도 안 잡힌다(전 채널 생성 경로다)."""
    clips = [_Clip(31.5, 56.5), _Clip(136.0, 145.0), _Clip(150.0, 180.0)]
    assert clips_beyond_source(clips, 189.9898) == []


def test_an_exact_end_boundary_is_not_flagged():
    """끝이 소스 길이와 정확히 같은 것은 정상이다(실측에서 흔하다)."""
    assert clips_beyond_source([_Clip(100.0, 189.9898)], 189.9898) == []


def test_sub_second_overrun_is_tolerated():
    """소수점 어긋남(실측 0~0.2s)으로 편을 죽이지 않는다."""
    assert clips_beyond_source([_Clip(100.0, 190.2)], 190.0) == []
    assert clips_beyond_source([_Clip(100.0, 190.0 + CLIP_LOST_TOLERANCE_SEC - 0.01)],
                               190.0) == []


def test_just_past_the_tolerance_is_flagged():
    """경계 바로 밖은 잡힌다 — 임계가 실제로 동작하는지 못박는다."""
    bad = clips_beyond_source([_Clip(100.0, 190.0 + CLIP_LOST_TOLERANCE_SEC + 0.01)], 190.0)
    assert len(bad) == 1


def test_unknown_source_duration_does_not_judge():
    """소스 길이를 못 읽었으면 판정하지 않는다 — 오판으로 멀쩡한 편을 죽이지 않는다."""
    clips = [_Clip(200.0, 226.0)]
    assert clips_beyond_source(clips, 0.0) == []
    assert clips_beyond_source(clips, None) == []


def test_every_offending_clip_is_reported_not_just_the_first():
    """사람이 한 번에 전체 그림을 봐야 한다 — 하나만 알려주면 재시도를 반복하게 된다."""
    clips = [_Clip(0.0, 10.0), _Clip(200.0, 226.0), _Clip(300.0, 320.0)]
    assert [b["index"] for b in clips_beyond_source(clips, 190.0)] == [1, 2]


def test_empty_input_is_safe():
    assert clips_beyond_source([], 190.0) == []
    assert clips_beyond_source(None, 190.0) == []
