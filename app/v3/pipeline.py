"""create_shorts_v3 오케스트레이션 — M1 범위: init → research → probe → proxy →
【grid ∥ character_index】 → 【Stage 1 seq_analyze】. 이후 단계는 M2+.

병행 구축 원칙: 기존 14단계 파이프라인 코드는 **한 줄도 건드리지 않는다** —
공유 모듈은 import 재사용만. 산출은 기존 job 디렉토리 레이아웃(run_log.json ·
checkpoint_*.json)을 그대로 따라 리플레이 하네스 로더가 자동 판별한다(M0 계약).

산출물:
  run_log.json                {job_id, pipeline:"v3_m1", input, provenance, steps[]}
  checkpoint_research.json    기존 파이프라인과 같은 모양(재사용)
  checkpoint_probe.json       〃
  checkpoint_grid_words.json  전사 단어 캐시(가장 비싼 단계 — 재개 시 재사용)
  grid.json                   정본 격자(§A) — words/scene_cuts/silence/arousal/spans
  stage1.json                 Stage 1 스키마(기획서 §3 그대로)
  <제목>_480.mp4 / <제목>_scan.mp4 / <제목>_16k.wav

character_index 병렬 슬롯: ArcFace 인덱스를 grid 와 병렬 실행한다.
face_id.py 는 014335e 가 지웠다가 2026-08-31 사용자 지시로 복원(레퍼런스-프리 클러스터링) —
아래 이력 주석은 보존한다. 구판 삭제 사유: 모듈은 2026-08-25 커밋 014335e 가 사용자 결정("필요없어")으로
지웠다 — 여기서는 **배선만 유지**한다: 모듈이 돌아오면 그대로 도는 병렬 슬롯 +
부재를 run_log 에 명시(조용한 누락 금지). 복원 여부는 사용자 결정 사안.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.media_probe import MediaInfo, probe_media
from app.modules.provenance import build_provenance
from app.modules.speech import extract_audio_from_video
from app.modules.subtitle import parse_subtitle
from app.v3 import seq_analyze as s1
from app.v3.arousal import compute_arousal
from app.v3.audio import detect_silence_intervals, load_pcm
from app.v3.scenecut import SCENE_THRESHOLD, detect_scene_cuts
from app.v3.timegrid import build_grid_doc, carve_spans
from app.v3.transcribe import WHISPER_MODEL_NAME, transcribe_words

V3_STEPS = ("init", "research", "probe", "proxy", "grid", "seq_analyze",
            "chunk_split", "chunk_analyze")


def _write_json(path: Path, doc: Any) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _character_index_slot(output_dir: Path, proxy_path: Path,
                          research: dict | None, out: dict, log=print) -> None:
    """grid 와 병렬로 도는 인물 인덱스 슬롯 — 모듈이 있으면 실행, 없으면 부재 기록."""
    try:
        from app.modules.face_id import FaceIdentifier  # 2026-08-31 복원 (레퍼런스-프리)
    except ImportError:
        out.update({"status": "module_absent",
                    "note": "face_id 모듈 부재 — 배선만 유지."})
        log("  [v3/character_index] 모듈 부재 — 건너뜀(run_log 기록)")
        return
    try:
        fi = FaceIdentifier()
    except ImportError as e:
        out.update({"status": "deps_absent",
                    "note": "deepface 미설치 — pip install -r requirements-faceid.txt "
                            "후 이 슬롯이 자동 활성화된다. 본편 진행에는 영향 없음.",
                    "error": str(e)})
        log("  [v3/character_index] deepface 미설치 — 건너뜀(run_log 기록)")
        return
    try:
        cast = []
        for c in (research or {}).get("cast_images") or []:
            if c.get("image_path"):
                cast.append(c)
        fi.build_references(cast)  # 레퍼런스는 선택 — 없으면 클러스터 라벨(person_N)
        appearances = fi.build_appearance_index(proxy_path)
        _write_json(output_dir / "checkpoint_character_index.json", appearances)
        labels = {a.get("character") for a in appearances}
        out.update({"status": "ok", "appearances": len(appearances),
                    "labels": len(labels),
                    "reference_free": not cast})
    except Exception as e:  # noqa: BLE001 — 기존 규약: 인덱스 실패는 본편을 막지 않는다
        out.update({"status": "failed", "error": f"{type(e).__name__}: {e}"})
        log(f"  [v3/character_index] WARN 실패 — 인덱스 없이 진행: {e}")


def run_v3(*, video_path: Path, work_title: str, outdir: Path,
           srt_path: Path | None = None, episode: int | None = None,
           job_id: str | None = None, from_step: str | None = None,
           skip_research: bool = False, skip_seq_analyze: bool = False,
           skip_stage2: bool = False, max_chunks: int | None = None,
           scene_threshold: float = SCENE_THRESHOLD, log=print) -> Path:
    """v3 실행(M1 grid·Stage1 + M2 chunk_split·Stage2) → output_dir 반환.
    실패해도 run_log 는 남긴다(finally). max_chunks 는 스모크용 — 계획 앞에서부터
    N 개만 재단·분석하고 나머지는 매니페스트에 file=null 로 남는다(커버리지 표기)."""
    if from_step is not None and from_step not in V3_STEPS:
        raise ValueError(f"--from-step 은 {V3_STEPS} 중 하나: {from_step}")

    # ── init — 기존 job_id 규약 그대로 ────────────────────────────────────
    safe_title = work_title.replace(" ", "_")
    if job_id:
        output_dir = Path(outdir) / job_id
        if not output_dir.is_dir():
            raise FileNotFoundError(f"재개할 job 디렉토리 없음: {output_dir}")
    else:
        job_id = f"{safe_title}_{uuid.uuid4().hex[:8]}"
        output_dir = Path(outdir) / job_id
        output_dir.mkdir(parents=True, exist_ok=False)
    log(f"[v3] job: {job_id} → {output_dir}")

    config = AppConfig()
    run_log_path = output_dir / "run_log.json"
    if run_log_path.exists():
        # 재개는 기존 run_log 에 **이어 쓴다** — 통째로 새로 만들면 전사 실패 창·
        # 휴리스틱 불일치 같은 감사 기록이 지워진다(리뷰 재현 수정 · 기존 파이프라인의
        # 재개 규약과 동일). 깨진 파일이면 크게 실패한다(조용한 초기화 금지).
        run_log = _read_json(run_log_path)
        run_log.setdefault("steps", []).append(
            {"step": "resume", "from_step": from_step})
    else:
        run_log = {
            "job_id": job_id,
            "pipeline": "v3_m2",
            "input": {"video_path": str(video_path), "work_title": work_title,
                      "srt_path": str(srt_path) if srt_path else None,
                      "episode": episode, "language": "ko"},
            "provenance": build_provenance(config),
            "steps": [],
        }
    # v3 신규 호출의 모델 역할 — provenance 모듈(공유)을 고치지 않고 가산 키로 남긴다
    run_log.setdefault("provenance", {}).setdefault("models", {})["roles_v3"] = {
        "seq_analyze": "pro", "chunk_analyze": "pro",
        "grid_transcribe": f"local:{WHISPER_MODEL_NAME}"}

    def step(name: str, **fields) -> None:
        run_log["steps"].append({"step": name, **fields})

    gemini = None

    def get_gemini():
        nonlocal gemini
        if gemini is None:
            from app.modules.gemini_client import load_gemini_client
            gemini = load_gemini_client()
        return gemini

    try:
        # ── research — 기존 체크포인트 모양 재사용 ─────────────────────────
        research: dict | None = None
        research_ckpt = output_dir / "checkpoint_research.json"
        if research_ckpt.exists():
            research = _read_json(research_ckpt)
            log("  [v3/research] 캐시 로드")
        elif not skip_research:
            t0 = time.time()
            from app.modules.work_researcher import research_work
            r = research_work(work_title, episode, get_gemini())
            research = {
                "work_context": r.work_context,
                "episodes_context": r.episodes_context,
                "raw_data": r.raw_data,
                "sources": r.sources,
                "cast_images": [
                    {"character_name": c.character_name, "actor_name": c.actor_name,
                     "role_description": c.role_description,
                     "image_path": str(c.image_path) if c.image_path else None,
                     "image_url": c.image_url}
                    for c in r.characters],
            }
            _write_json(research_ckpt, research)
            step("research", elapsed=round(time.time() - t0, 1),
                 has_context=bool(r.work_context))

        # ── probe — 기존 체크포인트 모양 재사용 ────────────────────────────
        probe_ckpt = output_dir / "checkpoint_probe.json"
        if probe_ckpt.exists():
            data = _read_json(probe_ckpt)
            media_info = MediaInfo(**{**data, "path": Path(data["path"])})
        else:
            media_info = probe_media(Path(video_path))
            _write_json(probe_ckpt, {**asdict(media_info), "path": str(media_info.path)})
            step("probe", result={"duration_sec": media_info.duration_sec,
                                  "fps": media_info.fps,
                                  "width": media_info.width,
                                  "height": media_info.height,
                                  "has_audio": media_info.has_audio})
        duration = float(media_info.duration_sec)

        # ── proxy — 480p(기존 인자 그대로) + scan 변형 ─────────────────────
        proxy_path = output_dir / f"{safe_title}_480.mp4"
        if not proxy_path.exists():
            t0 = time.time()
            ffmpeg = find_ffmpeg_command("ffmpeg")
            subprocess.run(
                [ffmpeg, "-y", "-i", str(Path(video_path).resolve()),
                 "-vf", "scale=-2:480,fps=4", "-fps_mode", "cfr",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                 "-c:a", "aac", "-ac", "1", "-ar", "22050",
                 "-threads", "4", str(proxy_path)],
                check=True, capture_output=True)
            step("proxy", elapsed=round(time.time() - t0, 1), kind="480p")
        scan_proxy = output_dir / f"{safe_title}_scan.mp4"
        if not skip_seq_analyze:
            t0 = time.time()
            s1.build_scan_proxy(Path(video_path), scan_proxy, log=log)
            step("proxy", elapsed=round(time.time() - t0, 1), kind="scan",
                 height=s1.SCAN_PROXY_HEIGHT, file_fps=s1.SCAN_PROXY_FILE_FPS)

        # ── grid ∥ character_index ────────────────────────────────────────
        grid_path = output_dir / "grid.json"
        grid_invalidate = from_step == "grid"
        ci_result: dict[str, Any] = {}
        ci_thread = threading.Thread(
            target=_character_index_slot,
            args=(output_dir, proxy_path, research, ci_result, log), daemon=True)
        ci_thread.start()

        if grid_path.exists() and not grid_invalidate:
            grid = _read_json(grid_path)
            log("  [v3/grid] 캐시 로드")
        else:
            t0 = time.time()
            audio_path = output_dir / f"{safe_title}_16k.wav"
            if not audio_path.exists():
                extract_audio_from_video(Path(video_path), audio_path)

            words_ckpt = output_dir / "checkpoint_grid_words.json"
            if words_ckpt.exists():
                wdata = _read_json(words_ckpt)
                words, failed = wdata["words"], [tuple(w) for w in wdata["failed_windows"]]
                log(f"  [v3/grid] 전사 캐시 로드 ({len(words)} 단어)")
            else:
                names = [c["character_name"] for c in (research or {}).get(
                    "cast_images") or [] if c.get("character_name")]
                words, failed = transcribe_words(
                    audio_path, duration, work_title=work_title,
                    character_names=names or None,
                    work_context=(research or {}).get("work_context") or None, log=log)
                _write_json(words_ckpt, {"model": WHISPER_MODEL_NAME, "words": words,
                                         "failed_windows": [list(f) for f in failed]})
            if failed:
                log(f"  [v3/grid] ⚠ 전사 실패 창 {len(failed)}건 — scene 폴백(무성 취급): "
                    + ", ".join(f"{a:.0f}~{b:.0f}s" for a, b in failed))

            scene_cuts = detect_scene_cuts(proxy_path, threshold=scene_threshold)
            silence = detect_silence_intervals(audio_path, duration)
            arousal = compute_arousal(load_pcm(audio_path), duration, words)
            spans = carve_spans(words, scene_cuts, silence, duration)

            srt_cues = None
            if srt_path:
                srt_cues = [{"t0": round(s.start_sec, 3), "t1": round(s.end_sec, 3),
                             "text": s.text} for s in parse_subtitle(Path(srt_path))]

            grid = build_grid_doc(
                source={"path": str(video_path), "duration_sec": round(duration, 3),
                        "fps": media_info.fps, "width": media_info.width,
                        "height": media_info.height},
                words=words, scene_cuts=scene_cuts, silence=silence,
                arousal=arousal, span_candidates=spans,
                transcript_meta={"backend": "whisper", "model": WHISPER_MODEL_NAME,
                                 "word_count": len(words),
                                 "failed_windows": [list(f) for f in failed],
                                 "srt_provided": bool(srt_path)},
                srt_cues=srt_cues)
            _write_json(grid_path, grid)
            n_voiced = sum(1 for s in spans if s["is_audio"])
            step("grid", elapsed=round(time.time() - t0, 1),
                 words=len(words), scene_cuts=len(scene_cuts),
                 silence_intervals=len(silence), arousal_points=len(arousal),
                 spans={"total": len(spans), "voiced": n_voiced,
                        "unvoiced": len(spans) - n_voiced},
                 transcribe_failed_windows=[list(f) for f in failed],
                 scene_threshold=scene_threshold)
            log(f"  [v3/grid] 완료 — 단어 {len(words)} · 장면컷 {len(scene_cuts)} · "
                f"span {len(spans)}(유성 {n_voiced})")

        ci_thread.join(timeout=3600)
        step("character_index", **(ci_result or {"status": "unknown"}))

        # ── Stage 1 seq_analyze ───────────────────────────────────────────
        stage1_path = output_dir / "stage1.json"
        if skip_seq_analyze:
            log("  [v3/stage1] 건너뜀(--skip-seq-analyze)")
        elif stage1_path.exists() and from_step not in ("grid", "seq_analyze"):
            log("  [v3/stage1] 캐시 존재 — 재사용(--from-step seq_analyze 로 재구성)")
        else:
            t0 = time.time()
            research_ctx = (research or {}).get("work_context") or ""
            doc, audit = s1.run_seq_analyze(get_gemini(), scan_proxy, grid,
                                            research_context=research_ctx, log=log)
            _write_json(stage1_path, doc)
            step("seq_analyze", elapsed=round(time.time() - t0, 1),
                 attempts=len(audit["attempts"]),
                 sequences=len(doc["sequences"]),
                 chunks=sum(len(sq["chunks"]) for sq in doc["sequences"]),
                 exception={k: v is not None
                            for k, v in doc["exception_sector"].items()},
                 heuristic_hints=audit.get("heuristic_hints"),
                 heuristic_mismatch=audit.get("heuristic_mismatch"),
                 audit_attempts=audit["attempts"])
            mism = audit.get("heuristic_mismatch") or []
            if mism:
                log(f"  [v3/stage1] ⚠ 휴리스틱-모델 불일치 {len(mism)}건(검수 신호) — "
                    "run_log 기록")
            log(f"  [v3/stage1] 완료 — sequence {len(doc['sequences'])}개")

        # ── M2: chunk_split → Stage 2 chunk_analyze ───────────────────────
        if skip_stage2:
            log("  [v3/stage2] 건너뜀(--skip-stage2)")
        elif not stage1_path.exists():
            log("  [v3/stage2] stage1.json 없음 — 건너뜀(Stage 1 이 선행돼야 한다)")
        else:
            _run_m2(output_dir=output_dir, video_path=Path(video_path),
                    stage1_path=stage1_path, grid=grid, research=research,
                    from_step=from_step, max_chunks=max_chunks,
                    get_gemini=get_gemini, step=step, log=log)
        return output_dir
    finally:
        _write_json(output_dir / "run_log.json", run_log)


def _run_m2(*, output_dir: Path, video_path: Path, stage1_path: Path, grid: dict,
            research: dict | None, from_step: str | None, max_chunks: int | None,
            get_gemini, step, log) -> None:
    """chunk_split + Stage 2 — run_v3 본체에서 분리(단계 블록이 길어져서).

    청크별 결과는 checkpoint_chunk_analyze.json 에 **증분 저장**된다 — Pro 호출이
    청크당 1회라 중단·재개 시 이미 분석한 청크의 요금을 다시 내면 안 된다."""
    from app.v3.chunk_analyze import run_chunk_analyze
    from app.v3.chunk_split import plan_chunks, split_chunks

    stage1_doc = _read_json(stage1_path)
    chunks_plan, exceptions = plan_chunks(stage1_doc)
    only = None
    if max_chunks is not None:
        only = [(c["seq_number"], c["chunk_number"]) for c in chunks_plan[:max_chunks]]

    t0 = time.time()
    manifest = split_chunks(video_path, chunks_plan, exceptions,
                            output_dir / "chunks", only=only, log=log)
    _write_json(output_dir / "checkpoint_chunk_split.json", manifest)
    n_split = sum(1 for c in manifest["chunks"] if c["file"])
    step("chunk_split", elapsed=round(time.time() - t0, 1),
         planned=len(manifest["chunks"]), split=n_split,
         exceptions_removed=exceptions,
         proxy=manifest["proxy"])
    log(f"  [v3/chunk_split] 계획 {len(manifest['chunks'])} · 재단 {n_split} · "
        f"exception 제거 {len(exceptions)}")

    appearances = None
    ci_path = output_dir / "checkpoint_character_index.json"
    if ci_path.exists():
        appearances = _read_json(ci_path)

    ca_ckpt = output_dir / "checkpoint_chunk_analyze.json"
    done: dict[str, Any] = {}
    if ca_ckpt.exists() and from_step not in ("chunk_split", "chunk_analyze"):
        done = _read_json(ca_ckpt)
        if done:
            log(f"  [v3/stage2] 청크 캐시 {len(done)}건 로드")

    research_ctx = (research or {}).get("work_context") or ""
    names = [c["character_name"] for c in (research or {}).get("cast_images") or []
             if c.get("character_name")]
    t0 = time.time()
    for entry in manifest["chunks"]:
        if not entry["file"]:
            continue
        key = f"s{entry['seq_number']}c{entry['chunk_number']}"
        if key in done:
            continue
        meanings, audit = run_chunk_analyze(
            get_gemini(), output_dir / "chunks" / entry["file"], entry,
            stage1_doc, grid, appearances=appearances,
            research_context=research_ctx, character_names=names or None, log=log)
        done[key] = {"meanings": meanings, "audit": audit}
        _write_json(ca_ckpt, done)                 # 청크마다 증분 저장(요금 보호)

    # ── stage2.json 조립 — stage1 문서에 meanings 를 채운다(§4 스키마 그대로) ──
    analyzed = failed = not_split = 0
    fail_notes: list[dict] = []
    for sq in stage1_doc.get("sequences") or []:
        for ch in sq.get("chunks") or []:
            key = f"s{sq['number']}c{ch['number']}"
            rec = done.get(key)
            if rec is None:
                not_split += 1
                continue
            if rec.get("meanings"):
                ch["meanings"] = rec["meanings"]
                analyzed += 1
            else:
                failed += 1
                fail_notes.append({"chunk": key,
                                   "reason": (rec.get("audit") or {}).get("failed")})
    # 검증 3종 집계(발주서 — 수치로)
    ta_checked = ta_ok = restored = voiced = 0
    cc_top = cc_all = 0
    attempts_total = 0
    for rec in done.values():
        a = rec.get("audit") or {}
        attempts_total += len(a.get("attempts") or [])
        ta = a.get("time_alignment") or {}
        ta_checked += ta.get("checked", 0)
        ta_ok += ta.get("from_grid", 0)
        tg = a.get("transcript_guard") or {}
        restored += tg.get("restored", 0)
        voiced += tg.get("voiced_spans", 0)
        cc = a.get("character_check") or {}
        if cc.get("status") == "ok":
            for row in cc.get("clusters") or []:
                if row.get("consistency") is not None:
                    cc_top += round(row["consistency"] * row["assignments"])
                    cc_all += row["assignments"]
    validation = {
        "time_alignment": {"checked": ta_checked, "from_grid": ta_ok,
                           "pct": round(ta_ok / ta_checked * 100, 2) if ta_checked else None},
        "transcript_guard": {"voiced_spans": voiced, "restored": restored},
        "character_check": {"overall_consistency":
                            round(cc_top / cc_all, 3) if cc_all else None,
                            "assignments": cc_all,
                            "status": "ok" if cc_all else "skipped"},
    }
    stage2_doc = {**stage1_doc, "schema": "v3_stage2/v1",
                  "coverage": {"chunks_planned": len(manifest["chunks"]),
                               "analyzed": analyzed, "failed": failed,
                               "not_split": not_split, "failures": fail_notes},
                  "validation": validation}
    _write_json(output_dir / "stage2.json", stage2_doc)
    step("chunk_analyze", elapsed=round(time.time() - t0, 1),
         analyzed=analyzed, failed=failed, not_split=not_split,
         attempts_total=attempts_total, validation=validation,
         failures=fail_notes,
         audits=[rec.get("audit") or {} for rec in done.values()])
    log(f"  [v3/stage2] 완료 — 분석 {analyzed} · 실패 {failed} · 미재단 {not_split} · "
        f"시각정합 {validation['time_alignment']['pct']}% · "
        f"전사복원 {restored}/{voiced} · "
        f"인물일관성 {validation['character_check']['overall_consistency']}")
