"""3단계 — 소스 인덱스: Gemini 가 창(10분) 단위로 영상을 보고 화자·장면·순간을 채록한다.

산출 `index.json`:
  speakers: {"L-001": "강비호", …}
  scenes:   [{id:"SC-001", start, end, place, summary, chars}]           (절대초)
  moments:  [{id:"S-001", start, end, kind, who, desc, sound}]           (절대초 · 시작순)
  windows:  [{start, end, status, synopsis, lines, moments}]
시각은 모델이 **클립 기준** MM:SS.ms 로 적고 코드가 창 시작을 더한다(긴 절대 시각보다 오차가 작다).
"""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.tikitaka.common import Job, fmt_tc, parse_tc, ms3, find_bin
from app.tikitaka.llm import Gemini
from app.tikitaka.prompts import INDEX_PROMPT

WINDOW_SEC = 600.0
_KINDS = {"reaction", "action", "ambience"}


def make_windows(duration: float, size: float = WINDOW_SEC) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    t = 0.0
    while t < duration - 1.0:
        e = min(duration, t + size)
        # 꼬리가 2분 미만이면 앞 창에 붙인다(너무 짧은 창은 문맥이 없다)
        if duration - e < 120.0:
            e = duration
        out.append((ms3(t), ms3(e)))
        t = e
    return out


def _cut_window_clip(job: Job, proxy: Path, ws: float, we: float, idx: int) -> Path:
    out = job.path("index_windows") / f"win_{idx:02d}.mp4"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_bin("ffmpeg")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{ws:.3f}", "-to", f"{we:.3f}", "-i", str(proxy),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "48k",
                    "-movflags", "+faststart", str(out)], check=True)
    return out


def _lines_block(lines: list[dict], ws: float) -> str:
    rows = []
    for l in lines:
        rows.append(f"{l['id']} [{fmt_tc(l['start']-ws)}~{fmt_tc(l['end']-ws)}] {l['text']}")
    return "\n".join(rows) if rows else "(이 구간에 인식된 대사 없음)"


def parse_window_result(raw: dict, ws: float, we: float, line_ids: set[str]) -> dict:
    """모델 응답(클립 기준) → 절대초. 형식 위반 항목은 버리고 개수를 남긴다(순수 — 테스트 대상)."""
    length = we - ws
    dropped = 0
    speakers = {}
    for k, v in (raw.get("speakers") or {}).items():
        if k in line_ids and isinstance(v, str) and v.strip():
            speakers[k] = v.strip()
    scenes = []
    for s in raw.get("scenes") or []:
        try:
            a, b = parse_tc(s["start"]), parse_tc(s["end"])
        except (KeyError, ValueError, TypeError):
            dropped += 1
            continue
        a, b = max(0.0, min(a, length)), max(0.0, min(b, length))
        if b - a < 0.5:
            dropped += 1
            continue
        scenes.append({"start": ms3(ws + a), "end": ms3(ws + b), "place": str(s.get("place") or ""),
                       "summary": str(s.get("summary") or ""), "chars": [str(c) for c in (s.get("chars") or [])]})
    moments = []
    for m in raw.get("moments") or []:
        try:
            a, b = parse_tc(m["start"]), parse_tc(m["end"])
        except (KeyError, ValueError, TypeError):
            dropped += 1
            continue
        kind = str(m.get("kind") or "").strip()
        if kind not in _KINDS:
            dropped += 1
            continue
        a, b = max(0.0, min(a, length)), max(0.0, min(b, length))
        if b <= a:
            b = min(length, a + 1.0)
        if b - a < 0.3:
            dropped += 1
            continue
        who = m.get("who")
        moments.append({"start": ms3(ws + a), "end": ms3(ws + b), "kind": kind,
                        "who": (str(who).strip() if who else None), "desc": str(m.get("desc") or "")[:60],
                        "sound": (str(m["sound"]).strip() if m.get("sound") else None)})
    return {"speakers": speakers, "scenes": scenes, "moments": moments,
            "synopsis": str(raw.get("synopsis") or ""), "dropped": dropped}


