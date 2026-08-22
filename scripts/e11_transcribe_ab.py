"""E11-3 — 자막 전사 백엔드 A/B 실측 도구 (내장 Whisper vs ElevenLabs Scribe).

발주서가 요구하는 5항목을 **한 번에 재는** 스크립트다. ai-video 를 통째로 두 번
돌리지 않고 같은 오디오를 두 백엔드에 먹여 비교한다 — 파이프라인 전체 A/B 는
`--transcribe-backend` 를 붙인 create_shorts 두 판으로 따로 돌린다.

    # 정확도·싱크·시간·요금 (레퍼런스 자막이 있으면 CER 까지)
    python -m scripts.e11_transcribe_ab --video 소스.mp4 --ref 정답자막.srt

    # 파이프라인 회귀 0 대조 — 같은 job 을 플래그 없이/`default` 로 돌린 산출 비교
    python -m scripts.e11_transcribe_ab --diff outputs/JOB_A outputs/JOB_B

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


def run_backends(video: Path, language: str) -> dict:
    from app.modules import stt_elevenlabs
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
    }

    print("── elevenlabs: Scribe ──")
    stt_elevenlabs.reset_usage()
    t0 = time.time()
    el_segs = stt_elevenlabs.transcribe(video, language=language)
    usage = stt_elevenlabs.usage_summary()
    out["elevenlabs"] = {
        "elapsed_sec": round(time.time() - t0, 1),
        "segments": len(el_segs),
        "text": _join(el_segs),
        "sync": _sync_row(el_segs),
        "cost_usd": usage["estimated_usd"],
        "audio_duration_secs": usage["audio_duration_secs"],
        "low_confidence_lines": usage["low_confidence_lines"],
        # 5항목 ⑤ — tag_audio_events=false 로도 (laughter) 류가 새는지
        "audio_event_leaks": [s.text for s in el_segs
                              if re.search(r"\((laughter|applause|music|footsteps|웃음)\)",
                                           s.text, re.I)],
    }
    return out


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
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description="E11-3 전사 백엔드 A/B 실측")
    ap.add_argument("--video", type=Path, help="소스 영상/오디오 (예능 대사가 섞인 한국어 1편)")
    ap.add_argument("--ref", type=Path, default=None,
                    help="정답 자막(SRT/ASS/VTT/SMI) — 있으면 CER 을 잰다")
    ap.add_argument("--language", default="ko")
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument("--diff", nargs=2, type=Path, metavar=("JOB_A", "JOB_B"),
                    help="회귀 0 대조: 두 출력 디렉토리의 자막 산출물 해시 비교")
    args = ap.parse_args()

    if args.diff:
        print("[회귀 0 대조]")
        raise SystemExit(1 if diff_outputs(*args.diff) else 0)

    if not args.video:
        ap.error("--video 또는 --diff 중 하나는 필요합니다")

    result = run_backends(args.video, args.language)

    if args.ref and args.ref.exists():
        ref_text = _join(parse_subtitle(args.ref))
        for name in ("default", "elevenlabs"):
            result[name]["cer"] = round(cer(ref_text, result[name]["text"]), 4)

    print("\n" + "=" * 60)
    for name, r in result.items():
        print(f"[{name}] cue {r['segments']}개 · {r['elapsed_sec']}s · ${r['cost_usd']:.5f}"
              + (f" · CER {r['cer'] * 100:.1f}%" if "cer" in r else ""))
        for st, en, tx in r["sync"]:
            print(f"    {st:8.2f}~{en:8.2f}  {tx}")
    leaks = result["elevenlabs"]["audio_event_leaks"]
    print(f"\n오디오 이벤트 누출: {len(leaks)}건 {leaks[:3]}")

    if args.json_out:
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"저장: {args.json_out}")


if __name__ == "__main__":
    main()
