"""편 단위 지표 추출 + 집계 — 전부 순수 함수(파일·네트워크 접근 없음).

시간 값은 소수 3자리로 반올림해 산출한다 — 같은 입력이면 바이트까지 같은 출력이
나와야 한다(결정성 합격 기준). 백분위는 보간 없이 `sorted[int(q*(n-1))]` 하나로
고정한다 — 문제 정의 §3의 p90 736초가 이 방식으로 재현됨을 실측으로 확인했다.
"""
from __future__ import annotations

import statistics
from typing import Any

# 문장 종결로 인정하는 꼬리 문자 — "마지막 자막 cue 가 종결부호로 끝나는가" 판정.
# 닫는 따옴표·괄호는 벗겨낸 뒤 판정한다("…했다.")도 종결이다).
SENTENCE_FINAL_CHARS = ".!?…~"
_TRAILING_WRAPPERS = "\"')]}』」》"


def clip_span(clip: dict) -> tuple[float, float]:
    """클립 dict → (start, end). 세 가지 표기를 모두 받는다:
    checkpoint_story(start_sec/end_sec) · edit_plan(clip_start_sec/clip_end_sec) ·
    editor_timeline/v1(start_sec/end_sec 또는 dur_sec)."""
    if "clip_start_sec" in clip:
        return float(clip.get("clip_start_sec") or 0.0), float(clip.get("clip_end_sec") or 0.0)
    s = float(clip.get("start_sec") or 0.0)
    e = clip.get("end_sec")
    if e is None and clip.get("dur_sec") is not None:
        return s, s + float(clip["dur_sec"])
    return s, float(e or 0.0)


def clip_len(clip: dict) -> float:
    s, e = clip_span(clip)
    return round(max(0.0, e - s), 3)


def percentile(values: list[float], q: float) -> float | None:
    """보간 없는 백분위 — sorted[int(q*(n-1))]. 빈 목록은 None."""
    if not values:
        return None
    s = sorted(values)
    return round(s[int(q * (len(s) - 1))], 3)


def median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.median(values), 3)


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.mean(values), 3)


def bucket_of(n: int) -> str:
    """원안 구간 수 → §3 분포 표의 버킷 이름."""
    return "4+" if n >= 4 else str(n)


def sentence_final(text: str) -> bool:
    """자막 cue 텍스트가 문장 종결부호로 끝나는가."""
    t = (text or "").strip()
    while t and t[-1] in _TRAILING_WRAPPERS:
        t = t[:-1].rstrip()
    return bool(t) and t[-1] in SENTENCE_FINAL_CHARS


def raw_storyline_clip_count(raw_response: dict) -> int | None:
    """raw_response.selected_storyline 구조상 클립 수(hook + build[] + payoff).

    §3 '원안'이 아니다(분포가 재현되지 않음 — 패키지 독스트링) — 조립 과정에서
    빠지거나 합쳐진 양(raw_n - orig_n)을 보는 보조 신호로만 쓴다."""
    if not isinstance(raw_response, dict):
        return None
    sel = raw_response.get("selected_storyline") or {}
    if raw_response.get("shorts_type") == "highlight" or sel.get("shorts_type") == "highlight":
        return 1
    st = sel.get("storyline") or {}
    n = 0
    if isinstance(st.get("hook"), dict):
        n += 1
    if isinstance(st.get("build"), list):
        n += len(st["build"])
    if isinstance(st.get("payoff"), dict):
        n += 1
    return n or None


def raw_storyline_ext_requested(raw_response: dict) -> int:
    """raw storyline 클립 중 context_extended=true 로 확장을 요청한 클립 수."""
    if not isinstance(raw_response, dict):
        return 0
    sel = raw_response.get("selected_storyline") or {}
    st = sel.get("storyline") or {}
    nodes: list[Any] = []
    if isinstance(st.get("hook"), dict):
        nodes.append(st["hook"])
    if isinstance(st.get("build"), list):
        nodes.extend(b for b in st["build"] if isinstance(b, dict))
    if isinstance(st.get("payoff"), dict):
        nodes.append(st["payoff"])
    if not nodes and isinstance(sel, dict) and sel.get("context_extended") is not None:
        nodes.append(sel)  # highlight 형
    return sum(1 for n in nodes if n.get("context_extended"))


