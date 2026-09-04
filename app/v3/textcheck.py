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


import re as _re

_LATIN_STEM = _re.compile(r"^[A-Za-z]{2,}$")
_ANY_LATIN = _re.compile(r"[A-Za-z]")


def _is_hangul(s: str) -> bool:
    return bool(s) and all(0xAC00 <= ord(c) <= 0xD7A3 for c in s)


def _split_josa_any(tok: str) -> tuple[str, str]:
    """조사 분리 — 어간 길이 제한 없음(영문 약어는 2자가 흔하다: RC·OK)."""
    for j in _JOSA:
        if tok.endswith(j) and len(tok) - len(j) >= 1:
            return tok[:-len(j)], j
    return tok, ""


def arbitrate_latin(token: str, heard_text: str, *, prev_word: str | None = None,
                    next_word: str | None = None) -> str | None:
    """받아쓰기가 **영문 약어**로 적은 어절 vs 모델 청취 — 한글로 되돌린다(순수).

    2026-09-04 실사고(가왕쇼 7화): whisper '또 RC가 이렇게 너무 더운데', 모델 청취
    '날씨가 이렇게 너무 더운데'(prob 0.676). 인명 대조는 인물표만 봐서 못 잡았고
    각색 방어(전사 복원)가 whisper 를 정본으로 되살려 화면에 'RC가' 가 나갔다.

    두 증인 규율 그대로 — ① 받아쓰기 어간이 라틴 문자만(2자+) ② 모델 청취에는 라틴
    문자가 **하나도 없고**(모델도 영문으로 들었으면 진짜 영문이다 — 그대로) ③ 청취
    문장에서 같은 조사로 끝나는 한글 어절이 **앞뒤 이웃 어절 일치**로 자리가 잡힐 때
    (동률·무일치면 None). 반환값은 조사가 붙은 새 어절."""
    raw = token.strip(_STRIP)
    if not raw or not heard_text or _ANY_LATIN.search(heard_text):
        return None
    stem, josa = _split_josa_any(raw)
    if not _LATIN_STEM.match(stem):
        return None
    heard = [h.strip(_STRIP) for h in heard_text.split()]
    heard = [h for h in heard if h]

    def _n(x: str | None) -> str:
        return (x or "").strip(_STRIP)

    cands: list[tuple[int, str]] = []
    for j, h in enumerate(heard):
        if josa and not h.endswith(josa):
            continue
        hs = h[:-len(josa)] if josa else h
        if not _is_hangul(hs):
            continue
        score = 0
        if prev_word and j > 0 and _n(heard[j - 1]) == _n(prev_word):
            score += 1
        if next_word and j + 1 < len(heard) and _n(heard[j + 1]) == _n(next_word):
            score += 1
        if score:
            cands.append((score, h))
    if not cands:
        return None
    best = max(sc for sc, _ in cands)
    top = [h for sc, h in cands if sc == best]
    return top[0] if len(top) == 1 else None


SPELLING_MAX_PROB = 0.85   # whisper 가 이 이상 확신한 어절은 안 건드린다
SPELLING_MIN_BODY = 3      # 공통 몸통 3음절+ — 2음절 기능어('이거/이게')는 각색과 구분 불가


