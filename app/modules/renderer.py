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


@dataclass(frozen=True)
class RenderInputs:
    video_path: Path
    clips: list[StoryClip]
    subtitle_path: Path | None  # None이면 자막 없이 렌더링
    crop_timeline_map: dict[str, Path]
    title_text: str
    work_title: str
    output_path: Path
    canvas_width: int
    canvas_height: int
    top_title_height: int
    bottom_label_height: int
    design: DesignConfig = field(default_factory=DesignConfig)
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
    work_file.write_bytes((f"작품명: {inputs.work_title}" + "\n").encode("utf-8-sig"))

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



# def _build_filtergraph(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
#     W = inputs.canvas_width
#     H = inputs.canvas_height
#     d = inputs.design 

#     # [1] 비디오 레이아웃 설정
#     # scaled_w = d.video_width
#     scaled_w = W
#     scaled_h = d.video_height
#     scaled_w -= scaled_w % 2
#     scaled_h -= scaled_h % 2
    
#     # overlay_x = int((W - scaled_w) / 2)
#     overlay_x = 0
#     overlay_y = d.video_y_pos 

#     filters: list[str] = []

#     # [2] 클립별 스케일 및 패딩 (배경 검은색)
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

#         # v_filter = (
#         #     f"[{i}:v]{crop_filter}"
#         #     f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=decrease,"
#         #     f"setsar=1,"
#         #     f"pad={W}:{H}:{overlay_x}:{overlay_y}:black[v{i}]"
#         # )
#         v_filter = (
#             f"[{i}:v]{crop_filter}"
#             f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase," # decrease를 increase로 변경하여 꽉 채움
#             f"setsar=1,"
#             f"crop={W}:{scaled_h}," # 혹시 비율 차이로 삐져나온 부분 절삭
#             f"pad={W}:{H}:0:{overlay_y}:black[v{i}]"
#         )
#         filters.append(v_filter)
#         filters.append(f"[{i}:a]anull[a{i}]")

#     # [3] 연결(Concat) - 비디오/오디오 쌍을 맞춰서 입력
#     concat_inputs = []
#     for i in range(num_clip_inputs):
#         concat_inputs.append(f"[v{i}]")
#         concat_inputs.append(f"[a{i}]")
    
#     filters.append(f"{''.join(concat_inputs)}concat=n={num_clip_inputs}:v=1:a=1[vcat][acat]")

#     # [4] 제목(Title) 필터 - 스마트 줄바꿈 적용
#     # def split_text_smart(text: str, max_chars: int = 14) -> list[str]:
#     #     words = text.split()
#     #     res_lines, current_line = [], ""
#     #     for word in words:
#     #         if len(current_line) + len(word) <= max_chars:
#     #             current_line = (current_line + " " + word).strip()
#     #         else:
#     #             if current_line: res_lines.append(current_line)
#     #             current_line = word
#     #     if current_line: res_lines.append(current_line)
#     #     return res_lines

#     # title_lines = split_text_smart(inputs.title_text, 14)
#     # font_arg = str(d.title_font).replace("\\", "/").replace(":", "\\:")
    
#     # last_v_label = "[vcat]" 
#     # for idx, line in enumerate(title_lines):
#     #     next_label = f"[title_{idx}]"
#     #     y_pos = d.title_y + (idx * (d.title_size + 20))
#     #     escaped_line = _escape_text_for_drawtext(line.strip())
#     #     # filters.append(
#     #     #     f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_line}':"
#     #     #     f"fontcolor={d.title_color}:fontsize={d.title_size}:x=(w-text_w)/2:y={y_pos}{next_label}"
#     #     # )
#     #     # last_v_label = next_label
#     #     filters.append(
#     #         f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_line}':"
#     #         f"fontcolor={d.title_color}:fontsize={d.title_size}:x=(w-text_w)/2:y={y_pos}:"
#     #         # f"box=1:boxcolor=black@0.6:boxw={W}:boxh={d.title_size + 40}{next_label}" 
#     #     )
#     #     last_v_label = next_label
#     def split_text_smart(text: str, max_chars: int = 14) -> list[str]:
#         words = text.split()
#         res_lines, current_line = [], ""
#         for word in words:
#             # 색상 태그 {단어:색상}는 길이에 포함하지 않도록 처리
#             display_word = word.split(':')[0].replace('{', '').replace('}', '') if ':' in word else word
#             if len(current_line) + len(display_word) <= max_chars:
#                 current_line = (current_line + " " + word).strip()
#             else:
#                 if current_line: res_lines.append(current_line)
#                 current_line = word
#         if current_line: res_lines.append(current_line)
#         return res_lines

