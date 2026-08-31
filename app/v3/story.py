"""Stage 3 — story. v3 의 심장부: 여기서부터 **시각 비접촉 구간**이다.

입력은 Stage 1+2 기록과 grid 뿐 — 영상은 다시 보지 않는다. Flash 가 span id 로만
비트(beat)를 편성하고, 확정 시각은 전부 grid lookup 이다(Stage 2 와 같은 구조 —
모델은 시각을 아예 출력하지 않는다).

역할 분담:
  모델(Flash · 텍스트 온리): 템플릿 선택 · 비트별 span id 연속 범위 · 카피
    (제목 2줄 · 내레이션 대본 · 괄호 라벨).
  코드(결정성): id 검증·반려(≤MAX_REASKS) · 길이 예산 다듬기(통삭제 금지 ·
    보호 목록 · arousal 은 동점 타이브레이커 ±0.5 상한 — §9-B 계약) ·
    TTS 슬롯 배치(ⓐ무성 → ⓑimportance≤3 뮤트 → ⓒ불가 시 드랍+기록) ·
    최소 1개 보장(재질의 소진 시 highlight 코드 폴백).

내레이션 견적은 7.5자/초(공백 제외 — recap 템플릿 실측). 실제 오디오는 resources
단계에서 합성·fit 되므로 여기 견적은 슬롯 크기 판정에만 쓴다.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.modules.gemini_client import (
    _extract_json_from_markdown,
    _loads_first_json,
)
from app.v3 import schemas
from app.v3.seq_analyze import MAX_REASKS

SCHEMA_STORY = "v3_story/v1"
TEMPLATES = ("recap_dialogue", "highlight")
STORY_TARGET_SEC = 53.0      # recap 템플릿 기준(레퍼런스 53s) — 채널 노브
STORY_MAX_SEC = 60.0         # 쇼츠 상한 아래 여유 — 초과분은 예산 다듬기가 던다
PIECES_MIN, PIECES_MAX = 6, 8   # 편성 조각 수 지향(합격 기준 분포 — soft)
NARRATION_CPS = 7.5          # 내레이션 자수/초(공백 제외)
NARRATION_MIN_SEC = 1.0      # 이보다 작은 슬롯은 슬롯이 아니다
AROUSAL_TIEBREAK_MAX = 0.5   # §9-B: arousal 은 보조지표 — importance 동점 근처에서만
MUTE_MAX_IMPORTANCE = 3      # ⓑ: 이 이하 유성 span 만 뮤트 후보(ⓒ: ≥4 는 절대 불가)
TITLE_MAX_CHARS = 16         # 상단 밴드 2줄 각각의 실측 상한(템플릿 폭 990px)
LOW_CONF = 0.5               # 이 아래 확신도는 재료 목록에 [저확신] 표기(M10-B)


# ── 재료 색인(순수) ─────────────────────────────────────────────────────────

def build_span_index(stage2_doc: dict, grid: dict) -> tuple[dict[str, dict], list[str]]:
    """분석된 chunk 의 span 들 → id 색인 + grid 순서 목록.

    Stage 3 의 재료는 **분석된 chunk 뿐**이다(커버리지 밖 span 을 편성하면 근거 없는
    컷이 된다). grid 의 t_in 순서가 곧 span 의 전역 순서다."""
    grid_order = [sp["id"] for sp in sorted(
        grid.get("span_candidates") or [], key=lambda s: (float(s["t_in"]), s["id"]))]
    grid_pos = {sid: i for i, sid in enumerate(grid_order)}

    index: dict[str, dict] = {}
    for sq in stage2_doc.get("sequences") or []:
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                for s in m.get("spans") or []:
                    sid = s.get("span_id")
                    if not isinstance(sid, str) or sid not in grid_pos:
                        continue
                    index[sid] = {
                        "t_in": schemas.parse_ts(s["time"]["start"]),
                        "t_out": schemas.parse_ts(s["time"]["end"]),
                        "is_audio": bool(s.get("is_audio")),
                        "importance": int(s.get("importance") or 3),
                        "audio_script": s.get("audio_script") or [],
                        # M9-C 판정 결과 — 자막 생성이 이걸 봐야 한다(리뷰 확정
                        # critical: 판정이 화면에 전파되지 않던 결함)
                        "text_source": s.get("text_source"),
                        "heard_text": s.get("heard_text") or "",
                        "conf": s.get("conf"),
                        "scene_script": s.get("scene_script") or "",
                        "meaning_content": m.get("content") or "",
                        "mood": m.get("mood") or "",
                        "pos": grid_pos[sid],
                    }
    order = sorted(index, key=lambda sid: index[sid]["pos"])
    return index, order


def arousal_adjust(arousal: list[dict], t0: float, t1: float) -> float:
    """구간 평균 arousal score → 타이브레이커 보정치(±AROUSAL_TIEBREAK_MAX 클램프).

    §9-B 계약: 전 장르 공통 피처의 z-합이라 크기를 믿지 않는다 — 방향만 쓰고
    상한으로 자른다. 포인트가 없으면 0(무보정)."""
    vals = [float(p["score"]) for p in arousal or []
            if t0 <= float(p.get("t", -1)) < t1 and isinstance(p.get("score"), (int, float))]
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    return max(-AROUSAL_TIEBREAK_MAX, min(AROUSAL_TIEBREAK_MAX, m * AROUSAL_TIEBREAK_MAX))


# ── 모델 응답 검증(순수) ────────────────────────────────────────────────────

def validate_story_response(resp: Any, span_index: dict[str, dict],
                            span_order: list[str]) -> tuple[dict | None, list[str], list[str]]:
    """모델 응답 → (정규화 스토리 | None, 반려 사유, 보정 노트).

    비트의 span_ids 는 **분석된 span 의 grid 연속 범위**여야 한다(부분 발췌·원거리
    결합은 비트를 나눠서 — 비트 하나 = 소스에서 이어지는 한 덩어리). 비트 간
    span 재사용 금지. 편성 순서는 자유다(원거리 결합)."""
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []

    template = resp.get("template")
    if template not in TEMPLATES:
        problems.append(f"template 은 {TEMPLATES} 중 하나: {template!r}")

    title = resp.get("title")
    if not isinstance(title, dict) or not str(title.get("line1") or "").strip() \
            or not str(title.get("line2") or "").strip():
        problems.append("title 은 {line1, line2} 두 줄 모두 필요")
        title = {"line1": "", "line2": ""}
    line1 = str(title.get("line1") or "").strip()
    line2 = str(title.get("line2") or "").strip()
    for name, line in (("line1", line1), ("line2", line2)):
        if len(line) > TITLE_MAX_CHARS:
            problems.append(f"title.{name} 이 {len(line)}자 — {TITLE_MAX_CHARS}자 이내로")

    beats_in = resp.get("beats")
    if not isinstance(beats_in, list) or not beats_in:
        return None, problems + ["beats 배열이 없다"], []

    pos_of = {sid: span_index[sid]["pos"] for sid in span_index}
    used: set[str] = set()
    beats: list[dict] = []
    for k, b in enumerate(beats_in):
        if not isinstance(b, dict):
            problems.append(f"beats[{k}] 가 객체가 아님")
            continue
        role = str(b.get("role") or "").strip() or "build"
        ids = b.get("span_ids")
        if not isinstance(ids, list) or not ids \
                or not all(isinstance(s, str) for s in ids):
            problems.append(f"beats[{k}] span_ids 는 문자열 id 배열이어야 한다")
            continue
        unknown = [s for s in ids if s not in pos_of]
        if unknown:
            problems.append(f"beats[{k}] 모르는/분석 밖 span id: {unknown[:5]} — "
                            "재료 목록의 id 로만 골라라")
            continue
        positions = [pos_of[s] for s in ids]
        if positions != sorted(positions) \
                or any(b2 - a2 != 1 for a2, b2 in zip(positions, positions[1:])):
            problems.append(f"beats[{k}] span_ids 가 grid 연속 범위가 아니다: "
                            f"{ids[0]}~{ids[-1]} — 떨어진 구간은 비트를 나눠라")
            continue
        reused = [s for s in ids if s in used]
        if reused:
            problems.append(f"beats[{k}] span 재사용: {reused[:5]} — 한 span 은 한 비트에만")
            continue
        used.update(ids)
        narration = b.get("narration")
        narration = str(narration).strip() if isinstance(narration, str) and \
            str(narration).strip() else None
        label = b.get("label")
        label = str(label).strip() if isinstance(label, str) and str(label).strip() else None
        if label and not (label.startswith("(") and label.endswith(")")):
            notes.append(f"beats[{k}] 라벨 괄호 보정: {label!r}")
            label = f"({label.strip('()')})"
        beats.append({"role": role, "span_ids": list(ids),
                      "narration": narration, "label": label})

    if template == "recap_dialogue" and beats \
            and not any(b["role"] == "climax" for b in beats):
        problems.append("recap_dialogue 는 climax 비트가 하나 필요하다")
    if problems:
        return None, problems, notes
    return {"template": template, "reason": str(resp.get("reason") or "").strip(),
            "title": {"line1": line1, "line2": line2}, "beats": beats}, [], notes


def story_duration(beats: list[dict], span_index: dict[str, dict]) -> float:
    return sum(span_index[s]["t_out"] - span_index[s]["t_in"]
               for b in beats for s in b["span_ids"])


# ── 길이 예산(순수) ─────────────────────────────────────────────────────────

def trim_to_budget(beats: list[dict], span_index: dict[str, dict],
                   arousal: list[dict], max_sec: float) -> list[dict]:
    """초과분을 비트 **가장자리에서만** 덜어낸다. 반환: 제거 로그.

    보호: climax 비트 전체 · importance 5 span · 비트의 마지막 남은 span(통삭제
    금지). 제거 순서 = importance + arousal 보정(±0.5)이 낮은 것부터 — 동점은
    긴 것부터(예산 회수 효율), 그다음 이른 시각(결정성)."""
    removed: list[dict] = []
    while story_duration(beats, span_index) > max_sec:
        cands: list[tuple[float, float, float, int, str]] = []
        for bi, b in enumerate(beats):
            if b["role"] == "climax" or len(b["span_ids"]) <= 1:
                continue
            for sid in (b["span_ids"][0], b["span_ids"][-1]):
                sp = span_index[sid]
                if sp["importance"] >= 5:
                    continue
                dur = sp["t_out"] - sp["t_in"]
                score = sp["importance"] + arousal_adjust(arousal, sp["t_in"], sp["t_out"])
                cands.append((score, -dur, sp["t_in"], bi, sid))
        if not cands:
            break   # 던 게 없다 — budget_unmet 은 호출자가 기록
        cands.sort()
        _score, _ndur, _t, bi, sid = cands[0]
        beats[bi]["span_ids"].remove(sid)
        sp = span_index[sid]
        removed.append({"beat": bi, "span_id": sid,
                        "sec": round(sp["t_out"] - sp["t_in"], 3),
                        "importance": sp["importance"]})
    return removed


# ── TTS 슬롯 배치(순수) ─────────────────────────────────────────────────────

def plan_narration_slots(beats: list[dict], span_index: dict[str, dict]) \
        -> tuple[list[dict], list[dict]]:
    """비트별 내레이션 → (cue 계획, 드랍 기록).

    규칙(발주서 §A-4): ⓐ비트 내 무성 span 런 위(기본) → ⓑ없으면 importance≤3
    유성 포함 런(해당 유성 span 뮤트) → ⓒimportance≥4 유성과는 절대 겹지 않는다 —
    창이 안 나오면 내레이션 드랍 + 기록(조용한 누락 금지).

    cue 의 source_time_sec = 창 시작(원본 절대초 — C2 신원 규약). 창이 견적보다
    작아도 NARRATION_MIN_SEC 이상이면 배치한다 — resources 의 fit 이 줄인다."""
    cues: list[dict] = []
    dropped: list[dict] = []
    for bi, b in enumerate(beats):
        text = b.get("narration")
        if not text:
            b["muted_span_ids"] = []
            continue
        est = max(NARRATION_MIN_SEC, len("".join(text.split())) / NARRATION_CPS)

        def runs(allow_mute: bool) -> list[list[str]]:
            """덮을 수 있는 span 의 **소스 연속** 런 — grid 인덱스 인접이어도 0.5s
            미만 전사 구멍으로 소스가 끊길 수 있다(적대 리뷰 확정: 창 끝이 구멍에
            떨어져 cue 소실+뮤트만 남는 재현). 구멍에서도 런을 끊는다."""
            out: list[list[str]] = []
            cur: list[str] = []
            for sid in b["span_ids"]:
                sp = span_index[sid]
                ok = (not sp["is_audio"]) or \
                    (allow_mute and sp["importance"] <= MUTE_MAX_IMPORTANCE)
                broken = bool(cur) and \
                    abs(sp["t_in"] - span_index[cur[-1]]["t_out"]) > 0.005
                if ok and not broken:
                    cur.append(sid)
                else:
                    if cur:
                        out.append(cur)
                    cur = [sid] if ok else []
            if cur:
                out.append(cur)
            return out

        def run_dur(run: list[str]) -> float:
            return sum(span_index[s]["t_out"] - span_index[s]["t_in"] for s in run)

        chosen: list[str] | None = None
        mode = None
        for allow_mute in (False, True):                      # ⓐ 먼저, 그다음 ⓑ
            fits = [r for r in runs(allow_mute) if run_dur(r) >= est]
            if fits:
                chosen, mode = fits[0], ("silent" if not allow_mute else "muted")
                break
        if chosen is None:                                    # 견적 미달 — 최장 런으로 fit
            all_runs = runs(True)
            if all_runs:
                longest = max(all_runs, key=lambda r: (run_dur(r), -span_index[r[0]]["pos"]))
                if run_dur(longest) >= NARRATION_MIN_SEC:
                    chosen, mode = longest, "fit"
        if chosen is None:                                    # ⓒ — 드랍 + 기록
            dropped.append({"beat": bi, "text": text,
                            "reason": "무성/뮤트 가능 창 없음(importance≥4 유성뿐)"})
            b["narration"] = None
            b["muted_span_ids"] = []
            continue
        w0 = span_index[chosen[0]]["t_in"]
        w1 = min(span_index[chosen[-1]]["t_out"], w0 + max(est, NARRATION_MIN_SEC))
        # 뮤트는 **창과 겹치는** 유성 span 만 — 런 전체 뮤트는 창 밖 대사까지
        # 무음으로 만들었다(적대 리뷰 확정: 내레이션도 대사도 없는 구간 재현)
        muted = [s for s in chosen if span_index[s]["is_audio"]
                 and span_index[s]["t_in"] < w1 - 0.01
                 and span_index[s]["t_out"] > w0 + 0.01]
        b["muted_span_ids"] = muted
        cues.append({"beat": bi, "text": text, "mode": mode,
                     "source_time_sec": round(w0, 3),
                     "source_end_sec": round(w1, 3),
                     "muted_span_ids": muted})
    return cues, dropped


def verify_tts_conflicts(cues: list[dict], beats: list[dict],
                         span_index: dict[str, dict]) -> list[str]:
    """벨트: cue 창이 뮤트 안 된 importance≥4 유성 span 과 겹치면 위반(0 이어야 한다)."""
    violations: list[str] = []
    for cue in cues:
        c0, c1 = cue["source_time_sec"], cue["source_end_sec"]
        muted = set(cue.get("muted_span_ids") or [])
        for b in beats:
            for sid in b["span_ids"]:
                sp = span_index[sid]
                if not sp["is_audio"] or sid in muted \
                        or sp["importance"] <= MUTE_MAX_IMPORTANCE:
                    continue
                if min(c1, sp["t_out"]) - max(c0, sp["t_in"]) > 0.01:
                    violations.append(f"cue(beat {cue['beat']}) ↔ {sid} "
                                      f"(importance {sp['importance']})")
    return violations


# ── 폴백 편성(순수) — 최소 1개 보장 ─────────────────────────────────────────

def fallback_highlight(span_index: dict[str, dict], span_order: list[str],
                       arousal: list[dict], target_sec: float,
                       work_title: str) -> dict:
    """재질의 소진 시 코드가 짓는 highlight 편성 — 카피 없이도 편집 가능한 최소.

    meaning importance 상위부터 그 meaning 의 span 연속 덩어리를 시각순으로 담는다.
    동점은 arousal 보정(±0.5) — 여기가 §9-B 의 '동점 타이브레이커' 소비처다."""
    # 그룹 = meaning content 가 같고 **grid 연속**인 런 — content 문자열만으로
    # 묶으면 동일 문구의 떨어진 meaning 이 병합돼 비연속 비트가 나온다(적대 리뷰
    # 확정: validate 였다면 반려될 편성을 폴백이 직접 생성).
    group_list: list[list[str]] = []
    for sid in span_order:
        if group_list and \
                span_index[group_list[-1][-1]]["meaning_content"] == span_index[sid]["meaning_content"] \
                and span_index[sid]["pos"] - span_index[group_list[-1][-1]]["pos"] == 1:
            group_list[-1].append(sid)
        else:
            group_list.append([sid])
    groups = {f"{span_index[ids[0]]['meaning_content']}#{i}": ids
              for i, ids in enumerate(group_list)}

    slot_sec = max(NARRATION_MIN_SEC, target_sec / PIECES_MIN)

    def core_run(ids: list[str]) -> list[str]:
        """meaning 의 span 런에서 최고 importance span 중심의 ~slot_sec 코어만.

        meaning 통째 편성은 드라이런에서 87s 한 덩어리를 낳았고, 트림도 importance 5
        보호에 막혀 줄이지 못했다 — 폴백은 애초에 코어만 담는다. 확장은 양옆 중
        (importance 높은 쪽, 동률이면 이른 쪽) — 결정성."""
        anchor = max(range(len(ids)),
                     key=lambda i: (span_index[ids[i]]["importance"], -i))
        lo = hi = anchor
        def dur(a: int, b: int) -> float:
            return sum(span_index[ids[i]]["t_out"] - span_index[ids[i]]["t_in"]
                       for i in range(a, b + 1))
        while dur(lo, hi) < slot_sec and (lo > 0 or hi < len(ids) - 1):
            left = span_index[ids[lo - 1]]["importance"] if lo > 0 else -1
            right = span_index[ids[hi + 1]]["importance"] if hi < len(ids) - 1 else -1
            if left >= right:
                lo -= 1
            else:
                hi += 1
        return ids[lo:hi + 1]

    scored = []
    for content, ids in groups.items():
        core = core_run(ids)
        t0, t1 = span_index[core[0]]["t_in"], span_index[core[-1]]["t_out"]
        imp = max(span_index[s]["importance"] for s in core)
        scored.append((-(imp + arousal_adjust(arousal, t0, t1)), t0, content, core))
    scored.sort()
    beats: list[dict] = []
    total = 0.0
    for _neg, t0, _content, ids in scored:
        # 조각 수 지향(PIECES_MIN)까지는 예산이 차도 계속 담는다 — 초과분은
        # trim_to_budget 이 가장자리에서 던다(한 덩어리 87s 편성이 나오던 드라이런 수정)
        if len(beats) >= PIECES_MAX or (total >= target_sec and len(beats) >= PIECES_MIN):
            break
        beats.append({"role": "build", "span_ids": list(ids),
                      "narration": None, "label": None})
        total += sum(span_index[s]["t_out"] - span_index[s]["t_in"] for s in ids)
    beats.sort(key=lambda b: span_index[b["span_ids"][0]]["t_in"])   # 시각순 편성
    return {"template": "highlight", "reason": "재질의 소진 — 코드 폴백(최소 1개 보장)",
            "title": {"line1": work_title, "line2": "하이라이트"}, "beats": beats}


# ── M10-C: 다안 심사 — 판단은 모델, 승자 선택은 코드(결정적) ────────────────

STORY_CANDIDATES = 3         # 한 호출에서 받는 안 개수(추가 LLM 호출 0)
RUBRIC_WEIGHTS = {           # 품질 사고 > 취향 — 실측이 만든 가중치
    "narration": 3.0,        # 내레이션 실현율(실측 0/3 드랍 사고)
    "material": 3.0,         # 재료 신뢰도(저확신 자막이 화면에 나간 사고)
    "cohesion": 1.5,         # 아크 응집도(원거리 짜집기 억제 — 사용자 지적)
    "progression": 1.0,      # 진행감(§9-D)
    "budget": 1.0,           # 예산 적합
    "intro": 1.0,            # 서론 금지(§9-D)
}
_GREETING = ("안녕", "반갑", "처음 뵙", "소개할게", "인사드리")


def score_story(story: dict, span_index: dict[str, dict], *,
                target_sec: float) -> dict:
    """안 하나 → 항목별 0~1 점수 + 총점. 순수·결정적.

    LLM 심사를 쓰지 않는다 — 검증자와 피검증자가 편향을 공유하면 안 된다(M9 원칙).
    항목은 전부 실측 사고에서 유래했다(주석의 사고 이름 참조)."""
    beats = story.get("beats") or []
    ids = [s for b in beats for s in b.get("span_ids") or []]
    voiced = [span_index[s] for s in ids
              if s in span_index and span_index[s]["is_audio"]]

    # ① 내레이션 실현율 — 계획한 내레이션 중 실제 슬롯을 얻는 비율
    planned = sum(1 for b in beats if b.get("narration"))
    probe = [dict(b, span_ids=list(b["span_ids"])) for b in beats]
    cues, dropped = plan_narration_slots(probe, span_index)
    if planned:
        narration = len(cues) / planned
    elif story.get("template") == "recap_dialogue":
        narration = 0.0        # 3:7 규약이 있는 템플릿에서 내레이션 0 = 규약 위반
    else:
        narration = 0.5        # highlight 등 — 실현율을 논할 대상이 아니다(중립)

    # ② 재료 신뢰도 — 채택 유성 span 의 확신도·판정 상태
    if voiced:
        ok = 0.0
        for sp in voiced:
            src, conf = sp.get("text_source"), sp.get("conf")
            if src == "none":
                continue                       # 대사 없음 = 0점
            if src == "heard":
                ok += 0.9                      # 확정 대사(청취) — 거의 만점
            elif conf is None:
                ok += 0.7                      # 미측정(구 문서) — 중립
            else:
                ok += min(1.0, max(0.0, (conf - 0.3) / 0.5))
        material = ok / len(voiced)
    else:
        material = 0.5                         # 대사 없는 편성 — 중립

    # ③ 아크 응집도 — 소스 시간 점프 횟수(비트 사이 불연속)
    starts = [span_index[b["span_ids"][0]]["t_in"] for b in beats
              if b.get("span_ids") and b["span_ids"][0] in span_index]
    ends = [span_index[b["span_ids"][-1]]["t_out"] for b in beats
            if b.get("span_ids") and b["span_ids"][-1] in span_index]
    jumps = sum(1 for a, b in zip(ends, starts[1:]) if abs(b - a) > 5.0)
    cohesion = max(0.0, 1.0 - jumps / max(1, len(beats) - 1))

    # ④ 진행감 — 비트 길이가 3초를 크게 넘는 구간 비율(자막·컷 이벤트 근사)
    long_beats = sum(1 for b in beats
                     if b.get("span_ids") and b["span_ids"][0] in span_index
                     and (span_index[b["span_ids"][-1]]["t_out"]
                          - span_index[b["span_ids"][0]]["t_in"]) > 12.0)
    progression = max(0.0, 1.0 - long_beats / max(1, len(beats)))

    # ⑤ 예산 적합
    total = story_duration(beats, span_index)
    budget = max(0.0, 1.0 - abs(total - target_sec) / max(1.0, target_sec))

    # ⑥ 서론 금지 — hook 첫 대사가 인사말인가
    intro = 1.0
    hook = next((b for b in beats if b.get("role") == "hook"), None)
    if hook:
        for sid in hook.get("span_ids") or []:
            sp = span_index.get(sid)
            if not sp or not sp["is_audio"]:
                continue
            line = " ".join(str(a.get("line") or "")
                            for a in sp.get("audio_script") or [])
            if any(g in line for g in _GREETING):
                intro = 0.0
            break

    parts = {"narration": narration, "material": material, "cohesion": cohesion,
             "progression": progression, "budget": budget, "intro": intro}
    total_score = sum(parts[k] * w for k, w in RUBRIC_WEIGHTS.items())
    return {"parts": {k: round(v, 3) for k, v in parts.items()},
            "score": round(total_score, 3),
            "total_sec": round(total, 2), "narration_dropped": len(dropped)}


def pick_best(cands: list[dict], span_index: dict[str, dict], *,
              target_sec: float) -> tuple[int, list[dict]]:
    """안 목록 → (승자 인덱스, 점수표). 동점은 낮은 인덱스(결정성)."""
    table = [{"index": i, **score_story(c, span_index, target_sec=target_sec)}
             for i, c in enumerate(cands)]
    best = max(range(len(table)), key=lambda i: (table[i]["score"], -i))
    return best, table


# ── 프롬프트·호출 ───────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """당신은 리캡 쇼츠 구성작가다. 아래 기록(전체 구조·의미 단위·span 목록)만으로 쇼츠 1편을 편성하라. 영상은 볼 수 없고, 볼 필요도 없다 — 기록이 정본이다. 시각은 절대 쓰지 않는다: **span id 로만** 말한다.

