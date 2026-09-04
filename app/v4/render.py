"""V4-M6/M7 §2~§4 — 초벌(11:draft) · 스타일(11:style) · 최종(11:render) · 검증(11:validate).

계약 정본 `docs/v4/M6-interfaces.md` §2·§3·§4. 승인 편마다 도는 네 조각이고, 편집
재료(§1 `app/v4/resources.py`)는 이 모듈의 밖이다 — 여기는 **재료를 받아 mp4 를 낸다**.

## 이 모듈이 v3 를 부르는 방식 (§0 재사용 방침)

v3 렌더 경로(`finalize.render_final`)는 **실제로 도는 화면 문법**이다(밴드 기하·화자
색·로고·번인 자막 회피·뮤트 창이 전부 그 안에 있다). v4 는 그것을 **부른다** — 다시
짓지 않는다. 그래서 이 파일은 대부분 얇은 어댑터고, 자체 코드를 가진 곳은 딱 둘이다:

  ① **초벌 렌더** — v3 `stage4.render_draft` 는 `-i 원본` 에 `[0:v]trim` 을 매달아
     **소스 전체를 디코드한다**(3시간 소재면 3시간). v4 는 클립별 **입력 seek**
     (`-ss/-t` per input)으로 만든다. v3 는 동결 표면이라 고칠 수 없어 v4 가 자체
     함수를 갖되 **필터그래프 어휘는 그대로 베꼈다**(scale·volume=0·concat) —
     화면이 달라지면 그 위에서 내리는 스타일 판정이 달라진다.
  ② **스타일 호출** — v3 `run_style` 은 `media_resolution` 인자를 받지 않는다
     (v3 는 미지정 = 실측상 LOW). 운영자 결정 O9 는 HIGH 다. 그래서 **호출만**
     `app/v4/video.call_video(..., media_resolution="HIGH")` 로 바꾼 얇은 대체 경로를
     갖고, 프롬프트·검증기·프리셋(`stage4.build_style_prompt`·
     `stage4.validate_style_response`·`stage4.get_style_preset`)은 v3 것을 그대로
     부른다. 여기서 프롬프트를 베끼면 두 파이프라인의 화면 문법이 갈린다.

🛑 **720p 와 HIGH 는 한 세트다**(기획서 §2-G · 운영자 결정 O9). 480p 소재에 HIGH 를
쓰는 것이 가장 나쁜 조합이다 — 없는 정보에 4배 요금을 낸다. 초벌이 720p 인 것과
스타일이 HIGH 인 것은 **같은 결정에서 나온 한 쌍**이라, 한쪽만 되돌리면 안 된다.

## 산출 이름 (§2·§3·§4)

    1위   draft_720.mp4   · style.json   · shorts.mp4   · validation.json
    2위↓  draft_720_2.mp4 · style_2.json · shorts_2.mp4 · validation_2.json

🛑 최종본 이름은 **`shorts.mp4`** 다(v3 기본값 `final_1080x1920.mp4` 가 아니라).
현지화가 그 이름을 읽는다 — `app/localize.RENDER_OUTPUT` 을 **import 해서** 쓴다
(기획서 §6). 값을 여기 다시 적으면 언젠가 한쪽만 고쳐져 컷오버가 조용히 깨진다.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from app.localize import RENDER_OUTPUT
from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.modules.grid import schemas as grid_schemas
from app.v3 import finalize, stage4
from app.v3.seq_analyze import MAX_REASKS
from app.v4 import proxy as proxy_mod
from app.v4 import video as video_mod

# ── §2 초벌 ────────────────────────────────────────────────────────────────

DRAFT_HEIGHT = 720          # 운영자 결정 O9 (v3 는 480). HIGH 와 한 세트다 — 모듈 독스트링.
DRAFT_FPS = 30.0            # O9. 프록시(`proxy.PROXY_FILE_FPS`)와 같은 자다.
# 인코딩 인자는 v3 `stage4.render_draft` 그대로다 — 초벌은 납품물이 아니라 **스타일
# 판정 재료**라 화질보다 속도다. 720p 로 올라간 것은 O9 뿐이고 crf·preset 은 안 건드렸다.
DRAFT_CRF = 28
DRAFT_PRESET = "ultrafast"
DRAFT_AUDIO_CHANNELS = 1
# 이름은 기하에서 파생한다(§2 의 `draft_720.mp4`) — 상수를 손으로 또 적으면 높이를
# 바꿨을 때 이름만 옛 기하로 남는다(`proxy.proxy_path_for` 와 같은 규율).
DRAFT_STEM = f"draft_{int(DRAFT_HEIGHT)}"

# ── §3 스타일 ──────────────────────────────────────────────────────────────

STYLE_MEDIA_RESOLUTION = "HIGH"           # 운영자 결정 O9 — 이 모듈의 존재 이유 ②
STYLE_SAMPLE_FPS = stage4.STYLE_SAMPLE_FPS   # 6.0 — v3 값을 **import** 한다(복제 금지)
# `video.py` 상수 주석이 이 자리를 이름으로 지목한다("짧은 출력을 받는 단계
# (6b 1024 · 11:style 8192)는 부르는 쪽이 줄인다"). v3 `_call_style_model` 도 8192 다.
# 절단(MAX_TOKENS)은 조용하지 않다 — `call_video` 가 크게 로그하고, 파싱 실패는
# 아래 루프가 **반려 재료**로 받는다.
STYLE_MAX_OUTPUT_TOKENS = 8192
# 🛑 이름이 `checkpoint_style.json` 이 **아니다**. 그 이름은 E15 연출 어휘
# (texts·title_fixed·title_segments·subtitle_styles)를 담는 계약 파일이고 현지화
# E16 이 그 키들을 읽는다. 여기 쓰는 문서는 v3 Stage 4 어휘(design·beats·labels·
# diff·notes)라 **모양이 다르다** — 계약 이름에 다른 모양을 얹는 것이 기획서가
# 금지한 바로 그 사고이고, `scripts/v4_contract_diff.py` 가 실제로 위반으로 잡는다.
# 그래서 v3 와 **같은 모양을 같은 이름**(`style.json`)으로 쓴다. 결과로 v4 잡에는
# `checkpoint_style.json` 이 없고, E16 은 연출을 안 켠 채널과 똑같이 지나간다
# (조용한 오작동이 아니라 정직한 부재). ⚠ v4 라벨은 화면에 한국어를 그리므로 JP
# 현지화에는 여전히 구멍이다 — 이 어휘를 E15 로 잇는 것이 남은 별건이다.
STYLE_STEM = "style"

# ── §4 최종·검증 ───────────────────────────────────────────────────────────

FINAL_NAME = RENDER_OUTPUT                # "shorts.mp4" — 현지화가 읽는 이름(기획서 §6)
VALIDATION_STEM = "validation"
# O9 는 최종본을 1080×1920 **30fps** 로 못박는다. 종전 `renderer.render_short` 는 argv 에
# `-r` 이 없어 출력 fps 가 **소재를 따라갔다**(24fps 소재 → 24fps 쇼츠). 렌더러에
# `output_fps` 가산 인자를 더해(기본 None = v1 argv 바이트 동일) 이 값을 싣는다 —
# 그래서 이 상수는 **경고 문구가 아니라 실제로 강제되는 값**이다.
FINAL_FPS = 30.0

__all__ = [
    "DRAFT_HEIGHT", "DRAFT_FPS", "DRAFT_CRF", "DRAFT_STEM",
    "STYLE_MEDIA_RESOLUTION", "STYLE_SAMPLE_FPS", "STYLE_MAX_OUTPUT_TOKENS",
    "FINAL_NAME", "FINAL_FPS",
    "render_paths", "draft_command", "render_draft",
    "run_style", "render_final", "stage_docs_for_validate", "run_validate",
]


# ── 산출 경로 ──────────────────────────────────────────────────────────────

def variant_suffix(variant: int) -> str:
    """1위는 접미사가 **없다**(v1 이름 그대로 — 현지화·편집실이 그것을 읽는다).

    2위↓만 `_2`·`_3`. 순수. 0·음수·정수 아님은 크게 실패한다 — 조용히 1 로 떨어지면
    2위 편이 1위의 산출을 덮어쓴다."""
    if not isinstance(variant, int) or isinstance(variant, bool) or variant < 1:
        raise ValueError(f"variant 는 1 이상의 정수다: {variant!r}")
    return "" if variant == 1 else f"_{variant}"


def render_paths(output_dir: Path | str, variant: int = 1) -> dict[str, Path]:
    """편별 산출 경로 — `{draft, style, final, validation}`. 순수.

    이름 규약은 계약 §2~§4 그대로다. **최종본만 v1 이름**(`shorts.mp4`)이고 나머지
    셋은 v3 이름을 잇는다 — 최종본은 현지화가, 스타일은 E16 이 읽는다."""
    out = Path(output_dir)
    sfx = variant_suffix(variant)
    final_stem, final_ext = os.path.splitext(FINAL_NAME)
    return {
        "draft": out / f"{DRAFT_STEM}{sfx}.mp4",
        "style": out / f"{STYLE_STEM}{sfx}.json",
        "final": out / f"{final_stem}{sfx}{final_ext}",
        "validation": out / f"{VALIDATION_STEM}{sfx}.json",
    }


# ── §2 초벌 — 클립별 입력 seek ─────────────────────────────────────────────

def draft_command(video_path: Path | str, timeline: list[dict], out_path: Path | str,
                  *, height: int = DRAFT_HEIGHT, fps: float = DRAFT_FPS,
                  crf: int = DRAFT_CRF) -> list[str]:
    r"""초벌 ffmpeg argv. **순수** — 테스트가 입력 seek 을 이 문자열로 고정한다.

    🛑 v3 와 다른 것은 **입력 쪽 한 줄**이다: 클립마다 `-ss <시작> -t <길이> -i <소스>`
    로 따로 연다. v3 는 소스를 한 번만 열고 `[0:v]trim` 으로 잘라서, 뒤쪽 클립 하나
    때문에 그 앞 전체를 디코드한다(3시간 소재면 3시간). 입력 seek 이면 **소요가 소재
    길이가 아니라 클립 길이에 비례한다.**

    ⚠ `-to` 가 아니라 `-t`(길이)를 쓴다 — 입력 옵션 `-to` 는 `-ss` 와 함께 쓸 때 기준이
    판본에 따라 헷갈리는 자리다. 길이는 어느 판본에서도 한 가지 뜻이다.

    필터 어휘는 v3 `stage4.render_draft` 를 그대로 베꼈다(`scale=-2:H` · 뮤트 클립
    `volume=0` · `concat=n=N:v=1:a=1`) — 초벌 화면이 달라지면 그 위에서 내리는 스타일
    판정(crop·pop·라벨 배치)이 달라진다. 더한 것은 `fps=` 하나이고 그것은 O9(30fps)
    때문이다(`proxy.build_proxy` 가 쓰는 것과 같은 어휘 `scale=…,fps=…` + `-fps_mode cfr`).

    ⚠ 입력 seek 은 각 입력의 PTS 를 0 부터 다시 시작하지 않으므로 `setpts=PTS-STARTPTS`
    는 **여전히 필요하다**(v3 와 같은 이유).
    """
    if not timeline:
        raise ValueError("timeline 이 비어 있다 — 초벌을 만들 수 없다")
    ffmpeg = find_ffmpeg_command("ffmpeg")
    src = str(Path(video_path).resolve())

    inputs: list[str] = []
    filters: list[str] = []
    parts: list[str] = []
    for i, c in enumerate(timeline):
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        dur = e - s
        if s < 0 or dur < video_mod.MIN_CLIP_SEC:
            # 보낼 수 없는 것에 크게 실패한다(`video.MIN_CLIP_SEC` 와 같은 규율) —
            # 길이 0 짜리 구간은 프레임이 한 장도 없어 concat 이 조용히 짧아진다.
            raise ValueError(
                f"초벌 클립[{i}] 구간이 물리적으로 렌더 불가: {s:.3f}~{e:.3f}s "
                f"(최소 {video_mod.MIN_CLIP_SEC}s)")
        inputs += ["-ss", f"{s:.3f}", "-t", f"{dur:.3f}", "-i", src]
        filters.append(
            f"[{i}:v]setpts=PTS-STARTPTS,scale=-2:{int(height)},fps={float(fps):g}[v{i}]")
        vol = "" if c.get("use_original_audio", True) else ",volume=0"
        filters.append(f"[{i}:a]asetpts=PTS-STARTPTS{vol}[a{i}]")
        parts.append(f"[v{i}][a{i}]")
    n = len(timeline)
    filters.append("".join(parts) + f"concat=n={n}:v=1:a=1[vout][aout]")

    return [ffmpeg, "-y", *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", DRAFT_PRESET, "-crf", str(int(crf)),
            "-c:a", "aac", "-ac", str(DRAFT_AUDIO_CHANNELS), str(out_path)]


def render_draft(video_path: Path | str, timeline: list[dict], out_path: Path | str,
                 *, height: int = DRAFT_HEIGHT, fps: float = DRAFT_FPS,
                 crf: int = DRAFT_CRF, log=print) -> dict:
    """edit_plan 컷만 이어붙인 저사양 중립 캔버스(720p/30fps · O9). → 비용 실측.

    반환 열쇠는 v4 규약(`proxy.build_proxy`)을 따른다:
    `{height, fps, clips, bytes, elapsed_sec, geometry}`. ⚠ v3 는 `elapsed` 였다 —
    v4 안에서 두 이름이 섞이면 감사 기록이 단계마다 다른 열쇠를 갖는다.

    v3 와 의도적으로 다른 것 셋(`proxy.build_proxy` 가 세운 규율 그대로):
      ① **임시 파일 → `os.replace`** — 인코딩 중에 죽으면 v3 는 반쪽 파일을 남기고
         다음 실행이 그것을 '존재하므로 재사용'한다. 원자 교체면 완성본만 존재한다.
      ② **만든 것의 기하를 다시 잰다** — 720p/30fps 가 아니면 크게 실패한다. 이
         파일로 계속 가면 스타일이 O9 와 다른 화면을 보고 판단한다.
      ③ ffmpeg stderr 를 삼키지 않는다(꼬리를 예외에 싣는다).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".{out_path.name}.part.mp4")
    cmd = draft_command(video_path, timeline, tmp_path, height=height, fps=fps, crf=crf)

    t0 = time.time()
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            "초벌 렌더 실패 — ffmpeg stderr 꼬리: "
            f"{(e.stderr or b'')[-400:].decode('utf-8', 'replace')}") from e
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, out_path)
    elapsed = round(time.time() - t0, 2)

    geo = proxy_mod.probe_geometry(out_path)
    ok = (geo and int(geo["height"]) == int(height)
          and abs(float(geo["fps"]) - float(fps)) <= proxy_mod.FPS_TOLERANCE)
    if not ok:
        raise RuntimeError(
            f"초벌 기하가 요구와 다르다: 만든 것={geo or '판독 불가'} · "
            f"요구={int(height)}p/{float(fps):g}fps ({out_path}) — 운영자 결정 O9")

    cost = {"height": int(geo["height"]), "fps": float(geo["fps"]),
            "clips": len(timeline), "bytes": out_path.stat().st_size,
            "elapsed_sec": elapsed, "geometry": geo}
    planned = sum(float(c["clip_end_sec"]) - float(c["clip_start_sec"])
                  for c in timeline)
    log(f"  [v4/draft] {out_path.name} — {cost['bytes'] / 1e6:.1f}MB · {elapsed:.1f}s · "
        f"클립 {len(timeline)}개 {planned:.1f}s · {int(height)}p/{float(fps):g}fps "
        f"(입력 seek — 소재 길이와 무관)")
    return cost


