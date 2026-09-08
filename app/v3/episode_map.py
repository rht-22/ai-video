"""회차 지도 — Stage 2 기록 위를 두 번 읽어(정방향 상태 갱신 → 역방향 수정) 사실/믿음 장부·
설정/회수 레지스터·열린 질문·diegesis 확정·사람 확인 목록을 만든다 (3단계, 2026-09-08).

근거: docs/v3_gaps_from_manual_shorts.md 「갭 너머 — 필요한 것 7가지」「무엇을 고정하고 무엇을
루프로 열지」. 인수인계: docs/v3_gaps_handoff.md §3.

- **Stage 2 를 덮어쓰지 않는 additive 주석 층**이다. stage2.json 은 불변, 산출은 episode_map.json.
- 텍스트 온리(Flash · 온도 0). 시퀀스당 정방향 1콜 + 역방향 1콜 + 재질의(≤MAX_REASKS).
- 정방향의 핵심 질문은 "이 장면을 보고 나서 **이야기에 대한 이해가 어떻게 바뀌었나**"(묘사가 아니다).
  역방향은 마지막 시퀀스부터 거꾸로, "앞 시퀀스의 판정 중 뒤 증거와 충돌하는 것"만 고친다 —
  카페 장면(EP01 2540~2600)이 2431 일터 근거로 imagined/unclear 로 확정되는 것이 존재 이유.
- 검증기(순수): 모든 id 가 존재하는 meaning/span 인가 · register kind 화이트리스트 · setup.t < payoff.t.
- 캐시 지문 = sha1(stage2 schema + meaning/span 내용) — 2단계 재실행이면 자동 무효.
- 사람 교정 `<job>/map_overrides.json` 은 지도 로드 직후 얹는다(diegesis·인명·레지스터 항목).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Callable

from app.v3 import schemas
from app.v3.story_flow.common import (MAX_REASKS, call_json, fmt_t, meaning_rows,
                                      reject_block, screen_text_tag)

SCHEMA_MAP = "v3_map/v1"
STATE_MAX_CHARS = 4000          # 정방향 프롬프트에 싣는 직전 상태 문서 상한(넘으면 압축)
REGISTER_KINDS = ("condition", "phrase", "object", "claim", "symmetry")
DIEGESIS_VALUES = ("actual", "imagined", "recalled", "unclear")
FACT_SOURCES = ("dialogue", "screen_text", "action")
REVIEW_KINDS = ("diegesis", "name", "cross_episode")
TEXT_MAX = 200
MAX_ITEMS_PER_SEQ = 40          # 시퀀스당 사실/믿음/레지스터 항목 상한(조용한 절단 금지 — note)

FORWARD_PROMPT = """당신은 드라마 회차의 **이야기 상태를 추적하는 기록가**다. 영상은 볼 수 없다 — 아래 사건 기록이 정본이다.
지금까지의 이해(직전 상태)를 들고 이 시퀀스를 읽은 뒤, **이야기에 대한 이해가 어떻게 바뀌었는지**를 적는다. 장면 묘사가 아니다.

규칙:
- **사실**(facts): 시청자에게 확정적으로 제시된 것 — 대사(dialogue)·화면 글자(screen_text · 📄 표시)·행동(action). 화면 글자는 대사와 같은 급의 사실이다. 근거 meaning id 를 단다.
- **믿음**(beliefs): 어떤 인물이 그렇다고 믿는 것(사실과 분리). holder 를 단다. 뒤에서 확인/번복될 수 있다.
- **레지스터**(register): 드라마가 심어 둔 설정과 그 회수 — condition(조건·약속), phrase(반복될 말), object(물건), claim(단언 — ⚠claim 표시된 대사), symmetry(같은 구도의 반복·대칭). setup 은 이 시퀀스의 meaning, payoff 는 회수가 **이 시퀀스에서** 일어났을 때만. 앞 시퀀스의 항목(r001…)이 여기서 회수되면 `payoff_of` 에 그 id 를 적어라.
- **열린 질문**(open_questions): 이 시점에 시청자가 궁금한 것.
- **diegesis**: 이 시퀀스의 span 중 실제 사건이 아닌 것(상상 imagined · 회상 recalled · 판단 불가 unclear)만 span id 로. 기록의 ⚠ 표시는 초벌 판정이다 — 앞뒤 사실로 판단이 서면 바꿔라, 확신 없으면 unclear.
- 지어내지 마라. 기록에 없는 사실은 없다.

## 작품
{work_title}{research_block}

## 직전까지의 상태
{state_block}

