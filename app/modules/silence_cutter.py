from __future__ import annotations

import os
from dataclasses import dataclass, replace
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


# ─────────────────────────────────────────────────────────────
# PR-6: gap-단위 무음 컷 + A/B 튜닝 프로파일
# ─────────────────────────────────────────────────────────────
# 벤치마크(재미쇼츠/스토리순삭 vs 시장 상위 클립): ai-video 클립 무음 비율 ~49% vs 승자 ~9%.
# 원인 — cut_silence_with_story_filter 가 clip 안 gap 하나라도 보호 대상이면 clip *전체* 무음을
# 유지(통째 보호)한다. PR-6 은 (1) gap-단위 보호(안전한 gap 은 컷하고 보호 대상 gap 만 유지)
# (2) 과도한 보호 조건 완화(메타 없음/단일 액션 동사만으로 긴 dead air 를 보호하지 않음) 를 도입.
#
# ⚠️ 이건 관찰 벤치마크에서 나온 *가설*이다. 실제 retention 개선은 검증 전이므로 하드코딩 플립이
# 아니라 *프로파일*로 토글한다. conservative = 기존 동작 정확 재현(A/B 베이스라인),
# aggressive = 가설(무음 적극 제거). 채널(재미쇼츠/스토리순삭) A/B 후 채택 여부 결정.


@dataclass(frozen=True)
class SilenceCutProfile:
    """무음 컷 공격성 프로파일 — A/B 비교용 튜너블.

    conservative: 레거시 통째-보호. clip 안 gap 하나라도 보호 대상이면 clip 전체 무음 유지.
    aggressive:   gap-단위 보호. 안전한 gap 은 컷하고 보호 대상 gap 만 유지 + 보호 조건 완화.
    """
    name: str = "conservative"
    # True → gap-단위(안전 gap 컷, 보호 gap 만 유지). False → 통째-보호(레거시).
    gap_level: bool = False
    max_gap_sec: float = 0.4          # 이 이상 무음 gap 만 컷 대상
    padding_sec: float = 0.15         # 대사 앞뒤 여유 (컷 후 재팽창 주의)
    min_interval_sec: float = 0.3     # 너무 짧은 keep 구간은 앞에 흡수
    # ── _is_gap_safe_to_cut 보호 조건 토글 (True = 그 조건에서 보호) ──
    protect_missing_candidate: bool = True   # candidate 메타 없음 → 보호?
    protect_action_verb: bool = True         # description 에 액션 동사 1개라도 → 보호?
    protect_speaker_change: bool = True      # 화자([내레이션]) 변경 → 보호?
    protect_clip_boundary_gap: bool = True   # 한쪽 segment 가 clip 경계(None) → 보호?
    # 보호 대상 *내부* gap 이라도 이 길이(초) 넘는 무음은 가운데를 잘라 이만큼만 유지.
    # None → 무제한(통째 유지). 값이 있으면 보호 gap 에도 dead-air 상한이 걸린다.
    protected_gap_max_sec: float | None = None
    # E19-6(2026-08-28) 잔여 정적 하한 — 안전한 gap 을 **잘라도** 이만큼의 정적(초)은
    # 남긴다(양쪽 절반씩). 벤치마크 실측: 반전 직후 0.3~0.5s 정적이 편의 호흡이다 —
    # aggressive 가 이것까지 밀면 호흡이 죽는다. None(기본) = 종전 동작 그대로(회귀 0 —
    # 기존 잔여 정적은 2×padding_sec 이고, 그 이하 값도 불변이다). gap-level 경로에만
    # 작용한다(conservative 는 손대지 않는다 — 발주서 §6).
    min_residual_pause_sec: float | None = None


# 레거시 정확 재현 — A/B 베이스라인. 기존 cut_silence_with_story_filter 기본값과 동일.
CONSERVATIVE_PROFILE = SilenceCutProfile(name="conservative")

