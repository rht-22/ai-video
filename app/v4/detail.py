"""10a 정밀 청취 — 승자 구간만 다시 듣는다. **선택 단계이고 기본은 꺼짐이다.**

계약 정본 `docs/v4/M5-interfaces.md` §3 · 기획 `docs/v4/v4-plan.md` §3(10a)·§4.

    승인 편의 조각들 ──합집합(겹침·인접 흡수)──▶ 창(≤180초)
        │  창마다 offset 파트 1개로 Pro 1콜(3fps)
        │  v3 Stage 2 의 검증·전사 판정 기계를 **그대로 부른다**
        ▼
    {span_id: {audio_script, importance, text_source, heard_text, …}}
        └─▶ `bridge.build_span_index(detail=…)` → v3 조립 기계

## 🛑 이 단계는 '전사 정확도 개선'만이 아니다 — **화자의 유일한 원천**이다

계약 §0 의 조사 결과다. `assemble.speaker_colors` 는 `span_index[sid]["audio_script"]`
의 `speaker` 로만 돈다. 그런데 whisper 전사에는 화자가 없고, v4 는 v3 의 **청크 상세
분석(Stage 2)을 없앴다.** 그러니 화자를 말해 주는 호출은 이 단계 하나뿐이다.

화자별 자막색은 M13 승계 체크리스트 항목이고 가왕쇼 템플릿의 "가장 큰 특징"이다
(CLAUDE.md V3-M13: "v3 는 전 줄 흰색이었다"를 고친 것). 10a 가 꺼져 있으면 그 색이
통째로 사라진다 — 그래서 `bridge.index_audit` 이 `speaker_source="none"` 과
`NO_SPEAKER_WARNING` 으로 소리를 낸다. **조용히 흰 자막으로 나가면 안 된다.**

⇒ 이 사실 때문에 계약 §0 은 N1(10a 기본 on/off)을 다시 보라고 적어 뒀다. 이 판은
   기본값을 바꾸지 않는다(기획서 §9 N1: "끄고 시작, A/B 뒤") — 사실만 남긴다.

## v3 와 무엇이 같고 무엇이 다른가

**같은 것(부른다 — 베끼지 않는다).** `chunk_analyze` 의
`spans_for_chunk`(중점 반개구간 소속) · `validate_stage2_response`(연속 구간 분할 검증) ·
`adjudicate_transcript`(M9-C 전사 판정 · 각색 임계 `TRANSCRIPT_DIFF_MAX`=0.35) ·
`assemble_chunk_meanings` · `verify_time_alignment`(시각 정합 벨트) · `character_cross_check`.
재질의 상한도 `seq_analyze.MAX_REASKS` 그대로다.

⚠ 계약 §3 은 전사 판정을 `textcheck.adjudicate_transcript` 라고 적었지만 그 함수는
`chunk_analyze` 에 있다(`textcheck` 는 그 안에서 반복 서명을 대는 하위 모듈이다).
**코드가 정본이라 `chunk_analyze` 것을 부른다.**

**다른 것 — 모델이 보는 좌표계.**

    v3 Stage 2 : 물리 재단한 청크 mp4 를 올린다. 파일의 0초 = 청크 시작.
    v4 10a     : 업로드는 프록시 **한 개**이고, 창마다 `VideoMetadata(start_offset,
                 end_offset)` 로 그 구간만 붙인다(`video.Clip`). 첨부된 파트의 0초 =
                 **창 시작**이다(원본 절대초가 아니다).

그래서 **시각 환산은 프롬프트 쪽에만 있다** — `window_offset_sec` 한 곳이고, span 표의
시각을 `t − window.start` 로 적는다. 반대 방향(모델 → 우리)의 환산은 **존재하지 않는다**:
모델은 시각을 아예 출력하지 않고 span id 로만 말하며, 확정 시각은 전부 격자 lookup 이다
(v3 가 시각 정합 100% 를 검증이 아니라 **구조**로 얻은 그 설계 그대로다).

⚠ v3 는 청크 파일이 0초부터 시작하는데도 span 표에 **원본 절대초**를 적었다. 응답에
시각이 없으니 무해했지만, 모델이 화면에서 보는 시각과 표의 시각이 다르다는 뜻이다.
v4 는 창 상대초로 적어 그 어긋남을 없앤다(파트 안에서 span 을 찾는 것이 이 호출의 일이다).

## 창은 왜 창마다 한 콜인가

`validate_stage2_response` 는 "이 목록의 **모든 span 이 정확히 하나의 meaning 에**,
연속 구간으로" 를 요구한다. 그 규칙은 **끊기지 않은 한 줄기 구간**에서만 뜻이 있다 —
멀리 떨어진 두 창의 span 을 한 목록으로 주면 모델이 두 창을 가로지르는 meaning 을 만들고,
그 meaning 의 시각 구간이 화면에 없는 사이 구간까지 삼킨다. 그래서 창 하나 = 목록 하나 =
콜 하나다(기획서 §4 의 비용표 "10a k × 40,000" 도 편당 180초 한 콜 기준이다).

## 실패는 원판정 유지다

창 하나가 실패하면 **그 창의 span 만 결과에서 빠진다.** 빠진 span 은 `bridge` 가 기본값
(importance 3 · 화자 없음 · `text_source="transcript"`)으로 채우므로 전사 채택으로
돌아간다 — 안전장치가 본편을 막지 않는다(6b `refine` 의 '원판정 유지'와 같은 규율).
전량 실패도 예외가 아니다: 10a 는 부가 단계이고, 없으면 v3 기본값으로 도는 것이 정상
동작이다. 대신 **무엇이 왜 빠졌는지는 전량 기록**한다(규율 3).

⚠ 반대로 **우리 쪽 결함은 삼키지 않는다** — 검증기가 보장한 값이 아닌 것이 오면
(importance 범위 밖·span 정렬 어긋남) 그대로 올린다. 배선 사고를 '모델이 실패했다'로
적어 두면 원인이 감사에 남지 않는다(`flags.run_flags` 와 같은 규율).

## 실호출로만 알 수 있는 것 (이 워크트리에 `GEMINI_API_KEY` 가 없다)

· 서버가 offset 파트를 **창 그대로** 보여 주는가 — 근거는 기획서 §2-B 의 REST 실측이고
  SDK 경로는 미검증이다(`video.py` 독스트링과 같은 항목).
· 모델이 화자를 사람과 같게 배정하는가 · 이름을 편 전체에서 일관되게 쓰는가.
  **화자 품질은 미확인**이고 여기 테스트는 배선·계약·좌표만 고정한다.
· `DETAIL_MAX_OUTPUT_TOKENS` 가 충분한가 — v3-M2 실측에서 thinking 이 출력 예산을 먹어
  절단이 났다(그때는 재질의가 자연 회복시켰다). 절단은 `call_video` 가 크게 남긴다.
"""
from __future__ import annotations

