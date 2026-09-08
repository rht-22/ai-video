"""ASS Fontname 이 폰트의 실제 패밀리명과 일치하는지 — 번들 폰트 전수 대조.

이 테스트가 있는 이유: 자막 폰트가 안 맞아도 **렌더는 성공하고 글자도 보인다**. libass 가
시스템 기본 폰트로 조용히 대체하기 때문이다. 실제로 그 상태로 오래 돌았고(2026-07-29 발견),
없는 폰트명으로 렌더한 것과 픽셀 단위로 같았다. 눈으로는 못 잡으니 기계가 잡아야 한다.

ffmpeg 없이 PIL 로 폰트 내부 이름을 읽어 대조하므로 빠르다.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import ImageFont

from app.config import FONT_FAMILY_MAP, to_font_family
from app.modules.subtitle import SubtitleStyle, _ass_header
from app.modules.subtitle_styles import SUBTITLE_PRESETS

FONTS_DIR = Path(__file__).resolve().parent.parent / "app" / "assets" / "fonts"
BUNDLED = sorted([*FONTS_DIR.glob("*.ttf"), *FONTS_DIR.glob("*.otf")])


def _family_of(path: Path) -> str:
    return ImageFont.truetype(str(path), 20).getname()[0]


def _ass_names_of(path: Path) -> set[str]:
    """libass 가 매칭하는 이름 후보 — PIL 은 typographic family(nameID 16)를 돌려주지만 libass 는
    nameID 1(레거시 패밀리, 웨이트가 붙을 수 있다)·4 만 본다. 둘 다 허용(Noto Sans CJK KR Black)."""
    fam, style = ImageFont.truetype(str(path), 20).getname()
    return {fam, f"{fam} {style}"} if style and style.lower() != "regular" else {fam}


def test_fonts_are_bundled():
    # 폰트가 하나도 없으면 아래 테스트가 통째로 무의미해진다(vacuous pass 방지)
    assert BUNDLED, f"{FONTS_DIR} 에 번들된 폰트가 없습니다"


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.stem)
def test_file_stem_maps_to_actual_family(path):
    """파일명 stem 을 넣으면 그 폰트의 실제 패밀리명이 나와야 한다."""
    assert to_font_family(path.stem) in _ass_names_of(path), (
        f"{path.name}: 실제 패밀리명은 {_family_of(path)!r} 인데 "
        f"to_font_family({path.stem!r}) 가 {to_font_family(path.stem)!r} 를 돌려줍니다. "
        f"config.FONT_FAMILY_MAP 에 추가하세요.")


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.stem)
def test_actual_family_passes_through(path):
    """이미 올바른 패밀리명을 준 경우 훼손하지 않아야 한다."""
    fam = _family_of(path)
    assert to_font_family(fam) in _ass_names_of(path)


def test_all_presets_resolve_to_a_bundled_family():
    """장르 프리셋이 지정한 폰트가 전부 실제 번들 폰트로 해석되는지.

    프리셋은 파일명 stem 을 쓰고 있어(JalnanGothic 등) 맵을 거치지 않으면 전부 대체 폰트가 된다.
    """
    families = {n for p in BUNDLED for n in _ass_names_of(p)}
    for pid, style in SUBTITLE_PRESETS.items():
        got = to_font_family(style.font_name)
        assert got in families, f"프리셋 {pid}: {style.font_name!r} → {got!r} 가 번들 폰트에 없습니다"


def test_ass_header_writes_family_not_filename():
    """회귀 방지 — _ass_header 가 파일명 맵을 다시 쓰면 여기서 걸린다."""
    header = _ass_header(SubtitleStyle(font_name="JalnanGothic"),
                         SubtitleStyle(font_name="여기어때 잘난체 2 TTF"))
    assert "Jalnan Gothic TTF" in header
    assert "Jalnan 2 TTF" in header
    # 파일명 stem 이 그대로 박히면 libass 가 못 찾는다
    assert ",JalnanGothic," not in header


def test_unknown_font_passes_through():
    # 시스템 폰트를 지정하는 경우가 있어 모르는 이름은 통과시켜야 한다
    assert to_font_family("Malgun Gothic") == "Malgun Gothic"


def test_map_has_no_self_referential_filename_entries():
    """맵의 값이 실제 패밀리명인지 — 파일명 stem 을 값으로 잘못 넣는 실수 방지."""
    stems = {p.stem for p in BUNDLED}
    families = {n for p in BUNDLED for n in _ass_names_of(p)}
    for key, value in FONT_FAMILY_MAP.items():
        assert value not in stems or value in families, (
            f"FONT_FAMILY_MAP[{key!r}] = {value!r} 는 파일명 stem 으로 보입니다 — 패밀리명이어야 합니다")
