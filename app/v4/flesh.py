"""10단계 — 살붙이기. 승인된 **편마다** 제목·내레이션·라벨·발행 메타를 짓는다.

계약 정본 `docs/v4/M5-interfaces.md` §2(+ §0 의 열쇠 조사표·§4 배선).
기획 `docs/v4/v4-plan.md` §3 "10 살붙이기" · 운영자 결정 **O8**(살붙이기를 승인 전부에).

    9 승인(후보 id 목록)
        │  bridge.cross   ← 다리는 **여기 한 곳**이다(계약 §4)
        ▼  스냅 → span → 인용 보호 → span_index → 비트 뼈대
    10 살붙이기 [편마다 텍스트 1콜 + 설명 1콜 · 병렬]
        │  story.trim_to_budget · plan_narration_slots · verify_tts_conflicts
        ▼
    story 문서 {cand_id: doc}  → (M6) 11:resources 가 조립·자막·TTS

## 이 단계가 6단계와 나뉘어 있는 이유 (운영자 결정 O4)

6단계는 후보 5~16개를 내는데, 거기서 내레이션·최종 제목까지 쓰게 하면 ① 출력이 잘리고
② **탈락할 후보의 카피 값까지 치른다**(출력 단가는 입력의 5배다). 그래서 6단계는 제목
**가안**만 받고, 확정 카피는 승인이 끝난 뒤 승인 편에만 쓴다. 그 대가로 이 단계는 v3
`story.PROMPT_TEMPLATE` 의 **규칙 4·5·6**(6단계가 일부러 안 실은 그것들)을 여기서 진다.

## 규율

1. **구간을 바꾸지 않는다.** 이 단계가 받는 것은 이미 6c·7·8·9 를 지난 확정 조각이다.
   모델에게도 그렇게 말한다(프롬프트 1행) — 여기서 시각이 움직이면 승인 게이트가 본
   화면과 발행되는 화면이 달라진다.
2. **모델은 span id 로만 말한다.** v3 Stage 3 이 세운 규약 그대로 — 확정 시각은 전부
   격자 조회다(`bridge.build_span_index` 가 채운 `t_in`/`t_out`).
3. **한 편의 실패는 그 편만**(기획서 §7). 다른 편의 산출을 되돌리지 않는다. 다만
   **전량 실패는 크게 실패**한다 — 승인이 있었는데 낼 것이 없으면 조용한 결번이고,
   그것이 이 레포에서 가장 나쁜 실패다(무인 노드 6대).
4. **우리 쪽 결함은 삼키지 않는다.** 모델 응답의 문제(반려·파싱·호출)는 그 편의 탈락
   으로 남기지만, TTS 충돌 벨트 위반(`verify_tts_conflicts`)은 **코드 결함**이라 그대로
   올라간다(`flags.run_flags` 와 같은 규율 · v3 `run_story` 의 AssertionError 계승).
5. **슬롯 배치·예산·충돌 벨트는 v3 것을 그대로 부른다**(계약 §2). 여기서 다시 짜면
   내레이션이 대사 위에 얹히는 규칙이 두 벌이 된다.

## 🛑 `trim_to_budget` 의 'climax' 하드코딩 (계약 §2 의 경고를 코드로 닫았다)

`story.trim_to_budget:271` 은 보호 대상을 **역할 문자열 `"climax"` 로 하드코딩**한다:

    if b["role"] == "climax" or len(b["span_ids"]) <= 1: continue

v4 템플릿 4종 중 그 이름을 쓰는 것은 `recap_dialogue` 뿐이다 — `conflict_payoff` 의
절정은 `turn`/`payoff`, `chemi_observe` 의 절정은 `ensemble` 이라 **보호가 안 걸린다**
(예산 초과 시 절정 비트의 가장자리 span 이 깎인다). 계약은 "확인하고 맞추거나 기록하라"
고 했고, 여기서는 **맞췄다**: `trim_to_budget_by_role` 이 그 템플릿의 `required_roles`
(레지스트리 `STORY_TEMPLATE_SPECS` 가 정본 — 목록을 여기서 새로 적지 않는다)를 잠깐
`"climax"` 로 갈아 끼운 **사본 뷰**로 v3 함수를 부르고, 돌아온 뒤 역할 이름을 되돌린다.
v3 함수를 한 글자도 안 고치면서 보호 범위만 템플릿을 따라가게 하는 방법이라 골랐다.
갈아 끼운 역할은 audit `protected_roles` 에 남는다.

## 이 판이 짓지 않는 것

· **효과 문구(효과 텍스트·스티커·자막 강조)** — 기획서 §3 은 11:style 이 초벌을 보고
  정한다고 못박는다(HIGH 로 방송 텔롭을 읽어 피할 자리를 안다). 계약 §2 의 검증기 목록
  에도 없다. 그래서 응답 열쇠에서 **빠져 있고, 오면 반려된다**(모르는 열쇠 규율).
· `checkpoint_story.json` 의 v1 모양(`variants[k]`·`clips`·`title_text`) — 계약 §5 대로
  M6 의 일이다. 여기 산출은 **편별 story 문서**까지다.

## 실호출로만 알 수 있는 것 (이 워크트리에 `GEMINI_API_KEY` 가 없다)

· `FLESH_MAX_OUTPUT_TOKENS`(16,384)가 충분한가 — 값은 v3 `story._call_story_model` 이
  같은 종류의 응답(제목·내레이션·라벨)에 쓰던 자다. thinking 이 같은 예산을 나눠 쓰므로
  절단이 실제로 나면 `_call_text` 가 MAX_TOKENS 를 크게 남긴다(v3-M2 판례).
· 규칙 4 의 12~16자를 모델이 실제로 지키는가 — 지키지 않으면 `plan_narration_slots` 가
  `mode="fit"` 으로 줄이거나 드랍하고, 그 사실은 `narration_dropped`·audit 에 남는다.
· 편당 입력 10,000 토큰(기획서 §4 비용표) 추정의 실측.
"""
from __future__ import annotations

import copy
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

from app.modules.gemini_client import _max_tokens_usage
# 제목 한 줄 상한의 정본은 **렌더러 제약**이다(20자를 넘으면 줄이 접혀 3줄이 된다 —
# E21-2). 숫자를 여기서 다시 적지 않는다(규율 4).
from app.modules.style_compose import MAX_TITLE_LINE_CHARS
from app.v3 import story as story_mod
from app.v3.seq_analyze import MAX_REASKS
from app.v3.story import LOW_CONF, STORY_TEMPLATE_SPECS
from app.v4 import bridge
from app.v4 import video as video_mod
# ⚠ 밑줄 없는 공개 이름 셋을 건너와 부른다 — 후보 id 규칙(`candidate_id`)·프롬프트 지문
# (`prompt_sha`)·템플릿 설명(`_template_block`)이 6·8단계와 **같은 자**여야 한다.
from app.v4.candidates import TITLE_DRAFT_MAX_CHARS, _template_block, prompt_sha
from app.v4.flags import candidate_id

# ── 계약 상수 (M5 §2) ───────────────────────────────────────────────────────

# v3 `story._call_story_model` 이 같은 종류의 응답에 쓰던 자(제목·내레이션·라벨).
FLESH_MAX_OUTPUT_TOKENS = 16384

# 제목 두 줄 각각의 확정 상한. **여기서는 strict** 다 — 6단계(가안)는 넘치면 잘라내고
# 노트로 넘어가지만(`candidates._title_draft`), 이 값이 곧 화면에 나가는 제목이다.
TITLE_MAX_CHARS = MAX_TITLE_LINE_CHARS

# v3 규칙 4 의 실측 자수(공백 포함). 프롬프트와 검증기가 **같은 숫자**를 봐야 한다 —
# 프롬프트만 고치면 모델은 계속 같은 길이를 내고, 검증기만 고치면 매 편 반려당한다(E17-1).
NARRATION_SENT_CHARS = (12, 16)

# v3 규칙 5 — 편 전체 라벨 수. 상한 초과는 **앞에서부터 남기고 자른 사실을 기록**한다
# (E15 하드캡 규율: 조용한 절단 금지).
LABELS_PER_EPISODE = (2, 4)

# 동시 4 로 시작한다 — 8단계와 같은 자리(기획서 §7 "8단계와 10단계는 병렬(4→8)").
# 여기는 텍스트 콜이라 콜당 토큰이 8단계의 1/20 이지만, 올리는 판단은 실측 뒤다.
FLESH_CONCURRENCY = 4

# 유튜브 설명·해시태그. ⚠ **플랫폼 문서 기억이지 실측이 아니다** — 설명 5,000자는 유튜브
# 의 입력 상한으로 알려진 값이고, 해시태그는 15개를 넘기면 전부 무시된다고 알려져 있어
# 여유를 크게 둬 10 으로 잡았다. 실발행 실측이 생기면 이 두 값만 갈아끼운다.
DESCRIPTION_MAX_CHARS = 5000
HASHTAGS_PER_EPISODE = (3, 10)

