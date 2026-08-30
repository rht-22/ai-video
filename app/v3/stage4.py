"""M4 전반부 — draft_render(11) + Stage 4 style(12).

2-pass 의 1패스: edit_plan 컷만 이어붙인 480p 중립 캔버스(디자인 미적용 — 납품물
아님, 스타일 분석 재료)를 만들고, 비트 경계 프레임을 뽑아 Flash vision 1회가
style.json 을 구성한다. **시각 비접촉 구간 유지** — Stage 4 는 프레임 샘플만 보고,
시각은 여전히 span ID lookup 이다.

style.json 계약(C5 — design 어휘 동결):
  `design` 키는 STYLE_ALLOWED 화이트리스트(어댑터 design-* 어휘와 1:1)만, 범위 검증.
  어휘 밖 키·범위 밖 값은 반려·재질의 ≤2회 → 소진 시 **프리셋 그대로**(스타일
  무변경 폴백 — 렌더는 항상 가능). 프리셋 대비 diff 를 기록한다.
  비트별 연출(팝 강도·효과음 큐·크롭 앵커)은 additive `v3_style` 에만 — 효과음은
  큐 정의까지(자산 소싱 별도 트랙), 얼굴 크롭 타임라인은 범위 외(클러스터 순도
  개선 전 — M2 검증 보고). §9-D 서론 금지는 story 프롬프트가, 진행감·루프 정합
  검사는 validate(finalize)가 맡는다.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.gemini_client import _extract_json_from_markdown, _loads_first_json
from app.v3.seq_analyze import MAX_REASKS

DRAFT_HEIGHT = 480

# 채널 프리셋 초기값 — templates/recap_shorts/template.json 실측(프리미어 수동 제작).
# 키는 어댑터 design-* 어휘와 1:1(ves CHANNEL_DESIGN_FLAGS) — C5 동결의 이행.
RECAP_PRESET: dict[str, Any] = {
    "title_color": "#FFE94A",       # 1줄 = 상황(노랑)
    "title_color2": "#FF3B2D",      # 2줄 = 펀치(빨강)
    "title_size": 88,
    "title_size2": 92,
    "title_bold": True,
    "title_bold2": True,
    "subtitle_color": "#FFFFFF",    # 대사 = 흰색
    "subtitle_size": 62,
    "tts_color": "#FFE94A",         # 내레이션 = 노랑
    "tts_size": 62,
    "work_color": "#FFFFFF",
    "aspect_ratio": "5:4",          # 영상 밴드 1080×864 ≈ 템플릿 855px
}

# 화이트리스트 + 범위 — 어휘 밖은 반려 재료(C5: 임의 신설 금지)
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
STYLE_ALLOWED: dict[str, Any] = {
    "title_color": _HEX, "title_color2": _HEX,
    "title_size": (40, 120), "title_size2": (40, 120),
    "title_bold": bool, "title_bold2": bool,
    "subtitle_color": _HEX, "subtitle_size": (36, 90),
    "subtitle_y_margin": (200, 900),
    "tts_color": _HEX, "tts_size": (36, 90), "tts_y_margin": (200, 1200),
    "work_color": _HEX,
    "aspect_ratio": re.compile(r"^\d{1,2}:\d{1,2}$"),
}
POP_LEVELS = ("none", "soft", "strong")
CROP_ANCHORS = ("left", "center", "right")


# ── draft_render (11) ───────────────────────────────────────────────────────

def render_draft(video_path: Path, timeline: list[dict], out_path: Path,
                 *, height: int = DRAFT_HEIGHT, log=print) -> dict:
    """edit_plan 컷만 이어붙인 저사양 중립 캔버스. 뮤트 클립은 무음(볼륨 0).

    filter_complex trim/concat 1패스 — 클립 수십 개 수준(v3 실측 10~13)에 충분하다.
    반환: 비용 실측(기획 멈춤 ② 재료 — elapsed·bytes)."""
    if not timeline:
        raise ValueError("timeline 이 비어 있다 — draft 를 만들 수 없다")
    ffmpeg = find_ffmpeg_command("ffmpeg")
    parts_v, parts_a = [], []
    filters = []
    for i, c in enumerate(timeline):
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        filters.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS,"
            f"scale=-2:{height}[v{i}]")
        vol = "" if c.get("use_original_audio", True) else ",volume=0"
        filters.append(
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS{vol}[a{i}]")
        parts_v.append(f"[v{i}]")
        parts_a.append(f"[a{i}]")
    n = len(timeline)
    filters.append("".join(f"{v}{a}" for v, a in zip(parts_v, parts_a))
                   + f"concat=n={n}:v=1:a=1[vout][aout]")
    t0 = time.time()
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(video_path),
             "-filter_complex", ";".join(filters),
             "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
             "-c:a", "aac", "-ac", "1", str(out_path)],
            check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        # capture_output 이 stderr 를 삼키면 원인 추적이 불가(리뷰 지적) — 꼬리를 싣는다
        raise RuntimeError(
            f"draft 렌더 실패 — ffmpeg stderr 꼬리: "
            f"{(e.stderr or b'')[-400:].decode('utf-8', 'replace')}") from e
    cost = {"elapsed": round(time.time() - t0, 1),
            "bytes": out_path.stat().st_size, "height": height,
            "clips": n}
    log(f"  [v3/draft] {out_path.name} — {cost['elapsed']}s · "
        f"{cost['bytes'] // 1024}KB · 클립 {n}")
    return cost


def edited_beat_windows(story_doc: dict, timeline: list[dict]) -> list[dict]:
    """비트별 편집본 시간 창 — 프레임 샘플 시각의 근거(순수).

    타임라인은 비트 순서대로 조립되므로(assemble) 클립 span_ids 의 비트 소속으로
    창을 복원한다."""
    spans_of_beat = [set(b["span_ids"]) for b in story_doc.get("beats") or []]
    windows: list[dict] = []
    off = 0.0
    for c in timeline:
        dur = float(c["clip_end_sec"]) - float(c["clip_start_sec"])
        owner = next((i for i, s in enumerate(spans_of_beat)
                      if set(c.get("span_ids") or []) & s), None)
        if owner is not None:
            for w in windows:
                if w["beat"] == owner:
                    w["end"] = off + dur
                    break
            else:
                windows.append({"beat": owner, "start": off, "end": off + dur,
                                "role": c.get("role", "")})
        off += dur
    return windows


def sample_beat_frames(draft_path: Path, windows: list[dict], out_dir: Path,
                       *, log=print) -> list[dict]:
    """비트당 시작(+0.2s)·중앙 프레임 추출 → Stage 4 vision 입력."""
    ffmpeg = find_ffmpeg_command("ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict] = []
    for w in windows:
        for tag, t in (("start", w["start"] + 0.2),
                       ("mid", (w["start"] + w["end"]) / 2)):
            t = min(max(t, w["start"]), max(w["start"], w["end"] - 0.05))
            path = out_dir / f"beat{w['beat']:02d}_{tag}.jpg"
            subprocess.run(
                [ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", str(draft_path),
                 "-frames:v", "1", "-q:v", "4", str(path)],
                check=True, capture_output=True)
            frames.append({"beat": w["beat"], "role": w.get("role", ""),
                           "tag": tag, "t_edited": round(t, 3), "path": str(path)})
    log(f"  [v3/draft] 프레임 샘플 {len(frames)}장 → {out_dir.name}/")
    return frames


# ── Stage 4 — style (12) ────────────────────────────────────────────────────

def validate_style_response(resp: Any, n_beats: int) \
        -> tuple[dict | None, list[str], list[str]]:
    """모델 응답 → (정규화 스타일 | None, 반려 사유, 노트). 순수.

    design 은 STYLE_ALLOWED 만 — 어휘 밖 키는 반려(C5), 범위 밖 값도 반려.
    beats 항목은 additive 라 관용(모르는 비트 번호만 반려)."""
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []
    design_in = resp.get("design")
    design: dict[str, Any] = {}
    if design_in is not None and not isinstance(design_in, dict):
        problems.append("design 은 객체여야 한다")
    elif isinstance(design_in, dict):
        for k, v in design_in.items():
            rule = STYLE_ALLOWED.get(k)
            if rule is None:
                problems.append(f"design.{k} 는 어휘 밖 키 — 허용 키: "
                                f"{sorted(STYLE_ALLOWED)}")
                continue
            if rule is bool:
                if not isinstance(v, bool):
                    problems.append(f"design.{k} 는 불리언: {v!r}")
                    continue
            elif isinstance(rule, tuple):
                if not isinstance(v, int) or isinstance(v, bool) \
                        or not rule[0] <= v <= rule[1]:
                    problems.append(f"design.{k} 범위 밖({rule[0]}~{rule[1]}): {v!r}")
                    continue
            elif not isinstance(v, str) or not rule.match(v):
                problems.append(f"design.{k} 형식 위반: {v!r}")
                continue
            if k == "aspect_ratio":
                w, h = (int(x) for x in str(v).split(":"))
                # 영상 밴드는 세로 캔버스 안의 가로형 조각 — h/w 가 1.2 를 넘으면
                # 밴드가 캔버스를 뚫는다('1:2' 통과 시 렌더 사망 — 리뷰 지적)
                if w == 0 or h == 0 or h / w > 1.2 or h / w < 0.4:
                    problems.append(f"design.aspect_ratio 비율 밖(0.4~1.2): {v!r}")
                    continue
            design[k] = v

    beats_out: list[dict] = []
    for b in resp.get("beats") or []:
        if not isinstance(b, dict):
            continue
        num = b.get("number")
        if not isinstance(num, int) or isinstance(num, bool) \
                or not 0 <= num < n_beats:
            problems.append(f"beats number 가 비트 범위(0~{n_beats - 1}) 밖: {num!r}")
            continue
        crop = b.get("crop") if b.get("crop") in CROP_ANCHORS else "center"
        pop = b.get("pop") if b.get("pop") in POP_LEVELS else "none"
        sfx = b.get("sfx")
        sfx = str(sfx).strip()[:40] if isinstance(sfx, str) and sfx.strip() else None
        if b.get("crop") is not None and b.get("crop") not in CROP_ANCHORS:
            notes.append(f"beat{num} crop {b.get('crop')!r} → center 보정")
        if b.get("pop") is not None and b.get("pop") not in POP_LEVELS:
            notes.append(f"beat{num} pop {b.get('pop')!r} → none 보정")
        beats_out.append({"number": num, "crop": crop, "pop": pop, "sfx_cue": sfx})
    if problems:
        return None, problems, notes
    return {"design": design, "beats": beats_out,
            "notes": str(resp.get("notes") or "").strip()[:400]}, [], notes


def style_diff(preset: dict, design: dict) -> dict:
    """프리셋 대비 변경분 기록(발주 합격 기준)."""
    return {k: {"preset": preset.get(k), "styled": v}
            for k, v in design.items() if preset.get(k) != v}


STYLE_PROMPT = """당신은 쇼츠 아트디렉터다. 첨부한 프레임들은 리캡 쇼츠 초벌(draft)의 비트별 장면 샘플이다. 채널 프리셋을 기준으로, 이 편의 화면에 맞는 미세 조정만 제안하라.

