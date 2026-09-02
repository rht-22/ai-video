#!/usr/bin/env python3
"""usage: run2.py <srcTag> <fps,csv> <outTag> [mode=list|count]"""
import json
import os
import sys

import run_fps_ceiling as R

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    src, fps_csv, out_tag = sys.argv[1], sys.argv[2], sys.argv[3]
    mode = sys.argv[4] if len(sys.argv) > 4 else "list"
    truth = json.load(open(os.path.join(BASE, f"{src}_truth.json")))
    tn = truth["numbers"]
    digits = truth.get("digits", 3)
    # digit-aware parsing + prompt (default helpers are hardcoded to 3)
    import re as _re
    R.parse_numbers = lambda t, _d=digits: _re.findall(r"\b(\d{%d})\b" % _d, t)
    # example values must NOT be real truth values, or they leak free hits
    tset = set(tn)
    ex = []
    cand = 10 ** (digits - 1)
    while len(ex) < 3:
        s = f"{cand:0{digits}d}"
        if s not in tset:
            ex.append(s)
        cand += 1
    assert not (set(ex) & tset), "prompt example leaks a truth value"
    R.PROMPT_LIST = (R.PROMPT_LIST.replace("3-digit", f"{digits}-digit")
                     .replace('["350","241","831"]', "[" + ",".join(f'"{x}"' for x in ex) + "]"))
    R.PROMPT_COUNT = R.PROMPT_COUNT.replace("3-digit", f"{digits}-digit")
    print("prompt examples (non-truth):", ex, flush=True)
    dur = truth["duration_sec"]
    video = os.path.join(BASE, f"{src}.mp4")
    fps_list = [float(x) for x in fps_csv.split(",")]

    name, uri, info = R.upload(video)
    print(f"[{src}] uploaded {name} sizeBytes={info.get('sizeBytes')} "
          f"file_fps={truth['file_fps']} slots={truth['count']} slot_ms={truth['slot_ms']}", flush=True)
    results = []
    try:
        for fps in fps_list:
            prompt = R.PROMPT_LIST if mode == "list" else R.PROMPT_COUNT
            st, resp, dt = R.call(uri, fps, prompt, 32768 if mode == "list" else 4096)
            if st != 200:
                print(f"fps={fps} HTTP {st} {json.dumps(resp)[:300]}", flush=True)
                results.append({"fps": fps, "http": st, "error": json.dumps(resp)[:400]})
                continue
            um = resp.get("usageMetadata", {})
            cand = resp.get("candidates", [{}])[0]
            fin = cand.get("finishReason")
            text = R.extract_text(resp)
            pred = R.parse_numbers(text)
            sampled = int(round(dur * fps))
            ceiling = min(sampled, truth["count"])
            sc = R.score(pred, tn) if mode == "list" else {}
            row = {
                "src": src, "fps": fps, "mode": mode,
                "elapsed_sec": round(dt, 3),
                "finishReason": fin, "truncated": fin == "MAX_TOKENS",
                "promptTokenCount": um.get("promptTokenCount"),
                "promptTokensDetails": um.get("promptTokensDetails"),
                "candidatesTokenCount": um.get("candidatesTokenCount"),
                "thoughtsTokenCount": um.get("thoughtsTokenCount"),
                "sampled_frames_expected": sampled,
                "theoretical_ceiling": ceiling,
            }
            row.update(sc)
            if mode == "list":
                row["recall_vs_ceiling"] = round(sc["hits"] / ceiling, 4) if ceiling else None
                row["recall_vs_all"] = round(sc["hits"] / truth["count"], 4)
            else:
                row["raw_text"] = text.strip()[:200]
            results.append(row)
            print(json.dumps({k: v for k, v in row.items()
                              if k not in ("matched_indices_first10", "matched_indices_last10")},
                             ensure_ascii=False), flush=True)
            open(os.path.join(BASE, f"raw_{out_tag}_fps{fps}.txt"), "w").write(text)
    finally:
        print("delete:", R.delete(name), flush=True)
    json.dump({"src": src, "truth_count": truth["count"], "duration_sec": dur,
               "file_fps": truth["file_fps"], "slot_ms": truth["slot_ms"], "results": results},
              open(os.path.join(BASE, f"results_{out_tag}.json"), "w"), indent=1)
    print("wrote", f"results_{out_tag}.json")


if __name__ == "__main__":
    main()