if TITLE_DRAFT_MAX_CHARS > TITLE_MAX_CHARS:
    # 6단계 가안이 10단계 확정보다 길면, 사람이 보기에 멀쩡한 가안을 받은 편이 여기서
    # 매번 반려된다(그리고 반려 소진 = 그 편 탈락). 그 전에 크게 실패한다.
    raise ValueError(
        f"6단계 제목 가안 상한({TITLE_DRAFT_MAX_CHARS})이 10단계 확정 상한"
        f"({TITLE_MAX_CHARS})보다 크다 — 가안을 통과한 제목이 확정에서 매번 반려된다")

# v3 `trim_to_budget` 이 **하드코딩으로** 보호하는 역할 이름(모듈 독스트링 §🛑).
PROTECTED_ROLE_CANON = "climax"

# ── 응답 계약 어휘 ──────────────────────────────────────────────────────────
# 이 집합 밖 열쇠는 **반려**다. 하나라도 통과시키면 다음 판에 그 값을 읽는 코드가 생기고,
# 그때부터 계약이 아니라 모델의 습관이 산출을 정한다(8단계 `ALLOWED_RESPONSE_KEYS` 규율).
RESPONSE_KEYS: frozenset[str] = frozenset({"title", "reason", "beats"})
TITLE_KEYS: frozenset[str] = frozenset({"line1", "line2"})
BEAT_KEYS: frozenset[str] = frozenset({"number", "role", "narration", "labels"})
LABEL_KEYS: frozenset[str] = frozenset({"text", "span_id"})
DESCRIPTION_KEYS: frozenset[str] = frozenset({"description", "hashtags"})

# ── 실패 어휘 ───────────────────────────────────────────────────────────────
# 이 문자열은 audit 과 run_log 에 **그대로** 실린다 — 바꾸면 저장된 잡의 감사 기록과
# 대조가 끊긴다(8단계 REASON_* 와 같은 규율). 테스트가 값으로 박제한다.
REASON_NO_SPANS = "bridge_no_spans"        # 다리가 span 을 못 잡았다(조각이 격자보다 짧다)
REASON_CALL_FAILED = "call_failed"         # 호출 실패(E11 분류는 `call_video` 쪽에서 끝난다)
REASON_REASK_EXHAUSTED = "reask_exhausted"  # 반려 소진 — 사유는 attempts 에 전량 있다
REASON_UNKNOWN_TEMPLATE = "unknown_template"   # 후보의 템플릿을 레지스트리가 모른다

SCHEMA_FLESH = "v4_story/v1"

__all__ = [
    "FLESH_MAX_OUTPUT_TOKENS", "TITLE_MAX_CHARS", "NARRATION_SENT_CHARS",
    "LABELS_PER_EPISODE", "FLESH_CONCURRENCY", "DESCRIPTION_MAX_CHARS",
    "HASHTAGS_PER_EPISODE", "PROTECTED_ROLE_CANON", "SCHEMA_FLESH",
    "RESPONSE_KEYS", "TITLE_KEYS", "BEAT_KEYS", "LABEL_KEYS", "DESCRIPTION_KEYS",
    "REASON_NO_SPANS", "REASON_CALL_FAILED", "REASON_REASK_EXHAUSTED",
    "REASON_UNKNOWN_TEMPLATE",
    "FLESH_PROMPT", "DESCRIPTION_PROMPT",
    "material_block", "protected_roles", "trim_to_budget_by_role",
    "build_flesh_prompt", "validate_flesh_response", "run_flesh",
    "build_description_prompt", "validate_description_response", "run_description",
]


# ── 텍스트 콜 ───────────────────────────────────────────────────────────────

def _call_text(gemini: Any, prompt: str, *, max_output_tokens: int,
               log=print) -> tuple[Any, dict]:
    """Flash 텍스트 1회 호출 → (파싱된 JSON, usage).

    🛑 **영상을 붙이지 않는다.** 10단계는 텍스트 온리다(기획서 §4 비용표: 편당 10,000
    토큰). 영상을 다시 보는 것은 10a(정밀 청취)뿐이고, 그쪽은 별도 파일이다.

    실패 어휘·분류·파싱 폴백은 `app/v4/video.py` 것을 **그대로 부른다**(이름이 video 지만
    이 모듈이 쓰는 실패 어휘의 정본이다):
      · 재시도 — `classify_error` 의 E11 규약(429·5xx·네트워크만 ≤2회, 백오프 2s·4s).
        그 밖의 4xx 는 즉시 실패한다(400 을 세 번 보내는 것은 요금만 세 배).
      · 파싱 — `_parse_response_text`(마크다운 펜스 제거 → `_loads_first_json` 폴백).
        2026-08-03 실측(분석 22회 중 12회 파싱 실패)을 구제한 그 경로다.
      · 예외 — `VideoCallError`/`VideoParseError`. 두 벌로 두면 401·429 판정이 갈린다.

    모델은 **Flash 슬롯**이다(CLAUDE.md 모델 정책: Pro 는 영상을 실제로 보는 호출에만).
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 가 비었다")
    types = gemini.types
    config = types.GenerateContentConfig(
        temperature=video_mod.TEMPERATURE,            # 결정성 — 같은 입력이면 같은 산출
        response_mime_type=video_mod.RESPONSE_MIME_TYPE,
        max_output_tokens=int(max_output_tokens),
    )
    model_name = gemini.config.flash_model_name

    response = None
    elapsed = 0.0
    attempts = 0
    for attempt in range(video_mod.MAX_RETRIES + 1):
        attempts = attempt + 1
        t0 = time.time()
        try:
            response = gemini.client.models.generate_content(
                model=model_name, contents=[prompt], config=config)
            elapsed = round(time.time() - t0, 3)
            break
        except Exception as e:                        # noqa: BLE001 — 아래에서 분류한다
            elapsed = round(time.time() - t0, 3)
            kind = video_mod.classify_error(e)
            if kind == "permanent" or attempt == video_mod.MAX_RETRIES:
                raise video_mod.VideoCallError(
                    f"텍스트 호출 실패({kind}, 시도 {attempts}/"
                    f"{video_mod.MAX_RETRIES + 1}) — {type(e).__name__}: {e}",
                    kind=kind) from e
            wait = video_mod.RETRY_BACKOFF_SEC * (attempt + 1)
            log(f"  [v4/flesh] 재시도 {attempt + 1}/{video_mod.MAX_RETRIES} — "
                f"{type(e).__name__}: {e} ({wait:.0f}s 뒤)")
            time.sleep(wait)

    # 영상 파트가 없으므로 fps·해상도는 **None**(0 이 아니다 — '안 썼다'와 '0 이었다'를
    # 같게 적으면 집계가 조용히 틀어진다. `usage_note` 독스트링의 규율).
    usage = video_mod.usage_note(response, elapsed_sec=elapsed, sample_fps=None,
                                 media_resolution=None, parts=0)
    usage["retries"] = attempts - 1
    usage["model"] = model_name

    truncated = _max_tokens_usage(response)
    if truncated:
        log(f"  [v4/flesh] 🛑 MAX_TOKENS 절단 — 응답이 끝나지 않았다 ({truncated}). "
            f"max_output_tokens={max_output_tokens}")
    text = getattr(response, "text", None) or ""
    try:
        return video_mod._parse_response_text(text), usage
    except ValueError as e:                           # json.JSONDecodeError 는 ValueError 다
        raise video_mod.VideoParseError(
            "응답 JSON 파싱 실패"
            + (f" (MAX_TOKENS 절단: {truncated})" if truncated else "")
            + f" [finish_reason={usage['finish_reason']}]: {e} — 앞 200자: {text[:200]!r}",
            usage=usage, raw_text=text) from e


# ── 재료 블록 ───────────────────────────────────────────────────────────────

def _speech_of(span: dict) -> str:
    """유성 span 의 대사 한 줄. 화자가 있으면 `화자: 대사`(10a 산출), 없으면 전사 그대로.

    ⚠ 화자의 유일한 원천은 10a 다(`bridge` 모듈 독스트링 §0) — 꺼져 있으면 여기도 이름이
    없고, 자막도 전 줄 흰색이다. 그 사실은 `bridge.index_audit` 이 경고로 알린다."""
    script = span.get("audio_script") or []
    lines = [f"{a.get('speaker')}: {a.get('line')}" for a in script
             if isinstance(a, dict) and str(a.get("line") or "").strip()]
    if lines:
        return " / ".join(lines)
    return str(span.get("text") or "").strip()


def _trust_tag(span: dict, speech: str) -> str:
    """대사 신뢰 표기 — v3 `story.build_material_block` 과 **같은 어휘·같은 임계**.

    `[대사없음]`·`[청취]`·`[저확신 …]` 셋 다 v4 에서 의미가 산다: 앞 둘은 10a 가 붙이는
    `text_source` 에서 오고(`bridge.build_span_index`), `[저확신]` 은 whisper 단어 확률의
    평균이다(임계 `story.LOW_CONF`).

    ⚠ 6단계 전사 블록의 같은 표지와 **같은 임계**를 써야 한다 — 6단계가 저확신이라 피하라고
    한 줄이 10단계 재료에서는 멀쩡해 보이면, 모델이 그 줄을 인용해 자막이 깨진다."""
    src = span.get("text_source")
    conf = span.get("conf")
    if src == "none" or not speech.strip():
        return " [대사없음]"
    if src == "heard":
        return " [청취]"
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and conf < LOW_CONF:
        return f" [저확신 {conf:.2f}]"
    return ""


def material_block(span_index: dict[str, dict], span_ids: Sequence[Sequence[str]]) -> str:
    """비트별 재료 표 — **채택된 span 만**. 순수·결정적.

    🛑 v3 `story.build_material_block` 은 분석된 span 을 **전량** 싣는다(`only` 인자가
    없다). 10단계가 그러면 67분 소재에서 프롬프트가 두 배가 되고, 그 대부분은 이 편이
    쓰지 않는 구간이다(계약 §2). 그래서 여기서는 `span_ids`(비트별 채택 목록)만 돈다.

    행 모양은 v3 재료 표를 따른다(`sid | 유성/무성 길이 | imp N [신뢰표기] | 내용`) —
    같은 모델이 v3 에서 읽던 표라 형식을 바꿀 이유가 없다. 다른 것은 묶는 단위뿐이다:
    v3 는 meaning(의미 단위)으로 묶고 v4 는 **비트**(= 확정 조각)로 묶는다."""
    lines: list[str] = []
    for bi, ids in enumerate(span_ids or []):
        ids = [str(s) for s in ids or []]
        known = [s for s in ids if s in span_index]
        if not known:
            # 빈 비트는 `bridge.to_beats` 가 이미 크게 실패시킨다(빈 비트 금지) — 여기까지
            # 오면 배선 사고이므로 표에 그대로 드러낸다(조용히 건너뛰지 않는다).
            lines.append(f"\n### 비트 {bi} — ⚠ 재료 span 이 없다")
            continue
        t0 = span_index[known[0]]["t_in"]
        t1 = span_index[known[-1]]["t_out"]
        lines.append(f"\n### 비트 {bi} — {t1 - t0:.1f}초 · span {len(known)}개")
        for sid in known:
            sp = span_index[sid]
            dur = float(sp["t_out"]) - float(sp["t_in"])
            if sp.get("is_audio"):
                speech = _speech_of(sp)
                lines.append(f"{sid} | 유성 {dur:.1f}s | imp {sp['importance']}"
                             f"{_trust_tag(sp, speech)} | {speech}")
            else:
                lines.append(f"{sid} | 무성 {dur:.1f}s | imp {sp['importance']} | "
                             # 장면 설명은 10a 가 채운다 — 꺼져 있으면 비는 것이 정상이다.
                             f"{str(sp.get('scene_script') or '').strip() or '(장면 설명 없음)'}")
    return "\n".join(lines)


# ── 프롬프트 ────────────────────────────────────────────────────────────────

# 규칙 4·5·6 은 v3 `story.PROMPT_TEMPLATE` 의 같은 번호 규칙이다(6단계가 일부러 안 실은
# 그것들 — `candidates.py` 모듈 독스트링이 그 사실을 적어 뒀다). **실측 인용 세 건은 v3
# 문장 그대로**다: "천재에게 반주를 부탁한 여학생"(15자·2.0s) · "돌아온 답은
# 최악이었죠"(12자·2.15s) · 잘려 나간 실측("놀랍게도 승객들은 … → 관광 접고 전유진").
# 그 문장이 규칙의 근거이므로 요약하거나 바꿔 적지 않는다.
#
# ⚠ v3 문장에서 **의도적으로 고친 곳 둘**:
#   ① 규칙 4 의 "hook/context/bridge 비트에만" — 그 역할 이름은 recap_dialogue 의 것이고
#      v4 는 템플릿 4종이 각자 다른 역할 어휘를 쓴다. 역할 이름 대신 "그 템플릿에서
#      전환·배경을 맡는 비트"로 적고, 역할 목록은 템플릿 설명이 준다.
#   ② 규칙 6 의 "각 N자 이내" 뒤에 **strict** 라는 사실을 더했다 — v3 에서는 가안 뒤
#      한 번 더 손볼 기회가 있었지만 v4 에서는 이 값이 곧 화면이다.
FLESH_PROMPT = """당신은 리캡 쇼츠 구성작가다. 아래 편은 **편집 구간이 이미 확정**됐다. 네가 하는 일은 그 위에 카피를 입히는 것 하나다 — 제목 두 줄, 비트별 내레이션, 화면 라벨.

