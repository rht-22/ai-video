"""잔망루피 작품의 현지화 설정 (P7, 2026-08-26).

롱폼 왕복 실측에서 체인이 **마지막 단계에서** 죽었다:

    RuntimeError: locales.json 에 작품 '잔망루피 유튜브 숏폼' 의 'ja' 항목이 없다

acquire·generate·upload·ingest·evaluate 를 다 지나고(그중 generate 는 3분·유료 호출)
현지화에서 설정 부재로 멈춘 것이다 — 비싼 단계 뒤의 설정 검사다.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

WORK = "잔망루피 유튜브 숏폼"
LOCALES = pathlib.Path("app/localize/data/locales.json")


def _ja():
    return json.loads(LOCALES.read_text(encoding="utf-8"))["works"][WORK]["ja"]


def test_the_work_is_registered():
    """채널 정본(channels_mirror.works[0])과 **글자 그대로** 같아야 찾는다."""
    cfg = json.loads(LOCALES.read_text(encoding="utf-8"))
    assert WORK in cfg["works"]
    assert "ja" in cfg["works"][WORK]


def test_required_fields_are_present():
    ja = _ja()
    for k in ("display", "context", "glossary", "notice_lines", "hashtags_base"):
        assert ja.get(k), k


def test_glossary_matches_the_overlay_source_of_truth():
    """🛑 두 계층이 같은 말을 다르게 쓰면 편마다 표기가 흔들린다.

    overlay(잔망루피 쇼츠)와 rerender(롱폼 생성본)는 같은 채널로 나간다."""
    import yaml
    terms = yaml.safe_load(
        pathlib.Path("app/localize/overlay/data/glossary.yaml").read_text(encoding="utf-8"))["terms"]
    mine = _ja()["glossary"]
    for ko, ja in terms.items():
        assert mine.get(ko) == ja, f"{ko}: overlay={ja!r} locales={mine.get(ko)!r}"


def test_copyright_line_matches_the_metadata_module():
    """설명란 © 줄이 메타 생성기와 갈리면 편마다 다른 표기가 나간다."""
    from app.localize.overlay.meta import DEFAULT_COPYRIGHT
    assert DEFAULT_COPYRIGHT in _ja()["notice_lines"]


def test_character_ending_is_not_decided_here():
    """⚠ '~뤂' 대응 어미는 persona.md §2 에서 [채택 대기]다 — 여기서 정하지 않는다."""
    note = _ja().get("_glossary_note", "")
    assert "채택 대기" in note


def test_shotcone_entry_is_untouched():
    """회귀 0 — 기존 작품 설정은 한 글자도 안 바뀐다."""
    cfg = json.loads(LOCALES.read_text(encoding="utf-8"))
    ja = cfg["works"]["혜미리예채파"]["ja"]
    assert ja["display"] == "ヘミリイェチェパ"
    assert ja["glossary"]["혜리"] == "ヘリ"
