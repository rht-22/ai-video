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

character_index 병렬 슬롯: 기획서는 ArcFace 인덱스를 grid 와 병렬 실행하라고
하지만 그 모듈(face_id.py)은 2026-08-25 커밋 014335e 가 사용자 결정("필요없어")으로
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

V3_STEPS = ("init", "research", "probe", "proxy", "grid", "seq_analyze")


def _write_json(path: Path, doc: Any) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _character_index_slot(output_dir: Path, proxy_path: Path,
                          research: dict | None, out: dict, log=print) -> None:
    """grid 와 병렬로 도는 인물 인덱스 슬롯 — 모듈이 있으면 실행, 없으면 부재 기록."""
    try:
        from app.modules.face_id import FaceIdentifier  # noqa: F401 — 014335e 가 제거
    except ImportError:
        out.update({"status": "module_absent",
                    "note": "face_id 모듈 부재(014335e 제거, 사용자 결정) — 배선만 유지. "
                            "복원되면 이 슬롯이 그대로 병렬 실행된다."})
        log("  [v3/character_index] 모듈 부재 — 건너뜀(run_log 기록)")
        return
    try:
        from app.modules.face_id import FaceIdentifier
        cast = []
        for c in (research or {}).get("cast_images") or []:
            if c.get("image_path"):
                cast.append(c)
        fi = FaceIdentifier()
        fi.build_references(cast)
        appearances = fi.build_appearance_index(proxy_path)
        _write_json(output_dir / "checkpoint_character_index.json", appearances)
        out.update({"status": "ok", "appearances": len(appearances)})
    except Exception as e:  # noqa: BLE001 — 기존 규약: 인덱스 실패는 본편을 막지 않는다
        out.update({"status": "failed", "error": f"{type(e).__name__}: {e}"})
        log(f"  [v3/character_index] WARN 실패 — 인덱스 없이 진행: {e}")


def run_v3(*, video_path: Path, work_title: str, outdir: Path,
           srt_path: Path | None = None, episode: int | None = None,
           job_id: str | None = None, from_step: str | None = None,
           skip_research: bool = False, skip_seq_analyze: bool = False,
           scene_threshold: float = SCENE_THRESHOLD, log=print) -> Path:
    """v3 M1 실행 → output_dir 반환. 실패해도 run_log 는 남긴다(finally)."""
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
    run_log: dict[str, Any] = {
        "job_id": job_id,
        "pipeline": "v3_m1",
        "input": {"video_path": str(video_path), "work_title": work_title,
                  "srt_path": str(srt_path) if srt_path else None,
                  "episode": episode, "language": "ko"},
        "provenance": build_provenance(config),
        "steps": [],
    }
    # v3 신규 호출의 모델 역할 — provenance 모듈(공유)을 고치지 않고 가산 키로 남긴다
    run_log["provenance"].setdefault("models", {})["roles_v3"] = {
        "seq_analyze": "pro", "grid_transcribe": f"local:{WHISPER_MODEL_NAME}"}

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
        return output_dir
    finally:
        _write_json(output_dir / "run_log.json", run_log)
