"""권리사 채널에 올라온 가로 클립들을 회차·업로드 순으로 이어 붙여 '당일판 몰아보기' 소스를 만든다. 작품 무관 — 재생목록형 작품
(권리사가 회차 공개일에 2~5분 클립 여러 개를 올리는 유형: 티빙·tvN·쿠팡플레이 클립 채널) 공통 절차다.

사용:
  .venv/bin/python scripts/concat_source_clips.py --work "로또_1등도_출근합니다" --tag 3-4화 \
      --ids sp5LNA8wR20 j9Biw7O-f5Q rEWxR34av40 9laVqueKQlE ibInXByzkNA CyY3s9XPwQc
  회차 표기가 다른 작품은 --episode-regex 로(예: "EP\\.?\\s*(\\d{1,3})").

산출(outputs/<work>/_source/concat_<tag>/):
  source.mp4      — 1920×1080 · 29.97fps · AAC 48k 스테레오 · 라우드니스 -16 LUFS 로 통일한 합본
  manifest.json   — 합본 시각 ↔ 원 클립(id·제목·회차·업로드 시각·트림한 앞뒤 초) 대응표. 장부·검수가 이걸로 원 클립 좌표를 되찾는다
  source_notes.md — 합본 구성 메모(클립 경계 · 선공개 구간 주의). 제작 가이드 형식이라 tikitaka 에 `--guide` 로 함께 넘기면
                    대본 프롬프트의 [제작 가이드] 절에 그대로 실린다(⚠ --guide 를 주면 자동 탐색이 꺼지므로 작품 가이드도 같이 넘길 것)
  clips/<id>.mp4  — 내려받은 원본(재실행 시 재사용)

규칙:
  - 순서는 --ids 로 준 순서 그대로(회차 → 업로드 시각 순으로 넘길 것). 자동 정렬은 하지 않는다 — 순서가 곧 시간축이라 사람이 본다.
  - **예외 — 선공개 영상은 항상 맨 앞**(2026-09-21 사용자 지시): 제목이 --prerelease-regex(기본 '선공개')에 걸리거나 --prerelease-ids 로
    지목한 클립은 --ids 어디에 있든 합본 머리로 옮긴다(선공개끼리는 준 순서 유지). 선공개 = 본방 전날 본편의 **한 장면을 통째로** 먼저 푼 것
    (예고편식 몽타주가 아니다 — 안의 흐름은 본편 그대로). 다만 본편의 어느 시점 장면인지 알 수 없으므로
    회차를 단정하지 않고(episode=null · 제목의 'N-M화' 표기만 episode_label 로) manifest·source_notes 에
    "실제 원본 시간 순서와 다를 수 있다"를 명시한다.
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


PRERELEASE_NOTE = "선공개 영상이라 실제 원본 시간 순서 흐름과 다를 수 있다"


def is_prerelease(meta: dict, regex: str, forced_ids: set[str]) -> bool:
    return meta["id"] in forced_ids or bool(regex and re.search(regex, meta.get("title") or ""))


def order_prerelease_first(metas: list[dict]) -> list[dict]:
    """선공개(prerelease=True)를 머리로 — 두 무리 안의 순서는 준 그대로(안정). 순수 — 테스트 대상."""
    return [m for m in metas if m.get("prerelease")] + [m for m in metas if not m.get("prerelease")]


def _mmss(sec: float) -> str:
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


def source_notes(manifest: dict) -> str:
    """합본 구성 메모(제작 가이드 형식 본문). 구조 키 줄(`키: 값`)은 쓰지 않는다 — 가이드 파서가 설정으로 읽으면 안 된다. 순수 — 테스트 대상."""
    clips = manifest["clips"]
    lines = [f"# 소스 구성 — {manifest['work'].replace('_', ' ')} {manifest['tag']} 합본", "",
             "이 소스는 권리사 유튜브 클립 여러 개를 이어 붙인 합본이다. 클립 경계는 장면 전환이지 시간 연속이 아니다 — "
             "경계를 사이에 둔 두 장면의 시간 흐름(직후·그날·다음 날)을 단정하지 않는다.", ""]
    for c in clips:
        ep = "선공개" if c.get("prerelease") else (f"{c['episode']}화" if c.get("episode") else "회차 미상")
        lines.append(f"- {_mmss(c['concat_start_sec'])}~{_mmss(c['concat_end_sec'])} [{ep}] {c['title']}")
    pre = [c for c in clips if c.get("prerelease")]
    if pre:
        span = ", ".join(f"{_mmss(c['concat_start_sec'])}~{_mmss(c['concat_end_sec'])}" for c in pre)
        lines += ["", f"⚠ 선공개 구간({span}): {PRERELEASE_NOTE}. 선공개는 예고편(여러 장면을 짧게 이어 붙인 편집)이 아니라 "
                  "본방 전날 권리사가 **본편의 한 장면을 통째로** 먼저 공개한 것이다 — 그 안의 흐름은 본편 그대로라 다른 클립과 똑같이 쓸 수 있다. "
                  "다만 합본 맨 앞에 있을 뿐 본편의 어느 회차·어느 시점 장면인지는 알 수 없다(가장 먼저 일어난 일이 아닐 수 있다). "
                  "그러니 이 장면을 근거 없이 '이야기의 시작'·'사건의 발단'으로 놓거나, 다른 클립 장면과의 선후('그 전에'·'그 뒤'·'사실 시작은')를 "
                  "내레이션으로 단정하지 않는다 — 선후는 대사·화면 내용이 직접 말해 줄 때만 쓴다. "
                  "뒤 클립에 같은 장면이 다시 나올 수 있다(같은 장면을 한 편에 두 번 쓰지 않는다)."]
    return "\n".join(lines) + "\n"


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
    ap.add_argument("--prerelease-regex", default="선공개", help="제목이 이 정규식에 걸리면 선공개 — 합본 맨 앞으로. 빈 문자열이면 제목 판정 끔")
    ap.add_argument("--prerelease-ids", nargs="*", default=[], help="제목과 무관하게 선공개로 취급할 유튜브 ID")
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
    metas = []
    for vid in a.ids:
        meta = metadata(vid, a.ytdlp, a.episode_regex)
        if is_prerelease(meta, a.prerelease_regex, set(a.prerelease_ids)):
            label = re.search(r"\d{1,2}\s*[-~]\s*\d{1,2}\s*[화회]", meta["title"])
            meta.update(prerelease=True, episode=None, episode_label=label.group(0) if label else None, order_note=PRERELEASE_NOTE)
        metas.append(meta)
    ordered = order_prerelease_first(metas)
    if [m["id"] for m in ordered] != list(a.ids):
        print(f"[선공개] 맨 앞으로 옮김: {' '.join(m['id'] for m in ordered if m.get('prerelease'))}", flush=True)
    for i, meta in enumerate(ordered):
        vid = meta["id"]
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
        ep_txt = "선공개" if meta.get("prerelease") else f"{meta['episode']}화"
        print(f"[{i+1}/{len(a.ids)}] {vid} {ep_txt} {dur:.1f}s trim {head}/{tail} → {t:.1f}~{t + kept:.1f}  {meta['title'][:50]}", flush=True)
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
    if any(e.get("prerelease") for e in entries):
        manifest["prerelease_note"] = PRERELEASE_NOTE
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "source_notes.md").write_text(source_notes(manifest), encoding="utf-8")
    print(f"\n합본 {final} — {total:.1f}s (계획 {t:.1f}s) · manifest {out_dir / 'manifest.json'}")
    if abs(total - t) > 0.5:
        print(f"⚠ 합본 길이가 계획과 {total - t:+.2f}s 다르다 — 트림·fps 변환을 확인할 것", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
