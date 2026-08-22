"""E11 (2026-08-22) — 자막 전사 백엔드: ElevenLabs Scribe STT 어댑터.

`--transcribe-backend elevenlabs` 일 때만 쓰인다. 미지정·`default` 는 이 모듈을
import 조차 하지 않는다(회귀 0 — 기존 내장 전사 경로는 한 줄도 안 바뀐다).

계약(발주서 e11-transcribe-backend):

- 입력 = 오디오(또는 영상) 경로 + 언어, 출력 = **내장 전사와 같은 내부 표현**
  (`SpeechSegment(start_sec, end_sec, text)`, 파일 시작 기준 상대시간).
  청크 오프셋 가산·병합은 호출부(`pipeline.transcribe_chunks`)가 두 백엔드에
  **똑같이** 적용한다 — 편집실 앵커(source_time_sec 역산)가 두 경로에서 같이 살려면
  좌표계가 하나여야 한다.
- 자격증명은 환경변수 `ELEVENLABS_API_KEY` 하나. 없으면 즉시 실패(조용한 폴백 금지).
- 오디오만 올린다: 같은 길이 1080p mp4 30~80MB vs 16k 모노 wav ~1MB.
  전처리(`-vn -ac 1 -ar 16000 -c:a pcm_s16le`)는 내장 Whisper 가 내부 디코딩으로
  만드는 것과 같은 규격이라 두 백엔드가 같은 소리를 듣는다.
- `words[]` 는 `word`·`spacing`(폭 0)·`audio_event` 가 섞여 온다 — `type == "word"`
  만 걸러 cue 로 묶는다. 안 거르면 줄 길이·타이밍이 조용히 어긋난다.
- `additional_formats`(SRT 직접 생성)는 **쓰지 않는다**. 이 파이프라인의 cue 규칙은
  merge_subtitle_segments·build_ass 가 정본이고, 서버가 끊어 준 SRT 를 받으면
  두 백엔드의 cue 규칙이 갈라진다(E11-1 전제 위반).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.speech import SpeechSegment

# ── API 계약 ────────────────────────────────────────────────────────────────
EL_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
# scribe_v1 은 폐기(제거 예정일 2026-07-09, 이미 지남) — 문서 enum 에 남아 있어도 믿지 않는다.
# 요율·모델이 또 바뀌면 코드 배포 없이 env 로 넘긴다.
EL_STT_MODEL_ID = os.environ.get("ELEVENLABS_STT_MODEL_ID", "scribe_v2")
EL_STT_TIMEOUT_SEC = float(os.environ.get("ELEVENLABS_STT_TIMEOUT", "600"))
# 배치 Scribe 요율(조사 시점 $0.22/오디오시간). 정산용 추정치 로그에만 쓰인다.
EL_STT_USD_PER_AUDIO_HOUR = float(os.environ.get("ELEVENLABS_STT_USD_PER_AUDIO_HOUR", "0.22"))
_EL_RETRIES = 2  # 첫 시도 제외 — 429·5xx·네트워크만

# 파일 파트 포맷. 기본은 wav 컨테이너 + file_format 미지정(=other, "all major formats").
# `pcm_s16le_16` 은 *헤더 없는* 16k 모노 s16le 원본을 뜻하므로 RIFF 헤더가 붙은 wav 를
# 그 이름으로 올리면 헤더가 샘플로 읽힐 수 있다 — env 로 켤 때만 실제 raw 로 올린다.
EL_STT_FILE_FORMAT = os.environ.get("ELEVENLABS_STT_FILE_FORMAT", "").strip()

# keyterms(고유명사 바이어싱 — 내장 Whisper 의 initial_prompt 대응)는 요율이 오르고
# (+$0.05/오디오시간) 폼 직렬화 형태를 실키로 확인하지 못했다. 기본 off, env 로만 켠다.
EL_STT_KEYTERMS_ENABLED = os.environ.get("ELEVENLABS_STT_KEYTERMS", "").strip().lower() in (
    "1", "on", "true", "yes",
)
_KEYTERM_MAX_CHARS = 50   # 문서 제한
_KEYTERM_MAX_COUNT = 1000

# ISO-639-1 → ISO-639-3. 발주서 예시가 'kor' 이고 문서상 둘 다 받는다 —
# 모르는 코드는 그대로 넘긴다(서버가 판단).
_LANG_ISO3 = {"ko": "kor", "en": "eng", "ja": "jpn", "zh": "zho", "es": "spa", "fr": "fra"}

# ── cue 묶음 규칙 (내장 Whisper 경로와 같은 좌표계·같은 결) ───────────────────
# Whisper 경로: VAD min_silence 500ms 로 segment 를 끊고, 30자 넘는 segment 는 문장
# 종결부호에서 재분할한다. Scribe 는 word 단위로만 오므로 같은 기준을 여기서 준다.
_CUE_GAP_SEC = 0.5          # 단어 사이 공백이 이 이상이면 새 cue (Whisper VAD 500ms)
_CUE_MAX_CHARS = 44         # merge_subtitle_segments 의 max_total_chars 와 동일
_CUE_MAX_DURATION_SEC = 6.0  # merge_subtitle_segments 의 max_duration_sec 와 동일
_SENTENCE_END = re.compile(r"[.!?…]$")
# 확신도가 낮은 줄 = 검수자가 먼저 볼 줄. 임계값은 로그 표시용일 뿐 결과를 바꾸지 않는다.
_LOW_CONFIDENCE_LOGPROB = -0.60


class ElevenLabsSTTError(RuntimeError):
    """Scribe 호출 실패. permanent(4xx) 는 재시도 없이, transient 는 재시도 후 올라온다."""


# ── 사용량/비용 집계 (run_log 기록용) ────────────────────────────────────────
_USAGE: dict = {
    "requests": 0,
    "audio_duration_secs": 0.0,
    "api_elapsed_sec": 0.0,
    "low_confidence_lines": [],
}


def reset_usage() -> None:
    _USAGE.update({
        "requests": 0,
        "audio_duration_secs": 0.0,
        "api_elapsed_sec": 0.0,
        "low_confidence_lines": [],
    })


def usage_summary() -> dict:
    """run_log 에 남길 요약 — 분 단위 과금이라 audio_duration_secs 가 정산 근거다."""
    secs = float(_USAGE["audio_duration_secs"])
    return {
        "backend": "elevenlabs",
        "model_id": EL_STT_MODEL_ID,
        "requests": int(_USAGE["requests"]),
        "audio_duration_secs": round(secs, 2),
        "api_elapsed_sec": round(float(_USAGE["api_elapsed_sec"]), 1),
        "usd_per_audio_hour": EL_STT_USD_PER_AUDIO_HOUR,
        "estimated_usd": round(secs / 3600.0 * EL_STT_USD_PER_AUDIO_HOUR, 6),
        # 검수자가 어디를 볼지 알려 주는 목록(내장 전사에는 없는 이 백엔드의 이득).
        "low_confidence_lines": list(_USAGE["low_confidence_lines"])[:20],
    }


def ensure_api_key() -> str:
    """키 확인 — 없으면 '무엇을 어디에 넣어야 하는지'까지 말하고 즉시 실패."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise ElevenLabsSTTError(
            "--transcribe-backend elevenlabs 인데 ELEVENLABS_API_KEY 환경변수가 없습니다. "
            "ElevenLabs 대시보드에서 API 키를 발급해 실행 노드의 환경변수 "
            "ELEVENLABS_API_KEY 에 넣으세요 (예: export ELEVENLABS_API_KEY=sk_...). "
            "키 없이 내장 전사로 조용히 넘어가지 않습니다 — 일레븐랩스로 바꿨다고 믿은 채 "
            "종전 전사로 발행되는 것을 막기 위한 것입니다."
        )
    return key


