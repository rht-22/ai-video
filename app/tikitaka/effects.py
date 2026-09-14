"""5.6단계 — 효과자막 정밀 배치·타이밍(2026-09-11 사용자 지시 "프레임 단위로 위치·시간 구간을 섬세하게").

타이밍: S 행은 단어 타임스탬프가 있으므로 Gemini(텍스트)에게 "이 자막이 반응하는 단어"를 고르게 하고 그 단어 **끝**(반응) 또는
**시작**(강조)에 맞춘다. A 행은 동작 시작, N 행은 문장 시작. 모든 시각은 1/30s 격자로 반올림(프레임 단위).
위치: 그 순간 화면의 주인물 얼굴(5.5 framing: x0/x1·y0/y1)을 피한다 — 얼굴이 왼쪽이면 오른쪽, 오른쪽이면 왼쪽, 가운데면 얼굴 위/아래
빈 쪽. 글자 폭(≈0.95em/글자)을 계산해 화면 안(여백 40px)에 들게 하고, 자막 영역(밴드 하단 22%)은 피한다.
산출: row["effect_plan"] = {start, end, x, y, anchor, why} · 캐시 `effects_v{n}.json`.
"""
from __future__ import annotations

import json

from app.tikitaka.common import Job, ms3
from app.tikitaka.llm import Gemini
from app.tikitaka.framing import frame_at

FPS = 30
HOLD_SEC = 1.6            # 효과자막 기본 유지
MIN_SHOW = 1.0            # 어떤 효과자막도 이보다 짧게 보이면 안 된다(2026-09-11 실측: 겹침 해소가 앞 자막을 0.55s 로 잘랐다)
SAME_SCENE_GAP = 45.0     # 다음 행 첫 컷이 이 행 마지막 컷에서 이만큼 안이면 같은 장면 — 효과자막이 다음 행으로 넘어가도 된다
RULES_VERSION = 7         # 타이밍 규칙 판 — 캐시(effects_v{n}.json)의 _rules 가 다르면 버린다 (7: 앵커 = 단어 **시작**, 사용자 지적 "타임스탬프가 느리다")
                          # (5: 배치 기하를 렌더와 동일하게 — 로고·카피 반영 / 6: 자리가 MIN_DROP 미만이면 그 자막을 생략하고 나머지를 다시 편다)
MIN_DROP = 0.8            # 겹침을 풀고도 이보다 짧게만 보일 자막은 생략한다 — 0.3s 깜빡임보다 없는 편이 낫다(2026-09-11 실측 2화 v6 [현장적발] 0.33s)
                          # (3: 캐시 키에 행 시작 t0 · 대사 자막 영역 회피 / 4: 다음 행이 대사면 넘어가지 않음 · 반응 자막은 단어 끝 −0.15s)
NEXT_LINE_GRACE = 0.3     # 다음 행이 새 대사(S)면 그 위로는 이만큼만 넘어간다 — 다른 대사 위에 앞 대사의 반응 자막이 남으면 내용이 어긋난다

TIMING_PROMPT = """너는 예능형 효과자막 타이밍 편집자다. 아래 각 항목은 쇼츠의 대사 한 줄(단어마다 [번호] 와 시각)과 그 줄에 붙일 효과자막이다.
효과자막이 **터져야 하는 순간의 단어**를 골라라 — 자막은 그 단어가 **입에서 나오는 순간(시작)** 에 뜬다. 반응·태클형("(동공지진)", "[팩트폭행]")은
그 반응을 일으키는 핵심 구절의 **첫 단어**(펀치라인이 시작되는 지점), 강조형("[선전포고]")은 강조할 말의 첫 단어. 대사 전체를 요약하는
자막이면 마지막 문장(펀치라인)의 첫 단어. 확신 없으면 마지막 문장의 첫 단어. 단어가 끝난 뒤에 뜨면 늦어 보인다.

{items}

출력 JSON 하나(코드블록 금지): {{"plans": [{{"i": 행번호, "word": 단어번호, "why": "≤20자"}}, …]}}
"""


def q(t: float) -> float:
    """1/30s 격자 반올림."""
    return ms3(round(t * FPS) / FPS)


