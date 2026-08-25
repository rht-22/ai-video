"""더빙 — route C·BC 의 뒷단 (L-P4 이식). 일본어 자막 → TTS 초안.

원본: video-localization-project `src/dub.py`. **충실히 이식**했다 — 페이싱·리타이밍·
백체크 임계값이 잔망루피 목소리의 정체라 한 숫자도 바꾸지 않았다.

⚠ **overlay 파이프라인이 이 단계를 부르지 않는다**(vlp 규약 그대로). `process_video`
머리말대로 검수 게이트를 지난 뒤 따로 돈다 — `runner.needs_dub` 이 뒤따를 단계가
있는지 알린다.

⚠ TTS 백엔드(XTTS·GPT-SoVITS·ElevenLabs)·전사(faster-whisper)·보컬 분리(demucs)는
**전부 지연 임포트**다. requirements 를 안 건드렸고, 없는 백엔드는 config 로 고른다.

원문 머리말:
선별 더빙 (C-9) — Level C 한정. 일본어 자막 → TTS 초안.

ja 자막(ja.srt/ja.ass)을 입력으로 ElevenLabs 등 TTS 호출, persona 보이스 디렉션 반영.
한국 성우 클로닝이 아니라 "일본 캐릭터 보이스 디렉션" 기본.
출력: outputs/{video_id}/dub_ja_draft.wav + alignment_report.json.
⚠ retention 리스크·hero 영상은 사람/성우 검토. 자동 영상 합성·게시 금지(드래프트 오디오까지만).

[Level C 가드] level != C 면 거부. 자막 파싱·정렬 리포트는 순수, TTS/ffmpeg 만 외부.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# ⚠ vlp 는 `src/dub.py` 라 레포 루트를 sys.path 에 넣었다. 여기는 패키지 안이라 필요 없다
# (아래 GPT-SoVITS 저장소 경로 주입은 그 저장소를 임포트하려는 것이라 **남긴다**).

from pathlib import Path  # noqa: E402
from typing import Any, Optional  # noqa: E402

from app.localize.overlay import common  # noqa: E402
from app.localize.overlay.common import ensure_dir, get_logger, get_secret, load_config, read_json, resolve_path, write_json  # noqa: E402
from app.localize.overlay import DUB_ROUTES  # noqa: E402
from app.localize.overlay.cuts import apply_cuts_to_events, cut_total, shift_time, validate_cuts  # noqa: E402

log = get_logger("dub")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def require_level_c(level: str) -> None:
    """더빙이 뒤따르는 route 인가. 아니면 거부.

    ⚠ vlp 원본은 `level != "C"` 로 **C 만** 통과시켰다(`src/dub.py:31`). 그런데 같은
    레포의 `DUB_ROUTES` 는 C·BC 둘이고 오케스트레이터 어댑터의 `needs_dub` 도 그렇다 —
    즉 BC 편은 "더빙이 뒤따른다"고 표시된 채 더빙 단계에서 거부당한다. BC 가 실제로
    돌아 본 적이 없어(vlp·이식본 모두) 안 드러난 불일치다. 세 곳이 갈리면 더빙이 빠진
    편이 더빙된 줄 알고 발행된다(2026-08-12 에 실제로 난 사고와 같은 모양) — 정본을
    `DUB_ROUTES` 하나로 모은다."""
    if str(level or "").upper() not in DUB_ROUTES:
        raise ValueError(f"더빙은 route {'·'.join(DUB_ROUTES)} 한정. 현재 level={level}. "
                         f"게이트/route 확인.")


def _srt_time(t: str) -> float:
    h, m, rest = t.split(":")
    s, _, ms = rest.replace(".", ",").partition(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(ms) / 1000 if ms else 0.0)


def _ass_time(t: str) -> float:
    h, m, rest = t.split(":")
    s, _, cs = rest.partition(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(cs) / 100 if cs else 0.0)


def parse_segments(subtitle_path: str) -> list[dict[str, Any]]:
    """ja.srt 또는 ja.ass → [{start,end,text}] (초)."""
    text = Path(subtitle_path).read_text(encoding="utf-8")
    segs: list[dict[str, Any]] = []
    if subtitle_path.endswith(".ass"):
        for line in text.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            fields = line[len("Dialogue:"):].split(",", 9)
            if len(fields) < 10:
                continue
            body = re.sub(r"\{[^}]*\}", "", fields[9]).replace("\\N", " ").strip()
            if body:
                segs.append({"start": _ass_time(fields[1].strip()),
                             "end": _ass_time(fields[2].strip()), "text": body})
    else:  # SRT
        for block in re.split(r"\n\s*\n", text.strip()):
            lines = [ln for ln in block.splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            tl = next((ln for ln in lines if "-->" in ln), None)
            if not tl:
                continue
            start, _, end = tl.partition("-->")
            body = " ".join(lines[lines.index(tl) + 1:]).strip()
            if body:
                segs.append({"start": _srt_time(start.strip()),
                             "end": _srt_time(end.strip()), "text": body})
    segs.sort(key=lambda s: s["start"])
    return segs


def atempo_filters(speed: float) -> str:
    """ffmpeg atempo 는 0.5~2.0 만 지원 → 필요한 배속을 체인으로 분해한 필터 문자열."""
    speed = max(0.25, min(4.0, speed))
    parts: list[str] = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


# ── 더빙 견고화 순수 로직 (싱크 / 환각방지 / 클리핑) ──────────────────────
def _fit_speed(dur: float, target: float, max_speedup: float = 1.6,
               min_slowdown: float = 0.7) -> float:
    """슬롯에 맞추기 위한 배속 비율. 과속(max_speedup)·과늘림(min_slowdown) 클램프."""
    if dur <= 0 or target <= 0:
        return 1.0
    speed = dur / target
    return min(speed, max_speedup) if speed > 1 else max(speed, min_slowdown)


def pacing_plan(natural: float, cap: float, max_speedup: float = 1.35) -> tuple[float, float]:
    """자연 속도 우선 페이싱 — (배속, 결과 길이).

    원본 보컬을 제거하는 더빙은 입싱크가 필요 없다 → 자막 슬롯에 억지로 압축하지 않고
    (실측 1.68x 배속 = '말이 빠르다' 피드백, 2026-07-09), 다음 대사 침범선(cap)만 지킨다.
    cap 안이면 그대로(1.0x), 넘으면 max_speedup 까지만 압축(잔여는 _fit_audio 캡이 페이드 컷)."""
    if natural <= 0 or cap <= 0:
        return 1.0, max(natural, 0.0)
    if natural <= cap:
        return 1.0, natural
    return min(natural / cap, max_speedup), natural / min(natural / cap, max_speedup)


def char_budget(slot_sec: float, chars_per_sec: float = 5.5, min_chars: int = 8) -> int:
    """더빙용 번역 길이 예산(문자 수) — 원본 슬롯 초수 × 합성 발화 속도.

    번역이 슬롯 대비 길면 어떤 페이싱으로도 빨라진다(근본 원인) → 번역 단계에서 제한."""
    return max(min_chars, int(slot_sec * chars_per_sec))


def retime_events(events: list[dict[str, Any]], durs: list[float],
                  guard: float = 0.05) -> list[dict[str, Any]]:
    """자막 이벤트 끝시각을 '실제 더빙 길이'에 맞춤(자연 페이싱과 자막 일치).

    다음 세그 시작 - guard 를 넘지 않게 클램프. 마지막 세그는 자유 연장.
    [규칙 신설 8/20] end_fixed(검수 반려 수정의 사용자 지정 end)는 **덮어쓰지 않는다**
    — 사람이 보고 정한 표시 시간이 실측 더빙 길이·클램프보다 우선한다."""
    out = []
    for i, e in enumerate(events):
        if e.get("end_fixed"):
            out.append(dict(e))
            continue
        end = e["start"] + (durs[i] if i < len(durs) and durs[i] > 0 else (e["end"] - e["start"]))
        if i + 1 < len(events):
            end = min(end, events[i + 1]["start"] - guard)
        out.append({**e, "end": round(end, 3)})
    return out


def apply_dub_overrides(events: list[dict[str, Any]],
                        ov: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """검수 반려 '수정 재렌더'(8/14): overrides subs{idx: …} 를 더빙 대사(events)에 병합.

    idx 는 ko_ja_pairs.json subs 의 idx(= ASR 세그 순번, 빈 대사 필터 **전**) — 검수함
    카드가 보여준 좌표 그대로 돌아온다. 값이 dict 면 {"ja", "style", "start_sec",
    "end_sec", "use"}(계약: docs/subtitle-style-overrides.md — 타입·범위 위반, 모르는
    style 키는 ValueError). start/end 는 영상 시간축 초 — SRT 1차 기록 전에 병합돼야
    페이싱 캡(segment_hard_caps)이 사용자 타이밍 기준으로 잡힌다. end 지정 세그는
    end_fixed 표시 → retime_events 가 덮지 않는다. use=false(소프트 삭제, E6-0)는
    이벤트에 표시만 하고 제외는 호출부(빈 대사 필터와 같은 지점)가 한다 — SRT 가
    합성 드라이버라 그 한 곳에서 합성·자막·retime 이 함께 빠진다. 없는 idx·빈 ja 는
    무시(운영자가 안 고친 줄). 사본 반환. 순수 — 테스트 대상."""
    from app.localize.overlay.render import validate_line_style, validate_line_timing
    subs = (ov or {}).get("subs") or {}
    out = [dict(e) for e in events]
    n = 0
    for key, v in subs.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(out):
            continue
        changed = False
        text = v.get("ja") if isinstance(v, dict) else v
        if isinstance(text, str) and text.strip():
            out[i]["text"] = text.strip()
            changed = True
        if isinstance(v, dict):
            if v.get("style") is not None:
                out[i]["style"] = validate_line_style(v["style"])       # 위반 = ValueError
                changed = True
            start, end = validate_line_timing(v)
            if start is not None:
                out[i]["start"] = start
                changed = True
            if end is not None:
                out[i]["end"] = end
                out[i]["end_fixed"] = True          # 사용자 값 우선 — retime 이 안 덮는다
                changed = True
            if "use" in v:                          # 소프트 삭제(E6-0)
                if not isinstance(v["use"], bool):
                    raise ValueError(f"subs[{key}].use 는 불리언(false=그 줄 제외): {v['use']!r}")
                out[i]["use"] = v["use"]
                changed = True
        n += 1 if changed else 0
    return out, n


def build_dub_pairs(segs: list[dict[str, Any]],
                    events: list[dict[str, Any]]) -> dict[str, Any]:
    """C 루트 ko_ja_pairs.json 확장 스키마(8/20) — 검수 카드·편집실 초기값용.

    subs[]: {idx(ASR 세그 순번=오버라이드 좌표), start, end(초, 영상 시간축),
             end_actual(False=계획값 — retime 전), ko, ja,
             style(오버라이드 있을 때만), end_fixed(사용자 지정 end 일 때만)}.
    오버라이드 병합 **후** 에 만들므로 start/end/style 은 현재 적용값이다.
    use=false(소프트 삭제)로 뺀 줄은 다음 카드에 실리지 않는다 — 편집실 재진입 때
    살아 돌아와 보이면 안 된다(E6-0). idx 는 필터 전 순번이라 좌표는 유지된다. 순수."""
    rows = []
    for s, e in zip(segs, events):
        if e.get("use") is False:
            continue
        row: dict[str, Any] = {"idx": e["idx"], "start": e["start"], "end": e["end"],
                               "end_actual": False, "ko": s["text"], "ja": e["text"]}
        if e.get("style"):
            row["style"] = e["style"]
        if e.get("end_fixed"):
            row["end_fixed"] = True
        rows.append(row)
    return {"subs": rows}


def update_pairs_actual_ends(pairs: dict[str, Any],
                             events: list[dict[str, Any]]) -> dict[str, Any]:
    """retime 후 실표시 end 를 ko_ja_pairs 에 반영 — end_actual=True 가 실측 표시값.

    retime 안 거친 행(빈 대사로 필터된 세그)은 계획값(end_actual=False)으로 남는다.
    사본 반환. 순수 — 테스트 대상."""
    by_idx = {e.get("idx"): e for e in events if e.get("idx") is not None}
    out = {**pairs, "subs": [dict(r) for r in pairs.get("subs", [])]}
    for r in out["subs"]:
        e = by_idx.get(r.get("idx"))
        if e is not None:
            r["end"], r["end_actual"] = e["end"], True
    return out


def _needs_truncate(dur: float, max_len: Optional[float]) -> bool:
    """배속 후에도 슬롯(다음 세그 시작)을 넘으면 잘라야 한다(드론/겹침 방지)."""
    return max_len is not None and dur > max_len + 0.05


def segment_hard_caps(spans: list[tuple[float, float]], guard: float = 0.05,
                      tail: float = 0.5) -> list[float]:
    """각 세그가 '다음 세그 시작'을 침범하지 않도록 최대 길이 캡 산출.

    한 세그의 합성이 환각으로 길어져도 다음 발화 위로 겹쳐 깔리는('에~~~' 드론)
    현상을 구조적으로 차단한다. 마지막 세그는 슬롯+tail 까지 허용.
    """
    caps: list[float] = []
    n = len(spans)
    for i, (s, e) in enumerate(spans):
        if i + 1 < n:
            caps.append(max(0.2, spans[i + 1][0] - s - guard))
        else:
            caps.append((e - s) + tail)
    return caps


def synthesize_with_retry(synth_fn, max_dur: float, tries: int = 5):
    """TTS 환각(비정상적으로 긴 출력) 방지: max_dur 이하가 나올 때까지 재합성.

    synth_fn() → (sr, audio[len 측정 가능]). 전부 길면 가장 짧은 결과를 반환(이후 캡됨).
    """
    best = None
    best_d = float("inf")
    for _ in range(max(1, tries)):
        sr, audio = synth_fn()
        d = (len(audio) / sr) if sr else 0.0
        if d < best_d:
            best, best_d = (sr, audio), d
        if d <= max_dur:
            return sr, audio
    return best


def f0_median(audio, sr: int, fmin: float = 80.0, fmax: float = 550.0) -> float:
    """프레임 자기상관 기반 중위 F0(Hz). 무성/저에너지 프레임 제외. 0=측정 불가."""
    import numpy as np
    x = np.asarray(audio, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    peak = np.abs(x).max()
    if not sr or peak <= 0:
        return 0.0
    x = x / peak
    w, hop = int(0.04 * sr), int(0.01 * sr)
    vals = []
    for i in range(0, len(x) - w, hop):
        f = x[i:i + w]
        if np.sqrt((f ** 2).mean()) < 0.05:
            continue
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[w - 1:]
        lo, hi = int(sr / fmax), int(sr / fmin)
        if hi >= len(ac):
            continue
        pk = int(np.argmax(ac[lo:hi])) + lo
        if ac[pk] > 0.3 * ac[0]:
            vals.append(sr / pk)
    import statistics
    return statistics.median(vals) if vals else 0.0


def pitch_distance_octaves(f0_a: float, f0_b: float) -> float:
    """두 F0 간 거리(옥타브). 측정 불가(0)면 inf — 비교 불가는 최악으로 취급."""
    import math
    if f0_a <= 0 or f0_b <= 0:
        return float("inf")
    return abs(math.log2(f0_a / f0_b))


def synth_level(audio) -> float:
    """합성 오디오의 정규화 피크(0~1). int/float dtype 모두 지원.

    GPT-SoVITS 가 간혹 '사실상 무음' 실패 합성을 내는데(실측: 커몬2 업로드본 —
    무음 후보가 피치 매칭을 통과한 뒤 정규화로 증폭돼 잡음이 됨), 피크 정규화 기반
    F0 측정은 이를 못 거른다 → 절대 레벨로 별도 게이트."""
    import numpy as np
    x = np.asarray(audio)
    if x.size == 0:
        return 0.0
    if np.issubdtype(x.dtype, np.integer):
        return float(np.abs(x).max()) / float(np.iinfo(x.dtype).max)
    return float(np.abs(x).max())


def _norm_scale(peak: float, target: float = 0.9) -> float:
    """보이스 트랙 피크 정규화 배율(헤드룸 확보 → limiter 펌핑 최소화). 무음 보호."""
    return (target / peak) if peak > 0 else 1.0


def _detect_lang(text: str, default: str = "ja") -> str:
    """세그 텍스트 언어 추정(영어 대사는 영어로 합성 유지). 라틴문자 위주면 en."""
    letters = re.sub(r"[^A-Za-z぀-ヿ一-鿿가-힣]", "", text)
    if not letters:
        return default
    ascii_alpha = sum(1 for c in letters if c.isascii() and c.isalpha())
    return "en" if ascii_alpha / len(letters) > 0.7 else default


# 먹방/ASMR 의성어·감탄(원본 유지 대상). 더빙은 '실제 대사'만(dialogue_only).
_ONOMATOPOEIA = {"음", "으음", "흠", "아", "어", "오", "와", "우와", "워", "앙", "냠", "냠냠",
                 "으", "읏", "하", "호", "헉", "캬", "얍", "요", "에", "음냠", "쩝", "후",
                 "휴", "으하", "으아", "쓰", "짱", "컥", "냥", "자", "야"}


def _is_dialogue(text: str) -> bool:
    """실제 대사 여부(true) vs 씹는소리/감탄(false). ASMR 리액션은 원본 유지하려 걸러냄."""
    s = re.sub(r"[\s!?.,~…♪♥★\-]+", "", text)
    if not s:
        return False
    if re.fullmatch(r"[A-Za-z' ]+", text) and len(text) <= 12:   # 짧은 영어 추임새
        return False
    if re.fullmatch(r"[\d,\s]+", text):                          # 숫자 카운트만
        return False
    kor = re.findall(r"[가-힣]", s)
    if len(kor) <= 2:                                            # 한글 2음절 이하 = 감탄
        return False
    if s in _ONOMATOPOEIA or (len(set(kor)) == 1):              # 의성어 / 같은 음절 반복(앙앙앙)
        return False
    return True


_SYL_RUN = re.compile(r"([가-힣])\1{2,}")        # 같은 음절 3+ 연속: 끙끙끙끙, 아아아
_UNIT_RUN = re.compile(r"([가-힣]{2})\1+")       # 두 음절 단위 반복: 아지아지, 음냐음냐


def strip_non_lexical(text: str) -> str:
    """'단어나 문장이 안 되는 자막은 필요 없어'(사용자 결정 8/14) — 옹알이 토큰 제거.

    ASR 이 캐릭터 옹알이를 한글로 받아쓴 것('끙끙끙끙야', '아지아지야')이 그대로
    더빙·자막이 되는 것을 막는다. 토큰 단위라 진짜 단어의 반복(노래 '배고파 배고파')은
    남는다. 걷어낸 뒤 남은 것이 대사가 못 되면(_is_dialogue 기준) 빈 문자열 —
    그 구간은 더빙·자막 대상이 아니며 원본 소리를 그대로 둔다. 순수 — 테스트 대상."""
    kept = []
    for tok in (text or "").split():
        core = re.sub(r"[\s!?.,~…♪♥★\-]+", "", tok)
        if not core:
            continue
        if _SYL_RUN.search(core) or _UNIT_RUN.search(core):
            rest = _UNIT_RUN.sub("", _SYL_RUN.sub("", core))
            if len(re.findall(r"[가-힣]", rest)) <= 2:
                continue                        # 반복 빼면 두 음절도 안 남는다 = 옹알이
        kept.append(tok)
    out = " ".join(kept)
    return out if _is_dialogue(out) else ""


def build_alignment_report(video_id: str, segments: list[dict[str, Any]],
                           voice_id: str) -> dict[str, Any]:
    return {
        "_warning": "더빙 초안. retention 리스크·hero 는 사람/성우 검토. 자동 게시 금지.",
        "video_id": video_id,
        "voice_id": voice_id,
        "segment_count": len(segments),
        "total_speech_sec": round(sum(s["end"] - s["start"] for s in segments), 2),
        "segments": segments,
    }


# ── TTS 백엔드 (lazy) ─────────────────────────────────────────────────────
def dub_backend(config: dict[str, Any]) -> str:
    """gptsovits(루피 음색 크로스링구얼 클로닝, 권장) | xtts | elevenlabs."""
    return config.get("dub", {}).get("tts_backend", "elevenlabs")


def segment_ext(config: dict[str, Any]) -> str:
    return ".wav" if dub_backend(config) in ("xtts", "gptsovits") else ".mp3"


_XTTS_MODEL = None  # 프로세스당 1회 로드(무거움)


def _xtts_model(config: dict[str, Any]):
    global _XTTS_MODEL
    if _XTTS_MODEL is None:
        from TTS.api import TTS  # coqui-tts

        name = config.get("dub", {}).get("xtts_model",
                                         "tts_models/multilingual/multi-dataset/xtts_v2")
        _XTTS_MODEL = TTS(name)
    return _XTTS_MODEL


def _synthesize_xtts(text: str, speaker_wav: str, config: dict[str, Any]) -> bytes:
    """XTTS-v2 크로스링구얼 보이스 클로닝: speaker_wav 목소리로 language 음성 합성."""
    import tempfile

    lang = config.get("dub", {}).get("language", "ja")
    out = tempfile.mktemp(suffix=".wav")
    _xtts_model(config).tts_to_file(text=text, speaker_wav=speaker_wav, language=lang, file_path=out)
    return Path(out).read_bytes()


_GSV = None  # GPT-SoVITS 핸들(프로세스당 1회 로드)


def reset_gptsovits_handle() -> None:
    """오염된 추론 상태 초기화 — 퇴화 레퍼런스가 모듈 내부 캐시를 오염시켜 이후
    '모든' 레퍼런스의 합성이 무음이 되는 실측 사례(2026-07-08 커몬2) 대응.
    다음 _gptsovits_handle 호출 시 모듈·가중치 재로드(~20s)."""
    global _GSV
    _GSV = None
    import sys as _sys
    for name in list(_sys.modules):
        if "inference_webui" in name:
            _sys.modules.pop(name, None)


def _gptsovits_handle(config: dict[str, Any]):
    """GPT-SoVITS 추론 모듈 로드 + 가중치 적용(1회). config.dub.gptsovits 로 경로 지정.

    repo_dir/model_dir 는 상대경로면 프로젝트 루트 기준. CPU 로 구동(Apple Silicon 안정).
    """
    global _GSV
    if _GSV is not None:
        return _GSV
    import os as _os
    import sys as _sys

    g = config.get("dub", {}).get("gptsovits", {})
    repo = resolve_path(g.get("repo_dir", "outputs/GPT-SoVITS"))
    _os.environ.setdefault("is_half", "False")
    _sys.path.insert(0, str(repo))
    _sys.path.insert(0, str(repo / "GPT_SoVITS"))
    cwd = _os.getcwd()
    _os.chdir(repo)                                  # 상대 pretrained_models 경로 해석용
    try:
        import GPT_SoVITS.inference_webui as iw
        iw.device = "cpu"; iw.is_half = False
        md = repo / g.get("model_dir", "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained")

        def _run_weight_fn(ret):
            """webui 의 change_*_weights 는 제너레이터(UI 스트림용)일 수 있음 — 호출만으론
            본문이 실행되지 않아 가중치 교체가 조용히 무시된다(2026-07-09 실측: v4 지정이
            v2 로 남음). 소진해야 로드되며, 말미 UI 업데이트 예외는 무해라 무시."""
            if hasattr(ret, "__iter__") and not isinstance(ret, (str, bytes, dict)):
                try:
                    for _ in ret:
                        pass
                except Exception:  # noqa: BLE001 — 가중치는 이미 적용, UI 잔여 코드 예외
                    pass

        _run_weight_fn(iw.change_gpt_weights(gpt_path=str(md / g.get(
            "gpt_ckpt", "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"))))
        _run_weight_fn(iw.change_sovits_weights(
            sovits_path=str(md / g.get("sovits_ckpt", "s2G2333k.pth"))))
        from tools.i18n.i18n import I18nAuto
        i18n = I18nAuto()
        _GSV = {"iw": iw, "lang": {"ja": i18n("日文"), "en": i18n("英文"), "ko": i18n("韩文")}}
    finally:
        _os.chdir(cwd)
    return _GSV


def _synthesize_gptsovits(text: str, lang: str, config: dict[str, Any]) -> bytes:
    """루피 음색 크로스링구얼 클로닝(한국어 ref → ja/en 합성). 멀티레퍼런스 + 환각방지 재시도."""
    import tempfile
    from types import SimpleNamespace

    import soundfile as sf

    import numpy as np

    from app.localize.overlay.refbank import profile_distance, spectral_centroid  # lazy(순환 import 회피)

    g = config.get("dub", {}).get("gptsovits", {})
    h = _gptsovits_handle(config)
    iw = h["iw"]
    aux = [SimpleNamespace(name=str(resolve_path(p))) for p in g.get("aux_refs", [])]

    clean = strip_stage_directions(text)
    if not clean:                                           # 순수 지문(（もぐもぐ）) → 스킵
        log.info("합성 스킵(지문/빈 텍스트): %r", text)
        return b""

    max_dur = float(g.get("max_synth_dur", 12.0))
    tries = int(g.get("retry_tries", 6))
    pitch_tries = int(g.get("pitch_match_tries", 3))
    min_level = float(g.get("min_synth_level", 0.05))
    max_chars = int(g.get("synth_max_chars", 24))

    # 매칭 목표 프로필 = target_profile(F0+밝기, 이 영상 원본) > target_f0 > ref.
    target_prof = g.get("target_profile")
    goal_f0 = float(g.get("target_f0", 0) or 0)
    if not target_prof and goal_f0 <= 0 and pitch_tries > 1:
        try:
            rx, rsr = sf.read(str(resolve_path(g["ref_wav"])))
            goal_f0 = f0_median(rx, rsr)
        except Exception:
            goal_f0 = 0.0
    if not target_prof and goal_f0 > 0:
        target_prof = {"f0": goal_f0, "centroid": 0.0}

    def _cand_dist(sr, audio):
        cf0 = f0_median(audio, sr)
        if target_prof.get("centroid", 0) > 0:
            return profile_distance({"f0": cf0, "centroid": spectral_centroid(audio, sr)},
                                    target_prof, brightness_weight=0.7)
        return pitch_distance_octaves(cf0, target_prof["f0"])

    def _synth_chunk(chunk: str):
        """짧은 청크 1개 합성 — 프로필 매칭 + 할루시네이션(과장 길이) 기각."""
        def _one():
            res = iw.get_tts_wav(
                ref_wav_path=str(resolve_path(g["ref_wav"])), prompt_text=g["prompt_text"],
                prompt_language=h["lang"][g.get("prompt_lang", "ko")],
                text=chunk, text_language=h["lang"].get(lang, h["lang"]["ja"]),
                top_k=int(g.get("top_k", 20)), top_p=float(g.get("top_p", 0.6)),
                temperature=float(g.get("temperature", 0.6)), inp_refs=(aux or None))
            sr, audio = list(res)[-1]
            return sr, audio
        cap = min(max_dur, max(expected_synth_dur(chunk) * 3.0, 4.0))   # 길이 대비 상한
        best, best_dist = None, float("inf")
        for _ in range(max(1, pitch_tries)):
            sr, audio = synthesize_with_retry(_one, max_dur=cap, tries=tries)
            if synth_level(audio) < min_level:
                continue
            over = (len(audio) / sr) > cap                  # 상한 초과 = 할루시네이션 의심
            dist = (_cand_dist(sr, audio) if target_prof else 0.0) + (10.0 if over else 0.0)
            if dist < best_dist:
                best, best_dist = (sr, audio), dist
            if best_dist <= 0.15:
                break
        if best is None:
            raise RuntimeError(f"청크 합성 전부 무음성: {chunk!r}")
        if best_dist >= 10.0:
            log.warning("청크 할루시네이션 의심(길이 초과) 채택 — 텍스트: %r", chunk)
        return best

    chunks = split_for_synth(clean, max_chars)
    sr_out, pieces = None, []
    for ch in chunks:
        sr, audio = _synth_chunk(ch)
        sr_out = sr
        if pieces:                                          # 청크 간 짧은 간격
            pieces.append(np.zeros(int(sr * 0.08), dtype=np.asarray(audio).dtype))
        pieces.append(np.asarray(audio))
    audio = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
    log.info("합성: %d청크 → %.1fs (텍스트 %d자, 프로필매칭%s)",
             len(chunks), len(audio) / sr_out, len(clean), " on" if target_prof else " off")
    out = tempfile.mktemp(suffix=".wav")
    sf.write(out, audio, sr_out)
    return Path(out).read_bytes()


def _synthesize_elevenlabs(text: str, voice_id: str, config: dict[str, Any]) -> bytes:
    key = get_secret("ELEVENLABS_API_KEY", required=True)
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError as e:
        raise ImportError("elevenlabs 필요: pip install elevenlabs") from e
    client = ElevenLabs(api_key=key)
    kwargs: dict[str, Any] = {}
    settings = config.get("dub", {}).get("eleven_voice_settings")
    if settings:  # IVC 클론 보이스용 튜닝(src/voice_clone.py 참고). 미설정 시 종전 동작.
        kwargs["voice_settings"] = settings
    audio = client.text_to_speech.convert(
        voice_id=voice_id, text=text,
        model_id=config.get("dub", {}).get("tts_model", "eleven_multilingual_v2"),
        output_format="mp3_44100_128", **kwargs)
    return b"".join(audio) if hasattr(audio, "__iter__") else audio


def synthesize_segment(text: str, config: dict[str, Any], voice_id: Optional[str] = None,
                       speaker_wav: Optional[str] = None, lang: Optional[str] = None) -> bytes:
    backend = dub_backend(config)
    if backend == "gptsovits":
        seg_lang = lang or _detect_lang(text, config.get("dub", {}).get("language", "ja"))
        return _synthesize_gptsovits(text, seg_lang, config)
    if backend == "xtts":
        if not speaker_wav:
            raise ValueError("xtts 백엔드: speaker_wav(클로닝용 음성 샘플) 필요")
        return _synthesize_xtts(text, speaker_wav, config)
    if not voice_id:
        raise ValueError("elevenlabs 백엔드: voice_id 필요")
    return _synthesize_elevenlabs(text, voice_id, config)


# ── 자가개선: ASR 백체크 — 합성음 재인식 → 발음 정확도(CER) 검증(2026-07-21) ──
_BC_ASR: dict[str, Any] = {}                          # 백체크 ASR 모델 캐시(크기별)


from app.localize.overlay.common import cer, levenshtein, norm_text  # noqa: E402 — 백체크 공용(engine/common)


def norm_for_cer(text: str, lang: str = "ja") -> str:
    """CER 비교용 정규화 — 표기 차이를 발음 기준으로 흡수.

    ja 는 pyopenjtalk 카나 독음(頑張れ→ガンバレ)으로 한자/가나 표기차를 제거.
    미설치·실패 시 NFKC+기호 제거 폴백(같은 표기끼리만 비교 가능)."""
    t = norm_text(text)
    if lang == "ja" and t:
        try:
            import pyopenjtalk                        # lazy — GPT-SoVITS 스택에 포함
            t = norm_text(pyopenjtalk.g2p(t, kana=True))
        except Exception:                             # noqa: BLE001 — 폴백도 유효한 비교
            pass
    return t


def _bc_asr_text(wav_path: str, config: dict[str, Any], lang: str) -> str:
    from faster_whisper import WhisperModel
    dcfg = config.get("dub", {})
    size = dcfg.get("backcheck", {}).get("asr_model") or dcfg.get("asr_model", "base")
    model = _BC_ASR.get(size)
    if model is None:
        model = _BC_ASR[size] = WhisperModel(size, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(wav_path), language=lang)
    return "".join(s.text for s in segs).strip()


def synthesize_checked(text: str, config: dict[str, Any],
                       voice_id: Optional[str] = None, speaker_wav: Optional[str] = None,
                       lang: Optional[str] = None, synth_fn=None,
                       asr_fn=None) -> tuple[bytes, Optional[float]]:
    """합성 + ASR 백체크: 재인식 CER 이 max_cer 초과면 재합성, 최저 CER 후보 채택.

    반환 (data, cer). cer=None 은 백체크 미수행(비활성·빈 합성·ASR 장애 — 더빙은 계속).
    synth_fn/asr_fn 은 테스트 주입용."""
    dcfg = config.get("dub", {})
    bc = dcfg.get("backcheck", {})
    synth = synth_fn or (lambda: synthesize_segment(
        text, config, voice_id=voice_id, speaker_wav=speaker_wav, lang=lang))
    data = synth()
    if not bc.get("enabled", False) or not data:
        return data, None
    seg_lang = lang or _detect_lang(text, dcfg.get("language", "ja"))
    target = norm_for_cer(text, seg_lang)
    if not target:
        return data, None

    def _measure(d: bytes) -> float:
        if asr_fn is not None:
            hyp = asr_fn(d)
        else:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                f.write(d)
                f.flush()
                hyp = _bc_asr_text(f.name, config, seg_lang)
        return cer(target, norm_for_cer(hyp, seg_lang))

    try:
        best_cer = _measure(data)
    except Exception as e:                            # noqa: BLE001 — ASR 장애가 더빙을 죽이지 않게
        log.warning("백체크 ASR 실패(건너뜀): %s", e)
        return data, None
    max_cer = float(bc.get("max_cer", 0.3))
    for _ in range(max(0, int(bc.get("retries", 2)))):
        if best_cer <= max_cer:
            break
        cand = synth()
        if not cand:
            break
        try:
            c = _measure(cand)
        except Exception:                             # noqa: BLE001
            break
        log.info("백체크 재합성: CER %.2f → %.2f (%r)", best_cer, c, text[:20])
        if c < best_cer:
            data, best_cer = cand, c
    if best_cer > float(bc.get("fail_cer", 0.5)):
        log.warning("백체크 실패 세그(CER %.2f): %r — QA hold 근거로 기록", best_cer, text[:30])
    return data, best_cer


def backcheck_summary(segments: list[dict[str, Any]], fail_cer: float) -> dict[str, Any]:
    """세그별 backcheck_cer → dub_backcheck.json 요약(자동 승인 QA 게이트 입력)."""
    cers = [float(s["backcheck_cer"]) for s in segments
            if s.get("backcheck_cer") is not None]
    if not cers:
        return {"checked": 0, "cer_avg": 0.0, "cer_max": 0.0, "failed": 0}
    return {"checked": len(cers),
            "cer_avg": round(sum(cers) / len(cers), 4),
            "cer_max": round(max(cers), 4),
            "failed": sum(1 for c in cers if c > fail_cer)}


def dub(video_id: str, subtitle_path: str, level: str, config: dict[str, Any],
        voice_id: Optional[str] = None, speaker_wav: Optional[str] = None) -> dict[str, Any]:
    require_level_c(level)
    backend = dub_backend(config)
    if backend == "gptsovits":
        # 레퍼런스(루피) 음성으로 크로스링구얼 클로닝 → voice_id/speaker_wav 불필요.
        gsv = config.get("dub", {}).get("gptsovits", {})
        if not gsv.get("ref_wav"):
            raise ValueError("gptsovits 백엔드: config.dub.gptsovits.ref_wav(레퍼런스 음성) 필요")
        voice_ref = gsv["ref_wav"]
    elif backend == "xtts":
        speaker_wav = speaker_wav or config.get("dub", {}).get("speaker_wav")
        if not speaker_wav:
            raise ValueError("xtts 백엔드: --speaker(클로닝용 루피 음성 샘플) 또는 config.dub.speaker_wav 필요")
        voice_ref = speaker_wav
    else:
        voice_id = voice_id or config.get("dub", {}).get("voice_id", "")
        if not voice_id:
            raise ValueError("elevenlabs 백엔드: voice_id 필요. --voice 또는 config.dub.voice_id")
        voice_ref = voice_id

    segments = parse_segments(subtitle_path)
    base = ensure_dir(resolve_path(f"{config['paths']['outputs_dir']}/{video_id}"))
    # (8/14 정정) 한글 대역 ko_ja_pairs 저장은 dub_from_video 로 옮겼다 — 여기엔 KO 원문이
    # 없다(subtitle_path 는 이미 일본어 SRT). 종전 블록은 이 스코프에 없는 변수(segs/events)
    # 를 참조해 모든 C 더빙이 NameError 로 죽는 상태였다(8/14 재검 실측).
    seg_dir = ensure_dir(base / "dub_segments")
    ext = segment_ext(config)
    log.warning("Level C 더빙 초안 생성(backend=%s). hero/리텐션 리스크는 사람 검토 필수.", backend)

    fit = config.get("dub", {}).get("fit_to_timing", True)
    max_sp = float(config.get("dub", {}).get("max_speedup", 1.35))
    caps = segment_hard_caps([(s.get("start", 0.0), s.get("end", 0.0)) for s in segments])
    seg_files: list[tuple[float, Path]] = []
    actual_durs: list[float] = []                     # 세그별 실제 길이(자막 retime 용)
    for i, seg in enumerate(segments):
        data, seg["backcheck_cer"] = synthesize_checked(
            seg["text"], config, voice_id=voice_id, speaker_wav=speaker_wav)
        if not data:                                  # 지문/빈 텍스트 → 더빙 스킵(드론 방지)
            log.info("세그 %d 스킵(합성 없음): %r", i, seg["text"])
            actual_durs.append(0.0)
            continue
        fp = seg_dir / f"seg_{i:04d}{ext}"
        if fit:
            # 자연 속도 우선: 슬롯이 아니라 cap(다음 대사 침범선) 기준 — '말 빠름' 해결.
            raw = seg_dir / f"seg_{i:04d}_raw{ext}"
            raw.write_bytes(data)
            natural = common.probe(raw).get("duration", 0.0)
            speed, _ = pacing_plan(natural, caps[i], max_speedup=max_sp)
            target = (natural / speed) if speed > 1.0 else natural
            _fit_audio(raw, fp, target, max_speedup=max_sp, max_len=caps[i])
            if speed > 1.0:
                log.info("세그 %d 페이싱: 자연 %.1fs > cap %.1fs → %.2fx", i, natural, caps[i], speed)
        else:
            fp.write_bytes(data)
        actual_durs.append(common.probe(fp).get("duration", 0.0))
        seg_files.append((seg["start"], fp))

    draft = base / "dub_ja_draft.wav"
    _assemble_timeline(seg_files, draft)
    _normalize_track(draft, float(config.get("dub", {}).get("voice_norm_peak", 0.9)))
    # 밝기 보정: 합성이 고역을 뭉개 원본보다 어둡고 자음이 흐릿(단어 뭉개짐) → 원본 음색 밝기에
    # 맞춰 하이쉘프 부스트. 음색 일치 + 단어 또렷함 동시 개선(2026-07-08 A/B 실측: 원본 4200 vs 더빙 2900).
    tgt_cen = float(config.get("dub", {}).get("gptsovits", {})
                    .get("target_profile", {}).get("centroid", 0) or 0)
    brighten_track(draft, tgt_cen, config)
    _normalize_track(draft, float(config.get("dub", {}).get("voice_norm_peak", 0.9)))
    write_json(build_alignment_report(video_id, segments, voice_ref),
               base / "alignment_report.json")
    bc = config.get("dub", {}).get("backcheck", {})
    if bc.get("enabled", False):                      # 자가개선: QA 게이트 입력(autopilot)
        summary = backcheck_summary(segments, float(bc.get("fail_cer", 0.5)))
        write_json(summary, base / "dub_backcheck.json")
        log.info("백체크 요약: 검사 %d세그, CER avg %.3f / max %.3f, 실패 %d",
                 summary["checked"], summary["cer_avg"], summary["cer_max"], summary["failed"])
    log.info("더빙 초안(검토 전): %s (세그먼트 %d, backend=%s)", draft, len(segments), backend)
    return {"draft": str(draft), "segments": len(segments), "backend": backend,
            "actual_durs": actual_durs}


# ── 영상→더빙 (ASR → 번역 → 합성 → 믹스) ─────────────────────────────────
# ⚠ 아래 self-ref 프로브가 **자기 자신을 서브프로세스로** 다시 부른다(모델 캐시 오염
# 격리 — 그 주석 참조). vlp 는 `src.dub` 였다. 이식하며 경로가 바뀌었고, 박아 두면
# 파일이 옮겨질 때 조용히 죽으므로 여기 한 곳에서 만든다.
_SELF_MODULE = __name__ if __name__ != "__main__" else "app.localize.overlay.dub"

_HANGUL_RE = re.compile(r"[가-힣]")
_STAGE_RE = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")   # 괄호 지문(（もぐもぐ）등)


def has_hangul(text: str) -> bool:
    return bool(_HANGUL_RE.search(text or ""))


def strip_stage_directions(text: str) -> str:
    """괄호 지문 제거 — 「（もぐもぐ！）」류는 발화가 아니라 지문이라 더빙하면 드론이 된다."""
    out = _STAGE_RE.sub("", text or "")
    return re.sub(r"\s+", " ", out).strip(" 、。・").strip()


def split_for_synth(text: str, max_chars: int = 24) -> list[str]:
    """긴 문장을 구두점(、。！？) 단위로 ≤max_chars 청크 분할 — GPT-SoVITS 는 짧은 발화가 안정적
    (긴 특이 가타카나 나열은 60초 할루시네이션 유발, 2026-07-08 실측)."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[、。！？!?])", text)
    chunks: list[str] = []
    cur = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if cur and len(cur) + len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + p) if not cur else (cur + p)
    if cur:
        chunks.append(cur)
    # 여전히 max_chars 크게 초과하는 단일 청크(구두점 없는 긴 나열)는 강제 분할
    out: list[str] = []
    for c in chunks:
        while len(c) > max_chars * 1.6:
            out.append(c[:max_chars])
            c = c[max_chars:]
        out.append(c)
    return out


