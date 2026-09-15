"""Shared planning constraints and compact narrative context for grid-review.

Plans are model suggestions, never proof. Preflight estimates expose problems
before paid assembly; only measured TTS and clip inspection approve a final cut.
"""
from __future__ import annotations

from app.tikitaka.guide import in_excluded
from app.tikitaka.timing import narration_plan_sec

SCHEMA = "production_plan/v1"
KINDS = {"action", "rule_summary", "transition", "question", "evaluation"}

SEMANTIC_RULES = """문장의 역할을 먼저 구분하라.
- action: 현재 보이는 구체 동작·물건을 지목. 그 동작·물건의 실제 화면이 필요하다.
- rule_summary: 원본 대사로 확인되는 규칙·설명 요약. 규칙 설명 상황·참여자 화면은 가능하다.
  '상대 표를 뺏는다' 같은 경쟁 비유를 실제 강탈 동작으로 요구하지 마라.
- transition/question/evaluation: 앞뒤 사건을 잇는 안내·질문·평가. 같은 상황의 화면과 맥락을 확인한다.
역할 표시는 작가의 제안이다. 질문 형태라도 새 사실을 전제하면 그 사실의 근거가 필요하다.
'왜일까요?'는 바로 앞 행동을 받는 경우 가능하지만, 받는 사건이 없으면 불명확하다.
맥락은 실제 화면 증거를 대체하지 않는다. 인물 오인·새 사실·구체 동작 모순은 여전히 반려한다.
화면에서 입증되지 않음과 화면이 반대 사실을 보여줌을 구분하고 반려 이유를 구체적으로 적어라."""


def planning_rules():
    # Read the assembler's limits, so changing a limit cannot leave the writer
    # following a different hard-coded contract. Imported lazily to avoid cycles.
    from app.tikitaka import grid_table as gt
    return ("\n## 실제 조립 제약 — 문장과 화면을 함께 계획\n" + SEMANTIC_RULES +
            f"\nN 화면은 미사용 ID 최대 {gt.MAX_STACK}컷, 일반 컷당 최대 {gt.MAX_CUT:g}초. "
            f"자료화면만 최대 {gt.MAX_HOLD:g}초 정지 가능; 일반 화면 정지·슬로우 금지. "
            "S/A로 쓰는 구간을 N 화면으로 다시 예약하지 마라. 인접 S/A 경계는 원본 순서대로 연결한다. "
            "대사 직전 화면에 N을 넣으면 그 고정 화면 길이가 상한이다. 짧은 화면에 긴 설명을 먼저 쓰지 마라. "
            "길이는 계획 추정이며 합성 후 실측으로 다시 확인한다. 화면 부족이면 문장·화면 조합을 함께 바꿔라. "
            "대사만으로 설명된 내용을 N으로 반복하지 마라.\n"
            '각 N에 production_plan={"kind":"action|rule_summary|transition|question|evaluation",'
            '"evidence_ids":["근거 L-/S- ID"],"cover_moment_ids":["사용할 화면 S- ID"]}를 붙여라. '
            "근거 대사와 보여줄 화면은 서로 다른 역할이다. rule_summary는 원본 대사 근거를 반드시 지정하고, "
            "모든 N은 실제 보여줄 화면을 제안하라. 없는 ID·임의 시각은 금지다.\n")


def normalize_plan(raw, index, transcript, exclude=()):
    """Keep valid IDs only, with explicit issues; old items stay byte-compatible."""
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        return None, ["production_plan 형식 오류"]
    sources = {x["id"]: x for x in transcript.get("lines", []) + index.get("moments", [])}
    moments = {x["id"] for x in index.get("moments", [])}
    issues = []
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        issues.append("내레이션 역할 미확정")
        kind = "unspecified"
    out = {"kind": kind}
    for field, maximum in (("evidence_ids", 6), ("cover_moment_ids", 4)):
        ids = raw.get(field, [])
        if not isinstance(ids, list):
            issues.append(f"{field} 목록 형식 오류")
            ids = []
        kept = []
        for sid in ids:
            if not isinstance(sid, str) or sid not in sources or (field == "cover_moment_ids" and sid not in moments):
                issues.append(f"{field}: 없는/잘못된 ID {sid!r}")
                continue
            src = sources[sid]
            if in_excluded(src["start"], src["end"], exclude):
                issues.append(f"{field}: 제외 구간 ID {sid}")
                continue
            if sid not in kept:
                kept.append(sid)
        if len(kept) > maximum:
            issues.append(f"{field}: {maximum}개 상한 초과")
        out[field] = kept[:maximum]
    return out, issues


