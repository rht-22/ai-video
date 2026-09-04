"""V4-M1 합격선 — AST 가드 + 승계 체크리스트 + 계약 문서 대조.

계약 정본 `docs/v4/M2-interfaces.md` §5. 배경은 기획서 `docs/v4/v4-plan.md` §5.

🛑 **이 파일이 있는 이유는 v3 의 사고다.** v3 는 "공유 모듈 재사용"을 선언하고 v1
모놀리스를 **한 줄도 import 하지 않았다**. 그 결과 E10(영상 밴드)·E12(TTS 캐시)·
E14(자막 노출 하한)·E18-2(구간별 번인 회피)·E19·E20·E21 대응을 **하나도 승계하지
못했다** — 선언은 사람이 읽고, 배선은 기계가 읽는다. 선언과 배선이 갈리면 아무도
모르는 채로 몇 달이 간다.

⇒ 이 가드가 셋을 기계로 못박는다:

  ① **AST 가드** — `app/v4/*` 가 모놀리스(`app.pipeline`)나 v3 배선(`app.v3.pipeline`)
     을 부르지 않는다. 그리고 `from_step` 판정을 손으로 적지 않는다.
  ② **승계 체크리스트**(`ABSORB_TABLE`) — v4 가 흡수할 동작의 **주소** 26개가 실존하고
     호출 가능한가. v3 는 동결되지만 **라이브러리로 남는다**(결정 4) — 그래서 표에
     `app.v3.*` 가 있는 것이 의도다. 진짜 흡수(프롬프트 분해·통합 스키마)는 M3·M7 의
     일이고, 이 파일은 그때까지 **주소가 살아 있는지**만 지킨다.
  ③ **계약 문서 ↔ 코드 대조** — `docs/v4/M1-interfaces.md`·`M2-interfaces.md` 가 이름을
     적어 둔 함수·상수가 실제로 있는가. 문서가 코드를 앞서가면 다음 사람이 없는 함수를
     부른다.

⚠ 이 파일에 **없는** 검사(중복이라 뺐다 — Wave 1 이 이미 한다):
  · 승격 6종이 `app.v3.<name>` 과 `app.modules.grid.<name>` 에서 같은 객체인가
    → `tests/test_v4_grid_promote.py::test_v3_path_is_the_same_module_object`
  · 추출된 v1 함수가 모놀리스와 같은 객체인가
    → `tests/test_v4_v1_extract.py::test_monolith_reexports_the_same_object`
  · 기획서 §2 단계 표 ↔ `app/v4/steps.py` 1:1
    → `tests/test_v4_steps.py::test_v4_steps_matches_plan_table_one_to_one`

실패 메시지는 **무엇을 어떻게 고칠지**까지 적는다 — 이 가드가 걸릴 때 사람은 급하다.
"""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
V4_DIR = REPO_ROOT / "app" / "v4"
DOCS = (REPO_ROOT / "docs" / "v4" / "M1-interfaces.md",
        REPO_ROOT / "docs" / "v4" / "M2-interfaces.md")


# ═══════════════════════════════════════════════════════════════════════════
# 공통 — 소스 수집과 AST 훑기
# ═══════════════════════════════════════════════════════════════════════════

def _v4_sources() -> list[Path]:
    """`app/v4/` 의 파이썬 소스 전량(정렬 — 실패 메시지가 결정적이어야 한다).

    ⚠ 목록을 손으로 적지 않는다. Wave 2 가 `pipeline.py`·`proxy.py`·`cli.py` 를
    올리는 중이고, 손으로 적은 목록은 **새로 생긴 파일을 조용히 빼놓는다** — 가드가
    가장 필요한 파일이 정확히 그 새 파일이다."""
    return sorted(p for p in V4_DIR.glob("*.py") if p.name != "__pycache__")


def _dotted(path: Path) -> str:
    """레포 상대 경로 → 점 표기 모듈명. `app/v4/x.py` → `app.v4.x`."""
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_of(path: Path) -> str:
    """상대 import 의 기준 패키지. `app/v4/x.py`·`app/v4/__init__.py` 둘 다 `app.v4`."""
    if path.name == "__init__.py":
        return _dotted(path)
    return _dotted(path).rsplit(".", 1)[0]


