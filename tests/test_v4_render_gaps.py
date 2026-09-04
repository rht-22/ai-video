"""V4-M6 §5 — v1·v3 렌더 경로의 **차이를 보이게 하는 계기판**.

이 파일은 기능 가드가 아니다. 고치는 코드가 없고, 지키는 계약도 없다.
**지금 v3 렌더 경로에 없는 것을 이름으로 못박아, 다음 사람이 그 목록을 보고
판단하게 하는 것**이 유일한 목적이다.

배경 — `docs/v4/M6-interfaces.md` §0·§5:

  기획서 §1-1 은 `app/modules/render_adapter.py` 추출(v1·v3 렌더 호출부 병합)을
  요구한다. 이 판은 **하지 않는다** — v1 렌더는 21개 채널이 도는 표면이라 건드리는
  것 자체가 별건이다(M7 잔여). 대신 차이를 테스트로 못박는다.

  조사 기록 그대로: "v3 판은 **E10 마진·이미지 오버레이·제목 창·효과음 전달**이
  빠져 있다."

⚠ **v4 도 같은 것이 빠진다.** M6 §0 의 재사용 방침대로 v4 는 자기 렌더 경로를 짓지
않고 v3 `finalize.render_final` 을 **부른다**. 그래서 v3 가 `RenderInputs` 에 안
넘기는 것은 v4 산출물에도 그대로 없다 — v4 로 만든 편에는 AI 스티커도, 시간대별
제목도, 효과음도 화면에 나오지 않는다. `app/v4/` 에 자체 렌더 호출부가 생기면
이 계기판이 두 갈래가 아니라 세 갈래를 재야 하므로, 그 사실도 아래에서 검사한다.

읽는 법:
  · 통과 = "차이가 어제와 같다". 표는 warnings 요약(기본 출력)과 stdout(`-s`)에 나온다.
  · 실패 = "차이가 **움직였다**". 늘었으면 회귀(v3 가 더 뒤처졌다), 줄었으면 누가
    메운 것이니 이 파일의 원장(GAP_NOTES)을 갱신해야 한다. 어느 쪽이든 메시지가
    무엇이 움직였는지 이름으로 말한다.

키 집합은 **AST 로 뽑는다**(정규식이 아니라) — 호출부가 여러 줄에 걸쳐 있고 주석에
같은 이름이 들어 있어(renderer.py 머리말의 설계 주석) 문자열 검색은 거짓을 센다.
LLM·네트워크·ffmpeg 0 — 전부 소스 파싱과 순수 계산이다.
"""
from __future__ import annotations

import ast
import dataclasses
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
V1_PIPELINE = REPO / "app" / "pipeline.py"
V3_FINALIZE = REPO / "app" / "v3" / "finalize.py"
V4_DIR = REPO / "app" / "v4"


class RenderGapReport(UserWarning):
    """통과했을 때도 표가 보이도록 쓰는 경고 — 계기판은 눈에 띄어야 계기판이다."""


# ── 소스에서 키 집합 뽑기 (AST) ─────────────────────────────────────────────

def render_inputs_call_sites(path: Path) -> list[tuple[int, frozenset[str]]]:
    """그 파일 안 `RenderInputs(...)` 호출들의 (줄번호, 키워드 이름 집합).

    ⚠ `**kwargs` 전개가 섞이면 키를 다 못 센다 — 그때는 조용히 적게 세는 대신
    크게 실패한다(계기판이 거짓을 가리키는 것이 최악이다).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites: list[tuple[int, frozenset[str]]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "RenderInputs"):
            continue
        if any(kw.arg is None for kw in node.keywords):
            raise AssertionError(
                f"{path.name}:{node.lineno} — RenderInputs 호출에 `**` 전개가 있다. "
                "AST 로는 키를 다 셀 수 없으니 이 계기판을 손봐야 한다.")
        if node.args:
            raise AssertionError(
                f"{path.name}:{node.lineno} — RenderInputs 를 위치 인자로 부른다. "
                "이 계기판은 키워드만 센다.")
        sites.append((node.lineno, frozenset(kw.arg for kw in node.keywords
                                             if kw.arg is not None)))
    return sorted(sites)


def called_function_names(path: Path) -> set[str]:
    """그 파일이 부르는 함수 이름(`f()`·`obj.f()` 의 마지막 이름)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def v1_primary_keys() -> frozenset[str]:
    """v1 정본(1위 쇼츠) 호출부의 키 집합.

    v1 에는 호출부가 둘이다 — 정본과 variant(#2·#3). **variant 는 애초에 자막 계열을
    안 받는 기존 구멍**이라(CLAUDE.md E15 절: "variant #2·#3 의 RenderInputs 는
    image_overlays·title_segments·text_subtitle_path 를 애초에 받지 않는다") 정본을
    기준으로 삼는다. '가장 큰 것이 정본'이라는 이 선택이 흔들리지 않는 것은
    `test_v1_variant_is_subset_of_primary` 가 지킨다.
    """
    sites = render_inputs_call_sites(V1_PIPELINE)
    return max(sites, key=lambda s: len(s[1]))[1]


