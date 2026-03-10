from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.story_builder import StoryClip

_VIDEO_ENCODER_CACHE: str | None = None


# @dataclass(frozen=True)
# class RenderInputs:
    # video_path: Path
    # clips: list[StoryClip]
    # subtitle_path: Path | None  # None이면 자막 없이 렌더링
    # crop_timeline_map: dict[str, Path]
    # title_text: str
    # work_title: str
    # output_path: Path
    # canvas_width: int
    # canvas_height: int
    # top_title_height: int
    # bottom_label_height: int
    # tts_audio_files: dict[int, Path] | None = None  # clip_idx -> tts mp3 path
    # original_audio_gain_db: int = -10
    # tts_audio_gain_db: int = -4
    # render_preset: str = "balanced"  # balanced|fastest|quality (현재는 balanced만 사용)
    # enable_hwaccel: bool = True
    # title_textfile: Path | None = None
    # work_title_textfile: Path | None = None
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

# def render_short(inputs: RenderInputs) -> list[str]:
    # """
    # 클립별 입력(-ss/-to -i) + concat filter로 필요한 구간만 처리한 뒤,
    # filter_complex로 비디오 합성(배경/텍스트/자막) 및 오디오 믹싱을 수행합니다.
    # - concat demuxer(inpoint/outpoint)는 컨테이너/코덱에 따라 컷이 무시되어 전체 영상을 처리할 수 있어 사용하지 않습니다.
    # - Windows 경로 호환을 위해 cwd를 output_dir로 두고 상대경로를 사용합니다.
    # """
    # output_dir = inputs.output_path.parent
    # output_dir.mkdir(parents=True, exist_ok=True)

    # if not inputs.clips:
    #     raise ValueError("렌더링할 clips가 비어 있습니다.")

    # # 제목 자동 줄바꿈 처리 (캔버스 너비 초과 시)
    # def _wrap_text(text: str, max_width_px: int, font_size: int) -> str:
    #     """텍스트를 최대 너비에 맞춰 줄바꿈 처리 (한글 기준 대략적 계산)"""
    #     # 한글 기준: 폰트 크기 * 문자수 * 0.8 (대략적 너비)
    #     chars_per_line = int(max_width_px / (font_size * 0.8))
    #     if len(text) <= chars_per_line:
    #         return text
    #     # 단어 단위로 분할하지 않고 문자 단위로 줄바꿈
    #     lines = []
    #     for i in range(0, len(text), chars_per_line):
    #         lines.append(text[i:i + chars_per_line])
    #     return "\n".join(lines)
    
    # # drawtext 한글/특수문자 안정화를 위해 textfile 사용 (UTF-8 BOM)
    # # title_font 값은 _build_filtergraph에서도 사용되므로 여기서 정의
    # title_font = 54
    # title_file = inputs.title_textfile or (output_dir / "title.txt")
    # work_file = inputs.work_title_textfile or (output_dir / "work_title.txt")
    # wrapped_title_for_file = _wrap_text(inputs.title_text, inputs.canvas_width - 80, title_font)
    # title_file.write_bytes((wrapped_title_for_file + "\n").encode("utf-8-sig"))
    # work_file.write_bytes((f"작품명: {inputs.work_title}" + "\n").encode("utf-8-sig"))

    # inputs = replace(inputs, title_textfile=title_file, work_title_textfile=work_file)

    # # TTS 입력 순서(커맨드 -i 추가 순서)와 오디오 필터 인덱스 계산을 일치시킵니다.
    # tts_keys_sorted: list[int] = sorted(inputs.tts_audio_files.keys()) if inputs.tts_audio_files else []

    # filter_script = _build_filtergraph(inputs, num_clip_inputs=len(inputs.clips), tts_keys_sorted=tts_keys_sorted)
    # filter_path = inputs.output_path.with_suffix(".filter.txt")
    # filter_path.write_text(filter_script, encoding="utf-8")

    # ffmpeg_cmd = find_ffmpeg_command("ffmpeg")

    # # output_dir 기준 상대경로로 실행 (ass 필터 안정화)
    # output_relative = _relpath_or_abs(inputs.output_path, output_dir)

    # def _build_input_args(hwaccel: str | None) -> list[str]:
    #     args: list[str] = []
    #     # 원본 컨테이너에 data/subtitle stream이 섞여 있어도 무시 (타임스탬프/길이 이상 방지)
    #     args.extend(["-dn", "-sn"])
    #     # 클립별 입력: -ss/-to를 입력 앞에 두어(입력 seeking) 속도를 우선합니다.
    #     for clip in inputs.clips:
    #         if hwaccel:
    #             args.extend(["-hwaccel", hwaccel])
    #         args.extend(
    #             [
    #                 "-ss",
    #                 f"{clip.start_sec}",
    #                 "-to",
    #                 f"{clip.end_sec}",
    #                 "-i",
    #                 str(_relpath_or_abs(inputs.video_path, output_dir)),
    #             ]
    #         )
    #     # TTS 오디오 파일들을 입력으로 추가 (입력 N..N+M-1)
    #     if inputs.tts_audio_files:
    #         for clip_idx in tts_keys_sorted:
    #             tts_path = inputs.tts_audio_files[clip_idx]
    #             args.extend(["-i", str(_relpath_or_abs(tts_path, output_dir))])
    #     return args

    # # GPU 인코더는 환경(드라이버) 따라 실패할 수 있으므로,
    # # 1) GPU 1개만 선택해서 시도
    # # 2) 실패 시 CPU(libx264)로 폴백
    # preferred = _pick_video_encoder(ffmpeg_cmd)
    # candidates = [preferred] if preferred != "libx264" else ["libx264"]
    # if "libx264" not in candidates:
    #     candidates.append("libx264")

    # last_err: Exception | None = None
    # hwaccel_candidates: list[str | None] = [None]
    # if inputs.enable_hwaccel:
    #     # Windows에서 흔히 성공하는 순서: d3d11va → cuda → none
    #     hwaccel_candidates = ["d3d11va", "cuda", None]

    # for video_encoder in candidates:
    #     encoder_args = _video_encoder_args(video_encoder, inputs.render_preset)
    #     for hwaccel in hwaccel_candidates:
    #         cmd_try: list[str] = [
    #             ffmpeg_cmd,
    #             "-y",
    #             *_build_input_args(hwaccel),
    #             "-r", 24,
    #             "-filter_complex",
    #             filter_script,
    #             "-map",
    #             "[vout]",
    #             "-map",
    #             "[aout]",
    #             "-c:v",
    #             video_encoder,
    #             *encoder_args,
    #             "-pix_fmt",
    #             "yuv420p",
    #             "-c:a",
    #             "aac",
    #             str(output_relative),
    #         ]
    #         try:
    #             subprocess.check_call(cmd_try, cwd=str(output_dir))
    #             return cmd_try
    #         except subprocess.CalledProcessError as e:
    #             last_err = e
    #             continue

    # raise RuntimeError(f"렌더링 실패: 사용 가능한 인코더로 출력하지 못했습니다. 마지막 오류: {last_err}")