def arbitrate_spelling(token: str, heard_text: str, *, prob: float | None = None
                       ) -> str | None:
    """받아쓰기 **맞춤법 오인식** vs 모델 청취 — 초성이 같고 모음·받침만 흔들린 어절을
    모델 표기로 되돌린다(순수). 2026-09-04 가왕쇼 7화: whisper '때양볕이라서'(0.64),
    모델 청취 '뙤약볕이라'. 사전으로는 못 푼다(다음엔 꿍치다·힐끔힐끔·밀당이 빠진다).

    인명 대조(initials)와 같은 통찰 — 한국어 오인식은 자음이 유지되고 모음·받침이
    흔들린다. LLM 은 철자를 알고 whisper 는 소리만 안다. 규칙(두 편 4,190어절 드라이런):
      ① 둘 다 한글 · 공통 몸통 ≥3음절 · 초성열 동일
      ② 모음/받침 차이 1~2음절, **몸통 마지막 음절 제외** — 마지막 음절 차이는
         어미·조사·시제(벗어주시고/벗어주실, 신발이/신발을, 나오는데/나왔는데)라
         각색이지 오인식이 아니다. 이 조건이 오작동 12/34 를 0 으로 만들었다.
      ③ whisper prob < SPELLING_MAX_PROB · 청취 문장 안 후보가 정확히 하나
    whisper 의 꼬리(어미 '이라서')는 그대로 두고 몸통만 바꾼다 — 타이밍·문법은
    받아쓰기 것이다. 실측 적중 20건 중 명백히 옳음 18 · 무해한 애매 2 · 깨짐 0."""
    raw = token.strip(_STRIP)
    if not raw or not heard_text or not _is_hangul(raw):
        return None
    if prob is not None and float(prob) >= SPELLING_MAX_PROB:
        return None
    heard = [h.strip(_STRIP) for h in heard_text.split()]
    heard = [h for h in heard if h]
    if raw in heard:
        return None
    cands: set[str] = set()
    for h in heard:
        if not _is_hangul(h):
            continue
        L = min(len(raw), len(h))
        if L < SPELLING_MIN_BODY or initials(raw[:L]) != initials(h[:L]):
            continue
        diff = [i for i in range(L) if raw[i] != h[i]]
        if not 1 <= len(diff) <= 2 or diff[-1] >= L - 1:
            continue
        cands.add(h[:L] + raw[L:])
    return cands.pop() if len(cands) == 1 else None


# ── 정렬 기반 맞춤법 교정 (2026-09-04, 지금불륜 EP01 실사고 후속) ──────────────
# arbitrate_spelling 은 어절 1:1 대조라 whisper 와 모델의 **띄어쓰기가 다르면 후보 자체가
# 없고**(이삿덕이요 ↔ 이사 떡이요 · 청년녀 중이구나 ↔ 청남여중이구나 · 빙그레샹 ↔ 빙그레 썅),
# 초성이 다르면 제외라(들이러 ↔ 드리러 ㅇ/ㄹ · 청년녀 ↔ 청남여 ㄴ/ㅇ) 쇼츠 자막 오타 6건 중
# 1건만 잡았다 — 모델 청취는 6/6 정답이었다. 공백을 뗀 두 문자열을 글자 단위로 정렬해
# whisper 어절에 대응하는 청취 조각을 찾고, **음절 수가 같고 자모 차이가 작을 때만** 뒤집는다.

ALIGNED_MAX_PROB = 0.90        # spelling(0.85)보다 조금 넓다 — 들이러·무실하잖아가 0.87
ALIGNED_MAX_JAMO = 2           # 2음절 어절의 자모 차이 상한(초·중·종 단위) — 드라이런 1,532span:
                               # 3 이상은 각색이 대부분(왔다→어디 3 · 치면→취미 3 · 이제→내가 4)
ALIGNED_MAX_JAMO_LONG = 3      # 3음절 이상은 3 까지(청년녀→청남여 · 개첩한→개차반 · 하연아→하여간)
ALIGNED_MAX_JAMO_PER_SYL = 2   # 한 음절이 통째로 바뀌면(3) 다른 말이다
# 지시어·대명사·감탄사는 각색(다른 말로 바꿔 말함)과 오인식을 구분할 수 없다 — 드라이런
# 오작동이 전부 이 부류였다(여기→이거 · 이건→그건 · 저기→여기 · 이게→이거 · 니가→네가).
# 마지막 음절만 다를 때 '어미·조사 각색'으로 보는 음절 — whisper 쪽이든 청취 쪽이든 이 집합에
# 들면 초성 변화만 허용한다(버려/버렸 · 방이/방에 · 찍으시게/겠 · 신발이/을). 둘 다 밖이면
# 명사의 오인식이라 자모 ≤2 를 전부 허용한다(행정법→행정반 · 2026-09-04 신병4 실측 누락).
JOSA_EOMI_SYLLABLES = frozenset(
    "이 가 을 를 은 는 에 의 도 로 와 과 랑 고 서 게 겠 요 죠 다 니 지 까 네 며 데 면 려 렸 셔 져 "
    "라 야 어 아 오 든 던 걸 건 거 것 만 뿐 임 함 음 됨".split())
