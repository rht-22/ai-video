"""4단계 — 스크립트 리빌딩(10가지 바이럴 패턴). 텍스트 온리 Flash.

입력: 인덱스(장면·화자 배정 대사·순간) → '소스 스크립트' 텍스트
출력 `rebuild.json`: versions[10]{n, strategy, title, structure, items[], analysis, plan_sec, issues[]}, recommended
검증(코드): S 항목의 line_ids 가 실재·연속·동일 화자인가, A 의 moment_id 가 실재하는가, 합계가 상한 안인가.
위반 항목은 **버리고 기록**한다(버전 전체를 죽이지 않는다 — 다른 항목은 멀쩡하다).
"""
from __future__ import annotations

import re

from app.tikitaka.common import Job, fmt_tc, MAX_SHORTS_SEC, ms3
from app.tikitaka.llm import Gemini
from app.tikitaka.prompts import REBUILD_PROMPT
from app.tikitaka.guide import guide_block, avoid_hits, excluded_ranges, in_excluded
from app.tikitaka.digest import digest_block
from app.tikitaka.timing import narration_plan_sec

TARGET_MIN_SEC = 45
TARGET_MAX_SEC = 70
STRATEGIES = ["결말 선공개형", "충격 폭로형", "감정 폭발형", "인지부조화/급발진형", "미스터리 떡밥형",
              "제3자 관찰자/리액션 먼저형", "타임어택 카운트다운형", "시점 교차/핑퐁형", "사이다/참교육형", "만약에/분기점형",
              "구간 순차형", "루프형", "장면 통째 압축형", "점층 빌드업형"]          # 11~14: 선형 서사 계열(2026-09-11 사용자 요청)
LINEAR_STRATEGIES = {"구간 순차형", "루프형", "장면 통째 압축형", "점층 빌드업형"}
HOOK_STRATEGIES = {"구간 순차형"}                # 콜드오픈(첫 항목만 순서 밖) 허용 — --seq-hook on 일 때
def canonical_strategy(name, n: int) -> str:
    """모델이 적은 전략 이름 → STRATEGIES 정본. 모델이 "점층 빌드업형(슬로우 번)" 처럼 꼬리를 붙여 와도 선형 벨트가 걸리게(2026-09-11 실측).
    못 맞추면 번호 순서의 기본 전략. 순수 — 테스트 대상."""
    raw = " ".join(str(name or "").split())
    for st in STRATEGIES:
        if raw == st or raw.startswith(st) or st in raw:
            return st
    return STRATEGIES[min(max(n, 1) - 1, len(STRATEGIES) - 1)]


SEQ_HOOK_RULES = {
    True: "**첫 항목**은 이 구간에서 가장 센 대사 한 줄(≤3초)을 콜드오픈으로 당겨 쓰고, 두 번째 항목부터 원본 순서대로 간다(그 줄은 제자리에서 다시 써도 된다).",
    False: "첫 항목부터 원본 순서대로 간다(콜드오픈 없음) — 첫 대사 자체가 훅이 되게 구간의 시작점을 고른다.",
}


def source_script(index: dict, transcript: dict, exclude: list[tuple[float, float]] | None = None) -> str:
    """장면 순서대로 대사 줄(L)·순간(S)을 시간순으로 엮은 텍스트 — 리빌딩 프롬프트의 재료.
    exclude(활용 불가 구간, 절대초)에 걸치는 줄·순간은 **아예 싣지 않는다**(모델이 고를 수 없게) — 자리엔 표시 한 줄만."""
    ex = exclude or []
    lines = [l for l in transcript["lines"] if not in_excluded(l["start"], l["end"], ex)]
    moments = [m for m in index["moments"] if not in_excluded(m["start"], m["end"], ex)]
    markers = [(s, f"(활용 불가 구간 {fmt_tc(s)}~{fmt_tc(e)} — 대사·순간 생략)") for s, e in ex]
    scenes = index["scenes"] or [{"id": "SC-000", "start": 0.0, "end": 1e9, "place": "", "summary": "", "chars": []}]
    out: list[str] = []
    li = mi = 0
    for sc in scenes:
        out.append(f"\n## {sc['id']} [{fmt_tc(sc['start'])}~{fmt_tc(sc['end'])}] {sc.get('place','')} — {sc.get('summary','')}"
                   f" (등장: {', '.join(sc.get('chars') or [])})")
        entries: list[tuple[float, str]] = []
        while li < len(lines) and lines[li]["start"] < sc["end"]:
            l = lines[li]
            entries.append((l["start"], f"{l['id']} [{fmt_tc(l['start'])}~{fmt_tc(l['end'])}] {l.get('speaker') or '미상'}: \"{l['text']}\""))
            li += 1
        while mi < len(moments) and moments[mi]["start"] < sc["end"]:
            m = moments[mi]
            who = f"{m['who']} — " if m.get("who") else ""
            snd = f" (소리: {m['sound']})" if m.get("sound") else ""
            entries.append((m["start"], f"{m['id']} [{fmt_tc(m['start'])}~{fmt_tc(m['end'])}] ({m['kind']}) {who}{m['desc']}{snd}"))
            mi += 1
        entries += [(t, txt) for t, txt in markers if sc["start"] <= t < sc["end"]]
        entries.sort(key=lambda e: e[0])
        out.extend(e[1] for e in entries)
    # 장면 밖 잔여
    rest = [f"{l['id']} [{fmt_tc(l['start'])}~{fmt_tc(l['end'])}] {l.get('speaker') or '미상'}: \"{l['text']}\"" for l in lines[li:]]
    rest += [f"{m['id']} [{fmt_tc(m['start'])}~{fmt_tc(m['end'])}] ({m['kind']}) {m['desc']}" for m in moments[mi:]]
    if rest:
        out.append("\n## (장면 미분류)")
        out.extend(rest)
    if index.get("grid_facts"):
        out.append("\n## 화면 근거(grid 정독): 인용은 아래 원문만. 회상·상상을 현재 사실로 단정하지 마라.")
        for moment in moments:
            text = moment.get("screen_text")
            diegesis = moment.get("diegesis")
            if text or diegesis in {"imagined", "recalled", "unclear"}:
                out.append(f"{moment['id']} · 화면 글자={text or '(없음)'} · 연출층위={diegesis}")
    return "\n".join(out)


