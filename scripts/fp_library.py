"""오디오 지문 도서관 — 원본 회차를 미리 지문으로 굽고, 쇼츠를 던지면 위치를 찾는다.

발주(2026-08-31 세션): 쇼츠가 원본 **어디에서** 왔는지를 소리로 자동 판정한다.
사용자의 선행 작업(아티팩트 「원본 영상과 쇼츠 자동 매칭 — 핑거프린팅 분석 보고」,
2026-07 · SNL 10회차 1,025쇼츠 99.9% 판정)의 구조를 이 레포에 옮긴 것이다.

왜 이 방법인가 — 대안(전사 텍스트 매칭)과 비교해 실측으로 우월하다:
  · SRT·전사가 필요 없다(코퍼스에서 SRT 있는 편은 4%뿐이었다).
  · **몇 화인지 몰라도 된다** — 전 회차와 한 번에 대조해 표가 몰린 곳이 답이다.
  · 자막·세로크롭·BGM 덮임에 견딘다(화면이 아니라 소리를 본다).
  · 계산이라 지어내지 않는다 — 표가 흩어지면 "원본에 없음"으로 답한다.
  · 도서관은 한 번만 굽고 쇼츠는 몇 개든 던진다(AI 비용이 쇼츠 수에 안 붙는다).

알고리즘은 Haitsma-Kalker 계열이고, 상수·수식은 이 기기의 선행 원형
`local-tools/audio-overlap/overlap_report.py` 와 **같은 값**을 쓴다 — 갈리면 그때
결과와 비교가 안 된다. 달라진 것은 구조뿐이다(쌍대쌍 비교 → 도서관 + 질의).

사용:
  python -m scripts.fp_library build --name apn --src <원본디렉토리> --out work/fp
  python -m scripts.fp_library match --lib work/fp/apn.npz --video <쇼츠.mp4>
  python -m scripts.fp_library match --lib work/fp/apn.npz --dir work/anatomy

⚠ 원본이 **전 회차가 아니면** 못 찾는 쇼츠가 정상적으로 나온다(다른 화에서 온 것).
   그 편은 `episode: null` 로 기록된다 — 억지로 붙이지 않는다.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 원형과 같은 상수 (local-tools/audio-overlap/overlap_report.py) ──────────
SR = 8000
WIN = 4096            # 0.512s
HOP = 640             # 0.08s → 12.5 fps
FPS = SR / HOP
N_BANDS = 34          # 33 차분 → 32비트
F_LO, F_HI = 300.0, 2000.0
MIN_SEG_SEC = 0.2     # 원형은 5.0 — 쇼츠 조각은 0.2초까지 잡는다(아래 VOTE_MIN 참조)
MAX_BER = 0.25
FRAME_BER = 10 / 32
GAP_FRAMES = 12       # 이 이하로 끊긴 것은 한 조각으로 잇는다(≈1초)
# ⚠ 원형은 12였는데 **정답을 아는 편에서 통째로 놓쳤다**(2026-08-31 실측).
#   tOilzWO_yVo(2화 내용 확인됨): 원시 투표 상위가 전부 2화인데 최다 5표라 12에 걸려
#   조각 0개로 버려졌다. 드라마 쇼츠는 원본 소리 위에 BGM 을 얹고 2초 안팎으로 잘게
#   썰어 건너뛰며 쓴다 — 정확 해시가 살아남는 프레임이 적어 표가 안 모인다.
#   낮춰도 특이도는 유지된다(실측 25편: 내레이션 167줄 중 오탐 3줄 = 2%).
VOTE_MIN = 3
MAX_POSTINGS = 400    # 흔한 해시(무음·상투적 음향)는 판별력이 없다
# ⚠ **질의 위상 훑기** — 이게 없으면 거의 아무것도 안 잡힌다(2026-08-31 실측).
#   해시는 HOP(80ms) 격자에 찍히는데 쇼츠 절단점은 아무 데나 있다. 실측:
#     정렬 절단(20.40s = 255프레임 정확) → offset 255 에 93/93 표
#     비정렬  (20.37s = 162.96프레임)    → 같은 offset 에  3/93 표  ← 미검출
#   그래서 **질의만** 여러 하위 위상으로 구워 가장 표가 몰리는 위상을 쓴다.
#   도서관은 한 위상 그대로다(굽는 비용·용량이 N배가 되면 안 된다). 쇼츠는
#   45초라 8배로 구워도 순식간이다.
QUERY_PHASES = 8      # HOP/8 = 10ms 간격 — 최악 오차 5ms

_POP = np.array([bin(i).count("1") for i in range(256)], np.uint8)


def decode(path: Path) -> np.ndarray:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"디코드 실패 {path.name}: {r.stderr[-200:].decode(errors='replace')}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def fingerprint(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """→ (uint32 해시열, 유효 프레임 마스크). 프레임 n 의 시각 = n*HOP/SR 초."""
    n = (len(audio) - WIN) // HOP + 1
    if n < 10:
        return np.zeros(0, np.uint32), np.zeros(0, bool)
    out_h = np.zeros(n - 1, np.uint32)
    out_v = np.zeros(n - 1, bool)
    edges = np.geomspace(F_LO, F_HI, N_BANDS + 1)
    freqs = np.fft.rfftfreq(WIN, 1 / SR)
    sels = [(freqs >= edges[b]) & (freqs < edges[b + 1]) for b in range(N_BANDS)]
    prev_d = None
    CH = 4096                                   # 청크로 나눠 메모리 상한을 둔다
    for s0 in range(0, n, CH):
        s1 = min(n, s0 + CH)
        idx = np.arange(WIN)[None, :] + (np.arange(s0, s1) * HOP)[:, None]
        fr = audio[idx] * np.hanning(WIN)[None, :]
        spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2
        E = np.empty((s1 - s0, N_BANDS), np.float64)
        for b, sel in enumerate(sels):
            E[:, b] = spec[:, sel].sum(axis=1)
        valid = E.sum(axis=1) > 1e-6
        E = np.log(E + 1e-12)
        d = E[:, :-1] - E[:, 1:]
        if prev_d is not None:
            d = np.vstack([prev_d, d])
            vfirst = True
        else:
            vfirst = False
        dd = d[1:] - d[:-1]
        bits = dd[:, :32] > 0
        h = np.zeros(len(bits), np.uint32)
        for k in range(32):
            h |= bits[:, k].astype(np.uint32) << k
        a = s0 - 1 if vfirst else s0
        out_h[a:a + len(h)] = h
        out_v[a:a + len(h)] = valid[1:] if not vfirst else valid
        prev_d = d[-1:]
    return out_h, out_v


def hamming(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x = (a ^ b).view(np.uint8).reshape(len(a), 4)
    return _POP[x].sum(axis=1)


# ── build ──────────────────────────────────────────────────────────────────
def cmd_build(args: argparse.Namespace) -> int:
    src = Path(args.src)
    files = sorted(p for p in src.iterdir()
                   if p.suffix.lower() in (".mp4", ".mkv", ".mov", ".m4a", ".wav"))
    if not files:
        raise SystemExit(f"원본 파일 없음: {src}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    eps, all_h, all_v, all_ep, all_fr = [], [], [], [], []
    for i, f in enumerate(files):
        audio = decode(f)
        h, v = fingerprint(audio)
        dur = len(audio) / SR
        eps.append({"idx": i, "file": f.name, "duration_sec": round(dur, 2),
                    "frames": int(len(h)), "valid": int(v.sum())})
        all_h.append(h)
        all_v.append(v)
        all_ep.append(np.full(len(h), i, np.int16))
        all_fr.append(np.arange(len(h), dtype=np.int32))
        print(f"  · {f.name}  {dur/60:.1f}분  지문 {len(h):,}개(유효 {v.sum():,})")

    H = np.concatenate(all_h)
    V = np.concatenate(all_v)
    EP = np.concatenate(all_ep)
    FR = np.concatenate(all_fr)
    np.savez_compressed(out / f"{args.name}.npz", H=H, V=V, EP=EP, FR=FR,
                        meta=np.array(json.dumps(
                            {"name": args.name, "episodes": eps,
                             "sr": SR, "win": WIN, "hop": HOP,
                             "bands": N_BANDS, "f_lo": F_LO, "f_hi": F_HI},
                            ensure_ascii=False)))
    tot = sum(e["duration_sec"] for e in eps)
    print(f"\n[build] {args.name}: {len(files)}회차 · {tot/3600:.2f}시간 · "
          f"지문 {len(H):,}개(유효 {int(V.sum()):,}) → {out/(args.name+'.npz')}")
    return 0


def load_lib(path: Path):
    z = np.load(path, allow_pickle=False)
    meta = json.loads(str(z["meta"]))
    H, V, EP, FR = z["H"], z["V"], z["EP"], z["FR"]
    index: dict[int, list[int]] = defaultdict(list)
    for pos in np.flatnonzero(V):
        index[int(H[pos])].append(int(pos))
    index = {h: p for h, p in index.items() if len(p) <= MAX_POSTINGS}
    return meta, H, V, EP, FR, index


# ── match ──────────────────────────────────────────────────────────────────
def match_audio(audio: np.ndarray, H, V, EP, FR, index) -> list[dict]:
    """쇼츠 오디오 → 원본 조각. **위상을 훑어** 가장 잘 맞는 것을 쓴다."""
    best: list[dict] = []
    best_score = -1.0
    for k in range(QUERY_PHASES):
        off = k * (HOP // QUERY_PHASES)
        qh, qv = fingerprint(audio[off:])
        if len(qh) == 0:
            continue
        segs = match_one(qh, qv, H, V, EP, FR, index)
        # 점수 = 덮은 시간(길수록 좋다), 동점이면 BER 낮은 쪽
        cov = sum(s["short_end"] - s["short_start"] for s in segs)
        score = cov - 0.001 * sum(s["ber"] for s in segs)
        if score > best_score:
            best_score, best = score, [
                {**s, "short_start": round(s["short_start"] + off / SR, 2),
                 "short_end": round(s["short_end"] + off / SR, 2),
                 "phase_ms": round(off / SR * 1000, 1)} for s in segs]
    return best


def match_one(qh, qv, H, V, EP, FR, index) -> list[dict]:
    """한 위상의 지문열 → 원본 조각 목록. 회차별로 표를 세고 offset 을 검증한다."""
    votes: Counter = Counter()
    for i in np.flatnonzero(qv):
        for pos in index.get(int(qh[i]), ()):
            votes[(int(EP[pos]), int(FR[pos]) - int(i))] += 1
    if not votes:
        return []
    merged: Counter = Counter()
    for (ep, d), v in votes.items():                 # ±1 프레임 지터 흡수
        merged[(ep, d)] = v + votes.get((ep, d - 1), 0) + votes.get((ep, d + 1), 0)

    cands, seen = [], set()
    for (ep, d), v in merged.most_common():
        if v < VOTE_MIN:
            break
        if any(e == ep and abs(d - s) <= 2 for e, s in seen):
            continue
        seen.add((ep, d))
        cands.append((ep, d, v))
        if len(cands) >= 24:
            break

    ep_start = {}                                    # 회차별 라이브러리 시작 위치
    for e in np.unique(EP):
        ep_start[int(e)] = int(np.flatnonzero(EP == e)[0])

    segs = []
    for ep, d, votes_n in cands:
        base = ep_start[ep]
        ep_len = int((EP == ep).sum())
        i0, i1 = max(0, -d), min(len(qh), ep_len - d)
        if i1 - i0 < int(MIN_SEG_SEC * FPS):
            continue
        ii = np.arange(i0, i1)
        src_pos = base + ii + d
        dist = hamming(qh[ii], H[src_pos]).astype(np.float64)
        ok = (dist <= FRAME_BER * 32) & qv[ii] & V[src_pos]
        run = None
        for k, flag in enumerate(ok):
            if flag:
                if run is None:
                    run = [k, k]
                elif k - run[1] <= GAP_FRAMES:
                    run[1] = k
                else:
                    segs.append((ep, d, i0 + run[0], i0 + run[1], dist, i0, votes_n))
                    run = [k, k]
            # 끊긴 구간은 run 을 유지한 채 GAP_FRAMES 로만 잇는다
        if run:
            segs.append((ep, d, i0 + run[0], i0 + run[1], dist, i0, votes_n))

    out = []
    for ep, d, s, e, dist, base_i, votes_n in segs:
        if e - s < int(MIN_SEG_SEC * FPS):
            continue
        ber = float(dist[s - base_i:e - base_i + 1].mean()) / 32.0
        if ber > MAX_BER:
            continue
        out.append({"episode_idx": ep,
                    "short_start": round(s / FPS, 2), "short_end": round(e / FPS, 2),
                    "source_start": round((s + d) / FPS, 2),
                    "source_end": round((e + d) / FPS, 2),
                    "ber": round(ber, 3), "votes": votes_n})
    # 쇼츠 시간축에서 겹치면 BER 낮은 것 우선
    out.sort(key=lambda x: x["ber"])
    kept: list[dict] = []
    for s in out:
        if all(s["short_end"] <= k["short_start"] or s["short_start"] >= k["short_end"]
               for k in kept):
            kept.append(s)
    kept.sort(key=lambda x: x["short_start"])
    return kept


def cmd_match(args: argparse.Namespace) -> int:
    meta, H, V, EP, FR, index = load_lib(Path(args.lib))
    names = {e["idx"]: e["file"] for e in meta["episodes"]}
    targets: list[Path] = []
    if args.video:
        targets = [Path(args.video)]
    if args.dir:
        targets += sorted((Path(args.dir) / "video").glob("*.mp4"))
    if not targets:
        raise SystemExit("--video 또는 --dir 을 줘라")

    outdir = Path(args.out) if args.out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    found = miss = 0
    for t in targets:
        audio = decode(t)
        segs = match_audio(audio, H, V, EP, FR, index)
        cov = sum(s["short_end"] - s["short_start"] for s in segs)
        dur = round(len(audio) / SR, 2)
        if segs:
            eps = sorted({s["episode_idx"] for s in segs})
            span = (min(s["source_start"] for s in segs),
                    max(s["source_end"] for s in segs))
            print(f"  ✓ {t.stem:16} 조각 {len(segs):2} · 덮음 {cov:5.1f}/{dur:.1f}s "
                  f"({cov/max(dur,0.1)*100:3.0f}%) · 회차 {[names[e] for e in eps]} · "
                  f"원본 {span[0]:.0f}~{span[1]:.0f}s")
            found += 1
        else:
            print(f"  – {t.stem:16} 원본에 없음(다른 회차이거나 소리를 교체) · {dur:.1f}s")
            miss += 1
        if outdir:
            (outdir / f"{t.stem}.json").write_text(json.dumps(
                {"shorts_id": t.stem, "duration_sec": dur, "library": meta["name"],
                 "episodes": names, "segments": segs,
                 "coverage_sec": round(cov, 2)}, ensure_ascii=False, indent=1),
                encoding="utf-8")
    print(f"\n[match] 찾음 {found} · 못 찾음 {miss}"
          + (f" → {outdir}" if outdir else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fp_library", description="오디오 지문 도서관")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="원본 회차 → 지문 도서관")
    b.add_argument("--name", required=True)
    b.add_argument("--src", required=True, help="원본 영상이 든 디렉토리")
    b.add_argument("--out", default="work/fp")
    b.set_defaults(fn=cmd_build)
    m = sub.add_parser("match", help="쇼츠 → 원본 위치")
    m.add_argument("--lib", required=True)
    m.add_argument("--video")
    m.add_argument("--dir", help="해부기 작업 디렉토리(video/*.mp4 를 전부)")
    m.add_argument("--out", help="결과 JSON 을 쓸 디렉토리")
    m.set_defaults(fn=cmd_match)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
