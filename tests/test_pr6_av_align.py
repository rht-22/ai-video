"""PR-6 — 가편집 영상 기반 dead-air 컷 좌표 변환 단위 테스트.

검증 대상 (app.modules.silence_cutter, av_align 단계용):
1. compute_clip_offsets — 각 clip 의 편집 타임라인 시작초(이전 clip 길이 누적합).
2. apply_dead_air_cuts — 편집 타임라인 dead_air_cuts 를 원본 clip 좌표로 역매핑해
   keep_intervals 재구성 + 편집좌표 remap 함수 반환. visual_essential 보호.
3. remap_timed_items — dialogue / tts_cues 의 편집좌표를 컷 반영해 보정, 컷에 사라진 항목 드롭.

좌표계 주의:
- clips 의 start_sec/end_sec 는 *원본 영상* 좌표.
- dead_air_cuts / dialogue / tts_cues 는 *가편집 타임라인*(0 = 쇼츠 시작) 좌표.
"""
from __future__ import annotations

from app.modules.gemini_client import (
    AV_ALIGN_PROMPT,
    _VALID_TTS_SPEEDS,
    _VALID_TTS_VOICES,
    _normalize_av_align_response,
    _normalize_dead_air_cuts,
    _normalize_dialogue,
    _subtract_dialogue_from_cuts,
)
from app.modules.renderer import _build_rough_cut_filter
from app.modules.silence_cutter import (
    Interval,
    apply_dead_air_cuts,
    compute_clip_offsets,
    flatten_to_clips,
    remap_timed_items,
)
from app.modules.story_builder import StoryClip
from app.pipeline import _STEP_ORDER, _step_tag


def _clip(start, end, visual_essential=False):
    return StoryClip(
        role="hook", start_sec=float(start), end_sec=float(end),
        subtitle="", use_original_audio=True,
        chunk_index=0, candidate_index=0,
        character_focus=("A",),
        visual_essential=visual_essential,
    )


# ──────────────────────────────────────────────────────────────
# compute_clip_offsets
# ──────────────────────────────────────────────────────────────


def test_offsets_empty():
    assert compute_clip_offsets([]) == []


def test_offsets_single():
    assert compute_clip_offsets([_clip(100, 120)]) == [0.0]


def test_offsets_cumulative():
    # 길이 10, 15 → 편집 시작 0, 10
    assert compute_clip_offsets([_clip(0, 10), _clip(20, 35)]) == [0.0, 10.0]


# ──────────────────────────────────────────────────────────────
# apply_dead_air_cuts — 단일 clip 내부 컷
# ──────────────────────────────────────────────────────────────


def test_single_clip_internal_cut():
    clips = [_clip(100, 120)]  # 편집 0~20 (원본 100~120)
    cuts = [{"start_sec": 5.0, "end_sec": 8.0, "reason": "대사 사이 정적"}]
    results, remap = apply_dead_air_cuts(clips, cuts)
    # 편집 5~8 → 원본 105~108 제거
    assert results[0].keep_intervals == [Interval(100.0, 105.0), Interval(108.0, 120.0)]
    assert abs(results[0].total_removed_sec - 3.0) < 1e-6
    # remap: 컷 이전 그대로, 이후 -3
    assert abs(remap(0.0) - 0.0) < 1e-6
    assert abs(remap(5.0) - 5.0) < 1e-6
    assert abs(remap(10.0) - 7.0) < 1e-6
    assert abs(remap(20.0) - 17.0) < 1e-6
    # 컷 내부(5~8)는 컷 시작으로 스냅
    assert abs(remap(6.5) - 5.0) < 1e-6


def test_visual_essential_protected():
    clips = [_clip(100, 120, visual_essential=True)]
    cuts = [{"start_sec": 5.0, "end_sec": 8.0}]
    results, remap = apply_dead_air_cuts(clips, cuts)
    # 보호 clip — 컷 무시, 통째 유지
    assert results[0].keep_intervals == [Interval(100.0, 120.0)]
    assert results[0].total_removed_sec == 0.0
    assert abs(remap(10.0) - 10.0) < 1e-6  # 컷 없음 → 항등


