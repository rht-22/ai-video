"""프레임 격자 정렬(2026-09-03) — 편집본 좌표가 렌더의 실제 타임라인과 맞는지.

계기(실측 · outputs/지금불륜이문제가아닙니다_b0ccda99):
  렌더는 클립을 따로 잘라(-ss/-to) concat 한다. concat 은 세그먼트의 영상 길이를
  **소리 길이에 맞추려고 마지막 프레임을 복제**하므로 세그먼트는 늘 프레임 정수 개다.
  계획 55.559s·13조각인데 완성본은 1338프레임 = Σceil(길이×fps) — 한 프레임도 안 틀렸다.

  그런데 편집본 좌표는 계획값의 **실수 누적합**이었다. 그래서 조각을 지날 때마다 좌표가
  밀려(뒤로 갈수록 커져 최대 0.3초), 덮개 뮤트 창이 화면보다 먼저 시작하고 먼저 끝났다:
    · 앞  — 화면은 아직 앞 장면인데 원음이 꺼져 대사가 말하다 말고 잘린다
    · 뒤  — 화면은 아직 덮개인데 원음이 켜져 덮개 꼬리의 "무슨 일 있어?"가 새어나온다
  자막·cue·라벨·효과음이 모두 같은 좌표를 쓰므로 함께 밀렸다.

고정하는 것:
  · fps 를 주면 좌표 누적이 프레임 격자 위에 놓인다 · 안 주면 종전 실수 누적(회귀 0)
  · 렌더가 클립을 **정확히 그 프레임 수**로 못 박는다(trim=end_frame) — 좌표와 같은 식
  · 덮개 뮤트 창이 클립의 실제 끝까지 빈틈없이 덮는다(유출 0)
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.config import DesignConfig
from app.modules.renderer import RenderInputs, _build_filtergraph
from app.modules.story_builder import StoryClip
from app.v3 import assemble, finalize

FPS = 24000 / 1001          # 23.976023976… — 실사고 소재의 fps

# 실사고 편의 계획 길이 13개(초). 마지막에서 두 번째가 문제의 덮개 ③.
REAL_DURS = [2.470, 6.976, 2.183, 7.922, 2.335, 1.406, 1.847, 14.751,
             2.288, 6.070, 4.300, 0.638, 2.373]
COVER_IDX = {0, 2, 8, 12}   # use_original_audio=False 인 덮개 클립


def _timeline(durs=REAL_DURS, covers=COVER_IDX) -> list[dict]:
    """소스 시각이 서로 떨어진 13조각(실사고와 같은 모양)."""
    out, t = [], 100.0
    for i, d in enumerate(durs):
        out.append({"role": "hook", "clip_start_sec": round(t, 3),
                    "clip_end_sec": round(t + d, 3), "subtitle": "",
                    "use_original_audio": i not in covers, "span_ids": []})
        t += d + 7.0            # 조각 사이에 소스 간격 — 이어 붙는 게 아님을 분명히
    return out


def _inputs(clips: list[StoryClip], fps: float | None) -> RenderInputs:
    return RenderInputs(
        video_path=Path("src.mp4"), clips=clips, subtitle_path=None,
        crop_timeline_map={}, title_text="제목", work_title="작품",
        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
        top_title_height=250, bottom_label_height=170,
        design=DesignConfig(), source_fps=fps)


# ── 격자 산술 ───────────────────────────────────────────────────────────────

def test_no_fps_keeps_float_durations():
    """fps 미상 = 종전 그대로. 옛 판·비-v3 경로가 한 톨도 안 바뀐다."""
    assert assemble.clip_frames(2.288, None) is None
    assert assemble.clip_duration(2.288, None) == 2.288
    for bad in (0, -1, None):
        assert assemble.clip_duration(2.288, bad) == 2.288


def test_duration_snaps_to_whole_frames():
    n = assemble.clip_frames(2.288, FPS)
    assert n == round(2.288 * FPS) == 55
    assert assemble.clip_duration(2.288, FPS) == pytest.approx(55 / FPS)
    # 0 프레임 클립은 만들지 않는다 — ffmpeg 가 빈 세그먼트로 즉사한다
    assert assemble.clip_frames(0.0001, FPS) == 1


def test_offsets_without_fps_unchanged():
    """회귀 0 — fps 를 안 주면 누적이 종전 실수합 그대로."""
    tl = _timeline()
    offs = assemble.edited_offsets(tl)
    assert offs[0][2] == 0.0
    for i in range(1, len(tl)):
        assert offs[i][2] == pytest.approx(sum(REAL_DURS[:i]))


def test_offsets_land_on_frame_grid():
    """fps 를 주면 모든 클립 시작이 프레임 경계 위에 놓인다."""
    offs = assemble.edited_offsets(_timeline(), FPS)
    for _s, _e, off in offs:
        frames = off * FPS
        assert frames == pytest.approx(round(frames), abs=1e-6), f"{off} 가 격자 밖"


def test_offsets_use_same_frame_count_as_renderer():
    """좌표 누적과 렌더 고정이 **같은 식**을 쓴다 — 이게 어긋나면 결함이 재발한다."""
    offs = assemble.edited_offsets(_timeline(), FPS)
    for i, d in enumerate(REAL_DURS[:-1]):
        step = offs[i + 1][2] - offs[i][2]
        assert step == pytest.approx(assemble.clip_frames(d, FPS) / FPS)


# ── 렌더가 그 길이를 지키는가 ───────────────────────────────────────────────

def test_renderer_without_fps_keeps_legacy_filtergraph():
    """source_fps 미지정 = 필터그래프 종전과 동일(고정 필터가 안 붙는다)."""
    clips = [StoryClip(role="hook", start_sec=0.0, end_sec=5.0, subtitle="s",
                       use_original_audio=True)]
    fg = _build_filtergraph(_inputs(clips, None), 1, 0)
    assert "[0:a]anull[a0]" in fg
    for banned in ("trim=end_frame", "tpad=", "apad,", "asetpts"):
        assert banned not in fg, f"미지정인데 {banned} 가 붙었다"


def test_renderer_pins_each_clip_to_its_frame_count():
    """클립마다 trim=end_frame=N · 소리도 같은 길이 — concat 이 덧댈 여지를 없앤다."""
    clips = [StoryClip(role="hook", start_sec=s, end_sec=s + d, subtitle="",
                       use_original_audio=True)
             for s, d in ((100.0, 2.288), (200.0, 6.070))]
    fg = _build_filtergraph(_inputs(clips, FPS), 2, 0)
    for i, d in enumerate((2.288, 6.070)):
        n = assemble.clip_frames(d, FPS)
        assert f"trim=end_frame={n},setpts=PTS-STARTPTS[v{i}]" in fg
        assert f"[{i}:a]apad,atrim=end={n / FPS:.6f},asetpts=PTS-STARTPTS[a{i}]" in fg
    # 소스 끝에 걸려 프레임이 모자랄 때를 대비한 복제(남으면 trim 이 잘라낸다)
    assert fg.count("tpad=stop_mode=clone") == 2


# ── 결함 자체 ───────────────────────────────────────────────────────────────

def test_cover_mute_covers_clip_to_its_rendered_end():
    """덮개 뮤트 창이 **다음 클립이 시작하는 바로 그 시각**까지 이어진다.

    실사고: 창이 계획 길이로 끝나 화면보다 0.32초 먼저 풀렸고, 그 사이 덮개 꼬리의
    원본 대사가 잘린 채 새어나왔다. 창 끝 < 다음 클립 시작이면 그만큼 샌다."""
    tl = _timeline()
    offs = assemble.edited_offsets(tl, FPS)
    wins = finalize.cover_mute_windows(tl, [], FPS)
    assert len(wins) == len(COVER_IDX)
    for (w0, w1), i in zip(wins, sorted(COVER_IDX)):
        assert w0 == pytest.approx(offs[i][2], abs=5e-4), f"덮개 {i} 창이 늦게 시작"
        nxt = (offs[i + 1][2] if i + 1 < len(tl)
               else offs[i][2] + assemble.clip_duration(REAL_DURS[i], FPS))
        assert w1 == pytest.approx(nxt, abs=5e-4), f"덮개 {i} 창이 일찍 끝나 원음이 샌다"


def test_render_and_coordinates_cannot_drift_apart():
    """옛 조합이 실제로 벌어졌음을 못 박고, 지금 조합은 벌어질 수 없음을 고정한다.

    옛 렌더는 concat 패딩 탓에 조각마다 **ceil**(길이×fps) 프레임을 냈는데 좌표는
    실수 누적이었다 — 오차가 한 방향으로만 쌓인다. 실사고 13조각에서 0.24초가
    벌어졌고(그 위에 PTS 재기록분이 더 얹혀 실측 0.3초), 그만큼 덮개 뮤트가 화면보다
    먼저 풀려 원음이 샜다. 지금은 좌표도 렌더도 같은 clip_frames() 를 쓴다."""
    legacy_render = sum(math.ceil(d * FPS) for d in REAL_DURS) / FPS
    old_coords = sum(REAL_DURS)
    assert legacy_render - old_coords > 0.2, "옛 조합은 실제로 벌어졌다(0.24초)"

    offs = assemble.edited_offsets(_timeline(), FPS)
    coords_total = offs[-1][2] + assemble.clip_duration(REAL_DURS[-1], FPS)
    render_total = sum(assemble.clip_frames(d, FPS) for d in REAL_DURS) / FPS
    assert coords_total == pytest.approx(render_total, abs=1e-9), "좌표와 렌더가 갈라졌다"


def test_partial_window_keeps_original_audio_after_narration():
    """M15 계약 유지 — 내레이션이 점유한 구간만 끄고, 그 뒤 원음은 살린다."""
    tl = [{"role": "hook", "clip_start_sec": 100.0, "clip_end_sec": 110.0,
           "subtitle": "", "use_original_audio": False, "span_ids": []}]
    # 내레이션이 점유한 103~106 만 끈다 — 그 앞뒤(0~3, 6~10)는 원음이 산다
    assert finalize.cover_mute_windows(tl, [(103.0, 106.0)], FPS) == [(3.0, 6.0)]

    # 내레이션이 클립 끝까지 가면 창도 **격자 끝**까지 — 계획 길이로 끊으면 샌다
    tail = finalize.cover_mute_windows(tl, [(106.0, 110.0)], FPS)
    assert tail[-1][1] == pytest.approx(assemble.clip_duration(10.0, FPS), abs=5e-4)