def v3_keys() -> frozenset[str]:
    sites = render_inputs_call_sites(V3_FINALIZE)
    assert len(sites) == 1, f"v3 렌더 호출부가 1개가 아니다: {[s[0] for s in sites]}"
    return sites[0][1]


# ── 원장: 지금 알려진 구멍 ──────────────────────────────────────────────────
#
# 키 이름 → (등급, 무엇인가 · 왜 중요한가). 등급 둘의 뜻:
#   · "화면·소리 누락" — 이 키가 없으면 **만든 연출이 최종본에 아예 없다**.
#     v1 에서 돌던 기능이 v3·v4 산출물에서 통째로 증발한다.
#   · "노브 상실"      — `RenderInputs` 기본값으로 돌기는 한다. 잃은 것은 채널·
#     실행마다 다르게 줄 수 있는 **손잡이**이지 화면 그 자체가 아니다.
#
# 이 사전이 곧 기대값이다(아래 EXPECTED_MISSING) — 목록과 설명이 갈릴 수 없게
# 한 곳에서 낳는다. 구멍을 메웠으면 여기서 항목을 지워야 테스트가 통과한다.
GAP_NOTES: dict[str, tuple[str, str]] = {
    "image_overlays": (
        "화면·소리 누락",
        "E15 스티커 + 편집실 이미지(v3 images, F-408). place_anchored_images 로 배치가 "
        "끝난 항목을 넘기는 통로다. 없으면 style 단계가 고른 스티커도, 사람이 올린 "
        "이미지도 최종본에 한 장도 안 나온다."),
    "title_segments": (
        "화면·소리 누락",
        "E8 시간대별 제목 창(+E18-1 빈틈 메우기 · E21 윗줄 고정/아랫줄 교체). 없으면 "
        "제목은 편 내내 top_title 하나로 고정된다 — 구간마다 제목을 바꾸는 연출 전체가 "
        "화면에 도달하지 못한다."),
    "sfx_audio": (
        "화면·소리 누락",
        "E19-5 효과음(SFX) 레이어. cue 와 같은 방식(adelay + volume dB + amix)으로 "
        "입력을 더한다. 없으면 SFX 는 파일까지 만들어 놓고 믹스에 한 번도 안 실린다."),
    "loudness_target_lufs": (
        "노브 상실",
        "출력 라우드니스 정규화 목표(LUFS). 기본값 -14.0 이라 **정규화 자체는 돈다** — "
        "잃은 것은 편·채널마다 목표를 바꾸거나 None 으로 꺼서 A/B 대조군을 만드는 길이다."),
    "render_preset": (
        "노브 상실",
        "balanced|fastest|quality. 기본 balanced 이고 renderer 주석이 '현재는 balanced 만 "
        "사용'이라 실화면 차이는 지금 없다 — 다른 프리셋을 켤 때 v3 경로만 못 켠다."),
    "enable_hwaccel": (
        "노브 상실",
        "하드웨어 디코드 가속. 기본 True 라 켜져서 돈다 — 잃은 것은 노드 config 로 "
        "끄는 길이다(가속이 말썽인 노드에서 v3 경로만 못 끈다)."),
}
EXPECTED_MISSING = frozenset(GAP_NOTES)

