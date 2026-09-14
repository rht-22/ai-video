"""실험 — "처음부터 agentic 한 번으로" vs 단계형 파이프라인 (2026-09-10, 사용자 요청).

두 arm 을 같은 소재·같은 렌더러로 돌려 **타임코드 정확도와 지침서 준수**를 잰다.
  E1 원샷:   영상 + 두 지침서 요약 → 대본과 편집 테이블(MM:SS.ms)을 한 번에 (전략은 파이프라인 추천 버전과 같은 것으로 고정)
  E2 시각만: 파이프라인 v{n} 대본을 그대로 주고 타임코드·컷만 모델이 영상에서 직접 찾는다
  P  파이프라인: table_v{n}.json 그대로(대조군 — 같은 지표를 같은 코드로 잰다)

지표(코드가 잰다 — 모델 자기평가 없음):
  · S 행 립싱크 오차: 모델 시각 vs whisper 단어 시각(E2 는 줄 ID, E1 은 인용 대사를 전사에 퍼지 매칭). 0.5s 초과 = 싱크 깨짐
  · 인용 대사 환각: 전사에 없는 대사(E1)
  · N 행: 컷 길이 합 vs TTS 실측 길이(제1원칙), 컷 1.0~2.0s 밖 비율(제2원칙), 3s 초과 홀드(슬로우모션/정지 대체)
  · 샷 경계 ±0.1s 마진 위반(제0-2원칙) · 행 간 컷 겹침 · 대사 구간과 겹치는 N 컷
실행: python -m app.tikitaka.experiment --job outputs_tikitaka/포핸즈_1회 --version 3 [--arms e1,e2]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

from app.tikitaka.common import Job, fmt_tc, parse_tc, ms3, load_dotenv_if_any, SAFE_MARGIN_SEC
from app.tikitaka.llm import Gemini
from app.tikitaka.timing import bind_dialogue, narration_plan_sec

GUIDE_REBUILD = """[스크립트 리빌딩 프로토콜 v14 요약]
- 모드 C(롱폼 드라마): 전체 기승전결을 이해한 뒤 핵심 하이라이트만 압축 추출. 원본 대사는 원문 그대로 인용(요약·창작 금지).
- 타임라인 강제 붕괴: 결말/최고조/가장 충격적인 대사를 0~3초에 전진 배치(In medias res). 선형 서사 금지.
- 나노 티키타카: [내레이션]→[대사]→[내레이션]→[대사]→[대사]→[현장음]… 내레이션은 접착제(8~30자), 설명충 금지.
- 예능형 효과자막: 항목마다 필요하면 ≤12자 자막 ([부들부들] (말문 막힘) [갑분싸] [정적...] 등).
- 전략 {strategy_n}({strategy}): {pattern}
- 목표 길이 45~70초."""

GUIDE_TABLE = """[티키타카 편집점 지침서 V14 요약]
- 제0-1 데이터 무결성: [소스 타임코드]+[장면 내용]이 한 세트. 타임코드 없는 행은 폐기. 근사치("대략") 엄금.
- 제0-2 절대 시간: 타임코드는 MM:SS.ms. 컷 경계(장면 전환)에서 ±0.1초 안쪽만 사용(글리치 방지).
- 제1 물리적 시간: 내레이션은 4글자/초. 내레이션 길이가 주(Master)이고 비디오가 그 길이에 맞춘다. 액션은 실제 완료 시간을 100% 보장.
- 제2 다이내믹 컷 분할: 내레이션이 길면 슬로우모션 금지, 정배속 컷 1.0~2.0초를 여러 개 쌓아 채운다.
- 제3 오디오 모드: [N] 내레이션 ON/원본 MUTE, 리액션·상황 컷 교차 / [S] 원본 대사 ON, 화자 립싱크 정확 / [A] 원본 현장음 ON(한숨·문 쾅·정적), 액션 싱크.
- 제4 프레임 정밀: 대사·액션이 시작되는 정확한 시각(초·밀리초)을 적는다. 반올림·버림 금지."""

OUT_SCHEMA = """출력 JSON 하나(코드블록 금지):
{"title": "후킹 제목(≤24자)",
 "rows": [
  {"mode": "N", "text": "내레이션 문장", "effect": "[자막]"|null, "cuts": [{"start": "MM:SS.ms", "end": "MM:SS.ms", "desc": "화면"}, …(2~4개, 각 1.0~2.0초, 합계 = 글자수/4초)]},
  {"mode": "S", "speaker": "화자", "text": "원본 대사 원문 그대로", "effect": null, "cuts": [{"start": "MM:SS.ms", "end": "MM:SS.ms", "desc": "[립싱크]"}]},
  {"mode": "A", "text": "(현장음) (소리 설명)", "effect": null, "cuts": [{"start": "MM:SS.ms", "end": "MM:SS.ms", "desc": "[액션] 동작"}]}
 ]}
