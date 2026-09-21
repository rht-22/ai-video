"""권리사 채널에 올라온 가로 클립들을 회차·업로드 순으로 이어 붙여 '당일판 몰아보기' 소스를 만든다. 작품 무관 — 재생목록형 작품
(권리사가 회차 공개일에 2~5분 클립 여러 개를 올리는 유형: 티빙·tvN·쿠팡플레이 클립 채널) 공통 절차다.

사용:
  .venv/bin/python scripts/concat_source_clips.py --work "로또_1등도_출근합니다" --tag 3-4화 \
      --ids sp5LNA8wR20 j9Biw7O-f5Q rEWxR34av40 9laVqueKQlE ibInXByzkNA CyY3s9XPwQc
  회차 표기가 다른 작품은 --episode-regex 로(예: "EP\\.?\\s*(\\d{1,3})").

산출(outputs/<work>/_source/concat_<tag>/):
  source.mp4      — 1920×1080 · 29.97fps · AAC 48k 스테레오 · 라우드니스 -16 LUFS 로 통일한 합본
  manifest.json   — 합본 시각 ↔ 원 클립(id·제목·회차·업로드 시각·트림한 앞뒤 초) 대응표. 장부·검수가 이걸로 원 클립 좌표를 되찾는다
  clips/<id>.mp4  — 내려받은 원본(재실행 시 재사용)

규칙:
  - 순서는 --ids 로 준 순서 그대로(회차 → 업로드 시각 순으로 넘길 것). 자동 정렬은 하지 않는다 — 순서가 곧 시간축이라 사람이 본다.
  - 앞 범퍼: 파일 머리의 '검정 화면 ∧ 무음'(최대 3초). 뒤 엔드카드: 파일 끝에 붙은 **무음 1.5초 이상**(최대 12초) — 권리사 엔드카드는
    검정이 아니라 브랜드 카드(티빙: 빨강 쐐기 + '티빙 바로가기')라 검정 검출로는 못 잡고, 무음이 정확한 신호였다(6클립 실측 8.5s).
    잘린 양은 manifest 에 남긴다.
  - 같은 장면이 두 클립에 있는지(겹침)는 여기서 검사하지 않는다 — 오디오 지문 대조(scratchpad fp.py)로 따로 본다.
  - yt-dlp 는 PATH 에 없으면 --ytdlp 로 경로를 준다.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
HEAD_MAX_SEC = 3.0        # 앞 범퍼(검정 ∧ 무음) 상한
TAIL_MAX_SEC = 12.0       # 뒤 엔드카드 상한 — 티빙 엔드카드는 무음 8.5s(정지 화면 + '티빙 바로가기' 카드) 실측
TAIL_MIN_SIL_SEC = 1.5    # 파일 끝에 붙은 무음이 이 이상일 때만 엔드카드로 본다(조용히 끝나는 장면 보호)
BLACK_TH = 0.10          # blackdetect pixel threshold
SIL_DB = -45             # silencedetect noise floor


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def download(vid: str, dest: Path, ytdlp: str) -> Path:
    out = dest / f"{vid}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    run([ytdlp, "-q", "--no-warnings",
         "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
         "--merge-output-format", "mp4", "-o", str(out), f"https://www.youtube.com/watch?v={vid}"])
    return out


def metadata(vid: str, ytdlp: str, ep_re: str) -> dict:
    j = json.loads(run([ytdlp, "-q", "--no-warnings", "--skip-download", "--extractor-args", "youtube:lang=ko",
                        "-J", f"https://www.youtube.com/watch?v={vid}"]).stdout)
    title = j.get("title") or ""
    m = re.search(ep_re, title)
    ts = j.get("timestamp")
    return {
        "id": vid, "title": title, "episode": int(m.group(1)) if m else None,
        "duration_sec": j.get("duration"),
        "published_kst": datetime.fromtimestamp(ts, KST).isoformat() if ts else None,
        "width": j.get("width"), "height": j.get("height"),
    }


def probe_duration(path: Path) -> float:
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).stdout.strip())


def _spans(stderr: str, start_key: str, end_key: str) -> list[tuple[float, float]]:
    starts = [float(x) for x in re.findall(rf"{start_key}:\s*([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(rf"{end_key}:\s*([\d.]+)", stderr)]
    return list(zip(starts, ends))


def bumper_trim(path: Path, dur: float) -> tuple[float, float]:
    """(앞에서 자를 초, 뒤에서 자를 초). 앞 = 검정 ∧ 무음, 뒤 = 파일 끝 무음(엔드카드)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                        "-vf", f"blackdetect=d=0.2:pix_th={BLACK_TH}", "-af", f"silencedetect=n={SIL_DB}dB:d=0.2",
                        "-f", "null", "-"], text=True, capture_output=True)
    blacks = _spans(p.stderr, "black_start", "black_end")
    sils = _spans(p.stderr, "silence_start", "silence_end")

    def edge(spans, at_start: bool) -> float:
        best = 0.0
        for a, b in spans:
            if at_start and a <= 0.05:
                best = max(best, b)
            if not at_start and b >= dur - 0.05:
                best = max(best, dur - a)
        return best

    head = min(edge(blacks, True), edge(sils, True), HEAD_MAX_SEC)
    tail_sil = edge(sils, False)
    tail = min(tail_sil, TAIL_MAX_SEC) if tail_sil >= TAIL_MIN_SIL_SEC else 0.0
    return round(head, 2), round(tail, 2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="outputs/<work> 폴더명 (예: 로또_1등도_출근합니다)")
    ap.add_argument("--tag", required=True, help="합본 이름 (예: 3-4화)")
    ap.add_argument("--ids", nargs="+", required=True, help="유튜브 ID, 이을 순서대로")
    ap.add_argument("--ytdlp", default=shutil.which("yt-dlp") or "yt-dlp")
    ap.add_argument("--episode-regex", default=r"(\d{1,2})화", help="제목에서 회차 번호를 뽑는 정규식(그룹 1). 기본 'N화'")
    ap.add_argument("--no-trim", action="store_true", help="양끝 범퍼 트림 생략")
    ap.add_argument("--lufs", type=float, default=-16.0)
    a = ap.parse_args(argv)

    out_dir = ROOT / "outputs" / a.work / "_source" / f"concat_{a.tag}"
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    t = 0.0
    inputs: list[str] = []
    filters: list[str] = []
    for i, vid in enumerate(a.ids):
        meta = metadata(vid, a.ytdlp, a.episode_regex)
        path = download(vid, clips_dir, a.ytdlp)
        dur = probe_duration(path)
        head, tail = (0.0, 0.0) if a.no_trim else bumper_trim(path, dur)
        kept = dur - head - tail
        entries.append({**meta, "file": str(path.relative_to(ROOT)), "orig_duration_sec": round(dur, 3),
                        "trim_head_sec": head, "trim_tail_sec": tail,
                        "concat_start_sec": round(t, 3), "concat_end_sec": round(t + kept, 3)})
        inputs += ["-i", str(path)]
        filters.append(
            f"[{i}:v]trim=start={head}:end={dur - tail},setpts=PTS-STARTPTS,"
            f"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
            f"fps=30000/1001,setsar=1,format=yuv420p[v{i}];"
            f"[{i}:a]atrim=start={head}:end={dur - tail},asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=channel_layouts=stereo[a{i}]")
        print(f"[{i+1}/{len(a.ids)}] {vid} {meta['episode']}화 {dur:.1f}s trim {head}/{tail} → {t:.1f}~{t + kept:.1f}  {meta['title'][:50]}", flush=True)
        t += kept
    concat = "".join(f"[v{i}][a{i}]" for i in range(len(a.ids))) + f"concat=n={len(a.ids)}:v=1:a=1[v][ac];[ac]loudnorm=I={a.lufs}:TP=-1.5:LRA=11,aresample=48000[a]"
    filter_complex = ";".join(filters) + ";" + concat
    final = out_dir / "source.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", filter_complex,
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-level", "4.2",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)]
    subprocess.run(cmd, check=True)
    total = probe_duration(final)
    manifest = {"work": a.work, "tag": a.tag, "created_kst": datetime.now(KST).isoformat(),
                "source": str(final.relative_to(ROOT)), "total_sec": round(total, 3),
                "planned_total_sec": round(t, 3), "lufs_target": a.lufs, "clips": entries}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n합본 {final} — {total:.1f}s (계획 {t:.1f}s) · manifest {out_dir / 'manifest.json'}")
    if abs(total - t) > 0.5:
        print(f"⚠ 합본 길이가 계획과 {total - t:+.2f}s 다르다 — 트림·fps 변환을 확인할 것", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
