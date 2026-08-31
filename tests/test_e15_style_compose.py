"""E15 — 스타일 구성 단계(AI 연출 플랜) 회귀 가드.

기획: ves-orchestrator `docs/prompts/e15-style-compose.md`.

이 테스트가 고정하는 계약:
· **회귀 0** — `--style-compose` 미지정이면 단계가 통째로 없다(플래그 기본값·자막 캐시 모양).
· **우선순위** — 편집실 > 채널 명시 design 키 > AI 플랜 > 기본값.
· **AI 에게만 좁은 규칙** — 자막 강조는 size·color 뿐, 배속은 안 열린다, 라벨은 불변 계약.
· **v3 재사용** — texts·images 는 v3 검증기가 그대로 거른다(따로 만든 검증이 아니다).
· **모델 정책(2026-08-23)** — Pro 는 영상 분석 하나뿐, 나머지는 Flash 최신.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.cli import _build_design_config, build_parser
from app.config import DesignConfig
from app.modules import style_compose as sc
from app.modules.story_builder import StoryClip


# ── 픽스처 ────────────────────────────────────────────────────────────────
def _clips():
    """원본 100~120s, 200~210s → 편집본 0~20s, 20~30s."""
    return [
        StoryClip(role="hook", start_sec=100.0, end_sec=120.0, subtitle="",
                  use_original_audio=True),
        StoryClip(role="payoff", start_sec=200.0, end_sec=210.0, subtitle="",
                  use_original_audio=True),
    ]


def _plan(**over):
    base = {"schema": "style_plan/v1"}
    base.update(over)
    return base


def _valid(plan, tmp_path, manifest=None, app_root=None):
    return sc.validate_plan(plan, manifest=manifest or {},
                            app_root=app_root or tmp_path, run_dir=tmp_path)


def _render_prompt(prompt: str) -> str:
    """STYLE_COMPOSITION_PROMPT 를 더미 값으로 채워 완성된 문장을 본다.

    포맷 인자가 늘면 **여기만** 고친다(compose_style 의 .format 과 같은 키여야 한다 —
    빠뜨리면 KeyError 로 여기서 먼저 걸린다).
    """
    return prompt.format(
        fonts="", sub_lo="", sub_hi="", voices="", speeds="",
        text_y_lo="0.34", text_y_hi="0.66",
        title_line_max=sc.MAX_TITLE_LINE_CHARS,
        max_texts=0, max_images=0, max_subs=0, max_titles=0,
        work_title="", title_text="",
        timeline_block="", transcript_block="", cues_block="", stickers_block="")


def _design(*extra):
    p = build_parser()
    args = p.parse_args(["create_shorts", "--title", "T", "--video", "x.mp4",
                         "--subtitle", "x.srt", *extra])
    return args


# ══════════════════════════════════════════════════════════════════════════
# 회귀 0 — 미지정이면 단계 자체가 없다
# ══════════════════════════════════════════════════════════════════════════
def test_style_compose_off_by_default():
    """PipelineInput 기본값이 꺼짐이어야 auto_update 6대가 종전대로 돈다."""
    from app.pipeline import PipelineInput
    assert PipelineInput.__dataclass_fields__["style_compose"].default is False
    args = _design()
    assert getattr(args, "style_compose", False) is False


def test_cli_flag_and_from_step():
    args = _design("--style-compose")
    assert args.style_compose is True
    # --from-step style 이 argparse 화이트리스트에 있어야 재개가 된다
    assert _design("--from-step", "style", "--job-id", "j").from_step == "style"


def test_subtitle_cache_shape_unchanged_without_style():
    """강조가 없으면 subtitle_segments.json 한 줄은 종전과 **키까지** 같아야 한다."""
    from app.pipeline import _subtitle_segment_json
    seg = SimpleNamespace(start_sec=1.0, end_sec=2.0, text="가")
    assert _subtitle_segment_json(seg) == {"start_sec": 1.0, "end_sec": 2.0, "text": "가"}
    # 강조가 있으면 그 줄에만 v3 와 같은 키로 실린다
    seg.style = {"size": 78.0, "color": "#FF4444"}
    assert _subtitle_segment_json(seg)["style"] == {"size": 78.0, "color": "#FF4444"}


def test_style_step_sits_between_silence_cut_and_resources():
    """자리가 계약이다 — 앵커 기준 클립이 확정된 뒤, TTS 합성보다 앞."""
    import inspect
    from app import pipeline
    src = inspect.getsource(pipeline.run_pipeline)
    order = src.split("step_order = [", 1)[1].split("\n    ]", 1)[0]
    names = [ln.split('"')[1] for ln in order.splitlines()
             if '"' in ln and not ln.strip().startswith("#")]
    assert names.index("silence_cut") < names.index("style") < names.index("resources")


def test_pipeline_block_guards_every_category_with_editor_override():
    """§5 우선순위는 모듈이 아니라 **파이프라인 블록**이 강제한다 — 다섯 카테고리 전부
    '편집실이 보냈으면 AI 는 진다' 가드를 달고 있어야 한다(하나라도 빠지면 사람이 지운
    연출이 살아 돌아온다)."""
    import inspect
    from app import pipeline
    src = inspect.getsource(pipeline.run_pipeline)
    block = src.split("[style] AI 연출 구성 (E15", 1)[1].split("[13/15] 리소스 생성", 1)[0]
    assert 'get("tts") and _tts_override is None' in block
    assert 'get("subtitle_styles") and _sub_override is None' in block
    assert 'get("texts") and not _text_overlays' in block
    assert 'get("images") and not _image_overlays' in block
    assert 'get("title_segments") and not _title_segments' in block
    # 채널 명시 키 방어는 design_overrides 가 payload.design_explicit_fields 로 한다
    assert "payload.design_explicit_fields" in block


def test_missing_sticker_file_does_not_kill_the_render():
    """체크포인트만 남고 style_assets/ 가 사라진 재개(번들 복원)에서 연출 하나 때문에
    본편 렌더가 죽으면 안 된다 — 편집실 이미지(크게 실패해야 맞다)와 반대 방향이다."""
    import inspect
    from app import pipeline
    src = inspect.getsource(pipeline.run_pipeline)
    block = src.split("[style] AI 연출 구성 (E15", 1)[1].split("[13/15] 리소스 생성", 1)[0]
    seg = block.split("resolve_image_files", 1)[0][-400:]
    assert "try:" in seg                       # 감싸지 않으면 EditOverrideError 가 렌더를 죽인다
    assert "스티커 없이 진행" in block


def test_style_checkpoint_is_not_recalled_on_editor_rerender():
    """편집실 재렌더가 매번 다른 연출을 내면 사람이 승인한 화면이 재렌더마다 달라진다."""
    import inspect
    from app import pipeline
    src = inspect.getsource(pipeline.run_pipeline)
    block = src.split("[style] AI 연출 구성 (E15", 1)[1].split("[13/15] 리소스 생성", 1)[0]
    assert 'checkpoint_style.exists() and start_idx > step_idx["style"]' in block
    assert "재호출 없음" in block


def test_resume_invalidations_include_style():
    """style 재개는 자막·리소스 캐시를 무효화해야 한다(silence_cut 사고 재발 방지)."""
    import inspect
    from app import pipeline
    src = inspect.getsource(pipeline.run_pipeline)
    sub = src.split("_subtitle_invalidate = (from_step in (", 1)[1].split(")", 1)[0]
    res = src.split("_resources_invalidate = (from_step in (", 1)[1].split(")", 1)[0]
    assert '"style"' in sub and '"style"' in res


# ══════════════════════════════════════════════════════════════════════════
# 검증 — v3 재사용 + AI 전용 좁은 규칙
# ══════════════════════════════════════════════════════════════════════════
def test_valid_plan_normalizes_like_v3(tmp_path):
    plan = _plan(texts=[{"text": "쿵!", "source_time_sec": 105.0, "duration_sec": 1.2,
                         "x": 0.7, "y": 0.25, "size": 96, "color": "#ffdd00",
                         "fx": "pop", "reason": "타격감"}])
    out, notes = _valid(plan, tmp_path)
    t = out["texts"][0]
    # overrides_texts 가 v3 기본값을 채운다 — 별도 정규화를 만들지 않았다는 증거
    assert t["stroke"] == "dark" and t["font"] == "Jalnan" and t["rotate"] == 0.0
    assert t["color"] == "#FFDD00"                 # v3 와 같은 대문자 정규화
    assert "reason" not in t                       # 플랜 전용 필드는 적용 전에 떨어진다
    assert notes == []


def test_unknown_top_level_key_rejected(tmp_path):
    with pytest.raises(sc.StylePlanError):
        _valid(_plan(bgm=[{"file": "x.mp3"}]), tmp_path)


def test_wrong_schema_rejected(tmp_path):
    with pytest.raises(sc.StylePlanError):
        _valid({"schema": "style_plan/v2"}, tmp_path)


def test_text_rules_come_from_v3_validator(tmp_path):
    """폰트 화이트리스트·범위는 v3 검증기가 본다 — 여기서 따로 만들지 않았다."""
    bad_font = _plan(texts=[{"text": "가", "source_time_sec": 105.0, "duration_sec": 1.0,
                             "x": 0.5, "y": 0.3, "font": "Arial"}])
    with pytest.raises(sc.StylePlanError):
        _valid(bad_font, tmp_path)
    bad_size = _plan(texts=[{"text": "가", "source_time_sec": 105.0, "duration_sec": 1.0,
                             "x": 0.5, "y": 0.3, "size": 900}])
    with pytest.raises(sc.StylePlanError):
        _valid(bad_size, tmp_path)


def test_subtitle_emphasis_is_size_and_color_only(tmp_path):
    """위치·회전은 사람 전용 — v3 는 넷을 허용하지만 AI 에겐 둘만 연다(§13-4)."""
    assert sc.STYLE_SUBTITLE_KEYS == ("size", "color")
    ok = _plan(subtitle_styles=[{"source_time_sec": 105.0,
                                 "style": {"size": 78, "color": "#FF4444"}}])
    assert _valid(ok, tmp_path)[0]["subtitle_styles"][0]["style"]["size"] == 78.0
    for bad_key in ("y", "rotate"):
        with pytest.raises(sc.StylePlanError):
            _valid(_plan(subtitle_styles=[{"source_time_sec": 105.0,
                                           "style": {bad_key: 0.5}}]), tmp_path)


def test_subtitle_emphasis_size_bounded(tmp_path):
    """화면을 덮는 크기를 막는다 — v3(사람)엔 상한이 없지만 AI 산출엔 있다."""
    with pytest.raises(sc.StylePlanError):
        _valid(_plan(subtitle_styles=[{"source_time_sec": 105.0,
                                       "style": {"size": 400}}]), tmp_path)


def test_tts_labels_are_the_immutable_contract(tmp_path):
    """라벨은 tts.py 에서 가져온다 — 문자열을 베끼면 언젠가 어긋난다."""
    from app.modules.tts import SPEED_TO_RATE, VOICE_PRESETS
    assert sc.STYLE_VOICES == tuple(VOICE_PRESETS)
    assert sc.STYLE_SPEEDS == tuple(SPEED_TO_RATE)
    ok = _plan(tts=[{"source_time_sec": 105.0, "voice": "ko_male_low", "speed": "slow"}])
    assert _valid(ok, tmp_path)[0]["tts"][0]["voice"] == "ko_male_low"
    with pytest.raises(sc.StylePlanError):
        _valid(_plan(tts=[{"source_time_sec": 105.0, "voice": "ko_female_soft"}]), tmp_path)


def test_elevenlabs_voice_id_not_allowed_from_ai(tmp_path):
    """E12 접두사는 계정 종속이라 사람(대시보드)이 고르는 값이다."""
    with pytest.raises(sc.StylePlanError):
        _valid(_plan(tts=[{"source_time_sec": 105.0,
                           "voice": "elevenlabs:abcdefghij123456"}]), tmp_path)


def test_video_speed_is_not_ai_openable(tmp_path):
    """배속은 렌더 효과가 아니라 **길이 예산**(클램프 ×S)이라 style 단계가 못 만진다.

    style 은 길이 클램프가 끝난 뒤에 도는데 거기서 배속을 바꾸면 40~60초 정책이
    적용된 편의 출력 길이만 조용히 달라진다(기획서 §4 를 코드 실측으로 뒤집은 지점).
    """
    assert "video_speed" not in sc.STYLE_DESIGN_ALLOWED
    with pytest.raises(sc.StylePlanError):
        _valid(_plan(design={"video_speed": 1.1}), tmp_path)


def test_design_rotate_range(tmp_path):
    assert _valid(_plan(design={"tts_rotate": -3}), tmp_path)[0]["design"]["tts_rotate"] == -3.0
    with pytest.raises(sc.StylePlanError):
        _valid(_plan(design={"tts_rotate": 200}), tmp_path)


def test_ai_cannot_rotate_the_title_at_all(tmp_path):
    """E18(3차) — 제목 기울기는 **AI 에게 닫혀 있다**.

    지시가 세 번 바뀐 키다: ① 통째로 차단 → ② ±15° 로 완화(E17-1) → ③ **다시 차단**
    ("제목은 회전하지 않도록 되는지 확인해서 ai가 회전을 못하게 해야돼", 2026-08-24).
    ②로 열어 둔 범위로도 매 편 기울어져 나와서 범위가 아니라 키를 닫았다.
    """
    assert "title_rotate" not in sc.STYLE_DESIGN_ALLOWED
    out, notes = _valid(_plan(design={"title_rotate": -3, "title_box": "round"}), tmp_path)
    assert "title_rotate" not in out["design"]          # 버려진다
    assert out["design"]["title_box"] == "round"        # 나머지 키는 그대로 산다
    assert any("title_rotate" in n for n in notes)      # 조용한 드롭 금지


def test_a_rotated_title_does_not_kill_the_whole_plan(tmp_path):
    """닫는 방식이 '모르는 키'가 아니라 **드롭+메모**인 이유를 못박는다.

    STYLE_DESIGN_ALLOWED 에서 빼기만 하면 unknown 검사가 플랜 **전체**를 거절한다 —
    LLM 은 이 키를 계속 낼 테고, 그때마다 효과 텍스트·제목 창까지 통째로 날아간다."""
    plan = _plan(design={"title_rotate": 30})
    plan["texts"] = [{"text": "쿵!", "source_time_sec": 10.0, "duration_sec": 1.0,
                      "x": 0.5, "y": 0.5}]
    out, _ = _valid(plan, tmp_path)
    assert len(out["texts"]) == 1                        # 플랜은 살아남는다
    assert not out.get("design")                         # 기울기만 사라졌다
    # tts_rotate 는 그대로 ±180 — 지시는 제목에 대한 것이다
    assert _valid(_plan(design={"tts_rotate": 90}), tmp_path)[0]["design"]["tts_rotate"] == 90.0


def test_an_old_checkpoint_cannot_resurrect_a_rotated_title():
    """옛 체크포인트(E17-1 시절 ±15°)는 재검증 없이 재적용된다 — 마지막 관문에서 막는다."""
    base = DesignConfig()
    kw, notes = sc.design_overrides({"title_rotate": -12.0, "tts_rotate": 5.0}, set(), base)
    assert kw == {"tts_rotate": 5.0}
    assert any("title_rotate" in n for n in notes)


def test_prompt_forbids_title_rotation_and_names_the_band_y_range():
    """프롬프트도 같은 계약을 말해야 한다 — 검증기만 고치면 LLM 이 계속 같은 값을 낸다."""
    from app.modules.gemini_client import STYLE_COMPOSITION_PROMPT as P

    assert "제목은 기울이지 않는다" in P
    filled = _render_prompt(P)
    assert "0.34~0.66" in filled                 # 하드코딩 0.15~0.35 가 아니라 계산값
    assert "0.15~0.35" not in filled


def test_hard_caps_truncate_and_report(tmp_path):
    """조용한 절단 금지 — 넘치면 자르고 반드시 기록한다."""
    many = [{"text": f"t{i}", "source_time_sec": 105.0, "duration_sec": 0.5,
             "x": 0.5, "y": 0.3} for i in range(sc.MAX_TEXTS + 3)]
    out, notes = _valid(_plan(texts=many), tmp_path)
    assert len(out["texts"]) == sc.MAX_TEXTS
    assert any("상한" in n for n in notes)


# ── 스티커 ────────────────────────────────────────────────────────────────
def _sticker_root(tmp_path):
    d = tmp_path / "app_root" / "assets" / "stickers"
    d.mkdir(parents=True)
    (d / "arrow.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return tmp_path / "app_root", {"arrow_red": {"file": "arrow.png", "desc": "빨간 화살표"}}


def test_sticker_id_only_and_staged_into_run_dir(tmp_path):
    """AI 는 id 만 준다 — 엔진이 파일 경로를 AI 에게 열지 않는 규율."""
    app_root, manifest = _sticker_root(tmp_path)
    run_dir = tmp_path / "run"; run_dir.mkdir()
    plan = _plan(images=[{"sticker": "arrow_red", "source_time_sec": 105.0,
                          "duration_sec": 1.5, "x": 0.5, "y": 0.3, "w": 0.2}])
    out, notes = sc.validate_plan(plan, manifest=manifest, app_root=app_root, run_dir=run_dir)
    # run_dir 상대 경로로 바뀌어 편집실 이미지와 **같은 v3 계약**을 탄다
    assert out["images"][0]["file"] == "style_assets/arrow.png"
    assert (run_dir / "style_assets" / "arrow.png").is_file()
    assert notes == []


def test_file_path_from_ai_is_rejected(tmp_path):
    app_root, manifest = _sticker_root(tmp_path)
    with pytest.raises(sc.StylePlanError):
        sc.validate_plan(
            _plan(images=[{"file": "../../etc/passwd", "source_time_sec": 105.0,
                           "duration_sec": 1.0, "x": 0.1, "y": 0.1, "w": 0.2}]),
            manifest=manifest, app_root=app_root, run_dir=tmp_path)


def test_unknown_sticker_drops_that_item_only(tmp_path):
    """스티커 하나 때문에 편이 죽으면 안 된다(E13 keyterms 필터와 같은 결)."""
    app_root, manifest = _sticker_root(tmp_path)
    run_dir = tmp_path / "run"; run_dir.mkdir()
    plan = _plan(images=[
        {"sticker": "없는거", "source_time_sec": 105.0, "duration_sec": 1.0,
         "x": 0.1, "y": 0.1, "w": 0.2},
        {"sticker": "arrow_red", "source_time_sec": 106.0, "duration_sec": 1.0,
         "x": 0.1, "y": 0.1, "w": 0.2},
    ])
    out, notes = sc.validate_plan(plan, manifest=manifest, app_root=app_root, run_dir=run_dir)
    assert len(out["images"]) == 1
    assert any("없는거" in n for n in notes)


def test_empty_manifest_is_normal(tmp_path):
    """라이선스 확인 전에는 목록이 비어 있는 것이 정상 상태다."""
    assert sc.load_sticker_manifest(tmp_path) == {}
    assert sc.sticker_catalog_for_prompt({}) == ""


def test_bundled_manifest_is_valid_json_and_empty():
    """번들 manifest 는 파싱돼야 하고, 지금은 비어 있어야 한다(§13-1 라이선스 미확정)."""
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent / "app"
    raw = json.loads((root / "assets" / "stickers" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["stickers"] == []
    assert sc.load_sticker_manifest(root) == {}


# ══════════════════════════════════════════════════════════════════════════
# 우선순위 — 편집실 > 채널 명시 > AI
# ══════════════════════════════════════════════════════════════════════════
def test_channel_explicit_design_key_beats_ai():
    base = DesignConfig()
    kw, notes = sc.design_overrides({"tts_rotate": -5.0}, {"tts_rotate"}, base)
    assert kw == {} and notes and "채널" in notes[0]
    kw2, _ = sc.design_overrides({"tts_rotate": -5.0}, set(), base)
    assert kw2 == {"tts_rotate": -5.0}


def test_explicit_fields_are_collected_from_cli_not_guessed():
    """'기본값과 다른가'로 판정하면 채널이 기본값과 같은 값을 명시한 경우를 놓친다."""
    got: set[str] = set()
    _build_design_config(_design("--design-title-rotate", "0"), collect=got)
    assert "title_rotate" in got          # 기본값(0.0)과 같아도 '명시'다
    got2: set[str] = set()
    _build_design_config(_design(), collect=got2)
    assert "title_rotate" not in got2


def test_ai_title_box_assembles_like_cli():
    """렌더러는 리스트만 읽는다 — cli 와 같은 조립이 아니면 화면에 안 나온다(F-409 교훈)."""
    base = DesignConfig()
    kw, _ = sc.design_overrides({"title_box": "round"}, set(), base)
    assert kw["title_boxes"] == ["round", base.title_boxes[1]]


def test_editor_subtitle_style_wins_over_ai():
    """사람이 이미 정한 줄은 AI 가 못 덮는다."""
    segs = [SimpleNamespace(start_sec=0.0, end_sec=2.0, text="가",
                            style={"size": 50.0}),
            SimpleNamespace(start_sec=2.0, end_sec=4.0, text="나")]
    n, _ = sc.apply_subtitle_styles(
        segs, [{"source_time_sec": 100.5, "style": {"size": 99.0}},
               {"source_time_sec": 103.0, "style": {"color": "#FF0000"}}], _clips())
    assert segs[0].style == {"size": 50.0}          # 사람 것 그대로
    assert segs[1].style == {"color": "#FF0000"}    # 빈 줄에만 얹힌다
    assert n == 1


# ══════════════════════════════════════════════════════════════════════════
# 앵커 변환 — v3 배치 규칙 재사용
# ══════════════════════════════════════════════════════════════════════════
def test_subtitle_emphasis_anchor_converts_to_edit_time():
    """원본 205s = 두 번째 클립 5초 지점 → 편집본 25s."""
    segs = [SimpleNamespace(start_sec=24.0, end_sec=26.0, text="표적")]
    n, dropped = sc.apply_subtitle_styles(
        segs, [{"source_time_sec": 205.0, "style": {"size": 80.0}}], _clips())
    assert n == 1 and segs[0].style == {"size": 80.0} and not dropped


def test_orphan_anchor_is_dropped_not_guessed():
    segs = [SimpleNamespace(start_sec=0.0, end_sec=2.0, text="가")]
    n, dropped = sc.apply_subtitle_styles(
        segs, [{"source_time_sec": 500.0, "style": {"size": 80.0}}], _clips())
    assert n == 0 and dropped


def test_title_segments_anchor_conversion_and_overlap():
    """앵커 쌍 → 편집본 창. 겹치는 창은 뒤엣것만 버린다(제목이 통째로 사라지면 안 된다)."""
    segs, dropped = sc.title_segments_from_anchors(
        [{"text": "첫 제목", "from_anchor": 100.0, "to_anchor": 112.0},
         {"text": "겹침", "from_anchor": 105.0, "to_anchor": 118.0}], _clips())
    assert segs == [{"text": "첫 제목", "start_sec": 0.0, "end_sec": 12.0}]
    assert dropped and "겹침" in str(dropped)


def test_title_segments_orphan_dropped():
    segs, dropped = sc.title_segments_from_anchors(
        [{"text": "밖", "from_anchor": 500.0, "to_anchor": 505.0}], _clips())
    assert segs == [] and dropped


# ══════════════════════════════════════════════════════════════════════════
# 모델 정책 (2026-08-23) — Pro 는 영상 분석 하나뿐
# ══════════════════════════════════════════════════════════════════════════
def test_only_analyze_chunk_uses_pro():
    """`model_name`(Pro 슬롯)을 쓰는 호출은 analyze_chunk 하나여야 한다."""
    import inspect
    from app.modules import gemini_client
    src = inspect.getsource(gemini_client)
    assert src.count("model=self.config.model_name") == 1
    analyze = inspect.getsource(gemini_client.GeminiClient.analyze_chunk)
    assert "model=self.config.model_name" in analyze


def test_relationships_and_research_moved_to_flash():
    import inspect
    from app.modules import gemini_client, work_researcher
    rel = inspect.getsource(gemini_client.GeminiClient.extract_relationships)
    assert "model=self.config.flash_model_name" in rel
    # 리서치는 세 갈래(그라운딩 켬/끔/폴백) 전부 같은 모델이어야 한다
    res = inspect.getsource(work_researcher._search_with_grounding)
    assert res.count("model=model_name") == 3
    assert "flash_model_name" in res
    assert "config.model_name" not in res


def test_gemini_config_default_is_not_a_banned_model():
    """CLAUDE.md 모델 규칙: 허용 모델 외 금지 — 2026-08-31 전 호출 Flash 3.7
    (v3 A/B 실측 근거 · 종전엔 pro-preview/3.6-flash 이원). 슬롯 구분은 유지."""
    from app.modules.gemini_client import GeminiConfig
    cfg = GeminiConfig(api_key="x")
    assert cfg.model_name == "gemini-3.7-flash"
    assert cfg.flash_model_name == "gemini-3.7-flash"


def test_provenance_records_role_to_slot_map():
    """두 슬롯 이름만으로는 전환 전후 산출물을 구분할 수 없다 — 역할 표를 남긴다."""
    from app.config import AppConfig
    from app.modules.provenance import build_provenance
    roles = build_provenance(AppConfig())["models"]["roles"]
    assert roles["analyze_chunk"] == "pro"
    assert {roles[k] for k in roles if k != "analyze_chunk"} == {"flash"}


def test_style_prompt_formats_without_keyerror():
    """`.format()` 프롬프트는 JSON 중괄호를 `{{}}` 로 이중화해야 한다 — 하나만 빠져도
    **런타임에만** KeyError 로 죽는다(이 레포 프롬프트 상수의 상습 파손 지점)."""
    from app.modules.gemini_client import GeminiClient, GeminiConfig

    captured = {}

    class _FakeModels:
        def generate_content(self, **kw):
            captured["prompt"] = kw["contents"][0]
            return SimpleNamespace(text='{"schema": "style_plan/v1", "texts": []}')

    client = GeminiClient.__new__(GeminiClient)          # __init__ 은 SDK 를 붙잡는다
    client.config = GeminiConfig(api_key="x")
    client.client = SimpleNamespace(models=_FakeModels())
    client.types = SimpleNamespace(
        GenerateContentConfig=lambda **kw: kw, ThinkingConfig=lambda **kw: kw)

    out = client.compose_style(
        work_title="작품", title_text="제목\n둘째 줄",
        timeline=[{"role": "hook", "source_start": 100.0, "source_end": 120.0,
                   "edit_start": 0.0}],
        transcript_lines=[{"source_sec": 105.0, "text": "대사"}],
        tts_cues=[{"source_time_sec": 103.0, "voice": "ko_female", "speed": "normal",
                   "text": "내레이션"}],
        sticker_catalog="", reject_note="자막이 너무 컸다")
    assert out == {"schema": "style_plan/v1", "texts": []}
    p = captured["prompt"]
    assert '"schema": "style_plan/v1"' in p          # 출력 예시가 살아 있다
    assert "ko_male_low" in p and "very_fast" in p   # 라벨이 실제로 채워졌다
    assert "재작업 지시" in p and "이번 연출 구성에서" in p
    assert "(번들된 스티커 없음" in p                 # 빈 목록 안내


def test_style_prompt_is_tracked_by_provenance():
    """`_PROMPT` 로 끝나는 모듈 상수여야 prompt_set_hash 에 자동으로 실린다."""
    from app.modules.gemini_client import STYLE_COMPOSITION_PROMPT
    from app.modules.provenance import _prompt_versions
    assert "style_composition_prompt" in _prompt_versions()
    assert "style_plan/v1" in STYLE_COMPOSITION_PROMPT


# ══════════════════════════════════════════════════════════════════════════
# E16 짝 변경 (2026-08-24) — JP 재렌더가 화면 글자를 일본어로 바꿀 수 있어야 한다
# ══════════════════════════════════════════════════════════════════════════
def test_localization_font_is_allowed_for_texts():
    """현지화가 texts 문구를 일본어로 바꾸면 폰트도 같이 바꾼다 — 화이트리스트에 없으면
    그 JP 재렌더가 통째로 거절된다(vlp apply_editor_text_translation 의 짝).

    번들 4종은 전부 한글 전용이라(mulmaru 만 가나, 한자는 넷 다 없음) 일본어가 두부(□)다.
    """
    from app.modules.edit_overrides import TEXT_FONTS, validate_overrides
    assert "ArialUnicode" in TEXT_FONTS
    doc = {"schema": "edit_overrides/v3",
           "texts": [{"text": "ドンッ！", "source_time_sec": 100.0, "duration_sec": 1.0,
                      "x": 0.5, "y": 0.3, "font": "ArialUnicode"}]}
    validate_overrides(doc)                      # 거절되면 여기서 EditOverrideError
    # 여전히 모르는 폰트는 거절한다(조용한 시스템 폰트 대체 차단 — 원래 규율)
    bad = {"schema": "edit_overrides/v3",
           "texts": [{"text": "x", "source_time_sec": 100.0, "duration_sec": 1.0,
                      "x": 0.5, "y": 0.3, "font": "Helvetica"}]}
    with pytest.raises(Exception):
        validate_overrides(bad)


def test_ai_plan_still_restricted_to_bundled_fonts():
    """AI 는 현지화 폰트를 고를 이유가 없다 — KR 연출은 번들 폰트다.

    (검증은 v3 화이트리스트를 공유하므로 ArialUnicode 도 통과한다. 이 테스트는 AI 가
    실제로 그 값을 쓰지 않는다는 것이 아니라, 프롬프트가 번들 4종만 제시함을 고정한다.)
    """
    from app.modules.gemini_client import STYLE_COMPOSITION_PROMPT
    assert "ArialUnicode" not in STYLE_COMPOSITION_PROMPT


# ══════════════════════════════════════════════════════════════════════════
# 제목 굵게 금지 · 두 줄 형식 고정 (E21, 2026-08-25 사용자 지시)
# ══════════════════════════════════════════════════════════════════════════
def test_ai_cannot_bold_the_title(tmp_path):
    """"제목은 굵게 하기 금지 — 굵은 폰트에 볼드를 얹으면 글자가 뭉개진다."

    회전과 **같은 방식**으로 닫는다(드롭+메모) — 플랜 전체를 거절하면 효과 텍스트·제목
    창까지 같이 날아간다. 사람·채널 값은 그대로다.
    """
    for key in ("title_bold", "title_bold2"):
        assert key not in sc.STYLE_DESIGN_ALLOWED
        assert key in sc.STYLE_DESIGN_IGNORED
        out, notes = _valid(_plan(design={key: True, "title_box": "round"}), tmp_path)
        assert key not in out.get("design", {})          # 버려진다
        assert out["design"]["title_box"] == "round"     # 나머지는 산다
        assert any(key in n and "굵게" in n for n in notes)   # 조용한 드롭 금지


def test_old_checkpoint_cannot_bring_bold_back(tmp_path):
    """옛 체크포인트는 재검증 없이 재적용된다(E15 재개 계약) — 조립에서 한 번 더 막는다."""
    kwargs, notes = sc.design_overrides({"title_bold": True, "title_box": "round"},
                                        set(), DesignConfig())
    assert "title_bolds" not in kwargs
    assert kwargs["title_boxes"][0] == "round"
    assert any("title_bold" in n for n in notes)


def test_channel_can_still_bold_the_title():
    """막은 것은 AI 뿐이다 — 사람이 보고 정한 값은 사람 것이다(E17-1 규율 유지)."""
    assert _build_design_config(_design("--design-title-bold")).title_bolds[0] is True


def test_title_window_text_is_the_second_line_only(tmp_path):
    """창은 아랫줄만 바꾼다 — 두 줄을 통째로 보내던 종전 산출은 여기서 걸린다."""
    ok = _plan(title_segments=[{"text": "아랫줄", "from_anchor": 105.0, "to_anchor": 110.0}])
    assert _valid(ok, tmp_path)[0]["title_segments"][0]["text"] == "아랫줄"

    with pytest.raises(sc.StylePlanError) as e:
        _valid(_plan(title_segments=[{"text": "윗줄\n아랫줄",
                                      "from_anchor": 105.0, "to_anchor": 110.0}]), tmp_path)
    assert "한 줄" in str(e.value)

    with pytest.raises(sc.StylePlanError):        # 20자 초과 = 줄이 접혀 3줄이 된다
        _valid(_plan(title_segments=[{"text": "가" * (sc.MAX_TITLE_LINE_CHARS + 1),
                                      "from_anchor": 105.0, "to_anchor": 110.0}]), tmp_path)


def test_title_fixed_is_one_short_line_and_needs_windows(tmp_path):
    p = _plan(title_fixed="고정 윗줄",
              title_segments=[{"text": "아랫줄", "from_anchor": 105.0, "to_anchor": 110.0}])
    assert _valid(p, tmp_path)[0]["title_fixed"] == "고정 윗줄"

    out, notes = _valid(_plan(title_fixed="고정 윗줄"), tmp_path)     # 창이 없으면 무의미
    assert "title_fixed" not in out and any("창이 없어" in n for n in notes)

    with pytest.raises(sc.StylePlanError):
        _valid(_plan(title_fixed="가" * (sc.MAX_TITLE_LINE_CHARS + 1),
                     title_segments=[{"text": "아", "from_anchor": 105.0,
                                      "to_anchor": 110.0}]), tmp_path)


def test_the_fixed_line_is_repeated_in_every_window():
    """모든 구간의 **첫 줄이 같아야** 한다 — 그게 '두 줄 형식 유지'의 실체다."""
    segs, _ = sc.title_segments_from_anchors(
        [{"text": "아랫줄A", "from_anchor": 105.0, "to_anchor": 110.0},
         {"text": "아랫줄B", "from_anchor": 205.0, "to_anchor": 208.0}],
        _clips(), base_title="기본 윗줄\n기본 아랫줄", fixed_line="고정 윗줄")
    assert {sg["text"].split("\n")[0] for sg in segs} == {"고정 윗줄"}
    assert [sg["text"].split("\n")[1] for sg in segs] == ["아랫줄A", "아랫줄B"]
    # 빈 시간 없음 + 다음 문구가 미리 새지 않음(앞 구간은 계속 아랫줄A)
    assert segs[0]["start_sec"] == 0.0 and segs[0]["end_sec"] == pytest.approx(25.0)
    assert segs[-1]["end_sec"] == pytest.approx(30.0)


def test_the_second_line_never_leaks_the_next_scene():
    """사용자 지적("앞에는 바비큐 안 좋아하는 내용이라") — 뒤 문구를 앞당겨 오지 않는다."""
    segs, _ = sc.title_segments_from_anchors(
        [{"text": "안 좋아한다더니", "from_anchor": 100.0, "to_anchor": 105.0},
         {"text": "고기 앞에서 무너짐", "from_anchor": 205.0, "to_anchor": 210.0}],
        _clips(), base_title="기본\n제목", fixed_line="캠프 첫날 바비큐")
    at_2s = [sg for sg in segs if sg["start_sec"] <= 2.0 < sg["end_sec"]][0]
    assert at_2s["text"] == "캠프 첫날 바비큐\n안 좋아한다더니"
    assert "무너짐" not in at_2s["text"]


def test_pipeline_passes_the_fixed_line():
    """파이프라인이 fixed_line 을 안 넘기면 윗줄 고정이 화면에 반영되지 않는다."""
    import inspect
    from app import pipeline
    src = inspect.getsource(pipeline.run_pipeline)
    block = src.split("[style] AI 연출 구성 (E15", 1)[1].split("[13/15] 리소스 생성", 1)[0]
    assert "fixed_line=_fixed_line" in block
    assert "_stylemod.split_title_lines(title_text)" in block
    assert 'get("title_fixed")' in block


def test_prompt_states_the_two_line_title_contract():
    """검증기만 바꾸면 모델이 매 편 거절당하는 값을 계속 낸다(E17-1 에서 배운 것)."""
    from app.modules.gemini_client import STYLE_COMPOSITION_PROMPT as P
    rendered = _render_prompt(P)
    assert "title_fixed" in rendered
    assert "제목을 굵게 하지 않는다" in rendered
    assert "직전 문구" in rendered                  # 빈틈 규칙을 모델도 알아야 한다
    assert str(sc.MAX_TITLE_LINE_CHARS) in rendered
