"""편집실 오버라이드 — 계약 검증 + 적용 규약(전량 교체·pinned·첫 variant 한정).

배경: edit_plan.json 은 기록물이라 사람이 고쳐도 렌더가 보지 않는다(2026-08-16 실측).
관제 편집실의 수정을 파이프라인에 먹이는 유일한 통로가 --edit-overrides 이고,
이 파일이 그 계약을 못 박는다. 계약이 조용히 바뀌면 사람이 고친 값이 반영 안 된 채
영상이 나가므로, 위반은 예외로 즉시 실패해야 한다.
"""
from __future__ import annotations

import json

import pytest

from app.modules.edit_overrides import (
    EditOverrideError,
    apply_overrides,
    load_edit_overrides,
    overrides_clips,
    total_duration,
    validate_overrides,
)
from app.modules.story_builder import StoryClip


def _clip(role, start, end):
    return StoryClip(role=role, start_sec=float(start), end_sec=float(end),
                     subtitle="원문", use_original_audio=True, chunk_index=3,
                     candidate_index=1, character_focus=("A",))


def _variants():
    return [([_clip("hook", 10, 25), _clip("payoff", 100, 120)], "옛 제목", 1.0),
            ([_clip("hook", 50, 70)], "variant2 제목", 0.8)]


OV_FULL = {
    "schema": "edit_overrides/v1",
    "title": {"top_title": "새 제목 1줄\n2줄"},
    "clips": [{"start_sec": 742.5, "end_sec": 771.0, "role": "hook"},
              {"start_sec": 1102.0, "end_sec": 1118.5, "role": "payoff",
               "use_original_audio": False}],
}


# ── 검증 ──────────────────────────────────────────────────────────────
def test_schema_must_match():
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v2", "title": {"top_title": "x"}})
    with pytest.raises(EditOverrideError):
        validate_overrides({"title": {"top_title": "x"}})          # 스키마 누락
    with pytest.raises(EditOverrideError):
        validate_overrides([1, 2, 3])                              # 객체 아님


def test_reversed_or_negative_span_rejected():
    for bad in ([{"start_sec": 30, "end_sec": 10}],                # 뒤집힘
                [{"start_sec": -5, "end_sec": 10}],                # 음수
                [{"start_sec": 10, "end_sec": 10}]):               # 길이 0
        with pytest.raises(EditOverrideError):
            validate_overrides({"schema": "edit_overrides/v1", "clips": bad})


def test_missing_fields_and_bad_role_rejected():
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v1", "clips": [{"start_sec": 1}]})
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v1",
                            "clips": [{"start_sec": 1, "end_sec": 2, "role": "몰라"}]})


def test_empty_clips_rejected_but_absent_key_ok():
    # 전량 교체 규약: 빈 배열은 '전부 지움'이 아니라 실수로 본다
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v1", "clips": []})
    # 구간을 안 고치는 경우 = 키 자체가 없음
    assert validate_overrides({"schema": "edit_overrides/v1",
                               "title": {"top_title": "제목만"}})


def test_blank_title_rejected():
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v1", "title": {"top_title": "   "}})


# ── 적용 ──────────────────────────────────────────────────────────────
def test_clips_replaced_wholesale_and_pinned():
    out, pinned = apply_overrides(_variants(), OV_FULL)
    clips, title, score = out[0]
    assert pinned is True                                   # 자동 보정 건너뛰기 신호
    assert title == "새 제목 1줄\n2줄"
    assert [(c.start_sec, c.end_sec) for c in clips] == [(742.5, 771.0), (1102.0, 1118.5)]
    assert [c.role for c in clips] == ["hook", "payoff"]
    assert clips[1].use_original_audio is False
    assert score == 1.0                                     # 점수는 보존
    # 사람이 고른 구간은 Gemini 후보에 매달려 있지 않다 → 확장·클램프 lookup 무력화
    assert clips[0].chunk_index == -1 and clips[0].candidate_index == -1


def test_title_only_does_not_pin():
    """제목만 고쳤으면 구간은 종전 자동 보정을 그대로 받아야 한다 —
    제목 수정 때문에 구간 품질이 달라지면 사람이 놀란다."""
    out, pinned = apply_overrides(_variants(), {"schema": "edit_overrides/v1",
                                                "title": {"top_title": "제목만 교체"}})
    clips, title, _ = out[0]
    assert pinned is False
    assert title == "제목만 교체"
    assert [(c.start_sec, c.end_sec) for c in clips] == [(10.0, 25.0), (100.0, 120.0)]


def test_other_variants_untouched():
    """편집은 shorts #1 한 편을 고치는 일 — variant #2 는 자동 후보로 남겨둔다."""
    out, _ = apply_overrides(_variants(), OV_FULL)
    assert len(out) == 2
    assert out[1][1] == "variant2 제목"
    assert [(c.start_sec, c.end_sec) for c in out[1][0]] == [(50.0, 70.0)]


def test_no_override_is_identity():
    v = _variants()
    out, pinned = apply_overrides(v, None)
    assert out is v and pinned is False
    out2, pinned2 = apply_overrides([], OV_FULL)
    assert out2 == [] and pinned2 is False


