"""아카이브 로더 — 두 레이아웃을 자동 판별해 같은 RunRecord dict 목록으로 만든다.

① 스냅샷 레이아웃 (`fetch` 가 만든다):
     <archive>/manifest.json
     <archive>/runs/<key>.json      — {clip_id, run_id, created_at, git_sha,
                                       checkpoint_story, edit_plan, run_log, human_edited}
     <archive>/baselines.json       — 사람 기준선(선택)
     <archive>/bundles/<run_id>/    — 2차 자료(선택): subtitle_segments.json ·
                                       checkpoint_gemini.json · checkpoint_silence_cut.json
② job 디렉토리 레이아웃 (엔진 산출 그대로 — v3 산출 디렉토리도 이 모양):
     <root>/<job_dir>/edit_plan.json (+ checkpoint_story.json · run_log.json ·
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
    }
    bd = bundles_dir / str(run_id)
    if bd.is_dir():
        bundle = _bundle_from_dir(bd)
        if bundle:
            rec["bundle"] = bundle
    return rec


def _record_from_jobdir(d: Path) -> dict | None:
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
        "story_clips": cs.get("clips") or [],
        "raw_response": cs.get("raw_response") or {},
        "timeline": (ep.get("timeline") or []) if isinstance(ep, dict) else [],
        "config": ((rl.get("provenance") or {}).get("config") or {}).get("app") or {},
        "human_edited": (d / "edit_overrides.json").exists(),
    }
    bundle = _bundle_from_dir(d)
    if bundle:
        rec["bundle"] = bundle
    return rec


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
                rec = _record_from_jobdir(d)
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                broken.append(f"{d.name}: {type(e).__name__}: {e}")
                continue
            if rec is not None:
                records.append(rec)
        if not records and not broken:
            # v3 초기 마일스톤(M1)의 job 디렉토리는 grid.json/run_log.json 만 있고
            # edit_plan.json 이 아직 없다 — 레이아웃은 인식하되 기록 0건으로 로드한다
            # (M2+ 에서 edit_plan 이 생기면 그대로 기록이 붙는다).
            v3_markers = any(
                (d / "run_log.json").exists() or (d / "grid.json").exists()
                for d in candidates)
            if not v3_markers:
                raise FileNotFoundError(
                    f"아카이브 레이아웃을 인식할 수 없습니다: {root} — "
                    "runs/*.json (스냅샷) 또는 <dir>/edit_plan.json (job 디렉토리) 필요")

    records.sort(key=lambda r: str(r.get("key", "")))
    return {"layout": layout, "records": records, "baselines": baselines, "broken": broken}