import math
import time
from typing import Any, Sequence

from app.modules.grid import schemas
from app.v3 import assemble
from app.v3.chunk_analyze import (
    TRANSCRIPT_DIFF_MAX,
    adjudicate_transcript,
    assemble_chunk_meanings,
    character_cross_check,
    spans_for_chunk,
    validate_stage2_response,
    verify_time_alignment,
)
from app.v3.seq_analyze import MAX_REASKS
from app.v4.candidates import prompt_sha
from app.v4.video import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MIN_CLIP_SEC,
    Clip,
    VideoCallError,
    VideoParseError,
    call_video,
    clips_within_source,
)

# ── 계약 상수 (M5 §3) ───────────────────────────────────────────────────────

# 기획서 §3(10a) "승인 편의 구간(≤180초)만 3fps 로". v3 Stage 2 의 `CHUNK_SAMPLE_FPS`
# 와 같은 값이지만 **가져다 쓰지 않는다** — 저쪽은 청크(≤10분) 전용이고 이쪽은 창(≤180초)
# 이라 근거가 다르다. 한쪽을 튜닝할 때 다른 쪽이 따라 움직이면 안 된다.
DETAIL_SAMPLE_FPS = 3.0

# 창 상한. 기획서 §4 비용표의 "10a k × 40,000" 이 이 길이 기준이다
# (180s × (3fps × 71 + 32) ≈ 44,100 — CLAUDE.md 의 count 산식).
DETAIL_WINDOW_MAX_SEC = 180.0

# 합집합에서 두 조각을 '붙어 있다'고 볼 관용. **부동소수 관용이지 '가까우니 붙이자'가
# 아니다** — `flags.CONTIGUOUS_TOLERANCE_SEC` 와 같은 자·같은 근거(0.5초 틈은 진짜 컷이고,
# 그 틈을 창에 넣으면 승인 편에 없는 화면을 모델에게 보여 주게 된다).
DETAIL_MERGE_GAP_SEC = 0.05

# v3 Stage 2 와 같은 자(`_call_stage2_model` 의 65536 = `video.DEFAULT_MAX_OUTPUT_TOKENS`).
# span 마다 scene_script + heard 를 받으므로 출력이 길다 — 8단계처럼 줄이면 절단난다.
DETAIL_MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

# 프롬프트 반려 사유는 앞에서부터 이만큼만 되돌린다(`candidates.REJECT_NOTE_MAX` 와 같은
# 취지 — 사유를 전량 실으면 재질의 프롬프트가 원 프롬프트보다 길어진다).
REJECT_NOTE_MAX = 20

