"""6단계 후보 편성 — v4 의 입구. 영상 전체를 한 번 보고 **얇은 후보**를 여럿 받는다.

계약 정본 `docs/v4/M3-interfaces.md` §2 · 기획 `docs/v4/v4-plan.md` §6(6 후보 편성).

    후보 하나 = {id, template, segments[{start_sec, end_sec, quote}], reason, title_draft}

여기서 **제목 본문·내레이션·라벨을 쓰지 않는다**(운영자 결정 O4). 후보마다 다 쓰면
출력이 잘리고(v3-M2 실측: thinking 이 출력 예산을 먹어 JSON 절단 → 파싱 실패 반려),
탈락할 후보의 출력 단가(입력의 5배)를 그대로 태운다. 살붙이기는 10단계 몫이다.

## v3 프롬프트를 그대로 못 쓰는 이유 (계약 §0)

v3 `story.PROMPT_TEMPLATE` 은 **span id 로만 말하고** 재료가 `stage2_doc`(청크 상세
분석)이다. v4 는 그 단계를 없앴고 모델이 **절대초 + 인용**을 낸다 — 재료도 좌표도 다르다.
그래서 프롬프트는 새로 쓰되 **문구 자산은 v3 에서 가져온다**(실측이 밴 문장들):

  · 규칙 7 서론 금지            — v3 PROMPT_TEMPLATE 7번
  · 규칙 8 대사 신뢰 어휘        — v3 PROMPT_TEMPLATE 8번(v4 판 각색은 아래 ⚠)
  · 템플릿 설명                  — `story.STORY_TEMPLATE_SPECS` 의 desc(추가 2종은 import,
                                   기본 2종은 v3 가 프롬프트 본문에 박아 둬 import 할 것이
                                   없다 — `TEMPLATE_BRIEFS` 에 v4 용으로 다시 적었다)

⚠ **규칙 4·5·6(내레이션 자수·라벨 앵커·제목 확정)은 싣지 않는다** — 10단계 몫이다.
싣는 순간 모델이 후보 16개마다 내레이션을 지어 오고, 그 토큰은 전부 버려진다.
`tests/test_v4_candidates.py` 가 그 어휘가 **안 실렸는지**를 고정한다.

⚠ **규칙 8 의 어휘 세 개 중 v4 에 있는 것은 `[저확신]` 하나다.** `[청취]`·`[대사없음]`
은 Stage 2(청크 분석)가 붙이던 표지인데 v4 에는 그 단계가 없다. 없는 표지를 프롬프트에
적으면 모델은 전사에서 그것을 찾다가 못 찾고, 규칙 자체를 무시한다. 그래서 `[저확신]`
(단어 확률 평균으로 여기서 붙인다)만 남기고 "전사에 줄이 없는 시각대 = 대사 없는 구간"
으로 나머지를 대신했다.

## 이 단계의 규율

- **quote 는 전사에서 그대로**여야 한다. 6c(`app/v4/verify.py`)가 그 글자로 조각의 시각을
  검증하는데, 모델이 다듬으면 정확 대조를 놓치고 관용 대조(편집거리 0.35)로 떨어지며,
  그마저 넘으면 **환각으로 드롭**된다. 프롬프트에서 가장 강하게 적은 지시가 이것이다.
- **검증기는 하나가 걸려도 그 후보만 버린다.** 전량이 걸리거나(=응답이 못 쓰게 왔거나)
  살아남은 수가 하한에 못 미칠 때만 반려·재질의한다.
- **제목 자수 초과는 반려가 아니라 잘라내고 노트**다(계약 §2). 가안이라 10단계가 strict
  로 다시 걸고, 여기서 반려하면 제목 한 줄 때문에 후보 16개가 통째로 날아간다.
- **반려 소진 = 편 전체 실패**(기획서 §7). 시각 정본의 입구라 조용히 통과시키지 않는다.

🛑 **이 판은 실호출로 검증되지 않았다** — 이 워크트리에 `GEMINI_API_KEY` 가 없다.
프롬프트 품질(모델이 실제로 quote 를 그대로 옮기는가 · 후보 5~16개가 정말 다른 아크로
오는가)은 키가 있는 노드의 몫이다. 코드가 고정하는 것은 **구조**뿐이다.
"""
from __future__ import annotations

import bisect
import hashlib
import math
import time
from typing import Any

from app.modules.grid.schemas import EXCEPTION_KEYS, format_ts
# 반려 상한은 **v3 의 것을 그대로 쓴다**(계약 §2 "재선언 금지"). 격자 스냅 반려 루프와
# 같은 예산이어야 사람이 '재질의 2회'를 한 가지 뜻으로 읽는다.
from app.modules.grid import sound_events as sound_events_mod
from app.v3.seq_analyze import (
    MAX_REASKS,
    heuristic_hints,
    hint_mismatch,
    summarize_grid,
)
from app.v3.story import LOW_CONF, STORY_TEMPLATE_SPECS
from app.v4 import video as video_mod

# ── 계약 상수 (M3 §2) ───────────────────────────────────────────────────────
CANDIDATES_MIN = 5            # 운영자 결정 O3
CANDIDATES_MAX = 16
TITLE_DRAFT_MAX_CHARS = 20    # v3 `_enforce_title_line_limit` · 렌더러 split_text_smart 와 같은 자
SEGMENTS_MAX = 8              # 후보 하나의 조각 상한(8단계 offset 파트 수와 한 몸)

SCHEMA_CANDIDATES = "v4_candidates/v1"   # M1 §8 — `checkpoint_candidates.json` 의 schema 값

# 템플릿 어휘의 **정본은 v3 레지스트리**다(계약 §0 "템플릿 4종 설명"). v3 는 채널이
# `--story-templates` 로 열어야 추가 2종이 제공됐지만(회귀 0 조건), v4 는 기획서 §6 이
# "스토리 템플릿 4종을 축으로"라 기본이 전부다.
TEMPLATES_DEFAULT: tuple[str, ...] = tuple(STORY_TEMPLATE_SPECS)

# 기본 2종의 설명은 v3 가 `PROMPT_TEMPLATE` **본문에 박아 뒀다**(desc 가 빈 문자열이다) —
# import 할 것이 없어 여기 다시 적는다. v3 문장에서 내레이션 비율·비트 역할 이름 같은
# 10단계 어휘는 뺐다(위 ⚠). 추가 2종은 `STORY_TEMPLATE_SPECS[name]["desc"]` 를 그대로 쓴다.
TEMPLATE_BRIEFS: dict[str, str] = {
    "recap_dialogue": (
        "- recap_dialogue(대사 리캡): 인물의 대사를 축으로 사건을 따라간다. 사건 한복판의 "
        "대사로 열고, 핵심 대사는 자르지 말고 통째로 담고, 떡밥·도전 대사 직후 컷."),
    "highlight": (
        "- highlight(하이라이트): 강한 순간만 시각순으로. 이야기 연결보다 순간의 세기가 축이다."),
}

