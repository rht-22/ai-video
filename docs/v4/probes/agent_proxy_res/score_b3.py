"""실소재 배터리 채점 — 1080p 프레임에서 사람이 확인한 항목만 정답지로 쓴다.

내가 눈으로 확인하지 못한 화면 글자는 채점에 넣지 않는다(누구에게도 감점 없음).
크기 등급은 1080 폭 프레임 기준 글자 높이의 대략치다.
"""
import json
import os
import re
from collections import defaultdict

SP = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(SP, "work_b3")

GT = [
    ("대(50~70px)", "가왕돌의 센터 전쟁"),
    ("대(50~70px)", "표심 스틸 대작전"),
    ("대(50~70px)", "센터 전쟁은 계속 된다"),
    ("대(50~70px)", "경쟁 하는 센터 후보들"),
    ("중(30~45px)", "잘 전달 부탁해요"),
    ("중(30~45px)", "오늘 1등은 나야"),
    ("중(30~45px)", "센터가 됩니다"),
    ("중(30~45px)", "여기서 센터 줘요"),
    ("중(30~45px)", "아니 이렇게 싸우는"),
    ("소(20~30px)", "크리에이터"),
    ("소(20~30px)", "노윤"),
    ("소(20~30px)", "총괄 프로듀서"),
    ("소(20~30px)", "전수경"),
    ("소(20~30px)", "숨 쉴 틈도 없이"),
    ("소(20~30px)", "촬영"),
    ("소(20~30px)", "가왕쇼"),
]
CREDITS = ["이기주", "이정웅", "송형석", "한제호", "김세권", "김수연",
           "박민수", "전재웅", "한석하", "문봉기", "전미주", "유태환",
           "장태희", "이한별"]
ORDER = ["대(50~70px)", "중(30~45px)", "소(20~30px)", "제작진이름(가장 작음)"]


def norm(s):
    return re.sub(r"[\s.,!?~♥★:()\[\]'\"·|-]+", "", s)


def main():
    res = json.load(open(os.path.join(WORK, "results.json")))
    rows = defaultdict(lambda: defaultdict(list))
    for r in res:
        if not r.get("ok"):
            continue
        proxy, mr, _ = r["tag"].split("|")
        body = norm(r["text"])
        hit = defaultdict(int)
        tot = defaultdict(int)
        for cls, item in GT:
            tot[cls] += 1
            if norm(item) in body:
                hit[cls] += 1
        cls = "제작진이름(가장 작음)"
        for n in CREDITS:
            tot[cls] += 1
            if n in body:
                hit[cls] += 1
        for c in ORDER:
            rows[f"{proxy}|{mr}"][c].append((hit[c], tot[c]))

    conds = [f"{p}|{m}" for m in ("default", "HIGH")
             for p in ("480p", "720p", "1080p")]
    conds = [c for c in conds if c in rows]
    print("=" * 86)
    print("실소재(가왕쇼 46~58s) 번인 글자 회수율 % — 1080p 프레임 육안 확인 항목만")
    print("=" * 86)
    print("조건".ljust(18) + "".join(c.rjust(21) for c in ORDER))
    print("-" * 86)
    for c in conds:
        line = c.ljust(18)
        for cl in ORDER:
            hs = rows[c][cl]
            h = sum(x[0] for x in hs)
            t = sum(x[1] for x in hs)
            line += f"{100*h/t:>17.0f} ({h}/{t})".rjust(21)
        print(line)
    print()
    for c in conds:
        cl = "제작진이름(가장 작음)"
        hs = rows[c][cl]
        print(f"  {c.ljust(18)} 제작진 이름 반복별: "
              f"{[f'{h}/{t}' for h, t in hs]}")


if __name__ == "__main__":
    main()
