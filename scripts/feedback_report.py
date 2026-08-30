"""M6-B — feedback 조인 배치(비동기 · 읽기 전용). 발주서 orders/v3-m6-feedback-loop.md.

  python -m scripts.feedback_report --env-file ~/dev/all.env \\
      [--channel "커리어데이 숏츠"] [--weeks 4] --out <dir>

관제 미러(PostgREST — replay_harness fetch 와 같은 자격·같은 클라이언트)만 읽는다.
산출: matching.json(매칭·사유 분해) + report.md(채널 N주 성과 + 변형 비교 골격).
프리셋 자동 보정은 하지 않는다 — 사람 승인 게이트 뒤(기획 §9-C 고정).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.replay.fetch import _Client, load_env_file
from app.v3.feedback import (
    _parse_ts,
    aggregate_video,
    match_clips,
    variant_groups,
)


def fetch_published_clips(client: _Client) -> list[dict]:
    rows = client.rows("clip_metadata", [
        ("select", "clip_id,ai_video_run_id,publish_snippet,edit_plan"),
        ("publish_snippet", "not.is.null"),
    ])
    out = []
    for r in rows:
        sn = r.get("publish_snippet") or {}
        ep = r.get("edit_plan") or {}
        out.append({
            "clip_id": r.get("clip_id"),
            "ai_video_run_id": r.get("ai_video_run_id"),
            "title": sn.get("title") or "",
            "channel": sn.get("channel") or "",
            "published_at": _parse_ts(sn.get("published_at")),
            "variant_id": ep.get("variant_id") if isinstance(ep, dict) else None,
        })
    return out


def fetch_perf_rows(client: _Client) -> list[dict]:
    return client.rows("perf_studio_daily", [
        ("select", "content_id,channel_id,video_title,publish_time,stat_date,"
                   "impressions,ctr,views,view_pct,kept_rate,watch_hours,"
                   "likes,shares,comments"),
    ])


def build_report(channel: str, weeks: int, match: dict,
                 perf_by_content: dict[str, list[dict]],
                 now: datetime) -> str:
    since = now - timedelta(weeks=weeks)
    rows = []
    for c in match["matched"]:
        if channel and c.get("channel") != channel:
            continue
        if c.get("published_at") and c["published_at"] < since:
            continue
        agg = aggregate_video(perf_by_content.get(c.get("content_id")) or [])
        rows.append((c, agg))
    rows.sort(key=lambda x: x[0].get("published_at") or now, reverse=True)

    lines = [f"# feedback 리포트 — {channel or '전 채널'} · 최근 {weeks}주",
             f"기준 시각 {now.date()} · 미러 지평선 {match['mirror_horizon']}",
             "",
             "## 매칭률 (§9-C 완료 조건)",
             "| 분모 | 매칭률 |", "|---|---|",
             f"| 전체 발행분 {match['counts']['total']}편 | {match['rates']['all']}% |",
             f"| 성숙분(발행 {4}일+) | {match['rates']['mature']}% |",
             f"| **matchable(미러 지평선 안)** | **{match['rates']['matchable']}%** |",
             "",
             f"사유 분해: immature {match['counts']['immature']} · "
             f"beyond_horizon {match['counts']['beyond_horizon']}"
             f"(미러 동기화 지연 — perf_sync 점검 신호) · "
             f"window_miss {match['counts']['window_miss']}"
             f"(±{14}일 밖 — 장기 비공개 전환/재업로드) · "
             f"absent {match['counts']['absent']}(깔때기 미유입 — 비공개 유지·노출 0·삭제)",
             "",
             f"## 편별 성과 ({len(rows)}편 · 생애 합/가중평균)",
             "| 발행일 | 제목 | views | kept% | 완주% | CTR% | likes |",
             "|---|---|---|---|---|---|---|"]
    for c, a in rows:
        d = c["published_at"].date() if c.get("published_at") else "?"
        title = (c["title"][:28] + "…") if len(c["title"]) > 29 else c["title"]
        lines.append(f"| {d} | {title} | {a['views']} | {a['kept_rate']} | "
                     f"{a['view_pct']} | {a['ctr']} | {a['likes']} |")

    def med(vals):
        vals = sorted(v for v in vals if v is not None)
        return vals[len(vals) // 2] if vals else None

    lines += ["",
              "## 분포(중앙값)",
              f"views {med([a['views'] for _, a in rows])} · "
              f"kept {med([a['kept_rate'] for _, a in rows])}% · "
              f"완주 {med([a['view_pct'] for _, a in rows])}% · "
              f"CTR {med([a['ctr'] for _, a in rows])}%",
              "",
              "## 훅 변형 비교 (§9-C)"]
    groups = variant_groups(match["matched"])
    if groups:
        for vid, cs in sorted(groups.items()):
            aggs = [aggregate_video(perf_by_content.get(c.get("content_id")) or [])
                    for c in cs]
            lines.append(f"- {vid}: {len(cs)}편 · kept 중앙 "
                         f"{med([a['kept_rate'] for a in aggs])}%")
    else:
        lines.append("- 변형 데이터 없음 — v3 훅 변형 발행이 쌓이면 여기서 "
                     "kept_watching_rate 상대 비교(골격 준비 완료).")
    lines += ["",
              "⚠ 한계(기획 §9-C 명시): 집계 지표라 초 단위 이탈 곡선 없음 — 변형 간 "
              "상대 비교·지표 조합으로만 판단. 프리셋 자동 보정 없음(사람 승인 게이트 뒤).",
              ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="feedback_report")
    p.add_argument("--env-file", default=None)
    p.add_argument("--channel", default=None, help="publish_snippet.channel 표기명")
    p.add_argument("--weeks", type=int, default=4)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    env = dict(os.environ)
    if args.env_file:
        env.update(load_env_file(args.env_file))
    client = _Client(env.get("SUPABASE_URL", ""), env.get("SUPABASE_SERVICE_KEY", ""))

    now = datetime.now(timezone.utc)
    clips = fetch_published_clips(client)
    perf = fetch_perf_rows(client)
    print(f"[feedback] 발행 클립 {len(clips)} · 미러 행 {len(perf)}")

    match = match_clips(clips, perf, now=now)
    perf_by_content: dict[str, list[dict]] = {}
    for r in perf:
        perf_by_content.setdefault(str(r.get("content_id")), []).append(r)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    serializable = {**match}
    for key in ("matched", "immature", "beyond_horizon", "window_miss", "absent"):
        serializable[key] = [
            {**c, "published_at": c["published_at"].isoformat()
             if c.get("published_at") else None} for c in match[key]]
    (out / "matching.json").write_text(
        json.dumps(serializable, ensure_ascii=False, indent=1), encoding="utf-8")
    report = build_report(args.channel or "", args.weeks, match,
                          perf_by_content, now)
    (out / "report.md").write_text(report, encoding="utf-8")
    print(f"[feedback] 매칭률 전체 {match['rates']['all']}% · 성숙 "
          f"{match['rates']['mature']}% · matchable {match['rates']['matchable']}% "
          f"→ {out}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
