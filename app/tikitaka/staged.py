"""staged 대본 흐름 — 뼈대(①) → 편별 문장·화면(②) → 게이트 → 첫 3초 기록. `--script-flow staged`.

2026-09-21 사용자 결정(v3 human-flow 방식으로). 종전(single)은 한 번의 호출로 14편의 **문장과 화면을 동시에** 냈는데,
작가가 받는 재료에는 말 있는 조각의 화면 묘사가 없었다(지금불륜 3화 실측 722개 중 28개만 전달). 그래서 "써 놓고 →
잘라 보고 → 반려"가 됐다 — 5개 잡 실측: 내레이션 255문장 중 75문장 수리 · 대본 56개 중 26개 탈락.

  ① 뼈대      1회 · 회차 전체의 가벼운 재료(종전 소스 스크립트 + 눈길 끄는 화면 목록)
               → 편마다 포맷·제목·훅·대사(S)·현장음(A)의 **순서만**. 내레이션은 자리도 문장도 정하지 않는다.
  ② 문장·화면  편마다 1회 · 그 편이 쓰는 장면의 **조각 단위 화면 표**만
               → 내레이션을 **어디에 넣을지도 여기서** 정한다(원본에서 바로 붙은 질문·대답 사이에도 넣을 수 있다 —
                 티키타카의 리듬). 화면을 먼저 고르고 그 화면에 보이는 것으로 문장을 쓴다. 문장은 즉시 합성해 길이를 실측한다.
  게이트       검사는 종전과 같은 자(TTS 실측 · 같은 씬에서 넓히기 · 잘라서 프로브). 고치는 순서만 다르다:
               화면 교체 1회 → **자리의 역할은 그대로, 검사관이 본 화면 중 하나를 골라 그 내용으로 재작성** →
               빼도 이야기가 이어지는 자리는 그 내레이션만 빼고(기록), 이어야 하는 자리(점프·되감기)는 실패.
  첫 3초       반려하지 않는다 — 강도(0~100)·사유·측정값을 편마다 기록한다(`version["opening"]`).

⚠ 1차 실측(2026-09-21 지금불륜 3화)에서 뼈대가 자리마다 자유 서술 `intent` 를 적게 했더니 작품 이해 문서 말투의 줄거리
("결심"·"심경"·"속셈")가 그 칸을 타고 걸음 ② 문장으로 흘러 첫 시도 통과율이 오히려 떨어졌다(67→58%). human-flow 에는 그런
통로가 없었다 — 자리의 역할은 **위치와 코드가 단 표식**(점프·되감기)이 말한다. 원본 간격 계산은 자리를 정하는 데 쓰지 않고
**실패한 내레이션을 빼도 되는가**에만 쓴다(사용자 결정).

산출은 종전 `rebuild.json` 과 같은 모양(+`script_flow`, N 항목의 slot·intent·required)이라 하류(순위·영상 확인·
조립·렌더)는 그대로다. ⚠ 게이트의 검사 부분은 `production.enforce_joint_plans` 와 같은 수식을 **같은 헬퍼**로 부른다
(cover_runs·widen_cover_plan·probe_cover·_tts_cached) — 옛 방식을 지울 때 한 곳으로 합친다.
"""
from __future__ import annotations

import math
import re

from app.tikitaka.common import fmt_tc, ms3, MAX_SHORTS_SEC, NARRATION_CHARS_PER_SEC
from app.tikitaka.guide import in_excluded, guide_block
from app.tikitaka.digest import digest_block
from app.tikitaka.prompts import REBUILD_PROMPT
from app.tikitaka.timing import narration_plan_sec, bind_dialogue
from app.tikitaka.title import title_prompt

SCHEMA = "tikitaka_staged/v3"
SA_SHARE = (0.5, 0.75)           # 뼈대의 대사·현장음 합계가 목표 길이에서 차지할 몫 — 나머지는 걸음 ② 의 내레이션(종전 대본 실측 N 비중 30~60%)
JUMP_SEC = 5.0                   # 이 이상 건너뛰거나 장면이 바뀌거나 되감으면 '이어야 하는 자리'(v3 human-flow compute_jumps 와 같은 값)
NAR_COUNT_HINT = (3, 7)          # 편당 내레이션 개수 안내(공감형은 0~3)
EYE_MIN_IMPORTANCE = 4           # 눈길 끄는 화면 · ★ 기준(v3 HOOK_COVER_MIN_IMPORTANCE 와 같은 값)
EYE_JOIN_GAP_SEC = 0.6           # 눈길 끄는 화면 묶기 — production.COVER_JOIN_GAP_SEC 와 같은 값
SCOPE_NEIGHBOR_SCENES = 0        # 걸음 ② 화면 표: 그 편이 쓰는 장면만(1차 실측: 이웃까지 실으면 편당 2만~5만 자)
NAR_MIN_CHARS, NAR_MAX_CHARS = 4, 34   # 공백 제외. 프롬프트는 8~30자를 말하고 코드는 여유를 둔다
REASK_MAX = 2                    # 걸음 ①·② 형식 위반 재질의 횟수
OPENING_SEC = 3.0
SHORT_FORM = {"공감형": (25, 45)}   # rebuild.SHORT_FORM_STRATEGIES 와 같은 값(순환 임포트를 피해 여기 둔다 — 테스트가 둘을 묶는다)
_COVER_DIALOGUE_CHARS = 24
_WS = re.compile(r"\s+")


# ── 프롬프트 재료 — 포맷 상자·내레이션 원칙은 REBUILD_PROMPT 한 곳이 정본이다 ─────────────
def prompt_sections() -> dict[str, str]:
    """REBUILD_PROMPT 를 '## ' 머리로 쪼갠다 → {머리 문구: 절 전체}. 순수 — 테스트가 필요한 절의 존재를 고정한다."""
    out: dict[str, str] = {}
    cur, buf = "(머리)", []
    for line in REBUILD_PROMPT.splitlines():
        if line.startswith("## "):
            out[cur] = "\n".join(buf).rstrip() + "\n"
            cur, buf = line[3:].strip(), []
        buf.append(line)
    out[cur] = "\n".join(buf).rstrip() + "\n"
    return out


def _section(prefix: str) -> str:
    for head, body in prompt_sections().items():
        if head.startswith(prefix):
            return body
    raise KeyError(f"REBUILD_PROMPT 에 '{prefix}' 절이 없다 — staged 프롬프트가 그 절을 빌려 쓴다")


OUTLINE_RULES = """## [이번 걸음: 뼈대만 짠다 — 내레이션은 자리도 문장도 정하지 않는다]
- 버전마다 **어느 장면의 어느 대사(S)·현장음(A)을 어떤 순서로 쓸지**만 정한다. 내레이션(N)은 항목에 넣지 마라 — 다음 걸음이
  그 편의 화면 목록을 직접 보면서 **어디에 넣을지부터** 정하고 문장을 쓴다(붙어 있는 질문과 대답 사이에도 들어갈 수 있다).
- 그래서 **대사·현장음 합계는 {sa_min}~{sa_max}초**로 짠다(목표 {target_min}~{target_max}초에서 내레이션 몫을 남긴 길이). 공감형은 20~40초.
- 제목을 회수하는 결말 대사·반응을 먼저 확보하고 앞부분을 길이에 맞춘다. 요약 내레이션으로 결말을 대신할 생각으로 핵심 S를 빼지 마라.
- 순서를 크게 건너뛰거나 되감는 본문 자리는 다음 걸음이 내레이션으로 잇는다 — 이을 수 없을 만큼 동떨어진 조각을 나열하지 마라.
- **첫 3초(훅)**: 첫 항목은 대사(S)일 수도, 말 없는 화면(A)일 수도 있다. 아래 [눈길 끄는 화면]에 이 편의 사건에 속한 센 화면이
  있으면 **그 화면으로 열고 내레이션이 뒤에서 받는** 구성을 먼저 고려하라. 첫 3초 안에 컷이 바뀌거나 말이 시작돼야 한다 —
  서 있기·걷기·뒷모습·풍경으로 열지 마라. 내레이션으로 여는 편이면 `hook.with` 에 "N" 이라고만 적는다(문장은 다음 걸음).
  `hook.why` 에 무엇으로 여는지와 왜 센지 적는다.
- A 는 원본에서 **맞닿은** 순간을 묶어 한 덩어리로 쓸 수 있다(`moment_ids` — 3초짜리 화면 훅). 떨어진 순간은 별도 A 로.
"""

OUTLINE_VOCAB = """## 항목(items) 어휘 — **타임코드를 쓰지 마라. ID 로만 가리킨다.**
- {{"type":"S","line_ids":["L-045","L-046"],"effect":"(동공지진)"|null}}  ← 화자가 바뀌어도 의미가 이어지는 연속 줄은 함께 묶는다. 대화·듀엣의 응답과 문장 끝을 빠뜨리지 않는다. 비연속 줄은 별도 S 항목으로 나눈다. 대사 텍스트는 적지 않는다.
- {{"type":"A","moment_id":"S-012","effect":"[정적...]"|null}} 또는 {{"type":"A","moment_ids":["S-012","S-013"],"effect":null}}  ← 말 없는 화면을 원본 소리 그대로 보여 주는 턴
(N 항목은 이 걸음에 없다.)
"""