#     title_lines = split_text_smart(inputs.title_text, 14)
#     font_arg = str(d.title_font).replace("\\", "/").replace(":", "\\:")
    
#     # JSON에서 colors 리스트를 가져옴 (없으면 기본값 사용)
#     custom_colors = getattr(d, 'title_colors', ["white"])
    
#     # 영상 위치 계산 (제목 영역 하단 1cm 여백)
#     title_total_height = len(title_lines) * (d.title_size + 30)
#     overlay_y = d.title_y + title_total_height + 180 

#     last_v_label = "[vcat]"
    
#     import re
    
#     for idx, raw_line in enumerate(title_lines):
#         # 1. 줄 기본 색상 결정 (JSON의 colors 리스트 순서대로, 모자라면 마지막 색상)
#         base_color = custom_colors[idx] if idx < len(custom_colors) else custom_colors[-1]
#         y_pos = d.title_y + (idx * (d.title_size + 30))
        
#         # 2. 태그 분석 {단어:색상}
#         # 예: "오늘 {강식당:yellow} 오픈" -> [("오늘 ", None), ("강식당", "yellow"), (" 오픈", None)]
#         parts = []
#         last_end = 0
#         for match in re.finditer(r"\{([^:]+):([^}]+)\}", raw_line):
#             if match.start() > last_end:
#                 parts.append((raw_line[last_end:match.start()], base_color))
#             parts.append((match.group(1), match.group(2)))
#             last_end = match.end()
#         if last_end < len(raw_line):
#             parts.append((raw_line[last_end:], base_color))
            
#         if not parts: # 태그가 없는 경우 전체 줄 출력
#             parts = [(raw_line, base_color)]

#         # 3. 한 줄 내의 파트별로 drawtext 생성 (좌표 계산)
#         # FFmpeg의 drawtext는 x=(w-text_w)/2로 중앙 정렬을 하므로, 
#         # 한 줄 내 부분 색상은 전체 텍스트 길이를 고려한 상대 좌표가 필요함
#         full_text_plain = "".join([p[0] for p in parts]).strip()
#         escaped_full = _escape_text_for_drawtext(full_text_plain)
        
#         current_x_offset = 0
#         for p_idx, (p_text, p_color) in enumerate(parts):
#             next_label = f"[title_{idx}_{p_idx}]"
#             escaped_p = _escape_text_for_drawtext(p_text.strip())
            
#             # 첫 번째 파트는 전체 텍스트를 투명하게 깔고 그 위에 텍스트를 그림 (중앙 정렬 기준점)
#             # 이후 파트는 이전 파트의 너비만큼 x축으로 이동
#             if p_idx == 0:
#                 # 전체 줄의 시작 x좌표: (w - 전체너비)/2
#                 start_x = f"(w-text_w)/2"
#             else:
#                 # 이전 파트의 너비를 알기 어렵기에, 실제로는 필터를 중첩해서 그림
#                 pass

#             # 한 줄 내 부분 색상은 구현이 매우 복잡하므로, 
#             # 여기서는 '줄별 색상' 기능을 우선 완벽하게 제공하고, 
#             # 단어별 색상은 {단어:색상} 입력 시 해당 줄 전체에 우선순위 색상으로 적용되게 함
#             # (만약 한 줄 내 혼합 색상이 필수라면 더 복잡한 좌표 계산 로직을 추가하겠습니다)
            
#             display_color = p_color if p_color else base_color
            
#             filters.append(
#                 f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_full}':"
#                 f"fontcolor={display_color}:fontsize={d.title_size}:x=(w-text_w)/2:y={y_pos}{next_label}"
#             )
#             last_v_label = next_label