## 이 시퀀스 {seq_no} [{seq_t0}~{seq_t1}] — {seq_content}
사건 단위 (id | 시각 | 길이 | importance | 분위기 | 인물 | 내용 · 📄 화면 글자 · ⚠ 연출 층위):
{meaning_block}
{span_block}{reject_block}
## 출력 (JSON 만)
{{"facts": [{{"text": "…", "source": "dialogue|screen_text|action", "meanings": ["m012"]}}],
  "beliefs": [{{"holder": "인물", "text": "…", "meanings": ["m013"]}}],
  "characters": {{"인물": {{"knows": ["…"], "believes": ["…"], "wants": ["…"]}}}},
  "open_questions": ["…"],
  "register": [{{"kind": "condition", "setup": {{"meaning": "m020", "quote": "…"}}, "payoff": null, "what_flipped": ""}},
               {{"kind": "claim", "payoff_of": "r001", "payoff": {{"meaning": "m037", "quote": "…"}}, "what_flipped": "…"}}],
  "diegesis": {{"sp1175": "imagined"}}}}"""

BACKWARD_PROMPT = """당신은 드라마 회차의 **이야기 상태를 되짚는 기록가**다. 영상은 볼 수 없다 — 기록이 정본이다.
회차 끝까지 읽은 **최종 상태**를 들고 시퀀스 {seq_no} 로 돌아왔다. 이 시퀀스에서 내린 판정 중 **뒤에 나온 증거와 충돌하는 것만** 고쳐라. 충돌이 없으면 빈 값을 내라 — 새 항목을 만들지 마라.
- diegesis_changes: 이 시퀀스 span 중 뒤 증거로 판정이 바뀌는 것(예: 같은 시각 그 인물이 다른 장소에 있었다 → imagined/unclear). evidence 에 근거 meaning id 와 한 줄.
- beliefs_confirmed: 이 시퀀스에서 생긴 믿음(b…)이 뒤에서 사실로 확인(true)/번복(false)됐으면.
- register_payoffs: 이 시퀀스의 레지스터 항목(r…)의 회수가 뒤에서 일어났는데 아직 payoff 가 비어 있으면 그 meaning 과 인용.

## 작품
{work_title}

## 최종 상태
{state_block}

