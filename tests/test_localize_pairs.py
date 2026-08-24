"""L-P1 — ko_ja_pairs · apply_overrides · L3 적용 회귀 가드
(vlp tests/test_localize_run_pairs.py 이식).

검수 카드 한글 대역과 반려-수정 재렌더의 계약이다. 좌표(idx)가 어긋나면 사람이 고친
값이 엉뚱한 줄에 붙으므로, 원본 테스트의 단언을 값까지 그대로 옮겼다.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.localize.apply import (  # noqa: E402
    apply_segments, clamp_hallucination, has_user_timing, l3_apply,
)
from app.localize.meta import build_description, build_ko_ja_pairs  # noqa: E402
from app.localize.overrides import apply_overrides  # noqa: E402


def test_pairs_from_backup_and_translation():
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "bk"; backup.mkdir()
        out = Path(tmp) / "out"; out.mkdir()
        (backup / "edit_plan.json").write_text(json.dumps(
            {"layout": {"top_title": "힐링 여행인 줄 알았는데"}}, ensure_ascii=False))
        (backup / "subtitle_segments.json").write_text(json.dumps(
            [{"start_sec": 1.0, "end_sec": 2.0, "text": "너무 예뻐요"},
             {"start_sec": 3.0, "end_sec": 4.0, "text": "이건가?"}], ensure_ascii=False))
        (backup / "checkpoint_resources.json").write_text(json.dumps(
            {"tts_cue_files": [{"path": "x.mp3", "cue_index": 0,
                                "cue": {"text": "과연 결과는?"}}]}, ensure_ascii=False))
        # 텔롭 소스는 onscreen_refined.json — idx = orig_index 좌표(계약)
        (out / "onscreen_refined.json").write_text(json.dumps(
            [{"text_ko": "과연 혜리는", "kind": "broadcast_telop", "orig_index": 0,
              "start_sec": 3.1, "end_sec": 6.2},
             {"text_ko": "지난 이야기", "kind": "broadcast_telop", "orig_index": 1,
              "start_sec": 8.0, "end_sec": 9.5}], ensure_ascii=False))
        tr = {"top_title_ja": "何もない田舎家で",
              "segments": [{"index": 0, "ja": "すごくきれい"}, {"index": 1, "ja": "これかな？"}],
              "tts_cues": [{"index": 0, "ja": "果たして結果は？"}],
              "telops": [{"index": 0, "use": True, "ja": "果たしてヘリは"},
                         {"index": 1, "use": False}]}
        pairs = build_ko_ja_pairs(backup, out, tr)
        assert pairs["top_title"] == {"ko": "힐링 여행인 줄 알았는데", "ja": "何もない田舎家で"}
        assert pairs["subs"][0] == {"idx": 0, "start": 1.0, "end": 2.0,
                                    "ko": "너무 예뻐요", "ja": "すごくきれい"}
        assert len(pairs["subs"]) == 2 and pairs["subs"][1]["idx"] == 1
        assert pairs["tts"] == [{"idx": 0, "start": None, "end": None,
                                 "ko": "과연 결과는?", "ja": "果たして結果は？"}]
        # use:false 텔롭(숨김 처리분)은 대역에서도 뺀다 — 남은 항목은 원래 idx 를 유지
        assert pairs["telops"] == [{"idx": 0, "start": 3.1, "end": 6.2,
                                    "ko": "과연 혜리는", "ja": "果たしてヘリは"}]


def test_pairs_survive_missing_files():
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "no"
        out = Path(tmp) / "no2"
        pairs = build_ko_ja_pairs(backup, out, {"top_title_ja": "タイトル"})
        assert pairs["top_title"]["ja"] == "タイトル" and pairs["subs"] == [] \
            and pairs["tts"] == [] and pairs["telops"] == []


def test_apply_overrides_merges_by_index():
    tr = {"youtube_title_ja": "旧タイトル", "top_title_ja": "旧\n上部", "description_ja": "旧説明",
          "segments": [{"index": 0, "ja": "一"}, {"index": 1, "ja": "二"}],
          "tts_cues": [{"index": 0, "ja": "ナレ"}],
          "telops": [{"index": 0, "use": True, "ja": "テロップ"}]}
    ov = {"youtube_title_ja": "新タイトル",
          "subs": {"1": "修正二", "9": "없는 인덱스는 무시"},
          "tts": {"0": {"ja": "新ナレ"}},
          "telops": {"0": {"ja": "新テロップ", "use": False}}}
    out = apply_overrides(tr, ov)
    assert out["youtube_title_ja"] == "新タイトル"
    assert out["top_title_ja"] == "旧\n上部"                 # 안 고친 필드는 그대로
    assert out["segments"][1]["ja"] == "修正二"
    assert out["tts_cues"][0]["ja"] == "新ナレ"
    assert out["telops"][0] == {"index": 0, "use": False, "ja": "新テロップ"}
    # 원본 불변(순수) — 렌더 정본은 호출부가 재기록으로만 바꾼다
    assert tr["youtube_title_ja"] == "旧タイトル" and tr["segments"][1]["ja"] == "二"


def test_apply_overrides_ignores_blank_and_bad_keys():
    tr = {"segments": [{"index": 0, "ja": "一"}]}
    out = apply_overrides(tr, {"youtube_title_ja": "  ", "subs": {"abc": "x", "0": "  "}})
    assert "youtube_title_ja" not in out and out["segments"][0]["ja"] == "一"


# ── 줄 스타일·타이밍 오버라이드 (편집실 계약) ──────────────────────────

def test_apply_overrides_merges_style_and_timing():
    tr = {"segments": [{"index": 0, "ja": "一"}],
          "telops": [{"index": 0, "use": True, "ja": "テロップ"}]}
    ov = {"subs": {"0": {"ja": "新一", "style": {"size": 64, "color": "#ffdd00"},
                         "start_sec": 1.5, "end_sec": 4.0}},
          "telops": {"0": {"style": {"rotate": -8, "y": 0.5}}}}
    out = apply_overrides(tr, ov)
    seg = out["segments"][0]
    assert seg["ja"] == "新一" and seg["start_sec"] == 1.5 and seg["end_sec"] == 4.0
    assert seg["style"] == {"size": 64.0, "color": "#FFDD00"}     # 검증·정규화 통과본
    assert out["telops"][0]["style"] == {"rotate": -8.0, "y": 0.5}
    assert "start_sec" not in out["telops"][0]                    # 안 보낸 키는 안 생긴다


def test_apply_overrides_rejects_unknown_style_key_and_bad_ranges():
    tr = {"segments": [{"index": 0, "ja": "一"}]}
    with pytest.raises(ValueError):                               # 모르는 style 키 거절
        apply_overrides(tr, {"subs": {"0": {"style": {"fontsize": 64}}}})
    with pytest.raises(ValueError):                               # y 범위 밖
        apply_overrides(tr, {"subs": {"0": {"style": {"y": 1.5}}}})
    with pytest.raises(ValueError):                               # end ≤ start
        apply_overrides(tr, {"subs": {"0": {"start_sec": 5.0, "end_sec": 5.0}}})


def test_apply_overrides_rejects_tts_style_and_timing_as_followup():
    tr = {"tts_cues": [{"index": 0, "ja": "ナレ"}]}
    with pytest.raises(ValueError):                               # 후속 범위 — 조용한 무시 금지
        apply_overrides(tr, {"tts": {"0": {"style": {"size": 40}}}})
    with pytest.raises(ValueError):
        apply_overrides(tr, {"tts": {"0": {"end_sec": 9.0}}})
    out = apply_overrides(tr, {"tts": {"0": {"ja": "新ナレ"}}})   # 텍스트 수정은 종전대로
    assert out["tts_cues"][0]["ja"] == "新ナレ"


# ── 환각 클램프 — L3 와 L5 가 **같은 함수**를 쓴다 ─────────────────────

def test_clamp_shortens_long_span_with_short_text():
    assert clamp_hallucination(0.0, 22.0, "短い", user_timing=False) == 4.0


def test_clamp_leaves_long_text_alone():
    assert clamp_hallucination(0.0, 22.0, "あ" * 21, user_timing=False) == 22.0


def test_clamp_leaves_short_span_alone():
    assert clamp_hallucination(0.0, 5.0, "短い", user_timing=False) == 5.0


def test_user_timing_beats_clamp():
    """사람이 보고 정한 값이 이긴다."""
    assert clamp_hallucination(30.0, 50.0, "短い", user_timing=True) == 50.0


def test_has_user_timing():
    assert has_user_timing({"start_sec": 1.0}) is True
    assert has_user_timing({"end_sec": 1.0}) is True
    assert has_user_timing({"ja": "x"}) is False


def test_pairs_reflect_style_timing_and_clamp_priority():
    """subs.end 는 실표시 값: 8s/20자 클램프 반영, 사용자 타이밍은 클램프를 이긴다."""
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "bk"; backup.mkdir()
        out = Path(tmp) / "out"; out.mkdir()
        (backup / "subtitle_segments.json").write_text(json.dumps(
            [{"start_sec": 0.0, "end_sec": 22.0, "text": "환각"},      # 초장 구간
             {"start_sec": 30.0, "end_sec": 50.0, "text": "사용자 지정"}], ensure_ascii=False))
        tr = {"top_title_ja": "T",
              "segments": [{"index": 0, "ja": "短い"},
                           {"index": 1, "ja": "短い2", "start_sec": 31.0, "end_sec": 48.0,
                            "style": {"color": "#FF0000"}}],
              "tts_cues": [], "telops": []}
        pairs = build_ko_ja_pairs(backup, out, tr)
        assert pairs["subs"][0]["end"] == 4.0                      # 클램프(L3 와 동일 함수)
        assert pairs["subs"][1]["start"] == 31.0 and pairs["subs"][1]["end"] == 48.0
        assert pairs["subs"][1]["style"] == {"color": "#FF0000"}


# ── 자막 소프트 삭제 (E6-0 — subs use:false) ──────────────────────────

def test_apply_overrides_sub_use_false_and_type_guards():
    tr = {"segments": [{"index": 0, "ja": "一"}, {"index": 1, "ja": "二"}],
          "tts_cues": [{"index": 0, "ja": "ナレ"}]}
    out = apply_overrides(tr, {"subs": {"0": {"use": False}}})
    assert out["segments"][0]["use"] is False and "use" not in out["segments"][1]
    assert "use" not in tr["segments"][0]                # 원본 불변(순수)
    with pytest.raises(ValueError):                      # 불리언 외 거절(조용한 무시 금지)
        apply_overrides(tr, {"subs": {"0": {"use": "false"}}})
    with pytest.raises(ValueError):                      # tts 삭제는 후속 범위 — fail-loud
        apply_overrides(tr, {"tts": {"0": {"use": False}}})


def test_pairs_skip_deleted_subs_keep_idx():
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "bk"; backup.mkdir()
        out = Path(tmp) / "out"; out.mkdir()
        (backup / "subtitle_segments.json").write_text(json.dumps(
            [{"start_sec": 1.0, "end_sec": 2.0, "text": "하나"},
             {"start_sec": 3.0, "end_sec": 4.0, "text": "둘"}], ensure_ascii=False))
        tr = {"segments": [{"index": 0, "ja": "一", "use": False},
                           {"index": 1, "ja": "二"}]}
        pairs = build_ko_ja_pairs(backup, out, tr)
        # 뺀 줄은 다음 카드에서 빠지고, 남은 줄은 필터 전 좌표(idx)를 유지한다
        assert [r["idx"] for r in pairs["subs"]] == [1]
        assert pairs["subs"][0]["ja"] == "二"


def test_apply_segments_drops_deleted_and_translates():
    segs = [{"start_sec": 1.0, "end_sec": 2.0, "text": "하나"},
            {"start_sec": 3.0, "end_sec": 4.0, "text": "둘"},
            {"start_sec": 5.0, "end_sec": 6.0, "text": "셋"}]
    tr = {"segments": [{"index": 0, "ja": "一", "use": False},
                       {"index": 1, "ja": "二"}, {"index": 2, "ja": "三"}]}
    kept, dropped = apply_segments(segs, tr)
    assert [s["text"] for s in kept] == ["二", "三"] and dropped == 1


def test_l3_apply_drops_deleted_segments():
    with tempfile.TemporaryDirectory() as tmp:
        job = Path(tmp) / "job"; job.mkdir()
        backup = job / "bk"; backup.mkdir()
        out = job / "out"; out.mkdir()
        (backup / "subtitle_segments.json").write_text(json.dumps(
            [{"start_sec": 1.0, "end_sec": 2.0, "text": "하나"},
             {"start_sec": 3.0, "end_sec": 4.0, "text": "둘"},
             {"start_sec": 5.0, "end_sec": 6.0, "text": "셋"}], ensure_ascii=False))
        (backup / "checkpoint_story.json").write_text(json.dumps({"title_text": "T"}))
        (backup / "edit_plan.json").write_text(json.dumps({"layout": {"top_title": "T"}}))
        (backup / "checkpoint_resources.json").write_text(json.dumps({"tts_cue_files": []}))
        tr = {"top_title_ja": "上", "segments": [
                  {"index": 0, "ja": "一", "use": False},
                  {"index": 1, "ja": "二"},
                  {"index": 2, "ja": "三"}],
              "tts_cues": [], "telops": []}
        l3_apply(job, backup, tr, [], {"display": "X"}, {"telop_font": "Arial"}, out)
        written = json.loads((job / "subtitle_segments.json").read_text())
        # use=false 줄은 전사에서 빠진다(ai-video 렌더 제외) — 나머지는 번역 반영
        assert [s["text"] for s in written] == ["二", "三"]


def test_l3_apply_writes_title_to_both_render_and_publish_sources():
    """렌더 정본은 checkpoint_story, 발행용은 edit_plan — 둘 다 갱신해야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        job = Path(tmp) / "job"; job.mkdir()
        backup = job / "bk"; backup.mkdir()
        out = job / "out"; out.mkdir()
        (backup / "subtitle_segments.json").write_text("[]")
        (backup / "checkpoint_story.json").write_text(json.dumps(
            {"variants": [{"title_text": "구제목"}, {"title_text": "구제목2"}]}))
        (backup / "edit_plan.json").write_text(json.dumps({"layout": {"top_title": "구"}}))
        (backup / "checkpoint_resources.json").write_text(json.dumps({"tts_cue_files": []}))
        l3_apply(job, backup, {"top_title_ja": "新\n題", "segments": [], "tts_cues": [],
                               "telops": []}, [],
                 {"display": "ヘミリイェチェパ"}, {"telop_font": "Arial"}, out)
        story = json.loads((job / "checkpoint_story.json").read_text())
        plan = json.loads((job / "edit_plan.json").read_text())
        assert [v["title_text"] for v in story["variants"]] == ["新\n題", "新\n題"]
        assert plan["layout"]["top_title"] == "新\n題"
        assert plan["layout"]["bottom_label"] == "ヘミリイェチェパ"


# ── 설명란 — 권리 고지는 번역하지 않는다 ───────────────────────────────

def test_build_description_keeps_notice_verbatim_and_dedupes_hashtags():
    wcfg = {"notice_lines": ["채널 ENA에서 시청 가능 / チャンネルENAで視聴可能"],
            "hashtags_base": ["#ヘミリイェチェパ", "#韓国バラエティ"]}
    tr = {"description_ja": "本文です。", "hashtags_extra": ["#韓国バラエティ", "#추가"]}
    desc, tags = build_description(tr, wcfg)
    assert "채널 ENA에서 시청 가능 / チャンネルENAで視聴可能" in desc      # 원문 그대로
    assert tags == ["#ヘミリイェチェパ", "#韓国バラエティ", "#추가"]        # 중복 제거·순서 유지
    assert desc.startswith("本文です。")