def expected_synth_dur(text: str, per_char: float = 0.16) -> float:
    """텍스트 길이로 추정한 정상 합성 길이(초) — 할루시네이션(과장 길이) 판정 기준."""
    n = len(re.sub(r"\s", "", text or ""))
    return max(1.0, n * per_char)


def fix_leaked_korean(text: str, config: dict[str, Any]) -> str:
    """일본어 번역에 남은 한글(주로 고유명사)을 가타카나로 변환 — 단어 명료도.

    한글 없으면 그대로. LLM 실패·여전히 한글 남으면 원문 유지(파이프라인 안 죽임)."""
    if not has_hangul(text):
        return text
    sys_p = ("다음 일본어 문장에 한국어 글자가 남아 있다. 한국어(한글) 부분을 하나도 남기지 말고 "
             "전부 문맥에 맞는 자연스러운 가타카나 발음 표기로 바꿔라. 나머지는 그대로. "
             "설명 없이 교정된 일본어 문장 한 줄만 출력.")
    prev = text
    for _ in range(2):                                  # LLM 비결정 → 재시도(한 번에 실패해도)
        try:
            from app.localize.overlay.llm import complete
            out = complete(sys_p, prev, config, max_tokens=256).strip().splitlines()[0].strip()
        except Exception as e:  # noqa: BLE001
            log.warning("한글 잔존 교정 오류(%s) — 원문 유지", e)
            return text
        if out and not has_hangul(out):
            log.info("한글 잔존 교정: %r → %r", text, out)
            return out
        prev = out or prev
    log.warning("한글 잔존 교정 2회 실패(여전히 한글) — 원문 유지: %r", text)
    return text


