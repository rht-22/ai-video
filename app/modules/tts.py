from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


# ── E11 (2026-08-21): KR 내레이션 합성의 1순위 백엔드는 ElevenLabs ──────────────
# ELEVENLABS_API_KEY 가 있으면 ElevenLabs, 없으면 edge-tts 폴백(stdout 에 명시 —
# 조용한 대체 금지). API 호출 실패는 짧은 재시도 후 **즉시 실패**다: edge-tts 로
# 조용히 넘어가면 같은 채널 목소리가 편마다 달라진다(fail-loud).
#
# voice/speed 의 **라벨 계약은 E11 이전과 동일**하다 — 편집실(edVoiceSel·edSpeedSel)·
# edit_overrides/v2·체크포인트 cue 에 이미 실린 값이라, 라벨을 바꾸면 하위호환이 깨진다.
# 라벨 전수 커버는 tests/test_e11_tts_elevenlabs.py 가 대조한다.

# 라벨 → ElevenLabs premade voice_id. premade 라이브러리의 전 계정 공통 안정 id 다.
# ElevenLabs 에는 pitch 파라미터가 없으므로 high/low 피치 변형은 목소리 자체를
# 달리 골라 재현한다. 교체는 여기 한 곳 — 채널 취향이 갈리면 id 만 바꾼다.
EL_VOICE_PRESETS: dict[str, str] = {
    # ── 한국어 내레이션 4종 (multilingual 모델이 한국어로 말한다) ──
    "ko_female":      "EXAVITQu4vr4xnSDxMaL",  # Sarah — 차분·명료한 여성 (기본)
    "ko_female_high": "FGY2WhTYpPnrIDTdsKH5",  # Laura — 밝고 통통 튀는 여성
    "ko_male":        "JBFqnCBsd6RMkjVDRZzb",  # George — 따뜻한 내레이션 남성
    "ko_male_low":    "nPczCjzI2devNBz1zQrb",  # Brian — 깊고 묵직한 남성
    # ── 트렌드 chat_* 4종 (구 edge multilingual 프리셋의 대응) ──
    "chat_emma":      "cgSgspJ2msm6clMCkdW9",  # Jessica — 밝고 명료한 여성
    "chat_brian":     "iP95p4xoKVk53GoZ742B",  # Chris — 친근한 캐주얼 남성
    "chat_seraphina": "pFZP5JQG7iQjIQuC4Bku",  # Lily — 차분한 영국계 여성
    "chat_florian":   "onwK4e9ZLuTAKqWW03F9",  # Daniel — 차분한 영국계 남성
}

# 라벨 → voice_settings.speed. 문서 허용 범위 0.7~1.2(1.0=기본, 전 모델·전 보이스).
# edge-tts rate(fast/slow ±10%)는 체감이 약해 '속도가 안 먹힌다'로 보였다 —
# ElevenLabs 는 스펙트럼 끝(0.7·1.2)이 뚜렷하고 fast/slow(0.85·1.1)도 귀로 구분된다.
EL_SPEED: dict[str, float] = {
    "very_slow": 0.7,
    "slow":      0.85,
    "normal":    1.0,
    "fast":      1.1,
    "very_fast": 1.2,
}

# 모델: 한국어 + voice_settings.speed 지원이 필수 조건. eleven_multilingual_v2 가
# 품질 우선 기본(API 기본값이기도 하다). 내레이션은 편당 cue 2~5개 × 수십 자라
# flash(단가 절반)와의 비용 차이가 미미해 품질을 택했다 — 단가가 문제되면 env 로
# eleven_flash_v2_5 로 바꾼다(코드 배포 불필요).
EL_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
EL_OUTPUT_FORMAT = "mp3_44100_128"
_EL_RETRIES = 2      # 첫 시도 제외 재시도 횟수 — 429(쿼터·동시성)·5xx·네트워크만


