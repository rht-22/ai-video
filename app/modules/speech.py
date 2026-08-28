from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


@dataclass(frozen=True)
class SpeechSegment:
    start_sec: float
    end_sec: float
    text: str


# E20-A3 정규화: 문장부호·공백 차이만 다른 두 전사를 같은 문장으로 본다
# ("주다니 말이야" vs "주다니 말이야." — 김부장 v3 실측 중복).
_DEDUP_NORM_RE = re.compile(r"[\s\.,!?…~‥'\"“”‘’·\-]+")


def dedup_overlapping_transcripts(segments: list) -> tuple[list, list]:
    """청크 오버랩 중복 전사 제거 (E20-A3, 2026-08-28).

    청크가 180초씩 겹치게 잘리므로 겹침 구간의 대사는 양쪽 청크가 **다** 전사한다 —
    시각이 조금 어긋나고 문장부호가 달라 병합부의 완전 일치 dedup 을 빠져나가,
    같은 대사가 화면에 두 벌 겹쳐 그려졌다(김부장 v3: "이렇게 자기 발로 들어와 주다니"
    가 39.7s·40.3s 두 벌).

    규칙(보수적 — 진짜 데이터를 지우면 안 된다):
    - **시간이 겹치고**(짧은 쪽 대비 50% 이상) **정규화 텍스트가 같을 때만** 뒤엣것을
      버린다. 연달아 두 번 말한 진짜 반복 대사는 시간이 안 겹쳐 남고(E14 규율 —
      겹침은 있는 그대로의 데이터), 텍스트가 다른 교차 대화도 그대로 남는다.
    - 반환 목록은 시작 시각순 정렬본이다(하류 소비자는 전수 스캔이거나 자체 정렬).

    반환: (남은 목록, 버린 목록) — 버린 건 호출부가 건별 로그로 남긴다(조용한 드롭 금지).
    """
    ordered = sorted(segments, key=lambda s: (float(s.start_sec), float(s.end_sec)))
    kept: list = []
    dropped: list = []
    for seg in ordered:
        norm = _DEDUP_NORM_RE.sub("", str(getattr(seg, "text", "")))
        dup = False
        for prev in reversed(kept[-8:]):        # 정렬돼 있어 근처만 보면 충분하다
            ov = (min(float(prev.end_sec), float(seg.end_sec))
                  - max(float(prev.start_sec), float(seg.start_sec)))
            shorter = min(float(prev.end_sec) - float(prev.start_sec),
                          float(seg.end_sec) - float(seg.start_sec))
            if ov <= 0 or shorter <= 0:
                continue
            if (ov / shorter >= 0.5 and norm
                    and _DEDUP_NORM_RE.sub("", str(getattr(prev, "text", ""))) == norm):
                dup = True
                break
        (dropped if dup else kept).append(seg)
    return kept, dropped


def extract_transcript(
    audio_path: Path,
    *,
    work_title: str | None = None,
    character_names: list[str] | None = None,
    work_context: str | None = None,
) -> list[SpeechSegment]:
    if _has_faster_whisper():
        return _extract_with_faster_whisper(
            audio_path,
            work_title=work_title,
            character_names=character_names,
            work_context=work_context,
        )
    return []


def extract_vad_segments(audio_path: Path) -> list[SpeechSegment]:
    return []


def extract_audio_segment(
    video_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    padding_sec: float = 2.0,
) -> tuple[Path, float]:
    """영상에서 특정 구간의 오디오만 추출합니다.

    Whisper 정확도를 위해 앞뒤 padding_sec만큼 여유를 두고 추출합니다.

    Args:
        video_path: 입력 영상 파일 경로
        output_path: 출력 오디오 파일 경로
        start_sec: 추출 시작 시간(초)
        end_sec: 추출 종료 시간(초)
        padding_sec: Whisper 컨텍스트 확보용 앞뒤 패딩(초)

    Returns:
        (추출된 오디오 경로, 실제 추출 시작 시간) 튜플.
        실제 시작 시간은 패딩이 적용된 값이며 전사 결과 오프셋 보정에 사용.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    actual_start = max(0.0, start_sec - padding_sec)
    duration = (end_sec + padding_sec) - actual_start

    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd, "-y",
        "-ss", str(actual_start),
        "-i", str(video_path),
        "-t", str(duration),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(output_path),
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path, actual_start


def extract_audio_from_video(video_path: Path, output_path: Path | None = None) -> Path:
    """영상에서 오디오를 추출합니다.

    Args:
        video_path: 입력 영상 파일 경로
        output_path: 출력 오디오 파일 경로 (None이면 시스템 임시 디렉토리에 생성)

    Returns:
        추출된 오디오 파일 경로
    """
    if output_path is None:
        temp_dir = Path(tempfile.gettempdir())
        output_path = temp_dir / f"audio_{video_path.stem}.wav"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",  # Whisper에 적합한 샘플레이트
        "-ac", "1",      # 모노 채널
        str(output_path),
    ]

    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return output_path


def _has_faster_whisper() -> bool:
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


# ── Whisper 모델 캐싱 (클립마다 재로드 방지) ──
_cached_model = None
_cached_model_key: str | None = None


def _detect_device_and_compute() -> tuple[str, str]:
    """GPU 가용성을 확인하고 최적 device/compute_type을 반환."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", "float16"
    except ImportError:
        pass
    return "cpu", "int8"