## 시퀀스 {seq_no} 의 판정
{seq_block}
{reject_block}
## 출력 (JSON 만)
{{"diegesis_changes": {{"sp1175": {{"value": "imagined", "evidence": ["m043: 2431 일터에 있었다"]}}}},
  "beliefs_confirmed": {{"b001": true}},
  "register_payoffs": [{{"id": "r001", "payoff": {{"meaning": "m037", "quote": "…"}}, "what_flipped": "…"}}]}}"""


# ── 재료(순수) ────────────────────────────────────────────────────────────────

def fingerprint_of(stage2_doc: dict) -> str:
    """stage2 의 schema + meaning/span 내용 지문 — 2단계 재실행이면 값이 바뀐다."""
    body = []
    for sq in stage2_doc.get("sequences") or []:
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                body.append([m.get("content"), m.get("importance"),
                             [[s.get("span_id"), s.get("scene_script"), s.get("screen_text"),
                               s.get("diegesis"), s.get("is_claim"),
                               [a.get("line") for a in s.get("audio_script") or []]]
                              for s in m.get("spans") or []]])
    return hashlib.sha1(json.dumps(
        {"schema": stage2_doc.get("schema"), "body": body}, sort_keys=True,
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def span_table(stage2_doc: dict) -> dict[str, dict]:
    """span id → {t0, t1, seq, meaning, diegesis, is_claim, screen_text, text}."""
    out: dict[str, dict] = {}
    mi = -1
    for si, sq in enumerate(stage2_doc.get("sequences") or []):
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                mi += 1
                for s in m.get("spans") or []:
                    sid = s.get("span_id")
                    if not isinstance(sid, str):
                        continue
                    out[sid] = {
                        "t0": schemas.parse_ts(s["time"]["start"]),
                        "t1": schemas.parse_ts(s["time"]["end"]),
                        "seq": si, "meaning": mi,
                        "diegesis": s.get("diegesis") or None,
                        "is_claim": bool(s.get("is_claim")),
                        "screen_text": s.get("screen_text") or None,
                        "text": " / ".join(str(a.get("line") or "") for a in s.get("audio_script") or []),
                        "scene_script": s.get("scene_script") or "",
                    }
    return out


def meaning_block_for(rows: list[dict], seq: int) -> str:
    lines = []
    for r in rows:
        if r["seq"] != seq:
            continue
        lines.append(
            f"m{r['idx']:03d} | {fmt_t(r['t0'])}~{fmt_t(r['t1'])} | {r['t1'] - r['t0']:.0f}s | "
            f"imp {r['importance']} | {r['mood']} | {'/'.join(r['characters'])} | {r['content']}"
            + screen_text_tag(r.get("screen_texts"), limit=2)
            + (f" ⚠ 회상/상상({'/'.join(r['diegesis'])})" if r.get("diegesis") else ""))
    return "\n".join(lines)


def span_block_for(spans: dict[str, dict], seq: int) -> str:
    """시퀀스의 **표시할 만한** span 만 — 화면 글자·단언·비actual 판정. 없으면 빈 문자열."""
    lines = []
    for sid, sp in sorted(spans.items(), key=lambda kv: kv[1]["t0"]):
        if sp["seq"] != seq:
            continue
        tags = []
        if sp.get("screen_text"):
            tags.append(f'📄 "{sp["screen_text"][:80]}"')
        if sp.get("is_claim"):
            tags.append(f"⚠claim 「{sp['text'][:60]}」")
        if sp.get("diegesis") and sp["diegesis"] != "actual":
            tags.append(f"⚠ 초벌 {sp['diegesis']}: {sp['scene_script'][:60]}")
        if tags:
            lines.append(f"{sid} m{sp['meaning']:03d} {fmt_t(sp['t0'])} | " + " · ".join(tags))
    if not lines:
        return ""
    return "\n표시 span (id | 사건 | 시각 | 화면 글자 📄 / 단언 ⚠claim / 연출 층위 초벌):\n" + "\n".join(lines) + "\n"


def empty_map(fingerprint: str, work_title: str = "") -> dict:
    return {"schema": SCHEMA_MAP, "fingerprint": fingerprint, "work_title": work_title,
            "state_by_sequence": [], "facts_ledger": [], "beliefs_ledger": [], "register": [],
            "diegesis_final": {}, "review": [], "corrections": []}


def compress_state(map_doc: dict, *, max_chars: int = STATE_MAX_CHARS) -> str:
    """정방향 프롬프트용 직전 상태 문서(압축 JSON). 상한을 넘으면 사실/믿음 본문을 줄이고
    열린 질문·레지스터·인물 상태만 남긴다(넘어도 잘라 낸다 — 프롬프트가 폭주하면 안 된다)."""
    last = map_doc["state_by_sequence"][-1] if map_doc["state_by_sequence"] else None
    full = {
        "characters": (last or {}).get("characters") or {},
        "open_questions": (last or {}).get("open_questions") or [],
        "facts": [{"id": f["id"], "text": f["text"]} for f in map_doc["facts_ledger"]],
        "beliefs": [{"id": b["id"], "holder": b["holder"], "text": b["text"],
                     "confirmed": b.get("confirmed")} for b in map_doc["beliefs_ledger"]],
        "register": [{"id": r["id"], "kind": r["kind"], "setup": r["setup"].get("quote"),
                      "payoff": (r.get("payoff") or {}).get("quote")} for r in map_doc["register"]],
    }
    if not last:
        return "(없음 — 첫 시퀀스)"
    s = json.dumps(full, ensure_ascii=False)
    if len(s) <= max_chars:
        return s
    slim = dict(full)
    slim["facts"] = f"{len(full['facts'])}건(생략 — 최근 8건: " + " / ".join(
        f["text"] for f in full["facts"][-8:]) + ")"
    slim["beliefs"] = [b for b in full["beliefs"] if b.get("confirmed") is None][-8:]
    s = json.dumps(slim, ensure_ascii=False)
    if len(s) <= max_chars:
        return s
    slim["register"] = slim["register"][-12:]
    s = json.dumps(slim, ensure_ascii=False)
    return s[:max_chars]


def _mid(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    m = re.fullmatch(r"\s*m?(\d+)\s*", str(v or ""))
    return int(m.group(1)) if m else None


def _ids(v: Any, known: set[int]) -> tuple[list[int], list[str]]:
    out, bad = [], []
    for x in (v if isinstance(v, list) else [v]) if v is not None else []:
        k = _mid(x)
        if k is None or k not in known:
            bad.append(str(x))
        elif k not in out:
            out.append(k)
    return out, bad


def _quote(v: Any) -> str:
    return str(v or "").strip()[:TEXT_MAX]


# ── 검증(순수) ────────────────────────────────────────────────────────────────

def validate_forward(resp: Any, *, seq: int, rows_by_idx: dict[int, dict],
                     spans: dict[str, dict], register_ids: set[str]
                     ) -> tuple[dict | None, list[str], list[str]]:
    """정방향 응답 → 정규화. id 부재·kind 밖·시간 역전은 반려(재질의 재료). 과잉은 note."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []
    problems: list[str] = []
    notes: list[str] = []
    seq_meanings = {i for i, r in rows_by_idx.items() if r["seq"] == seq}
    known = set(rows_by_idx)
    t_of = {i: r["t0"] for i, r in rows_by_idx.items()}

    facts = []
    for k, f in enumerate(resp.get("facts") or []):
        if not isinstance(f, dict) or not _quote(f.get("text")):
            problems.append(f"facts[{k}] text 없음")
            continue
        src = str(f.get("source") or "action")
        if src not in FACT_SOURCES:
            notes.append(f"facts[{k}] source {src!r} → action")
            src = "action"
        ids, bad = _ids(f.get("meanings"), known)
        if bad:
            problems.append(f"facts[{k}] 모르는 meaning id {bad[:3]}")
            continue
        if not ids:
            problems.append(f"facts[{k}] 근거 meaning 이 없다")
            continue
        facts.append({"text": _quote(f["text"]), "source": src, "meanings": ids,
                      "t": round(min(t_of[i] for i in ids), 3)})
    beliefs = []
    for k, b in enumerate(resp.get("beliefs") or []):
        if not isinstance(b, dict) or not _quote(b.get("text")):
            problems.append(f"beliefs[{k}] text 없음")
            continue
        ids, bad = _ids(b.get("meanings"), known)
        if bad:
            problems.append(f"beliefs[{k}] 모르는 meaning id {bad[:3]}")
            continue
        beliefs.append({"holder": _quote(b.get("holder")) or "미상", "text": _quote(b["text"]),
                        "meanings": ids})
    chars: dict[str, dict] = {}
    cin = resp.get("characters")
    if isinstance(cin, dict):
        for name, st in cin.items():
            if not isinstance(st, dict):
                continue
            chars[str(name)[:40]] = {key: [_quote(x) for x in (st.get(key) or []) if _quote(x)][:12]
                                     for key in ("knows", "believes", "wants")}
    elif cin is not None:
        notes.append("characters 가 객체가 아님 — 무시")
    open_q = [_quote(x) for x in (resp.get("open_questions") or []) if _quote(x)][:12]

    register = []
    for k, r in enumerate(resp.get("register") or []):
        if not isinstance(r, dict):
            problems.append(f"register[{k}] 가 객체가 아님")
            continue
        kind = str(r.get("kind") or "")
        if kind not in REGISTER_KINDS:
            problems.append(f"register[{k}] kind {kind!r} — {'/'.join(REGISTER_KINDS)} 중 하나")
            continue
        payoff = None
        if isinstance(r.get("payoff"), dict):
            pid = _mid(r["payoff"].get("meaning"))
            if pid is None or pid not in known:
                problems.append(f"register[{k}] payoff meaning 모름: {r['payoff'].get('meaning')!r}")
                continue
            if pid not in seq_meanings:
                problems.append(f"register[{k}] payoff 는 이 시퀀스의 meaning 이어야 한다(m{pid:03d} 는 다른 시퀀스)")
                continue
            payoff = {"meaning": pid, "t": round(t_of[pid], 3), "quote": _quote(r["payoff"].get("quote"))}
        pof = r.get("payoff_of")
        if pof:
            pof = str(pof).strip()
            if pof not in register_ids:
                problems.append(f"register[{k}] payoff_of {pof!r} — 없는 레지스터 id")
                continue
            if payoff is None:
                problems.append(f"register[{k}] payoff_of 인데 payoff 가 없다")
                continue
            register.append({"kind": kind, "payoff_of": pof, "payoff": payoff,
                             "what_flipped": _quote(r.get("what_flipped"))})
            continue
        setup = r.get("setup")
        if not isinstance(setup, dict):
            problems.append(f"register[{k}] setup 이 없다(새 항목은 setup 필수)")
            continue
        sid_ = _mid(setup.get("meaning"))
        if sid_ is None or sid_ not in known:
            problems.append(f"register[{k}] setup meaning 모름: {setup.get('meaning')!r}")
            continue
        if payoff is not None and not t_of[sid_] < payoff["t"]:
            problems.append(f"register[{k}] setup(m{sid_:03d}) 이 payoff(m{payoff['meaning']:03d}) 보다 뒤다")
            continue
        register.append({"kind": kind,
                         "setup": {"meaning": sid_, "t": round(t_of[sid_], 3), "quote": _quote(setup.get("quote"))},
                         "payoff": payoff, "what_flipped": _quote(r.get("what_flipped"))})
    dieg: dict[str, str] = {}
    din = resp.get("diegesis")
    if isinstance(din, dict):
        for sid, val in din.items():
            if sid not in spans:
                problems.append(f"diegesis {sid!r} — 없는 span id")
                continue
            if spans[sid]["seq"] != seq:
                notes.append(f"diegesis {sid} 는 다른 시퀀스 — 무시(역방향 몫)")
                continue
            if val not in DIEGESIS_VALUES:
                notes.append(f"diegesis {sid} {val!r} 폐기({'/'.join(DIEGESIS_VALUES)} 만)")
                continue
            dieg[sid] = str(val)
    elif din is not None:
        notes.append("diegesis 가 객체가 아님 — 무시")
    for name, lst in (("facts", facts), ("beliefs", beliefs), ("register", register)):
        if len(lst) > MAX_ITEMS_PER_SEQ:
            notes.append(f"{name} {len(lst)}건 → 앞 {MAX_ITEMS_PER_SEQ}건만(상한)")
            del lst[MAX_ITEMS_PER_SEQ:]
    if problems:
        return None, problems, notes
    return {"facts": facts, "beliefs": beliefs, "characters": chars, "open_questions": open_q,
            "register": register, "diegesis": dieg}, [], notes


