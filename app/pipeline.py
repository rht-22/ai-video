# # from __future__ import annotations

# # import json
# # import time
# # import uuid
# # from dataclasses import dataclass
# # from pathlib import Path
# # from typing import Any

# # from app.config import AppConfig, Paths
# # from app.modules.chunker import build_chunks, split_video_chunk
# # from app.modules.gemini_client import load_gemini_client
# # from app.modules.media_probe import probe_media
# # from app.modules.moment_ranker import rank_moments
# # from app.modules.reframe import build_crop_timeline
# # from app.modules.renderer import RenderInputs, render_short
# # from app.modules.scene_detect import detect_scenes
# # # from app.modules.speech import extract_audio_from_video, extract_transcript
# # from app.modules.story_builder import StoryClip, build_story
# # from app.modules.subtitle import (
# #     SubtitleStyle,
# #     SubtitleSegment,
# #     build_ass,
# #     build_ass_from_segments,
# #     merge_subtitle_segments,
# #     remap_transcript_to_edited_timeline,
# # )
# # from app.modules.tts import synthesize_tts
# # from app.modules.validator import validate_output


# # @dataclass(frozen=True)
# # class PipelineInput:
# #     video_path: Path
# #     work_title: str
# #     topic: str
# #     outdir: Path
# #     # design: DesignConfig = field(default_factory=DesignConfig)
# #     tone: str = "drama_variety"
# #     language: str = "ko"


# # @dataclass(frozen=True)
# # class PipelineOutput:
# #     output_video: Path
# #     edit_plan_path: Path
# #     run_log_path: Path


# # def run_pipeline(payload: PipelineInput, from_step: str | None = None, job_id: str | None = None) -> PipelineOutput:
# #     print("=" * 60)
# #     print("파이프라인 시작")
# #     print("=" * 60)
    
# #     start_time = time.time()
# #     config = AppConfig()
# #     paths = Paths(app_root=Path(__file__).resolve().parent)
    
# #     # 초기화 단계
# #     print("\n[1/13] 초기화 중...")
# #     if job_id:
# #         # 기존 작업 재개
# #         output_dir = payload.outdir / job_id
# #         if not output_dir.exists():
# #             raise ValueError(f"Job ID {job_id}의 디렉토리를 찾을 수 없습니다: {output_dir}")
# #         print(f"  - 기존 작업 재개: {job_id}")
# #         print(f"  - 출력 디렉토리: {output_dir}")
# #         # 기존 run_log 로드
# #         run_log_path = output_dir / "run_log.json"
# #         if run_log_path.exists():
# #             run_log = json.loads(run_log_path.read_text(encoding="utf-8"))
# #         else:
# #             run_log = {
# #                 "job_id": job_id,
# #                 "input": {
# #                     "video_path": str(payload.video_path),
# #                     "work_title": payload.work_title,
# #                     "topic": payload.topic,
# #                     "tone": payload.tone,
# #                     "language": payload.language,
# #                 },
# #                 "steps": [],
# #             }
# #     else:
# #         # 새 작업 시작
# #         job_id = uuid.uuid4().hex[:8]
# #         output_dir = payload.outdir / job_id
# #         output_dir.mkdir(parents=True, exist_ok=True)
# #         run_log = {
# #             "job_id": job_id,
# #             "input": {
# #                 "video_path": str(payload.video_path),
# #                 "work_title": payload.work_title,
# #                 "topic": payload.topic,
# #                 "tone": payload.tone,
# #                 "language": payload.language,
# #             },
# #             "steps": [],
# #         }
# #         print(f"  - Job ID: {job_id}")
# #         print(f"  - 출력 디렉토리: {output_dir}")
# #     print("[OK] 초기화 완료")
    
# #     # 단계별 실행 플래그
# #     step_order = ["init", "probe", "full_analysis", "storyline", "chunk", "gemini", "story", "resources", "temp_render", "extract_audio", "regenerate_subtitles", "final_render", "validate"]
# #     if from_step:
# #         start_idx = step_order.index(from_step)
# #         print(f"\n[WARN] {from_step} 단계부터 재시작합니다.")
# #     else:
# #         start_idx = 0

# #     # 미디어 프로브 단계
# #     checkpoint_probe = output_dir / "checkpoint_probe.json"
# #     if start_idx <= 1 and checkpoint_probe.exists() and from_step != "probe":
# #         print("\n[2/13] 미디어 정보 로드 중...")
# #         probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
# #         from app.modules.media_probe import MediaInfo
# #         media_info = MediaInfo(**probe_data)
# #         print(f"  - 영상 길이: {media_info.duration_sec:.1f}초")
# #         print(f"  - 해상도: {media_info.width}x{media_info.height}")
# #         print(f"  - FPS: {media_info.fps:.2f}")
# #         print(f"  - 오디오: {'있음' if media_info.has_audio else '없음'}")
# #         print("[OK] 미디어 정보 로드 완료 (체크포인트에서)")
# #     elif start_idx <= 1:
# #         print("\n[2/13] 미디어 정보 수집 중...")
# #         probe_start = time.time()
# #         media_info = probe_media(payload.video_path)
# #         probe_elapsed = time.time() - probe_start
# #         # Path 객체를 문자열로 변환하여 JSON 직렬화
# #         probe_dict = media_info.__dict__.copy()
# #         probe_dict["path"] = str(probe_dict["path"])
# #         run_log["steps"].append({"step": "probe", "result": probe_dict})
# #         checkpoint_probe.write_text(json.dumps(probe_dict, ensure_ascii=False, indent=2), encoding="utf-8")
# #         print(f"  - 영상 길이: {media_info.duration_sec:.1f}초")
# #         print(f"  - 해상도: {media_info.width}x{media_info.height}")
# #         print(f"  - FPS: {media_info.fps:.2f}")
# #         print(f"  - 오디오: {'있음' if media_info.has_audio else '없음'}")
# #         print(f"[OK] 미디어 프로브 완료 (소요 시간: {probe_elapsed:.1f}초)")
# #     else:
# #         # 이전 단계에서 로드 (필수)
# #         if not checkpoint_probe.exists():
# #             raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_probe}. 이전 단계를 먼저 실행하세요.")
# #         probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
# #         from app.modules.media_probe import MediaInfo
# #         media_info = MediaInfo(**probe_data)

# #     # 전체 영상 분석 단계
# #     # checkpoint_full_analysis = output_dir / "checkpoint_full_analysis.json"
# #     full_summary = None
# #     key_scenes = None
# #     emotion_arc = None
    

# #     # 청크 분할 단계
# #     print("\n[5/13] 영상 청크 분할 중...")
# #     chunks = build_chunks(
# #         payload.video_path,
# #         media_info.duration_sec,
# #         config.chunk_seconds,
# #         config.chunk_overlap,
# #     )
# #     print(f"  - 총 {len(chunks)}개 청크 생성")
    
# #     # 실제 영상 파일 분할
# #     from dataclasses import replace
# #     split_chunks = []
# #     for i, chunk in enumerate(chunks, 1):
# #         print(f"    청크 {i} 분할 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
# #         split_path = split_video_chunk(
# #             payload.video_path,
# #             chunk.start_sec,
# #             chunk.end_sec,
# #         )
# #         # Chunk 객체에 분할된 파일 경로 추가
# #         split_chunk = replace(chunk, split_path=split_path)
# #         split_chunks.append(split_chunk)
# #         print(f"      → {split_path.name} 생성 완료")
    
# #     chunks = split_chunks
# #     print("[OK] 청크 분할 완료")
    
# #     # Gemini 클라이언트 로드
# #     print("\n[6/13] Gemini 분석 준비 중...")
# #     gemini = load_gemini_client()
# #     print("[OK] Gemini 클라이언트 로드 완료")

# #     # Gemini 분석 단계
# #     checkpoint_gemini = output_dir / "checkpoint_gemini.json"
# #     if start_idx <= 5 and checkpoint_gemini.exists() and from_step != "gemini":
# #         print("\n[6/13] Gemini 분석 결과 로드 중...")
# #         gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
# #         all_candidates = []
# #         title_candidates = []
# #         previous_analyses = []
        
        
# #         for idx, chunk in enumerate(chunks, 1):
# #             print(f"  청크 {idx}/{len(chunks)} 분석 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
# #             chunk_start = time.time()
# #             scenes = detect_scenes(payload.video_path, media_info.fps, chunk.end_sec - chunk.start_sec)
# #             scene_boundaries = [scene.start_sec + chunk.start_sec for scene in scenes]
            
