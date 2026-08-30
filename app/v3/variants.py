"""M6-A — 훅 변형 모드 (§9-C: --hook-variants N).

같은 edit_plan 에서 **훅 비트·제목·훅 내레이션만** 교체한 N버전을 만든다 — 본편
스토리(다른 비트의 span 편성)는 불변이라 성과 차이가 곧 훅의 차이다(변형 간
상대 비교 — 집계 지표라 초 단위 이탈 곡선이 없는 한계의 우회).

Flash 1회가 대안 훅 N개를 함께 제안(span id 연속 범위 — story 와 같은 규율:
시각 무출력·재질의 ≤2회). 변형 신원 `variant_id` 는 edit_plan additive 로 실려
성과 조인(scripts/feedback_report.py)의 키가 된다. TTS 합성은 하지 않는다 —
변형 렌더 시점(resources)의 일이고, cue 계획은 텍스트·창까지 완결이다.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.modules.gemini_client import _extract_json_from_markdown, _loads_first_json
from app.v3 import story as st
from app.v3.seq_analyze import MAX_REASKS

VARIANTS_MAX = 3


def validate_variants_response(resp: Any, span_index: dict[str, dict],
                               story_doc: dict, n: int) \
        -> tuple[list[dict] | None, list[str]]:
    """모델 응답 → (변형 목록 | None, 반려 사유). 순수.

    훅 span 은 분석 재료의 grid 연속 범위여야 하고, 본편의 **훅 아닌 비트**가 쓰는
    span 과 겹치면 안 된다(본편 불변 원칙)."""
    problems: list[str] = []
    if not isinstance(resp, dict) or not isinstance(resp.get("variants"), list):
        return None, ["variants 배열이 없다"]
    non_hook_spans = {s for b in story_doc.get("beats") or []
                      if b.get("role") != "hook" for s in b.get("span_ids") or []}
    pos_of = {sid: span_index[sid]["pos"] for sid in span_index}
    out: list[dict] = []
    for k, v in enumerate(resp["variants"][:n]):
        if not isinstance(v, dict):
            problems.append(f"variants[{k}] 가 객체가 아님")
            continue
        title = v.get("title") or {}
        line1 = str(title.get("line1") or "").strip()
        line2 = str(title.get("line2") or "").strip()
        if not line1 or not line2:
            problems.append(f"variants[{k}] title 은 line1/line2 두 줄 필요")
            continue
        for name, line in (("line1", line1), ("line2", line2)):
            if len(line) > st.TITLE_MAX_CHARS:
                problems.append(f"variants[{k}] title.{name} {len(line)}자 — "
                                f"{st.TITLE_MAX_CHARS}자 이내로")
        ids = v.get("hook_span_ids")
        if not isinstance(ids, list) or not ids \
                or not all(isinstance(s, str) for s in ids):
            problems.append(f"variants[{k}] hook_span_ids 는 문자열 id 배열")
            continue
        unknown = [s for s in ids if s not in pos_of]
        if unknown:
            problems.append(f"variants[{k}] 모르는/분석 밖 span: {unknown[:5]}")
            continue
        positions = [pos_of[s] for s in ids]
        if positions != sorted(positions) \
                or any(b - a != 1 for a, b in zip(positions, positions[1:])):
            problems.append(f"variants[{k}] hook_span_ids 가 grid 연속 범위가 아니다")
            continue
        reused = [s for s in ids if s in non_hook_spans]
        if reused:
            problems.append(f"variants[{k}] 본편 비트의 span 재사용: {reused[:5]} — "
                            "본편 불변 원칙(훅만 교체)")
            continue
        narration = v.get("narration")
        narration = str(narration).strip() if isinstance(narration, str) \
            and str(narration).strip() else None
        out.append({"title": {"line1": line1, "line2": line2},
                    "hook_span_ids": list(ids), "narration": narration})
    if problems:
        return None, problems
    return out, []


def apply_hook_variant(story_doc: dict, variant: dict,
                       span_index: dict[str, dict], variant_id: str) -> dict:
    """본편 story 문서 + 변형 → 변형 story 문서(훅 비트·제목만 교체). 순수.

    TTS 슬롯은 전 비트 재배치(plan_narration_slots) — 결정 로직이라 훅 아닌 비트의
    cue 는 같은 자리에 다시 선다."""
    beats = [dict(b, span_ids=list(b["span_ids"])) for b in story_doc["beats"]]
    hook_idx = next((i for i, b in enumerate(beats) if b.get("role") == "hook"), None)
    if hook_idx is None:
        raise ValueError("훅 비트가 없다 — 훅 변형을 만들 수 없다")
    beats[hook_idx]["span_ids"] = list(variant["hook_span_ids"])
    beats[hook_idx]["narration"] = variant.get("narration")
    cues, dropped = st.plan_narration_slots(beats, span_index)
    conflicts = st.verify_tts_conflicts(cues, beats, span_index)
    if conflicts:
        raise AssertionError(f"변형 TTS 충돌 벨트 위반: {conflicts}")
    doc = {**story_doc,
           "variant_id": variant_id,
           "variant_of": "hook",
           "title": variant["title"],
           "beats": st._beat_doc(beats, span_index),
           "narration_cues": cues,
           "narration_dropped": dropped}
    return doc


PROMPT = """당신은 리캡 쇼츠 훅 전문가다. 아래 확정 편성에서 **훅 비트만** 다른 버전으로 바꾼 대안 {n}개를 제안하라. 본편(훅 아닌 비트)은 불변 — 그 span 들은 쓸 수 없다.

