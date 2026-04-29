#!/usr/bin/env python3
"""TTS Planner Phase A 단위 검증.

Gemini API를 호출하지 않고 다음을 검증:
  1. synthesize_tts(voice, speed) — 4 voice × 5 speed 일부 조합으로 mp3 생성
  2. TTSCue dataclass JSON round-trip
  3. _build_audio_filter / _build_input_args — 가짜 cue로 ffmpeg 필터/인자 생성 검증
  4. STORY_COMPOSITION_PROMPT / TTS_PLANNING_PROMPT format() 무결성

사용법: .venv/bin/python scripts/verify_tts_planner.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.modules.tts import (  # noqa: E402
    synthesize_tts,
    VOICE_PRESETS,
    SPEED_TO_RATE,
    DEFAULT_VOICE,
    DEFAULT_SPEED,
)
from app.modules.story_builder import TTSCue, StoryClip  # noqa: E402
from app.modules.gemini_client import (  # noqa: E402
    GEMINI_PROMPT_TEMPLATE,
    STORY_COMPOSITION_PROMPT,
    TTS_PLANNING_PROMPT,
)
from app.modules.renderer import RenderInputs, _build_audio_filter, _build_filtergraph  # noqa: E402
from app.config import DesignConfig  # noqa: E402


TMP = REPO / "outputs" / "_tts_verify"
TMP.mkdir(parents=True, exist_ok=True)


def _ffprobe_duration(p: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, check=True,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def step1_synth_combinations() -> None:
    print("\n[1/4] synthesize_tts: voice × speed 조합 검증")
    text = "테스트 음성입니다. 약 두 문장 정도의 길이."
    cases = [
        ("narrative_female", "normal"),
        ("narrative_male", "slow"),
        ("dramatic_low", "very_slow"),
        ("bright_high", "fast"),
        ("narrative_female", "very_fast"),
    ]
    for voice, speed in cases:
        out = TMP / f"sample_{voice}_{speed}.mp3"
        synthesize_tts(text, out, lang="ko", voice=voice, speed=speed)
        assert out.exists() and out.stat().st_size > 1024, f"mp3 생성 실패: {out}"
        dur = _ffprobe_duration(out)
        rate = SPEED_TO_RATE[speed]
        voice_id, pitch = VOICE_PRESETS[voice]
        print(f"  - {voice} ({voice_id}, pitch={pitch}) / {speed} ({rate}) → {dur:.2f}s, {out.stat().st_size} bytes")
        assert dur > 0.5, f"mp3 길이 비정상: {dur:.2f}s"
    print("  ✅ Phase 1 PASS")


def step2_cue_roundtrip() -> None:
    print("\n[2/4] TTSCue dataclass round-trip")
    cue = TTSCue(start_sec=3.5, end_sec=8.2, text="단호한 한 마디.", voice="dramatic_low", speed="slow")
    j = json.dumps(asdict(cue), ensure_ascii=False)
    back = TTSCue(**json.loads(j))
    assert back == cue
    print(f"  - JSON: {j}")
    print(f"  - round-trip 동등: {back == cue}")

    # 시간 정합
    assert cue.start_sec < cue.end_sec
    print("  ✅ Phase 2 PASS")


def step3_filter_graph() -> None:
    print("\n[3/4] _build_audio_filter / _build_filtergraph (가짜 cue)")
    cue_files = [
        {"cue_index": 0, "path": str(TMP / "sample_narrative_female_normal.mp3"),
         "cue": {"start_sec": 0.0, "end_sec": 3.5, "text": "오프닝 멘트.",
                 "voice": "narrative_female", "speed": "normal"}},
        {"cue_index": 1, "path": str(TMP / "sample_dramatic_low_very_slow.mp3"),
         "cue": {"start_sec": 12.0, "end_sec": 16.0, "text": "위협적 한 마디.",
                 "voice": "dramatic_low", "speed": "very_slow"}},
        {"cue_index": 2, "path": str(TMP / "sample_bright_high_fast.mp3"),
         "cue": {"start_sec": 30.0, "end_sec": 33.0, "text": "임팩트.",
                 "voice": "bright_high", "speed": "fast"}},
    ]
    fake_clips = [
        StoryClip(role="hook", start_sec=100.0, end_sec=110.0, subtitle="…",
                  use_original_audio=True),
        StoryClip(role="payoff", start_sec=200.0, end_sec=235.0, subtitle="…",
                  use_original_audio=True),
    ]
    inputs = RenderInputs(
        video_path=Path("/dev/null"),
        clips=fake_clips,
        subtitle_path=None,
        crop_timeline_map={},
        title_text="t",
        work_title="w",
        output_path=TMP / "fake_out.mp4",
        canvas_width=1080,
        canvas_height=1920,
        top_title_height=120,
        bottom_label_height=120,
        design=DesignConfig(),
        tts_cue_files=cue_files,
    )

    f = _build_audio_filter(inputs, num_clip_inputs=2, num_cue_inputs=3)
    print("  - audio filter:")
    for line in f.split(";"):
        print("      " + line)

    # 검증: 각 cue별 adelay 또는 cue0_vol(start=0인 경우) 확인
    assert "[2:a]volume=" in f, "cue 0 (input idx 2) 볼륨 필터 누락"  # num_clip_inputs=2 + 0
    assert "[3:a]volume=" in f, "cue 1 (input idx 3) 볼륨 필터 누락"
    assert "[4:a]volume=" in f, "cue 2 (input idx 4) 볼륨 필터 누락"
    assert "adelay=12000|12000" in f, "cue 1 adelay 절대시간 12초 누락"
    assert "adelay=30000|30000" in f, "cue 2 adelay 절대시간 30초 누락"
    assert "atempo" not in f, "atempo는 cue 기반 모델에선 제거되어야 한다"
    assert "between(t,0.000,3.500)" in f, "duck range cue0 누락"
    assert "between(t,12.000,16.000)" in f, "duck range cue1 누락"
    assert "amix=inputs=4" in f, "amix 입력 수 (orig + cue×3 = 4) 누락"

    # filtergraph 전체에 ass 합성 등 들어가는지 (간단 확인)
    full = _build_filtergraph(inputs, num_clip_inputs=2, num_cue_inputs=3)
    assert "amix=inputs=4" in full
    print("  ✅ Phase 3 PASS")


def step4_format_prompts() -> None:
    print("\n[4/4] 3개 프롬프트 format() 무결성")
    g = {"work_title": "X", "topic": "", "chunk_start_sec": 0, "chunk_end_sec": 10,
         "work_context_block": "", "narrative_skeleton_block": "",
         "previous_episodes_context_block": "", "character_appearances_block": "",
         "transcript_text": "", "scene_boundaries": "", "transcript_hint": "",
         "previous_context": "", "min_candidates": 3}
    GEMINI_PROMPT_TEMPLATE.format(**g)

    s = {"work_title": "X", "topic": "", "min_duration_sec": 50, "max_duration_sec": 75,
         "work_context_block": "", "episodes_context_block": "",
         "narrative_skeleton_json_block": "", "candidates_str": "[]", "story_topic_line": ""}
    STORY_COMPOSITION_PROMPT.format(**s)

    t = {"work_title": "X", "total_duration": 60.0, "clips_str": "- clip 0",
         "work_context_block": "", "episodes_context_block": "",
         "narrative_skeleton_json_block": ""}
    TTS_PLANNING_PROMPT.format(**t)

    # STORY_COMPOSITION_PROMPT에서 tts_line 표현이 사라졌는지
    assert '"tts_line"' not in STORY_COMPOSITION_PROMPT, "tts_line 출력 필드가 STORY_COMPOSITION_PROMPT에 잔존"
    print("  - GEMINI / STORY / TTS_PLANNING format OK")
    print("  - STORY_COMPOSITION_PROMPT에 tts_line 잔존 없음")
    print("  ✅ Phase 4 PASS")


def main() -> None:
    step1_synth_combinations()
    step2_cue_roundtrip()
    step3_filter_graph()
    step4_format_prompts()
    print("\n=== TTS Planner Phase A 검증 모두 PASS ===")


if __name__ == "__main__":
    main()
