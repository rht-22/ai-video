"""Channel policy for reading text, separate from subtitle layout and speech."""
from __future__ import annotations

import copy
from collections import Counter

ROLES = frozenset({"decorative", "speech_repeat", "identity", "story_info", "scene_text", "uncertain", "none"})
ESSENTIAL = frozenset({"story_info", "scene_text"})
TEXT_KEYS = ("screen_text", "screen_text_draft", "screen_text_source", "screen_text_kind", "has_text")
INSTRUCTION = """
## 채널 화면 글자 정책 — 위의 모든 글자 전사 지시보다 우선한다
글자의 역할을 screen_text_role로 분류하라:
decorative(로고·감탄·장식), speech_repeat(들리는 대사·가사 반복), identity(이름표),
story_info(대사에 없는 미션 조건·시간 제한·사건 정보), scene_text(문자·기사·편지 등 내용 자체가 근거),
uncertain(역할 불명), none(글자 없음).
판단 기준은 글자를 빼면 누가 무엇을 왜 하는지 이해가 달라지는가다. 방송사가 얹은 글자도
미션 조건처럼 새 사건 정보이면 story_info다. 위치·색상만으로 구분하지 마라.
story_info/scene_text만 screen_text에 필요한 원문을 적고 has_text를 켜라.
나머지 역할은 글자 전체를 전사하지 마라. 이름표는 인물 확인에만 쓰고 가사는 음성 전사에 맡긴다.
uncertain은 추측해서 인용하지 않는다. 관찰한 인물·행동·장면 묘사는 계속 기록하라.
"""


def text_role(span):
    role = span.get("screen_text_role")
    if isinstance(role, str) and role in ROLES:
        return role
    # Older analyses had only source kinds. Do not guess a role from a logo's position
    # or a keyword in an aggregated caption; unknown text stays explicitly uncertain.
    if span.get("screen_text_kind") in {"메시지", "기사", "댓글", "문서", "검색"}:
        return "scene_text"
    return "uncertain" if span.get("has_text") or span.get("screen_text") else "none"


def filter_document(document):
    """Return a view for essential text; preserve the original analysis for auditing."""
    from app.v3.screen_text import refresh_meaning_texts
    out = copy.deepcopy(document)
    counts = Counter()
    removed_chars = 0
    for sq in out.get("sequences", []):
        for ch in sq.get("chunks", []):
            for meaning in ch.get("meanings", []):
                for span in meaning.get("spans", []):
                    role = text_role(span)
                    counts[role] += 1
                    span["screen_text_role"] = role
                    if role not in ESSENTIAL:
                        removed_chars += len(span.get("screen_text") or "")
                        for key in TEXT_KEYS:
                            span.pop(key, None)
    refresh_meaning_texts(out)
    return out, {"roles": dict(counts), "removed_text_chars": removed_chars}


def filter_index(index):
    """Apply the same text view to every consumer without changing IDs or timing."""
    out = copy.deepcopy(index)
    out["v3_stage2"], audit = filter_document(out["v3_stage2"])
    spans = {s["span_id"]: s for sq in out["v3_stage2"]["sequences"]
             for ch in sq["chunks"] for m in ch.get("meanings", []) for s in m["spans"]}
    for sid, fact in out["grid_facts"].items():
        source = spans.get(sid, {})
        for key in (*TEXT_KEYS, "screen_text_role"):
            fact.pop(key, None)
            if key in source:
                fact[key] = copy.deepcopy(source[key])
    for moment in out["moments"]:
        source = out["grid_facts"].get(moment.get("span_id"), {})
        if not source and len(moment.get("span_ids", [])) == 1:
            source = out["grid_facts"].get(moment["span_ids"][0], {})
        for key in (*TEXT_KEYS, "screen_text_role"):
            moment.pop(key, None)
            if key in source:
                moment[key] = copy.deepcopy(source[key])
    for scene in out["scenes"]:
        scene.pop("screen_texts", None)
        texts = [out["grid_facts"][sid]["screen_text"] for sid in scene.get("span_ids", [])
                 if out["grid_facts"].get(sid, {}).get("screen_text")]
        if texts:
            scene["screen_texts"] = texts
    out["screen_text_policy"] = "essential"
    return out, audit