def imported_modules(source: str, *, package: str) -> list[tuple[str, int]]:
    """소스 하나가 import 하는 **모듈 전량** → [(점 표기, 줄번호), …].

    🛑 `ast.walk` 로 훑는 이유(P4 교훈): 파일 단위 이식이 **함수 안 지연 import** 를
    놓쳐 런타임에 죽은 사고가 이 레포에 3건 있다(CLAUDE.md 「이식 중 잡은 결함 3건」 —
    `dub.py` 의 `from engine import render`, `-m src.dub` 자기 재호출, 안 옮긴
    `src/refbank.py`). 셋 다 문법 검사·모듈 최상단 검사로는 안 잡혔다. 실제로 지금도
    `app/v4/verify.py` 는 `try/except` **안에서** grid 를 import 한다 — 최상단만 보는
    가드는 그 줄을 못 본다.

    수집 규칙:
    - `import a.b` → `a.b`
    - `from a.b import c` → `a.b` **와** `a.b.c` 둘 다.
      `from app import pipeline` 형태를 잡으려면 후자가 필요하다 — 모듈명만 보면
      `app` 이라 금지 목록을 그냥 지나간다.
    - `from . import x` / `from ..y import z` → 기준 패키지로 절대화.
      상대 import 를 안 풀면 `from ..pipeline import …`(= `app.pipeline`)가 샌다.

    순수 — 넘겨받은 소스를 건드리지 않는다."""
    tree = ast.parse(source)
    parts = package.split(".") if package else []
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # level=1 은 자기 패키지, 2 는 그 부모… 파이썬 규칙 그대로.
                keep = len(parts) - (node.level - 1)
                base = ".".join(parts[:keep]) if keep > 0 else ""
            else:
                base = ""
            head = ".".join(x for x in (base, node.module or "") if x)
            if head:
                out.append((head, node.lineno))
            for alias in node.names:
                if alias.name == "*":
                    continue
                full = ".".join(x for x in (head, alias.name) if x)
                if full:
                    out.append((full, node.lineno))
    return out


def _hits(dotted: str, forbidden: str) -> bool:
    """`app.pipeline` 은 `app.pipeline` 자신과 그 하위만 잡는다 —
    `app.pipeline_utils` 같은 이름이 접두사 일치로 오폭하면 안 된다."""
    return dotted == forbidden or dotted.startswith(forbidden + ".")


# ── 수집기 자가 검증 ────────────────────────────────────────────────────────
# 가드보다 수집기를 먼저 고정한다. 수집기가 조용히 0건을 돌려주면 아래 금지 검사가
# **전부 통과해 버린다** — 가드가 있는데 아무것도 안 보는 상태가 제일 나쁘다.

def test_collector_sees_deferred_imports_inside_functions():
    """함수 안 지연 import 를 본다 — 이걸 못 보면 P4 사고가 그대로 재현된다."""
    src = ("def f():\n"
           "    from app.pipeline import clips_beyond_source\n"
           "    return clips_beyond_source\n")
    found = {name for name, _ in imported_modules(src, package="app.v4")}
    assert "app.pipeline" in found, "함수 안 import 를 놓쳤다 — ast.walk 가 아니라 최상단만 보고 있다"


def test_collector_sees_imports_inside_try_and_class_bodies():
    """try/except·클래스 본문도 최상단이 아니다. `app/v4/verify.py` 가 실제로 쓰는 형태."""
    src = ("try:\n"
           "    import app.v3.pipeline\n"
           "except ImportError:\n"
           "    class C:\n"
           "        import app.pipeline\n")
    found = {name for name, _ in imported_modules(src, package="app.v4")}
    assert {"app.v3.pipeline", "app.pipeline"} <= found


def test_collector_resolves_relative_imports():
    """`from ..pipeline import x` 는 `app.pipeline` 이다 — 절대화를 안 하면 샌다."""
    found = {n for n, _ in imported_modules("from ..pipeline import x\n", package="app.v4")}
    assert "app.pipeline" in found
    found = {n for n, _ in imported_modules("from . import steps\n", package="app.v4")}
    assert "app.v4.steps" in found


def test_collector_sees_from_package_import_module_form():
    """`from app import pipeline` — 모듈명만 보면 `app` 이라 금지 목록을 지나간다."""
    found = {n for n, _ in imported_modules("from app import pipeline\n", package="app.v4")}
    assert "app.pipeline" in found


def test_collector_does_not_overreach_on_prefixes():
    """`app.pipeline_utils` 는 `app.pipeline` 이 아니다 — 오폭하는 가드는 아무도 안 본다."""
    assert _hits("app.pipeline", "app.pipeline")
    assert _hits("app.pipeline.helpers", "app.pipeline")
    assert not _hits("app.pipeline_utils", "app.pipeline")


