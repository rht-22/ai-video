"""비용 실측 — 실제 방송 롱폼 10분 구간으로 프록시 3종의
파일 크기 / 인코딩 시간 / Files API 업로드 시간을 잰다.

합성 회색 영상은 거의 압축돼 사라지므로 비용 질문의 답이 될 수 없다.
운영 scan 프록시 인자 그대로: ultrafast / crf 30 / fps 10 / scale=-2:H
"""
import json
import os
import subprocess
import sys
import time

SP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SP)
WORK = os.path.join(SP, "work_cost")
FFMPEG = "/opt/homebrew/bin/ffmpeg"
SRC = "/Users/gimsewon/Downloads/신병4_EP1_EPK.mp4"
START, DUR = 300, 600           # 5분 지점부터 10분
FPS, CRF, PRESET = 10, 30, "ultrafast"


def main():
    os.makedirs(WORK, exist_ok=True)
    rows = []
    for name, h in (("480p", 480), ("720p", 720), ("1080p", 1080)):
        out = os.path.join(WORK, f"lf_{name}.mp4")
        t0 = time.time()
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-ss", str(START), "-t", str(DUR),
             "-i", SRC,
             "-vf", f"scale=-2:{h},fps={FPS}", "-fps_mode", "cfr",
             "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
             "-c:a", "aac", "-ac", "1", "-ar", "22050",
             "-threads", "4", out], check=True)
        enc = time.time() - t0
        sz = os.path.getsize(out)
        rows.append({"name": name, "path": out, "bytes": sz,
                     "mb": round(sz / 1e6, 2), "encode_sec": round(enc, 1)})
        print(f"{name:6s} {sz/1e6:8.2f} MB   인코딩 {enc:6.1f}s", flush=True)

    from run_probe import upload
    for r in rows:
        u = upload(r["path"], f"cost_{r['name']}")
        r.update(upload_sec=u["upload_sec"], active_wait_sec=u["active_wait_sec"],
                 file_name=u["name"])
        print(f"{r['name']:6s} 업로드 {u['upload_sec']:6.2f}s  "
              f"ACTIVE 대기 {u['active_wait_sec']:5.2f}s  "
              f"({r['mb']/u['upload_sec']:.1f} MB/s)", flush=True)

    json.dump({"src": SRC, "start": START, "dur": DUR,
               "fps": FPS, "crf": CRF, "preset": PRESET, "rows": rows},
              open(os.path.join(WORK, "cost.json"), "w"), indent=1)
    print("\ncost.json 저장")


if __name__ == "__main__":
    main()
