"""3.7단계 — 작품 이해(digest): 대본을 짜기 **전에** 회차 전체를 읽고 "누가 누구이고 왜 그렇게 행동하는지"를 문서로 만든다.

2026-09-12 사용자 지적(2화): "전체 영상을 관통하는 내용이나 왜 등장인물이 저런 행동을 하는지 파악을 하고 대본을 짜야지" —
리빌딩·확인 패스 프롬프트에는 장면별 60자 요약과 대사만 들어갔다. 그래서 v10 은 "의뢰 → 납치 → 사고" 같은 인과를 지어내고,
v7 확인 패스는 얼굴 인상으로 김재철을 김지훈으로 바꿔 적었다.

산출 `digest.json` {digest, sha, model} · `digest.md`(사람용). 캐시 키는 인덱스(장면·순간·화자)·전사 텍스트·가이드 sha —
목소리 대조(3.5)가 화자를 고치면 다시 만든다. 프롬프트 주입은 `digest_block` 한 함수(리빌딩·확인 패스가 같은 문서를 본다).
"""
from __future__ import annotations

import hashlib
import json

from app.tikitaka.common import Job, fmt_tc
from app.tikitaka.llm import Gemini
from app.tikitaka.guide import guide_block

DIGEST_PROMPT = """# 작품 이해 문서 — 쇼츠 대본을 짜기 전에

너는 드라마 편집 PD 다. 아래는 작품 「{title}」 {episode} 한 회차의 **전체 소스 스크립트**(장면·화자 붙은 대사·시청각 순간)와
10분 창별 줄거리다. 쇼츠 대본을 쓰기 전에 회차 전체를 이해한 문서를 만든다. 이 문서는 대본 작가(다음 단계 모델)에게
**누가 누구이고(관계), 각 인물이 무엇을 원해서 왜 그렇게 행동하는지(동기), 어떤 사건이 어떤 사건을 낳는지(인과)** 를 알려준다.
작가는 이 문서에 없는 인과를 지어내면 안 되고, 인물 식별은 소스 스크립트의 화자 표기와 이 문서의 관계표를 따른다.

규칙
- 인물 이름은 **소스 스크립트의 화자 표기 그대로**(극중 이름). 닮은 인물·헷갈리기 쉬운 인물은 cautions 에 구분법을 적는다
  (예: 두 남편의 차이 — 누구의 집·차·직장인지).
- 인과는 화면·대사에 근거한 것만. 추정이면 "(추정)"을 붙인다. 모르는 것은 모른다고 쓴다.
- 각 장면(SC-xxx)마다 "무슨 일 → 왜 그렇게 하나 → 뒤에 무엇을 낳나"를 한 줄씩. 장면 요약을 베끼지 말고 **동기와 결과**를 쓴다.
- 반전·결말은 있는 그대로 적되, 아래 제작 가이드가 노출을 막는 내용은 cautions 에 "쇼츠에서 말하지 말 것"으로 표시한다.{guide}

## 출력 JSON 하나(코드블록 금지)
{{"logline": "회차 한 줄(≤60자)",
 "plot": ["기: …", "승: …", "전: …", "결: …"],
 "characters": [{{"name": "극중 이름", "role": "직업/위치(누구의 남편·딸·변호사…)", "wants": "이 회차에서 원하는 것",
                 "why": "그렇게 행동하는 이유(동기·상처·계산)", "state": "회차 끝에서의 상태"}}],
 "relationships": ["A—B: 관계 한 줄"],
 "threads": [{{"name": "관통하는 줄기", "beats": ["SC-00x: 무슨 일 → 왜 → 다음에 무엇을 낳나"]}}],
 "scene_notes": [{{"id": "SC-001", "what": "무슨 일", "why": "인물이 그렇게 하는 이유", "sets_up": "뒤에 낳는 것"}}],
 "turning_points": ["MM:SS 근처 — 무엇이 바뀌나"],
 "hooks": ["쇼츠 훅이 될 순간/대사(L-/S- ID) — 왜 강한지"],
 "cautions": ["작가가 오해하기 쉬운 것 — 닮은 인물 구분 · 오독하기 쉬운 인과 · 쇼츠에서 말하면 안 되는 반전"]}}

## 10분 창별 줄거리(인덱스 모델이 영상을 보고 쓴 것)
{synopses}

## 소스 스크립트
{script}
"""


