"""E17-2 (2026-08-24) — 소스에 이미 박힌 자막을 피해서 우리 자막을 놓는다.

사용자 지시: "영상에 원래 자막이 있으면 그 위치 피해서 자막이 들어가게 해줘.
물론, 자막이 제목과도 겹치면 안되고."

권리사 소재(방송 예능)는 대사 자막·텔롭이 **이미 화면에 구워져** 있다. 우리 자막은
영상 밴드 하단 10px 위(`pipeline._compute_subtitle_margin_v`)에 고정이라, 원본 자막이
같은 자리에 있으면 두 겹이 겹쳐 둘 다 못 읽는다. 여기서 그 띠를 찾아 우리 자막을
**위로만** 밀어 준다.

설계 규율 넷:

1. **좌표계는 캔버스다.** 소스 픽셀 행을 재서 렌더 수식으로 환산하면(리프레임 크롭이
   `t` 에 따라 움직인다) 언젠가 어긋난다. 그래서 검출도 렌더와 **같은 필터 체인**
   (crop_timeline → scale → crop)을 태운 뒤 재고, 나온 행은 밴드 안 비율 그대로
   캔버스 y 가 된다. 밴드 기하는 `band_geometry` 한 곳(렌더러 [2]와 같은 수식,
   `tests/test_e17_burned_subtitle.py` 가 `pipeline._video_band_bottom` 과 대조한다).
2. **올리기만 한다.** 아래로 내리면 로고·작품명 스택과 부딪히고, 시작을 당기면
   소리보다 자막이 먼저 뜬다. 위로 밀되 **제목 아래**를 넘지 않는다(사용자 조건).
   둘 다 만족 못 하면 제목을 우선하고 **모자란 만큼을 stdout 에 남긴다**(조용한 포기 금지).
3. **못 찾으면 아무것도 안 한다.** 검출 실패·ffmpeg 없음·프레임 부족은 전부 종전
   margin 그대로다(회귀 0). 연출이 아니라 안전장치다.
4. **판정은 순수 함수로 쪼갠다** — 프레임 바이트만 있으면 ffmpeg 없이 테스트된다.

⚠ 임계값(아래 상수)은 합성 프레임과 규격 추정으로 잡은 **초기값**이다. 실소재로
   다시 재려면 `python -m scripts.e17_burned_subtitle_probe --video … --start … --end …`
   가 행별 점수와 판정된 띠를 그대로 찍어 준다.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.modules.ffmpeg_utils import find_ffmpeg_command

# ── 검출 파라미터 ───────────────────────────────────────────────────────────
# 밴드를 이 크기 회색조로 줄여서 잰다. 가로를 크게 잡는 이유: 판정 근거가 **획의
# 밝기 경계 개수**라 가로로 뭉개면 글자가 사라진다. 세로는 행 구조만 보면 되므로 작다.
PROBE_W = 480
PROBE_H = 270
# 흰 글자 + 검은 외곽선의 경계 = "밝은 픽셀 옆에 급격한 밝기 차". 자연 영상의 완만한
# 그라데이션은 EDGE_DELTA 를 못 넘고, 어두운 잡티는 BRIGHT_MIN 을 못 넘는다.
BRIGHT_MIN = 190
EDGE_DELTA = 55
# 한 행이 '글자 행'으로 세어지는 최소 경계 개수(가로 폭 대비 비율).
MIN_EDGE_RATIO = 0.06
# 그 행이 **표본 프레임의 이 비율 이상**에서 글자 행이어야 띠로 인정한다. 원본 자막은
# 편 내내 같은 자리에 뜨고, 배경의 밝은 무늬는 장면이 바뀌면 사라진다.
MIN_FRAME_HIT_RATIO = 0.5
# 표본이 이보다 적으면 판정하지 않는다(장면 하나만 보고 채널 자막을 옮기면 안 된다).
MIN_FRAMES = 6
MIN_CLIPS = 2
# 띠 높이 한계(밴드 높이 대비). 너무 얇으면 잡티, 너무 두꺼우면 자막이 아니라 장면이다.
MIN_BAND_RATIO = 0.03
MAX_BAND_RATIO = 0.30
# 밴드의 아래쪽 이 비율부터만 찾는다 — 위·가운데 텔롭은 우리 자막과 겹치지 않으므로
# 건드릴 이유가 없다(겹치지도 않는 것을 피하려다 자막을 엉뚱한 데로 올리면 손해다).
SEARCH_FROM_RATIO = 0.50
# 행 사이 이 정도 빈틈은 같은 띠로 잇는다(2줄 자막의 줄 간격).
ROW_GAP_MERGE = 6
# 우리 자막과 원본 자막 사이·제목과 자막 사이에 두는 여백(캔버스 px).
GAP_PX = 14
# 표본 — 클립 수와 클립당 프레임 수. 늘리면 정확해지고 렌더 앞에 붙는 시간이 는다.
MAX_PROBE_CLIPS = 4
FRAMES_PER_CLIP = 6

# ASS 한 줄 높이 ≈ 글자 크기 × 이 값. 대사 자막은 최대 2줄(`_wrap_for_ass(max_lines=2)`).
LINE_HEIGHT_RATIO = 1.25
MAX_SUBTITLE_LINES = 2


@dataclass(frozen=True)
class BandGeometry:
    """캔버스 안 영상 밴드 직사각형. 렌더러 [2]와 같은 수식이다."""

    scaled_w: int
    scaled_h: int
    overlay_y: int
    pad_x: int

    @property
    def top(self) -> int:
        return self.overlay_y

    @property
    def bottom(self) -> int:
        return self.overlay_y + self.scaled_h


def band_geometry(design: Any, *, canvas_width: int = 1080,
                  canvas_height: int = 1920) -> BandGeometry:
    """DesignConfig → 밴드 기하. 렌더러 `_build_filtergraph` [2]와 **같은 순서**로 센다.

    (video_width 미지정 = 캔버스 꽉 참, video_y 미지정 = 세로 중앙, 짝수 보정은 클램프
    **뒤**에 온다 — 렌더러와 1px 이라도 달라지면 검출한 띠가 화면과 어긋난다.)
    """
    W, H = int(canvas_width), int(canvas_height)
    try:
        scaled_w = int(str(getattr(design, "video_width", None) or W))
    except (TypeError, ValueError):
        scaled_w = W
    try:
        r_w, r_h = map(int, str(getattr(design, "aspect_ratio", "1:1")).split(":"))
        scaled_h = int(scaled_w * r_h / r_w)
    except Exception:
        scaled_h = scaled_w
    video_y = getattr(design, "video_y", None)
    if video_y is not None:
        try:
            overlay_y = min(max(0, int(video_y)), max(0, H - scaled_h))
        except (TypeError, ValueError):
            overlay_y = (H - scaled_h) // 2
    else:
        overlay_y = (H - scaled_h) // 2
    scaled_w -= scaled_w % 2
    scaled_h -= scaled_h % 2
    return BandGeometry(scaled_w=scaled_w, scaled_h=scaled_h,
                        overlay_y=max(0, overlay_y), pad_x=(W - scaled_w) // 2)


def estimate_subtitle_height(font_size: int, *, lines: int = MAX_SUBTITLE_LINES) -> int:
    """자막 블록의 세로 크기(px) 추정 — 대사·TTS 자막 둘 다 이걸로 겹침 판정·위 여백을 잰다
    (TTS 는 `tts_line_font_size` 를 넣어 같은 함수를 다시 부른다 — E17-2 정정).

    **최대 줄 수 기준**이다(한 줄짜리에 맞추면 두 줄 자막이 원본 자막을 그대로 덮는다).
    ⚠ **회전(`design.tts_rotate`)은 반영하지 않는다** — 기울면 바운딩 박스가 커지지만,
    이 함수는 세로 폭 추정이 목적이라 축 정렬 높이만 잰다. 큰 각도로 기운 TTS 자막이
    원본 자막과 아슬아슬하게 붙는 경우는 이 회피가 완전히 못 잡을 수 있다(드문 조합).
    """
    return max(1, int(round(float(font_size) * LINE_HEIGHT_RATIO * max(1, int(lines)))))


def estimate_title_line_count(title_text: str, *, max_chars: int = 14) -> int:
    """제목이 화면에서 차지하는 **줄 수** 추정 — 렌더러 `split_text_smart` 와 같은 상한.

    렌더러는 어절 경계로 접으므로 실제로는 이보다 한 줄 더 늘 수도 있지만, 이 값은
    '자막을 이 아래로만 올린다'는 하한 계산에 쓰이므로 과하게 잡는 쪽이 안전하다.
    """
    lines = [ln for ln in str(title_text or "").split("\n") if ln.strip()]
    if not lines:
        return 1
    return max(1, sum(max(1, -(-len(ln) // max_chars)) for ln in lines))


def estimate_title_bottom(design: Any, geom: BandGeometry, *,
                          line_count: int = 2, line_spacing: int = 30) -> int:
    """제목 블록의 아래 끝 y(캔버스 px) — 렌더러 [5] 배치 규칙을 그대로 따른 **추정**.

    렌더러는 줄바꿈으로 늘어난 줄 수·길이별 축소까지 반영하지만, 여기 쓰임새는 '자막을
    이 아래로만 올린다'는 **하한**이라 블록을 크게 잡는 쪽이 안전하다(줄 수는 호출부가
    실제 제목으로 세어 넘긴다).
    """
    sizes = list(getattr(design, "title_sizes", None) or [int(getattr(design, "title_size", 70))])
    boxes = list(getattr(design, "title_boxes", None) or ["none"])
    n = max(1, int(line_count))
    total = 0
    for i in range(n):
        size = int(sizes[i] if i < len(sizes) else sizes[-1])
        box = str(boxes[i] if i < len(boxes) else boxes[-1])
        total += size + (2 * int(round(0.30 * size)) if box != "none" else 0)
    total += max(0, n - 1) * line_spacing

    title_y = int(getattr(design, "title_y", 120))
    dynamic_top = geom.overlay_y - total - 20      # 렌더러 _gap_above_video = 20
    if getattr(design, "title_y_fixed", False):
        top = title_y
    elif dynamic_top >= 10:
        top = dynamic_top
    else:
        top = title_y
    return top + total


# ─────────────────────────────────────────────────────────────────────────
# 판정 (순수 — ffmpeg 없이 테스트된다)
# ─────────────────────────────────────────────────────────────────────────
def row_edge_counts(frame: bytes, width: int, height: int) -> list[int]:
    """회색조 프레임 → 행별 '글자 경계' 개수.

    글자 경계 = 이웃 픽셀 밝기차가 EDGE_DELTA 이상이고 둘 중 하나가 BRIGHT_MIN 이상.
    흰 글자에 검은 외곽선이라는 자막의 형태를 그대로 세는 것이라, 완만한 배경
    그라데이션(차이가 작다)과 어두운 무늬(밝지 않다)는 안 걸린다.
    """
    out: list[int] = []
    for y in range(height):
        row = frame[y * width:(y + 1) * width]
        if len(row) < 2:
            out.append(0)
            continue
        out.append(sum(1 for a, b in zip(row, row[1:])
                       if abs(a - b) >= EDGE_DELTA and (a >= BRIGHT_MIN or b >= BRIGHT_MIN)))
    return out


def row_hit_ratios(frames: list[bytes], width: int, height: int) -> list[float]:
    """행마다 '글자 행이었던 프레임 비율'. 원본 자막은 여러 장면에 걸쳐 같은 행에 남는다."""
    if not frames:
        return [0.0] * height
    need = max(1, int(round(width * MIN_EDGE_RATIO)))
    hits = [0] * height
    for f in frames:
        for y, c in enumerate(row_edge_counts(f, width, height)):
            if c >= need:
                hits[y] += 1
    return [h / len(frames) for h in hits]


def band_from_ratios(ratios: list[float], *,
                     threshold: float = MIN_FRAME_HIT_RATIO,
                     search_from_ratio: float = SEARCH_FROM_RATIO,
                     gap_merge: int = ROW_GAP_MERGE,
                     min_rows: int | None = None,
                     max_rows: int | None = None) -> tuple[int, int] | None:
    """행 비율 목록 → (top_row, bottom_row) 또는 None. 반열린 구간(bottom 은 제외).

    아래쪽(search_from_ratio~끝)에서 threshold 를 넘는 행을 이어 붙이고, 높이 조건을
    만족하는 것 중 **가장 아래 것**을 고른다 — 우리 자막과 겹치는 것이 그것이다.
    """
    h = len(ratios)
    if h == 0:
        return None
    lo = int(h * search_from_ratio)
    min_rows = max(1, int(h * MIN_BAND_RATIO)) if min_rows is None else min_rows
    max_rows = max(min_rows, int(h * MAX_BAND_RATIO)) if max_rows is None else max_rows

    runs: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    gap = 0
    for y in range(lo, h):
        if ratios[y] >= threshold:
            if start is None:
                start = y
            gap = 0
            end = y + 1
        elif start is not None:
            gap += 1
            if gap > gap_merge:
                runs.append((start, end))
                start = None
                gap = 0
    if start is not None:
        runs.append((start, end))

    ok = [r for r in runs if min_rows <= (r[1] - r[0]) <= max_rows]
    return max(ok, key=lambda r: r[1]) if ok else None


def avoid_margin_v(margin_v: int, *,
                   canvas_height: int,
                   burned_top: int,
                   burned_bottom: int,
                   subtitle_height: int,
                   title_bottom: int,
                   band_top: int,
                   gap: int = GAP_PX) -> tuple[int, list[str]]:
    """원본 자막 띠를 피한 margin_v 와 사람이 읽을 메모. 순수.

    규칙(위 규율 2):
      · 겹치지 않으면 그대로 둔다.
      · 겹치면 우리 자막 **아래끝을 띠 위 gap 까지** 올린다.
      · 제목 아래(title_bottom+gap)·밴드 위(band_top)를 넘지 않는다 — 넘겨야 피할 수
        있는 경우엔 갈 수 있는 데까지만 가고 **모자란 양을 메모에 남긴다**.
      · 올린 결과가 원래보다 아래면 그대로 둔다(내리지 않는다).
    """
    H = int(canvas_height)
    notes: list[str] = []
    sub_bottom = H - int(margin_v)
    sub_top = sub_bottom - int(subtitle_height)
    if sub_top >= int(burned_bottom) or sub_bottom <= int(burned_top):
        return int(margin_v), notes            # 안 겹친다 — 종전 그대로(회귀 0)

    floor_top = max(int(title_bottom) + gap, int(band_top))
    want_bottom = int(burned_top) - gap
    want_top = want_bottom - int(subtitle_height)
    if want_top < floor_top:
        short = floor_top - want_top
        want_bottom = floor_top + int(subtitle_height)
        notes.append(
            f"[SubtitleAvoid/미달] 원본 자막을 다 피하려면 {short}px 더 올려야 하지만 "
            f"제목·영상 위끝(y={floor_top})에 막혔습니다 — 갈 수 있는 데까지만 올립니다")
    if want_bottom >= sub_bottom:
        notes.append("[SubtitleAvoid] 올릴 자리가 없어 종전 위치를 그대로 씁니다")
        return int(margin_v), notes
    new_margin = max(0, H - want_bottom)
    notes.append(
        f"[SubtitleAvoid] 원본 자막 띠 y={burned_top}~{burned_bottom} 회피 — "
        f"자막 아래끝 {sub_bottom} → {want_bottom} (margin_v {margin_v} → {new_margin})")
    return new_margin, notes


# ─────────────────────────────────────────────────────────────────────────
# 표본 수집 (ffmpeg)
# ─────────────────────────────────────────────────────────────────────────
def _crop_filter_for(crop_path: Path | None) -> str:
    """리프레임 크롭 타임라인 → 렌더러 [3]과 **같은** crop 필터 문자열(없으면 빈 문자열)."""
    if not crop_path:
        return ""
    p = Path(crop_path)
    if not p.exists():
        return ""
    try:
        from app.modules.renderer import _build_crop_expr

        data = json.loads(p.read_text(encoding="utf-8"))
        if not data:
            return ""
        cw, ch = data[0]["crop_w"], data[0]["crop_h"]
        x_expr = _build_crop_expr(data, "x_center")
        y_expr = _build_crop_expr(data, "y_center")
        return f"crop={cw}:{ch}:x='({x_expr})-{cw}/2':y='({y_expr})-{ch}/2',"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def sample_band_frames(video_path: Path, clip, geom: BandGeometry,
                       *, crop_path: Path | None = None,
                       frames: int = FRAMES_PER_CLIP,
                       probe_w: int = PROBE_W, probe_h: int = PROBE_H,
                       timeout_sec: float = 120.0) -> list[bytes]:
    """클립 하나에서 **밴드 화면 그대로** 회색조 프레임을 뽑는다(렌더와 같은 체인).

    `-ss/-to` 로 클립을 잘라 넣는 것도 렌더와 같다 — crop 표현식의 `t` 가 같은 0 기준이라야
    검출한 띠가 실제 화면과 같은 자리다.
    """
    start = float(getattr(clip, "start_sec", 0.0))
    end = float(getattr(clip, "end_sec", start))
    dur = max(0.1, end - start)
    fps = max(0.1, min(float(frames) / dur, 4.0))
    vf = (f"fps={fps:.4f},{_crop_filter_for(crop_path)}"
          f"scale={geom.scaled_w}:{geom.scaled_h}:force_original_aspect_ratio=increase,"
          f"setsar=1,crop={geom.scaled_w}:{geom.scaled_h},"
          f"scale={probe_w}:{probe_h},format=gray")
    cmd = [find_ffmpeg_command("ffmpeg"), "-v", "error", "-nostdin",
           "-ss", f"{start}", "-to", f"{end}", "-i", str(video_path),
           "-an", "-sn", "-dn", "-vf", vf, "-frames:v", str(int(frames)),
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout_sec)
    raw = proc.stdout or b""
    size = probe_w * probe_h
    return [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]


def detect_burned_band(video_path: Path, clips: list, design: Any, *,
                       crop_map: dict | None = None,
                       canvas_width: int = 1080, canvas_height: int = 1920,
                       max_clips: int = MAX_PROBE_CLIPS,
                       frames_per_clip: int = FRAMES_PER_CLIP,
                       sampler=None) -> dict[str, Any] | None:
    """소스에 박힌 자막 띠를 캔버스 y 로 돌려준다(못 찾으면 None).

    반환: {"top", "bottom", "frames", "clips", "probe_rows", "hit_ratio"}.
    sampler 는 테스트 주입용(클립 → 프레임 바이트 목록). 실패는 전부 None 이다 —
    안전장치가 본편 발행을 막으면 안 된다.
    """
    if not clips:
        return None
    geom = band_geometry(design, canvas_width=canvas_width, canvas_height=canvas_height)
    if geom.scaled_h <= 0:
        return None
    # 표본 클립은 앞·중간·뒤에 고루 — 한 장면만 보면 그 장면의 밝은 무늬가 자막이 된다.
    idxs = list(range(len(clips)))
    if len(idxs) > max_clips:
        step = len(idxs) / float(max_clips)
        idxs = [int(i * step) for i in range(max_clips)]
    frames: list[bytes] = []
    used_clips = 0
    for i in idxs:
        clip = clips[i]
        crop_path = None
        if crop_map:
            crop_path = crop_map.get(f"{getattr(clip, 'role', '')}_{i}")
        try:
            got = (sampler(clip, crop_path) if sampler else
                   sample_band_frames(Path(video_path), clip, geom, crop_path=crop_path,
                                      frames=frames_per_clip))
        except Exception as e:                      # ffmpeg 없음·디코드 실패 등
            print(f"  [SubtitleAvoid] 표본 수집 실패(clip {i}: {e}) — 이 클립은 건너뜁니다")
            continue
        if got:
            used_clips += 1
            frames.extend(got)
    if len(frames) < MIN_FRAMES or used_clips < min(MIN_CLIPS, len(idxs)):
        print(f"  [SubtitleAvoid] 표본 부족(프레임 {len(frames)}·클립 {used_clips}) — 판정하지 않습니다")
        return None

    ratios = row_hit_ratios(frames, PROBE_W, PROBE_H)
    rows = band_from_ratios(ratios)
    if rows is None:
        return None
    top_row, bottom_row = rows
    px_per_row = geom.scaled_h / float(PROBE_H)
    return {
        "top": int(geom.top + top_row * px_per_row),
        "bottom": int(geom.top + bottom_row * px_per_row),
        "frames": len(frames),
        "clips": used_clips,
        "probe_rows": [top_row, bottom_row],
        "hit_ratio": round(max(ratios[top_row:bottom_row] or [0.0]), 3),
    }
