"""pipeline v4 배선 — 1~9단계 실구현 + 10~11 자리.

계약 `docs/v4/M2-interfaces.md` §1·§2(1~5) · `docs/v4/M3-interfaces.md` §5(6~9).

    1 init → 2 research → 3 자막 전사 → 4 probe(눈금·표본 fps) → 5 업로드(720p/30fps)
    → 6 후보 편성 → 6b 경계 정밀 → 6c 구간 검증 → 7 깔때기 → 8 시각 플래그 → 9 승인
    → 10 살붙이기 … 11:validate                    (10 이후는 M5~M7 — 여기서는 자리만)

v3 배선(`app/v3/pipeline.py:102~440`)이 원본이고, **그 배선이 남긴 사고를 고치는 것**이
이 파일의 존재 이유다. 고친 것 여섯을 여기 적어 둔다(각 지점에도 주석이 있다):

① **단계 판정은 `steps.should_run` 하나다.** v3 는 손으로 적은 멤버십 검사 7곳이었고
   집합이 전부 달라서 `--from-step story` 가 상류 캐시를 그대로 쓰고 `--from-step
   resources` 는 거꾸로 상류인 story 를 무효화했다(조사 gotcha 4). 이 파일에서 단계
   이름을 직접 비교하는 코드는 **한 줄도 없다** — 아래 `_invalidated` 한 곳이 전부고
   회귀 가드가 소스를 훑어 고정한다.
② **run_log 는 단계마다 즉시·원자적으로 쓴다**(`job.make_step_logger`). v3 는 `finally`
   한 곳뿐이라 SIGKILL·OOM 이면 그 실행의 감사 기록이 통째로 사라졌다(gotcha 1).
③ **캐시 히트도 기록한다**(`cached=True`). v3 는 캐시 히트가 무기록이라 run_log 에
   단계가 없을 때 '안 돌았다'인지 '캐시였다'인지 구분되지 않았다(gotcha 5).
④ **스킵도 기록한다**(`skipped=…`). v3 는 스킵 기록 방식이 제각각이었다(gotcha 15).
⑤ **research 도 `should_run` 을 탄다.** v3 는 research 에 무효화 경로가 아예 없어
   `checkpoint_research.json` 이 한 번 생기면 어떤 `--from-step` 으로도 안 돌았다.
⑥ **dotenv 를 진입점에서 먼저 로드한다.** 안 하면 렌더가 PATH 의 ffmpeg 8
   (-filter_complex_script 거부)로 떨어져 죽는다 — v3 M4 스모크에서 실제로 났다.

산출(기존 job 레이아웃 그대로 — M0 리플레이 하네스 로더가 자동 판별한다):

    run_log.json                 {schema, pipeline:"v4", milestone, job_id, input, provenance, steps[]}
    checkpoint_research.json     v1·v3 와 같은 모양(재사용)
    checkpoint_grid_words.json   단어 전사 캐시 — **가장 비싼 단계**
    checkpoint_probe.json        MediaInfo + 표본 fps 판정 근거
    grid.json                    정본 격자(words/scene_cuts/silence/arousal/span_candidates)
    checkpoint_upload.json       프록시 기하 + Files API 핸들
    checkpoint_candidates.json   6~9 가 **증분으로** 쌓는 한 파일(M1 계약 §8):
                                 몸통(6) + boundary(6b) + verify(6c) + funnel·rank(7)
                                 + flags(8) + approval(9)
    <제목>_16k.wav               전사·arousal 용 오디오

⚠ **6~9 의 절 규약**: 각 단계는 `STEP_SECTIONS` 가 정한 **자기 절만** 쓰고, 자기보다
하류인 절은 **버린다**(`_purge_after`). 지문만으로도 하류는 어차피 재계산되지만, 새
후보 목록 옆에 옛 후보의 승인 결과가 남아 있으면 그 파일은 거짓말을 하기 때문이다.
6b 는 예외적으로 top 의 `exception_sectors` 를 **덮지 않고** 자기 절에 정정본을 둔다 —
덮으면 6b 재실행이 이미 옮긴 경계를 중심으로 ±60 창을 잡아 원판정을 잃는다.
읽는 곳은 `sector_source()` 하나다.

⚠ **`input` 키는 계약이다** — `app/replay/exception_score.py:137` 이
`run_log["input"]["video_path"]` 의 파일명으로 레이블을 매칭한다. 빼먹으면 채점기가
v4 job 을 못 찾는다(그리고 아무 소리도 안 난다).
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from app.config import AppConfig
from app.modules import job
from app.modules.grid.arousal import compute_arousal
from app.modules.grid.audio import (
    SILENCE_MIN_SEC,
    SILENCE_NOISE_DB,
    detect_silence_intervals,
    load_pcm,
)
from app.modules.grid.scenecut import SCENE_THRESHOLD, detect_scene_cuts
from app.modules.grid.timegrid import (
    EDGE_MARGIN_SEC,
    MAX_UNVOICED_SEC,
    MIN_UNVOICED_SEC,
    SCHEMA_GRID,
    build_grid_doc,
    carve_spans,
    grid_snap_times,
)
from app.modules.grid.transcribe import (
    WHISPER_MODEL_NAME,
    _build_whisper_prompt,
    retranscribe_gaps,
    transcribe_words,
)
from app.modules.media_probe import MediaInfo, probe_media
from app.modules.speech import extract_audio_from_video
from app.modules.subtitle import parse_subtitle
from app.v4 import approve, fps as fps_mod
from app.v4 import (
    boundary as boundary_mod,
    candidates as cand_mod,
    flags as flags_mod,
    funnel as funnel_mod,
    verify as verify_mod,
)
from app.v4.steps import STEP_ORDER, V4_STEPS, parse_from_step, should_run, step_label

# run_log 의 pipeline 이름. 마일스톤마다 바뀐다(v3 는 v3_m1 → v3_m3 로 갔다) —
# `job.resume_run_log` 가 이름이 갈리면 resume 줄에 요청값을 남겨 보이게 한다.
# 🛑 마커는 **계약이지 진행 상황이 아니다.** 기획서 §6 의 바깥 계약 표가 정한 값은
# `"v4"` 하나이고, 현지화·오케스트레이터 어댑터·리플레이 로더가 이걸로 분기한다.
# v3 는 마일스톤마다 이름을 바꿨고(v3_m1 → v3_m3) 그래서 읽는 쪽이 접두 일치를 하거나
# 편마다 다른 값을 만나야 했다. 진행 상황은 **별도 키**로 남긴다.
PIPELINE_NAME = "v4"
PIPELINE_MILESTONE = "M3"      # 이 판이 어디까지 지었나(감사용 — 분기 근거가 아니다)

# v4 모델 정책 — v3 와 같은 자(2026-08-31 A/B 실측: 전 호출 Flash 3.7 로 저하 없음).
# 공유 모듈 기본값은 건드리지 않고 **진입점에서만** 덮는다(병행 구축 규약). env 로
# 노드별 재지정 가능(문제 시 Pro 복귀 손잡이).
V4_MODEL_DEFAULT = "gemini-3.7-flash"
V4_MODEL_ENV = "GEMINI_V4_MODEL"

# ── 표본 fps 사전검사의 텍스트 토큰 추정(계약 §2) ────────────────────────────
# 🛑 **추정이다.** 한국어 1자 ≈ 1토큰이 아니고 이 레포에 실측이 아직 없다. 방향은
# 보수적으로 잡는다 — 텍스트를 과대평가하면 영상 예산이 줄어 표본 fps 가 **내려가고**,
# 그건 안전한 실패(화질 손해)다. 반대로 과소평가하면 1,048,576 상한을 넘겨 요청이
# 400 으로 죽는데, 그때는 이미 720p/30fps 프록시 인코딩과 업로드를 태운 뒤다.
# 프롬프트 골격·장면 목록 같은 고정 몫은 `fps.TEXT_RESERVE_MIN`(30,000)이 덮는 것으로
# 본다 — 여기서 또 더하면 같은 여유를 두 번 빼게 된다.
# M8 실측 라운드에서 이 상수를 갈아낀다(`text_tokens_estimated: true` 가 그 표지다).
TEXT_TOKENS_PER_CHAR = 1.5

# 6~11 단계가 아직 없다는 사실과 **언제 생기는지**를 함께 남긴다. 계약 §7 이
# "전부 M3~M7" 이라 했고, 어느 마일스톤인지는 M1 계약 §5 의 ABSORB 표(그 단계가
# 부르기 시작하는 함수의 마일스톤)에서 왔다. 배선을 짓는 마일스톤이 자기 줄을 지운다.
NOT_IMPLEMENTED_MILESTONE: dict[str, str] = {
    "flesh": "M5", "detail": "M5",
    "11:resources": "M6", "11:draft": "M6",
    "11:style": "M7", "11:render": "M7", "11:validate": "M7",
}

# 실제로 배선된 단계. `run_v4` 안의 `handlers` 와 **같은 집합**이어야 하고 그 사실을
# 배선이 스스로 확인한다(아래) — 표를 두 곳에 적으면 언젠가 갈린다(독스트링 ①).
# 회귀 가드는 이 상수를 읽는다: 마일스톤이 올라갈 때 테스트를 고칠 일이 없어야 한다.
IMPLEMENTED_STEPS: frozenset[str] = frozenset({
    "init", "research", "transcribe", "probe", "upload",
    "candidates", "boundary", "verify", "funnel", "flags", "approve",
})


def _check_step_coverage() -> None:
    """단계 표의 모든 이름이 **배선됐거나 미구현으로 선언됐는가**. 임포트 시점에 터진다.

    v3 는 단계를 더할 때 배선을 빠뜨려도 그 자리에서 조용히 지나갔다 — 여기서는
    `V4_STEPS` 에 이름을 더한 순간 임포트가 죽는다(조용한 누락 금지).
    """
    both = IMPLEMENTED_STEPS & set(NOT_IMPLEMENTED_MILESTONE)
    if both:
        raise RuntimeError(f"배선됐는데 미구현으로도 선언된 단계: {sorted(both)}")
    missing = [s for s in V4_STEPS
               if s not in IMPLEMENTED_STEPS and s not in NOT_IMPLEMENTED_MILESTONE]
    if missing:
        raise RuntimeError(
            f"단계 표에 있는데 배선도 미구현 선언도 없다: {missing} — "
            "`IMPLEMENTED_STEPS` 에 넣거나 `NOT_IMPLEMENTED_MILESTONE` 에 마일스톤을 적어라")
    stray = sorted((IMPLEMENTED_STEPS | set(NOT_IMPLEMENTED_MILESTONE)) - set(V4_STEPS))
    if stray:
        raise RuntimeError(f"단계 표에 없는 이름이 배선 표에 있다: {stray}")


_check_step_coverage()


# ── 진입점 부수 준비 ────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """.env(FFMPEG_BIN=ffmpeg 7 고정 등)를 **진입점에서** 결정적으로 로드한다.

    v3 `pipeline.py:123~128` 과 같은 순서·같은 이유다: 종전에는 `gemini_client` 가
    로드될 때만 .env 가 실려서, style 캐시가 있어 그 모듈을 안 부르는 실행이면 렌더가
    PATH 의 ffmpeg 8(-filter_complex_script 거부)로 떨어져 죽었다(M4 스모크 재현).
    **우연 의존 금지** — 어느 단계를 돌든 같은 환경이어야 한다.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    except ImportError:
        pass


