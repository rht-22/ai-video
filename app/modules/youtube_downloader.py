from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class YouTubeAssets:
    video_path: Path
    subtitle_path: Path | None


def video_id_of(url: str) -> str:
    """URL → 유튜브 영상 ID (캐시 경로 구분용). 못 뽑으면 URL 해시로 폴백.

    다운로드 경로가 작품 제목만으로 정해지면, 같은 작품의 다른 영상(URL)을 같은
    머신에서 돌릴 때 yt-dlp 가 기존 source.mp4 를 보고 다운로드를 건너뛰어
    **이전 영상으로 쇼츠를 만든다** — 경로에 영상 ID 를 넣어 원천 단위로 가른다."""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})(?![\w-])",
                  str(url or ""))
    if m:
        return m.group(1)
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:16]


def youtube_access_opts(env: dict | None = None) -> dict:
    """YouTube 접근 옵션 — 403 회피용. 순수(env 를 받으면 그것만 본다) — 테스트 대상.

    2026-08-18 실측: 최신 yt-dlp(2026.7.4)에서도 YouTube 소스 5개 채널이 전부
    `unable to download video data: HTTP Error 403` 으로 죽었다. 메타데이터·포맷 조회는
    통과하고 스트림 요청만 막히는데, 이는 버전이 아니라 **그 IP 에서의 재생 요청을
    막은 것**이다(드라이브 소스 12건은 같은 시각 전부 성공).

    ★2026-08-18 mm-06 실측(yt-dlp 2026.7.4, LAesXpgKtbw)이 답을 뒤집었다 —
    **쿠키가 해답이었고, 클라이언트 다중화는 오히려 독이었다.**

        클라이언트      쿠키O                              쿠키X
        tv              The page needs to be reloaded      (같음)
        web_embedded    Requested format is not available  (같음)
        android_vr      통과                                Sign in to confirm you're not a bot
        web_safari      Requested format is not available  (같음)
        default         통과                                Sign in to confirm you're not a bot

    쿠키 없이는 **모든** 클라이언트가 봇 검사에서 멈춘다(08-17 14:08 까지는 안 그랬다).
    그리고 web_safari·web_embedded 는 PO 토큰 없이는 포맷 자체가 안 나온다 — 그런 것을
    목록에 섞으면 셀렉터가 못 받을 포맷을 고르거나 추출이 통째로 엎어진다. 즉 처음의
    `tv,web_safari,default` 는 **막힌 문을 세 개 두드리는 설정**이었다.

    그래서 기본값은 다시 `default` 하나다. 다중화가 필요해지는 날이 오면 그때 env 로 켠다.

    두 가지 손잡이를 둔다:
      · player_client — 기본 `default`. 유튜브가 또 바뀌면 노드에서 값만 바꾼다
        (실측 기준 대안 1순위는 android_vr — PO 토큰이 필요 없는 몇 안 되는 클라이언트다).
      · 쿠키 — **이제는 필수다**. **코드가 아니라 env 로** 켠다
        (YTDLP_COOKIES=파일경로 또는 YTDLP_COOKIES_FROM_BROWSER=chrome). 재배포 없이
        노드에서 켜고 끌 수 있어야 차단이 왔을 때 즉시 대응된다.
    """
    e = os.environ if env is None else env
    clients = (e.get("YTDLP_PLAYER_CLIENT") or "default").strip()
    opts: dict = {
        "extractor_args": {"youtube": {"player_client": [c.strip() for c in clients.split(",") if c.strip()]}},
        # 차단은 간헐적일 때가 많다 — 조각 단위 재시도를 넉넉히 준다(기본 10 → 3회 시도로는 부족)
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
    }
    cookiefile = (e.get("YTDLP_COOKIES") or "").strip()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    browser = (e.get("YTDLP_COOKIES_FROM_BROWSER") or "").strip()
    if browser:
        opts["cookiesfrombrowser"] = tuple(browser.split(":"))
    return opts


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
        **youtube_access_opts(),
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