OUTLINE_OUTPUT = """## 출력 JSON (하나만, 코드블록 금지)
{{"versions": [
  {{"n": 1, "strategy": "<고른 포맷 이름 또는 자연 흐름>", "title": {{"line1": "상황·조건", "line2": "핵심 행동·반응"}}, "structure": "⑤하이라이트 → ②리액션 → ①발단 …",
    "hook": {{"with": "S|A|N", "why": "첫 3초가 왜 센가(화면·대사 근거)"}},
    "items": [ … ],
    "analysis": {{"grade": "매우 안전|안전|보통", "viral_point": "…", "comment": "…"}} }},
  … {n_versions}개 …],
 "ranking": [<조회수 기대가 높은 순으로 번호 전부>], "recommended": <ranking 의 첫 번호>, "reason": "한 줄"}}
"""

NARRATION_HEAD = """# 📜 티키타카 스크립트 리빌딩 — 걸음 ② 내레이션 문장·화면

## [System Role]
너는 이미 짜인 쇼츠 뼈대(대사·현장음의 순서)에 **접착용 티키타카 내레이션**을 써 넣는 작가이자 편집자다. 작품 「{title}」 {episode}.
이번에 쓰는 편: 버전 {n} · 포맷 {strategy} · 제목 "{title_text}" · 구조 {structure}
이 편의 훅: {hook}{guide}{digest}

## 할 일 — 내레이션을 **어디에 넣을지**부터 정하고, 화면을 고르고, 문장을 쓴다
1. [편성표]는 대사(S)·현장음(A)의 순서다. 내레이션은 **어느 항목 앞에든** 넣을 수 있다 — 원본에서 바로 붙은 질문과 대답 사이에도
   ("갑이 갑작스럽게 묻는데?" → 질문 대사 → "을의 대답은," → 대답 대사). 리듬은 [N]→[S]→[N]→[S]→[S]→[A]→[N] … 보통 {n_lo}~{n_hi}개
   (공감형은 0~3개). 이 편의 대사·현장음 합계는 {sa_sec:.0f}초다 — 내레이션까지 합쳐 {target_min}~{target_max}초(절대 상한 {hard_max}초).
2. `⚠ 이어야 하는 자리` 표식(코드가 원본 시각으로 계산: 장면이 바뀜 · {jump:g}초 이상 건너뜀 · 되감기)에는 **반드시** 내레이션을 넣는다 —
   시청자가 다음 장면을 따라오게 잇는 말이다. 본문 회상은 시간 관계를 짧게 알린다.
   루프 끝에서는 되감기 설명을 넣지 않는다. 설명이 필요한 루프는 버리고 실제 결말 대사·반응에서 끝낸다.
3. 자리를 정했으면 [쓸 수 있는 화면]에서 그 자리에 깔 화면을 **먼저** 고른다. 표의 묘사는 영상을 보고 적은 **실제로 보이는 것**이다.
   대사를 소개하는 내레이션이면 그 대사 조각 **바로 앞뒤 행**(말하려는 얼굴 · 듣는 표정)이 제 화면이다.
4. 그 화면에 보이는 것으로 문장을 쓴다. 화면에 없는 동작·물건·인물·속마음("결심했다"·"속셈")을 말하지 마라 — 네 문장과 고른 화면은 코드가
   잘라서 다시 보고 대조한다. 대사로만 전달된 일(「기사 봤어?」)은 화면이 없다 — 그 일을 말하고 싶으면 **보이는 것**(듣는 표정·손에 든
   휴대폰)을 주어로 써라.
- **길이**: 문장은 즉시 합성해 길이를 잰다(대략 공백 제외 글자수÷{cps:g}초). 고른 화면의 「묶음」 길이 안에 들어야 한다. 묶음 = 같은 장면에서
  그 조각과 맞닿아 이어 쓸 수 있는 화면의 총 길이(코드가 앞뒤로 이어 붙인다). 묶음이 짧으면 문장을 줄이거나 화면을 {max_stack}컷까지 쌓아라.
- 이 편의 **첫 항목 앞**(before: 1)에 넣는 내레이션은 훅이다 — ★ 화면에서 고른다(서 있기·걷기·뒷모습·풍경 와이드 금지). ★ 는 첫 화면에만
  쓰는 기준이고 나머지 자리는 관련성·자연스러움이 먼저다.
- `[이 편의 대사 화면]` 은 같은 화면이 두 번 나오게 되므로 피한다 — 그 사건의 화면이 정말 그것뿐일 때만.
- `[현장음 — 고를 수 없음]` 과 표에 없는 sp ID 는 고를 수 없다. 두 내레이션이 같은 화면을 쓰지 않는다. 한 자리에는 내레이션 하나.
- 내레이션이 **바로 뒤 대사의 내용을 미리 말하지 않는다**(같은 말이 두 번 나온다). 상황만 깔고 대사가 답하게.
- 결말은 제목이 약속한 실제 대사(S)·행동·반응(A)으로 보여준다. 결론을 내레이션으로 대신하지 않는다.
  after_last는 기본적으로 생략한다. 실제 결말 뒤에 꼭 필요한 새 정보가 있을 때만 쓴다. 길이가 부족하면 내레이션부터 줄인다.
"""

NARRATION_OUTPUT = """## 출력 JSON (하나만, 코드블록 금지)
{{"narrations": [
  {{"before": 1, "text": "내레이션 문장", "effect": "[자막]"|null,
    "production_plan": {{"kind": "action|rule_summary|transition|question|evaluation", "evidence_ids": ["L-044"],
                        "cover": [{{"span_id": "sp0482", "role": "evidence"}}, {{"span_id": "sp0483", "role": "support"}}]}}}},
  {{"before": 4, "text": "…", "effect": null, "production_plan": {{…}}}},
  {{"after_last": true, "text": "…", "effect": null, "production_plan": {{…}}}}]}}
(before = [편성표]의 항목 번호 — 그 항목 **앞**에 들어간다. after_last = 맨 끝.)
"""


def reject_block(problems: list[str]) -> str:
    if not problems:
        return ""
    return ("\n## ⚠ 지난 답의 문제 — 이것만 고쳐서 전체를 다시 내라\n" + "\n".join(f"- {p}" for p in problems[:20]) + "\n")


# ── 재료: 눈길 끄는 화면 · 조각 단위 화면 표 ─────────────────────────────────────────
def _scene_id_at(scenes: list[dict], t: float) -> str | None:
    return next((sc["id"] for sc in scenes if sc["start"] <= t < sc["end"]), None)


def eye_catchers(index: dict, exclude=()) -> list[str]:
    """말 없는 조각 중 importance ≥ EYE_MIN_IMPORTANCE — 걸음 ① 이 '화면으로 여는 편'을 짤 재료. 맞닿은 순간(같은 장면 ·
    틈 ≤ EYE_JOIN_GAP_SEC)은 한 묶음으로 합쳐 묶음 길이를 적는다. 순수 — 테스트 대상."""
    scenes = index.get("scenes") or []
    picks = [m for m in index.get("moments") or [] if int(m.get("importance") or 0) >= EYE_MIN_IMPORTANCE
             and not in_excluded(m["start"], m["end"], list(exclude))]
    picks.sort(key=lambda m: m["start"])
    runs: list[list[dict]] = []
    for m in picks:
        if runs and m["start"] - runs[-1][-1]["end"] <= EYE_JOIN_GAP_SEC \
                and _scene_id_at(scenes, m["start"]) == _scene_id_at(scenes, runs[-1][-1]["start"]):
            runs[-1].append(m)
        else:
            runs.append([m])
    out = []
    for run in runs:
        ids = run[0]["id"] if len(run) == 1 else f"{run[0]['id']}~{run[-1]['id']}"
        imp = max(int(m.get("importance") or 0) for m in run)
        descs = " / ".join(" ".join(str(m.get("desc") or "").split()) for m in run[:3]) + (" …" if len(run) > 3 else "")
        out.append(f"★{imp} {ids} | {_scene_id_at(scenes, run[0]['start']) or '-'} | {fmt_tc(run[0]['start'])[:-2]} | "
                   f"{run[-1]['end'] - run[0]['start']:.1f}s | {descs}")
    return out


def eye_block(index: dict, exclude=()) -> str:
    rows = eye_catchers(index, exclude)
    if not rows:
        return ""
    return ("\n## [눈길 끄는 화면] — 말 없는 화면 중 영상 분석이 중요도 4 이상으로 본 것(행: ★중요도 순간 ID | 장면 | 시작 | 길이 | 보이는 것)\n"
            "이 화면들은 A 항목으로 편을 열거나 이야기의 한 턴으로 쓸 수 있다. 묶음(S-a~S-b)은 `moment_ids` 로 통째로 쓴다.\n"
            + "\n".join(rows) + "\n")


def span_times(index: dict) -> dict[str, dict]:
    """화면 기록이 있는 모든 조각의 시각 {sid: {t_in, t_out, is_audio, importance}}. 순수."""
    from app.v3.schemas import parse_ts
    out: dict[str, dict] = {}
    for m in index.get("moments") or []:
        for sid in m.get("span_ids") or []:
            out[sid] = {"t_in": float(m["start"]), "t_out": float(m["end"]), "is_audio": False,
                        "importance": int(m.get("importance") or 0)}
    for sid, fact in (index.get("grid_facts") or {}).items():
        ts = fact.get("time") or {}
        if sid in out or "start" not in ts or "end" not in ts:
            continue
        out[sid] = {"t_in": parse_ts(ts["start"]), "t_out": parse_ts(ts["end"]), "is_audio": bool(fact.get("is_audio")),
                    "importance": int(fact.get("importance") or 0)}
    return out


