"""4.5단계 — 영상 확인 패스(verify). 2026-09-10 사용자 결정으로 파이프라인에 고정.

텍스트 리빌딩(4단계)이 10버전을 싸게 펼쳐 추천을 고르면, **그 버전만** 인덱스 텍스트 + 영상(agentic)을 함께 주고
후보 장면을 직접 확인하며 다시 짠다(실험 E3 의 정식화). 산출은 여전히 ID 로만(L-/S-) — 시각 확정은 5단계 코드가 한다.

- 기본 태도: 추천 버전의 전략·이야기를 유지하고, ① 화면에 없는 장면·과장은 빼고 ② 60자 요약에 안 잡힌 시각 비트를 A/N 으로
  보강한다. 이야기를 통째로 바꿀 명백한 이유가 있으면 바꾸되 `changes` 에 적는다.
- 산출 `verified_v{n}.json`(검증·다듬기 끝난 버전 + looked_at + changes) · `verified_v{n}.md`. 5단계는 이 파일이 있으면
  rebuild.json 의 그 버전 대신 이걸 쓴다. `--no-verify` 면 단계 자체가 없다(종전 P 경로).
- 실패(호출·파싱·검증 후 항목 0)는 크게 남기고 **텍스트 버전으로 진행**한다 — 확인 패스는 보강이지 발행 조건이 아니다.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.tikitaka.title import title_prompt
from app.tikitaka.common import Job, fmt_tc, ms3
from app.tikitaka.llm import Gemini
from app.tikitaka.rebuild import (validate_versions, source_script, polish_literal_actions, polish_guide, apply_name_map, TARGET_MIN_SEC,
                                  TARGET_MAX_SEC, LINEAR_STRATEGIES, HOOK_STRATEGIES, SHORT_FORM_STRATEGIES, polish_character_names)
from app.tikitaka.guide import guide_block, excluded_ranges
from app.tikitaka.digest import digest_block
from app.tikitaka.timing import narration_plan_sec

VERIFY_PROMPT = """# 📜 티키타카 스크립트 리빌딩 — 영상 확인 패스

## [System Role]
너는 '유니버설 비선형 편집 아키텍트'다. 첨부 영상은 작품 「{title}」 {episode} 원본 전체(360p·1fps 프록시)이고,
[소스 스크립트]는 그 영상을 미리 채록한 텍스트(장면·화자 붙은 대사·시청각 순간)다. 아래 [추천 대본]은 텍스트만 보고 짠 초안이다.
네 일은 **초안이 가리키는 장면들을 영상에서 직접 확인하고** 최종 대본을 내는 것이다.

## 확인 규칙
1. 초안의 포맷({strategy})과 이야기 골격을 **기본으로 유지**한다. 잘 서 있으면 그대로 둬도 된다. 단 포맷은 도구이지 틀이 아니다 —
   재료가 포맷을 못 받쳐 주면(질문을 던졌는데 답 장면이 없다 · 반전을 예고했는데 반전이 없다) 포맷을 버리고 자연스러운 흐름으로 고쳐라.
   **마지막 항목은 이야기를 닫아야 한다** — 마지막 내레이션이 "…싶었지만"처럼 이어질 듯 끝나거나, 던진 질문이 회수되지 않으면 고친다.
   가장 중요한 기준은 보는 사람이 자연스럽게 이해되는가다.
2. 각 S/A 항목이 가리키는 구간을 영상에서 본다. 초안이 말하는 상황·표정·소리가 화면에 실제로 있는지 확인하고, 없거나 약하면
   빼거나 바꾼다. 제목·내레이션의 **신체 동작 묘사는 화면에 실제로 있는 것만**(비유적 동작 금지 — 시청자가 화면과 대조한다).
3. 텍스트 요약(60자)에 안 잡힌 **시각 비트**(표정 변화, 손·소품 클로즈업, 정적, 소리)가 그 장면에 있으면 A 항목이나 N 문장으로
   살린다. A는 소스 스크립트의 정확한 순간(S-xxx) ID만 쓴다. N은 문장과 실제 덮개 sp ID를 함께 지정한다. 딱 맞는 화면이 없으면
   가장 가까운 순간으로 대신하지 말고, 확인한 화면이 실제로 보여주는 내용으로 문장을 바꾸거나 그 N을 쓰지 않는다.
