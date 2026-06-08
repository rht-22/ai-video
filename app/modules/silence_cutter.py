from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from app.modules.speech import SpeechSegment
from app.modules.story_builder import StoryClip


class Interval(NamedTuple):
    """원본 영상 기준 유지할 구간"""
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class SilenceCutResult:
    """침묵 컷 결과"""
    original_clip: StoryClip          # 원본 클립
    keep_intervals: list[Interval]    # 유지할 구간 목록 (원본 타임라인 기준)
    total_removed_sec: float          # 제거된 침묵 총 시간


def cut_silence_from_clips(
    clips: list[StoryClip],
    transcript_segments: list[SpeechSegment],
    *,
    max_gap_sec: float = 0.4,   # 이 이상 침묵이면 컷 — 0.8 → 0.4 (영상 템포 ↑, 라운드 3b)
    padding_sec: float = 0.15,  # 대사 앞뒤 여유 (너무 빡빡하게 자르면 어색함)
    min_interval_sec: float = 0.3,  # 너무 짧은 구간은 합치기
) -> list[SilenceCutResult]:
    """
    Gemini가 선택한 클립들에서 침묵 구간을 제거합니다.

    원리:
      1. 각 클립 구간 안에 있는 Whisper 세그먼트를 찾음
      2. 세그먼트 사이 gap이 max_gap_sec 이상이면 컷
      3. 유지할 구간 목록을 반환 (렌더링 단계에서 활용)

    Args:
        clips: Gemini가 선택한 스토리 클립 목록
        transcript_segments: Whisper 전사 세그먼트 목록
        max_gap_sec: 이 이상 침묵이면 컷 (기본 0.8초)
        padding_sec: 대사 앞뒤 여유 시간 (기본 0.15초)
        min_interval_sec: 이보다 짧은 구간은 앞 구간에 합치기 (기본 0.3초)

    Returns:
        각 클립별 SilenceCutResult 목록
    """
    # 전사 세그먼트 시간순 정렬
    sorted_segments = sorted(transcript_segments, key=lambda s: s.start_sec)
    results = []

    for clip in clips:
        # visual_essential=True면 시각 비트 보호 — 무음 구간도 그대로 유지.
        # (예: 펜 각인·표정·소품 클로즈업 등 대사 없는 핵심 비트가 잘려나가는 사례 방지)
        if getattr(clip, "visual_essential", False):
            results.append(SilenceCutResult(
                original_clip=clip,
                keep_intervals=[Interval(clip.start_sec, clip.end_sec)],
                total_removed_sec=0.0,
            ))
            continue

        # 이 클립 구간과 겹치는 세그먼트 추출
        clip_segments = [
            seg for seg in sorted_segments
            if seg.end_sec > clip.start_sec and seg.start_sec < clip.end_sec
        ]

        # 세그먼트가 없으면 (무음 클립이거나 전사 실패) 전체 구간 유지
        if not clip_segments:
            results.append(SilenceCutResult(
                original_clip=clip,
                keep_intervals=[Interval(clip.start_sec, clip.end_sec)],
                total_removed_sec=0.0,
            ))
            continue

        # 각 세그먼트에 padding 추가 후 클립 범위로 클리핑
        padded = []
        for seg in clip_segments:
            start = max(clip.start_sec, seg.start_sec - padding_sec)
            end = min(clip.end_sec, seg.end_sec + padding_sec)
            padded.append(Interval(start, end))

        # 겹치는 구간 병합
        merged: list[Interval] = []
        for interval in sorted(padded):
            if merged and interval.start_sec <= merged[-1].end_sec:
                # 겹치면 합치기
                merged[-1] = Interval(merged[-1].start_sec, max(merged[-1].end_sec, interval.end_sec))
            else:
                merged.append(interval)

        # 너무 짧은 구간 앞 구간에 흡수
        filtered: list[Interval] = []
        for interval in merged:
            duration = interval.end_sec - interval.start_sec
            if filtered and duration < min_interval_sec:
                # 앞 구간에 붙이기
                filtered[-1] = Interval(filtered[-1].start_sec, interval.end_sec)
            else:
                filtered.append(interval)

        # 제거된 침묵 시간 계산
        clip_duration = clip.end_sec - clip.start_sec
        kept_duration = sum(i.end_sec - i.start_sec for i in filtered)
        removed_sec = max(0.0, clip_duration - kept_duration)

        results.append(SilenceCutResult(
            original_clip=clip,
            keep_intervals=filtered,
            total_removed_sec=removed_sec,
        ))

    return results


