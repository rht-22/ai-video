from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.story_builder import StoryClip

_VIDEO_ENCODER_CACHE: str | None = None


@dataclass(frozen=True)
class RenderInputs:
    video_path: Path
    clips: list[StoryClip]
    subtitle_path: Path | None
    crop_timeline_map: dict[str, Path]
    title_text: str
    work_title: str
    output_path: Path
    canvas_width: int
    canvas_height: int
    top_title_height: int
    bottom_label_height: int
    tts_audio_files: dict[int, Path] | None = None
    original_audio_gain_db: int = -10
    tts_audio_gain_db: int = -4
    render_preset: str = "balanced"
    enable_hwaccel: bool = True
    title_textfile: Path | None = None
    work_title_textfile: Path | None = None


def render_short(inputs: RenderInputs) -> list[str]:
    output_dir = inputs.output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if not inputs.clips:
        raise ValueError("렌더링할 clips가 비어 있습니다.")

    # [수정] 스마트 줄바꿈 로직: 단어 단위를 최대한 유지하며 가독성 있게 분리
    def _wrap_text_smart(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        
        # 공백 기준으로 단어 분리
        words = text.split()
        lines = []
        current_line = ""
        
        for word in words:
            if len(current_line) + len(word) <= max_chars:
                current_line = (current_line + " " + word).strip()
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        return "\n".join(lines)
    
    # 숏츠 가로 너비(1080) 기준, 굵은 폰트일 때 한 줄에 12~14자가 가독성이 가장 좋습니다.
    wrapped_title = _wrap_text_smart(inputs.title_text, 14)
    
    title_file = inputs.title_textfile or (output_dir / "title.txt")
    work_file = inputs.work_title_textfile or (output_dir / "work_title.txt")
    
    # UTF-8 BOM으로 저장하여 한글 깨짐 방지
    title_file.write_bytes((wrapped_title + "\n").encode("utf-8-sig"))
    work_file.write_bytes((f"작품명: {inputs.work_title}" + "\n").encode("utf-8-sig"))

    inputs = replace(inputs, title_textfile=title_file, work_title_textfile=work_file)
    tts_keys_sorted: list[int] = sorted(inputs.tts_audio_files.keys()) if inputs.tts_audio_files else []

    filter_script = _build_filtergraph(inputs, num_clip_inputs=len(inputs.clips), tts_keys_sorted=tts_keys_sorted)
    filter_path = inputs.output_path.with_suffix(".filter.txt")
    filter_path.write_text(filter_script, encoding="utf-8")

    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    output_relative = _relpath_or_abs(inputs.output_path, output_dir)

    def _build_input_args(hwaccel: str | None) -> list[str]:
        args: list[str] = ["-dn", "-sn"]
        for clip in inputs.clips:
            if hwaccel:
                args.extend(["-hwaccel", hwaccel])
            args.extend([
                "-ss", f"{clip.start_sec}",
                "-to", f"{clip.end_sec}",
                "-i", str(_relpath_or_abs(inputs.video_path, output_dir)),
            ])
        if inputs.tts_audio_files:
            for clip_idx in tts_keys_sorted:
                tts_path = inputs.tts_audio_files[clip_idx]
                args.extend(["-i", str(_relpath_or_abs(tts_path, output_dir))])
        return args

    preferred = _pick_video_encoder(ffmpeg_cmd)
    candidates = [preferred] if preferred != "libx264" else ["libx264"]
    if "libx264" not in candidates:
        candidates.append("libx264")

    last_err: Exception | None = None
    hwaccel_candidates = ["d3d11va", "cuda", None] if inputs.enable_hwaccel else [None]

    for video_encoder in candidates:
        encoder_args = _video_encoder_args(video_encoder, inputs.render_preset)
        for hwaccel in hwaccel_candidates:
            cmd_try = [
                ffmpeg_cmd, "-y",
                *_build_input_args(hwaccel),
                "-filter_complex", filter_script,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", video_encoder, *encoder_args,
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                str(output_relative),
            ]
            try:
                subprocess.check_call(cmd_try, cwd=str(output_dir))
                return cmd_try
            except subprocess.CalledProcessError as e:
                last_err = e
                continue

    raise RuntimeError(f"렌더링 실패. 마지막 오류: {last_err}")


def _pick_video_encoder(ffmpeg_path: str) -> str:
    global _VIDEO_ENCODER_CACHE
    if _VIDEO_ENCODER_CACHE: return _VIDEO_ENCODER_CACHE
    try:
        out = subprocess.check_output([ffmpeg_path, "-hide_banner", "-encoders"], text=True, encoding="utf-8", errors="replace")
    except Exception:
        _VIDEO_ENCODER_CACHE = "libx264"
        return "libx264"
    for enc in ("h264_nvenc", "h264_amf", "h264_qsv"):
        if enc in out:
            _VIDEO_ENCODER_CACHE = enc
            return enc
    _VIDEO_ENCODER_CACHE = "libx264"
    return "libx264"


def _video_encoder_args(encoder: str, preset: str) -> list[str]:
    preset = (preset or "balanced").lower()
    if encoder == "h264_nvenc":
        p = {"fastest": "p2", "quality": "p6"}.get(preset, "p4")
        return ["-preset", p, "-rc", "vbr", "-cq", "21", "-b:v", "0"]
    if encoder == "h264_amf": return ["-quality", "balanced"]
    if encoder == "h264_qsv":
        p = {"fastest": "veryfast", "quality": "slow"}.get(preset, "medium")
        return ["-preset", p]
    p = {"fastest": "ultrafast", "quality": "faster"}.get(preset, "superfast")
    return ["-preset", p, "-crf", "21"]


def _relpath_or_abs(p: Path, base: Path) -> Path:
    try: return p.relative_to(base)
    except ValueError: return p


def _escape_text_for_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("[", "\\[").replace("]", "\\]")


def _probe_video_dims(video_path: Path) -> tuple[int, int]:
    ffprobe = find_ffmpeg_command("ffprobe")
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(video_path)]
    out = subprocess.check_output(cmd, text=True, encoding="utf-8", errors="replace")
    data = json.loads(out)
    streams = data.get("streams", [])
    if not streams: raise RuntimeError(f"Video stream missing: {video_path}")
    return int(streams[0].get("width", 0)), int(streams[0].get("height", 0))


