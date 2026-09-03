"""정본 격자(grid) — 이후 모든 시각의 유일한 출처(기획서 §1 주춧돌).

재료 4종(전사 단어·장면 전환·유/무성·arousal)을 합쳐 span 후보를 재단한다.

span 재단 규칙(새 규칙 발명 금지 — 전부 기존 수치 재사용):
  - **유성 span** = 발화 묶음. 묶음 규칙은 `stt_elevenlabs.words_to_segments`
    **그 함수를 그대로 호출**한다(0.5s 공백 · 문장 종결부호 · 44자 · 6.0s —
    E11 word→cue 정본. 발주서의 '40자'는 파이프라인 merge 의 config 값이고
    word→cue 정본 상수는 44 다, E14 곁다리 기록). time_authority="stt".
  - **무성 span** = 유성 span 사이 공백(≥ 0.5s = `_CUE_GAP_SEC`)을 장면 전환으로
    재단. 조각이 6.0s(`_CUE_MAX_DURATION_SEC`)를 넘으면 무음 경계(silencedetect
    전환점) → 그래도 넘으면 등분 순서로 더 쪼갠다(폴백: 문장 → 장면 → beat 의
    격자판 대응). time_authority="scene".
  - 전사 실패 창은 단어가 없으므로 자연히 무성 취급(scene 폴백)이 되고,
    grid.transcript.failed_windows 와 run_log 에 남는다(조용한 뭉갬 금지).

순수 함수만 — 파일 I/O 는 pipeline 쪽. 같은 입력 → 바이트까지 같은 산출(결정성).
"""
from __future__ import annotations

from app.modules.stt_elevenlabs import (
    _CUE_GAP_SEC,
    _CUE_MAX_DURATION_SEC,
    words_to_segments,
)

SCHEMA_GRID = "v3_grid/v1"
MIN_UNVOICED_SEC = _CUE_GAP_SEC            # 0.5 — 이보다 짧은 틈은 호흡이지 조각이 아니다
MAX_UNVOICED_SEC = _CUE_MAX_DURATION_SEC   # 6.0 — 유성 cue 상한과 같은 자
EDGE_MARGIN_SEC = 0.2                      # 무성 재단 시 경계에 너무 붙은 컷은 무시(슬리버 방지)


def group_words_to_cues(words: list[dict]) -> list[dict]:
    """v3 단어({t0,t1,text,prob}) → 유성 cue 목록 [{t_in,t_out,text}].

    묶음은 stt_elevenlabs.words_to_segments 를 **그대로** 태운다 — 같은 코드가
    같은 규칙을 집행해야 한쪽만 고쳐지는 사고가 없다(E15 규율)."""
    el_shaped = [{"type": "word", "text": w["text"], "start": w["t0"], "end": w["t1"],
                  "logprob": w.get("prob", 0.0)} for w in words]
    cues, _conf = words_to_segments(el_shaped)
    return [{"t_in": round(c.start_sec, 3), "t_out": round(c.end_sec, 3), "text": c.text}
            for c in cues]


def _split_points(a: float, b: float, cuts: list[float]) -> list[float]:
    """[a, b] 안쪽에서 쓸 수 있는 절단점(경계 여유 EDGE_MARGIN_SEC 확보)."""
    return [c for c in cuts if a + EDGE_MARGIN_SEC < c < b - EDGE_MARGIN_SEC]


