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
    max_gap_sec: float = 0.8,   # 이 이상 침묵이면 컷 (드라마는 넉넉하게)
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
                tts_line=clip.tts_line,
                use_original_audio=clip.use_original_audio,
            ))
        else:
            # 여러 구간으로 분리된 경우
            for idx, interval in enumerate(result.keep_intervals):
                new_clips.append(StoryClip(
                    role=clip.role,
                    start_sec=interval.start_sec,
                    end_sec=interval.end_sec,
                    # 첫 번째 구간만 subtitle/tts 유지, 나머지는 비움
                    subtitle=clip.subtitle if idx == 0 else "",
                    tts_line=clip.tts_line if idx == 0 else "",
                    use_original_audio=clip.use_original_audio,
                ))

    return new_clips


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