def cover_table(index: dict, transcript: dict, exclude=(), *, scenes: set[str] | None = None,
                own_dialogue: list[tuple[float, float]] | None = None,
                forbidden: list[tuple[float, float]] | None = None) -> tuple[list[str], dict[str, dict]]:
    """조각(sp) 단위 「쓸 수 있는 화면」 표 — v3 human-flow 의 narration.available_block 과 같은 모양. 순수 — 테스트 대상.

    scenes = 표에 실을 장면 id(None = 전부). own_dialogue = 이 편이 대사로 쓰는 구간(표시만) · forbidden = 고를 수 없는
    구간(현장음 A). 돌려주는 info[sid] = {t_in, t_out, run(묶음 길이), run_id(묶음의 첫 조각), importance, usable, own}.
    묶음 = 같은 장면에서 고를 수 있는 조각끼리 맞닿은(틈 ≤ COVER_JOIN_GAP_SEC) 덩어리의 길이 — 게이트가 실제로 이어 붙일 수
    있는 상한이라 작가가 문장 길이를 여기에 맞춘다(종전 반려의 절반이 '화면이 문장보다 짧음')."""
    from app.tikitaka.production import cover_runs
    from app.tikitaka.grid_table import overlap
    facts = index.get("grid_facts") or {}
    all_scenes = index.get("scenes") or []
    ex = list(exclude)
    times = span_times(index)
    lines = [l for l in transcript.get("lines") or [] if not in_excluded(l["start"], l["end"], ex)]
    moment_of = {sid: m for m in index.get("moments") or [] for sid in m.get("span_ids") or []}
    info: dict[str, dict] = {}
    by_scene: dict[str | None, list[str]] = {}
    for sid, sp in times.items():
        if sid not in facts or in_excluded(sp["t_in"], sp["t_out"], ex):
            continue
        sc = _scene_id_at(all_scenes, (sp["t_in"] + sp["t_out"]) / 2)
        if scenes is not None and sc not in scenes:
            continue
        info[sid] = {**sp, "scene": sc, "own": overlap(sp["t_in"], sp["t_out"], own_dialogue or []),
                     "usable": not overlap(sp["t_in"], sp["t_out"], forbidden or []), "run": 0.0, "run_id": sid}
        by_scene.setdefault(sc, []).append(sid)
    for sc, ids in by_scene.items():
        for run_ids, length in cover_runs([s for s in ids if info[s]["usable"]], info):
            for s in run_ids:
                info[s]["run"], info[s]["run_id"] = round(length, 2), run_ids[0]
    out: list[str] = []
    cur = object()
    for sid in sorted(info, key=lambda s: (info[s]["t_in"], s)):
        sp = info[sid]
        if sp["scene"] != cur:
            cur = sp["scene"]
            head = next((f"{x['id']} — {x.get('summary', '')}" for x in all_scenes if x["id"] == cur), "(장면 미분류)")
            out.append(f"### {head}")
        fact = facts[sid]
        desc = " ".join(str(fact.get("scene_script") or "").split()) or "(묘사 없음)"
        if sp["is_audio"]:
            hit = [l for l in lines if (l["start"] < sp["t_out"] and l["end"] > sp["t_in"])
                   or (l["end"] <= l["start"] and sp["t_in"] <= l["start"] < sp["t_out"])]
            said = " ".join(l["text"] for l in hit)
            said = said[:_COVER_DIALOGUE_CHARS] + ("…" if len(said) > _COVER_DIALOGUE_CHARS else "")
            tag = "유성 " + ("+".join(l["id"] for l in hit) if hit else "(줄 없음)")
            desc += f" (대사: {said})" if said else ""
        else:
            tag = f"무성 {moment_of[sid]['id']}" if sid in moment_of else "무성"
        if fact.get("screen_text"):
            desc += f" 📄\"{str(fact['screen_text'])[:40]}\""
        if fact.get("diegesis", "actual") != "actual":
            desc += f" ⚠{fact['diegesis']}"
        star = "★ " if sp["importance"] >= EYE_MIN_IMPORTANCE else ""
        mark = "" if sp["usable"] else " [현장음 — 고를 수 없음]"
        mark += " [이 편의 대사 화면]" if sp["own"] and sp["usable"] else ""
        run = f"묶음 {sp['run']:.1f}s" if sp["usable"] else "-"
        out.append(f"{sid} | {fmt_tc(sp['t_in'])[:-2]} | {sp['t_out'] - sp['t_in']:.1f}s | {run} | {star}{tag} | {desc}{mark}")
    return out, info


COVER_TABLE_HEADER = ("## 쓸 수 있는 화면 (조각 단위)\n"
                      "행: sp id | 시작 | 길이 | 묶음(같은 장면에서 이어 쓸 수 있는 총 길이) | ★=중요도 4+ · 유성(그 조각의 대사 줄)/무성(순간 ID) | 실제로 보이는 것\n"
                      "N 이 나오는 동안 원음은 꺼지므로 유성 조각도 덮개로 쓸 수 있다.")


# ── 걸음 ① 뼈대 ─────────────────────────────────────────────────────────────────
def outline_prompt(*, title: str, episode_label: str, duration_label: str, script: str, guide: dict | None, digest: dict | None,
                   material_note: str, seq_hook_rule: str, eye: str, n_versions: int, target_min: int, target_max: int) -> str:
    kw = dict(title=title, episode_label=episode_label, duration_label=duration_label, target_min=target_min, target_max=target_max,
              hard_max=int(MAX_SHORTS_SEC), material_note=material_note, guide=guide_block(guide), digest=digest_block(digest),
              seq_hook_rule=seq_hook_rule, n_versions=n_versions,
              sa_min=int(target_min * SA_SHARE[0]), sa_max=int(target_max * SA_SHARE[1]))
    parts = ["# 📜 티키타카 스크립트 리빌딩 — 걸음 ① 뼈대\n\n",
             _section("[System Role]").format(**kw), "\n", _section("입력").format(**kw), "\n",
             _section("[제1원칙").format(**kw), "\n", _section("[제2원칙").format(**kw), "\n",
             OUTLINE_RULES.format(**kw), "\n", _section("스토리 포맷 상자").format(**kw), "\n",
             OUTLINE_VOCAB.format(**kw), eye, "\n", OUTLINE_OUTPUT.format(**kw), "\n## 소스 스크립트\n", script, "\n", title_prompt((guide or {}).get("title_fit"))]
    return "".join(parts)


def expand_moment_runs(item: dict, moments_by_id: dict, scenes: list[dict]) -> tuple[list[dict], list[str]]:
    """A 항목의 moment_ids → 맞닿은 순간의 연속 A 항목들(하류 어휘는 moment_id 하나 그대로). 떨어졌거나 다른 장면이면 거기서
    끊고 기록한다. 순수 — 테스트 대상."""
    ids = item.get("moment_ids")
    if not isinstance(ids, list) or not ids:
        return [item], []
    known = sorted({str(i) for i in ids if str(i) in moments_by_id}, key=lambda i: moments_by_id[i]["start"])
    notes = [f"없는 순간 ID {sorted(set(map(str, ids)) - set(known))} — 드롭"] if len(known) < len(set(map(str, ids))) else []
    kept: list[str] = []
    for mid in known:
        m = moments_by_id[mid]
        if kept:
            prev = moments_by_id[kept[-1]]
            if m["start"] - prev["end"] > EYE_JOIN_GAP_SEC or _scene_id_at(scenes, m["start"]) != _scene_id_at(scenes, prev["start"]):
                notes.append(f"{mid} 는 {kept[-1]} 과 맞닿지 않는다 — 묶음에서 제외(떨어진 화면은 별도 A 로)")
                break
        kept.append(mid)
    return [{"type": "A", "moment_id": mid, "effect": item.get("effect") if k == 0 else None} for k, mid in enumerate(kept)], notes


def normalize_outline(raw: dict, index: dict, transcript: dict, *, avoid=None, exclude=None, seq_hook: bool = True,
                      copy_text: str | None = None) -> dict:
    """걸음 ① 산출 → 검증된 뼈대(S·A 만). 검증은 종전 `validate_versions` 그 함수다(ID 실재·제외 구간·선형/장면 순서·루프 꼬리·상한).
    모델이 N 을 섞어 내면 버리고 기록한다 — 내레이션은 걸음 ② 가 자리부터 정한다. 순수 — 테스트 대상."""
    from app.tikitaka.rebuild import validate_versions
    moments_by_id = {m["id"]: m for m in index.get("moments") or []}
    scenes = index.get("scenes") or []
    extra: dict[int, dict] = {}
    staged_raw = dict(raw, versions=[])
    for v in raw.get("versions") or []:
        if not isinstance(v, dict):
            continue
        items, notes = [], []
        for it in v.get("items") or []:
            if not isinstance(it, dict):
                continue
            t = str(it.get("type") or "").upper()
            if t == "A":
                expanded, n2 = expand_moment_runs(it, moments_by_id, scenes)
                items += expanded
                notes += n2
            elif t == "N":
                notes.append("뼈대에 N 항목 — 버림(내레이션은 걸음 ② 가 정한다)")
            else:
                items.append(it)
        n = int(v.get("n") or len(staged_raw["versions"]) + 1)
        hook = v.get("hook") if isinstance(v.get("hook"), dict) else {}
        extra[n] = {"notes": notes, "hook": {"with": str(hook.get("with") or "")[:1].upper(), "why": str(hook.get("why") or "")[:160]}}
        staged_raw["versions"].append(dict(v, n=n, items=items))
    data = validate_versions(staged_raw, index, transcript, avoid=avoid or [], exclude=exclude or [], seq_hook=seq_hook, copy_text=copy_text)
    for v in data["versions"]:
        v["issues"] += [f"[뼈대] {x}" for x in extra.get(v["n"], {}).get("notes", [])]
        v["hook"] = extra.get(v["n"], {}).get("hook") or {"with": "", "why": ""}
        v["script_flow"] = "staged"
    return data