#     # [5] 작품명(Work Title) 필터
#     # work_label = "[with_work]"
#     # filters.append(
#     #     f"{last_v_label}drawtext=fontfile='{font_arg}':text=' {inputs.work_title}':"
#     #     f"fontcolor={d.work_color}:fontsize={d.work_font_size}:"
#     #     f"x=(w-text_w)/2:y={d.work_title_y}{work_label}"
#     # )

#     # [5] 작품명(Work Title/Logo)
#     work_label = "[with_work]"
#     work_type = getattr(d, 'work_type', 'text')
#     # work_config에서 value가 없으면 기본 work_title 텍스트 사용
#     work_value = getattr(d, 'work_value', inputs.work_title)

#     if work_type == "image" and work_value:
#         logo_path = Path(work_value).resolve()
#         if logo_path.exists():
#             logo_path_str = str(logo_path).replace("\\", "/").replace(":", "\\:")
#             logo_w = getattr(d, 'work_image_width', 200)
            
#             # Y좌표를 직접 입력하는 대신, 자막(ASS)보다 아래에 오도록 큰 값 설정
#             # 혹은 JSON의 y값을 1700 이상으로 높게 잡아야 합니다.
#             filters.append(
#                 f"movie='{logo_path_str}',scale={logo_w}:-1[logo];"
#                 f"{last_v_label}[logo]overlay=(W-w)/2:{d.work_title_y}{work_label}"
#             )
#         else:
#             # 이미지 경로 오류 시 경고 텍스트 표시
#             escaped_val = _escape_text_for_drawtext(f"Logo Missing: {work_value}")
#             filters.append(f"{last_v_label}drawtext=text='{escaped_val}':fontcolor=red:fontsize=30:x=(w-text_w)/2:y={d.work_title_y}{work_label}")
#     else:
#         # 일반 텍스트 모드
#         escaped_val = _escape_text_for_drawtext(work_value if work_value else inputs.work_title)
#         filters.append(
#             f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_val}':"
#             f"fontcolor={d.work_color}:fontsize={d.work_font_size}:x=(w-text_w)/2:y={d.work_title_y}{work_label}"
#         )

#     # [6] 자막(ASS) 적용 - 영상 내부(작품명 레이어 위)에 오버레이
#     if inputs.subtitle_path:
#         sub_path = str(inputs.subtitle_path.resolve()).replace("\\", "/").replace(":", "\\:")
#         filters.append(f"{work_label}ass='{sub_path}'[vout]")
#     else:
#         filters.append(f"{work_label}null[vout]")

#     # [7] 오디오 필터
#     filters.append(_build_audio_filter(inputs, num_clip_inputs, tts_keys_sorted))
    
