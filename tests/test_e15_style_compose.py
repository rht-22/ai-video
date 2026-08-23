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
    assert _valid(_plan(design={"title_rotate": -3}), tmp_path)[0]["design"]["title_rotate"] == -3.0
    with pytest.raises(sc.StylePlanError):
        _valid(_plan(design={"title_rotate": 200}), tmp_path)


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
    kw, notes = sc.design_overrides({"title_rotate": -5.0}, {"title_rotate"}, base)
    assert kw == {} and notes and "채널" in notes[0]
    kw2, _ = sc.design_overrides({"title_rotate": -5.0}, set(), base)
    assert kw2 == {"title_rotate": -5.0}


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
    """CLAUDE.md 모델 규칙: 두 모델 외 금지. 종전 기본값은 'gemini-3.5-flash' 였다."""
    from app.modules.gemini_client import GeminiConfig
    cfg = GeminiConfig(api_key="x")
    assert cfg.model_name == "gemini-3.1-pro-preview"
    assert cfg.flash_model_name == "gemini-3.6-flash"


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
