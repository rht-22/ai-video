"""편 썸네일 후보 — 렌더가 끝난 편에서 후보 프레임을 뽑고, Flash 가 고르고 라벨을 제안한다(2026-09-26 사용자 결정).

사람이 손으로 하던 순서(로또 1-2화~5-6화 썸네일, tmp/lotto_*/thumbs*/clean.py · make_vN.py)를 옮긴 것이다.

1. 후보 프레임: 최종 렌더 필터 그래프(`final_1080x1920.filter.txt`)에서 그 클립의 크롭 체인 + 고정 띠(제목·로고·문구)만
   원본에 다시 태운다 → 자막·내레이션 글자·라벨이 없는 깨끗한 1080×1920 프레임. 픽셀 생성·보정 없음.
2. 점수: 영상 밴드 안 선명도 + 얼굴 크기(YuNet) + 원본 분석의 장면 중요도. 클립당 2장까지, 상위 CANDIDATE_MAX 장.
3. 선택: Flash 에 후보(밴드 부분만 작게) + 그 순간 대사·화면 기록을 보여 주고 최대 PICK_MAX 장을 순서대로 고르게 한다.
   라벨 문구·색도 같이 제안받는다. 결과는 캐시(select.json · 후보 지문).
4. 합성: 라벨은 영상 라벨과 같은 잘난고딕(Jalnan Gothic) · 외곽선 · 살짝 기울임. 위치는 얼굴 위쪽 빈자리(없으면 아래·밴드 위),
   글자 폭은 폰 안전 폭(safe_zone SAFE_X0~SAFE_X1) 안으로 줄인다.

사람이 고르는 단계는 남긴다 — 자동 선택은 '제안'이다(표정 취향은 사람이 자주 다시 고른다 · 2026-09-26 v8 장경철 컷).
`manual.json`(선택 목록)이 있으면 Flash 를 부르지 않고 그 목록으로 합성한다.

사용:
  python -m app.tikitaka.thumbnail <job_dir> v8            # 후보 → Flash 선택 → 합성
  python -m app.tikitaka.thumbnail <job_dir> v8 --no-llm   # 점수 상위로만(호출 없음)
산출: <job_dir>/thumbnails/v8/ — frames/ · sheet.jpg · select.json · thumb_1..N.png · compare.png · thumbnails.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from app.tikitaka.common import Job, find_bin

SAMPLE_FPS = 2                 # 클립마다 초당 2장(사람이 시트로 보던 간격)
MIN_CLIP_SEC = 0.5             # 이보다 짧은 조각은 2fps 표본이 안 나온다
PER_CLIP_MAX = 2
CANDIDATE_MAX = 16             # Flash 에 보여 줄 후보 수(밴드만 360px — 인라인 JPEG)
PICK_MAX = 6
LABEL_SIZE = 72
LABEL_MIN_SIZE = 52
LABEL_GAP = 50                 # 얼굴과 라벨 사이
LABEL_TOP_CLEAR = 150          # 밴드 윗변에서 이만큼은 비운다 — 플랫폼 로고(TVING 등)가 밴드 모서리에 있다(2026-09-26 v8 겹침)
PROMPT_VERSION = 3
SPLIT_SIZE = 110               # 양옆 나눔 라벨 크기(v11 「(긴 / 장)」 손작업 값)
SPLIT_MIN_SIZE = 80
SPLIT_GAP = 40                 # 얼굴 가장자리와 글자 사이
CANVAS_W, CANVAS_H = 1080, 1920
FONT_FILE = "JalnanGothic.ttf"
FONT_NAME = "Jalnan Gothic TTF"
COLORS = {                     # ASS &HBBGGRR& — 로또 썸네일에서 쓴 색
    "white": r"\1c&H00FFFFFF&\3c&H00000000&\bord5\shad1",
    "peach": r"\1c&H008AB3FF&\3c&H00000000&\bord6\shad2",
    "lime": r"\1c&H004AFFB7&\3c&H00000000&\bord6\shad2",
    "sky": r"\1c&H00FFC87E&\3c&H00000000&\bord6\shad2",
    "yellow": r"\1c&H003CE2FF&\3c&H00000000&\bord6\shad2",
    "neon": r"\1c&H00D2FFE6&\3c&H00002A10&\bord5\shad0",          # 형광: 글자 층(진한 테두리) — 뒤에 GLOW 층을 깐다
}
# 두 층으로 그리는 색 — 뒤 번짐 층만 두면 글자가 번져 안 읽힌다(2026-09-26 v12 「한 잔만 더 하자고~」 1차)
GLOW = {"neon": r"\1c&H0000FF9C&\3c&H0000FF9C&\bord14\blur10\shad0"}


def layers(color: str, tag_prefix: str, text: str) -> str:
    """ASS Dialogue 줄들 — 번짐 층(있으면, layer 0) + 글자 층(layer 1). 순수."""
    out = ""
    if color in GLOW:
        out += f"Dialogue: 0,0:00:00.00,0:00:10.00,Caption,,0,0,0,,{{{tag_prefix}{GLOW[color]}}}{text}\n"
    return out + f"Dialogue: 1,0:00:00.00,0:00:10.00,Caption,,0,0,0,,{{{tag_prefix}{COLORS[color]}}}{text}\n"
ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,5,1,5,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

SELECT_PROMPT = """너는 유튜브 쇼츠 썸네일 편집자다. 아래 후보 이미지는 쇼츠 「{title}」({work})의 한 장면씩이다.
이미지 순서가 곧 후보 번호다(0부터). 각 후보의 그 순간 대사·화면 기록:
{context}

