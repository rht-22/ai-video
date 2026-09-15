"""1.5단계 — 전사 글자 교정(방법 1). 시각은 STT 그대로, **글자만** Gemini 가 소리를 듣고 고친다.

10분 창의 오디오(aac 모노)를 Files API 로 올리고 그 창의 줄(ID·텍스트)을 주면, 잘못 들은 단어만 고친 줄을 돌려받는다
(실측 오류: 입시장산→입시 장사, 찌리니까→찔리니까, 목사판→복사판). 가드: 고친 줄은 원문과 유사도 ≥0.5 · 길이 비 0.6~1.6
— 문장을 다시 쓰면 버린다(기록). 단어 시각(word_i)은 손대지 않으므로 립싱크 바인딩은 그대로다.
산출: transcript.json 의 줄 `text` 교체 + `text_orig` 보존 + `polished: true`, 감사 기록 `transcript_polish.json`.
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
from pathlib import Path

from app.tikitaka.common import Job, fmt_tc, find_bin
from app.tikitaka.llm import Gemini
from app.tikitaka.grid import fingerprint, source_identity

SCHEMA = "transcript_polish/research_v2"
WINDOW_SEC = 600.0
_NORM = re.compile(r"[\s\.\,\!\?…~\"'“”‘’\-]+")

POLISH_PROMPT = """너는 한국어 드라마 전사 교정자다. 첨부 오디오는 작품 「{title}」의 {win_start}~{win_end} 구간이고, 아래 [전사 줄]은
음성 인식 결과다(줄 ID · 클립 기준 타임코드 · 텍스트). 등장인물: {cast}

[할 일] 오디오를 듣고 **잘못 들은 단어만** 고쳐라. 문장을 다시 쓰거나 줄이거나 늘리지 마라 — 같은 자리의 단어를 맞는 단어로
바꾸는 것만 허용된다(예: "입시장산하고" → "입시 장사하고", "목사판" → "복사판", "찌리니까" → "찔리니까"). 띄어쓰기·맞춤법도 고친다.
확신이 없으면 그대로 둔다. 고친 줄만 출력한다.

[작품·인물 리서치]
{research_context}

인명은 오디오·앞뒤 대사와 리서치의 극중 이름을 함께 대조하라. 비슷하게 들리는 오인식은 정식 이름으로
교정하되, 이름 목록에 없다는 이유만으로 강제 치환하지 마라. 별명·가명·다른 인물은 원문을 유지한다.
극중 이름을 배우 이름으로 바꾸지 마라. 리서치의 줄거리나 설명을 대사에 추가하지 마라.

[전사 줄]
{lines}

