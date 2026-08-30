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
            "chunk_split", "chunk_analyze", "story", "resources",
            "draft_render", "style", "render", "validate")


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
           skip_stage2: bool = False, skip_stage3: bool = False,
           skip_stage4: bool = False,
           story_target_sec: float | None = None,
           story_max_sec: float | None = None,
           max_chunks: int | None = None,
           scene_threshold: float = SCENE_THRESHOLD, log=print) -> Path:
    """v3 실행(M1 grid·Stage1 + M2 chunk_split·Stage2) → output_dir 반환.
    실패해도 run_log 는 남긴다(finally). max_chunks 는 스모크용 — 계획 앞에서부터
    N 개만 재단·분석하고 나머지는 매니페스트에 file=null 로 남는다(커버리지 표기)."""
    if from_step is not None and from_step not in V3_STEPS:
        raise ValueError(f"--from-step 은 {V3_STEPS} 중 하나: {from_step}")

    # .env(FFMPEG_BIN=ffmpeg 7 고정 등)를 진입점에서 결정적으로 로드한다 — 종전에는
    # gemini_client 가 로드될 때만 실려, style 캐시가 있으면 렌더가 PATH 의 ffmpeg 8
    # (-filter_complex_script 거부)로 떨어져 죽었다(M4 스모크 재현 — 우연 의존 금지).
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    except ImportError:
        pass

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
            "pipeline": "v3_m3",
            "input": {"video_path": str(video_path), "work_title": work_title,
                      "srt_path": str(srt_path) if srt_path else None,
                      "episode": episode, "language": "ko"},
            "provenance": build_provenance(config),
            "steps": [],
        }
    # v3 신규 호출의 모델 역할 — provenance 모듈(공유)을 고치지 않고 가산 키로 남긴다
    run_log.setdefault("provenance", {}).setdefault("models", {})["roles_v3"] = {
        "seq_analyze": "pro", "chunk_analyze": "pro", "story": "flash",
        "style": "flash", "frame_qc": "flash",
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

        # ── M3: Stage 3 story → edit_plan·자막·TTS cue ────────────────────
        stage2_path = output_dir / "stage2.json"
        if skip_stage3:
            log("  [v3/story] 건너뜀(--skip-stage3)")
        elif not stage2_path.exists():
            log("  [v3/story] stage2.json 없음 — 건너뜀(Stage 2 가 선행돼야 한다)")
        else:
            _run_m3(output_dir=output_dir, video_path=Path(video_path),
                    work_title=work_title, grid=grid, research=research,
                    from_step=from_step,
                    story_target_sec=story_target_sec,
                    story_max_sec=story_max_sec,
                    get_gemini=get_gemini, step=step, log=log)

        # ── M4: draft_render → style → render → validate ──────────────────
        if skip_stage4:
            log("  [v3/stage4] 건너뜀(--skip-stage4)")
        elif not (output_dir / "edit_plan.json").exists():
            log("  [v3/stage4] edit_plan.json 없음 — 건너뜀(M3 이 선행돼야 한다)")
        else:
            _run_m4(output_dir=output_dir, video_path=Path(video_path),
                    grid=grid, from_step=from_step,
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

    # 캐시 유효성은 from_step 이 아니라 **상류 내용**에 묶는다 — stage1/grid 를
    # 재구성하면 같은 번호의 chunk 라도 다른 경계·다른 span 체계다. 지문이 다르면
    # 캐시를 통째로 버린다(옛 grid 의 meanings 가 새 문서에 접합되던 리뷰 재현 수정).
    import hashlib
    fingerprint = hashlib.sha1(json.dumps(
        {"plan": chunks_plan,
         "spans": [[s["id"], s["t_in"], s["t_out"]] for s in
                   grid.get("span_candidates") or []]},
        sort_keys=True).encode("utf-8")).hexdigest()[:16]
    ca_ckpt = output_dir / "checkpoint_chunk_analyze.json"
    done: dict[str, Any] = {}
    if ca_ckpt.exists() and from_step not in ("chunk_split", "chunk_analyze"):
        cached = _read_json(ca_ckpt)
        if cached.get("fingerprint") == fingerprint:
            done = cached.get("chunks") or {}
            if done:
                log(f"  [v3/stage2] 청크 캐시 {len(done)}건 로드")
        elif cached:
            log("  [v3/stage2] ⚠ 상류(stage1/grid) 변경 감지 — 청크 캐시 폐기")

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
        try:
            meanings, audit = run_chunk_analyze(
                get_gemini(), output_dir / "chunks" / entry["file"], entry,
                stage1_doc, grid, appearances=appearances,
                research_context=research_ctx, character_names=names or None, log=log)
        except Exception as e:  # noqa: BLE001 — 부분 실패 계약: 다른 chunk 는 계속 간다
            meanings = None
            audit = {"chunk": key, "attempts": [],
                     "failed": f"예외: {type(e).__name__}: {e}"}
            log(f"  [v3/stage2] ⚠ {key} 예외 — 커버리지 표기 후 계속: {e}")
        done[key] = {"meanings": meanings, "audit": audit}
        # 청크마다 증분 저장(요금 보호 — 실패 기록도 저장해 재실행 재과금을 막는다.
        # 실패 chunk 재시도는 --from-step chunk_analyze 로 명시적으로만)
        _write_json(ca_ckpt, {"fingerprint": fingerprint, "chunks": done})

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


def _run_m3(*, output_dir: Path, video_path: Path, work_title: str, grid: dict,
            research: dict | None, from_step: str | None,
            story_target_sec: float | None, story_max_sec: float | None,
            get_gemini, step, log) -> None:
    """Stage 3(story) + 경계면 조립 + resources(TTS 합성) — 발주서 v3-m3.

    story 캐시는 M2 와 같은 규율로 **상류 지문**에 묶는다 — stage2 의 meaning/span
    편성이 바뀌면 같은 job 이라도 다른 재료다(사이드카 무효화 규율)."""
    import hashlib

    from app.v3 import assemble, story as st

    stage2_doc = _read_json(output_dir / "stage2.json")
    span_index, span_order = st.build_span_index(stage2_doc, grid)
    if not span_index:
        log("  [v3/story] 분석된 span 이 없다 — 건너뜀(커버리지 표기)")
        step("story", skipped="analyzed span 0")
        return
    fingerprint = hashlib.sha1(json.dumps(
        {"spans": [[sid, span_index[sid]["t_in"], span_index[sid]["t_out"],
                    span_index[sid]["importance"]] for sid in span_order]},
        sort_keys=True).encode("utf-8")).hexdigest()[:16]

    target = story_target_sec if story_target_sec is not None else st.STORY_TARGET_SEC
    max_sec = story_max_sec if story_max_sec is not None else st.STORY_MAX_SEC

    story_ckpt = output_dir / "checkpoint_story.json"
    story_doc = None
    if story_ckpt.exists() and from_step not in ("story", "resources"):
        cached = _read_json(story_ckpt)
        if cached.get("fingerprint") == fingerprint:
            story_doc = cached.get("story")
            log("  [v3/story] 캐시 로드(--from-step story 로 재구성)")
        else:
            log("  [v3/story] ⚠ 상류(stage2) 변경 감지 — story 캐시 폐기")
    if story_doc is None and from_step != "resources":
        t0 = time.time()
        research_ctx = (research or {}).get("work_context") or ""
        story_doc, audit = st.run_story(
            get_gemini(), stage2_doc, grid, work_title=work_title,
            research_context=research_ctx, target_sec=target, max_sec=max_sec,
            log=log)
        _write_json(story_ckpt, {"fingerprint": fingerprint, "story": story_doc})
        step("story", elapsed=round(time.time() - t0, 1),
             attempts=len(audit["attempts"]), fallback=audit.get("fallback", False),
             template=story_doc["template"], pieces=audit.get("pieces"),
             budget=story_doc["budget"],
             narration_dropped=len(story_doc.get("narration_dropped") or []),
             audit_attempts=audit["attempts"])
        log(f"  [v3/story] 완료 — {story_doc['template']} · 비트 "
            f"{len(story_doc['beats'])}개 · {story_doc['budget']['total_after_sec']}s")
    elif story_doc is None:
        if not story_ckpt.exists():
            log("  [v3/story] --from-step resources 인데 story 캐시가 없다 — 건너뜀")
            step("story", skipped="checkpoint_story 없음")
            return
        story_doc = _read_json(story_ckpt).get("story")

    # ── 경계면 조립(C1·C2·C6) — 순수, LLM 없음 ────────────────────────────
    plan = assemble.assemble_edit_plan(
        story_doc, span_index, video_path=str(video_path), work_title=work_title)
    belt = assemble.verify_edit_plan(plan, grid)
    if belt["pct"] is not None and belt["pct"] < 100.0:
        # Stage 2 벨트와 같은 규율 — 구조상 100% 여야 하고 아니면 코드 결함
        raise AssertionError(f"edit_plan 시각 정합 벨트 위반: {belt}")
    _write_json(output_dir / "edit_plan.json", plan)

    segments = assemble.word_subtitles(plan["timeline"], span_index,
                                       grid.get("words") or [])
    _write_json(output_dir / "subtitle_segments.json", segments)

    cues = assemble.finalize_cues(story_doc.get("narration_cues") or [],
                                  plan["timeline"], voice="ko_female", speed="normal")
    lost = [c for c in cues if c.get("start_sec") is None]
    cues = [c for c in cues if c.get("start_sec") is not None]

    # ── resources — TTS 합성(기존 tts.py 재사용 · fail-soft) ──────────────
    t0 = time.time()
    from app.modules.tts import (
        active_backend,
        elevenlabs_disabled,
        synthesize_tts_with_fit,
    )
    tts_cue_files = []
    for ci, cue in enumerate(cues):
        tts_path = output_dir / f"tts_cue_{ci}.mp3"
        try:
            # v1 과 같은 fit 합성 — 실측이 창(duration_sec)을 넘으면 다음 대사를
            # 밟는다(스모크 실측: 5.198s > 창 4.133s). 배속 재시도는 tts.py 몫.
            # 창 초과 시 축약은 v1 과 같이 Flash(shorten_text)에 맡긴다 —
            # shorten_fn 없이는 '단순 절단'이 문장을 중간에서 잘라먹는다(스모크 실측)
            shorten = getattr(get_gemini(), "shorten_text", None)
            final_text, actual = synthesize_tts_with_fit(
                cue["text"], tts_path, target_sec=float(cue["duration_sec"]),
                voice=cue["voice"], speed=cue["speed"], shorten_fn=shorten)
            cue["text"] = final_text
            cue["fit_actual_sec"] = round(actual, 3)
        except Exception as e:  # noqa: BLE001 — 합성 실패가 계획 산출을 막지 않는다
            log(f"  [v3/resources] ⚠ cue {ci} 합성 실패 — 계획만 유지: {e}")
            cue["fit_actual_sec"] = None
        tts_cue_files.append({"cue_index": ci, "path": str(tts_path), "cue": cue})
    backend = active_backend()
    resources = {"tts_cue_files": tts_cue_files, "tts_backend": backend}
    _write_json(output_dir / "checkpoint_resources.json", resources)

    stats = assemble.clip_stats(plan)
    entry = {"step": "resources", "elapsed": round(time.time() - t0, 1),
             "tts_backend": backend, "tts_cues": len(tts_cue_files),
             "cues_lost_to_trim": [c["text"][:40] for c in lost],
             "time_alignment": belt, "clip_stats": stats,
             "subtitle_segments": len(segments)}
    fallback = elevenlabs_disabled()
    if fallback:
        entry["tts_fallback_reason"] = "elevenlabs_auth_expired"
        entry["tts_fallback_detail"] = fallback[:200]
    step("resources", **{k: v for k, v in entry.items() if k != "step"})
    log(f"  [v3/resources] 완료 — 클립 {stats['clips']}개 {stats['total_sec']}s · "
        f"자막 {len(segments)}줄 · cue {len(tts_cue_files)}개({backend}) · "
        f"시각정합 {belt['pct']}%")


def _run_m4(*, output_dir: Path, video_path: Path, grid: dict,
            from_step: str | None, get_gemini, step, log) -> None:
    """M4 — 2-pass 렌더 + validate 확장 (발주서 v3-m4).

    draft/최종 렌더는 산출 파일 존재로 캐시(--from-step 으로 재구성). style 은
    M3 과 같은 상류 지문 규율 — edit_plan timeline 이 바뀌면 폐기."""
    import hashlib

    from app.v3 import finalize, stage4

    plan = _read_json(output_dir / "edit_plan.json")
    story_ckpt = _read_json(output_dir / "checkpoint_story.json")
    story_doc = story_ckpt.get("story") or {}
    segments = _read_json(output_dir / "subtitle_segments.json") \
        if (output_dir / "subtitle_segments.json").exists() else []
    resources = _read_json(output_dir / "checkpoint_resources.json") \
        if (output_dir / "checkpoint_resources.json").exists() else {}

    m4_invalidate = from_step in ("draft_render", "style", "render", "validate")

    # ── draft_render (11) ─────────────────────────────────────────────────
    draft_path = output_dir / "draft_480.mp4"
    frames_dir = output_dir / "draft_frames"
    windows = stage4.edited_beat_windows(story_doc, plan["timeline"])
    if draft_path.exists() and frames_dir.is_dir() \
            and from_step not in ("draft_render",):
        log("  [v3/draft] 캐시 존재 — 재사용(--from-step draft_render 로 재구성)")
        frames = sorted(frames_dir.glob("beat*.jpg"))
        frame_list = [{"path": str(p)} for p in frames]
    else:
        cost = stage4.render_draft(video_path, plan["timeline"], draft_path, log=log)
        frame_list = stage4.sample_beat_frames(draft_path, windows, frames_dir,
                                               log=log)
        step("draft_render", **cost, frames=len(frame_list))

    # ── style (12) — 상류 지문 규율 ───────────────────────────────────────
    fingerprint = hashlib.sha1(json.dumps(
        [[c["clip_start_sec"], c["clip_end_sec"], c.get("span_ids")]
         for c in plan["timeline"]], sort_keys=True).encode()).hexdigest()[:16]
    style_ckpt = output_dir / "checkpoint_style.json"
    style_doc = None
    if style_ckpt.exists() and from_step not in ("draft_render", "style"):
        cached = _read_json(style_ckpt)
        if cached.get("fingerprint") == fingerprint:
            style_doc = cached.get("style")
            log("  [v3/style] 캐시 로드")
        else:
            log("  [v3/style] ⚠ 상류(edit_plan) 변경 감지 — 캐시 폐기")
    if style_doc is None:
        t0 = time.time()
        style_doc, audit = stage4.run_style(get_gemini(), frame_list, story_doc,
                                            log=log)
        _write_json(style_ckpt, {"fingerprint": fingerprint, "style": style_doc})
        step("style", elapsed=round(time.time() - t0, 1),
             attempts=len(audit["attempts"]), fallback=audit.get("fallback", False),
             diff_keys=audit.get("diff_keys"), audit_attempts=audit["attempts"])
        log(f"  [v3/style] 완료 — 프리셋 diff {len(style_doc.get('diff') or {})}키")
    _write_json(output_dir / "style.json", style_doc)

    # ── render (13) ───────────────────────────────────────────────────────
    final_path = output_dir / "final_1080x1920.mp4"
    if final_path.exists() and from_step not in ("draft_render", "style", "render"):
        log("  [v3/render] 캐시 존재 — 재사용(--from-step render 로 재구성)")
    else:
        final_path, cost = finalize.render_final(
            video_path=video_path, plan=plan, style_doc=style_doc,
            segments=segments, resources=resources, story_doc=story_doc,
            output_dir=output_dir, log=log)
        step("render", **cost)

    # ── validate (14) — 항상 재계산(산출이 아니라 검증이다) ───────────────
    t0 = time.time()
    stage1_doc = _read_json(output_dir / "stage1.json")
    stage2_doc = _read_json(output_dir / "stage2.json")
    tmp_dir = output_dir / "validate_tmp"
    tmp_dir.mkdir(exist_ok=True)
    vdoc = finalize.run_validate(
        plan=plan, grid=grid, stage1_doc=stage1_doc, stage2_doc=stage2_doc,
        segments=segments, resources=resources, final_path=final_path,
        tmp_dir=tmp_dir, gemini=get_gemini(), log=log)
    _write_json(output_dir / "validation.json", vdoc)
    step("validate", elapsed=round(time.time() - t0, 1),
         hard_fail=vdoc["hard_fail"], warnings=vdoc["warnings_total"],
         snap_pct=vdoc["snap_belt"]["pct"],
         tts_violations=len(vdoc["tts_conflicts"]["violations"]),
         exception_violations=len(vdoc["exception_ingress"]["violations"]))
    log(f"  [v3/validate] 완료 — hard_fail={vdoc['hard_fail']} · "
        f"경고 {vdoc['warnings_total']}건 · 스냅 {vdoc['snap_belt']['pct']}%")
