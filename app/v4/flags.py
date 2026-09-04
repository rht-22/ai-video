"""8단계 — 시각 사고 플래그. **후보를 렌더하지 않고** 보여주고, 이진 판정만 받는다.

계약 정본 `docs/v4/M3-interfaces.md` §4 · 기획 `docs/v4/v4-plan.md` §3(8)·§7·§8 ·
`docs/v4/v4-pipeline-plan.md` "8단계 프롬프트 계약 — M9를 지키는 방식".

이 단계가 **M9 원칙의 시험대**다. 원칙은 v3 `story.py:488` 이 한 줄로 적어 뒀다:

    LLM 심사를 쓰지 않는다 — 검증자와 피검증자가 편향을 공유하면 안 된다(M9 원칙)

그래서 여기서 모델이 하는 일은 **관찰**뿐이다. 화면에 사고가 있는가/없는가 —
true/false 와 근거 시각. 감점 환산·가중치·순위·승인은 전부 코드가 한다
(`funnel.py`·`approve.py`). 모델이 점수를 답하면 **반려**하는 이유가 이것이다:
점수를 받기 시작하면 "몇 점 이상"이라는 임계가 어딘가에 생기고, 그 임계는 모델의
취향을 그대로 승인 규칙으로 승격시킨다.

프롬프트 계약은 `finalize.py:636` 의 `QC_PROMPT` 와 **같은 문장**을 쓴다
("화면 사고만 찾아라 — 취향 평가 금지"). 같은 계약을 두 번 새로 쓰지 않는다.

## 이 파일이 하는 네 가지

① **offset 멀티파트로 짜집기를 보여준다 — 렌더하지 않는다**(기획서 §2-B).
   후보의 조각들을 순서대로 같은 핸들에 붙이면 모델이 이어 붙인 영상을 본다
   (첨부 순서 = 편집 순서, 실측). 렌더 방식이었다면 후보 8개 × (렌더 + 업로드 왕복)
   = 벽시계 +11분이다. 배선은 `app/v4/video.call_video` 한 곳이 진다.

   🛑 **이음새 시각은 편집본 좌표다** — 조각 길이의 누적합. 모델이 보는 것이 이어 붙인
   영상이므로 원본 절대초를 주면 모델은 화면에 없는 시각을 가리키게 된다.

② **병렬**(동시 `FLAG_CONCURRENCY`). 실측(par_probe, 48콜): 순차 78~80s → 동시 4
   22~25s → 동시 8 12s, 콜당 토큰 동일, 429/503 0건. 예산 카운터는 **Lock 안
   check-and-increment** 다 — `app/v3/refine.py:359` 식 단순 int(`if used >= B: raise;
   used += 1`)는 두 스레드가 같은 값을 읽고 둘 다 통과해 **샌다**.

③ **실패는 미채점이다 — 0점이 아니다.** `{status: "failed", reason: …}` 로 남기고
   9단계 `approve.scored_flags` 가 그 어휘(`FLAGS_STATUS_OK`)를 본다. 호출이 죽은
   후보는 '나쁜 후보'가 아니라 '모르는 후보'다. 채점 실패를 0점으로 읽으면 전량 빈
   번역이 조용히 통과한 P4 사고와 같은 부류가 된다.
   ⚠ 그래서 상태 어휘는 여기서 새로 짓지 않고 `approve` 에서 **import** 한다.

④ **파트 상한**. 요청당 영상 파트는 문서 상한 10 · 실측 25 통과(미문서 동작)로
   불일치다(§10). 문서 상한을 지키되, 넘치면 **소스에서 연속인 조각을 병합**하고
   그래도 넘치면 그 후보는 미채점 + 사유다(§5 M12). 조용히 자르지 않는다 —
   앞 세 조각만 보여주고 "이음새 없음"을 받으면 그것은 거짓 통과다.

## 실호출로만 알 수 있는 것 (이 워크트리에 `GEMINI_API_KEY` 가 없다)

· 서버가 같은 file_uri 의 파트 여럿을 **첨부 순서대로 이어 붙여 보는가** — 근거는
  기획서 §2-B 의 REST 실측이고 SDK 경로 실호출은 미검증이다(`video.py` 독스트링).
· 프롬프트 품질 — 모델이 `seam_jump`/`hook_weak` 를 사람과 같게 판정하는가. §2-E
  재검증(160콜)이 아직 안 돌았고, 1·2차 실측은 이음새 κ 0.44 · hook_weak 12/12
  false(분산 0)였다. **판정 품질은 미확인**이고 여기 테스트는 배선·계약만 고정한다.
· `FLAG_MAX_OUTPUT_TOKENS = 4096` 이 충분한가 — 20편 기록의 p99×2 로 재설정하기로
  돼 있다(기획서 §9-A). 절단이 실제로 나면 `call_video` 가 MAX_TOKENS 를 크게 남기고,
  다음 손잡이는 `thinking_level` 하향이다(v3-M2 판례 — thinking 이 출력 예산을 먹었다).
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

from app.v4 import fps as fps_mod
from app.v4.approve import FLAG_KEYS, FLAGS_STATUS_OK
from app.v4.video import (
    Clip,
    VideoCallError,
    VideoParseError,
    call_video,
    clips_within_source,
)

# ── 호출 설정 ───────────────────────────────────────────────────────────────

# 기획서 §3 — 후보는 60초라 예산이 넉넉하다(후보당 ≈21,300 토큰 · 8후보 170,400).
FLAG_SAMPLE_FPS = 5.0

# 검수 권고 초기값(기획서 §9-A). ⚠ **캡을 지금 확정하지 않는다** — 20편 기록의 p99×2 로
# 재설정하기로 한 값이다. 이진 두 개 + 근거 시각뿐이라 출력 자체는 수백 토큰이지만,
# thinking 이 같은 예산을 나눠 쓴다(v3-M2 에서 실제로 절단이 났다).
FLAG_MAX_OUTPUT_TOKENS = 4096

# 동시 4 로 시작한다. AI Studio 에서 프로젝트 실제 TPM/RPM 을 확인한 뒤 8 로 올린다 —
# 단일 노드 실측만 있고 노드 6대 동시는 미검증이다(검수 생존 1 ⑤).
FLAG_CONCURRENCY = 4

# 요청당 영상 파트 상한. **문서 10 vs 실측 25 통과**로 불일치인데(§10) 미문서 동작에
# 기대지 않는다 — 문서 상한을 코드 상한으로 두고, 넘치면 병합·미채점으로 간다(§5 M12).
# ⚠ 6단계 `candidates.SEGMENTS_MAX`(=8, "8단계 offset 파트 수와 한 몸")와 **다른 자**다:
# 저쪽은 후보의 모양을 정하고(6단계 `validate_response` 가 이미 강제한다) 이쪽은 한 요청에
# 붙일 수 있는 파트 수를 정한다. 그래서 값을 가져다 쓰지 않고 각자 자기 근거로 선다 —
# 상류가 8 로 조여도 편집실·재개·구 체크포인트에서 온 후보는 그 검증을 안 지나므로
# 첨부 직전의 벨트가 따로 필요하다(8 ≤ 10 이라 정상 경로에서는 발동하지 않는다).
PART_LIMIT = 10

# 조각 둘이 소스에서 '연속'인가의 관용. **부동소수 관용이지 '가까우니 붙이자'가 아니다** —
# 0.5초 틈은 실제 컷이고 그 자리에는 진짜 이음새가 있다.
CONTIGUOUS_TOLERANCE_SEC = 0.05

# hook_weak 의 창. 기획서 §3-8 · `funnel.LEAD_IN_FREE_SEC` 와 같은 자(첫 2초 안에 사건).
HOOK_WINDOW_SEC = 2.0

# 모델이 답해도 되는 열쇠. **이 밖은 반려**한다(아래 `validate_flags_response` 주석).
EVIDENCE_KEY = "evidence_sec"
ALLOWED_RESPONSE_KEYS: frozenset[str] = frozenset(FLAG_KEYS) | {EVIDENCE_KEY}

# ── 미채점 사유 어휘 ────────────────────────────────────────────────────────
# 이 문자열은 `checkpoint_candidates.json` 의 `flags` 절과 run_log 에 **그대로** 실린다.
# 바꾸면 저장된 잡의 감사 기록과 대조가 끊긴다 — 테스트가 값으로 박제한다.
REASON_NO_CLIPS = "no_clips"            # 경계 벨트를 지나고 나니 붙일 조각이 없다
REASON_PART_LIMIT = "part_limit"        # 병합해도 파트 상한을 못 지킨다(§5 M12)
REASON_BUDGET = "budget_exhausted"      # 예산 소진 — 탈락이 아니라 '모른다'
REASON_CALL_FAILED = "call_failed"      # 호출 실패(E11 분류는 call_video 안에서 끝난다)
REASON_PARSE_FAILED = "parse_failed"    # 응답이 JSON 이 아니다
REASON_INVALID = "invalid_response"     # JSON 은 맞는데 계약 위반(점수·모르는 열쇠 등)

__all__ = [
    "FLAG_SAMPLE_FPS", "FLAG_MAX_OUTPUT_TOKENS", "FLAG_CONCURRENCY", "FLAG_KEYS",
    "FLAGS_STATUS_OK", "PART_LIMIT", "CONTIGUOUS_TOLERANCE_SEC", "HOOK_WINDOW_SEC",
    "EVIDENCE_KEY", "ALLOWED_RESPONSE_KEYS", "FLAGS_PROMPT",
    "REASON_NO_CLIPS", "REASON_PART_LIMIT", "REASON_BUDGET",
    "REASON_CALL_FAILED", "REASON_PARSE_FAILED", "REASON_INVALID",
    "TokenBudget", "candidate_clips", "merge_contiguous_clips", "edited_seam_times",
    "plan_clips", "build_flags_prompt", "validate_flags_response", "run_flags",
]


# ── 조각 → 파트 ─────────────────────────────────────────────────────────────

def _seg_time(seg: Any, key: str, *, where: str) -> float:
    """조각에서 시각 하나를 읽는다. 모양이 다르면 **크게 실패**한다.

    ⚠ `start`/`end` 별칭을 받지 않는다. `funnel._segments_of` 는 별칭을 받는데 그쪽은
    점수 계산이라 0.0 으로 떨어져도 감점에 그친다. 여기서 0.0 으로 떨어지면 **모델에게
    엉뚱한 구간을 보여주고** 그 판정을 승인 게이트가 그대로 믿는다. 자료 모양의 정본은
    `M1-interfaces.md` §8 의 `{start_sec, end_sec}` 이고, 그 밖의 모양은 배선 오류다.
    """
    value = seg.get(key) if isinstance(seg, dict) else getattr(seg, key, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{where}: 조각의 {key} 가 숫자가 아니다: {value!r} — "
            f"자료 모양 정본은 {{start_sec, end_sec}} 다(M1 §8). "
            f"별칭(start/end)을 추측으로 받지 않는다")
    return float(value)


def candidate_clips(cand: Any) -> list[Clip]:
    """후보 → 첨부 순서 그대로의 `Clip` 목록. 순수.

    🛑 **정렬하지 않는다.** 첨부 순서가 곧 편집 순서이므로(기획서 §2-B) 여기서 시각순
    정렬을 끼우면 후보가 의도한 편집 순서를 우리가 바꿔 버린다. 후보가 시각을 거슬러
    편성했다면 그것은 후보의 편성이고, 판정 대상도 그 편성이다.
    """
    cid = candidate_id(cand)
    raw = cand.get("segments") if isinstance(cand, dict) else getattr(cand, "segments", None)
    out: list[Clip] = []
    for i, seg in enumerate(raw or []):
        where = f"후보 {cid} 의 조각 {i}"
        start = _seg_time(seg, "start_sec", where=where)
        end = _seg_time(seg, "end_sec", where=where)
        out.append(Clip(start_sec=start, end_sec=end))
    return out


def candidate_id(cand: Any) -> str:
    """후보 id. 없으면 **크게 실패**한다 — id 는 6단계가 `c%02d` 로 부여한다(M1 §8).

    id 를 우리가 지어 주면 그 후보의 플래그가 `verify`·`funnel`·`approve` 의 어떤
    항목과도 이어지지 않는다(조용히 미채점이 된다)."""
    cid = cand.get("id") if isinstance(cand, dict) else getattr(cand, "id", None)
    if not isinstance(cid, str) or not cid.strip():
        raise ValueError(
            f"후보에 id 가 없다: {cand!r} — id 는 6단계가 부여하고(c%02d) "
            f"verify·funnel·approve 가 같은 열쇠로 후보를 잇는다")
    return cid.strip()


def merge_contiguous_clips(
    clips: Sequence[Clip], *, tolerance: float = CONTIGUOUS_TOLERANCE_SEC,
) -> tuple[list[Clip], list[dict]]:
    """소스에서 **연속인 이웃 파트**를 하나로 → (병합된 목록, 기록). 순수·결정적.

    두 가지를 한꺼번에 한다.

    ① **파트 상한 대비**(§5 M12) — 연속 조각을 합치면 모델이 보는 화면은 **한 프레임도
       달라지지 않으면서** 파트 수만 준다. 안전한 유일한 병합이라 이것만 한다(불연속
       조각을 합치면 사이 구간이 화면에 끼어들어 다른 영상이 된다).

    ② **거짓 이음새 제거** — `[10,20]`·`[20,30]` 은 이어 붙여도 컷이 아니다. 그 자리를
       이음새라고 알려 주면 모델은 있지도 않은 튐을 찾게 되고, `seam_jump=true` 하나가
       승인 게이트에서 그 후보를 통째로 떨어뜨린다(approve). 그래서 병합은 **상한에
       걸릴 때만이 아니라 늘** 한다.

    ⚠ 병합은 총 길이와 남은 이음새 시각을 바꾸지 않는다 — 누적합에서 그 경계 하나가
    빠질 뿐이다(테스트가 이 성질을 고정한다).
    """
    merged: list[Clip] = []
    records: list[dict] = []
    for clip in clips:
        if merged and not clip.whole and not merged[-1].whole:
            prev = merged[-1]
            if abs(float(clip.start_sec or 0.0) - float(prev.end_sec or 0.0)) <= tolerance:
                merged[-1] = Clip(start_sec=prev.start_sec, end_sec=clip.end_sec)
                records.append({
                    "action": "merged",
                    "reason": "소스에서 연속 — 이어 붙여도 컷이 아니다",
                    "left": [float(prev.start_sec or 0.0), float(prev.end_sec or 0.0)],
                    "right": [float(clip.start_sec or 0.0), float(clip.end_sec or 0.0)],
                })
                continue
        merged.append(clip)
    return merged, records


def edited_seam_times(clips: Sequence[Clip]) -> list[float]:
    """파트 목록 → **편집본 좌표**의 이음새 시각. 순수.

    이음새 = 파트 사이의 컷이다. 파트 n 개면 이음새는 n−1 개이고, i 번째 이음새는
    앞선 파트들의 **길이 누적합**이다. 모델이 보는 것이 이어 붙인 영상이므로 이 좌표계로
    말해야 한다 — 원본 절대초를 주면 모델은 화면에 없는 시각을 가리킨다.

    ⚠ 전체 첨부(`Clip()`)가 섞이면 길이를 모르므로 **판정하지 않고 크게 실패**한다.
    8단계에 오는 후보는 조각을 가진다(전체를 보여줄 이유가 없다)."""
    seams: list[float] = []
    acc = 0.0
    for i, clip in enumerate(clips):
        if clip.whole:
            raise ValueError(
                f"파트 {i} 가 전체 첨부다 — 길이를 모르면 이음새 좌표를 못 만든다. "
                f"8단계는 조각을 가진 후보만 본다")
        acc += float(clip.end_sec or 0.0) - float(clip.start_sec or 0.0)
        if i < len(clips) - 1:
            seams.append(round(acc, 3))
    return seams


def edited_total_sec(clips: Sequence[Clip]) -> float:
    """파트 목록 → 이어 붙인 영상의 총 길이. 순수."""
    total = 0.0
    for clip in clips:
        if clip.whole:
            raise ValueError("전체 첨부가 섞이면 총 길이를 모른다")
        total += float(clip.end_sec or 0.0) - float(clip.start_sec or 0.0)
    return round(total, 3)


def plan_clips(cand: Any, *, source_duration_sec: float,
               part_limit: int = PART_LIMIT) -> tuple[list[Clip], dict]:
    """후보 → (실제로 첨부할 파트, 기록). 순수·결정적. **호출 전에 전부 끝난다.**

    순서가 계약이다:
      ① 경계 벨트(`video.clips_within_source`) — `endOffset` 은 소스를 넘어도 오류 없이
         **조용히 클램프**된다(기획서 §7). 보내고 나면 모델이 무엇을 봤는지 알 수 없다.
      ② 연속 조각 병합 — 화면을 안 바꾸면서 파트 수를 줄이고 거짓 이음새를 없앤다.
      ③ 파트 상한 확인 — 못 지키면 **미채점**이다. 앞 몇 개만 보내고 '이음새 없음'을
         받는 것이 가장 나쁘다(거짓 통과).
      ④ 이음새·총 길이는 **①②를 지난 목록**으로 잰다 — 후보의 원래 조각으로 재면
         드롭·병합된 뒤의 화면과 좌표가 어긋난다.

    기록(`note`)은 전량 감사에 실린다 — 무엇을 왜 버리고 합쳤는지(규율 3).
    `note["problem"]` 이 있으면 그 후보는 미채점이고 값은 `(사유 코드, 상세)` 다.
    """
    raw = candidate_clips(cand)
    note: dict[str, Any] = {"of": len(raw)}
    if not raw:
        note["problem"] = (REASON_NO_CLIPS, "후보에 조각이 없다")
        note["parts"] = 0
        return [], note

    kept, belt = clips_within_source(raw, source_duration_sec)
    if belt:
        note["belt"] = belt
    if not kept:
        note["problem"] = (REASON_NO_CLIPS,
                           f"경계 벨트가 조각 {len(raw)}개를 전부 버렸다(소스 {source_duration_sec}s)")
        note["parts"] = 0
        return [], note

    merged, merge_records = merge_contiguous_clips(kept)
    if merge_records:
        note["merged"] = merge_records

    note["parts"] = len(merged)
    if len(merged) > part_limit:
        note["problem"] = (
            REASON_PART_LIMIT,
            f"파트 {len(merged)}개 > 상한 {part_limit} — 연속 조각을 병합해도 못 줄였다. "
            f"앞부분만 보내면 '이음새 없음'이 거짓으로 나온다(§5 M12)")
        return merged, note

    note["seam_sec"] = edited_seam_times(merged)
    note["duration_sec"] = edited_total_sec(merged)
    return merged, note


# ── 프롬프트 ────────────────────────────────────────────────────────────────

# `finalize.py:636` QC_PROMPT 와 **같은 계약**이다("화면 사고만 찾아라 — 취향 평가 금지").
# 문장을 바꾸면 판정이 통째로 흔들리므로 상수 하나로 두고, 지문(`sha1`)은 이 문자열을 센다.
#
# ⚠ **점수 금지를 세 번 말한다**(머리 · 항목 · 출력 규칙). M9 원칙의 시험대라 한 번만
# 말하면 모델이 `confidence`·`severity` 를 얹어 오고, 그 순간 승인 규칙이 모델의 취향을
# 임계로 승격시킨다. 검증기도 같은 것을 반려하지만 **프롬프트만 고치고 검증기를 안 고치면
# 매 편 반려당하고, 검증기만 고치면 모델은 계속 같은 응답을 낸다**(E17-1 판례).
FLAGS_PROMPT = """첨부한 영상은 쇼츠 후보의 조각들을 **편집 순서 그대로 이어 붙인 것**이다(총 {total_sec:.1f}초).
파트 경계가 곧 편집 컷이다.

