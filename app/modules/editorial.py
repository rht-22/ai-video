"""작품별 편집 지침(editorial) — 권리사 콘텐츠 가이드·실행 단위 요청을 프롬프트에 주입 (2026-08-20).

왜 필요한가: "경연 결과 노출 불가"(가왕쇼) 같은 **내용** 가이드는 지금까지 선정 AI 에
전달되지 않고 검수함 사후 반려로만 걸렸다 — 위반 장면이 렌더까지 간 뒤에야 잡혀
렌더 비용과 회차 슬롯이 헛돈다. 이 모듈이 그 지침을 구조화해 프롬프트에 먹인다.
프롬프트 반영은 **1차 필터**일 뿐이고 검수함의 사람 최종 확인은 그대로 유지된다.

계약: `--editorial-json '<json>'` 로 받는다.

    {
      "avoid":  ["경연 결과 노출 — 순위 발표·우승·탈락 장면과 그에 대한 언급"],
      "prefer": ["무대 하이라이트 — 가창 절정부, 객석·심사위원 리액션"],
      "tone":   "차분한 다큐 톤"        # 선택 — title·tts_cues 문체
    }

세 종류를 나누는 이유 — 틀렸을 때의 비용이 달라 강제 수준이 달라야 한다:
  · avoid  = 위반하면 권리 사고. 하드 필터(과잉 차단이 미달 차단보다 낫다).
  · prefer = 방향. 절대 규칙로 쓰면 소재 부족 회차에서 후보가 마르거나 LLM 이
             억지로 끼워 맞춘다(관찰 비약) — 랭킹 편향으로만 적용.
  · tone   = 표현. 선정과 무관, title·tts_cues 생성에만.

같은 필드도 단계마다 다른 동사로 렌더링된다(use_case):
  · analysis(청크 분석) — **절대 좁히지 않는다.** 청크는 후보 선정이자 영상의
    텍스트화 단계라, 여기서 잘린 정보는 하류에서 복구 불가다. avoid 는 후보에
    guideline_flags 태깅만 시키고, prefer 는 해당 구간 기술을 두껍게 시킨다.
  · story(스토리 구성 = 선정·제목·tts_cues 가 한 프롬프트) — avoid 는 하드 필터
    (장면 사용 금지 + **문구 언급도 금지** — 장면이 깨끗해도 제목이 결과를
    스포하면 똑같이 위반이다), prefer 는 랭킹 편향, tone 은 문체.

⚠️ avoid 를 채울 때는 원문을 그대로 옮기지 말 것 — 기계 가드로 이미 충족되는
   항목은 뺀다. 예: 가왕쇼 '무대 풀버전 금지'는 쇼츠 40~60초 하드캡이 구조적으로
   막는데, avoid 에 넣으면 LLM 이 금지를 과잉 일반화해("무대"라는 표면이 겹친다)
   무대 장면 전체를 회피할 위험만 생긴다. 그래서 avoid 항목은 ❌/✅ 쌍으로
   렌더링해 무엇이 금지가 **아닌지**를 함께 명시한다.

빈 지침이면 모든 함수가 빈 값을 돌려준다 — 지침 없는 실행은 프롬프트가 종전과
한 글자도 다르지 않다(reject_note 와 같은 규약).
"""
from __future__ import annotations

import json
from typing import Any

# 모르는 키는 즉시 실패 — 조용히 무시하면 오타 난 지침이 "적용되고 있다"는 착각을
# 낳는다(edit_overrides 의 제1원칙과 동일). '_' 프리픽스는 문서용 키라 허용·무시.
_ALLOWED_KEYS = frozenset({"avoid", "prefer", "tone"})


