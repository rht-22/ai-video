"""L-P0 — 현지화 엔진 A/B 대조 도구 (vlp localize_run vs ai-video app.localize).

발주서: ves-orchestrator `docs/LOCALIZE_UNIFY.md` §8(회귀 0 계약)·§9(P0).
이관이 성공했다는 것은 새 기능이 도는 게 아니라 **기존 것이 하나도 안 변했다**는
뜻이다. 그 판정을 사람 눈이 아니라 숫자로 하기 위한 도구다.

    # 두 job 디렉토리(구 엔진 산출 · 신 엔진 산출)를 대조
    python -m scripts.localize_ab --a outputs/JOB_OLD --b outputs/JOB_NEW

    # 같은 job 을 두 번 돌린 경우 — 돌리기 전에 스냅샷을 떠 둔다
    python -m scripts.localize_ab --snapshot outputs/JOB --to /tmp/snap_old
    python -m scripts.localize_ab --a /tmp/snap_old --b outputs/JOB

무엇을 재는가 (§8-2 판정 방법 그대로):
  ① 최종 mp4      길이 · 크기 · 샘플 프레임 해시
  ② 자막          subtitle_segments.json 바이트 동일성 + 줄 단위 차이
  ③ 메타          localize_ja/metadata.json 의 제목·설명·ko_ja_pairs
  ④ 백업          localize_backup_ko/ 가 한국어 원본을 그대로 들고 있는가

⚠ **번역문은 LLM 이라 런마다 다르다.** 이 도구로 "번역이 같은가"를 물으면 안 된다.
회귀 대조는 캐시된 translation.json 을 **고정 입력으로 주입**한 두 판을 놓고
렌더 계층만 본다(§8-2). 번역 품질은 사람 검수·역번역 QA 의 몫이다.
그래서 기본 판정에서 번역 텍스트 차이는 `translation` 항목으로 **따로 분리**해
보여주고 회귀 판정(verdict)에는 넣지 않는다 — `--strict` 로만 포함시킨다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.ffmpeg_utils import find_ffmpeg_command  # noqa: E402

# 대조 대상 — localize 계약(§3-3)이 산출한다고 약속한 파일들.
# 없는 파일은 '양쪽 다 없으면 통과, 한쪽만 없으면 실패'로 다룬다(조용한 누락 방지).
RENDER_FILES = ("shorts.mp4",)
DATA_FILES = ("subtitle_segments.json", "edit_plan.json",
              "checkpoint_story.json", "checkpoint_resources.json")
META_FILE = "localize_ja/metadata.json"
BACKUP_DIR = "localize_backup_ko"
SNAPSHOT_FILES = RENDER_FILES + DATA_FILES + (META_FILE, "shorts_ko.mp4", "title.txt")

FRAME_SAMPLES = 12          # 샘플 프레임 수 — 전 프레임 해시는 60초물에도 분 단위가 걸린다
DUR_TOL_SEC = 0.05          # 길이 허용 오차. 인코더 재현성은 ms 단위로 흔들린다


# ─────────────────────────── 순수 (테스트 대상) ───────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_json(obj):
    """대조용 정규화 — 키 순서·부동소수 표기 차이를 없앤다.

    시각은 ms(소수 3자리)로 반올림한다. 렌더 파이프라인이 초를 float 로 굴리는데
    같은 계산도 경로가 다르면 1e-9 자리가 흔들려서, 그대로 비교하면 의미 없는 차이가
    전부 회귀로 잡힌다."""
    if isinstance(obj, dict):
        return {k: norm_json(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [norm_json(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 3)
    return obj


def json_equal(a, b) -> bool:
    return norm_json(a) == norm_json(b)


def diff_segments(a: list, b: list, *, limit: int = 8) -> list[str]:
    """자막 세그먼트 줄 단위 차이 — 사람이 읽을 요약. 순수."""
    out: list[str] = []
    if len(a) != len(b):
        out.append(f"줄 수: {len(a)} → {len(b)}")
    for i, (x, y) in enumerate(zip(a, b)):
        if len(out) >= limit:
            out.append("…(생략)")
            break
        nx, ny = norm_json(x), norm_json(y)
        if nx == ny:
            continue
        keys = sorted(set(nx) | set(ny))
        moved = [k for k in keys if nx.get(k) != ny.get(k)]
        head = str(nx.get("text") or ny.get("text") or "")[:24]
        out.append(f"[{i}] {head!r} 달라진 키: {', '.join(moved)}")
    return out


def flatten_pairs(pairs) -> list[dict]:
    """`ko_ja_pairs` → 비교용 평평한 행 목록. 순수 — 테스트 대상.

    ⚠ 실제 `metadata.json` 의 `ko_ja_pairs` 는 **리스트가 아니라 dict** 다
    (`{top_title, subs[], tts[], telops[]}`). 리스트로 가정하면 dict 를 순회해
    **키(문자열)** 가 나오고 `.get` 이 없어 죽는다(노드 실측에서 그렇게 죽었다).
    옛 판(리스트)도 받아 준다."""
    if isinstance(pairs, list):
        return [r for r in pairs if isinstance(r, dict)]
    if not isinstance(pairs, dict):
        return []
    rows: list[dict] = []
    top = pairs.get("top_title")
    if isinstance(top, dict):
        rows.append({**top, "_sec": "top_title", "idx": 0})
    for sec in ("subs", "tts", "telops"):
        for r in pairs.get(sec) or []:
            if isinstance(r, dict):
                rows.append({**r, "_sec": sec})
    return rows


def pair_diff(a, b, *, limit: int = 8) -> list[str]:
    """ko_ja_pairs 대조 — 한국어(ko)가 달라지면 번역 이전 단계가 흔들린 것이라 중대하고,
    일본어(ja)만 달라지면 LLM 비결정성일 수 있다. 그래서 둘을 나눠 센다. 순수."""
    a, b = flatten_pairs(a), flatten_pairs(b)
    out: list[str] = []
    if len(a) != len(b):
        out.append(f"쌍 수: {len(a)} → {len(b)}")
    ko_moved = ja_moved = 0
    for i, (x, y) in enumerate(zip(a, b)):
        if (x or {}).get("ko") != (y or {}).get("ko"):
            ko_moved += 1
            if len(out) < limit:
                sec = (x or {}).get("_sec") or "?"
                out.append(f"[{sec}:{(x or {}).get('idx', i)}] ko 변경: "
                           f"{str((x or {}).get('ko'))[:30]!r} → "
                           f"{str((y or {}).get('ko'))[:30]!r}")
        if (x or {}).get("ja") != (y or {}).get("ja"):
            ja_moved += 1
    if ja_moved:
        out.append(f"ja 변경 {ja_moved}건 (LLM 비결정성일 수 있음 — 고정 입력으로 재확인)")
    if ko_moved:
        out.insert(0, f"⚠ ko 변경 {ko_moved}건 — 번역 이전 단계가 흔들렸다")
    return out


def verdict(findings: dict, *, strict: bool = False) -> tuple[bool, list[str]]:
    """항목별 결과 → 회귀 0 판정. 순수 — 테스트 대상.

    `translation` 은 기본 판정에서 뺀다(위 머리말 참조). --strict 면 포함한다."""
    blocking = [k for k, v in findings.items()
                if v.get("diff") and (strict or k != "translation")]
    lines = []
    for key, v in findings.items():
        mark = "OK " if not v.get("diff") else ("!! " if key != "translation" or strict else "~~ ")
        lines.append(f"{mark}{key}: {v.get('summary', '')}")
    return (not blocking), lines


def frame_timestamps(duration: float, n: int = FRAME_SAMPLES) -> list[float]:
    """샘플 시각 — 양끝을 피해 균등 분할. 0초는 페이드·검은 프레임이라 변별력이 없고,
    끝 프레임은 인코더 마감 처리 차이로 흔들린다. 순수."""
    if duration <= 0 or n <= 0:
        return []
    step = duration / (n + 1)
    return [round(step * (i + 1), 3) for i in range(n)]


# ─────────────────────────── IO ───────────────────────────

def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return None


def probe(video: Path) -> dict:
    """길이·크기 — ffprobe. 없으면 크기만."""
    info = {"exists": video.exists(),
            "bytes": video.stat().st_size if video.exists() else 0, "duration": 0.0}
    if not video.exists():
        return info
    ffprobe = find_ffmpeg_command("ffprobe")
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(video)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        try:
            info["duration"] = round(float(r.stdout.strip()), 3)
        except ValueError:
            pass
    return info


def frame_hashes(video: Path, stamps: list[float]) -> list[str]:
    """지정 시각의 프레임을 rawvideo 로 뽑아 해시. 인코딩 차이가 아니라 **그림**을 본다."""
    if not video.exists() or not stamps:
        return []
    ffmpeg = find_ffmpeg_command("ffmpeg")
    out = []
    for t in stamps:
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=120)
        out.append(sha256_bytes(r.stdout)[:16] if r.returncode == 0 and r.stdout else "-")
    return out


def snapshot(job: Path, dest: Path) -> int:
    """현지화 전/후 대조를 위해 산출 파일만 복사해 둔다. 원본 mp4 는 크므로 필요한 것만."""
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for rel in SNAPSHOT_FILES:
        src = job / rel
        if not src.exists():
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)
        n += 1
    bk = job / BACKUP_DIR
    if bk.is_dir():
        shutil.copytree(bk, dest / BACKUP_DIR, dirs_exist_ok=True)
        n += len(list(bk.rglob("*")))
    return n


def compare(a: Path, b: Path, *, strict: bool = False) -> tuple[bool, list[str], dict]:
    findings: dict = {}

    # ① 렌더 — 길이·프레임
    for rel in RENDER_FILES:
        pa, pb = probe(a / rel), probe(b / rel)
        if not pa["exists"] and not pb["exists"]:
            findings[f"render:{rel}"] = {"diff": False, "summary": "양쪽 다 없음"}
            continue
        if pa["exists"] != pb["exists"]:
            findings[f"render:{rel}"] = {"diff": True, "summary": "한쪽에만 있다"}
            continue
        dd = abs(pa["duration"] - pb["duration"])
        stamps = frame_timestamps(min(pa["duration"], pb["duration"]))
        ha, hb = frame_hashes(a / rel, stamps), frame_hashes(b / rel, stamps)
        mismatch = sum(1 for x, y in zip(ha, hb) if x != y)
        findings[f"render:{rel}"] = {
            "diff": dd > DUR_TOL_SEC or mismatch > 0,
            "summary": (f"길이 {pa['duration']}s → {pb['duration']}s (Δ{dd:.3f}s) · "
                        f"프레임 {len(ha) - mismatch}/{len(ha)} 일치")}

    # ② 데이터 파일 — 바이트/구조
    for rel in DATA_FILES:
        ja, jb = _read_json(a / rel), _read_json(b / rel)
        if ja is None and jb is None:
            findings[f"data:{rel}"] = {"diff": False, "summary": "양쪽 다 없음"}
            continue
        if (ja is None) != (jb is None):
            findings[f"data:{rel}"] = {"diff": True, "summary": "한쪽에만 있다"}
            continue
        same = json_equal(ja, jb)
        detail = ""
        if not same and rel == "subtitle_segments.json":
            sa = ja if isinstance(ja, list) else (ja or {}).get("segments") or []
            sb = jb if isinstance(jb, list) else (jb or {}).get("segments") or []
            detail = " | " + " ; ".join(diff_segments(sa, sb))
        findings[f"data:{rel}"] = {"diff": not same,
                                   "summary": ("동일" if same else "다름") + detail}

    # ③ 메타 — 제목·설명은 회귀, ko_ja_pairs 의 ja 는 번역 항목으로 분리
    ma, mb = _read_json(a / META_FILE), _read_json(b / META_FILE)
    if ma is None or mb is None:
        findings["meta"] = {"diff": ma is not mb, "summary": "metadata.json 한쪽 없음"
                            if ma is not mb else "양쪽 다 없음"}
    else:
        keys = ("youtube_title", "youtube_title_ko", "description", "description_ko")
        moved = [k for k in keys if ma.get(k) != mb.get(k)]
        findings["meta"] = {"diff": bool(moved),
                            "summary": "동일" if not moved else f"달라진 키: {', '.join(moved)}"}
        pd = pair_diff(ma.get("ko_ja_pairs") or [], mb.get("ko_ja_pairs") or [])
        findings["translation"] = {"diff": bool(pd),
                                   "summary": "동일" if not pd else " ; ".join(pd)}

    # ④ 백업 — 한국어 원본 보존
    fa, fb = sorted(p.name for p in (a / BACKUP_DIR).glob("*")), \
        sorted(p.name for p in (b / BACKUP_DIR).glob("*"))
    findings["backup"] = {"diff": fa != fb,
                          "summary": f"{len(fa)}개 → {len(fb)}개" + ("" if fa == fb else " (목록 다름)")}

    ok, lines = verdict(findings, strict=strict)
    return ok, lines, findings


def main() -> None:
    ap = argparse.ArgumentParser(description="현지화 엔진 A/B 대조 (회귀 0 판정)")
    ap.add_argument("--a", help="기준(구 엔진) job 디렉토리 또는 스냅샷")
    ap.add_argument("--b", help="대조(신 엔진) job 디렉토리")
    ap.add_argument("--snapshot", help="이 job 디렉토리의 산출을 --to 로 복사")
    ap.add_argument("--to", help="--snapshot 의 대상 경로")
    ap.add_argument("--strict", action="store_true",
                    help="번역문 차이도 회귀로 본다 (고정 translation.json 을 쓴 경우에만 의미)")
    ap.add_argument("--json", action="store_true", help="결과를 JSON 으로")
    args = ap.parse_args()

    if args.snapshot:
        if not args.to:
            ap.error("--snapshot 은 --to 가 필요합니다")
        n = snapshot(Path(args.snapshot), Path(args.to))
        print(f"[snapshot] {n}개 파일 → {args.to}")
        return

    if not (args.a and args.b):
        ap.error("--a 와 --b 가 필요합니다 (또는 --snapshot)")

    ok, lines, findings = compare(Path(args.a), Path(args.b), strict=args.strict)
    if args.json:
        print(json.dumps({"ok": ok, "findings": findings}, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== 현지화 A/B — {args.a} vs {args.b} ===")
        for ln in lines:
            print("  " + ln)
        print(f"\n판정: {'회귀 0' if ok else '차이 있음'}"
              f"{'' if args.strict else '  (번역문 차이는 ~~ 로 표시, 판정 제외)'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
