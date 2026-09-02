"""채점 — 크기별 정확 회수 / 부분일치(읽으려다 틀림) / 환각.

정답 코드는 전역 유일하므로 반환 코드의 신원만으로 (장면,크기) 를 역추적한다.
반환 코드마다 가장 가까운 정답을 찾아:
  거리 0            → exact   (그 셀 회수)
  거리 <= 2         → partial (그 셀을 '보긴 봤다')
  그 외             → 환각
한 정답 셀에는 최대 하나의 반환 코드만 귀속시킨다(가까운 것 우선).
"""
import json
import os
import sys
from collections import defaultdict

SP = os.path.dirname(os.path.abspath(__file__))
TAG = os.environ.get("TAG", "b1")
WORK = os.path.join(SP, f"work_{TAG}" if TAG != "b1" else "work")
sys.path.insert(0, SP)
from run_probe import extract  # noqa: E402

PARTIAL_MAX = 2


def lev(a, b):
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def score_one(codes, truth):
    """반환 코드 목록 → 셀별 상태 + 환각 수."""
    by_code = {t["code"]: t for t in truth}
    # (거리, 반환코드, 정답코드) 를 모아 가까운 것부터 귀속
    cand = []
    for c in codes:
        for tc in by_code:
            d = lev(c, tc)
            if d <= PARTIAL_MAX:
                cand.append((d, c, tc))
    cand.sort(key=lambda x: x[0])
    used_ret, used_truth, assign = set(), set(), {}
    for d, c, tc in cand:
        if c in used_ret or tc in used_truth:
            continue
        used_ret.add(c)
        used_truth.add(tc)
        assign[tc] = (c, d)
    cells = {}
    for t in truth:
        a = assign.get(t["code"])
        if a is None:
            cells[t["code"]] = "missed"
        elif a[1] == 0:
            cells[t["code"]] = "exact"
        else:
            cells[t["code"]] = "partial"
    halluc = [c for c in codes if c not in used_ret]
    return cells, halluc


def main():
    T = json.load(open(os.path.join(WORK, "truth.json")))
    truth, heights = T["truth"], T["heights"]
    by_code = {t["code"]: t for t in truth}
    results = json.load(open(os.path.join(WORK, "results.json")))

    # 조건 -> 크기 -> 카운터
    agg = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    hall = defaultdict(list)
    reps = defaultdict(int)
    per_rep = defaultdict(list)

    for r in results:
        if not r.get("ok"):
            continue
        proxy, mr, rep = r["tag"].split("|")
        cond = f"{proxy}|{mr}"
        codes = extract(r["text"])
        cells, halluc = score_one(codes, truth)
        reps[cond] += 1
        hall[cond].append(len(halluc))
        ex = 0
        for code, st in cells.items():
            h = by_code[code]["height"]
            agg[cond][h][st] += 1
            if st == "exact":
                ex += 1
        per_rep[cond].append(ex)

    conds = [f"{p}|{m}" for m in ("default", "HIGH")
             for p in ("480p", "720p", "1080p")]
    conds = [c for c in conds if c in agg]

    print("=" * 78)
    print("크기별 정확 회수율 (%)  — 분모 = 8장면 x 반복수")
    print("=" * 78)
    hdr = "조건".ljust(18) + "".join(f"{h:>7d}px" for h in heights) + "   전체"
    print(hdr)
    print("-" * 78)
    for c in conds:
        n = reps[c] * T["n_scenes"]
        row = c.ljust(18)
        tot = 0
        for h in heights:
            e = agg[c][h]["exact"]
            tot += e
            row += f"{100*e/n:>8.0f}"
        row += f"{100*tot/(n*len(heights)):>8.1f}"
        print(row)

    print()
    print("=" * 78)
    print("크기별 부분일치(읽으려다 틀림, 편집거리 1~2) 회수율 (%)")
    print("=" * 78)
    print(hdr)
    print("-" * 78)
    for c in conds:
        n = reps[c] * T["n_scenes"]
        row = c.ljust(18)
        tot = 0
        for h in heights:
            p = agg[c][h]["partial"]
            tot += p
            row += f"{100*p/n:>8.0f}"
        row += f"{100*tot/(n*len(heights)):>8.1f}"
        print(row)

    print()
    print("=" * 78)
    print("환각(정답에 없는 코드) 평균 개수 / 회  ·  반복별 정확 개수(/48)")
    print("=" * 78)
    for c in conds:
        print(f"{c.ljust(18)} 환각 {sum(hall[c])/len(hall[c]):5.1f}   "
              f"정확 {per_rep[c]}  (평균 {sum(per_rep[c])/len(per_rep[c]):.1f})")

    # 480p -> 720p 이득
    print()
    print("=" * 78)
    print("480p → 720p 정확 회수율 변화 (%p)")
    print("=" * 78)
    print("media_res".ljust(18) + "".join(f"{h:>7d}px" for h in heights) + "   전체")
    print("-" * 78)
    for mr in ("default", "HIGH"):
        a, b = f"480p|{mr}", f"720p|{mr}"
        if a not in agg or b not in agg:
            continue
        na, nb = reps[a] * T["n_scenes"], reps[b] * T["n_scenes"]
        row, ta, tb = mr.ljust(18), 0, 0
        for h in heights:
            ea, eb = agg[a][h]["exact"], agg[b][h]["exact"]
            ta += ea
            tb += eb
            row += f"{100*eb/nb - 100*ea/na:>+8.0f}"
        row += f"{100*tb/(nb*len(heights)) - 100*ta/(na*len(heights)):>+8.1f}"
        print(row)
    print()
    print("720p → 1080p 정확 회수율 변화 (%p)")
    print("-" * 78)
    for mr in ("default", "HIGH"):
        a, b = f"720p|{mr}", f"1080p|{mr}"
        if a not in agg or b not in agg:
            continue
        na, nb = reps[a] * T["n_scenes"], reps[b] * T["n_scenes"]
        row, ta, tb = mr.ljust(18), 0, 0
        for h in heights:
            ea, eb = agg[a][h]["exact"], agg[b][h]["exact"]
            ta += ea
            tb += eb
            row += f"{100*eb/nb - 100*ea/na:>+8.0f}"
        row += f"{100*tb/(nb*len(heights)) - 100*ta/(na*len(heights)):>+8.1f}"
        print(row)

    # 토큰
    print()
    print("=" * 78)
    print("입력 토큰 (usageMetadata.promptTokenCount)")
    print("=" * 78)
    tk = defaultdict(set)
    lat = defaultdict(list)
    for r in results:
        if r.get("ok"):
            proxy, mr, _ = r["tag"].split("|")
            tk[f"{proxy}|{mr}"].add(r["usage"].get("promptTokenCount"))
            lat[f"{proxy}|{mr}"].append(r["latency_sec"])
    for c in conds:
        ls = sorted(lat[c])
        print(f"{c.ljust(18)} prompt={sorted(tk[c])}  "
              f"응답지연 중앙 {ls[len(ls)//2]:.1f}s")

    json.dump({c: {str(h): dict(agg[c][h]) for h in heights} for c in conds},
              open(os.path.join(WORK, "score.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