# 화면에 실제로 있어야 하는 신체 동작어(어간) — 제목·내레이션에 나오는데 인덱스 순간·대사 어디에도 없으면 '비유 의심'
ACTION_STEMS = ("침 뱉", "침을 뱉", "뺨", "따귀", "멱살", "주먹", "발로 차", "발길질", "밀치", "머리채", "무릎 꿇", "물 뿌", "물을 끼얹",
                "집어던", "집어 던", "내던", "뛰쳐나", "박차고", "찢", "부수", "깨부수", "쓰러", "쓰러뜨", "때리", "때린", "때려")


def literal_action_flags(texts: list[str], index: dict, transcript: dict) -> list[str]:
    """제목·내레이션 문장에서 화면 근거 없는 동작어를 찾는다 → ["문장 ⟶ 동작어", …]. 순수 — 테스트 대상.
    근거 = 인덱스 순간 desc/sound + 전사 대사 전체(문자열 포함 검사). 오탐보다 미탐이 낫다(경고일 뿐 자동 수정은 다듬기 패스 몫)."""
    corpus = " ".join([m["desc"] + " " + (m.get("sound") or "") for m in index["moments"]] +
                      [l["text"] for l in transcript["lines"]] + [s.get("summary", "") for s in index.get("scenes", [])])
    corpus = corpus.replace(" ", "")
    flags: list[str] = []
    for t in texts:
        tt = str(t or "")
        for stem in ACTION_STEMS:
            if stem in tt and stem.replace(" ", "") not in corpus:
                flags.append(f"{tt[:40]} ⟶ '{stem}'")
    return flags


_META_LOOP = ("그리고 다시", "다시 처음", "처음으로 돌아", "무한 반복", "그리고 또다시")


def _is_meta_loop_narration(text: str) -> bool:
    t = " ".join(str(text or "").split())
    return t.endswith(("—", "-", "…")) and any(k in t for k in _META_LOOP) or any(t.startswith(k) for k in ("그리고 다시", "그리고 또다시"))


def _norm_copy(t: str) -> str:
    return "".join(str(t or "").split()).rstrip(".!")


def loop_tail(items: list[dict], lines_by_id: dict, moments_by_id: dict, all_lines: list[dict], exclude) -> dict | None:
    """루프형 벨트: 마지막 S/A 가 첫 S/A 앞(처음으로 되돌아오는 자리)이 아니면, **첫 S 대사 바로 앞 줄**(같은 장면 · 활용 불가 밖)을 S 항목으로
    맨 뒤에 붙인다 — 반복 재생에서 끝→처음이 이어지게. 붙인 항목을 돌려주고 없으면 None. 순수 — 테스트 대상."""
    sa = [(k, _item_time(it, lines_by_id, moments_by_id)) for k, it in enumerate(items)]
    sa = [(k, t) for k, t in sa if t is not None]
    if len(sa) < 2 or sa[-1][1] < sa[0][1]:
        return None
    first = items[sa[0][0]]
    if first["type"] != "S":
        return None
    order = {l["id"]: k for k, l in enumerate(all_lines)}
    k0 = order.get(first["line_ids"][0])
    if not k0:
        return None
    prev = all_lines[k0 - 1]
    if in_excluded(prev["start"], prev["end"], exclude or []) or prev["end"] > lines_by_id[first["line_ids"][0]]["start"] + 0.01 \
            or lines_by_id[first["line_ids"][0]]["start"] - prev["end"] > 20.0:
        return None
    tail = {"type": "S", "line_ids": [prev["id"]], "speaker": prev.get("speaker"), "text": prev["text"], "effect": None,
            "plan_sec": ms3(prev["end"] - prev["start"] + 0.2)}
    if items and items[-1]["type"] == "N":                    # 마지막 N("그리고 다시—")은 꼬리 대사 앞으로
        items.insert(len(items) - 1, tail)
    else:
        items.append(tail)
    return tail


