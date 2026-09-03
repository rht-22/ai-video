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
