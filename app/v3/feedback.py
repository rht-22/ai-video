"""M6-B — 사후 학습 루프의 조인·집계(순수 로직 — scripts/feedback_report.py 가 IO).

데이터 소스 규율(발주서 v3-m6): laeebly `youtube_studio` 가 정본이지만 **조인은
관제 미러(`perf_studio_daily`)를 읽는다** — laeebly 직접 질의 금지(읽기 전용·
파티션 프루닝 규율). 산출물 쪽 정본은 `clip_metadata.publish_snippet`(발행 제목·
발행 시각·채널)이다.

매칭 규칙(실측 분해 2026-08-31 로 확정):
  주 키 = **정규화 제목**(공백 제거) 일치, 같은 제목이 여럿이면 publish_time 최근접.
  시각 창 ±14일 — private 업로드 후 공개 전환으로 publish_time 이 이틀 넘게
  벌어지는 실사례 때문에 ±48h 는 좁다(실측: 성숙 미매칭 85건 중 30건이 창 밖).
분모의 정직(완료 조건 95%+ 의 해석):
  전체 발행분에는 구조적 미매칭이 섞인다 — ① immature(발행 후 며칠, 깔때기 미유입)
  ② beyond_horizon(미러 최신 stat 이후 발행 — perf_sync 지연 실측: 8일 낡은 미러)
  ③ absent(깔때기에 영영 안 잡힘 — 비공개 유지·노출 0·삭제). 리포트는 세 분모
  (전체 / 성숙 / matchable=지평선 안)를 모두 찍는다 — 지표는 matchable 기준.

지표 집계(perf_studio_daily 주석 규율): 그날치 **증분**이므로 생애 지표는 날짜별
합, 비율(ctr·kept·view_pct)은 노출·조회 **가중 평균**. ⚠ 집계 지표라 초 단위 이탈
곡선 없음 — 변형 간 상대 비교로만 읽는다. 프리셋 자동 보정 없음(승인 게이트 뒤).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

MATCH_WINDOW_DAYS = 14
MATURE_DAYS = 4          # 발행 후 이만큼 지나야 깔때기 유입을 기대한다(실측)


def _norm_title(s: str) -> str:
    return "".join(str(s or "").split())


def _parse_ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def match_clips(clips: list[dict], perf_rows: list[dict], *,
                now: datetime) -> dict:
    """산출물(발행 스니펫) × 미러 → 매칭·분해. 순수.

    clips: [{clip_id, ai_video_run_id, title, published_at(datetime), variant_id?}]
    perf_rows: perf_studio_daily 행 목록(video_title·publish_time·content_id·…)."""
    by_title: dict[str, list[dict]] = {}
    horizon = None
    for r in perf_rows:
        pt = _parse_ts(r.get("publish_time"))
        if pt and (horizon is None or pt > horizon):
            horizon = pt
        by_title.setdefault(_norm_title(r.get("video_title")), []).append(r)

    matched, immature, beyond, window_miss, absent = [], [], [], [], []
    for c in clips:
        pub = c.get("published_at")
        if pub is None:
            absent.append(c)
            continue
        cands = by_title.get(_norm_title(c.get("title"))) or []
        best, best_gap = None, None
        for r in cands:
            pt = _parse_ts(r.get("publish_time"))
            if pt is None:
                continue
            gap = abs((pt - pub).total_seconds())
            if best_gap is None or gap < best_gap:
                best, best_gap = r, gap
        if best is not None and best_gap is not None \
                and best_gap <= MATCH_WINDOW_DAYS * 86400:
            matched.append({**c, "content_id": best.get("content_id"),
                            "channel_id": best.get("channel_id"),
                            "publish_gap_hours": round(best_gap / 3600, 1)})
        elif pub > now - timedelta(days=MATURE_DAYS):
            immature.append(c)
        elif horizon is not None and pub > horizon:
            beyond.append(c)
        elif cands:
            window_miss.append(c)      # 제목은 있는데 ±14일 밖 — 재업로드/장기 전환
        else:
            absent.append(c)

    def rate(num: int, den: int) -> float | None:
        return round(100.0 * num / den, 1) if den else None

    n_m = len(matched)
    total = len(clips)
    mature = total - len(immature)
    matchable = mature - len(beyond)
    return {"matched": matched, "immature": immature, "beyond_horizon": beyond,
            "window_miss": window_miss, "absent": absent,
            "rates": {"all": rate(n_m, total),
                      "mature": rate(n_m, mature),
                      "matchable": rate(n_m, matchable)},
            "counts": {"total": total, "matched": n_m, "immature": len(immature),
                       "beyond_horizon": len(beyond),
                       "window_miss": len(window_miss), "absent": len(absent)},
            "mirror_horizon": horizon.isoformat() if horizon else None}


def aggregate_video(rows: list[dict]) -> dict:
    """한 영상의 일별 증분 행 → 생애 지표. 합=views 류 · 가중 평균=비율 류."""
    def num(r: dict, k: str) -> float:
        v = r.get(k)
        return float(v) if isinstance(v, (int, float)) else 0.0

    views = sum(num(r, "views") for r in rows)
    imps = sum(num(r, "impressions") for r in rows)
    out = {
        "days": len(rows),
        "views": int(views),
        "impressions": int(imps),
        "watch_hours": round(sum(num(r, "watch_hours") for r in rows), 2),
        "likes": int(sum(num(r, "likes") for r in rows)),
        "shares": int(sum(num(r, "shares") for r in rows)),
        "comments": int(sum(num(r, "comments") for r in rows)),
    }
    out["ctr"] = round(sum(num(r, "ctr") * num(r, "impressions") for r in rows)
                       / imps, 2) if imps else None
    for k in ("kept_rate", "view_pct"):
        out[k] = round(sum(num(r, k) * num(r, "views") for r in rows)
                       / views, 2) if views else None
    return out


def variant_groups(matched: list[dict]) -> dict[str, list[dict]]:
    """변형 비교 골격 — variant_id 별 묶음(§9-C: 변형 간 상대 비교).

    v3 변형 발행이 쌓이기 전에는 빈 dict — 리포트가 '데이터 없음'을 명시한다."""
    groups: dict[str, list[dict]] = {}
    for c in matched:
        vid = c.get("variant_id")
        if vid:
            groups.setdefault(str(vid), []).append(c)
    return groups
