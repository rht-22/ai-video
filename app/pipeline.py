from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended-A
    "\U00002600-\U000026FF"  # misc symbols
    "\U0000200D"             # zero width joiner
    "\U00002B50"             # star
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


# 짧은 소재 완화의 바닥값(초). 후보 커버리지가 이보다도 짧으면 완화하지 않는다 —
# 쇼츠로 성립하지 않는 길이까지 내려서 억지로 뽑느니 검증에서 걸리는 편이 낫다.
_ABS_MIN_DURATION_SEC = 15


def _snap_clip_boundaries_to_dialogue(
    variants: list[tuple[list[StoryClip], str, float]],
    transcript_segments: list,
    snap_back_max: float = 5.0,
    snap_forward_max: float = 5.0,
) -> list[tuple[list[StoryClip], str, float]]:
    """라운드 19C-2: 각 variant의 첫 clip start·마지막 clip end를 문장 경계에 스냅.

    - 첫 clip start: -snap_back_max ~ +1s 범위 안 가장 가까운 *문장 시작* (segment.start_sec - 0.2s)으로 조정.
    - 마지막 clip end: -1s ~ +snap_forward_max 범위 안 가장 가까운 *문장 끝* (segment.end_sec + 0.3s)으로 조정.

    transcript_segments: list[SpeechSegment | SimpleNamespace] (start_sec, end_sec, text 속성)
    """
    if not variants or not transcript_segments:
        return variants
    segs = sorted(
        [(float(getattr(s, "start_sec", 0)), float(getattr(s, "end_sec", 0))) for s in transcript_segments],
        key=lambda t: t[0],
    )
    if not segs:
        return variants

    new_variants: list[tuple[list[StoryClip], str, float]] = []
    for sl_clips, sl_title, sl_score in variants:
        if not sl_clips:
            new_variants.append((sl_clips, sl_title, sl_score))
            continue
        first = sl_clips[0]
        last = sl_clips[-1]
        # 첫 clip start 스냅 — 가장 가까운 segment.start_sec - 0.2s, 단 [first.start_sec - snap_back_max, first.start_sec + 1.0] 범위 안
        cand_starts = [s_start - 0.2 for s_start, _ in segs
                       if (first.start_sec - snap_back_max) <= (s_start - 0.2) <= (first.start_sec + 1.0)]
        new_first_start = min(cand_starts, key=lambda x: abs(x - first.start_sec)) if cand_starts else first.start_sec
        new_first_start = max(0.0, new_first_start)

        # 마지막 clip end 스냅 — 가장 가까운 segment.end_sec + 0.3s, 단 [last.end_sec - 1.0, last.end_sec + snap_forward_max] 범위 안
        cand_ends = [s_end + 0.3 for _, s_end in segs
                     if (last.end_sec - 1.0) <= (s_end + 0.3) <= (last.end_sec + snap_forward_max)]
        new_last_end = max(cand_ends, key=lambda x: x) if cand_ends else last.end_sec

        # 변경 적용
        new_clips = list(sl_clips)
        if abs(new_first_start - first.start_sec) > 0.05:
            new_clips[0] = StoryClip(
                role=first.role, start_sec=new_first_start, end_sec=first.end_sec,
                subtitle=first.subtitle, use_original_audio=first.use_original_audio,
                pacing_note=first.pacing_note,
                chunk_index=first.chunk_index, candidate_index=first.candidate_index,
                character_focus=first.character_focus,
                tts_draft=first.tts_draft,
            )
            print(f"  [snap] 첫 clip start: {first.start_sec:.2f}s → {new_first_start:.2f}s (대사 도입 포함)")
        if abs(new_last_end - last.end_sec) > 0.05:
            last_idx = len(new_clips) - 1
            last_now = new_clips[last_idx]
            new_clips[last_idx] = StoryClip(
                role=last_now.role, start_sec=last_now.start_sec, end_sec=new_last_end,
                subtitle=last_now.subtitle, use_original_audio=last_now.use_original_audio,
                pacing_note=last_now.pacing_note,
                chunk_index=last_now.chunk_index, candidate_index=last_now.candidate_index,
                character_focus=last_now.character_focus,
                tts_draft=last_now.tts_draft,
            )
            print(f"  [snap] 마지막 clip end: {last.end_sec:.2f}s → {new_last_end:.2f}s (대사 여운 포함)")
        new_variants.append((new_clips, sl_title, sl_score))
    return new_variants


