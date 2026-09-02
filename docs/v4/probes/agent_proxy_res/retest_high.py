"""HIGH 조건 재검증 — 480p vs 720p vs 1080p 의 작은 글자(8·10px) 회수를
반복 6회로 다시 잰다. 1회짜리 확인 실행이 앞선 3반복과 어긋나서 표본을 늘린다.
"""
import json
import os
import sys
from collections import defaultdict

SP = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("TAG", "b2")
sys.path.insert(0, SP)
from run_probe import call, extract, WORK  # noqa: E402
from score import score_one  # noqa: E402

REPS = int(os.environ.get("REPS", "6"))
# MR="" 면 미지정(=LOW=MEDIUM), 아니면 MEDIA_RESOLUTION_*
_MR = os.environ.get("MR", "MEDIA_RESOLUTION_HIGH") or None
_MRLABEL = os.environ.get("MR", "HIGH").replace("MEDIA_RESOLUTION_", "") or "default"


def main():
    ups = json.load(open(os.path.join(WORK, "uploads.json")))
    T = json.load(open(os.path.join(WORK, "truth.json")))
    by = {t["code"]: t for t in T["truth"]}
    heights = T["heights"]
    allr = []
    agg = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for px in ("480p", "720p", "1080p"):
        for r in range(REPS):
            res = call(ups[px]["uri"], _MR, f"{px}|{_MRLABEL}|x{r}")
            if not res.get("ok"):
                print(px, r, "FAIL", str(res)[:200], flush=True)
                continue
            codes = extract(res["text"])
            cells, hal = score_one(codes, T["truth"])
            for c, st in cells.items():
                agg[px][by[c]["height"]][st] += 1
            ex = sum(1 for v in cells.values() if v == "exact")
            sm = sum(1 for c, v in cells.items()
                     if v == "exact" and by[c]["height"] <= 10)
            print(f"{px:6s} r{r} 정확 {ex:2d}/48  8~10px {sm:2d}/16  "
                  f"환각 {len(hal):2d}  반환 {len(codes)}", flush=True)
            allr.append({"tag": res["tag"], "exact": ex, "small": sm,
                         "halluc": len(hal), "returned": len(codes),
                         "text": res["text"]})
    print("\n" + "=" * 70)
    print(f"{_MRLABEL} 조건 크기별 정확 회수율 % (반복 {REPS}회 · 분모 8x{REPS}={8*REPS})")
    print("=" * 70)
    print("프록시".ljust(10) + "".join(f"{h:>8d}px" for h in heights))
    print("-" * 70)
    for px in ("480p", "720p", "1080p"):
        n = REPS * T["n_scenes"]
        print(px.ljust(10) + "".join(
            f"{100*agg[px][h]['exact']/n:>10.0f}" for h in heights))
    json.dump(allr, open(os.path.join(WORK, f"retest_{_MRLABEL}.json"), "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
