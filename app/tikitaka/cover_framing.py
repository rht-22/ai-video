"""Narration-guided framing, independent of dialogue/speaker motion.

Observe the uncropped source after the edit clock is fixed. Store a target box
covering the selected subject throughout the clip; the renderer decides whether
it fits. Never change clip boundaries, audio, captions, or a manual reframe.
"""
from __future__ import annotations

import copy
import json
import math
import subprocess

from app.tikitaka.common import find_bin
from app.tikitaka.grid import fingerprint, source_identity

SCHEMA = "narration_framing/v2"
SAMPLES = (0.02, 0.25, 0.5, 0.75, 0.98)
PROMPT = """이 사진들은 쇼츠에 쓸 원본 구간의 시간순 표본이다. 함께 제공된 대사/내레이션 문맥에
맞춰 **꽉 찬 크롭 하나**로 보여줄 핵심 대상 하나를 고르라. 가로 전체 축소(fit)는 금지다.
인물 설명은 해당 인물의 얼굴, 행동은 핵심 손·물건, 자료 화면은 주장에 필요한 숫자·문구를 고른다.
두 사람이 멀리 떨어져 있으면 문맥상 중심 인물 한 명을 고른다. 둘을 담으려고 전체 박스를 주지 마라.
자료도 전체 표/페이지가 아니라 읽어야 할 핵심 정보 영역만. 주변 맥락이 빠져 뜻이 바뀌면 needs_review.
입 움직임이나 가장 큰 얼굴을 기준으로 고르지 마라. 내레이션 덮개에서는 원음을 쓰지 않는다.
이름은 문맥으로 확인될 때만 쓴다. 외모만으로 이름을 추측하지 마라.
좌표는 [왼쪽,위,오른쪽,아래], 검은 띠를 포함한 전체 이미지 기준 0~1 소수다.
인물 박스는 정수리~턱의 머리 전체만(어깨/몸통 제외). 물건/자료는 의미가 전달되는 최소 영역.
여러 표본에서 같은 대상을 유지할 수 없거나, 핵심 영역이 크롭 허용 폭보다 크면 needs_review.
JSON 객체 하나: {"mode":"focus|needs_review", "target":"대상", "kind":"person|action|object|text",
"confidence":"high|low", "target_box":[0.1,0.1,0.4,0.8], "reason":"문맥과 화면 근거"}.
"""


def normalize(raw):
    """Uncertain observations request reselection; they never authorize a wide fit."""
    out = {"mode": "needs_review", "target": "", "reason": "불확실한 판정 — 화면 재선택 필요"}
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, dict):
        return out
    out.update(target=str(raw.get("target") or "")[:120], reason=str(raw.get("reason") or out["reason"])[:500])
    box = raw.get("target_box")
    valid = (isinstance(box, list) and len(box) == 4
             and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box)
             and box[0] < box[2] and box[1] < box[3])
    if raw.get("mode") == "focus" and raw.get("confidence") == "high" and valid and out["target"]:
        out.update(mode="focus", target_box=box, kind=str(raw.get("kind") or "scene"))
    return out


def apply(job, plan, story, *, get_gemini, enabled=True, include_clips=(), crop_width_ratio=None):
    result = copy.deepcopy(plan)
    # Rerunning with the switch off must remove generated metadata, not manual edits.
    for c in result["timeline"]:
        c.pop("narration_framing", None)
    if not enabled:
        return result, {"schema": SCHEMA, "enabled": False, "clips": []}
    beats = {b["number"]: b for b in story.get("beats", [])}
    records = []
    identity = source_identity(job.source)
    for i, c in enumerate(result["timeline"]):
        if (not c.get("cover") and i not in include_clips) or c.get("reframe"):
            continue
        beat = beats.get(c.get("beat"), {})
        text = str(beat.get("action") or "")
        context = {"narration": text,
                   "previous": beats.get(c.get("beat", 0)-1, {}).get("action", ""),
                   "next": beats.get(c.get("beat", 0)+1, {}).get("action", ""),
                   "crop_max_width_ratio": crop_width_ratio, "original_audio": not c.get("cover")}
        prompt = PROMPT + "\n문맥: " + json.dumps(context, ensure_ascii=False)
        a, z = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        key = fingerprint([SCHEMA, identity, a, z, SAMPLES, prompt])
        name = f"cover_framing/{key}.json"
        cached = job.has(name)
        error = None
        def source_frames():
            paths = []
            for k, frac in enumerate(SAMPLES):
                p = job.path(f"cover_framing/{key}_{k}.jpg")
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    subprocess.run([find_bin("ffmpeg"), "-v", "error", "-y", "-ss", f"{a+(z-a)*frac:.6f}",
                        "-i", str(job.source), "-frames:v", "1", "-vf", "scale=-2:480", "-q:v", "3", str(p)],
                        check=True, capture_output=True)
                paths.append(p)
            return paths
        try:
            if cached:
                raw = job.load(name)
            elif not text.strip():
                raw = {"mode": "needs_review", "reason": "내레이션 문맥 없음"}
            else:
                raw = get_gemini().images_json(prompt, source_frames(), kind="cover_framing", max_output_tokens=2048)
                job.save(name, raw)
            decision = normalize(raw)
            obj = raw[0] if isinstance(raw, list) and len(raw) == 1 else raw
            if (isinstance(obj, dict) and obj.get("mode") == "focus"
                    and obj.get("confidence") == "high" and decision["mode"] != "focus"):
                # Mixed 0..1 / 0..1000 coordinates are not repaired by guessing.
                # Ask once with the same source evidence; another invalid answer
                # stays in review. Cache the bounded retry separately.
                retry_name = f"cover_framing/{key}_coordinates.json"
                if job.has(retry_name):
                    corrected = job.load(retry_name)
                else:
                    job.log(f"[덮개/구도] clip{i} 잘못된 좌표 — 원본 표본으로 1회 재확인")
                    corrected = get_gemini().images_json(prompt +
                        "\n이전 응답의 좌표 형식이 잘못되었다. 네 좌표 모두 0 이상 1 이하 소수여야 한다. "
                        "1000 기준 정수나 픽셀과 섞지 마라. 원본을 다시 보고 JSON 객체 하나만 반환하라.\n이전 응답: " +
                        json.dumps(raw, ensure_ascii=False), source_frames(), kind="cover_framing", max_output_tokens=2048)
                    job.save(retry_name, corrected)
                decision = normalize(corrected)
        except Exception as exc:
            # Fail visibly without authorizing wide framing. Do not cache transient failures.
            error = f"{type(exc).__name__}: {str(exc)[:180]}"
            decision = {"mode": "needs_review", "target": "", "reason": f"판정 실패 — 화면 재선택 필요: {error}"}
        c["narration_framing"] = decision
        records.append({"clip": i, "source": [a, z], "narration": text,
                        "cache": cached, "error": error, **decision})
        job.log(f"[덮개/구도] clip{i} {decision['mode']} · {decision['target']} — {decision['reason']}")
    audit = {"schema": SCHEMA, "enabled": True, "clips": records}
    job.save("cover_framing.json", audit)
    return result, audit