# ── 실패 사유 어휘 ──────────────────────────────────────────────────────────
# `checkpoint_winner_detail.json` 과 run_log 에 **그대로** 실린다. 바꾸면 저장된 잡의
# 감사 기록과 대조가 끊긴다 — 테스트가 값으로 박제한다.
REASON_NO_SPANS = "no_spans"            # 창에 span 이 없다(무성 극단 — v3 와 같은 사유)
REASON_BELT_DROPPED = "belt_dropped"    # 경계 벨트가 창을 버렸다(소스 밖·너무 짧음)
REASON_CALL_FAILED = "call_failed"      # 호출 실패(E11 분류는 call_video 안에서 끝난다)
REASON_PARSE_FAILED = "parse_failed"    # 응답이 JSON 이 아니다
REASON_REJECTED = "rejected"            # 재질의 소진 — 계약을 못 지킨 응답

STATUS_OK = "ok"

__all__ = [
    "DETAIL_SAMPLE_FPS", "DETAIL_WINDOW_MAX_SEC", "DETAIL_MERGE_GAP_SEC",
    "DETAIL_MAX_OUTPUT_TOKENS", "REJECT_NOTE_MAX", "MAX_REASKS",
    "TRANSCRIPT_DIFF_MAX", "STATUS_OK",
    "REASON_NO_SPANS", "REASON_BELT_DROPPED", "REASON_CALL_FAILED",
    "REASON_PARSE_FAILED", "REASON_REJECTED",
    "DETAIL_PROMPT", "window_offset_sec", "detail_windows", "span_table",
    "build_detail_prompt", "detail_nodes", "run_detail",
]


# ── 1) 창 만들기 ────────────────────────────────────────────────────────────

def _seg_time(seg: Any, key: str, *, where: str) -> float:
    """조각에서 시각 하나를 읽는다. 모양이 다르면 **크게 실패**한다.

    ⚠ `start`/`end` 별칭을 받지 않는다 — `flags._seg_time` 과 같은 규율이고 이유도 같다.
    여기서 0.0 으로 떨어지면 **모델에게 엉뚱한 구간을 들려주고**, 그 구간에서 받은 화자가
    승인 편의 자막색이 된다. 자료 모양의 정본은 `M1-interfaces.md` §8 의
    `{start_sec, end_sec}` 이고 그 밖의 모양은 배선 오류다."""
    value = seg.get(key) if isinstance(seg, dict) else getattr(seg, key, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{where}: 조각의 {key} 가 숫자가 아니다: {value!r} — "
            f"자료 모양 정본은 {{start_sec, end_sec}} 다(M1 §8). "
            f"별칭(start/end)을 추측으로 받지 않는다")
    return float(value)


def window_offset_sec(t: float, window: Clip) -> float:
    """원본 절대초 → **첨부 파트 기준 초**. 좌표 환산은 이 함수 하나다(모듈 독스트링).

    첨부한 파트의 0초는 `start_offset`(= 창 시작)이다. 그래서 환산은 뺄셈 하나이고,
    되돌리는 방향은 **없다** — 모델은 시각을 출력하지 않는다.

    ⚠ 창 밖 시각은 크게 실패한다. 창 밖 span 을 표에 적으면 모델은 화면에 없는 것을
    찾게 되고, 못 찾은 자리를 지어낸다."""
    if window.whole or window.start_sec is None or window.end_sec is None:
        raise ValueError("전체 첨부 창에는 상대 좌표가 없다 — 10a 는 구간만 본다")
    start, end = float(window.start_sec), float(window.end_sec)
    if not start - MIN_CLIP_SEC <= float(t) <= end + MIN_CLIP_SEC:
        raise ValueError(f"창 밖 시각이다: {t} ∉ [{start}, {end}]")
    return round(float(t) - start, 3)


def detail_windows(approved_segments: Sequence[Any] | None, *,
                   max_sec: float = DETAIL_WINDOW_MAX_SEC) -> list[Clip]:
    """승인 편 조각의 **합집합**(겹침·인접 흡수) → offset 파트 목록. 순수·결정적.

    입력은 승인된 편들의 조각을 그대로 이어 놓은 평평한 목록이다(`{start_sec, end_sec}`).
    편이 여럿이면 같은 구간을 두 편이 쓸 수 있으므로 **겹침을 흡수**한다 — 같은 구간을
    두 번 들려주면 요금만 두 배이고, 두 번의 화자 배정이 서로 다를 수도 있다.

    ⚠ `flags.merge_contiguous_clips` 를 쓰지 않는다. 저쪽은 '이웃이 맞닿을 때만' 이고
    끝을 **뒤 조각의 끝으로** 바꾼다 — 앞 조각이 뒤 조각을 감싸는 경우(포함) 창이
    **줄어든다**. 이쪽이 필요한 것은 합집합이라 연산 자체가 다르다(같은 이름을 쓰지
    않는 이유이기도 하다).

    상한을 넘는 구간은 **등분**한다(그리디로 자르면 마지막 조각이 몇 밀리초짜리 슬리버가
    되고, 그 조각은 경계 벨트에 걸려 통째로 버려진다). 등분 경계는 이어 붙으므로 그
    구간의 span 은 **중점 규칙으로 정확히 한 창에** 속한다(`spans_for_chunk` 의
    반개구간 타일링 — 두 창에 겹쳐 실리지 않는다).
    """
    limit = float(max_sec)
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError(f"창 상한은 양의 유한수여야 한다: {max_sec!r}")

    spans: list[tuple[float, float]] = []
    for i, seg in enumerate(approved_segments or []):
        where = f"승인 조각 {i}"
        s = _seg_time(seg, "start_sec", where=where)
        e = _seg_time(seg, "end_sec", where=where)
        if not e > s:
            # 6c 를 지난 조각은 역전일 수 없다 — 그렇다면 배선 사고다(조용히 고치면 숨는다).
            raise ValueError(f"{where}: 구간 역전 {s}~{e}")
        spans.append((s, e))

    merged: list[list[float]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1] + DETAIL_MERGE_GAP_SEC:
            merged[-1][1] = max(merged[-1][1], e)      # 합집합 — 포함 관계에서도 안 줄어든다
            continue
        merged.append([s, e])

    out: list[Clip] = []
    for s, e in merged:
        length = e - s
        n = max(1, int(math.ceil(length / limit - 1e-9)))
        step = length / n
        edges = [round(s + i * step, 3) for i in range(n)] + [round(e, 3)]
        for a, b in zip(edges, edges[1:]):
            if b > a:
                out.append(Clip(start_sec=a, end_sec=b))
    return out