def build_transcriber(
    *,
    language: str = "ko",
    work_title: str | None = None,
    character_names: list[str] | None = None,
    work_context: str | None = None,
):
    """`transcribe_chunks(transcriber=...)` 가 쓰는 콜러블(Path → list[SpeechSegment]).

    시그니처는 내장 경로(app.modules.speech.extract_transcript 래퍼)와 동일하다 —
    분기는 이 콜러블을 고르는 지점에서 끝나고 downstream 은 백엔드를 모른다.
    """
    ensure_api_key()
    keyterms = _build_keyterms(work_title, character_names) if EL_STT_KEYTERMS_ENABLED else None

    def _transcriber(media_path: Path) -> list[SpeechSegment]:
        return transcribe(Path(media_path), language=language, keyterms=keyterms)

    return _transcriber


def _build_keyterms(work_title: str | None, character_names: list[str] | None) -> list[str]:
    """고유명사 바이어싱 목록 — Whisper initial_prompt 의 인물명 주입과 같은 의도."""
    terms: list[str] = []
    for t in [work_title or ""] + list(character_names or []):
        t = (t or "").strip()
        if t and len(t) <= _KEYTERM_MAX_CHARS and t not in terms:
            terms.append(t)
    return terms[:_KEYTERM_MAX_COUNT]