# 가설 — 무음 적극 제거. gap-단위 + 과도한 보호 완화 + 보호 gap dead-air 상한.
AGGRESSIVE_PROFILE = SilenceCutProfile(
    name="aggressive",
    gap_level=True,
    max_gap_sec=0.3,                  # 0.4 → 0.3: 더 촘촘히 컷
    padding_sec=0.12,                 # 0.15 → 0.12: 컷 후 무음 재팽창 억제
    min_interval_sec=0.3,
    protect_missing_candidate=False,  # 메타 없음 ≠ 긴 dead air 보호
    protect_action_verb=False,        # 단일 액션 동사 ≠ clip 무음 통째 보호
    protect_speaker_change=True,      # 화자 변경은 유지(점프컷 방지)하되 cap 으로 상한
    protect_clip_boundary_gap=False,
    protected_gap_max_sec=1.0,        # 보호 gap 도 최대 1.0s 무음만
)

_PROFILES: dict[str, SilenceCutProfile] = {
    CONSERVATIVE_PROFILE.name: CONSERVATIVE_PROFILE,
    AGGRESSIVE_PROFILE.name: AGGRESSIVE_PROFILE,
}


# env 로 여는 잔여 정적 하한의 허용 범위 — 0 이하는 의미가 없고(끄려면 env 를 지운다),
# 2초 넘게 남기면 컷 자체가 무의미하다.
_RESIDUAL_ENV = "SILENCE_CUT_MIN_RESIDUAL_SEC"
_RESIDUAL_RANGE = (0.0, 2.0)          # (초과, 이하)


def get_silence_profile(name: str | None) -> SilenceCutProfile:
    """프로파일 이름 → SilenceCutProfile. 미지정/미상이면 conservative(베이스라인).

    E19-6: env `SILENCE_CUT_MIN_RESIDUAL_SEC` 가 있으면 gap-level 프로파일에 잔여 정적
    하한을 얹는다. 잘못된 값은 **즉시 실패** — 조용히 무시하면 오타가 기본값으로 발행돼
    'A/B 를 걸었는데 왜 그대로지'가 된다(transcribe-backend 와 같은 규율)."""
    prof = (_PROFILES.get(name.strip().lower(), CONSERVATIVE_PROFILE)
            if name else CONSERVATIVE_PROFILE)
    raw = os.environ.get(_RESIDUAL_ENV)
    if raw not in (None, ""):
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"{_RESIDUAL_ENV} 가 숫자가 아닙니다({raw!r})") from None
        lo, hi = _RESIDUAL_RANGE
        if not (lo < v <= hi):
            raise ValueError(f"{_RESIDUAL_ENV} 값 {v:g} 이 범위 밖입니다({lo:g} 초과 {hi:g} 이하)")
        if prof.gap_level:
            prof = replace(prof, min_residual_pause_sec=v)
    return prof


def _is_gap_safe_to_cut(
    candidate: dict | None,
    before_seg: SpeechSegment | None,
    after_seg: SpeechSegment | None,
    profile: SilenceCutProfile = CONSERVATIVE_PROFILE,
) -> bool:
    """gap 양쪽 segment + candidate 메타로 컷 가능 여부 판정.

    True 면 무음 컷 가능, False 면 보호. 각 보호 조건은 profile 토글로 켜고 끈다.
    (단 candidate.visual_essential 은 의도적 플래그라 프로파일과 무관하게 항상 보호.)
    """
    if candidate is None:
        # 메타 없음 — conservative 는 보호, aggressive 는 컷 허용.
        return not profile.protect_missing_candidate
    if candidate.get("visual_essential") is True:
        return False
    if profile.protect_action_verb and _has_action_verb(candidate.get("description") or ""):
        return False
    # 양쪽 segment 존재 패턴 검사
    if before_seg is None and after_seg is None:
        # clip 안 transcript 가 없음 — visual_essential=False 이고 액션 없으면 안전 (긴 무음 컷 가능)
        return True
    if before_seg is None or after_seg is None:
        # 한쪽 None 이면 clip 경계 인접 — conservative 는 보수적 보호.
        return not profile.protect_clip_boundary_gap
    # 양쪽 segment 모두 있음: [내레이션] 접두 패턴이 일치해야 (둘 다 있거나 둘 다 없음)
    if profile.protect_speaker_change and _is_narration_seg(before_seg) != _is_narration_seg(after_seg):
        return False
    return True