# ── 2) 프롬프트 ─────────────────────────────────────────────────────────────

# v3 `chunk_analyze.PROMPT_TEMPLATE` 과 **같은 과제·같은 출력 스키마**다. 그 응답을 읽는
# 검증기가 `validate_stage2_response` 이기 때문이다 — 프롬프트만 고치고 검증기를 안 고치면
# 매 창 반려당하고, 검증기만 고치면 모델은 계속 같은 응답을 낸다(E17-1 판례).
# 문자열을 import 해 오지 못하는 자리라(머리말·좌표 안내가 v4 전용) 테스트가 대신 묶는다:
# `validate_stage2_response` 가 요구하는 열쇠 이름이 전부 이 문자열에 있어야 한다.
#
# v4 에서 더한 문장은 둘이고 이유가 있다:
#  ① **좌표 안내** — 첨부한 파트의 0초는 창 시작이다(v3 는 물리 재단 파일이라 이 말이
#     필요 없었고, 그래서 표에 원본 절대초를 적어도 무해했다).
#  ② **화자 이름 일관** — 이 호출이 화자의 유일한 원천이고(모듈 독스트링), 같은 사람이
#     창마다 다른 이름을 받으면 `assemble.speaker_colors` 가 그 사람에게 색을 두 개 준다.
DETAIL_PROMPT = """당신은 방송 영상의 장면 기록가다. 첨부한 영상은 원본에서 잘라 낸 **한 구간**이다(원본 {abs_start}~{abs_end} · {dur:.1f}초).

⚠ 첨부 영상의 **처음이 0초**다. 아래 표의 시각도 전부 첨부 영상 기준이다 — 원본 방송 시각이 아니다.

{research_block}## 이 구간의 span 목록 (id | 시각 | 유성/무성 | 전사)
전사는 시각(span 경계)의 근거다. 대사 텍스트는 아래 heard 로 당신이 들은 것을 적고, 확정은 코드에 맡긴다.
{span_table}
{reject_block}
## 과제
1. 연속한 span 들을 하나의 meaning 으로 묶어라 — "누가 무엇을 하고 있다"가 바뀌는 지점이 경계다. 이 구간의 **모든 span 이 정확히 하나의 meaning** 에 속해야 한다(빈틈·겹침 금지).
2. meaning 마다: content(한 문장) · characters(등장인물명) · importance(1~5, 이야기 기여도) · mood(한 단어).
3. span 마다: scene_script(화면 묘사 한 문장) · characters · importance(1~5) · heard(유성 span 만 — **당신이 실제로 들은 대사**를 화자와 함께 적어라. 무성 span 은 생략).
   ⚠ heard 는 전사를 베끼는 칸이 아니다. 위 전사표는 참고일 뿐이고, **들리는 대로** 적어라 — 전사가 잡음·음악에 망가져 있을 수 있다(같은 말이 수십 번 반복되는 등). 최종 대사는 코드가 전사와 당신의 heard 를 대조해 확정하니, 당신은 각색하지 말고 들은 것만 정확히 옮기면 된다.
   ⚠ speaker 는 **같은 사람에게 늘 같은 이름**을 써라 — 이름을 모르면 화면에서 구분되는 호칭(예: "빨간 옷")이라도 일관되게 붙여라. 이 이름이 자막 색을 정한다.

## 출력 (JSON 만)
{{"meanings": [
  {{"first_span": "sp0000", "last_span": "sp0004",
    "content": "…", "characters": ["이름"], "importance": 4, "mood": "긴장",
    "spans": [
      {{"id": "sp0000", "scene_script": "…", "characters": ["이름"], "importance": 3,
        "heard": [{{"speaker": "이름", "line": "들은 대사 그대로"}}]}}
    ]}}
]}}"""


