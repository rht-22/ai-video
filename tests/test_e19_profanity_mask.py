"""E19-7 자막 욕설 마스킹 회귀 가드.

발주서: docs/prompts/e19-drama-clip-preset.md §7. 벤치마크: 음성은 원음(「새끼야」),
자막만 「XX끼야」 — 제목의 "X같은" 과 같은 플랫폼 노출 안전 규율. 계약 요점:

- 사전은 코드가 아니라 `app/data/profanity_mask_ko.json` 한 곳(E13 규율). 규칙은
  **공백 토큰(열) 완전 일치**뿐 — 부분 문자열 치환 금지(멀쩡한 단어를 깨뜨린다).
- 적용은 자막 텍스트에만 — 전사 원문·TTS 합성 입력은 그대로(소리는 원음 유지).
- 게이트: design 키 `subtitle_profanity_mask`(기본 "off" = 종전 그대로, 회귀 0).
- 편집실 자막 오버라이드보다 **앞** — 사람이 고친 문장은 마스킹이 덮지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.cli import _build_design_config, build_parser
from app.config import DesignConfig
from app.modules.profanity_mask import (
    apply_mask_to_segments,
    load_mask_rules,
    mask_profanity_text,
)

REPO = Path(__file__).resolve().parents[1]


def _design(*extra_args: str) -> DesignConfig:
    p = build_parser()
    args = p.parse_args(["create_shorts", "--title", "T", "--video", "x.mp4",
                         "--subtitle", "x.srt", *extra_args])
    return _build_design_config(args)


# ══════════════════════════════════════════════════════════════════════════
# 사전 — 데이터 파일 한 곳
# ══════════════════════════════════════════════════════════════════════════
def test_dictionary_file_exists_and_loads():
    data = json.loads((REPO / "app" / "data" / "profanity_mask_ko.json")
                      .read_text("utf-8"))
    assert data.get("token_map")
    rules = load_mask_rules()
    assert rules["token_map"]
    # 벤치마크의 그 형태가 사전에 있다 — 「새끼가」 → 「XX끼가」
    assert rules["token_map"].get("새끼가") == "XX끼가"


# ══════════════════════════════════════════════════════════════════════════
# 마스킹 — 완전 토큰 일치, 부분 문자열 금지
# ══════════════════════════════════════════════════════════════════════════
def test_masks_exact_token():
    out, changes = mask_profanity_text("아 이 새끼가 장난하나")
    assert out == "아 이 XX끼가 장난하나"
    assert changes == {"새끼가→XX끼가": 1}


def test_no_partial_substring_masking():
    """토큰 완전 일치만 — '새끼줄'(밧줄)·'말새끼들처럼' 같은 비일치 토큰은 그대로다."""
    for text in ("새끼줄 꼬는 법", "동물 새끼들처럼요"):
        out, changes = mask_profanity_text(text)
        assert out == text and changes == {}


def test_clean_text_unchanged():
    out, changes = mask_profanity_text("여기서 제일 소개시켜 주기 싫은 사람")
    assert out == "여기서 제일 소개시켜 주기 싫은 사람" and changes == {}


def test_segments_wrapper_masks_in_place_and_reports():
    segs = [SimpleNamespace(start_sec=0.0, end_sec=1.0, text="아 이 새끼가"),
            SimpleNamespace(start_sec=1.0, end_sec=2.0, text="괜찮은 대사")]
    n, details = apply_mask_to_segments(segs)
    assert n == 1
    assert segs[0].text == "아 이 XX끼가"
    assert segs[1].text == "괜찮은 대사"
    assert details and "새끼가" in details[0]


def test_masking_is_idempotent():
    """마스킹된 텍스트를 다시 태워도 불변 — 캐시 재개 경로가 두 번 지나도 안전하다."""
    once, _ = mask_profanity_text("아 이 새끼가")
    twice, changes = mask_profanity_text(once)
    assert twice == once and changes == {}


# ══════════════════════════════════════════════════════════════════════════
# 게이트 — design 키, 기본 off
# ══════════════════════════════════════════════════════════════════════════
def test_design_key_default_off_and_cli():
    assert DesignConfig().subtitle_profanity_mask == "off"
    assert _design().subtitle_profanity_mask == "off"
    d = _design("--design-subtitle-profanity-mask", "on")
    assert d.subtitle_profanity_mask == "on"


def test_pipeline_wiring():
    src = (REPO / "app" / "pipeline.py").read_text("utf-8")
    gate = src.index('subtitle_profanity_mask", "off") == "on"')
    # 자리: 자막 캐시 수렴 뒤 · 편집실 자막 오버라이드 **앞**(사람 문장은 안 덮는다)
    assert src.index("자막 캐시 로드 완료") < gate < src.index("_sub_override = overrides_subtitles")
    assert '"step": "subtitle_profanity_mask"' in src
    # TTS cue 텍스트에는 손대지 않는다 — 마스킹 적용 대상은 final_segments 뿐
    seg = src[gate:gate + 1200]
    assert "final_segments" in seg and "tts_cues" not in seg
