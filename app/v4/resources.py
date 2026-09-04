"""11:resources — 편집 재료. 승인 **편마다** 편집 계획·자막·cue·TTS 오디오를 만든다.

계약 정본 `docs/v4/M6-interfaces.md` §1(+ §6 배선). 원본은 v3 `pipeline._run_m3` 의
뒷부분(660~755줄)이고, **같은 순서로** 옮겼다.

    10 살붙이기 (편별 story 문서)
        │
        ▼  ① 뮤트 창 → ② 어절 자막 → ③ 반복 그물 → ④ 인명 →
    11:resources                ⑤ cue 좌표 → ⑥ TTS 합성 → ⑦ 창 초과 물리 트림
        │
        ▼  edit_plan.json · subtitle_segments.json · checkpoint_resources.json
    11:draft → 11:style → 11:render → 11:validate

## 순서가 계약인 이유

일곱 걸음은 서로의 산출을 먹는다 — 뮤트 창을 모르면 자막이 뮤트 구간까지 담고(원음이
없는데 자막만 뜬다), 반복 그물을 인명 교정 **뒤**에 돌리면 교정된 줄이 반복 판정을
빠져나가며, cue 좌표를 자막보다 먼저 잡으면 트리밍으로 사라진 창을 못 걸러낸다.
v3 가 실측으로 정착시킨 순서라 여기서 다시 정하지 않는다. 회귀 가드가 **호출 순서를
값으로** 고정한다(`tests/test_v4_resources.py`).

## 이 파일이 짓지 않는 것

· **초벌·스타일·최종 렌더**(11:draft~11:validate) — 계약 §2~§4, 같은 M6 의 다른 자리다.
· **story 문서** — 10단계(`flesh`)가 낸다. 여기서 오는 것은 `checkpoint_story.json` 의
  `variants[k]`(v4 가산 키 `title`·`beats`·`narration_cues`·`segments`·`span_ids` 전량)
  이고, 이 파일은 그것을 **읽기만** 한다.
· **목소리 선택** — cue 는 v3 와 같은 `ko_female`·`normal` 로 합성한다(아래 CUE_VOICE).
  채널·편집실 목소리(E11·E12)를 여기로 끌어오는 것은 M7 잔여다.

## 규율

1. **v3 함수를 부른다.** 클립 묶기(`assemble_edit_plan`)·자막 재단(`word_subtitles`)·
   cue 좌표(`finalize_cues`)·검사(`textcheck`)는 전부 v3 것이다. 베끼면 언젠가 한쪽만
   고쳐지고, 그때 `edit_plan.json`(편집실)과 `checkpoint_story.json`(현지화)이 서로
   다른 컷을 주장한다.
2. **벨트는 반드시 건다.** `verify_edit_plan(plan, grid)["pct"] == 100.0` 이 아니면
   `AssertionError` — 구조상 100% 여야 하고 아니면 코드 결함이다(v3 규약). 조립할 때와
   재료로 받을 때 **양쪽에서** 건다: 받은 plan 이 다른 격자에서 나온 낡은 캐시일 수 있다.
3. **조용한 드롭 금지.** 창을 못 찾은 cue(`start_sec is None`)·합성 실패·창 초과 트림은
   전부 건별로 stdout + audit 에 남는다.
4. **입력을 제자리에서 고치지 않는다.** `story_doc`·`span_index`·`grid`·`plan` 은 읽기
   전용이다. cue 는 `finalize_cues` 가 낸 **새 dict** 라 거기에만 쓴다(v3 도 그렇다).

## 실호출로만 알 수 있는 것

· `synthesize_tts_with_fit` 의 배속 재시도가 실제로 창 안에 들어오는 비율. 들어오지
  않으면 ⑦ 물리 트림이 발동하고 그 사실은 audit `tts_trimmed` 에 건별로 남는다.
· `gemini.shorten_text`(Flash) 축약 품질 — 없으면 `tts.py` 가 단순 절단으로 떨어지고
  문장이 중간에서 잘린다(v3 스모크 실측).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from app.v3 import assemble, textcheck

# ── 상수 ────────────────────────────────────────────────────────────────────
# cue 목소리·속도. v3 `pipeline._run_m3:697` 의 `finalize_cues(..., voice="ko_female",
# speed="normal")` 그대로다. ⚠ 채널·편집실 목소리(E11 라벨 · E12 `elevenlabs:` 접두)를
# 여기로 끌어오는 것은 M7 잔여 — 지금 기본값을 다른 값으로 바꾸면 v3 로 만든 편과
# 목소리가 갈린다.
CUE_VOICE = "ko_female"
CUE_SPEED = "normal"

# ⑦ 창 초과 물리 트림. 값은 v3 `pipeline._run_m3:722~731` 의 리터럴이다(v3 는 이름을
# 안 붙였다 — 동결 파일이라 그쪽에 상수를 심을 수 없어 이름은 여기서 붙인다).
#   · 관용 0.05s — fit 실측 오차까지 트림하면 멀쩡한 cue 를 매번 다시 인코딩한다.
#   · 페이드 0.12s — 창 끝에서 뚝 끊기면 다음 대사와의 경계가 튄다.
TRIM_TOLERANCE_SEC = 0.05
TRIM_FADE_SEC = 0.12

SCHEMA_RESOURCES = "v4_resources/v1"


# ── 편별 산출 경로 ──────────────────────────────────────────────────────────

def resource_paths(output_dir: Path, variant: int) -> dict[str, Path]:
    """편별 산출 경로(계약 §1). **1위는 v1 이름 그대로**다.

        variant 1 → edit_plan.json · subtitle_segments.json ·
                    checkpoint_resources.json
        variant n → edit_plan_{n}.json · subtitle_segments_{n}.json ·
                    checkpoint_resources_{n}.json

    1위가 v1 이름인 이유: 현지화(`app/localize/`)와 편집실이 그 이름을 읽는다 —
    `subtitle_segments.json` 은 L3 자막 적용의 정본이고 `checkpoint_resources.json` 은
    `tts_cue_files` 를 통해 편집실 미리듣기로 올라간다.

    ⚠ v3 훅 변형의 `*_variant_{k}.json` 과 **다른 이름**이다(그건 본편 불변의 훅 교체라
    같은 편의 파생본이고, 여기 2위↓ 는 **다른 편**이다).
    ⚠ `edit_plan_{n}.json` 은 계약 §1 의 두 파일 목록에는 없지만 v1 이 이미 쓰는 이름
    이다(`app/pipeline.py:5182`) — 같은 규칙을 따르라고 여기 함께 둔다. 이름을 새로
    지으면 편집실이 2위 편의 계획을 못 찾는다.
    """
    n = _check_variant(variant)
    suffix = "" if n == 1 else f"_{n}"
    out = Path(output_dir)
    return {
        "edit_plan": out / f"edit_plan{suffix}.json",
        "subtitle_segments": out / f"subtitle_segments{suffix}.json",
        "checkpoint_resources": out / f"checkpoint_resources{suffix}.json",
    }


def tts_cue_path(output_dir: Path, variant: int, cue_index: int) -> Path:
    """cue 오디오 파일 경로. **v1 이름 그대로**다(`app/pipeline.py:4506·5021`).

        variant 1 → tts_cue_{i}.mp3
        variant n → tts_{n}_cue_{i}.mp3

    편마다 이름이 갈려야 하는 이유는 ⑥ 의 실패 처리다 — 실패한 cue 는 **같은 이름의 옛
    mp3 를 지운다**. 편끼리 이름이 겹치면 2위 편의 실패가 1위 편의 멀쩡한 오디오를
    지운다.
    """
    n = _check_variant(variant)
    prefix = "tts_cue_" if n == 1 else f"tts_{n}_cue_"
    return Path(output_dir) / f"{prefix}{int(cue_index)}.mp3"


def _check_variant(variant: int) -> int:
    """편 번호는 1부터다. 0·음수는 조용히 1위 파일을 덮어쓸 수 있어 즉시 실패시킨다."""
    if isinstance(variant, bool) or not isinstance(variant, int) or variant < 1:
        raise ValueError(f"variant 는 1 이상의 정수여야 한다(1위 = v1 이름): {variant!r}")
    return variant


# ── 편집 계획 + 벨트 ────────────────────────────────────────────────────────

def _belt(plan: dict, grid: dict) -> dict:
    """시각 정합 벨트 — 위반이면 `AssertionError`(규율 2).

    벨트를 **한 곳**에 두는 이유: 조립할 때와 재료로 받을 때 판정이 갈리면, 낡은 캐시가
    조립 경로를 안 지나고 들어와 조용히 통과한다."""
    belt = assemble.verify_edit_plan(plan, grid)
    if belt["pct"] is not None and belt["pct"] < 100.0:
        # Stage 2 벨트와 같은 규율 — 구조상 100% 여야 하고 아니면 코드 결함이다.
        raise AssertionError(f"edit_plan 시각 정합 벨트 위반: {belt}")
    return belt


def build_edit_plan(story_doc: dict, span_index: dict[str, dict], grid: dict, *,
                    video_path: str, work_title: str) -> tuple[dict, dict]:
    """story 문서 → (`edit_plan` 문서, 벨트 기록). 순수 — 파일을 쓰지 않는다.

    조립 규칙(소스 불연속·뮤트 전환에서 자르기 · 라벨은 앵커 span 이 든 클립의
    `subtitle`)은 전부 `assemble.assemble_edit_plan` 것이다. `pipeline.story_clips` 가
    `checkpoint_story.json` 의 `clips` 를 만들 때 부르는 것과 **같은 함수**라, 같은
    입력이면 두 파일이 같은 컷을 주장한다.
    """
    plan = assemble.assemble_edit_plan(story_doc, span_index,
                                       video_path=video_path, work_title=work_title)
    return plan, _belt(plan, grid)


# ── 편집 재료 ───────────────────────────────────────────────────────────────

def build_resources(story_doc: dict, *, span_index: dict[str, dict], grid: dict,
                    plan: dict, research: Any, gemini: Any,
                    output_dir: Path, variant: int = 1,
                    fix_names: bool = False,
                    log=print) -> tuple[dict, list[dict], dict]:
    """편 하나의 편집 재료 → (`checkpoint_resources`, `subtitle_segments`, audit).

    순서는 v3 `pipeline._run_m3` 그대로다(모듈 독스트링 "순서가 계약인 이유"):

      ① `assemble.narration_windows`  → 내레이션이 점유하는 소스 구간(= 뮤트 창)
      ② `assemble.word_subtitles`     → 어절 자막(편집본 좌표 · 뮤트 창 밖만)
      ③ `textcheck.drop_repetition`   → 반복 환각 줄 제외(렌더 **전**에 뺀다)
      ④ `textcheck.check_names`       → 인명 오인식 경고 · `fix_names=True` 면 교정
      ⑤ `assemble.finalize_cues`      → cue 편집본 좌표(창을 못 찾은 것은 lost)
      ⑥ `tts.synthesize_tts_with_fit` → cue 합성(창 길이에 맞춘다)
      ⑦ 창 초과분 **물리 트림**       → 잘림 감수 오디오가 다음 대사를 밟는 것을 막는다

    세 산출 파일을 `resource_paths` 자리에 쓰고, 같은 내용을 돌려준다(부르는 쪽이
    run_log·렌더 입력으로 바로 쓰도록 — 방금 쓴 파일을 다시 읽지 않는다).

    ⚠ 인명 사전은 `research["cast_images"][].character_name` 이다(v3 와 같은 자리).
    ⚠ 합성 실패는 **계획을 막지 않는다**(fail-soft) — 다만 같은 이름의 옛 mp3 를 지우고
      `path=None` 으로 남긴다. 지우지 않으면 지난 실행의 낡은 대본이 최종 믹스에 그대로
      들어간다(v3 적대 리뷰 확정).
    """
    t0 = time.time()
    n = _check_variant(variant)
    paths = resource_paths(output_dir, n)

    # 규율 2 — 받은 plan 이 이 격자에서 나온 것인지 여기서 다시 본다.
    belt = _belt(plan, grid)
    timeline = plan["timeline"]

    # ① 내레이션 창 밖은 원음이 살아 있다 → 그 구간 대사는 자막을 낸다(M15).
    #    v3 와 같은 평탄화다 — 비트별 병합 창을 한 목록으로 모아 정렬만 한다.
    mute_windows = sorted(w for wins in assemble.narration_windows(story_doc).values()
                          for w in wins)

    # ② 어절 자막(편집본 좌표 · C6)
    segments = assemble.word_subtitles(timeline, span_index,
                                       grid.get("words") or [], mute_windows)

    # ③ 반복 환각 그물(순수 코드 · LLM 0콜) — 렌더 전에 뺀다
    segments, rep_warns = textcheck.drop_repetition(segments)

    # ④ 인명 오인식(경고 · 요청 시 교정)
    names = [c["character_name"] for c in (research or {}).get("cast_images") or []
             if c.get("character_name")]
    name_warns = textcheck.check_names(segments, names)
    if fix_names and name_warns:
        segments, name_fixes = textcheck.fix_names(segments, names)
    else:
        name_fixes = []
    if rep_warns:
        dropped_lines = {i for w in rep_warns if w["kind"] == "run"
                         for i in w.get("indexes") or []}
        log(f"  [v4/resources] ⚠ 반복 환각 {len(rep_warns)}건 — "
            f"{len(dropped_lines)}줄 제외 · 창 경고는 유지(사유 run_log)")
    if name_warns:
        log(f"  [v4/resources] ⚠ 인명 오인식 의심 {len(name_warns)}건: "
            + ", ".join(f"{w['token']}→{w['suggest']}" for w in name_warns[:3])
            + (" (교정 적용)" if name_fixes else " (경고만 — --fix-names 로 교정)"))
    _write_json(paths["subtitle_segments"], segments)

    # ⑤ cue 편집본 좌표. 창이 트리밍으로 사라진 cue 는 여기서 갈린다 —
    #    조용히 버리면 '내레이션이 왜 두 줄만 나가지'가 된다(규율 3).
    cues = assemble.finalize_cues(story_doc.get("narration_cues") or [], timeline,
                                  voice=CUE_VOICE, speed=CUE_SPEED)
    lost = [c for c in cues if c.get("start_sec") is None]
    cues = [c for c in cues if c.get("start_sec") is not None]
    for c in lost:
        log(f"  [v4/resources] ⚠ cue 드랍 — 창이 트리밍으로 사라졌다: {c['text'][:40]!r}")

    # ⑥⑦ TTS 합성 + 창 초과 물리 트림
    #    ⚠ 임포트를 늦추는 것은 v3 규약 승계다 — `tts.py` 는 env(ELEVENLABS_*)를 보고
    #      백엔드를 정하므로, 합성을 안 하는 실행까지 이 모듈을 끌어올 이유가 없다.
    from app.modules.tts import (
        active_backend,
        elevenlabs_disabled,
        synthesize_tts_with_fit,
    )
    tts_cue_files: list[dict] = []
    tts_failed: list[dict] = []
    tts_trimmed: list[dict] = []
    shorten = getattr(gemini, "shorten_text", None)
    for ci, cue in enumerate(cues):
        tts_path = tts_cue_path(output_dir, n, ci)
        try:
            # v1 과 같은 fit 합성 — 실측이 창(duration_sec)을 넘으면 다음 대사를 밟는다.
            # 배속 재시도는 tts.py 몫이고, 창 초과 시 축약은 v1 과 같이 Flash
            # (shorten_text)에 맡긴다 — shorten_fn 없이는 '단순 절단'이 문장을 중간에서
            # 잘라먹는다(v3 스모크 실측).
            final_text, actual = synthesize_tts_with_fit(
                cue["text"], tts_path, target_sec=float(cue["duration_sec"]),
                voice=cue["voice"], speed=cue["speed"], shorten_fn=shorten)
            cue["text"] = final_text
            cue["fit_actual_sec"] = round(actual, 3)
        except Exception as e:  # noqa: BLE001 — 합성 실패가 계획 산출을 막지 않는다
            log(f"  [v4/resources] ⚠ cue {ci} 합성 실패 — 계획만 유지: {e}")
            cue["fit_actual_sec"] = None
            # 이전 실행의 같은 이름 mp3 가 남아 있으면 낡은 대본이 최종 믹스에
            # 들어간다(v3 적대 리뷰 확정) — 지우고 경로도 비운다(렌더 필터가 걸러낸다).
            tts_path.unlink(missing_ok=True)
            tts_cue_files.append({"cue_index": ci, "path": None, "cue": cue})
            tts_failed.append({"cue_index": ci, "error": str(e)[:200]})
            continue
        # fit 소진 '잘림 감수' 오디오가 창을 넘으면 다음 대사를 밟는다(v3 리뷰 확정)
        # — 창 길이로 물리 트림(+페이드아웃)해 계약(창 안 오디오)을 강제한다.
        window = float(cue["duration_sec"])
        if cue["fit_actual_sec"] and cue["fit_actual_sec"] > window + TRIM_TOLERANCE_SEC:
            over = cue["fit_actual_sec"]
            _trim_to_window(tts_path, window)
            log(f"  [v4/resources] cue {ci} 실측 {over}s > 창 {window}s — 창 길이로 트림")
            tts_trimmed.append({"cue_index": ci, "actual_sec": over,
                                "window_sec": round(window, 3)})
            cue["fit_actual_sec"] = window
        tts_cue_files.append({"cue_index": ci, "path": str(tts_path), "cue": cue})

    backend = active_backend()
    resources = {"tts_cue_files": tts_cue_files, "tts_backend": backend}
    _write_json(paths["checkpoint_resources"], resources)
    _write_json(paths["edit_plan"], plan)

    stats = assemble.clip_stats(plan)
    audit = {
        "schema": SCHEMA_RESOURCES,
        "variant": n,
        # ⚠ audit 에서 실행마다 달라지는 값은 이것 하나다(결정성 가드가 이 열쇠를 뺀다).
        "elapsed_sec": round(time.time() - t0, 1),
        "tts_backend": backend,
        "tts_cues": len(tts_cue_files),
        "tts_failed": tts_failed,
        "tts_trimmed": tts_trimmed,
        "subtitle_segments": len(segments),
        "subtitle_repetition_warns": rep_warns,
        "subtitle_name_warns": name_warns,
        "subtitle_name_fixes": name_fixes,
        # 키 이름은 v3 것을 그대로 쓴다 — 하네스·집계가 run_log 에서 이 이름을 찾는다.
        "cues_lost_to_trim": [c["text"][:40] for c in lost],
        "time_alignment": belt,
        "clip_stats": stats,
        "paths": {k: str(v) for k, v in paths.items()},
    }
    fallback = elevenlabs_disabled()
    if fallback:
        audit["tts_fallback_reason"] = "elevenlabs_auth_expired"
        audit["tts_fallback_detail"] = fallback[:200]
    log(f"  [v4/resources] 완료 — 클립 {stats['clips']}개 {stats['total_sec']}s · "
        f"자막 {len(segments)}줄 · cue {len(tts_cue_files)}개({backend}) · "
        f"시각정합 {belt['pct']}%")
    return resources, segments, audit


def _trim_to_window(tts_path: Path, window: float) -> None:
    """창 길이로 물리 트림(+끝 페이드아웃). ffmpeg 인자는 v3 것 그대로다.

    같은 파일에 덮어쓸 수 없어(입력을 읽는 중이다) 임시 파일로 만든 뒤 `replace` 한다 —
    `replace` 는 같은 파일시스템에서 원자적이라 중간에 죽어도 반쪽 mp3 가 남지 않는다.
    실패는 `CalledProcessError` 로 그대로 올린다(창을 못 지킨 오디오를 조용히 싣지 않는다).
    """
    from app.modules.ffmpeg_utils import find_ffmpeg_command
    trimmed = tts_path.with_suffix(".trim.mp3")
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-i", str(tts_path),
         "-t", f"{window:.3f}",
         "-af", f"afade=t=out:st={max(0.0, window - TRIM_FADE_SEC):.3f}"
                f":d={TRIM_FADE_SEC}",
         str(trimmed)], check=True, capture_output=True)
    trimmed.replace(tts_path)


def _write_json(path: Path, doc: Any) -> None:
    """산출 기록. v4 의 다른 단계와 같은 통로(`job.write_json` — 원자적 기록)."""
    from app.modules import job
    job.write_json(path, doc)
