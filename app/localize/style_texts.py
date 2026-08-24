"""E16 — 화면에 얹는 글자의 현지화 (효과 텍스트·시간대별 제목·편집실 텍스트).

원본: vlp `localize_run` 의 E16 블록(0757b68). 발주서: ves-orchestrator
`docs/prompts/e16-jp-style-texts.md`.

JP 재렌더는 ai-video 를 `--from-step render` 로 다시 돌린다. 그때 화면에 그려지는
**한국어 글자**가 둘 있다:

  · AI 연출(E15) — `checkpoint_style.json` 의 `texts[]`·`title_segments[]`
  · 편집실 텍스트(F-411) — `edit_overrides.json` 의 `texts[]`
    (`rerender.visual_only_overrides` 가 재렌더로 넘긴다)

둘 다 E16 전에는 번역 없이 그대로 번인됐다. 여기서 일본어로 바꾼다.

**손대는 것은 문구와 폰트뿐이다.** 좌표·크기·색·fx·rotate 는 연출 의도이고 언어와
무관하다. 스티커(`images`)·자막 강조(`subtitle_styles`)도 언어 중립이라 그대로 둔다
(자막 강조는 L3 가 이미 일본어로 바꾼 줄에 얹히므로 지금도 정상 동작한다).

⚠ 이 파일의 함수들은 L1(translate)·L3(apply)·L4(rerender)·L5(meta) 가 **같이** 쓴다.
   vlp 는 한 파일이라 `_load_json_or_none`·`_ja_by_index` 를 비공개로 뒀지만, 여기서는
   모듈 경계를 넘으므로 공개 이름이다(이식본 규약 — `apply.clamp_hallucination` 과 같다).
"""
from __future__ import annotations

import json
from pathlib import Path

STYLE_PLAN_NAME = "checkpoint_style.json"

# L1 payload·응답이 쓰는 화면 글자 목록 이름 — 세 곳(스키마·정렬 검증·프롬프트)이 같은 이름을 본다.
STYLE_TEXT_KEYS = ("style_texts", "style_titles", "editor_texts")


def load_json_or_none(path: Path):
    """있으면 파싱, 없거나 깨졌으면 None. 연출은 부가물이라 여기서 잡을 걸지 않는다."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def style_plan_strings(plan: dict | None) -> tuple[list[str], list[str]]:
    """style_plan/v1 → (효과 텍스트 문구, 시간대별 제목 문구). 순수(테스트 대상)."""
    p = plan or {}
    return ([str(t.get("text", "")) for t in (p.get("texts") or [])],
            [str(sg.get("text", "")) for sg in (p.get("title_segments") or [])])


def editor_text_strings(ov: dict | None) -> list[str]:
    """편집실 오버라이드 → texts 문구. 순수(테스트 대상).

    `visual_only_overrides` 가 재렌더로 넘기는 것과 **같은 배열·같은 순서**여야 한다
    (인덱스가 좌표다 — 어긋나면 다른 문구가 들어간다)."""
    return [str(t.get("text", "")) for t in ((ov or {}).get("texts") or [])]


def ja_by_index(rows: list | None, n: int, what: str) -> list[str]:
    """[{index, ja}] → 인덱스 순 문자열 n개. 개수가 어긋나면 즉시 실패(자막 정렬과 같은 규율)."""
    rows = rows or []
    if len(rows) != n:
        raise RuntimeError(f"{what} 정렬 불일치: ko {n} vs ja {len(rows)}")
    out = [""] * n
    for r in rows:
        i = int(r["index"])
        if not 0 <= i < n:
            raise RuntimeError(f"{what} 인덱스 범위 밖: {i} (0~{n-1})")
        out[i] = str(r["ja"])
    return out


def apply_style_translation(plan: dict, tr: dict, font: str | None = None) -> dict:
    """번역을 style_plan 에 적용한 **새 dict**. 순수(테스트 대상).

    font: 번들 폰트 4종은 전부 한글 전용이라(mulmaru 만 가나가 있고 한자는 넷 다 없다 —
    2026-08-24 fontTools 실측) 일본어가 두부(□)로 나간다. 현지화 폰트로 바꾼다.
    ai-video 는 재렌더에서 이 플랜을 재검증하지 않고 그대로 태우지만, 편집실 텍스트 쪽은
    화이트리스트를 타므로 `edit_overrides.TEXT_FONTS` 에 같은 이름이 있어야 한다
    (짝 변경 c80da45 — ArialUnicode).
    """
    out = dict(plan or {})
    texts_ko, titles_ko = style_plan_strings(out)
    if texts_ko:
        ja = ja_by_index(tr.get("style_texts"), len(texts_ko), "style_texts")
        out["texts"] = [{**t, "text": ja[i], **({"font": font} if font else {})}
                        for i, t in enumerate(out["texts"])]
    if titles_ko:
        ja = ja_by_index(tr.get("style_titles"), len(titles_ko), "style_titles")
        out["title_segments"] = [{**sg, "text": ja[i]}
                                 for i, sg in enumerate(out["title_segments"])]
    return out


def apply_editor_text_translation(visual: dict, tr: dict, font: str | None = None) -> dict:
    """편집실 visual 오버라이드의 texts 를 일본어로. 순수(테스트 대상)."""
    out = dict(visual or {})
    ko = editor_text_strings(out)
    if ko:
        ja = ja_by_index(tr.get("editor_texts"), len(ko), "editor_texts")
        out["texts"] = [{**t, "text": ja[i], **({"font": font} if font else {})}
                        for i, t in enumerate(out["texts"])]
    return out
