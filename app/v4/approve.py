"""9단계 — 승인 게이트. **나가도 되는 영상은 전부 만든다**(운영자 결정 O7).

계약 정본 `docs/v4/M1-interfaces.md` §6 · 기획 `docs/v4/v4-plan.md` §3(9)·§7.

이 단계는 LLM 을 부르지 않는다. 앞 단계들이 남긴 **사실**만 읽어 순수 함수로 판정한다
(기획서 §1 원칙 ① — 모델은 관찰하고 제안하고, 판단은 코드가 한다).

    나가도 된다 = 결함 게이트 **전부** 통과
                = 7단계 하드 통과 ∧ 6c 통과 ∧ seam_jump=False ∧ hook_weak=False ∧ 채점됨

**절대 점수가 아니다.** "몇 점 이상"이라는 임계는 이 파일에 없다(기획서 §1 원칙 ③).
소프트 점수(`ranking`)는 **파일 번호와 발행 순서**에만 쓴다 — 승인 여부를 정하지 않는다.
top-3 를 이것이 대체했다: 결함이 없는데 4위라는 이유로 안 만들 근거가 없다.

세 가지가 이 파일의 존재 이유다.

① **미채점 ≠ 0점.**
   8단계(시각 플래그)는 성공→플래그 / 실패→**미채점** / 전량 실패→`scoring_unavailable`
   의 3갈래다(기획서 §7 · `v4-pipeline-plan.md` §338). 호출이 죽은 후보는 '나쁜 후보'가
   아니라 '모르는 후보'이고, **모르는 것을 내보내지 않는다.** 그러나 '나쁘다'로 기록하지도
   않는다 — 사유 어휘를 `unscored` 로 따로 둔 이유다. 전량 빈 번역이 조용히 통과했던
   P4 사고(CLAUDE.md '실런이 잡은 결함 3건')와 같은 부류를 여기서 막는다.

② **승인 0 이면 1위를 경고와 함께 낸다**(`fallback=True`).
   무인으로 도는 노드 6대에서 **조용한 결번이 가장 나쁘다.** 이 레포에는 같은 취지의
   '최소 1편 보장'이 이미 넷 깔려 있고, 이 폴백은 그 다섯 번째다:
     · `app/pipeline.py:3446` — `score < 임계 and len(all_storyline_variants) > 0`
       → 첫 편만은 점수 미달이어도 스킵하지 않는다.
     · `app/pipeline.py:3586` — `if not all_storyline_variants and _cov_rejected:`
       "48% 가 4% 보다 낫고, 무인 노드에서 편이 통째로 죽는 것보다도 낫다".
     · `app/pipeline.py:3598` — `if not all_storyline_variants:` selected_storyline 폴백.
     · `app/v3/story.py` `fallback_highlight` — "재질의 소진 시 코드가 짓는 최소 편성".
   폴백은 조용하지 않다 — `fallback_reasons` 에 **그 편이 왜 탈락했는지**를 그대로 싣는다
   (검수가 발행 여부를 판단할 재료).

③ **v1 의 `max_shorts` 3-클램프를 물려받지 않는다.**
   v1 은 상한을 두 곳에서 1~3 으로 조인다 — `app/cli.py:632`
   (`max_shorts = min(max(getattr(args, "max_shorts", 3), 1), 3)`) · `app/pipeline.py:3269`
   (`min(payload.max_shorts, config.max_shorts_count)`) · `app/config.py`
   (`max_shorts_count: int = 3`). 승인 편이 넷 이상 나오는 v4 가 그 클램프 밑에서 돌면
   **4번째 편부터 조용히 사라진다** — O7 의 실패 모드가 정확히 이것이라 `tests/test_v4_m0.py`
   가 세 자리를 문자열로 박제해 뒀다. v4 는 자기 상한(1~8)을 자기 진입점에서 정하고,
   상한으로 자른 편수는 `capped` 에 남긴다(조용한 절단 금지).

모든 함수는 순수·결정적이다. 넘겨받은 dict/list 를 제자리에서 고치지 않는다.
"""
from __future__ import annotations

from typing import Any

# ── 상한 (기획서 §9 N7 — 기본 8) ────────────────────────────────────────────
MAX_SHORTS_DEFAULT = 8
MAX_SHORTS_LIMIT = 8
MAX_SHORTS_MIN = 1

