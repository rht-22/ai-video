"""overlay 실행 진입 — `python -m app.cli localize --mode overlay` 가 부른다.

rerender 의 `app/localize/runner.py` 와 **같은 자리**의 모듈이다. 다른 점은 입력뿐:
rerender 는 job 디렉토리, overlay 는 외부 완성본 mp4 한 개.

⚠ **route 검증은 config 가 한다**(`pipeline._level_opts`). 여기서 화이트리스트를 또
두면 config 에 route 를 추가했을 때 두 곳을 고쳐야 하고, 한쪽만 고치면 조용히
어긋난다. 다만 CLI 는 `choices` 로 오타를 먼저 막는다(§3-3 — 조용한 기본값 금지).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.localize.overlay import DUB_ROUTES
from app.localize.overlay.common import load_config, resolve_path
from app.localize.overlay.pipeline import process_video


def needs_dub(route: str) -> bool:
    """이 route 가 더빙을 포함하는가. 순수 — 테스트 대상.

    오케스트레이터 어댑터의 `needs_dub` 과 **같은 값**이어야 한다(둘이 갈리면 더빙이
    빠진 편이 더빙된 줄 알고 발행된다 — 2026-08-12 에 실제로 났던 사고다)."""
    return str(route or "").upper() in DUB_ROUTES


def run_overlay(video: str | Path, video_id: str, *, route: str = "B",
                locale: str = "ja", content_type: Optional[str] = None,
                roi: Optional[tuple] = None, backend: Optional[str] = None,
                hero: bool = False, config_path: Optional[str | Path] = None) -> dict:
    """완성본 한 개를 현지화한다. 산출 dict(`final`·`report`·`render`)를 돌려준다.

    ⚠ 더빙(route C·BC)은 **여기서 하지 않는다** — vlp 규약 그대로다(`process_video`
    머리말: "Level C 더빙은 이 스크립트가 호출하지 않는다"). 검수 게이트를 지난 뒤
    별도 단계가 맡는다. `needs_dub(route)` 로 뒤따를 단계가 있는지 알린다."""
    cfg = load_config(config_path)
    video_path = Path(video)
    if not video_path.exists():
        raise SystemExit(f"원본 영상이 없다: {video_path}")

    print(f"=== overlay 현지화 시작: {video_id} (route={route} → {locale}) ===")
    result = process_video(str(video_path), video_id, str(route).upper(), cfg,
                           content_type=content_type, roi=roi, hero=hero,
                           inpaint_backend=backend)
    result["route"] = str(route).upper()
    result["locale"] = locale
    result["needs_dub"] = needs_dub(route)
    work = resolve_path(f"{cfg['paths']['outputs_dir']}/{video_id}")
    if result["needs_dub"]:
        print(f"[overlay] route {result['route']}: 더빙은 검수 게이트 통과 후 별도 단계다"
              f" — 이 산출물은 아직 한국어 오디오다")
    print(f"=== 완료: {result.get('final')} · 검수자료 {work} ===")
    return result
