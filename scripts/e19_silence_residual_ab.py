"""E19-6 잔여 정적 하한 A/B 대조 — 저장된 체크포인트에 파라미터만 다시 태운다.

E14 방식: 이미 만들어진 job 의 저장물(무음 컷 **전** 클립·전사·후보)을 그대로 쓰고
`cut_silence_with_story_filter` 를 두 번(베이스라인 vs 잔여 정적 하한) 돌려 산출을
대조한다 — 파이프라인을 다시 돌리지 않으므로 LLM 비결정성 없이 순수하게 이 파라미터의
효과만 보인다.

  python -m scripts.e19_silence_residual_ab --job outputs/<job_dir> --residual 0.35
  python -m scripts.e19_silence_residual_ab --job A --job B --residual 0.35   # 여러 편

읽는 것(전부 있어야 한다 — 없으면 그 job 은 건너뛰고 사유를 찍는다):
  checkpoint_story.json             variants[].clips (무음 컷 전 클립)
  checkpoint_chunk_transcripts.json 청크 전사 (SpeechSegment 원료)
  checkpoint_gemini.json            all_candidates (보호 판정 메타)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from app.modules.silence_cutter import (
    AGGRESSIVE_PROFILE,
    cut_silence_with_story_filter,
    get_silence_profile,
)
from app.modules.speech import SpeechSegment
from app.modules.story_builder import StoryClip
from app.pipeline import _build_candidates_lookup


def _load_job(job_dir: Path):
    story = json.loads((job_dir / "checkpoint_story.json").read_text(encoding="utf-8"))
    chunk_tr = json.loads(
        (job_dir / "checkpoint_chunk_transcripts.json").read_text(encoding="utf-8"))
    gem = json.loads((job_dir / "checkpoint_gemini.json").read_text(encoding="utf-8"))

    variants = []
    for v in story.get("variants") or [{"clips": story.get("clips") or []}]:
        clips = []
        for c in v.get("clips") or []:
            fields = {k: c[k] for k in c
                      if k in StoryClip.__dataclass_fields__}      # 스키마 진화 관용
            clips.append(StoryClip(**fields))
        if clips:
            variants.append(clips)

    segs: list[SpeechSegment] = []
    for ct in chunk_tr or []:
        for s in ct.get("segments") or []:
            try:
                segs.append(SpeechSegment(float(s["start_sec"]), float(s["end_sec"]),
                                          str(s.get("text", ""))))
            except (KeyError, TypeError, ValueError):
                continue
    lookup = _build_candidates_lookup(gem.get("all_candidates") or [])
    return variants, segs, lookup


def _stats(results):
    removed = sum(r.total_removed_sec for r in results)
    cuts = sum(max(0, len(r.keep_intervals) - 1) for r in results)
    kept = sum(iv.end_sec - iv.start_sec for r in results for iv in r.keep_intervals)
    return removed, cuts, kept


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job", action="append", required=True, help="job 디렉토리(복수 가능)")
    ap.add_argument("--residual", type=float, default=0.35,
                    help="잔여 정적 하한(초, 기본 0.35 — 벤치마크 0.3~0.5 구간)")
    ap.add_argument("--profile", default="aggressive",
                    help="베이스라인 프로파일(기본 aggressive — residual 은 gap-level 에만 작용)")
    args = ap.parse_args()

    base_prof = get_silence_profile(args.profile)
    if not base_prof.gap_level:
        print(f"⚠ 프로파일 {base_prof.name} 는 gap-level 이 아니라 residual 이 작용하지 "
              f"않는다 — aggressive 로 대조한다")
        base_prof = AGGRESSIVE_PROFILE
    b_prof = replace(base_prof, min_residual_pause_sec=None)
    a_prof = replace(base_prof, min_residual_pause_sec=args.residual)

    print(f"베이스라인: {b_prof.name}(residual=없음)  vs  잔여 정적 {args.residual:g}s\n")
    agg = {"removed": [0.0, 0.0], "cuts": [0, 0], "kept": [0.0, 0.0]}
    for job in args.job:
        job_dir = Path(job)
        try:
            variants, segs, lookup = _load_job(job_dir)
        except (OSError, ValueError, KeyError) as e:
            print(f"[{job_dir.name}] 건너뜀 — 체크포인트를 못 읽음: {e}")
            continue
        if not variants or not segs:
            print(f"[{job_dir.name}] 건너뜀 — 클립/전사 없음")
            continue
        for vi, clips in enumerate(variants):
            b = cut_silence_with_story_filter(clips, segs, lookup, profile=b_prof)
            a = cut_silence_with_story_filter(clips, segs, lookup, profile=a_prof)
            (br, bc, bk), (ar, ac, ak) = _stats(b), _stats(a)
            for k, pair in (("removed", (br, ar)), ("cuts", (bc, ac)), ("kept", (bk, ak))):
                agg[k][0] += pair[0]
                agg[k][1] += pair[1]
            print(f"[{job_dir.name} v{vi + 1}] 제거 {br:6.1f}s → {ar:6.1f}s "
                  f"(Δ{ar - br:+.1f})  컷 {bc:3d} → {ac:3d}  길이 {bk:6.1f}s → {ak:6.1f}s "
                  f"(Δ{ak - bk:+.1f})")

    print(f"\n합계  제거 {agg['removed'][0]:.1f}s → {agg['removed'][1]:.1f}s "
          f"(Δ{agg['removed'][1] - agg['removed'][0]:+.1f})  "
          f"컷 {agg['cuts'][0]} → {agg['cuts'][1]}  "
          f"길이 {agg['kept'][0]:.1f}s → {agg['kept'][1]:.1f}s "
          f"(Δ{agg['kept'][1] - agg['kept'][0]:+.1f})")
    print("⚠ 길이 증가분은 길이 클램프(40~60s 정책)가 있는 실행에서는 다른 무음이 더 "
          "잘리는 것으로 일부 상쇄될 수 있다 — 이 도구는 silence_cut 단계만 본다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