def narration_context(rows, row, index=None, transcript=None):
    """Local continuity + explicitly cited source evidence, never full episode text."""
    k = next((i for i, r in enumerate(rows) if r is row or
              (row.get("i") is not None and r.get("i") == row["i"])), None)
    adjacent = [{"mode": r["mode"], "text": r["text"]}
                for r in (rows[max(0, k-2):k+2] if k is not None else [])]
    plan = row.get("production_plan") or {}
    if not isinstance(plan, dict):
        plan = {}
    sources = {x["id"]: x for x in (transcript or {}).get("lines", []) + (index or {}).get("moments", [])}
    evidence = [{"id": sid, "text": sources[sid].get("text") or sources[sid].get("desc", ""),
                 "start": sources[sid]["start"], "end": sources[sid]["end"]}
                for sid in plan.get("evidence_ids", [])[:6] if sid in sources]
    return {"adjacent": adjacent, "suggested_kind": plan.get("kind", "unspecified"), "evidence": evidence}


def preflight(version, index, transcript, exclude=()):
    """Conservative diagnostics, not permission to render or evidence of meaning.

    Proposed moments are intersected with unused grid spans using the same
    assembler as final N cuts. Earlier proposed N footage is reserved too.
    """
    from app.tikitaka import grid_table as gt
    from app.tikitaka.timing import bind_dialogue
    lines = {x["id"]: x for x in transcript.get("lines", [])}
    moments = {x["id"]: x for x in index.get("moments", [])}
    spans = {sid: {**fact, "id": sid, "t_in": moment["start"], "t_out": moment["end"]}
             for moment in moments.values() for sid in moment.get("span_ids", [])
             if (fact := index.get("grid_facts", {}).get(sid)) is not None}
    reserved = list(exclude)
    for item in version["items"]:
        if item["type"] == "S":
            if transcript.get("words"):
                b = bind_dialogue(lines, transcript["words"], item["line_ids"])
                reserved.append((b["start"], b["end"]))
            else:
                reserved.extend((lines[s]["start"], lines[s]["end"]) for s in item["line_ids"] if s in lines)
        elif item["type"] == "A" and item.get("moment_id") in moments:
            m = moments[item["moment_id"]]
            reserved.append((m["start"], m["end"]))
    reports = []
    for number, item in enumerate(version["items"], 1):
        if item["type"] != "N":
            continue
        plan, issues = normalize_plan(item.get("production_plan"), index, transcript, exclude)
        plan = plan or {}
        if not plan:
            issues.append("문장·화면 동시 계획 없음 — 기존 캐시/미지정")
        if plan.get("kind") == "rule_summary" and not any(s in lines for s in plan.get("evidence_ids", [])):
            issues.append("규칙 요약의 원본 대사 근거 없음")
        proposed = list(dict.fromkeys(sid for mid in plan.get("cover_moment_ids", [])
                                     for sid in moments[mid].get("span_ids", [])))
        candidates = {sid: spans[sid] for sid in proposed if sid in spans
                      and not gt.overlap(spans[sid]["t_in"], spans[sid]["t_out"], reserved)}
        if proposed and len(candidates) < len(proposed):
            issues.append("계획 화면이 S/A·이전 N·제외 구간과 중복되거나 관찰 근거 없음")
        estimate = narration_plan_sec(item["text"])
        selected = []
        if not candidates:
            issues.append("사용 가능한 계획 화면 없음")
        else:
            try:
                selected = gt.assemble_cover(list(candidates), candidates, estimate, opening=number == 1)
            except (ValueError, KeyError) as e:
                issues.append(f"계획 길이/화면: {e}")
        reserved.extend((c["in"], c["out"]) for c in selected)
        reports.append({"item": number, "text": item["text"], "estimate_sec": estimate,
                        "planned_span_ids": proposed, "issues": issues})
    return {"schema": SCHEMA, "estimated_only": True, "items": reports,
            "issue_count": sum(len(r["issues"]) for r in reports)}
