"""60초 보정본으로 fps·media_resolution 별 count_tokens 를 재고 프레임당/초당 토큰을 역산한다.

    ffmpeg -y -f lavfi -i "testsrc2=size=854x480:rate=10:duration=60" -f lavfi -i "sine=frequency=440:duration=60" \
           -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p -c:a aac -ac 1 -ar 22050 -shortest cal60.mp4
    python measure.py cal60.mp4 60

2026-09-01 실측(gemini-3.7-flash): 프레임당 71 · 오디오 초당 32 · 6개 fps 표본 오차 0 (CLAUDE.md 'V3 표본 fps·해상도 계약').
"""
import os, sys, time, pathlib, re
from google import genai
from google.genai import types

# API 키: 환경변수 GEMINI_API_KEY (또는 저장소 .env — python-dotenv 가 있으면 자동 로드)
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
_key = os.environ.get("GEMINI_API_KEY")
if not _key:
    sys.exit("GEMINI_API_KEY 없음 — 환경변수 또는 .env 에 넣어라")
client = genai.Client(api_key=_key)

MODEL = os.environ.get("M", "gemini-3.7-flash")
path, dur = sys.argv[1], float(sys.argv[2])

print(f"모델 {MODEL} · {pathlib.Path(path).name} · {dur:g}s 업로드 중…", flush=True)
f = client.files.upload(file=path)
while f.state.name == "PROCESSING":
    time.sleep(3); f = client.files.get(name=f.name)
if f.state.name == "FAILED": sys.exit(f"업로드 실패: {f.state}")
print(f"업로드 완료 ({f.state.name})\n", flush=True)

RES = [("LOW", types.MediaResolution.MEDIA_RESOLUTION_LOW),
       ("MEDIUM", types.MediaResolution.MEDIA_RESOLUTION_MEDIUM),
       ("HIGH", types.MediaResolution.MEDIA_RESOLUTION_HIGH),
       ("(미지정)", None)]

def count(fps, res):
    p = types.Part(file_data=types.FileData(file_uri=f.uri, mime_type="video/mp4"))
    if fps: p.video_metadata = types.VideoMetadata(fps=fps)
    if res: p.media_resolution = res
    try:
        return client.models.count_tokens(model=MODEL, contents=[p]).total_tokens
    except Exception as e:
        return f"ERR:{str(e)[:60]}"

print(f"{'fps':>6} | " + " | ".join(f"{n:>10}" for n,_ in RES))
print("-"*62)
rows = {}
for fps in (0.25, 0.5, 1.0, 2.0, 3.0, 6.0):
    vals = [count(fps, r) for _, r in RES]
    rows[fps] = vals
    print(f"{fps:>6.2f} | " + " | ".join(f"{v:>10,}" if isinstance(v,int) else f"{str(v)[:10]:>10}" for v in vals), flush=True)

print("\n== 역산 (fps 1.0 vs 3.0 차분) ==")
for i,(n,_) in enumerate(RES):
    a, b = rows[1.0][i], rows[3.0][i]
    if isinstance(a,int) and isinstance(b,int):
        pf = (b-a)/(2.0*dur); rest = a - dur*pf
        print(f"  {n:>9}: 프레임당 ≈ {pf:6.1f} tok · 그 외 ≈ {rest:7.0f} tok (초당 {rest/dur:5.1f})")
client.files.delete(name=f.name)
print("\n업로드 파일 삭제 완료")
