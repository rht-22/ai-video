"""L3t — 내레이션 TTS 를 로케일 언어로 재합성한다.

원본: `localize_run.l3t_tts`.

원본 **대사 음성은 한국어 그대로**다(범위 밖). 바뀌는 것은 내레이션뿐이다.
cue 텍스트는 L3 가 이미 일본어로 바꿔 놨으므로 같은 경로의 mp3 를 덮어쓴다 —
렌더가 그 경로를 읽어 오디오 믹스와 자막 길이를 잡는다.

길이는 cue 계획 창(start~end) 안에 들어올 때까지 rate 를 올려 재합성한다.
⚠ 매핑 없는 voice(chat_* 등 multilingual)는 **건너뛴다** — 원 보이스로 일본어를
합성할 수 없으므로, 조용히 다른 목소리로 바꾸지 않고 한국어 음성을 남긴다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command

# edge-tts rate(%) 기준값 — ai-video VOICE_PRESETS 의 speed 라벨과 같은 어휘다.
SPEED_BASE = {"very_slow": -25, "slow": -10, "normal": 0, "fast": 10, "very_fast": 25}
RATE_BUMPS = (0, 15, 30)          # 창을 넘으면 이만큼씩 올려 재합성
WINDOW_MARGIN = 0.95              # 창의 95% 안에 들어오면 합격


def rate_string(base: int, bump: int) -> str:
    """edge-tts rate 인자 — 양수는 '+' 를 붙인다. 순수(테스트 대상)."""
    r = base + bump
    return f"{'+' if r >= 0 else ''}{r}%"


def fits_window(dur: float, window: float, margin: float = WINDOW_MARGIN) -> bool:
    """합성 길이가 계획 창에 들어오는가. 순수(테스트 대상)."""
    return 0.0 < dur <= window * margin


def _audio_dur(p: Path) -> float:
    try:
        return float(subprocess.run(
            [find_ffmpeg_command("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True).stdout.strip())
    except ValueError:
        return 0.0


def l3t_tts(job: Path, backup: Path, locale_cfg: dict):
    res_path = job / "checkpoint_resources.json"
    resources = json.loads(res_path.read_text(encoding="utf-8"))
    cues = resources.get("tts_cue_files", [])
    if not cues:
        print("[L3t] TTS cue 없음 — 생략")
        return
    import asyncio

    import edge_tts
    vmap = locale_cfg.get("tts_voice_map", {})
    for c in cues:
        cue = c["cue"]
        mp3 = Path(c["path"])
        bk = backup / mp3.name
        if mp3.exists() and not bk.exists():
            shutil.copy2(mp3, bk)                      # 한국어 mp3 보존(멱등)
        prof = vmap.get(cue.get("voice")) or vmap.get("_default")
        if prof is None:                               # chat_* 등 multilingual — 원 보이스 유지
            print(f"[L3t] cue {c['cue_index']}: voice {cue.get('voice')!r} 매핑 없음 — "
                  f"원 보이스로 일본어 합성 불가, 건너뜀")
            continue
        base = SPEED_BASE.get(cue.get("speed", "normal"), 0)
        window = float(cue["end_sec"]) - float(cue["start_sec"])
        text = cue["text"]
        dur = 0.0
        for bump in RATE_BUMPS:
            rate = rate_string(base, bump)

            async def _run():
                await edge_tts.Communicate(
                    text, prof["voice_id"], rate=rate, pitch=prof.get("pitch", "+0Hz")
                ).save(str(mp3))

            asyncio.run(_run())
            dur = _audio_dur(mp3)
            if fits_window(dur, window):
                break
        if dur > window:
            print(f"[L3t] ⚠️ cue {c['cue_index']}: {dur:.1f}s > 창 {window:.1f}s — 검수 필요")
        cue["fit_actual_sec"] = round(dur, 3)
        cue["voice_ja"] = prof["voice_id"]
        print(f"[L3t] cue {c['cue_index']}: {text!r} → {prof['voice_id']} "
              f"{dur:.1f}s (창 {window:.1f}s)")
    res_path.write_text(json.dumps(resources, ensure_ascii=False, indent=2), encoding="utf-8")