## 작품
{work_title}{research_block}

## 템플릿 (하나 선택)
- recap_dialogue(1호 — 기본): 8비트 구조. 내레이션:원본대사 ≈ 3:7. 비트 역할 = hook(내레이션 1문장, 제목과 호응) → conflict(대사 인용) → context(내레이션 배경 서술) → silent_break(자막·내레이션 없는 장면 1회 — 호흡) → climax(핵심 대사를 편집 없이 길게) → bridge(내레이션 1문장 전환) → reaction(상대 인물 대사) → ending(도전/떡밥 대사 직후 컷 — 아웃트로 없음).
- highlight(폴백): 역할 build 로 강한 순간만 시각순.

## 편성 규칙
1. 목표 {target_sec:.0f}초(상한 {max_sec:.0f}초) · 조각 {pieces_min}~{pieces_max}개.
2. 비트 하나 = **소스에서 이어지는 span 연속 범위 하나**. 떨어진 구간을 묶으려면 비트를 나눠라. 편성 순서는 자유(원거리 결합 가능)지만 이야기가 통해야 한다.
3. 한 span 은 한 비트에만. importance 높은 span 을 우선하되, 대사의 호흡(문장 시작~끝)을 자르지 마라.
4. 내레이션(narration)은 hook/context/bridge 비트에만 — 서술체(~했어요/~했죠), 문장당 2~4초, 그 비트의 무성 구간 위에 얹힌다(대사 위에 얹지 마라).
5. 라벨(label)은 "(팩폭 시전)" 식 괄호 심리 강조 — 꼭 필요한 비트에만 0~3개.
6. 제목 2줄: line1=상황(관계+사건), line2=펀치. 각 {title_max}자 이내.
7. 서론 금지 — 인사말·자기소개·상황 설명성 대사 span 은 hook 에 채택하지 않는다(후킹은 내레이션과 사건 한복판 대사의 몫이다).
8. 대사 신뢰 — `[대사없음]` span 은 **대사 인용 비트로 쓰지 마라**(무성 재료·장면으로는 가능). `[저확신 …]` 은 받아쓰기가 흔들린 구간이라 화면 자막이 깨질 수 있으니 가급적 피하고, 꼭 필요하면 그 비트의 다른 span 으로 대체하라. `[청취]` 는 확정된 대사다(그대로 써도 된다).
{reject_block}
## 재료 — 의미 단위와 span (id | 유성/무성 | importance | 내용)
{material_block}

