"""걸음 4 — 내레이션 쓰기 + 화면 지정 + 즉시 합성(길이 확정).

레퍼런스 실측(2026-09-03, LLM 제작 쇼츠 3편 · work/nar_ref): 내레이션 한 줄 =
공백 제외 10~14자 · 2.0~2.7초 · 4.4~7.0자/초. 쉼표로 끊고 다음 대사가 받는 형식이
다수. 그래서 한 문장 상한을 공백 제외 16자로 두고, 넘으면 문장부호·쉼표에서 나눈다.

**형식(2026-09-03 사용자 결정)**:
  · 내레이션마다 **밑에 깔 화면을 조각 id 로 지정**한다(`cover`). 재료는 고른 씬에서
    대사로 안 쓴 조각(scene_script 있음)뿐 — 기록에 없는 화면은 지정할 수 없으므로
    "화면에 없는 말"이 형식상 막힌다(실사고: 발장난 문장 밑에 깨진 잔 수습 장면).
  · 점프 자리(걸음 3 이 만든 비트 경계 — 긴 구멍·5초+ 간격)는 **필수 빈칸**이다.
    다리를 놓을지 판단하는 게 아니라 빈칸을 채우는 일이 된다.
**문장은 여기서 확정되고 이후 절대 줄지 않는다.** 합성 길이가 정본이고 덮개가 그
길이에 맞춰진다. 축약·배속 사다리·창 트림은 이 체인에 없다.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from app.v3.story_flow.common import fmt_t, nospace_len, reject_block, span_text

NAR_MAX_CHARS = 16           # 한 문장 공백 제외 상한(권장) — 넘으면 나눈다
NAR_HARD_MAX_CHARS = 24      # 나눠도 이보다 길면 반려
NAR_VOICE = "ko_female"
NAR_SPEED = "fast"           # ElevenLabs 1.1 (2026-09-03 사용자: very_fast 1.2 실측
                             # 7.4자/초 → 참고 쇼츠 평균 5~6자/초 쪽으로 한 단계 내림)
NAR_EST_CPS = 7.7            # 합성 실패 시 견적(공백 제외 자/초 · fast)
NAR_EST_LEAD_SEC = 0.35
MAX_NARRATIONS = 8

PROMPT = """당신은 리캡 쇼츠 구성작가다. 영상은 볼 수 없다 — 아래 편성 기록이 정본이다.

## 4단계 — 내레이션 쓰기
쇼츠는 사건 하나만 보여주므로 (a) 첫 장면 앞에 **이전 맥락** 한 줄이 필요하고, (b) 편성이 원본을 건너뛴 자리(아래 ⚠ 점프)는 시청자가 다음 장면·다음 대사를 따라가도록 **반드시** 잇는 말이 필요하다. 그 밖의 비트 앞에는 대사가 스스로 이어주면 넣지 마라. 대사 한 줄 → 내레이션 → 대사 한 줄도 된다.
규칙:
- 한 문장 = **공백 제외 {max_chars}자 이내**(2~3초). 길면 두 문장으로 나눠 따로 적어라. 쉼표로 끝내 다음 대사가 받게 하는 형식("협박까지 나왔는데,")이 좋다.
- 서술체(~했죠 / ~는데,). 예고형이 완료 묘사형보다 낫다. **다음 대사의 내용을 먼저 말하지 마라** — 상황만 깔고 대사가 답하게.
- 근거는 기록뿐: 구체 명사·인물명을 기록 그대로 써라. 화면에 없는 행동을 지어내지 마라.
- `cover`: 이 문장이 흐르는 동안 **보여줄 화면**을 아래 「쓸 수 있는 화면」에서 조각 id 로 골라라(1~3개, 이어지는 조각). 각 조각에 적힌 "before_beat k 용 / after_last 용" 자리에서만 쓸 수 있다 — 그 비트보다 뒤 시각의 화면을 앞에 깔면 완성본에서 시간이 되감긴다. 코드가 그 화면의 소리를 끄고 그 위에 얹는다. **문장은 그 화면이 보여주는 것을 말해야 한다** — 발장난을 말하려면 발장난 조각을 짚어라. 대사 중인 얼굴이라도 내레이션이 가리키는 장면이면 괜찮다. 짚을 화면이 없는 말은 쓰지 마라.
- 훅(before_beat: 0)은 **필수**. ⚠ 점프 자리도 **필수**. 엔딩 뒤 한 줄(after_last)은 선택 — 다음에 벌어질 일의 암시·떡밥(작품 정보·다른 씬 요약에 있는 사건은 화면 없이 말로 예고할 수 있다). 해소·정리 멘트 금지.
- 최대 {max_n}곳.