# 영상을 보는 호출의 설정 — **한 곳에 모은다**(video.py 모듈 독스트링의 이유 그대로).
CALL_MEDIA_RESOLUTION = "LOW"       # 예산 산식(`fps.TOKENS_PER_FRAME` 71)이 LOW 기준이다.
                                    # 미지정도 실측상 LOW 와 같지만(기획서 §2-C) 명시한다 —
                                    # HIGH 로 새면 프레임당 ×4 라 4단계가 정한 fps 가 상한을 넘는다.
CALL_TIMEOUT_SEC = 450.0            # 기획서 §7 "6단계 ≥450초"
CALL_MAX_OUTPUT_TOKENS = video_mod.DEFAULT_MAX_OUTPUT_TOKENS   # 65536 — 후보 16개 × 조각 8개

REJECT_NOTE_MAX = 20          # 재질의에 실을 사유 수(v3 story 와 같은 자 — 프롬프트가 불어나면
                              # 그만큼 영상 예산을 먹는다)
TRANSCRIPT_TIME_FMT = "[{t:.1f}]"     # 계약 §2 예시: `[120.0] 이건 정말 …`


# ── 작은 읽기 도우미 ────────────────────────────────────────────────────────

def _num(value: Any) -> float | None:
    """숫자로 읽히고 유한하면 float, 아니면 None. **bool 은 숫자로 치지 않는다.**

    ⚠ 문자열은 받지 않는다. 계약이 "숫자"이고 프롬프트가 절대초 실수를 요구하므로,
    `"00:02:00"` 을 관용적으로 읽어 주면 '시:분:초로 답해도 통과한다'가 되어 다음 편에서
    같은 표기가 다시 온다. 반려 사유에 그 값을 적어 모델에게 돌려주는 것이 계약이다.
    (`funnel._num`·`video._finite` 과 같은 자리의 지역 리더 — 이쪽은 '못 읽으면 None'.)"""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def prompt_sha(text: str) -> str:
    """프롬프트 지문(12 hex) — 배선이 캐시 지문 재료로 쓴다(계약 §5).

    ⚠ 지문은 **1차 시도의 프롬프트**로 잰다. 재질의 프롬프트는 반려 사유가 붙어 매번
    달라지므로, 그걸로 지문을 만들면 같은 입력이 실행마다 다른 지문을 받는다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ── 전사 블록 ───────────────────────────────────────────────────────────────

def _word_confidence_index(words: Any) -> tuple[list[float], list[float]]:
    """단어 목록 → (중점 오름차순, 같은 순서의 확률). 이분 탐색용. 순수.

    grid 의 단어는 `{t0, t1, text, prob}`(`grid.transcribe.transcribe_words`)다.
    확률이 없는 단어는 **세지 않는다** — 0 으로 채우면 확률을 안 주는 백엔드의 전사가
    통째로 '저확신'이 된다(모르는 것을 틀렸다고 하지 않는다)."""
    pairs: list[tuple[float, float]] = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        t0, t1, prob = _num(w.get("t0")), _num(w.get("t1")), _num(w.get("prob"))
        if t0 is None or t1 is None or prob is None:
            continue
        pairs.append(((t0 + t1) / 2.0, prob))
    pairs.sort()
    return [m for m, _ in pairs], [p for _, p in pairs]


def _mean_prob(mids: list[float], probs: list[float], t_in: float, t_out: float
               ) -> float | None:
    """[t_in, t_out] 안에 중점이 있는 단어들의 평균 확률. 없으면 None(판정하지 않는다)."""
    lo = bisect.bisect_left(mids, t_in)
    hi = bisect.bisect_right(mids, t_out)
    if hi <= lo:
        return None
    window = probs[lo:hi]
    return sum(window) / len(window)


def transcript_block(grid: dict, *, max_chars: int | None = None) -> str:
    """전사를 프롬프트 모양으로 → `[120.0] 이건 정말 대단한 순간이었습니다` 줄 목록. 순수.

    재료는 격자의 **유성 span**(`span_candidates` 의 `is_audio`)이다. 단어 단위로 실으면
    같은 내용이 몇 배로 불어나 영상 예산을 먹고, cue 단위가 곧 `quote` 의 자연 단위다
    (묶는 규칙은 `grid.timegrid.group_words_to_cues` = `stt_elevenlabs.words_to_segments`).

    ⚠ **`[저확신 …]` 표지의 임계는 미검증이다.** 값(`story.LOW_CONF` 0.5)은 v3 가 Stage 2
    모델의 자기신고 확신도에 쓰던 자인데, 여기 들어가는 것은 whisper 의 **단어 확률**로
    **척도가 다르다**. 표시만 하고 줄을 버리지 않는 것이 그래서다(E13 실측: 저확신 3건 중
    2건이 멀쩡한 한국어였다). 실측 뒤 갈아낄 자리 — 20편 라운드의 숙제다.

    ⚠ 이 블록이 곧 `fps.resolve_sample_fps(text_tokens=…)` 가 재려던 그 텍스트다.
    6단계는 **실제 블록 길이**를 audit 에 남긴다(`transcript_chars`) — M8 이 그 둘을
    맞대어 환산 상수를 갈아낀다.

    `max_chars` 를 주면 앞에서부터 채우고 **잘랐다는 사실을 블록 안에 크게 남긴다**.
    ⚠ 자르는 것은 최후 수단이다 — 뒤쪽이 빠지면 예고·크레딧 신고의 근거가 사라진다.
    텍스트가 예산을 먹으면 자르지 말고 표본 fps 를 내려야 한다(4단계가 이미 그렇게 한다).
    """
    spans = [s for s in (grid.get("span_candidates") or [])
             if isinstance(s, dict) and s.get("is_audio")]
    mids, probs = _word_confidence_index(grid.get("words"))

    rows: list[str] = []
    for s in sorted(spans, key=lambda x: (_num(x.get("t_in")) or 0.0,
                                          _num(x.get("t_out")) or 0.0)):
        t_in, t_out = _num(s.get("t_in")), _num(s.get("t_out"))
        text = str(s.get("text") or "").strip()
        if t_in is None or t_out is None or not text:
            continue
        conf = _mean_prob(mids, probs, t_in, t_out)
        tag = f" [저확신 {conf:.2f}]" if conf is not None and conf < LOW_CONF else ""
        rows.append(f"{TRANSCRIPT_TIME_FMT.format(t=t_in)}{tag} {text}")

    if max_chars is None:
        return "\n".join(rows)

    kept: list[str] = []
    used = 0
    for row in rows:
        if used + len(row) + 1 > int(max_chars):
            break
        kept.append(row)
        used += len(row) + 1
    if len(kept) == len(rows):
        return "\n".join(rows)
    # 조용한 절단 금지 — 무엇이 빠졌는지 프롬프트 안에 남긴다(모델도 사람도 본다).
    last = kept[-1] if kept else "(한 줄도 싣지 못했다)"
    kept.append(f"… ⚠ 전사 {len(rows)}줄 중 {len(kept)}줄만 실었다"
                f"(상한 {int(max_chars)}자). 마지막으로 실은 줄: {last[:40]}")
    return "\n".join(kept)


# ── 프롬프트 ────────────────────────────────────────────────────────────────

def _required_roles(template: Any) -> tuple[str, ...]:
    """그 템플릿이 요구하는 비트 역할. 정본은 v3 레지스트리다(재선언 금지). 순수.

    모르는 이름이면 빈 튜플 — 템플릿 화이트리스트 검사가 이미 그 사유를 낸다.
    여기서 또 실패시키면 사유가 둘로 갈려 사람이 무엇을 고칠지 모른다."""
    spec = STORY_TEMPLATE_SPECS.get(template) if isinstance(template, str) else None
    return tuple((spec or {}).get("required_roles") or ())


def _template_block(templates: tuple[str, ...]) -> str:
    """허용 템플릿의 설명 — 모르는 이름은 **즉시 실패**(조용히 빼면 모델은 그 템플릿을
    설명 없이 고르게 되고, 검증기는 통과시킨다)."""
    lines: list[str] = []
    for name in templates:
        spec = STORY_TEMPLATE_SPECS.get(name)
        if spec is None:
            raise ValueError(
                f"모르는 스토리 템플릿 {name!r} — 사용 가능: {sorted(STORY_TEMPLATE_SPECS)}")
        desc = (spec.get("desc") or "").strip() or TEMPLATE_BRIEFS.get(name, "").strip()
        if not desc:
            raise ValueError(
                f"템플릿 {name!r} 의 설명이 없다 — `STORY_TEMPLATE_SPECS[…]['desc']` 나 "
                f"`TEMPLATE_BRIEFS` 에 한 줄을 적어라(설명 없는 이름을 프롬프트에 실으면 "
                f"모델이 그 템플릿을 짐작으로 쓴다)")
        need = _required_roles(name)
        # 역할이 하나뿐이면 어떤 후보든 이미 만족한다(조각은 최소 1개다) — 자명한 문구를
        # 프롬프트에 넣으면 진짜 제약이 묻힌다.
        if len(need) >= 2:
            # 🛑 검증기만 좁히면 모델은 거절당할 값을 계속 낸다(E17-1 교훈 — 그때도
            # 프롬프트를 함께 고쳐야 했다). 조각 하나가 비트 하나이므로 필수 역할 수가
            # 곧 **최소 조각 수**다. 2026-09-04 실소재 라운드에서 조각 1개짜리
            # conflict_payoff 후보가 10단계까지 가서 3콜을 태우고 죽었다.
            desc = (f"{desc}\n  ⚠ 이 템플릿은 역할 {', '.join(need)} 이(가) 반드시 있어야 "
                    f"하고 **조각 하나가 역할 하나**다 — 조각을 최소 {len(need)}개로 나눠라.")
        lines.append(desc)
    return "\n".join(lines)


def _hints_block(hints: dict | None) -> str:
    """휴리스틱 사전 후보 — v3 `seq_analyze.build_prompt` 와 같은 모양(intro·credit 만)."""
    hints = hints or {}
    lines = []
    for k in ("intro", "credit"):
        h = hints.get(k)
        if isinstance(h, dict) and _num(h.get("start")) is not None:
            lines.append(f"- {k} 후보: {format_ts(float(h['start']))} ~ "
                         f"{format_ts(float(h['end']))}")
    return "\n".join(lines) if lines else "- (휴리스틱 후보 없음)"


# 비선형 편성을 여는 덧붙임 절(게이트 `--nonlinear`). 미지정이면 "" 이라 프롬프트가
# **종전과 바이트 동일**하다. 기본 프롬프트는 순서를 말하지 않고 출력 예시가 오름차순
# 이라, 모델이 조각을 늘 소스 시간 순으로 낸다(실측 11/11 후보 · 4/4 edit_plan 단조).
# 출력 예시의 조각 순서. 🛑 **예시가 절보다 강하다**(2026-09-04 A/B 실측):
# 비선형 절을 켜고도 예시가 오름차순이면 모델은 예시를 따랐다 — 동일 소재·동일
# 격자로 A/B 를 돌린 결과 게이트 ON 6/6 · OFF 9/9 가 전부 시간순이었고 비선형
# 후보는 **0개**였다. 예시는 규칙 목록보다 681자 뒤, 곧 모델이 마지막으로 읽는
# 것이라 지시와 어긋나면 지시가 진다.
EXAMPLE_SEGMENTS_LINEAR = (
    '     {"start_sec": 120.0, "end_sec": 145.5, "quote": "전사에서 그대로 옮긴 대사 한 줄"},\n'
    '     {"start_sec": 331.0, "end_sec": 348.25, "quote": null}'
)

# 게이트를 켰을 때의 예시 — **뒤 구간을 먼저** 붙인 모양(결말 선공개형).
EXAMPLE_SEGMENTS_NONLINEAR = (
    '     {"start_sec": 331.0, "end_sec": 348.25, "quote": "결말/최고조 대사 — 전사에서 그대로"},\n'
    '     {"start_sec": 120.0, "end_sec": 145.5, "quote": "그 일이 벌어진 발단 대사"}'
)

ORDER_FREE_CLAUSE = """9. **조각 배열의 순서가 곧 붙는 순서다** — 소스 시간 순일 필요가 없다. 결말·최고조 대사를 첫 조각으로 앞당기고 그 뒤에 발단을 붙이는 구성을 써도 된다. 다만 ① 조각끼리 소스 구간이 **겹치면 안 된다**(같은 화면이 두 번 나간다) ② 앞당긴 조각이 그 자체로 이해되어야 한다(맥락 없이 이름·지시대명사만 나오는 대사는 훅으로 쓰지 마라).
"""

# 프롬프트에 실을 소리 사건 상한. 33분 실측이 29건이라 넉넉하다 — 넘치면 센 것부터
# 남기고 자른 건수는 audit `sound_events` 에 남는다(조용한 절단 금지).
SOUND_EVENTS_MAX = 40

# 모드 [A](현장음 턴)를 여는 덧붙임 절(게이트 `--sound-events`). 미지정이면 "" 이라
# 프롬프트가 **종전과 바이트 동일**하다. 「티키타카 편집점 지침서」 제3원칙:
# "대사가 없더라도 강렬한 현장음이 필요한 순간을 포착하여 시청각적 임팩트를 극대화".
# 재료는 격자 요약 끝의 '대사 없는 소리 사건' 줄이고, 그 목록은 코드가 결정적으로
# 뽑는다(`grid.sound_events` — 판정은 코드, 선택은 모델).
SOUND_EVENT_CLAUSE = """10. **대사 없는 소리도 재료다** — 위 '대사 없는 소리 사건' 목록은 전사에 글자가 없지만 소리가 큰 구간이다(타격·웃음·한숨·환호). 그런 구간을 조각으로 써도 된다. 그때 `quote` 는 `null` 이고, 조각 길이는 그 소리가 끝나는 실제 시간까지다(늘리지 마라). 대사와 대사 사이에 이런 조각을 하나 두면 호흡이 생긴다 — 다만 편당 1~2개면 충분하고, 목록에 없는 구간을 소리 사건이라고 지어내지 마라.
"""

PROMPT_TEMPLATE = """당신은 쇼츠 편집 후보를 고르는 구성작가다. 첨부한 영상 전체를 훑고, 아래 전사·격자 요약을 근거로 **쇼츠 후보 {n_min}~{n_max}개**를 골라라.

