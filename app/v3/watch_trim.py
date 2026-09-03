"""시청 트림 패스 (2026-09-02, 사용자 설계) — 렌더된 초안을 LLM 이 다시 보고
늘어지는 구간을 지목하면, 코드가 검증해 **빼기만** 한다.

기록(중요도·전사)으로는 '여운인지 늘어짐인지'를 판정할 수 없다 — 화면을 봐야 안다.
M4 는 이미 초안(draft 480p)을 렌더하고 Stage 4 가 그것을 보며 라벨을 쓴다. 같은
초안에 질문 하나를 더 얹는 것이라 아키텍처("영상을 보는 단계" 목록)에 새 눈을
만들지 않는다.

규율 (시각 환각 방어 계승 + 빼기 전용):
- 모델은 **초안 시계**(0초부터)로 구간을 제안만 한다 — 모델 산 숫자는 ±초 단위로
  틀릴 수 있다(Stage 2 귀속 드리프트 실측 14.5s). 확정은 코드의 **안쪽 축소 스냅**:
  제안 구간 안에서 span 경계로 좁힌다. 제거분 ⊆ 제안분 — 모델이 지목 안 한 재료는
  수학적으로 안 잘리고, 컷은 항상 단어 끝·장면 경계에 떨어진다.
- 하드 가드(모델이 지목해도 코드가 거부+기록): 내레이션 창, importance≥4 유성 span,
  엔딩·payoff·마지막 클립, 총 제거량 상한(TRIM_CAP_RATIO).
- **빼기 전용** — 장면 추가 금지. 편성 권한은 Stage 3 하나다(권한이 갈라지면 반려
  루프 규율이 무너진다).
- fail-soft: 호출·파싱 실패 = 트림 0 + 기록(부가물이 본편 발행을 막지 않는다).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MAX_CUTS = 3                 # 한 판에 지목 가능한 구간 수
MAX_CUTS_BUDGET = 6          # 예산 컷 판(초과분을 반드시 덜어야 함)의 상한
MIN_CUT_SEC = 0.5            # 스냅 후 이보다 짧으면 자를 실익이 없다
SNAP_TOL_SEC = 0.25          # 모델은 0.1s 단위로 말한다 — 경계(예: 15.664)를 15.7 로
                             # 부르면 0.01 톨러런스는 놓친다(실사고: 정확한 지목이
                             # '실익 없음' 거부). 제안 밖 확장은 이 값이 상한.
TRIM_CAP_RATIO = 0.20        # 총 제거량 상한(초안 길이 대비) — 과잉 절제 방지
GUARD_PAD_SEC = 0.2          # 보호 구간 주변 여유
WATCH_SAMPLE_FPS = 6.0       # Stage 4 와 같은 표본(사용자 설정 6fps)


# ── 좌표·스냅 (순수) ────────────────────────────────────────────────────────

def edited_span_edges(timeline: list[dict], grid: dict) -> list[float]:
    """편집본 좌표의 스냅 어휘 — 클립 경계 ∪ 클립 내부 span 경계. 정렬 반환."""
    span_t = {sp["id"]: (float(sp["t_in"]), float(sp["t_out"]))
              for sp in grid.get("span_candidates") or []}
    edges: set[float] = set()
    off = 0.0
    for c in timeline:
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        edges.add(round(off, 3))
        edges.add(round(off + e - s, 3))
        for sid in c.get("span_ids") or []:
            if sid not in span_t:
                continue
            for t in span_t[sid]:
                if s + 0.01 < t < e - 0.01:
                    edges.add(round(off + t - s, 3))
        off += e - s
    return sorted(edges)


def snap_inward(start: float, end: float,
                edges: list[float]) -> tuple[float, float] | None:
    """제안 [start,end] → 안쪽 span 경계로 축소. 좁아져 실익 없으면 None."""
    s2 = next((v for v in edges if v >= start - SNAP_TOL_SEC), None)
    e2 = next((v for v in reversed(edges) if v <= end + SNAP_TOL_SEC), None)
    if s2 is None or e2 is None or e2 - s2 < MIN_CUT_SEC:
        return None
    return s2, e2


def protected_intervals(timeline: list[dict], resources: dict,
                        importance: dict[str, tuple[bool, int]],
                        grid: dict) -> list[tuple[float, float, str]]:
    """(start, end, 사유) 편집본 좌표 보호 목록 — 겹치는 제안은 거부한다."""
    out: list[tuple[float, float, str]] = []
    for f in resources.get("tts_cue_files") or []:
        cue = f.get("cue") or {}
        if cue.get("start_sec") is not None and cue.get("end_sec") is not None:
            out.append((float(cue["start_sec"]) - GUARD_PAD_SEC,
                        float(cue["end_sec"]) + GUARD_PAD_SEC, "내레이션 창"))
    span_t = {sp["id"]: (float(sp["t_in"]), float(sp["t_out"]))
              for sp in grid.get("span_candidates") or []}
    off = 0.0
    n = len(timeline)
    for i, c in enumerate(timeline):
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        dur = e - s
        if c.get("role") in ("ending", "payoff") or i == n - 1:
            out.append((off, off + dur, f"{c.get('role') or '마지막'} 클립(리빌·여운)"))
        else:
            for sid in c.get("span_ids") or []:
                aud, imp = importance.get(sid, (False, 3))
                if aud and imp >= 4 and sid in span_t:
                    a = max(span_t[sid][0], s)
                    z = min(span_t[sid][1], e)
                    if z > a:
                        out.append((off + a - s, off + z - s,
                                    f"핵심 대사({sid} imp{imp})"))
        off += dur
    return out


def _carve_guards(a: float, z: float, guards: list[tuple[float, float, str]]
                  ) -> tuple[list[tuple[float, float]], list[str]]:
    """보호 구간을 [a,z] 에서 **차감**한 조각들과, 스친 보호 이름 목록."""
    pieces = [(a, z)]
    hit_names: list[str] = []
    for g0, g1, name in guards:
        nxt: list[tuple[float, float]] = []
        for pa, pz in pieces:
            if g0 < pz and g1 > pa:
                hit_names.append(name)
                if pa < g0:
                    nxt.append((pa, g0))
                if g1 < pz:
                    nxt.append((g1, pz))
            else:
                nxt.append((pa, pz))
        pieces = nxt
    return [p for p in pieces if p[1] - p[0] >= MIN_CUT_SEC], hit_names


def validate_cuts(raw: Any, *, duration: float, edges: list[float],
                  guards: list[tuple[float, float, str]],
                  max_cuts: int = MAX_CUTS
                  ) -> tuple[list[dict], list[dict]]:
    """모델 제안 → (적용 목록, 거부 기록). 전부 편집본 좌표·스냅 완료 상태로 반환."""
    applied: list[dict] = []
    rejected: list[dict] = []
    cap = duration * TRIM_CAP_RATIO
    total = 0.0
    items = raw if isinstance(raw, list) else []
    for k, it in enumerate(items[:max_cuts]):
        try:
            a, z = float(it["start_sec"]), float(it["end_sec"])
        except (KeyError, TypeError, ValueError):
            rejected.append({"index": k, "reason": "형식 오류", "raw": str(it)[:80]})
            continue
        a, z = max(0.0, a), min(duration, z)
        if z - a < MIN_CUT_SEC:
            rejected.append({"index": k, "reason": "구간이 너무 짧다",
                             "range": [a, z]})
            continue
        # 보호 구간은 거부가 아니라 **차감**한다 — 모델 경계가 ±초 단위로 흐려
        # 보호 꼬리를 스치기만 해도 통째 거부하면 멀쩡한 제안이 다 죽는다.
        # 남는 조각 중 최대를 쓴다(여전히 제거분 ⊆ 제안분).
        pieces, hit_names = _carve_guards(a, z, guards)
        if not pieces:
            rejected.append({"index": k, "reason": "보호 구간이 전부 덮음: "
                             + ", ".join(sorted(set(hit_names))[:3]),
                             "range": [a, z]})
            continue
        a, z = max(pieces, key=lambda p: p[1] - p[0])
        snapped = snap_inward(a, z, edges)
        if snapped is None:
            rejected.append({"index": k, "reason": "스냅 후 실익 없음(<0.5s)",
                             "range": [a, z]})
            continue
        s2, e2 = snapped
        if any(c["start"] < e2 and c["end"] > s2 for c in applied):
            rejected.append({"index": k, "reason": "적용분과 겹침", "range": [a, z]})
            continue
        if total + (e2 - s2) > cap:
            rejected.append({"index": k, "reason": f"총량 상한({cap:.1f}s) 초과",
                             "range": [s2, e2]})
            continue
        total += e2 - s2
        applied.append({"start": s2, "end": e2,
                        "proposed": [round(a, 2), round(z, 2)],
                        "reason": str(it.get("reason") or "")[:80]})
    applied.sort(key=lambda c: c["start"])
    return applied, rejected


def budget_fallback_cuts(timeline: list[dict], grid: dict,
                         span_index: dict[str, dict], deficit: float,
                         guards: list[tuple[float, float, str]],
                         edges: list[float],
                         taken: list[dict]) -> list[dict]:
    """예산 산술 벨트 — 모델(초안 재분석)이 초과분을 다 못 덜었을 때만 남은 만큼.

    2026-09-02 사용자 결정: 길이 초과 처리의 1차는 watch-trim 모델 컷(자연스러움
    우선), 이 함수는 마지막 벨트다(초과 발행 사고 방지 — 8/24 권리사 길이 한도 실사고).
    story.trim_to_budget 의 우선순위를 편집본 좌표로 옮겼다: 클립 **가장자리 span**
    만, importance 낮은 것부터(동점은 긴 것부터), climax/ending/payoff/마지막 클립
    제외, 클립의 마지막 남은 span 통삭제 금지. 추가 보호 둘 — ↪ 이어짐·쉼 짝의
    한쪽만 덜면 반토막이 되살아나므로 짝이 타임라인에 남는 span 은 후보에서 뺀다,
    그리고 모델 컷과 같은 guard 차감·스냅을 지난다(적용 기계가 하나라 좌표 규율 동일).
    deficit 을 채우면 멈춘다 — 마지막 한 개는 초과 삭감(overshoot ≤ span 하나)을
    허용한다(조금 더 짧은 것이 상한 초과 발행보다 낫다)."""
    span_t = {sp["id"]: (float(sp["t_in"]), float(sp["t_out"]))
              for sp in grid.get("span_candidates") or []}
    present = {sid for c in timeline for sid in (c.get("span_ids") or [])}
    cands: list[tuple[int, float, float, float, float, str]] = []
    off = 0.0
    n = len(timeline)
    for i, c in enumerate(timeline):
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        dur = e - s
        ids = [sid for sid in (c.get("span_ids") or []) if sid in span_t]
        if c.get("role") in ("climax", "ending", "payoff") or i == n - 1 \
                or len(ids) <= 1:
            off += dur
            continue
        for sid in {ids[0], ids[-1]}:
            sp = span_index.get(sid) or {}
            imp = int(sp.get("importance") or 3)
            if sp.get("is_audio") and imp >= 4:
                continue                     # 핵심 대사 — guard 와 같은 선
            mates = {sp.get("continues_to"), sp.get("continues_from"),
                     sp.get("pause_cont_from")} - {None, sid}
            if mates & present:
                continue                     # ↪ 짝 보호 — 반토막 재발 방지
            a = max(span_t[sid][0], s)
            z = min(span_t[sid][1], e)
            if z - a < MIN_CUT_SEC:
                continue
            cands.append((imp, -(z - a), off + (a - s), off + (z - s), a, sid))
        off += dur
    cands.sort()
    picked: list[dict] = []
    got = 0.0
    occupied = list(taken)
    for imp, _nd, ea, ez, _t, sid in cands:
        if got >= deficit - 0.05:
            break
        pieces, _hit = _carve_guards(ea, ez, guards)
        if not pieces:
            continue
        a, z = max(pieces, key=lambda p: p[1] - p[0])
        snapped = snap_inward(a, z, edges)
        if snapped is None:
            continue
        s2, e2 = snapped
        if any(c["start"] < e2 and c["end"] > s2 for c in occupied):
            continue
        cut = {"start": s2, "end": e2, "proposed": [round(a, 2), round(z, 2)],
               "reason": f"예산 벨트({sid} imp{imp})", "belt": True}
        picked.append(cut)
        occupied.append(cut)
        got += e2 - s2
    return picked


# ── 적용 (순수) ─────────────────────────────────────────────────────────────

def remap_edited(t: float, cuts: list[dict]) -> float:
    """구 편집본 초 → 신 편집본 초. 제거 구간 안이면 그 구간 시작으로 붙는다."""
    new = t
    for c in cuts:
        if t >= c["end"]:
            new -= c["end"] - c["start"]
        elif t > c["start"]:
            new -= t - c["start"]
    return round(new, 3)


def apply_cuts_to_timeline(timeline: list[dict], cuts: list[dict], grid: dict,
                           label_anchors: set[str]) -> list[dict]:
    """편집본 좌표 제거 구간 → 새 timeline. 컷이 span 경계라 조각 경계도 span 경계다.

    subtitle(라벨 문구)은 앵커 span 을 담은 조각만 유지 — 앵커가 잘렸으면 문구도
    버린다(남의 조각에 라벨이 붙으면 안 된다)."""
    span_t = {sp["id"]: (float(sp["t_in"]), float(sp["t_out"]))
              for sp in grid.get("span_candidates") or []}
    out: list[dict] = []
    off = 0.0
    for c in timeline:
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        dur = e - s
        # 이 클립 위 제거 구간(소스 좌표로 환산)
        holes = []
        for cut in cuts:
            a = max(cut["start"], off)
            z = min(cut["end"], off + dur)
            if z - a > 0.01:
                holes.append((s + (a - off), s + (z - off)))
        if not holes:
            out.append(dict(c))
            off += dur
            continue
        pieces: list[tuple[float, float]] = []
        cur = s
        for a, z in sorted(holes):
            if a - cur > 0.05:
                pieces.append((cur, a))
            cur = max(cur, z)
        if e - cur > 0.05:
            pieces.append((cur, e))
        for pi, (a, z) in enumerate(pieces):
            ids = [sid for sid in c.get("span_ids") or []
                   if sid in span_t
                   and min(span_t[sid][1], z) - max(span_t[sid][0], a) > 0.05]
            piece = dict(c)
            piece["clip_start_sec"] = round(a, 3)
            piece["clip_end_sec"] = round(z, 3)
            piece["span_ids"] = ids
            if c.get("subtitle") and not (set(ids) & label_anchors):
                piece["subtitle"] = ""
            if pi > 0 or abs(a - s) > 0.001:
                piece.pop("head_trimmed", None)   # 시작이 바뀐 조각엔 무효
            out.append(piece)
        off += dur
    return out


def remap_segments(segments: list[dict], cuts: list[dict],
                   new_total: float) -> list[dict]:
    """자막(편집본 좌표)을 새 타임라인으로 재매핑 — 제거 구간 안은 드랍.

    재생성 대신 재매핑을 쓰는 이유: 리소스 단계의 후처리(반복 환각 필터·인명
    교정)가 이미 얹혀 있어 다시 만들면 그게 날아간다."""
    out = []
    for sg in segments:
        a, z = float(sg["start_sec"]), float(sg["end_sec"])
        kept = z - a - sum(max(0.0, min(z, c["end"]) - max(a, c["start"]))
                           for c in cuts)
        if kept < 0.15:
            continue
        row = dict(sg)
        row["start_sec"] = min(remap_edited(a, cuts), new_total)
        row["end_sec"] = min(remap_edited(z, cuts), new_total)
        if row["end_sec"] - row["start_sec"] >= 0.15:
            out.append(row)
    return out


def remap_resources(resources: dict, cuts: list[dict],
                    new_total: float) -> dict:
    """cue 창(편집본 좌표) 재매핑 — 창 자체는 보호돼 있어 이동만 한다.
    합성 산물(path·fit_actual·text)은 그대로(재합성 요금 0)."""
    res = json.loads(json.dumps(resources, ensure_ascii=False))
    for f in res.get("tts_cue_files") or []:
        cue = f.get("cue") or {}
        if cue.get("start_sec") is None:
            continue
        cue["start_sec"] = remap_edited(float(cue["start_sec"]), cuts)
        cue["end_sec"] = min(remap_edited(float(cue["end_sec"]), cuts), new_total)
        cue["duration_sec"] = round(cue["end_sec"] - cue["start_sec"], 3)
    return res


# ── 프롬프트 · 실행 ─────────────────────────────────────────────────────────

PROMPT = """너는 쇼츠의 최종 검수 편집자다. 첨부 영상은 렌더된 **초안**이다 — 시각은 전부 이 영상 자체의 시계다(첫 프레임 = 0초).

