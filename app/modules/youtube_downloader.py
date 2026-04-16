from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class YouTubeAssets:
    video_path: Path
    subtitle_path: Path | None


def download_youtube_assets(
    url: str,
    out_dir: Path,
    lang: str = "ko",
) -> YouTubeAssets:
    """YouTube URL에서 영상(mp4)과 자막을 다운로드.

    자막 우선순위: 수동 업로드 ko → 자동 생성 ko → 없음(None)

    out_dir/
      ├── source.mp4
      └── source.<lang>.srt  (없으면 subtitle_path=None)
    """
    import yt_dlp  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(out_dir / "source.%(ext)s")

    ydl_opts = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang, f"{lang}-*", "ko", "ko-KR"],
        "subtitlesformat": "srt/best",
        "postprocessors": [
            {"key": "FFmpegSubtitlesConvertor", "format": "srt"},
        ],
        "quiet": False,
        "no_warnings": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    # 영상 파일 찾기 (확장자 우선순위: mp4 > mkv > webm)
    video_path: Path | None = None
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        matches = sorted(out_dir.glob(f"source{ext}"))
        if matches:
            video_path = matches[0]
            break
    if video_path is None:
        # Fallback: source.* 중 자막(.vtt/.srt) 아닌 첫 번째 파일
        for p in sorted(out_dir.glob("source.*")):
            if p.suffix.lower() not in {".srt", ".vtt", ".ass"}:
                video_path = p
                break
    if video_path is None:
        raise RuntimeError(f"YouTube 영상 다운로드 실패: {url}")

    # 자막 파일 찾기 — 지정 언어 우선, 이후 폴백
    subtitle_path: Path | None = None
    for pattern in (
        f"source.{lang}.srt",
        f"source.{lang}-*.srt",
        "source.ko.srt",
        "source.ko-*.srt",
        "source.*.srt",
    ):
        matches = sorted(out_dir.glob(pattern))
        if matches:
            subtitle_path = matches[0]
            break

    return YouTubeAssets(video_path=video_path, subtitle_path=subtitle_path)
