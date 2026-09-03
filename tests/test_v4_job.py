"""V4-M1 job 규약 회귀 가드 — job 디렉토리·run_log·원자적 기록·지문(계약 §2).

핵심 고정:
  ① job_id 형식·`exist_ok=False`·재개 시 존재 필수 — **v3 인라인 구현과 같은 규약**
     (v3 pipeline.py :131 을 이 파일이 대신하게 되는 날 산출이 안 흔들려야 한다)
  ② run_log 는 **단계마다** 디스크에 확정된다 — v3 는 finally 한 곳뿐이라 SIGKILL 이면
     감사 기록이 통째로 사라졌다(계약이 고친 유일한 것)
  ③ 기록은 원자적 — 임시 파일이 남지 않고, 실패해도 이전 판이 온전히 남는다
  ④ 깨진 run_log 는 그대로 터진다(조용한 초기화 금지) · provenance 는 최초 생성분 유지
  ⑤ 지문은 순수·결정적이고 dict 키 순서에 무관
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from app.modules import job


# ── job 디렉토리 ────────────────────────────────────────────────────────────

def test_new_job_dir_follows_v3_naming(tmp_path):
    """신규 job 디렉토리 이름은 v3 규약: 제목의 공백은 `_` · uuid4 hex 8자 접미."""
    d = job.job_dir_for(tmp_path, "포핸즈 1회", None)
    assert d.is_dir()
    assert d.parent == tmp_path
    title, _, suffix = d.name.rpartition("_")
    assert title == "포핸즈_1회"      # 공백 → _
    assert len(suffix) == 8           # uuid4().hex[:8]
    int(suffix, 16)                   # hex 인지 (아니면 ValueError)


def test_new_job_dir_is_unique_per_call(tmp_path):
    a = job.job_dir_for(tmp_path, "같은 제목", None)
    b = job.job_dir_for(tmp_path, "같은 제목", None)
    assert a != b


def test_new_job_dir_collision_fails_loud(tmp_path, monkeypatch):
    """이미 있는 이름이면 크게 실패한다 — 조용히 재사용하면 남의 체크포인트에 섞인다."""
    fixed = uuid.UUID(int=0x1234)
    monkeypatch.setattr(job.uuid, "uuid4", lambda: fixed)
    job.job_dir_for(tmp_path, "작품", None)
    with pytest.raises(FileExistsError):
        job.job_dir_for(tmp_path, "작품", None)


def test_resume_requires_existing_dir(tmp_path):
    """job_id 를 주면 그 디렉토리가 **이미 있어야** 한다 — '임의 id 로 신규 생성' 불가."""
    with pytest.raises(FileNotFoundError):
        job.job_dir_for(tmp_path, "작품", "작품_deadbeef")
    (tmp_path / "작품_deadbeef").mkdir()
    assert job.job_dir_for(tmp_path, "작품", "작품_deadbeef") == tmp_path / "작품_deadbeef"


def test_resume_with_file_at_that_path_is_not_a_dir(tmp_path):
    """같은 이름의 **파일**이 있어도 재개로 통과시키지 않는다(is_dir 판정)."""
    (tmp_path / "작품_deadbeef").write_text("not a dir", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        job.job_dir_for(tmp_path, "작품", "작품_deadbeef")


# ── run_log 모양 (v3 대조) ──────────────────────────────────────────────────

# v3 가 인라인으로 만드는 run_log 의 필수 키(app/v3/pipeline.py :143~150).
# `input` 은 파이프라인마다 어휘가 달라 부르는 쪽이 얹는다 — 계약 §2 의 명시.
_V3_RUN_LOG_KEYS = {"job_id", "pipeline", "provenance", "steps"}


def test_new_run_log_carries_v3_required_keys():
    rl = job.new_run_log(pipeline="v4", job_id="작품_deadbeef")
    assert _V3_RUN_LOG_KEYS <= set(rl)
    assert rl["schema"] == "run_log/v1"
    assert rl["pipeline"] == "v4"
    assert rl["job_id"] == "작품_deadbeef"
    assert rl["steps"] == []


def test_new_run_log_minimal_provenance_without_config():
    """config 가 없어도 감사 기록은 남는다 — git_sha 등 최소 provenance."""
    pv = job.new_run_log(pipeline="v4", job_id="j")["provenance"]
    assert {"git_sha", "host", "machine"} <= set(pv)
    assert pv["config"] is None


def test_new_run_log_uses_shared_build_provenance(monkeypatch):
    """config 가 있으면 provenance 모듈의 **그 함수**를 부른다(수식 복제 금지)."""
    import app.modules.provenance as prov
    seen = {}

    def fake(config, design=None):
        seen["config"] = config
        return {"git_sha": "abc", "config": {"app": {}}}

    monkeypatch.setattr(prov, "build_provenance", fake)
    sentinel = object()
    rl = job.new_run_log(pipeline="v4", job_id="j", config=sentinel)
    assert seen["config"] is sentinel
    assert rl["provenance"]["git_sha"] == "abc"


# ── 재개 ────────────────────────────────────────────────────────────────────

def test_resume_appends_and_keeps_history(tmp_path):
    """재개는 이어 쓴다 — 기존 steps·provenance 가 살아 있고 resume 한 줄이 붙는다."""
    p = tmp_path / "run_log.json"
    rl = job.new_run_log(pipeline="v4", job_id="j")
    job.append_step(rl, "init")
    rl["provenance"]["git_sha"] = "최초sha"
    rl["input"] = {"video_path": "/a.mp4"}
    job.write_run_log(p, rl)

    back = job.resume_run_log(p, pipeline="v4", job_id="j", from_step="funnel")
    assert [s["step"] for s in back["steps"]] == ["init", "resume"]
    assert back["steps"][-1]["from_step"] == "funnel"
    assert back["provenance"]["git_sha"] == "최초sha"    # 최초 생성분 유지
    assert back["input"] == {"video_path": "/a.mp4"}     # 남의 절은 안 건드린다


def test_resume_does_not_restamp_provenance(tmp_path, monkeypatch):
    """provenance 는 재개마다 다시 스탬핑하지 않는다 — 최초 판이 A/B 대조의 기준이다."""
    import app.modules.provenance as prov
    p = tmp_path / "run_log.json"
    job.write_run_log(p, job.new_run_log(pipeline="v4", job_id="j"))

    def boom(*a, **k):  # 불렸다면 실패
        raise AssertionError("재개에서 provenance 를 다시 만들면 안 된다")

    monkeypatch.setattr(prov, "build_provenance", boom)
    monkeypatch.setattr(job, "_minimal_provenance", boom)
    job.resume_run_log(p, pipeline="v4", job_id="j", from_step=None)


def test_resume_broken_run_log_raises(tmp_path):
    """깨진 run_log 는 그대로 터뜨린다 — 조용한 초기화가 가장 나쁘다."""
    p = tmp_path / "run_log.json"
    p.write_text("{깨진 JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        job.resume_run_log(p, pipeline="v4", job_id="j", from_step=None)


def test_resume_non_object_run_log_raises(tmp_path):
    p = tmp_path / "run_log.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        job.resume_run_log(p, pipeline="v4", job_id="j", from_step=None)


def test_resume_job_id_mismatch_fails_loud(tmp_path):
    """다른 job 의 감사 기록에 이어 쓰지 않는다(두 편의 비용이 한 파일에 섞인다)."""
    p = tmp_path / "run_log.json"
    job.write_run_log(p, job.new_run_log(pipeline="v4", job_id="남의_job"))
    with pytest.raises(ValueError) as e:
        job.resume_run_log(p, pipeline="v4", job_id="내_job", from_step=None)
    assert "남의_job" in str(e.value) and "내_job" in str(e.value)


def test_resume_pipeline_rename_is_recorded_not_fatal(tmp_path):
    """파이프라인 이름은 마일스톤마다 바뀌어 왔다(v3_m1→v3_m3) — 막지 않고 남긴다."""
    p = tmp_path / "run_log.json"
    job.write_run_log(p, job.new_run_log(pipeline="v4_m1", job_id="j"))
    back = job.resume_run_log(p, pipeline="v4_m2", job_id="j", from_step=None)
    assert back["pipeline"] == "v4_m1"                       # 최초 값 유지
    assert back["steps"][-1]["pipeline_requested"] == "v4_m2"  # 보이게 남는다


def test_resume_without_file_creates_new(tmp_path):
    """파일이 없어도 재개 지점은 기록에 남는다."""
    back = job.resume_run_log(tmp_path / "run_log.json",
                              pipeline="v4", job_id="j", from_step="flags")
    assert back["steps"] == [{"step": "resume", "from_step": "flags"}]
    assert back["provenance"]["git_sha"] is None or isinstance(
        back["provenance"]["git_sha"], str)


# ── append_step ─────────────────────────────────────────────────────────────

def test_append_step_returns_the_entry_and_grows_in_place():
    rl = job.new_run_log(pipeline="v4", job_id="j")
    entry = job.append_step(rl, "upload", elapsed=1.5, usage={"input": 10})
    assert rl["steps"][-1] is entry
    assert entry == {"step": "upload", "elapsed": 1.5, "usage": {"input": 10}}


def test_append_step_tolerates_missing_steps_key():
    rl = {"pipeline": "v4"}
    job.append_step(rl, "init")
    assert rl["steps"] == [{"step": "init"}]


# ── 원자적 기록 · 단계마다 확정 ─────────────────────────────────────────────

def test_write_run_log_leaves_no_temp_file(tmp_path):
    """임시 파일이 남으면 다음 실행이 남의 조각을 보고 헷갈린다."""
    p = tmp_path / "run_log.json"
    job.write_run_log(p, job.new_run_log(pipeline="v4", job_id="j"))
    assert [f.name for f in tmp_path.iterdir()] == ["run_log.json"]


def test_failed_write_keeps_previous_content_and_no_temp(tmp_path, monkeypatch):
    """쓰는 중에 죽어도 독자는 **이전 판**을 온전히 본다(os.replace 전에는 원본 그대로).

    직렬화가 터지는 경우로 '쓰는 중 죽음'을 흉내 낸다 — 임시 파일도 남지 않아야 한다.
    """
    p = tmp_path / "run_log.json"
    job.write_run_log(p, {"schema": "run_log/v1", "steps": [{"step": "init"}]})
    with pytest.raises(TypeError):
        job.write_run_log(p, {"steps": [{"step": "bad", "x": object()}]})
    assert job.read_json(p)["steps"] == [{"step": "init"}]
    assert [f.name for f in tmp_path.iterdir()] == ["run_log.json"]


def test_run_log_is_on_disk_after_every_step(tmp_path):
    """🛑 계약이 고친 유일한 것 — v3 는 finally 한 곳에서만 썼다.

    단계마다 디스크를 읽어 그 시점까지의 기록이 **전부** 있는지 본다(프로세스가
    다음 줄에서 SIGKILL 돼도 남아 있어야 하는 내용).
    """
    p = tmp_path / "run_log.json"
    rl = job.new_run_log(pipeline="v4", job_id="j")
    step = job.make_step_logger(rl, p)
    names = ["init", "research", "transcribe", "probe", "upload"]
    for i, name in enumerate(names, start=1):
        step(name, elapsed=float(i))
        on_disk = job.read_json(p)
        assert [s["step"] for s in on_disk["steps"]] == names[:i]
        assert on_disk["steps"][-1]["elapsed"] == float(i)


def test_write_json_and_read_json_roundtrip_and_missing(tmp_path):
    p = tmp_path / "sub" / "doc.json"
    doc = {"한글": ["값", 1, None], "b": {"a": 1}}
    job.write_json(p, doc)
    assert job.read_json(p) == doc
    assert "한글" in p.read_text(encoding="utf-8")      # ensure_ascii=False
    with pytest.raises(FileNotFoundError):
        job.read_json(tmp_path / "없는파일.json")


def test_write_json_serialization_matches_v3(tmp_path):
    """v3 `_write_json` 과 같은 바이트를 낸다 — 같은 내용의 산출이 갈리면 대조가 깨진다."""
    doc = {"a": 1, "한": ["글", {"b": None}]}
    p = tmp_path / "doc.json"
    job.write_json(p, doc)
    assert p.read_text(encoding="utf-8") == json.dumps(doc, ensure_ascii=False,
                                                       indent=1)


# ── 지문 ────────────────────────────────────────────────────────────────────

def test_fingerprint_is_deterministic_and_key_order_free():
    a = job.fingerprint({"b": 1, "a": [1, 2]}, "x", 3.0)
    b = job.fingerprint({"a": [1, 2], "b": 1}, "x", 3.0)
    assert a == b == job.fingerprint({"b": 1, "a": [1, 2]}, "x", 3.0)
    assert len(a) == 16 and all(c in "0123456789abcdef" for c in a)


def test_fingerprint_changes_with_any_material():
    base = job.fingerprint("grid#1", 3.0, "prompt#1")
    assert base != job.fingerprint("grid#2", 3.0, "prompt#1")
    assert base != job.fingerprint("grid#1", 2.0, "prompt#1")
    assert base != job.fingerprint("grid#1", 3.0, "prompt#2")
    assert base != job.fingerprint("grid#1", 3.0)          # 재료 수가 줄어도 다르다
    # 순서도 재료다 — 자리 바꿈이 같은 지문을 내면 캐시가 다른 구성을 재사용한다
    assert job.fingerprint("a", "b") != job.fingerprint("b", "a")


def test_fingerprint_rejects_unserializable():
    """`default=str` 로 넘기면 객체 주소가 섞여 캐시가 영구 무효가 된다 — 터뜨린다."""
    with pytest.raises(TypeError):
        job.fingerprint(object())


def test_fingerprint_is_pure(tmp_path):
    """넘겨받은 재료를 건드리지 않는다."""
    material = {"b": [1, 2], "a": {"x": 1}}
    before = json.dumps(material, sort_keys=True)
    job.fingerprint(material)
    assert json.dumps(material, sort_keys=True) == before


# ── 파일시스템 규약 ─────────────────────────────────────────────────────────

def test_atomic_write_uses_same_directory_temp(tmp_path, monkeypatch):
    """임시 파일은 **같은 디렉토리**에 만든다 — 다른 파일시스템이면 rename 이 원자적이지
    않다(/tmp 로 새면 조용히 비원자적이 된다)."""
    seen = {}
    real = job.tempfile.mkstemp

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(job.tempfile, "mkstemp", spy)
    p = tmp_path / "nested" / "run_log.json"
    job.write_run_log(p, {"steps": []})
    assert Path(seen["dir"]) == p.parent
    assert os.path.isfile(p)
