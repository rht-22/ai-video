"""격자 스키마 — 시각 표기·Stage 1 산출 구조·스냅/커버리지 검증(순수).

시간 정본 원칙(기획서 §1 — 933초 사고 방어의 계승): LLM 은 경계를 **제안**할 뿐,
확정 시각은 정본 격자(grid)에 스냅되어 기록된다. 이 모듈의 검증기가 그 관문이다.

- 시각 문자열은 기획서 §3 그대로 "HH:MM:SS.mmm". 파서는 관용적으로 읽되(분·초만,
  콤마 밀리초 등) 출력은 항상 이 형식 하나다(결정성).
- 스냅: 경계마다 격자에서 가장 가까운 눈금으로. 오차 > SNAP_TOLERANCE_SEC(2.0)면
  그 경계는 스냅 실패 — 호출자가 반려·재질의한다(조용한 클램프 금지).
- 커버리지: 러닝타임 = sequences ∪ exception_sector, 빈틈·겹침 0(EPS 안에서).
"""
from __future__ import annotations

import math
from typing import Any

SCHEMA_STAGE1 = "v3_stage1/v1"
SNAP_TOLERANCE_SEC = 2.0     # 기획서 §3: 스냅 실패( >2s 오차)는 반려
COVERAGE_EPS_SEC = 0.05      # 스냅 뒤 타일링 잔차 허용(부동소수 · 격자 눈금 자체는 공유됨)
CHUNK_MAX_SEC = 600.0        # chunk ≤ 10분 — 10분은 상한이지 단위가 아니다(기획서 §2)
EXCEPTION_KEYS = ("intro", "recap", "teaser", "credit", "end")  # end/credit 분리 유지(확정)


