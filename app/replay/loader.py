"""아카이브 로더 — 두 레이아웃을 자동 판별해 같은 RunRecord dict 목록으로 만든다.

① 스냅샷 레이아웃 (`fetch` 가 만든다):
     <archive>/manifest.json
     <archive>/runs/<key>.json      — {clip_id, run_id, created_at, git_sha,
                                       checkpoint_story, edit_plan, run_log, human_edited}
     <archive>/baselines.json       — 사람 기준선(선택)
     <archive>/bundles/<run_id>/    — 2차 자료(선택): subtitle_segments.json ·
                                       checkpoint_gemini.json · checkpoint_silence_cut.json
② job 디렉토리 레이아웃 (엔진 산출 그대로 — v3 산출 디렉토리도 이 모양):
     <root>/<job_dir>/edit_plan.json (+ edit_plan_{n}.json — v4 승인 2위 이하 ·
                                        checkpoint_story.json · run_log.json ·
                                        subtitle_segments.json · checkpoint_gemini.json …)
     <root> 자체가 job 디렉토리 하나여도 된다(edit_plan.json 직접 보유).

⚠ 읽기만 한다 — 아카이브를 절대 고치지 않는다. 깨진 JSON 은 그 편만 skipped 로
남기고 계속 간다(한 편 때문에 614편 집계가 죽으면 안 된다). 몇 편이 왜 빠졌는지는
metrics.aggregate 의 skipped_keys 로 드러난다.
"""
from __future__ import annotations

import json
from pathlib import Path


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _bundle_from_dir(d: Path) -> dict:
    """번들 디렉토리(스냅샷 bundles/<run_id>/ 또는 job 디렉토리 자체) → bundle dict."""
    out: dict = {}
    f = d / "subtitle_segments.json"
    if f.exists():
        try:
            segs = _read_json(f)
            if isinstance(segs, list) and segs:
                out["subtitle_segments"] = segs
        except (json.JSONDecodeError, OSError):
            pass
    f = d / "checkpoint_gemini.json"
    if f.exists():
        try:
            g = _read_json(f)
            cands = g.get("all_candidates") if isinstance(g, dict) else None
            if cands:
                out["all_candidates"] = cands
        except (json.JSONDecodeError, OSError):
            pass
    f = d / "checkpoint_silence_cut.json"
    if f.exists():
        try:
            sc = _read_json(f)
            variants = sc.get("variants") if isinstance(sc, dict) else None
            if variants:
                out["silence_variants"] = variants
        except (json.JSONDecodeError, OSError):
            pass
    return out


def _record_from_snapshot(path: Path, bundles_dir: Path) -> dict:
    row = _read_json(path)
    cs = row.get("checkpoint_story") or {}
    ep = row.get("edit_plan") or {}
    rl = row.get("run_log") or {}
    run_id = row.get("run_id") or rl.get("job_id") or path.stem
    rec = {
        "key": row.get("clip_id") or path.stem,
        "run_id": run_id,
        "created_at": row.get("created_at", ""),
        "git_sha": ((rl.get("provenance") or {}).get("git_sha")
                    or row.get("git_sha") or ""),
        "story_clips": cs.get("clips") or [],
        "raw_response": cs.get("raw_response") or {},
        "timeline": ep.get("timeline") or [],
        "config": ((rl.get("provenance") or {}).get("config") or {}).get("app") or {},
        "human_edited": bool(row.get("human_edited")),
        "pipeline": _pipeline_marker(rl),
        "approved_rank": 1,
    }
    bd = bundles_dir / str(run_id)
    if bd.is_dir():
        bundle = _bundle_from_dir(bd)
        if bundle:
            rec["bundle"] = bundle
    return rec


def _pipeline_marker(rl: dict) -> str:
    """run_log 의 파이프라인 마커 — v1 은 없고(""), v3·v4 는 자기 이름을 싣는다.

    §3 분포를 파이프라인별로 쪼개 보려면 이 값이 있어야 한다. 없는 편(v1)은 빈
    문자열이라 종전 집계와 같은 자리에 남는다(회귀 0)."""
    return str(rl.get("pipeline") or "")


def _v4_variant_clips(cs: dict, rank: int) -> list[dict]:
    """v4 승인 rank(1-based)의 원안 구간 — checkpoint_story.variants[rank-1].clips.

    v4 는 결함 게이트를 통과한 편을 전부 낸다. 그 편들의 원안은 v1 모양
    `variants[]` 에 그대로 실린다(계약 표 — v1 render 재개가 읽는 모양). 1위는
    하위 호환 키 `clips` 와 같은 것이라 여기서도 같은 값이 나온다."""
    vs = cs.get("variants")
    if isinstance(vs, list) and 1 <= rank <= len(vs):
        v = vs[rank - 1]
        if isinstance(v, dict):
            return v.get("clips") or []
    return []