모든 start/end 는 첨부 영상의 절대 시각이다. 화면·소리에 실제로 있는 것만 적는다."""

E1_PROMPT = """너는 '유니버설 비선형 편집 아키텍트' 겸 '마스터 에디팅 아키텍트'다. 첨부 영상은 드라마 「{title}」 {episode} 원본 전체다.
아래 두 지침서를 따라 **쇼츠 1편의 대본과 마스터 편집 테이블을 한 번에** 작성하라. 영상을 직접 탐색해 장면·대사·소리를 찾고,
행마다 절대 타임코드(MM:SS.ms)를 적는다.

{guide_rebuild}

{guide_table}

{out_schema}
"""

E2_PROMPT = """너는 '마스터 에디팅 아키텍트'다. 첨부 영상은 드라마 「{title}」 {episode} 원본 전체다.
아래 대본(이미 확정)의 각 행에 **영상에서 직접 찾은 절대 타임코드**를 붙여라. 대본 문구는 바꾸지 않는다.
- [S] 행: 그 대사가 실제로 발화되는 구간(첫 음절 직전 ~ 마지막 음절 직후)을 찾아 립싱크가 맞게 적는다.
- [A] 행: 그 소리가 실제로 나는 동작 구간(0.8~3.0초)을 찾는다.
- [N] 행: 내레이션 글자수/4초 만큼을 정배속 컷 2~4개(각 1.0~2.0초)로 채운다. 그 내레이션의 문맥(앞뒤 대사 장면) 근처 리액션·상황 컷.

{guide_table}

[대본]
{script}

{out_schema}
"""

E3_PROMPT = """# 📜 티키타카 스크립트 리빌딩 — 절충안(인덱스 텍스트 + 영상 확인)

## [System Role]
너는 '유니버설 비선형 편집 아키텍트'다. 첨부 영상은 작품 「{title}」 {episode} 원본 전체(360p·1fps 프록시)이고,
아래 [소스 스크립트]는 그 영상을 미리 채록한 텍스트(장면·화자 붙은 대사·시청각 순간)다.
**텍스트로 전체 이야기를 잡고, 고르려는 후보 장면은 영상에서 직접 확인한 뒤** 대본을 짠다 — 표정·동작·분위기·대사 없는
시각 비트(피 묻은 손, 굳은 얼굴, 정적)는 텍스트 요약(60자)에 다 담기지 않으므로 반드시 눈으로 본다.

## 규칙(리빌딩 프로토콜 v14 · 모드 C)
- 원본 대사는 소스 스크립트의 줄(L-xxx)에서만 인용(ID 로만 가리킨다 — 텍스트를 적지 않는다). 리액션/현장음은 순간(S-xxx) ID.
- 타임라인 강제 붕괴: 가장 강한 대사를 0~3초에 전진 배치(In medias res). 내레이션은 접착제(8~30자), 설명충 금지.
- 전략 {strategy_n}({strategy}): {pattern}
- 항목마다 필요하면 effect(≤12자). 합계 {target_min}~{target_max}초(N=글자수/4초, S=줄 길이, A=순간 길이).
- 영상에서 확인한 시각 비트를 N 내레이션 문장이나 A 현장음으로 살려라. 확인하지 않은 장면은 쓰지 마라.
- 제목·내레이션의 **신체 동작 묘사는 화면에 실제로 있는 것만**("침 뱉고"·"뺨 때리는" 같은 비유적 동작 금지 — 시청자가 화면과 대조한다).