def run_metrics(rec: dict) -> dict:
    """편 1개(RunRecord dict) → 지표 dict. 입력을 건드리지 않는다.

    rec 필수 키: key · run_id · story_clips(list) · timeline(list)
    선택 키: created_at · git_sha · raw_response · config · human_edited(bool)
    """
    story = rec.get("story_clips") or []
    timeline = rec.get("timeline") or []
    orig_lens = [clip_len(c) for c in story]
    final_lens = [clip_len(c) for c in timeline]
    n_orig, n_final = len(story), len(timeline)

    # 원안 인접 갭(소스 시간 · 시작순 정렬) + 여러 청크 스팬 여부
    srt = sorted(story, key=lambda c: clip_span(c)[0])
    gaps = [
        round(max(0.0, clip_span(b)[0] - clip_span(a)[1]), 3)
        for a, b in zip(srt, srt[1:])
    ]
    chunks = {c.get("chunk_index") for c in story if c.get("chunk_index") is not None}

    raw = rec.get("raw_response") or {}
    out = {
        "key": rec.get("key", ""),
        "run_id": rec.get("run_id", ""),
        "created_at": rec.get("created_at", ""),
        "git_sha": (rec.get("git_sha") or "")[:12],
        "orig_n": n_orig,
        "final_n": n_final,
        "orig_lens": orig_lens,
        "final_lens": final_lens,
        "orig_total": round(sum(orig_lens), 3),
        "final_total": round(sum(final_lens), 3),
        "orig_gaps": gaps,
        "spans_multiple_chunks": len(chunks) > 1,
        "silence_split": n_final > n_orig > 0,
        "shrunk": 0 < n_final < n_orig,
        "raw_storyline_n": raw_storyline_clip_count(raw),
        "ext_requested_clips": raw_storyline_ext_requested(raw),
        "human_edited": bool(rec.get("human_edited")),
        "usable": n_orig > 0 and n_final > 0,
    }

    bundle = rec.get("bundle") or {}
    if bundle:
        out.update(_bundle_metrics(story, timeline, bundle))
    return out


def _bundle_metrics(story: list[dict], timeline: list[dict], bundle: dict) -> dict:
    """번들(2차 자료)이 있는 편의 추가 지표 — 문장 절단 · 맥락 확장 제안/실적용.

    bundle 키(전부 선택): subtitle_segments(list) · all_candidates(list) ·
    silence_variants(list — checkpoint_silence_cut.variants)."""
    out: dict[str, Any] = {}

    cues = bundle.get("subtitle_segments") or []
    if cues:
        # 편의 마지막 cue 가 종결부호로 끝나는가
        last = max(cues, key=lambda c: float(c.get("end_sec") or 0.0))
        out["last_cue_sentence_final"] = sentence_final(str(last.get("text", "")))
        # 구간 경계 절단: 편집 타임라인에서 각 클립의 끝 경계(마지막 클립 제외)에
        # 걸쳐 있는 cue = 말이 끝나기 전에 화면이 넘어간 자리.
        bases: list[float] = []
        acc = 0.0
        for c in timeline:
            acc += clip_len(c)
            bases.append(round(acc, 3))
        boundaries = bases[:-1]
        cut = 0
        for b in boundaries:
            for cue in cues:
                cs = float(cue.get("start_sec") or 0.0)
                ce = float(cue.get("end_sec") or 0.0)
                if cs < b - 0.05 and ce > b + 0.05:
                    cut += 1
                    break
        out["boundary_n"] = len(boundaries)
        out["boundary_cut_n"] = cut

    cands = bundle.get("all_candidates") or []
    if cands and story:
        lookup = {
            (int(c.get("chunk_index", -1)), int(c.get("candidate_index", -1))): c
            for c in cands
        }
        proposed = applied = 0.0
        matched = 0
        for clip in story:
            cand = lookup.get(
                (int(clip.get("chunk_index", -1)), int(clip.get("candidate_index", -1))))
            if not cand:
                continue
            matched += 1
            cs, ce = float(cand.get("start_sec") or 0.0), float(cand.get("end_sec") or 0.0)
            ks, ke = clip_span(clip)
            ext = cand.get("context_extension") or {}
            if ext.get("needed"):
                proposed += max(0.0, cs - float(ext.get("extended_start_sec", cs)))
                proposed += max(0.0, float(ext.get("extended_end_sec", ce)) - ce)
            applied += max(0.0, cs - ks) + max(0.0, ke - ce)
        out["ext_matched_clips"] = matched
        out["ext_proposed_sec"] = round(proposed, 3)
        out["ext_applied_sec"] = round(applied, 3)

    sv = bundle.get("silence_variants") or []
    if sv:
        out["silence_cut_n"] = len((sv[0] or {}).get("clips") or [])
    return out


