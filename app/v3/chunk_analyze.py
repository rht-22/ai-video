"""Stage 2 — chunk_analyze. v3 에서 "영상을 보는" 마지막 단계.

chunk 당 Pro 1회가 span 후보들을 meaning 으로 **묶는다**. 모델은 시각을 아예 출력하지
않는다 — meaning 은 span id 구간(first_span~last_span)으로만 제안하고, 확정 시각은
전부 grid 의 span 경계에서 lookup 한다(시각 정합 100% 는 검증이 아니라 구조다).
발주서의 "span 경계 밖 시각 반려"는 이 표현에서 "모르는/비연속 span id 반려"가 된다.

코드 검증 3종(발주서 §B — 전부 run_log 에 수치로):
  ① 시각 정합 — 산출 문서의 모든 meaning/span 시각이 grid 유래인지 100% 재확인(벨트).
  ② 전사 diff — audio_script 는 전사가 정본. 모델 제안(화자 배정·정정)과 전사의
     정규화 편집거리가 TRANSCRIPT_DIFF_MAX 를 넘으면 전사로 복원 + 건별 로그
     (모델의 대본 각색 금지 — 고유명사·간투사 정정은 통과한다).
  ③ 인물 교차 — 같은 ArcFace 클러스터 라벨이 등장하는 span 들에 같은 인물명이
     배정됐는가(일관성 비율). 경고 집계지 차단이 아니다. 인덱스 부재는 커버리지 표기.

부분 실패 chunk 는 커버리지 표기(M0 승인 방식) — meanings 빈 채로 사유를 남기고
다른 chunk 는 계속 간다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.modules.gemini_client import (
    _extract_json_from_markdown,
    _loads_first_json,
    _max_tokens_usage,
)
from app.v3 import schemas
from app.v3.seq_analyze import MAX_REASKS, _upload_video

TRANSCRIPT_DIFF_MAX = 0.35     # 정규화 편집거리(공백 제거) — 넘으면 각색으로 보고 복원
CHUNK_SAMPLE_FPS = 3.0         # Gemini 표본 fps(2026-08-31 사용자 설정 — 종전 기본 1fps)
MOOD_MAX_CHARS = 20


def _name_list(value: Any, notes: list[str], where: str) -> list[str]:
    """characters 관용 파서 — 문자열이 오면 이름 하나로 받는다(글자 단위로 쪼개져
    ['강','비','오'] 가 실리던 결함의 리뷰 재현 수정)."""
    if isinstance(value, str):
        v = value.strip()
        if v:
            notes.append(f"{where} characters 문자열 → [{v!r}] 로 해석")
        return [v] if v else []
    if isinstance(value, list):
        return [str(c).strip() for c in value if str(c).strip()]
    return []


# ── span 소속·전사 정본 ─────────────────────────────────────────────────────

def spans_for_chunk(grid: dict, start_sec: float, end_sec: float) -> list[dict]:
    """chunk 구간에 속한 span 목록(**중점 반개구간 규칙** · t_in 순). 순수.

    chunk 경계는 격자 스냅이라 대부분 span 경계와 일치하지만, 유성 span 안의 장면
    전환에 스냅된 경계는 span 을 가로지를 수 있다. 소속 판정은 span 중점이
    [start, end) 에 드는가 하나다 — chunk 들이 타임라인을 반개구간으로 타일링하므로
    어떤 span 도 두 chunk 에 동시에 속할 수 없다(겹침 '≥절반' 규칙은 정확히 반으로
    갈리는 동률에서 양쪽 다 참이 되는 결함이 리뷰에서 재현돼 교체)."""
    out = []
    for sp in grid.get("span_candidates") or []:
        t0, t1 = float(sp["t_in"]), float(sp["t_out"])
        if t1 <= t0:
            continue
        mid = (t0 + t1) / 2.0
        if start_sec <= mid < end_sec:
            out.append(sp)
    return sorted(out, key=lambda s: (float(s["t_in"]), s["id"]))


def edit_ratio(a: str, b: str) -> float:
    """정규화 편집거리 비율 — 공백 제거 후 levenshtein / max(len). 순수."""
    x = "".join((a or "").split())
    y = "".join((b or "").split())
    if not x and not y:
        return 0.0
    if not x or not y:
        return 1.0
    prev = list(range(len(y) + 1))
    for i, cx in enumerate(x, 1):
        cur = [i]
        for j, cy in enumerate(y, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cx != cy)))
        prev = cur
    return prev[-1] / max(len(x), len(y))


# ── 모델 응답 검증(순수) ────────────────────────────────────────────────────

def validate_stage2_response(resp: Any, chunk_spans: list[dict], *,
                             final_attempt: bool) -> tuple[list[dict], list[str], list[str]]:
    """모델 응답 → (정규화된 meaning 목록, 반려 사유, 보정 노트).

    meaning 은 span id 의 **연속 구간 분할**이어야 한다: 순서대로, 빈틈·겹침 없이
    chunk 의 span 전부를 덮는다. span 상세 항목 누락은 반려 — 재질의 소진 시에만
    전사 기반 기본값으로 채우고 노트에 남긴다(조용한 공백 금지)."""
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(resp, dict) or not isinstance(resp.get("meanings"), list) \
            or not resp["meanings"]:
        return [], ["meanings 배열이 없다"], []

    order = [sp["id"] for sp in chunk_spans]
    idx_of = {sid: i for i, sid in enumerate(order)}
    by_id = {sp["id"]: sp for sp in chunk_spans}

    ranges: list[tuple[int, int, dict]] = []
    for k, m in enumerate(resp["meanings"]):
        if not isinstance(m, dict):
            problems.append(f"meanings[{k}] 가 객체가 아님")
            continue
        a, b = m.get("first_span"), m.get("last_span")
        # id 는 문자열이어야 한다 — 리스트 등이 오면 unhashable 로 TypeError 가 반려
        # 루프를 뚫고 run 전체를 죽였다(리뷰 재현). 형식 위반도 반려 재료다.
        if not isinstance(a, str) or not isinstance(b, str) \
                or a not in idx_of or b not in idx_of:
            problems.append(f"meanings[{k}] 모르는/비문자열 span id: {a!r}~{b!r} — "
                            "이 chunk 의 span 목록에서 문자열 id 로만 골라라")
            continue
        ia, ib = idx_of[a], idx_of[b]
        if ib < ia:
            problems.append(f"meanings[{k}] span 순서 역전: {a}~{b}")
            continue
        ranges.append((ia, ib, m))
    if problems:
        return [], problems, []

    ranges.sort(key=lambda r: r[0])
    cursor = 0
    for ia, ib, _m in ranges:
        if ia > cursor:
            problems.append(f"span 빈틈: {order[cursor]}~{order[ia - 1]} 가 어느 meaning "
                            "에도 없다 — 모든 span 을 빠짐없이 묶어라")
        elif ia < cursor:
            problems.append(f"meaning 겹침: {order[ia]} 부터가 두 meaning 에 속한다")
        cursor = max(cursor, ib + 1)
    if cursor < len(order):
        problems.append(f"span 빈틈: {order[cursor]} 이후가 어느 meaning 에도 없다")
    if problems:
        return [], problems, []

    norm: list[dict] = []
    for k, (ia, ib, m) in enumerate(ranges):
        content = str(m.get("content") or "").strip()
        if not content:
            problems.append(f"meanings[{k}] content 없음")
        imp = m.get("importance")
        if not isinstance(imp, int) or isinstance(imp, bool) or not 1 <= imp <= 5:
            notes.append(f"meanings[{k}] importance {imp!r} → 3 보정")
            imp = 3
        mood = str(m.get("mood") or "").strip()[:MOOD_MAX_CHARS]
        chars = _name_list(m.get("characters"), notes, f"meanings[{k}]")

        span_entries: dict[str, dict] = {}
        dup_ids: list[str] = []
        bad_ids = 0
        for s in m.get("spans") or []:
            if not isinstance(s, dict):
                bad_ids += 1
                continue
            sid = s.get("id")
            if not isinstance(sid, str):        # None·리스트 키는 sorted/셋 연산을 깨뜨린다
                bad_ids += 1
                continue
            if sid in span_entries:
                dup_ids.append(sid)             # last-wins 무성 병합 금지 — 반려로 되묻는다
            span_entries[sid] = s
        if bad_ids:
            problems.append(f"meanings[{k}] id 없는/비문자열 span 상세 {bad_ids}건 — "
                            "모든 상세에 문자열 id 를 달아라")
        if dup_ids:
            problems.append(f"meanings[{k}] span 상세 중복: {sorted(set(dup_ids))[:5]} — "
                            "span 당 상세는 하나다")
        unknown = sorted(set(span_entries) - set(order[ia:ib + 1]))
        if unknown:
            problems.append(f"meanings[{k}] 범위 밖 span 상세: {unknown[:5]}")
        spans_out: list[dict] = []
        missing: list[str] = []
        for sid in order[ia:ib + 1]:
            gsp = by_id[sid]
            entry = span_entries.get(sid)
            if entry is None:
                missing.append(sid)
                entry = {}
            audio_in = entry.get("heard") if entry.get("heard") is not None \
                else entry.get("audio")
            if isinstance(audio_in, str):        # 관용 파서 — characters 와 같은 규율
                audio_in = ([{"speaker": "미상", "line": audio_in}]
                            if audio_in.strip() else [])
                notes.append(f"{sid} heard 문자열 → 1행으로 해석")
            elif isinstance(audio_in, dict):
                audio_in = [audio_in]
                notes.append(f"{sid} heard 객체 → 1행 배열로 해석")
            audio = []
            if gsp["is_audio"]:
                if isinstance(audio_in, list) and audio_in:
                    for line in audio_in:
                        if isinstance(line, dict) and str(line.get("line") or "").strip():
                            audio.append({
                                "speaker": str(line.get("speaker") or "").strip() or "미상",
                                "line": str(line["line"]).strip()})
                if not audio:
                    # heard 누락 = 모델이 못 들었다 — 전사 채택 경로로 보낸다
                    # (예전처럼 전사를 여기서 채워 넣으면 판정이 무의미해진다)
                    audio = []
            elif audio_in:
                notes.append(f"{sid} 무성 span 의 audio 제안 폐기(전사에 발화 없음)")
            sp_imp = entry.get("importance")
            if not isinstance(sp_imp, int) or isinstance(sp_imp, bool) \
                    or not 1 <= sp_imp <= 5:
                sp_imp = imp
            spans_out.append({
                "span_id": sid,
                "scene_script": str(entry.get("scene_script") or "").strip(),
                "characters": _name_list(entry.get("characters"), notes, sid),
                "importance": sp_imp,
                "audio": audio,
                "heard": list(audio) if gsp["is_audio"] else [],
            })
        if missing:
            if final_attempt:
                notes.append(f"meanings[{k}] span 상세 누락 {len(missing)}건 — 전사 "
                             f"기본값으로 채움(커버리지 표기): {missing[:5]}")
            else:
                problems.append(f"meanings[{k}] span 상세 누락 {len(missing)}건 "
                                f"({missing[:5]} …) — 모든 span 에 scene_script 를 써라")
        norm.append({"first_idx": ia, "last_idx": ib, "content": content,
                     "characters": chars, "importance": imp, "mood": mood,
                     "spans": spans_out})
    if problems:
        return [], problems, notes
    return norm, [], notes


# ── M9-C: 전사 판정 — 모델은 "들은 것"만 내고, 채택은 코드가 한다 ────────────
TRANSCRIPT_MIN_PROB = 0.35     # span 평균 확신도 — 이 아래는 전사를 못 믿는다
SPAN_SIGNATURE_MIN_WORDS = 6   # 반복·길이 서명을 볼 최소 단어 수(리뷰 확정: 작은
                               # 표본에서 비율·중앙값이 자명해져 정상 발화를 오판)
SPAN_MIN_MEDIAN_DUR = 0.18     # span 단어 길이 중앙 하한 — 실측: 실제 반복 발화
                               # 0.260s vs 환각 루프 0.000~0.130s
# 반복·길이 퇴화 임계 자체는 transcribe.GAP_REPEAT_* / GAP_MIN_MEDIAN_DUR 하나뿐이다
# — M8-B 재전사와 **같은 서명을 공유**한다(여기서 따로 상수를 두면 두 출처가 갈린다)


def transcript_broken(gsp: dict, words: list[dict] | None) -> str | None:
    """이 span 의 전사가 **기계적으로** 깨졌는가 → 사유(정상이면 None). 순수.

    판정은 전부 물리량이다(반복 서명·단어 길이·확신도) — 의미 판단은 하지 않는다.
    모델이 자기 텍스트를 스스로 승격시키지 못하게 하는 장치다."""
    text = str(gsp.get("text") or "").strip()
    if not text:
        return "전사 없음"
    ws = [w for w in (words or [])
          if gsp["t_in"] <= (float(w["t0"]) + float(w["t1"])) / 2 < gsp["t_out"]]
    if ws:
        # span 레벨은 **반복 ∧ 길이 이상**을 동시에 요구한다. 실측(가왕쇼 M9 완주)이
        # 근거다 — 실제 반복 발화 "이거 이거 티켓 티켓 필요 없고…"는 단어 길이
        # 중앙 0.260s·최소 0.120s 인데 반복 비율만 보고 뒤집혀 자막이 사람이 하지
        # 않은 말("그거 말고 제 티켓 받으세요")로 바뀌었다. 환각 루프는 길이가
        # 무너진다: "육십!"×53 중앙 0.130s·최소 0.000 · "그녀는"×71 중앙 0.000s.
        # 창(≥6s 재전사) 단계는 둘 중 하나로 충분하다 — 그쪽은 버려도 무성으로
        # 돌아갈 뿐이지만, 여기서 뒤집으면 **모델 텍스트가 화면에 나간다**.
        if len(ws) >= SPAN_SIGNATURE_MIN_WORDS:
            from collections import Counter
            texts = [str(w.get("text") or "") for w in ws]
            top, n = Counter(texts).most_common(1)[0]
            durs = sorted(float(w["t1"]) - float(w["t0"]) for w in ws)
            median = durs[len(durs) // 2]
            repeated = n >= 3 and n / len(texts) >= 0.4
            degenerate = median < SPAN_MIN_MEDIAN_DUR
            if repeated and degenerate:
                return (f"반복 환각: {top!r} ×{n}/{len(texts)} "
                        f"(길이 중앙 {median:.3f}s)")
            if degenerate and min(durs) <= 0.0:
                return f"길이 퇴화: 단어 길이 중앙 {median:.3f}s"
        # ⚠ 저확신 단독으로는 뒤집지 않는다(리뷰·실측): 1~2단어 span 은 확신도가
        # 구조적으로 낮고(실측 "안녕하세요." 0.04 인데 전사가 정답이었다), 전사를
        # 모델 추정으로 바꾸는 대가가 크다. 저확신은 validate 계기판의 몫이다.
    else:
        # 단어가 없는데 텍스트가 있다 = 격자 재료와 전사 텍스트의 불일치
        toks = text.split()
        if len(toks) >= SPAN_SIGNATURE_MIN_WORDS and len(set(toks)) == 1:
            return f"반복 환각: {toks[0]!r} ×{len(toks)}"
    return None


def degenerate_span_run(chunk_spans: list[dict]) -> dict[str, str]:
    """**구간 단위** 반복 환각 — span id → 사유. 순수.

    실측 재현(가왕쇼 991~1014s): 환각이 span 하나 안에 뭉쳐 있지 않고 "육십!" 한
    단어짜리 span 29개로 흩어져 있었다. 단일 span 검사(transcript_broken)는 각각을
    정상으로 보므로, 이웃한 유성 span 들의 텍스트 반복을 따로 본다 —
    자막 그물(textcheck.check_repetition)과 같은 서명·같은 임계를 재사용한다."""
    from app.v3 import textcheck
    voiced = [s for s in sorted(chunk_spans, key=lambda x: float(x["t_in"]))
              if s["is_audio"] and str(s.get("text") or "").strip()]
    segs = [{"start_sec": float(s["t_in"]), "end_sec": float(s["t_out"]),
             "text": str(s["text"]).strip()} for s in voiced]
    out: dict[str, str] = {}
    for w in textcheck.check_repetition(segs):
        for i in w.get("indexes") or []:
            out[voiced[i]["id"]] = f"구간 반복 환각: {w['text'][:16]!r} ×{w['n']}span"
    return out


def adjudicate_transcript(norm: list[dict], chunk_spans: list[dict],
                          words: list[dict] | None = None) -> list[dict]:
    """검증 ② (M9-C) — span 마다 전사 vs 모델 청취(heard) 중 채택본을 확정. 판정 기록.

    규칙(보수·결정적):
      전사 정상 → **전사 채택**(현행 유지). heard 가 크게 다르면 각색으로 보고
                  전사로 복원한다 — M2 부터의 각색 방어(실측 3.5%)를 유지.
      전사 깨짐 → **heard 채택**(있을 때만). 모델은 소리를 실제로 듣는다 —
                  환각 전사를 그대로 옮겨 적던 경로(실측 77/78)의 해소.
      둘 다 불가 → 자막 제외(text_source=none) — 조용한 공백 금지, 사유를 남긴다."""
    by_id = {sp["id"]: sp for sp in chunk_spans}
    run_broken = degenerate_span_run(chunk_spans)   # 구간 단위 서명(단일 span 밖)
    records: list[dict] = []
    for m in norm:
        for s in m["spans"]:
            gsp = by_id[s["span_id"]]
            if not gsp["is_audio"]:
                continue
            transcript = str(gsp.get("text") or "").strip()
            raw = s.get("heard")
            if isinstance(raw, str):          # 문서 표기(heard_text)가 되돌아온 경우
                heard_lines = [{"speaker": "미상", "line": raw}] if raw.strip() else []
            else:
                heard_lines = raw or s.get("audio") or []
            heard_lines = [x for x in heard_lines if isinstance(x, dict)]
            heard = " ".join(str(x.get("line") or "") for x in heard_lines).strip()
            speaker = (heard_lines[0].get("speaker") if heard_lines else None) or "미상"
            broken = run_broken.get(s["span_id"]) or transcript_broken(gsp, words)
            # M10-B: 재료 신뢰 신호 — 스토리가 "이 대사를 얼마나 믿을 수 있나"를
            # 보고 고르게 한다(실측: 저확신 구간을 골라 자막이 깨진 채 나갔다)
            _ws = [w for w in (words or [])
                   if gsp["t_in"] <= (float(w["t0"]) + float(w["t1"])) / 2 < gsp["t_out"]]
            _pr = [float(w["prob"]) for w in _ws if w.get("prob") is not None]
            s["conf"] = round(sum(_pr) / len(_pr), 2) if _pr else None
            rec = {"span_id": s["span_id"], "broken": broken, "conf": s["conf"]}
            if broken and heard:
                s["audio"] = [dict(x) for x in heard_lines]
                s["text_source"] = "heard"
                rec.update({"decision": "heard", "text": heard[:80],
                            "dropped_transcript": transcript[:80]})
            elif broken:
                s["audio"] = []
                s["text_source"] = "none"
                rec.update({"decision": "none", "text": ""})
            else:
                ratio = edit_ratio(heard, transcript) if heard else 1.0
                s["audio"] = [{"speaker": speaker, "line": transcript}]
                s["text_source"] = "transcript"
                rec.update({"decision": "transcript", "diff": round(ratio, 3),
                            "restored": bool(heard) and ratio > TRANSCRIPT_DIFF_MAX})
            s["heard_text"] = heard
            records.append(rec)
    return records


def character_cross_check(appearances: list[dict] | None,
                          norm: list[dict], chunk_spans: list[dict],
                          start_sec: float, end_sec: float) -> dict:
    """검증 ③ — ArcFace 클러스터 라벨 ↔ 인물명 배정의 일관성(경고 집계, 차단 아님).

    클러스터 하나의 등장 구간들과 겹치는 span 들에서 배정된 인물명을 모아,
    최빈 이름의 점유율을 그 클러스터의 일관성으로 본다. 인덱스가 없으면
    status=skipped(커버리지 표기 — deps_absent 노드)."""
    if not appearances:
        return {"status": "skipped", "reason": "character_index 없음(deps_absent 또는 미생성)"}
    span_time = {sp["id"]: (float(sp["t_in"]), float(sp["t_out"])) for sp in chunk_spans}
    span_chars: dict[str, list[str]] = {}
    for m in norm:
        for s in m["spans"]:
            span_chars[s["span_id"]] = s["characters"] or m["characters"]

    clusters: dict[str, dict[str, int]] = {}
    for ap in appearances:
        a0, a1 = float(ap.get("start_sec", 0)), float(ap.get("end_sec", 0))
        if min(a1, end_sec) - max(a0, start_sec) <= 0:
            continue
        label = str(ap.get("character") or "?")
        counts = clusters.setdefault(label, {})
        for sid, (t0, t1) in span_time.items():
            if min(t1, a1) - max(t0, a0) > 0:
                for name in span_chars.get(sid) or []:
                    counts[name] = counts.get(name, 0) + 1
    rows = []
    total_top = total_all = 0
    for label in sorted(clusters):
        counts = clusters[label]
        if not counts:
            rows.append({"label": label, "top_name": None, "consistency": None,
                         "assignments": 0})
            continue
        top_name = max(sorted(counts), key=lambda n: counts[n])
        top, allc = counts[top_name], sum(counts.values())
        total_top += top
        total_all += allc
        rows.append({"label": label, "top_name": top_name,
                     "consistency": round(top / allc, 3), "assignments": allc})
    return {"status": "ok",
            "clusters": rows,
            "overall_consistency": round(total_top / total_all, 3) if total_all else None,
            "clusters_in_chunk": len(clusters)}


def assemble_chunk_meanings(norm: list[dict], chunk_spans: list[dict]) -> list[dict]:
    """검증 통과한 묶음 → 기획서 §4 스키마(시각은 전부 grid lookup — 원본 절대초)."""
    out = []
    for num, m in enumerate(norm):
        first = chunk_spans[m["first_idx"]]
        last = chunk_spans[m["last_idx"]]
        spans_doc = []
        for j, s in enumerate(m["spans"]):
            gsp = chunk_spans[m["first_idx"] + j]
            spans_doc.append({
                "number": j,
                "span_id": s["span_id"],                      # additive — M3 가 ID 만 다룬다
                "time": {"start": schemas.format_ts(float(gsp["t_in"])),
                         "end": schemas.format_ts(float(gsp["t_out"]))},
                "is_audio": bool(gsp["is_audio"]),
                "audio_script": s["audio"] if gsp["is_audio"] else [],
                "heard_text": s.get("heard_text", "") if gsp["is_audio"] else "",
                "conf": s.get("conf") if gsp["is_audio"] else None,
                "text_source": s.get("text_source") if gsp["is_audio"] else None,
                "scene_script": s["scene_script"],
                "characters": s["characters"],
                "importance": s["importance"],
                "time_authority": gsp["time_authority"],
            })
        out.append({
            "number": num,
            "time": {"start": schemas.format_ts(float(first["t_in"])),
                     "end": schemas.format_ts(float(last["t_out"]))},
            "content": m["content"],
            "characters": m["characters"],
            "importance": m["importance"],
            "mood": m["mood"],
            "spans": spans_doc,
        })
    return out


def verify_time_alignment(meanings: list[dict], grid: dict) -> dict:
    """검증 ① 벨트 — 산출 시각이 전부 grid span 경계인가(100% 여야 한다)."""
    edges = set()
    for sp in grid.get("span_candidates") or []:
        edges.add(round(float(sp["t_in"]), 3))
        edges.add(round(float(sp["t_out"]), 3))
    checked = ok = 0
    bad: list[str] = []
    for m in meanings:
        for ts in (m["time"]["start"], m["time"]["end"]):
            checked += 1
            if round(schemas.parse_ts(ts), 3) in edges:
                ok += 1
            else:
                bad.append(ts)
        for s in m["spans"]:
            for ts in (s["time"]["start"], s["time"]["end"]):
                checked += 1
                if round(schemas.parse_ts(ts), 3) in edges:
                    ok += 1
                else:
                    bad.append(ts)
    return {"checked": checked, "from_grid": ok,
            "pct": round(ok / checked * 100, 2) if checked else None,
            "violations": bad[:10]}


# ── 프롬프트·호출 ───────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """당신은 방송 영상의 장면 기록가다. 첨부한 청크 영상을 보고, 아래 span 목록을 **의미 단위(meaning)로 묶어라**. 시각은 절대 쓰지 않는다 — span id 로만 말한다.