# #             # 분할된 파일 경로 가져오기
# #             split_path = chunk.split_path if chunk.split_path else None
            
          
# #             prompt_payload = {
# #                 "work_title": payload.work_title,
# #                 "topic": payload.topic,
# #                 "chunk_start_sec": chunk.start_sec,
# #                 "chunk_end_sec": chunk.end_sec,
# #                 "scene_boundaries": scene_boundaries,
# #                 "video_path": str(split_path) if split_path else None
# #             }
            
            

# #     # 스토리 구성 단계
# #     checkpoint_gemini = output_dir / "checkpoint_gemini.json"
    

# #     # [6/13] Gemini 영상 분석
# #     if start_idx <= 5:
# #         print("\n[6/13] Gemini 영상 분석 중 (오디오 없이 시각 분석)...")
# #         gemini = load_gemini_client()
# #         final_clips_data = []

# #         for idx, chunk in enumerate(chunks, 1):
# #             print(f"  청크 {idx}/{len(chunks)} 분석 중...")
# #             # 장면 경계 탐지
# #             scenes = detect_scenes(payload.video_path, media_info.fps, chunk.end_sec - chunk.start_sec)
# #             scene_boundaries = [s.start_sec + chunk.start_sec for s in scenes]
            
# #             transcript_summary = "N/A"
# #             full_summary = "N/A"
# #             storyline = "N/A"

# #             chunk_data = {
# #                 "work_title": payload.work_title,
# #                 "topic": payload.topic,
# #                 "chunk_start_sec": chunk.start_sec,
# #                 "chunk_end_sec": chunk.end_sec,
# #                 "video_path": str(chunk.split_path) if chunk.split_path else None,
# #                 "scene_boundaries": str(scene_boundaries),
# #                 "transcript_summary": transcript_summary,
# #                 "full_summary": full_summary,
# #                 "storyline": storyline
# #             }
# #             analysis_result = gemini.analyze_chunk(chunk_data)
            
# #             # Gemini가 뽑아준 모멘트들을 리스트에 추가
# #             moments = analysis_result.get("candidate_moments", [])
# #             for m in moments:
# #                 final_clips_data.append({
# #                     "role": m.get("story_role", "highlight"),
# #                     "start_sec": m["start_sec"],
# #                     "end_sec": m["end_sec"],
# #                     "subtitle": m.get("subtitle", ""), # Gemini가 영상 보고 생성한 자막
# #                     "tts_line": m.get("tts_line", ""),  # 필요 시 사용
# #                     "use_original_audio": True
# #                 })

# #         # gemini.json 저장
# #         checkpoint_gemini.write_text(json.dumps({
# #             "clips": final_clips_data,
# #             "titles": analysis_result.get("title_candidates", ["추천 제목 없음"])
# #         }, ensure_ascii=False, indent=2), encoding="utf-8")
# #         print(f"  - 분석 완료: {len(final_clips_data)}개 장면 추출됨.")

# #     # [7/13] 스토리 확정 (복잡한 재구성 없이 그대로 사용)
# #     clips: list[StoryClip] = []
# #     title_text: str = ""
# #     checkpoint_story = output_dir / "checkpoint_story.json"
# #     if start_idx <= 6 and checkpoint_story.exists() and from_step != "story":
# #         print("\n[7/13] 스토리 구성 결과 로드 중...")
# #         if not checkpoint_story.exists():
# #             # 8단계부터 시작했는데 파일이 없으면 진짜 에러
# #             raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_story}")
            
# #         print("\n[7/13] 스토리 구성 결과 로드 중...")
# #         story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
# #         clips = [StoryClip(**clip) for clip in story_data["clips"]]
# #         title_text = story_data["title_text"]
# #         print(f"  - {len(clips)}개 클립 로드 완료")

# #     # [8/13] 리소스 생성 (Crop, TTS 등) - 여기서부터 기존 8단계 코드 시작
# #     checkpoint_resources = output_dir / "checkpoint_resources.json"
    
# #     # 1. 이미 리소스 체크포인트가 있는 경우 (로드)
# #     if start_idx <= 7 and checkpoint_resources.exists() and from_step != "resources":
# #         print("\n[8/13] 리소스 로드 중...")
# #         resources_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
# #         crop_map = {k: Path(v) for k, v in resources_data.get("crop_map", {}).items()}
# #         tts_audio_files = {
# #             int(k): Path(v) for k, v in resources_data.get("tts_audio_files", {}).items()
# #         }
# #         print(f"  - [OK] 기존 리소스 로드 완료 (Crop: {len(crop_map)}, TTS: {len(tts_audio_files)})")

# #     # 2. 리소스를 새로 생성해야 하는 경우
# #     elif start_idx <= 7:
# #         print("\n[8/13] 리소스 생성 중...")
# #         resource_start = time.time()
# #         crop_map = {}
# #         tts_audio_files = {}

# #         # 크롭 타임라인 생성
# #         print(f"  크롭 타임라인 생성 중... ({len(clips)}개 클립)")
# #         for idx, clip in enumerate(clips):
# #             crop_path = output_dir / f"crop_{clips.role}_{idx}.json"
# #             # 얼굴 추적 및 크롭 좌표 계산
# #             build_crop_timeline(payload.video_path, crop_path, media_info.width, media_info.height, config.crop_sample_interval_sec)
# #             crop_map[f"{clip.role}_{idx}"] = crop_path
# #             if (idx + 1) % 5 == 0 or (idx + 1) == len(clips):
# #                 print(f"    진행 중... ({idx + 1}/{len(clips)})")
        
# #         # TTS 오디오 생성
# #         print("  TTS 오디오 생성 중...")
# #         for idx, clip in enumerate(clips):
# #             if clip.tts_line:
# #                 tts_path = output_dir / f"tts_{idx}.mp3"
# #                 synthesize_tts(clip.tts_line, tts_path, lang=payload.language)
# #                 tts_audio_files[idx] = tts_path
# #                 if (idx + 1) % 3 == 0 or (idx + 1) == len(clips):
# #                     print(f"    진행 중... ({idx + 1}/{len(clips)})")
        
# #         # 생성된 리소스 경로 저장
# #         resource_elapsed = time.time() - resource_start
# #         checkpoint_resources.write_text(
# #             json.dumps({
# #                 "crop_map": {k: str(v) for k, v in crop_map.items()},
# #                 "tts_audio_files": {str(k): str(v) for k, v in tts_audio_files.items()},
# #             }, ensure_ascii=False, indent=2),
# #             encoding="utf-8"
# #         )
# #         print(f"[OK] 리소스 생성 완료 (소요 시간: {resource_elapsed:.1f}초)")
    
# #     # 3. 그 외 (에러 처리 및 복원)
# #     else:
# #         if checkpoint_resources.exists():
# #             resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
# #             crop_map = {k: Path(v) for k, v in resources_data["crop_map"].items()}
# #             tts_audio_files = {
# #                 int(k): Path(v) for k, v in resources_data.get("tts_audio_files", {}).items()
# #             }
# #         else:
# #             raise FileNotFoundError("리소스를 로드할 수 없습니다. 8단계를 다시 실행하세요.")
# #     # 편집 계획 생성 (리소스 생성 단계에서만)
# #     edit_plan_path = output_dir / "edit_plan.json"
# #     if start_idx <= 7:
# #         print("  편집 계획 생성 중...")
# #         edit_plan = _build_edit_plan(payload, title_text, clips, crop_map, config)
# #         edit_plan_path.write_text(json.dumps(edit_plan, ensure_ascii=False, indent=2), encoding="utf-8")
# #         print(f"  - 편집 계획 저장: {edit_plan_path}")


# #     # 최종 렌더링 단계 (자막 포함, 1회만)
# #     output_video = output_dir / "shorts.mp4"
# #     if start_idx <= 11:
# #         print("\n[12/13] 최종 영상 렌더링 중... (자막 포함, 1회 렌더)")
# #         print(f"  출력 경로: {output_video}")

