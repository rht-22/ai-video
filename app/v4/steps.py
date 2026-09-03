"""V4 단계 표 — `--from-step` 재개 판정의 **유일한 정본**.

계약 정본 `docs/v4/M1-interfaces.md` §1 · 단계 이름 정본 `docs/v4/v4-plan.md` §2 표.

🛑 이 파일이 있는 이유는 v3 의 사고다. v3 는 `V3_STEPS` 튜플을 들고 있으면서 그것을
**순서로 쓰지 않았다** — `pipeline.py:118` 의 허용값 검사에만 쓰고, 실제 "이 단계부터
다시"는 단계마다 **손으로 적은 멤버십 검사**로 했다(조사 gotcha 4 · 실물 확인:

    grid            from_step == "grid"                                (pipeline.py:259)
    stage1          from_step not in ("grid", "seq_analyze")           (:353)
    chunk_analyze   from_step not in ("chunk_split", "chunk_analyze")  (:490)
    story           from_step not in ("story", "resources")            (:622)
    draft           from_step not in ("draft_render",)                 (:809)
    style           from_step not in ("draft_render", "style")         (:825)
    render          from_step not in ("draft_render","style","render") (:878)

). 집합이 전부 다르고 순서와도 무관해서 결과가 사람의 직관과 갈렸다: `--from-step story`
를 줘도 상류인 grid·stage1·chunk 캐시는 그대로 쓰이고, 거꾸로 `--from-step resources`
는 **상류인 story 를 무효화**한다(:622 는 캐시를 안 읽고 :629 는 재계산도 안 해
story_doc 이 None 으로 남는다). `init` 은 어디서도 비교되지 않아 사실상 no-op 이고,
research 는 무효화 경로가 **아예 없다**(gotcha 5 — 체크포인트가 한 번 생기면 어떤
`--from-step` 으로도 다시 안 돈다).

⇒ v4 는 판정 규칙을 **하나**로 한다: `should_run` = 순번 비교. 단계를 하나 더할 때
고칠 곳이 `V4_STEPS` 한 줄뿐이어야 아무도 안 놓친다.

`should_run` 은 **"캐시를 무효화하는가"** 의 판정이다(계약 §1). "그래서 실제로 캐시를
재사용하는가"는 각 단계가 **상류 지문**으로 다시 본다 — v3 가 남긴 규율 중 지킬 것
(gotcha 9: 지문 재료가 단계마다 달라 각각 다른 변경을 놓쳤다 → 재료는 부르는 쪽이
전량 명시한다).
"""
from __future__ import annotations

# ── 단계 표 (계약 §1 · 이름은 기획서 §2 표) ────────────────────────────────
# 순서가 곧 의미다 — should_run 이 이 튜플의 인덱스만 본다.
# 11 은 "승인 편마다 도는 다섯 조각"이라 한 단계가 아니라 하위 5개다(기획서 §2 표의
# 11행 '후반부' = 아래 `11:*` 다섯). 재개 지점이 그만큼 잘게 필요해서다 — 편집실
# 라운드는 스타일 호출을 다시 하지 않고 재료부터 다시 돈다(기획서 §3 11:style).
V4_STEPS: tuple[str, ...] = (
    "init", "research", "transcribe", "probe", "upload",
    "candidates", "boundary", "verify", "funnel", "flags", "approve",
    "flesh", "detail",
    "11:resources", "11:draft", "11:style", "11:render", "11:validate",
)

# 사람이 CLI 에 치는 번호 → 정본 id. 기획서 §2 표의 `#` 열이 이 열쇠다.
# ⚠ "11" 은 `11:resources`(11단계의 첫 조각)를 가리킨다 — 순번 비교와 합쳐지면
#   `--from-step 11` = 11단계 **전체** 재실행이 된다(계약 §1 의 의도).
# ⚠ 1~5 는 별칭이 없다(계약이 그렇게 못박았다) — 정본 id(init·research·transcribe·
#   probe·upload)로만 지정한다. parse_from_step 의 실패 메시지가 목록 전량을 실으므로
#   `--from-step 4` 를 친 사람은 그 자리에서 `probe` 를 본다.
STEP_ALIASES: dict[str, str] = {"11": "11:resources", "6": "candidates",
                                "6b": "boundary", "6c": "verify", "7": "funnel",
                                "8": "flags", "9": "approve", "10": "flesh",
                                "10a": "detail"}

# 이름 → 순번(0-based). V4_STEPS 에서 파생 — 손으로 적으면 언젠가 갈린다.
STEP_ORDER: dict[str, int] = {name: i for i, name in enumerate(V4_STEPS)}

# 로그용 사람 읽는 이름. 기획서 §2 표의 `#` + `단계` 를 붙인 것이고, 11 하위 다섯은
# 같은 문서 §3 의 소제목("11:style 스타일" 등)을 그대로 쓴다.
# 10a 의 "(선택)" 은 표 문구 그대로다 — 기본 꺼짐이라는 사실이 로그 한 줄에서
# 보이는 것이 이득이다(기획서 §3: "10a 정밀 청취 (선택, 기본 꺼짐)").
STEP_LABELS: dict[str, str] = {
    "init": "1 init",
    "research": "2 research",
    "transcribe": "3 자막 전사",
    "probe": "4 probe",
    "upload": "5 업로드",
    "candidates": "6 후보 편성",
    "boundary": "6b 경계 정밀",
    "verify": "6c 구간 검증",
    "funnel": "7 깔때기",
    "flags": "8 시각 플래그",
    "approve": "9 승인·순위",
    "flesh": "10 살붙이기",
    "detail": "10a 정밀 청취(선택)",
    "11:resources": "11:resources 편집 재료",
    "11:draft": "11:draft 초벌",
    "11:style": "11:style 스타일",
    "11:render": "11:render 최종",
    "11:validate": "11:validate 검증",
}