def test_scan_target_is_not_empty():
    """훑을 파일이 0개면 아래 금지 검사가 공짜로 통과한다."""
    files = _v4_sources()
    assert files, f"{V4_DIR} 에 파이썬 소스가 없다 — glob 이 깨졌거나 경로가 틀렸다"
    names = {p.name for p in files}
    assert "steps.py" in names, f"단계 표가 안 보인다 — 훑은 파일: {sorted(names)}"


# ═══════════════════════════════════════════════════════════════════════════
# ① AST 가드 — 모놀리스·v3 배선 import 금지
# ═══════════════════════════════════════════════════════════════════════════

# `app.pipeline` = v1 모놀리스(7,000줄). v4 는 그 안의 함수를 **`app/modules/` 로 추출된
# 것**으로 부른다(계약 §7 승격·추출). 모놀리스를 통째로 import 하면 v1 전역 상태·무거운
# 의존이 함께 딸려오고, v1 이 은퇴할 때 v4 가 같이 끊긴다.
# `app.v3.pipeline` = v3 배선. v4 의 배선은 v4 것이다(계약 §1) — v3 는 **라이브러리로만**
# 남는다(결정 4). 배선을 부르면 v3 의 `--from-step` 판정 7종을 그대로 물려받는다.
FORBIDDEN_IMPORTS = ("app.pipeline", "app.v3.pipeline")


def test_v4_does_not_import_the_monoliths():
    """`app/v4/*` 는 `app.pipeline`·`app.v3.pipeline` 을 import 하지 않는다.

    ⚠ 텍스트 grep 이 아니라 AST 다 — v4 소스의 **주석**은 모놀리스 줄번호를 근거로
    인용한다(`app/v4/approve.py` 가 `app/pipeline.py:3269` 를 인용한다). grep 가드는
    그 주석을 오폭해서, 사람이 근거 주석을 지우게 만든다."""
    violations: list[str] = []
    for path in _v4_sources():
        source = path.read_text(encoding="utf-8")
        pkg = _package_of(path)
        for dotted, lineno in imported_modules(source, package=pkg):
            for forbidden in FORBIDDEN_IMPORTS:
                if _hits(dotted, forbidden):
                    rel = path.relative_to(REPO_ROOT)
                    violations.append(f"  {rel}:{lineno} — import {dotted}")
    assert not violations, (
        "app/v4 가 모놀리스·v3 배선을 import 한다:\n"
        + "\n".join(sorted(violations))
        + "\n\n고치는 법:\n"
        "  · `app.pipeline` 의 함수가 필요하면 `app/modules/` 로 **추출**한 뒤 그쪽을\n"
        "    부른다(계약 docs/v4/M1-interfaces.md §7 표). 이미 추출된 것:\n"
        "      clips_beyond_source → app.modules.clip_guard\n"
        "      resolve_cue_anchors · snap_cues_to_dialogue_gaps → app.modules.cues\n"
        "  · `app.v3.pipeline` 의 배선은 v4 가 다시 짓는다. v3 의 **판단 함수**\n"
        "    (story·assemble·finalize…)는 부를 수 있다 — 아래 ABSORB_TABLE 참조."
    )


# ── from_step 판정은 should_run 하나뿐 ──────────────────────────────────────
# 🛑 v3 는 "이 단계부터 다시"를 단계마다 **손으로 적은 멤버십 검사**로 했다(조사
# gotcha 4 · app/v4/steps.py 독스트링에 7곳의 실물 줄번호가 있다):
#     from_step == "grid"                                (v3 pipeline.py:259)
#     from_step not in ("grid", "seq_analyze")           (:353)
#     from_step not in ("chunk_split", "chunk_analyze")  (:490)
#     …
# 집합이 전부 달라서 `--from-step resources` 가 **상류인 story 를 무효화**하는 등
# 사람의 직관과 갈렸다. v4 의 규칙은 `steps.should_run` 순번 비교 하나다.
#
# 예외는 `steps.py` 하나 — 거기가 규칙 그 자체이고, 정본 표(STEP_ORDER·STEP_ALIASES)
# 에 대한 멤버십 검사가 곧 `parse_from_step` 의 구현이다.
FROM_STEP_GUARD_EXEMPT = {"steps.py"}