# #         render_start = time.time()

# #         # TTS 오디오 파일 맵 준비
# #         tts_files_map = tts_audio_files if "tts_audio_files" in locals() else {}
# #         subtitle_path=[]
# #         render_inputs = RenderInputs(
# #             video_path=payload.video_path,
# #             clips=clips,
# #             subtitle_path=subtitle_path,
# #             crop_timeline_map=crop_map,
# #             title_text=title_text,
# #             work_title=payload.work_title,
# #             output_path=output_video,
# #             canvas_width=config.canvas_width,
# #             canvas_height=config.canvas_height,
# #             top_title_height=config.top_title_height,
# #             bottom_label_height=config.bottom_label_height,
# #             tts_audio_files=tts_files_map if tts_files_map else None,
# #             original_audio_gain_db=config.original_gain_db,
# #             tts_audio_gain_db=config.tts_gain_db,
# #             render_preset=config.render_preset,
# #             enable_hwaccel=config.enable_hwaccel,
# #         )
# #         ffmpeg_cmd = render_short(render_inputs)
# #         render_elapsed = time.time() - render_start
# #         cmd_serializable = [str(item) if isinstance(item, Path) else item for item in ffmpeg_cmd]
# #         run_log["steps"].append({"step": "final_render", "command": cmd_serializable})
# #         print(f"[OK] 최종 렌더링 완료 (소요 시간: {render_elapsed:.1f}초)")
# #     else:
# #         if not output_video.exists():
# #             raise FileNotFoundError(f"렌더링된 영상 파일을 찾을 수 없습니다: {output_video}. 최종 렌더링 단계를 먼저 실행하세요.")
# #         print("\n[12/13] 최종 렌더링 단계 스킵 (이미 완료됨)")

# #     # 검증 단계
# #     if start_idx <= 12:
# #         print("\n[13/13] 출력 검증 중...")
# #         if not output_video.exists():
# #             raise FileNotFoundError(f"검증할 영상 파일을 찾을 수 없습니다: {output_video}. 최종 렌더링 단계를 먼저 실행하세요.")
        
# #         validation = validate_output(
# #             output_video,
# #             config.min_duration_sec,
# #             config.max_duration_sec,
# #         )
# #         # Path 객체를 문자열로 변환
# #         validation_dict = validation.__dict__.copy()
# #         for key, value in validation_dict.items():
# #             if isinstance(value, Path):
# #                 validation_dict[key] = str(value)
# #         run_log["steps"].append({"step": "validate", "result": validation_dict})
# #         print(f"  - 길이 검증: {'OK' if validation.duration_ok else 'FAIL'}")
# #         print(f"  - 오디오 피크 검증: {'OK' if validation.audio_peak_ok else 'FAIL'}")
# #         print(f"  - 검은 프레임 검증: {'OK' if validation.black_frames_ok else 'FAIL'}")
# #         print("[OK] 검증 완료")
# #     else:
# #         print("\n[13/13] 검증 단계 스킵 (이미 완료됨)")

# #     # 최종 로그 저장
# #     # run_log의 모든 Path 객체를 문자열로 변환
# #     def _make_json_serializable(obj: Any) -> Any:
# #         """재귀적으로 객체의 모든 Path를 문자열로 변환"""
# #         if isinstance(obj, Path):
# #             return str(obj)
# #         elif isinstance(obj, dict):
# #             return {k: _make_json_serializable(v) for k, v in obj.items()}
# #         elif isinstance(obj, list):
# #             return [_make_json_serializable(item) for item in obj]
# #         elif isinstance(obj, tuple):
# #             return tuple(_make_json_serializable(item) for item in obj)
# #         else:
# #             return obj
    
# #     run_log_serializable = _make_json_serializable(run_log)
# #     run_log_path = output_dir / "run_log.json"
# #     run_log_path.write_text(json.dumps(run_log_serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    
# #     total_elapsed = time.time() - start_time
# #     print("\n" + "=" * 60)
# #     print("파이프라인 완료")
# #     print("=" * 60)
# #     print(f"총 소요 시간: {total_elapsed:.1f}초 ({total_elapsed/60:.1f}분)")
# #     print(f"\n출력 파일:")
# #     print(f"  - 영상: {output_video}")
# #     print(f"  - 편집 계획: {edit_plan_path}")
# #     print(f"  - 실행 로그: {run_log_path}")
# #     print("=" * 60)

# #     return PipelineOutput(
# #         output_video=output_video,
# #         edit_plan_path=edit_plan_path,
# #         run_log_path=run_log_path,
# #     )


# # def _snap_to_scenes(clips: list[StoryClip], scenes, threshold: float) -> list[StoryClip]:
# #     boundaries = sorted({scene.start_sec for scene in scenes} | {scene.end_sec for scene in scenes})
# #     if not boundaries:
# #         return clips
# #     snapped = []
# #     for clip in clips:
# #         start = _snap_time(clip.start_sec, boundaries, threshold)
# #         end = _snap_time(clip.end_sec, boundaries, threshold)
# #         if end - start <= 0.2:
# #             start, end = clip.start_sec, clip.end_sec
# #         snapped.append(
# #             StoryClip(
# #                 role=clip.role,
# #                 start_sec=start,
# #                 end_sec=end,
# #                 subtitle=clip.subtitle,
# #                 tts_line=clip.tts_line,
# #                 use_original_audio=clip.use_original_audio,
# #             )
# #         )
# #     return snapped


# # def _snap_time(value: float, boundaries: list[float], threshold: float) -> float:
# #     closest = min(boundaries, key=lambda b: abs(b - value))
# #     if abs(closest - value) <= threshold:
# #         return closest
# #     return value


# # def _build_edit_plan(
# #     payload: PipelineInput,
# #     title_text: str,
# #     clips: list[StoryClip],
# #     crop_map: dict[str, Path],
# #     config: AppConfig,
# # ) -> dict[str, Any]:
# #     timeline = []
# #     for idx, clip in enumerate(clips):
# #         timeline.append(
# #             {
# #                 "role": clip.role,
# #                 "clip_start_sec": clip.start_sec,
# #                 "clip_end_sec": clip.end_sec,
# #                 "subtitle": clip.subtitle,
# #                 "tts": clip.tts_line,
# #                 "use_original_audio": clip.use_original_audio,
# #                 "reframe": {
# #                     "mode": "face_track",
# #                     "crop_timeline_ref": crop_map[f"{clip.role}_{idx}"].name,
# #                 },
# #             }
# #         )
# #     return {
# #         "input": {
# #             "video_path": str(payload.video_path),
# #             "work_title": payload.work_title,
# #             "topic": payload.topic,
# #             "tone": payload.tone,
# #             "language": payload.language,
# #         },
# #         "layout": {
# #             "canvas": f"{config.canvas_width}x{config.canvas_height}",
# #             "top_title": title_text,
# #             "bottom_label": f"작품명: {payload.work_title}",
# #             "background_style": "blur",
# #         },
# #         "timeline": timeline,
# #         "audio_mix": {
# #             "tts_gain_db": config.tts_gain_db,
# #             "original_gain_db": config.original_gain_db,
# #             "bgm_gain_db": config.bgm_gain_db,
# #         },
# #     }


# #####
# # from __future__ import annotations

# # import json
# # import time
# # import uuid
# # from dataclasses import dataclass
# # from pathlib import Path
# # from typing import Any

# # from app.config import AppConfig, Paths
# # from app.modules.chunker import build_chunks, split_video_chunk
# # from app.modules.gemini_client import load_gemini_client
# # from app.modules.media_probe import probe_media
# # from app.modules.moment_ranker import rank_moments
# # from app.modules.reframe import build_crop_timeline
# # from app.modules.renderer import RenderInputs, render_short
# # from app.modules.scene_detect import detect_scenes
# # from app.modules.story_builder import StoryClip, build_story
# # from app.modules.tts import synthesize_tts
# # from app.modules.validator import validate_output


# # @dataclass(frozen=True)
# # class PipelineInput:
# #     video_path: Path
# #     work_title: str
# #     topic: str
# #     outdir: Path
# #     tone: str = "drama_variety"
# #     language: str = "ko"


# # @dataclass(frozen=True)
# # class PipelineOutput:
# #     output_video: Path
# #     edit_plan_path: Path
# #     run_log_path: Path