def place(face_x: float, face_y: float, *, text: str, size: int, band_y: int, band_h: int, W: int = 1080,
          sub_top: int | None = None) -> tuple[int, int]:
    """얼굴을 피하는 효과자막 중심 좌표(캔버스 px). 순수 — 테스트 대상.
    가로: 얼굴이 왼쪽(<0.45)이면 오른쪽 열, 오른쪽(>0.55)이면 왼쪽 열, 가운데면 가운데(세로로 피한다).
    세로: 기본 밴드 상단 26%; 얼굴이 가운데면 얼굴 아래 62%(얼굴이 낮으면 위 22%). sub_top(대사 자막 블록 윗변)이 주어지면
    그 위로 올린다 — 안전 영역 레이아웃(밴드 ≈950px)에서 62% 가 대사 자막과 겹쳤다(2026-09-11 실측)."""
    text_w = int(len(text.replace(" ", "")) * size * 0.95) + 40
    margin = 40
    half = text_w / 2
    if face_x < 0.45:
        cx = W * 0.72
    elif face_x > 0.55:
        cx = W * 0.28
    else:
        cx = W * 0.5
    cx = max(margin + half, min(W - margin - half, cx))
    # 얼굴이 가운데 열이면 위·아래로 피한다: 얼굴이 낮게(>0.6) 있으면 위 22%, 아니면 얼굴 아래 62%(클로즈업은 얼굴이 상단~중앙을
    # 다 차지하므로 26% 는 이마를 덮는다 — 2026-09-11 실측). 옆 열이면 상단 26%.
    if 0.35 <= face_x <= 0.65:
        cy = band_y + band_h * (0.22 if face_y > 0.6 else 0.62)
    else:
        cy = band_y + band_h * 0.26
    cy = min(cy, band_y + band_h * 0.72)
    if sub_top is not None:
        cy = min(cy, sub_top - size * 0.8)                    # 효과자막 아래끝이 대사 블록 윗변 위에
    cy = max(cy, band_y + size * 0.8)
    return int(cx), int(cy)


def fit_span(start: float, *, earliest: float, latest: float, hold: float = HOLD_SEC, min_show: float = MIN_SHOW) -> tuple[float, float]:
    """[start, start+hold] 를 [earliest, latest] 창 안에 넣되 min_show 를 지킨다 — 창 끝이 가까우면 시작을 앞당긴다(순수 — 테스트 대상).
    예: 대사 마지막 단어 끝(=행 끝)에 뜨는 반응 자막인데 다음 행이 다른 장면이면 행 안에서 1.0s 는 보이게 앞당긴다."""
    start = min(start, max(earliest, latest - min_show))
    end = min(start + hold, latest)
    if end - start < min_show:
        start = max(earliest, end - min_show)
    return q(start), q(end)


def resolve_overlaps(plans: list[dict], *, min_show: float = MIN_SHOW, hold: float = HOLD_SEC) -> None:
    """같은 시간에 두 효과자막이 겹치지 않게(순수 · 제자리 갱신 — 테스트 대상). 각 plan 의 earliest/latest(없으면 start/end)가 허용 창.
    시작순으로 보며 겹치면 **뒤 자막을 앞 자막이 끝난 뒤로 미룬다**(뒤 자막 창에 min_show 가 남을 때). 미룰 자리가 없으면 앞 자막을
    뒤 자막 시작 전에 끊되 min_show 미만이 되면 앞 자막 시작을 앞당긴다. 2026-09-11 실측: 종전 규칙은 앞 자막이 0.6s 만 보이면
    그냥 잘라서 [극대노]·[팩트폭행]·[태세전환]이 0.55s 로 깜빡였다."""
    for _ in range(12):
        order = sorted(range(len(plans)), key=lambda i: plans[i]["start"])
        changed = False
        for a, b in zip(order, order[1:]):
            pa, pb = plans[a], plans[b]
            if pb["start"] >= pa["end"] - 1e-6:
                continue
            b_latest = pb.get("latest", pb["end"])
            shift = q(pa["end"] + 0.05)
            if b_latest - shift >= min_show:
                pb["start"], pb["end"] = shift, q(min(shift + hold, b_latest))
            else:
                pa["end"] = q(pb["start"] - 0.05)
                if pa["end"] - pa["start"] < min_show:
                    pa["start"] = q(max(pa.get("earliest", pa["start"]), pa["end"] - min_show))
                if pa["end"] - pa["start"] < 0.3:                      # 그래도 안 들어가면 최소 0.3s 는 남긴다
                    pa["end"] = q(pa["start"] + 0.3)
                    pb["start"] = q(max(pb["start"], pa["end"] + 0.05))
                    pb["end"] = q(max(pb["end"], pb["start"] + 0.3))
            changed = True
        if not changed:
            return


