"""v3 잡 → 프리미어(FCP7 xmeml) 참조 시퀀스 + SRT (별건 · 자산 나머지, 2026-09-08).

수작업 `~/premiere_claude/builders/build12.py` 의 XML 생성부를 엔진 산출(edit_plan.json ·
checkpoint_resources.json · subtitle_segments.json)에서 변환한다. 사람이 프리미어에서 같은 컷을
열어 손보는 **참조용**이다 — 렌더 정본은 final_1080x1920.mp4 다.

    .venv/bin/python -m scripts.edit_plan_to_xml --job outputs/<job> [--out <file.xml>] [--fps 24]

- 비디오 트랙: 소스 클립을 편집본 순서로(in/out = 소스 프레임 · start/end = 편집본 프레임).
  hold_sec(정보 화면 붙잡기)는 마지막 프레임 정지가 xmeml 에 없어 **클립 길이에 포함하지 않고**
  주석(<comments>)에 남긴다.
- 오디오 트랙 1: 같은 클립(원음 끄는 클립은 Level 0). 트랙 2: 내레이션 cue(tts_cue_*.mp3).
- 프리미어는 <center> 를 무시하므로 크롭/위치는 넣지 않는다(build12 와 같은 한계).
- 자막은 SRT 사이드카(편집본 좌표) — 오버레이 PNG 는 만들지 않는다(엔진 렌더가 정본).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _rate(tb: int) -> str:
    return f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if tb in (24, 30, 60) else 'FALSE'}</ntsc></rate>"


def _furl(p: Path) -> str:
    return "file://localhost" + str(p.resolve()).replace(" ", "%20")


def _level(gain: float) -> str:
    return ('<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid>'
            '<effectcategory>audiolevels</effectcategory><effecttype>audiolevels</effecttype>'
            '<mediatype>audio</mediatype><parameter><parameterid>level</parameterid><name>Level</name>'
            f'<valuemin>0</valuemin><valuemax>3.98109</valuemax><value>{gain:.4f}</value>'
            '</parameter></effect></filter>')


def _srt_ts(sec: float) -> str:
    h = int(sec // 3600); m = int(sec % 3600 // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"


def build_xml(plan: dict, cues: list[dict], *, video_path: Path, fps: int,
              src_duration_sec: float, seq_name: str) -> str:
    """순수 — 문자열만 만든다(테스트 대상)."""
    def f(sec: float) -> int:
        return int(round(float(sec) * fps))
    tl = plan["timeline"]
    files: dict[str, str] = {}
    cid = [0]

    def file_el(path: Path, kind: str, df: int, w=None, h=None, audio=True) -> str:
        key = str(path)
        fid = "f" + str(abs(hash(key)) % 10**9)
        if key in files:
            return f'<file id="{files[key]}"/>'
        files[key] = fid
        x = (f'<file id="{fid}"><name>{escape(path.name)}</name>'
             f'<pathurl>{escape(_furl(path))}</pathurl>{_rate(fps)}<duration>{df}</duration><media>')
        if kind == "video":
            x += (f'<video><samplecharacteristics>{_rate(fps)}<width>{w}</width><height>{h}</height>'
                  '<pixelaspectratio>square</pixelaspectratio></samplecharacteristics></video>')
        if audio:
            x += ('<audio><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate>'
                  '</samplecharacteristics><channelcount>2</channelcount></audio>')
        return x + '</media></file>'

    def clip(name: str, path: Path, si: int, so: int, ci: int, co: int, kind: str, extra: str = "",
             df: int | None = None, w=None, h=None, audio=True, atrack=False, comment: str = "") -> str:
        cid[0] += 1
        x = (f'<clipitem id="c{cid[0]}"><name>{escape(name)}</name><enabled>TRUE</enabled>'
             f'<duration>{df or f(src_duration_sec)}</duration>{_rate(fps)}<start>{si}</start><end>{so}</end>'
             f'<in>{ci}</in><out>{co}</out>')
        x += file_el(path, kind, df or f(src_duration_sec), w, h, audio)
        x += ('<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>' if atrack
              else '<sourcetrack><mediatype>video</mediatype><trackindex>1</trackindex></sourcetrack>')
        if comment:
            x += f'<comments><mastercomment1>{escape(comment)}</mastercomment1></comments>'
        return x + extra + '</clipitem>'

    W, H = (int(x) for x in str((plan.get("layout") or {}).get("canvas") or "1080x1920").split("x"))
    total = sum(float(c["clip_end_sec"]) - float(c["clip_start_sec"]) for c in tl)
    out = ['<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE xmeml><xmeml version="5"><sequence id="seq1">',
           f'<name>{escape(seq_name)}</name><duration>{f(total)}</duration>{_rate(fps)}',
           f'<timecode><string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat>{_rate(fps)}</timecode>',
           f'<media><video><format><samplecharacteristics>{_rate(fps)}<width>{W}</width><height>{H}</height>'
           '<pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance></samplecharacteristics></format>',
           '<track>']
    t = 0.0
    rows = []
    for i, c in enumerate(tl):
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        rows.append((i, c, t, t + (e - s)))
        t += e - s
    for i, c, a, b in rows:
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        cm = f"clip{i} {c.get('role')}" + (f" cover={c['cover']}" if c.get("cover") else "") \
            + (f" hold={c['hold_sec']}s" if c.get("hold_sec") else "") \
            + (f" subject={c['subject_pos']}" if c.get("subject_pos") else "")
        out.append(clip(video_path.stem, video_path, f(a), f(b), f(s), f(e), "video", w=1920, h=1080, comment=cm))
    out.append('</track></video><audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics>'
               '<depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></format><track>')
    for i, c, a, b in rows:
        s, e = float(c["clip_start_sec"]), float(c["clip_end_sec"])
        gain = _level(0.0) if not c.get("use_original_audio") else ""
        out.append(clip(video_path.stem, video_path, f(a), f(b), f(s), f(e), "video", gain, atrack=True))
    out.append('</track><track>')
    for cue in cues:
        p = Path(cue["path"])
        st, du = float(cue["cue"]["start_sec"]), float(cue["cue"]["duration_sec"])
        out.append(clip(p.stem, p, f(st), f(st + du), 0, f(du), "audio", df=f(du), atrack=True,
                        comment=str(cue["cue"].get("text") or "")))
    out.append('</track></audio></media></sequence></xmeml>')
    return "".join(out)


def build_srt(segments: list[dict]) -> str:
    lines = []
    for i, sg in enumerate(sorted(segments, key=lambda x: float(x["start_sec"])), 1):
        lines.append(f"{i}\n{_srt_ts(float(sg['start_sec']))} --> {_srt_ts(float(sg['end_sec']))}\n{sg['text']}\n")
    return "\n".join(lines) + ("\n" if lines else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--job", required=True, help="v3 잡 디렉토리(edit_plan.json 이 있는 곳)")
    ap.add_argument("--out", default=None, help="출력 xml 경로(기본 <job>/premiere_ref.xml)")
    ap.add_argument("--fps", type=int, default=None, help="타임베이스(기본: 소스 fps 반올림)")
    args = ap.parse_args(argv)
    job = Path(args.job)
    plan = json.loads((job / "edit_plan.json").read_text(encoding="utf-8"))
    res_p = job / "checkpoint_resources.json"
    cues = json.loads(res_p.read_text(encoding="utf-8")).get("tts_cue_files", []) if res_p.exists() else []
    probe_p = job / "checkpoint_probe.json"
    probe = json.loads(probe_p.read_text(encoding="utf-8")) if probe_p.exists() else {}
    video = Path((plan.get("input") or {}).get("video_path") or probe.get("path") or "")
    fps = args.fps or int(round(float(plan.get("source_fps") or probe.get("fps") or 24)))
    dur = float(probe.get("duration_sec") or 0) or max(float(c["clip_end_sec"]) for c in plan["timeline"]) + 1
    xml = build_xml(plan, cues, video_path=video, fps=fps, src_duration_sec=dur, seq_name=job.name)
    out = Path(args.out) if args.out else job / "premiere_ref.xml"
    out.write_text(xml, encoding="utf-8")
    seg_p = job / "subtitle_segments.json"
    if seg_p.exists():
        (out.with_suffix(".srt")).write_text(build_srt(json.loads(seg_p.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"[xml] {out} (클립 {len(plan['timeline'])} · cue {len(cues)} · {fps}fps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