def _carve_region(a: float, b: float, scene_cuts: list[float],
                  silence_edges: list[float]) -> list[tuple[float, float]]:
    """무성 구간 하나를 조각으로 — 장면 전환 → 무음 경계 → 등분 순."""
    pts = sorted(set(_split_points(a, b, scene_cuts)))
    pieces: list[tuple[float, float]] = []
    prev = a
    for p in pts + [b]:
        if p - prev >= MIN_UNVOICED_SEC:
            pieces.append((prev, p))
            prev = p
        # 슬리버(< 0.5s)면 절단점을 버리고 다음 조각에 흡수
    if prev < b and (not pieces or pieces[-1][1] != b):
        if b - prev >= MIN_UNVOICED_SEC:
            pieces.append((prev, b))
        elif pieces:
            pieces[-1] = (pieces[-1][0], b)     # 꼬리 슬리버는 마지막 조각에 붙인다
        else:
            return []                            # 구간 전체가 슬리버 — 조각 없음

    out: list[tuple[float, float]] = []
    for s, e in pieces:
        if e - s <= MAX_UNVOICED_SEC:
            out.append((s, e))
            continue
        sub_pts = sorted(set(_split_points(s, e, silence_edges)))
        sub: list[tuple[float, float]] = []
        sp = s
        for p in sub_pts + [e]:
            if p - sp >= MIN_UNVOICED_SEC:
                sub.append((sp, p))
                sp = p
        if sp < e:
            if sub:
                sub[-1] = (sub[-1][0], e)
            else:
                sub = [(s, e)]
        for ss, ee in sub:
            if ee - ss <= MAX_UNVOICED_SEC:
                out.append((ss, ee))
            else:                                # 등분 — 마지막 폴백(결정적)
                n = int((ee - ss) // MAX_UNVOICED_SEC) + 1
                step = (ee - ss) / n
                for i in range(n):
                    out.append((ss + i * step, ee if i == n - 1 else ss + (i + 1) * step))
    return out


def carve_spans(words: list[dict], scene_cuts: list[float],
                silence: list[tuple[float, float]], duration_sec: float) -> list[dict]:
    """재료 → span 후보 목록(t_in 오름차순, id 부여). 입력 불변(순수)."""
    cues = group_words_to_cues(words)
    silence_edges = sorted({round(t, 3) for pair in silence for t in pair})

    spans: list[dict] = []
    for c in cues:
        spans.append({"t_in": c["t_in"], "t_out": c["t_out"], "is_audio": True,
                      "time_authority": "stt", "text": c["text"]})

    # 유성 span 의 여집합 = 무성 후보 구간
    prev_end = 0.0
    voiced = sorted(((c["t_in"], c["t_out"]) for c in cues))
    regions: list[tuple[float, float]] = []
    for s, e in voiced:
        if s - prev_end >= MIN_UNVOICED_SEC:
            regions.append((prev_end, s))
        prev_end = max(prev_end, e)
    if duration_sec - prev_end >= MIN_UNVOICED_SEC:
        regions.append((prev_end, duration_sec))

    for a, b in regions:
        for s, e in _carve_region(a, b, scene_cuts, silence_edges):
            spans.append({"t_in": round(s, 3), "t_out": round(e, 3), "is_audio": False,
                          "time_authority": "scene", "text": ""})

    spans.sort(key=lambda x: (x["t_in"], x["t_out"]))
    for i, sp in enumerate(spans):
        sp["id"] = f"sp{i:04d}"
    # 계약 키 순서 고정(결정성 — dict 순서가 곧 파일 바이트)
    return [{"id": sp["id"], "t_in": sp["t_in"], "t_out": sp["t_out"],
             "is_audio": sp["is_audio"], "time_authority": sp["time_authority"],
             "text": sp["text"]} for sp in spans]


def build_grid_doc(*, source: dict, words: list[dict], scene_cuts: list[float],
                   silence: list[tuple[float, float]], arousal: list[dict],
                   span_candidates: list[dict], transcript_meta: dict,
                   srt_cues: list[dict] | None = None) -> dict:
    """grid.json 본문 — 발주서 골격의 top 키(words/scene_cuts/arousal/span_candidates)
    를 그대로 쓰고, 유/무성·전사 메타를 보조 키로 싣는다."""
    doc = {
        "schema": SCHEMA_GRID,
        "source": source,
        "transcript": transcript_meta,
        "words": words,
        "scene_cuts": [round(t, 3) for t in scene_cuts],
        "silence": [[round(a, 3), round(b, 3)] for a, b in silence],
        "arousal": arousal,
        "span_candidates": span_candidates,
    }
    if srt_cues is not None:
        doc["srt_cues"] = srt_cues
    return doc


def grid_snap_times(grid: dict) -> list[float]:
    """Stage 1 스냅 어휘 — span 경계 ∪ 장면 전환 ∪ {0, 러닝타임}."""
    times = {0.0, float(grid["source"]["duration_sec"])}
    times.update(float(t) for t in grid["scene_cuts"])
    for sp in grid["span_candidates"]:
        times.add(float(sp["t_in"]))
        times.add(float(sp["t_out"]))
    return sorted(round(t, 3) for t in times)
