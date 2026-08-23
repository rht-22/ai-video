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

──────────────────────────────────────────────────────────────────────────────
E13 (2026-08-22) — 실측 뒤 다듬기. 발주서 e13-transcribe-polish.
셋 다 **이 파일 안(=elevenlabs 경로)에서 끝난다**. 내장 Whisper 산출은 안 건드린다.

1. **keyterms 기본 on** (P1-a) — 내장 Whisper 는 `_build_whisper_prompt` 로 리서치
   결과(작품명·인물명)를 늘 받는데 Scribe 쪽 대응물만 꺼져 있어 **두 백엔드가 비대칭
   조건으로 붙고 있었다**. 실측의 `SK텔레콤→에스케이텔레콤` 류가 그 결과다.
   ⚠ 종전 직렬화(`json.dumps` 로 배열 하나를 문자열 필드에 담기)는 **틀렸다** —
   서버가 배열 문자열 전체를 키텀 하나로 보고 400 `invalid_keyword_length` 를 준다
   (elevenlabs-python #819 의 v2.59.0 회귀와 같은 형태). 맞는 형태는 **같은 이름의
   폼 필드 반복**(`keyterms=A`, `keyterms=B`)이라 requests 에 list-of-tuples 로 넘긴다.
2. **언어 이탈 cue 차단** (P2-a) — `language_code=kor` 로 요청했는데 한자·가나 등
   요청 언어 밖 문자가 지배적인 cue 는 버린다(가왕쇼 EP1 의 `謝 謝`).
   logprob 임계로는 못 가른다 — 실측 저확신 3건 중 2건이 멀쩡한 한국어였다.
3. **표기 보정** (P1-b) — keyterms 로 못 잡는 일반 어휘(`CTO`·`IT업계`·`30년`)만
   **데이터 파일의 완전 토큰 일치**로 되돌린다. 사전은 app/data/transcribe_normalize_ko.json.

버리거나 고쳐 쓴 줄은 **전부 stdout 과 run_log 에 남는다**. 사람이 안 보는 사이에
전사 결과를 바꾸는 일이라, 흔적 없는 수정은 이 백엔드에서 제일 나쁜 실패다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.speech import SpeechSegment

# ── API 계약 ────────────────────────────────────────────────────────────────
EL_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
# scribe_v1 은 폐기(제거 예정일 2026-07-09, 이미 지남) — 문서 enum 에 남아 있어도 믿지 않는다.
# 요율·모델이 또 바뀌면 코드 배포 없이 env 로 넘긴다.
EL_STT_MODEL_ID = os.environ.get("ELEVENLABS_STT_MODEL_ID", "scribe_v2")
EL_STT_TIMEOUT_SEC = float(os.environ.get("ELEVENLABS_STT_TIMEOUT", "600"))
# 배치 Scribe 요율(조사 시점 $0.22/오디오시간, keyterms 를 실제로 실어 보낸 요청은
# +$0.05 → $0.27). 정산용 추정치 로그에만 쓰인다 — **요청 단위로 갈라 센다**.
# keyterms 를 켠 실행이라도 인물 리서치가 비면 폼에 안 실리므로 그 청크는 기본 요율이다.
EL_STT_USD_PER_AUDIO_HOUR = float(os.environ.get("ELEVENLABS_STT_USD_PER_AUDIO_HOUR", "0.22"))
EL_STT_KEYTERMS_USD_PER_AUDIO_HOUR = float(
    os.environ.get("ELEVENLABS_STT_KEYTERMS_USD_PER_AUDIO_HOUR", "0.27"))
_EL_RETRIES = 2  # 첫 시도 제외 — 429·5xx·네트워크만

# 파일 파트 포맷. 기본은 wav 컨테이너 + file_format 미지정(=other, "all major formats").
# `pcm_s16le_16` 은 *헤더 없는* 16k 모노 s16le 원본을 뜻하므로 RIFF 헤더가 붙은 wav 를
# 그 이름으로 올리면 헤더가 샘플로 읽힐 수 있다 — env 로 켤 때만 실제 raw 로 올린다.
EL_STT_FILE_FORMAT = os.environ.get("ELEVENLABS_STT_FILE_FORMAT", "").strip()


def _env_flag(name: str, *, default: bool) -> bool:
    """env 삼단 스위치 — 미설정이면 default, 그 외에는 명시값만 믿는다."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "on", "true", "yes")


# E13-1a: keyterms 기본 on. 내장 Whisper 가 initial_prompt 를 늘 받는 것과 대칭이어야
# 두 백엔드 비교가 공정하다. 노드에서 끄려면 ELEVENLABS_STT_KEYTERMS=off (코드 배포 불필요).
EL_STT_KEYTERMS_ENABLED = _env_flag("ELEVENLABS_STT_KEYTERMS", default=True)
# E13-1b: 표기 보정 기본 on. 끄려면 ELEVENLABS_STT_NORMALIZE=off.
EL_STT_NORMALIZE_ENABLED = _env_flag("ELEVENLABS_STT_NORMALIZE", default=True)

# 문서상 배치 한도: 1000개 · 각 50자 · 5단어 이하 · `<>{}[]\` 불가.
# **우리 쪽에서 먼저 거른다** — 서버 400(invalid_keyword_length)은 permanent 라
# 리서치 결과에 긴 작품명 하나가 섞였다고 전사 전체가 죽으면 안 된다.
_KEYTERM_MAX_CHARS = 50
_KEYTERM_MAX_WORDS = 5
_KEYTERM_MAX_COUNT = 1000
_KEYTERM_BANNED = set("<>{}[]\\")

# ISO-639-1 → ISO-639-3. 발주서 예시가 'kor' 이고 문서상 둘 다 받는다 —
# 모르는 코드는 그대로 넘긴다(서버가 판단).
_LANG_ISO3 = {"ko": "kor", "en": "eng", "ja": "jpn", "zh": "zho", "es": "spa", "fr": "fra"}

# ── cue 묶음 규칙 (내장 Whisper 경로와 같은 좌표계·같은 결) ───────────────────
# Whisper 경로: VAD min_silence 500ms 로 segment 를 끊고, 30자 넘는 segment 는 문장
# 종결부호에서 재분할한다. Scribe 는 word 단위로만 오므로 같은 기준을 여기서 준다.
_CUE_GAP_SEC = 0.5          # 단어 사이 공백이 이 이상이면 새 cue (Whisper VAD 500ms)
_CUE_MAX_CHARS = 44         # merge_subtitle_segments 의 max_total_chars **기본값**과 동일.
#                             파이프라인은 40(정본 경로)·config 계산값(variant 경로)으로 다시
#                             부르므로 여기서 44 로 묶은 cue 를 merge 가 한 번 더 쪼갠다.
#                             값을 40 으로 맞추지 않는 이유: E11·E13 이 이 수치로 낸 전사를
#                             바꾸게 된다(회귀). 정본은 merge 쪽이고 여기는 상한일 뿐이다.
_CUE_MAX_DURATION_SEC = 6.0  # merge_subtitle_segments 의 max_duration_sec 와 동일
# ⚠ E13-0 조사 결과: 두 백엔드의 cue 수가 갈리는 주범은 이 규칙이다. Scribe 는 한국어에
# 문장부호를 찍어 주고 Whisper 는 거의 안 찍어서, **같은 규칙이 Scribe 에서만 발동**한다
# (가왕쇼 EP1: 898 → 1468). 백엔드 선택은 채널 단위라 채널마다 자막 리듬이 다른 건
# 설계상 허용 범위 — 한쪽이 일관되게 낫지도 않다(같은 편에서 Whisper 는 24.7초짜리
# cue 를 냈다). **원인만 못 박아 두고 규칙은 그대로 둔다.**
_SENTENCE_END = re.compile(r"[.!?…]$")
# 확신도가 낮은 줄 = 검수자가 먼저 볼 줄. 임계값은 **표시용**이며 줄을 버리지 않는다
# (E13-2 실측: 저확신 3건 중 2건이 멀쩡한 한국어였다 — 임계로 자르면 대사가 사라진다).
_LOW_CONFIDENCE_LOGPROB = -0.60
# 저확신 구간을 파이프라인이 자막 줄에 표시할 수 있게 보관하는 상한(방어용).
_LOW_CONF_SPAN_CAP = 2000

# ── P2-a 언어 이탈 판정 ──────────────────────────────────────────────────────
# 요청 언어의 문자군 + 라틴문자(고유명사·약어) + 숫자·문장부호가 '정상'이고,
# 그 밖의 **문자(letter)** 가 지배적인 cue 를 버린다.
#
# 왜 절반인가: 한국어 자막에 한자가 정당하게 들어가는 경우는 한글 문장 안의 주석
# (`이 회장(李 會長)이`)이라 한자 비율이 낮다. 반대로 언어 이탈은 그 줄 전체가
# 다른 언어다(`謝 謝` = 100%). 실측 두 편에서 이 기준으로 잡히는 건 `謝 謝` 뿐이고
# 정상 한국어 오탐은 0건이다. 경계를 낮추면(예: 20%) 한자 주석까지 버리기 시작한다.
_FOREIGN_SCRIPT_RATIO = 0.5
_HANGUL_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏ꥠ-꥿]")
_LATIN_RE = re.compile(r"[A-Za-zÀ-ɏ]")
# 요청 언어 → '정상'으로 볼 문자군. 여기 없는 언어는 판정 자체를 건너뛴다
# (문자군을 모르면서 버리면 멀쩡한 자막이 사라진다).
_LANG_SCRIPTS: dict[str, str] = {
    "ko": "hangul", "kor": "hangul",
    "en": "latin", "eng": "latin",
    "es": "latin", "spa": "latin",
    "fr": "latin", "fra": "latin",
}

_NORMALIZE_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "transcribe_normalize_ko.json"


class ElevenLabsSTTError(RuntimeError):
    """Scribe 호출 실패. permanent(4xx) 는 재시도 없이, transient 는 재시도 후 올라온다."""


# ── 사용량/비용 집계 (run_log 기록용) ────────────────────────────────────────
def _empty_usage() -> dict:
    return {
        "requests": 0,
        "requests_with_keyterms": 0,
        "keyterms_sent": 0,
        "audio_duration_secs": 0.0,
        "audio_duration_secs_keyterms": 0.0,
        "api_elapsed_sec": 0.0,
        "low_confidence_lines": [],
        "low_confidence_count": 0,
        "dropped_language_escape": [],
        "normalized": {},
        "language_mismatch": [],
    }


_USAGE: dict = _empty_usage()
# 직전 transcribe() 한 번이 낸 저확신 구간(파일 상대시간). 호출부가 청크 오프셋을
# 얹어 절대시간으로 만든다 — SpeechSegment(3필드)를 늘리지 않고 옆으로 흘려보내는 통로다.
_LAST_LOW_CONF_SPANS: list[dict] = []


def reset_usage() -> None:
    _USAGE.clear()
    _USAGE.update(_empty_usage())
    _LAST_LOW_CONF_SPANS.clear()


def take_low_confidence_spans() -> list[dict]:
    """직전 `transcribe()` 가 낸 저확신 구간을 꺼내며 비운다(파일 시작 기준 상대시간).

    `transcribe_chunks` 가 청크마다 곧바로 가져가 `chunk.start_sec` 를 더한다.
    비우는 이유: 다음 청크 결과에 이전 청크 구간이 섞이면 자막 표시가 엉뚱한 줄에 붙는다.
    """
    out = list(_LAST_LOW_CONF_SPANS)
    _LAST_LOW_CONF_SPANS.clear()
    return out


def usage_summary() -> dict:
    """run_log 에 남길 요약 — 분 단위 과금이라 audio_duration_secs 가 정산 근거다."""
    secs = float(_USAGE["audio_duration_secs"])
    kt_secs = min(float(_USAGE["audio_duration_secs_keyterms"]), secs)
    base_secs = max(0.0, secs - kt_secs)
    # keyterms 를 실제로 실어 보낸 요청만 상향 요율로 센다 — 켜 놓고 인물 리서치가
    # 비어 폼에 안 실린 청크까지 $0.27 로 계산하면 정산이 틀린다.
    est = (base_secs / 3600.0 * EL_STT_USD_PER_AUDIO_HOUR
           + kt_secs / 3600.0 * EL_STT_KEYTERMS_USD_PER_AUDIO_HOUR)
    return {
        "backend": "elevenlabs",
        "model_id": EL_STT_MODEL_ID,
        "requests": int(_USAGE["requests"]),
        "requests_with_keyterms": int(_USAGE["requests_with_keyterms"]),
        "keyterms_sent": int(_USAGE["keyterms_sent"]),
        "audio_duration_secs": round(secs, 2),
        "audio_duration_secs_keyterms": round(kt_secs, 2),
        "api_elapsed_sec": round(float(_USAGE["api_elapsed_sec"]), 1),
        "usd_per_audio_hour": EL_STT_USD_PER_AUDIO_HOUR,
        "usd_per_audio_hour_keyterms": EL_STT_KEYTERMS_USD_PER_AUDIO_HOUR,
        "estimated_usd": round(est, 6),
        # 검수자가 어디를 볼지 알려 주는 목록(내장 전사에는 없는 이 백엔드의 이득).
        "low_confidence_lines": list(_USAGE["low_confidence_lines"])[:20],
        "low_confidence_count": int(_USAGE["low_confidence_count"]),
        # 결과를 바꾼 두 가지 — 흔적 없이 지우거나 고쳐 쓰지 않는다.
        # 버린 줄은 **전량**(상한 200) 싣는다: '오탐 0건'은 사람이 목록을 다 보고서야
        # 판정되는 조건이라, 앞 20건만 보여 주면 검수가 성립하지 않는다.
        "dropped_language_escape": list(_USAGE["dropped_language_escape"])[:200],
        "dropped_language_escape_count": len(_USAGE["dropped_language_escape"]),
        "normalized": dict(_USAGE["normalized"]),
        "language_mismatch": list(_USAGE["language_mismatch"])[:10],
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
    # 한 프로세스에서 두 번 전사(재개·A/B)하면 요금이 두 배로 보인다 — 여기서 리셋한다.
    reset_usage()
    keyterms = _build_keyterms(work_title, character_names) if EL_STT_KEYTERMS_ENABLED else None
    if EL_STT_KEYTERMS_ENABLED:
        if keyterms:
            print(f"  [Scribe] keyterms {len(keyterms)}개 사용 "
                  f"(요율 ${EL_STT_KEYTERMS_USD_PER_AUDIO_HOUR}/오디오시간): "
                  f"{', '.join(keyterms[:8])}{' …' if len(keyterms) > 8 else ''}")
        else:
            print(f"  [Scribe] keyterms on 이지만 리서치 결과(작품명·인물명)가 비어 "
                  f"보낼 항목이 없습니다 — 기본 요율 ${EL_STT_USD_PER_AUDIO_HOUR}/오디오시간")
    else:
        print("  [Scribe] keyterms off (ELEVENLABS_STT_KEYTERMS) — "
              "내장 Whisper 의 initial_prompt 와 비대칭 조건이 됩니다")

    def _transcriber(media_path: Path) -> list[SpeechSegment]:
        return transcribe(Path(media_path), language=language, keyterms=keyterms)

    return _transcriber


def _build_keyterms(work_title: str | None, character_names: list[str] | None) -> list[str]:
    """고유명사 바이어싱 목록 — Whisper initial_prompt 의 인물명 주입과 같은 의도.

    문서 제한(50자·5단어·`<>{}[]\\` 불가·1000개)을 **여기서** 지킨다. 서버가 주는
    400 invalid_keyword_length 는 permanent 라, 리서치 결과에 긴 항목 하나가 섞였다고
    전사 전체가 죽으면 안 된다. 버린 항목은 로그에 남긴다(조용히 사라지지 않는다).
    """
    terms: list[str] = []
    dropped: list[str] = []
    for t in [work_title or ""] + list(character_names or []):
        t = " ".join(str(t or "").split())     # 개행·연속 공백 정리(폼 필드 하나로 나간다)
        if not t or t in terms:
            continue
        if (len(t) > _KEYTERM_MAX_CHARS
                or len(t.split(" ")) > _KEYTERM_MAX_WORDS
                or any(ch in _KEYTERM_BANNED for ch in t)):
            dropped.append(t)
            continue
        terms.append(t)
    if dropped:
        print(f"  [Scribe] keyterms 제외 {len(dropped)}건(50자·5단어·금지문자 제한): "
              f"{', '.join(d[:20] for d in dropped[:5])}")
    if len(terms) > _KEYTERM_MAX_COUNT:
        print(f"  [Scribe] keyterms {len(terms)}개 → 상한 {_KEYTERM_MAX_COUNT}개로 절단")
        terms = terms[:_KEYTERM_MAX_COUNT]
    return terms


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

    # E13-2a: 언어 이탈 cue 차단 → E13-1b: 표기 보정. 둘 다 cue 를 다 묶은 **뒤**다
    # (경계 규칙이 원문 기준으로 계산돼야 cue 분할이 두 실행에서 같다).
    segments, confidences = _drop_language_escapes(segments, confidences, language=language)
    segments = _normalize_notation(segments)

    # 과금·정산 근거. 응답에 없으면(스키마 변동) 우리가 올린 오디오 길이로 대체한다.
    dur = payload.get("audio_duration_secs")
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        dur = _sum_segment_span(segments)
    dur = max(0.0, dur)
    _USAGE["requests"] += 1
    _USAGE["audio_duration_secs"] += dur
    if keyterms:
        _USAGE["requests_with_keyterms"] += 1
        _USAGE["keyterms_sent"] = len(keyterms)
        _USAGE["audio_duration_secs_keyterms"] += dur

    _log_result(payload, segments, confidences, dur, language=language)
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


# ── P2-a: 언어 이탈 cue 차단 ────────────────────────────────────────────────
def script_profile(text: str) -> tuple[int, int, int]:
    """(요청언어 밖 문자수, 한글 수, 라틴 수). 숫자·공백·문장부호는 세지 않는다."""
    hangul = latin = foreign = 0
    for ch in text or "":
        if _HANGUL_RE.match(ch):
            hangul += 1
        elif _LATIN_RE.match(ch):
            latin += 1
        elif unicodedata.category(ch).startswith("L"):
            foreign += 1
    return foreign, hangul, latin


def is_language_escape(text: str, *, language: str) -> bool:
    """요청 언어 밖 문자가 지배적인가 — logprob 과 무관하게 문자만 보고 판정한다.

    `謝 謝`(한자 100%)는 잡고, 한자 주석이 섞인 한국어 문장(`이 회장(李 會長)이`,
    한자 비율 ~15%)은 통과한다. 문자군을 모르는 언어는 판정하지 않는다.
    """
    script = _LANG_SCRIPTS.get((language or "").lower())
    if script is None:
        return False
    foreign, hangul, latin = script_profile(text)
    native = hangul if script == "hangul" else latin
    # 라틴 문자는 어느 언어에서든 고유명사·약어로 정상이라 '이탈'로 세지 않는다.
    denom = foreign + native + (latin if script != "latin" else 0)
    if denom <= 0:
        return False
    return (foreign / denom) >= _FOREIGN_SCRIPT_RATIO


def _drop_language_escapes(
    segments: list[SpeechSegment],
    confidences: list[float],
    *,
    language: str,
) -> tuple[list[SpeechSegment], list[float]]:
    """언어 이탈 cue 를 버린다. **버린 줄은 전량 로그·run_log 에 남긴다** —
    한국어 편에 한자가 정당하게 들어가는 판이 있을 수 있고, 조용히 사라지면 아무도 못 잡는다."""
    kept: list[SpeechSegment] = []
    kept_conf: list[float] = []
    for i, seg in enumerate(segments):
        if is_language_escape(seg.text, language=language):
            foreign, hangul, latin = script_profile(seg.text)
            print(f"    [Scribe-언어이탈 drop] {seg.start_sec:.1f}~{seg.end_sec:.1f}s "
                  f"({language} 요청, 이탈문자 {foreign}자/한글 {hangul}자/라틴 {latin}자) "
                  f"{seg.text[:30]!r}")
            _USAGE["dropped_language_escape"].append({
                "start_sec": round(float(seg.start_sec), 2),
                "end_sec": round(float(seg.end_sec), 2),
                "text": seg.text[:60],
                "foreign_chars": foreign,
                "native_chars": hangul + latin,
            })
            continue
        kept.append(seg)
        kept_conf.append(confidences[i] if i < len(confidences) else 0.0)
    return kept, kept_conf


# ── P1-b: 표기 보정(완전 토큰 일치 + 십의 자리 수사) ─────────────────────────
_NORM_CACHE: dict | None = None


def _load_normalize_rules() -> dict:
    """사전은 코드가 아니라 데이터 — app/data/transcribe_normalize_ko.json 한 곳에서만 고친다."""
    global _NORM_CACHE
    if _NORM_CACHE is not None:
        return _NORM_CACHE
    rules: dict = {"token_map": {}, "tens_re": None}
    try:
        raw = json.loads(_NORMALIZE_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  [Scribe-표기보정] 사전 로드 실패({e}) — 보정 없이 진행")
        _NORM_CACHE = rules
        return rules
    # 토큰 열이 긴 것부터 본다("아이티 업계" 가 "아이티" 보다 먼저 걸려야 한다).
    token_map = {" ".join(str(k).split()): str(v)
                 for k, v in (raw.get("token_map") or {}).items() if str(k).strip()}
    rules["token_map"] = dict(sorted(token_map.items(),
                                     key=lambda kv: -len(kv[0].split(" "))))
    units = [str(u) for u in (raw.get("tens_units") or []) if str(u).strip()]
    parts = [str(p) for p in (raw.get("tens_particles") or []) if str(p).strip()]
    if units:
        # 십의 자리 수사(10~99)만. 백·천·만은 '만 원'처럼 한글 표기가 정상인 경우가 많다.
        # 긴 것부터 교대해야 '년대'가 '년'에 먼저 먹히지 않는다.
        unit_alt = "|".join(sorted(map(re.escape, units), key=len, reverse=True))
        part_alt = "|".join(sorted(map(re.escape, parts), key=len, reverse=True))
        tail = f"({part_alt})?" if parts else "()"
        rules["tens_re"] = re.compile(
            rf"^([일이삼사오육칠팔구]?)십([일이삼사오육칠팔구]?)({unit_alt}){tail}$")
    _NORM_CACHE = rules
    return rules


_SINO_DIGIT = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
               "육": 6, "칠": 7, "팔": 8, "구": 9}


def normalize_notation_text(text: str) -> tuple[str, dict[str, int]]:
    """한 cue 의 표기 보정 → (보정된 텍스트, {무엇→무엇: 건수}). 순수 — 테스트 대상.

    규칙은 둘뿐이고 **둘 다 공백으로 끊은 토큰 단위 완전 일치**다:

    1. `token_map` — 토큰(또는 연속 토큰 열) 전체가 사전 키와 같을 때만 바꾼다.
       부분 문자열 치환은 하지 않는다: `아이티`가 들어간 멀쩡한 단어(아이티오·Haiti)를
       깨뜨린다. 그래서 `IT업계`는 단독 토큰이 아니라 `아이티 업계` 열로만 건다.
    2. 십의 자리 수사 + 단위(+조사) — `삼십년` → `30년`. 수사 자체가 모호하지 않은
       패턴이라 조사까지는 허용한다(`삼십년을` → `30년을`).
    """
    if not text or not text.strip():
        return text, {}
    rules = _load_normalize_rules()
    tokens = text.split(" ")
    out_tokens: list[str] = []
    changes: dict[str, int] = {}
    i = 0
    while i < len(tokens):
        matched = False
        for key, val in rules["token_map"].items():
            n = len(key.split(" "))
            if n and " ".join(tokens[i:i + n]) == key:
                out_tokens.append(val)
                changes[f"{key}→{val}"] = changes.get(f"{key}→{val}", 0) + 1
                i += n
                matched = True
                break
        if matched:
            continue

        tok = tokens[i]
        tens_re = rules["tens_re"]
        m = tens_re.match(tok) if tens_re else None
        if m:
            tens = _SINO_DIGIT.get(m.group(1), 1) if m.group(1) else 1
            ones = _SINO_DIGIT.get(m.group(2), 0) if m.group(2) else 0
            new_tok = f"{tens * 10 + ones}{m.group(3)}{m.group(4) or ''}"
            out_tokens.append(new_tok)
            changes[f"{tok}→{new_tok}"] = changes.get(f"{tok}→{new_tok}", 0) + 1
        else:
            out_tokens.append(tok)
        i += 1
    return " ".join(out_tokens), changes


def _normalize_notation(segments: list[SpeechSegment]) -> list[SpeechSegment]:
    """cue 전체에 표기 보정 적용. 바꾼 내역은 건별로 집계해 로그·run_log 에 남긴다."""
    if not EL_STT_NORMALIZE_ENABLED:
        return segments
    out: list[SpeechSegment] = []
    total: dict[str, int] = {}
    for seg in segments:
        new_text, changes = normalize_notation_text(seg.text)
        for k, v in changes.items():
            total[k] = total.get(k, 0) + v
        out.append(seg if new_text == seg.text
                   else SpeechSegment(start_sec=seg.start_sec, end_sec=seg.end_sec,
                                      text=new_text))
    if total:
        for k, v in total.items():
            _USAGE["normalized"][k] = _USAGE["normalized"].get(k, 0) + v
        summary = " · ".join(f"{k} {v}건" for k, v in sorted(total.items(),
                                                            key=lambda kv: -kv[1])[:6])
        print(f"    [Scribe-표기보정] {sum(total.values())}건 — {summary}")
    return out


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


def build_form_fields(
    *,
    language: str,
    keyterms: list[str] | None,
    is_raw: bool,
) -> list[tuple[str, str]]:
    """multipart 폼 필드를 (이름, 값) **열**로 만든다 — 순수, 테스트 대상.

    ⚠ `keyterms` 는 **같은 이름의 필드를 반복**해야 한다(`keyterms=A`, `keyterms=B`).
    배열 하나를 `json.dumps` 로 담아 보내면 서버가 그 JSON 문자열 전체를 키텀 하나로
    보고 400 `invalid_keyword_length`(All keywords must be less than 50 characters)를
    준다 — elevenlabs-python #819 의 v2.59.0 회귀와 같은 형태다. requests 는 dict 대신
    list-of-tuples 를 주면 이름 중복을 그대로 실어 보낸다.
    """
    fields: list[tuple[str, str]] = [
        ("model_id", EL_STT_MODEL_ID),
        ("language_code", _LANG_ISO3.get((language or "ko").lower(), language or "ko")),
        ("timestamps_granularity", "word"),
        # 기본이 true 다 — 켜두면 자막에 '(laughter)' 가 섞인다.
        ("tag_audio_events", "false"),
        ("diarize", "false"),
    ]
    if is_raw:
        fields.append(("file_format", "pcm_s16le_16"))
    for term in keyterms or []:
        fields.append(("keyterms", term))
    return fields


def _is_keyterm_rejection(status: int, body: str) -> bool:
    """서버가 keyterms 때문에 거절했는가 — 직렬화 계약이 또 바뀐 신호."""
    if status not in (400, 422):
        return False
    low = (body or "").lower()
    return "keyword" in low or "keyterm" in low


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
    믿은 채 종전 전사로 발행되는 것이 이 기능에서 제일 나쁜 실패다. keyterms 거절도
    마찬가지다: keyterms 를 빼고 몰래 재시도하면 정확도 조건이 소리 없이 달라진다.
    """
    import requests

    fields = build_form_fields(language=language, keyterms=keyterms, is_raw=is_raw)
    headers = {"xi-api-key": api_key}
    last_err: Exception | None = None
    for attempt in range(_EL_RETRIES + 1):
        t0 = time.time()
        try:
            with audio_path.open("rb") as fh:
                files = {"file": (audio_path.name, fh, "application/octet-stream")}
                resp = requests.post(EL_STT_URL, headers=headers, data=fields, files=files,
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
            if keyterms and _is_keyterm_rejection(resp.status_code, resp.text):
                raise ElevenLabsSTTError(
                    f"ElevenLabs STT {resp.status_code} — keyterms 를 거절했습니다: "
                    f"{resp.text[:200]}. 계약은 '같은 이름 폼 필드 반복'"
                    f"(keyterms=A, keyterms=B)이고 항목당 50자·5단어 이하입니다. "
                    f"서버 계약이 바뀌었다면 build_form_fields 를 고치세요. "
                    f"급하면 ELEVENLABS_STT_KEYTERMS=off 로 끄고 돌릴 수 있지만, "
                    f"그러면 내장 Whisper 의 initial_prompt 와 비대칭 조건이 됩니다 "
                    f"(고유명사가 음차로 뭉개집니다).")
            last_err = ElevenLabsSTTError(
                f"ElevenLabs STT {resp.status_code}: {resp.text[:300]}")
            if resp.status_code != 429 and resp.status_code < 500:
                break                                # permanent — 재시도 무의미
        if attempt < _EL_RETRIES:
            time.sleep(2 * (attempt + 1))
    raise ElevenLabsSTTError(f"ElevenLabs Scribe 전사 실패({audio_path.name}) — {last_err}")


def _log_result(payload: dict, segments: list, confidences: list[float], dur: float,
                *, language: str = "ko") -> None:
    lang = _get(payload, "language_code", "?")
    prob = _get(payload, "language_probability", None)
    prob_s = f" (확률 {float(prob):.2f})" if isinstance(prob, (int, float)) else ""
    rate = EL_STT_KEYTERMS_USD_PER_AUDIO_HOUR if _USAGE["requests_with_keyterms"] else \
        EL_STT_USD_PER_AUDIO_HOUR
    usd = dur / 3600.0 * rate
    print(f"  [Scribe] {EL_STT_MODEL_ID} 전사 완료: {len(segments)}개 cue · "
          f"언어 {lang}{prob_s} · 오디오 {dur:.1f}s · 추정 ${usd:.5f}")

    # 요청 언어와 응답 언어가 다르면 그 자체가 신호다 — 소스가 섞였거나 언어 코드가 틀렸다.
    want = _LANG_ISO3.get((language or "ko").lower(), language or "ko")
    got = str(lang or "")
    if got and got not in (want, (language or "").lower()):
        print(f"    [Scribe-언어불일치] 요청 {want} ≠ 응답 {got} — 소스 언어나 "
              f"language_code 를 확인하세요(언어 이탈 판정은 요청 언어 기준으로 돕니다)")
        _USAGE["language_mismatch"].append({"requested": want, "detected": got})

    # 확신도 낮은 줄 = 검수자가 먼저 볼 줄. **결과는 안 바꾸고** 표시만 한다
    # (실측 3건 중 2건이 멀쩡한 한국어였다 — 임계로 자르면 실제 대사가 사라진다).
    low = [(c, s) for c, s in zip(confidences, segments) if c < _LOW_CONFIDENCE_LOGPROB]
    low.sort(key=lambda x: x[0])
    _USAGE["low_confidence_count"] += len(low)
    for c, s in low[:3]:
        print(f"    [Scribe-확신도낮음] {s.start_sec:.1f}~{s.end_sec:.1f}s "
              f"logprob={c:.2f} {s.text[:30]!r}")
    for c, s in low:
        if len(_USAGE["low_confidence_lines"]) < 20:
            _USAGE["low_confidence_lines"].append({
                "start_sec": round(float(s.start_sec), 2),
                "end_sec": round(float(s.end_sec), 2),
                "logprob": round(float(c), 3),
                "text": s.text[:60],
            })
        # 자막 줄 표시(P2-b)용 구간은 상한까지 전부 모은다 — 호출부가 곧바로 가져간다.
        if len(_LAST_LOW_CONF_SPANS) < _LOW_CONF_SPAN_CAP:
            _LAST_LOW_CONF_SPANS.append({
                "start_sec": round(float(s.start_sec), 2),
                "end_sec": round(float(s.end_sec), 2),
                "logprob": round(float(c), 3),
            })