{research_block}## 이 편의 구조 (Stage 1)
{stage1_block}

## 이 청크의 span 목록 (id | 시각 | 유성/무성 | 전사)
전사는 시각(span 경계)의 근거다. 대사 텍스트는 아래 heard 로 당신이 들은 것을 적고, 확정은 코드에 맡긴다.
{span_table}
{faces_block}
## 과제
1. 연속한 span 들을 하나의 meaning 으로 묶어라 — "누가 무엇을 하고 있다"가 바뀌는 지점이 경계다. 이 청크의 **모든 span 이 정확히 하나의 meaning** 에 속해야 한다(빈틈·겹침 금지).
2. meaning 마다: content(한 문장) · characters(등장인물명) · importance(1~5, 이야기 기여도) · mood(한 단어).
3. span 마다: scene_script(화면 묘사 한 문장) · characters · importance(1~5) · heard(유성 span 만 — **당신이 실제로 들은 대사**를 화자와 함께 적어라. 무성 span 은 생략).
   ⚠ heard 는 전사를 베끼는 칸이 아니다. 위 전사표는 참고일 뿐이고, **들리는 대로** 적어라 — 전사가 잡음·음악에 망가져 있을 수 있다(같은 말이 수십 번 반복되는 등). 최종 대사는 코드가 전사와 당신의 heard 를 대조해 확정하니, 당신은 각색하지 말고 들은 것만 정확히 옮기면 된다.
{reject_block}
## 출력 (JSON 만)
{{"meanings": [
  {{"first_span": "sp0000", "last_span": "sp0004",
    "content": "…", "characters": ["이름"], "importance": 4, "mood": "긴장",
    "spans": [
      {{"id": "sp0000", "scene_script": "…", "characters": ["이름"], "importance": 3,
        "heard": [{{"speaker": "이름", "line": "들은 대사 그대로"}}]}}
    ]}}
]}}"""


def build_stage2_prompt(chunk: dict, stage1_doc: dict, chunk_spans: list[dict],
                        appearances: list[dict] | None,
                        research_context: str = "",
                        character_names: list[str] | None = None,
                        reject_note: str = "") -> str:
    research_block = ""
    if research_context or character_names:
        parts = []
        if character_names:
            parts.append("등장인물 사전: " + ", ".join(character_names[:20]))
        if research_context:
            parts.append(research_context.strip()[:1200])
        research_block = "## 작품 배경\n" + "\n".join(parts) + "\n\n"

    s1_lines = []
    for sq in stage1_doc.get("sequences") or []:
        mark = ""
        for ch in sq.get("chunks") or []:
            if (int(sq["number"]), int(ch["number"])) == (
                    chunk["seq_number"], chunk["chunk_number"]):
                mark = f" ← **이 청크 (chunk {ch['number']}: "\
                       f"{ch['time']['start']}~{ch['time']['end']})**"
        s1_lines.append(f"- seq{sq['number']} {sq['time']['start']}~{sq['time']['end']}: "
                        f"{sq['content']}{mark}")
    span_lines = []
    for sp in chunk_spans:
        kind = "유성" if sp["is_audio"] else "무성"
        text = sp.get("text") or "—"
        span_lines.append(
            f"{sp['id']} | {schemas.format_ts(float(sp['t_in']))}~"
            f"{schemas.format_ts(float(sp['t_out']))} | {kind} | {text}")

    faces_block = ""
    if appearances:
        rows = []
        for ap in appearances:
            a0, a1 = float(ap.get("start_sec", 0)), float(ap.get("end_sec", 0))
            if min(a1, chunk["end_sec"]) - max(a0, chunk["start_sec"]) > 0:
                rows.append(f"- {ap.get('character')}: "
                            f"{schemas.format_ts(max(a0, chunk['start_sec']))}~"
                            f"{schemas.format_ts(min(a1, chunk['end_sec']))}")
        if rows:
            faces_block = ("\n## 얼굴 클러스터 관측 (참고 — 같은 라벨 = 같은 인물)\n"
                           + "\n".join(rows[:40]) + "\n")
    reject_block = ""
    if reject_note:
        reject_block = f"\n## ⚠ 직전 제안 반려 사유 — 전부 고쳐서 다시 내라\n{reject_note}\n"
    return PROMPT_TEMPLATE.format(research_block=research_block,
                                  stage1_block="\n".join(s1_lines),
                                  span_table="\n".join(span_lines),
                                  faces_block=faces_block,
                                  reject_block=reject_block)


def _call_stage2_model(gemini, uploaded, prompt: str) -> dict:
    types = gemini.types
    part = types.Part(file_data=types.FileData(file_uri=uploaded.uri,
                                               mime_type="video/mp4"),
                      video_metadata=types.VideoMetadata(fps=CHUNK_SAMPLE_FPS))
    response = gemini.client.models.generate_content(
        model=gemini.config.model_name,          # Pro — 영상을 실제로 보는 호출
        contents=[part, prompt],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=65536,
            thinking_config=types.ThinkingConfig(
                thinking_level=gemini.config.analysis_thinking_level),
        ))
    truncated = _max_tokens_usage(response)
    text = _extract_json_from_markdown(response.text or "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        try:
            obj, _rest = _loads_first_json(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        raise ValueError(
            "응답 JSON 파싱 실패"
            + (f" (MAX_TOKENS 절단: {truncated})" if truncated else "")
            + f": {e} — 앞 200자: {text[:200]!r}") from e


def run_chunk_analyze(gemini, chunk_file: Path, chunk: dict, stage1_doc: dict,
                      grid: dict, *, appearances: list[dict] | None = None,
                      research_context: str = "",
                      character_names: list[str] | None = None,
                      log=print) -> tuple[list[dict] | None, dict]:
    """chunk 1개 분석 → (meanings | None(실패 — 커버리지 표기), 감사 기록)."""
    chunk_spans = spans_for_chunk(grid, chunk["start_sec"], chunk["end_sec"])
    audit: dict[str, Any] = {"chunk": f"s{chunk['seq_number']}c{chunk['chunk_number']}",
                             "spans": len(chunk_spans), "attempts": []}
    if not chunk_spans:
        audit["failed"] = "chunk 에 span 이 없다(무성 극단 케이스)"
        return None, audit

    uploaded = _upload_video(gemini, chunk_file, log=log)
    try:
        reject_note = ""
        for attempt in range(1 + MAX_REASKS):
            final = attempt == MAX_REASKS
            prompt = build_stage2_prompt(
                chunk, stage1_doc, chunk_spans, appearances,
                research_context=research_context, character_names=character_names,
                reject_note=reject_note)
            log(f"  [v3/stage2] {audit['chunk']} Pro 요청 "
                f"(시도 {attempt + 1}/{1 + MAX_REASKS}, span {len(chunk_spans)})")
            t0 = time.time()
            problems: list[str] = []
            notes: list[str] = []
            try:
                resp = _call_stage2_model(gemini, uploaded, prompt)
                norm, problems, notes = validate_stage2_response(
                    resp, chunk_spans, final_attempt=final)
            except ValueError as e:
                problems = [f"응답 오류: {e}"]
                norm = []
            rec = {"attempt": attempt + 1, "elapsed": round(time.time() - t0, 1),
                   "problems": problems, "notes": notes}
            audit["attempts"].append(rec)
            if problems:
                log(f"  [v3/stage2] {audit['chunk']} 반려 — 사유 {len(problems)}건")
                reject_note = "\n".join(f"- {p}" for p in problems[:20])
                continue

            decisions = adjudicate_transcript(norm, chunk_spans, grid.get("words"))
            meanings = assemble_chunk_meanings(norm, chunk_spans)
            picked = {k: sum(1 for d in decisions if d["decision"] == k)
                      for k in ("transcript", "heard", "none")}
            restored = [d for d in decisions if d.get("restored")]
            audit["transcript_guard"] = {
                "voiced_spans": sum(1 for sp in chunk_spans if sp["is_audio"]),
                "restored": len(restored), "details": restored[:10],
                "picked": picked,
                "broken": [d for d in decisions if d["broken"]][:10]}
            audit["character_check"] = character_cross_check(
                appearances, norm, chunk_spans, chunk["start_sec"], chunk["end_sec"])
            audit["time_alignment"] = verify_time_alignment(meanings, grid)
            for r in restored:
                log(f"  [v3/stage2] {audit['chunk']} 각색 복원 {r['span_id']} "
                    f"(diff {r.get('diff')}) → 전사 채택")
            for b in [d for d in decisions if d["decision"] == "heard"][:10]:
                log(f"  [v3/stage2] {audit['chunk']} 전사 깨짐 {b['span_id']} "
                    f"({b['broken']}) → 청취 채택: {b.get('text','')[:34]!r}")
            for b in [d for d in decisions if d["decision"] == "none"][:5]:
                log(f"  [v3/stage2] ⚠ {audit['chunk']} {b['span_id']} 대사 확보 실패 "
                    f"({b['broken']}) — 자막 제외")
            return meanings, audit
        audit["failed"] = ("반려 소진 — 마지막 사유: "
                           + "; ".join(audit["attempts"][-1]["problems"][:3]))
        return None, audit
    finally:
        try:
            gemini.client.files.delete(name=uploaded.name)
        except Exception as e:  # noqa: BLE001
            log(f"  [v3/stage2] WARN 서버 파일 삭제 실패: {e}")
