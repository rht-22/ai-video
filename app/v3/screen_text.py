"""화면 글자 정독 패스 (2단계 · 갭 1, 2026-09-08).

Stage 2(480p/3fps 표본)가 "글자가 있는데 못 읽었다"(`has_text: true` + `screen_text` 비거나
짧음)고 자기 신고한 span 만, **원본 해상도 프레임**을 Flash 에 다시 보여 원문을 받는다.
OCR 엔진은 쓰지 않는다 — Gemini 가 프레임을 이미 본다(갭 문서 「갭 1」).

규율:
- 장면 단위 1콜: 대상 span 을 grid 순으로 훑어 인접(틈 ≤ CLUSTER_GAP_SEC) span 을 한 장면으로
  묶는다. EP01 실측 86 span → 18 장면이 합격 기준.
- **대상은 글자 장면 전부다**(2026-09-08 트리거 교정 — 사용자 지적: 480p 초벌이 '자긴거 알지^^' 를
  '자전거 말자^^' 로 **자신 있게 잘못 읽은** 경우를 자기 신고 트리거가 못 잡았다). 못 읽은(unread)
  장면이 우선순위 맨 앞, 그 뒤는 importance·길이 순으로 예산까지. 초벌 판독은 프롬프트에 힌트로
  실어 원본 프레임과 대조하게 한다(힌트를 베끼지 말라는 지시 포함).
- 예산은 편당 상수가 아니라 회차 길이 비례(10분당 SCREEN_TEXT_BUDGET_PER_10MIN). 소진·실패는
  초벌 유지 + 기록(refine FLASH_BUDGET 규율).
- 순수 함수(targets·cluster·order·frame_times·validate)와 I/O(extract·call·run)를 나눈다 —
  회귀 가드는 순수 부분만 LLM·ffmpeg 없이 돈다.
- 결과는 대상 span 의 `screen_text` 를 채우고 `screen_text_source: "fullres"` 를 붙인다
  (Stage 2 초벌은 "stage2"). 사이드카 `checkpoint_screen_text.json` 은 Stage 2 캐시 지문에
  묶인다 — 재개 시 재호출 없음.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from app.modules.gemini_client import _extract_json_from_markdown
from app.v3 import schemas

SCREEN_TEXT_BUDGET_PER_10MIN = 4    # 편당 상한 = ceil(러닝타임/600) × 이 값 (EP01 3146s → 24)
CLUSTER_GAP_SEC = 2.0               # 대상 span 사이 틈이 이 이하면 한 장면
SHORT_TEXT_CHARS = 4                # 초벌 screen_text 가 이 길이 이하면 '못 읽은 것'으로 본다
LONG_SCENE_SEC = 4.0                # 이보다 긴 장면은 시작·끝 프레임을 더 뜬다(스크롤 화면)
EDGE_INSET_SEC = 0.3                # 시작·끝 프레임을 경계에서 이만큼 안쪽으로
MAX_CHARS = 300                     # chunk_analyze.SCREEN_TEXT_MAX_CHARS 와 같은 값
SOURCE_STAGE2 = "stage2"
SOURCE_FULLRES = "fullres"
CKPT_NAME = "checkpoint_screen_text.json"

PROMPT = """이 프레임(들)은 드라마 원본의 같은 장면이다. 화면에 보이는 **글자를 보이는 그대로** 옮겨라.
- 화면 종류를 하나 골라라: 메시지/기사/댓글/문서/검색/기타
- 원문 그대로 — 요약·해석·맞춤법 교정 금지. 여러 줄이면 `/` 로 잇는다. 프레임이 여럿이면 순서대로 이어 붙이되 같은 줄은 한 번만. 메시지·채팅이면 **상대 이름표·앱 이름**도 있으면 함께 옮겨라.
- 못 읽는 글자는 `?` 로 남겨라. **지어내지 마라.** 글자가 실제로 없거나 전혀 읽을 수 없으면 readable: false.{hint}
JSON 만: {{"kind": "메시지", "text": "…", "readable": true}}"""
HINT_LINE = ("\n- 저해상도 초벌 판독(참고 — 틀릴 수 있다, **베끼지 말고 프레임의 글자로 확인**하라): "
             "「{draft}」")


# ── 순수 ─────────────────────────────────────────────────────────────────────

def is_unread(span: dict) -> bool:
    """초벌이 못 읽은 자기 신고 — has_text 인데 screen_text 가 없거나 짧다(우선순위 맨 앞)."""
    if not span.get("has_text"):
        return False
    txt = str(span.get("screen_text") or "").strip()
    return len(txt) <= SHORT_TEXT_CHARS


def is_target(span: dict) -> bool:
    """정독 대상 — 글자가 있다고 표시된 span 전부(초벌이 읽은 것도 원본으로 대조한다)."""
    return bool(span.get("has_text") or str(span.get("screen_text") or "").strip())


def targets(stage2_doc: dict) -> list[dict]:
    """stage2 문서의 대상 span 목록(시각 순). 항목은 문서 span dict 를 **참조**로 든다 — 결과를
    그 자리에 써 넣기 위해(stage2.json 은 정독 결과가 반영된 문서 하나여야 한다)."""
    out: list[dict] = []
    for sq in stage2_doc.get("sequences") or []:
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                for s in m.get("spans") or []:
                    if not isinstance(s, dict) or not is_target(s):
                        continue
                    out.append({"span_id": s.get("span_id"),
                                "t0": schemas.parse_ts(s["time"]["start"]),
                                "t1": schemas.parse_ts(s["time"]["end"]),
                                "importance": int(s.get("importance") or 3),
                                "unread": is_unread(s),
                                "draft": (str(s.get("screen_text") or "").strip()
                                          if s.get("screen_text_source") != SOURCE_FULLRES else ""),
                                "meaning": m, "span": s})
    out.sort(key=lambda r: (r["t0"], str(r["span_id"])))
    return out


def cluster_scenes(rows: list[dict], gap_sec: float = CLUSTER_GAP_SEC) -> list[dict]:
    """시각 순 대상 → 장면 묶음 [{t0, t1, span_ids, importance, rows}]. 순수."""
    scenes: list[dict] = []
    for r in sorted(rows, key=lambda x: (x["t0"], str(x.get("span_id")))):
        if scenes and r["t0"] - scenes[-1]["t1"] <= gap_sec:
            sc = scenes[-1]
            sc["t1"] = max(sc["t1"], r["t1"])
            sc["span_ids"].append(r["span_id"])
            sc["importance"] = max(sc["importance"], r["importance"])
            sc["unread"] = sc["unread"] or bool(r.get("unread"))
            if r.get("draft") and r["draft"] not in sc["drafts"]:
                sc["drafts"].append(r["draft"])
            sc["rows"].append(r)
        else:
            scenes.append({"t0": r["t0"], "t1": r["t1"], "span_ids": [r["span_id"]],
                           "importance": r["importance"], "unread": bool(r.get("unread")),
                           "drafts": [r["draft"]] if r.get("draft") else [], "rows": [r]})
    return scenes


def budget_for(duration_sec: float | None,
               per_10min: int = SCREEN_TEXT_BUDGET_PER_10MIN) -> int:
    """편당 콜 상한 — 회차 길이 비례(10분 단위 올림). 길이를 모르면 10분 한 단위."""
    d = float(duration_sec or 0.0)
    units = max(1, int(math.ceil(d / 600.0))) if d > 0 else 1
    return units * per_10min


def order_scenes(scenes: list[dict]) -> list[dict]:
    """예산을 먼저 쓸 순서 — 못 읽은 장면(unread) 먼저, 그 뒤 importance 높고 긴 장면(동률은 시각 순)."""
    return sorted(scenes, key=lambda s: (0 if s.get("unread") else 1, -s["importance"],
                                         -(s["t1"] - s["t0"]), s["t0"]))


def scene_hint(scene: dict) -> str:
    """초벌 판독 힌트 줄 — 초벌이 없으면 빈 문자열(프롬프트 종전과 동일)."""
    drafts = [d for d in (scene.get("drafts") or []) if d]
    if not drafts:
        return ""
    return HINT_LINE.format(draft=" / ".join(drafts)[:200])


def frame_times(scene: dict, *, long_sec: float = LONG_SCENE_SEC,
                inset: float = EDGE_INSET_SEC) -> list[float]:
    """장면 대표 프레임 시각 — 중앙 1장. 장면이 long_sec 를 넘으면 시작·끝 2장 더(스크롤).
    짧아도 초벌 판독이 조각마다 다르면(타이핑·전송으로 글자가 바뀌는 화면 — '자긴거 알지^^' 실측:
    마지막 메시지가 끝 프레임에만 있다) 끝 프레임을 더 뜬다."""
    t0, t1 = float(scene["t0"]), float(scene["t1"])
    mid = round((t0 + t1) / 2.0, 3)
    z = round(max(t1 - inset, mid), 3)
    if t1 - t0 <= long_sec:
        if len(scene.get("drafts") or []) >= 2 and z > mid:
            return [mid, z]
        return [mid]
    a = round(min(t0 + inset, mid), 3)
    return [a, mid, z]


def validate_read(resp: Any) -> tuple[dict | None, list[str]]:
    """모델 답 → ({kind, text, readable} | None, 사유). text 는 MAX_CHARS 절단."""
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"]
    readable = resp.get("readable")
    if not isinstance(readable, bool):
        return None, ["readable 이 bool 이 아니다"]
    text = resp.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        return None, ["text 가 문자열이 아니다"]
    text = text.strip()[:MAX_CHARS]
    kind = str(resp.get("kind") or "기타").strip()[:20]
    if readable and not text:
        return None, ["readable 인데 text 가 비었다"]
    return {"kind": kind, "text": text, "readable": readable}, []


def apply_result(scene: dict, result: dict) -> int:
    """읽힌 원문을 장면의 대상 span 전부에 써 넣는다(같은 화면이다). 채운 span 수.
    초벌이 있던 span 은 초벌을 `screen_text_draft` 에 남긴다(무엇이 바뀌었는지 감사)."""
    if not result.get("readable") or not result.get("text"):
        return 0
    n = 0
    for r in scene["rows"]:
        sp = r["span"]
        old = str(sp.get("screen_text") or "").strip()
        if old and old != result["text"] and sp.get("screen_text_source") != SOURCE_FULLRES:
            sp["screen_text_draft"] = old
        sp["screen_text"] = result["text"]
        sp["has_text"] = True
        sp["screen_text_source"] = SOURCE_FULLRES
        sp["screen_text_kind"] = result["kind"]
        n += 1
    return n


def refresh_meaning_texts(stage2_doc: dict) -> None:
    """meaning 수준 screen_texts 를 span 상태에서 다시 만든다(정독 뒤 — 걸음 1·2 재료)."""
    for sq in stage2_doc.get("sequences") or []:
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                texts = [s["screen_text"] for s in m.get("spans") or []
                         if isinstance(s, dict) and s.get("screen_text")]
                if texts:
                    m["screen_texts"] = texts
                else:
                    m.pop("screen_texts", None)


def mark_stage2_sources(stage2_doc: dict) -> int:
    """초벌 screen_text 에 출처 표기(stage2) — 이미 출처가 있으면 그대로. 표기한 span 수."""
    n = 0
    for sq in stage2_doc.get("sequences") or []:
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                for s in m.get("spans") or []:
                    if isinstance(s, dict) and s.get("screen_text") and not s.get("screen_text_source"):
                        s["screen_text_source"] = SOURCE_STAGE2
                        n += 1
    return n


# ── I/O ──────────────────────────────────────────────────────────────────────

def extract_frame(ffmpeg: str, video: Path, t: float, out: Path) -> None:
    """원본 해상도 그대로(-q:v 3 JPEG) — verify_scene_binding 과 같은 호출 모양, 소재만 원본."""
    subprocess.run([ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", str(video),
                    "-frames:v", "1", "-q:v", "3", str(out)],
                   check=True, capture_output=True)


def call_read(gemini, frames: list[Path], hint: str = "") -> dict:
    types = gemini.types
    parts: list[Any] = []
    for i, f in enumerate(frames):
        parts.append(types.Part.from_bytes(data=f.read_bytes(), mime_type="image/jpeg"))
        parts.append(f"프레임 {i}")
    parts.append(PROMPT.format(hint=hint))
    resp = gemini.client.models.generate_content(
        model=gemini.config.flash_model_name, contents=parts,
        config=types.GenerateContentConfig(
            temperature=0.0, response_mime_type="application/json",
            max_output_tokens=2048))
    return json.loads(_extract_json_from_markdown(resp.text or ""))


def run_screen_text_pass(gemini, stage2_doc: dict, video_path: Path, out_dir: Path, *,
                         duration_sec: float | None, fingerprint: str,
                         ckpt_path: Path | None = None,
                         extract: Callable[[str, Path, float, Path], None] | None = None,
                         call: Callable[[Any, list[Path]], dict] | None = None,
                         ffmpeg: str | None = None,
                         log=print) -> dict:
    """대상 span 을 장면으로 묶어 원본 프레임으로 다시 읽고 stage2_doc 에 **그 자리에서** 써 넣는다.

    반환 audit = {scenes, calls, filled, budget, skipped, details}. 사이드카가 지문과 맞으면
    저장된 결과를 재적용하고 호출 0. 실패·예산 소진은 초벌 유지 + 기록."""
    mark_stage2_sources(stage2_doc)
    rows = targets(stage2_doc)
    scenes = cluster_scenes(rows)
    budget = budget_for(duration_sec)
    audit: dict[str, Any] = {"targets": len(rows), "scenes": len(scenes),
                             "unread_scenes": sum(1 for s in scenes if s.get("unread")),
                             "calls": 0, "filled": 0, "changed": 0, "budget": budget, "skipped": 0,
                             "details": []}
    if not scenes:
        refresh_meaning_texts(stage2_doc)
        return audit

    ckpt_path = ckpt_path or (out_dir / CKPT_NAME)
    saved: dict[str, dict] = {}
    if ckpt_path.exists():
        try:
            doc = json.loads(ckpt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        if doc.get("fingerprint") == fingerprint:
            saved = {k: v for k, v in (doc.get("scenes") or {}).items()}
            if saved:
                log(f"  [v3/screen_text] 사이드카 {len(saved)}장면 재적용(호출 0)")
        elif doc:
            log("  [v3/screen_text] ⚠ 상류 변경 감지 — 정독 사이드카 폐기")

    _extract = extract or extract_frame
    _call = call or call_read
    if _extract is extract_frame and ffmpeg is None:
        from app.modules.ffmpeg_utils import find_ffmpeg_command
        ffmpeg = find_ffmpeg_command("ffmpeg")
    frames_dir = out_dir / "screen_text_frames"
    used = 0
    results: dict[str, dict] = dict(saved)
    for sc in order_scenes(scenes):
        key = f"{sc['t0']:.3f}-{sc['t1']:.3f}"
        det: dict[str, Any] = {"key": key, "span_ids": list(sc["span_ids"]),
                               "importance": sc["importance"], "unread": bool(sc.get("unread"))}
        if sc.get("drafts"):
            det["draft"] = " / ".join(sc["drafts"])[:80]
        if key in results:
            res = results[key]
            det["result"] = "sidecar"
        elif used >= budget:
            audit["skipped"] += 1
            det["result"] = "예산 소진 — 초벌 유지"
            audit["details"].append(det)
            continue
        else:
            used += 1
            audit["calls"] += 1
            t0 = time.time()
            frames: list[Path] = []
            try:
                frames_dir.mkdir(parents=True, exist_ok=True)
                for i, t in enumerate(frame_times(sc)):
                    f = frames_dir / f"{key}_{i}.jpg"
                    _extract(ffmpeg or "ffmpeg", video_path, t, f)
                    frames.append(f)
                res, problems = validate_read(_call(gemini, frames, hint=scene_hint(sc)))
                if res is None:
                    det["result"] = "판정 불가 — 초벌 유지: " + "; ".join(problems)
                    det["elapsed"] = round(time.time() - t0, 1)
                    audit["details"].append(det)
                    continue
            except Exception as e:  # noqa: BLE001 — 초벌 유지(refine 규율)
                det["result"] = f"실패 — 초벌 유지: {type(e).__name__}: {str(e)[:80]}"
                det["elapsed"] = round(time.time() - t0, 1)
                audit["details"].append(det)
                continue
            det["elapsed"] = round(time.time() - t0, 1)
            det["result"] = "read"
            results[key] = res
            ckpt_path.parent.mkdir(parents=True, exist_ok=True)
            ckpt_path.write_text(json.dumps({"fingerprint": fingerprint, "scenes": results},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
        before = [str(r["span"].get("screen_text") or "") for r in sc["rows"]]
        n = apply_result(sc, res)
        changed = sum(1 for b, r in zip(before, sc["rows"]) if n and b and b != r["span"].get("screen_text"))
        det["readable"] = bool(res.get("readable"))
        det["kind"] = res.get("kind")
        det["text"] = (res.get("text") or "")[:80]
        det["filled"] = n
        det["changed"] = changed
        audit["filled"] += n
        audit["changed"] += changed
        audit["details"].append(det)
        if n:
            log(f"  [v3/screen_text] {schemas.format_ts(sc['t0'])} {res.get('kind')} "
                f"{n}조각 ← 「{(res.get('text') or '')[:40]}」"
                + (f" (초벌 {changed}조각 교정)" if changed else ""))
        else:
            log(f"  [v3/screen_text] {schemas.format_ts(sc['t0'])} 읽을 글자 없음(readable=false) — 초벌 유지")
    audit["details"].sort(key=lambda d: d["key"])
    refresh_meaning_texts(stage2_doc)
    if audit["skipped"]:
        log(f"  [v3/screen_text] ⚠ 예산 {budget}콜 소진 — {audit['skipped']}장면 초벌 유지")
    return audit
