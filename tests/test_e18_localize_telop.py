"""E18 — JP 텔롭이 원본 한국어 텔롭 위에 얹히지 않는다 (2026-08-24).

사용자 지시: "한글 자막과 일본 자막이 겹침".

실측(SHOTCONE 혜미리예채파 2화 완성본): 원본에 박힌 `냉장고도, 휴지도, 심지어 조명도
없는 이 집에서` 위에 일본어 `冷蔵庫も、紙も、照明すらない この家で` 가 그대로 얹혀
**둘 다 못 읽는** 화면이 나갔다.

기전은 둘이다.

  ① 세로 자리가 **고정**이었다 — `Style: Telop … MarginV 720` 하나로 모든 텔롭을
     그린다. y = 1920−720 = 1200 은 13:9 밴드(586~1334)의 82% 지점이라 하단 방송
     텔롭과 정면으로 부딪친다. L2 가 텔롭마다 `position`(top/middle/bottom)을 이미
     뽑아 두는데 이 함수가 **안 쓰고 있었다**.
  ② 박스로 덮는 길은 **없다** — 실렌더로 확인했다(아래 ② 절). 원본 텔롭은 840px 인데
     JP 한 줄 박스는 202px 다. 그래서 ①이 유일한 수단이다.

⚠ E17-2 의 번인 자막 회피는 이 트랙에 안 걸린다. 그건 우리 대사·TTS 자막용이고,
  판정도 '편 내내 같은 자리' 규칙이라 몇 초씩만 뜨는 방송 텔롭을 못 잡는다(별건).
  실제로 이 편의 run_log 에는 `subtitle_avoid_burned` 단계가 아예 없다(= 띠 미검출).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.localize import apply as loc_apply  # noqa: E402
from app.localize import telop as loc_telop  # noqa: E402


def _dialogue(tmp_path, telops, translation, name="t.ass"):
    out = tmp_path / name
    loc_apply.build_telop_ass(telops, translation, "ArialUnicode", out)
    return [ln for ln in out.read_text(encoding="utf-8").splitlines()
            if ln.startswith("Dialogue:")]


def _tr(idx=0, **kw):
    return {"telops": [{"index": idx, "use": True, "ja": "日本語", **kw}]}


# ── ① 세로 자리 ────────────────────────────────────────────────────────────
def test_a_bottom_telop_pushes_the_japanese_line_up():
    """이번 사고의 조건 — 원본이 아래에 있으면 일본어는 위로 피한다."""
    t = {"orig_index": 0, "start_sec": 1.0, "end_sec": 2.0, "position": "bottom"}
    assert loc_apply.telop_margin_v(t, {}) == loc_apply.TELOP_MARGIN_V_HIGH


def test_a_middle_telop_also_moves_up():
    """기본 자리(720 → y 1200)는 밴드 아래쪽이라 중앙 텔롭과도 가깝다 — 더 먼 쪽으로 간다."""
    assert loc_apply.telop_margin_v({"position": "middle"}, {}) == loc_apply.TELOP_MARGIN_V_HIGH


def test_a_top_telop_keeps_the_default_slot():
    """원본이 위에 있으면 아래가 비어 있다 — 종전 자리 그대로(이벤트 MarginV 0 = 스타일 기본)."""
    assert loc_apply.telop_margin_v({"position": "top"}, {}) == 0


def test_an_unknown_position_changes_nothing():
    """회귀 최소화 — position 이 없거나 모르는 값이면 종전과 완전히 같다."""
    for t in ({}, {"position": None}, {"position": ""}, {"position": "어딘가"}):
        assert loc_apply.telop_margin_v(t, {}) == 0


def test_a_human_override_still_wins():
    """편집실이 y 를 정했으면 그게 이긴다 — 사람 값이 회피 규칙에 먹히면 안 된다."""
    got = loc_apply.telop_margin_v({"position": "bottom"}, {"y": 0.5})
    assert got == loc_apply.style_margin_v({"y": 0.5}, 1920) == 960


def test_the_avoidance_reaches_the_rendered_line(tmp_path):
    """상수만 맞고 파일에 안 실리면 화면은 그대로다."""
    telops = [{"orig_index": 0, "start_sec": 1.0, "end_sec": 2.0, "position": "bottom"}]
    line = _dialogue(tmp_path, telops, _tr())[0]
    assert f",0,0,{loc_apply.TELOP_MARGIN_V_HIGH},," in line


def test_a_default_slot_line_is_byte_identical_to_before(tmp_path):
    """회피가 필요 없는 줄은 이식 원본과 한 글자도 같아야 한다(vlp 대조 대상)."""
    telops = [{"orig_index": 0, "start_sec": 1.0, "end_sec": 2.0}]
    assert _dialogue(tmp_path, telops, _tr()) == [
        "Dialogue: 0,0:00:01.00,0:00:02.00,Telop,,0,0,0,, 日本語"]


def test_the_high_slot_clears_the_title_block():
    """제목을 피하려다 제목 위로 올라가면 문제를 옮기기만 한 것이다.

    13:9·꽉 찬 폭·세로 중앙이면 제목 블록 아래끝은 overlay_y − 20 = 566px.
    HIGH 자리의 글자 아래끝은 1920 − 1120 = 800px, 두 줄 박스(≈150px)를 세워도
    위끝이 650px 근처라 제목 아래에 남는다."""
    y_bottom = 1920 - loc_apply.TELOP_MARGIN_V_HIGH
    assert y_bottom - 150 > 566
    assert y_bottom < 960                      # 밴드 중앙보다 위 = 중앙 텔롭과도 떨어진다


# ── ② 박스로는 못 덮는다 (실측으로 닫힌 선택지) ────────────────────────────
def test_the_box_colour_is_left_alone_because_it_cannot_mask(tmp_path):
    """"불투명 박스로 원본을 덮자"는 실렌더로 **기각됐다**(2026-08-24, ffmpeg 6.1.1).

    · BackColour 를 반투명(&H78......) → 불투명(&H00......)으로 바꿔도 프레임이
      **픽셀까지 동일**했다 — BorderStyle=3 의 박스는 OutlineColour(&H00000000,
      이미 불투명)로 그려지고 BackColour 는 그림자 색인데 Shadow=0 이다.
    · 여백(Outline)을 5 → 12 → 24 로 넓혀도 박스 폭 202 → 216 → 242px 인데 원본
      텔롭은 840px 였다. 여백은 마스크가 아니다.
    ⇒ 겹침을 없애는 수단은 자리 이동뿐이다. 이 테스트는 **되돌리지 말라는 표지**다."""
    out = tmp_path / "t.ass"
    loc_apply.build_telop_ass([], {"telops": []}, "ArialUnicode", out)
    style = next(ln for ln in out.read_text(encoding="utf-8").splitlines()
                 if ln.startswith("Style: Telop,"))
    assert style.split(",")[4] == "&H00000000"   # OutlineColour = 박스 색, 이미 불투명
    assert style.split(",")[5] == "&H78000000"   # BackColour = 그림자 색, Shadow=0 이라 무의미
    assert style.split(",")[9] == "5"            # Outline = 박스 여백(마스크가 아니다)
    assert style.split(",")[10] == "0"           # Shadow = 0 → BackColour 는 쓰이지 않는다


# ── 우리 연출 텍스트를 방송 텔롭으로 다시 뽑지 않는다 (5번) ─────────────────
def test_extraction_has_a_kind_for_our_own_style_text():
    """L2 분류에 E15 연출 텍스트 칸이 없어서 우리 글자가 broadcast_telop 으로 잡혔다.

    실측: E15 효과 텍스트 `멘붕?!`(원본 42.0s)이 검수 카드의 텔롭 목록에
    `idx5 ko="멘붕?!"`(편집본 9.75~11.25s ≒ 원본 41.25~42.75s)로 다시 들어와
    JP 텔롭 트랙이 같은 말을 한 번 더 그렸다."""
    kinds = loc_telop.SCHEMA_EXTRACT["items"]["properties"]["kind"]["enum"]
    assert "our_style_text" in kinds
    assert "our_style_text" in loc_telop.EXTRACT_PROMPT
    assert "멘붕?!" in loc_telop.EXTRACT_PROMPT or "쿵!" in loc_telop.EXTRACT_PROMPT


def test_our_style_text_is_not_a_broadcast_telop():
    """분류가 붙으면 공통 인덱스 목록에서 자동으로 빠진다 — 필터는 그대로 쓴다."""
    data = [{"text_ko": "냉장고도", "kind": "broadcast_telop"},
            {"text_ko": "멘붕?!", "kind": "our_style_text"},
            {"text_ko": "이 집이야?", "kind": "our_subtitle"}]
    assert [t["text_ko"] for t in loc_telop.only_broadcast_telops(data)] == ["냉장고도"]