def transcribe(
    media_path: Path,
    *,
    language: str = "ko",
    keyterms: list[str] | None = None,
) -> list[SpeechSegment]:
    """미디어 1개를 Scribe 로 전사 → 파일 시작 기준 상대시간 SpeechSegment 리스트."""
    api_key = ensure_api_key()
    audio_path, is_raw = _extract_audio(Path(media_path))
    try:
        payload = _post_speech_to_text(audio_path, api_key, language=language,
                                       keyterms=keyterms, is_raw=is_raw)
    finally:
        try:
            audio_path.unlink()
        except OSError:
            pass

    segments, confidences = words_to_segments(payload.get("words") or [])
    if not segments and str(payload.get("text") or "").strip():
        # 텍스트는 왔는데 타임코드 있는 word 가 없다 = 계약 파손(응답 스키마 변동).
        # 빈 자막으로 조용히 발행하지 않는다 — 자막이 통째로 사라진 판을 아무도 못 잡는다.
        raise ElevenLabsSTTError(
            "Scribe 응답에 timestamps 있는 words[] 가 없습니다(text 만 옴) — "
            "timestamps_granularity·model_id 계약이 바뀌었는지 확인하세요.")

    # 과금·정산 근거. 응답에 없으면(스키마 변동) 우리가 올린 오디오 길이로 대체한다.
    dur = payload.get("audio_duration_secs")
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        dur = _sum_segment_span(segments)
    _USAGE["requests"] += 1
    _USAGE["audio_duration_secs"] += max(0.0, dur)

    _log_result(payload, segments, confidences, dur)
    return segments


def words_to_segments(words: list) -> tuple[list[SpeechSegment], list[float]]:
    """Scribe `words[]` → cue(SpeechSegment) 목록 + cue 별 평균 logprob.

    - `type == "word"` 만 쓴다. `spacing`(폭 0)·`audio_event`(=(laughter) 류)는 버린다
      — tag_audio_events=false 로 요청하지만 서버가 섞어 보내도 자막에는 안 새게 이중 방어.
    - cue 경계: 0.5s 이상 공백 · 문장 종결부호 · 44자 · 6.0s.
    """
    cues: list[SpeechSegment] = []
    confidences: list[float] = []
    cur_words: list[str] = []
    cur_logprobs: list[float] = []
    cur_start: float | None = None
    cur_end: float = 0.0

    def _flush() -> None:
        nonlocal cur_words, cur_logprobs, cur_start, cur_end
        if cur_words and cur_start is not None and cur_end > cur_start:
            text = " ".join(cur_words).strip()
            if text:
                cues.append(SpeechSegment(start_sec=cur_start, end_sec=cur_end, text=text))
                confidences.append(
                    sum(cur_logprobs) / len(cur_logprobs) if cur_logprobs else 0.0)
        cur_words = []
        cur_logprobs = []
        cur_start = None
        cur_end = 0.0

    for w in words or []:
        if _get(w, "type", "word") != "word":
            continue
        text = str(_get(w, "text", "") or "").strip()
        if not text:
            continue
        try:
            start = float(_get(w, "start", 0.0))
            end = float(_get(w, "end", 0.0))
        except (TypeError, ValueError):
            continue
        if end < start:
            continue

        if cur_start is not None:
            gap = start - cur_end
            too_long = (end - cur_start) > _CUE_MAX_DURATION_SEC
            too_wide = len(" ".join(cur_words)) + 1 + len(text) > _CUE_MAX_CHARS
            if gap >= _CUE_GAP_SEC or too_long or too_wide:
                _flush()

        if cur_start is None:
            cur_start = start
        cur_words.append(text)
        cur_end = max(cur_end, end)
        try:
            cur_logprobs.append(float(_get(w, "logprob", 0.0)))
        except (TypeError, ValueError):
            pass

        if _SENTENCE_END.search(text):
            _flush()

    _flush()
    return cues, confidences