# ── 걸음 ② 문장·화면 ─────────────────────────────────────────────────────────────
def speaker_neutral(transcript: dict) -> dict:
    """캐시 지문용 전사 사본 — `speaker` 를 뺀다(2026-09-22 실사고: 확인 패스의 화자 교정 4줄이 transcript.json 에 되써져 소스 스크립트가
    달라지자 같은 잡의 뼈대·걸음 ② 14편이 통째로 다시 만들어졌다). 화자 교정은 같은 대본의 표기 수정이지 다른 재료가 아니다. 순수."""
    out = dict(transcript)
    out["lines"] = [{k: v for k, v in l.items() if k != "speaker"} for l in transcript.get("lines") or []]
    return out


def items_neutral(items: list[dict]) -> list[dict]:
    """version items 의 지문용 사본 — S 항목의 `speaker` 를 뺀다(같은 이유). 순수."""
    return [{k: v for k, v in it.items() if k != "speaker"} for it in items]


def outline_fingerprint(index: dict, transcript: dict, exclude, *, eye: str, guide: dict | None, digest, material_note: str,
                        seq_hook: bool, n_versions: int) -> str:
    """뼈대 캐시 지문 — 소스 스크립트를 **화자 없이** 다시 만들어 센다(프롬프트에 실리는 스크립트는 화자가 있다). 순수 — 테스트 대상."""
    from app.tikitaka.grid import fingerprint
    from app.tikitaka.rebuild import source_script
    return fingerprint([SCHEMA, source_script(index, speaker_neutral(transcript), exclude), eye, (guide or {}).get("sha"), bool(digest),
                        material_note, seq_hook, n_versions])


def script_fingerprint(version: dict, table, voice, guide: dict | None, digest, target) -> str:
    """걸음 ② 캐시 지문 — 항목의 화자 표기는 빼고 센다. 순수 — 테스트 대상."""
    from app.tikitaka.grid import fingerprint
    return fingerprint([SCHEMA, items_neutral(version["items"]), version["title"], table, voice, (guide or {}).get("sha"), bool(digest), target])


def item_windows(version: dict, index: dict, transcript: dict) -> list[tuple[float, float, str, int]]:
    """이 편의 S·A 항목이 차지하는 원본 구간 [(t0, t1, 'S'|'A', 항목 위치)]. 순수."""
    lines = {l["id"]: l for l in transcript.get("lines") or []}
    moments = {m["id"]: m for m in index.get("moments") or []}
    out = []
    for pos, it in enumerate(version["items"]):
        if it["type"] == "S" and all(i in lines for i in it["line_ids"]):
            if transcript.get("words"):
                b = bind_dialogue(lines, transcript["words"], it["line_ids"])
                out.append((b["start"], b["end"], "S", pos))
            else:
                out.append((lines[it["line_ids"][0]]["start"], lines[it["line_ids"][-1]]["end"], "S", pos))
        elif it["type"] == "A" and it.get("moment_id") in moments:
            m = moments[it["moment_id"]]
            out.append((m["start"], m["end"], "A", pos))
    return out


def neighbor_anchors(windows: list[tuple[float, float, str, int]], pos: int) -> list[float]:
    """수리 후보의 기준 시각 — 계획이 없는 N 은 **그 자리 앞뒤 S/A 항목의 원본 시각**을 기준으로 후보 100개를 고른다(2026-09-22 실사고:
    종전 0.0초 폴백이 소스 맨 앞 조각을 후보로 올려 훅 내레이션이 선공개 침대 장면 묘사문으로 재작성됐다). 앞 항목의 끝·뒤 항목의 시작.
    S/A 가 하나도 없으면 [0.0](종전). 순수 — 테스트 대상."""
    before = [w for w in windows if w[3] < pos]
    after = [w for w in windows if w[3] > pos]
    out = []
    if before:
        out.append(max(before, key=lambda w: w[3])[1])
    if after:
        out.append(min(after, key=lambda w: w[3])[0])
    return out or [0.0]


def scope_scenes(version: dict, index: dict, transcript: dict, *, neighbor: int = SCOPE_NEIGHBOR_SCENES) -> list[str]:
    """이 편의 화면 표에 실을 장면 — S·A 가 속한 장면 + about_ids 의 장면 ± 이웃. 순수 — 테스트 대상."""
    scenes = index.get("scenes") or []
    lines = {l["id"]: l for l in transcript.get("lines") or []}
    moments = {m["id"]: m for m in index.get("moments") or []}
    ts = [(a + b) / 2 for a, b, _k, _p in item_windows(version, index, transcript)]
    for it in version["items"]:
        for sid in it.get("about_ids") or []:
            src = lines.get(sid) or moments.get(sid)
            if src:
                ts.append((src["start"] + src["end"]) / 2)
    hit = {k for k, sc in enumerate(scenes) for t in ts if sc["start"] <= t < sc["end"]}
    wide = {j for k in hit for j in range(k - neighbor, k + neighbor + 1) if 0 <= j < len(scenes)}
    return [scenes[k]["id"] for k in sorted(wide)]


def bridge_marks(version: dict, index: dict, transcript: dict) -> dict[int, dict]:
    """'이어야 하는 자리' — 항목 위치(0-기반) k 의 **앞**이 원본에서 끊겨 있는가. {k: {kind, gap_sec, from_scene, to_scene}}.
    장면이 바뀜 · JUMP_SEC 이상 건너뜀 · 되감기. 이 계산은 내레이션 **자리를 정하지 않는다**(자리는 걸음 ② 가 자유롭게) —
    ① 그 자리에 내레이션이 꼭 있어야 하는가 ② 실패한 내레이션을 빼도 이야기가 이어지는가, 두 가지에만 쓴다. 순수 — 테스트 대상."""
    scenes = index.get("scenes") or []
    wins = sorted(item_windows(version, index, transcript), key=lambda w: w[3])
    out: dict[int, dict] = {}
    for (a0, b0, _k0, _p0), (a1, _b1, _k1, p1) in zip(wins, wins[1:]):
        # A real replay loop must work without a rewind explanation at its tail.
        if (version.get("strategy") == "루프형" and p1 == len(version["items"]) - 1
                and _k1 == "S" and a1 < wins[0][0]):
            continue
        gap = a1 - b0
        sc0, sc1 = _scene_id_at(scenes, (a0 + b0) / 2), _scene_id_at(scenes, a1 + 1e-3)
        if a1 < a0 - 1e-6:
            out[p1] = {"kind": "rewind", "gap_sec": round(gap, 1), "from_scene": sc0, "to_scene": sc1}
        elif gap >= JUMP_SEC or sc0 != sc1:
            out[p1] = {"kind": "jump", "gap_sec": round(gap, 1), "from_scene": sc0, "to_scene": sc1}
    return out


def slot_role(mark: dict | None, *, first: bool = False, last: bool = False) -> str:
    """게이트 수리 프롬프트에 주는 '자리의 역할' — 코드가 위치에서 만든다(모델의 자유 서술이 아니다). 순수."""
    if mark and mark["kind"] == "rewind":
        return f"원본에서 앞으로 되감는 자리({abs(mark['gap_sec']):.0f}초 앞) — 시간을 되돌린다는 것을 알리고 다음 장면으로 잇는다"
    if mark:
        return f"원본에서 {mark['gap_sec']:.0f}초 건너뛴 자리({mark['from_scene']}→{mark['to_scene']}) — 시청자가 다음 장면을 따라오게 잇는다"
    if first:
        return "편의 첫 항목(훅) — 첫 3초에 시선을 잡는다"
    if last:
        return "편의 마지막 — 실제 결말 뒤 꼭 필요한 새 정보만; 결말 요약이면 생략"
    return "붙어 있는 두 항목 사이의 리듬 내레이션 — 앞 항목에 대한 한마디이거나 다음 대사를 소개한다"


def lineup_block(version: dict, index: dict, transcript: dict) -> str:
    """걸음 ② 의 [편성표] — 대사(화자·시각·길이)·현장음(묘사) 순서 + 코드가 단 '이어야 하는 자리' 표식. 순수."""
    moments = {m["id"]: m for m in index.get("moments") or []}
    scenes = {sc["id"]: sc for sc in index.get("scenes") or []}
    win = {pos: (a, b) for a, b, _k, pos in item_windows(version, index, transcript)}
    marks = bridge_marks(version, index, transcript)
    out = []
    for pos, it in enumerate(version["items"]):
        if pos in marks:
            m = marks[pos]
            to = scenes.get(m["to_scene"], {}).get("summary", "")
            what = (f"되감기 — 원본에서 {abs(m['gap_sec']):.0f}초 앞으로 돌아간다" if m["kind"] == "rewind"
                    else f"{m['gap_sec']:.0f}초 건너뜀")
            out.append(f"   ⚠ 이어야 하는 자리(before: {pos + 1}) — {what} · {m['from_scene']} → {m['to_scene']} {to[:60]}")
        a, b = win.get(pos, (None, None))
        when = f" ({fmt_tc(a)[:-2]} · {b - a:.1f}s)" if a is not None else ""
        if it["type"] == "S":
            out.append(f"{pos + 1}. [S] {'+'.join(it['line_ids'])} {it.get('speaker') or '미상'}: \"{it.get('text', '')}\"{when}")
        elif it["type"] == "A":
            out.append(f"{pos + 1}. [A] {it['moment_id']} {moments.get(it['moment_id'], {}).get('desc', '')}{when}")
    return "\n".join(out)


