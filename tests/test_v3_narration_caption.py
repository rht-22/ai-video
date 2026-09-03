"""내레이션 자막 문구(2026-09-03, 사용자 지시) — 쉼표·온점은 화면에 안 그린다. 말투·합성·story 는 불변."""
from app.v3.finalize import narration_caption


def test_strips_commas_and_final_period_only():
    assert narration_caption("남편의 외도를 의심해,") == "남편의 외도를 의심해"
    assert narration_caption("하지만 이건 거대한 파국의 시작이었죠.") == "하지만 이건 거대한 파국의 시작이었죠"
    assert narration_caption("딴청 피우는, 남편에게 차갑게 묻는데,") == "딴청 피우는 남편에게 차갑게 묻는데"
    # 물음표·느낌표·말줄임표는 억양 정보라 남긴다
    assert narration_caption("정말 그랬을까?") == "정말 그랬을까?"
    assert narration_caption("설마…") == "설마…"
    assert narration_caption("") == ""


def test_tts_ass_uses_caption_text():
    import pathlib
    src = pathlib.Path("app/v3/finalize.py").read_text(encoding="utf-8")
    assert 'text=narration_caption(str(f["cue"]["text"]))' in src