def test_cut_spanning_two_clips():
    clips = [_clip(0, 10), _clip(100, 110)]  # 편집 0~10, 10~20
    cuts = [{"start_sec": 8.0, "end_sec": 13.0}]  # c0 8~10 + c1 10~13
    results, remap = apply_dead_air_cuts(clips, cuts)
    assert results[0].keep_intervals == [Interval(0.0, 8.0)]
    assert results[1].keep_intervals == [Interval(103.0, 110.0)]
    assert abs(remap(13.0) - 8.0) < 1e-6   # 컷 끝 → 새 편집 8
    assert abs(remap(20.0) - 15.0) < 1e-6  # 끝 = 8 + 7


def test_short_cut_ignored():
    clips = [_clip(0, 10)]
    cuts = [{"start_sec": 5.0, "end_sec": 5.1}]  # 0.1s < min_cut_sec
    results, _ = apply_dead_air_cuts(clips, cuts)
    assert results[0].keep_intervals == [Interval(0.0, 10.0)]
    assert results[0].total_removed_sec == 0.0


def test_empty_cuts_identity():
    clips = [_clip(0, 10), _clip(20, 30)]
    results, remap = apply_dead_air_cuts(clips, [])
    assert results[0].keep_intervals == [Interval(0.0, 10.0)]
    assert results[1].keep_intervals == [Interval(20.0, 30.0)]
    assert abs(remap(15.0) - 15.0) < 1e-6


def test_overlapping_cuts_merged():
    clips = [_clip(0, 20)]
    cuts = [
        {"start_sec": 5.0, "end_sec": 9.0},
        {"start_sec": 8.0, "end_sec": 12.0},  # 겹침 → 5~12 로 병합
    ]
    results, _ = apply_dead_air_cuts(clips, cuts)
    assert results[0].keep_intervals == [Interval(0.0, 5.0), Interval(12.0, 20.0)]
    assert abs(results[0].total_removed_sec - 7.0) < 1e-6


def test_flatten_after_dead_air_cut():
    # apply → flatten_to_clips 로 분할된 StoryClip 생성 (기존 헬퍼 재사용)
    clips = [_clip(100, 120)]
    cuts = [{"start_sec": 5.0, "end_sec": 8.0}]
    results, _ = apply_dead_air_cuts(clips, cuts)
    new_clips = flatten_to_clips(results)
    assert len(new_clips) == 2
    assert (new_clips[0].start_sec, new_clips[0].end_sec) == (100.0, 105.0)
    assert (new_clips[1].start_sec, new_clips[1].end_sec) == (108.0, 120.0)


def test_full_clip_cut_guarded():
    # 컷이 clip 전체를 덮어도 과도 컷 방지로 최소 원본 유지 (전부 사라지지 않음)
    clips = [_clip(0, 10)]
    cuts = [{"start_sec": 0.0, "end_sec": 10.0}]
    results, _ = apply_dead_air_cuts(clips, cuts)
    assert results[0].keep_intervals == [Interval(0.0, 10.0)]


# ──────────────────────────────────────────────────────────────
# remap_timed_items — dialogue / tts_cues 시간 보정
# ──────────────────────────────────────────────────────────────


def test_remap_items_shifts_after_cut():
    clips = [_clip(100, 120)]
    cuts = [{"start_sec": 5.0, "end_sec": 8.0}]
    _, remap = apply_dead_air_cuts(clips, cuts)
    items = [{"start_sec": 10.0, "end_sec": 12.0, "text": "x", "speaker": "A"}]
    out = remap_timed_items(items, remap)
    assert len(out) == 1
    assert abs(out[0]["start_sec"] - 7.0) < 1e-6
    assert abs(out[0]["end_sec"] - 9.0) < 1e-6
    # 다른 필드 보존
    assert out[0]["text"] == "x"
    assert out[0]["speaker"] == "A"


def test_remap_items_drops_item_inside_cut():
    clips = [_clip(0, 20)]
    cuts = [{"start_sec": 5.0, "end_sec": 10.0}]
    _, remap = apply_dead_air_cuts(clips, cuts)
    items = [{"start_sec": 6.0, "end_sec": 7.0, "text": "사라짐"}]  # 컷 내부 → 길이 0
    out = remap_timed_items(items, remap)
    assert out == []


def test_remap_items_empty():
    _, remap = apply_dead_air_cuts([_clip(0, 10)], [])
    assert remap_timed_items([], remap) == []


