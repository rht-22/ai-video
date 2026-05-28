#!/usr/bin/env python3
"""[5-mid/10] character_index Phase A 단위 검증.

Gemini API를 호출하지 않고 다음 4단계를 실제 데이터로 검증:
  1. FaceIdentifier.build_appearance_index() — proxy + cast_images로 인덱스 생성
  2. chunk-level 필터/오프셋 변환 — 임의 chunk에서 chunk_appearances 범위 체크
  3. GEMINI_PROMPT_TEMPLATE.format() — character_appearances_block 직렬화 노출 확인
  4. find_target_in_index() — 인덱스 lookup (정상 hit / character miss / time miss)

사용법:
    .venv/bin/python scripts/verify_character_index.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.modules.face_id import FaceIdentifier, find_target_in_index  # noqa: E402
from app.modules.gemini_client import GEMINI_PROMPT_TEMPLATE  # noqa: E402

JOB_DIR = Path("/Users/gimsewon/rhoonart/ai-video/outputs/콘크리트_마켓_0a")
PROXY = JOB_DIR / "콘크리트 마켓_480.mp4"


@dataclass
class CastChar:
    character_name: str
    actor_name: str
    image_path: Path


def _load_cast() -> list[CastChar]:
    research = json.loads((JOB_DIR / "checkpoint_research.json").read_text(encoding="utf-8"))
    return [
        CastChar(c["character_name"], c["actor_name"], Path(c["image_path"]))
        for c in research.get("cast_images", [])
        if c.get("image_path") and Path(c["image_path"]).exists()
    ]


def step1_build_index() -> list[dict]:
    print("\n[1/4] build_appearance_index 실행")
    cast = _load_cast()
    print(f"  - cast_images: {len(cast)}명")
    assert PROXY.exists(), f"proxy 영상 없음: {PROXY}"

    fi = FaceIdentifier()
    fi.build_references(cast)
    print(f"  - 레퍼런스 등록: {len(fi.references)}명")
    assert len(fi.references) >= 2, "레퍼런스 ≥ 2명 필요"

    appearances = fi.build_appearance_index(PROXY, sample_interval_sec=2.0)
    out = JOB_DIR / "checkpoint_character_index.json"
    out.write_text(json.dumps(appearances, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  - {len(appearances)}개 등장 구간 → {out}")

    by_char: dict[str, float] = {}
    sample_total = 0
    for a in appearances:
        by_char[a["character"]] = by_char.get(a["character"], 0.0) + (a["end_sec"] - a["start_sec"])
        sample_total += len(a.get("samples") or [])
    print(f"  - 샘플 좌표 총: {sample_total}")
    print("  - 인물별 누적 시간 (초):")
    for name, secs in sorted(by_char.items(), key=lambda x: -x[1]):
        print(f"      {name}: {secs:.1f}s")

    probe = json.loads((JOB_DIR / "checkpoint_probe.json").read_text(encoding="utf-8"))
    duration = float(probe["duration_sec"])
    total = sum(by_char.values())
    print(f"  - face_id 커버리지: {total/duration*100:.1f}% of {duration:.1f}s")

    assert len(appearances) >= 10, f"등장 구간이 너무 적음: {len(appearances)}"
    assert len(by_char) >= 2, f"인물 종류가 너무 적음: {len(by_char)}"
    # samples 검증: 최소 한 개 항목에 samples가 있어야 하고, 정규화 좌표 [0,1]
    has_samples = False
    for a in appearances:
        for smp in a.get("samples") or []:
            has_samples = True
            assert 0.0 <= smp["x_norm"] <= 1.0, f"x_norm 범위 이탈: {smp}"
            assert 0.0 <= smp["y_norm"] <= 1.0, f"y_norm 범위 이탈: {smp}"
    assert has_samples, "samples가 비어있음 — build_appearance_index 변경이 반영되지 않음"
    print("  ✅ Phase 1 PASS")
    return appearances


def step2_chunk_filter(appearances: list[dict]) -> dict:
    print("\n[2/4] chunk 필터/오프셋 변환 검증")
    probe = json.loads((JOB_DIR / "checkpoint_probe.json").read_text(encoding="utf-8"))
    duration = float(probe["duration_sec"])
    chunk_sec, overlap = 300.0, 10.0

    chunks: list[dict] = []
    t = 0.0
    while t < duration:
        chunks.append({
            "index": len(chunks),
            "start_sec": t,
            "end_sec": min(t + chunk_sec, duration),
            "actual_start_sec": t,
        })
        t += chunk_sec - overlap

    target = chunks[len(chunks) // 2]
    chunk_offset = target["actual_start_sec"]
    chunk_appearances: list[dict] = []
    for ap in appearances:
        if ap["end_sec"] <= target["start_sec"] or ap["start_sec"] >= target["end_sec"]:
            continue
        s = max(ap["start_sec"], target["start_sec"]) - chunk_offset
        e = min(ap["end_sec"], target["end_sec"]) - chunk_offset
        if e <= s:
            continue
        chunk_appearances.append({
            "character": ap["character"],
            "start_sec": float(s),
            "end_sec": float(e),
        })

    chunk_len = target["end_sec"] - target["start_sec"]
    print(f"  - target chunk: index={target['index']} {target['start_sec']:.1f}~{target['end_sec']:.1f}s (len={chunk_len:.1f})")
    print(f"  - chunk 안 등장 구간: {len(chunk_appearances)}개")
    for ca in chunk_appearances[:5]:
        print(f"      {ca['character']}: {ca['start_sec']:.1f}~{ca['end_sec']:.1f}s")
        assert 0 <= ca["start_sec"] < ca["end_sec"] <= chunk_len + 1e-3, f"범위 이탈: {ca}"
    print("  ✅ Phase 2 PASS")
    return {"chunk": target, "chunk_appearances": chunk_appearances}


def step3_prompt_serialize(filt: dict) -> None:
    print("\n[3/4] 프롬프트 직렬화 검증")
    chunk_appearances = filt["chunk_appearances"]

    if chunk_appearances:
        ap_lines = [
            f"- {a.get('character','?')}: {float(a.get('start_sec',0)):.1f}~{float(a.get('end_sec',0)):.1f}초"
            for a in chunk_appearances
        ]
        block = (
            "\n[face_id 사전 인식 결과 — 참고용]\n"
            "외부 얼굴 인식기가 추정한 캐릭터 등장 구간이다. 같은 인물명을 라벨로 일관되게 사용하되, "
            "픽셀에서 명백히 다르게 보이는 경우 영상 분석 결과를 우선한다.\n"
            + "\n".join(ap_lines)
        )
    else:
        block = ""

    prompt = GEMINI_PROMPT_TEMPLATE.format(
        work_title="콘크리트 마켓",
        topic="",
        chunk_start_sec=filt["chunk"]["start_sec"],
        chunk_end_sec=filt["chunk"]["end_sec"],
        work_context_block="",
        narrative_skeleton_block="",
        previous_episodes_context_block="",
        character_appearances_block=block,
        transcript_text="없음",
        scene_boundaries="없음",
        transcript_hint="없음",
        previous_context="",
        min_candidates=3,
    )

    assert "[face_id 사전 인식 결과 — 참고용]" in prompt, "face_id 헤더 누락"
    assert "**face_id 사전 인식 결과**" in prompt, "[인물 식별 단계]에 face_id 라벨 규칙 누락"
    if chunk_appearances:
        assert any("초" in line for line in block.splitlines()), "인물 라인 누락"

    idx = prompt.find("[face_id 사전 인식 결과 — 참고용]")
    excerpt = prompt[idx: idx + 600]
    print("  - 프롬프트 발췌:")
    for ln in excerpt.splitlines():
        print("    " + ln)
    print("  ✅ Phase 3 PASS")


def step4_lookup(appearances: list[dict]) -> None:
    print("\n[4/4] find_target_in_index 검증")
    if not appearances:
        print("  ⚠️ appearances 비어있음 — 검증 불가")
        return

    sample_with_data = next(
        (a for a in appearances if a.get("samples")),
        None,
    )
    assert sample_with_data is not None, "samples를 가진 항목 없음"
    target_char = sample_with_data["character"]
    smp = sample_with_data["samples"][len(sample_with_data["samples"]) // 2]
    t_query = smp["t"]

    # 원본 해상도 가정 (콘크리트 마켓 EP03)
    probe = json.loads((JOB_DIR / "checkpoint_probe.json").read_text(encoding="utf-8"))
    fw, fh = int(probe["width"]), int(probe["height"])
    print(f"  - 원본 해상도: {fw}x{fh}, 타겟: {target_char}, t={t_query:.1f}s")

    hit = find_target_in_index(appearances, target_char, t_query, fw, fh)
    assert hit is not None, f"hit 실패 (정확 sample t={t_query})"
    x, y = hit
    print(f"  - hit: ({x:.1f}, {y:.1f}) [in 0~{fw} × 0~{fh}]")
    assert 0 <= x <= fw and 0 <= y <= fh, "픽셀 좌표 범위 이탈"

    # +1초 시점 — 가장 가까운 sample이 max_dt_sec(3s) 안이므로 hit
    near = find_target_in_index(appearances, target_char, t_query + 1.0, fw, fh)
    assert near is not None, "근접 시간 hit 실패"
    print(f"  - near (+1.0s) hit: ({near[0]:.1f}, {near[1]:.1f})")

    # 존재하지 않는 인물 → miss
    miss_char = find_target_in_index(appearances, "__not_a_character__", t_query, fw, fh)
    assert miss_char is None, "존재하지 않는 인물 lookup이 hit (miss여야 함)"

    # 영상 끝 +100s 시점 → miss (max_dt_sec=3.0 초과)
    duration = float(probe["duration_sec"])
    miss_time = find_target_in_index(appearances, target_char, duration + 100.0, fw, fh)
    assert miss_time is None, "먼 시간 lookup이 hit (miss여야 함)"
    print("  - miss(존재안함/먼시간) 모두 None 반환")
    print("  ✅ Phase 4 PASS")


def main() -> None:
    appearances = step1_build_index()
    filt = step2_chunk_filter(appearances)
    step3_prompt_serialize(filt)
    step4_lookup(appearances)
    print("\n=== Phase A 검증 모두 PASS ===")


if __name__ == "__main__":
    main()