def build_index(job: Job, gemini: Gemini, transcript: dict, proxy: Path, duration: float, *,
                title: str, cast: list[str], workers: int = 3) -> dict:
    if job.has("index.json"):
        return job.load("index.json")
    windows = make_windows(duration)
    lines = transcript["lines"]
    job.log(f"[index] 창 {len(windows)}개 × {WINDOW_SEC:.0f}s · 대사 줄 {len(lines)}")

    def run_one(i: int, ws: float, we: float) -> dict:
        clip = _cut_window_clip(job, proxy, ws, we, i)
        win_lines = [l for l in lines if ws <= l["start"] < we]
        prompt = INDEX_PROMPT.format(title=title, win_start=fmt_tc(ws), win_end=fmt_tc(we),
                                     cast=", ".join(cast) if cast else "(미제공)", lines=_lines_block(win_lines, ws))
        cache = job.path("index_windows") / f"win_{i:02d}.json"
        if cache.exists():
            import json
            raw = json.loads(cache.read_text(encoding="utf-8"))
        else:
            raw = gemini.video_json(prompt, clip, kind=f"index{i:02d}", fps=1.0, thinking="low")
            import json
            cache.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        res = parse_window_result(raw, ws, we, {l["id"] for l in win_lines})
        res.update({"i": i, "start": ws, "end": we, "lines": len(win_lines), "status": "ok"})
        job.log(f"[index] 창{i:02d} {fmt_tc(ws)}~{fmt_tc(we)} 화자 {len(res['speakers'])}/{len(win_lines)} "
                f"장면 {len(res['scenes'])} 순간 {len(res['moments'])} 드롭 {res['dropped']}")
        return res

    results: list[dict] = []
    failed: list[tuple[int, float, float]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_one, i, ws, we): (i, ws, we) for i, (ws, we) in enumerate(windows)}
        for f, spec in futs.items():
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                job.log(f"[index] ⚠ 창{spec[0]:02d} 실패(1차): {e}")
                failed.append(spec)
    for i, ws, we in failed:                       # 2차: 순차 재시도(동시 업로드 부하를 뺀다) — 창이 빠지면 그 20분의 화자·순간이 사라진다
        try:
            results.append(run_one(i, ws, we))
            job.log(f"[index] 창{i:02d} 2차 재시도 성공")
        except Exception as e:  # noqa: BLE001
            job.log(f"[index] ⚠ 창{i:02d} 2차도 실패 — 이 구간은 화자 미상·순간 없음으로 진행: {e}")
    if len(results) < len(windows):
        job.log(f"[index] ⚠⚠ 창 {len(windows) - len(results)}개가 빠진 인덱스 — 대본 품질이 떨어진다. --redo index 로 다시 시도할 것")
    results.sort(key=lambda r: r["i"])
    speakers: dict[str, str] = {}
    scenes: list[dict] = []
    moments: list[dict] = []
    for r in results:
        speakers.update(r["speakers"])
        scenes.extend(r["scenes"])
        moments.extend(r["moments"])
    scenes.sort(key=lambda s: s["start"])
    moments.sort(key=lambda m: (m["start"], m["end"]))
    for k, s in enumerate(scenes, 1):
        s["id"] = f"SC-{k:03d}"
    for k, m in enumerate(moments, 1):
        m["id"] = f"S-{k:03d}"
    for l in lines:
        l["speaker"] = speakers.get(l["id"])
    data = {"title": title, "cast": cast, "speakers": speakers, "scenes": scenes, "moments": moments,
            "windows": [{k: r[k] for k in ("i", "start", "end", "status", "synopsis", "lines", "dropped")} | {"moments": len(r["moments"])} for r in results]}
    job.save("index.json", data)
    job.save("transcript.json", transcript)     # speaker 가 채워진 판으로 갱신
    n_sp = sum(1 for l in lines if l["speaker"])
    job.log(f"[index] 완료 — 화자 배정 {n_sp}/{len(lines)} · 장면 {len(scenes)} · 순간 {len(moments)}")
    job.record_step("index", windows=len(windows), ok=len(results), failed=len(windows) - len(results), speakers=n_sp, lines=len(lines),
                    scenes=len(scenes), moments=len(moments), llm=gemini.usage.calls[-len(results):] if results else [])
    return data
