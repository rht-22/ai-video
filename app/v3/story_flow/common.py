"""사람 편집 흐름 체인 — 공용 재료(순수).

체인(`app/v3/story_flow`)은 Stage 2 기록을 **한 번에 편성하지 않고** 사람이 하는
순서대로 다섯 걸음으로 나눈다(2026-09-03 사용자 결정):
  1 주제 → 2 씬 → 3 대사 → 4 내레이션(+즉시 합성 = 길이 확정) → 5 덮개(그 자리를
  다시 본다). 걸음마다 재료가 좁아지고(전체 meaning → 고른 씬의 span → 고른 대사),
  마지막 걸음만 영상을 국소적으로 다시 본다.
"""
from __future__ import annotations

import re
from typing import Any

from app.v3 import schemas

MAX_REASKS = 2               # 걸음마다 반려·재질의 상한(seq_analyze 와 같은 값)


def call_json(gemini, prompt: str) -> dict:
    """Flash 텍스트 온리 JSON 호출 — story 의 것을 그대로 쓴다(모델 정책 동일)."""
    from app.v3.story import _call_story_model
    return _call_story_model(gemini, prompt)


def nospace_len(text: Any) -> int:
    return len("".join(str(text or "").split()))


def fmt_t(t: float) -> str:
    """원본 절대초 → mm:ss.s (사람이 읽는 표기 — 프롬프트 전용)."""
    t = max(0.0, float(t))
    m, s = divmod(t, 60.0)
    return f"{int(m):02d}:{s:04.1f}"


def meaning_rows(stage2_doc: dict) -> list[dict]:
    """meaning 평면 목록. idx 는 `story.build_span_index` 의 meaning_idx 와 **같은
    순서**다(전역 번호 — 두 곳이 갈리면 씬 선택이 엉뚱한 span 을 가리킨다)."""
    rows: list[dict] = []
    mi = -1
    for si, sq in enumerate(stage2_doc.get("sequences") or []):
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                mi += 1
                t0 = schemas.parse_ts(m["time"]["start"])
                t1 = schemas.parse_ts(m["time"]["end"])
                rows.append({
                    "idx": mi, "seq": si, "t0": t0, "t1": t1,
                    "content": str(m.get("content") or "").strip(),
                    "characters": list(m.get("characters") or []),
                    "importance": int(m.get("importance") or 3),
                    "mood": str(m.get("mood") or ""),
                    "span_ids": [s.get("span_id") for s in m.get("spans") or []
                                 if isinstance(s.get("span_id"), str)],
                })
    return rows


def sequence_rows(stage2_doc: dict) -> list[dict]:
    out = []
    for sq in stage2_doc.get("sequences") or []:
        chs = sq.get("chunks") or []
        if not chs:
            continue
        out.append({"number": sq.get("number"),
                    "t0": schemas.parse_ts(chs[0]["time"]["start"]),
                    "t1": schemas.parse_ts(chs[-1]["time"]["end"]),
                    "content": str(sq.get("content") or "").strip()})
    return out


def meaning_table(rows: list[dict], only: set[int] | None = None) -> str:
    lines = []
    for r in rows:
        if only is not None and r["idx"] not in only:
            continue
        lines.append(
            f"m{r['idx']:03d} | {fmt_t(r['t0'])}~{fmt_t(r['t1'])} | "
            f"{r['t1'] - r['t0']:.0f}s | imp {r['importance']} | {r['mood']} | "
            f"{'/'.join(r['characters'])} | {r['content']}")
    return "\n".join(lines)


def span_row(sid: str, sp: dict) -> str:
    """재료 표 한 행 — story.build_material_block 과 같은 신뢰 표기."""
    dur = sp["t_out"] - sp["t_in"]
    if sp["is_audio"]:
        speech = " / ".join(f"{a.get('speaker')}: {a.get('line')}"
                            for a in sp.get("audio_script") or [])
        src, conf = sp.get("text_source"), sp.get("conf")
        tag = ""
        if src == "none" or not speech.strip():
            tag = " [대사없음]"
        elif src == "heard":
            tag = " [청취]"
        elif conf is not None and conf < 0.5:
            tag = f" [저확신 {conf:.2f}]"
        if sp.get("continues_to"):
            tag += " ↪다음과 한 문장"
        elif sp.get("pause_cont_from"):
            tag += " ↪앞 조각에서 이어졌을 수 있음"
        return f"{sid} | 유성 {dur:.1f}s | imp {sp['importance']}{tag} | {speech}"
    return f"{sid} | 무성 {dur:.1f}s | imp {sp['importance']} | {sp.get('scene_script', '')}"


def span_text(sp: dict) -> str:
    return " / ".join(f"{a.get('speaker')}: {a.get('line')}"
                      for a in sp.get("audio_script") or []).strip()


def parse_meaning_id(v: Any) -> int | None:
    """'m012' · 12 · '12' → 12. 그 외 None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    m = re.fullmatch(r"\s*m?(\d+)\s*", str(v or ""))
    return int(m.group(1)) if m else None


def reject_block(problems: list[str]) -> str:
    if not problems:
        return ""
    return ("\n## 직전 응답의 반려 사유 — 전부 고쳐서 다시 내라\n"
            + "\n".join(f"- {p}" for p in problems[:20]) + "\n")
