"""현지화 실행 — L0~L5 를 순서대로 돌린다.

원본: `localize_run.main`.

⚠ 이식하며 **의도적으로 바꾼 것 하나** — 진행 상태 파일 위치.
   vlp 는 자기 레포의 `results/localize_state.json` 에 전 job 의 진행을 모아 썼다.
   ai-video 는 엔진 레포에 런타임 상태를 쓰지 않으므로 **job 디렉토리 안**
   (`localize_<locale>/state.json`)에 둔다. 이 파일을 읽는 곳은 없다(오케스트레이터는
   `metadata.json` 존재만 본다) — 산출에 영향이 없다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.localize.apply import l3_apply
from app.localize.collect import invalidate_localize_cache, l0_backup
from app.localize.meta import l5_metadata
from app.localize.narration import l3t_tts
from app.localize.overrides import apply_overrides
from app.localize.rerender import l4_render
from app.localize.spec import LocalizeSpec, gemini_client
from app.localize.telop import l2_extract, l2b_refine_timing
from app.localize.translate import l1_translate


def _mark(state_path: Path, stage: str, **extra) -> None:
    """진행 기록 — 실패 지점을 사람이 찾을 수 있게. 산출에는 영향 없다."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except ValueError:
        state = {}
    state[stage] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **extra}
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_localize(job_dir: str | Path, locale: str = "ja", *,
                 overrides_path: str | Path | None = None,
                 skip_render: bool = False, rebuild: bool = False) -> Path:
    """job 디렉토리 하나를 현지화한다. 성공 마커(`localize_<locale>/metadata.json`) 경로를 돌려준다.

    rebuild(편집실 재렌더): 한국어 백업·번역/텔롭 캐시를 지금 job 디렉토리 상태로 갱신하고
    L1·L2 를 다시 돌린다. 이것 없이 재실행하면 **사람이 고친 한국어가 일본어판에 반영되지
    않는다**(2026-08-23 SHOTCONE 실측 — 새 검수 카드의 ko_ja_pairs 가 직전 카드와 바이트
    단위로 동일했다)."""
    spec = LocalizeSpec.build(job_dir, locale)
    state_path = spec.out_dir / "state.json"
    print(f"=== 현지화 시작: {spec.job.name} ({spec.work_title} → {locale}) ==="
          + (" [rebuild]" if rebuild else ""))

    if rebuild:
        removed = invalidate_localize_cache(spec.out_dir)
        print(f"[rebuild] 캐시 폐기: {', '.join(removed) or '없음'} — 고친 한국어 원본으로 "
              f"다시 번역한다")

    backup = l0_backup(spec.job, rebuild=rebuild)
    _mark(state_path, "L0")

    client = gemini_client()
    telop_data = l2_extract(spec.job, spec.out_dir, client)
    _mark(state_path, "L2", items=len(telop_data))
    telop_refined = l2b_refine_timing(spec.job, telop_data, spec.out_dir, client)
    _mark(state_path, "L2b", telops=len(telop_refined))
    translation = l1_translate(backup, telop_data, spec.work_title, spec.work_cfg,
                               spec.out_dir, client)
    _mark(state_path, "L1")

    if overrides_path:
        # 반려-수정 재렌더(8/14): 검수함에서 고친 텍스트를 L1 결과(캐시 포함)에 병합하고
        # translation.json 을 재기록 — 이후 L3(자막·제목·TTS 텍스트)·L4(재렌더)·L5(메타)가
        # 전부 고친 본을 쓴다. 같은 카드에서 여러 번 고치면 병합이 누적된다(마지막이 이긴다).
        ov = json.loads(Path(overrides_path).read_text(encoding="utf-8"))
        translation = apply_overrides(translation, ov)
        (spec.out_dir / "translation.json").write_text(
            json.dumps(translation, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[L1+] 반려 수정 병합({Path(overrides_path).name}) → translation.json 재기록")
        _mark(state_path, "L1+", overrides=Path(overrides_path).name)

    l3_apply(spec.job, backup, translation, telop_refined,
             spec.work_cfg, spec.locale_cfg, spec.out_dir)
    _mark(state_path, "L3")
    l3t_tts(spec.job, backup, spec.locale_cfg)
    _mark(state_path, "L3t")
    if not skip_render:
        l4_render(spec.job, spec.work_cfg, spec.locale_cfg, spec.out_dir)
        _mark(state_path, "L4")
    l5_metadata(spec.job, translation, spec.work_cfg, spec.out_dir)
    _mark(state_path, "L5", done=True)

    marker = spec.out_dir / "metadata.json"
    print(f"=== 완료: {spec.job / 'shorts.mp4'} ({locale}판) · 검수자료 {spec.out_dir} ===")
    return marker