def _load_gemini_client():
    """Gemini 클라이언트 생성 + v4 모델 고정. 테스트가 이 함수를 갈아끼운다."""
    import dataclasses

    from app.modules.gemini_client import load_gemini_client
    client = load_gemini_client()
    model = os.environ.get(V4_MODEL_ENV, V4_MODEL_DEFAULT)
    client.config = dataclasses.replace(client.config, model_name=model,
                                        flash_model_name=model)
    return client


# ── 캐시 지문 사이드카 ──────────────────────────────────────────────────────
# 체크포인트 파일 자체는 v1·v3 와 **같은 모양**을 유지해야 하는 것들이 섞여 있어
# (checkpoint_research.json 은 공유 모양이다) 지문을 파일 안에 넣지 않고 옆에 둔다.
# `checkpoint_grid_words.json` → `checkpoint_grid_words.v4meta.json`.

def _meta_path(path: Path) -> Path:
    return path.with_name(path.stem + ".v4meta.json")


def _read_meta_fp(path: Path) -> str | None:
    meta = _meta_path(path)
    if not meta.exists():
        return None
    doc = job.read_json(meta)      # 깨졌으면 그대로 터진다(조용한 기본값 금지)
    return str(doc.get("fingerprint")) if isinstance(doc, dict) else None


def _write_meta(path: Path, fingerprint: str, **extra: Any) -> None:
    job.write_json(_meta_path(path), {"schema": "v4_cache_meta/v1",
                                      "of": path.name,
                                      "fingerprint": fingerprint, **extra})


def _cache_state(path: Path, fingerprint: str, invalidate: bool) -> tuple[bool, str]:
    """캐시를 쓸 것인가 → (쓴다, 사유). 순수(파일을 읽기만 한다).

    판정은 두 겹이다(계약 §1): ① `should_run` 이 무효화라고 하면 무조건 다시 만든다
    ② 아니면 **자기 지문**으로 다시 본다. 지문 사이드카가 없는 옛 job 은 존재만으로
    재사용하되 그 사실을 사유에 남긴다 — 전사·리서치는 요금이 걸린 단계라 지문이
    없다고 다시 태우는 것이 더 나쁘고, 조용히 넘어가지 않는 것이 이 레포의 규율이다.
    """
    if invalidate:
        return False, "--from-step 무효화"
    if not path.exists():
        return False, "캐시 없음"
    was = _read_meta_fp(path)
    if was is None:
        return True, "지문 미기록(구 job) — 존재로 재사용"
    if was != fingerprint:
        return False, f"지문 불일치 {was} → {fingerprint}"
    return True, "지문 일치"


def _file_sig(path: Path | None) -> list[Any] | None:
    """지문 재료용 파일 서명 — (경로 이름, 바이트 수). 없으면 None.

    mtime 은 넣지 않는다 — 같은 파일을 복사만 해도 지문이 갈려 가장 비싼 전사가 매번
    다시 돈다. 이름+크기면 '다른 소재로 바뀐 것'은 잡고 '같은 파일'은 통과한다.
    """
    if path is None:
        return None
    p = Path(path)
    return [p.name, p.stat().st_size if p.exists() else None]


def _estimate_text_tokens(words: list[dict], research: dict | None) -> tuple[int, dict]:
    """전사·리서치 글자 수 → 텍스트 토큰 **추정**(계약 §2). 순수.

    반환 dict 는 근거 그대로다 — `text_tokens_estimated: true` 가 붙어 있고,
    실측이 생기면 이 함수와 `TEXT_TOKENS_PER_CHAR` 만 갈아끼우면 된다.
    """
    transcript_chars = sum(len(str(w.get("text") or "")) for w in words or [])
    r = research or {}
    research_chars = len(str(r.get("work_context") or "")) + \
        len(str(r.get("episodes_context") or ""))
    tokens = int(math.ceil((transcript_chars + research_chars) * TEXT_TOKENS_PER_CHAR))
    return tokens, {
        "text_tokens_estimated": True,
        "transcript_chars": transcript_chars,
        "research_chars": research_chars,
        "tokens_per_char": TEXT_TOKENS_PER_CHAR,
        "note": "실측 전 추정 — 프롬프트 골격 몫은 fps.TEXT_RESERVE_MIN 이 덮는다",
    }


# ── 6~9 후보 깔때기 — 한 파일에 증분으로 쌓는다(M1 계약 §8) ──────────────────
# `checkpoint_candidates.json` 하나에 6·6b·6c·7·8·9 가 자기 절만 얹는다. 6단계는 파일의
# **몸통 자체**(schema·candidates·exception_sectors…)를 만들므로 소유 절이 없다 — 다시
# 돌면 문서를 통째로 새로 쓰고, 그래서 하류 절이 자동으로 사라진다.
CANDIDATES_CKPT = "checkpoint_candidates.json"

# 단계 → 그 단계가 **소유한** 절. "누가 무엇을 쓰는가"의 정본은 이 표 하나이고,
# 하류 무효화(`_purge_after`)도 여기서 파생된다 — 단계마다 손으로 적으면 v3 처럼
# 일곱 갈래로 갈린다(모듈 독스트링 ①).
STEP_SECTIONS: dict[str, tuple[str, ...]] = {
    "candidates": (),                      # 몸통 — 절이 아니라 문서 전체
    "boundary": ("boundary",),
    "verify": ("verify",),
    "funnel": ("funnel", "rank"),
    "flags": ("flags",),
    "approve": ("approval",),
}


def _purge_after(step_name: str) -> tuple[str, ...]:
    """이 단계를 다시 쓸 때 **함께 버려야 하는** 하류 절. 순수·결정적.

    지문만으로도 하류는 어차피 재계산된다(각 절의 지문이 상류 지문을 재료로 삼는다).
    그런데도 지우는 이유는 **파일을 읽는 사람**이다 — 새 후보 목록 옆에 옛 후보의
    승인 결과가 남아 있으면 그 파일은 거짓말을 한다. 조용히 두지 않고 버리고, 무엇을
    버렸는지 run_log 에 남긴다.
    """
    order = STEP_ORDER[step_name]
    out: list[str] = []
    for name, sections in STEP_SECTIONS.items():
        if STEP_ORDER[name] > order:
            out.extend(sections)
    return tuple(sorted(out))


def _meta_doc(path: Path) -> dict:
    """사이드카 전문. 없으면 빈 dict(깨졌으면 그대로 터진다 — 조용한 기본값 금지)."""
    meta = _meta_path(path)
    if not meta.exists():
        return {}
    doc = job.read_json(meta)
    return doc if isinstance(doc, dict) else {}


def _section_fp(path: Path, name: str) -> str | None:
    """절 하나의 기록된 지문. 없으면 None."""
    node = (_meta_doc(path).get("sections") or {}).get(name)
    if not isinstance(node, dict):
        return None
    fp = node.get("fingerprint")
    return str(fp) if fp else None


def _section_meta(path: Path, name: str) -> dict:
    """절 하나의 사이드카 전체(8단계의 `per_id` 처럼 지문 밖 재료가 붙는다)."""
    node = (_meta_doc(path).get("sections") or {}).get(name)
    return dict(node) if isinstance(node, dict) else {}


