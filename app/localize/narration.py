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
TRIM_FADE_SEC = 0.15              # 잘린 끝을 페이드아웃(딸깍 소리 방지)


def rate_string(base: int, bump: int) -> str:
    """edge-tts rate 인자 — 양수는 '+' 를 붙인다. 순수(테스트 대상)."""
    r = base + bump
    return f"{'+' if r >= 0 else ''}{r}%"


def fits_window(dur: float, window: float, margin: float = WINDOW_MARGIN) -> bool:
    """합성 길이가 계획 창에 들어오는가. 순수(테스트 대상)."""
    return 0.0 < dur <= window * margin


def trim_args(src: Path, dst: Path, window: float, ffmpeg: str,
              fade: float = TRIM_FADE_SEC) -> list:
    """창 길이로 잘라내는 ffmpeg argv. 순수(테스트 대상).

    페이드아웃은 창의 10% 와 TRIM_FADE_SEC 중 짧은 쪽 — 짧은 cue 에서 페이드가 전체를
    먹지 않게 한다. 같은 파일을 읽으면서 쓸 수 없어 dst 는 반드시 다른 경로여야 한다."""
    f = max(0.0, min(fade, window * 0.1))
    af = f"afade=t=out:st={max(0.0, window - f):.3f}:d={f:.3f}" if f > 0 else "anull"
    return [ffmpeg, "-y", "-v", "error", "-i", str(src),
            "-t", f"{window:.3f}", "-af", af, str(dst)]


def _trim_to_window(mp3: Path, window: float) -> float:
    """cue 오디오를 창 안으로 잘라내고 잘린 뒤 길이를 돌려준다.

    ⚠ **이것이 없으면 편 전체가 죽는다.** 렌더는 `amix=duration=longest` 로 섞고
    ffmpeg 에 `-shortest` 가 없어서, 마지막 cue 오디오가 창을 넘으면 그만큼 **출력 mp4
    가 영상 트랙보다 길어진다**(2026-08-24 실측: 합성 2.0s 영상 + 2.5s cue → 컨테이너
    2.5s). 그러면 L4 의 컷 길이 대조(허용 0.05초)가 그 편을 통째로 실패시킨다
    (SHOTCONE 혜미리예채파 2화: ko 39.400s vs ja 39.900s 로 3회 연속 dead).

    한국어 경로는 `synthesize_cue_cached` 가 fit 재작성으로 창을 지키는데 일본어
    재합성만 rate 3단계를 다 쓰고도 안 맞으면 **경고만 찍고 넘어갔다** — 그 비대칭이
    이 버그의 정체다. 잘라내는 것은 내레이션 끝 일부를 잃지만, 편 전체가 발행되지
    못하는 것보다 낫고 **건별로 크게 남기므로** 검수에서 잡힌다."""
    if window <= 0:
        return _audio_dur(mp3)
    tmp = mp3.with_name(mp3.stem + ".trim" + mp3.suffix)
    try:
        subprocess.run(trim_args(mp3, tmp, window, find_ffmpeg_command("ffmpeg")),
                       capture_output=True, text=True, check=True)
        tmp.replace(mp3)
    except (OSError, subprocess.CalledProcessError) as e:
        tmp.unlink(missing_ok=True)
        stderr = getattr(e, "stderr", "") or ""
        print(f"[L3t] ⚠️ 창 맞춤 잘라내기 실패 — 원본 유지: {stderr.strip()[-200:] or e}")
    return _audio_dur(mp3)


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
        trimmed = dur > window
        if trimmed:
            # rate 를 끝까지 올려도 안 맞았다 — 여기서 넘기면 마지막 cue 일 때 출력 mp4 가
            # 영상보다 길어져 L4 컷 대조가 편 전체를 실패시킨다(_trim_to_window 독스트링).
            over = dur - window
            dur = _trim_to_window(mp3, window)
            print(f"[L3t] ⚠️ cue {c['cue_index']}: 창 {window:.1f}s 를 {over:.2f}s 초과 — "
                  f"창 길이로 잘랐다({dur:.1f}s). 내레이션 끝이 잘렸을 수 있으니 검수 필요")
        cue["fit_actual_sec"] = round(dur, 3)
        if trimmed:
            cue["fit_trimmed"] = True      # 검수 카드가 '끝이 잘린 cue' 를 짚을 근거
        cue["voice_ja"] = prof["voice_id"]
        print(f"[L3t] cue {c['cue_index']}: {text!r} → {prof['voice_id']} "
              f"{dur:.1f}s (창 {window:.1f}s)")
    res_path.write_text(json.dumps(resources, ensure_ascii=False, indent=2), encoding="utf-8")
