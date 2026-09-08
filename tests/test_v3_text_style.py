"""v3 자막 텍스트 스타일(2026-09-08, 참고 쇼츠 v10 대조) + 자막 편집본 오프셋 회귀.

사용자 지시: Noto Sans CJK 폰트 · 외곽선 두껍게 · 팝은 v10 타이밍 · 내레이션도 팝 + 균형
2줄 + 대사와 같은 y · 내레이션 색(노랑)은 대사·강조에 안 쓴다. 그리고 "최근 쇼츠는 대사
자막 타이밍이 다 어긋나 있다" — 원인은 word_subtitles 의 편집본 오프셋 누적이 hold_sec ·
프레임 격자를 빼먹은 것(붙잡은 덮개 뒤 전 자막이 1.8초 일찍).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import FONT_NAME_MAP, get_font_path, to_font_family
from app.modules.subtitle import SubtitleStyle, _LINE_FX, build_tts_ass
from app.v3 import assemble, finalize, stage4

ROOT = Path(__file__).resolve().parent.parent / "app"


# ── 자막 오프셋: hold_sec · 프레임 격자 ──────────────────────────────────────

def _span(t_in, t_out, heard):
    return {"t_in": t_in, "t_out": t_out, "pos": 0, "is_audio": True,
            "text_source": "heard", "heard_text": heard,
            "audio_script": [{"speaker": "갑", "line": heard}]}


def test_word_subtitles_accumulate_hold_and_frame_grid():
    """붙잡은 덮개(hold_sec) 뒤의 자막은 그만큼 늦게 시작해야 한다 — ep01full 실측 1.835s."""
    span = {"c": _span(2380.73, 2381.37, "뭐"), "d": _span(2366.0, 2368.0, "너 바람피니")}
    tl = [{"clip_start_sec": 2380.73, "clip_end_sec": 2381.37, "use_original_audio": False,
           "hold_sec": 1.835, "cover": "hold", "span_ids": ["c"]},
          {"clip_start_sec": 2366.0, "clip_end_sec": 2368.0, "use_original_audio": True,
           "span_ids": ["d"]}]
    fps = 24000 / 1001
    segs = assemble.word_subtitles(tl, span, [], fps=fps)
    assert segs and segs[0]["text"] == "너 바람피니"
    expect = assemble.clip_duration(assemble.clip_len(tl[0]), fps)      # 2.4608 — 렌더 격자
    assert segs[0]["start_sec"] == pytest.approx(expect, abs=1e-3)
    # fps 없이도 hold_sec 은 더한다(종전 0.64 가 아니다)
    segs2 = assemble.word_subtitles(tl, span, [])
    assert segs2[0]["start_sec"] == pytest.approx(0.64 + 1.835, abs=1e-3)
    # cue 좌표(finalize_cues)와 **같은 자** — 두 소비자가 갈리면 자막과 내레이션이 어긋난다
    offs = assemble.edited_offsets(tl, fps)
    assert offs[1][2] == pytest.approx(expect, abs=1e-9)


# ── 폰트 ──────────────────────────────────────────────────────────────────

def test_noto_cjk_font_is_bundled_and_resolves():
    p = get_font_path(finalize.V3_TEXT_FONT, ROOT)
    assert p.endswith("NotoSansCJKkr-Black.otf") and Path(p).exists()
    assert get_font_path("Noto Sans CJK KR", ROOT) == p                # 한글 이름 → 같은 파일
    assert to_font_family(finalize.V3_TEXT_FONT) == "Noto Sans CJK KR Black"  # ASS Fontname = nameID 1
    assert FONT_NAME_MAP["Noto Sans CJK KR"] == finalize.V3_TEXT_FONT
    # PIL 로 실제 한글 글리프가 있는지(NotoSansJP 번들은 한글이 .notdef 였다)
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np
    f = ImageFont.truetype(p, 40)
    def _r(ch):
        im = Image.new("L", (80, 80), 0); ImageDraw.Draw(im).text((5, 5), ch, font=f, fill=255)
        return np.array(im)
    assert not np.array_equal(_r("가"), _r("꟱"))


def test_v3_default_subtitle_font_is_noto_unless_channel_says():
    d = finalize.design_from_style({"subtitle_size": 60})
    assert d.subtitle_font == finalize.V3_TEXT_FONT
    d2 = finalize.design_from_style({"subtitle_font": "JalnanGothic"})
    assert d2.subtitle_font == "JalnanGothic"                           # 채널 명시가 이긴다
    from app.modules.edit_overrides import TEXT_FONTS
    assert finalize.V3_TEXT_FONT in TEXT_FONTS                          # 라벨(texts) 화이트리스트


# ── 외곽선 · 팝 ─────────────────────────────────────────────────────────────

def test_outline_and_pop_constants_match_v10():
    assert finalize.SUB_OUTLINE_PX == 8
    assert finalize.POP_TO_FX == {"soft": "pop_snap", "strong": "pop_snap_strong"}
    assert finalize.EMPHASIS_FX == "pop_snap_strong" and finalize.NARRATION_FX == "pop_snap"
    # v10 POP 1.30→0.92→1.06→1.00 (3프레임 ≈ 100ms) — 크게 튀어나왔다가 제자리
    assert _LINE_FX["pop_snap"].startswith("\\fscx130\\fscy130") and "\\t(66,100," in _LINE_FX["pop_snap"]
    assert _LINE_FX["pop_snap_strong"].startswith("\\fscx144\\fscy144")
    # 종전 v1 fx 는 그대로(회귀 0)
    assert _LINE_FX["pop_soft"].startswith("\\fscx90") and _LINE_FX["pop_strong"].startswith("\\fscx62")
    em = finalize.emphasis_styles({"emphasis": [{"index": 1, "scale": 1.25, "color": "#FF5540"}]}, 60)
    assert em[1]["fx"] == "pop_snap_strong" and em[1]["size"] == 75


def test_tts_ass_gets_pop_tag_only_when_style_says(tmp_path):
    st = SubtitleStyle(margin_v=580)
    a = tmp_path / "a.ass"; b = tmp_path / "b.ass"
    build_tts_ass([SimpleNamespace(start_sec=1.0, end_sec=2.0, text="남편의 불륜 찌라시", style=None)], a, st)
    build_tts_ass([SimpleNamespace(start_sec=1.0, end_sec=2.0, text="남편의 불륜 찌라시",
                                   style={"fx": "pop_snap"})], b, st)
    ta, tb = a.read_text("utf-8-sig"), b.read_text("utf-8-sig")
    assert "fscx" not in ta                                             # v1 경로 바이트 동일
    assert "{" + _LINE_FX["pop_snap"] + "}" in tb
    # 회전과 함께면 태그 하나로 합쳐진다
    c = tmp_path / "c.ass"
    build_tts_ass([SimpleNamespace(start_sec=1.0, end_sec=2.0, text="x", style={"fx": "pop_snap"})],
                  c, st, rotate_deg=3.0)
    line = [l for l in c.read_text("utf-8-sig").splitlines() if l.startswith("Dialogue")][0]
    assert line.count("{") == 1 and "\\frz-3" in line and "fscx130" in line


# ── 내레이션 2줄 균형 분할 ───────────────────────────────────────────────────

def test_balance_narration_lines():
    f = finalize.balance_narration_lines
    assert f("남편 지갑에 위치추적기를 심는데", max_chars=15) == "남편 지갑에\n위치추적기를 심는데"
    assert f("추적기 앱을 보며 뒤를 쫓는데", max_chars=15) == "추적기 앱을 보며\n뒤를 쫓는데"   # 종전 '뒤를 / 쫓는데' 꽁다리
    assert f("불륜 현장이 아닌 살인 현장이었죠", max_chars=15) == "불륜 현장이 아닌\n살인 현장이었죠"
    assert f("한 줄이면 그대로", max_chars=15) == "한 줄이면 그대로"
    assert f("가나다라마바사아자차카타파하가나다라마", max_chars=15) == "가나다라마바사아자차카타파하가나다라마"  # 못 나누면 손대지 않음
    # 줄 첫 어절이 의존명사·조사류면 벌점 — '것을' 로 줄을 시작하지 않는다
    assert f("아내가 내민 것을 남편이 받았다", max_chars=15) == "아내가 내민 것을\n남편이 받았다"


# ── 색 분리 ────────────────────────────────────────────────────────────────

def test_narration_yellow_is_reserved():
    assert "#FFE94A" not in assemble.SPEAKER_PALETTE
    assert "yellow" not in stage4.EMPH_PALETTE and "red" in stage4.EMPH_PALETTE
    notes: list[str] = []
    ev = [{"id": "L1", "kind": "line", "start": 1.0, "end": 2.0, "text": "너 바람피니?"}]
    out = stage4.validate_edit_fx({"emphasis": [{"line": "L1", "color": "yellow"}]},
                                  events=ev, clips=[], notes=notes)
    assert out["emphasis"][0]["color"] == stage4.EMPH_DEFAULT_COLOR
    assert any("내레이션 색" in n for n in notes)
    # 라벨 팔레트는 그대로(v10 도 라벨은 노랑)
    assert "yellow" in stage4.LABEL_PALETTE


def test_label_font_is_a_name_not_a_path():
    """render_final 은 design.subtitle_font 를 경로로 바꾼 뒤 라벨을 짓는다 — 라벨 dict 의 font 는
    경로가 아니라 **이름**이어야 한다(libass 는 경로 Fontname 을 못 찾고 기본 폰트로 조용히 대체 —
    2026-09-08 첫 재렌더 실측 `\\fn/Users/.../NotoSansCJKkr-Black.otf`)."""
    src = Path(__file__).resolve().parent.parent.joinpath("app/v3/finalize.py").read_text("utf-8")
    assert src.count('"font": _text_font_name') == 2
    assert '"font": design.subtitle_font' not in src
    assert src.index("_text_font_name = design.subtitle_font") < src.index("subtitle_font=get_font_path(")


# ── v3 화자 추적 노브(2026-09-08, 「너 바람피니?」 경희 얼굴 실사고) ─────────────────────────

def test_pick_speaker_area_relative_prefers_the_big_face():
    """프레임 대비 면적(종전)은 275px 얼굴도 0.036 이라 중앙 항이 이겨 작은 남편 얼굴을 골랐다.
    최대 얼굴 대비(area_relative)면 큰 얼굴이 면적 항 1.0 을 받는다. 종전 경로는 그대로."""
    pytest.importorskip("cv2")
    import numpy as np
    from app.modules.reframe import _pick_speaker
    gray = np.zeros((1080, 1920), dtype=np.uint8)
    faces = [(560, 170, 75, 105), (1425, 360, 275, 273)]      # EP01 2367.6s YuNet 실측 박스
    old = _pick_speaker(faces, gray, None, frame_w=1920, frame_h=1080, prev_x=960, prev_y=540)
    new = _pick_speaker(faces, gray, None, frame_w=1920, frame_h=1080, prev_x=960, prev_y=540,
                        area_relative=True)
    assert old == faces[0]                     # 종전: 중앙에 가까운 작은 얼굴
    assert new == faces[1]                     # v3: 그 프레임에서 가장 큰 얼굴


def test_speaker_crop_map_passes_v3_knobs_and_no_carry_over(tmp_path):
    from app.v3 import finalize
    tl = [{"role": "hook", "clip_start_sec": 10.0, "clip_end_sec": 12.0, "span_ids": []},
          {"role": "build", "clip_start_sec": 20.0, "clip_end_sec": 21.0, "span_ids": []}]
    calls = []
    def build(video, out, w, h, step, **kw):
        calls.append(kw)
        return [{"time_sec": kw["start_sec"], "x_center": 1700.0, "y_center": 540.0, "crop_w": 0, "crop_h": 0, "face_w": 100}]
    finalize.speaker_crop_map(tl, video_path=Path("v.mp4"), aspect_ratio="24:23", output_dir=tmp_path,
                              src_size=(1920, 1080), detector="yunet", build=build, log=lambda *a: None)
    assert all(c["initial_x"] is None and c["snap_first"] and c["area_relative"] for c in calls)
    assert all(c["ema_alpha"] == finalize.SPEAKER_EMA_ALPHA for c in calls)
    assert 0.12 < finalize.SPEAKER_EMA_ALPHA <= 1.0


# ── 계단식 화자 고정(hold, 2026-09-08 사용자 지시 "카메라가 왔다갔다 하지 않았으면") ─────────

def _row(t, x, fw=200):
    return {"time_sec": t, "x_center": x, "y_center": 540.0, "crop_w": 1000, "crop_h": 960,
            "face_cx": x, "face_cy": 500.0, "face_w": fw, "face_h": fw}


def test_hold_keyframes_step_per_utterance():
    from app.v3.finalize import hold_keyframes
    # 경희(x≈1530) 0~2s · 남편 "어?" 2.0~2.4s(짧음) · 남편 2.5~5s(x≈580)
    rows = [_row(0.0, 1530), _row(0.5, 1535), _row(1.0, 1528), _row(1.5, 1531),
            _row(2.0, 600, 80), _row(2.5, 585, 80), _row(3.0, 578, 80), _row(3.5, 582, 80),
            _row(4.0, 590, 80), _row(4.5, 575, 80)]
    utt = [(0.2, 1.9, "경희"), (2.0, 2.4, "남편"), (2.5, 5.0, "남편")]
    kfs, runs = hold_keyframes(rows, utt, clip_start=0.0, clip_end=5.2, fps=24.0, pic_w=1920)
    xs = [k["x_center"] for k in kfs]; ts = [k["time_sec"] for k in kfs]
    # 첫 프레임부터 경희 · 2.0 직전까지 유지 · 2.0 에 남편으로 한 프레임 점프 · 끝까지 유지
    assert ts[0] == 0.0 and xs[0] == 1531.0
    assert ts[1] == pytest.approx(2.0 - 1 / 24, abs=1e-3) and xs[1] == 1531.0
    assert ts[2] == 2.0 and xs[2] == pytest.approx(583.5)
    assert ts[-1] == 5.2 and xs[-1] == pytest.approx(583.5)
    assert len(kfs) == 4                                   # 팬 없음 — 표본 10개가 키프레임 4개로
    # 짧은 "어?" 는 뒤 run(같은 화자)과 병합돼 run 은 둘
    assert [r["speaker"] for r in runs] == ["경희", "남편"] and runs[1]["start"] == 2.0


def test_hold_keyframes_no_rehold_for_small_move_and_no_dialogue():
    from app.v3.finalize import hold_keyframes
    rows = [_row(0.0, 900), _row(0.5, 910), _row(1.0, 1000), _row(1.5, 1010)]
    # 같은 위치의 다른 화자(15% 미만 이동) → 재고정 없음 → 키프레임 시작/끝 둘뿐
    kfs, runs = hold_keyframes(rows, [(0.0, 1.0, "A"), (1.0, 2.0, "B")], clip_start=0.0, clip_end=2.0,
                               fps=24.0, pic_w=1920)
    assert [k["x_center"] for k in kfs] == [910.0, 910.0] and runs[1]["x"] == 910.0
    # 발화 없음 → 얼굴 중앙값 하나로 고정(팬 없음)
    kfs2, runs2 = hold_keyframes(rows, [], clip_start=0.0, clip_end=2.0, fps=24.0, pic_w=1920)
    assert len(kfs2) == 1 and kfs2[0]["x_center"] == 955.0 and runs2[0]["held"]
    # 얼굴 0 → x_center 중앙값
    kfs3, _ = hold_keyframes([{**_row(0.0, 700, 0)}, {**_row(0.5, 720, 0)}], [(0, 1, "A")],
                             clip_start=0.0, clip_end=1.0, fps=24.0, pic_w=1920)
    assert kfs3[0]["x_center"] == 710.0


def test_utterances_from_segments_maps_edited_to_source():
    from app.v3.finalize import utterances_from_segments
    tl = [{"clip_start_sec": 100.0, "clip_end_sec": 102.0, "use_original_audio": False, "hold_sec": 1.0},
          {"clip_start_sec": 200.0, "clip_end_sec": 205.0, "use_original_audio": True}]
    segs = [{"start_sec": 3.5, "end_sec": 4.5, "text": "x", "speaker": "경희"},
            {"start_sec": 7.0, "end_sec": 9.0, "text": "y", "speaker": "남편"}]     # 끝이 클립 밖 → 클립 끝으로
    out = utterances_from_segments(segs, tl, None)
    assert out == [(200.5, 201.5, "경희"), (204.0, 205.0, "남편")]


def test_rank_speaker_x_prefers_talking_face_over_big_face():
    from app.v3.finalize import rank_speaker_x
    # 경희(큰 얼굴, 입 안 움직임) vs 남편(작은 얼굴, 입 움직임) — 남편 대사 run
    samples = [{"t": t, "faces": [(1530.0, 500.0, 270, 260, 0.02), (580.0, 220.0, 78, 105, 0.5)]}
               for t in (0.0, 0.5, 1.0, 1.5)]
    x, how = rank_speaker_x(samples, 0.0, 1.5, 1920)
    assert x == 580.0 and how["method"] == "talk×√w" and how["people"] == 2
    # 움직임이 전부 0 이면 큰 얼굴
    still = [{"t": 0.0, "faces": [(1530.0, 500.0, 270, 260, 0.0), (580.0, 220.0, 78, 105, 0.0)]}]
    assert rank_speaker_x(still, 0.0, 1.0, 1920) == (1530.0, {"people": 2, "talk": 0.0, "w": 270, "method": "largest"})
    assert rank_speaker_x([], 0.0, 1.0, 1920) == (None, {"people": 0})


def test_rank_speaker_x_uses_speaker_memory():
    from app.v3.finalize import hold_keyframes, rank_speaker_x
    samples = [{"t": t, "faces": [(1620.0, 500.0, 272, 260, 0.83), (600.0, 220.0, 78, 105, 0.6)]}
               for t in (0.0, 0.5, 1.0)]
    # 기억 없음 → 리액션이 큰 경희(1620) · 기억(남편 609) → 600 쪽 얼굴
    assert rank_speaker_x(samples, 0.0, 1.0, 1920)[0] == 1620.0
    x, how = rank_speaker_x(samples, 0.0, 1.0, 1920, prefer_x=609.0)
    assert x == 600.0 and how["method"] == "memory"
    # hold_keyframes 가 run 마다 기억을 갱신·소비한다(60s 안)
    mem = {"임재홍": (609.0, 2375.7)}
    rows = [_row(2375.8, 1620), _row(2376.3, 1622), _row(2376.8, 1618)]
    smp = [{"t": t, "faces": [(1620.0, 500.0, 272, 260, 0.83), (600.0, 220.0, 78, 105, 0.6)]} for t in (2375.8, 2376.3, 2376.8)]
    kfs, runs = hold_keyframes(rows, [(2375.8, 2377.08, "임재홍")], clip_start=2375.8, clip_end=2377.08,
                               fps=24.0, pic_w=1920, samples=smp, speaker_memory=mem)
    assert runs[0]["x"] == 600.0 and mem["임재홍"] == (600.0, 2377.08)
    # 60s 넘게 지난 기억은 안 쓴다
    mem2 = {"임재홍": (609.0, 2000.0)}
    _, runs2 = hold_keyframes(rows, [(2375.8, 2377.08, "임재홍")], clip_start=2375.8, clip_end=2377.08,
                              fps=24.0, pic_w=1920, samples=smp, speaker_memory=mem2)
    assert runs2[0]["x"] == 1620.0