## 항목 어휘
{{"type":"N","text":"…","effect":"[…]"|null}} · {{"type":"S","line_ids":["L-045","L-046"],"effect":…}} · {{"type":"A","moment_id":"S-012","effect":…}}

## 출력 JSON 하나(코드블록 금지)
{{"n": {strategy_n}, "strategy": "{strategy}", "title": "후킹 제목(≤24자)", "structure": "…",
 "looked_at": [{{"start": "MM:SS.ms", "end": "MM:SS.ms", "why": "무엇을 확인했나"}}, …],
 "items": [ … 10~18개 … ],
 "analysis": {{"grade": "매우 안전|안전|보통", "viral_point": "…", "comment": "…"}}}}

## 소스 스크립트
{script}
"""

_NORM = re.compile(r"[\s\.\,\!\?…~\"'“”‘’\-]+")


def _norm(t: str) -> str:
    return _NORM.sub("", str(t or ""))


def parse_rows(raw: dict, duration: float) -> tuple[list[dict], list[str]]:
    """모델 테이블 → 행(절대초). 형식 위반은 버리고 기록."""
    rows: list[dict] = []
    issues: list[str] = []
    for k, r in enumerate(raw.get("rows") or [], 1):
        mode = str(r.get("mode") or "").upper()
        if mode not in ("N", "S", "A"):
            issues.append(f"행{k}: 모르는 mode {mode!r}")
            continue
        cuts = []
        for c in r.get("cuts") or []:
            try:
                a, b = parse_tc(c["start"]), parse_tc(c["end"])
            except (KeyError, ValueError, TypeError):
                issues.append(f"행{k}: 타임코드 형식 위반 {c}")
                continue
            if not (0 <= a < b <= duration + 0.5):
                issues.append(f"행{k}: 범위 밖 {fmt_tc(a)}~{fmt_tc(b)}")
                continue
            cuts.append({"src": f"M-{k}", "in": ms3(a), "out": ms3(b), "dur": ms3(b - a), "desc": str(c.get("desc") or "")[:60]})
        if not cuts:
            issues.append(f"행{k}: 컷 없음 → 폐기(제0-1원칙)")
            continue
        text = " ".join(str(r.get("text") or "").split())
        rows.append({"i": len(rows) + 1, "mode": mode, "text": text, "speaker": (r.get("speaker") or None),
                     "effect": (str(r["effect"]).strip()[:16] if r.get("effect") else None), "cuts": cuts,
                     "dur": ms3(sum(c["dur"] for c in cuts))})
    return rows, issues


def match_line(text: str, transcript: dict) -> tuple[dict | None, float]:
    """인용 대사 → 가장 비슷한 전사 줄(정규화 후 유사도). 0.55 미만이면 미매칭(환각/각색)."""
    q = _norm(text)
    if not q:
        return None, 0.0
    best, score = None, 0.0
    for l in transcript["lines"]:
        s = difflib.SequenceMatcher(None, q, _norm(l["text"])).ratio()
        if s > score:
            best, score = l, s
    # 여러 줄에 걸친 인용: 최고 줄 앞뒤로 이어 붙여 다시 비교
    if best and score < 0.9:
        lines = transcript["lines"]
        k = lines.index(best)
        for span in (2, 3):
            for off in range(-span + 1, 1):
                seg = lines[max(0, k + off): max(0, k + off) + span]
                s = difflib.SequenceMatcher(None, q, _norm(" ".join(x["text"] for x in seg))).ratio()
                if s > score:
                    score = s
                    best = {"id": "+".join(x["id"] for x in seg), "start": seg[0]["start"], "end": seg[-1]["end"],
                            "text": " ".join(x["text"] for x in seg), "word_i": [i for x in seg for i in x["word_i"]], "speaker": seg[0].get("speaker")}
    return (best if score >= 0.55 else None), round(score, 3)


def measure(rows: list[dict], transcript: dict, cuts: list[float], tts_len: dict[int, float] | None = None) -> dict:
    """지침서 준수 지표 — 세 arm 에 같은 코드로 적용."""
    words = transcript["words"]
    lines_by_id = {l["id"]: l for l in transcript["lines"]}
    s_err: list[dict] = []
    hallucinated = 0
    for r in rows:
        if r["mode"] != "S":
            continue
        if r.get("line_ids"):
            true = bind_dialogue(lines_by_id, words, r["line_ids"])
            score = 1.0
        else:
            m, score = match_line(r["text"], transcript)
            if not m:
                hallucinated += 1
                s_err.append({"i": r["i"], "text": r["text"][:30], "matched": None, "score": score})
                continue
            true = bind_dialogue({m["id"]: m}, words, [m["id"]])
        c = r["cuts"][0]
        s_err.append({"i": r["i"], "text": r["text"][:30], "matched": True, "score": score,
                      "start_err": ms3(c["in"] - true["start"]), "end_err": ms3(c["out"] - true["end"]),
                      "true": f"{fmt_tc(true['start'])}~{fmt_tc(true['end'])}", "model": f"{fmt_tc(c['in'])}~{fmt_tc(c['out'])}"})
    matched = [e for e in s_err if e.get("matched")]
    n_rows = [r for r in rows if r["mode"] == "N"]
    n_cuts = [c for r in n_rows for c in r["cuts"]]
    sum_vs_tts = []
    for r in n_rows:
        target = (tts_len or {}).get(r["i"]) or narration_plan_sec(r["text"])
        sum_vs_tts.append(ms3(r["dur"] - target))
    margin_viol = sum(1 for r in rows for c in r["cuts"] for b in (c["in"], c["out"])
                      if any(0 < abs(b - sc) < SAFE_MARGIN_SEC - 1e-6 for sc in cuts))
    all_cuts = [(r["i"], c) for r in rows for c in r["cuts"]]
    overlaps = sum(1 for x in range(len(all_cuts)) for y in range(x + 1, len(all_cuts))
                   if min(all_cuts[x][1]["out"], all_cuts[y][1]["out"]) - max(all_cuts[x][1]["in"], all_cuts[y][1]["in"]) > 0.001)
    dialog_iv = [(r["cuts"][0]["in"], r["cuts"][0]["out"]) for r in rows if r["mode"] == "S"]
    n_over_dialog = sum(1 for c in n_cuts if any(c["in"] < e and c["out"] > s for s, e in dialog_iv))
    return {
        "rows": len(rows), "S": sum(r["mode"] == "S" for r in rows), "N": len(n_rows), "A": sum(r["mode"] == "A" for r in rows),
        "total_sec": ms3(sum(r["dur"] for r in rows)),
        "s_matched": len(matched), "s_hallucinated": hallucinated,
        "s_start_err_mean": ms3(sum(abs(e["start_err"]) for e in matched) / len(matched)) if matched else None,
        "s_start_err_max": ms3(max(abs(e["start_err"]) for e in matched)) if matched else None,
        "s_sync_broken_over_0_5s": sum(1 for e in matched if abs(e["start_err"]) > 0.5 or abs(e["end_err"]) > 0.5),
        "n_cuts": len(n_cuts), "n_cut_outside_1_2s": sum(1 for c in n_cuts if not (1.0 - 1e-6 <= c["dur"] <= 2.0 + 1e-6)),
        "n_hold_over_3s": sum(1 for c in n_cuts if c["dur"] > 3.0),
        "n_sum_minus_tts_mean_abs": ms3(sum(abs(x) for x in sum_vs_tts) / len(sum_vs_tts)) if sum_vs_tts else None,
        "n_sum_minus_tts_max_abs": ms3(max(abs(x) for x in sum_vs_tts)) if sum_vs_tts else None,
        "margin_violations": margin_viol, "cut_overlaps": overlaps, "n_cuts_over_dialogue": n_over_dialog,
        "s_detail": s_err,
    }


def to_table(rows: list[dict], *, n: int, title: str, tts_fn) -> dict:
    """모델 테이블 → 렌더러가 먹는 table dict (N 행 TTS 합성 · t0 배치). 컷은 모델이 적은 그대로 — 그게 실험이다."""
    t = 0.0
    for r in rows:
        if r["mode"] == "N":
            path, sec = tts_fn(r["text"])
            r["tts"] = str(path)
            r["tts_dur"] = sec
        r["t0"] = ms3(t)
        r["dur_video"] = ms3(sum(c["dur"] for c in r["cuts"]))
        if r["mode"] == "N":
            r["dur"] = r["dur_video"]          # 렌더 길이 = 모델 컷 합(TTS 는 그 길이로 잘린다)
        t += r["dur_video"]
    return {"version": {"n": n, "strategy": "experiment", "title": title, "structure": "", "analysis": {}}, "rows": rows,
            "total_sec": ms3(t), "voice": "ko_female", "speed": "normal", "cut_search": "oneshot", "agentic": {}, "notes": []}


def _pipeline_rows_for_measure(table: dict, rebuild_version: dict) -> list[dict]:
    """파이프라인 table_v{n}.json → 같은 지표 코드에 넣을 행(S 행에 line_ids 를 실어 whisper 정본으로 잰다)."""
    rows = []
    for r in table["rows"]:
        rr = {"i": r["i"], "mode": r["mode"], "text": r["text"], "cuts": r["cuts"], "dur": r.get("dur_video") if r["mode"] == "N" else r["dur"]}
        if r["mode"] == "S":
            rr["line_ids"] = r["src"]
        rows.append(rr)
    return rows


def report_md(title: str, n: int, results: dict[str, dict], issues: dict[str, list[str]], metas: dict[str, dict]) -> str:
    keys = [("rows", "행 수"), ("S", "S 행"), ("N", "N 행"), ("A", "A 행"), ("total_sec", "총 길이(s)"),
            ("s_matched", "S 인용 매칭"), ("s_hallucinated", "S 전사에 없는 대사"), ("s_start_err_mean", "S 시작 오차 평균(s)"),
            ("s_start_err_max", "S 시작 오차 최대(s)"), ("s_sync_broken_over_0_5s", "S 싱크 깨짐(>0.5s)"),
            ("n_cuts", "N 컷 수"), ("n_cut_outside_1_2s", "N 컷 1~2s 밖"), ("n_hold_over_3s", "N 컷 3s 초과 홀드"),
            ("n_sum_minus_tts_mean_abs", "N 컷합−TTS 평균|Δ|(s)"), ("n_sum_minus_tts_max_abs", "N 컷합−TTS 최대|Δ|(s)"),
            ("margin_violations", "샷 경계 ±0.1s 위반"), ("cut_overlaps", "행 간 컷 겹침"), ("n_cuts_over_dialogue", "N 컷이 대사 구간 침범")]
    arms = list(results.keys())
    out = [f"# 실험 — 원샷 agentic vs 단계형 파이프라인 · 「{title}」 v{n}\n",
           "| 지표 | " + " | ".join(arms) + " |", "| :-- | " + " | ".join(":--" for _ in arms) + " |"]
    for k, label in keys:
        out.append(f"| {label} | " + " | ".join(str(results[a].get(k)) for a in arms) + " |")
    out.append("")
    for a in arms:
        m = metas.get(a) or {}
        if m:
            out.append(f"- **{a}** 호출: processing_call {m.get('processing_calls')} · 총 토큰 {m.get('total_tokens')} · {m.get('sec')}s"
                       + (" · JSON 복구됨" if m.get("repaired") else ""))
        if issues.get(a):
            out.append(f"- **{a}** 형식 위반: " + " / ".join(issues[a][:8]))
    out.append("\n## S 행 립싱크 상세\n")
    for a in arms:
        out.append(f"### {a}")
        out.append("| 행 | 대사 | 매칭(유사도) | 모델 시각 | 정본(whisper) | 시작 오차 | 끝 오차 |")
        out.append("| :-- | :-- | :-- | :-- | :-- | :-- | :-- |")
        for e in results[a]["s_detail"]:
            if e.get("matched"):
                out.append(f"| {e['i']} | {e['text']} | ✓ {e['score']} | {e['model']} | {e['true']} | {e['start_err']:+.2f} | {e['end_err']:+.2f} |")
            else:
                out.append(f"| {e['i']} | {e['text']} | ✗ 전사에 없음({e['score']}) | | | | |")
        out.append("")
    return "\n".join(out) + "\n"


def run_e3(job, gemini, a, version, index, transcript, cuts, duration, proxy, results, issues, metas, exp, patterns) -> None:
    """E3 절충안: 리빌딩(4단계)에 인덱스 텍스트 + 영상(agentic)을 함께 주고 ID 로만 대본을 받는다.
    산출 대본은 파이프라인의 5·6단계를 **그대로** 지난다 — 시각 확정·컷 검색·렌더는 P 와 같은 코드."""
    from app.tikitaka import table as TB, render as RD, report as RP
    from app.tikitaka.rebuild import validate_versions, source_script, TARGET_MIN_SEC, TARGET_MAX_SEC
    label = "E3 절충안"
    e3dir = exp / "e3"
    e3dir.mkdir(exist_ok=True)
    cache = exp / f"e3_raw_v{a.version}.json"
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        raw, meta = saved["raw"], dict(saved["meta"], cached=True)
        job.log("[exp/e3] 캐시 재사용")
    else:
        script = source_script(index, transcript)
        prompt = E3_PROMPT.format(title=a.title, episode=a.episode, strategy_n=a.version, strategy=version["strategy"],
                                  pattern=patterns.get(a.version, ""), target_min=TARGET_MIN_SEC, target_max=TARGET_MAX_SEC, script=script)
        raw, meta = gemini.agentic_video_json(prompt, proxy, kind="exp_e3", upload_cache=job.path("files_cache.json"), max_output_tokens=16384)
        cache.write_text(json.dumps({"meta": meta, "raw": raw}, ensure_ascii=False, indent=1), encoding="utf-8")
    rb = validate_versions({"versions": [dict(raw, n=a.version)], "recommended": a.version, "reason": "e3"}, index, transcript)
    v3 = rb["versions"][0]
    job.log(f"[exp/e3] 대본 항목 {len(v3['items'])} · 계획 {v3['plan_sec']:.1f}s · 이슈 {len(v3['issues'])} · 제목 {v3['title']!r} · looked_at {len(raw.get('looked_at') or [])}")
    (e3dir / "rebuild.json").write_text(json.dumps(rb, ensure_ascii=False, indent=1), encoding="utf-8")
    (e3dir / "rebuild_versions.md").write_text(RP.versions_md(rb, title=a.title), encoding="utf-8")
    # 5·6단계: 별도 잡 디렉토리(테이블·TTS·렌더 산출 분리) — 업로드 캐시는 본 잡 것을 공유
    sub = Job(source=job.source, out_dir=e3dir, title=job.title)
    fc = job.path("files_cache.json")
    if fc.exists() and not sub.path("files_cache.json").exists():
        sub.path("files_cache.json").write_text(fc.read_text(encoding="utf-8"), encoding="utf-8")
    tbl = TB.build_table(sub, gemini, rb, index, transcript, cuts, duration, proxy, version_n=a.version, title=a.title,
                         cut_search="agentic", voice="ko_female", speed="normal")
    sub.path(f"master_table_v{a.version}.md").write_text(RP.table_md(tbl, title=a.title), encoding="utf-8")
    out = RD.render(sub, tbl, title=tbl["version"]["title"], out_name=f"shorts_v{a.version}_e3.mp4")
    job.log(f"[exp/e3] 렌더 → {out}")
    tts_len = {r["i"]: r["dur"] for r in tbl["rows"] if r["mode"] == "N"}
    results[label] = measure(_pipeline_rows_for_measure(tbl, v3), transcript, cuts, tts_len)
    results[label]["story"] = {"title": v3["title"], "structure": v3["structure"], "items": len(v3["items"]),
                               "modes": "".join(it["type"] for it in v3["items"]),
                               "line_ids": [i for it in v3["items"] if it["type"] == "S" for i in it["line_ids"]],
                               "looked_at": raw.get("looked_at") or []}
    issues[label] = v3["issues"]
    metas[label] = meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, type=Path)
    ap.add_argument("--version", type=int, default=3)
    ap.add_argument("--arms", default="e1,e2")
    ap.add_argument("--title", default="포핸즈")
    ap.add_argument("--episode", default="1회")
    ap.add_argument("--render", action="store_true", default=True)
    a = ap.parse_args(argv)
    load_dotenv_if_any()
    from app.tikitaka import render as RD
    from app.tikitaka.table import _tts_cached
    from app.tikitaka.rebuild import STRATEGIES

    probe = json.loads((a.job / "probe.json").read_text(encoding="utf-8"))
    job = Job(source=Path(json.loads((a.job / "run_log.json").read_text(encoding="utf-8"))["source"]), out_dir=a.job, title=a.title)
    transcript = job.load("transcript.json")
    index = job.load("index.json")
    cuts = job.load("scenecuts.json")["cuts"]
    rebuild = job.load("rebuild.json")
    version = next(v for v in rebuild["versions"] if v["n"] == a.version)
    table = job.load(f"table_v{a.version}.json")
    exp = a.job / "experiment"
    exp.mkdir(exist_ok=True)
    gemini = Gemini(log=job.log)
    proxy = job.path("scan_360p_1fps.mp4")
    duration = probe["duration_sec"]
    tts_fn = lambda text: _tts_cached(job, text, "ko_female", "normal")  # noqa: E731

    patterns = {1: "[결말/최고조 대사]→[발단]→[전개]→[위기]", 2: "[결정적 폭로]→[주변인 경악]→[전말(과거)]→[결말]",
                3: "[가장 분노/오열/웃는 대사]→[왜 이렇게 됐는지]→[결말]", 4: "[평온한 대사]→[0.1초 만에 파국]→[발단]",
                5: "[의문의 한마디]→[내레이션 추리]→[진실 폭로]", 6: "[주변인 황당 리액션]→[갈등]→[일침]",
                7: "[파국 직전 대사]→[내레이션 'X시간 전']→[점층 고조]", 8: "[A 주장]→[B 반박]→[내레이션]→[팩트 폭로]",
                9: "[빌런 발언]→[사이다 일침]→[당황 리액션]", 10: "[파국 결말]→[내레이션 '이 말을 안 했다면?']→[결정적 말실수]→[나비효과]"}
    results: dict[str, dict] = {}
    issues: dict[str, list[str]] = {}
    metas: dict[str, dict] = {}

    # P — 파이프라인 대조군
    tts_len = {r["i"]: r["dur"] for r in table["rows"] if r["mode"] == "N"}
    results["P 파이프라인"] = measure(_pipeline_rows_for_measure(table, version), transcript, cuts, tts_len)

    for arm in [x.strip() for x in a.arms.split(",") if x.strip()]:
        if arm == "e3":
            run_e3(job, gemini, a, version, index, transcript, cuts, duration, proxy, results, issues, metas, exp, patterns)
            continue
        cache = exp / f"{arm}_raw_v{a.version}.json"
        if cache.exists():
            saved = json.loads(cache.read_text(encoding="utf-8"))
            raw, meta = saved["raw"], dict(saved["meta"], cached=True)
            job.log(f"[exp/{arm}] 캐시 재사용")
        else:
            if arm == "e1":
                prompt = E1_PROMPT.format(title=a.title, episode=a.episode,
                                          guide_rebuild=GUIDE_REBUILD.format(strategy_n=a.version, strategy=version["strategy"],
                                                                             pattern=patterns.get(a.version, "")),
                                          guide_table=GUIDE_TABLE, out_schema=OUT_SCHEMA)
            else:
                lines = []
                for k, it in enumerate(version["items"], 1):
                    if it["type"] == "N":
                        lines.append(f"{k}. [N] \"{it['text']}\" (효과자막 {it.get('effect')}) — 필요 길이 {narration_plan_sec(it['text']):.1f}s")
                    elif it["type"] == "S":
                        lines.append(f"{k}. [S] {it.get('speaker')}: \"{it['text']}\" (효과자막 {it.get('effect')})")
                    else:
                        lines.append(f"{k}. [A] (현장음) {it.get('sound') or it['desc']} — {it.get('who') or ''} {it['desc']}")
                prompt = E2_PROMPT.format(title=a.title, episode=a.episode, guide_table=GUIDE_TABLE, script="\n".join(lines), out_schema=OUT_SCHEMA)
            raw, meta = gemini.agentic_video_json(prompt, proxy, kind=f"exp_{arm}", upload_cache=job.path("files_cache.json"),
                                                  max_output_tokens=16384)
            cache.write_text(json.dumps({"meta": meta, "raw": raw}, ensure_ascii=False, indent=1), encoding="utf-8")
        rows, iss = parse_rows(raw, duration)
        if arm == "e2":                      # 같은 대본이므로 S 행에 줄 ID 를 실어 whisper 정본으로 잰다(순서 매칭)
            s_items = [it for it in version["items"] if it["type"] == "S"]
            s_rows = [r for r in rows if r["mode"] == "S"]
            for r, it in zip(s_rows, s_items):
                r["line_ids"] = it["line_ids"]
        results[f"{arm.upper()} " + ("원샷" if arm == "e1" else "시각만")] = measure(rows, transcript, cuts,
            {r["i"]: tts_fn(r["text"])[1] for r in rows if r["mode"] == "N"})
        issues[f"{arm.upper()} " + ("원샷" if arm == "e1" else "시각만")] = iss
        metas[f"{arm.upper()} " + ("원샷" if arm == "e1" else "시각만")] = meta
        job.log(f"[exp/{arm}] 행 {len(rows)} · 위반 {len(iss)} · 제목 {raw.get('title')!r}")
        (exp / f"{arm}_rows_v{a.version}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        if a.render and rows:
            tbl = to_table([dict(r) for r in rows], n=a.version, title=str(raw.get("title") or version["title"]), tts_fn=tts_fn)
            try:
                out = RD.render(job, tbl, title=tbl["version"]["title"], out_name=f"experiment/shorts_v{a.version}_{arm}.mp4")
                job.log(f"[exp/{arm}] 렌더 → {out}")
            except Exception as e:  # noqa: BLE001
                job.log(f"[exp/{arm}] ⚠ 렌더 실패: {str(e)[:300]}")
    prev = exp / f"metrics_v{a.version}.json"
    if prev.exists():                                    # 앞선 arm 결과와 합쳐 한 표로
        old_m = json.loads(prev.read_text(encoding="utf-8"))
        for k, v in old_m.get("results", {}).items():
            results.setdefault(k, v)
        for k, v in old_m.get("issues", {}).items():
            issues.setdefault(k, v)
        for k, v in old_m.get("metas", {}).items():
            metas.setdefault(k, v)
    md = report_md(a.title, a.version, results, issues, metas)
    for k, r in results.items():
        st = r.get("story")
        if st:
            md += f"\n## {k} — 대본\n\n제목 **{st['title']}** · 구조 {st['structure']} · 항목 {st['items']} ({st['modes']})  \n인용 줄 {', '.join(st['line_ids'])}\n\n확인한 구간(모델 자기 보고):\n" + \
                  "\n".join(f"- {x.get('start')}~{x.get('end')} — {x.get('why')}" for x in st["looked_at"]) + "\n"
    (exp / f"report_v{a.version}.md").write_text(md, encoding="utf-8")
    (exp / f"metrics_v{a.version}.json").write_text(json.dumps({"results": results, "issues": issues, "metas": metas}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
