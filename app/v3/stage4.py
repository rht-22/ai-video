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
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.gemini_client import _extract_json_from_markdown, _loads_first_json
from app.v3.seq_analyze import MAX_REASKS

DRAFT_HEIGHT = 480
STYLE_SAMPLE_FPS = 6.0    # Stage 4 가 draft 를 영상으로 볼 때의 Gemini 표본 fps
                          # (2026-08-31 사용자 설정 — 종전 비트 스틸 16장. 컷 리듬
                          #  기반 팝인·효과음 큐 판단은 정지 프레임으론 불가 — §7)

# 채널 프리셋 초기값 — templates/recap_shorts/template.json 실측(프리미어 수동 제작).
# 키는 어댑터 design-* 어휘와 1:1(ves CHANNEL_DESIGN_FLAGS) — C5 동결의 이행.
# 정본: ai-premiere-pro/templates/shinbyeong_shorts/template.json + gw_captions.example
# (가왕쇼 6화를 사람이 프리미어로 수동 제작한 실측 스펙). 숫자는 그 파일이 근거다.
RECAP_PRESET: dict[str, Any] = {
    "title_color": "#FFF04A",       # 1줄 = 셋업(노랑) — 템플릿 그라데이션 시작색
    "title_color2": "#FF6A2D",      # 2줄 = 펀치(주황~빨강) — 그라데이션 시작색
    "title_size": 92,               # 템플릿 line1 92px
    "title_size2": 112,             # 템플릿 line2 112px
    "subtitle_color": "#FFFFFF",    # 대사 기본 = 흰색(화자별 색은 별도 축)
    "subtitle_size": 60,            # 템플릿 60px
    "subtitle_y_margin": 518,       # 템플릿 center_y 1372 → 하단 마진 518
    "tts_color": "#FFE94A",         # 내레이션 = 노랑
    "tts_size": 62,
    "work_color": "#FFFFFF",
    # 영상 밴드 443~1477(높이 1034) — 템플릿 실측. 24:23 → int(1080×23/24)=1035,
    # 렌더러 짝수 보정으로 1034. video_y 를 명시해 정확히 443 에 앉힌다.
    # (종전 "5:4" 는 864px — 배율 0.80 이라 템플릿(0.956) 대비 화면을 16% 손해봤다)
    "aspect_ratio": "24:23",
    "video_y": 443,
}

# 드라마 갈등형 채널 프리셋 (2026-08-31, laeebly 벤치마크 후속) — 값 근거는 둘:
# ① 김부장(이거보고자) v1~v6 실전 렌더 실측 채널값(1:1 밴드 · video_y 450 ·
#    제목 66/84 흰/빨 · 자막 58 흰 · 내레이션 민트 #7DE8D8)
# ② laeebly 상위작 해부 — 제목 '전제 흰/결론 액센트' 2줄(쇼츠몽·빡빡이횽),
#    내레이션 민트는 신병4 벤치마크 공통색. 자막 하단 마진 400 은 E18-6 실측
#    base_margin_v(1:1 밴드 하단 앵커)와 같은 값이다.
DRAMA_CLIP_PRESET: dict[str, Any] = {
    "title_color": "#FFFFFF",       # 1줄 = 전제(흰)
    "title_color2": "#FF4632",      # 2줄 = 결론·사건(빨강) — 색 대비가 곧 약속
    "title_size": 66,
    "title_size2": 84,
    "subtitle_color": "#FFFFFF",
    "subtitle_size": 58,
    "subtitle_y_margin": 400,
    "tts_color": "#7DE8D8",         # 내레이션 = 민트(벤치마크 공통)
    "tts_size": 58,
    "work_color": "#FFFFFF",
    "aspect_ratio": "1:1",
    "video_y": 450,
}

# 프리셋 레지스트리 — 미지정(None)은 recap 그대로(회귀 0). 모르는 이름은 즉시 실패
# (조용히 recap 으로 떨어지면 채널은 새 프리셋을 켰다고 믿은 채 종전 화면을 받는다).
STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "recap": RECAP_PRESET,
    "drama_clip": DRAMA_CLIP_PRESET,
}


def get_style_preset(name: str | None) -> dict[str, Any]:
    if name is None or not str(name).strip():
        return RECAP_PRESET
    preset = STYLE_PRESETS.get(str(name).strip())
    if preset is None:
        raise ValueError(f"모르는 스타일 프리셋 {name!r} — 사용 가능: "
                         f"{sorted(STYLE_PRESETS)}")
    return preset


# 화이트리스트 + 범위 — 어휘 밖은 반려 재료(C5: 임의 신설 금지)
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
STYLE_ALLOWED: dict[str, Any] = {
    "title_color": _HEX, "title_color2": _HEX,
    "title_size": (40, 120), "title_size2": (40, 120),
    "subtitle_color": _HEX, "subtitle_size": (36, 90),
    "subtitle_y_margin": (200, 900),
    "tts_color": _HEX, "tts_size": (36, 90), "tts_y_margin": (200, 1200),
    "work_color": _HEX,
    "aspect_ratio": re.compile(r"^\d{1,2}:\d{1,2}$"),
}
# E21(v1 style_compose 와 같은 처리) — 제목 굵게는 AI 에게 닫혀 있다. 제목 폰트가
# 이미 굵어 볼드를 얹으면 글자 속이 메워진다(2026-08-21 bold.png 실측). 반려하면
# 플랜 전체가 날아가므로 **그 키만 버리고 메모**한다. 사람이 정한 채널·편집실 값은 그대로.
STYLE_DESIGN_IGNORED = {
    "title_bold": "제목 굵게는 폰트가 이미 굵어 글자가 뭉갭니다 — 채널·편집실이 정합니다",
    "title_bold2": "제목 굵게는 폰트가 이미 굵어 글자가 뭉갭니다 — 채널·편집실이 정합니다",
}
POP_LEVELS = ("none", "soft", "strong")
LABEL_SIZE = 56                # 라벨 글자 크기(px) — 템플릿 실측(렌더와 한 곳에서 관리)
LABEL_X_RANGE = (0.18, 0.82)   # 가로 여백 상한 — 폭 계산 결과와 **교집합**으로 쓴다
LABEL_CHAR_W = 0.80            # 한글 1자 폭 ÷ 글자크기 (Jalnan 58px 실측 40.8~46.3px)
LABEL_FX_OVERSHOOT = 1.10      # pop 등장 순간 110% 로 커진다 — 그 폭까지 화면 안이어야
LABEL_EDGE_PAD = 24            # 캔버스 가장자리 여백(px)
LABEL_Y_FALLBACK = 0.526       # 스타일이 위치를 안 줬을 때 렌더가 쓰는 세로(= finalize)
LABEL_BAND_MARGIN = 0.04       # 영상 밴드 안쪽 여백(검정 밴드 침범 방지)
LABEL_ROTATE_LIMIT = 8.0       # 기울기 상한(°, 시계방향 +) — 넘으면 가독성이 깨진다
# 모델은 **이름**으로 고른다(자유 hex 는 안 읽히는 색이 나온다). 값은 템플릿 사전.
LABEL_PALETTE = {"white": "#FFFFFF", "red": "#FF5540", "yellow": "#FFE94A",
                 "blue": "#7ED0FF", "orange": "#FFB637"}
