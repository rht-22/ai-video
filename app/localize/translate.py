"""L1 — 문맥 통번역 + ASR 교정 + 용어집 강제.

원본: `localize_run.l1_translate` · `_fit_title`.

세그먼트를 한 줄씩 번역하지 않는다 — 전체 대본 + 작품 문맥 + **L2 텔롭 원문**을 함께
넘긴다. 텔롭이 ASR 오청취를 교정하는 것이 실증됐다('멀티 그룹/그루브'). 예능 대사는
문맥 없이는 오역된다.

⚠ 구조화 출력(response_schema)은 필수다 — free-form JSON 은 텍스트 안 따옴표에서 깨진다.
⚠ 1:1 정렬이 깨지면 자막 싱크가 통째로 어긋나므로 **즉시 실패**한다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.localize.spec import model_flash, model_pro
from app.localize.telop import only_broadcast_telops

SCHEMA_TRANSLATE = {
    "type": "object",
    "properties": {
        "segments": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "ja": {"type": "string"}},
            "required": ["index", "ja"]}},
        "tts_cues": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "ja": {"type": "string"}},
            "required": ["index", "ja"]}},
        "top_title_ja": {"type": "string"},
        "youtube_title_ja": {"type": "string"},
        "youtube_title_ko": {"type": "string"},   # 검수 한글 대역(8/14)
        "description_ja": {"type": "string"},
        "description_ko": {"type": "string"},     # 검수 한글 대역(8/14)
        "hashtags_extra": {"type": "array", "items": {"type": "string"}},
        "telops": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "use": {"type": "boolean"}, "ja": {"type": "string"}},
            "required": ["index", "use"]}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["segments", "tts_cues", "top_title_ja", "youtube_title_ja",
                 "description_ja", "telops"],
}

TRANSLATE_PROMPT = """당신은 한국 예능 쇼츠를 일본 시청자용으로 현지화하는 전문 번역가입니다.
아래 입력(JSON)에는 한 쇼츠의 ① 음성인식 대사 자막(segments) ② 내레이션(tts_cues)
③ 상단 제목(top_title) ④ 화면에 보이는 방송 텔롭 원문(telops) ⑤ 작품 정보가 들어 있습니다.

작업:
1. **ASR 교정**: segments 는 음성인식 결과라 오류가 있습니다. telops(방송 자막 원문)와 문맥을
   근거로 잘못 들린 단어를 교정한 뒤 번역하세요. 교정한 것은 notes 에 한 줄씩 기록.
   비정상적으로 긴 구간(10초↑)에 짧은 텍스트가 걸린 segment 는 환각일 수 있으니 문맥상
   자연스러운 위치의 대사로만 번역하고 notes 에 표시.
2. **대사 번역(segments)**: 일본 쇼츠 자막 문체 — 짧은 구어체, 멤버 관계에 맞는 반말/존댓말,
   감탄사는 일본식으로 의역. 직역 금지. **1:1 정렬 유지(병합·삭제 금지)**.
   줄바꿈을 위해 **구절 경계마다 반각 공백**을 넣으세요(한 구절 최대 14자 목표).
3. **내레이션 번역(tts_cues)**: 짧고 힘있는 예능 내레이션 문체.
4. **상단 제목(top_title_ja)**: 2줄, **각 줄 전각 11자 이내(필수 — 넘으면 화면에서 잘린다)**. 낚시 금지 — 실제 내용 기반으로 궁금증 유발.
5-1. **한국어 대역(youtube_title_ko·description_ko)**: 한국인 검수자용 — 위에서 만든
   일본어 제목·설명의 한국어 직역을 함께 출력한다(간결하게, 의역 금지).
5. **유튜브 제목(youtube_title_ja)**: 90자 이내, 반드시 작품명 「{work_display}」 포함,
   일본 쇼츠 어법(w, 〜, ！ 활용 가능).
6. **설명란(description_ja)**: 1~2문장, 해시태그 없이.
7. **텔롭 번역(telops)**: kind 가 broadcast_telop 인 것만. 대사 자막(segments)이나 내레이션과
   내용이 중복되면 use=false. 나머지는 일본 예능 텔롭 문체로 짧게 번역(use=true).
8. **용어집 준수(필수)**: {glossary}

