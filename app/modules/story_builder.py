# 단순히 데이터를 합치는 수준을 넘어, **영상 스토리텔링의 기본 구조(기-승-전-결)**를 강제로 부여하고, 정해진 시간(예: 60초) 안에 영상을 맞추는 물리적 보정 로직이 핵심

# 코드의 핵심 역할
# 이 코드는 AI가 추천한 하이라이트들(RankedMoment)을 가져와서 **Hook(시선 끌기) - Build(전개) - Payoff(결론/반전)**의 구조로 재배치하고, 영상이 너무 짧거나 길지 않도록 초 단위로 길이를 조절하여 최종 편집 본(StoryClip)을 완성
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


# 스토리 구조 강제 (build_story)
# 코드를 보면 하이라이트들을 단순히 점수순으로 나열하지 않고 story_role에 따라 분류합니다.

# Hook: 가장 먼저 시선을 사로잡을 장면 (1개 선택)

# Build: 중간을 채워줄 장면들 (최대 6개까지 넉넉하게 선택)

# Payoff: 확실한 마무리 (1개 선택)

# 이 구조 덕분에 단순히 "웃긴 장면 모음"이 아니라, **기승전결이 있는 "하나의 이야기"**가 만들어집니다.
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

# 시간 맞추기 알고리즘 (_fit_duration)
# 쇼츠는 60초라는 명확한 한계가 있습니다. 이 함수는 영상 길이를 **target_duration_sec**에 맞추기 위해 필사적으로 계산합니다.

# 영상이 너무 길 때 (Trim):

# 핵심인 Hook과 Payoff는 건드리지 않습니다.

# 중간의 Build 클립들을 하나씩 추가해보며 최대 허용 길이를 넘지 않는 선에서 자릅니다.

# 영상이 너무 짧을 때 (Extend):

# 쇼츠가 너무 휙 지나가지 않도록 부족한 시간(needed)을 계산합니다.

# 각 클립의 길이를 최대 1.5배까지 조금씩 늘려가며 목표 시간을 채웁니다.

# 그래도 부족하면 마지막 클립을 강제로 늘려서라도 최소 길이를 맞춥니다.
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
