"""1단계 — 단어 타임스탬프 전사 → 대사 줄 인덱스(L-xxx). 백엔드 둘: ElevenLabs Scribe v2(기본) · faster-whisper.

Scribe 는 레포 `stt_elevenlabs` 의 요청 조립·keyterms·재시도를 그대로 쓰고(10분 창으로 나눠 올려 시각을 창 시작만큼 더한다),
단어 신뢰도 p 는 exp(logprob). 키 없으면 **즉시 실패**(레포 규율 — 조용히 whisper 로 떨어지지 않는다).

산출 `transcript.json`:
  words:  [{i, start, end, text, p}]              — 절대초(ms 반올림), S 모드 립싱크의 정본
  lines:  [{id:"L-001", start, end, text, word_i:[..], speaker:null}] — 문장 단위 줄(화자는 index 단계가 채운다)

줄 재단 규칙: 단어 사이 공백 ≥0.5s · 문장 종결부호 · 44자 · 6.0s 상한(레포 cue 규칙과 같은 수치).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.tikitaka.common import Job, ms3

_GAP_SEC = 0.5
_MAX_CHARS = 44
_MAX_SEC = 6.0
_END_PUNCT = re.compile(r"[.!?…]$")


def _detect_device() -> tuple[str, str]:
    try:
        import torch  # noqa
        if torch.cuda.is_available():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def words_to_lines(words: list[dict]) -> list[dict]:
    lines: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        text = " ".join(w["text"] for w in cur).strip()
        lines.append({"id": f"L-{len(lines)+1:03d}", "start": cur[0]["start"], "end": cur[-1]["end"],
                      "text": text, "word_i": [w["i"] for w in cur], "speaker": None})
        cur.clear()

    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            cur_len = len(" ".join(x["text"] for x in cur))
            cur_dur = cur[-1]["end"] - cur[0]["start"]
            if gap >= _GAP_SEC or _END_PUNCT.search(cur[-1]["text"]) or cur_len + len(w["text"]) + 1 > _MAX_CHARS or cur_dur + (w["end"] - w["start"]) > _MAX_SEC:
                flush()
        cur.append(w)
    flush()
    return lines


STT_WINDOW_SEC = 600.0


def scribe_words_to_words(payload_words: list, offset: float) -> list[dict]:
    """Scribe `words[]`(type word/spacing/audio_event · start/end/text/logprob) → 파이프라인 단어(절대초). 순수 — 테스트 대상."""
    import math
    out: list[dict] = []
    for w in payload_words or []:
        if str(w.get("type") or "word") != "word":
            continue
        t = str(w.get("text") or "").strip()
        if not t or w.get("start") is None or w.get("end") is None:
            continue
        lp = w.get("logprob")
        try:
            p = math.exp(float(lp)) if lp is not None else 0.9
        except (TypeError, ValueError):
            p = 0.9
        out.append({"start": ms3(offset + float(w["start"])), "end": ms3(offset + float(w["end"])), "text": t, "p": round(min(1.0, max(0.0, p)), 3)})
    return out


def transcribe_elevenlabs(job: Job, audio_path: Path, *, title: str = "", cast: list[str] | None = None) -> dict:
    """Scribe v2 — 16k wav 를 10분 창으로 잘라 올린다(창별 캐시). keyterms = 작품명·등장인물."""
    import subprocess
    from app.modules import stt_elevenlabs as EL
    from app.tikitaka.common import find_bin
    api_key = EL.ensure_api_key()
    keyterms = EL._build_keyterms(title or None, cast or None) if EL.EL_STT_KEYTERMS_ENABLED else None
    dur = float(job.load("probe.json")["duration_sec"]) if job.has("probe.json") else None
    if dur is None:
        raise RuntimeError("probe.json 이 필요하다")
    ffmpeg = find_bin("ffmpeg")
    wdir = job.path("stt_windows")
    wdir.mkdir(exist_ok=True)
    words: list[dict] = []
    t = 0.0
    k = 0
    usage_secs = 0.0
    while t < dur - 0.5:
        e = min(dur, t + STT_WINDOW_SEC)
        if dur - e < 60.0:
            e = dur
        cache = wdir / f"win_{k:02d}.json"
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
        else:
            seg = wdir / f"win_{k:02d}.wav"
            subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{t:.3f}", "-t", f"{e-t:.3f}", "-i", str(audio_path),
                            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(seg)], check=True)
            payload = EL._post_speech_to_text(seg, api_key, language="ko", keyterms=keyterms, is_raw=False)
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            seg.unlink(missing_ok=True)
        ws = scribe_words_to_words(payload.get("words") or [], t)
        usage_secs += float(payload.get("audio_duration_secs") or (e - t))
        job.log(f"[transcribe/scribe] 창{k:02d} {t:.0f}~{e:.0f}s 단어 {len(ws)}")
        words.extend(ws)
        t = e
        k += 1
    words.sort(key=lambda w: (w["start"], w["end"]))
    for i, w in enumerate(words):
        w["i"] = i
    lines = words_to_lines(words)
    data = {"model": f"scribe:{EL.EL_STT_MODEL_ID}", "language": "ko", "backend": "elevenlabs", "keyterms": keyterms or [],
            "words": words, "lines": lines}
    job.save("transcript.json", data)
    job.log(f"[transcribe/scribe] 완료 — 단어 {len(words)} · 줄 {len(lines)} · 오디오 {usage_secs/60:.1f}분 · keyterms {len(keyterms or [])}")
    job.record_step("transcribe", words=len(words), lines=len(lines), backend="elevenlabs", model=data["model"], audio_secs=round(usage_secs, 1))
    return data


def transcribe(job: Job, audio_path: Path, *, title: str = "", cast: list[str] | None = None, model_name: str = "large-v3-turbo",
               backend: str = "elevenlabs") -> dict:
    if job.has("transcript.json"):
        return job.load("transcript.json")
    if backend == "elevenlabs":
        return transcribe_elevenlabs(job, audio_path, title=title, cast=cast)
    return transcribe_whisper(job, audio_path, title=title, cast=cast, model_name=model_name)


def transcribe_whisper(job: Job, audio_path: Path, *, title: str = "", cast: list[str] | None = None, model_name: str = "large-v3-turbo") -> dict:
    from faster_whisper import WhisperModel
    device, compute = _detect_device()
    job.log(f"[transcribe] 모델 로드 {model_name} ({device}/{compute})")
    model = WhisperModel(model_name, device=device, compute_type=compute)
    prompt_bits = [f"드라마 '{title}' 대사." if title else "한국어 드라마 대사."]
    if cast:
        prompt_bits.append("등장인물: " + ", ".join(cast) + ".")
    initial_prompt = " ".join(prompt_bits)
    segments, info = model.transcribe(str(audio_path), language="ko", initial_prompt=initial_prompt,
                                      temperature=0.0, condition_on_previous_text=False,
                                      word_timestamps=True, vad_filter=True,
                                      vad_parameters=dict(min_silence_duration_ms=500), no_speech_threshold=0.6)
    words: list[dict] = []
    n_seg = 0
    for seg in segments:
        n_seg += 1
        for w in (seg.words or []):
            if w.start is None or w.end is None:
                continue
            t = (w.word or "").strip()
            if not t:
                continue
            words.append({"i": len(words), "start": ms3(w.start), "end": ms3(w.end), "text": t, "p": round(float(w.probability or 0), 3)})
        if n_seg % 50 == 0:
            job.log(f"[transcribe] … {n_seg} seg · {len(words)} words · {seg.end:.0f}s")
    lines = words_to_lines(words)
    data = {"model": model_name, "language": info.language, "backend": "whisper", "words": words, "lines": lines}
    job.save("transcript.json", data)
    job.log(f"[transcribe] 완료 — 단어 {len(words)} · 줄 {len(lines)}")
    job.record_step("transcribe", words=len(words), lines=len(lines), backend="whisper", model=model_name)
    return data
