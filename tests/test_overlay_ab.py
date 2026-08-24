"""L-P4 — overlay A/B 대조 도구의 순수 로직 고정.

이 도구의 판정이 컷오버 근거가 된다. 틀린 숫자는 없느니만 못하다 — P1 에서 **아무것도
안 돌았는데 '회귀 0' 이 두 번 찍혔다**. 그래서 거짓 합격 가드를 먼저 고정한다.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.overlay_ab import (  # noqa: E402
    ALIGN_TOL_SEC, align_diff, compare, events_of, load_entries, source_diff,
    target_cer, verdict,
)


def _tr(*rows):
    return {"video_id": "v", "entries": [{"source": s, "target": t, "use": u}
                                         for s, t, u in rows]}


def _ev(*rows):
    return {"video_id": "v", "events": [{"start": s, "end": e, "text": t}
                                        for s, e, t in rows]}


# ── 원문(한국어)이 흔들리면 회귀다 ──────────────────────────────────────
def test_source_change_is_a_regression():
    """OCR·탐지가 흔들렸다는 뜻이다 — 번역보다 상류라 더 무겁다."""
    a = load_entries(_tr(("안녕", "こんにちは", True)))
    b = load_entries(_tr(("안녕히", "こんにちは", True)))
    out = source_diff(a, b)
    assert out and "원문 변경" in out[0]


def test_same_source_is_silent():
    a = load_entries(_tr(("안녕", "こんにちは", True), ("잘가", "またね", True)))
    assert source_diff(a, list(a)) == []


def test_entry_count_change_is_flagged():
    a = load_entries(_tr(("안녕", "x", True)))
    b = load_entries(_tr(("안녕", "x", True), ("추가", "y", True)))
    assert any("항목 수" in s for s in source_diff(a, b))


def test_soft_delete_flip_is_a_regression():
    """use=false 는 그 줄을 렌더에서 뺀다 — 조용히 바뀌면 자막 하나가 사라진다."""
    a = load_entries(_tr(("안녕", "x", True)))
    b = load_entries(_tr(("안녕", "x", False)))
    assert any("use 변경" in s for s in source_diff(a, b))


def test_load_entries_tolerates_junk():
    assert load_entries(None) == [] and load_entries("x") == []
    assert load_entries({"entries": [None, 3]}) == []


# ── 번역문은 CER 로 거리만 본다 (판정 제외) ─────────────────────────────
def test_identical_translation_is_zero_cer():
    a = load_entries(_tr(("안녕", "こんにちは", True)))
    got = target_cer(a, list(a))
    assert got["mean"] == 0.0 and got["identical"] == 1


def test_different_translation_reports_distance_not_failure():
    a = load_entries(_tr(("안녕", "こんにちは", True)))
    b = load_entries(_tr(("안녕", "どうも", True)))
    got = target_cer(a, b)
    assert got["max"] > 0
    ok, _ = verdict({"번역문 CER": {"advisory": True, "summary": "x"}})
    assert ok is True                       # 번역 차이만으로는 불합격이 아니다


# ── 세그먼트 정렬 — 어긋나면 자막이 딴 장면에 뜬다 ──────────────────────
def test_alignment_within_tolerance_passes():
    a = events_of(_ev((1.0, 2.0, "가")))
    b = events_of(_ev((1.0 + ALIGN_TOL_SEC / 2, 2.0, "가")))
    lines, worst = align_diff(a, b)
    assert lines == [] and worst <= ALIGN_TOL_SEC


def test_alignment_beyond_tolerance_fails():
    a = events_of(_ev((1.0, 2.0, "가")))
    b = events_of(_ev((1.5, 2.5, "가")))
    lines, worst = align_diff(a, b)
    assert lines and worst == 0.5


def test_alignment_reports_the_worst_offset():
    a = events_of(_ev((1.0, 2.0, "가"), (3.0, 4.0, "나")))
    b = events_of(_ev((1.0, 2.0, "가"), (3.0, 4.9, "나")))
    _, worst = align_diff(a, b)
    assert abs(worst - 0.9) < 1e-6


def test_event_count_change_is_flagged():
    lines, _ = align_diff(events_of(_ev((1.0, 2.0, "가"))), [])
    assert any("이벤트 수" in s for s in lines)


def test_events_of_tolerates_junk():
    assert events_of(None) == [] and events_of("x") == [] and events_of({}) == []


# ── 🛑 거짓 합격 가드 (P1 에서 두 번 당했다) ────────────────────────────
def test_empty_dirs_are_not_a_pass(tmp_path):
    """산출이 하나도 없는데 '차이 없음'을 내면 안 된다."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    ok, lines, _ = compare(a, b)
    assert ok is False
    assert any("산출물이 없다" in s for s in lines)


def test_wrong_dir_kind_is_named_in_the_message(tmp_path):
    """rerender job 디렉토리를 실수로 주는 것이 가장 흔한 실수다 — 메시지가 짚어야 한다."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "shorts.mp4").write_text("x")          # rerender 산출
    ok, lines, _ = compare(a, b)
    assert ok is False and any("rerender" in s for s in lines)


def test_identical_outputs_pass(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "translations.json").write_text(
            json.dumps(_tr(("안녕", "こんにちは", True))), encoding="utf-8")
        (d / "ja_events.json").write_text(
            json.dumps(_ev((1.0, 2.0, "こんにちは"))), encoding="utf-8")
    ok, lines, checks = compare(a, b)
    assert ok is True, lines
    assert checks["원문(OCR·탐지)"]["diff"] is False


def test_source_change_fails_the_whole_comparison(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d, src in ((a, "안녕"), (b, "안녕히")):
        d.mkdir()
        (d / "translations.json").write_text(
            json.dumps(_tr((src, "こんにちは", True))), encoding="utf-8")
        (d / "ja_events.json").write_text(json.dumps(_ev((1.0, 2.0, "x"))), encoding="utf-8")
    ok, _, _ = compare(a, b)
    assert ok is False


def test_missing_artifact_on_one_side_is_flagged(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "translations.json").write_text(json.dumps(_tr(("가", "x", True))), encoding="utf-8")
    (a / "ja.srt").write_text("1\n")
    (b / "translations.json").write_text(json.dumps(_tr(("가", "x", True))), encoding="utf-8")
    ok, _, checks = compare(a, b)
    assert checks["산출 목록"]["diff"] is True and ok is False


def test_cer_uses_the_ported_function():
    """vlp 와 **같은 함수**여야 숫자가 비교 가능하다(베낀 수식은 언젠가 어긋난다)."""
    import scripts.overlay_ab as m
    from app.localize.overlay.common import cer as ported
    assert m.cer is ported