def _write_section_meta(path: Path, name: str, fingerprint: str, *,
                        purge: tuple[str, ...] = (), reset: bool = False,
                        **extra: Any) -> None:
    """절 지문을 사이드카에 얹는다(읽고-고치고-쓴다 — 남의 절을 지우지 않는다).

    `reset` 은 6단계 전용이다 — 문서를 통째로 새로 쓰므로 사이드카도 통째로 새로 쓴다.
    """
    doc: dict[str, Any] = {} if reset else _meta_doc(path)
    sections = {} if reset else dict(doc.get("sections") or {})
    for gone in purge:
        sections.pop(gone, None)
    sections[name] = {"fingerprint": fingerprint, **extra}
    job.write_json(_meta_path(path), {"schema": "v4_cache_meta/v1",
                                      "of": path.name, "sections": sections})


def _section_cache_state(doc: dict, path: Path, name: str, key: str,
                         fingerprint: str, invalidate: bool) -> tuple[bool, str]:
    """절 하나의 캐시를 쓸 것인가 → (쓴다, 사유). 순수(파일을 읽기만 한다).

    `_cache_state`(파일 단위)와 판정 규칙은 같지만 **지문 미기록의 처분이 반대다.**
    파일 단위 쪽은 v1·v3 시절 job 이 실재해서 '존재로 재사용'이 옳지만, 이 절들은 M3
    에서 처음 생겨 구 job 이 없다 — 지문이 없다는 것은 이 판이 쓴 것이 아니라는 뜻이라
    다시 만든다(모르는 재료를 재사용하는 것이 재호출보다 나쁘다).
    """
    if invalidate:
        return False, "--from-step 무효화"
    if key not in doc:
        return False, "절 없음"
    was = _section_fp(path, name)
    if was is None:
        return False, "지문 미기록 — 다시 만든다(이 절은 M3 신설이라 구 job 이 없다)"
    if was != fingerprint:
        return False, f"지문 불일치 {was} → {fingerprint}"
    return True, "지문 일치"


# ── 격자 → 하류 모듈이 읽는 모양 ───────────────────────────────────────────
# ⚠ 이름을 옮기기만 하는 함수들이다. 그런데 **안 옮기면 조용히 틀린다** — 아래 각
#   주석이 그 지점이다. 값을 다시 계산하지 않는 것이 규율이고(수식 복제 금지),
#   여기서 하는 일은 열쇠 이름 번역뿐이다.

def speech_segments_from_grid(grid: dict) -> list[dict]:
    """격자의 **유성 span** → 전사 세그먼트 `[{start_sec, end_sec, text}]`. 순수.

    cue 묶음 규칙은 격자가 이미 정했다(`grid.timegrid.group_words_to_cues` =
    `stt_elevenlabs.words_to_segments`) — 여기서 다시 묶지 않는다. 6단계 프롬프트가
    보여 준 전사 블록(`candidates.transcript_block`)과 **같은 재료**라, 모델이 옮겨 적은
    quote 와 6c 가 대조하는 글자가 같은 좌표계에 있다.
    """
    out: list[dict] = []
    for sp in (grid.get("span_candidates") or []):
        if not isinstance(sp, dict) or not sp.get("is_audio"):
            continue
        text = str(sp.get("text") or "").strip()
        if not text:
            continue
        try:
            start, end = float(sp["t_in"]), float(sp["t_out"])
        except (KeyError, TypeError, ValueError):
            # 격자는 우리가 만든 파일이다 — 숫자가 아니면 상류 계약 위반이므로 크게 실패.
            raise ValueError(f"격자 span 의 시각이 숫자가 아니다: {sp!r}")
        out.append({"start_sec": start, "end_sec": end, "text": text})
    return out


def words_for_funnel(grid: dict) -> list[dict]:
    """격자 단어(`t0`/`t1`) → 7단계가 읽는 열쇠(`start_sec`/`end_sec`). 순수.

    🛑 **번역을 빼먹으면 조용히 틀린다.** `funnel._field(w, "start_sec", "start")` 는
    `t0` 를 모르고, 못 찾으면 `_num(None, 0.0)` 이 **0.0** 을 돌려준다 — 전 단어가
    0초에 몰린 것으로 읽혀 lead_in·응집도 신호가 통째로 거짓이 된다(예외도 안 난다).
    """
    out: list[dict] = []
    for w in (grid.get("words") or []):
        if not isinstance(w, dict):
            continue
        try:
            out.append({"start_sec": float(w["t0"]), "end_sec": float(w["t1"])})
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"격자 단어의 시각이 숫자가 아니다: {w!r}")
    return out


def build_ranking(funnel_record: dict) -> list[dict]:
    """7단계 기록 → 9단계가 읽는 순위 목록 `[{id, score, signals}]`(M1 §8). 순수.

    순서 정본은 `funnel_record["kept"]` 다 — 그 목록이 이미 순위 순이고(점수 → 소스
    시각 → id), 여기서 다시 정렬하면 동점 처리 규칙이 두 벌이 된다.
    """
    scores = funnel_record.get("scores") or {}
    signals = funnel_record.get("signals") or {}
    out: list[dict] = []
    for cid in funnel_record.get("kept") or []:
        if cid not in scores:
            # 7단계가 kept 에 넣은 후보는 점수도 남긴다 — 없으면 배선 계약 위반이다.
            raise ValueError(f"7단계 kept 에 있는 {cid} 의 점수가 없다 — "
                             "순위 없이 승인하면 파일 번호가 실행마다 흔들린다")
        out.append({"id": cid, "score": scores[cid], "signals": signals.get(cid)})
    return out


# ── 배선 ────────────────────────────────────────────────────────────────────

