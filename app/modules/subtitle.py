from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.modules.speech import SpeechSegment
from app.modules.story_builder import StoryClip
from app.config import DesignConfig


def parse_srt(srt_path: Path) -> list[SpeechSegment]:
    """SRT 파일을 SpeechSegment 리스트로 변환합니다.

    SRT 형식:
        1
        00:00:01,000 --> 00:00:04,000
        자막 텍스트

    Returns:
        SpeechSegment 리스트 (start_sec, end_sec, text)
    """
    def _ts_to_sec(ts: str) -> float:
        # HH:MM:SS,mmm 또는 HH:MM:SS.mmm
        ts = ts.replace(",", ".")
        h, m, rest = ts.split(":")
        s, ms = rest.split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    text = srt_path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n{2,}", text.strip())

    segments: list[SpeechSegment] = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        # 첫 줄: 인덱스 번호 (skip), 두 번째 줄: 타임스탬프
        try:
            ts_line = lines[1]
            start_str, end_str = ts_line.split("-->")
            start_sec = _ts_to_sec(start_str.strip())
            end_sec = _ts_to_sec(end_str.strip())
        except (ValueError, IndexError):
            continue

        # 나머지 줄: 자막 텍스트 (HTML 태그 제거 후 합침)
        raw_text = " ".join(lines[2:])
        clean_text = re.sub(r"<[^>]+>", "", raw_text).strip()
        # 대사 앞 (이름) → "이름: 대사" 변환 (Gemini 발화자 인식용)
        speaker_match = re.match(r"^\(([^)]+)\)\s*", clean_text)
        if speaker_match:
            speaker = speaker_match.group(1)
            dialogue = clean_text[speaker_match.end():]
            clean_text = f"{speaker}: {dialogue}".strip()
        else:
            # 앞에 없는 괄호는 효과음으로 간주하여 제거
            clean_text = re.sub(r"\([^)]*\)", "", clean_text).strip()
        # 대괄호(음악/효과음 표기)는 항상 제거
        clean_text = re.sub(r"\[[^\]]*\]", "", clean_text).strip()
        if not clean_text:
            continue

        segments.append(SpeechSegment(start_sec=start_sec, end_sec=end_sec, text=clean_text))

    return segments


@dataclass(frozen=True)
class SubtitleStyle:
    font_name: str = "Malgun Gothic"  # Windows 기본 한글 폰트
    font_size: int = 52
    primary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    outline: int = 2
    shadow: int = 0
    margin_v: int = 480


def build_ass(
    clips: list[StoryClip],
    output_path: Path,
    style: SubtitleStyle,
    original_subtitles: list[SpeechSegment] | None = None,
) -> None:
    """ASS 자막 파일을 생성합니다.
    
    Args:
        clips: StoryClip 리스트 (편집된 자막)
        output_path: 출력 파일 경로
        style: 자막 스타일
        original_subtitles: 원본 음성 자막 (선택사항)
    """
    header = _ass_header(style)
    
    # StoryClip 자막 이벤트
    clip_events_list = []
    current_timeline_sec = 0.0
    
    for idx, clip in enumerate(clips):
        clip_dur = clip.end_sec - clip.start_sec
        
        # 쇼츠의 0초부터 시작하는 상대 시간으로 변환
        start = _format_time(current_timeline_sec)
        end = _format_time(current_timeline_sec + clip_dur)
        text = clip.subtitle.replace("\n", " ")
        
        line = f"Dialogue: 0,{start},{end},Default,,,,,, {text}\n"
        clip_events_list.append(line)
        
        current_timeline_sec += clip_dur # 다음 클립을 위해 시간 누적
        
    clip_events = "".join(clip_events_list)
    # 원본 음성 자막 이벤트 (있는 경우)
    original_events = ""
 
    
    # 이벤트 합치기
    events = clip_events
    if original_subtitles:
        # original_subtitles는 이미합니다.
        original_events = "\n".join(
            _ass_line_original(seg, style) for seg in original_subtitles
        )
    
    # 출력 디렉토리가 존재하는지 확인하고 생성
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 BOM 추가하여 한글 깨짐 방지
    content = header + events
    output_path.write_bytes(content.encode("utf-8-sig"))  # UTF-8 BOM