def _active_cut(row: dict, t_abs_in_row: float) -> tuple[dict | None, float]:
    """행 안 상대 시각 → (그때 화면에 있는 컷, 컷 상대 시각)."""
    t = 0.0
    for c in row["cuts"]:
        if t <= t_abs_in_row < t + c["dur"] + 1e-6:
            return c, t_abs_in_row - t
        t += c["dur"]
    if row["cuts"]:
        return row["cuts"][-1], row["cuts"][-1]["dur"]
    return None, 0.0


def _latest_end(rows: list[dict], idx: int, e_row: float) -> float:
    """효과자막이 머물 수 있는 마지막 시각. 다음 행이 **같은 장면의 리액션/상황 컷(N·A)** 이면 HOLD 만큼 넘어가도 된다(그 컷이 곧 반응 샷).
    다음 행이 새 대사(S)면 NEXT_LINE_GRACE 만 — 다른 대사 위에 앞 대사의 자막이 남으면 내용이 어긋난다. 다른 장면이면 이 행 끝."""
    r = rows[idx]
    nxt = rows[idx + 1] if idx + 1 < len(rows) else None
    if not (nxt and nxt.get("cuts") and r.get("cuts")):
        return e_row
    same_scene = abs(nxt["cuts"][0]["in"] - r["cuts"][-1]["out"]) <= SAME_SCENE_GAP
    if nxt["mode"] == "S":
        return e_row + (NEXT_LINE_GRACE if same_scene else 0.0)
    return e_row + (HOLD_SEC if same_scene else 0.0)


