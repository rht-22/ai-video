from __future__ import annotations

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


# ─────────────────────────────────────────────────────────────
# reduce 단계: 드래프트(이어붙인) 타임라인 컷 → clip별 소스 keep_intervals
# ─────────────────────────────────────────────────────────────
def map_draft_cuts_to_results(
    clips: list[StoryClip],
    cut_segments: list[dict],
    *,
    min_keep_sec: float = 0.3,
) -> list[SilenceCutResult]:
    """드래프트(=clips를 순서대로 이어붙인) 타임라인 기준 제거 구간을 각 clip의 소스
    keep_intervals 로 환산한다.

    드래프트 타임라인: clip i 는 [off_i, off_i + dur_i) 를 차지 (off_i = 앞 clip들 길이 누적).
    제거 구간 [cs, ce] 가 clip 범위와 겹치면, 그 부분을 소스 시간으로 환산해
    (src = clip.start_sec + (draft_t - off_i)) clip 의 유지 구간에서 차감한다.

    Args:
        clips: variant clip 목록 (start_sec/end_sec = 소스 시간, 드래프트 순서와 동일).
        cut_segments: {"start_sec","end_sec",...} 드래프트 타임라인 제거 구간 (이미 선택된 것).
        min_keep_sec: 이보다 짧아진 유지 조각은 버린다 (자투리 제거).

    Returns: clip 순서대로 SilenceCutResult 목록 (flatten_to_clips 로 펼칠 수 있음).
    """
    # 1) clip별 드래프트 오프셋 계산
    offsets: list[float] = []
    acc = 0.0
    for c in clips:
        offsets.append(acc)
        acc += float(c.end_sec - c.start_sec)

    # 2) 각 clip 의 소스 cut 구간 수집
    src_cuts_per_clip: list[list[tuple[float, float]]] = [[] for _ in clips]
    for seg in cut_segments or []:
        try:
            cs = float(seg.get("start_sec"))
            ce = float(seg.get("end_sec"))
        except (TypeError, ValueError, AttributeError):
            continue
        if ce <= cs:
            continue
        for i, clip in enumerate(clips):
            off = offsets[i]
            dur = float(clip.end_sec - clip.start_sec)
            ov_s = max(cs, off)
            ov_e = min(ce, off + dur)
            if ov_e <= ov_s:
                continue  # 이 clip 과 겹치지 않음
            src_s = clip.start_sec + (ov_s - off)
            src_e = clip.start_sec + (ov_e - off)
            src_cuts_per_clip[i].append((src_s, src_e))

    # 3) clip별로 cut 을 빼고 keep_intervals 생성
    results: list[SilenceCutResult] = []
    for i, clip in enumerate(clips):
        keeps: list[Interval] = [Interval(clip.start_sec, clip.end_sec)]
        for (a, b) in sorted(src_cuts_per_clip[i]):
            new_keeps: list[Interval] = []
            for kv in keeps:
                if b <= kv.start_sec or a >= kv.end_sec:
                    new_keeps.append(kv)  # 겹침 없음
                    continue
                if a > kv.start_sec:
                    new_keeps.append(Interval(kv.start_sec, a))  # 왼쪽 조각
                if b < kv.end_sec:
                    new_keeps.append(Interval(b, kv.end_sec))   # 오른쪽 조각
            keeps = new_keeps
        # 자투리 제거
        keeps = [kv for kv in keeps if (kv.end_sec - kv.start_sec) >= min_keep_sec]
        clip_dur = float(clip.end_sec - clip.start_sec)
        kept_dur = sum(kv.end_sec - kv.start_sec for kv in keeps)
        results.append(SilenceCutResult(
            original_clip=clip,
            keep_intervals=keeps,
            total_removed_sec=max(0.0, clip_dur - kept_dur),
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