def validate_backward(resp: Any, *, seq: int, rows_by_idx: dict[int, dict], spans: dict[str, dict],
                      map_doc: dict) -> tuple[dict | None, list[str], list[str]]:
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []
    problems: list[str] = []
    notes: list[str] = []
    known = set(rows_by_idx)
    t_of = {i: r["t0"] for i, r in rows_by_idx.items()}
    changes: dict[str, dict] = {}
    din = resp.get("diegesis_changes")
    if isinstance(din, dict):
        for sid, obj in din.items():
            if sid not in spans:
                problems.append(f"diegesis_changes {sid!r} — 없는 span id")
                continue
            if spans[sid]["seq"] != seq:
                notes.append(f"diegesis_changes {sid} 는 다른 시퀀스 — 무시")
                continue
            val = obj.get("value") if isinstance(obj, dict) else obj
            if val not in DIEGESIS_VALUES:
                notes.append(f"diegesis_changes {sid} {val!r} 폐기")
                continue
            ev = [_quote(x) for x in ((obj.get("evidence") or []) if isinstance(obj, dict) else [])][:4]
            changes[sid] = {"value": str(val), "evidence": ev}
    confirmed: dict[str, bool] = {}
    bids = {b["id"] for b in map_doc["beliefs_ledger"] if b.get("seq") == seq}
    cin = resp.get("beliefs_confirmed")
    if isinstance(cin, dict):
        for bid, v in cin.items():
            if bid not in bids:
                problems.append(f"beliefs_confirmed {bid!r} — 이 시퀀스의 믿음 id 가 아니다")
                continue
            if not isinstance(v, bool):
                notes.append(f"beliefs_confirmed {bid} {v!r} 폐기(bool 만)")
                continue
            confirmed[bid] = v
    payoffs: list[dict] = []
    reg_by_id = {r["id"]: r for r in map_doc["register"]}
    for k, p in enumerate(resp.get("register_payoffs") or []):
        if not isinstance(p, dict):
            continue
        rid = str(p.get("id") or "")
        r = reg_by_id.get(rid)
        if r is None or r.get("seq") != seq:
            problems.append(f"register_payoffs[{k}] {rid!r} — 이 시퀀스의 레지스터 id 가 아니다")
            continue
        po = p.get("payoff") if isinstance(p.get("payoff"), dict) else {}
        pid = _mid(po.get("meaning"))
        if pid is None or pid not in known:
            problems.append(f"register_payoffs[{k}] payoff meaning 모름")
            continue
        if not r["setup"]["t"] < t_of[pid]:
            problems.append(f"register_payoffs[{k}] payoff(m{pid:03d}) 가 setup 보다 앞이다")
            continue
        if r.get("payoff"):
            notes.append(f"register_payoffs[{k}] {rid} 는 이미 payoff 가 있다 — 무시")
            continue
        payoffs.append({"id": rid, "payoff": {"meaning": pid, "t": round(t_of[pid], 3),
                                              "quote": _quote(po.get("quote"))},
                        "what_flipped": _quote(p.get("what_flipped"))})
    if problems:
        return None, problems, notes
    return {"diegesis_changes": changes, "beliefs_confirmed": confirmed, "register_payoffs": payoffs}, [], notes


