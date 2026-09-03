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
NAME_MIN_LEN = 3           # 2음절 인명은 일반명사·다른 이름과 편집거리 1 이 흔해
                           # 오탐이 크다(리뷰 확정) — 3음절부터만 본다
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


_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


def initials(s: str) -> str:
    """한글 문자열 → 초성열(한글 아닌 글자는 그대로). 순수.

    한국어 인명 오인식은 **자음이 유지되고 모음·받침이 흔들리는** 성질이 있다
    (실측: 정유지↔전유진 ㅈㅇㅈ · 분이서↔빈예서 ㅂㅇㅅ) — 거리 2 를 무턱대고
    허용하면 오탐이 늘지만, 초성이 같으면 그 위험이 크게 준다."""
    out = []
    for c in str(s):
        o = ord(c)
        out.append(_CHO[(o - 0xAC00) // 588] if 0xAC00 <= o <= 0xD7A3 else c)
    return "".join(out)


def check_names(segments: list[dict], names: list[str]) -> list[dict]:
    """A — 자막 어절 × 인명 사전. 오인식 의심 목록(경고용). 순수.

    같은 길이에서 두 규칙의 합집합(실측 튜닝):
      ① 편집거리 1 — 한 글자만 흔들린 오인식(박**처**진 ← 박서진)
      ② 초성 일치 ∧ 거리 ≤2 — 자음이 유지된 오인식(**정유지** ← 전유진).
         한국 이름은 대부분 3음절이라 ①만으로는 이 유형을 통째로 놓쳤다(M9 실측:
         "정유지!"가 화면에 나감). 길이가 다르면 다른 낱말일 확률이 높아 뺀다.
    실측 오탐: 가왕쇼·포핸즈 자막 전량에서 0(전사 전체 표본에서도 전부 실오인식)."""
    pool = [n for n in dict.fromkeys(names or []) if len(n) >= NAME_MIN_LEN]
    exact = set(dict.fromkeys(names or []))   # 사전에 **정확히** 있는 이름은 정답이다
    out: list[dict] = []
    for seg in segments or []:
        for raw in str(seg.get("text") or "").split():
            tok = raw.strip(_STRIP)
            if len(tok) < NAME_MIN_LEN or tok in exact:
                # 편집거리 1 인 출연자 쌍(박서진/박세진)에서 정확한 이름이 다른
                # 이름의 오인식으로 판정되던 결함(리뷰 확정 major)
                continue
            for nm in pool:
                if tok == nm or len(tok) != len(nm):
                    continue
                d = edit_distance(tok, nm)
                if d == 1 or (d == 2 and initials(tok) == initials(nm)):
                    out.append({"at": round(float(seg.get("start_sec") or 0), 2),
                                "token": tok, "suggest": nm,
                                "line": str(seg.get("text") or "")})
                    break
    return out


# 인명 뒤에 붙는 조사 — 긴 것부터(2026-09-03). 뗀 뒤 남는 길이가 NAME_MIN_LEN 미만이면 안 뗀다.
_JOSA = ("에게서", "한테서", "이랑", "에게", "한테", "께서", "으로", "부터", "까지", "처럼",
         "이야", "이가", "은", "는", "이", "가", "을", "를", "아", "야", "의", "도", "만",
         "과", "와", "랑", "로", "께")


def split_josa(tok: str) -> tuple[str, str]:
    for j in _JOSA:
        if tok.endswith(j) and len(tok) - len(j) >= NAME_MIN_LEN:
            return tok[:-len(j)], j
    return tok, ""


def arbitrate_name(token: str, names: list[str], heard_text: str) -> str | None:
    """받아쓰기 어절 vs 모델 청취 — **두 증인이 같은 인명을 가리킬 때만** 뒤집는다(순수).

    2026-09-03 실사고(EP01): whisper 는 '임지영이', 모델 청취(heard_text)는 '임재홍, …',
    인물표에 임재홍. 규칙이 '대사는 받아쓰기가 정본'이라 화면에 임지영이가 나갔다.
    문장 각색엔 그 규칙이 맞지만 인명은 whisper 가 더 자주 틀린다.

    코드가 하는 건 판단이 아니라 대조다 — ① 인물표에 정확히 있는 이름 ② 모델이 들은
    문장에 그 이름이 정확히 있고 ③ 받아쓰기 어절(조사 뗀 것)이 그 이름과 가깝다
    (길이 ±1 · 편집거리 ≤2). 후보가 정확히 하나일 때만 교정, 아니면 None(경고는 다른
    길). 반환값은 조사를 그대로 붙인 새 어절."""
    raw = token.strip(_STRIP)
    if not raw or not heard_text:
        return None
    stem, josa = split_josa(raw)
    if len(stem) < NAME_MIN_LEN:
        return None
    exact = set(dict.fromkeys(names or []))
    if stem in exact:
        return None
    cands = []
    for nm in dict.fromkeys(names or []):
        if len(nm) < NAME_MIN_LEN or abs(len(nm) - len(stem)) > 1:
            continue
        if edit_distance(stem, nm) <= 2 and nm in heard_text:
            cands.append(nm)
    if len(cands) != 1:
        return None
    return cands[0] + josa


def fix_span_words(words: list[dict], names: list[str], heard_text: str
                   ) -> tuple[list[dict], list[dict]]:
    """span 의 whisper 단어 목록에 arbitrate_name 을 적용한 사본 + 교정 기록."""
    out, fixes = [], []
    for w in words:
        new = arbitrate_name(str(w.get("text") or ""), names, heard_text)
        if new and new != w.get("text"):
            fixes.append({"at": round(float(w.get("t0", 0)), 2), "from": w.get("text"), "to": new})
            w = dict(w, text=new)
        out.append(w)
    return out, fixes


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
        # 줄 전체 replace 는 다른 어절의 부분 문자열까지 오염시킨다(리뷰 확정) —
        # 어절 단위로 분해해 **정확히 일치하는 어절만** 바꾸고 구두점은 보존한다
        table = {h["token"]: h["suggest"] for h in hs}
        parts, n_sub = [], 0
        for raw in str(seg["text"]).split():
            core = raw.strip(_STRIP)
            if core in table:
                parts.append(raw.replace(core, table[core], 1))
                n_sub += 1
            else:
                parts.append(raw)
        text = " ".join(parts)
        fixed.append({**seg, "text": text})
        log.append({"at": key[0], "before": key[1], "after": text,
                    "subs": [{"token": k, "suggest": v} for k, v in table.items()],
                    "n": n_sub})
    return fixed, log


def check_repetition(segments: list[dict]) -> list[dict]:
    """B — 자막 반복 환각 서명. 경고 목록(비면 정상). 순수.

    실증: 환각 구간(격자에 "육십!"×53)을 자막 빌더에 태우면 34줄이 나오고 ①이
    13줄 연속을 적발한다. 실제 자막(가왕쇼 22줄·포핸즈 19줄)은 경고 0."""
    segs = list(segments or [])

    def sig(s: dict) -> str:
        """비교 키 — 구두점·공백 차이는 같은 줄로 본다("네." vs "네")."""
        return " ".join(str(s.get("text") or "").split()).strip(_STRIP)

    out: list[dict] = []
    i = 0
    while i < len(segs):                                   # ① 연속 동일 줄
        j = i
        while j + 1 < len(segs) and sig(segs[j + 1]) == sig(segs[i]):
            j += 1
        n = j - i + 1
        if n >= RUN_MIN and sig(segs[i]):
            out.append({"kind": "run", "n": n, "text": str(segs[i].get("text")),
                        "at": round(float(segs[i].get("start_sec") or 0), 2),
                        "indexes": list(range(i, j + 1))})
        i = j + 1
    # ② 창 내 점유 — 텍스트별로 **인덱스를 병합**해 경고 1건씩(창마다 중복 발행하거나
    # 뒤 창에만 나온 줄이 새던 결함: 리뷰 확정)
    hits: dict[str, set[int]] = {}
    for k in range(0, max(0, len(segs) - WINDOW + 1)):
        win = segs[k:k + WINDOW]
        top, n = Counter(sig(s) for s in win).most_common(1)[0]
        if n >= RUN_MIN and n / len(win) >= WINDOW_SHARE and top:
            hits.setdefault(top, set()).update(
                k + x for x, s in enumerate(win) if sig(s) == top)
    for top, idxs in hits.items():
        order = sorted(idxs)
        out.append({"kind": "window", "n": len(order),
                    "text": str(segs[order[0]].get("text")),
                    "at": round(float(segs[order[0]].get("start_sec") or 0), 2),
                    "indexes": order})
    return out


def drop_repetition(segments: list[dict]) -> tuple[list[dict], list[dict]]:
    """B 예방판 — **연속 런**에 걸린 줄을 자막에서 제외한 사본 + 경고 전체. 순수.

    조용한 뭉갬 금지: 몇 줄을 왜 뺐는지 호출자가 run_log 에 남긴다. 자막 없이
    영상만 나가는 것이 "육십!" 34줄이 화면을 덮는 것보다 낫다."""
    warns = check_repetition(segments)
    # 제거는 **연속 런만** — 창 규칙은 실제 대사의 짧은 맞장구("네." 4/8줄)에도
    # 걸릴 수 있어 경고 전용으로 강등한다(리뷰 확정: 정상 자막 제거 위험)
    drop = {i for w in warns if w["kind"] == "run" for i in w.get("indexes") or []}
    kept = [s for i, s in enumerate(segments or []) if i not in drop]
    return kept, warns
