"""영상 묶음 — 그리드 리뷰 잡 폴더에서 한 편(vN)에 필요한 것만 `videos/<suffix>/` 로 모은다.

잡 폴더는 공통 캐시(전사·분석·TTS)와 편별 파일(`*_vN.json`, `review_vN/`, `shorts_vN.mp4`)이 섞여 있다.
VES(검수·편집실·업로드)는 "한 편 = 한 폴더"를 본다. 이 모듈은 그 폴더를 만드는 **출구**일 뿐이다:
파이프라인의 캐시·무효화 규칙은 건드리지 않고, 원본 파일도 고치지 않는다(복사·하드링크만).

묶음(`tikitaka_video/v1`):
  video.json                  신분증 — 편 번호·제목·원본·렌더 지문·파일 해시·출처
  shorts.mp4                  완성본 (= 잡 폴더 shorts_<suffix>.mp4)
  edit_plan.json              review_<suffix>/edit_plan.json + input.video_path(원본 절대경로)
  subtitle_segments.json · tts_caption_segments.json · review.json
  checkpoint_resources.json   TTS 경로를 묶음 안 상대경로(tts/<파일>)로 바꾼 것
  tts/*.mp3                   이 편이 실제로 쓴 내레이션만
  grid_table.json             grid_table_guarded_<suffix>.json (없으면 grid_table_<suffix>.json)
  publish.json                publish_<suffix>.json
  v3_*.ass                    구운 자막 — final_1080x1920.filter.txt 가 실제로 쓴 것만(옛 렌더의 남은 파일 제외)
  editor_scan.mp4             편집실 미리보기용 360p 사본(작업당 하나를 복제) — 원본 1080p 는 키프레임 간격이 길어
                              구간이 바뀔 때마다 되감기가 멈춘다(2026-09-23 실측). 원본 시각 그대로(시작 0)·2초 키프레임.
  labels.json                 AI 보조 자막(Stage 4 라벨)·강조·드롭 기록 — 편집실 텍스트 레이어의 씨앗(tikitaka_labels/v1)

거절(묶음을 만들지 않는다 — 옛 영상과 새 대본이 섞인 묶음은 없는 것보다 나쁘다):
  - 전사 수정 무효화가 대기 중 (transcript_dependents_dirty.json)
  - review_<suffix>/ 가 없음 (전사 수정으로 보관 처리 → 다시 렌더 필요)
  - 미리보기 렌더(preview_only) · 렌더 지문 불일치 · 완성본이 review 의 최종본과 다름
  - publish_<suffix>.json 없음 · TTS 파일 없음

provenance.render(렌더 당시 코드 버전·실행 인자·디자인)는 파이프라인이 렌더 직후 묶을 때만 채운다.
기존 잡 폴더에는 그 기록이 없다 → 수동 묶음은 null(지어내지 않는다). export 는 수동 시점 코드 버전일 뿐이다.
전사가 바뀌면 cli.archive_transcript_dependents 가 mark_stale 로 기존 묶음에 stale 표시를 남긴다(파일은 그대로).

편집실 수정 기록(`tikitaka_edit/v1`) — 자리만 잡는다(적용하는 쪽은 아직 없다):
  <잡>/video_edits/<suffix>/<edit_id>.json   한 번 제출 = 파일 하나, 덮어쓰지 않는다(추가만)
  묶음(videos/<suffix>/)은 렌더마다 통째로 교체되므로 입력인 수정 기록을 그 안에 두지 않는다.
  전사 수정 무효화(cli.archive_transcript_dependents)·--redo 패턴에도 걸리지 않는 이름이다 — 검수자 수정이 캐시와 함께 치워지면 안 된다.
  based_on_render_fingerprint: 어느 판을 보고 고쳤는지. 적용기는 지금 렌더와 다르면 덮어쓰지 말고 확인 필요로 멈춰야 한다.
  video.json 의 edits: 렌더 기록(provenance.render.applied_edits)에 든 것은 applied, 나머지는 pending.
  video_edits/<suffix>/drafts/ 는 편집실 화면의 임시 저장(초안)이다 — 수정 기록이 아니며 적용 대상도 아니다.
  편집실(ves-workspace, Python 3.9)은 이 모듈을 import 하지 못한다 → 제출은 아래 record-edit 명령으로 쓴다.

제출: python -m app.tikitaka.bundle record-edit <잡 폴더> <suffix> --by <이메일> --based-on <렌더 지문> [--note ..] < overrides.json
  지금 묶음의 렌더 지문과 다르거나 묶음이 낡음이면 거절(종료 코드 3) — 옛 판을 보고 한 수정을 새 판에 조용히 얹지 않는다.

사용: python -m app.tikitaka.bundle <잡 폴더> [--version 6 --version 8] [--tag t]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "tikitaka_video/v1"
# 같은 스키마 안에서 담는 내용이 바뀌면 올린다 — 같은 렌더라도 다시 묶는다(2: labels.json · 구운 ASS 만).
BUNDLE_REV = 3  # 3: editor_scan.mp4
EDITOR_SCAN = "editor_scan_360p.mp4"   # 잡 폴더에 하나 — 같은 원본을 쓰는 모든 편이 공유
EDITOR_SCAN_REV = 1
EDIT_SCHEMA = "tikitaka_edit/v1"
VIDEOS_DIR = "videos"
EDITS_DIR = "video_edits"
# 기존 편집실 edit_overrides 와 같은 키 — 편집실 화면이 만드는 값을 그대로 받는다.
EDIT_KEYS = {"title", "subtitles", "clips", "tts", "images", "texts", "design"}
SUFFIX_RE = re.compile(r"^v(\d+)(?:_(.+))?$")
COPY_FROM_WORK = ["subtitle_segments.json", "tts_caption_segments.json", "review.json"]
BURNED_ASS = ["v3_subtitles.ass", "v3_tts.ass", "v3_labels.ass"]
LABELS_SCHEMA = "tikitaka_labels/v1"
FPS = 30  # finish.FPS · 편집본 클립 길이는 이 격자에 맞춘다
LABEL_KEYS = ("x", "y", "rotate", "color", "fx", "kind", "person", "anchor", "register_id")
REQUIRED_FROM_WORK = {"edit_plan.json", "subtitle_segments.json", "checkpoint_resources.json", "review.json"}


class BundleRefused(Exception):
    """묶음을 만들 수 없는 상태 — 사유는 사람이 읽는 한국어."""


@dataclass
class Result:
    suffix: str
    status: str          # exported | unchanged | refused
    detail: str = ""
    path: Path | None = None


def suffix_of(n: int, tag: str = "") -> str:
    return f"v{n}{'_' + tag if tag else ''}"


def discover(job_dir: Path) -> list[str]:
    """완성본이 있는 편 — shorts_<suffix>.mp4 (미리보기·보관본 제외), 번호순."""
    found = []
    for p in job_dir.glob("shorts_v*.mp4"):
        suffix = p.stem[len("shorts_"):]
        if suffix.endswith("_visual_preview") or ".prev_" in p.name or not SUFFIX_RE.match(suffix):
            continue
        found.append(suffix)
    return sorted(found, key=lambda s: (int(SUFFIX_RE.match(s).group(1)), s))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _load(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _place(src: Path, dst: Path) -> None:
    """묶음은 스냅숏이다 — 하드링크는 원본을 제자리에서 덮으면 묶음도 같이 바뀌어(해시 불일치) 쓰지 않는다.
    macOS(APFS)는 복제(cp -c, 쓰기 시 복사: 공간 거의 0·원본과 독립), 안 되면 일반 복사."""
    if sys.platform == "darwin":
        try:
            subprocess.run(["cp", "-c", str(src), str(dst)], check=True, capture_output=True, timeout=600)
            return
        except (OSError, subprocess.SubprocessError):
            dst.unlink(missing_ok=True)
    shutil.copy2(src, dst)


def _git(repo: Path) -> dict:
    try:
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=10, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                                    text=True, timeout=10, check=True).stdout.strip())
        return {"git_sha": sha, "git_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_sha": None, "git_dirty": None}


def _source_identity(job_dir: Path) -> dict:
    """pipeline_identity.json 의 원본 기록 + 지금도 같은 파일인지(크기·수정시각)."""
    ident = job_dir / "pipeline_identity.json"
    if not ident.exists():
        raise BundleRefused("pipeline_identity.json 없음 — 원본 영상을 특정할 수 없다")
    src = dict(_load(ident).get("source") or {})
    path = Path(src.get("path") or "")
    if not path.is_absolute():
        raise BundleRefused("pipeline_identity.json 에 원본 절대경로가 없다")
    try:
        st = path.stat()
        src["exists"] = True
        src["matches_now"] = (str(st.st_size) == str(src.get("size"))
                              and str(st.st_mtime_ns) == str(src.get("mtime_ns")))
    except OSError:
        src["exists"] = False
        src["matches_now"] = False
    return src


def collect(job_dir: Path, suffix: str) -> dict:
    """묶음 재료를 확인만 한다(쓰지 않는다). 거절 사유가 있으면 BundleRefused."""
    job_dir = job_dir.resolve()
    if (job_dir / "transcript_dependents_dirty.json").exists():
        raise BundleRefused("전사 수정 무효화 대기 중 — 파이프라인을 다시 돌린 뒤 묶는다")
    final = job_dir / f"shorts_{suffix}.mp4"
    work = job_dir / f"review_{suffix}"
    if not final.exists():
        raise BundleRefused(f"완성본 shorts_{suffix}.mp4 없음")
    if not work.is_dir():
        raise BundleRefused(f"review_{suffix}/ 없음 — 전사 수정으로 보관 처리됐거나 렌더 전. 다시 렌더 필요")
    missing = sorted(n for n in REQUIRED_FROM_WORK if not (work / n).exists())
    if missing:
        raise BundleRefused(f"review_{suffix}/ 에 {', '.join(missing)} 없음")
    review = _load(work / "review.json")
    if review.get("preview_only"):
        raise BundleRefused("미리보기 렌더(preview_only) — 최종본이 아니다")
    fp_file = work / "render_fingerprint.json"
    render_fp = _load(fp_file).get("fingerprint") if fp_file.exists() else None
    if not render_fp or review.get("render_fingerprint") != render_fp:
        raise BundleRefused("렌더 지문 불일치 — review.json 과 render_fingerprint.json 이 다른 렌더를 가리킨다")
    job_review = job_dir / f"review_{suffix}.json"
    if job_review.exists() and _load(job_review).get("render_fingerprint") != render_fp:
        raise BundleRefused(f"review_{suffix}.json 이 다른 렌더를 가리킨다")
    review_final = work / "final_1080x1920.mp4"
    final_sha = sha256(final)
    if review_final.exists() and sha256(review_final) != final_sha:
        raise BundleRefused(f"shorts_{suffix}.mp4 가 review_{suffix}/final_1080x1920.mp4 와 다르다 — 어느 쪽이 최신인지 불분명")
    publish = job_dir / f"publish_{suffix}.json"
    if not publish.exists():
        raise BundleRefused(f"publish_{suffix}.json 없음 — 제목·발행 정보가 확정되지 않았다")
    table = job_dir / f"grid_table_guarded_{suffix}.json"
    if not table.exists():
        table = job_dir / f"grid_table_{suffix}.json"
    if not table.exists():
        raise BundleRefused(f"grid_table_{suffix}.json 없음")

    resources = _load(work / "checkpoint_resources.json")
    tts = []
    for i, f in enumerate(resources.get("tts_cue_files") or []):
        p = Path(f.get("path") or f.get("file") or "")
        if not p.is_absolute():
            p = (work / p).resolve()
        if not p.is_file():
            raise BundleRefused(f"TTS 파일 없음: {p}")
        tts.append(p)
    return {"job_dir": job_dir, "suffix": suffix, "final": final, "final_sha": final_sha, "work": work,
            "review": review, "render_fp": render_fp, "publish": _load(publish), "table": table,
            "resources": resources, "tts": tts, "source": _source_identity(job_dir)}


def burned_ass(work: Path) -> list[str]:
    """최종 렌더가 실제로 구운 ASS. 라벨이 0개로 바뀐 재렌더는 옛 v3_labels.ass 를 지우지 않고 남기므로
    파일이 있다는 것만으로 넣으면 완성본에 없는 라벨이 묶음에 들어간다(2026-09-23 선공개 v6 실측)."""
    flt = work / "final_1080x1920.filter.txt"
    if flt.exists():
        text = flt.read_text(encoding="utf-8", errors="replace")
        return [n for n in BURNED_ASS if (work / n).exists() and n in text]
    style = work / "checkpoint_style.json"
    labels = ((_load(style).get("style") or {}).get("v3_style") or {}).get("labels") if style.exists() else None
    return [n for n in BURNED_ASS if (work / n).exists() and (n != "v3_labels.ass" or labels)]


def clip_edited_sec(c: dict) -> float:
    """편집본에서 이 클립이 차지하는 길이 — app.v3.assemble 과 같은 자(배속·붙잡기 포함, 30fps 격자)."""
    speed = float(c.get("playback_speed") or 1.0)
    raw = (float(c["clip_end_sec"]) - float(c["clip_start_sec"])) / speed + float(c.get("hold_sec") or 0.0)
    return round(raw * FPS) / FPS


def edited_to_source(timeline: list[dict], t: float) -> float | None:
    """편집본 초 → 원본 초. 붙잡은 꼬리(hold)는 클립 끝, 범위 밖은 None."""
    offset = 0.0
    for c in timeline:
        dur = clip_edited_sec(c)
        if offset - 1e-6 <= t < offset + dur - 1e-9:
            local = t - offset
            speed = float(c.get("playback_speed") or 1.0)
            span = (float(c["clip_end_sec"]) - float(c["clip_start_sec"])) / speed
            return round(float(c["clip_end_sec"]) if local >= span else float(c["clip_start_sec"]) + local * speed, 3)
        offset += dur
    return None


def labels_doc(work: Path, plan: dict) -> dict | None:
    """review_<suffix>/checkpoint_style.json → 편집실용 라벨 문서. 스타일 기록이 없으면 None.
    x·y 는 Stage 4 가 적은 글자 중심 비율(1080×1920) — 렌더 때 더하는 썸네일 밴드 이동·얼굴 회피 **이전** 값이다."""
    style_file = work / "checkpoint_style.json"
    if not style_file.exists():
        return None
    doc = _load(style_file)
    v3 = (doc.get("style") or {}).get("v3_style") or {}
    timeline = plan.get("timeline") or []
    labels = []
    for i, lb in enumerate(v3.get("labels") or []):
        if not str(lb.get("text") or "").strip():
            continue      # 문구 없는 옛 index 형 — 렌더가 따로 채운다(finalize.plan_labels)
        start, end = float(lb["start_sec"]), float(lb["end_sec"])
        labels.append({"id": f"lb{i}", "origin": "ai", "text": lb["text"], "start_sec": start, "end_sec": end,
                       "duration_sec": round(end - start, 3), "source_time_sec": edited_to_source(timeline, start),
                       **{k: lb[k] for k in LABEL_KEYS if k in lb}})
    emphasis = [{k: e[k] for k in ("index", "line", "text", "start_sec", "end_sec", "scale", "color", "semantic_level")
                 if k in e} for e in v3.get("emphasis") or []]
    dropped = [{k: p[k] for k in ("text", "anchor", "reason", "result") if k in p}
               for p in (doc.get("audit") or {}).get("label_probes") or [] if "드롭" in str(p.get("result") or "")]
    return {"schema": LABELS_SCHEMA, "time_base": "edited_sec", "canvas": [1080, 1920],
            "position": "stage4_authored", "style_fingerprint": doc.get("fingerprint"),
            "labels": labels, "emphasis": emphasis, "dropped": dropped}


def editor_scan(job_dir: Path, source: dict, *, ffmpeg: str | None = None) -> Path | None:
    """편집실 미리보기용 사본을 (없거나 원본이 바뀌었으면) 만든다. 못 만들면 None — 묶음은 계속 만든다."""
    out, meta = job_dir / EDITOR_SCAN, job_dir / (EDITOR_SCAN.rsplit(".", 1)[0] + ".json")
    want = {"rev": EDITOR_SCAN_REV, "source": {k: str(source.get(k)) for k in ("path", "size", "mtime_ns")}}
    if out.exists() and meta.exists() and _load(meta) == want:
        return out
    if not (source.get("exists") and source.get("matches_now")):
        return None
    ffmpeg = ffmpeg or os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    tmp = job_dir / f".{EDITOR_SCAN}.tmp.mp4"
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(source["path"]), "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
           "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", str(tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
    except (OSError, subprocess.SubprocessError):
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(out)
    _write_json(meta, want)
    return out


def _file_entry(path: Path, rel: str) -> dict:
    return {"path": rel, "bytes": path.stat().st_size, "sha256": sha256(path)}


def edits_dir(job_dir: Path, suffix: str) -> Path:
    return Path(job_dir) / EDITS_DIR / suffix


def record_edit(job_dir: Path, suffix: str, overrides: dict, *, based_on_render_fingerprint: str,
                created_by: str, note: str = "") -> dict:
    """편집실 제출 한 건을 기록한다(추가만). 렌더·적용은 하지 않는다."""
    if not SUFFIX_RE.match(suffix):
        raise ValueError(f"편 이름이 아니다: {suffix}")
    if not isinstance(overrides, dict) or not overrides:
        raise ValueError("수정 내용이 비었다")
    unknown = sorted(set(overrides) - EDIT_KEYS)
    if unknown:
        raise ValueError(f"모르는 수정 항목: {', '.join(unknown)}")
    if not based_on_render_fingerprint:
        raise ValueError("어느 렌더를 보고 고쳤는지(based_on_render_fingerprint)가 필요하다")
    if not created_by:
        raise ValueError("수정한 사람(created_by)이 필요하다")
    body = json.dumps(overrides, ensure_ascii=False, sort_keys=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    edit_id = f"{stamp}-{hashlib.sha256((body + str(time.time_ns())).encode()).hexdigest()[:8]}"
    record = {"schema": EDIT_SCHEMA, "edit_id": edit_id, "suffix": suffix,
              "based_on_render_fingerprint": based_on_render_fingerprint,
              "created_by": created_by, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "note": note, "overrides_sha256": hashlib.sha256(body.encode()).hexdigest(),
              "overrides": overrides}
    d = edits_dir(job_dir, suffix)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{edit_id}.json.tmp"
    _write_json(tmp, record)
    tmp.replace(d / f"{edit_id}.json")
    return record


def list_edits(job_dir: Path, suffix: str) -> list[dict]:
    """제출 순서대로. 깨진 파일은 조용히 건너뛰지 않고 예외 — 검수자 수정을 잃은 채 렌더하면 안 된다."""
    d = edits_dir(job_dir, suffix)
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        data = _load(f)
        if data.get("schema") != EDIT_SCHEMA or data.get("suffix") != suffix:
            raise ValueError(f"수정 기록 형식이 다르다: {f}")
        out.append(data)
    return out


def _edits_summary(job_dir: Path, suffix: str, render: dict | None) -> dict:
    applied_ids = set((render or {}).get("applied_edits") or [])
    edits = list_edits(job_dir, suffix)
    brief = lambda e: {"edit_id": e["edit_id"], "created_by": e["created_by"], "created_at": e["created_at"],
                       "based_on_render_fingerprint": e["based_on_render_fingerprint"]}
    return {"dir": f"{EDITS_DIR}/{suffix}",
            "applied": [brief(e) for e in edits if e["edit_id"] in applied_ids],
            "pending": [brief(e) for e in edits if e["edit_id"] not in applied_ids]}


def render_provenance(repo: Path | None = None, **settings: Any) -> dict:
    """렌더 직후 파이프라인이 넘기는 기록 — 코드 버전 + 실행 인자·설정(JSON 으로 못 쓰는 값은 문자열)."""
    repo = repo or Path(__file__).resolve().parents[2]
    info = {**_git(repo), "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "argv": sys.argv[1:], **settings}
    return json.loads(json.dumps(info, ensure_ascii=False, default=str))


def build(parts: dict, dest: Path, *, repo: Path, render: dict | None = None) -> dict:
    """dest(빈 폴더)에 묶음을 쓴다. 반환: video.json 내용."""
    work = parts["work"]
    files: dict[str, dict] = {}

    _place(parts["final"], dest / "shorts.mp4")
    files["video"] = {"path": "shorts.mp4", "bytes": parts["final"].stat().st_size, "sha256": parts["final_sha"]}

    plan = _load(work / "edit_plan.json")
    plan["input"] = {**(plan.get("input") or {}), "video_path": parts["source"]["path"]}
    _write_json(dest / "edit_plan.json", plan)
    files["edit_plan"] = _file_entry(dest / "edit_plan.json", "edit_plan.json")

    scan = editor_scan(parts["job_dir"], parts["source"])
    if scan is not None:
        _place(scan, dest / "editor_scan.mp4")
        files["editor_scan"] = _file_entry(dest / "editor_scan.mp4", "editor_scan.mp4")
    labels = labels_doc(work, plan)
    if labels is not None:
        _write_json(dest / "labels.json", labels)
        files["labels"] = {**_file_entry(dest / "labels.json", "labels.json"), "count": len(labels["labels"])}
    for name in COPY_FROM_WORK + burned_ass(work):
        if (work / name).exists():
            shutil.copy2(work / name, dest / name)
            files[name.rsplit(".", 1)[0]] = _file_entry(dest / name, name)

    (dest / "tts").mkdir()
    resources = json.loads(json.dumps(parts["resources"]))
    for f, src in zip(resources.get("tts_cue_files") or [], parts["tts"]):
        rel = f"tts/{src.name}"
        if not (dest / rel).exists():
            _place(src, dest / rel)
        f.pop("file", None)
        f["path"] = rel
    _write_json(dest / "checkpoint_resources.json", resources)
    files["checkpoint_resources"] = _file_entry(dest / "checkpoint_resources.json", "checkpoint_resources.json")

    shutil.copy2(parts["table"], dest / "grid_table.json")
    files["grid_table"] = {**_file_entry(dest / "grid_table.json", "grid_table.json"), "from": parts["table"].name}
    _write_json(dest / "publish.json", parts["publish"])
    files["publish"] = _file_entry(dest / "publish.json", "publish.json")

    m = SUFFIX_RE.match(parts["suffix"])
    publish = parts["publish"]
    video = {
        "schema": SCHEMA,
        "bundle_rev": BUNDLE_REV,
        "video_key": f"{parts['job_dir'].name}/{parts['suffix']}",
        "job_dir": str(parts["job_dir"]),
        "suffix": parts["suffix"],
        "version": int(m.group(1)),
        "tag": m.group(2),
        "pipeline": publish.get("pipeline") or "grid-review",
        "title": publish.get("title"),
        "work": publish.get("work"),
        "episode": publish.get("episode"),
        "source": parts["source"],
        "render_fingerprint": parts["render_fp"],
        "review_items": len(parts["review"].get("items") or []),
        "files": files,
        "tts_count": len({p.name for p in parts["tts"]}),
        "edits": _edits_summary(parts["job_dir"], parts["suffix"], render),
        "provenance": {"render": render,
                       "export": {**_git(repo), "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}},
        "status": "ready",
    }
    _write_json(dest / "video.json", video)
    return video


def export(job_dir: Path, suffix: str, *, repo: Path | None = None, render: dict | None = None) -> Result:
    """한 편을 묶는다. 같은 렌더·같은 완성본이면 건드리지 않는다. 바뀌었으면 이전 묶음을 .prev_<ns> 로 보관 후 교체."""
    repo = repo or Path(__file__).resolve().parents[2]
    try:
        parts = collect(job_dir, suffix)
    except BundleRefused as e:
        return Result(suffix, "refused", str(e))
    videos = parts["job_dir"] / VIDEOS_DIR
    target = videos / suffix
    current = target / "video.json"
    if current.exists():
        old = _load(current)
        same_render = (old.get("render_fingerprint") == parts["render_fp"]
                       and old.get("files", {}).get("video", {}).get("sha256") == parts["final_sha"])
        if render is None and same_render:
            # 같은 렌더를 수동으로 다시 묶을 때 파이프라인이 남긴 렌더 기록을 잃지 않는다
            render = (old.get("provenance") or {}).get("render")
        if (old.get("schema") == SCHEMA and old.get("bundle_rev") == BUNDLE_REV
                and old.get("render_fingerprint") == parts["render_fp"]
                and old.get("files", {}).get("video", {}).get("sha256") == parts["final_sha"]
                and old.get("status") == "ready"
                and old.get("edits") == _edits_summary(parts["job_dir"], suffix, render or (old.get("provenance") or {}).get("render"))
                and not (render and not (old.get("provenance") or {}).get("render"))):
            return Result(suffix, "unchanged", "같은 렌더 — 기존 묶음 유지", target)
    stamp = time.time_ns()
    tmp = videos / f".{suffix}.tmp-{stamp}"
    tmp.mkdir(parents=True)
    try:
        build(parts, tmp, repo=repo, render=render)
        if target.exists():
            target.rename(videos / f"{suffix}.prev_{stamp}")
        tmp.rename(target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return Result(suffix, "exported", "", target)


def mark_stale(job_dir: Path, reason: str, **detail: Any) -> list[str]:
    """기존 묶음에 '낡음' 표시 — 파일은 그대로 둔다(이미 VES 로 넘어간 영상의 기록). 다시 렌더·묶으면 교체된다."""
    marked = []
    videos = Path(job_dir) / VIDEOS_DIR
    for f in sorted(videos.glob("*/video.json")) if videos.is_dir() else []:
        if ".prev_" in f.parent.name or f.parent.name.startswith("."):
            continue
        data = _load(f)
        if data.get("status") == "stale":
            continue
        data["status"] = "stale"
        data["stale"] = {"reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **detail}
        tmp = f.with_suffix(".json.tmp")
        _write_json(tmp, data)
        tmp.replace(f)
        marked.append(f.parent.name)
    return marked


def record_edit_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.tikitaka.bundle record-edit")
    ap.add_argument("job_dir", type=Path)
    ap.add_argument("suffix")
    ap.add_argument("--by", required=True)
    ap.add_argument("--based-on", required=True)
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    def fail(msg: str, code: int) -> int:
        print(json.dumps({"error": msg}, ensure_ascii=False))
        return code
    current = a.job_dir / VIDEOS_DIR / a.suffix / "video.json"
    if not current.exists():
        return fail("영상 묶음이 없다", 2)
    video = _load(current)
    if video.get("status") != "ready":
        return fail("이 영상은 전사 수정 등으로 낡음 상태다. 다시 렌더된 뒤 편집실을 새로 열어 주세요.", 3)
    if video.get("render_fingerprint") != a.based_on:
        return fail("편집실을 연 뒤 영상이 다시 렌더됐다. 편집실을 새로 열어 최신 판에서 수정해 주세요.", 3)
    try:
        overrides = json.loads(sys.stdin.read())
        record = record_edit(a.job_dir, a.suffix, overrides, based_on_render_fingerprint=a.based_on,
                             created_by=a.by, note=a.note)
    except (ValueError, json.JSONDecodeError) as e:
        return fail(str(e), 2)
    print(json.dumps({k: record[k] for k in ("edit_id", "created_at", "created_by", "based_on_render_fingerprint")},
                     ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["record-edit"]:
        return record_edit_cli(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m app.tikitaka.bundle", description="그리드 리뷰 잡 → 편별 VES 묶음(videos/<vN>/)")
    ap.add_argument("job_dir", type=Path)
    ap.add_argument("--version", type=int, action="append", help="묶을 편 번호(여러 번 가능). 없으면 완성본이 있는 모든 편")
    ap.add_argument("--tag", default="", help="파이프라인의 --tag 와 같은 값")
    a = ap.parse_args(argv)
    if not a.job_dir.is_dir():
        ap.error(f"잡 폴더 없음: {a.job_dir}")
    suffixes = [suffix_of(n, a.tag) for n in a.version] if a.version else discover(a.job_dir)
    if not suffixes:
        print("완성본(shorts_vN.mp4)이 있는 편이 없다.")
        return 1
    label = {"exported": "묶음 생성", "unchanged": "변경 없음", "refused": "거절"}
    refused = 0
    for s in suffixes:
        r = export(a.job_dir, s)
        refused += r.status == "refused"
        print(f"{s:>10}  {label[r.status]}  {r.detail or r.path}")
    return 2 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
