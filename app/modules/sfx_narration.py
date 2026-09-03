"""내레이션 시작 효과음 배치 (E19-5 SFX 레이어에 실을 목록을 만든다).

AI 선택 경로(`style_compose.sfx_prompt_block`)와 **별개**다. 그쪽은 감정 비트의 원본
절대초에 모델이 고른 소리를 붙이고, 이쪽은 TTS 내레이션이 시작하는 자리에 규칙으로
붙인다. 둘은 매니페스트 파일부터 분리돼 있다 — `manifest.json` 이 생기면 AI 경로가
켜지므로 이쪽은 `narration_manifest.json` 을 따로 읽는다.

## 타이밍 규약

cue 는 `cue.start_sec`(편집 타임라인 초)에 놓이지만 **그 시각이 곧 목소리 시작은 아니다**.
합성된 mp3 앞에 2~168ms 의 무음이 붙어 있다(백엔드·문장마다 다르다). 실측한 이 리드인을
더해야 실제 발화 시각이 나온다. 효과음은 **피크**가 그 시각에 떨어져야 하므로 파일의
피크 위치만큼 앞당겨 시작한다.

    발화(출력시각) = cue.start_sec / speed + lead_in
    효과음 시작(출력시각) = 발화 - peak_sec
    → sfx.start_sec(편집시각) = cue.start_sec + (lead_in - peak_sec) * speed

렌더러가 `start_sec / speed` 를 다시 적용하므로 편집 타임라인 좌표로 되돌려 넘긴다.
허용 오차는 ±0.1초다(그보다 늦으면 첫 음절 뒤에 떨어져 어긋나게 들린다).

## 같은 소리 반복 방지

이름이 아니라 **계열**로 판정한다. `swoosh-015` 와 `swoosh-015-tight` 는 이름이 다르지만
같은 녹음의 다른 트림이라 연달아 나오면 반복으로 들린다. 계열은 매니페스트의 `family`
필드다(이름에서 `-tight` 와 끝자리 숫자를 뗀 것).

선택은 실행 디렉터리 이름을 시드로 삼아 **결정적**이다 — 재렌더해도 같은 소리가 나온다.
다만 매니페스트에서 항목을 빼면 후보 수가 달라져 기존 편의 선택도 바뀐다.
"""
from __future__ import annotations

import array
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

MANIFEST_NAME = "narration_manifest.json"
SFX_DIR_NAME = "sfx"
STAGE_SUBDIR = "style_assets"      # 스티커·AI SFX 와 같은 스테이징 위치
_PROBE_SR = 16000
_ONSET_REL = 0.02                  # 피크 대비 -34dB — 리드인 판정 기준


def load_narration_manifest(app_root: Path) -> dict[str, Any]:
    """`app/assets/sfx/narration_manifest.json`. 없거나 깨졌으면 빈 dict(= 기능 꺼짐)."""
    path = Path(app_root) / "assets" / SFX_DIR_NAME / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("sfx"), list):
        return {}
    cfg = raw.get("narration") or {}
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return {}
    items = [it for it in raw["sfx"]
             if isinstance(it, dict) and it.get("id") and it.get("file")]
    if not items:
        return {}
    return {"config": cfg, "items": items}


def lead_in_sec(audio_path: Path) -> float:
    """파일 앞쪽 무음 길이(초). 디코드 실패·무음 파일은 0.0(보정 없음 = 종전 동작)."""
    try:
        pcm = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(audio_path), "-ac", "1",
             "-ar", str(_PROBE_SR), "-f", "s16le", "-"],
            capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return 0.0
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    if not a:
        return 0.0
    peak = max(max(a), -min(a))
    if peak <= 0:
        return 0.0
    thr = peak * _ONSET_REL
    idx = next((i for i, v in enumerate(a) if abs(v) >= thr), 0)
    return idx / _PROBE_SR


def _pick(items: list[dict], tag: str, seed: str, step: int,
          recent: list[str], no_repeat: int) -> dict | None:
    """용도 태그에 맞는 후보 중 최근 계열을 피해 하나 고른다. 후보 없으면 None."""
    pool = [it for it in items
            if tag in (it.get("opening") or []) or tag in (it.get("mid") or [])]
    if not pool:
        return None
    order = sorted(pool, key=lambda it: hashlib.sha1(
        f"{seed}:{it['id']}".encode()).hexdigest())
    window = recent[-no_repeat:] if no_repeat > 0 else []
    cand = [it for it in order if (it.get("family") or it["id"]) not in window]
    if not cand:
        # 후보가 좁으면 직전 계열만 피한다(그것도 안 되면 어쩔 수 없이 전체)
        cand = [it for it in order
                if not recent or (it.get("family") or it["id"]) != recent[-1]] or order
    rng = int(hashlib.sha1(f"{seed}:{step}".encode()).hexdigest(), 16)
    return cand[rng % len(cand)]


