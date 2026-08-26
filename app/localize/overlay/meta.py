"""일본어 메타데이터 생성 — 제목/설명/태그/해시태그 트랜스크리에이션 (L-P5-발행).

vlp `src/metadata.py` 이식. 그 레포에서는 **autopilot 이** 이 모듈을 불렀는데,
autopilot 은 이식하지 않았다(사용자 결정 2 — 선별·승인은 사람·오케스트레이터 몫).
그래서 부르는 자리를 `runner.run_overlay` 안으로 옮겼다: 현지화가 끝나면 그 편의
일본어 메타 초벌이 산출 디렉토리에 함께 남고, 오케스트레이터가 검수 카드에 실어
**사람이 제목·설명을 보고 승인**한다.

⚠ 초벌이다. `_warning` 이 그것을 파일 안에 박아 둔다 — 이 파일만 보고 올리면 안 된다.

이식하며 바꾼 것 둘:
  ① 진입점(`_parse_args`/`main`)은 안 옮겼다 — 이 레포의 진입점은 `app.cli` 하나다
     (rerender 가 세운 규약 · overlay 이식과 같다).
  ② 모델은 `overlay/llm.resolve_model` 이 정한다 — 이 레포의 모델 규칙(Flash/Pro)이
     config 값을 이긴다. vlp config 의 금지 모델이 그대로 살아나지 않게 하는 장치다.
그 외 프롬프트·필드·경고 문구·© 라인은 vlp 와 같다(회귀 0 대조 대상).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import (get_logger, load_glossary, load_persona,
                                         resolve_path, write_json)

log = get_logger("meta")

WARNING = "초벌·네이티브 검수 전 — 게시 금지. 제목/표기/해시태그 사람 확인 필수."
DEFAULT_COPYRIGHT = "© ICONIX / OCON / EBS / SKbroadband"


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def build_prompt(source_title: str, source_desc: str, glossary: dict[str, str]) -> str:
    gl = ("\n固定表記: " + ", ".join(f"{k}→{v}" for k, v in glossary.items())) if glossary else ""
    return (
        "次の韓国語動画メタデータを、ペルソナ規則に従い日本チャンネル用に"
        "トランスクリエーションせよ。\n"
        f"原題: {source_title}\n原説明: {source_desc or '(なし)'}{gl}\n\n"
        "出力は JSON オブジェクトのみ:\n"
        '{"title_candidates": [3案], "title_candidates_ko": [同じ3案の韓国語訳], '
        '"description": "本文+改行+ハッシュタグ", "description_ko": "説明の韓国語訳", '
        '"hashtags": [#タグ...], "tags": [検索タグ15〜20]}\n'
        "制約: タイトルは惹き+キャラ性, 説明は共感トーン, "
        "ハッシュタグに #残念ルーピー #ルーピー を含める。"
        "_ko フィールドは韓国人レビュアーの対訳用 — 直訳で簡潔に。"
        # ⚠ vlp 원본에 없는 한 줄이다(의도된 차이). 실측 2026-08-26: 원제의 한국어
        # 해시태그(`#닛몰캐쉬`)가 제목·설명 양쪽에 그대로 남아 일본 채널에 나갈 뻔했다.
        # LLM 출력이라 이 지시가 **보장은 아니다** — 오케스트레이터가 승인·발행 두
        # 지점에서 다시 막는다. 여기서는 애초에 덜 나오게만 한다.
        "厳守: title/description/hashtags/tags に**韓国語(ハングル)を残さない** — "
        "原題のハッシュタグも日本語かローマ字に置き換えるか削除する。"
        "(_ko フィールドは例外 — レビュー用の対訳なので韓国語のままでよい)"
    )


def parse_llm_object(content: str) -> dict[str, Any]:
    """LLM 응답에서 JSON 오브젝트 추출(코드펜스/잡텍스트 허용)."""
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end != -1 and end > start:
        content = content[start:end + 1]
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("LLM 응답이 JSON 오브젝트가 아님")
    return data


def assemble_draft(video_id: str, llm: dict[str, Any], copyright_line: str) -> dict[str, Any]:
    """LLM 결과 → 저장용 드래프트(경고/©/필드 정규화)."""
    desc = (llm.get("description") or "").rstrip()
    if copyright_line and copyright_line not in desc:
        desc = f"{desc}\n\n{copyright_line}"
    return {
        "_warning": WARNING,
        "video_id": video_id,
        "title_candidates": (llm.get("title_candidates") or [])[:3],
        "title_candidates_ko": (llm.get("title_candidates_ko") or [])[:3],   # 검수 한글 대역
        "description": desc,
        "description_ko": (llm.get("description_ko") or "").rstrip(),
        "hashtags": llm.get("hashtags") or [],
        "tags": (llm.get("tags") or [])[:20],
        "copyright": copyright_line,
    }


def publishable(draft: dict[str, Any] | None) -> bool:
    """이 초벌로 발행 잡을 세울 수 있는가 — 제목 후보와 설명이 있어야 한다. 순수.

    빈 초벌로 올리면 원제(한국어)나 video_id 가 일본 채널 제목이 된다."""
    if not isinstance(draft, dict):
        return False
    cands = [str(t).strip() for t in (draft.get("title_candidates") or []) if str(t).strip()]
    return bool(cands) and bool(str(draft.get("description") or "").strip())


# ── 생성 ──────────────────────────────────────────────────────────────────
def generate(video_id: str, source_title: str, source_desc: str, config: dict[str, Any],
             hero: bool = False, out_path: Optional[str] = None) -> Path:
    persona = load_persona(config)
    glossary = load_glossary(config)
    tcfg = config.get("translate", {})
    from app.localize.overlay import llm
    model = llm.resolve_model(config, hero=hero)

    log.info("메타데이터 생성 provider=%s model=%s video_id=%s",
             llm.provider(config), model, video_id)
    raw = llm.complete(
        "あなたはYouTube日本市場のメタデータ最適化担当。\n" + persona.strip(),
        build_prompt(source_title, source_desc, glossary),
        config, model=model, max_tokens=int(tcfg.get("max_tokens", 1024)), hero=hero)
    draft = assemble_draft(video_id, parse_llm_object(raw), DEFAULT_COPYRIGHT)

    out = Path(out_path) if out_path else resolve_path(
        f"{config['paths']['outputs_dir']}/{video_id}/metadata_draft.json")
    write_json(draft, out)
    log.info("메타데이터 초벌 저장(검수 전): %s", out)
    return out
