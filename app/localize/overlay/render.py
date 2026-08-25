"""[엔진③-b] 일본어 텍스트 원본 스타일 재합성 (GhostCut 차별 레이어의 나머지 절반).

모드 A(replace): 인페인팅된 배경 위에 일본어를 원본 위치·색·크기로 Pillow 합성.
                 한국어→일본어 폰트는 font_map.yaml 로 매핑.
모드 B(subtitle): ASS(libass)/SRT 자막 트랙 생성(스타일 지정).

폰트 해석·텍스트 줄바꿈·ASS 타임코드·ASS/SRT 빌드는 순수 → 테스트 가능.
프레임 합성(Pillow)만 PIL 사용.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import ensure_dir, get_logger, load_yaml, resolve_path, write_json
from app.localize.overlay.schemas import BBox, DetectionDoc, Style, TranslationDoc

log = get_logger("render")


# ── 줄 단위 스타일·타이밍 오버라이드 계약 (docs/subtitle-style-overrides.md) ──
# ai-video edit_overrides/v3 subtitles[].style 과 같은 의미·범위. 두 경로(SHOTCONE·
# 잔망루피)가 이 검증을 공유한다 — 계약이 갈라지면 편집실 WYSIWYG 이 깨진다.
LINE_STYLE_KEYS = {"size", "y", "color", "rotate", "width"}
# 줄 폭(F-412) 허용 범위·기준 폭 — vlp 23648e0·ai-video SUBTITLE_WIDTH_RANGE 와 1:1.
# width 미지정은 기준 폭(0.852)과 같다는 계약이라 글자 수 환산의 앵커도 이 값이다:
# 기본 16자 × (width/0.852). 편집실 유령(edLayOutVlp 미러)이 같은 식을 쓴다.
LINE_WIDTH_RANGE = (0.3, 1.0)
LINE_WIDTH_BASE = (1080 - 2 * 80) / 1080
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def validate_line_style(style: Any) -> dict[str, Any]:
    """style 오버라이드 검증 → 정규화 사본. 위반은 ValueError(조용한 무시 = 사람 값 증발).

    size: 양수(1080×1920 캔버스 px) · y: 0~1(자막 하단, 하단=1) · color: #RRGGBB ·
    rotate: -180~180(도, **시계방향 양수** — images 와 동일 규약) ·
    width: 0.3~1.0(그 줄이 쓸 가로 폭 비율, F-412 — 글자 크기는 그대로 두고 통만 넓혀
    줄이 접히는 것을 막는다). 모르는 키 즉시 거절."""
    if not isinstance(style, dict):
        raise ValueError(f"style 은 객체여야 합니다: {style!r}")
    unknown = set(style) - LINE_STYLE_KEYS
    if unknown:
        raise ValueError(f"모르는 style 키: {sorted(unknown)} (허용: {sorted(LINE_STYLE_KEYS)})")
    out: dict[str, Any] = {}
    if style.get("size") is not None:
        size = float(style["size"])
        if size <= 0:
            raise ValueError(f"style.size 는 양수(px): {style['size']!r}")
        out["size"] = size
    if style.get("y") is not None:
        y = float(style["y"])
        if not 0.0 <= y <= 1.0:
            raise ValueError(f"style.y 는 0~1 비율: {style['y']!r}")
        out["y"] = y
    if style.get("color") is not None:
        c = str(style["color"])
        if not _COLOR_RE.match(c):
            raise ValueError(f"style.color 는 #RRGGBB: {style['color']!r}")
        out["color"] = c.upper()
    if style.get("rotate") is not None:
        rot = float(style["rotate"])
        if not -180.0 <= rot <= 180.0:
            raise ValueError(f"style.rotate 는 -180~180 도: {style['rotate']!r}")
        out["rotate"] = rot
    if style.get("width") is not None:
        w = float(style["width"])
        if not LINE_WIDTH_RANGE[0] <= w <= LINE_WIDTH_RANGE[1]:
            raise ValueError(
                f"style.width 는 {LINE_WIDTH_RANGE[0]}~{LINE_WIDTH_RANGE[1]} 비율: "
                f"{style['width']!r}")
        out["width"] = w
    return out


def validate_line_timing(item: Any) -> tuple[Optional[float], Optional[float]]:
    """start_sec/end_sec 오버라이드 검증 → (start, end). 없는 키는 None.

    편집본 시간축 초, 0 이상. 둘 다 있으면 end > start."""
    if not isinstance(item, dict):
        raise ValueError(f"오버라이드 항목은 객체여야 합니다: {item!r}")
    vals: list[Optional[float]] = []
    for key in ("start_sec", "end_sec"):
        v = item.get(key)
        if v is None:
            vals.append(None)
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{key} 는 숫자(초): {v!r}")
        if float(v) < 0:
            raise ValueError(f"{key} 는 0 이상: {v!r}")
        vals.append(float(v))
    start, end = vals
    if start is not None and end is not None and end <= start:
        raise ValueError(f"end_sec({end}) 는 start_sec({start}) 보다 커야 합니다")
    return start, end


def hex_to_ass_color(color: str) -> str:
    """#RRGGBB → ASS &HBBGGRR& (BGR)."""
    rr, gg, bb = color[1:3], color[3:5], color[5:7]
    return f"&H{bb}{gg}{rr}&".upper()