def narration_prompt(version: dict, index: dict, transcript: dict, table: list[str], *, title: str, episode: str,
                     guide: dict | None, digest: dict | None, scenes: list[str], problems: list[str] | None = None,
                     target: tuple[int, int] = (45, 70)) -> str:
    from app.tikitaka.production import planning_rules
    from app.tikitaka.grid_table import MAX_STACK
    scoped = dict(digest or {})
    if scoped.get("scene_notes"):
        scoped["scene_notes"] = [n for n in scoped["scene_notes"] if n.get("id") in set(scenes)]
    hook = version.get("hook") or {}
    head = NARRATION_HEAD.format(title=title, episode=episode, n=version["n"], strategy=version["strategy"],
                                 title_text=str(version["title"]).replace("\n", " / "), structure=version.get("structure", ""),
                                 hook=(hook.get("why") or "(미정)"), guide=guide_block(guide), digest=digest_block(scoped),
                                 cps=NARRATION_CHARS_PER_SEC, max_stack=MAX_STACK, n_lo=NAR_COUNT_HINT[0], n_hi=NAR_COUNT_HINT[1],
                                 sa_sec=sum(b - a for a, b, _k, _p in item_windows(version, index, transcript)), jump=JUMP_SEC,
                                 target_min=target[0], target_max=target[1], hard_max=int(MAX_SHORTS_SEC))
    return "".join([head, "\n", _section("[제3원칙"), "\n", _section("[제3-1원칙"), "\n", _section("[제4원칙"), planning_rules(),
                    "\n## 편성표\n", lineup_block(version, index, transcript), "\n\n", COVER_TABLE_HEADER, "\n", "\n".join(table), "\n\n",
                    NARRATION_OUTPUT.format(), reject_block(problems or [])])


def fill_evidence(plan: dict, info: dict[str, dict], index: dict, transcript: dict) -> None:
    """근거 칸이 비었으면 덮개 화면의 기록으로 채운다(그 조각의 순간 ID · 겹치는 대사 줄). 제자리 — 1차 실측 v12: 모델이 근거 칸에
    화면 번호를 적은 형식 오류 하나로 편이 탈락했다. 근거는 검사관이 읽는 참고 기록이지 통과 조건이 아니다."""
    if plan.get("evidence_ids"):
        return
    ids: list[str] = []
    moment_of = {sid: m["id"] for m in index.get("moments") or [] for sid in m.get("span_ids") or []}
    for x in plan.get("cover") or []:
        sid = x["span_id"]
        if sid in moment_of:
            ids.append(moment_of[sid])
        elif sid in info:
            ids += [l["id"] for l in transcript.get("lines") or [] if l["start"] < info[sid]["t_out"] and l["end"] > info[sid]["t_in"]]
    plan["evidence_ids"] = list(dict.fromkeys(ids))[:6]


def validate_narration(raw, version: dict, info: dict[str, dict], index: dict, transcript: dict, exclude=(), *,
                       measure=None, target: tuple[int, int] | None = None) -> tuple[list[dict], list[str]]:
    """걸음 ② 산출의 **형식** 검사 — 게이트(프로브)에 가기 전에 코드가 잡을 수 있는 것. → ([{pos, text, effect, production_plan, sec}], 문제).
    pos = 그 내레이션이 앞에 들어갈 항목 위치(0-기반 · len(items) = 맨 끝). measure(text) → 합성 실측 초(없으면 글자수 추정).
    순수(measure 주입) — 테스트 대상."""
    from app.tikitaka.production import normalize_plan, cover_runs
    from app.tikitaka.grid_table import MAX_STACK
    rows = raw.get("narrations") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return [], ["narrations 목록이 없다"]
    n_items = len(version["items"])
    marks = bridge_marks(version, index, transcript)
    out: list[dict] = []
    problems: list[str] = []
    used: dict[str, str] = {}
    taken: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("after_last") is True:
            pos = n_items
        else:
            try:
                pos = int(row.get("before")) - 1
            except (TypeError, ValueError):
                problems.append(f"자리 표기 오류 {row.get('before')!r} — before 는 [편성표] 항목 번호(1~{n_items}), 맨 끝은 after_last")
                continue
        tag = "after_last" if pos == n_items else f"before {pos + 1}"
        if not 0 <= pos <= n_items or pos in taken:
            problems.append(f"{tag}: 없는 자리이거나 한 자리에 내레이션이 둘이다")
            continue
        text = " ".join(str(row.get("text") or "").split())
        n_chars = len(_WS.sub("", text))
        if n_chars < NAR_MIN_CHARS:
            problems.append(f"{tag}: 문장이 없다")
            continue
        if version.get("strategy") == "루프형" and (pos == n_items or pos == n_items - 1):
            from app.tikitaka.rebuild import _is_meta_loop_narration
            if _is_meta_loop_narration(text, ending=True):
                problems.append(f"{tag}: 루프 끝의 되감기 설명은 금지 — 실제 결말에서 끝내거나 설명 없이 이어지는 대사를 고르세요")
                continue
        taken.add(pos)
        if n_chars > NAR_MAX_CHARS:
            problems.append(f"{tag}: {n_chars}자 — 한 항목 8~30자. 두 자리로 나누거나 핵심만 남겨 줄여라")
        plan, errors = normalize_plan(row.get("production_plan"), index, transcript, list(exclude))
        problems += [f"{tag}: {e}" for e in errors if not e.startswith("evidence_ids:")]      # 근거 칸 오기는 버리고 아래서 채운다
        cover = [x["span_id"] for x in (plan or {}).get("cover", [])]
        if not cover:
            problems.append(f"{tag}: cover 가 없다 — [쓸 수 있는 화면]에서 고른 sp ID 를 적어라")
            continue
        bad = [s for s in cover if s not in info or not info[s]["usable"]]
        if bad:
            problems.append(f"{tag}: 표에 없거나 고를 수 없는 화면 {bad}")
            continue
        fill_evidence(plan, info, index, transcript)
        twice = [s for s in cover if s in used]
        if twice:
            problems.append(f"{tag}: {twice} 는 {used[twice[0]]} 가 이미 쓴 화면이다 — 두 내레이션이 같은 화면을 쓰지 않는다")
        if len(cover_runs(cover, info)) > MAX_STACK:
            problems.append(f"{tag}: 화면 묶음 {len(cover_runs(cover, info))}개 > 최대 {MAX_STACK}컷")
        # 묶음 상한: 고른 조각들이 속한 '이어 쓸 수 있는 덩어리' 길이의 합(같은 덩어리는 한 번만 센다)
        budget = sum({info[s]["run_id"]: info[s]["run"] for s in cover}.values())
        sec = float(measure(text)) if measure else narration_plan_sec(text)
        if budget + 0.05 < sec:
            problems.append(f"{tag}: 문장 {sec:.1f}초{'(합성 실측)' if measure else ''} > 고른 화면의 묶음 {budget:.1f}초 — 문장을 줄이거나 묶음이 긴 화면을 골라라")
        if pos == 0:
            best = max((info[s]["importance"] for s in info if info[s]["usable"]), default=0)
            need = min(EYE_MIN_IMPORTANCE, best)
            if max(info[s]["importance"] for s in cover) < need:
                stars = [s for s in info if info[s]["usable"] and info[s]["importance"] >= need][:8]
                problems.append(f"{tag}: 첫 화면(훅)은 눈길을 끄는 조각에서 — 후보 {stars}")
        for s in cover:
            used.setdefault(s, tag)
        eff = row.get("effect")
        out.append({"pos": pos, "text": text, "effect": str(eff).strip()[:16] if eff else None, "production_plan": plan, "sec": round(sec, 3)})
    for pos, m in marks.items():
        if pos not in taken:
            problems.append(f"before {pos + 1}: ⚠ 이어야 하는 자리({'되감기' if m['kind'] == 'rewind' else str(round(m['gap_sec'])) + '초 건너뜀'})에 내레이션이 없다")
    if target:
        total = sum(b - a for a, b, _k, _p in item_windows(version, index, transcript)) + sum(x["sec"] for x in out)
        if total > MAX_SHORTS_SEC:
            problems.append(f"합계 {total:.0f}초 > 절대 상한 {MAX_SHORTS_SEC:g}초 — 리듬 내레이션을 줄여라")
    return sorted(out, key=lambda x: x["pos"]), problems


def apply_narration(version: dict, narrations: list[dict], index: dict, transcript: dict) -> None:
    """내레이션을 뼈대에 끼워 넣는다(제자리). 각 N 에 slot · required(= 이어야 하는 자리 — 빼면 이야기가 끊긴다) · intent(자리의 역할,
    코드가 위치에서 만든 말)를 붙인다 — 게이트가 이 셋을 본다."""
    marks = bridge_marks(version, index, transcript)
    n_items = len(version["items"])
    by_pos = {x["pos"]: x for x in narrations}
    items: list[dict] = []
    k = 0
    for pos in range(n_items + 1):
        got = by_pos.get(pos)
        if got:
            k += 1
            items.append({"type": "N", "text": got["text"], "effect": got["effect"], "production_plan": got["production_plan"],
                          "plan_sec": narration_plan_sec(got["text"]), "slot": f"N{k:02d}", "required": pos in marks,
                          "intent": slot_role(marks.get(pos), first=pos == 0, last=pos == n_items)})
        if pos < n_items:
            items.append(version["items"][pos])
    version["items"] = items
    version["plan_sec"] = ms3(sum(x["plan_sec"] for x in items))


