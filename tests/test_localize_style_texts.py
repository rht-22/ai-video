"""E16 — 화면에 얹는 글자의 현지화 (효과 텍스트·시간대별 제목·편집실 텍스트).

vlp `tests/test_localize_style_texts.py`(0757b68) 이식. 발주서: ves-orchestrator
`docs/prompts/e16-jp-style-texts.md`.

이 테스트가 고정하는 계약:
· **회귀 0** — 연출이 없는 편(checkpoint_style.json 없음)은 payload·산출이 종전과 같다.
· **멱등** — checkpoint_style.json 이 백업 목록에 있어야 두 번째 L3 가 재번역하지 않는다.
· **1:1 정렬** — 개수·인덱스가 어긋나면 즉시 실패(다른 문구가 다른 자리에 박히면 안 된다).
· **문구·폰트만 바꾼다** — 좌표·크기·색·fx·rotate 는 연출 의도라 불변.
· 스티커(images)·자막 강조(subtitle_styles)는 언어 중립이라 손대지 않는다.

이식본이 원본과 **같은 답을 내는지**는 `scripts/localize_port_diff.py` 가 따로 본다.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.localize import BACKUP_FILES  # noqa: E402
from app.localize.apply import l3_apply  # noqa: E402
from app.localize.collect import l0_backup  # noqa: E402
from app.localize.meta import build_ko_ja_pairs  # noqa: E402
from app.localize.rerender import visual_only_overrides  # noqa: E402
from app.localize.style_texts import (  # noqa: E402
    STYLE_PLAN_NAME, apply_editor_text_translation, apply_style_translation,
    editor_text_strings, style_plan_strings,
)
from app.localize.translate import build_payload, check_style_alignment  # noqa: E402

PLAN = {
    "schema": "style_plan/v1",
    "texts": [
        {"text": "쿵!", "source_time_sec": 105.0, "duration_sec": 1.2,
         "x": 0.7, "y": 0.25, "size": 110, "color": "#FFDD00",
         "stroke": "dark", "fx": "pop", "rotate": -8, "font": "Jalnan"},
        {"text": "설마…", "source_time_sec": 155.0, "duration_sec": 1.5,
         "x": 0.3, "y": 0.66, "size": 78, "color": "#FFFFFF", "font": "Jalnan"},
    ],
    "title_segments": [{"text": "반전 주의", "from_anchor": 150.0, "to_anchor": 160.0}],
    "images": [{"file": "style_assets/a.png", "source_time_sec": 107.0,
                "duration_sec": 2.0, "x": 0.5, "y": 0.3, "w": 0.2}],
    "subtitle_styles": [{"source_time_sec": 106.0, "style": {"size": 88, "color": "#FF4444"}}],
}
TR = {
    "style_texts": [{"index": 0, "ja": "ドンッ！"}, {"index": 1, "ja": "まさか…"}],
    "style_titles": [{"index": 0, "ja": "どんでん返し注意"}],
    "editor_texts": [{"index": 0, "ja": "エモい"}],
}


# ── 순수 헬퍼 ──────────────────────────────────────────────────────────────
def test_style_plan_strings():
    texts, titles = style_plan_strings(PLAN)
    assert texts == ["쿵!", "설마…"]
    assert titles == ["반전 주의"]
    assert style_plan_strings(None) == ([], [])
    assert style_plan_strings({}) == ([], [])


def test_editor_text_strings_matches_visual_override_order():
    ov = {"schema": "edit_overrides/v3", "texts": [{"text": "감동"}, {"text": "레전드"}]}
    assert editor_text_strings(ov) == ["감동", "레전드"]
    assert editor_text_strings(None) == []
    assert editor_text_strings({"images": []}) == []


def test_editor_text_index_matches_what_l4_actually_sends():
    """L1 이 센 순서와 L4 가 넘기는 배열이 같아야 한다 — 인덱스가 좌표다.

    visual_only_overrides 가 texts 를 걸러 내거나 순서를 바꾸면 '감동'의 번역이
    '레전드' 자리에 박힌다. 두 함수가 같은 배열을 보는지 여기서 못박는다."""
    ov = {"schema": "edit_overrides/v3", "subtitles": [{"index": 0, "text": "x"}],
          "texts": [{"text": "감동"}, {"text": "레전드"}], "images": [{"file": "a.png"}]}
    assert editor_text_strings(ov) == editor_text_strings(visual_only_overrides(ov))


def test_apply_translation_changes_only_text_and_font():
    out = apply_style_translation(PLAN, TR, font="ArialUnicode")
    assert [t["text"] for t in out["texts"]] == ["ドンッ！", "まさか…"]
    assert [t["font"] for t in out["texts"]] == ["ArialUnicode"] * 2
    assert out["title_segments"][0]["text"] == "どんでん返し注意"
    # 좌표·크기·색·fx·rotate 는 연출 의도 — 한 글자도 바뀌면 안 된다
    src, got = PLAN["texts"][0], out["texts"][0]
    for k in ("source_time_sec", "duration_sec", "x", "y", "size", "color",
              "stroke", "fx", "rotate"):
        assert got[k] == src[k], k
    # 제목 창의 앵커도 불변
    assert out["title_segments"][0]["from_anchor"] == 150.0
    # 언어 중립인 것은 손대지 않는다
    assert out["images"] == PLAN["images"]
    assert out["subtitle_styles"] == PLAN["subtitle_styles"]
    # 원본 불변(부작용 금지)
    assert PLAN["texts"][0]["text"] == "쿵!"


def test_apply_translation_font_optional():
    out = apply_style_translation(PLAN, TR)          # font 미지정 = 원래 폰트 유지
    assert [t["font"] for t in out["texts"]] == ["Jalnan", "Jalnan"]


def test_editor_text_translation():
    visual = {"schema": "edit_overrides/v3",
              "texts": [{"text": "감동", "x": 0.5, "y": 0.5, "size": 72}],
              "images": [{"file": "a.png"}]}
    out = apply_editor_text_translation(visual, TR, font="ArialUnicode")
    assert out["texts"][0]["text"] == "エモい"
    assert out["texts"][0]["font"] == "ArialUnicode"
    assert out["texts"][0]["x"] == 0.5 and out["texts"][0]["size"] == 72
    assert out["images"] == visual["images"]         # 스티커는 그대로


def test_alignment_mismatch_fails_loud():
    """개수가 어긋나면 다른 문구가 다른 자리에 박힌다 — 자막 정렬과 같은 규율."""
    for bad in ({"style_texts": [{"index": 0, "ja": "ドンッ！"}]},      # 2개인데 1개
                {"style_texts": []},
                {}):
        try:
            apply_style_translation(PLAN, bad)
        except RuntimeError as e:
            assert "style_texts" in str(e)
        else:
            raise AssertionError(f"정렬 불일치가 통과함: {bad}")


def test_index_out_of_range_fails_loud():
    try:
        apply_style_translation(PLAN, {"style_texts": [{"index": 0, "ja": "a"},
                                                       {"index": 5, "ja": "b"}]})
    except RuntimeError as e:
        assert "인덱스" in str(e)
    else:
        raise AssertionError("범위 밖 인덱스가 통과함")


def test_index_order_is_respected_not_array_order():
    """응답이 뒤섞여 와도 index 가 좌표다."""
    out = apply_style_translation(PLAN, {"style_texts": [{"index": 1, "ja": "B"},
                                                         {"index": 0, "ja": "A"}],
                                         "style_titles": TR["style_titles"]})
    assert [t["text"] for t in out["texts"]] == ["A", "B"]


# ── 회귀 0 · 멱등 ─────────────────────────────────────────────────────────
def test_no_style_plan_is_noop():
    """연출을 안 켠 채널(파일 없음)은 아무 일도 일어나지 않는다."""
    assert apply_style_translation({}, {}) == {}
    assert apply_editor_text_translation({}, {}) == {}
    assert apply_style_translation({"images": [1]}, {}) == {"images": [1]}


def test_style_plan_is_backed_up_for_idempotency():
    """백업에 없으면 두 번째 L3 가 **이미 일본어인 문구를 다시 번역**한다."""
    assert STYLE_PLAN_NAME in BACKUP_FILES
    with tempfile.TemporaryDirectory() as tmp:
        job = Path(tmp) / "job"; job.mkdir()
        (job / STYLE_PLAN_NAME).write_text(json.dumps(PLAN, ensure_ascii=False))
        (job / "shorts.mp4").write_bytes(b"x")
        backup = l0_backup(job)
        assert (backup / STYLE_PLAN_NAME).exists()
        saved = json.loads((backup / STYLE_PLAN_NAME).read_text(encoding="utf-8"))
        assert saved["texts"][0]["text"] == "쿵!"      # 한국어 원본이 보존된다


def _l3_fixture(tmp: str, with_style: bool):
    job = Path(tmp) / "job"; job.mkdir()
    backup = job / "bk"; backup.mkdir()
    out = job / "out"; out.mkdir()
    (backup / "subtitle_segments.json").write_text(json.dumps(
        [{"start_sec": 1.0, "end_sec": 2.0, "text": "하나"}], ensure_ascii=False))
    (backup / "checkpoint_story.json").write_text(json.dumps({"title_text": "T"}))
    (backup / "edit_plan.json").write_text(json.dumps({"layout": {"top_title": "T"}}))
    (backup / "checkpoint_resources.json").write_text(json.dumps({"tts_cue_files": []}))
    if with_style:
        (backup / STYLE_PLAN_NAME).write_text(json.dumps(PLAN, ensure_ascii=False))
    return job, backup, out


def test_l3_writes_japanese_style_plan():
    with tempfile.TemporaryDirectory() as tmp:
        job, backup, out = _l3_fixture(tmp, with_style=True)
        tr = {"top_title_ja": "上", "segments": [{"index": 0, "ja": "一"}],
              "tts_cues": [], "telops": [], **TR}
        l3_apply(job, backup, tr, [], {"display": "X"}, {"telop_font": "ArialUnicode"}, out)
        got = json.loads((job / STYLE_PLAN_NAME).read_text(encoding="utf-8"))
        assert [t["text"] for t in got["texts"]] == ["ドンッ！", "まさか…"]
        assert got["texts"][0]["font"] == "ArialUnicode"
        assert got["title_segments"][0]["text"] == "どんでん返し注意"
        # 백업(한국어 원본)은 그대로 — 두 번 돌려도 같은 결과가 나온다
        assert json.loads((backup / STYLE_PLAN_NAME).read_text())["texts"][0]["text"] == "쿵!"


def test_l3_without_style_plan_writes_nothing():
    """회귀 0 — 연출 없는 편은 job 에 checkpoint_style.json 이 생기지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        job, backup, out = _l3_fixture(tmp, with_style=False)
        tr = {"top_title_ja": "上", "segments": [{"index": 0, "ja": "一"}],
              "tts_cues": [], "telops": []}
        l3_apply(job, backup, tr, [], {"display": "X"}, {"telop_font": "ArialUnicode"}, out)
        assert not (job / STYLE_PLAN_NAME).exists()