def reliable_segment(no_speech_prob: float, avg_logprob: float,
                     max_no_speech: float = 0.5, min_logprob: float = -1.2) -> bool:
    """Whisper 할루시네이션 필터 — 음악/효과음에서 '유료 광고 포함' 류 문구를 지어내는
    세그먼트를 거른다(실측: 아기루피 Short, no_speech 0.75 로 광고 고지문 생성).
    no_speech_prob 높음 = 모델 스스로 '말 아님' / avg_logprob 매우 낮음 = 확신 없음."""
    return no_speech_prob <= max_no_speech and avg_logprob >= min_logprob


def detect_audio_language(media: str, config: dict[str, Any]) -> tuple[str, float]:
    """faster-whisper 자동 언어감지 → (언어코드, 확률). 강제 ko 인식 전 가드용.

    language=None 이면 첫 30초로 언어만 판정 — 세그먼트 제너레이터를 소비하지
    않으므로 받아쓰기 비용은 들지 않는다. transcribe 와 같은 모델/설정 사용."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ImportError("faster-whisper 필요: pip install faster-whisper") from e
    dconf = config.get("dub", {})
    model = WhisperModel(dconf.get("asr_model", "base"), device="cpu", compute_type="int8")
    _, info = model.transcribe(str(media), language=None,
                               vad_filter=bool(dconf.get("asr_vad_filter", True)))
    return info.language, float(info.language_probability)


def transcribe(media: str, config: dict[str, Any], language: str = "ko") -> list[dict[str, Any]]:
    """faster-whisper 로 음성 받아쓰기 → [{start,end,text}] (대사 없는 영상이면 빈 리스트).

    할루시네이션 세그먼트(reliable_segment 참고)는 제외 — 없는 대사를 더빙하지 않는다."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ImportError("faster-whisper 필요: pip install faster-whisper") from e
    dconf = config.get("dub", {})
    size = dconf.get("asr_model", "base")
    # 배경음악이 큰 영상은 VAD 가 대사를 통째로 거를 수 있어 config 로 끌 수 있게 함.
    vad = bool(dconf.get("asr_vad_filter", True))
    max_ns = float(dconf.get("asr_max_no_speech", 0.5))
    min_lp = float(dconf.get("asr_min_logprob", -1.2))
    model = WhisperModel(size, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(media), language=language, vad_filter=vad)
    out = []
    for s in segs:
        if not s.text.strip():
            continue
        if not reliable_segment(float(s.no_speech_prob), float(s.avg_logprob), max_ns, min_lp):
            log.info("ASR 할루시네이션 의심 제외: %r (no_speech=%.2f, logprob=%.2f)",
                     s.text.strip(), s.no_speech_prob, s.avg_logprob)
            continue
        out.append({"start": float(s.start), "end": float(s.end), "text": s.text.strip()})
    return out


