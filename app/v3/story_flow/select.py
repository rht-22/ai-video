"""걸음 1~3 — 주제 · 씬 · 대사 (Flash 텍스트 온리, 걸음마다 재료가 좁아진다).

재료 크기(2026-09-03 실측, 「지금 불륜이…」 1회): 종전 단일 프롬프트 79k자(span
1,373행) → 주제·씬 걸음은 meaning 52행, 대사 걸음은 고른 씬의 span 100행 안팎.

걸음 3 의 형식(2026-09-03 사용자 결정 — 반려가 아니라 **형식으로** 막는다):
비트 = 대화의 한 **구간**(첫 조각 ~ 마지막 조각, 사이 조각은 전부 들어간다) + 뺄 조각
(skip). 낱개 id 를 고르게 하면 모델이 텍스트만 보고 "그럴듯하게 붙는 두 줄"을 고른다
(실사고: 「손을 좀 비워가지고」→ 23초·6줄 건너뛴 「서로서로 도와야죠」). 구간 형식에서는
짧은 추임새(SKIP_MAX_*)는 빼서 호흡을 당길 수 있고, 그보다 긴 구멍은 **코드가 비트
경계로 승격**해 걸음 4 의 내레이션 빈칸(다리 필수)이 된다 — 반려 없이.
"""
from __future__ import annotations

from typing import Any

from app.v3.story_flow.common import (
    fmt_t,
    meaning_table,
    nospace_len,
    parse_meaning_id,
    reject_block,
    span_row,
    span_text,
)

PURPOSES = ("배경", "맥락", "과정", "결과", "반응")
ROLES = ("hook", "build", "turn", "climax", "reaction", "ending")
MIN_SCENES, MAX_SCENES = 2, 7
TITLE_MAX_CHARS = 16          # 상단 밴드 2줄 각각의 실측 상한(story.TITLE_MAX_CHARS)
BUDGET_FLOOR_RATIO = 0.85     # 대사 합계가 예산의 이 비율 미만이면 반려(재료를 더 넣어라) —
                              # 2026-09-03: 계획이 얇으면 watch_trim 이 자를 여유가 없고
                              # 잘라내면 목표에서 더 멀어진다(50s → 46s 실사고). 호출부가 켠다.
BUDGET_TOLERANCE = 1.3        # 대사 합계가 예산의 이 배수를 넘으면 반려(그 아래는
                              # watch-trim 이 초안을 보고 덜어낸다 — 산술 트림 금지)
SKIP_MAX_VOICED_SEC = 3.0     # 구간 안에서 빼도 되는 구멍 — 유성 합계 이하면 컷(추임새)
SKIP_MAX_LINES = 2            # · 유성 조각 수 이하
JUMP_GAP_SEC = 5.0            # 비트 사이 원본 간격이 이보다 크면 '점프' — 다리 내레이션 필수

TOPIC_PROMPT = """당신은 리캡 쇼츠 편집자다. 아래는 한 회차의 구조 기록이다(영상은 볼 수 없고 볼 필요도 없다 — 기록이 정본이다).

## 1단계 — 주제 정하기
이 회차에서 쇼츠 한 편(목표 {target_sec:.0f}초)으로 만들 **사건 하나**를 고른다.
기준: 작품을 모르는 사람이 한 번 보고 따라갈 수 있는 사건 · 시작부터 결과(또는 떡밥)까지가 {max_sec:.0f}초 안에 닫힌다 · **대사가 촘촘한 구간**이 유리하다(대사가 거의 없는 구간은 주 재료로 쓰지 마라) · 앞뒤 회차 전개와 인과·아이러니로 이어지는 사건이면 더 좋다.

## 작품
{work_title}{research_block}

## 시퀀스 요약
{sequence_block}
{hint_block}
## 사건 단위 (id | 시각 | 길이 | importance | 분위기 | 인물 | 내용)
{meaning_block}
{reject_block}
## 출력 (JSON 만)
{{"topic": "이 쇼츠가 무엇에 관한 이야기인지 한 문장", "why": "고른 이유 한 문장",
  "core_meanings": ["m012", "m013"], "title_draft": {{"line1": "상황", "line2": "후킹"}}}}"""