좋은 썸네일 컷의 조건(중요한 순):
1. 인물의 감정이 또렷한 얼굴 — 놀람·분노·당황·단호함·웃음. 무표정·멍한 얼굴보다 낫다.
2. 눈을 뜨고 있고, 초점이 맞고, 흔들리지 않는다. 입이 어정쩡하게 벌어진 순간·눈 감은 순간·뒤통수·옆모습만 보이는 컷은 피한다.
3. 얼굴이 화면에 크게 잡혀 있다. 인물이 작게 여럿 선 와이드 컷은 사건이 한눈에 보일 때만.
4. 원본 워터마크·글자가 얼굴을 가리지 않는다.
5. 제목과 이야기가 맞는 인물·순간이다.
서로 다른 인물·다른 순간으로 고르면 사람이 비교하기 좋다.

각 선택에 라벨 한 줄을 붙여라 — 화면 위에 얹는 짧은 말(공백 포함 14자 이하, 괄호로 감싼다. 예: "(가만히 있을 것 같냐?)", "(할 말을 잃음)").
라벨 형식은 둘 중 하나다.
- "line": 얼굴 위(또는 아래)에 한 줄. 대사를 줄이거나 표정을 말할 때.
- "split": 한 단어 감정(2~4글자 — 긴장·당황·분노·충격·억울 등)을 둘로 나눠 **얼굴 양옆**에 크게 얹는다. 예: parts ["(긴", "장)"].
  얼굴 하나가 크게 잡힌 클로즈업이고 얼굴 양옆에 빈자리가 있을 때만 쓴다.
라벨은 **이미지 속 그 인물**의 말이나 표정이어야 한다. 먼저 이미지에 누가 보이는지(기록의 등장인물과 대조) 확인하고,
그 인물이 말한 대사일 때만 대사를 줄여 쓴다. 다른 사람이 한 대사를 그 얼굴에 붙이지 마라 — 그럴 땐 그 인물의 표정·반응을
한마디로 말한다(예: 상대의 말을 듣는 얼굴이면 "(어이없음)"). 기록에 없는 사실·인물 이름을 지어내지 마라. 라벨이 필요 없으면 빈 문자열.
색은 white·peach·lime·sky·yellow·neon 중 하나(neon = 형광 번짐, 들뜬·취한 말투에).