def separate_vocals(media: str, out_dir, config: dict[str, Any]) -> Path:
    """Demucs 2-stem 분리 → no_vocals(반주·효과음) 스템 경로. 원본 목소리 제거용."""
    import subprocess

    out_dir = ensure_dir(out_dir)
    model = config.get("dub", {}).get("demucs_model", "htdemucs")
    nov = out_dir / model / Path(media).stem / "no_vocals.wav"
    if nov.exists():                                   # 이미 분리됨 → 재실행 생략(느린 CPU 절약)
        return nov
    subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", model,
                    "-o", str(out_dir), str(media)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not nov.exists():
        raise RuntimeError(f"Demucs no_vocals 스템 없음: {nov}")
    return nov


def loop_plan(duration: float, min_s: float = 3.2, max_s: float = 10.0,
              gap: float = 0.15) -> int:
    """GPT-SoVITS 레퍼런스 3~10초 요건 — 짧은 발화를 몇 번 반복할지(0=사용 불가).

    실측 검증(2026-07-02 loopy_short): "루피" 2.35초 발화를 0.15초 간격 2회 연결해
    4.85초 레퍼런스로 사용, 원본 음색 클로닝 성공."""
    import math
    if duration <= 0.3:                      # 유의미한 발화 아님
        return 0
    if duration >= min_s:
        return 1
    n = math.ceil((min_s + gap) / (duration + gap))
    total = n * duration + (n - 1) * gap
    return n if total <= max_s else 0