def build_ass_from_segments(
    segments: list[SpeechSegment],
    output_path: Path,
    style: SubtitleStyle,
    tts_segments: list[SpeechSegment] | None = None,
    tts_style: SubtitleStyle | None = None,
) -> None:
    """ASS 자막 파일을 생성합니다(전사/세그먼트 기반)."""
    header = _ass_header(style, tts_style)
    
    clip_events_list = []
    
    print("\n" + "="*50)
    print(f"DEBUG: [segments 기반] 자막 생성 시작 (총 {len(segments)}개)")

    for idx, seg in enumerate(segments):
        start_str = _format_time(seg.start_sec)
        end_str = _format_time(seg.end_sec)
        
        # 1. 텍스트 정리 및 줄바꿈 처리
        raw_text = seg.text.replace("\n", " ").strip()
        
        # 15자 기준 자동 줄바꿈
        if len(raw_text) > 15:
            words = raw_text.split()
            mid = len(words) // 2
            text = " ".join(words[:mid]) + r"\N" + " ".join(words[mid:])
        else:
            text = raw_text
            
        # 2. 터미널 출력 (이제 정상적으로 찍힐 겁니다)
        print(f"[{idx+1}] {start_str} ~ {end_str} | {text}")
        
        # 3. 라인 생성 (쉼표 6개 구조: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text)
        line = f"Dialogue: 0,{start_str},{end_str},Default,,,,,, {text}\n"
        clip_events_list.append(line)

    events = "".join(clip_events_list)

    # TTS 자막 이벤트 (TtsLine 스타일)
    if tts_segments and tts_style:
        for seg in tts_segments:
            start_str = _format_time(seg.start_sec)
            end_str = _format_time(seg.end_sec)
            text = seg.text.replace("\n", " ").strip()
            if len(text) > 15:
                words = text.split()
                mid = len(words) // 2
                text = " ".join(words[:mid]) + r"\N" + " ".join(words[mid:])
            events += f"Dialogue: 0,{start_str},{end_str},TtsLine,,,,,, {text}\n"

    # 4. 파일 저장
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = header + events
    output_path.write_bytes(content.encode("utf-8-sig"))
    
    print(f"DEBUG: 자막 저장 완료 -> {output_path}")
    print("="*50 + "\n")


def build_tts_ass(
    tts_segments: list[SpeechSegment],
    output_path: Path,
    style: SubtitleStyle,
) -> None:
    """TTS 자막만 담은 ASS 파일을 생성합니다."""
    header = _ass_header(style)
    events = ""
    for seg in tts_segments:
        start_str = _format_time(seg.start_sec)
        end_str = _format_time(seg.end_sec)
        text = seg.text.replace("\n", " ").strip()
        if len(text) > 15:
            words = text.split()
            mid = len(words) // 2
            text = " ".join(words[:mid]) + r"\N" + " ".join(words[mid:])
        events += f"Dialogue: 0,{start_str},{end_str},Default,,,,,, {text}\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes((header + events).encode("utf-8-sig"))


def remap_transcript_to_edited_timeline(
    clips: list[StoryClip],
    transcript_segments: list[SpeechSegment],
    *,
    tts_only_when_no_orig: bool = True,
) -> list[SpeechSegment]:
    """원본 타임라인 전사를 편집본 타임라인으로 재매핑합니다.

    - clip.use_original_audio=True: (clip.start_sec~clip.end_sec)와 겹치는 전사 세그먼트를 클리핑 후, 누적 offset만큼 이동
    - clip.use_original_audio=False: 전사 대신 TTS 문장(없으면 clip.subtitle)을 clip 전체 구간에 1개 이벤트로 생성
    """
    out: list[SpeechSegment] = []
    t = 0.0

    # 전사 세그먼트는 보통 시간순이지만, 안전하게 정렬
    transcript_sorted = sorted(transcript_segments, key=lambda s: (s.start_sec, s.end_sec))

    for clip in clips:
        clip_dur = max(0.0, clip.end_sec - clip.start_sec)
        if clip_dur <= 0:
            continue

        if clip.use_original_audio:
            for seg in transcript_sorted:
                if seg.end_sec <= clip.start_sec:
                    continue
                if seg.start_sec >= clip.end_sec:
                    break
                s = max(seg.start_sec, clip.start_sec)
                e = min(seg.end_sec, clip.end_sec)
                if e - s < 0.05:
                    continue
                out.append(
                    SpeechSegment(
                        start_sec=(s - clip.start_sec) + t,
                        end_sec=(e - clip.start_sec) + t,
                        text=seg.text.strip(),
                    )
                )
        else:
            if tts_only_when_no_orig:
                text = (clip.tts_line or "").strip() or (clip.subtitle or "").strip()
                if text:
                    out.append(SpeechSegment(start_sec=t, end_sec=t + clip_dur, text=text))

        t += clip_dur

    return out


