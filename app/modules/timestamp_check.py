"""후보 구간의 **타임스탬프가 내용과 맞는지** 전사(ASR)로 대조한다.

왜 필요한가: 청크 분석이 내용은 맞게 보면서 **시간축만 어긋난** 응답을 낼 때가 있다.
2026-08-06 샤먼: 미신전 2화 실측 —

    후보(청크0/후보4)  start=830 end=887
      description "…목매달아 죽은 귀신이 장난을 치는 것을 봤다고 말한다"
      transcript  "딱 봤는데 이거 목매달아 죽은 귀신인데 얘가 장난을 치는구나 라고 봤죠."
    전사(ASR)          518.9~524.2  "딱 봤는데 이거 목매달아 죽은 귀신인데 …"   ← 실제로는 여기

같은 응답의 다른 후보는 933~997 을 주장했는데 소스는 875초까지뿐이었다(전날 렌더 실패의
원인). 즉 **한 청크의 시간축이 통째로 밀린다.** 길이·부등호 검사(renderer.sanitize_clips)는
'물리적으로 불가능한 값'만 걸러서 이걸 못 잡는다 — 범위 안에 있으면서 내용이 다른 컷은
그대로 렌더돼 **엉뚱한 장면이 조용히 발행된다.**

판정은 보수적으로 한다 — **대사가 다른 곳에서 확실히 발견될 때만** 불일치로 본다.
전사에 대사가 없거나(무성 장면·배경음) 애매하면 통과시킨다. 여기서 과잉 차단하면
멀쩡한 후보까지 버려 회차가 말라붙는다.
"""
from __future__ import annotations

import re

# 대조 파라미터 — 값의 근거는 위 실측 사례(310초 밀림)와 전사 문장 길이 분포다.
MIN_QUOTE_CHARS = 10      # 이보다 짧은 인용은 우연 일치가 잦아 대조하지 않는다
MATCH_RATIO = 0.6         # 인용 토큰의 60% 이상이 한 전사 문장에 있으면 '그 대사'로 본다
TOLERANCE_SEC = 5.0       # 후보 구간 앞뒤 이만큼은 경계 오차로 허용

_BRACKET_TIME = re.compile(r"\[\s*\d+(?:\.\d+)?\s*[~-]\s*\d+(?:\.\d+)?\s*\]")
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]+")


def normalize(text) -> str:
    """대조용 정규화 — 인용에 섞인 '[518.9~524.2]' 표기와 구두점·공백을 없앤다. 순수."""
    s = _BRACKET_TIME.sub(" ", str(text or ""))
    return _NON_WORD.sub("", s)


def quotes_of(candidate: dict) -> list[str]:
    """후보가 '이 구간에서 나온다'고 주장하는 대사들. 순수.

    transcript 는 후보 전체의 인용이고, beats[].dialogue[].line 은 비트별 대사다.
    둘 다 모델이 **그 시간에 있다고 말한** 내용이므로 대조 대상이 된다."""
    out = []
    t = candidate.get("transcript")
    if t:
        out.append(str(t))
    for beat in candidate.get("beats") or []:
        for d in beat.get("dialogue") or []:
            line = d.get("line") if isinstance(d, dict) else None
            if line:
                out.append(str(line))
    return [q for q in out if len(normalize(q)) >= MIN_QUOTE_CHARS]


def _timeline(segments: list[dict]) -> tuple[str, list[tuple[float, float]]]:
    """전사 → (이어붙인 정규화 문자열, 글자별 (start,end)). 순수.

    🛑 문장 단위로 따로 비교하면 안 된다. 전사는 짧게 쪼개져 있고 인용은 여러 문장을 이어 붙인
    것이라, 문장별 비교는 **짧은 조각이 긴 인용에 들어맞는 착시**를 만든다(0.8초짜리 조각이
    30초짜리 대사와 매칭돼 엉뚱한 시각을 가리켰다 — 2026-08-06 실측). 이어 붙여 놓고 찾으면
    인용이 실제로 연속해서 발화된 위치만 잡힌다."""
    buf, spans = [], []
    for s in segments or []:
        text = normalize(s.get("text"))
        if not text:
            continue
        try:
            st, en = float(s["start_sec"]), float(s["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        buf.append(text)
        spans.extend([(st, en)] * len(text))
    return "".join(buf), spans


def find_quote_times(quote: str, segments: list[dict]) -> list[tuple[float, float]]:
    """전사에서 그 대사가 실제로 나온 (start, end) 목록. 순수.

    긴 인용은 앞부분(MIN_QUOTE_CHARS 이상, 전체의 MATCH_RATIO 까지)만으로 찾는다 — 뒤쪽은
    전사 오인식이나 인용 축약으로 어긋나기 쉬운 반면, 앞부분이 통째로 일치하면 그 대사다."""
    joined, spans = _timeline(segments)
    q = normalize(quote)
    if not joined or len(q) < MIN_QUOTE_CHARS:
        return []
    needle = q[:max(MIN_QUOTE_CHARS, int(len(q) * MATCH_RATIO))]

    hits, at = [], joined.find(needle)
    while at >= 0:
        end_idx = min(at + len(needle) - 1, len(spans) - 1)
        hits.append((spans[at][0], spans[end_idx][1]))
        at = joined.find(needle, at + 1)
    return hits


def candidate_problem(candidate: dict, segments: list[dict], *,
                      tolerance_sec: float = TOLERANCE_SEC) -> str | None:
    """후보의 시간이 내용과 맞는가 → 문제 사유 또는 None. 순수.

    ⛔ 판정 조건은 하나다: **인용한 대사가 전사에서 발견되는데, 그 위치가 후보 구간 밖**.
    발견되지 않으면(대사 없는 장면·전사 실패) 판단하지 않는다 — 모르는 것을 틀렸다고 하지 않는다."""
    try:
        start, end = float(candidate.get("start_sec")), float(candidate.get("end_sec"))
    except (TypeError, ValueError):
        return None                      # 값 자체의 문제는 renderer.sanitize_clips 담당
    lo, hi = start - tolerance_sec, end + tolerance_sec

    for quote in quotes_of(candidate):
        hits = find_quote_times(quote, segments)
        if not hits:
            continue
        if any(lo <= s <= hi or lo <= e <= hi for s, e in hits):
            return None                  # 한 곳이라도 구간 안에서 발견되면 정상으로 본다
        s, e = hits[0]
        return (f"인용 대사가 주장 구간 [{start:.1f}, {end:.1f}] 이 아니라 "
                f"[{s:.1f}, {e:.1f}] 에 있습니다 (차이 {abs(s - start):.0f}초) — "
                f"\"{str(quote)[:24]}…\"")
    return None


def filter_candidates(candidates: list[dict], transcripts: list[dict],
                      *, tolerance_sec: float = TOLERANCE_SEC) -> tuple[list[dict], list[str]]:
    """시간축이 어긋난 후보를 걸러 (살아남은 후보, 사유 메모). 순수.

    전사는 청크별로 오지만 **후보와 같은 절대 시간축**이라 전부 합쳐서 본다 — 청크 경계
    근처의 대사가 옆 청크 전사에만 있는 경우를 놓치지 않기 위해서다."""
    segments: list[dict] = []
    for ct in transcripts or []:
        segments.extend(ct.get("segments") or [])
    if not segments:
        return list(candidates or []), []

    keep, notes = [], []
    for c in candidates or []:
        prob = candidate_problem(c, segments, tolerance_sec=tolerance_sec)
        if prob:
            notes.append(f"청크{c.get('chunk_index')}/후보{c.get('candidate_index')}: {prob}")
        else:
            keep.append(c)
    return keep, notes