def validate_versions(raw: dict, index: dict, transcript: dict, *, hard_max: float = MAX_SHORTS_SEC, avoid: list[str] | None = None,
                      exclude: list[tuple[float, float]] | None = None, seq_hook: bool = True, copy_text: str | None = None) -> dict:
    """모델 산출 → 검증된 versions. 순수 — 테스트 대상.
    avoid(제작 가이드의 지양 단어)가 있으면: 그 단어가 든 대사 줄(S)은 드롭(화면 자막에 그대로 나가므로), 제목·내레이션·효과자막은
    `[가이드 위반]` 으로 표시만 한다(polish_guide 가 고쳐 쓴다)."""
    avoid = [w for w in (avoid or []) if w]
    ex = exclude or []
    lines_by_id = {l["id"]: l for l in transcript["lines"]}
    moments_by_id = {m["id"]: m for m in index["moments"]}
    order = {l["id"]: k for k, l in enumerate(transcript["lines"])}
    versions: list[dict] = []
    for v in raw.get("versions") or []:
        n = int(v.get("n") or len(versions) + 1)
        items: list[dict] = []
        issues: list[str] = []
        total = 0.0
        for k, it in enumerate(v.get("items") or [], 1):
            t = str(it.get("type") or "").upper()
            eff = it.get("effect")
            eff = str(eff).strip()[:16] if eff else None
            if t == "N":
                text = " ".join(str(it.get("text") or "").split())
                if not text:
                    issues.append(f"item{k}: 빈 내레이션 — 드롭")
                    continue
                sec = narration_plan_sec(text)
                items.append({"type": "N", "text": text, "effect": eff, "plan_sec": sec})
                total += sec
            elif t == "S":
                ids = [str(x) for x in (it.get("line_ids") or [])]
                if not ids or any(i not in lines_by_id for i in ids):
                    issues.append(f"item{k}: 없는 줄 ID {ids} — 드롭")
                    continue
                ids = sorted(set(ids), key=lambda i: order[i])
                ks = [order[i] for i in ids]
                if ks[-1] - ks[0] != len(ks) - 1:
                    issues.append(f"item{k}: 비연속 줄 {ids} → 첫 연속 구간만")
                    keep = [ids[0]]
                    for i in ids[1:]:
                        if order[i] == order[keep[-1]] + 1:
                            keep.append(i)
                        else:
                            break
                    ids = keep
                sp = {lines_by_id[i].get("speaker") for i in ids}
                if len(sp) > 1:
                    issues.append(f"item{k}: 화자 섞임 {sp} → 첫 화자 줄만")
                    first = lines_by_id[ids[0]].get("speaker")
                    ids = [i for i in ids if lines_by_id[i].get("speaker") == first]
                if any(in_excluded(lines_by_id[i]["start"], lines_by_id[i]["end"], ex) for i in ids):
                    issues.append(f"item{k}: [가이드] 활용 불가 구간의 대사 {ids} — 드롭")
                    continue
                bad = [(w, i) for i in ids for w in avoid if w in (lines_by_id[i].get("text") or "")]
                if bad:
                    issues.append(f"item{k}: [가이드] 지양 단어 {sorted({w for w, _ in bad})} 대사 {sorted({i for _, i in bad})} — 드롭")
                    continue
                sec = ms3(lines_by_id[ids[-1]]["end"] - lines_by_id[ids[0]]["start"] + 0.2)
                items.append({"type": "S", "line_ids": ids, "speaker": lines_by_id[ids[0]].get("speaker"),
                              "text": " ".join(lines_by_id[i]["text"] for i in ids), "effect": eff, "plan_sec": sec})
                total += sec
            elif t == "A":
                mid = str(it.get("moment_id") or "")
                m = moments_by_id.get(mid)
                if not m:
                    issues.append(f"item{k}: 없는 순간 ID {mid!r} — 드롭")
                    continue
                if in_excluded(m["start"], m["end"], ex):
                    issues.append(f"item{k}: [가이드] 활용 불가 구간의 순간 {mid} — 드롭")
                    continue
                sec = ms3(max(0.8, m["end"] - m["start"]))
                items.append({"type": "A", "moment_id": mid, "desc": m["desc"], "sound": m.get("sound"), "who": m.get("who"),
                              "effect": eff, "plan_sec": sec})
                total += sec
            else:
                issues.append(f"item{k}: 모르는 type {t!r} — 드롭")
        # A 순간이 어떤 S 항목의 대사 구간과 겹치면 같은 장면이 두 번 나온다 → A 를 뺀다(대사가 우선). 2026-09-11 실측 v14·v2
        s_spans = [(lines_by_id[it["line_ids"][0]]["start"], lines_by_id[it["line_ids"][-1]]["end"]) for it in items if it["type"] == "S"]
        kept: list[dict] = []
        for it in items:
            if it["type"] == "A":
                m = moments_by_id[it["moment_id"]]
                if any(m["start"] < e0 - 0.3 and m["end"] > s0 + 0.3 for s0, e0 in s_spans):
                    issues.append(f"[중복] A {it['moment_id']} 가 대사 구간과 겹쳐 드롭")
                    total -= it["plan_sec"]
                    continue
            kept.append(it)
        items = kept
        strategy_name = canonical_strategy(v.get("strategy"), n)
        if copy_text:                                              # 하단에 박히는 카피 문구를 내레이션으로 또 읽으면 안 된다(2026-09-11 실측 v12 마지막 N)
            before = len(items)
            items = [it for it in items if not (it["type"] == "N" and (_norm_copy(copy_text) in _norm_copy(it["text"]) or promo_like(it["text"], copy_text)))]
            if len(items) != before:
                total = sum(it["plan_sec"] for it in items)
                issues.append(f"[가이드] 카피 문구를 읽는 내레이션 {before - len(items)}건 드롭('…에서 확인하세요' 류 홍보 문장 포함)")
        if strategy_name not in LINEAR_STRATEGIES:                # 비선형이라도 한 장면 안에서는 원본 순서(첫 훅 제외)
            issues += enforce_scene_order(items, lines_by_id, moments_by_id, index.get("scenes") or [])
        if strategy_name in LINEAR_STRATEGIES:
            issues += enforce_linear_order(items, lines_by_id, moments_by_id, hook=(strategy_name in HOOK_STRATEGIES and seq_hook),
                                           loop=(strategy_name == "루프형"))
            if strategy_name == "루프형":
                meta = [it for it in items if it["type"] == "N" and _is_meta_loop_narration(it["text"])]
                if meta:                                          # "그리고 다시—" 류 메타 내레이션은 TTS 로 읽히면 어색하다(2026-09-11 v12 실측)
                    items = [it for it in items if it not in meta]
                    total = sum(it["plan_sec"] for it in items)
                    issues.append(f"[루프] 메타 내레이션 {len(meta)}건 드롭: " + " / ".join(m["text"][:20] for m in meta))
                added = loop_tail(items, lines_by_id, moments_by_id, transcript["lines"], ex)
                if added:
                    total += added["plan_sec"]
                    issues.append(f"[루프] 끝이 처음으로 안 돌아와 첫 대사 직전 줄 {added['line_ids'][0]} 을 꼬리로 부착")
        # 상한 초과는 꼬리부터 잘라 벨트를 건다(기록)
        while items and total > hard_max:
            dropped = items.pop()
            total -= dropped["plan_sec"]
            issues.append(f"상한 {hard_max}s 초과 → 꼬리 항목 드롭({dropped['type']})")
        title = " ".join(str(v.get("title") or "").split())[:40]
        flags = literal_action_flags([title] + [it["text"] for it in items if it["type"] == "N"], index, transcript)
        issues += [f"[비유 의심] {f}" for f in flags]
        g_hits = guide_hits({"title": title, "items": items}, avoid)
        issues += [f"[가이드 위반] {w!r} in {t!r}" for w, t in g_hits]
        versions.append({"n": n, "strategy": strategy_name,
                         "title": title,
                         "structure": str(v.get("structure") or ""), "items": items,
                         "analysis": v.get("analysis") or {}, "plan_sec": ms3(total), "issues": issues,
                         "literal_flags": flags, "guide_hits": [list(h) for h in g_hits]})
    rec = raw.get("recommended")
    try:
        rec = int(rec)
    except (TypeError, ValueError):
        rec = None
    valid = [v["n"] for v in versions]
    if rec not in valid and versions:
        rec = versions[0]["n"]
    ranking = rank_versions(raw.get("ranking"), rec, valid)
    return {"versions": versions, "recommended": ranking[0] if ranking else rec, "ranking": ranking,
            "reason": str(raw.get("reason") or "")}


