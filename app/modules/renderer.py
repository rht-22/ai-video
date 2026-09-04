# from __future__ import annotations

# import json
# import subprocess
# from dataclasses import dataclass, replace, field
# from pathlib import Path
# from typing import Any, Iterable

# from app.modules.ffmpeg_utils import find_ffmpeg_command
# from app.modules.story_builder import StoryClip
# from app.config import DesignConfig,AppConfig,get_font_path

# import os
# from pathlib import Path

# _VIDEO_ENCODER_CACHE: str | None = None


# @dataclass(frozen=True)
# class RenderInputs:
#     video_path: Path
#     clips: list[StoryClip]
#     subtitle_path: Path | None  # None이면 자막 없이 렌더링
#     crop_timeline_map: dict[str, Path]
#     title_text: str
#     work_title: str
#     output_path: Path
#     canvas_width: int
#     canvas_height: int
#     top_title_height: int
#     bottom_label_height: int
#     design: DesignConfig = field(default_factory=DesignConfig)
#     tts_audio_files: dict[int, Path] | None = None  # clip_idx -> tts mp3 path
#     original_audio_gain_db: int = -10
#     tts_audio_gain_db: int = -4
#     render_preset: str = "balanced"  # balanced|fastest|quality (현재는 balanced만 사용)
#     enable_hwaccel: bool = True
#     title_textfile: Path | None = None
#     work_title_textfile: Path | None = None


# def render_short(inputs: RenderInputs) -> list[str]:
#     """
#     클립별 입력(-ss/-to -i) + concat filter로 필요한 구간만 처리한 뒤,
#     filter_complex로 비디오 합성(배경/텍스트/자막) 및 오디오 믹싱을 수행합니다.
#     - concat demuxer(inpoint/outpoint)는 컨테이너/코덱에 따라 컷이 무시되어 전체 영상을 처리할 수 있어 사용하지 않습니다.
#     - Windows 경로 호환을 위해 cwd를 output_dir로 두고 상대경로를 사용합니다.
#     """
#     output_dir = inputs.output_path.parent
#     output_dir.mkdir(parents=True, exist_ok=True)

#     if not inputs.clips:
#         raise ValueError("렌더링할 clips가 비어 있습니다.")

#     # 제목 자동 줄바꿈 처리 (캔버스 너비 초과 시)
#     def _wrap_text(text: str, max_width_px: int, font_size: int) -> str:
#         """텍스트를 최대 너비에 맞춰 줄바꿈 처리 (한글 기준 대략적 계산)"""
   

#         if len(text) <= max_width_px:
#             return text
        
#         # 공백 기준으로 단어 분리
#         words = text.split()
#         lines = []
#         current_line = ""
        
#         for word in words:
#             if len(current_line) + len(word) <= max_width_px:
#                 current_line = (current_line + " " + word).strip()
#             else:
#                 if current_line:
#                     lines.append(current_line)
#                 current_line = word
#         if current_line:
#             lines.append(current_line)
#         return "\n".join(lines)
    
#     wrapped_title = _wrap_text(inputs.title_text, 14, inputs.design.title_size)

#     title_file = inputs.title_textfile or (output_dir / "title.txt")
#     work_file = inputs.work_title_textfile or (output_dir / "work_title.txt")

#     title_file.write_bytes((wrapped_title + "\n").encode("utf-8-sig"))
#     work_file.write_bytes((f"작품명: {inputs.work_title}" + "\n").encode("utf-8-sig"))

#     inputs = replace(inputs, title_textfile=title_file, work_title_textfile=work_file)
#     tts_keys_sorted: list[int] = sorted(inputs.tts_audio_files.keys()) if inputs.tts_audio_files else []
#     filter_script = _build_filtergraph(inputs, num_clip_inputs=len(inputs.clips), tts_keys_sorted=tts_keys_sorted)
#     filter_path = inputs.output_path.with_suffix(".filter.txt")
#     filter_path.write_text(filter_script, encoding="utf-8")

#     ffmpeg_cmd = find_ffmpeg_command("ffmpeg")

#     # output_dir 기준 상대경로로 실행 (ass 필터 안정화)
#     output_relative = _relpath_or_abs(inputs.output_path, output_dir)



#     def _build_input_args(hwaccel: str | None) -> list[str]:
#         args: list[str] = ["-dn", "-sn"]
#         for clip in inputs.clips:
#             if hwaccel:
#                 args.extend(["-hwaccel", hwaccel])
#             args.extend([
#                 "-ss", f"{clip.start_sec}",
#                 "-to", f"{clip.end_sec}",
#                 "-i", str(_relpath_or_abs(inputs.video_path, output_dir)),
#             ])
#         if inputs.tts_audio_files:
#             for clip_idx in tts_keys_sorted:
#                 tts_path = inputs.tts_audio_files[clip_idx]
#                 args.extend(["-i", str(_relpath_or_abs(tts_path, output_dir))])
#         return args

#     # GPU 인코더는 환경(드라이버) 따라 실패할 수 있으므로,
#     # 1) GPU 1개만 선택해서 시도
#     # 2) 실패 시 CPU(libx264)로 폴백
#     preferred = _pick_video_encoder(ffmpeg_cmd)
#     candidates = [preferred] if preferred != "libx264" else ["libx264"]
#     if "libx264" not in candidates:
#         candidates.append("libx264")

#     last_err: Exception | None = None
#     # hwaccel_candidates: list[str | None] = [None]
#     hwaccel_candidates = ["d3d11va", "cuda", None] if inputs.enable_hwaccel else [None]

#     for video_encoder in candidates:
#         encoder_args = _video_encoder_args(video_encoder, inputs.render_preset)
#         for hwaccel in hwaccel_candidates:
#             cmd_try = [
#                 ffmpeg_cmd, "-y",
#                 *_build_input_args(hwaccel),
#                 "-filter_complex", filter_script,
#                 "-map", "[vout]", "-map", "[aout]",
#                 "-c:v", video_encoder, *encoder_args,
#                 "-pix_fmt", "yuv420p",
#                 "-c:a", "aac", "-b:a", "192k",
#                 str(output_relative),
#             ]
#             try:
#                 subprocess.check_call(cmd_try, cwd=str(output_dir))
#                 return cmd_try
#             except subprocess.CalledProcessError as e:
#                 last_err = e
#                 continue

#     raise RuntimeError(f"렌더링 실패. 마지막 오류: {last_err}")


# def _pick_video_encoder(ffmpeg_path: str) -> str:
#     """
#     가능한 경우 GPU 인코더를 사용합니다.
#     우선순위: NVENC > AMF > QSV > libx264
#     """
#     global _VIDEO_ENCODER_CACHE
#     if _VIDEO_ENCODER_CACHE:
#         return _VIDEO_ENCODER_CACHE

#     try:
#         out = subprocess.check_output(
#             [ffmpeg_path, "-hide_banner", "-encoders"],
#             text=True,
#             encoding="utf-8",
#             errors="replace",
#         )
#     except Exception:
#         _VIDEO_ENCODER_CACHE = "libx264"
#         return _VIDEO_ENCODER_CACHE

#     for enc in ("h264_nvenc", "h264_amf", "h264_qsv"):
#         if enc in out:
#             _VIDEO_ENCODER_CACHE = enc
#             return enc

#     _VIDEO_ENCODER_CACHE = "libx264"
#     return _VIDEO_ENCODER_CACHE


# def _video_encoder_args(encoder: str, preset: str) -> list[str]:
#     preset = (preset or "balanced").lower()
#     if preset not in {"fastest", "balanced", "quality"}:
#         preset = "balanced"

#     if encoder == "h264_nvenc":
#         # p1(빠름)~p7(느림/고품질)
#         if preset == "fastest":
#             return ["-preset", "p2", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
#         if preset == "quality":
#             return ["-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
#         # balanced
#         return ["-preset", "p4", "-rc", "vbr", "-cq", "17", "-b:v", "0"]

#     if encoder == "h264_amf":
#         # AMF는 드라이버/버전 편차가 커서 보수적으로 유지
#         return ["-quality", "balanced"]

#     if encoder == "h264_qsv":
#         if preset == "fastest":
#             return ["-preset", "veryfast"]
#         if preset == "quality":
#             return ["-preset", "slow"]
#         return ["-preset", "medium"]

#     # libx264
#     if preset == "fastest":
#         return ["-preset", "ultrafast", "-crf", "23"]
#     if preset == "quality":
#         return ["-preset", "faster", "-crf", "20"]
#     return ["-preset", "superfast", "-crf", "21"]


# def _relpath_or_abs(p: Path, base: Path) -> Path:
#     try:
#         return p.relative_to(base)
#     except ValueError:
#         return p


# def _escape_text_for_drawtext(text: str) -> str:
#     # drawtext에서 문제가 되는 문자 이스케이프
#     text = text.replace("\\", "\\\\")
#     text = text.replace("'", "\\'")
#     text = text.replace(":", "\\:")
#     text = text.replace("[", "\\[")
#     text = text.replace("]", "\\]")
#     return text


# def _probe_video_dims(video_path: Path) -> tuple[int, int]:
#     """
#     renderer 내부에서 원본 영상의 가로/세로를 얻어, '원본 비율 유지 + 검은 배경' 레이아웃 계산에 사용합니다.
#     """
#     ffprobe = find_ffmpeg_command("ffprobe")
#     cmd = [
#         ffprobe,
#         "-v",
#         "error",
#         "-select_streams",
#         "v:0",
#         "-show_entries",
#         "stream=width,height",
#         "-of",
#         "json",
#         str(video_path),
#     ]
#     out = subprocess.check_output(cmd, text=True, encoding="utf-8", errors="replace")
#     data: dict[str, Any] = json.loads(out)
#     streams = data.get("streams", [])
#     if not streams:
#         raise RuntimeError(f"ffprobe 결과에 video stream이 없습니다: {video_path}")
#     w = int(streams[0].get("width") or 0)
#     h = int(streams[0].get("height") or 0)
#     if w <= 0 or h <= 0:
#         raise RuntimeError(f"ffprobe에서 유효한 해상도를 얻지 못했습니다: {video_path} ({w}x{h})")
#     return w, h


# def _build_filtergraph(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
#     W = inputs.canvas_width
#     H = inputs.canvas_height
#     d = inputs.design 

#     ratio = getattr(d, 'aspect_ratio', '16:9')

#     # [1] 제목 줄바꿈 로직 (기존 유지)
#     def split_text_smart(text: str, max_chars: int = 14) -> list[str]:
#         if not text: return []
#         lines = text.split('\n')
#         res_lines = []
#         for line in lines:
#             words = line.split()
#             current_line = ""
#             for word in words:
#                 clean_word = word.split(':')[0].replace('{', '').replace('}', '') if ':' in word else word
#                 if len(current_line) + len(clean_word) <= max_chars:
#                     current_line = (current_line + " " + word).strip()
#                 else:
#                     if current_line: res_lines.append(current_line)
#                     current_line = word
#             if current_line: res_lines.append(current_line)
#         return res_lines

#     title_lines = split_text_smart(inputs.title_text, 14)

#     # [2] 비디오 및 레이아웃 위치 동적 계산
#     try:
#         r_w, r_h = map(int, ratio.split(':'))
#         scaled_h = int(W * r_h / r_w)
#     except:
#         scaled_h = W

#     # 영상 정중앙 배치
#     overlay_y = (H - scaled_h) // 2

#     # 제목(Title) 위치: 영상 시작점(overlay_y) 기준 위로 20px
#     line_spacing = 30
#     title_total_height = (len(title_lines) * d.title_size) + (max(0, len(title_lines) - 1) * line_spacing)
#     dynamic_title_y = overlay_y - title_total_height - 20 

#     # 작품명/로고(Work) 위치: 영상 끝점(overlay_y + scaled_h) 기준 아래로 20px
#     dynamic_work_y = overlay_y + scaled_h + 20

#     # FFmpeg 짝수 보정
#     scaled_w = W - (W % 2)
#     scaled_h -= (scaled_h % 2)
#     overlay_y = max(0, overlay_y)

#     filters: list[str] = []

#     # [3] 클립별 스케일 및 패딩 (기존 로직 유지)
#     for i, clip in enumerate(inputs.clips):
#         crop_key = f"{clip.role}_{i}"
#         crop_data_path = inputs.crop_timeline_map.get(crop_key)
#         crop_filter = ""
#         if crop_data_path and crop_data_path.exists():
#             try:
#                 crop_json = json.loads(crop_data_path.read_text(encoding="utf-8"))
#                 if crop_json and len(crop_json) > 0:
#                     avg_cx = sum(kf['x_center'] for kf in crop_json) / len(crop_json)
#                     cw, ch = crop_json[0]['crop_w'], crop_json[0]['crop_h']
#                     cy = crop_json[0]['y_center']
#                     crop_filter = f"crop={cw}:{ch}:{avg_cx}-{cw}/2:{cy}-{ch}/2,"
#             except: pass

#         v_filter = (
#             f"[{i}:v]{crop_filter}"
#             f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase,"
#             f"setsar=1,"
#             f"crop={W}:{scaled_h},"
#             f"pad={W}:{H}:0:{overlay_y}:black[v{i}]"
#         )
#         filters.append(v_filter)
#         filters.append(f"[{i}:a]anull[a{i}]")

#     # [4] 연결(Concat) - 기존 유지
#     concat_inputs = []
#     for i in range(num_clip_inputs):
#         concat_inputs.append(f"[v{i}]")
#         concat_inputs.append(f"[a{i}]")
#     filters.append(f"{''.join(concat_inputs)}concat=n={num_clip_inputs}:v=1:a=1[vcat][acat]")

#     # 폰트 경로
#     current_file_path = Path(__file__).resolve()
#     project_root = current_file_path.parent.parent  # /app 폴더 위치
#     font_folder = project_root / "assets" / "fonts"

