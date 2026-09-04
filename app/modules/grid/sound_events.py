"""격자에서 **대사 없는 소리 사건**을 뽑는다 — 문 쾅·웃음·한숨·타격음.

왜 필요한가(2026-09-04): 후보 편성 프롬프트가 모델에게 주는 것은 전사(대사 글자)와
격자 요약(발화 분포·무발화 30초+ 구간·장면 전환)뿐이다. 그래서 **대사가 없는 소리는
존재 자체가 모델에게 보이지 않는다** — 문이 쾅 닫히든 좌중이 폭소하든 전사에는 한
글자도 없고 요약에도 안 나온다. 「티키타카 편집점 지침서」의 모드 [A](현장음 턴)를
우리가 만들 수 없던 이유가 그것이다.

이 모듈은 **관찰만 한다** — 판정은 코드가, 선택은 뒷단계가 한다(M9). 격자에 이미
있는 재료(`arousal` · `span_candidates`)만 읽고 새로 계산하지 않는다.

## 판별 규칙 (실측으로 정했다 — 신병4 EPK 33분)

기준선은 **그 소재의 유성 span 최대에너지 중앙값**이다. 절대 dB 를 쓰면 마스터링
레벨이 다른 소재에서 전부 걸리거나 전부 빠진다 — 같은 편 안의 말소리를 자로 쓴다.

  · 무성(`is_audio == false`) span 이어야 한다 — 대사가 있으면 그건 [S] 턴이다.
  · 최대 에너지 ≥ 기준선 + `PEAK_MARGIN_DB`.
  · 길이 `MIN_SEC` ~ `MAX_SEC`.

⚠ **길이 상한이 핵심 판별자다.** 실측에서 임계를 넘긴 35건 중 긴 것(13.1s · 16.9s)은
전부 배경 TV·음악이었고(1033초 구간은 일기예보 방송음), 짧은 것(0.9~2.6s)이 리액션·
타격음이었다. 지침서의 [A]도 짧다("컵을 쾅 내려놓는 소리 1.5초 · 정적 2.0초").

임계별 실측(병합 후): 기준+0dB 110건 · +1.5dB 61건 · +3.0dB 35건 · +4.5dB 17건.
33분에 35건(분당 약 1건)이 프롬프트에 실을 만한 밀도라 +3.0 을 골랐다.

## 검출된 것이 실제로 무엇인가 (원본 PCM 실측 · 상위 12건)

정직하게 적는다 — **대부분은 충격음이 아니다.** 스펙트럼 중심주파수와 저역 비율로:

  · 저역 충격음(문·발소리·타격) 1건 — 512.6s(중심 618Hz · 저역비 0.65)
  · 고역 잡음성(환호·박수·웃음) 2건 — 197.5s · 211.7s(중심 2.2~2.3kHz)
  · 중역(음악·현장 소음·말소리 잔향) 9건

즉 이 모듈이 내는 것은 "**대사 없이 소리가 큰 구간**"이지 "타격음"이 아니다. 이름이
약속하는 것 이상을 주장하지 않는다 — 그중 무엇이 쓸 만한지는 화면을 보는 모델이
고르고(M9: 코드가 관찰·모델이 선택·코드가 판정), 고른 결과는 6c·7·8이 다시 본다.
충격음만 남기려면 스펙트럼 피처가 필요한데 그것은 오디오 재디코드라 별건이다.

⚠ 표본은 **한 편**이다. 다른 장르(음악 예능·야외 버라이어티)에서 다시 재야 한다.

## 깔때기와의 관계 (실측)

소리 사건 조각은 발화가 없어 7단계 `speech_coverage` 를 **낮춘다**. 실측(승인 후보
6개에 1.5s·3.0s 조각을 하나씩 얹음): 커버리지 −0.01~−0.02 로 6/6 이 게이트(0.55)
위에 남았다. 다만 이미 게이트에 가까운 후보(0.589)는 밀려날 수 있다 — 그래서
프롬프트 절이 "편당 1~2개면 충분하다"고 못박는다. 대사가 주인 쇼츠라 이 우선순위는
의도된 것이다.
"""

from __future__ import annotations

__all__ = ["PEAK_MARGIN_DB", "MIN_SEC", "MAX_SEC", "MERGE_GAP_SEC",
           "voiced_peak_baseline", "detect_sound_events"]

