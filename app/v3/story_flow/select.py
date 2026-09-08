"""걸음 1~3 — 주제 · 씬 · 대사 (Flash 텍스트 온리, 걸음마다 재료가 좁아진다).

재료 크기(2026-09-03 실측, 「지금 불륜이…」 1회): 종전 단일 프롬프트 79k자(span
1,373행) → 주제·씬 걸음은 meaning 52행, 대사 걸음은 고른 씬의 span 100행 안팎.

걸음 3 의 형식(2026-09-03 사용자 결정 — 반려가 아니라 **형식으로** 막는다):
비트 = 대화의 한 **구간**(첫 조각 ~ 마지막 조각, 사이 조각은 전부 들어간다) + 뺄 조각
(skip). 낱개 id 를 고르게 하면 모델이 텍스트만 보고 "그럴듯하게 붙는 두 줄"을 고른다
(실사고: 「손을 좀 비워가지고」→ 23초·6줄 건너뛴 「서로서로 도와야죠」). 구간 형식에서는
짧은 추임새(SKIP_MAX_*)는 빼서 호흡을 당길 수 있고, 그보다 긴 구멍은 **코드가 비트
경계로 승격**해 걸음 4 의 내레이션 빈칸(다리 필수)이 된다 — 반려 없이.
"""
from __future__ import annotations

from typing import Any

from app.v3.story import CONT_CHAIN_HARD_MAX
from app.v3.story_flow.common import (
    fmt_t,
    meaning_table,
    nospace_len,
    parse_meaning_id,
    reject_block,
    span_row,
    span_text,
)

PURPOSES = ("배경", "맥락", "과정", "결과", "반응")
# 4단계(2026-09-08, 갭 5·9·11): 대비형(contrast)·아이러니형(irony) 주제는 씬의 쓰임 축이 다르다 —
# 선언(설정) · 경과 · 반전(회수). event(기본)는 종전 축 그대로.
TOPIC_KINDS = ("event", "contrast", "irony")
# 리빌딩 프로토콜 v14 의 10가지 바이럴 전략(2026-09-08, 사용자 지시 — ~/premiere_claude/guides).
# 걸음 1 이 편마다 하나 고르고(모델), 걸음 2·3 프롬프트에 그 구조를 실어 준다. 코드는 id 만 본다.
STRATEGIES: dict[int, tuple[str, str]] = {
    1: ("결말 선공개형", "[결말/최고조 대사] → [발단] → [전개] → [위기]"),
    2: ("충격 폭로형", "[결정적 폭로/망언] → [주변인 경악 리액션] → [사건의 전말(과거)] → [결말]"),
    3: ("감정 폭발형", "[가장 분노/오열/웃는 대사] → [왜 이렇게 됐는지] → [결말]"),
    4: ("인지부조화/급발진형", "[가장 평온한 대사] → [0.1초 만에 파국/갈등 대사] → [발단]"),
    5: ("미스터리 떡밥형", "[의문스러운 한마디] → [내레이션의 추리/질문] → [진실 폭로(하이라이트)]"),
    6: ("제3자 관찰자/리액션 먼저형", "[주변인의 황당한 리액션] → [메인 화자들의 갈등] → [일침/결론]"),
    7: ("타임어택 카운트다운형", "[파국 직전의 긴박한 대사] → [내레이션: 정확히 X시간 전] → [점층적 갈등 고조]"),
    8: ("시점 교차/핑퐁형", "[A의 변명/주장] → [B의 반박] → [내레이션 개입] → [진짜 팩트 폭로]"),
    9: ("사이다/참교육형", "[답답한 빌런/고구마 발언] → [참다못한 사이다 일침] → [당황하는 리액션]"),
    10: ("만약에/분기점형", "[파국 결말] → [내레이션: 이때 이 말을 안 했다면?] → [결정적 말실수] → [나비효과 폭발]"),
}


def strategy_block() -> str:
    return "\n".join(f"  {k}. {name}: {shape}" for k, (name, shape) in STRATEGIES.items())


def strategy_line(sid: int | None) -> str:
    if not sid or sid not in STRATEGIES:
        return ""
    name, shape = STRATEGIES[sid]
    return f"\n적용 전략: {sid}. {name} — {shape}. 이 순서가 비트의 뼈대다(선형 서사 금지, 훅이 먼저)."
CONTRAST_PURPOSES = ("선언", "경과", "반전")
ROLES = ("hook", "build", "turn", "climax", "reaction", "ending",
         "setup", "payoff", "hook_return")   # 뒤 셋은 4단계 추가(hook_return 은 훅 조각 재사용 1회)
DIEGESIS_GATE_ROLES = ("hook", "climax")   # 갭 2: 이 역할에 상상/unclear 조각이 들면 검수 항목
MIN_SCENES, MAX_SCENES = 2, 7
TITLE_MAX_CHARS = 16          # 상단 밴드 2줄 각각의 실측 상한(story.TITLE_MAX_CHARS)
BUDGET_FLOOR_RATIO = 0.85     # 대사 합계가 예산의 이 비율 미만이면 반려(재료를 더 넣어라) —
                              # 2026-09-03: 계획이 얇으면 watch_trim 이 자를 여유가 없고
                              # 잘라내면 목표에서 더 멀어진다(50s → 46s 실사고). 호출부가 켠다.
BUDGET_TOLERANCE = 1.3        # 대사 합계가 예산의 이 배수를 넘으면 반려(그 아래는
                              # watch-trim 이 초안을 보고 덜어낸다 — 산술 트림 금지)
