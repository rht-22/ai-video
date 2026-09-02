#!/usr/bin/env python3
"""라운드 채점: python3 score_any.py <dir> [환산배율]"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
d = os.path.join(HERE, sys.argv[1])
scale = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
truth = json.load(open(os.path.join(d, "truth.json")))
res = json.load(open(os.path.join(d, "results.json")))

HEIGHTS = [l["height_px"] for l in truth[0]["lines"]]
T = {(f["frame"], l["row"]): l["code"] for f in truth for l in f["lines"]}

agg = defaultdict(lambda: defaultdict(int))
chars = defaultdict(lambda: [0, 0])

for r in res:
    label = r["label"]
    for fi, s in enumerate(r["screens"]):
        for ri in range(len(HEIGHTS)):
            h = HEIGHTS[ri]
            exp = T[(fi, ri)]
            got = (s[ri] if ri < len(s) else "").strip().upper()
            if not got:
                agg[(label, h)]["missing"] += 1
                chars[(label, h)][1] += len(exp)
                continue
            n = sum(1 for x, y in zip(exp, got) if x == y)
            chars[(label, h)][0] += n
            chars[(label, h)][1] += len(exp)
            if got == exp:
                agg[(label, h)]["exact"] += 1
            elif n >= 4:
                agg[(label, h)]["near"] += 1
            else:
                agg[(label, h)]["wrong"] += 1

N = len(truth) * (len(res) // 2)
print(f"\n### {sys.argv[1]}  (크기당 {N}칸 = 8화면 x 3회)"
      + (f"  · 480p 환산 배율 {scale}" if scale != 1 else ""))
hdr = f"{'글자높이':>9} |" + "".join(
    f" {lab:^33}|" for lab in ("미지정(=LOW)", "HIGH"))
print(hdr)
print(f"{'':>9} |" + " 정확  근접  오답  누락  문자정확 |" * 2)
for h in HEIGHTS:
    eff = h * scale
    lbl = f"{h}px" if scale == 1 else f"{h}→{eff:.1f}"
    line = f"{lbl:>9} |"
    for label in ("unspecified", "HIGH"):
        a = agg[(label, h)]
        cc, ct = chars[(label, h)]
        line += (f" {a['exact']:>2}/{N} {a['near']:>4} {a['wrong']:>5} {a['missing']:>5}"
                 f" {cc/ct*100:>7.1f}% |")
    print(line)

print("\n  회차별 정확 일치(48칸):", end=" ")
for label in ("unspecified", "HIGH"):
    per = []
    for r in res:
        if r["label"] != label:
            continue
        c = sum(1 for fi, s in enumerate(r["screens"])
                for ri in range(len(HEIGHTS))
                if (s[ri] if ri < len(s) else "").strip().upper() == T[(fi, ri)])
        per.append(c)
    print(f"{label}={per}", end="  ")
print()