def aggregate(per_run: list[dict]) -> dict:
    """편 단위 지표 목록 → 집계. §3 분포 표 + 발견 2·3 + 길이 분포.

    usable=False(원안 또는 최종이 빈 편)는 분포에서 빼고 개수만 보고한다 —
    커버리지가 100%가 아니면 몇 편이 왜 빠졌는지 리포트에 드러나야 한다(멈춤 시점 1)."""
    usable = [m for m in per_run if m.get("usable")]
    skipped = [m for m in per_run if not m.get("usable")]

    buckets: dict[str, dict] = {}
    for name in ("1", "2", "3", "4+"):
        rows = [m for m in usable if bucket_of(m["orig_n"]) == name]
        finals = [m["final_n"] for m in rows]
        buckets[name] = {
            "n": len(rows),
            "pct": round(len(rows) / len(usable) * 100, 1) if usable else None,
            "final_mean": mean([float(x) for x in finals]),
            "split_n": sum(1 for m in rows if m["silence_split"]),
            "shrunk_n": sum(1 for m in rows if m["shrunk"]),
        }

    # 발견 2 — 원안 1구간이 끝까지 통짜로 나간 편
    ones = [m for m in usable if m["orig_n"] == 1]
    solo = [m for m in ones if m["final_n"] == 1]
    finding2 = {
        "orig1_n": len(ones),
        "solo_n": len(solo),
        "solo_pct": round(len(solo) / len(ones) * 100, 1) if ones else None,
        "solo_len_mean": mean([m["final_lens"][0] for m in solo if m["final_lens"]]),
    }

    # 발견 3 — 원안 인접 갭 + 여러 청크 스팬(원안 2구간 이상 편 기준)
    multi = [m for m in usable if m["orig_n"] >= 2]
    all_gaps = [g for m in multi for g in m["orig_gaps"]]
    finding3 = {
        "gap_median": median(all_gaps),
        "gap_p90": percentile(all_gaps, 0.9),
        "multi_run_n": len(multi),
        "multi_chunk_n": sum(1 for m in multi if m["spans_multiple_chunks"]),
        "multi_chunk_pct": (
            round(sum(1 for m in multi if m["spans_multiple_chunks"]) / len(multi) * 100, 1)
            if multi else None),
    }

    orig_lens = [x for m in usable for x in m["orig_lens"]]
    final_lens = [x for m in usable for x in m["final_lens"]]
    lengths = {
        "orig_len_median": median(orig_lens),
        "orig_len_p90": percentile(orig_lens, 0.9),
        "final_len_median": median(final_lens),
        "final_len_p90": percentile(final_lens, 0.9),
        "orig_n_mean": mean([float(m["orig_n"]) for m in usable]),
        "final_n_mean": mean([float(m["final_n"]) for m in usable]),
        "final_n_median": median([float(m["final_n"]) for m in usable]),
        "final_total_median": median([m["final_total"] for m in usable]),
    }

    # 맥락 확장 — tier A(항상): 요청 클립 수. tier B(번들 있는 편만): 제안/실적용 초.
    ext_b = [m for m in usable if "ext_proposed_sec" in m]
    ext = {
        "requested_clips": sum(m["ext_requested_clips"] for m in usable),
        "requested_runs": sum(1 for m in usable if m["ext_requested_clips"] > 0),
        "tierb_runs": len(ext_b),
        "proposed_sec_total": round(sum(m["ext_proposed_sec"] for m in ext_b), 3),
        "applied_sec_total": round(sum(m["ext_applied_sec"] for m in ext_b), 3),
    }

    # 문장 절단 — 번들 있는 편만
    sc_rows = [m for m in usable if "last_cue_sentence_final" in m]
    b_rows = [m for m in usable if m.get("boundary_n")]
    sentence_cut = {
        "coverage_n": len(sc_rows),
        "last_cue_final_n": sum(1 for m in sc_rows if m["last_cue_sentence_final"]),
        "last_cue_final_pct": (
            round(sum(1 for m in sc_rows if m["last_cue_sentence_final"]) / len(sc_rows) * 100, 1)
            if sc_rows else None),
        "boundary_total": sum(m["boundary_n"] for m in b_rows),
        "boundary_cut_total": sum(m["boundary_cut_n"] for m in b_rows),
    }

    return {
        "n_rows": len(per_run),
        "n_usable": len(usable),
        "n_skipped": len(skipped),
        "skipped_keys": sorted(m.get("key", "") for m in skipped),
        "n_human_edited": sum(1 for m in usable if m["human_edited"]),
        "buckets": buckets,
        "finding2": finding2,
        "finding3": finding3,
        "lengths": lengths,
        "ext": ext,
        "sentence_cut": sentence_cut,
    }