# 유성 span 최대에너지 중앙값 위로 이만큼 커야 '사건'이다. 실측 §머리말 참조.
PEAK_MARGIN_DB = 3.0
# 이보다 짧으면 프레임 한 장 수준이라 편집점이 못 된다(arousal 은 0.5s hop 이다).
MIN_SEC = 0.4
# 이보다 길면 사건이 아니라 **배경**이다(실측: 13~17초 구간은 전부 TV·음악).
MAX_SEC = 6.0
# 이 안으로 붙어 있는 span 은 한 사건으로 본다(재단이 나눠 놓았을 뿐이다).
MERGE_GAP_SEC = 0.05


def _peaks(arousal: list[dict], t0: float, t1: float) -> tuple[float, float] | None:
    """구간 [t0, t1) 의 (최대 energy_db, 최대 dynamics). 표본이 없으면 None. 순수."""
    best_e = None
    best_d = 0.0
    for p in arousal or []:
        t = p.get("t")
        if not isinstance(t, (int, float)) or not (t0 <= float(t) < t1):
            continue
        e = p.get("energy_db")
        if isinstance(e, (int, float)):
            best_e = float(e) if best_e is None else max(best_e, float(e))
        d = p.get("dynamics")
        if isinstance(d, (int, float)):
            best_d = max(best_d, float(d))
    return None if best_e is None else (best_e, best_d)


def voiced_peak_baseline(grid: dict) -> float | None:
    """유성 span 최대에너지의 **중앙값** — 그 소재 안에서의 '말소리 크기' 자.

    None 이면 잴 재료가 없다는 뜻이고, 그때는 검출을 하지 않는다(오판 금지)."""
    arousal = grid.get("arousal") or []
    peaks = []
    for s in grid.get("span_candidates") or []:
        if not s.get("is_audio"):
            continue
        got = _peaks(arousal, float(s["t_in"]), float(s["t_out"]))
        if got is not None:
            peaks.append(got[0])
    if not peaks:
        return None
    peaks.sort()
    n = len(peaks)
    return peaks[n // 2] if n % 2 else (peaks[n // 2 - 1] + peaks[n // 2]) / 2.0


def detect_sound_events(grid: dict, *, margin_db: float = PEAK_MARGIN_DB,
                        min_sec: float = MIN_SEC, max_sec: float = MAX_SEC,
                        limit: int | None = None) -> list[dict]:
    """격자 → 대사 없는 소리 사건 목록. 순수·결정적.

    반환은 **시각순**이고 항목은 `{start_sec, end_sec, peak_db, transient_db,
    over_db}` 다. `over_db` 는 기준선을 얼마나 넘었는가(세기) — 상한(`limit`)으로
    자를 때의 정렬 열쇠이자 사람이 읽을 근거다.

    `limit` 은 **센 것부터** 남기고 그다음 다시 시각순으로 돌려준다. 자른 건수는
    부르는 쪽이 알 수 있도록 세어서 쓰라 — 여기서 조용히 버리지 않는다."""
    base = voiced_peak_baseline(grid)
    if base is None:
        return []
    arousal = grid.get("arousal") or []
    threshold = base + float(margin_db)

    raw: list[dict] = []
    for s in grid.get("span_candidates") or []:
        if s.get("is_audio"):
            continue
        t0, t1 = float(s["t_in"]), float(s["t_out"])
        if t1 - t0 < min_sec:
            continue
        got = _peaks(arousal, t0, t1)
        if got is None or got[0] < threshold:
            continue
        raw.append({"start_sec": t0, "end_sec": t1,
                    "peak_db": got[0], "transient_db": got[1]})

    # 재단이 나눠 놓은 이웃을 한 사건으로 되붙인다.
    merged: list[dict] = []
    for ev in raw:
        if merged and ev["start_sec"] - merged[-1]["end_sec"] <= MERGE_GAP_SEC:
            prev = merged[-1]
            prev["end_sec"] = ev["end_sec"]
            prev["peak_db"] = max(prev["peak_db"], ev["peak_db"])
            prev["transient_db"] = max(prev["transient_db"], ev["transient_db"])
        else:
            merged.append(dict(ev))

    out = []
    for ev in merged:
        if ev["end_sec"] - ev["start_sec"] > max_sec:
            continue                      # 사건이 아니라 배경이다(머리말 실측)
        out.append({"start_sec": round(ev["start_sec"], 3),
                    "end_sec": round(ev["end_sec"], 3),
                    "peak_db": round(ev["peak_db"], 1),
                    "transient_db": round(ev["transient_db"], 1),
                    "over_db": round(ev["peak_db"] - base, 1)})

    if limit is not None and len(out) > int(limit):
        # 센 것부터 남긴다 — 동점은 이른 시각(결정성).
        out.sort(key=lambda e: (-e["over_db"], e["start_sec"]))
        out = out[:int(limit)]
        out.sort(key=lambda e: e["start_sec"])
    return out