# ── §3 스타일 — media_resolution=HIGH ──────────────────────────────────────

def _call_style_high(gemini: Any, draft_path: Path, prompt: str, *,
                     sample_fps: float, media_resolution: str,
                     max_output_tokens: int, thinking_level: str | None,
                     log=print) -> tuple[Any, dict]:
    """v3 `stage4._call_style_model` 의 자리를 대신한다 — **호출만** 다르다.

    다른 것은 둘뿐이다: `media_resolution`(O9 = HIGH)과, 호출을 `video.call_video` 가
    한다는 것(E11 재시도 분류·MAX_TOKENS 절단 로그·usage 기록이 그 안에 있다).

    🛑 **초벌 핸들은 여기서 지운다.** 소재 프록시 핸들은 6·6b·8·10a 가 공유하므로
    `proxy.release_handle` 을 단계 안에서 부르면 안 되지만(그 모듈 독스트링), 초벌
    핸들은 소비자가 이 호출 하나뿐이다 — 안 지우면 편마다 서버 파일이 쌓여 다음
    실행의 할당량을 먹는다(v3 `_call_style_model` 도 finally 에서 지운다).
    """
    handle, up_meta = proxy_mod.upload_handle(gemini, Path(draft_path), log=log)
    try:
        resp, usage = video_mod.call_video(
            gemini, handle, prompt,
            sample_fps=sample_fps,
            media_resolution=media_resolution,
            max_output_tokens=max_output_tokens,
            thinking_level=thinking_level,
            # model 미지정 = Flash 슬롯. 스타일은 **연출 판단**이라 Pro 가 아니다
            # (CLAUDE.md 역할 표 · v3 `_call_style_model` 도 flash_model_name).
            log=log)
    finally:
        proxy_mod.release_handle(gemini, handle, log=log)
    usage = dict(usage or {})
    usage["upload"] = up_meta
    usage["media_resolution"] = media_resolution
    return resp, usage