def _get(obj, key, default=None):
    """dict 응답과 SDK 객체 응답을 같이 받는다(테스트 페이크 포함)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _sum_segment_span(segments: list[SpeechSegment]) -> float:
    if not segments:
        return 0.0
    return max(s.end_sec for s in segments)


def _extract_audio(media_path: Path) -> tuple[Path, bool]:
    """영상/오디오 → 16kHz 모노 s16le. (경로, raw여부) 반환.

    내장 Whisper 는 같은 규격을 내부 디코딩으로 만든다 — 두 백엔드가 같은 소리를 듣는다.
    """
    raw = EL_STT_FILE_FORMAT == "pcm_s16le_16"
    suffix = ".pcm" if raw else ".wav"
    fd, tmp = tempfile.mkstemp(prefix="el_stt_", suffix=suffix)
    os.close(fd)
    out = Path(tmp)
    cmd = [find_ffmpeg_command("ffmpeg"), "-y", "-i", str(media_path), "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    if raw:
        cmd += ["-f", "s16le"]
    cmd.append(str(out))
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        out.unlink(missing_ok=True)
        raise ElevenLabsSTTError(f"오디오 추출 실패({media_path}): {e}") from e
    return out, raw


def _post_speech_to_text(
    audio_path: Path,
    api_key: str,
    *,
    language: str,
    keyterms: list[str] | None,
    is_raw: bool,
) -> dict:
    """multipart POST + 실패 분류.

    permanent(401·403·4xx = 키/인자 오류) → 재시도 없이 즉시 실패.
    transient(429·5xx·네트워크) → 2회 재시도(2s·4s) 후 실패.
    어느 쪽이든 **내장 전사로 조용히 넘어가지 않는다** — 사람이 일레븐랩스로 바꿨다고
    믿은 채 종전 전사로 발행되는 것이 이 기능에서 제일 나쁜 실패다.
    """
    import requests

    data: dict = {
        "model_id": EL_STT_MODEL_ID,
        "language_code": _LANG_ISO3.get((language or "ko").lower(), language or "ko"),
        "timestamps_granularity": "word",
        # 기본이 true 다 — 켜두면 자막에 '(laughter)' 가 섞인다.
        "tag_audio_events": "false",
        "diarize": "false",
    }
    if is_raw:
        data["file_format"] = "pcm_s16le_16"
    if keyterms:
        # 문서상 list 파라미터 — 폼에서는 JSON 배열로 직렬화해 보낸다(env 로 켤 때만).
        data["keyterms"] = json.dumps(keyterms, ensure_ascii=False)

    headers = {"xi-api-key": api_key}
    last_err: Exception | None = None
    for attempt in range(_EL_RETRIES + 1):
        t0 = time.time()
        try:
            with audio_path.open("rb") as fh:
                files = {"file": (audio_path.name, fh, "application/octet-stream")}
                resp = requests.post(EL_STT_URL, headers=headers, data=data, files=files,
                                     timeout=EL_STT_TIMEOUT_SEC)
        except requests.RequestException as e:      # 네트워크 — transient
            last_err = e
        else:
            _USAGE["api_elapsed_sec"] += time.time() - t0
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as e:
                    raise ElevenLabsSTTError(
                        f"Scribe 응답 JSON 파싱 실패: {resp.text[:200]}") from e
            last_err = ElevenLabsSTTError(
                f"ElevenLabs STT {resp.status_code}: {resp.text[:300]}")
            if resp.status_code != 429 and resp.status_code < 500:
                break                                # permanent — 재시도 무의미
        if attempt < _EL_RETRIES:
            time.sleep(2 * (attempt + 1))
    raise ElevenLabsSTTError(f"ElevenLabs Scribe 전사 실패({audio_path.name}) — {last_err}")


def _log_result(payload: dict, segments: list, confidences: list[float], dur: float) -> None:
    lang = _get(payload, "language_code", "?")
    prob = _get(payload, "language_probability", None)
    prob_s = f" (확률 {float(prob):.2f})" if isinstance(prob, (int, float)) else ""
    usd = dur / 3600.0 * EL_STT_USD_PER_AUDIO_HOUR
    print(f"  [Scribe] {EL_STT_MODEL_ID} 전사 완료: {len(segments)}개 cue · "
          f"언어 {lang}{prob_s} · 오디오 {dur:.1f}s · 추정 ${usd:.5f}")

    # 확신도 낮은 줄 = 검수자가 먼저 볼 줄. 결과는 안 바꾸고 로그·run_log 에만 남긴다.
    low = [(c, s) for c, s in zip(confidences, segments) if c < _LOW_CONFIDENCE_LOGPROB]
    low.sort(key=lambda x: x[0])
    for c, s in low[:3]:
        print(f"    [Scribe-확신도낮음] {s.start_sec:.1f}~{s.end_sec:.1f}s "
              f"logprob={c:.2f} {s.text[:30]!r}")
    for c, s in low:
        if len(_USAGE["low_confidence_lines"]) >= 20:
            break
        _USAGE["low_confidence_lines"].append({
            "start_sec": round(float(s.start_sec), 2),
            "end_sec": round(float(s.end_sec), 2),
            "logprob": round(float(c), 3),
            "text": s.text[:60],
        })
