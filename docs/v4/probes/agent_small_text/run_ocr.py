#!/usr/bin/env python3
"""미지정(=LOW) vs HIGH — 글자 크기별 코드 회수율 실측."""
import json
import os
import re
import sys

import gem

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(HERE, "ocr16.mp4")
REPEATS = int(os.environ.get("REPEATS", "3"))

PROMPT = """이 영상의 각 화면에는 "AB-1234" 형태의 코드(대문자 2개 + 하이픈 + 숫자 4자리)가
세로로 여러 줄 적혀 있다. 화면은 몇 초마다 바뀌고, 바뀔 때마다 코드도 전부 새로 바뀐다.

화면에 실제로 보이는 코드를 전부 읽어라.
- 화면이 나오는 순서대로, 각 화면 안에서는 위에서 아래로.
- 같은 화면이 여러 프레임에 걸쳐 나오면 그 화면은 한 번만 적어라.
- 글자가 작아서 못 읽은 줄은 그냥 빼라. 절대 지어내지 마라. 추측해서 채우지 마라.
- 일부만 읽힌 코드도 읽힌 그대로 적어라(예: "XM-92??" 처럼 쓰지 말고 확신하는 글자만).

JSON 으로만 답하라:
{"screens": [["AB-1234", "CD-5678"], ["EF-9012"]]}"""


def flat(resp_text):
    """응답에서 코드 토큰만 평평하게 뽑는다."""
    try:
        obj = json.loads(resp_text)
        codes = []
        if isinstance(obj, dict) and "screens" in obj:
            for s in obj["screens"]:
                if isinstance(s, list):
                    codes.extend([str(x) for x in s])
                else:
                    codes.append(str(s))
            return codes
    except Exception:
        pass
    # 폴백: 정규식
    return re.findall(r"[A-Z]{2}-\d{4}", resp_text)


def run(label, media_resolution, uri, mime):
    rows = []
    for i in range(REPEATS):
        r = gem.generate(uri, mime, PROMPT, fps=1,
                         media_resolution=media_resolution)
        txt = gem.text_of(r)
        um = r.get("usageMetadata", {})
        codes = flat(txt)
        rows.append({
            "rep": i,
            "label": label,
            "media_resolution": media_resolution,
            "codes": codes,
            "raw": txt,
            "usage": um,
            "finish": [c.get("finishReason") for c in r.get("candidates", [])],
        })
        print(f"  [{label} rep{i}] codes={len(codes)} "
              f"prompt_tok={um.get('promptTokenCount')} "
              f"details={um.get('promptTokensDetails')} "
              f"finish={rows[-1]['finish']}")
    return rows


def main():
    print(f"[upload] {VIDEO}")
    name, uri, mime = gem.upload(VIDEO, "ocr_size_probe")
    print(f"  -> {name} {uri}")
    out = []
    try:
        out += run("unspecified", None, uri, mime)
        out += run("HIGH", "MEDIA_RESOLUTION_HIGH", uri, mime)
    finally:
        print(f"[delete] {name}: {gem.delete(name)}")
    with open(os.path.join(HERE, "results.json"), "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print("[ok] results.json")


if __name__ == "__main__":
    main()