SKIP_MAX_VOICED_SEC = 3.0     # 구간 안에서 빼도 되는 구멍 — 유성 합계 이하면 컷(추임새)
SKIP_MAX_LINES = 2            # · 유성 조각 수 이하
JUMP_GAP_SEC = 5.0            # 비트 사이 원본 간격이 이보다 크면 '점프' — 다리 내레이션 필수
SILENT_RUN_MIN_SEC = 6.0      # 무대사 구간 목록의 하한(수작업 실측 자 — EP01 48개·1,195초)
SILENT_BLOCK_MAX = 30         # 프롬프트에 싣는 무대사 구간 수 상한(긴 것부터)
EXCLUDE_OVERLAP_RATIO = 0.5   # 제외 구간과 사건 단위가 이 비율 이상 겹치면 그 사건은 제외(2026-09-07)
# 훅 화자 금지 목록(2026-09-08, 「고소해버릴 거야」 실사고): 화면에 없는 사람의 말로 편을 열면 시청자가 누구
# 말인지 모른다. 통화 상대·미상은 훅이 될 수 없다(반려 — 다른 한마디를 골라라).
HOOK_SPEAKER_BANNED = ("통화 상대", "상대방", "전화 상대", "미상", "unknown", "?", "")
REWIND_MAX = 1                # 훅 선행·훅 회수를 뺀 되감기(다음 비트가 원본에서 앞) 상한
SILENT_BEAT_MIN_SEC = 6.0     # 비트 안 무대사 구간이 이 이상이면 제 비트로 떼어 내레이션 자리를 만든다

TOPIC_PROMPT = """당신은 리캡 쇼츠 편집자다. 아래는 한 회차의 구조 기록이다(영상은 볼 수 없고 볼 필요도 없다 — 기록이 정본이다).

## 1단계 — 주제 정하기
이 회차에서 쇼츠 한 편(목표 {target_sec:.0f}초)으로 만들 **사건 하나**를 고른다.
기준: 작품을 모르는 사람이 한 번 보고 따라갈 수 있는 사건 · 시작부터 결과(또는 떡밥)까지가 {max_sec:.0f}초 안에 닫힌다 · **대사가 촘촘한 구간**이 유리하다.
사건과 함께 **리빌딩 전략**(아래 10가지 중 하나, `strategy` 번호)을 고른다 — 원본의 시간 순서를 그대로 따르지 않는다. 오프닝 3초에 결말·하이라이트·가장 충격적인 한마디를 앞세우고, 그 한마디가 편 끝에서 되풀이되면(훅 회수) 가장 강하다.
{strategy_block} 단, **행동 또는 화면 속 글자만으로 뜻이 닫히는 구간**(도구를 꺼낸다·숨긴다·따라간다·몰래 읽는다 / 기사·메시지·게시글·문서 같은 자료화면과 그것을 보는 인물의 반응)은 대사가 없어도 주 재료가 될 수 있다 — 이때는 내레이션이 뼈대를 맡는다 · 앞뒤 회차 전개와 인과·아이러니로 이어지는 사건이면 더 좋다.

## 작품
{work_title}{research_block}

## 시퀀스 요약
{sequence_block}
{hint_block}
## 사건 단위 (id | 시각 | 길이 | importance | 분위기 | 인물 | 내용)
{meaning_block}
{silent_block}{exclude_block}{map_block}{reject_block}
## 출력 (JSON 만)
{{"topic": "이 쇼츠가 무엇에 관한 이야기인지 한 문장", "why": "고른 이유 한 문장",
  "core_meanings": ["m012", "m013"], "strategy": 3,
  "hook_line": {{"speaker": "그 말을 한 인물(재료 그대로 — 통화 상대·미상이면 그대로)", "text": "편을 여는 가장 강한 한마디(원문 그대로)"}},
  "title_draft": {{"line1": "상황", "line2": "후킹"}}}}"""

SCENES_PROMPT = """당신은 리캡 쇼츠 편집자다. 영상은 볼 수 없다 — 기록이 정본이다.

## 2단계 — 사용 씬 고르기
주제: {topic}{strategy_line}
이 사건을 **작품을 모르는 사람이 봐도 다 이해하고 재미있으려면** 어떤 씬을 보여줘야 하나. 각 씬의 쓰임을 정하라 — {purpose_axis}. 필요한 것만 {min_scenes}~{max_scenes}개, **원본 시간 순서**로. 씬 = 아래 사건 단위(id). 길이 감각: 완성본 {target_sec:.0f}초이고 씬 원본 합계는 그 2~3배까지 허용된다(다음 단계에서 대사를 골라 줄인다).
제목 두 줄도 정하라 — line1(위) = 상황·도입, line2(아래) = 후킹. 각 {title_max}자 이내, 이어 읽어 한 호흡. **대사의 화자를 틀리지 마라** — 어떤 말을 누가 했는지는 재료 기록(화자 표기)이 정본이다. '통화 상대'·'미상'으로 표기된 말을 화면 속 인물의 말로 쓰면 제목이 거짓이 된다.{hook_speaker_line} 결말을 다 말하지 마라(읽은 사람이 '그래서?'를 묻게). 아랫줄이 사건의 **결과·반전 자체**(누가 무엇을 했다/당했다)를 말해버리면 볼 이유가 사라진다 — 결과 대신 그 직전의 질문·위기를 남겨라. `title_review.line2_reveals_ending` 에 네 판정을 적고, true 면 고쳐서 내라.

## 작품
{work_title}{research_block}

## 사건 단위 (전체)
{meaning_block}
{silent_block}{exclude_block}{map_block}{reject_block}
## 출력 (JSON 만)
{{"scenes": [{{"meaning": "m012", "purpose": "{purpose_choices}", "why": "이 씬이 하는 일 한 줄"}}],
  "title": {{"line1": "…", "line2": "…"}},
  "title_review": {{"line2_reveals_ending": false}}}}"""

