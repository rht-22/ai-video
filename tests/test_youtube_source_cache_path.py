"""유튜브 소스 캐시 경로 — 영상 ID 로 갈리는지 검증.

배경(2026-08-13 실측 지적): 다운로드 경로가 작품 제목만으로 정해져서, 같은 작품의
다른 영상(URL)을 같은 머신에서 돌리면 yt-dlp 가 기존 source.mp4 를 보고 다운로드를
건너뛰어 이전 영상으로 쇼츠를 만들 수 있었다. 오케스트레이터의 영상 단위 소비
개편(한 작품 = 여러 URL 순환)에서는 상시 조건이라 경로에 영상 ID 를 넣는다.
"""
from __future__ import annotations

from app.modules.youtube_downloader import video_id_of


def test_video_id_from_common_url_shapes():
    assert video_id_of("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id_of("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id_of("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id_of("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s") == "dQw4w9WgXcQ"


def test_different_videos_get_different_dirs():
    a = video_id_of("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    b = video_id_of("https://www.youtube.com/watch?v=bbbbbbbbbbb")
    assert a != b


def test_unparseable_url_falls_back_to_stable_hash():
    u = "https://example.com/some/clip"
    assert video_id_of(u) == video_id_of(u)          # 결정론 — 재실행 시 캐시 재사용
    assert video_id_of(u) != video_id_of(u + "?x=1")  # 다른 원천은 다른 경로
    assert video_id_of("")                            # 빈 값도 경로 조각으로 안전
