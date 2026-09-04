"""TTS cue 앵커 해석 · 대사 갭 스냅 — 내레이션 cue 의 시간 좌표를 정하는 두 함수.

v1 모놀리스(`app/pipeline.py`)에 있던 것을 V4-M1 §7 규약대로 물리 이동한 것이다.
⚠ **옮긴 이유**: v3 는 v1 모놀리스를 부를 수 없어(`_resolve_cue_anchors` 는 비공개였다)
앵커 해석·겹침 스냅(E19-3)을 승계하지 못했다. v4 는 **이 함수를 부른다** — 같은 판정을
두 번 적으면 언젠가 한쪽만 고쳐진다. 모놀리스는 `app.pipeline` 에서 같은 이름으로
재수출하고, 옛 비공개 이름 `_resolve_cue_anchors` 도 별칭으로 계속 부를 수 있다.

동작·상수는 이동 전과 **한 글자도 다르지 않다**(회귀 0 이 이동의 조건 —
`tests/test_cue_anchor_resolve.py`·`tests/test_e19_cue_overlap.py` 가 값으로 고정한다).

⚠ `RENDER_SAFETY_MARGIN_SEC` 은 **여기로 오지 않았다**. 그 상수는 E19-3 블록 옆에
적혀 있을 뿐 cue 판정이 아니라 **길이 클램프·narrative-ext 예산**이 쓰는 값이라
(pipeline 의 `_fit_storyline_to_duration` 계열) 그 코드 옆에 남는 것이 맞다.
"""

from __future__ import annotations

from typing import Any

# ─────────────────────────────────────────────────────────────
# TTS cue 앵커 해석 — 원본 절대시간 → 편집 타임라인 절대시간
# ─────────────────────────────────────────────────────────────
# cue 는 story 단계에서 (clip_index, offset_sec) 앵커로 정규화되고 source_time_sec
# (원본 영상 절대시간)를 갖는다. 클립 경계는 그 뒤 silence_cut(분할) → snap/extend/fill →
# 길이 클램프로 여러 번 바뀌므로, 절대시간 변환은 **최종 클립이 확정된 뒤 여기서 한 번만** 한다.
# 최종 클립 자체가 원본 구간(start_sec~end_sec)을 갖고 있어 "원본시간 → 편집시간" 조각별
# 매핑만으로 충분하다 — 첫 클립이 앞으로 확장돼도 cue 는 *화면 내용*에 붙어 따라온다.

_MIN_CUE_TAIL = 0.5  # 앵커 소재가 잘려나갔을 때 클립 끝에서 확보할 최소 여유


