#!/usr/bin/env python3
"""실소재 교차 확인: 실제 480p 프록시의 번인 텔롭을 두 해상도로 받아쓰게 한다."""
import json
import os

import gem

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(HERE, "real", "clip480.mp4")
REPEATS = int(os.environ.get("REPEATS", "2"))

PROMPT = """이 영상에는 방송 자막·텔롭(한국어 글자)이 화면에 구워져 있다.

화면에 보이는 글자를 전부, 나오는 순서대로 그대로 옮겨 적어라.
- 대사 자막, 큰 예능 텔롭, 구석의 작은 태그, 로고까지 전부.
- 같은 글자가 여러 프레임에 걸쳐 계속 보이면 한 번만 적어라.
- 한 글자도 바꾸지 말고 화면에 있는 그대로. 맞춤법을 고치지 마라.
- 못 읽은 글자는 빼라. 절대 지어내지 마라.

JSON 으로만 답하라:
{"texts": [{"t": 대략_초, "text": "화면 글자", "kind": "대사자막|텔롭|태그|로고"}]}"""


def main():
    print(f"[upload] {VIDEO}")
    name, uri, mime = gem.upload(VIDEO, "ocr_real_dokkaebi")
    print(f"  -> {name}")
    out = []
    try:
        for label, mr in (("unspecified", None), ("HIGH", "MEDIA_RESOLUTION_HIGH")):
            for i in range(REPEATS):
                r = gem.generate(uri, mime, PROMPT, fps=1, media_resolution=mr,
                                 max_output_tokens=16384)
                txt = gem.text_of(r)
                um = r.get("usageMetadata", {})
                try:
                    items = json.loads(txt)["texts"]
                except Exception:
                    items = None
                out.append({"label": label, "rep": i, "raw": txt, "usage": um,
                            "items": items,
                            "finish": [c.get("finishReason") for c in r.get("candidates", [])]})
                vt = [p["tokenCount"] for p in um.get("promptTokensDetails", [])
                      if p["modality"] == "VIDEO"]
                print(f"  [{label} rep{i}] items={len(items) if items else 'PARSE_FAIL'} "
                      f"vid_tok={vt} prompt={um.get('promptTokenCount')} "
                      f"finish={out[-1]['finish']}")
    finally:
        print(f"[delete] {name}: {gem.delete(name)}")
    json.dump(out, open(os.path.join(HERE, "real", "results.json"), "w"),
              ensure_ascii=False, indent=1)
    print("[ok] real/results.json")


if __name__ == "__main__":
    main()