과제: 시청자가 이탈할 만큼 **늘어지는 구간**이 있으면 0~{max_cuts}개 지목하라.
- 늘어짐 = 소리도 정보도 없는 대기 시간, 같은 화면의 무의미한 지속, 이야기에 기여 없는 반복.
- **여운과 구분하라**: 감정이 남는 정지(놀란 얼굴, 발견 직후)는 편집이다 — 자르지 마라.
- 자르면 안 되는 것: 내레이션이 흐르는 시간, 핵심 대사, 엔딩. (어차피 코드가 거부하지만, 지목 자체를 아껴라.)
- **확신이 없으면 지목하지 마라 — 빈 배열이 정답인 판이 많다.**
{budget_block}
참고 재료표(클립·대사·내레이션 창 — 판단은 네가 본 화면으로, 표는 좌표 확인용):
{material_block}

또 하나, 자르기와 별개로 **호흡 판정**을 적어라(참고 기록 — 이 판정으로 편집을 되돌리지는 않는다):
초안이 전체적으로 루즈한가, 시청자가 '지금 누구·무슨 상황'인지 놓칠 자리(내레이션이 있어야 할 자리)가 있는가.
출력(JSON 만):
{{"cuts": [{{"start_sec": 12.0, "end_sec": 14.5, "reason": "한 줄"}}],
 "pacing": {{"loose": false, "narration_missing_at": [31.5], "note": "한 줄"}}}}
