from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoryClip:
    role: str
    start_sec: float
    end_sec: float
    subtitle: str
    use_original_audio: bool
    pacing_note: str = ""
    chunk_index: int = -1
    candidate_index: int = -1
    character_focus: tuple[str, ...] = ()


@dataclass(frozen=True)
class TTSCue:
    """편집 타임라인 절대 시간 기준 TTS cue.

    start_sec=0이 쇼츠 시작점. 클립 경계와 무관하게 어디든 배치 가능.
    voice/speed는 app.modules.tts의 프리셋 라벨.
    """
    start_sec: float
    end_sec: float
    text: str
    voice: str = "ko_female"
    speed: str = "normal"


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
    """인접 클립 간 인물/시간 연속성을 검사해 경고 문자열 목록을 반환합니다."""
    warnings: list[str] = []
    for i in range(1, len(clips)):
        prev, curr = clips[i - 1], clips[i]
        # 시간 점프
        gap = curr.start_sec - prev.end_sec
        if gap > time_jump_warning_sec:
            warnings.append(
                f"[clip {i}] 큰 시간 점프 ({gap:.0f}초)"
            )
        # 인물 단절
        if prev.character_focus and curr.character_focus:
            prev_set = set(prev.character_focus)
            curr_set = set(curr.character_focus)
            if not (prev_set & curr_set):
                warnings.append(
                    f"[clip {i}] 인물 연속성 없음 ({prev_set} → {curr_set})"
                )
    return warnings


