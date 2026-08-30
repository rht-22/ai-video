"""chunk_split — Stage 1 경계대로 청크 분석용 프록시를 물리 분할한다.

- **원본은 건드리지 않는다.** 청크 파일은 원본에서 직접 480p/10fps 로 재단한다.
  ⚠ 기존 `chunker.split_video_chunk` 를 재사용하지 않은 사유: 그 함수는 이미 만든
  프록시(4fps)를 다시 자르는 물건이라 발주서의 10fps 를 만들 수 없고, output seek
  (-ss 를 -i 뒤에)라 67분 원본의 뒤쪽 청크마다 앞부분 전체를 디코드한다. v3 는
  input seek + 재인코딩(정확 — copy 가 아니라서 키프레임 정렬 문제 없음) + scale/fps
  필터로 한 번에 자른다. 인코딩 인자(ultrafast·crf 26·모노 22050)는 기존 규약 그대로.
- **exception 구간은 여기서 물리적으로 제거된다** — Stage 1 커버리지 계약(러닝타임 =
  sequences ∪ exception, 겹침 0)으로 chunk 는 애초에 exception 과 겹칠 수 없지만,
  믿지 않고 재검증한다(`assert_no_exception_overlap`). 이후 단계는 exception 의
  존재 자체를 모른다.
- 매니페스트(`checkpoint_chunk_split.json`)가 파일 ↔ sequence/chunk 번호 ↔ 소스 시각
  오프셋의 정본이다 — 이후 모든 시각 환산의 근거(발주서 §A).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.v3 import schemas

SCHEMA_CHUNK_SPLIT = "v3_chunk_split/v1"
CHUNK_PROXY_HEIGHT = 480
CHUNK_PROXY_FPS = 10          # 발주서 명시 — 기존 4fps 프록시 재단과 달리 원본에서 직접


def plan_chunks(stage1_doc: dict) -> tuple[list[dict], list[dict]]:
    """stage1 문서 → (청크 계획, exception 목록). 순수 — 테스트 대상.

    반환 계획 항목: {seq_number, chunk_number, start_sec, end_sec}.
    10분 상한을 재검증한다(Stage 1 이 지켰어도 여기가 마지막 관문)."""
    chunks: list[dict] = []
    for sq in stage1_doc.get("sequences") or []:
        for ch in sq.get("chunks") or []:
            s = schemas.parse_ts(ch["time"]["start"])
            e = schemas.parse_ts(ch["time"]["end"])
            if e <= s:
                raise ValueError(f"chunk 구간 역전: seq{sq['number']} ch{ch['number']}")
            if e - s > schemas.CHUNK_MAX_SEC + schemas.COVERAGE_EPS_SEC:
                raise ValueError(
                    f"chunk 10분 상한 위반: seq{sq['number']} ch{ch['number']} {e - s:.1f}s")
            chunks.append({"seq_number": int(sq["number"]),
                           "chunk_number": int(ch["number"]),
                           "start_sec": round(s, 3), "end_sec": round(e, 3)})
    if not chunks:
        raise ValueError("stage1 에 chunk 가 없다")
    chunks.sort(key=lambda c: (c["start_sec"], c["seq_number"], c["chunk_number"]))

    exceptions = []
    for k, v in (stage1_doc.get("exception_sector") or {}).items():
        if v is not None:
            exceptions.append({"key": k,
                               "start_sec": round(schemas.parse_ts(v["start"]), 3),
                               "end_sec": round(schemas.parse_ts(v["end"]), 3)})
    return chunks, exceptions


def assert_no_exception_overlap(chunks: list[dict], exceptions: list[dict],
                                eps: float = schemas.COVERAGE_EPS_SEC) -> None:
    """chunk ∩ exception = 0 재검증 — 어긋나면 크게 실패(조용한 유입 금지)."""
    for c in chunks:
        for x in exceptions:
            lap = min(c["end_sec"], x["end_sec"]) - max(c["start_sec"], x["start_sec"])
            if lap > eps:
                raise ValueError(
                    f"exception 유입: seq{c['seq_number']} ch{c['chunk_number']} "
                    f"({c['start_sec']}~{c['end_sec']}) ∩ {x['key']} "
                    f"({x['start_sec']}~{x['end_sec']}) = {lap:.3f}s")


def chunk_file_name(seq_number: int, chunk_number: int) -> str:
    return f"chunk_s{seq_number:02d}_c{chunk_number:02d}.mp4"


def split_chunks(video_path: Path, chunks: list[dict], exceptions: list[dict],
                 out_dir: Path, *, only: list[tuple[int, int]] | None = None,
                 log=print) -> dict:
    """청크 파일 재단 + 매니페스트 dict 반환. 이미 있는 파일은 재사용.

    only 가 주어지면 그 (seq, chunk) 목록만 재단한다(스모크 — 매니페스트에는 전
    계획이 실리고 파일 없는 항목은 file=null)."""
    assert_no_exception_overlap(chunks, exceptions)
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg_command("ffmpeg")
    manifest_chunks: list[dict] = []
    for c in chunks:
        name = chunk_file_name(c["seq_number"], c["chunk_number"])
        entry = {**c, "duration_sec": round(c["end_sec"] - c["start_sec"], 3),
                 "file": None}
        wanted = only is None or (c["seq_number"], c["chunk_number"]) in only
        if wanted:
            path = out_dir / name
            if not path.exists():
                cmd = [ffmpeg, "-y",
                       "-ss", f"{c['start_sec']:.3f}",
                       "-i", str(Path(video_path).resolve()),
                       "-t", f"{entry['duration_sec']:.3f}",
                       "-vf", f"scale=-2:{CHUNK_PROXY_HEIGHT},fps={CHUNK_PROXY_FPS}",
                       "-fps_mode", "cfr",
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                       "-c:a", "aac", "-ac", "1", "-ar", "22050",
                       "-avoid_negative_ts", "make_zero",
                       "-threads", "4", str(path)]
                log(f"  [v3/chunk_split] seq{c['seq_number']} ch{c['chunk_number']} "
                    f"{entry['duration_sec']:.0f}s → {name}")
                subprocess.run(cmd, check=True, capture_output=True)
            entry["file"] = name
        manifest_chunks.append(entry)
    return {
        "schema": SCHEMA_CHUNK_SPLIT,
        "proxy": {"height": CHUNK_PROXY_HEIGHT, "fps": CHUNK_PROXY_FPS,
                  "source": str(video_path)},
        "chunks": manifest_chunks,
        "exceptions_removed": exceptions,
    }