def run_style(gemini: Any, draft_path: Path | str, story_doc: dict, *,
              preset: dict | None = None, windows: list[dict] | None = None,
              labels: list[dict] | None = None,
              band: tuple[float, float] | None = None,
              sample_fps: float = STYLE_SAMPLE_FPS,
              media_resolution: str = STYLE_MEDIA_RESOLUTION,
              max_output_tokens: int = STYLE_MAX_OUTPUT_TOKENS,
              thinking_level: str | None = None,
              log=print) -> tuple[dict, dict]:
    """Stage 4 스타일 → (style 문서, 감사 기록). v3 `stage4.run_style` 의 v4 판.

    **프롬프트·검증기·프리셋·문서 모양은 전부 v3 것이다**(`build_style_prompt`·
    `validate_style_response`·`style_diff`). 여기가 다시 짓는 것은 호출뿐이다 —
    모듈 독스트링 ②. 반려 루프도 v3 규약(`MAX_REASKS`)을 그대로 쓴다.

    ⚠ **소진 시 프리셋 폴백**(v3 규약 그대로 — "스타일 무변경 폴백, 렌더는 항상 간다").
    연출은 부가물이라 승인된 편의 발행을 막지 않는다(E15 규율). 폴백은 조용하지 않다 —
    로그와 `audit["fallback"]` 에 남는다.

    ⚠ 호출 실패(`VideoCallError`)도 반려 재료로 받는다. 다만 permanent(4xx)면 같은
    프롬프트를 두 번 더 태워도 결과가 같으므로 **즉시 폴백으로 간다** — v3 는 이
    예외가 루프 밖으로 새서 편이 통째로 죽었다.
    """
    preset = dict(preset if preset is not None else stage4.RECAP_PRESET)
    if band is None:
        # v3 와 같은 유도 — 밴드를 모르면 라벨이 검정 밴드를 뚫는다(v3 적대 리뷰 M2).
        band = finalize.video_band_ratio(finalize.design_from_style(preset))
    n_beats = len(story_doc.get("beats") or [])

    audit: dict[str, Any] = {
        "attempts": [], "input": "draft_video",
        "sample_fps": float(sample_fps),
        "media_resolution": media_resolution,      # O9 가 실제로 실렸는지의 근거
        "max_output_tokens": int(max_output_tokens),
    }
    styled: dict | None = None
    reject_note = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = stage4.build_style_prompt(preset, story_doc, reject_note,
                                           windows=windows, labels=labels, band=band)
        log(f"  [v4/style] Flash vision 요청 (시도 {attempt + 1}/{1 + MAX_REASKS} · "
            f"draft {float(sample_fps):g}fps 표본 · {media_resolution})")
        t0 = time.time()
        problems: list[str] = []
        notes: list[str] = []
        usage: dict | None = None
        fatal = False
        try:
            resp, usage = _call_style_high(
                gemini, Path(draft_path), prompt, sample_fps=sample_fps,
                media_resolution=media_resolution,
                max_output_tokens=max_output_tokens,
                thinking_level=thinking_level, log=log)
        except video_mod.VideoParseError as e:
            # 파싱 실패는 상시 모드다(이 레포 실측) — 크래시가 아니라 반려 재료다.
            usage, styled, problems = e.usage, None, [f"응답 JSON 파싱 실패: {e}"]
        except video_mod.VideoCallError as e:
            usage, styled, problems = e.usage, None, [f"영상 호출 실패: {e}"]
            fatal = (e.kind == "permanent")
        else:
            styled, problems, notes = stage4.validate_style_response(
                resp, n_beats, band=band, labels=labels, preset=preset)
        audit["attempts"].append({
            "attempt": attempt + 1, "elapsed_sec": round(time.time() - t0, 3),
            "problems": problems, "notes": notes, "usage": usage})
        if styled is not None:
            for n in notes:
                log(f"    · {n}")
            break
        log(f"  [v4/style] 반려 — 사유 {len(problems)}건")
        for p in problems[:15]:
            log(f"    · {p}")
        if fatal:
            log("  [v4/style] ⚠ permanent 실패 — 같은 프롬프트를 다시 태우지 않는다")
            break
        reject_note = "\n".join(f"- {p}" for p in problems[:15])

    if styled is None:
        log("  [v4/style] ⚠ 재질의 소진 — 프리셋 그대로(스타일 무변경 폴백)")
        styled = {"design": {}, "beats": [], "labels": [],
                  "notes": "재질의 소진 — 프리셋 폴백"}
        audit["fallback"] = True

    doc = {
        "schema": "v3_style/v1",          # ⚠ v3 와 **같은 스키마**다 — 렌더 어댑터
                                          #   (`finalize.render_final`)가 이 모양을 읽는다.
        "design": {**preset, **styled["design"]},
        "diff": stage4.style_diff(preset, styled["design"]),
        "v3_style": {"beats": styled["beats"],
                     "labels": styled.get("labels") or [],
                     "notes": styled["notes"]},
    }
    audit["diff_keys"] = sorted(doc["diff"])
    audit["reasks_used"] = len(audit["attempts"]) - 1
    log(f"  [v4/style] 완료 — 프리셋 diff {len(doc['diff'])}키 · "
        f"라벨 {len(doc['v3_style']['labels'])}개")
    return doc, audit