def enforce_scene_order(items: list[dict], lines_by_id: dict, moments_by_id: dict, scenes: list[dict], *, hook_exempt: bool = True) -> list[str]:
    """비선형 전략도 **한 장면 안에서는** 원본 순서를 지킨다(2026-09-11 실측 v1: 피 웅덩이 → "살인 현장" → "내가 잘못했네" → 쿵 — 반전 뒤에
    그 앞 대사가 붙었다). 연속된 S/A 항목이 같은 장면(index scenes)이면 그 묶음 안을 시간순으로 안정 정렬(N 은 바로 뒤 S/A 에 붙는다).
    장면 경계를 넘는 순서(결말 선공개 등)는 손대지 않는다. 첫 항목(훅)은 hook_exempt 면 제외. 제자리 갱신·메모. 순수 — 테스트 대상."""
    if not scenes:
        return []

    def scene_of(t: float | None) -> str | None:
        if t is None:
            return None
        return next((sc["id"] for sc in scenes if sc["start"] <= t < sc["end"]), None)

    times = [_item_time(it, lines_by_id, moments_by_id) for it in items]
    ids = [scene_of(t) for t in times]
    # 묶음: 연속 항목 중 S/A 의 장면이 같은 구간(N 은 다음 S/A 의 장면을 따른다)
    eff_scene: list[str | None] = [None] * len(items)
    nxt = None
    for k in range(len(items) - 1, -1, -1):
        if ids[k] is not None:
            nxt = ids[k]
        eff_scene[k] = nxt
    issues: list[str] = []
    start = 1 if hook_exempt and items else 0
    k = start
    moved_total = 0
    while k < len(items):
        j = k
        while j + 1 < len(items) and eff_scene[j + 1] == eff_scene[k] and eff_scene[k] is not None:
            j += 1
        block = list(range(k, j + 1))
        sa = [b for b in block if times[b] is not None]
        seq = [times[b] for b in sa]
        if len(sa) >= 2 and any(a > b for a, b in zip(seq, seq[1:])):
            # N 은 **직전 S/A 뒤**에 붙는다(같은 장면 안 내레이션은 방금 본 것에 대한 반응 — 2026-09-11 v1: "살인 현장이었다니" 가 피 웅덩이
            # 앞으로 밀리던 것 교정). 블록 첫머리의 N 은 첫 S/A 앞에 둔다.
            anchor: dict[int, float] = {}
            prev: float | None = None
            first_t = min(times[b] for b in sa)
            for b in block:
                if times[b] is not None:
                    prev = times[b]
                    anchor[b] = prev
                else:
                    anchor[b] = (prev + 1e-3) if prev is not None else (first_t - 1e-3)
            order = sorted(block, key=lambda b: (anchor[b], b))
            new = [items[b] for b in order]
            moved = sum(1 for a, b in zip([items[b] for b in block], new) if a is not b)
            items[k:j + 1] = new
            moved_total += moved
            issues.append(f"[장면 순서] {eff_scene[k]} 안 {len(block)}항목을 원본 순서로 재배열({moved}항목 이동)")
        k = j + 1
    return issues


def apply_scene_order_version(version: dict, index: dict, transcript: dict, *, log=print) -> int:
    """캐시된(확인 패스 끝난) 버전에도 장면 안 순서 벨트를 건다(멱등). 바뀐 항목 수."""
    lines_by_id = {l["id"]: l for l in transcript["lines"]}
    moments_by_id = {m["id"]: m for m in index["moments"]}
    if canonical_strategy(version.get("strategy"), version.get("n", 1)) in LINEAR_STRATEGIES:
        return 0
    before = [id(it) for it in version["items"]]
    notes = enforce_scene_order(version["items"], lines_by_id, moments_by_id, index.get("scenes") or [])
    if notes:
        version["issues"] = version.get("issues", []) + notes
        for n in notes:
            log(f"[rebuild] v{version.get('n')} {n}")
    return sum(1 for a, b in zip(before, [id(it) for it in version["items"]]) if a != b)


def _item_time(it: dict, lines_by_id: dict, moments_by_id: dict) -> float | None:
    if it["type"] == "S":
        return lines_by_id[it["line_ids"][0]]["start"]
    if it["type"] == "A":
        return moments_by_id[it["moment_id"]]["start"]
    return None


def enforce_linear_order(items: list[dict], lines_by_id: dict, moments_by_id: dict, *, hook: bool, loop: bool) -> list[str]:
    """선형 계열 벨트(제자리 갱신 · 순수 — 테스트 대상). S/A 항목의 원본 시각이 단조 증가해야 한다 — 어긋나면 안정 정렬로 재배열한다.
    N 항목은 바로 뒤 S/A 에 붙어 간다(내레이션은 다음 대사를 여는 접착제). hook 이면 첫 S/A 항목(콜드오픈)은 순서 밖에 둔다.
    loop 면 마지막 S/A 가 첫 S/A 앞(원본에서 처음으로 되돌아가는 자리)인지 기록만 한다(구성 자유도)."""
    issues: list[str] = []
    times = [_item_time(it, lines_by_id, moments_by_id) for it in items]
    sa = [k for k, t in enumerate(times) if t is not None]
    if len(sa) < 2:
        return issues
    body = sa[1:] if hook else sa
    seq = [times[k] for k in body]
    if loop and len(sa) >= 2:
        first_t, last_t = times[sa[0]], times[sa[-1]]
        issues.append(f"[루프] 끝 항목 원본 {fmt_tc(last_t)} → 첫 항목 {fmt_tc(first_t)} ({'처음 앞으로 되돌아옴' if last_t < first_t else '되돌아오지 않음 — 마지막을 첫 항목 직전 상황으로'})")
        body = body[:-1] if last_t < first_t else body        # 루프의 마지막 항목(처음 앞)은 순서 검사에서 뺀다
        seq = [times[k] for k in body]
    if all(a <= b for a, b in zip(seq, seq[1:])):
        return issues
    # 재배열: 고정(콜드오픈·루프 꼬리) 항목은 제자리, 나머지는 원본 시각순(N 은 다음 S/A 시각을 빌린다)
    pinned = set(sa[:1]) if hook else set()
    if loop and times[sa[-1]] < times[sa[0]]:
        pinned.add(sa[-1])
    movable = [k for k in range(len(items)) if k not in pinned]
    anchor: dict[int, float] = {}
    nxt: float | None = None
    for k in reversed(movable):
        if times[k] is not None:
            nxt = times[k]
        anchor[k] = nxt if nxt is not None else 1e12
    order = sorted(movable, key=lambda k: (anchor[k], k))
    new_items = []
    mi = iter(order)
    for k in range(len(items)):
        new_items.append(items[k] if k in pinned else items[next(mi)])
    moved = sum(1 for a, b in zip(items, new_items) if a is not b)
    items[:] = new_items
    issues.append(f"[순차] 원본 순서로 재배열 {moved}항목")
    return issues


def rank_versions(raw_ranking, recommended, valid: list[int]) -> list[int]:
    """모델 ranking(조회수 기대 순) → 유효 번호만 · 중복 제거 · 빠진 번호는 추천 → 번호순으로 뒤에. 순수 — 테스트 대상."""
    out: list[int] = []
    for x in (raw_ranking or []):
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n in valid and n not in out:
            out.append(n)
    if recommended in valid and recommended not in out:
        out.insert(0, recommended)
    for n in valid:
        if n not in out:
            out.append(n)
    return out