def pick_ref_segments(segs: list[dict[str, Any]], max_total: float = 8.0) -> list[dict[str, Any]]:
    """레퍼런스로 쓸 대사 세그먼트 — 앞에서부터 그리디로 합계 max_total 초까지."""
    out, total = [], 0.0
    for s in segs:
        d = max(0.0, float(s.get("end", 0)) - float(s.get("start", 0)))
        if d <= 0:
            continue
        if out and total + d > max_total:
            break
        out.append(s)
        total += d
    return out


def build_self_ref(video: str, segs: list[dict[str, Any]], config: dict[str, Any],
                   out_dir) -> Optional[dict[str, str]]:
    """영상 '자체 목소리'로 GPT-SoVITS 레퍼런스 구축 → {ref_wav, prompt_text} 또는 None.

    음색 은행(config ref)보다 해당 영상 목소리가 항상 더 정확하다(2026-07-02 실측 —
    먹방 레퍼런스로 더빙하자 "루피 목소리가 아니다" 피드백, self-ref 로 원본 피치 일치).
    플로우: demucs 보컬 분리 → 대사 구간 컷·정제 → 3초 미만이면 반복 연결.
    실패(대사 없음·너무 짧음·분리 실패) 시 None — 호출자가 은행 레퍼런스로 폴백."""
    import subprocess
    from app.localize.overlay import common

    picked = pick_ref_segments(segs)
    if not picked:
        return None
    out_dir = ensure_dir(out_dir)
    audio = out_dir / "self_src.wav"
    if common.extract_audio(video, audio) is None:
        return None
    try:
        nov = separate_vocals(str(audio), out_dir / "stems", config)
        voc = Path(nov).parent / "vocals.wav"
    except Exception as e:                    # demucs 미설치·실패 → 폴백
        log.warning("self-ref 보컬 분리 실패(%s) → 은행 레퍼런스 사용", e)
        return None
    # 대사 구간만 이어붙이고 정제(저역 컷·노이즈 감쇠·레벨 정규화), mono 32k
    pad = 0.05
    parts, filters = [], []
    for i, s in enumerate(picked):
        st, en = max(0.0, float(s["start"]) - pad), float(s["end"]) + pad
        filters.append(f"[0:a]atrim={st:.3f}:{en:.3f},asetpts=N/SR/TB[a{i}]")
        parts.append(f"[a{i}]")
    fc = (";".join(filters) + ";" + "".join(parts)
          + f"concat=n={len(picked)}:v=0:a=1,"
          + "highpass=f=60,afftdn=nf=-25,dynaudnorm=p=0.7:m=10[out]")
    seg_wav = out_dir / "self_seg.wav"
    subprocess.run([common.ffmpeg_bin(), "-y", "-v", "error", "-i", str(voc), "-filter_complex", fc,
                    "-map", "[out]", "-ac", "1", "-ar", "32000", str(seg_wav)], check=True)
    dur = float(common.probe(seg_wav).get("duration", 0.0) or 0.0)
    n = loop_plan(dur)
    if n == 0:
        log.info("self-ref 발화 부족(%.2fs) → 은행 레퍼런스 사용", dur)
        return None
    ref = seg_wav
    if n > 1:                                 # 0.15s 무음 간격으로 n 회 반복 연결
        ref = out_dir / "self_ref.wav"
        inputs = ["-i", str(seg_wav), "-f", "lavfi", "-t", "0.15",
                  "-i", "anullsrc=r=32000:cl=mono"]
        seq = "".join(["[0:a]" if i % 2 == 0 else "[1:a]" for i in range(2 * n - 1)])
        subprocess.run([common.ffmpeg_bin(), "-y", "-v", "error", *inputs, "-filter_complex",
                        f"{seq}concat=n={2 * n - 1}:v=0:a=1", "-ar", "32000", "-ac", "1",
                        str(ref)], check=True)
    text = " ".join(s["text"] for s in picked)
    prompt = " ".join([text] * n)
    log.info("self-ref 레퍼런스: %s (%.2fs x%d, 전사=%r)", ref, dur, n, text[:60])
    return {"ref_wav": str(ref), "prompt_text": prompt}