def place_narration_sfx(tts_cue_files: list[dict], *, app_root: Path, run_dir: Path,
                        seed: str, speed: float = 1.0) -> list[dict[str, Any]]:
    """TTS cue 목록 → 렌더에 실을 `sfx_audio` 항목들.

    매니페스트가 없거나 꺼져 있으면 빈 리스트(= 종전과 완전히 같은 렌더).
    파일은 `run_dir/style_assets/` 로 스테이징한다 — 체크포인트 재렌더가 번들 없이도
    같은 소리를 재현해야 한다(AI SFX·스티커와 같은 규율).
    """
    mf = load_narration_manifest(app_root)
    if not mf or not tts_cue_files:
        return []
    cfg = mf["config"]
    gain_db = float(cfg.get("gain_db", -6.0))
    opening_max_t = float(cfg.get("opening_max_t", 1.5))
    no_repeat = int(cfg.get("no_repeat_window", 3))
    src_dir = Path(app_root) / "assets" / SFX_DIR_NAME
    dest_dir = Path(run_dir) / STAGE_SUBDIR

    out: list[dict[str, Any]] = []
    recent: list[str] = []
    for step, cf in enumerate(tts_cue_files):
        cue = cf.get("cue") or {}
        try:
            cue_start = float(cue.get("start_sec"))
        except (TypeError, ValueError):
            continue
        tag = "opening" if (step == 0 and cue_start / speed < opening_max_t) else "line_start"
        it = _pick(mf["items"], tag, seed, step, recent, no_repeat)
        if it is None:
            continue
        src = src_dir / it["file"]
        if not src.is_file():
            print(f"  [sfx-narration] 번들에 파일이 없어 건너뜀: {it['file']}")
            continue

        lead = lead_in_sec(Path(cf["path"])) if cf.get("path") else 0.0
        peak = float(it.get("peak_sec", 0.0))
        # 편집 타임라인 좌표로 되돌린다(렌더러가 다시 /speed 한다).
        start = cue_start + (lead - peak) * speed
        if start < 0:
            start = 0.0

        dest_dir.mkdir(parents=True, exist_ok=True)
        # 절대경로로 넘긴다 — 렌더러는 ffmpeg 를 다른 작업 디렉터리에서 부를 수 있고,
        # 상대경로가 섞이면 그 입력만 못 열어 렌더 전체가 죽는다(실측).
        dest = (dest_dir / src.name).resolve()
        if not dest.exists():
            shutil.copy2(src, dest)

        out.append({"path": dest, "start_sec": round(start, 3), "gain_db": gain_db,
                    "_narration": {"id": it["id"], "family": it.get("family"),
                                   "tag": tag, "cue_index": cf.get("cue_index", step),
                                   "lead_in_sec": round(lead, 4), "peak_sec": peak}})
        recent.append(it.get("family") or it["id"])
    return out


# ── 라벨(효과 텍스트) 등장 효과음 ────────────────────────────────────────────
#
# 내레이션과 다른 점 둘.
#   ① 리드인 보정이 없다 — 라벨은 화면에 그려지는 것이라 `start_sec` 가 곧 등장 시각이다.
#   ② 밑에 소리가 있는지를 본다. 사용자 분류(2026-09-03)에서 `pop-up-something` 은
#      "대사 중에는 X, 화면에 라벨만 있고 주목시킬 때"라 조건이 붙었다 — 그 조건을
#      **대사도 내레이션도 없다**로 환산한다(관객이 들을 게 없으면 화면이 유일한 사건).
#
# ⚠ 실측(2026-09-03, 6편): 소리가 완전히 빈 구간은 편의 20~52%(0.8초 이상 구간 2~7개)다.
#    `label_visual_only` 후보가 한 번도 안 걸리는 편이 있으므로 폴백이 필수다.

LABEL_TAG = "label"
LABEL_QUIET_TAG = "label_visual_only"


def _overlaps(a: float, b: float, windows) -> bool:
    return any(not (y <= a or x >= b) for x, y in windows)


