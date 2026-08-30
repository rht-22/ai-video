"""v3 전사 — **단어 타임스탬프가 산출물**이다(기존 speech 는 단어를 쓰고 버린다).

재사용(같은 코드·같은 설정 — 새 값 발명 금지):
  - 모델·디바이스·캐시: `speech._get_whisper_model` / `_detect_device_and_compute`
    (large-v3-turbo · CUDA→float16 / CPU→int8)
  - initial_prompt: `speech._build_whisper_prompt` (리서치 인물명·시놉시스 주입)
  - transcribe 옵션: speech._extract_with_faster_whisper 와 동일
    (language=ko · no_speech_threshold=0.6 · temperature=0.0 ·
     condition_on_previous_text=False · word_timestamps=True · vad_filter=True ·
     min_silence_duration_ms=500)

전사 실패 커버리지(발주 원칙 — 조용한 뭉갬 금지): 전체 1회 전사가 죽으면 창(10분)
단위로 재시도하고, 끝내 실패한 창은 `failed_windows` 로 반환한다 — grid 가 그 구간을
scene 폴백(무성 취급)으로 재단하고 run_log 에 명시한다.

SRT 가 있으면(공식 자막): cue 는 SRT 텍스트가 정본이되 **시각 정본은 여전히 whisper
단어**다(SRT 시각은 방송 싱크 오프셋이 있을 수 있다). M1 은 SRT cue 를 grid 의
`srt_cues` 레이어로 함께 싣기만 한다 — 단어별 강제 정렬(forced alignment)은 이 레포에
정렬기가 없어 범위 밖(스모크 소재도 SRT 없음). 필요해지면 별건.
"""
from __future__ import annotations

from pathlib import Path

from app.modules.speech import (
    _build_whisper_prompt,
    _detect_device_and_compute,
    _get_whisper_model,
)

WHISPER_MODEL_NAME = "large-v3-turbo"     # speech.py:273 과 같은 값
RETRY_WINDOW_SEC = 600.0                  # 전체 실패 시 재시도 창 = chunk_seconds 와 동일


def _transcribe_range(model, audio_path: Path, *, prompt: str,
                      clip_start: float | None = None,
                      clip_end: float | None = None) -> list[dict]:
    """한 구간을 전사해 단어 목록 [{t0, t1, text, prob}] 반환(구간은 클립 필터로)."""
    kwargs = dict(
        language="ko",
        initial_prompt=prompt,
        no_speech_threshold=0.6,
        temperature=0.0,
        condition_on_previous_text=False,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    if clip_start is not None:
        # faster-whisper 는 clip_timestamps 지정 시 vad_filter 를 조용히 무시한다
        # (리뷰 확인) — 조용한 불일치 대신 명시적으로 끈다. 이 경로는 전체 전사가
        # 실패했을 때의 창 단위 구제라, VAD 부재로 인한 환각 위험은 no_speech_threshold
        # 가 1차로 막고 잔여는 실패 창 기록이 감사 대상으로 남긴다.
        kwargs["vad_filter"] = False
        del kwargs["vad_parameters"]
        kwargs["clip_timestamps"] = [clip_start, clip_end]
    segments, _info = model.transcribe(str(audio_path), **kwargs)
    words: list[dict] = []
    for seg in segments:
        for w in getattr(seg, "words", None) or []:
            if w.start is None or w.end is None:
                continue
            text = (w.word or "").strip()
            if not text:
                continue
            words.append({
                "t0": round(float(w.start), 3),
                "t1": round(float(w.end), 3),
                "text": text,
                "prob": round(float(getattr(w, "probability", 0.0) or 0.0), 3),
            })
    return words


def transcribe_words(audio_path: Path, duration_sec: float, *,
                     work_title: str | None = None,
                     character_names: list[str] | None = None,
                     work_context: str | None = None,
                     log=print) -> tuple[list[dict], list[tuple[float, float]]]:
    """전체 오디오 → (단어 목록, 실패 창 목록).

    반환 단어는 t0 오름차순. 실패 창은 [(t0, t1)] — 비어 있으면 전 구간 전사 성공."""
    device, compute_type = _detect_device_and_compute()
    model = _get_whisper_model(WHISPER_MODEL_NAME, device, compute_type)
    prompt = _build_whisper_prompt(work_title=work_title,
                                   character_names=character_names,
                                   work_context=work_context)
    log(f"  [v3/전사] {WHISPER_MODEL_NAME} ({device}/{compute_type}) — "
        f"{duration_sec:.0f}s 단어 타임스탬프 전사")
    try:
        words = _transcribe_range(model, audio_path, prompt=prompt)
        return sorted(words, key=lambda w: (w["t0"], w["t1"])), []
    except Exception as e:  # noqa: BLE001 — 전체 실패 → 창 단위 부분 구제
        log(f"  [v3/전사] ⚠ 전체 전사 실패({type(e).__name__}: {e}) — "
            f"{RETRY_WINDOW_SEC:.0f}s 창 단위로 재시도")

    words = []
    failed: list[tuple[float, float]] = []
    t = 0.0
    while t < duration_sec:
        end = min(t + RETRY_WINDOW_SEC, duration_sec)
        try:
            words.extend(_transcribe_range(model, audio_path, prompt=prompt,
                                           clip_start=t, clip_end=end))
        except Exception as e:  # noqa: BLE001
            log(f"  [v3/전사] ⚠ 창 {t:.0f}~{end:.0f}s 실패: {type(e).__name__}: {e}")
            failed.append((round(t, 3), round(end, 3)))
        t = end
    return sorted(words, key=lambda w: (w["t0"], w["t1"])), failed