🛑 구간은 바꿀 수 없다. 시각을 쓰지 마라. 비트 번호와 span id 로만 말한다. 새 구간을 제안하거나 span 을 빼자고 하지 마라 — 그런 답은 통째로 버려진다.

## 작품
{work_title}{research_block}

## 이 편 (템플릿 {template} · 길이 {total_sec:.0f}초 · 목표 {target_sec:.0f}초 · 상한 {max_sec:.0f}초)
{template_block}
- 6단계 제목 가안: {draft_line1} / {draft_line2}
- 6단계 선택 사유: {candidate_reason}

## 재료 — 이 편이 쓰는 span 만 (id | 유성/무성 길이 | importance | 내용)
{material_block}

## 규칙
1. 비트마다 역할(role)을 붙여라. 위 템플릿 설명의 역할 어휘를 쓰고, {required_roles_text}
2. 라벨·내레이션은 **위에 있는 span id** 로만 앵커한다. 목록 밖 id 를 쓰면 반려된다.
3. 대사 신뢰 — {trust_rule}
4. 내레이션(narration)은 그 템플릿에서 전환·배경 설명을 맡는 비트에만, **짧은 문장 2~3개의 배열**로 써라. **문장당 공백 포함 {nar_min}~{nar_max}자**를 지켜라 — 레퍼런스 실측이 그 길이다("천재에게 반주를 부탁한 여학생" 15자·2.0s / "돌아온 답은 최악이었죠" 12자·2.15s). {nar_over}자를 넘으면 얹을 창을 못 찾아 코드가 잘라내고, 그러면 문장이 아니라 조각이 화면에 나간다(실측: "놀랍게도 승객들은 유람선 관광까지 포기하고…" → "관광 접고 전유진"). 서술체(~했어요/~했죠), 대사 위에 얹지 마라.
5. 라벨(labels)은 "(팩폭 시전)" 식 괄호 심리 강조 — **편 전체에서 {lab_min}~{lab_max}개**, 심리·상황이 꺾이는 **그 대사 순간의 span** 에 앵커한다(`{{"text": "(팩폭 시전)", "span_id": "sp0123"}}`). 앵커는 반드시 그 비트의 span 중 하나.
6. 제목 2줄: line1=상황(관계+사건), line2=펀치. 각 {title_max}자 이내. **가안이 아니라 확정이다** — 한 글자라도 넘으면 그 편이 반려된다(넘으면 줄이 접혀 3줄이 되고 제목 블록이 깨진다).
{reject_block}
## 출력 (JSON 만)
{{"title": {{"line1": "…", "line2": "…"}}, "reason": "이 카피를 고른 이유 한 문장",
  "beats": [
   {{"number": 0, "role": "hook", "narration": ["짧은 문장1", "짧은 문장2"], "labels": []}},
   {{"number": 1, "role": "{climax_role}", "narration": [], "labels": [{{"text": "(…)", "span_id": "sp0103"}}]}}
  ]}}
