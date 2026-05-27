from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.modules.speech import SpeechSegment
from app.modules.story_builder import StoryClip
from app.config import DesignConfig


def _split_text_to_lines(text: str, max_chars: int = 15) -> list[str]:
    """긴 텍스트를 max_chars 단위로 어절 경계에서 분할합니다."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip() if current else word
    if current:
        lines.append(current)
    return lines if lines else [text]


# 화자 prefix(예: "의주: ", "Soyeon: ") — ASS 출력 직전에 제거.
# parse_srt 단계의 "(이름) → 이름:" 변환은 Gemini 발화자 인식용으로 유지하되, 화면에는 노출되지 않게 한다.
# merge_subtitle_segments 로 여러 cue 가 합쳐지면 텍스트 중간에 다른 화자 표기가 끼어들 수 있어
# "문장 시작" 또는 "공백 직후" 의 짧은 단어(<=5자) + 콜론 패턴은 모두 화자로 간주해 제거한다.
# 길이를 5자로 제한해 일반 한국어 문장(8자 이상 어절 + 콜론)을 잘못 제거하지 않게 한다.
_SPEAKER_PREFIX_RE = re.compile(
    r"(?:^|(?<=\s))(?:[가-힣][가-힣0-9]{0,4}|[A-Za-z][A-Za-z0-9]{0,9})\s*[:：]\s*"
)


def _strip_speaker_prefix(text: str) -> str:
    if not text:
        return text
    return _SPEAKER_PREFIX_RE.sub("", text)


def _wrap_for_ass(text: str, max_chars: int = 15, max_lines: int = 2) -> str:
    """텍스트를 max_chars 단위로 분할한 뒤 ASS \\N 으로 한 화면에 묶는다.

    줄 수가 max_lines 를 넘으면 max_chars 한도를 늘려 균등 재분할(첫 max_lines 줄만 사용).
    이렇게 하면 한 segment 가 시간 균등 분할로 깜빡이지 않고 통째로 한 화면에 표시된다.
    """
    lines = _split_text_to_lines(text, max_chars=max_chars)
    if len(lines) > max_lines:
        # 균등하게 max_lines 줄로 다시 자름
        rebalanced_chars = max(max_chars, (len(text) + max_lines - 1) // max_lines + 2)
        lines = _split_text_to_lines(text, max_chars=rebalanced_chars)[:max_lines]
    return "\\N".join(lines)


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

        # 나머지 줄: 자막 텍스트
        # 라운드 7-C: HTML 태그(<i>, <b>) + ASS 위치/스타일 태그({\an2}, {\b1} 등) 모두 제거.
        # parse_ass()는 이미 동일 패턴 사용 — SRT 안에 ASS 태그가 있어도 일관 처리.
        raw_text = " ".join(lines[2:])
        clean_text = re.sub(r"<[^>]+>|\{[^}]*\}", "", raw_text).strip()
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


def parse_ass(ass_path: Path) -> list[SpeechSegment]:
    """ASS/SSA 자막 파일을 SpeechSegment 리스트로 변환합니다."""
    def _ass_ts_to_sec(ts: str) -> float:
        # H:MM:SS.cc (centiseconds)
        h, m, rest = ts.strip().split(":")
        s, cs = rest.split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100

    text = ass_path.read_text(encoding="utf-8-sig")
    segments: list[SpeechSegment] = []
    in_events = False

    for line in text.splitlines():
        if line.strip().lower() == "[events]":
            in_events = True
            continue
        if line.strip().startswith("[") and in_events:
            break
        if not in_events or not line.startswith("Dialogue:"):
            continue

        # Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        try:
            start_sec = _ass_ts_to_sec(parts[1])
            end_sec = _ass_ts_to_sec(parts[2])
        except (ValueError, IndexError):
            continue

        raw_text = parts[9]
        # ASS 태그 제거: {\tag}
        clean = re.sub(r"\{[^}]*\}", "", raw_text)
        # \N, \n → 공백
        clean = clean.replace("\\N", " ").replace("\\n", " ").strip()
        if not clean:
            continue
        segments.append(SpeechSegment(start_sec=start_sec, end_sec=end_sec, text=clean))

    return segments


def parse_vtt(vtt_path: Path) -> list[SpeechSegment]:
    """WebVTT 자막 파일을 SpeechSegment 리스트로 변환합니다."""
    def _vtt_ts_to_sec(ts: str) -> float:
        ts = ts.strip()
        parts = ts.split(":")
        if len(parts) == 3:
            h, m, rest = parts
        else:
            h = "0"
            m, rest = parts
        s, ms = rest.split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    text = vtt_path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n{2,}", text.strip())
    segments: list[SpeechSegment] = []

    for block in blocks:
        lines = block.strip().splitlines()
        # 타임스탬프 라인 찾기
        ts_idx = -1
        for i, l in enumerate(lines):
            if "-->" in l:
                ts_idx = i
                break
        if ts_idx < 0:
            continue

        try:
            start_str, end_str = lines[ts_idx].split("-->")
            # position/alignment 메타 제거
            end_str = end_str.split()[0] if end_str.strip() else end_str
            start_sec = _vtt_ts_to_sec(start_str)
            end_sec = _vtt_ts_to_sec(end_str)
        except (ValueError, IndexError):
            continue

        raw_text = " ".join(lines[ts_idx + 1:])
        # <v Speaker> 태그에서 화자 추출
        speaker_match = re.match(r"<v\s+([^>]+)>", raw_text)
        if speaker_match:
            speaker = speaker_match.group(1)
            dialogue = re.sub(r"</?v[^>]*>|\{[^}]*\}", "", raw_text).strip()
            clean = f"{speaker}: {dialogue}"
        else:
            # 라운드 7-C: HTML 태그 + ASS 중괄호 태그 모두 제거
            clean = re.sub(r"<[^>]+>|\{[^}]*\}", "", raw_text).strip()
        if not clean:
            continue
        segments.append(SpeechSegment(start_sec=start_sec, end_sec=end_sec, text=clean))

    return segments


def parse_smi(smi_path: Path) -> list[SpeechSegment]:
    """SAMI(SMI) 자막 파일을 SpeechSegment 리스트로 변환합니다."""
    text = smi_path.read_text(encoding="utf-8-sig")

    # <SYNC Start=밀리초> 블록 추출
    sync_pattern = re.compile(
        r"<SYNC\s+Start=(\d+)>(.*?)(?=<SYNC|</BODY>|$)",
        re.IGNORECASE | re.DOTALL,
    )
    matches = list(sync_pattern.finditer(text))
    segments: list[SpeechSegment] = []

    for i, m in enumerate(matches):
        start_ms = int(m.group(1))
        content = m.group(2).strip()
        # 다음 SYNC의 시작 시간을 end로 사용
        end_ms = int(matches[i + 1].group(1)) if i + 1 < len(matches) else start_ms + 3000

        # HTML 태그 제거
        clean = re.sub(r"<[^>]+>", "", content)
        clean = clean.replace("&nbsp;", " ").strip()
        if not clean or clean == "\xa0":
            continue

        # <P Class=Speaker> 에서 화자 추출
        class_match = re.search(r'<P\s+Class=(["\']?)(\w+)\1', content, re.IGNORECASE)
        if class_match:
            speaker = class_match.group(2)
            if speaker.lower() not in ("krcc", "encc", "default"):
                clean = f"{speaker}: {clean}"

        segments.append(SpeechSegment(
            start_sec=start_ms / 1000,
            end_sec=end_ms / 1000,
            text=clean,
        ))

    return segments


def parse_subtitle(path: Path) -> list[SpeechSegment]:
    """파일 확장자를 기반으로 적절한 자막 파서를 선택합니다.

    지원 형식: SRT, ASS/SSA, VTT, SMI
    """
    ext = path.suffix.lower()
    if ext == ".srt":
        return parse_srt(path)
    elif ext in (".ass", ".ssa"):
        return parse_ass(path)
    elif ext == ".vtt":
        return parse_vtt(path)
    elif ext in (".smi", ".sami"):
        return parse_smi(path)
    else:
        raise ValueError(f"지원하지 않는 자막 형식: {ext} (지원: .srt, .ass, .ssa, .vtt, .smi)")


@dataclass(frozen=True)
class SubtitleStyle:
    font_name: str = "Malgun Gothic"  # Windows 기본 한글 폰트
    font_size: int = 52
    primary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    outline: int = 2
    shadow: int = 0
    margin_v: int = 480
    # 라운드 10 — 장르별 자막 프리셋용 신규 필드
    bold: bool = False
    alignment: int = 2          # ASS Alignment (1=좌하, 2=중하 기본, 3=우하, 5/6/7=상단)
    border_style: int = 1       # 1=outline+drop shadow, 3=opaque box
    back_color: str = "&H00000000"  # BorderStyle=3 박스 배경색


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
    tts_time_ranges: list[tuple[float, float]] | None = None,
) -> None:
    """ASS 자막 파일을 생성합니다(전사/세그먼트 기반)."""
    header = _ass_header(style, tts_style)
    
    clip_events_list = []
    
    print("\n" + "="*50)
    print(f"DEBUG: [segments 기반] 자막 생성 시작 (총 {len(segments)}개)")

    for idx, seg in enumerate(segments):
        start_str = _format_time(seg.start_sec)
        end_str = _format_time(seg.end_sec)

        # 1. 텍스트 정리 (화자 prefix 제거 + 줄바꿈 → 공백)
        raw_text = _strip_speaker_prefix(seg.text.replace("\n", " ").strip())
        if not raw_text:
            continue
        text = raw_text

        # 2. 터미널 출력
        print(f"[{idx+1}] {start_str} ~ {end_str} | {text}")

        # 3. 한 segment = 한 화면. \N 으로 묶어 시간 균등 분할 깜빡임 제거.
        joined = _wrap_for_ass(text, max_chars=15, max_lines=2)
        clip_events_list.append(
            f"Dialogue: 0,{start_str},{end_str},Default,,,,,, {joined}\n"
        )

    events = "".join(clip_events_list)

    # TTS 자막 이벤트 (TtsLine 스타일) — 동시 표시 적용
    if tts_segments and tts_style:
        for seg in tts_segments:
            text = _strip_speaker_prefix(seg.text.replace("\n", " ").strip())
            if not text:
                continue
            joined = _wrap_for_ass(text, max_chars=15, max_lines=2)
            events += f"Dialogue: 0,{_format_time(seg.start_sec)},{_format_time(seg.end_sec)},TtsLine,,,,,, {joined}\n"

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
        text = _strip_speaker_prefix(seg.text.replace("\n", " ").strip())
        if not text:
            continue
        joined = _wrap_for_ass(text, max_chars=15, max_lines=2)
        events += f"Dialogue: 0,{_format_time(seg.start_sec)},{_format_time(seg.end_sec)},Default,,,,,, {joined}\n"
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
                text = (clip.subtitle or "").strip()
                if text:
                    out.append(SpeechSegment(start_sec=t, end_sec=t + clip_dur, text=text))

        t += clip_dur

    return out


def _is_hallucinated_text(text: str, *, repeat_threshold: int = 3, length_outlier_factor: float = 5.0,
                          avg_length: float | None = None) -> bool:
    """전사 환각 패턴 감지.

    기준 a) 동일 어구가 3회 이상 연속 반복 (n-gram 2~10자 범위로 스캔)
    기준 b) avg_length가 주어지고 길이가 평균의 length_outlier_factor배 이상

    환각 사례: "코리아 시즌 2등 코리아 시즌 2등 ..." — Gemini/Whisper가 BGM·박수
    구간에서 반복 패턴을 만들어내는 케이스.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False

    # 기준 b) 길이 outlier
    if avg_length is not None and avg_length > 0:
        if len(stripped) > avg_length * length_outlier_factor and len(stripped) > 80:
            return True

    # 기준 a) 동일 n-gram 반복: 2~10자 범위에서 같은 부분문자열이 repeat_threshold번 이상 즉시 인접 반복
    # 예: "코리아 시즌 2등 " (8자)이 반복되면 검출
    cleaned = re.sub(r"\s+", " ", stripped)
    n_max = min(20, len(cleaned) // 2)
    for n in range(2, n_max + 1):
        # 슬라이딩 윈도우로 즉시 반복 검사
        for i in range(len(cleaned) - n * repeat_threshold + 1):
            chunk = cleaned[i:i + n]
            if not chunk.strip():
                continue
            ok = True
            for k in range(1, repeat_threshold):
                if cleaned[i + k * n:i + (k + 1) * n] != chunk:
                    ok = False
                    break
            if ok:
                return True
    return False


def merge_subtitle_segments(
    segments: list[SpeechSegment],
    *,
    max_gap_sec: float = 0.25,
    max_total_chars: int = 44,
    max_duration_sec: float = 6.0,
    min_duration_sec: float = 0.6,
) -> list[SpeechSegment]:
    """자막 이벤트 수를 줄여(렌더 부담 감소) 가독성을 개선합니다.

    추가 정제:
    - 환각 텍스트(같은 어구 3회+ 반복 / 평균 5배+ 길이) 자동 제거
    - 동일 (start, end, text) 중복 제거
    - 시간 겹침 + 동일 텍스트 케이스에서 더 긴 구간 유지
    """
    if not segments:
        return []

    # 0) 환각 + 중복 사전 정제
    # 평균 길이 산출 (환각 의심 outlier 판단용 기준선)
    valid_lengths = [len(s.text.strip()) for s in segments if s.text and s.text.strip()]
    avg_len = sum(valid_lengths) / len(valid_lengths) if valid_lengths else 0.0

    cleaned_segments: list[SpeechSegment] = []
    seen_keys: set[tuple[float, float, str]] = set()
    dropped_hallucinations = 0
    dropped_duplicates = 0
    for seg in segments:
        seg_text = (seg.text or "").replace("\n", " ").strip()
        if not seg_text:
            continue
        if _is_hallucinated_text(seg_text, avg_length=avg_len):
            dropped_hallucinations += 1
            continue
        # 동일 (start, end, text) 키 중복 — 청크 경계에서 자주 발생
        key = (round(seg.start_sec, 2), round(seg.end_sec, 2), seg_text)
        if key in seen_keys:
            dropped_duplicates += 1
            continue
        seen_keys.add(key)
        cleaned_segments.append(SpeechSegment(start_sec=seg.start_sec, end_sec=seg.end_sec, text=seg_text))

    # 시간 겹침 + 동일 텍스트 → 더 긴(end-start 큰) 구간 유지
    cleaned_segments.sort(key=lambda s: (s.start_sec, s.end_sec))
    deoverlapped: list[SpeechSegment] = []
    for seg in cleaned_segments:
        absorbed = False
        for i, prev in enumerate(deoverlapped):
            if prev.text == seg.text and seg.start_sec < prev.end_sec and prev.start_sec < seg.end_sec:
                # 겹침 → 더 긴 쪽으로 통합
                merged_seg = SpeechSegment(
                    start_sec=min(prev.start_sec, seg.start_sec),
                    end_sec=max(prev.end_sec, seg.end_sec),
                    text=seg.text,
                )
                deoverlapped[i] = merged_seg
                absorbed = True
                dropped_duplicates += 1
                break
        if not absorbed:
            deoverlapped.append(seg)

    # 라운드 8a: 인접 세그먼트 동일 텍스트 환각 처리.
    # Whisper 폴백에서 같은 문장이 N개 세그먼트 연속 반복되는 케이스(예: "강아수도 갖다주더니
    # 왜 저래" × 10회)를 통합/제거. SRT 있을 때는 거의 발생 안 함.
    # - 3회+ 연속 동일 → 1개 세그먼트로 통합 (시간만 합침)
    # - 5회+ 연속 동일 → 환각으로 간주, 통째로 reject (자막 0이 깜빡임 11회보다 나음)
    deoverlapped.sort(key=lambda s: (s.start_sec, s.end_sec))
    consecutive_collapsed = 0
    consecutive_rejected = 0
    if deoverlapped:
        run_start_idx = 0
        i = 1
        result: list[SpeechSegment] = []
        while i <= len(deoverlapped):
            same_as_prev = (
                i < len(deoverlapped)
                and deoverlapped[i].text == deoverlapped[run_start_idx].text
            )
            if not same_as_prev:
                run = deoverlapped[run_start_idx:i]
                run_len = len(run)
                if run_len >= 5:
                    # 환각 의심 — 통째로 reject
                    consecutive_rejected += run_len
                elif run_len >= 3:
                    # 통합 (시간 범위만 합침)
                    result.append(SpeechSegment(
                        start_sec=run[0].start_sec,
                        end_sec=run[-1].end_sec,
                        text=run[0].text,
                    ))
                    consecutive_collapsed += run_len - 1
                else:
                    result.extend(run)
                run_start_idx = i
            i += 1
        deoverlapped = result

    if dropped_hallucinations or dropped_duplicates or consecutive_collapsed or consecutive_rejected:
        print(
            f"  [SubtitleClean] 환각 {dropped_hallucinations}건 / 중복 {dropped_duplicates}건 / "
            f"연속 통합 {consecutive_collapsed}건 / 연속 환각 reject {consecutive_rejected}건"
        )

    if not deoverlapped:
        return []

    segs = sorted(deoverlapped, key=lambda s: (s.start_sec, s.end_sec))
    merged: list[SpeechSegment] = []

    cur = SpeechSegment(
        start_sec=segs[0].start_sec,
        end_sec=segs[0].end_sec,
        text=segs[0].text.replace("\n", " ").strip(),
    )

    # 한국어 문장 종결 신호 — 이게 없으면 cur 가 문장 중간이라 다음과 더 적극 merge
    _sentence_end_chars = set(".!?…\"」』)")
    _sentence_end_endings = ("다", "요", "죠", "네", "까", "야", "어", "지", "군", "니", "오")

    def _is_sentence_complete(t: str) -> bool:
        s = t.rstrip()
        if not s:
            return True
        if s[-1] in _sentence_end_chars:
            return True
        # 한국어 종결어미로 끝나면 문장 완결로 간주
        return any(s.endswith(e) for e in _sentence_end_endings)

    for seg in segs[1:]:
        seg_text = seg.text.replace("\n", " ").strip()
        if not seg_text:
            continue

        gap = max(0.0, seg.start_sec - cur.end_sec)
        combined_text = (cur.text + " " + seg_text).strip()
        combined_dur = max(seg.end_sec, cur.end_sec) - cur.start_sec

        # cur 가 문장 중간이면 max_total_chars 한도를 30% 완화해 문장 끊김 방지.
        effective_max_chars = max_total_chars
        if not _is_sentence_complete(cur.text):
            effective_max_chars = int(max_total_chars * 1.3)

        should_merge = (
            gap <= max_gap_sec
            and len(combined_text) <= effective_max_chars
            and combined_dur <= max_duration_sec
        )
        # 너무 짧은 자막은 되도록 합치기(가독성/렌더 부담 개선)
        if (cur.end_sec - cur.start_sec) < min_duration_sec and gap <= max_gap_sec:
            should_merge = should_merge or len(combined_text) <= effective_max_chars

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
    font_name = FONT_NAME_MAP.get(style.font_name, style.font_name)

    # 라운드 10 — Format에 BackColour 컬럼 추가, Style에 동적 Bold/BorderStyle/Alignment/BackColour.
    # ASS V4+ 표준: BackColour는 BorderStyle=3 (박스)일 때 박스 배경색.
    bold_flag = -1 if style.bold else 0  # ASS bold: -1=true, 0=false
    styles_block = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{style.font_size},{style.primary_color},{style.outline_color},{style.back_color},{bold_flag},0,{style.border_style},{style.outline},{style.shadow},{style.alignment},80,80,{margin_v},0\n"
    )

    if tts_style:
        tts_font = FONT_NAME_MAP.get(tts_style.font_name, tts_style.font_name)
        tts_margin_v = tts_style.margin_v if tts_style.margin_v >= 0 else 300
        tts_bold = -1 if tts_style.bold else 0
        styles_block += (
            f"Style: TtsLine,{tts_font},{tts_style.font_size},{tts_style.primary_color},{tts_style.outline_color},{tts_style.back_color},{tts_bold},0,{tts_style.border_style},{tts_style.outline},{tts_style.shadow},{tts_style.alignment},80,80,{tts_margin_v},0\n"
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
    text = _strip_speaker_prefix(clip.subtitle.replace("\n", " "))
    joined = _wrap_for_ass(text, max_chars=15, max_lines=2)
    return f"Dialogue: 0,{start},{end},Default,,,,,, {joined}\n"


def _ass_line_original(segment: SpeechSegment, style: SubtitleStyle) -> str:
    """원본 음성 자막 라인 생성 (레이어 1 사용)"""
    start = _format_time(segment.start_sec)
    end = _format_time(segment.end_sec)
    text = _strip_speaker_prefix(segment.text.replace("\n", " "))
    joined = _wrap_for_ass(text, max_chars=15, max_lines=2)
    # 원본 자막은 레이어 1에 배치하고 약간 다른 스타일 적용 가능
    return f"Dialogue: 1,{start},{end},Default,,,,,, {joined}\n"


def _ass_line_segment(segment: SpeechSegment) -> str:
    start = _format_time(segment.start_sec)
    end = _format_time(segment.end_sec)
    text = _strip_speaker_prefix(segment.text.replace("\n", " "))
    joined = _wrap_for_ass(text, max_chars=15, max_lines=2)
    return f"Dialogue: 0,{start},{end},Default,,,,,, {joined}\n"


def _format_time(seconds: float) -> str:
    total_ms = int(seconds * 1000)
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    s = total_sec % 60
    total_min = total_sec // 60
    m = total_min % 60
    h = total_min // 60
    return f"{h:d}:{m:02d}:{s:02d}.{ms // 10:02d}"