def record_drop(job, version: dict, entry: dict) -> None:
    """내레이션을 뺄 때마다 남긴다(사용자 결정) — 대본(`narration_dropped`) · 로그 · run_log. 조용히 빠지는 내레이션은 없다."""
    version.setdefault("narration_dropped", []).append(entry)
    job.log(f"[staged/뺌] v{version.get('n')} {entry.get('slot') or 'before ' + str(entry.get('before'))} ({entry.get('stage')}) "
            f"「{entry.get('text')}」 — {str(entry.get('why'))[:200]}")
    if hasattr(job, "record_step"):
        job.record_step("staged_narration_dropped", version=version.get("n"), **entry)


def name_rule(job) -> str | None:
    """검사관에게 주는 인물 표기 규칙 — 가이드가 내레이션을 배우 이름으로 쓰게 하는데 검사관은 그걸 몰라 '인물이 다르다'고 반려했다
    (1차 실측 v5: 임재홍→김지훈). 가이드에 배우 표가 없으면 None(프로브 문맥 종전과 동일)."""
    if not (hasattr(job, "has") and job.has("staged_names.json")):
        return None
    actors = job.load("staged_names.json") or {}
    if not actors:
        return None
    return ("내레이션은 제작 가이드에 따라 인물을 **배우 이름**으로 부른다 — 같은 사람이다: "
            + ", ".join(f"{c}={a}" for c, a in actors.items()) + ". 극중 이름과 배우 이름의 표기 차이는 불일치가 아니다.")


def write_narration(job, gemini, version: dict, index: dict, transcript: dict, exclude, *, title: str, episode: str,
                    guide: dict | None, digest: dict | None, sfx: str = "", target: tuple[int, int] = (45, 70)) -> dict:
    """걸음 ② — 한 편의 내레이션(자리·화면·문장)을 정한다. 캐시 `script_v{n}.json`(뼈대 항목·화면 표·목소리가 같으면 재사용).
    문장은 형식 검사에서 **즉시 합성해 길이를 실측**한다(human-flow 걸음 4 와 같은 순서 — 추정으로 통과한 문장이 게이트 실측에서
    다시 걸리지 않게). 합성본은 게이트·조립이 같은 캐시를 쓴다."""
    from app.tikitaka.grid import fingerprint
    wins = item_windows(version, index, transcript)
    scenes = scope_scenes(version, index, transcript)
    table, info = cover_table(index, transcript, exclude, scenes=set(scenes),
                              own_dialogue=[(a, b) for a, b, k, _p in wins if k == "S"],
                              forbidden=[(a, b) for a, b, k, _p in wins if k == "A"])
    voice = job.load("planning_tts_settings.json") if job.has("planning_tts_settings.json") else {}
    measure = None
    if voice:
        from app.tikitaka.grid_table import _tts_cached
        measure = lambda text: _tts_cached(job, text, voice["voice"], voice["speed"])[1]   # noqa: E731
    fp = script_fingerprint(version, table, voice, guide, digest, target)
    name = f"script_v{version['n']}{sfx}.json"
    if job.has(name) and job.load(name).get("fingerprint") == fp:
        narrations = job.load(name)["narrations"]
    else:
        problems: list[str] = []
        narrations: list[dict] = []
        for attempt in range(1 + REASK_MAX):
            prompt = narration_prompt(version, index, transcript, table, title=title, episode=episode, guide=guide,
                                      digest=digest, scenes=scenes, problems=problems, target=target)
            if attempt == 0:
                job.log(f"[staged/②] v{version['n']} 항목 {len(version['items'])} · 이어야 하는 자리 {len(bridge_marks(version, index, transcript))} · "
                        f"장면 {len(scenes)} · 화면 {len(info)}조각 · 프롬프트 {len(prompt):,}자")
            raw = gemini.text_json(prompt, kind="staged_narration", thinking="medium")
            job.save(f"script_v{version['n']}{sfx}_raw.json", raw)
            narrations, problems = validate_narration(raw, version, info, index, transcript, exclude, measure=measure, target=target)
            if not problems:
                break
            job.log(f"[staged/②] v{version['n']} 형식 문제 {len(problems)}건({attempt + 1}/{1 + REASK_MAX}): {problems[:3]}")
        dropped: list[dict] = []
        if problems:                                   # 재질의 소진 — 문제가 남은 자리: 빼도 이어지는 자리는 빼고(기록), 이어야 하는 자리는 편 실패
            marks = bridge_marks(version, index, transcript)
            n_items = len(version["items"])
            bad_pos = set()
            for prob in problems:
                m = re.match(r"(before (\d+)|after_last):", prob)
                if not m:
                    raise ValueError(f"걸음 ② 형식 문제 소진: {problems[:4]}")
                bad_pos.add(n_items if m.group(1) == "after_last" else int(m.group(2)) - 1)
            if bad_pos & set(marks):
                raise ValueError(f"걸음 ② 형식 문제 소진(이어야 하는 자리): {[x for x in problems][:4]}")
            for x in [x for x in narrations if x["pos"] in bad_pos]:
                dropped.append({"stage": "write", "before": x["pos"] + 1, "text": x["text"],
                                "why": "; ".join(q for q in problems if q.startswith(("after_last" if x["pos"] == n_items else f"before {x['pos'] + 1}") + ":"))[:240]})
            narrations = [x for x in narrations if x["pos"] not in bad_pos]
        job.save(name, {"schema": SCHEMA, "fingerprint": fp, "scenes": scenes, "narrations": narrations, "dropped": dropped})
    for d in (job.load(name).get("dropped") or []):
        record_drop(job, version, d)
    apply_narration(version, narrations, index, transcript)
    return version


