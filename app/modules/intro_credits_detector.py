"""인트로/크레딧 감지 및 제외 모듈.

하이브리드 접근:
  Layer 1 — Config 하드 경계 (skip_intro_sec, skip_credits_sec)
  Layer 2 — SRT 패턴 감지 (자막 키워드 클러스터)
  Layer 3 — 포스트필터 (Gemini 분석 후 제외 구간 내 moment 제거)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.modules.speech import SpeechSegment


# ── SRT 크레딧 감지용 패턴 ──
CREDIT_PATTERNS_KO = [
    "감독", "연출", "극본", "각본", "출연", "제작", "제공",
    "기획", "협찬", "조연출", "촬영", "편집", "음악", "미술",
    "조명", "동시녹음", "작가", "PD", "프로듀서",
]
CREDIT_PATTERNS_EN = [
    "directed by", "produced by", "written by", "cast",
    "executive producer", "director", "screenplay",
    "cinematography", "editor", "music by",
    "CAST", "END", "CREDITS", "THE END",
]
ALL_CREDIT_PATTERNS = CREDIT_PATTERNS_KO + CREDIT_PATTERNS_EN

# 감지 파라미터
SCAN_WINDOW_SEC = 120.0   # 첫/마지막 N초 구간만 스캔
CLUSTER_WINDOW_SEC = 30.0  # 이 윈도우 안에 패턴이 3개 이상이면 크레딧
MIN_CLUSTER_COUNT = 3      # 최소 패턴 매칭 수 (오탐 방지)


@dataclass(frozen=True)
class ExclusionZones:
    """인트로/크레딧 제외 구간 정보."""
    intro_end_sec: float        # 콘텐츠 시작점 (이전은 인트로)
    credits_start_sec: float    # 콘텐츠 종료점 (이후는 크레딧)
    detection_method: str       # "config" | "srt" | "hybrid" | "none"
    confidence: float           # 0.0~1.0

    def to_dict(self) -> dict:
        return {
            "intro_end_sec": self.intro_end_sec,
            "credits_start_sec": self.credits_start_sec,
            "detection_method": self.detection_method,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ExclusionZones:
        return cls(
            intro_end_sec=data["intro_end_sec"],
            credits_start_sec=data["credits_start_sec"],
            detection_method=data["detection_method"],
            confidence=data["confidence"],
        )

    @classmethod
    def none(cls, duration_sec: float) -> ExclusionZones:
        """제외 없음 (기본값)."""
        return cls(
            intro_end_sec=0.0,
            credits_start_sec=duration_sec,
            detection_method="none",
            confidence=1.0,
        )


def detect_credits_from_srt(
    srt_segments: list[SpeechSegment],
    duration_sec: float,
    *,
    patterns: list[str] | None = None,
    scan_window_sec: float = SCAN_WINDOW_SEC,
    cluster_window_sec: float = CLUSTER_WINDOW_SEC,
    min_cluster_count: int = MIN_CLUSTER_COUNT,
) -> tuple[float, float]:
    """SRT 자막에서 인트로/크레딧 경계를 감지합니다.

    Args:
        srt_segments: 파싱된 SRT 세그먼트 목록
        duration_sec: 영상 전체 길이 (초)
        patterns: 감지할 패턴 목록 (None이면 기본 패턴 사용)
        scan_window_sec: 첫/마지막 N초만 스캔
        cluster_window_sec: 클러스터 판정 윈도우 크기
        min_cluster_count: 최소 패턴 매칭 수

    Returns:
        (intro_end_sec, credits_start_sec) 튜플
        감지 실패 시 (0.0, duration_sec) 반환
    """
    if not srt_segments:
        return 0.0, duration_sec

    if patterns is None:
        patterns = ALL_CREDIT_PATTERNS

    patterns_lower = [p.lower() for p in patterns]

    # ── 인트로 감지 (영상 시작 ~ scan_window_sec) ──
    intro_end = 0.0
    intro_matches = _find_pattern_matches(
        srt_segments, patterns_lower,
        range_start=0.0, range_end=min(scan_window_sec, duration_sec),
    )
    if intro_matches:
        intro_cluster_end = _find_cluster_boundary(
            intro_matches, cluster_window_sec, min_cluster_count, direction="forward",
        )
        if intro_cluster_end is not None:
            intro_end = intro_cluster_end

    # ── 크레딧 감지 (영상 끝 - scan_window_sec ~ 끝) ──
    credits_start = duration_sec
    credits_matches = _find_pattern_matches(
        srt_segments, patterns_lower,
        range_start=max(0.0, duration_sec - scan_window_sec),
        range_end=duration_sec,
    )
    if credits_matches:
        credits_cluster_start = _find_cluster_boundary(
            credits_matches, cluster_window_sec, min_cluster_count, direction="backward",
        )
        if credits_cluster_start is not None:
            credits_start = credits_cluster_start

    return intro_end, credits_start


def _find_pattern_matches(
    segments: list[SpeechSegment],
    patterns_lower: list[str],
    range_start: float,
    range_end: float,
) -> list[tuple[float, float, str]]:
    """주어진 시간 범위 내에서 패턴 매칭을 찾습니다.

    Returns:
        (start_sec, end_sec, matched_pattern) 리스트
    """
    matches = []
    for seg in segments:
        if seg.end_sec <= range_start or seg.start_sec >= range_end:
            continue
        text_lower = seg.text.lower()
        for pattern in patterns_lower:
            if pattern in text_lower:
                matches.append((seg.start_sec, seg.end_sec, pattern))
                break  # 세그먼트당 1매칭만
    return matches


def _find_cluster_boundary(
    matches: list[tuple[float, float, str]],
    window_sec: float,
    min_count: int,
    direction: str,
) -> float | None:
    """패턴 매칭들에서 클러스터 경계를 찾습니다.

    Args:
        matches: (start_sec, end_sec, pattern) 리스트
        window_sec: 클러스터 윈도우 크기
        min_count: 최소 매칭 수
        direction: "forward" (인트로) 또는 "backward" (크레딧)

    Returns:
        클러스터 경계 시간 (초), 클러스터 미달 시 None
    """
    if len(matches) < min_count:
        return None

    sorted_matches = sorted(matches, key=lambda m: m[0])

    if direction == "forward":
        # 인트로: 앞에서부터 윈도우 슬라이딩
        for i in range(len(sorted_matches)):
            window_start = sorted_matches[i][0]
            window_end = window_start + window_sec
            count = sum(1 for m in sorted_matches if window_start <= m[0] <= window_end)
            if count >= min_count:
                # 클러스터의 마지막 매칭 end_sec를 인트로 종료점으로
                cluster_end = max(
                    m[1] for m in sorted_matches
                    if window_start <= m[0] <= window_end
                )
                return cluster_end
    else:
        # 크레딧: 뒤에서부터 윈도우 슬라이딩
        for i in range(len(sorted_matches) - 1, -1, -1):
            window_end = sorted_matches[i][1]
            window_start = window_end - window_sec
            count = sum(1 for m in sorted_matches if window_start <= m[0] <= window_end)
            if count >= min_count:
                # 클러스터의 첫 매칭 start_sec를 크레딧 시작점으로
                cluster_start = min(
                    m[0] for m in sorted_matches
                    if window_start <= m[0] <= window_end
                )
                return cluster_start

    return None


def detect_exclusion_zones(
    duration_sec: float,
    *,
    skip_intro_sec: float = 0.0,
    skip_credits_sec: float = 0.0,
    auto_detect: bool = True,
    srt_segments: list[SpeechSegment] | None = None,
    gemini_result: dict[str, Any] | None = None,
) -> ExclusionZones:
    """인트로/크레딧 제외 구간을 감지합니다.

    하이브리드 접근 (우선순위):
      1. Gemini 영상 분석 (가장 정확 — 실제 화면을 보고 판별)
      2. SRT 패턴 감지 (보조)
      3. Config 하드 경계 (사용자 수동 지정, 항상 적용)
      4. 모든 소스 중 가장 넓은 범위 적용

    Args:
        duration_sec: 영상 전체 길이 (초)
        skip_intro_sec: CLI로 지정한 인트로 건너뛰기 (초)
        skip_credits_sec: CLI로 지정한 크레딧 건너뛰기 (초)
        auto_detect: 자동 감지 활성화 여부
        srt_segments: SRT 자막 세그먼트
        gemini_result: Gemini 영상 분석 결과 (detect_intro_credits 반환값)

    Returns:
        ExclusionZones 객체

    Raises:
        ValueError: skip_intro + skip_credits >= duration_sec일 때
    """
    # 입력 검증
    if skip_intro_sec + skip_credits_sec >= duration_sec:
        raise ValueError(
            f"인트로({skip_intro_sec}초) + 크레딧({skip_credits_sec}초) 합이 "
            f"영상 길이({duration_sec:.1f}초) 이상입니다."
        )

    # Layer 1: Config 하드 경계
    config_intro_end = skip_intro_sec
    config_credits_start = duration_sec - skip_credits_sec

    # Layer 2: Gemini 영상 분석 (가장 신뢰도 높음)
    gemini_intro_end = 0.0
    gemini_credits_start = duration_sec
    used_gemini = False

    if auto_detect and gemini_result:
        if gemini_result.get("has_intro") and gemini_result.get("intro_end_sec", 0) > 0:
            gemini_intro_end = gemini_result["intro_end_sec"]
            used_gemini = True
            print(f"  - Gemini 인트로 감지: {gemini_intro_end:.1f}초까지")
            if gemini_result.get("intro_description"):
                print(f"    근거: {gemini_result['intro_description']}")
        if gemini_result.get("has_credits") and gemini_result.get("credits_start_sec", duration_sec) < duration_sec:
            gemini_credits_start = gemini_result["credits_start_sec"]
            used_gemini = True
            print(f"  - Gemini 크레딧 감지: {gemini_credits_start:.1f}초부터")
            if gemini_result.get("credits_description"):
                print(f"    근거: {gemini_result['credits_description']}")

    # Layer 3: SRT 패턴 감지 (보조)
    srt_intro_end = 0.0
    srt_credits_start = duration_sec
    used_srt = False

    if auto_detect and srt_segments:
        srt_intro_end, srt_credits_start = detect_credits_from_srt(
            srt_segments, duration_sec,
        )
        if srt_intro_end > 0 or srt_credits_start < duration_sec:
            used_srt = True
            if srt_intro_end > 0:
                print(f"  - SRT 인트로 감지: {srt_intro_end:.1f}초까지")
            if srt_credits_start < duration_sec:
                print(f"  - SRT 크레딧 감지: {srt_credits_start:.1f}초부터")

    # 하이브리드: 모든 소스 중 가장 넓은 범위 (더 많이 제외)
    final_intro_end = max(config_intro_end, gemini_intro_end, srt_intro_end)
    final_credits_start = min(config_credits_start, gemini_credits_start, srt_credits_start)

    # 유효성 재검증 (감지 결과가 비현실적일 수 있음)
    if final_intro_end >= final_credits_start:
        # Gemini/SRT 결과가 이상하면 config 값만 사용
        print("  [WARN] 감지 결과가 비현실적 — config 값만 사용")
        final_intro_end = config_intro_end
        final_credits_start = config_credits_start
        used_gemini = False
        used_srt = False

    # detection_method 결정
    has_config = skip_intro_sec > 0 or skip_credits_sec > 0
    sources = []
    if used_gemini:
        sources.append("gemini")
    if used_srt:
        sources.append("srt")
    if has_config:
        sources.append("config")

    if not sources:
        method = "none"
        confidence = 1.0
    elif len(sources) == 1:
        method = sources[0]
        confidence = {"gemini": 0.85, "srt": 0.7, "config": 1.0}[method]
    else:
        method = "hybrid"
        # Gemini가 포함되면 신뢰도 높음
        confidence = 0.9 if used_gemini else 0.8

    return ExclusionZones(
        intro_end_sec=final_intro_end,
        credits_start_sec=final_credits_start,
        detection_method=method,
        confidence=confidence,
    )


def filter_excluded_moments(
    candidates: list[dict[str, Any]],
    zones: ExclusionZones,
) -> list[dict[str, Any]]:
    """Gemini 분석 후보 중 제외 구간에 걸리는 것을 필터링합니다.

    moment의 중심(midpoint)이 제외 구간에 있으면 제거합니다.
    (부분적으로 걸리는 경우 중심 기준으로 판단)

    Args:
        candidates: Gemini 후보 모멘트 리스트
        zones: 제외 구간 정보

    Returns:
        필터링된 후보 리스트
    """
    if zones.detection_method == "none":
        return candidates

    filtered = []
    removed_count = 0

    for moment in candidates:
        start = moment.get("start_sec", 0.0)
        end = moment.get("end_sec", 0.0)
        midpoint = (start + end) / 2.0

        # 인트로 구간이거나 크레딧 구간이면 제거
        if midpoint < zones.intro_end_sec or midpoint >= zones.credits_start_sec:
            removed_count += 1
            continue
        filtered.append(moment)

    if removed_count > 0:
        print(f"  [포스트필터] {removed_count}개 모멘트 제외 (인트로/크레딧 구간)")

    return filtered


def print_exclusion_summary(zones: ExclusionZones, duration_sec: float) -> None:
    """제외 구간 요약 출력."""
    effective = zones.credits_start_sec - zones.intro_end_sec
    removed = duration_sec - effective

    print(f"  - 감지 방식: {zones.detection_method}")
    print(f"  - 인트로 종료: {zones.intro_end_sec:.1f}초")
    print(f"  - 크레딧 시작: {zones.credits_start_sec:.1f}초")
    print(f"  - 유효 구간: {effective:.1f}초 (원본 {duration_sec:.1f}초에서 {removed:.1f}초 제외)")
    print(f"  - 감지 신뢰도: {zones.confidence:.0%}")
