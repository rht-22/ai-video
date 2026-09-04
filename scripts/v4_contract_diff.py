"""V4-M1 합격선 — **바깥 계약 대조 도구**. 기획서 `docs/v4/v4-plan.md` §6 을 코드로 옮긴 것.

    python -m scripts.v4_contract_diff --job <job 디렉토리> [--against <다른 job>] [--json]

## 왜 이 도구가 있나

v3 가 현지화를 깨뜨린 방식은 "없는 파일을 만든 것"이 아니라 **기존 이름을 다른 모양으로
쓴 것**이다. `app/localize/apply.py:182~186` 은 `checkpoint_story.json` 의
`variants[*].title_text` 를 일본어로 갈아 끼우는데, v3 가 쓰는
`checkpoint_story.json` 은 `{"fingerprint", "story": {"title": {"line1","line2"}, …}}`
라 `variants` 도 `title_text` 도 없다(`app/v3/story.py:825~836` · `app/v3/pipeline.py:637`).
파일 이름이 같으니 아무도 안 죽고, JP 판에 한국어 제목이 그대로 번인된다.

⇒ **파일의 존재가 아니라 키의 존재를 기계로 본다.** 그래서 이 도구의 판정은 셋이다:

    없는 파일        skipped   — 그 단계를 안 돈 잡일 수 있다(v4 는 `--stop-after` 가 있다)
    깨진/모양 틀린 것 violation — 소비자가 KeyError 로 죽거나 조용히 빈 값을 쓴다
    통과            ok

## 필수 키의 근거

`CONTRACTS` 의 키는 **전부 이 저장소 안에 실제 소비자가 있다**(주석에 파일·줄을 적었다).
근거 없는 키는 넣지 않는다 — 근거 없는 계약은 지켜지지 않고, 지켜지지 않는 가드는
언젠가 통째로 꺼진다.

## 키 경로 문법

    layout.top_title              중첩 객체
    variants[].title_text         목록 안 항목마다
    variants[].clips[].start_sec  중첩 목록
    tts_cue_files[].cue.text      목록 → 객체 → 필드
    [].start_sec                  문서 뿌리가 목록인 파일(subtitle_segments.json)
    ?texts[].text                 선행 `?` = **선택 컨테이너**(컨테이너가 아예 없으면 통과,
                                  있으면 항목마다 검사)

목록이 **비어 있으면 통과**한다 — "항목이 없는 것"과 "항목의 키가 없는 것"은 다른 사건이다.
빈 목록을 위반으로 세면 아직 후보가 0인 잡·연출이 없는 편이 전부 빨갛게 되고, 그러면
사람이 이 표를 안 본다.

## --against

두 잡의 **키 집합 diff** 다(v1 잡 ↔ v4 잡 대조 — M1 합격선). 관측된 키를 같은 문법으로
뽑아 파일별로 맞댄다. ⚠ diff 자체는 **종료 코드를 바꾸지 않는다** — v4 는 키를 더하는
것이 정상이고(§6 "새 정보는 새 파일이나 새 키로만"), v1 에만 있는 키 중 지켜야 할 것은
이미 `CONTRACTS` 가 필수로 붙들고 있다. diff 는 "필수로 올릴 후보"를 사람에게 보여 주는
자리이지 자동 판정기가 아니다. 판정은 `CONTRACTS` 하나다.

종료 코드: 위반이 하나라도 있으면 1(A·B 어느 쪽이든), 아니면 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ── 계약 표 ────────────────────────────────────────────────────────────────
#
# 기획서 §6 "바깥 계약 — 이름과 모양을 지키는 파일" 그대로. 각 키 옆의 주석이
# **누가 그 키를 읽는가**다. 하드(KeyError 로 죽는다)와 소프트(조용히 빈 값을 쓴다)를
# 구분해 적었다 — 소프트가 더 위험하다(v3 사고가 전부 소프트였다).

CONTRACTS: dict[str, tuple[str, ...]] = {

    # 실행 기록. 재개·집계·레이블 매칭이 전부 이 파일로 잡을 식별한다.
    "run_log.json": (
        # app/replay/loader.py:_pipeline_marker — v1("")·v3·v4 를 이 값으로 가른다.
        # 없으면 v4 편이 v1 로 집계돼 §3 분포가 파이프라인별로 안 쪼개진다(소프트).
        "pipeline",
        # app/replay/loader.py:_record_from_jobdir — run_id 정본(소프트, 디렉토리명 폴백).
        "job_id",
        # app/replay/loader.py(git_sha·config.app) · app/localize/rerender.py:37
        # (gen_flags_for_job 이 provenance.config.app 으로 원 런의 컷을 재현한다 —
        #  이게 없으면 JP 재렌더가 다른 컷을 내고 L4 길이 대조가 편을 죽인다).
        "provenance",
        # app/localize/rerender.py:233 `run_log["input"]["video_path"]` — 하드(KeyError).
        # app/replay/exception_score.py:137 match_job_to_label 도 이 값으로 레이블을 잇는다.
        "input.video_path",
        # 단계별 감사 기록. ves 오케스트레이터가 steps[{step:"resources"}].tts_backend 등을
        # 읽는다(CLAUDE.md E11·E17 계약) + app/modules/job.py 가 이 배열에 append 한다.
        "steps",
    ),

    # 렌더 정본이자 현지화·v1 재개·집계가 함께 읽는 파일. **v3 가 깨뜨린 그 파일이다.**
    "checkpoint_story.json": (
        # app/localize/apply.py:183~184 — JP 제목을 여기에 덮어쓴다(variants 분기).
        # app/pipeline.py:3472 `v["title_text"]` — v1 render 재개, 하드(KeyError).
        "variants[].title_text",
        # app/pipeline.py:3470~3471 `v["clips"]` → StoryClip(**c), 하드.
        "variants[].clips",
        # StoryClip(app/modules/story_builder.py:7~12)의 필수 5필드 — 하나라도 없으면
        # v1 재개가 TypeError 로 죽는다. app/replay/metrics.py:clip_span 도 start/end 를 읽는다.
        "variants[].clips[].role",
        "variants[].clips[].start_sec",
        "variants[].clips[].end_sec",
        "variants[].clips[].subtitle",
        "variants[].clips[].use_original_audio",
        # 하위 호환 키(v1 이 variants 와 **함께** 쓴다 — app/pipeline.py:3444~3446).
        # app/localize/apply.py:186 의 variants 없는 분기 · app/pipeline.py:3477~3478 ·
        # app/replay/loader.py:180 `cs.get("clips")`(없으면 그 편이 집계 0으로 빠진다).
        "title_text",
        "clips",
    ),

    # 오케스트레이터·편집실이 읽는 발행 기록. 어댑터 계약 C1(app/v3/assemble.py:4).
    "edit_plan.json": (
        # app/localize/apply.py:191 `plan["layout"]["top_title"]=` (layout 없으면 하드) ·
        # app/localize/meta.py:35 검수 카드 대역 · app/pipeline.py:3480 재개, 하드.
        "layout.top_title",
        # C1 동결 키(app/v3/assemble.py:4). app/localize/apply.py:192 가 작품 표기를
        # 여기에 쓴다 — 키 자리가 없으면 JP 판 하단 라벨이 한국어로 남는다.
        "layout.bottom_label",
        # app/pipeline.py:3486~3490 재개 — 다섯 키 전부 하드(KeyError).
        # app/replay/metrics.py:clip_span 은 clip_start_sec 유무로 표기를 가른다.
        "timeline[].role",
        "timeline[].clip_start_sec",
        "timeline[].clip_end_sec",
        "timeline[].subtitle",
        "timeline[].use_original_audio",
    ),

    # 뿌리가 목록인 파일. 편집본 좌표 {start_sec, end_sec, text} 세 필드가 계약이다(C6).
    "subtitle_segments.json": (
        # app/localize/translate.py:178 `s["start_sec"]`·`s["end_sec"]`·`s["text"]` — 하드.
        # app/localize/apply.py:154~165 · app/modules/edit_overrides.py:721(편집실 왕복이
        # 이 3필드로 정규화된다 — 모양이 다르면 사람이 고친 자막이 되돌아오지 않는다).
        "[].start_sec",
        "[].end_sec",
        "[].text",
    ),

    # TTS 재료. JP 재합성(L3t)이 이 파일만 보고 mp3 를 다시 만든다.
    "checkpoint_resources.json": (
        # app/localize/narration.py:106~118 — `c["path"]`·`c["cue"]`·`c["cue_index"]`·
        # `cue["start_sec"]`·`cue["end_sec"]`·`cue["text"]` 전부 하드(KeyError).
        # app/localize/translate.py:168 `c["cue"]["text"]` 도 하드.
        "tts_cue_files[].cue_index",
        "tts_cue_files[].path",
        "tts_cue_files[].cue.text",
        "tts_cue_files[].cue.start_sec",
        "tts_cue_files[].cue.end_sec",
    ),

    # E15 AI 연출. **파일 자체가 선택**이다(style_compose 를 안 켠 채널엔 없다 → skipped).
    "checkpoint_style.json": (
        # app/modules/style_compose.py:397~399 — 스키마가 다르면 StylePlanError 로 크게 실패.
        # 🛑 **값까지** 본다. 있기만 보던 때, v4 의 `schema="v3_style/v1"` 문서가 이
        # 이름으로 통과했다(실측 2026-09-04) — E16 이 조용히 no-op 할 파일이 합격
        # 도장을 받았다. 이름이 같아도 신원이 다르면 다른 문서다.
        "schema=style_plan/v1",
        # app/localize/style_texts.py:57(style_plan_strings) → apply_style_translation.
        # 목록 자체는 선택이지만(연출에 효과 텍스트가 없을 수 있다), **있으면** 항목마다
        # text 가 있어야 한다 — 없으면 그 자리가 빈 문자열로 번역돼 화면에서 글자가 사라진다.
        "?texts[].text",
        "?title_segments[].text",
    ),

    # v3/v4 Stage 4 연출 문서. ⚠ 위 `checkpoint_style.json` 과 **다른 파일이고 다른
    # 어휘다** — 그 이름은 E15(texts·title_fixed·title_segments)의 것이고, 이쪽은
    # v3 Stage 4 어휘(design·beats·labels·diff·notes)다. 한 이름에 두 모양을 얹지
    # 않으려고 파일을 나눴다(app/v4/render.py STYLE_STEM 🛑). v4 잡에는 이 파일이
    # 있고 `checkpoint_style.json` 은 없다 — 그래서 현지화 E16 은 연출을 안 켠
    # 채널과 똑같이 지나간다(정직한 부재).
    "style.json": (
        # 반대 방향도 못박는다 — E15 문서가 이 이름으로 오면 `design` 이 없어 걸리고,
        # 걸리기 전에 신원에서 먼저 걸린다.
        "schema=v3_style/v1",
        # app/v3/finalize.py:312 design_from_style(style_doc.get("design") or {}) —
        # 없으면 조용히 빈 dict 가 되어 채널 프리셋이 통째로 증발한다(전 편이
        # 엔진 기본 디자인으로 나간다). 렌더가 화면을 그리는 유일한 재료다.
        "design",
    ),

    # 정본 격자. 6c 검증·편집실 스냅·assemble 이 같은 눈금을 본다.
    "grid.json": (
        # app/modules/grid/timegrid.py:154~161 grid_snap_times — 하드(KeyError).
        "source.duration_sec",
        "scene_cuts",
        # app/v3/overrides.py:79~83 spans_in_window(편집실 컷 스냅) · grid_snap_times.
        "span_candidates[].id",
        "span_candidates[].t_in",
        "span_candidates[].t_out",
        # app/modules/grid/timegrid.py:38 carve_spans · app/v3/assemble.py:307
        # word_subtitles(어절 자막) · app/v3/chunk_analyze.py:630 전사 대조.
        "words[].t0",
        "words[].t1",
        "words[].text",
    ),

    # v4 신설(§6). 편집실·성과 조인이 읽고, app/replay/loader.py:250 은 이 파일의 존재를
    # "편성까지는 돌았다"의 증거로 본다. 키는 M1 시점에 **이미 있는 모듈의 어휘**만 적는다.
    "checkpoint_candidates.json": (
        "schema",
        # id 는 6c·7·8·9 를 잇는 좌표다 — app/v4/verify.py:365(중복이면 ValueError) ·
        # app/v4/funnel.py:507 · app/v4/approve.py:189~(flags 키가 곧 id).
        "candidates[].id",
        # app/v4/verify.py:111~119 _parse_span — 읽을 수 없으면 그 조각을 드롭한다.
        "candidates[].segments[].start_sec",
        "candidates[].segments[].end_sec",
        # 순위 순 승인 id 목록 — `shorts_{n}.mp4` 번호가 이 순서다(§6 바깥 계약).
        # ⚠ 자리는 **`approval` 절 안**이다. 파이프라인은 단계마다 절을 두고
        # (boundary·verify·funnel·flags·approval) 그 안에 산출을 담는다 — 최상위
        # `approved` 로 적었던 첫 판은 실잡에서 위반으로 떴고, 틀린 것은 파일이
        # 아니라 이 표였다.
        "approval.approved",
    ),
}

# 뿌리가 목록인 파일 — 키 경로가 `[]` 로 시작한다.
_ROOT_LIST_PREFIX = "[]"

# 승인 편이 여럿인 것이 v4 의 성질이다(운영자 결정 O7) — 1위는 `edit_plan.json`,
# 2위↓ 는 `edit_plan_2.json`… 이다(§6 표의 `· _{n}` · app/replay/loader.py:113~).
# 2위 이하가 1위와 다른 모양이면 그 편만 조용히 깨지므로 **같은 계약으로 함께 본다**.
# ⚠ v3 훅 변형 `edit_plan_variant_{k}.json` 은 숫자 접미가 아니라 여기 안 걸린다
#   (loader 와 같은 판별 — 그건 승인 편이 아니다).
NUMBERED_SIBLINGS = ("edit_plan.json", "checkpoint_style.json", "style.json")


def contract_keys_for(name: str) -> tuple[str, ...] | None:
    """파일명 → 그 파일에 걸린 계약 키. 계약 밖 파일이면 None. 순수.

    `edit_plan_2.json` 처럼 숫자 접미가 붙은 승인 2위↓ 파일은 **원본과 같은 키**를 받는다.
    """
    if name in CONTRACTS:
        return CONTRACTS[name]
    if name.endswith(".json"):
        stem = name[: -len(".json")]
        base, _, suffix = stem.rpartition("_")
        if base and suffix.isdigit():
            parent = f"{base}.json"
            if parent in NUMBERED_SIBLINGS and parent in CONTRACTS:
                return CONTRACTS[parent]
    return None


# ── 키 경로 파서 (순수) ─────────────────────────────────────────────────────

def split_expected(path: str) -> tuple[str, str | None]:
    """`키=값` → (키, 기대값). 값이 없으면 (키, None). 순수.

    🛑 **이 문법이 있는 이유**(2026-09-04 실측): 계약 표가 `schema` 를 '있는가'로만
    보던 시절, v4 의 연출 문서(`schema="v3_style/v1"`)를 E15 계약 이름
    `checkpoint_style.json` 에 넣었더니 도구가 **통과시켰다** — 키는 있고 나머지
    E15 목록은 선택이라 하나도 안 걸렸다. 그대로였다면 현지화 E16 이 조용히
    no-op 하는 파일이 계약 합격 도장을 받고 나갔다. 이름이 같아도 **신원이 다르면**
    다른 문서다. 그 신원을 확인하는 자리가 여기다."""
    raw = str(path)
    if "=" not in raw:
        return raw, None
    key, _, want = raw.partition("=")
    if not key or not want:
        raise ValueError(f"`키=값` 의 양쪽이 다 있어야 한다: {path!r}")
    if "=" in want:
        raise ValueError(f"기대값에 '=' 를 또 쓸 수 없다: {path!r}")
    return key, want


def parse_key_path(path: str) -> tuple[bool, tuple[tuple[str, str], ...]]:
    """키 경로 → (선택 컨테이너인가, 스텝 목록). `=값` 꼬리는 여기서 떼고 본다.

    스텝은 두 종류다:
      ("field", 이름)  — 객체에서 그 이름을 꺼낸다
      ("each", 이름)   — 그 이름의 값이 목록이고, 항목마다 이어서 본다
                         (이름 "" 이면 문서 뿌리가 목록이라는 뜻)

    문법 위반은 즉시 실패한다 — 계약 표의 오타가 조용히 '검사 안 함'이 되면
    이 도구가 있는 이유가 사라진다.
    """
    raw, _want = split_expected(path)
    optional = raw.startswith("?")
    if optional:
        raw = raw[1:]
    if not raw:
        raise ValueError(f"빈 키 경로: {path!r}")

    steps: list[tuple[str, str]] = []
    if raw.startswith(_ROOT_LIST_PREFIX):
        steps.append(("each", ""))
        raw = raw[len(_ROOT_LIST_PREFIX):]
        if raw.startswith("."):
            raw = raw[1:]
        elif raw:
            raise ValueError(f"뿌리 목록 뒤에는 '.' 이 와야 한다: {path!r}")
    for part in [p for p in raw.split(".") if p != ""] if raw else []:
        if part.endswith(_ROOT_LIST_PREFIX):
            name = part[: -len(_ROOT_LIST_PREFIX)]
            if not name:
                raise ValueError(f"이름 없는 목록 스텝: {path!r}")
            steps.append(("each", name))
        else:
            if "[" in part or "]" in part:
                raise ValueError(f"키 경로 문법 오류: {path!r}")
            steps.append(("field", part))
    if not steps:
        raise ValueError(f"스텝이 없는 키 경로: {path!r}")
    return optional, tuple(steps)


def check_key(doc: Any, path: str) -> dict:
    """문서 하나에서 키 경로 하나를 본다 → 판정 dict. 순수(입력을 고치지 않는다).

    반환 `verdict`:
      "ok"                통과(빈 목록 통과 포함)
      "container_absent"  중간 컨테이너가 없다 — `?` 면 통과로 접힌다
      "missing"           항목/객체 안에 그 필드가 없다
      "not_object"        객체여야 할 자리가 객체가 아니다
      "not_list"          목록이어야 할 자리가 목록이 아니다

    `where` 는 실제로 걸린 자리다(예: `variants[0].clips[2]`) — 어느 항목이
    문제인지 안 적으면 사람이 파일을 눈으로 훑어야 한다.
    """
    optional, steps = parse_key_path(path)
    verdict, where = _walk(doc, steps, "")
    if optional and verdict == "container_absent":
        verdict, where = "ok", ""
    _key, want = split_expected(path)
    got = None
    if verdict == "ok" and want is not None:
        # 값 대조는 **통과한 뒤**에만 한다 — 키가 없는 것과 값이 다른 것은 다른
        # 사건이고, 사람이 고칠 곳도 다르다.
        got = _value_at(doc, steps)
        if str(got) != want:
            verdict, where = "value_mismatch", _key.lstrip("?")
    out = {"key": path, "verdict": verdict, "where": where,
           "ok": verdict == "ok", "optional": optional}
    if want is not None:
        out["expected"], out["got"] = want, got
    return out


def _value_at(node: Any, steps: tuple[tuple[str, str], ...]) -> Any:
    """스텝을 따라 내려가 값 하나를 꺼낸다. `_walk` 가 ok 를 준 뒤에만 부른다.

    목록 스텝(`each`)이 섞인 경로는 값이 여럿이라 대조 대상이 아니다 — 그런 표는
    문법 검사에서 막는다(아래 `CONTRACTS` 검증)."""
    cur = node
    for kind, name in steps:
        if kind != "field":
            raise ValueError("목록 스텝이 있는 경로에는 기대값을 붙일 수 없다")
        cur = cur[name]
    return cur


def _walk(node: Any, steps: tuple[tuple[str, str], ...], trail: str) -> tuple[str, str]:
    """스텝을 따라 내려가며 첫 위반을 돌려준다. 결정적 — 항목은 인덱스 순으로 본다."""
    if not steps:
        return "ok", ""
    (kind, name), rest = steps[0], steps[1:]
    last = not rest

    if kind == "each":
        if name:
            if not isinstance(node, dict):
                return "not_object", trail or "<root>"
            if name not in node:
                # 목록 컨테이너 자체가 없다 — 마지막 스텝이든 아니든 '컨테이너 부재'다
                # (`?` 로 접을 수 있는 자리는 여기 하나뿐이다).
                return "container_absent", _join(trail, name)
            child, ctrail = node[name], _join(trail, name)
        else:
            child, ctrail = node, trail or "<root>"
        if not isinstance(child, list):
            return "not_list", ctrail
        # 빈 목록은 통과 — "항목이 없는 것"과 "항목의 키가 없는 것"은 다른 사건이다.
        for i, item in enumerate(child):
            v, w = _walk(item, rest, f"{ctrail}[{i}]")
            if v != "ok":
                return v, w
        return "ok", ""

    # kind == "field"
    if not isinstance(node, dict):
        return "not_object", trail or "<root>"
    if name not in node:
        # 마지막 필드가 없으면 "missing", 중간 객체가 없으면 "container_absent".
        return ("missing" if last else "container_absent"), _join(trail, name)
    if last:
        return "ok", ""
    return _walk(node[name], rest, _join(trail, name))


def _join(trail: str, name: str) -> str:
    return f"{trail}.{name}" if trail else name


# ── 파일·잡 단위 판정 (순수) ────────────────────────────────────────────────

def check_document(name: str, doc: Any,
                   keys: tuple[str, ...] | None = None) -> dict:
    """이미 읽어 둔 문서 하나 → 파일 판정. 순수(IO 없음 — 테스트가 여기를 친다)."""
    if keys is None:
        keys = contract_keys_for(name)
        if keys is None:
            raise KeyError(f"계약 표에 없는 파일: {name}")
    results = [check_key(doc, k) for k in keys]
    bad = [r for r in results if not r["ok"]]
    return {"file": name,
            "status": "violation" if bad else "ok",
            "checked": len(results),
            "violations": bad}


def check_job(docs: dict[str, Any], *, present: dict[str, str] | None = None,
              unreadable: dict[str, str] | None = None,
              job: str = "") -> dict:
    """읽어 온 문서 지도 → 잡 판정. 순수.

    docs        파일명 → 파싱된 문서(있는 파일만)
    unreadable  파일명 → 사유(있지만 JSON 이 아닌 파일). **skipped 가 아니라 위반**이다 —
                파일이 있는데 못 읽는 것은 그 단계를 안 돈 것과 완전히 다른 사건이다.
    present     파일명 → 실제 경로(보고용, 선택)
    """
    unreadable = unreadable or {}
    present = present or {}
    files: list[dict] = []
    # 계약 표 순서 + 실제로 발견된 숫자 접미 형제(정렬 — 결정성).
    extra = sorted(n for n in (set(docs) | set(unreadable)) if n not in CONTRACTS)
    for name in list(CONTRACTS) + extra:
        if name in unreadable:
            files.append({"file": name, "status": "unreadable",
                          "reason": unreadable[name], "checked": 0, "violations": []})
            continue
        if name not in docs:
            # 없는 파일은 위반이 아니다 — `--stop-after probe` 로 멈춘 잡, 연출을 안 켠
            # 채널, 후보 편성이 실패해 렌더까지 못 간 잡이 전부 정상적으로 여기 온다.
            files.append({"file": name, "status": "skipped",
                          "reason": "파일 없음 — 그 단계를 안 돌았을 수 있다",
                          "checked": 0, "violations": []})
            continue
        r = check_document(name, docs[name])
        if name in present:
            r["path"] = present[name]
        files.append(r)
    n_bad = sum(1 for f in files if f["status"] in ("violation", "unreadable"))
    return {"job": job,
            "files": files,
            "ok": n_bad == 0,
            "counts": {
                "ok": sum(1 for f in files if f["status"] == "ok"),
                "skipped": sum(1 for f in files if f["status"] == "skipped"),
                "violation": sum(1 for f in files if f["status"] == "violation"),
                "unreadable": sum(1 for f in files if f["status"] == "unreadable"),
            }}


# ── 관측 키 집합 · diff (순수) ──────────────────────────────────────────────

MAX_OBSERVE_DEPTH = 6      # 계약 표의 가장 깊은 경로가 4다 — 여유 두 칸.
MAX_OBSERVE_ITEMS = 200    # 목록 항목 표본 상한. 키의 합집합을 보는 것이라 전량이 필요 없다.


def observed_key_paths(doc: Any, *, depth: int = MAX_OBSERVE_DEPTH) -> set[str]:
    """문서 → 관측된 키 경로 집합(계약 표와 **같은 문법**). 순수·결정적.

    목록은 항목의 키를 **합집합**으로 본다 — v1 은 `reframe` 이 있는 클립과 없는 클립을
    섞어 쓰므로(app/pipeline.py:5326) 첫 항목만 보면 diff 가 실행마다 달라진다.
    """
    out: set[str] = set()
    _observe(doc, "", out, depth)
    return out


def _observe(node: Any, prefix: str, out: set[str], depth: int) -> None:
    if depth <= 0:
        return
    if isinstance(node, dict):
        for k in node:
            path = f"{prefix}.{k}" if prefix else str(k)
            out.add(path)
            _observe(node[k], path, out, depth - 1)
    elif isinstance(node, list):
        path = f"{prefix}[]" if prefix else _ROOT_LIST_PREFIX
        for item in node[:MAX_OBSERVE_ITEMS]:
            # 목록은 키 층이 아니라 같은 층의 반복이므로 dict 항목에서는 depth 를 쓰지
            # 않는다. 다만 목록 안 목록은 깊이를 줄여 재귀가 자료 깊이에 갇히게 한다.
            if isinstance(item, dict):
                _observe(item, path, out, depth)
            elif isinstance(item, list):
                _observe(item, path, out, depth - 1)


def diff_key_sets(a: dict[str, Any], b: dict[str, Any], *,
                  label_a: str = "A", label_b: str = "B") -> dict:
    """두 잡의 문서 지도 → 파일별 키 집합 diff. 순수·결정적(정렬된 목록).

    계약 파일만 본다 — 잡 디렉토리에는 중간 산출이 잔뜩 있고, 그것까지 diff 하면
    "지켜야 하는 것"이 소음에 묻힌다.
    """
    rows: list[dict] = []
    extra = sorted(n for n in (set(a) | set(b)) if n not in CONTRACTS)
    for name in list(CONTRACTS) + extra:
        in_a, in_b = name in a, name in b
        if not in_a and not in_b:
            continue
        ka = observed_key_paths(a[name]) if in_a else set()
        kb = observed_key_paths(b[name]) if in_b else set()
        rows.append({
            "file": name,
            "present": {label_a: in_a, label_b: in_b},
            f"only_in_{label_a}": sorted(ka - kb),
            f"only_in_{label_b}": sorted(kb - ka),
            "shared": len(ka & kb),
            # 계약 필수 키 중 A 에만 있는 것 — "필수로 올릴 후보"가 아니라
            # **이미 필수인데 B 가 안 낸 것**이다. B 의 위반 목록과 같은 사건을 가리킨다.
            "contract_only_in_a": sorted(
                k for k in _contract_leaf_paths(name) if k in ka and k not in kb),
        })
    return {"label_a": label_a, "label_b": label_b, "files": rows}


def _contract_leaf_paths(name: str) -> set[str]:
    """계약 키 경로 → 관측 문법 표기(선행 `?` 를 뗀 것). 두 표기를 맞대기 위한 변환."""
    return {k[1:] if k.startswith("?") else k for k in (contract_keys_for(name) or ())}


# ── 보고 (순수) ────────────────────────────────────────────────────────────

_VERDICT_KO = {
    "missing": "키 없음",
    "container_absent": "컨테이너 없음",
    "not_object": "객체가 아님",
    "not_list": "목록이 아님",
}


def render_report(result: dict, diff: dict | None = None) -> str:
    """사람이 읽는 보고. 위반 키 **이름을 반드시 적는다** — 개수만 적으면 못 고친다."""
    lines = [f"# v4 계약 대조 — {result.get('job') or '(job)'}", ""]
    c = result["counts"]
    lines.append(f"통과 {c['ok']} · 위반 {c['violation']} · 읽기 실패 {c['unreadable']} "
                 f"· 건너뜀 {c['skipped']}(파일 없음)")
    lines.append("")
    lines.append("| 파일 | 판정 | 검사 | 비고 |")
    lines.append("|---|---|---|---|")
    for f in result["files"]:
        note = f.get("reason", "")
        if f["violations"]:
            note = f"{len(f['violations'])}건 위반"
        lines.append(f"| `{f['file']}` | {f['status']} | {f['checked']} | {note} |")
    bad = [f for f in result["files"] if f["violations"]]
    if bad:
        lines += ["", "## 위반 상세", ""]
        for f in bad:
            lines.append(f"### `{f['file']}`")
            for v in f["violations"]:
                ko = _VERDICT_KO.get(v["verdict"], v["verdict"])
                at = f" (자리: `{v['where']}`)" if v["where"] else ""
                lines.append(f"- `{v['key']}` — {ko}{at}")
            lines.append("")
    unread = [f for f in result["files"] if f["status"] == "unreadable"]
    if unread:
        lines += ["## 읽기 실패", ""]
        for f in unread:
            lines.append(f"- `{f['file']}` — {f.get('reason','')}")
        lines.append("")
    if diff is not None:
        lines += _render_diff(diff)
    return "\n".join(lines).rstrip() + "\n"


def _render_diff(diff: dict) -> list[str]:
    a, b = diff["label_a"], diff["label_b"]
    lines = ["## 키 집합 diff", "",
             f"⚠ diff 는 종료 코드를 바꾸지 않는다 — 키를 더하는 것은 정상이고"
             f"(§6 '새 정보는 새 키로만'), 지켜야 할 것은 위 계약 표가 붙들고 있다.", "",
             f"| 파일 | {a} 에만 | {b} 에만 | 공통 |", "|---|---|---|---|"]
    for row in diff["files"]:
        only_a = row[f"only_in_{a}"]
        only_b = row[f"only_in_{b}"]
        lines.append(f"| `{row['file']}` | {len(only_a)} | {len(only_b)} | {row['shared']} |")
    lines.append("")
    for row in diff["files"]:
        only_a = row[f"only_in_{a}"]
        only_b = row[f"only_in_{b}"]
        if not only_a and not only_b and not row["contract_only_in_a"]:
            continue
        lines.append(f"### `{row['file']}`")
        if row["contract_only_in_a"]:
            lines.append(f"- 🛑 **계약 필수인데 {b} 에 없음**: "
                         + ", ".join(f"`{k}`" for k in row["contract_only_in_a"]))
        if only_a:
            lines.append(f"- {a} 에만: " + ", ".join(f"`{k}`" for k in only_a))
        if only_b:
            lines.append(f"- {b} 에만: " + ", ".join(f"`{k}`" for k in only_b))
        lines.append("")
    return lines


# ── IO (얇게) ──────────────────────────────────────────────────────────────

def load_job(job: Path) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """job 디렉토리 → (문서 지도, 경로 지도, 읽기 실패 지도). 계약 파일만 읽는다."""
    job = Path(job)
    if not job.is_dir():
        raise NotADirectoryError(f"job 디렉토리가 아니다: {job}")
    docs: dict[str, Any] = {}
    present: dict[str, str] = {}
    unreadable: dict[str, str] = {}
    for name in list(CONTRACTS) + _numbered_present(job):
        p = job / name
        if not p.exists():
            continue
        present[name] = str(p)
        try:
            docs[name] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            unreadable[name] = f"{type(e).__name__}: {e}"
    return docs, present, unreadable


def _numbered_present(job: Path) -> list[str]:
    """디렉토리에 실제로 있는 숫자 접미 형제(`edit_plan_2.json` …). 정렬 — 결정성."""
    found: set[str] = set()
    for base in NUMBERED_SIBLINGS:
        if base not in CONTRACTS:
            continue
        stem = base[: -len(".json")]
        for f in job.glob(f"{stem}_*.json"):
            if contract_keys_for(f.name) is not None:
                found.add(f.name)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="v4_contract_diff",
        description="v4 바깥 계약(기획서 §6) 대조 — 필수 키 검사 + 잡 간 키 집합 diff")
    ap.add_argument("--job", required=True, help="검사할 job 디렉토리")
    ap.add_argument("--against", default=None,
                    help="키 집합을 맞댈 다른 job 디렉토리(v1 잡 ↔ v4 잡)")
    ap.add_argument("--json", action="store_true", help="기계 판독용 JSON 출력")
    args = ap.parse_args(argv)

    docs, present, unreadable = load_job(Path(args.job))
    result = check_job(docs, present=present, unreadable=unreadable,
                       job=str(Path(args.job)))
    doc: dict[str, Any] = {"job": result}

    if args.against:
        b_docs, b_present, b_unreadable = load_job(Path(args.against))
        against = check_job(b_docs, present=b_present, unreadable=b_unreadable,
                            job=str(Path(args.against)))
        doc["against"] = against
        doc["diff"] = diff_key_sets(docs, b_docs, label_a="job", label_b="against")
        ok = result["ok"] and against["ok"]
    else:
        ok = result["ok"]

    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
    else:
        print(render_report(result, doc.get("diff")))
        if "against" in doc:
            print(render_report(doc["against"]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
