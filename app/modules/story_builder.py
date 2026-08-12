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
    visual_essential: bool = False
    tts_draft: str = ""


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
    min_clip_count: int = 3,
    max_duration_tolerance: float = 1.5,
) -> tuple[bool, str]:
    """스토리 클립이 유효한 쇼츠를 구성하는지 검증합니다.

    min_clip_count: highlight형은 1개로도 가능하므로 호출부에서 1로 낮춰야 함.
    storytelling형은 hook/build/payoff 최소 3개 보장.
    max_duration_tolerance: 총 길이 상한 배수. 1.5=레거시(최대 90s 허용 → 100s 클립 원인).
      길이를 시장 승자(~46s)에 맞추려면 호출부에서 1.1 등으로 낮춘다 (config.max_duration_tolerance).
    """
    if not clips:
        return False, "클립이 없습니다"

    if len(clips) < min_clip_count:
        return False, f"클립 수 부족: {len(clips)}개 < {min_clip_count}개"

    total_dur = sum(c.end_sec - c.start_sec for c in clips)
    max_allowed = max_duration_sec * max_duration_tolerance
    if total_dur < min_duration_sec:
        return False, f"너무 짧음: {total_dur:.1f}초 < {min_duration_sec}초"
    if total_dur > max_allowed:
        return False, f"너무 김: {total_dur:.1f}초 > {max_allowed:.1f}초"

    for c in clips:
        if c.end_sec <= c.start_sec:
            return False, f"잘못된 시간 범위: {c.start_sec}~{c.end_sec}"

    return True, "OK"


def select_diverse_storylines(
    storylines: list[dict],
    *,
    max_count: int,
    skeleton: dict | None = None,
) -> list[dict]:
    """ranked_storylines에서 chunk_index/emotional_phase 다양성을 보장하며 max_count개 선택.

    전략:
    1) 우선 점수 1위는 무조건 선택
    2) 이후 후보부터: 이미 선택된 것들과 (a) chunk_index가 다르거나 (b) emotional_phase가 다르면 선택
    3) 다양성 후보가 부족하면 점수 순으로 채움 (폴백)

    storyline의 chunk_index 추출:
    - storytelling: storyline.hook.chunk_index (없으면 build[0].chunk_index)
    - highlight: storyline.chunk_index

    emotional_phase는 skeleton.emotional_arc[]의 time_range에 storyline의 시작 시간이
    포함되는 phase 이름으로 결정.
    """
    if not storylines:
        return []

    if max_count <= 1 or len(storylines) <= 1:
        return list(storylines[:max_count])

    def _start_sec(sl: dict) -> float:
        if sl.get("shorts_type") == "highlight":
            return float(sl.get("start_sec", 0.0))
        story = sl.get("storyline") or {}
        hook = story.get("hook") or {}
        if hook:
            return float(hook.get("start_sec", 0.0))
        builds = story.get("build") or []
        if builds:
            return float(builds[0].get("start_sec", 0.0))
        payoff = story.get("payoff") or {}
        return float(payoff.get("start_sec", 0.0))

    def _chunk_idx(sl: dict) -> int:
        if sl.get("shorts_type") == "highlight":
            return int(sl.get("chunk_index", -1))
        story = sl.get("storyline") or {}
        hook = story.get("hook") or {}
        if hook and "chunk_index" in hook:
            return int(hook.get("chunk_index", -1))
        builds = story.get("build") or []
        if builds:
            return int(builds[0].get("chunk_index", -1))
        return -1

    def _phase(sl: dict) -> str:
        if not skeleton or not skeleton.get("emotional_arc"):
            return ""
        s = _start_sec(sl)
        for ph in skeleton.get("emotional_arc", []):
            tr = ph.get("time_range", "")
            if "-" not in tr:
                continue
            try:
                lo, hi = tr.split("-", 1)
                lo_f, hi_f = float(lo), float(hi)
                if lo_f <= s <= hi_f:
                    return ph.get("phase", "")
            except (ValueError, TypeError):
                continue
        return ""

    # 점수 내림차순 정렬 가정 (호출부에서 정렬됨)
    selected: list[dict] = []
    used_chunks: set[int] = set()
    used_phases: set[str] = set()

    # 1순위: 1등은 무조건
    selected.append(storylines[0])
    used_chunks.add(_chunk_idx(storylines[0]))
    p0 = _phase(storylines[0])
    if p0:
        used_phases.add(p0)

    # 2순위 이후: 다양성 우선
    for sl in storylines[1:]:
        if len(selected) >= max_count:
            break
        c = _chunk_idx(sl)
        p = _phase(sl)
        is_diverse = (c not in used_chunks) or (p and p not in used_phases)
        if is_diverse:
            selected.append(sl)
            used_chunks.add(c)
            if p:
                used_phases.add(p)

    # 폴백: 다양성 후보가 부족하면 남은 것을 점수 순으로 채움
    if len(selected) < max_count:
        for sl in storylines[1:]:
            if len(selected) >= max_count:
                break
            if sl not in selected:
                selected.append(sl)

    return selected[:max_count]


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
        # 같은 role 인접 컷의 원본 구간 겹침 — 그대로 렌더되면 같은 대사가 두 번 재생된다
        # (2026-08-06 실측). 다른 role 간 겹침은 선공개형 훅(의도된 반복)일 수 있어 경고하지 않는다.
        if gap < 0 and prev.role == curr.role:
            warnings.append(
                f"[clip {i}] 같은 role({curr.role}) 인접 컷 겹침 {-gap:.2f}초 "
                f"({prev.start_sec:.2f}~{prev.end_sec:.2f} ↔ {curr.start_sec:.2f}~{curr.end_sec:.2f})"
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