# ── 탈락 사유 어휘 ──────────────────────────────────────────────────────────
# 이 문자열은 run_log·`checkpoint_candidates.json`·검수 카드에 **그대로** 실린다.
# 바꾸면 저장된 잡의 감사 기록과 대조가 끊긴다 — 테스트가 값으로 박제한다.
REASON_FUNNEL = "funnel_dropped"    # 7단계 하드 탈락(예고 구역·소스 밖·커버리지·길이)
REASON_VERIFY = "verify_failed"     # 6c 구간 검증 미통과(환각 인용·조각 소실)
REASON_UNSCORED = "unscored"        # 8단계 미채점 — ⚠ '나쁘다'가 아니라 '모른다'
REASON_SEAM_JUMP = "seam_jump"      # 이음새에서 인물·장소가 설명 없이 바뀐다
REASON_HOOK_WEAK = "hook_weak"      # 첫 2초 안에 사건이 없다
REASON_UNRANKED = "unranked"        # 순위가 없다 → 파일 번호를 못 준다(아래 주석)
REASON_CAPPED = "capped"            # max_shorts 상한에 잘렸다(결함 아님)

# 사유 나열 순서 — 결정성. 판정 순서와 같게 두어 둘이 갈리지 않게 한다.
REASON_ORDER: tuple[str, ...] = (
    REASON_FUNNEL, REASON_VERIFY, REASON_UNSCORED,
    REASON_SEAM_JUMP, REASON_HOOK_WEAK, REASON_UNRANKED, REASON_CAPPED,
)

# 8단계가 '채점 성공'으로 남기는 status. 실패는 {status, reason, attempts} 로 남는다
# (`v4-plan-audit.md` — "실패는 영구 캐시가 아니라 {status, reason, attempts} 로").
# status 가 없으면 성공으로 본다 — 플래그 두 개가 실제로 실려 있어야 하므로 무해하다.
FLAGS_STATUS_OK = "ok"

# 8단계가 답해야 하는 이진 플래그. 하나라도 없으면 그 후보는 미채점이다.
FLAG_KEYS: tuple[str, ...] = ("seam_jump", "hook_weak")


def clamp_max_shorts(value: int | None) -> int:
    """`--max-shorts` 정규화. None → 기본 8 · 범위 밖은 **ValueError**.

    ⚠ 조용한 클램프 금지가 이 함수의 전부다. v1 은 `min(max(v, 1), 3)` 으로 말없이
    조여서, 8을 준 사람이 3편만 받고도 그 사실을 모른다(cli.py:632). 여기서는 범위 밖이면
    죽는다 — 상한을 잘못 준 것은 설정 실수이고, 설정 실수는 시작 전에 아는 것이 낫다
    (E11 `--transcribe-backend` 의 argparse choices 규율과 같은 자리).
    """
    if value is None:
        return MAX_SHORTS_DEFAULT
    # bool 은 int 의 하위형이라 True 가 1 로 통과한다 — 넘어온 것이 플래그면 배선 실수다.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"max_shorts 는 정수여야 한다: {value!r} ({type(value).__name__}). "
            f"허용 {MAX_SHORTS_MIN}~{MAX_SHORTS_LIMIT}")
    if not (MAX_SHORTS_MIN <= value <= MAX_SHORTS_LIMIT):
        raise ValueError(
            f"max_shorts 범위 밖: {value} (허용 {MAX_SHORTS_MIN}~{MAX_SHORTS_LIMIT}). "
            "v4 는 결함 게이트를 통과한 편을 전부 낸다(O7) — 조용히 조이지 않는다.")
    return value


def scored_flags(flags: dict, cand_id: str) -> dict | None:
    """8단계 플래그가 **채점됐는가** → 채점됐으면 {seam_jump, hook_weak}, 아니면 None. 순수.

    None 은 '플래그가 False 다'가 아니라 **'모른다'** 다(기획서 §7 실패 3갈래).
    미채점으로 보는 경우 셋:
      · 그 후보의 항목 자체가 없다(호출 전 · 전량 실패)
      · `status` 가 `ok` 가 아니다(호출 실패 — {status, reason, attempts})
      · 플래그 두 개 중 하나라도 값이 없다(응답 절단 · 스키마 위반)

    ⚠ 값이 있는데 bool 이 아니면 **크게 실패한다**. `"false"`(문자열)를 False 로 읽으면
    결함 편이 조용히 발행되고, True 로 읽으면 멀쩡한 편이 조용히 죽는다 — 어느 쪽도
    추측으로 정할 값이 아니다(v3 story 의 TTS 충돌 벨트가 AssertionError 를 던지는 것과
    같은 규율: 코드 결함은 조용한 송출보다 낫다).
    """
    entry = flags.get(cand_id) if isinstance(flags, dict) else None
    if not isinstance(entry, dict):
        return None
    status = entry.get("status")
    if status is not None and status != FLAGS_STATUS_OK:
        return None
    out: dict[str, bool] = {}
    for key in FLAG_KEYS:
        val = entry.get(key)
        if val is None:
            return None
        if not isinstance(val, bool):
            raise ValueError(
                f"후보 {cand_id} 의 flags[{key!r}] 가 bool 이 아니다: {val!r} "
                f"({type(val).__name__}). 8단계는 true/false 만 답한다 — "
                "추측하면 결함 편이 조용히 나가거나 멀쩡한 편이 조용히 죽는다.")
        out[key] = val
    return out