def _keep_whole(clip: StoryClip) -> SilenceCutResult:
    """clip 전체 유지 (무음 컷 없음)."""
    return SilenceCutResult(
        original_clip=clip,
        keep_intervals=[Interval(clip.start_sec, clip.end_sec)],
        total_removed_sec=0.0,
    )


def _absorb_short_intervals(
    intervals: list[Interval], min_interval_sec: float,
) -> list[Interval]:
    """min_interval_sec 보다 짧은 keep 구간은 앞 구간에 흡수 (기존 동작 보존)."""
    filtered: list[Interval] = []
    for interval in intervals:
        duration = interval.end_sec - interval.start_sec
        if filtered and duration < min_interval_sec:
            filtered[-1] = Interval(filtered[-1].start_sec, interval.end_sec)
        else:
            filtered.append(interval)
    return filtered


def _finalize(clip: StoryClip, keep_intervals: list[Interval]) -> SilenceCutResult:
    clip_duration = clip.end_sec - clip.start_sec
    kept = sum(i.end_sec - i.start_sec for i in keep_intervals)
    return SilenceCutResult(
        original_clip=clip,
        keep_intervals=keep_intervals,
        total_removed_sec=max(0.0, clip_duration - kept),
    )


def _cut_clip_whole(
    clip: StoryClip,
    clip_segments: list[SpeechSegment],
    cand: dict | None,
    profile: SilenceCutProfile,
) -> SilenceCutResult:
    """레거시 통째-보호: 내부 gap 하나라도 보호 대상이면 clip 전체 유지."""
    if clip_segments:
        edges: list[tuple[SpeechSegment | None, SpeechSegment | None]] = [
            (clip_segments[i], clip_segments[i + 1]) for i in range(len(clip_segments) - 1)
        ]
    else:
        edges = [(None, None)]

    for before, after in edges:
        gap_start = before.end_sec if before is not None else clip.start_sec
        gap_end = after.start_sec if after is not None else clip.end_sec
        if gap_end - gap_start < profile.max_gap_sec:
            continue  # gap 작으면 컷 대상 아님 → 안전 판정 무관
        if not _is_gap_safe_to_cut(cand, before, after, profile):
            return _keep_whole(clip)  # 하나라도 보호 → clip 통째 유지

    if not clip_segments:
        return _keep_whole(clip)

    padded = [
        Interval(max(clip.start_sec, seg.start_sec - profile.padding_sec),
                 min(clip.end_sec, seg.end_sec + profile.padding_sec))
        for seg in clip_segments
    ]
    merged: list[Interval] = []
    for interval in sorted(padded):
        if merged and interval.start_sec <= merged[-1].end_sec:
            merged[-1] = Interval(merged[-1].start_sec, max(merged[-1].end_sec, interval.end_sec))
        else:
            merged.append(interval)
    return _finalize(clip, _absorb_short_intervals(merged, profile.min_interval_sec))