#     # [5] 제목(Title) 필터
#     requested_font_name = str(d.title_font).strip()
#     custom_colors = getattr(d, 'title_colors', [d.title_color])
#     if not custom_colors:  # 만약 리스트가 비어있다면 기본값 설정
#         custom_colors = [d.title_color]
    
#     # 1. 폰트 파일 찾기 (에러 방지를 위해 리스트 순회)
#     target_font_file = None
#     if font_folder.exists():
#         for f in font_folder.iterdir():
#             if f.stem.lower() == requested_font_name.lower():
#                 target_font_file = f
#                 break

#     if target_font_file:
#         import shutil
#         # 2. 핵심: 복잡한 윈도우 경로를 피하기 위해 실행 폴더로 복사 (공백 없는 이름으로)
#         temp_font_name = "title_font_temp.ttf"
#         try:
#             shutil.copy2(str(target_font_file), temp_font_name)
#             # 경로 없이 파일명만 전달하여 'C:' 문제를 원천 차단
#             font_arg = temp_font_name
#         except:
#             # 복사 실패 시 예비책 (이스케이프 강화)
#             font_arg = str(target_font_file.resolve()).replace("\\", "/").replace(":", "\\:")
#     else:
#         font_arg = requested_font_name

#     last_v_label = "[vcat]"
#     # 3. 필터 구성
#     for idx, raw_line in enumerate(title_lines):
#         base_color = custom_colors[idx] if idx < len(custom_colors) else custom_colors[-1]
#         y_pos = d.title_y + (idx * (d.title_size + line_spacing))
#         escaped_full = _escape_text_for_drawtext(raw_line)
#         next_label = f"[title_{idx}]"
        
#         # fontfile='title_font_temp.ttf' 형태로 아주 깨끗하게 들어감
#         drawtext_cmd = (
#             f"drawtext=fontfile='{font_arg}':"
#             f"text='{escaped_full}':"
#             f"fontcolor={base_color}:"
#             f"fontsize={d.title_size}:"
#             f"x=(w-text_w)/2:y={y_pos}"
#         )
#         filters.append(f"{last_v_label}{drawtext_cmd}{next_label}")
#         last_v_label = next_label

  
#     # [6] 작품명(Logo) 필터
#     work_label = "[with_work]"
#     work_type = getattr(d, 'work_type', 'text')
#     work_value = getattr(d, 'work_value', inputs.work_title)
#     if work_type == "image" and work_value:
#         logo_path_str = str(Path(work_value).resolve()).replace("\\", "/").replace(":", "\\:")
#         logo_w = getattr(d, 'work_image_width', 200)
#         filters.append(f"movie='{logo_path_str}',scale={logo_w}:-1[logo];{last_v_label}[logo]overlay=(W-w)/2:{dynamic_work_y}{work_label}")
#     else:
#         escaped_val = _escape_text_for_drawtext(work_value if work_value else inputs.work_title)
#         filters.append(f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_val}':fontcolor={d.work_color}:fontsize={d.work_font_size}:x=(w-text_w)/2:y={dynamic_work_y}{work_label}")


    
#     #  [7] 자막 설정
#     if inputs.subtitle_path:
#         # 1. 클래스 기본값(DesignConfig.subtitle_font) 가져오기
#         default_font = DesignConfig.subtitle_font
#         # 2. API로 받은 실제 이름 (예: "여기어때 잘난체 2 TTF")
#         requested_font = d.subtitle_font
        
#         ass_path = inputs.subtitle_path.resolve()
#         ass_content = ass_path.read_text(encoding="utf-8")
        
#         # 3. 파일 안의 기본 이름을 실제 이름으로 치환
#         ass_content = ass_content.replace(default_font, requested_font)
#         ass_path.write_text(ass_content, encoding="utf-8")

#         sub_path_fixed = str(ass_path).replace("\\", "/").replace(":", "\\:")
#         font_dir_fixed = str(font_folder.resolve()).replace("\\", "/").replace(":", "\\:")
        
#         filters.append(f"{work_label}ass='{sub_path_fixed}':original_size={W}x{H}:fontsdir='{font_dir_fixed}'[vout]")
#     else:
#         filters.append(f"{work_label}null[vout]")
    
#     filters.append(_build_audio_filter(inputs, num_clip_inputs, tts_keys_sorted))
#     return ";".join(filters)

# def _build_audio_filter(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
#     # concat된 원본 오디오 볼륨 조절
#     original_vol = f"[acat]volume={inputs.original_audio_gain_db}dB[orig_vol]"

#     if not inputs.tts_audio_files:
#         return original_vol.replace("[orig_vol]", "[aout]")

#     # 각 클립의 시작 시간(편집 타임라인 기준) 계산
#     clip_times: list[tuple[float, StoryClip]] = []
#     current = 0.0
#     for clip in inputs.clips:
#         clip_times.append((current, clip))
#         current += clip.end_sec - clip.start_sec

#     tts_filters: list[str] = []
#     mix_inputs: list[str] = ["[orig_vol]"]

#     # 입력 인덱스: 0..(num_clip_inputs-1)=클립, num_clip_inputs..=tts (tts_keys_sorted 순서)
#     tts_pos = {clip_idx: pos for pos, clip_idx in enumerate(tts_keys_sorted)}
#     for clip_idx, (start_time, _) in enumerate(clip_times):
#         if clip_idx not in inputs.tts_audio_files:
#             continue
#         tts_input_idx = num_clip_inputs + tts_pos[clip_idx]
#         # TTS 속도 1.5배 적용 (최신 트렌드)
#         tts_speed = f"[{tts_input_idx}:a]atempo=1.2[tts{clip_idx}_speed]"
#         tts_filters.append(tts_speed)
#         tts_vol = f"[tts{clip_idx}_speed]volume={inputs.tts_audio_gain_db}dB[tts{clip_idx}_vol]"
#         tts_filters.append(tts_vol)
#         if start_time > 0:
#             delay_ms = int(start_time * 1000)
#             tts_delayed = (
#                 f"[tts{clip_idx}_vol]adelay={delay_ms}|{delay_ms}[tts{clip_idx}_delayed]"
#             )
#             tts_filters.append(tts_delayed)
#             mix_inputs.append(f"[tts{clip_idx}_delayed]")
#         else:
#             mix_inputs.append(f"[tts{clip_idx}_vol]")

#     if len(mix_inputs) <= 1:
#         return original_vol.replace("[orig_vol]", "[aout]")

#     # amix 입력은 공백 없이 연결해야 함: [a][b][c]amix=...
#     mix_inputs_str = "".join(mix_inputs)
#     mix_filter = (
#         f"{';'.join(tts_filters)};{mix_inputs_str}"
#         f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=2[aout]"
#     )
#     return f"{original_vol};{mix_filter}"



from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass, replace, field
from pathlib import Path
from typing import Any, Iterable

from app.modules.edit_overrides import validate_title_segments
from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.story_builder import StoryClip
from app.config import DesignConfig,AppConfig

_VIDEO_ENCODER_CACHE: str | None = None


