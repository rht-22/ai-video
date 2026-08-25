"""L-P4 — overlay A/B 대조 도구의 순수 로직 고정.

이 도구의 판정이 컷오버 근거가 된다. 틀린 숫자는 없느니만 못하다 — P1 에서 **아무것도
안 돌았는데 '회귀 0' 이 두 번 찍혔다**. 그래서 거짓 합격 가드를 먼저 고정한다.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.overlay_ab import (  # noqa: E402
    ALIGN_TOL_SEC, align_diff, compare, detection_diff, detection_texts, events_of,
    load_entries, source_diff, target_cer, verdict,
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
    # ⚠ 잰 축을 하나 함께 둔다 — 2026-08-25 부터 **판정 축이 하나도 없으면 실패**다
    #   (route BC 가 그 구멍을 드러냈다). 여기서 재는 것은 'CER 은 판정에 안 든다' 하나.
    ok, _ = verdict({"번역문 CER": {"advisory": True, "summary": "x"},
                     "최종본 길이": {"diff": False, "summary": "같다"}})
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


def test_cer_flags_short_references():
    """⚠ CER 은 편집거리/참조길이다 — 참조가 1자면 1.0 을 쉽게 넘는다('L'→'エル' = 2.0).

    실측(5b2NhVS2h_o)에서 평균 3.35 가 나왔는데 입력 18건이 거의 전부 기호·단일 문자였다.
    숫자만 보여주면 '335% 틀렸다'로 읽힌다 — 짧은 참조 비중을 함께 내야 한다."""
    # ⚠ 참조는 **구 번역문**(target)이지 원문(source)이 아니다 — CER 의 분모가 그것이다.
    a = [("L", "L", True), ("그", "あの…", True)]
    b = [("L", "エル", True), ("그", "その", True)]
    got = target_cer(a, b)
    assert got["short_ref"] == 1                 # 'L'(1자)만 짧다 — 'あの…' 는 3자
    assert got["max"] == 2.0                     # 'L' → 'エル' = 편집거리 2 / 길이 1


def test_long_references_report_no_short_warning():
    a = [("안녕하세요 여러분", "皆さんこんにちは", True)]
    b = [("안녕하세요 여러분", "みなさんこんにちは", True)]
    assert target_cer(a, b)["short_ref"] == 0


# ── 비교 항목 0개는 통과가 아니다 (route BC 가 드러냈다, 2026-08-25) ──────────
def test_verdict_excludes_empty_axes_and_fails_when_nothing_is_measured():
    """route BC 는 번역·자막을 안 만든다 — 종전엔 `원문 0항목 동일`·`정렬 0.0s` 로 읽고
    ✅ 를 냈다. 아무것도 안 재고 합격하는 모양이라 판정에서 빼고, 남은 축이 없으면 실패."""
    ok, lines = verdict({"원문(OCR·탐지)": {"empty": True, "summary": "못 쟀다"},
                         "세그먼트 정렬": {"empty": True, "summary": "비교 항목 0"}})
    assert not ok
    assert any("판정에 들어간 축이 하나도 없다" in ln for ln in lines)


def test_verdict_passes_when_at_least_one_axis_was_measured():
    ok, lines = verdict({"원문(OCR·탐지)": {"empty": True, "summary": "비교 항목 0"},
                         "최종본 길이": {"diff": False, "summary": "11.245s vs 11.245s"}})
    assert ok


def test_verdict_still_fails_on_a_real_diff():
    ok, _ = verdict({"세그먼트 정렬": {"diff": True, "summary": "3건"},
                     "최종본 길이": {"diff": False, "summary": "같다"}})
    assert not ok


# ── 번역이 없는 route 는 탐지 산출로 OCR 축을 잰다 ──────────────────────────
def _det(*rows):
    return {"frames": [{"frame_idx": i, "regions": [{"text": t} for t in texts]}
                       for i, texts in rows]}


def test_detection_texts_flattens_in_frame_order():
    assert detection_texts(_det((0, ["가", "나"]), (15, ["다"]))) == [
        (0, "가"), (0, "나"), (15, "다")]


def test_detection_texts_survives_junk():
    assert detection_texts(None) == []
    assert detection_texts({"frames": None}) == []


def test_detection_diff_is_exact_because_ocr_is_deterministic():
    a = detection_texts(_det((0, ["가"]), (15, ["나"])))
    assert detection_diff(a, list(a)) == []
    b = detection_texts(_det((0, ["가"]), (15, ["다"])))
    assert any("'나' → '다'" in ln for ln in detection_diff(a, b))


def test_detection_diff_reports_count_mismatch_first():
    a = detection_texts(_det((0, ["가", "나"])))
    b = detection_texts(_det((0, ["가"])))
    assert "탐지 영역 수가 다르다" in detection_diff(a, b)[0]
