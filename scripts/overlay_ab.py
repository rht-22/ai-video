#!/usr/bin/env python3
"""L-P4 회귀 0 대조 — overlay 산출물을 구·신 엔진 사이에서 맞댄다.

    python -m scripts.overlay_ab --a <구엔진 outputs/{id}> --b <신엔진 outputs/{id}>

🛑 **`scripts/localize_ab.py` 로는 이 판정을 못 한다.** 그것은 rerender 전용이라
`shorts_ko.mp4`·`localize_backup_ko/`·`metadata.json` 을 찾는데 overlay 산출에는 그
파일들이 **아예 없다** — 하나도 못 찾은 채 '차이 없음'을 내는 거짓 합격이 된다
(P1 노드 실측에서 두 번 당한 그 실패 모드다). 그래서 별도 도구다.

계획서 §8-3 이 요구하는 세 가지를 잰다:

  · **CER**      — 한국어는 WER 이 아니다. 원문(source)은 **완전 일치**여야 하고
                   (다르면 OCR·탐지가 흔들린 것), 번역문(target)은 CER 로 거리만 본다
                   (LLM 비결정성은 회귀가 아니다 — localize_ab 와 같은 규율).
  · **라우드니스** — 최종본의 integrated LUFS. 목표는 -16 이고 **구·신 차이**를 본다.
  · **세그먼트 정렬** — `ja_events.json` 의 start/end 가 같은 자리인가. 여기가 어긋나면
                   자막이 딴 장면에 뜬다 — 사람 눈에 바로 보이는 회귀다.

CER 은 이식본 `overlay/common.cer` 를 그대로 쓴다 — vlp 와 **같은 함수**여야 숫자가
비교 가능하다(베낀 수식은 언젠가 어긋난다는 E13 교훈).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.localize.overlay.common import cer, norm_text  # noqa: E402
from app.modules.ffmpeg_utils import find_ffmpeg_command  # noqa: E402

# 판정 임계 — 넘으면 회귀로 본다
ALIGN_TOL_SEC = 0.05      # 세그먼트 시작·끝 허용 오차(인코더·양자화 흔들림)
LUFS_TOL_DB = 1.0         # 라우드니스 허용 오차. 정규화가 같으면 이보다 훨씬 붙는다
DUR_TOL_SEC = 0.05
TARGET_LUFS = -16.0       # 계획서 §8-3 의 목표값 (참고 표시용)
FRAME_SAMPLES = 8

ARTIFACTS = ("detections.json", "translations.json", "ja_events.json",
             "ja.srt", "ja.ass", "final_draft.mp4")


# ─────────────────────────── 순수 (테스트 대상) ───────────────────────────

def load_entries(doc) -> list:
    """translations.json → [(source, target, use)]. 모양이 달라도 죽지 않는다."""
    if not isinstance(doc, dict):
        return []
    out = []
    for e in doc.get("entries") or []:
        if isinstance(e, dict):
            out.append((str(e.get("source", "")), str(e.get("target", "")),
                        bool(e.get("use", True))))
    return out


def source_diff(a: list, b: list) -> list:
    """원문(한국어) 차이 — **이것이 나면 회귀다.** OCR·탐지가 흔들렸다는 뜻이다."""
    lines = []
    if len(a) != len(b):
        lines.append(f"⚠ 항목 수: {len(a)} → {len(b)}")
    for i, (x, y) in enumerate(zip(a, b)):
        if norm_text(x[0]) != norm_text(y[0]):
            lines.append(f"⚠ [{i}] 원문 변경: {x[0]!r} → {y[0]!r}")
        if x[2] != y[2]:
            lines.append(f"⚠ [{i}] use 변경: {x[2]} → {y[2]}")
    return lines


def target_cer(a: list, b: list) -> dict:
    """번역문 거리. 회귀 판정에는 안 쓰고 **크기만 보고한다**(LLM 비결정성).

    같은 번역이면 0.0 이다 — 고정 translations.json 으로 돌리면 그것까지 확인된다."""
    pairs = [(x[1], y[1]) for x, y in zip(a, b)]
    if not pairs:
        return {"n": 0, "mean": 0.0, "max": 0.0, "identical": 0}
    vals = [cer(x, y) for x, y in pairs]
    return {"n": len(vals), "mean": round(sum(vals) / len(vals), 4),
            "max": round(max(vals), 4), "identical": sum(1 for v in vals if v == 0)}


def align_diff(a_events: list, b_events: list, tol: float = ALIGN_TOL_SEC) -> tuple:
    """세그먼트 정렬. (문제 줄, 최대 오차). 여기가 어긋나면 자막이 딴 장면에 뜬다."""
    lines, worst = [], 0.0
    if len(a_events) != len(b_events):
        lines.append(f"⚠ 이벤트 수: {len(a_events)} → {len(b_events)}")
    for i, (x, y) in enumerate(zip(a_events, b_events)):
        ds = abs(float(x.get("start", 0)) - float(y.get("start", 0)))
        de = abs(float(x.get("end", 0)) - float(y.get("end", 0)))
        worst = max(worst, ds, de)
        if ds > tol or de > tol:
            lines.append(f"⚠ [{i}] 시각 어긋남 Δstart {ds:.3f}s · Δend {de:.3f}s"
                         f" — {str(x.get('text',''))[:16]!r}")
    return lines, round(worst, 4)


def events_of(doc) -> list:
    return (doc or {}).get("events") or [] if isinstance(doc, dict) else []


def verdict(checks: dict) -> tuple:
    """회귀 판정. 번역문 CER 은 **판정에서 빠진다**(localize_ab 와 같은 규율)."""
    lines, ok = [], True
    for name, c in checks.items():
        if c.get("advisory"):
            lines.append(f"~~ {name}: {c['summary']}")
            continue
        if c.get("diff"):
            ok = False
            lines.append(f"!! {name}: {c['summary']}")
        else:
            lines.append(f"   {name}: {c['summary']}")
    return ok, lines


# ─────────────────────── 외부 (ffmpeg·파일) ───────────────────────

def _read_json(p: pathlib.Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def loudness(video: pathlib.Path) -> float | None:
    """integrated LUFS. ffmpeg 이 없거나 실패하면 None — 못 재는 것과 다른 것은 다르다.

    ⚠ `"ffmpeg"` 를 박아 쓰지 않는다. 비대화형 SSH 의 PATH 에는 Homebrew 경로가 없어서
    운영 노드에서 조용히 '못 쟀다'로 떨어진다 — E13 곁다리가 고친 바로 그 함정이고,
    라우드니스는 §8-3 판정 항목이라 안 재고 넘어가면 판정이 반쪽이 된다."""
    if not video.exists():
        return None
    try:
        r = subprocess.run(
            [find_ffmpeg_command("ffmpeg"), "-nostats", "-i", str(video),
             "-filter_complex", "ebur128", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600)
    except (OSError, RuntimeError, SystemExit, subprocess.SubprocessError):
        return None
    val = None
    for line in (r.stderr or "").splitlines():
        s = line.strip()
        if s.startswith("I:") and "LUFS" in s:
            try:
                val = float(s.split()[1])
            except (IndexError, ValueError):
                pass
    return val


def duration(video: pathlib.Path) -> float | None:
    if not video.exists():
        return None
    try:
        r = subprocess.run(
            [find_ffmpeg_command("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=120)
        return float((r.stdout or "").strip())
    except (OSError, ValueError, RuntimeError, SystemExit, subprocess.SubprocessError):
        return None


def present(d: pathlib.Path) -> list:
    return [n for n in ARTIFACTS if (d / n).exists()]


def compare(a: pathlib.Path, b: pathlib.Path) -> tuple:
    checks: dict = {}

    # 🛑 거짓 합격 가드 — 산출이 없는데 '차이 없음'을 내면 안 된다(P1 실측 교훈).
    pa, pb = present(a), present(b)
    if not pa or not pb:
        return False, [f"!! 산출물이 없다 — A {pa or '없음'} · B {pb or '없음'}\n"
                       f"   (overlay 산출 디렉토리는 outputs/<video_id> 다. "
                       f"rerender job 디렉토리를 주지 않았는지 확인하라)"], {}
    if set(pa) != set(pb):
        checks["산출 목록"] = {"diff": True,
                             "summary": f"A만 {sorted(set(pa)-set(pb))} · B만 {sorted(set(pb)-set(pa))}"}
    else:
        checks["산출 목록"] = {"diff": False, "summary": f"{len(pa)}개 동일"}

    # ① 원문(한국어) — 회귀 판정 대상
    ea = load_entries(_read_json(a / "translations.json"))
    eb = load_entries(_read_json(b / "translations.json"))
    sd = source_diff(ea, eb)
    checks["원문(OCR·탐지)"] = {
        "diff": bool(sd),
        "summary": (f"{len(sd)}건 변경 — " + " · ".join(sd[:3])) if sd
                   else f"{len(ea)}항목 동일"}

    # ② 번역문 CER — 보고만
    c = target_cer(ea, eb)
    checks["번역문 CER"] = {
        "advisory": True,
        "summary": (f"평균 {c['mean']} · 최대 {c['max']} · 동일 {c['identical']}/{c['n']}"
                    " (LLM 비결정성 — 판정에서 뺀다)") if c["n"] else "비교할 항목 없음"}

    # ③ 세그먼트 정렬 — 회귀 판정 대상
    al, worst = align_diff(events_of(_read_json(a / "ja_events.json")),
                           events_of(_read_json(b / "ja_events.json")))
    checks["세그먼트 정렬"] = {
        "diff": bool(al),
        "summary": (f"{len(al)}건 — " + " · ".join(al[:3])) if al
                   else f"최대 오차 {worst}s (허용 {ALIGN_TOL_SEC}s)"}

    # ④ 최종본 길이·라우드니스
    da, db = duration(a / "final_draft.mp4"), duration(b / "final_draft.mp4")
    if da is None or db is None:
        checks["최종본 길이"] = {"advisory": True,
                       "summary": "⚠ 못 쟀다 — ffprobe 없음 또는 final_draft.mp4 없음"}
    else:
        checks["최종본 길이"] = {"diff": abs(da - db) > DUR_TOL_SEC,
                              "summary": f"{da:.3f}s vs {db:.3f}s"}
    la, lb = loudness(a / "final_draft.mp4"), loudness(b / "final_draft.mp4")
    if la is None or lb is None:
        checks["라우드니스"] = {"advisory": True,
                      "summary": "⚠ 못 쟀다 — ffmpeg ebur128 필요. §8-3 판정 항목이라 이대로면 판정이 반쪽이다(FFMPEG_BIN 확인)"}
    else:
        checks["라우드니스"] = {
            "diff": abs(la - lb) > LUFS_TOL_DB,
            "summary": f"{la:.2f} vs {lb:.2f} LUFS (목표 {TARGET_LUFS} · 허용 ±{LUFS_TOL_DB})"}

    ok, lines = verdict(checks)
    return ok, lines, checks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="overlay 엔진 A/B 대조 (L-P4 회귀 0)")
    ap.add_argument("--a", required=True, help="기준(구 엔진 vlp) outputs/<video_id>")
    ap.add_argument("--b", required=True, help="대조(신 엔진 ai-video) outputs/<video_id>")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    got = compare(pathlib.Path(args.a), pathlib.Path(args.b))
    ok, lines = got[0], got[1]
    checks = got[2] if len(got) > 2 else {}
    if args.json:
        print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print(f"A(구) {args.a}\nB(신) {args.b}\n")
        for ln in lines:
            print(ln)
        print("\n판정: " + ("✅ 회귀 0" if ok else "❌ 회귀 있음"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