## 작품 · 주제 · 제목
{work_title}{research_block}
주제: {topic}
제목: {title_line1} / {title_line2}

## 편성 (비트 순 · 대사와 화면 기록)
{beats_block}

## 쓸 수 있는 화면 (대사로 안 쓴 조각 — cover 는 여기서 고른다 · id | 시각 | 길이 | 화면)
{available_block}
{tone_block}{reject_block}
## 출력 (JSON 만)
{{"narrations": [
  {{"before_beat": 0, "text": "…", "cover": ["sp0101", "sp0102"]}},
  {{"before_beat": 3, "text": "…", "cover": ["sp0140"]}},
  {{"after_last": true, "text": "…", "cover": ["sp0190"]}}
]}}"""


def beats_block(beats: list[dict], span_index: dict[str, dict],
                rows_by_idx: dict[int, dict], jumps: list[dict] | None = None) -> str:
    jump_at = {j["before_beat"]: j for j in (jumps or [])}
    out: list[str] = []
    for i, b in enumerate(beats):
        j = jump_at.get(i)
        if j is not None:
            skipped = " / ".join(j.get("skipped_text") or [])
            out.append(f"\n⚠ 점프: 비트 [{i - 1}] → [{i}] 사이 원본 {j['gap_sec']:.0f}초 건너뜀"
                       + (f" · 건너뛴 대사: {skipped}" if skipped else "")
                       + f" → **before_beat: {i} 내레이션 필수**")
        r = rows_by_idx.get(b["scene"], {})
        out.append(f"\n[{i}] {b['role']} · 씬 m{b['scene']:03d} {r.get('content', '')}"
                   + (f" · {b['action']}" if b.get("action") else ""))
        for sid in b["span_ids"]:
            sp = span_index[sid]
            if sp["is_audio"]:
                out.append(f"   대사 {sid}: {span_text(sp)}")
            else:
                out.append(f"   화면 {sid}: {sp.get('scene_script', '')}")
    return "\n".join(out)


def _beat_ranges(beats: list[dict], span_index: dict[str, dict]) -> list[tuple[float, float]]:
    out = []
    for b in beats:
        ids = [x for x in b.get("span_ids") or [] if x in span_index]
        if ids:
            out.append((span_index[ids[0]]["t_in"], span_index[ids[-1]]["t_out"]))
    return out


def available_covers(beats: list[dict], scene_rows: dict[int, dict],
                     span_index: dict[str, dict]) -> list[str]:
    """고른 씬의 분석된 조각 중 어느 비트 **구간 밖**인 것 — grid 순.

    비트 구간 안의 조각(skip 으로 뺀 추임새·구멍)은 제외한다(2026-09-03 실사고: 뺀
    조각을 덮개로 쓰니 완성본에서 시간이 되감겼다 — 43:12 화면 뒤에 43:04 대사)."""
    ranges = _beat_ranges(beats, span_index)
    out = []
    for r in scene_rows.values():
        for sid in r["span_ids"]:
            sp = span_index.get(sid)
            if sp is None or sp.get("unanalyzed"):
                continue
            if any(a - 1e-6 <= sp["t_in"] and sp["t_out"] <= z + 1e-6 for a, z in ranges):
                continue
            out.append(sid)
    return sorted(set(out), key=lambda s: span_index[s]["pos"])


def anchor_label(sid: str, beats: list[dict], span_index: dict[str, dict]) -> str:
    """이 조각을 쓸 수 있는 자리 — 첫 비트보다 앞이면 before_beat 0, 비트 k-1 과 k 사이면
    before_beat k, 마지막 비트 뒤면 after_last."""
    t = span_index[sid]["t_in"]
    ranges = _beat_ranges(beats, span_index)
    for k, (a, _z) in enumerate(ranges):
        if t < a:
            return f"before_beat {k}"
    return "after_last"


def cover_allowed(sid: str, anchor: tuple[str, int], beats: list[dict],
                  span_index: dict[str, dict]) -> bool:
    """before k → 비트 k 첫 조각보다 앞 시각(그리고 비트 k-1 뒤) · after → 마지막 비트 뒤.
    시간을 되감는 덮개를 형식으로 막는다."""
    kind, k = anchor
    ranges = _beat_ranges(beats, span_index)
    if not ranges:
        return True
    sp = span_index[sid]
    if kind == "after":
        return sp["t_in"] >= ranges[-1][1] - 1e-6
    hi = ranges[min(k, len(ranges) - 1)][0]
    lo = ranges[k - 1][1] if k > 0 else float("-inf")
    return sp["t_out"] <= hi + 1e-6 and sp["t_in"] >= lo - 1e-6


def filter_covers_by_anchor(groups: list[dict], beats: list[dict],
                            span_index: dict[str, dict], log=print) -> None:
    for g in groups:
        ok = [x for x in g.get("cover_ids") or [] if cover_allowed(x, g["anchor"], beats, span_index)]
        bad = [x for x in g.get("cover_ids") or [] if x not in ok]
        if bad:
            log(f"  [v3/flow/narration] · {g['anchor'][0]}{g['anchor'][1]} cover {bad} 는 그 자리와 "
                "시각이 어긋나 무시(되감기 방지)")
        g["cover_ids"] = ok


def available_block(available: list[str], span_index: dict[str, dict],
                    scene_of: dict[str, int], beats: list[dict] | None = None) -> str:
    out: list[str] = []
    cur_scene = None
    for sid in available:
        sp = span_index[sid]
        sc = scene_of.get(sid)
        if sc != cur_scene:
            out.append(f"\n### 씬 m{sc:03d}" if sc is not None else "\n### (씬 미상)")
            cur_scene = sc
        desc = sp.get("scene_script") or ""
        if sp["is_audio"]:
            desc = (desc + f" (대사: {span_text(sp)[:30]})").strip()
        slot = f" | {anchor_label(sid, beats, span_index)} 용" if beats else ""
        out.append(f"{sid} | {fmt_t(sp['t_in'])} | {sp['t_out'] - sp['t_in']:.1f}s{slot} | {desc}")
    return "\n".join(out) if out else "(없음 — 고른 씬의 모든 조각이 대사로 쓰였다)"


_SENT_END = re.compile(r"(?<=[.!?…])\s+")


def split_sentences(text: str, max_chars: int = NAR_MAX_CHARS) -> list[str]:
    """긴 문장을 문장부호 → 쉼표 순으로 나눈다(공백 제외 max_chars 기준). 순수."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    pieces = [p.strip() for p in _SENT_END.split(text) if p.strip()]
    out: list[str] = []
    for p in pieces:
        if nospace_len(p) <= max_chars:
            out.append(p)
            continue
        cur = ""
        for part in [x.strip() for x in re.split(r"(?<=,)\s*", p) if x.strip()]:
            cand = f"{cur} {part}".strip() if cur else part
            if cur and nospace_len(cand) > max_chars:
                out.append(cur)
                cur = part
            else:
                cur = cand
        if cur:
            out.append(cur)
    return out