# ── §4 최종 ────────────────────────────────────────────────────────────────

def render_final(*, video_path: Path | str, plan: dict, style_doc: dict,
                 segments: list[dict], resources: dict, story_doc: dict,
                 output_dir: Path | str, variant: int = 1,
                 span_times: dict[str, float] | None = None,
                 log=print) -> tuple[Path, dict]:
    """v3 `finalize.render_final` 을 **부른다** — 이 함수가 하는 일은 이름 하나다.

    🛑 `out_name` 을 넘기는 것이 전부이자 계약이다. v3 기본값(`final_1080x1920.mp4`)
    으로 나가면 현지화가 최종본을 못 찾는다(`app/localize.RENDER_OUTPUT` = `shorts.mp4`).

    `output_fps`(O9 = 30) 도 함께 넘긴다 — 렌더러 가산 인자라 v3·v1 호출은 안 움직인다.
    만든 것의 fps 를 다시 재서 다르면 **크게 실패한다**(경고가 아니다): 지정한 값이
    안 먹었다는 뜻이고, 30fps 아닌 쇼츠가 조용히 발행되면 안 된다.
    """
    paths = render_paths(output_dir, variant)
    out_path, cost = finalize.render_final(
        video_path=Path(video_path), plan=plan, style_doc=style_doc,
        segments=segments, resources=resources, story_doc=story_doc,
        output_dir=Path(output_dir), out_name=paths["final"].name,
        output_fps=FINAL_FPS, span_times=span_times, log=log)

    geo = proxy_mod.probe_geometry(out_path)
    cost["variant"] = int(variant)
    cost["geometry"] = geo
    if geo is None:
        log(f"  [v4/render] ⚠ 최종본 기하를 재지 못했다 — {out_path.name}")
    elif abs(float(geo["fps"]) - FINAL_FPS) > proxy_mod.FPS_TOLERANCE:
        raise RuntimeError(
            f"최종본이 {geo['fps']:g}fps 다 — O9 는 {FINAL_FPS:g}fps 를 못박는다 "
            f"({out_path}). `output_fps` 를 넘겼는데 안 먹었다는 뜻이다.")
    return out_path, cost


