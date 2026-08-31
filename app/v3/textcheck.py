"""M9-A/B — 자막 텍스트 신뢰 검사(순수 코드 · LLM 0콜). 발주서 v3-m9.

검증자와 피검증자가 편향을 공유하면 안 된다 — Stage 2(LLM)가 환각 전사를 77/78
그대로 베낀 것이 근거다. 여기 검사는 전부 문자열·통계 연산이라 결정적이고 CI 에
상시 걸린다.

  A 인명 대조 — 리서치 인명 사전 × 자막 어절. 같은 길이·편집거리 1 = 오인식 의심
     (실측: 가왕쇼 자막 22줄에서 적중 1 "박처진"→"박서진" · 오탐 0).
     ⚠ 저확신(prob) 단독 경고는 실측 기각 — 22줄 중 10줄이 걸려("이거"·"빨리"류
     간투사) 노이즈만 만들었다.
  B 반복 그물 — ① 연속 동일 줄 ≥3 ② 창(8줄) 내 한 텍스트 점유 ≥40%.
     ③ 줄 내 어절 반복은 기각(실측 오탐: "이거 가지세요 이거"는 실제 발화).
"""
from __future__ import annotations

from collections import Counter

NAME_MAX_LEN_DIFF = 0      # 같은 길이만 비교 — 길이가 다르면 다른 말일 확률이 높다
NAME_MIN_LEN = 2           # 한 글자 이름은 일반명사와 충돌
RUN_MIN = 3                # 연속 동일 줄 임계
WINDOW = 8                 # 창 크기(줄)
WINDOW_SHARE = 0.4         # 창 내 최빈 텍스트 점유 임계
_STRIP = ".,!?…'\"·~ "


def edit_distance(a: str, b: str) -> int:
    """레벤슈타인 거리. 순수."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def check_names(segments: list[dict], names: list[str]) -> list[dict]:
    """A — 자막 어절 × 인명 사전. 오인식 의심 목록(경고용). 순수.

    같은 길이 + 편집거리 1 만 잡는다: 한국어 인명 오인식은 대개 비슷한 소리의
    한 글자가 바뀐다(박**처**진). 길이까지 다르면 다른 낱말일 확률이 높아 뺀다."""
    pool = [n for n in dict.fromkeys(names or []) if len(n) >= NAME_MIN_LEN]
    out: list[dict] = []
    for seg in segments or []:
        for raw in str(seg.get("text") or "").split():
            tok = raw.strip(_STRIP)
            if len(tok) < NAME_MIN_LEN:
                continue
            for nm in pool:
                if tok == nm or len(tok) != len(nm):
                    continue
                if edit_distance(tok, nm) == 1:
                    out.append({"at": round(float(seg.get("start_sec") or 0), 2),
                                "token": tok, "suggest": nm,
                                "line": str(seg.get("text") or "")})
                    break
    return out


def fix_names(segments: list[dict], names: list[str]) -> tuple[list[dict], list[dict]]:
    """A 승격판 — 의심 어절을 사전 값으로 교정한 사본 + 교정 기록. 순수.

    게이트(--fix-names) 뒤에서만 쓴다 — 동명이인·일반명사 충돌 위험이 남아 있어
    기본은 경고다. 원본 목록은 건드리지 않는다."""
    hits = check_names(segments, names)
    by_at: dict[tuple, list[dict]] = {}
    for h in hits:
        by_at.setdefault((h["at"], h["line"]), []).append(h)
    fixed, log = [], []
    for seg in segments or []:
        key = (round(float(seg.get("start_sec") or 0), 2), str(seg.get("text") or ""))
        hs = by_at.get(key)
        if not hs:
            fixed.append(dict(seg))
            continue
        text = str(seg["text"])
        for h in hs:
            text = text.replace(h["token"], h["suggest"])
        fixed.append({**seg, "text": text})
        log.append({"at": key[0], "before": key[1], "after": text})
    return fixed, log


def check_repetition(segments: list[dict]) -> list[dict]:
    """B — 자막 반복 환각 서명. 경고 목록(비면 정상). 순수.

    실증: 환각 구간(격자에 "육십!"×53)을 자막 빌더에 태우면 34줄이 나오고 ①이
    13줄 연속을 적발한다. 실제 자막(가왕쇼 22줄·포핸즈 19줄)은 경고 0."""
    segs = list(segments or [])
    out: list[dict] = []
    i = 0
    while i < len(segs):                                   # ① 연속 동일 줄
        j = i
        while j + 1 < len(segs) and \
                str(segs[j + 1].get("text")) == str(segs[i].get("text")):
            j += 1
        n = j - i + 1
        if n >= RUN_MIN and str(segs[i].get("text") or "").strip():
            out.append({"kind": "run", "n": n, "text": str(segs[i].get("text")),
                        "at": round(float(segs[i].get("start_sec") or 0), 2),
                        "indexes": list(range(i, j + 1))})
        i = j + 1
    for k in range(0, max(0, len(segs) - WINDOW + 1)):     # ② 창 내 점유
        win = segs[k:k + WINDOW]
        top, n = Counter(str(s.get("text")) for s in win).most_common(1)[0]
        if n >= RUN_MIN and n / len(win) >= WINDOW_SHARE and top.strip():
            if not any(o["kind"] == "window" and o["text"] == top for o in out):
                out.append({"kind": "window", "n": n, "text": top,
                            "at": round(float(win[0].get("start_sec") or 0), 2),
                            "indexes": [k + x for x, s in enumerate(win)
                                        if str(s.get("text")) == top]})
    return out


def drop_repetition(segments: list[dict]) -> tuple[list[dict], list[dict]]:
    """B 예방판 — 반복 서명에 걸린 줄을 자막에서 제외한 사본 + 제외 기록. 순수.

    조용한 뭉갬 금지: 몇 줄을 왜 뺐는지 호출자가 run_log 에 남긴다. 자막 없이
    영상만 나가는 것이 "육십!" 34줄이 화면을 덮는 것보다 낫다."""
    warns = check_repetition(segments)
    drop = {i for w in warns for i in w.get("indexes") or []}
    kept = [s for i, s in enumerate(segments or []) if i not in drop]
    return kept, warns