# # def run_pipeline(payload: PipelineInput, from_step: str | None = None, job_id: str | None = None) -> PipelineOutput:
# #     print("=" * 60)
# #     print("파이프라인 시작")
# #     print("=" * 60)
    
# #     start_time = time.time()
# #     config = AppConfig()
# #     paths = Paths(app_root=Path(__file__).resolve().parent)
    
# #     # [1/13] 초기화 단계
# #     print("\n[1/13] 초기화 중...")
# #     if job_id:
# #         output_dir = payload.outdir / job_id
# #         if not output_dir.exists():
# #             raise ValueError(f"Job ID {job_id}의 디렉토리를 찾을 수 없습니다: {output_dir}")
# #         run_log_path = output_dir / "run_log.json"
# #         run_log = json.loads(run_log_path.read_text(encoding="utf-8")) if run_log_path.exists() else {"steps": []}
# #     else:
# #         job_id = uuid.uuid4().hex[:8]
# #         output_dir = payload.outdir / job_id
# #         output_dir.mkdir(parents=True, exist_ok=True)
# #         run_log = {"job_id": job_id, "input": vars(payload), "steps": []}

# #     step_order = ["init", "probe", "full_analysis", "storyline", "chunk", "gemini", "story", "resources", "final_render", "validate"]
# #     start_idx = step_order.index(from_step) if from_step in step_order else 0

# #     # [2/13] 미디어 프로브
# #     checkpoint_probe = output_dir / "checkpoint_probe.json"
# #     if start_idx <= 1 and checkpoint_probe.exists() and from_step != "probe":
# #         probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
# #         from app.modules.media_probe import MediaInfo
# #         media_info = MediaInfo(**probe_data)
# #     elif start_idx <= 1:
# #         media_info = probe_media(payload.video_path)
# #         probe_dict = media_info.__dict__.copy()
# #         probe_dict["path"] = str(probe_dict["path"])
# #         checkpoint_probe.write_text(json.dumps(probe_dict, ensure_ascii=False, indent=2), encoding="utf-8")
# #     else:
# #         probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
# #         from app.modules.media_probe import MediaInfo
# #         media_info = MediaInfo(**probe_data)

# #     # [5/13] 청크 분할
# #     print("\n[5/13] 영상 청크 분할 중...")
# #     chunks = build_chunks(payload.video_path, media_info.duration_sec, config.chunk_seconds, config.chunk_overlap)
# #     split_chunks = []
# #     from dataclasses import replace
# #     for i, chunk in enumerate(chunks, 1):
# #         split_path = split_video_chunk(payload.video_path, chunk.start_sec, chunk.end_sec)
# #         split_chunks.append(replace(chunk, split_path=split_path))
# #     chunks = split_chunks

# #     # [6/13] Gemini 영상 분석
# #     checkpoint_gemini = output_dir / "checkpoint_gemini.json"
# #     final_clips_data = []
# #     analysis_result = {}

# #     if start_idx <= 5:
# #         print("\n[6/13] Gemini 영상 분석 중...")
# #         gemini = load_gemini_client()
# #         for idx, chunk in enumerate(chunks, 1):
# #             scenes = detect_scenes(payload.video_path, media_info.fps, chunk.end_sec - chunk.start_sec)
# #             scene_boundaries = [s.start_sec + chunk.start_sec for s in scenes]
# #             chunk_data = {
# #                 "work_title": payload.work_title, "topic": payload.topic,
# #                 "chunk_start_sec": chunk.start_sec, "chunk_end_sec": chunk.end_sec,
# #                 "video_path": str(chunk.split_path), "scene_boundaries": str(scene_boundaries),
# #                 "transcript_summary": "N/A", "full_summary": "N/A", "storyline": "N/A"
# #             }
# #             analysis_result = gemini.analyze_chunk(chunk_data)
# #             moments = analysis_result.get("candidate_moments", [])
# #             for m in moments:
# #                 final_clips_data.append({
# #                     "role": m.get("story_role", "highlight"),
# #                     "start_sec": m["start_sec"], "end_sec": m["end_sec"],
# #                     "subtitle": m.get("subtitle", ""), "tts_line": m.get("tts_line", ""),
# #                     "use_original_audio": True
# #                 })
# #         checkpoint_gemini.write_text(json.dumps({
# #             "clips": final_clips_data,
# #             "titles": analysis_result.get("title_candidates", ["추천 제목 없음"])
# #         }, ensure_ascii=False, indent=2), encoding="utf-8")

# #     # [7/13] 스토리 확정 (중요: 여기서 clips 변수를 확실히 생성)
# #     clips: list[StoryClip] = []
# #     title_text: str = payload.work_title
# #     checkpoint_story = output_dir / "checkpoint_story.json"

# #     if start_idx <= 6:
# #         if checkpoint_story.exists() and from_step != "story":
# #             print("\n[7/13] 스토리 로드 중...")
# #             story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
# #             clips = [StoryClip(**clip) for clip in story_data["clips"]]
# #             title_text = story_data.get("title_text", payload.work_title)
# #         else:
# #             print("\n[7/13] 분석 결과 연결 중...")
# #             # 6단계에서 방금 만든 데이터가 있거나, 파일이 있다면 로드
# #             source_data = final_clips_data if final_clips_data else json.loads(checkpoint_gemini.read_text(encoding="utf-8"))["clips"]
# #             clips = [StoryClip(**c) for c in source_data]
            
# #             # story.json 저장
# #             checkpoint_story.write_text(json.dumps({
# #                 "clips": [c.__dict__ for c in clips],
# #                 "title_text": title_text
# #             }, ensure_ascii=False, indent=2), encoding="utf-8")
# #     else:
# #         # 8단계 이후부터 시작할 때 복구
# #         story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
# #         clips = [StoryClip(**clip) for clip in story_data["clips"]]
# #         title_text = story_data.get("title_text", payload.work_title)

# #     # [8/13] 리소스 생성
# #     checkpoint_resources = output_dir / "checkpoint_resources.json"
# #     crop_map = {}
# #     tts_audio_files = {}

# #     if start_idx <= 7 and checkpoint_resources.exists() and from_step != "resources":
# #         print("\n[8/13] 리소스 로드 중...")
# #         res_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
# #         crop_map = {k: Path(v) for k, v in res_data.get("crop_map", {}).items()}
# #         tts_audio_files = {int(k): Path(v) for k, v in res_data.get("tts_audio_files", {}).items()}
# #     elif start_idx <= 7:
# #         print(f"\n[8/13] 리소스 생성 중... ({len(clips)}개 클립)")
# #         resource_start = time.time()
# #         for idx, clip in enumerate(clips):
# #             crop_path = output_dir / f"crop_{clip.role}_{idx}.json"
# #             build_crop_timeline(payload.video_path, crop_path, media_info.width, media_info.height, config.crop_sample_interval_sec)
# #             crop_map[f"{clip.role}_{idx}"] = crop_path
            
# #             if clip.tts_line:
# #                 tts_path = output_dir / f"tts_{idx}.mp3"
# #                 synthesize_tts(clip.tts_line, tts_path, lang=payload.language)
# #                 tts_audio_files[idx] = tts_path

# #         checkpoint_resources.write_text(json.dumps({
# #             "crop_map": {k: str(v) for k, v in crop_map.items()},
# #             "tts_audio_files": {str(k): str(v) for k, v in tts_audio_files.items()},
# #         }, ensure_ascii=False, indent=2), encoding="utf-8")
# #     else:
# #         res_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
# #         crop_map = {k: Path(v) for k, v in res_data.get("crop_map", {}).items()}
# #         tts_audio_files = {int(k): Path(v) for k, v in res_data.get("tts_audio_files", {}).items()}

# #     # 편집 계획 생성
# #     edit_plan_path = output_dir / "edit_plan.json"
# #     if start_idx <= 7:
# #         edit_plan = _build_edit_plan(payload, title_text, clips, crop_map, config)
# #         edit_plan_path.write_text(json.dumps(edit_plan, ensure_ascii=False, indent=2), encoding="utf-8")