최대 {n}장을 좋은 순서대로 고른다. JSON 하나만:
{{"picks": [{{"id": 0, "person": "이미지 속 인물", "style": "line", "label": "(…)", "parts": [], "color": "white", "why": "한 문장"}}]}}"""


# ── 필터 그래프에서 깨끗한 프레임 ─────────────────────────────────────────────
def parse_filter(filter_text: str) -> tuple[dict[int, str], str]:
    """최종 렌더 필터 그래프 → (클립 번호 → 크롭 체인, 고정 띠 체인). 순수 — 테스트 대상.
    띠 = `[vcat]drawtext…` 부터 자막 ASS(`[with_cap]ass=`) 앞까지(제목·로고·문구·플랫폼 표기)."""
    try:
        brand = filter_text[filter_text.index("[vcat]drawtext"):filter_text.index(";[with_cap]ass=")]
    except ValueError as e:
        raise ValueError("필터 그래프에서 제목·로고 띠를 찾지 못했다(렌더 형식이 바뀌었나?)") from e
    chains = {int(m.group(1)): part for part in filter_text.split(";")
              for m in [re.match(r"\[(\d+):v\]", part)] if m}
    return chains, brand


def clean_graph(chain: str, i: int, brand: str, tail: str) -> str:
    """한 클립만 입력 0 으로 다시 태우는 그래프 — 순수."""
    return chain.replace(f"[{i}:v]", "[0:v]", 1).replace(f"[v{i}]", "[vcat]") + ";" + brand + ";" + tail


def _run_clip(source: Path, clip: dict, graph: str, out: Path, extra: tuple = ()) -> None:
    subprocess.run([find_bin("ffmpeg"), "-v", "error", "-y", "-ss", f"{clip['clip_start_sec']}", "-to", f"{clip['clip_end_sec']}",
                    "-i", str(source), "-filter_complex", graph, "-map", "[out]", *extra, str(out)],
                   check=True, capture_output=True)


def scan_frames(source: Path, timeline: list[dict], chains: dict[int, str], brand: str, frames_dir: Path, *, log=print) -> list[dict]:
    """클립마다 2fps 깨끗한 프레임(1080×1920 JPEG). 이미 있으면 재사용."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for i, c in enumerate(timeline):
        if i not in chains or c["clip_end_sec"] - c["clip_start_sec"] < MIN_CLIP_SEC:
            continue
        pattern = frames_dir / f"c{i:02d}_%02d.jpg"
        if not any(frames_dir.glob(f"c{i:02d}_*.jpg")):
            _run_clip(source, c, clean_graph(chains[i], i, brand, f"[with_cap]fps={SAMPLE_FPS}[out]"), pattern, ("-q:v", "2"))
        speed = float(c.get("playback_speed") or 1.0)
        for p in sorted(frames_dir.glob(f"c{i:02d}_*.jpg")):
            k = int(p.stem.split("_")[1]) - 1
            t = k / SAMPLE_FPS
            out.append({"id": p.stem, "clip": i, "clip_time_sec": t, "source_sec": round(c["clip_start_sec"] + t * speed, 3),
                        "span_ids": list(c.get("span_ids") or []), "path": str(p)})
    log(f"[thumb] 후보 프레임 {len(out)}장 — 클립 {len({f['clip'] for f in out})}개")
    return out


# ── 점수 ────────────────────────────────────────────────────────────────────
def band_rect(design: dict) -> tuple[int, int]:
    """영상 밴드 (윗변 y, 높이) — 채널 디자인의 aspect_ratio·video_y. 순수."""
    rw, rh = (int(x) for x in str(design.get("aspect_ratio") or "1:1").split(":"))
    h = int(CANVAS_W * rh / rw)
    y = design.get("video_y")
    y = int(y) if y is not None else (CANVAS_H - h) // 2
    return max(0, min(y, CANVAS_H - h)), h


def measure(frames: list[dict], band: tuple[int, int], importance: dict[str, int]) -> None:
    """선명도·얼굴·중요도를 프레임에 적고 score 를 매긴다(제자리)."""
    import cv2
    from app.modules.reframe import YUNET_MODEL_PATH, _YuNetDetector
    det = _YuNetDetector(YUNET_MODEL_PATH)
    y0, h = band
    for f in frames:
        img = cv2.imread(f["path"])
        crop = img[y0:y0 + h]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        faces = det.detect(gray, crop)
        boxes = [[int(x), int(y) + y0, int(w), int(hh)] for x, y, w, hh in (faces if len(faces) else [])]
        big = max((b[2] * b[3] for b in boxes), default=0) / float(CANVAS_W * h)
        imp = max((importance.get(s, 0) for s in f["span_ids"]), default=0)
        f.update(sharpness=round(sharp, 1), faces=boxes, face_area=round(big, 4), importance=imp)
        f["score"] = round(score(sharp, big, imp), 3)