LINES_PROMPT = """당신은 리캡 쇼츠 편집자다. 영상은 볼 수 없다 — 기록이 정본이다.

## 3단계 — 쓸 대사 고르기
주제: {topic}{strategy_line}
제목: {title_line1} / {title_line2}{hook_line_block}
씬마다 보여줄 **대화 구간**을 비트로 잡아라. 비트 = `first`(첫 조각) ~ `last`(마지막 조각)이고 **사이 조각은 전부 들어간다** — 사람이 편집하듯 대화의 어디서 시작해 어디서 끝낼지를 정하는 것이다. 무슨 일이 왜 일어나는지를 말하는 대사가 뼈대다 — 감탄사·리액션 조각만 모으면 시청자는 무슨 얘기인지 모른다.
규칙:
- 구간 안에서 없어도 맥락이 이어지는 **짧은 추임새**는 `skip` 에 넣어 빼라(호흡을 당긴다). 단 뺀 자리의 대사가 유성 {skip_sec:.0f}초·{skip_lines}줄을 넘으면 코드가 그 자리를 비트 경계로 나누고 다음 단계가 내레이션 다리를 놓는다 — 그러니 **멀리 떨어진 대사를 한 비트에 붙이지 마라**, 비트를 새로 열어라.
- 문장 중간에서 끊지 마라 — ↪ 표시된 조각은 first/last 로 가르지 마라. `↪이어짐(연속 발화)` 로 길게 이어지는 독백은 사슬째 넣을 수 없으니 **문장이 끝나는 조각**(…습니다/…거든요/…고요 뒤)에서 끊어라.
- `[무성·인물 없음]` 조각(사물·작업·풍경 컷)이 구간 **안**에 끼어 있고 **importance 가 낮고 사건과 안 붙으면** `skip` 에 넣어라. 단서·물건·흔적·발견·자료화면처럼 이야기의 정보가 되는 무성 조각은 인물이 없어도 남긴다 — 묘사와 importance 로 판단하라.
- **주고받음이 보여야 한다**: 질문·비난이 들어가면 상대의 답도 구간 안에 있어야 한다.
- 화면만으로 뜻이 오는 무성 장면(리빌·행동)도 구간으로 잡을 수 있다(first·last 가 무성 조각). 결정적 무성 장면은 잘게 썰지 말고 이어지는 구간 하나로.
- 내레이션 자리는 여기서 만들지 않는다 — 다음 단계가 따로 만든다.
- 예산: 구간 길이 합(skip 제외) ≤ **{budget_sec:.0f}초** — 길이 열을 더해 가며 짜라. **예산을 채워라** — 85% 미만이면 반려한다(얇은 편은 다듬을 여유가 없다). 초안을 본 뒤 코드가 늘어지는 곳을 몇 초 잘라내므로 조금 넉넉한 게 맞다.
- 비트 역할: hook(관심을 끄는 장면 — 인사·자기소개·상황 설명 대사 금지) · build · turn · climax(핵심 대사는 통째로) · reaction · ending(펀치·선언·떡밥 대사 직후 뚝 — 해소·정리 장면 금지) · **hook_return**(훅 장면을 원본 순서상 제자리에서 한 번 더 — 선택).
- **훅 구조**: 관심을 끌 장면(질문·선언·폭로 — 그 한마디만 들어도 사건이 보이는 줄)을 hook 으로 **맨 앞**에 두고, 나머지 비트는 **원본 순서**로 "그 장면이 어쩌다 나오게 됐는지"를 보여준다. 훅이 원본에서 뒤에 있으면 앞으로 가져오고(되감기는 내레이션이 잇는다), 원본 순서가 그 장면에 다시 닿으면 `hook_return` 으로 한 번 더 보여줄 수 있다(훅 조각만 · 1회 · 길이 ≤ 훅). 훅 뒤에 오는 장면은 훅 뒤에 두어야 자연스럽다 — 순서를 뒤섞지 마라.
- 비트는 **hook 이 맨 앞**(원본에서 뒤여도 — 코드가 앞으로 옮기고 되감기는 내레이션이 잇는다), 나머지는 원본 시간 순서(hook_return 포함). 구간끼리 겹치지 않게.

## 재료 (씬별 · id | 유성/무성 길이 | importance | 내용)
{material_block}
{reject_block}
## 출력 (JSON 만)
{{"beats": [{{"scene": "m012", "role": "hook", "first": "sp0100", "last": "sp0107", "skip": ["sp0103"],
             "action": "무슨 일이 일어나는지 동사구"}}]}}"""


# ── 검증(순수) ─────────────────────────────────────────────────────────────

