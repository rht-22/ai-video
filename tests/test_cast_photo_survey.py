"""배우 사진 커버리지 조사의 순수 함수 — `scripts/cast_photo_survey.py`.

이 조사가 '켤 가치가 있나'를 정하므로, 세는 규칙이 틀리면 결정이 틀린다."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cast_photo_survey import count_cast, usable  # noqa: E402


def test_count_cast_separates_url_from_surviving_file(tmp_path):
    """파일은 job 정리로 지워지지만 URL 은 남는다 — 둘을 같이 세면 안 된다."""
    live = tmp_path / "a.jpg"
    live.write_bytes(b"x")
    r = count_cast({"cast_images": [
        {"actor_name": "A", "image_url": "http://x/a.jpg", "image_path": str(live)},
        {"actor_name": "B", "image_url": "http://x/b.jpg", "image_path": str(tmp_path / "gone.jpg")},
        {"actor_name": "C"},
    ]})
    assert r == {"cast": 3, "with_url": 2, "with_file": 1}


def test_count_cast_handles_missing_and_empty():
    assert count_cast({}) == {"cast": 0, "with_url": 0, "with_file": 0}
    assert count_cast({"cast_images": None}) == {"cast": 0, "with_url": 0, "with_file": 0}


def test_usable_when_only_urls_remain():
    """파일이 다 지워져도 URL 이 있으면 레퍼런스를 복원할 수 있다."""
    assert usable({"cast": 3, "with_url": 2, "with_file": 0})


def test_not_usable_when_neither():
    """인물 항목이 있어도 사진이 없으면 켜 봐야 화자 추적 폴백이다."""
    assert not usable({"cast": 6, "with_url": 0, "with_file": 0})
