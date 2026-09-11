"""권리사 활용 불가 구간(banned) — 시각으로 받아 장면 단위로 막는다 (2026-09-10, 사용자 결정).

배경(쿠팡플레이 「지금 불륜이 문제가 아닙니다」 가이드 PDF): 회차별로 "2화 22:06~22:50 (수정&재홍 호텔 씬
일체)" 처럼 **시각 + 장면 설명**이 온다. 시각만 믿으면 안 된다는 증거 — 가이드가 2화 금지 구간을
44:00~엔딩으로 적고 그 안의 대사로 「엄마 지금 다시 가면 그 아저씨 살 수 있을지도 몰라」를 들었는데,
우리 EP02 파일의 전사에서 그 대사는 43:33 에 있다(27초 앞). 사람이 대충 적은 시각이라 우리 파일의
시간축과 어긋나고, 타임스탬프 경계 그대로 자르면 금지 대사가 통과한다.

그래서 셋을 같이 쓴다:
  · **입력은 시각** — 권리사가 준 정본. 템플릿 `banned` 키에 회차별 `{t0, t1, what, keywords?}`.
  · **차단 단위는 장면(Stage 2 사건 단위)** — 시각 구간과 조금이라도 겹치는 사건 단위 전체를 금지로
    확장한다. 사건 단위는 연속 구간이라 수십 초의 어긋남을 흡수하고, 경계에 걸친 span 반쪽이 새지 않는다.
    금지 단위의 span 은 대사·덮개·뮤트·소리 어느 용도로도 못 쓴다(4화 USB 는 소리까지 금지).
  · **설명은 증인** — 구간 앞뒤 `BANNED_SEARCH_PAD_SEC` 안의 이웃 사건 단위 중 설명 키워드(호텔·수정·
    재홍…)가 Stage 2 기록(사건 문장·청취·화면 묘사·인물)에 나오는 단위는 함께 막는다(위 27초 케이스).
    직접 겹침 ∪ 이웃 어디에도 키워드가 없으면 **시간축 불일치 의심** — 크게 경고하고 이웃 후보까지 전부
    막는다(과잉 차단이 누락보다 싸다: 위반은 권리 사고, 누락은 소재 하나).
  · **벨트는 조립 뒤** — edit_plan 의 클립(덮개 포함)을 금지 구간·금지 span 과 대조해 하나라도 걸리면
    크게 실패시킨다. 프롬프트 지시는 1차 필터일 뿐이다.

`--exclude-range`(이미 만든 편 제외 · 사건 단위 50% 겹침 반려)와는 다른 규율이다 — 그쪽은 소재 중복
회피라 부분 겹침을 통과시켜도 되지만, 여기는 부분 겹침이 곧 위반이다.

이 모듈은 순수다(LLM·파일 없음). 미지정이면 파이프라인 어디에도 흔적이 없다(회귀 0).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

BANNED_SEARCH_PAD_SEC = 60.0   # 설명 증인을 찾는 이웃 창(구간 앞뒤) — EP02 실측 어긋남 27s 의 2배
BANNED_EPS_SEC = 0.02          # 경계가 맞닿는 부동소수 접촉은 겹침이 아니다
BANNED_MIN_KEYWORD_LEN = 2

_ITEM_KEYS = frozenset({"t0", "t1", "what", "keywords", "_doc"})
# 설명 문장에서 키워드를 뽑을 때 버리는 일반어(가이드 문구의 형식어) — 증거가 못 된다
_GENERIC_WORDS = frozenset({
    "씬", "일체", "활용", "불가", "구간", "해당", "모든", "사용", "대사", "장면", "사용x", "엔딩",
    "및", "중", "만", "가능", "직접적", "부탁", "드립니다", "노출", "않도록", "일괄", "까지", "속",
    "보는", "하는", "있는", "그", "이", "저", "혹은", "또는", "등",
})
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")


class BannedError(ValueError):
    """금지 구간 입력 형식 오류 — 조용히 무시하면 '막았다고 믿은 채' 위반 편이 나간다."""


# ── 입력 ────────────────────────────────────────────────────────────────────

def parse_ts_flex(v: Any) -> float:
    """'mm:ss' · 'hh:mm:ss' · 초(숫자/문자열) → 초. 가이드는 mm:ss 로 적는다."""
    if isinstance(v, (int, float)):
        out = float(v)
    else:
        s = str(v).strip()
        if not s:
            raise BannedError("빈 시각")
        parts = s.split(":")
        try:
            if len(parts) == 1:
                out = float(parts[0])
            elif len(parts) == 2:
                out = int(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:
                out = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            else:
                raise ValueError
        except ValueError:
            raise BannedError(f"시각 형식은 mm:ss · hh:mm:ss · 초: {v!r}")
    if out < 0:
        raise BannedError(f"음수 시각: {v!r}")
    return out


def _parse_item(it: Any, where: str) -> dict:
    if not isinstance(it, dict):
        raise BannedError(f"{where}: 항목은 객체여야 한다 ({it!r})")
    bad = sorted(set(it) - _ITEM_KEYS)
    if bad:
        raise BannedError(f"{where}: 모르는 키 {bad} (허용: t0·t1·what·keywords)")
    if "t0" not in it or "t1" not in it:
        raise BannedError(f"{where}: t0·t1 필수")
    t0 = parse_ts_flex(it["t0"])
    t1_raw = it["t1"]
    t1 = None if (isinstance(t1_raw, str) and t1_raw.strip().lower() in ("end", "엔딩")) \
        else parse_ts_flex(t1_raw)
    if t1 is not None and not t1 > t0:
        raise BannedError(f"{where}: t0 < t1 이어야 한다 ({it['t0']!r}~{it['t1']!r})")
    what = str(it.get("what") or "").strip()
    kws = it.get("keywords")
    if kws is not None and not (isinstance(kws, list) and all(isinstance(k, str) for k in kws)):
        raise BannedError(f"{where}: keywords 는 문자열 배열")
    keywords = [k.strip() for k in (kws or []) if k.strip()]
    if not what and not keywords:
        raise BannedError(f"{where}: what(설명) 또는 keywords 중 하나는 있어야 증인 대조가 된다")
    return {"t0": t0, "t1": t1, "what": what, "keywords": keywords}


def parse_banned_doc(doc: Any) -> dict[str, list[dict]]:
    """템플릿 `banned` 키 → {회차(str): [항목]}. `_doc` 키는 문서용. 형식 오류는 즉시 실패."""
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise BannedError("banned 는 {회차: [항목…]} 객체여야 한다")
    out: dict[str, list[dict]] = {}
    for ep, items in doc.items():
        if str(ep).startswith("_"):
            continue
        try:
            key = str(int(str(ep).strip()))
        except ValueError:
            raise BannedError(f"banned 의 회차 키는 정수: {ep!r}")
        if not isinstance(items, list):
            raise BannedError(f"banned[{key}] 는 배열")
        out[key] = [_parse_item(it, f"banned[{key}][{i}]") for i, it in enumerate(items)]
    return out


def merge_banned(*docs: dict[str, list[dict]] | None) -> dict[str, list[dict]]:
    """회차별 목록 합집합(템플릿 ⊕ --banned-json)."""
    out: dict[str, list[dict]] = {}
    for d in docs:
        for ep, items in (d or {}).items():
            out.setdefault(ep, []).extend(items)
    return out


def banned_for_episode(doc: dict[str, list[dict]], episode: int | None) -> list[dict]:
    """그 회차의 항목. 금지 목록이 있는데 회차를 모르면 즉시 실패 — 어느 회차 규칙을 적용할지
    알 수 없는 채 '막았다'고 믿으면 안 된다."""
    if not doc:
        return []
    if episode is None:
        raise BannedError("금지 구간(banned)이 있는데 --episode 가 없다 — 회차별 규칙이라 필수")
    return list(doc.get(str(int(episode))) or [])


def keywords_of(item: dict) -> list[str]:
    """명시 keywords 가 있으면 그것, 없으면 설명(what)의 토큰(2자 이상 · 일반어 제외)."""
    if item.get("keywords"):
        return list(item["keywords"])
    toks: list[str] = []
    for t in _TOKEN_RE.findall(str(item.get("what") or "")):
        if len(t) >= BANNED_MIN_KEYWORD_LEN and t not in _GENERIC_WORDS and t not in toks:
            toks.append(t)
    return toks


# ── 장면 확장 + 증인 ─────────────────────────────────────────────────────────

def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def unit_text(row: dict, span_index: dict[str, dict] | None) -> str:
    """사건 단위의 증인 텍스트 — 사건 문장 + 인물 + 그 span 들의 청취·전사·화면 묘사·화면 글자."""
    parts = [str(row.get("content") or ""), " ".join(str(c) for c in row.get("characters") or [])]
    for sid in row.get("span_ids") or []:
        sp = (span_index or {}).get(sid)
        if not sp:
            continue
        parts.append(str(sp.get("heard_text") or ""))
        parts.append(str(sp.get("text") or ""))
        parts.append(str(sp.get("scene_script") or ""))
        parts.append(str(sp.get("screen_text") or ""))
        parts.append(" ".join(str(c) for c in sp.get("characters") or []))
        parts.append(" ".join(str(a.get("text") or a) if isinstance(a, dict) else str(a)
                              for a in sp.get("audio_script") or []))
    return " ".join(p for p in parts if p)


def _hits(text: str, keywords: Iterable[str]) -> list[str]:
    compact = re.sub(r"\s+", "", text)
    return [k for k in keywords if k and (k in text or re.sub(r"\s+", "", k) in compact)]


def resolve_banned(items: list[dict], rows: list[dict], span_candidates: list[dict],
                   runtime: float | None, *, span_index: dict[str, dict] | None = None,
                   pad: float = BANNED_SEARCH_PAD_SEC) -> dict:
    """시각 항목 → {units, span_ids, intervals, warnings, items}. 순수.

    rows: `story_flow.common.meaning_rows` (idx/t0/t1/content/characters/span_ids).
    span_candidates: grid 의 span 목록(id/t_in/t_out) — 분석 커버리지 밖 span 도 시각으로 막는다.
    runtime: 소스 러닝타임(t1="end" 해석). 모르면 그 항목은 마지막 사건 단위 끝까지."""
    units: set[int] = set()
    span_ids: set[str] = set()
    intervals: list[list[float]] = []
    warnings: list[str] = []
    details: list[dict] = []
    last_end = max((float(r["t1"]) for r in rows), default=0.0)
    for k, it in enumerate(items or []):
        t0 = float(it["t0"])
        t1 = float(it["t1"]) if it.get("t1") is not None else float(runtime or last_end or t0 + 1.0)
        kws = keywords_of(it)
        direct: set[int] = set()
        neigh: set[int] = set()
        witness: set[int] = set()
        for r in rows:
            r0, r1 = float(r["t0"]), float(r["t1"])
            if _overlap(r0, r1, t0, t1) > BANNED_EPS_SEC:
                direct.add(int(r["idx"]))
            elif _overlap(r0, r1, t0 - pad, t1 + pad) > BANNED_EPS_SEC:
                neigh.add(int(r["idx"]))
        by_idx = {int(r["idx"]): r for r in rows}
        matched_any = False
        for idx in direct:
            if _hits(unit_text(by_idx[idx], span_index), kws):
                matched_any = True
        for idx in neigh:
            if _hits(unit_text(by_idx[idx], span_index), kws):
                witness.add(idx)
                matched_any = True
        widened: set[int] = set()
        if kws and not matched_any:
            # 시간축 불일치 의심 — 설명이 구간 안팎 어디에도 없다. 이웃까지 전부 막는다(과잉 차단).
            widened = set(neigh)
            warnings.append(
                f"banned[{k}] [{_fmt(t0)}~{_fmt(t1)}] '{it.get('what') or '/'.join(kws)}' — 키워드 "
                f"{kws} 가 구간 안팎 사건 단위 어디에도 없다(시간축 불일치 의심) → 이웃 {len(widened)}개까지 차단")
        chosen = direct | witness | widened
        units |= chosen
        seg_lo, seg_hi = t0, t1
        for idx in chosen:
            r = by_idx[idx]
            span_ids.update(str(s) for s in r.get("span_ids") or [])
            seg_lo = min(seg_lo, float(r["t0"]))
            seg_hi = max(seg_hi, float(r["t1"]))
        intervals.append([round(seg_lo, 3), round(seg_hi, 3)])
        # 분석 커버리지 밖 span 도 시각으로 막는다(사건 단위가 없는 구간)
        for sp in span_candidates or []:
            if _overlap(float(sp["t_in"]), float(sp["t_out"]), seg_lo, seg_hi) > BANNED_EPS_SEC:
                span_ids.add(str(sp["id"]))
        details.append({"t0": t0, "t1": t1, "what": it.get("what") or "", "keywords": kws,
                        "direct": sorted(direct), "witness": sorted(witness),
                        "widened": sorted(widened), "interval": [round(seg_lo, 3), round(seg_hi, 3)]})
    return {"units": sorted(units), "span_ids": sorted(span_ids),
            "intervals": _merge_intervals(intervals), "warnings": warnings, "items": details}


def _merge_intervals(iv: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for a, z in sorted(iv):
        if out and a <= out[-1][1] + BANNED_EPS_SEC:
            out[-1][1] = max(out[-1][1], z)
        else:
            out.append([a, z])
    return out


def _fmt(t: float) -> str:
    m = int(t // 60)
    return f"{m:02d}:{t - m * 60:05.2f}"


# ── 프롬프트 블록 ──────────────────────────────────────────────────────────────

def banned_block(resolved: dict | None) -> str:
    """걸음 1·2 프롬프트 뒤에 덧붙는 블록. 비면 빈 문자열(프롬프트 종전과 동일)."""
    if not resolved or not resolved.get("items"):
        return ""
    lines = ["\n## 권리사 활용 불가 구간 — 어느 용도로도 쓰지 마라(대사·배경·반응·덮개 화면·소리 전부)"]
    for it in resolved["items"]:
        a, z = it["interval"]
        lines.append(f"- [{_fmt(a)}~{_fmt(z)}] {it.get('what') or ', '.join(it.get('keywords') or [])}")
    lines.append("이 구간의 사건은 재료 표에서 이미 뺐다. 이 구간의 내용을 **언급하는** 제목·내레이션도 금지다.")
    return "\n".join(lines) + "\n"


# ── 벨트 ───────────────────────────────────────────────────────────────────────

def violations(timeline: list[dict], resolved: dict | None) -> list[dict]:
    """edit_plan 클립(덮개 포함)이 금지 구간·금지 span 에 닿는가. 순수."""
    if not resolved:
        return []
    ivs = [(float(a), float(z)) for a, z in resolved.get("intervals") or []]
    banned_ids = set(resolved.get("span_ids") or [])
    out: list[dict] = []
    for i, c in enumerate(timeline or []):
        try:
            c0, c1 = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        hit_iv = [[a, z] for a, z in ivs if _overlap(c0, c1, a, z) > BANNED_EPS_SEC]
        hit_sp = sorted(set(str(s) for s in c.get("span_ids") or []) & banned_ids)
        if hit_iv or hit_sp:
            out.append({"clip": i, "role": c.get("role"), "cover": c.get("cover"),
                        "t0": round(c0, 3), "t1": round(c1, 3),
                        "intervals": hit_iv, "span_ids": hit_sp})
    return out


def enforce(timeline: list[dict], resolved: dict | None, *, where: str, log=print) -> None:
    """위반이 하나라도 있으면 전부 적어 크게 실패 — 조용한 위반 발행이 최악이다."""
    bad = violations(timeline, resolved)
    if not bad:
        return
    for b in bad:
        log(f"  [v3/banned] ✖ {where} — 클립 {b['clip']}({b['role']}{'/' + str(b['cover']) if b.get('cover') else ''}) "
            f"[{_fmt(b['t0'])}~{_fmt(b['t1'])}] 금지 구간 {b['intervals']} span {b['span_ids']}")
    raise ValueError(f"권리사 활용 불가 구간 위반 {len(bad)}건({where}) — "
                     + json.dumps(bad, ensure_ascii=False)[:800])


__all__ = ["BANNED_SEARCH_PAD_SEC", "BANNED_EPS_SEC", "BannedError", "parse_ts_flex",
           "parse_banned_doc", "merge_banned", "banned_for_episode", "keywords_of",
           "unit_text", "resolve_banned", "banned_block", "violations", "enforce"]