def resolve_cue_anchors(
    cues: list[dict],
    clips: list,  # 최종 variant StoryClip 리스트 (silence_cut·snap/extend/fill·클램프 반영 후)
) -> list[dict]:
    """앵커 cue 의 source_time_sec 를 최종 편집 타임라인 절대 start_sec/end_sec 로 변환.

    - source_time 을 포함하는 최종 클립 조각을 찾아 그 안의 상대 위치로 배치.
      같은 원본 구간이 여러 조각에 걸치면 앵커의 (chunk_index, candidate_index) 일치 우선.
    - 소재가 컷으로 빠졌으면: 같은 (chunk_index, candidate_index) 조각의 다음 kept 시작으로
      스냅, 그마저 없으면 마지막 조각 끝 - _MIN_CUE_TAIL. 앵커 클립이 통째로 제거됐으면 드롭.
    - end = start + duration_sec, 영상 전체 길이로 클램프.
    - 변환 후 시간순 정렬 + cue 간 겹침 제거 (뒤 cue 시작을 앞 cue 끝 + 0.05 로 이동).
    - source_time_sec 없는 cue (옛 체크포인트 재개 경로) 는 절대시간으로 간주하고
      영상 길이 클램프만 적용해 통과시킨다 (구 동작 보존).

    Returns: start_sec/end_sec 가 채워진 cue dict 리스트 (앵커 필드는 디버깅용 보존)
    """
    if not cues or not clips:
        return []

    # 편집 타임라인 조각: (원본 start, 원본 end, 편집 base, chunk_index, candidate_index)
    spans: list[tuple[float, float, float, int, int]] = []
    base = 0.0
    for c in clips:
        c_start = float(c.start_sec)
        c_end = float(c.end_sec)
        spans.append((c_start, c_end, base,
                      int(getattr(c, "chunk_index", -1)), int(getattr(c, "candidate_index", -1))))
        base += max(0.0, c_end - c_start)
    total = base

    out: list[dict] = []
    for cue in cues:
        duration = float(cue.get("duration_sec", 0.0) or 0.0)
        t_raw = cue.get("source_time_sec")

        if t_raw is None:
            # 구 스키마 (이미 절대시간) — 옛 체크포인트에서 재개된 경로. 클램프만.
            try:
                s = float(cue.get("start_sec"))
                e = float(cue.get("end_sec"))
            except (TypeError, ValueError):
                continue
            if e <= s or s >= total - 0.1:
                continue
            new_cue = dict(cue)
            new_cue["end_sec"] = min(e, total)
            out.append(new_cue)
            continue

        t = float(t_raw)
        key = (int(cue.get("chunk_index", -1)), int(cue.get("candidate_index", -1)))
        containing = [sp for sp in spans if sp[0] <= t < sp[1]]
        pick = None
        if containing:
            keyed = [sp for sp in containing if (sp[3], sp[4]) == key]
            pick = (keyed or containing)[0]
        else:
            # 앵커 지점의 소재가 컷/트림으로 빠짐 → 같은 앵커 클립 소재 안으로 스냅
            keyed = [sp for sp in spans if (sp[3], sp[4]) == key]
            after = [sp for sp in keyed if sp[0] >= t]
            if after:
                pick = min(after, key=lambda sp: sp[0])
                t = pick[0]
            elif keyed:
                pick = max(keyed, key=lambda sp: sp[1])
                t = max(pick[0], pick[1] - _MIN_CUE_TAIL)
            else:
                print(f"  [cue-resolve] 앵커 클립 소재가 최종 타임라인에 없음 → cue 드롭: "
                      f"{str(cue.get('text', ''))[:24]!r}")
                continue

        start = pick[2] + (t - pick[0])
        end = min(start + duration, total)
        if end - start < 0.3:
            print(f"  [cue-resolve] 변환 후 길이 {end - start:.2f}s < 0.3s → cue 드롭: "
                  f"{str(cue.get('text', ''))[:24]!r}")
            continue
        new_cue = dict(cue)
        new_cue["start_sec"] = start
        new_cue["end_sec"] = end
        out.append(new_cue)

    # 시간순 정렬 + 겹침 제거 (duration 유지한 채 뒤 cue 를 밀고, 영상 밖이면 드롭)
    out.sort(key=lambda x: x["start_sec"])
    resolved: list[dict] = []
    for cue in out:
        if resolved and cue["start_sec"] < resolved[-1]["end_sec"]:
            shift_to = resolved[-1]["end_sec"] + 0.05
            dur = cue["end_sec"] - cue["start_sec"]
            cue = dict(cue)
            cue["start_sec"] = shift_to
            cue["end_sec"] = min(shift_to + dur, total)
            if cue["end_sec"] - cue["start_sec"] < 0.3:
                print(f"  [cue-resolve] 겹침 보정 후 영상 밖 → cue 드롭: "
                      f"{str(cue.get('text', ''))[:24]!r}")
                continue
        resolved.append(cue)
    return resolved



# ─────────────────────────────────────────────────────────────
# E19-3: 내레이션 cue–대사 겹침 검사기 (2026-08-28)
# ─────────────────────────────────────────────────────────────
# 발주서: docs/prompts/e19-drama-clip-preset.md §3. 벤치마크(신병4·꿀벌무비 실측)의
# "끊김 없는 호흡"의 반은 내레이션이 대사와 절대 겹치지 않는 릴레이 문법이다 —
# 앵커가 어긋나면 TTS 가 원음 대사 위에 그대로 겹쳐 나가던 구멍을 여기서 막는다.
# 게이트는 톤 프로파일(narration.placement == "dialogue_gaps_only") — 미지정 채널은
# 이 검사 자체가 없다(회귀 0). 자리는 앵커 해석 직후·resources(비싼 합성) 앞.

# 릴레이는 경계가 맞닿는 문법이라(cue 가 대사 끝에 바로 붙는다) 관용치가 필요하다.
CUE_OVERLAP_TOLERANCE_SEC = 0.2