def validate_narrations(resp: Any, n_beats: int, *,
                        max_chars: int = NAR_MAX_CHARS,
                        hard_max: int = NAR_HARD_MAX_CHARS,
                        required: set[int] | None = None,
                        available: set[str] | None = None,
                        ) -> tuple[list[dict] | None, list[str], list[str]]:
    """→ [{anchor: ("before", k) | ("after", n-1), lines: [문장…], cover_ids: [...]}].
    required: 내레이션이 반드시 있어야 하는 before_beat 집합(기본 {0}).
    available: cover 로 고를 수 있는 id 집합(None 이면 검사 안 함)."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []
    required = {0} if required is None else set(required)
    problems: list[str] = []
    notes: list[str] = []
    groups: dict[tuple[str, int], dict] = {}
    order: list[tuple[str, int]] = []
    for k, n in enumerate(resp.get("narrations") or []):
        if not isinstance(n, dict):
            continue
        text = " ".join(str(n.get("text") or "").split())
        if not text:
            continue
        if n.get("after_last"):
            key = ("after", max(0, n_beats - 1))
        else:
            try:
                bi = int(n.get("before_beat"))
            except (TypeError, ValueError):
                problems.append(f"narrations[{k}] before_beat 가 정수가 아니다")
                continue
            if not 0 <= bi < n_beats:
                problems.append(f"narrations[{k}] before_beat {bi} 는 0~{n_beats - 1} 밖")
                continue
            key = ("before", bi)
        lines = split_sentences(text, max_chars)
        for ln in lines:
            if nospace_len(ln) > hard_max:
                problems.append(f"narrations[{k}] 문장이 공백 제외 {nospace_len(ln)}자 — "
                                f"{max_chars}자 이내 문장 둘로 나눠 적어라: {ln!r}")
        if len(lines) > 1:
            notes.append(f"narrations[{k}] 긴 문장 → {len(lines)}줄로 분할")
        cover_in = [str(x) for x in (n.get("cover") or []) if isinstance(x, str)]
        cover = cover_in
        if available is not None:
            cover = [x for x in cover_in if x in available]
            if len(cover) != len(cover_in):
                notes.append(f"narrations[{k}] 쓸 수 없는 cover {len(cover_in) - len(cover)}개 무시")
            if cover_in and not cover:
                problems.append(f"narrations[{k}] cover {cover_in[:3]} 가 「쓸 수 있는 화면」에 없다 — "
                                "그 목록의 id 로 다시 골라라")
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"anchor": key, "lines": [], "cover_ids": [],
                               "refers_to": str(n.get("refers_to") or "").strip()[:160]}
            order.append(key)
        g["lines"].extend(lines)
        for x in cover:
            if x not in g["cover_ids"]:
                g["cover_ids"].append(x)
    for bi in sorted(required):
        if ("before", bi) not in groups:
            problems.append(f"before_beat: {bi} 내레이션이 없다 — "
                            + ("도입(훅)은 필수" if bi == 0 else "점프 자리라 다리가 필수"))
    if len(order) > MAX_NARRATIONS:
        notes.append(f"내레이션 {len(order)}곳 → 앞 {MAX_NARRATIONS}곳만")
        order = order[:MAX_NARRATIONS]
    if problems:
        return None, problems, notes
    if not order:
        return None, ["내레이션이 하나도 없다"], notes
    return [groups[k] for k in order], [], notes


def default_synth(text: str, out_path: Path) -> float:
    """합성 → 실측 길이. tts.py 의 백엔드 선택(ElevenLabs/edge)을 그대로 탄다."""
    from app.modules.tts import get_audio_duration, synthesize_tts
    out_path.parent.mkdir(parents=True, exist_ok=True)
    synthesize_tts(text, out_path, voice=NAR_VOICE, speed=NAR_SPEED)
    return float(get_audio_duration(out_path))


def estimate_sec(text: str) -> float:
    return round(NAR_EST_LEAD_SEC + nospace_len(text) / NAR_EST_CPS, 3)


def synthesize_groups(groups: list[dict], out_dir: Path,
                      synth_fn: Callable[[str, Path], float] | None,
                      log=print) -> None:
    """각 줄을 합성해 measured_sec·audio_path 를 채운다(in place). 실패는 견적 폴백 +
    로그(조용한 실패 금지) — 그 줄은 resources 단계가 다시 합성한다."""
    fn = synth_fn or default_synth
    for gi, g in enumerate(groups):
        g["measured"] = []
        g["audio_paths"] = []
        for li, text in enumerate(g["lines"]):
            path = out_dir / f"nar_{gi:02d}_{li}.mp3"
            try:
                sec = fn(text, path)
                if not sec or sec <= 0:
                    raise ValueError("길이 0")
                g["measured"].append(round(float(sec), 3))
                g["audio_paths"].append(str(path))
            except Exception as e:  # noqa: BLE001
                est = estimate_sec(text)
                log(f"  [v3/flow] ⚠ 내레이션 합성 실패({text[:12]}…): {e} — 견적 {est}s")
                g["measured"].append(est)
                g["audio_paths"].append(None)


__all__ = ["PROMPT", "NAR_MAX_CHARS", "NAR_SPEED", "NAR_VOICE", "beats_block",
           "available_covers", "available_block", "anchor_label", "cover_allowed",
           "filter_covers_by_anchor", "split_sentences",
           "validate_narrations", "synthesize_groups", "default_synth", "estimate_sec",
           "reject_block"]