화면 사고만 찾아라 — 취향 평가 금지. 점수를 매기지 마라.
아래 항목에 true/false 와 근거 시각(초)만 답하라.
  · seam_jump : 조각 이음새에서 인물·장소가 설명 없이 바뀌는가
  · hook_weak : 첫 {hook_sec:g}초 안에 사건(대사·동작·리액션)이 없는가

이음새 시각: {seams}
⚠ 이 시각과 네가 답할 근거 시각은 전부 **이어 붙인 영상의 처음(0초)부터 잰 초**다 —
원본 방송 시각이 아니다.

출력(JSON만): {{"seam_jump": true, "hook_weak": false, "evidence_sec": [12.5]}}
  · 세 열쇠만 답하라. 점수·정도·확신도·설명 열쇠를 넣으면 반려된다.
  · 근거 시각이 없으면 빈 배열."""


def _format_seams(seam_times: Sequence[float]) -> str:
    if not seam_times:
        return "없음 — 조각이 하나라 이음새가 없다"
    return ", ".join(f"{float(t):.1f}s" for t in seam_times)


def build_flags_prompt(cand: Any, *, seam_times: Sequence[float],
                       total_sec: float | None = None) -> str:
    """8단계 프롬프트. 순수·결정적.

    ⚠ 후보의 **제목 가안·사유는 싣지 않는다.** 실을 자리가 없어서가 아니라, 사람 말로 된
    의도를 주면 모델이 화면 대신 그 의도를 채점하기 시작하기 때문이다("취향 평가 금지"가
    프롬프트에만 있고 재료에는 없으면 소용이 없다). 이 호출이 보는 것은 화면뿐이다.

    ⚠ `total_sec` 은 **실제로 첨부한 파트**의 길이 합이다(계약 시그니처에 없는 선택 인자).
    경계 벨트·병합을 지난 뒤 총 길이가 달라질 수 있는데, 후보의 원래 조각으로 재면
    프롬프트가 화면에 없는 길이를 말하게 된다. 안 주면 후보 조각으로 계산한다.
    """
    if total_sec is None:
        total_sec = edited_total_sec(candidate_clips(cand))
    return FLAGS_PROMPT.format(total_sec=float(total_sec), hook_sec=HOOK_WINDOW_SEC,
                               seams=_format_seams(seam_times))


# ── 응답 검증 ───────────────────────────────────────────────────────────────

def validate_flags_response(resp: Any) -> tuple[dict | None, list[str]]:
    """→ ({seam_jump, hook_weak, evidence_sec} | None, 반려 사유). 순수·결정적.

    🛑 **점수·정도를 답하면 반려한다.** 계약은 불리언이다(M9 원칙 — 위 모듈 독스트링).
    구체적으로 셋을 막는다:
      · 플래그 값이 bool 이 아닌 것(`0.8`·`"true"`·`1`) — `"false"` 를 False 로 읽으면
        결함 편이 조용히 나가고 True 로 읽으면 멀쩡한 편이 조용히 죽는다. 어느 쪽도
        추측으로 정할 값이 아니다(`approve.scored_flags` 가 같은 이유로 크게 실패한다).
      · **모르는 열쇠**(`confidence`·`severity`·`note`…) — 하나라도 통과시키면 다음 판에
        그 값을 읽는 코드가 생기고, 그때 모델의 취향이 승인 임계가 된다.
        ⚠ 대가는 안다: 무해한 `note` 하나에 후보 하나가 미채점이 된다. 그래도 이쪽을
        고른다 — 미채점은 '모른다'로 정직하게 남지만(승인 안 됨), 점수 열쇠를 받아들인
        판은 아무도 못 알아챈다.
      · 근거 시각이 숫자 배열이 아닌 것.

    ⚠ `bool` 은 `int` 의 하위형이라 `isinstance(v, int)` 로는 못 거른다 — 순서가 중요하다.
    """
    problems: list[str] = []
    if not isinstance(resp, dict):
        return None, [f"응답이 객체가 아니다: {type(resp).__name__} — "
                      f"계약은 {{seam_jump, hook_weak, evidence_sec}} 하나다"]

    unknown = sorted(k for k in resp if k not in ALLOWED_RESPONSE_KEYS)
    if unknown:
        problems.append(
            f"모르는 열쇠 {unknown} — 점수·정도·확신도·설명은 답하지 마라. "
            f"허용 열쇠는 {sorted(ALLOWED_RESPONSE_KEYS)} 뿐이다")

    out: dict[str, Any] = {}
    for key in FLAG_KEYS:
        if key not in resp:
            problems.append(f"{key} 가 없다 — true/false 로 반드시 답하라")
            continue
        value = resp[key]
        if not isinstance(value, bool):
            problems.append(
                f"{key} 가 true/false 가 아니다: {value!r} ({type(value).__name__}) — "
                f"점수를 매기지 마라(계약은 불리언이다)")
            continue
        out[key] = value

    evidence: list[float] = []
    if EVIDENCE_KEY in resp:
        raw = resp[EVIDENCE_KEY]
        if not isinstance(raw, list):
            problems.append(f"{EVIDENCE_KEY} 가 배열이 아니다: {raw!r} — "
                            f"근거 시각이 없으면 빈 배열로 답하라")
        else:
            for i, item in enumerate(raw):
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    problems.append(
                        f"{EVIDENCE_KEY}[{i}] 가 초(숫자)가 아니다: {item!r} — "
                        f"근거 시각은 이어 붙인 영상 기준 초다")
                    continue
                evidence.append(float(item))

    if problems:
        return None, problems
    out[EVIDENCE_KEY] = evidence
    return out, []


# ── 예산 ────────────────────────────────────────────────────────────────────

class TokenBudget:
    """누적 토큰 예산 — **check-and-increment 를 Lock 안에서 원자적으로**.

    🛑 이 클래스가 있는 이유는 `app/v3/refine.py:359` 다:

        if audit["flash_calls"] >= FLASH_BUDGET:
            raise RuntimeError("Flash 예산 소진")
        audit["flash_calls"] += 1

    순차 실행에서는 맞지만 **병렬에서는 샌다** — 두 스레드가 같은 값을 읽고 둘 다 검사를
    통과한 뒤 각자 1을 더한다. 8단계는 동시 4~8 이라 그대로 옮기면 예산이 소리 없이
    초과된다(기획서 §8 · 검수 생존 1 ③).

    `limit=None` 이면 막지 않지만 **세기는 한다** — 감사가 실제 소비를 알아야 다음 판의
    예산을 정할 수 있다(기획서 §9-A 의 'p99×2 로 재설정'이 이 숫자를 먹는다).
    """

    def __init__(self, limit: int | None = None) -> None:
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)
                                  or limit <= 0):
            raise ValueError(f"토큰 예산은 양의 정수이거나 None 이어야 한다: {limit!r}")
        self.limit = limit
        self.reserved = 0       # 아직 실제값이 안 온 예약분
        self.used = 0           # 실호출로 확정된 소비
        self._lock = threading.Lock()

    @property
    def committed(self) -> int:
        """지금 예산을 물고 있는 총량(확정 + 예약). 감사·판정의 기준."""
        with self._lock:
            return self.used + self.reserved

    def reserve(self, cost: int) -> bool:
        """예상 비용을 **원자적으로** 물고 자리를 잡는다 → 잡았으면 True.

        비용을 모른 채 호출하고 나서 세면 이미 늦다(예산은 쓰기 전에 막는 장치다).
        예상치는 `fps.usage_tokens` 로 낸다 — 산식을 여기서 다시 적지 않는다."""
        cost = max(0, int(cost))
        with self._lock:
            if self.limit is not None and self.used + self.reserved + cost > self.limit:
                return False
            self.reserved += cost
            return True

    def settle(self, reserved: int, actual: int | None) -> None:
        """예약분을 풀고 실제 소비를 계상한다. 실제값을 모르면(None) 예약분을 그대로 문다.

        ⚠ 모를 때 0 으로 치면 실패한 호출이 공짜가 된다 — 실패해도 입력 토큰은 나갔다."""
        reserved = max(0, int(reserved))
        with self._lock:
            self.reserved = max(0, self.reserved - reserved)
            self.used += reserved if actual is None else max(0, int(actual))


def _estimate_tokens(duration_sec: float) -> int:
    """이 후보 한 콜의 예상 입력 토큰. `fps.usage_tokens` 를 **부른다**(산식 복제 금지).

    과금·예산 집계는 usage 산식(프레임 66 · 오디오 초당 25)이고 상한 판정용
    `count_tokens`(71/32)와 **다른 자**다 — 예산은 과금 쪽을 쓴다(기획서 §4)."""
    return fps_mod.usage_tokens(max(0.0, float(duration_sec)), FLAG_SAMPLE_FPS)


# ── 실행 ────────────────────────────────────────────────────────────────────

def _failed(reason: str, detail: str, *, attempts: int = 0,
            usage: dict | None = None) -> dict:
    """미채점 기록. **0점이 아니다** — `approve.scored_flags` 가 이 모양을 본다."""
    entry: dict[str, Any] = {"status": "failed", "reason": reason,
                             "detail": detail, "attempts": attempts}
    if usage is not None:
        entry["usage"] = usage
    return entry


def run_flags(gemini: Any, handle: Any, cands: Sequence[Any], *,
              source_duration_sec: float,
              concurrency: int = FLAG_CONCURRENCY,
              budget_tokens: int | None = None,
              part_limit: int = PART_LIMIT,
              log=print) -> tuple[dict, dict]:
    """후보별 1콜(병렬) → (`{cand_id: 플래그}`, audit).

    · **후보 id 정렬 순서**로 결과를 담는다(결정성 — 저장 파일이 실행마다 달라지면 안 된다).
    · 예산은 **제출 전에 id 순서로 미리 잡는다.** 워커 안에서 잡으면 어느 후보가 예산에
      걸리는지가 스레드 스케줄링에 따라 달라져 같은 입력이 다른 결과를 낸다. 자리를 잡는
      것과 정산은 둘 다 `TokenBudget` 의 Lock 을 지난다.
    · 실패는 전부 **미채점**이다(`{status:"failed", …}`) — 0점으로 읽지 않는다. 한 후보가
      죽어도 나머지는 그대로 채점된다(후보 단위 증분).
    · **재질의를 두지 않는다.** 8단계는 채점이고 '모른다'가 정상 상태 셋 중 하나다
      (기획서 §7 실패 3갈래). 네트워크·5xx 재시도는 `call_video` 안에서 E11 규약으로
      이미 끝나고(그 횟수가 `attempts` 다), 계약 위반 응답까지 재질의하면 비용만 두 배가
      된다. 반려 사유는 감사에 전량 남으므로 프롬프트를 고칠 근거는 보존된다.

    ⚠ **우리 쪽 결함은 미채점으로 삼키지 않는다.** 호출 실패(`VideoCallError`)·파싱
    실패(`VideoParseError`)·계약 위반 응답은 미채점이지만, 그 밖의 예외는 그대로 올라간다
    — 모델이 준 자료의 문제와 우리 코드의 결함은 다른 것이고, 코드 결함을 '모른다'로
    적어 두면 8후보가 전부 미채점인 채 원인이 감사에 남지 않는다(조용한 송출보다 코드
    결함이 낫다는 v3 story TTS 충돌 벨트의 규율).

    🛑 핸들을 삭제하지 않는다 — 6·6b·8·10a 가 공유한다(`video.py` 독스트링).
    """
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError(f"concurrency 는 1 이상의 정수여야 한다: {concurrency!r}")

    budget = TokenBudget(budget_tokens)

    # ① 계획은 전부 순수 함수다 — 호출 전에 무엇을 붙일지가 확정된다.
    ordered = sorted(cands or [], key=candidate_id)
    ids = [candidate_id(c) for c in ordered]
    dupes = sorted({cid for cid in ids if ids.count(cid) > 1})
    if dupes:
        raise ValueError(f"후보 id 가 중복이다: {dupes} — id 는 6단계가 c%02d 로 부여한다")

    plans: list[dict] = []
    for cand in ordered:
        cid = candidate_id(cand)
        clips, note = plan_clips(cand, source_duration_sec=source_duration_sec,
                                 part_limit=part_limit)
        rec: dict[str, Any] = {"id": cid, "cand": cand, "clips": clips,
                               "note": note, "reserved": 0}
        problem = note.get("problem")
        if problem is None:
            cost = _estimate_tokens(note["duration_sec"])
            if budget.reserve(cost):
                rec["reserved"] = cost
            else:
                # 예산 소진은 **탈락이 아니라 미채점**이다(refine 의 '원판정 유지'와 같은 규율).
                note["problem"] = (
                    REASON_BUDGET,
                    f"예상 {cost} 토큰이 예산에 안 들어간다"
                    f"(한도 {budget_tokens} · 사용 {budget.committed})")
        plans.append(rec)

    for rec in plans:
        problem = rec["note"].get("problem")
        if problem is not None:
            log(f"  [v4/flags] ⚠ {rec['id']} 미채점 — {problem[0]}: {problem[1]}")

    runnable = [r for r in plans if r["note"].get("problem") is None]

    # ② 호출. 순서는 id 순으로 제출하고, 결과는 아래에서 다시 id 순으로 담는다.
    def _one(rec: dict) -> dict:
        cid = rec["id"]
        note = rec["note"]
        prompt = build_flags_prompt(rec["cand"], seam_times=note["seam_sec"],
                                    total_sec=note["duration_sec"])
        usage: dict | None = None
        try:
            resp, usage = call_video(
                gemini, handle, prompt,
                sample_fps=FLAG_SAMPLE_FPS, clips=rec["clips"],
                media_resolution=None,          # 미지정 = 실측상 LOW(HIGH 는 11:style 몫)
                max_output_tokens=FLAG_MAX_OUTPUT_TOKENS,
                model=None,                     # Flash 슬롯 고정(기획서 §6)
                log=log)
        except VideoParseError as e:
            usage = e.usage
            return {"id": cid, "usage": usage,
                    "entry": _failed(REASON_PARSE_FAILED, str(e),
                                     attempts=_attempts(usage), usage=usage)}
        except VideoCallError as e:
            usage = getattr(e, "usage", None)
            return {"id": cid, "usage": usage,
                    "entry": _failed(REASON_CALL_FAILED, str(e),
                                     attempts=_attempts(usage), usage=usage)}
        flags, problems = validate_flags_response(resp)
        if flags is None:
            return {"id": cid, "usage": usage,
                    "entry": _failed(REASON_INVALID, " · ".join(problems),
                                     attempts=_attempts(usage), usage=usage)}
        entry = {**flags, "status": FLAGS_STATUS_OK, "attempts": _attempts(usage)}
        return {"id": cid, "usage": usage, "entry": entry}

    results: dict[str, dict] = {}
    if runnable:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(runnable)),
                                thread_name_prefix="v4-flags") as pool:
            for out in pool.map(_one, runnable):
                results[out["id"]] = out
    for rec in runnable:
        out = results.get(rec["id"])
        usage = (out or {}).get("usage")
        total = usage.get("total") if isinstance(usage, dict) else None
        budget.settle(rec["reserved"], total if isinstance(total, int) else None)
        rec["usage"] = usage

    # ③ 담기 — **id 정렬 순서**. 저장 파일이 실행마다 달라지면 안 된다.
    flags_out: dict[str, dict] = {}
    audit_cands: list[dict] = []
    scored = 0
    for rec in plans:
        cid = rec["id"]
        note = rec["note"]
        problem = note.get("problem")
        if problem is not None:
            entry = _failed(problem[0], problem[1])
        else:
            entry = results[cid]["entry"]
        if entry.get("status") == FLAGS_STATUS_OK:
            scored += 1
        elif problem is None:
            # 호출까지 갔다가 진 것 — 계획 단계 실패는 위에서 이미 남겼다(중복 금지).
            log(f"  [v4/flags] ⚠ {cid} 미채점 — "
                f"{entry.get('reason')}: {entry.get('detail')}")
        flags_out[cid] = entry
        row: dict[str, Any] = {
            "id": cid, "of_segments": note.get("of"), "parts": note.get("parts"),
            "status": entry.get("status"),
        }
        for key in ("reason", "detail"):
            if key in entry:
                row[key] = entry[key]
        for key in ("belt", "merged", "seam_sec", "duration_sec"):
            if key in note:
                row[key] = note[key]
        if rec.get("usage"):
            row["usage"] = rec["usage"]
        audit_cands.append(row)

    audit = {
        "of": len(plans),
        "scored": scored,
        "unscored": len(plans) - scored,
        "calls": len(runnable),
        "concurrency": concurrency,
        "sample_fps": FLAG_SAMPLE_FPS,
        "max_output_tokens": FLAG_MAX_OUTPUT_TOKENS,
        "part_limit": part_limit,
        "budget_tokens": budget_tokens,
        "budget_used": budget.committed,
        "candidates": audit_cands,
    }
    # 전량 미채점은 크게 남긴다 — 9단계가 `scoring_unavailable` 로 읽고 1위만 경고와 함께
    # 낸다(기획서 §7 실패 3갈래). 조용하면 '왜 아무도 승인이 안 됐지'가 된다.
    if plans and scored == 0:
        log(f"  [v4/flags] 🛑 전량 미채점 {len(plans)}/{len(plans)} — "
            f"순위 신호 없음(scoring_unavailable)")
    else:
        log(f"  [v4/flags] 채점 {scored}/{len(plans)} · 콜 {len(runnable)} · "
            f"동시 {concurrency} · 토큰 {budget.committed}")
    return flags_out, audit


def _attempts(usage: dict | None) -> int:
    """실제로 몇 번 쳤나. `call_video` 가 `retries`(재시도 횟수)를 usage 에 남긴다.

    ⚠ 재질의(반려 후 재질문) 횟수가 아니라 **E11 재시도** 횟수다 — 8단계에는 재질의가
    없다(`run_flags` 독스트링)."""
    if not isinstance(usage, dict):
        return 1
    retries = usage.get("retries")
    return int(retries) + 1 if isinstance(retries, int) and retries >= 0 else 1
