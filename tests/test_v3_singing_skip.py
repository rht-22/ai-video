"""노래 구간 자막 제외(2026-09-07, 가왕쇼) + 로고 아래 캡션 + design 템플릿 — 회귀 가드.

고정하는 것:
  · singing: 박이 규칙적인 합성 신호는 양성, 무작위 버스트(말 흉내)는 음성 · 결정성 ·
    짧은 양성은 버리고 틈은 잇는다 · Stage 2 문장 두 번째 증인(노래 근거 없으면 기각)
  · word_subtitles(skip_windows): 창 안 줄만 빠지고 건별 기록, None 이면 종전 동일
  · work_caption: 캡션 줄이 로고/작품명 아래에 그려지고 하단 한계가 그만큼 올라간다 ·
    미지정이면 필터 문자열 바이트 동일 · finalize 스택 추정이 같은 수식
  · design 템플릿: 명시 플래그 > 템플릿, 모르는 키 즉시 실패, gawangsho.json 이 로드된다
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v3_stage3 import _mk_grid, _mk_stage2  # noqa: E402

from app.config import DesignConfig  # noqa: E402
from app.v3 import assemble, singing, story as st  # noqa: E402

SR = singing.SAMPLE_RATE


def _music(sec: float, bpm: float = 120.0, seed: int = 0) -> np.ndarray:
    """킥(박마다 감쇠 노이즈 버스트) + 지속 톤 — 반주 흉내."""
    rng = np.random.default_rng(seed)
    n = int(sec * SR)
    x = 0.05 * np.sin(2 * np.pi * 220 * np.arange(n) / SR)
    step = int(SR * 60 / bpm)
    for i in range(0, n, step):
        L = min(int(0.05 * SR), n - i)
        x[i:i + L] += rng.standard_normal(L) * np.exp(-np.arange(L) / (0.01 * SR)) * 0.8
    return x.astype(np.float32)


def _speech_like(sec: float, seed: int = 1) -> np.ndarray:
    """불규칙 간격의 노이즈 버스트 — 말 흉내(박 없음)."""
    rng = np.random.default_rng(seed)
    n = int(sec * SR)
    x = np.zeros(n, dtype=np.float32)
    t = 0
    while t < n:
        L = min(int(rng.uniform(0.08, 0.35) * SR), n - t)
        x[t:t + L] = rng.standard_normal(L) * 0.3
        t += L + int(rng.uniform(0.05, 0.6) * SR)
    return x


def test_beat_curve_separates_music_from_speech_like_and_is_deterministic():
    m = singing.beat_curve(_music(20))
    assert min(c["beat"] for c in m) >= singing.BEAT_MIN
    assert min(c["h2"] for c in m) >= singing.H2_MIN
    wins = singing.singing_windows(m)
    assert len(wins) == 1 and wins[0][0] == 0.0 and wins[0][1] >= 16.0   # 마지막 창은 18s 에서 끝난다
    # 말 흉내(불규칙 버스트)는 피크가 우연히 0.28 을 넘는 창이 있어도(실측 seed 1: 0.349)
    # 2배 지연 배음(h2)이 안 서서 양성이 되지 않는다 — 두 조건이 한 벌인 이유
    for seed in range(1, 6):
        assert singing.singing_windows(singing.beat_curve(_speech_like(20, seed=seed))) == []
    assert singing.beat_curve(_music(20)) == m                    # 결정성


def test_singing_windows_merge_gap_and_drop_short_runs():
    cur = [{"t0": 2.0 * i, "t1": 2.0 * i + 4, "beat": 0.0, "h2": 0.0} for i in range(30)]
    for i in list(range(0, 6)) + list(range(8, 12)):           # 0~14 · 16~26 (틈 2s → 잇는다)
        cur[i].update(beat=0.5, h2=0.3)
    cur[20].update(beat=0.5, h2=0.3)                           # 40~44 단발 — 8s 미만 → 버림
    assert singing.singing_windows(cur) == [(0.0, 26.0)]
    assert singing.singing_windows([]) == []


def test_confirm_windows_needs_stage2_singing_hint():
    rows = [{"idx": 0, "t0": 0.0, "t1": 30.0, "content": "가왕들이 관객에게 감사를 전한다"},
            {"idx": 1, "t0": 30.0, "t1": 90.0, "content": "전유진이 '올래'를 열창한다"}]
    ok, rej = singing.confirm_windows([(2.0, 20.0), (40.0, 80.0), (200.0, 220.0)], rows)
    assert ok == [(40.0, 80.0)]
    assert [r["t0"] for r in rej] == [2.0, 200.0] and "근거 없음" in rej[0]["why"]


def test_detect_singing_on_file_roundtrip(tmp_path):
    import wave
    x = np.concatenate([_speech_like(12), _music(24), _speech_like(12)])
    f = tmp_path / "a.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    doc = singing.detect_singing(f)
    assert doc["schema"] == singing.SCHEMA and len(doc["windows"]) == 1
    a, z = doc["windows"][0]
    assert 8.0 <= a <= 14.0 and 34.0 <= z <= 40.0            # 창 경계(2s 격자) 안에서 노래 구간


# ── word_subtitles(skip_windows) ─────────────────────────────────────────────

GRID = _mk_grid([
    (0.0, 2.0, True, "여러분 기다리셨습니다"),   # sp0000 말
    (2.0, 5.0, True, "올래 올래 튕기지 말고"),   # sp0001 노래
    (5.0, 7.0, True, "고맙습니다"),              # sp0002 말
])
S2 = _mk_stage2(GRID, [(0, 2, 4, "무대")])
IDX, ORDER = st.build_span_index(S2, GRID)
# _mk_grid 는 words 를 비워 둔다 — 어절 자막은 단어 타임코드에서 나오므로 span 텍스트를
# 어절 단위로 균등 배치해 채운다(중점 소속 규율 그대로)
for _sp in GRID["span_candidates"]:
    if _sp["is_audio"]:
        _ws = _sp["text"].split()
        _d = (_sp["t_out"] - _sp["t_in"]) / len(_ws)
        GRID["words"].extend({"text": w, "t0": round(_sp["t_in"] + i * _d, 3),
                              "t1": round(_sp["t_in"] + (i + 1) * _d, 3), "prob": 0.95}
                             for i, w in enumerate(_ws))
TL = [{"clip_start_sec": 0.0, "clip_end_sec": 7.0, "use_original_audio": True,
       "span_ids": ["sp0000", "sp0001", "sp0002"]}]


def test_word_subtitles_skip_windows_drops_only_lines_inside_and_logs():
    base = assemble.word_subtitles(TL, IDX, GRID["words"])
    log: list[dict] = []
    out = assemble.word_subtitles(TL, IDX, GRID["words"], skip_windows=[(2.0, 5.0)], skip_log=log)
    assert [s["text"] for s in base] == ["여러분 기다리셨습니다", "올래 올래 튕기지 말고", "고맙습니다"]
    assert [s["text"] for s in out] == ["여러분 기다리셨습니다", "고맙습니다"]
    assert len(log) == 1 and log[0]["span_id"] == "sp0001" and log[0]["text"] == "올래 올래 튕기지 말고"
    assert assemble.word_subtitles(TL, IDX, GRID["words"], skip_windows=None) == base


# ── work_caption ─────────────────────────────────────────────────────────────

def _inputs(d):
    from app.modules.renderer import RenderInputs
    from app.modules.story_builder import StoryClip
    clip = StoryClip(role="hook", start_sec=0.0, end_sec=10.0, subtitle="s", use_original_audio=True)
    return RenderInputs(video_path=Path("src.mp4"), clips=[clip], subtitle_path=None,
                        crop_timeline_map={}, title_text="제목", work_title="가왕쇼",
                        output_path=Path("out.mp4"), canvas_width=1080, canvas_height=1920,
                        top_title_height=250, bottom_label_height=170, design=d)


def test_renderer_caption_draws_below_work_and_raises_bottom_limit(tmp_path):
    from PIL import Image
    from app.modules.renderer import _build_filtergraph
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (2501, 1127)).save(logo)
    base = DesignConfig(work_type="image", work_value=str(logo), work_image_width=620,
                        work_image_height=280, work_image_align="center", aspect_ratio="13:9",
                        video_y=500)
    cap = DesignConfig(**{**base.__dict__, "work_caption": "티빙에서 풀버전 시청 및 투표 가능!"})
    a = _build_filtergraph(_inputs(base), 1, 0)
    b = _build_filtergraph(_inputs(cap), 1, 0)
    assert "[with_cap]" not in a and "with_cap" in b
    assert "text='티빙에서 풀버전 시청 및 투표 가능!'" in b
    # 로고(278px)가 center 로 앉는 범위가 캡션 블록(56+12)만큼 줄어 34px 위로 올라간다
    ya = int(a.split("overlay=(W-w)/2:")[1].split("[")[0])
    yb = int(b.split("overlay=(W-w)/2:")[1].split("[")[0])
    assert ya - yb == (56 + 12) // 2
    assert f":y={yb + 278 + 12}+(56-text_h)/2[with_cap]" in b     # 캡션 = 로고 아랫변 + 12


def test_renderer_caption_text_work_title_moves_up_when_clamped():
    from app.modules.renderer import _build_filtergraph
    base = DesignConfig(work_type="text", work_title_y=1860, aspect_ratio="13:9", video_y=500)
    cap = DesignConfig(**{**base.__dict__, "work_caption": "캡션"})
    a = _build_filtergraph(_inputs(base), 1, 0)
    b = _build_filtergraph(_inputs(cap), 1, 0)
    assert ":y=1844[with_work]" in a                # 1900 − 56
    assert ":y=1776[with_work]" in b                # 1900 − 68 − 56
    assert ":y=1844+(56-text_h)/2[with_cap]" in b   # 1776 + 56 + 12


def test_finalize_stack_reserves_caption_like_renderer(tmp_path):
    from PIL import Image
    from app.v3.finalize import estimate_work_height, estimate_work_top, work_caption_block
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (2501, 1127)).save(logo)
    base = DesignConfig(work_type="image", work_value=str(logo), work_image_width=620,
                        work_image_height=280, work_image_align="center")
    cap = DesignConfig(**{**base.__dict__, "work_caption": "캡션", "work_caption_font_size": 40})
    assert work_caption_block(base) == 0 and work_caption_block(cap) == 56 + 12
    assert estimate_work_height(cap) == estimate_work_height(base) + 68
    assert estimate_work_top(base, band_bottom=1246) - estimate_work_top(cap, band_bottom=1246) == 34
    t = DesignConfig(work_type="text", work_title_y=1860, work_caption="캡션")
    assert estimate_work_top(t, band_bottom=1246) == 1776


# ── design 템플릿 ────────────────────────────────────────────────────────────

def test_design_preset_fills_only_unset_flags_and_bundled_gawangsho_loads():
    from app.v3.cli import (CHANNEL_DESIGN_ARGS, apply_design_preset, build_parser,
                            load_design_preset)
    preset = load_design_preset("gawangsho")
    assert set(preset["design"]) <= set(CHANNEL_DESIGN_ARGS)
    assert preset["design"]["work_caption"] == "티빙에서 풀버전 시청 및 투표 가능!"
    assert preset["options"] == {"no_reframe": True, "subtitle_skip_singing": True}
    args = build_parser().parse_args(["--video", "x.mp4", "--work-title", "가왕쇼",
                                      "--design-video-y", "440"])
    filled = apply_design_preset(args, preset)
    assert args.design_video_y == 440 and "video_y" not in filled       # 명시가 이긴다
    assert args.design_work_caption == preset["design"]["work_caption"]
    assert args.no_reframe is True and args.subtitle_skip_singing is True


def test_design_preset_unknown_name_or_key_fails_loud(tmp_path):
    from app.v3.cli import load_design_preset
    with pytest.raises(SystemExit):
        load_design_preset("없는이름", base_dir=tmp_path)
    (tmp_path / "bad.json").write_text('{"design": {"nope": 1}}', encoding="utf-8")
    with pytest.raises(SystemExit):
        load_design_preset("bad", base_dir=tmp_path)
    (tmp_path / "bad2.json").write_text('{"options": {"launch_nukes": true}}', encoding="utf-8")
    with pytest.raises(SystemExit):
        load_design_preset("bad2", base_dir=tmp_path)


def test_renderer_logo_follows_band_offset_not_absolute_work_title_y(tmp_path):
    from PIL import Image
    from app.modules.renderer import _build_filtergraph
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (2501, 1127)).save(logo)
    d = DesignConfig(work_type="image", work_value=str(logo), work_image_width=620,
                     work_image_height=280, work_image_align="center", aspect_ratio="13:9",
                     video_y=420, work_band_offset=40, work_title_y=1430)
    fg = _build_filtergraph(_inputs(d), 1, 0)
    band_bottom = 420 + 746
    assert f"overlay=(W-w)/2:{band_bottom + 40}[" in fg          # 1206 — 1430 하한 무시


def test_bridge_windows_joins_gaps_inside_singing_meaning_and_only_extends():
    rows = [{"idx": 0, "t0": 0.0, "t1": 20.0, "content": "MC 멘트"},
            {"idx": 1, "t0": 20.0, "t1": 100.0, "content": "듀엣 무대로 월미도를 달군다"},
            {"idx": 2, "t0": 100.0, "t1": 130.0, "content": "관객과 대화"}]
    wins = [(22.0, 40.0), (48.0, 60.0), (80.0, 96.0), (110.0, 120.0)]
    out = singing.bridge_windows(wins, rows)
    # 노래 단위(20~100) 안 세 창은 하나로 잇고 경계(20·100)까지 늘린다(각 ≤10s). 밖의 창은 그대로
    assert out == [(20.0, 100.0), (110.0, 120.0)]
    # 경계가 10s 넘게 멀면 늘리지 않는다(MC 멘트 보존) · 다음 단위로 넘어간 창은 자르지 않는다
    rows2 = [{"idx": 1, "t0": 20.0, "t1": 100.0, "content": "'올래' 솔로 무대"}]
    assert singing.bridge_windows([(37.0, 130.0)], rows2) == [(37.0, 130.0)]
    assert singing.bridge_windows([], rows) == []
    assert singing.bridge_windows([(5.0, 15.0)], rows) == [(5.0, 15.0)]     # 근거 없는 단위는 불변