## 채널 프리셋 (기준값 — 바꿀 필요 없으면 빈 design)
{preset_block}

## 비트 구성
{beats_block}

## 판단 기준
1. 자막 가독성: 화면 하단이 밝거나 복잡하면 subtitle_color/외곽선 대비, 필요시 subtitle_y_margin 조정.
2. 제목 밴드: 기본 유지 — 화면과 무관(검정 밴드 위)이라 특별한 사유 없으면 손대지 않는다.
3. 비트별: crop(인물이 왼/오른쪽에 쏠린 프레임 → left/right, 기본 center) · pop(팝인 강도 none/soft/strong — 컷 리듬이 빠른 비트만 soft+) · sfx(효과음 큐 한 줄 제안, 필수 아님).
4. 허용 design 키(이 밖은 금지): {allowed_keys}
{reject_block}
## 출력 (JSON 만)
{{"design": {{"subtitle_color": "#FFFFFF"}},
 "beats": [{{"number": 0, "crop": "center", "pop": "soft", "sfx": null}}],
 "notes": "판단 근거 한두 문장"}}"""


def build_style_prompt(preset: dict, story_doc: dict, reject_note: str = "") -> str:
    beats_block = "\n".join(
        f"- beat{b['number']} {b['role']} ({b['time']['start'][3:]}~"
        f"{b['time']['end'][3:]})" + (f" | {b['label']}" if b.get("label") else "")
        for b in story_doc.get("beats") or [])
    reject_block = ""
    if reject_note:
        reject_block = f"\n## ⚠ 직전 제안 반려 — 고쳐서 다시\n{reject_note}\n"
    return STYLE_PROMPT.format(
        preset_block=json.dumps(preset, ensure_ascii=False, indent=1),
        beats_block=beats_block,
        allowed_keys=", ".join(sorted(STYLE_ALLOWED)),
        reject_block=reject_block)


def _call_style_model(gemini, frames: list[dict], prompt: str) -> dict:
    """Flash vision — 프레임을 inline 바이트로 싣는다(업로드 API 불필요한 크기)."""
    types = gemini.types
    parts = []
    for f in frames:
        parts.append(types.Part.from_bytes(
            data=Path(f["path"]).read_bytes(), mime_type="image/jpeg"))
    parts.append(prompt)
    response = gemini.client.models.generate_content(
        model=gemini.config.flash_model_name,
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=8192,
        ))
    text = _extract_json_from_markdown(response.text or "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        try:
            obj, _rest = _loads_first_json(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"응답 JSON 파싱 실패: {e} — 앞 200자: {text[:200]!r}") from e


def run_style(gemini, frames: list[dict], story_doc: dict, *,
              preset: dict | None = None, log=print) -> tuple[dict, dict]:
    """Stage 4 실행 → (style 문서, 감사 기록). 소진 시 프리셋 폴백 — 렌더는 항상 간다."""
    preset = dict(preset if preset is not None else RECAP_PRESET)
    n_beats = len(story_doc.get("beats") or [])
    audit: dict[str, Any] = {"attempts": [], "frames": len(frames)}
    styled: dict | None = None
    reject_note = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = build_style_prompt(preset, story_doc, reject_note)
        log(f"  [v3/style] Flash vision 요청 (시도 {attempt + 1}/{1 + MAX_REASKS}, "
            f"프레임 {len(frames)})")
        t0 = time.time()
        problems: list[str] = []
        notes: list[str] = []
        try:
            resp = _call_style_model(gemini, frames, prompt)
            styled, problems, notes = validate_style_response(resp, n_beats)
        except ValueError as e:
            styled, problems = None, [f"응답 오류: {e}"]
        audit["attempts"].append({"attempt": attempt + 1,
                                  "elapsed": round(time.time() - t0, 1),
                                  "problems": problems, "notes": notes})
        if styled is not None:
            break
        log(f"  [v3/style] 반려 — 사유 {len(problems)}건")
        reject_note = "\n".join(f"- {p}" for p in problems[:15])
    if styled is None:
        log("  [v3/style] ⚠ 재질의 소진 — 프리셋 그대로(스타일 무변경 폴백)")
        styled = {"design": {}, "beats": [], "notes": "재질의 소진 — 프리셋 폴백"}
        audit["fallback"] = True

    design = {**preset, **styled["design"]}
    doc = {
        "schema": "v3_style/v1",
        "design": design,
        "diff": style_diff(preset, styled["design"]),
        "v3_style": {"beats": styled["beats"], "notes": styled["notes"]},
    }
    audit["diff_keys"] = sorted(doc["diff"])
    return doc, audit
