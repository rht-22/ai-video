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


def _apply_silence_cut_to_variants(
    variants: list[tuple[list[StoryClip], str, float]],
    transcript_segments: list,
) -> list[tuple[list[StoryClip], str, float]]:
    """모든 storyline variant의 sl_clips에 무음 컷 적용 후 갱신.

    라운드 6a-2: tts_plan이 무음 컷 *전* clips를 받으면 cue 시간이 영상 길이 초과 가능.
    각 variant의 sl_clips에 cut_silence_from_clips → flatten_to_clips 적용해 동일 처리.
    """
    if not variants:
        return variants
    new_variants: list[tuple[list[StoryClip], str, float]] = []
    for sl_clips, sl_title, sl_score in variants:
        try:
            cut = cut_silence_from_clips(sl_clips, transcript_segments, max_gap_sec=0.4, padding_sec=0.15)
            sl_clips_new = flatten_to_clips(cut)
        except Exception:
            sl_clips_new = sl_clips  # 폴백: 변경 없음
        new_variants.append((sl_clips_new, sl_title, sl_score))
    return new_variants


def _clamp_cues_to_variants(
    tts_cues_per_variant: list[list[dict]],
    variants: list[tuple[list[StoryClip], str, float]],
) -> list[list[dict]]:
    """각 variant의 cue.end_sec가 그 variant의 영상 총 길이를 초과하지 않도록 강제.

    라운드 6a-2 후처리 안전판: LLM 환각 또는 무음 컷 추정 오차로 cue가 영상 끝을 넘기는
    케이스를 마지막에 cap.
    """
    out: list[list[dict]] = []
    for v_idx, cues in enumerate(tts_cues_per_variant or []):
        if v_idx >= len(variants) or not cues:
            out.append(cues or [])
            continue
        sl_clips = variants[v_idx][0]
        total = sum(float(c.end_sec - c.start_sec) for c in sl_clips)
        clamped: list[dict] = []
        for cue in cues:
            new_cue = dict(cue)
            s = float(new_cue.get("start_sec", 0.0))
            e = float(new_cue.get("end_sec", 0.0))
            if e > total:
                new_cue["end_sec"] = total
                if s >= total:
                    # cue가 통째로 영상 밖이면 무효화 (start = end로 만들어 자막에 안 찍힘)
                    new_cue["start_sec"] = max(0.0, total - 0.1)
                    new_cue["end_sec"] = total
                print(f"  [cue-clamp] variant {v_idx + 1} cue: {e:.1f}s → {new_cue['end_sec']:.1f}s (영상 {total:.1f}s 초과 방지)")
            clamped.append(new_cue)
        out.append(clamped)
    return out


def _enforce_title_line2_limit(text: str, max_chars: int = 13) -> str:
    """LLM이 title_line2 글자수 가이드를 어겼을 때 안전판 — 13자 이내로 강제 절단.

    어절 경계 기준으로 자르되, 단어 하나가 13자 초과면 그대로 잘림.
    """
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    # 어절 경계로 자르기
    words = text.split()
    out = ""
    for w in words:
        candidate = (out + " " + w).strip() if out else w
        if len(candidate) > max_chars:
            break
        out = candidate
    if not out:
        out = text[:max_chars]
    return out.strip()


from app.config import AppConfig, Paths, DesignConfig, get_font_path
from app.modules.chunker import build_chunks, split_video_chunk
from app.modules.gemini_client import load_gemini_client
from app.modules.media_probe import probe_media
from app.modules.moment_ranker import assign_sequence_ids
from app.modules.reframe import build_crop_timeline
from app.modules.renderer import RenderInputs, render_short
from app.modules.scene_detect import detect_scenes
from app.modules.speech import extract_audio_segment, extract_transcript, SpeechSegment
from app.modules.story_builder import (
    StoryClip,
    validate_story_clips,
    validate_clip_coherence,
    select_diverse_storylines,
)
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