# ──────────────────────────────────────────────────────────────
# Gemini av_align 응답 정규화 (gemini_client)
# ──────────────────────────────────────────────────────────────


def test_normalize_dialogue_basic():
    raw = [
        {"start_sec": 5.0, "end_sec": 7.0, "speaker": "민준", "text": "안녕"},
        {"start_sec": 1.0, "end_sec": 2.0, "speaker": "수아", "text": "왔어?"},
    ]
    out = _normalize_dialogue(raw)
    # 시간순 정렬
    assert [d["text"] for d in out] == ["왔어?", "안녕"]
    assert out[0]["speaker"] == "수아"


def test_normalize_dialogue_drops_empty_and_invalid():
    raw = [
        {"start_sec": 1.0, "end_sec": 2.0, "text": "   "},   # 빈 텍스트
        {"start_sec": 3.0, "end_sec": 3.0, "text": "0길이"},  # end<=start
        {"start_sec": 4.0, "end_sec": 5.0, "text": "정상"},
    ]
    out = _normalize_dialogue(raw)
    assert len(out) == 1
    assert out[0]["text"] == "정상"


def test_normalize_dialogue_clamps_out_of_range():
    raw = [{"start_sec": 50.0, "end_sec": 55.0, "text": "범위 밖"}]
    out = _normalize_dialogue(raw, total_duration=20.0)
    assert out == []


def test_normalize_dead_air_cuts_min_and_sort():
    raw = [
        {"start_sec": 10.0, "end_sec": 10.2, "reason": "짧음"},  # 0.2 < 0.4 드롭
        {"start_sec": 5.0, "end_sec": 6.0, "reason": "정적"},
        {"start_sec": 1.0, "end_sec": 2.0},
    ]
    out = _normalize_dead_air_cuts(raw)
    assert [c["start_sec"] for c in out] == [1.0, 5.0]


def test_normalize_dead_air_cuts_clamps_duration():
    raw = [{"start_sec": 18.0, "end_sec": 25.0}]  # total 20 → 18~20
    out = _normalize_dead_air_cuts(raw, total_duration=20.0)
    assert len(out) == 1
    assert out[0]["end_sec"] == 20.0


def test_subtract_dialogue_no_overlap_keeps_cut():
    cuts = [{"start_sec": 5.0, "end_sec": 8.0, "reason": "x"}]
    dialogue = [{"start_sec": 0.0, "end_sec": 2.0, "text": "a"}]
    out = _subtract_dialogue_from_cuts(cuts, dialogue)
    assert out == [{"start_sec": 5.0, "end_sec": 8.0, "reason": "x"}]


def test_subtract_dialogue_splits_cut_around_speech():
    # 컷 4~12 안에 대사 6~8 → 컷이 4~6, 8~12 로 쪼개짐 (대사 절대 보호)
    cuts = [{"start_sec": 4.0, "end_sec": 12.0, "reason": "x"}]
    dialogue = [{"start_sec": 6.0, "end_sec": 8.0, "text": "대사"}]
    out = _subtract_dialogue_from_cuts(cuts, dialogue)
    assert len(out) == 2
    assert (out[0]["start_sec"], out[0]["end_sec"]) == (4.0, 6.0)
    assert (out[1]["start_sec"], out[1]["end_sec"]) == (8.0, 12.0)


def test_subtract_dialogue_drops_cut_fully_inside_speech():
    # 컷 6~7 이 대사 5~9 안에 완전히 포함 → 제거
    cuts = [{"start_sec": 6.0, "end_sec": 7.0, "reason": "x"}]
    dialogue = [{"start_sec": 5.0, "end_sec": 9.0, "text": "긴 대사"}]
    out = _subtract_dialogue_from_cuts(cuts, dialogue)
    assert out == []


def test_subtract_dialogue_trims_partial_overlap_below_min_dropped():
    # 컷 5~8, 대사 7.8~9 → 남는 5~7.8 (>=0.4 유지)
    cuts = [{"start_sec": 5.0, "end_sec": 8.0, "reason": "x"}]
    dialogue = [{"start_sec": 7.8, "end_sec": 9.0, "text": "대사"}]
    out = _subtract_dialogue_from_cuts(cuts, dialogue)
    assert len(out) == 1
    assert out[0]["start_sec"] == 5.0
    assert abs(out[0]["end_sec"] - 7.8) < 1e-6