def parse_editorial(raw: str | None) -> dict[str, Any] | None:
    """JSON 문자열 → 검증된 editorial dict. 빈 입력·빈 지침은 None."""
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"editorial JSON 파싱 실패: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"editorial 은 객체여야 합니다: {type(data).__name__}")
    unknown = {k for k in data if not k.startswith("_")} - _ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"editorial 에 알 수 없는 키 {sorted(unknown)} — 허용: {sorted(_ALLOWED_KEYS)}")
    out: dict[str, Any] = {}
    for key in ("avoid", "prefer"):
        items = data.get(key)
        if items is None:
            continue
        if not isinstance(items, list) or not all(isinstance(s, str) for s in items):
            raise ValueError(f"editorial.{key} 는 문자열 배열이어야 합니다: {items!r}")
        cleaned = [s.strip() for s in items if s.strip()]
        if cleaned:
            out[key] = cleaned
    tone = data.get("tone")
    if tone is not None:
        if not isinstance(tone, str):
            raise ValueError(f"editorial.tone 은 문자열이어야 합니다: {tone!r}")
        if tone.strip():
            out["tone"] = tone.strip()
    return out or None


def merge_editorial(base: dict[str, Any] | None,
                    run: dict[str, Any] | None) -> dict[str, Any] | None:
    """상시 지침(작품 카드) ⊕ 실행 단위 지시(1회성).

    prefer 는 추가, tone 은 실행분이 교체. **avoid 는 합집합만** — 권리 제약에
    "이번만 예외"는 없으므로 실행 단위 지시로 완화할 수 없다.
    """
    if not base:
        return run or None
    if not run:
        return base
    out: dict[str, Any] = {}
    avoid = list(base.get("avoid") or [])
    avoid += [s for s in (run.get("avoid") or []) if s not in avoid]
    if avoid:
        out["avoid"] = avoid
    prefer = list(base.get("prefer") or [])
    prefer += [s for s in (run.get("prefer") or []) if s not in prefer]
    if prefer:
        out["prefer"] = prefer
    tone = run.get("tone") or base.get("tone")
    if tone:
        out["tone"] = tone
    return out or None


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {s}" for s in items)


def format_editorial_block(editorial: dict[str, Any] | None, use_case: str) -> str:
    """editorial → 프롬프트 뒤에 붙일 블록. use_case: 'analysis' | 'story'."""
    if not editorial:
        return ""
    avoid = editorial.get("avoid") or []
    prefer = editorial.get("prefer") or []
    tone = editorial.get("tone")

    if use_case == "analysis":
        if not avoid and not prefer:
            return ""  # tone 만 있는 지침 — 청크 분석과 무관하니 프롬프트를 건드리지 않는다
        parts = ["\n\n[작품별 편집 지침 — 태깅·기술 지침 (권리사 가이드/운영 지시)]"]
        parts.append(
            "이 지침은 후보를 **줄이라는 뜻이 아니다.** 후보 선정 개수·범위와 segments "
            "기술은 종전과 완전히 동일하게 하라 — 여기서 좁히면 다음 단계가 쓸 정보가 사라진다.")
        if avoid:
            parts.append(
                "\n1. 금지 요소 태깅 (판단 재료 생성): 각 candidate 에 `guideline_flags` 배열 "
                "필드를 **추가로** 출력하라. 아래 금지 요소가 그 후보 구간에 실제로 보이거나 "
                "들리면 해당 항목 문자열을 배열에 넣고, 없으면 빈 배열 [] 을 출력한다.\n"
                + _bullets(avoid) + "\n"
                "⚠️ 금지 요소가 있어도 그 후보를 **제외하지 말라** — 태깅만 하라. 제외 판단은 "
                "다음 단계가 한다. description·segments 도 금지 요소 여부와 무관하게 온전히 "
                "기술하라(텍스트화가 이 단계의 임무다). 애매하면 태깅하라(과잉 태깅이 누락보다 낫다).")
        if prefer:
            parts.append(
                "\n2. 우선 소재 상세 기술: 아래 소재가 나오는 구간은 description 을 더 상세히 "
                "(인물·행동·감정의 시각 단서까지) 기술하고, 해당 candidate 의 reason 에 그 소재를 "
                "명시하라.\n"
                + _bullets(prefer) + "\n"
                "⚠️ 이 소재**만** 고르라는 뜻이 아니다 — 위 소재가 없는 구간의 후보도 종전과 "
                "동일하게 뽑아라. 소재가 안 보이는데 억지로 갖다 붙이지 말라([원칙 P1·P2] 그대로 적용).")
        return "\n".join(parts) + "\n"

    if use_case == "story":
        parts = ["\n\n[작품별 편집 지침 — 권리사 가이드/운영 지시]"]
        if avoid:
            parts.append(
                "\n금지 요소 — 절대 규칙 (아래 우선 소재·다른 모든 지침보다 우선한다):\n"
                + _bullets(avoid) + "\n"
                "- ❌ 위 요소가 담긴 candidate 사용 금지: `guideline_flags` 가 비어 있지 않은 "
                "candidate 는 스토리라인에 쓰지 말라. 태깅이 누락됐더라도 description·transcript "
                "에서 위 요소가 확인되면 마찬가지로 쓰지 말라.\n"
                "- ❌ **문구도 금지**: title·topic·tts_cues 등 모든 출력 텍스트에서 위 요소를 "
                "언급·암시하지 말라 — 장면에 없어도 제목이 결과를 스포하면 똑같은 위반이다.\n"
                "- ✅ 위 요소가 **없는** 장면·소재는 종전과 완전히 동일하게 자유롭게 쓴다 — "
                "이 금지를 주변 소재로 확대 적용하지 말라.")
        if prefer:
            parts.append(
                "\n우선 소재 — 랭킹 편향 (절대 규칙 아님):\n"
                + _bullets(prefer) + "\n"
                "- 조건이 비슷한 후보끼리는 위 소재가 담긴 쪽을 우선 선택하고, 스토리라인의 "
                "topic·구성도 이 방향을 우선하라. title·tts_cues 도 이 소재를 부각하라.\n"
                "- 단, 위 소재의 후보가 부족하면 **억지로 끼워 맞추지 말고** 다른 후보로 정상 "
                "구성하라 — 소재 없는 회차에서 이 지침 때문에 품질을 떨어뜨리는 것이 더 나쁘다.")
        if tone:
            parts.append(
                "\n문구 톤 지시 — title·tts_cues 문체에만 적용 (장면 선택 기준이 아니다):\n"
                f"- {tone}")
        return "\n".join(parts) + "\n"

    raise ValueError(f"알 수 없는 use_case: {use_case!r}")