# Voice 프리셋 라벨 → (Edge TTS voice id, pitch). **폴백 경로 전용**(키 없는 개발 환경).
VOICE_PRESETS: dict[str, tuple[str, str]] = {
    # ── 자연스러운 한국어 (우선 사용) ──
    "ko_female":      ("ko-KR-SunHiNeural",                  "+0Hz"),    # 기본 한국 여성
    "ko_female_high": ("ko-KR-SunHiNeural",                  "+30Hz"),   # 밝은 한국 여성 (트렌드 톤)
    "ko_male":        ("ko-KR-InJoonNeural",                 "+0Hz"),    # 기본 한국 남성
    "ko_male_low":    ("ko-KR-InJoonNeural",                 "-15Hz"),   # 낮은 한국 남성 (묵직)
    # ── 트렌드 multilingual (작품 톤 어울릴 때만) ──
    "chat_emma":      ("en-US-EmmaMultilingualNeural",       "+0Hz"),    # 밝고 명료한 챗봇 여성 (en)
    "chat_brian":     ("en-US-BrianMultilingualNeural",      "+0Hz"),    # 친근한 캐주얼 남성 (en)
    "chat_seraphina": ("de-DE-SeraphinaMultilingualNeural",  "+0Hz"),    # 차분한 유럽계 여성 (de)
    "chat_florian":   ("de-DE-FlorianMultilingualNeural",    "+0Hz"),    # 차분한 유럽계 남성 (de)
}

# 속도 라벨 → Edge TTS rate 문자열. **폴백 경로 전용**.
SPEED_TO_RATE: dict[str, str] = {
    "very_slow": "-25%",
    "slow":      "-10%",
    "normal":    "+0%",
    "fast":      "+10%",
    "very_fast": "+25%",
}

DEFAULT_VOICE = "ko_female"
DEFAULT_SPEED = "normal"

# ── E12 (2026-08-22): 편집실이 ElevenLabs voice_id 를 직접 고르는 어휘 ────────
# voice = "ko_female" | "chat_*" | …        (지금 그대로 — 한 글자도 안 바뀐다)
#       | "elevenlabs:{voice_id}"            (신설 — 그 id 로 바로 합성)
#
# **엔진은 이름표를 들지 않는다.** 사람이 읽는 이름("차분한 여성" 등)은 대시보드가 든다 —
# 목록이 두 곳에 생기면 반드시 어긋난다(계정마다 보이스 라이브러리가 달라 1:1 미러가 불가능).
# 형태는 오케스트레이터 RPC 가 먼저 거르고(영숫자 16~32자), 엔진은 같은 형태를 재확인만 한다.
EL_VOICE_PREFIX = "elevenlabs:"
_EL_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{16,32}$")

_prefixed_announced = False


def is_elevenlabs_voice(voice: str | None) -> bool:
    """편집실이 고른 ElevenLabs voice_id 인가 — 접두사 하나로만 갈린다."""
    return str(voice or "").startswith(EL_VOICE_PREFIX)


def elevenlabs_voice_id(voice: str) -> str:
    """"elevenlabs:{id}" → id. 형태가 깨졌으면 즉시 실패(permanent).

    조용히 기본 목소리로 떨어지지 않는다 — 사람은 바꿨다고 믿은 채 종전 소리로 발행된다.
    """
    vid = str(voice)[len(EL_VOICE_PREFIX):].strip()
    if not _EL_VOICE_ID_RE.match(vid):
        raise RuntimeError(
            f"ElevenLabs voice_id 형태가 아닙니다: {vid!r} "
            f"(영숫자 16~32자). 편집실 목소리 목록(ops_config.editor_tts_voices)의 "
            f"값이 잘못 실렸는지 확인하세요.")
    return vid


_backend_announced = False


def active_backend() -> str:
    """이번 프로세스가 쓸 합성 백엔드 — run_log·체크포인트 기록용(부작용 없음).

    elevenlabs > edge-tts > silence(개발 환경 최후 폴백) 순. 검수함에서 어느
    백엔드로 나간 판인지 추적할 수 있어야 한다(E11 — 조용한 대체 금지 원칙)."""
    if os.environ.get("ELEVENLABS_API_KEY"):
        return "elevenlabs"
    if _has_edge_tts():
        return "edge-tts"
    return "silence"


def _announce_backend() -> str:
    """백엔드를 stdout 에 프로세스당 1회 명시한다 — 키가 빠진 노드가 edge-tts 로
    조용히 도는 것을 로그에서 바로 잡아내기 위한 것(E11 발주 규율)."""
    global _backend_announced
    backend = active_backend()
    if not _backend_announced:
        if backend == "elevenlabs":
            print(f"[TTS] backend=elevenlabs model={EL_MODEL_ID}")
        elif backend == "edge-tts":
            print("[TTS] backend=edge-tts — ELEVENLABS_API_KEY 없음")
        else:
            print("[TTS] backend=silence — ELEVENLABS_API_KEY·edge_tts 둘 다 없음(개발 폴백)")
        _backend_announced = True
    return backend