def digest_sha(index: dict, transcript: dict, guide: dict | None) -> str:
    """캐시 키 — 장면·순간 ID, 줄별 화자·텍스트, 가이드 sha. 순수 — 테스트 대상."""
    h = hashlib.sha1()
    for s in index.get("scenes") or []:
        h.update(f"{s.get('id')}|{s.get('summary')}|{','.join(s.get('chars') or [])}".encode("utf-8"))
    for m in index.get("moments") or []:
        h.update(f"{m.get('id')}|{m.get('desc')}".encode("utf-8"))
    for l in transcript.get("lines") or []:
        h.update(f"{l.get('id')}|{l.get('text')}".encode("utf-8"))      # 화자 제외(2026-09-22) — 확인 패스의 화자 교정으로 매 실행 재생성되던 것
    h.update(((guide or {}).get("sha") or "").encode("utf-8"))
    return h.hexdigest()[:12]


def _clean(d: dict) -> dict:
    """모델 산출 정규화 — 키 누락은 빈 값, 문자열은 공백 정리. 순수 — 테스트 대상."""
    def s(x):
        return " ".join(str(x or "").split())
    out = {"logline": s(d.get("logline")), "plot": [s(x) for x in (d.get("plot") or []) if s(x)],
           "characters": [], "relationships": [s(x) for x in (d.get("relationships") or []) if s(x)], "threads": [],
           "scene_notes": [], "turning_points": [s(x) for x in (d.get("turning_points") or []) if s(x)],
           "hooks": [s(x) for x in (d.get("hooks") or []) if s(x)], "cautions": [s(x) for x in (d.get("cautions") or []) if s(x)]}
    for c in d.get("characters") or []:
        if isinstance(c, dict) and s(c.get("name")):
            out["characters"].append({k: s(c.get(k)) for k in ("name", "role", "wants", "why", "state")})
    for t in d.get("threads") or []:
        if isinstance(t, dict) and s(t.get("name")):
            out["threads"].append({"name": s(t.get("name")), "beats": [s(b) for b in (t.get("beats") or []) if s(b)]})
    for n in d.get("scene_notes") or []:
        if isinstance(n, dict) and s(n.get("id")):
            out["scene_notes"].append({k: s(n.get(k)) for k in ("id", "what", "why", "sets_up")})
    return out


def build_digest(job: Job, gemini: Gemini, index: dict, transcript: dict, *, title: str, episode_label: str,
                 guide: dict | None = None) -> dict | None:
    """회차 이해 문서. 캐시가 같은 sha 면 재사용. 실패는 None + 로그(대본 단계는 문서 없이도 돈다 — 종전 동작)."""
    from app.tikitaka.rebuild import source_script
    sha = digest_sha(index, transcript, guide)
    if job.has("digest.json"):
        cached = job.load("digest.json")
        if cached.get("sha") == sha and cached.get("digest"):
            return cached["digest"]
        job.log(f"[digest] 캐시 sha {cached.get('sha')} ≠ {sha}(화자·전사·가이드 변경) → 다시 만든다")
    script = source_script(index, transcript, None)          # 이해는 전체를 본다 — 활용 불가 구간도 알아야 스포일러를 지킨다
    synopses = "\n".join(f"- 창 {w.get('i')} [{fmt_tc(w.get('start', 0))[:5]}~{fmt_tc(w.get('end', 0))[:5]}] {w.get('synopsis') or ''}"
                         for w in (index.get("windows") or []))
    prompt = DIGEST_PROMPT.format(title=title, episode=episode_label, guide=guide_block(guide), synopses=synopses, script=script)
    job.log(f"[digest] 작품 이해 문서 생성 — 장면 {len(index.get('scenes') or [])} · 줄 {len(transcript.get('lines') or [])} · 스크립트 {len(script):,}자")
    try:
        raw = gemini.text_json(prompt, kind="digest", thinking="high")
    except Exception as e:  # noqa: BLE001
        job.log(f"[digest] ⚠ 생성 실패 → 이해 문서 없이 진행: {type(e).__name__}: {str(e)[:200]}")
        job.record_step("digest", status="failed", error=str(e)[:300])
        return None
    d = _clean(raw if isinstance(raw, dict) else {})
    if not d["characters"] or not d["plot"]:
        job.log("[digest] ⚠ 산출이 비었다(인물/줄거리 없음) → 이해 문서 없이 진행")
        job.record_step("digest", status="empty")
        return None
    job.save("digest.json", {"digest": d, "sha": sha, "model": gemini.text_model})
    job.path("digest.md").write_text(digest_md(d, title=title, episode=episode_label), encoding="utf-8")
    job.log(f"[digest] 인물 {len(d['characters'])} · 관계 {len(d['relationships'])} · 줄기 {len(d['threads'])} · 장면 메모 {len(d['scene_notes'])} · "
            f"주의 {len(d['cautions'])} — {d['logline']}")
    job.record_step("digest", characters=len(d["characters"]), threads=len(d["threads"]), scene_notes=len(d["scene_notes"]),
                    llm=gemini.usage.calls[-1:])
    return d