def style_ass_tags(style: dict[str, Any], play_res_y: int = 1920) -> str:
    """style → 인라인 ASS 태그(\\fs·\\1c·\\frz). y 는 별도(MarginV/\\pos — style_margin_v).

    size 는 1080×1920 캔버스 px 계약 → PlayResY 가 다르면 비율 환산.
    rotate 는 계약이 시계방향 양수, ASS \\frz 는 반시계 양수 — **부호 반전은 엔진(여기)
    책임**(ai-video subtitle.py 와 동일 규약). 0 은 태그를 안 박는다."""
    tags = []
    if style.get("size") is not None:
        tags.append(f"\\fs{max(1, round(float(style['size']) * play_res_y / 1920))}")
    if style.get("color"):
        tags.append(f"\\1c{hex_to_ass_color(str(style['color']))}")
    if style.get("rotate") is not None and float(style["rotate"]) != 0.0:
        tags.append(f"\\frz{-float(style['rotate']):g}")
    return "".join(tags)


def style_margin_v(style: dict[str, Any], play_res_y: int) -> int:
    """y(자막 하단 비율) → 이벤트 MarginV(px). 미지정 0(=스타일 기본값 사용).

    최소 1 — ASS 규약상 이벤트 MarginV=0 은 '스타일 기본값'이라 y=1(맨 하단)이 증발한다."""
    if style.get("y") is None:
        return 0
    return max(1, round((1.0 - float(style["y"])) * play_res_y))


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def resolve_font(style: Style, font_map: dict[str, Any]) -> str:
    """스타일 → 일본어 폰트 파일명(룰 우선, 없으면 default)."""
    weight = "bold" if style.bold else "regular"
    fam = "serif" if style.serif else "sans"
    for rule in font_map.get("rules", []):
        m = rule.get("match", {})
        if "weight" in m and m["weight"] != weight:
            continue
        if "style" in m and m["style"] != fam:
            continue
        if "size_min" in m and style.font_size < m["size_min"]:
            continue
        if "size_max" in m and style.font_size > m["size_max"]:
            continue
        return rule["jp_font"]
    return font_map.get("default", "NotoSansJP-Bold.ttf")


def width_max_chars(base_chars: int, style: Optional[dict[str, Any]]) -> int:
    """style.width(F-412) → 이 줄의 글자 수 한도. 미지정이면 base 그대로.

    16자(기본)가 곧 통의 폭이다 — 픽셀 여백보다 이 한도가 훨씬 좁아서, 폭을 넓힌다는
    것은 사실상 이 숫자를 늘린다는 뜻이다. 앵커는 LINE_WIDTH_BASE(계약 주석 참고)."""
    w = (style or {}).get("width")
    if w is None:
        return base_chars
    return max(3, round(base_chars * float(w) / LINE_WIDTH_BASE))


