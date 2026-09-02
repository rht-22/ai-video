"""media_resolution 적용 경로 3가지(Part 필드 / count_tokens config / generate_content usage)를 대조한다.

    python res.py cal60.mp4

2026-09-01 실측: 미지정 = LOW(5,462 동일) · HIGH ≈ 3.2배(17,342).
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
MODEL="gemini-3.7-flash"
f = client.files.upload(file=sys.argv[1])
while f.state.name=="PROCESSING": time.sleep(2); f=client.files.get(name=f.name)

p = types.Part(file_data=types.FileData(file_uri=f.uri, mime_type="video/mp4"),
               video_metadata=types.VideoMetadata(fps=1.0))
p.media_resolution = types.MediaResolution.MEDIA_RESOLUTION_HIGH
print("① Part.media_resolution=HIGH (count_tokens):")
try: print("   ", f"{client.models.count_tokens(model=MODEL, contents=[p]).total_tokens:,}")
except Exception as e: print("    ERR:", str(e)[:220])

print("\n② generation_config 경유 (count_tokens):")
for name, r in [("LOW",types.MediaResolution.MEDIA_RESOLUTION_LOW),
                ("HIGH",types.MediaResolution.MEDIA_RESOLUTION_HIGH)]:
    p2 = types.Part(file_data=types.FileData(file_uri=f.uri, mime_type="video/mp4"),
                    video_metadata=types.VideoMetadata(fps=1.0))
    try:
        n = client.models.count_tokens(model=MODEL, contents=[p2],
              config=types.CountTokensConfig(
                generation_config=types.GenerationConfig(media_resolution=r))).total_tokens
        print(f"    {name}: {n:,}")
    except Exception as e: print(f"    {name}: ERR", str(e)[:180])

print("\n③ generate_content 의 usage_metadata 로 교차 확인 (HIGH, 출력 1토큰):")
for name, r in [("(미지정)",None),("LOW",types.MediaResolution.MEDIA_RESOLUTION_LOW),
                ("HIGH",types.MediaResolution.MEDIA_RESOLUTION_HIGH)]:
    p3 = types.Part(file_data=types.FileData(file_uri=f.uri, mime_type="video/mp4"),
                    video_metadata=types.VideoMetadata(fps=1.0))
    cfg = {"max_output_tokens":1}
    if r: cfg["media_resolution"]=r
    try:
        resp = client.models.generate_content(model=MODEL, contents=[p3,"."],
                 config=types.GenerateContentConfig(**cfg))
        print(f"    {name}: prompt_token_count = {resp.usage_metadata.prompt_token_count:,}")
    except Exception as e: print(f"    {name}: ERR", str(e)[:160])
client.files.delete(name=f.name)