4. 이야기를 통째로 바꿔야 할 **명백한 이유**(초안의 핵심 장면이 화면에 없다, 훨씬 강한 장면을 확인했다)가 있을 때만 바꾸고
   `changes` 에 이유를 적는다. 확인하지 않은 장면은 쓰지 않는다.
5. 원본 대사는 줄(L-xxx) ID 로만, 화자가 바뀌어도 의미가 이어지는 연속 줄은 함께 묶는다. 대화·듀엣의 응답과 문장 끝을 빠뜨리지 않는다. 비연속 줄은 별도 S 항목으로 나눈다. 합계 {target_min}~{target_max}초(N=글자수/4초, S=줄 길이, A=순간 길이).
6. 내레이션은 설명만 늘어놓지 말고 절반쯤은 **크리에이터의 리액션·의견**(시청자에게 말 걸기 · 1인칭 감상 · 드립 · 팩폭)으로 — 초안이
   해설 위주면 몇 줄을 의견형으로 바꾼다(8~30자 · 화면에 있는 사실만).
7. **내레이션이 바로 뒤 대사의 내용을 미리 말하지 않는다** — 대사가 말할 것을 내레이션이 먼저 요약하면 같은 말이 두 번 나온다(실측: "무시하기로
   합의!" 뒤에 "인사도 안 해" 대사). 내레이션은 앞 대사의 반응/의견이거나 건너뛴 시간을 잇는 말이어야 한다.
8. **제목이 약속한 것은 대본 안에 있어야 한다** — 제목이 "전말·이유·정체·결말"을 말하면 그 내용이 실제로 나오는 대사/장면이 있어야 한다.
   없으면 제목을 대본 내용에 맞게 고쳐라(제목이 대본을 과장하면 시청자가 속았다고 느낀다).
9. 1.2초 미만의 짧은 대사 조각은 앞뒤 줄과 묶거나(화자 전환을 포함한 연속 줄) 뺀다 — "싹 무시해." 처럼 맥락 없는 꼬리만 남기지 않는다. 단독으로
   훅이 되는 짧은 말("어?", "뭐?")은 예외.
10. **화자 확인**: 소스 스크립트의 화자 표기가 영상과 다르면(입이 움직이는 사람이 다른 인물) `speaker_fixes` 에 줄 ID → 맞는 인물 이름을
    적어라(실측: "조심 좀 하지." 를 임재홍으로 적었지만 화면에선 박경희가 말했다). 확인한 줄만.
13. **인물 식별은 소스 스크립트의 화자 표기와 [작품 이해]의 관계표가 정본이다** — 화면의 얼굴 인상으로 내레이션·제목의 인물 이름을 바꾸지 마라
    (실측 2화 v7: 초안 "김재철"이 맞았는데 확인 패스가 닮은 얼굴을 보고 "김지훈"으로 고쳐 적었다). 화자 표기가 틀렸다고 확신할 때만 규칙 10 의
    speaker_fixes 로 고치고, 내레이션의 인물명은 그 줄의 화자·장면 등장 인물과 일치해야 한다. 인과·동기도 [작품 이해]에 있는 것만 말한다.{strategy_note}{guide}{digest}

## 항목 어휘
{{"type":"N","text":"…","effect":"[…]"|null}} · {{"type":"S","line_ids":["L-045","L-046"],"effect":…}} · {{"type":"A","moment_id":"S-012","effect":…}}

## 출력 JSON 하나(코드블록 금지)
{{"n": {n}, "strategy": "{strategy}", "title": {{"line1": "상황·조건", "line2": "핵심 행동·반응"}}, "structure": "…",
 "looked_at": [{{"start": "MM:SS.ms", "end": "MM:SS.ms", "why": "무엇을 확인했나"}}, …],
 "changes": ["초안 대비 바꾼 것과 이유", …],
 "speaker_fixes": {{"L-606": "박경희", …}}  (없으면 {{}}),
 "items": [ … ],
 "analysis": {{"grade": "매우 안전|안전|보통", "viral_point": "…", "comment": "…"}}}}

## 추천 대본 (초안 · 버전 {n} · {strategy} · 제목 "{draft_title}")
{draft}

## 소스 스크립트
{script}
"""


def apply_speaker_fixes(job: Job, transcript: dict, index: dict, fixes, *, cast: list[str]) -> dict[str, str]:
    """확인 패스가 영상에서 본 화자 교정(줄 ID → 이름)을 전사·인덱스에 반영하고 저장한다. 등장인물 목록에 있는 이름만(모델의 오타·배우명 차단).
    바뀐 줄 {id: 이름}. 순수 아님(파일 저장) — 규칙은 테스트 대상(_valid_fixes)."""
    ok = _valid_fixes(fixes, transcript, cast)
    if not ok:
        return {}
    by = {l["id"]: l for l in transcript["lines"]}
    for lid, name in ok.items():
        by[lid]["speaker"] = name
        index.setdefault("speakers", {})[lid] = name
    job.save("transcript.json", transcript)
    job.save("index.json", index)
    return ok


def _valid_fixes(fixes, transcript: dict, cast: list[str]) -> dict[str, str]:
    if not isinstance(fixes, dict):
        return {}
    by = {l["id"]: l for l in transcript["lines"]}
    out: dict[str, str] = {}
    for lid, name in fixes.items():
        lid, name = str(lid).strip(), " ".join(str(name or "").split())
        if lid in by and name and name in cast and by[lid].get("speaker") != name:
            out[lid] = name
    return out


def draft_block(version: dict) -> str:
    out = []
    for k, it in enumerate(version["items"], 1):
        if it["type"] == "N":
            out.append(f"{k}. [N] \"{it['text']}\" (effect {it.get('effect')})")
            if it.get("production_plan"):
                out.append(f"   문장·화면 계획: {it['production_plan']}")
        elif it["type"] == "S":
            out.append(f"{k}. [S] {'+'.join(it['line_ids'])} {it.get('speaker')}: \"{it['text']}\" (effect {it.get('effect')})")
        else:
            out.append(f"{k}. [A] {it['moment_id']} {it.get('who') or ''} {it['desc']} (effect {it.get('effect')})")
    return "\n".join(out)


def diff_summary(draft: dict, final: dict) -> dict:
    """초안 대비 무엇이 달라졌는지(코드가 센다 — 모델 자기 보고와 별개)."""
    d_lines = [i for it in draft["items"] if it["type"] == "S" for i in it["line_ids"]]
    f_lines = [i for it in final["items"] if it["type"] == "S" for i in it["line_ids"]]
    d_mom = [it["moment_id"] for it in draft["items"] if it["type"] == "A"]
    f_mom = [it["moment_id"] for it in final["items"] if it["type"] == "A"]
    return {"title_changed": draft["title"] != final["title"],
            "items": f"{len(draft['items'])} → {len(final['items'])}",
            "plan_sec": f"{draft['plan_sec']:.1f} → {final['plan_sec']:.1f}",
            "lines_kept": len(set(d_lines) & set(f_lines)), "lines_added": sorted(set(f_lines) - set(d_lines)),
            "lines_removed": sorted(set(d_lines) - set(f_lines)),
            "moments_added": sorted(set(f_mom) - set(d_mom)), "moments_removed": sorted(set(d_mom) - set(f_mom))}


def _gate(rebuild: dict):
    """대본 흐름에 맞는 게이트 — staged 는 고치는 순서가 다르다(화면 교체 → 의도 유지 재작성 → 선택 자리는 뺌)."""
    if rebuild.get("script_flow") == "staged":
        from app.tikitaka.staged import enforce_staged_plans
        return enforce_staged_plans
    from app.tikitaka.production import enforce_joint_plans
    return enforce_joint_plans


def verify_version(job: Job, gemini: Gemini, rebuild: dict, version_n: int, index: dict, transcript: dict, proxy: Path,
                   *, title: str, episode: str, guide: dict | None = None, extra_exclude: list[tuple[float, float]] | None = None,
                   tag: str = "", digest: dict | None = None) -> dict:
    """추천 버전 → 영상 확인 패스 → 검증·다듬기된 최종 버전. 캐시 `verified_v{n}[_tag].json`. 실패 시 초안 그대로(기록).
    digest = 작품 이해 문서(3.7) — 프롬프트에 [작품 이해] 절로 들어간다(없으면 종전 프롬프트)."""
    sfx = f"_{tag}" if tag else ""
    name = f"verified_v{version_n}{sfx}.json"
    if job.has(name):
        cached = job.load(name)
        if not index.get("grid_facts"):
            return cached["version"]
        from app.tikitaka.production import SCHEMA as production_schema
        if cached.get("production_plan_schema") == production_schema:
            _gate(rebuild)(job, gemini, cached["version"], index, transcript,
                                sorted(excluded_ranges(guide, max((l["end"] for l in transcript["lines"]), default=0.0) + 3600.0) + list(extra_exclude or [])))
            job.save(name, cached)
            return cached["version"]
        job.log(f"[verify] 문장·덮개 공동 계획 스키마 변경 → v{version_n} 확인 패스 재실행")
    draft = next(v for v in rebuild["versions"] if v["n"] == version_n)
    exclude = sorted(excluded_ranges(guide, max((l["end"] for l in transcript["lines"]), default=0.0) + 3600.0) + list(extra_exclude or []))
    strategy_note = ""
    if draft["strategy"] in LINEAR_STRATEGIES:
        strategy_note = (f"\n11. 이 버전은 **선형 서사 계열({draft['strategy']})** 이다 — 항목의 원본 순서를 유지한다"
                         + ("(콜드오픈 첫 항목만 예외)" if draft["strategy"] in HOOK_STRATEGIES and rebuild.get("seq_hook", True) else "")
                         + ". 순서를 섞어 훅을 만들지 마라.")
        if draft["strategy"] == "루프형":
            strategy_note += ("\n12. 루프형의 끝: 마지막 항목은 **첫 대사를 직접 유발하는 같은 대화의 바로 앞 대사(S)** 여야 하고, 혼자 들어도 뜻이 통해야"
                              " 한다(맥락 없는 조각 금지 — 실측: \"그것도 막 빨간 하트로.\"). \"그리고 다시—\" 같은 메타 내레이션은 쓰지 않는다 —"
                              " 마지막은 대사로 끝난다. 초안이 이 조건에 안 맞으면 마지막 두 항목을 고쳐라.")
        if draft["strategy"] in SHORT_FORM_STRATEGIES:
            lo, hi = SHORT_FORM_STRATEGIES[draft["strategy"]]
            strategy_note += (f"\n12. 이 포맷은 **길이 예외 {lo}~{hi}초**다(위 {TARGET_MIN_SEC}초 하한을 따르지 않는다). 내레이션은 0~3줄 —"
                              " 이유·배경·인과·교훈(\"~때문에\", \"사실은\")을 말하지 않고, 끝은 해결·사이다가 아니라 리액션이다."
                              " 길이를 채우려고 넣은 설명 내레이션은 빼라.")
    prompt = VERIFY_PROMPT.format(title=title, episode=episode, strategy=draft["strategy"], n=version_n, draft_title=draft["title"],
                                  draft=draft_block(draft), script=source_script(index, transcript, exclude),
                                  target_min=TARGET_MIN_SEC, target_max=TARGET_MAX_SEC, guide=guide_block(guide), strategy_note=strategy_note,
                                  digest=digest_block(digest))
    feedback_name = f"production_retry_v{version_n}{sfx}.json"
    if job.has(feedback_name):
        feedback = job.load(feedback_name)
        prompt += ("\n## 이전 조립 실패 피드백\n" + str(feedback.get("error") or "")
                   + "\n실패한 항목의 문장·화면 계획만 실제 재료에 맞게 다시 작성하라. "
                   "같은 주제와 정상 대사를 보존하고, 없는 행동이나 감정을 지어내지 마라. "
                   "피드백이 화면과 문장의 사실 모순이면 사건의 의미는 유지하되 영상에서 직접 관찰되는 사실로 내레이션을 다시 쓰고, "
                   "그 사실을 실제로 보여 주는 덮개만 지정하라. 이때 실패한 내레이션을 같은 문장으로 반환하면 안 된다. "
                   "피드백의 seen 설명을 우선 근거로 삼고, 고친 문장과 이유를 changes에도 명시하라. "
                   "화면 길이 부족만 문제라면 내레이션 문구를 유지하고 "
                   "덮개 확장·추가·뒤 대사 화면 재사용으로 충분한 ID를 지정하라.")
    prompt += title_prompt((guide or {}).get("title_fit"))
    if index.get("grid_facts"):
        from app.tikitaka.production import planning_rules, preflight
        prompt += planning_rules() + f"\n초안의 조립 사전검사(추정): {preflight(draft, index, transcript, exclude)}"
        prompt += "\n영상 확인 시 문제가 있는 N 문장과 화면 계획만 함께 고쳐라. 정상 대사·다른 항목은 보존하라."
        if rebuild.get("script_flow") == "staged":
            from app.tikitaka.staged import verify_table_block
            prompt += verify_table_block(draft, index, transcript, exclude)
    job.log(f"[verify] v{version_n} 초안 항목 {len(draft['items'])} → 영상 확인 패스(agentic)" + (" · 작품 이해 문서 첨부" if digest else ""))
    try:
        raw, meta = gemini.agentic_video_json(prompt, proxy, kind="verify", upload_cache=job.path("files_cache.json"), max_output_tokens=16384)
    except Exception as e:  # noqa: BLE001
        job.log(f"[verify] ⚠ 확인 패스 실패 → 초안 그대로 진행: {type(e).__name__}: {str(e)[:200]}")
        job.record_step(f"verify_v{version_n}", status="failed", error=str(e)[:300])
        _gate(rebuild)(job, gemini, draft, index, transcript, exclude)
        return draft
    from app.tikitaka.production import SCHEMA as production_schema
    job.save(f"verify_raw_v{version_n}{sfx}.json", {"meta": meta, "raw": raw,
                                                     "production_plan_schema": production_schema})
    return finalize_verified(job, gemini, rebuild, version_n, index, transcript, raw, meta, draft=draft, exclude=exclude,
                             title=title, guide=guide, tag=tag)


def finalize_from_raw(job: Job, gemini: Gemini, rebuild: dict, version_n: int, index: dict, transcript: dict, *,
                      title: str, guide: dict | None = None, extra_exclude: list[tuple[float, float]] | None = None, tag: str = "") -> dict | None:
    """캐시된 원응답(verify_raw)으로 **후처리만** 다시 돌린다 — 검증 규칙(장면 순서·중복·화자)이 바뀌었을 때 agentic 재호출 없이 재생성.
    원응답이 없으면 None."""
    sfx = f"_{tag}" if tag else ""
    name = f"verify_raw_v{version_n}{sfx}.json"
    if not job.has(name):
        return None
    doc = job.load(name)
    if index.get("grid_facts"):
        from app.tikitaka.production import SCHEMA as production_schema
        if doc.get("production_plan_schema") != production_schema:
            return None
    draft = next(v for v in rebuild["versions"] if v["n"] == version_n)
    exclude = sorted(excluded_ranges(guide, max((l["end"] for l in transcript["lines"]), default=0.0) + 3600.0) + list(extra_exclude or []))
    job.log(f"[verify] v{version_n} 원응답 캐시로 후처리 재실행(재호출 없음)")
    return finalize_verified(job, gemini, rebuild, version_n, index, transcript, doc["raw"], dict(doc.get("meta") or {}, cached=True),
                             draft=draft, exclude=exclude, title=title, guide=guide, tag=tag)


def finalize_verified(job: Job, gemini: Gemini, rebuild: dict, version_n: int, index: dict, transcript: dict, raw: dict, meta: dict, *,
                      draft: dict, exclude, title: str, guide: dict | None, tag: str = "") -> dict:
    sfx = f"_{tag}" if tag else ""
    name = f"verified_v{version_n}{sfx}.json"
    fixes = apply_speaker_fixes(job, transcript, index, raw.get("speaker_fixes"), cast=index.get("cast") or [])
    if fixes:
        job.log(f"[verify] 화자 교정 {fixes} — 영상에서 확인한 줄만(transcript.json 갱신)")
    rb = validate_versions({"versions": [dict(raw, n=version_n, strategy=raw.get("strategy") or draft["strategy"])], "recommended": version_n,
                            "reason": "verify"}, index, transcript, avoid=(guide or {}).get("avoid") or [], exclude=exclude,
                           seq_hook=rebuild.get("seq_hook", True), copy_text=(guide or {}).get("copy"))
    final = rb["versions"][0]
    if not final["items"] or sum(1 for it in final["items"] if it["type"] == "S") == 0:
        job.log("[verify] ⚠ 확인 패스 산출이 비었거나 대사가 없다 → 초안 그대로 진행")
        job.record_step(f"verify_v{version_n}", status="empty", agentic=meta)
        _gate(rebuild)(job, gemini, draft, index, transcript, exclude)
        return draft
    final.setdefault("strategy", draft["strategy"])
    if final.get("literal_flags"):
        polish_literal_actions(gemini, final, index, transcript, log=job.log)
    polish_character_names(gemini, final, index, transcript, actors=(guide or {}).get("actors") or {}, log=job.log)   # 확인 패스가 얼굴로 바꾼 인물명
    if guide:
        polish_guide(gemini, final, guide, log=job.log)
        apply_name_map(final, guide.get("actors") or {}, log=job.log)
    if rebuild.get("script_flow") == "staged":
        from app.tikitaka.staged import carry_slots, opening_record
        carry_slots(draft, final, index, transcript)
    _gate(rebuild)(job, gemini, final, index, transcript, exclude)
    if rebuild.get("script_flow") == "staged":
        final["opening"] = opening_record(final, index, transcript, job.load("grid.json"))
    final["looked_at"] = raw.get("looked_at") or []
    final["changes"] = [str(c) for c in (raw.get("changes") or [])]
    final["verified"] = True
    diff = diff_summary(draft, final)
    from app.tikitaka.production import SCHEMA as production_schema
    job.save(name, {"version": final, "draft_title": draft["title"], "diff": diff, "agentic": meta,
                    "production_plan_schema": production_schema})
    job.path(f"verified_v{version_n}{sfx}.md").write_text(verified_md(final, draft, diff, title=title), encoding="utf-8")
    job.log(f"[verify] 완료 — 항목 {diff['items']} · 계획 {diff['plan_sec']}s · 대사 유지 {diff['lines_kept']} · 추가 {diff['lines_added']} · "
            f"제거 {diff['lines_removed']} · 확인 {len(final['looked_at'])}곳 · 토큰 {meta.get('total_tokens')}")
    for c in final["changes"]:
        job.log(f"[verify]   변경: {c}")
    job.record_step(f"verify_v{version_n}", status="ok", diff=diff, looked_at=final["looked_at"], changes=final["changes"], agentic=meta)
    return final


def verified_md(final: dict, draft: dict, diff: dict, *, title: str) -> str:
    from app.tikitaka.report import versions_md
    body = versions_md({"versions": [final], "recommended": final["n"], "reason": "영상 확인 패스"}, title=title)
    head = [f"# 영상 확인 패스 — 「{title}」 v{final['n']}\n", f"초안 제목 **{draft['title']}** → 최종 **{final['title']}**  ",
            f"항목 {diff['items']} · 계획 {diff['plan_sec']}s · 대사 유지 {diff['lines_kept']} · 추가 {diff['lines_added']} · 제거 {diff['lines_removed']}\n",
            "**확인한 구간(모델 자기 보고)**"] + [f"- {x.get('start')}~{x.get('end')} — {x.get('why')}" for x in final.get("looked_at", [])] + \
           ["", "**초안 대비 변경(모델 보고)**"] + [f"- {c}" for c in final.get("changes", [])] + ["", "---", ""]
    return "\n".join(head) + body
