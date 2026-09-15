from pathlib import Path

import pytest

from app.tikitaka import research
from app.tikitaka.cli import cascade_redo
from app.tikitaka.common import Job
from app.modules.work_researcher import CharacterInfo, ResearchResult


def test_reuses_v3_research_and_caches_by_title_episode(tmp_path, monkeypatch):
    calls = []
    def search(title, episode, client):
        calls.append((title, episode, client))
        return ResearchResult("인물 설명", "1회 줄거리", {}, ["https://example.org"],
                              [CharacterInfo("박경희", "김혜수", "인플루언서")])
    monkeypatch.setattr("app.modules.work_researcher.research_work", search)
    job = Job(Path("source.mp4"), tmp_path, "작품")
    first = research.run(job, lambda: "client", episode=1)
    assert first["cast_images"][0]["character_name"] == "박경희"
    assert research.run(job, lambda: pytest.fail("cached client requested"), episode=1) == first
    assert len(calls) == 1
    research.run(job, lambda: "client", episode=2)
    research.run(job, lambda: "client", episode=2, force=True)
    assert [c[1] for c in calls] == [1, 2, 2]
    assert list(tmp_path.glob("checkpoint_research.json.prev_*"))


def test_empty_research_is_visible_and_retried(tmp_path, monkeypatch):
    calls = []
    def search(*args):
        calls.append(1)
        return ResearchResult("", "", {}, [], [])
    monkeypatch.setattr("app.modules.work_researcher.research_work", search)
    job = Job(Path("source.mp4"), tmp_path, "작품")
    for _ in range(2):
        research.run(job, lambda: None, episode=1)
    assert len(calls) == 2
    assert "리서치 결과 없음" in job.path("run.log").read_text()


def test_context_and_guide_keep_identity_and_cache_dependencies():
    assert research.with_context(None, "참고")["actors"] == {}
    guide = {"sha": "original", "text": "제작 지침", "actors": {"박경희": "김혜수"}, "avoid": ["금지"]}
    data = {"work_context": "작품 배경", "episodes_context": "1회 사건", "cast_images": [
        {"character_name": "박경희", "actor_name": "김혜수"}]}
    names = research.cast_names(data, ["게스트"], guide)
    assert names == ["게스트", "박경희", "김혜수"]
    context = research.context(data, names, guide)
    assert all(s in context for s in ("박경희=김혜수", "1회 사건", "미상", "영상에 없는"))
    enriched = research.with_context(guide, context)
    assert enriched["actors"] == guide["actors"] and enriched["avoid"] == ["금지"]
    assert "작품 배경" in enriched["text"] and "제작 지침" in enriched["text"]
    assert guide["sha"] == "original"
    assert enriched["sha"] != research.with_context(guide, context + "변경")["sha"]
    redo = cascade_redo({"research"})
    assert {"index", "digest", "render"} <= redo
    assert "transcribe" not in redo


def test_episode_scope():
    for label in ("1", "1화", "1회"):
        assert research.episode_number(label) == 1
    assert research.episode_number("") is None
    for label in ("-1", "0", "special"):
        with pytest.raises(ValueError):
            research.episode_number(label)