def rebuild(job: Job, gemini: Gemini, index: dict, transcript: dict, *, title: str, episode_label: str, duration: float,
            guide: dict | None = None, extra_exclude: list[tuple[float, float]] | None = None, range_label: str | None = None,
            seq_hook: bool = True, cache_name: str = "rebuild.json", digest: dict | None = None) -> dict:
    if job.has(cache_name):
        return job.load(cache_name)
    exclude = sorted(excluded_ranges(guide, duration) + list(extra_exclude or []))
    script = source_script(index, transcript, exclude)
    job.path(cache_name.replace("rebuild", "source_script").replace(".json", ".md")).write_text(script, encoding="utf-8")
    material = f"\n**재료 구간: {range_label}** — 이 구간의 대사·순간만 재료다(구간 밖은 소스 스크립트에 없다). 버전 11~14 는 이 구간을 순서대로 다룬다." if range_label else ""
    prompt = REBUILD_PROMPT.format(title=title, episode_label=episode_label, duration_label=fmt_tc(duration)[:5] + "분",
                                   target_min=TARGET_MIN_SEC, target_max=TARGET_MAX_SEC, hard_max=int(MAX_SHORTS_SEC), script=script,
                                   guide=guide_block(guide), material_note=material, seq_hook_rule=SEQ_HOOK_RULES[bool(seq_hook)],
                                   digest=digest_block(digest))
    job.log(f"[rebuild] 소스 스크립트 {len(script):,}자 → {len(STRATEGIES)}버전 요청" + (f" (제작 가이드 {guide['sha']})" if guide else "")
            + (" · 작품 이해 문서 첨부" if digest else " · ⚠ 작품 이해 문서 없음")
            + (f" · 재료 구간 {range_label}" if range_label else "") + f" · 순차형 콜드오픈 {'on' if seq_hook else 'off'}")
    raw = gemini.text_json(prompt, kind="rebuild", thinking="high")
    job.save(cache_name.replace(".json", "_raw.json"), raw)
    data = validate_versions(raw, index, transcript, avoid=(guide or {}).get("avoid") or [], exclude=exclude, seq_hook=seq_hook,
                             copy_text=(guide or {}).get("copy"))
    data["guide_sha"] = guide["sha"] if guide else None
    data["digest"] = bool(digest)
    data["range"] = range_label
    data["seq_hook"] = bool(seq_hook)
    for v in data["versions"]:                      # 화면에 없는 동작어 → 그 문구만 고쳐 쓴다(프롬프트 규칙의 벨트)
        if v.get("literal_flags"):
            polish_literal_actions(gemini, v, index, transcript, log=job.log)
        polish_character_names(gemini, v, index, transcript, actors=(guide or {}).get("actors") or {}, log=job.log)   # 장면에 없는 인물명
        if guide and v.get("guide_hits"):           # 지양 단어가 든 제목·내레이션·효과자막 → 그 문구만 고쳐 쓴다
            polish_guide(gemini, v, guide, log=job.log)
        if guide and guide.get("actors"):           # 극중 이름 → 배우 이름(프롬프트가 놓친 것만 치환)
            apply_name_map(v, guide["actors"], log=job.log)
    apply_rerank(job, gemini, data, title=title, episode=episode_label, basis="draft")   # 리빌딩 호출의 순위는 예시에 끌린다 — 별도 호출로
    n_issues = sum(len(v["issues"]) for v in data["versions"])
    job.save(cache_name, data)
    for v in data["versions"]:
        job.log(f"[rebuild] v{v['n']:02d} {v['strategy']:<14} {v['plan_sec']:5.1f}s 항목 {len(v['items']):2d} "
                f"이슈 {len(v['issues'])} — {v['title']}")
    job.log(f"[rebuild] 추천 v{data['recommended']} · 순위 {data['ranking']} — {data['reason']}")
    job.record_step("rebuild", versions=len(data["versions"]), issues=n_issues, recommended=data["recommended"],
                    llm=gemini.usage.calls[-1:])
    return data


_PROMO_RE = re.compile(r"(시청|확인|보세요|만나요|하세요|보러|본편|풀\s*영상|풀버전)")


def promo_like(text: str, copy_text: str | None) -> bool:
    """카피 문구를 그대로 읽지는 않지만 같은 역할의 홍보 문장인가 — 카피의 '<플랫폼>에서' 어절이 들어 있고 시청 유도 어휘가 있으면 참
    (실측 2화 v8 마지막 N "조여정의 통쾌한 참교육, 쿠팡플레이에서 확인하세요!"). 순수 — 테스트 대상."""
    if not copy_text or not text:
        return False
    plats = [w for w in str(copy_text).split() if w.endswith("에서") and len(w) > 3]
    return any(pl in text for pl in plats) and bool(_PROMO_RE.search(text))


def _scene_of(t: float, scenes: list[dict]) -> dict | None:
    return next((s for s in scenes if s["start"] <= t < s["end"]), None)


def _item_people(it: dict, lines_by_id: dict, moments_by_id: dict, scenes: list[dict]) -> set[str]:
    """S/A 항목이 실제로 보여주는 인물 — 화자/행위자 + 그 장면의 등장 인물."""
    if it["type"] == "S":
        l = lines_by_id.get((it.get("line_ids") or [None])[0])
        if not l:
            return set()
        sc = _scene_of(l["start"], scenes)
        return {l.get("speaker") or ""} | set((sc or {}).get("chars") or [])
    if it["type"] == "A":
        m = moments_by_id.get(it.get("moment_id"))
        if not m:
            return set()
        sc = _scene_of(m["start"], scenes)
        return {m.get("who") or ""} | set((sc or {}).get("chars") or [])
    return set()


