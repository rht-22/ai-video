"""E14 회귀 가드 — 자막 최소 노출 시간(읽기 속도 하한).

발주서: ves-orchestrator `docs/prompts/e14-subtitle-min-exposure.md`.

E11·E13 과 달리 이 변경은 `--transcribe-backend` 플래그 뒤에 숨지 않는다.
`merge_subtitle_segments` 는 **모든 생성이 지나는 길**이라(elevenlabs·whisper·SRT 파싱
전부) 여기서 어긋나면 전 채널 자막 타이밍이 한 번에 바뀐다. 그래서 이 파일은 두 가지를
같이 고정한다:

1. 하한이 실제로 걸리는가 (못 읽는 줄이 사라지는가)
2. **하한이 걸리지 않아야 할 것에 걸리지 않는가** — 병합 결과 자체·이미 겹친 쌍·
   짧은 감탄사·start_sec.
"""
from __future__ import annotations

import pytest

from app.modules.speech import SpeechSegment
from app.modules.subtitle import (
    SUBTITLE_MIN_CHARS_PER_SEC,
    SUBTITLE_MIN_EXPOSURE_SEC,
    _enforce_min_exposure,
    merge_subtitle_segments,
)


def seg(start: float, end: float, text: str) -> SpeechSegment:
    return SpeechSegment(start_sec=start, end_sec=end, text=text)


def word(n: int, start: int = 0) -> str:
    """서로 다른 한글 음절 n 자. 같은 글자를 반복하면 merge 의 환각 필터가
    먼저 잡아 버려서(_is_hallucinated_text) 하한 검증이 안 된다."""
    return "".join(chr(0xAC00 + start + i) for i in range(n))


def floor(segs, **kw):
    kw.setdefault("min_chars_per_sec", SUBTITLE_MIN_CHARS_PER_SEC)
    kw.setdefault("min_exposure_sec", SUBTITLE_MIN_EXPOSURE_SEC)
    kw.setdefault("max_duration_sec", 6.0)
    return _enforce_min_exposure(list(segs), **kw)


# ── 하한이 걸려야 하는 것 ───────────────────────────────────────────────────

def test_고립된_초단시간_cue_가_절대하한까지_늘어난다():
    """실측 9번: 0.07초짜리 cue. 다음 cue 까지 1.02초 떨어져 있어 병합 대상이 아니었다.

    min_duration_sec=0.6 은 gap ≤ max_gap_sec 일 때만 발동하는 **병합 힌트**라
    이 줄을 구제하지 못했다 — 그래서 0.07초가 그대로 화면에 나갔다.
    """
    out = floor([seg(10.0, 10.07, "수도."), seg(11.09, 12.5, "다음 줄입니다")])
    assert out[0].end_sec == pytest.approx(10.4, abs=1e-6)   # 3자 → 절대 하한 0.4 가 지배
    assert out[1] == seg(11.09, 12.5, "다음 줄입니다")        # 뒤 cue 는 무관


def test_시간은_넘겨도_읽기속도가_모자라면_늘어난다():
    """실측 10번: 0.72초/18자 = 25자/초. min_duration_sec=0.6 은 넘겼지만 못 읽는다.

    시간 하한만으로는 절대 안 잡히는 케이스 — E14 가 '시간'이 아니라 '읽기 속도'
    하한인 이유가 이것이다.
    """
    text = "활짝 예쁜 꽃이 피었습니다 어라요"  # 18자
    assert len(text) == 18
    out = floor([seg(5.0, 5.72, text), seg(20.0, 21.0, "한참 뒤")])
    assert out[0].end_sec == pytest.approx(5.0 + 18 / 12.0, abs=1e-6)  # 1.5초 필요
    assert len(out[0].text) / (out[0].end_sec - out[0].start_sec) == pytest.approx(12.0)


def test_마지막_cue_는_다음_줄_제약이_없다():
    out = floor([seg(0.0, 1.0, "앞줄"), seg(30.0, 30.1, "가"*24)])
    assert out[1].end_sec == pytest.approx(32.0, abs=1e-6)  # 24자/12 = 2.0초