LABEL_COLOR_CYCLE = ("#FF5540", "#FFE94A", "#7ED0FF", "#FFB637")  # 미지정 시 순환
# 라벨 저작(M16, 2026-09-01) — Stage 4 가 초안을 보며 문구·시각·위치를 직접 쓴다.
# 라벨의 재료는 화면(표정·행동·구도)인데 종전에는 화면을 못 보는 Stage 3 이 문구와
# 시각을 정했다 — 배치를 잘해도 문구·시각이 장면과 안 물리는 실사고(사용자 지적).
LABEL_MAX_COUNT = 3            # 편당 상한(Stage 3 규칙 5 의 0~3 계승) — 초과는 드롭+기록
LABEL_MIN_DUR_SEC = 0.6        # 이보다 짧으면 읽기 전에 사라진다
LABEL_MAX_DUR_SEC = 4.0        # finalize.LABEL_MAX_SEC 와 같은 값(레퍼런스 실측 상한)
LABEL_TEXT_MAX = 12            # 괄호 제외 자수 — 길면 화면을 가로지른다
# 등장 효과 — 렌더러(build_texts_ass/_text_fx_tags)가 이미 굽는 어휘. 기본은 pop
# ("띠용" 오버슈트 30%→110%→100%, 220ms). 미지정이 none 이면 라벨이 그냥 튀어나온다.
LABEL_FX = ("pop", "glow", "shake", "none")
# 라벨 앵커 + 프로브(2026-09-04, 지금불륜 EP01 '(영혼 탈곡됨)' 실사고 — 모델이 6fps 초안을
# 보고 절대초로 적은 시각이 다른 아이의 웃는 얼굴 위에 앉았다). 시각은 절대초가 아니라
# **편집본 이벤트**(대사 줄 L·대사 직후 정적 G·컷 시작 C)에 앵커하고 코드가 이벤트 표에서
# 뽑는다(933 방어 규율) — 사라진 이벤트엔 못 붙는다. 그 뒤 앵커 창을 잘라 10fps 로 다시
# 보며 표정이 시작하는 프레임을 잡는다(덮개 프로브와 같은 기계 · refine._call_probe).
LABEL_ANCHOR_OFFSET_RANGE = (-1.0, 2.0)   # 앵커 시작 기준 오프셋(초) 허용 범위
LABEL_ANCHOR_TOLERANCE_SEC = 1.5          # 앵커 해석값과 모델 절대초가 이 이상 갈리면 드롭
LABEL_GAP_MIN_SEC = 0.3                   # 이보다 짧은 대사 사이는 정적 이벤트로 안 센다
LABEL_DEFAULT_DUR_SEC = 1.5
LABEL_PROBE_PRE_SEC = 1.0                 # 프로브 창 = 앵커 시각 −1.0 ~ +3.0 (클립 안으로)
LABEL_PROBE_POST_SEC = 3.0
LABEL_PROBE_MIN_WIN_SEC = 0.8             # 창이 이보다 짧으면 프로브 생략(앵커값 유지)
LABEL_PROBE_BUDGET = 3                    # 편당 Flash 프로브 상한(= LABEL_MAX_COUNT)
CROP_ANCHORS = ("left", "center", "right")