# #     # [12/13] 최종 렌더링
# #     output_video = output_dir / "shorts.mp4"
# #     if start_idx <= 11:
# #         print(f"\n[12/13] 최종 렌더링 시작: {output_video}")
# #         if not clips: raise ValueError("렌더링할 clips가 비어 있습니다.")
        
# #         render_inputs = RenderInputs(
# #             video_path=payload.video_path, clips=clips, subtitle_path=[],
# #             crop_timeline_map=crop_map, title_text=title_text, work_title=payload.work_title,
# #             output_path=output_video, canvas_width=config.canvas_width, canvas_height=config.canvas_height,
# #             top_title_height=config.top_title_height, bottom_label_height=config.bottom_label_height,
# #             tts_audio_files=tts_audio_files, original_audio_gain_db=config.original_gain_db,
# #             tts_audio_gain_db=config.tts_gain_db, render_preset=config.render_preset, enable_hwaccel=config.enable_hwaccel,
# #         )
# #         render_short(render_inputs)

# #     # [13/13] 검증
# #     print("\n[13/13] 검증 중...")
# #     validate_output(output_video, config.min_duration_sec, config.max_duration_sec)
    
# #     print(f"\n파이프라인 완료! 출력: {output_video}")
# #     return PipelineOutput(output_video=output_video, edit_plan_path=edit_plan_path, run_log_path=output_dir / "run_log.json")


# # def _build_edit_plan(payload, title_text, clips, crop_map, config):
# #     timeline = []
# #     for idx, clip in enumerate(clips):
# #         timeline.append({
# #             "role": clip.role, "clip_start_sec": clip.start_sec, "clip_end_sec": clip.end_sec,
# #             "subtitle": clip.subtitle, "tts": clip.tts_line, "use_original_audio": clip.use_original_audio,
# #             "reframe": {"mode": "face_track", "crop_timeline_ref": crop_map[f"{clip.role}_{idx}"].name}
# #         })
# #     return {"layout": {"top_title": title_text}, "timeline": timeline}

# ###
# # 

# from __future__ import annotations

# import json
# import time
# import uuid
# import subprocess
# import concurrent.futures
# from dataclasses import dataclass, replace
# from pathlib import Path
# from typing import Any

# from app.config import AppConfig, Paths
# from app.modules.chunker import build_chunks, split_video_chunk
# from app.modules.gemini_client import load_gemini_client
# from app.modules.media_probe import probe_media, MediaInfo
# from app.modules.moment_ranker import rank_moments
# from app.modules.reframe import build_crop_timeline
# from app.modules.renderer import RenderInputs, render_short
# from app.modules.scene_detect import detect_scenes
# from app.modules.story_builder import StoryClip, build_story
# from app.modules.tts import synthesize_tts
# from app.modules.validator import validate_output
# from app.modules.ffmpeg_utils import find_ffmpeg_command

# @dataclass(frozen=True)
# class PipelineInput:
#     video_path: Path
#     work_title: str
#     topic: str
#     outdir: Path
#     tone: str = "drama_variety"
#     language: str = "ko"

# @dataclass(frozen=True)
# class PipelineOutput:
#     output_video: Path
#     edit_plan_path: Path
#     run_log_path: Path

# def _run_crop(idx, clip_role, video_path, output_dir, width, height, interval):
#     """Pickle 가능한 최상위 레벨 함수로 분리"""
#     from app.modules.reframe import build_crop_timeline
#     crop_path = Path(output_dir) / f"crop_{clip_role}_{idx}.json"
#     build_crop_timeline(Path(video_path), crop_path, width, height, interval)
#     return f"{clip_role}_{idx}", crop_path

# def run_pipeline(payload: PipelineInput, from_step: str | None = None, job_id: str | None = None) -> PipelineOutput:
#     print("=" * 60)
#     print("파이프라인 시작")
#     print("=" * 60)
    
#     start_time_total = time.time()
#     config = AppConfig()
    
#     # 1. 초기화
#     if job_id:
#         output_dir = payload.outdir / job_id
#     else:
#         job_id = f"{payload.work_title}_{uuid.uuid4().hex[:4]}"
#         output_dir = payload.outdir / job_id
#         output_dir.mkdir(parents=True, exist_ok=True)

#     print(f"  - Job ID: {job_id}")
#     print(f"  - 출력 디렉토리: {output_dir}")

#     step_order = ["init", "probe", "full_analysis", "storyline", "chunk", "gemini", "story", "resources", "final_render", "validate"]
#     start_idx = step_order.index(from_step) if from_step in step_order else 0

#     # 2. 미디어 프로브
#     checkpoint_probe = output_dir / "checkpoint_probe.json"
#     if checkpoint_probe.exists() and start_idx > 1:
#         probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
#         media_info = MediaInfo(**probe_data)
#     else:
#         media_info = probe_media(payload.video_path)
#         probe_dict = media_info.__dict__.copy()
#         probe_dict["path"] = str(probe_dict["path"])
#         checkpoint_probe.write_text(json.dumps(probe_dict, ensure_ascii=False, indent=2), encoding="utf-8")

#     # 3. 프록시 생성 (분석용)
#     proxy_video_path = output_dir / "proxy_720p.mp4"
#     if not proxy_video_path.exists():
#         print("\n[3/13] 분석용 프록시 영상 생성 중...")
#         ffmpeg_exe = find_ffmpeg_command("ffmpeg")
#         subprocess.run([
#             ffmpeg_exe, '-y', '-i', str(payload.video_path),
#             '-vf', 'scale=-2:720', '-c:v', 'libx264', '-crf', '32', '-preset', 'ultrafast', '-an',
#             str(proxy_video_path)
#         ], check=True, capture_output=True)

#     # 5. 청크 분할
#     print("\n[5/13] 영상 청크 분할 중...")
#     chunks = build_chunks(proxy_video_path, media_info.duration_sec, config.chunk_seconds, config.chunk_overlap)
#     split_chunks = []
#     for i, chunk in enumerate(chunks):
#         split_path = split_video_chunk(proxy_video_path, chunk.start_sec, chunk.end_sec)
#         split_chunks.append(replace(chunk, split_path=split_path))
    
#     # 6. Gemini 분석
#     checkpoint_gemini = output_dir / "checkpoint_gemini.json"
#     if checkpoint_gemini.exists() and start_idx > 5:
#         gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
#         all_candidates = gemini_data["all_candidates"]
#         title_candidates = gemini_data["title_candidates"]
#     else:
#         print("\n[6/13] Gemini 분석 진행 중...")
#         gemini = load_gemini_client()
#         all_candidates = []
#         title_candidates = []
#         for chunk in split_chunks:
#             scenes = detect_scenes(payload.video_path, media_info.fps, chunk.end_sec - chunk.start_sec)
#             prompt_payload = {
#                 "work_title": payload.work_title,
#                 "topic": payload.topic,
#                 "chunk_start_sec": chunk.start_sec,
#                 "chunk_end_sec": chunk.end_sec,
#                 "scene_boundaries": [s.start_sec + chunk.start_sec for s in scenes],
#                 "video_path": str(chunk.split_path)
#             }
#             res = gemini.analyze_chunk(prompt_payload)
#             for m in res.get("candidate_moments", []):
#                 m["start_sec"] += chunk.start_sec
#                 m["end_sec"] += chunk.start_sec
#                 all_candidates.append(m)
#             title_candidates.extend(res.get("title_candidates", []))
#             if chunk.split_path.exists(): chunk.split_path.unlink() # 용량 확보
#         checkpoint_gemini.write_text(json.dumps({"all_candidates": all_candidates, "title_candidates": title_candidates}, ensure_ascii=False, 
#         indent=2), encoding="utf-8")

#     # 7. 스토리 구성
#     checkpoint_story = output_dir / "checkpoint_story.json"
#     if checkpoint_story.exists() and start_idx > 6:
#         story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
#         clips = [StoryClip(**c) for c in story_data["clips"]]
#         title_text = story_data["title_text"]
#     else:
#         print("\n[7/13] 스토리 구성 중...")
#         gemini = load_gemini_client()
#         story_plan = gemini.compose_story_with_context(all_candidates, payload.work_title, payload.topic)
#         selected_moments = [all_candidates[idx] for idx in story_plan["selected_ids"]]
#         clips = [StoryClip(role=m['story_role'], start_sec=m['start_sec'], end_sec=m['end_sec'], 
#                            subtitle=m.get('subtitle', ""), tts_line=m.get('tts_line', ""), use_original_audio=True) 
#                  for m in selected_moments]
#         title_text = title_candidates[0] if title_candidates else payload.work_title
#         checkpoint_story.write_text(json.dumps({"clips": [c.__dict__ for c in clips], "title_text": title_text}, ensure_ascii=False, 
#         indent=2), encoding="utf-8")