· 위 세 열쇠(title·reason·beats)와 비트의 네 열쇠(number·role·narration·labels) 밖은 답하지 마라 — 모르는 열쇠가 있으면 반려된다.
· 내레이션이 없는 비트는 `narration` 을 빈 배열로 둔다. 비트를 빠뜨려도 된다(그 비트는 내레이션·라벨이 없는 것으로 읽는다)."""


def _research_block(research_context: Any) -> str:
    """리서치 절 — 상한 800자는 v3 `story.build_story_prompt` 와 같은 자(같은 재료·같은 자리)."""
    text = str(research_context or "").strip()
    return ("\n" + text[:800]) if text else ""


# 신뢰 표기별 지시 한 줄씩. 순서가 고정이라 같은 재료면 같은 문장이 나온다(결정성).
_TRUST_CLAUSES: tuple[tuple[str, str], ...] = (
    ("[대사없음]", "`[대사없음]` span 은 대사 인용으로 다루지 마라"
                   "(무성 재료·장면으로는 가능)."),
    ("[저확신", "`[저확신 …]` 은 받아쓰기가 흔들린 구간이라 자막이 깨질 수 있으니 "
                "그 대사를 라벨·내레이션의 근거로 삼지 마라."),
    ("[청취]", "`[청취]` 는 확정된 대사다(그대로 써도 된다)."),
)


def _trust_rule(block: str) -> str:
    """규칙 3(대사 신뢰) — **재료에 실제로 붙은 표기만** 말한다. 순수.

    🛑 6단계가 배운 것을 그대로 지킨다(`candidates.py` 모듈 독스트링): *"없는 표지를
    프롬프트에 적으면 모델은 전사에서 그것을 찾다가 못 찾고, 규칙 자체를 무시한다."*
    v4 에서 `[청취]`·`[대사없음]` 은 10a(정밀 청취)가 붙이는 표기라, 10a 가 꺼진 편의
    재료에는 아예 없다 — 그런 편에 그 규칙을 적으면 규칙 3 전체가 헛돈다."""
    said = [text for tag, text in _TRUST_CLAUSES if tag in block]
    if not said:
        return "이 편의 재료에는 신뢰 표기가 붙은 줄이 없다(전사 그대로 써도 된다)."
    return " ".join(said)


def _required_roles_text(template: str) -> str:
    spec = STORY_TEMPLATE_SPECS.get(template)
    if spec is None:
        raise ValueError(f"모르는 스토리 템플릿 {template!r} — 사용 가능: "
                         f"{sorted(STORY_TEMPLATE_SPECS)}")
    need = tuple(spec.get("required_roles") or ())
    if not need:
        return "역할 이름은 자유롭게 골라도 된다(빈 값은 안 된다)."
    return (f"**{' · '.join(need)} 비트는 반드시 하나씩 있어야 한다** — 없으면 반려된다.")


def build_flesh_prompt(*, work_title: str, candidate: dict,
                       span_index: dict[str, dict], span_ids: Sequence[Sequence[str]],
                       research_context: str = "", template: str,
                       target_sec: float, max_sec: float,
                       reject_note: str = "") -> str:
    """10단계 프롬프트. 순수 — 같은 인자면 같은 문자열(지문의 전제).

    담는 것(계약 §2): v3 규칙 **4·5·6**(내레이션 자수 · 라벨 2~4개 + span 앵커 · 제목 두 줄
    strict) + 채택 span 재료 + 템플릿 설명 + 필수 역할.
    담지 않는 것: 구간 제안·효과 문구(모듈 독스트링 "이 판이 짓지 않는 것").

    ⚠ 제목 가안·선택 사유는 **싣는다** — 8단계(플래그)가 같은 것을 일부러 뺀 것과 반대다.
    거기는 화면 사고를 보는 채점이라 사람 말이 편향이 되지만, 여기는 그 의도를 이어받아
    카피를 쓰는 자리다(기획서 §3: "제목은 가안이다 … 확정은 뒷단계가 한다")."""
    total_sec = sum(float(span_index[s]["t_out"]) - float(span_index[s]["t_in"])
                    for ids in (span_ids or []) for s in ids if s in span_index)
    draft = candidate.get("title_draft") or {}
    reject_block = ""
    if reject_note and reject_note.strip():
        reject_block = ("\n## ⚠ 직전 제안 반려 사유 — 전부 고쳐서 다시 내라\n"
                        f"{reject_note.strip()}\n")
    spec = STORY_TEMPLATE_SPECS.get(template) or {}
    need = tuple(spec.get("required_roles") or ())
    block = material_block(span_index, span_ids)
    return FLESH_PROMPT.format(
        work_title=work_title, research_block=_research_block(research_context),
        template=template,
        # 템플릿 설명은 6단계가 쓰는 **그 함수**가 만든다 — 두 단계가 다른 문장으로 같은
        # 템플릿을 설명하면 모델이 6단계에서 고른 구성과 다른 구성을 쓴다.
        template_block=_template_block((template,)),
        total_sec=total_sec, target_sec=float(target_sec), max_sec=float(max_sec),
        draft_line1=str(draft.get("line1") or "(없음)"),
        draft_line2=str(draft.get("line2") or "(없음)"),
        candidate_reason=str(candidate.get("reason") or "(없음)"),
        material_block=block,
        # 규칙 3 은 **재료에 실제로 붙은 표기만** 말한다(`_trust_rule` 독스트링).
        trust_rule=_trust_rule(block),
        required_roles_text=_required_roles_text(template),
        nar_min=NARRATION_SENT_CHARS[0], nar_max=NARRATION_SENT_CHARS[1],
        nar_over=NARRATION_SENT_CHARS[1] + 1,
        lab_min=LABELS_PER_EPISODE[0], lab_max=LABELS_PER_EPISODE[1],
        title_max=TITLE_MAX_CHARS, reject_block=reject_block,
        # 예시의 둘째 비트는 **그 템플릿의 절정 역할**이다(라벨이 붙는 자리) —
        # 첫 비트에 절정을 박아 두면 모델이 그 순서로 편성한다.
        climax_role=(need[-1] if need else "build"))


# ── 응답 검증 ───────────────────────────────────────────────────────────────

def _norm_narration(raw: Any, *, where: str, problems: list[str],
                    notes: list[str]) -> list[str]:
    """내레이션 → 문장 배열. 배열이 계약이고, 단일 문자열은 **받되 기록**한다.

    v3 `validate_story_response` 는 단일 문자열을 조용히 감쌌다(하위호환). 여기서는 감싸되
    노트를 남긴다 — 조용히 고치면 모델은 계속 문자열을 내고, 규칙 4 의 '문장 2~3개' 리듬이
    사라진 것을 아무도 모른다."""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        notes.append(f"{where}: narration 이 배열이 아니라 문자열이다 — 한 문장으로 읽었다")
        raw = [text]
    if not isinstance(raw, list):
        problems.append(f"{where}: narration 은 짧은 문장의 배열이어야 한다"
                        f"({type(raw).__name__})")
        return []
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            problems.append(f"{where} narration[{i}]: 문자열이 아니다({type(item).__name__})")
            continue
        text = item.strip()
        if not text:
            notes.append(f"{where} narration[{i}]: 빈 문장 — 버렸다")
            continue
        if len(text) > NARRATION_SENT_CHARS[1]:
            # 🛑 반려하지 않는다. 이 위반의 결과는 이미 아래에서 **기계가 처리**한다 —
            # `plan_narration_slots` 가 창을 못 찾으면 fit 으로 줄이거나 드랍하고 그 사실을
            # `narration_dropped` 에 남긴다(v3 도 자수를 검증기에서 막지 않는다). 여기서
            # 반려하면 카피 한 문장 때문에 승인된 편이 통째로 탈락한다.
            notes.append(f"{where} narration[{i}]: {len(text)}자 — 규칙 4 상한"
                         f"({NARRATION_SENT_CHARS[1]}자) 초과. 창을 못 찾으면 코드가 줄인다: "
                         f"{text[:20]!r}")
        out.append(text)
    return out


def _norm_labels(raw: Any, *, where: str, span_ids: set[str],
                 problems: list[str], notes: list[str]) -> list[dict]:
    """라벨 → `[{text, span_id}]`. 앵커는 **이 편의 span** 이어야 한다(계약 §2)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(f"{where}: labels 는 배열이어야 한다({type(raw).__name__})")
        return []
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            # 단일 문자열 라벨은 v3 가 '첫 span 앵커'로 받았지만 여기서는 반려한다 —
            # 라벨의 값은 **어느 대사 순간에 붙는가**이고(규칙 5), 앵커 없는 라벨은
            # 우리가 자리를 지어내야 한다. 지어낸 자리는 근거가 없다.
            problems.append(f"{where} labels[{i}]: {{text, span_id}} 객체여야 한다"
                            f"({type(item).__name__}) — 앵커 없는 라벨은 받지 않는다")
            continue
        unknown = sorted(k for k in item if k not in LABEL_KEYS)
        if unknown:
            problems.append(f"{where} labels[{i}]: 모르는 열쇠 {unknown} — "
                            f"{sorted(LABEL_KEYS)} 뿐이다")
            continue
        text = str(item.get("text") or "").strip()
        anchor = item.get("span_id")
        if not text:
            notes.append(f"{where} labels[{i}]: 빈 라벨 — 버렸다")
            continue
        if not isinstance(anchor, str) or anchor not in span_ids:
            problems.append(f"{where} labels[{i}]: 앵커 {anchor!r} 가 이 편의 span 이 "
                            f"아니다 — 재료 목록의 id 로만 앵커하라")
            continue
        if not (text.startswith("(") and text.endswith(")")):
            # v3 `validate_story_response` 와 같은 보정·같은 기록(괄호는 규칙 5 의 형식이다).
            notes.append(f"{where} labels[{i}] 라벨 괄호 보정: {text!r}")
            text = f"({text.strip('()')})"
        out.append({"text": text, "span_id": anchor})
    return out