def flatten_to_clips(cut_results: list[SilenceCutResult]) -> list[StoryClip]:
    """
    SilenceCutResult를 다시 StoryClip 목록으로 펼칩니다.

    침묵이 제거된 구간들을 각각 별도 StoryClip으로 만들어
    기존 파이프라인(렌더링 등)이 그대로 사용할 수 있게 합니다.

    예시:
      원본 클립: 10초 ~ 40초
      침묵 컷 후: [10~18초, 22~35초, 37~40초]
      → StoryClip 3개로 분리
    """
    new_clips: list[StoryClip] = []

    for result in cut_results:
        clip = result.original_clip

        if len(result.keep_intervals) == 1:
            # 침묵 컷이 없거나 1구간이면 그대로 유지
            interval = result.keep_intervals[0]
            new_clips.append(StoryClip(
                role=clip.role,
                start_sec=interval.start_sec,
                end_sec=interval.end_sec,
                subtitle=clip.subtitle,
                use_original_audio=clip.use_original_audio,
                pacing_note=clip.pacing_note,
                chunk_index=clip.chunk_index,
                candidate_index=clip.candidate_index,
                character_focus=clip.character_focus,
                visual_essential=clip.visual_essential,
                tts_draft=clip.tts_draft,
            ))
        else:
            # 여러 구간으로 분리된 경우
            for idx, interval in enumerate(result.keep_intervals):
                new_clips.append(StoryClip(
                    role=clip.role,
                    start_sec=interval.start_sec,
                    end_sec=interval.end_sec,
                    # 첫 번째 구간만 subtitle 유지, 나머지는 비움
                    subtitle=clip.subtitle if idx == 0 else "",
                    use_original_audio=clip.use_original_audio,
                    pacing_note=clip.pacing_note,
                    chunk_index=clip.chunk_index,
                    candidate_index=clip.candidate_index,
                    character_focus=clip.character_focus,
                    visual_essential=clip.visual_essential,
                    # 분할된 구간에서 첫 번째에만 tts_draft 유지 (cue 위치도 첫 구간 시점)
                    tts_draft=clip.tts_draft if idx == 0 else "",
                ))

    return new_clips


# ─────────────────────────────────────────────────────────────
# PR-5: 스토리-aware 무음 컷
# ─────────────────────────────────────────────────────────────
# 기존 cut_silence_from_clips 는 *모든* gap 을 일률적으로 컷한다 (visual_essential 만 제외).
# PR-5 는 candidate 메타 (description 액션 동사 / 화자 변경 / visual_essential) 를 추가
# 검사해 *컷해도 스토리상 안전한 gap* 만 컷한다. 컷 가능 여부는 _is_gap_safe_to_cut 로 판정.

# 액션 동사 / 비주얼 비트 키워드 — description 에 이게 들어 있으면 컷 보호
_ACTION_VERB_KEYWORDS: tuple[str, ...] = (
    "시선", "눈빛", "마주본", "마주친",
    "키스", "포옹", "안는다", "껴안",
    "표정", "굳", "찡그", "씁쓸",
    "충돌", "부딪",
    "주먹", "휘두", "때린", "뺨",
    "달려든", "달려가", "뛰어",
    "쓰러", "넘어",
    "흘리", "눈물",
    "잡아", "끌어",
)


def _has_action_verb(description: str | None) -> bool:
    """description 에 액션·비주얼 비트 키워드가 있는지."""
    if not description:
        return False
    for kw in _ACTION_VERB_KEYWORDS:
        if kw in description:
            return True
    return False


def _is_narration_seg(seg: SpeechSegment | None) -> bool:
    """segment text 가 [내레이션] 접두로 시작하는지."""
    if seg is None or not getattr(seg, "text", None):
        return False
    return seg.text.lstrip().startswith("[내레이션]")


