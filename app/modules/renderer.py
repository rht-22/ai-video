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
import subprocess
from dataclasses import dataclass, replace, field
from pathlib import Path
from typing import Any, Iterable

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
    tts_audio_files: dict[int, Path] | None = None  # clip_idx -> tts mp3 path
    original_audio_gain_db: int = -10
    tts_audio_gain_db: int = -4
    render_preset: str = "balanced"  # balanced|fastest|quality (현재는 balanced만 사용)
    enable_hwaccel: bool = True
    title_textfile: Path | None = None
    work_title_textfile: Path | None = None


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
    
    wrapped_title = _wrap_text(inputs.title_text, 14, inputs.design.title_size)

    title_file = inputs.title_textfile or (output_dir / "title.txt")
    work_file = inputs.work_title_textfile or (output_dir / "work_title.txt")

    title_file.write_bytes((wrapped_title + "\n").encode("utf-8-sig"))
    work_file.write_bytes((inputs.work_title + "\n").encode("utf-8-sig"))

    inputs = replace(inputs, title_textfile=title_file, work_title_textfile=work_file)
    tts_keys_sorted: list[int] = sorted(inputs.tts_audio_files.keys()) if inputs.tts_audio_files else []
    filter_script = _build_filtergraph(inputs, num_clip_inputs=len(inputs.clips), tts_keys_sorted=tts_keys_sorted)
    filter_path = inputs.output_path.with_suffix(".filter.txt")
    filter_path.write_text(filter_script, encoding="utf-8")

    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")

    # output_dir 기준 상대경로로 실행 (ass 필터 안정화)
    output_relative = _relpath_or_abs(inputs.output_path, output_dir)



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
        if inputs.tts_audio_files:
            for clip_idx in tts_keys_sorted:
                tts_path = inputs.tts_audio_files[clip_idx]
                args.extend(["-i", str(_relpath_or_abs(tts_path, output_dir))])
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
                "-filter_complex_script", str(filter_path),
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
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
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



def _build_filtergraph(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
    W = inputs.canvas_width
    H = inputs.canvas_height
    d = inputs.design 

    ratio = getattr(d, 'aspect_ratio', '16:9')

    # [1] 제목 줄바꿈 로직 (기존 유지)
    def split_text_smart(text: str, max_chars: int = 14) -> list[str]:
        if not text: return []
        lines = text.split('\n')
        res_lines = []
        for line in lines:
            words = line.split()
            current_line = ""
            for word in words:
                clean_word = word.split(':')[0].replace('{', '').replace('}', '') if ':' in word else word
                if len(current_line) + len(clean_word) <= max_chars:
                    current_line = (current_line + " " + word).strip()
                else:
                    if current_line: res_lines.append(current_line)
                    current_line = word
            if current_line: res_lines.append(current_line)
        return res_lines


    


    title_lines = split_text_smart(inputs.title_text, 14)

    # 줄별 폰트 크기 (title_sizes가 있으면 사용, 없으면 title_size로 통일)
    title_sizes = getattr(d, 'title_sizes', [d.title_size])

    # [2] 비디오 레이아웃 설정
    scaled_w = W

    line_spacing = 30
    title_total_height = sum(
        (title_sizes[i] if i < len(title_sizes) else title_sizes[-1])
        for i in range(len(title_lines))
    ) + max(0, len(title_lines) - 1) * line_spacing

    try:
        r_w, r_h = map(int, ratio.split(':'))
        scaled_h = int(W * r_h / r_w)
    except:
        scaled_h = W

    overlay_y = (H - scaled_h) // 2

    # FFmpeg 짝수 보정
    scaled_w -= scaled_w % 2
    scaled_h -= scaled_h % 2
    overlay_y = max(0, overlay_y)

    filters: list[str] = []

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

        v_filter = (
            f"[{i}:v]{crop_filter}"
            f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase,"
            f"setsar=1,"
            f"crop={W}:{scaled_h},"
            f"pad={W}:{H}:0:{overlay_y}:color=#0D0011[v{i}]"
        )
        filters.append(v_filter)
        filters.append(f"[{i}:a]anull[a{i}]")

    # [4] 연결(Concat)
    concat_inputs = []
    for i in range(num_clip_inputs):
        concat_inputs.append(f"[v{i}]")
        concat_inputs.append(f"[a{i}]")
    filters.append(f"{''.join(concat_inputs)}concat=n={num_clip_inputs}:v=1:a=1[vcat][acat]")


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
    last_v_label = "[vcat]"
    custom_colors = getattr(d, 'title_colors', ["white"])
    if "/" in actual_font or "\\" in actual_font:
        # 유니코드 경로(한글 등)를 FFmpeg이 인식할 수 있도록 8.3 단축 경로로 변환
        short_path = _to_short_path(actual_font.replace("/", "\\"))
        clean_path = short_path.replace("\\", "/").replace(":", "\\:")
        font_arg = clean_path

    cumulative_y = d.title_y
    for idx, raw_line in enumerate(title_lines):
        base_color = custom_colors[idx] if idx < len(custom_colors) else custom_colors[-1]
        font_size = title_sizes[idx] if idx < len(title_sizes) else title_sizes[-1]
        y_pos = cumulative_y
        cumulative_y += font_size + line_spacing
        escaped_full = _escape_text_for_drawtext(raw_line)
        next_label = f"[title_{idx}]"

        filters.append(
            f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_full}':"
            f"fontcolor={base_color}:fontsize={font_size}:"
            f"x=(w-text_w)/2:y={y_pos}{next_label}"
        )
        last_v_label = next_label


    # [6] 작품명(Logo)
    work_label = "[with_work]"
    work_type = getattr(d, 'work_type', 'text')
    work_value = getattr(d, 'work_value', inputs.work_title)
    if work_type == "image" and work_value:
        logo_path_str = str(Path(work_value).resolve()).replace("\\", "/").replace(":", "\\:")
        logo_w = getattr(d, 'work_image_width', 300)
        filters.append(f"movie='{logo_path_str}',scale={logo_w}:-1[logo];{last_v_label}[logo]overlay=(W-w)/2:{d.work_title_y}{work_label}")
    else:
        raw_work = work_value if work_value else inputs.work_title
        # 레터스페이싱 적용: 글자 사이에 공백 삽입
        if getattr(d, 'work_letter_spacing', False):
            raw_work = " ".join(raw_work)
        escaped_val = _escape_text_for_drawtext(raw_work)
        filters.append(f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_val}':fontcolor={d.work_color}:fontsize={d.work_font_size}:x=(w-text_w)/2:y={d.work_title_y}{work_label}")

  
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
        filters.append(f"{work_label}ass='{sub_path_fixed}':fontsdir='{font_dir_fixed}'[vsub]")
        last_v_label = "[vsub]"
    else:
        filters.append(f"{work_label}null[vsub]")
        last_v_label = "[vsub]"

    if inputs.tts_subtitle_path and inputs.tts_subtitle_path.exists():
        tts_ass_path = inputs.tts_subtitle_path.resolve()
        tts_sub_fixed = _to_short_path(str(tts_ass_path)).replace("\\", "/").replace(":", "\\:")
        filters.append(f"{last_v_label}ass='{tts_sub_fixed}':fontsdir='{font_dir_fixed}'[vout]")
        last_v_label = "[vout]"
    else:
        filters.append(f"{last_v_label}null[vout]")
        last_v_label = "[vout]"

    filters.append(_build_audio_filter(inputs, num_clip_inputs, tts_keys_sorted))
    return ";".join(filters)

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

