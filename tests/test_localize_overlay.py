"""L-P4 — overlay 계층 이식 고정.

이식 원본은 vlp `engine/*` + `src/process_video.py`. 회귀 0 이 조건이라 단계 순서·
설정 키·산출 이름을 안 바꿨고, 바꾼 둘(모델 규칙·경로 기준)은 여기서 못박는다.

이 파일이 지키는 것 넷:
  ① **모드 게이트** — overlay 를 안 쓰는 실행은 종전 rerender 그대로다
  ② **모델 규칙** — config 의 금지 모델을 읽지 않는다 (CLAUDE.md)
  ③ **라이선스 게이트** — propainter 는 상업 확인 없이 못 돈다 (§8-7)
  ④ **route ↔ 더빙 일치** — 어댑터와 값이 갈리면 더빙 빠진 편이 발행된다
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.localize.overlay import DUB_ROUTES, ROUTES  # noqa: E402
from app.localize.overlay import common, inpaint, llm  # noqa: E402
from app.localize.overlay.runner import needs_dub  # noqa: E402


# ── ② 모델 규칙 — 이식하며 의도적으로 갈라진 지점 ───────────────────────
def test_forbidden_config_models_are_ignored():
    """vlp config 는 gemini-3.5-flash·gemini-pro-latest 를 쓴다 — 이 레포는 금지다."""
    got = llm.resolve_model({"translate": {"model": "gemini-3.5-flash",
                                           "hero_model": "gemini-pro-latest"}})
    assert got == "gemini-3.6-flash"
    hero = llm.resolve_model({"translate": {"hero_model": "gemini-pro-latest"}}, hero=True)
    assert hero == "gemini-3.1-pro-preview"


def test_model_follows_env_like_the_rest_of_the_repo(monkeypatch):
    monkeypatch.setenv("GEMINI_FLASH_MODEL_NAME", "gemini-9.9-flash")
    assert llm.resolve_model({}) == "gemini-9.9-flash"


def test_explicit_llm_model_env_still_wins():
    """vlp 규약 유지 — 사람이 env 로 못박으면 그것이 이긴다."""
    import os
    os.environ["LLM_MODEL"] = "pinned-model"
    try:
        assert llm.resolve_model({}) == "pinned-model"
    finally:
        del os.environ["LLM_MODEL"]


# ── 경로 기준 — ai-video 레포 루트로 옮겼다 ─────────────────────────────
def test_project_root_is_the_ai_video_repo():
    """vlp 는 자기 레포였다. 여기가 어긋나면 config 의 상대경로가 전부 딴 데를 가리킨다."""
    assert (common.PROJECT_ROOT / "app" / "cli.py").exists()


def test_config_loads_from_the_overlay_data_dir():
    cfg = common.load_config()
    assert cfg["paths"]["persona"].startswith("app/localize/overlay/data/")
    assert common.resolve_path(cfg["paths"]["persona"]).exists()


def test_font_map_and_glossary_resolve():
    cfg = common.load_config()
    for key in ("font_map", "glossary"):
        assert common.resolve_path(cfg["paths"][key]).exists(), key


# ── route 정의는 config 가 정본 ─────────────────────────────────────────
def test_every_route_exists_in_the_config():
    """ROUTES 와 config.levels 가 갈리면 CLI 가 통과시킨 route 를 엔진이 거절한다."""
    levels = common.load_config()["levels"]
    assert set(ROUTES) == set(levels), (set(ROUTES) ^ set(levels))


def test_dub_routes_match_the_config_flag():
    """④ 어댑터 needs_dub 과 같은 값이어야 한다 — 갈리면 더빙 빠진 편이 발행된다."""
    levels = common.load_config()["levels"]
    from_config = {r for r, o in levels.items() if o.get("dub")}
    assert from_config == set(DUB_ROUTES)


@pytest.mark.parametrize("route,want", [("C", True), ("BC", True), ("c", True),
                                        ("A", False), ("B", False), ("BJ", False),
                                        ("", False), (None, False)])
def test_needs_dub(route, want):
    assert needs_dub(route) is want


def test_inpaint_and_render_mode_per_route():
    """route 표(계획서 §3-2)가 설정과 일치하는지 — 표가 거짓말이면 사람이 잘못 고른다."""
    levels = common.load_config()["levels"]
    assert (levels["A"]["inpaint"], levels["A"]["render_mode"]) == (False, "subtitle")
    assert (levels["B"]["inpaint"], levels["B"]["render_mode"]) == (True, "replace")
    assert (levels["BJ"]["inpaint"], levels["BJ"]["render_mode"]) == (False, "bilingual")
    assert (levels["C"]["inpaint"], levels["C"]["render_mode"]) == (True, "replace")
    assert (levels["BC"]["inpaint"], levels["BC"]["render_mode"]) == (True, "clean")


# ── ③ 라이선스 게이트 (§8-7) ────────────────────────────────────────────
def test_propainter_is_blocked_without_commercial_ack():
    """🛑 S-Lab 비상업 라이선스다. 상업 채널에 조용히 들어가면 안 된다."""
    with pytest.raises(RuntimeError, match="상업|NON-?COMMERCIAL|비상업"):
        inpaint.make_inpainter("propainter", {})


def test_ack_changes_which_wall_you_hit():
    """ack=true 면 **라이선스 벽**을 지나 그 다음 벽(가중치 미연동)에 닿아야 한다.

    두 메시지가 같으면 게이트가 도는지 확인할 방법이 없다 — 실제로 미연동 메시지는
    라이선스 문구를 안내로 덧붙이므로 '문구 포함'으로는 구분이 안 된다. 구분자는
    '미연동'이다."""
    with pytest.raises(RuntimeError) as blocked:
        inpaint.make_inpainter("propainter", {})
    assert "미연동" not in str(blocked.value)          # 라이선스에서 먼저 막혔다

    with pytest.raises(RuntimeError) as passed:
        inpaint.make_inpainter("propainter", {"inpaint": {"propainter_commercial_ack": True}})
    assert "미연동" in str(passed.value)               # 라이선스는 통과, 가중치에서 막혔다


def test_opencv_backend_needs_no_weights():
    """무설치 폴백이 살아 있어야 한다 — 무거운 의존 없이 파이프라인이 검증된다."""
    assert inpaint.make_inpainter("opencv", {}).name == "opencv"


# ── ① 모드 게이트 — overlay 를 안 쓰면 종전 그대로 ──────────────────────
def _parse(argv):
    from app.cli import build_parser
    return build_parser().parse_args(argv)


def test_mode_defaults_to_rerender():
    a = _parse(["localize", "--job-dir", "/j"])
    assert a.mode is None and a.job_dir == "/j"


def test_unknown_route_fails_at_the_cli():
    """조용히 기본값으로 떨어지면 사람은 일본어판을 만든 줄 알고 한국어판을 발행한다."""
    with pytest.raises(SystemExit):
        _parse(["localize", "--mode", "overlay", "--video", "/v.mp4",
                "--video-id", "x", "--route", "Z"])


def test_unknown_mode_fails_at_the_cli():
    with pytest.raises(SystemExit):
        _parse(["localize", "--mode", "sidecar", "--job-dir", "/j"])


def test_overlay_flags_exist_with_documented_defaults():
    a = _parse(["localize", "--mode", "overlay", "--video", "/v.mp4", "--video-id", "x"])
    assert a.route == "B" and a.locale == "ja"
    assert a.content_type is None and a.inpaint_backend is None


# ── 무거운 의존은 지연 임포트다 (§10-1 — 6대에 강제하지 않는다) ─────────
def test_importing_overlay_pulls_no_heavy_deps():
    """paddleocr·torch 가 임포트 시점에 필요하면 requirements 가 6대를 물게 된다."""
    import subprocess
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; import app.localize.overlay.pipeline as m; "
         "heavy=[n for n in ('paddleocr','torch','easyocr','rapidocr_onnxruntime') "
         "if n in sys.modules]; print(','.join(heavy))"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent))
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.strip() == "", f"임포트만으로 끌려온 무거운 의존: {r.stdout.strip()}"


# ── 이식 대조 — vlp 가 앞서가면 여기서 먼저 운다 ────────────────────────
def test_port_matches_vlp_when_the_source_is_present():
    """P2b·E16 때 vlp 가 두 번 앞서갔다. 그때 이식본이 뒤처진 것을 사람이 뒤늦게 알았다.

    vlp 체크아웃이 있는 환경에서만 돈다 — 없으면 건너뛴다(CI·컨테이너)."""
    from scripts.overlay_port_diff import DEFAULT_VLP, compare
    if not (DEFAULT_VLP / "engine" / "common.py").exists():
        pytest.skip(f"vlp 체크아웃 없음: {DEFAULT_VLP}")
    assert compare(DEFAULT_VLP) == 0, "vlp 와 어긋났다 — overlay_port_diff 출력을 보라"


def test_every_expected_diff_carries_a_reason():
    """사유 없는 예외를 두면 다음 사람이 '원래 그런 것'으로 읽는다."""
    from scripts.overlay_port_diff import (EXPECTED_CONST_DIFFS, EXPECTED_DIFFS,
                                           EXPECTED_MISSING)
    for table in (EXPECTED_DIFFS, EXPECTED_CONST_DIFFS, EXPECTED_MISSING):
        for key, why in table.items():
            assert len(why) > 20, f"{key} 의 사유가 너무 짧다: {why!r}"
