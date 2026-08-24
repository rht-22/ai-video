"""E17-2 — 소스에 박힌 원본 자막 띠 검출 실측 도구.

엔진의 `app/modules/subtitle_region.py` 와 **같은 함수**로 재고, 그 결과를 사람이
볼 수 있게 찍는다(임계값을 실소재로 다시 잡을 때 쓰는 자다 — 코드를 베끼지 않는다).

    # 이 구간을 재서 행별 검출률과 판정된 띠를 본다
    python -m scripts.e17_burned_subtitle_probe --video 소스.mp4 --start 120 --end 180

    # 채널 디자인(밴드 모양·위치)을 맞춰서 — 자막 회피는 밴드 좌표계에서 일어난다
    python -m scripts.e17_burned_subtitle_probe --video 소스.mp4 --start 120 --end 180 \
        --aspect-ratio 13:9 --video-y 440

출력:
  · 행별 검출률 막대(위→아래) — 어디가 임계를 넘는지 눈으로 본다
  · 판정된 띠(캔버스 y)와 그때 자막 margin_v 가 어떻게 바뀌는지
  · `--json` 이면 같은 내용을 기계가 읽을 형태로

⚠ 이 스크립트는 **읽기만 한다** — job 디렉토리도 체크포인트도 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DesignConfig                      # noqa: E402
from app.modules import subtitle_region as sr            # noqa: E402


def _bar(v: float, width: int = 40) -> str:
    return "█" * int(round(v * width))


def main() -> int:
    ap = argparse.ArgumentParser(description="원본 자막 띠 검출 실측")
    ap.add_argument("--video", required=True, help="소스 영상")
    ap.add_argument("--start", type=float, default=0.0, help="표본 구간 시작(초)")
    ap.add_argument("--end", type=float, default=30.0, help="표본 구간 끝(초)")
    ap.add_argument("--frames", type=int, default=12, help="표본 프레임 수")
    ap.add_argument("--aspect-ratio", default=None, help="채널 화면비(예: 13:9)")
    ap.add_argument("--video-width", type=int, default=None, help="밴드 가로(px)")
    ap.add_argument("--video-y", type=int, default=None, help="밴드 상단 y(px)")
    ap.add_argument("--subtitle-size", type=int, default=65, help="대사 자막 글자 크기")
    ap.add_argument("--title", default="제목 한 줄\n두 번째 줄", help="제목(줄 수 계산용)")
    ap.add_argument("--min-ratio", type=float, default=sr.MIN_FRAME_HIT_RATIO,
                    help="띠로 인정할 프레임 검출률 임계(기본은 엔진 상수)")
    ap.add_argument("--json", action="store_true", help="기계가 읽을 형태로도 출력")
    args = ap.parse_args()

    kwargs = {}
    if args.aspect_ratio:
        kwargs["aspect_ratio"] = args.aspect_ratio
    if args.video_width is not None:
        kwargs["video_width"] = args.video_width
    if args.video_y is not None:
        kwargs["video_y"] = args.video_y
    if args.subtitle_size:
        kwargs["subtitle_size"] = args.subtitle_size
    design = DesignConfig(**kwargs)
    geom = sr.band_geometry(design)
    clip = SimpleNamespace(role="main", start_sec=args.start, end_sec=args.end)

    print(f"[밴드] {geom.scaled_w}x{geom.scaled_h} @ y={geom.top}~{geom.bottom} "
          f"(pad_x={geom.pad_x})")
    frames = sr.sample_band_frames(Path(args.video), clip, geom, frames=args.frames)
    print(f"[표본] {len(frames)}프레임 ({args.start:g}~{args.end:g}s)")
    if not frames:
        print("표본이 비었습니다 — 구간·경로를 확인하세요.")
        return 1

    ratios = sr.row_hit_ratios(frames, sr.PROBE_W, sr.PROBE_H)
    px = geom.scaled_h / float(sr.PROBE_H)
    print(f"\n[행별 검출률] 임계 {args.min_ratio:g} · 1행 ≈ {px:.1f}px "
          f"(아래 {sr.SEARCH_FROM_RATIO:.0%} 부터만 판정 대상)")
    for row in range(0, sr.PROBE_H, 2):
        if ratios[row] <= 0.05:
            continue
        y = int(geom.top + row * px)
        mark = "◀" if (ratios[row] >= args.min_ratio
                       and row >= sr.PROBE_H * sr.SEARCH_FROM_RATIO) else " "
        print(f"  row {row:3d} (y={y:4d}) {ratios[row]:4.2f} {_bar(ratios[row])}{mark}")

    band_rows = sr.band_from_ratios(ratios, threshold=args.min_ratio)
    if not band_rows:
        print("\n[판정] 원본 자막 띠 없음 — 자막 위치는 그대로입니다.")
        if args.json:
            print(json.dumps({"band": None}, ensure_ascii=False))
        return 0

    top = int(geom.top + band_rows[0] * px)
    bottom = int(geom.top + band_rows[1] * px)
    print(f"\n[판정] 원본 자막 띠 y={top}~{bottom} (높이 {bottom - top}px)")

    from app.pipeline import _compute_subtitle_margin_v

    base = _compute_subtitle_margin_v(design)
    new, notes = sr.avoid_margin_v(
        base, canvas_height=1920, burned_top=top, burned_bottom=bottom,
        subtitle_height=sr.estimate_subtitle_height(args.subtitle_size),
        title_bottom=sr.estimate_title_bottom(
            design, geom, line_count=sr.estimate_title_line_count(args.title)),
        band_top=geom.top)
    print(f"[자막] margin_v {base} → {new}"
          + ("" if new != base else "  (변화 없음)"))
    for n in notes:
        print(f"  {n}")
    if args.json:
        print(json.dumps({"band": {"top": top, "bottom": bottom},
                          "margin_v": {"before": base, "after": new},
                          "notes": notes}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