출력 JSON 하나(코드블록 금지): {{"corrections": {{"L-045": "고친 문장", …}}}}  (고친 줄이 없으면 빈 객체)
"""


def _norm(t: str) -> str:
    return _NORM.sub("", str(t or ""))


def accept_correction(orig: str, new: str, *, min_sim: float = 0.5, len_lo: float = 0.6, len_hi: float = 1.6) -> tuple[bool, str]:
    """교정 수용 규칙 — 순수, 테스트 대상. (수용?, 사유)"""
    o, n = _norm(orig), _norm(new)
    if not n:
        return False, "빈 문장"
    if o == n:
        return False, "변화 없음"
    ratio = len(n) / max(1, len(o))
    if not (len_lo <= ratio <= len_hi):
        return False, f"길이 비 {ratio:.2f}"
    sim = difflib.SequenceMatcher(None, o, n).ratio()
    if sim < min_sim:
        return False, f"유사도 {sim:.2f}"
    return True, f"유사도 {sim:.2f}"


def _window_audio(job: Job, wav: Path, t0: float, t1: float, k: int) -> Path:
    out = job.path("polish_windows") / f"win_{k:02d}.m4a"
    if out.exists():
        return out
    out.parent.mkdir(exist_ok=True)
    ffmpeg = find_bin("ffmpeg")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1-t0:.3f}", "-i", str(wav),
                    "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", str(out)], check=True)
    return out


def polish_transcript(job: Job, gemini: Gemini, transcript: dict, wav: Path, duration: float, *, title: str, cast: list[str], research_context: str = "") -> dict:
    original_lines = [{**l, "text": l.get("text_orig", l["text"])} for l in transcript["lines"]]
    identity = fingerprint([SCHEMA, POLISH_PROMPT, title, cast, research_context, duration,
                            source_identity(job.source),
                            [(l["id"], l["start"], l["end"], l["text"]) for l in original_lines]])
    if transcript.get("polished") and transcript.get("polish", {}).get("fingerprint") == identity:
        return transcript
    previous = {l["id"]: l["text"] for l in transcript["lines"]}
    if transcript.get("polished"):
        job.log("[polish] 리서치·인물·교정 입력 변경 → 기존 교정 캐시 재평가")
    lines = transcript["lines"]
    for l in lines:                        # 재실행(--redo polish) 이면 원문으로 되돌리고 시작
        if l.get("text_orig"):
            l["text"] = l["text_orig"]
    audit = {"schema": SCHEMA, "fingerprint": identity, "research_context": research_context,
             "cast": cast, "windows": [], "applied": [], "rejected": []}
    t = 0.0
    k = 0
    while t < duration - 0.5:
        e = min(duration, t + WINDOW_SEC)
        if duration - e < 60.0:
            e = duration
        win_lines = [l for l in lines if t <= l["start"] < e]
        if win_lines:
            cache = job.path("polish_windows") / f"win_{k:02d}_{identity}.json"
            if cache.exists():
                raw = json.loads(cache.read_text(encoding="utf-8"))
            else:
                audio = _window_audio(job, wav, t, e, k)
                block = "\n".join(f"{l['id']} [{fmt_tc(l['start']-t)}~{fmt_tc(l['end']-t)}] {l['text']}" for l in win_lines)
                prompt = POLISH_PROMPT.format(title=title, win_start=fmt_tc(t), win_end=fmt_tc(e), cast=", ".join(cast) if cast else "(미제공)", lines=block, research_context=research_context or "(미제공)")
                try:
                    raw = gemini.media_json(prompt, audio, mime="audio/mp4", kind=f"polish{k:02d}", thinking="low")
                except Exception as exc:  # noqa: BLE001 — 교정은 보강이라 창 하나가 실패해도 전사 원문으로 진행한다(기록)
                    job.log(f"[polish] ⚠ 창{k:02d} 교정 실패 → 원문 유지: {type(exc).__name__}: {str(exc)[:160]}")
                    audit["windows"].append({"k": k, "start": t, "end": e, "lines": len(win_lines), "applied": 0, "rejected": 0,
                                             "error": str(exc)[:200]})
                    t = e
                    k += 1
                    continue
                cache.parent.mkdir(exist_ok=True)
                cache.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
            corr = raw.get("corrections") or {}
            by_id = {l["id"]: l for l in win_lines}
            n_ok = n_no = 0
            for lid, new in corr.items():
                l = by_id.get(lid)
                if not l or not isinstance(new, str):
                    continue
                ok, why = accept_correction(l["text"], new)
                if ok:
                    l["text_orig"] = l["text"]
                    l["text"] = " ".join(new.split())
                    audit["applied"].append({"id": lid, "orig": l["text_orig"], "new": l["text"], "why": why})
                    n_ok += 1
                elif why != "변화 없음":
                    audit["rejected"].append({"id": lid, "orig": l["text"], "new": new, "why": why})
                    n_no += 1
            audit["windows"].append({"k": k, "start": t, "end": e, "lines": len(win_lines), "applied": n_ok, "rejected": n_no})
            job.log(f"[polish] 창{k:02d} {fmt_tc(t)}~{fmt_tc(e)} 줄 {len(win_lines)} · 교정 {n_ok} · 기각 {n_no}")
        t = e
        k += 1
    transcript["polished"] = not any(w.get("error") for w in audit["windows"])
    transcript["polish"] = {"fingerprint": identity, "applied": len(audit["applied"]), "rejected": len(audit["rejected"])}
    changed = [l["id"] for l in lines if previous[l["id"]] != l["text"]]
    if changed:
        job.save("transcript_dependents_dirty.json", {"fingerprint": identity, "changed_line_ids": changed})
    job.save("transcript.json", transcript)
    job.save("transcript_polish.json", audit)
    for a in audit["applied"][:12]:
        job.log(f"[polish]   {a['id']}: {a['orig']!r} → {a['new']!r}")
    job.log(f"[polish] 완료 — 교정 {len(audit['applied'])}줄 · 기각 {len(audit['rejected'])}줄 (전체 {len(lines)}줄)")
    job.record_step("transcript_polish", applied=len(audit["applied"]), rejected=len(audit["rejected"]), lines=len(lines),
                    llm=gemini.usage.calls[-len(audit["windows"]):] if audit["windows"] else [])
    return transcript
