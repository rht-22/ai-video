"""Shared planning constraints and compact narrative context for grid-review.

Plans are model suggestions, never proof. Preflight estimates expose problems
before paid assembly; only measured TTS and clip inspection approve a final cut.
"""
from __future__ import annotations

from app.tikitaka.guide import in_excluded
from app.tikitaka.timing import narration_plan_sec
from app.tikitaka.common import NARRATION_CHARS_PER_SEC

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
            f"\nN 화면은 최대 {gt.MAX_STACK}컷(시각이 이어진 인접 ID 들은 한 컷으로 센다), 일반 컷당 최대 {gt.MAX_CUT:g}초. "
            f"모든 덮개 정지·슬로우 금지; 긴 덮개는 최대 {gt.MAX_COVER_SPEED:g}배속. "
            "N 이 나오는 동안 원음은 꺼지므로 **대사(S) 화면도 덮개로 쓸 수 있다** — 문장이 말하는 행동이 실제로 보이는 화면을 골라라. "
            "현장음(A) 화면과 다른 N 이 이미 덮은 화면만 피하라. 인접 S/A 경계는 원본 순서대로 연결한다. "
            "지정한 화면이 짧으면 코드가 같은 씬 안에서 앞뒤 인접 화면을 이어 붙이고 컷을 쌓아 길이를 채운다 — 그래도 그 씬에 화면이 "
            f"모자라면 문장이 실패하니, N 문장은 처음부터 그 사건의 화면 길이 안에 들게 써라(계획 길이 = 공백 제외 글자수 ÷ {NARRATION_CHARS_PER_SEC:g}). "
            "대사 직전 화면에 N을 넣으면 그 고정 화면 길이가 상한이다. "
            "길이는 계획 추정이며 합성 후 실측으로 다시 확인한다. "
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
        # 상한은 ID 개수가 아니라 이어진 묶음(컷) 수다 — 게이트가 붙여 저장한 인접 조각들이 재검증에서
        # "4개 초과"로 거절되던 것(2026-09-17 v4 재조립 실측). 시각을 모르는 ID 는 각각 한 묶음으로 센다.
        spans_t = {sid: {'t_in': span_sources[sid]['start'], 't_out': span_sources[sid]['end']}
                   for sid in (x['span_id'] for x in kept_cover) if sid in span_sources}
        runs = cover_runs([x['span_id'] for x in kept_cover], spans_t) if spans_t else []
        loose = [x['span_id'] for x in kept_cover if x['span_id'] not in spans_t]
        n_cuts = len(runs) + len(loose)
        if n_cuts > 4:
            issues.append(f"cover: 4컷 상한 초과({n_cuts}묶음)")
            keep_ids = {sid for run_ids, _l in runs[:4] for sid in run_ids} if runs else set(loose[:4])
            kept_cover = [x for x in kept_cover if x['span_id'] in keep_ids]
        out["cover"] = kept_cover
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



COVER_JOIN_GAP_SEC = 0.6      # 이 틈 이하로 떨어진 같은 씬 조각은 한 흐름으로 잇는다(틈의 영상도 그대로 들어간다). 격자 조각 사이엔 0.1~0.5s 틈이 흔하다(10화 실측 v6: 0.16·0.18s)
STACK_MIN_PART_SEC = 0.6      # 컷 쌓기 조각 하한(v3 STACK_MIN_PART_SEC 와 같다)
BUDGET_SLACK_SEC = 1 / 30     # 조립 클록 한 프레임(4.96 vs 4.96 류 측정 찌꺼기)