# 반대 방향 — v3 만 넘기는 것. 이쪽도 고정한다: v1 이 나중에 받아 가면(병합) 그 사실이
# 드러나야 하고, v3 가 잃으면 그건 v3 회귀다.
EXPECTED_V3_ONLY: dict[str, str] = {
    "muted_windows": (
        "V3-M4 원본 오디오 뮤트 창(편집본 좌표). v3 의 use_original_audio=False 클립 "
        "(TTS 슬롯 ⓑ 뮤트)이 여기 실린다 — v1 에는 그 개념이 없어 안 넘긴다."),
    "output_fps": (
        "V4-M7 출력 fps 고정(운영자 결정 O9 = 30). 종전 argv 에는 `-r` 이 없어 출력이 "
        "소재 fps 를 따라갔다 — v4 는 값을 싣고 v1 은 안 싣는다(None = argv 바이트 동일). "
        "v1 이 나중에 이 값을 쓰기 시작하면 전 채널 출력 fps 가 함께 움직이므로, 그 "
        "사실이 이 원장에서 드러나야 한다."),
}

# 두 호출부 어느 쪽도 안 넘기는 필드. **구멍이 아니다** — renderer 가 스스로 채운다
# (render_short 가 title.txt·work_title.txt 를 쓰고 replace 로 되꽂는다). 새 필드가
# 생겼는데 아무도 안 넘기면 이 목록이 늘어나 눈에 띈다.
RENDERER_FILLED: frozenset[str] = frozenset({"title_textfile", "work_title_textfile"})


