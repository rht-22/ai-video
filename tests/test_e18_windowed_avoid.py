"""E18-2 — 몇 초씩만 뜨는 방송 텔롭도 피한다 (2026-08-24).

E17-2 의 전역 판정은 *"표본 프레임의 **절반 이상**에서 걸리는 행"* 만 띠로 본다. 편 내내
같은 자리에 있는 번인 대사 자막에는 맞지만 **방송 텔롭은 몇 초씩만 뜬다.**

실측 근거: SHOTCONE 혜미리예채파 2화는 E17 을 포함한 sha(`6effc4c`)로 돌았는데 run_log 에
`subtitle_avoid_burned` 단계가 **아예 없다**(= 띠 미검출). 그런데 완성본 프레임에는 원본
한국어 텔롭 `냉장고도, 휴지도, 심지어 조명도 없는 이 집에서` 가 뚜렷하게 박혀 있었다.

고친 방식: 표본을 **시각과 함께** 모아 두고, 자막 줄이 떠 있는 **그 창 안에서만** 다시
판정한다. 전역 판정은 그대로 두고 **더 올려야 하는 줄만 추가로 올린다**(단조 개선) —
어떤 줄도 오늘보다 덜 올라가지 않으므로 이미 승인된 화면이 내려가는 일이 없다.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pipeline  # noqa: E402
from app.config import DesignConfig  # noqa: E402
from app.modules import subtitle_region as sr  # noqa: E402

# 기본 채널 기하(1:1) — 밴드 420~1500px, probe 1행 = 4px.
# 대사 자막(margin_v 430)은 캔버스 1327~1490 에 놓이므로, 그 자리에 겹치는 probe 행은
# (1340~1404)/4 = 230~246 이다. E17-2 합성 실측이 검출한 띠(y 1324~1376)와 같은 자리다.
DESIGN = DesignConfig()
GEOM = sr.band_geometry(DESIGN)
BURNED_ROWS = list(range(230, 247))


def _cue(start, end, **kw):
    return SimpleNamespace(start_sec=start, end_sec=end, text="대사", **kw)


def _profiles(spans):
    """[(시각, 글자행 있음?)] → 저장 형식."""
    return [[t, list(BURNED_ROWS) if on else []] for t, on in spans]


# ── 창 안에서만 센다 ────────────────────────────────────────────────────────
def test_only_frames_inside_the_window_are_counted():
    prof = _profiles([(0.0, False), (1.0, True), (1.5, True), (5.0, False)])
    inside = sr.ratios_in_window(prof, 0.9, 1.6)
    assert inside is not None and inside[BURNED_ROWS[0]] == 1.0
    outside = sr.ratios_in_window(prof, 4.0, 6.0, min_frames=1)
    assert outside is not None and outside[BURNED_ROWS[0]] == 0.0


def test_a_window_with_too_few_samples_is_not_judged():
    """1장으로 판정하면 그 장면의 밝은 무늬가 자막이 된다."""
    prof = _profiles([(1.0, True)])
    assert sr.ratios_in_window(prof, 0.5, 1.5) is None
    assert sr.ratios_in_window(prof, 0.5, 1.5, min_frames=1) is not None


def test_the_window_band_lands_on_canvas_coordinates():
    prof = _profiles([(1.0, True), (1.5, True), (2.0, True)])
    rows = sr.band_in_window(prof, 0.9, 2.1, GEOM)
    assert rows is not None
    px_per_row = GEOM.scaled_h / float(sr.PROBE_H)
    assert rows[0] == int(GEOM.top + BURNED_ROWS[0] * px_per_row)
    assert GEOM.top < rows[0] < GEOM.top + GEOM.scaled_h


# ── 줄마다 필요한 만큼만 ────────────────────────────────────────────────────
def _margins(cues, prof, base=430):
    return sr.per_cue_margins(
        cues, prof, GEOM, base_margin_v=base, canvas_height=1920,
        subtitle_height=sr.estimate_subtitle_height(65),
        title_bottom=sr.estimate_title_bottom(DESIGN, GEOM))


def test_the_intermittent_telop_is_caught_for_the_lines_it_overlaps():
    """텔롭이 3~6초에만 떠 있다 — 그 구간의 줄만 올라가고 나머지는 그대로."""
    prof = _profiles([(t / 2, 3.0 <= t / 2 <= 6.0) for t in range(0, 40)])
    margins, notes = _margins([_cue(1.0, 2.5), _cue(3.5, 5.5), _cue(8.0, 9.5)], prof)
    assert margins[0] is None and margins[2] is None
    assert margins[1] is not None and margins[1] > 430
    assert any("구간" in n for n in notes)          # 조용한 이동 금지


def test_a_line_is_never_pushed_lower_than_the_global_margin():
    """단조 개선 — 창 판정이 낮은 자리를 가리켜도 내리지 않는다."""
    prof = _profiles([(t / 2, True) for t in range(0, 20)])
    margins, _ = _margins([_cue(0.0, 9.0)], [], base=900)
    assert margins == [None]
    margins2, _ = _margins([_cue(0.0, 9.0)], prof, base=900)
    assert margins2[0] is None or margins2[0] >= 900


def test_no_profiles_means_no_change_at_all():
    """회귀 0 — 표본이 없으면(구 체크포인트·검출 실패) 종전과 완전히 같다."""
    assert _margins([_cue(1.0, 2.0), _cue(3.0, 4.0)], [])[0] == [None, None]


def test_a_broken_cue_time_does_not_crash():
    prof = _profiles([(1.0, True), (1.5, True)])
    margins, _ = _margins([SimpleNamespace(start_sec=None, end_sec=2.0)], prof)
    assert margins == [None]


# ── 파이프라인 배선 ─────────────────────────────────────────────────────────
class _Cfg:
    canvas_width, canvas_height = 1080, 1920


def _run_windowed(segs, profiles, base=430):
    pipeline._BURNED_PROFILES[:] = profiles
    try:
        return pipeline._avoid_burned_windowed(
            segs, base, design=DESIGN, config=_Cfg(),
            title_text="제목\n두 줄", font_size=65)
    finally:
        pipeline._BURNED_PROFILES[:] = []


def test_segments_are_copied_never_mutated():
    """`final_segments` 가 움직이면 subtitle_segments.json 이 달라진다(편집실 왕복 자료)."""
    prof = _profiles([(t / 2, True) for t in range(0, 20)])
    seg = _cue(1.0, 5.0)
    out = _run_windowed([seg], prof)
    assert not hasattr(seg, "style")               # 원본은 그대로
    assert isinstance(out[0].style, dict) and "y" in out[0].style


def test_a_line_the_editor_positioned_is_left_alone():
    """사람이 y 를 정한 줄은 건드리지 않는다 — 사람이 이긴다."""
    prof = _profiles([(t / 2, True) for t in range(0, 20)])
    seg = _cue(1.0, 5.0, style={"y": 0.9, "size": 70})
    out = _run_windowed([seg], prof)
    assert out[0] is seg


def test_other_style_keys_survive_the_merge():
    """E15 자막 강조(size·color)가 회피 때문에 사라지면 안 된다."""
    prof = _profiles([(t / 2, True) for t in range(0, 20)])
    out = _run_windowed([_cue(1.0, 5.0, style={"size": 88, "color": "#FF4444"})], prof)
    assert out[0].style["size"] == 88 and out[0].style["color"] == "#FF4444"
    assert 0.0 < out[0].style["y"] < 1.0


def test_without_profiles_the_same_objects_come_back():
    seg = _cue(1.0, 5.0)
    assert _run_windowed([seg], []) == [seg]


# ── 표본 좌표계 ─────────────────────────────────────────────────────────────
def test_profile_times_are_edited_timeline_not_source():
    """창을 맞대려면 자막 세그먼트와 같은 좌표계여야 한다(클립 base 누적)."""
    clips = [SimpleNamespace(role="hook", start_sec=100.0, end_sec=102.0),
             SimpleNamespace(role="payoff", start_sec=500.0, end_sec=502.0)]
    blank = bytes(sr.PROBE_W * sr.PROBE_H)

    def sampler(clip, crop_path, rate):
        return [(0.0, blank), (1.0, blank)]

    prof = sr.detect_burned_profiles(Path("x.mp4"), clips, DESIGN, sampler=sampler)
    assert [row[0] for row in prof] == [0.0, 1.0, 2.0, 3.0]    # 두 번째 클립은 base 2.0


def test_a_failing_clip_does_not_lose_the_others():
    clips = [SimpleNamespace(role="a", start_sec=0.0, end_sec=2.0),
             SimpleNamespace(role="b", start_sec=10.0, end_sec=12.0)]
    blank = bytes(sr.PROBE_W * sr.PROBE_H)
    calls = {"n": 0}

    def sampler(clip, crop_path, rate):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ffmpeg 없음")
        return [(0.5, blank)]

    prof = sr.detect_burned_profiles(Path("x.mp4"), clips, DesignConfig(), sampler=sampler)
    assert [row[0] for row in prof] == [2.5]       # 첫 클립 길이 2.0 + 0.5


# ── TTS ASS 의 줄별 MarginV ─────────────────────────────────────────────────
def test_tts_ass_is_byte_identical_without_a_line_style(tmp_path):
    """회귀 0 — style 이 없으면 종전과 한 글자도 같아야 한다(전 채널 TTS 경로)."""
    from app.modules.subtitle import SubtitleStyle, build_tts_ass

    out = tmp_path / "t.ass"
    build_tts_ass([_cue(1.0, 2.0)], out, SubtitleStyle(margin_v=580))
    line = next(ln for ln in out.read_text(encoding="utf-8-sig").splitlines()
                if ln.startswith("Dialogue:"))
    assert line.startswith("Dialogue: 0,0:00:01.00,0:00:02.00,Default,,,,,, ")


def test_tts_ass_carries_the_per_line_margin(tmp_path):
    from app.modules.subtitle import SubtitleStyle, build_tts_ass

    out = tmp_path / "t.ass"
    build_tts_ass([_cue(1.0, 2.0, style={"y": 0.7})], out, SubtitleStyle(margin_v=580))
    line = next(ln for ln in out.read_text(encoding="utf-8-sig").splitlines()
                if ln.startswith("Dialogue:"))
    assert ",,,,576,," in line                      # (1-0.7)*1920 = 576


def test_tts_rotation_origin_follows_the_moved_line(tmp_path):
    """기울인 TTS 자막의 회전 원점이 옛 자리에 남으면 글자가 엉뚱하게 돈다."""
    from app.modules.subtitle import SubtitleStyle, build_tts_ass

    out = tmp_path / "t.ass"
    build_tts_ass([_cue(1.0, 2.0, style={"y": 0.7})], out,
                  SubtitleStyle(margin_v=580, font_size=60), rotate_deg=10)
    body = out.read_text(encoding="utf-8-sig")
    assert "\\org(540," in body and ",1340)" not in body


def test_profiles_do_not_leak_between_episodes(tmp_path, capsys):
    """모듈 수준 저장소라 한 프로세스에서 여러 편을 돌리면 앞 편 표본이 남는다 —
    앞 편의 원본 자막 자리로 이 편 자막을 올리면 조용히 틀린다."""
    pipeline._BURNED_PROFILES[:] = _profiles([(1.0, True), (1.5, True)])
    payload = SimpleNamespace(design=DesignConfig(subtitle_avoid_burned="off"),
                              video_path=Path("x.mp4"))
    assert pipeline._detect_burned_band_cached(payload, _Cfg(), [], {}, tmp_path) is None
    assert pipeline._BURNED_PROFILES == []


# ══════════════════════════════════════════════════════════════════════════
# 감사 기록 — 돌았는지 나중에 확인할 수 있어야 한다 (2026-08-24 후속)
# ══════════════════════════════════════════════════════════════════════════
# 검증 런에서 실제로 막혔다: `subtitle_avoid_burned` 단계가 없을 때 '띠가 없었다'인지
# '구간 판정 자체가 안 돌았다'인지 구분할 방법이 없었고, 효과 텍스트 클램프는 stdout
# 에만 남아 DB 로는 확인이 불가능했다. 그래서 셋을 run_log 에 남긴다.
def test_windowed_moves_are_recorded_for_the_run_log():
    prof = _profiles([(t / 2, True) for t in range(0, 20)])
    pipeline._BURNED_WINDOW_MOVES[:] = []
    try:
        _run_windowed([_cue(1.0, 5.0), _cue(6.0, 8.0)], prof)
        assert len(pipeline._BURNED_WINDOW_MOVES) == 1
        rec = pipeline._BURNED_WINDOW_MOVES[0]
        assert rec["moved"] == 2 and rec["lines"] == 2 and rec["base_margin_v"] == 430
    finally:
        pipeline._BURNED_WINDOW_MOVES[:] = []


def test_nothing_moved_records_nothing():
    """회귀 0 — 보정이 없던 실행의 run_log 는 종전과 같아야 한다."""
    pipeline._BURNED_WINDOW_MOVES[:] = []
    try:
        _run_windowed([_cue(1.0, 5.0)], [])
        assert pipeline._BURNED_WINDOW_MOVES == []
    finally:
        pipeline._BURNED_WINDOW_MOVES[:] = []


def test_the_move_log_is_cleared_per_episode(tmp_path):
    """앞 편 기록이 남으면 이 편 run_log 가 거짓말을 한다(표본과 같은 규율)."""
    pipeline._BURNED_WINDOW_MOVES[:] = [{"track": "이전 편", "moved": 9}]
    payload = SimpleNamespace(design=DesignConfig(subtitle_avoid_burned="off"),
                              video_path=Path("x.mp4"))
    pipeline._detect_burned_band_cached(payload, _Cfg(), [], {}, tmp_path)
    assert pipeline._BURNED_WINDOW_MOVES == []


def test_pipeline_records_that_the_judgement_ran_even_with_no_band():
    """띠를 못 찾아도 판정이 돌았다는 사실과 표본 수는 남긴다."""
    src = (Path(__file__).resolve().parents[1] / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert 'if payload.show_subtitles and (_burned_band or _BURNED_PROFILES):' in src
    assert '"profile_frames": len(_BURNED_PROFILES),' in src
    # 구간 결과는 ASS 조립 뒤에 채워진다 — 같은 dict 를 들고 있다가 마지막에 붙인다.
    assert '_avoid_log["windowed"] = list(_BURNED_WINDOW_MOVES)' in src


def test_pipeline_records_the_text_clamp_in_the_style_step():
    src = (Path(__file__).resolve().parents[1] / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert '"text_clamp": _text_clamp,' in src
    assert '_text_clamp = {"clamped": len(_clamp_notes), "of": len(_clamped),' in src
