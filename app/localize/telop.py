"""L2 · L2b — 화면에 박힌 텍스트 추출과 타이밍 재보정.

원본: `localize_run.l2_extract` · `l2b_refine_timing`.

L2 는 완성 영상을 Gemini Pro 에 넣어 화면 글자를 목록으로 뽑는다.
L2b 가 따로 있는 이유: **Gemini 영상 타임스탬프가 런마다 10~20초씩 어긋난다**
(파일럿 _74 실측 — 스파이크에선 우연히 정확했다). 그래서 텍스트는 L2 를 믿되
타이밍은 1.5초 간격 프레임을 **1장=1콜**로 대조해 다시 잡는다.
⚠ 프레임 여러 장을 한 콜에 넣으면 인덱스가 오배정된다 — 반드시 1장 1콜.

⚠ 텔롭 인덱스 규약: **broadcast_telop 만 추린 목록의 순서**가 L1·L2b·L3 공통 키다.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from app.localize.spec import model_flash, model_pro
from app.modules.ffmpeg_utils import find_ffmpeg_command

# response_schema — free-form JSON 은 텍스트 안 따옴표에서 곧잘 깨진다(파일럿 실측).
SCHEMA_EXTRACT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "start_sec": {"type": "number"},
            "end_sec": {"type": "number"},
            "text_ko": {"type": "string"},
            "position": {"type": "string", "enum": ["top", "middle", "bottom"]},
            "kind": {"type": "string",
                     "enum": ["broadcast_telop", "our_subtitle", "our_tts", "top_title",
                              "our_style_text", "other"]},
        },
        "required": ["start_sec", "end_sec", "text_ko", "position", "kind"],
    },
}

SCHEMA_REFINE = {
    "type": "object",
    "properties": {"visible": {"type": "array", "items": {"type": "integer"}}},
    "required": ["visible"],
}

EXTRACT_PROMPT = """이 영상은 한국 예능 프로그램의 쇼츠(세로 1080x1920)입니다.
화면에 **렌더링된 텍스트(글자)** 를 전부 찾아 주세요. 음성이 아니라 화면에 보이는 글자입니다.

분류(kind):
- "broadcast_telop": 원본 방송이 넣은 자막·텔롭·효과 문구 (내용 이해에 중요)
- "our_subtitle": 우리가 넣은 대사 자막 (존재만 기록)
- "our_tts": 우리가 넣은 하늘색 내레이션 자막 (존재만 기록)
- "top_title": 최상단 검정 배경 위 흰/노랑 2줄 제목 (존재만 기록)
- "our_style_text": 우리가 얹은 **연출 텍스트**. 짧은 감탄·의성어·강조 한두 마디를
  굵은 색 글씨(노랑/빨강 등)로 화면 가운데쯤에 톡 얹어 놓은 것 (존재만 기록)
- "other": 배경 간판·소품·로고 등

⚠ 방송 텔롭과 우리 연출 텍스트를 헷갈리지 마세요. 방송 텔롭은 **문장**이고 원본 화면에
박혀 있으며 흔히 여러 줄입니다. 우리 연출 텍스트는 "멘붕?!" "쿵!" 처럼 **한두 마디**이고
원본 화면 위에 덧그려져 있습니다.

