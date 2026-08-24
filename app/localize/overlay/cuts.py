"""구간 잘라내기(cuts) — E9 잔망루피 편집 동등화의 순수 로직.

p_edits 최상위 선택 키 `cuts: [{start_sec, end_sec}]`(오케스트레이터 0038 확장이 정본).
좌표는 **완성본(현지화 영상) 시간축** 초 — 편집실 타임라인·ko_ja_pairs 의 start/end
와 같은 축. 그 구간을 영상에서 들어내면 앞뒤가 이어 붙는다.

타임라인 재정렬이 본체: 컷 뒤쪽의 모든 시각이 삭제 길이만큼 당겨진다.
컷 창에 걸친 항목 규칙 — 완전히 안이면 그 줄 제외(use:false 와 동일 의미),
걸치면 경계로 클램프. 두 규칙 모두 `shift_time`(단조 매핑) 하나로 나온다:
안쪽 시각은 컷 시작점으로 클램프되므로, 완전히 안인 구간은 길이 0 → 제외.

여기는 순수(테스트 대상) — 실제 영상 컷은 engine/common.cut_video(ffmpeg).
"""
from __future__ import annotations

from typing import Any, Optional

MAX_CUTS = 20            # 실수 방지 상한 (발주 규칙)
MAX_TOTAL_FRAC = 0.8     # 총 삭제 길이가 원본의 80% 이상이면 거절 (발주 규칙)
_EPS = 1e-6              # 컷 후 길이가 사실상 0인 구간의 판정 여유


def validate_cuts(raw: Any, duration: Optional[float] = None) -> list[dict[str, float]]:
    """cuts 오버라이드 검증 → start_sec 오름차순 정규화 사본. 위반은 ValueError(즉시 실패).

    규칙: start_sec≥0 · end_sec>start_sec · 서로 겹침 거절 · 최대 20개 ·
    duration 을 알면 총 삭제 길이 ≥ 원본의 80% 거절. None/빈 목록 → [].
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"cuts 는 목록이어야 합니다: {raw!r}")
    if len(raw) > MAX_CUTS:
        raise ValueError(f"cuts 는 최대 {MAX_CUTS}개: {len(raw)}개")
    out: list[dict[str, float]] = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise ValueError(f"cuts[{i}] 는 객체여야 합니다: {c!r}")
        vals: list[float] = []
        for key in ("start_sec", "end_sec"):
            v = c.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"cuts[{i}].{key} 는 숫자(초): {v!r}")
            vals.append(float(v))
        start, end = vals
        if start < 0:
            raise ValueError(f"cuts[{i}].start_sec 는 0 이상: {start!r}")
        if end <= start:
            raise ValueError(f"cuts[{i}].end_sec({end}) 는 start_sec({start}) 보다 커야 합니다")
        out.append({"start_sec": start, "end_sec": end})
    out.sort(key=lambda c: c["start_sec"])
    for prev, cur in zip(out, out[1:]):
        if cur["start_sec"] < prev["end_sec"]:
            raise ValueError(
                f"cuts 겹침 거절: [{prev['start_sec']}, {prev['end_sec']}] 와 "
                f"[{cur['start_sec']}, {cur['end_sec']}]")
    if duration and duration > 0:
        total = cut_total(out)
        if total >= MAX_TOTAL_FRAC * duration:
            raise ValueError(
                f"총 삭제 {total:.1f}s 가 원본 {duration:.1f}s 의 "
                f"{MAX_TOTAL_FRAC:.0%} 이상 — 실수로 판단해 거절")
    return out


def cut_total(cuts: list[dict[str, float]]) -> float:
    """총 삭제 길이(초). 검증된(겹침 없는) cuts 전제."""
    return sum(c["end_sec"] - c["start_sec"] for c in cuts)


def shift_time(t: float, cuts: list[dict[str, float]]) -> float:
    """원본 시간축 t → 컷 후 시간축(단조 매핑). 검증된 cuts(정렬·비겹침) 전제.

    컷보다 앞 = 그대로, 뒤 = 삭제 길이만큼 당김, 컷 안 = 컷 시작점으로 클램프.
    """
    removed = 0.0
    for c in cuts:
        if t <= c["start_sec"]:
            break
        removed += min(t, c["end_sec"]) - c["start_sec"]
    return t - removed


def apply_cuts_to_events(events: list[dict[str, Any]], cuts: list[dict[str, float]],
                         start_key: str = "start", end_key: str = "end",
                         ) -> tuple[list[dict[str, Any]], int]:
    """이벤트(start/end 를 가진 dict 목록)에 cuts 를 반영한 사본과 제외 수를 반환.

    각 이벤트의 start/end 를 shift_time 으로 당긴다 — 걸친 구간은 자동으로 경계
    클램프가 된다. 당긴 길이가 사실상 0(완전히 컷 안)이면 `use=False` 로 표시만
    한다(E6-0 소프트 삭제와 동일 의미) — 실제 제외는 use:false 와 같은 지점에서
    호출부가 한다(합성·자막·pairs 가 한 곳에서 함께 빠지는 기존 구도 유지).
    이미 use=False 인 이벤트는 시각만 당긴다(어차피 빠질 줄). 단조 매핑이라
    이벤트 순서는 유지된다. 순수 — 테스트 대상.
    """
    if not cuts:
        return [dict(e) for e in events], 0
    out: list[dict[str, Any]] = []
    n_dropped = 0
    for e in events:
        e = dict(e)
        ns = shift_time(float(e[start_key]), cuts)
        ne = shift_time(float(e[end_key]), cuts)
        if ne - ns <= _EPS:                      # 완전히 컷 안 — use:false 와 동일 의미
            if e.get("use") is not False:
                e["use"] = False
                n_dropped += 1
        e[start_key] = round(ns, 3)
        e[end_key] = round(max(ne, ns), 3)
        out.append(e)
    return out, n_dropped


def keep_segments(cuts: list[dict[str, float]], duration: float) -> list[tuple[float, Optional[float]]]:
    """cuts 의 여집합 — 영상에서 살아남는 (start, end) 구간 목록(ffmpeg trim 입력).

    duration 을 넘는 컷 끝은 duration 으로 클램프. 마지막 구간이 영상 끝까지면
    end=None(트림 없이 끝까지). 검증된 cuts 전제. 순수 — 테스트 대상.
    """
    keeps: list[tuple[float, Optional[float]]] = []
    pos = 0.0
    for c in cuts:
        start = min(c["start_sec"], duration)
        if start - pos > _EPS:
            keeps.append((pos, start))
        pos = max(pos, min(c["end_sec"], duration))
    if duration - pos > _EPS:
        keeps.append((pos, None))
    return keeps