def test_l3_is_idempotent():
    """두 번 돌려도 일본어가 다시 번역되지 않는다(백업 기준 규약)."""
    with tempfile.TemporaryDirectory() as tmp:
        job, backup, out = _l3_fixture(tmp, with_style=True)
        tr = {"top_title_ja": "上", "segments": [{"index": 0, "ja": "一"}],
              "tts_cues": [], "telops": [], **TR}
        args = ([], {"display": "X"}, {"telop_font": "ArialUnicode"}, out)
        l3_apply(job, backup, tr, *args)
        first = (job / STYLE_PLAN_NAME).read_text(encoding="utf-8")
        l3_apply(job, backup, tr, *args)
        assert (job / STYLE_PLAN_NAME).read_text(encoding="utf-8") == first


# ── L1 payload (이식본 분리 — build_payload 가 화면 글자를 걷는다) ─────────
def _payload_fixture(tmp: str, *, style: bool, editor: bool):
    """job/localize_backup_ko 구조 — build_payload 는 backup.parent 를 job 으로 본다."""
    job = Path(tmp) / "job"; job.mkdir()
    backup = job / "localize_backup_ko"; backup.mkdir()
    (backup / "subtitle_segments.json").write_text(json.dumps(
        [{"start_sec": 1.0, "end_sec": 2.0, "text": "하나"}], ensure_ascii=False))
    (backup / "checkpoint_resources.json").write_text(json.dumps({"tts_cue_files": []}))
    (backup / "title.txt").write_text("제목", encoding="utf-8")
    if style:
        (backup / STYLE_PLAN_NAME).write_text(json.dumps(PLAN, ensure_ascii=False))
    if editor:
        (job / "edit_overrides.json").write_text(json.dumps(
            {"schema": "edit_overrides/v3", "texts": [{"text": "감동"}]}, ensure_ascii=False))
    return job, backup