def _require_elevenlabs_key(voice: str) -> None:
    """접두사 목소리인데 키가 없으면 '무엇을 어디에 넣어야 하는지'까지 말하고 즉시 실패."""
    if not os.environ.get("ELEVENLABS_API_KEY"):
        raise RuntimeError(
            f"편집실이 고른 목소리 {voice!r} 는 ElevenLabs 목소리인데 "
            "ELEVENLABS_API_KEY 환경변수가 없습니다. ElevenLabs 대시보드에서 API 키를 "
            "발급해 실행 노드의 ELEVENLABS_API_KEY 에 넣으세요 "
            "(예: export ELEVENLABS_API_KEY=sk_...). edge-tts 기본 목소리로 조용히 "
            "떨어지지 않습니다 — 목소리를 바꿨다고 믿은 채 종전 소리로 발행되는 것을 막습니다.")


def _announce_prefixed_voice() -> None:
    """접두사 경로를 프로세스당 1회 명시 — 어느 경로로 나간 판인지 로그에 남는다."""
    global _prefixed_announced
    if not _prefixed_announced:
        print(f"[TTS] backend=elevenlabs model={EL_MODEL_ID} (편집실 지정 voice_id)")
        _prefixed_announced = True


def synthesize_tts(
    text: str,
    output_path: Path,
    lang: str = "ko",
    voice: str = DEFAULT_VOICE,
    speed: str = DEFAULT_SPEED,
) -> Path:
    """텍스트를 mp3로 합성. voice/speed는 프리셋 라벨.

    라벨이 프리셋에 없으면 기본값으로 폴백한다(종전 계약 유지 — 구 run 의
    narrative_* 등 레거시 라벨이 체크포인트에 남아 있다).
    """
    # E12: 접두사가 붙은 목소리는 백엔드 선택을 건너뛴다 — 사람이 그 목소리를 고른 것이라
    # edge-tts 폴백은 답이 될 수 없다. 키가 없으면 여기서 실패한다(조용한 대체 금지).
    if is_elevenlabs_voice(voice):
        _require_elevenlabs_key(voice)
        _announce_prefixed_voice()
        return _synthesize_elevenlabs(text, Path(output_path), voice=voice, speed=speed)

    backend = _announce_backend()
    if backend == "elevenlabs":
        return _synthesize_elevenlabs(text, Path(output_path), voice=voice, speed=speed)
    if backend == "edge-tts":
        voice_id, pitch = VOICE_PRESETS.get(voice, VOICE_PRESETS[DEFAULT_VOICE])
        rate = SPEED_TO_RATE.get(speed, SPEED_TO_RATE[DEFAULT_SPEED])
        return _synthesize_edge_tts(text, Path(output_path), voice_id=voice_id, rate=rate, pitch=pitch)
    return _synthesize_silence(Path(output_path), duration_sec=1.0)