def filter_flagged_candidates(
    all_candidates: list[dict[str, Any]],
    editorial: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """avoid 태깅(guideline_flags)이 붙은 후보를 기계적으로 제외. (kept, dropped).

    프롬프트 지시(스토리 단계의 사용 금지)만으로는 LLM 이 놓칠 수 있어, 코드에서
    결정적으로 거른다. editorial 에 avoid 가 없으면 태깅 자체가 요청되지 않았으므로
    전부 통과 — 옛 체크포인트(guideline_flags 없는 분석)도 자연히 전부 통과한다.
    """
    if not editorial or not editorial.get("avoid"):
        return list(all_candidates), []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for cand in all_candidates:
        flags = cand.get("guideline_flags")
        if isinstance(flags, list) and any(str(f).strip() for f in flags):
            dropped.append(cand)
        else:
            kept.append(cand)
    return kept, dropped


def filter_log_entry(kept: list[dict[str, Any]],
                     dropped: list[dict[str, Any]]) -> dict[str, Any]:
    """필터 결과 → run_log steps 항목. 순수 — 테스트 대상.

    대시보드(검수함)가 이걸 읽어 "필터 작동: 제외 N건"을 보여주고, dropped 의 원본
    절대초 좌표로 편집실 타임라인에 제외 구간을 마킹할 수 있다. description 을 넣는
    이유: 검수자가 run_log 만 보고 "왜 걸렸는지"를 판단해야 오태깅(무대 장면인데 결과
    노출로 태깅)을 발견할 수 있다. dropped 0건이어도 기록한다 — "필터가 돌았는데 걸린
    게 없다"와 "필터가 안 돌았다"는 다른 사실이다.
    """
    return {
        "step": "editorial_filter",
        "kept": len(kept),
        "dropped": [
            {
                "chunk_index": c.get("chunk_index"),
                "start_sec": c.get("start_sec"),
                "end_sec": c.get("end_sec"),
                "guideline_flags": c.get("guideline_flags"),
                "description": c.get("description"),
            }
            for c in dropped
        ],
    }