def _table(missing: frozenset[str], v3_only: frozenset[str]) -> str:
    """사람이 보고 판단할 표 — 실패 메시지와 통과 경고에 같은 것을 싣는다."""
    lines = [
        "",
        "━━ v1 → v3 렌더 경로 차이 (M6 §5 계기판) ━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  v1 정본 {V1_PIPELINE.name}:{max(render_inputs_call_sites(V1_PIPELINE), key=lambda s: len(s[1]))[0]}"
        f"  →  v3 {V3_FINALIZE.parent.name}/{V3_FINALIZE.name}:{render_inputs_call_sites(V3_FINALIZE)[0][0]}",
        f"  v3(=v4)에 없는 것 {len(missing)}개:",
    ]
    for key in sorted(missing):
        grade, note = GAP_NOTES.get(key, ("?", "설명 없음 — GAP_NOTES 에 한 줄 적어라"))
        lines.append(f"    · {key:<22} [{grade}]")
        for chunk in _wrap(note, 74):
            lines.append(f"        {chunk}")
    lines.append(f"  v3 에만 있는 것 {len(v3_only)}개:")
    for key in sorted(v3_only):
        lines.append(f"    · {key:<22} {EXPECTED_V3_ONLY.get(key, '(원장에 없음)')}")
    lines += [
        "  ⚠ v4 는 v3 render_final 을 부른다 — 위 '없는 것'은 v4 산출물에도 없다.",
        "  ⚠ E10 밴드 앵커 마진은 키가 아니라 **호출부 상류**에 있어 이 표에 안 나온다",
        "     — test_e10_band_anchored_margin_absent_in_v3 가 따로 잰다.",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out


# ── 호출부가 계기판이 가정한 모양인가 ───────────────────────────────────────

def test_call_sites_have_the_shape_this_gauge_assumes():
    """원장 전체가 '호출부 2 + 1' 위에 서 있다 — 그 전제가 깨지면 먼저 말한다."""
    v1_sites = render_inputs_call_sites(V1_PIPELINE)
    v3_sites = render_inputs_call_sites(V3_FINALIZE)
    assert len(v1_sites) == 2, (
        f"v1 RenderInputs 호출부가 2개(정본·variant)가 아니라 {len(v1_sites)}개다 "
        f"(줄 {[s[0] for s in v1_sites]}). 누가 렌더 호출부를 늘리거나 합쳤다 — "
        "이 파일의 '정본 = 가장 큰 것' 규칙부터 다시 정해야 한다.")
    assert len(v3_sites) == 1, (
        f"v3 RenderInputs 호출부가 1개가 아니라 {len(v3_sites)}개다 "
        f"(줄 {[s[0] for s in v3_sites]}).")


def test_v1_variant_is_subset_of_primary():
    """variant 호출부는 정본의 부분집합이어야 '가장 큰 것 = 정본'이 성립한다.

    부분집합이라는 사실 자체가 CLAUDE.md E15 절이 적어 둔 기존 구멍의 확인이다
    (variant #2·#3 은 image_overlays·title_segments·text_subtitle_path 를 안 받는다).
    """
    sites = render_inputs_call_sites(V1_PIPELINE)
    primary = max(sites, key=lambda s: len(s[1]))
    others = [s for s in sites if s[0] != primary[0]]
    for lineno, keys in others:
        extra = keys - primary[1]
        assert not extra, (
            f"v1:{lineno} variant 호출부가 정본에 없는 키를 넘긴다: {sorted(extra)}. "
            "'가장 큰 것이 정본'이라는 이 계기판의 기준이 더는 안 맞는다.")
    # 구멍이 있다는 사실을 값으로 남긴다 — 메워지면 여기서 먼저 드러난다.
    assert primary[1] - others[0][1], (
        "v1 variant 호출부가 정본과 같아졌다 — E15 절의 'variant 는 자막 계열을 "
        "안 받는다'가 더는 사실이 아니다. CLAUDE.md 와 이 주석을 갱신하라.")


# ── 본 계기판 ───────────────────────────────────────────────────────────────

def test_render_key_gap_ledger():
    """v1 정본 − v3 차집합이 원장과 같은가. **표를 반드시 사람에게 보인다.**

    통과가 목적이 아니다 — 통과는 "어제와 같다"는 뜻일 뿐이다. 차이가 움직였으면
    어느 쪽으로 움직였는지 이름으로 말하고 실패한다:
      · 늘었다 = v3 가 더 뒤처졌다(회귀).
      · 줄었다 = 누가 메웠다. 축하할 일이지만 **원장이 낡았다** — 낡은 계기판은
        거짓말을 하므로 갱신 전까지 통과시키지 않는다.
    """
    v1, v3 = v1_primary_keys(), v3_keys()
    missing, v3_only = v1 - v3, v3 - v1
    table = _table(missing, v3_only)
    print(table)

    if missing != EXPECTED_MISSING:
        filled = EXPECTED_MISSING - missing      # 원장엔 있는데 이제 없다 = 메워졌다
        opened = missing - EXPECTED_MISSING      # 원장엔 없는데 생겼다 = 회귀
        msg = [table, ""]
        if filled:
            msg.append(
                "✅ 메워진 구멍: " + ", ".join(sorted(filled)) + "\n"
                "   v3 렌더 경로가 이제 이것을 넘긴다. GAP_NOTES 에서 그 항목을 지우고 "
                "docs/v4/UNVERIFIED.md 의 해당 줄도 함께 정리하라.")
        if opened:
            msg.append(
                "🛑 새로 벌어진 구멍: " + ", ".join(sorted(opened)) + "\n"
                "   v1 이 넘기는데 v3 는 안 넘긴다 = v3·v4 산출물에서 그만큼이 사라진다. "
                "메우거나, 못 메울 사유와 함께 GAP_NOTES 에 등재하라.")
        pytest.fail("\n\n".join(msg))

    # 통과 경로 — 표가 stdout 캡처에 묻히지 않게 경고로도 띄운다.
    warnings.warn(table, RenderGapReport, stacklevel=1)


def test_v3_only_keys_match_the_ledger():
    """반대 방향도 고정 — v1 이 받아 가거나 v3 가 잃으면 드러난다."""
    v1, v3 = v1_primary_keys(), v3_keys()
    assert v3 - v1 == frozenset(EXPECTED_V3_ONLY), (
        f"v3 에만 있는 키가 {sorted(v3 - v1)} 로 바뀌었다 "
        f"(원장: {sorted(EXPECTED_V3_ONLY)}). 병합이 진행됐거나 v3 가 뭔가 잃었다.")


def test_every_gap_key_is_a_real_render_field():
    """원장의 이름이 실제 `RenderInputs` 필드인가 — 필드가 개명되면 원장이 유령이 된다."""
    from app.modules.renderer import RenderInputs
    fields = {f.name for f in dataclasses.fields(RenderInputs)}
    unknown = (EXPECTED_MISSING | set(EXPECTED_V3_ONLY) | RENDERER_FILLED) - fields
    assert not unknown, (
        f"원장에 있는데 RenderInputs 에 없는 이름: {sorted(unknown)}. "
        "필드가 개명·삭제됐다 — 이 계기판이 존재하지 않는 구멍을 가리키고 있다.")
    for key in EXPECTED_MISSING:
        grade, note = GAP_NOTES[key]
        assert grade in ("화면·소리 누락", "노브 상실"), f"{key}: 모르는 등급 {grade!r}"
        assert len(note) > 30, (
            f"{key}: 설명이 너무 짧다. 목록만 있으면 다음 사람이 판단할 근거가 없다 — "
            "무엇이고 없으면 화면에서 무엇이 사라지는지 한 줄로 적어라.")


def test_fields_nobody_passes_are_renderer_filled():
    """두 호출부 어느 쪽도 안 넘기는 필드 = 렌더러가 채우는 것뿐이어야 한다."""
    from app.modules.renderer import RenderInputs
    fields = {f.name for f in dataclasses.fields(RenderInputs)}
    unpassed = fields - (v1_primary_keys() | v3_keys())
    assert unpassed == RENDERER_FILLED, (
        f"아무도 안 넘기는 필드가 {sorted(unpassed)} 다(예상 {sorted(RENDERER_FILLED)}). "
        "새 필드가 생겼는데 호출부가 안 따라갔거나, 렌더러가 스스로 채우던 것을 "
        "누가 넘기기 시작했다 — 어느 쪽인지 확인하고 이 목록을 갱신하라.")


# ── 키로는 안 보이는 구멍: E10 밴드 앵커 마진 ───────────────────────────────

def test_e10_band_anchored_margin_absent_in_v3():
    """조사 기록의 'E10 마진'은 **키 차집합에 안 나온다** — 상류에 있기 때문이다.

    v1 은 `RenderInputs` 를 짓기 전에 `_compute_subtitle_margin_v` ·
    `_compute_tts_margin_v` 로 자막 margin_v 를 **밴드 하단에서 역산**해 ASS 를
    굽는다(CLAUDE.md E10: "메인 자막 margin_v — 항상 밴드 하단 10px 위").
    v3 `render_final` 은 `design.subtitle_y_margin`·`design.tts_line_y_margin`
    (프리셋에 손으로 적힌 상수)을 그대로 쓴다. 두 함수를 부르지 않는다.

    ⚠ 메우는 것은 '그 함수를 부르면 끝'이 아니다 — `_compute_subtitle_margin_v` 의
    밴드 앵커는 `video_width` 가 **명시된 경우에만** 발동하는데(8/21 발주 검수 교정)
    v3 프리셋은 `video_y` 만 주고 `video_width` 를 안 준다. 그대로 부르면 종전 기하
    (세로 중앙 가정)로 떨어진다. 이 사실을 모르고 배선하면 조용히 아무 일도 안 난다.
    """
    v1_calls = called_function_names(V1_PIPELINE)
    v3_calls = called_function_names(V3_FINALIZE)
    e10 = {"_compute_subtitle_margin_v", "_compute_tts_margin_v"}
    assert e10 <= v1_calls, (
        f"v1 이 E10 밴드 앵커 마진 함수를 더는 안 부른다: {sorted(e10 - v1_calls)}. "
        "비교 기준이 사라졌다 — 이 계기판을 다시 세워야 한다.")
    still_absent = e10 - v3_calls
    assert still_absent == e10, (
        f"✅ v3 가 E10 밴드 앵커 마진을 쓰기 시작했다({sorted(e10 & v3_calls)}). "
        "조사 기록의 'E10 마진' 구멍이 메워졌다 — 이 테스트와 모듈 독스트링, "
        "docs/v4/UNVERIFIED.md 를 갱신하라.")


def test_e10_gap_costs_pixels_when_the_style_model_moves_the_band():
    """E10 구멍이 무해하지 않다는 것을 **픽셀 수로** 보인다.

    v3 프리셋의 `subtitle_y_margin` 은 그 프리셋의 밴드에 맞춰 손으로 잰 값이다
    (recap 주석: "템플릿 center_y 1372 → 하단 마진 518"). 그런데 `aspect_ratio` 는
    `stage4.STYLE_ALLOWED` 에 있어 **스타일 모델이 바꿀 수 있고**, `design_from_style`
    이 그대로 반영한다. 밴드 높이는 따라 움직이는데 마진은 상수라 자막이 밴드를
    벗어난다 — v1 이라면 밴드 하단에서 다시 역산됐을 자리다.

    수식은 베끼지 않고 `subtitle_region.band_geometry`(렌더러 [2]와 같은 순서를
    보증하는 그 함수)를 그대로 부른다.
    """
    from app.config import AppConfig
    from app.modules.subtitle_region import band_geometry
    from app.v3.finalize import design_from_style
    from app.v3.stage4 import RECAP_PRESET

    H = AppConfig().canvas_height   # 캔버스 정본은 AppConfig (v3 render_final 과 같다)

    base = design_from_style(dict(RECAP_PRESET))
    band = band_geometry(base, canvas_height=H)
    baseline = H - int(base.subtitle_y_margin)
    # 프리셋 그대로면 자막은 밴드 **안**에 있다(사람이 그 밴드에 맞춰 쟀으니까).
    assert baseline < band.overlay_y + band.scaled_h, (
        f"프리셋 기본 상태에서 이미 자막이 밴드 밖이다 "
        f"(baseline {baseline} ≥ 밴드 하단 {band.overlay_y + band.scaled_h}).")

    # 모델이 화면비만 바꾼 경우 — STYLE_ALLOWED 가 허용하는 값이다.
    moved = design_from_style({**RECAP_PRESET, "aspect_ratio": "16:9"})
    band2 = band_geometry(moved, canvas_height=H)
    bottom2 = band2.overlay_y + band2.scaled_h
    drop = baseline - bottom2
    assert drop > 0, (
        "화면비를 16:9 로 바꿔도 자막이 밴드 안에 남는다 — 이 시나리오가 더는 "
        "E10 구멍을 보여 주지 못하니 다른 예를 찾아라.")
    warnings.warn(
        f"\nE10 구멍의 대가(계산): 화면비 24:23 → 16:9 로만 바꿔도 밴드 하단이 "
        f"{band.overlay_y + band.scaled_h} → {bottom2} 로 올라가는데 자막 baseline 은 "
        f"{baseline} 에 그대로 있다 = 검은 밴드 위 {drop}px. "
        f"v1 이라면 밴드 하단에서 다시 역산됐을 자리다.",
        RenderGapReport, stacklevel=1)


# ── v4 도 같은 것이 빠진다 ──────────────────────────────────────────────────

def test_v4_has_no_render_call_of_its_own():
    """v4 가 자체 `RenderInputs` 호출부를 갖지 않는가 = v3 의 구멍을 그대로 물려받는가.

    M6 §0 의 재사용 방침이다("v3 render_final 을 **부른다**"). 그래서 위 차집합은
    v4 산출물의 결손 목록이기도 하다. v4 가 자기 호출부를 갖게 되면 이 계기판은
    두 갈래가 아니라 **세 갈래**를 재야 한다 — 그때 먼저 말한다.
    """
    own: list[str] = []
    for path in sorted(V4_DIR.rglob("*.py")):
        for lineno, keys in render_inputs_call_sites(path):
            own.append(f"{path.relative_to(REPO)}:{lineno} ({len(keys)}키)")
    assert not own, (
        "v4 가 자체 RenderInputs 호출부를 가졌다: " + ", ".join(own) + "\n"
        "이제 v1·v3·v4 세 갈래를 비교해야 한다 — 이 파일의 원장을 3열로 넓혀라.")
