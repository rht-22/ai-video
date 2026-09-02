#!/usr/bin/env python3
"""크기별 채점 — 위치 정렬(8화면x6줄) 기준 + 전역 집합 교차 확인."""
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
truth = json.load(open(os.path.join(HERE, "truth.json")))
res = json.load(open(os.path.join(HERE, "results.json")))
HEIGHTS = [10, 14, 18, 24, 32, 44]

T = {(f["frame"], l["row"]): l["code"] for f in truth for l in f["lines"]}
ALL_TRUE = set(T.values())


def charmatch(a, b):
    if len(a) != len(b):
        return sum(1 for x, y in zip(a, b) if x == y)
    return sum(1 for x, y in zip(a, b) if x == y)


agg = defaultdict(lambda: defaultdict(int))   # (label,h) -> counter
chars = defaultdict(lambda: [0, 0])           # (label,h) -> [correct, total]
halluc = defaultdict(int)
per_rep = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

for r in res:
    label = r["label"]
    obj = json.loads(r["raw"])
    screens = obj["screens"]
    got_flat = [c for s in screens for c in s]
    for fi, s in enumerate(screens):
        for ri, got in enumerate(s):
            h = HEIGHTS[ri]
            exp = T[(fi, ri)]
            got = str(got).strip().upper()
            n = charmatch(exp, got)
            chars[(label, h)][0] += n
            chars[(label, h)][1] += len(exp)
            if got == exp:
                k = "exact"
            elif n >= 4:
                k = "near"      # 7자 중 4자 이상 맞음 = 읽으려다 틀림
            else:
                k = "wrong"
            agg[(label, h)][k] += 1
            per_rep[label][r["rep"]][k] += 1
    halluc[label] += sum(1 for c in got_flat if c.upper() not in ALL_TRUE)

print("== 위치 정렬 채점 (조건별 8화면 x 3회 = 크기당 24칸) ==")
print(f"{'글자높이':>8} | {'미지정 정확':>10} {'근접':>5} {'오답':>5} {'문자정확도':>9} | "
      f"{'HIGH 정확':>9} {'근접':>5} {'오답':>5} {'문자정확도':>9}")
rows = []
for h in HEIGHTS:
    line = [h]
    cells = []
    for label in ("unspecified", "HIGH"):
        a = agg[(label, h)]
        tot = a["exact"] + a["near"] + a["wrong"]
        cc, ct = chars[(label, h)]
        cells.append((a["exact"], tot, a["near"], a["wrong"], cc / ct * 100))
    u, hg = cells
    print(f"{h:>6}px | {u[0]:>4}/{u[1]:<3} {u[0]/u[1]*100:>4.0f}% {u[2]:>5} {u[3]:>5} {u[4]:>8.1f}% | "
          f"{hg[0]:>3}/{hg[1]:<3} {hg[0]/hg[1]*100:>4.0f}% {hg[2]:>5} {hg[3]:>5} {hg[4]:>8.1f}%")
    rows.append((h, u, hg))

print("\n== 회차별 정확 일치 (48칸 중) ==")
for label in ("unspecified", "HIGH"):
    per = [per_rep[label][i]["exact"] for i in sorted(per_rep[label])]
    print(f"  {label:>12}: {per}  (총 {sum(per)}/{48*len(per)})")

print("\n== 전역 집합 교차 확인 (순서 무시, 정답 48개 중 응답 어디엔가 있는가) ==")
for label in ("unspecified", "HIGH"):
    for r in res:
        if r["label"] != label:
            continue
        got = {c.upper() for c in r["codes"]}
        hit = len(ALL_TRUE & got)
        print(f"  {label:>12} rep{r['rep']}: {hit}/48  환각(정답에 없는 코드) {len(got - ALL_TRUE)}")

print("\n== 토큰 ==")
for label in ("unspecified", "HIGH"):
    r = [x for x in res if x["label"] == label][0]
    d = {p["modality"]: p["tokenCount"] for p in r["usage"]["promptTokensDetails"]}
    print(f"  {label:>12}: prompt={r['usage']['promptTokenCount']} "
          f"VIDEO={d.get('VIDEO')} TEXT={d.get('TEXT')}")