def validate_flesh_response(resp: Any, *, span_ids: set[str], n_beats: int,
                            required_roles: Sequence[str] = (),
                            title_max: int = TITLE_MAX_CHARS,
                            ) -> tuple[dict | None, list[str]]:
    """→ (story 문서 조각 | None, 반려 사유). 순수·결정적.

    검사(계약 §2):
      · **제목 2줄·자수 strict** — 여기서는 가안이 아니라 확정이다. 넘치면 반려한다
        (6단계는 같은 위반을 잘라내고 노트로 넘긴다 — 자리가 다르다).
      · **내레이션 문장 배열** — 배열이 계약이고, 자수 초과는 노트다(위 `_norm_narration`).
      · **라벨 앵커가 그 편의 span 안** — 밖이면 반려(자리를 지어내지 않는다).
      · **모르는 열쇠 반려** — 응답 세 열쇠·비트 네 열쇠·라벨 두 열쇠 밖은 전부.
      · 템플릿 **필수 역할**(`STORY_TEMPLATE_SPECS[...]['required_roles']`) 존재 —
        v3 `validate_story_response` 와 같은 규칙·같은 반려 문구 형식이다.

    반환 dict: `{"title", "reason", "beats": [{number, role, narration, labels}], "notes"}`.
    비트는 **번호 순**으로 정렬해 돌려준다(결정성 — 모델이 순서를 섞어 내도 산출이 같다).

    ⚠ 라벨 상한 초과는 **반려가 아니라 앞에서부터 남기고 자른다**(E15 하드캡 규율:
    조용한 절단 금지 · 자른 사실은 notes 에). 하한 미달은 노트뿐이다 — 없는 라벨을 우리가
    지어낼 수는 없고, 라벨이 하나 적다고 승인된 편을 버릴 이유도 없다."""
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(resp, dict):
        return None, [f"응답이 객체가 아니다: {type(resp).__name__} — "
                      f"계약은 {{title, reason, beats}} 하나다"]

    unknown = sorted(k for k in resp if k not in RESPONSE_KEYS)
    if unknown:
        problems.append(f"모르는 열쇠 {unknown} — 허용은 {sorted(RESPONSE_KEYS)} 뿐이다"
                        "(효과 문구·스티커·자막 강조는 11:style 이 정한다)")

    # ── 제목 (strict) ──
    title = resp.get("title")
    line1 = line2 = ""
    if not isinstance(title, dict):
        problems.append(f"title 은 {{line1, line2}} 객체여야 한다({type(title).__name__})")
    else:
        t_unknown = sorted(k for k in title if k not in TITLE_KEYS)
        if t_unknown:
            problems.append(f"title 에 모르는 열쇠 {t_unknown} — {sorted(TITLE_KEYS)} 뿐이다")
        line1 = str(title.get("line1") or "").strip()
        line2 = str(title.get("line2") or "").strip()
        for name, line in (("line1", line1), ("line2", line2)):
            if not line:
                problems.append(f"title.{name} 이 비었다 — 두 줄 모두 필요하다")
            elif len(line) > title_max:
                problems.append(f"title.{name} 이 {len(line)}자 — {title_max}자 이내로 "
                                f"(확정 제목이라 잘라내지 않는다): {line!r}")

    # ── 비트 ──
    beats_in = resp.get("beats")
    beats: list[dict] = []
    if not isinstance(beats_in, list) or not beats_in:
        problems.append("beats 배열이 없다 — 비트마다 역할·내레이션·라벨을 답하라")
        beats_in = []
    seen: set[int] = set()
    for k, b in enumerate(beats_in):
        where = f"beats[{k}]"
        if not isinstance(b, dict):
            problems.append(f"{where} 가 객체가 아니다({type(b).__name__})")
            continue
        b_unknown = sorted(key for key in b if key not in BEAT_KEYS)
        if b_unknown:
            problems.append(f"{where}: 모르는 열쇠 {b_unknown} — {sorted(BEAT_KEYS)} 뿐이다")
            continue
        number = b.get("number")
        if isinstance(number, bool) or not isinstance(number, int) \
                or not 0 <= number < int(n_beats):
            problems.append(f"{where}: number 는 0~{int(n_beats) - 1} 의 비트 번호여야 "
                            f"한다: {number!r}")
            continue
        if number in seen:
            # 같은 비트를 두 번 답하면 어느 쪽 내레이션이 화면에 나가는지 우리가 고르게 된다.
            problems.append(f"{where}: 비트 {number} 가 두 번 나왔다 — 비트당 한 번만")
            continue
        seen.add(number)
        role = str(b.get("role") or "").strip()
        if not role:
            problems.append(f"{where}: role 이 비었다 — 템플릿의 역할 이름을 붙여라")
            continue
        where = f"beats[{number}]"
        beats.append({
            "number": number, "role": role,
            "narration": _norm_narration(b.get("narration"), where=where,
                                         problems=problems, notes=notes),
            "labels": _norm_labels(b.get("labels"), where=where, span_ids=set(span_ids),
                                   problems=problems, notes=notes),
        })

    beats.sort(key=lambda x: x["number"])

    for need in required_roles or ():
        if beats and not any(b["role"] == need for b in beats):
            problems.append(f"{need} 비트가 하나 필요하다 — 템플릿이 요구하는 역할이다")

    if problems:
        return None, problems

    # ── 라벨 편 전체 상한(E15 규율) ──
    total = sum(len(b["labels"]) for b in beats)
    if total > LABELS_PER_EPISODE[1]:
        left = LABELS_PER_EPISODE[1]
        for b in beats:
            keep = b["labels"][:max(0, left)]
            for dropped in b["labels"][len(keep):]:
                notes.append(f"beats[{b['number']}] 라벨 상한 초과로 버렸다: "
                             f"{dropped['text']!r} @{dropped['span_id']} "
                             f"(편 전체 {total} > {LABELS_PER_EPISODE[1]})")
            b["labels"] = keep
            left -= len(keep)
    elif total < LABELS_PER_EPISODE[0]:
        notes.append(f"라벨이 {total}개 — 규칙 5 의 하한({LABELS_PER_EPISODE[0]}개)에 "
                     f"못 미친다(지어내지 않고 그대로 간다)")

    return {"title": {"line1": line1, "line2": line2},
            "reason": str(resp.get("reason") or "").strip(),
            "beats": beats, "notes": notes}, []


# ── 예산 다듬기 — 역할 보호를 템플릿에 맞춘다 ───────────────────────────────

def protected_roles(template: str) -> tuple[str, ...]:
    """이 템플릿에서 **통삭제 보호**를 받아야 하는 역할. 순수.

    정본은 레지스트리 `STORY_TEMPLATE_SPECS[...]['required_roles']` 다 — 목록을 여기서
    새로 적으면 템플릿이 늘 때 한쪽만 고쳐진다. `"climax"` 는 항상 넣는다(v3 가 하드코딩
    으로 보호하던 그 이름이고, recap_dialogue 의 required_roles 이기도 하다)."""
    spec = STORY_TEMPLATE_SPECS.get(template)
    if spec is None:
        raise ValueError(f"모르는 스토리 템플릿 {template!r} — 사용 가능: "
                         f"{sorted(STORY_TEMPLATE_SPECS)}")
    return tuple(dict.fromkeys((PROTECTED_ROLE_CANON, *(spec.get("required_roles") or ()))))


def trim_to_budget_by_role(beats: list[dict], span_index: dict[str, dict],
                           arousal: list[dict], max_sec: float, *, template: str,
                           ) -> tuple[list[dict], list[dict], list[str]]:
    """예산 다듬기 → (다듬은 비트 사본, 제거 로그, 보호를 위해 갈아 끼운 역할). 순수.

    🛑 v3 `trim_to_budget` 을 **그대로 부른다**(계약 §2). 다만 그 함수는 보호 대상을
    `role == "climax"` 로 하드코딩하므로(모듈 독스트링 §🛑), 이 템플릿의 필수 역할을
    잠깐 `"climax"` 로 갈아 끼운 **사본 뷰**를 넘기고 돌아온 뒤 이름을 되돌린다.

    · 순수하다 — 넘겨받은 `beats` 를 제자리에서 고치지 않는다(v3 함수는 인자를 고친다).
    · 되돌린 뒤의 역할 이름은 입력과 **글자까지 같다**(테스트가 고정한다).
    """
    prot = set(protected_roles(template))
    view = copy.deepcopy(list(beats))
    renamed: list[str] = []
    original: list[str] = []
    for b in view:
        role = str(b.get("role") or "")
        original.append(role)
        if role in prot and role != PROTECTED_ROLE_CANON:
            b["role"] = PROTECTED_ROLE_CANON
            renamed.append(role)
    removed = story_mod.trim_to_budget(view, span_index, arousal, max_sec)
    for b, role in zip(view, original):
        b["role"] = role
    return view, removed, sorted(set(renamed))


