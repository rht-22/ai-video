"""내용 기반(content-aware) 길이 단축.

candidate_moment 분석 단계에서 생성된 `beats`(세부 구간)를 활용해, 스토리라인 합계가
target_max(보통 60초)를 넘으면 *덜 중요한 beat*를 골라 제거한다. 기존 `_fit_storyline_to_duration`
처럼 build를 통째 버리거나 시작/끝을 기계적으로 자르는 대신, 흐름을 끊지 않는 지점을 고른다.

- 제거 판단: Flash(`flash_drop_fn`)가 스토리 맥락으로 어떤 beat를 뺄지 선택. 실패/없으면 importance
  기반 결정적 폴백.
- 컷 지점: 중간 beat 제거 허용 → 한 clip이 2개 이상 contiguous StoryClip으로 분할(점프컷). 컷 경계는
  beat 경계를 그대로 쓴다 — beats 가 발화/의미 단위로 생성되어(analyze_chunk 프롬프트가 "발화 도중
  끊지 말 것"을 강제) 발화 중간이 아니므로, 별도 Whisper 스냅 없이도 대사가 잘리지 않는다.
- 보호: hook 첫 beat / payoff 마지막 beat / carries_payoff beat는 절대 제거 안 함.
- 하한: 누적 제거가 (현재합 - target_min)을 넘기면 중단(과도 단축 방지).

beats가 없거나(옛 캐시) 아무 drop도 채택 못 하면 ([], "")를 반환 → 호출부가 기존
`_fit_storyline_to_duration`로 폴백한다. 절대 예외를 던지지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable

from app.modules.silence_cutter import Interval
from app.modules.story_builder import StoryClip

_EPS = 1e-6
_IMPORTANCE_ORDER = {"droppable": 0, "supporting": 1, "core": 2}


def _kept_intervals_from_drops(
    clip_range: tuple[float, float],
    beats: list[dict],
    dropped_idxs: set[int],
    *,
    min_clip_dur: float = 3.0,
) -> list[Interval]:
    """clip 범위에서 제거 beat 구간을 빼고 남은 contiguous Interval 목록을 만든다.

    - kept = clip 범위 − (제거 beat 범위). 생존 beat의 union이 아니므로 beats가 구간을 다 못 덮어도
      그 틈은 유지된다.
    - 컷 경계는 beat 경계를 그대로 쓴다. beats 가 발화/의미 단위로 생성되므로(analyze_chunk 프롬프트가
      "발화 도중 끊지 말 것"을 강제) 발화 중간 컷이 생기지 않는다 → 별도 Whisper 스냅을 하지 않는다.
    - min_clip_dur 미만 조각은 폐기. 전부 폐기되면 가장 긴 조각 하나만 남긴다.
    """
    clip_start, clip_end = float(clip_range[0]), float(clip_range[1])
    if clip_end <= clip_start:
        return [Interval(clip_start, clip_end)]

    # 제거 beat 구간을 clip 범위로 클립
    drops: list[tuple[float, float]] = []
    for i in dropped_idxs:
        if i < 0 or i >= len(beats):
            continue
        b = beats[i]
        try:
            bs = max(clip_start, float(b["start_sec"]))
            be = min(clip_end, float(b["end_sec"]))
        except (KeyError, TypeError, ValueError):
            continue
        if be > bs:
            drops.append((bs, be))

    if not drops:
        return [Interval(clip_start, clip_end)]

    # 겹치는 제거 구간 병합
    drops.sort()
    merged: list[list[float]] = []
    for s, e in drops:
        if merged and s <= merged[-1][1] + _EPS:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # kept = clip 범위 − 병합된 제거 구간
    kept: list[list[float]] = []
    cursor = clip_start
    for s, e in merged:
        if s > cursor + _EPS:
            kept.append([cursor, s])
        cursor = max(cursor, e)
    if cursor < clip_end - _EPS:
        kept.append([cursor, clip_end])

    if not kept:
        # 모든 구간이 제거됨 → 호출부 보호(생존 beat ≥1 보장)와 함께 안전망: 통째 유지
        return [Interval(clip_start, clip_end)]

    # 컷 경계는 beat 경계를 그대로 사용 (Whisper 스냅 없음 — beats 가 발화 단위로 생성됨)
    candidates = [Interval(a, b) for a, b in kept]

    filtered = [iv for iv in candidates if (iv.end_sec - iv.start_sec) >= min_clip_dur - _EPS]
    if not filtered:
        longest = max(candidates, key=lambda iv: iv.end_sec - iv.start_sec)
        return [longest]
    return filtered


def _clip_beats(clip: StoryClip, beats: list[dict]) -> list[dict]:
    """candidate.beats를 clip 실제 [start,end]로 클립·정렬. 교집합 없는 beat는 제외."""
    out: list[dict] = []
    for b in beats:
        if not isinstance(b, dict):
            continue
        try:
            bs = max(clip.start_sec, float(b["start_sec"]))
            be = min(clip.end_sec, float(b["end_sec"]))
        except (KeyError, TypeError, ValueError):
            continue
        if be > bs:
            out.append({**b, "start_sec": bs, "end_sec": be})
    out.sort(key=lambda x: x["start_sec"])
    return out


def _protected_idxs(clip: StoryClip, beats: list[dict], is_only_clip: bool) -> set[int]:
    """제거 금지 beat 인덱스: carries_payoff / hook 첫 beat / payoff 마지막 beat / 단일 clip 양끝."""
    protected: set[int] = set()
    for i, b in enumerate(beats):
        if b.get("carries_payoff"):
            protected.add(i)
    if beats and (clip.role == "hook" or is_only_clip):
        protected.add(0)
    if beats and (clip.role == "payoff" or is_only_clip):
        protected.add(len(beats) - 1)
    return protected


def beat_trim_storyline(
    clips: list[StoryClip],
    candidates_lookup: dict,
    *,
    target_min: float,
    target_max: float,
    flash_drop_fn: Callable[[dict], dict] | None,
    min_clip_dur: float = 3.0,
) -> tuple[list[StoryClip], str]:
    """내용 기반 길이 단축. (new_clips, msg) 반환.

    beats 없음/실패/무-drop이면 ([], "") → 호출부가 기존 fit으로 폴백.
    """
    if not clips:
        return [], ""

    def total(cs: list[StoryClip]) -> float:
        return sum(c.end_sec - c.start_sec for c in cs)

    cur_total = total(clips)
    if cur_total <= target_max:
        return [], ""  # 초과 아님 → 확장은 기존 함수 담당

    # 각 clip의 beats 확보 (하나라도 없으면 전략 혼용 금지 → 폴백)
    per_clip_beats: list[list[dict]] = []
    for c in clips:
        cand = candidates_lookup.get((c.chunk_index, c.candidate_index)) if candidates_lookup else None
        raw = (cand or {}).get("beats") if isinstance(cand, dict) else None
        if not raw or not isinstance(raw, list):
            return [], ""
        cb = _clip_beats(c, raw)
        if not cb:
            return [], ""
        per_clip_beats.append(cb)

    is_only_clip = len(clips) == 1
    protected = [_protected_idxs(c, per_clip_beats[i], is_only_clip) for i, c in enumerate(clips)]

    must_remove = cur_total - target_max

    # 제거 후보 순서 결정 — Flash 우선, 실패 시 importance 기반 결정적 정렬
    ordered_drops: list[tuple[int, int]] = []
    if flash_drop_fn is not None:
        payload = {
            "target_max_sec": round(target_max, 1),
            "current_total_sec": round(cur_total, 1),
            "must_remove_sec": round(must_remove, 1),
            "clips": [
                {
                    "clip_idx": ci,
                    "role": c.role,
                    "duration": round(c.end_sec - c.start_sec, 1),
                    "beats": [
                        {
                            "beat_idx": bi,
                            "dur": round(b["end_sec"] - b["start_sec"], 1),
                            "summary": b.get("summary", ""),
                            "mood": b.get("mood", ""),
                            "importance": b.get("importance", "supporting"),
                            "carries_payoff": bool(b.get("carries_payoff", False)),
                        }
                        for bi, b in enumerate(per_clip_beats[ci])
                    ],
                }
                for ci, c in enumerate(clips)
            ],
        }
        try:
            resp = flash_drop_fn(payload) or {}
            for d in resp.get("drops") or []:
                try:
                    ci, bi = int(d["clip_idx"]), int(d["beat_idx"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= ci < len(per_clip_beats) and 0 <= bi < len(per_clip_beats[ci]):
                    ordered_drops.append((ci, bi))
        except Exception as e:  # noqa: BLE001 — Flash 실패는 결정적 폴백으로 흡수
            print(f"  [beat-trim] Flash drop 실패, 결정적 폴백: {e}")

    if not ordered_drops:
        ranked: list[tuple[int, int, int]] = []
        for ci, beats in enumerate(per_clip_beats):
            for bi, b in enumerate(beats):
                imp = b.get("importance", "supporting")
                if imp == "core":
                    continue
                ranked.append((_IMPORTANCE_ORDER.get(imp, 1), ci, bi))
        ranked.sort(key=lambda x: x[0])
        ordered_drops = [(ci, bi) for _, ci, bi in ranked]

    # 순서대로 적용 — 보호/core 제외, clip당 생존 ≥1, 하한(ceiling) 강제
    ceiling = cur_total - target_min  # 이보다 더 제거하면 target_min 미만
    removed = 0.0
    accepted: dict[int, set[int]] = defaultdict(set)
    survivors = {ci: len(beats) for ci, beats in enumerate(per_clip_beats)}
    for ci, bi in ordered_drops:
        if removed >= must_remove:
            break
        if bi in protected[ci]:
            continue
        if per_clip_beats[ci][bi].get("importance") == "core":
            continue
        if survivors[ci] <= 1:
            continue
        if bi in accepted[ci]:
            continue
        b = per_clip_beats[ci][bi]
        bdur = b["end_sec"] - b["start_sec"]
        if removed + bdur > ceiling + _EPS:
            break  # 하한 깨짐 → 중단
        accepted[ci].add(bi)
        survivors[ci] -= 1
        removed += bdur

    if not any(accepted.values()):
        return [], ""  # 아무것도 못 뺌 → 기존 fit 경로로

    # 분할·재구성 (subtitle/tts_draft는 첫 sub-clip에만, visual_essential 보존)
    new_clips: list[StoryClip] = []
    for ci, c in enumerate(clips):
        drops = accepted.get(ci) or set()
        if not drops:
            new_clips.append(c)
            continue
        intervals = _kept_intervals_from_drops(
            (c.start_sec, c.end_sec), per_clip_beats[ci], drops,
            min_clip_dur=min_clip_dur,
        )
        for idx, iv in enumerate(intervals):
            new_clips.append(StoryClip(
                role=c.role,
                start_sec=iv.start_sec,
                end_sec=iv.end_sec,
                subtitle=c.subtitle if idx == 0 else "",
                use_original_audio=c.use_original_audio,
                pacing_note=c.pacing_note,
                chunk_index=c.chunk_index,
                candidate_index=c.candidate_index,
                character_focus=c.character_focus,
                visual_essential=c.visual_essential,
                tts_draft=c.tts_draft if idx == 0 else "",
            ))

    new_total = total(new_clips)
    n_drops = sum(len(v) for v in accepted.values())
    msg = (
        f"내용 기반 trim: beat {n_drops}개 제거, "
        f"{len(clips)}→{len(new_clips)}클립 ({cur_total:.1f}s→{new_total:.1f}s)"
    )
    return new_clips, msg