# ── 게이트 — 검사는 종전과 같은 자, 고치는 순서만 다르다 ────────────────────────────────
def enforce_staged_plans(job, gemini, version: dict, index: dict, transcript: dict, exclude=()) -> None:
    """N 마다: 검사 → ① 화면 교체 1회(문장 고정) → ② 의도는 그대로 · 검사관이 **이미 본** 화면 중 하나를 골라 그 내용으로 재작성
    → 선택 자리는 빼고 필수 자리는 ValueError. 검사(자리·TTS 실측 길이·넓히기·묶음 프로브)는 `production.enforce_joint_plans`
    와 같은 헬퍼·같은 순서다."""
    from app.tikitaka.grid import fingerprint
    from app.tikitaka.grid_table import probe_cover, overlap, MIN_CUT, MAX_STACK, FPS, _tts_cached
    from app.tikitaka.production import (normalize_plan, widen_cover_plan, cover_runs, narration_context, planning_rules,
                                         BUDGET_SLACK_SEC)
    spans = {s["id"]: s for s in job.load("grid.json")["span_candidates"]}
    moments = {m["id"]: m for m in index.get("moments", [])}
    lines = {l["id"]: l for l in transcript["lines"]}
    rows = [dict(it, mode=it["type"], text=it.get("text") or moments.get(it.get("moment_id"), {}).get("desc", ""))
            for it in version["items"]]
    fixed = [(a, b, pos, kind) for a, b, kind, pos in item_windows(version, index, transcript)]
    settings = job.load("planning_tts_settings.json") if hasattr(job, "has") and job.has("planning_tts_settings.json") else None
    reserved = list(exclude)
    dropped: list[dict] = []
    names = name_rule(job)

    def check(candidate: dict, pos: int, hard) -> tuple[dict | None, list[str], list[dict]]:
        plan, errors = normalize_plan(candidate.get("production_plan"), index, transcript, exclude)
        errors = [e for e in errors if not e.startswith("evidence_ids:")]          # 근거 칸 오기로 편을 죽이지 않는다 — 버리고 채운다
        probed: list[dict] = []
        if not plan or not plan.get("cover"):
            errors.append("각 N에는 유효한 sp ID의 cover 계획이 필요함")
        elif not plan.get("evidence_ids"):
            fill_evidence(plan, {sid: {"t_in": sp["t_in"], "t_out": sp["t_out"]} for sid, sp in spans.items()}, index, transcript)
        available = {sid for sid, sp in spans.items() if sid in index["grid_facts"] and not overlap(sp["t_in"], sp["t_out"], hard)}
        if plan:
            unavailable = [x["span_id"] for x in plan.get("cover", []) if x["span_id"] not in available]
            if unavailable:
                errors.append(f"제외 구간·현장음·다른 내레이션 덮개와 겹치는 화면: {unavailable}")
        if errors:
            return plan, errors, probed
        needed = narration_plan_sec(candidate["text"])
        if settings:
            _, needed = _tts_cached(job, candidate["text"], settings["voice"], settings["speed"])
        needed = math.ceil(needed * FPS - 1e-7) / FPS + 1 / FPS
        # 넓힐 때는 이 편이 대사로 쓰는 화면을 먼저 피한다(같은 화면이 두 번 나온다) — 그래도 모자랄 때만 허용(9/17 방침: 대사 화면 재사용 가능)
        own = [(a, b) for a, b, _p, kind in fixed if kind == "S"]
        auto = widen_cover_plan(plan["cover"], spans, index, needed, hard + own, max_runs=MAX_STACK)
        auto += widen_cover_plan(plan["cover"], spans, index, needed, hard, max_runs=MAX_STACK)
        if auto:
            plan["auto_extended"] = auto
            job.log(f"[staged/gate] v{version.get('n')} 항목{pos + 1}: 화면 넓힘 {auto}")
        runs = cover_runs([x["span_id"] for x in plan["cover"]], spans)
        short = [ids for ids, length in runs if length < MIN_CUT]
        if short:
            errors.append(f"짧은 화면 {short}: 이어진 조각 합이 {MIN_CUT:g}초 미만이고 같은 씬에서 더 이을 조각이 없다")
        if len(runs) > MAX_STACK:
            errors.append(f"덮개 묶음 {len(runs)}개 > 최대 {MAX_STACK}컷")
        budget = sum(max(0.0, math.floor(spans[ids[-1]]["t_out"] * FPS + 1e-7) / FPS - math.ceil(spans[ids[0]]["t_in"] * FPS - 1e-7) / FPS)
                     for ids, _l in runs)
        if not errors and budget + BUDGET_SLACK_SEC < needed:
            errors.append(f"화면 {budget:.2f}초 < 필요 발화 {needed:.2f}초: 같은 씬에 더 이을 화면이 없다")
        if errors:
            return plan, errors, probed
        candidate = dict(candidate, production_plan=plan)
        cand_rows = list(rows)
        cand_rows[pos] = dict(candidate, mode="N")
        context = narration_context(cand_rows, cand_rows[pos], index, transcript)
        role_of = {x["span_id"]: x.get("role") for x in plan["cover"]}
        for run_ids, length in runs:
            cut = {"in": spans[run_ids[0]]["t_in"], "out": spans[run_ids[-1]]["t_out"],
                   "desc": " / ".join(index["grid_facts"][x].get("scene_script", "") for x in run_ids)}
            role = "evidence" if any(role_of.get(x) == "evidence" for x in run_ids) else "support"
            ctx = dict(context, cover_role=role, **({"인물 표기 규칙": names} if names else {}))
            result = probe_cover(job, gemini, cut, candidate["text"],
                                 key=fingerprint(["joint-plan-gate/v2", cut, candidate["text"], ctx]), context=ctx)
            ok = isinstance(result, dict) and result.get("text_matches") is True
            probed.append({"span_ids": list(run_ids), "sec": round(length, 2), "ok": ok,
                           "seen": (result or {}).get("seen") if isinstance(result, dict) else None,
                           "reason": (result or {}).get("reason") if isinstance(result, dict) else None})
            if not ok:
                errors.append(f"{'+'.join(run_ids)}: {result}")
        return plan, errors, probed

    for pos, item in enumerate(version["items"]):
        if item["type"] != "N":
            continue
        hard = reserved + [(a, b) for a, b, _p, kind in fixed if kind == "A"]
        candidate, history, seen_pool = dict(item), [], []
        passed = None
        for attempt in range(3):
            plan, errors, probed = check(candidate, pos, hard)
            seen_pool += [p for p in probed if p.get("seen")]
            if not errors:
                passed = (candidate, plan)
                break
            history.append(errors)
            job.log(f"[staged/gate] v{version.get('n')} 항목{pos + 1}({item.get('slot')}) 시도{attempt + 1}: {str(errors)[:300]}")
            if attempt == 2:
                break
            sources = {**moments, **lines}
            anchors = [sources[s]["start"] for s in ((candidate.get("production_plan") or {}).get("evidence_ids") or []) if s in sources]
            anchors += [spans[x["span_id"]]["t_in"] for x in ((item.get("production_plan") or {}).get("cover") or []) if x.get("span_id") in spans]
            anchors = anchors or neighbor_anchors(item_windows(version, index, transcript), pos)
            avail = [sid for sid, sp in spans.items() if sid in index["grid_facts"] and not overlap(sp["t_in"], sp["t_out"], hard)]
            nearby = sorted(avail, key=lambda sid: min(abs(spans[sid]["t_in"] - t) for t in anchors))[:100]
            material = [{"span_id": sid, "start": spans[sid]["t_in"], "sec": round(spans[sid]["t_out"] - spans[sid]["t_in"], 2),
                         "scene_script": index["grid_facts"][sid].get("scene_script", "")} for sid in sorted(nearby, key=lambda s: spans[s]["t_in"])]
            rewrite = attempt == 1 and not item.get("user_edits")
            if rewrite and seen_pool:
                allowed = {s for p in seen_pool for s in p["span_ids"]}
                rule = ("자리의 역할은 그대로 지키고 문장을 다시 써라. 아래 [검사관이 실제로 본 화면] 중 **하나를 골라**, 그 화면에서 본 "
                        "내용(seen)으로 문장을 쓴다. 보이지 않는 행동·감정·인물을 말하지 마라. cover 는 고른 후보의 span_ids 안에서만 "
                        f"지정하고, 문장 길이(공백 제외 글자수÷{NARRATION_CHARS_PER_SEC:g}초)는 그 후보의 sec 안에 들게 하라.\n"
                        f"[검사관이 실제로 본 화면]: {seen_pool}\n")
            elif rewrite:
                allowed = set(nearby)
                rule = ("같은 씬에 그만한 화면이 없다. 자리의 역할은 그대로 지키고, 아래 화면의 길이 안에 드는 더 짧은 완결 문장으로 "
                        "다시 써라(요약체·절단 금지). 보이지 않는 행동·감정은 쓰지 마라.\n")
            else:
                allowed = set(nearby)
                rule = "문장은 한 글자도 바꾸지 말고 **화면만** 다시 골라라 — 문장이 말하는 것이 실제로 보이는 화면으로.\n"
            prompt = (planning_rules() + "\n문제가 있는 N 한 개만 수정하라. 대사나 다른 항목은 수정하지 마라.\n" + rule
                      + "검사를 피하려 action 을 evaluation 으로 이름만 바꾸지 마라. 정지·슬로우 금지.\n"
                      "JSON {text: string, production_plan: object}.\n"
                      f"자리의 역할: {item.get('intent')!r}\n"
                      f"대본: {[{k: v for k, v in x.items() if k in ('type', 'text', 'slot', 'intent')} for x in version['items']]}\n"
                      f"대상: {candidate}\n실패 이력: {history}\n사용 가능한 화면(아래 span_id 만): {material}\n")
            raw = gemini.text_json(prompt, kind="staged_gate_repair")
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"].strip():
                continue
            plan_in = raw.get("production_plan") if isinstance(raw.get("production_plan"), dict) else {}
            outside = [x.get("span_id") for x in plan_in.get("cover") or [] if isinstance(x, dict) and x.get("span_id") not in allowed]
            if outside:
                history.append([f"후보 밖 화면 {outside} — 제시한 span_id 안에서만 고른다"])
                plan_in = dict(plan_in, cover=[x for x in plan_in.get("cover") or [] if isinstance(x, dict) and x.get("span_id") in allowed])
            candidate = dict(item, production_plan=plan_in)
            if rewrite:
                candidate["text"] = " ".join(raw["text"].split())
        if passed:
            candidate, plan = passed
            plan["gate_verified"] = True
            reserved.extend((spans[x["span_id"]]["t_in"], spans[x["span_id"]]["t_out"]) for x in plan["cover"])
            old = dict(item)
            item.update(text=candidate["text"], production_plan=plan, plan_sec=narration_plan_sec(candidate["text"]))
            rows[pos].update(item)
            if history:
                version.setdefault("joint_plan_repairs", []).append(
                    {"item": pos + 1, "slot": item.get("slot"), "before": old, "after": dict(item), "failures": history,
                     "rewritten": old.get("text") != item["text"]})
            continue
        if item.get("required"):
            raise ValueError(f"문장·화면 계획 수정 소진: 이어야 하는 자리 항목{pos + 1}({item.get('slot')}) — {item.get('intent')} {history[-1]}")
        dropped.append({"slot": item.get("slot"), "role": item.get("intent"), "stage": "gate", "text": item.get("text"),
                        "tried": [str(h)[:200] for h in history], "why": str(history[-1])[:300]})
        rows[pos]["text"] = ""
        item["_dropped"] = True
    if dropped:
        version["items"] = [it for it in version["items"] if not it.pop("_dropped", False)]
        for d in dropped:
            record_drop(job, version, d)
    version["plan_sec"] = ms3(sum(it["plan_sec"] for it in version["items"]))


# ── 첫 3초 — 반려하지 않고 기록한다 ─────────────────────────────────────────────────
OPENING_WEIGHTS = {"importance": 35, "cuts": 20, "voice": 25, "arousal": 20}   # 초기값(추정) — 시청 지속률과 대조해 맞춘다