def digest_block(digest: dict | None, *, max_scene_notes: int = 60) -> str:
    """프롬프트에 붙일 [작품 이해] 절. 없으면 빈 문자열(프롬프트 바이트 동일 — 회귀 0). 순수 — 테스트 대상."""
    if not digest or not digest.get("characters"):
        return ""
    L = ["", "", "## [작품 이해 — 대본을 짜기 전에 읽는다. 인물·관계·인과는 이 문서 기준]",
         f"- 한 줄: {digest.get('logline', '')}"]
    if digest.get("plot"):
        L.append("- 줄거리: " + " / ".join(digest["plot"]))
    if digest["characters"]:
        L.append("- 인물(극중 이름 — 역할 · 원하는 것 · 왜):")
        for c in digest["characters"]:
            L.append(f"  · {c['name']} — {c.get('role', '')} · 원함: {c.get('wants', '')} · 왜: {c.get('why', '')}")
    if digest.get("relationships"):
        L.append("- 관계: " + " / ".join(digest["relationships"]))
    for t in digest.get("threads") or []:
        L.append(f"- 줄기 「{t['name']}」: " + " → ".join(t.get("beats") or []))
    notes = digest.get("scene_notes") or []
    if notes:
        L.append("- 장면별 동기·결과:")
        for n in notes[:max_scene_notes]:
            L.append(f"  · {n['id']}: {n.get('what', '')} — 왜: {n.get('why', '')} — 낳는 것: {n.get('sets_up', '')}")
    if digest.get("turning_points"):
        L.append("- 전환점: " + " / ".join(digest["turning_points"]))
    if digest.get("hooks"):
        L.append("- 훅 후보: " + " / ".join(digest["hooks"]))
    if digest.get("cautions"):
        L.append("- ⚠ 주의(오해하기 쉬운 것·말하면 안 되는 것): " + " / ".join(digest["cautions"]))
    L.append("- 규칙: 내레이션·제목이 말하는 **인과(~때문에, 결국 ~로 번진다)와 관계(전남편·딸·상대 변호사)는 위 문서에 있는 것만**. 인물 식별은 "
             "소스 스크립트의 화자 표기와 관계표를 따른다 — 화면 인상(닮은 얼굴)으로 인물을 바꾸지 마라. 제목은 그 장면에서 실제로 일어난 일을 말한다.")
    return "\n".join(L)


def digest_md(d: dict, *, title: str, episode: str) -> str:
    L = [f"# 작품 이해 — 「{title}」 {episode}", "", f"**{d.get('logline', '')}**", "", "## 줄거리"] + [f"- {p}" for p in d.get("plot", [])]
    L += ["", "## 인물"] + [f"- **{c['name']}** — {c.get('role', '')}. 원함: {c.get('wants', '')}. 왜: {c.get('why', '')}. 끝: {c.get('state', '')}"
                          for c in d.get("characters", [])]
    L += ["", "## 관계"] + [f"- {r}" for r in d.get("relationships", [])]
    for t in d.get("threads", []):
        L += ["", f"## 줄기: {t['name']}"] + [f"- {b}" for b in t.get("beats", [])]
    L += ["", "## 장면별 동기·결과"] + [f"- {n['id']}: {n.get('what', '')} — 왜: {n.get('why', '')} — 낳는 것: {n.get('sets_up', '')}"
                                   for n in d.get("scene_notes", [])]
    L += ["", "## 전환점"] + [f"- {x}" for x in d.get("turning_points", [])]
    L += ["", "## 훅 후보"] + [f"- {x}" for x in d.get("hooks", [])]
    L += ["", "## 주의"] + [f"- {x}" for x in d.get("cautions", [])]
    return "\n".join(L) + "\n"
