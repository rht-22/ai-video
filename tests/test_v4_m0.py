"""V4-M0 — v4 착수 전 선행 수정 회귀 가드.

기획: `docs/v4/v4-plan.md` §8 M0. 세 가지를 고정한다.

① 편집실 `design` 오버라이드가 **조용히 버려지지 않는다**(v3 결함 · 선행 수정).
② 리플레이 로더가 **v4 를 인식한다** — 파이프라인 마커·승인 2위 이하·후보 마커.
③ v1 의 `max_shorts` 3-클램프 자리를 **못박는다** — v4 는 1~8 이라 이걸 물려받으면
   승인 편이 4번째부터 조용히 사라진다(O7 의 실패 모드).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.replay.loader import load_archive
from app.v3.overrides import (
    DESIGN_KEYS,
    apply_design_to_style,
    apply_overrides_to_plan,
)

GRID = {"span_candidates": [
    {"id": "sp0000", "t_in": 100.0, "t_out": 104.0, "is_audio": True},
    {"id": "sp0001", "t_in": 104.0, "t_out": 110.0, "is_audio": False},
]}
PLAN = {"schema": "edit_plan/v3", "layout": {"top_title": "가\n나"},
        "timeline": [{"role": "hook", "clip_start_sec": 100.0, "clip_end_sec": 104.0,
                      "subtitle": "", "use_original_audio": True,
                      "reframe": {"mode": "center"}, "span_ids": ["sp0000"]}]}


# ── ① design 오버라이드 ─────────────────────────────────────────────────────

def test_design_override_is_applied_as_last_layer():
    style = {"design": {"subtitle_color": "#FFFFFF", "subtitle_size": 60},
             "beats": [], "labels": []}
    new, rec = apply_design_to_style({"subtitle_color": "#FF0000"}, style)
    assert new["design"]["subtitle_color"] == "#FF0000"     # 사람이 이긴다
    assert new["design"]["subtitle_size"] == 60             # 안 건드린 키는 그대로
    assert rec["applied"] == ["subtitle_color"] and rec["ignored"] == []
    assert new["editor_design"] == ["subtitle_color"]       # 누가 정했는지 남는다
    # 순수 — 넘겨받은 dict 는 그대로다
    assert style["design"]["subtitle_color"] == "#FFFFFF"


def test_design_keys_outside_the_render_path_are_recorded_not_swallowed():
    """화면에 닿지 않는 키는 **크게 남긴다**. 조용한 무시가 이 결함의 정체였다."""
    _new, rec = apply_design_to_style(
        {"title_bold": True, "made_up_key": 1, "title_size": 92}, {})
    assert rec["applied"] == ["title_size"]
    assert rec["ignored"] == ["made_up_key", "title_bold"]


def test_title_bold_is_not_in_design_keys():
    """제목 굵게는 E21 로 닫혔고 design_from_style 도 조립하지 않는다 —
    DESIGN_KEYS 에 넣으면 그쪽에서 조용히 사라진다."""
    assert "title_bold" not in DESIGN_KEYS and "title_bold2" not in DESIGN_KEYS


def test_design_keys_match_what_the_renderer_actually_reads():
    """DESIGN_KEYS ↔ finalize.design_from_style 은 1:1 계약이다.
    한쪽만 넓히면 조용히 무시된다(가왕쇼 443px 판례와 같은 부류)."""
    src = Path("app/v3/finalize.py").read_text(encoding="utf-8")
    body = src[src.index("def design_from_style"):src.index("def video_band_ratio")]
    for k in DESIGN_KEYS:
        assert f'"{k}"' in body, f"{k} 를 design_from_style 이 읽지 않는다"


def test_plan_overrides_defer_design_instead_of_claiming_or_dropping_it():
    """`apply_overrides_to_plan` 은 design 을 처리하지 않는다(style_doc 을 받지도
    않는다). 종전에는 HANDLED_KEYS 에 있어서 unhandled 기록조차 없었다 —
    이제 deferred 로 남는다."""
    ov = {"schema": "edit_overrides/v2", "design": {"subtitle_size": 70}}
    _p, _s, _r, rec = apply_overrides_to_plan(ov, PLAN, GRID, [], {})
    assert rec["deferred"] == ["design → style 단계에서 적용"]
    assert "design" not in rec["unhandled"]
    assert not any("design" in a for a in rec["applied"])


def test_pipeline_applies_editor_design_after_the_style_checkpoint():
    """자리가 계약이다 — 체크포인트에는 AI 플랜만, 사람 층은 그 **뒤**에.
    앞에 두면 사람 값이 체크포인트에 굳어 다음 재개에서 AI 것과 구분되지 않고,
    렌더 지문(확정 style 기준) 계산 **앞**이어야 design 수정이 최종본 캐시를 폐긴다."""
    src = Path("app/v3/pipeline.py").read_text(encoding="utf-8")
    i_ckpt = src.index('_write_json(style_ckpt, {"fingerprint"')
    i_apply = src.index("apply_design_to_style")
    i_style_json = src.index('_write_json(output_dir / "style.json", style_doc)')
    i_render_fp = src.index("render_fp = hashlib.sha1")
    assert i_ckpt < i_apply < i_style_json < i_render_fp


# ── ② 리플레이 로더 v4 인식 ─────────────────────────────────────────────────

def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _v4_job(root: Path, name: str = "job_v4") -> Path:
    d = root / name
    _write(d / "run_log.json", {"job_id": name, "pipeline": "v4",
                                "provenance": {"git_sha": "abc1234"}})
    _write(d / "edit_plan.json", {"timeline": [
        {"clip_start_sec": 10.0, "clip_end_sec": 20.0}]})
    _write(d / "edit_plan_2.json", {"timeline": [
        {"clip_start_sec": 30.0, "clip_end_sec": 45.0}]})
    _write(d / "checkpoint_story.json", {
        "clips": [{"start_sec": 10.0, "end_sec": 20.0}],
        "variants": [{"clips": [{"start_sec": 10.0, "end_sec": 20.0}]},
                     {"clips": [{"start_sec": 30.0, "end_sec": 45.0}]}]})
    _write(d / "checkpoint_candidates.json", {"approved": [{"id": "c1"}, {"id": "c2"}]})
    return d


def test_loader_reads_every_approved_edit_of_a_v4_job(tmp_path):
    """v4 는 결함 게이트 통과분을 전부 낸다 — 1위만 세면 승인 편수를 과소 보고한다."""
    _v4_job(tmp_path)
    arc = load_archive(tmp_path)
    assert arc["layout"] == "jobdir" and not arc["broken"]
    assert len(arc["records"]) == 2
    ranks = sorted(r["approved_rank"] for r in arc["records"])
    assert ranks == [1, 2]
    second = next(r for r in arc["records"] if r["approved_rank"] == 2)
    assert second["key"].endswith("#2")
    assert second["timeline"][0]["clip_start_sec"] == 30.0
    # 원안도 그 승인 편의 것이어야 한다(1위 것을 재사용하면 분포가 거짓이 된다)
    assert second["story_clips"][0]["start_sec"] == 30.0
    assert all(r["pipeline"] == "v4" for r in arc["records"])


def test_loader_ignores_v3_hook_variant_plans(tmp_path):
    """v3 훅 변형(`edit_plan_variant_{k}.json`)은 본편 불변의 훅 교체이지
    승인 편이 아니다 — 숫자 접미만 승인으로 센다."""
    d = tmp_path / "job_v3"
    _write(d / "run_log.json", {"job_id": "job_v3", "pipeline": "v3"})
    _write(d / "edit_plan.json", {"timeline": []})
    _write(d / "edit_plan_variant_1.json", {"timeline": []})
    arc = load_archive(tmp_path)
    assert len(arc["records"]) == 1 and arc["records"][0]["pipeline"] == "v3"


def test_v1_job_records_are_unchanged(tmp_path):
    """회귀 0 — v1 편은 마커가 빈 문자열이고 기록 수도 종전과 같다."""
    d = tmp_path / "job_v1"
    _write(d / "run_log.json", {"job_id": "job_v1"})
    _write(d / "edit_plan.json", {"timeline": [{"clip_start_sec": 1.0,
                                                "clip_end_sec": 2.0}]})
    _write(d / "checkpoint_story.json", {"clips": [{"start_sec": 1.0, "end_sec": 2.0}]})
    arc = load_archive(tmp_path)
    assert len(arc["records"]) == 1
    r = arc["records"][0]
    assert r["pipeline"] == "" and r["approved_rank"] == 1
    assert r["story_clips"] == [{"start_sec": 1.0, "end_sec": 2.0}]


def test_candidates_only_job_is_recognized_not_rejected(tmp_path):
    """후보 깔때기까지 돌고 편성이 실패한 v4 편 — 레이아웃은 인식하고 기록 0건."""
    d = tmp_path / "job_failed"
    _write(d / "checkpoint_candidates.json", {"candidates": []})
    arc = load_archive(tmp_path)
    assert arc["layout"] == "jobdir" and arc["records"] == []


def test_unknown_layout_still_fails_loud(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        load_archive(tmp_path)


# ── ③ v1 max_shorts 3-클램프 자리 못박기 ────────────────────────────────────

def test_v1_clamps_max_shorts_to_three_in_two_places():
    """v4 는 승인 편을 1~8 낸다(O7). v1 의 이 두 클램프를 물려받으면 4번째부터
    **조용히 사라진다** — v4 는 자기 상한을 자기 진입점에서 정해야 한다."""
    cli = Path("app/cli.py").read_text(encoding="utf-8")
    assert 'max_shorts = min(max(getattr(args, "max_shorts", 3), 1), 3)' in cli
    pipe = Path("app/pipeline.py").read_text(encoding="utf-8")
    assert "max_shorts = min(payload.max_shorts, config.max_shorts_count)" in pipe
    cfg = Path("app/config.py").read_text(encoding="utf-8")
    assert "max_shorts_count: int = 3" in cfg