def _cut_clip_gap_level(
    clip: StoryClip,
    clip_segments: list[SpeechSegment],
    cand: dict | None,
    profile: SilenceCutProfile,
) -> SilenceCutResult:
    """gap-단위 보호: 안전한 내부 gap 은 컷하고 보호 대상 gap 만 유지.

    통째-보호와 달리 보호 대상 gap 1개가 같은 clip 의 다른 안전한 gap 컷을 막지 않는다.
    보호 대상 gap 도 profile.protected_gap_max_sec 가 설정되면 dead-air 상한이 걸린다.
    clip 시작/끝 인접 dead air 는 padding 클리핑으로 항상 제거 (통째-보호와 동일).
    """
    if not clip_segments:
        # 대사 없는 비주얼 비트 — 컷하면 비트 자체가 소멸하므로 통째 유지.
        # (전체 길이 조정은 파이프라인의 length-fit 단계가 담당.)
        return _keep_whole(clip)

    pad = profile.padding_sec
    cap = profile.protected_gap_max_sec
    padded = [
        Interval(max(clip.start_sec, seg.start_sec - pad),
                 min(clip.end_sec, seg.end_sec + pad))
        for seg in clip_segments
    ]

    out: list[Interval] = []
    cur = padded[0]
    # 🛑 gap 판정은 '지금까지 소비한 최대 종료점(cover_end)' 기준이어야 한다. 직전 세그먼트
    # 하나만 보면(구현이 그랬다) 청크 오버랩(180s)으로 이중 전사된 세그먼트가 이미 덮인
    # 구간 한가운데서 양수 gap 으로 읽혀 새 섬을 열고, keep-interval 끼리 겹쳐 **같은 대사가
    # 두 번 재생**된다(2026-08-06 실측: 918.00~930.16 / 928.15~933.55, 2.01s 중복).
    # _cut_clip_whole(위)·cut_silence_from_clips 의 merge 규약을 이 함수에도 적용한 것.
    cover_end = clip_segments[0].end_sec
    for i in range(1, len(clip_segments)):
        prev_seg, this_seg = clip_segments[i - 1], clip_segments[i]
        raw_gap = this_seg.start_sec - cover_end
        should_cut = (
            raw_gap >= profile.max_gap_sec
            and _is_gap_safe_to_cut(cand, prev_seg, this_seg, profile)
        )
        if should_cut:
            resid = profile.min_residual_pause_sec
            if resid is not None and resid > 2 * pad:
                # E19-6: 잘라도 이만큼의 정적은 남긴다(양쪽 절반씩 — cap 분기와 같은
                # 기하). gap 이 하한보다 짧으면 gap 전체가 남는다 — 없는 정적을
                # 만들어내지 않는다. resid ≤ 2×pad 면 기존 패딩이 이미 그만큼 남기므로
                # 아래 종전 분기와 산출이 같다(회귀 0 조건의 다른 표현).
                keep = min(resid, raw_gap)
                half = keep / 2.0
                left_end = min(clip.end_sec, cover_end + max(half, pad))
                right_start = max(clip.start_sec, this_seg.start_sec - max(half, pad))
                closed = Interval(cur.start_sec, max(cur.end_sec, left_end))
                out.append(closed)
                cur = Interval(max(min(padded[i].start_sec, right_start), closed.end_sec),
                               padded[i].end_sec)
            else:
                # 안전한 gap — 현재 섬을 닫고 다음 섬 시작 (사이 무음 제거).
                # 새 섬은 직전 섬 끝 이전으로 되감지 않는다(겹침 차단).
                out.append(cur)
                cur = Interval(max(padded[i].start_sec, cur.end_sec), padded[i].end_sec)
        elif cap is not None and raw_gap > cap:
            # 보호하지만 cap 초과 무음 — 가운데를 잘라 cap(양쪽 cap/2)만 유지
            half = cap / 2.0
            left_end = min(clip.end_sec, cover_end + half)
            right_start = max(clip.start_sec, this_seg.start_sec - half)
            closed = Interval(cur.start_sec, max(cur.end_sec, left_end))
            out.append(closed)
            cur = Interval(max(min(padded[i].start_sec, right_start), closed.end_sec),
                           padded[i].end_sec)
        else:
            # 보호(또는 작은 gap) — 두 섬을 잇는다 (무음 유지)
            cur = Interval(cur.start_sec, max(cur.end_sec, padded[i].end_sec))
        cover_end = max(cover_end, this_seg.end_sec)
    out.append(cur)

    # 완전 포함 방어: 클램프로 길이가 0 이하가 된 섬(뒤 조각이 앞 섬에 통째로 덮임)은
    # 내용이 이미 앞 섬에 있으므로 버린다(실측 4건이 이 유형).
    out = [iv for iv in out if iv.end_sec > iv.start_sec]

    return _finalize(clip, _absorb_short_intervals(out, profile.min_interval_sec))