def _names_in(node: ast.AST) -> set[str]:
    """식 안에 쓰인 이름·속성명 전량(`args.from_step` 의 `from_step` 도 잡는다)."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
    return out


def from_step_comparisons(source: str) -> list[tuple[str, int]]:
    """`from_step` 을 좌변에 둔 비교 전량 → [(연산자 이름, 줄번호), …].

    잡는 것은 v3 가 실제로 저지른 두 형태다:
      · 멤버십 — `from_step in (...)` · `from_step not in (...)`
      · 동치   — `from_step == "grid"` · `!=`  (v3 pipeline.py:259 가 이 형태다)

    `from_step is None` 은 잡지 않는다 — '미지정인가'는 단계 판정이 아니라 배선의
    정상 분기다(계약 §1: from_step=None 이면 전부 돈다)."""
    tree = ast.parse(source)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if "from_step" not in _names_in(node.left):
            continue
        for op in node.ops:
            if isinstance(op, (ast.In, ast.NotIn)):
                out.append(("in" if isinstance(op, ast.In) else "not in", node.lineno))
            elif isinstance(op, (ast.Eq, ast.NotEq)):
                out.append(("==" if isinstance(op, ast.Eq) else "!=", node.lineno))
    return out


def test_from_step_comparison_detector_catches_the_v3_forms():
    """탐지기를 먼저 고정한다 — 탐지기가 0건을 돌려주면 가드가 공짜로 통과한다.

    v3 의 실물 7곳을 그대로 재현해 넣는다."""
    src = ("if from_step == 'grid':\n    pass\n"
           "if from_step not in ('grid', 'seq_analyze'):\n    pass\n"
           "if args.from_step in ('story', 'resources'):\n    pass\n")
    found = {op for op, _ in from_step_comparisons(src)}
    assert found == {"==", "not in", "in"}, f"탐지기가 v3 형태를 놓쳤다: {found}"


def test_from_step_comparison_detector_ignores_the_none_check():
    """`from_step is None` 은 정상 분기다 — 오폭하면 배선이 가드를 우회하게 된다."""
    assert from_step_comparisons("if from_step is None:\n    pass\n") == []


def test_v4_never_hand_writes_from_step_membership():
    """`app/v4/*`(steps.py 제외)에 손으로 적은 `from_step` 판정이 **한 건도 없다**.

    계약 §5 는 `app/v4/pipeline.py` 를 지목하지만, 훑는 대상은 v4 소스 **전량**이다 —
    파일 하나를 지목하면 판정이 옆 파일로 옮겨간 순간 가드가 눈을 감는다(v3 는 판정이
    7개 파일 위치로 흩어져서 아무도 전체를 못 봤다)."""
    violations: list[str] = []
    for path in _v4_sources():
        if path.name in FROM_STEP_GUARD_EXEMPT:
            continue
        for op, lineno in from_step_comparisons(path.read_text(encoding="utf-8")):
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"  {rel}:{lineno} — `from_step {op} …`")
    assert not violations, (
        "손으로 적은 from_step 판정이 있다 (v3 gotcha 4 의 재발):\n"
        + "\n".join(sorted(violations))
        + "\n\n고치는 법: `app.v4.steps.should_run(step, from_step)` 하나만 쓴다.\n"
        "  단계 이름 정규화가 필요하면 `steps.parse_from_step`.\n"
        "  '캐시를 실제로 재사용하는가'는 별개다 — 그건 각 단계가 **상류 지문**으로\n"
        "  본다(app/modules/job.py 의 fingerprint). from_step 을 다시 비교하지 마라."
    )


def test_pipeline_module_is_covered_when_it_lands():
    """계약 §5 가 지목한 `app/v4/pipeline.py` 가 생기면 위 두 가드가 자동으로 훑는가.

    Wave 2 가 배선을 올리는 중이라 지금은 없을 수 있다. 파일이 생겼는데 목록에서
    빠지는 일(오타·glob 실수)만 잡는다."""
    pipeline_py = V4_DIR / "pipeline.py"
    if not pipeline_py.exists():
        pytest.skip("app/v4/pipeline.py 아직 없음(M2 배선) — 생기면 이 검사가 켜진다")
    assert pipeline_py in _v4_sources()


# ═══════════════════════════════════════════════════════════════════════════
# ② 승계 체크리스트 — M9~M15 + v1 E-항목
# ═══════════════════════════════════════════════════════════════════════════

# (모듈 경로, 이름, 이걸 **실제로 부르기 시작하는** 마일스톤)
#
# 계약 `docs/v4/M2-interfaces.md` §5 의 표 그대로다(아래 대조 테스트가 문서와 묶는다).
#
# ⚠ 표에 `app.v3.*` 가 있는 것은 **의도다**. v3 는 동결되지만 **라이브러리로 남는다**
#   (결정 4) — v4 는 v3 의 판단 함수(story·assemble·finalize·refine·stage4·textcheck)를
#   베끼지 않고 **부른다**. 7,035줄을 베끼면 언젠가 한쪽만 고쳐지기 때문이다(M1 계약 §0).
#   진짜 흡수(프롬프트 분해·통합 스키마)는 **M3·M7 의 일**이고, 그때까지 이 표는
#   "주소가 살아 있는가"만 지킨다.
#
# ⚠ 마일스톤 열은 **문서화**다 — "언제부터 실제로 부르기 시작하는가". M1 시점에는 아직
#   부르는 단계가 하나도 없어서 호출을 강제할 수 없다. 그래서 지금 고정하는 것은
#   `import 가능 + 호출 가능`이고, 실제 호출은 그 마일스톤이 켠다(계약 §5 원문).
ABSORB_TABLE: tuple[tuple[str, str, str], ...] = (
    ("app.modules.clip_guard", "clips_beyond_source", "M3"),
    ("app.modules.cues", "resolve_cue_anchors", "M6"),
    ("app.modules.cues", "snap_cues_to_dialogue_gaps", "M6"),
    ("app.modules.grid.timegrid", "carve_spans", "M2"),
    ("app.modules.grid.transcribe", "transcribe_words", "M2"),
    ("app.modules.grid.transcribe", "retranscribe_gaps", "M2"),
    ("app.modules.timestamp_check", "find_quote_times", "M3"),
    ("app.v3.textcheck", "check_names", "M6"),
    ("app.v3.textcheck", "drop_repetition", "M6"),
    ("app.v3.assemble", "word_subtitles", "M6"),
    ("app.v3.assemble", "narration_windows", "M6"),
    ("app.v3.assemble", "split_by_windows", "M6"),
    ("app.v3.assemble", "speaker_colors", "M6"),
    ("app.v3.story", "plan_narration_slots", "M5"),
    ("app.v3.story", "verify_tts_conflicts", "M5"),
    ("app.v3.story", "build_span_index", "M3"),
    ("app.v3.refine", "boundary_probe_windows", "M3"),
    ("app.v3.stage4", "run_style", "M7"),
    ("app.v3.finalize", "plan_labels", "M7"),
    ("app.v3.finalize", "place_above_burned", "M7"),
    ("app.v3.finalize", "resolve_work_logo", "M7"),
    ("app.v3.finalize", "fit_title_sizes", "M7"),
    ("app.v3.finalize", "subtitle_fx_windows", "M7"),
    ("app.v3.finalize", "run_validate", "M7"),
    ("app.modules.subtitle_region", "runs_in_window", "M7"),
    ("app.modules.style_compose", "title_windows_owner", "M7"),
)

# 마일스톤 어휘 — 오타(`m3`·`M03`)가 들어오면 "언제 켜지는가"가 정렬 불가가 된다.
KNOWN_MILESTONES = {"M2", "M3", "M5", "M6", "M7"}


@pytest.mark.parametrize("module_path,name,milestone", ABSORB_TABLE,
                         ids=[f"{m.rsplit('.', 1)[-1]}.{n}" for m, n, _ in ABSORB_TABLE])
def test_absorb_targets_all_exist(module_path: str, name: str, milestone: str):
    """M9~M15 승계 체크리스트 — 이 이름들이 v4 가 흡수할 동작의 **주소**다.

    v3 동결 중에 누가 지우거나 이름을 바꾸면 여기서 잡힌다. 이름이 바뀐 것을 모른 채
    M3~M7 에 도달하면, 그 마일스톤이 "재사용한다"고 적고는 v3 처럼 **아무것도 안 부르는**
    코드를 낳는다 — 그게 정확히 이 파일이 막으려는 사고다."""
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 주소가 죽은 것이다
        pytest.fail(
            f"{module_path} 를 import 할 수 없다 ({type(exc).__name__}: {exc})\n"
            f"  이 모듈은 {milestone} 이 {name}() 를 부르기로 한 곳이다.\n"
            f"  모듈을 옮겼다면 ABSORB_TABLE 의 주소를 함께 고쳐라 "
            f"(계약 docs/v4/M2-interfaces.md §5 표도)."
        )
    target = getattr(module, name, None)
    assert target is not None, (
        f"{module_path}.{name} 이 없다 — {milestone} 이 부르기로 한 함수다.\n"
        f"  이름을 바꿨다면 ABSORB_TABLE 과 계약 문서 §5 표를 함께 고쳐라.\n"
        f"  지웠다면: 그 동작을 v4 가 어떻게 승계하는지 먼저 정하고 지워라 —\n"
        f"  v3 는 이 확인을 안 해서 E10·E14·E18-2 를 통째로 잃었다."
    )
    assert callable(target), (
        f"{module_path}.{name} 이 호출 가능하지 않다(지금은 {type(target).__name__}).\n"
        f"  {milestone} 이 함수로 부를 계획이라 상수·클래스로 바뀌면 그때 죽는다."
    )


def test_absorb_table_has_no_duplicate_addresses():
    """같은 주소가 두 번 있으면 하나를 지울 때 다른 하나가 남아 가드가 반만 산다."""
    addrs = [(m, n) for m, n, _ in ABSORB_TABLE]
    dupes = sorted({a for a in addrs if addrs.count(a) > 1})
    assert not dupes, f"ABSORB_TABLE 에 중복 주소: {dupes}"


def test_absorb_table_milestones_are_known():
    """마일스톤 어휘 고정 — 오타가 들어오면 '언제 켜지는가'를 기계로 못 센다."""
    bad = sorted({ms for _, _, ms in ABSORB_TABLE if ms not in KNOWN_MILESTONES})
    assert not bad, (
        f"모르는 마일스톤 표기: {bad} — 허용: {sorted(KNOWN_MILESTONES)}\n"
        f"  새 마일스톤이라면 KNOWN_MILESTONES 에 근거와 함께 더해라."
    )


def _absorb_table_from_contract() -> tuple[tuple[str, str, str], ...]:
    """계약 문서 §5 의 `ABSORB_TABLE` 리터럴을 그대로 읽는다.

    문서의 표는 파이썬 리터럴로 적혀 있어 `ast.literal_eval` 이 그대로 먹는다 —
    사람이 옮겨 적는 순간 둘이 갈리므로 기계가 대조한다."""
    text = (REPO_ROOT / "docs" / "v4" / "M2-interfaces.md").read_text(encoding="utf-8")
    match = re.search(r"^ABSORB_TABLE[^=]*=\s*(\(.*?\n\))\s*$", text, re.S | re.M)
    assert match, "계약 문서 §5 에서 ABSORB_TABLE 리터럴을 못 찾았다 — 문서 형식이 바뀌었나?"
    return tuple(tuple(row) for row in ast.literal_eval(match.group(1)))


def test_absorb_table_matches_the_contract_document():
    """코드의 표 = 계약 문서 §5 의 표. 순서까지 같아야 한다.

    한쪽만 고치면 다음 사람이 문서를 믿고 없는 주소를 부른다 — v3 가 "재사용한다"고
    적어 놓고 안 부른 것과 같은 종류의 거짓말이다."""
    doc_table = _absorb_table_from_contract()
    assert doc_table == ABSORB_TABLE, (
        "ABSORB_TABLE 이 계약 문서(docs/v4/M2-interfaces.md §5)와 다르다.\n"
        f"  문서에만 있는 행: {sorted(set(doc_table) - set(ABSORB_TABLE))}\n"
        f"  코드에만 있는 행: {sorted(set(ABSORB_TABLE) - set(doc_table))}\n"
        "  둘을 같이 고쳐라(어느 쪽이 옳은지는 사람이 정한다)."
    )


# ═══════════════════════════════════════════════════════════════════════════
# ③ 계약 문서 ↔ 코드 대조
# ═══════════════════════════════════════════════════════════════════════════

# 계약 문서가 이름을 적었지만 **아직 안 지은** 모듈. 값은 사유(언제 누가 짓는가).
#
# ⚠ 이 목록은 '없어도 된다'는 뜻이지 '검사 안 한다'가 아니다 — 파일이 생기는 순간
#   아래 검사가 자동으로 그 모듈의 이름 전량을 대조한다(목록에서 뺄 필요 없다).
#   반대로 여기 없는 모듈이 문서에만 있으면 **크게 실패**한다: 문서가 코드를 앞서가면
#   다음 사람이 없는 함수를 부른다.
DOC_PENDING_MODULES: dict[str, str] = {
    "app.v4.pipeline": "M2 배선(계약 M2 §1) — Wave 2 가 짓는 중",
    "app.v4.proxy": "M2 프록시·업로드(계약 M2 §3) — Wave 2 가 짓는 중",
    "app.v4.cli": "M2 진입점(계약 M2 §6) — Wave 2 가 짓는 중",
}

_DOC_DEF = re.compile(r"^def\s+([A-Za-z_]\w*)\s*\(")
_DOC_CONST = re.compile(r"^([A-Z][A-Z0-9_]*)\s*[:=]")
_DOC_MODULE_PATH = re.compile(r"`((?:app|scripts|tests)/[A-Za-z0-9_/]+\.py)`")


def contract_declarations(doc: Path) -> dict[str, set[str]]:
    """계약 문서 → {모듈 경로: 그 문서가 약속한 최상위 이름들}.

    귀속은 **제목 줄**(`## 4. 구간 검증 — \\`app/v4/verify.py\\``)의 모듈 경로로 한다.
    본문 아무 데나 나온 마지막 경로로 귀속하면 §4 처럼 "그 모듈은 옮기지 않고 **부른다**"
    며 다른 경로(`app/modules/timestamp_check.py`)를 인용한 절에서 통째로 어긋난다
    (실측 — 첫 판이 정확히 그렇게 틀렸다).

    코드블록은 시그니처만 적힌 의사코드라 `ast.parse` 가 안 먹는다(`…`·본문 생략).
    그래서 **열 0의 `def`/대문자 상수**만 줄 단위로 걷는다 — 들여쓴 것(튜플 원소·중첩
    함수)은 최상위 이름이 아니므로 걷지 않는 것이 맞다."""
    out: dict[str, set[str]] = {}
    current: str | None = None
    in_block = False
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            if line.startswith("#"):
                found = _DOC_MODULE_PATH.findall(line)
                current = found[0] if found else None
            continue
        if current is None:
            continue
        m = _DOC_DEF.match(line) or _DOC_CONST.match(line)
        if m:
            out.setdefault(current, set()).add(m.group(1))
    return out


def test_contract_parser_finds_the_known_sections():
    """파서를 먼저 고정한다 — 0건을 뽑으면 아래 대조가 공짜로 통과한다."""
    m1 = contract_declarations(DOCS[0])
    assert "app/v4/steps.py" in m1 and {"V4_STEPS", "should_run"} <= m1["app/v4/steps.py"]
    # §4 의 제목은 verify.py 인데 본문이 timestamp_check.py 를 인용한다 — 제목 귀속의 근거.
    assert "app/v4/verify.py" in m1 and "verify_candidate" in m1["app/v4/verify.py"]
    assert "verify_candidate" not in m1.get("app/modules/timestamp_check.py", set())
    m2 = contract_declarations(DOCS[1])
    assert m2.get("app/v4/pipeline.py") == {"run_v4"}


@pytest.mark.parametrize("doc", DOCS, ids=[d.name for d in DOCS])
def test_contract_documents_do_not_run_ahead_of_the_code(doc: Path):
    """계약 문서가 이름을 적은 함수·상수가 실제로 있는가.

    문서가 코드를 앞서가면 다음 사람이 **없는 함수를 부른다**. 반대로 코드가 문서보다
    많은 것은 잡지 않는다 — 문서는 계약이지 목록이 아니다(내부 헬퍼까지 적을 이유가 없다).

    `tests/`·`scripts/` 경로는 대조 대상이 아니다: §5(이 파일 자신)·§4(대조 도구)는
    문서가 **구현 예시**를 싣는 절이라 이름이 1:1 일 이유가 없다."""
    problems: list[str] = []
    for rel_path, names in sorted(contract_declarations(doc).items()):
        if not rel_path.startswith("app/"):
            continue
        dotted = rel_path[: -len(".py")].replace("/", ".").removesuffix(".__init__")
        try:
            module = importlib.import_module(dotted)
        except ModuleNotFoundError:
            why = DOC_PENDING_MODULES.get(dotted)
            if why is None:
                problems.append(
                    f"  {rel_path} — 문서가 {sorted(names)} 를 약속했는데 모듈이 없다.\n"
                    f"    지었으면 경로를 맞추고, 아직이면 DOC_PENDING_MODULES 에\n"
                    f"    '누가 언제 짓는가'와 함께 등록해라(조용한 스킵 금지)."
                )
            continue
        missing = sorted(n for n in names if not hasattr(module, n))
        if missing:
            problems.append(
                f"  {rel_path} — 문서에만 있는 이름: {missing}\n"
                f"    문서가 앞서갔거나(구현 필요) 이름이 바뀌었다(문서 수정 필요)."
            )
    assert not problems, f"{doc.name} 이 코드와 어긋난다:\n" + "\n".join(problems)


def test_pending_modules_are_only_the_unbuilt_ones():
    """`DOC_PENDING_MODULES` 는 '아직 없다'는 사실의 기록이다.

    이미 있는 모듈이 목록에 남아 있어도 위 대조는 그 모듈을 **하드 검사**한다(목록은
    ModuleNotFoundError 일 때만 본다). 그래서 이 검사는 청소 알림이지 차단이 아니다 —
    다만 사유가 비어 있으면 다음 사람이 왜 유예됐는지 못 읽으므로 그건 막는다."""
    for dotted, why in DOC_PENDING_MODULES.items():
        assert why.strip(), f"{dotted} 의 유예 사유가 비었다 — 누가 언제 짓는지 적어라"


# ── 추출한 v1 함수는 **추출된 자리에서** 부른다 ─────────────────────────────
# 계약 `docs/v4/M1-interfaces.md` §7 표(물리 이동). 모놀리스는 재수출 껍데기로 남아
# 있어서 `app.pipeline.clips_beyond_source` 로도 같은 객체가 잡힌다 — 그래서 "이름을
# 쓰는가"만 보면 v4 가 모놀리스 경로로 부르는 것을 못 잡는다. 부르는 **출처**를 본다.
EXTRACTED_V1_FUNCTIONS: dict[str, str] = {
    "clips_beyond_source": "app.modules.clip_guard",
    "resolve_cue_anchors": "app.modules.cues",
    "snap_cues_to_dialogue_gaps": "app.modules.cues",
}


def test_v4_calls_the_extracted_v1_functions():
    """추출했다고 끝이 아니다 — v3 는 '재사용'을 선언하고 **한 줄도 부르지 않았다**.

    M1 시점에는 아직 부르는 단계가 없다(각각 M3·M6 이 켠다 — ABSORB_TABLE 참조).
    그래서 지금 고정하는 것은 둘이다:
      ① 부를 대상이 실존하고 import 가능한가 → 위 `test_absorb_targets_all_exist`
      ② v4 가 그 이름을 쓰기 시작하는 순간, **추출된 모듈**에서 가져오는가 → 여기.

    ②를 지금 걸어 두는 이유: 나중에 배선을 쓰는 사람은 `app.pipeline` 에도 같은 함수가
    (재수출 껍데기로) 있다는 사실을 모른 채 그쪽을 부르기 쉽다. 그러면 v1 이 은퇴할 때
    v4 가 함께 끊긴다 — `test_v4_does_not_import_the_monoliths` 가 그 import 를 막지만,
    이 검사는 '어디서 가져와야 하는가'를 이름 단위로 말해 준다."""
    used_from: dict[str, list[str]] = {}   # 이름 → ["파일:줄 (출처)", …]
    for path in _v4_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        rel = path.relative_to(REPO_ROOT)
        pkg = _package_of(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            head = ".".join(x for x in (
                (".".join(pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)])
                 if node.level else ""),
                node.module or "",
            ) if x)
            for alias in node.names:
                if alias.name in EXTRACTED_V1_FUNCTIONS:
                    used_from.setdefault(alias.name, []).append(
                        f"{rel}:{node.lineno} ({head})")

    wrong = [
        f"  {name}: {where} — 정본은 {EXTRACTED_V1_FUNCTIONS[name]}"
        for name, places in sorted(used_from.items())
        for where in places
        if f"({EXTRACTED_V1_FUNCTIONS[name]})" not in where
    ]
    assert not wrong, (
        "추출한 v1 함수를 엉뚱한 자리에서 가져온다:\n" + "\n".join(wrong)
        + "\n\n모놀리스는 재수출 껍데기라 그쪽에서도 '되긴 한다' — 그래서 더 위험하다.\n"
        "  계약 docs/v4/M1-interfaces.md §7 표의 추출 자리에서 가져와라."
    )
