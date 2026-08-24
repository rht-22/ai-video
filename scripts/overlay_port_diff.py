#!/usr/bin/env python3
"""L-P4 이식 대조 — overlay 계층이 vlp 원본과 **같은 코드**인지 기계로 확인한다.

P1 의 `localize_port_diff.py` 와 같은 역할이고 방법이 다르다: rerender 는 산출물
(render_flags·ass·pairs)을 맞댔지만, overlay 는 이식이 **파일 단위 복사**라 함수 본문을
AST 로 맞댄다. 주석·docstring 차이는 무시하고 **실행문만** 본다.

    python -m scripts.overlay_port_diff [--vlp <경로>] [--verbose]

의도적으로 갈라진 것은 EXPECTED_DIFFS 에 사유와 함께 적혀 있고, **거기 없는 차이가
나오면 실패**한다. vlp 가 또 앞서가면(P2b·E16 때 두 번 그랬다) 이 스크립트가 먼저 운다.

vlp 를 동결(P8)하면 이 스크립트도 함께 은퇴한다.
"""
from __future__ import annotations

import argparse
import ast
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_VLP = pathlib.Path(
    os.environ.get("VLP_ROOT") or REPO.parent / "video-localization-project")

# 이식 대상: (vlp 상대경로, ai-video 상대경로)
PAIRS = [
    ("engine/common.py",     "app/localize/overlay/common.py"),
    ("engine/detect.py",     "app/localize/overlay/detect.py"),
    ("engine/mask.py",       "app/localize/overlay/mask.py"),
    ("engine/inpaint.py",    "app/localize/overlay/inpaint.py"),
    ("engine/translate.py",  "app/localize/overlay/translate.py"),
    ("engine/render.py",     "app/localize/overlay/render.py"),
    ("engine/cuts.py",       "app/localize/overlay/cuts.py"),
    ("engine/schemas.py",    "app/localize/overlay/schemas.py"),
    ("engine/qa.py",         "app/localize/overlay/qa.py"),
    ("engine/qa_compare.py", "app/localize/overlay/qa_compare.py"),
    ("engine/llm.py",        "app/localize/overlay/llm.py"),
    ("src/process_video.py", "app/localize/overlay/pipeline.py"),
    ("src/dub.py",           "app/localize/overlay/dub.py"),
    ("src/refbank.py",       "app/localize/overlay/refbank.py"),
    ("src/precheck.py",      "app/localize/overlay/precheck.py"),
]

# 🛑 의도적으로 갈라진 것 — 사유가 없으면 여기 못 들어온다.
EXPECTED_DIFFS = {
    "llm.resolve_model":
        "이 레포의 모델 규칙(CLAUDE.md)을 강제한다 — vlp config 의 gemini-3.5-flash·"
        "gemini-pro-latest 는 사용 금지 모델이다. P1 이 localize_run Flash 를 바꾼 것과 같은 규약.",
    "common.load_config":
        "설정 기본 경로가 vlp 레포의 config/ 가 아니라 이 계층의 data/ 다.",
    "pipeline.process_video":
        "로그가 안내하는 더빙 실행 경로를 이식 위치로 고쳤다 — vlp 의 `python -m src.dub` 는 "
        "이 레포에 없는 모듈이라 그대로 두면 사람을 없는 파일로 보낸다. 실행문은 같다.",
    "llm._gemini":
        "vlp 의 ThinkingConfig(thinking_budget=0) 은 2.5 시대 매개변수라 이 레포가 허용하는 "
        "Gemini 3.x 가 400 INVALID_ARGUMENT 로 거절한다(2026-08-24 mm-06 실측). 같은 의도를 "
        "3.x 어휘 thinking_level='minimal' 로 옮겼다 — 모델 규칙과 요청 모양은 짝이다.",
    "llm._gemini_vision":
        "위와 같음 — 비전 경로도 같은 매개변수를 쓴다.",
    "translate._transcreate_one":
        "응답을 한 줄도 못 읽으면 크게 실패시킨다. vlp 는 못 찾은 항목을 빈 문자열로 채우고 "
        "그대로 진행하는데, 전부 못 찾으면 '번역이 전부 빈' 자막이 조용히 렌더까지 간다 "
        "(2026-08-24 mm-06 실측: 18/18 이 빈 채로 이벤트 0개짜리 산출이 나왔다). "
        "유료 호출을 하고 빈 결과를 내는 것은 실패지 결과가 아니다 — 이 레포의 무성 폴백 금지 규율.",
    "dub.dub_from_video":
        "self-ref 프로브가 자기 자신을 서브프로세스로 다시 부르는데(모델 캐시 오염 격리) "
        "vlp 는 `src.dub` 를 박아 뒀다. 모듈 경로를 _SELF_MODULE 한 곳에서 만들도록 바꿨다 "
        "— 안 고치면 이 레포에서 그 프로브가 즉사한다.",
}
# 모듈 수준 상수 중 갈라진 것
EXPECTED_CONST_DIFFS = {
    "common.PROJECT_ROOT":
        "vlp 는 자기 레포 루트, 여기는 ai-video 레포 루트(app/localize/overlay 에서 4단계 위). "
        "config 의 상대경로가 이 기준으로 풀린다.",
}
# vlp 에만 있어도 되는 것 (이식하지 않기로 한 것)
EXPECTED_MISSING = {
    "pipeline._parse_args": "이 레포의 진입점은 app.cli 하나다(rerender 가 세운 규약).",
    "pipeline.main": "위와 같음 — CLI 가 인자를 넘긴다.",
}