# ── 하한이 걸리면 안 되는 것 ────────────────────────────────────────────────

def test_짧은_감탄사는_손대지_않는다():
    """'박수!'(0.52초·3자)는 정상이다. 글자수 기준 하한이면 자연히 통과한다
    (3자면 0.25초면 족하고 절대 하한 0.4 도 밑돈다). 절대 하한을 크게 잡아
    감탄사까지 늘이면 장면이 지나간 뒤에도 자막이 남는다."""
    before = [seg(3.0, 3.52, "박수!"), seg(9.0, 10.0, "다음")]
    assert floor(before) == before


def test_이미_겹친_쌍은_손대지_않는다():
    """겹침은 고칠 대상이 아니라 있는 그대로의 데이터다 — merge 가 그렇게 만들고
    ASS 도 그대로 그린다(edit_overrides.py:236~ 의 실측 기록). 늘리는 것은
    **빈 구간**으로만 제한한다."""
    before = [seg(0.2, 3.67, "가"*60), seg(1.7, 5.3, "나"*20)]
    assert floor(before) == before   # 0번은 하한 미달이지만 이미 겹쳐 있다


def test_빈_구간까지만_늘리고_새_겹침을_만들지_않는다():
    out = floor([seg(0.0, 0.5, "가"*30), seg(1.2, 4.0, "나"*10)])
    assert out[0].end_sec == pytest.approx(1.2, abs=1e-6)   # 30자면 2.5초 필요하지만 여기까지
    assert out[0].end_sec <= out[1].start_sec + 1e-9        # 겹침 0 유지


def test_start_sec_는_불가침이고_end_는_줄지_않는다():
    """start 를 당기면 소리보다 자막이 먼저 뜬다 — 늘리는 건 end 뿐이다."""
    before = [seg(1.0, 1.1, "가"*40), seg(9.0, 9.2, "나"*40)]
    for b, a in zip(before, floor(before)):
        assert a.start_sec == b.start_sec
        assert a.text == b.text
        assert a.end_sec >= b.end_sec


def test_이미_충분히_긴_cue_는_줄어들지_않는다():
    before = [seg(0.0, 20.8, "가"*10)]   # 실측 최장 20.8초 — 상한 6초로 잘리면 안 된다
    assert floor(before) == before


# ── 상한·경계 ──────────────────────────────────────────────────────────────

def test_6초_상한이_하한을_이긴다():
    """max_duration_sec 와 충돌하면 상한이 이긴다(6초 넘게 늘리지 않는다)."""
    out = floor([seg(0.0, 0.5, "가"*120)])   # 120자면 10초가 필요하지만
    assert out[0].end_sec == pytest.approx(6.0, abs=1e-6)


def test_하한을_0_으로_주면_아무_일도_하지_않는다():
    before = [seg(0.0, 0.05, "가"*30), seg(5.0, 5.1, "나")]
    assert floor(before, min_chars_per_sec=0, min_exposure_sec=0) == before


def test_빈_입력():
    assert floor([]) == []


# ── 로그 ───────────────────────────────────────────────────────────────────

def test_늘려도_모자라면_건별로_로그에_남는다(capsys):
    """조용히 포기하면 '고쳤는데 왜 그대로지'가 된다."""
    floor([seg(0.0, 0.2, "가"*40), seg(0.6, 4.0, "나"*10)])
    out = capsys.readouterr().out
    assert "[SubtitleFloor]" in out
    assert "[SubtitleFloor/미달]" in out
    assert "다음 줄 시작에 막힘" in out


def test_겹쳐서_포기한_경우도_로그에_남는다(capsys):
    floor([seg(0.2, 3.67, "가"*60), seg(1.7, 5.3, "나"*20)])
    out = capsys.readouterr().out
    assert "다음 줄과 이미 겹쳐 있어 연장 안 함" in out


def test_건드릴_것이_없으면_조용하다(capsys):
    floor([seg(0.0, 5.0, "가"*10), seg(6.0, 11.0, "나"*10)])
    assert capsys.readouterr().out == ""


