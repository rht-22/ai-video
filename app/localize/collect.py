"""L0 — 한국어 원본 보존. **번역의 입력은 언제나 이 백업본이다.**

원본: `localize_run.l0_backup`. 멱등이 핵심이다 — 재실행해도 이미 일본어가 된 파일을
다시 번역하지 않는다(이중 번역 방지).

⚠ 그 멱등이 편집실 재렌더에서는 **반대로 해롭다**(rebuild — vlp 1da2a16 이식):
사람이 편집실에서 한국어를 고쳐 새로 렌더해도 백업이 있으면 최초 생성본을 계속 읽어
고친 것이 일본어판에 한 글자도 반영되지 않는다.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from app.localize import BACKUP_FILES, RENDER_OUTPUT, SOURCE_VIDEO_BACKUP


# 편집실 재렌더에서 폐기해야 하는 out_dir 캐시 — 전부 '한국어 원본에서 파생된 것'이라
# 원본이 바뀌면 통째로 낡는다. telops.ass·metadata.json 은 L3/L5 가 매번 다시 쓴다.
REBUILD_STALE = ("translation.json", "onscreen.json", "onscreen_refined.json")


def invalidate_localize_cache(out_dir: Path) -> list:
    """rebuild 시 폐기할 캐시를 지우고 지운 이름을 돌려준다.

    refine_frames/ 도 함께 — shorts_ko.mp4 에서 뽑은 프레임이라 영상이 바뀌면 남의 그림이다."""
    removed = []
    for name in REBUILD_STALE:
        p = out_dir / name
        if p.exists():
            p.unlink()
            removed.append(name)
    frames = out_dir / "refine_frames"
    if frames.exists():
        shutil.rmtree(frames, ignore_errors=True)
        removed.append("refine_frames/")
    return removed


def l0_backup(job: Path, rebuild: bool = False) -> Path:
    """한국어 원본을 localize_backup_ko/ 에 보존하고 그 경로를 돌려준다. 멱등.

    rebuild=True (편집실 재렌더): 백업과 shorts_ko.mp4 를 **지금 job 디렉토리 상태로
    갱신**한다. 종전엔 백업이 있으면 무조건 재사용해서, 사람이 편집실에서 고친 한국어
    자막·제목·구간이 담긴 새 렌더가 와도 L1/L3 가 최초 생성본을 계속 읽었다 —
    고친 것이 일본어판에 한 글자도 반영되지 않았다(2026-08-23 SHOTCONE 실측).

    ⚠ 갱신은 BACKUP_FILES 덮어쓰기다(디렉토리 삭제가 아니다). l3t_tts 가 같은 디렉토리에
    보존해 둔 한국어 mp3 원본까지 날리지 않기 위해서다."""
    backup = job / "localize_backup_ko"
    fresh = not backup.exists()
    if fresh:
        backup.mkdir()
    if fresh or rebuild:
        for name in BACKUP_FILES:
            src = job / name
            if src.exists():
                shutil.copy2(src, backup / name)
        print(f"[L0] 백업 {'생성' if fresh else '갱신(rebuild)'}: {backup}")
    else:
        print(f"[L0] 기존 백업 사용: {backup}")
    ko_video = job / SOURCE_VIDEO_BACKUP
    if rebuild and ko_video.exists():
        # 새 한국어 렌더로 갈아끼운다 — L2 텔롭 추출·L4 길이 대조의 기준이다
        ko_video.unlink()
    if not ko_video.exists():
        src = job / RENDER_OUTPUT
        if not src.exists():
            raise SystemExit(f"{RENDER_OUTPUT} 가 없다: {job}")
        shutil.copy2(src, ko_video)
        print(f"[L0] 한국어판 보존: {SOURCE_VIDEO_BACKUP}")
    return backup
