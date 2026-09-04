"""V4 단계 표 회귀 가드 (M1 · 계약 `docs/v4/M1-interfaces.md` §1).

고정하는 것은 **판정 규칙이 하나라는 사실**이다. v3 는 "이 단계부터 다시"를 단계마다
손으로 적은 멤버십 검사 7종(집합은 6가지)으로 했고 각각 다른 상류를 봤다 —
`--from-step story` 는 상류 캐시를 그대로 쓰고, `--from-step resources` 는 거꾸로
상류인 story 를 무효화했다(조사 gotcha 4 · `app/v3/pipeline.py:259·353·490·622·809·
825·878`). v4 는 `should_run` = 순번 비교 하나뿐이라, 아래 테스트가 무너지면 그
사고가 돌아온 것이다.

값으로 못박는 것 여섯:
  ① `V4_STEPS` 가 기획서 §2 단계 표와 **1:1**(개수·순서·이름) — 표를 실제로 파싱해
     대조한다. 코드에만 단계를 더하거나 표에만 더하면 그 자리에서 깨진다.
  ② 별칭 정규화(정본 id·별칭 둘 다 받는다).
  ③ fail-loud 메시지가 **허용 목록 전량**을 싣는다(무인 노드에서 오타를 그 로그
     한 줄로 고칠 수 있어야 한다).
  ④ `should_run` 경계 — 자기 자신 포함, 직전 단계는 제외.
  ⑤ 11 하위 id 순서 — `--from-step 11` = 11단계 전체 재실행.
  ⑥ 순수성·결정성 — 모듈 상태를 건드리지 않고, 같은 입력이면 같은 출력.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app.v4 import steps as S

REPO = Path(__file__).resolve().parents[1]


# ── ① 기획서 §2 단계 표와 1:1 ──────────────────────────────────────────────
def _plan_step_rows() -> list[tuple[str, str]]:
    """`docs/v4/v4-plan.md` §2 표에서 (`#`, `단계`) 열만 뽑는다.

    헤더 `| # | 단계 |` 로 표를 특정한다 — 같은 문서에 5열 표가 또 있어서
    (§8 마일스톤) 열 개수만으로는 고를 수 없다."""
    text = (REPO / "docs" / "v4" / "v4-plan.md").read_text(encoding="utf-8")
    rows: list[tuple[str, str]] = []
    in_table = False
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("|"):
            if in_table:
                break            # 표가 끝났다
            continue
        if cells[:2] == ["#", "단계"]:
            in_table = True
            continue
        if not in_table:
            continue
        if set("".join(cells)) <= {"-", ":"}:
            continue             # 헤더 구분선
        rows.append((cells[0], cells[1]))
    return rows


def test_plan_table_is_parsable():
    """표 파싱 자체를 먼저 고정한다 — 파서가 조용히 0행을 뽑으면 ①이 통과해 버린다."""
    rows = _plan_step_rows()
    assert len(rows) == 14, rows
    assert rows[0] == ("1", "init")
    assert rows[7] == ("6c", "구간 검증")
    assert rows[-1] == ("11", "후반부")


def test_v4_steps_matches_plan_table_one_to_one():
    """§2 표 14행 = 앞 13단계 + 11행이 펼쳐진 하위 5개. 개수·순서·이름 전부."""
    rows = _plan_step_rows()
    # 11 은 '승인 편마다 도는 다섯 조각'이라 표 한 행이 하위 5개로 펼쳐진다.
    assert len(S.V4_STEPS) == (len(rows) - 1) + 5 == 18

    for i, (num, name) in enumerate(rows[:-1]):
        step = S.V4_STEPS[i]
        # 라벨은 표의 두 열을 그대로 붙인 것 — 로그와 문서를 눈으로 맞추기 위해.
        # 이 등식이 곧 '표 i행 = V4_STEPS[i]' 의 증거다(이름까지 대조된다).
        assert S.step_label(step) == f"{num} {name}", step
        # 별칭이 있는 행(6 이후)은 그 번호가 같은 단계를 가리켜야 한다.
        if num in S.STEP_ALIASES:
            assert S.parse_from_step(num) == step, (num, step)

    sub = S.V4_STEPS[13:]
    assert sub == ("11:resources", "11:draft", "11:style", "11:render",
                   "11:validate")
    for step in sub:
        assert S.step_label(step).startswith(step + " "), step


def test_step_order_is_derived_and_dense():
    """STEP_ORDER 는 V4_STEPS 에서 파생 — 손으로 적힌 순번이 끼면 언젠가 갈린다."""
    assert S.STEP_ORDER == {n: i for i, n in enumerate(S.V4_STEPS)}
    assert sorted(S.STEP_ORDER.values()) == list(range(len(S.V4_STEPS)))


def test_aliases_are_the_plan_numbers_of_steps_six_and_later():
    """별칭 집합 = §2 표에서 6 이후 행의 `#`.

    ⚠ 1~5 는 별칭이 **없다**(계약이 그렇게 못박았다) — `--from-step 4` 는 실패하고
    `probe` 를 써야 한다. 이 비대칭을 테스트로 드러내 둔다(보고서 contract_issues)."""
    rows = _plan_step_rows()
    late = {num for num, _ in rows if num not in {"1", "2", "3", "4", "5"}}
    assert set(S.STEP_ALIASES) == late
    for num in {"1", "2", "3", "4", "5"}:
        with pytest.raises(ValueError):
            S.parse_from_step(num)


def test_alias_table_is_self_consistent():
    """별칭은 실재하는 단계를 가리키고 정본 이름을 가리지 않는다(임포트 시 검사)."""
    for alias, target in S.STEP_ALIASES.items():
        assert target in S.STEP_ORDER
        assert alias not in S.STEP_ORDER
    S._check_table()          # 표가 모순이면 여기서 RuntimeError


# ── ② 별칭 정규화 ─────────────────────────────────────────────────────────
def test_parse_from_step_normalizes_aliases_and_canonical_ids():
    assert S.parse_from_step(None) is None
    assert S.parse_from_step("6c") == "verify"
    assert S.parse_from_step("verify") == "verify"          # 정본 id 도 받는다
    assert S.parse_from_step("11") == "11:resources"        # 상위 → 첫 하위
    assert S.parse_from_step("11:style") == "11:style"
    assert S.parse_from_step("10a") == "detail"
    assert S.parse_from_step("init") == "init"


def test_parse_from_step_trims_surrounding_whitespace():
    """공백을 안 떼면 목록엔 `6c` 가 보이는데 메시지엔 `'6c '` 가 떠서 사람이 못 찾는다."""
    assert S.parse_from_step("  6c \n") == "verify"


def test_parse_from_step_is_idempotent():
    """정규화 결과를 다시 넣어도 같다 — 배선이 두 번 정규화해도 안전해야 한다."""
    for name in list(S.V4_STEPS) + list(S.STEP_ALIASES):
        once = S.parse_from_step(name)
        assert S.parse_from_step(once) == once


# ── ③ fail-loud ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "  ", "6d", "verify2", "story", "resources",
                                 "draft_render", "11:", "11:resource", "VERIFY"])
def test_unknown_from_step_fails_loud(bad):
    """모르는 값은 조용히 None(=처음부터)으로 떨어지지 않는다.

    v3 단계 이름(story·resources·draft_render)을 특히 넣어 둔다 — v4 배선이 v3 문서를
    베끼면 그 이름이 그대로 들어올 텐데, 그때 조용히 전 단계를 다시 돌면 요금이
    말없이 나간다. 빈 문자열도 '미지정'이 아니라 오타다."""
    with pytest.raises(ValueError) as exc:
        S.parse_from_step(bad)
    assert "--from-step" in str(exc.value)


def test_error_message_carries_the_whole_allowed_list():
    """메시지에 정본 id 전량 + 별칭 전량. 사람이 그 로그 한 줄로 오타를 고쳐야 한다."""
    with pytest.raises(ValueError) as exc:
        S.parse_from_step("nope")
    msg = str(exc.value)
    for name in S.V4_STEPS:
        assert name in msg, name
    for alias, target in S.STEP_ALIASES.items():
        assert f"{alias}={target}" in msg, alias
    assert "nope" in msg          # 받은 값도 함께


def test_error_message_is_deterministic_and_in_pipeline_order():
    """같은 실패는 늘 같은 문자열(결정성). 별칭은 가리키는 단계 순번대로 늘어선다."""
    def _msg():
        try:
            S.parse_from_step("nope")
        except ValueError as e:
            return str(e)
        raise AssertionError("실패해야 한다")

    assert _msg() == _msg()
    msg = _msg()
    positions = [msg.index(f"{a}=") for a, _ in
                 sorted(S.STEP_ALIASES.items(),
                        key=lambda kv: (S.STEP_ORDER[kv[1]], kv[0]))]
    assert positions == sorted(positions)


@pytest.mark.parametrize("bad", ["nope", "", "story", None])
def test_should_run_rejects_unknown_step_name(bad):
    """`step` 자리의 오타는 from_step 이 None 이어도 잡는다 — 같은 코드가 실행마다
    다른 얼굴이면(평상시 True · 재개에서만 죽음) 아무도 못 찾는다.
    그리고 KeyError 가 아니라 ValueError 다(계약 §1)."""
    with pytest.raises(ValueError):
        S.should_run(bad, None)
    with pytest.raises(ValueError):
        S.should_run(bad, "verify")


def test_should_run_rejects_unknown_from_step():
    with pytest.raises(ValueError):
        S.should_run("verify", "nope")


def test_step_label_rejects_unknown():
    with pytest.raises(ValueError):
        S.step_label("nope")


# ── ④ should_run 경계 ────────────────────────────────────────────────────
def test_should_run_none_runs_everything():
    assert all(S.should_run(s, None) for s in S.V4_STEPS)


@pytest.mark.parametrize("step", S.V4_STEPS)
def test_should_run_includes_itself(step):
    """`--from-step X` 는 X 를 **다시 돈다**(재개 지점이지 '그 다음부터'가 아니다)."""
    assert S.should_run(step, step) is True


def test_should_run_is_a_single_order_comparison():
    """전 조합을 순번 비교와 대조 — 규칙이 하나임을 못박는다(v3 는 7군데였다)."""
    for i, step in enumerate(S.V4_STEPS):
        for j, frm in enumerate(S.V4_STEPS):
            assert S.should_run(step, frm) is (i >= j), (step, frm)


def test_should_run_excludes_the_step_just_before():
    """직전 단계는 안 돈다 — 경계가 한 칸 밀리면 비싼 단계가 조용히 재실행된다."""
    for i in range(1, len(S.V4_STEPS)):
        assert S.should_run(S.V4_STEPS[i - 1], S.V4_STEPS[i]) is False


def test_should_run_accepts_aliases_on_both_sides():
    """로그 라벨·오케스트레이터 파라미터에서 온 `"6c"` 가 KeyError 로 죽지 않는다."""
    assert S.should_run("6c", "6") is True
    assert S.should_run("6", "6c") is False
    assert S.should_run("verify", "6c") is True


def test_upload_precedes_candidates_and_verify_follows_boundary():
    """비싼 단계 순서 — 업로드(5)는 후보 편성(6) 앞, 검증(6c)은 경계 정밀(6b) 뒤."""
    assert S.STEP_ORDER["upload"] < S.STEP_ORDER["candidates"]
    assert S.STEP_ORDER["boundary"] < S.STEP_ORDER["verify"] < S.STEP_ORDER["funnel"]
    assert S.STEP_ORDER["approve"] < S.STEP_ORDER["flesh"] < S.STEP_ORDER["detail"]


# ── ⑤ 11 하위 id ────────────────────────────────────────────────────────
def test_from_step_eleven_reruns_the_whole_eleventh_stage():
    """`11` 은 `11:resources` 의 별칭 → 11단계 다섯 조각이 전부 다시 돈다."""
    sub = S.V4_STEPS[13:]
    assert all(S.should_run(s, "11") for s in sub)
    # 10단계까지는 안 돈다(승인·살붙이기를 다시 하면 요금이 또 나간다).
    assert not any(S.should_run(s, "11") for s in S.V4_STEPS[:13])


def test_eleventh_substeps_are_ordered_resources_first():
    """다섯 조각의 순서는 재료 → 초벌 → 스타일 → 최종 → 검증(기획서 §3)."""
    assert [S.STEP_ORDER[s] for s in
            ("11:resources", "11:draft", "11:style", "11:render", "11:validate")] \
        == sorted(S.STEP_ORDER[s] for s in S.V4_STEPS[13:])
    # 편집실 라운드 규약: 스타일부터 재개하면 재료·초벌은 그대로 쓴다.
    assert S.should_run("11:render", "11:style") is True
    assert S.should_run("11:draft", "11:style") is False
    assert S.should_run("11:resources", "11:draft") is False


def test_eleventh_substeps_are_all_after_flesh():
    """11 은 살붙이기(10)·정밀 청취(10a) 뒤다 — 승인 편마다 도는 후반부."""
    assert min(S.STEP_ORDER[s] for s in S.V4_STEPS[13:]) > S.STEP_ORDER["detail"]


# ── ⑥ 순수성·결정성 ─────────────────────────────────────────────────────
def test_module_tables_are_not_mutated():
    """판정 함수는 순수 — 모듈 표를 건드리면 다음 잡의 재개가 달라진다."""
    before = copy.deepcopy((S.V4_STEPS, S.STEP_ALIASES, S.STEP_ORDER, S.STEP_LABELS))
    for s in S.V4_STEPS:
        S.should_run(s, "verify")
        S.step_label(s)
        S.parse_from_step(s)
    for a in S.STEP_ALIASES:
        S.parse_from_step(a)
    with pytest.raises(ValueError):
        S.parse_from_step("nope")
    after = (S.V4_STEPS, S.STEP_ALIASES, S.STEP_ORDER, S.STEP_LABELS)
    assert before == after


def test_repeated_calls_are_stable():
    """같은 입력이면 같은 출력 — 재개 판정에 실행 순서 의존이 끼면 안 된다."""
    for _ in range(3):
        assert S.should_run("11:style", "6c") is True
        assert S.should_run("probe", "6c") is False
        assert S.parse_from_step("11") == "11:resources"
        assert S.step_label("6c") == "6c 구간 검증"
