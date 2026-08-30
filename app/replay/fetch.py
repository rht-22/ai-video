"""아카이브 스냅샷 — 오케스트레이터 DB/Storage → 로컬 디렉토리(표준 라이브러리만).

이 레포 venv 에 DB 드라이버가 없으므로 PostgREST(HTTP)로 읽는다 — psycopg 불필요.
필요 env: SUPABASE_URL · SUPABASE_SERVICE_KEY (없으면 즉시 실패 — 조용한 폴백 금지).

읽는 것(전부 SELECT — 쓰기 없음):
  clip_metadata      편 정본(checkpoint_story·edit_plan·run_log 요약) — §3의 614편이 이 표
  editor_baselines   AI 원안 타임라인 스냅샷(사람 기준선의 AI 쪽)
  job_queue          generate 잡의 params.edit_overrides(사람 기준선의 사람 쪽)
  ves-runs Storage   (--bundles) 런 번들의 2차 자료 3종 — 8/13 이후 런에만 존재

산출 레이아웃은 loader.load_archive 의 ① 스냅샷 레이아웃.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PAGE = 100                    # clip_metadata 행이 커서(수십 KB) 페이지를 작게 잡는다
BUNDLE_FILES = ("subtitle_segments.json", "checkpoint_gemini.json",
                "checkpoint_silence_cut.json")


def load_env_file(path: str | Path) -> dict[str, str]:
    """KEY=VALUE 파일(all.env 류) → dict. export·주석·빈 줄 관용."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def storage_bundle_prefix(run_id: str) -> str:
    """run_id → Storage 키 접두사. ves 어댑터 base.storage_key 와 같은 규칙
    (sha256 16자 — 한글 run_id 의 ASCII 화). 갈리면 번들을 못 찾으므로 테스트가 고정한다."""
    return hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:16]


class _Client:
    def __init__(self, base_url: str, service_key: str):
        if not (base_url and service_key):
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY 필요 — "
                               "--env-file 로 넘기거나 환경변수로 설정하세요")
        self.base = base_url.rstrip("/")
        self.headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}

    def _get(self, url: str, retries: int = 2) -> bytes | None:
        req = urllib.request.Request(url, headers=self.headers)
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):
                    return None            # 없는 오브젝트 — 번들 미존재는 정상 상태
                if e.code in (429,) or e.code >= 500:
                    if attempt < retries:
                        time.sleep(2 * (attempt + 1))
                        continue
                raise
            except urllib.error.URLError:
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        return None

    def rows(self, table: str, params: list[tuple[str, str]]) -> list[dict]:
        """PostgREST SELECT — limit/offset 페이지네이션으로 전량."""
        out: list[dict] = []
        offset = 0
        while True:
            q = params + [("limit", str(PAGE)), ("offset", str(offset))]
            url = f"{self.base}/rest/v1/{table}?" + urllib.parse.urlencode(q)
            body = self._get(url)
            if body is None:
                raise RuntimeError(f"PostgREST {table} 조회 실패(4xx): {url}")
            batch = json.loads(body)
            out.extend(batch)
            if len(batch) < PAGE:
                return out
            offset += PAGE

    def storage_object(self, bucket: str, key: str) -> bytes | None:
        return self._get(f"{self.base}/storage/v1/object/{bucket}/{key}")