def _get_whisper_model(model_name: str, device: str, compute_type: str):
    """Whisper 모델을 캐싱하여 반환 (동일 설정이면 재사용)."""
    global _cached_model, _cached_model_key
    key = f"{model_name}:{device}:{compute_type}"
    if _cached_model_key != key:
        from faster_whisper import WhisperModel
        print(f"  [Whisper] 모델 로드 중... ({model_name}, {device}, {compute_type})")
        _cached_model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _cached_model_key = key
    return _cached_model


def _build_whisper_prompt(
    work_title: str | None = None,
    character_names: list[str] | None = None,
    work_context: str | None = None,
) -> str:
    """리서치 결과를 반영한 Whisper initial_prompt를 생성."""
    parts = ["한국어 드라마, 예능 프로그램입니다."]
    if work_title:
        parts.append(f"작품명: {work_title}.")
    if character_names:
        # Whisper initial_prompt ~224 토큰 제한 → 인물명 15명으로 제한
        names_str = ", ".join(character_names[:15])
        parts.append(f"등장인물: {names_str}.")
    if work_context:
        # 시놉시스는 100자만 (너무 길면 역효과)
        parts.append(f"내용: {work_context[:100].rstrip()}")
    parts.append("한국어와 외국어가 혼용될 수 있습니다.")
    return " ".join(parts)


def _extract_with_faster_whisper(
    audio_path: Path,
    *,
    work_title: str | None = None,
    character_names: list[str] | None = None,
    work_context: str | None = None,
) -> list[SpeechSegment]:
    """faster-whisper large-v3-turbo로 음성을 텍스트로 변환합니다.

    - large-v3-turbo: base 대비 한국어 정확도 대폭 향상
    - 리서치 결과(인물명, 작품명)를 initial_prompt에 주입하여 고유명사 인식 강화
    - 모델 캐싱으로 다중 클립 전사 시 재로드 방지
    - GPU 자동 감지 (CUDA 가용 시 float16 사용)
    """
    device, compute_type = _detect_device_and_compute()
    model = _get_whisper_model("large-v3-turbo", device, compute_type)

    initial_prompt = _build_whisper_prompt(
        work_title=work_title,
        character_names=character_names,
        work_context=work_context,
    )
    print(f"  [Whisper] 프롬프트: {initial_prompt[:80]}...")

    print("  [Whisper] 전사 시작...")
    # 라운드 14.1: word_timestamps=True 활성화 + segment 내 문장 단위 재분할.
    # 이전엔 Whisper가 24초 영역을 한 덩어리 segment로 출력 → build_ass가 균등 시간 분할
    # → 자막이 음성 박자와 어긋남. word·문장 시간 활용으로 정확 매핑.
    segments_generator, info = model.transcribe(
        str(audio_path),
        language="ko",
        initial_prompt=initial_prompt,
        no_speech_threshold=0.6,
        temperature=0.0,
        condition_on_previous_text=False,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=500,
        ),
    )

    print(f"  [Whisper] 감지된 언어: {info.language} (확률: {info.language_probability:.2f})")

    import re as _re
    _SENTENCE_END = _re.compile(r"[.!?]")

    segments = []
    for seg in segments_generator:
        text = seg.text.strip()
        if not text:
            continue
        seg_start = float(seg.start)
        seg_end = float(seg.end)

        # 라운드 14.1: words 정보가 있으면 문장 끝 단어의 word.end를 시간 경계로 활용.
        # 한 segment 안 여러 문장(마침표·물음표·느낌표)을 별도 SpeechSegment로 분할.
        # word.start/end가 None인 케이스는 안전하게 필터링 (faster-whisper 일부 버전).
        words_raw = getattr(seg, "words", None) or []
        words = [w for w in words_raw
                 if getattr(w, "start", None) is not None and getattr(w, "end", None) is not None]
        if words and len(text) > 30:
            # 문장 분할: 단어 단위로 누적하면서 [.!?] 만나면 chunk 마감
            sentence_chunks: list[tuple[str, float, float]] = []
            cur_text = ""
            cur_start: float | None = float(words[0].start)
            cur_end: float = cur_start
            for w in words:
                w_text = (w.word or "").strip()
                if not w_text:
                    continue
                if cur_start is None:
                    cur_start = float(w.start)
                cur_text = (cur_text + " " + w_text).strip() if cur_text else w_text
                cur_end = float(w.end)
                # 단어 끝이 문장 종결 부호로 끝나면 chunk 마감
                if _SENTENCE_END.search(w_text):
                    sentence_chunks.append((cur_text, cur_start, cur_end))
                    cur_text = ""
                    cur_start = None
            # 잔여 텍스트
            if cur_text and cur_start is not None:
                sentence_chunks.append((cur_text, cur_start, cur_end))

            if len(sentence_chunks) >= 2:
                for ch_text, ch_start, ch_end in sentence_chunks:
                    if ch_end - ch_start < 0.05:
                        continue
                    segments.append(SpeechSegment(
                        start_sec=ch_start, end_sec=ch_end, text=ch_text,
                    ))
                continue  # 분할 성공 → 원본 segment 추가 안 함

        # 분할 안 됨: 원본 segment 그대로
        segments.append(
            SpeechSegment(
                start_sec=seg_start,
                end_sec=seg_end,
                text=text,
            )
        )

    print(f"  [Whisper] 전사 완료: {len(segments)}개 세그먼트 (문장 단위 분할 적용)")
    return segments