출력은 아래 JSON 스키마만:
{{
  "segments": [{{"index": 0, "ja": "..."}}, ...],
  "tts_cues": [{{"index": 0, "ja": "..."}}, ...],
  "top_title_ja": "1줄목\\n2줄목",
  "youtube_title_ja": "...",
  "description_ja": "...",
  "hashtags_extra": ["#..."],
  "telops": [{{"index": 0, "use": true, "ja": "..."}}, ...],
  "notes": ["교정/특이사항 ..."]
}}"""

TITLE_LINE_MAX = 11   # 전각 기준. 렌더 자동 축소는 13자부터라 CJK 는 그 전에 잘린다(파일럿 실측)


def title_needs_fit(top_title_ja: str, limit: int = TITLE_LINE_MAX) -> bool:
    """줄 하나라도 상한(+1 관용)을 넘는가. 순수(테스트 대상)."""
    return not all(len(line) <= limit + 1 for line in (top_title_ja or "").split("\n"))


def check_alignment(ko_segments: list, ko_cues: list, data: dict) -> None:
    """1:1 정렬 검증 — 깨지면 자막 싱크가 통째로 어긋나므로 즉시 실패. 순수(테스트 대상)."""
    if len(data["segments"]) != len(ko_segments):
        raise RuntimeError(
            f"segments 정렬 불일치: ko {len(ko_segments)} vs ja {len(data['segments'])}")
    if len(data["tts_cues"]) != len(ko_cues):
        raise RuntimeError(
            f"tts_cues 정렬 불일치: ko {len(ko_cues)} vs ja {len(data['tts_cues'])}")


def check_work_display(data: dict, work_display: str) -> None:
    """유튜브 제목에 작품명이 들어갔는가 — 권리사 가이드 요구. 순수(테스트 대상)."""
    if work_display not in data["youtube_title_ja"]:
        raise RuntimeError(
            f"유튜브 제목에 작품명({work_display}) 누락: {data['youtube_title_ja']!r}")


def _fit_title(data: dict, client) -> bool:
    """top_title_ja 각 줄이 상한을 넘으면 그 줄만 축약해 고쳐 쓴다. 고쳤으면 True."""
    if not title_needs_fit(data["top_title_ja"]):
        return False
    from google.genai import types
    resp = client.models.generate_content(
        model=model_flash(),
        contents=[f"다음 일본어 쇼츠 제목의 각 줄을 전각 {TITLE_LINE_MAX}자 이내로 줄여 주세요. "
                  f"의미·임팩트 유지, 2줄 유지, 줄바꿈은 \\n.\n\n{data['top_title_ja']}"],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={"type": "object",
                             "properties": {"top_title_ja": {"type": "string"}},
                             "required": ["top_title_ja"]}),
    )
    fixed = json.loads(resp.text)["top_title_ja"]
    print(f"[L1] 제목 축약: {data['top_title_ja']!r} → {fixed!r}")
    data["top_title_ja"] = fixed
    return True


def build_payload(backup: Path, telop_data: list, work_title: str, wcfg: dict) -> dict:
    """번역 입력 조립 — 입력은 **항상 KO 백업본**이다(재실행 이중 번역 방지). 순수 IO."""
    segments = json.loads((backup / "subtitle_segments.json").read_text(encoding="utf-8"))
    resources = json.loads((backup / "checkpoint_resources.json").read_text(encoding="utf-8"))
    cues = [c["cue"]["text"] for c in resources.get("tts_cue_files", [])]
    top_title = (backup / "title.txt").read_text(encoding="utf-8").strip("﻿\n")
    # ⚠️ 인덱스는 broadcast_telop 만 추린 목록의 순서다 — L2b(orig_index)·L3(ass 매칭)와 같은 규약.
    telops = [{"index": i, "start_sec": t["start_sec"], "end_sec": t["end_sec"],
               "text_ko": t["text_ko"], "position": t.get("position")}
              for i, t in enumerate(only_broadcast_telops(telop_data))]
    payload = {
        "work": {"title_ko": work_title, "display_ja": wcfg["display"], "context": wcfg["context"]},
        "top_title": top_title,
        "segments": [{"index": i, "start_sec": s["start_sec"], "end_sec": s["end_sec"],
                      "ko": s["text"]} for i, s in enumerate(segments)],
        "tts_cues": [{"index": i, "ko": t} for i, t in enumerate(cues)],
        "telops": telops,
    }
    return {"payload": payload, "segments": segments, "cues": cues}


def l1_translate(backup: Path, telop_data: list, work_title: str, wcfg: dict,
                 out_dir: Path, client) -> dict:
    out_path = out_dir / "translation.json"
    if out_path.exists():
        print("[L1] 기존 번역 결과 사용")
        data = json.loads(out_path.read_text(encoding="utf-8"))
        if _fit_title(data, client):
            out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    from google.genai import types
    built = build_payload(backup, telop_data, work_title, wcfg)
    prompt = TRANSLATE_PROMPT.format(work_display=wcfg["display"],
                                     glossary=json.dumps(wcfg["glossary"], ensure_ascii=False))
    t0 = time.time()
    resp = client.models.generate_content(
        model=model_pro(),
        contents=[prompt, json.dumps(built["payload"], ensure_ascii=False, indent=1)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=SCHEMA_TRANSLATE),
    )
    data = json.loads(resp.text)
    check_alignment(built["segments"], built["cues"], data)
    check_work_display(data, wcfg["display"])
    _fit_title(data, client)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[L1] {time.time()-t0:.0f}s — segments {len(data['segments'])} · telop 사용 "
          f"{sum(1 for t in data['telops'] if t.get('use'))}건 · notes {len(data.get('notes', []))}건")
    for n in data.get("notes", []):
        print(f"     note: {n}")
    return data