# ── merge_subtitle_segments 통합 — 병합 결과 자체는 안 바뀐다 ─────────────────

def _ends_removed(segs):
    """하한이 건드리는 것은 end_sec 뿐이라는 대조용 — 병합 경계(start·text)만 남긴다."""
    return [(s.start_sec, s.text) for s in segs]


def test_하한은_순수_후처리라_병합_경계를_바꾸지_않는다():
    """cue 를 어떻게 묶느냐(= 몇 줄이 되고 각 줄이 어디서 시작하고 무슨 글자냐)는
    종전 그대로여야 한다. E14 는 묶인 뒤 end_sec 만 늘린다."""
    segs = [
        seg(0.0, 0.4, "짧은 줄"), seg(0.5, 0.9, "이어지는 말"),
        seg(3.0, 3.07, "수도."), seg(4.09, 5.0, "그리고 다음 문장입니다"),
        seg(6.0, 6.72, "활짝 예쁜 꽃이 피었습니다어라"),
    ]
    with_floor = merge_subtitle_segments(segs, max_total_chars=40)
    without = merge_subtitle_segments(
        segs, max_total_chars=40, min_chars_per_sec=0, min_exposure_sec=0)
    assert len(with_floor) == len(without)
    assert _ends_removed(with_floor) == _ends_removed(without)
    # end 는 늘어난 쪽이 크거나 같다
    assert all(a.end_sec >= b.end_sec for a, b in zip(with_floor, without))
    assert any(a.end_sec > b.end_sec for a, b in zip(with_floor, without))


def test_merge_기본값으로도_하한이_걸린다():
    """두 호출부(pipeline 3466·4106) 모두 min_chars_per_sec 을 넘기지 않으므로
    기본값으로 동작해야 한다."""
    out = merge_subtitle_segments([seg(10.0, 10.07, "수도."), seg(30.0, 31.0, "한참 뒤")])
    assert out[0].end_sec == pytest.approx(10.4, abs=1e-6)


def test_max_total_chars_가_달라도_하한_계산은_묶인_결과를_따른다():
    """3466 은 40, 4106 은 config 계산값(기본 30)이라 cue 길이가 다르다.
    하한은 '묶인 뒤의 글자수 / 12'라 두 경로가 각자 일관된다 — 40 쪽이 더 긴 cue 를
    만들고 그만큼 더 긴 노출을 요구한다."""
    segs = [seg(0.0, 0.2, word(20)), seg(0.3, 0.5, word(20, 100)), seg(50.0, 51.0, "끝")]
    wide = merge_subtitle_segments(segs, max_total_chars=40)
    narrow = merge_subtitle_segments(segs, max_total_chars=15)
    assert len(wide[0].text) > len(narrow[0].text)
    assert (wide[0].end_sec - wide[0].start_sec) > (narrow[0].end_sec - narrow[0].start_sec)


# ── 실렌더 실측 고정 ────────────────────────────────────────────────────────

def test_실측_겹침_비율이_유지된다():
    """실렌더 45편 639줄 대조에서 겹치는 cue 쌍 수는 수정 전후 동일했다(증가 0).
    아래는 그중 가왕쇼_d797f721 의 겹침 구간을 그대로 옮긴 것 — 하한이 겹침을
    한 쌍도 새로 만들지 않아야 한다."""
    real = [
        seg(44.280, 46.765, "가"*30), seg(44.510, 46.765, "나"*34),
        seg(46.765, 48.110, "다"*25), seg(46.765, 48.110, "라"*25),
        seg(48.830, 51.390, "마"*32), seg(50.530, 51.910, "바"*22),
        seg(51.390, 52.040, "사"*28), seg(52.040, 55.080, "아"*46),
    ]

    def n_overlaps(segs):
        return sum(1 for i in range(len(segs) - 1)
                   if segs[i].end_sec > segs[i + 1].start_sec + 1e-9)

    assert n_overlaps(floor(real)) == n_overlaps(real)