def opening_record(version: dict, index: dict, transcript: dict, grid: dict, *, tts_sec=None) -> dict:
    """편성 기준(조립 전)으로 첫 OPENING_SEC 초에 무엇이 나가는지 재서 강도·사유·측정값을 남긴다. 순수 — 테스트 대상.
    tts_sec(text) 가 있으면 내레이션 길이는 합성 실측, 없으면 계획값."""
    times = span_times(index)
    win = {pos: (a, b) for a, b, _k, pos in item_windows(version, index, transcript)}
    segs: list[dict] = []                     # 편집본 시간축의 조각 {at, dur, kind, src:(a,b)|None, importance}
    at = 0.0
    for pos, it in enumerate(version["items"]):
        if at >= OPENING_SEC:
            break
        if it["type"] in ("S", "A") and pos in win:
            a, b = win[pos]
            imp = max((sp["importance"] for sp in times.values() if sp["t_in"] < b and sp["t_out"] > a), default=0)
            segs.append({"at": at, "dur": b - a, "kind": it["type"], "src": (a, b), "importance": imp})
            at += b - a
        elif it["type"] == "N":
            dur = float(tts_sec(it["text"])) if tts_sec and it.get("text") else float(it.get("plan_sec") or 3.5)
            cover = sorted(((times[x["span_id"]]["t_in"], times[x["span_id"]]["t_out"], times[x["span_id"]]["importance"])
                            for x in (it.get("production_plan") or {}).get("cover", []) if x["span_id"] in times))
            left, t = dur, at
            for a, b, imp in cover or [(None, None, 0)]:
                if left <= 0:
                    break
                d = min(left, (b - a) if a is not None else left)
                segs.append({"at": t, "dur": d, "kind": "N", "src": (a, a + d) if a is not None else None, "importance": imp})
                t, left = t + d, left - d
            at += dur
    head = [s for s in segs if s["at"] < OPENING_SEC]
    cuts = [round(s["at"], 2) for s in head[1:]]
    for s in head:                           # 조각 안의 원본 장면 전환
        if s["src"]:
            cuts += [round(s["at"] + c - s["src"][0], 2) for c in grid.get("scene_cuts") or []
                     if s["src"][0] + 0.1 < c < s["src"][1] - 0.1 and s["at"] + c - s["src"][0] < OPENING_SEC]
    cuts = sorted(set(cuts))
    first_dialogue = next((round(s["at"], 2) for s in head if s["kind"] == "S"), None)
    first_narration = next((round(s["at"], 2) for s in head if s["kind"] == "N"), None)
    importance = max((s["importance"] for s in head), default=0)
    live = [s for s in head if s["kind"] in ("S", "A") and s["src"]]
    samples = [x["score"] for x in grid.get("arousal") or [] for s in live
               if s["src"][0] <= x["t"] < min(s["src"][1], s["src"][0] + OPENING_SEC - s["at"])]
    pool = sorted(x["score"] for x in grid.get("arousal") or [])
    arousal = None
    if samples and pool:                      # 회차 안에서의 상대 위치(0~1) — 절대값은 소재마다 다르다
        mean = sum(samples) / len(samples)
        arousal = round(sum(1 for x in pool if x <= mean) / len(pool), 2)
    w = OPENING_WEIGHTS
    voice = 1.0 if first_dialogue is not None and first_dialogue <= 1.0 else 0.72 if first_dialogue is not None \
        else 0.48 if first_narration is not None else 0.0
    strength = round(w["importance"] * max(0, importance - 1) / 4 + w["cuts"] * min(len(cuts), 2) / 2 + w["voice"] * voice
                     + w["arousal"] * (arousal if arousal is not None else 0.5))
    first = version["items"][0]["type"] if version["items"] else None
    opener = {"S": "대사", "A": "말 없는 화면(원음)", "N": "내레이션"}.get(first, "?")
    reason = (f"{opener}로 연다 · 첫 화면 중요도 {importance or '미상'} · 3초 안 컷 {len(cuts)}회"
              + (f" · 첫 대사 {first_dialogue:.1f}s" if first_dialogue is not None else " · 3초 안 대사 없음")
              + (f" · 내레이션 {first_narration:.1f}s" if first_narration is not None else "")
              + (f" · 소리 에너지 회차 내 상위 {round((1 - arousal) * 100)}%" if arousal is not None else ""))
    return {"strength": int(max(0, min(100, strength))), "reason": reason,
            "facts": {"first_item": first, "cuts_in_3s": len(cuts), "first_cut_sec": cuts[0] if cuts else None,
                      "first_dialogue_sec": first_dialogue, "first_narration_sec": first_narration,
                      "cover_importance": importance, "arousal_pct": arousal},
            "writer_why": (version.get("hook") or {}).get("why") or "", "weights": dict(w), "basis": "plan"}


# ── 묶음: 걸음 ① → ② (rebuild.rebuild 가 부른다) ───────────────────────────────────
def draft_versions(job, gemini, index: dict, transcript: dict, *, title: str, episode_label: str, duration_label: str, script: str,
                   exclude, guide: dict | None, digest: dict | None, material_note: str, seq_hook: bool, seq_hook_rule: str,
                   n_versions: int, target_min: int, target_max: int, sfx: str = "") -> dict:
    """걸음 ①(뼈대 1회) → 걸음 ②(편마다). 돌려주는 것은 종전 validate_versions 산출과 같은 모양(+staged 키).
    걸음 ② 에서 필수 자리를 못 채운 편은 `production_plan_blocked` — 종전 게이트 탈락과 같은 취급."""
    if not index.get("grid_facts"):
        raise ValueError("--script-flow staged 는 화면 기록(grid_facts)이 있는 grid-review 분석에서만 쓴다")
    from app.tikitaka.grid import fingerprint
    eye = eye_block(index, exclude)
    fp = outline_fingerprint(index, transcript, exclude, eye=eye, guide=guide, digest=digest, material_note=material_note,
                             seq_hook=seq_hook, n_versions=n_versions)
    name = f"outline{sfx}.json"
    if job.has(name) and job.load(name).get("fingerprint") == fp:
        data = job.load(name)["data"]
        job.log(f"[staged/①] 뼈대 캐시 — {len(data['versions'])}버전")
    else:
        problems: list[str] = []
        for attempt in range(1 + REASK_MAX):
            prompt = outline_prompt(title=title, episode_label=episode_label, duration_label=duration_label, script=script, guide=guide,
                                    digest=digest, material_note=material_note, seq_hook_rule=seq_hook_rule,
                                    eye=eye, n_versions=n_versions, target_min=target_min,
                                    target_max=target_max) + reject_block(problems)
            if attempt == 0:
                job.log(f"[staged/①] 뼈대 요청 — 프롬프트 {len(prompt):,}자 · 눈길 끄는 화면 {len(eye_catchers(index, exclude))}묶음")
            raw = gemini.text_json(prompt, kind="staged_outline", thinking="high")
            job.save(f"outline{sfx}_raw.json", raw)
            from app.tikitaka.rebuild import StoryBudgetError
            try:
                data = normalize_outline(raw, index, transcript, avoid=(guide or {}).get("avoid") or [], exclude=exclude,
                                         seq_hook=seq_hook, copy_text=(guide or {}).get("copy"))
            except StoryBudgetError as exc:
                problems = [str(exc)]
                job.log(f"[staged/①] 결말 보존 재구성({attempt + 1}/{1 + REASK_MAX}): {exc}")
                if attempt == REASK_MAX:
                    raise
                continue
            problems = [f"버전 {v['n']}: 대사(S) 항목이 없다" for v in data["versions"] if not any(it["type"] == "S" for it in v["items"])]
            if not data["versions"]:
                problems = ["versions 가 비었다"]
            if not problems:
                break
            job.log(f"[staged/①] 형식 문제({attempt + 1}/{1 + REASK_MAX}): {problems[:3]}")
        if not data["versions"]:
            raise ValueError("걸음 ① 뼈대가 비었다")
        job.save(name, {"schema": SCHEMA, "fingerprint": fp, "data": data})
    job.save("staged_names.json", dict((guide or {}).get("actors") or {}))
    for v in data["versions"]:
        job.log(f"[staged/①] v{v['n']:02d} {v['strategy']:<14} 대사·현장음 {v['plan_sec']:5.1f}s 항목 {len(v['items']):2d} · "
                f"훅 {v['hook'].get('with') or '-'} · 첫 항목 {v['items'][0]['type'] if v['items'] else '-'} — {v['title']}")
        try:
            short = SHORT_FORM.get(v["strategy"])
            write_narration(job, gemini, v, index, transcript, exclude, title=title, episode=episode_label, guide=guide,
                            digest=digest, sfx=sfx, target=short or (target_min, target_max))
            ns = [it for it in v["items"] if it["type"] == "N"]
            job.log(f"[staged/②] v{v['n']:02d} 내레이션 {len(ns)}(이어야 하는 자리 {sum(bool(x.get('required')) for x in ns)}) · 합계 {v['plan_sec']:.1f}s")
        except ValueError as exc:
            v["production_plan_blocked"] = f"ValueError: {exc}"
            v.setdefault("issues", []).append(f"[걸음 ② 실패] {str(exc)[:300]}")
            job.log(f"[staged/②] v{v['n']} 자동 후보 제외 — {str(exc)[:240]}")
    data["script_flow"] = "staged"
    return data


# ── 영상 확인 패스(verify) 접점 ────────────────────────────────────────────────────
def verify_table_block(draft: dict, index: dict, transcript: dict, exclude=()) -> str:
    """확인 패스도 덮개를 고친다 — 그 편의 장면(±이웃) 화면 표를 같이 준다(회차 전체 표가 아니다)."""
    wins = item_windows(draft, index, transcript)
    table, _info = cover_table(index, transcript, exclude, scenes=set(scope_scenes(draft, index, transcript)),
                               own_dialogue=[(a, b) for a, b, k, _p in wins if k == "S"],
                               forbidden=[(a, b) for a, b, k, _p in wins if k == "A"])
    if not table:
        return ""
    return ("\n" + COVER_TABLE_HEADER + "\nN 의 cover 는 이 표에서 고른다 — 화면을 먼저 고르고 그 화면에 보이는 것으로 문장을 쓴다. "
            "문장 길이는 「묶음」 길이 안에.\n" + "\n".join(table) + "\n")


def carry_slots(draft: dict, final: dict, index: dict, transcript: dict) -> None:
    """확인 패스 산출은 validate_versions 를 다시 지나며 slot·required·intent 를 잃는다 — **위치에서 다시 계산한다**(제자리).
    required = 그 내레이션 바로 뒤 항목의 앞이 '이어야 하는 자리'인가. 초안에서 베끼지 않는다(확인 패스가 순서를 바꿀 수 있다). 순수."""
    bare = dict(final, items=[it for it in final.get("items", []) if it["type"] != "N"])
    marks = bridge_marks(bare, index, transcript)
    pos, k, n_items = 0, 0, len(bare["items"])
    for it in final.get("items", []):
        if it["type"] != "N":
            pos += 1
            continue
        k += 1
        it.update(slot=f"N{k:02d}", required=pos in marks, intent=slot_role(marks.get(pos), first=pos == 0, last=pos == n_items))
    final["script_flow"] = "staged"
    if draft.get("hook"):
        final.setdefault("hook", draft["hook"])
