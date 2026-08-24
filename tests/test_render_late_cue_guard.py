"""영상 밖에서 시작하는 TTS cue 를 렌더가 싣지 않는다 (2026-08-24).

SHOTCONE 혜미리예채파 2화 실측:

    비디오 스트림  25.025s   (마지막 비디오 패킷 24.93s — 이후 프레임이 **아예 없다**)
    컨테이너      39.400s
    cue 창        37.0 ~ 40.5s   ← 영상이 끝난 지 12초 뒤에 배치돼 있었다

렌더는 `amix=duration=longest` 를 `-shortest` 없이 섞으므로 그 cue 가 컨테이너를
14.4초 늘렸다. 시청자는 정지 화면 위로 내레이션만 듣는다.

상류(`pipeline._resolve_cue_anchors`)는 cue 를 영상 안에 가두는데(`end = min(...)`)
**그 클램프는 정상 동작했다** — 기준이 된 영상 길이가 소스 밖 클립을 포함한 기획
길이(51.0s)였을 뿐이다(아래 마지막 테스트가 숫자로 못박는다). 이 파일이 지키는 것은
**렌더 직전의 안전망**이다 — 상류가 무엇을 놓치든 증상을 끊는다.

  ① 영상 밖에서 **시작하는** cue 만 버린다
  ② 영상 안에서 시작하면 끝이 넘쳐도 **안 건드린다**(사람이 의도한 소리다 — 별건)
  ③ 클립 정보가 없으면 아무것도 안 버린다(가드 오작동이 꼬리보다 나쁘다)
  ④ 판정은 배속에 좌우되지 않는다(양변이 함께 ÷S 되어 상쇄된다)
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.renderer import cues_within_video, video_out_duration  # noqa: E402


@dataclass
class _Clip:
    start_sec: float
    end_sec: float


def _cue(start, end, text="내레이션"):
    return {"path": "x.mp3", "cue_index": 0,
            "cue": {"start_sec": start, "end_sec": end, "text": text}}


# 실제 사고를 재현하는 클립 구성: 합계 25.025초
CLIPS = [_Clip(100.0, 115.0), _Clip(150.0, 160.025)]


def test_video_out_duration_is_the_sum_of_clips():
    assert video_out_duration(CLIPS) == pytest.approx(25.025)


def test_video_out_duration_divides_by_speed():
    """배속이 걸리면 출력 영상은 그만큼 짧다."""
    assert video_out_duration(CLIPS, 2.0) == pytest.approx(25.025 / 2)


def test_the_real_incident_cue_is_dropped():
    """이번 사고의 숫자 — 37.0s cue 는 25.025s 영상 밖이라 실리면 안 된다."""
    kept, dropped = cues_within_video([_cue(4.8, 8.3), _cue(37.0, 40.5)], CLIPS)
    assert [c["cue"]["start_sec"] for c in kept] == [4.8]
    assert [c["cue"]["start_sec"] for c in dropped] == [37.0]


def test_a_cue_that_starts_inside_is_kept_even_if_its_end_overruns():
    """② — 화면 위에서 들리기 시작하는 소리는 사람이 의도한 것이다. 건드리지 않는다."""
    kept, dropped = cues_within_video([_cue(24.0, 30.0)], CLIPS)
    assert len(kept) == 1 and dropped == []


def test_a_normal_episode_is_untouched():
    """회귀 0 — 정상 편은 cue 가 한 건도 안 빠진다(전 채널 KR 렌더 경로다)."""
    cues = [_cue(1.0, 5.0), _cue(9.0, 12.0), _cue(20.0, 24.0)]
    kept, dropped = cues_within_video(cues, CLIPS)
    assert kept == cues and dropped == []


def test_no_clips_means_no_dropping():
    """③ — 가드가 오작동해 멀쩡한 내레이션을 지우는 것이 꼬리보다 나쁘다."""
    cues = [_cue(37.0, 40.5)]
    assert cues_within_video(cues, []) == (cues, [])
    assert cues_within_video(cues, None) == (cues, [])


@pytest.mark.parametrize("speed", [1.0, 1.5, 2.0])
def test_the_verdict_is_scale_invariant(speed):
    """④ — 배속은 판정을 **바꾸지 못한다**: cue 시작과 영상 길이가 함께 ÷S 되므로
    `start/S >= total/S` 는 `start >= total` 과 같다(S>0). 그래도 필터가 나누는 것과
    같은 좌표계로 재는 편이 정직해서 나눗셈을 남겨 뒀다 — 이 테스트가 그 사실을 못박는다."""
    inside, outside = _cue(4.8, 8.3), _cue(37.0, 40.5)
    kept, dropped = cues_within_video([inside, outside], CLIPS, speed)
    assert [c["cue"]["start_sec"] for c in kept] == [4.8]
    assert [c["cue"]["start_sec"] for c in dropped] == [37.0]


def test_zero_or_negative_speed_does_not_crash():
    """망가진 배속 값으로 렌더가 죽으면 안 된다(0 나눗셈)."""
    for bad_speed in (0.0, -1.0):
        kept, _ = cues_within_video([_cue(4.8, 8.3)], CLIPS, bad_speed)
        assert len(kept) == 1


def test_a_malformed_cue_is_kept_not_crashed():
    """시간이 깨진 cue 로 렌더가 죽으면 안 된다 — 판정을 포기하고 싣는다."""
    bad = {"path": "x.mp3", "cue": {"start_sec": None, "end_sec": 3.0}}
    kept, dropped = cues_within_video([bad], CLIPS)
    assert kept == [bad] and dropped == []


# ═══════════════════════════════════════════════════════════════════
# 상류 규명 (2026-08-24) — cue 는 클램프를 **우회하지 않았다**
# ═══════════════════════════════════════════════════════════════════
def test_the_late_cue_came_from_the_planned_total_not_a_bypass():
    """이 파일 위쪽이 '어디서 샜는지 모른다'고 적었던 그 cue 의 출처.

    DB 실측(혜미리예채파_2b2b46c6, checkpoint_story/edit_plan):

        최종 클립   [31.5, 56.5] · [200.0, 226.0]   ← 두 번째는 소스(189.99s) 밖
        cue 앵커     source_time_sec=212.0, duration_sec=3.5

    `_resolve_cue_anchors` 는 212.0 을 담는 조각 (200,226,base=25.0) 을 정확히 찾아
    start = 25.0 + (212.0-200.0) = **37.0**, end = min(40.5, total 51.0) = 40.5 로
    놓았다 — 관측값과 같다. 즉 클램프는 정상 동작했고, **기준이 된 total 51.0 이
    소스 밖 클립을 포함한 기획 길이**였을 뿐이다. 실제 렌더는 25.025s 에서 끝난다.

    ⇒ 원인은 소스 밖 클립 하나이고(상류에서 차단됨), 이 파일의 가드는 그와 무관하게
      증상을 끊는 안전망이다. style_compose 와도 무관하다.
    """
    from app.modules.story_builder import StoryClip
    from app.pipeline import _resolve_cue_anchors

    def _clip(start, end, ci):
        return StoryClip(role="x", start_sec=start, end_sec=end, subtitle="",
                         use_original_audio=True, chunk_index=0, candidate_index=ci)

    planned = [_clip(31.5, 56.5, 0), _clip(200.0, 226.0, 2)]
    cue = {"text": "결국 0캐시로 전부 날려버렸다.", "source_time_sec": 212.0,
           "duration_sec": 3.5, "chunk_index": 0, "candidate_index": 2}

    resolved = _resolve_cue_anchors([cue], planned)
    assert len(resolved) == 1
    assert resolved[0]["start_sec"] == pytest.approx(37.0)
    assert resolved[0]["end_sec"] == pytest.approx(40.5)

    # 같은 cue 를 **실제로 렌더되는** 길이(25.025s)와 맞대면 이 파일의 가드가 잡는다.
    kept, dropped = cues_within_video(
        [{"path": "x.mp3", "cue": resolved[0]}], CLIPS)
    assert kept == [] and len(dropped) == 1