def _is_gap_safe_to_cut(
    candidate: dict | None,
    before_seg: SpeechSegment | None,
    after_seg: SpeechSegment | None,
) -> bool:
    """gap 양쪽 segment + candidate 메타로 컷 가능 여부 판정.

    True 면 무음 컷 가능, False 면 보호.

    규칙 (모두 통과해야 True):
    - candidate 메타 있음 (없으면 보수적으로 False)
    - candidate.visual_essential == False
    - candidate.description 에 액션 동사 없음
    - 화자 변경 없음 ([내레이션] 접두가 양쪽 모두 있거나 모두 없음)
    - 양쪽 segment 둘 다 있거나 둘 다 None (한쪽만 None 이면 보수적 False)
    """
    if candidate is None:
        return False
    if candidate.get("visual_essential") is True:
        return False
    if _has_action_verb(candidate.get("description") or ""):
        return False
    # 양쪽 segment 존재 패턴 검사
    if before_seg is None and after_seg is None:
        # clip 안 transcript 가 없음 — visual_essential=False 이고 액션 없으면 안전 (긴 무음 컷 가능)
        return True
    if before_seg is None or after_seg is None:
        # 한쪽 None 이면 clip 경계 인접 — 보수적으로 보호
        return False
    # 양쪽 segment 모두 있음: [내레이션] 접두 패턴이 일치해야 (둘 다 있거나 둘 다 없음)
    if _is_narration_seg(before_seg) != _is_narration_seg(after_seg):
        return False
    return True


def cut_silence_with_story_filter(
    clips: list[StoryClip],
    transcript_segments: list[SpeechSegment],
    candidates_lookup: dict[tuple[int, int], dict],
    *,
    max_gap_sec: float = 0.4,
    padding_sec: float = 0.15,
    min_interval_sec: float = 0.3,
) -> list[SilenceCutResult]:
    """스토리-aware 무음 컷.

    cut_silence_from_clips 와 동일한 keep_intervals 출력을 만들되, 각 clip 의 candidate
    메타 (visual_essential / description / 화자 변경) 로 *gap 컷 가능 여부* 를 추가 판별.
    하나라도 보호 조건이면 그 clip 의 모든 gap 보호 (clip 통째 유지).

    더 fine-grained 한 gap-단위 판별이 필요해지면 후속 PR 에서 확장.
    """
    sorted_segments = sorted(transcript_segments, key=lambda s: s.start_sec)
    results: list[SilenceCutResult] = []

    for clip in clips:
        # 기존 cut_silence_from_clips 의 visual_essential 보호 동일 — clip 통째 유지
        if getattr(clip, "visual_essential", False):
            results.append(SilenceCutResult(
                original_clip=clip,
                keep_intervals=[Interval(clip.start_sec, clip.end_sec)],
                total_removed_sec=0.0,
            ))
            continue

        # 이 clip 의 candidate 메타 조회 — chunk_index=0 falsy 함정 회피 (or -1 패턴 금지)
        _ci = getattr(clip, "chunk_index", None)
        _cd = getattr(clip, "candidate_index", None)
        key = (int(_ci) if _ci is not None else -1,
               int(_cd) if _cd is not None else -1)
        cand = candidates_lookup.get(key)

        # clip 안의 segments
        clip_segments = [
            seg for seg in sorted_segments
            if seg.end_sec > clip.start_sec and seg.start_sec < clip.end_sec
        ]

        # gap 안전성 판정 — clip 안 segment 사이 gap 만 검사.
        # clip 시작/끝 인접 dead air (segment 와 clip 경계 사이) 는 항상 안전 처리 (cut_silence_from_clips
        # 가 padding 처리하는 기존 동작 보존). _is_gap_safe_to_cut 은 양쪽 segment 가 모두 있는 *내부 gap*
        # 에 대한 화자 변경 검사용.
        if clip_segments:
            edges: list[tuple[SpeechSegment | None, SpeechSegment | None]] = []
            for i in range(len(clip_segments) - 1):
                edges.append((clip_segments[i], clip_segments[i + 1]))
        else:
            # clip 안 segment 가 없음 — visual_essential/액션 없으면 전체가 dead air 라 안전
            edges = [(None, None)]

        any_unsafe = False
        for before, after in edges:
            # gap 크기 (segment 없으면 clip 경계와 거리)
            gap_start = before.end_sec if before is not None else clip.start_sec
            gap_end = after.start_sec if after is not None else clip.end_sec
            if gap_end - gap_start < max_gap_sec:
                continue  # gap 작으면 컷 대상 아님 → 안전 판정 무관
            if not _is_gap_safe_to_cut(cand, before, after):
                any_unsafe = True
                break

        if any_unsafe:
            results.append(SilenceCutResult(
                original_clip=clip,
                keep_intervals=[Interval(clip.start_sec, clip.end_sec)],
                total_removed_sec=0.0,
            ))
            continue

        # 안전 — 기존 cut_silence_from_clips 와 동일한 keep_intervals 빌딩
        if not clip_segments:
            results.append(SilenceCutResult(
                original_clip=clip,
                keep_intervals=[Interval(clip.start_sec, clip.end_sec)],
                total_removed_sec=0.0,
            ))
            continue

        padded: list[Interval] = []
        for seg in clip_segments:
            s = max(clip.start_sec, seg.start_sec - padding_sec)
            e = min(clip.end_sec, seg.end_sec + padding_sec)
            padded.append(Interval(s, e))

        merged: list[Interval] = []
        for interval in sorted(padded):
            if merged and interval.start_sec <= merged[-1].end_sec:
                merged[-1] = Interval(merged[-1].start_sec, max(merged[-1].end_sec, interval.end_sec))
            else:
                merged.append(interval)

        filtered: list[Interval] = []
        for interval in merged:
            duration = interval.end_sec - interval.start_sec
            if filtered and duration < min_interval_sec:
                filtered[-1] = Interval(filtered[-1].start_sec, interval.end_sec)
            else:
                filtered.append(interval)

        clip_duration = clip.end_sec - clip.start_sec
        kept_duration = sum(i.end_sec - i.start_sec for i in filtered)
        removed_sec = max(0.0, clip_duration - kept_duration)

        results.append(SilenceCutResult(
            original_clip=clip,
            keep_intervals=filtered,
            total_removed_sec=removed_sec,
        ))

    return results