def character_context_flags(version: dict, index: dict, transcript: dict, *, actors: dict[str, str] | None = None) -> list[str]:
    """내레이션·제목이 부르는 인물이 **그 자리의 장면에 없으면** `[인물 의심]`. 판정 근거는 앞뒤 S/A 항목의 화자·행위자·장면 등장 인물
    (제목은 대본 전체). 극중 이름과 배우 이름(actors 매핑) 둘 다 인식한다. 순수 — 테스트 대상.
    (실측 2화 v7: 차고 장면(주보성·주민서)의 내레이션이 "새벽에 귀가한 김지훈" — 확인 패스가 얼굴로 인물을 바꿔 적었다.)"""
    scenes = index.get("scenes") or []
    lines_by_id = {l["id"]: l for l in transcript["lines"]}
    moments_by_id = {m["id"]: m for m in index["moments"]}
    actors = actors or {}
    names = {c for c in (index.get("cast") or []) if c} | set(actors.keys()) | set(actors.values())
    if not names:
        return []
    inv = {a: c for c, a in actors.items()}                        # 배우 → 극중

    def mentioned(text: str) -> set[str]:
        out = set()
        for nm in names:
            if nm and nm in (text or ""):
                out.add(inv.get(nm, nm))
        return out

    items = version.get("items") or []
    people = [_item_people(it, lines_by_id, moments_by_id, scenes) for it in items]
    everyone = set().union(*people) if people else set()
    flags: list[str] = []
    bad_title = mentioned(version.get("title") or "") - everyone
    if bad_title and everyone:
        flags.append(f"[인물 의심] 제목: {sorted(bad_title)} — 대본에 등장: {sorted(x for x in everyone if x)}")
    for k, it in enumerate(items):
        if it["type"] != "N":
            continue
        ctx: set[str] = set()
        for j in range(k - 1, -1, -1):                              # 바로 앞 S/A
            if items[j]["type"] in ("S", "A"):
                ctx |= people[j]; break
        for j in range(k + 1, len(items)):                          # 바로 뒤 S/A
            if items[j]["type"] in ("S", "A"):
                ctx |= people[j]; break
        ctx.discard("")
        if not ctx:
            continue
        bad = mentioned(it["text"]) - ctx
        if bad:
            flags.append(f"[인물 의심] N{k+1}: {sorted(bad)} — 이 자리 등장: {sorted(ctx)} — \"{it['text'][:30]}\"")
    return flags


CHARACTER_POLISH_PROMPT = """너는 쇼츠 대본 교정자다. 아래 제목·내레이션 문장이 **그 자리의 장면에 없는 인물 이름**을 부르고 있다.
두 경우를 구분하라:
① 문장이 **화면에 보이는 인물의 행동·표정·상태를 그 이름으로 서술**하는데 실제 그 자리에는 다른 인물이 있다(예: 차고 장면에 "새벽에 귀가한 김지훈" —
   실제는 김재철) → 그 자리에 실제로 등장하는 인물로 이름을 고쳐 써라. 누가 맞는지 확실하지 않으면 이름을 빼고 관계·역할로("전남편").
② 문장이 **화면 밖 인물을 설명하는 서사**다(예: "진짜 불륜 상대는 이웃집 조여정이었죠" — 지금 화면엔 박경희만 있어도 사실 설명) → **그대로 둔다**.
뜻·리듬·길이는 유지한다(±20%). 걸리지 않은 문장과 ②는 한 글자도 바꾸지 마라. 배우 이름 표기 규칙: {actors}

[작품 이해 — 관계표]
{relations}

[걸린 문장과 그 자리의 등장 인물]
{flags}

[문장 목록 — 같은 개수·같은 순서로 돌려준다]
{texts}

출력 JSON 하나(코드블록 금지): {{"texts": ["...", ...]}}
"""


def polish_character_names(gemini: Gemini, version: dict, index: dict, transcript: dict, *, actors: dict[str, str] | None = None,
                           digest: dict | None = None, log=print) -> int:
    """`[인물 의심]` 문구만 고쳐 쓴다(제자리). 바뀐 문구 수. 걸린 게 없으면 호출 0. 기록은 version["character_flags"]."""
    flags = character_context_flags(version, index, transcript, actors=actors)
    version["character_flags"] = flags
    if not flags:
        return 0
    entries = [("title", version.get("title") or "", None)] + [("N", it["text"], k) for k, it in enumerate(version["items"]) if it["type"] == "N"]
    texts = [t for _, t, _ in entries]
    flagged_idx = set()
    for f in flags:
        if f.startswith("[인물 의심] 제목"):
            flagged_idx.add(0)
        else:
            m = re.search(r"N(\d+):", f)
            if m:
                k = int(m.group(1)) - 1
                flagged_idx |= {i for i, (kind, _, kk) in enumerate(entries) if kind == "N" and kk == k}
    rel = " / ".join(((digest or {}).get("relationships") or [])[:20]) or "(없음)"
    raw = gemini.text_json(CHARACTER_POLISH_PROMPT.format(actors=", ".join(f"{c}={a}" for c, a in (actors or {}).items()) or "(없음 — 극중 이름 그대로)",
                                                           relations=rel, flags="\n".join(f"- {f}" for f in flags),
                                                           texts="\n".join(f"{k}. {t}" for k, t in enumerate(texts, 1))),
                           kind="character_polish", thinking="low")
    new = [" ".join(str(x).split()) for x in (raw.get("texts") or [])]
    if len(new) != len(texts):
        log(f"[인물] ⚠ 다듬기 응답 개수 불일치({len(new)}≠{len(texts)}) — 적용 안 함")
        return 0
    changed = 0
    for i, ((kind, old, k), nt) in enumerate(zip(entries, new)):
        if i not in flagged_idx or not nt or nt == old:
            continue
        if kind == "title":
            version["title"] = nt[:40]
        else:
            version["items"][k]["text"] = nt
            version["items"][k]["plan_sec"] = narration_plan_sec(nt)
        changed += 1
        log(f"[인물] {kind}: \"{old}\" → \"{nt}\"")
    version["plan_sec"] = ms3(sum(it["plan_sec"] for it in version["items"]))
    left = character_context_flags(version, index, transcript, actors=actors)
    if left:
        log(f"[인물] ⚠⚠ 고친 뒤에도 남음: {left}")
    version["character_flags"] = left
    return changed


POLISH_PROMPT = """너는 쇼츠 대본 교정자다. 아래 제목·내레이션 문장 중 **화면에 없는 신체 동작**을 말하는 표현이 있다.
시청자는 그 말을 화면과 대조하므로, 그 표현만 **감정·태도 표현으로 바꿔라**(예: "침 뱉고 뛰쳐나간" → "면전에서 판을 깨고 뛰쳐나간",
"뺨 때리는" → "정면으로 들이받는"). 문장의 나머지·리듬·길이(±20%)는 유지한다. 걸리지 않은 문장은 그대로 돌려준다.

화면에 실제로 있는 순간(참고): {moments}

[걸린 표현]
{flags}

[문장]
{texts}

출력 JSON 하나(코드블록 금지): {{"texts": ["…", …]}}  (입력과 같은 개수·같은 순서)
"""