def test_av_align_prompt_formats_without_error():
    # 프롬프트의 자리표시자/중괄호 이스케이프가 깨지지 않는지 (format KeyError/IndexError 방지)
    out = AV_ALIGN_PROMPT.format(
        work_title="비밀의 정원",
        character_names="라임, 주원",
        context="재회 장면",
        total_duration=58.0,
        voice_options=", ".join(sorted(_VALID_TTS_VOICES)),
        speed_options=", ".join(sorted(_VALID_TTS_SPEEDS)),
        tts_drafts_block="  - 그날의 진실",
    )
    assert "비밀의 정원" in out
    assert "58.0" in out
    assert "그날의 진실" in out
    # JSON 예시의 이중 중괄호가 포맷 후 단일 중괄호 키로 살아있어야 함
    assert '"dialogue"' in out
    assert '"dead_air_cuts"' in out


# ──────────────────────────────────────────────────────────────
# 가편집(rough cut) 필터 그래프 구성 (renderer)
# ──────────────────────────────────────────────────────────────


def test_rough_cut_filter_no_timecode():
    filt, vmap = _build_rough_cut_filter(2, height=480, fps=10, timecode_font_arg=None)
    assert vmap == "[vc]"
    assert "scale=-2:480" in filt
    assert "fps=10" in filt
    # 클립 2개 → concat 입력 라벨 + n=2
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vc][aout]" in filt
    assert "drawtext" not in filt


def test_rough_cut_filter_with_timecode():
    filt, vmap = _build_rough_cut_filter(1, height=480, fps=10, timecode_font_arg="font.ttf")
    assert vmap == "[vout]"
    # 타임코드 번인 — 폰트 + pts 표현식
    assert "drawtext=fontfile='font.ttf'" in filt
    assert "pts" in filt
    assert filt.rstrip().endswith("[vout]")


def test_rough_cut_filter_single_clip_concat():
    filt, _ = _build_rough_cut_filter(1, height=360, fps=8, timecode_font_arg=None)
    assert "[v0][a0]concat=n=1:v=1:a=1[vc][aout]" in filt
    assert "scale=-2:360" in filt


def test_normalize_av_align_response_integration():
    data = {
        "dialogue": [{"start_sec": 6.0, "end_sec": 8.0, "speaker": "A", "text": "대사"}],
        "tts_cues": [{"start_sec": 0.5, "end_sec": 3.0, "text": "나레이션", "voice": "ko_female", "speed": "normal"}],
        "dead_air_cuts": [{"start_sec": 4.0, "end_sec": 12.0, "reason": "정적"}],  # 대사 6~8 포함
    }
    out = _normalize_av_align_response(data, total_duration=20.0)
    assert len(out["dialogue"]) == 1
    assert len(out["tts_cues"]) == 1
    # dead_air_cut 이 대사 구간을 피해 4~6, 8~12 로 쪼개짐
    assert len(out["dead_air_cuts"]) == 2
    assert (out["dead_air_cuts"][0]["start_sec"], out["dead_air_cuts"][0]["end_sec"]) == (4.0, 6.0)
    assert (out["dead_air_cuts"][1]["start_sec"], out["dead_air_cuts"][1]["end_sec"]) == (8.0, 12.0)


# ──────────────────────────────────────────────────────────────
# 파이프라인 단계 순서 / 진행 로그 태그 (pipeline)
# ──────────────────────────────────────────────────────────────


def test_step_order_av_align_replaces_silence_cut():
    # PR-6: silence_cut 단계가 av_align 으로 완전 대체됨
    assert "av_align" in _STEP_ORDER
    assert "silence_cut" not in _STEP_ORDER
    # av_align 은 story 와 resources 사이
    assert _STEP_ORDER.index("story") < _STEP_ORDER.index("av_align") < _STEP_ORDER.index("resources")


def test_step_tag_dynamic_numbering():
    n = len(_STEP_ORDER)
    assert _step_tag("init") == f"[1/{n}]"
    assert _step_tag("validate") == f"[{n}/{n}]"
    assert _step_tag("av_align") == f"[{_STEP_ORDER.index('av_align') + 1}/{n}]"
