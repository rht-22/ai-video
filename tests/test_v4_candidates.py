"""V4-M3 §2 후보 편성 회귀 가드 — `app/v4/candidates.py`.

고정하는 것은 계약 `docs/v4/M3-interfaces.md` §2 의 문장들이다:

① **프롬프트에 반드시 실리는 것** — 절대초 · quote 를 전사에서 **그대로** · 개수 ·
   exception_sector 신고 · 제목 가안 자수.
② **프롬프트에 실리면 안 되는 것** — v3 규칙 4·5·6(내레이션 자수·라벨 앵커·제목 확정)의
   어휘. 실으면 모델이 후보 16개마다 내레이션을 지어 오고 그 토큰은 전부 버려진다.
③ **검증기는 하나가 걸려도 그 후보만 버린다.** 전량이 걸릴 때만 None.
④ **제목 자수 초과는 반려가 아니라 잘라내고 노트**(가안이라 10단계가 strict 로 다시 건다).
⑤ **반려 소진 = 편 전체 실패** — 시각 정본의 입구라 조용히 통과시키지 않는다.
⑥ **결정성·순수성** — 같은 입력이면 같은 후보 절, 넘겨받은 응답을 제자리에서 고치지 않는다.

🛑 네트워크 0(가짜 클라이언트). **프롬프트 품질**(모델이 실제로 quote 를 그대로 옮기는가,
후보가 정말 다른 아크로 오는가)은 키가 있는 노드의 몫이고 이 파일의 범위 밖이다 —
여기가 고정하는 것은 **구조**뿐이다.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from app.modules.grid.schemas import EXCEPTION_KEYS
from app.v3.seq_analyze import MAX_REASKS
from app.v4 import candidates as C


# ── 재료 ────────────────────────────────────────────────────────────────────

DURATION = 600.0


def _grid(duration: float = DURATION) -> dict:
    """v4 격자의 최소 모양 — `grid.timegrid.build_grid_doc` 이 내는 키만 쓴다."""
    return {
        "source": {"duration_sec": duration, "fps": 30.0, "width": 1920, "height": 1080},
        "scene_cuts": [10.0, 120.0, 300.0],
        "silence": [],
        "arousal": [],
        "words": [
            {"t0": 5.0, "t1": 6.0, "text": "안녕", "prob": 0.95},
            {"t0": 6.0, "t1": 9.0, "text": "여러분", "prob": 0.93},
            {"t0": 120.0, "t1": 121.0, "text": "이건", "prob": 0.21},
            {"t0": 121.0, "t1": 124.0, "text": "대단했다", "prob": 0.19},
        ],
        "span_candidates": [
            {"id": "sp0000", "t_in": 5.0, "t_out": 9.0, "is_audio": True,
             "text": "안녕하세요 여러분"},
            {"id": "sp0001", "t_in": 9.0, "t_out": 120.0, "is_audio": False, "text": ""},
            {"id": "sp0002", "t_in": 120.0, "t_out": 124.0, "is_audio": True,
             "text": "이건 정말 대단한 순간이었습니다"},
        ],
    }


def _cand(i: int, *, start: float = 100.0, template: str = "recap_dialogue",
          **over) -> dict:
    """모델이 낸 후보 하나의 모양(정상판)."""
    out = {
        "id": f"c{i:02d}",
        "template": template,
        "reason": "핵심 갈등이 한 구간에 모여 있다",
        "title_draft": {"line1": "반주 부탁한 여학생", "line2": "돌아온 답은 최악"},
        "segments": [
            {"start_sec": start, "end_sec": start + 25.0, "quote": "이건 정말 대단한 순간"},
            {"start_sec": start + 60.0, "end_sec": start + 90.0, "quote": None},
        ],
    }
    out.update(over)
    return out


def _resp(n: int = 6, **over) -> dict:
    doc = {
        "candidates": [_cand(i + 1, start=100.0 + i * 10) for i in range(n)],
        "exception_sector": {"intro": {"start_sec": 0.0, "end_sec": 43.0},
                             "recap": None, "teaser": None, "credit": None, "end": None},
    }
    doc.update(over)
    return doc


# ── 가짜 Gemini ─────────────────────────────────────────────────────────────

class _FakeModels:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.queue.pop(0) if self.queue else {"candidates": []}
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return SimpleNamespace(
            text=text,
            usage_metadata=SimpleNamespace(prompt_token_count=500_000,
                                           thoughts_token_count=1_000,
                                           candidates_token_count=2_000,
                                           cached_content_token_count=0,
                                           total_token_count=503_000),
            model_version="gemini-3.7-flash-001",
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))])


class _FakeGemini:
    """`gemini.types` 는 **진짜 google-genai 타입**이다 — 가짜 타입으로 조립하면 필드
    이름·타입이 틀려도 통과한다(`tests/test_v4_video.py` 와 같은 규율)."""

    def __init__(self, queue=()):
        from google.genai import types

        self.types = types
        self.models = _FakeModels(queue)
        self.client = SimpleNamespace(models=self.models)
        self.config = SimpleNamespace(model_name="gemini-3.7-flash",
                                      flash_model_name="gemini-3.7-flash",
                                      analysis_thinking_level="high")


HANDLE = SimpleNamespace(uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
                         name="files/abc")


def _run(gemini, **over):
    kwargs = dict(work_title="포핸즈", grid=_grid(), research=None, sample_fps=3.0,
                  templates=C.TEMPLATES_DEFAULT, target_sec=53.0, max_sec=60.0,
                  log=lambda *a, **k: None)
    kwargs.update(over)
    return C.run_candidates(gemini, HANDLE, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# 1. 계약 상수
# ═══════════════════════════════════════════════════════════════════════════

def test_contract_constants():
    """계약 §2 가 값으로 적은 것들 — 바꾸면 하류(8단계 파트 수·10단계 제목)가 갈린다."""
    assert (C.CANDIDATES_MIN, C.CANDIDATES_MAX) == (5, 16)   # 운영자 결정 O3
    assert C.TITLE_DRAFT_MAX_CHARS == 20
    assert C.SEGMENTS_MAX == 8


def test_max_reasks_is_imported_not_redeclared():
    """`MAX_REASKS` 는 v3 것을 import 한다(계약 §2 "재선언 금지").

    v3 가 예산을 바꾸면 6단계도 같이 바뀌어야 한다 — 여기 숫자를 다시 적으면 언젠가
    한쪽만 고쳐지고, 사람은 '재질의 2회'를 두 가지 뜻으로 읽게 된다."""
    assert C.MAX_REASKS is MAX_REASKS
    src = (C.__file__ and open(C.__file__, encoding="utf-8").read()) or ""
    assert "MAX_REASKS = " not in src, "MAX_REASKS 를 이 파일에서 다시 선언했다"


def test_media_resolution_is_low_because_the_fps_budget_assumes_low():
    """4단계 예산(`fps.TOKENS_PER_FRAME` 71)이 LOW 기준이다 — HIGH 는 프레임당 ×4 라
    그 예산으로 정한 fps 가 입력 상한을 넘겨 400 을 받는다."""
    from app.v4 import fps as F

    assert C.CALL_MEDIA_RESOLUTION == "LOW"
    assert F.TOKENS_PER_FRAME == 71 and F.HIGH_FRAME_MULTIPLIER == 4.0


def test_default_templates_are_the_v3_registry():
    """템플릿 어휘의 정본은 v3 레지스트리다(설명도 거기서 온다)."""
    from app.v3.story import STORY_TEMPLATE_SPECS

    assert C.TEMPLATES_DEFAULT == tuple(STORY_TEMPLATE_SPECS)
    assert len(C.TEMPLATES_DEFAULT) == 4


# ═══════════════════════════════════════════════════════════════════════════
# 2. 전사 블록
# ═══════════════════════════════════════════════════════════════════════════

def test_transcript_block_shape_and_low_confidence_tag():
    """계약 §2 예시 모양 `[120.0] …` · 무성 span 은 안 싣는다 · 저확신은 **표시만**."""
    block = C.transcript_block(_grid())
    lines = block.splitlines()
    assert lines[0] == "[5.0] 안녕하세요 여러분"
    assert lines[1].startswith("[120.0] [저확신 0.")
    assert lines[1].endswith("이건 정말 대단한 순간이었습니다")   # 글자는 그대로 남는다
    assert len(lines) == 2, "무성 span 이 실렸다"


def test_transcript_block_marks_low_confidence_only_below_threshold():
    """임계는 v3 `story.LOW_CONF` 하나다(여기서 다시 적지 않는다)."""
    from app.v3.story import LOW_CONF

    assert LOW_CONF == 0.5
    grid = _grid()
    for w in grid["words"]:
        w["prob"] = 0.9
    assert "[저확신" not in C.transcript_block(grid)


def test_transcript_block_without_probabilities_marks_nothing():
    """확률이 없으면 **판정하지 않는다** — 0 으로 채우면 전사가 통째로 저확신이 된다."""
    grid = _grid()
    for w in grid["words"]:
        w.pop("prob")
    assert "[저확신" not in C.transcript_block(grid)


def test_transcript_block_truncation_is_loud():
    """자를 때는 **잘랐다는 사실을 블록 안에** 남긴다(조용한 절단 금지)."""
    full = C.transcript_block(_grid())
    cut = C.transcript_block(_grid(), max_chars=len(full.splitlines()[0]) + 1)
    assert "⚠ 전사" in cut and "줄만 실었다" in cut
    assert C.transcript_block(_grid(), max_chars=10_000) == full   # 넉넉하면 손대지 않는다


def test_transcript_block_is_pure():
    grid = _grid()
    before = copy.deepcopy(grid)
    C.transcript_block(grid)
    assert grid == before


# ═══════════════════════════════════════════════════════════════════════════
# 3. 프롬프트 — 실려야 하는 것
# ═══════════════════════════════════════════════════════════════════════════

def _prompt(**over) -> str:
    kwargs = dict(work_title="포핸즈", transcript=C.transcript_block(_grid()),
                  grid_summary="러닝타임: 00:10:00.000 (600.0초)",
                  templates=C.TEMPLATES_DEFAULT, target_sec=53.0, max_sec=60.0)
    kwargs.update(over)
    return C.build_prompt(**kwargs)


def test_prompt_demands_absolute_seconds():
    """① 절대초 실수 — 시:분:초 표기를 명시적으로 막는다(v4 좌표계의 전제)."""
    p = _prompt()
    assert "절대초" in p
    assert "00:02:00" in p, "거절되는 표기의 실례가 없으면 모델은 계속 그렇게 낸다"


def test_prompt_demands_verbatim_quote_strongly():
    """② 가장 중요한 지시 — quote 를 **전사에서 그대로**. 6c 가 그 글자로 시각을 검증한다."""
    p = _prompt()
    assert "그대로 복사" in p
    assert "다듬지 마라" in p
    assert "null" in p, "대사 없는 조각의 표기를 안 알려주면 모델이 지어낸다"
    assert "지어내면" in p


def test_prompt_carries_counts_length_and_template_names():
    p = _prompt(n_min=5, n_max=16)
    assert "5~16개" in p
    assert "1~8개" in p                       # SEGMENTS_MAX
    assert "53초" in p and "60초" in p
    for name in C.TEMPLATES_DEFAULT:
        assert name in p


def test_prompt_asks_for_exception_sector_with_the_grid_vocabulary():
    """⑤ 인트로·예고·크레딧 자가 신고 — 출력 예시의 키가 격자 어휘와 같아야 한다."""
    p = _prompt()
    assert "exception_sector" in p
    for key in EXCEPTION_KEYS:
        assert f'"{key}"' in p, f"출력 예시에 {key} 가 없다 — 모델이 그 종을 안 낸다"
    assert "이른 쪽" in p, "예고 경계가 불확실할 때의 방향(가왕쇼 사고)이 빠졌다"


def test_prompt_title_is_a_draft_with_the_char_cap():
    p = _prompt()
    assert "가안" in p
    assert f"{C.TITLE_DRAFT_MAX_CHARS}자" in p


def test_prompt_carries_v3_rule7_no_preamble():
    """규칙 7(서론 금지) — 계약 §0 이 가져오라고 한 문구 자산."""
    assert "서론 금지" in _prompt()


def test_prompt_carries_the_low_confidence_vocabulary():
    """규칙 8(대사 신뢰) — v4 에 실재하는 표지는 `[저확신]` 하나이고, 그것만 싣는다.

    ⚠ `[청취]`·`[대사없음]` 은 Stage 2 가 붙이던 표지인데 v4 에는 그 단계가 없다.
    없는 표지를 규칙에 적으면 모델이 전사에서 찾다가 못 찾고 규칙 자체를 무시한다."""
    p = _prompt()
    assert "[저확신" in p
    assert "[청취]" not in p and "[대사없음]" not in p


def test_prompt_reject_note_is_injected():
    p = _prompt(reject_note="- 후보[0] 조각0: 구간 역전")
    assert "직전 제안 반려 사유" in p and "구간 역전" in p
    assert "직전 제안 반려 사유" not in _prompt(), "반려 사유가 없는데 절이 생겼다"


def test_prompt_is_pure_and_deterministic():
    assert _prompt() == _prompt()


def test_prompt_rejects_unknown_template_loudly():
    """모르는 템플릿을 조용히 빼면 모델이 설명 없이 그 이름을 쓰게 된다."""
    with pytest.raises(ValueError, match="모르는 스토리 템플릿"):
        _prompt(templates=("recap_dialogue", "없는템플릿"))


# ═══════════════════════════════════════════════════════════════════════════
# 4. 프롬프트 — 실리면 안 되는 것 (10단계 몫)
# ═══════════════════════════════════════════════════════════════════════════

def test_prompt_does_not_carry_stage10_vocabulary():
    """v3 규칙 4·5·6 의 어휘가 실리면 모델이 후보마다 내레이션·라벨을 지어 온다
    (출력이 잘리고, 탈락 후보의 출력 단가는 입력의 5배다 — 운영자 결정 O4).

    ⚠ 템플릿 설명(v3 레지스트리에서 그대로 가져온다)에는 '내레이션'·'라벨'이라는 **낱말**이
    남아 있다 — 지우면 남의 파일 문구를 베껴 고치는 것이 된다. 그래서 막는 것은 낱말이
    아니라 **지시와 출력 열쇠**이고, 프롬프트는 그 바로 아래에서 "뒷단계가 쓴다"고 못박는다."""
    p = _prompt()
    for banned in ("공백 포함", "12~16자", "서술체", "span_id",
                   '"narration"', '"labels"', '"label"'):
        assert banned not in p, f"10단계 어휘가 실렸다: {banned!r}"
    assert "뒷단계가 쓴다" in p


def test_prompt_output_schema_has_no_narration_or_label_slot():
    """출력 스키마에 자리가 있으면 모델은 채운다 — 자리 자체를 두지 않는다."""
    p = _prompt()
    schema = p[p.index("## 출력"):]
    for key in ("narration", "label", "beats", "span_ids"):
        assert key not in schema


# ═══════════════════════════════════════════════════════════════════════════
# 5. 검증기 — 정상판
# ═══════════════════════════════════════════════════════════════════════════

def _validate(resp, **over):
    kwargs = dict(source_duration_sec=DURATION, templates=C.TEMPLATES_DEFAULT)
    kwargs.update(over)
    return C.validate_response(resp, **kwargs)


def test_validate_happy_path():
    cands, sector, problems = _validate(_resp(6))
    assert problems == []
    assert [c["id"] for c in cands] == ["c01", "c02", "c03", "c04", "c05", "c06"]
    assert cands[0]["segments"][0] == {"start_sec": 100.0, "end_sec": 125.0,
                                       "quote": "이건 정말 대단한 순간"}
    assert cands[0]["segments"][1]["quote"] is None
    assert sector["intro"] == {"start_sec": 0.0, "end_sec": 43.0}
    assert set(sector) == set(EXCEPTION_KEYS), "다섯 키를 전부 실어야 6b 가 '신고 없음'을 안다"


def test_validate_is_pure():
    """순수 — 넘겨받은 응답을 제자리에서 고치지 않는다."""
    resp = _resp(6)
    before = copy.deepcopy(resp)
    _validate(resp)
    assert resp == before


def test_validate_is_deterministic():
    a = _validate(_resp(7))
    b = _validate(_resp(7))
    assert a == b


def test_validate_assigns_ids_without_collision():
    """id 가 빠진 후보에는 `c%02d` 를 주되 **이미 쓰인 이름을 건너뛴다**.

    id 는 checkpoint 의 좌표다(M1 §8) — 겹치면 6c 가 `후보 id 가 중복입니다` 로 죽는다."""
    resp = _resp(5)
    resp["candidates"][0]["id"] = "c02"
    for c in resp["candidates"][1:]:
        c.pop("id")
    cands, _sector, _problems = _validate(resp)
    ids = [c["id"] for c in cands]
    assert ids[0] == "c02"
    assert len(set(ids)) == len(ids), f"id 가 겹쳤다: {ids}"


def test_validate_drops_duplicate_id_candidate_only():
    resp = _resp(6)
    resp["candidates"][3]["id"] = resp["candidates"][0]["id"]
    cands, _sector, problems = _validate(resp)
    assert len(cands) == 5
    assert any("중복" in p for p in problems)


# ═══════════════════════════════════════════════════════════════════════════
# 6. 검증기 — 항목별 (하나가 걸려도 그 후보만 버린다)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("mutate, needle", [
    (lambda c: c.update(template="없는템플릿"), "template"),
    (lambda c: c.update(reason="  "), "reason"),
    (lambda c: c.update(segments=[]), "segments"),
    (lambda c: c.update(segments=[{"start_sec": 10.0 + i, "end_sec": 12.0 + i,
                                   "quote": None} for i in range(9)]), "8개 이하"),
    (lambda c: c["segments"][0].update(start_sec=200.0, end_sec=150.0), "구간 역전"),
    (lambda c: c["segments"][0].update(start_sec="00:02:00"), "숫자가 아니다"),
    (lambda c: c["segments"][0].update(end_sec=DURATION + 5.0), "소스 범위 밖"),
    (lambda c: c["segments"][0].update(start_sec=-1.0), "소스 범위 밖"),
    (lambda c: c["segments"][0].update(quote=123), "quote"),
])
def test_validate_drops_only_the_broken_candidate(mutate, needle):
    """계약 §2 ⚠ — 하나가 걸려도 그 후보만 버리고 나머지는 살린다."""
    resp = _resp(6)
    mutate(resp["candidates"][2])
    cands, _sector, problems = _validate(resp)
    assert cands is not None and len(cands) == 5, "성한 후보까지 날아갔다"
    assert any(needle in p for p in problems), problems
    assert all("후보[2]" not in p or needle in p for p in problems)


def test_validate_returns_none_only_when_everything_is_gone():
    resp = _resp(6)
    for c in resp["candidates"]:
        c["template"] = "없는템플릿"
    cands, _sector, problems = _validate(resp)
    assert cands is None
    assert any("전량 탈락" in p for p in problems)


def test_validate_bool_is_not_a_number():
    """`True` 는 1 이 아니다 — 파이썬에서 조용히 숫자로 읽히면 0초짜리 조각이 생긴다."""
    resp = _resp(6)
    resp["candidates"][0]["segments"][0]["start_sec"] = True
    cands, _sector, problems = _validate(resp)
    assert len(cands) == 5 and any("숫자가 아니다" in p for p in problems)


def test_validate_counts_too_few_and_too_many():
    """개수 부족은 사유에 **몇 개 왔는지** 적고, 초과는 앞에서부터 자른다(결정적)."""
    _c, _s, few = _validate(_resp(3))
    assert any("3개" in p and "최소 5" in p for p in few)

    cands, _s, many = _validate(_resp(18))
    assert len(cands) == C.CANDIDATES_MAX
    assert [c["id"] for c in cands] == [f"c{i + 1:02d}" for i in range(C.CANDIDATES_MAX)]
    assert any("18개" in p for p in many)


def test_validate_rejects_the_whole_response_when_it_is_unusable():
    for resp, needle in [("문자열", "객체가 아니다"),
                         ({"candidates": "x"}, "candidates 배열이 없다"),
                         ({"candidates": []}, "candidates 배열이 없다")]:
        cands, sector, problems = _validate(resp)
        assert cands is None and sector is None
        assert any(needle in p for p in problems), problems


def test_validate_requires_a_real_source_duration():
    """길이는 격자에서 온다 — 0 이 오면 배선 오류이고, 전량 통과시키면 벨트가 사라진다."""
    with pytest.raises(ValueError, match="소스 길이"):
        _validate(_resp(6), source_duration_sec=0)


# ═══════════════════════════════════════════════════════════════════════════
# 7. 검증기 — 제목 가안(반려가 아니라 절단 + 노트)
# ═══════════════════════════════════════════════════════════════════════════

def test_title_over_limit_is_trimmed_with_a_note_not_a_rejection():
    """계약 §2 ⚠ — 여기서 반려하면 제목 한 줄 때문에 후보 16개가 통째로 날아간다."""
    long = "가" * 40
    resp = _resp(6)
    resp["candidates"][0]["title_draft"]["line1"] = long
    cands, _sector, problems = _validate(resp)
    assert len(cands) == 6, "제목 때문에 후보를 버렸다"
    assert cands[0]["title_draft"]["line1"] == "가" * C.TITLE_DRAFT_MAX_CHARS
    assert any("잘랐다" in n for n in cands[0]["notes"]), cands[0]
    assert problems == [], "제목 절단은 반려 사유가 아니다"


@pytest.mark.parametrize("raw", [None, "한 줄 제목", 12345, {"line1": None}])
def test_title_draft_shapes_never_drop_a_candidate(raw):
    resp = _resp(6)
    resp["candidates"][0]["title_draft"] = raw
    cands, _sector, _problems = _validate(resp)
    assert len(cands) == 6
    t = cands[0]["title_draft"]
    assert set(t) == {"line1", "line2"} and all(isinstance(v, str) for v in t.values())
    if raw != {"line1": None}:
        assert cands[0].get("notes"), "손댄 사실이 안 남았다"


# ═══════════════════════════════════════════════════════════════════════════
# 8. 검증기 — exception_sector
# ═══════════════════════════════════════════════════════════════════════════

def test_unknown_sector_key_rejects_the_whole_response():
    """계약 §2 — 모르는 키는 반려다. 조용히 무시하면 모델이 새 이름으로 신고한 예고
    구역이 판정에서 통째로 빠지고 그 후보가 그대로 나간다(가왕쇼 6화 사고)."""
    resp = _resp(6, exception_sector={"intro": None, "preview": {"start_sec": 1.0,
                                                                "end_sec": 2.0}})
    cands, sector, problems = _validate(resp)
    assert cands is None and sector is None
    assert any("모르는 exception_sector 키" in p and "preview" in p for p in problems)


def test_sector_shapes():
    """값 하나가 못 읽히면 **그 키만** null(6b 꼬리 의무 창이 덮는다) · 끝은 소스로 당긴다."""
    resp = _resp(6, exception_sector={
        "intro": {"start_sec": 0.0, "end_sec": 43.0},
        "recap": "이상한 값",
        "teaser": {"start_sec": 500.0, "end_sec": DURATION + 30.0},
        "credit": {"start_sec": 300.0, "end_sec": 200.0},
        "end": None})
    cands, sector, problems = _validate(resp)
    assert len(cands) == 6, "sector 하나 때문에 후보를 버렸다"
    assert sector["intro"] == {"start_sec": 0.0, "end_sec": 43.0}
    assert sector["recap"] is None and sector["credit"] is None
    assert sector["teaser"] == {"start_sec": 500.0, "end_sec": DURATION}
    assert len([p for p in problems if "exception_sector" in p]) == 3


def test_missing_sector_is_recorded_not_ignored():
    resp = _resp(6)
    resp.pop("exception_sector")
    cands, sector, problems = _validate(resp)
    assert len(cands) == 6
    assert all(sector[k] is None for k in EXCEPTION_KEYS)
    assert any("exception_sector 가 없다" in p for p in problems)


def test_sector_output_is_readable_by_the_funnel():
    """7단계가 이 모양을 그대로 읽어야 한다 — 이름이 갈리면 예고 게이트가 조용히 빈다."""
    from app.v4 import funnel

    _c, sector, _p = _validate(_resp(6, exception_sector={
        "intro": None, "recap": None, "credit": None, "end": None,
        "teaser": {"start_sec": 400.0, "end_sec": 500.0}}))
    cand = {"segments": [{"start_sec": 380.0, "end_sec": 420.0}]}
    problems = funnel.hard_problems(cand, exception_sectors=sector,
                                    source_duration_sec=DURATION,
                                    speech_intervals=[], min_sec=0.0, max_sec=1000.0)
    assert any("sector_overlap" in p for p in problems), problems


# ═══════════════════════════════════════════════════════════════════════════
# 9. 실행 — 호출 설정·반려 루프·실패
# ═══════════════════════════════════════════════════════════════════════════

def test_run_calls_the_model_once_with_the_contract_settings():
    gem = _FakeGemini([_resp(6)])
    section, audit = _run(gem)

    assert len(gem.models.calls) == 1
    call = gem.models.calls[0]
    assert call["model"] == gem.config.model_name, "영상을 보는 호출은 Pro 슬롯이다"
    cfg = call["config"]
    assert cfg.temperature == 0.0                       # 결정성 조항
    assert cfg.media_resolution.name.endswith("LOW")
    # SDK 가 enum 으로 승격한다(`ThinkingLevel.HIGH`) — 값으로 비교한다.
    level = cfg.thinking_config.thinking_level
    assert str(getattr(level, "value", level)).lower() == "high", \
        "v3 Stage 1 과 같은 자(config.analysis_thinking_level)를 써야 한다"
    # 타임아웃은 기획서 §7 "6단계 ≥450초". ⚠ SDK 의 단위는 **밀리초**다(video.py 실측).
    assert cfg.http_options.timeout == int(C.CALL_TIMEOUT_SEC * 1000) >= 450_000
    # 영상은 **전체 한 파트**다(조각 첨부는 8단계) — offset 이 붙으면 안 된다.
    parts = [p for p in call["contents"] if getattr(p, "file_data", None) is not None]
    assert len(parts) == 1
    meta = parts[0].video_metadata.model_dump(exclude_none=True)
    assert meta == {"fps": 3.0}, meta

    assert [c["id"] for c in section["candidates"]] == [f"c{i:02d}" for i in range(1, 7)]
    assert section["exception_sectors"]["intro"]["end_sec"] == 43.0
    assert section["source_duration_sec"] == DURATION
    assert audit["attempts"][0]["usage"]["prompt"] == 500_000


def test_run_section_is_deterministic_and_carries_no_clock():
    """후보 절은 지문·재개 대조가 그 위에 선다 — 소요·시각이 들어가면 안 된다."""
    a, _ = _run(_FakeGemini([_resp(6)]))
    b, _ = _run(_FakeGemini([_resp(6)]))
    assert a == b
    # 파일 바이트까지 같아야 재개 대조가 성립한다(반올림·키 순서 포함).
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(b, ensure_ascii=False, sort_keys=True)
    assert "elapsed" not in json.dumps(a, ensure_ascii=False)


def test_run_section_carries_the_fingerprint_material():
    """계약 §5 — 6 지문 = [격자 지문, sample_fps, **프롬프트 sha**, 모델명, 템플릿 키,
    (n_min,n_max)]. 배선이 그 재료를 여기서 받는다."""
    section, audit = _run(_FakeGemini([_resp(6)]))
    assert section["schema"] == C.SCHEMA_CANDIDATES == "v4_candidates/v1"
    assert section["sample_fps"] == 3.0
    assert section["templates"] == list(C.TEMPLATES_DEFAULT)
    assert section["prompt_sha"] == audit["prompt_sha"]
    assert len(section["prompt_sha"]) == 12
    assert set(section) == {"schema", "source_duration_sec", "sample_fps", "prompt_sha",
                            "templates", "candidates", "exception_sectors"}


def test_run_records_the_transcript_size_for_the_fps_measurement():
    """계약 §2 — 4단계는 **추정치**로 fps 를 정했다. 실제 블록 길이를 남겨야 M8 이
    그 둘을 맞대어 환산 상수를 갈아낄 수 있다."""
    _section, audit = _run(_FakeGemini([_resp(6)]))
    assert audit["transcript_chars"] == len(C.transcript_block(_grid()))
    assert audit["transcript_lines"] == 2


def test_run_reask_injects_the_reason_and_the_second_try_passes():
    """1차 반려(후보 부족) → 사유 주입 → 2차 통과. audit 에 시도별 전량 기록."""
    gem = _FakeGemini([_resp(2), _resp(6)])
    section, audit = _run(gem)

    assert len(gem.models.calls) == 2
    first = [c for c in gem.models.calls[0]["contents"] if isinstance(c, str)][0]
    second = [c for c in gem.models.calls[1]["contents"] if isinstance(c, str)][0]
    assert "직전 제안 반려 사유" not in first
    assert "직전 제안 반려 사유" in second and "최소 5개" in second

    assert len(section["candidates"]) == 6
    assert audit["reasks_used"] == 1
    assert [a["accepted"] for a in audit["attempts"]] == [False, True]
    assert audit["attempts"][0]["problems"], "1차 사유가 안 남았다"
    assert audit["attempts"][0]["usage"] is not None, "반려된 시도의 토큰도 남긴다"
    assert audit["prompt_sha"] == audit["attempts"][0]["prompt_sha"], \
        "지문은 1차 프롬프트로 잰다 — 재질의 프롬프트로 재면 실행마다 달라진다"


def test_run_parse_failure_is_reject_material_not_a_crash():
    """파싱 실패는 이 레포 실측의 상시 모드다(분석 22회 중 12회) — 반려 재료로 쓴다."""
    gem = _FakeGemini(["{이건 JSON 이 아니다", _resp(6)])
    section, audit = _run(gem)
    assert len(section["candidates"]) == 6
    assert any("파싱 실패" in p for p in audit["attempts"][0]["problems"])


def test_run_exhausted_reasks_fails_the_episode():
    """⑤ 반려 소진 = 편 전체 실패 — 시각 정본의 입구라 조용히 통과시키지 않는다."""
    gem = _FakeGemini([_resp(2), _resp(2), _resp(2), _resp(6)])
    with pytest.raises(ValueError, match="재질의") as e:
        _run(gem)
    assert len(gem.models.calls) == 1 + MAX_REASKS, "재질의 예산을 안 지켰다"
    assert "시도 1" in str(e.value) and "시도 3" in str(e.value), \
        "왜 죽었는지가 안 남으면 사람이 되짚을 수 없다"


def test_run_keeps_going_when_some_candidates_are_dropped():
    """부분 실패로 하한을 넘기면 재질의 없이 진행하되 손댄 항목을 전부 남긴다."""
    resp = _resp(7)
    resp["candidates"][0]["template"] = "없는템플릿"
    gem = _FakeGemini([resp])
    section, audit = _run(gem)
    assert len(gem.models.calls) == 1
    assert len(section["candidates"]) == 6
    assert audit["attempts"][0]["accepted"] is True
    assert audit["attempts"][0]["problems"], "버린 이유가 안 남았다"


def test_run_propagates_api_errors_instead_of_reasking():
    """400 을 세 번 보내는 것은 요금만 세 배다(E11 규약 — video.classify_error)."""
    from app.v4.video import VideoCallError

    err = VideoCallError("영상 호출 실패(permanent)", kind="permanent")
    gem = _FakeGemini([err])
    with pytest.raises(VideoCallError):
        _run(gem)
    assert len(gem.models.calls) == 1


def test_run_requires_the_grid_duration():
    gem = _FakeGemini([_resp(6)])
    grid = _grid()
    grid["source"].pop("duration_sec")
    with pytest.raises(ValueError, match="소스 길이"):
        _run(gem, grid=grid)
    assert gem.models.calls == [], "비싼 호출을 태우고 나서 죽었다"


def test_run_hint_mismatch_uses_the_v3_judgement():
    """휴리스틱 대조는 v3 함수 하나가 한다 — 관용치(±2s)를 여기 다시 적지 않는다."""
    gem = _FakeGemini([_resp(6)])
    _section, audit = _run(gem)
    assert "hint_mismatch" in audit and isinstance(audit["hint_mismatch"], list)
    assert "hints" in audit


def test_run_accepts_research_as_checkpoint_or_string():
    """v3 와 같은 열쇠(`work_context`) — 체크포인트를 그대로 넘겨도 된다."""
    gem = _FakeGemini([_resp(6)])
    _run(gem, research={"work_context": "포핸즈는 피아노 드라마다"})
    prompt = [c for c in gem.models.calls[0]["contents"] if isinstance(c, str)][0]
    assert "포핸즈는 피아노 드라마다" in prompt

    gem2 = _FakeGemini([_resp(6)])
    _run(gem2, research="문자열도 받는다")
    prompt2 = [c for c in gem2.models.calls[0]["contents"] if isinstance(c, str)][0]
    assert "문자열도 받는다" in prompt2


# ═══════════════════════════════════════════════════════════════════════════
# 10. 하류 접점 — 6c 가 이 후보를 그대로 읽는다
# ═══════════════════════════════════════════════════════════════════════════

def test_candidates_are_readable_by_verify():
    """6단계 산출이 6c 의 입력이다 — 모양이 갈리면 그 자리에서 죽는다(계약 §5 배선)."""
    from app.v4 import verify

    section, _audit = _run(_FakeGemini([_resp(6)]))
    segments = [{"start_sec": 100.0, "end_sec": 125.0,
                 "text": "이건 정말 대단한 순간이었습니다"}]
    kept, record = verify.verify_candidates(
        section["candidates"], segments=segments, source_duration_sec=DURATION,
        grid_times=[100.0, 125.0])
    assert record["kept"], record
    assert len(kept) + len(record["dropped"]) == len(section["candidates"])