SCENES_PROMPT = """당신은 리캡 쇼츠 편집자다. 영상은 볼 수 없다 — 기록이 정본이다.

## 2단계 — 사용 씬 고르기
주제: {topic}
이 사건을 **작품을 모르는 사람이 봐도 다 이해하고 재미있으려면** 어떤 씬을 보여줘야 하나. 각 씬의 쓰임을 정하라 — 배경(왜 이 상황인지) · 맥락(인물 관계·무엇이 걸렸는지) · 과정(사건 진행) · 결과(정점·반전) · 반응(리액션·여운). 필요한 것만 {min_scenes}~{max_scenes}개, **원본 시간 순서**로. 씬 = 아래 사건 단위(id). 길이 감각: 완성본 {target_sec:.0f}초이고 씬 원본 합계는 그 2~3배까지 허용된다(다음 단계에서 대사를 골라 줄인다).
제목 두 줄도 정하라 — line1(위) = 상황·도입, line2(아래) = 후킹. 각 {title_max}자 이내, 이어 읽어 한 호흡. 결말을 다 말하지 마라(읽은 사람이 '그래서?'를 묻게). 아랫줄이 사건의 **결과·반전 자체**(누가 무엇을 했다/당했다)를 말해버리면 볼 이유가 사라진다 — 결과 대신 그 직전의 질문·위기를 남겨라. `title_review.line2_reveals_ending` 에 네 판정을 적고, true 면 고쳐서 내라.

## 작품
{work_title}{research_block}

## 사건 단위 (전체)
{meaning_block}
{reject_block}
## 출력 (JSON 만)
{{"scenes": [{{"meaning": "m012", "purpose": "배경|맥락|과정|결과|반응", "why": "이 씬이 하는 일 한 줄"}}],
  "title": {{"line1": "…", "line2": "…"}},
  "title_review": {{"line2_reveals_ending": false}}}}"""

LINES_PROMPT = """당신은 리캡 쇼츠 편집자다. 영상은 볼 수 없다 — 기록이 정본이다.

## 3단계 — 쓸 대사 고르기
주제: {topic}
제목: {title_line1} / {title_line2}
씬마다 보여줄 **대화 구간**을 비트로 잡아라. 비트 = `first`(첫 조각) ~ `last`(마지막 조각)이고 **사이 조각은 전부 들어간다** — 사람이 편집하듯 대화의 어디서 시작해 어디서 끝낼지를 정하는 것이다. 무슨 일이 왜 일어나는지를 말하는 대사가 뼈대다 — 감탄사·리액션 조각만 모으면 시청자는 무슨 얘기인지 모른다.
규칙:
- 구간 안에서 없어도 맥락이 이어지는 **짧은 추임새**는 `skip` 에 넣어 빼라(호흡을 당긴다). 단 뺀 자리의 대사가 유성 {skip_sec:.0f}초·{skip_lines}줄을 넘으면 코드가 그 자리를 비트 경계로 나누고 다음 단계가 내레이션 다리를 놓는다 — 그러니 **멀리 떨어진 대사를 한 비트에 붙이지 마라**, 비트를 새로 열어라.
- 문장 중간에서 끊지 마라 — ↪ 표시된 조각은 first/last 로 가르지 마라.
- **주고받음이 보여야 한다**: 질문·비난이 들어가면 상대의 답도 구간 안에 있어야 한다.
- 화면만으로 뜻이 오는 무성 장면(리빌·행동)도 구간으로 잡을 수 있다(first·last 가 무성 조각). 결정적 무성 장면은 잘게 썰지 말고 이어지는 구간 하나로.
- 내레이션 자리는 여기서 만들지 않는다 — 다음 단계가 따로 만든다.
- 예산: 구간 길이 합(skip 제외) ≤ **{budget_sec:.0f}초** — 길이 열을 더해 가며 짜라. **예산을 채워라** — 85% 미만이면 반려한다(얇은 편은 다듬을 여유가 없다). 초안을 본 뒤 코드가 늘어지는 곳을 몇 초 잘라내므로 조금 넉넉한 게 맞다.
- 비트 역할: hook(사건 한복판에서 시작 — 인사·자기소개·상황 설명 대사 금지) · build · turn · climax(핵심 대사는 통째로) · reaction · ending(펀치·선언·떡밥 대사 직후 뚝 — 해소·정리 장면 금지).
- 비트는 원본 시간 순서, 구간끼리 겹치지 않게.

## 재료 (씬별 · id | 유성/무성 길이 | importance | 내용)
{material_block}
{reject_block}
## 출력 (JSON 만)
{{"beats": [{{"scene": "m012", "role": "hook", "first": "sp0100", "last": "sp0107", "skip": ["sp0103"],
             "action": "무슨 일이 일어나는지 동사구"}}]}}"""


# ── 검증(순수) ─────────────────────────────────────────────────────────────

