"""L5 — 유튜브 메타데이터 + 검수 대역(ko_ja_pairs).

원본: `localize_run.build_ko_ja_pairs` · `l5_metadata`.

`ko_ja_pairs` 는 관제 검수 카드가 **일본어 옆에 한국어 원문을 나란히** 보여주는 재료다
(기획서 §7 — 편집실이 이걸 편집 정본으로 승격시킨다).

⚠ 좌표 규약: 각 항목의 `idx` 는 translation 각 목록의 `index` 필드와 같은 좌표다 —
검수함 '수정 재렌더'의 오버라이드가 이 좌표로 돌아온다(overrides.apply_overrides).
텔롭의 idx 는 `orig_index`(= broadcast_telop 만 추린 순번)다.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.localize.apply import clamp_hallucination, has_user_timing

MAX_PAIR_ITEMS = 40      # 검수 카드가 감당할 분량. 편집실은 별 테이블로 전량을 받는다(P6)


def build_ko_ja_pairs(backup: Path, out_dir: Path, translation: dict,
                      max_items: int = MAX_PAIR_ITEMS) -> dict:
    """한글⇄일본어 대역. 원문은 백업(KO)·L2b 텔롭에서, 번역은 translation 에서.

    · subs: end 는 클램프·오버라이드 반영 후의 **실표시 값**(apply 와 같은 함수를 쓴다).
    · tts: cue 의 계획 창(start/end).
    · telops: onscreen_refined.json(실렌더 목록) 기준, idx = orig_index.
    실패는 조용히 비운다 — 대역은 검수 편의지 렌더 정본이 아니다. 순수(테스트 대상)."""
    pairs = {"top_title": None, "subs": [], "tts": [], "telops": []}
    try:
        ep = json.loads((backup / "edit_plan.json").read_text(encoding="utf-8"))
        ko = ((ep.get("layout") or {}).get("top_title") or "").strip()
        pairs["top_title"] = {"ko": ko or None, "ja": translation.get("top_title_ja")}
    except Exception:                                     # noqa: BLE001
        pairs["top_title"] = {"ko": None, "ja": translation.get("top_title_ja")}
    try:
        segs = json.loads((backup / "subtitle_segments.json").read_text(encoding="utf-8"))
        for i, (seg, tr) in enumerate(
                list(zip(segs, translation.get("segments") or []))[:max_items]):
            if tr.get("use") is False:            # 소프트 삭제(E6-0) — 다음 카드에서 뺀다
                continue                          # (idx 는 필터 전 순번이라 좌표 유지)
            user_timing = has_user_timing(tr)
            start = float(tr["start_sec"]) if tr.get("start_sec") is not None \
                else float(seg.get("start_sec", 0.0))
            end = float(tr["end_sec"]) if tr.get("end_sec") is not None \
                else float(seg.get("end_sec", 0.0))
            end = clamp_hallucination(start, end, tr.get("ja") or "", user_timing=user_timing)
            row = {"idx": tr.get("index", i), "start": start, "end": end,
                   "ko": seg.get("text"), "ja": tr.get("ja")}
            if tr.get("style"):
                row["style"] = tr["style"]
            pairs["subs"].append(row)
    except Exception:                                     # noqa: BLE001
        pass
    try:
        resources = json.loads((backup / "checkpoint_resources.json").read_text(encoding="utf-8"))
        cues = [c["cue"] for c in resources.get("tts_cue_files", [])]
        by_i = {t.get("index"): t for t in (translation.get("tts_cues") or [])}
        for i, cue in enumerate(cues[:max_items]):
            pairs["tts"].append({"idx": i, "start": cue.get("start_sec"),
                                 "end": cue.get("end_sec"), "ko": cue.get("text"),
                                 "ja": (by_i.get(i) or {}).get("ja")})
    except Exception:                                     # noqa: BLE001
        pass
    try:
        telops = json.loads((out_dir / "onscreen_refined.json").read_text(encoding="utf-8"))
        by_idx = {t.get("index"): t for t in (translation.get("telops") or [])}
        for t in telops[:max_items]:
            idx = t.get("orig_index")
            tr = by_idx.get(idx) or {}
            if tr.get("use") is False:
                continue
            start = float(tr["start_sec"]) if tr.get("start_sec") is not None \
                else t.get("start_sec")
            end = float(tr["end_sec"]) if tr.get("end_sec") is not None \
                else t.get("end_sec")
            row = {"idx": idx, "start": start, "end": end,
                   "ko": t.get("text_ko"), "ja": tr.get("ja")}
            if tr.get("style"):
                row["style"] = tr["style"]
            pairs["telops"].append(row)
    except Exception:                                     # noqa: BLE001
        pass
    return pairs


def build_description(translation: dict, wcfg: dict) -> tuple[str, list]:
    """설명란 본문 + 해시태그. 순수(테스트 대상).

    ⚠ 필수 고지(notice_lines)는 권리사 가이드 문구라 **번역하지 않고 그대로** 싣는다."""
    hashtags = list(dict.fromkeys(
        wcfg.get("hashtags_base", []) + translation.get("hashtags_extra", [])))
    lines = [translation["description_ja"], ""]
    lines += wcfg.get("notice_lines", [])
    lines += ["", " ".join(hashtags)]
    return "\n".join(lines), hashtags


def l5_metadata(job: Path, translation: dict, wcfg: dict, out_dir: Path):
    description, hashtags = build_description(translation, wcfg)
    meta = {
        "youtube_title": translation["youtube_title_ja"],
        "youtube_title_ko": translation.get("youtube_title_ko") or "",   # 검수 한글 대역(8/14)
        "description": description,
        "description_ko": translation.get("description_ko") or "",
        "tags": [h.lstrip("#") for h in hashtags],
        "top_title_burned": translation["top_title_ja"],
        "notes": translation.get("notes", []),
        # 한글 대역(8/14) — 검수 카드가 그대로 내려받아 보여준다
        "ko_ja_pairs": build_ko_ja_pairs(job / "localize_backup_ko", out_dir, translation),
        "_publish": "publish_youtube.py 의 --title·--description·--hashtags 로 이 값들이 "
                    "그대로 발행된다(ves-orchestrator approve_and_publish → brain Publish 배선, "
                    "2026-08-23). description 은 참고용이 아니라 **발행 본문**이다 — 권리사 "
                    "필수 고지(notice_lines)가 여기 반드시 들어가야 하고, publish_youtube 가 "
                    "빠졌으면 발행을 거부한다.",
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[L5] metadata.json — 제목: {meta['youtube_title']}")
