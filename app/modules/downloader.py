from __future__ import annotations

import hashlib
import re
from pathlib import Path


_YOUTUBE_PATTERN = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)"
)


def is_youtube_url(s: str) -> bool:
    return bool(_YOUTUBE_PATTERN.search(s))


def _url_hash(url: str) -> str:
    """URL을 8자리 hex 해시로 변환합니다."""
    return hashlib.sha256(url.encode()).hexdigest()[:8]


def download_youtube(url: str, cache_dir: Path) -> Path:
    """yt-dlp로 YouTube 영상을 다운로드하고 로컬 경로를 반환합니다.

    cache_dir 안에 URL 해시 이름으로 저장하며, 이미 존재하면 재사용합니다.
    """
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        raise RuntimeError(
            "yt-dlp가 설치되지 않았습니다. 'pip install yt-dlp'를 실행하세요."
        )

    cache_dir.mkdir(parents=True, exist_ok=True)

    # 캐시 파일 경로: {hash}.mp4
    cached_path = cache_dir / f"{_url_hash(url)}.mp4"
    if cached_path.exists():
        print(f"  [캐시] 이미 다운로드된 영상 재사용: {cached_path.name}")
        return cached_path

    # 임시 파일명으로 다운로드 후 해시 이름으로 변경
    tmp_tmpl = str(cache_dir / f"{_url_hash(url)}_tmp.%(ext)s")

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": tmp_tmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    tmp_path = cache_dir / f"{_url_hash(url)}_tmp.mp4"
    if not tmp_path.exists():
        candidates = list(cache_dir.glob(f"{_url_hash(url)}_tmp.*"))
        if not candidates:
            raise FileNotFoundError(f"다운로드 후 파일을 찾을 수 없습니다: {tmp_tmpl}")
        tmp_path = candidates[0]

    tmp_path.rename(cached_path)
    return cached_path
