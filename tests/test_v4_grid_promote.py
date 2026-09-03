"""V4-M1 §7 승격 회귀 가드 — 격자 재료 6종의 `app/modules/grid/` 이주.

승격의 목적은 하나다: **v3 가 동결돼도 v4 가 끊기지 않는 것**(v4 기획 결정 4).
그래서 이 파일이 고정하는 것도 셋뿐이다.

① 정본이 `app/modules/grid/` 에 **하나만** 있다 — 베낀 사본이 아니라 이동이다.
   `app.v3.<name>` 은 그 모듈과 **같은 객체**여야 한다(`is`). 사본이 되는 순간
   언젠가 한쪽만 고쳐진다(이 레포가 L-P1·P4 이식에서 반복해 배운 것).
② 껍데기가 **아무 이름도 빠뜨리지 않는다** — 공개 이름은 정본 소스를 AST 로 훑어
   대조하고, 밖에서 부르는 비공개 이름은 호출처를 적어 명시 고정한다.
   그리고 **monkeypatch 가 살아 있다** — 이것이 `from … import *` 껍데기를
   실측으로 기각시킨 조항이다(그 방식으로 v3 테스트 5건이 깨졌다).
③ 격자 모듈은 **leaf 다** — `app.v3`·`app.pipeline` 을 import 하지 않는다.
   여기가 깨지면 승격이 값어치를 잃는다(v3 을 지우는 날 v4 도 같이 죽는다).

⚠ 여기서 v3 산출물의 동등성 자체는 안 잰다 — 그건 `tests/test_v3_*.py` 전량이
이미 하고 있고, 승격의 합격 조건이 바로 "그 1,900여 건이 그대로 통과"다.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

import app.modules.grid as grid
import app.v3 as v3

# 승격 대상 6종. 순서는 계약(M1-interfaces.md §7) 표 그대로.
PROMOTED = ("schemas", "timegrid", "scenecut", "audio", "arousal", "transcribe")

# 계약 §7 이 "패키지 수준에 올린다"고 못박은 이름들. 부르는 쪽이
# `from app.modules.grid import build_grid_doc` 한 줄로 끝낼 수 있어야 한다.
CONTRACT_PACKAGE_NAMES = {
    "build_grid_doc": "timegrid",
    "carve_spans": "timegrid",
    "transcribe_words": "transcribe",
    "retranscribe_gaps": "transcribe",
    "detect_scene_cuts": "scenecut",
    "detect_silence_intervals": "audio",
    "load_pcm": "audio",
    "compute_arousal": "arousal",
    "parse_ts": "schemas",
    "SNAP_TOLERANCE_SEC": "schemas",
}

# 모듈 **밖에서** 부르는 비공개 이름과 그 호출처. `from x import *` 는 이것들을
# 안 가져오므로 껍데기 방식이 바뀌면 여기가 먼저 깨져야 한다.
PRIVATE_NAMES_USED_OUTSIDE = {
    # app/v3/pipeline.py — 공백 재전사에 본전사와 같은 프롬프트를 태운다
    ("transcribe", "_build_whisper_prompt"),
    # tests/test_v3_stage1.py — 창 전사가 vad 를 명시적으로 끄는지 본다
    ("transcribe", "_transcribe_range"),
    # tests/test_v3_refine.py — 가짜 whisper 를 심는다(monkeypatch)
    ("transcribe", "_get_whisper_model"),
    ("transcribe", "_detect_device_and_compute"),
}

GRID_DIR = Path(grid.__file__).parent
V3_DIR = Path(v3.__file__).parent


def _public_toplevel_names(source: Path) -> set[str]:
    """소스를 AST 로 훑어 최상위 공개 바인딩 이름 집합. import 한 이름도 센다 —
    `import *` 라면 그것들도 따라왔을 테니 승격 전 네임스페이스와 같아야 한다."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: set[str] = set()

    def add(name: str) -> None:
        if name and not name.startswith("_"):
            names.add(name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    add(tgt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                add(alias.asname or alias.name.split(".")[0])
    return names


def _imported_modules(source: Path) -> set[str]:
    """최상위·함수 안을 가리지 않고 이 파일이 import 하는 모듈 경로 전량.
    ⚠ 함수 안 임포트를 놓치면 안 된다 — P4 이식이 그것 때문에 런타임에 죽었다."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


# ── ① 정본은 하나 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", PROMOTED)
def test_canonical_module_lives_under_app_modules_grid(name: str):
    """정본 파일이 실제로 옮겨졌는가 — 이름만 새로 만들고 v3 에 코드를 남겨 두면
    v3 은퇴 시 v4 가 끊긴다(승격을 한 이유 그 자체)."""
    mod = importlib.import_module(f"app.modules.grid.{name}")
    assert Path(mod.__file__).parent == GRID_DIR
    assert (GRID_DIR / f"{name}.py").is_file()


@pytest.mark.parametrize("name", PROMOTED)
def test_v3_path_is_the_same_module_object(name: str):
    """구 경로는 사본이 아니라 **같은 객체**여야 한다. 두 모듈 객체로 갈리면
    한쪽에만 monkeypatch·전역 상태가 걸려 조용히 다른 산출이 나온다."""
    shell = importlib.import_module(f"app.v3.{name}")
    canonical = importlib.import_module(f"app.modules.grid.{name}")
    assert shell is canonical
    assert sys.modules[f"app.v3.{name}"] is canonical
    # 패키지 속성 경로(`from app.v3 import audio`)도 같은 것을 준다
    assert getattr(v3, name) is canonical


@pytest.mark.parametrize("name", PROMOTED)
def test_v3_shell_carries_no_logic(name: str):
    """껍데기에 코드를 붙여도 아무도 못 본다 — sys.modules 치환 뒤로는 정본이
    곧바로 돌아가기 때문이다. 붙이는 사람이 없도록 값으로 막는다."""
    tree = ast.parse((V3_DIR / f"{name}.py").read_text(encoding="utf-8"))
    defs = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert defs == [], f"app/v3/{name}.py 에 정의가 생겼다 — 정본에 써라"


# ── ② 이름을 빠뜨리지 않는다 ────────────────────────────────────────────────

@pytest.mark.parametrize("name", PROMOTED)
def test_shell_exposes_every_public_name_of_the_canonical_source(name: str):
    """정본 소스의 공개 최상위 이름이 구 경로에서 전부 같은 객체로 보인다.
    ⚠ 소스를 AST 로 다시 읽어 대조한다 — 런타임 네임스페이스끼리 비교하면
    껍데기 방식이 무엇이든 항상 참인 동어반복이 된다."""
    canonical = importlib.import_module(f"app.modules.grid.{name}")
    shell = sys.modules[f"app.v3.{name}"]
    for public in sorted(_public_toplevel_names(GRID_DIR / f"{name}.py")):
        assert hasattr(shell, public), f"app.v3.{name} 에 {public} 이 없다"
        assert getattr(shell, public) is getattr(canonical, public)


@pytest.mark.parametrize("name,attr", sorted(PRIVATE_NAMES_USED_OUTSIDE))
def test_private_names_used_outside_survive_the_shell(name: str, attr: str):
    """밖에서 부르는 비공개 이름 — 호출처는 이 파일 상단 표에 적혀 있다.
    `import *` 껍데기는 이것들을 못 가져와 app/v3/pipeline.py 가 즉사했다(실측)."""
    shell = importlib.import_module(f"app.v3.{name}")
    assert hasattr(shell, attr)


def test_monkeypatch_through_v3_path_reaches_the_canonical_function(monkeypatch):
    """🛑 `from … import *` 껍데기를 기각시킨 조항이다(실측 5건 실패).

    구 경로에 가짜를 심으면 정본 함수가 그 가짜를 봐야 한다. 껍데기가 별도
    모듈이면 정본은 자기 globals 를 보므로 가짜가 안 먹고, 테스트가 진짜
    ffmpeg·whisper 를 부르러 나간다(test_v3_stage1·refine·stage2 가 그렇게 깨졌다).
    """
    from app.v3 import audio as v3_audio

    sentinel = RuntimeError("가짜 find_ffmpeg_command 가 불렸다")

    def _boom(_name):
        raise sentinel

    monkeypatch.setattr(v3_audio, "find_ffmpeg_command", _boom)
    with pytest.raises(RuntimeError) as got:
        v3_audio.detect_silence_intervals(Path("/does/not/exist.wav"), 10.0)
    assert got.value is sentinel


# ── 패키지 수준 이름(계약 §7) ───────────────────────────────────────────────

@pytest.mark.parametrize("attr,owner", sorted(CONTRACT_PACKAGE_NAMES.items()))
def test_contract_names_are_lifted_to_package_level(attr: str, owner: str):
    """계약이 지정한 이름이 패키지 수준에 있고, 소유 모듈의 것과 같은 객체다."""
    assert hasattr(grid, attr), f"app.modules.grid 에 {attr} 이 없다(계약 §7)"
    assert getattr(grid, attr) is getattr(
        importlib.import_module(f"app.modules.grid.{owner}"), attr)


def test_submodules_are_reachable_from_the_package():
    """`from app.modules.grid import audio` 도 성립해야 한다 — 계약이 여섯 모듈
    재수출을 함께 요구한다(호출자가 모듈 단위로 쓰는 곳이 있다)."""
    for name in PROMOTED:
        assert getattr(grid, name) is importlib.import_module(
            f"app.modules.grid.{name}")


def test_package_all_matches_what_is_actually_exported():
    """`__all__` 이 실물과 갈리면 `import *` 가 조용히 AttributeError 를 낸다."""
    for attr in grid.__all__:
        assert hasattr(grid, attr), f"__all__ 에 있는 {attr} 이 실물엔 없다"
    for attr in (*PROMOTED, *CONTRACT_PACKAGE_NAMES):
        assert attr in grid.__all__, f"{attr} 이 __all__ 에서 빠졌다"


# ── ③ leaf 성질 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", PROMOTED)
def test_grid_modules_do_not_import_v3_or_the_monolith(name: str):
    """승격의 값어치는 '파이프라인과 무관하게 부를 수 있다'에 있다. 여기로
    `app.v3`·`app.pipeline` 이 새로 들어오면 v3 동결이 곧 v4 단절이 된다."""
    for mod in _imported_modules(GRID_DIR / f"{name}.py"):
        assert not mod.startswith("app.v3"), f"{name} 이 {mod} 를 import 한다"
        assert not mod.startswith("app.v4"), f"{name} 이 {mod} 를 import 한다"
        assert mod != "app.pipeline" and not mod.startswith("app.pipeline."), (
            f"{name} 이 v1 모놀리스를 import 한다")


def test_intra_group_dependency_stays_only_arousal_to_audio():
    """여섯은 leaf 이고 서로에 대한 의존은 `arousal → audio`(SAMPLE_RATE) 하나뿐이다.
    이 성질이 깨지면 v4 가 재료 하나만 골라 쓰지 못한다 — 값으로 고정한다."""
    edges: set[tuple[str, str]] = set()
    for name in PROMOTED:
        for mod in _imported_modules(GRID_DIR / f"{name}.py"):
            if mod.startswith("app.modules.grid"):
                tail = mod.split(".")[-1]
                if tail in PROMOTED and tail != name:
                    edges.add((name, tail))
    assert edges == {("arousal", "audio")}


def test_grid_package_does_not_depend_on_v3_package():
    """패키지 __init__ 자체도 v3 를 모른다 — 알면 순환 승격이 된다."""
    for mod in _imported_modules(GRID_DIR / "__init__.py"):
        assert not mod.startswith("app.v3")
