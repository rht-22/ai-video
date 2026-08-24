"""L3 — 번역 결과를 job 데이터 계층에 적용한다. **렌더는 이 파일들만 읽는다.**

원본: `localize_run.l3_apply` · `build_telop_ass`.

렌더 입력의 정본은 `checkpoint_story.json`(제목)·`subtitle_segments.json`(대사)·
`checkpoint_resources.json`(TTS)이고, `edit_plan.json` 은 발행(DB 제목 조회)용으로
함께 갱신한다(SPIKE §설계수정-1). 적용은 **항상 KO 백업 기준**이라 멱등이다.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.localize.style_texts import (
    STYLE_PLAN_NAME, apply_style_translation, load_json_or_none, style_plan_strings,
)
from app.localize.styles import style_ass_tags, style_margin_v

# ASR 환각성 초장 구간 방어 — 짧은 대사가 10초 넘게 떠 있으면 어색하다(파일럿 _74 실측 22s).
HALLUCINATION_SPAN_SEC = 8.0
HALLUCINATION_MAX_CHARS = 20
HALLUCINATION_CLAMP_SEC = 4.0


def clamp_hallucination(start: float, end: float, ja: str, *, user_timing: bool) -> float:
    """긴 구간에 짧은 텍스트면 표시를 4초로 줄인 end 를 돌려준다. 순수(테스트 대상).

    ⚠ 이 규칙은 **두 곳이 같이 써야 한다** — L3(실제 렌더)과 L5 의 ko_ja_pairs(검수 화면이
    보는 값). 원본은 같은 수식을 두 번 적어 뒀는데, 베낀 수식은 언젠가 어긋난다(E13 교훈).
    사용자 지정 타이밍이 있으면 건드리지 않는다 — 사람이 보고 정한 값이 이긴다."""
    if user_timing:
        return end
    if (end - start) > HALLUCINATION_SPAN_SEC and len(ja or "") <= HALLUCINATION_MAX_CHARS:
        return start + HALLUCINATION_CLAMP_SEC
    return end


def has_user_timing(tr: dict) -> bool:
    """검수자가 타이밍을 손댔는가. 순수(테스트 대상)."""
    return tr.get("start_sec") is not None or tr.get("end_sec") is not None


def _ass_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _fmt_ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


# ── JP 텔롭 트랙의 세로 자리 (E18, 2026-08-24) ─────────────────────────────
# 종전엔 **모든** 텔롭이 MarginV 720 고정이었다. 주석대로 우리 대사(430)·TTS(580)는
# 피하지만 **원본에 박힌 한국어 텔롭은 고려하지 않는다** — y=1920−720=1200 은 13:9 밴드
# (586~1334)의 82% 지점이라 하단 방송 텔롭과 정면으로 부딪친다(2026-08-24 SHOTCONE
# 실측: 일본어 두 줄이 한국어 텔롭 위에 그대로 얹혀 둘 다 못 읽는 화면이 나갔다).
#
# L2 는 텔롭마다 `position`(top/middle/bottom)을 이미 뽑고 있는데 이 함수가 안 쓰고
# 있었다. 그 값으로 **원본에서 먼 구역**을 고른다 — 새 검출 비용 0.
#   · bottom·middle → HIGH(밴드 위쪽). y=1920−1120=800, 박스 위끝 ≈650 →
#     제목 블록 아래끝(13:9 기준 566)에서 84px, 밴드 중앙(960)에서 160px 떨어진다.
#   · top·미상      → 기본(720). 원본이 위에 있으면 아래가 비어 있고, 미상은 종전 그대로다.
# ⚠ E17-2 의 번인 자막 회피는 이 트랙에 안 걸린다 — 그건 우리 대사·TTS 자막용이고,
#   판정도 '편 내내 같은 자리' 규칙이라 몇 초씩만 뜨는 방송 텔롭을 못 잡는다(별건).
TELOP_MARGIN_V = 720
TELOP_MARGIN_V_HIGH = 1120
TELOP_AVOID_POSITIONS = ("bottom", "middle")


def telop_margin_v(telop: dict, style: dict, play_res_y: int = 1920) -> int:
    """이 텔롭 줄의 이벤트 MarginV. 순수(테스트 대상).

    우선순위: **사람 오버라이드(style.y) > 원본 회피 > 스타일 기본값**.
    0 을 돌려주면 ASS 규약상 스타일 기본값(=TELOP_MARGIN_V)이 쓰인다 — 회피가 필요
    없는 줄은 종전 출력과 **한 글자도 같다**."""
    if style.get("y") is not None:
        return style_margin_v(style, play_res_y)
    if str(telop.get("position") or "").strip().lower() in TELOP_AVOID_POSITIONS:
        return round(TELOP_MARGIN_V_HIGH * play_res_y / 1920)
    return 0


def build_telop_ass(telop_data: list, translation: dict, font: str, out_path: Path) -> int:
    """방송 텔롭의 일본어 병기 트랙. 대사(430)·TTS(580)와 겹치지 않게 MarginV 720 기본,
    박스(BorderStyle=3) 위에 그린다. 세로 자리는 `telop_margin_v` 가 원본 위치를
    피해 고른다(위 상수 주석).

    ⚠ **박스로 원본을 덮는 길은 없다 — 실측으로 닫힌 선택지다**(E18, 2026-08-24,
      ffmpeg 6.1.1 실렌더). 두 가지를 재 봤다:
      · BackColour 를 반투명(&H78......) → 불투명(&H00......)으로 바꿔도 **프레임이
        픽셀까지 동일**했다. BorderStyle=3 의 박스는 **OutlineColour**(&H00000000,
        이미 불투명)로 그려지고 BackColour 는 그림자 색인데 Shadow=0 이라 안 쓰인다.
      · 박스 여백(Outline)을 5 → 12 → 24 로 넓혀도 박스 폭은 202 → 216 → 242px 인데
        원본 텔롭은 840px 였다(잔존 51.7k → 46.2k / 63k). 여백은 마스크가 아니다.
      ⇒ 겹침을 없애는 유일한 수단은 **자리를 옮기는 것**이다(`telop_margin_v`).
        정말 덮어야 한다면 전폭 마스크 도형을 따로 그려야 하는데, 그건 텔롭이 뜰 때마다
        영상 한 줄을 검게 지우는 것이라 사람이 결정할 일이다.

    (8/20) 검수 수정이 translation.telops 항목에 실은 줄 오버라이드 반영:
    · style {size,y,color,rotate} → 인라인 태그(\\fs·\\1c·\\frz — 계약 rotate 는 시계방향
      양수, ASS \\frz 는 반시계 양수라 부호 반전은 태그 조립(style_ass_tags)이 책임)
      + y 는 이벤트 MarginV(=(1−y)×1920, Telop 스타일이 하단 정렬이라 MarginV 방식).
    · start_sec/end_sec → L2b 재보정 타이밍보다 우선(사람이 보고 정한 값).
    태그는 _ass_escape 밖에서 조립한다 — 이스케이프가 { } 를 바꾼다."""
    telops = [t for t in telop_data if t.get("kind") == "broadcast_telop"] \
        if telop_data and "orig_index" not in telop_data[0] else telop_data
    by_index = {t["index"]: t for t in translation.get("telops", [])}
    lines = []
    for i, t in enumerate(telops):
        tr = by_index.get(t.get("orig_index", i))
        if not tr or not tr.get("use") or not tr.get("ja"):
            continue
        start = float(tr["start_sec"]) if tr.get("start_sec") is not None else float(t["start_sec"])
        end = float(tr["end_sec"]) if tr.get("end_sec") is not None else float(t["end_sec"])
        style = tr.get("style") or {}
        tags = style_ass_tags(style, 1920) if style else ""
        margin_v = telop_margin_v(t, style or {}, 1920)
        tag_block = f"{{{tags}}}" if tags else ""
        lines.append(f"Dialogue: 0,{_fmt_ts(start)},{_fmt_ts(end)},"
                     f"Telop,,0,0,{margin_v},, {tag_block}{_ass_escape(tr['ja'])}")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Telop,{font},52,&H00FFFFFF,&H00000000,&H78000000,-1,0,3,5,0,2,70,70,{TELOP_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    out_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def apply_segments(segments: list, translation: dict) -> tuple[list, int]:
    """대사 세그먼트에 번역·스타일·타이밍을 얹고 소프트 삭제를 반영한다. 순수(테스트 대상).

    (E6-0) use=false = 소프트 삭제 — 전사에서 빼면 ai-video 렌더에서도 빠진다."""
    dropped = set()
    for si, (seg, tr) in enumerate(zip(segments, translation["segments"])):
        if tr.get("use") is False:
            dropped.add(si)
            continue
        seg["text"] = tr["ja"]
        # 줄 스타일·타이밍(8/20 검수 수정): subtitle_segments.json 에 전사 —
        # ai-video(v3 캐시 규약) 렌더가 이 파일의 style 을 그대로 소비한다.
        if tr.get("style"):
            seg["style"] = tr["style"]
        user_timing = has_user_timing(tr)
        if tr.get("start_sec") is not None:
            seg["start_sec"] = float(tr["start_sec"])
        if tr.get("end_sec") is not None:
            seg["end_sec"] = float(tr["end_sec"])
        seg["end_sec"] = clamp_hallucination(
            float(seg["start_sec"]), float(seg["end_sec"]), tr["ja"], user_timing=user_timing)
    kept = [s for si, s in enumerate(segments) if si not in dropped]
    return kept, len(dropped)


def l3_apply(job: Path, backup: Path, translation: dict, telop_data: list,
             wcfg: dict, locale_cfg: dict, out_dir: Path):
    # 대사 자막 — 항상 KO 백업 기준으로 교체(멱등)
    segments = json.loads((backup / "subtitle_segments.json").read_text(encoding="utf-8"))
    segments, n_dropped = apply_segments(segments, translation)
    if n_dropped:
        print(f"[L3] 소프트 삭제: 대사 {n_dropped}줄 제외(use=false)")
    (job / "subtitle_segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")

    # 상단 제목 — 렌더 정본(checkpoint_story) + 발행용(edit_plan) 둘 다 (SPIKE §설계수정-1)
    story = json.loads((backup / "checkpoint_story.json").read_text(encoding="utf-8"))
    if "variants" in story:
        for v in story["variants"]:
            v["title_text"] = translation["top_title_ja"]
    else:
        story["title_text"] = translation["top_title_ja"]
    (job / "checkpoint_story.json").write_text(
        json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = json.loads((backup / "edit_plan.json").read_text(encoding="utf-8"))
    plan["layout"]["top_title"] = translation["top_title_ja"]
    plan["layout"]["bottom_label"] = wcfg["display"]
    (job / "edit_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # TTS cue 텍스트 (오디오 재합성은 L3t 가 한다 — 여기서는 텍스트만)
    resources = json.loads((backup / "checkpoint_resources.json").read_text(encoding="utf-8"))
    for c, tr in zip(resources.get("tts_cue_files", []), translation["tts_cues"]):
        c["cue"]["text"] = tr["ja"]
    (job / "checkpoint_resources.json").write_text(
        json.dumps(resources, ensure_ascii=False, indent=2), encoding="utf-8")

    # E16: AI 연출(style_plan) 문구 — 백업본에서 읽어 일본어로 바꿔 job 에 쓴다(다른 파일과 같은 규약).
    # 파일이 없으면(style_compose 안 켠 채널·구 런) 아무것도 하지 않는다.
    style_plan = load_json_or_none(backup / STYLE_PLAN_NAME)
    if style_plan is not None:
        t_ko, s_ko = style_plan_strings(style_plan)
        applied = apply_style_translation(style_plan, translation,
                                          font=locale_cfg.get("telop_font"))
        (job / STYLE_PLAN_NAME).write_text(
            json.dumps(applied, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[L3] 연출 텍스트 {len(t_ko)}건 · 제목 창 {len(s_ko)}건 일본어 적용"
              f"{' (폰트 ' + locale_cfg['telop_font'] + ')' if t_ko else ''}")

    n = build_telop_ass(telop_data, translation, locale_cfg["telop_font"], out_dir / "telops.ass")
    print(f"[L3] 적용 완료 — 대사 {len(segments)}건 · 텔롭 병기 {n}건 (telops.ass)")