def render_short(inputs: RenderInputs) -> list[str]:
    output_dir = inputs.output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # [수정] 제목 가독성을 위한 자동 줄바꿈 로직
    def _smart_wrap(text: str, max_chars: int = 14) -> str:
        """한 줄에 약 14자 내외로 끊어서 가독성 있게 줄바꿈"""
        if len(text) <= max_chars:
            return text
        words = text.split()
        lines = []
        current_line = ""
        for word in words:
            if len(current_line) + len(word) <= max_chars:
                current_line += (word + " ")
            else:
                lines.append(current_line.strip())
                current_line = word + " "
        lines.append(current_line.strip())
        return "\n".join(lines)

    title_font = 65  # 바이럴을 위해 폰트 크기 키움
    title_file = output_dir / "title.txt"
    work_file = output_dir / "work_title.txt"
    
    # 가독성 있게 잘린 제목 생성
    wrapped_title = _smart_wrap(inputs.title_text)
    title_file.write_bytes(wrapped_title.encode("utf-8-sig"))
    work_file.write_bytes(f"#{inputs.work_title.replace(' ', '')}".encode("utf-8-sig"))

    inputs = replace(inputs, title_textfile=title_file, work_title_textfile=work_file)
    tts_keys_sorted = sorted(inputs.tts_audio_files.keys()) if inputs.tts_audio_files else []
    
    filter_script = _build_filtergraph(inputs, len(inputs.clips), tts_keys_sorted)
    filter_path = inputs.output_path.with_suffix(".filter.txt")
    filter_path.write_text(filter_script, encoding="utf-8")

    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    output_relative = inputs.output_path.relative_to(output_dir)

    # 렌더링 실행 (기본 로직 유지)
    video_encoder = _pick_video_encoder(ffmpeg_cmd)
    encoder_args = _video_encoder_args(video_encoder, inputs.render_preset)
    
    cmd = [
        ffmpeg_cmd, "-y",
        *["-i", str(inputs.video_path.relative_to(output_dir))] * len(inputs.clips),
        *(["-i", str(p.relative_to(output_dir))] for p in inputs.tts_audio_files.values() if inputs.tts_audio_files),
        "-filter_complex", filter_script,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", video_encoder, *encoder_args,
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(output_relative)
    ]
    # 실제 실행 시에는 리스트를 평탄화하여 실행
    flat_cmd = []
    for item in cmd:
        if isinstance(item, list): flat_cmd.extend(item)
        else: flat_cmd.append(str(item))
        
    subprocess.check_call(flat_cmd, cwd=str(output_dir))
    return flat_cmd

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
    
    # 720p 쇼츠에 적합한 타겟 비트레이트 설정 (약 2500k~3000k)
    target_bitrate = "2500k"
    max_bitrate = "3000k"
    buf_size = "5000k"

    if encoder == "h264_nvenc":
        if preset == "fastest":
            return ["-preset", "p1", "-rc", "vbr", "-cq", "28", "-b:v", target_bitrate, "-maxrate", max_bitrate, "-bufsize", buf_size]
        if preset == "quality":
            return ["-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", target_bitrate, "-maxrate", max_bitrate, "-bufsize", buf_size]
        # balanced (기존 -b:v 0 제거)
        return ["-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", target_bitrate, "-maxrate", max_bitrate, "-bufsize", buf_size]

    # libx264 (CPU 인코딩) - 프리셋을 더 빠른 쪽으로 조정
    if preset == "fastest":
        return ["-preset", "ultrafast", "-crf", "28", "-b:v", target_bitrate]
    if preset == "quality":
        return ["-preset", "faster", "-crf", "20", "-b:v", target_bitrate]
    # balanced (superfast -> ultrafast로 변경 권장)
    return ["-preset", "ultrafast", "-crf", "23", "-b:v", target_bitrate]


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
    # """
    # 요구사항 반영:
    # - 중앙 영상은 '원본 비율 유지'로 통째로 보이게 배치(필요 시 다운스케일만, 업스케일 X)
    # - 남는 영역은 검은색
    # - 제목/작품명은 중앙 영상 바로 위/아래에 작은 마진으로 배치
    # - 자막은 optional (subtitle_path가 있을 때만 ass 적용)
    # """
    # W = inputs.canvas_width
    # H = inputs.canvas_height

    # src_w, src_h = _probe_video_dims(inputs.video_path)

    # # 텍스트/마진(고정) — 요청대로 "조금만"
    # margin = 20
    # title_font = 54
    # work_font = 44
    # font_name = "Malgun Gothic"

    # # 텍스트 영역을 확보하기 위해 상/하 여유를 잡고 그 안에서 영상이 '통째로' 들어가게 fit
    # # (업스케일은 하지 않음)
    # reserve_h = int(title_font * 1.6) + int(work_font * 1.6) + margin * 4
    # avail_h = max(1, H - reserve_h)
    # scale = min(W / src_w, avail_h / src_h, 1.0)

    # scaled_w = int(src_w * scale)
    # scaled_h = int(src_h * scale)
    # # x264 안정성을 위해 짝수로
    # scaled_w -= scaled_w % 2
    # scaled_h -= scaled_h % 2
    # scaled_w = max(2, scaled_w)
    # scaled_h = max(2, scaled_h)

    # overlay_x = int((W - scaled_w) / 2)
    # overlay_y = int((H - scaled_h) / 2)

    # escaped_title = _escape_text_for_drawtext(inputs.title_text)
    # escaped_work_title = _escape_text_for_drawtext(f"작품명: {inputs.work_title}")

    # # 자막 경로(있으면 상대경로 우선)
    # output_dir = inputs.output_path.parent
    # subtitle_path_escaped: str | None = None
    # if inputs.subtitle_path:
    #     try:
    #         subtitle_path_relative = inputs.subtitle_path.relative_to(output_dir)
    #         subtitle_path_escaped = str(subtitle_path_relative).replace("\\", "/")
    #     except ValueError:
    #         subtitle_path_escaped = str(inputs.subtitle_path.resolve()).replace("\\", "/")

    # # 제목/작품명 y 위치: 영상 경계 기준
    # # 제목: 영상 위쪽 경계 - margin - text_h
    # title_y_expr = f"{overlay_y - margin}-text_h"
    # # 작품명: 영상 아래쪽 경계 + margin
    # work_y = overlay_y + scaled_h + margin

    # # drawtext는 textfile을 우선 사용 (한글/따옴표 안정성)
    # # 여러 줄 제목은 각 줄마다 별도 drawtext 필터 생성 (줄바꿈 지원, "..." 생략 금지)
    # title_draw_parts: list[str] = []
    # last_output_label: str | None = None
    
    # if inputs.title_textfile:
    #     title_path = _relpath_or_abs(inputs.title_textfile, output_dir)
    #     # 파일에서 줄바꿈된 텍스트 읽기
    #     title_lines = inputs.title_textfile.read_text(encoding="utf-8-sig").strip().split("\n")
    #     current_input = "[vcat]"
    #     line_idx = 0
    #     for line in title_lines:
    #         if not line.strip():
    #             continue
    #         escaped_line = _escape_text_for_drawtext(line.strip())
    #         line_y_offset = line_idx * (title_font + 10)  # 줄 간격
    #         output_label = f"[title_line{line_idx}]"
    #         title_draw_parts.append(
    #             f"{current_input}drawtext=font='{font_name}':fontcolor=white:fontsize={title_font}:"
    #             f"text='{escaped_line}':x=(w-text_w)/2:y={title_y_expr}+{line_y_offset}{output_label}"
    #         )
    #         current_input = output_label
    #         last_output_label = output_label
    #         line_idx += 1
        
    #     if not title_draw_parts:
    #         title_draw = "[vcat]null[with_title]"
    #     else:
    #         # 마지막 출력을 with_title로 변경
    #         title_draw = ";".join(title_draw_parts).replace(last_output_label, "[with_title]")
    # else:
    #     # textfile 없으면 직접 텍스트 사용 (줄바꿈 처리)
    #     title_lines = inputs.title_text.split("\n")
    #     current_input = "[vcat]"
    #     line_idx = 0
    #     for line in title_lines:
    #         if not line.strip():
    #             continue
    #         escaped_line = _escape_text_for_drawtext(line.strip())
    #         line_y_offset = line_idx * (title_font + 10)
    #         output_label = f"[title_line{line_idx}]"
    #         title_draw_parts.append(
    #             f"{current_input}drawtext=font='{font_name}':fontcolor=white:fontsize={title_font}:"
    #             f"text='{escaped_line}':x=(w-text_w)/2:y={title_y_expr}+{line_y_offset}{output_label}"
    #         )
    #         current_input = output_label
    #         last_output_label = output_label
    #         line_idx += 1
        
    #     if not title_draw_parts:
    #         title_draw = "[vcat]null[with_title]"
    #     else:
    #         title_draw = ";".join(title_draw_parts).replace(last_output_label, "[with_title]")

    # if inputs.work_title_textfile:
    #     work_path = _relpath_or_abs(inputs.work_title_textfile, output_dir)
    #     work_draw = (
    #         f"[with_title]drawtext=font='{font_name}':fontcolor=white:fontsize={work_font}:"
    #         f"textfile='{str(work_path).replace('\\\\', '/')}':reload=0:x=(w-text_w)/2:y={work_y}[with_work]"
    #     )
    # else:
    #     work_draw = (
    #         f"[with_title]drawtext=font='{font_name}':fontcolor=white:fontsize={work_font}:"
    #         f"text='{escaped_work_title}':x=(w-text_w)/2:y={work_y}[with_work]"
    #     )

    # # 각 입력(클립)마다 scale+pad로 캔버스(검정)까지 완성 → concat filter로 이어붙임
    # filters: list[str] = []
    # for i in range(num_clip_inputs):
    #     filters.append(
    #         f"[{i}:v]scale={scaled_w}:{scaled_h},"
    #         f"pad={W}:{H}:{overlay_x}:{overlay_y}:black[v{i}]"
    #     )
    #     filters.append(f"[{i}:a]anull[a{i}]")

    # # concat filter: [v0][a0][v1][a1]...concat=n=..:v=1:a=1[vcat][acat]
    # concat_inputs: list[str] = []
    # for i in range(num_clip_inputs):
    #     concat_inputs.append(f"[v{i}]")
    #     concat_inputs.append(f"[a{i}]")
    # filters.append(
    #     f"{''.join(concat_inputs)}concat=n={num_clip_inputs}:v=1:a=1[vcat][acat]"
    # )

    # filters.extend([title_draw, work_draw])

    # if subtitle_path_escaped:
    #     filters.append(f"[with_work]ass='{subtitle_path_escaped}'[vout]")
    # else:
    #     # 자막 없으면 identity
    #     filters.append("[with_work]null[vout]")

    # filters.append(_build_audio_filter(inputs, num_clip_inputs=num_clip_inputs, tts_keys_sorted=tts_keys_sorted))
    # return ";".join(filters)





