"""v3의 작품 리서치를 grid-review의 전사·관찰·대본 입력으로 연결한다."""
from __future__ import annotations

import copy
import re
import time

from app.tikitaka.grid import fingerprint


def episode_number(label: str) -> int | None:
    if not label.strip():
        return None
    match = re.fullmatch(r"([1-9]\d*)(?:회|화)?", label.strip())
    if not match:
        raise ValueError("리서치 회차는 양의 정수 또는 '1화'/'1회' 형식이어야 합니다")
    return int(match[1])


def run(job, get_client, *, episode: int | None, force: bool = False) -> dict:
    from app.modules.work_researcher import research_work

    identity = {"title": job.title, "episode": episode}
    name = "checkpoint_research.json"
    meta = "checkpoint_research_meta.json"
    if not force and job.has(name) and job.has(meta) and job.load(meta) == identity:
        cached = job.load(name)
        if cached.get("work_context"):
            job.log("[research] v3 작품 리서치 캐시 로드")
            return cached
    started = time.monotonic()
    job.log(f"[research] v3 작품 리서치 시작 — {job.title} · 회차 {episode}")
    result = research_work(job.title, episode, get_client())
    # v3 pipeline의 checkpoint_research.json과 같은 형태.
    data = {
        "work_context": result.work_context,
        "episodes_context": result.episodes_context,
        "raw_data": result.raw_data,
        "sources": result.sources,
        "cast_images": [
            {"character_name": c.character_name, "actor_name": c.actor_name,
             "role_description": c.role_description,
             "image_path": str(c.image_path) if c.image_path else None,
             "image_url": c.image_url}
            for c in result.characters],
    }
    for filename in (name, meta):
        if job.has(filename):
            job.path(filename).rename(job.path(f"{filename}.prev_{time.time_ns()}"))
    job.save(name, data)
    job.save(meta, identity)
    job.record_step("research", elapsed=round(time.monotonic() - started, 1),
                    has_context=bool(result.work_context), characters=len(result.characters),
                    sources=result.sources)
    job.log(f"[research] 인물 {len(result.characters)}명 · 출처 {len(result.sources)}개")
    if not result.work_context:
        job.log("[research] ⚠ 리서치 결과 없음 — 가이드 인물 정보로 진행; 다음 실행에서 재시도")
    return data


def cast_names(research: dict | None, explicit: list[str], guide: dict | None) -> list[str]:
    names = list(explicit) + list((guide or {}).get("actors", {}))
    for person in (research or {}).get("cast_images") or []:
        names.extend([person.get("character_name"), person.get("actor_name")])
    names.extend((guide or {}).get("actors", {}).values())
    return list(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()))


def context(research: dict | None, cast: list[str], guide: dict | None) -> str:
    chunks = ["[작품·인물 참고 정보 — 영상에서 확인한 사실과 구분한다]",
              "인물 이름은 아래 참고 정보를 사용하라. 가이드 인물·배우 대응이 검색 결과보다 우선한다.",
              "화면/음성으로 누구인지 확실하지 않으면 미상으로 기록하고 임의의 인물명을 만들지 마라.",
              "검색 줄거리에 있다는 이유로 영상에 없는 사건·행동을 관찰에 추가하지 마라."]
    if cast:
        chunks.append("등장인물·배우 후보: " + ", ".join(cast))
    for key in ("work_context", "episodes_context"):
        if (research or {}).get(key):
            chunks.append(research[key])
    actors = (guide or {}).get("actors") or {}
    if actors:
        chunks.append("가이드 인물 정본(극중 인물=배우): " + ", ".join(f"{c}={a}" for c, a in actors.items()))
    return "\n".join(chunks)


def with_context(guide: dict | None, text: str) -> dict:
    """기존 guide 경로로 digest·리빌딩·확인·덮개 단계까지 동일한 리서치를 전달."""
    result = copy.deepcopy(guide) if guide else {
        "files": [], "avoid": [], "text": "", "actors": {}, "exclude": [], "hashtags": []}
    result["text"] = text + "\n\n[제작 가이드 — 충돌 시 아래 지침 우선]\n" + result.get("text", "")
    result["sha"] = fingerprint([result.get("sha"), text])
    return result