def _build_crop_expr(keyframes: list[dict], axis: str) -> str:
    """키프레임 → ffmpeg crop x/y용 piecewise linear 시간 표현식.

    axis: 'x_center' 또는 'y_center'.
    클립 입력은 -ss로 0초부터 시작한다고 가정 → time을 첫 KF 기준으로 정규화.
    표현식 길이 가드: 80개 초과 시 다운샘플.
    """
    if not keyframes:
        return "0"
    if len(keyframes) > 80:
        step = (len(keyframes) // 80) + 1
        keyframes = keyframes[::step] + [keyframes[-1]]
    t0 = keyframes[0]['time_sec']
    pts = [(kf['time_sec'] - t0, float(kf[axis])) for kf in keyframes]
    if len(pts) == 1:
        return f"{pts[0][1]:.2f}"
    expr = f"{pts[-1][1]:.2f}"
    for i in range(len(pts) - 2, -1, -1):
        t_a, v_a = pts[i]
        t_b, v_b = pts[i + 1]
        dt = max(t_b - t_a, 1e-3)
        lerp = f"({v_a:.2f}+({v_b - v_a:.2f})*(t-{t_a:.3f})/{dt:.3f})"
        expr = f"if(lt(t,{t_b:.3f}),{lerp},{expr})"
    return expr


def sanitize_clips(
    clips: list[StoryClip],
    source_duration_sec: float | None,
    *,
    min_len_sec: float = 0.2,
    max_overshoot_sec: float = 3.0,
) -> tuple[list[StoryClip], list[str]]:
    """렌더 전에 **물리적으로 불가능한 컷**을 걸러낸다 → (살아남은 컷, 사유 메모). 순수.

    왜 필요한가: ffmpeg 는 이런 컷을 만나면 `-ss`/`-to` 단계에서 죽거나(Invalid argument)
    비디오 0프레임짜리 파일을 만들어 낸다. 그 실패는 **분석이 다 끝난 렌더 단계**에서야
    드러나므로 30~90분과 Gemini 비용을 그대로 버린다. 2026-08-05 하루에 두 번 겪었다:
      · 샤먼 2화  — 컷 [948, 997] 인데 소스는 875초 → 0프레임 → 블랙프레임 검사가 exit 234
      · 약한영웅  — 컷 [2348.68, 2348.52] (시작>끝) → `Error opening input files: Invalid argument`
    두 경우 다 여기서 한 번 보면 걸러진다.

    고치는 방향은 **안전측 축소**뿐이다 — 범위를 넘으면 자르고, 자를 수 없으면 버린다.
    없는 구간을 만들어내지 않는다(추측 금지).

    ⚠️ **크게 넘친 컷은 자르지 않고 버린다**(max_overshoot_sec). 소수점 몇 초는 경계 반올림이지만,
    수십 초씩 넘치는 것은 그 청크의 **시간축 자체가 밀렸다는 신호**라 잘라낸 구간의 내용도 믿을 수
    없다(2026-08-06 샤먼 2화: 인용 대사의 실제 위치가 310초 어긋나 있었다). 잘라서 렌더하면
    엉뚱한 장면이 조용히 발행되므로, 실패로 두는 편이 안전하다. 내용 대조는 별도로
    modules/timestamp_check 가 렌더 전에 한다."""
    clean: list[StoryClip] = []
    notes: list[str] = []
    dur = float(source_duration_sec) if source_duration_sec else None

    for i, c in enumerate(clips):
        try:
            start, end = float(c.start_sec), float(c.end_sec)
        except (TypeError, ValueError):
            notes.append(f"컷{i} 버림 — 시간이 숫자가 아님({c.start_sec!r}~{c.end_sec!r})")
            continue
        if start != start or end != end:            # NaN
            notes.append(f"컷{i} 버림 — 시간이 NaN")
            continue

        if start < 0:
            notes.append(f"컷{i} 시작 {start:.2f}s → 0s 로 보정")
            start = 0.0
        if dur is not None and end > dur + max_overshoot_sec:
            notes.append(f"컷{i} 버림 — 끝 {end:.2f}s 가 소스 길이 {dur:.2f}s 를 "
                         f"{end - dur:.1f}s 초과(경계 오차가 아니라 시간축이 밀린 것)")
            continue
        if dur is not None and end > dur:
            notes.append(f"컷{i} 끝 {end:.2f}s → 소스 길이 {dur:.2f}s 로 잘림(경계 오차)")
            end = dur
        if dur is not None and start >= dur:
            notes.append(f"컷{i} 버림 — 시작 {start:.2f}s 가 소스 길이 {dur:.2f}s 밖")
            continue
        if end - start < min_len_sec:
            notes.append(f"컷{i} 버림 — 길이 {end - start:.2f}s (시작 {start:.2f} ≥ 끝 {end:.2f} 포함)")
            continue

        clean.append(c if (start == c.start_sec and end == c.end_sec)
                     else replace(c, start_sec=start, end_sec=end))
    return clean, notes


@dataclass(frozen=True)
class RenderInputs:
    video_path: Path
    clips: list[StoryClip]
    subtitle_path: Path | None  # None이면 일반 자막 없이 렌더링
    crop_timeline_map: dict[str, Path]
    title_text: str
    work_title: str
    output_path: Path
    canvas_width: int
    canvas_height: int
    top_title_height: int
    bottom_label_height: int
    design: DesignConfig = field(default_factory=DesignConfig)
    tts_subtitle_path: Path | None = None  # TTS 자막 전용 ASS (show_subtitles와 무관하게 표시)
    tts_cue_files: list[dict] | None = None  # 각 항목: {"cue_index": int, "path": str, "cue": {start_sec, end_sec, text, voice, speed}}
    original_audio_gain_db: int = -10
    tts_audio_gain_db: int = -4
    # 최종 출력 라우드니스 정규화 목표(LUFS). 쇼츠 표준 ≈ -14. None 이면 비활성(A/B 대조군용).
    # ai-video 클립이 시장 클립 대비 ~9 LUFS 더 조용한 문제(벤치마크) 교정.
    loudness_target_lufs: float | None = -14.0
    render_preset: str = "balanced"  # balanced|fastest|quality (현재는 balanced만 사용)
    enable_hwaccel: bool = True
    title_textfile: Path | None = None
    work_title_textfile: Path | None = None
    # 편집실 이미지 오버레이(edit_overrides/v3 images, F-408) — place_anchored_images 로
    # 배치가 끝난 항목: {file(절대 경로), start_sec, end_sec(편집본 시간축), x, y, w, layer}
    image_overlays: list[dict] | None = None
    # 시간대별 제목(E8, edit_overrides/v3 title.segments) — 각 항목:
    # {text, start_sec, end_sec(편집본 시간축)}. 있으면 그 창들만 그린다(창 밖 = 제목
    # 없음, title_text 는 무시). 창의 ×1/S(배속) 변환은 필터 조립부가 한다.
    title_segments: list[dict] | None = None
    # 편집실 자유 텍스트(edit_overrides/v3 texts, F-411) — build_texts_ass 가 쓴 ASS.
    # 대사 자막·TTS 자막 위, images layer≥1 아래에 입힌다. None/부재 = 레이어 없음.
    text_subtitle_path: Path | None = None
    # E19-5 효과음(SFX) — {path(파일), start_sec(편집본 시간축), gain_db}. cue 와 같은
    # 방식으로 입력을 더하고(adelay + volume dB + amix) 원본 오디오를 덕킹하지는 않는다
    # (짧은 스팅에 덕킹을 걸면 원음이 펌핑한다). None/빈 목록 = 입력·필터 종전과 동일.
    sfx_audio: list[dict] | None = None
    # V3-M4(2026-08-31): 원본 오디오 뮤트 창 — (start_sec, end_sec) 편집본 시간축.
    # v3 의 use_original_audio=False 클립(TTS 슬롯 ⓑ 뮤트)이 여기 실린다 — 원본
    # 트랙([acat])에만 volume=0 을 걸어 cue 오디오는 그대로 산다. None/빈 목록 =
    # 필터 종전과 완전히 동일(v1 회귀 0 — sfx_audio 와 같은 additive 규약).
    muted_windows: list[tuple[float, float]] | None = None
    # 2026-09-04(사용자 요청): 뮤트 창의 원본 볼륨 — None = 종전 volume=0(완전 무음).
    # dB 음수(예: -12)를 주면 그 창에서 원본을 **줄이기만** 한다(내레이션 밑에 현장음이
    # 남는다). 이때 cue 덕킹(×0.5)은 뮤트 창 **밖**에서만 걸어 두 감쇠가 겹쳐 쌓이지
    # 않게 한다 — None 이면 0×0.5=0 이라 필터 문자열 종전과 바이트 동일(회귀 0).
    muted_gain_db: float | None = None
    # 2026-09-03: 소스 fps. 주면 클립을 **프레임 정수 개로 고정**해서 낸다(아래 [3]).
    # concat 이 세그먼트 길이를 소리에 맞추며 프레임을 덧대는 바람에 실제 편집본이
    # 계획보다 길어지고, 계획 좌표로 찍은 뮤트 창·자막·cue·라벨·효과음이 뒤로 갈수록
    # 어긋났다(실측 최대 0.3초 — 덮개 꼬리 대사 유출). assemble.clip_frames 와 같은 식.
    # None = 종전과 완전히 동일한 필터그래프(회귀 0).
    source_fps: float | None = None


def render_short(inputs: RenderInputs) -> list[str]:
    """
    클립별 입력(-ss/-to -i) + concat filter로 필요한 구간만 처리한 뒤,
    filter_complex로 비디오 합성(배경/텍스트/자막) 및 오디오 믹싱을 수행합니다.
    - concat demuxer(inpoint/outpoint)는 컨테이너/코덱에 따라 컷이 무시되어 전체 영상을 처리할 수 있어 사용하지 않습니다.
    - Windows 경로 호환을 위해 cwd를 output_dir로 두고 상대경로를 사용합니다.
    """
    output_dir = inputs.output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if not inputs.clips:
        raise ValueError("렌더링할 clips가 비어 있습니다.")

    # ── 컷 시간 검증 (렌더 전) ──
    # 소스 길이는 ffprobe 1회(≈50ms). 실패하면 길이 검사만 건너뛰고 시작>끝 검사는 그대로 한다
    # — 프로브가 안 된다고 렌더를 막으면 종전에 되던 것까지 막힌다.
    try:
        from app.modules.media_probe import probe_media
        source_duration = probe_media(inputs.video_path).duration_sec
    except Exception as e:  # noqa: BLE001 — 프로브 실패는 치명적이지 않다
        print(f"  [WARN] 소스 길이 확인 실패 — 길이 검사 생략: {e}")
        source_duration = None

    clean_clips, notes = sanitize_clips(list(inputs.clips), source_duration)
    for n in notes:
        print(f"  [WARN] 컷 검증: {n}")
    if not clean_clips:
        raise ValueError(
            "렌더 가능한 컷이 없습니다 — 모든 컷이 소스 범위 밖이거나 시작≥끝입니다"
            + (f" (소스 {source_duration:.2f}s)" if source_duration else "")
            + f". 원본 컷: {[(c.start_sec, c.end_sec) for c in inputs.clips]}"
        )
    if len(clean_clips) != len(inputs.clips):
        print(f"  [WARN] 컷 {len(inputs.clips)}개 중 {len(clean_clips)}개만 렌더합니다")
    inputs = replace(inputs, clips=clean_clips)

    # 제목 자동 줄바꿈 처리 (캔버스 너비 초과 시)
    def _wrap_text(text: str, max_width_px: int, font_size: int) -> str:
        """텍스트를 최대 너비에 맞춰 줄바꿈 처리 (한글 기준 대략적 계산)"""
   

        if len(text) <= max_width_px:
            return text
        
        # 공백 기준으로 단어 분리
        words = text.split()
        lines = []
        current_line = ""
        
        for word in words:
            if len(current_line) + len(word) <= max_width_px:
                current_line = (current_line + " " + word).strip()
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        return "\n".join(lines)
    
    # 좌우 padding을 둔 텍스트박스 폭 기준으로 자동 줄바꿈 + 줄 수 기반 폰트 축소
    title_padding_px = 60  # 캔버스 좌우 각각 60px (총 120px 여백)
    title_max_width_px = max(200, inputs.canvas_width - 2 * title_padding_px)

    def _max_chars_for(font_size: int) -> int:
        # 한글 글자 폭은 폰트 크기와 거의 동일 (font_size px당 ~1글자)
        return max(8, int(title_max_width_px / max(1, font_size)))

    base_size = inputs.design.title_size
    wrapped_title = _wrap_text(inputs.title_text, _max_chars_for(base_size), base_size)
    # E8: 시간대별 제목이면 창 밖에서 title_text 는 안 그려진다 — 줄 수 기반 폰트 축소는
    # 전 세그먼트 공통 title_size(디자인 레벨)에 대한 것이므로, 실제로 그려질 세그먼트
    # 중 **가장 줄이 많은 것**을 기준으로 한 번만 정한다(기준선 고정과 같은 이유 —
    # 세그먼트마다 크기가 널뛰면 그건 세그먼트별 스타일이고, 이번 판 범위 밖이다).
    if inputs.title_segments:
        title_lines = max(
            sum(_wrap_text(ln, _max_chars_for(base_size), base_size).count("\n") + 1
                for ln in str(sg["text"]).split("\n") if ln.strip())
            for sg in inputs.title_segments
        )
    else:
        title_lines = wrapped_title.count("\n") + 1

    if title_lines >= 4:
        scaled_title_size = max(24, int(base_size * 0.70))
    elif title_lines == 3:
        scaled_title_size = max(28, int(base_size * 0.85))
    else:
        scaled_title_size = base_size

    if scaled_title_size != base_size:
        # 줄어든 폰트로 재계산 (더 잘 들어갈 가능성 — 4줄→3줄 등)
        wrapped_title = _wrap_text(
            inputs.title_text, _max_chars_for(scaled_title_size), scaled_title_size
        )
        inputs = replace(
            inputs,
            design=replace(inputs.design, title_size=scaled_title_size),
        )

    title_file = inputs.title_textfile or (output_dir / "title.txt")
    work_file = inputs.work_title_textfile or (output_dir / "work_title.txt")

    title_file.write_bytes((wrapped_title + "\n").encode("utf-8-sig"))
    work_file.write_bytes((inputs.work_title + "\n").encode("utf-8-sig"))

    inputs = replace(inputs, title_textfile=title_file, work_title_textfile=work_file)

    # 안전망: 영상이 끝난 뒤 시작하는 cue 는 싣지 않는다(cues_within_video 독스트링).
    # ⚠ **여기서 한 번만** 거른다 — 아래 _build_input_args 와 _build_audio_filter 가
    # 같은 목록을 봐야 cue 입력 인덱스(num_clip_inputs + ci)가 어긋나지 않는다.
    _kept_cues, _late_cues = cues_within_video(
        inputs.tts_cue_files, inputs.clips,
        float(getattr(inputs.design, "video_speed", 1.0) or 1.0))
    if _late_cues:
        _out_dur = video_out_duration(
            inputs.clips, float(getattr(inputs.design, "video_speed", 1.0) or 1.0))
        for _cf in _late_cues:
            _c = _cf.get("cue") or {}
            print(f"  [cue-late] 영상({_out_dur:.2f}s) 밖에서 시작 → 드롭: "
                  f"start={_c.get('start_sec')} end={_c.get('end_sec')} "
                  f"{str(_c.get('text', ''))[:24]!r}")
        inputs = replace(inputs, tts_cue_files=_kept_cues)

    # E19-5: SFX 도 같은 안전망을 **여기서 한 번만** 탄다 — 아래 두 조립부가 같은 목록을
    # 봐야 입력 인덱스(클립+cue+si)가 어긋나지 않는다(cue 규율과 동일).
    if getattr(inputs, "sfx_audio", None):
        _kept_sfx, _late_sfx = sfx_within_video(
            inputs.sfx_audio, inputs.clips,
            float(getattr(inputs.design, "video_speed", 1.0) or 1.0))
        for _sf in _late_sfx:
            print(f"  [sfx-late] 영상 밖에서 시작 → 드롭: start={_sf.get('start_sec')} "
                  f"{Path(str(_sf.get('path', ''))).name}")
        if _late_sfx:
            inputs = replace(inputs, sfx_audio=_kept_sfx or None)

    num_cue_inputs = len(inputs.tts_cue_files or [])
    filter_script = _build_filtergraph(inputs, num_clip_inputs=len(inputs.clips), num_cue_inputs=num_cue_inputs)
    filter_path = inputs.output_path.with_suffix(".filter.txt")
    filter_path.write_text(filter_script, encoding="utf-8")

    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")

    # output_dir 기준 상대경로로 실행 (ass 필터 안정화)
    output_relative = _relpath_or_abs(inputs.output_path, output_dir)
    # 필터 스크립트도 같은 규약 — 여기만 원본 경로를 넘기면 output_path 가 상대경로일 때
    # cwd=output_dir 에서 ffmpeg 가 파일을 못 연다(2026-09-02 실사고: --outdir outputs
    # 상대 실행에서 최종 렌더만 즉사. 절대경로 실행은 종전과 동일하게 통과한다).
    filter_relative = _relpath_or_abs(filter_path, output_dir)



    def _build_input_args(hwaccel: str | None) -> list[str]:
        args: list[str] = ["-dn", "-sn"]
        for clip in inputs.clips:
            if hwaccel:
                args.extend(["-hwaccel", hwaccel])
            args.extend([
                "-thread_queue_size", "512",
                "-ss", f"{clip.start_sec}",
                "-to", f"{clip.end_sec}",
                "-i", str(_relpath_or_abs(inputs.video_path, output_dir)),
            ])
        if inputs.tts_cue_files:
            for cf in inputs.tts_cue_files:
                cue_path = Path(cf["path"]) if isinstance(cf.get("path"), str) else cf.get("path")
                if cue_path is None:
                    continue
                args.extend(["-i", str(_relpath_or_abs(cue_path, output_dir))])
        # E19-5: SFX 입력은 cue 뒤 — _build_audio_filter 의 인덱스(클립+cue+si)와 짝.
        for sf in (getattr(inputs, "sfx_audio", None) or []):
            sfx_path = Path(sf["path"]) if isinstance(sf.get("path"), str) else sf.get("path")
            if sfx_path is None:
                continue
            args.extend(["-i", str(_relpath_or_abs(sfx_path, output_dir))])
        return args

    # GPU 인코더는 환경(드라이버) 따라 실패할 수 있으므로,
    # 1) GPU 1개만 선택해서 시도
    # 2) 실패 시 CPU(libx264)로 폴백
    preferred = _pick_video_encoder(ffmpeg_cmd)
    candidates = [preferred] if preferred != "libx264" else ["libx264"]
    if "libx264" not in candidates:
        candidates.append("libx264")

    last_err: Exception | None = None
    # hwaccel_candidates: list[str | None] = [None]
    hwaccel_candidates = ["d3d11va", "cuda", None] if inputs.enable_hwaccel else [None]

    for video_encoder in candidates:
        encoder_args = _video_encoder_args(video_encoder, inputs.render_preset)
        for hwaccel in hwaccel_candidates:
            cmd_try = [
                ffmpeg_cmd, "-y",
                *_build_input_args(hwaccel),
                "-filter_complex_script", str(filter_relative),
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
    """
    가능한 경우 GPU 인코더를 사용합니다.
    우선순위: NVENC > AMF > QSV > libx264
    """
    global _VIDEO_ENCODER_CACHE
    if _VIDEO_ENCODER_CACHE:
        return _VIDEO_ENCODER_CACHE

    try:
        out = subprocess.check_output(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        _VIDEO_ENCODER_CACHE = "libx264"
        return _VIDEO_ENCODER_CACHE

    for enc in ("h264_nvenc", "h264_amf", "h264_qsv"):
        if enc in out:
            _VIDEO_ENCODER_CACHE = enc
            return enc

    _VIDEO_ENCODER_CACHE = "libx264"
    return _VIDEO_ENCODER_CACHE


def _video_encoder_args(encoder: str, preset: str) -> list[str]:
    preset = (preset or "balanced").lower()
    if preset not in {"fastest", "balanced", "quality"}:
        preset = "balanced"

    if encoder == "h264_nvenc":
        # p1(빠름)~p7(느림/고품질)
        if preset == "fastest":
            return ["-preset", "p2", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
        if preset == "quality":
            return ["-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
        # balanced
        return ["-preset", "p4", "-rc", "vbr", "-cq", "17", "-b:v", "0"]

    if encoder == "h264_amf":
        # AMF는 드라이버/버전 편차가 커서 보수적으로 유지
        return ["-quality", "balanced"]

    if encoder == "h264_qsv":
        if preset == "fastest":
            return ["-preset", "veryfast"]
        if preset == "quality":
            return ["-preset", "slow"]
        return ["-preset", "medium"]

    # libx264
    if preset == "fastest":
        return ["-preset", "ultrafast", "-crf", "23"]
    if preset == "quality":
        return ["-preset", "faster", "-crf", "20"]
    return ["-preset", "superfast", "-crf", "21"]


def _relpath_or_abs(p: Path, base: Path) -> Path:
    """ffmpeg 에 넘길 경로 — base(=cwd 가 될 output_dir) 기준 상대경로, 안 되면 절대경로.

    ⚠ 상대경로 입력은 **프로세스 cwd 기준으로 먼저 절대화**한다(2026-09-03 실사고:
    `--video sources_local/x.mp4` 처럼 상대로 주면 relative_to 가 실패해 상대경로가
    그대로 나가고, ffmpeg 는 cwd=output_dir 에서 그 파일을 못 찾아 최종 렌더만 즉사한다.
    초안 렌더는 cwd 를 안 바꿔 통과하므로 여기서만 터진다). 절대경로 입력은 종전 그대로."""
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    try:
        return p.relative_to(base)
    except ValueError:
        return p


def _to_short_path(path: str) -> str:
    """Windows에서 한글 등 유니코드 경로를 FFmpeg이 인식할 수 있는 8.3 단축 경로로 변환."""
    import os
    if os.name != 'nt':
        return path
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetShortPathNameW(path, buf, 32768)
    return buf.value or path


def _escape_text_for_drawtext(text: str) -> str:
    # drawtext에서 문제가 되는 문자 이스케이프
    # ※ '%'는 여기서 처리하지 않는다. drawtext가 '%'를 확장 문법으로 해석해
    #    해당 필터를 통째로 스킵(rc=0, "Stray %" 경고만)하는 문제는 호출부의
    #    expansion=none 으로 막는다. '%%' 치환은 동작하지 않음(실측).
    # ※ 작은따옴표는 이스케이프가 아니라 **타이포그래피 따옴표(’)로 치환**한다. drawtext 의
    #    text 는 작은따옴표로 감싸는데, 그 안에서는 백슬래시가 이스케이프로 동작하지 않아
    #    "\'" 를 넣으면 필터 파싱이 깨져 그 줄이 통째로 사라진다(% 와 같은 침묵 실패).
    #    실측한 대안 중 "\\'" 는 백슬래시가 화면에 찍히고, "'\''" 는 따옴표가 사라진다 —
    #    글자가 그대로 보이는 방법은 ’ 치환뿐이었다(2026-08-10, ffmpeg 7.1.5/8.1.2).
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "’")
    text = text.replace(":", "\\:")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    return text


def _probe_video_dims(video_path: Path) -> tuple[int, int]:
    """
    renderer 내부에서 원본 영상의 가로/세로를 얻어, '원본 비율 유지 + 검은 배경' 레이아웃 계산에 사용합니다.
    """
    ffprobe = find_ffmpeg_command("ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, text=True, encoding="utf-8", errors="replace")
    data: dict[str, Any] = json.loads(out)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"ffprobe 결과에 video stream이 없습니다: {video_path}")
    w = int(streams[0].get("width") or 0)
    h = int(streams[0].get("height") or 0)
    if w <= 0 or h <= 0:
        raise RuntimeError(f"ffprobe에서 유효한 해상도를 얻지 못했습니다: {video_path} ({w}x{h})")
    return w, h



def _parse_drawtext_color(color: str) -> tuple[int, int, int, int]:
    """drawtext 색 문자열(#RRGGBB · white · black@0.6 · #RRGGBBAA)을 Pillow RGBA 로.
    모르는 이름은 검정 — 박스가 안 보이는 것보다 검은 박스가 눈에 띄어 고치기 쉽다."""
    from PIL import ImageColor
    s = str(color).strip()
    alpha = 1.0
    if "@" in s:
        s, a = s.rsplit("@", 1)
        try:
            alpha = min(1.0, max(0.0, float(a)))
        except ValueError:
            alpha = 1.0
    try:
        rgb = ImageColor.getrgb(s)
    except ValueError:
        rgb = (0, 0, 0)
    r, g, b = rgb[:3]
    a = rgb[3] if len(rgb) == 4 else 255
    return (r, g, b, int(round(a * alpha)))


def _measure_title_text_width(text: str, font_path: str, font_size: int) -> int:
    """drawtext 가 그릴 글자 폭의 근사 — 같은 TTF 를 Pillow 로 재서 둥근 박스 PNG 폭을 정한다.
    ffmpeg 와 수 px 차이는 박스 여백(0.30em)이 흡수한다. 폰트 파일이 아니면(테스트·폰트명만
    온 경우) 1em/글자 근사 — 한글·CJK 는 거의 정사각이라 충분하다."""
    try:
        if font_path and Path(font_path).is_file():
            from PIL import ImageFont
            return int(math.ceil(ImageFont.truetype(font_path, font_size).getlength(text)))
    except Exception:
        pass
    return font_size * max(1, len(text))


def _make_title_box_png(text: str, font_path: str, font_size: int, color: str,
                        pad: int, radius: int, stroke_w: int, out_path: Path) -> tuple[Path, int, int]:
    """둥근네모 제목 배경 — drawtext 의 box 는 모서리를 못 둥글리므로 Pillow 로 RGBA PNG 를
    만들어 movie+overlay 로 글자 **아래**에 깐다(로고·이미지 오버레이와 같은 경로).
    반환 (경로, 폭, 높이). 폭·높이는 짝수로 맞춘다(yuv 오버레이 안전)."""
    from PIL import Image, ImageDraw
    text_w = _measure_title_text_width(text, font_path, font_size)
    box_w = text_w + 2 * pad + 2 * stroke_w
    box_h = font_size + 2 * pad
    box_w += box_w % 2
    box_h += box_h % 2
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        [0, 0, box_w - 1, box_h - 1], radius=max(0, radius), fill=_parse_drawtext_color(color))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path, box_w, box_h


_TITLE_BOX_STYLES = ("none", "round", "rect")


# ─────────────────────────────────────────────────────────────
# E19-2: 제목 단어 단위 색 강조 (2026-08-28)
# ─────────────────────────────────────────────────────────────
# 발주서: docs/prompts/e19-drama-clip-preset.md §2. 벤치마크(신병4)의 "가장 **X같은**
# 질문" — 제목 문자열 안 `{{어절}}` 마크업이 그 어절만 design.title_highlight_color 로
# 그려진다. **마크업이 없으면 이 경로 전체가 no-op** — 렌더 필터그래프가 종전과 바이트
# 동일하다(회귀 0, E10 문자열 가드가 함께 지킨다).

_TITLE_HL_TOKEN_RE = re.compile(r"(\{\{|\}\})")


def strip_title_markup(text: str) -> tuple[str, list[str]]:
    """잔여물 정리 — 균형 잡힌 `{{…}}` 쌍만 남기고 나머지 마커는 뗀다(경고 목록 반환).

    홀짝 불일치·중첩·빈 강조(`{{}}`)는 마커만 제거하고 건별 경고 — 제목이 깨진 채
    (중괄호 노출) 발행되는 것이 최악이다(발주서 §2). 마크업 없는 제목은 그대로
    돌려준다(빠른 경로 — 기존 제목 전부가 이 줄에서 끝난다)."""
    if "{{" not in text and "}}" not in text:
        return text, []
    tokens = _TITLE_HL_TOKEN_RE.split(text)
    warnings: list[str] = []
    keep: set[int] = set()
    open_idx: int | None = None
    for i, tok in enumerate(tokens):
        if tok == "{{":
            if open_idx is None:
                open_idx = i
            else:
                warnings.append(f"중첩된 {{{{ — 마커 제거 ({text[:24]!r})")
        elif tok == "}}":
            if open_idx is not None:
                inner = "".join(tokens[open_idx + 1:i])
                if inner:
                    keep.add(open_idx)
                    keep.add(i)
                else:
                    warnings.append(f"빈 강조 {{{{}}}} — 마커 제거 ({text[:24]!r})")
                open_idx = None
            else:
                warnings.append(f"짝 없는 }}}} — 마커 제거 ({text[:24]!r})")
    if open_idx is not None:
        warnings.append(f"닫히지 않은 {{{{ — 마커 제거 ({text[:24]!r})")
    out = "".join(tok if tok not in ("{{", "}}") or i in keep else ""
                  for i, tok in enumerate(tokens))
    return out, warnings


def extract_title_highlights(
    lines: list[tuple[int, str]],
) -> tuple[list[tuple[int, str]], dict[int, list[tuple[str, bool]]]]:
    """wrap 된 줄들에서 마커를 떼고, 강조가 있는 줄의 세그먼트를 돌려준다.

    입력은 strip_title_markup 을 지난(= 마커가 전역 균형인) 줄 목록. 강조가 wrap
    경계를 넘으면 열림 상태가 다음 줄로 이월된다(마커는 어절에 붙으므로 어절 단위
    wrap 이 마커 토큰 자체를 쪼개는 일은 없다).

    반환: (마커 제거된 줄 목록, {visual_idx: [(조각, 강조여부), …]}) —
    강조 없는 줄은 dict 에 없고 텍스트도 원본 그대로다(회귀 0)."""
    out_lines: list[tuple[int, str]] = []
    hl: dict[int, list[tuple[str, bool]]] = {}
    open_hl = False
    for vi, (oi, line) in enumerate(lines):
        if "{{" not in line and "}}" not in line and not open_hl:
            out_lines.append((oi, line))
            continue
        segs: list[tuple[str, bool]] = []
        buf = ""
        for tok in _TITLE_HL_TOKEN_RE.split(line):
            if tok in ("{{", "}}"):
                if buf:
                    segs.append((buf, open_hl))
                    buf = ""
                open_hl = tok == "{{"
            else:
                buf += tok
        if buf:
            segs.append((buf, open_hl))
        clean = "".join(t for t, _ in segs)
        out_lines.append((oi, clean))
        if clean.strip() and any(h for _, h in segs):
            hl[vi] = segs
    return out_lines, hl


def _build_filtergraph(inputs: RenderInputs, num_clip_inputs: int, num_cue_inputs: int) -> str:
    W = inputs.canvas_width
    H = inputs.canvas_height
    d = inputs.design

    ratio = getattr(d, 'aspect_ratio', '16:9')

    # E7: 계약 범위를 렌더 경계에서도 지킨다 — CLI 를 안 거친 호출(파이프라인 재개·테스트)이
    # 범위 밖 값을 들고 오면 조용히 이상한 영상을 만드는 대신 즉시 실패한다(제1원칙).
    speed = float(getattr(d, 'video_speed', 1.0) or 1.0)
    if not (0.8 <= speed <= 2.0):
        raise ValueError(f"video_speed 범위 밖: {speed} (0.8~2.0)")
    title_rotate = float(getattr(d, 'title_rotate', 0.0) or 0.0)
    if not (-180.0 <= title_rotate <= 180.0):
        raise ValueError(f"title_rotate 범위 밖: {title_rotate} (-180~180)")
    # E10: 영상 밴드 가로 크기(캔버스 px). None = 미지정 = 꽉 찬 폭(종전 필터그래프와
    # 바이트 동일). 명시 시(1080 포함) 자막·TTS margin 이 밴드 앵커로 전환되는 것은
    # pipeline 쪽 몫이고, 렌더 기하는 명시 1080 과 미지정이 동일하다. 비숫자·범위 밖은
    # 같은 원칙으로 즉시 실패 — int(str(...)) 라 800.5 같은 소수도 조용히 절단되지 않는다.
    _vw_raw = getattr(d, 'video_width', None)
    try:
        video_width = W if _vw_raw is None else int(str(_vw_raw))
    except ValueError:
        raise ValueError(f"video_width 가 정수가 아닙니다: {_vw_raw!r}") from None
    if not (320 <= video_width <= 1080):
        raise ValueError(f"video_width 범위 밖: {video_width} (320~1080)")

    # [1] 제목 줄바꿈 로직 — 라운드 7-A: orig_line_idx 보존하여 색상/폰트 매핑 정확히
    def split_text_smart(text: str, max_chars: int = 14) -> list[tuple[int, str]]:
        """입력 text를 \\n 구분으로 원본 라인 분리 후 각 라인을 max_chars 어절 경계로 wrap.

        반환: [(orig_line_idx, wrapped_text), ...]
        예: split_text_smart("긴line1\\n짧line2", 14) →
            [(0, "긴line1_part1"), (0, "긴line1_part2"), (1, "짧line2")]
        호출부는 orig_idx로 custom_colors/title_sizes를 lookup해야 wrap이 일어나도
        line1 모든 줄이 line1 색상, line2 모든 줄이 line2 색상으로 정확히 렌더된다.
        """
        if not text: return []
        lines = text.split('\n')
        res_lines: list[tuple[int, str]] = []
        for orig_idx, line in enumerate(lines):
            words = line.split()
            current_line = ""
            for word in words:
                # E19-2: {{강조}} 마커는 글자 수에 안 센다 — 마커 때문에 줄바꿈·축소가
                # 달라지면 안 된다(마커 없는 어절은 종전과 동일).
                clean_word = (word.split(':')[0].replace('{', '').replace('}', '')
                              if ':' in word else word.replace('{{', '').replace('}}', ''))
                if len(current_line) + len(clean_word) <= max_chars:
                    current_line = (current_line + " " + word).strip()
                else:
                    if current_line: res_lines.append((orig_idx, current_line))
                    current_line = word
            if current_line: res_lines.append((orig_idx, current_line))
        return res_lines


    # 라운드 22: 최대 20자까지 한 줄 유지 (이전 15자). 20자 초과는 pipeline에서 LLM 재작성 또는 절단.
    # E19-2: {{어절}} 강조 마크업 — 잔여물을 먼저 떼고(경고), wrap 뒤 세그먼트를 추출한다.
    # 마크업 없는 제목은 세 단계 전부 no-op 라 종전과 바이트 동일하다(회귀 0).
    _hl_warnings: list[str] = []
    _clean_title_text, _w = strip_title_markup(inputs.title_text)
    _hl_warnings += _w
    title_lines = split_text_smart(_clean_title_text, 20)
    title_lines, _title_hl = extract_title_highlights(title_lines)

    # E8: 시간대별 제목 — 세그먼트가 있으면 그 창들만 그린다(창 밖 시간은 제목 없음이
    # 유효값, title_text 는 무시). 창은 자막과 같은 편집본 시간축으로 들어오고, 제목은
    # setpts 뒤에 얹히므로 enable 창만 출력 시각(×1/S)으로 나눈다 — 이미지 오버레이
    # ([6.5])와 같은 규약. CLI·오버라이드 검증을 안 거친 호출(파이프라인 재개·테스트)이
    # 깨진 창을 들고 오면 즉시 실패한다(E7 과 같은 렌더 경계 검증).
    _tsegs = list(inputs.title_segments or [])
    _tseg_hl: list[dict[int, list[tuple[str, bool]]]] = []
    if _tsegs:
        validate_title_segments(_tsegs)
        _tsegs.sort(key=lambda sg: (float(sg["start_sec"]), float(sg["end_sec"])))
        _tseg_lines = []
        for sg in _tsegs:
            _txt, _w = strip_title_markup(str(sg["text"]))     # E19-2 — 제목과 같은 문법
            _hl_warnings += _w
            _ls, _hl = extract_title_highlights(split_text_smart(_txt, 20))
            _tseg_lines.append(_ls)
            _tseg_hl.append(_hl)
    else:
        _tseg_lines = []
    for _hw in _hl_warnings:
        # 잔여물은 조용히 못 넘긴다 — 제목이 깨진 채 발행되는 것이 최악(발주서 §2).
        print(f"  [TitleHighlight] ⚠ {_hw}")

    # 줄별 폰트 크기 (title_sizes가 있으면 사용, 없으면 title_size로 통일)
    title_sizes = getattr(d, 'title_sizes', [d.title_size])

    # 줄별 배경 박스·굵게(2026-08-21) — 인덱스는 원본 줄(orig_idx). 리스트가 짧으면 마지막
    # 값을 잇는다(title_colors 규약). 여백·라운드·획은 **그 줄의 최종 글자 크기** 비례
    # 고정값이라 길이 축소(_TITLE_LENGTH_SCALE)와 같이 줄어든다. 범위 밖 값은 즉시 실패.
    # 굵게 획 0.025em(70·90px → 2px): Jalnan 은 원래 극굵 글꼴이라 3px 부터 글자 속이 메워진다
    # (2026-08-21 실측 bold.png) — 가는 글꼴(mulmaru·Griun)에서 효과가 더 크다.
    title_boxes = list(getattr(d, 'title_boxes', None) or ["none"])
    title_box_colors = list(getattr(d, 'title_box_colors', None) or ["#000000"])
    title_bolds = list(getattr(d, 'title_bolds', None) or [False])
    for _bs in title_boxes:
        if _bs not in _TITLE_BOX_STYLES:
            raise ValueError(f"title_box 값 '{_bs}' 은 none·round·rect 중 하나여야 합니다")

    def _per_line(lst: list, orig_idx: int):
        return lst[orig_idx] if orig_idx < len(lst) else lst[-1]

    def _box_pad(font_size: int, box_style: str) -> int:
        return int(round(0.30 * font_size)) if box_style != "none" else 0

    def _bold_w(font_size: int, bold: bool) -> int:
        return max(1, int(round(0.025 * font_size))) if bold else 0

    # [2] 비디오 레이아웃 설정 — E10: 밴드 폭 = video_width(미지정 = 캔버스 꽉 참).
    # 화면비는 밴드 직사각형의 모양을, video_width 는 크기를 정한다(편집실 미리보기와 합의된 계약).
    scaled_w = video_width

    line_spacing = 30

    def _title_block_height(lines: list[tuple[int, str]]) -> int:
        # 박스 줄은 위아래 여백(pad)이 줄 높이에 들어간다 — 동적 배치가 **박스 기준**으로
        # 영상 위 20px 을 지키고, 이웃 줄의 박스끼리 겹치지 않는다(박스 없으면 종전과 동일).
        return sum(
            _per_line(title_sizes, orig_idx)
            + 2 * _box_pad(_per_line(title_sizes, orig_idx), _per_line(title_boxes, orig_idx))
            for orig_idx, _ in lines
        ) + max(0, len(lines) - 1) * line_spacing

    # E8: 자동 배치의 기준 y 는 전 세그먼트 공통 한 벌이어야 한다(세그먼트마다 제목이
    # 위아래로 튀면 안 된다) — **최대 블록 높이(=최대 줄 수) 기준**으로 잡으면 가장 큰
    # 세그먼트도 영상과 안 겹치고, 모든 세그먼트의 첫 줄이 같은 y 에서 시작한다.
    title_total_height = (max(_title_block_height(ls) for ls in _tseg_lines)
                          if _tseg_lines else _title_block_height(title_lines))

    try:
        r_w, r_h = map(int, ratio.split(':'))
        scaled_h = int(scaled_w * r_h / r_w)   # E10: 밴드 **폭** 기준 — 캔버스(W) 기준이 아니다
    except:
        scaled_h = scaled_w

    # 영상 세로 위치 — video_y 지정 시 그 위치(위로 올려 아래 밴드를 넓히는 템플릿용),
    # 미지정이면 종전대로 세로 중앙. 캔버스를 벗어나지 않게 클램프.
    _video_y = getattr(d, 'video_y', None)
    if _video_y is not None:
        overlay_y = min(max(0, int(_video_y)), max(0, H - scaled_h))
    else:
        overlay_y = (H - scaled_h) // 2

    # FFmpeg 짝수 보정
    scaled_w -= scaled_w % 2
    scaled_h -= scaled_h % 2
    overlay_y = max(0, overlay_y)
    # E10: 밴드가 캔버스보다 좁으면 가로 중앙(가로 위치 지정은 범위 밖 — 항상 중앙)
    pad_x = (W - scaled_w) // 2

    filters: list[str] = []

    # 프레임 고정에 쓸 fps(없으면 종전 동작). 0·음수·비정상은 없는 것으로 본다.
    try:
        _clip_fps = float(getattr(inputs, "source_fps", None) or 0) or None
    except (TypeError, ValueError):
        _clip_fps = None
    if _clip_fps is not None and not (1.0 < _clip_fps < 1000.0):
        _clip_fps = None

    # [3] 클립별 스케일 및 패딩 (여백 및 자막 위치 수정)
    for i, clip in enumerate(inputs.clips):
        crop_key = f"{clip.role}_{i}"
        crop_data_path = inputs.crop_timeline_map.get(crop_key)
        crop_filter = ""
        if crop_data_path and crop_data_path.exists():
            try:
                crop_json = json.loads(crop_data_path.read_text(encoding="utf-8"))
                if crop_json and len(crop_json) > 0:
                    cw, ch = crop_json[0]['crop_w'], crop_json[0]['crop_h']
                    x_expr = _build_crop_expr(crop_json, 'x_center')
                    y_expr = _build_crop_expr(crop_json, 'y_center')
                    # ffmpeg crop x/y는 좌상단 좌표 → center에서 cw/2, ch/2 빼기
                    crop_filter = f"crop={cw}:{ch}:x='({x_expr})-{cw}/2':y='({y_expr})-{ch}/2',"
            except: pass

        # 프레임 고정 — 이 클립이 정확히 N 프레임(= N/fps 초)으로 나가게 못 박는다.
        # 안 박으면 concat 이 영상 길이를 소리 길이에 맞추려 마지막 프레임을 복제해
        # 세그먼트가 제멋대로 길어지고, 그 오차가 조각마다 누적된다(2026-09-03 실측:
        # 13조각에 13프레임). tpad 는 컷이 소스 끝에 걸려 프레임이 모자랄 때만 마지막
        # 프레임을 복제해 N 을 채운다 — 남으면 trim 이 잘라내므로 평소엔 무해하다.
        # 소리도 같은 길이로 맞춰야(apad→atrim) concat 이 영상을 덧대지 않는다.
        pin_v, pin_a = "", "anull"
        _hold = float(getattr(clip, "hold_sec", 0.0) or 0.0)   # 정보 화면 붙잡기(2026-09-03)
        if _clip_fps:
            n_fr = max(1, round((float(clip.end_sec) - float(clip.start_sec) + _hold) * _clip_fps))
            dur_q = n_fr / _clip_fps
            pin_v = (f",tpad=stop_mode=clone:stop_duration={1 + _hold:.3f}"
                     f",trim=end_frame={n_fr},setpts=PTS-STARTPTS")
            pin_a = f"apad,atrim=end={dur_q:.6f},asetpts=PTS-STARTPTS"
        elif _hold > 0:
            _len = float(clip.end_sec) - float(clip.start_sec) + _hold
            pin_v = f",tpad=stop_mode=clone:stop_duration={_hold:.3f},setpts=PTS-STARTPTS"
            pin_a = f"apad,atrim=end={_len:.6f},asetpts=PTS-STARTPTS"

        v_filter = (
            f"[{i}:v]{crop_filter}"
            f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase,"
            f"setsar=1,"
            f"crop={scaled_w}:{scaled_h},"
            f"pad={W}:{H}:{pad_x}:{overlay_y}:color=#0D0011{pin_v}[v{i}]"
        )
        filters.append(v_filter)
        filters.append(f"[{i}:a]{pin_a}[a{i}]")

    # [4] 연결(Concat)
    concat_inputs = []
    for i in range(num_clip_inputs):
        concat_inputs.append(f"[v{i}]")
        concat_inputs.append(f"[a{i}]")
    filters.append(f"{''.join(concat_inputs)}concat=n={num_clip_inputs}:v=1:a=1[vcat][acat]")

    # [4.5] 영상 배속(E7-2, 구현 지점 A) — concat **직후** 딱 한 번 setpts.
    # 얼굴 추종 crop 은 [3]의 클립별 체인(= setpts 앞)에서 원본 t 로 계산이 끝났으므로
    # 시간축이 어긋나지 않는다. 이 지점 **이후**의 모든 시간 좌표(ASS 이벤트·이미지
    # enable 창·adelay·덕킹 창)는 출력 시각(×1/S)이어야 한다 — ASS 는 파이프라인이
    # 이벤트 시각을 ×1/S 로 써서 넘기고, 나머지는 이 파일 안에서 나눈다.
    # 오디오 배속(atempo, 원본·현장음만)은 _build_audio_filter 가 같은 규약으로 담당.
    base_after_concat = "[vcat]"
    if speed != 1.0:
        filters.append(f"[vcat]setpts=PTS/{speed:g}[vspd]")
        base_after_concat = "[vspd]"


    # 폰트 경로
    current_file_path = Path(__file__).resolve()
    project_root = current_file_path.parent.parent  # /app 폴더 위치
    font_folder = project_root / "assets" / "fonts"
    
    # [5] 제목(Title) 필터
    # font_arg = str(d.title_font).replace("\\", "/").replace(":", "\\:")
    # custom_colors = getattr(d, 'title_colors', [DesignConfig.title_color])
    # last_v_label = "[vcat]"
    # for idx, raw_line in enumerate(title_lines):
    #     base_color = custom_colors[idx] if idx < len(custom_colors) else custom_colors[-1]
    #     y_pos = d.title_y + (idx * (d.title_size + line_spacing))
    #     escaped_full = _escape_text_for_drawtext(raw_line)
    #     next_label = f"[title_{idx}]"
    #     filters.append(f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_full}':fontcolor={base_color}:fontsize={d.title_size}:x=(w-text_w)/2:y={y_pos}{next_label}")
    #     last_v_label = next_label
    # [5] 제목(Title) 필터 수정본
    # actual_font = str(d.title_font)
    
    # # 1. 경로인지 일반 폰트명인지 구분 (get_font_path의 결과에 따라)
    # if "/" in actual_font or "\\" in actual_font:
    #     # 파일 경로인 경우: 콜론(:) 이스케이프 후 fontfile 사용
    #     font_arg = actual_font.replace(":", "\\:")
    #     font_param = f"fontfile='{font_arg}'"
    # else:
    #     # 시스템 폰트명인 경우 (예: Malgun Gothic): font 사용
    #     font_param = f"font='{actual_font}'"

    # # 기본 색상 설정 (DesignConfig 클래스 상수가 아닌 인스턴스 d의 값을 참조하도록 수정)
    # custom_colors = getattr(d, 'title_colors', [d.title_color])
    
    # last_v_label = "[vcat]"
    # for idx, raw_line in enumerate(title_lines):
    #     # 색상 선택 (라인별 색상이 지정되어 있으면 사용, 없으면 마지막 색상 사용)
    #     base_color = custom_colors[idx] if idx < len(custom_colors) else custom_colors[-1]
        
    #     # y축 위치 계산 (라인별 높이 + 간격)
    #     y_pos = d.title_y + (idx * (d.title_size + line_spacing))
        
    #     # 텍스트 이스케이프 (따옴표, 콜론 등 특수문자 처리)
    #     escaped_full = _escape_text_for_drawtext(raw_line)
        
    #     next_label = f"[title_{idx}]"
        
    #     # [핵심 수정] fontfile= 대신 위에서 생성한 {font_param}을 통째로 삽입
    #     filters.append(
    #         f"{last_v_label}drawtext={font_param}:text='{escaped_full}':"
    #         f"fontcolor={base_color}:fontsize={d.title_size}:"
    #         f"x=(w-text_w)/2:y={y_pos}{next_label}"
    #     )
    #     last_v_label = next_label
    # [5] 제목(Title) 필터 
    actual_font = str(d.title_font)

    font_arg = actual_font
    last_v_label = base_after_concat
    custom_colors = getattr(d, 'title_colors', ["white"])
    if "/" in actual_font or "\\" in actual_font:
        # 유니코드 경로(한글 등)를 FFmpeg이 인식할 수 있도록 8.3 단축 경로로 변환
        short_path = _to_short_path(actual_font.replace("/", "\\"))
        clean_path = short_path.replace("\\", "/").replace(":", "\\:")
        font_arg = clean_path

    # 제목 위치: 영상 영역 시작점 바로 위에 동적 배치 (사용자 요구).
    # 영상 위쪽 여백(=overlay_y)이 충분히 크면 그 안에 제목 + 20px gap을 두고 배치.
    # 여백이 부족하면(=영상이 캔버스 위쪽까지 차지) d.title_y 폴백.
    # title_y_fixed(F-409)면 동적 배치를 끄고 title_y 를 그대로 쓴다 — 편집실 제목
    # 드래그가 보내는 좌표는 절대 위치라, 동적 배치가 이기면 드래그가 반영되지 않는다.
    _gap_above_video = 20
    _dynamic_title_top = overlay_y - title_total_height - _gap_above_video
    if getattr(d, 'title_y_fixed', False):
        cumulative_y = d.title_y
    elif _dynamic_title_top >= 10:
        cumulative_y = _dynamic_title_top
    else:
        cumulative_y = d.title_y
    # 라운드 7-A: title_lines가 (orig_line_idx, text) 튜플 리스트.
    # 색상·폰트는 orig_idx로 lookup → wrap이 일어나도 line1 모든 줄은 line1 색/폰트, line2도 동일.
    # 13자까지 풀사이즈, 14~20자 명시적 lookup으로 더 강하게 축소.
    # line2 base=90px 기준 캔버스(1080-120 padding=960px) 가로 잘림 방지를 위해
    # 종전 sqrt 곡선보다 한 단계씩 낮춘 값으로 재조정.
    # 21자+ pipeline에서 LLM 재작성 또는 절단되므로 20자에서 cap.
    _TITLE_LENGTH_SCALE = {
        14: 0.90,
        15: 0.83,
        16: 0.77,
        17: 0.72,
        18: 0.67,
        19: 0.63,
        20: 0.60,
    }

    def _scale_font_for_length(base_size: int, char_count: int) -> int:
        if char_count <= 13:
            return base_size
        cc = min(char_count, 20)
        scale = _TITLE_LENGTH_SCALE[cc]
        return max(1, int(round(base_size * scale)))

    # 줄별 스펙(색·크기·프레임 y)을 먼저 확정 — 회전 유무와 무관하게 좌표 규약은 동일하다.
    # E8: 기준선(_title_block_top)은 전 세그먼트 공통 한 벌 — 세그먼트마다 줄 수가
    # 달라도 모든 세그먼트의 첫 줄이 같은 y 에서 시작한다(위 title_total_height 참고).
    _title_block_top = cumulative_y

    def _line_specs(lines: list[tuple[int, str]],
                    hl_map: dict[int, list[tuple[str, bool]]] | None = None,
                    ) -> list[tuple[str, int, int, str, dict]]:
        # (color, font_size, frame_y, escaped, style) — frame_y 는 **글자** 윗변(박스 윗변은
        # frame_y - pad). style: box(none|round|rect)·box_color·pad·bold_w·raw(원문, PNG 폭 측정용)
        # ·hl_segments(E19-2 — 강조 줄만, [(조각, 강조여부), …])
        specs: list[tuple[str, int, int, str, dict]] = []
        y = _title_block_top
        for visual_idx, (orig_idx, raw_line) in enumerate(lines):
            base_color = custom_colors[orig_idx] if orig_idx < len(custom_colors) else custom_colors[-1]
            base_font_size = title_sizes[orig_idx] if orig_idx < len(title_sizes) else title_sizes[-1]
            font_size = _scale_font_for_length(base_font_size, len(raw_line))
            box_style = _per_line(title_boxes, orig_idx)
            pad = _box_pad(font_size, box_style)
            style = {
                "box": box_style,
                "box_color": _per_line(title_box_colors, orig_idx),
                "pad": pad,
                "bold_w": _bold_w(font_size, bool(_per_line(title_bolds, orig_idx))),
                "raw": raw_line,
                "hl_segments": (hl_map or {}).get(visual_idx),
            }
            specs.append((base_color, font_size, y + pad, _escape_text_for_drawtext(raw_line), style))
            y += font_size + 2 * pad + line_spacing
        return specs

    def _drawtext_extra(base_color: str, style: dict) -> str:
        # 각진 박스는 drawtext 내장 box(글자 폭에 딱 맞게 ffmpeg 가 직접 잰다), 굵게는 같은 색
        # 외곽선. 단일값 boxborderw 만 쓴다(4값형은 ffmpeg 버전에 따라 없다 — 함대 호환).
        extra = ""
        if style["box"] == "rect":
            extra += f":box=1:boxcolor={style['box_color']}:boxborderw={style['pad']}"
        if style["bold_w"]:
            extra += f":borderw={style['bold_w']}:bordercolor={base_color}"
        return extra

    def _emit_round_boxes(specs, in_label: str, prefix: str, y_offset: int,
                          enable_clause: str = "") -> str:
        # 둥근 박스는 PNG overlay — **모든 줄의 박스를 먼저** 깔고 글자는 그 뒤에 그린다.
        # 그래야 어떤 줄의 박스도 다른 줄 글자를 덮지 못한다. y_offset 은 회전 캔버스
        # 좌표계(캔버스 윗변 = 0)로 옮길 때 쓴다. PNG 는 출력 파일 옆에 남긴다(디버그 자료).
        label = in_label
        for visual_idx, (_c, font_size, frame_y, _e, style) in enumerate(specs):
            if style["box"] != "round":
                continue
            png_path = inputs.output_path.parent / f"title_box_{prefix}{visual_idx}.png"
            png, box_w, box_h = _make_title_box_png(
                style["raw"], actual_font, font_size, style["box_color"], style["pad"],
                int(round(0.25 * font_size)), style["bold_w"], png_path)
            bx = int(round((W - box_w) / 2.0))
            by = frame_y - style["pad"] - y_offset
            src = str(png).replace("\\", "/").replace(":", "\\:")
            print(f"  [TitleBox] 줄{visual_idx} round {box_w}x{box_h} @ ({bx},{by}) {style['box_color']}")
            filters.append(
                f"movie='{src}'[{prefix}bx{visual_idx}];"
                f"{label}[{prefix}bx{visual_idx}]overlay={bx}:{by}{enable_clause}[{prefix}bo{visual_idx}]"
            )
            label = f"[{prefix}bo{visual_idx}]"
        return label

    _title_specs = _line_specs(title_lines, _title_hl)

    def _emit_line_text(in_label: str, out_label: str, sub: str, base_color: str,
                        font_size: int, y_val: int, escaped_full: str, style: dict,
                        enable_clause: str) -> None:
        """한 줄의 글자 그리기 — 강조가 없으면 종전과 **문자열까지 동일한** drawtext 한 개
        (회귀 0, E10 문자열 가드가 지킨다). 강조 줄(E19-2)은 세그먼트별 drawtext 를 절대
        x 로 나란히 놓는다 — 폭은 같은 TTF 를 Pillow 로 잰다(둥근 박스 PNG 와 같은 신뢰,
        ffmpeg 와의 수 px 차이는 세그먼트 경계에서만 나고 실렌더 확인 대상)."""
        segs = style.get("hl_segments")
        if not segs:
            filters.append(
                f"{in_label}drawtext=expansion=none:fontfile='{font_arg}':text='{escaped_full}':"
                f"fontcolor={base_color}:fontsize={font_size}:"
                f"x=(w-text_w)/2:y={y_val}{_drawtext_extra(base_color, style)}{enable_clause}{out_label}"
            )
            return
        hl_color = getattr(d, "title_highlight_color", None) or "#FFE24A"
        widths = [_measure_title_text_width(t, actual_font, font_size) for t, _ in segs]
        x0 = int(round((W - sum(widths)) / 2.0))
        label = in_label
        if style["box"] == "rect":
            # 세그먼트별 box 는 조각난 상자가 된다 — 상자색 글자+box 밑그림 한 장을 먼저
            # 깔아 상자만 보이게 하고(ffmpeg 가 전체 폭을 직접 잰다), 글자는 그 위에 그린다.
            filters.append(
                f"{label}drawtext=expansion=none:fontfile='{font_arg}':text='{escaped_full}':"
                f"fontcolor={style['box_color']}:fontsize={font_size}:"
                f"x=(w-text_w)/2:y={y_val}:box=1:boxcolor={style['box_color']}:"
                f"boxborderw={style['pad']}{enable_clause}[{sub}hb]"
            )
            label = f"[{sub}hb]"
        print(f"  [TitleHighlight] {len(segs)}세그먼트 x0={x0} "
              f"({'/'.join(t for t, h in segs if h)!r} → {hl_color})")
        off = 0
        for k, ((seg_text, is_hl), seg_w) in enumerate(zip(segs, widths)):
            nl = out_label if k == len(segs) - 1 else f"[{sub}h{k}]"
            col = hl_color if is_hl else base_color
            extra = (f":borderw={style['bold_w']}:bordercolor={col}"
                     if style["bold_w"] else "")
            filters.append(
                f"{label}drawtext=expansion=none:fontfile='{font_arg}':"
                f"text='{_escape_text_for_drawtext(seg_text)}':"
                f"fontcolor={col}:fontsize={font_size}:x={x0 + off}:y={y_val}"
                f"{extra}{enable_clause}{nl}"
            )
            label = nl
            off += seg_w

    def _emit_title_lines(specs: list[tuple[str, int, int, str]], in_label: str,
                          prefix: str, enable_clause: str = "") -> str:
        """줄별 drawtext 를 메인 체인에 직결 — 종전(무회전) 제목 경로."""
        label = _emit_round_boxes(specs, in_label, prefix, 0, enable_clause)
        for visual_idx, (base_color, font_size, frame_y, escaped_full, style) in enumerate(specs):
            next_label = f"[{prefix}{visual_idx}]"
            _emit_line_text(label, next_label, f"{prefix}{visual_idx}", base_color,
                            font_size, frame_y, escaped_full, style, enable_clause)
            label = next_label
        return label

    def _emit_rotated_title(specs: list[tuple[str, int, int, str]], in_label: str,
                            prefix: str, out_label: str, enable_clause: str = "") -> str:
        """E7-1: drawtext 는 회전이 없다 — 제목 줄 묶음 전체를 투명 캔버스에 그린 뒤
        rotate(시계방향 양수 = ffmpeg rotate 와 동일, F-410 실측) → overlay.
        원점 = 텍스트 블록 중심. 줄들은 x 로 중앙 정렬이라 캔버스(W×블록높이)의 중심이
        곧 블록 중심이다. 위아래 패딩은 대칭이라 중심을 움직이지 않으면서 글리프
        디센더가 캔버스 모서리에 잘리는 것을 막는다. 바운딩 박스·중심 고정 되물림은
        images[].rotate(F-410)와 같은 계산이다. E8 세그먼트는 세그먼트별 캔버스로
        같은 창(enable_clause)을 overlay 에 단다 — 캔버스 높이는 그 세그먼트의 블록
        높이라 회전 원점(블록 중심)도 세그먼트별로 제 자리다."""
        # 박스 줄은 위아래 pad 만큼 블록이 커진다(_line_specs 의 y 전진과 같은 값) — 박스는
        # 캔버스 안에서 회전 **전에** 깔리므로 글자와 같이 돈다.
        _blk_pad = (max(fs for _, fs, _, _, _ in specs) + 1) // 2
        _blk_h = (sum(fs + 2 * st["pad"] for _, fs, _, _, st in specs)
                  + max(0, len(specs) - 1) * line_spacing + 2 * _blk_pad)
        _blk_h += _blk_h % 2
        _canvas_top = _title_block_top - _blk_pad
        _rad = math.radians(title_rotate)
        _bb_w = int(math.ceil(abs(W * math.cos(_rad)) + abs(_blk_h * math.sin(_rad)) - 1e-6))
        _bb_h = int(math.ceil(abs(W * math.sin(_rad)) + abs(_blk_h * math.cos(_rad)) - 1e-6))
        _ox = int(round(W / 2.0 - _bb_w / 2.0))
        _oy = int(round(_canvas_top + _blk_h / 2.0 - _bb_h / 2.0))
        _ttl_label = f"[{prefix}0]"
        filters.append(f"color=c=black@0.0:s={W}x{_blk_h}:d=1,format=rgba{_ttl_label}")
        _ttl_label = _emit_round_boxes(specs, _ttl_label, prefix, _canvas_top)
        for visual_idx, (base_color, font_size, frame_y, escaped_full, style) in enumerate(specs):
            next_label = f"[{prefix}{visual_idx + 1}]"
            _emit_line_text(_ttl_label, next_label, f"{prefix}r{visual_idx}", base_color,
                            font_size, frame_y - _canvas_top, escaped_full, style, "")
            _ttl_label = next_label
        print(f"  [TitleRotate] {title_rotate:g}° — 블록 {W}x{_blk_h} → bb {_bb_w}x{_bb_h}, "
              f"overlay=({_ox},{_oy})")
        filters.append(f"{_ttl_label}rotate={_rad:.10f}:ow={_bb_w}:oh={_bb_h}:c=black@0[{prefix}rot]")
        filters.append(f"{in_label}[{prefix}rot]overlay={_ox}:{_oy}{enable_clause}{out_label}")
        return out_label

    if _tsegs:
        # E8: 세그먼트별로 같은 기준선에서 스펙을 만들고 창(enable)만 단다 — 겹침은
        # 검증에서 거절됐으므로 어느 시점이든 최대 한 세그먼트만 그려진다. 회전이
        # 있으면 세그먼트별 캔버스 → rotate → overlay 의 enable 파라미터로 같은 창.
        for k, (sg, seg_lines) in enumerate(zip(_tsegs, _tseg_lines)):
            s_out = float(sg["start_sec"]) / speed
            e_out = float(sg["end_sec"]) / speed
            enable = f":enable='between(t,{s_out:.3f},{e_out:.3f})'"
            specs = _line_specs(seg_lines, _tseg_hl[k])
            print(f"  [TitleSegment {k}] {s_out:.3f}~{e_out:.3f}s ({len(specs)}줄)"
                  + (f" — 편집본 {float(sg['start_sec']):g}~{float(sg['end_sec']):g}s"
                     f" ×1/{speed:g}" if speed != 1.0 else ""))
            if title_rotate:
                last_v_label = _emit_rotated_title(
                    specs, last_v_label, f"tsg{k}_", f"[tsg{k}out]", enable)
            else:
                last_v_label = _emit_title_lines(specs, last_v_label, f"tseg{k}_", enable)
    elif _title_specs and title_rotate:
        last_v_label = _emit_rotated_title(_title_specs, last_v_label, "ttl", "[with_title]")
    else:
        last_v_label = _emit_title_lines(_title_specs, last_v_label, "title_")


    # [5.5] 플랫폼 표기 — 권리사 '영상 내 플랫폼 노출' 요구(티빙·Wavve·쿠팡플레이 등).
    # 위치는 캔버스가 아니라 **영상영역 왼쪽 상단**(overlay_y) 기준 오프셋 — aspect_ratio 를
    # 바꿔도 표기가 영상을 따라간다. 이미지·텍스트 중 이미지 우선(둘 다 준 경우).
    _pf_img = getattr(d, 'platform_image', None)
    _pf_txt = getattr(d, 'platform_text', None)
    if _pf_img or _pf_txt:
        pf_off = getattr(d, 'platform_x', 24)          # 앵커 쪽 모서리에서의 오프셋
        pf_right = getattr(d, 'platform_align', 'left') == "right"
        # E10: 앵커는 캔버스가 아니라 **밴드 모서리** — video_width 로 밴드가 좁아져도
        # 표기가 영상 위에 남는다(위 '영상영역 기준' 계약과 동일). right 앵커의 오프셋은
        # 캔버스 오른쪽 가장자리 기준으로 환산해 둔다(밴드 오른쪽까지의 여백 + platform_x).
        pf_x = pad_x + pf_off                           # left 기본 — right 는 아래서 재계산
        _pf_right_margin = (W - (pad_x + scaled_w)) + pf_off
        pf_y = overlay_y + getattr(d, 'platform_y', 24)
        if _pf_img:
            _pf_path = Path(_pf_img).resolve()
            _bw = getattr(d, 'platform_image_width', 150)
            _bh = getattr(d, 'platform_image_height', None) or _bw
            # 작품 로고와 같은 이유로 실측한다 — 비율은 파일마다 제각각이라 가정이 성립하지 않는다.
            try:
                _pw, _ph = _probe_video_dims(_pf_path)
            except Exception as e:
                print(f"  [Platform] 크기 측정 실패({e}) — 너비 {_bw} 고정, 높이는 자동")
                _pw = _ph = 0
            if _pw > 0 and _ph > 0:
                _s = min(_bw / _pw, _bh / _ph)
                pf_w, pf_h = max(2, int(_pw * _s) // 2 * 2), max(2, int(_ph * _s) // 2 * 2)
            else:
                pf_w, pf_h = _bw, -1   # 높이 자동(-1) 폴백
            if pf_right:
                # 이미지 폭을 알면 밴드 오른쪽 가장자리 기준 고정 좌표.
                # 폭 미상(-1 폴백)이면 overlay 식으로 — 캔버스 오른쪽에서 환산 여백만큼 안쪽.
                pf_x = (f"W-w-{_pf_right_margin}" if pf_h == -1
                        else pad_x + scaled_w - pf_w - pf_off)
            _pf_str = str(_pf_path).replace("\\", "/").replace(":", "\\:")
            print(f"  [Platform] 로고 {_pw}x{_ph} → {pf_w}x{pf_h} @ ({pf_x},{pf_y})")
            filters.append(
                f"movie='{_pf_str}',scale={pf_w}:{pf_h}[pfm];"
                f"{last_v_label}[pfm]overlay={pf_x}:{pf_y}[with_pf]"
            )
        else:
            if pf_right:
                # drawtext 는 텍스트 폭을 자기 식으로 안다 — 캔버스 오른쪽에서 환산 여백만큼 안쪽
                pf_x = f"w-text_w-{_pf_right_margin}"
            _pf_esc = _escape_text_for_drawtext(str(_pf_txt))
            print(f"  [Platform] 텍스트 '{_pf_txt}' @ ({pf_x},{pf_y})")
            filters.append(
                f"{last_v_label}drawtext=expansion=none:fontfile='{font_arg}':text='{_pf_esc}':"
                f"fontcolor={getattr(d, 'platform_color', 'white')}:"
                f"fontsize={getattr(d, 'platform_font_size', 40)}:"
                f"x={pf_x}:y={pf_y}[with_pf]"
            )
        last_v_label = "[with_pf]"

    # [6] 작품명(Logo)
    work_label = "[with_work]"
    work_type = getattr(d, 'work_type', 'text')
    work_value = getattr(d, 'work_value', inputs.work_title)

    # 비디오 영역(overlay_y ~ overlay_y+scaled_h) 과 겹치지 않도록 클램프.
    # - 비디오 하단 + 20px 여백 아래로 자동 푸시
    # - 사용자가 더 아래(큰 y)를 명시했으면 존중
    # - 캔버스 하단을 벗어나면 (H - 추정 로고높이 - 여백) 으로 끌어올림
    _gap_below_video = 20
    _safe_work_top = overlay_y + scaled_h + _gap_below_video
    work_y_final = max(d.work_title_y, _safe_work_top)
    if work_type == "image" and work_value:
        logo_w = getattr(d, 'work_image_width', 350)
        logo_box_h = getattr(d, 'work_image_height', None)
        _logo_path = Path(work_value).resolve()
        # 라운드 24: 로고 높이를 추측하지 않고 실측한다.
        # 종전엔 scale={w}:-1 로 높이를 자동에 맡기고 클램프는 w/2 로 가정했는데, 세로형 로고
        # (도깨비 159x308)에서 실제 높이가 가정의 3.9배라 클램프가 발동조차 안 하고 캔버스 밖으로
        # 잘려 나갔다. 원본 비율은 작품·권리사마다 제각각이라 가정이 성립하지 않는다.
        try:
            _nat_w, _nat_h = _probe_video_dims(_logo_path)
        except Exception as e:
            print(f"  [Logo] 크기 측정 실패({e}) — 너비 {logo_w} 고정, 높이는 자동")
            _nat_w = _nat_h = 0

        if _nat_w > 0 and _nat_h > 0:
            # 박스 안에 비율 유지로 맞춘다(contain). logo_box_h 미지정이면 종전처럼 너비만 구속.
            _box_h = logo_box_h if logo_box_h else int(_nat_h * (logo_w / _nat_w))
            _s = min(logo_w / _nat_w, _box_h / _nat_h)
            logo_w_final = max(2, int(_nat_w * _s) // 2 * 2)
            logo_h_final = max(2, int(_nat_h * _s) // 2 * 2)
        else:
            logo_w_final, logo_h_final = logo_w, max(60, int(logo_w * 0.5))

        # 정렬: top=영상 하단에 붙임(종전) · center=영상 하단~캔버스 하단 밴드의 세로 중앙.
        # center 는 로고 높이가 달라져도 균형이 유지돼 작품별로 y 를 다시 찾지 않아도 된다.
        if getattr(d, 'work_image_align', 'top') == "center":
            work_y_final = _safe_work_top + (H - 20 - _safe_work_top - logo_h_final) // 2
        if work_y_final + logo_h_final > H - 20:
            work_y_final = H - logo_h_final - 20
        work_y_final = max(_safe_work_top, work_y_final)

        logo_path_str = str(_logo_path).replace("\\", "/").replace(":", "\\:")
        print(f"  [Logo] {_nat_w}x{_nat_h} → {logo_w_final}x{logo_h_final} @ y={work_y_final}")
        filters.append(
            f"movie='{logo_path_str}',scale={logo_w_final}:{logo_h_final}[logo];"
            f"{last_v_label}[logo]overlay=(W-w)/2:{work_y_final}{work_label}"
        )
    else:
        raw_work = work_value if work_value else inputs.work_title
        # 레터스페이싱 적용: 글자 사이에 공백 삽입
        if getattr(d, 'work_letter_spacing', False):
            raw_work = " ".join(raw_work)
        _estimated_text_h = int(d.work_font_size * 1.4)
        if work_y_final + _estimated_text_h > H - 20:
            work_y_final = max(_safe_work_top, H - _estimated_text_h - 20)
        escaped_val = _escape_text_for_drawtext(raw_work)
        filters.append(f"{last_v_label}drawtext=expansion=none:fontfile='{font_arg}':text='{escaped_val}':fontcolor={d.work_color}:fontsize={d.work_font_size}:x=(w-text_w)/2:y={work_y_final}{work_label}")


    # [6.5] 편집실 이미지 오버레이(edit_overrides/v3 images, F-408)
    # 좌표는 1080×1920 캔버스 비율→px(w 만 지정, 세로는 원본비 = scale 높이 -2),
    # 시간 창은 편집본 시간축(enable=between) — 배치는 place_anchored_images 가 끝냈다.
    # layer ≤ 0 은 자막 아래(기본 0), ≥ 1 은 자막(ASS 두 겹) 위. 같은 그룹 안에서는
    # layer 오름차순으로 쌓이고, 동률이면 배열 순서(stable sort)로 뒤가 위에 온다.
    # rotate(F-410, 도 단위 시계방향 양수)는 원본비 스케일 후 이미지 **중심** 기준
    # 회전 — w 는 회전 전 원본 기준이고, 회전으로 커진 바운딩 박스는 중심이 안
    # 움직이게 overlay 좌표를 되물린다.
    _eimgs = sorted(list(inputs.image_overlays or []),
                    key=lambda im: int(im.get("layer") or 0))
    _eimgs_below = [im for im in _eimgs if int(im.get("layer") or 0) <= 0]
    _eimgs_above = [im for im in _eimgs if int(im.get("layer") or 0) > 0]

    def _overlay_images(imgs: list[dict], in_label: str, tag: str) -> str:
        label = in_label
        for j, im in enumerate(imgs):
            img_path = str(Path(str(im["file"])).resolve()).replace("\\", "/").replace(":", "\\:")
            img_w = max(2, int(round(float(im["w"]) * W)) // 2 * 2)
            img_x = int(round(float(im["x"]) * W))
            img_y = int(round(float(im["y"]) * H))
            # 창은 편집본(소스) 시간축으로 들어온다 — 오버레이는 setpts 뒤라 출력 시각(×1/S).
            s, e = float(im["start_sec"]) / speed, float(im["end_sec"]) / speed
            rot = float(im.get("rotate") or 0.0)
            if rot:
                # 회전은 스케일 높이를 미리 알아야 한다(-2 자동값으론 바운딩 박스
                # 계산 불가) — 원본 크기를 probe 해 짝수 px 로 직접 정한다.
                src_w, src_h = _probe_image_size(str(im["file"]))
                img_h = max(2, int(round(img_w * src_h / src_w / 2.0)) * 2)
                rad = math.radians(rot)          # ffmpeg rotate 도 시계방향 양수(라디안)
                # ceil 앞 1e-6 보정: cos(π/2) 류가 정확히 0 이 아니어서(6e-17) 90°
                # 같은 직각이 한 픽셀 커지는 것을 막는다.
                bb_w = int(math.ceil(abs(img_w * math.cos(rad)) + abs(img_h * math.sin(rad)) - 1e-6))
                bb_h = int(math.ceil(abs(img_w * math.sin(rad)) + abs(img_h * math.cos(rad)) - 1e-6))
                # x/y(회전 전 좌상단) → 중심 고정: 커진 박스의 절반만큼 되물린다
                ox = img_x + int(round((img_w - bb_w) / 2.0))
                oy = img_y + int(round((img_h - bb_h) / 2.0))
                # format=rgba 선행 — jpg(알파 없음)도 모서리 여백이 투명(c=black@0)이
                # 되게 한다. ow/oh 를 직접 주면 rotw/roth 반올림 차이가 안 생긴다.
                src_chain = (f"movie='{img_path}',scale={img_w}:{img_h},format=rgba,"
                             f"rotate={rad:.10f}:ow={bb_w}:oh={bb_h}:c=black@0")
            else:
                ox, oy = img_x, img_y
                src_chain = f"movie='{img_path}',scale={img_w}:-2"
            out = f"[{tag}{j}]"
            print(f"  [EditImage] {Path(str(im['file'])).name}: w={img_w}px @ ({img_x},{img_y}) "
                  f"{s:.2f}~{e:.2f}s layer={int(im.get('layer') or 0)}"
                  + (f" rotate={rot:g}° → overlay=({ox},{oy})" if rot else ""))
            filters.append(
                f"{src_chain}[{tag}src{j}];"
                f"{label}[{tag}src{j}]overlay={ox}:{oy}:"
                f"enable='between(t,{s:.3f},{e:.3f})'{out}"
            )
            label = out
        return label

    pre_sub_label = _overlay_images(_eimgs_below, work_label, "eimgb")

    # [7] 자막(ASS) 적용
    font_dir_fixed = _to_short_path(str(font_folder.resolve())).replace("\\", "/").replace(":", "\\:")

    if inputs.subtitle_path:
        default_font = DesignConfig.subtitle_font
        requested_font = d.subtitle_font

        ass_path = inputs.subtitle_path.resolve()
        ass_content = ass_path.read_text(encoding="utf-8")
        ass_content = ass_content.replace(default_font, requested_font)
        ass_path.write_text(ass_content, encoding="utf-8")

        sub_path_fixed = _to_short_path(str(ass_path)).replace("\\", "/").replace(":", "\\:")
        filters.append(f"{pre_sub_label}ass='{sub_path_fixed}':fontsdir='{font_dir_fixed}'[vsub]")
        last_v_label = "[vsub]"
    else:
        filters.append(f"{pre_sub_label}null[vsub]")
        last_v_label = "[vsub]"

    # 자막 위에 더 얹을 것(텍스트 레이어 F-411 · layer≥1 이미지)이 있으면 ASS 출력을
    # 중간 라벨로 받고 그 위에 얹은 뒤 [vout] 으로 마감
    _txt_path = getattr(inputs, "text_subtitle_path", None)
    _has_texts = bool(_txt_path and Path(_txt_path).exists())
    _tts_out = "[vpretxt]" if _has_texts else ("[vpretop]" if _eimgs_above else "[vout]")
    if inputs.tts_subtitle_path and inputs.tts_subtitle_path.exists():
        tts_ass_path = inputs.tts_subtitle_path.resolve()
        tts_sub_fixed = _to_short_path(str(tts_ass_path)).replace("\\", "/").replace(":", "\\:")
        filters.append(f"{last_v_label}ass='{tts_sub_fixed}':fontsdir='{font_dir_fixed}'{_tts_out}")
        last_v_label = _tts_out
    else:
        filters.append(f"{last_v_label}null{_tts_out}")
        last_v_label = _tts_out

    # [7.5] 자유 텍스트 레이어(F-411) — 대사·TTS 자막 위, layer≥1 이미지 아래
    if _has_texts:
        _txt_out = "[vpretop]" if _eimgs_above else "[vout]"
        txt_fixed = _to_short_path(str(Path(_txt_path).resolve())).replace("\\", "/").replace(":", "\\:")
        filters.append(f"{last_v_label}ass='{txt_fixed}':fontsdir='{font_dir_fixed}'{_txt_out}")
        last_v_label = _txt_out

    if _eimgs_above:
        _top_label = _overlay_images(_eimgs_above, last_v_label, "eimga")
        filters.append(f"{_top_label}null[vout]")
        last_v_label = "[vout]"

    audio_filter = _build_audio_filter(inputs, num_clip_inputs, num_cue_inputs)
    audio_filter = _apply_loudnorm(audio_filter, getattr(inputs, "loudness_target_lufs", None))
    filters.append(audio_filter)
    return ";".join(filters)

def _probe_image_size(path: str) -> tuple[int, int]:
    """이미지 원본 (width, height) 를 ffprobe 로 읽는다 — rotate(F-410) 바운딩 박스
    계산용. 파일 존재는 resolve_image_files 가 이미 보장했으므로 여기서 못 읽으면
    파일이 이미지가 아니라는 뜻 — 조용히 회전을 빼는 대신 즉시 실패한다(제1원칙)."""
    ffprobe = find_ffmpeg_command("ffprobe")
    out = subprocess.check_output([
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_streams", str(path)
    ])
    streams = json.loads(out).get("streams") or []
    for st in streams:
        w, h = st.get("width"), st.get("height")
        if w and h:
            return int(w), int(h)
    raise RuntimeError(f"이미지 크기를 읽지 못했습니다(회전 합성에 필요): {path}")


def _get_audio_duration(path: Path) -> float:
    """ffprobe로 오디오 파일 길이를 측정합니다."""
    ffprobe = find_ffmpeg_command("ffprobe")
    try:
        result = subprocess.check_output([
            ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(path)
        ], stderr=subprocess.DEVNULL)
        data = json.loads(result)
        for stream in data.get("streams", []):
            if "duration" in stream:
                return float(stream["duration"])
    except Exception:
        pass
    return 0.0

def _apply_loudnorm(audio_filter: str, target_lufs: float | None) -> str:
    """최종 오디오 라벨 [aout] 에 loudnorm 정규화 단계를 덧붙인다.

    ai-video 출력이 시장 클립 대비 ~9 LUFS 조용한 문제(벤치마크) 교정 → 쇼츠 표준(≈-14 LUFS).
    target_lufs None 이면 무변경(A/B 대조군). [aout] 은 _build_audio_filter 의 모든 반환 경로에서
    마지막에 정확히 한 번만 등장하므로 단순 치환이 안전하다.
    """
    if target_lufs is None:
        return audio_filter
    return (audio_filter.replace("[aout]", "[apremix]")
            + f";[apremix]loudnorm=I={target_lufs}:TP=-1.5:LRA=11[aout]")


def video_out_duration(clips, speed: float = 1.0) -> float:
    """출력 영상 길이(초) = 클립 길이 합 ÷ 배속. 순수(테스트 대상).

    `concat` 이 내는 길이와 같다 — 2026-08-24 실측으로 확인했다(클립 구간이 소스 끝을
    넘겨도 비디오·오디오가 함께 짧아지므로 이 합이 그대로 출력 길이다)."""
    total = sum(max(0.0, float(c.end_sec) - float(c.start_sec)) for c in (clips or []))
    return total / speed if speed > 0 else total


def cues_within_video(cue_files, clips, speed: float = 1.0, epsilon: float = 0.05):
    """**영상이 끝난 뒤 시작하는** TTS cue 를 걸러낸다. 순수(테스트 대상).

    Returns: (실을 cue 목록, 버린 cue 목록)

    렌더는 `amix=duration=longest` 를 `-shortest` 없이 섞으므로, 영상 밖에 놓인 cue 는
    **출력 컨테이너를 그만큼 늘린다** — 화면은 이미 끝났는데 정지 화면 위로 내레이션만
    흐르는 꼬리가 된다(2026-08-24 SHOTCONE 혜미리예채파 2화 실측: 비디오 25.025s ·
    컨테이너 39.400s, cue 창이 37.0~40.5s 였다. 25초 이후 비디오 패킷이 아예 없다).

    상류(`pipeline._resolve_cue_anchors`)는 `end = min(start+duration, total)` 로 cue 를
    영상 안에 가두므로 정상 경로에서는 여기 걸릴 것이 없다 — **이건 그 클램프를 우회한
    cue 를 렌더 직전에 막는 안전망**이다. 어디서 새는지와 무관하게 증상을 끊는다.

    ⚠ **영상 밖에서 시작하는 것만 버린다.** 영상 안에서 시작해 끝만 넘치는 cue 는
    화면 위에서 들리기 시작하므로 사람이 의도한 소리다 — 건드리지 않는다(별건).
    클립 정보가 없으면(길이 0) 아무것도 버리지 않는다 — 가드가 오작동해 멀쩡한
    내레이션을 지우는 것이 꼬리보다 나쁘다."""
    out_dur = video_out_duration(clips, speed)
    if out_dur <= 0:
        return list(cue_files or []), []
    kept, dropped = [], []
    for cf in (cue_files or []):
        cue = cf.get("cue") or {}
        try:
            start_out = float(cue.get("start_sec", 0.0)) / (speed if speed > 0 else 1.0)
        except (TypeError, ValueError):
            kept.append(cf)
            continue
        (dropped if start_out >= out_dur - epsilon else kept).append(cf)
    return kept, dropped


def sfx_within_video(sfx_items, clips, speed: float = 1.0):
    """E19-5 — 영상 밖에서 시작하는 SFX 를 걸러낸다. cue 안전망(cues_within_video)을
    **그대로 재사용**한다(수식 복제 금지 — 판정이 갈리면 언젠가 한쪽만 고쳐진다).
    Returns: (실을 목록, 버린 목록)."""
    wrapped = [{"_sfx": s, "cue": {"start_sec": (s or {}).get("start_sec")}}
               for s in (sfx_items or [])]
    kept, dropped = cues_within_video(wrapped, clips, speed)
    return [w["_sfx"] for w in kept], [w["_sfx"] for w in dropped]


def _build_audio_filter(inputs: RenderInputs, num_clip_inputs: int, num_cue_inputs: int) -> str:
    """편집 타임라인 절대 시간 기준 cue 리스트로 오디오 필터를 만든다.

    cue.voice/cue.speed 는 이미 mp3 합성 시점에 적용됐으므로 내레이션에 atempo 를 걸지
    않는다. 영상 배속(E7-2, design.video_speed)도 마찬가지로 **원본·현장음([acat])에만**
    atempo 를 건다 — 내레이션 속도는 cue.speed 의 소관이다. atempo 뒤의 시간축은 출력
    시각이므로 덕킹 창·adelay 위치는 cue 시간(소스 편집 타임라인) ×1/S 로 변환한다.
    0.8~2.0 은 atempo 단일 필터 범위(0.5~100) 안이다.
    """
    speed = float(getattr(inputs.design, "video_speed", 1.0) or 1.0)
    _tempo = f"atempo={speed:g}," if speed != 1.0 else ""

    # V3-M4: 원본 트랙 뮤트 창 — 덕킹과 같은 출력 시각(×1/S) 규약. 미지정 = 빈 문자열
    # (필터 종전과 바이트 동일).
    _mw = [(float(a) / speed, float(b) / speed)
           for a, b in (getattr(inputs, "muted_windows", None) or []) if b > a]
    _mute = ""
    _mute_expr = ""
    _mute_gain = getattr(inputs, "muted_gain_db", None)
    if _mw:
        _mute_expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in _mw)
        _mute_vol = "0" if _mute_gain is None else f"{float(_mute_gain):g}dB"
        _mute = f"volume=enable='{_mute_expr}':volume={_mute_vol},"

    # E19-5: SFX 는 cue 와 같은 믹스 경로(입력 + volume dB + adelay + amix)를 탄다.
    # 다만 원본 오디오를 **덕킹하지 않는다**(짧은 스팅에 덕킹을 걸면 원음이 펌핑한다).
    sfx_items = list(getattr(inputs, "sfx_audio", None) or [])

    if not inputs.tts_cue_files and not sfx_items:
        return f"[acat]{_tempo}{_mute}volume={inputs.original_audio_gain_db}dB[aout]"

    cue_files = list(inputs.tts_cue_files or [])

    # 덕킹 구간: cue.start_sec ~ cue.end_sec (출력 시각으로 ×1/S)
    duck_ranges: list[tuple[float, float]] = []
    for cf in cue_files:
        cue = cf.get("cue") or {}
        s = float(cue.get("start_sec", 0.0)) / speed
        e = float(cue.get("end_sec", 0.0)) / speed
        if e > s:
            duck_ranges.append((s, e))

    if duck_ranges:
        duck_expr = "+".join(
            f"between(t,{s:.3f},{e:.3f})" for s, e in duck_ranges
        )
        if _mute_expr and _mute_gain is not None:
            # 뮤트 창을 볼륨 감쇠로 쓸 때만: 그 창 안에서는 덕킹을 겹치지 않는다
            duck_expr = f"({duck_expr})*not({_mute_expr})"
        original_vol = (
            f"[acat]{_tempo}{_mute}volume=enable='{duck_expr}':volume=0.5,"
            f"volume={inputs.original_audio_gain_db}dB[orig_vol]"
        )
    else:
        original_vol = f"[acat]{_tempo}{_mute}volume={inputs.original_audio_gain_db}dB[orig_vol]"

    tts_filters: list[str] = []
    mix_inputs: list[str] = ["[orig_vol]"]

    # 입력 인덱스: 0..(num_clip_inputs-1)=클립, num_clip_inputs..=cue (cue_files 순서)
    for ci, cf in enumerate(cue_files):
        cue = cf.get("cue") or {}
        cue_input_idx = num_clip_inputs + ci
        tts_vol = f"[{cue_input_idx}:a]volume={inputs.tts_audio_gain_db}dB[cue{ci}_vol]"
        tts_filters.append(tts_vol)

        start_sec = float(cue.get("start_sec", 0.0)) / speed
        if start_sec > 0:
            delay_ms = int(start_sec * 1000)
            tts_delayed = (
                f"[cue{ci}_vol]adelay={delay_ms}|{delay_ms}[cue{ci}_delayed]"
            )
            tts_filters.append(tts_delayed)
            mix_inputs.append(f"[cue{ci}_delayed]")
        else:
            mix_inputs.append(f"[cue{ci}_vol]")

    # E19-5: 입력 인덱스 = 클립 수 + cue 수 + si — _build_input_args 가 같은 목록·같은
    # 순서로 -i 를 쌓으므로(render_video 에서 한 번만 거른 목록) 어긋나지 않는다.
    for si, sf in enumerate(sfx_items):
        sfx_input_idx = num_clip_inputs + num_cue_inputs + si
        gain = float(sf.get("gain_db", -6.0))
        start_sec = float(sf.get("start_sec", 0.0)) / speed
        # 영상 끝을 넘는 효과음 꼬리를 자른다(2026-09-03 실사고: 마지막 내레이션의 6.6s
        # whoosh 가 영상보다 3.7s 길어 amix=longest 가 컨테이너를 늘렸다 — 마지막 프레임이
        # 멈춘 채 소리만 흐른다). 파일이 그보다 짧으면 atrim 은 무해하다. cue 경로는 불변.
        _remain = video_out_duration(inputs.clips, speed) - start_sec
        _trim = f"atrim=end={_remain:.3f}," if _remain > 0 else ""
        tts_filters.append(f"[{sfx_input_idx}:a]{_trim}volume={gain:g}dB[sfx{si}_vol]")
        if start_sec > 0:
            delay_ms = int(start_sec * 1000)
            tts_filters.append(f"[sfx{si}_vol]adelay={delay_ms}|{delay_ms}[sfx{si}_delayed]")
            mix_inputs.append(f"[sfx{si}_delayed]")
        else:
            mix_inputs.append(f"[sfx{si}_vol]")

    if len(mix_inputs) <= 1:
        return original_vol.replace("[orig_vol]", "[aout]")

    mix_inputs_str = "".join(mix_inputs)
    mix_filter = (
        f"{';'.join(tts_filters)};{mix_inputs_str}"
        f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=2[aout]"
    )
    return f"{original_vol};{mix_filter}"