def polish_literal_actions(gemini: Gemini, version: dict, index: dict, transcript: dict, *, log=print) -> int:
    """[비유 의심] 이 걸린 버전의 제목·내레이션만 고쳐 쓴다(제자리 갱신). 바뀐 문장 수를 돌려준다. 걸린 게 없으면 호출 0."""
    flags = literal_action_flags([version["title"]] + [it["text"] for it in version["items"] if it["type"] == "N"], index, transcript)
    if not flags:
        return 0
    texts = [version["title"]] + [it["text"] for it in version["items"] if it["type"] == "N"]
    used_ids = {i for it in version["items"] if it["type"] == "S" for i in it["line_ids"]}
    lo = min((l["start"] for l in transcript["lines"] if l["id"] in used_ids), default=0.0) - 60
    hi = max((l["end"] for l in transcript["lines"] if l["id"] in used_ids), default=1e9) + 60
    moments = "; ".join(f"{m['id']} {m['desc']}" for m in index["moments"] if lo <= m["start"] <= hi)[:1500]
    raw = gemini.text_json(POLISH_PROMPT.format(moments=moments, flags="\n".join(flags), texts="\n".join(f"{k}. {t}" for k, t in enumerate(texts, 1))),
                           kind="polish", thinking="low")
    new = [" ".join(str(x).split()) for x in (raw.get("texts") or [])]
    if len(new) != len(texts):
        log(f"[rebuild] ⚠ 다듬기 응답 개수 불일치({len(new)}≠{len(texts)}) — 적용 안 함")
        return 0
    changed = 0
    if new[0] and new[0] != version["title"]:
        log(f"[rebuild] 제목 교정: {version['title']!r} → {new[0]!r}")
        version["title"] = new[0][:40]
        changed += 1
    k = 1
    for it in version["items"]:
        if it["type"] == "N":
            if new[k] and new[k] != it["text"]:
                log(f"[rebuild] 내레이션 교정: {it['text']!r} → {new[k]!r}")
                it["text"] = new[k]
                it["plan_sec"] = narration_plan_sec(new[k])
                changed += 1
            k += 1
    version["issues"] = [x for x in version["issues"] if not x.startswith("[비유 의심]")] + [f"[비유 교정] {changed}건"]
    version["literal_flags"] = literal_action_flags([version["title"]] + [it["text"] for it in version["items"] if it["type"] == "N"], index, transcript)
    return changed


# ── 제작 가이드 벨트(2026-09-11): 지양 단어가 든 문구만 고쳐 쓴다 ────────────────
GUIDE_POLISH_PROMPT = """너는 쇼츠 대본 교정자다. 아래 [제작 가이드]의 지양 단어가 제목·내레이션·효과자막에 들어 있다. 걸린 문구만 뜻은 살리고
**지양 단어를 쓰지 않는 완곡한 표현**으로 고쳐 써라(예: "콘돔 발견" → "수상한 물건 발견"). 걸리지 않은 문구는 한 글자도 바꾸지 마라.
효과자막은 대괄호·소괄호 포함 16자 이내, 제목은 24자 이내, 내레이션은 8~30자.

[제작 가이드]
{guide}
지양 단어: {avoid}

[걸린 문구]
{hits}

[문구 목록 — 같은 개수·같은 순서로 돌려준다]
{texts}

출력 JSON 하나(코드블록 금지): {{"texts": ["...", ...]}}
"""


def _guide_texts(version: dict) -> list[tuple[str, str, int | None]]:
    """(종류, 문구, 항목 번호) — 제목 · N 내레이션 · 모든 항목의 효과자막(화면에 나가는 문구 전부)."""
    out: list[tuple[str, str, int | None]] = [("title", version.get("title") or "", None)]
    for k, it in enumerate(version["items"]):
        if it["type"] == "N":
            out.append(("N", it.get("text") or "", k))
        if it.get("effect"):
            out.append(("effect", it["effect"], k))
    return out


def guide_hits(version: dict, avoid: list[str] | None) -> list[tuple[str, str]]:
    """제목·내레이션·효과자막 중 지양 단어가 든 문구. 순수 — 테스트 대상."""
    if not avoid:
        return []
    return avoid_hits([t for _, t, _ in _guide_texts(version)], avoid)


def polish_guide(gemini: Gemini, version: dict, guide: dict, *, log=print) -> int:
    """[가이드 위반] 문구만 고쳐 쓴다(제자리 갱신). 바뀐 문구 수. 걸린 게 없으면 호출 0. 고친 뒤에도 남으면 크게 기록한다."""
    avoid = guide.get("avoid") or []
    hits = guide_hits(version, avoid)
    if not hits:
        return 0
    entries = _guide_texts(version)
    texts = [t for _, t, _ in entries]
    raw = gemini.text_json(GUIDE_POLISH_PROMPT.format(guide=guide.get("text", "")[:2000], avoid=", ".join(avoid),
                                                       hits="\n".join(f"- {w!r}: {t}" for w, t in hits),
                                                       texts="\n".join(f"{k}. {t}" for k, t in enumerate(texts, 1))),
                           kind="guide_polish", thinking="low")
    new = [" ".join(str(x).split()) for x in (raw.get("texts") or [])]
    if len(new) != len(texts):
        log(f"[guide] ⚠ 다듬기 응답 개수 불일치({len(new)}≠{len(texts)}) — 적용 안 함")
        return 0
    changed = 0
    for (kind, old, k), nt in zip(entries, new):
        if not nt or nt == old or not any(w in old for w in avoid):
            continue                                       # 걸린 문구만 바꾼다 — 나머지는 모델이 손대도 무시
        if kind == "title":
            version["title"] = nt[:40]
        elif kind == "N":
            version["items"][k]["text"] = nt
            version["items"][k]["plan_sec"] = narration_plan_sec(nt)
        else:
            version["items"][k]["effect"] = nt[:16]
        log(f"[guide] {kind} 교정: {old!r} → {nt!r}")
        changed += 1
    version["plan_sec"] = ms3(sum(it["plan_sec"] for it in version["items"]))
    remain = guide_hits(version, avoid)
    version["guide_hits"] = [list(h) for h in remain]
    version["issues"] = [x for x in version.get("issues", []) if not x.startswith("[가이드 위반]")] + [f"[가이드 교정] {changed}건"] \
        + [f"[가이드 위반] {w!r} in {t!r} (교정 후에도 남음)" for w, t in remain]
    for w, t in remain:
        log(f"[guide] ⚠⚠ 교정 후에도 지양 단어 {w!r} 가 남았다: {t!r}")
    return changed


