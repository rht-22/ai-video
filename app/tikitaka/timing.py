"""타임코드 확정 — 순수 함수. LLM 이 고른 소스 ID 를 전사 단어·장면 컷 경계에 묶는다.

- S(대사): 첫 단어 시작 −0.05s ~ 마지막 단어 끝 +0.15s (립싱크 정본은 단어 타임스탬프)
- 컷 안전 마진: 샷 경계 ±0.1s 안쪽만 (제0-2원칙)
- N(내레이션): TTS 실측 길이가 마스터 — 정배속 컷 1.0~2.0s 를 쌓아 그 길이를 꽉 채운다(제1·2원칙)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.tikitaka.common import (SAFE_MARGIN_SEC, NARRATION_CHARS_PER_SEC, STACK_CUT_MIN_SEC, STACK_CUT_MAX_SEC,
                                 DIALOGUE_LEAD_SEC, DIALOGUE_TAIL_SEC, ms3)
from app.tikitaka.scenecut import scene_bounds

_WS = re.compile(r"\s+")


def narration_plan_sec(text: str) -> float:
    """제1원칙: 공백 제외 글자수 / 4 (계획값)."""
    n = len(_WS.sub("", text or ""))
    return ms3(n / NARRATION_CHARS_PER_SEC)


def bind_dialogue(lines_by_id: dict[str, dict], words: list[dict], line_ids: list[str]) -> dict:
    """줄 ID 들 → {start, end, text, speaker}. 없는 ID 는 KeyError(무결성 — 조용히 넘기지 않는다)."""
    ls = [lines_by_id[i] for i in line_ids]
    if not ls:
        raise ValueError("line_ids 가 비었다")
    wi = [i for l in ls for i in l["word_i"]]
    w0, w1 = words[min(wi)], words[max(wi)]
    return {"start": ms3(max(0.0, w0["start"] - DIALOGUE_LEAD_SEC)), "end": ms3(w1["end"] + DIALOGUE_TAIL_SEC),
            "text": " ".join(l["text"] for l in ls), "speaker": ls[0].get("speaker")}


def safe_shot(cuts: list[float], t: float, duration: float) -> tuple[float, float]:
    """t 를 품는 샷의 안전 구간(±0.1s 안쪽). 샷이 0.3s 미만이면 마진 없이 원 경계."""
    lo, hi = scene_bounds(cuts, t, duration)
    if hi - lo < 3 * SAFE_MARGIN_SEC:
        return ms3(lo), ms3(hi)
    return ms3(lo + SAFE_MARGIN_SEC), ms3(hi - SAFE_MARGIN_SEC)


def nudge_in(cuts: list[float], t: float) -> float:
    """시작점이 샷 경계 ±0.1s 안이면 경계 뒤 0.1s 로 민다(제0-2원칙 — 경계에 걸치지만 않으면 샷을 넘어도 된다)."""
    for c in cuts:
        if abs(t - c) < SAFE_MARGIN_SEC:
            return ms3(c + SAFE_MARGIN_SEC)
    return ms3(t)


def nudge_out(cuts: list[float], t: float) -> float:
    """끝점이 샷 경계 ±0.1s 안이면 경계 앞 0.1s 로 당긴다."""
    for c in cuts:
        if abs(t - c) < SAFE_MARGIN_SEC:
            return ms3(c - SAFE_MARGIN_SEC)
    return ms3(t)


def action_window(cuts: list[float], start: float, end: float, duration: float, *, min_sec: float = 0.8) -> tuple[float, float]:
    """A 행(액션) 구간: 제안 구간을 **그대로** 쓰되 양 끝만 경계에서 떼고, 짧으면 뒤로 늘린다(액션은 샷을 가로질러도 된다 —
    2026-09-10 실측: 악보 던지기 2592.1~2593.4 사이에 컷 2개, 한 샷에 가두면 0.5s 가 된다)."""
    a = nudge_in(cuts, max(0.0, start))
    b = nudge_out(cuts, min(duration, end))
    if b - a < min_sec:
        b = nudge_out(cuts, min(duration, a + min_sec))
        if b - a < min_sec:
            b = ms3(min(duration, a + min_sec))
    return ms3(a), ms3(b)


def snap_moment(cuts: list[float], start: float, end: float, duration: float, *, min_sec: float = 0.5) -> tuple[float, float]:
    """순간 [start,end] → 중점이 속한 샷의 안전 구간 안으로 클램프. 너무 짧아지면 샷 안에서 늘린다."""
    mid = (start + end) / 2
    lo, hi = safe_shot(cuts, mid, duration)
    a, b = max(lo, start), min(hi, end)
    if b - a < min_sec:
        b = min(hi, a + min_sec)
        a = max(lo, b - min_sec)
    return ms3(a), ms3(b)


@dataclass
class CutSource:
    id: str
    avail_in: float      # 이 소스에서 쓸 수 있는 시작(순간 시작과 샷 안전 시작 중 늦은 쪽)
    avail_out: float     # 샷 안전 끝(순간 끝을 넘어 같은 샷 안에서 늘릴 수 있다)
    desc: str = ""
    prop_out: float | None = None   # 제안된 끝(순간/agentic 이 적은 끝) — A 행처럼 '동작 구간 그대로'가 필요할 때 쓴다

    @property
    def avail(self) -> float:
        return max(0.0, self.avail_out - self.avail_in)


def stack_cuts(sources: list[CutSource], target: float, *, min_cut: float = STACK_CUT_MIN_SEC,
               max_cut: float = STACK_CUT_MAX_SEC) -> tuple[list[dict], float]:
    """정배속 컷 분할: 소스 순서대로 컷을 쌓아 target 초를 채운다 → (cuts, shortfall).

    컷 하나 1.0~2.0s 가 원칙이나, 채울 소스가 모자라면 같은 샷 안에서 2.0s 를 넘겨 늘린다(슬로우 모션 금지가
    더 상위 규칙). 그래도 모자라면 shortfall > 0 으로 돌려주고 호출자가 인접 샷을 더한다.
    """
    target = ms3(target)
    usable = [s for s in sources if s.avail >= 0.8]     # 0.8s 미만은 플래시 컷 — 소스로 안 친다(실측: 0.6s 컷이 섞였다)
    if target <= 0 or not usable:
        return [], target
    n = max(1, min(len(usable), math.ceil(target / max_cut)))
    if target / n < min_cut:
        n = max(1, math.floor(target / min_cut))
    base = target / n
    cuts: list[dict] = []
    remaining = target
    used = 0
    for s in usable:
        if len(cuts) >= n or remaining <= 0.001:
            break
        used += 1
        dur = min(base, s.avail, remaining)
        if dur < 0.3:
            continue
        cuts.append({"src": s.id, "in": ms3(s.avail_in), "out": ms3(s.avail_in + dur), "dur": ms3(dur), "desc": s.desc,
                     "cap": ms3(s.avail_out)})
        remaining = ms3(remaining - dur)

    def _extend(limit_fn) -> None:
        nonlocal remaining
        for c in reversed(cuts):
            if remaining <= 0.001:
                return
            room = min(limit_fn(c), c["cap"]) - c["out"]
            add = min(max(0.0, room), remaining)
            if add > 0:
                c["out"] = ms3(c["out"] + add)
                c["dur"] = ms3(c["dur"] + add)
                remaining = ms3(remaining - add)

    # ① 이미 쓴 컷을 2.0s 상한까지 늘린다 → ② 남은 소스로 0.8s 이상 컷을 더한다 → ③ 같은 샷 안에서 상한을 넘겨 늘린다
    _extend(lambda c: c["in"] + max_cut)
    for s in usable[used:]:
        if remaining < 0.8:
            break
        dur = min(remaining, s.avail)
        if dur < 0.8:
            continue
        cuts.append({"src": s.id, "in": ms3(s.avail_in), "out": ms3(s.avail_in + dur), "dur": ms3(dur), "desc": s.desc,
                     "cap": ms3(s.avail_out)})
        remaining = ms3(remaining - dur)
    _extend(lambda c: float("inf"))
    for c in cuts:
        c.pop("cap", None)
    if remaining <= 0.01:          # ms 반올림 잔차는 부족이 아니다
        remaining = 0.0
    return cuts, ms3(max(0.0, remaining))


def prev_shots_before(cuts: list[float], t: float, *, count: int = 6) -> list[tuple[float, float]]:
    """t 이전의 샷 목록(안전 마진 적용) — 가까운 것부터. 뒤쪽 샷이 전부 막혔을 때(활용 불가 구간·이미 쓴 구간) 보충용. 순수 — 테스트 대상."""
    out: list[tuple[float, float]] = []
    bounds = sorted([c for c in cuts if c < t], reverse=True)
    nxt = t
    for c in bounds:
        if len(out) >= count:
            return out
        lo, hi = c + SAFE_MARGIN_SEC, nxt - SAFE_MARGIN_SEC
        if hi - lo >= 0.5:
            out.append((ms3(lo), ms3(hi)))
        nxt = c
    if len(out) < count and nxt > 0:                      # 경계를 다 썼으면 소스 시작~첫 경계
        lo, hi = SAFE_MARGIN_SEC, nxt - SAFE_MARGIN_SEC
        if hi - lo >= 0.5:
            out.append((ms3(lo), ms3(hi)))
    return out


def next_shots_after(cuts: list[float], t: float, duration: float, *, count: int = 6) -> list[tuple[float, float]]:
    """t 이후의 샷 경계 목록(안전 마진 적용) — 컷 부족 시 인접 샷 보충용."""
    out: list[tuple[float, float]] = []
    bounds = [c for c in cuts if c > t]
    prev = t
    for c in bounds[:count]:
        lo, hi = prev + SAFE_MARGIN_SEC, c - SAFE_MARGIN_SEC
        if hi - lo >= 0.5:
            out.append((ms3(lo), ms3(hi)))
        prev = c
    if len(out) < count and prev < duration:
        lo, hi = prev + SAFE_MARGIN_SEC, duration - SAFE_MARGIN_SEC
        if hi - lo >= 0.5:
            out.append((ms3(lo), ms3(hi)))
    return out