def format_ts(sec: float) -> str:
    """초 → "HH:MM:SS.mmm". 음수 금지 — 시각은 언제나 러닝타임 안이다."""
    if sec < 0:
        raise ValueError(f"음수 시각: {sec}")
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def parse_ts(value: Any) -> float:
    """"HH:MM:SS.mmm"(관용: MM:SS 도, 콤마 밀리초도) → 초. 숫자면 그대로 초로 읽는다.

    LLM 산출을 읽는 자리라 관용적으로 받되, 못 읽으면 ValueError 로 크게 실패한다 —
    조용히 0 이 되면 커버리지 검증이 엉뚱한 곳을 가리킨다."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # json.loads 는 NaN/Infinity 토큰을 허용한다 — 통과시키면 스냅 dict 재조회에서
        # KeyError: nan 으로 뒤늦게 죽는다(리뷰 재현). 여기서 크게 거른다.
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"시각이 유한한 양수가 아님: {value!r}")
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"시각이 아님: {value!r}")
    t = value.strip().replace(",", ".")
    parts = t.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"시각 형식 오류: {value!r}")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"시각 형식 오류: {value!r}") from e
    if any(n < 0 or not math.isfinite(n) for n in nums):
        raise ValueError(f"시각 성분이 유한한 양수가 아님: {value!r}")
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    return sec


# ── 스냅 ────────────────────────────────────────────────────────────────────

def snap_time(sec: float, grid_times: list[float],
              tolerance: float = SNAP_TOLERANCE_SEC) -> tuple[float | None, float]:
    """격자에서 가장 가까운 눈금으로 스냅 → (스냅된 시각 | None, 오차).

    격자가 비었거나 오차 > tolerance 면 (None, 오차) — 확정 시각을 만들지 않는다.
    동률(양쪽 눈금이 정확히 같은 거리)은 이른 쪽 — 결정성."""
    if not grid_times:
        return None, float("inf")
    best = min(grid_times, key=lambda g: (abs(g - sec), g))
    err = abs(best - sec)
    if err > tolerance:
        return None, err
    return best, err


def snap_boundaries(bounds: list[float], grid_times: list[float],
                    tolerance: float = SNAP_TOLERANCE_SEC) -> tuple[list[float], list[dict]]:
    """경계 목록을 한 번에 스냅 → (스냅된 목록, 실패 목록).

    인접 구간이 공유하는 경계는 호출자가 **한 점으로 합쳐서** 넘겨야 한다 —
    구간별로 각자 스냅하면 같은 경계가 두 눈금으로 갈라져 스냅 자체가 빈틈을 만든다.
    실패 항목: {"boundary_sec", "nearest_err"}. 스냅 뒤 순서가 뒤집히면(격자 눈금이
    성겨서 두 경계가 같은 눈금에 붙는 등) 그 쌍도 실패로 본다."""
    failures: list[dict] = []
    out: list[float] = []
    for b in bounds:
        snapped, err = snap_time(b, grid_times, tolerance)
        if snapped is None:
            failures.append({"boundary_sec": round(b, 3), "nearest_err": round(err, 3)})
            out.append(b)
        else:
            out.append(snapped)
    for i in range(1, len(out)):
        if out[i] <= out[i - 1] and not any(
                f["boundary_sec"] == round(bounds[i], 3) for f in failures):
            failures.append({"boundary_sec": round(bounds[i], 3),
                             "nearest_err": round(out[i - 1] - out[i], 3),
                             "reason": "스냅 후 순서 역전(격자 눈금 부족)"})
    return out, failures


# ── Stage 1 산출 정규화·검증 ────────────────────────────────────────────────

def _range_of(node: Any, where: str) -> tuple[float, float]:
    if not isinstance(node, dict) or "time" not in node:
        raise ValueError(f"{where}: time 없음")
    t = node["time"]
    if not isinstance(t, dict):
        # LLM 이 time 을 문자열·리스트로 내는 모양 — AttributeError 로 재질의 루프를
        # 뚫으면 Pro 호출 비용을 치른 뒤 즉사한다(리뷰 재현). ValueError 로 반려 재료화.
        raise ValueError(f"{where}: time 이 객체가 아님({type(t).__name__}): {t!r}")
    s, e = parse_ts(t.get("start")), parse_ts(t.get("end"))
    if e <= s:
        raise ValueError(f"{where}: 구간 역전 {t}")
    return s, e


def normalize_stage1_response(raw: dict) -> dict:
    """LLM 응답 → 내부 표현(초 단위). 구조가 틀리면 ValueError(재질의 재료).

    반환: {"sequences": [{"content", "start", "end",
                          "chunks": [{"start", "end"}]}],
           "exception_sector": {키: {"start","end"} | None}}"""
    if not isinstance(raw, dict):
        raise ValueError("응답이 JSON 객체가 아님")
    seqs_in = raw.get("sequences")
    if not isinstance(seqs_in, list) or not seqs_in:
        raise ValueError("sequences 가 비어 있음")
    seqs = []
    for i, sq in enumerate(seqs_in):
        s, e = _range_of(sq, f"sequences[{i}]")
        chunks = []
        for j, ch in enumerate(sq.get("chunks") or []):
            cs, ce = _range_of(ch, f"sequences[{i}].chunks[{j}]")
            chunks.append({"start": cs, "end": ce})
        seqs.append({"content": str(sq.get("content") or "").strip(),
                     "start": s, "end": e, "chunks": chunks})
    seqs.sort(key=lambda x: x["start"])

    exc_in = raw.get("exception_sector")
    if not isinstance(exc_in, dict):
        raise ValueError("exception_sector 없음")
    unknown = sorted(set(exc_in) - set(EXCEPTION_KEYS))
    if unknown:
        raise ValueError(f"exception_sector 모르는 키: {unknown}")
    exc: dict[str, dict | None] = {}
    for k in EXCEPTION_KEYS:
        v = exc_in.get(k)
        if v is None:
            exc[k] = None
            continue
        s, e = _range_of({"time": v}, f"exception_sector.{k}")
        exc[k] = {"start": s, "end": e}
    return {"sequences": seqs, "exception_sector": exc}


def validate_coverage(norm: dict, duration_sec: float,
                      eps: float = COVERAGE_EPS_SEC) -> list[str]:
    """커버리지 검증 — 위반 사유 목록(비면 통과).

    러닝타임 = sequences ∪ exception(빈틈·겹침 0) · sequence 당 chunk ≥1 ·
    chunk 는 자기 sequence 를 타일링 · chunk ≤ 10분."""
    problems: list[str] = []
    ranges: list[tuple[float, float, str]] = []
    for i, sq in enumerate(norm["sequences"]):
        ranges.append((sq["start"], sq["end"], f"sequence[{i}]"))
    for k, v in norm["exception_sector"].items():
        if v is not None:
            ranges.append((v["start"], v["end"], f"exception.{k}"))
    ranges.sort(key=lambda r: r[0])

    if not ranges:
        return ["구간이 하나도 없음"]
    if abs(ranges[0][0] - 0.0) > eps:
        problems.append(f"시작 빈틈: 0 ~ {ranges[0][0]:.3f} ({ranges[0][2]} 이전)")
    for (s1, e1, n1), (s2, e2, n2) in zip(ranges, ranges[1:]):
        gap = s2 - e1
        if gap > eps:
            problems.append(f"빈틈 {gap:.3f}s: {n1} 끝({e1:.3f}) ~ {n2} 시작({s2:.3f})")
        elif gap < -eps:
            problems.append(f"겹침 {-gap:.3f}s: {n1}({e1:.3f}) ↔ {n2}({s2:.3f})")
    if abs(ranges[-1][1] - duration_sec) > max(eps, 0.5):
        # 컨테이너 길이와 마지막 눈금은 프레임 잔차가 있어 0.5s 까지 관용
        problems.append(
            f"끝 빈틈: 마지막 {ranges[-1][2]} 끝({ranges[-1][1]:.3f}) ≠ 러닝타임({duration_sec:.3f})")

    for i, sq in enumerate(norm["sequences"]):
        chunks = sq["chunks"]
        if not chunks:
            problems.append(f"sequence[{i}] chunk 0개 (≥1 필요)")
            continue
        cs = sorted(chunks, key=lambda c: c["start"])
        if abs(cs[0]["start"] - sq["start"]) > eps or abs(cs[-1]["end"] - sq["end"]) > eps:
            problems.append(f"sequence[{i}] chunk 가 구간을 안 덮음")
        for (a, b) in zip(cs, cs[1:]):
            if abs(b["start"] - a["end"]) > eps:
                problems.append(f"sequence[{i}] chunk 사이 빈틈/겹침")
                break
        for j, c in enumerate(cs):
            if c["end"] - c["start"] > CHUNK_MAX_SEC + eps:
                problems.append(
                    f"sequence[{i}].chunk[{j}] {c['end'] - c['start']:.1f}s > 10분 상한")
    return problems


def to_stage1_doc(norm: dict) -> dict:
    """내부 표현 → 기획서 §3 스키마 그대로(시각은 HH:MM:SS.mmm · meanings 빈 배열 예약)."""
    seqs = []
    for i, sq in enumerate(norm["sequences"]):
        chunks = [{"number": j,
                   "time": {"start": format_ts(c["start"]), "end": format_ts(c["end"])},
                   "meanings": []}
                  for j, c in enumerate(sorted(sq["chunks"], key=lambda c: c["start"]))]
        seqs.append({"number": i,
                     "time": {"start": format_ts(sq["start"]), "end": format_ts(sq["end"])},
                     "content": sq["content"],
                     "chunks": chunks})
    exc = {}
    for k in EXCEPTION_KEYS:
        v = norm["exception_sector"].get(k)
        exc[k] = None if v is None else {"start": format_ts(v["start"]),
                                         "end": format_ts(v["end"])}
    return {"schema": SCHEMA_STAGE1, "sequences": seqs, "exception_sector": exc}