# ── §4 검증 어댑터 ─────────────────────────────────────────────────────────

# v3 `run_validate` 가 stage1/stage2 문서에서 **실제로 읽는 것**은 둘뿐이다(코드 확인):
#   · `check_exception_overlap` → `stage1_doc["exception_sector"]`
#   · `check_tts_conflicts` → `story.build_span_index(stage2_doc, grid)`
# 그래서 그 둘만 채운다. 없는 것을 그럴듯하게 채우면 다음 사람이 그것을 계약으로 읽는다.
_SPAN_KEYS = ("is_audio", "importance", "audio_script", "text_source",
              "heard_text", "conf", "scene_script")


def stage_docs_for_validate(candidates_doc: dict, span_index: dict[str, dict],
                            grid: dict) -> tuple[dict, dict]:
    """v4 산출 → v3 `finalize.run_validate` 가 읽는 두 문서. 순수·결정적.

    ① **stage1_doc** — `{"exception_sector": {키: {"start": "HH:MM:SS.mmm", "end": …}}}`.
       🛑 **시각은 문자열이어야 한다.** `check_exception_overlap` 은
       `if zone.get("start") and zone.get("end")` 로 거르는데, intro 는 `start_sec=0.0`
       이라 숫자로 넣으면 **falsy 라서 통째로 빠진다** — 예고·인트로 유입 벨트가 조용히
       0 구역을 검사하게 된다(가왕쇼 6화 사고가 그 벨트의 존재 이유다).
       null 구역("신고 없음")은 싣지 않는다 — 검사할 구간이 없다는 뜻이다.

    ② **stage2_doc** — 시퀀스 1 · 청크 1 · meaning 1 에 **모든 span** 을 담는다(계약 §4).
       `build_span_index` 는 grid 에 있는 span 만 색인하므로 순서는 grid 순(`pos`)이다.

    🛑 **크게 실패하는 두 자리**(둘 다 조용하면 벨트가 사라진다):
      · `candidates_doc` 에 `exception_sectors` 키가 없다 → 배선 오류다. 빈 dict 로
        넘기면 유입 검사가 '구역 0개 = 위반 0건'이 되어 **항상 통과**한다.
      · `span_index` 에 격자에 없는 span id 가 있다 → `build_span_index` 가 조용히
        버리고, 그 span 은 TTS 충돌 검사에서 통째로 빠진다(다른 격자의 산출이라는
        신호이기도 하다 — `bridge.build_span_index` 의 stale 규율과 같다).

    ⚠ **되싣지 못하는 것 둘**: `meaning_content`·`mood` 는 v3 가 **meaning 노드**에서
    읽는데 meaning 이 하나뿐이라 span 마다 다른 값을 복원할 수 없다(계약 §4 의 모양이
    그렇다). `run_validate` 의 어떤 검사도 그 둘을 보지 않아 판정에는 영향이 없다 —
    왕복 테스트가 비교하는 열쇠에서도 빠진다. 다른 소비자가 생기면 그때 meaning 을
    span 마다 쪼개야 한다."""
    sectors = (candidates_doc or {}).get("exception_sectors")
    if sectors is None:
        raise ValueError(
            "candidates_doc 에 exception_sectors 가 없다 — 예고·인트로 유입 벨트가 "
            "구역 0개로 항상 통과하게 된다(6단계 산출을 그대로 넘겨라)")
    if not isinstance(sectors, dict):
        raise ValueError(f"exception_sectors 가 객체가 아니다: {type(sectors).__name__}")

    zones: dict[str, dict[str, str]] = {}
    for key in sorted(sectors):                      # 결정적 순서
        node = sectors[key]
        if node is None:
            continue                                  # "신고 없음" — 검사할 구간이 없다
        if not isinstance(node, dict):
            raise ValueError(f"exception_sectors.{key} 가 객체가 아니다: {node!r}")
        start, end = node.get("start_sec"), node.get("end_sec")
        try:
            s, e = float(start), float(end)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"exception_sectors.{key} 의 시각이 숫자가 아니다: "
                f"start_sec={start!r} end_sec={end!r}") from exc
        if not e > s:
            raise ValueError(f"exception_sectors.{key} 구간이 뒤집혔다: {s}~{e}")
        zones[key] = {"start": grid_schemas.format_ts(s),
                      "end": grid_schemas.format_ts(e)}
    stage1_doc = {"exception_sector": zones}

    grid_ids = {str(sp["id"]) for sp in (grid.get("span_candidates") or [])}
    unknown = sorted(set(span_index or {}) - grid_ids)
    if unknown:
        raise ValueError(
            f"span_index 에 격자에 없는 span id 가 있다: {unknown[:5]} "
            f"(총 {len(unknown)}개) — 그 span 은 TTS 충돌 검사에서 조용히 빠진다")

    spans = []
    for sid in sorted(span_index or {},
                      key=lambda s: (span_index[s].get("pos", 0),
                                     float(span_index[s]["t_in"]), s)):
        sp = span_index[sid]
        node: dict[str, Any] = {
            "span_id": sid,
            # 시각은 v3 문서 방언(문자열) 그대로다 — ms 로 양자화된다. 격자 시각은
            # 이미 ms 로 반올림돼 있어(`timegrid` round(...,3)) 왕복에서 잃는 것이 없다.
            "time": {"start": grid_schemas.format_ts(float(sp["t_in"])),
                     "end": grid_schemas.format_ts(float(sp["t_out"]))},
        }
        for k in _SPAN_KEYS:
            node[k] = sp.get(k)
        spans.append(node)
    stage2_doc = {"sequences": [{"chunks": [{"meanings": [{"spans": spans}]}]}]}
    return stage1_doc, stage2_doc


