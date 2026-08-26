"""호출 키워드와 시그니처를 묶는다 — 2026-08-26 생성 전멸 사고.

🛑 인물 인식(TMDb·deepface) 제거 때 `build_crop_timeline` 에서 `prev_target_character`
   인자를 지우면서 **호출부에 남겨 뒀다.** 그날 09:00 KST 생성 배치가 통째로 죽었다:

       TypeError: build_crop_timeline() got an unexpected keyword argument
                  'prev_target_character'
       generate 11건 → dead 8 · running 2 · pending 1  (전부 같은 원인)

   문법은 통과한다. 임포트도 통과한다. **부르기 전까지는 아무도 모른다** — 같은 부류를
   이 세션에서 두 번 겪었다(find_ffmpeg_command 도 인자 하나였다).

이 파일은 그 부류를 정적으로 잡는다: 파이프라인이 실제로 넘기는 키워드가 함수
시그니처에 다 있는지 본다. 인자를 지우면 여기서 먼저 붉어진다.
"""
import ast
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.modules import reframe  # noqa: E402

PIPELINE = pathlib.Path("app/pipeline.py")


def _call_keywords(src: str, func_name: str) -> list:
    """소스에서 그 함수를 부르는 모든 호출의 키워드 이름들."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if name == func_name:
            out.append([kw.arg for kw in node.keywords if kw.arg])
    return out


def test_pipeline_only_passes_arguments_that_exist():
    params = set(inspect.signature(reframe.build_crop_timeline).parameters)
    calls = _call_keywords(PIPELINE.read_text(encoding="utf-8"), "build_crop_timeline")
    assert calls, "호출부를 못 찾았다 — 이 가드가 아무것도 안 지키고 있다"
    for kws in calls:
        unknown = [k for k in kws if k not in params]
        assert not unknown, f"시그니처에 없는 인자를 넘긴다: {unknown}"


def test_the_removed_argument_is_gone_from_both_sides():
    """인물 인식은 2026-08-25 에 사라졌다 — 양쪽 다에서 사라져야 한다."""
    assert "prev_target_character" not in inspect.signature(
        reframe.build_crop_timeline).parameters
    assert "prev_target_character" not in PIPELINE.read_text(encoding="utf-8")


def test_sticky_anchor_still_works():
    """지운 것은 '인물' sticky 뿐이다 — 위치 sticky(라운드 24)는 살아 있어야 한다."""
    params = set(inspect.signature(reframe.build_crop_timeline).parameters)
    assert {"initial_x", "initial_y"} <= params
    src = PIPELINE.read_text(encoding="utf-8")
    assert "initial_x=prev_anchor_x" in src and "initial_y=prev_anchor_y" in src


def test_no_dead_bookkeeping_left_behind():
    """쓰지 않는 변수를 남기면 다음 사람이 '이건 뭐지' 로 되살린다."""
    src = PIPELINE.read_text(encoding="utf-8")
    assert "prev_focus_char" not in src and "current_target_char" not in src