# ── 병합(순수) ────────────────────────────────────────────────────────────────

def merge_forward(map_doc: dict, seq: int, obj: dict, spans: dict[str, dict]) -> dict:
    """정방향 결과를 지도에 얹는다(id 는 코드가 매긴다). 반환 = 같은 dict(그 자리에서)."""
    fid = len(map_doc["facts_ledger"])
    fact_ids = []
    for f in obj["facts"]:
        fid += 1
        item = {"id": f"f{fid:03d}", "seq": seq, **f}
        map_doc["facts_ledger"].append(item)
        fact_ids.append(item["id"])
    bid = len(map_doc["beliefs_ledger"])
    belief_ids = []
    for b in obj["beliefs"]:
        bid += 1
        item = {"id": f"b{bid:03d}", "seq": seq, "confirmed": None, **b}
        map_doc["beliefs_ledger"].append(item)
        belief_ids.append(item["id"])
    rid = len(map_doc["register"])
    reg_by_id = {r["id"]: r for r in map_doc["register"]}
    for r in obj["register"]:
        if r.get("payoff_of"):
            tgt = reg_by_id.get(r["payoff_of"])
            if tgt is not None and not tgt.get("payoff"):
                tgt["payoff"] = r["payoff"]
                tgt["what_flipped"] = r.get("what_flipped") or tgt.get("what_flipped", "")
                tgt["payoff_seq"] = seq
            continue
        rid += 1
        map_doc["register"].append({"id": f"r{rid:03d}", "seq": seq, **r})
    for sid, val in obj["diegesis"].items():
        prev = spans.get(sid, {}).get("diegesis")
        entry = {"value": val, "evidence": [f"forward seq {seq}"],
                 "changed_from": prev if prev != val else None, "stage2": prev}
        if val == "actual" and (prev in (None, "actual")):
            continue                                   # 기본값 확인은 기록할 것이 없다
        map_doc["diegesis_final"][sid] = entry
    map_doc["state_by_sequence"].append({
        "seq": seq, "characters": obj["characters"], "open_questions": obj["open_questions"],
        "facts": fact_ids, "beliefs": belief_ids})
    return map_doc


