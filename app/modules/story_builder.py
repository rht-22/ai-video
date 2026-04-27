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
    chunk_index: int = -1
    candidate_index: int = -1
    character_focus: tuple[str, ...] = ()
    bridges_from_previous: str = ""


def validate_story_clips(
    clips: list[StoryClip],
    min_duration_sec: float,
    max_duration_sec: float,
) -> tuple[bool, str]:
    """스토리 클립이 유효한 쇼츠를 구성하는지 검증합니다."""
    if not clips:
        return False, "클립이 없습니다"

    total_dur = sum(c.end_sec - c.start_sec for c in clips)
    if total_dur < min_duration_sec:
        return False, f"너무 짧음: {total_dur:.1f}초 < {min_duration_sec}초"
    if total_dur > max_duration_sec * 1.5:
        return False, f"너무 김: {total_dur:.1f}초 > {max_duration_sec * 1.5}초"

    for c in clips:
        if c.end_sec <= c.start_sec:
            return False, f"잘못된 시간 범위: {c.start_sec}~{c.end_sec}"

    return True, "OK"


def validate_clip_coherence(
    clips: list[StoryClip],
    time_jump_warning_sec: float = 300.0,
) -> list[str]:
    """인접 클립 간 인물/시간 연속성을 검사해 경고 문자열 목록을 반환합니다.

    실패시키지 않고 단지 경고만 반환 (fix는 TTS 브릿지로).
    """
    warnings: list[str] = []
    for i in range(1, len(clips)):
        prev, curr = clips[i - 1], clips[i]
        # 시간 점프
        gap = curr.start_sec - prev.end_sec
        if gap > time_jump_warning_sec:
            warnings.append(
                f"[clip {i}] 큰 시간 점프 ({gap:.0f}초) — bridges_from_previous 필요"
            )
        # 인물 단절
        if prev.character_focus and curr.character_focus:
            prev_set = set(prev.character_focus)
            curr_set = set(curr.character_focus)
            if not (prev_set & curr_set) and not curr.bridges_from_previous:
                warnings.append(
                    f"[clip {i}] 인물 연속성 없음 ({prev_set} → {curr_set}) — bridges_from_previous 권장"
                )
    return warnings


def build_story(
    ranked: Iterable[RankedMoment],
    target_duration_sec: float,
    tolerance_sec: float,
    min_duration_sec: float | None = None,
    max_duration_sec: float | None = None,
) -> list[StoryClip]:
    """RankedMoment에서 story_role 기반으로 클립을 구성합니다 (폴백용)."""
    ranked_list = list(ranked)
    hook_candidates = [m for m in ranked_list if m.story_role == "hook"]
    build_candidates = [m for m in ranked_list if m.story_role == "build"]
    payoff_candidates = [m for m in ranked_list if m.story_role == "payoff"]

    if not hook_candidates or not build_candidates or not payoff_candidates:
        raise ValueError("hook/build/payoff 후보가 충분하지 않습니다")

    selected = [hook_candidates[0]]
    selected.extend(build_candidates[:6])
    selected.append(payoff_candidates[0])

    clips = [
        StoryClip(
            role=m.story_role,
            start_sec=m.start_sec,
            end_sec=m.end_sec,
            subtitle=m.suggested_tts_line or m.description,
            tts_line=m.suggested_tts_line,
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
    min_dur = min_duration_sec if min_duration_sec is not None else target_duration_sec - tolerance_sec
    max_dur = max_duration_sec if max_duration_sec is not None else target_duration_sec + tolerance_sec

    duration = sum(c.end_sec - c.start_sec for c in clips)

    if min_dur <= duration <= max_dur:
        return clips

    # 너무 긴 경우: build 클립 제거
    if duration > max_dur:
        trimmed = [clips[0]]  # hook 유지
        build_clips = [c for c in clips if c.role == "build"]
        payoff = [c for c in clips if c.role == "payoff"]

        for clip in build_clips:
            current_dur = sum(c.end_sec - c.start_sec for c in trimmed + payoff)
            if current_dur + (clip.end_sec - clip.start_sec) > max_dur:
                break
            trimmed.append(clip)
        trimmed.extend(payoff)

        final_dur = sum(c.end_sec - c.start_sec for c in trimmed)
        if final_dur < min_dur and trimmed:
            needed = min_dur - final_dur
            last = trimmed[-1]
            trimmed[-1] = StoryClip(
                role=last.role,
                start_sec=last.start_sec,
                end_sec=last.end_sec + needed,
                subtitle=last.subtitle,
                tts_line=last.tts_line,
                use_original_audio=last.use_original_audio,
                chunk_index=last.chunk_index,
                candidate_index=last.candidate_index,
            )
        return trimmed

    # 너무 짧은 경우: 각 클립 길이 확장
    if duration < min_dur:
        needed = min_dur - duration
        max_extend_per_clip = (
            min(needed / len(clips), max(c.end_sec - c.start_sec for c in clips) * 0.5)
            if clips else 0
        )

        adjusted = []
        remaining_extend = needed
        for i, clip in enumerate(clips):
            clip_duration = clip.end_sec - clip.start_sec
            if i < len(clips) - 1:
                extend_by = min(max_extend_per_clip, remaining_extend / (len(clips) - i))
            else:
                extend_by = remaining_extend

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
                    chunk_index=clip.chunk_index,
                    candidate_index=clip.candidate_index,
                )
            )
            remaining_extend -= extend_by

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
                chunk_index=last.chunk_index,
                candidate_index=last.candidate_index,
            )
        return adjusted

    return clips
