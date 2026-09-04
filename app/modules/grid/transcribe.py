"""격자 전사 — **단어 타임스탬프가 산출물**이다(기존 speech 는 단어를 쓰고 버린다).

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


# ── M8-B: 전사 공백 재전사 (발주서 v3-m8 — 트리거 실측 수정판) ────────────────
GAP_RETRY_SEC = 6.0          # 단어 간 공백 트리거 — 긴 공백은 드물어 상한 불필요
GAP_SILENCE_SKIP = 0.8       # 창의 이 비율 이상이 silencedetect 무음이면 진짜 무음
GAP_MIN_PROB = 0.4           # vad off 재전사의 환각 방어 ① — 저확신 단어 버림
# ② 반복 환각(퇴화 루프) 방어 — 실측(가왕쇼 2026-08-31): vad off 재전사가 음악·
# 함성 구간에서 같은 토큰을 수십 번 뱉는다("그녀는"×71·"육십!"×53). prob 은
# 0.84~0.97 로 높아 ①이 못 잡는다. 서명은 두 가지: 한 토큰의 창 내 점유율이
# 높거나, 단어 길이가 0 에 수렴(정상 발화는 중앙 0.3~0.9s).
GAP_REPEAT_RATIO = 0.4       # 최빈 토큰 점유율 — 이 이상이면 루프로 본다
GAP_REPEAT_MIN_N = 3         # 최소 반복 횟수(짧은 창의 우연 일치 제외)
GAP_MIN_WORD_DUR = 0.04      # 이보다 짧은 단어는 실물이 아니다(정렬 산출물)
GAP_MIN_MEDIAN_DUR = 0.06    # 창 단어 길이 중앙값이 이 아래면 창 전체가 퇴화


def is_degenerate_loop(found: list[dict]) -> str | None:
    """복원 단어 목록이 반복 환각인가 → 사유 문자열(정상이면 None). 순수.

    창 **전체를 버린다** — 루프가 낀 창은 나머지 단어도 신뢰할 수 없고, 버려도
    M8 이전 동작(그 구간 무성)으로 돌아갈 뿐이라 손실이 없다(보수 규율)."""
    if not found:
        return None
    from collections import Counter
    counts = Counter(w["text"] for w in found)
    top, n = counts.most_common(1)[0]
    if n >= GAP_REPEAT_MIN_N and n / len(found) >= GAP_REPEAT_RATIO:
        return f"반복 환각: {top!r} ×{n}/{len(found)}"
    durs = sorted(w["t1"] - w["t0"] for w in found)
    median = durs[len(durs) // 2]
    if median < GAP_MIN_MEDIAN_DUR:
        return f"길이 퇴화: 단어 길이 중앙 {median:.3f}s"
    return None


def retranscribe_gaps(audio_path: Path, words: list[dict], duration_sec: float,
                      silence: list[tuple[float, float]], *,
                      prompt: str = "", log=print) -> tuple[list[dict], dict]:
    """단어 간 긴 공백(≥6s)만 완화 설정으로 재전사해 병합. 입력 불변 — 사본 반환.

    가왕쇼 실사고: VAD 가 시끄러운 현장음 속 발화를 삼켜 11.5s 대사 구간이 무성
    오분류 → 쇼츠 자막 공백. 완화 재전사(vad off·no_speech 0.9)가 실제 대사를
    복원함을 착수 실측으로 확인. 진짜 무음 창(silencedetect 겹침 ≥80%)은 건너뛴다
    — 무음에 vad off 전사를 돌리면 환각 위험만 산다. 에너지 트리거는 실측 기각
    (유성 −27.8dB vs 무성 −26.9dB — 예능은 무성도 시끄럽다)."""
    ws = sorted(words, key=lambda w: (w["t0"], w["t1"]))
    gaps = []
    prev_end = 0.0
    for w in ws:
        if w["t0"] - prev_end >= GAP_RETRY_SEC:
            gaps.append((prev_end, w["t0"]))
        prev_end = max(prev_end, w["t1"])
    if duration_sec - prev_end >= GAP_RETRY_SEC:
        gaps.append((prev_end, duration_sec))

    def silence_frac(a: float, b: float) -> float:
        ov = sum(max(0.0, min(b, s1) - max(a, s0)) for s0, s1 in silence or [])
        return ov / (b - a) if b > a else 1.0

    audit = {"gaps": len(gaps), "windows": [], "recovered_words": 0}
    todo = [(a, b) for a, b in gaps if silence_frac(a, b) < GAP_SILENCE_SKIP]
    audit["skipped_silence"] = len(gaps) - len(todo)
    if not todo:
        return list(words), audit

    device, compute_type = _detect_device_and_compute()
    model = _get_whisper_model(WHISPER_MODEL_NAME, device, compute_type)
    merged = list(words)
    for a, b in todo:
        t0, t1 = max(0.0, a - 1.0), min(duration_sec, b + 1.0)
        rec = {"window": [round(a, 1), round(b, 1)], "found": 0}
        try:
            segs, _info = model.transcribe(
                str(audio_path), language="ko", temperature=0.0,
                initial_prompt=prompt or None, word_timestamps=True,
                vad_filter=False, no_speech_threshold=0.9,
                condition_on_previous_text=False, clip_timestamps=[t0, t1])
            found = []
            for s in segs:
                for w in s.words or []:
                    if w.probability is None or w.probability < GAP_MIN_PROB:
                        continue
                    if float(w.end) - float(w.start) < GAP_MIN_WORD_DUR:
                        continue          # 0 길이 단어 = 정렬 산출물(환각 서명)
                    if not (a + 0.1 < float(w.start) and float(w.end) < b - 0.1):
                        continue          # 기존 단어와의 경계 중복 방지 — 공백 안만
                    text = (w.word or "").strip()
                    if text:
                        found.append({"t0": round(float(w.start), 3),
                                      "t1": round(float(w.end), 3),
                                      "text": text,
                                      "prob": round(float(w.probability), 3)})
            loop = is_degenerate_loop(found)
            if loop:
                rec["dropped"] = loop      # 조용한 폐기 금지 — 사유를 남긴다
                audit.setdefault("dropped_windows", 0)
                audit["dropped_windows"] += 1
                log(f"  [v3/전사] ⚠ 공백 재전사 {a:.0f}~{b:.0f}s 폐기 — {loop}")
                audit["windows"].append(rec)
                continue
            merged.extend(found)
            rec["found"] = len(found)
            audit["recovered_words"] += len(found)
            if found:
                log(f"  [v3/전사] 공백 재전사 {a:.0f}~{b:.0f}s — 단어 {len(found)}개 복원")
        except Exception as e:  # noqa: BLE001 — 재전사 실패는 현행 유지(경고 모드)
            rec["error"] = f"{type(e).__name__}: {e}"
        audit["windows"].append(rec)
    return sorted(merged, key=lambda w: (w["t0"], w["t1"])), audit
