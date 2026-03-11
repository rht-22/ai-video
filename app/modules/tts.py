# 코드의 핵심 역할
# 이 모듈은 전달받은 텍스트를 음성 파일(.mp3 또는 .wav)로 변환합니다. 주로 구글의 TTS 엔진인 gTTS를 사용하며, 만약 라이브러리가 없거나 생성에 실패할 경우를 대비한 안전장치(정적 오디오 생성)까지 갖추고 있습니다.
from __future__ import annotations

import subprocess
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command


def synthesize_tts(text: str, output_path: Path, lang: str = "ko") -> Path:
    if _has_gtts():
        return _synthesize_gtts(text, output_path, lang)
    return _synthesize_silence(output_path, duration_sec=1.0)

# 동적 라이브러리 체크
# 다른 모듈들과 마찬가지로 실행 시점에 gtts 설치 여부를 확인하여 환경에 유연하게 대응
def _has_gtts() -> bool:
    import importlib.util

    return importlib.util.find_spec("gtts") is not None

# 구글 TTS 연동 (_synthesize_gtts)
# gTTS 라이브러리: Google Translate의 TTS API를 활용하여 자연스러운 한국어(또는 지정된 언어) 음성을 생성합니다.
# 언어 설정 (lang="ko"): 기본적으로 한국어 설정을 지원하여 국내 쇼츠 제작에 최적화되어 있습니다.
def _synthesize_gtts(text: str, output_path: Path, lang: str) -> Path:
    from gtts import gTTS

    tts = gTTS(text=text, lang=lang)
    tts.save(str(output_path))
    return output_path

# 폴백 로직: 무음 생성 (_synthesize_silence)
# 이 부분이 개발자의 꼼꼼함이 돋보이는 지점입니다.
# TTS 생성에 실패하거나 라이브러리가 없을 때 프로세스가 멈추지 않도록, **FFmpeg의 lavfi 필터(aevalsrc)**를 사용하여 지정된 길이만큼의 '무음(Silence)' 파일을 강제로 만듭니다.
# 이렇게 하면 나중에 오디오 믹싱 단계에서 파일이 없어 에러가 나는 상황을 방지할 수 있습니다.
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
