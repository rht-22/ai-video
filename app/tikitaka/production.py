"""Shared planning constraints and compact narrative context for grid-review.

Plans are model suggestions, never proof. Preflight estimates expose problems
before paid assembly; only measured TTS and clip inspection approve a final cut.
"""
from __future__ import annotations

from app.tikitaka.guide import in_excluded
from app.tikitaka.timing import narration_plan_sec

SCHEMA = "production_plan/v2_joint_cover"
KINDS = {"action", "rule_summary", "transition", "question", "evaluation"}
COVER_ROLES = {"evidence", "support"}

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
            f"모든 덮개 정지·슬로우 금지; 긴 덮개는 최대 {gt.MAX_COVER_SPEED:g}배속. "
            "원칙적으로 S/A 구간을 N 화면으로 다시 예약하지 마라. 아래의 뒤쪽 S 1회 재사용만 예외다. 인접 S/A 경계는 원본 순서대로 연결한다. "
            "대사 직전 화면에 N을 넣으면 그 고정 화면 길이가 상한이다. 짧으면 문장을 줄이지 말고 다른 덮개를 확보하라. "
            "길이는 계획 추정이며 합성 후 실측으로 다시 확인한다. 화면 부족이면 문장·TTS를 유지하고 덮개 계획만 바꿔라. "
            "지정·support·같은 씬 미사용 화면으로도 길이가 부족할 때만, 문장 내용과 직접 맞는 뒤쪽 S 대사 화면을 "
            "N에서 원음을 끄고 한 번 먼저 보여준 뒤 그 S에서 원음으로 한 번 더 쓸 수 있다. 같은 대사 화면을 여러 N이 재사용하면 안 된다. "
            "덮개 정지는 금지한다. 덮개가 내레이션보다 길면 전체 동작을 살리기 위해 최대 1.2배속으로 맞추고, 그래도 길면 자연스러운 경계에서 남는 부분을 자른다. "
            "대사만으로 설명된 내용을 N으로 반복하지 마라.\n"
            '각 N은 문장과 화면을 같은 단계에서 정하고 production_plan={'
            '"kind":"action|rule_summary|transition|question|evaluation",'
            '"evidence_ids":["문장을 쓸 수 있는 근거 L-/S- ID"],'
            '"cover":[{"span_id":"실제로 보여줄 sp ID","role":"evidence|support"}]}를 붙여라. '
            "evidence 역할 화면에는 문장의 핵심 인물·행동·물건이 직접 보여야 하고, support는 같은 사건의 반응·연결 화면만 가능하다. "
            "구체 행동을 말하는 action 문장은 evidence 덮개가 하나 이상 필요하다. "
            "근거와 덮개는 서로 다른 역할이다. rule_summary는 원본 대사 근거를 반드시 지정한다. "
            "화면 목록에 정확히 맞는 sp ID가 없으면 가까운 화면을 대신 쓰지 말고 문장을 그 화면이 실제로 보여주는 내용으로 바꾸거나 N을 쓰지 마라. "
            "없는 ID·임의 시각은 금지다.\n")


def planned_cover_span_ids(plan, index):
    """Return exact grid spans from v2 plans, expanding legacy moment plans."""
    if not isinstance(plan, dict):
        return []
    cover = plan.get("cover")
    if isinstance(cover, list):
        return list(dict.fromkeys(
            item.get("span_id") for item in cover
            if isinstance(item, dict) and isinstance(item.get("span_id"), str)
        ))
    moments = {x["id"]: x for x in index.get("moments", [])}
    return list(dict.fromkeys(
        sid for mid in plan.get("cover_moment_ids", [])
        if mid in moments for sid in moments[mid].get("span_ids", [])
    ))