def apply_backward(map_doc: dict, seq: int, obj: dict, spans: dict[str, dict]) -> dict:
    """역방향 diff 적용 — diegesis_final 변경분·beliefs.confirmed·레지스터 payoff 연결."""
    changed = 0
    for sid, ch in obj["diegesis_changes"].items():
        cur = map_doc["diegesis_final"].get(sid)
        before = cur["value"] if cur else (spans.get(sid, {}).get("diegesis") or "actual")
        if before == ch["value"]:
            continue
        map_doc["diegesis_final"][sid] = {
            "value": ch["value"], "evidence": ch["evidence"] or [f"backward seq {seq}"],
            "changed_from": before, "stage2": spans.get(sid, {}).get("diegesis")}
        changed += 1
    by_b = {b["id"]: b for b in map_doc["beliefs_ledger"]}
    for bid, v in obj["beliefs_confirmed"].items():
        if bid in by_b:
            by_b[bid]["confirmed"] = v
    by_r = {r["id"]: r for r in map_doc["register"]}
    for p in obj["register_payoffs"]:
        r = by_r.get(p["id"])
        if r is not None and not r.get("payoff"):
            r["payoff"] = p["payoff"]
            r["what_flipped"] = p.get("what_flipped") or r.get("what_flipped", "")
            r["payoff_pass"] = "backward"
    map_doc.setdefault("backward_changes", []).append(
        {"seq": seq, "diegesis": changed, "beliefs": len(obj["beliefs_confirmed"]),
         "payoffs": len(obj["register_payoffs"])})
    return map_doc


def build_review(map_doc: dict, spans: dict[str, dict], name_variants: list[dict] | None = None) -> list[dict]:
    """사람 확인 목록 — diegesis(actual 아님·초벌과 다름)·인명 불일치. 순수."""
    out: list[dict] = []
    for sid, e in sorted(map_doc["diegesis_final"].items(), key=lambda kv: spans.get(kv[0], {}).get("t0", 0)):
        if e["value"] == "actual" and not e.get("changed_from"):
            continue
        sp = spans.get(sid, {})
        out.append({"kind": "diegesis", "span_ids": [sid],
                    "meanings": [sp.get("meaning")] if sp.get("meaning") is not None else [],
                    "value": e["value"], "changed_from": e.get("changed_from"),
                    "why": "; ".join(e.get("evidence") or []) or "연출 층위 판정 — 사람 확인"})
    for v in name_variants or []:
        out.append({"kind": "name", "a": v.get("a"), "b": v.get("b"), "suggest": v.get("suggest"),
                    "why": "전사 내부 표기 불일치"})
    return out


def apply_overrides(map_doc: dict, overrides: dict | None, spans: dict[str, dict]) -> list[str]:
    """`map_overrides.json` 병합 — {diegesis: {sp: value}, names: {틀림: 맞음}, register: [항목]}.
    모르는 span·값은 즉시 실패(사람 값이 조용히 증발하면 안 된다 — styles 규율). 적용 노트 반환."""
    if not overrides:
        return []
    if not isinstance(overrides, dict):
        raise ValueError("map_overrides.json 은 객체여야 한다")
    notes: list[str] = []
    for sid, val in (overrides.get("diegesis") or {}).items():
        if sid not in spans:
            raise ValueError(f"map_overrides diegesis: 없는 span {sid!r}")
        if val not in DIEGESIS_VALUES:
            raise ValueError(f"map_overrides diegesis {sid}: 값 {val!r}")
        cur = map_doc["diegesis_final"].get(sid, {}).get("value") or spans[sid].get("diegesis") or "actual"
        map_doc["diegesis_final"][sid] = {"value": val, "evidence": ["human override"],
                                          "changed_from": cur if cur != val else None,
                                          "stage2": spans[sid].get("diegesis"), "human": True}
        notes.append(f"diegesis {sid} → {val}(사람)")
    names = overrides.get("names") or {}
    if names:
        if not isinstance(names, dict):
            raise ValueError("map_overrides names 는 객체({틀림: 맞음})여야 한다")
        map_doc["name_overrides"] = {str(a): str(b) for a, b in names.items()}
        notes.append(f"인명 교정 {len(names)}건(사람)")
    for k, r in enumerate(overrides.get("register") or []):
        if not isinstance(r, dict) or r.get("kind") not in REGISTER_KINDS:
            raise ValueError(f"map_overrides register[{k}]: kind 가 {REGISTER_KINDS} 중 하나여야 한다")
        setup, payoff = r.get("setup") or {}, r.get("payoff")
        sm = _mid(setup.get("meaning"))
        if sm is None:
            raise ValueError(f"map_overrides register[{k}]: setup.meaning 필요")
        item = {"id": f"h{k + 1:03d}", "seq": None, "kind": r["kind"], "human": True,
                "setup": {"meaning": sm, "t": float(setup.get("t") or 0.0), "quote": _quote(setup.get("quote"))},
                "payoff": ({"meaning": _mid(payoff.get("meaning")), "t": float(payoff.get("t") or 0.0),
                            "quote": _quote(payoff.get("quote"))} if isinstance(payoff, dict) else None),
                "what_flipped": _quote(r.get("what_flipped"))}
        map_doc["register"].append(item)
        notes.append(f"레지스터 {item['id']} {r['kind']}(사람)")
    return notes


