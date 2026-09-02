#!/usr/bin/env python3
"""임의 라운드 실행: python3 run_any.py <dir>"""
import json
import os
import re
import sys

import gem

HERE = os.path.dirname(os.path.abspath(__file__))
REPEATS = int(os.environ.get("REPEATS", "3"))

PROMPT = """이 영상의 각 화면에는 "AB-1234" 형태의 코드(대문자 2개 + 하이픈 + 숫자 4자리)가
세로로 6줄 적혀 있다. 위쪽 줄일수록 글자가 작다. 화면은 2초마다 바뀌고, 바뀔 때마다
코드도 전부 새로 바뀐다. 화면은 총 8개다.

화면에 실제로 보이는 코드를 전부 읽어라.
- 화면이 나오는 순서대로, 각 화면 안에서는 위에서 아래로.
- 같은 화면이 여러 프레임에 걸쳐 나오면 그 화면은 한 번만 적어라.
- 글자가 너무 작아서 못 읽은 줄은 빈 문자열 ""로 두어라. 절대 지어내지 마라.
  추측해서 채우지 마라. 확신이 없으면 "" 로 두는 편이 낫다.
- 각 화면은 반드시 6개 항목(못 읽은 줄은 "")으로 답하라 — 줄 순서가 중요하다.

JSON 으로만 답하라:
{"screens": [["AB-1234", "", "CD-5678", "", "", "EF-9012"], ...]}"""


def flat(t):
    try:
        o = json.loads(t)
        if isinstance(o, dict) and "screens" in o:
            return [[str(x) for x in s] for s in o["screens"]]
    except Exception:
        pass
    return None


def main():
    d = os.path.join(HERE, sys.argv[1])
    video = os.path.join(d, "clip.mp4")
    print(f"[upload] {video}")
    name, uri, mime = gem.upload(video, f"ocr_{sys.argv[1]}")
    print(f"  -> {name}")
    out = []
    try:
        for label, mr in (("unspecified", None), ("HIGH", "MEDIA_RESOLUTION_HIGH")):
            for i in range(REPEATS):
                r = gem.generate(uri, mime, PROMPT, fps=1, media_resolution=mr)
                txt = gem.text_of(r)
                um = r.get("usageMetadata", {})
                sc = flat(txt)
                out.append({"rep": i, "label": label, "raw": txt, "usage": um,
                            "screens": sc,
                            "finish": [c.get("finishReason") for c in r.get("candidates", [])]})
                print(f"  [{label} rep{i}] screens={len(sc) if sc else 'PARSE_FAIL'} "
                      f"vid_tok={[p['tokenCount'] for p in um.get('promptTokensDetails',[]) if p['modality']=='VIDEO']} "
                      f"finish={out[-1]['finish']}")
    finally:
        print(f"[delete] {name}: {gem.delete(name)}")
    json.dump(out, open(os.path.join(d, "results.json"), "w"),
              ensure_ascii=False, indent=1)
    print(f"[ok] {d}/results.json")


if __name__ == "__main__":
    main()