def _candidate_coverage_sec(candidates: list[dict]) -> float:
    """후보들이 덮는 원본 구간의 *합집합* 길이(초). context_extension 범위까지 포함.

    소재가 짧아 후보가 몇 개 안 나오면(예: 66s 소스 → 후보 2개) 어떤 조합으로도
    min_duration_sec 을 못 채운다. 그 상황을 호출부가 미리 알아야 LLM·length fit 이
    같은 구간을 두 번 넣어 길이를 맞추는 일을 막을 수 있다.
    """
    spans: list[tuple[float, float]] = []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        try:
            start = float(cand.get("start_sec"))
            end = float(cand.get("end_sec"))
        except (TypeError, ValueError):
            continue
        ext = cand.get("context_extension") or {}
        if ext.get("needed"):
            try:
                start = min(start, float(ext.get("extended_start_sec", start)))
                end = max(end, float(ext.get("extended_end_sec", end)))
            except (TypeError, ValueError):
                pass
        if end > start:
            spans.append((start, end))
    if not spans:
        return 0.0
    spans.sort()
    total = 0.0
    cur_start, cur_end = spans[0]
    for start, end in spans[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    return total + (cur_end - cur_start)


def _dedup_storyline_clips(
    clips: list[StoryClip],
    min_keep_sec: float = 1.5,
) -> tuple[list[StoryClip], dict[int, int], str]:
    """한 storyline 안에서 원본 구간이 겹치는 clip 을 잘라내거나 제거한다.

    후보가 부족하면 compose_story 가 같은 candidate 를 hook 과 build 에 그대로 두 번
    배치한다(2026-08-12 스트릿 레스토랑 파이터: hook·build 모두 32~50s). 그러면 완성본에서
    같은 화면이 두 번 재생된다. _validate_storyline_timeline 은 hook↔payoff 만 보므로
    여기서 시간축 기준으로 직접 복구한다.

    - 앞선 clip 이 이미 쓴 구간은 뒤 clip 에서 뺀다 (payoff 는 앞이 잘려 reveal 이 남는다)
    - 남은 조각이 없거나 min_keep_sec 미만이면 그 clip 을 통째로 버린다

    ⚠ 선공개형 훅(케이스 3, _clips_from_storyline)은 예외 — hook_preview 로 페이오프의
    핵심 몇 초를 앞에 보여주고 뒤에서 같은 장면을 온전히 다시 재생하는 *의도된* 반복이다
    (gemini_client.py 스토리 프롬프트: "같은 장면이 두 번 등장하므로 hook_preview 는
    임팩트 핵심만 짧게"). 시그니처는 "앞선 clip 구간이 뒤 clip 구간의 진부분집합" —
    맛보기(수 초)가 본편(더 긺) 안에 통째로 들어 있다. 이런 clip 은 잘라내지도,
    사용 구간으로 등록하지도 않는다. 사고 케이스(hook==build 완전 동일 / payoff 부분
    겹침)는 진부분집합이 아니라 이 예외에 걸리지 않는다.

    Returns: (복구된 clips, {원래 index: 새 index}, 사람용 요약 메시지)
    """
    kept: list[StoryClip] = []
    index_map: dict[int, int] = {}
    covered: list[tuple[float, float]] = []
    msgs: list[str] = []

    def _is_intended_preview(idx: int) -> bool:
        """뒤에 오는 clip 이 이 clip 의 구간을 진부분집합으로 품으면 선공개 맛보기."""
        me = clips[idx]
        for later in clips[idx + 1:]:
            contains = later.start_sec <= me.start_sec and me.end_sec <= later.end_sec
            strictly_longer = (later.end_sec - later.start_sec) > (me.end_sec - me.start_sec)
            if contains and strictly_longer:
                return True
        return False

    for orig_idx, clip in enumerate(clips):
        if _is_intended_preview(orig_idx):
            # 선공개형: 그대로 통과 + covered 미등록 → 뒤의 온전한 재생이 잘리지 않는다
            msgs.append(
                f"{clip.role}({clip.start_sec:.1f}~{clip.end_sec:.1f}s) 선공개 맛보기 — 반복 유지"
            )
            index_map[orig_idx] = len(kept)
            kept.append(clip)
            continue

        pieces: list[tuple[float, float]] = []
        cursor = clip.start_sec
        for cov_start, cov_end in covered:
            if cov_end <= cursor:
                continue
            if cov_start >= clip.end_sec:
                break
            if cov_start > cursor:
                pieces.append((cursor, min(cov_start, clip.end_sec)))
            cursor = max(cursor, cov_end)
            if cursor >= clip.end_sec:
                break
        if cursor < clip.end_sec:
            pieces.append((cursor, clip.end_sec))

        pieces = [(s, e) for s, e in pieces if e > s]
        if not pieces:
            msgs.append(f"{clip.role}({clip.start_sec:.1f}~{clip.end_sec:.1f}s) 전체 중복 → 제거")
            continue
        new_start, new_end = max(pieces, key=lambda p: p[1] - p[0])
        if new_end - new_start < min_keep_sec:
            msgs.append(
                f"{clip.role}({clip.start_sec:.1f}~{clip.end_sec:.1f}s) 잔여 "
                f"{new_end - new_start:.1f}s < {min_keep_sec}s → 제거"
            )
            continue
        if (new_start, new_end) != (clip.start_sec, clip.end_sec):
            msgs.append(
                f"{clip.role} {clip.start_sec:.1f}~{clip.end_sec:.1f}s → "
                f"{new_start:.1f}~{new_end:.1f}s (겹침 제거)"
            )
            clip = replace(clip, start_sec=new_start, end_sec=new_end)

        index_map[orig_idx] = len(kept)
        kept.append(clip)

        # covered 갱신 (정렬 + 인접/중첩 병합)
        covered.append((new_start, new_end))
        covered.sort()
        merged: list[tuple[float, float]] = []
        for span in covered:
            if merged and span[0] <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
            else:
                merged.append(span)
        covered = merged

    return kept, index_map, "; ".join(msgs)


def _remap_cue_clip_indexes(raw_cues, index_map: dict[int, int]):
    """_dedup_storyline_clips 로 clip 이 빠지면 LLM cue 의 clip_index 도 밀린다.

    사라진 clip 을 가리키던 cue 는 드롭 (하류 정규화가 어차피 범위 밖으로 버린다).
    """
    if not raw_cues or not index_map:
        return raw_cues
    out = []
    for cue in raw_cues:
        if not isinstance(cue, dict) or "clip_index" not in cue:
            out.append(cue)
            continue
        try:
            old_idx = int(cue["clip_index"])
        except (TypeError, ValueError):
            continue
        if old_idx not in index_map:
            continue
        new_cue = dict(cue)
        new_cue["clip_index"] = index_map[old_idx]
        out.append(new_cue)
    return out


def _extend_storyline_for_narrative(
    variants: list[tuple[list[StoryClip], str, float]],
    candidates_lookup: dict,
    target_max: float = 60.0,
    max_extend_per_side: float = 8.0,
) -> list[tuple[list[StoryClip], str, float]]:
    """라운드 19C-3: 길이 < target_max면 candidate.context_extension까지 always-on 확장.

    - 첫 clip: candidate.extended_start_sec까지 앞 확장 (max +max_extend_per_side, target_max 초과 안 함)
    - 마지막 clip: candidate.extended_end_sec까지 뒤 확장 (max +max_extend_per_side, target_max 초과 안 함)
    """
    if not variants or not candidates_lookup:
        return variants
    new_variants: list[tuple[list[StoryClip], str, float]] = []
    for sl_clips, sl_title, sl_score in variants:
        if not sl_clips:
            new_variants.append((sl_clips, sl_title, sl_score))
            continue
        cur_total = sum(c.end_sec - c.start_sec for c in sl_clips)
        if cur_total >= target_max:
            new_variants.append((sl_clips, sl_title, sl_score))
            continue
        budget = target_max - cur_total
        new_clips = list(sl_clips)

        # 첫 clip 앞 확장
        first = new_clips[0]
        first_cand = candidates_lookup.get((first.chunk_index, first.candidate_index))
        if first_cand is not None and budget > 0.5:
            ext = first_cand.get("context_extension") or {}
            ext_start = float(ext.get("extended_start_sec", first.start_sec)) if ext else first.start_sec
            available = first.start_sec - max(0.0, ext_start)
            if available > 0:
                ext_amount = min(budget, max_extend_per_side, available)
                if ext_amount > 0.5:
                    new_clips[0] = StoryClip(
                        role=first.role, start_sec=first.start_sec - ext_amount, end_sec=first.end_sec,
                        subtitle=first.subtitle, use_original_audio=first.use_original_audio,
                        pacing_note=first.pacing_note,
                        chunk_index=first.chunk_index, candidate_index=first.candidate_index,
                        character_focus=first.character_focus,
                        tts_draft=first.tts_draft,
                    )
                    budget -= ext_amount
                    print(f"  [narrative-ext] 첫 clip 앞 +{ext_amount:.1f}s ({first.start_sec:.1f}→{first.start_sec - ext_amount:.1f})")

        # 마지막 clip 뒤 확장
        last = new_clips[-1]
        last_cand = candidates_lookup.get((last.chunk_index, last.candidate_index))
        if last_cand is not None and budget > 0.5:
            ext = last_cand.get("context_extension") or {}
            ext_end = float(ext.get("extended_end_sec", last.end_sec)) if ext else last.end_sec
            available = ext_end - last.end_sec
            if available > 0:
                ext_amount = min(budget, max_extend_per_side, available)
                if ext_amount > 0.5:
                    last_idx = len(new_clips) - 1
                    new_clips[last_idx] = StoryClip(
                        role=last.role, start_sec=last.start_sec, end_sec=last.end_sec + ext_amount,
                        subtitle=last.subtitle, use_original_audio=last.use_original_audio,
                        pacing_note=last.pacing_note,
                        chunk_index=last.chunk_index, candidate_index=last.candidate_index,
                        character_focus=last.character_focus,
                        tts_draft=last.tts_draft,
                    )
                    print(f"  [narrative-ext] 마지막 clip 뒤 +{ext_amount:.1f}s ({last.end_sec:.1f}→{last.end_sec + ext_amount:.1f})")

        new_variants.append((new_clips, sl_title, sl_score))
    return new_variants


def _fill_intra_storyline_gaps(
    variants: list[tuple[list[StoryClip], str, float]],
    max_gap_sec: float = 3.0,
) -> list[tuple[list[StoryClip], str, float]]:
    """라운드 19C-4: 인접 clip 사이 갭이 ≤ max_gap_sec + character_focus 교집합 ≥ 1명이면
    pre clip의 end_sec을 next clip의 start_sec까지 확장 (=두 clip 결합).

    무음·암전 검증은 단순화: 갭 길이만 보고 결정 (정밀 검증은 silence cut 단계에서 이미 처리됨).
    """
    if not variants:
        return variants
    new_variants: list[tuple[list[StoryClip], str, float]] = []
    for sl_clips, sl_title, sl_score in variants:
        if len(sl_clips) < 2:
            new_variants.append((sl_clips, sl_title, sl_score))
            continue
        new_clips: list[StoryClip] = []
        for i, cur in enumerate(sl_clips):
            # 이어지는 다음 clip이 있고 갭 채움 조건이면 cur.end_sec 연장
            if i + 1 < len(sl_clips):
                nxt = sl_clips[i + 1]
                gap = nxt.start_sec - cur.end_sec
                cur_chars = set(cur.character_focus or ())
                nxt_chars = set(nxt.character_focus or ())
                if 0 < gap <= max_gap_sec and (cur_chars & nxt_chars):
                    cur = StoryClip(
                        role=cur.role, start_sec=cur.start_sec, end_sec=nxt.start_sec,
                        subtitle=cur.subtitle, use_original_audio=cur.use_original_audio,
                        pacing_note=cur.pacing_note,
                        chunk_index=cur.chunk_index, candidate_index=cur.candidate_index,
                        character_focus=cur.character_focus,
                        tts_draft=cur.tts_draft,
                    )
                    print(f"  [gap-fill] clip {i}↔{i+1} 갭 {gap:.2f}s 채움 (공통 character: {cur_chars & nxt_chars})")
            new_clips.append(cur)
        new_variants.append((new_clips, sl_title, sl_score))
    return new_variants


def _validate_storyline_timeline(sl_data: dict, iou_max: float = 0.3) -> tuple[bool, str]:
    """라운드 20: storyline 시간 일관성 검증.

    LLM이 hook/payoff에 같은 timestamp 부여하거나 build가 hook보다 앞선 시간 역행 케이스
    감지. 이런 storyline은 폐기하고 다음 ranked로 넘어간다.

    검증 항목:
    1. hook ↔ payoff IoU > iou_max 이면 같은 장면을 두 번 라벨한 환각 (예: SNL EP06 #2의
       2131-2224 같은 timestamp)
    2. 시간순 검증: build < hook < payoff (정시간순) 또는 build < payoff < hook (결과선공개)
       어느 쪽도 만족 안 하면 시간 역행

    highlight 타입은 hook만 있으므로 통과.
    """
    if not isinstance(sl_data, dict):
        return False, "not a dict"

    sl = sl_data.get("storyline") or {}
    hook = sl.get("hook") if isinstance(sl, dict) else None
    payoff = sl.get("payoff") if isinstance(sl, dict) else None
    builds = sl.get("build") or [] if isinstance(sl, dict) else []

    # highlight 타입 (storyline 키 없거나 hook만): 통과
    if not isinstance(hook, dict) or not isinstance(payoff, dict):
        return True, "highlight or simple structure"

    try:
        h_s = float(hook.get("start_sec", 0))
        h_e = float(hook.get("end_sec", 0))
        p_s = float(payoff.get("start_sec", 0))
        p_e = float(payoff.get("end_sec", 0))
    except (TypeError, ValueError):
        return False, "invalid hook/payoff timestamps"

    if h_e <= h_s or p_e <= p_s:
        return False, "non-positive duration"

    # IoU 검증
    inter = max(0.0, min(h_e, p_e) - max(h_s, p_s))
    union = (h_e - h_s) + (p_e - p_s) - inter
    iou = (inter / union) if union > 0 else 0.0
    if iou > iou_max:
        return False, f"hook ↔ payoff IoU={iou:.2f} > {iou_max} (같은 장면 중복 환각)"

    # 시간순 검증
    if builds:
        try:
            b_starts = [float(b.get("start_sec", 0)) for b in builds if isinstance(b, dict)]
            b_ends = [float(b.get("end_sec", 0)) for b in builds if isinstance(b, dict)]
        except (TypeError, ValueError):
            return False, "invalid build timestamps"
        if b_starts and b_ends:
            min_b_start = min(b_starts)
            max_b_end = max(b_ends)
            # Case 1 (정시간순): hook ≤ build ≤ payoff (build가 hook과 payoff 사이)
            case1 = (h_s - 0.5) <= min_b_start and max_b_end <= (p_e + 0.5) and h_e <= (p_s + 0.5)
            # Case 2 (결과선공개): build < payoff < hook (hook이 끝)
            case2 = max_b_end <= (p_s + 0.5) and p_e <= (h_s + 0.5)
            if not (case1 or case2):
                return False, (
                    f"timeline violation: hook[{h_s:.0f},{h_e:.0f}] "
                    f"build[{min_b_start:.0f}~{max_b_end:.0f}] "
                    f"payoff[{p_s:.0f},{p_e:.0f}]"
                )

    return True, "ok"


def _apply_silence_cut_to_variants(
    variants: list[tuple[list[StoryClip], str, float]],
    transcript_segments: list,
    candidates_lookup: dict | None = None,
    target_min: float = 40.0,
    target_max: float = 60.0,
) -> list[tuple[list[StoryClip], str, float]]:
    """모든 storyline variant의 sl_clips에 무음 컷 적용 후 갱신 + 길이 재보정.

    라운드 6a-2: tts_plan이 무음 컷 *전* clips를 받으면 cue 시간이 영상 길이 초과 가능.
    각 variant의 sl_clips에 cut_silence_from_clips → flatten_to_clips 적용해 동일 처리.

    라운드 12.1-A: 무음 컷 후 길이가 target_min(40s) 미만으로 떨어지는 케이스가 발생함
    (highlight 40s → 무음 컷 후 25s 같은 사례). candidates_lookup이 제공되면 _fit_storyline_to_duration
    재호출로 길이 재보정 (마지막 clip 끝을 candidate.end_sec까지 확장).
    """
    if not variants:
        return variants
    new_variants: list[tuple[list[StoryClip], str, float]] = []
    for sl_clips, sl_title, sl_score in variants:
        try:
            cut = cut_silence_from_clips(sl_clips, transcript_segments, max_gap_sec=0.4, padding_sec=0.15)
            sl_clips_new = flatten_to_clips(cut)
        except Exception:
            sl_clips_new = sl_clips  # 폴백: 변경 없음

        # 라운드 13-B: 무음 컷 롤백 — 무음 컷 후 길이가 target_min 미만으로 떨어지고
        # 무음 컷 *전* 길이가 target_max 안에 들어오면 무음 컷 *전* clips 사용 (영상 길이 회복).
        def _total(cs):
            return sum(c.end_sec - c.start_sec for c in cs)
        new_total = _total(sl_clips_new)
        orig_total = _total(sl_clips)
        if new_total < target_min and orig_total <= target_max:
            print(f"  [LengthFit-rollback] 무음 컷 후 {new_total:.1f}s < {target_min:.0f}s → 무음 컷 전 ({orig_total:.1f}s) 사용")
            sl_clips_new = list(sl_clips)

        # 라운드 12.1-A: 무음 컷 후 길이 재보정 (40~60s 보장)
        if candidates_lookup is not None:
            sl_clips_new, _fit_msg = _fit_storyline_to_duration(
                sl_clips_new, candidates_lookup,
                target_min=target_min, target_max=target_max,
            )
            if _fit_msg:
                print(f"  [LengthFit-postsilence] {_fit_msg}")

        new_variants.append((sl_clips_new, sl_title, sl_score))
    return new_variants


def _dedup_overlapping_candidates(
    candidates: list[dict],
    iou_threshold: float = 0.7,
) -> list[dict]:
    """라운드 19B: 청크 오버랩(180s)으로 동일 사건이 두 청크에서 보고된 경우, IoU 기반 dedup.

    chunk N과 N+1이 180s 영역을 공유하므로 동일 moment가 약간 다른 boundary로
    두 번 보고됨. (start_sec, end_sec) IoU ≥ threshold이면 score(또는 viral_score)
    높은 쪽만 유지.

    Args:
        candidates: all_candidates list (각 dict에 start_sec, end_sec, score, chunk_index 등)
        iou_threshold: IoU >= 이 값이면 중복으로 간주 (기본 0.7)

    Returns:
        dedup된 candidates list (정렬 보존, score 높은 쪽 유지)
    """
    if not candidates:
        return candidates

    def _iou(a: dict, b: dict) -> float:
        a_s, a_e = float(a.get("start_sec", 0)), float(a.get("end_sec", 0))
        b_s, b_e = float(b.get("start_sec", 0)), float(b.get("end_sec", 0))
        if a_e <= a_s or b_e <= b_s:
            return 0.0
        inter = max(0.0, min(a_e, b_e) - max(a_s, b_s))
        union = (a_e - a_s) + (b_e - b_s) - inter
        return inter / union if union > 0 else 0.0

    def _score(c: dict) -> float:
        # viral_score, score 중 큰 값 사용 (둘 다 없으면 0.5 fallback)
        return float(c.get("viral_score", c.get("score", 0.5)) or 0.5)

    # 시간순 정렬
    sorted_cands = sorted(candidates, key=lambda c: (float(c.get("start_sec", 0)), float(c.get("end_sec", 0))))
    keep: list[dict] = []
    drops = 0
    for cand in sorted_cands:
        merged = False
        for i, kept in enumerate(keep):
            if _iou(cand, kept) >= iou_threshold:
                # 중복 감지 → score 높은 쪽 유지
                if _score(cand) > _score(kept):
                    keep[i] = cand
                drops += 1
                merged = True
                break
        if not merged:
            keep.append(cand)
    if drops:
        print(f"  [cand-dedup] IoU ≥ {iou_threshold} 중복 {drops}개 제거 ({len(candidates)} → {len(keep)})")
    return keep


def _filter_candidates_by_chunk_intro_credits(
    candidates: list[dict],
    chunk_meta_list: list[dict],
    *,
    overlap_threshold: float = 0.5,
) -> list[dict]:
    """PR-2: 8단계 청크 분석이 식별한 chunk_intro_credits_ranges 와 겹치는 candidate 제거.

    candidate 시간의 overlap_threshold(기본 0.5 = 50%) 이상이 어떤 ranges 항목과 겹치면
    비-콘텐츠로 간주해 drop. 시간 기준은 chunk_meta_list 의 절대시간(actual_cut_offset 가산 후).

    Args:
        candidates: all_candidates list (각 dict 에 chunk_index, start_sec, end_sec)
        chunk_meta_list: 청크별 메타 — 각 항목의 "intro_credits_ranges" 는 절대시간 ranges 배열
        overlap_threshold: 0.0~1.0 (기본 0.5)

    Returns:
        필터 통과 candidate list (입력 순서 보존)
    """
    cmap: dict[int, list[dict]] = {}
    for cm in chunk_meta_list or []:
        ci_raw = cm.get("chunk_index")
        if ci_raw is None:
            continue
        try:
            ci = int(ci_raw)
        except (TypeError, ValueError):
            continue
        rng = cm.get("intro_credits_ranges") or []
        if ci >= 0 and rng:
            cmap[ci] = rng

    if not cmap:
        return list(candidates)

    kept: list[dict] = []
    dropped = 0
    for cand in candidates or []:
        try:
            ci = int(cand.get("chunk_index", -1))
        except (TypeError, ValueError):
            ci = -1
        cstart = float(cand.get("start_sec", 0) or 0)
        cend = float(cand.get("end_sec", 0) or 0)
        cdur = cend - cstart
        if ci not in cmap or cdur <= 0:
            kept.append(cand)
            continue
        drop_this = False
        for r in cmap[ci]:
            rs = float(r.get("start_sec", 0) or 0)
            re = float(r.get("end_sec", 0) or 0)
            ov = max(0.0, min(cend, re) - max(cstart, rs))
            if ov / cdur >= overlap_threshold:
                drop_this = True
                break
        if drop_this:
            dropped += 1
        else:
            kept.append(cand)
    if dropped:
        print(f"  [intro/credits] chunk 분석 기반 {dropped}개 candidate 제거 ({len(candidates)} → {len(kept)})")
    return kept


# ─────────────────────────────────────────────────────────────
# TTS cue 앵커 해석 — 원본 절대시간 → 편집 타임라인 절대시간
# ─────────────────────────────────────────────────────────────
# cue 는 story 단계에서 (clip_index, offset_sec) 앵커로 정규화되고 source_time_sec
# (원본 영상 절대시간)를 갖는다. 클립 경계는 그 뒤 silence_cut(분할) → snap/extend/fill →
# 길이 클램프로 여러 번 바뀌므로, 절대시간 변환은 **최종 클립이 확정된 뒤 여기서 한 번만** 한다.
# 최종 클립 자체가 원본 구간(start_sec~end_sec)을 갖고 있어 "원본시간 → 편집시간" 조각별
# 매핑만으로 충분하다 — 첫 클립이 앞으로 확장돼도 cue 는 *화면 내용*에 붙어 따라온다.

_MIN_CUE_TAIL = 0.5  # 앵커 소재가 잘려나갔을 때 클립 끝에서 확보할 최소 여유

# 소스 밖으로 나가 실제로 렌더되지 않는 분량이 이보다 크면 실패시킨다.
# 1.0s: 끝 경계가 소스 길이와 소수점에서 어긋나는 정상 케이스(실측 0~0.2s)는 통과시키고,
# 클립이 통째로/대부분 증발하는 경우만 잡는다.
CLIP_LOST_TOLERANCE_SEC = 1.0


def clips_beyond_source(clips, source_duration_sec: float,
                        tolerance: float = CLIP_LOST_TOLERANCE_SEC) -> list[dict]:
    """소스 영상 밖이라 **실제로 렌더되지 않는** 클립을 찾는다. 순수(테스트 대상).

    ⚠ **왜 필요한가**: 렌더는 클립마다 `-ss start -to end -i <소스>` 로 읽는데, 구간이
    소스 끝을 넘으면 그만큼 **조용히 짧아지고**, 시작 자체가 소스 밖이면 **프레임도
    오디오도 0개**가 나온다. concat 은 남은 클립만 이어 붙이므로 **반쪽짜리 쇼츠가
    아무 경고 없이 발행된다.**

    2026-08-24 실측(185개 런 중 5건, 4개 채널): 전부 클립 2개짜리에서 하나가 통째로
    증발해 18~26.6초(기획 분량의 절반 가까이)를 잃었다. 예: 혜미리예채파 2화 —
    소스 189.99s 인데 payoff 클립이 200.0~226.0s. 내레이션은 "결국 0캐시로 전부
    날려버렸다"고 말하는데 **그 장면이 화면에 없다.**

    Gemini 가 소스 길이 밖 타임스탬프를 만들어내는 것이 원인이고, 파이프라인 어디에도
    클립 경계를 소스 길이와 대조하는 코드가 없었다.

    Returns: 소실이 tolerance 를 넘는 클립별 dict
             (index·start·end·planned·rendered·lost) — 비어 있으면 정상."""
    src = float(source_duration_sec or 0.0)
    if src <= 0:
        return []                      # 소스 길이를 모르면 판정하지 않는다(오판 금지)
    out: list[dict] = []
    for i, c in enumerate(clips or []):
        start = float(getattr(c, "start_sec", 0.0))
        end = float(getattr(c, "end_sec", 0.0))
        planned = max(0.0, end - start)
        rendered = max(0.0, min(end, src) - start)
        lost = planned - rendered
        if lost > tolerance:
            out.append({"index": i, "start": start, "end": end,
                        "planned": planned, "rendered": rendered, "lost": lost})
    return out


def _resolve_cue_anchors(
    cues: list[dict],
    clips: list,  # 최종 variant StoryClip 리스트 (silence_cut·snap/extend/fill·클램프 반영 후)
) -> list[dict]:
    """앵커 cue 의 source_time_sec 를 최종 편집 타임라인 절대 start_sec/end_sec 로 변환.

    - source_time 을 포함하는 최종 클립 조각을 찾아 그 안의 상대 위치로 배치.
      같은 원본 구간이 여러 조각에 걸치면 앵커의 (chunk_index, candidate_index) 일치 우선.
    - 소재가 컷으로 빠졌으면: 같은 (chunk_index, candidate_index) 조각의 다음 kept 시작으로
      스냅, 그마저 없으면 마지막 조각 끝 - _MIN_CUE_TAIL. 앵커 클립이 통째로 제거됐으면 드롭.
    - end = start + duration_sec, 영상 전체 길이로 클램프.
    - 변환 후 시간순 정렬 + cue 간 겹침 제거 (뒤 cue 시작을 앞 cue 끝 + 0.05 로 이동).
    - source_time_sec 없는 cue (옛 체크포인트 재개 경로) 는 절대시간으로 간주하고
      영상 길이 클램프만 적용해 통과시킨다 (구 동작 보존).

    Returns: start_sec/end_sec 가 채워진 cue dict 리스트 (앵커 필드는 디버깅용 보존)
    """
    if not cues or not clips:
        return []

    # 편집 타임라인 조각: (원본 start, 원본 end, 편집 base, chunk_index, candidate_index)
    spans: list[tuple[float, float, float, int, int]] = []
    base = 0.0
    for c in clips:
        c_start = float(c.start_sec)
        c_end = float(c.end_sec)
        spans.append((c_start, c_end, base,
                      int(getattr(c, "chunk_index", -1)), int(getattr(c, "candidate_index", -1))))
        base += max(0.0, c_end - c_start)
    total = base

    out: list[dict] = []
    for cue in cues:
        duration = float(cue.get("duration_sec", 0.0) or 0.0)
        t_raw = cue.get("source_time_sec")

        if t_raw is None:
            # 구 스키마 (이미 절대시간) — 옛 체크포인트에서 재개된 경로. 클램프만.
            try:
                s = float(cue.get("start_sec"))
                e = float(cue.get("end_sec"))
            except (TypeError, ValueError):
                continue
            if e <= s or s >= total - 0.1:
                continue
            new_cue = dict(cue)
            new_cue["end_sec"] = min(e, total)
            out.append(new_cue)
            continue

        t = float(t_raw)
        key = (int(cue.get("chunk_index", -1)), int(cue.get("candidate_index", -1)))
        containing = [sp for sp in spans if sp[0] <= t < sp[1]]
        pick = None
        if containing:
            keyed = [sp for sp in containing if (sp[3], sp[4]) == key]
            pick = (keyed or containing)[0]
        else:
            # 앵커 지점의 소재가 컷/트림으로 빠짐 → 같은 앵커 클립 소재 안으로 스냅
            keyed = [sp for sp in spans if (sp[3], sp[4]) == key]
            after = [sp for sp in keyed if sp[0] >= t]
            if after:
                pick = min(after, key=lambda sp: sp[0])
                t = pick[0]
            elif keyed:
                pick = max(keyed, key=lambda sp: sp[1])
                t = max(pick[0], pick[1] - _MIN_CUE_TAIL)
            else:
                print(f"  [cue-resolve] 앵커 클립 소재가 최종 타임라인에 없음 → cue 드롭: "
                      f"{str(cue.get('text', ''))[:24]!r}")
                continue

        start = pick[2] + (t - pick[0])
        end = min(start + duration, total)
        if end - start < 0.3:
            print(f"  [cue-resolve] 변환 후 길이 {end - start:.2f}s < 0.3s → cue 드롭: "
                  f"{str(cue.get('text', ''))[:24]!r}")
            continue
        new_cue = dict(cue)
        new_cue["start_sec"] = start
        new_cue["end_sec"] = end
        out.append(new_cue)

    # 시간순 정렬 + 겹침 제거 (duration 유지한 채 뒤 cue 를 밀고, 영상 밖이면 드롭)
    out.sort(key=lambda x: x["start_sec"])
    resolved: list[dict] = []
    for cue in out:
        if resolved and cue["start_sec"] < resolved[-1]["end_sec"]:
            shift_to = resolved[-1]["end_sec"] + 0.05
            dur = cue["end_sec"] - cue["start_sec"]
            cue = dict(cue)
            cue["start_sec"] = shift_to
            cue["end_sec"] = min(shift_to + dur, total)
            if cue["end_sec"] - cue["start_sec"] < 0.3:
                print(f"  [cue-resolve] 겹침 보정 후 영상 밖 → cue 드롭: "
                      f"{str(cue.get('text', ''))[:24]!r}")
                continue
        resolved.append(cue)
    return resolved


# ─────────────────────────────────────────────────────────────
# PR-3: chunk_transcribe — 청크별 transcript segments 선행 생성
# ─────────────────────────────────────────────────────────────
# 기존 11단계 transcribe 는 storyline 결정 *후* 선택 clip만 Whisper로 돌리고,
# 8단계 analyze_chunk 는 SRT가 있을 때만 청크별 자막을 전달받음 (Whisper 없음).
# PR-3 은 청크 분할 직후(character_index 이후, gemini 이전) 청크별 transcript 를
# 한 번에 만들어 캐시. 8단계는 SRT/Whisper 구분 없이 이 결과를 사용.


def _slice_segments_for_chunk(
    segments: list,
    chunk_start_sec: float,
    chunk_end_sec: float,
) -> list:
    """SRT/Whisper 결과 segments 중 청크 범위와 *겹치는* 항목만 반환 (input order 보존)."""
    out = []
    for s in segments or []:
        try:
            ss = float(s.start_sec)
            se = float(s.end_sec)
        except (AttributeError, TypeError, ValueError):
            continue
        # 청크 범위와 겹침 (양 끝 정확히 닿기만 하면 0 길이 겹침 → 제외)
        if ss < chunk_end_sec and se > chunk_start_sec:
            out.append(s)
    return out


def _serialize_chunk_transcripts(chunk_transcripts: list[dict]) -> list[dict]:
    """in-memory [{chunk_index, segments: [SpeechSegment, ...]}] → JSON-safe dict list."""
    out: list[dict] = []
    for ct in chunk_transcripts or []:
        ci = ct.get("chunk_index")
        if ci is None:
            continue
        segs = []
        for s in ct.get("segments") or []:
            try:
                segs.append({
                    "start_sec": float(s.start_sec),
                    "end_sec": float(s.end_sec),
                    "text": str(getattr(s, "text", "") or ""),
                })
            except (AttributeError, TypeError, ValueError):
                continue
        out.append({"chunk_index": int(ci), "segments": segs})
    return out


def _deserialize_chunk_transcripts(payload: list[dict]) -> list[dict]:
    """JSON-loaded dict list → in-memory [{chunk_index, segments: [SpeechSegment, ...]}]."""
    from app.modules.speech import SpeechSegment as _SS
    out: list[dict] = []
    for entry in payload or []:
        if "chunk_index" not in entry:
            continue
        try:
            ci = int(entry["chunk_index"])
        except (TypeError, ValueError):
            continue
        segs: list = []
        for s in entry.get("segments") or []:
            try:
                segs.append(_SS(
                    start_sec=float(s.get("start_sec", 0)),
                    end_sec=float(s.get("end_sec", 0)),
                    text=str(s.get("text", "") or ""),
                ))
            except (TypeError, ValueError):
                continue
        out.append({"chunk_index": ci, "segments": segs})
    return out


def transcribe_chunks(
    chunks: list,
    srt_path: Path | None,
    *,
    work_title: str | None = None,
    character_names: list[str] | None = None,
    work_context: str | None = None,
    audio_workdir: Path,
    transcriber=None,
    backend: str | None = None,
    language: str = "ko",
    low_confidence_out: list | None = None,
    effective_backend_out: list | None = None,
) -> list[dict]:
    """청크별 transcript segments 를 *원본 영상 절대시간* 기준으로 생성.

    SRT 있으면 [parse_subtitle](app/modules/subtitle.py) 후 청크별 슬라이스.
    SRT 없으면 각 chunk.split_path 에서 transcriber(기본: extract_transcript) 호출.
    Whisper 결과는 chunk-relative 이므로 chunk.start_sec 만큼 시프트해 절대시간으로 변환.

    Args:
        chunks: Chunk list (각 항목에 index, start_sec, end_sec, split_path 속성)
        srt_path: SRT/ASS/VTT/SMI 자막 경로. None이면 Whisper 분기.
        audio_workdir: Whisper 분기에서 사용할 임시 작업 디렉토리 (현재는 직접 split_path 사용이라
                       파라미터만 받고 미사용 — DI 호환용)
        transcriber: SRT 없을 때 호출되는 콜러블 (Path → list[SpeechSegment]).
                     None이면 backend 가 고른 백엔드 사용(기본: 내장 Whisper).
                     테스트에서 Whisper 의존 없이 fake injection 가능.
        backend: E11 자막 전사 백엔드 — None·"default"=내장 Whisper(종전 그대로),
                 "elevenlabs"=ElevenLabs Scribe STT. 분기는 *여기서 끝난다* —
                 반환 표현·청크 오프셋 규칙은 두 백엔드가 같다(downstream 은 백엔드를 모름).
        language: 전사 언어 코드(ISO-639-1). 내장 Whisper 는 종전대로 "ko" 고정이라
                  ElevenLabs 경로에만 전달된다.
        low_confidence_out: E13-2b. 주면 elevenlabs 경로의 저확신 cue 구간을
                  **원본 절대시간**으로 여기에 append 한다(내장 경로는 아무것도 안 넣는다 —
                  Whisper 는 줄별 확신도를 안 준다). SpeechSegment(3필드)를 늘리지 않고
                  옆으로 흘려보내는 통로다: 늘리면 remap·merge 가 재구성하며 필드를
                  떨어뜨리고, 그 둘은 두 백엔드 공용이라 손대면 회귀 0 이 깨진다.
        effective_backend_out: E17. 주면 **실제로 쓴** 백엔드 이름을 순서대로 append 한다
                  (마지막 값이 최종이다). 토큰 만료 폴백이 일어나면 ["elevenlabs",
                  "default"] 가 되고, 호출부는 run_log·사이드카에 최종값을 적는다 —
                  그래야 키를 갱신한 다음 실행이 캐시를 무효화하고 다시 전사한다.

    Returns:
        [{"chunk_index": int, "segments": list[SpeechSegment]}, ...]
    """
    backend_name = (backend or "default").strip().lower()

    # SRT 분기 — 사람이 준 자막이 있으면 *전사 자체가 일어나지 않는다*. 백엔드 선택은
    # 받아쓰기(STT) 엔진을 고르는 노브라 이 경우 할 일이 없다. 조용히 무시하지 않고
    # 한 줄 남긴다 — 일레븐랩스로 바꿨는데 결과가 그대로인 이유가 로그에 있어야 한다.
    if srt_path is not None and Path(srt_path).exists():
        if backend_name != "default":
            print(f"  [transcribe] backend={backend_name} 지정됐지만 자막 파일이 있어 "
                  f"SRT 를 그대로 씁니다({Path(srt_path).name}) — 전사 호출 없음")
        all_segments = parse_subtitle(srt_path)
        return [
            {
                "chunk_index": int(c.index),
                "segments": _slice_segments_for_chunk(
                    all_segments, float(c.start_sec), float(c.end_sec)
                ),
            }
            for c in chunks
        ]

    def _builtin_transcriber(audio_path: Path):
        from app.modules.speech import extract_transcript

        return extract_transcript(
            audio_path,
            work_title=work_title,
            character_names=character_names,
            work_context=work_context,
        )

    # STT 분기 (E11: 백엔드 선택 지점 — 여기서 콜러블 하나로 감싸고 분기는 끝난다)
    if transcriber is None:
        if backend_name == "elevenlabs":
            from app.modules.stt_elevenlabs import EL_STT_MODEL_ID, build_transcriber

            # 키 없으면 여기서 즉시 실패한다(내장 전사로 조용히 안 떨어진다).
            transcriber = build_transcriber(
                language=language,
                work_title=work_title,
                character_names=character_names,
                work_context=work_context,
            )
            print(f"  [transcribe] backend=elevenlabs model={EL_STT_MODEL_ID}")
        else:
            if backend is not None:      # 명시적 default — 미지정과 결과는 같다
                print("  [transcribe] backend=default (내장 Whisper)")
            transcriber = _builtin_transcriber

    from app.modules.speech import SpeechSegment as _SS

    def _run_all(tr, name: str) -> list[dict]:
        """청크 전량을 한 백엔드로 돌린다 — 백엔드가 바뀌면 **처음부터** 다시 부른다."""
        out: list[dict] = []
        for c in chunks:
            sp = getattr(c, "split_path", None)
            if sp is None or not Path(sp).exists():
                out.append({"chunk_index": int(c.index), "segments": []})
                continue
            try:
                raw = tr(Path(sp))
            except Exception as e:
                if name == "elevenlabs":
                    # 유료 백엔드를 사람이 명시적으로 골랐다 — 빈 자막으로 조용히 발행하느니
                    # 크게 실패한다(E11 규율). 재시도 분류는 어댑터가 이미 끝냈다.
                    # E17: **인증 거절(ElevenLabsAuthError)** 만 바깥이 잡아 내장 전사로
                    # 내려간다 — 여기서는 종류를 가리지 않고 그대로 올린다.
                    raise
                print(f"  [chunk_transcribe] chunk {c.index} Whisper 실패 ({e}) — 빈 결과로 진행")
                raw = []
            # chunk-relative → 절대시간 (split_video_chunk 가 PTS 0 정규화 했으므로 chunk.start_sec 가산)
            chunk_offset = float(getattr(c, "start_sec", 0.0))
            # E13-2b: 저확신 구간도 **같은 오프셋**을 받는다 — 자막과 좌표계가 하나여야
            # 나중에 어느 자막 줄이 저확신인지 겹침으로 판정할 수 있다.
            if name == "elevenlabs" and low_confidence_out is not None:
                from app.modules.stt_elevenlabs import take_low_confidence_spans

                for _sp in take_low_confidence_spans():
                    low_confidence_out.append({
                        "start_sec": round(float(_sp["start_sec"]) + chunk_offset, 2),
                        "end_sec": round(float(_sp["end_sec"]) + chunk_offset, 2),
                        "logprob": _sp.get("logprob"),
                    })
            abs_segs: list = []
            for s_ in raw or []:
                try:
                    abs_segs.append(_SS(
                        start_sec=float(s_.start_sec) + chunk_offset,
                        end_sec=float(s_.end_sec) + chunk_offset,
                        text=str(getattr(s_, "text", "") or ""),
                    ))
                except (AttributeError, TypeError, ValueError):
                    continue
            out.append({"chunk_index": int(c.index), "segments": abs_segs})
        return out

    if effective_backend_out is not None:
        effective_backend_out.append(backend_name)
    if backend_name != "elevenlabs":
        return _run_all(transcriber, backend_name)

    # E17(2026-08-24): 토큰 만료(401·403)면 **내장 전사로 처음부터 다시** 돌린다.
    # 그 외 실패는 종전대로 크게 실패한다(유료 백엔드를 골랐는데 자막이 비면 안 된다).
    # 청크마다 백엔드를 섞지 않는 이유는 ElevenLabsAuthError 주석에 있다 — 같은 소재에서
    # cue 수가 백엔드별로 크게 갈려(E13-0 실측) 한 편 안에서 자막 리듬이 달라진다.
    from app.modules.stt_elevenlabs import ElevenLabsAuthError

    try:
        return _run_all(transcriber, backend_name)
    except ElevenLabsAuthError as e:
        print(f"  [transcribe] ElevenLabs 토큰 거절 — {e}")
        print("  [transcribe] 내장 전사(Whisper)로 전환해 청크 전량을 다시 전사합니다 "
              "(토큰을 갱신하면 다음 실행부터 다시 ElevenLabs 로 돌아갑니다).")
        if low_confidence_out is not None:
            # 만료 전 청크가 남긴 저확신 구간은 이제 없는 전사의 좌표다 — 비운다.
            del low_confidence_out[:]
        if effective_backend_out is not None:
            effective_backend_out.append("default")
        return _run_all(_builtin_transcriber, "default")


def _tts_cue_cache_key(text: str, voice: str, speed: str, target_sec: float) -> str:
    """cue 합성 캐시 키 = (문구, voice, speed, fit 목표창). 넷 중 하나만 달라도 다른 소리다."""
    raw = f"{text}\x00{voice}\x00{speed}\x00{round(float(target_sec), 2)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def synthesize_cue_cached(
    text: str,
    output_path: Path,
    *,
    target_sec: float,
    voice: str,
    speed: str,
    shorten_fn=None,
    cache_dir: Path | None = None,
) -> tuple[str, float]:
    """TTS cue 합성 — **elevenlabs 유료 합성만** 캐시한다(E12).

    편집실 재렌더는 `from_step=resources` 로 오므로 한 줄만 고쳐도 전 cue 가 다시 돈다.
    캐시가 없으면 그 요금이 그대로 곱해진다. 캐시 키가 같으면 mp3 를 복사해 쓰고 API 를
    아예 안 부른다(요금 0).

    접두사 없는 종전 목소리는 **캐시를 타지 않는다** — 종전 호출을 그대로 통과시킨다.
    edge-tts 는 공짜라 아낄 요금이 없고, 캐시를 끼우면 fit 재작성(Flash 단축)의 결과가
    달라져 지금 도는 내레이션이 변한다(회귀 0 조건).
    """
    from app.modules.tts import (
        elevenlabs_disabled,
        is_elevenlabs_voice,
        synthesize_tts_with_fit,
    )

    if not is_elevenlabs_voice(voice) or cache_dir is None:
        return synthesize_tts_with_fit(
            text, output_path, target_sec=target_sec,
            voice=voice, speed=speed, shorten_fn=shorten_fn)

    key = _tts_cue_cache_key(text, voice, speed, target_sec)
    cached_mp3 = Path(cache_dir) / f"{key}.mp3"
    cached_meta = Path(cache_dir) / f"{key}.json"
    if cached_mp3.exists() and cached_meta.exists():
        try:
            meta = json.loads(cached_meta.read_text(encoding="utf-8"))
            shutil.copyfile(cached_mp3, output_path)
            print(f"  [TTS-cache] hit {key[:8]} — 재합성 없음(요금 0)")
            return str(meta["text"]), float(meta["actual_sec"])
        except (OSError, ValueError, KeyError) as e:
            print(f"  [TTS-cache] 캐시 로드 실패({e}) — 재합성")

    final_text, actual_sec = synthesize_tts_with_fit(
        text, output_path, target_sec=target_sec,
        voice=voice, speed=speed, shorten_fn=shorten_fn)
    if elevenlabs_disabled():
        # E17: 토큰 만료로 기본 백엔드가 낸 소리다 — ElevenLabs 캐시 키에 넣으면 키를
        # 갱신한 뒤에도 이 mp3 가 되살아나 '고쳤는데 왜 그대로지'가 된다.
        print(f"  [TTS-cache] 저장 안 함 — 토큰 만료 폴백 산물({key[:8]})")
        return final_text, actual_sec
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output_path, cached_mp3)
        cached_meta.write_text(
            json.dumps({"text": final_text, "actual_sec": actual_sec},
                       ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"  [TTS-cache] 캐시 저장 실패({e}) — 다음 렌더에서 재합성됨")
    return final_text, actual_sec


def _enforce_title_line_limit(text: str, max_chars: int = 20) -> str:
    """LLM이 title_line1/line2 글자수 가이드를 어겼을 때 안전판 — max_chars 이내로 강제 절단.

    어절 경계 기준으로 자르되, 단어 하나가 max_chars 초과면 그대로 잘림.
    라운드 7-B에서 line2 전용 함수를 line1·line2 공용으로 일반화.
    라운드 22: default 13 → 20 (사용자 요청). 단 22C에서 LLM 재작성 먼저 시도 후 안전판으로 사용.
    """
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    # 어절 경계로 자르기
    words = text.split()
    out = ""
    for w in words:
        candidate = (out + " " + w).strip() if out else w
        if len(candidate) > max_chars:
            break
        out = candidate
    if not out:
        out = text[:max_chars]
    return out.strip()


# 라운드 5에서 도입된 이름의 호환성 유지를 위한 별칭 (line2 전용 호출처)
_enforce_title_line2_limit = _enforce_title_line_limit


def _looks_garbled(original: str, shortened: str) -> bool:
    """LLM 제목 단축 결과가 원문 글자를 거의 재사용하지 않으면(압축이 아니라 환각/깨짐) True.
    제목 단축은 원문의 '압축'이어야 정상 — shorten_text(Flash)가 가끔 깨진 한국어/훼손된
    이름을 짧게 뱉는데 수용 조건이 길이뿐이라 그대로 채택되던 버그 방지. 거부 시 호출부가
    어절 경계 절단(_enforce_title_line_limit)으로 폴백한다. (자가개선 루프 R3: 타이틀 첫 줄 깨짐 수정.)"""
    o = set(original.replace(" ", ""))
    s = set(shortened.replace(" ", ""))
    if not s:
        return True
    # 정상 압축은 원문 글자의 부분집합(overlap≈1.0). 0.85 미만이면 새 글자 유입(부분 깨짐/이름 훼손
    # 또는 위험한 패러프레이즈) → 거부하고 어절 절단(원문 보존)으로 폴백.
    return (len(s & o) / len(s)) < 0.85


from app.config import AppConfig, Paths, DesignConfig, get_font_path
from app.modules.provenance import build_provenance
from app.modules.story_builder import StoryClip


def _fit_storyline_to_duration(
    clips: list,
    candidates_lookup: dict,
    target_min: float = 40.0,
    target_max: float = 60.0,
    allow_remove: bool = True,
) -> tuple[list, str]:
    """라운드 11: storyline clips 합계 길이를 target 범위로 단축/확장 자동 보정.

    동작:
    - 합계 ≤ target_max 그리고 ≥ target_min → 변경 없음
    - 합계 > target_max → 점수 낮은 build부터 제거. build 모두 제거 후에도 초과면
      가장 긴 clip의 끝을 잘라 단축. hook/payoff는 가능한 보존.
    - 합계 < target_min → 마지막 clip의 끝을 같은 candidate.end_sec까지 확장 (있으면).
      그래도 부족하면 워닝 메시지 (호출부에서 reject 또는 다음 storyline 시도).

    allow_remove=False 면 build 통째 제거를 금지하고 역할별 부분 trim 만 쓴다 —
    편집실 pinned 구간용(2026-08-19 실측: 사람이 추가한 19.6s build 가 상한을 0.08s
    넘겼다는 이유로 통째로 사라졌다. 사람이 넣은 장면은 지워도 되는 후보가 아니다.
    편집실 추가 장면은 chunk_index=-1 → score 0.0 이라 항상 첫 제거 후보가 된다).

    반환: (조정된 clips, 처방 메시지) — 메시지가 비어 있으면 변경 없음.
    """
    if not clips:
        return clips, ""

    def total_dur(cs):
        return sum(float(c.end_sec - c.start_sec) for c in cs)

    msgs: list[str] = []
    out = list(clips)
    cur_total = total_dur(out)
    orig_total = cur_total

    # 1) 단축: 합계 > target_max
    if cur_total > target_max:
        # build 클립만 추출, score 오름차순 (낮은 점수 먼저 제거 후보)
        def build_score(c):
            cand = candidates_lookup.get((c.chunk_index, c.candidate_index)) if candidates_lookup else None
            if cand is None:
                return 0.0
            return float(cand.get("viral_score", cand.get("score", 0.0)) or 0.0)

        # build 인덱스 + score 페어
        build_indices = [(i, build_score(c)) for i, c in enumerate(out) if c.role == "build"]
        # 점수 낮은 순으로 정렬 — 낮은 점수부터 제거 후보
        build_indices.sort(key=lambda x: x[1])

        removed_count = 0
        # 점수 낮은 build부터 1개씩 제거하며 target_max 이하로
        # 단, 다음 제거가 target_min 미만으로 떨어뜨리면 *부분 단축*으로 전환
        partial_trim = False
        while allow_remove and cur_total > target_max and build_indices:
            idx, _score = build_indices[0]
            build_clip = out[idx]
            build_dur = build_clip.end_sec - build_clip.start_sec
            proposed_total = cur_total - build_dur
            if proposed_total < target_min:
                # 통째로 제거 시 너무 짧아짐 → 이 build의 끝만 잘라 target_max에 맞춤
                cut_amount = cur_total - target_max
                if cut_amount < build_dur:
                    new_end = build_clip.end_sec - cut_amount
                    out[idx] = StoryClip(
                        role=build_clip.role,
                        start_sec=build_clip.start_sec,
                        end_sec=new_end,
                        subtitle=build_clip.subtitle,
                        use_original_audio=build_clip.use_original_audio,
                        pacing_note=build_clip.pacing_note,
                        chunk_index=build_clip.chunk_index,
                        candidate_index=build_clip.candidate_index,
                        character_focus=build_clip.character_focus,
                        visual_essential=build_clip.visual_essential,
                        tts_draft=build_clip.tts_draft,
                    )
                    cur_total = total_dur(out)
                    partial_trim = True
                break  # 더 이상 build 제거 안 함
            # 통째로 제거
            build_indices.pop(0)
            out = [c for i, c in enumerate(out) if i != idx]
            build_indices = [(i if i < idx else i - 1, s) for i, s in build_indices]
            removed_count += 1
            cur_total = total_dur(out)
        if removed_count or partial_trim:
            parts = []
            if removed_count:
                parts.append(f"build {removed_count}개 제거")
            if partial_trim:
                parts.append("build 부분 단축")
            msgs.append(f"{' + '.join(parts)} ({orig_total:.1f}s→{cur_total:.1f}s)")

        # build 모두 제거 후에도 target_max 초과 → 라운드 21: 역할별(role-aware) 지능형 trim.
        # - hook: 시작 보존(후킹 모먼트), 끝 자름
        # - build: 양 끝 균등 자름 (피크 보존)
        # - payoff: 끝 보존(reveal/punchline), 시작 자름
        # 각 clip 비례 단축하되 role에 따라 어느 쪽에서 잘리는지 결정.
        # 라운드 12.1-B: 모든 clip 비례적 단축 (각 clip ≥3초 보장).
        if cur_total > target_max and out:
            ratio = target_max / cur_total
            min_clip_dur = 3.0
            cut_summary = []
            for idx, clip in enumerate(out):
                clip_dur = clip.end_sec - clip.start_sec
                new_dur = max(min_clip_dur, clip_dur * ratio)
                if new_dur >= clip_dur:
                    continue
                excess = clip_dur - new_dur
                # 역할별 trim 위치 결정
                if clip.role == "payoff":
                    # payoff: 끝 보존(reveal), 시작에서 자름
                    new_start = clip.start_sec + excess
                    new_end = clip.end_sec
                    side = "start"
                elif clip.role == "build":
                    # build: 양 끝 균등 (피크 중간 보존)
                    new_start = clip.start_sec + excess / 2
                    new_end = clip.end_sec - excess / 2
                    side = "both"
                else:
                    # hook 및 기타: 시작 보존(후킹 모먼트), 끝에서 자름
                    new_start = clip.start_sec
                    new_end = clip.end_sec - excess
                    side = "end"
                out[idx] = StoryClip(
                    role=clip.role,
                    start_sec=new_start, end_sec=new_end,
                    subtitle=clip.subtitle, use_original_audio=clip.use_original_audio,
                    pacing_note=clip.pacing_note,
                    chunk_index=clip.chunk_index, candidate_index=clip.candidate_index,
                    character_focus=clip.character_focus,
                    visual_essential=clip.visual_essential,
                    tts_draft=clip.tts_draft,
                )
                cut_summary.append(f"{clip.role}({side}) {clip_dur:.1f}s→{new_dur:.1f}s")
            cur_total = total_dur(out)
            if cut_summary:
                msgs.append(f"역할별 trim [{', '.join(cut_summary)}] (총 {orig_total:.1f}s→{cur_total:.1f}s)")

    # 2) 확장: 합계 < target_min — 라운드 13: 양방향 확장 (첫 시작 + 마지막 끝)
    elif cur_total < target_min:
        # 2-1) 마지막 clip 끝 확장 (기존)
        shortage = target_min - cur_total
        last_idx = len(out) - 1
        last = out[last_idx]
        last_cand = candidates_lookup.get((last.chunk_index, last.candidate_index)) if candidates_lookup else None
        if last_cand is not None:
            cand_end = float(last_cand.get("end_sec", last.end_sec))
            ext = last_cand.get("context_extension") or {}
            if ext.get("needed"):
                cand_end = max(cand_end, float(ext.get("extended_end_sec", cand_end)))
            available_post = cand_end - last.end_sec
            if available_post > 0:
                ext_amount = min(shortage, available_post)
                new_end = last.end_sec + ext_amount
                out[last_idx] = StoryClip(
                    role=last.role,
                    start_sec=last.start_sec, end_sec=new_end,
                    subtitle=last.subtitle, use_original_audio=last.use_original_audio,
                    pacing_note=last.pacing_note,
                    chunk_index=last.chunk_index, candidate_index=last.candidate_index,
                    character_focus=last.character_focus,
                    visual_essential=last.visual_essential,
                    tts_draft=last.tts_draft,
                )
                cur_total = total_dur(out)
                msgs.append(f"마지막 clip 끝 확장 ({orig_total:.1f}s→{cur_total:.1f}s, +{ext_amount:.1f}s)")

        # 2-2) 라운드 13 신규 — 첫 clip 시작 확장 (역방향)
        if cur_total < target_min:
            shortage = target_min - cur_total
            first = out[0]
            first_cand = candidates_lookup.get((first.chunk_index, first.candidate_index)) if candidates_lookup else None
            if first_cand is not None:
                cand_start = float(first_cand.get("start_sec", first.start_sec))
                ext = first_cand.get("context_extension") or {}
                if ext.get("needed"):
                    cand_start = min(cand_start, float(ext.get("extended_start_sec", cand_start)))
                available_pre = first.start_sec - cand_start
                if available_pre > 0:
                    ext_amount = min(shortage, available_pre)
                    new_start = first.start_sec - ext_amount
                    # 첫 clip start 변경 시 _start 0초 보장
                    new_start = max(0.0, new_start)
                    out[0] = StoryClip(
                        role=first.role,
                        start_sec=new_start, end_sec=first.end_sec,
                        subtitle=first.subtitle, use_original_audio=first.use_original_audio,
                        pacing_note=first.pacing_note,
                        chunk_index=first.chunk_index, candidate_index=first.candidate_index,
                        character_focus=first.character_focus,
                        visual_essential=first.visual_essential,
                        tts_draft=first.tts_draft,
                    )
                    cur_total = total_dur(out)
                    msgs.append(f"첫 clip 시작 확장 (+{(first.start_sec - new_start):.1f}s, 총 {cur_total:.1f}s)")

        # 그래도 부족하면 워닝 (호출부가 결정)
        if cur_total < target_min:
            msgs.append(f"⚠️ 길이 부족 {cur_total:.1f}s < {target_min:.0f}s — 확장 한계")

    return out, "; ".join(msgs)
from app.modules.chunker import build_chunks, split_video_chunk
from app.modules.editorial import filter_flagged_candidates, filter_log_entry
from app.modules.edit_overrides import (
    EditOverrideError,
    apply_overrides,
    load_edit_overrides,
    overrides_subtitles,
    overrides_texts,
    overrides_title_segments,
    overrides_tts,
    place_anchored_images,
    place_anchored_subtitles,
    place_anchored_texts,
    resolve_image_files,
    total_duration,
)
from app.modules.gemini_client import (
    _normalize_storyline_tts_cues,
    load_gemini_client,
)
from app.modules.media_probe import probe_media
from app.modules.moment_ranker import assign_sequence_ids
from app.modules.reframe import build_crop_timeline
from app.modules.renderer import RenderInputs, render_short
from app.modules.scene_detect import detect_scenes
from app.modules import timestamp_check
from app.modules.speech import SpeechSegment  # PR-5c-4: extract_audio_segment / extract_transcript 직접 사용 제거 — chunk_transcribe 헬퍼 내부 lazy import 로 일원화
from app.modules.story_builder import (
    StoryClip,
    validate_story_clips,
    validate_clip_coherence,
    select_diverse_storylines,
)
from app.modules.subtitle import (
    SubtitleStyle,
    build_ass_from_segments,
    build_tts_ass,
    merge_subtitle_segments,
    parse_subtitle,
    remap_transcript_to_edited_timeline,
)
from app.modules.tts import synthesize_tts
from app.modules.work_researcher import research_work, CharacterInfo
from app.modules.validator import validate_output
from app.modules.ffmpeg_utils import find_ffmpeg_command
from types import SimpleNamespace
from app.modules.silence_cutter import (
    cut_silence_from_clips,
    cut_silence_with_story_filter,
    flatten_to_clips,
    get_silence_profile,
    print_silence_cut_summary,
)
from app.modules.beat_trimmer import beat_trim_storyline


def _video_band_bottom(
    design: DesignConfig,
    *,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
    legacy_center: bool = False,
) -> int:
    """영상 밴드 하단 y(캔버스 px) — 렌더러 [2]와 같은 수식.

    - scaled_h = int(video_width * r_h / r_w) (E10: 밴드 **폭** 기준) + 짝수 보정
    - overlay_y = video_y 지정 시 그 위치(H - scaled_h 클램프), 미지정 = 세로 중앙

    legacy_center=True 면 video_width·video_y 를 무시한 종전 기하(꽉 찬 폭·세로 중앙)
    — TTS margin 델타(_compute_tts_margin_v)의 기준선용. 비숫자 video_width/video_y 는
    비율 파싱과 같은 관용으로 폴백(꽉 찬 폭/세로 중앙) — 범위 검증은 렌더 경계 담당이라
    어차피 직후 렌더가 즉시 실패한다. 밴드가 캔버스를 넘치면 반환값도 H 를 넘을 수 있다
    — 호출부가 용도에 맞게 클램프한다.
    """
    H = canvas_height
    W = canvas_width
    scaled_w = W
    if not legacy_center:
        try:
            scaled_w = int(str(getattr(design, "video_width", W)))
        except (TypeError, ValueError):
            scaled_w = W
    try:
        r_w, r_h = map(int, str(getattr(design, "aspect_ratio", "1:1")).split(":"))
        scaled_h = int(scaled_w * r_h / r_w)
    except Exception:
        scaled_h = scaled_w
    scaled_h -= scaled_h % 2
    video_y = None
    if not legacy_center:
        try:
            video_y = int(getattr(design, "video_y", None))
        except (TypeError, ValueError):
            video_y = None
    if video_y is not None:
        overlay_y = min(max(0, video_y), max(0, H - scaled_h))
    else:
        overlay_y = max(0, (H - scaled_h) // 2)
    return overlay_y + scaled_h


def _compute_subtitle_margin_v(
    design: DesignConfig,
    *,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
    padding_px: int = 10,
) -> int:
    """ASS 메인 자막의 margin_v — 영상 밴드 하단에서 padding_px 위에 오도록 동적 계산.

    - 자막 baseline = 밴드 하단(_video_band_bottom) - padding_px
    - ASS alignment=2(하단 중앙) 기준 margin_v = canvas_height - baseline
    - 밴드가 캔버스를 채우면 하단 끝에서 padding_px 위(padding 하한)

    aspect_ratio는 DesignConfig에, 캔버스 크기는 AppConfig에 있으므로 호출부에서 명시 전달.
    E10 후속 + 8/21 발주 검수 교정: 신 기하(밴드 하단 앵커 — video_width·video_y 반영)는
    **video_width 가 명시된 경우에만** 적용한다. 미지정이면 종전 기하(꽉 찬 폭·세로 중앙
    가정) 그대로 — video_y 만 쓰는 기존 채널(한 입 주막 video_y=440, 2026-08-19 실렌더
    픽셀 튜닝)의 자막이 조용히 움직이면 안 된다. video_width 는 오케스트레이터
    editor_e10 게이트 뒤에서만 들어오므로 '명시 = 신규 채널'이 곧 회귀 0 조건이다.
    """
    H = canvas_height
    bottom = _video_band_bottom(
        design, canvas_width=canvas_width, canvas_height=canvas_height,
        legacy_center=getattr(design, "video_width", None) is None)
    return max(padding_px, H - bottom + padding_px)


def _compute_tts_margin_v(
    design: DesignConfig,
    *,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
) -> int:
    """TTS 자막 MarginV — tts_line_y_margin 을 영상 밴드에 앵커한다 (E10 후속 3).

    tts_line_y_margin(기본 580)은 종전엔 캔버스 하단 기준 고정이라 video_width·video_y
    로 밴드가 움직여도 TTS 줄이 제자리에 남았다. 종전 기하(꽉 찬 폭·세로 중앙,
    legacy_center) 대비 밴드 하단이 움직인 델타만큼 margin 을 함께 움직인다 —
    **밴드 하단으로부터의 오프셋이 상수**가 되는 변환이다. 메인 자막(항상 밴드 하단
    10px 위)과 달리 사용자 노브(tts_line_y_margin)를 유지해야 해서 절대 재계산이 아니라
    델타로 따른다. 하단은 H 클램프(캔버스 밖 밴드 무의미), 결과는 0 하한 —
    build_tts_ass 가 음수 margin 을 '미지정'(480 폴백)으로 해석한다.

    8/21 발주 검수 교정: 델타 앵커는 **video_width 가 명시된 경우에만**. 미지정이면
    tts_line_y_margin 절대값 그대로(종전 동작) — video_y 만 쓰는 기존 채널(한 입 주막
    tts 550 = 하단 y 1370, 사람이 실렌더로 픽셀 튜닝)의 TTS·로고 스택이 조용히
    움직이면 안 된다. 메인 자막의 명시 조건과 동일한 게이트.
    """
    H = canvas_height
    base = int(getattr(design, "tts_line_y_margin", 580))
    if getattr(design, "video_width", None) is None:
        return max(0, base)
    legacy_bottom = min(H, _video_band_bottom(
        design, canvas_width=canvas_width, canvas_height=canvas_height,
        legacy_center=True))
    band_bottom = min(H, _video_band_bottom(
        design, canvas_width=canvas_width, canvas_height=canvas_height))
    return max(0, base + (legacy_bottom - band_bottom))


# ─────────────────────────────────────────────────────────────────────────
# E17-2 (2026-08-24) — 소스에 박힌 원본 자막 회피
# ─────────────────────────────────────────────────────────────────────────
# 사용자 지시: "영상에 원래 자막이 있으면 그 위치 피해서 자막이 들어가게 해줘.
# 물론, 자막이 제목과도 겹치면 안되고."
#
# 판정·수식은 전부 `app/modules/subtitle_region.py` 에 있다(밴드 기하·행 점수·회피 계산).
# 여기 두 함수는 그것을 파이프라인에 붙이는 배선일 뿐이다:
#   · 검출은 **렌더 단계에서 한 번**만 하고 `checkpoint_burned_subtitle.json` 에 남긴다 —
#     편집실 재렌더(`--from-step render|resources`)마다 다시 재면 같은 편의 자막 위치가
#     실행마다 달라진다(E15 체크포인트 규약과 같은 이유).
#   · 클립이 바뀌면(구간 재편집) 캐시는 무효다 — 표본을 뜬 화면 자체가 다른 화면이다.
#   · 실패는 전부 '아무것도 안 함'이다. 이건 연출이 아니라 안전장치라 본편을 막지 않는다.
# 구간별 표본 `[[편집본 시각, 글자행 목록], …]` — 검출 한 번, variant 까지 공유한다.
# (전역 띠와 같은 체크포인트 파일에 남아 `--from-step render` 재개에서도 유지된다.)
_BURNED_PROFILES: list[list] = []

# 구간별로 실제로 올린 줄 수 — run_log 감사 기록용 (E18 후속, 2026-08-24).
# 이 보정은 종전에 stdout 에만 남아서 **돌았는지 안 돌았는지 나중에 알 수가 없었다**
# (검증 런에서 실제로 막혔다: `subtitle_avoid_burned` 단계가 없으면 '띠를 못 찾은 것'인지
# '구간 판정 자체가 안 돈 것'인지 구분이 안 된다). 판정이 돌았다는 사실과 결과를 남긴다.
_BURNED_WINDOW_MOVES: list[dict] = []


def _burned_band_signature(clips: list) -> str:
    """표본을 뜬 클립 구성 지문 — 구간이 바뀌면 캐시를 버린다."""
    raw = ";".join(f"{float(getattr(c, 'start_sec', 0.0)):.3f}-{float(getattr(c, 'end_sec', 0.0)):.3f}"
                   for c in clips or [])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _detect_burned_band_cached(
    payload,
    config,
    clips: list,
    crop_map: dict,
    output_dir: Path,
) -> dict[str, Any] | None:
    """원본 자막 띠(캔버스 y) — 없으면 None. 캐시 우선, 검출은 실행당 한 번."""
    # ⚠ 모듈 수준 저장소라 한 프로세스에서 여러 편을 돌리면 앞 편 표본이 남는다 —
    #   앞 편의 원본 자막 자리로 이 편 자막을 올리면 조용히 틀린다. 시작에서 비운다.
    _BURNED_PROFILES[:] = []
    _BURNED_WINDOW_MOVES[:] = []
    mode = str(getattr(payload.design, "subtitle_avoid_burned", "auto") or "auto").lower()
    if mode != "auto":
        return None
    cache = Path(output_dir) / "checkpoint_burned_subtitle.json"
    sig = _burned_band_signature(clips)
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("clips_signature") == sig:
                band = data.get("band")
                _BURNED_PROFILES[:] = list(data.get("profiles") or [])
                if band:
                    print(f"  [SubtitleAvoid] 검출 결과 재사용: y={band['top']}~{band['bottom']}")
                if _BURNED_PROFILES:
                    print(f"  [SubtitleAvoid] 구간별 표본 재사용: {len(_BURNED_PROFILES)}프레임")
                return band
        except (OSError, ValueError, KeyError):
            pass
    try:
        from app.modules.subtitle_region import detect_burned_band

        band = detect_burned_band(
            Path(payload.video_path), clips, payload.design,
            crop_map=crop_map,
            canvas_width=config.canvas_width, canvas_height=config.canvas_height,
        )
    except Exception as e:                       # 검출은 본편을 막지 않는다
        print(f"  [SubtitleAvoid] 검출 실패({e}) — 자막 위치는 종전 그대로입니다")
        return None
    # E18-2 — 구간별 재판정용 표본. 전역 판정과 **따로** 뜬다(전역 표본 구성을 건드리면
    # 이미 승인된 채널의 자막이 움직인다). 실패는 빈 목록 = 구간 보정 없음.
    try:
        from app.modules.subtitle_region import detect_burned_profiles

        _BURNED_PROFILES[:] = detect_burned_profiles(
            Path(payload.video_path), clips, payload.design, crop_map=crop_map,
            canvas_width=config.canvas_width, canvas_height=config.canvas_height)
        if _BURNED_PROFILES:
            print(f"  [SubtitleAvoid] 구간별 표본 {len(_BURNED_PROFILES)}프레임 수집")
    except Exception as e:
        print(f"  [SubtitleAvoid] 구간별 표본 실패({e}) — 구간 보정 없이 진행합니다")
        _BURNED_PROFILES[:] = []
    try:
        cache.write_text(json.dumps({"clips_signature": sig, "band": band,
                                     "profiles": _BURNED_PROFILES},
                                    ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    if band:
        print(f"  [SubtitleAvoid] 원본 자막 띠 검출: y={band['top']}~{band['bottom']} "
              f"(표본 {band['frames']}프레임/{band['clips']}클립, 검출률 {band['hit_ratio']})")
    else:
        print("  [SubtitleAvoid] 원본 자막 띠 없음 — 자막 위치는 종전 그대로입니다")
    return band


def _avoid_burned_margin_v(
    margin_v: int,
    band: dict[str, Any] | None,
    *,
    design: DesignConfig,
    config,
    title_text: str,
    font_size: int,
    label: str = "",
) -> int:
    """검출된 띠를 피한 margin_v. 띠가 없거나 피할 자리가 없으면 입력 그대로.

    `label`: 로그 접두("[TTS] " 등) — 대사 자막·TTS 자막 둘 다 이 함수를 타므로
    stdout 만 보고 어느 줄이 움직였는지 구분하려면 필요하다. 반환값·판정 로직에는
    영향 없다(순수 로그용)."""
    if not band:
        return int(margin_v)
    from app.modules import subtitle_region as _sr

    geom = _sr.band_geometry(design, canvas_width=config.canvas_width,
                             canvas_height=config.canvas_height)
    new_margin, notes = _sr.avoid_margin_v(
        int(margin_v),
        canvas_height=config.canvas_height,
        burned_top=int(band["top"]), burned_bottom=int(band["bottom"]),
        subtitle_height=_sr.estimate_subtitle_height(font_size),
        title_bottom=_sr.estimate_title_bottom(
            design, geom, line_count=_sr.estimate_title_line_count(title_text)),
        band_top=geom.top,
    )
    for n in notes:
        print(f"  {label}{n}")
    return int(new_margin)


def _avoid_burned_windowed(
    segments: list,
    base_margin_v: int,
    *,
    design: DesignConfig,
    config,
    title_text: str,
    font_size: int,
    label: str = "",
) -> list:
    """구간별 원본 자막 회피 — 줄마다 필요한 만큼만 더 올린 **사본**을 돌려준다 (E18-2).

    E17-2 의 전역 판정은 '표본 프레임의 절반 이상에서 걸리는 행'만 띠로 봐서 **몇 초씩만
    뜨는 방송 텔롭을 못 잡는다**(2026-08-24 실측: 한국어 텔롭이 뚜렷한 편의 run_log 에
    `subtitle_avoid_burned` 단계가 아예 없었다). 여기서는 자막 줄이 떠 있는 **그 창 안의**
    표본만으로 다시 판정한다.

    ⚠ 결과는 줄 단위 스타일 `style["y"]` 로 얹는다 — 편집실(v3)이 쓰는 것과 **같은 통로**라
      ASS 조립부를 새로 만들지 않는다. 사람이 y 를 정한 줄은 **건드리지 않는다**(사람이 이긴다).
    ⚠ `final_segments` 자체는 바꾸지 않는다(사본만) — `subtitle_segments.json` 이 움직이면
      편집실이 보는 자료와 오케스트레이터 왕복이 달라진다.
    """
    if not segments or not _BURNED_PROFILES:
        return list(segments or [])
    from app.modules import subtitle_region as _sr

    geom = _sr.band_geometry(design, canvas_width=config.canvas_width,
                             canvas_height=config.canvas_height)
    margins, notes = _sr.per_cue_margins(
        segments, _BURNED_PROFILES, geom,
        base_margin_v=int(base_margin_v),
        canvas_height=config.canvas_height,
        subtitle_height=_sr.estimate_subtitle_height(font_size),
        title_bottom=_sr.estimate_title_bottom(
            design, geom, line_count=_sr.estimate_title_line_count(title_text)))
    for n in notes:
        print(f"  {label}{n}")
    _moved = sum(1 for m in margins if m is not None)
    if _moved:
        _BURNED_WINDOW_MOVES.append(
            {"track": (label.strip() or "자막").strip("[] "), "moved": _moved,
             "lines": len(margins), "base_margin_v": int(base_margin_v)})
    out = []
    for seg, m in zip(segments, margins):
        style = getattr(seg, "style", None)
        if m is None or (isinstance(style, dict) and style.get("y") is not None):
            out.append(seg)                       # 보정 불필요 · 사람이 정한 줄은 그대로
            continue
        ns = SimpleNamespace(**vars(seg))
        merged = dict(style) if isinstance(style, dict) else {}
        merged["y"] = round((config.canvas_height - int(m)) / float(config.canvas_height), 5)
        ns.style = merged
        out.append(ns)
    return out


def _scale_segments_for_speed(segments: list, speed: float) -> list:
    """자막 세그먼트의 출력 시각 ×1/S (E7-2, 구현 지점 A). 순수 — 입력을 바꾸지 않는다.

    영상 배속(video_speed=S)은 렌더 그래프의 concat 직후 setpts/atempo 한 번이고, ASS 는
    그 **뒤**에 입혀지므로 이벤트 시각은 출력 시각이어야 한다. 여기서 사본의
    start_sec/end_sec 만 나눈다 — segments_cache_path(소스 편집 타임라인, 편집실이 읽는
    파일)는 절대 건드리지 않는다. style 등 다른 속성은 그대로 보존(편집실 자막 오버라이드).
    """
    if speed == 1.0:
        return list(segments)
    out = []
    for s in segments:
        ns = SimpleNamespace(**vars(s))
        ns.start_sec = float(s.start_sec) / speed
        ns.end_sec = float(s.end_sec) / speed
        out.append(ns)
    return out


def _flag_low_confidence_segments(
    final_segments: list,
    clips: list,
    meta_path: Path,
    *,
    backend: str | None,
) -> int:
    """E13-2b — 저확신 전사 구간과 겹치는 자막 줄에 **표시 하나**를 얹는다. 반환: 표시한 줄 수.

    텍스트도 시간도 안 바꾼다. 버리지도 않는다 — 실측 저확신 3건 중 2건이 멀쩡한
    한국어였다(`아저씨 이때 좋아요.`). 임계로 자르면 실제 대사가 사라지므로, 편집실
    자막 검수 화면에서 **사람이 먼저 볼 줄**로 표시만 하는 것이 이 기능의 전부다.

    좌표 변환은 자막이 쓴 것과 **같은 함수**(remap_transcript_to_edited_timeline)를 다시
    태워서 얻는다 — 누적 오프셋 수식을 여기에 베껴 두면 둘이 언젠가 어긋난다.
    `tts_only_when_no_orig=False` 는 원본 오디오를 안 쓰는 클립에 clip.subtitle 이
    끼어드는 것을 막는다(전사 구간이 아니다).

    ⚠ elevenlabs 백엔드를 **명시한 실행에서만** 돈다. 미지정·default 실행은 사이드카
    자체가 없어 한 줄도 표시되지 않고, 표시가 없으면 subtitle_segments.json 은 종전과
    바이트까지 동일하다(회귀 0).
    """
    if backend != "elevenlabs" or not final_segments or not clips:
        return 0
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    spans = meta.get("low_confidence_spans") or []
    if not spans:
        return 0

    src: list[SpeechSegment] = []
    for sp in spans:
        try:
            src.append(SpeechSegment(start_sec=float(sp["start_sec"]),
                                     end_sec=float(sp["end_sec"]), text="lc"))
        except (KeyError, TypeError, ValueError):
            continue
    if not src:
        return 0
    edited = remap_transcript_to_edited_timeline(clips, src, tts_only_when_no_orig=False)
    if not edited:
        return 0

    flagged = 0
    for seg in final_segments:
        s0, s1 = float(seg.start_sec), float(seg.end_sec)
        for sp in edited:
            if min(s1, sp.end_sec) - max(s0, sp.start_sec) >= 0.05:
                seg.low_confidence = True
                flagged += 1
                break
    return flagged


def _subtitle_segment_json(seg) -> dict[str, Any]:
    """subtitle_segments.json 한 줄. **표시된 줄에만** low_confidence 키가 붙는다 —
    무조건 붙이면 플래그 없는 채널의 파일까지 모양이 달라진다(회귀 0)."""
    out: dict[str, Any] = {"start_sec": seg.start_sec, "end_sec": seg.end_sec,
                           "text": seg.text}
    if getattr(seg, "low_confidence", False):
        out["low_confidence"] = True
    # E15: AI 가 얹은 줄 강조(style)도 같은 이유로 **있을 때만** 싣는다. 키 이름·모양은
    # v3 그대로다(편집실 오버라이드 경로가 이미 이 파일에 style 을 저장한다 — 캐시 규약
    # 'style 은 남는다'). 강조가 없으면 파일은 종전과 바이트까지 같다.
    _style = getattr(seg, "style", None)
    if _style:
        out["style"] = dict(_style)
    return out


def _build_candidates_lookup(all_candidates: list[dict]) -> dict[tuple[int, int], dict]:
    """all_candidates에서 (chunk_index, candidate_index) → candidate dict 맵 생성.

    LLM이 storyline 출력 시 시간을 변형해도 이 lookup으로 정본 candidate 시간을 복원할 수 있게 한다.
    """
    lookup: dict[tuple[int, int], dict] = {}
    for cand in all_candidates or []:
        ci = int(cand.get("chunk_index", -1))
        cj = int(cand.get("candidate_index", -1))
        if ci >= 0 and cj >= 0:
            lookup[(ci, cj)] = cand
    return lookup


def _dedup_boundary_candidates(
    all_candidates: list[dict], *,
    overlap_threshold: float = 0.5,
) -> dict[tuple[int, int], tuple[int, int]]:
    """청크 경계에서 같은 장면이 양쪽 청크에 따로 등록된 candidate 페어를 감지해 alias 맵 반환.

    예: chunk0_cand9(570~600, 청크 끝에서 잘림) ↔ chunk1_cand0(598~615) → 같은 장면
        → alias[(0, 9)] = (1, 0) 또는 그 반대로 정본 선택 후 alias 등록

    정본 선택 기준:
    1. 더 긴 시간 범위 (청크 경계에서 잘리지 않은 쪽 우선)
    2. 같으면 더 일찍 시작하는 쪽

    Returns:
        alias dict: 중복 candidate (slave) → 정본 candidate (master)
    """
    by_chunk: dict[int, list[dict]] = {}
    for c in all_candidates or []:
        ci = int(c.get("chunk_index", -1))
        if ci < 0:
            continue
        by_chunk.setdefault(ci, []).append(c)

    alias: dict[tuple[int, int], tuple[int, int]] = {}
    chunks_sorted = sorted(by_chunk.keys())
    for i in range(len(chunks_sorted) - 1):
        c1, c2 = chunks_sorted[i], chunks_sorted[i + 1]
        for a in by_chunk[c1]:
            for b in by_chunk[c2]:
                a_s, a_e = float(a.get("start_sec", 0)), float(a.get("end_sec", 0))
                b_s, b_e = float(b.get("start_sec", 0)), float(b.get("end_sec", 0))
                if a_e <= a_s or b_e <= b_s:
                    continue
                lap = max(0.0, min(a_e, b_e) - max(a_s, b_s))
                if lap <= 0:
                    continue
                # 같은 장면 판정: (a) 작은 쪽 클립의 overlap_threshold 이상 겹치거나
                # (b) 절대 1.5초 이상 겹침 (청크 경계 잘린 케이스 대응 — 사용자 사례:
                #     chunk0_cand9(570~600) ↔ chunk1_cand0(598~615), overlap 2초)
                a_dur = a_e - a_s
                b_dur = b_e - b_s
                if lap < min(a_dur, b_dur) * overlap_threshold and lap < 1.5:
                    continue
                # 두 candidate가 같은 장면 → 합친 시간 범위가 정본
                # (청크 경계에서 잘린 쪽이 어느 쪽이든 둘을 합치면 잘리지 않은 전체 장면이 됨)
                merged_start = min(a_s, b_s)
                merged_end = max(a_e, b_e)
                # master는 다음 청크의 candidate (보통 청크 시작이 자연스러운 장면 시작점)
                master, slave = b, a
                m_key = (int(master.get("chunk_index")), int(master.get("candidate_index")))
                s_key = (int(slave.get("chunk_index")), int(slave.get("candidate_index")))
                # master candidate의 시간을 합친 범위로 덮어씀 (in-place — 라운타임 사본)
                master["start_sec"] = merged_start
                master["end_sec"] = merged_end
                alias[s_key] = m_key
                print(
                    f"  [dedup] chunk{s_key[0]} cand{s_key[1]}({slave.get('start_sec')}~{slave.get('end_sec')}) "
                    f"+ chunk{m_key[0]} cand{m_key[1]}({b_s}~{b_e}) "
                    f"→ 합친 정본 chunk{m_key[0]} cand{m_key[1]}({merged_start}~{merged_end}) "
                    f"(overlap {lap:.1f}s, 청크 경계 잘림 보정)"
                )
    return alias


def _resolve_clip_times(
    src: dict,
    candidates_lookup: dict[tuple[int, int], dict],
    boundary_alias: dict[tuple[int, int], tuple[int, int]] | None = None,
) -> dict:
    """LLM 출력의 start_sec/end_sec을 무시하고 candidate에서 lookup해 정본 시간으로 복원.

    - boundary_alias: 청크 경계 중복 candidate를 정본으로 redirect
    - context_extended=true면 candidate.context_extension의 extended_start/end_sec을 적용
    - lookup 실패 시 입력 그대로 폴백
    """
    ci = int(src.get("chunk_index", -1))
    cj = int(src.get("candidate_index", -1))
    if boundary_alias:
        # 청크 경계 dedup: alias가 있으면 정본 candidate로 redirect
        master = boundary_alias.get((ci, cj))
        if master is not None:
            ci, cj = master
    cand = candidates_lookup.get((ci, cj))
    if cand is None:
        return src  # 폴백
    extended = bool(src.get("context_extended"))
    ext = cand.get("context_extension") or {}
    if extended and ext.get("needed"):
        start = float(ext.get("extended_start_sec", cand.get("start_sec", src.get("start_sec", 0.0))))
        end = float(ext.get("extended_end_sec", cand.get("end_sec", src.get("end_sec", 0.0))))
    else:
        start = float(cand.get("start_sec", src.get("start_sec", 0.0)))
        end = float(cand.get("end_sec", src.get("end_sec", 0.0)))
    out = dict(src)
    out["start_sec"] = start
    out["end_sec"] = end
    # 정본 candidate의 chunk_index/candidate_index로도 갱신 (alias가 적용된 경우)
    out["chunk_index"] = ci
    out["candidate_index"] = cj
    # description은 항상 candidate(analyze_chunk) 원본 사용.
    # compose_story 단계의 재작성을 우회해 swap·압축·도식 매핑 등의 변형 위험 차단.
    # (compose_story 프롬프트에도 "그대로 복사" 지시가 있으나 그것을 어겨도 여기서 강제)
    if cand.get("description"):
        out["description"] = cand["description"]
    if "character_focus" not in out and cand.get("characters_in_scene"):
        out["character_focus"] = cand.get("characters_in_scene")
    # visual_essential은 candidate 정본 값을 그대로 통과 (compose_story가 누락해도 보전).
    if "visual_essential" not in out:
        out["visual_essential"] = bool(cand.get("visual_essential", False))
    # tts_draft는 analyze_chunk(candidate)에서 생성. compose_story가 누락하거나
    # 임의로 채워 넣어도 candidate 정본 값으로 덮어쓴다.
    if cand.get("tts_draft"):
        out["tts_draft"] = cand["tts_draft"]
    return out


def _apply_lookup_to_storyline(
    sl: dict,
    candidates_lookup: dict[tuple[int, int], dict],
    boundary_alias: dict[tuple[int, int], tuple[int, int]] | None = None,
) -> dict:
    """storyline dict 안의 모든 클립 노드(hook/build/payoff/highlight 자체)에 _resolve_clip_times 적용.

    저장 시점에 사용 — checkpoint_story.json에 LLM 환각이 남지 않도록 정본으로 덮어쓴다.
    """
    out = dict(sl)
    if sl.get("shorts_type") == "highlight":
        out.update(_resolve_clip_times(sl, candidates_lookup, boundary_alias))
    else:
        st = dict(sl.get("storyline", {}) or {})
        if isinstance(st.get("hook"), dict):
            st["hook"] = _resolve_clip_times(st["hook"], candidates_lookup, boundary_alias)
        if isinstance(st.get("build"), list):
            st["build"] = [_resolve_clip_times(b, candidates_lookup, boundary_alias) for b in st["build"]]
        if isinstance(st.get("payoff"), dict):
            st["payoff"] = _resolve_clip_times(st["payoff"], candidates_lookup, boundary_alias)
        out["storyline"] = st
    return out


def _clips_from_storyline(
    storyline_data: dict,
    fallback_title: str = "",
    candidates_lookup: dict[tuple[int, int], dict] | None = None,
    boundary_alias: dict[tuple[int, int], tuple[int, int]] | None = None,
) -> tuple[list[StoryClip], str]:
    """스토리라인 dict에서 (clips, title_text)를 추출합니다.

    candidates_lookup: (chunk_index, candidate_index) → candidate dict 맵.
    제공되면 LLM 출력의 start_sec/end_sec을 무시하고 candidate 시간으로 복원 (이슈 2·6 해결).
    boundary_alias: 청크 경계 dedup alias (라운드 4) — 같은 장면 양쪽 청크 등록 케이스 통합.
    """
    clips: list[StoryClip] = []

    # 제목 구성 (이모지 제거)
    # 라운드 22: line1·line2 최대 20자 허용 (이전 15자) — renderer가 14~20자에서 sqrt 폰트 자동 축소.
    # 20자 초과 시 LLM `shorten_text` 재작성 시도 → 실패 시 어절 경계 절단 안전판.
    title_line1_raw = _strip_emoji(storyline_data.get("title_line1", ""))
    title_line2_raw = _strip_emoji(storyline_data.get("title_line2", ""))

    # 라운드 22C: 20자 초과 시 LLM 재작성 (의미·뉘앙스 보존)
    if len(title_line1_raw) > 20 or len(title_line2_raw) > 20:
        try:
            _gem = load_gemini_client()
            _shorten = getattr(_gem, "shorten_text", None)
            if callable(_shorten):
                if len(title_line1_raw) > 20:
                    _new1 = _shorten(title_line1_raw, target_chars=20)
                    if _new1 and len(_new1) <= 20 and not _looks_garbled(title_line1_raw, _new1):
                        print(f"  [title-shorten] line1 {len(title_line1_raw)}자 → {len(_new1)}자: {_new1!r}")
                        title_line1_raw = _new1
                    elif _new1:
                        print(f"  [title-shorten] line1 LLM 결과 거부(깨짐/불일치): {_new1!r} → 어절 절단 폴백")
                if len(title_line2_raw) > 20:
                    _new2 = _shorten(title_line2_raw, target_chars=20)
                    if _new2 and len(_new2) <= 20 and not _looks_garbled(title_line2_raw, _new2):
                        print(f"  [title-shorten] line2 {len(title_line2_raw)}자 → {len(_new2)}자: {_new2!r}")
                        title_line2_raw = _new2
                    elif _new2:
                        print(f"  [title-shorten] line2 LLM 결과 거부(깨짐/불일치): {_new2!r} → 어절 절단 폴백")
        except Exception as e:
            print(f"  [WARN] title shorten 실패: {e} — 어절 절단 폴백")

    title_line1 = _enforce_title_line_limit(title_line1_raw, max_chars=20)
    title_line2 = _enforce_title_line_limit(title_line2_raw, max_chars=20)
    if title_line1 and title_line2:
        title_text = f"{title_line1}\n{title_line2}"
    else:
        title_text = storyline_data.get("topic", fallback_title)

    # 시간 고정: LLM이 어떻게 출력했든 candidate 정본 시간으로 복원
    def _resolve(src: dict) -> dict:
        if candidates_lookup:
            return _resolve_clip_times(src, candidates_lookup, boundary_alias)
        return src

    if storyline_data.get("shorts_type") == "highlight":
        resolved = _resolve(storyline_data)
        _hl_dur = resolved["end_sec"] - resolved["start_sec"]
        _hl_extended = bool(resolved.get("context_extended", False))
        print(f"  - highlight 클립 길이 {_hl_dur:.1f}s" + (" (context 확장됨)" if _hl_extended else ""))
        clips.append(StoryClip(
            role="payoff",
            start_sec=resolved["start_sec"],
            end_sec=resolved["end_sec"],
            subtitle=(
                resolved.get("description")
                or storyline_data.get("description")
                or resolved.get("topic", "")
                or storyline_data.get("topic", "")
            ),
            use_original_audio=resolved.get("use_original_audio", True),
            chunk_index=resolved.get("chunk_index", -1),
            candidate_index=resolved.get("candidate_index", -1),
            visual_essential=bool(resolved.get("visual_essential", False)),
            tts_draft=str(resolved.get("tts_draft") or storyline_data.get("tts_draft") or ""),
        ))
    else:
        actual_storyline = storyline_data.get("storyline", {})

        def _make_clip(role: str, src: dict) -> StoryClip:
            # 시간 고정: candidate lookup으로 정본 start/end 복원
            src = _resolve(src)
            return StoryClip(
                role=role,
                start_sec=float(src["start_sec"]),
                end_sec=float(src["end_sec"]),
                subtitle=src.get("description", ""),
                use_original_audio=src.get("use_original_audio", True),
                chunk_index=src.get("chunk_index", -1),
                candidate_index=src.get("candidate_index", -1),
                character_focus=tuple(src.get("character_focus") or []),
                visual_essential=bool(src.get("visual_essential", False)),
                tts_draft=str(src.get("tts_draft") or ""),
            )

        # 라운드 12: 시퀀스블록형 분기 — 같은 sequence_id의 candidate들을 시간순으로 추출
        seq_type = storyline_data.get("sequence_type", "여정몰입형")
        if seq_type == "시퀀스블록형":
            hook = actual_storyline.get("hook")
            sequence_block = actual_storyline.get("sequence_block", []) or []

            if isinstance(hook, dict):
                clips.append(_make_clip("hook", hook))

            block_clips: list[StoryClip] = []
            for ref in sequence_block:
                if not isinstance(ref, dict):
                    continue
                ci = int(ref.get("chunk_index", -1))
                cj = int(ref.get("candidate_index", -1))
                if candidates_lookup is None:
                    continue
                cand = candidates_lookup.get((ci, cj))
                if cand is None:
                    continue
                block_clips.append(StoryClip(
                    role="build",
                    start_sec=float(cand.get("start_sec", 0.0)),
                    end_sec=float(cand.get("end_sec", 0.0)),
                    subtitle=cand.get("description", ""),
                    use_original_audio=True,
                    chunk_index=ci, candidate_index=cj,
                    character_focus=tuple(cand.get("characters_in_scene") or cand.get("character_focus") or []),
                    visual_essential=bool(cand.get("visual_essential", False)),
                    tts_draft=str(cand.get("tts_draft") or ""),
                ))
            block_clips.sort(key=lambda c: c.start_sec)
            # 마지막 clip을 payoff role로 — 자막·렌더 일관성
            if block_clips:
                last = block_clips[-1]
                block_clips[-1] = StoryClip(
                    role="payoff",
                    start_sec=last.start_sec, end_sec=last.end_sec,
                    subtitle=last.subtitle, use_original_audio=last.use_original_audio,
                    pacing_note=last.pacing_note,
                    chunk_index=last.chunk_index, candidate_index=last.candidate_index,
                    character_focus=last.character_focus,
                    visual_essential=last.visual_essential,
                    tts_draft=last.tts_draft,
                )
            clips.extend(block_clips)
            print(f"  - 시퀀스블록형: hook {1 if isinstance(hook, dict) else 0}개 + sequence_block {len(block_clips)}개")
            return clips, title_text  # 기존 hook/build/payoff 분기 건너뜀

        hook = actual_storyline.get("hook")
        hook_preview = actual_storyline.get("hook_preview")
        build_list = actual_storyline.get("build", []) or []
        payoff = actual_storyline.get("payoff")

        # hook_preview 유효성 검증: hook 시간 안 + 길이 ≥ 1초
        valid_preview = False
        if isinstance(hook_preview, dict) and isinstance(hook, dict):
            try:
                hp_s = float(hook_preview["start_sec"])
                hp_e = float(hook_preview["end_sec"])
                h_s = float(hook["start_sec"])
                h_e = float(hook["end_sec"])
                valid_preview = (h_s <= hp_s < hp_e <= h_e) and (hp_e - hp_s >= 1.0)
            except (KeyError, TypeError, ValueError):
                valid_preview = False

        if valid_preview:
            # 케이스 3: hook_preview → build → [hook 본체] → payoff
            # hook 본체와 payoff 시간이 겹치면 자막이 중복(이중/삼중) 표시되므로
            # hook 본체를 생략하고 payoff에 흡수한다 (payoff.start_sec을 hook 시작점까지 확장).
            clips.append(_make_clip("hook", hook_preview))
            for b in build_list:
                clips.append(_make_clip("build", b))

            hp_dur = float(hook_preview["end_sec"]) - float(hook_preview["start_sec"])
            h_s_abs = float(hook["start_sec"]) if hook else 0.0
            h_e_abs = float(hook["end_sec"]) if hook else 0.0
            p_s_abs = float(payoff["start_sec"]) if payoff else float("inf")
            p_e_abs = float(payoff["end_sec"]) if payoff else float("inf")
            overlap = (
                hook is not None and payoff is not None
                and p_s_abs < h_e_abs and h_s_abs < p_e_abs
            )

            if overlap and payoff is not None:
                # hook 본체와 payoff 시간 겹침 → hook 본체 생략, payoff에 흡수
                merged_payoff = dict(payoff)
                merged_payoff["start_sec"] = min(p_s_abs, h_s_abs)
                merged_payoff["end_sec"] = max(p_e_abs, h_e_abs)
                clips.append(_make_clip("payoff", merged_payoff))
                merged_dur = merged_payoff["end_sec"] - merged_payoff["start_sec"]
                print(
                    f"  - hook_preview({hp_dur:.1f}s) + build×{len(build_list)} "
                    f"+ payoff(hook 본체 흡수, {merged_dur:.1f}s) — 자막 중복 방지"
                )
            else:
                # 겹침 없음: hook 본체 처리
                # 라운드 8c — hook_preview와 hook 본체가 같은 candidate (chunk·cand 동일)면
                # 본체 추가 생략. 같은 영역이 두 번 등장하면 자막 없는 영상 길이만 늘어남.
                # 실제 사례 (유미 EP03 스토리라인 3): hook_preview·hook 본체 모두 (2,0) 1194~1215
                # → 이전엔 hook 본체가 build로 끼어 86초 영상 중 자막 6초밖에 없음.
                same_cand = bool(
                    hook is not None
                    and hook.get("chunk_index") == hook_preview.get("chunk_index")
                    and hook.get("candidate_index") == hook_preview.get("candidate_index")
                )
                if hook is not None and not same_cand:
                    clips.append(_make_clip("build", hook))
                if payoff is not None:
                    clips.append(_make_clip("payoff", payoff))
                if same_cand:
                    print(
                        f"  - hook_preview({hp_dur:.1f}s) + build×{len(build_list)} + payoff "
                        f"(hook 본체는 preview와 동일 candidate라 생략)"
                    )
                else:
                    print(
                        f"  - hook_preview({hp_dur:.1f}s) + build×{len(build_list)} "
                        f"+ hook(본체) + payoff (이중 사용)"
                    )
        else:
            # 케이스 1·2 또는 hook_preview 무효: 기존 흐름
            if hook is not None:
                clips.append(_make_clip("hook", hook))
            for b in build_list:
                clips.append(_make_clip("build", b))
            if payoff is not None:
                clips.append(_make_clip("payoff", payoff))
            # 케이스 3 의심 — LLM 가이드 위반 경고
            if hook is not None and payoff is not None and build_list:
                try:
                    b0 = float(build_list[0]["start_sec"])
                    pe = float(payoff["end_sec"])
                    h_s = float(hook["start_sec"])
                    if b0 <= h_s <= pe and not isinstance(hook_preview, dict):
                        print(
                            f"  [WARN] hook이 build/payoff 사이(케이스 3)인데 hook_preview 누락 — build→payoff 점프 발생 가능"
                        )
                except (KeyError, TypeError, ValueError):
                    pass

    return clips, title_text


def _get_audio_duration(path: Path) -> float:
    """ffprobe로 오디오 파일의 재생 시간을 읽습니다."""
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd, "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


@dataclass(frozen=True)
class PipelineInput:
    video_path: Path
    work_title: str
    topic: str
    outdir: Path
    design: DesignConfig = field(default_factory=DesignConfig)
    language: str = "ko"
    previous_episodes_context: str | None = None
    work_context: str | None = None
    srt_path: Path | None = None
    show_subtitles: bool = True
    show_tts_subtitles: bool = True
    # JP 파이프라인(2026-08-13): ai-video 는 '내용까지만' — 텍스트·TTS 는 현지화가 일본어로.
    show_title_overlay: bool = True     # False = 상단 제목·하단 작품명 오버레이 생략
    include_tts_audio: bool = True      # False = TTS 오디오 믹스 생략(원본 오디오만).
                                        #   tts_subtitles.ass 는 계속 생성 — 현지화 타이밍 원료
    max_shorts: int = 3
    skip_research: bool = False
    episode: int | None = None
    skip_intro_sec: float = 0.0
    skip_credits_sec: float = 0.0
    # 출력 라우드니스 정규화 목표(LUFS). None 이면 비활성(A/B 대조군). RenderInputs 로 전달.
    loudness_target_lufs: float | None = -14.0
    # 사람이 직전 결과를 반려한 사유. 영상 분석(analyze_chunk)과 스토리 구성 프롬프트에
    # '재작업 지시'로 주입된다. None 이면 프롬프트가 종전과 완전히 동일하다.
    reject_note: str | None = None
    # 작품별 편집 지침(권리사 가이드/운영 지시) — app/modules/editorial.py 가 계약 정본.
    # 청크 분석에는 태깅·상세 기술 지시로, 스토리 구성에는 하드 필터·랭킹 편향·문체로
    # 주입되고, avoid 태깅된 후보는 스토리 구성 전에 코드에서도 결정적으로 걸러진다.
    # None 이면 프롬프트가 종전과 완전히 동일하다(reject_note 와 같은 규약).
    editorial: dict | None = None
    # 실행 단위 지시의 **원문**(--editorial-run-json 파싱본). 프롬프트에는 위 editorial
    # (병합본)만 쓰이고, 이건 run_log 표시용 — 검수 카드가 "이번 편에만 얹은 지시"를
    # 상시 지침과 구분해 보여준다(운영 결정 8/20). None 이면 기록 안 함.
    editorial_run: dict | None = None
    # 관제 편집실이 고친 제목·구간 JSON 경로(edit_overrides/v1). None 이면 종전과 동일.
    # 이 값이 있으면 체크포인트보다 **사람 입력이 이긴다** — app/modules/edit_overrides.py.
    edit_overrides_path: Path | None = None
    # E11(2026-08-22) 자막 전사 백엔드 — None(미지정)·"default"=내장 Whisper 경로 그대로,
    # "elevenlabs"=ElevenLabs Scribe STT. 허용값 밖은 CLI argparse choices 가 이미 막는다.
    # **미지정이면 파이프라인은 종전과 완전히 동일하다**(회귀 0 — auto_update 배포 조건).
    transcribe_backend: str | None = None
    # E15(2026-08-23) 스타일 구성 — True 면 스토리 구성 뒤 AI 가 편 단위 연출(효과 텍스트·
    # 자막 강조·스티커·시간대별 제목·회전·내레이션 톤)을 구성해 그대로 렌더한다.
    # **False(기본)면 style 단계가 통째로 없다** — 체크포인트를 쓰지도 읽지도 않고
    # 산출은 종전과 한 글자도 다르지 않다(회귀 0).
    style_compose: bool = False
    # 채널(CLI)이 **명시한** design 필드 이름들. AI 스타일 플랜이 덮어도 되는지 가리는 데만
    # 쓴다(§5 우선순위: 편집실 > 채널 명시 > AI > 기본값). '기본값과 다른가'로 판정하면
    # 채널이 기본값과 같은 값을 일부러 명시한 경우를 구분하지 못해 사람 결정이 덮인다.
    design_explicit_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PipelineOutput:
    output_videos: list[Path]
    edit_plan_path: Path
    run_log_path: Path

    @property
    def output_video(self) -> Path:
        """하위 호환: 첫 번째 영상 경로 반환."""
        return self.output_videos[0]


# ─────────────────────────────────────────────────────────
# 메인 파이프라인 (10단계)
# ─────────────────────────────────────────────────────────
def run_pipeline(payload: PipelineInput, from_step: str | None = None, job_id: str | None = None) -> PipelineOutput:
    print("=" * 60)
    print("파이프라인 시작")
    print("=" * 60)

    start_time = time.time()
    config = AppConfig()
    paths = Paths(app_root=Path(__file__).resolve().parent)

    # ═══════════════════════════════════════
    # [1/15] 초기화
    # ═══════════════════════════════════════
    print("\n[1/15] 초기화 중...")
    if job_id:
        output_dir = payload.outdir / job_id
        if not output_dir.exists():
            raise ValueError(f"Job ID {job_id}의 디렉토리를 찾을 수 없습니다: {output_dir}")
        print(f"  - 기존 작업 재개: {job_id}")
        print(f"  - 출력 디렉토리: {output_dir}")
        run_log_path = output_dir / "run_log.json"
        if run_log_path.exists():
            run_log = json.loads(run_log_path.read_text(encoding="utf-8"))
            # 재개 실행에 지침이 (새로) 실려 왔으면 기록을 이번 실행 기준으로 갱신 —
            # 반려 재생성이 이 경로를 타므로, 실행 단위 지시가 여기서 provenance 에 남는다.
            if payload.editorial:
                run_log.setdefault("input", {})["editorial"] = payload.editorial
            if payload.editorial_run:
                run_log.setdefault("input", {})["editorial_run"] = payload.editorial_run
        else:
            run_log = {
                "job_id": job_id,
                "input": {
                    "video_path": str(payload.video_path),
                    "work_title": payload.work_title,
                    "topic": payload.topic,
                    "language": payload.language,
                    # 편집 지침(병합 후 실제 적용본) — 검수함이 "이 run 에 어떤 지침이
                    # 적용됐는지"를 여기서 읽는다. 지침 없는 run 은 키 자체가 없다.
                    **({"editorial": payload.editorial} if payload.editorial else {}),
                    # 실행 단위 지시 원문 — 검수 카드가 상시 지침과 구분해 보여준다(8/20)
                    **({"editorial_run": payload.editorial_run} if payload.editorial_run else {}),
                },
                "steps": [],
            }
    else:
        safe_title = payload.work_title.replace(" ", "_")
        # run_id 충돌 방지 (2026-08 맥2·맥4 실측):
        # 접미가 2자리(256가지)면 작품당 run 이 쌓일수록 충돌이 필연이었다
        # (참교육 8번째 run, 산지직송 11번째 run 에서 발생). 상태 파일·DB·검수함
        # 업로더가 모두 job_id 를 키로 쓰기 때문에, 충돌하면 신규 장면이 옛 run 의
        # 기록에 가려져 에러 없이 조용히 누락된다. 8자리로 확대해 실질 소멸시킨다.
        # 아래 exist_ok=False 재시도는 같은 outdir 안의 충돌만 잡는 최소 가드이며,
        # 옛 job 폴더가 정리된 뒤의 충돌은 잡지 못한다 → 러너 쪽 확정 시점 가드가 별도로 필요.
        for _attempt in range(8):
            job_id = f"{safe_title}_{uuid.uuid4().hex[:8]}"
            output_dir = payload.outdir / job_id
            try:
                output_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError(f"job_id 발급 실패: {safe_title} 의 고유 job_id 를 8회 시도 안에 얻지 못했습니다.")
        # PR-5c-3/4: 제거된 단계의 옛 checkpoint 자동 삭제 (마이그레이션).
        # exclusion → chunk_intro_credits_ranges (PR-2), tts_plan → storyline.tts_cues (PR-4),
        # transcribe → chunk_transcripts 슬라이스 (PR-5c-4, full_audio.json 사라짐).
        for _legacy in ("checkpoint_exclusion.json", "checkpoint_tts_plan.json", "full_audio.json"):
            _legacy_path = output_dir / _legacy
            if _legacy_path.exists():
                try:
                    _legacy_path.unlink()
                    print(f"  [migrate] 옛 체크포인트 제거: {_legacy}")
                except OSError:
                    pass
        run_log = {
            "job_id": job_id,
            "input": {
                "video_path": str(payload.video_path),
                "work_title": payload.work_title,
                "topic": payload.topic,
                "language": payload.language,
                # 편집 지침(병합 적용본 + 실행 단위 원문) — 위 재개 분기와 같은 규약.
                **({"editorial": payload.editorial} if payload.editorial else {}),
                **({"editorial_run": payload.editorial_run} if payload.editorial_run else {}),
            },
            "steps": [],
        }
        print(f"  - Job ID: {job_id}")
        print(f"  - 출력 디렉토리: {output_dir}")
    run_log.setdefault("provenance", build_provenance(config))
    print("[OK] 초기화 완료")

    # ═══════════════════════════════════════
    # [2/15] 작품 자동 리서치
    # ═══════════════════════════════════════
    checkpoint_research = output_dir / "checkpoint_research.json"
    cast_images: list[CharacterInfo] = []
    # 라운드 10: research raw_data를 함수 단위로 보관 — sub_style 자동 선택(genre_tag) 등에 사용.
    research_raw_data: dict = {}

    if not payload.skip_research and not payload.work_context:
        if checkpoint_research.exists():
            print("\n[2/15] 작품 리서치 로드 중... (체크포인트)")
            _rdata = json.loads(checkpoint_research.read_text(encoding="utf-8"))
            research_raw_data = _rdata.get("raw_data", {}) or {}
            payload = replace(payload,
                work_context=_rdata.get("work_context", ""),
                previous_episodes_context=_rdata.get("episodes_context") or payload.previous_episodes_context,
            )
            # 캐스트 이미지 복원
            for ci in _rdata.get("cast_images", []):
                img_p = Path(ci["image_path"]) if ci.get("image_path") else None
                cast_images.append(CharacterInfo(
                    character_name=ci.get("character_name", ""),
                    actor_name=ci.get("actor_name", ""),
                    role_description=ci.get("role_description", ""),
                    image_path=img_p if img_p and img_p.exists() else None,
                    image_url=ci.get("image_url"),
                ))
            print(f"  리서치 로드 완료 ({len(cast_images)}명 캐릭터)")
        else:
            print("\n[2/15] 작품 자동 리서치 중...")
            research_start = time.time()
            gemini = load_gemini_client()
            research = research_work(payload.work_title, payload.episode, gemini)
            research_elapsed = time.time() - research_start

            if research.work_context:
                research_raw_data = research.raw_data or {}
                payload = replace(payload,
                    work_context=research.work_context,
                    previous_episodes_context=research.episodes_context or payload.previous_episodes_context,
                )
                print(f"  시놉시스: {research.work_context[:80]}...")
                print(f"  등장인물: {len(research.characters)}명")

                # TMDb 배우 이미지 다운로드
                import os
                tmdb_key = os.environ.get("TMDB_API_KEY")
                if tmdb_key:
                    from app.modules.tmdb_client import download_cast_images as _dl_cast
                    research_dir = output_dir / "_research"
                    cast_images = _dl_cast(
                        research.raw_data.get("characters", []),
                        research_dir,
                        tmdb_key,
                    )
                else:
                    cast_images = list(research.characters)
                    print("  [TMDb] TMDB_API_KEY 미설정 — 배우 이미지 없이 진행")

                # 체크포인트 저장
                checkpoint_research.write_text(json.dumps({
                    "work_context": research.work_context,
                    "episodes_context": research.episodes_context,
                    "raw_data": research.raw_data,
                    "sources": research.sources,
                    "cast_images": [
                        {
                            "character_name": ci.character_name,
                            "actor_name": ci.actor_name,
                            "role_description": ci.role_description,
                            "image_path": str(ci.image_path) if ci.image_path else None,
                            "image_url": ci.image_url,
                        }
                        for ci in cast_images
                    ],
                }, ensure_ascii=False, indent=2), encoding="utf-8")

                run_log["steps"].append({"step": "research", "elapsed": research_elapsed,
                                         "characters": len(cast_images)})
                print(f"[OK] 작품 리서치 완료 (소요 시간: {research_elapsed:.1f}초)")
            else:
                print("  [WARN] 리서치 결과 없음 — 작품 정보 없이 진행")
    elif payload.work_context:
        print("\n[2/15] 작품 리서치 건너뜀 (수동 work_context 제공)")
    else:
        print("\n[2/15] 작품 리서치 건너뜀 (--no-research)")

    # 단계별 실행 플래그 (PR-5c-4: transcribe 제거 → 13단계)
    # PR-2 chunk_intro_credits_ranges 가 exclusion 대체, PR-4 storyline.tts_cues 가 tts_plan 대체,
    # PR-3 chunk_transcribe 가 transcribe 의 전사 부분을 대체 (chunk_transcripts 슬라이스).
    step_order = [
        "init",             # 0  -> [1/13]
        "research",         # 1  -> [2/13]
        "probe",            # 2  -> [3/13]
        "proxy",            # 3  -> [4/13]
        "chunk",            # 4  -> [5/13]
        "character_index",  # 5  -> [6/13]
        "chunk_transcribe", # 6  -> [7/13]
        "gemini",           # 7  -> [8/13]
        "graph",            # 8  -> [9/13]
        "story",            # 9  -> [10/13]
        "silence_cut",      # 10 -> [11/13]
        # E15(2026-08-23): AI 연출 구성. **--style-compose 를 준 실행에만 실제로 돈다** —
        # 미지정이면 이 단계는 아무것도 하지 않고 체크포인트를 쓰지도 읽지도 않는다(회귀 0).
        # 자리가 여기인 이유: 앵커 좌표의 기준인 최종 클립이 silence_cut·클램프·중복제거를
        # 다 지나야 확정되고, TTS 톤은 resources 의 합성보다 앞서야 소리에 반영된다.
        "style",            # 11
        "resources",        # 12
        "render",           # 13
        "validate",         # 14 -> 종료
    ]
    # PR-5b: 매직 넘버 → step_idx 매핑. 모든 `start_idx <= N` 비교를 단계명 기반으로 전환.
    step_idx: dict[str, int] = {name: i for i, name in enumerate(step_order)}
    # PR-5c-4: 제거된 단계의 from_step 입력 하위 호환 — 가까운 단계로 redirect.
    _legacy_step_alias = {
        "exclusion": "chunk",
        "transcribe": "silence_cut",
        "tts_plan": "silence_cut",
    }
    if from_step in _legacy_step_alias:
        _orig_from = from_step
        from_step = _legacy_step_alias[from_step]
        print(f"\n[WARN] '{_orig_from}' 단계가 제거됨 — '{from_step}' 단계로 redirect 합니다.")
    if from_step:
        start_idx = step_order.index(from_step)
        print(f"\n[WARN] {from_step} 단계부터 재시작합니다.")
    else:
        start_idx = 0

    # 편집실 오버라이드는 여기서 한 번만 읽고 검증한다 — 계약 위반이면 무거운 단계를
    # 돌기 전에 즉시 실패시킨다(사람이 고친 값이 조용히 무시된 채 영상이 나가는 것이 최악).
    _edit_overrides = load_edit_overrides(payload.edit_overrides_path)
    # v3 images(F-408) 파일 검증 — file 은 run_dir(=output_dir) 상대 경로 계약이다.
    # 없는 파일·계약 밖 파일은 무거운 단계를 돌기 전에 여기서 fail-loud (스토리지
    # 다운로드는 오케스트레이터 어댑터 몫 — 여기 도달했으면 파일이 있어야 정상).
    _edit_images = resolve_image_files(_edit_overrides, output_dir)
    if _edit_overrides:
        print(f"\n[edit] 편집실 오버라이드 로드: {payload.edit_overrides_path}")
        if _edit_images:
            print(f"[edit] 이미지 오버레이 {len(_edit_images)}건 파일 검증 완료")

    # ═══════════════════════════════════════
    # [3/15] 미디어 프로브
    # ═══════════════════════════════════════
    checkpoint_probe = output_dir / "checkpoint_probe.json"
    if start_idx <= step_idx["probe"] and checkpoint_probe.exists() and from_step != "probe":
        print("\n[3/15] 미디어 정보 로드 중...")
        probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
        from app.modules.media_probe import MediaInfo
        media_info = MediaInfo(**probe_data)
        print(f"  - 영상 길이: {media_info.duration_sec:.1f}초")
        print(f"  - 해상도: {media_info.width}x{media_info.height}")
        print(f"  - FPS: {media_info.fps:.2f}")
        print(f"  - 오디오: {'있음' if media_info.has_audio else '없음'}")
        print("[OK] 미디어 정보 로드 완료 (체크포인트에서)")
    elif start_idx <= step_idx["probe"]:
        print("\n[3/15] 미디어 정보 수집 중...")
        probe_start = time.time()
        media_info = probe_media(payload.video_path)
        probe_elapsed = time.time() - probe_start
        probe_dict = media_info.__dict__.copy()
        probe_dict["path"] = str(probe_dict["path"])
        run_log["steps"].append({"step": "probe", "result": probe_dict})
        checkpoint_probe.write_text(json.dumps(probe_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  - 영상 길이: {media_info.duration_sec:.1f}초")
        print(f"  - 해상도: {media_info.width}x{media_info.height}")
        print(f"  - FPS: {media_info.fps:.2f}")
        print(f"  - 오디오: {'있음' if media_info.has_audio else '없음'}")
        print(f"[OK] 미디어 프로브 완료 (소요 시간: {probe_elapsed:.1f}초)")
    else:
        if not checkpoint_probe.exists():
            raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_probe}")
        probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
        from app.modules.media_probe import MediaInfo
        media_info = MediaInfo(**probe_data)

    # ═══════════════════════════════════════
    # [4/15] 프록시 영상 생성
    # ═══════════════════════════════════════
    proxy_video_path = output_dir / f"{payload.work_title}_480.mp4"
    if not proxy_video_path.exists():
        print("\n[4/15] 분석용 프록시 영상 생성 중...")
        proxy_start = time.time()
        ffmpeg_exe = find_ffmpeg_command("ffmpeg")
        subprocess.run([
            ffmpeg_exe, '-y', '-i', str(payload.video_path.resolve()),
            '-vf', 'scale=-2:480,fps=4',
            '-fps_mode', 'cfr',
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '26',
            '-c:a', 'aac', '-ac', '1', '-ar', '22050',
            '-threads', '4',
            str(proxy_video_path)
        ], check=True, capture_output=True)
        proxy_elapsed = time.time() - proxy_start
        print(f"[OK] 프록시 영상 생성 완료 (소요 시간: {proxy_elapsed:.1f}초)")
    else:
        print("\n[4/15] 프록시 영상 이미 존재 — 건너뜀")

    # ═══════════════════════════════════════
    # [chunk] 청크 분할 (PR-5c-3: 5단계 exclusion 제거 — 사용자 명시 skip 인자만 사용)
    # ═══════════════════════════════════════
    # 인트로/크레딧 자동 감지는 8단계 chunk_intro_credits_ranges 가 영상 분석으로 직접 처리 (PR-2).
    # 사용자가 --skip-intro / --skip-credits 로 명시한 시간만 청크 분할에서 잘라낸다.
    _content_start = float(payload.skip_intro_sec or 0)
    _content_end = (
        float(media_info.duration_sec) - float(payload.skip_credits_sec)
        if payload.skip_credits_sec
        else float(media_info.duration_sec)
    )
    print("\n[chunk] 영상 청크 분할 중...")
    chunks = build_chunks(
        proxy_video_path,
        media_info.duration_sec,
        config.chunk_seconds,
        config.chunk_overlap,
        content_start_sec=_content_start,
        content_end_sec=_content_end,
    )
    print(f"  - 총 {len(chunks)}개 청크 생성")
    if _content_start > 0 or _content_end < media_info.duration_sec:
        print(f"  - 사용자 skip 인자 적용: {_content_start:.1f}s ~ {_content_end:.1f}s")

    # 분할(=재인코딩)은 split_path 를 실제로 쓰는 단계까지만 — chunk_transcribe(6)·gemini(7).
    # 그 뒤 단계로 재개하면 두 단계 모두 체크포인트를 읽으므로 분할본이 쓰이지 않는데,
    # 종전엔 무조건 돌아 매 재렌더마다 전 청크를 libx264 로 다시 인코딩했다
    # (4시간물 ≈ 34청크 = 수십 분). 편집실의 '고치고 → 보고 → 또 고치고' 루프가
    # 성립하려면 이 낭비를 없애야 한다(2026-08-16). 게다가 분할본은 gemini 분석 직후
    # 삭제되므로(아래 2309 근처) 재개 시점엔 어차피 파일이 남아 있지도 않다.
    if start_idx <= step_idx["gemini"]:
        split_chunks = []
        for i, chunk in enumerate(chunks, 1):
            print(f"    청크 {i} 분할 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
            split_path, actual_start_sec = split_video_chunk(
                proxy_video_path,
                chunk.start_sec,
                chunk.end_sec,
            )
            split_chunk = replace(chunk, split_path=split_path, actual_start_sec=actual_start_sec)
            split_chunks.append(split_chunk)
            print(f"      → {split_path.name} 생성 완료 (실제 시작: {actual_start_sec:.2f}초)")

        chunks = split_chunks
        print("[OK] 청크 분할 완료")
    else:
        print(f"[SKIP] 청크 분할 생략 — {from_step} 단계 재개(분할본을 쓰는 단계를 이미 지남)")

    # ═══════════════════════════════════════
    # [7/15] 인물 등장 인덱스 (face_id 사전 패스)
    # ═══════════════════════════════════════
    # 프록시 영상을 일정 간격으로 샘플링하여 등장 인물별 구간을 미리 산출.
    # 결과는 chunk별로 필터링되어 Gemini analyze_chunk 페이로드에 첨부된다.
    character_appearances: list[dict[str, Any]] = []
    checkpoint_char_idx = output_dir / "checkpoint_character_index.json"
    if checkpoint_char_idx.exists() and from_step != "character_index":
        try:
            character_appearances = json.loads(checkpoint_char_idx.read_text(encoding="utf-8"))
            print(f"\n[7/15] 인물 등장 인덱스 로드 ({len(character_appearances)}개 구간)")
        except Exception as e:
            print(f"\n[7/15] 인물 등장 인덱스 로드 실패: {e} — 새로 생성")
            character_appearances = []

    if not character_appearances and cast_images and payload.design.enable_face_recognition:
        try:
            from app.modules.face_id import FaceIdentifier
            print("\n[7/15] 인물 등장 인덱스 생성 중 (face_id 사전 패스)...")
            char_idx_start = time.time()
            _fi_pre = FaceIdentifier()
            _fi_pre.build_references(cast_images)
            if _fi_pre.references:
                character_appearances = _fi_pre.build_appearance_index(
                    proxy_video_path,
                    sample_interval_sec=2.0,
                )
                checkpoint_char_idx.write_text(
                    json.dumps(character_appearances, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  → {len(character_appearances)}개 등장 구간 (소요 시간: {time.time() - char_idx_start:.1f}초)")
            else:
                print("  [WARN] 유효한 face 레퍼런스 없음 — 인물 인덱스 생략")
        except ImportError:
            print("  [WARN] deepface 미설치 — 인물 인덱스 생략")
        except Exception as e:
            print(f"  [WARN] 인물 인덱스 생성 실패: {e} — 인덱스 없이 진행")
            character_appearances = []

    # ═══════════════════════════════════════
    # [chunk_transcribe] 청크별 transcript 선행 생성 (PR-3 신규)
    # ═══════════════════════════════════════
    # 8단계 analyze_chunk 에 transcript_segments 를 SRT/Whisper 구분 없이 항상 제공.
    # SRT 있으면 parse_subtitle 후 청크별 슬라이스, 없으면 각 chunk.split_path 에서 Whisper.
    # PR-3 시점에서는 step_order 에 추가하지 않음 (매직 넘버 18곳 일괄 정비는 PR-5 에서).
    # 캐시 무효화 트리거: from_step in (chunk, character_index, gemini) 또는 캐시 파일 없음.
    checkpoint_chunk_tr = output_dir / "checkpoint_chunk_transcripts.json"
    chunk_transcripts: list[dict] = []
    # "gemini" 제외: gemini 재개 시 청크는 동일 경계로 재분할되므로 SRT 기반 chunk_transcripts 는
    # 그대로 유효하다. 과거엔 "gemini" 가 무효화 대상이라 캐시 로드를 막았는데, 재생성 가드
    # (start_idx <= chunk_transcribe)는 gemini 가 뒤 단계라 False → 전사도 재실행 안 됨 → 빈 상태로
    # 떨어져 자막이 candidate.transcript 폴백(1줄/clip)으로 깨지는 버그가 있었다.
    _chunk_tr_invalidate = from_step in ("chunk", "character_index")
    # E11: 백엔드를 바꿔 재개하면 이전 백엔드의 전사가 캐시에서 살아나면 안 된다 —
    # 사람은 일레븐랩스로 바꿨다고 믿는데 종전 전사가 그대로 발행된다. 백엔드는
    # 사이드카에만 적는다(플래그 미지정 실행은 파일을 쓰지도 읽지도 않는다 = 회귀 0).
    checkpoint_chunk_tr_meta = output_dir / "checkpoint_chunk_transcripts_meta.json"
    if payload.transcribe_backend is not None:
        _prev_backend = None
        if checkpoint_chunk_tr_meta.exists():
            try:
                _prev_backend = json.loads(
                    checkpoint_chunk_tr_meta.read_text(encoding="utf-8")).get("transcribe_backend")
            except Exception:
                _prev_backend = None
        if _prev_backend is not None and _prev_backend != payload.transcribe_backend:
            print(f"\n[chunk_transcribe] 백엔드 변경 {_prev_backend} → "
                  f"{payload.transcribe_backend} — 캐시 무효화 후 재전사")
            _chunk_tr_invalidate = True
    if checkpoint_chunk_tr.exists() and not _chunk_tr_invalidate:
        try:
            _ctr_data = json.loads(checkpoint_chunk_tr.read_text(encoding="utf-8"))
            chunk_transcripts = _deserialize_chunk_transcripts(_ctr_data)
            _total_segs = sum(len(ct.get("segments", [])) for ct in chunk_transcripts)
            print(f"\n[chunk_transcribe] 청크별 전사 로드 완료 ({len(chunk_transcripts)}개 청크, {_total_segs}개 segment)")
        except Exception as e:
            print(f"\n[chunk_transcribe] 캐시 로드 실패: {e} — 새로 생성")
            chunk_transcripts = []

    if not chunk_transcripts and start_idx <= step_idx["chunk_transcribe"]:
        print("\n[chunk_transcribe] 청크별 transcript 생성 중...")
        _ctr_start = time.time()
        _char_names = list(dict.fromkeys(
            [ci.character_name for ci in cast_images if ci.character_name]
            + [ci.actor_name for ci in cast_images if ci.actor_name]
        )) if cast_images else []
        _low_conf_spans: list[dict[str, Any]] = []
        # E17: 토큰 만료로 내장 전사에 내려갔는지 — run_log·사이드카에 **실제로 쓴**
        # 백엔드를 적어야 키를 갱신한 다음 실행이 캐시를 무효화하고 다시 전사한다.
        _tr_backends: list[str] = []
        chunk_transcripts = transcribe_chunks(
            chunks=chunks,
            srt_path=payload.srt_path,
            low_confidence_out=_low_conf_spans,
            effective_backend_out=_tr_backends,
            work_title=payload.work_title,
            character_names=_char_names,
            work_context=payload.work_context,
            audio_workdir=output_dir,
            backend=payload.transcribe_backend,
            language=payload.language,
        )
        _tr_effective = _tr_backends[-1] if _tr_backends else payload.transcribe_backend
        _tr_fell_back = (payload.transcribe_backend is not None
                         and _tr_effective != (payload.transcribe_backend or "default"))
        checkpoint_chunk_tr.write_text(
            json.dumps(_serialize_chunk_transcripts(chunk_transcripts), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _total_segs = sum(len(ct.get("segments", [])) for ct in chunk_transcripts)
        _branch = ("SRT" if payload.srt_path else
                   ("ElevenLabs" if _tr_effective == "elevenlabs" else "Whisper"))
        print(f"  → [{_branch}] {len(chunk_transcripts)}개 청크, 총 {_total_segs}개 segment (소요 시간: {time.time()-_ctr_start:.1f}초)")
        # E11: 백엔드를 명시한 실행만 기록한다 — 미지정 실행은 run_log·산출 파일이
        # 종전과 한 글자도 달라지지 않아야 한다(auto_update 배포 조건).
        if payload.transcribe_backend is not None:
            _tr_entry: dict[str, Any] = {
                "step": "chunk_transcribe",
                # E17: 요청값이 아니라 **실제로 쓴** 백엔드다(폴백이면 아래 두 키가 붙는다).
                "transcribe_backend": _tr_effective,
                "source": "srt" if payload.srt_path else "stt",
                "chunks": len(chunk_transcripts),
                "segments": _total_segs,
                "elapsed_sec": round(time.time() - _ctr_start, 1),
            }
            if _tr_fell_back:
                _tr_entry["transcribe_backend_requested"] = payload.transcribe_backend
                _tr_entry["transcribe_fallback_reason"] = "elevenlabs_auth_expired"
            if payload.transcribe_backend == "elevenlabs" and not payload.srt_path:
                # 분 단위 과금 — audio_duration_secs 가 나중 정산의 근거다.
                from app.modules.stt_elevenlabs import usage_summary as _el_stt_usage
                _u = _el_stt_usage()
                # E17: 만료로 한 건도 못 보냈으면 0 짜리 정산 블록은 남기지 않는다
                # (성공 요청이 있었다면 그만큼은 실제로 과금됐으니 그대로 남긴다).
                if _u["requests"]:
                    _tr_entry["elevenlabs_stt"] = _u
                    print(f"  → [ElevenLabs STT] 요청 {_u['requests']}건"
                          f"(keyterms {_u['requests_with_keyterms']}건/{_u['keyterms_sent']}개) · "
                          f"오디오 {_u['audio_duration_secs']:.1f}s · "
                          f"추정 ${_u['estimated_usd']:.5f}")
                    if _u["dropped_language_escape"]:
                        print(f"  → [ElevenLabs STT] 언어 이탈로 버린 줄 "
                              f"{len(_u['dropped_language_escape'])}건 (run_log 에 전문 보존)")
                    if _u["normalized"]:
                        print(f"  → [ElevenLabs STT] 표기 보정 "
                              f"{sum(_u['normalized'].values())}건 {_u['normalized']}")
            run_log["steps"].append(_tr_entry)
            # E13-2b: 저확신 구간은 자막 매핑 단계(한참 뒤)가 읽는다 — 사이드카에 남겨야
            # `--from-step resources` 재개(전사 단계를 건너뛴다)에서도 표시가 유지된다.
            # E17: 사이드카에도 **실제로 쓴** 백엔드를 적는다 — 토큰을 갱신한 다음 실행이
            # 'default → elevenlabs' 변경으로 읽고 캐시를 무효화해 다시 전사한다.
            _meta: dict[str, Any] = {"transcribe_backend": _tr_effective}
            if _low_conf_spans:
                _meta["low_confidence_spans"] = _low_conf_spans[:2000]
                _meta["low_confidence_count"] = len(_low_conf_spans)
            checkpoint_chunk_tr_meta.write_text(
                json.dumps(_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ═══════════════════════════════════════
    # [8/15] Gemini 분석 (바이럴 최적화)
    # ═══════════════════════════════════════
    checkpoint_gemini = output_dir / "checkpoint_gemini.json"
    if start_idx <= step_idx["gemini"] and checkpoint_gemini.exists() and from_step != "gemini":
        print("\n[8/15] Gemini 분析 결과 로드 중...")
        gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
        all_candidates = gemini_data["all_candidates"]
        chunk_meta_list = gemini_data.get("chunk_meta", [])
        print(f"  - 총 {len(all_candidates)}개 후보 모멘트, chunk_meta {len(chunk_meta_list)}건")
        print("[OK] Gemini 분석 결과 로드 완료 (체크포인트에서)")
    elif start_idx <= step_idx["gemini"]:
        print("\n[8/15] Gemini 분석 준비 중...")
        gemini = load_gemini_client()
        print("[OK] Gemini 클라이언트 로드 완료")

        print("\n[8/15] Gemini 분석 진행 중...")
        all_candidates: list[dict[str, Any]] = []
        chunk_meta_list: list[dict[str, Any]] = []
        gemini_start = time.time()
        previous_analyses: list[dict[str, Any]] = []

        # 진단용 보존: Gemini 원본 응답(상대 시간 그대로)을 별도 파일에 즉시 기록.
        # checkpoint_gemini.json은 오프셋 적용 후 데이터만 들어가고, run_log는 종료 시점에
        # _slim_run_log()로 응답 본문이 카운트만 남고 날아가므로, 원본 추적 불가 상태가 된다.
        # 이 파일은 어떤 후속 단계에서도 덮어쓰지 않는다 (gemini 단계 재실행 시에만 리셋).
        gemini_raw_log_path = output_dir / "run_log_gemini.json"
        gemini_raw_log_path.write_text(
            json.dumps({"job_id": job_id, "chunks": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # PR-3: chunk_transcripts (chunk_transcribe 단계의 출력) 을 인덱스 맵으로 변환.
        # SRT/Whisper 구분 없이 통합. 빈 리스트면 자막 없는 케이스 (analyze_chunk 가 "없음" 처리).
        chunk_transcripts_by_idx = {
            int(ct.get("chunk_index", -1)): list(ct.get("segments") or [])
            for ct in chunk_transcripts
        }
        _total_segs = sum(len(v) for v in chunk_transcripts_by_idx.values())
        if _total_segs:
            _branch = ("SRT" if payload.srt_path else
                       ("ElevenLabs" if payload.transcribe_backend == "elevenlabs" else "Whisper"))
            print(f"  - chunk_transcripts [{_branch}] {_total_segs}개 segment → analyze_chunk 입력")

        for idx, chunk in enumerate(chunks, 1):
            print(f"  청크 {idx}/{len(chunks)} 분석 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
            chunk_start = time.time()

            split_path = chunk.split_path if chunk.split_path else None
            scenes = detect_scenes(split_path, media_info.fps, chunk.end_sec - chunk.start_sec)
            scene_boundaries = [scene.start_sec for scene in scenes]

            # PR-3: 청크별 transcript 는 chunk_transcripts 에서 직접 (이미 청크 범위 슬라이스됨)
            chunk_transcript_segs = chunk_transcripts_by_idx.get(chunk.index, [])


            # face_id 사전 인식 결과를 chunk 범위로 필터링하고 0초 기준 상대 시간으로 변환
            chunk_offset = (
                chunk.actual_start_sec
                if getattr(chunk, "actual_start_sec", None) is not None
                else chunk.start_sec
            )
            chunk_appearances: list[dict[str, Any]] = []
            for _ap in character_appearances:
                if _ap["end_sec"] <= chunk.start_sec or _ap["start_sec"] >= chunk.end_sec:
                    continue
                _s = max(_ap["start_sec"], chunk.start_sec) - chunk_offset
                _e = min(_ap["end_sec"], chunk.end_sec) - chunk_offset
                if _e <= _s:
                    continue
                chunk_appearances.append({
                    "character": _ap["character"],
                    "start_sec": float(_s),
                    "end_sec": float(_e),
                })

            prompt_payload = {
                "work_title": payload.work_title,
                "topic": payload.topic,
                "reject_note": payload.reject_note,
                "editorial": payload.editorial,
                "previous_episodes_context": payload.previous_episodes_context,
                "work_context": payload.work_context,
                "chunk_index": chunk.index,
                "chunk_start_sec": chunk.start_sec,
                "chunk_end_sec": chunk.end_sec,
                "scene_boundaries": scene_boundaries,
                "video_path": str(split_path) if split_path else None,
                "previous_analyses": previous_analyses.copy(),
                "transcript_segments": chunk_transcript_segs,
                "character_appearances": chunk_appearances,
            }

            try:
                response = gemini.analyze_chunk(prompt_payload)
                chunk_elapsed = time.time() - chunk_start
                run_log["steps"].append({"step": "gemini", "chunk": chunk.index, "response": response})
                moment_count = len(response.get("candidate_moments", []))
                print(f"    → {moment_count}개 후보 모멘트 발견 (소요 시간: {chunk_elapsed:.1f}초)")

                # 진단용 즉시 보존: 아래의 in-place offset 적용(+chunk.start_sec) 전에
                # JSON round-trip 으로 deep copy 해서 원본 chunk-relative 응답을 그대로 기록.
                # → checkpoint_gemini.json(절대시간) vs 여기(상대시간) 비교로
                #    Gemini가 지시를 어기고 절대시간을 반환했는지 후속 진단 가능.
                try:
                    _gemini_raw_doc = json.loads(gemini_raw_log_path.read_text(encoding="utf-8"))
                except Exception:
                    _gemini_raw_doc = {"job_id": job_id, "chunks": []}
                _gemini_raw_doc.setdefault("chunks", []).append({
                    "chunk_index": chunk.index,
                    "chunk_start_sec": chunk.start_sec,
                    "chunk_end_sec": chunk.end_sec,
                    "elapsed_sec": chunk_elapsed,
                    "response": json.loads(json.dumps(response, ensure_ascii=False)),
                })
                gemini_raw_log_path.write_text(
                    json.dumps(_gemini_raw_doc, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                # ─────────────────────────────────────────────────────────────
                # Sanity check: Gemini가 지시(0초 기준 상대시간)를 어기고
                # 절대시간을 반환한 경우를 감지하여 offset 가산을 스킵한다.
                #
                # 배경: split_video_chunk가 PTS를 0초로 정규화하므로 정상 응답은
                #   [0, chunk_duration] 범위여야 한다. 그런데 prompt에 들어가는
                #   previous_analyses가 in-place mutation으로 절대시간이 섞여 있어,
                #   AI가 가끔 컨텍스트를 흉내내 절대시간을 반환한다 (비결정적).
                #   이 경우 평소처럼 chunk.start_sec를 더하면 라벨이 정확히 2배가 된다.
                #
                # 감지 규칙: raw 응답의 max(end_sec)가 chunk_duration을 1초 이상 초과하면
                #   절대시간 반환으로 판정. 이때는 offset=0 으로 처리 (값이 이미 절대).
                # ─────────────────────────────────────────────────────────────
                _chunk_duration = chunk.end_sec - chunk.start_sec
                _raw_ends = [float(m.get("end_sec", 0)) for m in (response.get("candidate_moments") or [])]
                _raw_ends += [float(s.get("end_sec", 0)) for s in (response.get("segments") or [])]
                _raw_max_end = max(_raw_ends) if _raw_ends else 0.0
                _ai_returned_absolute = _raw_max_end > _chunk_duration + 1.0
                if _ai_returned_absolute:
                    actual_cut_offset = 0.0
                    print(
                        f"    [WARN] Gemini가 절대시간을 반환함 — "
                        f"raw_max_end={_raw_max_end:.1f}s > chunk_duration={_chunk_duration:.1f}s. "
                        f"chunk.start_sec({chunk.start_sec:.1f}s) 가산 스킵 (이중 가산 방지)."
                    )
                else:
                    actual_cut_offset = chunk.start_sec

                previous_analyses.append({
                    "chunk_index": chunk.index,
                    "summary": response.get("summary", ""),
                    "candidate_moments": response.get("candidate_moments", []),
                    "segments": response.get("segments", []),
                })

                # chunk-level 메타 (segments 포함) 누적 — checkpoint_gemini.json에 보존되고
                # story 단계의 segments 요약 컨텍스트로도 활용된다.
                # segments의 start_sec/end_sec을 절대 시간으로 변환 — Gemini가 상대시간을
                # 반환한 정상 케이스에선 actual_cut_offset = chunk.start_sec, 절대시간을
                # 반환한 이상 케이스에선 actual_cut_offset = 0 (위 sanity check 참조).
                _chunk_segments_abs: list[dict[str, Any]] = []
                for s in (response.get("segments") or []):
                    _ss = float(s.get("start_sec", 0)) + actual_cut_offset
                    _ee = float(s.get("end_sec", 0)) + actual_cut_offset
                    _chunk_segments_abs.append({
                        "segment_index": s.get("segment_index"),
                        "start_sec": _ss,
                        "end_sec": _ee,
                        "description": s.get("description", ""),
                    })

                # PR-2: chunk_intro_credits_ranges 절대시간 변환 후 chunk_meta 에 보존.
                # 8단계 청크 분석이 영상을 직접 보고 식별한 intro/credits/recap 구간.
                # 후속 포스트필터(_filter_candidates_by_chunk_intro_credits)에서 활용.
                _intro_ranges_abs: list[dict[str, Any]] = []
                for r in (response.get("chunk_intro_credits_ranges") or []):
                    try:
                        _rs = float(r.get("start_sec", 0)) + actual_cut_offset
                        _re = float(r.get("end_sec", 0)) + actual_cut_offset
                    except (TypeError, ValueError):
                        continue
                    if _re <= _rs:
                        continue
                    _intro_ranges_abs.append({
                        "start_sec": _rs,
                        "end_sec": _re,
                        "kind": r.get("kind", "intro"),
                        "confidence": float(r.get("confidence", 0.5) or 0.5),
                    })

                chunk_meta_list.append({
                    "chunk_index": chunk.index,
                    "summary": response.get("summary", ""),
                    "segments": _chunk_segments_abs,
                    "characters_tracking": response.get("characters_tracking", []),
                    "title_candidates": response.get("title_candidates", []),
                    "intro_credits_ranges": _intro_ranges_abs,
                })

                # split_video_chunk가 PTS를 0초로 정규화하므로 (output seek + -avoid_negative_ts make_zero),
                # Gemini는 0초 기준 상대 시간으로 응답하는 게 정상이고, 이 경우 actual_cut_offset
                # 은 위 sanity check에서 chunk.start_sec으로 설정되어 있다.
                # AI가 지시를 어기고 절대시간을 반환한 경우엔 actual_cut_offset=0으로 설정되어
                # 이중 가산을 막는다 (라벨 2배 시프트 버그 방지).
                _intro_credits_dropped = 0
                for moment in response["candidate_moments"]:
                    # PR-2: LLM 이 직접 is_intro_credits=true 로 표시한 candidate 는
                    # all_candidates 에 추가하지 않고 즉시 drop. chunk-level
                    # chunk_intro_credits_ranges 기반 포스트필터(_filter_candidates_by_chunk_intro_credits)
                    # 와 함께 이중 안전망.
                    if moment.get("is_intro_credits") is True:
                        _intro_credits_dropped += 1
                        continue
                    moment["start_sec"] += actual_cut_offset
                    moment["end_sec"] += actual_cut_offset
                    moment["chunk_index"] = chunk.index

                    # context_extension 시간도 절대값으로 변환 (highlight 자동 확장용)
                    ce = moment.get("context_extension")
                    if isinstance(ce, dict):
                        if "extended_start_sec" in ce and ce["extended_start_sec"] is not None:
                            try:
                                ce["extended_start_sec"] = float(ce["extended_start_sec"]) + actual_cut_offset
                            except (TypeError, ValueError):
                                ce["needed"] = False
                        if "extended_end_sec" in ce and ce["extended_end_sec"] is not None:
                            try:
                                ce["extended_end_sec"] = float(ce["extended_end_sec"]) + actual_cut_offset
                            except (TypeError, ValueError):
                                ce["needed"] = False
                        # 안전 검증: extended가 start/end를 감싸지 않으면 needed=false 강등
                        if ce.get("needed"):
                            es = ce.get("extended_start_sec")
                            ee = ce.get("extended_end_sec")
                            if es is None or ee is None:
                                ce["needed"] = False
                            elif not (es <= moment["start_sec"] and moment["end_sec"] <= ee):
                                ce["needed"] = False
                            else:
                                # 라운드 6a (B): 청크 범위 벗어남 검증 — LLM 환각 차단
                                # extended가 청크 절대 시간 범위 [chunk.start_sec, chunk.end_sec]를
                                # ±0.5초 완충 두고 벗어나면 needed=false 강등.
                                _chunk_lo = chunk.start_sec - 0.5
                                _chunk_hi = chunk.end_sec + 0.5
                                if es < _chunk_lo or ee > _chunk_hi:
                                    ce["needed"] = False
                                    print(
                                        f"    [WARN] context_extension 청크 범위 벗어남 "
                                        f"(es={es:.1f}, ee={ee:.1f} vs chunk[{chunk.start_sec:.1f}~{chunk.end_sec:.1f}]) "
                                        f"→ needed=false 강등"
                                    )

                    # beats 시간도 절대값으로 변환 (내용 기반 trim·스토리 구성용).
                    # 시간 결손/역전 beat는 drop. context_extension 변환과 동일 패턴.
                    _beats = moment.get("beats")
                    if isinstance(_beats, list):
                        _valid_beats = []
                        for _b in _beats:
                            if not isinstance(_b, dict):
                                continue
                            try:
                                _b["start_sec"] = float(_b["start_sec"]) + actual_cut_offset
                                _b["end_sec"] = float(_b["end_sec"]) + actual_cut_offset
                            except (KeyError, TypeError, ValueError):
                                continue
                            if _b["end_sec"] > _b["start_sec"]:
                                _valid_beats.append(_b)
                        moment["beats"] = _valid_beats

                    all_candidates.append(moment)
            finally:
                if split_path and split_path.exists():
                    try:
                        split_path.unlink()
                        print(f"    → 분할 파일 삭제 완료: {split_path.name}")
                    except Exception as e:
                        print(f"    [WARN] 분할 파일 삭제 실패: {split_path.name} ({e})")

        gemini_elapsed = time.time() - gemini_start


        checkpoint_gemini.write_text(
            json.dumps({
                "all_candidates": all_candidates,
                "chunk_meta": chunk_meta_list,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] Gemini 분석 완료 (총 {len(all_candidates)}개 후보, 소요 시간: {gemini_elapsed:.1f}초)")
    else:
        if not checkpoint_gemini.exists():
            raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_gemini}")
        gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
        all_candidates = gemini_data["all_candidates"]
        chunk_meta_list = gemini_data.get("chunk_meta", [])

    # PR-2 + PR-5c-3: 인트로/크레딧 필터 — 8단계 청크 분석의 chunk_intro_credits_ranges 만 사용.
    # (5단계 exclusion + filter_excluded_moments 는 PR-5c-3 에서 제거 — chunk_intro_credits_ranges
    # 가 영상 분석으로 더 정확하게 동일 일을 한다.)
    all_candidates = _filter_candidates_by_chunk_intro_credits(all_candidates, chunk_meta_list)

    # ── 라운드 19B: 청크 오버랩(180s) 중복 candidate dedup (IoU 기반) ──
    all_candidates = _dedup_overlapping_candidates(all_candidates, iou_threshold=0.7)

    # ── 타임스탬프 ↔ 내용 대조 (전사 기준) ──
    # 청크 분석이 내용은 맞게 보면서 시간축만 밀린 응답을 낼 때가 있다(2026-08-06 샤먼 2화:
    # 인용 대사의 실제 위치가 518.9s 인데 후보는 830~887 을 주장, 같은 응답의 다른 후보는
    # 소스 길이 875s 를 넘는 933~997). 길이·부등호 검사는 '불가능한 값'만 걸러 이걸 못 잡고,
    # 범위 안이면 **엉뚱한 장면이 그대로 렌더돼 조용히 발행된다.** 판정은 보수적이다 —
    # 인용 대사가 전사에서 확실히 다른 위치에 있을 때만 버린다.
    if chunk_transcripts:
        _before = len(all_candidates)
        # source_duration_sec: 소스 밖 후보를 **스토리가 고르기 전에** 뺀다 — 여기서
        # 빼야 다른 성한 후보로 대신 만든다. 나중에 clips_beyond_source 가 잡으면 그 편은
        # 통째로 실패한다(2026-08-24 §소스 밖 클립 검증).
        all_candidates, _ts_notes = timestamp_check.filter_candidates(
            all_candidates, chunk_transcripts,
            source_duration_sec=float(getattr(media_info, "duration_sec", 0.0) or 0.0))
        for _n in _ts_notes:
            print(f"  [WARN] 타임스탬프 불일치 → 후보 제외: {_n}")
        if len(all_candidates) != _before:
            print(f"  [WARN] 후보 {_before}개 중 {_before - len(all_candidates)}개가 "
                  f"타임스탬프 불일치로 제외됨 (남은 {len(all_candidates)}개)")

    # ═══════════════════════════════════════
    # [9/15] 관계 그래프 추출
    # ═══════════════════════════════════════
    checkpoint_graph = output_dir / "checkpoint_graph.json"
    relationship_edges: list[dict[str, Any]] = []

    if start_idx <= step_idx["graph"] and checkpoint_graph.exists() and from_step not in ("gemini", "graph"):
        print("\n[9/15] 관계 그래프 로드 중...")
        graph_data = json.loads(checkpoint_graph.read_text(encoding="utf-8"))
        relationship_edges = graph_data.get("edges", [])
        print(f"  - {len(relationship_edges)}개 관계 엣지 로드")
        print("[OK] 관계 그래프 로드 완료 (체크포인트에서)")
    elif start_idx <= step_idx["graph"]:
        print("\n[9/15] 관계 그래프 추출 중...")
        gemini = load_gemini_client()
        relationship_edges = gemini.extract_relationships(all_candidates)
        checkpoint_graph.write_text(
            json.dumps({"edges": relationship_edges}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] 관계 그래프 추출 완료 ({len(relationship_edges)}개 엣지)")

    # ═══════════════════════════════════════
    # [10/15] 스토리 구성 (바이럴 최적화 — 멀티쇼츠)
    # ═══════════════════════════════════════
    # all_storyline_variants: list of (clips, title_text, score)
    all_storyline_variants: list[tuple[list[StoryClip], str, float]] = []
    # PR-4: storyline 마다 LLM 이 미리 생성한 tts_cues. all_storyline_variants 와 인덱스 1:1.
    # 비어 있으면 TTS 없이 진행 (내레이션 없는 쇼츠 — 정상 경로).
    storyline_tts_cues_pool: list[list[dict[str, Any]]] = []
    max_shorts = min(payload.max_shorts, config.max_shorts_count)
    story_plan = None

    checkpoint_story = output_dir / "checkpoint_story.json"
    if start_idx <= step_idx["story"] and checkpoint_story.exists() and from_step != "story":
        print("\n[10/15] 스토리 구성 결과 로드 중...")
        story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))

        # 멀티쇼츠 체크포인트 로드
        # 중복 복구는 여기서도 돈다 — 중복이 든 채 저장된 옛 체크포인트로 재렌더하면
        # 같은 결과물이 다시 나오기 때문(2026-08-12 스트릿 레스토랑 파이터 EP1).
        if "variants" in story_data:
            for v in story_data["variants"]:
                v_clips = [StoryClip(**c) for c in v["clips"]]
                v_clips, v_cue_map, v_dedup_msg = _dedup_storyline_clips(v_clips)
                if v_dedup_msg:
                    print(f"  [clip-dedup] 체크포인트 스토리라인: {v_dedup_msg}")
                if not v_clips:
                    continue
                all_storyline_variants.append((v_clips, v["title_text"], v.get("score", 0.0)))
                # PR-4: 캐시된 storyline 에 tts_cues 가 있으면 함께 로드, 없으면 [] (fallback)
                storyline_tts_cues_pool.append(
                    list(_remap_cue_clip_indexes(list(v.get("tts_cues") or []), v_cue_map))
                )
            print(f"  - {len(all_storyline_variants)}개 스토리라인 로드")
        else:
            # 하위 호환: 이전 단일 체크포인트
            clips = [StoryClip(**clip) for clip in story_data["clips"]]
            clips, _ckpt_cue_map, _ckpt_dedup_msg = _dedup_storyline_clips(clips)
            if _ckpt_dedup_msg:
                print(f"  [clip-dedup] 체크포인트 스토리라인: {_ckpt_dedup_msg}")
            title_text = story_data["title_text"]
            all_storyline_variants.append((clips, title_text, 1.0))
            storyline_tts_cues_pool.append(
                list(_remap_cue_clip_indexes(list(story_data.get("tts_cues") or []), _ckpt_cue_map))
            )
            print(f"  - {len(clips)}개 클립, 제목: {title_text}")
        print("[OK] 스토리 구성 결과 로드 완료 (체크포인트에서)")

    elif start_idx <= step_idx["story"]:
        print("\n[10/15] 스토리 구성 중...")
        gemini = load_gemini_client()
        story_start = time.time()

        # sequence_id 부여: continues_from + 관계 그래프 continuous 엣지 기반
        all_candidates = assign_sequence_ids(all_candidates, edges=relationship_edges or None)

        # PR-5c-4: candidate.transcript 갱신 — chunk_transcripts 슬라이스로 단순화.
        # 라운드 15 의도(LLM 시각 추정 → 실제 음성) 는 그대로. clip 단위 audio 재추출 +
        # Whisper 호출은 제거 (chunk_transcribe 단계가 청크별로 한 번에 처리 완료).
        print("\n[10-pre/13] candidate.transcript 갱신 (chunk_transcripts 슬라이스)")
        _cand_segs: list = []
        for _ct in chunk_transcripts or []:
            _cand_segs.extend(_ct.get("segments") or [])
        _updated = 0
        for _m in all_candidates:
            _s = float(_m.get("start_sec", 0))
            _e = float(_m.get("end_sec", 0))
            if _e <= _s:
                continue
            _related = sorted(
                [seg for seg in _cand_segs if seg.end_sec > _s and seg.start_sec < _e],
                key=lambda x: x.start_sec,
            )
            if _related:
                _combined = " ".join(
                    seg.text.strip() for seg in _related if seg.text and seg.text.strip()
                ).strip()
                if _combined:
                    _m["transcript"] = _combined
                    _updated += 1
        print(f"  candidate.transcript 갱신: {_updated}/{len(all_candidates)}건")

        # 작품별 편집 지침 — avoid 태깅된 후보를 스토리 구성 전에 결정적으로 제외.
        # 프롬프트 지시(사용 금지)만으로는 LLM 이 놓칠 수 있어 코드가 이중으로 거른다.
        # 제외 내역은 반드시 찍는다 — 조용한 축소는 "다 봤다"로 읽힌다(no silent caps).
        all_candidates, _ed_dropped = filter_flagged_candidates(all_candidates, payload.editorial)
        if payload.editorial and payload.editorial.get("avoid"):
            # dropped 0건이어도 기록 — "필터가 돌았는데 걸린 게 없다"는 사실 자체가 정보다.
            run_log["steps"].append(filter_log_entry(all_candidates, _ed_dropped))
        if _ed_dropped:
            print(f"  [editorial] 금지 태깅 후보 {len(_ed_dropped)}건 제외:")
            for _d in _ed_dropped:
                print(f"    - chunk {_d.get('chunk_index')} "
                      f"{float(_d.get('start_sec', 0)):.1f}~{float(_d.get('end_sec', 0)):.1f}s "
                      f"flags={_d.get('guideline_flags')}")
            if not all_candidates:
                # 전부 금지면 위반 영상을 만드는 것보다 크게 실패하는 쪽이 낫다 —
                # 과잉 태깅(회차 전체가 걸림)이면 지침 문구나 태깅 기준을 손봐야 한다.
                raise RuntimeError(
                    "[editorial] 모든 후보가 금지 태깅으로 제외됐습니다 — "
                    "avoid 지침이 과잉 적용됐는지(작품 카드 editorial) 확인하세요.")

        # 짧은 소재 완화 — 후보 커버리지가 min_duration_sec 에 못 미치면 이번 런의 최소 길이를
        # 커버리지까지 내린다. 안 내리면 LLM(프롬프트의 최소 길이)과 _fit_storyline_to_duration 이
        # 40s 를 맞추려고 같은 구간을 두 번 넣는다 — 2026-08-12 스트릿 레스토랑 파이터 EP1
        # (66s 소스 · 후보 2개 · 커버리지 28s)에서 hook·build 가 똑같이 32~50s 로 나왔다.
        # 정상 소재의 40~60s 정책은 그대로 — 후보를 다 써도 못 채우는 경우에만 적용된다.
        _coverage_sec = _candidate_coverage_sec(all_candidates)
        _short_source = _coverage_sec + 0.5 < float(config.min_duration_sec)
        if _short_source:
            _eff_min = max(_ABS_MIN_DURATION_SEC, int(_coverage_sec))
            print(
                f"  [short-source] 후보 커버리지 {_coverage_sec:.1f}s < 최소 {config.min_duration_sec}s "
                f"→ 이번 런 최소 길이 {_eff_min}s 로 완화 (같은 구간 중복 사용 방지)"
            )
            config = replace(config, min_duration_sec=_eff_min)

        # Gemini 바이럴 스토리 구성 (Flash + skeleton). 점수 산정은 Gemini가 description/highlight_eligible 등으로 직접 판단
        story_plan = gemini.compose_story_with_context(
            all_candidates,
            payload.work_title,
            payload.topic,
            min_duration_sec=config.min_duration_sec,
            max_duration_sec=config.max_duration_sec,
            work_context=payload.work_context,
            previous_episodes_context=payload.previous_episodes_context,
            reject_note=payload.reject_note,
            editorial=payload.editorial,
            relationship_edges=relationship_edges or None,
            chunk_meta=chunk_meta_list or None,
        )

        # 멀티쇼츠: ranked_storylines에서 최대 max_shorts개 추출
        ranked_storylines = story_plan.get("ranked_storylines", [])
        if not ranked_storylines:
            # 하위 호환: ranked_storylines가 없으면 selected_storyline 사용
            sel = story_plan.get("selected_storyline", {})
            if sel:
                ranked_storylines = [sel]

        # 시간 고정 lookup 빌드: LLM이 출력한 start/end_sec를 candidate 정본으로 복원
        candidates_lookup = _build_candidates_lookup(all_candidates)
        print(f"  - 시간 고정 lookup: {len(candidates_lookup)}개 candidate 인덱싱")
        # 청크 경계 dedup alias: 같은 장면이 양쪽 청크에 등록된 경우 합친 정본으로 redirect (라운드 4)
        boundary_alias = _dedup_boundary_candidates(all_candidates)
        if boundary_alias:
            print(f"  - 청크 경계 dedup: {len(boundary_alias)}개 중복 alias 적용")

        # 다양성 우선 재선정: 같은 chunk/phase가 max_shorts개 모두 차지하지 않게
        # 점수 1위는 무조건 유지하고, 이후는 chunk_index/emotional_phase가 다른 후보를 우선
        diverse_pool = select_diverse_storylines(
            ranked_storylines,
            max_count=max(max_shorts * 2, len(ranked_storylines)),  # 폴백 여유분 확보
            skeleton=None,  # skeleton 단계 제거 (라운드 6b) — chunk_index 다양성으로만 폴백
        )

        # 내용 기반 trim용: chunk_transcripts 의 모든 SpeechSegment 1회 평탄화 (문장 경계 스냅)
        _beat_trim_segs: list = []
        for ct in chunk_transcripts or []:
            _beat_trim_segs.extend(ct.get("segments") or [])

        # 라운드 11.1-A: _fit_storyline_to_duration이 clips 변경하면 그 시점 자막 캐시는
        # 옛 storyline 영역 기준이라 무효화해야 한다. 루프 안에서 변경 감지 후 종료 시 일괄 처리.
        _storyline_fit_changed = False
        for sl_idx, sl_data in enumerate(diverse_pool):
            if len(all_storyline_variants) >= max_shorts:
                break
            score = sl_data.get("score", 0.0)
            if score < config.viral_score_min_threshold and len(all_storyline_variants) > 0:
                print(f"  - 스토리라인 {sl_idx + 1} 스킵 (점수 {score:.2f} < 임계값 {config.viral_score_min_threshold})")
                continue

            # 라운드 20: storyline 시간 검증 — hook ↔ payoff 같은 timestamp / 시간 역행 폐기
            tl_valid, tl_reason = _validate_storyline_timeline(sl_data)
            if not tl_valid:
                print(f"  [SKIP-timeline] 스토리라인 {sl_idx + 1} 시간 검증 실패: {tl_reason}")
                continue

            try:
                sl_clips, sl_title = _clips_from_storyline(
                    sl_data, payload.work_title,
                    candidates_lookup=candidates_lookup,
                    boundary_alias=boundary_alias,
                )
            except (KeyError, TypeError) as e:
                print(f"  - 스토리라인 {sl_idx + 1} 파싱 실패: {e}")
                continue

            # 같은 원본 구간이 두 번 들어간 storyline 복구 (후보 부족 시 LLM 이 hook·build 에
            # 같은 candidate 를 배치한다). _validate_storyline_timeline 은 hook↔payoff 만 본다.
            sl_clips, _cue_index_map, _dedup_msg = _dedup_storyline_clips(sl_clips)
            if _dedup_msg:
                print(f"  [clip-dedup] 스토리라인 {sl_idx + 1}: {_dedup_msg}")
            if not sl_clips:
                print(f"  [SKIP] 스토리라인 {sl_idx + 1}: 중복 제거 후 남는 클립 없음")
                continue

            # TTS cue 앵커 기준 클립 — LLM 의 clip_index 는 storyline 구조(hook→build→payoff)
            # 그대로의 배열을 가리키므로, beat trim/length fit 으로 쪼개지기 *전* 을 잡아둔다.
            _anchor_clips = list(sl_clips)

            # 내용 기반 trim 우선: 60s 초과 시 beats 기반으로 덜 중요한 구간 제거(흐름 보존).
            # beats 없음/실패/무-drop이면 ([], "") 반환 → 아래 기존 _fit_storyline_to_duration 폴백.
            bt_clips, bt_msg = beat_trim_storyline(
                sl_clips, candidates_lookup, _beat_trim_segs,
                target_min=float(config.min_duration_sec),
                target_max=float(config.max_duration_sec),
                flash_drop_fn=gemini.choose_beat_drops,
            )
            if bt_clips:
                sl_clips = bt_clips
                print(f"  [BeatTrim] 스토리라인 {sl_idx + 1}: {bt_msg}")
                _storyline_fit_changed = True

            # 라운드 11: 길이 자동 보정 — 잔여 초과 시 build 제거/비례 trim, 40s 미만 시 인접 candidate 확장.
            # 내용 기반 trim 후에도 남는 초과·부족을 정리하는 backstop.
            sl_clips, fit_msg = _fit_storyline_to_duration(
                sl_clips, candidates_lookup,
                target_min=float(config.min_duration_sec),
                target_max=float(config.max_duration_sec),
            )
            if fit_msg:
                print(f"  [LengthFit] 스토리라인 {sl_idx + 1}: {fit_msg}")
                # 라운드 11.1-A: clips 시간 변경됨 → 자막 캐시 무효화 신호
                _storyline_fit_changed = True

            # 라운드 6c — 첫 쇼츠 title을 story_plan top-level title로 덮어쓰는 로직 제거.
            # 각 storyline의 LLM 출력 title_line1/line2가 정본. select_diverse_storylines로 정렬이
            # 바뀌면서 #1 자리 storyline의 title이 다른 storyline의 top-level title로 교체되어
            # 1·2번 쇼츠 제목이 동일해지는 버그를 직접 발생시켰음.

            # 클립 수 검증: 라운드 12 — sequence_type별 차등화
            # - highlight: 1개 (단일 강한 장면)
            # - storytelling 시퀀스블록형: 1개 (sequence 1 candidate도 OK)
            # - storytelling 그 외 (여정몰입형/결과선공개형/반전형): 2개 완화 (3 → 2, hook+build/payoff)
            is_highlight = (sl_data.get("shorts_type") == "highlight")
            _seq_type = sl_data.get("sequence_type", "여정몰입형")
            if is_highlight:
                min_clip_count = 1
            elif _seq_type == "시퀀스블록형":
                min_clip_count = 1
            elif _short_source:
                # 짧은 소재: 중복 제거 후 클립이 1개만 남을 수 있다. 같은 장면을 한 번 더
                # 붙여 2개를 만드는 것보다 1클립으로 내보내는 편이 낫다.
                min_clip_count = 1
            else:
                min_clip_count = 2
            is_valid, msg = validate_story_clips(
                sl_clips, config.min_duration_sec, config.max_duration_sec,
                min_clip_count=min_clip_count,
                max_duration_tolerance=config.max_duration_tolerance,
            )
            if not is_valid:
                print(f"  [SKIP] 스토리라인 {sl_idx + 1} 검증 실패: {msg}")
                continue  # storytelling 1~2클립이면 스킵 → 다음 후보로
            coh_warnings = validate_clip_coherence(sl_clips)
            for w in coh_warnings:
                print(f"  [COHERENCE] 스토리라인 {sl_idx + 1}: {w}")

            all_storyline_variants.append((sl_clips, sl_title, score))
            # PR-4: storyline 의 tts_cues 정규화 후 parallel pool 에 append.
            # sl_data 의 직접 필드 또는 sl_data["storyline"] 안 어디든 위치 가능 (LLM 변형 대응).
            _raw_cues = sl_data.get("tts_cues") if isinstance(sl_data.get("tts_cues"), list) else None
            if _raw_cues is None and isinstance(sl_data.get("storyline"), dict):
                _raw_cues = sl_data["storyline"].get("tts_cues") if isinstance(sl_data["storyline"].get("tts_cues"), list) else None
            # 중복 제거로 clip 이 빠졌으면 cue 의 clip_index 도 새 배열 기준으로 옮긴다.
            _raw_cues = _remap_cue_clip_indexes(_raw_cues, _cue_index_map)
            _normalized_cues = _normalize_storyline_tts_cues(
                _raw_cues or [],
                anchor_clips=_anchor_clips,
                max_cues=5,
            )
            storyline_tts_cues_pool.append(_normalized_cues)
            _cue_voices = sorted({c["voice"] for c in _normalized_cues})
            print(
                f"  - 스토리라인 {sl_idx + 1}: {len(sl_clips)}개 클립, 점수 {score:.2f}, 제목: {sl_title}"
                + (f" [story tts_cues={len(_normalized_cues)} voice={_cue_voices}]" if _normalized_cues else " [story tts_cues=0]")
            )

        # 폴백: 유효한 스토리가 없으면 selected_storyline에서 1개 생성
        if not all_storyline_variants:
            sel = story_plan.get("selected_storyline", {})
            if sel:
                fb_clips, fb_title = _clips_from_storyline(
                    sel, payload.work_title,
                    candidates_lookup=candidates_lookup,
                    boundary_alias=boundary_alias,
                )
                # 폴백은 검증을 안 거치는 경로다 — 중복 복구만은 반드시 통과시킨다.
                # (이 경로가 열려 있으면 위에서 걸러도 같은 화면이 두 번 든 결과물이 그대로 나간다)
                fb_clips, _fb_cue_map, _fb_dedup_msg = _dedup_storyline_clips(fb_clips)
                if _fb_dedup_msg:
                    print(f"  [clip-dedup] 폴백 스토리라인: {_fb_dedup_msg}")
                all_storyline_variants.append((fb_clips, fb_title, sel.get("score", 0.5)))
                # PR-4: 폴백 storyline 도 tts_cues 정규화 후 parallel pool 등록
                _fb_raw_cues = sel.get("tts_cues") if isinstance(sel.get("tts_cues"), list) else None
                if _fb_raw_cues is None and isinstance(sel.get("storyline"), dict):
                    _fb_raw_cues = sel["storyline"].get("tts_cues") if isinstance(sel["storyline"].get("tts_cues"), list) else None
                storyline_tts_cues_pool.append(_normalize_storyline_tts_cues(
                    _remap_cue_clip_indexes(_fb_raw_cues, _fb_cue_map) or [],
                    anchor_clips=fb_clips,
                    max_cues=5,
                ))
                print(f"  - 폴백 스토리라인: {len(fb_clips)}개 클립")

        # 라운드 4: story_plan에 정본 시간 덮어쓰기 → checkpoint_story.json에 LLM 환각이 안 남음
        # 모든 storylines (rank/selected/diverse 변형) 안의 클립 노드에 candidates_lookup 적용
        if "storylines" in story_plan and isinstance(story_plan["storylines"], list):
            story_plan["storylines"] = [
                _apply_lookup_to_storyline(sl, candidates_lookup, boundary_alias)
                for sl in story_plan["storylines"]
            ]
        if isinstance(story_plan.get("ranked_storylines"), list):
            story_plan["ranked_storylines"] = [
                _apply_lookup_to_storyline(sl, candidates_lookup, boundary_alias)
                for sl in story_plan["ranked_storylines"]
            ]
        if isinstance(story_plan.get("selected_storyline"), dict):
            story_plan["selected_storyline"] = _apply_lookup_to_storyline(
                story_plan["selected_storyline"], candidates_lookup, boundary_alias
            )

        # 라운드 11.1-A: storyline clips 변경됐으면 자막·TTS·resources 캐시 무효화 + transcribe 단계로
        # start_idx 다운그레이드 → 새 영역 기준으로 재전사. from_step="render"로 들어왔어도 작동.
        if _storyline_fit_changed:
            print("  [LengthFit] storyline clips 변경 감지 → 자막·TTS·리소스 캐시 무효화")
            for _fname in ("subtitle_segments.json", "full_audio.json",
                           "checkpoint_tts_plan.json", "checkpoint_resources.json"):
                _p = output_dir / _fname
                if _p.exists():
                    try:
                        _p.unlink()
                    except OSError:
                        pass
            try:
                _transcribe_idx = step_order.index("transcribe")
                if start_idx > _transcribe_idx:
                    start_idx = _transcribe_idx
                    print(f"  [LengthFit] start_idx → {_transcribe_idx} (transcribe부터 재실행)")
            except ValueError:
                pass

        story_elapsed = time.time() - story_start
        print(f"  → 총 {len(all_storyline_variants)}개 쇼츠 생성 예정")

        # 체크포인트 저장 — PR-4: 각 variant 의 tts_cues 도 함께 저장 (재실행 시 cue 재생성 비용 절약).
        # storyline_tts_cues_pool 와 all_storyline_variants 가 인덱스 1:1 (parallel list 보장).
        checkpoint_data = {
            "raw_response": story_plan,
            "variants": [
                {
                    "clips": [c.__dict__ for c in clips],
                    "title_text": title,
                    "score": score,
                    "tts_cues": (storyline_tts_cues_pool[v_idx] if v_idx < len(storyline_tts_cues_pool) else []),
                }
                for v_idx, (clips, title, score) in enumerate(all_storyline_variants)
            ],
            # 하위 호환
            "clips": [c.__dict__ for c in all_storyline_variants[0][0]] if all_storyline_variants else [],
            "title_text": all_storyline_variants[0][1] if all_storyline_variants else "",
            "tts_cues": (storyline_tts_cues_pool[0] if storyline_tts_cues_pool else []),
        }
        checkpoint_story.write_text(
            json.dumps(checkpoint_data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] 스토리 구성 완료 (소요 시간: {story_elapsed:.1f}초)")
    else:
        edit_plan_path = output_dir / "edit_plan.json"
        if checkpoint_story.exists():
            story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
            if "variants" in story_data:
                for v in story_data["variants"]:
                    v_clips = [StoryClip(**c) for c in v["clips"]]
                    all_storyline_variants.append((v_clips, v["title_text"], v.get("score", 0.0)))
                    # PR-4: 캐시된 storyline tts_cues 함께 로드 (정규화는 캐시 저장 시점에 이미 완료)
                    storyline_tts_cues_pool.append(list(v.get("tts_cues") or []))
            else:
                clips = [StoryClip(**clip) for clip in story_data["clips"]]
                title_text = story_data["title_text"]
                all_storyline_variants.append((clips, title_text, 1.0))
                storyline_tts_cues_pool.append(list(story_data.get("tts_cues") or []))
        elif edit_plan_path.exists():
            print("\n[10/15] 기존 파일에서 스토리 복원 중...")
            edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8"))
            clips = []
            for clip_data in edit_plan["timeline"]:
                clips.append(StoryClip(
                    role=clip_data["role"],
                    start_sec=clip_data["clip_start_sec"],
                    end_sec=clip_data["clip_end_sec"],
                    subtitle=clip_data["subtitle"],
                    use_original_audio=clip_data["use_original_audio"],
                ))
            title_text = edit_plan["layout"]["top_title"]
            all_storyline_variants.append((clips, title_text, 1.0))
            # PR-4: edit_plan 폴백 — tts_cues 정보 없음, 빈 리스트 → tts_plan fallback 으로 채워짐
            storyline_tts_cues_pool.append([])
            print(f"  - {len(clips)}개 클립, 제목: {title_text}")
            print("[OK] 스토리 복원 완료 (edit_plan.json에서)")
        else:
            raise FileNotFoundError("체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다.")

    # 첫 번째 스토리라인을 기본 clips/title_text로 설정 (하위 호환)
    clips, title_text, _ = all_storyline_variants[0]

    # ═══════════════════════════════════════
    # [silence_cut] 스토리-aware 무음 컷 (PR-5 / PR-6 gap-단위)
    # ═══════════════════════════════════════
    # cut_silence_with_story_filter 로 각 variant clips 컷. cue 는 앵커 상태로 통과하며
    # 시간 변환은 [tts cues] 블록의 _resolve_cue_anchors 가 최종 타임라인 기준으로 한다.
    # 무음 공격성은 config.silence_cut_profile (env SILENCE_CUT_PROFILE)
    # 로 토글 — conservative(베이스라인) vs aggressive(가설). 채널 A/B 비교용.
    silence_profile = get_silence_profile(config.silence_cut_profile)
    checkpoint_silence_cut = output_dir / "checkpoint_silence_cut.json"
    _silence_cut_invalidate = from_step in ("gemini", "story", "silence_cut")
    # 프로파일이 바뀌면 캐시 무효화 — A/B 두 arm 이 같은 output_dir 를 재사용할 때 stale 결과 방지.
    _sc_cache_ok = checkpoint_silence_cut.exists() and not _silence_cut_invalidate
    if _sc_cache_ok:
        _sc_data = json.loads(checkpoint_silence_cut.read_text(encoding="utf-8"))
        if _sc_data.get("profile", "conservative") != silence_profile.name:
            print(f"\n[silence_cut] 프로파일 변경 ({_sc_data.get('profile')} → {silence_profile.name}) → 캐시 무시, 재계산")
            _sc_cache_ok = False
    if _sc_cache_ok:
        print(f"\n[silence_cut] 캐시 로드 중... (프로파일: {silence_profile.name})")
        _new_variants: list[tuple[list[StoryClip], str, float]] = []
        _new_cues_pool: list[list[dict[str, Any]]] = []
        for v in _sc_data.get("variants", []):
            v_clips = [StoryClip(**c) for c in v["clips"]]
            _new_variants.append((v_clips, v["title_text"], v.get("score", 0.0)))
            _new_cues_pool.append(list(v.get("tts_cues") or []))
        if _new_variants:
            all_storyline_variants = _new_variants
            storyline_tts_cues_pool = _new_cues_pool
            clips = list(all_storyline_variants[0][0])
        print(f"  - {len(_new_variants)}개 variant 로드 완료")
    elif start_idx <= step_idx["silence_cut"]:
        print(f"\n[silence_cut] 스토리-aware 무음 컷 진행 중... (프로파일: {silence_profile.name}, gap_level={silence_profile.gap_level})")
        _sc_start = time.time()
        # chunk_transcripts 의 모든 SpeechSegment 평탄화 (cut_silence_with_story_filter 가
        # 자체적으로 각 clip 범위 안 segments 만 필터링).
        _all_chunk_segs: list = []
        for ct in chunk_transcripts or []:
            _all_chunk_segs.extend(ct.get("segments") or [])
        _candidates_lookup_sc = _build_candidates_lookup(all_candidates)

        new_variants: list[tuple[list[StoryClip], str, float]] = []
        new_cues_pool: list[list[dict[str, Any]]] = []
        for v_idx, (v_clips, v_title, v_score) in enumerate(all_storyline_variants):
            cut_results = cut_silence_with_story_filter(
                v_clips, _all_chunk_segs, _candidates_lookup_sc,
                profile=silence_profile,
            )
            v_clips_new = flatten_to_clips(cut_results)
            # 너무 짧아지면 롤백 (라운드 13-B 동일 패턴)
            new_total = sum(c.end_sec - c.start_sec for c in v_clips_new)
            orig_total = sum(c.end_sec - c.start_sec for c in v_clips)
            # cue 는 앵커(source_time_sec) 상태로 통과 — 시간 변환은 최종 타임라인 확정 후
            # _resolve_cue_anchors 가 한 번에 한다 (클립 분할·경계 변경을 전부 자동 반영).
            v_cues_orig = list(storyline_tts_cues_pool[v_idx] if v_idx < len(storyline_tts_cues_pool) else [])
            if new_total < float(config.min_duration_sec) and orig_total <= float(config.max_duration_sec):
                print(f"  [variant {v_idx+1} rollback] {new_total:.1f}s < {config.min_duration_sec:.0f}s → 원본 ({orig_total:.1f}s) 사용")
                v_clips_new = list(v_clips)
            shifted_cues = v_cues_orig
            # 길이 재보정 (40~60s)
            v_clips_new, _fit_msg = _fit_storyline_to_duration(
                v_clips_new, _candidates_lookup_sc,
                target_min=float(config.min_duration_sec),
                target_max=float(config.max_duration_sec),
            )
            if _fit_msg:
                print(f"  [LengthFit-postsilence variant {v_idx+1}] {_fit_msg}")
            new_variants.append((v_clips_new, v_title, v_score))
            new_cues_pool.append(shifted_cues)
            _removed = sum(r.total_removed_sec for r in cut_results)
            _cuts = sum(max(0, len(r.keep_intervals) - 1) for r in cut_results)
            print(f"  - variant {v_idx + 1}: {_cuts}회 컷, {_removed:.1f}초 제거")
        all_storyline_variants = new_variants
        storyline_tts_cues_pool = new_cues_pool
        clips = list(all_storyline_variants[0][0]) if all_storyline_variants else clips
        checkpoint_silence_cut.write_text(
            json.dumps({
                "profile": silence_profile.name,
                "variants": [
                    {"clips": [c.__dict__ for c in vc], "title_text": t, "score": s, "tts_cues": new_cues_pool[i]}
                    for i, (vc, t, s) in enumerate(all_storyline_variants)
                ],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] silence_cut 완료 (소요 시간: {time.time()-_sc_start:.1f}초)")

    # 라운드 14: transcribe는 모든 variant clips의 union 영역에 대해 수행한다.
    # 이전엔 첫 variant clips만 transcribe → variant 2/3는 candidate.transcript(LLM 추정) 폴백 사용
    # → 자막이 음성과 다르고 균등 분할로 박자 어긋남.
    # union으로 transcribe하면 모든 variant 자막이 Whisper segment 단위 정확 매핑.
    union_clips: list[StoryClip] = []
    _seen_keys: set[tuple[float, float]] = set()
    for _vc, _, _ in all_storyline_variants:
        for _c in _vc:
            _key = (round(_c.start_sec, 2), round(_c.end_sec, 2))
            if _key not in _seen_keys:
                _seen_keys.add(_key)
                union_clips.append(_c)
    union_clips.sort(key=lambda c: c.start_sec)

    # ═══════════════════════════════════════
    # [transcript + 자막] chunk_transcripts 슬라이스 → snap/extend/fill → 자막 매핑 (PR-5c-4)
    # ═══════════════════════════════════════
    # transcribe 단계 제거. transcript_text 는 PR-3 의 chunk_transcripts 가 청크 분할 직후
    # 한 번에 생성한 결과를 union_clips 영역으로 슬라이스해서 사용. 자막 매핑 흐름은 그대로.
    transcript_text: list = []
    segments_cache_path = output_dir / "subtitle_segments.json"

    # chunk_transcripts 평탄화 + union_clips 영역 슬라이스 (기존 SRT 분기와 동일 패턴)
    _all_segs: list = []
    for ct in chunk_transcripts or []:
        _all_segs.extend(ct.get("segments") or [])
    _seen_seg_keys: set[tuple[float, float, str]] = set()
    for clip in union_clips:
        for seg in _all_segs:
            if seg.end_sec > clip.start_sec and seg.start_sec < clip.end_sec:
                _k = (round(seg.start_sec, 2), round(seg.end_sec, 2), seg.text)
                if _k not in _seen_seg_keys:
                    _seen_seg_keys.add(_k)
                    transcript_text.append(seg)

    # SRT 직접 파싱 폴백 — chunk_transcripts 가 비면(예: --from-step gemini 재개) variant 경로처럼
    # SRT 를 직접 파싱해 라인 단위 자막을 복원한다. candidate.transcript(1줄/clip) 폴백보다 우선.
    if not transcript_text and payload.srt_path and payload.srt_path.exists():
        try:
            _srt_segs = parse_subtitle(payload.srt_path)
            for clip in union_clips:
                for seg in _srt_segs:
                    if seg.end_sec > clip.start_sec and seg.start_sec < clip.end_sec:
                        _k = (round(seg.start_sec, 2), round(seg.end_sec, 2), seg.text)
                        if _k not in _seen_seg_keys:
                            _seen_seg_keys.add(_k)
                            transcript_text.append(seg)
            if transcript_text:
                print(f"  [SRT 직접 파싱] chunk_transcripts 비어있음 — SRT에서 {len(transcript_text)}개 segment 복원")
        except Exception as _exc:
            print(f"  [경고] SRT 직접 파싱 실패: {_exc}")

    # Gemini 폴백 — chunk_transcripts·SRT 모두 비어 있고 candidates 의 transcript 가 있을 때
    used_gemini_fallback = False
    if not transcript_text and all_candidates:
        used_gemini_fallback = True
        print("  [FALLBACK] chunk_transcripts 비어있음 — Gemini 대사 데이터로 자막 생성")
        for clip in clips:
            for m in all_candidates:
                m_start = m.get("start_sec", 0)
                m_end = m.get("end_sec", 0)
                if m_end > clip.start_sec and m_start < clip.end_sec and m.get("transcript"):
                    transcript_text.append(SpeechSegment(
                        start_sec=clip.start_sec,
                        end_sec=clip.end_sec,
                        text=m["transcript"],
                    ))
                    break

    _branch = "chunk_transcripts" if not used_gemini_fallback else "Gemini 폴백"
    print(f"\n[transcript] {len(transcript_text)}개 segment ({_branch})")

    # ── 편집실 오버라이드(2026-08-16) ─────────────────────────────────────
    # 사람이 관제 편집실에서 고친 제목·구간을 여기서 먹인다. 적용 지점이 여기인 이유:
    # silence_cut 캐시 로드가 all_storyline_variants 를 **통째로 교체**하므로 그보다
    # 앞에서 적용하면 덮여 사라지고, 자동 보정(아래 snap/extend/fill)보다는 앞이어야
    # pinned 로 그 보정을 건너뛸 수 있다.
    _edit_pinned = False
    if _edit_overrides:
        _before_clips = len(clips)
        all_storyline_variants, _edit_pinned = apply_overrides(
            all_storyline_variants, _edit_overrides)
        clips = list(all_storyline_variants[0][0])
        title_text = all_storyline_variants[0][1]
        print(f"\n[edit] 편집실 오버라이드 적용 — 제목: {title_text[:24]!r}"
              f"{' · 구간 ' + str(_before_clips) + '→' + str(len(clips)) + '개(고정)' if _edit_pinned else ''}"
              f" · 합계 {total_duration(clips):.1f}s")

    # 시간대별 제목(E8, title.segments) — 좌표가 자막과 같은 편집본 시간축이라 앵커
    # 변환이 필요 없다(원본 절대초가 아니다). 첫 variant 렌더에만 넘긴다(오버라이드
    # 공통 규약). 배속(×1/S) 변환은 제목을 setpts 뒤에 얹는 렌더러가 이미지 오버레이와
    # 같은 지점에서 한다.
    _title_segments = overrides_title_segments(_edit_overrides)
    if _title_segments:
        print(f"  [edit] 시간대별 제목 {len(_title_segments)}개 창 — 창 밖 시간은 제목 없음")

    # snap/extend/fill (라운드 19C — Gemini 폴백 데이터는 타이밍 부정확이라 건너뜀)
    # 사람이 구간을 지정했으면(_edit_pinned) 건너뛴다 — 대사 경계 스냅(±5초)·서사 확장
    # (편측 8초)·갭 메우기가 사람 입력을 최대 8초까지 밀어버리기 때문이다. 편집기에서
    # 12.5초라고 지정한 것이 20초가 되면 그건 편집기가 아니다. 대신 아래 길이 클램프와
    # 중복 제거는 그대로 통과시킨다 — 그 둘은 렌더 안전장치(60초 상한·겹침)라 사람이
    # 넘겨도 되는 종류가 아니다.
    if _edit_pinned:
        print("  [edit] 자동 보정(스냅·확장·갭메움) 생략 — 사람이 지정한 구간 고정")
    # E7-2 영상 배속: 출력 길이 = 소스 합계 / S. 길이 정책(40~60s·쇼츠 3분)은 **출력**에
    # 대한 것이므로, 소스 시간축에서 도는 확장 상한·길이 클램프의 예산은 정책 × S 다 —
    # S=0.8 이면 소스 ≤ 47.8s(출력 ≤ 59.7s), S=2.0 이면 소스 80~120s 를 요구한다.
    # 출력 mp4 를 재는 validate_output 의 정책 값은 그대로 둔다.
    _speed = float(getattr(payload.design, "video_speed", 1.0) or 1.0)
    if _speed != 1.0:
        print(f"  [speed] video_speed={_speed:g} — 소스 길이 예산 ×{_speed:g} "
              f"(출력 = 소스/{_speed:g})")
    if transcript_text and not used_gemini_fallback and not _edit_pinned:
        all_storyline_variants = _snap_clip_boundaries_to_dialogue(
            all_storyline_variants, transcript_text, snap_back_max=5.0, snap_forward_max=5.0,
        )
        all_storyline_variants = _extend_storyline_for_narrative(
            all_storyline_variants,
            candidates_lookup=_build_candidates_lookup(all_candidates),
            target_max=float(config.max_duration_sec) * _speed,
            max_extend_per_side=8.0,
        )
        all_storyline_variants = _fill_intra_storyline_gaps(
            all_storyline_variants, max_gap_sec=3.0,
        )
        # snap/extend/fill 이 variant clips 를 변경했으면 shorts #1 clips 동기화 (라운드 17 패턴)
        if all_storyline_variants:
            synced_first = list(all_storyline_variants[0][0])
            if synced_first:
                old_total = sum(c.end_sec - c.start_sec for c in clips)
                new_total = sum(c.end_sec - c.start_sec for c in synced_first)
                if abs(old_total - new_total) > 0.5:
                    print(f"  [clips-sync] shorts #1 clips: {old_total:.1f}s → {new_total:.1f}s")
                clips = synced_first

    # 최종 길이 클램프(무조건 실행): snap/extend/fill 이 target_max 를 넘길 수 있고(대사 경계 스냅·
    # 갭 메움), ffmpeg 렌더 시 컨테이너/오디오 priming 오버헤드(~0.1s)가 더해져 최종 mp4 가 60s 를
    # 살짝 넘는 문제를 막는다. 역할별 trim 으로 hook 시작·payoff 끝을 보존하며 줄인다.
    #
    # 편집실 pinned(첫 variant)는 규칙이 다르다(2026-08-19 참교육_b32cec9f 실측 — 상한을
    # 0.08s 넘었다고 사람이 추가한 19.6s build 를 통째로 삭제했다):
    #   · 상한 = 쇼츠 성립 한계(3분)까지 — 60s 는 자동 생성물의 편집 정책이지 사람의
    #     한계가 아니다. YouTube 는 세로 ≤3분이면 쇼츠로 분류한다(2024-10 확대).
    #   · 통째 제거 금지(allow_remove=False) — 넘치면 역할별 부분 trim 으로만 줄인다.
    #   · 하한(확장) 없음 — 사람이 짧게 만들었으면 그것이 의도다.
    # 클램프 예산도 위 [speed] 규약대로 ×S (정의는 snap/extend 블록 위 _speed 참고)
    _RENDER_SAFETY_MARGIN = 0.3
    _EDIT_PINNED_MAX_SEC = (180.0 - _RENDER_SAFETY_MARGIN) * _speed   # 쇼츠 3분 한계 - 렌더 오버헤드
    _clamp_max = max(float(config.min_duration_sec),
                     float(config.max_duration_sec) - _RENDER_SAFETY_MARGIN) * _speed
    _clamp_lookup = _build_candidates_lookup(all_candidates)
    _clamped_variants: list[tuple[list[StoryClip], str, float]] = []
    for _ci, (_vc, _vt, _vs) in enumerate(all_storyline_variants):
        _pinned_v = _edit_pinned and _ci == 0
        _before = sum(c.end_sec - c.start_sec for c in _vc)
        _vc2, _ = _fit_storyline_to_duration(
            _vc, _clamp_lookup,
            target_min=0.0 if _pinned_v else float(config.min_duration_sec) * _speed,
            target_max=_EDIT_PINNED_MAX_SEC if _pinned_v else _clamp_max,
            allow_remove=not _pinned_v,
        )
        _after = sum(c.end_sec - c.start_sec for c in _vc2)
        if _before - _after > 0.05:
            print(f"  [length-clamp] {_vt[:18]}: {_before:.1f}s → {_after:.1f}s (≤{_clamp_max:.1f}s)")
        _clamped_variants.append((_vc2, _vt, _vs))
    all_storyline_variants = _clamped_variants

    # 렌더 직전 backstop: snap/extend/fill·clamp 가 clip 을 옮기면서 겹침이 다시 생겼을 수 있다.
    # 여기까지 통과한 시간이 곧 concat 될 구간이라, 중복은 이 지점에서 마지막으로 끊는다.
    _deduped_variants: list[tuple[list[StoryClip], str, float]] = []
    for _vi, (_vc, _vt, _vs) in enumerate(all_storyline_variants):
        _vc2, _vmap, _vmsg = _dedup_storyline_clips(_vc)
        if _vmsg:
            print(f"  [clip-dedup/final] {_vt[:18]}: {_vmsg}")
            if _vi < len(storyline_tts_cues_pool):
                storyline_tts_cues_pool[_vi] = list(
                    _remap_cue_clip_indexes(storyline_tts_cues_pool[_vi], _vmap)
                )
        _deduped_variants.append((_vc2 or _vc, _vt, _vs))
    all_storyline_variants = _deduped_variants

    if all_storyline_variants:
        clips = list(all_storyline_variants[0][0])

    # ═══════════════════════════════════════
    # [clip-bounds] 소스 밖 클립 검증 (2026-08-24, 사용자 결정: 크게 실패시킨다)
    # ═══════════════════════════════════════
    # 소스 밖 구간은 렌더에서 아무것도 내놓지 않아 **반쪽짜리 쇼츠가 조용히 발행된다**
    # (clips_beyond_source 독스트링에 실측·기전). 조용한 절반 발행이 실패보다 나쁘므로
    # 여기서 멈춘다 — 여기는 클램프·dedup 이 끝나 클립이 확정된 지점이고, 아직 TTS 합성·
    # 렌더 같은 비싼 단계를 시작하기 전이다.
    #
    # **실제로 렌더될 variant 만** 본다 — 안 쓰는 variant 때문에 편을 죽이지 않는다.
    _src_dur = float(getattr(media_info, "duration_sec", 0.0) or 0.0)
    _oob_lines: list[str] = []
    for _vi, (_vc, _vt, _vs) in enumerate(all_storyline_variants[:max(1, max_shorts)]):
        for _b in clips_beyond_source(_vc, _src_dur):
            _oob_lines.append(
                f"  variant#{_vi}({_vt[:16]}) clip{_b['index']}: "
                f"{_b['start']:.1f}~{_b['end']:.1f}s → 실제 렌더 {_b['rendered']:.1f}s "
                f"(소실 {_b['lost']:.1f}s)")
    if _oob_lines:
        raise ValueError(
            f"소스 밖 클립 — 소스는 {_src_dur:.1f}초인데 그 밖을 가리키는 구간이 있습니다. "
            f"그대로 렌더하면 해당 클립이 통째로/대부분 빠진 **반쪽짜리 쇼츠**가 됩니다.\n"
            + "\n".join(_oob_lines)
            + "\n  → 스토리 재생성이 필요합니다(같은 입력으로 재시도하면 같은 결과입니다).")

    # 자막 데이터 생성 (transcript_text → 편집 타임라인 매핑)
    final_segments = []
    # 편집실이 구간을 바꿨으면 자막을 반드시 다시 매핑한다 — subtitle_segments.json 은
    # **편집본 타임라인 기준**(쇼츠 0초 시작)이라, 구간이 조금만 달라져도 캐시가 통째로
    # 어긋난 자막이 된다(2026-08-16).
    _subtitle_invalidate = (from_step in ("gemini", "graph", "story", "silence_cut", "style",
                                          "resources")
                            or _edit_pinned)
    if not segments_cache_path.exists() or _subtitle_invalidate:
        print("  자막 타임라인 매핑 중...")
        remapped = remap_transcript_to_edited_timeline(
            clips, transcript_text, tts_only_when_no_orig=True,
        )
        merged_segments = merge_subtitle_segments(
            remapped,
            max_gap_sec=0.25,
            # 라운드 24: 15*2=30 → 40. 한국어 한 문장 평균 길이를 더 잘 묶어
            # build_ass 의 \N 동시 표시(2줄)로 깨끗하게 한 화면 표현되도록 한다.
            max_total_chars=40,
        )
        for seg in merged_segments:
            seg_dict = {
                'start_sec': seg.get('start', seg.get('start_sec')) if isinstance(seg, dict) else seg.start_sec,
                'end_sec': seg.get('end', seg.get('end_sec')) if isinstance(seg, dict) else seg.end_sec,
                'text': seg.get('text', "") if isinstance(seg, dict) else seg.text,
            }
            final_segments.append(SimpleNamespace(**seg_dict))
        # E13-2b: 저확신 전사 줄 표시(elevenlabs 명시 실행 전용 — 그 외엔 무조건 0건).
        _lc_flagged = _flag_low_confidence_segments(
            final_segments, clips, checkpoint_chunk_tr_meta,
            backend=payload.transcribe_backend,
        )
        if _lc_flagged:
            print(f"  [Scribe-확신도낮음] 자막 {_lc_flagged}줄에 표시 — 텍스트는 그대로 두고 "
                  f"편집실 자막 검수에서 사람이 먼저 볼 줄로만 남긴다")
            run_log["steps"].append({
                "step": "subtitle_low_confidence",
                "transcribe_backend": payload.transcribe_backend,
                "flagged_lines": _lc_flagged,
                "total_lines": len(final_segments),
            })
        segments_cache_path.write_text(
            json.dumps([_subtitle_segment_json(s) for s in final_segments],
                       ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"  자막 데이터 준비 완료 ({len(final_segments)} segments)")
    elif segments_cache_path.exists():
        cached_data = json.loads(segments_cache_path.read_text(encoding="utf-8"))
        final_segments = [SimpleNamespace(**seg) for seg in cached_data]
        print(f"  자막 캐시 로드 완료 ({len(final_segments)} segments)")

    # ── 편집실 자막 오버라이드(2026-08-17) ──────────────────────────────────
    # 반드시 **재매핑 뒤**다. 구간을 함께 고치면 위 블록이 전사에서 자막을 새로 만드는데,
    # 그보다 앞에서 덮으면 사람이 고친 문장이 조용히 기계 전사로 되돌아간다.
    # 캐시 파일에도 되쓴다 — 편집실을 다시 열 때(editor_assets 가 이 파일을 읽는다)와
    # 다음 렌더에서도 사람이 고친 문장이 그대로 보여야 한다.
    _sub_override = overrides_subtitles(_edit_overrides)
    if _sub_override is not None:
        # v3 앵커 자막(F-401) — source_time_sec 있는 항목은 tts 와 같은 규칙으로
        # 최종 클립(위에서 확정된 clips) 기준 편집 타임라인으로 변환 배치한다.
        # 앵커가 최종 구간에 없으면 tts 고아 규칙과 동일하게 드롭 — 반드시 로그에 남긴다.
        _sub_override, _sub_orphans = place_anchored_subtitles(_sub_override, clips)
        for _o in _sub_orphans:
            print(f"  [edit] 앵커 소재가 최종 타임라인에 없음 → 자막 드롭"
                  f"(tts 고아 규칙과 동일): source_time_sec={_o.get('source_time_sec')} "
                  f"{str(_o.get('text', ''))[:24]!r}")
        final_segments = [SimpleNamespace(**s) for s in _sub_override]
        segments_cache_path.write_text(
            json.dumps(_sub_override, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [edit] 자막 오버라이드 적용 ({len(final_segments)}건) — 사람이 고친 문장")

    # ── 편집실 이미지 오버레이(v3 F-408) ────────────────────────────────────
    # 앵커 규칙은 자막과 동일(원본 절대초 → 담은 클립 오프셋 + 상대시각, 슬롭 ±0.5s)
    # 이라 최종 clips 가 확정된 여기서만 배치할 수 있다. 고아는 tts 규칙대로 드롭 + 로그.
    # 첫 variant 한정(오버라이드 공통 규약) — variant #2·#3 렌더에는 넘기지 않는다.
    _image_overlays: list[dict[str, Any]] = []
    if _edit_images:
        _image_overlays, _img_orphans = place_anchored_images(_edit_images, clips)
        for _o in _img_orphans:
            print(f"  [edit] 앵커 소재가 최종 타임라인에 없음 → 이미지 드롭"
                  f"(tts 고아 규칙과 동일): source_time_sec={_o.get('source_time_sec')} "
                  f"{Path(str(_o.get('file', ''))).name}")
        if _image_overlays:
            print(f"  [edit] 이미지 오버레이 {len(_image_overlays)}건 배치 — 편집 타임라인 변환 완료")

    # ── 편집실 자유 텍스트(v3 F-411) — 배치 규칙은 images 와 동일 ───────────────
    _text_overlays: list[dict[str, Any]] = []
    _edit_texts = overrides_texts(_edit_overrides)
    if _edit_texts:
        _text_overlays, _txt_orphans = place_anchored_texts(_edit_texts, clips)
        for _o in _txt_orphans:
            print(f"  [edit] 앵커 소재가 최종 타임라인에 없음 → 텍스트 드롭"
                  f"(tts 고아 규칙과 동일): source_time_sec={_o.get('source_time_sec')} "
                  f"{str(_o.get('text', ''))[:20]!r}")
        if _text_overlays:
            print(f"  [edit] 텍스트 오버레이 {len(_text_overlays)}건 배치 — 편집 타임라인 변환 완료")

    # ── 편집실 내레이션 오버라이드(v2) ──────────────────────────────────────
    # 반드시 앵커 해석(_resolve_cue_anchors) **앞**이다 — 사람이 보내는 좌표가 원본
    # 절대초(source_time_sec)라, 편집시간 변환은 최종 클립이 확정된 아래 블록이 한다.
    # 구간을 함께 고쳐도(_edit_pinned) 좌표가 안 흔들리는 이유가 이것이다.
    # 첫 variant 만 대상 — clips 오버라이드와 같은 규약.
    _tts_override = overrides_tts(
        _edit_overrides, storyline_tts_cues_pool[0] if storyline_tts_cues_pool else [])
    if _tts_override is not None:
        _before_cues = len(storyline_tts_cues_pool[0]) if storyline_tts_cues_pool else 0
        if storyline_tts_cues_pool:
            storyline_tts_cues_pool[0] = _tts_override
        else:
            storyline_tts_cues_pool = [_tts_override]
        print(f"  [edit] 내레이션 오버라이드 적용 {_before_cues}→{len(_tts_override)}건 "
              f"— 사람이 고친 문구로 재합성")

    # ═══════════════════════════════════════
    # [tts cues] 앵커 해석 — storyline_tts_cues_pool → tts_cues_per_variant
    # ═══════════════════════════════════════
    # cue 는 story 단계에서 (clip_index, offset_sec) 앵커 + source_time_sec(원본 절대시간)로
    # 정규화돼 pool 에 있다. 여기는 silence_cut(분할) → snap/extend/fill → 길이 클램프가
    # **모두 끝난 뒤**이므로, variant 별 최종 클립으로 편집 타임라인 절대시간을 여기서 한 번에
    # 계산한다 (_resolve_cue_anchors). 이후 어떤 단계도 클립 경계를 바꾸지 않는다 —
    # 바꾸는 단계를 새로 넣으면 반드시 이 블록보다 앞이어야 한다.
    tts_cues_per_variant: list[list[dict[str, Any]]] = [
        _resolve_cue_anchors(
            list(storyline_tts_cues_pool[i]) if i < len(storyline_tts_cues_pool) else [],
            all_storyline_variants[i][0],
        )
        for i in range(len(all_storyline_variants))
    ]
    _total_cues = sum(len(c) for c in tts_cues_per_variant)
    if _total_cues:
        print(f"\n[tts cues] 앵커 해석 완료 → {len(tts_cues_per_variant)}개 variant ({_total_cues}개 cue)")
    else:
        print(f"\n[tts cues] cue 0개 — story 단계에서 tts_cues 미생성 / TTS 없이 진행")

    # 첫 번째 variant의 cue를 기본으로 사용 (다중 쇼츠는 [14/15]에서 variant마다 따로)
    tts_cues = tts_cues_per_variant[0] if tts_cues_per_variant else []

    # ═══════════════════════════════════════
    # [style] AI 연출 구성 (E15, 2026-08-23)
    # ═══════════════════════════════════════
    # 기획: ves-orchestrator docs/prompts/e15-style-compose.md · 계약: app/modules/style_compose.py.
    #
    # **--style-compose 를 준 실행에만 돈다.** 미지정이면 이 블록은 통째로 건너뛰고
    # checkpoint_style.json 을 쓰지도 읽지도 않는다 — 필터그래프·subtitle_segments.json·
    # cue 가 종전과 한 글자도 다르지 않아야 한다(회귀 0, auto_update 6대 배포 조건).
    #
    # 왜 여기인가: 위 [tts cues] 블록 주석대로 클립 경계는 **여기서 이미 확정**이다.
    # 연출은 그 위에 얹는 것이라 경계를 건드리지 않는다 — 그래서 이 블록은 좌표를 만들
    # 뿐 clips 를 바꾸지 않는다(바꾸면 자막·cue 좌표가 전부 어긋난다).
    _style_design_kwargs: dict[str, Any] = {}
    _style_plan_applied: dict[str, Any] | None = None
    _text_clamp: dict[str, Any] | None = None      # 효과 텍스트 밴드 클램프 기록(E18-2)
    checkpoint_style = output_dir / "checkpoint_style.json"
    if payload.style_compose:
        from app.modules import style_compose as _stylemod
        print("\n[style] AI 연출 구성 중...")
        _style_notes: list[str] = []
        _style_plan: dict[str, Any] | None = None
        # 재개(--from-step resources|render)에서는 **다시 호출하지 않는다** — 편집실
        # 재렌더가 매번 다른 연출을 내면 사람이 승인한 화면이 재렌더마다 달라진다.
        # 같은 이유로 체크포인트에는 '검증까지 끝난' 플랜을 저장한다.
        if checkpoint_style.exists() and start_idx > step_idx["style"]:
            try:
                _style_plan = json.loads(checkpoint_style.read_text(encoding="utf-8"))
                print(f"  [style] 체크포인트에서 연출 플랜 로드 — 재호출 없음")
            except (OSError, ValueError) as _e:
                print(f"  [style] 체크포인트 읽기 실패({_e}) — 연출 없이 진행")
                _style_plan = None
        elif start_idx <= step_idx["style"]:
            _manifest = _stylemod.load_sticker_manifest(paths.app_root)
            if not _manifest:
                print("  [style] 번들된 스티커 없음 — 스티커 없이 구성한다"
                      "(라이선스 확인된 자산만 번들하는 규율)")
            # LLM 입력은 전부 **원본 절대초** 좌표다 — 플랜의 좌표계와 같아야 표를 보고
            # 그대로 베낄 수 있다(편집본 좌표를 섞으면 앵커가 통째로 어긋난다).
            _tl = [{"role": c.role, "source_start": round(c.start_sec, 1),
                    "source_end": round(c.end_sec, 1),
                    "edit_start": round(sum(x.end_sec - x.start_sec for x in clips[:i]), 1)}
                   for i, c in enumerate(clips)]
            _tr_lines = []
            for _c_i, _c in enumerate(clips):
                _base = sum(x.end_sec - x.start_sec for x in clips[:_c_i])
                for _s in final_segments:
                    _rel = float(_s.start_sec) - _base
                    if 0 <= _rel <= (_c.end_sec - _c.start_sec):
                        _tr_lines.append({"source_sec": round(_c.start_sec + _rel, 1),
                                          "text": str(_s.text)[:60]})
            try:
                _gem_style = locals().get("gemini") or load_gemini_client()
                _raw_plan = _gem_style.compose_style(
                    work_title=payload.work_title, title_text=title_text,
                    timeline=_tl, transcript_lines=_tr_lines,
                    tts_cues=[{"source_time_sec": round(float(c.get("source_time_sec", 0.0)), 1),
                               "voice": c.get("voice"), "speed": c.get("speed"),
                               "text": str(c.get("text", ""))[:50]} for c in tts_cues],
                    sticker_catalog=_stylemod.sticker_catalog_for_prompt(_manifest),
                    # 효과 텍스트 y 허용 구간 — 이 채널의 실제 밴드에서 계산한다(E18).
                    # 프롬프트에 하드코딩된 구간(0.15~0.35)이 제목 자리와 겹쳐 있었다.
                    text_y_range=_stylemod.text_y_range(payload.design),
                    editorial=payload.editorial, reject_note=payload.reject_note,
                )
            except Exception as _e:      # noqa: BLE001 — 연출은 부가물, 본편을 막지 않는다
                print(f"  [style] 호출 실패({type(_e).__name__}: {_e}) — 연출 없이 진행")
                _raw_plan = None
            if _raw_plan is not None:
                try:
                    _style_plan, _style_notes = _stylemod.validate_plan(
                        _raw_plan, manifest=_manifest, app_root=paths.app_root,
                        run_dir=output_dir)
                    checkpoint_style.write_text(
                        json.dumps(_style_plan, ensure_ascii=False, indent=2), encoding="utf-8")
                except _stylemod.StylePlanError as _e:
                    # 계약 위반은 조용히 넘기지 않는다 — 프롬프트가 계약을 못 지킨 것이라
                    # 사람이 알아야 고칠 수 있다. 다만 본편 발행은 막지 않는다.
                    print(f"  [style] 플랜이 계약 위반 → 연출 없이 진행: {_e}")
                    _style_plan = None
                    _style_notes = [f"거절: {_e}"]
            else:
                _style_notes = ["LLM 응답 없음"]

        if _style_plan:
            _style_plan_applied = _style_plan
            for _n in _style_notes:
                print(f"  [style] {_n}")

            # ── 1) 내레이션 톤 (resources 합성보다 앞) ────────────────────
            # 편집실이 내레이션을 보냈으면 그 카테고리는 통째로 사람 것이다(§5).
            if _style_plan.get("tts") and _tts_override is None:
                _tone = {round(float(t["source_time_sec"]), 2): t for t in _style_plan["tts"]}
                _n_tone = 0
                for _cue_list in tts_cues_per_variant:
                    for _cue in _cue_list:
                        _k = round(float(_cue.get("source_time_sec", -1.0)), 2)
                        _hit = _tone.get(_k) or next(
                            (v for kk, v in _tone.items() if abs(kk - _k) <= 1.0), None)
                        if not _hit:
                            continue
                        if _hit.get("voice"):
                            _cue["voice"] = _hit["voice"]
                        if _hit.get("speed"):
                            _cue["speed"] = _hit["speed"]
                        _n_tone += 1
                if _n_tone:
                    print(f"  [style] 내레이션 톤 {_n_tone}개 cue 적용")
            elif _style_plan.get("tts"):
                print("  [style] 내레이션은 편집실이 보낸 것이 이깁니다 — AI 톤 무시")

            # ── 2) 자막 강조 ─────────────────────────────────────────────
            if _style_plan.get("subtitle_styles") and _sub_override is None:
                _n_sub, _sub_drop = _stylemod.apply_subtitle_styles(
                    final_segments, _style_plan["subtitle_styles"], clips)
                for _o in _sub_drop:
                    print(f"  [style] 자막 강조 드롭(tts 고아 규칙과 동일): "
                          f"source_time_sec={_o.get('source_time_sec')} {_o.get('why', '')}")
                if _n_sub:
                    # 강조가 캐시에 남아야 --from-step resources 재개에서도 유지된다.
                    segments_cache_path.write_text(
                        json.dumps([_subtitle_segment_json(s) for s in final_segments],
                                   ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"  [style] 자막 강조 {_n_sub}줄 적용")
            elif _style_plan.get("subtitle_styles"):
                print("  [style] 자막은 편집실이 보낸 것이 이깁니다 — AI 강조 무시")

            # ── 3) 효과 텍스트·스티커 (편집실이 보냈으면 그 카테고리는 사람 것) ──
            if _style_plan.get("texts") and not _text_overlays:
                # 밴드 밖(제목·자막 구역)으로 나간 텍스트는 **드롭이 아니라 클램프**다
                # (2026-08-24 사용자 결정 — 연출을 살리고 위치만 당긴다).
                # ⚠ 배치 **직전**에 건다: 체크포인트에서 되살아난 옛 플랜도 이 길을 지난다.
                # 밴드 기하(aspect_ratio·video_width·video_y)는 AI 가 못 바꾸는 키라
                # 채널 design 이 정본이다(STYLE_DESIGN_ALLOWED 에 없다).
                _y_lo, _y_hi = _stylemod.text_y_range(payload.design)
                _clamped, _clamp_notes = _stylemod.clamp_texts_to_band(
                    _style_plan["texts"], _y_lo, _y_hi)
                for _n in _clamp_notes:
                    print(f"  [style] {_n}")
                # 몇 건을 당겼는지 run_log 에 남긴다 — stdout 에만 있으면 나중에
                # '클램프가 돌긴 했나'를 확인할 길이 없다(검증에서 실제로 막혔다).
                _text_clamp = {"clamped": len(_clamp_notes), "of": len(_clamped),
                               "y_range": [round(_y_lo, 4), round(_y_hi, 4)]}
                _placed, _drop = place_anchored_texts(_clamped, clips)
                for _o in _drop:
                    print(f"  [style] 앵커 소재가 최종 타임라인에 없음 → 텍스트 드롭"
                          f"(tts 고아 규칙과 동일): source_time_sec={_o.get('source_time_sec')} "
                          f"{str(_o.get('text', ''))[:20]!r}")
                _text_overlays = _placed
                if _placed:
                    print(f"  [style] 효과 텍스트 {len(_placed)}건 배치")
            elif _style_plan.get("texts"):
                print("  [style] 텍스트는 편집실이 보낸 것이 이깁니다 — AI 텍스트 무시")

            if _style_plan.get("images") and not _image_overlays:
                # 편집실 이미지와 **같은 함수**로 파일을 확인한다(경로 탈출·용량 상한).
                # ⚠ 편집실 이미지는 파일이 없으면 크게 실패해야 맞다(어댑터 버그이고 사람이
                # 올린 것이 증발하면 안 된다). 스티커는 반대다 — 체크포인트만 남고
                # style_assets/ 가 사라진 재개(번들 복원 등)에서 연출 하나 때문에 본편
                # 렌더가 죽으면 안 된다. 그래서 여기서만 잡아 연출을 접는다.
                try:
                    _resolved = resolve_image_files({"images": _style_plan["images"]}, output_dir)
                except EditOverrideError as _e:
                    print(f"  [style] 스티커 파일을 못 찾음 → 스티커 없이 진행: {_e}")
                    _resolved = []
                _placed_i, _drop_i = place_anchored_images(_resolved, clips)
                for _o in _drop_i:
                    print(f"  [style] 앵커 소재가 최종 타임라인에 없음 → 스티커 드롭"
                          f"(tts 고아 규칙과 동일): source_time_sec={_o.get('source_time_sec')} "
                          f"{Path(str(_o.get('file', ''))).name}")
                _image_overlays = _placed_i
                if _placed_i:
                    print(f"  [style] 스티커 {len(_placed_i)}건 배치")
            elif _style_plan.get("images"):
                print("  [style] 이미지는 편집실이 보낸 것이 이깁니다 — AI 스티커 무시")

            # ── 4) 시간대별 제목 (편집실 title.segments 가 이긴다) ─────────
            if _style_plan.get("title_segments") and not _title_segments:
                try:
                    # base_title 을 주면 창이 못 덮은 시간을 기본 제목으로 메운다 —
                    # **AI 가 만든 창은 제목을 없애지 못한다**(2026-08-24 사용자 지시).
                    _segs, _seg_drop = _stylemod.title_segments_from_anchors(
                        _style_plan["title_segments"], clips, base_title=title_text)
                    for _o in _seg_drop:
                        if _o.get("note"):
                            print(f"  [style] {_o.get('why', '')}")
                            continue
                        print(f"  [style] 제목 창 드롭: {_o.get('why', '앵커가 타임라인 밖')} "
                              f"{str(_o.get('text', ''))[:20]!r}")
                    if _segs:
                        _title_segments = _segs
                        print(f"  [style] 시간대별 제목 {len(_segs)}개 창 "
                              f"— 빈 시간은 기본 제목으로 채웁니다")
                except EditOverrideError as _e:
                    print(f"  [style] 제목 창이 계약 위반 → 제목은 종전대로: {_e}")
            elif _style_plan.get("title_segments"):
                print("  [style] 제목은 편집실이 보낸 것이 이깁니다 — AI 제목 창 무시")

            # ── 5) 디자인 (채널이 명시한 키는 AI 가 못 덮는다) ──────────────
            _style_design_kwargs, _d_notes = _stylemod.design_overrides(
                _style_plan.get("design"), payload.design_explicit_fields, payload.design)
            for _n in _d_notes:
                print(f"  [style] {_n}")
            if _style_design_kwargs:
                print(f"  [style] 디자인 {sorted(_style_design_kwargs)} 적용")

        run_log["steps"].append({
            "step": "style",
            "plan": bool(_style_plan),
            "texts": len((_style_plan or {}).get("texts") or []),
            "images": len((_style_plan or {}).get("images") or []),
            "subtitle_styles": len((_style_plan or {}).get("subtitle_styles") or []),
            "title_segments": len((_style_plan or {}).get("title_segments") or []),
            "tts_tone": len((_style_plan or {}).get("tts") or []),
            "design": sorted((_style_plan or {}).get("design") or {}),
            "notes": _style_notes,
            "concept": (_style_plan or {}).get("notes"),
            # 검수 카드가 '왜 이 연출인지'를 보여 준다(오케스트레이터 brain 이 읽는다).
            "reasons": [str(t.get("reason") or "")
                        for t in ((_style_plan or {}).get("texts") or []) if t.get("reason")],
            # E18-2 — 밴드 밖으로 나간 효과 텍스트를 몇 건 당겼는지. 배치가 안 돌았으면 None.
            "text_clamp": _text_clamp,
        })

    # ═══════════════════════════════════════
    # [13/15] 리소스 생성 (크롭, TTS, 편집 계획)
    # ═══════════════════════════════════════
    checkpoint_resources = output_dir / "checkpoint_resources.json"
    edit_plan_path = output_dir / "edit_plan.json"

    # story/graph 단계부터 재실행 시 클립 구성이 달라질 수 있으므로 resources 캐시 무효화.
    # 이전 라운드의 crop_map 키(role_idx)와 새 라운드의 clip 키가 어긋나면 KeyError 발생.
    # 2026-08-16 보정: 'silence_cut' 이 빠져 있었다. tts_plan·transcribe 는 위에서 이미
    # silence_cut 으로 redirect 된 뒤라 **절대 참이 될 수 없는 죽은 조건**이고(하위호환용
    # 으로 남겨둠), 정작 클립을 바꾸는 silence_cut 재개가 목록에 없어 crop_map 키(role_idx)가
    # 새 clips 와 어긋난 채 캐시가 채택되고 있었다 — 크롭이 조용히 빠지거나 남의 것이 붙는다.
    # 편집실이 구간을 바꾼 경우(_edit_pinned)도 같은 이유로 재생성한다.
    # 내레이션을 고친 경우(_tts_override)도 재생성 — mp3 는 cue 문구에서 합성되므로
    # 캐시를 쓰면 사람이 고친 문구가 소리에 반영되지 않는다.
    # E7-2: 배속이 바뀌면 TTS fit 목표((e-s)/S)가 달라진다 — 캐시 mp3 를 그대로 쓰면
    # 배속 후 cue 창을 넘치는 내레이션이 조용히 나간다. 체크포인트에 video_speed 를
    # 기록해 두고 다르면 리소스를 재생성한다(크롭 재생성 비용은 정확성에 양보).
    _resources_speed_stale = False
    if checkpoint_resources.exists():
        try:
            _ck_speed = float(json.loads(
                checkpoint_resources.read_text(encoding="utf-8")).get("video_speed", 1.0))
            _resources_speed_stale = abs(_ck_speed - _speed) > 1e-9
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            _resources_speed_stale = True
        if _resources_speed_stale:
            print(f"  [speed] 체크포인트 video_speed ≠ {_speed:g} — 리소스(TTS fit) 재생성")
    _resources_invalidate = (from_step in ("gemini", "graph", "story", "tts_plan",
                                           "transcribe", "silence_cut", "style", "resources")
                             or _edit_pinned or _tts_override is not None
                             or _resources_speed_stale)
    # 라운드 9-fix: face_identifier를 resources 단계 *밖*에서도 안전하게 사용할 수 있도록 미리 None 초기화.
    # --from-step render로 들어와 라인 1490 캐시 로드 분기를 타면 face_identifier가 정의되지 않아
    # 멀티 variant 렌더링 단계(라인 1862, 1873)에서 UnboundLocalError 발생.
    face_identifier = None
    # 라운드 11-fix: 라운드 10 자막 스타일 관련 변수들을 모두 함수 단위로 호이스팅 —
    # 멀티 variant 분기에서도 안전하게 사용. 라운드 10이 첫 variant 분기 안에서만 정의해서
    # 멀티 variant #2/#3 렌더링 시 NameError 발생했음 (face_identifier 라운드 9-fix와 동일 패턴).
    from app.config import DesignConfig as _DefaultDC
    from app.modules.subtitle_styles import select_subtitle_style
    _default_design = _DefaultDC()
    _genre_tag = research_raw_data.get("genre_tag")
    _cli_override = getattr(payload.design, "subtitle_style_preset", None)
    if start_idx <= step_idx["resources"] and checkpoint_resources.exists() and not _resources_invalidate:
        print("\n[13/15] 리소스 로드 중...")
        resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
        # --no-reframe 로 재렌더할 때 캐시된 크롭을 그대로 쓰면 플래그가 무시된다
        # (재렌더는 resources 를 다시 만들지 않으므로 여기서 걸러야 한다).
        crop_map = ({} if not getattr(payload.design, "enable_reframe", True)
                    else {k: Path(v) for k, v in resources_data["crop_map"].items()})
        tts_cue_files = resources_data.get("tts_cue_files", []) or []
        print(f"  - 크롭 타임라인: {len(crop_map)}개, TTS cue 오디오: {len(tts_cue_files)}개")
        print("[OK] 리소스 로드 완료 (체크포인트에서)")
    elif (start_idx <= step_idx["resources"] or _edit_pinned or _tts_override is not None
          or _resources_speed_stale):
        # _edit_pinned(2026-08-16): --from-step render 로 들어와도 사람이 구간을 바꿨으면
        # 크롭을 다시 만든다. crop_map 키는 f"{role}_{idx}" 라 구간이 달라진 채 캐시를 쓰면
        # **옛 구간의 얼굴 위치**로 크롭이 잡힌다(키가 우연히 맞으면 남의 크롭, 안 맞으면 무크롭).
        # 클립 총량이 60초라 재생성 비용은 작고, 틀린 크롭보다 낫다.
        print("\n[13/15] 리소스 생성 중...")
        resource_start = time.time()

        # Phase 12: 인물 인식 레퍼런스 빌드 (배우 사진이 있을 때만)
        face_identifier = None
        if cast_images and payload.design.enable_face_recognition:
            try:
                from app.modules.face_id import FaceIdentifier
                fi = FaceIdentifier()
                fi.build_references(cast_images)
                if fi.references:
                    face_identifier = fi
                    print(f"  [FaceID] 인물 인식 레퍼런스: {len(fi.references)}명")
                else:
                    print("  [FaceID] 유효한 레퍼런스 없음 — 화자 추적 폴백")
            except ImportError:
                print("  [FaceID] deepface 미설치 — 화자 추적 폴백")
            except Exception as e:
                print(f"  [FaceID] 초기화 실패: {e} — 화자 추적 폴백")

        # 얼굴 크롭 타임라인
        crop_map = {}
        # enable_reframe=False (--no-reframe) 면 크롭 타임라인을 아예 만들지 않는다. 렌더러는
        # crop_map 에 없는 클립을 가운데 정렬로 넣으므로 빈 dict 가 곧 '리프레이밍 없음'이다.
        # 이 단계가 resources 에서 가장 느리므로 생성 시간도 그만큼 줄어든다.
        _reframe_on = getattr(payload.design, "enable_reframe", True)
        if not _reframe_on:
            print(f"  크롭 타임라인 생략 (--no-reframe) — 원본 가운데 정렬로 렌더")
        else:
            print(f"  크롭 타임라인 생성 중... ({len(clips)}개 클립)")
        # 라운드 24: 컷 경계 위치 점프 완화. 직전 클립의 마지막 keyframe(x,y)과 타겟 인물을 다음 호출에 sticky anchor 로 전달.
        prev_anchor_x: float | None = None
        prev_anchor_y: float | None = None
        prev_focus_char: str | None = None
        for idx, clip in enumerate(clips) if _reframe_on else []:
            crop_path = output_dir / f"crop_{clip.role}_{idx}.json"
            # 라운드 19A: ≥2 character_focus + face_identifier/character_index 가능하면 멀티 크롭 (와이드 프레이밍)
            multi_targets = list(clip.character_focus) if (clip.character_focus and len(clip.character_focus) >= 2) else None
            current_target_char: str | None = None
            if multi_targets and (face_identifier or character_appearances) and payload.design.enable_face_recognition:
                from app.modules.reframe import build_multi_face_crop_timeline
                print(f"    [multi-crop] clip {idx}: {multi_targets} 와이드 프레이밍")
                build_multi_face_crop_timeline(
                    payload.video_path.resolve(),
                    crop_path,
                    media_info.width,
                    media_info.height,
                    config.crop_sample_interval_sec,
                    start_sec=clip.start_sec,
                    end_sec=clip.end_sec,
                    target_characters=multi_targets,
                    face_identifier=face_identifier,
                    character_index=character_appearances,
                )
                # multi 분기는 sticky 미적용 (MVP) — anchor 갱신만
                current_target_char = multi_targets[0] if multi_targets else None
            else:
                # Phase 12: character_focus 첫 번째 인물을 타겟으로
                target_char = clip.character_focus[0] if clip.character_focus and face_identifier else None
                build_crop_timeline(
                    payload.video_path.resolve(),
                    crop_path,
                    media_info.width,
                    media_info.height,
                    config.crop_sample_interval_sec,
                    start_sec=clip.start_sec,
                    end_sec=clip.end_sec,
                    enable_speaker_tracking=payload.design.enable_speaker_tracking,
                    target_character=target_char,
                    face_identifier=face_identifier,
                    character_index=character_appearances,
                    initial_x=prev_anchor_x,
                    initial_y=prev_anchor_y,
                    prev_target_character=prev_focus_char,
                )
                current_target_char = target_char
            crop_map[f"{clip.role}_{idx}"] = crop_path
            # 라운드 24: 방금 생성된 keyframe 의 마지막 위치를 다음 클립에 전달
            try:
                _kfs = json.loads(crop_path.read_text(encoding="utf-8"))
                if isinstance(_kfs, list) and _kfs:
                    prev_anchor_x = float(_kfs[-1].get("x_center", prev_anchor_x or 0.0))
                    prev_anchor_y = float(_kfs[-1].get("y_center", prev_anchor_y or 0.0))
                    prev_focus_char = current_target_char
            except (json.JSONDecodeError, OSError, KeyError, ValueError):
                pass  # 다음 클립은 sticky 없이 진행
            if (idx + 1) % 5 == 0 or (idx + 1) == len(clips):
                print(f"    진행 중... ({idx + 1}/{len(clips)})")

        # TTS 오디오 생성 (cue별 — voice/speed 적용)
        # cue 시간(end_sec - start_sec) 안에 들어가도록 fit. 초과 시 Flash로 텍스트 단축.
        print("  TTS 오디오 생성 중 (cue별, fit 적용)...")
        from app.modules.tts import active_backend
        from app.modules.tts import elevenlabs_disabled as _tts_elevenlabs_disabled
        _flash_for_shorten = locals().get("gemini") or load_gemini_client()
        _shorten = getattr(_flash_for_shorten, "shorten_text", None) if _flash_for_shorten else None
        tts_cue_files: list[dict[str, Any]] = []
        for ci, cue in enumerate(tts_cues):
            tts_path = output_dir / f"tts_cue_{ci}.mp3"
            # E7-2: cue 창(소스 편집 타임라인)은 배속 후 출력에서 (e-s)/S 로 줄어든다 —
            # 내레이션 오디오는 배속하지 않으므로 fit 목표가 곧 출력 시각의 창이어야 한다.
            target_sec = max(0.5, (float(cue.get("end_sec", 0.0))
                                   - float(cue.get("start_sec", 0.0))) / _speed)
            final_text, actual_sec = synthesize_cue_cached(
                cue["text"], tts_path, target_sec=target_sec,
                voice=cue.get("voice", "narrative_female"),
                speed=cue.get("speed", "normal"),
                shorten_fn=_shorten,
                cache_dir=output_dir / "tts_cache",
            )
            # fit 결과를 cue에 반영(자막 일관성 + 디버깅용)
            cue["text"] = final_text
            cue["fit_actual_sec"] = actual_sec
            tts_cue_files.append({
                "cue_index": ci,
                "path": str(tts_path),
                "cue": cue,
            })
            if (ci + 1) % 3 == 0 or (ci + 1) == len(tts_cues):
                print(f"    진행 중... ({ci + 1}/{len(tts_cues)})")

        resource_elapsed = time.time() - resource_start
        # E11: 어느 TTS 백엔드로 합성됐는지 남긴다 — 키 없는 노드의 edge-tts 폴백을
        # 검수함·run_log 에서 추적할 수 있어야 한다(조용한 대체 금지 원칙).
        _tts_backend = active_backend()
        # E17: 키가 있는데도 edge-tts 로 나갔다면 토큰이 거절당한 것이다 — 사유를 남긴다
        # (남기지 않으면 '이 편만 목소리가 다르다'의 원인을 검수함에서 찾을 수 없다).
        _tts_fallback = _tts_elevenlabs_disabled()
        _res_entry: dict[str, Any] = {"step": "resources", "tts_backend": _tts_backend,
                                      "tts_cues": len(tts_cue_files)}
        if _tts_fallback:
            _res_entry["tts_fallback_reason"] = "elevenlabs_auth_expired"
            _res_entry["tts_fallback_detail"] = _tts_fallback[:200]
            print(f"  - [주의] ElevenLabs 토큰 거절로 기본 백엔드 합성: {_tts_fallback[:120]}")
        run_log["steps"].append(_res_entry)
        checkpoint_resources.write_text(
            json.dumps({
                "crop_map": {k: str(v) for k, v in crop_map.items()},
                "tts_cue_files": tts_cue_files,
                # E7-2: TTS fit 목표가 (e-s)/S 라 배속이 바뀌면 이 캐시는 못 쓴다(위 무효화 참고)
                "video_speed": _speed,
                "tts_backend": _tts_backend,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] 리소스 생성 완료 (소요 시간: {resource_elapsed:.1f}초)")
        print(f"  - TTS cue 오디오: {len(tts_cue_files)}개 (backend={_tts_backend})")
    else:
        if checkpoint_resources.exists():
            resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
            crop_map = ({} if not getattr(payload.design, "enable_reframe", True)
                        else {k: Path(v) for k, v in resources_data["crop_map"].items()})
            tts_cue_files = resources_data.get("tts_cue_files", []) or []
        elif edit_plan_path.exists():
            edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8"))
            crop_map = {}
            for idx, clip_data in (enumerate(edit_plan["timeline"])
                                   if getattr(payload.design, "enable_reframe", True) else []):
                crop_filename = clip_data["reframe"]["crop_timeline_ref"]
                crop_path = output_dir / crop_filename
                if crop_path.exists():
                    crop_map[f"{clip_data['role']}_{idx}"] = crop_path
            tts_cue_files = []
        else:
            raise FileNotFoundError("체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다.")

    # 편집 계획 생성 — **항상** 쓴다(2026-08-16 수정).
    # 종전엔 `start_idx <= resources` 일 때만 써서, --from-step render 재렌더가 옛 edit_plan 을
    # 그대로 남겼다. 그러면 디스크의 계획과 실제 mp4 가 서로 다른 내용을 주장하고, edit_plan 을
    # 읽는 하류(ingest·관제 편집실)가 영상에 없는 구간을 보게 된다. 렌더 시점엔 clips·title·
    # crop_map 이 모두 확정돼 있으므로 여기서 쓰는 값이 곧 화면에 나간 값이다.
    print("  편집 계획 생성 중...")
    edit_plan = _build_edit_plan(payload, title_text, clips, crop_map, config,
                                 title_segments=_title_segments)
    # 라운드 6b: skeleton 단계 제거됨. edit_plan에 임베드하던 narrative_skeleton 키도 사라짐.
    edit_plan_path.write_text(json.dumps(edit_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  - 편집 계획 저장: {edit_plan_path}")

    # ═══════════════════════════════════════
    # [14/15] 자막 디자인 + 최종 렌더링
    # ═══════════════════════════════════════
    output_video = output_dir / "shorts.mp4"
    subtitle_path = output_dir / "subtitles.ass"

    # E17-2: 소스에 박힌 원본 자막 띠(캔버스 y) — 렌더 블록에서 한 번 재고 variant 도 같은
    # 값을 쓴다(같은 소재의 같은 방송 자막이다). 검출은 `--design-subtitle-avoid-burned off`
    # 로 끌 수 있고, 못 찾으면 None = 종전 자막 위치 그대로다.
    _burned_band: dict[str, Any] | None = None
    _avoid_log: dict[str, Any] | None = None

    if start_idx <= step_idx["render"] or from_step == "render" or not output_video.exists():
        # 자막 디자인 적용
        print("\n[14/15] 자막 디자인 적용 및 최종 렌더링 중...")

        # 자막을 아예 안 그리는 채널(--no-subtitles)은 잴 것도 없다 — 표본 뜨는 시간만 든다.
        if payload.show_subtitles:
            _burned_band = _detect_burned_band_cached(payload, config, clips, crop_map, output_dir)
        # ⚠ 종전엔 **띠를 찾은 실행만** 남겼다. 그러면 단계가 없을 때 '띠가 없었다'인지
        #   '판정 자체가 안 돌았다'인지 구분이 안 된다 — 실제로 검증에서 막혔다.
        #   판정이 돌았으면(자막 있는 채널 · auto) 결과가 비어도 남긴다.
        #   `off` 채널·`--no-subtitles` 는 표본을 아예 안 뜨므로 종전과 같이 아무것도 안 남는다.
        if payload.show_subtitles and (_burned_band or _BURNED_PROFILES):
            _avoid_log = {
                "step": "subtitle_avoid_burned",
                "band": ({k: _burned_band[k] for k in ("top", "bottom")}
                         if _burned_band else None),
                "frames": (_burned_band or {}).get("frames"),
                "clips": (_burned_band or {}).get("clips"),
                "hit_ratio": (_burned_band or {}).get("hit_ratio"),
                # 구간별 재판정(E18-6)이 쓴 표본 수 — 0 이면 구간 보정이 아예 불가능했다는 뜻.
                "profile_frames": len(_BURNED_PROFILES),
            }
            run_log["steps"].append(_avoid_log)

        if not final_segments and segments_cache_path.exists():
            cached_data = json.loads(segments_cache_path.read_text(encoding="utf-8"))
            final_segments = [SimpleNamespace(**seg) for seg in cached_data]

        tts_subtitle_path = None
        if final_segments:
            # 자막 위치: 영상 영역 끝에서 10px 위 (사용자 요구)
            _sub_margin_v = _compute_subtitle_margin_v(
                payload.design,
                canvas_width=config.canvas_width,
                canvas_height=config.canvas_height,
                padding_px=10,
            )
            # 라운드 10: 장르 기반 자막 스타일 자동 선택
            # (_default_design / select_subtitle_style / _genre_tag / _cli_override 모두
            #  함수 단위로 미리 초기화됨 — 라운드 11-fix)
            # CLI 사용자가 명시 변경한 필드만 프리셋 위에 덮어쓰기 (기본값과 같으면 None=프리셋 그대로)
            _user_overrides = {
                "font_name": payload.design.subtitle_font if payload.design.subtitle_font != _default_design.subtitle_font else None,
                "font_size": payload.design.subtitle_size if payload.design.subtitle_size != _default_design.subtitle_size else None,
                "primary_color": payload.design.subtitle_color,  # None = 프리셋 색 유지
                "margin_v": _sub_margin_v,  # 라운드 5 동적 계산은 항상 우선
            }
            sub_style, _applied_preset = select_subtitle_style(_genre_tag, _cli_override, _user_overrides)
            print(f"  [SubtitleStyle] genre_tag={_genre_tag!r} cli={_cli_override!r} → preset={_applied_preset}")
            # E17-2: 원본 자막 회피는 **프리셋이 정해진 뒤**다 — 자막 글자 크기가 확정돼야
            # 블록 높이(겹치는지·얼마나 올릴지)를 잴 수 있다(프리셋마다 56~72px 로 다르다).
            _sub_margin_v2 = _avoid_burned_margin_v(
                _sub_margin_v, _burned_band, design=payload.design, config=config,
                title_text=title_text, font_size=sub_style.font_size, label="[대사] ")
            if _sub_margin_v2 != _sub_margin_v:
                sub_style = replace(sub_style, margin_v=_sub_margin_v2)
                _sub_margin_v = _sub_margin_v2

            # TTS 자막 세그먼트 생성 — 라운드 12.2: end_sec을 *mp3 실제 길이* 기준으로 갱신.
            # 이전엔 cue 계획 시간 그대로 사용 → mp3가 cue 길이보다 짧으면(0.5~1s) 음성 끝난 후 자막
            # 잔류. 사용자 보고 "TTS 자막이 오디오와 다르다"의 원인.
            # E7-2: TTS 자막은 렌더 그래프의 setpts **뒤**에 입혀지므로 출력 시각이다 —
            # 시작은 cue 시각 ×1/S, 끝은 시작 + mp3 실제 길이(내레이션은 배속하지 않으니
            # 실시간 그대로). fit 목표가 (e-s)/S 라 창(e/S)을 넘지 않는다.
            tts_line_segs: list[SimpleNamespace] = []
            for _cf in tts_cue_files:
                _cue = _cf.get("cue", {})
                _cue_start = float(_cue.get("start_sec", 0.0)) / _speed
                _cue_end_planned = float(_cue.get("end_sec", 0.0)) / _speed
                _mp3_path = _cf.get("path")
                _cue_end = _cue_end_planned
                if _mp3_path and Path(_mp3_path).exists():
                    _mp3_dur = _get_audio_duration(Path(_mp3_path))
                    if _mp3_dur > 0:
                        _cue_end = _cue_start + _mp3_dur
                tts_line_segs.append(SimpleNamespace(
                    start_sec=_cue_start,
                    end_sec=_cue_end,
                    text=str(_cue.get("text", "")),
                ))

            # E17-2: TTS(내레이션) 자막도 원본 자막과 겹치면 안 된다(대사 자막과 같은 이유·
            # 같은 상한 — 제목 아래를 넘지 않는다). 밴드 앵커(E10) 뒤, 프리셋(TTS 는 채널
            # tts_line_font_size 고정이라 여기서 이미 확정)이 있으니 바로 계산한다.
            _tts_margin_v = _compute_tts_margin_v(
                payload.design, canvas_width=config.canvas_width,
                canvas_height=config.canvas_height)
            _tts_margin_v = _avoid_burned_margin_v(
                _tts_margin_v, _burned_band, design=payload.design, config=config,
                title_text=title_text, font_size=payload.design.tts_line_font_size,
                label="[TTS] ")
            tts_line_style = SubtitleStyle(
                font_name=payload.design.subtitle_font,
                font_size=payload.design.tts_line_font_size,
                primary_color=payload.design.tts_line_color,
                # E10 후속 3: 밴드(video_width·video_y)가 움직인 만큼 TTS 줄도 따라간다
                margin_v=_tts_margin_v,
            ) if tts_line_segs else None

            # TTS 활성 시간 범위 계산 (메인 자막 숨김용)
            tts_time_ranges = [(seg.start_sec, seg.end_sec) for seg in tts_line_segs] if tts_line_segs else None

            # E7-2: 대사 자막도 출력 시각(×1/S)으로 사본만 변환해 ASS 를 만든다 —
            # segments_cache(소스 편집 타임라인, 편집실 원료)는 그대로 둔다.
            # E18-2: 전역 띠로 정한 margin 위에, 줄마다 그 순간의 원본 자막을 다시 피한다.
            # 사본만 만든다 — final_segments·subtitle_segments.json 은 안 움직인다.
            _segs_for_ass = _avoid_burned_windowed(
                final_segments, _sub_margin_v, design=payload.design, config=config,
                title_text=title_text, font_size=sub_style.font_size, label="[대사] ")
            build_ass_from_segments(_scale_segments_for_speed(_segs_for_ass, _speed),
                                    subtitle_path, sub_style, tts_time_ranges=tts_time_ranges)
            print(f"  [OK] 자막 파일 생성 완료: {subtitle_path}")

            tts_subtitle_path = output_dir / "tts_subtitles.ass"
            if tts_line_segs and tts_line_style:
                build_tts_ass(_avoid_burned_windowed(
                                  tts_line_segs, _tts_margin_v, design=payload.design,
                                  config=config, title_text=title_text,
                                  font_size=tts_line_style.font_size, label="[TTS] "),
                              tts_subtitle_path, tts_line_style,
                              rotate_deg=getattr(payload.design, "tts_rotate", 0.0))
                print(f"  [OK] TTS 자막 파일 생성 완료: {tts_subtitle_path}")
            else:
                tts_subtitle_path = None
        else:
            # 라운드 11.1-B: final_segments=[]면 *빈 ass*를 작성해서 옛 ass 잔재가
            # ffmpeg에 들어가지 않게 한다. 이전엔 skip해서 옛 storyline 시점의 자막이
            # 새 영상에 그대로 입혀지는 어긋남 발생.
            print("  - [주의] 자막 데이터 0건 — 빈 .ass로 옛 자막 잔재 제거")
            _genre_tag = research_raw_data.get("genre_tag")
            _cli_override = getattr(payload.design, "subtitle_style_preset", None)
            _empty_sub_style, _ = select_subtitle_style(_genre_tag, _cli_override, None)
            build_ass_from_segments([], subtitle_path, _empty_sub_style, tts_time_ranges=None)
            # fix(subtitle): 대사 자막이 0개여도 TTS 내레이션 자막은 독립적으로 표시한다
            #   (renderer.py:540 주석의 의도 — TTS 자막은 메인 자막과 무관). 이전엔 여기서
            #   None으로 꺼버려 TTS 음성만 나오고 자막이 사라지는 버그가 있었다.
            tts_line_segs = []
            for _cf in tts_cue_files:
                _cue = _cf.get("cue", {})
                # E7-2: 위 분기와 동일 — 시작 ×1/S, 끝 = 시작 + mp3 실시간 길이
                _cue_start = float(_cue.get("start_sec", 0.0)) / _speed
                _cue_end = float(_cue.get("end_sec", 0.0)) / _speed
                _mp3_path = _cf.get("path")
                if _mp3_path and Path(_mp3_path).exists():
                    _mp3_dur = _get_audio_duration(Path(_mp3_path))
                    if _mp3_dur > 0:
                        _cue_end = _cue_start + _mp3_dur
                tts_line_segs.append(SimpleNamespace(
                    start_sec=_cue_start, end_sec=_cue_end, text=str(_cue.get("text", "")),
                ))
            if tts_line_segs:
                # E17-2: 대사 자막이 0건이어도 TTS 는 독립적으로 나가므로 여기도 회피를 탄다.
                _tts_margin_v = _compute_tts_margin_v(
                    payload.design, canvas_width=config.canvas_width,
                    canvas_height=config.canvas_height)
                _tts_margin_v = _avoid_burned_margin_v(
                    _tts_margin_v, _burned_band, design=payload.design, config=config,
                    title_text=title_text, font_size=payload.design.tts_line_font_size,
                    label="[TTS] ")
                tts_line_style = SubtitleStyle(
                    font_name=payload.design.subtitle_font,
                    font_size=payload.design.tts_line_font_size,
                    primary_color=payload.design.tts_line_color,
                    # E10 후속 3: 정본 분기와 동일 — 밴드 이동을 TTS 줄이 따라간다
                    margin_v=_tts_margin_v,
                )
                tts_subtitle_path = output_dir / "tts_subtitles.ass"
                build_tts_ass(_avoid_burned_windowed(
                                  tts_line_segs, _tts_margin_v, design=payload.design,
                                  config=config, title_text=title_text,
                                  font_size=tts_line_style.font_size, label="[TTS] "),
                              tts_subtitle_path, tts_line_style,
                              rotate_deg=getattr(payload.design, "tts_rotate", 0.0))
                print(f"  [OK] TTS 자막 파일 생성 완료 (대사자막 0건): {tts_subtitle_path}")
            else:
                tts_subtitle_path = None

        # 최종 렌더링
        print(f"  최종 영상 렌더링 중... (출력: {output_video})")
        render_start = time.time()
        actual_font_path = get_font_path(payload.design.title_font, paths.app_root)
        actual_subtitle_font_path = get_font_path(payload.design.subtitle_font, paths.app_root)
        updated_design = replace(
            payload.design,
            title_font=actual_font_path,
            subtitle_font=actual_subtitle_font_path,
            # E15: AI 연출의 디자인 키(제목 기울기·박스 등). 채널이 명시한 키는 style 단계에서
            # 이미 걸러졌다(design_overrides). 빈 dict 면 종전과 완전히 같은 호출이다.
            **_style_design_kwargs,
        )

        # 자유 텍스트 ASS(F-411) — 항목이 있을 때만 파일을 만들고 렌더에 넘긴다. 배속은
        # 대사 자막(E7-2)과 같은 이유로 ×1/S.
        text_subtitle_path: Path | None = None
        if _text_overlays:
            from app.modules.subtitle import build_texts_ass
            text_subtitle_path = output_dir / "texts.ass"
            build_texts_ass(_text_overlays, text_subtitle_path, speed=_speed)
            print(f"  [OK] 텍스트 레이어 ASS 생성: {text_subtitle_path} ({len(_text_overlays)}건)")

        render_inputs = RenderInputs(
            video_path=payload.video_path,
            clips=clips,
            subtitle_path=subtitle_path if (payload.show_subtitles and subtitle_path.exists()) else None,
            crop_timeline_map=crop_map,
            title_text=title_text if payload.show_title_overlay else "",
            work_title=payload.work_title if payload.show_title_overlay else "",
            design=updated_design,
            output_path=output_video,
            loudness_target_lufs=payload.loudness_target_lufs,
            canvas_width=config.canvas_width,
            canvas_height=config.canvas_height,
            top_title_height=config.top_title_height,
            bottom_label_height=config.bottom_label_height,
            tts_subtitle_path=tts_subtitle_path if payload.show_tts_subtitles else None,
            tts_cue_files=(tts_cue_files
                           if (tts_cue_files and payload.include_tts_audio) else None),
            original_audio_gain_db=config.original_gain_db,
            tts_audio_gain_db=config.tts_gain_db,
            render_preset=config.render_preset,
            enable_hwaccel=config.enable_hwaccel,
            image_overlays=_image_overlays or None,
            title_segments=(_title_segments
                            if (payload.show_title_overlay and _title_segments) else None),
            text_subtitle_path=text_subtitle_path,
        )

        ffmpeg_cmd = render_short(render_inputs)
        render_elapsed = time.time() - render_start

        cmd_serializable = [str(item) if isinstance(item, Path) else item for item in ffmpeg_cmd]
        run_log["steps"].append({"step": "render", "command": cmd_serializable})
        print(f"[OK] 최종 렌더링 완료 (소요 시간: {render_elapsed:.1f}초)")
    else:
        print(f"\n[14/15] 렌더링 스킵 (이미 파일 존재: {output_video.name})")

    # ═══════════════════════════════════════
    # [15/15] 출력 검증
    # ═══════════════════════════════════════
    if start_idx <= step_idx["validate"]:
        print("\n[15/15] 출력 검증 중...")
        if not output_video.exists():
            raise FileNotFoundError(f"검증할 영상 파일을 찾을 수 없습니다: {output_video}")

        # 편집실 pinned 는 클램프와 같은 기준으로 검증 — 사람이 3분 안에서 정한 길이를
        # 60s 정책으로 FAIL 찍으면 로그가 거짓말을 한다(클램프 블록 주석 참고).
        validation = validate_output(
            output_video,
            1.0 if _edit_pinned else config.min_duration_sec,
            180.0 if _edit_pinned else config.max_duration_sec,
        )
        validation_dict = validation.__dict__.copy()
        for key, value in validation_dict.items():
            if isinstance(value, Path):
                validation_dict[key] = str(value)
        run_log["steps"].append({"step": "validate", "result": validation_dict})
        print(f"  - 길이 검증: {'OK' if validation.duration_ok else 'FAIL'}")
        print(f"  - 오디오 피크 검증: {'OK' if validation.audio_peak_ok else 'FAIL'}")
        print(f"  - 검은 프레임 검증: {'OK' if validation.black_frames_ok else 'FAIL'}")
        print("[OK] 검증 완료")
    else:
        print("\n[15/15] 검증 단계 스킵")

    # ═══════════════════════════════════════
    # 추가 쇼츠 렌더링 (2번째, 3번째 스토리라인)
    # ═══════════════════════════════════════
    all_output_videos: list[Path] = [output_video]

    if len(all_storyline_variants) > 1 and start_idx <= step_idx["render"]:
        print(f"\n추가 쇼츠 렌더링 ({len(all_storyline_variants) - 1}개)...")

        # variant 쇼츠도 쇼츠 #1과 동일하게 라인 단위 SRT 기반 자막을 사용하도록 미리 파싱
        srt_segments_for_variants: list[SpeechSegment] = []
        if payload.srt_path and payload.srt_path.exists():
            try:
                srt_segments_for_variants = parse_subtitle(payload.srt_path)
            except Exception as _exc:
                print(f"  [경고] variant용 SRT 재파싱 실패: {_exc}")

        for var_idx in range(1, len(all_storyline_variants)):
            var_clips, var_title, var_score = all_storyline_variants[var_idx]
            var_num = var_idx + 1
            var_video = output_dir / f"shorts_{var_num}.mp4"
            print(f"\n  ── 쇼츠 #{var_num} (점수: {var_score:.2f}) ──")
            print(f"  제목: {var_title}")

            # exists-스킵은 변이 렌더 도중 죽은 실행을 재개할 때 완성분을 다시 그리지 않기 위한 것.
            # ⚠️ --from-step render 는 **명시적 재렌더 요청**이라 스킵하면 안 된다 — 기존 변이가
            # 옛 디자인 플래그로 만든 것일 수 있다(2026-08-07 실측: 채널 템플릿 재렌더에서
            # shorts_1 만 새로 그려지고 shorts_2 는 옛 디자인으로 남아 검수함에 그대로 올라감).
            if var_video.exists() and from_step != "render":
                print(f"  → 이미 존재: {var_video.name}")
                all_output_videos.append(var_video)
                continue

            try:
                var_start = time.time()

                # 전사 — 라운드 14 우선순위:
                # 1. SRT (라인 단위) — remap이 clip 범위로 잘라냄
                # 2. 통합 transcript_text (Whisper 전사 — 모든 variant union 영역 포함, 라운드 14)
                # 3. candidate.transcript (Gemini 추정) — 위 둘 다 비어있을 때 폴백
                var_transcript: list = []
                if srt_segments_for_variants:
                    var_transcript = list(srt_segments_for_variants)
                else:
                    # 라운드 14-B: 통합 transcript_text에서 var_clips 영역과 겹치는 segment 추출
                    # 자막 텍스트·시간 모두 Whisper 전사 결과 → variant 2/3도 음성과 정확히 일치.
                    if transcript_text:
                        for seg in transcript_text:
                            seg_start = getattr(seg, "start_sec", None)
                            seg_end = getattr(seg, "end_sec", None)
                            seg_text = getattr(seg, "text", None)
                            if seg_start is None or seg_end is None or not seg_text:
                                continue
                            for clip in var_clips:
                                if seg_end > clip.start_sec and seg_start < clip.end_sec:
                                    var_transcript.append(SpeechSegment(
                                        start_sec=float(seg_start), end_sec=float(seg_end),
                                        text=str(seg_text),
                                    ))
                                    break

                    # 폴백: transcript_text에 var_clips 영역 segment가 없으면 candidate.transcript 사용
                    if not var_transcript and all_candidates:
                        for clip in var_clips:
                            for m in all_candidates:
                                m_start = float(m.get("start_sec", 0))
                                m_end = float(m.get("end_sec", 0))
                                if m_end > clip.start_sec and m_start < clip.end_sec and m.get("transcript"):
                                    # 실제 candidate 구간 시간을 보존 (clip 전체 덮어쓰기 금지)
                                    var_transcript.append(SpeechSegment(
                                        start_sec=m_start, end_sec=m_end, text=m["transcript"],
                                    ))
                                    break

                # 자막 타임라인 매핑
                var_remapped = remap_transcript_to_edited_timeline(var_clips, var_transcript, tts_only_when_no_orig=True)
                var_merged = merge_subtitle_segments(
                    var_remapped, max_gap_sec=0.25,
                    max_total_chars=int(config.subtitle_max_chars_per_line * config.subtitle_max_lines),
                )
                var_final_segs = [
                    SimpleNamespace(
                        start_sec=s.get("start", s.get("start_sec")) if isinstance(s, dict) else s.start_sec,
                        end_sec=s.get("end", s.get("end_sec")) if isinstance(s, dict) else s.end_sec,
                        text=s.get("text", "") if isinstance(s, dict) else s.text,
                    ) for s in var_merged
                ]

                # TTS 생성 (variant별 cue 사용) — fit 적용으로 cue 시간 안에 들어가게 합성
                _g_v = locals().get("gemini") or load_gemini_client()
                _shorten_v = getattr(_g_v, "shorten_text", None) if _g_v else None
                var_cues = tts_cues_per_variant[var_idx] if var_idx < len(tts_cues_per_variant) else []
                var_tts_cue_files: list[dict[str, Any]] = []
                for ci, cue in enumerate(var_cues):
                    tts_out = output_dir / f"tts_{var_num}_cue_{ci}.mp3"
                    # E7-2: 배속이 바뀌면 variant mp3 도 fit 이 낡는다 — 존재해도 재합성
                    if not tts_out.exists() or _resources_speed_stale:
                        # E7-2: 정본과 동일 — fit 목표는 배속 후 출력 창 (e-s)/S
                        target_sec = max(0.5, (float(cue.get("end_sec", 0.0))
                                               - float(cue.get("start_sec", 0.0))) / _speed)
                        final_text, actual_sec = synthesize_cue_cached(
                            cue["text"], tts_out, target_sec=target_sec,
                            voice=cue.get("voice", "narrative_female"),
                            speed=cue.get("speed", "normal"),
                            shorten_fn=_shorten_v,
                            cache_dir=output_dir / "tts_cache",
                        )
                        cue["text"] = final_text
                        cue["fit_actual_sec"] = actual_sec
                    var_tts_cue_files.append({
                        "cue_index": ci,
                        "path": str(tts_out),
                        "cue": cue,
                    })

                # TTS 자막 — 라운드 12.2: end_sec을 mp3 실제 길이로 갱신 (자막↔오디오 동기화)
                var_tts_segs: list[SimpleNamespace] = []
                for _cf in var_tts_cue_files:
                    _cue = _cf.get("cue", {})
                    # E7-2: 정본과 동일 — 시작 ×1/S, 끝 = 시작 + mp3 실시간 길이
                    _cue_start = float(_cue.get("start_sec", 0.0)) / _speed
                    _cue_end_planned = float(_cue.get("end_sec", 0.0)) / _speed
                    _mp3_path = _cf.get("path")
                    _cue_end = _cue_end_planned
                    if _mp3_path and Path(_mp3_path).exists():
                        _mp3_dur = _get_audio_duration(Path(_mp3_path))
                        if _mp3_dur > 0:
                            _cue_end = _cue_start + _mp3_dur
                    var_tts_segs.append(SimpleNamespace(
                        start_sec=_cue_start,
                        end_sec=_cue_end,
                        text=str(_cue.get("text", "")),
                    ))

                # 자막 ASS 파일 생성
                var_sub_path = output_dir / f"subtitles_{var_num}.ass"
                var_tts_sub_path = output_dir / f"tts_subtitles_{var_num}.ass"

                # 라운드 10: variant도 같은 자막 프리셋 적용 (영상 1개당 동일 스타일)
                _var_margin_v = _compute_subtitle_margin_v(
                    payload.design,
                    canvas_width=config.canvas_width,
                    canvas_height=config.canvas_height,
                    padding_px=10,
                )
                _var_user_overrides = {
                    "font_name": payload.design.subtitle_font if payload.design.subtitle_font != _default_design.subtitle_font else None,
                    "font_size": payload.design.subtitle_size if payload.design.subtitle_size != _default_design.subtitle_size else None,
                    "primary_color": payload.design.subtitle_color,  # None = 프리셋 색 유지
                    "margin_v": _var_margin_v,
                }
                sub_style, _ = select_subtitle_style(_genre_tag, _cli_override, _var_user_overrides)
                # E17-2: variant 도 같은 띠를 피한다 — 원본 자막은 소재의 성질이라
                # 어느 variant 를 뽑아도 같은 자리에 있다(재검출 없이 정본 값을 쓴다).
                _var_margin_v2 = _avoid_burned_margin_v(
                    _var_margin_v, _burned_band, design=payload.design, config=config,
                    title_text=var_title, font_size=sub_style.font_size, label="[대사] ")
                if _var_margin_v2 != _var_margin_v:
                    sub_style = replace(sub_style, margin_v=_var_margin_v2)
                var_tts_ranges = [(s.start_sec, s.end_sec) for s in var_tts_segs] if var_tts_segs else None
                # E7-2: 대사 자막도 출력 시각(×1/S) 사본으로 ASS 생성 (정본과 동일)
                var_final_segs = _avoid_burned_windowed(
                    var_final_segs, _var_margin_v, design=payload.design, config=config,
                    title_text=title_text, font_size=sub_style.font_size, label="[대사] ")
                build_ass_from_segments(_scale_segments_for_speed(var_final_segs, _speed),
                                        var_sub_path, sub_style, tts_time_ranges=var_tts_ranges)

                # E17-2: variant 의 TTS 자막도 같은 띠를 피한다(대사 자막과 같은 규약).
                _var_tts_margin_v = _compute_tts_margin_v(
                    payload.design, canvas_width=config.canvas_width,
                    canvas_height=config.canvas_height)
                _var_tts_margin_v = _avoid_burned_margin_v(
                    _var_tts_margin_v, _burned_band, design=payload.design, config=config,
                    title_text=var_title, font_size=payload.design.tts_line_font_size,
                    label="[TTS] ")
                var_tts_line_style = SubtitleStyle(
                    font_name=payload.design.subtitle_font,
                    font_size=payload.design.tts_line_font_size,
                    primary_color=payload.design.tts_line_color,
                    # E10 후속 3: variant 도 정본과 같은 밴드 앵커 (영상 1개당 동일 위치)
                    margin_v=_var_tts_margin_v,
                ) if var_tts_segs else None

                var_tts_sub_final = None
                if var_tts_segs and var_tts_line_style:
                    var_tts_segs = _avoid_burned_windowed(
                        var_tts_segs, _var_tts_margin_v, design=payload.design, config=config,
                        title_text=title_text, font_size=var_tts_line_style.font_size,
                        label="[TTS] ")
                    build_tts_ass(var_tts_segs, var_tts_sub_path, var_tts_line_style,
                                  rotate_deg=getattr(payload.design, "tts_rotate", 0.0))
                    var_tts_sub_final = var_tts_sub_path

                # 얼굴 크롭 타임라인 (--no-reframe 면 정본과 같이 생략 — 빈 map = 가운데 정렬)
                var_crop_map = {}
                for cidx, cclip in enumerate(var_clips) if getattr(payload.design, "enable_reframe", True) else []:
                    crop_file = output_dir / f"crop_{var_num}_{cclip.role}_{cidx}.json"
                    var_target_char = cclip.character_focus[0] if cclip.character_focus and face_identifier else None
                    build_crop_timeline(
                        payload.video_path.resolve(),
                        crop_file,
                        media_info.width,
                        media_info.height,
                        config.crop_sample_interval_sec,
                        start_sec=cclip.start_sec,
                        end_sec=cclip.end_sec,
                        enable_speaker_tracking=payload.design.enable_speaker_tracking,
                        target_character=var_target_char,
                        face_identifier=face_identifier,
                        character_index=character_appearances,
                    )
                    var_crop_map[f"{cclip.role}_{cidx}"] = crop_file

                # 렌더링
                actual_font_path = get_font_path(payload.design.title_font, paths.app_root)
                actual_subtitle_font_path = get_font_path(payload.design.subtitle_font, paths.app_root)
                updated_design = replace(
                    payload.design,
                    title_font=actual_font_path,
                    subtitle_font=actual_subtitle_font_path,
                )

                var_render_inputs = RenderInputs(
                    video_path=payload.video_path,
                    clips=var_clips,
                    subtitle_path=var_sub_path if (payload.show_subtitles and var_sub_path.exists()) else None,
                    crop_timeline_map=var_crop_map,
                    title_text=var_title if payload.show_title_overlay else "",
                    work_title=payload.work_title if payload.show_title_overlay else "",
                    design=updated_design,
                    output_path=var_video,
                    loudness_target_lufs=payload.loudness_target_lufs,
                    canvas_width=config.canvas_width,
                    canvas_height=config.canvas_height,
                    top_title_height=config.top_title_height,
                    bottom_label_height=config.bottom_label_height,
                    tts_subtitle_path=var_tts_sub_final if payload.show_tts_subtitles else None,
                    tts_cue_files=(var_tts_cue_files
                                   if (var_tts_cue_files and payload.include_tts_audio)
                                   else None),
                    original_audio_gain_db=config.original_gain_db,
                    tts_audio_gain_db=config.tts_gain_db,
                    render_preset=config.render_preset,
                    enable_hwaccel=config.enable_hwaccel,
                )

                render_short(var_render_inputs)
                var_elapsed = time.time() - var_start
                all_output_videos.append(var_video)

                # 변이별 편집 계획 — shorts.mp4 만 edit_plan.json 이 있고 #2·#3 은 없었다.
                # 하류가 전부 edit_plan 을 읽는다: scene_loop 은 장면 구간을, ingest 는 클립
                # 메타를 여기서 가져온다. 없으면 렌더 비용은 3배로 쓰고 **검수·발행에 올라가는
                # 것은 1편뿐**이다(2026-08-06 확인). 같은 스키마로 써 두면 ingest 는
                # `--edit-plan edit_plan_<n>.json` 으로 그대로 받는다.
                var_plan_path = output_dir / f"edit_plan_{var_num}.json"
                var_plan_path.write_text(
                    json.dumps(_build_edit_plan(payload, var_title, var_clips, var_crop_map, config),
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  [OK] 쇼츠 #{var_num} 렌더링 완료 ({var_elapsed:.1f}초) · 편집 계획 {var_plan_path.name}")

            except Exception as e:
                print(f"  [ERROR] 쇼츠 #{var_num} 렌더링 실패: {e}")
                continue

    # 최종 로그 저장
    def _make_json_serializable(obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        elif isinstance(obj, dict):
            return {k: _make_json_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_make_json_serializable(item) for item in obj]
        elif isinstance(obj, tuple):
            return tuple(_make_json_serializable(item) for item in obj)
        else:
            return obj

    # run_log 다이어트: gemini step의 거대한 response(characters_tracking 포함 ~수십KB/청크)는
    # 같은 디렉토리의 checkpoint_gemini.json에 이미 보존되므로 run_log에는 요약만 남긴다.
    # render step의 ffmpeg argv도 검증·디버깅용 → 길이만 기록. 효과: 232KB → 5KB 수준.
    def _slim_run_log(rl: dict) -> dict:
        slim = {k: v for k, v in rl.items() if k != "steps"}
        slim_steps: list[dict] = []
        for step in rl.get("steps", []):
            s = dict(step)
            if s.get("step") == "gemini" and isinstance(s.get("response"), dict):
                resp = s["response"]
                s["response"] = {
                    "chunk_index": resp.get("chunk_index"),
                    "summary_chars": len(resp.get("summary", "")) if isinstance(resp.get("summary"), str) else 0,
                    "candidate_count": len(resp.get("candidate_moments", []) or []),
                    "segment_count": len(resp.get("segments", []) or []),
                    "characters_tracking_count": len(resp.get("characters_tracking", []) or []),
                }
            elif s.get("step") == "render" and isinstance(s.get("command"), list):
                s["command"] = {
                    "argv_count": len(s["command"]),
                    "first": s["command"][:6],  # 디버깅용 첫 인자 일부만
                }
            slim_steps.append(s)
        slim["steps"] = slim_steps
        return slim

    # 구간별 보정(E18-6)은 대사·TTS·variant ASS 를 조립하면서 일어난다 — 위 단계 기록
    # 시점보다 뒤다. 같은 dict 를 들고 있다가 여기서 채운다(별도 단계를 만들면 사람이
    # 두 곳을 맞춰 봐야 한다).
    if _avoid_log is not None and _BURNED_WINDOW_MOVES:
        _avoid_log["windowed"] = list(_BURNED_WINDOW_MOVES)

    run_log_serializable = _make_json_serializable(_slim_run_log(run_log))
    run_log_path = output_dir / "run_log.json"
    run_log_path.write_text(json.dumps(run_log_serializable, ensure_ascii=False, indent=2), encoding="utf-8")

    total_elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("파이프라인 완료")
    print("=" * 60)
    print(f"총 소요 시간: {total_elapsed:.1f}초 ({total_elapsed / 60:.1f}분)")
    print(f"\n출력 파일 ({len(all_output_videos)}개 쇼츠):")
    for idx, vp in enumerate(all_output_videos, 1):
        print(f"  - 쇼츠 #{idx}: {vp}")
    print(f"  - 편집 계획: {edit_plan_path}")
    print(f"  - 실행 로그: {run_log_path}")
    print("=" * 60)

    return PipelineOutput(
        output_videos=all_output_videos,
        edit_plan_path=edit_plan_path,
        run_log_path=run_log_path,
    )


# ─────────────────────────────────────────────
# 유틸리티 함수
# ─────────────────────────────────────────────

def _snap_to_scenes(clips: list[StoryClip], scenes, threshold: float) -> list[StoryClip]:
    boundaries = sorted({scene.start_sec for scene in scenes} | {scene.end_sec for scene in scenes})
    if not boundaries:
        return clips
    snapped = []
    for clip in clips:
        start = _snap_time(clip.start_sec, boundaries, threshold)
        end = _snap_time(clip.end_sec, boundaries, threshold)
        if end - start <= 0.2:
            start, end = clip.start_sec, clip.end_sec
        snapped.append(
            StoryClip(
                role=clip.role,
                start_sec=start,
                end_sec=end,
                subtitle=clip.subtitle,
                use_original_audio=clip.use_original_audio,
            )
        )
    return snapped


def _snap_time(value: float, boundaries: list[float], threshold: float) -> float:
    closest = min(boundaries, key=lambda b: abs(b - value))
    if abs(closest - value) <= threshold:
        return closest
    return value


def _build_edit_plan(
    payload: PipelineInput,
    title_text: str,
    clips: list[StoryClip],
    crop_map: dict[str, Path],
    config: AppConfig,
    title_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timeline = []
    for idx, clip in enumerate(clips):
        timeline.append(
            {
                "role": clip.role,
                "clip_start_sec": clip.start_sec,
                "clip_end_sec": clip.end_sec,
                "subtitle": clip.subtitle,
                "use_original_audio": clip.use_original_audio,
                # --no-reframe 면 crop_map 이 비어 있다 — 예전엔 여기서 KeyError 로 죽었다.
                "reframe": (
                    {"mode": "face_track",
                     "crop_timeline_ref": crop_map[f"{clip.role}_{idx}"].name}
                    if f"{clip.role}_{idx}" in crop_map else {"mode": "center"}
                ),
            }
        )
    layout: dict[str, Any] = {
        "canvas": f"{config.canvas_width}x{config.canvas_height}",
        "top_title": title_text,
        "bottom_label": payload.work_title,
        "background_style": "blur",
        # E7-2: timeline 합계는 소스 시간이고 출력 mp4 는 합계/S 다 — 이 값을 빼먹으면
        # edit_plan 으로 길이를 검산하는 하류(ingest·관제)가 영상과 다른 답을 얻는다.
        "video_speed": float(getattr(payload.design, "video_speed", 1.0) or 1.0),
    }
    if title_segments:
        # E8: 적용된 시간대별 제목 기록(편집본 시간축) — 하류 검산·검수 노출용.
        # 없을 때는 키 자체를 빼 구 edit_plan 소비자와의 diff 를 만들지 않는다.
        layout["title_segments"] = title_segments
    return {
        "input": {
            "video_path": str(payload.video_path),
            "work_title": payload.work_title,
            "topic": payload.topic,
            "language": payload.language,
        },
        "layout": layout,
        "timeline": timeline,
        "audio_mix": {
            "tts_gain_db": config.tts_gain_db,
            "original_gain_db": config.original_gain_db,
            "bgm_gain_db": config.bgm_gain_db,
        },
    }
