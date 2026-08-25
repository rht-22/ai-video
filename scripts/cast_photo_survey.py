"""배우 사진이 실제로 얼마나 붙는가 — 인물 인식을 켤 가치가 있는지 먼저 본다.

`enable_face_recognition` 은 배우 사진 레퍼런스가 있어야 동작한다(없으면 화자 추적
폴백 = 끈 것과 같다). 그런데 사진은 TMDb 에서 오고, **한국 예능은 TMDb 커버리지가
얇다.** 채널을 켜기 전에 그 채널 소재에 사진이 붙는지부터 세는 것이 순서다.

    python -m scripts.cast_photo_survey /opt/ves/engines/ai-video/outputs

각 job 의 `checkpoint_research.json` 에서 `image_url`(원본 출처)과 `image_path`
(지금 남아 있는 파일)를 따로 센다 — 파일은 job 정리로 지워지지만 URL 은 남는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def count_cast(research: dict) -> dict:
    """{총 인물, url 있는 수, 파일이 남아 있는 수}. 순수 — 테스트 대상."""
    cast = (research or {}).get("cast_images") or []
    urls = sum(1 for c in cast if c.get("image_url"))
    files = sum(1 for c in cast
                if c.get("image_path") and Path(str(c["image_path"])).exists())
    return {"cast": len(cast), "with_url": urls, "with_file": files}


def usable(row: dict) -> bool:
    """레퍼런스를 만들 수 있는가 — 파일이 남았거나, 없어도 URL 로 다시 받을 수 있다. 순수."""
    return row["with_file"] > 0 or row["with_url"] > 0


def main() -> None:
    ap = argparse.ArgumentParser(description="배우 사진 커버리지 조사")
    ap.add_argument("outputs", type=Path, help="ai-video outputs 디렉토리")
    ap.add_argument("--limit", type=int, default=40, help="최근 N개만")
    args = ap.parse_args()

    jobs = sorted((p for p in args.outputs.glob("*/checkpoint_research.json")),
                  key=lambda p: p.stat().st_mtime, reverse=True)[:args.limit]
    if not jobs:
        raise SystemExit(f"checkpoint_research.json 이 있는 job 이 없다: {args.outputs}")

    ok = 0
    for rp in jobs:
        try:
            row = count_cast(json.loads(rp.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ?  {rp.parent.name}: 읽기 실패 {type(e).__name__}")
            continue
        mark = "✅" if usable(row) else "· "
        ok += 1 if usable(row) else 0
        print(f"  {mark} {rp.parent.name:44s} 인물 {row['cast']:2d} · "
              f"url {row['with_url']:2d} · 파일 {row['with_file']:2d}")
    print(f"\n레퍼런스를 만들 수 있는 job: {ok}/{len(jobs)}")
    if not ok:
        print("⇒ 이 소재들에는 배우 사진이 안 붙는다. 인물 인식을 켜도 화자 추적 폴백이라")
        print("  화면이 안 바뀌고 인덱스 스캔 비용만 든다.")


if __name__ == "__main__":
    main()