각 항목: start_sec, end_sec (숫자), text_ko (원문 그대로), position ("top"/"middle"/"bottom"), kind.
번역은 하지 마세요. JSON 배열만 출력하세요."""

REFINE_STEP_SEC = 1.5


def only_broadcast_telops(telop_data: list) -> list:
    """공통 인덱스 규약 — broadcast_telop 만 추린 목록. 순수(테스트 대상)."""
    return [t for t in (telop_data or []) if t.get("kind") == "broadcast_telop"]


def group_hits(hits: list[int], max_gap: int = 2) -> list[list[int]]:
    """프레임 히트 → 연속 구간들. 한 텔롭이 두 번 뜨는 경우를 나눈다. 순수(테스트 대상)."""
    if not hits:
        return []
    groups, cur = [], [hits[0]]
    for fi in hits[1:]:
        if fi - cur[-1] <= max_gap:
            cur.append(fi)
        else:
            groups.append(cur)
            cur = [fi]
    groups.append(cur)
    return groups


def _probe_duration(video: Path) -> float:
    return float(subprocess.run(
        [find_ffmpeg_command("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True).stdout.strip())


def l2_extract(job: Path, out_dir: Path, client) -> list:
    out_path = out_dir / "onscreen.json"
    if out_path.exists():
        print("[L2] 기존 추출 결과 사용")
        return json.loads(out_path.read_text(encoding="utf-8"))
    from google.genai import types
    video = job / "shorts_ko.mp4"
    print(f"[L2] 업로드: {video.name} ({video.stat().st_size/1e6:.0f}MB)")
    f = client.files.upload(file=str(video))
    while f.state.name == "PROCESSING":
        time.sleep(5)
        f = client.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"업로드 실패: state={f.state.name}")
    t0 = time.time()
    resp = client.models.generate_content(
        model=model_pro(), contents=[f, EXTRACT_PROMPT],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=SCHEMA_EXTRACT),
    )
    data = json.loads(resp.text)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    telops = only_broadcast_telops(data)
    print(f"[L2] {time.time()-t0:.0f}s — 전체 {len(data)}건, 방송 텔롭 {len(telops)}건")
    return data


def l2b_refine_timing(job: Path, telop_data: list, out_dir: Path, client) -> list:
    """broadcast_telop 의 start/end 를 프레임 대조로 재보정한 목록을 돌려준다."""
    out_path = out_dir / "onscreen_refined.json"
    if out_path.exists():
        print("[L2b] 기존 재보정 결과 사용")
        return json.loads(out_path.read_text(encoding="utf-8"))
    from google.genai import types
    telops = only_broadcast_telops(telop_data)
    if not telops:
        out_path.write_text("[]", encoding="utf-8")
        return []
    video = job / "shorts_ko.mp4"
    dur = _probe_duration(video)
    frames_dir = out_dir / "refine_frames"
    frames_dir.mkdir(exist_ok=True)
    ts = [round(i * REFINE_STEP_SEC, 2) for i in range(int(dur / REFINE_STEP_SEC) + 1)]
    listing = "\n".join(f"{i}: {t['text_ko']}" for i, t in enumerate(telops))
    base_prompt = (
        "이 이미지는 한국 예능 쇼츠의 한 프레임입니다. 아래 텔롭 목록 중 **이 프레임 화면에 "
        "글자로 보이는 것**의 번호만 고르세요. 부분적으로 보여도(잘림·페이드) 포함합니다. "
        "비슷한 다른 문구와 혼동하지 마세요. 하나도 없으면 빈 배열.\n\n"
        f"텔롭 목록:\n{listing}")
    ffmpeg = find_ffmpeg_command("ffmpeg")
    frame_paths = []
    frame_extract_failed = 0
    for i, t in enumerate(ts):
        fp = frames_dir / f"f{i:03d}.jpg"
        if not fp.exists():
            r = subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", str(t), "-i", str(video),
                                "-frames:v", "1", "-vf", "scale=360:-1", str(fp)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                # 프레임 한 장 추출 실패로 전체 L2b 가 죽으면 안 된다 — 아래 판독 실패
                # ("판독 실패, 제외")와 같은 규율로 이 프레임만 빼고 계속한다. stderr 를
                # 남기는 이유: check=True 의 CalledProcessError 는 exit code 만 보여주고
                # 진짜 이유(디코드 실패·시크 범위 밖 등)는 버려서 원인을 못 봤다.
                print(f"[L2b] ⚠️ frame {i}(t={t}s) 추출 실패(exit {r.returncode}) — 제외: "
                      f"{(r.stderr or r.stdout or '').strip()[-300:]}")
                frame_paths.append(None)
                frame_extract_failed += 1
                continue
        frame_paths.append(fp)

    t0 = time.time()
    flash = model_flash()

    def check_frame(i):
        fp = frame_paths[i]
        if fp is None:
            return i, []
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=flash,
                    contents=[base_prompt,
                              types.Part.from_bytes(data=fp.read_bytes(),
                                                    mime_type="image/jpeg")],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", response_schema=SCHEMA_REFINE),
                )
                return i, [int(v) for v in json.loads(resp.text).get("visible", [])]
            except Exception as e:                       # noqa: BLE001
                if attempt == 2:
                    print(f"[L2b] ⚠️ frame {i} 판독 실패: {e}")
                    return i, []
                time.sleep(2 * (attempt + 1))

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check_frame, range(len(ts))))
    seen = {i: vis for i, vis in results}
    refined = []
    for ti, t in enumerate(telops):
        hits = sorted(fi for fi, vis in seen.items() if ti in vis and 0 <= fi < len(ts))
        if not hits:
            print(f"[L2b] ⚠️ 텔롭 {ti} ({t['text_ko'][:20]!r}) — 프레임 대조 실패, 제외")
            continue
        main = max(group_hits(hits), key=len)    # 병기는 가장 길게 보인 구간 하나만
        start = max(0.0, ts[main[0]] - REFINE_STEP_SEC / 2)
        end = min(dur, ts[main[-1]] + REFINE_STEP_SEC / 2)
        refined.append({**t, "orig_index": ti,
                        "start_sec": round(start, 2), "end_sec": round(end, 2)})
    out_path.write_text(json.dumps(refined, ensure_ascii=False, indent=2), encoding="utf-8")
    fail_note = f" (추출 실패 {frame_extract_failed}장 제외)" if frame_extract_failed else ""
    print(f"[L2b] {time.time()-t0:.0f}s — 프레임 {len(ts)}장 대조{fail_note}, "
          f"텔롭 {len(refined)}/{len(telops)}건 타이밍 확정")
    return refined