## 후보 {n_cands}안을 내라 (중요)
서로 **다른 아크·다른 소재**로 {n_cands}개를 제안하라 — 같은 구간의 어절만 바꾼 안은 안 된다. 각 안은 위 규칙을 모두 지켜야 하고, 코드가 내레이션 실현 가능성·대사 신뢰도·응집도로 채점해 하나를 고른다. 그러니 "안전한 안"과 "과감한 안"을 섞어도 좋다.

## 출력 (JSON 만)
{{"candidates": [
 {{"template": "recap_dialogue", "reason": "선택 사유 한 문장",
   "title": {{"line1": "…", "line2": "…"}},
   "beats": [
    {{"role": "hook", "span_ids": ["sp0000", "sp0001"], "narration": "…" , "label": null}},
    {{"role": "climax", "span_ids": ["sp0102"], "narration": null, "label": "(…)"}}
   ]}}
]}}"""


def build_material_block(stage2_doc: dict, span_index: dict[str, dict]) -> str:
    """분석된 meaning 들을 시각순으로 — span 행은 모델이 고를 원자 목록."""
    lines: list[str] = []
    for sq in stage2_doc.get("sequences") or []:
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                lines.append(f"\n### [{m['time']['start']}~{m['time']['end']}] "
                             f"{m.get('content', '')} "
                             f"(importance {m.get('importance')} · {m.get('mood', '')} · "
                             f"{'/'.join(m.get('characters') or [])})")
                for s in m.get("spans") or []:
                    sid = s.get("span_id")
                    if sid not in span_index:
                        continue
                    if s.get("is_audio"):
                        speech = " / ".join(
                            f"{a.get('speaker')}: {a.get('line')}"
                            for a in s.get("audio_script") or [])
                        # M10-B: 신뢰 표기 — 모델이 못 미더운 대사를 피해 고르게
                        src = s.get("text_source")
                        conf = s.get("conf")
                        tag = ""
                        if src == "none" or not speech.strip():
                            tag = " [대사없음]"
                        elif src == "heard":
                            tag = " [청취]"
                        elif conf is not None and conf < LOW_CONF:
                            tag = f" [저확신 {conf:.2f}]"
                        lines.append(f"{sid} | 유성 | imp {s.get('importance')}"
                                     f"{tag} | {speech}")
                    else:
                        lines.append(f"{sid} | 무성 | imp {s.get('importance')} | "
                                     f"{s.get('scene_script', '')}")
    return "\n".join(lines)


def build_story_prompt(stage2_doc: dict, span_index: dict[str, dict], *,
                       work_title: str, research_context: str = "",
                       target_sec: float = STORY_TARGET_SEC,
                       max_sec: float = STORY_MAX_SEC,
                       reject_note: str = "") -> str:
    research_block = ""
    if research_context:
        research_block = "\n" + research_context.strip()[:800]
    reject_block = ""
    if reject_note:
        reject_block = f"\n## ⚠ 직전 제안 반려 사유 — 전부 고쳐서 다시 내라\n{reject_note}\n"
    return PROMPT_TEMPLATE.format(
        work_title=work_title, research_block=research_block,
        n_cands=STORY_CANDIDATES,
        target_sec=target_sec, max_sec=max_sec,
        pieces_min=PIECES_MIN, pieces_max=PIECES_MAX,
        title_max=TITLE_MAX_CHARS, reject_block=reject_block,
        material_block=build_material_block(stage2_doc, span_index))


def _call_story_model(gemini, prompt: str) -> dict:
    """Flash 텍스트 온리 — 모델 정책(Pro 는 영상 분석만) 그대로."""
    types = gemini.types
    response = gemini.client.models.generate_content(
        model=gemini.config.flash_model_name,
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=16384,
        ))
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
        raise ValueError(f"응답 JSON 파싱 실패: {e} — 앞 200자: {text[:200]!r}") from e


def _beat_doc(beats: list[dict], span_index: dict[str, dict]) -> list[dict]:
    """비트 → 문서 표기(시각은 grid lookup — 원본 절대초)."""
    out = []
    for i, b in enumerate(beats):
        first, last = b["span_ids"][0], b["span_ids"][-1]
        out.append({
            "number": i, "role": b["role"], "span_ids": list(b["span_ids"]),
            "time": {"start": schemas.format_ts(span_index[first]["t_in"]),
                     "end": schemas.format_ts(span_index[last]["t_out"])},
            "narration": b.get("narration"),
            "label": b.get("label"),
            "muted_span_ids": list(b.get("muted_span_ids") or []),
        })
    return out


def run_story(gemini, stage2_doc: dict, grid: dict, *, work_title: str,
              research_context: str = "",
              target_sec: float = STORY_TARGET_SEC,
              max_sec: float = STORY_MAX_SEC,
              log=print) -> tuple[dict, dict]:
    """Stage 3 실행 → (story 문서, 감사 기록). 실패해도 폴백으로 반드시 1개."""
    span_index, span_order = build_span_index(stage2_doc, grid)
    if not span_index:
        raise ValueError("분석된 span 이 없다 — Stage 2 가 선행돼야 한다")
    arousal = grid.get("arousal") or []
    audit: dict[str, Any] = {"attempts": [], "spans_available": len(span_index)}

    story: dict | None = None
    reject_note = ""
    for attempt in range(1 + MAX_REASKS):
        prompt = build_story_prompt(
            stage2_doc, span_index, work_title=work_title,
            research_context=research_context, target_sec=target_sec,
            max_sec=max_sec, reject_note=reject_note)
        log(f"  [v3/story] Flash 편성 요청 (시도 {attempt + 1}/{1 + MAX_REASKS}, "
            f"{STORY_CANDIDATES}안)")
        t0 = time.time()
        problems: list[str] = []
        notes: list[str] = []
        cands: list[dict] = []
        try:
            resp = _call_story_model(gemini, prompt)
            # M10-C: N안 수집 — 하나라도 통과하면 진행(전량 반려 시에만 재질의).
            # 구 응답(단일 안)도 그대로 받는다(하위호환).
            raw = resp.get("candidates") if isinstance(resp, dict) else None
            raw = raw if isinstance(raw, list) and raw else [resp]
            for k, one in enumerate(raw[:STORY_CANDIDATES]):
                st, pr, nt = validate_story_response(one, span_index, span_order)
                notes.extend(nt)
                if st is not None:
                    cands.append(st)
                else:
                    problems.extend(f"안{k}: {x}" for x in pr[:4])
        except ValueError as e:
            problems = [f"응답 오류: {e}"]
        rec = {"attempt": attempt + 1, "elapsed": round(time.time() - t0, 1),
               "candidates": len(cands), "problems": problems, "notes": notes}
        if cands:
            best, table = pick_best(cands, span_index, target_sec=target_sec)
            story = cands[best]
            rec["scores"] = table
            rec["winner"] = best
            audit["attempts"].append(rec)
            audit["scores"] = table
            audit["winner"] = best
            log(f"  [v3/story] {len(cands)}안 심사 → 안{best} 채택 "
                f"(점수 {table[best]['score']} · "
                + " · ".join(f"{k} {v}" for k, v in table[best]["parts"].items()) + ")")
            break
        audit["attempts"].append(rec)
        log(f"  [v3/story] 반려 — 사유 {len(problems)}건")
        reject_note = "\n".join(f"- {p}" for p in problems[:20])

    if story is None:
        log("  [v3/story] ⚠ 재질의 소진 — highlight 코드 폴백(최소 1개 보장)")
        story = fallback_highlight(span_index, span_order, arousal,
                                   target_sec, work_title)
        audit["fallback"] = True

    beats = story["beats"]
    total_before = story_duration(beats, span_index)
    removed = trim_to_budget(beats, span_index, arousal, max_sec)
    beats = [b for b in beats if b["span_ids"]]          # 방어 — 통삭제는 규칙상 없음
    total_after = story_duration(beats, span_index)
    budget_unmet = total_after > max_sec

    cues, dropped = plan_narration_slots(beats, span_index)
    conflicts = verify_tts_conflicts(cues, beats, span_index)
    if conflicts:
        # 배치 규칙상 나올 수 없다 — 나오면 코드 결함이므로 크게 실패(조용한 송출 금지)
        raise AssertionError(f"TTS-대사 충돌 벨트 위반: {conflicts}")

    doc = {
        "schema": SCHEMA_STORY,
        "template": story["template"],
        "reason": story.get("reason", ""),
        "title": story["title"],
        "beats": _beat_doc(beats, span_index),
        "narration_cues": cues,
        "narration_dropped": dropped,
        "budget": {"target_sec": target_sec, "max_sec": max_sec,
                   "total_before_sec": round(total_before, 3),
                   "total_after_sec": round(total_after, 3),
                   "removed": removed, "unmet": budget_unmet},
    }
    audit["tts_conflicts"] = 0
    audit["pieces"] = len(beats)
    if budget_unmet:
        log(f"  [v3/story] ⚠ 예산 미달성 — 던 뒤에도 {total_after:.1f}s > {max_sec}s "
            "(보호 span 만 남음, run_log 기록)")
    return doc, audit