# ── 유튜브 설명·해시태그 (별도 텍스트 호출 — 기획서 §3) ────────────────────

DESCRIPTION_PROMPT = """아래 쇼츠 한 편의 **유튜브 발행 메타**를 써라. 편집은 이미 끝났다 — 화면에 나가는 제목과 그 편이 담은 대사·내레이션이 재료 전부다.

## 작품
{work_title}{research_block}

## 이 편
제목: {line1} / {line2}
{beat_block}

## 규칙
1. `description` 은 **{desc_max}자 이내**, 3~4문장. 첫 문장이 미리보기에 걸리므로 제목이 약속한 사건을 그대로 적어라. **위 재료에 없는 인물·사건·수치를 지어내지 마라** — 없는 사실이 발행 메타에 들어가면 화면과 설명이 어긋난다.
2. `hashtags` 는 **{tag_min}~{tag_max}개**, `#` 로 시작하는 붙여 쓴 한 낱말. 작품명 → 인물명 → 상황 순.
3. 두 열쇠만 답하라 — 그 밖의 열쇠가 있으면 반려된다.

## 출력 (JSON 만)
{{"description": "…", "hashtags": ["#작품명", "#인물명"]}}"""


def _beat_block(story_doc: dict) -> str:
    """설명 호출의 재료 — 비트별 역할·내레이션·라벨. 순수.

    ⚠ 대사 전문을 다시 싣지 않는다(살붙이기 프롬프트가 이미 그 자리를 치렀다). 설명은
    '무슨 일이 있었나'만 알면 되고, 재료가 커지면 편당 두 번째 콜의 값이 그만큼 는다."""
    lines: list[str] = []
    for b in story_doc.get("beats") or []:
        nar = " / ".join(b.get("narration") or [])
        labels = " ".join(x.get("text", "") for x in (b.get("labels") or []))
        bits = [f"- {b.get('role')}"]
        if nar:
            bits.append(f"내레이션: {nar}")
        if labels:
            bits.append(f"라벨: {labels}")
        lines.append(" · ".join(bits))
    return "\n".join(lines) if lines else "- (비트 기록 없음)"


def build_description_prompt(story_doc: dict, *, work_title: str,
                             research_context: str = "") -> str:
    """설명·해시태그 프롬프트. 순수.

    🛑 **살붙이기와 나눈 호출이다**(기획서 §3 · v4-plan §5 v5 항목 "설명 별도 호출").
    한 응답에 합치면 발행 메타 형식 하나가 틀렸을 때 그 편의 **카피 전체가 반려**된다 —
    story 검증기를 건드리지 않으려는 분리다."""
    title = story_doc.get("title") or {}
    return DESCRIPTION_PROMPT.format(
        work_title=work_title, research_block=_research_block(research_context),
        line1=str(title.get("line1") or ""), line2=str(title.get("line2") or ""),
        beat_block=_beat_block(story_doc),
        desc_max=DESCRIPTION_MAX_CHARS,
        tag_min=HASHTAGS_PER_EPISODE[0], tag_max=HASHTAGS_PER_EPISODE[1])


def validate_description_response(resp: Any) -> tuple[dict | None, list[str]]:
    """→ ({description, hashtags} | None, 반려 사유). 순수·결정적.

    · 설명이 비었거나 상한을 넘으면 **반려**한다(잘라내면 문장이 중간에서 끊긴 채 발행된다).
    · 해시태그는 보정한다 — `#` 이 없으면 붙이고, 공백은 없애고, 중복은 지우고, 상한을
      넘으면 앞에서부터 남긴다. 전부 노트로 남긴다(조용한 보정 금지).
    · 하한 미달은 노트뿐이다(지어내지 않는다)."""
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(resp, dict):
        return None, [f"응답이 객체가 아니다: {type(resp).__name__} — "
                      f"계약은 {{description, hashtags}} 하나다"]
    unknown = sorted(k for k in resp if k not in DESCRIPTION_KEYS)
    if unknown:
        problems.append(f"모르는 열쇠 {unknown} — {sorted(DESCRIPTION_KEYS)} 뿐이다")

    desc = resp.get("description")
    if not isinstance(desc, str) or not desc.strip():
        problems.append("description 이 비었다 — 3~4문장으로 써라")
        desc = ""
    else:
        desc = desc.strip()
        if len(desc) > DESCRIPTION_MAX_CHARS:
            problems.append(f"description 이 {len(desc)}자 — {DESCRIPTION_MAX_CHARS}자 "
                            f"이내로(잘라내면 문장이 중간에서 끊긴다)")

    tags_raw = resp.get("hashtags")
    tags: list[str] = []
    if tags_raw is None:
        problems.append("hashtags 가 없다 — 빈 배열이라도 답하라")
    elif not isinstance(tags_raw, list):
        problems.append(f"hashtags 는 배열이어야 한다({type(tags_raw).__name__})")
    else:
        for i, item in enumerate(tags_raw):
            if not isinstance(item, str):
                problems.append(f"hashtags[{i}] 가 문자열이 아니다({type(item).__name__})")
                continue
            tag = "".join(item.split())          # 유튜브 해시태그에 공백은 없다
            if not tag.strip("#"):
                notes.append(f"hashtags[{i}]: 빈 태그 — 버렸다")
                continue
            if not tag.startswith("#"):
                notes.append(f"hashtags[{i}] '#' 보정: {item!r}")
                tag = "#" + tag
            if tag in tags:
                notes.append(f"hashtags[{i}] 중복 제거: {tag!r}")
                continue
            tags.append(tag)

    if problems:
        return None, problems
    if len(tags) > HASHTAGS_PER_EPISODE[1]:
        for tag in tags[HASHTAGS_PER_EPISODE[1]:]:
            notes.append(f"해시태그 상한 초과로 버렸다: {tag}")
        tags = tags[:HASHTAGS_PER_EPISODE[1]]
    elif len(tags) < HASHTAGS_PER_EPISODE[0]:
        notes.append(f"해시태그가 {len(tags)}개 — 하한({HASHTAGS_PER_EPISODE[0]}개)에 "
                     f"못 미친다(지어내지 않고 그대로 간다)")
    return {"description": desc, "hashtags": tags, "notes": notes}, []


def run_description(gemini: Any, story_doc: dict, *, work_title: str,
                    research_context: str = "", log=print) -> tuple[dict, dict]:
    """설명·해시태그 1콜(+재질의 ≤MAX_REASKS) → (`{description, hashtags}`, audit).

    🛑 **실패해도 그 편을 죽이지 않는다.** 발행 메타가 없는 편은 사람이 채울 수 있지만,
    영상이 없는 편은 결번이다. 실패는 `audit["status"]="failed"` 와 사유로 남고 부르는
    쪽(`run_flesh`)이 stdout·run_log 에 싣는다(조용한 누락 금지)."""
    cid = str(story_doc.get("candidate_id") or "")
    audit: dict[str, Any] = {"id": cid, "attempts": [], "status": "ok"}
    reject_note = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = build_description_prompt(
            story_doc, work_title=work_title, research_context=research_context)
        if reject_note:
            prompt += ("\n\n## ⚠ 직전 응답 반려 사유 — 전부 고쳐서 다시 내라\n"
                       + reject_note)
        t0 = time.time()
        usage: dict | None = None
        try:
            resp, usage = _call_text(gemini, prompt,
                                     max_output_tokens=FLESH_MAX_OUTPUT_TOKENS, log=log)
        except video_mod.VideoParseError as e:
            usage = e.usage
            meta, problems = None, [f"응답 JSON 파싱 실패: {e}"]
        except video_mod.VideoCallError as e:
            audit["attempts"].append({"attempt": attempt + 1,
                                      "elapsed_sec": round(time.time() - t0, 3),
                                      "problems": [str(e)], "usage": None})
            audit["status"] = "failed"
            audit["reason"] = REASON_CALL_FAILED
            audit["detail"] = str(e)
            return {}, audit
        else:
            meta, problems = validate_description_response(resp)
        audit["attempts"].append({
            "attempt": attempt + 1, "elapsed_sec": round(time.time() - t0, 3),
            "prompt_sha": prompt_sha(prompt), "problems": problems, "usage": usage,
            "accepted": meta is not None})
        if meta is not None:
            audit["notes"] = meta.pop("notes", [])
            return meta, audit
        reject_note = "\n".join(f"- {p}" for p in problems[:20])

    audit["status"] = "failed"
    audit["reason"] = REASON_REASK_EXHAUSTED
    audit["detail"] = "; ".join(
        p for rec in audit["attempts"] for p in rec.get("problems") or [])[:800]
    return {}, audit


# ── 편 하나 살붙이기 ────────────────────────────────────────────────────────