def _build_filtergraph(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
    # 캔버스 전체는 표준 9:16 (1080x1920) 유지
    W, H = 1080, 1920 
    
    # 1. 영상 크기 설정: 제목 너비에 맞춘 슬림한 가로폭 (800)
    target_video_w = 800  
    target_video_h = 1100 # 세로 길이를 적절히 유지
    target_video_w &= ~1
    target_video_h &= ~1

    # 2. 영상 배치 위치 (중앙 정렬 및 아래로 이동)
    overlay_x = (W - target_video_w) // 2
    overlay_y = 450 

    font_name = "Malgun Gothic"
    title_font_size = 70 
    work_font_size = 45

    filters = []
    
    # [Step A] 영상 변형 및 패딩 배치
    for i in range(num_clip_inputs):
        filters.append(
            f"[{i}:v]scale={target_video_w}:{target_video_h}:force_original_aspect_ratio=increase,"
            f"crop={target_video_w}:{target_video_h},"
            f"pad={W}:{H}:{overlay_x}:{overlay_y}:black[v{i}]" 
        )
        filters.append(f"[{i}:a]anull[a{i}]")

    # [Step B] 영상/오디오 합치기
    concat_combined = "".join([f"[v{i}][a{i}]" for i in range(num_clip_inputs)])
    filters.append(f"{concat_combined}concat=n={num_clip_inputs}:v=1:a=1[v_base][acat]")

    # [Step C] 제목 그리기: 상단 여백(0~500) 공간에 배치
    title_lines = inputs.title_textfile.read_text(encoding="utf-8-sig").strip().split("\n")
    current_v = "[v_base]"
    
    for i, line in enumerate(title_lines):
        escaped_line = _escape_text_for_drawtext(line.strip())
        # [수정] y 위치를 영상과 겹치지 않게 상단 여백의 적절한 위치로 고정 (약 120px 지점부터 시작)
        line_y = 200 + (i * (title_font_size + 40)) 
        next_v = f"[v_title_{i}]"
        filters.append(
            f"{current_v}drawtext=font='{font_name}':fontcolor=yellow:fontsize={title_font_size}:"
            f"text='{escaped_line}':x=(w-text_w)/2:y={line_y}:"
            f"borderw=5:bordercolor=black{next_v}"
        )
        current_v = next_v

    # [Step D] 작품명: 영상 하단 여백에 배치[cite: 3]
    work_y = overlay_y + target_video_h + 100 
    filters.append(
        f"{current_v}drawtext=font='{font_name}':fontcolor=white:fontsize={work_font_size}:"
        f"text='{_escape_text_for_drawtext(f'{inputs.work_title}')}':x=(w-text_w)/2:y={work_y}:"
        f"borderw=2:bordercolor=black[v_texts]"
    )

    # 자막 처리 및 마무리[cite: 3]
    if inputs.subtitle_path:
        sub_path = str(inputs.subtitle_path.absolute()).replace("\\", "/").replace(":", "\\:")
        filters.append(f"[v_texts]ass='{sub_path}'[vout]")
    else:
        filters.append("[v_texts]null[vout]")

    filters.append(_build_audio_filter(inputs, num_clip_inputs, tts_keys_sorted))
    return ";".join(filters)

# [_build_filtergraph : api 호출 테스트, 위에가 원본]
# def _build_filtergraph(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
#     # 캔버스 전체는 표준 9:16 (1080x1920) 유지
#     W, H = 1080, 1920 
    
#     # 1. 영상 크기 설정: 제목 너비에 맞춘 슬림한 가로폭 (800)
#     target_video_w = 800  
#     target_video_h = 1100 # 세로 길이를 적절히 유지
#     target_video_w &= ~1
#     target_video_h &= ~1

#     # 2. 영상 배치 위치 (중앙 정렬 및 아래로 이동)
#     overlay_x = (W - target_video_w) // 2
#     overlay_y = 450 

#     font_name = "Malgun Gothic"
#     title_font_size = 70 
#     work_font_size = 45

#     filters = []
    
#     # [Step A] 영상 변형 및 패딩 배치
#     for i in range(num_clip_inputs):
#         filters.append(
#             f"[{i}:v]scale={target_video_w}:{target_video_h}:force_original_aspect_ratio=increase,"
#             f"crop={target_video_w}:{target_video_h},"
#             f"pad={W}:{H}:{overlay_x}:{overlay_y}:black[v{i}]" 
#         )
#         filters.append(f"[{i}:a]anull[a{i}]")

#     # [Step B] 영상/오디오 합치기
#     concat_combined = "".join([f"[v{i}][a{i}]" for i in range(num_clip_inputs)])
#     filters.append(f"{concat_combined}concat=n={num_clip_inputs}:v=1:a=1[v_base][acat]")

#     # [Step C] 제목 그리기: 상단 여백(0~500) 공간에 배치
#     title_lines = inputs.title_textfile.read_text(encoding="utf-8-sig").strip().split("\n")
#     current_v = "[v_base]"
    
#     for i, line in enumerate(title_lines):
#         escaped_line = _escape_text_for_drawtext(line.strip())
#         # [수정] y 위치를 영상과 겹치지 않게 상단 여백의 적절한 위치로 고정 (약 120px 지점부터 시작)
#         line_y = 200 + (i * (title_font_size + 40)) 
#         next_v = f"[v_title_{i}]"
#         filters.append(
#             f"{current_v}drawtext=font='{font_name}':fontcolor=yellow:fontsize={title_font_size}:"
#             f"text='{escaped_line}':x=(w-text_w)/2:y={line_y}:"
#             f"borderw=5:bordercolor=black{next_v}"
#         )
#         current_v = next_v

#     # [Step D] 작품명: 영상 하단 여백에 배치[cite: 3]
#     work_y = overlay_y + target_video_h + 100 
#     filters.append(
#         f"{current_v}drawtext=font='{font_name}':fontcolor=white:fontsize={work_font_size}:"
#         f"text='{_escape_text_for_drawtext(f'{inputs.work_title}')}':x=(w-text_w)/2:y={work_y}:"
#         f"borderw=2:bordercolor=black[v_texts]"
#     )

#     # 자막 처리 및 마무리[cite: 3]
#     if inputs.subtitle_path:
#         sub_path = str(inputs.subtitle_path.absolute()).replace("\\", "/").replace(":", "\\:")
#         filters.append(f"[v_texts]ass='{sub_path}'[vout]")
#     else:
#         filters.append("[v_texts]null[vout]")

#     filters.append(_build_audio_filter(inputs, num_clip_inputs, tts_keys_sorted))
#     return ";".join(filters)


def _build_audio_filter(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
    original_vol = f"[acat]volume={inputs.original_audio_gain_db}dB[orig_vol]"
    if not inputs.tts_audio_files: return original_vol.replace("[orig_vol]", "[aout]")

    current = 0.0
    clip_times = []
    for clip in inputs.clips:
        clip_times.append(current)
        current += clip.end_sec - clip.start_sec

    tts_filters, mix_inputs = [], ["[orig_vol]"]
    tts_pos = {idx: p for p, idx in enumerate(tts_keys_sorted)}

    for idx, start_time in enumerate(clip_times):
        if idx not in inputs.tts_audio_files: continue
        
        in_idx = num_clip_inputs + tts_pos[idx]
        # [수정] 유튜브 숏츠 트렌드: TTS 1.5배속 적용
        tts_speed = f"[{in_idx}:a]atempo=1.5[tts{idx}_sp]"
        tts_vol = f"[tts{idx}_sp]volume={inputs.tts_audio_gain_db}dB[tts{idx}_v]"
        tts_filters.append(tts_speed)
        tts_filters.append(tts_vol)
        
        delay_ms = int(start_time * 1000)
        tts_final = f"[tts{idx}_v]adelay={delay_ms}|{delay_ms}[tts{idx}_d]"
        tts_filters.append(tts_final)
        mix_inputs.append(f"[tts{idx}_d]")

    mix_filter = f"{';'.join(tts_filters)};{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=2[aout]"
    return f"{original_vol};{mix_filter}"