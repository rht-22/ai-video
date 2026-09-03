"""걸음 5 — 내레이션 덮개(cover): 그 자리를 **다시 본다**.

사람은 내레이션 길이를 알고 원본을 스크럽하며 그림이 되는 in/out 을 찍는다. 그
동작의 기계화(2026-09-03 사용자 결정):
  · 문장·합성 길이 L 은 정본(걸음 4) — 덮개가 L 에 맞춘다. 문장은 안 줄인다.
  · 덮개 후보 창을 코드가 정한다(① 앵커 앞 빈 구간 → ② 같은 씬의 미사용 구간
    (B-roll) → ③ 고른 씬 전체의 미사용 구간 → ④ 대사 머리 뮤트 — 참고 쇼츠가 실제로
    쓰는 방식이고 기록을 크게 남긴다). 대사로 고른 조각은 ①~③에서 절대 재생하지
    않는다(같은 대사가 두 번 나가면 안 된다).
  · 창이 L 보다 넉넉하면 **그 창만 잘라 Flash 에 보여 주고 시작점을 받는다**
    (refine.py 의 프로브 기계 재사용). 답은 장면 전환·grid 경계에 스냅. 실패·예산
    소진이면 앵커에 붙이는 산술 폴백 + 기록.
  · 덮개 경계는 grid 위가 아닐 수 있다 — assemble 이 `cover` 클립으로 정식 등록한다
    (tail_pad·head_trimmed 와 같은 지위: 출처가 실측/관찰이라 시각 환각 방어 위반이
    아니다).

⚠ 뮤트 판정은 화면 종류가 아니다 — 참고 쇼츠 3편 실측(work/nar_ref): 발화
클로즈업이라도 내레이션이 가리키는 장면이면 그대로 덮는다. importance 도 안 본다.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from app.v3.story_flow.common import fmt_t

NAR_PAD_SEC = 0.25           # 합성 길이에 더하는 여유(머리 0.1 · 꼬리 0.15)
NAR_HEAD_PAD_SEC = 0.10      # 엔딩 덮개 — 머리 여유만(내레이션 끝 = 컷)
JOIN_GAP_SEC = 0.6           # 덮개 끝~앵커 틈이 이보다 짧으면 덮개를 앵커까지 늘려 잇는다
SNAP_TOL_SEC = 0.30          # 프로브 답을 장면 전환·grid 경계에 붙이는 관용
PROBE_MIN_SLACK_SEC = 1.0    # 창 − L 이 이보다 작으면 고를 게 없다 — 호출 생략
PROBE_MAX_WINDOW_SEC = 30.0  # 프로브 클립 상한(넘으면 지정 화면 중심으로 자른다)
FLASH_BUDGET = 10            # 편당 덮개 프로브 호출 상한(refine 과 같은 규율)
MIN_FREE_SEC = 0.5

PROBE_PROMPT = """당신은 쇼츠 편집자다. 첨부 클립은 원본 {t0}~{t1} 구간이다(클립 0초 = 원본 {t0}). 이 클립의 **화면 위에** 내레이션을 얹을 것이다 — 소리는 끄고 화면만 쓴다.
내레이션: 「{text}」 ({L:.1f}초)
내레이션이 가리키는 화면: {refers_to}
규칙: 시작점 start_sec(클립 기준 초, 0.1초 단위) **하나만** 골라라. 내레이션은 start_sec 부터 {L:.1f}초 동안 흐르므로 start_sec ≤ {latest:.1f} 이어야 한다({end_note}). 내레이션 문구와 맞는 그림이 흐르는 구간에서, 동작이 시작되는 순간이나 장면 전환 직후에 들어가라. 동작 중간·컷 직전에서 시작하지 마라.
장면 전환 후보(클립 기준): {cuts}
화면 기록(클립 기준): {scripts}
{{"start_sec": 3.2, "reason": "한 줄", "confidence": "high|low"}}"""


# ── 구간 산술(순수) ────────────────────────────────────────────────────────

def used_intervals(beats: list[dict], span_index: dict[str, dict]) -> list[tuple[float, float]]:
    iv = sorted((span_index[x]["t_in"], span_index[x]["t_out"])
                for b in beats for x in b.get("span_ids") or [] if x in span_index)
    merged: list[tuple[float, float]] = []
    for a, z in iv:
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], z))
        else:
            merged.append((a, z))
    return merged


def subtract(window: tuple[float, float],
             used: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """창에서 사용 구간을 뺀 빈 구간들(시간순)."""
    a0, z0 = window
    out: list[tuple[float, float]] = []
    cur = a0
    for a, z in used:
        if z <= cur or a >= z0:
            continue
        if a > cur:
            out.append((cur, min(a, z0)))
        cur = max(cur, z)
        if cur >= z0:
            break
    if cur < z0:
        out.append((cur, z0))
    return [(a, z) for a, z in out if z - a >= MIN_FREE_SEC]


def snap_time(t: float, grid: dict, tol: float = SNAP_TOL_SEC) -> tuple[float, str]:
    """장면 전환 → grid span 경계 순으로 tol 안 최근접에 붙인다. 없으면 원값."""
    best: tuple[float, str] | None = None
    for c in grid.get("scene_cuts") or []:
        c = float(c)
        if abs(c - t) <= tol and (best is None or abs(c - t) < abs(best[0] - t)):
            best = (c, "scene_cut")
    if best is not None:
        return round(best[0], 3), best[1]
    for sp in grid.get("span_candidates") or []:
        for k in ("t_in", "t_out"):
            v = float(sp[k])
            if abs(v - t) <= tol and (best is None or abs(v - t) < abs(best[0] - t)):
                best = (v, "grid_edge")
    if best is not None:
        return round(best[0], 3), best[1]
    return round(t, 2), "raw"


def spans_overlapping(t0: float, t1: float, span_index: dict[str, dict]) -> list[str]:
    out = [sid for sid, sp in span_index.items()
           if sp["t_in"] < t1 - 1e-6 and sp["t_out"] > t0 + 1e-6]
    return sorted(out, key=lambda s: span_index[s]["pos"])


def scene_bounds(scene_idx: int, rows_by_idx: dict[int, dict],
                 fallback: tuple[float, float]) -> tuple[float, float]:
    r = rows_by_idx.get(scene_idx)
    return (r["t0"], r["t1"]) if r else fallback


# ── 후보 창 ────────────────────────────────────────────────────────────────

def _merge(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for a, z in sorted(iv):
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], z))
        else:
            merged.append((a, z))
    return merged


def candidate_windows(anchor: tuple[str, int], beats: list[dict],
                      span_index: dict[str, dict], rows_by_idx: dict[int, dict],
                      L: float,
                      extra_used: list[tuple[float, float]] | None = None) -> list[dict]:
    """앵커(before k | after k)에 대한 덮개 후보 창 — 우선순위 순.
    각 {kind, w0, w1, attach: 'end'|'start'} — attach 는 L 창을 어느 끝에 붙이는지
    (프로브가 없을 때의 산술 위치).

    rows_by_idx 는 **고른 씬만** — B-roll 을 편 전체에서 끌어오면 다른 줄거리의
    화면이 섞인다. extra_used = 이미 놓인 덮개(같은 화면을 두 내레이션이 쓰면 안 된다).
    lead_spill/tail_spill: 앵커 앞 빈 구간이 L 에 못 미칠 때 **모자란 만큼만** 대사
    머리(꼬리)를 뮤트해 잇는다 — 대사 전체를 뮤트하는 것보다 덜 잃는다."""
    kind, k = anchor
    used = _merge(used_intervals(beats, span_index) + list(extra_used or []))
    b = beats[k]
    ids = b["span_ids"]
    first_t = span_index[ids[0]]["t_in"]
    last_t = span_index[ids[-1]]["t_out"]
    sb0, sb1 = scene_bounds(b["scene"], rows_by_idx, (first_t, last_t))
    out: list[dict] = []
    if kind == "before":
        voiced = [x for x in ids if span_index[x]["is_audio"]]
        anchor_t = span_index[voiced[0]]["t_in"] if voiced else first_t
        # 비트 머리의 무성 조각은 덮개 재료다(같은 장면 · 대사 아님) — 사용 구간에서 뺀다
        head_visual = [x for x in ids if span_index[x]["pos"]
                       < span_index[voiced[0]]["pos"]] if voiced else []
        used_no_head = _merge(used_intervals(
            [dict(bb, span_ids=[x for x in bb["span_ids"] if x not in head_visual])
             for bb in beats], span_index) + list(extra_used or []))
        lo = sb0
        # 앵커 앞에서 끝나거나 앵커를 걸치는 사용 구간(대사·이미 놓인 덮개) 뒤부터
        prev_end = max([min(z, anchor_t) for a, z in used_no_head
                        if a < anchor_t - 1e-6 and z > lo], default=None)
        if prev_end is not None:
            lo = max(lo, prev_end)
        lead_len = anchor_t - lo
        if lead_len >= L:
            out.append({"kind": "lead_in", "w0": lo, "w1": anchor_t, "attach": "end"})
        spill = ({"kind": "lead_spill", "w0": lo, "w1": lo + L, "attach": "start"}
                 if MIN_FREE_SEC <= lead_len < L else None)
        # B-roll — 같은 씬 → 고른 씬 전체, 앵커에서 가까운 순
        for scope, ok_scene in (("broll_scene", lambda s: s == b["scene"]),
                                ("broll_any", lambda s: True)):
            frees: list[tuple[float, float, float]] = []
            for si, r in rows_by_idx.items():
                if not ok_scene(si):
                    continue
                for a, z in subtract((r["t0"], r["t1"]), used_no_head):
                    if z - a >= L:
                        frees.append((abs((a + z) / 2 - anchor_t), a, z))
            for _d, a, z in sorted(frees):
                if not any(abs(w["w0"] - a) < 1e-6 and abs(w["w1"] - z) < 1e-6 for w in out):
                    out.append({"kind": scope, "w0": a, "w1": z, "attach": "start"})
        if spill:
            out.append(spill)
        # 최후 — 대사 머리 뮤트(참고 쇼츠가 쓰는 방식 · 기록 필수)
        out.append({"kind": "mute_head", "w0": anchor_t, "w1": anchor_t + L,
                    "attach": "start"})
    else:
        hi = sb1
        next_start = min([max(a, last_t) for a, z in used
                          if z > last_t + 1e-6 and a < hi], default=None)
        if next_start is not None:
            hi = min(hi, next_start)
        tail_len = hi - last_t
        if tail_len >= L:
            out.append({"kind": "tail", "w0": last_t, "w1": hi, "attach": "start"})
        spill = ({"kind": "tail_spill", "w0": hi - L, "w1": hi, "attach": "start"}
                 if MIN_FREE_SEC <= tail_len < L else None)
        for scope, ok_scene in (("broll_scene", lambda s: s == b["scene"]),
                                ("broll_any", lambda s: True)):
            frees = []
            for si, r in rows_by_idx.items():
                if not ok_scene(si):
                    continue
                for a, z in subtract((r["t0"], r["t1"]), used):
                    if z - a >= L:
                        frees.append((abs((a + z) / 2 - last_t), a, z))
            for _d, a, z in sorted(frees):
                if not any(abs(w["w0"] - a) < 1e-6 and abs(w["w1"] - z) < 1e-6 for w in out):
                    out.append({"kind": scope, "w0": a, "w1": z, "attach": "start"})
        if spill:
            out.append(spill)
        out.append({"kind": "mute_tail", "w0": max(first_t, last_t - L), "w1": last_t,
                    "attach": "end"})
    return out


def _cover_desc(group: dict, span_index: dict[str, dict]) -> str:
    return " / ".join(span_index[x].get("scene_script") or "" for x in group.get("cover_ids") or []
                      if x in span_index)[:300]


def designated_window(cover_ids: list[str], beats: list[dict], span_index: dict[str, dict],
                      L: float, extra_used: list[tuple[float, float]] | None = None
                      ) -> dict | None:
    """모델이 지정한 화면 조각 → 그 조각들을 담는 연속 창(kind='designated').
    지정 조각 자체는 비트 밖(미사용)이어야 하고, L 에 못 미치면 grid 인접 미사용
    조각으로 앞뒤로 넓힌다(대사로 고른 조각·이미 놓인 덮개는 넘지 않는다)."""
    ids = [x for x in cover_ids if x in span_index]
    if not ids:
        return None
    used = _merge(used_intervals(beats, span_index) + list(extra_used or []))
    ids.sort(key=lambda x: span_index[x]["pos"])
    f0 = span_index[ids[0]]["t_in"]
    f1 = span_index[ids[-1]]["t_out"]
    if any(a < f1 - 1e-6 and z > f0 + 1e-6 for a, z in used):
        return None                      # 지정 화면이 대사·덮개와 겹친다 — 후보 탐색으로
    by_pos = {sp["pos"]: sid for sid, sp in span_index.items()}
    w0, w1 = f0, f1
    lo_pos, hi_pos = span_index[ids[0]]["pos"], span_index[ids[-1]]["pos"]
    # 뒤로, 앞으로 번갈아 넓힌다(지정 화면이 창 가운데 근처에 오게)
    grow_back = True
    while w1 - w0 < L - 1e-6:
        moved = False
        for _ in range(2):
            if grow_back:
                cand = by_pos.get(hi_pos + 1)
                if cand is not None and abs(span_index[cand]["t_in"] - w1) < 0.05 \
                        and not any(a < span_index[cand]["t_out"] - 1e-6 and z > w1 + 1e-6
                                    for a, z in used):
                    w1 = span_index[cand]["t_out"]
                    hi_pos += 1
                    moved = True
            else:
                cand = by_pos.get(lo_pos - 1)
                if cand is not None and abs(span_index[cand]["t_out"] - w0) < 0.05 \
                        and not any(a < w0 - 1e-6 and z > span_index[cand]["t_in"] + 1e-6
                                    for a, z in used):
                    w0 = span_index[cand]["t_in"]
                    lo_pos -= 1
                    moved = True
            grow_back = not grow_back
            if w1 - w0 >= L - 1e-6:
                break
        if not moved:
            break
    return {"kind": "designated", "w0": w0, "w1": w1, "attach": "start",
            "focus0": f0, "focus1": f1}


def arithmetic_start(win: dict, L: float) -> float:
    """프로브 없이 정하는 시작점 — attach 끝에 L 을 붙인다."""
    if win["attach"] == "end":
        return max(win["w0"], win["w1"] - L)
    return win["w0"]


# ── 프로브(영상을 다시 본다) ────────────────────────────────────────────────

def probe_window(win: dict, L: float, focus: tuple[float, float] | None = None
                 ) -> tuple[float, float]:
    """프로브 클립 창. 창이 상한(PROBE_MAX_WINDOW_SEC) 안이면 **통째로** 보여 준다 —
    앵커 쪽만 잘라 보여 주던 규칙은 폐지(2026-09-03 실사고: 발장난 화면이 창의
    먼 쪽에 있었는데 잘려 나가 모델이 깨진 잔 수습 장면을 골랐다). 상한을 넘으면
    focus(모델이 지정한 화면 구간)를 가운데 두고, focus 가 없을 때만 앵커 쪽을 남긴다."""
    w0, w1 = win["w0"], win["w1"]
    cap = PROBE_MAX_WINDOW_SEC
    if w1 - w0 <= cap:
        return w0, w1
    if focus is not None:
        c = (focus[0] + focus[1]) / 2
        a = min(max(w0, c - cap / 2), w1 - cap)
        return a, a + cap
    if win["attach"] == "end":
        return w1 - cap, w1
    return w0, w0 + cap


def build_probe_prompt(t0: float, t1: float, L: float, text: str, refers_to: str,
                       grid: dict, span_index: dict[str, dict], *, attach: str) -> str:
    from app.v3.refine import scene_cut_candidates
    cuts = scene_cut_candidates(grid, t0, t1)
    cut_s = ", ".join(f"{c['rel']:.1f}s" for c in cuts) or "없음"
    scripts = []
    for sid in spans_overlapping(t0, t1, span_index):
        sp = span_index[sid]
        desc = sp.get("scene_script") or ""
        if sp["is_audio"]:
            from app.v3.story_flow.common import span_text
            desc = (desc + " · 대사: " + span_text(sp)).strip(" ·")
        if desc:
            scripts.append(f"{max(0.0, sp['t_in'] - t0):.1f}~{min(t1, sp['t_out']) - t0:.1f}s {desc}")
    latest = max(0.0, (t1 - t0) - L)
    end_note = ("클립 끝에 다음 대사가 온다" if attach == "end" else "클립 길이 안")
    return PROBE_PROMPT.format(t0=fmt_t(t0), t1=fmt_t(t1), text=text, L=L,
                               refers_to=refers_to or "(기록 없음)",
                               latest=latest, end_note=end_note, cuts=cut_s,
                               scripts=" / ".join(scripts)[:1500] or "없음")


def validate_probe(resp: Any, L: float, clip_len: float) -> tuple[float | None, str]:
    if not isinstance(resp, dict):
        return None, "응답이 객체가 아니다"
    try:
        s = float(resp.get("start_sec"))
    except (TypeError, ValueError):
        return None, "start_sec 가 숫자가 아니다"
    latest = clip_len - L
    if s < -0.05 or s > latest + 0.5:
        return None, f"start_sec {s} 가 0~{latest:.1f} 밖"
    return round(min(max(s, 0.0), max(0.0, latest)), 2), ""


def cut_clip(video: Path, t0: float, t1: float, out: Path) -> None:
    from app.modules.ffmpeg_utils import find_ffmpeg_command
    from app.v3.refine import _cut_probe_clip
    out.parent.mkdir(parents=True, exist_ok=True)
    _cut_probe_clip(find_ffmpeg_command("ffmpeg"), video, t0, t1, out)


def run_probe(gemini, video: Path, out_dir: Path, tag: str, win: dict, L: float,
              text: str, refers_to: str, grid: dict, span_index: dict[str, dict],
              log=print, focus: tuple[float, float] | None = None) -> dict | None:
    """창을 잘라 Flash 에 묻는다 → {start(원본 절대초), snap, raw, reason} | None."""
    from app.v3.refine import _call_probe
    t0, t1 = probe_window(win, L, focus)
    clip = out_dir / f"cover_probe_{tag}.mp4"
    try:
        cut_clip(video, t0, t1, clip)
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"  [v3/cover] ⚠ 프로브 클립 재단 실패({tag}): {e}")
        return None
    prompt = build_probe_prompt(t0, t1, L, text, refers_to, grid, span_index,
                                attach=win["attach"])
    resp = None
    for attempt in range(2):                # JSON 깨짐·일시 오류는 한 번 더(실측: 문자열 미종결)
        try:
            resp = _call_probe(gemini, clip, prompt)
            break
        except Exception as e:  # noqa: BLE001 — 프로브 실패는 폴백(조용한 실패 아님 — 로그)
            log(f"  [v3/cover] ⚠ 프로브 호출 실패({tag}, 시도 {attempt + 1}/2): {e}")
    if resp is None:
        return None
    rel, why = validate_probe(resp, L, t1 - t0)
    if rel is None:
        log(f"  [v3/cover] ⚠ 프로브 답 거절({tag}): {why} — {json.dumps(resp, ensure_ascii=False)[:120]}")
        return None
    raw = t0 + rel
    snapped, how = snap_time(raw, grid)
    # 스냅이 창 밖·L 미확보로 밀면 원값
    if snapped < win["w0"] - 1e-6 or snapped + L > win["w1"] + 1e-6:
        snapped, how = round(raw, 2), "raw"
    return {"start": snapped, "snap": how, "raw": round(raw, 2),
            "reason": str(resp.get("reason") or "")[:120],
            "confidence": str(resp.get("confidence") or ""),
            "probe_window": [round(t0, 3), round(t1, 3)]}


# ── 덮개 확정 ──────────────────────────────────────────────────────────────

def choose_cover(anchor: tuple[str, int], group: dict, beats: list[dict],
                 span_index: dict[str, dict], rows_by_idx: dict[int, dict],
                 grid: dict, *, gemini=None, video: Path | None = None,
                 out_dir: Path | None = None, budget: dict | None = None,
                 placed: list[tuple[float, float]] | None = None,
                 log=print) -> dict:
    """한 내레이션 묶음의 덮개 → {position, kind, t_in, t_out, span_ids, probe, note}."""
    # 엔딩(after) 덮개는 내레이션이 끝나는 곳에서 **뚝** 끊는다 — 꼬리 여유 없이 머리
    # 0.1s 만(2026-09-03 사용자 지적: 마지막 내레이션 뒤 장면이 길다)
    pad = NAR_HEAD_PAD_SEC if anchor[0] == "after" else NAR_PAD_SEC
    L = round(sum(group["measured"]) + pad, 3)
    kind, k = anchor
    tag = f"{kind}{k}"
    text = " ".join(group["lines"])
    # ⓪ 모델이 지정한 화면(걸음 4 cover id) — 그 조각들을 담는 연속 창. L 에 못 미치면
    # 인접 미사용 조각으로 넓힌다. 이 창이 후보 0순위다(내레이션이 가리키는 그 장면).
    wins: list[dict] = []
    focus: tuple[float, float] | None = None
    if group.get("cover_ids"):
        dw = designated_window(group["cover_ids"], beats, span_index, L, extra_used=placed)
        if dw is not None:
            wins.append(dw)
            focus = (dw["focus0"], dw["focus1"])
    wins += candidate_windows(anchor, beats, span_index, rows_by_idx, L,
                              extra_used=placed)
    for win in wins:
        if win["w1"] - win["w0"] < L - 1e-6:
            # 창이 L 에 못 미친다 — 부족한 덮개 위에 얹으면 다음 대사를 밟는다. 다음 후보로.
            continue
        probe = None
        slack = (win["w1"] - win["w0"]) - L
        can_probe = (gemini is not None and video is not None and out_dir is not None
                     and slack >= PROBE_MIN_SLACK_SEC
                     and not win["kind"].startswith("mute")
                     and not win["kind"].endswith("spill"))
        if can_probe and budget is not None and budget.get("left", 0) <= 0:
            log(f"  [v3/cover] 프로브 예산 소진 — {tag} 산술 배치")
            can_probe = False
        if can_probe:
            if budget is not None:
                budget["left"] = budget.get("left", 0) - 1
                budget["used"] = budget.get("used", 0) + 1
            probe = run_probe(gemini, video, out_dir, tag, win, L, text,
                              group.get("refers_to", "") or _cover_desc(group, span_index),
                              grid, span_index, log=log,
                              focus=focus if win["kind"] == "designated" else None)
        if probe:
            start = probe["start"]
        elif win["kind"] == "designated":
            start = max(win["w0"], min(win["focus0"], win["w1"] - L))
        else:
            start = arithmetic_start(win, L)
        end = start + L
        note = ""
        if win["kind"] == "lead_in" and 0 < win["w1"] - end < JOIN_GAP_SEC:
            end = win["w1"]                # 앵커까지 잇는다(작은 틈 없애기)
        if win["kind"] in ("mute_head", "mute_tail"):
            note = (f"덮개 자리가 없어 {'첫' if kind == 'before' else '마지막'} 대사 "
                    f"{L:.1f}s 를 뮤트하고 얹는다(참고 쇼츠 방식) — 검수 대상")
            log(f"  [v3/cover] ⚠ {tag}: {note}")
        elif win["kind"] in ("lead_spill", "tail_spill"):
            _lack = L - (win["w1"] - win["w0"]) if win["kind"] == "tail_spill" \
                else (win["w1"] - (span_index[beats[k]["span_ids"][0]]["t_in"]
                                   if kind == "before" else win["w1"]))
            note = (f"앞 여백이 {L:.1f}s 에 못 미쳐 대사 "
                    f"{'머리' if kind == 'before' else '꼬리'} 일부를 뮤트해 잇는다 — 검수 대상")
            log(f"  [v3/cover] ⚠ {tag}: {note}")
        return {"position": kind, "kind": win["kind"],
                "t_in": round(start, 3), "t_out": round(end, 3),
                "span_ids": spans_overlapping(start, end, span_index),
                "probe": probe, "note": note or None,
                "window": [round(win["w0"], 3), round(win["w1"], 3)],
                "L": L}
    raise AssertionError(f"덮개 후보가 하나도 없다({tag}) — mute 후보는 항상 있어야 한다")


def apply_cover_to_beats(cover: dict, beats: list[dict], span_index: dict[str, dict],
                         k: int) -> list[str]:
    """덮개가 재생하는 구간을 비트에서 뺀다(같은 화면 두 번 금지). 덮개 끝이 span
    중간이면 그 span 은 head_trim 으로 시작을 당긴다. 반환: 뺀 span id."""
    t_in, t_out = cover["t_in"], cover["t_out"]
    removed: list[str] = []
    for bi, b in enumerate(beats):
        keep: list[str] = []
        for x in b["span_ids"]:
            sp = span_index[x]
            if sp["t_in"] >= t_in - 1e-6 and sp["t_out"] <= t_out + 1e-6:
                removed.append(x)
                continue
            # 대사 머리 뮤트 덮개는 비트 머리(앵커 앞 무성)를 대신한다 — 덮개가
            # 비트 클립 **앞**에 놓이므로 앵커 앞 조각이 남으면 순서가 뒤집힌다
            if bi == k and cover.get("kind") in ("mute_head", "lead_spill") \
                    and cover["position"] == "before" and sp["t_out"] <= t_in + 1e-6:
                removed.append(x)
                continue
            keep.append(x)
        if keep != b["span_ids"]:
            b["span_ids"] = keep
            b["lines"] = [x for x in b.get("lines") or [] if x in keep]
            b["visual"] = [x for x in b.get("visual") or [] if x in keep]
        if bi == k and cover["position"] == "before" and keep:
            first = span_index[keep[0]]
            if first["t_in"] < t_out - 0.05 < first["t_out"]:
                b["head_trim_sec"] = round(t_out, 3)
    return removed


HEAD_GAP_MAX_SEC = 1.5   # 덮개(내레이션) 끝 → 첫 대사까지 허용하는 무발화 머리(2026-09-03)


def cap_head_gap(beat: dict, span_index: dict[str, dict],
                 cap: float = HEAD_GAP_MAX_SEC) -> dict | None:
    """덮개 뒤 비트의 무발화 머리를 cap 초까지만 남긴다(순수 · 비트를 제자리에서 고친다).

    사용자 지적(2026-09-03): 내레이션이 끝나고 첫 대사(또는 훅 장면)까지 너무 멀면 문제 —
    이번 실런은 훅 비트 머리에 6초가 비어 있었다(내레이션 → 무발화 6초 → 첫 대사).
    이건 시간 문제라 코드 자리다. 앞의 **무성 조각을 통째로** 덜어내고, 그래도 남으면
    첫 조각 안에서 head_trim_sec 로 시작을 당긴다(assemble 은 첫 조각 안의 트림만 안다).
    유성 조각은 안 건드린다. 반환: 바뀌었으면 기록 dict, 아니면 None."""
    ids = [x for x in beat.get("span_ids") or [] if x in span_index]
    voiced = [x for x in ids if span_index[x]["is_audio"]]
    if not ids or not voiced:
        return None
    anchor_t = span_index[voiced[0]]["t_in"]
    start_t = float(beat.get("head_trim_sec") or span_index[ids[0]]["t_in"])
    before = anchor_t - start_t
    if before <= cap + 1e-6:
        return None
    removed: list[str] = []
    keep = list(ids)
    while keep and keep[0] != voiced[0] and not span_index[keep[0]]["is_audio"] \
            and anchor_t - span_index[keep[0]]["t_out"] >= cap - 1e-6:
        removed.append(keep.pop(0))
    head_trim = None
    first = span_index[keep[0]]
    if not first["is_audio"] and anchor_t - first["t_in"] > cap + 1e-6:
        head_trim = round(anchor_t - cap, 3)
    if removed or head_trim is not None:
        beat["span_ids"] = keep
        beat["lines"] = [x for x in beat.get("lines") or [] if x in keep]
        beat["visual"] = [x for x in beat.get("visual") or [] if x in keep]
        if head_trim is not None:
            beat["head_trim_sec"] = head_trim
        elif beat.get("head_trim_sec") is not None and beat["head_trim_sec"] < span_index[keep[0]]["t_in"]:
            beat.pop("head_trim_sec")
    after = anchor_t - (head_trim if head_trim is not None else span_index[keep[0]]["t_in"])
    return {"before_sec": round(before, 3), "after_sec": round(after, 3),
            "removed_span_ids": removed, "head_trim_sec": head_trim}


__all__ = ["choose_cover", "apply_cover_to_beats", "candidate_windows", "subtract",
           "used_intervals", "snap_time", "validate_probe", "build_probe_prompt",
           "arithmetic_start", "probe_window", "spans_overlapping", "NAR_PAD_SEC",
           "JOIN_GAP_SEC", "FLASH_BUDGET", "HEAD_GAP_MAX_SEC", "cap_head_gap"]