def contamination_check(baselines: list[dict], per_run_index: dict[str, dict]) -> dict:
    """'최종' 오염 진단 — clip_metadata.edit_plan 은 편집 재렌더가 덮는다.

    editor_baselines(재렌더 전 AI 최종 스냅샷)의 클립 수와 지금 edit_plan 의 클립 수가
    다르면 그 편의 '최종'은 측정 시점에 따라 달라지는 값이다. §3 재현에서 최종 쪽
    수치(최종 평균·쪼갬)가 소폭 어긋나는 원인이 이것임을 실측으로 확인했다(2026-08-30:
    48편 대조 중 24편 덮임)."""
    checked = 0
    overwritten: list[str] = []
    for b in baselines:
        ai = b.get("ai_clips") or []
        rm = per_run_index.get(b.get("run_id", ""))
        if not ai or not rm:
            continue
        checked += 1
        if len(ai) != rm.get("final_n"):
            overwritten.append(b.get("run_id", ""))
    return {"checked": checked, "final_overwritten": len(overwritten),
            "runs": sorted(overwritten)}


def human_pairs(baselines: list[dict], per_run_index: dict[str, dict]) -> dict:
    """사람 편집 기준선 대조 — AI 최종 vs 사람 최종.

    쌍 성립 = clips 를 고친 edit_overrides 가 1건 이상인 run.
    AI 쪽 우선순위: editor_baselines 타임라인(재렌더 전 스냅샷 — 오염 불가)
    → 없으면 그 run 의 edit_plan(⚠ 재렌더로 사람 값이 덮였을 수 있다 — 사람 clips 와
    개수·경계가 0.05초 안에서 같으면 contaminated 로 센다). 문제 정의도 이 표는
    "방향만 참고"라고 못박는다 — 재현 검증의 1차 대상이 아니다."""
    pairs = []
    for b in baselines:
        edits = [e for e in (b.get("human_edits") or []) if e.get("clips")]
        if not edits:
            continue
        human_clips = edits[-1]["clips"]
        ai_clips = b.get("ai_clips") or []
        source = "baseline"
        contaminated = False
        if not ai_clips:
            rm = per_run_index.get(b.get("run_id", ""))
            if not rm:
                continue
            # edit_plan 폴백 — final_lens 만 있으면 길이 비교는 가능
            ai_lens = rm.get("final_lens") or []
            hu_lens = [clip_len(c) for c in human_clips]
            contaminated = (
                len(ai_lens) == len(hu_lens)
                and all(abs(a - h) <= 0.05 for a, h in zip(sorted(ai_lens), sorted(hu_lens))))
            pairs.append({
                "run_id": b.get("run_id", ""), "source": "edit_plan",
                "contaminated": contaminated,
                "ai_n": len(ai_lens), "ai_lens": ai_lens,
                "hu_n": len(hu_lens), "hu_lens": hu_lens,
            })
            continue
        pairs.append({
            "run_id": b.get("run_id", ""), "source": source, "contaminated": False,
            "ai_n": len(ai_clips), "ai_lens": [clip_len(c) for c in ai_clips],
            "hu_n": len(human_clips), "hu_lens": [clip_len(c) for c in human_clips],
        })

    ai_lens = [x for p in pairs for x in p["ai_lens"]]
    hu_lens = [x for p in pairs for x in p["hu_lens"]]
    return {
        "pairs_n": len(pairs),
        "contaminated_n": sum(1 for p in pairs if p["contaminated"]),
        "ai_n_mean": mean([float(p["ai_n"]) for p in pairs]),
        "ai_len_median": median(ai_lens),
        "ai_total": round(sum(ai_lens), 3),
        "hu_n_mean": mean([float(p["hu_n"]) for p in pairs]),
        "hu_len_median": median(hu_lens),
        "hu_total": round(sum(hu_lens), 3),
        "increased_n": sum(1 for p in pairs if p["hu_n"] > p["ai_n"]),
        "same_n": sum(1 for p in pairs if p["hu_n"] == p["ai_n"]),
        "decreased_n": sum(1 for p in pairs if p["hu_n"] < p["ai_n"]),
        "pairs": sorted(pairs, key=lambda p: p["run_id"]),
    }