def validate_topic(resp: Any, rows: list[dict], *,
                   excluded: set[int] | None = None) -> tuple[dict | None, list[str]]:
    """excluded: 이미 만든 편의 사건 단위 idx(제외 구간·주제 — `excluded_meaning_ids`).
    core_meanings 가 하나라도 거기 들면 반려(프롬프트 지시만으로는 모델이 같은 사건으로
    되돌아온다 — 형식으로 막는다)."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"]
    problems: list[str] = []
    topic = str(resp.get("topic") or "").strip()
    if not topic:
        problems.append("topic 이 비었다")
    known = {r["idx"] for r in rows}
    core = []
    for v in resp.get("core_meanings") or []:
        k = parse_meaning_id(v)
        if k is not None and k in known and k not in core:
            core.append(k)
    hit = [k for k in core if excluded and k in excluded]
    if hit:
        problems.append("이미 만든 쇼츠의 사건이다 — 제외 목록의 사건 단위("
                        + "/".join(f"m{k:03d}" for k in hit)
                        + ")를 핵심으로 고르지 마라. 다른 사건을 골라라")
    td = resp.get("title_draft") if isinstance(resp.get("title_draft"), dict) else {}
    # 4단계 additive — kind(없으면 event) · contrast/irony 는 setup·payoff 사건 단위 필수(setup 이 앞)
    kind = str(resp.get("kind") or "event").strip()
    if kind not in TOPIC_KINDS:
        problems.append(f"kind {kind!r} — {'/'.join(TOPIC_KINDS)} 중 하나")
    setup = parse_meaning_id(resp.get("setup")) if resp.get("setup") is not None else None
    payoff = parse_meaning_id(resp.get("payoff")) if resp.get("payoff") is not None else None
    if kind in ("contrast", "irony"):
        if setup is None or setup not in known or payoff is None or payoff not in known:
            problems.append(f"{kind} 주제는 setup·payoff 사건 단위(m012 형식)가 둘 다 필요하다")
        else:
            by_idx = {r["idx"]: r for r in rows}
            if not by_idx[setup]["t0"] < by_idx[payoff]["t0"]:
                problems.append(f"setup(m{setup:03d}) 이 payoff(m{payoff:03d}) 보다 앞이어야 한다")
            for k in (setup, payoff):
                if k not in core:
                    core.append(k)
            core.sort()
    if not core:
        problems.append("core_meanings 에 아는 사건 단위 id 가 없다(m012 형식)")
    strategy = None
    if resp.get("strategy") is not None:
        try:
            strategy = int(resp.get("strategy"))
        except (TypeError, ValueError):
            strategy = None
        if strategy not in STRATEGIES:
            problems.append(f"strategy {resp.get('strategy')!r} — 1~10 중 하나")
    hl = resp.get("hook_line")
    hook_speaker = ""
    _hook_given = bool(hl)
    if isinstance(hl, dict):
        hook_speaker = str(hl.get("speaker") or "").strip()[:40]
        hook_line = str(hl.get("text") or "").strip()[:80]
    else:
        hook_line = str(hl or "").strip()[:80]
        if ":" in hook_line and len(hook_line.split(":", 1)[0]) <= 12:
            hook_speaker, hook_line = (x.strip() for x in hook_line.split(":", 1))
    if _hook_given and (hook_speaker.strip() in HOOK_SPEAKER_BANNED or not hook_speaker.strip()):
        problems.append(f"hook_line 의 화자가 {hook_speaker!r} — 훅은 **화면에 있는 인물**의 말이어야 한다(통화 상대·미상 불가). "
                        "화자를 재료 표기대로 적고, 그런 화자면 다른 한마디를 골라라")
    if problems:
        return None, problems
    return {"topic": topic, "why": str(resp.get("why") or "").strip(),
            "core_meanings": sorted(core),
            "title_draft": {"line1": str(td.get("line1") or "").strip(),
                            "line2": str(td.get("line2") or "").strip()},
            "kind": kind,
            **({"strategy": strategy} if strategy else {}),
            **({"hook_line": hook_line} if hook_line else {}),
            **({"hook_speaker": hook_speaker} if hook_speaker else {}),
            **({"setup": setup, "payoff": payoff} if kind != "event" else {})}, []


def purpose_axis(kind: str) -> tuple[tuple[str, ...], str, str]:
    """주제 종류 → (쓰임 화이트리스트, 프롬프트 축 설명, 출력 선택지). event 는 종전 문구 그대로."""
    if kind in ("contrast", "irony"):
        return (CONTRAST_PURPOSES,
                "선언(설정 — 인물이 단언·약속·조건을 세우는 장면) · 경과(그 사이 벌어진 일 — 짧게) · "
                "반전(회수 — 단언이 번복되고 조건이 뒤집히는 장면). 선언과 반전을 **나란히** 보여주는 것이 이 편의 재미다",
                "선언|경과|반전")
    return (PURPOSES,
            "배경(왜 이 상황인지) · 맥락(인물 관계·무엇이 걸렸는지) · 과정(사건 진행) · 결과(정점·반전) · 반응(리액션·여운)",
            "배경|맥락|과정|결과|반응")


def validate_scenes(resp: Any, rows: list[dict], *,
                    title_max: int = TITLE_MAX_CHARS,
                    excluded: set[int] | None = None,
                    purposes: tuple[str, ...] = PURPOSES) -> tuple[dict | None, list[str], list[str]]:
    """purposes(4단계): 주제 종류별 쓰임 축 — 기본은 종전 PURPOSES(폴백 '과정'), contrast 는 '경과'."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []
    problems: list[str] = []
    notes: list[str] = []
    by_idx = {r["idx"]: r for r in rows}
    default_purpose = "과정" if purposes is PURPOSES or "과정" in purposes else purposes[1]
    scenes: list[dict] = []
    seen: set[int] = set()
    for k, s in enumerate(resp.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        idx = parse_meaning_id(s.get("meaning"))
        if idx is None or idx not in by_idx:
            notes.append(f"scenes[{k}] 모르는 사건 단위 {s.get('meaning')!r} 무시")
            continue
        if idx in seen:
            notes.append(f"scenes[{k}] 중복 m{idx:03d} 무시")
            continue
        seen.add(idx)
        purpose = str(s.get("purpose") or "").strip()
        if purpose not in purposes:
            notes.append(f"scenes[{k}] purpose {purpose!r} → {default_purpose}")
            purpose = default_purpose
        scenes.append({"meaning": idx, "purpose": purpose,
                       "why": str(s.get("why") or "").strip()[:120]})
    ex_hit = [s["meaning"] for s in scenes if excluded and s["meaning"] in excluded]
    if ex_hit:
        problems.append("제외 목록의 사건 단위(" + "/".join(f"m{k:03d}" for k in ex_hit)
                        + ")를 씬으로 썼다 — 이미 만든 쇼츠의 장면은 배경·반응으로도 쓰지 마라")
    if len(scenes) < MIN_SCENES:
        problems.append(f"씬이 {len(scenes)}개 — 최소 {MIN_SCENES}개(배경/맥락 + 과정/결과)")
    if len(scenes) > MAX_SCENES:
        problems.append(f"씬이 {len(scenes)}개 — 최대 {MAX_SCENES}개, 덜 중요한 씬을 빼라")
    order = [s["meaning"] for s in scenes]
    if order != sorted(order):
        notes.append("씬이 시간순이 아니다 — 원본 순서로 정렬")
        scenes.sort(key=lambda s: s["meaning"])
    title = resp.get("title") if isinstance(resp.get("title"), dict) else {}
    l1 = str(title.get("line1") or "").strip()
    l2 = str(title.get("line2") or "").strip()
    if not l1 or not l2:
        problems.append("title 은 {line1, line2} 두 줄 모두 필요")
    for name, line in (("line1", l1), ("line2", l2)):
        if len(line) > title_max:
            problems.append(f"title.{name} 이 {len(line)}자 — {title_max}자 이내로")
    # 스포 판정은 모델의 것(title_review) — 코드는 되돌려 보낼 뿐
    tr = resp.get("title_review") if isinstance(resp.get("title_review"), dict) else {}
    if tr.get("line2_reveals_ending") is True:
        problems.append(f"제목 아랫줄이 결말을 말한다(네 판정) — 결과 대신 직전의 질문·위기로 "
                        f"다시 써라: {l2!r}")
    if problems:
        return None, problems, notes
    return {"scenes": scenes, "title": {"line1": l1, "line2": l2}}, [], notes


def _span_sec(ids: list[str], span_index: dict[str, dict]) -> float:
    return sum(span_index[x]["t_out"] - span_index[x]["t_in"] for x in ids)


def _voiced_sec(ids: list[str], span_index: dict[str, dict]) -> float:
    return sum(span_index[x]["t_out"] - span_index[x]["t_in"]
               for x in ids if span_index[x]["is_audio"])


def split_at_holes(beat: dict, span_index: dict[str, dict],
                   *, max_voiced_sec: float = SKIP_MAX_VOICED_SEC,
                   max_lines: int = SKIP_MAX_LINES) -> list[dict]:
    """구간(range_ids) 에서 skip 을 뺀 뒤, 뺀 구멍이 허용치를 넘는 자리에서 비트를
    나눈다(반려가 아니라 형식 — 긴 구멍은 경계가 되고 걸음 4 의 다리 빈칸이 된다).
    순수. 반환: 비트 목록(각 span_ids·skipped·hole_before)."""
    rng = beat["range_ids"]
    skip = set(beat.get("skip") or [])
    pieces: list[dict] = []
    cur: list[str] = []
    hole: list[str] = []
    cur_skipped: list[str] = []
    for sid in rng:
        if sid in skip:
            hole.append(sid)
            continue
        if cur and hole:
            voiced = [x for x in hole if span_index[x]["is_audio"]]
            if _voiced_sec(hole, span_index) > max_voiced_sec or len(voiced) > max_lines:
                pieces.append({**beat, "span_ids": cur, "skipped": cur_skipped,
                               "hole_before": None if not pieces else pieces[-1].get("_hole_after")})
                pieces[-1]["_hole_after"] = list(hole)
                cur, cur_skipped = [], []
            else:
                cur_skipped.extend(hole)
        hole = []
        cur.append(sid)
    if cur:
        pieces.append({**beat, "span_ids": cur, "skipped": cur_skipped})
    # hole_before 를 정리(직전 조각의 _hole_after)
    out: list[dict] = []
    prev_hole: list[str] | None = None
    for p in pieces:
        p = dict(p)
        p["hole_before"] = prev_hole
        prev_hole = p.pop("_hole_after", None)
        p.pop("range_ids", None)
        p.pop("skip", None)
        out.append(p)
    return out


def split_at_silent_runs(beat: dict, span_index: dict[str, dict],
                         *, min_sec: float = SILENT_BEAT_MIN_SEC) -> list[dict]:
    """비트 안 연속 무성 조각 합이 min_sec 이상이면 그 런을 **제 비트**(role 유지 · silent=True)로 떼어 낸다 —
    내레이션 앵커가 비트 앞뿐이라, 긴 무대사 화면 위에 말을 얹으려면 비트 경계가 있어야 한다
    (EP01 실사고: 게시판·SNS 14초 무대사에 내레이션 2.3초). 순수. hole_before 는 첫 조각이 갖는다."""
    ids = beat["span_ids"]
    if not ids or not any(span_index[x]["is_audio"] for x in ids):
        return [beat]                       # 통째 무대사 비트는 그대로(갭 3 경로)
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(ids):
        if span_index[ids[i]]["is_audio"]:
            i += 1
            continue
        j = i
        while j < len(ids) and not span_index[ids[j]]["is_audio"]:
            j += 1
        dur = sum(span_index[x]["t_out"] - span_index[x]["t_in"] for x in ids[i:j])
        if dur >= min_sec:
            runs.append((i, j))
        i = j
    if not runs:
        return [beat]
    out: list[dict] = []
    cur = 0
    for a, z in runs:
        if a > cur:
            out.append({**beat, "span_ids": ids[cur:a], "hole_before": beat.get("hole_before") if not out else None})
        out.append({**beat, "span_ids": ids[a:z], "silent": True, "hole_before": None if out else beat.get("hole_before")})
        cur = z
    if cur < len(ids):
        out.append({**beat, "span_ids": ids[cur:], "hole_before": None})
    return out


def compute_jumps(beats: list[dict], span_index: dict[str, dict],
                  *, gap_sec: float = JUMP_GAP_SEC) -> list[dict]:
    """비트 사이 '점프'(다리 내레이션이 필요한 자리) — 원본 간격 > gap_sec 이거나
    구멍 승격으로 나뉜 경계. 각 {before_beat, gap_sec, skipped_ids, skipped_text}."""
    jumps: list[dict] = []
    for i in range(1, len(beats)):
        a, b = beats[i - 1], beats[i]
        if not a.get("span_ids") or not b.get("span_ids"):
            continue
        gap = span_index[b["span_ids"][0]]["t_in"] - span_index[a["span_ids"][-1]]["t_out"]
        hole = b.get("hole_before") or []
        # 되감기(다음 비트가 원본에서 앞이다 — 훅 선공개·훅 회수)는 언제나 점프: 다리 내레이션 필수
        if gap < 0:
            gap = abs(span_index[a["span_ids"][0]]["t_in"] - span_index[b["span_ids"][-1]]["t_out"])
        if gap > gap_sec or hole:
            skipped = [x for x in hole if span_index[x]["is_audio"]]
            jumps.append({"before_beat": i, "gap_sec": round(gap, 2),
                          "skipped_ids": list(hole),
                          "skipped_text": [span_text(span_index[x]) for x in skipped][:6]})
    return jumps


def validate_beats(resp: Any, span_index: dict[str, dict], allowed: dict[str, int],
                   *, budget_sec: float, floor_ratio: float | None = None,
                   material_sec: float | None = None, require_dialogue: bool = False
                   ) -> tuple[list[dict] | None, list[str], list[str]]:
    """allowed: span id → 씬(meaning idx). 반환 비트: {scene, role, span_ids, skipped,
    hole_before, action}. 구간은 grid 순(pos)으로 펼치고 긴 구멍은 나눈다."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []
    problems: list[str] = []
    notes: list[str] = []
    by_scene: dict[int, list[str]] = {}
    for sid, sc in allowed.items():
        if sid in span_index:
            by_scene.setdefault(sc, []).append(sid)
    for sc in by_scene:
        by_scene[sc].sort(key=lambda x: span_index[x]["pos"])
    used: set[str] = set()
    raw_beats: list[dict] = []
    hook_range: list[str] | None = None
    hook_return_seen = False
    for k, b in enumerate(resp.get("beats") or []):
        if not isinstance(b, dict):
            problems.append(f"beats[{k}] 가 객체가 아님")
            continue
        first, last = str(b.get("first") or ""), str(b.get("last") or "")
        if first not in allowed or last not in allowed:
            problems.append(f"beats[{k}] first/last 가 재료 밖이다: {first!r}~{last!r}")
            continue
        scene = allowed[first]
        if allowed[last] != scene:
            problems.append(f"beats[{k}] first 와 last 가 다른 씬이다 — 씬마다 비트를 나눠라")
            continue
        p0, p1 = span_index[first]["pos"], span_index[last]["pos"]
        if p0 > p1:
            notes.append(f"beats[{k}] first/last 순서 뒤집힘 — 바로잡음")
            p0, p1 = p1, p0
        rng = [x for x in by_scene[scene] if p0 <= span_index[x]["pos"] <= p1]
        reused = [x for x in rng if x in used]
        role_in = str(b.get("role") or "").strip()
        hook_return = False
        if reused and role_in == "hook_return":
            # 훅 회수(4단계 · 갭 11): hook 비트의 조각을 **편당 1회** 되풀이할 수 있다(길이 ≤ 훅).
            # 재사용 조각은 덮개 후보에서도 빠진다(used_intervals 가 같은 구간을 점유물로 본다).
            hook_ids = hook_range
            if hook_ids is None:
                problems.append(f"beats[{k}] hook_return 인데 앞에 hook 비트가 없다")
                continue
            if hook_return_seen:
                problems.append(f"beats[{k}] hook_return 은 편당 하나다")
                continue
            if not set(rng) <= set(hook_ids):
                problems.append(f"beats[{k}] hook_return 은 hook 비트의 조각만 되풀이할 수 있다 {sorted(set(rng) - set(hook_ids))[:3]}")
                continue
            if _span_sec(rng, span_index) > _span_sec(hook_ids, span_index) + 1e-6:
                problems.append(f"beats[{k}] hook_return 이 hook 보다 길다")
                continue
            hook_return = True
        elif reused:
            problems.append(f"beats[{k}] 구간이 다른 비트와 겹친다 {reused[:3]} — 구간끼리 겹치지 않게")
            continue
        skip_in = [str(x) for x in (b.get("skip") or []) if isinstance(x, str)]
        skip = [x for x in skip_in if x in rng]
        if len(skip) != len(skip_in):
            notes.append(f"beats[{k}] 구간 밖 skip {len(skip_in) - len(skip)}개 무시")
        if set(skip) >= set(rng):
            problems.append(f"beats[{k}] 구간 전부를 skip 했다")
            continue
        # 문장 반토막 — 구간 경계·skip 이 ↪ 짝을 가르면 안 된다
        keep = [x for x in rng if x not in skip]
        for x in keep:
            sp = span_index[x]
            nxt, prv = sp.get("continues_to"), sp.get("continues_from")
            # 긴 사슬(연속 발화 — 인터뷰 독백)은 어디선가 끊을 수밖에 없다 → 메모(story.CONT_CHAIN_HARD_MAX)
            _sink = notes if int(sp.get("cont_chain") or 1) > CONT_CHAIN_HARD_MAX else problems
            if nxt and nxt in span_index and nxt not in keep:
                _sink.append(f"beats[{k}] 문장 반토막: {x} 는 {nxt} 로 이어진다 — 짝을 함께 넣거나 둘 다 빼라")
            if prv and prv in span_index and prv not in keep and prv not in used:
                _sink.append(f"beats[{k}] 문장 반토막: {x} 는 {prv} 에서 이어진다 — 짝을 함께 넣거나 둘 다 빼라")
        if not hook_return:
            used.update(rng)
        role = role_in
        if role not in ROLES:
            notes.append(f"beats[{k}] role {role!r} → build")
            role = "build"
        if role == "hook_return" and not hook_return:
            notes.append(f"beats[{k}] hook_return 인데 훅 조각 재사용이 아니다 → build")
            role = "build"
        if role == "hook" and hook_range is None:
            hook_range = list(rng)
        if hook_return:
            hook_return_seen = True
        raw_beats.append({"scene": scene, "role": role, "range_ids": rng, "skip": skip,
                          "action": str(b.get("action") or "").strip()[:80],
                          **({"reuse_of": "hook"} if hook_return else {})})
    if not raw_beats:
        return None, problems + ["beats 가 비었다"], notes
    # 훅 구조(2026-09-08 사용자 정의): 관심을 끄는 장면을 **맨 앞**에 두고, 그 장면이 어쩌다 나오게 됐는지를
    # 원본 순서로 보여준다. hook_return(훅 장면 되풀이)은 **원본 순서상 그 장면이 오는 자리**에 놓이고 그 뒤
    # 장면은 그대로 이어진다(맨 뒤 강제 아님 — 훅 뒷장면이 훅 앞으로 밀리던 실사고). 훅만 예외로 맨 앞.
    raw_beats.sort(key=lambda b: (0 if b["role"] == "hook" else 1,
                                  span_index[b["range_ids"][0]]["pos"],
                                  0 if b.get("reuse_of") else 1))
    # 되감기 상한(2026-09-08): 훅 선행(hook→다음)과 훅 회수는 빼고, 그 밖에 다음 비트가 원본에서 앞이면 되감기.
    # 두 번 이상 되감으면 다리 내레이션 한 줄로는 못 잇는다(EP01 실사고: 41:02→39:30→…→41:02→44:42).
    rewinds = 0
    for i in range(1, len(raw_beats)):
        a, b = raw_beats[i - 1], raw_beats[i]
        if b.get("reuse_of") or a["role"] == "hook":
            continue
        if span_index[b["range_ids"][0]]["pos"] < span_index[a["range_ids"][-1]]["pos"]:
            rewinds += 1
    if rewinds > REWIND_MAX:
        problems.append(f"되감기가 {rewinds}번 — 훅 선행·훅 회수 말고는 {REWIND_MAX}번까지다. 나머지 비트는 원본 순서대로")
    beats: list[dict] = []
    for rb in raw_beats:
        pieces = split_at_holes(rb, span_index)
        if len(pieces) > 1:
            notes.append(f"{rb['role']} 비트 안 긴 구멍 {len(pieces) - 1}곳 → 비트 {len(pieces)}개로 나눔"
                         "(다리 내레이션 자리)")
        for pc in pieces:
            sub_pieces = split_at_silent_runs(pc, span_index)
            if len(sub_pieces) > 1:
                notes.append(f"{rb['role']} 비트 안 {SILENT_BEAT_MIN_SEC:.0f}초 이상 무대사 구간 → 제 비트로 떼어 냄"
                             "(내레이션이 화면 위에 얹힐 자리)")
            beats.extend(sub_pieces)
    total = sum(span_index[x]["t_out"] - span_index[x]["t_in"]
                for b in beats for x in b["span_ids"])
    if total > budget_sec * BUDGET_TOLERANCE:
        per = " · ".join(
            f"{b['role']} {sum(span_index[x]['t_out'] - span_index[x]['t_in'] for x in b['span_ids']):.0f}s"
            for b in beats)
        problems.append(f"구간 합계 {total:.0f}초 — 예산 {budget_sec:.0f}초를 크게 "
                        f"넘는다(비트별: {per}). 구간을 좁히거나 비트를 빼서 다시 내라")
    elif floor_ratio and (material_sec is None or material_sec >= budget_sec) \
            and total < budget_sec * floor_ratio:
        # 재료(고른 씬 합계)가 예산보다 적으면 미달을 따지지 않는다 — 없는 재료를
        # 채우라 할 순 없다. 실전은 재료가 목표의 2~3배라 예산 기준이 그대로 선다.
        problems.append(f"구간 합계 {total:.0f}초 — 예산 {budget_sec:.0f}초의 {floor_ratio:.0%} 미만이다. "
                        "재료가 얇으면 완성본이 짧고 다듬을 여유도 없다 — 같은 사건 안의 "
                        "대사 구간·리액션을 더 넣어 예산을 채워라")
    # 연출 층위 게이트(갭 2, 2026-09-08 · 반려 아님 — 경고+검수 항목): hook/climax 비트에
    # 상상·회상·unclear 조각이 있으면 note 를 남기고 비트에 diegesis_flags 를 붙인다.
    # 사람 확인 지점으로 설계한다 — 모델이 혼자 100% 맞힐 문제가 아니다.
    for k, b in enumerate(beats):
        flags = {x: span_index[x].get("diegesis") for x in b["span_ids"]
                 if span_index[x].get("diegesis") and span_index[x].get("diegesis") != "actual"}
        if flags:
            b["diegesis_flags"] = flags
            if b["role"] in DIEGESIS_GATE_ROLES:
                notes.append(f"⚠ 비트 {k}({b['role']}) 에 상상/불확실 장면 "
                             f"{'/'.join(sorted(set(flags.values())))} {sorted(flags)[:3]} — 사람 확인")
    if not any(span_index[x]["is_audio"] for b in beats for x in b["span_ids"]):
        # 무대사 편(갭 3, 2026-09-07): 반려하지 않는다 — "대사 인용이 뼈대다"가 지키려던 건
        # *뼈대가 있어야 한다*이지 *대사여야 한다*가 아니다. 무대사 편에서는 걸음 4 의
        # 내레이션이 뼈대를 맡고(밀도 하한 `narration.SILENT_NARRATION_MIN_RATIO`), 여기서는
        # 표시만 한다. require_dialogue=True 는 종전 반려(회귀 가드용).
        if require_dialogue:
            problems.append("대사가 하나도 없다 — 대사 인용이 뼈대다")
        else:
            notes.append("무대사 편성 — 대사가 하나도 없다. 내레이션이 뼈대를 맡는다(걸음 4 밀도 하한)")
    if problems:
        return None, problems, notes
    return beats, [], notes


# ── 무대사 구간 목록(순수) ───────────────────────────────────────────────────
# 갭 3(2026-09-07): 무대사 구간은 meaning 표 안에 유성 사건과 섞여 있어 모델이 대사 있는
# 쪽으로 쏠린다(실측: 같은 회차 6회 중 4회가 같은 유성 사건, 지갑 66초 무대사 장면은
# 2~23초 조각으로만). 코드가 전사 단어 간격으로 재서 별도 블록으로 싣는다 — 수작업이 쓴
# 계산 그대로("6초 이상 발화 없음").

def silent_runs(words: list[dict], duration_sec: float | None = None,
                min_sec: float = SILENT_RUN_MIN_SEC) -> list[tuple[float, float]]:
    """전사 단어(t0/t1) 사이 min_sec 이상 빈 구간 → [(t0, t1)] 시간순. 순수."""
    prev = 0.0
    out: list[tuple[float, float]] = []
    for w in sorted((w for w in words or [] if isinstance(w, dict)),
                    key=lambda w: float(w.get("t0", 0.0))):
        t0, t1 = float(w.get("t0", 0.0)), float(w.get("t1", 0.0))
        if t0 - prev >= min_sec:
            out.append((round(prev, 3), round(t0, 3)))
        prev = max(prev, t1)
    if duration_sec is not None and float(duration_sec) - prev >= min_sec:
        out.append((round(prev, 3), round(float(duration_sec), 3)))
    return out


def silent_block(runs: list[tuple[float, float]], rows: list[dict],
                 *, max_items: int = SILENT_BLOCK_MAX) -> str:
    """무대사 구간 → 프롬프트 블록(사건 단위 id·최고 importance 병기). 없으면 빈 문자열
    (블록이 비면 프롬프트는 종전과 같다)."""
    if not runs:
        return ""
    picked = sorted(sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:max_items])
    lines = []
    for a, z in picked:
        hit = [r for r in rows if r["t1"] > a and r["t0"] < z]
        ids = "/".join(f"m{r['idx']:03d}" for r in hit) or "-"
        imp = max((r["importance"] for r in hit), default=0)
        lines.append(f"- {fmt_t(a)}~{fmt_t(z)} ({z - a:.0f}s) {ids} imp {imp}")
    return ("\n## 무대사 구간 (코드 실측 — 6초 이상 발화 없음 · 화면 내용은 위 사건 단위 표 참조)\n"
            "대사 기반 탐색은 이 구간을 못 본다. 행동·자료화면만으로 뜻이 닫히는 구간이면 주 재료로 쓸 수 있다(내레이션이 뼈대).\n"
            + "\n".join(lines) + "\n")


# ── 제외(이미 만든 쇼츠) ──────────────────────────────────────────────────────
# 2026-09-07 사용자 지시(가왕쇼 7화): 같은 회차로 다시 돌리되 이미 만든 장면은 빼라.
# 텍스트만 프롬프트에 실으면 모델은 같은 사건(가장 강한 후보)으로 되돌아온다 — 제외
# 구간(원본 초)과 사건 단위의 겹침으로 idx 집합을 코드가 만들고 걸음 1·2 검증기가
# **형식으로** 반려한다. 제외가 없으면 블록이 빈 문자열이라 프롬프트는 종전과 같다.

def excluded_meaning_ids(rows: list[dict], ranges,
                         *, ratio: float = EXCLUDE_OVERLAP_RATIO) -> set[int]:
    """제외 구간 [(t0, t1)] 과 사건 단위(rows: t0/t1)의 겹침이 사건 길이의 ratio 이상이면
    그 idx 를 제외 집합에 넣는다. 순수."""
    out: set[int] = set()
    for r in rows:
        length = float(r["t1"]) - float(r["t0"])
        if length <= 0:
            continue
        ov = 0.0
        for a, z in ranges or ():
            ov += max(0.0, min(float(r["t1"]), float(z)) - max(float(r["t0"]), float(a)))
        if ov / length >= ratio:
            out.add(r["idx"])
    return out


def exclude_block(topics, excluded: set[int], rows: list[dict]) -> str:
    """제외 주제(문장)·제외 사건 단위 → 프롬프트 블록. 둘 다 비면 빈 문자열."""
    topics = [str(t).strip() for t in (topics or ()) if str(t).strip()]
    if not topics and not excluded:
        return ""
    lines = ["\n## 제외 — 이미 만든 쇼츠(고르지 마라 · 배경·반응 씬으로도 쓰지 마라)"]
    for t in topics:
        lines.append(f"- 주제: {t}")
    by_idx = {r["idx"]: r for r in rows}
    for k in sorted(excluded):
        r = by_idx.get(k)
        if r is None:
            continue
        lines.append(f"- m{k:03d} [{fmt_t(r['t0'])}~{fmt_t(r['t1'])}] {str(r.get('content') or '')[:60]}")
    lines.append("이 사건과 다른 **별개의 사건**을 골라라(같은 사건의 다른 각도도 금지).")
    return "\n".join(lines) + "\n"


# ── 재료 표 ────────────────────────────────────────────────────────────────

def lines_material(scenes: list[dict], rows: list[dict],
                   span_index: dict[str, dict]) -> tuple[str, dict[str, int]]:
    """고른 씬의 span 만 — (재료 표, 허용 id → 씬 idx)."""
    by_idx = {r["idx"]: r for r in rows}
    out: list[str] = []
    allowed: dict[str, int] = {}
    for s in scenes:
        r = by_idx[s["meaning"]]
        out.append(f"\n### 씬 m{r['idx']:03d} [{s['purpose']}] {fmt_t(r['t0'])}~{fmt_t(r['t1'])} "
                   f"— {r['content']}")
        for sid in r["span_ids"]:
            sp = span_index.get(sid)
            if sp is None or sp.get("unanalyzed"):
                continue
            allowed[sid] = r["idx"]
            out.append(span_row(sid, sp))
    return "\n".join(out), allowed


def title_len_ok(title: dict, title_max: int = TITLE_MAX_CHARS) -> bool:
    return all(0 < len(str(title.get(k) or "")) <= title_max for k in ("line1", "line2"))


__all__ = ["TOPIC_PROMPT", "SCENES_PROMPT", "LINES_PROMPT", "validate_topic",
           "validate_scenes", "validate_beats", "split_at_holes", "compute_jumps",
           "lines_material", "meaning_table", "nospace_len", "reject_block", "PURPOSES",
           "silent_runs", "silent_block", "SILENT_RUN_MIN_SEC", "SILENT_BLOCK_MAX",
           "excluded_meaning_ids", "exclude_block", "EXCLUDE_OVERLAP_RATIO",
           "ROLES", "TITLE_MAX_CHARS", "BUDGET_TOLERANCE", "SKIP_MAX_VOICED_SEC",
           "SKIP_MAX_LINES", "JUMP_GAP_SEC"]