def _failed(cid: str, reason: str, detail: str, **extra: Any) -> dict:
    """편별 탈락 기록. 다른 편을 되돌리지 않는다(계약 §2 · 기획서 §7)."""
    return {"id": cid, "status": "failed", "reason": reason, "detail": detail, **extra}


def _flesh_one(gemini: Any, cand: dict, *, grid: dict, source_duration_sec: float,
               work_title: str, research_context: str, target_sec: float,
               max_sec: float, detail: dict | None, with_description: bool,
               log=print) -> dict:
    """편 하나 → 감사 기록(성공이면 `["doc"]` 에 story 문서). 다른 편과 독립이다.

    🛑 다리는 **여기 입구 한 곳**에서 건넌다(계약 §4). 후보마다 인용 span 이 다르므로
    `span_index` 도 편마다 새로 짓는다 — 하나를 여러 편이 나눠 쓰면 A 가 인용한 span 이
    B 에서도 `QUOTE_IMPORTANCE` 로 올라가 B 의 내레이션 뮤트가 조용히 막힌다."""
    cid = candidate_id(cand)
    template = str(cand.get("template") or "")
    rec: dict[str, Any] = {"id": cid, "template": template, "attempts": []}
    if template not in STORY_TEMPLATE_SPECS:
        # 6단계가 이미 검증한 값이라 정상 경로에서는 오지 않는다 — 구 체크포인트·편집실
        # 재개처럼 그 검증을 안 지난 후보가 있을 수 있다. 프롬프트 조립(`_template_block`
        # ·`_required_roles_text`)이 크게 실패하도록 두면 **다른 편까지 죽으므로**, 그 편만
        # 탈락시키고 사유를 남긴다(전량이 이러면 위에서 크게 죽는다).
        return _failed(cid, REASON_UNKNOWN_TEMPLATE,
                       f"모르는 스토리 템플릿 {template!r} — 사용 가능: "
                       f"{sorted(STORY_TEMPLATE_SPECS)}",
                       template=template, attempts=[])

    crossed = bridge.cross(cand, grid=grid, source_duration_sec=source_duration_sec,
                           detail=detail)
    if crossed["beats"] is None:
        # `spans_for` 가 남긴 기록이 사유다 — 조각이 격자 눈금보다 짧아 빈 비트가 된다.
        missing = crossed["audit"]["spans_missing"]
        return _failed(cid, REASON_NO_SPANS,
                       f"조각 {len(missing)}개가 span 을 못 잡았다: "
                       + "; ".join(f"조각{m['segment']} {m['window']}" for m in missing[:5]),
                       template=template, bridge=crossed["audit"], attempts=[])

    span_index = crossed["span_index"]
    span_ids = crossed["span_ids"]
    all_spans = {sid for ids in span_ids for sid in ids}
    need = tuple((STORY_TEMPLATE_SPECS.get(template) or {}).get("required_roles") or ())

    story_part: dict | None = None
    reject_note = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = build_flesh_prompt(
            work_title=work_title, candidate=cand, span_index=span_index,
            span_ids=span_ids, research_context=research_context, template=template,
            target_sec=target_sec, max_sec=max_sec, reject_note=reject_note)
        t0 = time.time()
        usage: dict | None = None
        try:
            resp, usage = _call_text(gemini, prompt,
                                     max_output_tokens=FLESH_MAX_OUTPUT_TOKENS, log=log)
        except video_mod.VideoParseError as e:
            # 파싱 실패는 이 레포 실측의 상시 모드다 — 크래시가 아니라 **반려 재료**다.
            usage = e.usage
            got, problems = None, [f"응답 JSON 파싱 실패: {e}"]
        except video_mod.VideoCallError as e:
            # 호출 자체가 죽었다 — 같은 프롬프트를 다시 보내도 낫지 않는다(E11 분류·재시도는
            # `_call_text` 안에서 이미 끝났다). 재질의를 태우지 않고 그 편만 탈락시킨다.
            rec["attempts"].append({"attempt": attempt + 1,
                                    "elapsed_sec": round(time.time() - t0, 3),
                                    "problems": [str(e)], "usage": None,
                                    "accepted": False})
            return _failed(cid, REASON_CALL_FAILED, str(e), template=template,
                           bridge=crossed["audit"], attempts=rec["attempts"])
        else:
            got, problems = validate_flesh_response(
                resp, span_ids=all_spans, n_beats=len(span_ids),
                required_roles=need)
        rec["attempts"].append({
            "attempt": attempt + 1, "elapsed_sec": round(time.time() - t0, 3),
            "prompt_sha": prompt_sha(prompt), "prompt_chars": len(prompt),
            "problems": problems, "usage": usage, "accepted": got is not None})
        if got is not None:
            story_part = got
            break
        log(f"  [v4/flesh] {cid} 반려 — 사유 {len(problems)}건")
        for p in problems[:20]:
            log(f"    · {p}")
        reject_note = "\n".join(f"- {p}" for p in problems[:20])

    if story_part is None:
        detail_text = "; ".join(
            p for a in rec["attempts"] for p in (a.get("problems") or []))[:800]
        return _failed(cid, REASON_REASK_EXHAUSTED,
                       f"재질의 {MAX_REASKS}회 소진 — {detail_text}",
                       template=template, bridge=crossed["audit"],
                       attempts=rec["attempts"])

    notes = list(story_part.get("notes") or [])

    # ── 카피를 비트 뼈대에 얹는다 ──
    beats = copy.deepcopy(crossed["beats"])
    by_number = {b["number"]: b for b in story_part["beats"]}
    owner_of = {sid: bi for bi, ids in enumerate(span_ids) for sid in ids}
    for bi, beat in enumerate(beats):
        part = by_number.get(bi)
        if part is None:
            continue                       # 내레이션·라벨 없는 비트 — 정상(예: climax)
        beat["role"] = part["role"]
        beat["narration"] = list(part["narration"])
    # 라벨은 **앵커가 사는 비트**에 붙인다. 모델이 다른 비트에 적어 냈어도 자리는 앵커가
    # 정한다(하류가 `span_id` 의 시각에 그린다) — 옮긴 사실은 남긴다.
    for beat in beats:
        beat["labels"] = []
    for part in story_part["beats"]:
        for label in part["labels"]:
            owner = owner_of[label["span_id"]]
            if owner != part["number"]:
                notes.append(f"라벨 {label['text']!r} 을 비트 {part['number']} → "
                             f"{owner} 로 옮겼다(앵커 {label['span_id']} 가 그 비트의 span)")
            beats[owner]["labels"].append(label)

    # ── 길이 예산 → 슬롯 배치 → 충돌 벨트 (v3 함수 그대로) ──
    arousal = grid.get("arousal") or []
    total_before = story_mod.story_duration(beats, span_index)
    beats, removed, renamed = trim_to_budget_by_role(
        beats, span_index, arousal, max_sec, template=template)
    for beat in beats:                               # 방어 — 통삭제는 규칙상 없다
        if not beat["span_ids"] and (beat.get("narration") or beat.get("labels")):
            notes.append(f"비트 {beat['role']!r} 가 예산 다듬기로 통째로 비었다 — "
                         f"거기 실린 카피도 함께 사라진다(내레이션 "
                         f"{len(beat.get('narration') or [])}문장 · "
                         f"라벨 {len(beat.get('labels') or [])}개)")
    beats = [b for b in beats if b["span_ids"]]
    # 🛑 라벨은 예산 다듬기 **전**에 붙었다 — 앵커 span 이 깎여 나갔으면 그 라벨은 화면에
    # 없는 시각을 가리킨다(하류는 `span_id` 의 시각에 그린다). 조용히 두면 M6 가 타임라인
    # 밖에 라벨을 그리거나 KeyError 로 죽는다. 여기서 떼고 **건별로 기록**한다.
    for beat in beats:
        alive = set(beat["span_ids"])
        kept = [x for x in beat["labels"] if x["span_id"] in alive]
        for gone in beat["labels"]:
            if gone["span_id"] not in alive:
                notes.append(f"라벨 {gone['text']!r} 을 뗐다 — 앵커 {gone['span_id']} 가 "
                             f"예산 다듬기로 빠졌다(화면에 없는 시각을 가리킨다)")
        beat["labels"] = kept
    total_after = story_mod.story_duration(beats, span_index)
    budget_unmet = total_after > max_sec

    cues, dropped = story_mod.plan_narration_slots(beats, span_index)
    conflicts = story_mod.verify_tts_conflicts(cues, beats, span_index)
    if conflicts:
        # 🛑 배치 규칙상 나올 수 없다 — 나오면 **우리 코드의 결함**이다(v3 `run_story` 와
        # 같은 규율). 편별 격리로 삼키지 않는다: 조용한 송출보다 크게 죽는 것이 낫다.
        raise AssertionError(f"[{cid}] TTS-대사 충돌 벨트 위반: {conflicts}")

    doc: dict[str, Any] = {
        "schema": SCHEMA_FLESH,
        "candidate_id": cid,                 # v4 가산 — 어느 후보에서 나온 편인가(§4)
        "template": template,
        "reason": story_part["reason"],
        "title": story_part["title"],
        # 비트 문서 표기는 v3 `_beat_doc` 그대로다(시각은 격자 조회 — 원본 절대초).
        "beats": story_mod._beat_doc(beats, span_index),
        "narration_cues": cues,
        "narration_dropped": dropped,
        "budget": {"target_sec": target_sec, "max_sec": max_sec,
                   "total_before_sec": round(total_before, 3),
                   "total_after_sec": round(total_after, 3),
                   "removed": removed, "unmet": budget_unmet},
        # 다리가 만든 재료 — M6(11:resources)이 조립·자막·TTS 에 그대로 쓴다.
        "segments": crossed["segments"],
        "span_ids": [list(ids) for ids in span_ids],
    }

    if with_description:
        meta, meta_audit = run_description(gemini, doc, work_title=work_title,
                                           research_context=research_context, log=log)
        rec["description"] = meta_audit
        if meta:
            # 목적지는 `edit_plan.layout.description / hashtags` 가산 키(계약 §2) —
            # 열쇠 이름을 그 자리와 같게 둔다(M6 가 그대로 옮긴다).
            doc["description"] = meta["description"]
            doc["hashtags"] = meta["hashtags"]
        else:
            log(f"  [v4/flesh] ⚠ {cid} 발행 메타 실패 — {meta_audit.get('reason')}: "
                f"{str(meta_audit.get('detail'))[:200]} (영상은 그대로 낸다)")

    rec.update({
        "status": "ok",
        "doc": doc,
        "bridge": crossed["audit"],
        "notes": notes,
        "protected_roles": list(protected_roles(template)),
        "roles_renamed_for_trim": renamed,
        "beats_count": len(beats),
        "narration_cues": len(cues),
        "narration_dropped": len(dropped),
        "labels": sum(len(b.get("labels") or []) for b in beats),
        "budget_unmet": budget_unmet,
        "trimmed_spans": len(removed),
    })
    return rec