def apply_name_map(version: dict, actors: dict[str, str], *, log=print) -> int:
    """제목·내레이션·효과자막의 극중 이름을 배우 이름으로 치환(제자리). 긴 이름부터 치환해 부분 겹침(예: '경희'⊂'박경희')을 막는다.
    대사(S text)는 소리 그대로라 손대지 않는다 — 화자 라벨은 렌더가 바꾼다. 바뀐 문구 수. 순수(호출 0) — 테스트 대상."""
    if not actors:
        return 0
    pairs = sorted(((c, a) for c, a in actors.items() if c and a and c != a), key=lambda x: -len(x[0]))

    def sub(t: str) -> str:
        for c, a in pairs:
            t = t.replace(c, a)
        return t

    changed = 0
    new_title = sub(version.get("title") or "")
    if new_title != (version.get("title") or ""):
        log(f"[guide] 제목 배우 표기: {version['title']!r} → {new_title!r}")
        version["title"] = new_title[:40]
        changed += 1
    for it in version["items"]:
        if it["type"] == "N":
            nt = sub(it["text"])
            if nt != it["text"]:
                log(f"[guide] 내레이션 배우 표기: {it['text']!r} → {nt!r}")
                it["text"] = nt
                it["plan_sec"] = narration_plan_sec(nt)
                changed += 1
        if it.get("effect"):
            ne = sub(it["effect"])
            if ne != it["effect"]:
                it["effect"] = ne[:16]
                changed += 1
    if changed:
        version["plan_sec"] = ms3(sum(it["plan_sec"] for it in version["items"]))
        version["issues"] = version.get("issues", []) + [f"[배우 표기] {changed}건 치환"]
    return changed


# ── 재순위(2026-09-11): 리빌딩 호출의 ranking 은 프롬프트 예시를 베끼거나 예시의 추천 번호(3)에 끌렸다 — 예시 없는 별도 호출로 다시 매긴다 ──
RERANK_PROMPT = """너는 드라마 쇼츠 채널의 편집장이다. 아래는 작품 「{title}」 {episode} 로 만든 쇼츠 대본 {k}개다(번호·전략·제목·항목).
**조회수 기대가 높은 순**으로 줄 세워라. 기준: ① 첫 3초 훅의 강도(대사 자체의 자극·의외성) ② 갈등의 선명함과 대사 티키타카 밀도
③ 결말의 미끼(다음이 궁금한가) ④ 같은 장면을 재탕한 대본은 뒤로 ⑤ 선형 계열(구간 순차형·루프형·장면 통째 압축형·점층 빌드업형)은
흐름이 매끄럽고 끝이 잘 닫히는지. 번호는 아무 의미가 없다 — 앞 번호를 우대하지 마라. 각 대본을 실제로 읽고 비교해 정한다.

{blocks}

출력 JSON 하나(코드블록 금지):
{{"ranking": [번호 {k}개 전부, 중복 없이, 높은 순], "recommended": ranking 의 첫 번호, "reason": "1위를 고른 이유 한 줄",
 "notes": {{"번호": "그 대본의 강점/약점 한 줄", …}}}}
"""


def version_block(v: dict) -> str:
    out = [f"### 버전 {v['n']} · {v['strategy']} · 제목 \"{v['title']}\" · 계획 {v.get('plan_sec', 0):.0f}s"]
    for it in v["items"]:
        eff = f" 〈{it['effect']}〉" if it.get("effect") else ""
        if it["type"] == "N":
            out.append(f"- N: {it['text']}{eff}")
        elif it["type"] == "S":
            out.append(f"- S {it.get('speaker') or '미상'}: {it.get('text', '')[:80]}{eff}")
        else:
            out.append(f"- A: {it.get('desc', '')[:60]}{eff}")
    return "\n".join(out)


def rerank(gemini: Gemini, versions: list[dict], *, title: str, episode: str, log=print) -> dict:
    """대본 목록 → {ranking, recommended, reason, notes}. 실패하면 번호순(기록). 순수하지 않음(호출 1회) — 파서는 rank_versions 재사용."""
    valid = [v["n"] for v in versions]
    blocks = "\n\n".join(version_block(v) for v in versions)
    try:
        raw = gemini.text_json(RERANK_PROMPT.format(title=title, episode=episode, k=len(versions), blocks=blocks), kind="rerank", thinking="medium",
                               max_output_tokens=8192)
    except Exception as e:  # noqa: BLE001
        log(f"[rerank] ⚠ 재순위 실패 → 번호순 유지: {type(e).__name__}: {str(e)[:160]}")
        return {"ranking": valid, "recommended": valid[0] if valid else None, "reason": f"재순위 실패({type(e).__name__})", "notes": {}, "ok": False}
    try:
        rec = int(raw.get("recommended"))
    except (TypeError, ValueError):
        rec = None
    ranking = rank_versions(raw.get("ranking"), rec, valid)
    notes = {str(k): str(v)[:80] for k, v in (raw.get("notes") or {}).items()} if isinstance(raw.get("notes"), dict) else {}
    return {"ranking": ranking, "recommended": ranking[0] if ranking else rec, "reason": str(raw.get("reason") or "")[:200], "notes": notes, "ok": True}


def apply_rerank(job: Job, gemini: Gemini, data: dict, *, title: str, episode: str, basis: str = "draft") -> dict:
    """rebuild 데이터의 ranking/recommended/reason 을 재순위로 교체(제자리). 리빌딩 모델의 원래 값은 model_ranking 으로 보존.
    basis: 'draft'(리빌딩 초안) | 'verified'(확인 패스 최종본 — 호출부가 versions 를 최종본으로 바꿔 준다)."""
    res = rerank(gemini, data["versions"], title=title, episode=episode, log=job.log)
    if not res["ok"]:
        return data
    data.setdefault("model_ranking", {"ranking": data.get("ranking"), "recommended": data.get("recommended"), "reason": data.get("reason")})
    data["ranking"], data["recommended"], data["reason"] = res["ranking"], res["recommended"], res["reason"]
    data["rerank"] = {"basis": basis, "notes": res["notes"]}
    job.log(f"[rerank] 재순위({basis}) → {res['ranking']} · 추천 v{res['recommended']} — {res['reason']}")
    job.record_step("rerank", basis=basis, ranking=res["ranking"], recommended=res["recommended"], llm=gemini.usage.calls[-1:])
    return data