ALIGNED_STOPWORDS = frozenset({
    "이거", "이게", "이건", "그거", "그게", "그건", "저거", "저게", "저건",
    "여기", "저기", "거기", "아니", "아이", "니가", "네가", "내가", "우리", "어디",
    # 시간 지시어·감탄사 — 규칙 완화(어미 아닌 마지막 음절) 드라이런에서 이제→이따 · 아유→아주 가
    # 걸렸다. 둘 다 뜻이 다른 말이라 각색과 구분 불가.
    "이제", "이따", "지금", "아까", "아유", "아우", "아휴",
})


def _jamo(c: str) -> tuple[int, int, int] | None:
    o = ord(c)
    if not 0xAC00 <= o <= 0xD7A3:
        return None
    o -= 0xAC00
    return (o // 588, (o % 588) // 28, o % 28)


def _jamo_diff(a: str, b: str) -> tuple[int, bool]:
    """같은 길이 두 한글 문자열의 자모 차이 개수와 '모음 차이 포함' 여부. 순수."""
    n, vowel = 0, False
    for x, y in zip(a, b):
        jx, jy = _jamo(x), _jamo(y)
        if jx is None or jy is None:
            return 99, True
        for k in range(3):
            if jx[k] != jy[k]:
                n += 1
                if k == 1:
                    vowel = True
    return n, vowel


def align_tokens_to_heard(tokens: list[str], heard_text: str) -> list[dict | None]:
    """whisper 어절 목록 ↔ 모델 청취 문장을 **공백 제거 문자열**로 정렬해 어절마다 대응
    정보를 돌려준다(대응이 없거나 길이가 다르면 None). 순수.
      piece     대응 청취 조각(같은 길이·연속)
      at_start  조각 시작이 청취 어절 경계인가
      at_end    조각 끝이 청취 어절 경계인가
      sent_end  at_end 이고 그 청취 어절이 문장부호(.?!…)로 끝나는가 — 줄 끊기 힌트
      prefix    같은 청취 어절 안에서 조각 **바로 앞**의, whisper 어절 어디에도 대응하지 않는
                글자들(whisper 가 빠뜨린 음절 — '한' ↔ '과한' 의 '과'). 없으면 ""
    """
    import difflib
    toks = [t.strip(_STRIP) for t in tokens]
    W = "".join(toks)
    raw_hw = [h for h in heard_text.split() if h.strip(_STRIP)]
    hw = [h.strip(_STRIP) for h in raw_hw]
    H = "".join(hw)
    if not W or not H:
        return [None] * len(tokens)
    starts, tok_of, sent_end_at = set(), [], set()
    pos = 0
    for k, h in enumerate(hw):
        starts.add(pos)
        tok_of.extend([k] * len(h))
        if any(c in ".?!…" for c in raw_hw[k][len(raw_hw[k].rstrip(_STRIP)):]):
            sent_end_at.add(pos + len(h) - 1)
        pos += len(h)
    ends = {b - 1 for b in starts if b > 0} | {len(H) - 1}   # 각 청취 어절의 마지막 글자 인덱스
    tok_start = {}
    for i, k in enumerate(tok_of):
        tok_start.setdefault(k, i)
    # W 의 각 글자 → H 의 글자 인덱스(equal 은 1:1, replace 는 같은 길이일 때만 1:1)
    wmap: list[int | None] = [None] * len(W)
    sm = difflib.SequenceMatcher(None, W, H, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                wmap[i1 + k] = j1 + k
    hmapped = {j for j in wmap if j is not None}
    out: list[dict | None] = []
    pos = 0
    for t in toks:
        idx = [wmap[pos + k] for k in range(len(t))]
        pos += len(t)
        if not t or any(i is None for i in idx):
            out.append(None)
            continue
        if any(idx[k + 1] != idx[k] + 1 for k in range(len(idx) - 1)):
            out.append(None)      # 사이에 청취 쪽 삽입 — 한 조각이 아니다
            continue
        j0, j1 = idx[0], idx[-1]
        pre = ""
        ts = tok_start[tok_of[j0]]
        if j0 > ts and all(j not in hmapped for j in range(ts, j0)):
            pre = H[ts:j0]
        out.append({"piece": H[j0:j1 + 1], "at_start": j0 in starts, "at_end": j1 in ends,
                    "sent_end": j1 in ends and j1 in sent_end_at, "prefix": pre})
    return out


def arbitrate_aligned(token: str, piece: dict | None, *,
                      prob: float | None = None) -> str | None:
    """정렬로 찾은 청취 조각으로 whisper 어절을 뒤집을지 판정. 순수.
      ① 둘 다 한글 · 2음절 이상 · 음절 수 같음(정렬이 보장) · 다름 · 지시어 아님
      ② 조각 시작이 청취 어절 경계(청취 어절 **가운데**에서 시작하는 조각은 whisper 가
         띄어쓰기를 다르게 끊은 것 — '가운이 보니까' ↔ '가운 입으니까' 의 '으니까')
      ③ 자모 차이 합 ≤ 2(2음절) / ≤ 3(3음절+), 음절당 ≤ 2
      ④ 마지막 음절만 다르고 그 음절(어느 쪽이든)이 어미·조사 음절(JOSA_EOMI_SYLLABLES)이면
         **초성 차이만** 허용(로컴→로펌) — 모음·종성 차이는 각색(신발이/을 · 버려/버렸 ·
         찍으시게/겠 · 가운이/입). 둘 다 어미·조사가 아니면 명사 오인식(행정법→행정반) — ③만 본다.
      ⑤ whisper prob < ALIGNED_MAX_PROB
    구두점은 whisper 것을 그대로 남긴다(타이밍·문장부호는 받아쓰기 것)."""
    raw = token.strip(_STRIP)
    if not raw or not piece:
        return None
    heard_piece, at_start = piece["piece"], piece["at_start"]
    if raw == heard_piece or len(raw) < 2 or raw in ALIGNED_STOPWORDS or not at_start:
        return None
    if not (_is_hangul(raw) and _is_hangul(heard_piece)) or len(raw) != len(heard_piece):
        return None
    if prob is not None and float(prob) >= ALIGNED_MAX_PROB:
        return None
    n, _ = _jamo_diff(raw, heard_piece)
    if not 1 <= n <= (ALIGNED_MAX_JAMO_LONG if len(raw) >= 3 else ALIGNED_MAX_JAMO):
        return None
    diff = [i for i in range(len(raw)) if raw[i] != heard_piece[i]]
    if any(_jamo_diff(raw[i], heard_piece[i])[0] > ALIGNED_MAX_JAMO_PER_SYL for i in diff):
        return None
    if diff == [len(raw) - 1] and (raw[-1] in JOSA_EOMI_SYLLABLES
                                   or heard_piece[-1] in JOSA_EOMI_SYLLABLES):
        jx, jy = _jamo(raw[-1]), _jamo(heard_piece[-1])
        if jx is None or jy is None or jx[1:] != jy[1:]:
            return None           # 초성만 다를 때만(로컴→로펌) — 모음·종성은 어미·조사 각색
    head = token[:len(token) - len(token.lstrip(_STRIP))]
    tail = token[len(token.rstrip(_STRIP)):]
    return head + heard_piece + tail


def fix_span_words(words: list[dict], names: list[str], heard_text: str
                   ) -> tuple[list[dict], list[dict]]:
    """span 의 whisper 단어 목록에 arbitrate_name(인명) → arbitrate_latin(영문 오인식) →
    arbitrate_spelling(맞춤법)을 적용한 사본 + 교정 기록(인명 외는 kind 표시).
    names 가 비어도 latin·spelling 대조는 돈다."""
    out, fixes = [], []
    pieces = align_tokens_to_heard([str(w.get("text") or "") for w in words], heard_text)
    for i, w in enumerate(words):
        text = str(w.get("text") or "")
        new, kind = arbitrate_name(text, names, heard_text), "name"
        if not new:
            new, kind = arbitrate_latin(
                text, heard_text,
                prev_word=str(words[i - 1].get("text") or "") if i > 0 else None,
                next_word=str(words[i + 1].get("text") or "") if i + 1 < len(words) else None,
            ), "latin"
        if not new:
            new, kind = arbitrate_spelling(text, heard_text, prob=w.get("prob")), "spelling"
        if not new:
            new, kind = arbitrate_aligned(text, pieces[i], prob=w.get("prob")), "aligned"
        if new and new != w.get("text"):
            rec = {"at": round(float(w.get("t0", 0)), 2), "from": w.get("text"), "to": new}
            if kind != "name":                  # 인명 기록 모양은 종전 그대로(additive)
                rec["kind"] = kind
            fixes.append(rec)
            w = dict(w, text=new, _fixed=True)
        pre = recover_missing_prefix(str(w.get("text") or ""), pieces[i], prob=w.get("prob"))
        if pre:
            fixes.append({"at": round(float(w.get("t0", 0)), 2), "from": w.get("text"),
                          "to": pre, "kind": "prefix"})
            w = dict(w, text=pre, _fixed=True)
        if pieces[i] and pieces[i].get("sent_end") and not str(w.get("text") or "").rstrip().endswith(tuple(".?!…")):
            w = dict(w, sent_end=True)          # 줄 끊기 힌트(화면 글자는 안 바뀐다)
        out.append(w)
    out, merged = merge_split_words(out, pieces)
    fixes.extend(merged)
    for w in out:
        w.pop("_fixed", None)
    return out, fixes


def recover_missing_prefix(token: str, piece: dict | None, *, prob: float | None = None
                           ) -> str | None:
    """whisper 가 어절 **앞 음절을 빠뜨린** 경우 모델 청취로 되살린다(순수). 2026-09-04
    실사고: whisper '한 정성도 좀'(prob 0.58) ↔ 청취 '과한 정성도 좀'.
    조건: ① 정렬이 같은 청취 어절 안에서 조각 바로 앞에 **어느 whisper 어절에도 대응하지
    않는** 한 음절(prefix)을 찾았다 — 앞 whisper 어절이 그 글자에 대응하면(안+계시더라구요 ↔
    안계시더라고요) 띄어쓰기 차이지 누락이 아니다 ② 조각이 그 청취 어절 끝까지 간다(어절 =
    prefix + 조각) ③ 둘 다 한글 ④ whisper prob < ALIGNED_MAX_PROB. 구두점은 whisper 것."""
    if not piece or not piece.get("prefix") or not piece.get("at_end"):
        return None
    pre = str(piece["prefix"])
    raw = token.strip(_STRIP)
    if len(pre) != 1 or not _is_hangul(pre) or not raw or not _is_hangul(raw):
        return None
    if prob is not None and float(prob) >= ALIGNED_MAX_PROB:
        return None
    head = token[:len(token) - len(token.lstrip(_STRIP))]
    return head + pre + token[len(head):]


def merge_split_words(words: list[dict], pieces: list) -> tuple[list[dict], list[dict]]:
    """whisper 가 **한 단어를 두 어절로 끊었고** 모델 청취는 한 어절인 쌍을 합친다(순수·사본).
    2026-09-04 실사고: 청취 '청남여중이구나' ↔ whisper '청년녀 / 중이구나' — 교정은 됐지만
    줄이 '어머, 너도 청남여' / '중이구나.' 로 갈라졌다(라인 분할이 어절 단위라).

    조건(전부): ① 앞 어절 조각의 끝이 청취 어절 경계가 **아니고** 뒤 어절 조각이 그 자리에서
    이어진다(= 모델은 한 어절) ② 둘 다 한글이고 앞 어절에 구두점이 없다 ③ 둘 중 하나는
    이번 대조로 **교정된** 어절(오인식이 확인된 자리에서만 — 모델과 whisper 의 단순 띄어쓰기
    불일치('안 계시더라구요' vs '안계시더라고요')는 whisper 편). 시각은 앞 t0 ~ 뒤 t1,
    prob 는 둘 중 작은 값."""
    out: list[dict] = []
    merged: list[dict] = []
    i = 0
    while i < len(words):
        w = words[i]
        if i + 1 < len(words):
            a, b = pieces[i] if i < len(pieces) else None, pieces[i + 1] if i + 1 < len(pieces) else None
            nxt = words[i + 1]
            ta, tb = str(w.get("text") or ""), str(nxt.get("text") or "")
            if (a and b and not a["at_end"] and not b["at_start"]
                    and _is_hangul(ta) and _is_hangul(tb.rstrip(_STRIP))
                    and (w.get("_fixed") or nxt.get("_fixed"))):
                mw = dict(w, text=ta + tb, t1=nxt.get("t1", w.get("t1")),
                          prob=min(float(w.get("prob") or 1.0), float(nxt.get("prob") or 1.0)),
                          _fixed=True)
                merged.append({"at": round(float(w.get("t0", 0)), 2),
                               "from": f"{ta} {tb}", "to": ta + tb, "kind": "merge"})
                out.append(mw)
                i += 2
                continue
        out.append(w)
        i += 1
    return out, merged


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
