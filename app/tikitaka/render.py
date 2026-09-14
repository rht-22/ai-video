"""6단계 — 렌더. 편집 테이블 행 → ffmpeg 타임라인(1080×1920) + ASS(제목·대사·내레이션·효과자막) + TTS 믹스.

오디오 정책(제3원칙): N = 원본 MUTE + 내레이션 / S·A = 원본 ON, 내레이션 없음.
비디오: 16:9 소스를 1080×608 밴드로 세로 중앙 배치(검정 배경). 컷은 전부 정배속.
"""
from __future__ import annotations

import json
import re
import subprocess
from math import gcd
from pathlib import Path

from app.tikitaka.common import Job, find_bin, ms3

W, H = 1080, 1920
BAND_H = 608
BAND_Y = (H - BAND_H) // 2
FPS = 30

# ── 레이아웃 프리셋 (2026-09-11 · 레퍼런스 youtube.com/shorts/_GTpP-MUizw 실측) ─────────────────────────
# fill: 16:9 소스를 좌우 크롭해 5:6 으로 세로를 크게 채운다(1080×1296 · y 190~1486). 제목은 영상 위쪽에 겹쳐(2줄, 노랑/주황),
#       대사·내레이션 자막은 영상 안 하단(y≈1400), 효과자막은 영상 위 1/3 지점(y≈620)에 66px, 작품명은 아래 검정 여백 중앙.
# band: 종전 — 16:9 원본 그대로 1080×608 밴드를 세로 중앙, 제목은 밴드 위, 자막은 밴드 아래.
# 유튜브 쇼츠 UI 안전 영역(2026-09-11 사용자 스크린샷): 상단 ~160px 에 재생/음량/CC/전체화면 컨트롤, 하단 ~250px(모바일은 더 큼)에
# 채널·구독·제목 오버레이가 얹혀 제목 윗줄과 로고가 가려졌다. 제목은 SAFE_TOP 아래에서 시작하고, 영상 밴드 + 아래 스택(로고/작품명·
# 카피)은 H-SAFE_BOTTOM 위에서 끝난다 — 5:6 은 **상한**이고, 남는 세로에 맞춰 밴드가 더 납작해진다(가로 1080 은 유지, 비율만 변한다).
SAFE_TOP = 200
SAFE_BOTTOM = 360

LAYOUTS = {
    # fill: 제목 블록(y≈200~)을 위 검정 영역에, 영상은 제목 잉크 바닥 +14px 부터, 아래 검정 영역에 로고/작품명+카피 — 전부 안전 영역 안
    "fill": {"aspect": (5, 6), "band_y": 444, "title_y": 200, "title_size_max": 104, "title_size_min": 64,
             "sub_from_band_bottom": 86, "sub_size": 62, "sub_wrap": 16, "effect_frac": 0.30, "effect_size": 66,
             "work_title": True, "title_line2_color": "#FF4632", "safe_bottom": SAFE_BOTTOM},
    "band": {"aspect": (16, 9), "band_y": None, "title_y": None, "title_size_max": 66, "title_size_min": 66,
             "sub_from_band_bottom": -170, "sub_size": 54, "sub_wrap": 18, "effect_frac": None, "effect_size": 92,
             "work_title": False, "title_line2_color": "#FFE24A", "safe_bottom": None},
}
DEFAULT_LAYOUT = "fill"


TITLE_TOP = SAFE_TOP    # 제목 블록 위 여백 — 유튜브 상단 컨트롤 바 아래(종전 140 은 컨트롤에 윗줄이 걸렸다)
TITLE_GAP = 10          # 제목 잉크 바닥(+4)과 영상 사이 간격 — 0 이면 겹쳐 보인다는 재지적(2026-09-11)으로 총 14px 여백
TITLE_LINE_H = 1.08     # 잘난체 줄 높이(em) — 1.15 는 둘째 줄 아래 빈 띠가 남았다
TITLE_PAD = 6


_INK_CACHE: dict[tuple, int] = {}


