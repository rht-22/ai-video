from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import AppConfig, Paths
from app.modules.chunker import build_chunks, split_video_chunk
from app.modules.gemini_client import load_gemini_client
from app.modules.media_probe import probe_media
from app.modules.moment_ranker import rank_moments
from app.modules.reframe import build_crop_timeline
from app.modules.renderer import RenderInputs, render_short
from app.modules.scene_detect import detect_scenes
# from app.modules.speech import extract_audio_from_video, extract_transcript
from app.modules.story_builder import StoryClip, build_story
from app.modules.subtitle import (
    SubtitleStyle,
    SubtitleSegment,
    build_ass,
    build_ass_from_segments,
    merge_subtitle_segments,
    remap_transcript_to_edited_timeline,
)
from app.modules.tts import synthesize_tts
from app.modules.validator import validate_output


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


def run_pipeline(payload: PipelineInput, from_step: str | None = None, job_id: str | None = None) -> PipelineOutput:
    print("=" * 60)
    print("파이프라인 시작")
    print("=" * 60)
    
    start_time = time.time()
    config = AppConfig()
    paths = Paths(app_root=Path(__file__).resolve().parent)
    
    # 초기화 단계
    print("\n[1/13] 초기화 중...")
    if job_id:
        # 기존 작업 재개
        output_dir = payload.outdir / job_id
        if not output_dir.exists():
            raise ValueError(f"Job ID {job_id}의 디렉토리를 찾을 수 없습니다: {output_dir}")
        print(f"  - 기존 작업 재개: {job_id}")
        print(f"  - 출력 디렉토리: {output_dir}")
        # 기존 run_log 로드
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
    else:
        # 새 작업 시작
        job_id = uuid.uuid4().hex[:8]
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

    # 미디어 프로브 단계
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

    # 전체 영상 분석 단계
    checkpoint_full_analysis = output_dir / "checkpoint_full_analysis.json"
    full_summary = None
    key_scenes = None
    emotion_arc = None
    
    if start_idx <= 2 and checkpoint_full_analysis.exists() and from_step != "full_analysis":
        print("\n[3/13] 전체 영상 분석 결과 로드 중...")
        full_analysis_data = json.loads(checkpoint_full_analysis.read_text(encoding="utf-8"))
        full_summary = full_analysis_data.get("summary", "")
        key_scenes = full_analysis_data.get("key_scenes", [])
        emotion_arc = full_analysis_data.get("emotion_arc", "")
        print(f"  - 줄거리 요약 로드 완료")
        print(f"  - 주요 장면: {len(key_scenes)}개")
        print("[OK] 전체 영상 분석 결과 로드 완료 (체크포인트에서)")
    elif start_idx <= 2:
        print("\n[3/13] 전체 영상 분석 중 (오디오 분석 제외)...")
        full_analysis_start = time.time()
        
        # 1. 변수를 미리 빈 값으로 만들어줌 (이게 없어서 에러가 났던 것!)
        transcript_segments = [] 
        transcript_text = "오디오 분석 생략"
        
        # 2. Gemini 전체 분석 호출 (transcript에 빈 텍스트 전달)
        print("  Gemini 시각 분석 중...")
        gemini = load_gemini_client()
        full_analysis = gemini.analyze_full_video(
            video_path=payload.video_path,
            transcript=transcript_text,
            work_title=payload.work_title,
            topic=payload.topic,
            duration_sec=media_info.duration_sec,
        )
        
        full_summary = full_analysis.get("summary", "")
        key_scenes = full_analysis.get("key_scenes", [])
        emotion_arc = full_analysis.get("emotion_arc", "")
        
        # 3. 로그 및 체크포인트에 빈 데이터 저장
        full_analysis["transcript_segments"] = [] 
        checkpoint_full_analysis.write_text(
            json.dumps(full_analysis, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        
        full_analysis_elapsed = time.time() - full_analysis_start
        print(f"  - 줄거리 요약 완료")
        print(f"  - 주요 장면: {len(key_scenes)}개")
        print(f"[OK] 전체 영상 분석 완료 (소요 시간: {full_analysis_elapsed:.1f}초)")
        
        # 전사 결과 저장 (나중에 자막으로 사용)
        transcript_segments_data = [
            {"start_sec": seg.start_sec, "end_sec": seg.end_sec, "text": seg.text}
            for seg in transcript_segments
        ]
        full_analysis["transcript_segments"] = transcript_segments_data
        
        # 체크포인트 다시 저장 (전사 결과 포함)
        checkpoint_full_analysis.write_text(
            json.dumps(full_analysis, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        
        # 임시 오디오 파일 삭제
        try:
            audio_path.unlink()
        except Exception:
            pass
    else:
        # 이전 단계에서 로드 (필수)
        if not checkpoint_full_analysis.exists():
            raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_full_analysis}. 이전 단계를 먼저 실행하세요.")
        full_analysis_data = json.loads(checkpoint_full_analysis.read_text(encoding="utf-8"))
        full_summary = full_analysis_data.get("summary", "")
        key_scenes = full_analysis_data.get("key_scenes", [])
        emotion_arc = full_analysis_data.get("emotion_arc", "")

    # 스토리라인 생성 단계
    checkpoint_storyline = output_dir / "checkpoint_storyline.json"
    storyline_data = None
    selected_storyline = None
    
    if start_idx <= 3 and checkpoint_storyline.exists() and from_step != "storyline":
        print("\n[4/13] 스토리라인 로드 중...")
        storyline_data = json.loads(checkpoint_storyline.read_text(encoding="utf-8"))
        all_storylines = storyline_data.get("storylines", [])
        selected_idx = storyline_data.get("selected_storyline_index", 0)
        selected_storyline = storyline_data.get("selected_storyline") or (all_storylines[selected_idx] if selected_idx < len(all_storylines) else None)
        print(f"  - 생성된 스토리라인 수: {len(all_storylines)}개")
        print(f"  - 선택된 주제: {selected_storyline.get('topic', 'N/A') if selected_storyline else 'N/A'}")
        print(f"  - 흥미도 점수: {selected_storyline.get('interest_score', 0.0):.2f}" if selected_storyline else "")
        print("[OK] 스토리라인 로드 완료 (체크포인트에서)")
    elif start_idx <= 3:
        print("\n[4/13] 스토리라인 생성 중...")
        storyline_start = time.time()
        
        gemini = load_gemini_client()
        storyline_data = gemini.generate_shorts_storyline(
            full_summary=full_summary or "",
            key_scenes=key_scenes or [],
            emotion_arc=emotion_arc or "",
            work_title=payload.work_title,
        )
        
        # 선택된 스토리라인 추출
        all_storylines = storyline_data.get("storylines", [])
        selected_idx = storyline_data.get("selected_storyline_index", 0)
        selected_storyline = storyline_data.get("selected_storyline") or (all_storylines[selected_idx] if selected_idx < len(all_storylines) else None)
        
        # 체크포인트 저장
        checkpoint_storyline.write_text(
            json.dumps(storyline_data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        
        storyline_elapsed = time.time() - storyline_start
        print(f"  - 생성된 스토리라인 수: {len(all_storylines)}개")
        print(f"  - 선택된 주제: {selected_storyline.get('topic', 'N/A') if selected_storyline else 'N/A'}")
        print(f"  - 흥미도 점수: {selected_storyline.get('interest_score', 0.0):.2f}" if selected_storyline else "")
        print(f"[OK] 스토리라인 생성 완료 (소요 시간: {storyline_elapsed:.1f}초)")
    else:
        # 이전 단계에서 로드
        if checkpoint_storyline.exists():
            storyline_data = json.loads(checkpoint_storyline.read_text(encoding="utf-8"))
            all_storylines = storyline_data.get("storylines", [])
            selected_idx = storyline_data.get("selected_storyline_index", 0)
            selected_storyline = storyline_data.get("selected_storyline") or (all_storylines[selected_idx] if selected_idx < len(all_storylines) else None)
        else:
            storyline_data = None
            selected_storyline = None

    # 청크 분할 단계
    print("\n[5/13] 영상 청크 분할 중...")
    chunks = build_chunks(
        payload.video_path,
        media_info.duration_sec,
        config.chunk_seconds,
        config.chunk_overlap,
    )
    print(f"  - 총 {len(chunks)}개 청크 생성")
    
    # 실제 영상 파일 분할
    from dataclasses import replace
    split_chunks = []
    for i, chunk in enumerate(chunks, 1):
        print(f"    청크 {i} 분할 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
        split_path = split_video_chunk(
            payload.video_path,
            chunk.start_sec,
            chunk.end_sec,
        )
        # Chunk 객체에 분할된 파일 경로 추가
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
        
        # 전사 세그먼트를 미리 로드 (연속 컨텍스트 전달용)
        from app.modules.speech import SpeechSegment
        transcript_segments_for_context: list[SpeechSegment] | None = None
        if checkpoint_full_analysis.exists():
            data = json.loads(checkpoint_full_analysis.read_text(encoding="utf-8"))
            segs = data.get("transcript_segments")
            if isinstance(segs, list):
                transcript_segments_for_context = [
                    SpeechSegment(
                        start_sec=float(seg["start_sec"]),
                        end_sec=float(seg["end_sec"]),
                        text=str(seg["text"]),
                    )
                    for seg in segs
                    if isinstance(seg, dict) and "start_sec" in seg and "end_sec" in seg and "text" in seg
                ]
        
        # 이전 분석 결과 누적 저장
        previous_analyses: list[dict[str, Any]] = []
        
        for idx, chunk in enumerate(chunks, 1):
            print(f"  청크 {idx}/{len(chunks)} 분석 중... ({chunk.start_sec:.1f}초 ~ {chunk.end_sec:.1f}초)")
            chunk_start = time.time()
            scenes = detect_scenes(payload.video_path, media_info.fps, chunk.end_sec - chunk.start_sec)
            scene_boundaries = [scene.start_sec + chunk.start_sec for scene in scenes]
            
            # 분할된 파일 경로 가져오기
            split_path = chunk.split_path if chunk.split_path else None
            
            # 전체 줄거리와 선택된 스토리라인 정보 준비
            storyline_summary = ""
            if selected_storyline:
                storyline_obj = selected_storyline.get("storyline", {})
                storyline_summary = (
                    f"선택된 주제: {selected_storyline.get('topic', 'N/A')}\n"
                    f"주제 선택 이유: {selected_storyline.get('topic_reason', 'N/A')}\n"
                    f"Hook: {storyline_obj.get('hook', {}).get('description', 'N/A')}\n"
                    f"Build: {storyline_obj.get('build', {}).get('description', 'N/A')}\n"
                    f"Payoff: {storyline_obj.get('payoff', {}).get('description', 'N/A')}"
                )
            
            # 이전 청크들의 전사 세그먼트 추출 (시간 범위 기반)
            previous_transcripts: list[dict[str, Any]] = []
            if transcript_segments_for_context:
                for seg in transcript_segments_for_context:
                    # 이전 청크들(chunk.start_sec 이전)에 해당하는 전사만
                    if seg.start_sec < chunk.start_sec:
                        previous_transcripts.append({
                            "start_sec": seg.start_sec,
                            "end_sec": seg.end_sec,
                            "text": seg.text,
                        })
            
            prompt_payload = {
                "work_title": payload.work_title,
                "topic": payload.topic,
                "chunk_start_sec": chunk.start_sec,
                "chunk_end_sec": chunk.end_sec,
                "transcript_summary": None,
                "scene_boundaries": scene_boundaries,
                "video_path": str(split_path) if split_path else None,
                "full_summary": full_summary or "없음",
                "storyline": storyline_summary or "없음",
                "previous_analyses": previous_analyses.copy(),  # 이전 분석 결과 전달
                "previous_transcripts": previous_transcripts,  # 이전 전사 전달
            }
            
            # 분석 및 파일 삭제를 try-finally로 보장
            try:
                response = gemini.analyze_chunk(prompt_payload)
                chunk_elapsed = time.time() - chunk_start
                run_log["steps"].append({"step": "gemini", "chunk": chunk.index, "response": response})
                moment_count = len(response.get("candidate_moments", []))
                print(f"    → {moment_count}개 후보 모멘트 발견 (소요 시간: {chunk_elapsed:.1f}초)")
                title_candidates.extend(response.get("title_candidates", []))
                
                # 이전 분석 결과에 현재 결과 추가 (다음 청크를 위해)
                previous_analyses.append({
                    "summary": response.get("summary", ""),
                    "candidate_moments": response.get("candidate_moments", []),
                })
                
                for moment in response["candidate_moments"]:
                    moment["start_sec"] += chunk.start_sec
                    moment["end_sec"] += chunk.start_sec
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

    # 스토리 구성 단계
    checkpoint_story = output_dir / "checkpoint_story.json"
    if start_idx <= 6 and checkpoint_story.exists() and from_step != "story":
        print("\n[7/13] 스토리 구성 결과 로드 중...")
        story_data = json.loads(checkpoint_story.read_text(encoding="utf-8"))
        clips = [StoryClip(**clip) for clip in story_data["clips"]]
        title_text = story_data["title_text"]
        print(f"  - {len(clips)}개 클립")
        print(f"  - 선택된 제목: {title_text}")
        print("[OK] 스토리 구성 결과 로드 완료 (체크포인트에서)")
    elif start_idx <= 6:
        print("\n[7/13] 스토리 구성 중...")
        story_start = time.time()
        ranked = rank_moments(all_candidates)
        print(f"  - 모멘트 랭킹 완료 ({len(ranked)}개)")
        
        # 후보 3~5개 생성: 상위 후보만 사용하여 여러 스토리 시도
        # hook/build/payoff 각각 상위 후보만 사용하도록 필터링
        top_hooks = [m for m in ranked if m.story_role == "hook"][:5]
        top_builds = [m for m in ranked if m.story_role == "build"][:5]
        top_payoffs = [m for m in ranked if m.story_role == "payoff"][:5]
        
        # 최대 3~5개의 스토리 조합 시도 (hook 1개 × build 3~4개 × payoff 1개)
        story_candidates: list[tuple[list[StoryClip], float]] = []
        for hook in top_hooks[:3]:  # hook 상위 3개만 시도
            for payoff in top_payoffs[:2]:  # payoff 상위 2개만 시도
                # build는 점수 순으로 선택
                for build_count in [3, 4, 5]:
                    if len(top_builds) < build_count:
                        continue
                    selected_builds = top_builds[:build_count]
                    candidate_ranked = [hook] + selected_builds + [payoff]
                    
                    try:
                        candidate_clips = build_story(
                            candidate_ranked,
                            config.target_duration_sec,
                            config.target_duration_tolerance_sec,
                            min_duration_sec=config.min_duration_sec,
                            max_duration_sec=config.max_duration_sec,
                        )
                        # 스토리 품질 점수 계산 (평균 점수)
                        avg_score = sum(m.final_score for m in candidate_ranked) / len(candidate_ranked)
                        story_candidates.append((candidate_clips, avg_score))
                    except ValueError:
                        continue
                    if len(story_candidates) >= 5:  # 최대 5개까지만
                        break
                if len(story_candidates) >= 5:
                    break
            if len(story_candidates) >= 5:
                break
        
        # 최고 점수 스토리 선택
        if story_candidates:
            story_candidates.sort(key=lambda x: x[1], reverse=True)
            clips = story_candidates[0][0]
            print(f"  - {len(story_candidates)}개 스토리 후보 생성, 최고 점수 {story_candidates[0][1]:.2f} 선택")
        else:
            # 후보 생성 실패 시 기본 방식 사용
            print("  [WARN] 스토리 후보 생성 실패, 기본 방식 사용")
            clips = build_story(
                ranked,
                config.target_duration_sec,
                config.target_duration_tolerance_sec,
                min_duration_sec=config.min_duration_sec,
                max_duration_sec=config.max_duration_sec,
            )
        
        print(f"  - 스토리 클립 생성 완료 ({len(clips)}개 클립)")
        clips = _snap_to_scenes(clips, detect_scenes(payload.video_path, media_info.fps, media_info.duration_sec), config.scene_snap_threshold_sec)
        story_elapsed = time.time() - story_start
        title_text = title_candidates[0] if title_candidates else payload.topic
        checkpoint_story.write_text(
            json.dumps({"clips": [clip.__dict__ for clip in clips], "title_text": title_text}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
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
                    subtitle=clip_data["subtitle"],
                    tts_line=clip_data["tts"],
                    use_original_audio=clip_data["use_original_audio"],
                ))
            title_text = edit_plan["layout"]["top_title"]
            print(f"  - {len(clips)}개 클립")
            print(f"  - 선택된 제목: {title_text}")
            print("[OK] 스토리 복원 완료 (edit_plan.json에서)")
        else:
            raise FileNotFoundError(f"체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다: {checkpoint_story}. 이전 단계를 먼저 실행하세요.")

    # 리소스 생성 단계
    checkpoint_resources = output_dir / "checkpoint_resources.json"
    edit_plan_path = output_dir / "edit_plan.json"
    
    if start_idx <= 7 and checkpoint_resources.exists() and from_step != "resources":
        print("\n[8/13] 리소스 로드 중...")
        resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
        crop_map = {k: Path(v) for k, v in resources_data["crop_map"].items()}
        subtitle_path = Path(resources_data["subtitle_path"])
        tts_audio_files = {
            int(k): Path(v) for k, v in resources_data.get("tts_audio_files", {}).items()
        } if "tts_audio_files" in resources_data else {}
        print(f"  - 크롭 타임라인: {len(crop_map)}개")
        print(f"  - 자막 파일: {subtitle_path}")
        print(f"  - TTS 오디오: {len(tts_audio_files)}개")
        print("[OK] 리소스 로드 완료 (체크포인트에서)")
    elif start_idx <= 7:
        print("\n[8/13] 리소스 생성 중...")
        resource_start = time.time()
        crop_map = {}
        tts_audio_files = {}
        print(f"  크롭 타임라인 생성 중... ({len(clips)}개 클립)")
        for idx, clip in enumerate(clips):
            crop_path = output_dir / f"crop_{clip.role}_{idx}.json"
            build_crop_timeline(payload.video_path, crop_path, media_info.width, media_info.height, config.crop_sample_interval_sec)
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

    # (기존) 임시 렌더/재전사 단계를 제거하고, 전체 분석 단계의 전사를 재활용합니다.
    # - temp_render/extract_audio는 대용량 인코딩/전사로 이어져 전체 시간이 크게 늘어납니다.

    # 전사 세그먼트 로드 우선순위:
    # 1) checkpoint_full_analysis.json의 transcript_segments (권장)
    # 2) checkpoint_audio_transcript.json (구버전 호환)
    # 3) 최후 수단: 원본 영상에서 오디오 추출/전사
    from app.modules.speech import SpeechSegment

    transcript_segments: list[SpeechSegment] | None = None
    checkpoint_full_analysis = output_dir / "checkpoint_full_analysis.json"
    if checkpoint_full_analysis.exists():
        data = json.loads(checkpoint_full_analysis.read_text(encoding="utf-8"))
        segs = data.get("transcript_segments")
        if isinstance(segs, list):
            transcript_segments = [
                SpeechSegment(
                    start_sec=float(seg["start_sec"]),
                    end_sec=float(seg["end_sec"]),
                    text=str(seg["text"]),
                )
                for seg in segs
                if isinstance(seg, dict) and "start_sec" in seg and "end_sec" in seg and "text" in seg
            ]

    checkpoint_audio_transcript = output_dir / "checkpoint_audio_transcript.json"
    # if transcript_segments is None and checkpoint_audio_transcript.exists():
    #     transcript_data = json.loads(checkpoint_audio_transcript.read_text(encoding="utf-8"))
    #     transcript_segments = [
    #         SpeechSegment(
    #             start_sec=float(seg["start_sec"]),
    #             end_sec=float(seg["end_sec"]),
    #             text=str(seg["text"]),
    #         )
    #         for seg in transcript_data.get("segments", [])
    #         if isinstance(seg, dict) and "start_sec" in seg and "end_sec" in seg and "text" in seg
    #     ]

    transcript_segments=[]
    # if transcript_segments is None:
    #     print("\n[WARN] 전사 체크포인트가 없어 원본 영상에서 오디오 추출/전사를 수행합니다(최후 수단).")
    #     audio_transcript_start = time.time()
    #     audio_path = extract_audio_from_video(payload.video_path)
    #     transcript_segments = extract_transcript(audio_path)
    #     # 체크포인트 저장(다음 실행부터는 재사용)
    #     transcript_data = [
    #         {"start_sec": seg.start_sec, "end_sec": seg.end_sec, "text": seg.text}
    #         for seg in transcript_segments
    #     ]
    #     checkpoint_audio_transcript.write_text(
    #         json.dumps({"segments": transcript_data}, ensure_ascii=False, indent=2),
    #         encoding="utf-8",
    #     )
    #     audio_transcript_elapsed = time.time() - audio_transcript_start
    #     print(f"[OK] 오디오 추출 및 전사 완료 (소요 시간: {audio_transcript_elapsed:.1f}초)")

    # 자막 생성(전사 기반 + 편집 타임라인 재매핑)
    subtitle_path = output_dir / "subtitles.ass"
    print("\n[11/13] 자막 생성 중... (원본 전사 → 편집 타임라인 재매핑)")
    remapped = [
        SubtitleSegment(start_sec=clip.start_sec, end_sec=clip.end_sec, text=clip.subtitle)
        for clip in clips if clip.subtitle
    ]
    
    if not remapped:
        # 자막이 아예 없을 경우 빈 파일 방지용
        remapped = [SubtitleSegment(start_sec=0, end_sec=1, text="")]

    merged = merge_subtitle_segments(
        remapped,
        max_gap_sec=0.25,
        max_total_chars=int(config.subtitle_max_chars_per_line * config.subtitle_max_lines),
    )
    build_ass_from_segments(
        merged,
        subtitle_path,
        SubtitleStyle(margin_v=config.subtitle_margin_bottom),
    )
    print(f"[OK] 자막 생성 완료: {subtitle_path} (events={len(merged)})")

    # 최종 렌더링 단계 (자막 포함, 1회만)
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
            subtitle_path=subtitle_path,
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

    return PipelineOutput(
        output_video=output_video,
        edit_plan_path=edit_plan_path,
        run_log_path=run_log_path,
    )


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