def label_x_range(text: str, *, size: int = LABEL_SIZE,
                  canvas_w: int = 1080) -> tuple[float, float]:
    r"""그 라벨이 잘리지 않는 x(중심) 범위. 순수.

    ASS 는 \an5\pos = **글자 중심** 기준이라 반폭이 캔버스를 넘으면 그냥 잘린다
    (libass 는 \pos 에서 자동 줄바꿈을 하지 않는다). 13자 라벨은 x=0.82 에서
    106px 잘린다 — 적대 리뷰 H1 실측."""
    half = (len(str(text)) * size * LABEL_CHAR_W * LABEL_FX_OVERSHOOT) / 2
    half += size * 0.07 + LABEL_EDGE_PAD           # 외곽선 + 가장자리 여백
    lo = max(LABEL_X_RANGE[0], half / canvas_w)
    hi = min(LABEL_X_RANGE[1], 1.0 - half / canvas_w)
    return (0.5, 0.5) if lo >= hi else (lo, hi)


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
        # 정보 화면 붙잡기(2026-09-03): 초안도 마지막 프레임을 hold_sec 만큼 유지해야
        # watch_trim·style 이 보는 시계가 최종과 같다
        _h = float(c.get("hold_sec") or 0.0)
        _hold_v = f",tpad=stop_mode=clone:stop_duration={_h:.3f}" if _h > 0 else ""
        _hold_a = f",apad,atrim=end={e - s + _h:.3f}" if _h > 0 else ""
        filters.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS,"
            f"scale=-2:{height}{_hold_v}[v{i}]")
        vol = "" if c.get("use_original_audio", True) else ",volume=0"
        filters.append(
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS{vol}{_hold_a}[a{i}]")
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


def edited_clip_windows(timeline: list[dict]) -> list[dict]:
    """타임라인 → 편집본 좌표 클립 창 [{clip, start, end}] (hold_sec 포함). 순수."""
    out, t = [], 0.0
    for k, c in enumerate(timeline or []):
        dur = float(c["clip_end_sec"]) - float(c["clip_start_sec"]) + float(c.get("hold_sec") or 0.0)
        out.append({"clip": k, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur
    return out


def _clip_at(t: float, clips: list[dict]) -> dict | None:
    """편집본 시각 t 가 속한 클립 — **반개구간** [start, end). 경계 시각(컷 시작 = 앞 클립 끝)은
    뒤 클립이다(부동소수 여유 1e-6 — 4.0 이 [0,4] 에 잡히던 실측 결함)."""
    return next((c for c in clips if c["start"] - 1e-6 <= t < c["end"] - 1e-6), None)


def label_events(dialogue: list[dict] | None, timeline: list[dict] | None) -> list[dict]:
    """라벨 앵커 어휘 — 편집본 좌표 이벤트 표. 순수·결정적.
      L<n>  대사 줄 n(자막 세그먼트 순번) — start~end
      G<n>  대사 줄 n 직후 정적 — 줄 끝 ~ 다음 줄 시작(또는 그 클립 끝), ≥ LABEL_GAP_MIN_SEC
      C<k>  클립 k 시작 — 클립 창 전체
    대사 없는 구간의 라벨은 G 나 C 에 앵커한다 — 라벨을 못 다는 자리는 없다."""
    clips = edited_clip_windows(timeline or [])
    ev: list[dict] = []
    segs = [sg for sg in (dialogue or []) if sg.get("start_sec") is not None]
    for n, sg in enumerate(segs):
        s0, s1 = float(sg["start_sec"]), float(sg["end_sec"])
        ev.append({"id": f"L{n}", "kind": "line", "start": round(s0, 3), "end": round(s1, 3),
                   "text": str(sg.get("text") or "")})
        nxt = float(segs[n + 1]["start_sec"]) if n + 1 < len(segs) else None
        _c = _clip_at(s1 - 1e-3, clips)          # 줄 끝이 컷 경계면 그 줄이 속한(앞) 클립
        clip_end = _c["end"] if _c else None
        g1 = min(x for x in (nxt, clip_end) if x is not None) if (nxt is not None or clip_end is not None) else None
        if g1 is not None and g1 - s1 >= LABEL_GAP_MIN_SEC:
            ev.append({"id": f"G{n}", "kind": "gap", "start": round(s1, 3), "end": round(g1, 3),
                       "text": f"「{sg.get('text')}」 직후 정적"})
    for c in clips:
        ev.append({"id": f"C{c['clip']}", "kind": "clip", "start": c["start"], "end": c["end"],
                   "text": ""})
    return ev


def label_events_block(events: list[dict]) -> tuple[str, str]:
    """프롬프트 블록 (대사+정적 줄, 컷 경계 줄). 순수."""
    lines, cuts = [], []
    for e in events:
        if e["kind"] == "line":
            lines.append(f"- {e['id']} {e['start']:.1f}~{e['end']:.1f}s 「{e['text']}」")
        elif e["kind"] == "gap":
            lines.append(f"    · {e['id']} 정적 {e['start']:.1f}~{e['end']:.1f}s")
        else:
            cuts.append(f"{e['id']} {e['start']:.1f}~{e['end']:.1f}s")
    return ("\n".join(lines) or "- (대사 없음)"), (" · ".join(cuts) or "(없음)")


def resolve_label_anchor(item: dict, events: list[dict], clips: list[dict]
                         ) -> tuple[float | None, float | None, str | None, str | None]:
    """앵커 → (start, end, anchor_id, 사유). 순수.
    start = 이벤트 시작 + offset(범위 클램프) → 그 시각이 속한 **클립 안**으로 클램프(라벨이 컷을
    넘어 다음 장면 얼굴 위에 남지 않게) · end = start + duration(0.6~2.5) ≤ 클립 끝.
    모델이 절대초도 함께 냈고 앵커 해석값과 LABEL_ANCHOR_TOLERANCE_SEC 이상 갈리면 사유를
    돌려준다(이벤트를 잘못 짚었다는 신호 — 드롭)."""
    aid = str(item.get("anchor") or "").strip().upper()
    ev = next((e for e in events if e["id"] == aid), None)
    if ev is None:
        return None, None, aid or None, f"앵커 {aid!r} 이 이벤트 표에 없다" if aid else "앵커 없음"
    try:
        off = float(item.get("offset_sec") or 0.0)
    except (TypeError, ValueError):
        off = 0.0
    if not math.isfinite(off):
        off = 0.0
    off = min(max(off, LABEL_ANCHOR_OFFSET_RANGE[0]), LABEL_ANCHOR_OFFSET_RANGE[1])
    try:
        dur = float(item.get("duration_sec") or LABEL_DEFAULT_DUR_SEC)
    except (TypeError, ValueError):
        dur = LABEL_DEFAULT_DUR_SEC
    if not math.isfinite(dur):
        dur = LABEL_DEFAULT_DUR_SEC
    dur = min(max(dur, LABEL_MIN_DUR_SEC), LABEL_MAX_DUR_SEC)
    start = ev["start"] + off
    # 가두는 클립은 **앵커 이벤트가 속한** 클립이다 — 오프셋이 컷을 넘어도 다음 장면으로
    # 새지 않는다(이번 사고의 절반: 다음 컷의 다른 얼굴 위에 남은 라벨)
    if ev["kind"] == "clip":
        clip = next((c for c in clips if f"C{c['clip']}" == ev["id"]), None)
    else:
        clip = _clip_at(ev["start"], clips)
    if clip is None:
        clip = _clip_at(start, clips)
    if clip is not None:
        start = min(max(start, clip["start"]), max(clip["start"], clip["end"] - LABEL_MIN_DUR_SEC))
        end = min(start + dur, clip["end"])
    else:
        end = start + dur
    if end - start < LABEL_MIN_DUR_SEC:
        end = start + LABEL_MIN_DUR_SEC
    why = None
    try:
        abs_s = float(item["start_sec"]) if item.get("start_sec") is not None else None
    except (TypeError, ValueError):
        abs_s = None
    if abs_s is not None and abs(abs_s - start) > LABEL_ANCHOR_TOLERANCE_SEC:
        why = f"앵커 {aid}({start:.1f}s) 와 절대초 {abs_s:.1f}s 가 {abs(abs_s - start):.1f}s 갈림"
    return round(start, 3), round(end, 3), aid, why


LABEL_PROBE_PROMPT = """당신은 쇼츠 편집자다. 첨부한 클립은 편집본의 {t0}~{t1}초 구간(클립 안 0초 = 편집본 {t0}초)이다.
이 자리에 괄호 라벨 {text} 를 띄우려 한다. 라벨은 화면에 대한 반응이다 — 표정이 꺾이는 순간, 인물 정체, 눈에 띄는 행동.
{context}
질문:
1. 이 클립 안에 이 라벨이 **맞는** 표정·행동이 있는가? 인물의 감정이 라벨과 반대이거나(웃는 얼굴에 '충격') 그런 순간이 없으면 fit=false.
2. 있다면 그 표정·행동이 **시작하는** 시각(클립 안 초, 0~{length:.1f})은? 라벨은 그 순간부터 {dur:.1f}초 뜬다.
## 출력 (JSON 만)
{{"fit": true, "start_sec": 0.8, "reason": "한 문장"}}"""


def probe_labels(labels: list[dict], clips: list[dict], *, ask, log=print,
                 budget: int = LABEL_PROBE_BUDGET) -> tuple[list[dict], list[dict]]:
    """앵커된 라벨마다 창(앵커 −1.0 ~ +3.0, 클립 안)을 잘라 Flash 에 묻는다 — ask(t0, t1, label)
    → {fit, start_sec, reason} | None. fit=false 는 드롭(문구가 화면과 안 맞음), 맞으면 시작을
    그 순간으로 옮기고(클립 안 클램프) 길이는 유지. 호출 실패·예산 초과는 앵커값 유지 + 기록.
    순수(ask 가 유일한 부수효과) — 라벨 dict 는 사본."""
    out, audit = [], []
    calls = 0
    for lb in labels:
        s0, s1 = float(lb["start_sec"]), float(lb["end_sec"])
        dur = s1 - s0
        clip = _clip_at(s0, clips)
        lo = max(s0 - LABEL_PROBE_PRE_SEC, clip["start"] if clip else 0.0)
        hi = min(s0 + LABEL_PROBE_POST_SEC, clip["end"] if clip else s0 + LABEL_PROBE_POST_SEC)
        rec = {"text": lb["text"], "anchor": lb.get("anchor"), "window": [round(lo, 3), round(hi, 3)],
               "before": [round(s0, 3), round(s1, 3)]}
        if hi - lo < LABEL_PROBE_MIN_WIN_SEC:
            rec["result"] = "창이 짧아 생략"
            audit.append(rec); out.append(dict(lb)); continue
        if calls >= budget:
            rec["result"] = f"예산 {budget} 초과 — 미검사"
            audit.append(rec); out.append(dict(lb)); continue
        calls += 1
        try:
            resp = ask(lo, hi, lb)
        except Exception as e:  # noqa: BLE001 — 프로브 실패는 앵커값 유지(조용한 실패 아님)
            resp = None
            rec["error"] = str(e)[:160]
        if not isinstance(resp, dict):
            rec["result"] = "응답 없음 — 앵커값 유지"
            log(f"  [v3/label-probe] ⚠ {lb['text']} 프로브 실패 — 앵커값 유지")
            audit.append(rec); out.append(dict(lb)); continue
        rec["reason"] = str(resp.get("reason") or "")[:160]
        if resp.get("fit") is False:
            rec["result"] = "불일치 — 드롭"
            log(f"  [v3/label-probe] {lb['text']} 화면과 불일치 → 드롭 ({rec['reason'][:60]})")
            audit.append(rec); continue
        try:
            rel = float(resp.get("start_sec"))
        except (TypeError, ValueError):
            rec["result"] = "start_sec 없음 — 앵커값 유지"
            audit.append(rec); out.append(dict(lb)); continue
        if not math.isfinite(rel) or rel < -0.05 or rel > (hi - lo) + 0.05:
            rec["result"] = f"start_sec {rel} 창 밖 — 앵커값 유지"
            audit.append(rec); out.append(dict(lb)); continue
        new_s = lo + max(0.0, rel)
        if clip is not None:
            new_s = min(new_s, max(clip["start"], clip["end"] - LABEL_MIN_DUR_SEC))
        new_e = new_s + dur
        if clip is not None:
            new_e = min(new_e, clip["end"])
        if new_e - new_s < LABEL_MIN_DUR_SEC:
            new_e = new_s + LABEL_MIN_DUR_SEC
        rec["after"] = [round(new_s, 3), round(new_e, 3)]
        rec["result"] = f"이동 {new_s - s0:+.2f}s" if abs(new_s - s0) > 0.05 else "유지(±0.05s)"
        log(f"  [v3/label-probe] {lb['text']} {s0:.2f} → {new_s:.2f}s ({rec['result']}) · {rec['reason'][:60]}")
        audit.append(rec)
        out.append(dict(lb, start_sec=round(new_s, 3), end_sec=round(new_e, 3),
                        probe={"window": rec["window"], "raw": round(rel, 2),
                               "moved": round(new_s - s0, 3), "reason": rec["reason"]}))
    return out, audit


def edited_beat_windows(story_doc: dict, timeline: list[dict]) -> list[dict]:
    """비트별 편집본 시간 창 — 프레임 샘플 시각의 근거(순수).

    타임라인은 비트 순서대로 조립되므로(assemble) 클립 span_ids 의 비트 소속으로
    창을 복원한다."""
    spans_of_beat = [set(b["span_ids"]) for b in story_doc.get("beats") or []]
    windows: list[dict] = []
    off = 0.0
    for c in timeline:
        dur = float(c["clip_end_sec"]) - float(c["clip_start_sec"]) + float(c.get("hold_sec") or 0.0)
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

def validate_style_response(resp: Any, n_beats: int,
                            band: tuple[float, float] | None = None,
                            labels: list[dict] | None = None,
                            preset: dict | None = None,
                            duration: float | None = None,
                            events: list[dict] | None = None,
                            clips: list[dict] | None = None) \
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
            if k in STYLE_DESIGN_IGNORED:
                notes.append(f"design.{k} 무시 — {STYLE_DESIGN_IGNORED[k]}")
                continue
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

    # ── M12 라벨 배치 — 모델이 화면을 보고 정한 x·y·기울기·색·등장효과 ───────
    # 원칙: **라벨을 잃지 않는다.** 어떤 항목이 잘못돼도 반려가 아니라 그 항목만
    # 버리고(→ 렌더가 기본 위치로) 노트를 남긴다. 위치 판단 실패가 스타일 전체
    # (비트 crop/pop/sfx·design) 실패로 번지면 매 편 손해다(적대 리뷰 H2 확정).
    #
    # 밴드는 **확정된 design 기준**으로 다시 잰다 — 모델이 aspect_ratio 를 바꾸면
    # 프리셋으로 계산한 밴드는 렌더 기하와 어긋난다(적대 리뷰 C1 확정: 16:9 로
    # 바꾸면 y=0.68 이 검정 밴드 43px 침범).
    if preset is not None:
        from app.v3.finalize import design_from_style, video_band_ratio
        band = video_band_ratio(design_from_style({**preset, **design}))
    label_items = resp.get("labels") or []
    if label_items and band is None:
        # 밴드를 모르면 검정 밴드 침범을 못 막는다 → 배치를 포기하되 **플랜은 살린다**
        notes.append("밴드 기하 미상 — 라벨은 기본 위치로 둔다")
        label_items = []
    # ── M16 저작 라벨 — item 에 text 가 있으면 모델이 직접 쓴 라벨이다 ──────
    # 규율은 M12 그대로: **라벨을 잃지 않는다** — 항목 단위 드롭+노트, 플랜 반려 없음.
    authored: list[dict] = []
    remaining: list = []
    for item in label_items:
        if isinstance(item, dict) and isinstance(item.get("text"), str) \
                and item["text"].strip():
            authored.append(item)
        else:
            remaining.append(item)
    authored_out: list[dict] = []
    for item in authored:
        if len(authored_out) >= LABEL_MAX_COUNT:
            notes.append(f"라벨 {LABEL_MAX_COUNT}개 초과 — 이후 항목 드롭: "
                         f"{item.get('text')!r}")
            continue
        text = item["text"].strip()
        if not (text.startswith("(") and text.endswith(")")):
            notes.append(f"라벨 괄호 보정: {text!r}")
            text = f"({text.strip('()')})"
        if len(text) - 2 > LABEL_TEXT_MAX:
            notes.append(f"라벨 {len(text) - 2}자 — {LABEL_TEXT_MAX}자 초과 드롭: {text!r}")
            continue
        anchor_id = None
        anchored = False
        if events is not None and item.get("anchor"):
            a_s, a_e, anchor_id, why = resolve_label_anchor(item, events, clips or [])
            if a_s is not None and why is None:
                t0, t1, anchored = a_s, a_e, True
            elif a_s is not None:
                notes.append(f"라벨 {text!r} {why} — 드롭")
                continue
            else:
                notes.append(f"라벨 {text!r} {why} — 절대초로 폴백")
        if not anchored:
            if events is not None and not item.get("anchor"):
                notes.append(f"라벨 {text!r} 앵커 없음 — 절대초로 폴백")
            try:
                t0, t1 = float(item["start_sec"]), float(item["end_sec"])
            except (KeyError, TypeError, ValueError):
                notes.append(f"라벨 {text!r} 시각 없음/형식 오류 — 드롭")
                continue
        if not (math.isfinite(t0) and math.isfinite(t1)) or t1 <= t0:
            notes.append(f"라벨 {text!r} 시각 역전/비정상 — 드롭")
            continue
        if duration is not None:
            t0 = min(max(t0, 0.0), max(0.0, duration - LABEL_MIN_DUR_SEC))
            t1 = min(t1, duration)
        if t1 - t0 < LABEL_MIN_DUR_SEC:
            t1 = t0 + LABEL_MIN_DUR_SEC
            if duration is not None and t1 > duration:
                notes.append(f"라벨 {text!r} 영상 밖 — 드롭")
                continue
        if t1 - t0 > LABEL_MAX_DUR_SEC:
            notes.append(f"라벨 {text!r} {t1 - t0:.1f}s → {LABEL_MAX_DUR_SEC:g}s 로 자름")
            t1 = t0 + LABEL_MAX_DUR_SEC
        try:
            x, y = float(item.get("x")), float(item.get("y"))
        except (TypeError, ValueError):
            x, y = 0.5, LABEL_Y_FALLBACK
            notes.append(f"라벨 {text!r} 좌표 없음 — 기본 위치")
        if not (math.isfinite(x) and math.isfinite(y)):
            x, y = 0.5, LABEL_Y_FALLBACK
        x_lo, x_hi = label_x_range(text)
        cx = min(max(x, x_lo), x_hi)
        cy = y
        if band is not None:
            cy = min(max(y, band[0] + LABEL_BAND_MARGIN), band[1] - LABEL_BAND_MARGIN)
        try:
            rot = float(item.get("rotate") or 0.0)
        except (TypeError, ValueError):
            rot = 0.0
        if not math.isfinite(rot):
            rot = 0.0
        cr = min(max(rot, -LABEL_ROTATE_LIMIT), LABEL_ROTATE_LIMIT)
        color = LABEL_PALETTE.get(str(item.get("color") or "").strip().lower()) \
            or LABEL_COLOR_CYCLE[len(authored_out) % len(LABEL_COLOR_CYCLE)]
        fx = str(item.get("fx") or "pop").strip().lower()
        if fx not in LABEL_FX:
            fx = "pop"
        entry = {"text": text,
                 "start_sec": round(t0, 3), "end_sec": round(t1, 3),
                 "x": round(cx, 3), "y": round(cy, 3),
                 "rotate": round(cr, 1), "color": color, "fx": fx}
        if anchored:
            entry["anchor"] = anchor_id           # additive — 감사 기록("라벨 ← 이벤트")
        authored_out.append(entry)
    label_items = remaining          # 남은 것은 구(index) 경로 — 하위호환

    by_index = {int(lb["index"]): lb for lb in labels or [] if "index" in lb}
    n_labels = len(labels) if labels is not None else None
    labels_out: list[dict] = []
    seen: set[int] = set()
    for item in label_items:
        if not isinstance(item, dict):
            notes.append(f"labels 항목이 객체가 아님 — 버린다: {item!r}")
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            notes.append(f"labels index 가 정수가 아님 — 버린다: {idx!r}")
            continue
        if n_labels is not None and idx >= n_labels:
            # 모델이 1-based 로 답하면 전 라벨이 조용히 한 칸씩 밀린다(리뷰 C3)
            notes.append(f"labels index {idx} 는 라벨 {n_labels}개 밖 — 버린다")
            continue
        if idx in seen:
            notes.append(f"labels[{idx}] 중복 — 먼저 온 것을 쓴다")
            continue
        try:
            x, y = float(item["x"]), float(item["y"])
        except (KeyError, TypeError, ValueError):
            notes.append(f"labels[{idx}] x·y 없음/형식 오류 — 기본 위치로 둔다")
            continue
        if not (math.isfinite(x) and math.isfinite(y)):     # NaN 은 렌더를 죽인다
            notes.append(f"labels[{idx}] 좌표가 수가 아님 — 기본 위치로 둔다")
            continue
        seen.add(idx)
        x_lo, x_hi = label_x_range(str((by_index.get(idx) or {}).get("text", "")))
        cx = min(max(x, x_lo), x_hi)
        cy = min(max(y, band[0] + LABEL_BAND_MARGIN), band[1] - LABEL_BAND_MARGIN)
        if (cx, cy) != (x, y):
            notes.append(f"labels[{idx}] 위치 보정: ({x:.2f},{y:.2f})→({cx:.2f},{cy:.2f})")
        try:
            rot = float(item.get("rotate") or 0.0)
        except (TypeError, ValueError):
            rot = 0.0
            notes.append(f"labels[{idx}] rotate 형식 오류 → 0°")
        if not math.isfinite(rot):
            rot = 0.0
        cr = min(max(rot, -LABEL_ROTATE_LIMIT), LABEL_ROTATE_LIMIT)
        if cr != rot:
            notes.append(f"labels[{idx}] 기울기 보정: {rot:g}°→{cr:g}°")
        raw_color = item.get("color")
        color = LABEL_PALETTE.get(str(raw_color or "").strip().lower())
        if color is None:
            # 미지정은 조용히 순환 기본값(단조 회귀 방지) · 오타는 노트를 남긴다
            color = LABEL_COLOR_CYCLE[idx % len(LABEL_COLOR_CYCLE)]
            if raw_color:
                notes.append(f"labels[{idx}] 색 이름 미지원({raw_color!r}) → 기본 순환")
        raw_fx = item.get("fx")
        fx = str(raw_fx or "pop").strip().lower()
        if fx not in LABEL_FX:
            notes.append(f"labels[{idx}] fx 미지원({raw_fx!r}) → pop")
            fx = "pop"
        labels_out.append({"index": idx, "x": round(cx, 3), "y": round(cy, 3),
                           "rotate": round(cr, 1), "color": color, "fx": fx})
    if n_labels:
        # 기능이 조용히 no-op 이 되면 사용자가 신고할 때까지 모른다(리뷰 M4)
        missed = n_labels - len(labels_out)
        if missed:
            notes.append(f"라벨 {n_labels}개 중 {missed}개 미배치 — 기본 위치로 렌더")

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
            "labels": authored_out + labels_out,
            "notes": str(resp.get("notes") or "").strip()[:400]}, [], notes


def style_diff(preset: dict, design: dict) -> dict:
    """프리셋 대비 변경분 기록(발주 합격 기준)."""
    return {k: {"preset": preset.get(k), "styled": v}
            for k, v in design.items() if preset.get(k) != v}


STYLE_PROMPT = """당신은 쇼츠 아트디렉터다. 첨부한 영상은 리캡 쇼츠 초벌(draft — 디자인 미적용 중립 캔버스, 편집본 전체)이다. 채널 프리셋을 기준으로, 이 편의 화면·리듬에 맞는 미세 조정만 제안하라.

## 채널 프리셋 (기준값 — 바꿀 필요 없으면 빈 design)
{preset_block}

## 비트 구성 (영상 내 시각 — 편집본 좌표)
{beats_block}

## 대사 타임라인 (시각은 이 영상 기준 — 화면과 대조하라. L=대사 줄, G=그 대사 직후 정적)
{dialogue_block}

## 컷(클립) 경계 (C=컷 시작 — 대사 없는 장면의 앵커)
{cuts_block}

## 판단 기준
0. **라벨 작성 + 배치** — 초안을 보고 괄호 라벨 **0~3개를 직접 써라**(문구·시각·위치 전부 네 몫이다). 라벨은 화면에 대한 반응이다 — 표정이 꺾이는 순간, 정체를 알려야 할 인물, 눈에 띄는 행동. **없으면 안 써도 된다(0개가 정상일 수 있다).**
   - `text`: 괄호 심리·행동·정체 강조, 괄호 제외 12자 이내. **이 편의 화면·대사에서 뽑아라** — 특히 값진 것은 ① 인물 정체(작품을 모르는 시청자가 누군지 그 자리에서 알게) ② 심리가 꺾이는 순간 ③ 눈에 띄는 행동. 페이오프(핵심 대답·반전) 순간에는 얹지 마라 — 웃기는 것은 대사 자신이다.
   - **시각은 절대초가 아니라 앵커로 적어라.** `anchor` = 위 표의 이벤트 id 하나(L=그 대사 줄, G=그 대사 직후 정적, C=컷 시작 — 대사 없는 장면). 리액션 라벨은 보통 그 대사 **직후 정적(G)** 이나 다음 컷 시작(C)이다. `offset_sec`(-1.0~2.0): 앵커 시작에서 표정·행동이 보이기까지의 지연. `duration_sec`: 0.6~2.5(길게 띄우지 않는다). 코드가 앵커 시각을 표에서 뽑아 그 컷 안으로 가둔 뒤 그 창을 다시 보며 프레임을 맞춘다 — 절대초를 세지 마라. 표에 없는 순간엔 라벨을 달 수 없다.
   - **종류별 배치.** ⓐ **인물 지목 라벨**(호칭·직업·정체·이름)은 **그 인물 바로 옆**: 머리 위나 어깨 옆, 얼굴을 가리지 않는 **가장 가까운** 여백(화면 반대편에 두면 여러 명 중 누구를 가리키는지 모른다). ⓑ 심리·행동·훈수 라벨은 빈 곳으로.
   - `x`·`y`(0~1 비율): (ⓑ 기준) 인물 얼굴·방송 자체 자막·자막 밴드를 **피해서** 빈 곳에. 세로는 영상 밴드({band_lo:.2f}~{band_hi:.2f}) 안. 인물이 왼쪽이면 오른쪽 여백, 오른쪽이면 왼쪽 — 가운데(0.5)는 양쪽이 다 막혔을 때만. 긴 라벨일수록 가장자리 금지(잘리면 코드가 안쪽으로 당긴다). ⓐ 는 이 규칙의 예외다 — 인물을 따라가되 얼굴·자막은 가리지 않는다.
   - `rotate`(-8~8°, 시계방향 +): 감정이 튀는 라벨만 살짝(3~6°). 차분한 라벨은 0.
   - `color`: {palette_names} 중 하나 — **배경과의 대비가 우선이다.** 화면이 그 색 계열이면 쓰지 마라(붉은 조명 위 red 는 글자가 사라진다). 확신이 없으면 white·yellow. 연달아 나오면 서로 다른 색.
   - `fx`: `pop`(기본 — 띠용) · `glow`(**어두운 화면 전용** — 밝거나 같은 색 계열 배경에서는 외곽선이 사라져 안 보인다) · `shake`(충격·놀람) · `none`(차분).
1. 자막 가독성: 화면 하단이 밝거나 복잡하면 subtitle_color/외곽선 대비, 필요시 subtitle_y_margin 조정.
2. 제목 밴드: 기본 유지 — 화면과 무관(검정 밴드 위)이라 특별한 사유 없으면 손대지 않는다.
3. 비트별: crop(인물이 왼/오른쪽에 쏠린 구간 → left/right, 기본 center) · pop(팝인 강도 none/soft/strong — **실제 컷 리듬을 보고**: 컷이 잦고 호흡 빠른 비트만 soft+) · sfx(리듬 전환점의 효과음 큐 한 줄, 필수 아님).
4. 허용 design 키(이 밖은 금지): {allowed_keys}
   ⚠ **제목을 굵게 하지 마라** — `title_bold`·`title_bold2` 는 쓸 수 없다(보내면 그 키만 버려진다). 제목 폰트가 이미 굵어서 볼드를 얹으면 글자 속이 메워진다.
{reject_block}
## 출력 (JSON 만)
{{"design": {{"subtitle_color": "#FFFFFF"}},
 "beats": [{{"number": 0, "crop": "center", "pop": "soft", "sfx": null}}],
 "labels": [{{"text": "(…)", "anchor": "G7", "offset_sec": 0.2, "duration_sec": 1.5, "x": 0.72, "y": 0.36, "rotate": -4, "color": "yellow", "fx": "pop"}}],
 "notes": "판단 근거 한두 문장"}}"""


def build_style_prompt(preset: dict, story_doc: dict, reject_note: str = "",
                       dialogue: list[dict] | None = None,
                       windows: list[dict] | None = None,
                       labels: list[dict] | None = None,
                       band: tuple[float, float] = (0.231, 0.769),
                       events: list[dict] | None = None) -> str:
    if windows:
        # draft 는 편집본 좌표 — 원본 절대초(b.time)를 보여주면 영상 속 시각과 어긋난다
        by_beat = {w["beat"]: w for w in windows}
        beats_block = "\n".join(
            f"- beat{b['number']} {b['role']} "
            f"({by_beat[b['number']]['start']:.1f}~{by_beat[b['number']]['end']:.1f}s)"
            + (f" | {b['label']}" if b.get("label") else "")
            for b in story_doc.get("beats") or [] if b["number"] in by_beat)
    else:
        beats_block = "\n".join(
            f"- beat{b['number']} {b['role']} ({b['time']['start'][3:]}~"
            f"{b['time']['end'][3:]})" + (f" | {b['label']}" if b.get("label") else "")
            for b in story_doc.get("beats") or [])
    reject_block = ""
    if reject_note:
        reject_block = f"\n## ⚠ 직전 제안 반려 — 고쳐서 다시\n{reject_note}\n"
    if events is not None:
        dialogue_block, cuts_block = label_events_block(events)
    else:
        dialogue_block = "\n".join(
            f"- {float(sg['start_sec']):.1f}~{float(sg['end_sec']):.1f}s 「{sg['text']}」"
            for sg in dialogue or []) or "- (대사 없음)"
        cuts_block = "(없음 — 타임라인 미제공)"
    return STYLE_PROMPT.format(
        dialogue_block=dialogue_block, cuts_block=cuts_block,
        preset_block=json.dumps(preset, ensure_ascii=False, indent=1),
        beats_block=beats_block,
        band_lo=band[0] + LABEL_BAND_MARGIN, band_hi=band[1] - LABEL_BAND_MARGIN,
        band_mid=LABEL_Y_FALLBACK,
        palette_names=" · ".join(LABEL_PALETTE),
        allowed_keys=", ".join(sorted(STYLE_ALLOWED)),
        reject_block=reject_block)


def _call_style_model(gemini, draft_path: Path, prompt: str) -> dict:
    """Flash vision — draft 영상 전체를 6fps 표본으로 본다(2026-08-31 사용자 설정).

    스틸 16장으로는 컷 리듬(팝인·효과음 큐 근거 — §7)을 볼 수 없었다. draft 는
    1분 남짓이라 6fps 표본도 수만 토큰 — Pro 청크 분석 대비 미미하다."""
    from app.v3.seq_analyze import _upload_video
    types = gemini.types
    uploaded = _upload_video(gemini, draft_path, log=lambda *a: None)
    try:
        part = types.Part(file_data=types.FileData(file_uri=uploaded.uri,
                                                   mime_type="video/mp4"),
                          video_metadata=types.VideoMetadata(fps=STYLE_SAMPLE_FPS))
        response = gemini.client.models.generate_content(
            model=gemini.config.flash_model_name,
            contents=[part, prompt],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                max_output_tokens=8192,
            ))
    finally:
        try:
            gemini.client.files.delete(name=uploaded.name)
        except Exception:  # noqa: BLE001
            pass
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


def _default_label_probe(gemini, draft_path: Path, *, log=print):
    """draft(편집본 좌표)를 창으로 잘라 Flash 에 묻는 ask(t0, t1, label). refine 의 프로브
    기계 재사용(480p·10fps 재단 · 6fps 표본 · JSON 응답)."""
    from app.modules.ffmpeg_utils import find_ffmpeg_command
    from app.v3.refine import _call_probe, _cut_probe_clip
    ffmpeg = find_ffmpeg_command("ffmpeg")
    out_dir = Path(draft_path).parent / "label_probes"

    def ask(t0: float, t1: float, lb: dict) -> dict | None:
        out_dir.mkdir(parents=True, exist_ok=True)
        clip = out_dir / f"label_{int(round(t0 * 100)):06d}.mp4"
        _cut_probe_clip(ffmpeg, Path(draft_path), t0, t1, clip)
        ctx = f"앵커: {lb.get('anchor')} · 라벨 창(편집본): {lb['start_sec']:.1f}~{lb['end_sec']:.1f}s"
        prompt = LABEL_PROBE_PROMPT.format(
            t0=f"{t0:.1f}", t1=f"{t1:.1f}", text=lb["text"], context=ctx,
            length=t1 - t0, dur=float(lb["end_sec"]) - float(lb["start_sec"]))
        return _call_probe(gemini, clip, prompt)
    return ask


def run_style(gemini, draft_path: Path, story_doc: dict, *,
              preset: dict | None = None, windows: list[dict] | None = None,
              labels: list[dict] | None = None,
              dialogue: list[dict] | None = None,
              duration: float | None = None,
              band: tuple[float, float] | None = None,
              timeline: list[dict] | None = None,
              probe_ask=None,
              log=print) -> tuple[dict, dict]:
    """Stage 4 실행 → (style 문서, 감사 기록). 소진 시 프리셋 폴백 — 렌더는 항상 간다.
    timeline 을 주면 라벨은 **앵커 어휘**(label_events)로 받고 앵커된 라벨은 프로브로 프레임을
    맞춘다(probe_ask 미지정 = draft 를 잘라 Flash 에 묻는 기본 구현 · None 이 아닌 가짜를 주면
    테스트). timeline 이 없으면 종전(절대초) 그대로."""
    preset = dict(preset if preset is not None else RECAP_PRESET)
    if band is None:
        # 유도 없는 매직 넘버를 두면 밴드를 안 넘기는 호출부가 조용히 검정 밴드를
        # 뚫는다(적대 리뷰 M2) — 프리셋 기하에서 잰다
        from app.v3.finalize import design_from_style, video_band_ratio
        band = video_band_ratio(design_from_style(preset))
    n_beats = len(story_doc.get("beats") or [])
    audit: dict[str, Any] = {"attempts": [], "input": "draft_video",
                             "sample_fps": STYLE_SAMPLE_FPS}
    events = label_events(dialogue, timeline) if timeline is not None else None
    clips = edited_clip_windows(timeline) if timeline is not None else None
    if events is not None:
        audit["label_events"] = len(events)
    styled: dict | None = None
    reject_note = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = build_style_prompt(preset, story_doc, reject_note,
                                    dialogue=dialogue, windows=windows,
                                    labels=labels, band=band, events=events)
        log(f"  [v3/style] Flash vision 요청 (시도 {attempt + 1}/{1 + MAX_REASKS}, "
            f"draft {STYLE_SAMPLE_FPS:g}fps 표본)")
        t0 = time.time()
        problems: list[str] = []
        notes: list[str] = []
        try:
            resp = _call_style_model(gemini, draft_path, prompt)
            styled, problems, notes = validate_style_response(
                resp, n_beats, band=band, labels=labels, preset=preset,
                duration=duration, events=events, clips=clips)
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
        styled = {"design": {}, "beats": [], "labels": [],
                  "notes": "재질의 소진 — 프리셋 폴백"}
        audit["fallback"] = True

    # ── 라벨 프로브 — 앵커된 라벨만(절대초 폴백 라벨은 그대로) ──
    _labels = list(styled.get("labels") or [])
    if clips is not None and any(lb.get("anchor") for lb in _labels):
        ask = probe_ask or _default_label_probe(gemini, draft_path, log=log)
        anchored = [lb for lb in _labels if lb.get("anchor")]
        others = [lb for lb in _labels if not lb.get("anchor")]
        probed, paudit = probe_labels(anchored, clips, ask=ask, log=log)
        styled["labels"] = probed + others
        audit["label_probes"] = paudit

    design = {**preset, **styled["design"]}
    doc = {
        "schema": "v3_style/v1",
        "design": design,
        "diff": style_diff(preset, styled["design"]),
        "v3_style": {"beats": styled["beats"],
                     "labels": styled.get("labels") or [],
                     "notes": styled["notes"]},
    }
    audit["diff_keys"] = sorted(doc["diff"])
    return doc, audit