def validate_topic(resp: Any, rows: list[dict]) -> tuple[dict | None, list[str]]:
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"]
    problems: list[str] = []
    topic = str(resp.get("topic") or "").strip()
    if not topic:
        problems.append("topic 이 비었다")
    known = {r["idx"] for r in rows}
    core = []
    for v in resp.get("core_meanings") or []:
        k = parse_meaning_id(v)
        if k is not None and k in known and k not in core:
            core.append(k)
    if not core:
        problems.append("core_meanings 에 아는 사건 단위 id 가 없다(m012 형식)")
    td = resp.get("title_draft") if isinstance(resp.get("title_draft"), dict) else {}
    if problems:
        return None, problems
    return {"topic": topic, "why": str(resp.get("why") or "").strip(),
            "core_meanings": sorted(core),
            "title_draft": {"line1": str(td.get("line1") or "").strip(),
                            "line2": str(td.get("line2") or "").strip()}}, []


def validate_scenes(resp: Any, rows: list[dict], *,
                    title_max: int = TITLE_MAX_CHARS) -> tuple[dict | None, list[str], list[str]]:
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []
    problems: list[str] = []
    notes: list[str] = []
    by_idx = {r["idx"]: r for r in rows}
    scenes: list[dict] = []
    seen: set[int] = set()
    for k, s in enumerate(resp.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        idx = parse_meaning_id(s.get("meaning"))
        if idx is None or idx not in by_idx:
            notes.append(f"scenes[{k}] 모르는 사건 단위 {s.get('meaning')!r} 무시")
            continue
        if idx in seen:
            notes.append(f"scenes[{k}] 중복 m{idx:03d} 무시")
            continue
        seen.add(idx)
        purpose = str(s.get("purpose") or "").strip()
        if purpose not in PURPOSES:
            notes.append(f"scenes[{k}] purpose {purpose!r} → 과정")
            purpose = "과정"
        scenes.append({"meaning": idx, "purpose": purpose,
                       "why": str(s.get("why") or "").strip()[:120]})
    if len(scenes) < MIN_SCENES:
        problems.append(f"씬이 {len(scenes)}개 — 최소 {MIN_SCENES}개(배경/맥락 + 과정/결과)")
    if len(scenes) > MAX_SCENES:
        problems.append(f"씬이 {len(scenes)}개 — 최대 {MAX_SCENES}개, 덜 중요한 씬을 빼라")
    order = [s["meaning"] for s in scenes]
    if order != sorted(order):
        notes.append("씬이 시간순이 아니다 — 원본 순서로 정렬")
        scenes.sort(key=lambda s: s["meaning"])
    title = resp.get("title") if isinstance(resp.get("title"), dict) else {}
    l1 = str(title.get("line1") or "").strip()
    l2 = str(title.get("line2") or "").strip()
    if not l1 or not l2:
        problems.append("title 은 {line1, line2} 두 줄 모두 필요")
    for name, line in (("line1", l1), ("line2", l2)):
        if len(line) > title_max:
            problems.append(f"title.{name} 이 {len(line)}자 — {title_max}자 이내로")
    # 스포 판정은 모델의 것(title_review) — 코드는 되돌려 보낼 뿐
    tr = resp.get("title_review") if isinstance(resp.get("title_review"), dict) else {}
    if tr.get("line2_reveals_ending") is True:
        problems.append(f"제목 아랫줄이 결말을 말한다(네 판정) — 결과 대신 직전의 질문·위기로 "
                        f"다시 써라: {l2!r}")
    if problems:
        return None, problems, notes
    return {"scenes": scenes, "title": {"line1": l1, "line2": l2}}, [], notes


def _voiced_sec(ids: list[str], span_index: dict[str, dict]) -> float:
    return sum(span_index[x]["t_out"] - span_index[x]["t_in"]
               for x in ids if span_index[x]["is_audio"])


def split_at_holes(beat: dict, span_index: dict[str, dict],
                   *, max_voiced_sec: float = SKIP_MAX_VOICED_SEC,
                   max_lines: int = SKIP_MAX_LINES) -> list[dict]:
    """구간(range_ids) 에서 skip 을 뺀 뒤, 뺀 구멍이 허용치를 넘는 자리에서 비트를
    나눈다(반려가 아니라 형식 — 긴 구멍은 경계가 되고 걸음 4 의 다리 빈칸이 된다).
    순수. 반환: 비트 목록(각 span_ids·skipped·hole_before)."""
    rng = beat["range_ids"]
    skip = set(beat.get("skip") or [])
    pieces: list[dict] = []
    cur: list[str] = []
    hole: list[str] = []
    cur_skipped: list[str] = []
    for sid in rng:
        if sid in skip:
            hole.append(sid)
            continue
        if cur and hole:
            voiced = [x for x in hole if span_index[x]["is_audio"]]
            if _voiced_sec(hole, span_index) > max_voiced_sec or len(voiced) > max_lines:
                pieces.append({**beat, "span_ids": cur, "skipped": cur_skipped,
                               "hole_before": None if not pieces else pieces[-1].get("_hole_after")})
                pieces[-1]["_hole_after"] = list(hole)
                cur, cur_skipped = [], []
            else:
                cur_skipped.extend(hole)
        hole = []
        cur.append(sid)
    if cur:
        pieces.append({**beat, "span_ids": cur, "skipped": cur_skipped})
    # hole_before 를 정리(직전 조각의 _hole_after)
    out: list[dict] = []
    prev_hole: list[str] | None = None
    for p in pieces:
        p = dict(p)
        p["hole_before"] = prev_hole
        prev_hole = p.pop("_hole_after", None)
        p.pop("range_ids", None)
        p.pop("skip", None)
        out.append(p)
    return out


def compute_jumps(beats: list[dict], span_index: dict[str, dict],
                  *, gap_sec: float = JUMP_GAP_SEC) -> list[dict]:
    """비트 사이 '점프'(다리 내레이션이 필요한 자리) — 원본 간격 > gap_sec 이거나
    구멍 승격으로 나뉜 경계. 각 {before_beat, gap_sec, skipped_ids, skipped_text}."""
    jumps: list[dict] = []
    for i in range(1, len(beats)):
        a, b = beats[i - 1], beats[i]
        if not a.get("span_ids") or not b.get("span_ids"):
            continue
        gap = span_index[b["span_ids"][0]]["t_in"] - span_index[a["span_ids"][-1]]["t_out"]
        hole = b.get("hole_before") or []
        if gap > gap_sec or hole:
            skipped = [x for x in hole if span_index[x]["is_audio"]]
            jumps.append({"before_beat": i, "gap_sec": round(gap, 2),
                          "skipped_ids": list(hole),
                          "skipped_text": [span_text(span_index[x]) for x in skipped][:6]})
    return jumps


def validate_beats(resp: Any, span_index: dict[str, dict], allowed: dict[str, int],
                   *, budget_sec: float, floor_ratio: float | None = None,
                   material_sec: float | None = None
                   ) -> tuple[list[dict] | None, list[str], list[str]]:
    """allowed: span id → 씬(meaning idx). 반환 비트: {scene, role, span_ids, skipped,
    hole_before, action}. 구간은 grid 순(pos)으로 펼치고 긴 구멍은 나눈다."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []
    problems: list[str] = []
    notes: list[str] = []
    by_scene: dict[int, list[str]] = {}
    for sid, sc in allowed.items():
        if sid in span_index:
            by_scene.setdefault(sc, []).append(sid)
    for sc in by_scene:
        by_scene[sc].sort(key=lambda x: span_index[x]["pos"])
    used: set[str] = set()
    raw_beats: list[dict] = []
    for k, b in enumerate(resp.get("beats") or []):
        if not isinstance(b, dict):
            problems.append(f"beats[{k}] 가 객체가 아님")
            continue
        first, last = str(b.get("first") or ""), str(b.get("last") or "")
        if first not in allowed or last not in allowed:
            problems.append(f"beats[{k}] first/last 가 재료 밖이다: {first!r}~{last!r}")
            continue
        scene = allowed[first]
        if allowed[last] != scene:
            problems.append(f"beats[{k}] first 와 last 가 다른 씬이다 — 씬마다 비트를 나눠라")
            continue
        p0, p1 = span_index[first]["pos"], span_index[last]["pos"]
        if p0 > p1:
            notes.append(f"beats[{k}] first/last 순서 뒤집힘 — 바로잡음")
            p0, p1 = p1, p0
        rng = [x for x in by_scene[scene] if p0 <= span_index[x]["pos"] <= p1]
        reused = [x for x in rng if x in used]
        if reused:
            problems.append(f"beats[{k}] 구간이 다른 비트와 겹친다 {reused[:3]} — 구간끼리 겹치지 않게")
            continue
        skip_in = [str(x) for x in (b.get("skip") or []) if isinstance(x, str)]
        skip = [x for x in skip_in if x in rng]
        if len(skip) != len(skip_in):
            notes.append(f"beats[{k}] 구간 밖 skip {len(skip_in) - len(skip)}개 무시")
        if set(skip) >= set(rng):
            problems.append(f"beats[{k}] 구간 전부를 skip 했다")
            continue
        # 문장 반토막 — 구간 경계·skip 이 ↪ 짝을 가르면 안 된다
        keep = [x for x in rng if x not in skip]
        for x in keep:
            sp = span_index[x]
            nxt, prv = sp.get("continues_to"), sp.get("continues_from")
            if nxt and nxt in span_index and nxt not in keep:
                problems.append(f"beats[{k}] 문장 반토막: {x} 는 {nxt} 로 이어진다 — 짝을 함께 넣거나 둘 다 빼라")
            if prv and prv in span_index and prv not in keep and prv not in used:
                problems.append(f"beats[{k}] 문장 반토막: {x} 는 {prv} 에서 이어진다 — 짝을 함께 넣거나 둘 다 빼라")
        used.update(rng)
        role = str(b.get("role") or "").strip()
        if role not in ROLES:
            notes.append(f"beats[{k}] role {role!r} → build")
            role = "build"
        raw_beats.append({"scene": scene, "role": role, "range_ids": rng, "skip": skip,
                          "action": str(b.get("action") or "").strip()[:80]})
    if not raw_beats:
        return None, problems + ["beats 가 비었다"], notes
    raw_beats.sort(key=lambda b: span_index[b["range_ids"][0]]["pos"])
    beats: list[dict] = []
    for rb in raw_beats:
        pieces = split_at_holes(rb, span_index)
        if len(pieces) > 1:
            notes.append(f"{rb['role']} 비트 안 긴 구멍 {len(pieces) - 1}곳 → 비트 {len(pieces)}개로 나눔"
                         "(다리 내레이션 자리)")
        beats.extend(pieces)
    total = sum(span_index[x]["t_out"] - span_index[x]["t_in"]
                for b in beats for x in b["span_ids"])
    if total > budget_sec * BUDGET_TOLERANCE:
        per = " · ".join(
            f"{b['role']} {sum(span_index[x]['t_out'] - span_index[x]['t_in'] for x in b['span_ids']):.0f}s"
            for b in beats)
        problems.append(f"구간 합계 {total:.0f}초 — 예산 {budget_sec:.0f}초를 크게 "
                        f"넘는다(비트별: {per}). 구간을 좁히거나 비트를 빼서 다시 내라")
    elif floor_ratio and (material_sec is None or material_sec >= budget_sec) \
            and total < budget_sec * floor_ratio:
        # 재료(고른 씬 합계)가 예산보다 적으면 미달을 따지지 않는다 — 없는 재료를
        # 채우라 할 순 없다. 실전은 재료가 목표의 2~3배라 예산 기준이 그대로 선다.
        problems.append(f"구간 합계 {total:.0f}초 — 예산 {budget_sec:.0f}초의 {floor_ratio:.0%} 미만이다. "
                        "재료가 얇으면 완성본이 짧고 다듬을 여유도 없다 — 같은 사건 안의 "
                        "대사 구간·리액션을 더 넣어 예산을 채워라")
    if not any(span_index[x]["is_audio"] for b in beats for x in b["span_ids"]):
        problems.append("대사가 하나도 없다 — 대사 인용이 뼈대다")
    if problems:
        return None, problems, notes
    return beats, [], notes


# ── 재료 표 ────────────────────────────────────────────────────────────────

def lines_material(scenes: list[dict], rows: list[dict],
                   span_index: dict[str, dict]) -> tuple[str, dict[str, int]]:
    """고른 씬의 span 만 — (재료 표, 허용 id → 씬 idx)."""
    by_idx = {r["idx"]: r for r in rows}
    out: list[str] = []
    allowed: dict[str, int] = {}
    for s in scenes:
        r = by_idx[s["meaning"]]
        out.append(f"\n### 씬 m{r['idx']:03d} [{s['purpose']}] {fmt_t(r['t0'])}~{fmt_t(r['t1'])} "
                   f"— {r['content']}")
        for sid in r["span_ids"]:
            sp = span_index.get(sid)
            if sp is None or sp.get("unanalyzed"):
                continue
            allowed[sid] = r["idx"]
            out.append(span_row(sid, sp))
    return "\n".join(out), allowed


def title_len_ok(title: dict, title_max: int = TITLE_MAX_CHARS) -> bool:
    return all(0 < len(str(title.get(k) or "")) <= title_max for k in ("line1", "line2"))


__all__ = ["TOPIC_PROMPT", "SCENES_PROMPT", "LINES_PROMPT", "validate_topic",
           "validate_scenes", "validate_beats", "split_at_holes", "compute_jumps",
           "lines_material", "meaning_table", "nospace_len", "reject_block", "PURPOSES",
           "ROLES", "TITLE_MAX_CHARS", "BUDGET_TOLERANCE", "SKIP_MAX_VOICED_SEC",
           "SKIP_MAX_LINES", "JUMP_GAP_SEC"]