이 단계에서 하는 일은 **구간 고르기 하나**다. 내레이션 문장·화면 라벨·효과 문구·최종 제목은 쓰지 않는다 — 뒷단계가 쓴다. 지금 쓰면 출력이 잘려 전부 버려진다.

## 작품
{work_title}{research_block}

## 템플릿 (후보마다 하나 선택)
{template_block}

⚠ 위 설명에 나오는 내레이션·라벨·별명은 **뒷단계가 쓴다**. 지금은 그 구성이 성립할 **구간**만 고르면 된다 — 문구를 지어내지 마라.

## 격자 요약
{grid_summary}

위 장면 전환 목록은 컷 경계의 **권장 눈금**이다 — 조각 경계를 되도록 그 근처로 잡되, 대사가 잘리면 대사의 호흡(문장 시작~끝)을 우선하라.

## 휴리스틱 사전 후보 (참고용 — 코드가 자막 텍스트로 추정한 값이다. 화면과 다르면 화면이 정답이다)
{hints_block}

## 전사 (모든 시각은 원본 절대초 · 이 글자가 시각의 정본이다)
{transcript}

## 후보 하나의 모양
- 조각(segments) **1~{segments_max}개**. 조각 하나 = 원본에서 이어지는 한 구간 [start_sec, end_sec].
- 조각 길이 합계 목표 **{target_sec:.0f}초**(상한 {max_sec:.0f}초).
- 조각마다 `quote` — 그 구간에서 실제로 발화되는 대사 **한 줄**.
- 제목 가안 두 줄(line1=상황, line2=펀치) · 선택 사유 한 문장.