# ── 실행 ────────────────────────────────────────────────────────────────────

def run_flesh(gemini: Any, handle: Any, approved: Sequence[dict], *, grid: dict,
              work_title: str, research: Any = None,
              source_duration_sec: float | None = None,
              target_sec: float = story_mod.STORY_TARGET_SEC,
              max_sec: float = story_mod.STORY_MAX_SEC,
              detail: dict | None = None, with_description: bool = True,
              concurrency: int = FLESH_CONCURRENCY,
              log=print) -> tuple[dict, dict]:
    """승인 편마다 살붙이기(병렬) → (`{cand_id: story_doc}`, audit).

    · 실패는 **그 편만** 탈락 + 기록(다른 편을 되돌리지 않는다).
    · **전량 실패면 크게 실패**한다 — 승인이 있었는데 낼 것이 없다 = 조용한 결번.
    · 결과는 **후보 id 정렬 순서**로 담는다(결정성 — 저장 파일이 실행마다 달라지면 안 된다).
      제출도 같은 순서다. 병렬이라 완료 순서는 정해지지 않으므로 담을 때 다시 정렬한다
      (8단계 `run_flags` 와 같은 규약).
    · 슬롯 배치·예산·충돌 벨트는 v3 것을 그대로 부른다(`plan_narration_slots` ·
      `trim_to_budget`(역할 보호 뷰) · `verify_tts_conflicts`).

    🛑 `handle` 은 이 판에서 **쓰지 않는다** — 10단계는 텍스트 온리다(기획서 §4 비용표:
    편당 10,000 토큰). 계약 §2 시그니처가 잡아 둔 자리라 지우지 않는다: 영상을 다시 보는
    10a(정밀 청취)와 같은 자리에서 같은 핸들을 받아야 배선이 갈리지 않는다.

    ⚠ 계약 §2 시그니처와 다른 곳 둘(의도적):
      ① `span_index` 를 **받지 않는다** — 인용 보호(`QUOTE_IMPORTANCE`)가 후보마다 다른
         span 에 걸리므로 편마다 새로 지어야 한다(`_flesh_one` 독스트링). 대신 `grid` 와
         선택적 `detail`(10a 산출)을 받아 다리를 편마다 건넌다.
      ② `templates` 를 받지 않는다 — 템플릿은 후보가 이미 들고 있고(6단계가 검증했다),
         여기서 다시 허용 목록을 받으면 승인된 편을 템플릿 이름으로 또 떨어뜨리게 된다.
    """
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError(f"concurrency 는 1 이상의 정수여야 한다: {concurrency!r}")
    if source_duration_sec is None:
        try:
            source_duration_sec = float((grid.get("source") or {})["duration_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "소스 길이를 알 수 없다 — `grid['source']['duration_sec']` 가 있어야 "
                "다리의 눈금 범위가 정해진다") from exc

    ordered = sorted(approved or [], key=candidate_id)
    ids = [candidate_id(c) for c in ordered]
    dupes = sorted({cid for cid in ids if ids.count(cid) > 1})
    if dupes:
        raise ValueError(f"승인 후보 id 가 중복이다: {dupes} — id 는 6단계가 c%02d 로 부여한다")
    research_context = _research_text(research)

    def _one(cand: dict) -> dict:
        return _flesh_one(gemini, cand, grid=grid,
                          source_duration_sec=float(source_duration_sec),
                          work_title=work_title, research_context=research_context,
                          target_sec=target_sec, max_sec=max_sec, detail=detail,
                          with_description=with_description, log=log)

    t0 = time.time()
    records: dict[str, dict] = {}
    if ordered:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(ordered)),
                                thread_name_prefix="v4-flesh") as pool:
            for rec in pool.map(_one, ordered):
                records[rec["id"]] = rec

    docs: dict[str, dict] = {}
    rows: list[dict] = []
    usage_total = 0
    for cid in ids:                                   # id 정렬 순서 그대로
        rec = records[cid]
        row = {k: v for k, v in rec.items() if k != "doc"}
        rows.append(row)
        for att in rec.get("attempts") or []:
            total = (att.get("usage") or {}).get("total")
            usage_total += total if isinstance(total, int) else 0
        for att in (rec.get("description") or {}).get("attempts") or []:
            total = (att.get("usage") or {}).get("total")
            usage_total += total if isinstance(total, int) else 0
        if rec.get("status") == "ok":
            docs[cid] = rec["doc"]
            log(f"  [v4/flesh] {cid} 완성 — 제목 {rec['doc']['title']['line1']!r} / "
                f"{rec['doc']['title']['line2']!r} · 비트 {rec['beats_count']} · "
                f"내레이션 {rec['narration_cues']}(드랍 {rec['narration_dropped']}) · "
                f"라벨 {rec['labels']}")
            for note in rec.get("notes") or []:
                log(f"    · {note}")
        else:
            log(f"  [v4/flesh] ⚠ {cid} 탈락 — {rec.get('reason')}: "
                f"{str(rec.get('detail'))[:300]}")

    audit = {
        "of": len(ids),
        "ok": len(docs),
        "failed": len(ids) - len(docs),
        "concurrency": concurrency,
        "max_output_tokens": FLESH_MAX_OUTPUT_TOKENS,
        "with_description": bool(with_description),
        "detail_spans": len(detail or {}),
        "target_sec": target_sec, "max_sec": max_sec,
        "usage_total": usage_total,
        "elapsed_sec": round(time.time() - t0, 3),
        "episodes": rows,
    }
    if ids and not docs:
        # 🛑 조용한 결번 금지(모듈 독스트링 규율 3) — 승인이 있었는데 낼 것이 없다.
        raise ValueError(
            f"10단계 살붙이기가 승인 {len(ids)}편 전량 실패했다 — 낼 편이 없다.\n"
            + "\n".join(f"  {r['id']}: {r.get('reason')} — {str(r.get('detail'))[:200]}"
                        for r in rows))
    log(f"  [v4/flesh] 살붙이기 {len(docs)}/{len(ids)}편 · 동시 {concurrency} · "
        f"토큰 {usage_total} · {audit['elapsed_sec']:.1f}s")
    return docs, audit


def _research_text(research: Any) -> str:
    """리서치 → 프롬프트에 실을 한 덩어리. 순수.

    ⚠ 열쇠는 6단계 `candidates._research_context` 가 읽는 것과 같다(`work_context` ·
    `episodes_context`) — 두 단계가 다른 열쇠를 읽으면 한쪽만 리서치를 못 본다."""
    if isinstance(research, str):
        return research.strip()
    r = research or {}
    if not isinstance(r, dict):
        return ""
    parts = [str(r.get("work_context") or "").strip(),
             str(r.get("episodes_context") or "").strip()]
    return "\n".join(p for p in parts if p)
