"""합본 소스 — 선공개 영상은 항상 맨 앞 + '원본 시간 순서와 다를 수 있다' 명시(2026-09-21 사용자 지시)."""
from scripts import concat_source_clips as c
from app.tikitaka.guide import parse_guide_text


def _m(vid, title, **kw):
    return {"id": vid, "title": title, "episode": 1, **kw}


def test_prerelease_detected_by_title_or_forced_id():
    pre = _m("p", "[로또] 재미난 거 보여줄게 | 1-2화 선공개 | TVING")
    clip = _m("a", "[로또] 팀장님 | 1화 클립 | TVING")
    assert c.is_prerelease(pre, "선공개", set())
    assert not c.is_prerelease(clip, "선공개", set())
    assert c.is_prerelease(clip, "선공개", {"a"})
    assert not c.is_prerelease(pre, "", set())          # 제목 판정 끔


def test_prerelease_moves_to_front_and_keeps_given_order():
    metas = [_m("a", "1화"), _m("p1", "선공개", prerelease=True), _m("b", "2화"), _m("p2", "선공개2", prerelease=True)]
    assert [m["id"] for m in c.order_prerelease_first(metas)] == ["p1", "p2", "a", "b"]
    plain = [_m("a", "1화"), _m("b", "2화")]
    assert c.order_prerelease_first(plain) == plain     # 선공개 없으면 순서 불변


def test_source_notes_states_prerelease_warning_without_guide_keys():
    manifest = {"work": "로또_1등도_출근합니다", "tag": "1-2화", "clips": [
        {"id": "p", "title": "선공개 영상", "episode": None, "prerelease": True, "concat_start_sec": 0.0, "concat_end_sec": 153.5},
        {"id": "a", "title": "1화 클립", "episode": 1, "concat_start_sec": 153.5, "concat_end_sec": 305.0}]}
    text = c.source_notes(manifest)
    assert c.PRERELEASE_NOTE in text and "00:00.0~02:33.5" in text and "[1화] 1화 클립" in text
    parsed = parse_guide_text(text)                      # 메모가 가이드 구조 키(로고·카피·활용 불가…)로 읽히면 안 된다
    assert not parsed["avoid"] and not parsed["exclude"] and parsed["logo"] is None and parsed["copy"] is None
    no_pre = c.source_notes({**manifest, "clips": manifest["clips"][1:]})
    assert "선공개" not in no_pre