def cover_runs(ids, spans, *, gap_sec=COVER_JOIN_GAP_SEC):
    """덮개 조각을 소스 시각 순으로 이어진 묶음으로 → [(ids, 길이초)]. 순수."""
    ordered = sorted((sid for sid in ids if sid in spans), key=lambda x: spans[x]['t_in'])
    runs: list[tuple[list, float]] = []
    for sid in ordered:
        sp = spans[sid]
        if runs and sp['t_in'] - spans[runs[-1][0][-1]]['t_out'] <= gap_sec:
            runs[-1][0].append(sid)
            runs[-1] = (runs[-1][0], spans[sid]['t_out'] - spans[runs[-1][0][0]]['t_in'])
        else:
            runs.append(([sid], sp['t_out'] - sp['t_in']))
    return runs


def scene_of_spans(index, spans):
    """조각 → index 씬 id(중점 기준). 씬 밖이면 None. 순수."""
    scenes = index.get('scenes', [])
    out = {}
    for sid, sp in spans.items():
        mid = (sp['t_in'] + sp['t_out']) / 2
        out[sid] = next((sc['id'] for sc in scenes if sc['start'] <= mid < sc['end']), None)
    return out


def widen_cover_plan(cover, spans, index, needed, blocked, *, max_runs):
    """계획 덮개가 needed 초에 못 미치면 **같은 index 씬 안에서만** 화면을 넓힌다(v3-human-flow
    designated_window·stack_window 이식, 2026-09-17 사용자 결정 — 대사 화면도 무음으로 덮개 가능).

    ① 이어 붙이기: 지정 조각 묶음의 앞뒤로 틈 ≤ COVER_JOIN_GAP_SEC 인 조각을 뒤·앞 번갈아 붙인다
       (묶음 창은 첫 조각 시작~마지막 조각 끝 — 틈의 영상도 포함). 다른 씬·blocked(제외 구간·현장음·다른 N 덮개) 는 넘지 않는다 → 장면이 바뀌는 경계에서
       엉뚱한 배경이 붙지 않는다(사용자 우려).
    ② 컷 쌓기: 그래도 모자라면 같은 씬의 미사용 조각(≥ STACK_MIN_PART_SEC)을 묶음에 가까운 순으로
       더한다(묶음 수 ≤ max_runs).
    cover 리스트를 제자리에서 늘리고(추가 항목 role='support', auto=join|stack) 추가한 id 목록을 돌려준다.
    붙인 조각도 호출자가 전부 프로브한다."""
    from app.tikitaka.grid_table import overlap
    have = [x['span_id'] for x in cover]
    if not have:
        return []
    scene_of = scene_of_spans(index, spans)
    order = sorted(spans, key=lambda x: spans[x]['t_in'])
    by_pos = dict(enumerate(order))
    pos_of = {sid: i for i, sid in by_pos.items()}
    home = {scene_of[x] for x in have if x in scene_of}
    home.discard(None)
    def total():
        # 조립과 같은 프레임 단위 자(시작 올림·끝 내림) — 검사(budget)와 같은 값을 보고 멈춘다
        from app.tikitaka.grid_table import FPS
        import math
        return sum(max(0.0, math.floor(spans[ids[-1]]['t_out'] * FPS + 1e-7) / FPS
                       - math.ceil(spans[ids[0]]['t_in'] * FPS - 1e-7) / FPS)
                   for ids, _length in cover_runs([x['span_id'] for x in cover], spans))
    def usable(sid):
        sp = spans[sid]
        return (sid not in {x['span_id'] for x in cover} and scene_of.get(sid) in home
                and not overlap(sp['t_in'], sp['t_out'], blocked))
    added = []
    grow_back = True
    while total() + 1e-6 < needed:                                   # ① 이어 붙이기
        runs = cover_runs([x['span_id'] for x in cover], spans)
        ev = {x['span_id'] for x in cover if x.get('role') == 'evidence'}
        runs.sort(key=lambda r: (not any(i in ev for i in r[0]), -r[1]))
        moved = False
        for run_ids, _length in runs:
            for _ in range(2):
                if grow_back:
                    cand = by_pos.get(pos_of[run_ids[-1]] + 1)
                    ok = cand is not None and usable(cand) and \
                        spans[cand]['t_in'] - spans[run_ids[-1]]['t_out'] <= COVER_JOIN_GAP_SEC
                else:
                    cand = by_pos.get(pos_of[run_ids[0]] - 1)
                    ok = cand is not None and usable(cand) and \
                        spans[run_ids[0]]['t_in'] - spans[cand]['t_out'] <= COVER_JOIN_GAP_SEC
                grow_back = not grow_back
                if ok:
                    cover.append({'span_id': cand, 'role': 'support', 'auto': 'join'})
                    added.append(cand)
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break
    # ①-b 길이는 찼어도 MIN_CUT 미만으로 홀로 남은 묶음은 이웃을 붙여 컷으로 성립시킨다(v4 재조립 실측:
    # 다른 덮개로 길이가 차 있어 0.3s 「버리지 마라」 조각이 '더 이을 조각 없음'으로 거절됐다).
    from app.tikitaka.grid_table import MIN_CUT as _MIN_CUT
    for _ in range(8):
        short = [r for r in cover_runs([x['span_id'] for x in cover], spans) if r[1] < _MIN_CUT]
        if not short:
            break
        run_ids = short[0][0]
        grown = False
        for cand in (by_pos.get(pos_of[run_ids[-1]] + 1), by_pos.get(pos_of[run_ids[0]] - 1)):
            if cand is None or not usable(cand):
                continue
            gap = (spans[cand]['t_in'] - spans[run_ids[-1]]['t_out'] if pos_of[cand] > pos_of[run_ids[-1]]
                   else spans[run_ids[0]]['t_in'] - spans[cand]['t_out'])
            if gap <= COVER_JOIN_GAP_SEC:
                cover.append({'span_id': cand, 'role': 'support', 'auto': 'join'})
                added.append(cand)
                grown = True
                break
        if not grown:
            break
    if total() + 1e-6 < needed:                                      # ② 컷 쌓기
        runs = cover_runs([x['span_id'] for x in cover], spans)
        center = sum((spans[r[0][0]]['t_in'] + spans[r[0][-1]]['t_out']) / 2 for r in runs) / len(runs)
        pool = sorted((sid for sid in spans if usable(sid)
                       and spans[sid]['t_out'] - spans[sid]['t_in'] >= STACK_MIN_PART_SEC),
                      key=lambda sid: abs((spans[sid]['t_in'] + spans[sid]['t_out']) / 2 - center))
        for sid in pool:
            if total() + 1e-6 >= needed:
                break
            trial = cover + [{'span_id': sid, 'role': 'support', 'auto': 'stack'}]
            if len(cover_runs([x['span_id'] for x in trial], spans)) > max_runs:
                continue
            cover.append(trial[-1])
            added.append(sid)
    return added


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
        # 2026-09-17 사용자 결정: 대사(S) 화면도 원음을 끄고 덮개로 쓸 수 있다(v3-human-flow 방식).
        # 막는 것은 제외 구간·현장음(A) 화면·다른 N 이 이미 덮은 화면뿐. 길이는 조각이 아니라
        # 이어진 묶음(cover_runs)에 본다 — 격자 조각 35% 가 0.6s 미만이라 조각 단위 하한은 작가가
        # 정확히 고른 화면(「손을 뻗는다」 0.50s)까지 거절했다(10화 실측).
        hard = reserved + [(a, b) for a, b, k, mode in fixed if mode == 'A']
        available = {sid for sid, sp in spans.items()
                     if sid in index['grid_facts'] and not overlap(sp['t_in'], sp['t_out'], hard)}
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
                    errors.append(f'제외 구간·현장음·다른 내레이션 덮개와 겹치는 화면: {unavailable}')
            if not errors:
                from app.tikitaka.grid_table import MAX_STACK, FPS
                import math
                needed = narration_plan_sec(candidate['text'])
                if hasattr(job, 'has') and job.has('planning_tts_settings.json'):
                    from app.tikitaka.grid_table import _tts_cached
                    settings = job.load('planning_tts_settings.json')
                    _, needed = _tts_cached(job, candidate['text'], settings['voice'], settings['speed'])
                # 작가가 고른 화면이 짧으면 코드가 같은 씬 안에서 앞뒤로 잇고, 그래도 모자라면 컷을 쌓는다.
                # 붙인 조각은 아래 프로브를 전부 지난다(장면 경계에서 다른 배경이 붙으면 거기서 걸린다).
                # 조립은 프레임 단위로 내림(소스)·올림(TTS)하므로 게이트는 한 프레임 여유를 더 요구한다 —
                # 게이트를 턱걸이로 지난 계획이 조립에서 한 프레임 모자라 죽지 않게(v2 항목6 실측 3.34 vs 3.29).
                needed = math.ceil(needed * FPS - 1e-7) / FPS + 1 / FPS
                auto = widen_cover_plan(plan['cover'], spans, index, needed, hard, max_runs=MAX_STACK)
                if auto:
                    plan['auto_extended'] = auto
                    job.log(f'[joint_plan] v{version.get("n")} 항목{pos+1}: 화면 넓힘 {auto}')
                runs = cover_runs([x['span_id'] for x in plan['cover']], spans)
                short = [ids for ids, length in runs if length < MIN_CUT]
                if short:
                    errors.append(f'짧은 화면 {short}: 이어진 조각 합이 {MIN_CUT:g}초 미만이고 같은 씬에서 더 이을 조각이 없다 — 다른 화면을 지정하라')
                if len(runs) > MAX_STACK:
                    errors.append(f'덮개 묶음 {len(runs)}개 > 최대 {MAX_STACK}컷')
                # 조립(assemble_cover)과 같은 자로 잰다 — 묶음 경계를 프레임 안쪽으로 맞추면(시작 올림·끝 내림)
                # 묶음마다 최대 2프레임이 준다. 조각 합으로 재면 게이트 3.34s 가 조립 3.267s 로 줄어 TTS 3.29s 에
                # 모자랐다(v2 항목6 실측).
                budget = sum(max(0.0, math.floor(spans[ids[-1]]['t_out'] * FPS + 1e-7) / FPS
                                 - math.ceil(spans[ids[0]]['t_in'] * FPS - 1e-7) / FPS) for ids, _length in runs)
                if not errors and budget + BUDGET_SLACK_SEC < needed:
                    errors.append(f'화면 {budget:.2f}초 < 필요 발화 {needed:.2f}초: 같은 씬에 더 이을 화면이 없다. '
                                  f'다른 화면을 지정하거나 문장을 그 길이에 맞게 다시 써라')
            if not errors:
                candidate['production_plan'] = plan
                candidate_rows = list(rows)
                candidate_rows[pos] = dict(candidate, mode='N')
                context = narration_context(candidate_rows, candidate_rows[pos], index, transcript)
                role_of = {x['span_id']: x.get('role') for x in plan['cover']}
                for run_ids, _length in cover_runs([x['span_id'] for x in plan['cover']], spans):
                    # 이어진 묶음은 한 샷이다 — 조각마다가 아니라 묶음째 한 번 본다
                    cut = {'in': spans[run_ids[0]]['t_in'], 'out': spans[run_ids[-1]]['t_out'],
                           'desc': ' / '.join(index['grid_facts'][x].get('scene_script', '') for x in run_ids)}
                    role = 'evidence' if any(role_of.get(x) == 'evidence' for x in run_ids) else 'support'
                    ctx = dict(context, cover_role=role)
                    result = probe_cover(job, gemini, cut, candidate['text'],
                                         key=fingerprint(['joint-plan-gate/v2', cut, candidate['text'], ctx]), context=ctx)
                    if not isinstance(result, dict) or result.get('text_matches') is not True:
                        errors.append(f'{"+".join(run_ids)}: {result}')
            if not errors:
                plan['gate_verified'] = True          # 조립(grid_table)은 이 계획 화면을 다시 프로브하지 않는다
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
            # Normal joint-plan repair keeps the approved sentence immutable.
            # If a previous production run failed on a visual fact
            # contradiction, however, changing covers alone can never recover:
            # every candidate will correctly reject the same unsupported
            # action.  In that explicit retry path only, let Gemini rewrite the
            # one failing N from the probe's observed `seen` facts.  Dialogue
            # and all other items remain immutable.
            retry_name = f'production_retry_v{version.get("n")}.json'
            retrying = hasattr(job, 'has') and job.has(retry_name)
            if item.get('user_edits'):
                # 사람이 고친 문장은 불변 — 덮개만 다시 계획한다(2026-09-17 v4 재조립 실측: 게이트가 사용자 문장을
                # 옛 덮개에 맞춰 되돌려 썼다).
                retrying = False
            contradiction_retry = retrying and any(
                'text_matches' in str(failure) and 'False' in str(failure) for failure in history)
            # 코드가 같은 씬에서 넓히고 쌓아도 모자라면 그 씬에는 그만한 화면이 없는 것 — 덮개 교체를
            # 한 번 해 본 뒤(이력 2건째)부터는 문장을 화면 길이에 맞게 다시 쓰게 한다(요약·절단이 아니라
            # 같은 의미의 더 짧은 완결 문장). 첫 수정은 종전대로 덮개만.
            budget_retry = (not item.get('user_edits')) and (retrying or len(history) >= 2) and any('필요 발화' in str(failure) for failure in history)
            if contradiction_retry:
                sentence_rule = ('실패 이력의 seen에서 직접 관찰되는 사실만 사용해 이 N 문장도 다시 써라. '
                                 '사건의 의미는 유지하고 보이지 않는 행동·감정은 쓰지 마라. ')
            elif budget_retry:
                sentence_rule = ('같은 씬에 화면이 그만큼 없다. 같은 사건·의미를 유지한 채, 확보 가능한 덮개 길이 안에 드는 '
                                 '더 짧은 완결 문장으로 이 N 문장을 다시 써라(요약체·절단 금지). 보이지 않는 행동·감정은 쓰지 마라. ')
            else:
                sentence_rule = '문장은 한 글자도 바꾸지 말고 덮개만 다시 계획하라. '
            prompt = (planning_rules() + '\n문제가 있는 N 한 개만 수정하라. 대사나 다른 항목은 수정하지 마라. '
                      + sentence_rule + '지정한 화면이 짧으면 코드가 같은 씬 안에서 앞뒤 인접 화면을 이어 붙이고 컷을 쌓아 준다 — '
                      '문장이 말하는 행동이 실제로 보이는 화면을 고르는 데 집중하라. 지정 화면의 미사용 앞뒤 부분, 같은 사건 support, 같은 씬 미사용 반응 화면 순으로 확보하라. 그래도 부족하면 내용에 맞는 뒤 대사 화면을 한 번 재사용하라. 정지·슬로우 금지. '
                      '검사를 피하려 action을 evaluation으로 이름만 바꾸지 마라. '
                      'JSON {text: string, production_plan: object}.\n'
                      f'대본: {version["items"]}\n대상: {candidate}\n실패 이력: {history}\n사용 가능한 화면(아래 span_id만 사용): {material}\n'
                      f'대사·사건 근거: {references}')
            raw = gemini.text_json(prompt, kind='joint_plan_repair')
            if not isinstance(raw, dict) or not isinstance(raw.get('text'), str) or not raw['text'].strip():
                continue
            candidate = dict(item, production_plan=raw.get('production_plan'))
            if contradiction_retry or budget_retry:
                candidate['text'] = raw['text'].strip()


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