def apply_speech_gap_pacing(
    clips: list[StoryClip],
    transcript_segments: list,
    tts_cues: list[dict] | None,
    params: dict,
) -> tuple[list[StoryClip], dict]:
    """E20-B1(2026-08-28) 발화 갭 페이싱 — 조립 마지막에 도는 **무발화** 상한 패스.

    silence_cut(dB·전사 혼합)과 별개로 필요한 이유(김부장 v3 실측):
    ① 앰비언스 바닥이 −16dB 면 dB 무음이 0건이라 커터가 무력하고,
    ② visual_essential 클립은 통째 보존이라 내부 무발화(1.1·2.3초)가 그대로 나가고,
    ③ silence_cut 은 snap·narrative-ext·gap-fill **앞**에 돌아서 확장이 만든 무발화를
       못 보며, 유일한 컷(1.0초)은 gap-fill 이 도로 메웠다.
    그래서 이 패스는 gap-fill **뒤**·length-clamp **앞**에서 최종 클립을 본다.

    - 커버리지 = 대사 전사 ∪ 계획된 내레이션 cue 창(source_time_sec ~ +duration+0.2)
      — cue 가 살 자리를 잘라 버리면 안 된다(E19-3 dialogue_gaps_only 와 한 몸).
      클립 시작 직전(-6s 이내) 앵커는 cue-resolve 가 클립 머리로 클램프하므로 같은
      자리를 커버리지로 친다.
    - 내부 무발화 run > max_speech_gap_sec → 가운데를 잘라 gap_residual_sec(양쪽
      절반)만 남기고 클립을 쪼갠다. 조각이 0.8초 미만이 되는 컷은 접는다(조각 난사 금지).
    - 머리는 첫 커버리지 - head_lead_in_sec, 꼬리는 마지막 커버리지 + tail_hold_sec
      까지만 — 꼬리 여유가 리액션 컷 자리다(v3 엔딩 원수 표정 컷이 +2.5s 확장 구간에
      살았다).
    - 전사가 하나도 없는 클립은 건드리지 않는다(비주얼 비트 — 오판 금지).
      visual_essential 이라도 **대사가 있으면** 상한을 받는다 — v3 payoff 가 그 유형.

    순수 함수 — 넘겨받은 clips 를 건드리지 않는다. 반환 (새 클립 목록, stats).
    stats = {clips_in, clips_out, gaps_cut, removed_sec, details[]}.
    """
    max_gap = float(params["max_speech_gap_sec"])
    residual = float(params["gap_residual_sec"])
    lead_in = float(params.get("head_lead_in_sec", 1.0))
    tail_hold = float(params.get("tail_hold_sec", 2.5))
    min_piece = 0.8

    out_clips: list[StoryClip] = []
    gaps_cut = 0
    details: list[str] = []
    total_in = sum(c.end_sec - c.start_sec for c in clips)

    for clip in clips:
        cov: list[list[float]] = []
        has_speech = False
        for seg in transcript_segments or []:
            a, b = float(seg.start_sec), float(seg.end_sec)
            if b > clip.start_sec and a < clip.end_sec:
                cov.append([max(clip.start_sec, a), min(clip.end_sec, b)])
                has_speech = True
        if not has_speech:
            out_clips.append(clip)
            continue
        for cue in tts_cues or []:
            t = cue.get("source_time_sec")
            if t is None:
                continue
            d = float(cue.get("duration_sec") or 1.5) + 0.2
            a = float(t)
            if clip.start_sec - 6.0 <= a < clip.start_sec:
                a = clip.start_sec                     # cue-resolve 의 머리 클램프와 동일
            if a + d > clip.start_sec and a < clip.end_sec:
                cov.append([max(clip.start_sec, a), min(clip.end_sec, a + d)])
        cov.sort()
        merged: list[list[float]] = []
        for a, b in cov:
            if merged and a <= merged[-1][1] + 1e-9:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])

        head = max(clip.start_sec, merged[0][0] - lead_in)
        tail = min(clip.end_sec, merged[-1][1] + tail_hold)
        pieces: list[Interval] = []
        cur_start, cur_end = head, merged[0][1]
        for a, b in merged[1:]:
            gap = a - cur_end
            if gap > max_gap:
                left_end = cur_end + residual / 2.0
                right_start = a - residual / 2.0
                if left_end - cur_start >= min_piece and b - right_start >= min_piece:
                    pieces.append(Interval(cur_start, left_end))
                    details.append(f"무발화 {gap:.1f}s 컷 "
                                   f"({cur_end:.1f}~{a:.1f} → 잔여 {residual:g}s)")
                    gaps_cut += 1
                    cur_start = right_start
            cur_end = max(cur_end, b)
        pieces.append(Interval(cur_start, tail))
        if head > clip.start_sec + 0.05:
            details.append(f"머리 무발화 {head - clip.start_sec:.1f}s 정리"
                           f"(리드인 {lead_in:g}s)")
        if tail < clip.end_sec - 0.05:
            details.append(f"꼬리 무발화 {clip.end_sec - tail:.1f}s 정리"
                           f"(여유 {tail_hold:g}s)")
        for iv in pieces:
            if iv.end_sec > iv.start_sec:
                out_clips.append(replace(clip, start_sec=iv.start_sec, end_sec=iv.end_sec))

    total_out = sum(c.end_sec - c.start_sec for c in out_clips)
    stats = {"clips_in": len(clips), "clips_out": len(out_clips), "gaps_cut": gaps_cut,
             "removed_sec": round(total_in - total_out, 3), "details": details}
    return out_clips, stats