def _mute_windows(in_path: Path, out_path: Path, windows: list[tuple[float, float]]) -> None:
    """오디오에서 지정 시간창만 음소거(나머지 원본 유지). dialogue_only 시 대사 구간만 제거."""
    import subprocess

    if not windows:
        out_path.write_bytes(Path(in_path).read_bytes())
        return
    expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in windows)   # OR(합>0)
    subprocess.run([common.ffmpeg_bin(), "-y", "-i", str(in_path), "-af", f"volume=0:enable='{expr}'",
                    str(out_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _mix_two(a: Path, b: Path, out: Path) -> None:
    """두 오디오 합성(정규화 없이 합산). 반주(no_vocals) + 리액션(대사 제거 보컬)."""
    import subprocess

    subprocess.run([common.ffmpeg_bin(), "-y", "-i", str(a), "-i", str(b), "-filter_complex",
                    "amix=inputs=2:duration=longest:normalize=0", str(out)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dub_from_video(video_id: str, video: str, level: str, config: dict[str, Any],
                   speaker_wav: Optional[str] = None, source_lang: str = "ko",
                   mux: bool = True) -> dict[str, Any]:
    """대사 있는 영상 풀 더빙: 받아쓰기(ASR) → 트랜스크리에이션 → 클론 합성 → 영상에 믹스.

    [필수 게이트] Level C 한정. 결과는 초안 — retention·hero 는 사람/성우 검토.
    """
    require_level_c(level)
    from app.localize.overlay import common as _common
    from app.localize.overlay import render as render_mod
    from app.localize.overlay.translate import transcreate

    segs = transcribe(video, config, language=source_lang)
    if not segs:
        raise ValueError("받아쓰기된 대사 없음 — 대사 없는 영상(ASMR 등)일 수 있음. 대사 있는 영상 필요.")
    if config.get("dub", {}).get("dialogue_only", False):
        kept = [s for s in segs if _is_dialogue(s["text"])]
        log.info("dialogue_only: ASR %d개 중 실제 대사 %d개만 더빙(리액션/씹는소리는 원본 유지)",
                 len(segs), len(kept))
        segs = kept
        if not segs:
            raise ValueError("dialogue_only 필터 결과 대사 0개. asr_model 또는 필터 확인.")
    # 옹알이 게이트(8/14 사용자 결정): dialogue_only 와 별개로 **항상** 돈다 —
    # '단어나 문장이 안 되는' ASR 구간은 더빙·자막에서 뺀다(원본 소리는 그대로).
    # 실측: '끙끙끙끙야 아지아지야 오 너무 예뻐' → '오 너무 예뻐'. 좌표(idx)는 이
    # 게이트 **뒤**의 목록 기준이라 검수 카드·수정 재렌더와 어긋나지 않는다.
    if config.get("dub", {}).get("drop_gibberish", True):
        cleaned = []
        for s in segs:
            t = strip_non_lexical(s["text"])
            if not t:
                continue
            cleaned.append({**s, "text": t} if t != s["text"] else s)
        if len(cleaned) != len(segs):
            log.info("옹알이 게이트: %d→%d 구간(단어 안 되는 자막 제외)", len(segs), len(cleaned))
        segs = cleaned
        if not segs:
            raise ValueError("옹알이 게이트 결과 대사 0개 — 대사 없는 영상(리액션만)일 수 "
                             "있습니다. no_dialogue_fallback(BJ) 대상.")
    log.info("ASR 대사 %d개 받아쓰기 완료 → 트랜스크리에이션", len(segs))

    # 더빙용 길이 예산: 번역이 슬롯 대비 길면 어떤 페이싱으로도 말이 빨라진다(근본 원인)
    # → 원문 슬롯 초수 × 합성 발화속도(자/초)로 문자 예산을 걸어 간결한 번역 유도.
    cps = float(config.get("dub", {}).get("dub_chars_per_sec", 5.5))
    budgets = [char_budget(s["end"] - s["start"], cps) for s in segs]
    entries = transcreate([s["text"] for s in segs], config,   # 한국어→일본어(LLM, persona)
                          char_budgets=budgets)
    # 한글 잔존 교정 + 괄호 지문 제거: LLM 이 고유명사(마라엽'떡')를 한글로 남기거나
    # 지문(（もぐもぐ）)을 넣으면 더빙이 억지 발음해 뭉개짐 → 가타카나 변환 + 지문 제거.
    jmap = {e.source: strip_stage_directions(fix_leaked_korean(e.target, config)) for e in entries}
    events = [{"idx": i, "start": s["start"], "end": s["end"], "text": jmap.get(s["text"], "")}
              for i, s in enumerate(segs)]              # idx = 오버라이드·pairs 좌표(필터 전)

    base = ensure_dir(resolve_path(f"{config['paths']['outputs_dir']}/{video_id}"))
    # 반려-수정 재렌더(8/14): 검수함에서 고친 대사·스타일·타이밍(overrides.json)을 합성
    # **전**에 병합 — 타이밍이 SRT 1차 기록보다 앞서야 페이싱 캡(segment_hard_caps)에
    # 먹힌다. 좌표(idx)는 아래 ko_ja_pairs 의 idx 와 같은 '빈 대사 필터 전 세그 순번' —
    # 그래서 필터보다 먼저 적용한다(지문 제거로 비었던 줄을 운영자가 채우는 것도 허용).
    ov_path = base / "overrides.json"
    cuts: list[dict[str, float]] = []
    if ov_path.exists():
        try:
            ov = read_json(ov_path)
            # cuts 검증을 병합보다 먼저 — 위반 시 이 재렌더의 오버라이드 **전체** 무시
            # (E5 정책: 검증 위반이 부분 반영으로 새지 않는다. 재검수에서 걸러진다.)
            if ov.get("cuts"):
                cuts = validate_cuts(ov["cuts"],
                                     duration=float(_common.probe(video).get("duration", 0.0)))
            events, n_ov = apply_dub_overrides(events, ov)
            log.info("반려 수정 병합: 더빙 대사 %d건 교체(overrides.json)", n_ov)
        except Exception as e:  # noqa: BLE001 — 병합 실패가 더빙을 죽이지 않게(원문대로 진행)
            cuts = []
            log.warning("overrides.json 병합 실패(무시하고 원문 진행): %s", e)
    # 구간 잘라내기(E9): use:false·문구·타이밍 병합 **뒤**, pairs·합성·SRT 기록 **전** —
    # 사용자 타이밍(end_fixed)도 당김 대상이다(절대값이 아니라 그 장면에 붙어 있다).
    # 완전히 컷 안인 줄은 use=false 와 동일 의미로 표시돼 아래 한 곳에서 함께 빠지고,
    # 걸친 줄은 경계로 클램프된다. segs(뮤트 창·self-ref 좌표)와 영상도 같은 축으로 —
    # 이 아래의 video 는 전부 컷본이다(합성 슬롯·스템 분리·믹스·번인이 새 시간축).
    if cuts:
        events, n_cut = apply_cuts_to_events(events, cuts)
        segs = [{**s, "start": shift_time(s["start"], cuts),
                 "end": shift_time(s["end"], cuts)} for s in segs]
        video = str(_common.cut_video(video, base / "video_cut.mp4", cuts))
        log.info("구간 잘라내기(E9): 컷 %d개(총 %.1fs 삭제) — 완전 포함 %d줄 제외, 이후 시각 당김",
                 len(cuts), cut_total(cuts), n_cut)
    # 한글 대역(관제 검수 카드용, 8/14): 더빙 대사 KO⇄JA 쌍. idx 는 검수함 '수정 재렌더'
    # 오버라이드의 좌표로도 쓰인다. B 루트는 translations.json 이 있지만 C 루트는 여기가 유일.
    # (8/20 확장) end·end_actual·style 동봉 — retime 후 실측 end 로 갱신된다(아래).
    # (E9) cuts 적용 **후** 에 만들므로 다음 카드는 당겨진 시각을 본다. 적용된 cuts 는
    # 별도 키로 동봉 — 검수자가 '왜 짧아졌는지' 안다.
    pairs = build_dub_pairs(segs, events)
    if cuts:
        pairs["cuts"] = cuts
    write_json(pairs, base / "ko_ja_pairs.json")
    # 지문만이던 세그(요음!→（もぐもぐ）)와 use=false(소프트 삭제, E6-0) 제거 —
    # 이 아래 SRT 가 합성 드라이버라, 여기 한 곳에서 TTS 합성·자막(srt/ass 번인)·
    # retime(durs 정렬)이 함께 빠진다. 시작 시각은 아무도 안 옮기므로 뺀 줄의 창은
    # 무음으로 남고 뒤 이벤트가 당겨오지 않는다(발주 규칙).
    n_del = sum(1 for e in events if e.get("use") is False)
    if n_del:
        log.info("소프트 삭제: 자막·대사 %d줄 제외(use=false — 합성·자막·pairs 공통)", n_del)
    events = [e for e in events if e.get("use") is not False and e["text"].strip()]
    # 사용자 타이밍 이동으로 순서가 바뀔 수 있다 — SRT·페이싱 캡·retime(durs) 정렬은
    # 전부 '시작 시각 오름차순' 전제라 여기서 한 번 확정한다.
    events.sort(key=lambda e: e["start"])

    # self-ref: 이 영상의 원본 목소리를 레퍼런스로(음색 은행보다 정확).
    # 실패 시 → 은행에서 이 영상 목소리에 '음향적으로 가장 가까운' 레퍼런스 자동 선택(refbank).
    gsv = config.get("dub", {}).get("gptsovits", {})
    if dub_backend(config) == "gptsovits" and gsv.get("self_ref", True):
        ref_dir = base / "ref"
        sref = build_self_ref(video, segs, config, ref_dir)
        # 이 영상 원본 목소리 프로필(F0+밝기) — 합성 후보 선택의 매칭 목표(음색·음높이 모두).
        from app.localize.overlay import refbank
        seg_wav = ref_dir / "self_seg.wav"
        self_prof = refbank.wav_profile(str(seg_wav)) if seg_wav.exists() else None
        adopted = False
        if sref:
            import copy
            cand = copy.deepcopy(config)
            g = cand["dub"]["gptsovits"]
            g["ref_wav"], g["prompt_text"] = sref["ref_wav"], sref["prompt_text"]
            g["prompt_lang"], g["aux_refs"] = "ko", []
            if self_prof and self_prof.get("f0", 0) > 0:
                g["target_profile"] = self_prof
            # 사전 프로브(⚠ 반드시 서브프로세스): 퇴화 self-ref 는 프로세스 내
            # 모델 캐시를 오염시켜 이후 '은행 ref 포함 모든' 합성을 무음으로 만든다
            # (2026-07-08 실측 — 모듈 리셋도 무효). 격리 프로브 통과 시에만 채택.
            import subprocess as _sp
            res = _sp.run([sys.executable, "-m", _SELF_MODULE,
                           f"--probe-ref={sref['ref_wav']}",
                           f"--prompt-text={sref['prompt_text']}"],
                          capture_output=True, text=True, timeout=600,
                          cwd=str(resolve_path(".")))
            if res.returncode == 0:
                config = cand
                adopted = True
                log.info("self-ref 프로브 통과(격리) → 채택")
            else:
                log.warning("self-ref 프로브 실패 → 은행 최적매칭 시도: %s",
                            (res.stdout + res.stderr)[-120:])
        if not adopted:
            # 은행 최적매칭: 이 영상 목소리(self_prof)에 가장 가까운 은행 레퍼런스(음색).
            pick = refbank.best_ref(self_prof, config, exclude_source=video_id) if self_prof else None
            if pick:
                import copy
                config = copy.deepcopy(config)
                g = config["dub"]["gptsovits"]
                g["ref_wav"], g["prompt_text"] = pick["ref_wav"], pick["prompt_text"]
                g["prompt_lang"], g["aux_refs"] = "ko", pick["aux_refs"]
                # 후보 선택은 이 영상 원본 프로필(F0+밝기)을 목표로 — 음색은 은행, 음높이는 원본.
                g["target_profile"] = self_prof
            else:
                log.warning("은행 매칭 불가(은행 비었거나 프로필 측정 실패) → config 고정 ref(%s)",
                            gsv.get("ref_wav"))
        # harvest: 이 영상의 '긴 깨끗한 대사'를 은행에 축적(다음 영상 매칭 개선). 실패 무시.
        try:
            from app.localize.overlay import refbank
            n = refbank.harvest(video, config, video_id, segs=segs)
            if n:
                log.info("은행 축적: %s 에서 %d클립", video_id, n)
        except Exception as e:  # noqa: BLE001
            log.info("harvest 생략(%s)", e)

    ja_srt = base / "ja_dub.srt"
    ja_srt.write_text(render_mod.build_srt(events, int(config.get("render", {}).get("line_max_chars", 26))),
                      encoding="utf-8")
    res = dub(video_id, str(ja_srt), level, config, speaker_wav=speaker_wav)

    # 자연 페이싱으로 발화 길이가 슬롯과 달라짐 → 자막 끝시각을 실제 더빙 길이에 재정렬.
    # (사용자 지정 end 는 retime_events 가 보존한다 — 규칙 8/20)
    durs = res.get("actual_durs") or []
    if durs:
        events = retime_events(events, durs)
        ja_srt.write_text(render_mod.build_srt(events, int(config.get("render", {}).get("line_max_chars", 26))),
                          encoding="utf-8")
        # 검수 노출: retime 후 실표시 end 를 ko_ja_pairs 에 반영(end_actual=True 로 구분)
        pairs_path = base / "ko_ja_pairs.json"
        try:
            write_json(update_pairs_actual_ends(read_json(pairs_path), events), pairs_path)
        except Exception as e:  # noqa: BLE001 — 대역은 검수 편의지 렌더 정본이 아니다
            log.warning("ko_ja_pairs 실측 end 갱신 실패(무시): %s", e)

    if mux:
        dconf = config.get("dub", {})
        bg = float(dconf.get("bg_volume", 0.3))
        bg_audio = None
        if dconf.get("remove_original_vocals", False):
            nov = separate_vocals(video, base / "stems", config)            # 반주/효과음 스템
            if dconf.get("dialogue_only", False):
                # 대사 구간의 보컬만 제거(일본어 더빙으로 교체) + 리액션/씹는소리는 원본 유지.
                voc = Path(nov).parent / "vocals.wav"
                _mute_windows(voc, base / "reactions.wav",
                              [(s["start"], s["end"]) for s in segs])
                _mix_two(nov, base / "reactions.wav", base / "bg_reactions_mix.wav")
                bg_audio = str(base / "bg_reactions_mix.wav")
                bg = max(bg, 0.85)                                           # 리액션/ASMR 또렷하게
                log.info("dialogue_only: 대사 구간만 원본 제거, 리액션/씹는소리 보존")
            else:
                bg_audio = str(nov)
                bg = max(bg, 0.4)                                            # ASMR/반주 보존
                log.info("원본 보컬 제거(Demucs) → 반주 스템 믹스")
        # ASMR 다이내믹 보존: loudnorm 대신 limiter 로 피크만 제한(째짐 방지)
        out = _common.mux_dub(video, res["draft"], base / "final_dubbed.mp4",
                              bg_volume=bg, voice_volume=float(dconf.get("voice_volume", 1.1)),
                              bg_audio=bg_audio, loudnorm=bool(dconf.get("loudnorm", False)),
                              limiter=bool(dconf.get("limiter", True)),
                              limit=float(dconf.get("peak_limit", 0.97)))
        res["dubbed_video"] = str(out)
        log.info("더빙 영상(초안): %s", out)
        # 화면에 한국어 자막(번인 텍스트)이 없는 영상 → 더빙된 일본어 오디오에 맞춘
        # 일본어 자막을 번인(시청자가 대사를 읽을 수 있게). ASR 타이밍 그대로 사용.
        if dconf.get("burn_dub_subtitle", True):
            meta = _common.probe(video)
            # 원본 한국어 캡션과 공존 배치(사용자 결정 2026-07-10: 캡션 제거 대신 위치 회피):
            # precheck.json 의 캡션 bbox 하단 밴드 위로 일본어 자막을 올린다.
            margin_v = None
            if dconf.get("subtitle_avoid_captions", True):
                pre_path = base / "precheck.json"
                if pre_path.exists():
                    try:
                        from app.localize.overlay.precheck import caption_margin_v
                        pre = read_json(pre_path)
                        pc = config.get("autopilot", {}).get("precheck", {})
                        margin_v = caption_margin_v(
                            pre.get("ocr_frames", []), int(meta["height"]),
                            float(pc.get("min_conf", 0.75)), int(pc.get("min_hangul", 2)))
                        log.info("자막 배치: 캡션 회피 MarginV=%d (기본 30)", margin_v)
                    except Exception as e:  # noqa: BLE001 — 배치 실패는 기본 위치로
                        log.warning("캡션 회피 배치 실패(%s) — 기본 위치", e)
            ja_ass = base / "ja_dub.ass"
            ja_ass.write_text(
                render_mod.build_ass(events, meta["width"], meta["height"],
                                     int(config.get("render", {}).get("line_max_chars", 26)),
                                     margin_v=margin_v),
                encoding="utf-8")
            subbed = _common.burn_subtitles(
                str(out), str(ja_ass), base / "final_dubbed_subbed.mp4",
                fonts_dir=str(resolve_path(config["paths"]["fonts_dir"])))
            res["dubbed_video_subbed"] = str(subbed)
            log.info("일본어 더빙 자막 번인: %s", subbed)
    return res


def _fit_audio(in_path: Path, out_path: Path, target_sec: float, max_speedup: float = 1.6,
               max_len: Optional[float] = None) -> None:
    """합성 음성을 슬롯 길이에 맞게 time-stretch(피치 유지). 과도한 변형은 클램프.

    max_len 지정 시: 배속 후에도 그 길이를 넘으면 잘라내고 끝에 짧은 페이드아웃.
    → 한 세그의 환각/과길이가 '다음 발화' 위로 겹쳐 깔리는 드론을 구조적으로 차단.
    """
    import subprocess

    dur = common.probe(in_path).get("duration", 0.0)
    if dur <= 0 or target_sec <= 0:
        out_path.write_bytes(in_path.read_bytes())
    else:
        speed = _fit_speed(dur, target_sec, max_speedup)
        if abs(speed - 1.0) < 0.05:                # 충분히 근접 → 그대로
            out_path.write_bytes(in_path.read_bytes())
        else:
            subprocess.run([common.ffmpeg_bin(), "-y", "-i", str(in_path), "-filter:a",
                            atempo_filters(speed), str(out_path)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if max_len is not None:                         # 침범 방지 캡
        cur = common.probe(out_path).get("duration", 0.0)
        if _needs_truncate(cur, max_len):
            tmp = out_path.with_suffix(".cap" + out_path.suffix)
            fade_st = max(0.0, max_len - 0.12)
            subprocess.run([common.ffmpeg_bin(), "-y", "-i", str(out_path), "-t", f"{max_len:.3f}",
                            "-af", f"afade=t=out:st={fade_st:.3f}:d=0.12", str(tmp)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tmp.replace(out_path)


def needs_brighten(cur_centroid: float, target_centroid: float, tol: float = 0.98) -> bool:
    """더빙 밝기(centroid)가 원본보다 유의하게 낮으면 보정 필요."""
    return cur_centroid > 0 and target_centroid > 0 and cur_centroid < target_centroid * tol


def brighten_track(wav: Path, target_centroid: float, config: dict[str, Any]) -> float:
    """더빙 보이스 밝기를 원본 target_centroid 에 맞게 하이쉘프+프레즌스 부스트(적응형).

    자음 또렷함(단어 정확도) + 음색 일치 동시 개선. 게인은 원본 대비 부족분에 따라 자동,
    max_gain_db 로 상한(과하면 쉭/노이즈 증폭). 반환: 적용 게인(dB). 비활성/불필요 시 0."""
    import shutil
    import subprocess

    from app.localize.overlay.refbank import spectral_centroid

    bconf = config.get("dub", {}).get("brighten", {})
    if not bconf.get("enabled", True) or target_centroid <= 0:
        return 0.0
    max_db = float(bconf.get("max_gain_db", 8.0))
    shelf_hz = float(bconf.get("shelf_hz", 2500))
    presence_hz = float(bconf.get("presence_hz", 3500))
    step = float(bconf.get("step_db", 2.0))

    def _cen(p: Path) -> float:
        import soundfile as sf
        x, sr = sf.read(str(p))
        return spectral_centroid(x, sr)

    cur = _cen(wav)
    if not needs_brighten(cur, target_centroid):
        log.info("밝기 보정 생략(이미 충분: %.0f ≥ %.0f)", cur, target_centroid)
        return 0.0
    orig = wav.with_suffix(".prebright.wav")
    shutil.copy(str(wav), str(orig))
    gain, out_cen = 0.0, cur
    while gain < max_db and needs_brighten(out_cen, target_centroid):
        gain = min(max_db, gain + step)
        # 하이쉘프(전반적 밝기) + 프레즌스 피킹(자음/명료도) + 리미터(피크 보호)
        af = (f"highshelf=f={shelf_hz}:g={gain:.1f},"
              f"equalizer=f={presence_hz}:width_type=q:w=1.2:g={gain * 0.6:.1f},"
              f"alimiter=limit=0.97")
        subprocess.run([common.ffmpeg_bin(), "-y", "-v", "error", "-i", str(orig),
                        "-af", af, str(wav)], check=True)
        out_cen = _cen(wav)
    orig.unlink(missing_ok=True)
    log.info("밝기 보정: +%.1fdB(shelf@%.0f, presence@%.0f) → centroid %.0f→%.0f (목표 %.0f)",
             gain, shelf_hz, presence_hz, cur, out_cen, target_centroid)
    return gain


def _assemble_timeline(seg_files: list[tuple[float, Path]], out: Path) -> None:
    """각 세그먼트를 시작 시각에 배치해 한 트랙으로 mix(ffmpeg adelay+amix)."""
    if not seg_files:
        log.warning("세그먼트 없음 → 더빙 트랙 생략")
        return
    if not common.has_ffmpeg():
        raise RuntimeError("ffmpeg 필요(더빙 트랙 합성).")
    import subprocess

    cmd: list[str] = [common.ffmpeg_bin(), "-y"]
    for _, fp in seg_files:
        cmd += ["-i", str(fp)]
    parts, labels = [], []
    for idx, (start, _) in enumerate(seg_files):
        ms = int(start * 1000)
        parts.append(f"[{idx}]adelay={ms}|{ms}[a{idx}]")
        labels.append(f"[a{idx}]")
    filt = ";".join(parts) + ";" + "".join(labels) + \
        f"amix=inputs={len(seg_files)}:normalize=0[out]"
    cmd += ["-filter_complex", filt, "-map", "[out]", str(out)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _normalize_track(wav: Path, target_peak: float = 0.9) -> None:
    """보이스 트랙 피크 정규화(헤드룸 확보). amix 합산 후 클리핑/limiter 펌핑 완화."""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:                              # 의존성 없으면 건너뜀(원본 유지)
        return
    a, sr = sf.read(str(wav))
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    scale = _norm_scale(peak, target_peak)
    if abs(scale - 1.0) > 1e-3:
        sf.write(str(wav), a * scale, sr)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Level C 더빙 초안(드래프트 오디오까지)")
    p.add_argument("--video-id", required=True)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--subtitle", help="ja.srt/ja.ass (자막 기반 더빙)")
    src.add_argument("--video", help="대사 있는 영상 (ASR→번역→더빙 풀 플로우)")
    p.add_argument("--level", default="C", help="C 가 아니면 거부")
    p.add_argument("--backend", default=None, help="xtts(오픈소스 클로닝) | elevenlabs")
    p.add_argument("--speaker", default=None, help="xtts: 클로닝용 음성 샘플(wav/mp3) 경로")
    p.add_argument("--voice", default=None, help="elevenlabs: voice_id")
    p.add_argument("--source-lang", default="ko", help="--video ASR 원본 언어")
    p.add_argument("--config", default=None)
    return p.parse_args(argv)


def _probe_ref_main(argv: list[str]) -> None:
    """`--probe-ref` 모드 — 레퍼런스 1개를 시험 합성해 exit 0(정상)/1(무음성 퇴화).

    반드시 별도 프로세스로 호출할 것: 퇴화 레퍼런스는 프로세스 내 모델 캐시를
    오염시켜 이후 모든 합성을 무음으로 만든다(2026-07-08 실측 — 모듈 리셋으로도
    복구 불가, 하위 모듈 캐시 잔존). 격리가 유일하게 확실한 방역."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--probe-ref", required=True)
    p.add_argument("--prompt-text", required=True)
    p.add_argument("--config", default=None)
    a = p.parse_args(argv)
    config = load_config(a.config)
    import copy
    cfg = copy.deepcopy(config)
    g = cfg["dub"]["gptsovits"]
    g["ref_wav"], g["prompt_text"], g["prompt_lang"] = a.probe_ref, a.prompt_text, "ko"
    g["aux_refs"], g["pitch_match_tries"], g["retry_tries"] = [], 1, 1
    try:
        _synthesize_gptsovits("ルーピー", "ja", cfg)
        print("PROBE_OK")
        sys.exit(0)
    except Exception as e:
        print(f"PROBE_FAIL: {str(e)[:120]}")
        sys.exit(1)


def main(argv: Optional[list[str]] = None) -> None:
    argv = list(sys.argv[1:]) if argv is None else argv
    if any(a.startswith("--probe-ref") for a in argv):
        _probe_ref_main(argv)
        return
    args = _parse_args(argv)
    config = load_config(args.config)
    if args.backend:
        config.setdefault("dub", {})["tts_backend"] = args.backend
    if args.video:
        dub_from_video(args.video_id, args.video, args.level, config,
                       speaker_wav=args.speaker, source_lang=args.source_lang)
    else:
        dub(args.video_id, args.subtitle, args.level, config,
            voice_id=args.voice, speaker_wav=args.speaker)


if __name__ == "__main__":
    main()
