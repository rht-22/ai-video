"""M4 후반부 — 최종 렌더(13) + validate 확장(14) + §9-D 미시문법 검사.

렌더는 기존 모듈 재사용이 원칙(발주서 §C): `renderer.render_short` 가 v1 과 같은
화면 문법(상단 제목 2줄·하단 작품명·자막 밴드·TTS 믹스·덕킹)을 그대로 그린다 —
v3 는 **입력 어댑터만** 짓는다. style.json 의 design 어휘(어댑터 design-* 1:1)를
DesignConfig 로 매핑하고, 어절 자막(C6)·TTS cue(C2)·괄호 라벨을 ASS 3종으로 바꾼다.

validate 확장(경고 모드 — 기획: 차단하지 않는다):
  신규 4종 ① 컷 경계=grid span 경계 100%(벨트 재사용) ② TTS-importance≥4 겹침 0
  ③ exception 구간 유입 0 ④ 프레임 비전 QC(자막 잘림·겹침 — Flash, 경고만).
  §9-D ① 진행감 — 화면 변화 이벤트(컷·자막 등장·cue 시작) 간격 > 3s 경고
       ② 루프 정합 — 첫/끝 프레임 시각 거리(평균 절대 오차) 기록 + 큰 단절 경고
       (서론 금지는 story 프롬프트 규칙 — 편성 시점 방어).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from app.config import AppConfig, DesignConfig
from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.renderer import RenderInputs, render_short
from app.modules.speech import SpeechSegment
from app.modules.story_builder import StoryClip
from app.modules.subtitle import (
    SubtitleStyle,
    _hex_to_ass_color,
    build_ass_from_segments,
    build_texts_ass,
    build_tts_ass,
)
from app.v3 import assemble, schemas

PROGRESSION_MAX_GAP_SEC = 3.0     # §9-D 진행감 — 2~3초마다 화면 변화
LOOP_DIFF_WARN = 60.0             # 첫/끝 프레임 평균 절대 오차(0~255) 경고 임계
LABEL_Y_RATIO = 0.526             # 괄호 라벨 세로 위치 — 템플릿 1010/1920 실측
LABEL_MAX_SEC = 4.0               # 라벨 표시 상한 — 레퍼런스 실측 3.54~4.00s
QC_FRAME_COUNT = 4


def _style_color(hex_color: str) -> str:
    """hex → ASS Style 블록 표기(&H00BBGGRR). 인라인 태그(&H..&)와 혼동 금지."""
    return f"&H00{_hex_to_ass_color(hex_color).strip('&H&')}"


# ── design 어휘 → DesignConfig 매핑 ─────────────────────────────────────────

def design_from_style(design: dict) -> DesignConfig:
    """style.json design(어댑터 design-* 어휘) → 렌더러 DesignConfig. 순수.

    DesignConfig 는 frozen — dataclasses.replace 로 짓는다. tts 색은 DesignConfig
    가 ASS 표기라 hex → ASS 변환을 여기서 책임진다."""
    import dataclasses
    base = DesignConfig()
    up: dict[str, Any] = {}
    if "aspect_ratio" in design:
        up["aspect_ratio"] = str(design["aspect_ratio"])
    up["title_colors"] = [str(design.get("title_color", base.title_colors[0])),
                          str(design.get("title_color2", base.title_colors[1]))]
    up["title_sizes"] = [int(design.get("title_size", base.title_sizes[0])),
                         int(design.get("title_size2", base.title_sizes[1]))]
    up["title_bolds"] = [bool(design.get("title_bold", False)),
                         bool(design.get("title_bold2", False))]
    if design.get("subtitle_color"):
        up["subtitle_color"] = str(design["subtitle_color"])
    if design.get("subtitle_size"):
        up["subtitle_size"] = int(design["subtitle_size"])
    if design.get("subtitle_y_margin"):
        up["subtitle_y_margin"] = int(design["subtitle_y_margin"])
    if design.get("tts_color"):
        # _hex_to_ass_color 는 인라인 태그용(&HBBGGRR&) — DesignConfig.tts_line_color
        # 는 Style 블록 표기(&H00BBGGRR)라 알파 포함으로 재조립한다
        up["tts_line_color"] = _style_color(str(design["tts_color"]))
    if design.get("tts_size"):
        up["tts_line_font_size"] = int(design["tts_size"])
    if design.get("tts_y_margin"):
        up["tts_line_y_margin"] = int(design["tts_y_margin"])
    if design.get("work_color"):
        up["work_color"] = str(design["work_color"])
    return dataclasses.replace(base, **up)


# ── 최종 렌더 어댑터 ────────────────────────────────────────────────────────

def render_final(*, video_path: Path, plan: dict, style_doc: dict,
                 segments: list[dict], resources: dict, story_doc: dict,
                 output_dir: Path, out_name: str = "final_1080x1920.mp4",
                 log=print) -> tuple[Path, dict]:
    """edit_plan + style + 자막/cue → 1080×1920 최종본. 반환: (경로, 실측)."""
    config = AppConfig()
    design = design_from_style(style_doc.get("design") or {})
    # 폰트 이원화(M4 스모크 프레임 실측 2건의 교훈):
    #   drawtext(제목·작품명) = **파일 경로** — 이름만 주면 fontconfig 폴백으로 한글
    #     글리프가 없어 두부(□)가 된다.
    #   ASS Style(자막·TTS) = **패밀리명** — 경로를 Fontname 에 넣으면 fontsdir 매칭이
    #     실패해 시스템 폴백에 의존한다(맥은 우연히 되지만 프로드 노드에선 두부).
    import dataclasses as _dc

    import app.config as _cfgmod
    from app.config import get_font_path, to_font_family
    _root = Path(_cfgmod.__file__).resolve().parent   # v1 pipeline 과 같은 app_root
    ass_family = to_font_family(design.subtitle_font)
    design = _dc.replace(
        design,
        title_font=get_font_path(design.title_font, _root),
        subtitle_font=get_font_path(design.subtitle_font, _root))

    clips = [StoryClip(role=str(c.get("role") or "build"),
                       start_sec=float(c["clip_start_sec"]),
                       end_sec=float(c["clip_end_sec"]),
                       subtitle=str(c.get("subtitle") or ""),
                       use_original_audio=bool(c.get("use_original_audio", True)))
             for c in plan["timeline"]]

    # 대사 ASS — C6 세그먼트(편집본 좌표)를 그대로 이벤트로
    sub_path = output_dir / "v3_subtitles.ass"
    sub_style = SubtitleStyle(
        font_name=ass_family, font_size=design.subtitle_size,
        primary_color=_style_color(design.subtitle_color or "#FFFFFF"),
        outline=3, margin_v=design.subtitle_y_margin)
    build_ass_from_segments(
        [SpeechSegment(start_sec=float(s["start_sec"]), end_sec=float(s["end_sec"]),
                       text=str(s["text"])) for s in segments],
        sub_path, sub_style)

    # TTS 자막 ASS — cue 텍스트(합성 fit 반영본)
    cue_files = [f for f in (resources.get("tts_cue_files") or [])
                 if f.get("cue", {}).get("start_sec") is not None
                 and Path(f.get("path", "")).exists()]
    tts_path = None
    if cue_files:
        tts_path = output_dir / "v3_tts.ass"
        tts_style = SubtitleStyle(
            font_name=ass_family, font_size=design.tts_line_font_size,
            primary_color=design.tts_line_color, outline=3,
            margin_v=design.tts_line_y_margin)
        build_tts_ass(
            [SpeechSegment(start_sec=float(f["cue"]["start_sec"]),
                           end_sec=float(f["cue"]["end_sec"]),
                           text=str(f["cue"]["text"])) for f in cue_files],
            tts_path, tts_style)

    # 괄호 라벨 — 편집실 자유 텍스트 레이어 재사용(비트 창 전체에 표시)
    labels = []
    offsets = assemble.edited_offsets(plan["timeline"])
    # M11-B: 라벨은 **앵커 span 시각**에 뜬다(레퍼런스는 대사 순간에 붙는다 —
    # 비트 시작 고정이면 긴 비트에서 앞부분에만 잠깐 떴다). 앵커 span 의 소스
    # 시각을 편집본으로 옮겨 배치한다.
    span_t = {}
    for c in plan["timeline"]:
        for sid in c.get("span_ids") or []:
            span_t.setdefault(sid, float(c["clip_start_sec"]))
    for b in story_doc.get("beats") or []:
        items = b.get("labels") or ([{"text": b["label"],
                                      "span_id": (b.get("span_ids") or [None])[0]}]
                                    if b.get("label") else [])
        for lb in items:
            src = span_t.get(lb.get("span_id"))
            if src is None:
                src = schemas.parse_ts(b["time"]["start"])
            s0 = assemble.to_edited_sec(src, offsets, kind="start")
            s1 = assemble.to_edited_sec(schemas.parse_ts(b["time"]["end"]), offsets,
                                        kind="end")
            if s0 is None or s1 is None or s1 <= s0:  # 역전 = 음수 길이 ASS(리뷰 확정)
                continue
            labels.append({"text": lb["text"], "start_sec": s0,
                           "end_sec": min(s1, s0 + LABEL_MAX_SEC),
                           "x": 0.5, "y": LABEL_Y_RATIO, "size": 58,
                           "color": "#FF4A3B", "stroke": "dark"})
    texts_path = None
    if labels:
        texts_path = output_dir / "v3_labels.ass"
        build_texts_ass(labels, texts_path)

    # 적대 리뷰 확정(critical): renderer 는 use_original_audio 를 읽지 않았다 —
    # 뮤트 창(편집본 좌표)을 additive 필드로 넘겨 원본 트랙에만 volume=0 (cue 는 산다).
    muted_windows: list[tuple[float, float]] = []
    _off = 0.0
    for c in clips:
        _dur = c.end_sec - c.start_sec
        if not c.use_original_audio:
            muted_windows.append((round(_off, 3), round(_off + _dur, 3)))
        _off += _dur

    out_path = output_dir / out_name
    audio_mix = plan.get("audio_mix") or {}
    inputs = RenderInputs(
        video_path=Path(video_path),
        clips=clips,
        subtitle_path=sub_path,
        crop_timeline_map={},                    # 얼굴 크롭은 범위 외(발주서)
        title_text=(plan.get("layout") or {}).get("top_title") or "",
        work_title=(plan.get("layout") or {}).get("bottom_label") or "",
        output_path=out_path,
        canvas_width=config.canvas_width, canvas_height=config.canvas_height,
        top_title_height=config.top_title_height,
        bottom_label_height=config.bottom_label_height,
        design=design,
        tts_subtitle_path=tts_path,
        tts_cue_files=cue_files or None,
        original_audio_gain_db=int(audio_mix.get("original_gain_db", -3)),
        tts_audio_gain_db=int(audio_mix.get("tts_gain_db", -3)),
        text_subtitle_path=texts_path,
        muted_windows=muted_windows or None,
    )
    t0 = time.time()
    render_short(inputs)
    cost = {"elapsed": round(time.time() - t0, 1), "bytes": out_path.stat().st_size,
            "clips": len(clips), "cues": len(cue_files),
            "muted_windows": len(inputs.muted_windows or []), "labels": len(labels)}
    log(f"  [v3/render] {out_path.name} — {cost['elapsed']}s · "
        f"{cost['bytes'] // (1024 * 1024)}MB")
    return out_path, cost


# ── validate 확장 (14) — 전부 순수 계산 + 프레임 실측 ──────────────────────

def check_exception_overlap(timeline: list[dict], stage1_doc: dict) -> dict:
    """신규 ③ — 최종 컷이 exception 구간과 겹치면 유입(0 이어야 한다)."""
    zones = []
    for key, zone in (stage1_doc.get("exception_sector") or {}).items():
        if isinstance(zone, dict) and zone.get("start") and zone.get("end"):
            zones.append((key, schemas.parse_ts(zone["start"]),
                          schemas.parse_ts(zone["end"])))
    hits = []
    for c in timeline:
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        for key, z0, z1 in zones:
            if min(e, z1) - max(s, z0) > 0.01:
                hits.append({"zone": key, "clip": [s, e],
                             "overlap_sec": round(min(e, z1) - max(s, z0), 3)})
    return {"zones": len(zones), "violations": hits}


def check_tts_conflicts(resources: dict, plan: dict, stage2_doc: dict,
                        grid: dict) -> dict:
    """신규 ② — cue 창(소스 좌표 신원)이 뮤트 안 된 importance≥4 유성 span 과 겹침 0."""
    from app.v3.story import MUTE_MAX_IMPORTANCE, build_span_index
    span_index, _ = build_span_index(stage2_doc, grid)
    selected = [sid for c in plan.get("timeline") or []
                for sid in c.get("span_ids") or []]
    violations = []
    for f in resources.get("tts_cue_files") or []:
        cue = f.get("cue") or {}
        if cue.get("start_sec") is None:
            continue
        c0 = float(cue["source_time_sec"])
        # 계획 창이 아니라 **실측 오디오 길이**가 실제 겹침이다(적대 리뷰 —
        # fit 소진 '잘림 감수' 오디오가 창을 넘어 다음 대사를 밟는 재현)
        c1 = c0 + max(float(cue.get("duration_sec") or 0),
                      float(cue.get("fit_actual_sec") or 0))
        muted = set(cue.get("muted_span_ids") or [])
        for sid in selected:
            sp = span_index.get(sid)
            if not sp or not sp["is_audio"] or sid in muted \
                    or sp["importance"] <= MUTE_MAX_IMPORTANCE:
                continue
            if min(c1, sp["t_out"]) - max(c0, sp["t_in"]) > 0.01:
                violations.append({"cue_beat": cue.get("beat"), "span": sid,
                                   "importance": sp["importance"]})
    return {"checked_cues": len(resources.get("tts_cue_files") or []),
            "violations": violations}


def check_progression(timeline: list[dict], segments: list[dict],
                      resources: dict) -> dict:
    """§9-D ① 진행감 — 화면 변화 이벤트(컷·자막 등장·cue 시작) 간 최대 간격.

    3s 를 넘는 창은 경고로 나열한다 — silent_break 비트는 의도된 호흡이라
    role 표기를 함께 실어 사람이 가려 읽게 한다(차단 아님)."""
    events = [0.0]
    role_at: list[tuple[float, float, str]] = []
    off = 0.0
    for c in timeline:
        dur = float(c["clip_end_sec"]) - float(c["clip_start_sec"])
        role_at.append((off, off + dur, str(c.get("role") or "")))
        events.append(off)
        off += dur
    total = off
    events += [float(s["start_sec"]) for s in segments]
    for f in resources.get("tts_cue_files") or []:
        cue = f.get("cue") or {}
        if cue.get("start_sec") is not None:
            events.append(float(cue["start_sec"]))
    events = sorted({round(min(max(e, 0.0), total), 3) for e in events}) + [total]
    warnings = []
    for a, b in zip(events, events[1:]):
        if b - a > PROGRESSION_MAX_GAP_SEC:
            role = next((r for s, e, r in role_at if s <= a < e), "")
            warnings.append({"window": [round(a, 2), round(b, 2)],
                            "gap_sec": round(b - a, 2), "role": role})
    return {"events": len(events), "max_gap_allowed": PROGRESSION_MAX_GAP_SEC,
            "warnings": warnings}


def check_loop_continuity(video_path: Path, tmp_dir: Path) -> dict:
    """§9-D ② 루프 정합 — 첫/끝 프레임의 평균 절대 오차(0~255). 경고 모드."""
    ffmpeg = find_ffmpeg_command("ffmpeg")
    first, last = tmp_dir / "loop_first.png", tmp_dir / "loop_last.png"
    try:
        subprocess.run([ffmpeg, "-y", "-i", str(video_path), "-frames:v", "1",
                        str(first)], check=True, capture_output=True)
        subprocess.run([ffmpeg, "-y", "-sseof", "-0.5", "-i", str(video_path),
                        "-frames:v", "1", str(last)], check=True, capture_output=True)
        from PIL import Image
        import numpy as np
        a = np.asarray(Image.open(first).convert("L").resize((90, 160)), dtype=float)
        b = np.asarray(Image.open(last).convert("L").resize((90, 160)), dtype=float)
        diff = float(abs(a - b).mean())
    except Exception as e:  # noqa: BLE001 — 측정 실패는 커버리지 표기(경고 모드)
        return {"status": "skipped", "reason": f"{type(e).__name__}: {e}"}
    return {"status": "ok", "mean_abs_diff": round(diff, 1),
            "warning": diff > LOOP_DIFF_WARN}


QC_PROMPT = """첨부한 프레임들은 세로 쇼츠(1080×1920) 최종본의 샘플이다. 화면 사고만 찾아라 — 취향 평가 금지.
검사 항목: ① 자막/제목이 화면 밖으로 잘림 ② 텍스트 레이어끼리 겹침 ③ 검정 밴드 침범(영상이 제목/로고 밴드를 덮음) ④ 빈 화면(검정/단색) ⑤ 글자 깨짐 — □(두부)·물음표 연속 등 글리프 누락.
출력(JSON만): {"issues": [{"frame": 0, "kind": "clip|overlap|band|blank|glyph", "note": "한 줄"}]} — 문제없으면 빈 배열."""


def frame_vision_qc(gemini, video_path: Path, tmp_dir: Path, *,
                    n_frames: int = QC_FRAME_COUNT, log=print) -> dict:
    """신규 ④ — 최종본 샘플 프레임 Flash 검사(경고 모드 — 차단 아님)."""
    ffmpeg = find_ffmpeg_command("ffmpeg")
    ffprobe = find_ffmpeg_command("ffprobe")
    out = subprocess.run([ffprobe, "-v", "quiet", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(video_path)],
                         capture_output=True, text=True)
    try:
        dur = float(out.stdout.strip())
    except ValueError:
        return {"status": "skipped", "reason": "duration 측정 실패"}
    frames = []
    try:
        for i in range(n_frames):
            t = dur * (i + 0.5) / n_frames
            p = tmp_dir / f"qc_{i}.jpg"
            subprocess.run([ffmpeg, "-y", "-ss", f"{t:.2f}", "-i", str(video_path),
                            "-frames:v", "1", "-q:v", "5", str(p)],
                           check=True, capture_output=True)
            frames.append(p)
    except subprocess.CalledProcessError as e:
        # 경고 모드 — 추출 실패가 파이프라인을 죽이면 안 된다
        return {"status": "skipped",
                "reason": f"프레임 추출 실패: {(e.stderr or b'')[-200:]!r}"}
    types = gemini.types
    parts = [types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg")
             for p in frames]
    parts.append(QC_PROMPT)
    try:
        resp = gemini.client.models.generate_content(
            model=gemini.config.flash_model_name, contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.0, response_mime_type="application/json",
                max_output_tokens=2048))
        from app.modules.gemini_client import _extract_json_from_markdown
        data = json.loads(_extract_json_from_markdown(resp.text or ""))
        issues = data.get("issues") if isinstance(data, dict) else None
        issues = issues if isinstance(issues, list) else []
    except Exception as e:  # noqa: BLE001 — QC 실패가 발행을 막지 않는다(경고 모드)
        return {"status": "skipped", "reason": f"{type(e).__name__}: {e}"}
    if issues:
        log(f"  [v3/validate] ⚠ 프레임 QC 경고 {len(issues)}건")
    return {"status": "ok", "frames": n_frames, "issues": issues}


def run_validate(*, plan: dict, grid: dict, stage1_doc: dict, stage2_doc: dict,
                 segments: list[dict], resources: dict,
                 final_path: Path | None, tmp_dir: Path,
                 cast_names: list[str] | None = None,
                 gemini=None, log=print) -> dict:
    """validate 확장 — 수치 4종 + §9-D. 경고는 차단하지 않는다(기획: 경고 모드)."""
    doc: dict[str, Any] = {"schema": "v3_validate/v1"}
    doc["snap_belt"] = assemble.verify_edit_plan(plan, grid)                    # ①
    doc["tts_conflicts"] = check_tts_conflicts(resources, plan, stage2_doc, grid)  # ②
    doc["exception_ingress"] = check_exception_overlap(plan.get("timeline") or [],
                                                       stage1_doc)             # ③
    doc["progression"] = check_progression(plan.get("timeline") or [],
                                           segments, resources)                # §9-D ①
    # M9-A/B 계기판 — 조립 시점의 예방을 통과한 뒤에도 남은 게 있는지(이중 방어)
    from app.v3 import textcheck
    doc["subtitle_text"] = {
        "repetition": textcheck.check_repetition(segments),
        "name_suspects": textcheck.check_names(segments, cast_names or [])}
    if final_path is not None and final_path.exists():
        doc["loop_continuity"] = check_loop_continuity(final_path, tmp_dir)    # §9-D ②
        if gemini is not None:
            doc["frame_qc"] = frame_vision_qc(gemini, final_path, tmp_dir, log=log)  # ④
        else:
            doc["frame_qc"] = {"status": "skipped", "reason": "gemini 미제공"}
    hard_fail = (
        (doc["snap_belt"]["pct"] is not None and doc["snap_belt"]["pct"] < 100.0)
        or doc["tts_conflicts"]["violations"]
        or doc["exception_ingress"]["violations"])
    doc["hard_fail"] = bool(hard_fail)
    doc["warnings_total"] = (
        len(doc["subtitle_text"]["repetition"])
        + len(doc["subtitle_text"]["name_suspects"])
        + len(doc["progression"]["warnings"])
        + (1 if doc.get("loop_continuity", {}).get("warning") else 0)
        + len(doc.get("frame_qc", {}).get("issues") or []))
    return doc