def run_v4(*, video_path: Path, work_title: str, outdir: Path,
           srt_path: Path | None = None, episode: int | None = None,
           job_id: str | None = None, from_step: str | None = None,
           stop_after: str | None = None,
           skip_research: bool = False,
           max_shorts: int | None = None,
           scene_threshold: float = SCENE_THRESHOLD,
           edit_overrides_path: Path | None = None,
           log=print) -> Path:
    """v4 전 단계 배선. 반환은 job 디렉토리(계약 §1).

    1~5단계는 실제로 돌고, 6~11 은 `not_implemented` 를 남기고 **정상 종료**한다
    (조용히 건너뛰지 않는다 — 무엇이 남았는지 stdout·run_log 양쪽에 적는다).
    """
    _load_dotenv()

    # 인자 정규화는 **비싼 일을 시작하기 전에** 전부 끝낸다(E11 규율). 오타 하나로
    # 무인 노드가 전사까지 태우고 죽는 것이 가장 나쁘다.
    from_step = parse_from_step(from_step)
    stop_after = parse_from_step(stop_after)
    if (stop_after is not None and from_step is not None
            and STEP_ORDER[stop_after] < STEP_ORDER[from_step]):
        raise ValueError(
            f"--stop-after {stop_after} 가 --from-step {from_step} 보다 앞이다 — "
            "그러면 아무 단계도 돌지 않는다(조용한 무실행 금지)")
    max_shorts = approve.clamp_max_shorts(max_shorts)

    video_path = Path(video_path)
    srt_path = Path(srt_path) if srt_path else None
    safe_title = str(work_title).replace(" ", "_")

    # ── 1 init ─────────────────────────────────────────────────────────────
    output_dir = job.job_dir_for(Path(outdir), work_title, job_id)
    job_id = output_dir.name
    run_log_path = output_dir / job.RUN_LOG_NAME
    config = AppConfig()
    resumed = run_log_path.exists()
    if resumed:
        run_log = job.resume_run_log(run_log_path, pipeline=PIPELINE_NAME,
                                     job_id=job_id, from_step=from_step, config=config)
    else:
        run_log = job.new_run_log(pipeline=PIPELINE_NAME, job_id=job_id, config=config)
        run_log["milestone"] = PIPELINE_MILESTONE
    # ⚠ `input` 은 job.new_run_log 가 만들지 않는다(파이프라인마다 어휘가 다르다) —
    #   배선이 얹는 것이 계약이고, M0 채점기(exception_score:137)가 이걸 읽는다.
    #   재개에서는 최초 값을 유지한다(무엇으로 시작한 job 인지가 A/B 의 기준이다).
    run_log.setdefault("input", {
        "video_path": str(video_path), "work_title": work_title,
        "srt_path": str(srt_path) if srt_path else None,
        "episode": episode, "language": "ko"})
    run_log.setdefault("provenance", {}).setdefault("models", {})["roles_v4"] = {
        "all_llm": os.environ.get(V4_MODEL_ENV, V4_MODEL_DEFAULT),
        "grid_transcribe": f"local:{WHISPER_MODEL_NAME}"}

    step = job.make_step_logger(run_log, run_log_path)   # 단계마다 즉시 디스크 확정
    log(f"[v4] job: {job_id} → {output_dir}")
    step("init", job_id=job_id, resumed=resumed, from_step=from_step,
         stop_after=stop_after, max_shorts=max_shorts,
         scene_threshold=scene_threshold, skip_research=skip_research,
         edit_overrides=str(edit_overrides_path) if edit_overrides_path else None)

    # 단계 사이로 넘기는 상태. 파일이 정본이고 이건 그 판의 메모리 사본이다.
    state: dict[str, Any] = {"research": None, "words": [], "failed_windows": [],
                             "silence": None, "grid": None, "media_info": None,
                             "sample_fps": None, "words_fingerprint": None,
                             "media_from_cache": False,
                             # 6~9 가 지문 재료로 쓰는 상류 좌표. 값을 여기 모아 두는
                             # 것은 각 단계가 상류 지문을 **전량 명시**해야 하기
                             # 때문이다(gotcha 9) — 재료를 못 찾으면 크게 실패한다.
                             "research_fingerprint": None, "grid_fingerprint": None,
                             "text_tokens_estimate": None, "cands_doc": None}

    gemini: Any = None

    def get_gemini():
        nonlocal gemini
        if gemini is None:
            gemini = _load_gemini_client()
        return gemini

    def _invalidated(name: str) -> bool:
        """이 단계의 캐시를 **버려야 하는가**. 판정 규칙은 이 함수 하나다.

        `should_run` 은 "이 실행의 범위에 드는가"이고, 그것이 곧 "캐시를 버린다"가
        되는 것은 **재개 지점을 지정한 실행**뿐이다. 지정하지 않은 평상시 실행은 전
        단계가 범위 안이지만 캐시는 각자 지문으로 판정한다 — 안 그러면 재개마다
        가장 비싼 전사를 다시 태운다(계약 §1: "should_run 이 True 여도 자기 지문으로
        캐시를 다시 본다").

        ⚠ 단계 이름을 비교하는 곳은 여기 하나다. 단계마다 손으로 적으면 v3 가 그랬듯
        일곱 갈래로 갈린다(모듈 독스트링 ①).
        """
        return from_step is not None and should_run(name, from_step)

    probe_invalidate = _invalidated("probe")
    probe_ckpt = output_dir / "checkpoint_probe.json"

    def media_info() -> MediaInfo:
        """소재 계측(ffprobe) — 처음 필요한 단계가 재고, 그 뒤로는 메모리 사본.

        3단계 전사가 duration 을 먼저 필요로 하므로 4단계보다 앞서 불릴 수 있다.
        그래도 checkpoint_probe.json 을 쓰는 것은 이 함수 하나이고, '이번 실행에서
        실제로 쟀는지'는 `media_from_cache` 로 남아 4단계 기록이 거짓말을 안 한다.
        """
        if state["media_info"] is not None:
            return state["media_info"]
        fp = job.fingerprint("v4_probe/v1", _file_sig(video_path))
        use_cache, _why = _cache_state(probe_ckpt, fp, probe_invalidate)
        if use_cache:
            data = job.read_json(probe_ckpt)
            info = MediaInfo(path=Path(data["path"]),
                             duration_sec=float(data["duration_sec"]),
                             fps=float(data["fps"]), width=int(data["width"]),
                             height=int(data["height"]),
                             has_audio=bool(data["has_audio"]))
            state["media_from_cache"] = True
        else:
            info = probe_media(video_path)
            job.write_json(probe_ckpt, {**asdict(info), "path": str(info.path),
                                        "schema": "v4_probe/v1"})
            _write_meta(probe_ckpt, fp)
            state["media_from_cache"] = False
        state["media_info"] = info
        state["probe_fingerprint"] = fp
        return info

    # ── 2 research ─────────────────────────────────────────────────────────
    def _step_research() -> None:
        ckpt = output_dir / "checkpoint_research.json"
        if skip_research:
            # 스킵도 기록한다(v3 는 방식이 제각각이었다 — gotcha 15).
            # 지문은 None 으로 남는다 — '리서치 없음'도 6단계 프롬프트를 바꾸는 재료라
            # 그 사실이 지문에 들어야 한다(스킵 실행과 정상 실행의 후보가 섞이면 안 된다).
            step("research", skipped="--skip-research")
            log("  [v4/research] 건너뜀(--skip-research)")
            return
        fp = job.fingerprint("v4_research/v1", work_title, episode)
        use_cache, why = _cache_state(ckpt, fp, _invalidated("research"))
        state["research_fingerprint"] = fp
        if use_cache:
            state["research"] = job.read_json(ckpt)
            step("research", cached=True, fingerprint=fp, cache_reason=why)
            log(f"  [v4/research] 캐시 로드({why})")
            return
        # 리서치는 2단계 — 전사·인코딩보다 **앞**이라 여기서 죽는 것이 가장 싸다.
        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError(
                "GEMINI_API_KEY 없음 — 2단계 리서치는 LLM 호출이다. "
                "키 없이 격자까지만 만들려면 --skip-research 를 준다.")
        t0 = time.time()
        from app.modules.work_researcher import research_work
        r = research_work(work_title, episode, get_gemini())
        doc = {
            "work_context": r.work_context,
            "episodes_context": r.episodes_context,
            "raw_data": r.raw_data,
            "sources": r.sources,
            "cast_images": [
                {"character_name": c.character_name, "actor_name": c.actor_name,
                 "role_description": c.role_description,
                 "image_path": str(c.image_path) if c.image_path else None,
                 "image_url": c.image_url}
                for c in r.characters],
        }
        job.write_json(ckpt, doc)
        _write_meta(ckpt, fp)
        state["research"] = doc
        step("research", elapsed=round(time.time() - t0, 1),
             has_context=bool(r.work_context), cast=len(doc["cast_images"]),
             fingerprint=fp, cache_reason=why)

    # ── 3 자막 전사 ────────────────────────────────────────────────────────
    def _step_transcribe() -> None:
        ckpt = output_dir / "checkpoint_grid_words.json"
        # ⚠ 전사 지문은 **오디오·SRT·모델**뿐이다(계약 §1 의 의도된 비대칭). 격자 지문은
        #   장면 임계·재단 상수까지 본다 — 그쪽은 다시 계산해도 몇 초지만 전사는 가장
        #   비싼 단계라 재료가 그대로면 다시 태우지 않는다. v3 도 같은 비대칭이었고
        #   (`from_step=='grid'` 가 격자만 폐기했다) 그건 의도된 동작이었다.
        fp = job.fingerprint("v4_words/v1", _file_sig(video_path), _file_sig(srt_path),
                             WHISPER_MODEL_NAME)
        state["words_fingerprint"] = fp
        if srt_path:
            # SRT 는 전사를 **대체하지 않는다**(v3 규약 그대로): 시각 정본은 whisper
            # 단어다(방송 SRT 는 싱크 오프셋이 있고 이 레포에 강제 정렬기가 없다).
            # 조용히 무시하지 않고 무엇을 하는지 적는다.
            log(f"  [v4/transcribe] SRT 제공({srt_path.name}) — 전사를 대체하지 않는다. "
                "격자 srt_cues 레이어로 싣고 시각 정본은 whisper 단어다.")
        use_cache, why = _cache_state(ckpt, fp, _invalidated("transcribe"))
        if use_cache:
            d = job.read_json(ckpt)
            state["words"] = d["words"]
            state["failed_windows"] = [tuple(w) for w in d.get("failed_windows") or []]
            # 무음 구간은 전사 캐시에 함께 실린다 — 4단계가 다시 재지 않게 하려는 것이고,
            # 옛 캐시에 없으면 None 으로 남아 4단계가 그때 잰다(조용한 빈 값 금지).
            sil = d.get("silence")
            state["silence"] = [tuple(s) for s in sil] if sil is not None else None
            step("transcribe", cached=True, words=len(state["words"]),
                 fingerprint=fp, cache_reason=why,
                 srt_layer=bool(srt_path))
            log(f"  [v4/transcribe] 캐시 로드 ({len(state['words'])} 단어 · {why})")
            return

        t0 = time.time()
        info = media_info()
        duration = float(info.duration_sec)
        audio_path = output_dir / f"{safe_title}_16k.wav"
        if not audio_path.exists():
            extract_audio_from_video(video_path, audio_path)
        # 무음을 전사 **앞**에 잰다 — 공백 재전사가 진짜 무음 창을 건너뛰는 근거다
        # (무음에 vad off 전사를 돌리면 환각 위험만 산다 — v3 M8-B 실측).
        silence = detect_silence_intervals(audio_path, duration)

        names = [c["character_name"] for c in (state["research"] or {}).get(
            "cast_images") or [] if c.get("character_name")]
        work_context = (state["research"] or {}).get("work_context") or None
        words, failed = transcribe_words(
            audio_path, duration, work_title=work_title,
            character_names=names or None, work_context=work_context, log=log)
        # 재전사에도 본전사와 **같은 프롬프트**(인명 사전·맥락) — 빼먹으면 복원 단어만
        # 인명 표기가 갈린다(v3 리뷰 확정).
        words, gap_audit = retranscribe_gaps(
            audio_path, words, duration, silence,
            prompt=_build_whisper_prompt(work_title=work_title,
                                         character_names=names or None,
                                         work_context=work_context), log=log)
        job.write_json(ckpt, {"model": WHISPER_MODEL_NAME, "words": words,
                              "failed_windows": [list(f) for f in failed],
                              "gap_retry": gap_audit,
                              "silence": [[a, b] for a, b in silence],
                              "srt_provided": bool(srt_path)})
        _write_meta(ckpt, fp)
        state["words"], state["failed_windows"], state["silence"] = words, failed, silence
        if failed:
            log(f"  [v4/transcribe] ⚠ 전사 실패 창 {len(failed)}건 — scene 폴백(무성 취급): "
                + ", ".join(f"{a:.0f}~{b:.0f}s" for a, b in failed))
        step("transcribe", elapsed=round(time.time() - t0, 1), words=len(words),
             failed_windows=[list(f) for f in failed], gap_retry=gap_audit,
             silence_intervals=len(silence), fingerprint=fp, cache_reason=why,
             srt_layer=bool(srt_path))

    # ── 4 probe — 소재 계측 → 격자 → **표본 fps 사전검사** ─────────────────
    def _step_probe() -> None:
        t0 = time.time()
        info = media_info()
        duration = float(info.duration_sec)
        grid_path = output_dir / "grid.json"
        # 격자 지문 = 전사 지문 + 격자를 바꾸는 상수 전량(계약 §1). 재료를 부르는 쪽이
        # **전량 명시**하는 것이 규율이다 — v3 는 지문 4종의 재료가 서로 달라 각각
        # 다른 변경을 놓쳤다(gotcha 9).
        grid_fp = job.fingerprint(
            "v4_grid/v1", state["words_fingerprint"], SCHEMA_GRID,
            round(float(scene_threshold), 4), SILENCE_NOISE_DB, SILENCE_MIN_SEC,
            MIN_UNVOICED_SEC, MAX_UNVOICED_SEC, EDGE_MARGIN_SEC, _file_sig(srt_path))
        use_cache, why = _cache_state(grid_path, grid_fp, probe_invalidate)
        counts: dict[str, Any] = {}
        if use_cache:
            grid = job.read_json(grid_path)
            log(f"  [v4/probe] 격자 캐시 로드({why})")
        else:
            audio_path = output_dir / f"{safe_title}_16k.wav"
            if not audio_path.exists():
                extract_audio_from_video(video_path, audio_path)
            silence = state["silence"]
            if silence is None:
                silence = detect_silence_intervals(audio_path, duration)
                state["silence"] = silence
            # ⚠ 장면 전환을 **원본**에서 잰다. v3 는 480p 프록시를 먼저 만들어 거기서
            #   쟀지만, v4 의 프록시는 5단계(업로드용 720p/30fps)라 4단계에는 아직 없다.
            #   눈금은 원본 좌표계가 정본이므로 순서를 바꾸지 않고 원본에서 잰다 —
            #   디코드 비용은 M8 실측 대상이다(긴 소재에서 프록시 선행이 이득이면
            #   그때 근거를 갖고 옮긴다).
            scene_cuts = detect_scene_cuts(video_path, threshold=scene_threshold)
            arousal = compute_arousal(load_pcm(audio_path), duration, state["words"])
            spans = carve_spans(state["words"], scene_cuts, silence, duration)
            srt_cues = None
            if srt_path:
                srt_cues = [{"t0": round(s.start_sec, 3), "t1": round(s.end_sec, 3),
                             "text": s.text} for s in parse_subtitle(srt_path)]
            grid = build_grid_doc(
                source={"path": str(video_path), "duration_sec": round(duration, 3),
                        "fps": info.fps, "width": info.width, "height": info.height},
                words=state["words"], scene_cuts=scene_cuts, silence=silence,
                arousal=arousal, span_candidates=spans,
                transcript_meta={"backend": "whisper", "model": WHISPER_MODEL_NAME,
                                 "word_count": len(state["words"]),
                                 "failed_windows": [list(f) for f in
                                                    state["failed_windows"]],
                                 "srt_provided": bool(srt_path)},
                srt_cues=srt_cues)
            job.write_json(grid_path, grid)
            _write_meta(grid_path, grid_fp)
            n_voiced = sum(1 for s in spans if s["is_audio"])
            counts = {"words": len(state["words"]), "scene_cuts": len(scene_cuts),
                      "silence_intervals": len(silence), "arousal_points": len(arousal),
                      "spans": {"total": len(spans), "voiced": n_voiced,
                                "unvoiced": len(spans) - n_voiced}}
            log(f"  [v4/probe] 격자 완료 — 단어 {len(state['words'])} · "
                f"장면컷 {len(scene_cuts)} · span {len(spans)}(유성 {n_voiced})")
        state["grid"] = grid
        state["grid_fingerprint"] = grid_fp

        # 표본 fps 사전검사 — **프록시 인코딩·업로드 앞**이다(계약 §2). 720p/30fps 는
        # 인코딩도 업로드도 비싸다(3시간 소재 업로드 실측 364초). 여기서 죽으면
        # 그 비용을 안 태운다.
        text_tokens, tt_note = _estimate_text_tokens(state["words"], state["research"])
        try:
            sample_fps, fps_note = fps_mod.resolve_sample_fps(
                duration, text_tokens=text_tokens)
        except ValueError as e:
            # 죽더라도 **왜 죽었는지는 남긴다** — checkpoint_probe.json 과 run_log 양쪽.
            note = getattr(e, "note", None)
            _write_probe_ckpt(probe_ckpt, info, None, note, tt_note)
            step("probe", elapsed=round(time.time() - t0, 1), grid_cached=use_cache,
                 grid_fingerprint=grid_fp, cache_reason=why,
                 text_tokens=text_tokens, text_tokens_note=tt_note,
                 sample_fps=None, sample_fps_note=note, error=str(e), **counts)
            log(f"  [v4/probe] ✖ 표본 fps 사전검사 실패 — {e}")
            raise
        state["sample_fps"] = sample_fps
        # 6단계가 **실제 전사 블록 길이**와 맞대어 볼 추정치(계약 §5) — M8 이 이 둘로
        # `TEXT_TOKENS_PER_CHAR` 를 갈아낀다.
        state["text_tokens_estimate"] = text_tokens
        _write_probe_ckpt(probe_ckpt, info, sample_fps, fps_note, tt_note)
        log(f"  [v4/probe] 표본 fps {sample_fps:g} ({fps_note['reason']}) · "
            f"텍스트 {text_tokens:,} 토큰(추정) · 예상 {fps_note['est_count_tokens']:,}")
        step("probe", elapsed=round(time.time() - t0, 1), cached=use_cache,
             media_from_cache=state["media_from_cache"],
             probe_fingerprint=state.get("probe_fingerprint"),
             grid_fingerprint=grid_fp, cache_reason=why,
             duration_sec=round(duration, 3),
             text_tokens=text_tokens, text_tokens_note=tt_note,
             sample_fps=sample_fps, sample_fps_note=fps_note, **counts)

    # ── 5 업로드 — 720p/30fps 프록시 + Files API 1회 ───────────────────────
    def _step_upload() -> None:
        # 지연 임포트다: `app/v4/proxy.py` 는 계약 §3 의 별건 모듈이고, 그것이 없어도
        # 4단계까지(`--stop-after probe`)는 돌아야 한다. 없으면 **크게 실패**한다 —
        # 조용히 건너뛰면 업로드 없이 6단계가 죽는다.
        try:
            from app.v4 import proxy as proxy_mod
        except ImportError as e:
            raise RuntimeError(
                "app/v4/proxy.py 가 없다 — 5단계(프록시·업로드)의 정본은 그 모듈이다"
                "(M2 계약 §3). 4단계까지만 돌리려면 --stop-after probe.") from e

        ckpt = output_dir / "checkpoint_upload.json"
        # 지문·문서 모양은 **proxy 모듈이 정본**이다(계약 §3). 여기서 다시 조립하면
        # 인코딩 인자 하나가 빠져도 아무도 모른 채 옛 프록시가 재사용된다(gotcha 9).
        fp = proxy_mod.proxy_fingerprint(video_path)
        use_cache, why = _cache_state(ckpt, fp, _invalidated("upload"))
        if use_cache:
            doc = job.read_json(ckpt)
            ref = (doc.get("handle") or {}).get("name") or \
                (doc.get("handle") or {}).get("uri")
            # Files API 핸들은 48시간이면 사라진다 — 살아 있는지 **서버에 묻고**(시계
            # 산술이 아니다), 죽었으면 다시 올린다. 조용히 죽은 핸들을 넘기면 6단계가
            # 뜻 모를 400 으로 죽는다.
            if ref and proxy_mod.handle_alive(get_gemini(), ref):
                state["upload"] = doc
                step("upload", cached=True, fingerprint=fp, cache_reason=why,
                     handle=doc.get("handle"))
                log(f"  [v4/upload] 캐시 재사용({why}) — 핸들 살아 있음")
                return
            log("  [cache] ⚠ 업로드 핸들 만료·부재 — 다시 올린다")
            why = f"{why} → 핸들 만료(재업로드)"

        t0 = time.time()
        out_path = proxy_mod.proxy_path_for(output_dir, height=proxy_mod.PROXY_HEIGHT,
                                            file_fps=proxy_mod.PROXY_FILE_FPS)
        proxy_path, proxy_meta = proxy_mod.build_proxy(video_path, out_path, log=log)
        handle, handle_meta = proxy_mod.upload_handle(get_gemini(), proxy_path, log=log)
        # ⚠ `release_handle` 은 여기서 부르지 않는다. 6·6b·8·10a 가 **같은 핸들을
        #   공유**하므로 수명은 마지막 소비자(11:render)를 짓는 마일스톤이 닫는다.
        #   지금 실패 경로에서 지우면 재개가 3시간 프록시를 다시 올린다(업로드 실측
        #   364초) — 만료는 위 handle_alive 가 어차피 잡는다.
        doc = proxy_mod.upload_checkpoint_doc(fingerprint=fp, proxy_path=proxy_path,
                                              proxy_meta=proxy_meta,
                                              handle_meta=handle_meta)
        job.write_json(ckpt, doc)
        _write_meta(ckpt, fp)
        state["upload"], state["handle"] = doc, handle
        step("upload", elapsed=round(time.time() - t0, 1), fingerprint=fp,
             cache_reason=why, proxy=proxy_meta, handle=handle_meta)

    # ── 6~9 후보 깔때기 — 한 파일(`checkpoint_candidates.json`)에 증분 ─────
    cands_ckpt = output_dir / CANDIDATES_CKPT
    model_name = os.environ.get(V4_MODEL_ENV, V4_MODEL_DEFAULT)
    # 길이 정책은 `AppConfig` 하나다(E20-⓪ 이후 40~120초 · env 로 채널별 예외).
    # 목표치는 범위 중앙 — v1 스토리 프롬프트가 `ideal_duration_sec` 를 만드는 식과
    # 같은 자다(`app/modules/gemini_client.py:2066`).
    min_sec = float(config.min_duration_sec)
    max_sec = float(config.max_duration_sec)
    target_sec = (min_sec + max_sec) / 2.0
    # 단계별 절 지문. 하류 단계가 상류 지문을 **전량 명시**해 재료로 삼는다(gotcha 9).
    # 사이드카를 다시 읽지 않고 메모리에 들고 있는 이유는 캐시 히트·신규 어느 경로든
    # 같은 값이어야 하기 때문이다(히트면 '맞은' 그 지문이다).
    section_fp: dict[str, str] = {}

    def video_handle() -> Any:
        """5단계가 남긴 영상 핸들. 갓 올린 실행은 핸들 객체, 재개는 uri 문자열이다.

        `video._file_uri` 가 두 모양을 다 받는다 — Files API 핸들은 서버 자원이라
        재개에서 객체를 되살릴 방법이 없고, 체크포인트에는 uri 만 남는다.
        """
        if state.get("handle") is not None:
            return state["handle"]
        uri = ((state.get("upload") or {}).get("handle") or {}).get("uri")
        if not uri:
            raise RuntimeError(
                "5단계 업로드 결과가 없다 — 6단계 이후는 올린 영상을 보는 호출이다. "
                "`--from-step upload` 로 다시 올려라.")
        return uri

    def cands_doc() -> dict:
        """`checkpoint_candidates.json` 의 이 판 사본. 없으면 빈 문서."""
        if state["cands_doc"] is None:
            state["cands_doc"] = (job.read_json(cands_ckpt) if cands_ckpt.exists()
                                  else {})
        return state["cands_doc"]

    def require_key(key: str, owner: str) -> Any:
        """상류 절을 꺼낸다. 없으면 **크게 실패** — 조용히 빈 값으로 진행하면 그 뒤
        전 단계가 '후보 0개'를 정상 상태로 읽고 결번이 조용히 나간다."""
        doc = cands_doc()
        if key not in doc:
            raise RuntimeError(
                f"`{cands_ckpt.name}` 에 `{key}` 절이 없다 — {owner} 가 먼저 돌아야 한다. "
                f"`--from-step {owner}` 로 다시 만들어라.")
        return doc[key]

    def save_sections(step_name: str, sections: dict[str, Any], *,
                      fingerprint: str, **meta_extra: Any) -> tuple[str, ...]:
        """자기 절만 얹고 **하류 절은 버린다** → 버린 절 이름들.

        소유 검사는 `STEP_SECTIONS` 표 하나가 한다 — 어느 단계가 남의 절을 쓰려 하면
        여기서 죽는다(계약 §5: "남의 절을 건드리지 마라").
        """
        owned = set(STEP_SECTIONS[step_name])
        if set(sections) != owned:
            raise RuntimeError(
                f"{step_name} 단계가 소유하지 않은 절을 쓰려 한다: "
                f"{sorted(set(sections) ^ owned)} (소유: {sorted(owned)})")
        doc = dict(cands_doc())
        purge = _purge_after(step_name)
        purged = tuple(g for g in purge if g in doc)
        for gone in purged:
            doc.pop(gone, None)
        doc.update(sections)
        job.write_json(cands_ckpt, doc)
        _write_section_meta(cands_ckpt, step_name, fingerprint, purge=purge,
                            **meta_extra)
        state["cands_doc"] = doc
        if purged:
            log(f"  [v4/{step_name}] 하류 절 폐기: {' '.join(purged)} "
                f"(상류가 바뀌었으므로 그 결과는 다른 후보에 대한 것이다)")
        return purged

    def sector_source() -> tuple[dict, str]:
        """7단계가 볼 예고 구역 → (구역, 출처). 읽는 곳을 이 함수 **하나**로 모은다.

        🛑 6b 는 top 의 `exception_sectors` 를 **덮지 않는다.** 자기 절만 쓴다는 계약
        (§5)이기도 하지만, 덮으면 6b 재실행이 **이미 옮긴 경계**를 중심으로 ±60 창을
        잡아 원판정을 잃는다 — 창의 기준은 언제나 6단계의 원 신고여야 한다.
        """
        doc = cands_doc()
        node = doc.get("boundary")
        if isinstance(node, dict) and isinstance(node.get("exception_sectors"), dict):
            return node["exception_sectors"], "boundary"
        return (doc.get("exception_sectors") or {}), "candidates"

    def source_duration() -> float:
        return float(state["grid"]["source"]["duration_sec"])

    # ── 6 후보 편성 (LLM 1콜 + 재질의 ≤2) ─────────────────────────────────
    def _step_candidates() -> None:
        templates = cand_mod.TEMPLATES_DEFAULT
        # ⚠ 지문에 넣는 것은 **프롬프트 골격(PROMPT_TEMPLATE)의 sha** 이지 렌더된
        #   프롬프트의 sha 가 아니다. 렌더된 문자열은 (골격 · work_title · 격자 ·
        #   리서치 · 템플릿 목록 · 후보 수 범위 · 길이 목표)의 결정적 함수이고 그
        #   재료가 **전부 아래에 명시돼 있다**. 여기서 다시 렌더하려면 run_candidates
        #   내부의 재료 조립(transcript_block·summarize_grid·heuristic_hints)을 베껴야
        #   하는데, 베낀 조립은 언젠가 한쪽만 고쳐진다(규율 4). 실제로 쓴 프롬프트의
        #   sha 는 audit·절(`prompt_sha`)에 그대로 남는다.
        fp = job.fingerprint(
            "v4_candidates/v1", state["grid_fingerprint"],
            state["research_fingerprint"], work_title, state["sample_fps"],
            cand_mod.prompt_sha(cand_mod.PROMPT_TEMPLATE), model_name,
            list(templates),
            [cand_mod.CANDIDATES_MIN, cand_mod.CANDIDATES_MAX],
            round(target_sec, 3), round(max_sec, 3))
        section_fp["candidates"] = fp
        doc = cands_doc()
        use_cache, why = _section_cache_state(doc, cands_ckpt, "candidates",
                                              "candidates", fp,
                                              _invalidated("candidates"))
        if use_cache:
            step("candidates", cached=True, fingerprint=fp, cache_reason=why,
                 candidates=len(doc.get("candidates") or []))
            log(f"  [v4/candidates] 캐시 로드({why}) — "
                f"후보 {len(doc.get('candidates') or [])}개")
            return

        t0 = time.time()
        section, audit = cand_mod.run_candidates(
            get_gemini(), video_handle(), work_title=work_title, grid=state["grid"],
            research=state["research"], sample_fps=state["sample_fps"],
            templates=templates, target_sec=target_sec, max_sec=max_sec, log=log)
        # 6단계는 파일의 **몸통**을 만든다 — 새 문서를 쓰면 하류 절이 함께 사라진다.
        state["cands_doc"] = {**section, "fingerprint": fp}
        job.write_json(cands_ckpt, state["cands_doc"])
        _write_section_meta(cands_ckpt, "candidates", fp, reset=True)
        # 계약 §5 — 4단계의 **추정치**와 6단계의 **실측 블록 길이**를 나란히 남긴다.
        # M8 이 이 둘로 `TEXT_TOKENS_PER_CHAR` 를 갈아낀다.
        step("candidates", elapsed=round(time.time() - t0, 1), fingerprint=fp,
             cache_reason=why, candidates=len(section["candidates"]),
             prompt_sha=section["prompt_sha"], templates=section["templates"],
             reasks_used=audit.get("reasks_used"),
             exception_sectors=section["exception_sectors"],
             text_tokens_check={
                 "estimated_at_probe": state["text_tokens_estimate"],
                 "transcript_chars": audit.get("transcript_chars"),
                 "transcript_lines": audit.get("transcript_lines"),
                 "tokens_per_char": TEXT_TOKENS_PER_CHAR,
                 "note": "4단계 추정 대 6단계 실측 — M8 환산 상수의 재료(계약 §5)"},
             audit=audit)

    # ── 6b 경계 정밀 (LLM ≤FLASH_BUDGET) ──────────────────────────────────
    def _step_boundary() -> None:
        base = require_key("exception_sectors", "candidates")
        fp = job.fingerprint(
            "v4_boundary/v1", section_fp["candidates"],
            boundary_mod.PROBE_WINDOW_SEC, boundary_mod.TAIL_PROBE_SEC,
            boundary_mod.PROBE_SAMPLE_FPS, boundary_mod.FLASH_BUDGET,
            boundary_mod.BOUNDARY_TOKEN_BUDGET, boundary_mod.MAX_WINDOW_EXTENSIONS,
            cand_mod.prompt_sha(boundary_mod.BOUNDARY_PROMPT), model_name)
        section_fp["boundary"] = fp
        doc = cands_doc()
        use_cache, why = _section_cache_state(doc, cands_ckpt, "boundary", "boundary",
                                              fp, _invalidated("boundary"))
        if use_cache:
            step("boundary", cached=True, fingerprint=fp, cache_reason=why,
                 moved=(doc.get("boundary") or {}).get("moved"))
            log(f"  [v4/boundary] 캐시 로드({why})")
            return

        t0 = time.time()
        corrected, audit = boundary_mod.run_boundary_probe(
            get_gemini(), video_handle(), exception_sector=base, grid=state["grid"],
            duration_sec=source_duration(), log=log)
        section = {"exception_sectors": corrected, "before": base,
                   "moved": audit.get("moved", 0), "audit": audit}
        purged = save_sections("boundary", {"boundary": section}, fingerprint=fp)
        if audit.get("stopped"):
            # 예산 소진·실패는 **원판정 유지**다(오염 방지 비대칭) — 조용히 넘기지 않는다.
            log(f"  [v4/boundary] ⚠ 중단: {audit['stopped']} — 남은 경계는 원판정 유지")
        step("boundary", elapsed=round(time.time() - t0, 1), fingerprint=fp,
             cache_reason=why, moved=audit.get("moved", 0),
             flash_calls=audit.get("flash_calls"), tokens=audit.get("tokens"),
             stopped=audit.get("stopped"), purged=list(purged),
             exception_sectors=corrected, audit=audit)

    # ── 6c 구간 검증 (코드 · LLM 0콜) ─────────────────────────────────────
    def _step_verify() -> None:
        cands = require_key("candidates", "candidates")
        grid = state["grid"]
        fp = job.fingerprint(
            "v4_verify/v1", section_fp["candidates"], state["grid_fingerprint"],
            verify_mod.QUOTE_MATCH_TOLERANCE_SEC, verify_mod.QUOTE_FUZZY_MAX_RATIO,
            verify_mod.MIN_CANDIDATE_SEC, verify_mod.MIN_SEGMENT_SEC)
        section_fp["verify"] = fp
        doc = cands_doc()
        use_cache, why = _section_cache_state(doc, cands_ckpt, "verify", "verify", fp,
                                              _invalidated("verify"))
        if use_cache:
            node = doc["verify"]
            step("verify", cached=True, fingerprint=fp, cache_reason=why,
                 kept=len(node.get("kept") or []),
                 dropped=len(node.get("dropped") or []))
            log(f"  [v4/verify] 캐시 로드({why}) — 통과 {len(node.get('kept') or [])}개")
            return

        t0 = time.time()
        kept, record = verify_mod.verify_candidates(
            cands, segments=speech_segments_from_grid(grid),
            source_duration_sec=source_duration(),
            grid_times=grid_snap_times(grid))
        # ⚠ `candidates` 를 절에 함께 싣는다(M1 §8 의 예시에는 없는 가산 키다).
        #   6c 는 조각을 **재배치·클램프**하므로 그 결과가 하류(7·8)로 전달되지 않으면
        #   6c 가 한 일이 사라진다. 재개(`--from-step flags`)도 이 목록을 읽는다.
        purged = save_sections("verify", {"verify": {**record, "candidates": kept}},
                               fingerprint=fp)
        for d in record["dropped"]:
            log(f"  [v4/verify] ✖ {d['id']} 드롭 — {d['why']}")
        if not kept:
            # 전량 드롭은 크게 남긴다. 기획서 §6c 는 여기서 6단계 재질의를 말하지만
            # 그 되먹임은 이 판의 범위가 아니다 — 9단계가 `no_publishable` 로 크게
            # 실패하므로 결번이 조용히 나가지는 않는다(계약 §5).
            log("  [v4/verify] 🛑 전량 드롭 — 인용이 전사 어디에도 없다(환각) 또는 "
                "조각을 잃어 길이 하한 미만. 9단계에서 편 전체가 실패한다.")
        step("verify", elapsed=round(time.time() - t0, 1), fingerprint=fp,
             cache_reason=why, of=len(cands), kept=len(kept),
             dropped=len(record["dropped"]), relocated=record["relocated"],
             clamped=record["clamped"], purged=list(purged),
             dropped_detail=record["dropped"])

    # ── 7 깔때기 (코드 · LLM 0콜) ─────────────────────────────────────────
    def _step_funnel() -> None:
        node = require_key("verify", "verify")
        cands = list(node.get("candidates") or [])
        sectors, sectors_from = sector_source()
        grid = state["grid"]
        fp = job.fingerprint(
            "v4_funnel/v1", section_fp["verify"], section_fp["boundary"],
            state["grid_fingerprint"], round(min_sec, 3), round(max_sec, 3),
            funnel_mod.FUNNEL_KEEP, funnel_mod.IOU_DEDUP,
            funnel_mod.MIN_SPEECH_COVERAGE, funnel_mod.DEFAULT_WEIGHTS)
        section_fp["funnel"] = fp
        doc = cands_doc()
        use_cache, why = _section_cache_state(doc, cands_ckpt, "funnel", "funnel", fp,
                                              _invalidated("funnel"))
        if use_cache:
            step("funnel", cached=True, fingerprint=fp, cache_reason=why,
                 kept=len((doc["funnel"].get("kept") or [])))
            log(f"  [v4/funnel] 캐시 로드({why})")
            return

        t0 = time.time()
        kept, record = funnel_mod.run_funnel(
            cands, exception_sectors=sectors, source_duration_sec=source_duration(),
            scene_cuts=[float(t) for t in (grid.get("scene_cuts") or [])],
            speech_intervals=speech_segments_from_grid(grid),
            words=words_for_funnel(grid),
            min_sec=min_sec, max_sec=max_sec)
        ranking = build_ranking(record)
        purged = save_sections("funnel",
                               {"funnel": {**record, "candidates": kept,
                                           "sectors_from": sectors_from},
                                "rank": ranking}, fingerprint=fp)
        for d in record["dropped"]:
            log(f"  [v4/funnel] ✖ {d['id']} 탈락 — {'; '.join(d['reasons'])}")
        if record.get("fallback"):
            log(f"  [v4/funnel] ⚠ 전량 하드 탈락 폴백: {record['fallback']}")
        step("funnel", elapsed=round(time.time() - t0, 1), fingerprint=fp,
             cache_reason=why, of=record["of"], kept=record["kept"],
             dropped=record["dropped"], scores=record["scores"],
             dedup=record["dedup"], capped=record["capped"],
             intro_overlap=record["intro_overlap"], fallback=record["fallback"],
             speech_filtered=record["speech_filtered"],
             sectors_from=sectors_from, purged=list(purged))

    # ── 8 시각 플래그 (LLM ≤8 · 후보 단위 증분) ───────────────────────────
    def _step_flags() -> None:
        node = require_key("funnel", "funnel")
        cands = list(node.get("candidates") or [])
        duration = source_duration()
        tmpl_sha = cand_mod.prompt_sha(flags_mod.FLAGS_PROMPT)

        # 후보 **하나마다** 지문이 있다(계약 §5) — 한 후보가 바뀌어도 나머지 채점은 산다.
        # 재료는 계약대로 **후보 timeline** 이다: 조각의 (시작, 끝)만 본다. quote·제목
        # 가안은 8단계 프롬프트에 싣지 않으므로(`build_flags_prompt` 독스트링 — 사람 말로
        # 된 의도를 주면 모델이 화면 대신 그 의도를 채점한다) 그것이 바뀌었다고 다시
        # 물어보면 요금만 나간다.
        per_fp: dict[str, str] = {}
        for cand in cands:
            cid = flags_mod.candidate_id(cand)
            timeline = [[c.start_sec, c.end_sec]
                        for c in flags_mod.candidate_clips(cand)]
            per_fp[cid] = job.fingerprint(
                "v4_flags_cand/v1", timeline,
                flags_mod.FLAG_SAMPLE_FPS, tmpl_sha, flags_mod.PART_LIMIT,
                flags_mod.FLAG_MAX_OUTPUT_TOKENS, round(duration, 3), model_name)
        fp = job.fingerprint("v4_flags/v1", sorted(per_fp.items()))
        section_fp["flags"] = fp

        doc = cands_doc()
        invalidate = _invalidated("flags")
        use_cache, why = _section_cache_state(doc, cands_ckpt, "flags", "flags", fp,
                                              invalidate)
        prev = doc.get("flags") if isinstance(doc.get("flags"), dict) else {}
        prev_fp = _section_meta(cands_ckpt, "flags").get("per_id") or {}
        reused: dict[str, dict] = {}
        if not invalidate:
            for cid, one in per_fp.items():
                entry = prev.get(cid)
                # ⚠ 재사용은 **채점에 성공한 것만**이다. 미채점(`failed`)은 '나쁘다'가
                #   아니라 '모른다'라서(계약 §4), 재개는 그것을 다시 물어보는 것이 옳다.
                #   그 대가로 끝내 실패하는 후보는 재개마다 한 콜을 더 쓴다 — 감사에
                #   `recomputed` 로 남는다.
                if (isinstance(entry, dict)
                        and entry.get("status") == approve.FLAGS_STATUS_OK
                        and prev_fp.get(cid) == one):
                    reused[cid] = entry
        todo = [c for c in cands if flags_mod.candidate_id(c) not in reused]
        stale = sorted(set(prev) - set(per_fp))

        if use_cache and not todo:
            step("flags", cached=True, fingerprint=fp, cache_reason=why,
                 of=len(cands), reused=len(reused), recomputed=0)
            log(f"  [v4/flags] 캐시 로드({why}) — 채점 {len(reused)}/{len(cands)}")
            return

        t0 = time.time()
        fresh: dict[str, dict] = {}
        audit: dict[str, Any] = {"of": 0, "scored": 0, "calls": 0,
                                 "note": "재계산할 후보가 없다(전량 캐시 재사용)"}
        if todo:
            fresh, audit = flags_mod.run_flags(
                get_gemini(), video_handle(), todo, source_duration_sec=duration,
                log=log)
        merged: dict[str, dict] = {}
        for cid in sorted(per_fp):
            entry = reused.get(cid) or fresh.get(cid)
            if entry is None:
                # run_flags 는 넘긴 후보 전량에 항목을 낸다 — 없다면 배선 결함이다.
                raise RuntimeError(f"8단계가 {cid} 의 결과를 내지 않았다 — "
                                   "미채점도 항목으로 남아야 한다(조용한 증발 금지)")
            merged[cid] = entry
        if stale:
            log(f"  [v4/flags] 지난 실행의 채점 {len(stale)}건 폐기(후보 목록이 바뀌었다): "
                + " ".join(stale))
        purged = save_sections("flags", {"flags": merged}, fingerprint=fp,
                               per_id=per_fp)
        scored = sum(1 for e in merged.values()
                     if e.get("status") == approve.FLAGS_STATUS_OK)
        step("flags", elapsed=round(time.time() - t0, 1), fingerprint=fp,
             cache_reason=why, of=len(cands), reused=len(reused),
             recomputed=len(todo), stale_dropped=stale, scored=scored,
             unscored=len(merged) - scored, purged=list(purged), audit=audit)

    # ── 9 승인·순위 (코드 · LLM 0콜) ──────────────────────────────────────
    def _step_approve() -> None:
        funnel_rec = require_key("funnel", "funnel")
        verify_rec = require_key("verify", "verify")
        flags_map = require_key("flags", "flags")
        ranking = require_key("rank", "funnel")
        fp = job.fingerprint("v4_approve/v1", section_fp["funnel"],
                             section_fp["verify"], section_fp["flags"],
                             int(max_shorts))
        section_fp["approve"] = fp
        doc = cands_doc()
        use_cache, why = _section_cache_state(doc, cands_ckpt, "approve", "approval",
                                              fp, _invalidated("approve"))
        if use_cache:
            result = doc["approval"]
            step("approve", cached=True, fingerprint=fp, cache_reason=why,
                 approved=result.get("approved"),
                 no_publishable=result.get("no_publishable"))
            log(f"  [v4/approve] 캐시 로드({why}) — 승인 "
                f"{len(result.get('approved') or [])}편")
        else:
            t0 = time.time()
            result = approve.approve(funnel=funnel_rec, verify=verify_rec,
                                     flags=flags_map, ranking=ranking,
                                     max_shorts=max_shorts)
            save_sections("approve", {"approval": result}, fingerprint=fp)
            step("approve", elapsed=round(time.time() - t0, 1), fingerprint=fp,
                 cache_reason=why, approved=result["approved"],
                 rejected=result["rejected"], fallback=result["fallback"],
                 fallback_id=result["fallback_id"],
                 fallback_reasons=result["fallback_reasons"],
                 capped=result["capped"], max_shorts=result["max_shorts"],
                 scoring_unavailable=result["scoring_unavailable"],
                 considered=result["considered"],
                 no_publishable=result["no_publishable"])

        if result.get("scoring_unavailable"):
            log("  [v4/approve] ⚠ 8단계 전량 미채점 — 순위가 결정적 신호(6c·7)만으로 "
                "정해졌다. 검수가 알아야 한다.")
        if result.get("fallback"):
            log(f"  [v4/approve] ⚠ 게이트 통과 0편 — 순위 1위 {result['fallback_id']} 를 "
                f"경고와 함께 낸다: {'; '.join(result['fallback_reasons'])}")
        if result.get("no_publishable"):
            # 🛑 조용한 결번이 이 레포에서 가장 나쁜 실패다(무인 노드 6대) — 크게 죽는다.
            raise ValueError(
                "9단계 승인 0편이고 폴백할 순위도 없다 — 발행할 편이 없다(조용한 결번 금지).\n"
                f"  심사 대상 {result.get('considered')}건 · "
                f"7단계 통과 {len(funnel_rec.get('kept') or [])}건 · "
                f"6c 통과 {len(verify_rec.get('kept') or [])}건\n"
                f"  탈락 사유: " + "; ".join(
                    f"{r['id']}={','.join(r['reasons'])}"
                    for r in (result.get("rejected") or [])[:16]))
        log(f"  [v4/approve] 승인 {len(result['approved'])}편 "
            f"(상한 {result['max_shorts']} · 잘림 {result['capped']}): "
            + " ".join(result["approved"]))

    handlers: dict[str, Callable[[], None]] = {
        "research": _step_research,
        "transcribe": _step_transcribe,
        "probe": _step_probe,
        "upload": _step_upload,
        "candidates": _step_candidates,
        "boundary": _step_boundary,
        "verify": _step_verify,
        "funnel": _step_funnel,
        "flags": _step_flags,
        "approve": _step_approve,
    }
    # 표와 배선을 묶는다 — `IMPLEMENTED_STEPS` 만 고치고 handler 를 빠뜨리면 그 단계가
    # 조용히 `KeyError` 로 죽거나(미구현 표에도 없으니) 엉뚱하게 정상 종료한다.
    if set(handlers) | {"init"} != IMPLEMENTED_STEPS:
        raise RuntimeError(
            "IMPLEMENTED_STEPS 와 handlers 가 갈렸다: "
            f"{sorted((set(handlers) | {'init'}) ^ IMPLEMENTED_STEPS)}")

    # 🛑 순서·정지·미구현 판정은 이 루프 하나이고, 캐시 무효화는 `_invalidated` 하나다.
    #   단계 이름을 비교하는 코드를 이 둘 밖에 더하지 마라 — v3 가 그렇게 일곱 갈래로
    #   갈렸고, 그때 사람의 직관과 실제 동작이 조용히 어긋났다(모듈 독스트링 ①).
    current = "init"
    try:
        for name in V4_STEPS[1:]:          # init 은 위에서 이미 돌았다
            current = name
            if stop_after is not None and STEP_ORDER[name] > STEP_ORDER[stop_after]:
                remaining = [s for s in V4_STEPS if STEP_ORDER[s] >= STEP_ORDER[name]]
                step(name, skipped=f"--stop-after {stop_after}", remaining=remaining)
                log(f"[v4] --stop-after {stop_after} — 남은 단계 {len(remaining)}개: "
                    + " ".join(remaining))
                return output_dir
            handler = handlers.get(name)
            if handler is None:
                milestone = NOT_IMPLEMENTED_MILESTONE[name]
                remaining = [s for s in V4_STEPS if STEP_ORDER[s] >= STEP_ORDER[name]]
                step(name, not_implemented=milestone, remaining=remaining)
                log(f"[v4] {step_label(name)} 는 아직 없다({milestone}) — 여기서 정상 "
                    f"종료한다. 남은 단계: " + " ".join(remaining))
                return output_dir
            handler()
        return output_dir
    except Exception as e:
        # 실패도 감사 기록이다 — 어느 단계에서 무엇으로 죽었는지 남기고 그대로 올린다
        # (삼키지 않는다). step 마다 즉시 기록하므로 여기까지의 단계는 이미 디스크에 있다.
        step("error", at=current, type=type(e).__name__, message=str(e))
        raise


def _write_probe_ckpt(path: Path, info: MediaInfo, sample_fps: float | None,
                      fps_note: dict | None, text_note: dict) -> None:
    """checkpoint_probe.json — MediaInfo(v1·v3 와 같은 필드) + 표본 fps 판정 근거.

    ⚠ 실패한 실행도 이 파일을 쓴다(계약 §2: "쓰고 죽는다"). 왜 죽었는지가 job
    디렉토리에 남아야 사람이 소재를 나눌지 텍스트를 줄일지 판단할 수 있다.
    """
    job.write_json(path, {**asdict(info), "path": str(info.path),
                          "schema": "v4_probe/v1",
                          "sample_fps": sample_fps,
                          "sample_fps_note": fps_note,
                          "text_tokens_note": text_note})