#     # 8. 리소스 생성 (가장 중요한 수정 구간)
#     crop_map = {}
#     tts_audio_files = {}
#     checkpoint_resources = output_dir / "checkpoint_resources.json"

#     if checkpoint_resources.exists() and start_idx > 7:
#         print("\n[8/13] 기존 리소스 로드 중...")
#         res_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
#         crop_map = {k: Path(v) for k, v in res_data["crop_map"].items()}
#         tts_audio_files = {int(k): Path(v) for k, v in res_data["tts_audio_files"].items()}
#     else:
#         print(f"\n[8/13] 리소스 생성 중... ({len(clips)}개 클립)")
#         res_start = time.time()
        
#         # 크롭 타임라인 병렬 생성
#         with concurrent.futures.ProcessPoolExecutor(max_workers=min(len(clips), 4)) as executor:
#             futures = [executor.submit(_run_crop, i, clip.role, payload.video_path, output_dir, 
#                                        media_info.width, media_info.height, config.crop_sample_interval_sec) 
#                        for i, clip in enumerate(clips)]
#             for f in concurrent.futures.as_completed(futures):
#                 k, p = f.result()
#                 crop_map[k] = p
#                 print(f"    - 크롭 완료: {k}")

#         # TTS 생성
#         for i, clip in enumerate(clips):
#             if clip.tts_line:
#                 tts_p = output_dir / f"tts_{i}.mp3"
#                 synthesize_tts(clip.tts_line, tts_p, lang=payload.language)
#                 tts_audio_files[i] = tts_p
#                 print(f"    - TTS 완료: {i}")

#         checkpoint_resources.write_text(json.dumps({
#             "crop_map": {k: str(v) for k, v in crop_map.items()},
#             "tts_audio_files": {str(k): str(v) for k, v in tts_audio_files.items()}
#         }, ensure_ascii=False, 
#         indent=2), encoding="utf-8")
#         print(f"[OK] 리소스 생성 완료 ({time.time()-res_start:.1f}초)")

#     # 12. 최종 렌더링
#     output_video = output_dir / "shorts.mp4"
#     print("\n[12/13] 최종 영상 렌더링 중...")
#     render_inputs = RenderInputs(
#         video_path=payload.video_path, clips=clips, subtitle_path=[],
#         crop_timeline_map=crop_map, title_text=title_text, work_title=payload.work_title,
#         output_path=output_video, canvas_width=config.canvas_width, canvas_height=config.canvas_height,
#         top_title_height=config.top_title_height, bottom_label_height=config.bottom_label_height,
#         tts_audio_files=tts_audio_files, original_audio_gain_db=config.original_gain_db,
#         tts_audio_gain_db=config.tts_gain_db, render_preset=config.render_preset, enable_hwaccel=config.enable_hwaccel
#     )
#     render_short(render_inputs)

#     # 13. 검증
#     print("\n[13/13] 출력 검증 중...")
#     validate_output(output_video, config.min_duration_sec, config.max_duration_sec)
    
#     print(f"\n[완료] 소요시간: {time.time()-start_time_total:.1f}초")
#     print(f"결과물: {output_video}")
    
#     return PipelineOutput(output_video=output_video, edit_plan_path=output_dir/"edit_plan.json", run_log_path=output_dir/"run_log.json")

# def _snap_time(value: float, boundaries: list[float], threshold: float) -> float:
#     closest = min(boundaries, key=lambda b: abs(b - value))
#     return closest if abs(closest - value) <= threshold else value



# 마지막 버전
from __future__ import annotations

import json
import time
import uuid
import subprocess
import concurrent.futures
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.config import AppConfig, Paths
from app.modules.chunker import build_chunks, split_video_chunk
from app.modules.gemini_client import load_gemini_client
from app.modules.media_probe import probe_media, MediaInfo
from app.modules.moment_ranker import rank_moments
from app.modules.reframe import build_crop_timeline
from app.modules.renderer import RenderInputs, render_short
from app.modules.scene_detect import detect_scenes
from app.modules.story_builder import StoryClip, build_story
from app.modules.tts import synthesize_tts
from app.modules.validator import validate_output
from app.modules.ffmpeg_utils import find_ffmpeg_command

@dataclass(frozen=True)
class PipelineInput:
    video_path: Path
    work_title: str
    topic: str
    outdir: Path
    tone: str = "drama_variety"
    language: str = "ko"

@dataclass(frozen=True)
class PipelineOutput:
    output_video: Path
    edit_plan_path: Path
    run_log_path: Path

def _run_crop(idx, clip_role, video_path, output_dir, width, height, interval):
    """Pickle 가능한 최상위 레벨 함수로 분리"""
    from app.modules.reframe import build_crop_timeline
    crop_path = Path(output_dir) / f"crop_{clip_role}_{idx}.json"
    build_crop_timeline(Path(video_path), crop_path, width, height, interval)
    return f"{clip_role}_{idx}", crop_path

def run_pipeline(payload: PipelineInput, from_step: str | None = None, job_id: str | None = None) -> PipelineOutput:
    print("=" * 60)
    print("파이프라인 시작")
    print("=" * 60)
    
    start_time = time.time()
    config = AppConfig()
    
    # [1/13] 초기화 단계
    print("\n[1/13] 초기화 중...")
    if job_id:
        output_dir = payload.outdir / job_id
        if not output_dir.exists():
            raise ValueError(f"Job ID {job_id}의 디렉토리를 찾을 수 없습니다: {output_dir}")
        print(f"  - 기존 작업 재개: {job_id}")
        print(f"  - 출력 디렉토리: {output_dir}")

        #기존 run_log 로드
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
                    "tone": payload.tone,
                    "language": payload.language,
                },
                "steps": [],
            }       