# ── 실행 ──────────────────────────────────────────────────────────────────────

def _research_block(research_context: str, names: list[str] | None) -> str:
    parts = []
    if names:
        parts.append("등장인물: " + ", ".join(names[:20]))
    rc = (research_context or "").strip()
    if rc:
        parts.append(rc[:2000])
    return ("\n" + "\n".join(parts)) if parts else ""


def seq_state_block(map_doc: dict, seq: int) -> str:
    st = next((s for s in map_doc["state_by_sequence"] if s["seq"] == seq), None)
    if st is None:
        return "(이 시퀀스 판정 없음)"
    facts = [f for f in map_doc["facts_ledger"] if f["id"] in set(st["facts"])]
    beliefs = [b for b in map_doc["beliefs_ledger"] if b["id"] in set(st["beliefs"])]
    regs = [r for r in map_doc["register"] if r.get("seq") == seq]
    dieg = {sid: e["value"] for sid, e in map_doc["diegesis_final"].items()
            if e.get("evidence") and any(f"seq {seq}" in x for x in e["evidence"])}
    return json.dumps({
        "facts": [{"id": f["id"], "text": f["text"]} for f in facts],
        "beliefs": [{"id": b["id"], "holder": b["holder"], "text": b["text"], "confirmed": b.get("confirmed")}
                    for b in beliefs],
        "register": [{"id": r["id"], "kind": r["kind"], "setup": r["setup"].get("quote"),
                      "payoff": (r.get("payoff") or {}).get("quote")} for r in regs],
        "diegesis": dieg, "open_questions": st.get("open_questions")}, ensure_ascii=False)[:STATE_MAX_CHARS]


def run_episode_map(gemini, stage2_doc: dict, grid: dict, *, work_title: str,
                    research_context: str = "", character_names: list[str] | None = None,
                    prior_map: dict | None = None, overrides: dict | None = None,
                    call: Callable[[Any, str], dict] | None = None,
                    log=print) -> tuple[dict, dict]:
    """정방향(시퀀스 순) → 역방향(거꾸로) → 검수 목록. 반려·재질의 ≤MAX_REASKS, 소진 시 크게 실패."""
    from app.v3.story_flow import _loop
    _call = call or call_json
    rows = meaning_rows(stage2_doc)
    rows_by_idx = {r["idx"]: r for r in rows}
    spans = span_table(stage2_doc)
    seqs = sorted({r["seq"] for r in rows})
    if not seqs:
        raise ValueError("분석된 사건 단위가 없다 — Stage 2 가 선행돼야 한다")
    fp = fingerprint_of(stage2_doc)
    map_doc = empty_map(fp, work_title)
    if prior_map:
        map_doc["prior_episode"] = {"fingerprint": prior_map.get("fingerprint"),
                                    "open_questions": ((prior_map.get("state_by_sequence") or [{}])[-1]
                                                       .get("open_questions") or []),
                                    "register_open": [r for r in prior_map.get("register") or []
                                                      if not r.get("payoff")][:20]}
    audit: dict[str, Any] = {"sequences": len(seqs), "calls": 0, "forward": {}, "backward": {}}
    research_block = _research_block(research_context, character_names)
    seq_meta = {s: (sq.get("content") or "") for s, sq in enumerate(stage2_doc.get("sequences") or [])}

    import app.v3.story_flow as _sf
    _orig = _sf.call_json
    _sf.call_json = lambda g, p: _call(gemini, p)      # noqa: E731 — 주입(테스트·실행 공통)
    try:
        for seq in seqs:
            srows = [r for r in rows if r["seq"] == seq]
            t0, t1 = min(r["t0"] for r in srows), max(r["t1"] for r in srows)
            reg_ids = {r["id"] for r in map_doc["register"]}
            prior_blk = ""
            if map_doc.get("prior_episode") and seq == seqs[0]:
                prior_blk = "\n(이전 회차 열린 질문: " + " / ".join(map_doc["prior_episode"]["open_questions"][:6]) + ")"
            obj = _loop(f"map_fwd{seq}", lambda rej, _s=seq, _t0=t0, _t1=t1, _pb=prior_blk: FORWARD_PROMPT.format(
                work_title=work_title, research_block=research_block + _pb,
                state_block=compress_state(map_doc), seq_no=_s, seq_t0=fmt_t(_t0), seq_t1=fmt_t(_t1),
                seq_content=seq_meta.get(_s, ""), meaning_block=meaning_block_for(rows, _s),
                span_block=span_block_for(spans, _s), reject_block=rej),
                lambda r, _s=seq, _ids=reg_ids: validate_forward(
                    r, seq=_s, rows_by_idx=rows_by_idx, spans=spans, register_ids=_ids),
                gemini, audit["forward"], log)
            audit["calls"] += len(audit["forward"][f"map_fwd{seq}"])
            merge_forward(map_doc, seq, obj, spans)
            log(f"  [v3/map] seq {seq} → 사실 {len(obj['facts'])} · 믿음 {len(obj['beliefs'])} · "
                f"레지스터 {len(obj['register'])} · 열린 질문 {len(obj['open_questions'])} · diegesis {len(obj['diegesis'])}")
        for seq in reversed(seqs):
            obj = _loop(f"map_bwd{seq}", lambda rej, _s=seq: BACKWARD_PROMPT.format(
                work_title=work_title, state_block=compress_state(map_doc), seq_no=_s,
                seq_block=seq_state_block(map_doc, _s), reject_block=rej),
                lambda r, _s=seq: validate_backward(r, seq=_s, rows_by_idx=rows_by_idx, spans=spans, map_doc=map_doc),
                gemini, audit["backward"], log)
            audit["calls"] += len(audit["backward"][f"map_bwd{seq}"])
            apply_backward(map_doc, seq, obj, spans)
            if obj["diegesis_changes"] or obj["register_payoffs"] or obj["beliefs_confirmed"]:
                log(f"  [v3/map] ↩ seq {seq} 역방향 — diegesis {len(obj['diegesis_changes'])} · "
                    f"믿음 확정 {len(obj['beliefs_confirmed'])} · 회수 연결 {len(obj['register_payoffs'])}")
    finally:
        _sf.call_json = _orig
    notes = apply_overrides(map_doc, overrides, spans)
    for n in notes:
        log(f"  [v3/map] 사람 교정 — {n}")
    map_doc["review"] = build_review(map_doc, spans)
    audit["review"] = len(map_doc["review"])
    audit["register"] = len(map_doc["register"])
    audit["register_paid"] = sum(1 for r in map_doc["register"] if r.get("payoff"))
    return map_doc, audit