def is_approved(cand_id: str, *, funnel_kept: set[str], verify_ok: set[str],
                flags: dict) -> tuple[bool, list[str]]:
    """나가도 되는가 → (승인, 탈락 사유 목록). 순수·결정적.

    조건은 **결함 게이트 전부 통과** 하나다 — 점수는 보지 않는다.
    사유는 첫 하나에서 멈추지 않고 **해당하는 것을 전부** 담는다(검수가 이 목록만 보고
    무엇을 고쳐야 하는지 알아야 한다 — 하나씩 알려주면 재시도를 반복하게 된다는
    `clips_beyond_source` 의 교훈과 같다).

    ⚠ `flags` 는 §8 자료 모양 그대로 **후보 id 로 키가 잡힌 지도**다
    ({"c01": {"seam_jump": false, ...}}) — 후보 하나의 항목이 아니다.
    """
    reasons: list[str] = []
    if cand_id not in funnel_kept:
        reasons.append(REASON_FUNNEL)
    if cand_id not in verify_ok:
        reasons.append(REASON_VERIFY)
    scored = scored_flags(flags, cand_id)
    if scored is None:
        # 미채점 — 플래그 둘을 판정하지 않는다. 모르는 것을 '틀렸다'고 적지 않는다.
        reasons.append(REASON_UNSCORED)
    else:
        if scored["seam_jump"]:
            reasons.append(REASON_SEAM_JUMP)
        if scored["hook_weak"]:
            reasons.append(REASON_HOOK_WEAK)
    return (not reasons, reasons)


def _ids(items: Any, *, where: str) -> list[str]:
    """후보 목록 → id 목록. 순수.

    ⚠ 계약(§6·§8)이 `kept` 의 원소 모양을 한쪽은 `[id...]`(verify), 다른 쪽은 `[...]`
    (funnel)로만 적어 놓아 **둘 다 받는다** — 문자열이면 그대로, dict 면 `id` 키.
    그 밖의 모양은 즉시 실패한다(모르는 모양을 추측으로 통과시키면 후보가 조용히 증발한다).
    """
    out: list[str] = []
    for i, item in enumerate(items or []):
        if isinstance(item, str):
            cid = item
        elif isinstance(item, dict):
            cid = item.get("id")
        else:
            raise TypeError(f"{where}[{i}] 의 모양을 모른다: {item!r} "
                            "(후보 id 문자열 또는 'id' 키를 가진 dict 여야 한다)")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"{where}[{i}] 에 후보 id 가 없다: {item!r}")
        out.append(cid)
    return out