_WCFG = {"display": "X", "context": "c", "glossary": {}}


def test_payload_carries_screen_texts():
    with tempfile.TemporaryDirectory() as tmp:
        _, backup = _payload_fixture(tmp, style=True, editor=True)
        built = build_payload(backup, [], "작품", _WCFG)
        assert built["payload"]["style_texts"] == [{"index": 0, "ko": "쿵!"},
                                                   {"index": 1, "ko": "설마…"}]
        assert built["payload"]["style_titles"] == [{"index": 0, "ko": "반전 주의"}]
        assert built["payload"]["editor_texts"] == [{"index": 0, "ko": "감동"}]


def test_payload_without_screen_texts_is_byte_identical_to_before():
    """회귀 0 — 연출 없는 편의 프롬프트 입력에 새 키가 **하나도** 붙지 않는다.

    한 글자만 달라져도 그 편의 자막 번역 결과까지 흔들린다."""
    with tempfile.TemporaryDirectory() as tmp:
        _, backup = _payload_fixture(tmp, style=False, editor=False)
        payload = build_payload(backup, [], "작품", _WCFG)["payload"]
        assert set(payload) == {"work", "top_title", "segments", "tts_cues", "telops"}


def test_check_style_alignment_only_looks_at_what_was_sent():
    """보내지 않은 목록은 응답에 없어도 통과 — 있으면 1:1 이 아니면 즉시 실패."""
    built = {"style_texts": ["쿵!"], "style_titles": [], "editor_texts": []}
    check_style_alignment(built, {"style_texts": [{"index": 0, "ja": "ドンッ！"}]})
    check_style_alignment({"style_texts": [], "style_titles": [], "editor_texts": []}, {})
    try:
        check_style_alignment(built, {})
    except RuntimeError as e:
        assert "style_texts" in str(e)
    else:
        raise AssertionError("정렬 불일치가 통과함")


# ── 검수 카드 대역 ─────────────────────────────────────────────────────────
def test_pairs_expose_style_texts():
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "bk"; backup.mkdir()
        out = Path(tmp) / "out"; out.mkdir()
        (backup / STYLE_PLAN_NAME).write_text(json.dumps(PLAN, ensure_ascii=False))
        pairs = build_ko_ja_pairs(backup, out, {"segments": [], "tts_cues": [], **TR})
        assert [(r["idx"], r["ko"], r["ja"]) for r in pairs["style_texts"]] == [
            (0, "쿵!", "ドンッ！"), (1, "설마…", "まさか…")]
        assert pairs["style_texts"][0]["start"] == 105.0


def test_pairs_without_style_plan_is_empty():
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "bk"; backup.mkdir()
        out = Path(tmp) / "out"; out.mkdir()
        pairs = build_ko_ja_pairs(backup, out, {"segments": [], "tts_cues": []})
        assert pairs["style_texts"] == []