## 규칙 (위반하면 그 후보가 버려진다)
1. **모든 시각은 원본 절대초의 실수**로 답하라. `120.5` 처럼 쓴다 — `"00:02:00"` 같은 시:분:초 표기, 편집본 기준 시각, 문자열로 감싼 숫자는 전부 거절된다.
2. 🛑 **`quote` 는 위 전사에서 그대로 복사하라.** 다듬지 마라 — 맞춤법·조사·띄어쓰기·말줄임을 고치지 말고, 두 줄을 합치거나 요약하지도 마라. 코드가 **이 글자로 그 조각의 시각이 맞는지 대조한다**. 글자가 달라지면 시각을 확인할 수 없어 그 조각은 옮겨지거나 버려진다. 대사가 없는 조각은 `quote` 를 `null` 로 둬라 — 없는 대사를 지어내면 그 후보는 통째로 버려진다.
3. 후보 {n_min}~{n_max}개는 **서로 다른 아크·다른 소재**여야 한다. 같은 구간의 경계만 바꾼 안은 안 된다. 템플릿을 섞고, "안전한 안"과 "과감한 안"을 함께 내라.
4. 제목은 **가안**이다. 두 줄 각 {title_max}자 이내로 짧게 — 확정은 뒷단계가 한다. 길면 잘려 나간다.
5. **서론 금지** — 인사말·자기소개·상황 설명성 대사로 시작하는 구간은 첫 조각(훅)으로 채택하지 마라. 후킹은 사건 한복판 대사의 몫이다.
6. **대사 신뢰** — 전사 줄에 `[저확신 …]` 이 붙어 있으면 받아쓰기가 흔들린 구간이라 그 줄을 `quote` 로 쓰지 마라(그 구간을 꼭 쓰려면 같은 조각의 다른 줄을 골라라). 전사에 줄이 없는 시각대는 **대사가 없는 구간**이다 — 그런 조각의 `quote` 는 `null` 이다.
7. **인트로·지난 줄거리·예고·엔딩 크레딧·방송 종료 화면**을 `exception_sector` 로 함께 신고하라. 없는 항목은 `null`. 예고(teaser)는 화면에 콜라주·장식 프레임, 스태프롤 병행, "다음 이야기" 문구, 본편과 단절된 빠른 몽타주(장소·의상이 컷마다 바뀜)로 알아본다. ⚠ 예고의 **시작은 장식 프레임이 뜨는 순간이 아니라** 본편 서사가 끝난 뒤 예고 소재가 시작되는 **첫 컷**이다. 경계가 불확실하면 **이른 쪽**을 골라라 — 본편에 예고가 새는 것이 예고를 조금 잘라내는 것보다 훨씬 나쁘다.
8. 후보의 조각은 본편에서 고른다 — 위에서 신고한 인트로·예고·크레딧 구간은 쓰지 마라.
{order_block}{sound_block}{reject_block}
## 출력 (JSON 만)
{{"candidates": [
 {{"id": "c01", "template": "{first_template}", "reason": "선택 사유 한 문장",
   "title_draft": {{"line1": "…", "line2": "…"}},
   "segments": [
{example_segments}
   ]}}
 ],
 "exception_sector": {{"intro": {{"start_sec": 0.0, "end_sec": 43.0}}, "recap": null,
   "teaser": {{"start_sec": 4029.25, "end_sec": 4110.0}}, "credit": null, "end": null}}}}"""


def build_prompt(*, work_title: str, transcript: str, grid_summary: str,
                 research_context: str = "", hints: dict | None = None,
                 templates: tuple[str, ...], target_sec: float, max_sec: float,
                 n_min: int = CANDIDATES_MIN, n_max: int = CANDIDATES_MAX,
                 reject_note: str = "", nonlinear: bool = False,
                 sound_events: bool = False) -> str:
    """6단계 프롬프트. 순수 — 같은 인자면 같은 문자열(지문의 전제).

    담는 것(계약 §2): ① 절대초로 답하라 ② quote 를 **전사에서 그대로** ③ 서로 다른 아크로
    n_min~n_max개 ④ 제목 가안 한 줄씩 {TITLE_DRAFT_MAX_CHARS}자 ⑤ exception_sector 신고.
    담지 않는 것: 내레이션 자수·라벨 앵커·제목 확정(10단계 몫)."""
    if not templates:
        raise ValueError("templates 가 비었다 — 허용 템플릿을 적어도 하나 넘겨라")
    research_block = ""
    if research_context and research_context.strip():
        # 상한 2000자는 v3 `seq_analyze.build_prompt` 와 같은 자다(같은 재료·같은 자리).
        research_block = "\n\n## 작품 배경 (리서치)\n" + research_context.strip()[:2000]
    # 게이트 — 미지정이면 "" 이라 렌더된 프롬프트가 종전과 바이트 동일하다.
    order_block = ORDER_FREE_CLAUSE if nonlinear else ""
    example_segments = (EXAMPLE_SEGMENTS_NONLINEAR if nonlinear
                        else EXAMPLE_SEGMENTS_LINEAR)
    sound_block = SOUND_EVENT_CLAUSE if sound_events else ""
    reject_block = ""
    if reject_note and reject_note.strip():
        reject_block = ("\n## ⚠ 직전 제안 반려 사유 — 전부 고쳐서 다시 내라\n"
                        f"{reject_note.strip()}\n")
    return PROMPT_TEMPLATE.format(
        work_title=work_title, research_block=research_block,
        template_block=_template_block(tuple(templates)),
        grid_summary=grid_summary, hints_block=_hints_block(hints),
        transcript=transcript, segments_max=SEGMENTS_MAX,
        target_sec=float(target_sec), max_sec=float(max_sec),
        n_min=int(n_min), n_max=int(n_max), title_max=TITLE_DRAFT_MAX_CHARS,
        order_block=order_block, sound_block=sound_block,
        example_segments=example_segments,
        reject_block=reject_block, first_template=templates[0])


# ── 검증 ────────────────────────────────────────────────────────────────────

def _title_draft(raw: Any, notes: list[str]) -> dict[str, str]:
    """제목 가안 정규화 → {line1, line2}. **후보를 버리지 않는다**(계약 §2).

    가안이라 10단계가 strict 로 다시 건다. 여기서 반려하면 제목 한 줄 때문에 후보 16개가
    통째로 날아간다. 손댄 것은 전부 `notes` 로 남긴다(조용한 절단 금지)."""
    if isinstance(raw, dict):
        lines = {"line1": raw.get("line1"), "line2": raw.get("line2")}
    elif isinstance(raw, str):
        # 한 줄로 온 모양(M1 §8 예시가 문자열이다) — 윗줄로 읽고 남긴다.
        lines = {"line1": raw, "line2": ""}
        notes.append("title_draft 가 문자열로 왔다 — line1 로 읽었다")
    elif raw is None:
        lines = {"line1": "", "line2": ""}
        notes.append("title_draft 가 없다 — 빈 가안으로 둔다(10단계가 짓는다)")
    else:
        lines = {"line1": "", "line2": ""}
        notes.append(f"title_draft 형식 오류({type(raw).__name__}) — 빈 가안으로 둔다")

    out: dict[str, str] = {}
    for key in ("line1", "line2"):
        text = str(lines.get(key) or "").strip()
        if len(text) > TITLE_DRAFT_MAX_CHARS:
            notes.append(f"title_draft.{key} 가 {len(text)}자 — "
                         f"{TITLE_DRAFT_MAX_CHARS}자로 잘랐다: {text!r}")
            text = text[:TITLE_DRAFT_MAX_CHARS]
        out[key] = text
    return out


def _validate_segments(raw: Any, *, where: str, source_duration_sec: float,
                       ) -> tuple[list[dict] | None, list[str]]:
    """조각 배열 검증 → (정규화 목록 | None, 사유). 순수.

    ⚠ 소스 범위는 여기서 **버린다**(클램프하지 않는다). 클램프는 6c 가 전사와 함께 보고
    하는 일이고(`timestamp_check.bounds_problem`), 여기서 미리 당기면 모델이 낸 시각이
    조용히 달라져 6c 의 대조 재료가 바뀐다."""
    problems: list[str] = []
    if not isinstance(raw, list) or not raw:
        return None, [f"{where}: segments 가 비어 있거나 배열이 아니다"]
    if len(raw) > SEGMENTS_MAX:
        return None, [f"{where}: 조각이 {len(raw)}개 — {SEGMENTS_MAX}개 이하로 나눠라"]

    out: list[dict] = []
    for i, seg in enumerate(raw):
        if not isinstance(seg, dict):
            problems.append(f"{where} 조각{i}: 객체가 아니다({type(seg).__name__})")
            continue
        start, end = _num(seg.get("start_sec")), _num(seg.get("end_sec"))
        if start is None or end is None:
            problems.append(
                f"{where} 조각{i}: 시각이 숫자가 아니다 "
                f"(start_sec={seg.get('start_sec')!r} end_sec={seg.get('end_sec')!r}) — "
                f"원본 절대초의 실수로 답하라")
            continue
        if not end > start:
            problems.append(f"{where} 조각{i}: 구간 역전 {start}~{end} — start < end")
            continue
        if start < 0 or end > float(source_duration_sec):
            problems.append(
                f"{where} 조각{i}: 소스 범위 밖 {start}~{end} "
                f"(소스 0~{float(source_duration_sec):.1f}s)")
            continue

        quote = seg.get("quote")
        if quote is None:
            text: str | None = None
        elif isinstance(quote, str):
            # 빈 문자열은 '대사 없음'과 같은 뜻이다 — null 로 읽는다(정보를 잃지 않는다).
            text = quote.strip() or None
        else:
            problems.append(f"{where} 조각{i}: quote 는 문자열이거나 null 이어야 한다"
                            f"({type(quote).__name__})")
            continue
        out.append({"start_sec": round(start, 3), "end_sec": round(end, 3), "quote": text})

    if problems:
        return None, problems
    return out, []


def _validate_candidate(raw: Any, *, where: str, templates: tuple[str, ...],
                        source_duration_sec: float,
                        ) -> tuple[dict | None, list[str]]:
    """후보 하나 → (정규화 후보 | None, 사유). 순수. **id 는 부르는 쪽이 확정한다.**"""
    if not isinstance(raw, dict):
        return None, [f"{where}: 후보가 객체가 아니다({type(raw).__name__})"]

    problems: list[str] = []
    template = raw.get("template")
    if template not in templates:
        problems.append(f"{where}: template 은 {list(templates)} 중 하나여야 한다: "
                        f"{template!r}")
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        problems.append(f"{where}: reason(선택 사유 한 문장)이 비었다")

    segments, seg_problems = _validate_segments(
        raw.get("segments"), where=where, source_duration_sec=source_duration_sec)
    problems.extend(seg_problems)

    # 🛑 **조각 수 < 템플릿 필수 역할 수 = 구조적으로 불가능한 후보다.**
    # 다리는 조각 하나를 비트 하나로 옮기므로(`bridge.to_beats`), 조각이 1개면 비트도
    # 1개이고 `conflict_payoff` 처럼 역할 둘(turn·payoff)을 요구하는 템플릿은 절대
    # 만족할 수 없다. 10단계가 그걸 반려하는데 거기서 걸리면 **편당 3콜을 태우고 편을
    # 잃는다** — 2026-09-04 실소재 라운드에서 실제로 c01 이 그렇게 죽었다.
    # 여기서 걸면 값이 0 이고, 모델이 재질의에서 조각을 더 낼 수 있다.
    need = _required_roles(template)
    if need and segments is not None and len(segments) < len(need):
        problems.append(
            f"{where}: 템플릿 {template!r} 는 역할 {sorted(need)} 를 요구하는데 조각이 "
            f"{len(segments)}개다 — 조각 하나가 비트 하나이므로 최소 {len(need)}개로 "
            f"나누거나 다른 템플릿을 고르라")
    if problems:
        return None, problems

    notes: list[str] = []
    cand: dict[str, Any] = {
        "id": "",                       # 부르는 쪽이 채운다(중복·결번 없이)
        "template": template,
        "reason": reason,
        "title_draft": _title_draft(raw.get("title_draft"), notes),
        "segments": segments,
    }
    if notes:
        cand["notes"] = notes
    return cand, []


def _validate_sector(raw: Any, *, source_duration_sec: float,
                     ) -> tuple[dict | None, list[str]]:
    """exception_sector → (정규화 | None, 사유). None 은 **전량 반려**를 뜻한다. 순수.

    정규화 모양은 `{키: {"start_sec", "end_sec"} | None}` 이고 **다섯 키를 전부** 싣는다
    (`grid.schemas.EXCEPTION_KEYS`). 명시적 null 은 "신고 없음"이고, 그것을 아는 것이
    6b 의 재료다(신고가 없는 편은 꼬리 180초를 의무 확인한다).

    · 키를 모르면 **반려**한다(계약 §2). 조용히 무시하면 모델이 새 이름으로 신고한 예고
      구역이 판정에서 통째로 빠지고, 그 후보가 그대로 나간다 — 가왕쇼 6화 사고 그대로다.
    · 값 하나가 못 읽히면 **그 키만** null 로 두고 사유를 남긴다(반려는 아니다) —
      6b 의 꼬리 의무 창이 그 자리를 덮는다."""
    sector: dict[str, Any] = {k: None for k in EXCEPTION_KEYS}
    if raw is None:
        return sector, ["exception_sector 가 없다 — 전부 null 로 읽었다(신고 없음)"]
    if not isinstance(raw, dict):
        return None, [f"exception_sector 가 객체가 아니다({type(raw).__name__}) — "
                      f"{list(EXCEPTION_KEYS)} 키를 가진 객체로 답하라"]

    unknown = sorted(k for k in raw if k not in EXCEPTION_KEYS)
    if unknown:
        return None, [f"모르는 exception_sector 키: {unknown} — "
                      f"허용: {list(EXCEPTION_KEYS)}"]

    problems: list[str] = []
    for key in EXCEPTION_KEYS:
        node = raw.get(key)
        if node is None:
            continue
        if not isinstance(node, dict):
            problems.append(f"exception_sector.{key}: 객체가 아니다"
                            f"({type(node).__name__}) — null 로 읽었다")
            continue
        start = _num(node.get("start_sec"))
        end = _num(node.get("end_sec"))
        if start is None or end is None:
            problems.append(
                f"exception_sector.{key}: 시각이 숫자가 아니다 "
                f"(start_sec={node.get('start_sec')!r} end_sec={node.get('end_sec')!r}) "
                f"— null 로 읽었다")
            continue
        if not end > start or start < 0 or start >= float(source_duration_sec):
            problems.append(f"exception_sector.{key}: 구간이 소스 안이 아니다 "
                            f"{start}~{end} (소스 0~{float(source_duration_sec):.1f}s) "
                            f"— null 로 읽었다")
            continue
        if end > float(source_duration_sec):
            # 꼬리 구역은 끝을 소스 길이로 당긴다 — 내용을 더하지 않으므로 안전하고,
            # 예고가 끝까지 간다는 신고를 통째로 버리면 그 후보가 검사 없이 통과한다.
            problems.append(f"exception_sector.{key}: 끝 {end} 이 소스 밖 — "
                            f"{float(source_duration_sec):.3f}s 로 당겼다")
            end = float(source_duration_sec)
        sector[key] = {"start_sec": round(start, 3), "end_sec": round(end, 3)}
    return sector, problems


def validate_response(resp: Any, *, source_duration_sec: float,
                      templates: tuple[str, ...],
                      n_min: int = CANDIDATES_MIN, n_max: int = CANDIDATES_MAX,
                      ) -> tuple[list[dict] | None, dict | None, list[str]]:
    """→ (후보 목록 | None, exception_sector | None, 반려 사유 목록). 순수·결정적.

    검사는 전부 **구조**다 — 내용 판정(인용이 진짜인가·예고와 겹치는가·길이가 맞는가)은
    6c·7 의 몫이고, 여기서 같이 하면 같은 판정이 두 곳에 살게 된다.

    ⚠ **하나가 걸려도 그 후보만 버린다.** 전량이 걸릴 때만 None 이다. 다만 아래 셋은
    응답 전체가 못 쓰는 것이라 (None, None, 사유)로 반려한다:
      · 응답이 객체가 아니거나 `candidates` 가 배열이 아니다
      · `exception_sector` 가 객체가 아니다 / 모르는 키가 있다

    ⚠ 후보 수가 `n_max` 를 넘으면 **앞에서부터 자르고 사유를 남긴다**(응답 순서 = 결정적).
      모자라면 살아남은 것을 그대로 돌려주고 사유에 몇 개인지 적는다 — 재질의 여부는
      부르는 쪽(`run_candidates`)이 정한다.
    """
    dur = _num(source_duration_sec)
    if dur is None or dur <= 0:
        # 격자에서 오는 값이다(`grid["source"]["duration_sec"]`). 0 이 온다는 것은 배선
        # 오류이고, 그때 전량 통과시키면 소스 범위 검사가 조용히 사라진다.
        raise ValueError(f"소스 길이가 유효하지 않다: {source_duration_sec!r}")
    if not templates:
        raise ValueError("templates 가 비었다 — 허용 목록 없이는 판정할 수 없다")

    if not isinstance(resp, dict):
        return None, None, [f"응답이 객체가 아니다({type(resp).__name__})"]
    raw_cands = resp.get("candidates")
    if not isinstance(raw_cands, list) or not raw_cands:
        return None, None, ["candidates 배열이 없다 — "
                            "{\"candidates\": [...], \"exception_sector\": {...}} 로 답하라"]

    sector, problems = _validate_sector(resp.get("exception_sector"),
                                        source_duration_sec=dur)
    if sector is None:
        return None, None, problems

    kept: list[dict] = []
    taken: set[str] = set()
    pending: list[dict] = []          # id 를 아직 못 받은 후보(2차 배정 대상)
    for i, raw in enumerate(raw_cands):
        where = f"후보[{i}]"
        cand, cand_problems = _validate_candidate(
            raw, where=where, templates=tuple(templates), source_duration_sec=dur)
        if cand is None:
            problems.extend(cand_problems)
            continue

        cid = str((raw.get("id") if isinstance(raw, dict) else "") or "").strip()
        if cid and cid in taken:
            # id 는 checkpoint_candidates.json 의 좌표다(M1 §8) — 중복이면 verify·funnel·
            # approve 가 서로 다른 후보를 같은 이름으로 가리킨다(verify 는 거기서 죽는다).
            problems.append(f"{where}: id {cid!r} 가 중복이다 — 후보를 버렸다")
            continue
        if cid:
            taken.add(cid)
        cand["id"] = cid
        kept.append(cand)
        if not cid:
            pending.append(cand)

    # 빈 id 채우기 — `c%02d`(1-based)에서 **이미 쓰인 이름을 건너뛴다**. 순서대로 도는
    # 결정적 배정이라 같은 응답이면 늘 같은 id 다.
    counter = 1
    for cand in pending:
        while f"c{counter:02d}" in taken:
            counter += 1
        cand["id"] = f"c{counter:02d}"
        taken.add(cand["id"])

    if not kept:
        return None, sector, problems + [
            f"쓸 수 있는 후보가 하나도 없다({len(raw_cands)}개 중 전량 탈락)"]
    if len(kept) > n_max:
        problems.append(f"후보가 {len(kept)}개 — 상한 {n_max}개까지만 쓴다"
                        f"(뒤쪽 {len(kept) - n_max}개를 잘랐다)")
        kept = kept[:n_max]
    if len(kept) < n_min:
        problems.append(f"쓸 수 있는 후보가 {len(kept)}개 — 최소 {n_min}개가 필요하다")
    return kept, sector, problems


# ── 실행 ────────────────────────────────────────────────────────────────────

def _research_context(research: Any) -> str:
    """리서치 재료 → 프롬프트에 실을 문자열.

    v3 는 `(research or {}).get("work_context")` 로 읽는다(`v3/pipeline.py:357`) —
    `checkpoint_research.json` 을 그대로 넘겨도, 이미 뽑아 둔 문자열을 넘겨도 되게 한다."""
    if research is None:
        return ""
    if isinstance(research, str):
        return research
    if isinstance(research, dict):
        return str(research.get("work_context") or "")
    raise TypeError(f"research 는 dict·str·None 이어야 한다: {type(research).__name__}")


def _hint_mismatch(hints: dict, sector: dict) -> list[dict]:
    """휴리스틱 후보 vs 모델 신고의 불일치(검수 신호 — audit 용).

    ⚠ `seq_analyze.hint_mismatch` 는 v3 Stage 1 의 표기(`start`/`end`)를 읽는다. v4 의
    정규화 모양은 `start_sec`/`end_sec` 이라 여기서 한 번 옮긴다 — **판정 수식은 그쪽
    함수 하나**다(관용치 ±2s 를 여기 다시 적지 않는다)."""
    final_exc = {k: ({"start": v["start_sec"], "end": v["end_sec"]} if v else None)
                 for k, v in (sector or {}).items()}
    return hint_mismatch(hints or {}, final_exc)


def run_candidates(gemini: Any, handle: Any, *, work_title: str, grid: dict,
                   research: Any = None, sample_fps: float,
                   templates: tuple[str, ...] = TEMPLATES_DEFAULT,
                   target_sec: float, max_sec: float,
                   n_min: int = CANDIDATES_MIN, n_max: int = CANDIDATES_MAX,
                   nonlinear: bool = False, sound_events: bool = False,
                   log=print) -> tuple[dict, dict]:
    """1콜(+재질의 ≤MAX_REASKS) → (`checkpoint_candidates` 의 후보 절, audit).

    · 영상은 **전체를 한 파트로** 붙인다(조각 첨부는 8단계의 일이다). 표본 fps 는 4단계가
      정한 값(`fps.resolve_sample_fps`)을 그대로 받는다 — 여기서 다시 고르지 않는다.
    · `media_resolution` 은 **LOW 명시**다. 4단계 예산이 LOW 기준(프레임당 71)이라
      HIGH 로 새면 상한을 넘겨 400 을 받는다. 슬롯은 **Pro**(CLAUDE.md: 영상을 실제로
      보는 호출) — v4 는 두 슬롯이 같은 모델이지만 역할 표는 지킨다.
    · **반려 소진 = 편 전체 실패**(기획서 §7 — 시각 정본의 입구라 조용히 통과시키지
      않는다). audit 에 시도별 {attempt, problems, usage} 를 전량 남긴다.

    반환하는 '후보 절'은 **결정적**이다(시간·소요가 들어가지 않는다) — 지문·재개 대조가
    그 위에 선다. 시간은 audit 에만 남는다."""
    duration = _num((grid.get("source") or {}).get("duration_sec"))
    if duration is None or duration <= 0:
        raise ValueError("격자에 소스 길이가 없다 — `grid['source']['duration_sec']` 가 "
                         "있어야 6단계의 소스 범위 검사가 성립한다")
    templates = tuple(templates)

    transcript = transcript_block(grid)
    # 모드 [A] 재료. 게이트를 안 켜면 목록을 만들지도 넘기지도 않는다 — 요약이
    # 종전과 바이트 동일해야 프롬프트 지문이 안 움직인다.
    events = (sound_events_mod.detect_sound_events(grid, limit=SOUND_EVENTS_MAX)
              if sound_events else [])
    grid_summary = summarize_grid(grid, sound_events=events or None)
    hints = heuristic_hints(grid)
    research_ctx = _research_context(research)

    audit: dict[str, Any] = {
        "attempts": [],
        "templates": list(templates),
        "sample_fps": sample_fps,
        "transcript_chars": len(transcript),
        "transcript_lines": transcript.count("\n") + 1 if transcript else 0,
        "hints": hints,
        "n_range": [int(n_min), int(n_max)],
        "nonlinear": bool(nonlinear),
        "sound_events": len(events),
    }

    accepted: list[dict] | None = None
    sector: dict | None = None
    base_sha = ""
    reject_note = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = build_prompt(
            work_title=work_title, transcript=transcript, grid_summary=grid_summary,
            research_context=research_ctx, hints=hints, templates=templates,
            target_sec=target_sec, max_sec=max_sec, n_min=n_min, n_max=n_max,
            reject_note=reject_note, nonlinear=nonlinear,
            sound_events=bool(sound_events))
        sha = prompt_sha(prompt)
        if attempt == 0:
            base_sha = sha
        log(f"  [v4/candidates] 후보 편성 요청 (시도 {attempt + 1}/{1 + MAX_REASKS} · "
            f"{n_min}~{n_max}안 · fps {sample_fps:g} · 프롬프트 {len(prompt):,}자)")

        t0 = time.time()
        usage: dict | None = None
        problems: list[str]
        cands: list[dict] | None = None
        got: dict | None = None
        try:
            resp, usage = video_mod.call_video(
                gemini, handle, prompt, sample_fps=sample_fps,
                media_resolution=CALL_MEDIA_RESOLUTION,
                max_output_tokens=CALL_MAX_OUTPUT_TOKENS,
                thinking_level=gemini.config.analysis_thinking_level,
                model=gemini.config.model_name,          # Pro 슬롯(영상을 보는 호출)
                timeout_sec=CALL_TIMEOUT_SEC, log=log)
        except video_mod.VideoParseError as e:
            # 파싱 실패는 이 레포 실측의 상시 모드다(분석 22회 중 12회) — 크래시가 아니라
            # **반려 재료**로 쓴다. usage 를 들고 오므로 절단이면 그 숫자가 남는다.
            usage = e.usage
            problems = [f"응답 JSON 파싱 실패: {e}"]
        else:
            cands, got, problems = validate_response(
                resp, source_duration_sec=duration, templates=templates,
                n_min=n_min, n_max=n_max)

        ok = cands is not None and len(cands) >= n_min
        rec = {
            "attempt": attempt + 1,
            "elapsed_sec": round(time.time() - t0, 3),
            "prompt_sha": sha, "prompt_chars": len(prompt),
            "candidates": len(cands or []),
            "problems": problems,
            "usage": usage,
            "accepted": bool(ok),
        }
        audit["attempts"].append(rec)

        if ok:
            accepted, sector = cands, got
            if problems:
                # 통과했어도 손댄 것은 전부 보이게 남긴다(후보 드롭·제목 절단·상한 절단).
                log(f"  [v4/candidates] 후보 {len(accepted)}개 채택 — "
                    f"손댄 항목 {len(problems)}건")
                for p in problems:
                    log(f"    · {p}")
            else:
                log(f"  [v4/candidates] 후보 {len(accepted)}개 채택")
            break

        log(f"  [v4/candidates] 반려 — 사유 {len(problems)}건")
        for p in problems[:REJECT_NOTE_MAX]:
            log(f"    · {p}")
        reject_note = "\n".join(f"- {p}" for p in problems[:REJECT_NOTE_MAX])

    if accepted is None or sector is None:
        # 조용한 통과 금지 — 여기가 시각 정본의 입구다(기획서 §7 · 편 전체 실패 두 곳 중 하나).
        lines = [f"시도 {r['attempt']}: " + ("; ".join(r["problems"][:5]) or "사유 없음")
                 for r in audit["attempts"]]
        raise ValueError(
            f"6단계 후보 편성이 재질의 {MAX_REASKS}회를 소진했다 — 편 전체 실패.\n"
            + "\n".join(lines))

    audit["reasks_used"] = len(audit["attempts"]) - 1
    audit["hint_mismatch"] = _hint_mismatch(hints, sector)
    audit["prompt_sha"] = base_sha

    section = {
        # 파일 top 의 schema·fingerprint 는 배선(M5)이 쓴다. 값은 여기서 낸다 —
        # 두 곳이 각자 문자열을 적으면 언젠가 한쪽만 고쳐진다.
        "schema": SCHEMA_CANDIDATES,
        "source_duration_sec": round(duration, 3),
        "sample_fps": sample_fps,
        "prompt_sha": base_sha,
        "templates": list(templates),
        "candidates": accepted,
        # ⚠ 이름이 복수다 — 파일 계약(M1 §8)과 7단계 인자(`funnel.hard_problems(
        #   exception_sectors=…)`)가 복수형이고, 단수 `exception_sector` 는 **모델 응답의**
        #   열쇠다. 둘을 같은 이름으로 두면 어느 쪽 모양인지 읽는 쪽이 알 수 없다.
        "exception_sectors": sector,
    }
    return section, audit