def get_audio_duration(path: Path) -> float:
    """ffprobe로 mp3/audio 파일의 재생 시간(초)을 반환."""
    try:
        ffprobe = find_ffmpeg_command("ffprobe")
    except Exception:
        return 0.0
    cmd = [
        ffprobe, "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except (ValueError, AttributeError, subprocess.SubprocessError):
        return 0.0


def synthesize_tts_with_fit(
    text: str,
    output_path: Path,
    target_sec: float,
    *,
    voice: str = DEFAULT_VOICE,
    speed: str = DEFAULT_SPEED,
    shorten_fn=None,
    max_retries: int = 2,
    safety_margin: float = 0.95,
) -> tuple[str, float]:
    """target_sec 안에 들어가도록 TTS 합성. 초과 시 shorten_fn으로 텍스트를 줄여 재합성.

    shorten_fn: callable(text: str, target_chars: int) -> str
        제공되지 않으면 단순 절단 (의미 손상 위험) 폴백.

    반환: (최종 사용된 텍스트, 실제 합성 길이 초)
    """
    current_text = text
    last_actual = 0.0
    for attempt in range(max_retries + 1):
        synthesize_tts(current_text, output_path, voice=voice, speed=speed)
        actual = get_audio_duration(output_path)
        last_actual = actual
        if actual <= target_sec or actual <= 0.0:
            return current_text, actual
        if attempt == max_retries:
            print(f"  [TTS-fit-WARN] {len(current_text)}자 → {actual:.1f}s > target {target_sec:.1f}s — 잘림 감수")
            return current_text, actual
        # 줄여야 하는 비율 계산 (안전 마진 5% 추가 단축)
        shrink_ratio = (target_sec / actual) * safety_margin
        target_chars = max(8, int(len(current_text) * shrink_ratio))
        if shorten_fn:
            try:
                shorter = shorten_fn(current_text, target_chars=target_chars)
                shorter = (shorter or "").strip()
                if shorter and shorter != current_text:
                    print(f"  [TTS-fit] 재합성 시도 {attempt + 1}: {len(current_text)}자 → {len(shorter)}자 (target {target_chars}자)")
                    current_text = shorter
                    continue
            except Exception as e:
                print(f"  [TTS-fit] shorten 실패 ({e}) — 단순 절단 폴백")
        # shorten_fn 미제공 또는 실패 → 단순 절단
        current_text = current_text[:target_chars].rstrip("., \t\n") + "."
        print(f"  [TTS-fit] 재합성 시도 {attempt + 1} (단순 절단): {len(current_text)}자")
    return current_text, last_actual


def _synthesize_elevenlabs(
    text: str,
    output_path: Path,
    voice: str = DEFAULT_VOICE,
    speed: str = DEFAULT_SPEED,
) -> Path:
    """ElevenLabs REST 합성. requests 는 config.py(폰트 다운로드)와 같은 기존 의존.

    voice_settings 는 stability/similarity 를 표준값으로 **명시**한다 — 보이스별
    저장 기본값에 기대면 premade 보이스가 교체·조정될 때 채널 톤이 소리 없이
    변한다. 실패 정책: 429·5xx·네트워크만 재시도(_EL_RETRIES회, 2s·4s), 그 외
    4xx(키·voice_id·요청 오류)는 재시도해도 안 낫는다 — 즉시 실패."""
    import requests

    # E12: 접두사면 편집실이 고른 voice_id 그대로, 아니면 종전 라벨 매핑(E11 계약).
    voice_id = (elevenlabs_voice_id(voice) if is_elevenlabs_voice(voice)
                else EL_VOICE_PRESETS.get(voice, EL_VOICE_PRESETS[DEFAULT_VOICE]))
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
           f"?output_format={EL_OUTPUT_FORMAT}")
    body = {
        "text": text,
        "model_id": EL_MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "speed": EL_SPEED.get(speed, EL_SPEED[DEFAULT_SPEED]),
        },
    }
    headers = {"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}
    last_err: Exception | None = None
    for attempt in range(_EL_RETRIES + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=60)
        except requests.RequestException as e:     # 네트워크 — 재시도 대상
            last_err = e
        else:
            if resp.status_code == 200:
                output_path.write_bytes(resp.content)
                return output_path
            last_err = RuntimeError(f"ElevenLabs {resp.status_code}: {resp.text[:200]}")
            if resp.status_code != 429 and resp.status_code < 500:
                break                              # 4xx — 재시도 무의미, 즉시 실패
        if attempt < _EL_RETRIES:
            time.sleep(2 * (attempt + 1))
    # edge-tts 로 조용히 넘어가지 않는다 — 같은 채널 목소리가 편마다 달라진다(E11).
    raise RuntimeError(
        f"ElevenLabs TTS 합성 실패(voice={voice}, speed={speed}, {len(text)}자) — {last_err}")


def _has_edge_tts() -> bool:
    import importlib.util

    return importlib.util.find_spec("edge_tts") is not None


def _synthesize_edge_tts(
    text: str,
    output_path: Path,
    voice_id: str = "ko-KR-SunHiNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> Path:
    import edge_tts

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch)
        await communicate.save(str(output_path))

    # E11: '실패하면 rate/pitch 빼고 재시도'하던 무성 폴백을 제거했다 — 속도·피치가
    # 소리 없이 무시되는 유일한 지점이었고, 렌더는 성공하므로 아무도 모른다
    # (2026-07-29 폰트 조용한 대체와 같은 계열). 실패는 실패로 드러낸다.
    asyncio.run(_run())
    return output_path


def _synthesize_silence(output_path: Path, duration_sec: float) -> Path:
    ffmpeg_cmd = find_ffmpeg_command("ffmpeg")
    cmd = [
        ffmpeg_cmd,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc=0:d={duration_sec}",
        str(output_path),
    ]
    subprocess.check_call(cmd)
    return output_path