# ── story_flow 소비(순수) ─────────────────────────────────────────────────────

def register_block(map_doc: dict | None, rows_by_idx: dict[int, dict] | None = None) -> str:
    """걸음 1·2 프롬프트에 덧붙는 레지스터 표 — 지도가 없거나 비면 빈 문자열."""
    if not map_doc or not map_doc.get("register"):
        return ""
    lines = []
    for r in map_doc["register"]:
        s, p = r["setup"], r.get("payoff")
        lines.append(f"- {r['id']} {r['kind']}: 설정 m{s['meaning']:03d}({fmt_t(s['t'])}) 「{s.get('quote', '')[:60]}」"
                     + (f" → 회수 m{p['meaning']:03d}({fmt_t(p['t'])}) 「{p.get('quote', '')[:60]}」"
                        + (f" — {r['what_flipped'][:60]}" if r.get("what_flipped") else "")
                        if p else " → 회수 없음(열린 설정)"))
    return ("\n## 드라마가 이미 만들어 둔 설정↔회수 (레지스터 — 단언↔번복·조건↔결과·대칭)\n"
            "대비형(설정과 회수를 나란히 보여주는) 편의 재료다. 회수가 있는 항목은 설정·회수 두 사건 단위를 함께 고를 수 있다.\n"
            + "\n".join(lines[:30]) + "\n")


def facts_block(map_doc: dict | None) -> str:
    if not map_doc or not map_doc.get("facts_ledger"):
        return ""
    fl = map_doc["facts_ledger"]
    lines = [f"- {f['id']} ({fmt_t(f['t'])}, {f['source']}) {f['text'][:80]}" for f in fl[:40]]
    more = f"\n(+{len(fl) - 40}건)" if len(fl) > 40 else ""
    return "\n## 사실 장부 (시청자에게 확정적으로 제시된 것 — 글자 화면 포함)\n" + "\n".join(lines) + more + "\n"


def apply_diegesis_final(span_index: dict[str, dict], map_doc: dict | None) -> int:
    """지도의 diegesis_final 로 색인의 diegesis 를 덮는다(색인 층에서만 — stage2.json 불변). 바꾼 수."""
    if not map_doc:
        return 0
    n = 0
    for sid, e in (map_doc.get("diegesis_final") or {}).items():
        sp = span_index.get(sid)
        if sp is None:
            continue
        val = e.get("value")
        cur = sp.get("diegesis") or None
        new = None if val == "actual" else val
        if cur != new:
            sp["diegesis"] = new
            n += 1
    return n


def correction_entry(stage: str, reason: str, *, t: float | None, span_id: str | None,
                     frame_path: str | None = None) -> dict:
    """갭 8 역류 관측 스키마 — {stage, reason, evidence:{t, span_id, frame_path}}."""
    return {"stage": stage, "reason": str(reason)[:200],
            "evidence": {"t": (round(float(t), 3) if t is not None else None),
                         "span_id": span_id, "frame_path": frame_path}}


__all__ = ["SCHEMA_MAP", "run_episode_map", "fingerprint_of", "validate_forward", "validate_backward",
           "merge_forward", "apply_backward", "apply_overrides", "build_review", "compress_state",
           "register_block", "facts_block", "apply_diegesis_final", "correction_entry",
           "span_table", "empty_map"]
