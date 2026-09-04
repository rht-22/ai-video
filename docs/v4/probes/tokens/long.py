"""3시간 합성 영상을 실제로 업로드해 fps 별 count_tokens 와 실호출 성패를 본다(길이 하드 상한 검증).

    ffmpeg -y -f lavfi -i "testsrc2=size=854x480:rate=2:duration=10800" -f lavfi -i "sine=frequency=440:duration=10800" \
           -c:v libx264 -preset ultrafast -crf 40 -pix_fmt yuv420p -c:a aac -ac 1 -ar 22050 -shortest long3h.mp4
    python long.py long3h.mp4

2026-09-01 실측: 업로드 ACTIVE(364s) · fps 0.85 성공(prompt 875,890) · fps 1.0 은 400(입력 상한 초과).
"""
import sys, time, pathlib, re
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
MODEL="gemini-3.7-flash"; LIMIT=1_048_576
t0=time.time()
print("3시간 파일 업로드 중… (314MB)", flush=True)
f = client.files.upload(file=sys.argv[1])
while f.state.name=="PROCESSING": time.sleep(5); f=client.files.get(name=f.name)
print(f"업로드 {f.state.name} · {time.time()-t0:.0f}s\n", flush=True)
if f.state.name=="FAILED": sys.exit("업로드 실패 — 길이 상한에 걸렸을 가능성")

print(f"{'fps':>6} | {'count_tokens':>14} | {'1M 대비':>9} | 판정")
print("-"*52)
for fps in (0.25,0.5,0.75,0.85,0.9,1.0,1.5,2.0):
    p=types.Part(file_data=types.FileData(file_uri=f.uri,mime_type="video/mp4"),
                 video_metadata=types.VideoMetadata(fps=fps))
    try:
        n=client.models.count_tokens(model=MODEL,contents=[p]).total_tokens
        pct=n/LIMIT*100
        print(f"{fps:>6.2f} | {n:>14,} | {pct:>8.1f}% | {'OK' if n<=LIMIT else '초과'}")
    except Exception as e:
        print(f"{fps:>6.2f} | {'ERR':>14} | {'':>9} | {str(e)[:70]}")

print("\n== 실제 호출도 되는가 (fps=0.85, 출력 1토큰) ==")
p=types.Part(file_data=types.FileData(file_uri=f.uri,mime_type="video/mp4"),
             video_metadata=types.VideoMetadata(fps=0.85))
try:
    r=client.models.generate_content(model=MODEL,contents=[p,"이 영상 길이를 한 단어로."],
        config=types.GenerateContentConfig(max_output_tokens=16))
    print(f"   ✅ 성공 · prompt_token_count = {r.usage_metadata.prompt_token_count:,}")
except Exception as e:
    print("   ❌", str(e)[:200])

print("\n== fps=1.0 은? (산식상 1,112,400 = 상한 6% 초과) ==")
p=types.Part(file_data=types.FileData(file_uri=f.uri,mime_type="video/mp4"),
             video_metadata=types.VideoMetadata(fps=1.0))
try:
    r=client.models.generate_content(model=MODEL,contents=[p,"이 영상 길이를 한 단어로."],
        config=types.GenerateContentConfig(max_output_tokens=16))
    print(f"   ✅ 성공 · prompt_token_count = {r.usage_metadata.prompt_token_count:,}")
except Exception as e:
    print("   ❌", str(e)[:200])
client.files.delete(name=f.name)
print("\n삭제 완료")