## 확정 편성
제목: {title_line1} / {title_line2}
{beats_block}

## 규칙
1. 대안마다: 새 훅 span 연속 범위(재료 목록에서·본편 span 재사용 금지) + 새 제목 2줄(각 {title_max}자) + 훅 내레이션 1문장(서술체 ~했어요/~했죠, 없어도 됨).
2. 서론 금지 — 인사말·상황 설명 대사는 훅이 될 수 없다. 사건 한복판·강한 대사·궁금증 유발이 훅이다.
3. 대안끼리는 **서로 다른 가설**이어야 한다(같은 장면의 어절 바꿈 금지) — 스와이프 잔존(kept_watching_rate) 상대 비교가 목적이다.
{reject_block}
## 재료 — 훅 후보 span (id | 유성/무성 | importance | 내용)
{material_block}

## 출력 (JSON 만)
{{"variants": [
  {{"title": {{"line1": "…", "line2": "…"}}, "hook_span_ids": ["sp0001", "sp0002"], "narration": "…"}}
]}}"""


def build_variants_prompt(story_doc: dict, stage2_doc: dict,
                          span_index: dict[str, dict], n: int,
                          reject_note: str = "") -> str:
    beats_block = "\n".join(
        f"- beat{b['number']} {b['role']}: {b['time']['start'][3:]}~"
        f"{b['time']['end'][3:]} (spans {b['span_ids'][0]}~{b['span_ids'][-1]})"
        + (" ← 교체 대상" if b["role"] == "hook" else "")
        for b in story_doc.get("beats") or [])
    reject_block = ""
    if reject_note:
        reject_block = f"\n## ⚠ 직전 제안 반려 — 고쳐서 다시\n{reject_note}\n"
    return PROMPT.format(
        n=n, title_line1=story_doc["title"]["line1"],
        title_line2=story_doc["title"]["line2"],
        beats_block=beats_block, title_max=st.TITLE_MAX_CHARS,
        reject_block=reject_block,
        material_block=st.build_material_block(stage2_doc, span_index))


def run_hook_variants(gemini, story_doc: dict, stage2_doc: dict, grid: dict, *,
                      n: int, log=print) -> tuple[list[dict], dict]:
    """훅 변형 N개 생성 → (변형 story 문서 목록, 감사). Flash 1회(+재질의 ≤2)."""
    n = max(1, min(int(n), VARIANTS_MAX))
    span_index, _order = st.build_span_index(stage2_doc, grid)
    audit: dict[str, Any] = {"attempts": [], "requested": n}
    variants: list[dict] | None = None
    reject_note = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = build_variants_prompt(story_doc, stage2_doc, span_index, n,
                                       reject_note)
        log(f"  [v3/variants] Flash 훅 대안 요청 (시도 {attempt + 1}/{1 + MAX_REASKS})")
        t0 = time.time()
        problems: list[str] = []
        try:
            resp = st._call_story_model(gemini, prompt)
            variants, problems = validate_variants_response(
                resp, span_index, story_doc, n)
        except ValueError as e:
            variants, problems = None, [f"응답 오류: {e}"]
        audit["attempts"].append({"attempt": attempt + 1,
                                  "elapsed": round(time.time() - t0, 1),
                                  "problems": problems})
        if variants is not None:
            break
        log(f"  [v3/variants] 반려 — 사유 {len(problems)}건")
        reject_note = "\n".join(f"- {p}" for p in problems[:15])
    if variants is None:
        log("  [v3/variants] ⚠ 재질의 소진 — 변형 없이 본편만(커버리지 표기)")
        audit["failed"] = True
        return [], audit
    docs = [apply_hook_variant(story_doc, v, span_index, f"hook_v{k + 1}")
            for k, v in enumerate(variants)]
    audit["produced"] = len(docs)
    return docs, audit