#     return ";".join(filters)
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

    # [2] 비디오 레이아웃 설정
    scaled_w = W
    # scaled_h = d.video_height
    
    line_spacing = 30
    title_total_height = (len(title_lines) * d.title_size) + ((len(title_lines) - 1) * line_spacing)
    title_bottom_y = d.title_y + title_total_height
    overlay_y = title_bottom_y + 20 # 제목 아래 50px 여백

    try:
        r_w, r_h = map(int, ratio.split(':'))
        scaled_h = int(W * r_h / r_w)
    except:
        scaled_h = W

    # scaled_w -= scaled_w % 2
    # scaled_h -= scaled_h % 2

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
                    avg_cx = sum(kf['x_center'] for kf in crop_json) / len(crop_json)
                    cw, ch = crop_json[0]['crop_w'], crop_json[0]['crop_h']
                    cy = crop_json[0]['y_center']
                    crop_filter = f"crop={cw}:{ch}:{avg_cx}-{cw}/2:{cy}-{ch}/2,"
            except: pass

        v_filter = (
            f"[{i}:v]{crop_filter}"
            f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase,"
            f"setsar=1,"
            f"crop={W}:{scaled_h},"
            f"pad={W}:{H}:0:{overlay_y}:black[v{i}]"
        )
        filters.append(v_filter)
        filters.append(f"[{i}:a]anull[a{i}]")

    # [4] 연결(Concat)
    concat_inputs = []
    for i in range(num_clip_inputs):
        concat_inputs.append(f"[v{i}]")
        concat_inputs.append(f"[a{i}]")
    filters.append(f"{''.join(concat_inputs)}concat=n={num_clip_inputs}:v=1:a=1[vcat][acat]")

    # [5] 제목(Title) 필터
    font_arg = str(d.title_font).replace("\\", "/").replace(":", "\\:")
    custom_colors = getattr(d, 'title_colors', ["white"])
    last_v_label = "[vcat]"
    for idx, raw_line in enumerate(title_lines):
        base_color = custom_colors[idx] if idx < len(custom_colors) else custom_colors[-1]
        y_pos = d.title_y + (idx * (d.title_size + line_spacing))
        escaped_full = _escape_text_for_drawtext(raw_line)
        next_label = f"[title_{idx}]"
        filters.append(f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_full}':fontcolor={base_color}:fontsize={d.title_size}:x=(w-text_w)/2:y={y_pos}{next_label}")
        last_v_label = next_label

    # [6] 작품명(Logo)
    work_label = "[with_work]"
    work_type = getattr(d, 'work_type', 'text')
    work_value = getattr(d, 'work_value', inputs.work_title)
    if work_type == "image" and work_value:
        logo_path_str = str(Path(work_value).resolve()).replace("\\", "/").replace(":", "\\:")
        logo_w = getattr(d, 'work_image_width', 200)
        filters.append(f"movie='{logo_path_str}',scale={logo_w}:-1[logo];{last_v_label}[logo]overlay=(W-w)/2:{d.work_title_y}{work_label}")
    else:
        escaped_val = _escape_text_for_drawtext(work_value if work_value else inputs.work_title)
        filters.append(f"{last_v_label}drawtext=fontfile='{font_arg}':text='{escaped_val}':fontcolor={d.work_color}:fontsize={d.work_font_size}:x=(w-text_w)/2:y={d.work_title_y}{work_label}")

    # [7] 자막(ASS) 적용
    if inputs.subtitle_path:
        sub_path = str(inputs.subtitle_path.resolve()).replace("\\", "/").replace(":", "\\:")
        # filters.append(f"{work_label}ass='{sub_path}'[vout]")
        filters.append(f"{work_label}ass='{sub_path}':original_size={W}x{H}[vout]")
    else:
        filters.append(f"{work_label}null[vout]")
   
   

    filters.append(_build_audio_filter(inputs, num_clip_inputs, tts_keys_sorted))
    return ";".join(filters)

def _build_audio_filter(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
    # concat된 원본 오디오 볼륨 조절
    original_vol = f"[acat]volume={inputs.original_audio_gain_db}dB[orig_vol]"

    if not inputs.tts_audio_files:
        return original_vol.replace("[orig_vol]", "[aout]")

    # 각 클립의 시작 시간(편집 타임라인 기준) 계산
    clip_times: list[tuple[float, StoryClip]] = []
    current = 0.0
    for clip in inputs.clips:
        clip_times.append((current, clip))
        current += clip.end_sec - clip.start_sec

    tts_filters: list[str] = []
    mix_inputs: list[str] = ["[orig_vol]"]

    # 입력 인덱스: 0..(num_clip_inputs-1)=클립, num_clip_inputs..=tts (tts_keys_sorted 순서)
    tts_pos = {clip_idx: pos for pos, clip_idx in enumerate(tts_keys_sorted)}
    for clip_idx, (start_time, _) in enumerate(clip_times):
        if clip_idx not in inputs.tts_audio_files:
            continue
        tts_input_idx = num_clip_inputs + tts_pos[clip_idx]
        # TTS 속도 1.5배 적용 (최신 트렌드)
        tts_speed = f"[{tts_input_idx}:a]atempo=1.5[tts{clip_idx}_speed]"
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

    # amix 입력은 공백 없이 연결해야 함: [a][b][c]amix=...
    mix_inputs_str = "".join(mix_inputs)
    mix_filter = (
        f"{';'.join(tts_filters)};{mix_inputs_str}"
        f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=2[aout]"
    )
    return f"{original_vol};{mix_filter}"