def _compute_subtitle_margin_v(
    design: DesignConfig,
    *,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
    padding_px: int = 10,
) -> int:
    """ASS 자막의 margin_v를 영상 영역 끝에서 padding_px 위에 위치하도록 동적으로 계산.

    캔버스 canvas_width×canvas_height에 영상이 aspect_ratio로 중앙 배치될 때:
    - 영상 영역 끝점 = overlay_y + scaled_h
    - 자막 baseline = 영상 영역 끝 - padding_px
    - ASS alignment=2(하단 중앙) 기준 margin_v = canvas_height - 자막 baseline = canvas_height - (overlay_y + scaled_h) + padding_px

    aspect_ratio는 DesignConfig에, 캔버스 크기는 AppConfig에 있으므로 호출부에서 명시 전달.
    """
    H = canvas_height
    W = canvas_width
    try:
        r_w, r_h = map(int, str(getattr(design, "aspect_ratio", "1:1")).split(":"))
        scaled_h = int(W * r_h / r_w)
    except Exception:
        scaled_h = W
    scaled_h -= scaled_h % 2
    if scaled_h >= H:
        # 영상이 캔버스 전체 채움 → 하단 끝에서 padding_px 위
        return padding_px
    overlay_y = max(0, (H - scaled_h) // 2)
    return max(padding_px, H - (overlay_y + scaled_h) + padding_px)


def _build_candidates_lookup(all_candidates: list[dict]) -> dict[tuple[int, int], dict]:
    """all_candidates에서 (chunk_index, candidate_index) → candidate dict 맵 생성.

    LLM이 storyline 출력 시 시간을 변형해도 이 lookup으로 정본 candidate 시간을 복원할 수 있게 한다.
    """
    lookup: dict[tuple[int, int], dict] = {}
    for cand in all_candidates or []:
        ci = int(cand.get("chunk_index", -1))
        cj = int(cand.get("candidate_index", -1))
        if ci >= 0 and cj >= 0:
            lookup[(ci, cj)] = cand
    return lookup


def _dedup_boundary_candidates(
    all_candidates: list[dict], *,
    overlap_threshold: float = 0.5,
) -> dict[tuple[int, int], tuple[int, int]]:
    """청크 경계에서 같은 장면이 양쪽 청크에 따로 등록된 candidate 페어를 감지해 alias 맵 반환.

    예: chunk0_cand9(570~600, 청크 끝에서 잘림) ↔ chunk1_cand0(598~615) → 같은 장면
        → alias[(0, 9)] = (1, 0) 또는 그 반대로 정본 선택 후 alias 등록

    정본 선택 기준:
    1. 더 긴 시간 범위 (청크 경계에서 잘리지 않은 쪽 우선)
    2. 같으면 더 일찍 시작하는 쪽

    Returns:
        alias dict: 중복 candidate (slave) → 정본 candidate (master)
    """
    by_chunk: dict[int, list[dict]] = {}
    for c in all_candidates or []:
        ci = int(c.get("chunk_index", -1))
        if ci < 0:
            continue
        by_chunk.setdefault(ci, []).append(c)

    alias: dict[tuple[int, int], tuple[int, int]] = {}
    chunks_sorted = sorted(by_chunk.keys())
    for i in range(len(chunks_sorted) - 1):
        c1, c2 = chunks_sorted[i], chunks_sorted[i + 1]
        for a in by_chunk[c1]:
            for b in by_chunk[c2]:
                a_s, a_e = float(a.get("start_sec", 0)), float(a.get("end_sec", 0))
                b_s, b_e = float(b.get("start_sec", 0)), float(b.get("end_sec", 0))
                if a_e <= a_s or b_e <= b_s:
                    continue
                lap = max(0.0, min(a_e, b_e) - max(a_s, b_s))
                if lap <= 0:
                    continue
                # 같은 장면 판정: (a) 작은 쪽 클립의 overlap_threshold 이상 겹치거나
                # (b) 절대 1.5초 이상 겹침 (청크 경계 잘린 케이스 대응 — 사용자 사례:
                #     chunk0_cand9(570~600) ↔ chunk1_cand0(598~615), overlap 2초)
                a_dur = a_e - a_s
                b_dur = b_e - b_s
                if lap < min(a_dur, b_dur) * overlap_threshold and lap < 1.5:
                    continue
                # 두 candidate가 같은 장면 → 합친 시간 범위가 정본
                # (청크 경계에서 잘린 쪽이 어느 쪽이든 둘을 합치면 잘리지 않은 전체 장면이 됨)
                merged_start = min(a_s, b_s)
                merged_end = max(a_e, b_e)
                # master는 다음 청크의 candidate (보통 청크 시작이 자연스러운 장면 시작점)
                master, slave = b, a
                m_key = (int(master.get("chunk_index")), int(master.get("candidate_index")))
                s_key = (int(slave.get("chunk_index")), int(slave.get("candidate_index")))
                # master candidate의 시간을 합친 범위로 덮어씀 (in-place — 라운타임 사본)
                master["start_sec"] = merged_start
                master["end_sec"] = merged_end
                alias[s_key] = m_key
                print(
                    f"  [dedup] chunk{s_key[0]} cand{s_key[1]}({slave.get('start_sec')}~{slave.get('end_sec')}) "
                    f"+ chunk{m_key[0]} cand{m_key[1]}({b_s}~{b_e}) "
                    f"→ 합친 정본 chunk{m_key[0]} cand{m_key[1]}({merged_start}~{merged_end}) "
                    f"(overlap {lap:.1f}s, 청크 경계 잘림 보정)"
                )
    return alias


def _resolve_clip_times(
    src: dict,
    candidates_lookup: dict[tuple[int, int], dict],
    boundary_alias: dict[tuple[int, int], tuple[int, int]] | None = None,
) -> dict:
    """LLM 출력의 start_sec/end_sec을 무시하고 candidate에서 lookup해 정본 시간으로 복원.

    - boundary_alias: 청크 경계 중복 candidate를 정본으로 redirect
    - context_extended=true면 candidate.context_extension의 extended_start/end_sec을 적용
    - lookup 실패 시 입력 그대로 폴백
    """
    ci = int(src.get("chunk_index", -1))
    cj = int(src.get("candidate_index", -1))
    if boundary_alias:
        # 청크 경계 dedup: alias가 있으면 정본 candidate로 redirect
        master = boundary_alias.get((ci, cj))
        if master is not None:
            ci, cj = master
    cand = candidates_lookup.get((ci, cj))
    if cand is None:
        return src  # 폴백
    extended = bool(src.get("context_extended"))
    ext = cand.get("context_extension") or {}
    if extended and ext.get("needed"):
        start = float(ext.get("extended_start_sec", cand.get("start_sec", src.get("start_sec", 0.0))))
        end = float(ext.get("extended_end_sec", cand.get("end_sec", src.get("end_sec", 0.0))))
    else:
        start = float(cand.get("start_sec", src.get("start_sec", 0.0)))
        end = float(cand.get("end_sec", src.get("end_sec", 0.0)))
    out = dict(src)
    out["start_sec"] = start
    out["end_sec"] = end
    # 정본 candidate의 chunk_index/candidate_index로도 갱신 (alias가 적용된 경우)
    out["chunk_index"] = ci
    out["candidate_index"] = cj
    # description은 candidate가 더 풍부 (LLM이 잘랐을 가능성 → candidate.description 우선)
    if not src.get("description") and cand.get("description"):
        out["description"] = cand["description"]
    if "character_focus" not in out and cand.get("characters_in_scene"):
        out["character_focus"] = cand.get("characters_in_scene")
    return out


def _apply_lookup_to_storyline(
    sl: dict,
    candidates_lookup: dict[tuple[int, int], dict],
    boundary_alias: dict[tuple[int, int], tuple[int, int]] | None = None,
) -> dict:
    """storyline dict 안의 모든 클립 노드(hook/build/payoff/highlight 자체)에 _resolve_clip_times 적용.

    저장 시점에 사용 — checkpoint_story.json에 LLM 환각이 남지 않도록 정본으로 덮어쓴다.
    """
    out = dict(sl)
    if sl.get("shorts_type") == "highlight":
        out.update(_resolve_clip_times(sl, candidates_lookup, boundary_alias))
    else:
        st = dict(sl.get("storyline", {}) or {})
        if isinstance(st.get("hook"), dict):
            st["hook"] = _resolve_clip_times(st["hook"], candidates_lookup, boundary_alias)
        if isinstance(st.get("build"), list):
            st["build"] = [_resolve_clip_times(b, candidates_lookup, boundary_alias) for b in st["build"]]
        if isinstance(st.get("payoff"), dict):
            st["payoff"] = _resolve_clip_times(st["payoff"], candidates_lookup, boundary_alias)
        out["storyline"] = st
    return out


def _clips_from_storyline(
    storyline_data: dict,
    fallback_title: str = "",
    candidates_lookup: dict[tuple[int, int], dict] | None = None,
    boundary_alias: dict[tuple[int, int], tuple[int, int]] | None = None,
) -> tuple[list[StoryClip], str]:
    """스토리라인 dict에서 (clips, title_text)를 추출합니다.

    candidates_lookup: (chunk_index, candidate_index) → candidate dict 맵.
    제공되면 LLM 출력의 start_sec/end_sec을 무시하고 candidate 시간으로 복원 (이슈 2·6 해결).
    boundary_alias: 청크 경계 dedup alias (라운드 4) — 같은 장면 양쪽 청크 등록 케이스 통합.
    """
    clips: list[StoryClip] = []

    # 제목 구성 (이모지 제거)
    title_line1 = _strip_emoji(storyline_data.get("title_line1", ""))
    title_line2 = _enforce_title_line2_limit(_strip_emoji(storyline_data.get("title_line2", "")))
    if title_line1 and title_line2:
        title_text = f"{title_line1}\n{title_line2}"
    else:
        title_text = storyline_data.get("topic", fallback_title)

    # 시간 고정: LLM이 어떻게 출력했든 candidate 정본 시간으로 복원
    def _resolve(src: dict) -> dict:
        if candidates_lookup:
            return _resolve_clip_times(src, candidates_lookup, boundary_alias)
        return src

    if storyline_data.get("shorts_type") == "highlight":
        resolved = _resolve(storyline_data)
        _hl_dur = resolved["end_sec"] - resolved["start_sec"]
        _hl_extended = bool(resolved.get("context_extended", False))
        print(f"  - highlight 클립 길이 {_hl_dur:.1f}s" + (" (context 확장됨)" if _hl_extended else ""))
        clips.append(StoryClip(
            role="payoff",
            start_sec=resolved["start_sec"],
            end_sec=resolved["end_sec"],
            subtitle=resolved.get("topic", "") or storyline_data.get("topic", ""),
            use_original_audio=resolved.get("use_original_audio", True),
            chunk_index=resolved.get("chunk_index", -1),
            candidate_index=resolved.get("candidate_index", -1),
        ))
    else:
        actual_storyline = storyline_data.get("storyline", {})

        def _make_clip(role: str, src: dict) -> StoryClip:
            # 시간 고정: candidate lookup으로 정본 start/end 복원
            src = _resolve(src)
            return StoryClip(
                role=role,
                start_sec=float(src["start_sec"]),
                end_sec=float(src["end_sec"]),
                subtitle=src.get("description", ""),
                use_original_audio=src.get("use_original_audio", True),
                chunk_index=src.get("chunk_index", -1),
                candidate_index=src.get("candidate_index", -1),
                character_focus=tuple(src.get("character_focus") or []),
            )

        hook = actual_storyline.get("hook")
        hook_preview = actual_storyline.get("hook_preview")
        build_list = actual_storyline.get("build", []) or []
        payoff = actual_storyline.get("payoff")

        # hook_preview 유효성 검증: hook 시간 안 + 길이 ≥ 1초
        valid_preview = False
        if isinstance(hook_preview, dict) and isinstance(hook, dict):
            try:
                hp_s = float(hook_preview["start_sec"])
                hp_e = float(hook_preview["end_sec"])
                h_s = float(hook["start_sec"])
                h_e = float(hook["end_sec"])
                valid_preview = (h_s <= hp_s < hp_e <= h_e) and (hp_e - hp_s >= 1.0)
            except (KeyError, TypeError, ValueError):
                valid_preview = False

        if valid_preview:
            # 케이스 3: hook_preview → build → [hook 본체] → payoff
            # hook 본체와 payoff 시간이 겹치면 자막이 중복(이중/삼중) 표시되므로
            # hook 본체를 생략하고 payoff에 흡수한다 (payoff.start_sec을 hook 시작점까지 확장).
            clips.append(_make_clip("hook", hook_preview))
            for b in build_list:
                clips.append(_make_clip("build", b))

            hp_dur = float(hook_preview["end_sec"]) - float(hook_preview["start_sec"])
            h_s_abs = float(hook["start_sec"]) if hook else 0.0
            h_e_abs = float(hook["end_sec"]) if hook else 0.0
            p_s_abs = float(payoff["start_sec"]) if payoff else float("inf")
            p_e_abs = float(payoff["end_sec"]) if payoff else float("inf")
            overlap = (
                hook is not None and payoff is not None
                and p_s_abs < h_e_abs and h_s_abs < p_e_abs
            )

            if overlap and payoff is not None:
                # hook 본체와 payoff 시간 겹침 → hook 본체 생략, payoff에 흡수
                merged_payoff = dict(payoff)
                merged_payoff["start_sec"] = min(p_s_abs, h_s_abs)
                merged_payoff["end_sec"] = max(p_e_abs, h_e_abs)
                clips.append(_make_clip("payoff", merged_payoff))
                merged_dur = merged_payoff["end_sec"] - merged_payoff["start_sec"]
                print(
                    f"  - hook_preview({hp_dur:.1f}s) + build×{len(build_list)} "
                    f"+ payoff(hook 본체 흡수, {merged_dur:.1f}s) — 자막 중복 방지"
                )
            else:
                # 겹침 없음: 기존 시퀀스 (hook 본체 → payoff 별도)
                if hook is not None:
                    clips.append(_make_clip("build", hook))
                if payoff is not None:
                    clips.append(_make_clip("payoff", payoff))
                print(
                    f"  - hook_preview({hp_dur:.1f}s) + build×{len(build_list)} "
                    f"+ hook(본체) + payoff (이중 사용)"
                )
        else:
            # 케이스 1·2 또는 hook_preview 무효: 기존 흐름
            if hook is not None:
                clips.append(_make_clip("hook", hook))
            for b in build_list:
                clips.append(_make_clip("build", b))
            if payoff is not None:
                clips.append(_make_clip("payoff", payoff))
            # 케이스 3 의심 — LLM 가이드 위반 경고
            if hook is not None and payoff is not None and build_list:
                try:
                    b0 = float(build_list[0]["start_sec"])
                    pe = float(payoff["end_sec"])
                    h_s = float(hook["start_sec"])
                    if b0 <= h_s <= pe and not isinstance(hook_preview, dict):
                        print(
                            f"  [WARN] hook이 build/payoff 사이(케이스 3)인데 hook_preview 누락 — build→payoff 점프 발생 가능"
                        )
                except (KeyError, TypeError, ValueError):
                    pass

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
    show_tts_subtitles: bool = True
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
    # [1/15] 초기화
    # ═══════════════════════════════════════
    print("\n[1/15] 초기화 중...")
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
    # [2/15] 작품 자동 리서치
    # ═══════════════════════════════════════
    checkpoint_research = output_dir / "checkpoint_research.json"
    cast_images: list[CharacterInfo] = []

    if not payload.skip_research and not payload.work_context:
        if checkpoint_research.exists():
            print("\n[2/15] 작품 리서치 로드 중... (체크포인트)")
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
            print("\n[2/15] 작품 자동 리서치 중...")
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
        print("\n[2/15] 작품 리서치 건너뜀 (수동 work_context 제공)")
    else:
        print("\n[2/15] 작품 리서치 건너뜀 (--no-research)")

    # 단계별 실행 플래그 (16단계)
    # 라운드 6b: skeleton 단계 완전 제거. 총 15단계로 재구성.
    step_order = [
        "init",            # 0  -> [1/15]
        "research",        # 1  -> [2/15]
        "probe",           # 2  -> [3/15]
        "proxy",           # 3  -> [4/15]
        "exclusion",       # 4  -> [5/15]
        "chunk",           # 5  -> [6/15]
        "character_index", # 6  -> [7/15]
        "gemini",          # 7  -> [8/15]
        "graph",           # 8  -> [9/15]
        "story",           # 9  -> [10/15]
        "transcribe",      # 10 -> [11/15]   (라운드 6a: tts_plan과 swap)
        "tts_plan",        # 11 -> [12/15]
        "resources",       # 12 -> [13/15]
        "render",          # 13 -> [14/15]
        "validate",        # 14 -> [15/15]
    ]
    if from_step:
        start_idx = step_order.index(from_step)
        print(f"\n[WARN] {from_step} 단계부터 재시작합니다.")
    else:
        start_idx = 0

    # ═══════════════════════════════════════
    # [3/15] 미디어 프로브
    # ═══════════════════════════════════════
    checkpoint_probe = output_dir / "checkpoint_probe.json"
    if start_idx <= 2 and checkpoint_probe.exists() and from_step != "probe":
        print("\n[3/15] 미디어 정보 로드 중...")
        probe_data = json.loads(checkpoint_probe.read_text(encoding="utf-8"))
        from app.modules.media_probe import MediaInfo
        media_info = MediaInfo(**probe_data)
        print(f"  - 영상 길이: {media_info.duration_sec:.1f}초")
        print(f"  - 해상도: {media_info.width}x{media_info.height}")
        print(f"  - FPS: {media_info.fps:.2f}")
        print(f"  - 오디오: {'있음' if media_info.has_audio else '없음'}")
        print("[OK] 미디어 정보 로드 완료 (체크포인트에서)")
    elif start_idx <= 2:
        print("\n[3/15] 미디어 정보 수집 중...")
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
    # [4/15] 프록시 영상 생성
    # ═══════════════════════════════════════
    proxy_video_path = output_dir / f"{payload.work_title}_480.mp4"
    if not proxy_video_path.exists():
        print("\n[4/15] 분석용 프록시 영상 생성 중...")
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
        print("\n[4/15] 프록시 영상 이미 존재 — 건너뜀")

    # ═══════════════════════════════════════
    # [5/15] 인트로/크레딧 제외 구간 감지
    # ═══════════════════════════════════════
    from app.modules.intro_credits_detector import (
        detect_exclusion_zones, filter_excluded_moments, print_exclusion_summary, ExclusionZones,
    )

    checkpoint_exclusion = output_dir / "checkpoint_exclusion.json"
    if checkpoint_exclusion.exists():
        _ez_data = json.loads(checkpoint_exclusion.read_text(encoding="utf-8"))
        exclusion_zones = ExclusionZones.from_dict(_ez_data)
        print(f"\n[5/15] 제외 구간 로드 완료")
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
            print(f"\n[5/15] 인트로/크레딧 제외 구간 감지")
            print_exclusion_summary(exclusion_zones, media_info.duration_sec)
            checkpoint_exclusion.write_text(
                json.dumps(exclusion_zones.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # ═══════════════════════════════════════
    # [6/15] 청크 분할
    # ═══════════════════════════════════════
    print("\n[6/15] 영상 청크 분할 중...")
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
    # [7/15] 인물 등장 인덱스 (face_id 사전 패스)
    # ═══════════════════════════════════════
    # 프록시 영상을 일정 간격으로 샘플링하여 등장 인물별 구간을 미리 산출.
    # 결과는 chunk별로 필터링되어 Gemini analyze_chunk 페이로드에 첨부된다.
    character_appearances: list[dict[str, Any]] = []
    checkpoint_char_idx = output_dir / "checkpoint_character_index.json"
    if checkpoint_char_idx.exists() and from_step not in ("gemini",):
        try:
            character_appearances = json.loads(checkpoint_char_idx.read_text(encoding="utf-8"))
            print(f"\n[7/15] 인물 등장 인덱스 로드 ({len(character_appearances)}개 구간)")
        except Exception as e:
            print(f"\n[7/15] 인물 등장 인덱스 로드 실패: {e} — 새로 생성")
            character_appearances = []

    if not character_appearances and cast_images and payload.design.enable_face_recognition:
        try:
            from app.modules.face_id import FaceIdentifier
            print("\n[7/15] 인물 등장 인덱스 생성 중 (face_id 사전 패스)...")
            char_idx_start = time.time()
            _fi_pre = FaceIdentifier()
            _fi_pre.build_references(cast_images)
            if _fi_pre.references:
                character_appearances = _fi_pre.build_appearance_index(
                    proxy_video_path,
                    sample_interval_sec=2.0,
                )
                checkpoint_char_idx.write_text(
                    json.dumps(character_appearances, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  → {len(character_appearances)}개 등장 구간 (소요 시간: {time.time() - char_idx_start:.1f}초)")
            else:
                print("  [WARN] 유효한 face 레퍼런스 없음 — 인물 인덱스 생략")
        except ImportError:
            print("  [WARN] deepface 미설치 — 인물 인덱스 생략")
        except Exception as e:
            print(f"  [WARN] 인물 인덱스 생성 실패: {e} — 인덱스 없이 진행")
            character_appearances = []

    # ═══════════════════════════════════════
    # [8/15] Gemini 분석 (바이럴 최적화)
    # ═══════════════════════════════════════
    checkpoint_gemini = output_dir / "checkpoint_gemini.json"
    if start_idx <= 8 and checkpoint_gemini.exists() and from_step != "gemini":
        print("\n[8/15] Gemini 분析 결과 로드 중...")
        gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
        all_candidates = gemini_data["all_candidates"]
        chunk_meta_list = gemini_data.get("chunk_meta", [])
        print(f"  - 총 {len(all_candidates)}개 후보 모멘트, chunk_meta {len(chunk_meta_list)}건")
        print("[OK] Gemini 분석 결과 로드 완료 (체크포인트에서)")
    elif start_idx <= 8:
        print("\n[8/15] Gemini 분석 준비 중...")
        gemini = load_gemini_client()
        print("[OK] Gemini 클라이언트 로드 완료")

        print("\n[8/15] Gemini 분석 진행 중...")
        all_candidates: list[dict[str, Any]] = []
        chunk_meta_list: list[dict[str, Any]] = []
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


            # face_id 사전 인식 결과를 chunk 범위로 필터링하고 0초 기준 상대 시간으로 변환
            chunk_offset = (
                chunk.actual_start_sec
                if getattr(chunk, "actual_start_sec", None) is not None
                else chunk.start_sec
            )
            chunk_appearances: list[dict[str, Any]] = []
            for _ap in character_appearances:
                if _ap["end_sec"] <= chunk.start_sec or _ap["start_sec"] >= chunk.end_sec:
                    continue
                _s = max(_ap["start_sec"], chunk.start_sec) - chunk_offset
                _e = min(_ap["end_sec"], chunk.end_sec) - chunk_offset
                if _e <= _s:
                    continue
                chunk_appearances.append({
                    "character": _ap["character"],
                    "start_sec": float(_s),
                    "end_sec": float(_e),
                })

            prompt_payload = {
                "work_title": payload.work_title,
                "topic": payload.topic,
                "previous_episodes_context": payload.previous_episodes_context,
                "work_context": payload.work_context,
                "chunk_index": chunk.index,
                "chunk_start_sec": chunk.start_sec,
                "chunk_end_sec": chunk.end_sec,
                "scene_boundaries": scene_boundaries,
                "video_path": str(split_path) if split_path else None,
                "previous_analyses": previous_analyses.copy(),
                "transcript_segments": chunk_transcript_segs,
                "character_appearances": chunk_appearances,
            }

            try:
                response = gemini.analyze_chunk(prompt_payload)
                chunk_elapsed = time.time() - chunk_start
                run_log["steps"].append({"step": "gemini", "chunk": chunk.index, "response": response})
                moment_count = len(response.get("candidate_moments", []))
                print(f"    → {moment_count}개 후보 모멘트 발견 (소요 시간: {chunk_elapsed:.1f}초)")


                previous_analyses.append({
                    "chunk_index": chunk.index,
                    "summary": response.get("summary", ""),
                    "candidate_moments": response.get("candidate_moments", []),
                    "segments": response.get("segments", []),
                })

                # chunk-level 메타 (segments 포함) 누적 — checkpoint_gemini.json에 보존되고
                # story 단계의 segments 요약 컨텍스트로도 활용된다.
                # segments의 start_sec/end_sec은 chunk-relative이므로 chunk.start_sec를 더해 절대 시간으로 변환.
                _chunk_segments_abs: list[dict[str, Any]] = []
                for s in (response.get("segments") or []):
                    _ss = float(s.get("start_sec", 0)) + chunk.start_sec
                    _ee = float(s.get("end_sec", 0)) + chunk.start_sec
                    _chunk_segments_abs.append({
                        "segment_index": s.get("segment_index"),
                        "start_sec": _ss,
                        "end_sec": _ee,
                        "description": s.get("description", ""),
                    })
                chunk_meta_list.append({
                    "chunk_index": chunk.index,
                    "summary": response.get("summary", ""),
                    "segments": _chunk_segments_abs,
                    "characters_tracking": response.get("characters_tracking", []),
                    "title_candidates": response.get("title_candidates", []),
                })

                # split_video_chunk가 PTS를 0초로 정규화하므로 (output seek + -avoid_negative_ts make_zero),
                # Gemini는 항상 0초 기준 상대 시간으로 응답한다고 신뢰할 수 있다.
                # 따라서 chunk.start_sec을 더해 원본 영상 절대 시간으로 변환한다.
                # (이전엔 actual_start_sec 양수 분기로 0.05초 같은 PTS 잔여값을 잘못 사용해
                #  chunk.start_sec이 무시되는 버그가 있었음)
                actual_cut_offset = chunk.start_sec
                for moment in response["candidate_moments"]:
                    moment["start_sec"] += actual_cut_offset
                    moment["end_sec"] += actual_cut_offset
                    moment["chunk_index"] = chunk.index

                    # context_extension 시간도 절대값으로 변환 (highlight 자동 확장용)
                    ce = moment.get("context_extension")
                    if isinstance(ce, dict):
                        if "extended_start_sec" in ce and ce["extended_start_sec"] is not None:
                            try:
                                ce["extended_start_sec"] = float(ce["extended_start_sec"]) + actual_cut_offset
                            except (TypeError, ValueError):
                                ce["needed"] = False
                        if "extended_end_sec" in ce and ce["extended_end_sec"] is not None:
                            try:
                                ce["extended_end_sec"] = float(ce["extended_end_sec"]) + actual_cut_offset
                            except (TypeError, ValueError):
                                ce["needed"] = False
                        # 안전 검증: extended가 start/end를 감싸지 않으면 needed=false 강등
                        if ce.get("needed"):
                            es = ce.get("extended_start_sec")
                            ee = ce.get("extended_end_sec")
                            if es is None or ee is None:
                                ce["needed"] = False
                            elif not (es <= moment["start_sec"] and moment["end_sec"] <= ee):
                                ce["needed"] = False
                            else:
                                # 라운드 6a (B): 청크 범위 벗어남 검증 — LLM 환각 차단
                                # extended가 청크 절대 시간 범위 [chunk.start_sec, chunk.end_sec]를
                                # ±0.5초 완충 두고 벗어나면 needed=false 강등.
                                _chunk_lo = chunk.start_sec - 0.5
                                _chunk_hi = chunk.end_sec + 0.5
                                if es < _chunk_lo or ee > _chunk_hi:
                                    ce["needed"] = False
                                    print(
                                        f"    [WARN] context_extension 청크 범위 벗어남 "
                                        f"(es={es:.1f}, ee={ee:.1f} vs chunk[{chunk.start_sec:.1f}~{chunk.end_sec:.1f}]) "
                                        f"→ needed=false 강등"
                                    )

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
                "chunk_meta": chunk_meta_list,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] Gemini 분석 완료 (총 {len(all_candidates)}개 후보, 소요 시간: {gemini_elapsed:.1f}초)")
    else:
        if not checkpoint_gemini.exists():
            raise FileNotFoundError(f"체크포인트 파일을 찾을 수 없습니다: {checkpoint_gemini}")
        gemini_data = json.loads(checkpoint_gemini.read_text(encoding="utf-8"))
        all_candidates = gemini_data["all_candidates"]
        chunk_meta_list = gemini_data.get("chunk_meta", [])

    # ── 인트로/크레딧 포스트필터 (안전망) ──
    if exclusion_zones.detection_method != "none":
        before_count = len(all_candidates)
        all_candidates = filter_excluded_moments(all_candidates, exclusion_zones)
        if before_count != len(all_candidates):
            print(f"  인트로/크레딧 필터 적용: {before_count} → {len(all_candidates)}개 후보")

    # ═══════════════════════════════════════
    # [9/15] 관계 그래프 추출
    # ═══════════════════════════════════════
    checkpoint_graph = output_dir / "checkpoint_graph.json"
    relationship_edges: list[dict[str, Any]] = []

    if start_idx <= 9 and checkpoint_graph.exists() and from_step not in ("gemini", "graph"):
        print("\n[9/15] 관계 그래프 로드 중...")
        graph_data = json.loads(checkpoint_graph.read_text(encoding="utf-8"))
        relationship_edges = graph_data.get("edges", [])
        print(f"  - {len(relationship_edges)}개 관계 엣지 로드")
        print("[OK] 관계 그래프 로드 완료 (체크포인트에서)")
    elif start_idx <= 9:
        print("\n[9/15] 관계 그래프 추출 중...")
        gemini = load_gemini_client()
        relationship_edges = gemini.extract_relationships(all_candidates)
        checkpoint_graph.write_text(
            json.dumps({"edges": relationship_edges}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] 관계 그래프 추출 완료 ({len(relationship_edges)}개 엣지)")

    # ═══════════════════════════════════════
    # [10/15] 스토리 구성 (바이럴 최적화 — 멀티쇼츠)
    # ═══════════════════════════════════════
    # all_storyline_variants: list of (clips, title_text, score)
    all_storyline_variants: list[tuple[list[StoryClip], str, float]] = []
    max_shorts = min(payload.max_shorts, config.max_shorts_count)
    story_plan = None

    checkpoint_story = output_dir / "checkpoint_story.json"
    if start_idx <= 10 and checkpoint_story.exists() and from_step != "story":
        print("\n[10/15] 스토리 구성 결과 로드 중...")
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

    elif start_idx <= 10:
        print("\n[10/15] 스토리 구성 중...")
        gemini = load_gemini_client()
        story_start = time.time()

        # sequence_id 부여: continues_from + 관계 그래프 continuous 엣지 기반
        all_candidates = assign_sequence_ids(all_candidates, edges=relationship_edges or None)

        # Gemini 바이럴 스토리 구성 (Flash + skeleton). 점수 산정은 Gemini가 description/highlight_eligible 등으로 직접 판단
        story_plan = gemini.compose_story_with_context(
            all_candidates,
            payload.work_title,
            payload.topic,
            min_duration_sec=config.min_duration_sec,
            max_duration_sec=config.max_duration_sec,
            work_context=payload.work_context,
            previous_episodes_context=payload.previous_episodes_context,
            relationship_edges=relationship_edges or None,
            chunk_meta=chunk_meta_list or None,
        )

        # 멀티쇼츠: ranked_storylines에서 최대 max_shorts개 추출
        ranked_storylines = story_plan.get("ranked_storylines", [])
        if not ranked_storylines:
            # 하위 호환: ranked_storylines가 없으면 selected_storyline 사용
            sel = story_plan.get("selected_storyline", {})
            if sel:
                ranked_storylines = [sel]

        # 시간 고정 lookup 빌드: LLM이 출력한 start/end_sec를 candidate 정본으로 복원
        candidates_lookup = _build_candidates_lookup(all_candidates)
        print(f"  - 시간 고정 lookup: {len(candidates_lookup)}개 candidate 인덱싱")
        # 청크 경계 dedup alias: 같은 장면이 양쪽 청크에 등록된 경우 합친 정본으로 redirect (라운드 4)
        boundary_alias = _dedup_boundary_candidates(all_candidates)
        if boundary_alias:
            print(f"  - 청크 경계 dedup: {len(boundary_alias)}개 중복 alias 적용")

        # 다양성 우선 재선정: 같은 chunk/phase가 max_shorts개 모두 차지하지 않게
        # 점수 1위는 무조건 유지하고, 이후는 chunk_index/emotional_phase가 다른 후보를 우선
        diverse_pool = select_diverse_storylines(
            ranked_storylines,
            max_count=max(max_shorts * 2, len(ranked_storylines)),  # 폴백 여유분 확보
            skeleton=None,  # skeleton 단계 제거 (라운드 6b) — chunk_index 다양성으로만 폴백
        )

        for sl_idx, sl_data in enumerate(diverse_pool):
            if len(all_storyline_variants) >= max_shorts:
                break
            score = sl_data.get("score", 0.0)
            if score < config.viral_score_min_threshold and len(all_storyline_variants) > 0:
                print(f"  - 스토리라인 {sl_idx + 1} 스킵 (점수 {score:.2f} < 임계값 {config.viral_score_min_threshold})")
                continue

            try:
                sl_clips, sl_title = _clips_from_storyline(
                    sl_data, payload.work_title,
                    candidates_lookup=candidates_lookup,
                    boundary_alias=boundary_alias,
                )
            except (KeyError, TypeError) as e:
                print(f"  - 스토리라인 {sl_idx + 1} 파싱 실패: {e}")
                continue

            # 최상위 제목 (첫 번째 스토리라인만 story_plan의 title 사용 가능)
            if len(all_storyline_variants) == 0:
                top_line1 = _strip_emoji(story_plan.get("title_line1", ""))
                top_line2 = _enforce_title_line2_limit(_strip_emoji(story_plan.get("title_line2", "")))
                if top_line1 and top_line2:
                    sl_title = f"{top_line1}\n{top_line2}"

            # 클립 수 검증: highlight형은 1개도 OK, storytelling형은 최소 3개
            is_highlight = (sl_data.get("shorts_type") == "highlight")
            min_clip_count = 1 if is_highlight else 3
            is_valid, msg = validate_story_clips(
                sl_clips, config.min_duration_sec, config.max_duration_sec,
                min_clip_count=min_clip_count,
            )
            if not is_valid:
                print(f"  [SKIP] 스토리라인 {sl_idx + 1} 검증 실패: {msg}")
                continue  # storytelling 1~2클립이면 스킵 → 다음 후보로
            coh_warnings = validate_clip_coherence(sl_clips)
            for w in coh_warnings:
                print(f"  [COHERENCE] 스토리라인 {sl_idx + 1}: {w}")

            all_storyline_variants.append((sl_clips, sl_title, score))
            print(f"  - 스토리라인 {sl_idx + 1}: {len(sl_clips)}개 클립, 점수 {score:.2f}, 제목: {sl_title}")

        # 폴백: 유효한 스토리가 없으면 selected_storyline에서 1개 생성
        if not all_storyline_variants:
            sel = story_plan.get("selected_storyline", {})
            if sel:
                fb_clips, fb_title = _clips_from_storyline(
                    sel, payload.work_title,
                    candidates_lookup=candidates_lookup,
                    boundary_alias=boundary_alias,
                )
                all_storyline_variants.append((fb_clips, fb_title, sel.get("score", 0.5)))
                print(f"  - 폴백 스토리라인: {len(fb_clips)}개 클립")

        # 라운드 4: story_plan에 정본 시간 덮어쓰기 → checkpoint_story.json에 LLM 환각이 안 남음
        # 모든 storylines (rank/selected/diverse 변형) 안의 클립 노드에 candidates_lookup 적용
        if "storylines" in story_plan and isinstance(story_plan["storylines"], list):
            story_plan["storylines"] = [
                _apply_lookup_to_storyline(sl, candidates_lookup, boundary_alias)
                for sl in story_plan["storylines"]
            ]
        if isinstance(story_plan.get("ranked_storylines"), list):
            story_plan["ranked_storylines"] = [
                _apply_lookup_to_storyline(sl, candidates_lookup, boundary_alias)
                for sl in story_plan["ranked_storylines"]
            ]
        if isinstance(story_plan.get("selected_storyline"), dict):
            story_plan["selected_storyline"] = _apply_lookup_to_storyline(
                story_plan["selected_storyline"], candidates_lookup, boundary_alias
            )

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
            print("\n[10/15] 기존 파일에서 스토리 복원 중...")
            edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8"))
            clips = []
            for clip_data in edit_plan["timeline"]:
                clips.append(StoryClip(
                    role=clip_data["role"],
                    start_sec=clip_data["clip_start_sec"],
                    end_sec=clip_data["clip_end_sec"],
                    subtitle=clip_data["subtitle"],
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
    # [11/15] 선택된 클립 전사 + 무음 제거 (라운드 6a — TTS cue 계획 *전*에 수행)
    # ═══════════════════════════════════════
    # 무음 컷으로 clips가 짧아지므로 그 *후*의 새 clips 기준으로 TTS cue 시간을 계산해야
    # 자막·TTS 밀림이 발생하지 않는다. 라운드 6a 핵심: tts_plan을 transcribe 뒤로 이동.
    transcript_text: list = []
    full_audio_path = output_dir / "full_audio.json"
    segments_cache_path = output_dir / "subtitle_segments.json"

    # full_audio는 선택된 클립 범위에만 전사된 데이터. story/graph 단계부터 재실행 시
    # 클립 범위가 달라지면 매핑 미스(0 segments)가 발생하므로 full_audio도 재생성한다.
    _full_audio_invalidate = from_step in ("graph", "story", "transcribe", "tts_plan", "resources")

    if start_idx <= 11 and full_audio_path.exists() and not _full_audio_invalidate:
        print("\n[11/15] 전사 데이터 로드 중...")
        loaded_data = json.loads(full_audio_path.read_text(encoding="utf-8"))
        transcript_text = [SimpleNamespace(**seg) for seg in loaded_data]
        print(f"  - {len(transcript_text)}개 세그먼트 로드 완료")

        # 무음 제거 (이미 전사 데이터가 있으므로 바로 실행)
        print("  무음 구간 제거 중...")
        cut_results = cut_silence_from_clips(clips, transcript_text, max_gap_sec=0.4, padding_sec=0.15)
        print_silence_cut_summary(cut_results)
        clips = flatten_to_clips(cut_results)
        # 라운드 6a-2: 모든 storyline variant의 sl_clips도 무음 컷 적용 후 갱신.
        # tts_plan이 변형되지 않은 sl_clips를 받으면 cue 시간이 영상 길이 초과 가능.
        all_storyline_variants = _apply_silence_cut_to_variants(
            all_storyline_variants, transcript_text
        )
        print(f"[OK] 전사 로드 + 무음 제거 완료 ({len(clips)}개 클립)")

    elif start_idx <= 11:
        print("\n[11/15] 선택된 클립 구간 전사 중...")
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
            cut_results = cut_silence_from_clips(clips, transcript_text, max_gap_sec=0.4, padding_sec=0.15)
            print_silence_cut_summary(cut_results)
            clips = flatten_to_clips(cut_results)
            # 라운드 6a-2: 모든 storyline variant의 sl_clips도 갱신 (cue 영상 길이 초과 방지)
            all_storyline_variants = _apply_silence_cut_to_variants(
                all_storyline_variants, transcript_text
            )
        print(f"[OK] 전사 완료 ({len(clips)}개 클립)")
    else:
        # 이전 단계 데이터 로드
        if full_audio_path.exists():
            loaded_data = json.loads(full_audio_path.read_text(encoding="utf-8"))
            transcript_text = [SimpleNamespace(**seg) for seg in loaded_data]
        print(f"\n[11/15] 전사 단계 건너뜀 ({len(transcript_text)}개 세그먼트 로드)")

    # 자막 데이터 생성 (전사 → 편집 타임라인 매핑)
    # transcribe 이전(graph/story) 단계부터 재실행이면 클립 구성/길이가 달라졌으므로
    # 자막 캐시도 무효화해서 새 클립 기준으로 remap + merge(환각 필터 포함) 재수행.
    final_segments = []
    _transcribe_invalidate = from_step in ("graph", "story", "transcribe", "tts_plan", "resources")
    if start_idx <= 11 and (not segments_cache_path.exists() or _transcribe_invalidate):
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
    # [12/15] TTS 큐 계획 (voice/speed/위치 결정) — 라운드 6a: transcribe 뒤로 이동
    # ═══════════════════════════════════════
    # 결정된 storyline의 *무음 컷 후* 클립 시퀀스를 받아 편집 타임라인 절대 시간 기준으로
    # cue 리스트를 만든다. cue 시간/voice/speed는 다음 [13/15]에서 mp3 합성에 사용.
    # 무음 컷이 clips 길이를 줄여놓은 *후* cue를 계산하므로 TTS·자막 밀림이 발생하지 않는다.
    checkpoint_tts = output_dir / "checkpoint_tts_plan.json"
    tts_cues_per_variant: list[list[dict[str, Any]]] = []
    if checkpoint_tts.exists() and from_step not in ("graph", "story", "transcribe", "tts_plan"):
        print("\n[12/15] TTS cue 계획 로드 중...")
        try:
            cached = json.loads(checkpoint_tts.read_text(encoding="utf-8"))
            tts_cues_per_variant = cached.get("variants", [])
            print(f"  - {len(tts_cues_per_variant)}개 variant cue 로드")
        except Exception as e:
            print(f"  [WARN] cue 캐시 로드 실패: {e} — 새로 계획")
            tts_cues_per_variant = []

    if not tts_cues_per_variant and start_idx <= 12:
        print("\n[12/15] TTS cue 계획 중 (Flash, 무음 컷 후 clips 기준)...")
        tts_start = time.time()
        gemini = load_gemini_client()
        for sl_idx, (sl_clips, _t, _s) in enumerate(all_storyline_variants):
            try:
                cues = gemini.plan_tts_cues(
                    sl_clips,
                    payload.work_title,
                    work_context=payload.work_context,
                    previous_episodes_context=payload.previous_episodes_context,
                )
                tts_cues_per_variant.append(cues)
                voices = sorted({c["voice"] for c in cues})
                speeds = sorted({c["speed"] for c in cues})
                print(f"  - variant {sl_idx + 1}: {len(cues)}개 cue (voice={voices}, speed={speeds})")
            except Exception as e:
                print(f"  [WARN] variant {sl_idx + 1} cue 계획 실패: {e} — TTS 없이 진행")
                tts_cues_per_variant.append([])
        checkpoint_tts.write_text(
            json.dumps({"variants": tts_cues_per_variant}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] TTS cue 계획 완료 (소요 시간: {time.time() - tts_start:.1f}초)")

    # 라운드 6a-2 후처리 안전판: 각 variant의 cue.end_sec가 그 variant의 영상 길이를 넘지 않도록 cap.
    tts_cues_per_variant = _clamp_cues_to_variants(tts_cues_per_variant, all_storyline_variants)

    # 첫 번째 variant의 cue를 기본으로 사용 (다중 쇼츠는 [14/15]에서 variant마다 따로)
    tts_cues = tts_cues_per_variant[0] if tts_cues_per_variant else []

    # ═══════════════════════════════════════
    # [13/15] 리소스 생성 (크롭, TTS, 편집 계획)
    # ═══════════════════════════════════════
    checkpoint_resources = output_dir / "checkpoint_resources.json"
    edit_plan_path = output_dir / "edit_plan.json"

    # story/graph 단계부터 재실행 시 클립 구성이 달라질 수 있으므로 resources 캐시 무효화.
    # 이전 라운드의 crop_map 키(role_idx)와 새 라운드의 clip 키가 어긋나면 KeyError 발생.
    _resources_invalidate = from_step in ("graph", "story", "tts_plan", "transcribe", "resources")
    if start_idx <= 13 and checkpoint_resources.exists() and not _resources_invalidate:
        print("\n[13/15] 리소스 로드 중...")
        resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
        crop_map = {k: Path(v) for k, v in resources_data["crop_map"].items()}
        tts_cue_files = resources_data.get("tts_cue_files", []) or []
        print(f"  - 크롭 타임라인: {len(crop_map)}개, TTS cue 오디오: {len(tts_cue_files)}개")
        print("[OK] 리소스 로드 완료 (체크포인트에서)")
    elif start_idx <= 13:
        print("\n[13/15] 리소스 생성 중...")
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
                character_index=character_appearances,
            )
            crop_map[f"{clip.role}_{idx}"] = crop_path
            if (idx + 1) % 5 == 0 or (idx + 1) == len(clips):
                print(f"    진행 중... ({idx + 1}/{len(clips)})")

        # TTS 오디오 생성 (cue별 — voice/speed 적용)
        # cue 시간(end_sec - start_sec) 안에 들어가도록 fit. 초과 시 Flash로 텍스트 단축.
        print("  TTS 오디오 생성 중 (cue별, fit 적용)...")
        from app.modules.tts import synthesize_tts_with_fit
        _flash_for_shorten = locals().get("gemini") or load_gemini_client()
        _shorten = getattr(_flash_for_shorten, "shorten_text", None) if _flash_for_shorten else None
        tts_cue_files: list[dict[str, Any]] = []
        for ci, cue in enumerate(tts_cues):
            tts_path = output_dir / f"tts_cue_{ci}.mp3"
            target_sec = max(0.5, float(cue.get("end_sec", 0.0)) - float(cue.get("start_sec", 0.0)))
            final_text, actual_sec = synthesize_tts_with_fit(
                cue["text"], tts_path, target_sec=target_sec,
                voice=cue.get("voice", "narrative_female"),
                speed=cue.get("speed", "normal"),
                shorten_fn=_shorten,
            )
            # fit 결과를 cue에 반영(자막 일관성 + 디버깅용)
            cue["text"] = final_text
            cue["fit_actual_sec"] = actual_sec
            tts_cue_files.append({
                "cue_index": ci,
                "path": str(tts_path),
                "cue": cue,
            })
            if (ci + 1) % 3 == 0 or (ci + 1) == len(tts_cues):
                print(f"    진행 중... ({ci + 1}/{len(tts_cues)})")

        resource_elapsed = time.time() - resource_start
        checkpoint_resources.write_text(
            json.dumps({
                "crop_map": {k: str(v) for k, v in crop_map.items()},
                "tts_cue_files": tts_cue_files,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] 리소스 생성 완료 (소요 시간: {resource_elapsed:.1f}초)")
        print(f"  - TTS cue 오디오: {len(tts_cue_files)}개")
    else:
        if checkpoint_resources.exists():
            resources_data = json.loads(checkpoint_resources.read_text(encoding="utf-8"))
            crop_map = {k: Path(v) for k, v in resources_data["crop_map"].items()}
            tts_cue_files = resources_data.get("tts_cue_files", []) or []
        elif edit_plan_path.exists():
            edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8"))
            crop_map = {}
            for idx, clip_data in enumerate(edit_plan["timeline"]):
                crop_filename = clip_data["reframe"]["crop_timeline_ref"]
                crop_path = output_dir / crop_filename
                if crop_path.exists():
                    crop_map[f"{clip_data['role']}_{idx}"] = crop_path
            tts_cue_files = []
        else:
            raise FileNotFoundError("체크포인트 파일이나 edit_plan.json을 찾을 수 없습니다.")

    # 편집 계획 생성
    if start_idx <= 13:
        print("  편집 계획 생성 중...")
        edit_plan = _build_edit_plan(payload, title_text, clips, crop_map, config)
        # 라운드 6b: skeleton 단계 제거됨. edit_plan에 임베드하던 narrative_skeleton 키도 사라짐.
        edit_plan_path.write_text(json.dumps(edit_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  - 편집 계획 저장: {edit_plan_path}")

    # ═══════════════════════════════════════
    # [14/15] 자막 디자인 + 최종 렌더링
    # ═══════════════════════════════════════
    output_video = output_dir / "shorts.mp4"
    subtitle_path = output_dir / "subtitles.ass"

    if start_idx <= 14 or from_step == "render" or not output_video.exists():
        # 자막 디자인 적용
        print("\n[14/15] 자막 디자인 적용 및 최종 렌더링 중...")

        if not final_segments and segments_cache_path.exists():
            cached_data = json.loads(segments_cache_path.read_text(encoding="utf-8"))
            final_segments = [SimpleNamespace(**seg) for seg in cached_data]

        tts_subtitle_path = None
        if final_segments:
            # 자막 위치: 영상 영역 끝에서 10px 위 (사용자 요구)
            _sub_margin_v = _compute_subtitle_margin_v(
                payload.design,
                canvas_width=config.canvas_width,
                canvas_height=config.canvas_height,
                padding_px=10,
            )
            sub_style = SubtitleStyle(
                font_name=payload.design.subtitle_font,
                font_size=payload.design.subtitle_size,
                primary_color=payload.design.subtitle_color,
                margin_v=_sub_margin_v,
            )

            # TTS 자막 세그먼트 생성 (cue 절대시간 그대로 사용)
            tts_line_segs: list[SimpleNamespace] = []
            for _cf in tts_cue_files:
                _cue = _cf.get("cue", {})
                tts_line_segs.append(SimpleNamespace(
                    start_sec=float(_cue.get("start_sec", 0.0)),
                    end_sec=float(_cue.get("end_sec", 0.0)),
                    text=str(_cue.get("text", "")),
                ))

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
            tts_subtitle_path=tts_subtitle_path if payload.show_tts_subtitles else None,
            tts_cue_files=tts_cue_files if tts_cue_files else None,
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
        print(f"\n[14/15] 렌더링 스킵 (이미 파일 존재: {output_video.name})")

    # ═══════════════════════════════════════
    # [15/15] 출력 검증
    # ═══════════════════════════════════════
    if start_idx <= 15:
        print("\n[15/15] 출력 검증 중...")
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
        print("\n[15/15] 검증 단계 스킵")

    # ═══════════════════════════════════════
    # 추가 쇼츠 렌더링 (2번째, 3번째 스토리라인)
    # ═══════════════════════════════════════
    all_output_videos: list[Path] = [output_video]

    if len(all_storyline_variants) > 1 and start_idx <= 14:
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

                # TTS 생성 (variant별 cue 사용) — fit 적용으로 cue 시간 안에 들어가게 합성
                from app.modules.tts import synthesize_tts_with_fit
                _g_v = locals().get("gemini") or load_gemini_client()
                _shorten_v = getattr(_g_v, "shorten_text", None) if _g_v else None
                var_cues = tts_cues_per_variant[var_idx] if var_idx < len(tts_cues_per_variant) else []
                var_tts_cue_files: list[dict[str, Any]] = []
                for ci, cue in enumerate(var_cues):
                    tts_out = output_dir / f"tts_{var_num}_cue_{ci}.mp3"
                    if not tts_out.exists():
                        target_sec = max(0.5, float(cue.get("end_sec", 0.0)) - float(cue.get("start_sec", 0.0)))
                        final_text, actual_sec = synthesize_tts_with_fit(
                            cue["text"], tts_out, target_sec=target_sec,
                            voice=cue.get("voice", "narrative_female"),
                            speed=cue.get("speed", "normal"),
                            shorten_fn=_shorten_v,
                        )
                        cue["text"] = final_text
                        cue["fit_actual_sec"] = actual_sec
                    var_tts_cue_files.append({
                        "cue_index": ci,
                        "path": str(tts_out),
                        "cue": cue,
                    })

                # TTS 자막 (cue 절대시간 그대로)
                var_tts_segs: list[SimpleNamespace] = []
                for _cf in var_tts_cue_files:
                    _cue = _cf.get("cue", {})
                    var_tts_segs.append(SimpleNamespace(
                        start_sec=float(_cue.get("start_sec", 0.0)),
                        end_sec=float(_cue.get("end_sec", 0.0)),
                        text=str(_cue.get("text", "")),
                    ))

                # 자막 ASS 파일 생성
                var_sub_path = output_dir / f"subtitles_{var_num}.ass"
                var_tts_sub_path = output_dir / f"tts_subtitles_{var_num}.ass"

                sub_style = SubtitleStyle(
                    font_name=payload.design.subtitle_font,
                    font_size=payload.design.subtitle_size,
                    primary_color=payload.design.subtitle_color,
                    margin_v=_compute_subtitle_margin_v(
                        payload.design,
                        canvas_width=config.canvas_width,
                        canvas_height=config.canvas_height,
                        padding_px=10,
                    ),
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
                        character_index=character_appearances,
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
                    tts_subtitle_path=var_tts_sub_final if payload.show_tts_subtitles else None,
                    tts_cue_files=var_tts_cue_files if var_tts_cue_files else None,
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

    # run_log 다이어트: gemini step의 거대한 response(characters_tracking 포함 ~수십KB/청크)는
    # 같은 디렉토리의 checkpoint_gemini.json에 이미 보존되므로 run_log에는 요약만 남긴다.
    # render step의 ffmpeg argv도 검증·디버깅용 → 길이만 기록. 효과: 232KB → 5KB 수준.
    def _slim_run_log(rl: dict) -> dict:
        slim = {k: v for k, v in rl.items() if k != "steps"}
        slim_steps: list[dict] = []
        for step in rl.get("steps", []):
            s = dict(step)
            if s.get("step") == "gemini" and isinstance(s.get("response"), dict):
                resp = s["response"]
                s["response"] = {
                    "chunk_index": resp.get("chunk_index"),
                    "summary_chars": len(resp.get("summary", "")) if isinstance(resp.get("summary"), str) else 0,
                    "candidate_count": len(resp.get("candidate_moments", []) or []),
                    "segment_count": len(resp.get("segments", []) or []),
                    "characters_tracking_count": len(resp.get("characters_tracking", []) or []),
                }
            elif s.get("step") == "render" and isinstance(s.get("command"), list):
                s["command"] = {
                    "argv_count": len(s["command"]),
                    "first": s["command"][:6],  # 디버깅용 첫 인자 일부만
                }
            slim_steps.append(s)
        slim["steps"] = slim_steps
        return slim

    run_log_serializable = _make_json_serializable(_slim_run_log(run_log))
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