def normalize_plan(raw, index, transcript, exclude=()):
    """Keep valid IDs only, with explicit issues; old items stay byte-compatible."""
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        return None, ["production_plan 형식 오류"]
    sources = {x["id"]: x for x in transcript.get("lines", []) + index.get("moments", [])}
    moments = {x["id"] for x in index.get("moments", [])}
    span_sources = {sid: moment for moment in index.get("moments", []) for sid in moment.get("span_ids", [])}
    # Spoken spans are absent from moments by design; the grid is authoritative.
    for sid, fact in index.get("grid_facts", {}).items():
        if fact.get("t_in") is not None and fact.get("t_out") is not None:
            span_sources[sid] = {"start": fact["t_in"], "end": fact["t_out"]}
        elif isinstance(fact.get("time"), dict):
            from app.v3.schemas import parse_ts
            span_sources[sid] = {"start": parse_ts(fact["time"]["start"]),
                                 "end": parse_ts(fact["time"]["end"])}
    grid_spans = set(index.get("grid_facts", {}))
    issues = []
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        issues.append("내레이션 역할 미확정")
        kind = "unspecified"
    out = {"kind": kind}
    for field, maximum in (("evidence_ids", 6),):
        ids = raw.get(field, [])
        if not isinstance(ids, list):
            issues.append(f"{field} 목록 형식 오류")
            ids = []
        kept = []
        for sid in ids:
            if not isinstance(sid, str) or sid not in sources:
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
    if "cover" in raw:
        cover_in = raw.get("cover")
        if not isinstance(cover_in, list):
            issues.append("cover 목록 형식 오류")
            cover_in = []
        kept_cover = []
        seen = set()
        for item in cover_in:
            if not isinstance(item, dict):
                issues.append(f"cover 항목 형식 오류 {item!r}")
                continue
            sid, role = item.get("span_id"), item.get("role")
            if not isinstance(sid, str) or sid not in grid_spans or sid not in span_sources:
                issues.append(f"cover: 없는/잘못된 span_id {sid!r}")
                continue
            if role not in COVER_ROLES:
                issues.append(f"cover: 잘못된 역할 {role!r}")
                continue
            src = span_sources[sid]
            if in_excluded(src["start"], src["end"], exclude):
                issues.append(f"cover: 제외 구간 ID {sid}")
                continue
            if sid not in seen:
                kept_cover.append({"span_id": sid, "role": role})
                seen.add(sid)
        if len(kept_cover) > 4:
            issues.append("cover: 4개 상한 초과")
        out["cover"] = kept_cover[:4]
        if kind == "action" and not any(x["role"] == "evidence" for x in out["cover"]):
            issues.append("action: 핵심 행동을 직접 보여주는 evidence 덮개 없음")
    else:
        # Old checkpoints remain readable and retain their original shape.
        ids = raw.get("cover_moment_ids", [])
        if not isinstance(ids, list):
            issues.append("cover_moment_ids 목록 형식 오류")
            ids = []
        kept = []
        for sid in ids:
            if not isinstance(sid, str) or sid not in sources or sid not in moments:
                issues.append(f"cover_moment_ids: 없는/잘못된 ID {sid!r}")
                continue
            src = sources[sid]
            if in_excluded(src["start"], src["end"], exclude):
                issues.append(f"cover_moment_ids: 제외 구간 ID {sid}")
                continue
            if sid not in kept:
                kept.append(sid)
        if len(kept) > 4:
            issues.append("cover_moment_ids: 4개 상한 초과")
        out["cover_moment_ids"] = kept[:4]
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
    fixed = []
    for item_no, item in enumerate(version["items"], 1):
        if item["type"] == "S":
            if transcript.get("words"):
                b = bind_dialogue(lines, transcript["words"], item["line_ids"])
                fixed.append((b["start"], b["end"], "S", item_no))
            else:
                fixed.extend((lines[s]["start"], lines[s]["end"], "S", item_no)
                             for s in item["line_ids"] if s in lines)
        elif item["type"] == "A" and item.get("moment_id") in moments:
            m = moments[item["moment_id"]]
            fixed.append((m["start"], m["end"], "A", item_no))
    reserved_n = []
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
        proposed = planned_cover_span_ids(plan, index)
        hard_reserved = list(exclude) + reserved_n + [(a, z) for a, z, mode, pos in fixed
                                                       if mode == "A" or pos <= number]
        later_s = [(a, z) for a, z, mode, pos in fixed if mode == "S" and pos > number]
        reused_dialogue = []
        candidates = {}
        for sid in proposed:
            if sid not in spans:
                continue
            sp = spans[sid]
            if gt.overlap(sp["t_in"], sp["t_out"], hard_reserved):
                continue
            overlaps_fixed = gt.overlap(sp["t_in"], sp["t_out"], [(a, z) for a, z, _, _ in fixed])
            if overlaps_fixed and not gt.overlap(sp["t_in"], sp["t_out"], later_s):
                continue
            candidates[sid] = sp
            if overlaps_fixed:
                reused_dialogue.append(sid)
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
        reserved_n.extend((c["in"], c["out"]) for c in selected)
        reports.append({"item": number, "text": item["text"], "estimate_sec": estimate,
                        "planned_span_ids": proposed,
                        "reused_later_dialogue_span_ids": [sid for sid in reused_dialogue if sid in candidates],
                        "issues": issues})
    return {"schema": SCHEMA, "estimated_only": True, "items": reports,
            "issue_count": sum(len(r["issues"]) for r in reports)}


