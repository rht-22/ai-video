#!/usr/bin/env python3
"""로고 크기 후보를 한 장의 대조표(contact sheet)로 만든다.

왜 필요한가: 로고는 작품·권리사마다 원본 비율이 제각각이라(가로 워드마크 6:1 ~ 세로 캘리그래피
1:2) "적당한 크기"를 숫자로 미리 알 수 없다. 그렇다고 후보 하나 볼 때마다 전체 렌더를 돌리면
편당 수십 분이라 비교가 불가능하다. 완성본에서 프레임 1장만 뽑아 후보를 나란히 합성하면
몇 초 만에 눈으로 고를 수 있다. 고른 값은 작품 카드에 박아 고정한다.

배치 수식은 renderer.py 의 로고 오버레이와 **같아야** 의미가 있다(안 그러면 시트에서 고른 값이
실제 렌더에서 다른 자리에 나온다). 그래서 영상 영역을 추측하지 않고 그 job 이 실제로 쓴
`shorts.filter.txt` 의 `pad=W:H:x:y` 에서 읽는다 — 렌더 결과 그 자체가 정본이다.

실행:
  python scripts/logo_contact_sheet.py --job-dir outputs_ab/dokkaebi_c2_v2/도깨비_10주년_여행_8a \\
      --logo app/assets/logos/RZsv4.png --heights 120,160,200,240,280
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys

# renderer.py 와 맞춘 상수 — 바뀌면 양쪽을 같이 고쳐야 한다.
GAP_BELOW_VIDEO = 20      # 영상 하단과 로고 사이 최소 여백
CANVAS_MARGIN = 20        # 캔버스 하단 안전 여백
PAD_COLOR = "#0D0011"     # 렌더의 pad 배경색(레터박스 영역)

PAD_RE = re.compile(r"pad=(\d+):(\d+):(\d+):(\d+)")


def ff(name: str) -> str:
    p = shutil.which(name)
    if not p:
        sys.exit(f"{name} 을 PATH 에서 찾지 못했습니다 (brew install ffmpeg)")
    return p


def probe_dims(path: pathlib.Path) -> tuple[int, int]:
    """이미지·영상의 실제 픽셀 크기. 로고 높이를 '추측'하지 않기 위한 것."""
    out = subprocess.run(
        [ff("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")[0].split("x")[:2]
    return int(w), int(h)


def read_geometry(job_dir: pathlib.Path) -> tuple[int, int, int, int]:
    """그 job 이 실제로 쓴 필터 체인에서 (캔버스 W, H, 영상 top, 영상 높이)를 읽는다."""
    f = job_dir / "shorts.filter.txt"
    if not f.exists():
        sys.exit(f"{f} 가 없습니다 — 렌더가 끝난 job 디렉토리를 지정하세요")
    m = PAD_RE.search(f.read_text(encoding="utf-8", errors="ignore"))
    if not m:
        sys.exit(f"{f} 에서 pad= 를 찾지 못했습니다 — 레이아웃을 읽을 수 없습니다")
    cw, ch, _px, py = (int(g) for g in m.groups())
    # pad 는 캔버스 크기와 영상이 놓인 y. 영상 높이는 캔버스에서 위·아래 여백을 뺀 값이 아니라
    # 정사각 크롭(=캔버스 폭)이므로 폭과 같다.
    return cw, ch, py, cw


def contain(nw: int, nh: int, box_w: int, box_h: int) -> tuple[int, int]:
    """원본 비율을 지키며 박스 안에 맞춘 크기. 짝수로 맞춘다(인코더 호환)."""
    s = min(box_w / nw, box_h / nh)
    return max(2, int(nw * s) // 2 * 2), max(2, int(nh * s) // 2 * 2)


def place_y(logo_h: int, video_top: int, video_h: int, canvas_h: int, work_title_y: int,
            align: str = "top", nudge: int = 0) -> int:
    """로고 y 좌표.

    align='top'    — renderer.py 현행: 영상 하단에 붙인다. 아래 여백이 남아 시각적으로 처진다.
    align='center' — 영상 하단~캔버스 하단 밴드의 세로 중앙. 로고 높이가 달라져도 균형이 유지된다.
    nudge 는 계산된 값에서의 미세 조정(+면 아래로). 어느 쪽이든 밴드를 벗어나지 않게 클램프한다.
    """
    safe_top = video_top + video_h + GAP_BELOW_VIDEO
    usable_bottom = canvas_h - CANVAS_MARGIN
    if align == "center":
        y = safe_top + (usable_bottom - safe_top - logo_h) // 2
    else:
        y = max(work_title_y, safe_top)
    y += nudge
    return max(safe_top, min(y, usable_bottom - logo_h))


def main() -> None:
    ap = argparse.ArgumentParser(description="로고 크기 후보 대조표 생성")
    ap.add_argument("--job-dir", required=True, help="렌더가 끝난 job 디렉토리(shorts.mp4 + shorts.filter.txt)")
    ap.add_argument("--logo", required=True, help="로고 이미지 경로")
    ap.add_argument("--heights", default="120,160,200,240,280", help="후보 로고 높이(px), 쉼표 구분")
    ap.add_argument("--align", choices=["top", "center"], default="top",
                    help="top=영상 하단에 붙임(renderer 현행) · center=밴드 세로 중앙")
    ap.add_argument("--nudges", default=None,
                    help="지정하면 높이 대신 y 미세조정을 훑는다(쉼표 구분, +면 아래로). "
                         "--heights 는 첫 값만 쓴다")
    ap.add_argument("--box-width", type=int, default=520, help="가로 상한(px). 가로형 로고에서만 구속된다")
    ap.add_argument("--at", type=float, default=8.0, help="프레임 추출 시각(초)")
    ap.add_argument("--work-title-y", type=int, default=1400, help="DesignConfig.work_title_y")
    ap.add_argument("--scale", type=float, default=0.5, help="타일 축소 배율")
    ap.add_argument("--out", default=None, help="출력 PNG (기본: <job-dir>/logo_contact_sheet.png)")
    a = ap.parse_args()

    job = pathlib.Path(a.job_dir).resolve()
    logo = pathlib.Path(a.logo).resolve()
    if not logo.exists():
        sys.exit(f"로고 파일이 없습니다: {logo}")
    video = job / "shorts.mp4"
    if not video.exists():
        sys.exit(f"{video} 가 없습니다")
    out = pathlib.Path(a.out) if a.out else job / "logo_contact_sheet.png"

    cw, ch, video_top, video_h = read_geometry(job)
    nw, nh = probe_dims(logo)
    heights = [int(x) for x in a.heights.split(",") if x.strip()]

    video_bottom = video_top + video_h
    # 보여줄 구간: 영상 하단 살짝 위 ~ 캔버스 끝. 로고가 놓이는 밴드에 집중한다.
    crop_top = max(0, video_bottom - 120)
    crop_h = ch - crop_top

    font = pathlib.Path(__file__).resolve().parent.parent / "app" / "assets" / "fonts" / "Jalnan.ttf"
    tile_w, tile_h = int(cw * a.scale), int((crop_h + 60) * a.scale)

    print(f"캔버스 {cw}x{ch} · 영상 y={video_top}~{video_bottom} · 로고 원본 {nw}x{nh} "
          f"(비율 {nw/nh:.2f}, {'세로형' if nh > nw else '가로형'})")
    band = ch - CANVAS_MARGIN - (video_bottom + GAP_BELOW_VIDEO)
    print(f"로고가 놓일 수 있는 세로 공간: {band}px\n")

    # 훑을 축: 기본은 높이, --nudges 를 주면 위치.
    if a.nudges:
        cases = [("nudge", heights[0], int(n)) for n in a.nudges.split(",") if n.strip()]
    else:
        cases = [("h", h, 0) for h in heights]

    tmp = out.parent / ".logo_tiles"
    tmp.mkdir(exist_ok=True)
    tiles = []
    for idx, (axis, h, nudge) in enumerate(cases):
        lw, lh = contain(nw, nh, a.box_width, h)
        y = place_y(lh, video_top, video_h, ch, a.work_title_y, a.align, nudge)
        overflow = "  ⚠️ 밴드 초과(클램프됨)" if lh > band else ""
        label = f"y\\={y}" if axis == "nudge" else f"h\\={h}  ({lw}x{lh})"
        print(f"  {'nudge=%+d' % nudge if axis == 'nudge' else 'h=%4d' % h} → "
              f"로고 {lw}x{lh}, y={y}{overflow}")

        tile = tmp / f"t{idx}.png"
        # 기존 작품명 텍스트를 덮고(drawbox) 로고를 얹은 뒤, 밴드만 잘라 라벨을 붙인다.
        vf = (
            f"[0:v]drawbox=x=0:y={video_bottom}:w={cw}:h={ch - video_bottom}:"
            f"color={PAD_COLOR}:t=fill[base];"
            f"[1:v]scale={lw}:{lh}[lg];"
            f"[base][lg]overlay=(W-w)/2:{y}[ov];"
            f"[ov]crop={cw}:{crop_h}:0:{crop_top},"
            f"pad={cw}:{crop_h + 60}:0:60:color=black,"
            f"drawtext=fontfile='{font}':text='{label}':"
            f"fontcolor=white:fontsize=40:x=(w-text_w)/2:y=10,"
            f"scale={tile_w}:{tile_h},"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=#333333:t=2[out]"
        )
        subprocess.run(
            [ff("ffmpeg"), "-v", "error", "-y", "-ss", str(a.at), "-i", str(video),
             "-i", str(logo), "-filter_complex", vf, "-map", "[out]",
             "-frames:v", "1", str(tile)], check=True)
        tiles.append(tile)

    inputs = []
    for t in tiles:
        inputs += ["-i", str(t)]
    subprocess.run(
        [ff("ffmpeg"), "-v", "error", "-y", *inputs,
         "-filter_complex", f"hstack=inputs={len(tiles)}", "-frames:v", "1", str(out)],
        check=True)
    for t in tiles:
        t.unlink()
    tmp.rmdir()
    print(f"\n✅ {out}")


if __name__ == "__main__":
    main()