def snap_cues_to_dialogue_gaps(
    cues: list[dict],
    segments: list,          # 대사 자막 — start_sec/end_sec 속성 (final_segments)
    total_sec: float,
) -> tuple[list[dict], dict[str, Any]]:
    """대사와 겹치는 cue 를 가장 가까운 대사 gap 으로 스냅한다.

    - 겹침 합계가 CUE_OVERLAP_TOLERANCE_SEC 이하면 그대로 둔다.
    - 스냅은 **cue 길이가 통째로 들어가는 gap** 이 있을 때만 — 원위치에서 가장 가까운
      배치점을 고른다(gap 안에서 원래 시작에 최대한 붙인다). 이미 자리 잡은 다른 cue 의
      창도 점유물로 본다(스냅이 cue 끼리의 새 겹침을 만들면 안 된다).
    - 들어갈 gap 이 없으면 **옮기지 않고 경고만** 센다 — 멀쩡한 내레이션을 지우거나
      엉뚱한 자리로 보내는 것이 겹침보다 나쁘다(영상 밖 cue 안전망의 규율).
    - 시간이 깨진 cue 는 판정을 포기하고 그대로 싣는다(같은 규율).
    - 순수: 넘겨받은 cue 를 건드리지 않고 사본을 돌려준다.

    Returns: (새 cue 리스트(시간순), {"of", "cue_snapped", "warned", "details"}).
    details 항목: {text, from_sec, to_sec(실패 시 None), overlap_sec}.
    """
    report: dict[str, Any] = {"of": len(cues), "cue_snapped": 0, "warned": 0, "details": []}
    out = [dict(c) for c in cues]
    if not out:
        return out, report

    # 대사 구간 정리(클램프·병합) — 재료가 없으면 아무것도 안 한다.
    dialogue: list[tuple[float, float]] = []
    for seg in segments or []:
        try:
            s = float(getattr(seg, "start_sec"))
            e = float(getattr(seg, "end_sec"))
        except (TypeError, ValueError, AttributeError):
            continue
        s, e = max(0.0, s), min(float(total_sec), e)
        if e > s:
            dialogue.append((s, e))
    dialogue.sort()
    merged: list[list[float]] = []
    for s, e in dialogue:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if not merged or float(total_sec) <= 0:
        return out, report

    def _times(c: dict) -> tuple[float, float] | None:
        try:
            s, e = float(c["start_sec"]), float(c["end_sec"])
        except (TypeError, ValueError, KeyError):
            return None
        return (s, e) if e > s else None

    def _overlap_sec(s: float, e: float) -> float:
        return sum(max(0.0, min(e, de) - max(s, ds)) for ds, de in merged)

    for i, cue in enumerate(out):
        t = _times(cue)
        if t is None:
            continue
        s, e = t
        ov = _overlap_sec(s, e)
        if ov <= CUE_OVERLAP_TOLERANCE_SEC:
            continue
        cue_len = e - s
        # 점유물 = 대사 + (자기 자신을 뺀) 다른 cue 들의 현재 창
        occupied = [(ds, de) for ds, de in merged]
        for j, other in enumerate(out):
            if j == i:
                continue
            to = _times(other)
            if to is not None:
                occupied.append(to)
        occupied.sort()
        occ: list[list[float]] = []
        for os_, oe in occupied:
            if occ and os_ <= occ[-1][1]:
                occ[-1][1] = max(occ[-1][1], oe)
            else:
                occ.append([os_, oe])
        gaps: list[tuple[float, float]] = []
        prev = 0.0
        for os_, oe in occ:
            if os_ - prev >= cue_len - 1e-9:
                gaps.append((prev, os_))
            prev = max(prev, oe)
        if float(total_sec) - prev >= cue_len - 1e-9:
            gaps.append((prev, float(total_sec)))
        detail = {"text": str(cue.get("text", ""))[:24], "from_sec": round(s, 3),
                  "to_sec": None, "overlap_sec": round(ov, 2)}
        if not gaps:
            report["warned"] += 1
            report["details"].append(detail)
            continue
        best = min(gaps, key=lambda g: (abs(min(max(s, g[0]), g[1] - cue_len) - s), g[0]))
        new_s = min(max(s, best[0]), best[1] - cue_len)
        cue["start_sec"] = round(new_s, 3)
        cue["end_sec"] = round(new_s + cue_len, 3)
        report["cue_snapped"] += 1
        detail["to_sec"] = cue["start_sec"]
        report["details"].append(detail)

    # 시간순 정렬 — 깨진 cue 는 원래 자리 순서를 유지한 채 뒤로 보낸다.
    order = sorted(range(len(out)),
                   key=lambda i: (0, _times(out[i])[0]) if _times(out[i]) else (1, i))
    return [out[i] for i in order], report


# 옛 비공개 이름 — 모놀리스와 그 테스트(`tests/test_cue_anchor_resolve.py`)가 이 이름으로
# 부른다. 이동이 회귀 0 이려면 **같은 객체**여야 한다(별칭이지 감싼 함수가 아니다).
_resolve_cue_anchors = resolve_cue_anchors