def score(sharp: float, face_area: float, importance: int) -> float:
    """후보 점수 — 얼굴 크기 > 선명도 > 장면 중요도. 얼굴 없으면 크게 깎는다. 순수."""
    s = min(math.log10(max(sharp, 1.0)) / 3.0, 1.0)            # 라플라시안 분산 10~1000 → 0.33~1
    f = min(face_area / 0.06, 1.0)                              # 밴드 대비 6% 이상이면 만점(클로즈업)
    base = 0.5 * f + 0.3 * s + 0.2 * (importance / 5.0)
    return base if face_area > 0 else base * 0.4


def shortlist(frames: list[dict], n: int = CANDIDATE_MAX, per_clip: int = PER_CLIP_MAX) -> list[dict]:
    """점수 순 · 클립당 per_clip 장까지 · 같은 클립 안 표본은 1초 이상 떨어진 것만. 순수."""
    picked: list[dict] = []
    for f in sorted(frames, key=lambda x: -x["score"]):
        same = [p for p in picked if p["clip"] == f["clip"]]
        if len(same) >= per_clip or any(abs(p["clip_time_sec"] - f["clip_time_sec"]) < 1.0 for p in same):
            continue
        picked.append(f)
        if len(picked) >= n:
            break
    return picked


# ── Flash 선택 ─────────────────────────────────────────────────────────────
def context_lines(cands: list[dict], transcript: dict, facts: dict) -> str:
    lines = transcript.get("lines") or []
    out = []
    for k, c in enumerate(cands):
        t = c["source_sec"]
        said = [f"{l.get('speaker') or '?'}: {l['text']}" for l in lines if l["start"] - 1.5 <= t <= l["end"] + 1.5][:3]
        scene = next((facts[s].get("scene_script") for s in c["span_ids"] if facts.get(s, {}).get("scene_script")), "")
        chars = sorted({ch for s in c["span_ids"] for ch in (facts.get(s, {}).get("characters") or [])})
        out.append(f"{k}. 화면: {scene or '-'} / 이 컷 등장인물: {', '.join(chars) or '-'} / 앞뒤 대사: {' | '.join(said) or '-'}")
    return "\n".join(out)


