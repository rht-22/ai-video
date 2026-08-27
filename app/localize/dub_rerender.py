"""L4d — 롱폼(rerender) 일본어판 대사 더빙 (2026-08-27, 운영자 결정).

잔망루피 롱폼은 혜미리예채파와 달리 **대사도 일본어**여야 한다("대사도 일본어여야
하니까 반려하고 롱폼에 더빙 붙여줘" — 한국어 원음 + 일본어 자막 포맷은 예능 전용).
나레이션은 생성 단계에서 이미 없다(`--no-narration`).

설계 — 쇼츠 C 루트의 더빙 기계를 **그대로** 쓴다(별도 구현 금지):
  · 합성·백체크·페이싱은 `overlay/dub.dub()` — 페이싱 상한 1.35 등 "잔망루피 목소리의
    정체"인 숫자들이 쇼츠와 같은 config 한 곳(pipeline.config.yaml)에서 나온다.
  · 목소리도 쇼츠와 같은 루피 보이스(config `dub.voice_id`) — 같은 캐릭터가 롱폼과
    쇼츠에서 다른 목소리를 내면 안 된다. work_cfg 로 편별 교체는 열어 둔다.
  · 보컬 제거는 `separate_vocals`(demucs) · 믹스는 `common.mux_dub`.

⚠ 쇼츠 더빙과 **다른 것 둘**:
  ① ASR·번역이 없다 — 대사 문구·시각의 정본은 L3 가 이미 만든 일본어
     `subtitle_segments.json` 이다. 렌더가 구운 자막과 더빙이 말하는 문장이
     **같은 소스**라 어긋날 수 없다(ASR 을 다시 돌리면 두 번역이 갈린다).
  ② 자막을 굽지 않는다(`burn_dub_subtitle=False`) — L4 가 이미 구웠다.

게이트: `work_cfg["dub"]` 가 없으면 **단계가 통째로 없다**(SHOTCONE 등 회귀 0).
실패는 **크게** — 대사가 일본어여야 한다는 것이 이 채널의 발행 조건이라, 조용히
한국어 원음으로 발행되는 것이 실패보다 나쁘다(2026-08-12 needs_dub 사고와 같은 부류).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


def dub_events(segments: list) -> list[dict]:
    """subtitle_segments.json → dub 입력 이벤트. 순수(테스트 대상).

    빈 줄은 뺀다(합성할 것이 없다). start/end 는 편집본 시간축 그대로 —
    렌더가 구운 자막과 같은 좌표라 목소리가 자막이 뜬 그 자리에서 난다."""
    out = []
    for s in segments or []:
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        out.append({"start": float(s["start_sec"]), "end": float(s["end_sec"]),
                    "text": text})
    return out


def dub_config(base_config: dict, work_dub_cfg: dict, out_dir: Path) -> dict:
    """overlay config 를 롱폼용으로 좁힌 사본. 순수(테스트 대상).

    · outputs_dir 를 job 의 localize 디렉토리로 — 엔진 레포 outputs/ 를 더럽히지 않는다.
    · burn_dub_subtitle off — 자막은 L4 가 이미 구웠다(이중 자막 금지).
    · voice_id 는 work_cfg 가 이기되, 기본은 쇼츠와 같은 루피 보이스.
    페이싱·백체크·믹스 숫자는 **건드리지 않는다** — 쇼츠와 같은 정체성."""
    import copy
    cfg = copy.deepcopy(base_config)
    cfg.setdefault("paths", {})["outputs_dir"] = str(out_dir)
    d = cfg.setdefault("dub", {})
    d["burn_dub_subtitle"] = False
    if work_dub_cfg.get("voice_id"):
        d["voice_id"] = str(work_dub_cfg["voice_id"])
    return cfg


def l4d_dub(job: Path, work_cfg: dict, locale_cfg: dict, out_dir: Path):
    """L4 산출(shorts.mp4, JA판)의 한국어 대사를 일본어 더빙으로 교체한다.

    반환: 기록용 dict | None(게이트 꺼짐·대사 0줄). 산출:
      <out_dir>/dub/…(합성 세그·draft·백체크) · <out_dir>/shorts_ja_nodub.mp4(교체 전 보존)
      · <job>/shorts.mp4 교체본."""
    dcfg = (work_cfg or {}).get("dub")
    if not dcfg:
        return None                              # 게이트 — 종전 채널은 이 단계가 없다
    from app.localize.overlay import common as ocommon
    from app.localize.overlay import dub as odub
    from app.localize.overlay import render as orender

    video = Path(job) / "shorts.mp4"
    if not video.exists():
        raise FileNotFoundError(f"[L4d] L4 산출이 없다: {video} — 더빙은 렌더 뒤에만 돈다")
    segments = json.loads((Path(job) / "subtitle_segments.json").read_text(encoding="utf-8"))
    events = dub_events(segments)
    if not events:
        # 대사 0줄(노래뿐인 편 등)은 더빙 대상이 아니다 — 원음 유지가 맞다(空飛ぶルーピー
        # 실사고: 대사 없는 편의 보컬 제거는 노래 가사를 지운다). 조용히는 안 넘어간다.
        print("[L4d] 대사 자막 0줄 — 더빙 없이 원본 오디오 유지(노래·무대사 편)")
        return {"segments": 0, "skipped": "no_dialogue"}

    # 의존 프리체크 — 비싼 합성(요금) **앞**에서 잡는다(E-실측 교훈: 비싼 단계 뒤
    # 검사는 그 시간을 버린다). 생성 노드 venv 에는 overlay 무거운 의존이 없을 수 있다.
    import importlib.util
    missing = [m for m in ("demucs", "faster_whisper")
               if importlib.util.find_spec(m) is None]
    if missing:
        raise RuntimeError(
            f"[L4d] 더빙 의존이 엔진 venv 에 없다: {', '.join(missing)} — "
            f"생성 노드에서 `<ai-video>/.venv/bin/pip install {' '.join(missing)}` 후 재시도")

    config = dub_config(ocommon.load_config(None), dcfg, Path(out_dir))
    line_max = int(config.get("render", {}).get("line_max_chars", 16))
    dub_dir = ocommon.ensure_dir(Path(out_dir) / "dub")
    srt = dub_dir / "ja_dub_input.srt"
    srt.write_text(orender.build_srt(events, line_max), encoding="utf-8")

    print(f"[L4d] 대사 더빙 시작: {len(events)}줄 → voice {config['dub'].get('voice_id')}"
          f" (백체크 {'on' if config['dub'].get('backcheck', {}).get('enabled') else 'off'})")
    # base = outputs_dir/"dub" = <out_dir>/dub — 위 dub_dir 과 같은 자리다
    res = odub.dub("dub", str(srt), "C", config,
                   voice_id=str(config["dub"].get("voice_id") or ""))
    draft = Path(res["draft"])
    if not draft.exists() or draft.stat().st_size == 0:
        raise RuntimeError("[L4d] 더빙 트랙이 비었다 — 합성 실패(한국어 원음 발행 금지)")

    # 한국어 보컬 제거(demucs) → 반주 스템 위에 일본어 더빙 믹스 → shorts.mp4 교체
    nov = odub.separate_vocals(str(video), dub_dir / "stems", config)
    predub = Path(out_dir) / "shorts_ja_nodub.mp4"
    shutil.copy2(video, predub)                  # 교체 전 보존 — 더빙 A/B·문제 추적용
    d = config["dub"]
    dubbed = ocommon.mux_dub(video, draft, dub_dir / "final_dubbed.mp4",
                             bg_volume=max(float(d.get("bg_volume", 0.3)), 0.4),
                             voice_volume=float(d.get("voice_volume", 1.1)),
                             bg_audio=str(nov),
                             loudnorm=bool(d.get("loudnorm", False)),
                             limiter=bool(d.get("limiter", True)),
                             limit=float(d.get("peak_limit", 0.97)))
    shutil.copy2(dubbed, video)
    print(f"[L4d] 대사 더빙 완료: {len(events)}줄, shorts.mp4 교체"
          f" (교체 전 보존: {predub.name})")
    return {"segments": len(events), "backend": res.get("backend"),
            "draft": str(draft)}