def _check_table() -> None:
    """표 자체의 모순은 **임포트 시점에** 터뜨린다.

    별칭이 없는 단계를 가리키거나 정본 id 를 가려 버리는 별칭이 들어오면 그 뒤의
    모든 재개 판정이 조용히 엉뚱해진다 — 배선이 아니라 표가 틀린 것이라 첫 임포트에서
    죽는 편이 낫다(조용한 기본값 금지)."""
    if len(STEP_ORDER) != len(V4_STEPS):
        raise RuntimeError(f"V4_STEPS 에 중복이 있다: {V4_STEPS}")
    for alias, target in STEP_ALIASES.items():
        if target not in STEP_ORDER:
            raise RuntimeError(f"별칭 {alias!r} 이 없는 단계 {target!r} 를 가리킨다")
        if alias in STEP_ORDER:
            raise RuntimeError(f"별칭 {alias!r} 이 정본 단계 이름을 가린다")
    missing = [s for s in V4_STEPS if s not in STEP_LABELS]
    if missing or len(STEP_LABELS) != len(V4_STEPS):
        raise RuntimeError(f"STEP_LABELS 가 단계 표와 1:1 이 아니다 — 누락 {missing}")


_check_table()


def _allowed_text() -> str:
    """실패 메시지에 싣는 허용 목록 **전량**.

    별칭은 가리키는 단계의 순번대로 늘어놓는다 — 파이프라인 순서로 읽히고, 같은
    입력이면 같은 문자열이다(결정성)."""
    aliases = sorted(STEP_ALIASES.items(), key=lambda kv: (STEP_ORDER[kv[1]], kv[0]))
    return ("  정본 id: " + " ".join(V4_STEPS) + "\n"
            + "  별칭: " + " ".join(f"{a}={t}" for a, t in aliases))


def parse_from_step(value: str | None) -> str | None:
    """별칭을 정본 id 로 정규화. 모르는 값은 `ValueError`(허용 목록 전량을 메시지에).

    - `None` → `None`(= 처음부터 전부 돈다).
    - 앞뒤 공백만 떼고 **정확히** 맞춘다. 대소문자나 비슷한 이름으로 봐주지 않는다 —
      공백을 안 떼면 목록에 `6c` 가 보이는데 메시지엔 `'6c '` 가 뜨는, 사람이 절대
      못 찾는 실패가 된다. 빈 문자열은 '미지정'이 아니라 오타이므로 실패시킨다
      (조용히 None 으로 떨어지면 전 단계를 다시 도는 요금이 말없이 나간다).
    - 목록을 메시지에 싣는 이유: 무인 노드에서 오타 하나가 편을 죽이는데, 그때
      로그에 허용값이 없으면 사람이 코드를 열어야 한다."""
    if value is None:
        return None
    key = value.strip()
    if key in STEP_ALIASES:
        return STEP_ALIASES[key]
    if key in STEP_ORDER:
        return key
    raise ValueError(
        f"--from-step 은 아래 중 하나여야 한다 — 받은 값: {value!r}\n"
        + _allowed_text()
    )


def should_run(step: str, from_step: str | None) -> bool:
    """`from_step` 이후(**자기 자신 포함**)의 단계인가. 순수·부작용 없음.

    `from_step=None` 이면 전부 True(처음부터 돈다). 규칙은 순번 비교 하나다 —
    이 모듈 독스트링의 v3 사고(집합 7종이 서로 다른 상류를 봤다)를 반복하지 않는
    유일한 방법이다.

    양쪽 모두 별칭을 받는다. `step` 쪽까지 정규화하는 것은 로그 라벨이나 오케스트레이터
    파라미터에서 온 `"6c"` 가 `KeyError` 로 죽지 않게 하려는 것이고, 관문이 하나라
    두 인자의 어휘가 갈릴 수 없다.

    모르는 이름은 `KeyError` 가 아니라 `ValueError` 다(계약 §1) — 멤버십 실패와
    dict 조회 실패가 호출자에게 같은 얼굴로 보여야 한다.

    ⚠ `step` 은 `from_step` 이 None 이어도 **먼저** 검증한다. 오타를 재개 실행에서만
    잡으면 그 단계는 평상시 내내 조용히 True 를 돌려주다가 어느 날 재개에서 처음
    죽는다 — 같은 코드가 실행마다 다른 얼굴이면 안 된다."""
    order = STEP_ORDER[_canonical(step)]
    target = parse_from_step(from_step)
    if target is None:
        return True
    return order >= STEP_ORDER[target]


def step_label(step: str) -> str:
    """로그용 사람 읽는 이름(예: `"6c 구간 검증"`). 별칭도 받는다.

    번호를 이름에 붙여 두는 이유는 기획서 §2 표와 로그를 눈으로 맞추기 위해서다 —
    정본 id 만 찍으면(`verify`) 그것이 몇 번째 단계인지 문서를 열어야 안다."""
    return STEP_LABELS[_canonical(step)]


def _canonical(step: str) -> str:
    """단계 이름 하나를 정본 id 로. 모르는 값은 `ValueError`(목록 전량 포함)."""
    name = parse_from_step(step)
    if name is None:
        # parse_from_step 은 None 만 None 으로 통과시킨다. 단계 이름 자리에 None 은
        # 호출자 결함이므로 from_step 과 같은 얼굴(ValueError)로 되돌린다.
        raise ValueError("단계 이름이 None 이다 — 정본 id 나 별칭을 줘야 한다\n"
                         + _allowed_text())
    return name