def measure_title_ink_bottom(l1: str, l2: str, size: int, center_y: int) -> int | None:
    """제목을 검은 1080×1920 프레임에 실제로 그려 **잉크의 맨 아래 행**을 잰다(ffmpeg ass + PIL). 실패하면 None.
    2026-09-11 사용자 스크린샷: 공식(줄수×크기×1.08)로 잡은 블록 바닥과 실제 글자 바닥 사이에 빈 띠가 남았다 — 재서 붙인다."""
    key = (l1, l2, size, center_y)
    if key in _INK_CACHE:
        return _INK_CACHE[key]
    try:
        import tempfile
        from PIL import Image
        from app.config import to_font_family
        f_title = to_font_family("Jalnan")
        black = "&H00000000"
        t_text = _esc(l1) + (f"\\N{{\\c{ass_color('#FF4632')}&}}{_esc(l2)}" if l2 else "")
        ass = ("[Script Info]\nScriptType: v4.00+\n" f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
               "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
               "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
               f"Style: Title,{f_title},{size},{ass_color(COLOR_TITLE_ACCENT)},&H000000FF,{black},&H80000000,0,0,0,0,100,100,0,0,1,6,0,5,40,40,0,1\n\n"
               "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               f"Dialogue: 0,0:00:00.00,0:00:01.00,Title,,0,0,0,,{{\\an5\\pos({W//2},{center_y})}}{t_text}\n")
        with tempfile.TemporaryDirectory(prefix="tk_title_") as d:
            ass_p = Path(d) / "t.ass"
            png = Path(d) / "t.png"
            ass_p.write_text(ass, encoding="utf-8")
            subprocess.run([find_bin("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=1:d=1",
                            "-frames:v", "1", "-vf", f"ass='{_filter_path(ass_p)}':fontsdir='{_filter_path(FONTS_DIR)}'", str(png)], check=True)
            with Image.open(png) as im:
                g = im.convert("L")
                w, h = g.size
                px = g.load()
                bottom = None
                for y in range(h - 1, -1, -1):
                    if any(px[x, y] > 40 for x in range(0, w, 3)):
                        bottom = y
                        break
        _INK_CACHE[key] = bottom
        return bottom
    except Exception:  # noqa: BLE001 — 측정 실패면 공식 폴백
        return None


COPY_SIZE = 40            # 카피 문구 글자 크기(px)
COPY_LINE_H = 48          # 카피 한 줄 높이
WORK_TEXT_H = 62          # 작품명 텍스트(54px) 블록 높이
BOTTOM_GAP = 16           # 아래 검정 영역 안 요소 사이 간격
LOGO_MAX_W = 640          # 로고 최대 폭(px)
LOGO_MAX_H = 130          # 로고 최대 높이(px) — 안전 영역 안에 밴드를 최대한 남기려고 170 → 130


def stack_need(logo_size: tuple[int, int] | None, copy_text: str | None) -> int:
    """아래 스택(로고/작품명 + 카피 + 여백)이 필요한 세로 px — 밴드 높이를 정할 때 먼저 뺀다. 순수."""
    has_copy = bool(copy_text and copy_text.strip())
    if logo_size:
        lw, lh = max(1, int(logo_size[0])), max(1, int(logo_size[1]))
        main_h = int(lh * min(LOGO_MAX_W / lw, LOGO_MAX_H / lh)) // 2 * 2
    else:
        main_h = WORK_TEXT_H
    return main_h + (COPY_LINE_H + BOTTOM_GAP if has_copy else 0) + 2 * BOTTOM_GAP


def bottom_stack(band_bottom: int, *, logo_size: tuple[int, int] | None, copy_text: str | None, copy_pos: str = "below",
                 bottom: int = H) -> dict:
    """아래 검정 영역(밴드 하단~bottom)에 [로고 또는 작품명] + [카피 문구]를 세로 중앙 정렬로 쌓는다(순수 — 테스트 대상).
    반환: {work_y(작품명 텍스트 중심 · 로고면 None), logo_box{x,y,w,h}(없으면 None), copy_y(중심 · 없으면 None), copy_size}.
    2026-09-11 사용자 요청: 작품명을 로고 이미지로, 그 위/아래에 카피('풀 영상은 쿠팡플레이에서 시청하세요').
    bottom 은 유튜브 하단 오버레이 위 경계(H-SAFE_BOTTOM) — 캔버스 끝이 아니다."""
    area_top, area_h = band_bottom, bottom - band_bottom
    out = {"work_y": None, "logo_box": None, "copy_y": None, "copy_size": COPY_SIZE}
    if area_h < 80:
        return out
    has_copy = bool(copy_text and copy_text.strip())
    copy_h = COPY_LINE_H if has_copy else 0
    if logo_size:
        lw, lh = max(1, int(logo_size[0])), max(1, int(logo_size[1]))
        max_h = max(24, min(LOGO_MAX_H, area_h - (copy_h + BOTTOM_GAP if has_copy else 0) - 2 * BOTTOM_GAP))
        sc = min(LOGO_MAX_W / lw, max_h / lh)
        main_h, main_w = int(lh * sc) // 2 * 2, int(lw * sc) // 2 * 2
    else:
        main_h, main_w = WORK_TEXT_H, 0
    items = [("copy", copy_h), ("main", main_h)] if (has_copy and copy_pos == "above") else [("main", main_h), ("copy", copy_h)]
    items = [it for it in items if it[1] > 0]
    total = sum(h for _, h in items) + BOTTOM_GAP * (len(items) - 1)
    y = area_top + max(0, (area_h - total) // 2)
    for kind, h in items:
        if kind == "main":
            if logo_size:
                out["logo_box"] = {"x": (W - main_w) // 2, "y": int(y), "w": main_w, "h": main_h}
            else:
                out["work_y"] = int(y + h / 2)
        else:
            out["copy_y"] = int(y + h / 2)
        y += h + BOTTOM_GAP
    return out


def compute_layout(preset: str = DEFAULT_LAYOUT, *, src_w: int = 1920, src_h: int = 1080, title: str | None = None,
                   logo_size: tuple[int, int] | None = None, copy_text: str | None = None, copy_pos: str = "below") -> dict:
    """프리셋 → 픽셀 기하(순수). crop 은 소스 중앙에서 목표 비율만큼 잘라내는 ffmpeg 식(소스 해상도 무관).
    fill 에 title 을 주면 제목 블록 높이를 글자 크기에서 계산해 **영상이 제목 잉크 바닥 +14px** 에서 시작한다.
    logo_size(원본 px)·copy_text 를 주면 아래 검정 영역에 로고/작품명 + 카피를 쌓는다(bottom_stack).
    fill 은 유튜브 안전 영역을 지킨다: 밴드 높이 = min(5:6, (H-SAFE_BOTTOM) - 아래 스택 - band_y) — 비율이 5:6 보다 납작해질 수 있다."""
    p = dict(LAYOUTS[preset])
    aw, ah = p["aspect"]
    band_h = int(round(W * ah / aw)) // 2 * 2
    title_size = p["title_size_max"]
    title_y = p["title_y"]
    if title is not None:
        l1, l2 = split_title(title)
        title_size = title_font_size(l1, l2, size_max=p["title_size_max"], size_min=p["title_size_min"])
        n_lines = 2 if l2 else 1
        block_h = int(n_lines * title_size * TITLE_LINE_H) + TITLE_PAD
        if p["band_y"] is not None:                       # fill: 제목 블록 → 영상 밀착
            title_y = TITLE_TOP + block_h // 2
            ink_bottom = measure_title_ink_bottom(l1, l2, title_size, title_y)     # 실측 잉크 바닥(없으면 공식)
            bottom = (ink_bottom + 4) if ink_bottom else (TITLE_TOP + block_h)
            p["band_y"] = (bottom + TITLE_GAP) // 2 * 2
    band_y = p["band_y"] if p["band_y"] is not None else (H - band_h) // 2
    usable_bottom = H
    if p.get("safe_bottom") is not None:                  # 안전 영역: 밴드 + 아래 스택이 하단 오버레이 위에서 끝나게 밴드를 납작하게
        usable_bottom = H - p["safe_bottom"]
        band_h = min(band_h, usable_bottom - stack_need(logo_size, copy_text) - band_y) // 2 * 2
        g = gcd(W, band_h)
        aw, ah = W // g, band_h // g
    band_bottom = band_y + band_h
    # 소스에서 목표 비율로 중앙 크롭: 세로를 다 쓰고 가로를 잘라낸다(세로가 모자라면 반대)
    crop = f"crop='min(iw,ih*{aw}/{ah})':'min(ih,iw*{ah}/{aw})'"      # 중앙(기본). 얼굴 크롭은 render() 가 x 식을 덧붙인다
    crop_w = min(src_w, int(round(src_h * aw / ah)))
    sub_y = band_bottom - p["sub_from_band_bottom"] if p["sub_from_band_bottom"] >= 0 else band_bottom + (-p["sub_from_band_bottom"])
    effect_y = int(band_y + band_h * p["effect_frac"]) if p["effect_frac"] is not None else band_y + 130
    title_y = title_y if title_y is not None else band_y - 190
    stack = bottom_stack(band_bottom, logo_size=logo_size, copy_text=copy_text, copy_pos=copy_pos, bottom=usable_bottom) if p["work_title"] else \
        {"work_y": None, "logo_box": None, "copy_y": None, "copy_size": COPY_SIZE}
    return {"preset": preset, "band_h": band_h, "band_y": band_y, "band_bottom": band_bottom, "crop": crop,
            "crop_w": crop_w, "src_w": src_w, "src_h": src_h, "aspect": (aw, ah), "title_size": title_size,
            "sub_y": int(sub_y), "sub_top": int(sub_y - (38 + 2 * p["sub_size"] * 1.2 + 12)), "sub_size": p["sub_size"], "sub_wrap": p["sub_wrap"], "effect_y": effect_y,
            "effect_size": p["effect_size"], "title_y": title_y, "title_size_max": p["title_size_max"],
            "title_size_min": p["title_size_min"], "work_y": stack["work_y"], "logo_box": stack["logo_box"],
            "copy_y": stack["copy_y"], "copy_size": stack["copy_size"], "title_line2_color": p["title_line2_color"]}


def title_font_size(l1: str, l2: str, *, size_max: int, size_min: int, max_px: int = 1000) -> int:
    """긴 제목이 화면 밖으로 나가지 않게 — 가장 긴 줄이 max_px 안에 들도록 글자 크기를 줄인다(잘난체 ≈ 1.0em/글자)."""
    n = max(len(l1), len(l2), 1)
    return int(max(size_min, min(size_max, max_px / n)))
FONTS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"

COLOR_TITLE_ACCENT = "#FFE24A"
COLOR_NARR = "#7DE8D8"
COLOR_EFFECT = "#FFE24A"
# 화자별 자막 색(참고 쇼츠 dtjASrRSXFc·aZEWYEIPnt8: 화자마다 연두·보라 등으로 구분) — 등장 순서대로 배정, 첫 화자는 흰색
UNKNOWN_SPEAKERS = ("", "미상", "불명", "unknown", "?")   # 자막 라벨을 안 그리는 화자 표기(2026-09-12 2화 v4 실측 "미상" 라벨)
SPEAKER_COLORS = ["#FFFFFF", "#B8FF7A", "#D2A8FF", "#FFB36B", "#7AD9FF"]
SHORT_LINE_CHARS = 5          # 이하 글자수의 대사 줄은 감탄·반응이라 크게(참고: "어?", "왜 없어?")
SHORT_LINE_SCALE = 1.35
EFFECT_HOLD_SEC = 1.8         # 효과자막 유지 시간


def speaker_colors(rows: list[dict]) -> dict[str, str]:
    order: list[str] = []
    for r in rows:
        if r["mode"] == "S" and r.get("speaker") and r["speaker"] not in order:
            order.append(r["speaker"])
    return {sp: SPEAKER_COLORS[i % len(SPEAKER_COLORS)] for i, sp in enumerate(order)}


def effect_window(row: dict, s: float, e: float) -> tuple[float, float]:
    """효과자막이 뜨는 순간(순수 — 테스트 대상). S 행 = 펀치라인(마지막 줄) 시작에 · A 행 = 동작 시작 · N 행 = 문장 시작.
    2026-09-11: 행 시작에 일괄로 띄우던 것을 참고 쇼츠처럼 '반응이 오는 순간'으로 옮겼다. 다음 행으로 조금 넘어가도 된다(별도 레이어)."""
    if row["mode"] == "S":
        subs = row.get("sub_lines") or []
        base = row["cuts"][0]["in"]
        start = s + max(0.0, (subs[-1]["start"] - base)) if len(subs) >= 2 else s
        return start, start + EFFECT_HOLD_SEC
    return s, min(s + EFFECT_HOLD_SEC, max(s + 0.8, e))


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _esc(text: str) -> str:
    return str(text).replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


_FONT_CACHE: dict[tuple[str, int], object] = {}


def _font(path: str, size: int):
    key = (path, size)
    if key not in _FONT_CACHE:
        from PIL import ImageFont
        _FONT_CACHE[key] = ImageFont.truetype(path, size)
    return _FONT_CACHE[key]


def text_px(text: str, font_path: str, size: int) -> float:
    """실제 폰트로 잰 글자 폭(px). 폰트를 못 열면 1em/글자 근사."""
    try:
        return float(_font(font_path, size).getlength(text))
    except Exception:  # noqa: BLE001
        return len(text.replace(" ", "")) * size + text.count(" ") * size * 0.3


def fit_subtitle(text: str, *, font_path: str, size: int, max_px: int = 1000, max_lines: int = 2, min_size: int = 46) -> tuple[list[str], int]:
    """어절 단위로 줄을 나누되 **픽셀 폭**으로 잰다(글자 수 기준은 잘난고딕에서 1080px 을 넘겼다 — 2026-09-11 실측 26줄).
    max_lines 안에 안 들어가면 글자 크기를 min_size 까지 줄이고, 그래도 안 되면 세 줄까지 허용한다. 순수 — 테스트 대상."""
    words = str(text).split()

    def wrap(sz: int, lines_cap: int) -> list[str] | None:
        lines: list[str] = []
        cur = ""
        for w in words:
            cand = f"{cur} {w}".strip()
            if cur and text_px(cand, font_path, sz) > max_px:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            lines.append(cur)
        if len(lines) > lines_cap or any(text_px(l, font_path, sz) > max_px for l in lines):
            return None
        return lines

    for sz in range(size, min_size - 1, -2):
        got = wrap(sz, max_lines)
        if got:
            return got, sz
    got = wrap(min_size, max_lines + 1)
    if got:
        return got, min_size
    return wrap(min_size, 99) or [text], min_size


def wrap_lines(text: str, max_chars: int = 18, max_lines: int = 2) -> str:
    """어절 단위 줄바꿈 → `\\N`. max_lines 를 넘으면 마지막 줄에 몰아넣는다(잘라내지 않는다)."""
    words = str(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars and len(lines) < max_lines - 1:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\\N".join(lines)


def split_title(title: str) -> tuple[str, str]:
    """제목을 두 줄로: 가운데에 가장 가까운 공백에서 가른다. 공백이 없으면 한 줄."""
    t = " ".join(str(title).split())
    if len(t) <= 12 or " " not in t:
        return t, ""
    mid = len(t) // 2
    best = min((i for i, ch in enumerate(t) if ch == " "), key=lambda i: abs(i - mid))
    return t[:best].strip(), t[best + 1:].strip()


def build_ass(table: dict, *, title: str, layout: dict | None = None, work_title: str | None = None,
              copy_text: str | None = None, name_map: dict[str, str] | None = None) -> str:
    from app.config import to_font_family
    L = layout or compute_layout(DEFAULT_LAYOUT)
    f_title = to_font_family("Jalnan")
    f_sub = to_font_family("JalnanGothic")
    total = table["total_sec"]
    white, black, shadow = "&H00FFFFFF", "&H00000000", "&H80000000"
    l1, l2 = split_title(title)
    t_size = L.get("title_size") or title_font_size(l1, l2, size_max=L["title_size_max"], size_min=L["title_size_min"])
    styles = [
        f"Style: Title,{f_title},{t_size},{ass_color(COLOR_TITLE_ACCENT)},&H000000FF,{black},{shadow},0,0,0,0,100,100,0,0,1,6,0,5,40,40,0,1",
        f"Style: Dialog,{f_sub},{L['sub_size']},{white},&H000000FF,{black},{shadow},0,0,0,0,100,100,0,0,1,4,1,2,60,60,0,1",
        f"Style: Narr,{f_sub},{L['sub_size']},{ass_color(COLOR_NARR)},&H000000FF,{black},{shadow},0,0,0,0,100,100,0,0,1,4,1,2,60,60,0,1",
        f"Style: Effect,{f_title},{L['effect_size']},{ass_color(COLOR_EFFECT)},&H000000FF,{black},{shadow},0,0,0,0,100,100,0,0,1,5,2,5,40,40,0,1",
        f"Style: Work,{f_title},54,{white},&H000000FF,{black},{shadow},0,0,0,0,100,100,6,0,1,2,0,5,40,40,0,1",
        f"Style: Copy,{f_sub},{L.get('copy_size', COPY_SIZE)},{white},&H000000FF,{black},{shadow},0,0,0,0,100,100,1,0,1,2,0,5,40,40,0,1",
    ]
    head = ("[Script Info]\nScriptType: v4.00+\n" f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            + "\n".join(styles) + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev: list[str] = []

    def add(layer: int, s: float, e: float, style: str, text: str) -> None:
        ev.append(f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},{style},,0,0,0,,{text}")

    t_text = _esc(l1) + (f"\\N{{\\c{ass_color(L['title_line2_color'])}&}}{_esc(l2)}" if l2 else "")
    add(0, 0.0, total, "Title", f"{{\\an5\\pos({W//2},{L['title_y']})}}{t_text}")
    if L.get("work_y") and work_title and not L.get("logo_box"):          # 로고가 있으면 작품명 텍스트는 안 그린다(로고가 대신)
        add(0, 0.0, total, "Work", f"{{\\an5\\pos({W//2},{L['work_y']})}}{_esc(work_title)}")
    if L.get("copy_y") and copy_text:
        add(0, 0.0, total, "Copy", f"{{\\an5\\pos({W//2},{L['copy_y']})}}{_esc(copy_text)}")
    sub_y = L["sub_y"]
    colors = speaker_colors(table["rows"])
    big = int(L["sub_size"] * SHORT_LINE_SCALE)
    for r in table["rows"]:
        s = r["t0"]
        e = s + (r["dur_video"] if r["mode"] == "N" else r["dur"])
        if r["mode"] == "S":
            spk = r.get("speaker") or ""
            sp = "" if spk in UNKNOWN_SPEAKERS else _esc((name_map or {}).get(spk, spk))   # 화자 라벨은 배우 이름(가이드 '배우:') · 미상은 라벨 없음
            if sp and r.get("label_suffix"):                                              # "(전화)" — 화면에 없는 목소리임을 알린다(2026-09-13 2화 v8)
                sp = f"{sp} {_esc(r['label_suffix'])}"
            col = ass_color(colors.get(r.get("speaker") or "", "#FFFFFF"))
            subs = r.get("sub_lines") or [{"start": r["cuts"][0]["in"], "end": r["cuts"][0]["out"], "text": r["text"]}]
            base = r["cuts"][0]["in"]
            for j, sl in enumerate(subs):                       # 줄마다 자기 단어 시각에 — 다음 줄 시작까지 유지(빈틈 없이)
                ss = s + max(0.0, sl["start"] - base)
                ee = s + (subs[j + 1]["start"] - base) if j + 1 < len(subs) else e
                ee = max(ss + 0.3, min(e, ee))
                text = sl["text"]
                lines_fit, fs = fit_subtitle(text, font_path=str(FONTS_DIR / "JalnanGothic.ttf"), size=L["sub_size"])
                size_tag = f"\\fs{big}" if len(text.replace(" ", "")) <= SHORT_LINE_CHARS else (f"\\fs{fs}" if fs != L["sub_size"] else "")
                body = _esc("\\N".join(lines_fit)) if False else "\\N".join(_esc(x) for x in lines_fit)
                label = f"{{\\fs38\\c{ass_color(COLOR_TITLE_ACCENT)}&}}{sp}{{\\r}}\\N" if sp else ""      # 화자를 모르는 줄(내레이션 목소리·군중)은 라벨 줄 자체를 뺀다
                add(1, ss, ee, "Dialog", f"{{\\an2\\pos({W//2},{sub_y})}}{label}{{\\c{col}&{size_tag}}}{body}")
        elif r["mode"] == "N":
            lines_fit, fs = fit_subtitle(r["text"], font_path=str(FONTS_DIR / "JalnanGothic.ttf"), size=L["sub_size"])
            fs_tag = f"\\fs{fs}" if fs != L["sub_size"] else ""
            add(1, s, e, "Narr", f"{{\\an2\\pos({W//2},{sub_y}){fs_tag}}}" + "\\N".join(_esc(x) for x in lines_fit))
        if r.get("effect") and not ("effect_plan" in r and r["effect_plan"] is None):     # None = 5.6 이 '자리 없음'으로 생략한 자막
            plan = r.get("effect_plan")
            if plan:                                            # 5.6 정밀 배치(얼굴 회피 · 단어 앵커 · 1/30s 격자)
                es, ee, ex, ey = plan["start"], plan["end"], plan["x"], plan["y"]
            else:
                es, ee = effect_window(r, s, e)
                ex, ey = W // 2, L["effect_y"]
            add(2, es, ee, "Effect", f"{{\\an5\\pos({ex},{ey})\\fscx35\\fscy35\\t(0,150,\\fscx115\\fscy115)\\t(150,280,\\fscx100\\fscy100)}}{_esc(r['effect'])}")
    return head + "\n".join(ev) + "\n"


def _filter_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _image_size(p: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(p) as im:
        return im.size


def render(job: Job, table: dict, *, title: str, out_name: str | None = None, layout: str = DEFAULT_LAYOUT,
           work_title: str | None = None, logo: Path | None = None, copy_text: str | None = None, copy_pos: str = "below",
           name_map: dict[str, str] | None = None, tag: str = "") -> Path:
    n = table["version"]["n"]
    src_w, src_h = 1920, 1080
    pre = ""
    if job.has("probe.json"):
        from app.tikitaka.probe import detect_active_area
        act = detect_active_area(job)                      # 레터박스 띠를 먼저 잘라낸다 — 이후 모든 좌표는 활성 영역 기준
        src_w, src_h = int(act["w"]), int(act["h"])
        if act.get("letterbox"):
            pre = f"crop={act['w']}:{act['h']}:{act['x']}:{act['y']},"
    logo = Path(logo) if logo else None
    if logo and not logo.exists():
        raise FileNotFoundError(f"로고 이미지가 없다: {logo}")
    L = compute_layout(layout, src_w=src_w, src_h=src_h, title=title, logo_size=_image_size(logo) if logo else None,
                       copy_text=copy_text, copy_pos=copy_pos)
    sfx = f"_{tag}" if tag else ""
    out = job.path(out_name or f"shorts_v{n}{sfx}.mp4")
    ass_path = job.path(f"subtitles_v{n}{sfx}.ass")
    ass_path.write_text(build_ass(table, title=title, layout=L, work_title=work_title or job.title, copy_text=copy_text, name_map=name_map),
                        encoding="utf-8")
    ffmpeg = find_bin("ffmpeg")
    cmd = [ffmpeg, "-y", "-v", "error", "-stats"]
    seg_filters: list[str] = []
    k = 0
    for r in table["rows"]:
        mute = r["mode"] == "N"
        for c in r["cuts"]:
            cmd += ["-ss", f"{c['in']:.3f}", "-t", f"{c['dur']:.3f}", "-i", str(job.source)]
            crop = L["crop"]
            if c.get("frame_segs"):                                # 5.5단계 샷별 주인물 크롭 x(같은 샷 안에서만 팬 · 경계에서 점프)
                from app.tikitaka.framing import crop_x_expr_segs
                cx = crop_x_expr_segs(c["frame_segs"], src_w=L["src_w"], crop_w=L["crop_w"])
                crop = f"crop={L['crop_w']}:{L['src_h']}:{cx}:0"
            elif "frame_x0" in c:                                 # 구간 정보 없는 옛 테이블 — 컷 하나 = 구간 하나
                from app.tikitaka.framing import crop_x_expr
                cx = crop_x_expr(c["frame_x0"], c.get("frame_x1", c["frame_x0"]), src_w=L["src_w"], crop_w=L["crop_w"], dur=c["dur"])
                crop = f"crop={L['crop_w']}:{L['src_h']}:{cx}:0"
            if c.get("frame_wide"):                               # 와이드: 흐린 배경(중앙 크롭) 위에 전체 프레임을 폭 맞춰 얹는다
                seg_filters.append(f"[{k}:v]fps={FPS},{pre}trim=duration={c['dur']:.3f},setpts=PTS-STARTPTS,split=2[v{k}a][v{k}b]")
                seg_filters.append(f"[v{k}a]{L['crop']},scale={W}:{L['band_h']}:flags=bicubic,boxblur=luma_radius=30:luma_power=2:"
                                   f"chroma_radius=15:chroma_power=1,eq=brightness=-0.12[v{k}bg]")
                seg_filters.append(f"[v{k}b]scale={W}:-2:flags=bicubic[v{k}fg]")
                seg_filters.append(f"[v{k}bg][v{k}fg]overlay=(W-w)/2:(H-h)/2,setsar=1,pad={W}:{H}:0:{L['band_y']}:color=black[v{k}]")
            else:
                seg_filters.append(f"[{k}:v]fps={FPS},{pre}{crop},scale={W}:{L['band_h']}:flags=bicubic,setsar=1,"
                                   f"pad={W}:{H}:0:{L['band_y']}:color=black,trim=duration={c['dur']:.3f},setpts=PTS-STARTPTS[v{k}]")
            vol = ",volume=0" if mute else ""
            seg_filters.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={c['dur']:.3f},"
                               f"asetpts=PTS-STARTPTS{vol}[a{k}]")
            k += 1
    n_clips = k
    tts_inputs: list[tuple[int, dict]] = []
    for r in table["rows"]:
        if r["mode"] == "N" and r.get("tts"):
            cmd += ["-i", r["tts"]]
            tts_inputs.append((k, r))
            k += 1
    logo_idx = None
    if logo and L.get("logo_box"):
        cmd += ["-i", str(logo)]                          # 정지 이미지 한 장 — overlay 기본 eof_action=repeat 로 끝까지 유지
        logo_idx = k
        k += 1
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(n_clips))
    filters = seg_filters + [f"{concat_in}concat=n={n_clips}:v=1:a=1[vc][ac]"]
    v_cur = "[vc]"
    if logo_idx is not None:
        b = L["logo_box"]
        filters.append(f"[{logo_idx}:v]format=rgba,scale={b['w']}:{b['h']}:flags=lanczos[lg]")
        filters.append(f"{v_cur}[lg]overlay={b['x']}:{b['y']}:format=auto[vl]")
        v_cur = "[vl]"
    mix_in = "[ac]"
    for j, (idx, r) in enumerate(tts_inputs):
        delay_ms = int(round(r["t0"] * 1000))
        filters.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={max(0.05, r['dur_video']):.3f},"
                       f"adelay={delay_ms}|{delay_ms}[t{j}]")
        mix_in += f"[t{j}]"
    if tts_inputs:
        filters.append(f"{mix_in}amix=inputs={len(tts_inputs)+1}:duration=first:normalize=0[am]")
        a_out = "[am]"
    else:
        a_out = "[ac]"
    filters.append(f"{v_cur}ass='{_filter_path(ass_path)}':fontsdir='{_filter_path(FONTS_DIR)}'[vout]")
    script = job.path(f"filtergraph_v{n}{sfx}.txt")
    script.write_text(";\n".join(filters), encoding="utf-8")
    cmd += ["-filter_complex_script", str(script), "-map", "[vout]", "-map", a_out,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    job.log(f"[render] 레이아웃 {layout}(밴드 {W}×{L['band_h']} y{L['band_y']} · 소스 활성 {src_w}×{src_h}) · 클립 {n_clips} · TTS {len(tts_inputs)} · 예상 {table['total_sec']:.1f}s → {out.name}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 실패: {proc.stderr[-1500:]}")
    ffprobe = find_bin("ffprobe")
    dur = json.loads(subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(out)],
                                    capture_output=True, text=True).stdout)["format"]["duration"]
    job.log(f"[render] 완료 — {out.name} {float(dur):.2f}s ({out.stat().st_size/1e6:.1f}MB)")
    job.record_step(f"render_v{n}{sfx}", output=str(out), duration_sec=ms3(float(dur)), clips=n_clips, tts=len(tts_inputs), voice=table.get("voice"), speed=table.get("speed"),
                    logo=str(logo) if logo else None, logo_box=L.get("logo_box"), copy_text=copy_text, copy_pos=copy_pos if copy_text else None)
    return out