def fetch_archive(out_dir: str | Path, *, base_url: str, service_key: str,
                  since: str = "2026-07-15", until: str = "2026-08-25",
                  bundles: bool = False, log=print) -> dict:
    """스냅샷 아카이브 생성 → manifest dict 반환. 이미 있는 파일은 덮어쓴다."""
    out = Path(out_dir)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    cli = _Client(base_url, service_key)

    log(f"[fetch] clip_metadata {since} ~ {until} …")
    rows = cli.rows("clip_metadata", [
        ("select", "clip_id,ai_video_run_id,git_sha,created_at,"
                   "checkpoint_story,edit_plan,run_log"),
        ("created_at", f"gte.{since}"),
        ("created_at", f"lt.{until}"),
        ("order", "created_at.asc,clip_id.asc"),
    ])
    log(f"[fetch] 편 {len(rows)}건")

    log("[fetch] editor_baselines + edit_overrides …")
    base_rows = cli.rows("editor_baselines", [
        ("select", "run_id,work_order_id,timeline,captured_at"),
        ("order", "run_id.asc"),
    ])
    edit_rows = cli.rows("job_queue", [
        ("select", "id,work_order_id,created_at,run_id:params->>run_id,"
                   "edit_overrides:params->edit_overrides"),
        ("kind", "eq.generate"),
        ("params->edit_overrides", "not.is.null"),
        ("created_at", f"lt.{until}"),
        ("order", "created_at.asc,id.asc"),
    ])
    edits_by_run: dict[str, list[dict]] = {}
    for e in edit_rows:
        rid = e.get("run_id") or ""
        if rid:
            edits_by_run.setdefault(rid, []).append({
                "created_at": e.get("created_at", ""),
                "clips": (e.get("edit_overrides") or {}).get("clips") or [],
            })

    run_ids: list[str] = []
    for row in rows:
        rid = row.get("ai_video_run_id") or ""
        run_ids.append(rid)
        doc = {
            "clip_id": str(row.get("clip_id")),
            "run_id": rid,
            "created_at": row.get("created_at", ""),
            "git_sha": row.get("git_sha", ""),
            "human_edited": rid in edits_by_run,
            "checkpoint_story": row.get("checkpoint_story") or {},
            "edit_plan": row.get("edit_plan") or {},
            "run_log": row.get("run_log") or {},
        }
        (out / "runs" / f"{doc['clip_id']}.json").write_text(
            json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    # 사람 기준선 — baseline 이 있는 run + baseline 없이 편집만 있는 run 모두 싣는다
    # (AI 쪽 폴백은 metrics.human_pairs 가 edit_plan 으로 잇는다).
    baselines: list[dict] = []
    base_by_run = {b.get("run_id"): b for b in base_rows}
    for rid in sorted(set(base_by_run) | set(edits_by_run)):
        b = base_by_run.get(rid) or {}
        tl = b.get("timeline") or {}
        baselines.append({
            "run_id": rid,
            "work_order_id": str(b.get("work_order_id") or ""),
            "captured_at": b.get("captured_at", ""),
            "ai_clips": tl.get("clips") or [],
            "human_edits": edits_by_run.get(rid, []),
        })
    (out / "baselines.json").write_text(
        json.dumps(baselines, ensure_ascii=False, sort_keys=True, indent=1),
        encoding="utf-8")

    bundle_hits = 0
    if bundles:
        bdir = out / "bundles"
        for i, rid in enumerate(sorted(set(r for r in run_ids if r))):
            prefix = storage_bundle_prefix(rid)
            got_any = False
            for name in BUNDLE_FILES:
                body = cli.storage_object("ves-runs", f"{prefix}/bundle/{name}")
                if body is None:
                    continue
                d = bdir / rid
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(body)
                got_any = True
            bundle_hits += 1 if got_any else 0
            if (i + 1) % 50 == 0:
                log(f"[fetch] 번들 {i + 1}/{len(set(run_ids))} 확인 (있음 {bundle_hits})")
        log(f"[fetch] 번들 있는 런 {bundle_hits}건")

    manifest = {
        "schema": "replay_archive/v1",
        "source": "clip_metadata(PostgREST) + editor_baselines + job_queue.edit_overrides"
                  + (" + ves-runs bundle" if bundles else ""),
        "window": {"since": since, "until": until},
        "counts": {"runs": len(rows), "baseline_rows": len(baselines),
                   "edited_runs": len(edits_by_run), "bundle_runs": bundle_hits},
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1),
        encoding="utf-8")
    log(f"[fetch] 완료 → {out}")
    return manifest
