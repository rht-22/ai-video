"""일본어 TTS 읽기 보정 — 표기와 발음이 갈리는 지점을 합성 입력에서만 고친다.

캐릭터 어미 「〜ルプ」(persona.md §2, 2026-08-27 확정): 표기는 「ルプ」 그대로 두고
**발음만** 한국어 "뤂"처럼 받침으로 닫는다. 일본어 표기로는 종성 /p/ 가 불가능하므로
문말 「ルプ」 뒤에 촉음(ッ)을 붙여 모음을 끊는 것이 TTS 로 낼 수 있는 가장 가까운
소리다("ル・プ" 2음절 방지).

⚠ 이 치환은 **합성 직전에만** 건다 — 자막·SRT·ja_events·검수 카드에는 보이지 않는다.
   (overlay dub `synthesize_segment` · rerender `narration.l3t_tts` 두 곳이 부른다)
"""
from __future__ import annotations

import re

# 문말 판정: 「ルプ」 뒤에 닫는 따옴표·괄호나 문장 종결 부호만 남을 때.
# 이미 「ルプッ」 이면 뒤가 ッ 라 문말 판정에 안 걸린다(이중 치환 없음).
_SENT_END = re.compile(r"ルプ(?=[」』）)\]]*(?:[。．、！？!?…♪〜~]|\s|$))")


def clip_character_ending(text: str) -> str:
    """문말 「ルプ」 → 「ルプッ」 (합성 입력 전용). 문중 「ルプ」·다른 텍스트는 그대로."""
    return _SENT_END.sub("ルプッ", text or "")