def enforce_joint_plans(job, gemini, version, index, transcript, exclude=()):
    """Repair only a failing N; never approve missing or unobserved cover evidence."""
    if not index.get('grid_facts'):
        return
    from app.tikitaka.grid_table import probe_cover
    from app.tikitaka.rebuild import source_script
    from app.tikitaka.grid import fingerprint
    spans = {s['id']: s for s in job.load('grid.json')['span_candidates']}
    moments = {m['id']: m for m in index.get('moments', [])}
    rows = [dict(it, mode=it['type'], text=it.get('text') or
                 moments.get(it.get('moment_id'), {}).get('desc', '')) for it in version['items']]
    from app.tikitaka.timing import bind_dialogue
    from app.tikitaka.grid_table import overlap, MIN_CUT
    lines = {l['id']: l for l in transcript['lines']}
    fixed = []
    for k, it in enumerate(version['items']):
        if it['type'] == 'S':
            bound = bind_dialogue(lines, transcript['words'], it['line_ids'])
            fixed.append((bound['start'], bound['end'], k, 'S'))
        elif it['type'] == 'A':
            m = moments[it['moment_id']]
            fixed.append((m['start'], m['end'], k, 'A'))
    reserved = list(exclude)
    for pos, item in enumerate(version['items']):
        if item['type'] != 'N':
            continue
        hard = reserved + [(a, b) for a, b, k, mode in fixed if k <= pos or mode == 'A']
        available = {sid for sid, sp in spans.items()
                     if sid in index['grid_facts'] and sp['t_out']-sp['t_in'] >= MIN_CUT
                     and not overlap(sp['t_in'], sp['t_out'], hard)}
        candidate = dict(item)
        history = []
        for attempt in range(4):
            plan, errors = normalize_plan(candidate.get('production_plan'), index, transcript, exclude)
            if not plan or not plan.get('cover'):
                errors.append('각 N에는 유효한 sp ID의 cover 계획이 필요함')
            if plan and not plan.get('evidence_ids'):
                errors.append('문장의 근거 evidence_ids가 필요함')
            if plan:
                unavailable = [x['span_id'] for x in plan.get('cover', []) if x['span_id'] not in available]
                if unavailable:
                    errors.append(f'이미 사용되었거나 짧거나 제외된 화면: {unavailable}')
            if not errors:
                from app.tikitaka.grid_table import MAX_CUT
                budget = sum(min(MAX_CUT, spans[x['span_id']]['t_out']-spans[x['span_id']]['t_in']) for x in plan['cover'])
                needed = narration_plan_sec(candidate['text'])
                if hasattr(job, 'has') and job.has('planning_tts_settings.json'):
                    from app.tikitaka.grid_table import _tts_cached
                    settings = job.load('planning_tts_settings.json')
                    _, needed = _tts_cached(job, candidate['text'], settings['voice'], settings['speed'])
                if budget < needed:
                    errors.append(f'화면 {budget:.2f}초 < 필요 발화 {needed:.2f}초: 문장은 고정이다. 사용 가능한 덮개를 확장·추가하거나 뒤 대사 화면을 재사용하라')
            if not errors:
                candidate['production_plan'] = plan
                candidate_rows = list(rows)
                candidate_rows[pos] = dict(candidate, mode='N')
                context = narration_context(candidate_rows, candidate_rows[pos], index, transcript)
                for cover in plan['cover']:
                    sid = cover['span_id']
                    sp = spans.get(sid)
                    if sp is None:
                        errors.append(f'{sid}: 실제 그리드 구간 없음')
                        continue
                    cut = {'in': sp['t_in'], 'out': sp['t_out'],
                           'desc': index['grid_facts'][sid].get('scene_script', '')}
                    # Support is allowed to show the same event, without repeating its action.
                    ctx = dict(context, cover_role=cover['role'])
                    result = probe_cover(job, gemini, cut, candidate['text'],
                                         key=fingerprint(['joint-plan-gate/v1', cut, candidate['text'], ctx]), context=ctx)
                    if not isinstance(result, dict) or result.get('text_matches') is not True:
                        errors.append(f'{sid}: {result}')
            if not errors:
                reserved.extend((spans[x['span_id']]['t_in'], spans[x['span_id']]['t_out']) for x in plan['cover'])
                old = dict(item)
                item.update(text=candidate['text'], production_plan=plan,
                            plan_sec=narration_plan_sec(candidate['text']))
                rows[pos].update(item)
                if history:
                    version.setdefault('joint_plan_repairs', []).append(
                        {'item': pos+1, 'before': old, 'after': dict(item), 'failures': history})
                break
            history.append(errors)
            job.log(f'[joint_plan] v{version.get("n")} 항목{pos+1}: {errors}')
            if attempt == 3:
                raise ValueError(f'문장·화면 계획 수정 소진: 항목{pos+1} {errors}')
            sources = {**moments, **lines}
            evidence_times = [sources[sid]['start'] for sid in
                              (candidate.get('production_plan') or {}).get('evidence_ids', []) if sid in sources]
            anchors = evidence_times or [0.0]
            nearby = sorted(available, key=lambda sid: min(abs(spans[sid]['t_in']-t) for t in anchors))[:100]
            material = [{'span_id': sid, 'start': spans[sid]['t_in'], 'end': spans[sid]['t_out'],
                         'scene_script': index['grid_facts'][sid].get('scene_script', '')} for sid in nearby]
            references = {sid: x for sid, x in sources.items()
                          if any(abs(x['start']-t) < 35 for t in anchors)}
            prompt = (planning_rules() + '\n문제가 있는 N 한 개만 수정하라. 대사나 다른 항목은 수정하지 마라. '
                      '문장은 한 글자도 바꾸지 말고 덮개만 다시 계획하라. 지정 화면의 미사용 앞뒤 부분, 같은 사건 support, 같은 씬 미사용 반응 화면 순으로 확보하라. 그래도 부족하면 내용에 맞는 뒤 대사 화면을 한 번 재사용하라. 정지·슬로우 금지. '
                      '검사를 피하려 action을 evaluation으로 이름만 바꾸지 마라. '
                      'JSON {text: string, production_plan: object}.\n'
                      f'대본: {version["items"]}\n대상: {candidate}\n실패 이력: {history}\n사용 가능한 화면(아래 span_id만 사용): {material}\n'
                      f'대사·사건 근거: {references}')
            raw = gemini.text_json(prompt, kind='joint_plan_repair')
            if not isinstance(raw, dict) or not isinstance(raw.get('text'), str) or not raw['text'].strip():
                continue
            candidate = dict(item, production_plan=raw.get('production_plan'))


def validate_cover_selection(plan, ids):
    """A downstream selection may shorten a plan, not silently replace its proof."""
    if not isinstance(plan, dict) or not isinstance(plan.get('cover'), list):
        return  # legacy non-grid plans
    allowed = {x['span_id'] for x in plan['cover']}
    if not isinstance(ids, list) or not ids or any(sid not in allowed for sid in ids):
        raise ValueError('확정 계획 밖의 덮개 ID: 문장·화면 계획 재검증 필요')
    evidence = {x['span_id'] for x in plan['cover'] if x.get('role') == 'evidence'}
    if plan.get('kind') == 'action' and not evidence.intersection(ids):
        raise ValueError('action 덮개에서 확정된 evidence 화면을 누락할 수 없음')
