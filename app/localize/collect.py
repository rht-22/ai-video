"""L0 — 한국어 원본 보존. **번역의 입력은 언제나 이 백업본이다.**

원본: `localize_run.l0_backup`. 멱등이 핵심이다 — 재실행해도 이미 일본어가 된 파일을
다시 번역하지 않는다(이중 번역 방지).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from app.localize import BACKUP_FILES, RENDER_OUTPUT, SOURCE_VIDEO_BACKUP


def l0_backup(job: Path) -> Path:
    """한국어 원본을 localize_backup_ko/ 에 보존하고 그 경로를 돌려준다. 멱등."""
    backup = job / "localize_backup_ko"
    if not backup.exists():
        backup.mkdir()
        for name in BACKUP_FILES:
            src = job / name
            if src.exists():
                shutil.copy2(src, backup / name)
        print(f"[L0] 백업 생성: {backup}")
    else:
        print(f"[L0] 기존 백업 사용: {backup}")
    ko_video = job / SOURCE_VIDEO_BACKUP
    if not ko_video.exists():
        src = job / RENDER_OUTPUT
        if not src.exists():
            raise SystemExit(f"{RENDER_OUTPUT} 가 없다: {job}")
        shutil.copy2(src, ko_video)
        print(f"[L0] 한국어판 보존: {SOURCE_VIDEO_BACKUP}")
    return backup