def print_silence_cut_summary(cut_results: list[SilenceCutResult]) -> None:
    """침묵 컷 결과 요약 출력"""
    total_removed = sum(r.total_removed_sec for r in cut_results)
    total_cuts = sum(max(0, len(r.keep_intervals) - 1) for r in cut_results)

    print(f"  - 총 침묵 제거: {total_removed:.1f}초")
    print(f"  - 총 컷 횟수: {total_cuts}회")

    for i, result in enumerate(cut_results):
        clip = result.original_clip
        clip_dur = clip.end_sec - clip.start_sec
        cuts = max(0, len(result.keep_intervals) - 1)
        print(
            f"    클립 {i+1} ({clip.role}): "
            f"{clip_dur:.1f}초 → {clip_dur - result.total_removed_sec:.1f}초 "
            f"({cuts}회 컷, {result.total_removed_sec:.1f}초 제거)"
        )


# ─────────────────────────────────────────────────────────────
# PR-6: 가편집 영상 기반 dead-air 컷 (av_align 단계)
# ─────────────────────────────────────────────────────────────
# av_align 단계에서 Gemini 가 가편집 영상을 보고 산출한 dead_air_cuts(가편집 타임라인 좌표)
# 를 원본 clip 좌표로 역매핑해 clips 를 재분할하고, 같은 가편집 타임라인 위의 dialogue /
# tts_cues 시간도 컷만큼 보정한다. Whisper gap 기반 cut_silence_* 를 대체.
#
# 좌표계:
#   - clip.start_sec/end_sec        : 원본 영상 좌표
#   - dead_air_cuts/dialogue/tts_cues : 가편집 타임라인 좌표(0 = 쇼츠 시작, 클립을 순서대로 이어붙임)


def compute_clip_offsets(clips: list[StoryClip]) -> list[float]:
    """각 clip 의 가편집 타임라인 시작초(이전 clip 길이 누적합) 목록."""
    offsets: list[float] = []
    acc = 0.0
    for c in clips:
        offsets.append(acc)
        acc += max(0.0, float(c.end_sec) - float(c.start_sec))
    return offsets