def _v4_approved_records(d: Path, base: dict, cs: dict) -> list[dict]:
    """v4 승인 2위 이하(`edit_plan_{n}.json`) → 추가 기록. additive.

    한 job 에 최종본이 여럿인 것이 v4 의 성질이다(1위 `edit_plan.json` = base,
    2위↓ `edit_plan_2.json`…). 이걸 안 읽으면 분포 집계가 1위만 세어 승인 편수를
    과소 보고한다 — v3 편이 "집계 0"으로 빠졌던 것과 같은 부류의 구멍이다.

    ⚠ 숫자 접미만 본다. v3 훅 변형은 `edit_plan_variant_{k}.json` 이라 여기 걸리지
    않는다(그건 본편 불변의 훅 교체이고 승인 편이 아니다)."""
    out: list[dict] = []
    for f in sorted(d.glob("edit_plan_*.json")):
        stem = f.stem[len("edit_plan_"):]
        if not stem.isdigit():
            continue
        try:
            ep = _read_json(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(ep, dict):
            continue
        rank = int(stem)
        out.append({**base,
                    "key": f"{base['key']}#{rank}",
                    "approved_rank": rank,
                    "story_clips": _v4_variant_clips(cs, rank) or base.get("story_clips") or [],
                    "timeline": ep.get("timeline") or []})
    return out


def _v3_story_clips(cs: dict) -> list[dict]:
    """v3 checkpoint_story({fingerprint, story:{beats}}) → 원안 구간 목록(additive).

    v1 의 "원안(checkpoint_story.clips) → 최종(edit_plan.timeline)" 대응이 v3 에선
    "비트 편성 → 클립"이다 — 비트를 원안으로 읽어야 §3 분포 집계(usable)가 v3 편에도
    붙는다("집계 0" 리뷰 재현 수정). 시각 문자열은 초로 환산해 clip_span 이 읽는
    start_sec/end_sec 로 싣는다."""
    beats = ((cs.get("story") or {}).get("beats")) or []
    out: list[dict] = []
    for b in beats:
        tm = b.get("time") or {}
        try:
            from app.v3.schemas import parse_ts
            s, e = parse_ts(tm.get("start")), parse_ts(tm.get("end"))
        except (ValueError, TypeError):
            continue
        out.append({"start_sec": round(s, 3), "end_sec": round(e, 3),
                    "role": b.get("role"), "span_ids": b.get("span_ids") or []})
    return out


def _record_from_jobdir(d: Path) -> tuple[dict, dict] | None:
    """job 디렉토리 → (1위 기록, checkpoint_story). cs 는 v4 승인 편 확장에 쓰인다."""
    ep_path = d / "edit_plan.json"
    if not ep_path.exists():
        return None
    ep = _read_json(ep_path)
    cs: dict = {}
    cs_path = d / "checkpoint_story.json"
    if cs_path.exists():
        try:
            cs = _read_json(cs_path)
        except (json.JSONDecodeError, OSError):
            cs = {}
    rl: dict = {}
    rl_path = d / "run_log.json"
    if rl_path.exists():
        try:
            rl = _read_json(rl_path)
        except (json.JSONDecodeError, OSError):
            rl = {}
    rec = {
        "key": d.name,
        "run_id": rl.get("job_id") or d.name,
        "created_at": "",
        "git_sha": (rl.get("provenance") or {}).get("git_sha") or "",
        "story_clips": cs.get("clips") or _v3_story_clips(cs),
        "raw_response": cs.get("raw_response") or {},
        "timeline": (ep.get("timeline") or []) if isinstance(ep, dict) else [],
        "config": ((rl.get("provenance") or {}).get("config") or {}).get("app") or {},
        "human_edited": (d / "edit_overrides.json").exists(),
        "pipeline": _pipeline_marker(rl),
        "approved_rank": 1,
    }
    bundle = _bundle_from_dir(d)
    if bundle:
        rec["bundle"] = bundle
    return rec, cs


def load_archive(root: str | Path) -> dict:
    """아카이브 로드 → {"layout", "records", "baselines", "broken"}.

    records 는 key 오름차순 — 같은 아카이브면 언제나 같은 순서(결정성)."""
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"아카이브 없음: {root}")

    records: list[dict] = []
    broken: list[str] = []
    baselines: list[dict] = []

    runs_dir = root / "runs"
    if runs_dir.is_dir():
        layout = "snapshot"
        bundles_dir = root / "bundles"
        for f in sorted(runs_dir.glob("*.json")):
            try:
                records.append(_record_from_snapshot(f, bundles_dir))
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                broken.append(f"{f.name}: {type(e).__name__}: {e}")
        bl = root / "baselines.json"
        if bl.exists():
            try:
                baselines = _read_json(bl)
            except (json.JSONDecodeError, OSError) as e:
                broken.append(f"baselines.json: {type(e).__name__}: {e}")
    else:
        layout = "jobdir"
        candidates = [root] if (root / "edit_plan.json").exists() else sorted(
            d for d in root.iterdir() if d.is_dir())
        for d in candidates:
            try:
                got = _record_from_jobdir(d)
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                broken.append(f"{d.name}: {type(e).__name__}: {e}")
                continue
            if got is not None:
                rec, _cs = got
                records.append(rec)
                # v4 는 승인 편을 전부 낸다 — 2위 이하도 같은 집계에 넣는다
                records.extend(_v4_approved_records(d, rec, _cs))
        if not records and not broken:
            # v3 초기 마일스톤(M1)의 job 디렉토리는 grid.json/run_log.json 만 있고
            # edit_plan.json 이 아직 없다 — 레이아웃은 인식하되 기록 0건으로 로드한다
            # (M2+ 에서 edit_plan 이 생기면 그대로 기록이 붙는다).
            # v4 는 후보 깔때기까지 돌고 편성이 실패하면 edit_plan 이 없다 —
            # checkpoint_candidates.json 이 그 편의 존재 증거다(레이아웃은 인식).
            v3_markers = any(
                (d / "run_log.json").exists() or (d / "grid.json").exists()
                or (d / "checkpoint_candidates.json").exists()
                for d in candidates)
            if not v3_markers:
                raise FileNotFoundError(
                    f"아카이브 레이아웃을 인식할 수 없습니다: {root} — "
                    "runs/*.json (스냅샷) 또는 <dir>/edit_plan.json (job 디렉토리) 필요")

    records.sort(key=lambda r: str(r.get("key", "")))
    return {"layout": layout, "records": records, "baselines": baselines, "broken": broken}
