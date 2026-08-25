"""검수 반려 '수정 재렌더' — 운영자가 고친 텍스트를 번역본에 병합한다.

원본: `localize_run.apply_overrides`.

좌표계는 검수 카드 `ko_ja_pairs` 의 idx(= translation 각 목록의 index 필드).
편집실이 보낸 값이 LLM 산 번역을 이긴다 — 사람이 최종 결정권자다.
"""
from __future__ import annotations

import copy

from app.localize.styles import validate_line_style, validate_line_timing
from app.modules.edit_overrides import SUBTITLE_MAX_LINES

# 통째 교체하는 문자열 필드
SCALAR_FIELDS = ("youtube_title_ja", "youtube_title_ko",
                 "description_ja", "description_ko", "top_title_ja")
# 오버라이드 목록 이름 → translation 안의 목록 이름
LIST_MAP = (("subs", "segments"), ("tts", "tts_cues"), ("telops", "telops"))
# tts 에 아직 못 받는 키 — 조용히 무시하지 않고 즉시 거절한다
TTS_UNSUPPORTED = {"style", "start_sec", "end_sec", "use"}
# 줄 수 상한(F-412)이 걸리는 목록 — 대사 자막·TTS 자막은 렌더(_lay_out_for_ass)가
# 상한을 넘는 줄을 **조용히 잘라내므로** 여기서 거절해야 한다(사람 값 증발 방지 —
# KR 계약 edit_overrides._validate_manual_lines 와 같은 이유·같은 상한).
# 텔롭은 제외: build_telop_ass 는 \N 을 그대로 그리고 잘라내지 않는다.
LINE_CAPPED = {"segments", "tts_cues"}


def _validate_ja_lines(src: str, key, ja: str) -> None:
    """일본어 문구의 수동 줄바꿈 수 검증 — 상한 초과는 즉시 실패(F-412)."""
    lines = [ln for ln in str(ja).replace("\r\n", "\n").split("\n") if ln.strip()]
    if len(lines) > SUBTITLE_MAX_LINES:
        raise ValueError(
            f"{src}[{key}]: 줄바꿈이 너무 많습니다 ({len(lines)}줄) — 자막은 최대 "
            f"{SUBTITLE_MAX_LINES}줄입니다 (렌더가 넘는 줄을 조용히 잘라내므로 거절)")


def apply_overrides(translation: dict, ov: dict) -> dict:
    """검수 수정 병합. 원본은 건드리지 않고 사본을 돌려준다. 순수 — 테스트 대상.

    · 문자열 필드: youtube_title_ja/_ko · description_ja/_ko · top_title_ja — 통째 교체.
    · subs{idx}→segments · tts{idx}→tts_cues · telops{idx}→telops: 값이 dict 면 항목에
      update(예: {"ja":"…","use":false}), 문자열이면 ja 만 교체. 없는 idx 는 무시.
    · (8/20) subs·telops 의 dict 값은 style{size,y,color,rotate}·start_sec·end_sec 를
      실을 수 있다 — 타입·범위 위반, 모르는 style 키는 ValueError(fail-loud).
      **tts 의 style·start_sec/end_sec/use 는 후속 범위**(ai-video 계약이 cue 단위 스타일을
      안 받고, 타이밍·삭제는 재합성 창 재계산이 얽힌다) — 즉시 거절한다.
    · (E6-0) subs 의 use=false = 소프트 삭제 — apply 가 렌더에서 빼고, meta 가 다음
      카드에서 뺀다. 불리언 외에는 거절."""
    out = copy.deepcopy(translation)
    for k in SCALAR_FIELDS:
        v = (ov or {}).get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    for src, dst in LIST_MAP:
        edits = (ov or {}).get(src)
        if not isinstance(edits, dict):
            continue
        by_idx = {e.get("index"): e for e in (out.get(dst) or []) if isinstance(e, dict)}
        for key, v in edits.items():
            try:
                item = by_idx.get(int(key))
            except (TypeError, ValueError):
                continue
            if item is None:
                continue
            if isinstance(v, dict):
                v = dict(v)
                if dst in LINE_CAPPED and isinstance(v.get("ja"), str):
                    _validate_ja_lines(src, key, v["ja"])
                if dst == "tts_cues" and (TTS_UNSUPPORTED & set(v)):
                    raise ValueError(
                        f"tts[{key}]: style/start_sec/end_sec/use 오버라이드는 아직 지원하지 "
                        "않습니다(후속 — docs/subtitle-style-overrides.md)")
                if "use" in v and not isinstance(v["use"], bool):
                    raise ValueError(
                        f"{src}[{key}].use 는 불리언(false=그 줄 제외): {v['use']!r}")
                if v.get("style") is not None:
                    v["style"] = validate_line_style(v["style"])   # 위반 = 즉시 실패
                validate_line_timing(v)
                item.update(v)
            elif isinstance(v, str) and v.strip():
                if dst in LINE_CAPPED:
                    _validate_ja_lines(src, key, v)
                item["ja"] = v.strip()
    return out
