#!/usr/bin/env python3
"""권리사 제공 로고 → 여백 트림된 정규화본. 작품 등록 때 사람이 1회 실행한다.

왜 필요한가: 로고 파일의 여백은 권리사마다 제각각이다. 유미의 세포들은 1920x1080 캔버스에
804x444 그림만 들어 있어 **58%가 빈 픽셀**이고, 도깨비는 여백이 0이다. 그런데 렌더는 파일
치수를 기준으로 크기를 맞추므로, 같은 설정을 줘도 여백이 많은 쪽이 그만큼 작게 나온다
(실측: 같은 height=280 에서 보이는 로고가 280px vs 115px). 파일 경계를 그림 경계에 맞춰두면
이 차이가 사라지고, 작품 카드의 크기 값이 "보이는 로고 크기"를 직접 의미하게 된다.

왜 렌더 때가 아니라 등록 때인가: 트림은 배경색 추정·임계값 같은 **판단이 들어가는 작업**이라
결과를 사람이 봐야 한다. 밤 4시 자동 생성 안에 숨기면 조용히 틀린 채로 발행된다. 특히 아래
'화이트-온-화이트' 함정은 실제로 가능한 조합이다(보유 로고 2종이 모두 화이트 버전이다).

실행:
  python scripts/normalize_logo.py --code ZSByI --input "app/assets/logos/_raw/유미_로고.png"
  python scripts/normalize_logo.py --code ZSByI --input ... --dry-run   # 저장 없이 측정만
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import sys

import numpy as np
from PIL import Image

# 안티에일리어싱 가장자리를 살리기 위한 하한. 0 이면 거의 안 보이는 픽셀까지 잉크로 친다.
ALPHA_THRESHOLD = 8
# 배경색과 '다르다'고 볼 채널 합계 차이. JPEG 아티팩트·미세한 그라데이션을 배경으로 흡수한다.
RGB_TOLERANCE = 30
# 잉크가 이보다 작으면 측정 실패로 본다 — 화이트-온-화이트에서 로고를 통째로 먹는 사고 방지.
MIN_INK_RATIO = 0.05
# 여백 좌우/상하 차이가 이 비율을 넘으면 비대칭으로 경고(트림이 광학 중심을 옮긴다).
ASYMMETRY_TOLERANCE = 0.05
# 크기 기본 박스 — 도깨비·유미 선택값에서 역산(contain 피팅). loop_policy 와 같이 움직여야 한다.
DEFAULT_BOX = (395, 280)


class TrimError(Exception):
    """사람이 봐야 하는 실패. 조용히 진행하면 안 되는 경우에만 던진다."""


def ink_bbox(im: Image.Image) -> tuple[tuple[int, int, int, int], str]:
    """(x0, y0, x1, y1), 판정방식. 알파가 쓸모없으면 배경색으로 폴백한다."""
    a = np.array(im.convert("RGBA"))
    alpha = a[..., 3]

    if not (alpha > ALPHA_THRESHOLD).all():
        mask = alpha > ALPHA_THRESHOLD
        method = "alpha"
    else:
        # 전부 불투명 = 알파가 정보를 안 준다(rgb 를 rgba 로 저장한 경우 포함) → 배경색 추정.
        rgb = a[..., :3].astype(int)
        corners = [tuple(rgb[0, 0]), tuple(rgb[0, -1]), tuple(rgb[-1, 0]), tuple(rgb[-1, -1])]
        bg = max(set(corners), key=corners.count)
        if corners.count(bg) < 3:
            raise TrimError(
                f"네 모서리 색이 제각각입니다({corners}) — 배경이 단색이 아니라 "
                f"자동 추정이 성립하지 않습니다. 배경을 제거한 파일을 받아 주세요.")
        mask = np.abs(rgb - np.array(bg)).sum(axis=2) > RGB_TOLERANCE
        method = f"bgcolor{bg}"

    if not mask.any():
        raise TrimError("잉크로 판정된 픽셀이 없습니다 — 빈 이미지이거나 배경색 추정이 틀렸습니다.")
    ys, xs = np.nonzero(mask)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1), method


def contain(nw: int, nh: int, box_w: int, box_h: int) -> tuple[int, int]:
    """박스 안에 비율 유지로 맞춘 크기. renderer 의 로고 피팅과 같은 계산."""
    s = min(box_w / nw, box_h / nh)
    return max(2, int(nw * s) // 2 * 2), max(2, int(nh * s) // 2 * 2)


def main() -> None:
    ap = argparse.ArgumentParser(description="로고 여백 트림 → 정규화본 생성")
    ap.add_argument("--code", required=True, help="laeebly 식별코드(=출력 파일명). 예: ZSByI")
    ap.add_argument("--input", required=True, help="권리사 원본 로고 경로")
    ap.add_argument("--logos-dir", default=None, help="기본: <repo>/app/assets/logos")
    ap.add_argument("--box", default=f"{DEFAULT_BOX[0]}x{DEFAULT_BOX[1]}",
                    help="크기 확인용 박스 WxH (저장물에는 영향 없음)")
    ap.add_argument("--dry-run", action="store_true", help="측정만 하고 저장하지 않는다")
    ap.add_argument("--accept-bgcolor", action="store_true",
                    help="알파가 없어 배경색으로 추정한 결과를 승인한다. _mask.png 를 눈으로 "
                         "확인한 뒤에만 쓸 것 — 확인 없이 붙이면 안 되는 플래그다.")
    a = ap.parse_args()

    src = pathlib.Path(a.input).expanduser().resolve()
    if not src.exists():
        sys.exit(f"입력 파일이 없습니다: {src}")
    logos = pathlib.Path(a.logos_dir) if a.logos_dir else \
        pathlib.Path(__file__).resolve().parent.parent / "app" / "assets" / "logos"
    raw_dir = logos / "_raw"
    out_png = logos / f"{a.code}.png"
    out_json = logos / f"{a.code}.json"

    im = Image.open(src).convert("RGBA")
    W, H = im.size
    try:
        (x0, y0, x1, y1), method = ink_bbox(im)
    except TrimError as e:
        sys.exit(f"⛔ {a.code}: {e}")

    iw, ih = x1 - x0, y1 - y0
    ratio = (iw * ih) / (W * H)
    if ratio < MIN_INK_RATIO:
        sys.exit(
            f"⛔ {a.code}: 잉크가 원본의 {ratio*100:.1f}% 뿐입니다(하한 {MIN_INK_RATIO*100:.0f}%).\n"
            f"   배경색 추정이 그림을 먹었을 가능성이 큽니다 — 화이트 로고가 흰 배경으로 저장된 경우가\n"
            f"   대표적입니다. 알파가 있는 파일을 받거나, 수동으로 잘라 주세요.")

    pad = {"left": x0, "right": W - x1, "top": y0, "bottom": H - y1}
    # 축 기준으로 보고한다 — 렌더 크기는 선형 스케일로 결정되므로 "가로 58% 여백"이 곧
    # "같은 설정에서 42% 크기로 나온다"는 뜻이다. 면적 기준(83%)은 커 보이지만 해석이 어렵다.
    trim_w, trim_h = (1 - iw / W) * 100, (1 - ih / H) * 100
    asym = []
    if abs(pad["left"] - pad["right"]) > W * ASYMMETRY_TOLERANCE:
        asym.append(f"좌우 {pad['left']}/{pad['right']}")
    if abs(pad["top"] - pad["bottom"]) > H * ASYMMETRY_TOLERANCE:
        asym.append(f"상하 {pad['top']}/{pad['bottom']}")

    box_w, box_h = (int(v) for v in a.box.lower().split("x"))
    fit_w, fit_h = contain(iw, ih, box_w, box_h)

    print(f"■ {a.code}")
    print(f"   원본        {src.name}  {W}x{H}")
    print(f"   판정방식    {method}")
    print(f"   잉크 bbox   {iw}x{ih}  (여백 가로 {trim_w:.0f}% · 세로 {trim_h:.0f}%)")
    if max(trim_w, trim_h) >= 20:
        print(f"      → 트림 전이면 같은 설정에서 {min(iw / W, ih / H) * 100:.0f}% 크기로만 렌더됐습니다.")
    if asym:
        print(f"   ⚠️ 여백 비대칭 — {' · '.join(asym)}")
        print(f"      트림하면 로고의 광학 중심이 이동합니다. 의도된 구도인지 확인하세요.")
    else:
        print(f"   여백 대칭   좌우 {pad['left']}/{pad['right']} · 상하 {pad['top']}/{pad['bottom']}  ✅")
    print(f"   박스 {box_w}x{box_h} 적용 시 → {fit_w}x{fit_h}")

    # 배경색 추정은 원리적으로 애매하다 — 로고와 배경이 같은 색인 부분은 복구할 수 없다.
    # 실측 사례: 화이트 로고를 흰 배경 rgb 로 저장하면 흰 글자가 배경으로 흡수되고 유채색 부분만
    # 남아, 잉크 비율 하한(면적 5%)을 넘겨 버린다(유미 화이트본 → 분홍 '3'·하트만 5.4% 로 생존).
    # 그래서 비율 검사로는 못 막는다. 알파 경로는 자동 승인하되, 이쪽은 사람이 마스크를 보고
    # 명시적으로 승인하게 한다.
    if method.startswith("bgcolor") and not a.dry_run and not a.accept_bgcolor:
        mask_path = logos / f"{a.code}_mask.png"
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        prev = np.array(im.convert("RGB")).copy()
        m = np.zeros(prev.shape[:2], bool)
        m[y0:y1, x0:x1] = True
        prev[~m] = (prev[~m] * 0.25).astype(np.uint8)
        Image.fromarray(prev).save(mask_path)
        sys.exit(
            f"\n⛔ {a.code}: 알파가 없어 배경색으로 추정했습니다 — 자동 승인하지 않습니다.\n"
            f"   로고와 배경이 같은 색인 부분은 복구할 수 없습니다(흰 로고 + 흰 배경이 대표적).\n"
            f"   → {mask_path.name} 에 검출 영역을 표시했습니다. 로고 전체가 들어왔는지 확인하고,\n"
            f"     맞으면 --accept-bgcolor 를 붙여 다시 실행하세요.\n"
            f"     잘렸다면 알파가 살아 있는 원본을 권리사에게 요청하는 편이 확실합니다.")

    if a.dry_run:
        print("   (--dry-run: 저장하지 않음)")
        return

    raw_dir.mkdir(parents=True, exist_ok=True)
    kept = raw_dir / src.name
    if src.parent.resolve() != raw_dir.resolve():
        shutil.copy2(src, kept)
    im.crop((x0, y0, x1, y1)).save(out_png)
    out_json.write_text(json.dumps({
        "code": a.code,
        "source_file": src.name,
        "method": method,
        "original_size": [W, H],
        "ink_bbox": [x0, y0, x1, y1],
        "ink_size": [iw, ih],
        "trim_percent_w": round(trim_w, 1),
        "trim_percent_h": round(trim_h, 1),
        "trim_percent_area": round((1 - ratio) * 100, 1),
        "padding": pad,
        "normalized_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"   → {out_png.relative_to(logos.parent.parent.parent)} 저장 (원본 보존: _raw/{src.name})")


if __name__ == "__main__":
    main()