def run_validate(*, plan: dict, grid: dict, candidates_doc: dict,
                 span_index: dict[str, dict], segments: list[dict],
                 resources: dict, final_path: Path | None, tmp_dir: Path | str,
                 cast_names: list[str] | None = None,
                 gemini: Any = None, log=print) -> dict:
    """v3 `finalize.run_validate` 를 어댑터를 끼워 부른다 → `validation.json` 내용.

    ⚠ **hard_fail 은 예외가 아니다.** 이 함수는 판정을 문서로 돌려줄 뿐이고, "그 편만
    실패로 기록하고 다음 편으로 간다"(계약 §4)는 부르는 쪽(배선)의 일이다 — 여기서
    올려 버리면 한 편의 벨트 위반이 나머지 승인 편의 산출까지 못 만들게 한다."""
    stage1_doc, stage2_doc = stage_docs_for_validate(candidates_doc, span_index, grid)
    tmp = Path(tmp_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    doc = finalize.run_validate(
        plan=plan, grid=grid, stage1_doc=stage1_doc, stage2_doc=stage2_doc,
        segments=segments, resources=resources,
        final_path=Path(final_path) if final_path is not None else None,
        tmp_dir=tmp, cast_names=cast_names, gemini=gemini, log=log)
    log(f"  [v4/validate] hard_fail={doc['hard_fail']} · "
        f"경고 {doc['warnings_total']}건 · 스냅 {doc['snap_belt']['pct']}% · "
        f"예고 구역 {doc['exception_ingress']['zones']}개")
    return doc