"""

# 예산 컷 판(초과 발행 금지)에만 끼워 넣는 절 — 없으면 프롬프트는 종전과 동일하다.
BUDGET_BLOCK = """
⚠ 이 판은 **예산 컷**이 함께 걸려 있다: 초안이 길이 상한을 {deficit:.1f}초 넘겼다.
늘어짐이 안 보여도 **합계 {deficit:.1f}초 이상**을 반드시 지목하라 — 이야기 손상이
가장 적은 구간부터 골라라(위의 자르면 안 되는 것 목록은 그대로다). 네가 모자라게
지목하면 코드가 중요도 숫자만 보고 마저 자른다 — 화면을 본 네 선택이 더 낫다.
이 판에서는 빈 배열이 정답이 아니다."""


def build_material_block(timeline: list[dict], segments: list[dict],
                         resources: dict) -> str:
    lines = []
    off = 0.0
    for i, c in enumerate(timeline):
        dur = float(c["clip_end_sec"]) - float(c["clip_start_sec"])
        lines.append(f"클립{i:02d} {off:.1f}~{off + dur:.1f}s [{c.get('role')}]")
        off += dur
    for sg in segments or []:
        lines.append(f"  대사 {float(sg['start_sec']):.1f}~"
                     f"{float(sg['end_sec']):.1f}s | {str(sg.get('text'))[:24]}")
    for f in resources.get("tts_cue_files") or []:
        cue = f.get("cue") or {}
        if cue.get("start_sec") is not None:
            lines.append(f"  내레이션 {float(cue['start_sec']):.1f}~"
                         f"{float(cue['end_sec']):.1f}s | {str(cue.get('text'))[:24]}")
    return "\n".join(lines)


def run_watch_trim(gemini, draft_path: Path, *, timeline: list[dict],
                   grid: dict, resources: dict, segments: list[dict],
                   importance: dict[str, tuple[bool, int]],
                   span_index: dict[str, dict] | None = None,
                   budget_deficit: float = 0.0,
                   log=print) -> tuple[list[dict], dict]:
    """초안 시청 → (적용 컷 목록[편집본 좌표], 감사 기록). fail-soft.

    budget_deficit > 0 이면 **예산 컷 판**: 프롬프트에 초과분을 싣고(빈 배열 금지),
    모델이 덜 지목하면 budget_fallback_cuts 산술 벨트가 남은 만큼을 마저 던다.
    호출 실패도 벨트는 돈다 — 상한 초과 발행이 트림 부재보다 나쁘다(8/24 실사고)."""
    duration = sum(float(c["clip_end_sec"]) - float(c["clip_start_sec"])
                   for c in timeline)
    audit: dict[str, Any] = {"duration": round(duration, 2),
                             "sample_fps": WATCH_SAMPLE_FPS}
    edges = edited_span_edges(timeline, grid)
    guards = protected_intervals(timeline, resources, importance, grid)

    def _belt(applied: list[dict]) -> list[dict]:
        got = sum(c["end"] - c["start"] for c in applied)
        remaining = budget_deficit - got
        if budget_deficit <= 0.1 or remaining <= 0.1:
            if budget_deficit > 0.1:
                audit["budget"] = {"deficit": round(budget_deficit, 2),
                                   "model_sec": round(got, 2),
                                   "belt_sec": 0.0, "unmet": False}
            return applied
        belt = budget_fallback_cuts(timeline, grid, span_index or {},
                                    remaining, guards, edges, applied)
        belt_sec = round(sum(c["end"] - c["start"] for c in belt), 2)
        unmet = got + belt_sec < budget_deficit - 0.1
        audit["budget"] = {"deficit": round(budget_deficit, 2),
                           "model_sec": round(got, 2),
                           "belt_sec": belt_sec, "belt": belt, "unmet": unmet}
        if belt:
            log(f"  [v3/watch-trim] 예산 벨트 — 모델이 {round(got, 1)}s/"
                f"{round(budget_deficit, 1)}s 만 덜어 산술로 {belt_sec}s 추가 컷")
        if unmet:
            log(f"  [v3/watch-trim] ⚠ 예산 미달성 — 벨트 후에도 "
                f"{round(budget_deficit - got - belt_sec, 1)}s 초과 남음(전부 보호 구간)")
        return sorted(applied + belt, key=lambda c: c["start"])

    is_budget = budget_deficit > 0.1
    max_cuts = MAX_CUTS_BUDGET if is_budget else MAX_CUTS
    prompt = PROMPT.format(max_cuts=max_cuts,
                           budget_block=(BUDGET_BLOCK.format(
                               deficit=budget_deficit) if is_budget else ""),
                           material_block=build_material_block(
                               timeline, segments, resources))
    t0 = time.time()
    try:
        from app.v3.seq_analyze import _upload_video
        types = gemini.types
        uploaded = _upload_video(gemini, draft_path, log=lambda *a: None)
        try:
            part = types.Part(
                file_data=types.FileData(file_uri=uploaded.uri,
                                         mime_type="video/mp4"),
                video_metadata=types.VideoMetadata(fps=WATCH_SAMPLE_FPS))
            response = gemini.client.models.generate_content(
                model=gemini.config.flash_model_name,
                contents=[part, prompt],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                    max_output_tokens=2048,
                ))
        finally:
            try:
                gemini.client.files.delete(name=uploaded.name)
            except Exception:  # noqa: BLE001
                pass
        parsed = json.loads(response.text or "{}")
        raw = parsed.get("cuts") if isinstance(parsed, dict) else None
        # 호흡 판정(2026-09-03, C-2층): 기록만 — 되돌리기는 몇 편 쌓인 뒤 결정
        pr = parsed.get("pacing") if isinstance(parsed, dict) else None
        if isinstance(pr, dict):
            audit["pacing_review"] = {
                "loose": bool(pr.get("loose")),
                "narration_missing_at": [round(float(x), 2) for x in (pr.get("narration_missing_at") or [])
                                         if isinstance(x, (int, float))][:10],
                "note": str(pr.get("note") or "")[:200]}
    except Exception as e:  # noqa: BLE001 — 관측 실패가 본편 발행을 막지 않는다
        audit["error"] = str(e)[:200]
        log(f"  [v3/watch-trim] ⚠ 호출 실패 — "
            + ("예산 벨트만 적용" if is_budget else "트림 없이 진행") + f": {e}")
        return _belt([]), audit
    audit["elapsed"] = round(time.time() - t0, 1)
    audit["proposed"] = raw if isinstance(raw, list) else []
    applied, rejected = validate_cuts(raw, duration=duration, edges=edges,
                                      guards=guards, max_cuts=max_cuts)
    audit["rejected"] = rejected
    applied = _belt(applied)
    audit["applied"] = applied
    return applied, audit
