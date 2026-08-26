"""L-P5-발행 ① — 일본어 메타 초벌(overlay/meta.py).

이 파일이 지키는 것:
  ① vlp 프롬프트·필드·경고·© 라인이 그대로다(회귀 0 — 대조 가능해야 한다)
  ② 초벌이 **본편을 막지 않는다**(영상은 이미 만들어졌다)
  ③ 그러나 빈 초벌로는 발행할 수 없다(한국어 제목이 조용히 나가는 일 금지)
"""
import ast
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.localize.overlay import meta  # noqa: E402


# ── ① vlp 대조 ────────────────────────────────────────────────────────────
def test_prompt_and_fields_match_vlp():
    """프롬프트가 달라지면 번역 결과가 통째로 달라진다 — 이식은 글자까지 같아야 한다."""
    p = meta.build_prompt("원제", "설명", {"루피": "ルーピー"})
    assert "トランスクリエーション" in p
    assert "固定表記: 루피→ルーピー" in p
    assert '"title_candidates"' in p and '"title_candidates_ko"' in p
    assert "#残念ルーピー" in p and "#ルーピー" in p


def test_draft_carries_the_warning_and_copyright():
    d = meta.assemble_draft("v1", {"description": "本文", "title_candidates": ["A", "B"]},
                            meta.DEFAULT_COPYRIGHT)
    assert d["_warning"] == meta.WARNING          # 초벌임을 파일 안에 박는다
    assert d["description"].endswith(meta.DEFAULT_COPYRIGHT)
    assert d["copyright"] == meta.DEFAULT_COPYRIGHT


def test_copyright_is_not_doubled():
    """두 번 돌려도 © 줄이 두 번 붙지 않는다(재생성이 흔하다)."""
    once = meta.assemble_draft("v1", {"description": f"本文\n\n{meta.DEFAULT_COPYRIGHT}"},
                               meta.DEFAULT_COPYRIGHT)["description"]
    assert once.count(meta.DEFAULT_COPYRIGHT) == 1


def test_caps_are_kept():
    llm = {"title_candidates": list("ABCDE"), "tags": [f"t{i}" for i in range(30)],
           "description": "x"}
    d = meta.assemble_draft("v1", llm, "")
    assert len(d["title_candidates"]) == 3 and len(d["tags"]) == 20


def test_llm_object_survives_code_fences_and_chatter():
    got = meta.parse_llm_object('말머리\n```json\n{"a": 1}\n```\n꼬리')
    assert got == {"a": 1}


# ── ② 초벌 실패가 본편을 죽이지 않는다 ─────────────────────────────────────
def test_metadata_failure_does_not_fail_the_run():
    """🛑 영상은 수십 분짜리다. 메타 LLM 이 죽었다고 그것을 다시 돌리게 하면 안 된다."""
    src = pathlib.Path("app/localize/overlay/runner.py").read_text(encoding="utf-8")
    fn = src.split("def _metadata_draft(", 1)[1].split("\ndef ", 1)[0]
    assert "except Exception" in fn and "metadata_error" in fn
    assert "raise" not in fn


def test_run_overlay_only_makes_metadata_when_asked():
    """회귀 0 — `source_title` 이 없으면 LLM 호출 자체가 없다(종전 실행과 같다)."""
    src = pathlib.Path("app/localize/overlay/runner.py").read_text(encoding="utf-8")
    assert "if source_title:" in src


# ── ③ 빈 초벌로는 발행 못 한다 ─────────────────────────────────────────────
def test_publishable_requires_title_and_description():
    assert meta.publishable({"title_candidates": ["日本語タイトル"], "description": "本文"})
    assert not meta.publishable({"title_candidates": [], "description": "本文"})
    assert not meta.publishable({"title_candidates": ["  "], "description": "本文"})
    assert not meta.publishable({"title_candidates": ["t"], "description": "   "})
    assert not meta.publishable(None) and not meta.publishable("문자열")


# ── 이식 규율 ─────────────────────────────────────────────────────────────
def test_no_cli_entrypoint_in_the_port():
    """이 레포의 진입점은 app.cli 하나다(rerender·overlay 이식이 세운 규약)."""
    tree = ast.parse(pathlib.Path("app/localize/overlay/meta.py").read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "main" not in names and "_parse_args" not in names


def test_model_comes_from_the_repo_rule_not_the_config():
    """vlp config 의 금지 모델이 되살아나지 않는다 — resolve_model 이 정본이다."""
    src = pathlib.Path("app/localize/overlay/meta.py").read_text(encoding="utf-8")
    assert "llm.resolve_model(config, hero=hero)" in src
    assert "config.get(\"llm\"" not in src


def test_generate_writes_the_draft_next_to_the_outputs(tmp_path, monkeypatch):
    """산출 디렉토리 규약 — 오케스트레이터가 이 경로로 찾아 검수 카드에 싣는다."""
    cfg = {"paths": {"outputs_dir": str(tmp_path), "persona": None}, "translate": {}}
    monkeypatch.setattr(meta, "load_persona", lambda c: "ペルソナ")
    monkeypatch.setattr(meta, "load_glossary", lambda c: {})
    fake = type(sys)("app.localize.overlay.llm")
    fake.resolve_model = lambda c, hero=False: "gemini-3.6-flash"
    fake.provider = lambda c: "gemini"
    fake.complete = lambda *a, **k: json.dumps(
        {"title_candidates": ["日本語1", "日本語2"], "description": "本文",
         "tags": ["t"], "hashtags": ["#ルーピー"]})
    # ⚠ `from app.localize.overlay import llm` 는 **패키지 속성**을 먼저 본다 —
    # sys.modules 만 갈아끼우면 다른 테스트가 이미 임포트한 진짜 모듈이 이긴다
    # (단독 실행은 통과하고 전체 실행만 깨지는 부류).
    import app.localize.overlay as pkg
    monkeypatch.setattr(pkg, "llm", fake, raising=False)
    monkeypatch.setitem(sys.modules, "app.localize.overlay.llm", fake)
    out = meta.generate("vid1", "원제", "", cfg, out_path=str(tmp_path / "vid1" / "metadata_draft.json"))
    got = json.loads(pathlib.Path(out).read_text(encoding="utf-8"))
    assert got["video_id"] == "vid1" and meta.publishable(got)