def approve(*, funnel: dict, verify: dict, flags: dict, ranking: list[dict],
            max_shorts: int = MAX_SHORTS_DEFAULT) -> dict:
    """승인·순위 확정 → 감사 기록 dict. 순수·결정적.

    반환:
      {"approved": [id...],                 # 순위 순 — 파일 번호 1..k 가 이 순서다
       "rejected": [{"id", "reasons"}...],  # 심사 대상 전량(승인분 제외)
       "fallback": bool, "fallback_id": str|None, "fallback_reasons": [...],
       "capped": int, "max_shorts": int,
       "scoring_unavailable": bool, "considered": int}

    · 승인 편은 **전부** 낸다(top-K 아님 — O7). 순위는 파일 번호·발행 순서에만 쓴다.
    · 순서 정본은 `ranking` 이다. 순위에 없는 후보는 게이트를 다 통과했더라도 승인하지
      않고 `unranked` 로 남긴다 — 파일 번호를 줄 근거가 없는데 내보내면 `shorts_{n}.mp4`
      번호가 실행마다 흔들린다(§6 바깥 계약). 조용히 버리지는 않는다.
    · 심사 대상은 ranking ∪ funnel(kept·dropped) ∪ verify(kept·dropped) ∪ flags 키다 —
      어느 단계가 남긴 후보든 승인/탈락 어느 한쪽에는 반드시 이름이 남는다.
    · 승인 0 이면 `ranking` 1위를 `fallback=True` 로 낸다(모듈 독스트링 ②).
      폴백 편은 `rejected` 에서 빼고 그 사유를 `fallback_reasons` 로 옮긴다 — 같은 id 가
      승인·탈락 양쪽에 있으면 하류가 어느 쪽을 믿을지 정할 수 없다.
    · `ranking` 이 비어 있으면 폴백할 대상 자체가 없다 → 빈 승인을 그대로 돌려준다.
      후보가 0 인 것은 6단계의 실패이지 이 단계가 지어낼 일이 아니다(판정 근거가 없으면
      판정하지 않는다).
    """
    cap = clamp_max_shorts(max_shorts)
    funnel = funnel or {}
    verify = verify or {}
    flags = flags or {}

    funnel_kept = set(_ids(funnel.get("kept"), where="funnel['kept']"))
    verify_ok = set(_ids(verify.get("kept"), where="verify['kept']"))

    ranked_ids = _ids(ranking, where="ranking")
    seen: set[str] = set()
    for cid in ranked_ids:
        if cid in seen:
            # 같은 후보가 순위에 두 번 있으면 파일 번호가 겹친다 — 조용히 하나를 버리면
            # 어느 편이 shorts_2.mp4 인지 실행마다 달라진다.
            raise ValueError(f"ranking 에 후보 id 가 중복이다: {cid}")
        seen.add(cid)

    # 심사 대상 — 순서는 ranking 순, 그 밖은 id 정렬(결정성).
    others = set(_ids(funnel.get("dropped"), where="funnel['dropped']"))
    others |= funnel_kept
    others |= set(_ids(verify.get("dropped"), where="verify['dropped']"))
    others |= verify_ok
    for key in flags:
        # flags 는 후보 id 로 키가 잡힌 지도다(§8). 키가 문자열이 아니면 8단계 배선 실수라
        # 조용히 건너뛰지 않는다 — 건너뛰면 그 후보가 감사 기록에서 통째로 증발한다.
        if not isinstance(key, str):
            raise ValueError(f"flags 의 키는 후보 id 문자열이어야 한다: {key!r}")
        others.add(key)
    extra_ids = sorted(others - seen)
    considered = ranked_ids + extra_ids

    reasons_of: dict[str, list[str]] = {}
    approved_ids: list[str] = []
    for cid in considered:
        ok, reasons = is_approved(cid, funnel_kept=funnel_kept,
                                  verify_ok=verify_ok, flags=flags)
        if cid not in seen:
            # 순위가 없으면 승인하지 않는다(위 독스트링) — 게이트 통과 여부와 무관하게 붙인다.
            reasons = reasons + [REASON_UNRANKED]
            ok = False
        reasons_of[cid] = reasons
        if ok:
            approved_ids.append(cid)

    # 상한 — 자른 편수를 반드시 남긴다(조용한 절단 금지). v1 의 3-클램프는 물려받지 않는다.
    capped = 0
    if len(approved_ids) > cap:
        for cid in approved_ids[cap:]:
            reasons_of[cid] = reasons_of[cid] + [REASON_CAPPED]
        capped = len(approved_ids) - cap
        approved_ids = approved_ids[:cap]

    # 8단계 전량 실패 — 순위에 오른 후보 중 채점된 것이 하나도 없다(기획서 §7 3갈래의 셋째).
    # 결정적 신호(6c·7)만으로 순위가 정해진 상태라는 뜻이라 검수가 알아야 한다.
    scoring_unavailable = bool(ranked_ids) and not any(
        scored_flags(flags, cid) is not None for cid in ranked_ids)

    fallback = False
    fallback_id: str | None = None
    fallback_reasons: list[str] = []
    if not approved_ids and ranked_ids:
        fallback_id = ranked_ids[0]
        fallback_reasons = list(reasons_of.get(fallback_id, []))
        approved_ids = [fallback_id]
        fallback = True

    approved_set = set(approved_ids)
    rejected = [{"id": cid, "reasons": list(reasons_of[cid])}
                for cid in considered if cid not in approved_set]

    return {
        "approved": approved_ids,
        "rejected": rejected,
        "fallback": fallback,
        "fallback_id": fallback_id,
        "fallback_reasons": fallback_reasons,
        "capped": capped,
        "max_shorts": cap,
        "scoring_unavailable": scoring_unavailable,
        "considered": len(considered),
    }
