"""L3t 내레이션 창 초과 — 편 전체를 죽이던 오디오 꼬리 (2026-08-24).

SHOTCONE 혜미리예채파 2화가 **3회 연속 같은 숫자로** dead 였다:
`컷 길이 불일치: ko 39.400s vs ja 39.900s`.

기전(실측으로 확인):
  · 렌더는 `amix=inputs=N:duration=longest` 로 섞고 ffmpeg 에 `-shortest` 가 **없다**.
  · 그래서 마지막 cue 오디오가 창을 넘으면 **컨테이너 길이가 영상 트랙보다 길어진다**
    (합성 2.0s 영상 + 2.5s cue → 출력 2.5s).
  · L3t 는 rate 3단계(+0/+15/+30%)를 다 쓰고도 안 맞으면 **경고만 찍고 넘어갔다** —
    한국어 경로는 `synthesize_cue_cached` 가 fit 재작성으로 창을 지키는데 일본어
    재합성만 이 비대칭이 있었다.
  · 그 0.5초를 L4 의 컷 대조(허용 0.05초)가 잡아 편 전체를 실패시켰다.

이 파일이 지키는 것:
  ① 창을 넘긴 cue 는 창 길이로 **잘린다**(넘어가지 않는다) — 실제 ffmpeg 으로 확인
  ② 창 안에 드는 cue 는 **손대지 않는다**(회귀 0)
  ③ L4 실패 메시지가 '오디오 꼬리'와 '컷 재현 실패'를 구분한다(조치가 다르다)
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.localize import narration  # noqa: E402
from app.localize.rerender import cut_reproduced  # noqa: E402

_FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg 없음")


# ── 순수: 잘라내기 argv ────────────────────────────────────────────────────
def test_trim_args_caps_duration_and_fades():
    argv = narration.trim_args(Path("a.mp3"), Path("b.mp3"), 3.0, "ffmpeg")
    assert argv[argv.index("-t") + 1] == "3.000"
    af = argv[argv.index("-af") + 1]
    assert af.startswith("afade=t=out:")
    assert "d=0.150" in af                       # TRIM_FADE_SEC
    assert argv[argv.index("-i") + 1] == "a.mp3" and argv[-1] == "b.mp3"


def test_trim_args_fade_never_eats_a_short_cue():
    """창의 10% 상한 — 0.4초 cue 에 0.15초 페이드를 걸면 소리가 거의 사라진다."""
    argv = narration.trim_args(Path("a.mp3"), Path("b.mp3"), 0.4, "ffmpeg")
    af = argv[argv.index("-af") + 1]
    assert "d=0.040" in af                       # 0.4 * 0.1
    assert "st=0.360" in af


def test_trim_args_zero_window_has_no_fade_filter():
    argv = narration.trim_args(Path("a.mp3"), Path("b.mp3"), 0.0, "ffmpeg")
    assert argv[argv.index("-af") + 1] == "anull"


# ── 실제 ffmpeg: 창을 넘긴 오디오가 정말 잘리는가 ──────────────────────────
def _sine(path: Path, seconds: float):
    subprocess.run([_FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=f=440:d={seconds}", "-c:a", "libmp3lame", str(path)],
                   check=True)


@needs_ffmpeg
def test_overrunning_cue_is_trimmed_into_its_window(tmp_path):
    """이것이 이 버그의 핵심 — 자르지 않으면 출력 mp4 가 영상보다 길어진다."""
    mp3 = tmp_path / "cue.mp3"
    _sine(mp3, 2.5)
    assert narration._audio_dur(mp3) > 2.4
    dur = narration._trim_to_window(mp3, 2.0)
    assert dur <= 2.0 + 0.05, f"창 밖으로 남았다: {dur}"
    assert not (tmp_path / "cue.trim.mp3").exists(), "임시 파일이 남았다"


@needs_ffmpeg
def test_trim_is_not_called_for_a_cue_that_already_fits(tmp_path, monkeypatch):
    """회귀 0 — 창에 드는 cue 는 파일을 아예 안 건드린다(전 채널 내레이션이 안 흔들린다)."""
    mp3 = tmp_path / "cue.mp3"
    _sine(mp3, 1.0)
    before = mp3.read_bytes()
    called = []
    monkeypatch.setattr(narration, "_trim_to_window",
                        lambda *a: called.append(a) or 0.0)
    assert narration.fits_window(narration._audio_dur(mp3), 2.0) is True
    assert called == []
    assert mp3.read_bytes() == before


@needs_ffmpeg
def test_zero_window_is_left_alone(tmp_path):
    """창이 0 이면 자를 기준이 없다 — 원본을 유지하고 죽지 않는다."""
    mp3 = tmp_path / "cue.mp3"
    _sine(mp3, 1.0)
    before = mp3.read_bytes()
    narration._trim_to_window(mp3, 0.0)
    assert mp3.read_bytes() == before


def test_trim_failure_keeps_the_original_and_does_not_raise(tmp_path, capsys):
    """잘라내기가 실패해도 내레이션을 잃지 않는다 — 원본 유지 + 로그."""
    mp3 = tmp_path / "cue.mp3"
    mp3.write_bytes(b"not really an mp3")
    narration._trim_to_window(mp3, 1.0)           # ffmpeg 이 실패한다
    assert mp3.read_bytes() == b"not really an mp3"
    assert "원본 유지" in capsys.readouterr().out
    assert not (tmp_path / "cue.trim.mp3").exists()


# ── L4 컷 검증: 비디오 스트림끼리 비교한다 (2026-08-24 사용자 결정) ────────
def test_the_real_incident_now_passes():
    """실제로 난 사고의 숫자 — 스트림은 같고 컨테이너만 0.5초 다르다.

    컷은 완벽히 재현됐으므로 **통과**해야 한다. KO 완성본에도 이미 14.4초 오디오
    꼬리가 있었다(KR 은 이 검사가 없어 아무도 몰랐다) — 0.5초를 이유로 발행을 막는
    것은 앞뒤가 맞지 않는다."""
    ok, detail = cut_reproduced(25.025, 25.025, 39.400, 39.900)
    assert ok
    assert "컷 재현 확인" in detail


def test_an_audio_tail_is_reported_even_though_it_passes():
    """통과시키되 조용히 넘기지 않는다 — 안 보이면 '고쳤는데 왜 그대로지'가 된다."""
    _, detail = cut_reproduced(25.025, 25.025, 39.400, 39.900)
    assert "오디오 꼬리" in detail and "39.400" in detail and "39.900" in detail


def test_a_clean_run_says_nothing_about_audio():
    """컨테이너까지 같으면 꼬리 문구가 안 붙는다(경고가 늘 붙으면 무뎌진다)."""
    ok, detail = cut_reproduced(25.025, 25.025, 25.100, 25.100)
    assert ok and "오디오 꼬리" not in detail


def test_a_real_cut_mismatch_still_fails():
    """가드가 사라지면 안 된다 — 스트림이 다르면 자막 싱크가 깨진 것이다."""
    ok, detail = cut_reproduced(49.700, 53.300, 49.700, 53.300)
    assert not ok
    assert "gen_flags 재현 실패" in detail


def test_container_is_never_compared_against_a_stream():
    """⚠ 회귀 가드 — ko **컨테이너**(39.400)와 ja **스트림**(25.025)을 맞대던 단위 착오.
    그 착오가 있으면 이 입력이 실패로 읽힌다."""
    ok, _ = cut_reproduced(25.025, 25.025, 39.400, 39.900)
    assert ok, "컨테이너와 스트림을 맞대고 있다"


def test_both_numbers_are_always_printed_so_a_human_can_recheck():
    for _, detail in (cut_reproduced(25.025, 25.025, 39.400, 39.900),
                      cut_reproduced(49.700, 53.300, 49.700, 53.300)):
        assert "ko" in detail and "ja" in detail


def test_unreadable_stream_falls_back_to_the_container_check():
    """가드가 조용히 사라지는 것이 오판보다 나쁘다 — 폴백하고 그 사실을 말한다."""
    ok, detail = cut_reproduced(0.0, 25.025, 39.400, 39.900)
    assert not ok
    assert "폴백" in detail and "못 읽어" in detail

    ok2, detail2 = cut_reproduced(0.0, 0.0, 39.400, 39.410)
    assert ok2 and "폴백" in detail2
