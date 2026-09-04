"""V4-M6 11:resources 회귀 가드 — `app/v4/resources.py`.

계약 정본 `docs/v4/M6-interfaces.md` §1. 이 파일이 못박는 것:

  · **일곱 걸음의 호출 순서**(뒤바뀌면 자막이 달라진다) — 스파이로 값으로 고정한다.
  · 인명 사전 자리 `research["cast_images"][].character_name`.
  · TTS 실패 시 **같은 이름의 옛 mp3 unlink** + `path=None`(낡은 대본 차단).
  · 창을 못 찾은 cue 를 조용히 버리지 않는다(lost 기록).
  · 시각 정합 벨트 위반 = `AssertionError`.
  · 편별 산출 경로(1위 = v1 이름 · 2위↓ = `_{n}`).
  · 자막이 **뮤트 창 밖**만 담는다.
  · 입력 불변 + 같은 입력이면 같은 산출.

LLM·TTS·ffmpeg 실호출은 없다(가짜로 덮는다).
"""
from __future__ import annotations

import copy
import subprocess

import pytest

from app.v3 import assemble, textcheck
from app.v4 import resources as res


# ── 픽스처 ─────────────────────────────────────────────────────────────────
# 소재: 유성 span 3개(각 4초)가 붙어 있고, 가운데 s1 만 내레이션 밑에서 뮤트된다.
#   s0 [0,4)  원음      s1 [4,8)  뮤트      s2 [8,12) 원음
# 내레이션 창은 (4.5, 7.0) — s1 안이지만 **s1 전체가 아니다**. 그래서 4.0~4.5 와
# 7.0~8.0 은 뮤트 클립이면서도 원음이 살아 있고, 그 구간 대사는 자막이 있어야 한다
# (`assemble.word_subtitles` 의 M15 계약). 이 픽스처의 존재 이유가 그 경계다.

def _grid() -> dict:
    return {
        "span_candidates": [
            {"id": "s0", "t_in": 0.0, "t_out": 4.0, "is_audio": True},
            {"id": "s1", "t_in": 4.0, "t_out": 8.0, "is_audio": True},
            {"id": "s2", "t_in": 8.0, "t_out": 12.0, "is_audio": True},
        ],
        "words": [
            {"t0": 0.2, "t1": 0.9, "text": "진짜"},
            {"t0": 1.0, "t1": 1.8, "text": "대박이다"},
            # s1 의 두 어절: 앞은 뮤트 창 **밖**(4.1~4.4), 뒤는 창 **안**(5.0~5.6).
            # "잠깐." 의 문장부호가 라인을 끊어 둘이 다른 자막 줄이 된다.
            {"t0": 4.1, "t1": 4.4, "text": "잠깐."},
            {"t0": 5.0, "t1": 5.6, "text": "뭐라고"},
            {"t0": 8.2, "t1": 8.9, "text": "끝났다."},
        ],
    }


def _span_index() -> dict:
    def one(t_in, t_out, speaker):
        return {"t_in": t_in, "t_out": t_out, "is_audio": True, "importance": 3,
                "text_source": "transcript", "heard_text": "",
                "audio_script": [{"speaker": speaker, "text": ""}]}
    return {"s0": one(0.0, 4.0, "강비호"),
            "s1": one(4.0, 8.0, "홍재인"),
            "s2": one(8.0, 12.0, "강비호")}


def _story_doc(*, with_lost_cue: bool = False) -> dict:
    cues = [{"text": "여기서 판이 뒤집힌다", "source_time_sec": 4.5,
             "source_end_sec": 7.0, "beat": 0, "mode": "gap",
             "muted_span_ids": ["s1"]}]
    if with_lost_cue:
        # 타임라인 밖(30s) — `finalize_cues` 가 start_sec None 으로 돌려준다.
        cues.append({"text": "잘려 나간 구간의 내레이션", "source_time_sec": 30.0,
                     "source_end_sec": 32.0, "beat": 0, "mode": "gap",
                     "muted_span_ids": []})
    return {
        "title": {"line1": "판이 뒤집힌 순간", "line2": "그래서 어떻게 됐냐면"},
        "beats": [{"number": 0, "role": "hook",
                   "span_ids": ["s0", "s1", "s2"], "muted_span_ids": ["s1"],
                   "labels": [], "narration": ["여기서 판이 뒤집힌다"]}],
        "narration_cues": cues,
    }


