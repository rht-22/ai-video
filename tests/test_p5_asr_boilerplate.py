"""받아쓰기 상용구 환각 — 노래를 더빙해 버린 사고 (2026-08-26).

실사고 `-wgxMaRHaYQ`(잔망루피, 노래뿐인 14초 편):

    ko_ja_pairs   ko="다음 영상에서 만나요!" → ja="次の動画で会おうね！"
    alignment     segment_count 1 · total_speech_sec 14.0 · 0.0~14.0
    stems         htdemucs (보컬 분리)
    backcheck     CER 0.11 · failed 0  ← **통과했다**

영상에 그 말은 없다. 받아쓰기가 음악 위에서 지어냈고, 그 뒤 전부가 그것을 사실로 믿었다:
demucs 가 보컬(=노래 가사)을 지우고 그 자리에 없는 말을 더빙·자막으로 박았다.
백체크가 초록불인 이유는 TTS 가 시킨 대로 말했기 때문이다 — 환각을 검증하지 않는다.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.localize.overlay.dub import (hallucinated_boilerplate,  # noqa: E402
                                      is_boilerplate, reliable_segment)


def test_the_phrase_that_shipped_is_recognised():
    assert is_boilerplate("다음 영상에서 만나요!")
    assert is_boilerplate("次の動画で会おうね！")
    assert is_boilerplate("Thanks for watching!")
    assert is_boilerplate("구독과 좋아요 부탁드려요")


def test_real_dialogue_is_not_boilerplate():
    for t in ["루피야 뭐해?", "이거 진짜 맛있뤂", "청하 or 루피 골라봐"]:
        assert not is_boilerplate(t), t


def test_the_real_incident_is_caught():
    """14초 영상의 0~14초를 덮는 한 문장 — 발화가 아니라 환각이다."""
    assert hallucinated_boilerplate("다음 영상에서 만나요!", 14.0, 14.0)


def test_a_genuine_outro_line_survives():
    """🛑 문구만으로 버리지 않는다 — 진짜로 그 말을 하는 편이 있다.

    그때 세그먼트는 발화 길이(1~2초)다."""
    assert not hallucinated_boilerplate("다음 영상에서 만나요!", 1.6, 30.0)


def test_long_boilerplate_is_caught_even_in_a_long_video():
    """5초짜리 '다음 영상에서 만나요' 는 사람이 그렇게 말하지 않는다."""
    assert hallucinated_boilerplate("다음 영상에서 만나요!", 6.0, 120.0)


def test_coverage_rule_needs_the_phrase_too():
    """대사가 영상을 다 덮는다고 버리면 안 된다 — 한 문장짜리 편이 있다."""
    assert not hallucinated_boilerplate("루피야 뭐해?", 14.0, 14.0)


def test_the_old_confidence_guard_missed_this_one():
    """왜 새 축이 필요했는지 — 음악 위 환각은 모델이 자신 있게 내놓는다."""
    assert reliable_segment(no_speech_prob=0.2, avg_logprob=-0.4)   # 통과시킨다


def test_transcribe_drops_it_and_says_why():
    src = pathlib.Path("app/localize/overlay/dub.py").read_text(encoding="utf-8")
    body = src.split("def transcribe(", 1)[1].split("\ndef ", 1)[0]
    assert "hallucinated_boilerplate(" in body
    assert "상용구 환각 제외" in body
    # 대사가 하나도 안 남으면 그 사실을 크게 남긴다 — 뒤에서 더빙이 실패하고,
    # 보컬 제거도 일어나지 않는다(원곡이 남는다).
    assert "보컬 제거도 하지 않으므로" in body


def test_no_silent_dubbing_of_songs():
    """🛑 이 가드가 사라지면 노래만 있는 편이 다시 통째로 더빙된다."""
    src = pathlib.Path("app/localize/overlay/dub.py").read_text(encoding="utf-8")
    assert "if not out:" in src.split("def transcribe(", 1)[1]
