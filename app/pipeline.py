from __future__ import annotations

import json
import re
import time
import uuid
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended-A
    "\U00002600-\U000026FF"  # misc symbols
    "\U0000200D"             # zero width joiner
    "\U00002B50"             # star
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


from app.config import AppConfig, Paths, DesignConfig, get_font_path
from app.modules.chunker import build_chunks, split_video_chunk
from app.modules.gemini_client import load_gemini_client
from app.modules.media_probe import probe_media
from app.modules.moment_ranker import assign_sequence_ids
from app.modules.reframe import build_crop_timeline
from app.modules.renderer import RenderInputs, render_short
from app.modules.scene_detect import detect_scenes
from app.modules.speech import extract_audio_segment, extract_transcript, SpeechSegment
from app.modules.story_builder import StoryClip, validate_story_clips, validate_clip_coherence
from app.modules.subtitle import (
    SubtitleStyle,
    build_ass_from_segments,
    build_tts_ass,
    merge_subtitle_segments,
    parse_subtitle,
    remap_transcript_to_edited_timeline,
)
from app.modules.tts import synthesize_tts
from app.modules.work_researcher import research_work, CharacterInfo
from app.modules.validator import validate_output
from app.modules.ffmpeg_utils import find_ffmpeg_command
from types import SimpleNamespace
from app.modules.silence_cutter import cut_silence_from_clips, flatten_to_clips, print_silence_cut_summary


def _clips_from_storyline(storyline_data: dict, fallback_title: str = "") -> tuple[list[StoryClip], str]:
    """스토리라인 dict에서 (clips, title_text)를 추출합니다."""
    clips: list[StoryClip] = []

    # 제목 구성 (이모지 제거)
    title_line1 = _strip_emoji(storyline_data.get("title_line1", ""))
    title_line2 = _strip_emoji(storyline_data.get("title_line2", ""))
    if title_line1 and title_line2:
        title_text = f"{title_line1}\n{title_line2}"
    else:
        title_text = storyline_data.get("topic", fallback_title)

    if storyline_data.get("shorts_type") == "highlight":
        tts_line_val = storyline_data.get("tts_line", "")
        clips.append(StoryClip(
            role="payoff",
            start_sec=storyline_data["start_sec"],
            end_sec=storyline_data["end_sec"],
            subtitle=tts_line_val if tts_line_val else storyline_data.get("topic", ""),
            tts_line=tts_line_val,
            use_original_audio=storyline_data.get("use_original_audio", True),
            chunk_index=storyline_data.get("chunk_index", -1),
            candidate_index=storyline_data.get("candidate_index", -1),
        ))
    else:
        actual_storyline = storyline_data.get("storyline", {})

        if "hook" in actual_storyline:
            h = actual_storyline["hook"]
            tts_val = h.get("tts_line", "")
            clips.append(StoryClip(
                role="hook",
                start_sec=h["start_sec"], end_sec=h["end_sec"],
                subtitle=tts_val if tts_val else h.get("description", ""),
                tts_line=tts_val,
                use_original_audio=h.get("use_original_audio", True),
                chunk_index=h.get("chunk_index", -1),
                candidate_index=h.get("candidate_index", -1),
                character_focus=tuple(h.get("character_focus") or []),
                bridges_from_previous=h.get("bridges_from_previous") or "",
            ))

        if "build" in actual_storyline:
            for b in actual_storyline["build"]:
                tts_val = b.get("tts_line", "")
                clips.append(StoryClip(
                    role="build",
                    start_sec=b["start_sec"], end_sec=b["end_sec"],
                    subtitle=tts_val if tts_val else b.get("description", ""),
                    tts_line=tts_val,
                    use_original_audio=b.get("use_original_audio", True),
                    chunk_index=b.get("chunk_index", -1),
                    candidate_index=b.get("candidate_index", -1),
                    character_focus=tuple(b.get("character_focus") or []),
                    bridges_from_previous=b.get("bridges_from_previous") or "",
                ))

        if "payoff" in actual_storyline:
            p = actual_storyline["payoff"]
            tts_val = p.get("tts_line", "")
            clips.append(StoryClip(
                role="payoff",
                start_sec=p["start_sec"], end_sec=p["end_sec"],
                subtitle=tts_val if tts_val else p.get("description", ""),
                tts_line=tts_val,
                use_original_audio=p.get("use_original_audio", True),
                chunk_index=p.get("chunk_index", -1),
                candidate_index=p.get("candidate_index", -1),
                character_focus=tuple(p.get("character_focus") or []),
                bridges_from_previous=p.get("bridges_from_previous") or "",
            ))

    # tts_line이 없는 클립은 원본 오디오 사용으로 강제
    clips = [replace(c, use_original_audio=True) if not c.tts_line else c for c in clips]
    return clips, title_text


def _get_audio_duration(path: Path) -> float:
    """ffprobe로 오디오 파일의 재생 시간을 읽습니다."""
    ffprobe_cmd = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe_cmd, "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


@dataclass(frozen=True)
class PipelineInput:
    video_path: Path
    work_title: str
    topic: str
    outdir: Path
    design: DesignConfig = field(default_factory=DesignConfig)
    language: str = "ko"
    previous_episodes_context: str | None = None
    work_context: str | None = None
    srt_path: Path | None = None
    show_subtitles: bool = True
    max_shorts: int = 3
    skip_research: bool = False
    episode: int | None = None
    skip_intro_sec: float = 0.0
    skip_credits_sec: float = 0.0


@dataclass(frozen=True)
class PipelineOutput:
    output_videos: list[Path]
    edit_plan_path: Path
    run_log_path: Path

    @property
    def output_video(self) -> Path:
        """하위 호환: 첫 번째 영상 경로 반환."""
        return self.output_videos[0]