def width_margin_lr(style: Optional[dict[str, Any]], play_res_x: int) -> int:
    """style.width → 이벤트 MarginL/R(px, 좌우 대칭). 미지정 0(=스타일 기본값).

    글자 수 한도(width_max_chars)가 1차 관문이고 이 여백은 2차(libass) 안전망이다 —
    폭을 좁힌 줄이 픽셀에서도 좁아지게. 최소 1(이벤트 여백 0 = ASS '스타일 기본값')."""
    w = (style or {}).get("width")
    if w is None:
        return 0
    return max(1, round(play_res_x * (1.0 - float(w)) / 2))


def wrap_text(text: str, max_chars: int) -> list[str]:
    """CJK 줄바꿈: 공백이 없을 수 있으므로 글자 수 기준 단순 래핑.

    **사람이 넣은 줄바꿈(개행)이 있으면 그 경계를 그대로 쓰고 재분할하지 않는다**
    (F-412 — ai-video _lay_out_for_ass·vlp 23648e0 과 같은 규약: 편집실에서 정한
    경계가 정본이다. 종전엔 text.split() 이 개행을 공백과 함께 삼켰다)."""
    text = str(text).replace("\r\n", "\n").strip()
    if not text:
        return []
    manual = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(manual) > 1:
        return manual
    text = manual[0] if manual else ""
    if " " in text:  # 공백 있으면 단어 단위 우선
        words, line, out = text.split(), "", []
        for w in words:
            if len(line) + len(w) + 1 > max_chars and line:
                out.append(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            out.append(line)
        return out
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def ass_timestamp(seconds: float) -> str:
    """초 → ASS 타임코드 H:MM:SS.cs (반올림 캐리를 분/시까지 전파)."""
    cs_total = int(round(max(0.0, seconds) * 100))   # 먼저 센티초로 반올림 후 분해
    h, rem = divmod(cs_total, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _align_code(position: str) -> int:
    """position 버킷 → ASS numpad alignment(1..9)."""
    v, _, h = position.partition("-")
    row = {"bottom": 0, "center": 3, "top": 6}.get(v, 0)
    col = {"left": 1, "center": 2, "right": 3}.get(h, 2)
    return row + col


def build_ass(events: list[dict[str, Any]], width: int, height: int,
              line_max_chars: int = 16, font_name: str = "Noto Sans JP",
              margin_v: Optional[int] = None) -> str:
    """events: [{start,end,text,position}] → ASS 문자열.

    margin_v: 하단 마진 오버라이드 — 원본 한국어 캡션과의 공존 배치(겹침 회피)용.
    이벤트에 style(줄 단위 오버라이드, validate_line_style 통과본)이 실려 오면
    인라인 태그 + 이벤트 MarginV(하단 정렬) 또는 \\pos(그 외)로 그 줄만 얹는다."""
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
         "Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        (f"Style: Default,{font_name},{max(24, height // 18)},&H00FFFFFF,&H00000000,"
         f"&H00000000,1,3,0,2,20,20,{margin_v if margin_v else 30},1"), "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines = list(header)
    for ev in events:
        style_ov = ev.get("style") or {}
        # 폭(F-412): 글자 수 한도가 1차 관문(기본 16자가 곧 통의 폭이다), 이벤트
        # MarginL/R 은 2차(libass) 안전망. 미지정이면 둘 다 종전과 바이트 동일.
        wrapped = wrap_text(ev["text"], width_max_chars(line_max_chars, style_ov))
        if not wrapped:
            continue
        text = "\\N".join(wrapped)
        an = _align_code(ev.get("position", "bottom-center"))
        tags = style_ass_tags(style_ov, height) if style_ov else ""
        ev_margin_v = 0
        ev_margin_lr = width_margin_lr(style_ov, width)
        if style_ov.get("y") is not None:
            if an <= 3:                       # 하단 정렬 → MarginV (v3 와 동일 방식)
                ev_margin_v = style_margin_v(style_ov, height)
            else:                             # 상단/중단 정렬 → \pos 폴백
                tags += f"\\pos({width // 2},{max(1, round(float(style_ov['y']) * height))})"
        lines.append(
            f"Dialogue: 0,{ass_timestamp(ev['start'])},{ass_timestamp(ev['end'])},"
            f"Default,,{ev_margin_lr},{ev_margin_lr},{ev_margin_v},,{{\\an{an}{tags}}}{text}")
    return "\n".join(lines) + "\n"


def build_bilingual_ass(events: list[dict[str, Any]], width: int, height: int,
                        line_max_chars: int = 16, font_name: str = "Noto Sans JP",
                        position: str = "above", gap_px: int = 8) -> str:
    """한국어 자막은 그대로 두고, 그 위/아래에 일본어를 \\pos 로 덧붙이는 ASS.

    position="above": 한국어 bbox 위에(일본어 하단이 bbox 상단-gap), \\an2(하단중앙) 기준.
    position="below": 한국어 bbox 아래에(일본어 상단이 bbox 하단+gap), \\an8(상단중앙) 기준.
    bbox 없는 이벤트는 화면 상/하단 가장자리에 배치.
    이벤트 style(줄 단위 오버라이드): 태그(\\fs·\\1c·\\frz)를 얹고, y 가 있으면
    위/아래 자동 배치 대신 \\an2\\pos(cx, y×height) — 사람이 정한 위치가 이긴다.
    """
    fs = max(20, height // 22)
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
         "Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        f"Style: JP,{font_name},{fs},&H0000FFFF,&H00000000,&H00000000,1,3,0,2,10,10,10,1",
        "",  # 일본어=노란색(&H0000FFFF, BGR)로 한국어와 구분
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines = list(header)
    for ev in events:
        style_ov = ev.get("style") or {}
        wrapped = wrap_text(ev["text"], width_max_chars(line_max_chars, style_ov))
        if not wrapped:
            continue
        text = "\\N".join(wrapped)
        bbox = ev.get("bbox")
        extra = style_ass_tags(style_ov, height) if style_ov else ""
        if style_ov.get("y") is not None:      # 사용자 위치 — 자동 배치보다 우선
            cx = (bbox[0] + bbox[2]) // 2 if bbox else width // 2
            y = max(1, round(float(style_ov["y"]) * height))
            tag = f"{{\\an2\\pos({cx},{y}){extra}}}"
        elif bbox:
            cx = (bbox[0] + bbox[2]) // 2
            if position == "below":
                an, y = 8, min(height - 4, bbox[3] + gap_px)         # 한국어 아래
            else:
                an, y = 2, max(4, bbox[1] - gap_px)                  # 한국어 위
            tag = f"{{\\an{an}\\pos({cx},{y}){extra}}}"
        else:   # bbox 없으면 가장자리
            an = 8 if position == "below" else 2
            tag = f"{{\\an{an}{extra}}}"
        lines.append(
            f"Dialogue: 0,{ass_timestamp(ev['start'])},{ass_timestamp(ev['end'])},"
            f"JP,,0,0,0,,{tag}{text}")
    return "\n".join(lines) + "\n"


def build_srt(events: list[dict[str, Any]], line_max_chars: int = 16) -> str:
    """events → SRT 문자열(srt 라이브러리 있으면 사용, 없으면 수동)."""
    valid = [e for e in events if e.get("text", "").strip()]
    try:
        import datetime
        import srt

        subs = []
        for i, ev in enumerate(valid, 1):
            subs.append(srt.Subtitle(
                index=i,
                start=datetime.timedelta(seconds=ev["start"]),
                end=datetime.timedelta(seconds=ev["end"]),
                content="\n".join(wrap_text(ev["text"], line_max_chars)),
            ))
        return srt.compose(subs)
    except ImportError:
        return _build_srt_manual(valid, line_max_chars)


def _srt_timestamp(seconds: float) -> str:
    """초 → SRT 타임코드 HH:MM:SS,mmm (반올림 캐리를 분/시까지 전파)."""
    ms_total = int(round(max(0.0, seconds) * 1000))   # 먼저 밀리초로 반올림 후 분해
    h, rem = divmod(ms_total, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt_manual(events: list[dict[str, Any]], line_max_chars: int) -> str:
    out = []
    for i, ev in enumerate(events, 1):
        body = "\n".join(wrap_text(ev["text"], line_max_chars))
        out.append(f"{i}\n{_srt_timestamp(ev['start'])} --> {_srt_timestamp(ev['end'])}\n{body}\n")
    return "\n".join(out)


# ── 자막 이벤트 생성 (detect+translate → 시간구간 병합) ──────────────────
def detections_to_events(doc: DetectionDoc, tmap: dict[str, str]) -> list[dict[str, Any]]:
    """샘플 프레임의 텍스트를 같은 내용이 이어지는 시간 구간으로 병합."""
    step_t = doc.sample_every / doc.fps if doc.fps else 0.5
    events: list[dict[str, Any]] = []
    active: dict[str, dict[str, Any]] = {}  # source -> open event
    for fr in doc.frames:
        present = set()
        for r in fr.regions:
            src = r.text.strip()
            if not src or src not in tmap or not tmap[src]:
                continue
            present.add(src)
            if src not in active:
                active[src] = {"start": fr.timestamp, "end": fr.timestamp + step_t,
                               "text": tmap[src], "source": src,   # 오버라이드 좌표 연결용 원문
                               "position": r.style.position,
                               "bbox": r.bbox}    # 한국어 자막 위치(일본어 위/아래 배치용)
            else:
                active[src]["end"] = fr.timestamp + step_t
        for src in list(active):
            if src not in present:
                events.append(active.pop(src))
    events.extend(active.values())
    events.sort(key=lambda e: e["start"])
    return events


def attach_entry_overrides(events: list[dict[str, Any]],
                           entries: list[Any]) -> list[dict[str, Any]]:
    """translations.json entries 의 줄 오버라이드(style·start_sec·end_sec)를 이벤트에 전사.

    이벤트 ↔ 항목은 source 텍스트(detections_to_events 의 tmap 규약과 동일)로 잇고,
    entry_idx(= entries 순번 — 검수 카드·overrides.json subs 의 idx 좌표)를 함께 싣는다.
    같은 source 가 여러 시간 구간(이벤트)으로 나뉘면 style 은 전부에, 타이밍은 **첫
    이벤트에만** 적용(같은 절대 시각으로 겹쳐 쌓이는 것 방지) + 경고 로그. 순수."""
    def _get(e: Any, k: str) -> Any:
        return e.get(k) if isinstance(e, dict) else getattr(e, k, None)

    by_source: dict[str, tuple[int, Any]] = {}
    for i, e in enumerate(entries or []):
        src = (_get(e, "source") or "").strip()
        if src and src not in by_source:
            by_source[src] = (i, e)
    out: list[dict[str, Any]] = []
    timed: dict[int, int] = {}                       # entry_idx → 타이밍 적용 이벤트 수
    for ev in events:
        ev = dict(ev)
        hit = by_source.get((ev.get("source") or "").strip())
        if hit is not None:
            idx, entry = hit
            ev["entry_idx"] = idx
            style = _get(entry, "style")
            if style:
                ev["style"] = dict(style)
            start, end = _get(entry, "start_sec"), _get(entry, "end_sec")
            if start is not None or end is not None:
                n_prev = timed.get(idx, 0)
                if n_prev == 0:
                    if start is not None:
                        ev["start"] = float(start)
                    if end is not None:
                        ev["end"] = float(end)
                        ev["end_fixed"] = True       # 사용자 지정 — 이후 단계가 덮지 않는다
                else:
                    log.warning("타이밍 오버라이드 대상 source 가 %d개 이벤트 — 첫 이벤트에만 "
                                "적용(entry_idx=%d): %r", n_prev + 1, idx, ev.get("text", "")[:20])
                timed[idx] = n_prev + 1
        out.append(ev)
    out.sort(key=lambda e: e["start"])
    return out


def events_json_doc(video_id: str, events: list[dict[str, Any]],
                    cuts: Optional[list[dict[str, float]]] = None) -> dict[str, Any]:
    """ja_events.json 스키마 — 검수(review_meta/ves 어댑터)가 읽는 이벤트 실좌표 노출.

    B/BJ 루트의 자막 타이밍은 detections(0.5s 양자화) 기반이라 translations.json 만으론
    화면 표시 구간을 알 수 없다 → render 가 최종 이벤트를 그대로 떨군다. 순수.
    events[]: {entry_idx(translations entries 순번=오버라이드 좌표, 미매칭 null),
               start, end(초, 편집본 시간축), text(ja), position, bbox([x1,y1,x2,y2]|null),
               style(현재 줄 스타일, 없으면 null), end_fixed(사용자 지정 타이밍 여부)}
    (E9) cuts 가 적용된 렌더면 그 목록을 동봉 — start/end 는 이미 당겨진(컷 후)
    시간축이고, 검수자가 '왜 짧아졌는지' 안다."""
    doc = {"video_id": video_id, "coord": "translations.json entries 순번(entry_idx)",
           "events": [{"entry_idx": ev.get("entry_idx"),
                       "start": ev["start"], "end": ev["end"], "text": ev["text"],
                       "position": ev.get("position"),
                       "bbox": list(ev["bbox"]) if ev.get("bbox") else None,
                       "style": ev.get("style") or None,
                       "end_fixed": bool(ev.get("end_fixed"))}
                      for ev in events]}
    if cuts:
        doc["cuts"] = list(cuts)
    return doc


# ── 모드 A: Pillow 합성 ──────────────────────────────────────────────────
def render_replace(inpainted_dir: str, doc: DetectionDoc, tmap: dict[str, str],
                   config: dict[str, Any], out_dir: str, font_map: dict[str, Any]) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    rcfg = config.get("render", {})
    fonts_dir = resolve_path(config["paths"]["fonts_dir"])
    stroke = int(rcfg.get("stroke_width", 3))
    out = ensure_dir(out_dir)
    step = doc.sample_every
    by_key = {f.frame_idx: f for f in doc.frames}

    def _font(style: Style):
        fp = fonts_dir / resolve_font(style, font_map)
        try:
            return ImageFont.truetype(str(fp), size=max(12, style.font_size))
        except Exception:
            log.warning("폰트 로드 실패(%s) → 기본 폰트. font_map/fonts_dir 확인", fp)
            return ImageFont.load_default()

    frames = sorted(Path(inpainted_dir).glob("*.png"))
    for fp in frames:
        idx = int(fp.stem)
        fd = by_key.get((idx // step) * step)
        img = Image.open(fp).convert("RGB")
        if fd:
            draw = ImageDraw.Draw(img)
            for r in fd.regions:
                jp = tmap.get(r.text.strip(), "")
                if not jp:
                    continue
                lines = wrap_text(jp, int(rcfg.get("line_max_chars", 16)))
                font = _font(r.style)
                bbox_w = r.bbox[2] - r.bbox[0]
                y = r.bbox[1]
                for ln in lines:
                    lw = draw.textlength(ln, font=font)          # bbox 폭 기준 가로 중앙 정렬
                    lx = r.bbox[0] + max(0, (bbox_w - lw) / 2)
                    draw.text((lx, y), ln, font=font, fill=r.style.color,
                              stroke_width=stroke, stroke_fill=r.style.stroke_color)
                    y += int(r.style.font_size * 1.1)
        img.save(out / fp.name)
    log.info("재렌더(replace) %d 프레임 → %s", len(frames), out)
    return out


# ── 자가개선: 렌더 OCR 백체크 — 번인된 일본어를 되읽어 잘림/폰트깨짐 검출(2026-07-21) ──
def match_cer(expected: str, ocr_texts: list[str]) -> float:
    """기대 텍스트 vs OCR 결과 최소 CER(순수). 후보 = 각 결과 + 전체 연결(줄분리 흡수)."""
    from app.localize.overlay.common import cer, norm_text
    exp = norm_text(expected)
    if not exp:
        return 0.0
    cands = [norm_text(t) for t in ocr_texts if t] + [norm_text("".join(ocr_texts))]
    return min((cer(exp, c) for c in cands), default=1.0)


def pick_backcheck_frames(frame_indices: list[int], limit: int) -> list[int]:
    """검사 프레임 샘플(순수) — 균등 간격으로 최대 limit 개(비용 가드)."""
    if limit <= 0 or not frame_indices:
        return []
    if len(frame_indices) <= limit:
        return list(frame_indices)
    step = len(frame_indices) / limit
    return [frame_indices[int(i * step)] for i in range(limit)]


def render_backcheck(rendered_dir: str, doc: DetectionDoc, tmap: dict[str, str],
                     config: dict[str, Any]) -> dict[str, Any]:
    """번인 프레임 샘플을 일본어 OCR 로 되읽어 기대 텍스트와 대조 → 요약 dict.

    OCR 은 PP-OCRv5 기본 rec(일본어 지원) — detect 의 한국어 rec 과 별도 인스턴스.
    실패(모델 부재 등)는 요약에 error 로 기록하고 예외를 올리지 않는다(렌더는 유효)."""
    import cv2

    from app.localize.overlay.detect import make_ocr
    bc = config.get("render", {}).get("backcheck", {})
    max_cer = float(bc.get("max_cer", 0.3))
    with_text = [(f.frame_idx, [tmap.get(r.text.strip(), "") for r in f.regions
                                if tmap.get(r.text.strip(), "")])
                 for f in doc.frames]
    with_text = [(i, texts) for i, texts in with_text if texts]
    picked = set(pick_backcheck_frames([i for i, _ in with_text],
                                       int(bc.get("frames", 6))))
    if not picked:
        return {"checked": 0, "matched": 0, "cer_avg": 0.0, "failed": 0}
    try:
        dcfg = config.get("detect", {})
        ocr = make_ocr("paddleocr", languages=["japan"],
                       paddle_opts={"det_model": dcfg.get("paddle_det_model"),
                                    "rec_model": bc.get("rec_model", "PP-OCRv5_mobile_rec")})
    except Exception as e:                            # noqa: BLE001 — 백체크 불가 ≠ 렌더 실패
        log.warning("렌더 백체크 OCR 초기화 실패(생략): %s", e)
        return {"checked": 0, "matched": 0, "cer_avg": 0.0, "failed": 0, "error": str(e)[:200]}
    cers: list[float] = []
    for idx, texts in with_text:
        if idx not in picked:
            continue
        fp = Path(rendered_dir) / f"{idx:06d}.png"
        if not fp.exists():                           # 프레임 파일명 규칙 불일치 등
            continue
        frame = cv2.imread(str(fp))
        if frame is None:
            continue
        ocr_texts = [t for _, t, _ in ocr.recognize(frame)]
        for expected in texts:
            cers.append(match_cer(expected, ocr_texts))
    if not cers:
        return {"checked": 0, "matched": 0, "cer_avg": 0.0, "failed": 0}
    failed = sum(1 for c in cers if c > max_cer)
    summary = {"checked": len(cers), "matched": len(cers) - failed,
               "cer_avg": round(sum(cers) / len(cers), 4), "failed": failed}
    log.info("렌더 백체크: %d건 검사, 일치 %d, CER avg %.3f",
             summary["checked"], summary["matched"], summary["cer_avg"])
    return summary


# ── 오케스트레이션 ───────────────────────────────────────────────────────
def render(doc_path: str, translations_path: str, config: dict[str, Any],
           mode: Optional[str] = None, inpainted_dir: Optional[str] = None,
           out_dir: Optional[str] = None,
           cuts: Optional[list[dict[str, float]]] = None) -> dict[str, str]:
    doc = DetectionDoc.load(doc_path)
    tdoc = TranslationDoc.load(translations_path)
    tmap = tdoc.as_map()
    font_map = load_yaml(resolve_path(config["paths"]["font_map"]))
    mode = mode or config.get("render", {}).get("default_mode", "subtitle")
    base = Path(out_dir) if out_dir else resolve_path(
        f"{config['paths']['outputs_dir']}/{doc.video_id}")
    ensure_dir(base)
    line_max = int(config.get("render", {}).get("line_max_chars", 16))

    # 자막 트랙은 항상 생성(검수·접근성)
    events = detections_to_events(doc, tmap)
    # 줄 단위 스타일·타이밍 오버라이드(검수 반려 수정) — entries 에 실려 온 값을 이벤트로.
    events = attach_entry_overrides(events, tdoc.entries)
    # 구간 잘라내기(E9) — 오버라이드 병합 **뒤**(사용자 타이밍도 당김 대상), ass/srt·
    # ja_events 기록 **전**. 완전히 컷 안인 줄은 use:false 와 동일 의미로 제외되고,
    # 걸친 줄은 경계 클램프. 영상 자체의 컷은 재조립(_reassemble)이 같은 cuts 로 한다.
    if cuts:
        from app.localize.overlay.cuts import apply_cuts_to_events
        events, n_cut_del = apply_cuts_to_events(events, cuts)
        events = [e for e in events if e.get("use") is not False]
        log.info("구간 잘라내기(E9): 컷 %d개 — 완전 포함 %d줄 제외, 이후 시각 당김",
                 len(cuts), n_cut_del)
    # 이벤트 실좌표 노출(검수용) — B/BJ 타이밍은 detections(0.5s 양자화) 기반이라
    # 여기서 떨궈야 review_meta(ves 어댑터)가 표시 구간·현재 스타일을 안다.
    events_path = base / "ja_events.json"
    write_json(events_json_doc(doc.video_id, events, cuts=cuts), events_path)
    ass_path = base / "ja.ass"
    srt_path = base / "ja.srt"
    ass_path.write_text(build_ass(events, doc.width, doc.height, line_max), encoding="utf-8")
    srt_path.write_text(build_srt(events, line_max), encoding="utf-8")
    result = {"ass": str(ass_path), "srt": str(srt_path), "events": str(events_path)}

    if mode == "replace":
        if not inpainted_dir:
            raise ValueError("replace 모드는 --inpainted (인페인팅된 프레임) 필요")
        result["frames"] = str(render_replace(inpainted_dir, doc, tmap, config,
                                              str(base / "rendered"), font_map))
        if config.get("render", {}).get("backcheck", {}).get("enabled", False):
            # 자가개선: 번인 결과 OCR 대조 → QA 게이트
            write_json(render_backcheck(result["frames"], doc, tmap, config),
                       base / "render_backcheck.json")
    elif mode == "bilingual":
        # 한국어 자막 유지 + 그 위/아래에 일본어 추가(인페인팅 없음). 원본 영상에 번인.
        pos = config.get("render", {}).get("overlay_position", "above")
        bi = base / "ja_bilingual.ass"
        bi.write_text(build_bilingual_ass(events, doc.width, doc.height, line_max, position=pos),
                      encoding="utf-8")
        result["bilingual_ass"] = str(bi)
    log.info("렌더 완료(초벌, 검수 전) mode=%s → %s", mode, result)
    return result


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="일본어 재합성/자막 생성")
    p.add_argument("--detections", required=True)
    p.add_argument("--translations", required=True)
    p.add_argument("--config", default=None)
    p.add_argument("--mode", default=None, help="replace|subtitle")
    p.add_argument("--inpainted", default=None, help="replace 모드: 인페인팅된 프레임 디렉토리")
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    from app.localize.overlay.common import load_config

    args = _parse_args(argv)
    config = load_config(args.config)
    render(args.detections, args.translations, config, mode=args.mode,
           inpainted_dir=args.inpainted, out_dir=args.out)


if __name__ == "__main__":
    main()
