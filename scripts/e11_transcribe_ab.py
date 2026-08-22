"""E11-3 — 자막 전사 백엔드 A/B 실측 도구 (내장 Whisper vs ElevenLabs Scribe).

발주서가 요구하는 5항목을 **한 번에 재는** 스크립트다. ai-video 를 통째로 두 번
돌리지 않고 같은 오디오를 두 백엔드에 먹여 비교한다 — 파이프라인 전체 A/B 는
`--transcribe-backend` 를 붙인 create_shorts 두 판으로 따로 돌린다.

    # 정확도·싱크·시간·요금 (레퍼런스 자막이 있으면 CER 까지)
    python -m scripts.e11_transcribe_ab --video 소스.mp4 --ref 정답자막.srt \
        --keyterm SK텔레콤 --keyterm NC소프트

    # 파이프라인 회귀 0 대조 — 같은 job 을 플래그 없이/`default` 로 돌린 산출 비교
    python -m scripts.e11_transcribe_ab --diff outputs/JOB_A outputs/JOB_B

E13(2026-08-22)부터 Scribe 는 기본으로 **keyterms on/off 두 판**을 돌린다
(`--keyterms-mode`). 발주 보고 5항목 중 ①표기 보존·②언어 이탈·③요금·⑤cue 분포가
한 실행의 stdout 에 그대로 찍힌다. ④회귀 0 만은 여기서 못 잰다 — 이 스크립트는
전사 단계만 돌리므로, 완성본 비교는 `--diff` 로 파이프라인 두 판을 놓고 봐야 한다.

⚠ 한국어는 **WER 대신 CER** 로 잰다 — 띄어쓰기 규칙 차이만으로 WER 이 10%p 넘게
흔들려 같은 소리를 두고 엉뚱한 결론이 난다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.speech import SpeechSegment  # noqa: E402
from app.modules.subtitle import parse_subtitle  # noqa: E402

# 운영 노드는 키를 레포 .env 에 두고 dotenv 로 읽는다(gemini_client 와 같은 규약).
# 이 스크립트는 gemini_client 를 import 하지 않으므로 여기서 직접 로드한다 —
# 안 하면 키가 .env 에 멀쩡히 있는데도 "키 없음"으로 죽는다.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

_PUNCT = re.compile(r"[\s.,!?…·\"'’“”\-—~()\[\]]+")


def _norm(text: str) -> str:
    """CER 비교용 정규화 — 공백·문장부호 제거(띄어쓰기 규칙 차이를 점수에서 뺀다)."""
    return _PUNCT.sub("", text or "")


def cer(reference: str, hypothesis: str) -> float:
    """문자 오류율 = (치환+삽입+삭제) / 정답 문자수. 표준 Levenshtein."""
    ref, hyp = _norm(reference), _norm(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, 1):
        cur = [i]
        for j, hc in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(ref)


def _join(segs: list[SpeechSegment]) -> str:
    return " ".join(s.text.strip() for s in segs if s.text and s.text.strip())


def _sync_row(segs: list[SpeechSegment]) -> list[tuple[float, float, str]]:
    """싱크 확인용 — 첫 줄·중간 줄·끝 줄."""
    if not segs:
        return []
    picks = [0, len(segs) // 2, len(segs) - 1]
    return [(segs[i].start_sec, segs[i].end_sec, segs[i].text[:40])
            for i in sorted(set(picks))]


def _cue_len_stats(segs: list[SpeechSegment]) -> dict:
    """E13-3 ⑤ — cue 수·길이 분포. 이번 변경이 cue 분할을 흔들지 않았는지 보는 대조군."""
    if not segs:
        return {"count": 0}
    durs = sorted(max(0.0, s.end_sec - s.start_sec) for s in segs)
    chars = sorted(len(s.text) for s in segs)

    def _pct(xs, p):
        return round(xs[min(len(xs) - 1, int(len(xs) * p))], 2)

    return {
        "count": len(segs),
        "dur_p50": _pct(durs, 0.5), "dur_p90": _pct(durs, 0.9), "dur_max": round(durs[-1], 2),
        "chars_p50": _pct(chars, 0.5), "chars_max": chars[-1],
    }


def _run_scribe(video: Path, language: str, *, keyterms: list[str] | None, label: str) -> dict:
    from app.modules import stt_elevenlabs

    print(f"── {label}: Scribe ──")
    stt_elevenlabs.reset_usage()
    t0 = time.time()
    segs = stt_elevenlabs.transcribe(video, language=language, keyterms=keyterms)
    usage = stt_elevenlabs.usage_summary()
    return {
        "elapsed_sec": round(time.time() - t0, 1),
        "segments": len(segs),
        "text": _join(segs),
        "sync": _sync_row(segs),
        "cost_usd": usage["estimated_usd"],
        "audio_duration_secs": usage["audio_duration_secs"],
        "usd_per_audio_hour": (usage["usd_per_audio_hour_keyterms"] if keyterms
                               else usage["usd_per_audio_hour"]),
        "keyterms": list(keyterms or []),
        "low_confidence_lines": usage["low_confidence_lines"],
        "low_confidence_count": usage["low_confidence_count"],
        # E13-2a — 언어 이탈로 버린 줄. **전량**을 실어야 오탐 0건을 눈으로 확인할 수 있다.
        "dropped_language_escape": usage["dropped_language_escape"],
        # E13-1b — 표기 보정으로 고쳐 쓴 내역.
        "normalized": usage["normalized"],
        "cue_stats": _cue_len_stats(segs),
        # 5항목 ⑤ — tag_audio_events=false 로도 (laughter) 류가 새는지
        "audio_event_leaks": [s.text for s in segs
                              if re.search(r"\((laughter|applause|music|footsteps|웃음)\)",
                                           s.text, re.I)],
    }


def run_backends(video: Path, language: str, *, keyterms_mode: str,
                 keyterms: list[str]) -> dict:
    from app.modules.speech import extract_transcript

    out: dict = {}

    print("── 내장(default): faster-whisper large-v3-turbo ──")
    t0 = time.time()
    default_segs = extract_transcript(video)
    out["default"] = {
        "elapsed_sec": round(time.time() - t0, 1),
        "segments": len(default_segs),
        "text": _join(default_segs),
        "sync": _sync_row(default_segs),
        "cost_usd": 0.0,
        "cue_stats": _cue_len_stats(default_segs),
    }

    # E13-1a: keyterms on/off 를 한 번에 재려고 두 판을 돌린다 — '표기 5건이 살았는가'는
    # 같은 오디오로 나란히 놓고 봐야 판정이 된다(요금은 그만큼 두 배로 나간다).
    if keyterms_mode in ("off", "both"):
        out["elevenlabs"] = _run_scribe(video, language, keyterms=None,
                                        label="elevenlabs (keyterms off)")
    if keyterms_mode in ("on", "both"):
        out["elevenlabs_keyterms"] = _run_scribe(video, language, keyterms=keyterms,
                                                 label="elevenlabs (keyterms on)")
    return out


# E13-3 ① — 실측에서 뭉갰던 표기 5건. 재측정에서 **건별로** 살았는지 센다.
NOTATION_PROBES = ["SK텔레콤", "NC소프트", "CTO", "IT업계", "30년"]


def notation_report(result: dict) -> dict:
    """각 판의 전사 텍스트에 표기 5건이 몇 번 나오는지 — 발주 보고 1번 항목."""
    return {name: {p: r["text"].count(p) for p in NOTATION_PROBES}
            for name, r in result.items() if r.get("text") is not None}


def diff_outputs(a: Path, b: Path) -> int:
    """회귀 0 대조 — 두 job 디렉토리의 자막 관련 산출물을 해시로 비교."""
    names = ["subtitle_segments.json", "checkpoint_chunk_transcripts.json",
             "edit_plan.json", "subtitles.ass", "tts_subtitles.ass"]
    bad = 0
    for n in names:
        pa, pb = a / n, b / n
        if not pa.exists() and not pb.exists():
            print(f"  - {n}: 양쪽 다 없음 (건너뜀)")
            continue
        if not pa.exists() or not pb.exists():
            print(f"  ✗ {n}: 한쪽만 존재 ({pa.exists()}/{pb.exists()})")
            bad += 1
            continue
        ha = hashlib.sha256(pa.read_bytes()).hexdigest()[:12]
        hb = hashlib.sha256(pb.read_bytes()).hexdigest()[:12]
        mark = "✓" if ha == hb else "✗"
        bad += ha != hb
        print(f"  {mark} {n}: {ha} / {hb}")
        if ha != hb:
            _print_first_diff(pa, pb)
    return bad


def _print_first_diff(pa: Path, pb: Path, *, max_lines: int = 5) -> None:
    """해시가 갈렸을 때 **어디가** 갈렸는지 첫 몇 줄만 — 회귀를 찾으려면 값이 필요하다."""
    try:
        la = pa.read_text(encoding="utf-8").splitlines()
        lb = pb.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        print("      (바이너리 또는 읽기 실패 — 내용 비교 생략)")
        return
    shown = 0
    for i in range(max(len(la), len(lb))):
        a = la[i] if i < len(la) else "<없음>"
        b = lb[i] if i < len(lb) else "<없음>"
        if a != b:
            print(f"      L{i + 1} A: {a[:100]}")
            print(f"      L{i + 1} B: {b[:100]}")
            shown += 1
            if shown >= max_lines:
                print("      …")
                return
    if not shown:
        print("      (줄 내용은 같음 — 줄바꿈·인코딩 차이)")


def main() -> None:
    ap = argparse.ArgumentParser(description="E11-3 전사 백엔드 A/B 실측")
    ap.add_argument("--video", type=Path, help="소스 영상/오디오 (예능 대사가 섞인 한국어 1편)")
    ap.add_argument("--ref", type=Path, default=None,
                    help="정답 자막(SRT/ASS/VTT/SMI) — 있으면 CER 을 잰다")
    ap.add_argument("--language", default="ko")
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument("--diff", nargs=2, type=Path, metavar=("JOB_A", "JOB_B"),
                    help="회귀 0 대조: 두 출력 디렉토리의 자막 산출물 해시 비교")
    # E13-1a — keyterms 재측정. 파이프라인은 리서치 결과(작품명·인물명)를 자동으로
    # 넘기지만, 이 스크립트는 파이프라인을 안 타므로 사람이 직접 준다.
    ap.add_argument("--keyterms-mode", choices=("on", "off", "both"), default="both",
                    help="Scribe 를 keyterms on/off/양쪽으로 돌린다 (기본 both)")
    ap.add_argument("--keyterm", action="append", default=[],
                    help="keyterms 항목 (여러 번). 작품명·인물명·회사명 등")
    args = ap.parse_args()

    if args.diff:
        print("[회귀 0 대조]")
        raise SystemExit(1 if diff_outputs(*args.diff) else 0)

    if not args.video:
        ap.error("--video 또는 --diff 중 하나는 필요합니다")
    if args.keyterms_mode in ("on", "both") and not args.keyterm:
        ap.error("--keyterms-mode on/both 에는 --keyterm 이 하나 이상 필요합니다 "
                 "(예: --keyterm SK텔레콤 --keyterm NC소프트) — 빈 목록으로 돌리면 "
                 "off 판과 같은 조건이라 비교가 성립하지 않습니다")

    result = run_backends(args.video, args.language,
                          keyterms_mode=args.keyterms_mode, keyterms=args.keyterm)

    if args.ref and args.ref.exists():
        ref_text = _join(parse_subtitle(args.ref))
        for name in result:
            result[name]["cer"] = round(cer(ref_text, result[name]["text"]), 4)

    print("\n" + "=" * 60)
    for name, r in result.items():
        print(f"[{name}] cue {r['segments']}개 · {r['elapsed_sec']}s · ${r['cost_usd']:.5f}"
              + (f" (${r['usd_per_audio_hour']}/오디오시간)" if "usd_per_audio_hour" in r else "")
              + (f" · CER {r['cer'] * 100:.1f}%" if "cer" in r else ""))
        if r.get("cue_stats"):
            cs = r["cue_stats"]
            print(f"    cue 길이 p50 {cs.get('dur_p50')}s / p90 {cs.get('dur_p90')}s / "
                  f"max {cs.get('dur_max')}s · 글자수 p50 {cs.get('chars_p50')}")
        for st, en, tx in r["sync"]:
            print(f"    {st:8.2f}~{en:8.2f}  {tx}")

    print("\n[① 표기 보존 — 각 판에서 몇 번 나오는가]")
    for name, counts in notation_report(result).items():
        print(f"  {name}: " + " · ".join(f"{k} {v}" for k, v in counts.items()))

    for name, r in result.items():
        if "dropped_language_escape" not in r:
            continue
        dropped = r["dropped_language_escape"]
        print(f"\n[② {name} 언어 이탈로 버린 줄 {len(dropped)}건] "
              f"— 오탐 0건이 통과 조건이다, 전량 확인할 것")
        for d in dropped:
            print(f"    {d['start_sec']:8.2f}~{d['end_sec']:8.2f}  {d['text']!r} "
                  f"(이탈 {d['foreign_chars']}자 / 정상 {d['native_chars']}자)")
        if r.get("normalized"):
            print(f"  [표기 보정 {sum(r['normalized'].values())}건] {r['normalized']}")
        print(f"  [저확신 줄 {r.get('low_confidence_count', 0)}건 — 버리지 않고 표시만]")
        leaks = r["audio_event_leaks"]
        print(f"  [오디오 이벤트 누출 {len(leaks)}건] {leaks[:3]}")

    if args.json_out:
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"저장: {args.json_out}")


if __name__ == "__main__":
    main()