def _merge_edit_cuts(dead_air_cuts: list[dict], min_cut_sec: float) -> list[tuple[float, float]]:
    """가편집 좌표 컷 구간 정규화: min_cut_sec 미만 무시 + 겹침 병합 + 정렬."""
    raw: list[tuple[float, float]] = []
    for d in dead_air_cuts or []:
        s = float(d.get("start_sec", 0.0) or 0.0)
        e = float(d.get("end_sec", 0.0) or 0.0)
        if e - s >= min_cut_sec:
            raw.append((s, e))
    raw.sort()
    merged: list[tuple[float, float]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def apply_dead_air_cuts(
    clips: list[StoryClip],
    dead_air_cuts: list[dict],
    *,
    protect_visual_essential: bool = True,
    min_cut_sec: float = 0.3,
) -> tuple[list[SilenceCutResult], Callable[[float], float]]:
    """가편집 타임라인 dead_air_cuts 를 원본 clip 좌표로 역매핑해 컷 적용.

    Returns:
        (results, remap)
        results — flatten_to_clips 로 펼칠 SilenceCutResult 목록 (clips 와 1:1).
        remap(edit_sec) — 컷 반영된 *새* 가편집 좌표. 컷 구간 내부 입력은 직전 유지 구간의
        끝(= 컷 시작에 해당하는 새 좌표)으로 스냅한다.
    """
    offsets = compute_clip_offsets(clips)
    cuts_edit = _merge_edit_cuts(dead_air_cuts, min_cut_sec)

    # clip별 원본좌표 제거 구간 모으기 (visual_essential 은 보호 → 제거 안 함)
    per_clip_removes: list[list[tuple[float, float]]] = [[] for _ in clips]
    for (cs, ce) in cuts_edit:
        for i, c in enumerate(clips):
            dur = max(0.0, float(c.end_sec) - float(c.start_sec))
            c_es, c_ee = offsets[i], offsets[i] + dur
            ov_s = max(cs, c_es)
            ov_e = min(ce, c_ee)
            if ov_e - ov_s <= 1e-9:
                continue
            if protect_visual_essential and getattr(c, "visual_essential", False):
                continue
            src_s = float(c.start_sec) + (ov_s - c_es)
            src_e = float(c.start_sec) + (ov_e - c_es)
            per_clip_removes[i].append((src_s, src_e))

    # clip별 keep_intervals 계산 + remap 용 segments 테이블 구성
    results: list[SilenceCutResult] = []
    # segments: (old_edit_start, old_edit_end, new_edit_start) — 유지 구간만, 시간순
    segments: list[tuple[float, float, float]] = []
    new_acc = 0.0
    for i, c in enumerate(clips):
        c_start, c_end = float(c.start_sec), float(c.end_sec)
        removes = sorted(per_clip_removes[i])
        keep: list[Interval] = []
        cursor = c_start
        for (rs, re) in removes:
            rs = max(rs, c_start)
            re = min(re, c_end)
            if rs > cursor:
                keep.append(Interval(cursor, rs))
            cursor = max(cursor, re)
        if cursor < c_end:
            keep.append(Interval(cursor, c_end))
        if not keep:
            # 과도 컷 가드 — clip 전체가 제거되면 원본 통째 유지
            keep = [Interval(c_start, c_end)]
        for iv in keep:
            old_es = offsets[i] + (iv.start_sec - c_start)
            old_ee = offsets[i] + (iv.end_sec - c_start)
            segments.append((old_es, old_ee, new_acc))
            new_acc += (iv.end_sec - iv.start_sec)
        kept = sum(iv.end_sec - iv.start_sec for iv in keep)
        removed = max(0.0, (c_end - c_start) - kept)
        results.append(SilenceCutResult(original_clip=c, keep_intervals=keep, total_removed_sec=removed))

    def remap(edit_sec: float) -> float:
        e = float(edit_sec)
        prev_new_end = 0.0
        for (oes, oee, nes) in segments:
            if e <= oee + 1e-9:
                if e >= oes - 1e-9:
                    return nes + (e - oes)
                # 컷 구간 내부 → 직전 유지 구간 끝으로 스냅
                return prev_new_end
            prev_new_end = nes + (oee - oes)
        return prev_new_end  # 마지막 유지 구간 이후

    return results, remap


def remap_timed_items(
    items: list[dict],
    remap: Callable[[float], float],
    *,
    min_keep_sec: float = 0.05,
) -> list[dict]:
    """dialogue / tts_cues 의 가편집 좌표(start_sec/end_sec)를 remap 으로 보정.

    컷으로 길이가 min_keep_sec 미만이 된 항목은 드롭(컷 구간에 완전히 포함됐던 것).
    그 외 필드는 보존한다.
    """
    out: list[dict] = []
    for it in items or []:
        ns = remap(float(it.get("start_sec", 0.0) or 0.0))
        ne = remap(float(it.get("end_sec", 0.0) or 0.0))
        if ne - ns < min_keep_sec:
            continue
        new_it = dict(it)
        new_it["start_sec"] = ns
        new_it["end_sec"] = ne
        out.append(new_it)
    return out