def test_clips_absent_keeps_original_clips():
    out, pinned = apply_overrides(_variants(), {"schema": "edit_overrides/v1"})
    assert pinned is False
    assert out[0][1] == "옛 제목"                            # 제목도 그대로


def test_overrides_clips_none_when_no_key():
    assert overrides_clips({"schema": "edit_overrides/v1"}) is None
    assert overrides_clips(None) is None


def test_total_duration():
    assert total_duration([_clip("hook", 10, 25), _clip("payoff", 100, 120)]) == 35.0
    assert total_duration([]) == 0.0


# ── 로드 ──────────────────────────────────────────────────────────────
def test_load_roundtrip(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text(json.dumps(OV_FULL, ensure_ascii=False), encoding="utf-8")
    assert load_edit_overrides(p)["title"]["top_title"] == "새 제목 1줄\n2줄"
    assert load_edit_overrides(None) is None


def test_load_missing_or_broken_fails_loudly(tmp_path):
    with pytest.raises(EditOverrideError):
        load_edit_overrides(tmp_path / "없는파일.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{ 깨진 json", encoding="utf-8")
    with pytest.raises(EditOverrideError):
        load_edit_overrides(bad)


# ── 메타데이터 상속 (품질 직결) ────────────────────────────────────────
def test_metadata_inherited_from_best_overlap():
    """경계를 몇 초 옮긴 것은 같은 소재를 그대로 쓰는 것 — character_focus 를 잃으면
    얼굴 추적 타겟이 사라져 리프레이밍 품질이 눈에 띄게 나빠진다."""
    olds = [
        StoryClip(role="hook", start_sec=10, end_sec=25, subtitle="옛 자막A",
                  use_original_audio=True, chunk_index=2, candidate_index=5,
                  character_focus=("혜리", "리정"), visual_essential=True, tts_draft="초안"),
        StoryClip(role="payoff", start_sec=100, end_sec=120, subtitle="옛 자막B",
                  use_original_audio=True, chunk_index=7, candidate_index=1,
                  character_focus=("미연",)),
    ]
    ov = {"schema": "edit_overrides/v1",
          "clips": [{"start_sec": 12.0, "end_sec": 24.0, "role": "hook"},      # A 와 대부분 겹침
                    {"start_sec": 105.0, "end_sec": 118.0, "role": "payoff"}]}  # B 와 겹침
    got = overrides_clips(ov, olds)
    assert got[0].character_focus == ("혜리", "리정")
    assert (got[0].chunk_index, got[0].candidate_index) == (2, 5)
    assert got[0].subtitle == "옛 자막A" and got[0].visual_essential is True
    assert got[0].tts_draft == "초안"
    assert got[1].character_focus == ("미연",) and got[1].chunk_index == 7
    # 구간 자체는 사람이 지정한 값 그대로
    assert (got[0].start_sec, got[0].end_sec) == (12.0, 24.0)


def test_new_span_with_no_overlap_gets_empty_metadata():
    """원본 어디와도 겹치지 않는 새 구간 = 그 자리에 계획된 나레이션·인물 타겟이 없다."""
    olds = [StoryClip(role="hook", start_sec=10, end_sec=25, subtitle="A",
                      use_original_audio=True, chunk_index=2, candidate_index=5,
                      character_focus=("혜리",))]
    got = overrides_clips({"schema": "edit_overrides/v1",
                           "clips": [{"start_sec": 900.0, "end_sec": 915.0}]}, olds)
    assert got[0].character_focus == () and got[0].chunk_index == -1
    assert got[0].subtitle == "" and got[0].use_original_audio is True


def test_explicit_fields_win_over_inheritance():
    olds = [StoryClip(role="hook", start_sec=10, end_sec=25, subtitle="옛 자막",
                      use_original_audio=True, chunk_index=2, candidate_index=5)]
    got = overrides_clips({"schema": "edit_overrides/v1",
                           "clips": [{"start_sec": 11.0, "end_sec": 20.0,
                                      "subtitle": "사람이 쓴 자막",
                                      "use_original_audio": False}]}, olds)
    assert got[0].subtitle == "사람이 쓴 자막" and got[0].use_original_audio is False
    assert got[0].chunk_index == 2          # 명시 안 한 값은 여전히 상속


# ── CLI 배선 ──────────────────────────────────────────────────────────
def test_cli_accepts_edit_overrides_flag():
    from app.cli import build_parser
    args = build_parser().parse_args([
        "create_shorts", "--video", "v.mp4", "--title", "작품",
        "--from-step", "render", "--job-id", "작품_abc12345",
        "--edit-overrides", "/tmp/ov.json",
    ])
    assert args.edit_overrides == "/tmp/ov.json"
    assert args.from_step == "render" and args.job_id == "작품_abc12345"
    # 미지정 시 None — 종전 동작과 완전히 동일해야 한다
    plain = build_parser().parse_args(["create_shorts", "--video", "v.mp4", "--title", "작품"])
    assert plain.edit_overrides is None