def place_label_sfx(labels: list[dict], *, app_root: Path, run_dir: Path, seed: str,
                    speed: float = 1.0,
                    busy_windows: list[tuple[float, float]] | None = None,
                    ) -> list[dict[str, Any]]:
    """라벨 목록 → 렌더에 실을 `sfx_audio` 항목들.

    `busy_windows` 는 편집 타임라인의 **소리 있는 구간**(내레이션 cue 창 ∪ 대사 자막).
    비면 전부 '소리 있음'으로 본다(모르면 보수적으로 — 조용한 자리 전용 소리를
    시끄러운 자리에 놓지 않는다).

    매니페스트 `label` 절이 없거나 꺼져 있으면 빈 리스트(= 렌더 종전과 동일).
    """
    mf = load_narration_manifest(app_root)
    if not mf or not labels:
        return []
    cfg = (mf["config"].get("label") or {}) if isinstance(mf["config"], dict) else {}
    if not cfg.get("enabled"):
        return []
    gain_db = float(cfg.get("gain_db", -6.0))
    no_repeat = int(cfg.get("no_repeat_window", 3))
    # "all" = 라벨마다 무조건(테스트 기본) · "quiet_only" = 소리 빈 자리에만
    mode = str(cfg.get("mode", "all"))
    # None(모름) 과 빈 리스트(재 봤더니 소리가 없음)는 다르다 — 모르면 보수적으로
    # '소리 있음'으로 봐서 화면 전용 소리를 시끄러운 자리에 놓지 않는다.
    unknown = busy_windows is None
    busy = list(busy_windows or [])
    src_dir = Path(app_root) / "assets" / SFX_DIR_NAME
    dest_dir = Path(run_dir) / STAGE_SUBDIR

    out: list[dict[str, Any]] = []
    recent: list[str] = []
    for step, lb in enumerate(labels):
        try:
            at = float(lb["start_sec"])
            end = float(lb.get("end_sec", at))
        except (TypeError, ValueError, KeyError):
            continue
        quiet = (not unknown) and not _overlaps(at, max(end, at + 0.01), busy)
        if mode == "quiet_only" and not quiet:
            continue
        # 조용한 자리면 전용 소리를 먼저 — 없으면 일반 라벨 소리로 떨어진다(폴백 필수).
        it = None
        if quiet:
            it = _pick(mf["items"], LABEL_QUIET_TAG, seed, step, recent, no_repeat)
            # 화면 전용 후보가 하나뿐이면 반복 방지가 무력해진다(실측 94a86e4c_run1:
            # 라벨 2개가 둘 다 조용해서 같은 소리가 연달아 나갔다). 직전과 같은
            # 계열이면 일반 라벨 풀에서 다른 것을 찾는다 — 전용 소리를 한 번 포기하는
            # 편이 같은 소리 반복보다 낫다(사용자 지시: 연속 반복은 지루하다).
            if it is not None and recent and (it.get("family") or it["id"]) == recent[-1]:
                alt = _pick(mf["items"], LABEL_TAG, seed, step, recent, no_repeat)
                if alt is not None and (alt.get("family") or alt["id"]) != recent[-1]:
                    it = alt
        if it is None:
            it = _pick(mf["items"], LABEL_TAG, seed, step, recent, no_repeat)
        if it is None:
            continue
        src = src_dir / it["file"]
        if not src.is_file():
            print(f"  [sfx-label] 번들에 파일이 없어 건너뜀: {it['file']}")
            continue

        peak = float(it.get("peak_sec", 0.0))
        start = at - peak * speed          # 피크가 라벨 등장 프레임에 떨어지게
        if start < 0:
            start = 0.0

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = (dest_dir / src.name).resolve()
        if not dest.exists():
            shutil.copy2(src, dest)

        out.append({"path": dest, "start_sec": round(start, 3), "gain_db": gain_db,
                    "_label": {"id": it["id"], "family": it.get("family"),
                               "quiet": quiet, "at": round(at, 3),
                               "text": str(lb.get("text") or "")[:20],
                               "peak_sec": peak}})
        recent.append(it.get("family") or it["id"])
    return out


# 두 타격이 이 안에 들면 한 번의 두꺼운 소리로 뭉쳐 들린다(플램). 그보다 벌어지면
# 서로 다른 두 사건으로 들리므로 겹침으로 보지 않는다.
COLLIDE_TOL_SEC = 0.05


def drop_label_collisions(narration: list[dict], labels: list[dict], *,
                          tol: float = COLLIDE_TOL_SEC,
                          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """내레이션 효과음과 **동시에 타격하는** 라벨 효과음을 버린다(사용자 지시:
    동시에 시작하면 내레이션만 남긴다). Returns: (남길 라벨, 버린 라벨).

    ⚠ 비교는 `start_sec` 이 아니라 **타격 시각**(start_sec + peak_sec)이다.
    두 소리는 피크 보정만큼 서로 다른 시각에 시작하지만 귀에 닿는 순간은 앵커다 —
    시작으로 재면 같은 순간에 때리는 쌍을 놓치고, 엉뚱한 쌍을 겹침으로 잡는다.
    """
    def hit(s: dict, key: str) -> float:
        return float(s.get("start_sec", 0.0)) + float((s.get(key) or {}).get("peak_sec", 0.0))

    narr_hits = [hit(s, "_narration") for s in narration]
    keep: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for lb in labels:
        h = hit(lb, "_label")
        if any(abs(h - n) <= tol for n in narr_hits):
            dropped.append(lb)
        else:
            keep.append(lb)
    return keep, dropped