def merge_subtitle_segments(
    segments: list[SpeechSegment],
    *,
    max_gap_sec: float = 0.25,
    max_total_chars: int = 44,
    max_duration_sec: float = 6.0,
    min_duration_sec: float = 0.6,
) -> list[SpeechSegment]:
    """자막 이벤트 수를 줄여(렌더 부담 감소) 가독성을 개선합니다."""
    if not segments:
        return []

    segs = sorted(segments, key=lambda s: (s.start_sec, s.end_sec))
    merged: list[SpeechSegment] = []

    cur = SpeechSegment(
        start_sec=segs[0].start_sec,
        end_sec=segs[0].end_sec,
        text=segs[0].text.replace("\n", " ").strip(),
    )

    for seg in segs[1:]:
        seg_text = seg.text.replace("\n", " ").strip()
        if not seg_text:
            continue

        gap = max(0.0, seg.start_sec - cur.end_sec)
        combined_text = (cur.text + " " + seg_text).strip()
        combined_dur = max(seg.end_sec, cur.end_sec) - cur.start_sec

        should_merge = (
            gap <= max_gap_sec
            and len(combined_text) <= max_total_chars
            and combined_dur <= max_duration_sec
        )
        # 너무 짧은 자막은 되도록 합치기(가독성/렌더 부담 개선)
        if (cur.end_sec - cur.start_sec) < min_duration_sec and gap <= max_gap_sec:
            should_merge = should_merge or len(combined_text) <= max_total_chars

        if should_merge:
            cur = SpeechSegment(
                start_sec=cur.start_sec,
                end_sec=max(cur.end_sec, seg.end_sec),
                text=combined_text,
            )
        else:
            merged.append(cur)
            cur = SpeechSegment(
                start_sec=seg.start_sec,
                end_sec=seg.end_sec,
                text=seg_text,
            )

    merged.append(cur)
    return merged


def _ass_header(style: SubtitleStyle, tts_style: SubtitleStyle | None = None) -> str:
    from app.config import FONT_NAME_MAP

    margin_v = style.margin_v if style.margin_v >= 0 else 480
    alignment = 2  # 2 = 하단 중앙
    font_name = FONT_NAME_MAP.get(style.font_name, style.font_name)

    styles_block = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{style.font_size},{style.primary_color},{style.outline_color},0,0,1,{style.outline},{style.shadow},{alignment},80,80,{margin_v},0\n"
    )

    if tts_style:
        tts_font = FONT_NAME_MAP.get(tts_style.font_name, tts_style.font_name)
        tts_margin_v = tts_style.margin_v if tts_style.margin_v >= 0 else 300
        styles_block += (
            f"Style: TtsLine,{tts_font},{tts_style.font_size},{tts_style.primary_color},{tts_style.outline_color},0,0,1,{tts_style.outline},{tts_style.shadow},{alignment},80,80,{tts_margin_v},0\n"
        )

    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        + styles_block +
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _ass_line(index: int, clip: StoryClip, style: SubtitleStyle) -> str:
    start = _format_time(clip.start_sec)
    end = _format_time(clip.end_sec)
    text = clip.subtitle.replace("\n", " ")
    return f"Dialogue: 0,{start},{end},Default,,,,,, {text}\n"


def _ass_line_original(segment: SpeechSegment, style: SubtitleStyle) -> str:
    """원본 음성 자막 라인 생성 (레이어 1 사용)"""
    start = _format_time(segment.start_sec)
    end = _format_time(segment.end_sec)
    text = segment.text.replace("\n", " ")
    # 원본 자막은 레이어 1에 배치하고 약간 다른 스타일 적용 가능
    return f"Dialogue: 1,{start},{end},Default,,,,,, {text}\n"


def _ass_line_segment(segment: SpeechSegment) -> str:
    start = _format_time(segment.start_sec)
    end = _format_time(segment.end_sec)
    text = segment.text.replace("\n", " ")
    return f"Dialogue: 0,{start},{end},Default,,,,,, {text}\n"


def _format_time(seconds: float) -> str:
    total_ms = int(seconds * 1000)
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    s = total_sec % 60
    total_min = total_sec // 60
    m = total_min % 60
    h = total_min // 60
    return f"{h:d}:{m:02d}:{s:02d}.{ms // 10:02d}"