def span_table(spans: Sequence[dict], window: Clip) -> str:
    """span 목록 → 프롬프트 표. 시각은 **창 상대초**다(`window_offset_sec`). 순수.

    표기는 v3 Stage 2 와 같은 모양(`id | 시각 | 유성/무성 | 전사`)이고 시각 포맷도
    `schemas.format_ts` 그대로다 — 모델이 두 단계에서 같은 표를 본다."""
    lines = []
    for sp in spans:
        kind = "유성" if sp["is_audio"] else "무성"
        text = sp.get("text") or "—"
        t0 = window_offset_sec(float(sp["t_in"]), window)
        t1 = window_offset_sec(float(sp["t_out"]), window)
        lines.append(f"{sp['id']} | {schemas.format_ts(t0)}~{schemas.format_ts(t1)} | "
                     f"{kind} | {text}")
    return "\n".join(lines)


def build_detail_prompt(window: Clip, spans: Sequence[dict], *,
                        research_context: str = "",
                        character_names: Sequence[str] | None = None,
                        reject_note: str = "") -> str:
    """10a 프롬프트. 순수·결정적.

    ⚠ 인물 사전(`character_names`)·작품 배경은 v3 Stage 2 와 같은 재료다. 화자 이름을
    맞히는 것이 이 호출의 절반이므로, 있으면 싣는다(없으면 블록 자체가 없다 — 프롬프트
    지문이 흔들리지 않게)."""
    if window.whole or window.start_sec is None or window.end_sec is None:
        raise ValueError("10a 는 구간만 본다 — 전체 첨부 창에는 프롬프트를 만들지 않는다")
    start, end = float(window.start_sec), float(window.end_sec)

    research_block = ""
    if research_context or character_names:
        parts = []
        if character_names:
            parts.append("등장인물 사전: " + ", ".join(list(character_names)[:20]))
        if research_context:
            parts.append(str(research_context).strip()[:1200])
        research_block = "## 작품 배경\n" + "\n".join(parts) + "\n\n"

    reject_block = ""
    if reject_note:
        reject_block = f"\n## ⚠ 직전 제안 반려 사유 — 전부 고쳐서 다시 내라\n{reject_note}\n"

    return DETAIL_PROMPT.format(
        abs_start=schemas.format_ts(start), abs_end=schemas.format_ts(end),
        dur=end - start, research_block=research_block,
        span_table=span_table(spans, window), reject_block=reject_block)


# ── 3) 응답 → span 노드 ─────────────────────────────────────────────────────

def detail_nodes(norm: Sequence[dict], spans: Sequence[dict]) -> dict[str, dict]:
    """검증·판정을 지난 meaning 목록 → `{span_id: 노드}`. 순수.

    노드의 열쇠는 `bridge.build_span_index(detail=…)` 가 읽는 그것들이다(계약 §0 표):
    `audio_script` · `importance` · `text_source` · `heard_text` · `scene_script` ·
    `meaning_content` · `mood`. `characters` 는 가산이다(10단계 프롬프트 재료).

    ⚠ **`conf` 는 싣지 않는다.** `adjudicate_transcript` 가 같은 이름으로 재 놓지만
    `bridge` 는 격자 단어에서 자기가 다시 잰다 — 같은 숫자의 출처를 둘로 두면 언젠가
    갈린다(E13 '베낀 수식' 교훈).

    ⚠ `importance` 가 1~5 정수가 아니면 **크게 실패한다.** `validate_stage2_response` 가
    이미 보장하는 값이라, 아니라면 우리 배선이 틀린 것이다(`bridge` 도 같은 값을 같은
    이유로 거절한다 — 여기서 먼저 죽어야 어디서 깨졌는지 보인다).
    """
    out: dict[str, dict] = {}
    for m in norm:
        for j, s in enumerate(m["spans"]):
            gsp = spans[m["first_idx"] + j]
            sid = s["span_id"]
            if sid != gsp["id"]:
                # `assemble_chunk_meanings` 가 말없이 기대는 정렬이다 — 어긋나면 다른
                # span 의 대사·화자가 이 span 에 붙는다(가장 조용한 사고).
                raise ValueError(
                    f"span 정렬이 어긋났다: 응답 {sid!r} vs 격자 {gsp['id']!r} "
                    f"(meaning first_idx={m['first_idx']} + {j})")
            imp = s.get("importance")
            if isinstance(imp, bool) or not isinstance(imp, int) or not 1 <= imp <= 5:
                raise ValueError(f"{sid}: importance 가 1~5 정수가 아니다: {imp!r} — "
                                 f"validate_stage2_response 를 지난 값이어야 한다")
            audio = bool(gsp["is_audio"])
            out[sid] = {
                # 화자는 여기서만 나온다(모듈 독스트링) — 무성 span 은 빈 목록이 정상이다.
                "audio_script": [dict(x) for x in (s.get("audio") or [])] if audio else [],
                "text_source": (s.get("text_source") if audio else None),
                "heard_text": (str(s.get("heard_text") or "") if audio else ""),
                "importance": imp,
                "scene_script": str(s.get("scene_script") or ""),
                "characters": list(s.get("characters") or []),
                "meaning_content": str(m.get("content") or ""),
                "mood": str(m.get("mood") or ""),
            }
    return out