def _build_audio_filter(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
 
    if not inputs.tts_audio_files:
        # TTS 없으면 원본 오디오 그대로
        return f"[acat]volume={inputs.original_audio_gain_db}dB[aout]"
 
    # 각 클립의 편집 타임라인 기준 시작 시간 계산
    clip_times: list[tuple[float, StoryClip]] = []
    current = 0.0
    for clip in inputs.clips:
        clip_times.append((current, clip))
        current += clip.end_sec - clip.start_sec
 
    # TTS가 재생되는 구간 계산 → 원본 오디오 음소거 구간
    # atempo=1.5 적용 후 실제 재생 길이 = 원본 길이 / 1.5
    mute_ranges: list[tuple[float, float]] = []
    tts_pos = {clip_idx: pos for pos, clip_idx in enumerate(tts_keys_sorted)}
    for clip_idx, (start_time, _) in enumerate(clip_times):
        if clip_idx not in inputs.tts_audio_files:
            continue
        tts_path = inputs.tts_audio_files[clip_idx]
        raw_dur = _get_audio_duration(tts_path)
        if raw_dur > 0:
            # atempo=1.5이므로 실제 재생 길이는 짧아짐
            actual_dur = raw_dur / 1.2
            mute_end = start_time + actual_dur
            mute_ranges.append((start_time, mute_end))
 
    # 원본 오디오: TTS 구간만 음소거, 나머지는 정상 볼륨
    if mute_ranges:
        # volume 필터의 enable 표현식: TTS 재생 구간에서 volume=0
        mute_expr = "+".join(
            f"between(t,{s:.3f},{e:.3f})" for s, e in mute_ranges
        )
        original_vol = (
            f"[acat]volume=enable='{mute_expr}':volume=0,"
            f"volume={inputs.original_audio_gain_db}dB[orig_vol]"
        )
    else:
        original_vol = f"[acat]volume={inputs.original_audio_gain_db}dB[orig_vol]"
 
    tts_filters: list[str] = []
    mix_inputs: list[str] = ["[orig_vol]"]
 
    # 입력 인덱스: 0..(num_clip_inputs-1)=클립, num_clip_inputs..=tts
    for clip_idx, (start_time, _) in enumerate(clip_times):
        if clip_idx not in inputs.tts_audio_files:
            continue
        tts_input_idx = num_clip_inputs + tts_pos[clip_idx]
 
        # TTS 속도 1.5배
        tts_speed = f"[{tts_input_idx}:a]atempo=1.2[tts{clip_idx}_speed]"
        tts_filters.append(tts_speed)
 
        tts_vol = f"[tts{clip_idx}_speed]volume={inputs.tts_audio_gain_db}dB[tts{clip_idx}_vol]"
        tts_filters.append(tts_vol)
 
        if start_time > 0:
            delay_ms = int(start_time * 1000)
            tts_delayed = (
                f"[tts{clip_idx}_vol]adelay={delay_ms}|{delay_ms}[tts{clip_idx}_delayed]"
            )
            tts_filters.append(tts_delayed)
            mix_inputs.append(f"[tts{clip_idx}_delayed]")
        else:
            mix_inputs.append(f"[tts{clip_idx}_vol]")
 
    if len(mix_inputs) <= 1:
        return original_vol.replace("[orig_vol]", "[aout]")
 
    # amix로 원본 + TTS 믹싱
    mix_inputs_str = "".join(mix_inputs)
    mix_filter = (
        f"{';'.join(tts_filters)};{mix_inputs_str}"
        f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=2[aout]"
    )
    return f"{original_vol};{mix_filter}"