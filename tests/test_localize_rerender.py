"""L-P1 — 현지화 rerender 계층 회귀 가드 (vlp tests/test_scene_rerender.py 이식).

이관이 충실했는지를 이 파일이 증명한다. 원본 테스트의 단언을 **값까지 그대로** 옮겼고,
이식하며 새로 뺀 순수 함수(group_hits·render_argv 등)의 가드를 덧붙였다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.localize import apply as loc_apply  # noqa: E402
from app.localize import rerender, spec, telop  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ───────── 엔진 경로 (워커에서 깨지지 않는 것이 핵심) ─────────

def test_engine_path_falls_back_to_sibling(monkeypatch):
    """로컬 ~/ves/<engine> · 워커 $VES_HOME/engines/<engine> 둘 다 형제 배치다."""
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    assert spec.engine_path("BRAIN_ROOT", "ai-improvement-edit-video") == \
        ROOT.parent / "ai-improvement-edit-video"


def test_engine_path_env_wins(monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", "/opt/ves/engines/brain")
    assert str(spec.engine_path("BRAIN_ROOT", "x")) == "/opt/ves/engines/brain"


def test_no_absolute_user_paths_in_localize_package():
    """특정 계정 홈이 박히면 워커에서 100% 실패한다 — 회귀 방지."""
    for py in (ROOT / "app" / "localize").rglob("*.py"):
        assert "/Users/" not in py.read_text(encoding="utf-8"), py


def test_model_rule_is_ai_video_not_vlp(monkeypatch):
    """CLAUDE.md 모델 규칙 — vlp 가 쓰던 `gemini-3-flash-preview` 는 이 레포에서 금지다."""
    monkeypatch.delenv("GEMINI_MODEL_NAME", raising=False)
    monkeypatch.delenv("GEMINI_FLASH_MODEL_NAME", raising=False)
    assert spec.model_pro() == "gemini-3.1-pro-preview"
    assert spec.model_flash() == "gemini-3.6-flash"
    # 산문(주석)에는 '왜 안 쓰는지'를 적어 두므로, **코드로 쓰인 형태**만 막는다.
    for py in (ROOT / "app" / "localize").rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert '"gemini-3-flash-preview"' not in src, py
        assert "'gemini-3-flash-preview'" not in src, py


# ───────── 재렌더 노브 복원 (컷 재현 = 자막 싱크) ─────────

def _run_log(**app):
    return {"provenance": {"config": {"app": app}}}


def test_render_flags_restores_aggressive_tight():
    f = rerender.render_flags(_run_log(silence_cut_profile="aggressive", target_duration_sec=45,
                                       max_duration_sec=50, max_duration_tolerance=1.1))
    assert f[:2] == ["--silence-profile", "aggressive"]
    assert "--length-profile" in f and f[f.index("--length-profile") + 1] == "tight"
    assert "--loudness-lufs" in f


def test_render_flags_conservative_standard_has_no_length_flag():
    f = rerender.render_flags(_run_log(silence_cut_profile="conservative", target_duration_sec=60,
                                       max_duration_sec=70, max_duration_tolerance=1.5))
    assert f[:2] == ["--silence-profile", "conservative"]
    assert "--length-profile" not in f


def test_render_flags_tolerates_missing_provenance():
    """옛 런(provenance 없음)이라도 죽지 않고 최소한 라우드니스는 준다."""
    assert "--loudness-lufs" in rerender.render_flags({})


def test_render_flags_ignores_unknown_profile(monkeypatch, tmp_path):
    """모르는 프로파일은 무시한다 — 폴백까지 없을 때의 순수 동작.

    ⚠ brain 이 형제로 있으면 여기서 loop_policy.json 폴백이 발동한다(설계대로).
    그 폴백을 테스트하려면 아래 test_render_flags_falls_back_to_policy 를 본다."""
    monkeypatch.setattr(rerender, "BRAIN", tmp_path / "nonexistent")
    f = rerender.render_flags(_run_log(silence_cut_profile="wild"))
    assert "--silence-profile" not in f
    assert f == ["--loudness-lufs", "-14"]


def test_render_flags_falls_back_to_policy(monkeypatch, tmp_path):
    """provenance 에서 아무 노브도 못 건지면 brain 의 현재 정책으로 폴백한다."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "loop_policy.json").write_text(
        '{"gen_flags_base": ["--silence-profile", "aggressive"]}', encoding="utf-8")
    monkeypatch.setattr(rerender, "BRAIN", tmp_path)
    f = rerender.render_flags({})
    assert f[:2] == ["--silence-profile", "aggressive"]
    assert "--loudness-lufs" in f