def _speakers_in(nodes: dict[str, dict]) -> list[str]:
    """노드에서 얻은 **이름 있는** 화자 목록(정렬). 순수.

    `assemble.UNKNOWN_SPEAKERS` 를 그대로 쓴다 — `speaker_colors` 가 색을 주지 않는
    이름을 감사에 '화자를 얻었다'로 세면 계기판이 거짓말을 한다."""
    names: set[str] = set()
    for node in nodes.values():
        for line in node.get("audio_script") or []:
            name = str(line.get("speaker") or "").strip()
            if name and name.lower() not in assemble.UNKNOWN_SPEAKERS:
                names.add(name)
    return sorted(names)


# ── 4) 실행 ─────────────────────────────────────────────────────────────────

def _window_row(index: int, window: Clip) -> dict:
    return {"index": index,
            "window": [round(float(window.start_sec or 0.0), 3),
                       round(float(window.end_sec or 0.0), 3)],
            "duration_sec": round(float(window.end_sec or 0.0)
                                  - float(window.start_sec or 0.0), 3)}


def run_detail(gemini: Any, handle: Any, *, windows: Sequence[Clip], grid: dict,
               research_context: str = "",
               character_names: Sequence[str] | None = None,
               appearances: list[dict] | None = None,
               log=print) -> tuple[dict, dict]:
    """승자 구간만 `DETAIL_SAMPLE_FPS` 로 다시 듣는다 → (`{span_id: 노드}`, audit).

    창 하나 = 콜 하나(모듈 독스트링). 창마다 v3 Stage 2 의 기계를 그대로 태운다:
    `validate_stage2_response`(재질의 ≤`MAX_REASKS`) → `adjudicate_transcript`
    (각색 임계 `TRANSCRIPT_DIFF_MAX`) → `assemble_chunk_meanings` →
    `verify_time_alignment`. 값은 전부 위 상수에서 오고 여기 숫자로 적지 않는다.

    · **실패는 원판정 유지** — 그 창의 span 이 결과에서 빠지고, `bridge` 가 기본값으로
      채운다(전사 채택). 다른 창은 그대로 남는다.
    · **전량 실패도 예외를 올리지 않는다.** 10a 는 선택 단계이고 '없음'이 정상 상태다
      (6단계처럼 편을 죽이는 자리가 아니다). 대신 창별 사유가 audit 에 전량 남는다.
    · 순차 실행이다. 창은 편당 한둘이고 Pro 콜이라, 8단계 같은 병렬 예산 장치를 여기
      끌어오지 않는다(감사 순서도 창 순서 그대로여서 결정적이다).
    · `research_context`·`character_names`·`appearances` 는 **계약 시그니처에 없는 가산
      인자**다. v3 Stage 2 가 같은 재료를 받고(작품 배경·인물 사전·얼굴 클러스터), 화자
      이름을 맞히는 것이 이 호출의 절반이라 있으면 싣는다. 없으면 프롬프트에 블록 자체가
      없고 `character_check` 는 `status="skipped"` 로 남는다(커버리지 표기).

    ⚠ 우리 쪽 결함(정렬 어긋남·범위 밖 importance)은 삼키지 않고 올린다 — 모델이 준
    자료의 문제와 우리 코드의 결함은 다른 것이다.

    🛑 핸들을 삭제하지 않는다 — 6·6b·8·10a 가 공유한다(`video.py` 독스트링).
    """
    try:
        duration = float(grid["source"]["duration_sec"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("격자에서 소스 길이를 못 읽었다 "
                         "(grid['source']['duration_sec']) — 배선 오류다") from exc

    model_name = gemini.config.model_name        # Pro 슬롯(영상을 실제로 보는 호출 — CLAUDE.md)
    detail: dict[str, dict] = {}
    rows: list[dict] = []
    usage_total = {"prompt": 0, "total": 0, "calls": 0}

    for index, window in enumerate(windows or []):
        if not isinstance(window, Clip):
            raise TypeError(f"windows[{index}] 가 Clip 이 아니다: {type(window).__name__}")
        row = _window_row(index, window)
        rows.append(row)

        # ① 경계 벨트 — endOffset 은 소스를 넘어도 **조용히 클램프**된다(기획서 §7).
        #    보내고 나면 모델이 무엇을 봤는지 알 수 없으므로 보내기 전에 자른다.
        kept, belt = clips_within_source([window], duration)
        if belt:
            row["belt"] = [{**rec, "index": index} for rec in belt]
        if not kept:
            row.update(status="failed", reason=REASON_BELT_DROPPED,
                       detail=f"경계 벨트가 창을 버렸다(소스 {duration}s)")
            log(f"  [v4/detail] ⚠ 창{index} {row['window']} 건너뜀 — "
                f"{REASON_BELT_DROPPED}: {row['detail']}")
            continue
        clip = kept[0]
        row["sent_window"] = [round(float(clip.start_sec or 0.0), 3),
                              round(float(clip.end_sec or 0.0), 3)]

        # ② span 소속은 **보낼 창**으로 잰다(클램프됐으면 그 창이다 — 표와 화면이 갈리면
        #    모델이 화면에 없는 span 을 찾는다). 규칙은 v3 의 중점 반개구간 그대로.
        spans = spans_for_chunk(grid, float(clip.start_sec or 0.0), float(clip.end_sec or 0.0))
        row["spans"] = len(spans)
        row["voiced_spans"] = sum(1 for sp in spans if sp["is_audio"])
        if not spans:
            row.update(status="failed", reason=REASON_NO_SPANS,
                       detail="이 창에 중점이 드는 span 이 없다(무성 극단)")
            log(f"  [v4/detail] ⚠ 창{index} {row['window']} 건너뜀 — "
                f"{REASON_NO_SPANS}: {row['detail']}")
            continue

        # ③ 재질의 루프 — v3 Stage 2 와 같은 상한·같은 반려 재료.
        attempts: list[dict] = []
        row["attempts"] = attempts
        reject_note = ""
        nodes: dict[str, dict] | None = None
        # 재질의를 다 쓰고도 못 받았을 때 남길 사유. 파싱 실패와 계약 위반을 구분한다 —
        # 전자는 프롬프트가 아니라 **출력 예산**의 문제일 수 있다(v3-M2: thinking 이
        # 예산을 먹어 JSON 이 절단됐다). 사유가 뭉뚱그려지면 다음 손잡이를 못 고른다.
        last_reason = REASON_REJECTED
        for attempt in range(1 + MAX_REASKS):
            final = attempt == MAX_REASKS
            prompt = build_detail_prompt(clip, spans, research_context=research_context,
                                         character_names=character_names,
                                         reject_note=reject_note)
            if attempt == 0:
                row["prompt_sha"] = prompt_sha(prompt)
            log(f"  [v4/detail] 창{index} {row['window']} 요청 "
                f"(시도 {attempt + 1}/{1 + MAX_REASKS} · span {len(spans)} · "
                f"fps {DETAIL_SAMPLE_FPS:g})")
            t0 = time.time()
            usage: dict | None = None
            problems: list[str]
            notes: list[str] = []
            norm: list[dict] = []
            fatal: str | None = None
            try:
                resp, usage = call_video(
                    gemini, handle, prompt,
                    sample_fps=DETAIL_SAMPLE_FPS, clips=[clip],
                    media_resolution=None,      # 미지정 = 실측상 LOW(v3 Stage 2 와 같다)
                    max_output_tokens=DETAIL_MAX_OUTPUT_TOKENS,
                    thinking_level=gemini.config.analysis_thinking_level,
                    model=model_name, log=log)
            except VideoParseError as e:
                # 파싱 실패는 이 레포 실측의 상시 모드다 — 크래시가 아니라 **반려 재료**다.
                # ⚠ 여기서 멈추면 안 된다: v3-M2 실측에서 1차 MAX_TOKENS 절단 → JSON 파싱
                # 실패 → **재질의 2차에서 자연 회복**했다. 그 판례대로 되묻는다.
                usage = e.usage
                problems = [f"응답 JSON 파싱 실패: {e}"]
                last_reason = REASON_PARSE_FAILED
            except VideoCallError as e:
                # 호출 자체가 죽으면 되물을 것이 없다 — E11 재시도(429·5xx·네트워크)는
                # call_video 안에서 이미 끝났고, permanent 4xx 는 같은 요청을 다시 보내도
                # 같은 답이다(요금만 두 배).
                usage = getattr(e, "usage", None)
                problems = [f"호출 실패: {e}"]
                fatal = REASON_CALL_FAILED
            else:
                norm, problems, notes = validate_stage2_response(
                    resp, spans, final_attempt=final)
                if problems:
                    last_reason = REASON_REJECTED

            rec = {"attempt": attempt + 1, "elapsed_sec": round(time.time() - t0, 3),
                   "problems": problems, "notes": notes, "usage": usage}
            attempts.append(rec)
            # 콜 수는 **시도 수**다(usage 가 안 온 실패도 입력 토큰은 나갔다 — flags 의
            # `settle` 과 같은 규율). E11 재시도는 이 안에서 세지고 `usage["retries"]` 에 있다.
            usage_total["calls"] += 1
            if usage:
                for key in ("prompt", "total"):
                    value = usage.get(key)
                    if isinstance(value, int):
                        usage_total[key] += value

            if fatal is not None:
                row.update(status="failed", reason=fatal, detail=problems[0])
                log(f"  [v4/detail] ⚠ 창{index} 실패 — {fatal}: {problems[0]}")
                break
            if problems:
                log(f"  [v4/detail] 창{index} 반려 — 사유 {len(problems)}건")
                for p in problems[:5]:
                    log(f"    · {p}")
                reject_note = "\n".join(f"- {p}" for p in problems[:REJECT_NOTE_MAX])
                continue

            # ④ 전사 판정(M9-C) — 모델은 '들은 것'만 냈고 채택은 코드가 한다.
            decisions = adjudicate_transcript(norm, spans, grid.get("words"))
            meanings = assemble_chunk_meanings(norm, spans)
            row["time_alignment"] = verify_time_alignment(meanings, grid)
            row["character_check"] = character_cross_check(
                appearances, norm, spans,
                float(clip.start_sec or 0.0), float(clip.end_sec or 0.0))
            picked = {k: sum(1 for d in decisions if d["decision"] == k)
                      for k in ("transcript", "heard", "none")}
            restored = [d for d in decisions if d.get("restored")]
            row["transcript_guard"] = {
                "voiced_spans": row["voiced_spans"],
                "restored": len(restored), "details": restored[:10],
                "picked": picked,
                "broken": [d for d in decisions if d["broken"]][:10]}
            for r in restored:
                log(f"  [v4/detail] 창{index} 각색 복원 {r['span_id']} "
                    f"(diff {r.get('diff')}) → 전사 채택")
            for b in [d for d in decisions if d["decision"] == "heard"][:10]:
                log(f"  [v4/detail] 창{index} 전사 깨짐 {b['span_id']} "
                    f"({b['broken']}) → 청취 채택: {b.get('text', '')[:34]!r}")
            for b in [d for d in decisions if d["decision"] == "none"][:5]:
                log(f"  [v4/detail] ⚠ 창{index} {b['span_id']} 대사 확보 실패 "
                    f"({b['broken']}) — 자막 제외")

            nodes = detail_nodes(norm, spans)
            break
        else:
            row.update(status="failed", reason=last_reason,
                       detail="재질의 소진 — 마지막 사유: "
                              + "; ".join(attempts[-1]["problems"][:3]))
            log(f"  [v4/detail] ⚠ 창{index} 실패 — {last_reason} "
                f"(원판정 유지: span {len(spans)}개는 전사 채택으로 남는다)")

        if nodes is None:
            continue
        overlap = sorted(set(nodes) & set(detail))
        if overlap:
            # 창은 합집합·등분으로 만들어 서로 겹치지 않고, span 소속은 중점 반개구간이라
            # 한 span 이 두 창에 들 수 없다. 그런데도 겹쳤다면 창 계산이 틀린 것이다 —
            # 나중에 쓴 값이 조용히 이기면 어느 창의 화자인지 알 수 없게 된다.
            raise ValueError(f"창{index}: 다른 창과 span 이 겹친다 {overlap[:5]} — "
                             f"창은 겹치지 않아야 한다(detail_windows 합집합 규약)")
        detail.update(nodes)
        speakers = _speakers_in(nodes)
        row.update(status=STATUS_OK, detailed=len(nodes), speakers=speakers)
        log(f"  [v4/detail] 창{index} {row['window']} 완료 — span {len(nodes)}개 · "
            f"화자 {len(speakers)}명 {speakers[:6]}")

    ok = sum(1 for r in rows if r.get("status") == STATUS_OK)
    speakers = _speakers_in(detail)
    audit = {
        "of_windows": len(rows),
        "ok": ok,
        "failed": len(rows) - ok,
        "spans_detailed": len(detail),
        "voiced_detailed": sum(1 for n in detail.values() if n["audio_script"]),
        # 🛑 계기판의 핵심 — 화자를 얻었는가. 0 이면 자막이 전 줄 흰색이다(모듈 독스트링).
        "speakers": speakers,
        "text_source": {k: sum(1 for n in detail.values() if n["text_source"] == k)
                        for k in ("transcript", "heard", "none")},
        "sample_fps": DETAIL_SAMPLE_FPS,
        "window_max_sec": DETAIL_WINDOW_MAX_SEC,
        "max_output_tokens": DETAIL_MAX_OUTPUT_TOKENS,
        "transcript_diff_max": TRANSCRIPT_DIFF_MAX,
        "model": model_name,
        "usage_total": usage_total,
        "windows": rows,
    }
    if not speakers:
        # 조용히 넘어가면 '10a 를 켰는데 왜 색이 그대로지'가 된다(계약 §0 의 발견).
        audit["warning"] = ("10a 가 돌았지만 이름 있는 화자를 하나도 얻지 못했다 — "
                            "자막은 전 줄 흰색으로 나간다")
        log(f"  [v4/detail] ⚠ {audit['warning']}")
    log(f"  [v4/detail] 창 {ok}/{len(rows)} 성공 · span {len(detail)}개 · "
        f"화자 {len(speakers)}명 · 콜 {usage_total['calls']}회")
    return detail, audit