# ─────────────────────────────────────────────────────────
# 메인 파이프라인 (10단계)
# ─────────────────────────────────────────────────────────
def run_pipeline(payload: PipelineInput, from_step: str | None = None, job_id: str | None = None) -> PipelineOutput:
    print("=" * 60)
    print("파이프라인 시작")
    print("=" * 60)

    start_time = time.time()
    config = AppConfig()
    paths = Paths(app_root=Path(__file__).resolve().parent)

    # ═══════════════════════════════════════
    # [1/10] 초기화
    # ═══════════════════════════════════════
    print("\n[1/10] 초기화 중...")
    if job_id:
        output_dir = payload.outdir / job_id
        if not output_dir.exists():
            raise ValueError(f"Job ID {job_id}의 디렉토리를 찾을 수 없습니다: {output_dir}")
        print(f"  - 기존 작업 재개: {job_id}")
        print(f"  - 출력 디렉토리: {output_dir}")
        run_log_path = output_dir / "run_log.json"
        if run_log_path.exists():
            run_log = json.loads(run_log_path.read_text(encoding="utf-8"))
        else:
            run_log = {
                "job_id": job_id,
                "input": {
                    "video_path": str(payload.video_path),
                    "work_title": payload.work_title,
                    "topic": payload.topic,
                    "language": payload.language,
                },
                "steps": [],
            }
    else:
        safe_title = payload.work_title.replace(" ", "_")
        job_id = f"{safe_title}_{uuid.uuid4().hex[:2]}"
        output_dir = payload.outdir / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        run_log = {
            "job_id": job_id,
            "input": {
                "video_path": str(payload.video_path),
                "work_title": payload.work_title,
                "topic": payload.topic,
                "language": payload.language,
            },
            "steps": [],
        }
        print(f"  - Job ID: {job_id}")
        print(f"  - 출력 디렉토리: {output_dir}")
    print("[OK] 초기화 완료")

    # ═══════════════════════════════════════
    # [1.5/10] 작품 자동 리서치
    # ═══════════════════════════════════════
    checkpoint_research = output_dir / "checkpoint_research.json"
    cast_images: list[CharacterInfo] = []

    if not payload.skip_research and not payload.work_context:
        if checkpoint_research.exists():
            print("\n[1.5/10] 작품 리서치 로드 중... (체크포인트)")
            _rdata = json.loads(checkpoint_research.read_text(encoding="utf-8"))
            payload = replace(payload,
                work_context=_rdata.get("work_context", ""),
                previous_episodes_context=_rdata.get("episodes_context") or payload.previous_episodes_context,
            )
            # 캐스트 이미지 복원
            for ci in _rdata.get("cast_images", []):
                img_p = Path(ci["image_path"]) if ci.get("image_path") else None
                cast_images.append(CharacterInfo(
                    character_name=ci.get("character_name", ""),
                    actor_name=ci.get("actor_name", ""),
                    role_description=ci.get("role_description", ""),
                    image_path=img_p if img_p and img_p.exists() else None,
                    image_url=ci.get("image_url"),
                ))
            print(f"  리서치 로드 완료 ({len(cast_images)}명 캐릭터)")
        else:
            print("\n[1.5/10] 작품 자동 리서치 중...")
            research_start = time.time()
            gemini = load_gemini_client()
            research = research_work(payload.work_title, payload.episode, gemini)
            research_elapsed = time.time() - research_start

            if research.work_context:
                payload = replace(payload,
                    work_context=research.work_context,
                    previous_episodes_context=research.episodes_context or payload.previous_episodes_context,
                )
                print(f"  시놉시스: {research.work_context[:80]}...")
                print(f"  등장인물: {len(research.characters)}명")

                # TMDb 배우 이미지 다운로드
                import os
                tmdb_key = os.environ.get("TMDB_API_KEY")
                if tmdb_key:
                    from app.modules.tmdb_client import download_cast_images as _dl_cast
                    research_dir = output_dir / "_research"
                    cast_images = _dl_cast(
                        research.raw_data.get("characters", []),
                        research_dir,
                        tmdb_key,
                    )
                else:
                    cast_images = list(research.characters)
                    print("  [TMDb] TMDB_API_KEY 미설정 — 배우 이미지 없이 진행")

                # 체크포인트 저장
                checkpoint_research.write_text(json.dumps({
                    "work_context": research.work_context,
                    "episodes_context": research.episodes_context,
                    "raw_data": research.raw_data,
                    "sources": research.sources,
                    "cast_images": [
                        {
                            "character_name": ci.character_name,
                            "actor_name": ci.actor_name,
                            "role_description": ci.role_description,
                            "image_path": str(ci.image_path) if ci.image_path else None,
                            "image_url": ci.image_url,
                        }
                        for ci in cast_images
                    ],
                }, ensure_ascii=False, indent=2), encoding="utf-8")

                run_log["steps"].append({"step": "research", "elapsed": research_elapsed,
                                         "characters": len(cast_images)})
                print(f"[OK] 작품 리서치 완료 (소요 시간: {research_elapsed:.1f}초)")
            else:
                print("  [WARN] 리서치 결과 없음 — 작품 정보 없이 진행")
    elif payload.work_context:
        print("\n[1.5/10] 작품 리서치 건너뜀 (수동 work_context 제공)")
    else:
        print("\n[1.5/10] 작품 리서치 건너뜀 (--no-research)")

    # 단계별 실행 플래그
    step_order = [
        "init", "probe", "proxy", "chunk", "gemini",
        "graph", "story", "transcribe", "resources", "render", "validate",
    ]
    if from_step:
        start_idx = step_order.index(from_step)
        print(f"\n[WARN] {from_step} 단계부터 재시작합니다.")
    else:
        start_idx = 0

    # ═══════════════════════════════════════
    # [2/10] 미디어 프로브
    # ═══════════════════════════════════════
    checkpoint_probe = output_dir / "checkpoint_probe.json"
    if start_idx <= 1 and checkpoint_probe.exists() and from_step != "probe":
        print("\n[2/10] 미디어 정보 로드 중...")
        probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
        from app.modules.media_probe import MediaInfo
        media_info = MediaInfo(**probe_data)
        print(f"  - 영상 길이: {media_info.duration_sec:.1f}초")
        print(f"  - 해상도: {media_info.width}x{media_info.height}")
        print(f"  - FPS: {media_info.fps:.2f}")
        print(f"  - 오디오: {'있음' if media_info.has_audio else '없음'}")
        print("[OK] 미디어 정보 로드 완료 (체크포인트에서)")
    elif start_idx <= 1:
        print("\n[2/10] 미디어 정보 수집 중...")
        probe_start = time.time()
        media_info = probe_media(payload.video_path)
        probe_elapsed = time.time() - probe_start
        probe_dict = media_info.__dict__.copy()
        probe_dict["path"] = str(probe_dict["path"])
        run_log["steps"].append({"step": "probe", "result": probe_dict})
        checkpoint_probe.write_text(json.dumps(probe_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  - 영상 길이: {media_info.duration_sec:.1f}초")
        print(f"  - 해상도: {media_info.width}x{media_info.height}")
        print(f"  - FPS: {media_info.fps:.2f}")
        print(f"  - 오디오: {'있음' if media_info.has_audio else '없음'}")
        print(f"[OK] 미디어 프로브 완료 (소요 시간: {probe_elapsed:.1f}초)")
    else:
        if not checkpoint_probe.exists():
            raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_probe}")
        probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
        from app.modules.media_probe import MediaInfo
        media_info = MediaInfo(**probe_data)

    # ═══════════════════════════════════════
    # [3/10] 프록시 영상 생성
    # ═══════════════════════════════════════
    proxy_video_path = output_dir / f"{payload.work_title}_480.mp4"
    if not proxy_video_path.exists():
        print("\n[3/10] 분석용 프록시 영상 생성 중...")
        proxy_start = time.time()
        ffmpeg_exe = find_ffmpeg_command("ffmpeg")
        subprocess.run([
            ffmpeg_exe, '-y', '-i', str(payload.video_path.resolve()),
            '-vf', 'scale=-2:480,fps=4',
            '-fps_mode', 'cfr',
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '26',
            '-c:a', 'aac', '-ac', '1', '-ar', '22050',
            '-threads', '4',
            str(proxy_video_path)
        ], check=True, capture_output=True)
        proxy_elapsed = time.time() - proxy_start
        print(f"[OK] 프록시 영상 생성 완료 (소요 시간: {proxy_elapsed:.1f}초)")
    else:
        print("\n[3/10] 프록시 영상 이미 존재 — 건너뜀")

    # ═══════════════════════════════════════
    # [3.5/10] 인트로/크레딧 제외 구간 감지
    # ═══════════════════════════════════════
    from app.modules.intro_credits_detector import (
        detect_exclusion_zones, filter_excluded_moments, print_exclusion_summary, ExclusionZones,
    )

    checkpoint_exclusion = output_dir / "checkpoint_exclusion.json"
    if checkpoint_exclusion.exists():
        _ez_data = json.loads(checkpoint_exclusion.read_text(encoding="utf-8"))
        exclusion_zones = ExclusionZones.from_dict(_ez_data)
        print(f"\n[3.5/10] 제외 구간 로드 완료")
        print_exclusion_summary(exclusion_zones, media_info.duration_sec)
    else:
        # SRT가 있으면 자동 감지에 활용
        _srt_segs_for_detect = None
        if payload.srt_path and payload.srt_path.exists():
            try:
                _srt_segs_for_detect = parse_subtitle(payload.srt_path)
            except Exception:
                pass

        exclusion_zones = detect_exclusion_zones(
            media_info.duration_sec,
            skip_intro_sec=payload.skip_intro_sec,
            skip_credits_sec=payload.skip_credits_sec,
            auto_detect=True,
            srt_segments=_srt_segs_for_detect,
        )
        if exclusion_zones.detection_method != "none":
            print(f"\n[3.5/10] 인트로/크레딧 제외 구간 감지")
            print_exclusion_summary(exclusion_zones, media_info.duration_sec)
            checkpoint_exclusion.write_text(
                json.dumps(exclusion_zones.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # ═══════════════════════════════════════
    # [4/10] 청크 분할
    # ═══════════════════════════════════════
    print("\n[4/10] 영상 청크 분할 중...")
    chunks = build_chunks(
        proxy_video_path,
        media_info.duration_sec,
        config.chunk_seconds,
        config.chunk_overlap,
        content_start_sec=exclusion_zones.intro_end_sec,
        content_end_sec=exclusion_zones.credits_start_sec,
    )
    print(f"  - 총 {len(chunks)}개 청크 생성")
    if exclusion_zones.detection_method != "none":
        print(f"  - 유효 구간: {exclusion_zones.intro_end_sec:.1f}s ~ {exclusion_zones.credits_start_sec:.1f}s")

    split_chunks = []
    for i, chunk in enumerate(chunks, 1):
        print(f"    청크 {i} 분할 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
        split_path, actual_start_sec = split_video_chunk(
            proxy_video_path,
            chunk.start_sec,
            chunk.end_sec,
        )
        split_chunk = replace(chunk, split_path=split_path, actual_start_sec=actual_start_sec)
        split_chunks.append(split_chunk)
        print(f"      → {split_path.name} 생성 완료 (실제 시작: {actual_start_sec:.2f}초)")

    chunks = split_chunks
    print("[OK] 청크 분할 완료")

    # ═══════════════════════════════════════
    # [5-pre/10] 영상 의도 사전 분석 (narrative_skeleton)
    # ═══════════════════════════════════════
    skeleton_path = output_dir / "narrative_skeleton.json"
    narrative_skeleton: dict[str, Any] = {}
    if start_idx <= 4 and skeleton_path.exists() and from_step != "gemini":
        print("\n[5-pre/10] narrative_skeleton 로드 중...")
        try:
            narrative_skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
            print(f"  - skeleton intent: {narrative_skeleton.get('intent', '')[:60]}")
        except Exception as e:
            print(f"  [WARN] skeleton 로드 실패: {e}")
            narrative_skeleton = {}
    elif start_idx <= 4:
        print("\n[5-pre/10] 전체 영상 의도 사전 분석 중 (Flash)...")
        try:
            _intent_gemini = load_gemini_client()
            narrative_skeleton = _intent_gemini.analyze_video_intent(
                proxy_path=proxy_video_path,
                work_title=payload.work_title,
                topic=payload.topic,
                work_context=payload.work_context,
                previous_episodes_context=payload.previous_episodes_context,
            )
            if narrative_skeleton:
                skeleton_path.write_text(
                    json.dumps(narrative_skeleton, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  [OK] skeleton 저장: intent={narrative_skeleton.get('intent', '')[:60]}")
                print(f"       mandatory_scenes: {len(narrative_skeleton.get('mandatory_scenes', []))}개")
            else:
                print("  [WARN] skeleton이 비어 있음 — 청크 분석 시 생략")
        except Exception as e:
            print(f"  [WARN] 영상 의도 분석 실패: {e} — skeleton 없이 진행")
            narrative_skeleton = {}
    else:
        if skeleton_path.exists():
            try:
                narrative_skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
            except Exception:
                narrative_skeleton = {}

    # ── Gemini skeleton으로 인트로/크레딧 제외 구간 업데이트 ──
    if narrative_skeleton and (narrative_skeleton.get("has_intro") or narrative_skeleton.get("has_credits")):
        _srt_for_ez = None
        if payload.srt_path and payload.srt_path.exists():
            try:
                _srt_for_ez = parse_subtitle(payload.srt_path)
            except Exception:
                pass
        exclusion_zones = detect_exclusion_zones(
            media_info.duration_sec,
            skip_intro_sec=payload.skip_intro_sec,
            skip_credits_sec=payload.skip_credits_sec,
            auto_detect=True,
            srt_segments=_srt_for_ez,
            gemini_result=narrative_skeleton,
        )
        if exclusion_zones.detection_method != "none":
            print("  [인트로/크레딧] Gemini 영상 분석 결과 반영")
            print_exclusion_summary(exclusion_zones, media_info.duration_sec)
            checkpoint_exclusion.write_text(
                json.dumps(exclusion_zones.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # ═══════════════════════════════════════
    # [5/10] Gemini 분석 (바이럴 최적화)
    # ═══════════════════════════════════════
    checkpoint_gemini = output_dir / "checkpoint_gemini.json"
    if start_idx <= 4 and checkpoint_gemini.exists() and from_step != "gemini":
        print("\n[5/10] Gemini 분析 결과 로드 중...")
        gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
        all_candidates = gemini_data["all_candidates"]
        for m in all_candidates:
            m.setdefault("story_role", "build")
        print(f"  - 총 {len(all_candidates)}개 후보 모멘트")
        print("[OK] Gemini 분석 결과 로드 완료 (체크포인트에서)")
    elif start_idx <= 4:
        print("\n[5/10] Gemini 분석 준비 중...")
        gemini = load_gemini_client()
        print("[OK] Gemini 클라이언트 로드 완료")

        print("\n[5/10] Gemini 분석 진행 중...")
        all_candidates: list[dict[str, Any]] = []
        gemini_start = time.time()
        previous_analyses: list[dict[str, Any]] = []

        # SRT 자막이 있으면 미리 파싱하여 Gemini에 화자명 전달
        srt_segments_for_gemini: list[SpeechSegment] = []
        if payload.srt_path:
            srt_segments_for_gemini = parse_subtitle(payload.srt_path)
            print(f"  - SRT 자막 {len(srt_segments_for_gemini)}개 세그먼트 → Gemini 인물 식별용 전달")

        for idx, chunk in enumerate(chunks, 1):
            print(f"  청크 {idx}/{len(chunks)} 분석 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
            chunk_start = time.time()

            split_path = chunk.split_path if chunk.split_path else None
            scenes = detect_scenes(split_path, media_info.fps, chunk.end_sec - chunk.start_sec)
            scene_boundaries = [scene.start_sec for scene in scenes]

            # 해당 청크 범위의 자막만 필터링하여 전달
            chunk_transcript_segs = [
                s for s in srt_segments_for_gemini
                if s.start_sec < chunk.end_sec and s.end_sec > chunk.start_sec
            ] if srt_segments_for_gemini else []


            prompt_payload = {
                "work_title": payload.work_title,
                "topic": payload.topic,
                "previous_episodes_context": payload.previous_episodes_context,
                "work_context": payload.work_context,
                "chunk_start_sec": chunk.start_sec,
                "chunk_end_sec": chunk.end_sec,
                "scene_boundaries": scene_boundaries,
                "video_path": str(split_path) if split_path else None,
                "previous_analyses": previous_analyses.copy(),
                "transcript_segments": chunk_transcript_segs,
                "narrative_skeleton": narrative_skeleton,
            }

            try:
                response = gemini.analyze_chunk(prompt_payload)
                chunk_elapsed = time.time() - chunk_start
                run_log["steps"].append({"step": "gemini", "chunk": chunk.index, "response": response})
                moment_count = len(response.get("candidate_moments", []))
                print(f"    → {moment_count}개 후보 모멘트 발견 (소요 시간: {chunk_elapsed:.1f}초)")


                previous_analyses.append({
                    "summary": response.get("summary", ""),
                    "candidate_moments": response.get("candidate_moments", []),
                })

                for moment in response["candidate_moments"]:
                    if chunk.start_sec == 0:
                        actual_cut_offset = 0.0
                    elif chunk.actual_start_sec is not None and chunk.actual_start_sec > 0:
                        actual_cut_offset = chunk.actual_start_sec
                    else:
                        actual_cut_offset = chunk.start_sec
                    moment["start_sec"] += actual_cut_offset
                    moment["end_sec"] += actual_cut_offset
                    moment["chunk_index"] = chunk.index
                    all_candidates.append(moment)
            finally:
                if split_path and split_path.exists():
                    try:
                        split_path.unlink()
                        print(f"    → 분할 파일 삭제 완료: {split_path.name}")
                    except Exception as e:
                        print(f"    [WARN] 분할 파일 삭제 실패: {split_path.name} ({e})")

        gemini_elapsed = time.time() - gemini_start


        checkpoint_gemini.write_text(
            json.dumps({
                "all_candidates": all_candidates,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] Gemini 분석 완료 (총 {len(all_candidates)}개 후보, 소요 시간: {gemini_elapsed:.1f}초)")
    else:
        if not checkpoint_gemini.exists():
            raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_gemini}")
        gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
        all_candidates = gemini_data["all_candidates"]
        for m in all_candidates:
            m.setdefault("story_role", "build")

    # ── 인트로/크레딧 포스트필터 (안전망) ──
    if exclusion_zones.detection_method != "none":
        before_count = len(all_candidates)
        all_candidates = filter_excluded_moments(all_candidates, exclusion_zones)
        if before_count != len(all_candidates):
            print(f"  인트로/크레딧 필터 적용: {before_count} → {len(all_candidates)}개 후보")

    # ═══════════════════════════════════════
    # [5.5/10] 관계 그래프 추출
    # ═══════════════════════════════════════
    checkpoint_graph = output_dir / "checkpoint_graph.json"
    relationship_edges: list[dict[str, Any]] = []

    if start_idx <= 5 and checkpoint_graph.exists() and from_step not in ("gemini", "graph"):
        print("\n[5.5/10] 관계 그래프 로드 중...")
        graph_data = json.loads(checkpoint_graph.read_text(encoding="utf-8"))
        relationship_edges = graph_data.get("edges", [])
        print(f"  - {len(relationship_edges)}개 관계 엣지 로드")
        print("[OK] 관계 그래프 로드 완료 (체크포인트에서)")
    elif start_idx <= 5:
        print("\n[5.5/10] 관계 그래프 추출 중...")
        gemini = load_gemini_client()
        relationship_edges = gemini.extract_relationships(all_candidates)
        checkpoint_graph.write_text(
            json.dumps({"edges": relationship_edges}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] 관계 그래프 추출 완료 ({len(relationship_edges)}개 엣지)")

    # ═══════════════════════════════════════
    # [6/10] 스토리 구성 (바이럴 최적화 — 멀티쇼츠)
    # ═══════════════════════════════════════
    # all_storyline_variants: list of (clips, title_text, score)
    all_storyline_variants: list[tuple[list[StoryClip], str, float]] = []
    max_shorts = min(payload.max_shorts, config.max_shorts_count)
    story_plan = None

    checkpoint_story = output_dir / "checkpoint_story.json"
    if start_idx <= 6 and checkpoint_story.exists() and from_step != "story":
        print("\n[6/10] 스토리 구성 결과 로드 중...")
        story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))

        # 멀티쇼츠 체크포인트 로드
        if "variants" in story_data:
            for v in story_data["variants"]:
                v_clips = [StoryClip(**c) for c in v["clips"]]
                all_storyline_variants.append((v_clips, v["title_text"], v.get("score", 0.0)))
            print(f"  - {len(all_storyline_variants)}개 스토리라인 로드")
        else:
            # 하위 호환: 이전 단일 체크포인트
            clips = [StoryClip(**clip) for clip in story_data["clips"]]
            title_text = story_data["title_text"]
            all_storyline_variants.append((clips, title_text, 1.0))
            print(f"  - {len(clips)}개 클립, 제목: {title_text}")
        print("[OK] 스토리 구성 결과 로드 완료 (체크포인트에서)")

    elif start_idx <= 6:
        print("\n[6/10] 스토리 구성 중...")
        gemini = load_gemini_client()
        story_start = time.time()

        # sequence_id 부여: continues_from + 관계 그래프 continuous 엣지 기반
        all_candidates = assign_sequence_ids(all_candidates, edges=relationship_edges or None)

        # ranking 임시 비활성화: compose_story에 전체 후보를 시간순 그대로 전달
        # ranked_candidates = rank_moments_enhanced(all_candidates, shorts_type="storytelling")
        ranked_dicts = [{**m, "final_score": 0.0} for m in all_candidates]

        # Gemini 바이럴 스토리 구성 (Flash + skeleton + 장르/포맷/포커스 블록)
        story_plan = gemini.compose_story_with_context(
            ranked_dicts,
            payload.work_title,
            payload.topic,
            min_duration_sec=config.min_duration_sec,
            max_duration_sec=config.max_duration_sec,
            work_context=payload.work_context,
            previous_episodes_context=payload.previous_episodes_context,
            narrative_skeleton=narrative_skeleton,
            relationship_edges=relationship_edges or None,
        )

        # narrative_plan 출력 (storyline별)
        for _sl in story_plan.get("storylines", []):
            _np = _sl.get("narrative_plan")
            if _np and isinstance(_np, dict):
                _idx = _sl.get("storyline_index", "?")
                _stype = _sl.get("shorts_type", "")
                print(f"\n  ── narrative_plan [storyline {_idx} / {_stype}] ──")
                for key, val in _np.items():
                    print(f"  [{key}] {val}")
                print("  " + "─" * 44)

        # 멀티쇼츠: ranked_storylines에서 최대 max_shorts개 추출
        ranked_storylines = story_plan.get("ranked_storylines", [])
        if not ranked_storylines:
            # 하위 호환: ranked_storylines가 없으면 selected_storyline 사용
            sel = story_plan.get("selected_storyline", {})
            if sel:
                ranked_storylines = [sel]

        for sl_idx, sl_data in enumerate(ranked_storylines[:max_shorts]):
            score = sl_data.get("score", 0.0)
            if score < config.viral_score_min_threshold and sl_idx > 0:
                print(f"  - 스토리라인 {sl_idx + 1} 스킵 (점수 {score:.2f} < 임계값 {config.viral_score_min_threshold})")
                continue

            try:
                sl_clips, sl_title = _clips_from_storyline(sl_data, payload.work_title)
            except (KeyError, TypeError) as e:
                print(f"  - 스토리라인 {sl_idx + 1} 파싱 실패: {e}")
                continue

            # 최상위 제목 (첫 번째 스토리라인만 story_plan의 title 사용 가능)
            if sl_idx == 0:
                top_line1 = _strip_emoji(story_plan.get("title_line1", ""))
                top_line2 = _strip_emoji(story_plan.get("title_line2", ""))
                if top_line1 and top_line2:
                    sl_title = f"{top_line1}\n{top_line2}"

            is_valid, msg = validate_story_clips(sl_clips, config.min_duration_sec, config.max_duration_sec)
            if not is_valid:
                print(f"  [WARN] 스토리라인 {sl_idx + 1} 검증 실패: {msg}")
            coh_warnings = validate_clip_coherence(sl_clips)
            for w in coh_warnings:
                print(f"  [COHERENCE] 스토리라인 {sl_idx + 1}: {w}")

            all_storyline_variants.append((sl_clips, sl_title, score))
            print(f"  - 스토리라인 {sl_idx + 1}: {len(sl_clips)}개 클립, 점수 {score:.2f}, 제목: {sl_title}")

        # 폴백: 유효한 스토리가 없으면 selected_storyline에서 1개 생성
        if not all_storyline_variants:
            sel = story_plan.get("selected_storyline", {})
            if sel:
                fb_clips, fb_title = _clips_from_storyline(sel, payload.work_title)
                all_storyline_variants.append((fb_clips, fb_title, sel.get("score", 0.5)))
                print(f"  - 폴백 스토리라인: {len(fb_clips)}개 클립")

        story_elapsed = time.time() - story_start
        print(f"  → 총 {len(all_storyline_variants)}개 쇼츠 생성 예정")

        # 체크포인트 저장
        checkpoint_data = {
            "raw_response": story_plan,
            "variants": [
                {"clips": [c.__dict__ for c in clips], "title_text": title, "score": score}
                for clips, title, score in all_storyline_variants
            ],
            # 하위 호환
            "clips": [c.__dict__ for c in all_storyline_variants[0][0]] if all_storyline_variants else [],
            "title_text": all_storyline_variants[0][1] if all_storyline_variants else "",
        }
        checkpoint_story.write_text(
            json.dumps(checkpoint_data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] 스토리 구성 완료 (소요 시간: {story_elapsed:.1f}초)")
    else:
        edit_plan_path = output_dir / "edit_plan.json"
        if checkpoint_story.exists():
            story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
            if "variants" in story_data:
                for v in story_data["variants"]:
                    v_clips = [StoryClip(**c) for c in v["clips"]]
                    all_storyline_variants.append((v_clips, v["title_text"], v.get("score", 0.0)))
            else:
                clips = [StoryClip(**clip) for clip in story_data["clips"]]
                title_text = story_data["title_text"]
                all_storyline_variants.append((clips, title_text, 1.0))
        elif edit_plan_path.exists():
            print("\n[6/10] 기존 파일에서 스토리 복원 중...")
            edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8"))
            clips = []
            for clip_data in edit_plan["timeline"]:
                clips.append(StoryClip(
                    role=clip_data["role"],
                    start_sec=clip_data["clip_start_sec"],
                    end_sec=clip_data["clip_end_sec"],
                    subtitle=clip_data["subtitle"],
                    tts_line=clip_data["tts"],
                    use_original_audio=clip_data["use_original_audio"],
                ))
            title_text = edit_plan["layout"]["top_title"]
            all_storyline_variants.append((clips, title_text, 1.0))
            print(f"  - {len(clips)}개 클립, 제목: {title_text}")
            print("[OK] 스토리 복원 완료 (edit_plan.json에서)")
        else:
            raise FileNotFoundError("체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다.")

    # 첫 번째 스토리라인을 기본 clips/title_text로 설정 (하위 호환)
    clips, title_text, _ = all_storyline_variants[0]

    # ═══════════════════════════════════════
    # [7/10] 선택된 클립 전사 + 무음 제거
    # ═══════════════════════════════════════
    transcript_text: list = []
    full_audio_path = output_dir / "full_audio.json"
    segments_cache_path = output_dir / "subtitle_segments.json"

    if start_idx <= 7 and full_audio_path.exists() and from_step != "transcribe":
        print("\n[7/10] 전사 데이터 로드 중...")
        loaded_data = json.loads(full_audio_path.read_text(encoding="utf-8"))
        transcript_text = [SimpleNamespace(**seg) for seg in loaded_data]
        print(f"  - {len(transcript_text)}개 세그먼트 로드 완료")

        # 무음 제거 (이미 전사 데이터가 있으므로 바로 실행)
        print("  무음 구간 제거 중...")
        cut_results = cut_silence_from_clips(clips, transcript_text, max_gap_sec=0.8, padding_sec=0.15)
        print_silence_cut_summary(cut_results)
        clips = flatten_to_clips(cut_results)
        print(f"[OK] 전사 로드 + 무음 제거 완료 ({len(clips)}개 클립)")

    elif start_idx <= 7:
        print("\n[7/10] 선택된 클립 구간 전사 중...")
        transcribe_start = time.time()

        if payload.srt_path:
            print(f"  SRT 파일 사용 중: {payload.srt_path}")
            all_srt_segments = parse_subtitle(payload.srt_path)
            # 선택된 클립 범위에 해당하는 세그먼트만 필터링
            for clip in clips:
                for seg in all_srt_segments:
                    if seg.end_sec > clip.start_sec and seg.start_sec < clip.end_sec:
                        transcript_text.append(seg)
            print(f"  - SRT 필터링 완료: {len(transcript_text)}개 세그먼트")
        else:
            print("  Whisper 전사 중... (선택된 클립만)")
            # 리서치 결과에서 인물명 추출 (Whisper 프롬프트용)
            _char_names = list(dict.fromkeys(
                [ci.character_name for ci in cast_images if ci.character_name]
                + [ci.actor_name for ci in cast_images if ci.actor_name]
            ))
            for idx, clip in enumerate(clips):
                clip_audio_path = output_dir / f"clip_audio_{idx}.wav"
                try:
                    _, actual_start = extract_audio_segment(
                        payload.video_path,
                        clip_audio_path,
                        start_sec=clip.start_sec,
                        end_sec=clip.end_sec,
                        padding_sec=2.0,
                    )
                    segments = extract_transcript(
                        clip_audio_path,
                        work_title=payload.work_title,
                        character_names=_char_names,
                        work_context=payload.work_context,
                    )
                    for seg in segments:
                        # 패딩 오프셋 보정: actual_start는 (clip.start_sec - padding) 일 수 있음
                        adjusted_start = seg.start_sec + actual_start
                        adjusted_end = seg.end_sec + actual_start
                        # 실제 클립 범위 내의 세그먼트만 유지
                        if adjusted_end > clip.start_sec and adjusted_start < clip.end_sec:
                            transcript_text.append(SpeechSegment(
                                start_sec=max(adjusted_start, clip.start_sec),
                                end_sec=min(adjusted_end, clip.end_sec),
                                text=seg.text,
                            ))
                    print(f"    클립 {idx + 1}/{len(clips)} 전사 완료")
                finally:
                    if clip_audio_path.exists():
                        clip_audio_path.unlink()

        # Whisper 실패 폴백: 전사 결과가 없으면 Gemini 분석의 대사 데이터 활용
        used_gemini_fallback = False
        if not transcript_text and all_candidates:
            used_gemini_fallback = True
            print("  [FALLBACK] Whisper 전사 결과 없음 — Gemini 대사 데이터로 자막 생성")
            for clip in clips:
                for m in all_candidates:
                    m_start = m.get("start_sec", 0)
                    m_end = m.get("end_sec", 0)
                    # 클립 범위와 겹치는 moment의 transcript 사용
                    if m_end > clip.start_sec and m_start < clip.end_sec and m.get("transcript"):
                        # 대사를 클립 전체 구간에 매핑 (무음제거 방지)
                        transcript_text.append(SpeechSegment(
                            start_sec=clip.start_sec,
                            end_sec=clip.end_sec,
                            text=m["transcript"],
                        ))
                        break  # 클립당 1개만
            if transcript_text:
                print(f"    Gemini 대사 폴백: {len(transcript_text)}개 세그먼트 생성")

        # 전사 데이터 저장
        save_data = [{"start_sec": s.start_sec, "end_sec": s.end_sec, "text": s.text} for s in transcript_text]
        full_audio_path.write_text(json.dumps(save_data, ensure_ascii=False, indent=2), encoding="utf-8")

        transcribe_elapsed = time.time() - transcribe_start
        print(f"  - 전사 완료: {len(transcript_text)}개 세그먼트 (소요 시간: {transcribe_elapsed:.1f}초)")

        # 무음 구간 제거 (Gemini 폴백 시 건너뜀 — 타이밍이 부정확하므로)
        if used_gemini_fallback:
            print("  무음 제거 건너뜀 (Gemini 폴백 데이터는 타이밍이 부정확)")
        else:
            print("  무음 구간 제거 중...")
            cut_results = cut_silence_from_clips(clips, transcript_text, max_gap_sec=0.8, padding_sec=0.15)
            print_silence_cut_summary(cut_results)
            clips = flatten_to_clips(cut_results)
        print(f"[OK] 전사 완료 ({len(clips)}개 클립)")
    else:
        # 이전 단계 데이터 로드
        if full_audio_path.exists():
            loaded_data = json.loads(full_audio_path.read_text(encoding="utf-8"))
            transcript_text = [SimpleNamespace(**seg) for seg in loaded_data]
        print(f"\n[7/10] 전사 단계 건너뜀 ({len(transcript_text)}개 세그먼트 로드)")

    # 자막 데이터 생성 (전사 → 편집 타임라인 매핑)
    final_segments = []
    if start_idx <= 8 and not (segments_cache_path.exists() and from_step not in ("transcribe", "resources")):
        print("  자막 타임라인 매핑 중...")
        remapped = remap_transcript_to_edited_timeline(
            clips, transcript_text, tts_only_when_no_orig=True,
        )
        merged_segments = merge_subtitle_segments(
            remapped,
            max_gap_sec=0.25,
            max_total_chars=int(config.subtitle_max_chars_per_line * config.subtitle_max_lines),
        )
        for seg in merged_segments:
            seg_dict = {
                'start_sec': seg.get('start', seg.get('start_sec')) if isinstance(seg, dict) else seg.start_sec,
                'end_sec': seg.get('end', seg.get('end_sec')) if isinstance(seg, dict) else seg.end_sec,
                'text': seg.get('text', "") if isinstance(seg, dict) else seg.text,
            }
            final_segments.append(SimpleNamespace(**seg_dict))
        segments_cache_path.write_text(
            json.dumps([{"start_sec": s.start_sec, "end_sec": s.end_sec, "text": s.text} for s in final_segments],
                       ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"  자막 데이터 준비 완료 ({len(final_segments)} segments)")
    elif segments_cache_path.exists():
        cached_data = json.loads(segments_cache_path.read_text(encoding="utf-8"))
        final_segments = [SimpleNamespace(**seg) for seg in cached_data]
        print(f"  자막 캐시 로드 완료 ({len(final_segments)} segments)")

    # ═══════════════════════════════════════
    # [8/10] 리소스 생성 (크롭, TTS, 편집 계획)
    # ═══════════════════════════════════════
    checkpoint_resources = output_dir / "checkpoint_resources.json"
    edit_plan_path = output_dir / "edit_plan.json"

    if start_idx <= 8 and checkpoint_resources.exists() and from_step != "resources":
        print("\n[8/10] 리소스 로드 중...")
        resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
        crop_map = {k: Path(v) for k, v in resources_data["crop_map"].items()}
        tts_audio_files = {
            int(k): Path(v) for k, v in resources_data.get("tts_audio_files", {}).items()
        } if "tts_audio_files" in resources_data else {}
        print(f"  - 크롭 타임라인: {len(crop_map)}개, TTS 오디오: {len(tts_audio_files)}개")
        print("[OK] 리소스 로드 완료 (체크포인트에서)")
    elif start_idx <= 8:
        print("\n[8/10] 리소스 생성 중...")
        resource_start = time.time()

        # Phase 12: 인물 인식 레퍼런스 빌드 (배우 사진이 있을 때만)
        face_identifier = None
        if cast_images and payload.design.enable_face_recognition:
            try:
                from app.modules.face_id import FaceIdentifier
                fi = FaceIdentifier()
                fi.build_references(cast_images)
                if fi.references:
                    face_identifier = fi
                    print(f"  [FaceID] 인물 인식 레퍼런스: {len(fi.references)}명")
                else:
                    print("  [FaceID] 유효한 레퍼런스 없음 — 화자 추적 폴백")
            except ImportError:
                print("  [FaceID] deepface 미설치 — 화자 추적 폴백")
            except Exception as e:
                print(f"  [FaceID] 초기화 실패: {e} — 화자 추적 폴백")

        # 얼굴 크롭 타임라인
        crop_map = {}
        print(f"  크롭 타임라인 생성 중... ({len(clips)}개 클립)")
        for idx, clip in enumerate(clips):
            crop_path = output_dir / f"crop_{clip.role}_{idx}.json"
            # Phase 12: character_focus 첫 번째 인물을 타겟으로
            target_char = clip.character_focus[0] if clip.character_focus and face_identifier else None
            build_crop_timeline(
                payload.video_path.resolve(),
                crop_path,
                media_info.width,
                media_info.height,
                config.crop_sample_interval_sec,
                start_sec=clip.start_sec,
                end_sec=clip.end_sec,
                enable_speaker_tracking=payload.design.enable_speaker_tracking,
                target_character=target_char,
                face_identifier=face_identifier,
            )
            crop_map[f"{clip.role}_{idx}"] = crop_path
            if (idx + 1) % 5 == 0 or (idx + 1) == len(clips):
                print(f"    진행 중... ({idx + 1}/{len(clips)})")

        # TTS 오디오 생성
        print("  TTS 오디오 생성 중...")
        tts_audio_files = {}
        for idx, clip in enumerate(clips):
            if clip.tts_line:
                tts_path = output_dir / f"tts_{idx}.mp3"
                synthesize_tts(clip.tts_line, tts_path, lang=payload.language)
                tts_audio_files[idx] = tts_path
                if (idx + 1) % 3 == 0 or (idx + 1) == len(clips):
                    print(f"    진행 중... ({idx + 1}/{len(clips)})")

        resource_elapsed = time.time() - resource_start
        checkpoint_resources.write_text(
            json.dumps({
                "crop_map": {k: str(v) for k, v in crop_map.items()},
                "tts_audio_files": {str(k): str(v) for k, v in tts_audio_files.items()},
            }, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] 리소스 생성 완료 (소요 시간: {resource_elapsed:.1f}초)")
        print(f"  - TTS 오디오: {len(tts_audio_files)}개")
    else:
        if checkpoint_resources.exists():
            resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
            crop_map = {k: Path(v) for k, v in resources_data["crop_map"].items()}
            tts_audio_files = {
                int(k): Path(v) for k, v in resources_data.get("tts_audio_files", {}).items()
            } if "tts_audio_files" in resources_data else {}
        elif edit_plan_path.exists():
            edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8"))
            crop_map = {}
            for idx, clip_data in enumerate(edit_plan["timeline"]):
                crop_filename = clip_data["reframe"]["crop_timeline_ref"]
                crop_path = output_dir / crop_filename
                if crop_path.exists():
                    crop_map[f"{clip_data['role']}_{idx}"] = crop_path
            tts_audio_files = {}
        else:
            raise FileNotFoundError("체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다.")

    # 편집 계획 생성
    if start_idx <= 8:
        print("  편집 계획 생성 중...")
        edit_plan = _build_edit_plan(payload, title_text, clips, crop_map, config)
        if narrative_skeleton:
            edit_plan["narrative_skeleton"] = narrative_skeleton
        edit_plan_path.write_text(json.dumps(edit_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  - 편집 계획 저장: {edit_plan_path}")

    # ═══════════════════════════════════════
    # [9/10] 자막 디자인 + 최종 렌더링
    # ═══════════════════════════════════════
    output_video = output_dir / "shorts.mp4"
    subtitle_path = output_dir / "subtitles.ass"

    if start_idx <= 9 or from_step == "render" or not output_video.exists():
        # 자막 디자인 적용
        print("\n[9/10] 자막 디자인 적용 및 최종 렌더링 중...")

        if not final_segments and segments_cache_path.exists():
            cached_data = json.loads(segments_cache_path.read_text(encoding="utf-8"))
            final_segments = [SimpleNamespace(**seg) for seg in cached_data]

        tts_subtitle_path = None
        if final_segments:
            sub_style = SubtitleStyle(
                font_name=payload.design.subtitle_font,
                font_size=payload.design.subtitle_size,
                primary_color=payload.design.subtitle_color,
                margin_v=payload.design.subtitle_y_margin,
            )

            # TTS 자막 세그먼트 생성
            tts_line_segs: list[SimpleNamespace] = []
            _tts_files = tts_audio_files if "tts_audio_files" in locals() else {}
            _t = 0.0
            for _idx, _clip in enumerate(clips):
                _dur = max(0.0, _clip.end_sec - _clip.start_sec)
                if _dur > 0 and _clip.tts_line:
                    _tts_path = _tts_files.get(_idx)
                    if _tts_path and Path(_tts_path).exists():
                        _tts_dur = min(_get_audio_duration(Path(_tts_path)), _dur)
                    else:
                        _tts_dur = _dur
                    tts_line_segs.append(SimpleNamespace(start_sec=_t, end_sec=_t + _tts_dur, text=_clip.tts_line))
                _t += _dur

            tts_line_style = SubtitleStyle(
                font_name=payload.design.subtitle_font,
                font_size=payload.design.tts_line_font_size,
                primary_color=payload.design.tts_line_color,
                margin_v=payload.design.tts_line_y_margin,
            ) if tts_line_segs else None

            # TTS 활성 시간 범위 계산 (메인 자막 숨김용)
            tts_time_ranges = [(seg.start_sec, seg.end_sec) for seg in tts_line_segs] if tts_line_segs else None

            build_ass_from_segments(final_segments, subtitle_path, sub_style, tts_time_ranges=tts_time_ranges)
            print(f"  [OK] 자막 파일 생성 완료: {subtitle_path}")

            tts_subtitle_path = output_dir / "tts_subtitles.ass"
            if tts_line_segs and tts_line_style:
                build_tts_ass(tts_line_segs, tts_subtitle_path, tts_line_style)
                print(f"  [OK] TTS 자막 파일 생성 완료: {tts_subtitle_path}")
            else:
                tts_subtitle_path = None
        else:
            print("  - [주의] 자막 데이터가 없어 .ass 생성을 건너뜁니다.")

        # 최종 렌더링
        print(f"  최종 영상 렌더링 중... (출력: {output_video})")
        render_start = time.time()
        tts_files_map = tts_audio_files if "tts_audio_files" in locals() else {}
        actual_font_path = get_font_path(payload.design.title_font, paths.app_root)
        actual_subtitle_font_path = get_font_path(payload.design.subtitle_font, paths.app_root)
        updated_design = replace(
            payload.design,
            title_font=actual_font_path,
            subtitle_font=actual_subtitle_font_path,
        )

        render_inputs = RenderInputs(
            video_path=payload.video_path,
            clips=clips,
            subtitle_path=subtitle_path if (payload.show_subtitles and subtitle_path.exists()) else None,
            crop_timeline_map=crop_map,
            title_text=title_text,
            work_title=payload.work_title,
            design=updated_design,
            output_path=output_video,
            canvas_width=config.canvas_width,
            canvas_height=config.canvas_height,
            top_title_height=config.top_title_height,
            bottom_label_height=config.bottom_label_height,
            tts_subtitle_path=tts_subtitle_path,
            tts_audio_files=tts_files_map if tts_files_map else None,
            original_audio_gain_db=config.original_gain_db,
            tts_audio_gain_db=config.tts_gain_db,
            render_preset=config.render_preset,
            enable_hwaccel=config.enable_hwaccel,
        )

        ffmpeg_cmd = render_short(render_inputs)
        render_elapsed = time.time() - render_start

        cmd_serializable = [str(item) if isinstance(item, Path) else item for item in ffmpeg_cmd]
        run_log["steps"].append({"step": "render", "command": cmd_serializable})
        print(f"[OK] 최종 렌더링 완료 (소요 시간: {render_elapsed:.1f}초)")
    else:
        print(f"\n[9/10] 렌더링 스킵 (이미 파일 존재: {output_video.name})")

    # ═══════════════════════════════════════
    # [10/10] 출력 검증
    # ═══════════════════════════════════════
    if start_idx <= 10:
        print("\n[10/10] 출력 검증 중...")
        if not output_video.exists():
            raise FileNotFoundError(f"검증할 영상 파일을 찾을 수 없습니다: {output_video}")

        validation = validate_output(
            output_video,
            config.min_duration_sec,
            config.max_duration_sec,
        )
        validation_dict = validation.__dict__.copy()
        for key, value in validation_dict.items():
            if isinstance(value, Path):
                validation_dict[key] = str(value)
        run_log["steps"].append({"step": "validate", "result": validation_dict})
        print(f"  - 길이 검증: {'OK' if validation.duration_ok else 'FAIL'}")
        print(f"  - 오디오 피크 검증: {'OK' if validation.audio_peak_ok else 'FAIL'}")
        print(f"  - 검은 프레임 검증: {'OK' if validation.black_frames_ok else 'FAIL'}")
        print("[OK] 검증 완료")
    else:
        print("\n[10/10] 검증 단계 스킵")

    # ═══════════════════════════════════════
    # 추가 쇼츠 렌더링 (2번째, 3번째 스토리라인)
    # ═══════════════════════════════════════
    all_output_videos: list[Path] = [output_video]

    if len(all_storyline_variants) > 1 and start_idx <= 9:
        print(f"\n추가 쇼츠 렌더링 ({len(all_storyline_variants) - 1}개)...")

        # variant 쇼츠도 쇼츠 #1과 동일하게 라인 단위 SRT 기반 자막을 사용하도록 미리 파싱
        srt_segments_for_variants: list[SpeechSegment] = []
        if payload.srt_path and payload.srt_path.exists():
            try:
                srt_segments_for_variants = parse_subtitle(payload.srt_path)
            except Exception as _exc:
                print(f"  [경고] variant용 SRT 재파싱 실패: {_exc}")

        for var_idx in range(1, len(all_storyline_variants)):
            var_clips, var_title, var_score = all_storyline_variants[var_idx]
            var_num = var_idx + 1
            var_video = output_dir / f"shorts_{var_num}.mp4"
            print(f"\n  ── 쇼츠 #{var_num} (점수: {var_score:.2f}) ──")
            print(f"  제목: {var_title}")

            if var_video.exists():
                print(f"  → 이미 존재: {var_video.name}")
                all_output_videos.append(var_video)
                continue

            try:
                var_start = time.time()

                # 전사: SRT가 있으면 라인 단위 SRT를 그대로 사용 (remap이 clip 범위로 잘라냄).
                # SRT가 없을 때만 Gemini candidate transcript로 폴백.
                var_transcript: list = []
                if srt_segments_for_variants:
                    var_transcript = list(srt_segments_for_variants)
                elif all_candidates:
                    for clip in var_clips:
                        for m in all_candidates:
                            m_start = float(m.get("start_sec", 0))
                            m_end = float(m.get("end_sec", 0))
                            if m_end > clip.start_sec and m_start < clip.end_sec and m.get("transcript"):
                                # 실제 candidate 구간 시간을 보존 (clip 전체 덮어쓰기 금지)
                                var_transcript.append(SpeechSegment(
                                    start_sec=m_start, end_sec=m_end, text=m["transcript"],
                                ))
                                break

                # 자막 타임라인 매핑
                var_remapped = remap_transcript_to_edited_timeline(var_clips, var_transcript, tts_only_when_no_orig=True)
                var_merged = merge_subtitle_segments(
                    var_remapped, max_gap_sec=0.25,
                    max_total_chars=int(config.subtitle_max_chars_per_line * config.subtitle_max_lines),
                )
                var_final_segs = [
                    SimpleNamespace(
                        start_sec=s.get("start", s.get("start_sec")) if isinstance(s, dict) else s.start_sec,
                        end_sec=s.get("end", s.get("end_sec")) if isinstance(s, dict) else s.end_sec,
                        text=s.get("text", "") if isinstance(s, dict) else s.text,
                    ) for s in var_merged
                ]

                # TTS 생성
                var_tts_files: dict[int, Path] = {}
                for tidx, tclip in enumerate(var_clips):
                    if tclip.tts_line:
                        tts_out = output_dir / f"tts_{var_num}_{tidx}.mp3"
                        if not tts_out.exists():
                            synthesize_tts(tclip.tts_line, tts_out, lang=payload.language)
                        var_tts_files[tidx] = tts_out

                # TTS 자막
                var_tts_segs: list[SimpleNamespace] = []
                _vt = 0.0
                for _vi, _vc in enumerate(var_clips):
                    _vdur = max(0.0, _vc.end_sec - _vc.start_sec)
                    if _vdur > 0 and _vc.tts_line:
                        _vtp = var_tts_files.get(_vi)
                        if _vtp and _vtp.exists():
                            _vtd = min(_get_audio_duration(Path(_vtp)), _vdur)
                        else:
                            _vtd = _vdur
                        var_tts_segs.append(SimpleNamespace(start_sec=_vt, end_sec=_vt + _vtd, text=_vc.tts_line))
                    _vt += _vdur

                # 자막 ASS 파일 생성
                var_sub_path = output_dir / f"subtitles_{var_num}.ass"
                var_tts_sub_path = output_dir / f"tts_subtitles_{var_num}.ass"

                sub_style = SubtitleStyle(
                    font_name=payload.design.subtitle_font,
                    font_size=payload.design.subtitle_size,
                    primary_color=payload.design.subtitle_color,
                    margin_v=payload.design.subtitle_y_margin,
                )
                var_tts_ranges = [(s.start_sec, s.end_sec) for s in var_tts_segs] if var_tts_segs else None
                build_ass_from_segments(var_final_segs, var_sub_path, sub_style, tts_time_ranges=var_tts_ranges)

                var_tts_line_style = SubtitleStyle(
                    font_name=payload.design.subtitle_font,
                    font_size=payload.design.tts_line_font_size,
                    primary_color=payload.design.tts_line_color,
                    margin_v=payload.design.tts_line_y_margin,
                ) if var_tts_segs else None

                var_tts_sub_final = None
                if var_tts_segs and var_tts_line_style:
                    build_tts_ass(var_tts_segs, var_tts_sub_path, var_tts_line_style)
                    var_tts_sub_final = var_tts_sub_path

                # 얼굴 크롭 타임라인
                var_crop_map = {}
                for cidx, cclip in enumerate(var_clips):
                    crop_file = output_dir / f"crop_{var_num}_{cclip.role}_{cidx}.json"
                    var_target_char = cclip.character_focus[0] if cclip.character_focus and face_identifier else None
                    build_crop_timeline(
                        payload.video_path.resolve(),
                        crop_file,
                        media_info.width,
                        media_info.height,
                        config.crop_sample_interval_sec,
                        start_sec=cclip.start_sec,
                        end_sec=cclip.end_sec,
                        enable_speaker_tracking=payload.design.enable_speaker_tracking,
                        target_character=var_target_char,
                        face_identifier=face_identifier,
                    )
                    var_crop_map[f"{cclip.role}_{cidx}"] = crop_file

                # 렌더링
                actual_font_path = get_font_path(payload.design.title_font, paths.app_root)
                actual_subtitle_font_path = get_font_path(payload.design.subtitle_font, paths.app_root)
                updated_design = replace(
                    payload.design,
                    title_font=actual_font_path,
                    subtitle_font=actual_subtitle_font_path,
                )

                var_render_inputs = RenderInputs(
                    video_path=payload.video_path,
                    clips=var_clips,
                    subtitle_path=var_sub_path if (payload.show_subtitles and var_sub_path.exists()) else None,
                    crop_timeline_map=var_crop_map,
                    title_text=var_title,
                    work_title=payload.work_title,
                    design=updated_design,
                    output_path=var_video,
                    canvas_width=config.canvas_width,
                    canvas_height=config.canvas_height,
                    top_title_height=config.top_title_height,
                    bottom_label_height=config.bottom_label_height,
                    tts_subtitle_path=var_tts_sub_final,
                    tts_audio_files=var_tts_files if var_tts_files else None,
                    original_audio_gain_db=config.original_gain_db,
                    tts_audio_gain_db=config.tts_gain_db,
                    render_preset=config.render_preset,
                    enable_hwaccel=config.enable_hwaccel,
                )

                render_short(var_render_inputs)
                var_elapsed = time.time() - var_start
                all_output_videos.append(var_video)
                print(f"  [OK] 쇼츠 #{var_num} 렌더링 완료 ({var_elapsed:.1f}초)")

            except Exception as e:
                print(f"  [ERROR] 쇼츠 #{var_num} 렌더링 실패: {e}")
                continue

    # 최종 로그 저장
    def _make_json_serializable(obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        elif isinstance(obj, dict):
            return {k: _make_json_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_make_json_serializable(item) for item in obj]
        elif isinstance(obj, tuple):
            return tuple(_make_json_serializable(item) for item in obj)
        else:
            return obj

    run_log_serializable = _make_json_serializable(run_log)
    run_log_path = output_dir / "run_log.json"
    run_log_path.write_text(json.dumps(run_log_serializable, ensure_ascii=False, indent=2), encoding="utf-8")

    total_elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("파이프라인 완료")
    print("=" * 60)
    print(f"총 소요 시간: {total_elapsed:.1f}초 ({total_elapsed / 60:.1f}분)")
    print(f"\n출력 파일 ({len(all_output_videos)}개 쇼츠):")
    for idx, vp in enumerate(all_output_videos, 1):
        print(f"  - 쇼츠 #{idx}: {vp}")
    print(f"  - 편집 계획: {edit_plan_path}")
    print(f"  - 실행 로그: {run_log_path}")
    print("=" * 60)

    return PipelineOutput(
        output_videos=all_output_videos,
        edit_plan_path=edit_plan_path,
        run_log_path=run_log_path,
    )


# ─────────────────────────────────────────────
# 유틸리티 함수
# ─────────────────────────────────────────────

def _snap_to_scenes(clips: list[StoryClip], scenes, threshold: float) -> list[StoryClip]:
    boundaries = sorted({scene.start_sec for scene in scenes} | {scene.end_sec for scene in scenes})
    if not boundaries:
        return clips
    snapped = []
    for clip in clips:
        start = _snap_time(clip.start_sec, boundaries, threshold)
        end = _snap_time(clip.end_sec, boundaries, threshold)
        if end - start <= 0.2:
            start, end = clip.start_sec, clip.end_sec
        snapped.append(
            StoryClip(
                role=clip.role,
                start_sec=start,
                end_sec=end,
                subtitle=clip.subtitle,
                tts_line=clip.tts_line,
                use_original_audio=clip.use_original_audio,
            )
        )
    return snapped


def _snap_time(value: float, boundaries: list[float], threshold: float) -> float:
    closest = min(boundaries, key=lambda b: abs(b - value))
    if abs(closest - value) <= threshold:
        return closest
    return value


def _build_edit_plan(
    payload: PipelineInput,
    title_text: str,
    clips: list[StoryClip],
    crop_map: dict[str, Path],
    config: AppConfig,
) -> dict[str, Any]:
    timeline = []
    for idx, clip in enumerate(clips):
        timeline.append(
            {
                "role": clip.role,
                "clip_start_sec": clip.start_sec,
                "clip_end_sec": clip.end_sec,
                "subtitle": clip.subtitle,
                "tts": clip.tts_line,
                "use_original_audio": clip.use_original_audio,
                "reframe": {
                    "mode": "face_track",
                    "crop_timeline_ref": crop_map[f"{clip.role}_{idx}"].name,
                },
            }
        )
    return {
        "input": {
            "video_path": str(payload.video_path),
            "work_title": payload.work_title,
            "topic": payload.topic,
            "language": payload.language,
        },
        "layout": {
            "canvas": f"{config.canvas_width}x{config.canvas_height}",
            "top_title": title_text,
            "bottom_label": payload.work_title,
            "background_style": "blur",
        },
        "timeline": timeline,
        "audio_mix": {
            "tts_gain_db": config.tts_gain_db,
            "original_gain_db": config.original_gain_db,
            "bgm_gain_db": config.bgm_gain_db,
        },
    }