def test_render_flags_provenance_beats_policy(monkeypatch, tmp_path):
    """정본은 그 런이 **실제로 쓴** 값이다 — 정책은 런 이후 바뀌었을 수 있다."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "loop_policy.json").write_text(
        '{"gen_flags_base": ["--silence-profile", "aggressive"]}', encoding="utf-8")
    monkeypatch.setattr(rerender, "BRAIN", tmp_path)
    f = rerender.render_flags(_run_log(silence_cut_profile="conservative"))
    assert f[:2] == ["--silence-profile", "conservative"]


# ───────── 재렌더 argv (job-id 고정이 핵심) ─────────

def test_render_argv_pins_job_id_and_step():
    argv = rerender.render_argv("/py", Path("/out/JOB1"), "ヘミリイェチェパ", "/src.mp4",
                                {"title_font": "ArialUnicode", "subtitle_font": "ArialUnicode"},
                                ["--silence-profile", "aggressive"])
    assert argv[:4] == ["/py", "-m", "app.cli", "create_shorts"]
    # --job-id 를 주므로 제목이 달라도 디렉토리가 새로 생기지 않는다
    assert argv[argv.index("--job-id") + 1] == "JOB1"
    assert argv[argv.index("--from-step") + 1] == "render"
    assert argv[argv.index("--outdir") + 1] == "/out"
    assert argv[argv.index("--title") + 1] == "ヘミリイェチェパ"
    assert argv[-2:] == ["--silence-profile", "aggressive"]


# ───────── 텔롭 목록·프레임 그룹 (이식하며 뺀 순수 함수) ─────────

def test_only_broadcast_telops_is_the_index_contract():
    """L1·L2b·L3 공통 좌표 = broadcast_telop 만 추린 순번."""
    data = [{"kind": "our_subtitle"}, {"kind": "broadcast_telop", "text_ko": "가"},
            {"kind": "other"}, {"kind": "broadcast_telop", "text_ko": "나"}]
    assert [t["text_ko"] for t in telop.only_broadcast_telops(data)] == ["가", "나"]


def test_group_hits_splits_on_gap():
    assert telop.group_hits([1, 2, 3, 9, 10]) == [[1, 2, 3], [9, 10]]


def test_group_hits_tolerates_one_frame_dropout():
    """max_gap=2 — 한 프레임 놓쳐도 같은 구간으로 본다(페이드·잘림)."""
    assert telop.group_hits([4, 6, 7]) == [[4, 6, 7]]


def test_group_hits_empty():
    assert telop.group_hits([]) == []


# ───────── 텔롭 병기 트랙 ─────────

def test_build_telop_ass_uses_orig_index_and_skips_unused(tmp_path):
    """L2b 재보정본은 orig_index 로 번역과 짝을 맞춘다(필터로 순번이 밀리므로)."""
    refined = [{"orig_index": 2, "start_sec": 1.0, "end_sec": 2.0, "text_ko": "가"},
               {"orig_index": 5, "start_sec": 3.0, "end_sec": 4.0, "text_ko": "나"}]
    tr = {"telops": [{"index": 2, "use": True, "ja": "アガ"},
                     {"index": 5, "use": False, "ja": "ナ"}]}
    out = tmp_path / "telops.ass"
    assert loc_apply.build_telop_ass(refined, tr, "ArialUnicode", out) == 1
    body = out.read_text(encoding="utf-8")
    assert "アガ" in body and "ナ" not in body
    assert "0:00:01.00,0:00:02.00" in body


def test_build_telop_ass_filters_kind_when_not_refined(tmp_path):
    raw = [{"kind": "our_subtitle", "start_sec": 0.0, "end_sec": 1.0, "text_ko": "우리"},
           {"kind": "broadcast_telop", "start_sec": 1.0, "end_sec": 2.0, "text_ko": "텔롭"}]
    tr = {"telops": [{"index": 0, "use": True, "ja": "テロップ"}]}
    out = tmp_path / "t.ass"
    assert loc_apply.build_telop_ass(raw, tr, "ArialUnicode", out) == 1


def test_ass_escape_and_timestamp():
    assert loc_apply._ass_escape("a\nb") == "a\\Nb"
    assert loc_apply._ass_escape("{x}") == "(x)"          # ASS 오버라이드 블록 무력화
    assert loc_apply._fmt_ts(3661.5) == "1:01:01.50"


def test_build_telop_ass_line_style_and_timing_override(tmp_path):
    """줄 오버라이드: \\fs·\\1c(BGR)·\\frz(부호 반전)·y→MarginV, 타이밍 우선.

    태그는 _ass_escape 밖에서 조립돼야 한다(이스케이프가 { } 를 바꾼다)."""
    refined = [{"orig_index": 0, "start_sec": 1.0, "end_sec": 2.0, "text_ko": "가"}]
    tr = {"telops": [{"index": 0, "use": True, "ja": "テロップ{注}",
                      "style": {"size": 64, "color": "#FFDD00", "rotate": -8, "y": 0.5},
                      "start_sec": 3.5, "end_sec": 6.0}]}
    out = tmp_path / "telops.ass"
    assert loc_apply.build_telop_ass(refined, tr, "ArialUnicode", out) == 1
    line = next(ln for ln in out.read_text(encoding="utf-8").splitlines()
                if ln.startswith("Dialogue:"))
    assert "0:00:03.50,0:00:06.00" in line              # 사용자 타이밍이 L2b 값을 이긴다
    assert "\\fs64" in line and "\\1c&H00DDFF&" in line  # #FFDD00 → BGR 00DDFF
    assert "\\frz8" in line                              # 계약 -8(시계) → ASS +8(반시계)
    assert ",0,0,960,," in line                          # y=0.5 → MarginV (1-0.5)*1920
    assert "テロップ(注)" in line                        # 본문 { } 는 이스케이프, 태그는 생존
    assert line.index("\\fs64") < line.index("テロップ")


def test_build_telop_ass_no_style_keeps_legacy_line(tmp_path):
    refined = [{"orig_index": 0, "start_sec": 1.0, "end_sec": 2.0, "text_ko": "가"}]
    tr = {"telops": [{"index": 0, "use": True, "ja": "そのまま"}]}
    out = tmp_path / "t.ass"
    loc_apply.build_telop_ass(refined, tr, "ArialUnicode", out)
    line = next(ln for ln in out.read_text(encoding="utf-8").splitlines()
                if ln.startswith("Dialogue:"))
    assert ",0,0,0,, そのまま" in line                   # 이벤트 MarginV 0 = 스타일 기본(720)


def test_telop_ass_header_is_unchanged(tmp_path):
    """헤더 한 글자가 바뀌면 렌더 결과가 달라진다 — 이식 충실성 고정."""
    out = tmp_path / "t.ass"
    loc_apply.build_telop_ass([], {"telops": []}, "ArialUnicode", out)
    body = out.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in body and "PlayResY: 1920" in body
    assert ("Style: Telop,ArialUnicode,52,&H00FFFFFF,&H00000000,&H78000000,"
            "-1,0,3,5,0,2,70,70,720,1") in body


# ───────── 폰트 프로비저닝 (노드 실측: SIP 플래그로 죽었다) ─────────

def test_provision_fonts_copies_contents_only(monkeypatch, tmp_path):
    """copy2 는 macOS 시스템 폰트의 SIP 플래그를 chflags 로 옮기려다
    `PermissionError: Operation not permitted` 로 죽는다 — 내용만 복사해야 한다."""
    src = tmp_path / "sys.ttf"
    src.write_bytes(b"GLYPHS")
    fonts = tmp_path / "assets" / "fonts"
    monkeypatch.setattr(rerender, "SYSTEM_JP_FONT", src)
    monkeypatch.setattr(rerender, "FONTS_DIR", fonts)

    def no_copy2(*a, **k):                    # copy2 를 쓰면 테스트가 실패하도록
        raise AssertionError("copy2 는 메타데이터까지 옮겨 SIP 플래그에서 죽는다")

    monkeypatch.setattr(rerender.shutil, "copy2", no_copy2)
    rerender._provision_fonts({"title_font": "ArialUnicode"})
    assert (fonts / "ArialUnicode.ttf").read_bytes() == b"GLYPHS"


def test_provision_fonts_skips_when_already_present(monkeypatch, tmp_path):
    """운영 노드는 폰트가 이미 있어 이 경로를 안 탄다 — 그래서 여태 안 드러났다."""
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "ArialUnicode.ttf").write_bytes(b"OLD")
    monkeypatch.setattr(rerender, "FONTS_DIR", fonts)
    monkeypatch.setattr(rerender, "SYSTEM_JP_FONT", tmp_path / "does-not-exist.ttf")
    rerender._provision_fonts({"title_font": "ArialUnicode"})    # 안 죽는다
    assert (fonts / "ArialUnicode.ttf").read_bytes() == b"OLD"


def test_provision_fonts_ignores_other_font_names(monkeypatch, tmp_path):
    monkeypatch.setattr(rerender, "FONTS_DIR", tmp_path / "nope")
    monkeypatch.setattr(rerender, "SYSTEM_JP_FONT", tmp_path / "nope.ttf")
    rerender._provision_fonts({"title_font": "JalnanGothic"})     # 시스템 폰트를 안 찾는다
    assert not (tmp_path / "nope").exists()