def plan_effects(job: Job, gemini: Gemini, table: dict, transcript: dict, layout: dict, *, tag: str = "") -> int:
    n = table["version"]["n"]
    sfx = f"_{tag}" if tag else ""
    name = f"effects_v{n}{sfx}.json"
    words = transcript["words"]
    lines_by_id = {l["id"]: l for l in transcript["lines"]}
    rows = [r for r in table["rows"] if r.get("effect") and r.get("cuts")]      # 컷 0개 행(화면 없음)은 효과자막을 못 얹는다
    for r in table["rows"]:
        if r.get("effect") and not r.get("cuts"):
            job.log(f"[effects] ⚠ 행{r['i']} 컷 0개 — 효과자막 {r['effect']} 생략")
    if not rows:
        return 0
    cache = job.load(name) if job.has(name) else {}
    if cache.get("_rules") != RULES_VERSION:                                      # 타이밍 규칙이 바뀌면 옛 배치를 버린다
        cache = {}
    cache.pop("_rules", None)
    # 캐시 키에 행 시작 t0 를 넣는다 — 앞 행의 TTS 길이가 바뀌면(문구 교정 등) 뒤 행의 출력 시각이 통째로 밀리는데, 종전 키(행·문구·소스 in)는
    # 그걸 몰라 옛 절대 시각을 재사용했다(2026-09-11 실측: 1화 v3 배우 표기 재렌더 뒤 행8~10 효과자막이 +1.4s 늦게 떴다)
    key_of = lambda r: f"{r['i']}|{r['effect']}|{r['cuts'][0]['in']:.3f}|{r['t0']:.3f}"  # noqa: E731
    # ── 타이밍(S 행만 모델에게) ──────────────────────────────────────────
    s_rows = [r for r in rows if r["mode"] == "S" and key_of(r) not in cache]
    timing: dict[int, dict] = {}
    if s_rows:
        items = []
        for r in s_rows:
            wi = [i for lid in r["src"] for i in lines_by_id[lid]["word_i"]]
            wl = " ".join(f"[{k}]{words[i]['text']}({words[i]['start']:.2f}~{words[i]['end']:.2f})" for k, i in enumerate(wi, 1))
            items.append(f"행 {r['i']} · 효과자막 {r['effect']!r} · 화자 {r.get('speaker','')}\n  {wl}")
        try:
            raw = gemini.text_json(TIMING_PROMPT.format(items="\n".join(items)), kind="effects", thinking="low", max_output_tokens=4096)
            for p in raw.get("plans") or []:
                try:
                    timing[int(p["i"])] = {"word": int(p.get("word") or 0), "why": str(p.get("why") or "")[:30]}
                except (TypeError, ValueError, KeyError):
                    continue
        except Exception as e:  # noqa: BLE001 — 타이밍 판정 실패면 종전 규칙(마지막 줄 시작)으로
            job.log(f"[effects] ⚠ 타이밍 판정 실패 → 기본 규칙: {type(e).__name__}: {str(e)[:120]}")
    # ── 행별 plan ──────────────────────────────────────────────────────
    all_rows = table["rows"]
    for r in rows:
        k = key_of(r)
        if k in cache:
            r["effect_plan"] = cache[k]                          # None 이면 '생략'도 캐시된 결정이다
            continue
        s = r["t0"]
        row_dur = r["dur_video"] if r["mode"] == "N" else r["dur"]
        e_row = s + row_dur
        latest = _latest_end(all_rows, all_rows.index(r), e_row)
        base = r["cuts"][0]["in"]
        anchor = "행 시작"
        why = ""
        if r["mode"] == "S":
            wi = [i for lid in r["src"] for i in lines_by_id[lid]["word_i"]]
            tp = timing.get(r["i"])
            if tp and 1 <= tp["word"] <= len(wi):
                w = words[wi[tp["word"] - 1]]
                t_rel = w["start"] - base                                   # 단어가 입에서 나오는 순간 — 끝에 띄우면 늦어 보인다
                anchor = f"단어 {tp['word']} '{w['text']}' start"
                why = tp["why"]
            else:
                subs = r.get("sub_lines") or []
                t_rel = (subs[-1]["start"] - base) if len(subs) >= 2 else 0.0   # 판정 없으면 펀치라인(마지막 줄) 시작
                anchor = "마지막 줄 시작"
            start = q(s + max(0.0, t_rel))
        else:
            start = q(s)
        start, end = fit_span(start, earliest=s, latest=latest)   # 같은 장면이면 행이 끝나도 유지, 다른 장면이면 행 안에서 1.0s 보장
        c, t_in_cut = _active_cut(r, start - s)
        fx, fy = frame_at(c or {}, t_in_cut)                  # 그 시각의 샷 구간 얼굴(컷이 샷 경계를 가로질러도 맞는 앵글)
        x, y = place(fx, fy, text=r["effect"], size=layout["effect_size"], band_y=layout["band_y"], band_h=layout["band_h"],
                     sub_top=layout.get("sub_top"))
        plan = {"start": start, "end": end, "x": x, "y": y, "anchor": anchor, "why": why, "face": [round(fx, 3), round(fy, 3)],
                "earliest": q(s), "latest": q(latest)}
        r["effect_plan"] = plan
        cache[k] = plan
    rows = [r for r in rows if r.get("effect_plan") is not None]        # 캐시된 '생략'은 다시 안 편다
    plans = [r["effect_plan"] for r in rows]
    orig = [(p["start"], p["end"]) for p in plans]
    resolve_overlaps(plans)
    dropped = [r for r, p in zip(rows, plans) if p["end"] - p["start"] < MIN_DROP - 1e-6]
    if dropped:                                                    # 자리가 없는 자막은 빼고, 남은 자막은 원래 창으로 되돌려 다시 편다
        for r in dropped:
            job.log(f"[effects] 행{r['i']} {r['effect']} 생략 — 겹침을 풀어도 {r['effect_plan']['end'] - r['effect_plan']['start']:.2f}s 만 보인다(다음 행 자막과 자리 경쟁)")
            r["effect_plan"] = None
        keep = [(r, p, o) for r, p, o in zip(rows, plans, orig) if r["effect_plan"] is not None]
        for _, p, o in keep:
            p["start"], p["end"] = o
        resolve_overlaps([p for _, p, _ in keep])
    for r in rows:
        cache[key_of(r)] = r["effect_plan"]
    job.save(name, dict(cache, _rules=RULES_VERSION))
    for r in rows:
        p = r["effect_plan"]
        if p:
            job.log(f"[effects] 행{r['i']} {r['effect']} @ {p['start']:.2f}~{p['end']:.2f}s pos({p['x']},{p['y']}) — {p['anchor']} {p['why']}")
    job.record_step(f"effects_v{n}{sfx}", planned=len(rows), shown=sum(1 for r in rows if r.get("effect_plan")), timed_by_model=len(timing), rules=RULES_VERSION)
    return sum(1 for r in rows if r.get("effect_plan"))