def parse_picks(raw: Any, n_cands: int, limit: int = PICK_MAX) -> list[dict]:
    """Flash 응답 → 유효한 선택만(번호 범위·중복·색·라벨 길이). 순수 — 테스트 대상."""
    obj = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], dict) else raw
    picks = (obj or {}).get("picks") if isinstance(obj, dict) else None
    out, seen = [], set()
    for p in picks or []:
        try:
            i = int(p.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not 0 <= i < n_cands or i in seen:
            continue
        seen.add(i)
        label = str(p.get("label") or "").strip()
        if len(label) > 18:
            label = ""                                   # 너무 긴 제안은 버린다(사람이 채운다)
        color = p.get("color") if p.get("color") in COLORS else "white"
        parts = [str(x).strip() for x in (p.get("parts") or []) if str(x).strip()]
        style = "split" if p.get("style") == "split" and len(parts) == 2 and sum(len(x) for x in parts) <= 8 else "line"
        if style == "split" and not label:
            label = "".join(parts)
        out.append({"id": i, "label": label, "style": style, "parts": parts if style == "split" else [], "color": color,
                    "person": str(p.get("person") or ""), "why": str(p.get("why") or "")})
        if len(out) >= limit:
            break
    return out


def _band_jpg(src: str, dst: Path, band: tuple[int, int]) -> Path:
    if not dst.exists():
        y0, h = band
        subprocess.run([find_bin("ffmpeg"), "-v", "error", "-y", "-i", src, "-vf", f"crop={CANVAS_W}:{h}:0:{y0},scale=360:-2",
                        "-q:v", "4", str(dst)], check=True, capture_output=True)
    return dst


def select(job: Job, get_gemini, cands: list[dict], band: tuple[int, int], *, title: str, work: str,
           transcript: dict, facts: dict, out_dir: Path, log=print) -> list[dict]:
    key = hashlib.sha1(json.dumps([c["id"] for c in cands] + [title, PROMPT_VERSION], ensure_ascii=False).encode()).hexdigest()[:12]
    cache = out_dir / "select.json"
    if cache.exists():
        doc = json.loads(cache.read_text(encoding="utf-8"))
        if doc.get("key") == key:
            return parse_picks(doc["raw"], len(cands))
    (out_dir / "small").mkdir(exist_ok=True)
    small = [_band_jpg(c["path"], out_dir / "small" / f"{c['id']}.jpg", band) for c in cands]
    prompt = SELECT_PROMPT.format(title=title.replace("\n", " / "), work=work, n=PICK_MAX,
                                  context=context_lines(cands, transcript, facts))
    raw = get_gemini().images_json(prompt, small, kind="thumbnail_select", max_output_tokens=4096)
    cache.write_text(json.dumps({"key": key, "raw": raw}, ensure_ascii=False, indent=1), encoding="utf-8")
    picks = parse_picks(raw, len(cands))
    log(f"[thumb] Flash 선택 {len(picks)}장: " + ", ".join(f"{cands[p['id']]['id']} {p['label']}" for p in picks))
    return picks


# ── 라벨 배치·합성 ─────────────────────────────────────────────────────────
def fit_size(text: str, font_path: Path, *, x0: int, x1: int, size: int = LABEL_SIZE, min_size: int = LABEL_MIN_SIZE) -> int:
    """가운데 정렬 라벨이 x0~x1 안에 들어가는 가장 큰 크기(size 부터 줄인다)."""
    from PIL import ImageFont
    for s in range(size, min_size - 1, -2):
        if ImageFont.truetype(str(font_path), s).getlength(text) <= (x1 - x0):
            return s
    return min_size


def label_y(faces: list[list[int]], band: tuple[int, int], size: int) -> int:
    """라벨 중심 y — 가장 위 얼굴 위쪽 빈자리, 안 되면 가장 아래 얼굴 밑, 그것도 안 되면 밴드 윗부분. 순수."""
    y0, h = band
    top_ok, bot_ok = y0 + LABEL_TOP_CLEAR, y0 + h - size
    if faces:
        above = min(f[1] for f in faces) - LABEL_GAP - size // 2
        if above >= top_ok:
            return int(above)
        below = max(f[1] + f[3] for f in faces) + LABEL_GAP + size // 2
        if below <= bot_ok:
            return int(below)
    return int(top_ok)


def split_layout(parts: list[str], faces: list[list[int]], font_path: Path, *, x0: int, x1: int) -> dict | None:
    """양옆 나눔 라벨 자리 — 가장 큰 얼굴의 좌우에 글자 중심을 둔다. 둘 다 얼굴을 안 가리고 안전 폭 안에 들어가는
    가장 큰 크기(SPLIT_SIZE 부터). 안 되면 None(한 줄로 바꾼다). 순수에 가깝다(글꼴 폭만 잰다)."""
    from PIL import ImageFont
    if len(parts) != 2 or not faces:
        return None
    fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
    for size in range(SPLIT_SIZE, SPLIT_MIN_SIZE - 1, -6):
        font = ImageFont.truetype(str(font_path), size)
        wl, wr = font.getlength(parts[0]), font.getlength(parts[1])
        lx, rx = fx - SPLIT_GAP - wl / 2, fx + fw + SPLIT_GAP + wr / 2
        if lx - wl / 2 >= x0 and rx + wr / 2 <= x1:
            return {"size": size, "y": int(fy + fh * 0.45), "left_x": int(lx), "right_x": int(rx)}
    return None


def compose(frame: dict, pick: dict, out: Path, band: tuple[int, int], fonts_dir: Path, *, x0: int, x1: int) -> dict:
    font = fonts_dir / FONT_FILE
    label = pick.get("label") or ""
    placed = {}
    split = split_layout(pick.get("parts") or [], frame.get("faces") or [], font, x0=x0, x1=x1) if pick.get("style") == "split" else None
    if split:
        color = pick.get("color") or "white"
        l, r = pick["parts"]
        ass = out.with_suffix(".ass")
        ass.write_text(ASS_HEAD.format(font=FONT_NAME, size=split["size"]) + "".join(
            layers(color, "\\an5\\pos(%d,%d)\\fs%d\\frz%d" % (x, split["y"], split["size"], rot), t)
            for x, rot, t in ((split["left_x"], 6, l), (split["right_x"], -6, r))), encoding="utf-8")
        subprocess.run([find_bin("ffmpeg"), "-v", "error", "-y", "-i", frame["path"], "-vf", f"ass='{ass}':fontsdir='{fonts_dir}'",
                        "-frames:v", "1", str(out)], check=True, capture_output=True)
        return {"label": l + " / " + r, "style": "split", "color": pick.get("color"), "y": split["y"], "size": split["size"],
                "x": [split["left_x"], split["right_x"]]}
    if pick.get("style") == "split":
        label = label or "".join(pick.get("parts") or [])   # 옆자리가 모자라면 한 줄로
    if label:
        size = fit_size(label, font, x0=x0 + 20, x1=x1 - 20)
        y = int(pick["y"]) if pick.get("y") else label_y(frame.get("faces") or [], band, size)   # 사람이 정한 자리가 이긴다
        ass = out.with_suffix(".ass")
        ass.write_text(ASS_HEAD.format(font=FONT_NAME, size=size) +
                       layers(pick.get("color") or "white", "\\an5\\pos(540,%d)\\fs%d\\frz-3" % (y, size), label), encoding="utf-8")
        vf = f"ass='{ass}':fontsdir='{fonts_dir}'"
        placed = {"label": label, "style": "line", "color": pick.get("color"), "y": y, "size": size}
    else:
        vf = "null"
    subprocess.run([find_bin("ffmpeg"), "-v", "error", "-y", "-i", frame["path"], "-vf", vf, "-frames:v", "1", str(out)],
                   check=True, capture_output=True)
    return placed


def compare_sheet(files: list[Path], out: Path) -> None:
    ins = sum([["-i", str(f)] for f in files], [])
    fc = "".join(f"[{i}:v]scale=360:640[s{i}];" for i in range(len(files))) + "".join(f"[s{i}]" for i in range(len(files))) \
        + f"hstack=inputs={len(files)}[o]"
    subprocess.run([find_bin("ffmpeg"), "-v", "error", "-y", *ins, "-filter_complex", fc, "-map", "[o]", "-frames:v", "1", str(out)],
                   check=True, capture_output=True)


def contact_sheet(frames: list[dict], out: Path, cols: int = 12) -> None:
    from PIL import Image, ImageDraw
    thumbs = []
    for f in frames:
        im = Image.open(f["path"]).resize((180, 320))
        ImageDraw.Draw(im).text((6, 6), f["id"], fill=(255, 255, 0))
        thumbs.append(im)
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 180, max(rows, 1) * 320))
    for k, im in enumerate(thumbs):
        sheet.paste(im, ((k % cols) * 180, (k // cols) * 320))
    sheet.save(out, quality=80)


# ── 실행 ────────────────────────────────────────────────────────────────────
def run(job_dir: Path, suffix: str, *, use_llm: bool = True, get_gemini=None, log=print) -> dict:
    from app.v3.safe_zone import SAFE_X0, SAFE_X1
    job_dir = Path(job_dir).resolve()
    review = job_dir / f"review_{suffix}"
    video = json.loads((job_dir / "videos" / suffix / "video.json").read_text(encoding="utf-8"))
    source = Path(video["source"]["path"])
    design = ((video.get("provenance") or {}).get("render") or {}).get("design") or {}
    title = (review / "title.txt").read_text(encoding="utf-8-sig").strip() if (review / "title.txt").exists() else (video.get("title") or "")
    timeline = json.loads((review / "edit_plan.json").read_text(encoding="utf-8"))["timeline"]
    chains, brand = parse_filter((review / "final_1080x1920.filter.txt").read_text(encoding="utf-8"))
    out_dir = job_dir / "thumbnails" / suffix
    out_dir.mkdir(parents=True, exist_ok=True)
    job = Job(source, job_dir, video.get("work") or "")
    band = band_rect(design)

    frames = scan_frames(source, timeline, chains, brand, out_dir / "frames", log=log)
    index = job.load("index.json") if job.has("index.json") else {}
    facts = index.get("grid_facts") or {}
    measure(frames, band, {k: int(v.get("importance") or 0) for k, v in facts.items()})
    contact_sheet(frames, out_dir / "sheet.jpg")
    cands = shortlist(frames)
    if not cands:
        raise ValueError("후보 프레임이 없다")

    manual = out_dir / "manual.json"
    by_id = {f["id"]: f for f in frames}
    if manual.exists():                                     # 사람이 고른 목록이 이긴다
        chosen = [(by_id[m["frame"]], m) for m in json.loads(manual.read_text(encoding="utf-8")) if m.get("frame") in by_id]
        how = "manual"
    elif use_llm:
        transcript = job.load("transcript.json") if job.has("transcript.json") else {}
        get_gemini = get_gemini or _default_gemini(job)
        picks = select(job, get_gemini, cands, band, title=title, work=video.get("work") or "", transcript=transcript,
                       facts=facts, out_dir=out_dir, log=log)
        chosen = [(cands[p["id"]], p) for p in picks] or [(c, {"label": ""}) for c in cands[:PICK_MAX]]
        how = "flash" if picks else "score"
    else:
        chosen = [(c, {"label": ""}) for c in cands[:PICK_MAX]]
        how = "score"

    for old in out_dir.glob("thumb_*.png"):
        old.unlink()
    fonts = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    results, files = [], []
    for k, (frame, pick) in enumerate(chosen, 1):
        f = out_dir / f"thumb_{k}.png"
        placed = compose(frame, pick, f, band, fonts, x0=SAFE_X0, x1=SAFE_X1)
        files.append(f)
        results.append({"rank": k, "file": f.name, "frame": frame["id"], "clip": frame["clip"], "clip_time_sec": frame["clip_time_sec"],
                        "source_sec": frame["source_sec"], "score": frame["score"], "person": pick.get("person", ""),
                        "why": pick.get("why", ""), **placed})
    if files:
        compare_sheet(files, out_dir / "compare.png")
    doc = {"schema": "tikitaka_thumbnails/v1", "suffix": suffix, "title": title, "how": how, "band": list(band),
           "picks": results, "candidates": [{k: c[k] for k in ("id", "clip", "clip_time_sec", "source_sec", "score",
                                                                "sharpness", "face_area", "importance")} for c in cands],
           "frames": [{"id": f["id"], "file": f"frames/{f['id']}.jpg", "clip": f["clip"], "clip_time_sec": f["clip_time_sec"],
                       "source_sec": f["source_sec"], "score": f["score"], "faces": f.get("faces") or []} for f in frames],
           "colors": sorted(COLORS), "safe_x": [SAFE_X0, SAFE_X1],
           "color_adjustment": False, "generative_edit": False}
    (out_dir / "thumbnails.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[thumb] {suffix} 썸네일 {len(files)}장 ({how}) → {out_dir}")
    return doc


def _default_gemini(job: Job):
    from app.tikitaka.llm import Gemini
    g = {}

    def get():
        if "g" not in g:
            g["g"] = Gemini(log=job.log)
        return g["g"]
    return get


def main(argv: list[str] | None = None) -> int:
    from app.tikitaka.common import load_dotenv_if_any
    ap = argparse.ArgumentParser(description="렌더가 끝난 편의 썸네일 후보 — 깨끗한 프레임 → Flash 선택·라벨 제안 → 합성")
    ap.add_argument("job_dir", type=Path)
    ap.add_argument("suffix", help="편 (예: v8)")
    ap.add_argument("--no-llm", action="store_true", help="Flash 를 부르지 않고 점수 상위로만")
    a = ap.parse_args(argv)
    load_dotenv_if_any()
    run(a.job_dir, a.suffix, use_llm=not a.no_llm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