def _research() -> dict:
    return {"cast_images": [{"character_name": "강비호"},
                            {"character_name": "홍재인"},
                            {"character_name": None}]}


class _FakeGemini:
    """`shorten_text` 만 있는 가짜 — 실호출 없음."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def shorten_text(self, text: str, target_chars: int) -> str:
        self.calls.append((text, target_chars))
        return text[:target_chars]


def _plan(story_doc: dict, span_index: dict, grid: dict) -> dict:
    plan, _belt = res.build_edit_plan(story_doc, span_index, grid,
                                      video_path="/x/src.mp4", work_title="작품")
    return plan


def _fake_tts(monkeypatch, *, actual_of=None, fail_on=(), record=None):
    """가짜 합성 — 파일을 만들고 (텍스트, 실측초)를 돌려준다.

    `actual_of(index, target)` 로 실측을 정하고, `fail_on` 색인은 예외를 던진다."""
    import app.modules.tts as tts_mod

    state = {"i": 0}

    def fake(text, output_path, target_sec, *, voice, speed, shorten_fn=None,
             **kw):
        i = state["i"]
        state["i"] += 1
        if record is not None:
            record.append("tts")
        if i in fail_on:
            raise RuntimeError("가짜 합성 실패")
        output_path.write_bytes(b"mp3")
        actual = actual_of(i, target_sec) if actual_of else target_sec
        return text, actual

    monkeypatch.setattr(tts_mod, "synthesize_tts_with_fit", fake)
    monkeypatch.setattr(tts_mod, "active_backend", lambda: "edge-tts")
    monkeypatch.setattr(tts_mod, "elevenlabs_disabled", lambda: None)


def _fake_ffmpeg(monkeypatch, record=None):
    """가짜 ffmpeg — 인자를 모으고 산출 파일만 만든다."""
    import app.modules.ffmpeg_utils as ff

    seen: list[list[str]] = []

    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        if record is not None:
            record.append("trim")
        # `-y -i <입력> ... <출력>` — 마지막이 산출 경로다(replace 가 그것을 옮긴다)
        from pathlib import Path
        Path(cmd[-1]).write_bytes(b"trimmed")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ff, "find_ffmpeg_command", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(res.subprocess, "run", fake_run)
    return seen


_DEFAULT = object()          # `research=None`("리서치 없음")과 미지정을 구분하는 표식


def _build(tmp_path, monkeypatch, *, variant=1, fix_names=False,
           story_doc=None, research=_DEFAULT, actual_of=None, fail_on=(),
           record=None, log=None):
    grid, span_index = _grid(), _span_index()
    doc = story_doc if story_doc is not None else _story_doc()
    plan = _plan(doc, span_index, grid)
    _fake_tts(monkeypatch, actual_of=actual_of, fail_on=fail_on, record=record)
    return res.build_resources(
        doc, span_index=span_index, grid=grid, plan=plan,
        research=_research() if research is _DEFAULT else research,
        gemini=_FakeGemini(), output_dir=tmp_path, variant=variant,
        fix_names=fix_names, log=(log if log is not None else (lambda *a, **k: None)))


# ── 편별 산출 경로 ─────────────────────────────────────────────────────────

def test_variant1_uses_v1_file_names(tmp_path):
    """1위는 v1 이름 그대로여야 한다 — 현지화·편집실이 이 이름을 읽는다."""
    p = res.resource_paths(tmp_path, 1)
    assert p["subtitle_segments"].name == "subtitle_segments.json"
    assert p["checkpoint_resources"].name == "checkpoint_resources.json"
    assert p["edit_plan"].name == "edit_plan.json"


def test_variant2_uses_underscore_suffix_not_variant_word(tmp_path):
    """2위↓ 는 `_{n}` 이다. v3 훅 변형의 `*_variant_{k}.json` 과 **다른 이름**이다."""
    p = res.resource_paths(tmp_path, 2)
    assert p["subtitle_segments"].name == "subtitle_segments_2.json"
    assert p["checkpoint_resources"].name == "checkpoint_resources_2.json"
    assert p["edit_plan"].name == "edit_plan_2.json"
    assert "variant" not in p["subtitle_segments"].name


def test_tts_cue_path_matches_v1_naming(tmp_path):
    """cue mp3 도 v1 이름 — 편마다 갈려야 실패한 편이 남의 오디오를 지우지 않는다."""
    assert res.tts_cue_path(tmp_path, 1, 0).name == "tts_cue_0.mp3"
    assert res.tts_cue_path(tmp_path, 3, 2).name == "tts_3_cue_2.mp3"


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1"])
def test_variant_must_be_positive_int(tmp_path, bad):
    """0·음수는 조용히 1위 파일을 덮어쓸 수 있다 — 즉시 실패시킨다."""
    with pytest.raises(ValueError):
        res.resource_paths(tmp_path, bad)


# ── 벨트 ───────────────────────────────────────────────────────────────────

def test_belt_passes_and_reports_100pct():
    plan, belt = res.build_edit_plan(_story_doc(), _span_index(), _grid(),
                                     video_path="/x.mp4", work_title="작품")
    assert belt["pct"] == 100.0
    assert [c["clip_start_sec"] for c in plan["timeline"]] == [0.0, 4.0, 8.0]
    # 뮤트 span 은 제 클립을 갖는다(use_original_audio 가 클립 단위 계약이라)
    assert [c["use_original_audio"] for c in plan["timeline"]] == [True, False, True]


def test_belt_violation_raises_assertion_error():
    """격자와 어긋난 계획은 코드 결함이다 — 조용히 통과시키지 않는다."""
    other_grid = {"span_candidates": [{"id": "z", "t_in": 1.5, "t_out": 3.5}],
                  "words": []}
    with pytest.raises(AssertionError, match="벨트 위반"):
        res.build_edit_plan(_story_doc(), _span_index(), other_grid,
                            video_path="/x.mp4", work_title="작품")


def test_build_resources_reruns_the_belt_on_a_given_plan(tmp_path, monkeypatch):
    """재료로 받은 plan 이 다른 격자의 낡은 캐시일 수 있다 — 여기서도 본다."""
    span_index, doc = _span_index(), _story_doc()
    plan = _plan(doc, span_index, _grid())
    _fake_tts(monkeypatch)
    stale_grid = {"span_candidates": [{"id": "z", "t_in": 1.5, "t_out": 3.5}],
                  "words": []}
    with pytest.raises(AssertionError, match="벨트 위반"):
        res.build_resources(doc, span_index=span_index, grid=stale_grid, plan=plan,
                            research=None, gemini=None, output_dir=tmp_path,
                            log=lambda *a, **k: None)


# ── 순서 (계약의 핵심) ─────────────────────────────────────────────────────

def test_seven_steps_run_in_the_contracted_order(tmp_path, monkeypatch):
    """① 뮤트창 ② 어절자막 ③ 반복그물 ④ 인명 ⑤ cue ⑥ TTS ⑦ 물리트림.

    순서가 뒤바뀌면 자막이 달라진다(모듈 독스트링) — 값으로 못박는다."""
    calls: list[str] = []

    def spy(name, fn):
        def wrapper(*a, **k):
            calls.append(name)
            return fn(*a, **k)
        return wrapper

    monkeypatch.setattr(res.assemble, "narration_windows",
                        spy("① narration_windows", assemble.narration_windows))
    monkeypatch.setattr(res.assemble, "word_subtitles",
                        spy("② word_subtitles", assemble.word_subtitles))
    monkeypatch.setattr(res.textcheck, "drop_repetition",
                        spy("③ drop_repetition", textcheck.drop_repetition))
    monkeypatch.setattr(res.textcheck, "check_names",
                        spy("④ check_names", textcheck.check_names))
    monkeypatch.setattr(res.assemble, "finalize_cues",
                        spy("⑤ finalize_cues", assemble.finalize_cues))
    _fake_ffmpeg(monkeypatch, record=calls)
    # 실측이 창을 넘게 만들어 ⑦ 을 실제로 발동시킨다
    _build(tmp_path, monkeypatch, actual_of=lambda i, t: t + 1.0, record=calls)

    assert calls == ["① narration_windows", "② word_subtitles", "③ drop_repetition",
                     "④ check_names", "⑤ finalize_cues", "tts", "trim"]


def test_repetition_net_runs_before_name_fix(tmp_path, monkeypatch):
    """③ 이 ④ 보다 먼저다 — 교정된 줄이 반복 판정을 빠져나가면 안 된다."""
    order: list[str] = []
    monkeypatch.setattr(res.textcheck, "drop_repetition",
                        lambda segs: (order.append("drop"), (segs, []))[1])
    monkeypatch.setattr(res.textcheck, "check_names",
                        lambda segs, names: (order.append("names"), [])[1])
    _build(tmp_path, monkeypatch)
    assert order == ["drop", "names"]


# ── 인명 사전 ──────────────────────────────────────────────────────────────

def test_name_dictionary_comes_from_cast_images_character_name(tmp_path, monkeypatch):
    """사전 자리는 `research["cast_images"][].character_name` 이다(v3 와 같은 자리)."""
    seen: list[list[str]] = []
    monkeypatch.setattr(res.textcheck, "check_names",
                        lambda segs, names: (seen.append(list(names)), [])[1])
    _build(tmp_path, monkeypatch)
    assert seen == [["강비호", "홍재인"]]          # None 인 항목은 빠진다


def test_missing_research_yields_empty_dictionary(tmp_path, monkeypatch):
    """리서치가 없어도 검사는 돈다(경고가 0건일 뿐) — 건너뛰면 오인식이 안 보인다."""
    seen: list[list[str]] = []
    monkeypatch.setattr(res.textcheck, "check_names",
                        lambda segs, names: (seen.append(list(names)), [])[1])
    _build(tmp_path, monkeypatch, research=None)
    assert seen == [[]]


def test_fix_names_only_applies_when_asked(tmp_path, monkeypatch):
    """`fix_names=False` 면 교정 함수를 아예 안 부른다(경고만)."""
    called: list[str] = []
    monkeypatch.setattr(res.textcheck, "check_names",
                        lambda segs, names: [{"token": "강비오", "suggest": "강비호"}])
    monkeypatch.setattr(res.textcheck, "fix_names",
                        lambda segs, names: (called.append("fix"), (segs, [{"x": 1}]))[1])

    _, _, audit = _build(tmp_path, monkeypatch, fix_names=False)
    assert called == [] and audit["subtitle_name_fixes"] == []

    _, _, audit2 = _build(tmp_path, monkeypatch, fix_names=True)
    assert called == ["fix"] and audit2["subtitle_name_fixes"] == [{"x": 1}]


# ── 자막 ───────────────────────────────────────────────────────────────────

def test_subtitles_keep_only_lines_outside_the_mute_window(tmp_path, monkeypatch):
    """뮤트 클립이라도 내레이션 창 **밖**은 원음이 산다 → 그 줄은 자막이 있다.

    창 안(5.0~5.6 "뭐라고")은 소리가 없으니 자막도 없다."""
    _, segments, _ = _build(tmp_path, monkeypatch)
    texts = [s["text"] for s in segments]
    assert "잠깐." in texts                    # 4.1~4.4 — 뮤트 클립이지만 창 밖
    assert "뭐라고" not in texts               # 5.0~5.6 — 창 안
    assert "진짜 대박이다" in texts and "끝났다." in texts


def test_subtitle_segments_file_holds_the_returned_segments(tmp_path, monkeypatch):
    from app.modules import job
    _, segments, _ = _build(tmp_path, monkeypatch)
    assert job.read_json(tmp_path / "subtitle_segments.json") == segments


# ── cue · TTS ──────────────────────────────────────────────────────────────

def test_lost_cue_is_separated_and_recorded_not_silently_dropped(tmp_path, monkeypatch):
    """창이 트리밍으로 사라진 cue(`start_sec is None`)는 기록으로 남는다."""
    logs: list[str] = []
    resources, _, audit = _build(tmp_path, monkeypatch,
                                 story_doc=_story_doc(with_lost_cue=True),
                                 log=lambda m, *a, **k: logs.append(str(m)))
    assert audit["cues_lost_to_trim"] == ["잘려 나간 구간의 내레이션"]
    assert any("cue 드랍" in m for m in logs)
    # 합성은 살아남은 cue 만 — 없는 창에 오디오를 만들지 않는다
    assert [c["cue_index"] for c in resources["tts_cue_files"]] == [0]
    assert resources["tts_cue_files"][0]["cue"]["text"] == "여기서 판이 뒤집힌다"


def test_tts_failure_unlinks_the_stale_mp3_and_leaves_path_none(tmp_path, monkeypatch):
    """낡은 대본이 최종 믹스에 들어가는 것을 막는다(v3 적대 리뷰 확정)."""
    stale = res.tts_cue_path(tmp_path, 1, 0)
    stale.write_bytes("지난 실행의 낡은 대본".encode())
    resources, _, audit = _build(tmp_path, monkeypatch, fail_on={0})

    assert not stale.exists()
    row = resources["tts_cue_files"][0]
    assert row["path"] is None and row["cue"]["fit_actual_sec"] is None
    assert audit["tts_failed"][0]["cue_index"] == 0
    # 합성 실패가 계획 산출을 막지 않는다(fail-soft)
    assert (tmp_path / "checkpoint_resources.json").exists()


def test_second_episode_failure_does_not_delete_the_first_episodes_audio(
        tmp_path, monkeypatch):
    """편마다 mp3 이름이 갈리는 이유 — 2위의 실패가 1위 오디오를 지우면 안 된다."""
    first = res.tts_cue_path(tmp_path, 1, 0)
    first.write_bytes("1위 오디오".encode())
    _build(tmp_path, monkeypatch, variant=2, fail_on={0})
    assert first.read_bytes() == "1위 오디오".encode()


def test_over_window_audio_is_physically_trimmed_with_v3_ffmpeg_args(
        tmp_path, monkeypatch):
    """fit 소진 '잘림 감수' 오디오가 다음 대사를 밟지 않도록 창 길이로 자른다."""
    seen = _fake_ffmpeg(monkeypatch)
    resources, _, audit = _build(tmp_path, monkeypatch,
                                 actual_of=lambda i, t: t + 1.0)
    cmd = seen[0]
    path = res.tts_cue_path(tmp_path, 1, 0)
    assert cmd == ["/usr/bin/ffmpeg", "-y", "-i", str(path),
                   "-t", "2.500",
                   "-af", "afade=t=out:st=2.380:d=0.12",
                   str(path.with_suffix(".trim.mp3"))]
    # 트림본이 원본 자리로 들어간다 + 기록에 남는다
    assert path.read_bytes() == b"trimmed"
    assert not path.with_suffix(".trim.mp3").exists()
    assert audit["tts_trimmed"] == [{"cue_index": 0, "actual_sec": 3.5,
                                     "window_sec": 2.5}]
    assert resources["tts_cue_files"][0]["cue"]["fit_actual_sec"] == 2.5


def test_within_tolerance_audio_is_not_reencoded(tmp_path, monkeypatch):
    """관용(0.05s) 안이면 건드리지 않는다 — 멀쩡한 cue 를 매번 다시 인코딩하지 않는다."""
    seen = _fake_ffmpeg(monkeypatch)
    _build(tmp_path, monkeypatch,
           actual_of=lambda i, t: t + res.TRIM_TOLERANCE_SEC)
    assert seen == []


def test_cue_voice_and_speed_follow_v3(tmp_path, monkeypatch):
    """cue 목소리는 v3 와 같아야 한다 — 바꾸면 v3 로 만든 편과 목소리가 갈린다."""
    assert (res.CUE_VOICE, res.CUE_SPEED) == ("ko_female", "normal")
    resources, _, _ = _build(tmp_path, monkeypatch)
    cue = resources["tts_cue_files"][0]["cue"]
    assert (cue["voice"], cue["speed"]) == ("ko_female", "normal")


def test_audit_carries_backend_and_belt_and_clip_stats(tmp_path, monkeypatch):
    resources, segments, audit = _build(tmp_path, monkeypatch)
    assert resources["tts_backend"] == "edge-tts"
    assert audit["tts_backend"] == "edge-tts"
    assert audit["time_alignment"]["pct"] == 100.0
    assert audit["clip_stats"]["clips"] == 3
    assert audit["subtitle_segments"] == len(segments)
    assert audit["schema"] == res.SCHEMA_RESOURCES


def test_elevenlabs_fallback_reason_is_recorded(tmp_path, monkeypatch):
    """E17 폴백은 추적 가능해야 한다 — 조용한 백엔드 교체 금지."""
    import app.modules.tts as tts_mod
    _fake_tts(monkeypatch)
    monkeypatch.setattr(tts_mod, "elevenlabs_disabled", lambda: "401 만료")
    grid, span_index, doc = _grid(), _span_index(), _story_doc()
    _, _, audit = res.build_resources(
        doc, span_index=span_index, grid=grid, plan=_plan(doc, span_index, grid),
        research=None, gemini=None, output_dir=tmp_path, log=lambda *a, **k: None)
    assert audit["tts_fallback_reason"] == "elevenlabs_auth_expired"
    assert audit["tts_fallback_detail"] == "401 만료"


# ── 산출 자리 ──────────────────────────────────────────────────────────────

def test_all_three_files_land_at_resource_paths(tmp_path, monkeypatch):
    from app.modules import job
    resources, segments, _ = _build(tmp_path, monkeypatch, variant=2)
    p = res.resource_paths(tmp_path, 2)
    assert job.read_json(p["checkpoint_resources"]) == resources
    assert job.read_json(p["subtitle_segments"]) == segments
    assert job.read_json(p["edit_plan"])["schema"] == "edit_plan/v3"
    # 2위 편이 1위 이름을 건드리지 않는다
    assert not (tmp_path / "checkpoint_resources.json").exists()
    assert not (tmp_path / "subtitle_segments.json").exists()
    assert not (tmp_path / "edit_plan.json").exists()


# ── 순수 · 결정성 ──────────────────────────────────────────────────────────

def test_inputs_are_not_mutated(tmp_path, monkeypatch):
    """규율 4 — story_doc·span_index·grid·plan 은 읽기 전용이다."""
    grid, span_index, doc = _grid(), _span_index(), _story_doc()
    plan = _plan(doc, span_index, grid)
    before = copy.deepcopy((grid, span_index, doc, plan))
    _fake_tts(monkeypatch)
    res.build_resources(doc, span_index=span_index, grid=grid, plan=plan,
                        research=_research(), gemini=_FakeGemini(),
                        output_dir=tmp_path, log=lambda *a, **k: None)
    assert (grid, span_index, doc, plan) == before


def test_same_input_gives_the_same_output(tmp_path, monkeypatch):
    """결정성 — audit 에서 실행마다 달라지는 값은 `elapsed_sec` 하나여야 한다."""
    a_res, a_seg, a_audit = _build(tmp_path, monkeypatch)
    b_res, b_seg, b_audit = _build(tmp_path, monkeypatch)
    assert (a_res, a_seg) == (b_res, b_seg)
    assert {k: v for k, v in a_audit.items() if k != "elapsed_sec"} \
        == {k: v for k, v in b_audit.items() if k != "elapsed_sec"}


def test_build_edit_plan_is_pure(tmp_path):
    """조립은 파일을 쓰지 않는다 — 쓰는 자리는 `build_resources` 하나다."""
    grid, span_index, doc = _grid(), _span_index(), _story_doc()
    res.build_edit_plan(doc, span_index, grid, video_path="/x.mp4",
                        work_title="작품")
    assert list(tmp_path.iterdir()) == []