#   
    else:
        # 새 작업 시작
        job_id = f"{payload.work_title}_{uuid.uuid4().hex[:2]}"
        output_dir = payload.outdir / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        run_log = {
            "job_id": job_id,
            "input": {
                "video_path": str(payload.video_path),
                "work_title": payload.work_title,
                "topic": payload.topic,
                "tone": payload.tone,
                "language": payload.language,
            },
            "steps": [],
        }
        print(f"  - Job ID: {job_id}")
        print(f"  - 출력 디렉토리: {output_dir}")
    print("[OK] 초기화 완료")


    # 단계별 실행 플래그
    step_order = ["init", "probe", "full_analysis", "storyline", "chunk", "gemini", "story", "resources", "temp_render", "extract_audio", "regenerate_subtitles", "final_render", "validate"]
    if from_step:
        start_idx = step_order.index(from_step)
        print(f"\n[WARN] {from_step} 단계부터 재시작합니다.")
    else:
        start_idx = 0

     # [2/13]미디어 프로브 단계
    checkpoint_probe = output_dir / "checkpoint_probe.json"
    if start_idx <= 1 and checkpoint_probe.exists() and from_step != "probe":
        print("\n[2/13] 미디어 정보 로드 중...")
        probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
        from app.modules.media_probe import MediaInfo
        media_info = MediaInfo(**probe_data)
        print(f"  - 영상 길이: {media_info.duration_sec:.1f}초")
        print(f"  - 해상도: {media_info.width}x{media_info.height}")
        print(f"  - FPS: {media_info.fps:.2f}")
        print(f"  - 오디오: {'있음' if media_info.has_audio else '없음'}")
        print("[OK] 미디어 정보 로드 완료 (체크포인트에서)")
    elif start_idx <= 1:
        print("\n[2/13] 미디어 정보 수집 중...")
        probe_start = time.time()
        media_info = probe_media(payload.video_path)
        probe_elapsed = time.time() - probe_start
        # Path 객체를 문자열로 변환하여 JSON 직렬화
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
        # 이전 단계에서 로드 (필수)
        if not checkpoint_probe.exists():
            raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_probe}. 이전 단계를 먼저 실행하세요.")
        probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
        from app.modules.media_probe import MediaInfo
        media_info = MediaInfo(**probe_data)

    # 3. 프록시 생성 (분석용)
    proxy_video_path = output_dir / f"{payload.work_title}_720.mp4"
    if not proxy_video_path.exists():
        print("\n[3/13] 분석용 프록시 영상 생성 중...")
        ffmpeg_exe = find_ffmpeg_command("ffmpeg")
        subprocess.run([
            ffmpeg_exe, '-y', '-i', str(payload.video_path),
            '-vf', 'scale=-2:720', '-c:v', 'libx264', '-crf', '32', '-preset', 'ultrafast', '-an',
            str(proxy_video_path)
        ], check=True, capture_output=True)

    # 5. 청크 분할
    print("\n[5/13] 영상 청크 분할 중...")
    chunks = build_chunks(
        proxy_video_path, 
        media_info.duration_sec, 
        config.chunk_seconds, 
        config.chunk_overlap
    )
    print(f"  - 총 {len(chunks)}개 청크 생성")
    
    # 실제 영상 파일 분할
    split_chunks = []
    for i, chunk in enumerate(chunks):
        print(f"    청크 {i} 분할 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
        split_path = split_video_chunk(
            proxy_video_path, 
            chunk.start_sec, 
            chunk.end_sec
        )
        split_chunk = replace(chunk, split_path=split_path)
        split_chunks.append(split_chunk)
        print(f"      → {split_path.name} 생성 완료")
    
    chunks = split_chunks
    print("[OK] 청크 분할 완료")
    
    # Gemini 클라이언트 로드
    print("\n[6/13] Gemini 분석 준비 중...")
    gemini = load_gemini_client()
    print("[OK] Gemini 클라이언트 로드 완료")

    # Gemini 분석 단계
    checkpoint_gemini = output_dir / "checkpoint_gemini.json"

    if start_idx <= 5 and checkpoint_gemini.exists() and from_step != "gemini":
        print("\n[6/13] Gemini 분석 결과 로드 중...")
        gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
        all_candidates = gemini_data["all_candidates"]
        title_candidates = gemini_data["title_candidates"]
        print(f"  - 총 {len(all_candidates)}개 후보 모멘트")
        print(f"  - {len(title_candidates)}개 제목 후보")
        print("[OK] Gemini 분석 결과 로드 완료 (체크포인트에서)")

    elif start_idx <= 7:
        print("\n[6/13] Gemini 분석 진행 중...")
        all_candidates: list[dict[str, Any]] = []
        title_candidates: list[str] = []
        gemini_start = time.time()
        gemini = load_gemini_client()

        # 이전 분석 결과 누적 저장
        previous_analyses: list[dict[str, Any]] = []

        for idx, chunk in enumerate(chunks, 1):
            print(f"  청크 {idx}/{len(chunks)} 분석 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
            chunk_start = time.time()
            
            # [수정] Scene Detection 복구: Gemini에게 정확한 컷 경계 제공
            scenes = detect_scenes(payload.video_path, media_info.fps, chunk.end_sec - chunk.start_sec)
            scene_boundaries = [scene.start_sec + chunk.start_sec for scene in scenes]
            
            # 분할된 파일 경로 가져오기
            split_path = chunk.split_path if chunk.split_path else None

            prompt_payload = {
                "work_title": payload.work_title,
                "topic": payload.topic,
                "chunk_start_sec": chunk.start_sec,
                "chunk_end_sec": chunk.end_sec,
                "transcript_summary": None,
                "scene_boundaries": scene_boundaries, # 탐지된 경계값 전달
                "video_path": str(split_path) if split_path else None,
                "previous_analyses": previous_analyses.copy(),
            }

             # 분석 및 파일 삭제를 try-finally로 보장
            try:
                # [중요] analyze_chunk 내부에서 최대한 많은 candidate_moments를 반환하도록 설계되어 있어야 함
                response = gemini.analyze_chunk(prompt_payload)
                chunk_elapsed = time.time() - chunk_start
                run_log["steps"].append({"step": "gemini", "chunk": chunk.index, "response": response})
                
                moments = response.get("candidate_moments", [])
                moment_count = len(moments)
                print(f"    → {moment_count}개 후보 모멘트 발견 (소요 시간: {chunk_elapsed:.1f}초)")
                
                title_candidates.extend(response.get("title_candidates", []))
                
                previous_analyses.append({
                    "summary": response.get("summary", ""),
                    "candidate_moments": moments,
                })
                
                for moment in moments:
                    # [수정] 후보군 유실 방지: 모든 메타데이터(importance, reason 등)를 유지하며 시간 동기화
                    moment["start_sec"] += chunk.start_sec
                    moment["end_sec"] += chunk.end_sec
                    all_candidates.append(moment)
            finally:
                # 분석 완료 후 분할 파일 즉시 삭제
                if split_path and split_path.exists():
                    try:
                        split_path.unlink()
                        print(f"    → 분할 파일 삭제 완료: {split_path.name}")
                    except Exception as e:
                        print(f"    [WARN] 분할 파일 삭제 실패: {split_path.name} ({e})")
        gemini_elapsed = time.time() - gemini_start
        checkpoint_gemini.write_text(
            json.dumps({"all_candidates": all_candidates, "title_candidates": title_candidates}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] Gemini 분석 완료 (총 {len(all_candidates)}개 후보, 소요 시간: {gemini_elapsed:.1f}초)")
 
    else:
        # 이전 단계에서 로드 (필수)
        if not checkpoint_gemini.exists():
            raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_gemini}. 이전 단계를 먼저 실행하세요.")
        gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
        all_candidates = gemini_data["all_candidates"]
        title_candidates = gemini_data["title_candidates"]
    

    # 7. 스토리 구성
    checkpoint_story = output_dir / "checkpoint_story.json"
    if start_idx <= 6 and checkpoint_story.exists() and from_step != "story":
        print("\n[7/13] 스토리 구성 결과 로드 중...")
        story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
        clips = [StoryClip(**c) for c in story_data["clips"]]
        title_text = story_data["title_text"]
        print(f"  - {len(clips)}개 클립")
        print(f"  - 선택된 제목: {title_text}")
        print("[OK] 스토리 구성 결과 로드 완료 (체크포인트에서)")
    elif start_idx <= 6:
        print("\n[7/13] 스토리 구성 중...")
        gemini = load_gemini_client()
        story_start = time.time()
        story_plan = gemini.compose_story_with_context(all_candidates, payload.work_title, payload.topic)
        selected_moments = [all_candidates[idx] for idx in story_plan["selected_ids"]]
        clips = []
        for m in selected_moments:
            clips.append(StoryClip(
                role=m.get('story_role', 'build'), 
                start_sec=m['start_sec'], 
                end_sec=m['end_sec'], 
                subtitle=m.get('subtitle', ""), 
                tts_line=m.get('tts_line', ""), 
                use_original_audio=True
            ))
        print(f"  - 스토리 클립 생성 완료 ({len(clips)}개 클립)")
        story_elapsed = time.time() - story_start
        title_text = title_candidates[0] if title_candidates else payload.work_title
        checkpoint_story.write_text(json.dumps({"clips": [c.__dict__ for c in clips], "title_text": title_text}, ensure_ascii=False, 
        indent=2), encoding="utf-8")
        print(f"[OK] 스토리 구성 완료 (소요 시간: {story_elapsed:.1f}초)")
        print(f"  - 선택된 제목: {title_text}")
    else:
        # 이전 단계에서 로드
        # 체크포인트가 없으면 edit_plan.json에서 복원 시도
        edit_plan_path = output_dir / "edit_plan.json"
        if checkpoint_story.exists():
            story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
            clips = [StoryClip(**clip) for clip in story_data["clips"]]
            title_text = story_data["title_text"]
        elif edit_plan_path.exists():
            print("\n[7/13] 기존 파일에서 스토리 복원 중...")
            edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8"))
            clips = []
            for clip_data in edit_plan["timeline"]:
                clips.append(StoryClip(
                    role=clip_data["role"],
                    start_sec=clip_data["clip_start_sec"],
                    end_sec=clip_data["clip_end_sec"],
                    # subtitle=clip_data["subtitle"],
                    tts_line=clip_data["tts"],
                    use_original_audio=clip_data["use_original_audio"],
                ))
            title_text = edit_plan["layout"]["top_title"]
            print(f"  - {len(clips)}개 클립")
            print(f"  - 선택된 제목: {title_text}")
            print("[OK] 스토리 복원 완료 (edit_plan.json에서)")
        else:
            raise FileNotFoundError(f"체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다: {checkpoint_story}. 이전 단계를 먼저 실행하세요.")


    # 8. 리소스 생성 (가장 중요한 수정 구간)
    crop_map = {}
    tts_audio_files = {}
    checkpoint_resources = output_dir / "checkpoint_resources.json"
    edit_plan_path = output_dir / "edit_plan.json"

    if checkpoint_resources.exists() and start_idx > 7:
        resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
        crop_map = {k: Path(v) for k, v in resources_data["crop_map"].items()}
        # subtitle_path = Path(resources_data["subtitle_path"])
        subtitle_path=[]
        tts_audio_files = {
            int(k): Path(v) for k, v in resources_data.get("tts_audio_files", {}).items()
        } if "tts_audio_files" in resources_data else {}
        print(f"  - 크롭 타임라인: {len(crop_map)}개")
        print(f"  - TTS 오디오: {len(tts_audio_files)}개")
        print("[OK] 리소스 로드 완료 (체크포인트에서)")
    elif start_idx <= 7:
        print("\n[8/13] 리소스 생성 중...")
        resource_start = time.time()
        print(f"  크롭 타임라인 생성 중... ({len(clips)}개 클립)")
        
        # 크롭 타임라인 병렬 생성
        with concurrent.futures.ProcessPoolExecutor(max_workers=min(len(clips), 4)) as executor:
            futures = [executor.submit(_run_crop, i, clip.role, payload.video_path, output_dir, 
                                       media_info.width, media_info.height, config.crop_sample_interval_sec) 
                       for i, clip in enumerate(clips)]
            for f in concurrent.futures.as_completed(futures):
                k, p = f.result()
                crop_map[k] = p
                print(f"    - 크롭 완료: {k}")

        # TTS 생성
        tts_audio_files = {}
        for idx, clip in enumerate(clips):
            if clip.tts_line:
                tts_path = output_dir / f"tts_{idx}.mp3"
                synthesize_tts(clip.tts_line, tts_path, lang=payload.language)
                tts_audio_files[idx] = tts_path
                if (idx + 1) % 3 == 0 or (idx + 1) == len(clips):
                    print(f"    진행 중... ({idx + 1}/{len(clips)})")
        resource_elapsed = time.time() - resource_start

        checkpoint_resources.write_text(json.dumps({
            "crop_map": {k: str(v) for k, v in crop_map.items()},
            "tts_audio_files": {str(k): str(v) for k, v in tts_audio_files.items()}
        }, ensure_ascii=False, 
        indent=2), encoding="utf-8")
        print(f"[OK] 리소스 생성 완료 (소요 시간: {resource_elapsed:.1f}초)")
        print(f"  - TTS 오디오: {len(tts_audio_files)}개")
    else:
        # 이전 단계에서 로드
        # 체크포인트가 없으면 edit_plan.json에서 복원 시도
        if checkpoint_resources.exists():
            resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
            crop_map = {k: Path(v) for k, v in resources_data["crop_map"].items()}
            tts_audio_files = {
                int(k): Path(v) for k, v in resources_data.get("tts_audio_files", {}).items()
            } if "tts_audio_files" in resources_data else {}
        elif edit_plan_path.exists():
            print("\n[8/13] 기존 파일에서 리소스 복원 중...")
            # edit_plan.json에서 크롭 맵 복원
            edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8"))
            crop_map = {}
            for idx, clip_data in enumerate(edit_plan["timeline"]):
                crop_filename = clip_data["reframe"]["crop_timeline_ref"]
                crop_path = output_dir / crop_filename
                if crop_path.exists():
                    crop_map[f"{clip_data['role']}_{idx}"] = crop_path
            print(f"  - 크롭 타임라인: {len(crop_map)}개 (기존 파일에서 복원)")
            print("[OK] 리소스 복원 완료")
        else:
            raise FileNotFoundError(f"체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다. 이전 단계를 먼저 실행하세요.")

    # 편집 계획 생성 (리소스 생성 단계에서만)
    edit_plan_path = output_dir / "edit_plan.json"
    if start_idx <= 7:
        print("  편집 계획 생성 중...")
        edit_plan = _build_edit_plan(payload, title_text, clips, crop_map, config)
        edit_plan_path.write_text(json.dumps(edit_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  - 편집 계획 저장: {edit_plan_path}")

    # 12. 최종 렌더링
    output_video = output_dir / "shorts.mp4"
    if start_idx <= 11:
        print("\n[12/13] 최종 영상 렌더링 중... (자막 포함, 1회 렌더)")
        print(f"  출력 경로: {output_video}")

        render_start = time.time()

        # TTS 오디오 파일 맵 준비
        tts_files_map = tts_audio_files if "tts_audio_files" in locals() else {}

        render_inputs = RenderInputs(
            video_path=payload.video_path,
            clips=clips,
            subtitle_path=[],
            crop_timeline_map=crop_map,
            title_text=title_text,
            work_title=payload.work_title,
            output_path=output_video,
            canvas_width=config.canvas_width,
            canvas_height=config.canvas_height,
            top_title_height=config.top_title_height,
            bottom_label_height=config.bottom_label_height,
            tts_audio_files=tts_files_map if tts_files_map else None,
            original_audio_gain_db=config.original_gain_db,
            tts_audio_gain_db=config.tts_gain_db,
            render_preset=config.render_preset,
            enable_hwaccel=config.enable_hwaccel,
        )
        ffmpeg_cmd = render_short(render_inputs)
        render_elapsed = time.time() - render_start
        cmd_serializable = [str(item) if isinstance(item, Path) else item for item in ffmpeg_cmd]
        run_log["steps"].append({"step": "final_render", "command": cmd_serializable})
        print(f"[OK] 최종 렌더링 완료 (소요 시간: {render_elapsed:.1f}초)")
    else:
        if not output_video.exists():
            raise FileNotFoundError(f"렌더링된 영상 파일을 찾을 수 없습니다: {output_video}. 최종 렌더링 단계를 먼저 실행하세요.")
        print("\n[12/13] 최종 렌더링 단계 스킵 (이미 완료됨)")


    # 검증 단계
    if start_idx <= 12:
        print("\n[13/13] 출력 검증 중...")
        if not output_video.exists():
            raise FileNotFoundError(f"검증할 영상 파일을 찾을 수 없습니다: {output_video}. 최종 렌더링 단계를 먼저 실행하세요.")
        
        validation = validate_output(
            output_video,
            config.min_duration_sec,
            config.max_duration_sec,
        )
        # Path 객체를 문자열로 변환
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
        print("\n[13/13] 검증 단계 스킵 (이미 완료됨)")
    
    # 최종 로그 저장
    # run_log의 모든 Path 객체를 문자열로 변환
    def _make_json_serializable(obj: Any) -> Any:
        """재귀적으로 객체의 모든 Path를 문자열로 변환"""
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
    print(f"총 소요 시간: {total_elapsed:.1f}초 ({total_elapsed/60:.1f}분)")
    print(f"\n출력 파일:")
    print(f"  - 영상: {output_video}")
    print(f"  - 편집 계획: {edit_plan_path}")
    print(f"  - 실행 로그: {run_log_path}")
    print("=" * 60)

    
    return PipelineOutput(output_video=output_video, edit_plan_path=output_dir/"edit_plan.json", run_log_path=output_dir/"run_log.json")

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
            "tone": payload.tone,
            "language": payload.language,
        },
        "layout": {
            "canvas": f"{config.canvas_width}x{config.canvas_height}",
            "top_title": title_text,
            "bottom_label": f"작품명: {payload.work_title}",
            "background_style": "blur",
        },
        "timeline": timeline,
        "audio_mix": {
            "tts_gain_db": config.tts_gain_db,
            "original_gain_db": config.original_gain_db,
            "bgm_gain_db": config.bgm_gain_db,
        },
    }