def cut_silence_with_story_filter(
    clips: list[StoryClip],
    transcript_segments: list[SpeechSegment],
    candidates_lookup: dict[tuple[int, int], dict],
    *,
    profile: SilenceCutProfile = CONSERVATIVE_PROFILE,
) -> list[SilenceCutResult]:
    """스토리-aware 무음 컷.

    각 clip 의 candidate 메타(visual_essential / description / 화자 변경)로 gap 컷 가능 여부를
    판별한다. profile.gap_level 에 따라:
      - False(conservative): 보호 대상 gap 이 하나라도 있으면 clip 통째 유지 (레거시).
      - True(aggressive):    안전한 gap 은 컷하고 보호 대상 gap 만 유지 (gap-단위).

    profile 은 A/B 비교용 튜너블 — get_silence_profile(name) 로 해석. 기본 = conservative(베이스라인).
    """
    sorted_segments = sorted(transcript_segments, key=lambda s: s.start_sec)
    results: list[SilenceCutResult] = []

    for clip in clips:
        # visual_essential clip 은 프로파일과 무관하게 통째 유지 (의도적 시각 비트 보호)
        if getattr(clip, "visual_essential", False):
            results.append(_keep_whole(clip))
            continue

        # 이 clip 의 candidate 메타 조회 — chunk_index=0 falsy 함정 회피 (or -1 패턴 금지)
        _ci = getattr(clip, "chunk_index", None)
        _cd = getattr(clip, "candidate_index", None)
        key = (int(_ci) if _ci is not None else -1,
               int(_cd) if _cd is not None else -1)
        cand = candidates_lookup.get(key)

        clip_segments = [
            seg for seg in sorted_segments
            if seg.end_sec > clip.start_sec and seg.start_sec < clip.end_sec
        ]

        if profile.gap_level:
            results.append(_cut_clip_gap_level(clip, clip_segments, cand, profile))
        else:
            results.append(_cut_clip_whole(clip, clip_segments, cand, profile))

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