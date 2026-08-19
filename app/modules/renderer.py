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
    num_cue_inputs = len(inputs.tts_cue_files or [])
    filter_script = _build_filtergraph(inputs, num_clip_inputs=len(inputs.clips), num_cue_inputs=num_cue_inputs)
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
        if inputs.tts_cue_files:
            for cf in inputs.tts_cue_files:
                cue_path = Path(cf["path"]) if isinstance(cf.get("path"), str) else cf.get("path")
                if cue_path is None:
                    continue
                args.extend(["-i", str(_relpath_or_abs(cue_path, output_dir))])
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



def _build_filtergraph(inputs: RenderInputs, num_clip_inputs: int, num_cue_inputs: int) -> str:
    W = inputs.canvas_width
    H = inputs.canvas_height
    d = inputs.design

    ratio = getattr(d, 'aspect_ratio', '16:9')

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
                clean_word = word.split(':')[0].replace('{', '').replace('}', '') if ':' in word else word
                if len(current_line) + len(clean_word) <= max_chars:
                    current_line = (current_line + " " + word).strip()
                else:
                    if current_line: res_lines.append((orig_idx, current_line))
                    current_line = word
            if current_line: res_lines.append((orig_idx, current_line))
        return res_lines


    # 라운드 22: 최대 20자까지 한 줄 유지 (이전 15자). 20자 초과는 pipeline에서 LLM 재작성 또는 절단.
    title_lines = split_text_smart(inputs.title_text, 20)

    # 줄별 폰트 크기 (title_sizes가 있으면 사용, 없으면 title_size로 통일)
    title_sizes = getattr(d, 'title_sizes', [d.title_size])

    # [2] 비디오 레이아웃 설정
    scaled_w = W

    line_spacing = 30
    title_total_height = sum(
        (title_sizes[orig_idx] if orig_idx < len(title_sizes) else title_sizes[-1])
        for orig_idx, _ in title_lines
    ) + max(0, len(title_lines) - 1) * line_spacing

    try:
        r_w, r_h = map(int, ratio.split(':'))
        scaled_h = int(W * r_h / r_w)
    except:
        scaled_h = W

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

    for visual_idx, (orig_idx, raw_line) in enumerate(title_lines):
        base_color = custom_colors[orig_idx] if orig_idx < len(custom_colors) else custom_colors[-1]
        base_font_size = title_sizes[orig_idx] if orig_idx < len(title_sizes) else title_sizes[-1]
        font_size = _scale_font_for_length(base_font_size, len(raw_line))
        y_pos = cumulative_y
        cumulative_y += font_size + line_spacing
        escaped_full = _escape_text_for_drawtext(raw_line)
        next_label = f"[title_{visual_idx}]"

        filters.append(
            f"{last_v_label}drawtext=expansion=none:fontfile='{font_arg}':text='{escaped_full}':"
            f"fontcolor={base_color}:fontsize={font_size}:"
            f"x=(w-text_w)/2:y={y_pos}{next_label}"
        )
        last_v_label = next_label


    # [5.5] 플랫폼 표기 — 권리사 '영상 내 플랫폼 노출' 요구(티빙·Wavve·쿠팡플레이 등).
    # 위치는 캔버스가 아니라 **영상영역 왼쪽 상단**(overlay_y) 기준 오프셋 — aspect_ratio 를
    # 바꿔도 표기가 영상을 따라간다. 이미지·텍스트 중 이미지 우선(둘 다 준 경우).
    _pf_img = getattr(d, 'platform_image', None)
    _pf_txt = getattr(d, 'platform_text', None)
    if _pf_img or _pf_txt:
        pf_off = getattr(d, 'platform_x', 24)          # 앵커 쪽 모서리에서의 오프셋
        pf_right = getattr(d, 'platform_align', 'left') == "right"
        pf_x = pf_off                                   # left 기본 — right 는 아래서 재계산
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
                # 이미지 폭을 알면 우변 기준 고정 좌표. 폭 미상(-1 폴백)이면 overlay 식으로.
                pf_x = f"W-w-{pf_off}" if pf_h == -1 else W - pf_w - pf_off
            _pf_str = str(_pf_path).replace("\\", "/").replace(":", "\\:")
            print(f"  [Platform] 로고 {_pw}x{_ph} → {pf_w}x{pf_h} @ ({pf_x},{pf_y})")
            filters.append(
                f"movie='{_pf_str}',scale={pf_w}:{pf_h}[pfm];"
                f"{last_v_label}[pfm]overlay={pf_x}:{pf_y}[with_pf]"
            )
        else:
            if pf_right:
                pf_x = f"w-text_w-{pf_off}"   # drawtext 는 텍스트 폭을 자기 식으로 안다
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
            s, e = float(im["start_sec"]), float(im["end_sec"])
            out = f"[{tag}{j}]"
            print(f"  [EditImage] {Path(str(im['file'])).name}: w={img_w}px @ ({img_x},{img_y}) "
                  f"{s:.2f}~{e:.2f}s layer={int(im.get('layer') or 0)}")
            filters.append(
                f"movie='{img_path}',scale={img_w}:-2[{tag}src{j}];"
                f"{label}[{tag}src{j}]overlay={img_x}:{img_y}:"
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

    # 자막 위 이미지가 있으면 ASS 출력을 중간 라벨로 받고 그 위에 얹은 뒤 [vout] 으로 마감
    _tts_out = "[vpretop]" if _eimgs_above else "[vout]"
    if inputs.tts_subtitle_path and inputs.tts_subtitle_path.exists():
        tts_ass_path = inputs.tts_subtitle_path.resolve()
        tts_sub_fixed = _to_short_path(str(tts_ass_path)).replace("\\", "/").replace(":", "\\:")
        filters.append(f"{last_v_label}ass='{tts_sub_fixed}':fontsdir='{font_dir_fixed}'{_tts_out}")
        last_v_label = _tts_out
    else:
        filters.append(f"{last_v_label}null{_tts_out}")
        last_v_label = _tts_out

    if _eimgs_above:
        _top_label = _overlay_images(_eimgs_above, last_v_label, "eimga")
        filters.append(f"{_top_label}null[vout]")
        last_v_label = "[vout]"

    audio_filter = _build_audio_filter(inputs, num_clip_inputs, num_cue_inputs)
    audio_filter = _apply_loudnorm(audio_filter, getattr(inputs, "loudness_target_lufs", None))
    filters.append(audio_filter)
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


def _build_audio_filter(inputs: RenderInputs, num_clip_inputs: int, num_cue_inputs: int) -> str:
    """편집 타임라인 절대 시간 기준 cue 리스트로 오디오 필터를 만든다.

    voice/speed는 이미 mp3 합성 시점에 적용됐으므로 atempo는 추가하지 않는다.
    cue 시간(start_sec~end_sec)이 원본 오디오 덕킹 구간이며 adelay 위치이다.
    """
    if not inputs.tts_cue_files:
        return f"[acat]volume={inputs.original_audio_gain_db}dB[aout]"

    cue_files = list(inputs.tts_cue_files or [])

    # 덕킹 구간: cue.start_sec ~ cue.end_sec 그대로
    duck_ranges: list[tuple[float, float]] = []
    for cf in cue_files:
        cue = cf.get("cue") or {}
        s = float(cue.get("start_sec", 0.0))
        e = float(cue.get("end_sec", 0.0))
        if e > s:
            duck_ranges.append((s, e))

    if duck_ranges:
        duck_expr = "+".join(
            f"between(t,{s:.3f},{e:.3f})" for s, e in duck_ranges
        )
        original_vol = (
            f"[acat]volume=enable='{duck_expr}':volume=0.5,"
            f"volume={inputs.original_audio_gain_db}dB[orig_vol]"
        )
    else:
        original_vol = f"[acat]volume={inputs.original_audio_gain_db}dB[orig_vol]"

    tts_filters: list[str] = []
    mix_inputs: list[str] = ["[orig_vol]"]

    # 입력 인덱스: 0..(num_clip_inputs-1)=클립, num_clip_inputs..=cue (cue_files 순서)
    for ci, cf in enumerate(cue_files):
        cue = cf.get("cue") or {}
        cue_input_idx = num_clip_inputs + ci
        tts_vol = f"[{cue_input_idx}:a]volume={inputs.tts_audio_gain_db}dB[cue{ci}_vol]"
        tts_filters.append(tts_vol)

        start_sec = float(cue.get("start_sec", 0.0))
        if start_sec > 0:
            delay_ms = int(start_sec * 1000)
            tts_delayed = (
                f"[cue{ci}_vol]adelay={delay_ms}|{delay_ms}[cue{ci}_delayed]"
            )
            tts_filters.append(tts_delayed)
            mix_inputs.append(f"[cue{ci}_delayed]")
        else:
            mix_inputs.append(f"[cue{ci}_vol]")

    if len(mix_inputs) <= 1:
        return original_vol.replace("[orig_vol]", "[aout]")

    mix_inputs_str = "".join(mix_inputs)
    mix_filter = (
        f"{';'.join(tts_filters)};{mix_inputs_str}"
        f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=2[aout]"
    )
    return f"{original_vol};{mix_filter}"