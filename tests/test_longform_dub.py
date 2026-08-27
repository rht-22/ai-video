"""L4d 롱폼 대사 더빙 + --no-narration (2026-08-27 운영자 결정).

계약 셋:
  ① 게이트 — work_cfg["dub"] 없는 작품(SHOTCONE 등)은 단계 자체가 없다(회귀 0).
  ② 문구·시각의 정본은 L3 의 일본어 subtitle_segments.json — ASR·재번역 없음
     (구운 자막과 더빙이 말하는 문장이 같은 소스여야 어긋나지 않는다).
  ③ 내레이션은 생성에서 아예 안 만든다(--no-narration) — cue 합성·자막·믹스 전부.
"""
import inspect
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.localize.dub_rerender import dub_config, dub_events, l4d_dub  # noqa: E402


# ── ① 게이트 ───────────────────────────────────────────────────────────
def test_no_dub_key_means_no_stage(tmp_path):
    assert l4d_dub(tmp_path, {}, {}, tmp_path / "localize_ja") is None
    assert l4d_dub(tmp_path, {"display": "ヘミリ"}, {}, tmp_path / "out") is None


def test_loopy_work_has_the_gate_and_shotcone_does_not():
    d = json.loads(pathlib.Path("app/localize/data/locales.json").read_text(encoding="utf-8"))
    assert d["works"]["잔망루피 유튜브 숏폼"]["ja"]["dub"]["enabled"] is True
    assert "dub" not in d["works"]["혜미리예채파"]["ja"]          # 회귀 0


def test_runner_calls_l4d_after_l4_and_only_with_render():
    from app.localize import runner
    src = inspect.getsource(runner.run_localize)
    body = src.split("if not skip_render:", 1)[1].split("l5_metadata", 1)[0]
    # 렌더 없는 실행(skip_render)은 더빙도 없다 — 더빙은 L4 산출 위에서만 돈다
    assert "l4d_dub" in body
    assert src.index("l4_render") < src.index("l4d_dub")


# ── ② 이벤트·설정 ──────────────────────────────────────────────────────
def test_dub_events_come_from_ja_segments_verbatim():
    segs = [
        {"start_sec": 1.0, "end_sec": 2.5, "text": "できたルプ！"},
        {"start_sec": 3.0, "end_sec": 4.0, "text": "  "},        # 빈 줄 — 합성 없음
        {"start_sec": 5.0, "end_sec": 6.0, "text": "むりルプ", "style": {"size": 40}},
    ]
    assert dub_events(segs) == [
        {"start": 1.0, "end": 2.5, "text": "できたルプ！"},
        {"start": 5.0, "end": 6.0, "text": "むりルプ"},
    ]
    assert dub_events([]) == []


def test_dub_config_narrows_without_touching_identity(tmp_path):
    base = {"paths": {"outputs_dir": "outputs"},
            "dub": {"voice_id": "XCUaBo3FxL00wvRju0PX", "max_speedup": 1.35,
                    "burn_dub_subtitle": True,
                    "backcheck": {"enabled": True, "max_cer": 0.3}},
            "render": {"line_max_chars": 16}}
    cfg = dub_config(base, {"enabled": True}, tmp_path)
    # 자막은 L4 가 이미 구웠다 — 이중 자막 금지
    assert cfg["dub"]["burn_dub_subtitle"] is False
    # 산출은 job 안으로 — 엔진 레포 outputs/ 를 더럽히지 않는다
    assert cfg["paths"]["outputs_dir"] == str(tmp_path)
    # 루피 목소리의 정체(페이싱·백체크·기본 voice)는 그대로
    assert cfg["dub"]["voice_id"] == "XCUaBo3FxL00wvRju0PX"
    assert cfg["dub"]["max_speedup"] == 1.35
    assert cfg["dub"]["backcheck"] == {"enabled": True, "max_cer": 0.3}
    assert base["dub"]["burn_dub_subtitle"] is True                # 원본 불변(사본)


def test_dub_config_work_voice_wins(tmp_path):
    base = {"paths": {}, "dub": {"voice_id": "config-default-0000"}}
    cfg = dub_config(base, {"voice_id": "WorkSpecificVoice123"}, tmp_path)
    assert cfg["dub"]["voice_id"] == "WorkSpecificVoice123"


def test_no_dialogue_keeps_original_audio(tmp_path):
    """대사 0줄(노래뿐인 편)은 더빙하지 않는다 — 空飛ぶルーピー 실사고: 대사 없는 편의
    보컬 제거는 노래 가사를 지운다. 크게 실패하지도 않는다(원음 유지가 맞는 동작)."""
    job = tmp_path / "job"; job.mkdir()
    (job / "shorts.mp4").write_bytes(b"x")
    (job / "subtitle_segments.json").write_text("[]", encoding="utf-8")
    out = l4d_dub(job, {"dub": {"enabled": True}}, {}, tmp_path / "lja")
    assert out == {"segments": 0, "skipped": "no_dialogue"}


def test_missing_render_output_fails_loud(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        l4d_dub(tmp_path, {"dub": {"enabled": True}}, {}, tmp_path / "lja")


# ── ③ --no-narration ──────────────────────────────────────────────────
def test_cli_has_no_narration_flag():
    from app.cli import build_parser
    p = build_parser()
    base = ["create_shorts", "--video", "x.mp4", "--title", "t"]
    args = p.parse_args(base + ["--no-narration"])
    assert args.no_narration is True
    args2 = p.parse_args(base)
    assert getattr(args2, "no_narration", False) is False          # 미지정 = 종전(회귀 0)


def test_pipeline_drops_all_cues_when_narration_off():
    """개입 지점은 앵커 해석 직전 한 곳 — 모든 경로(신규·체크포인트 재개·편집실
    오버라이드)가 그 지점을 지난다. cue 0 은 이미 정상 상태라 하류가 자연히 빈다."""
    from app import pipeline
    src = inspect.getsource(pipeline)
    i_gate = src.index("if not payload.include_narration:")
    i_anchor = src.index("[tts cues] 앵커 해석 —")
    assert i_gate < i_anchor
    # 편집실 내레이션 오버라이드 병합보다도 뒤 — 채널 결정이 이긴다(버린 건수는 로그)
    assert src.index("내레이션 오버라이드 적용") < i_gate


def test_pipeline_input_default_keeps_narration():
    from app.pipeline import PipelineInput
    import dataclasses
    fld = {f.name: f.default for f in dataclasses.fields(PipelineInput)}
    assert fld["include_narration"] is True                        # 기본 = 종전 그대로