# 
def _build_filtergraph(inputs: RenderInputs, num_clip_inputs: int, tts_keys_sorted: list[int]) -> str:
    W, H = inputs.canvas_width, inputs.canvas_height
    # 쇼츠 최적화: 배경은 블러 처리된 영상, 중앙에 원본 비율 영상 배치
    filters = []
    for i in range(num_clip_inputs):
        # 배경 (블러) + 중앙 정렬 영상 합성
        filters.append(
            f"[{i}:v]scale={W}:-1,boxblur=20:10,crop={W}:{H}[bg{i}];"
            f"[{i}:v]scale={W}:-1[fg{i}];"
            f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2[v{i}]"
        )
        filters.append(f"[{i}:a]anull[a{i}]")

    concat_str = "".join(f"[v{i}][a{i}]" for i in range(num_clip_inputs))
    filters.append(f"{concat_str}concat=n={num_clip_inputs}:v=1:a=1[vcat][acat]")

    # 제목 디자인 (노란색 강조, 검정 테두리 - 바이럴 스타일)
    title_style = "font='Malgun Gothic':fontcolor=yellow:fontsize=70:borderw=4:bordercolor=black"
    filters.append(f"[vcat]drawtext={title_style}:textfile='title.txt':x=(w-text_w)/2:y=(h-text_h)/4[vtitle]")
    
    # 해시태그/작품명 디자인
    work_style = "font='Malgun Gothic':fontcolor=white:fontsize=40:borderw=2:bordercolor=black"
    filters.append(f"[vtitle]drawtext={work_style}:textfile='work_title.txt':x=(w-text_w)/2:y=h-200[vwork]")

    if inputs.subtitle_path:
        sub_path = str(inputs.subtitle_path.relative_to(inputs.output_path.parent)).replace("\\", "/")
        filters.append(f"[vwork]ass='{sub_path}'[vout]")
    else:
        filters.append("[vwork]null[vout]")

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

