from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.modules.moment_ranker import RankedMoment


@dataclass(frozen=True)
class StoryClip:
    role: str
    start_sec: float
    end_sec: float
    subtitle: str
    tts_line: str
    use_original_audio: bool


def build_story(
    ranked: Iterable[RankedMoment],
    target_duration_sec: float,
    tolerance_sec: float,
    min_duration_sec: float | None = None,
    max_duration_sec: float | None = None,
) -> list[StoryClip]:
    hook_candidates = [m for m in ranked if m.story_role == "hook"]
    build_candidates = [m for m in ranked if m.story_role == "build"]
    payoff_candidates = [m for m in ranked if m.story_role == "payoff"]

    if not hook_candidates or not build_candidates or not payoff_candidates:
        raise ValueError("Not enough candidates for hook/build/payoff")

    # 초기 선택: hook 1개, build 여러 개, payoff 1개
    selected = [hook_candidates[0]]
    # build 클립을 더 많이 선택하여 최소 길이 보장
    selected.extend(build_candidates[:6])  # 4개에서 6개로 증가
    selected.append(payoff_candidates[0])

    clips = [
        StoryClip(
            role=m.story_role,
            start_sec=m.start_sec,
            end_sec=m.end_sec,
            subtitle=m.subtitle,
            tts_line=m.tts_line,
            use_original_audio=True,
        )
        for m in selected
    ]

    return _fit_duration(clips, target_duration_sec, tolerance_sec, min_duration_sec, max_duration_sec)


def _fit_duration(
    clips: list[StoryClip],
    target_duration_sec: float,
    tolerance_sec: float,
    min_duration_sec: float | None = None,
    max_duration_sec: float | None = None,
) -> list[StoryClip]:
    # 최소/최대 길이 설정
    min_dur = min_duration_sec if min_duration_sec is not None else target_duration_sec - tolerance_sec
    max_dur = max_duration_sec if max_duration_sec is not None else target_duration_sec + tolerance_sec
    
    duration = sum(c.end_sec - c.start_sec for c in clips)
    
    # 목표 범위 내에 있으면 반환
    if min_dur <= duration <= max_dur:
        return clips

    # 너무 긴 경우: 클립 제거
    if duration > max_dur:
        trimmed = [clips[0]]  # hook 유지
        build_clips = [c for c in clips if c.role == "build"]
        payoff = [c for c in clips if c.role == "payoff"]
        
        # build 클립을 하나씩 추가하면서 최대 길이를 넘지 않도록
        for clip in build_clips:
            current_dur = sum(c.end_sec - c.start_sec for c in trimmed + payoff)
            if current_dur + (clip.end_sec - clip.start_sec) > max_dur:
                break
            trimmed.append(clip)
        trimmed.extend(payoff)
        
        # 최소 길이를 보장하기 위해 필요시 클립 길이 조정
        final_dur = sum(c.end_sec - c.start_sec for c in trimmed)
        if final_dur < min_dur and trimmed:
            # 마지막 클립을 늘려서 최소 길이 보장
            needed = min_dur - final_dur
            last = trimmed[-1]
            trimmed[-1] = StoryClip(
                role=last.role,
                start_sec=last.start_sec,
                end_sec=last.end_sec + needed,
                subtitle=last.subtitle,
                tts_line=last.tts_line,
                use_original_audio=last.use_original_audio,
            )
        return trimmed

    # 너무 짧은 경우: 각 클립의 길이를 늘림 (원본 타임스탬프 유지)
    if duration < min_dur:
        needed = min_dur - duration
        # 각 클립에 균등하게 분배하되, 최대 1.5배까지만 늘림
        max_extend_per_clip = min(needed / len(clips), max(c.end_sec - c.start_sec for c in clips) * 0.5) if clips else 0
        
        adjusted = []
        remaining_extend = needed
        for i, clip in enumerate(clips):
            clip_duration = clip.end_sec - clip.start_sec
            # 마지막 클립이 아니면 균등 분배, 마지막 클립은 남은 분량 모두 할당
            if i < len(clips) - 1:
                extend_by = min(max_extend_per_clip, remaining_extend / (len(clips) - i))
            else:
                extend_by = remaining_extend
            
            # 클립 길이를 최대 1.5배까지만 늘림
            max_extend = clip_duration * 0.5
            extend_by = min(extend_by, max_extend)
            
            adjusted.append(
                StoryClip(
                    role=clip.role,
                    start_sec=clip.start_sec,
                    end_sec=clip.end_sec + extend_by,
                    subtitle=clip.subtitle,
                    tts_line=clip.tts_line,
                    use_original_audio=clip.use_original_audio,
                )
            )
            remaining_extend -= extend_by
        
        # 여전히 부족하면 마지막 클립을 더 늘림
        final_dur = sum(c.end_sec - c.start_sec for c in adjusted)
        if final_dur < min_dur and adjusted:
            needed = min_dur - final_dur
            last = adjusted[-1]
            adjusted[-1] = StoryClip(
                role=last.role,
                start_sec=last.start_sec,
                end_sec=last.end_sec + needed,
                subtitle=last.subtitle,
                tts_line=last.tts_line,
                use_original_audio=last.use_original_audio,
            )
        return adjusted
    
    return clips
