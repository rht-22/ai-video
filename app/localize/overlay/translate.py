"""[엔진③-a] 번역 + 트랜스크리에이션 (GhostCut 차별 레이어의 절반).

detections.json 의 한국어 텍스트를 일본어로 *트랜스크리에이션*.
- persona.md 를 LLM system 에 주입(캐릭터 톤·어미·표기 규칙).
- glossary.yaml 고정 용어 후처리로 표기 일관성 보장.
- 선택적으로 DeepL 초벌 병용(--deepl) — LLM 이 이를 참고해 다듬음.
- 결과는 **초벌**(draft=True): 네이티브 검수 게이트 전.

LLM/HTTP 는 lazy import. 프롬프트 빌드·glossary 적용·JSON 파싱은 순수 → 테스트 가능.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import (get_logger, get_secret, load_config, load_glossary,
                           load_persona, resolve_path)
from app.localize.overlay.schemas import DetectionDoc, TranslationDoc, TranslationEntry

log = get_logger("translate")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def build_system_prompt(persona: str, glossary: dict[str, str]) -> str:
    parts = [
        "あなたは韓国アニメ『ジャンマンルーピー(잔망루피)』日本版チャンネルの",
        "字幕トランスクリエーター。以下のペルソナ規則に厳密に従う。\n",
        "=== PERSONA ===", persona.strip(), "=== /PERSONA ===\n",
        "原則: 直訳禁止。意味と感情とミームを日本語の文脈で再創作する。",
        "画面字幕は原文より長くしない。キャラ語尾は PERSONA の規則のみ適用",
        "(未確定なら標準語にし notes に記す)。",
    ]
    if glossary:
        gl = "\n".join(f"- {k} → {v}" for k, v in glossary.items())
        parts += ["\n=== 用語集(固定表記・厳守) ===", gl]
    return "\n".join(parts)


def build_user_prompt(texts: list[str], drafts: Optional[dict[str, str]] = None,
                      char_budgets: Optional[list[int]] = None) -> str:
    lines = [
        "次の韓国語テキストを日本語にトランスクリエーションせよ。",
        '出力は JSON 配列のみ。各要素 {"source","target","notes","flagged"}。',
        "flagged は文脈不足・固有名詞不確実など人手確認が要る場合 true。",
    ]
    if char_budgets:   # 더빙용: 슬롯 초수 기반 길이 예산 — 초과하면 말이 빨라진다
        lines.append("これは吹き替え用。各行の日本語は指定の文字数以内で、"
                     "話し言葉として自然に短くまとめよ(内容の要点は保持)。")
        # 긴 가타카나 복합어 나열은 TTS 가 가장 못 읽는 형태(2026-07-09 실측:
        # 'マーラータントッポッキにタッパル…' 발음 붕괴) → 자연 문장 흐름 강제.
        lines.append("料理名・固有名詞: 長いカタカナ複合語(8文字超)や名詞の羅列は避け、"
                     "日本の視聴者に通じる短く自然な言い方に意訳せよ(要素の省略可)。"
                     "名詞を詰め込まず、助詞・動詞のある話し言葉の文にすること。")
    lines.append("")
    for i, t in enumerate(texts):
        d = f"  (DeepL下訳: {drafts[t]})" if drafts and t in drafts else ""
        b = (f"  [≤{char_budgets[i]}文字]"
             if char_budgets and i < len(char_budgets) else "")
        lines.append(f"{i + 1}. {t}{d}{b}")
    return "\n".join(lines)


# 모델이 프롬프트의 번호 매김을 source 에 되돌려주는 경우가 있다 — build_user_prompt 는
# 각 줄을 `1. {텍스트}` 로 적는다. 2026-08-24 mm-06 실측: gemini-3.6-flash 가 source 를
# `'1. L'` 로 돌려줘 18/18 이 안 붙었다(같은 입력으로 붙는 판도 있다 — 비결정적이다).
_SOURCE_INDEX_PREFIX = re.compile(r"^\s*\d+\.\s*")


def _index_by_source(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """응답 행을 source 로 색인. 순수 — 테스트 대상.

    ⚠ **프롬프트는 안 고친다.** 문구를 바꾸면 번역 결과가 통째로 달라져 vlp 와 대조가
    안 된다(회귀 0 판정의 근거를 잃는다). 대신 붙이는 쪽을 견고하게 만든다.

    정확 일치가 **항상 이긴다** — 원문이 진짜로 `3. 항목` 인 경우를 번호로 오해하면
    안 되므로, 번호를 뗀 열쇠는 정확 일치를 다 넣은 **뒤**에 빈 자리만 채운다."""
    idx: dict[str, dict[str, Any]] = {}
    for r in rows:
        idx.setdefault(str(r.get("source", "")), r)
    for r in rows:
        bare = _SOURCE_INDEX_PREFIX.sub("", str(r.get("source", "")))
        idx.setdefault(bare, r)
    return idx


def apply_glossary(text: str, glossary: dict[str, str]) -> str:
    """번역 결과에 고정 용어집을 후처리로 강제(한국어 원형이 남아있으면 치환)."""
    for ko, ja in glossary.items():
        text = text.replace(ko, ja)
    return text


def parse_llm_json(content: str) -> list[dict[str, Any]]:
    """LLM 응답에서 JSON 배열 추출(코드펜스/잡텍스트 허용)."""
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
    start, end = content.find("["), content.rfind("]")
    if start != -1 and end != -1 and end > start:
        content = content[start:end + 1]
    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("LLM 응답이 JSON 배열이 아님")
    return data


# ── DeepL 초벌 (선택, requests) ──────────────────────────────────────────
def deepl_draft(texts: list[str], config: dict[str, Any]) -> dict[str, str]:
    key = get_secret("TRANSLATE_API_KEY")
    if not key:
        log.warning("TRANSLATE_API_KEY 없음 → DeepL 초벌 건너뜀")
        return {}
    import requests

    endpoint = config.get("translate", {}).get("deepl_endpoint",
                                                "https://api-free.deepl.com/v2/translate")
    out: dict[str, str] = {}
    for t in texts:
        try:
            r = requests.post(endpoint, data={"auth_key": key, "text": t,
                                              "source_lang": "KO", "target_lang": "JA"}, timeout=30)
            r.raise_for_status()
            out[t] = r.json()["translations"][0]["text"]
        except Exception as e:  # noqa: BLE001
            log.warning("DeepL 실패(%s): %s", t, e)
    return out


# ── LLM 트랜스크리에이션 ─────────────────────────────────────────────────
def batches(n: int, size: int) -> list[tuple[int, int]]:
    """[0,n) 을 size 이하 구간으로 자른다. 순수 — 테스트 대상.
    size<=0 이면 통짜 1구간(종전 동작)."""
    if size <= 0 or n <= size:
        return [(0, n)] if n else []
    return [(i, min(i + size, n)) for i in range(0, n, size)]


def transcreate(texts: list[str], config: dict[str, Any], hero: bool = False,
                use_deepl: bool = False,
                char_budgets: Optional[list[int]] = None) -> list[TranslationEntry]:
    """자막 전체를 일본어로 트랜스크리에이션.

    ⚠ 한 번에 다 보내면 긴 영상에서 응답이 max_tokens 에 잘려 JSON 이 안 닫힌다
      (2026-08-12 실측: 혜미리예채파 5화 — 19,380자에서 잘려 parse_llm_json 이
       'Unterminated string' 로 실패, localize 잡이 통째로 죽었다. max_tokens 8192).
      항목 수에 비례해 커지는 응답이라 상한을 올려도 더 긴 영상에서 같은 벽을 만난다.
      → translate.batch_size 개씩 나눠 보내고 결과를 합친다. 용어 일관성은 glossary
        후처리와 매 배치에 들어가는 persona 가 지킨다."""
    if not texts:
        return []
    tcfg0 = config.get("translate", {})
    size = int(tcfg0.get("batch_size", 40) or 0)
    spans = batches(len(texts), size)
    if len(spans) > 1:
        out: list[TranslationEntry] = []
        for a, b in spans:
            out += _transcreate_one(
                texts[a:b], config, hero=hero, use_deepl=use_deepl,
                char_budgets=(char_budgets[a:b] if char_budgets else None))
        return out
    return _transcreate_one(texts, config, hero=hero, use_deepl=use_deepl,
                            char_budgets=char_budgets)


def _transcreate_one(texts: list[str], config: dict[str, Any], hero: bool = False,
                     use_deepl: bool = False,
                     char_budgets: Optional[list[int]] = None) -> list[TranslationEntry]:
    if not texts:
        return []
    persona = load_persona(config)
    glossary = load_glossary(config)
    tcfg = config.get("translate", {})
    from app.localize.overlay import llm
    model = llm.resolve_model(config, hero=hero)

    drafts = deepl_draft(texts, config) if use_deepl else None
    system = build_system_prompt(persona, glossary)
    user = build_user_prompt(texts, drafts, char_budgets=char_budgets)

    log.info("트랜스크리에이션 provider=%s model=%s 텍스트=%d hero=%s deepl=%s",
             llm.provider(config), model, len(texts), hero, use_deepl)
    raw = llm.complete(system, user, config, model=model,
                       max_tokens=int(tcfg.get("max_tokens", 1024)), hero=hero)
    rows = parse_llm_json(raw)

    by_source = _index_by_source(rows)
    entries: list[TranslationEntry] = []
    for t in texts:  # 입력 순서·완전성 보장
        r = by_source.get(t, {})
        target = apply_glossary(r.get("target", ""), glossary)
        entries.append(TranslationEntry(source=t, target=target,
                                        notes=r.get("notes", ""),
                                        flagged=bool(r.get("flagged", not target))))
    # 🛑 **전부 비었으면 크게 실패한다.** 위 루프는 응답에서 못 찾은 항목을 빈 문자열로
    # 채우는데, 전부 못 찾으면 '번역이 전부 빈 채로' 렌더까지 조용히 간다.
    #
    # 2026-08-24 mm-06 실측이 정확히 그랬다: JSON 은 정상이었지만 source 키가 하나도 안
    # 맞아 18/18 이 빈 문자열이 됐고, 로그에는 'flagged 18' 만 찍힌 채 파이프라인이 끝까지
    # 돌아 **이벤트 0개짜리 자막**을 만들었다. 사람이 완성본을 볼 때까지 아무도 모른다.
    # 유료 호출을 하고 빈 결과를 내는 것은 실패지 결과가 아니다(무성 폴백 금지 규율).
    #
    # ⚠ 부분 누락은 그대로 둔다 — 한두 항목은 flagged 로 검수에서 잡힌다. 여기서 막는
    # 것은 '한 건도 못 붙인' 경우뿐이다.
    if texts and not any(e.target for e in entries):
        raise RuntimeError(
            f"트랜스크리에이션이 한 건도 안 붙었다(model={model}, 입력 {len(texts)}건 · "
            f"응답 행 {len(rows)}건) — 빈 번역으로 진행하지 않는다.\n"
            f"  응답의 source 키: {[r.get('source') for r in rows[:5]]}\n"
            f"  입력 텍스트:      {texts[:5]}")
    return entries


def translate(detections_path: str, config: dict[str, Any], hero: bool = False,
              use_deepl: bool = False, out_path: Optional[str] = None) -> TranslationDoc:
    doc = DetectionDoc.load(detections_path)
    texts = doc.unique_texts()
    from app.localize.overlay import llm
    model = llm.resolve_model(config, hero=hero)
    entries = transcreate(texts, config, hero=hero, use_deepl=use_deepl)
    tdoc = TranslationDoc(video_id=doc.video_id, model=model or "unknown", draft=True, entries=entries)

    out = Path(out_path) if out_path else resolve_path(
        f"{config['paths']['outputs_dir']}/{doc.video_id}/translations.json")
    tdoc.save(out)
    flagged = sum(1 for e in entries if e.flagged)
    log.info("번역 초벌 저장(검수 전): %s (항목 %d, 검수필요 %d)", out, len(entries), flagged)
    return tdoc


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="한국어 → 일본어 트랜스크리에이션(초벌)")
    p.add_argument("--detections", required=True, help="detections.json 경로")
    p.add_argument("--config", default=None)
    p.add_argument("--hero", action="store_true", help="고품질 모델(hero_model) 사용")
    p.add_argument("--deepl", action="store_true", help="DeepL 초벌 병용")
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    config = load_config(args.config)
    translate(args.detections, config, hero=args.hero, use_deepl=args.deepl, out_path=args.out)


if __name__ == "__main__":
    main()