class _Normalize(ast.NodeTransformer):
    """패키지 이름만 다른 임포트를 같게 본다.

    이식은 파일 복사라 본문이 같은데, 함수 **안**의 `from engine.X import …` 가
    `from app.localize.overlay.X import …` 로 바뀌면서 AST 가 달라진다. 그 한 가지를
    정규화하지 않으면 멀쩡한 이식 16건이 전부 '예상 밖 차이'로 뜬다 — 도구가 늘 울면
    사람이 도구를 안 본다."""

    _PREFIXES = ("engine.", "app.localize.overlay.", "src.")

    def visit_ImportFrom(self, node):                       # noqa: N802
        mod = node.module or ""
        for pre in self._PREFIXES:
            if mod.startswith(pre):
                node.module = "«pkg»." + mod[len(pre):]
                break
        else:
            if mod in ("engine", "app.localize.overlay", "src"):
                node.module = "«pkg»"
        return self.generic_visit(node)


def _funcs(src: str) -> dict:
    """모듈의 함수·메서드 → 실행문 AST 덤프(docstring 제외). 클래스 메서드는 Cls.method."""
    out = {}

    def body_of(node):
        b = node.body
        if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant):
            b = b[1:]
        return "\n".join(ast.dump(_Normalize().visit(s), annotate_fields=False) for s in b)

    for n in ast.parse(src).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = body_of(n)
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{n.name}.{m.name}"] = body_of(m)
    return out


def _consts(src: str) -> dict:
    """모듈 수준 대문자 상수 → 소스 표현."""
    out = {}
    for n in ast.parse(src).body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id.isupper():
            out[n.targets[0].id] = ast.dump(n.value, annotate_fields=False)
    return out


def compare(vlp_root: pathlib.Path, verbose: bool = False) -> int:
    if not (vlp_root / "engine" / "common.py").exists():
        print(f"❌ vlp 를 못 찾았다: {vlp_root}\n   --vlp 또는 VLP_ROOT 로 지정하세요.")
        return 2

    same = 0
    unexpected: list[str] = []
    accounted: list[str] = []

    for rel_v, rel_p in PAIRS:
        mod = pathlib.Path(rel_p).stem
        pv, pp = vlp_root / rel_v, REPO / rel_p
        if not pp.exists():
            unexpected.append(f"{rel_p}: 이식본이 없다")
            continue
        fv, fp = _funcs(pv.read_text()), _funcs(pp.read_text())
        cv, cp = _consts(pv.read_text()), _consts(pp.read_text())

        for name, body in fv.items():
            key = f"{mod}.{name}"
            if name not in fp:
                (accounted if key in EXPECTED_MISSING else unexpected).append(
                    f"{key}: 이식본에 없음" + (f" — {EXPECTED_MISSING[key]}"
                                              if key in EXPECTED_MISSING else ""))
            elif fp[name] == body:
                same += 1
            elif key in EXPECTED_DIFFS:
                accounted.append(f"{key}: 의도된 차이 — {EXPECTED_DIFFS[key]}")
            else:
                unexpected.append(f"{key}: 본문이 다르다")

        for name, val in cv.items():
            key = f"{mod}.{name}"
            if name not in cp:
                unexpected.append(f"{key}: 상수가 없다")
            elif cp[name] == val:
                same += 1
            elif key in EXPECTED_CONST_DIFFS:
                accounted.append(f"{key}: 의도된 차이 — {EXPECTED_CONST_DIFFS[key]}")
            else:
                unexpected.append(f"{key}: 상수 값이 다르다 ({val} → {cp[name]})")

    print(f"vlp {vlp_root}")
    print(f"  동일          {same}")
    print(f"  의도된 차이   {len(accounted)}")
    for line in accounted:
        print(f"    · {line}" if verbose else f"    · {line.split(':')[0]}")
    print(f"  예상 밖 차이  {len(unexpected)}")
    for line in unexpected:
        print(f"    !! {line}")

    if unexpected:
        print("\n판정: ❌ 예상 밖 차이가 있다.\n"
              "  vlp 가 앞서갔다면 그 변경을 이식하고, 이식본이 일부러 다르면\n"
              "  EXPECTED_DIFFS 에 **사유와 함께** 적어라(사유 없는 예외는 두지 않는다).")
        return 1
    print("\n판정: ✅ 이식본이 vlp 와 같다 (의도된 차이 제외)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="overlay 이식 대조 (L-P4)")
    ap.add_argument("--vlp", default=str(DEFAULT_VLP))
    ap.add_argument("--verbose", action="store_true", help="의도된 차이의 사유까지 출력")
    a = ap.parse_args(argv)
    return compare(pathlib.Path(a.vlp), a.verbose)


if __name__ == "__main__":
    sys.exit(main())
