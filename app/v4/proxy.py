"""프록시 인코딩 · Files API 핸들 수명 — 5단계(upload)의 정본.

계약 `docs/v4/M2-interfaces.md` §3 · 기획 `docs/v4/v4-unified-plan.md` §1 행 5(운영자
결정 O1: **720p / 30fps**). v3 `seq_analyze.build_scan_proxy`(480p/10fps)·`_upload_video`
의 ffmpeg·업로드 인자를 그대로 물려받고 **기하만** 바꾼다.

이 모듈이 v3 함수를 그냥 부르지 않고 따로 존재하는 이유는 둘이다. 둘 다 조사에서
잡힌 실동작이라 주석으로 못박는다.

🛑 ① **파일명에 기하를 박는다.** v3 는 `if out_path.exists(): return out_path` —
   **존재만으로** 재사용한다. 이름이 `scan.mp4` 처럼 기하를 안 들고 있으면 480p/10fps
   시절 잔재를 720p/30fps 실행이 조용히 재사용하고, 그 편만 표본 fps 상한이 10 인
   프록시로 분석된다(로그에는 '재사용' 한 줄뿐이라 아무도 모른다). v4 는
   `scan_720p30.mp4` 로 이름에 기하를 박고, 그 위에 **ffprobe 로 실제 기하까지**
   확인한다 — 이름은 맞는데 내용이 다른 경우(중단된 인코딩, 손으로 옮겨 둔 파일)가
   남기 때문이다.

🛑 ② **핸들 수명을 파이프라인이 관리한다.** v3 는 호출 직후 `finally: files.delete` 를
   **네 곳**에서 한다(`seq_analyze:546` · `chunk_analyze:658` · `refine:338` ·
   `stage4:488` — 직접 확인). v4 는 6·6b·8·10a 가 **같은 핸들을 공유**하므로 단계
   안에서 지우면 뒷단계가 죽은 핸들을 쓴다. 그래서 v4 는 v3 의 그 **호출부**를 쓰지
   않고 이 모듈이 업로드·확인·삭제를 맡는다. 삭제는 최종 렌더가 끝난 뒤(또는 실패
   시 배선의 finally) 한 번이다.
   ⚠ `upload_handle` 은 v3 `_upload_video` 를 **부른다** — 그 함수 자체는 삭제를 하지
   않는다(업로드 + PROCESSING 폴링뿐). 삭제는 전부 그 함수의 호출부에 있었다.

⚠ **720p/30fps 의 파일 크기·업로드 시간은 아직 추정이다**(기획서 §12 '추정' 항목:
480p/10fps 80MB·50초 실측에서 외삽 → 2.5~3.3배 · 업로드 2~3분). 그래서 `PROXY_CRF` 는
v3 값(30)을 **그대로** 시작값으로 쓰되, `build_proxy` 가 `bytes`·`elapsed_sec` 를
돌려주고 로그에 MB/분을 찍어 M2 실측 라운드가 값을 갈아낄 근거를 남긴다.

⚠ 파일 30fps 는 **표본 fps 의 상한**일 뿐이다 — 지금 요청하는 표본 fps 최대는 6b 의
6 이라 30fps 의 추가 정보에는 소비처가 없고, 비용은 업로드 크기다(기획서 §4 각주).
되돌릴 때 고칠 자리는 `app/v4/fps.PROXY_FILE_FPS` 한 곳이다(아래 참조).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.v4 import fps as _fps

# ── 기하 (운영자 결정 O1) ───────────────────────────────────────────────────
PROXY_HEIGHT = 720          # v3 는 480 — 720 은 나중에 6b·8 을 HIGH 로 올릴 때 필수 조합
# ⚠ 파일 fps 의 정본은 `app/v4/fps.py` 다. 그쪽이 "표본 fps 는 파일 fps 를 넘을 수
# 없다"는 상한 판정에 이미 이 값을 쓰고 있어서, 여기 숫자를 또 적으면 한쪽만 고쳐질 때
# 예산 계산과 실제 파일이 갈린다(E13 '베낀 수식' 교훈). 계약 §3 이 이 이름을 이
# 모듈에서 부르므로 **재수출**한다 — 값은 하나다(테스트가 둘을 묶는다).
PROXY_FILE_FPS = _fps.PROXY_FILE_FPS        # = 30.0
PROXY_CRF = 30              # ⚠ v3 값 그대로. 720p/30fps 에서 파일이 크게 늘어난다 —
                            #   M2 실측 뒤 조정 대상(기획서 §12 '추정' 항목)

# 아래 넷은 v3 `build_scan_proxy` 와 **같은 값**이다. 바꾸면 프록시가 v3 와 다른 파일이
# 되므로 상수로 세워 지문 재료에 넣는다(값이 바뀌면 캐시가 무효가 된다).
PROXY_PRESET = "ultrafast"
PROXY_AUDIO_CHANNELS = 1
PROXY_AUDIO_RATE = 22050    # 오디오는 **유지한다** — intro/teaser·훅 판정에 음악·톤이
                            # 실질 단서다(v3 build_scan_proxy 독스트링과 같은 결정)
PROXY_THREADS = 4

# ffprobe 로 잰 기하가 이 안이면 같은 것으로 본다. fps 는 컨테이너가 30000/1001 처럼
# 유리수로 적는 경우가 있어 정확 비교가 성립하지 않는다.
FPS_TOLERANCE = 0.05

SCHEMA_UPLOAD = "v4_upload/v1"
CHECKPOINT_UPLOAD_NAME = "checkpoint_upload.json"

__all__ = [
    "PROXY_HEIGHT", "PROXY_FILE_FPS", "PROXY_CRF", "PROXY_PRESET",
    "PROXY_AUDIO_CHANNELS", "PROXY_AUDIO_RATE", "PROXY_THREADS",
    "FPS_TOLERANCE", "SCHEMA_UPLOAD", "CHECKPOINT_UPLOAD_NAME",
    "proxy_path_for", "probe_geometry", "build_proxy", "proxy_fingerprint",
    "upload_handle", "release_handle", "handle_alive", "handle_name_of",
    "upload_checkpoint_doc",
]


# ── 프록시 ──────────────────────────────────────────────────────────────────

def _fps_label(file_fps: float) -> str:
    """파일명에 박을 fps 표기 — 30.0 → "30" · 29.97 → "29_97".

    소수점을 그대로 쓰면 파일명에 확장자가 둘인 것처럼 보이고(`scan_720p29.97.mp4`)
    확장자만 보고 자르는 코드에 걸린다. 밑줄로 바꾼다."""
    f = float(file_fps)
    if f == int(f):
        return str(int(f))
    return ("%g" % f).replace(".", "_")


def proxy_path_for(output_dir: Path, *, height: int = PROXY_HEIGHT,
                   file_fps: float = PROXY_FILE_FPS) -> Path:
    """파일명에 기하를 박는다 — `scan_720p30.mp4`. 순수.

    🛑 모듈 독스트링 ①: v3 `build_scan_proxy` 는 out_path 존재만으로 재사용한다.
    이름에 기하가 없으면 480p 잔재를 720p 실행이 조용히 재사용한다(조사 지적).
    기하가 다르면 **다른 경로**가 나오는 것이 이 함수의 전부이자 계약이다."""
    return Path(output_dir) / f"scan_{int(height)}p{_fps_label(file_fps)}.mp4"


def probe_geometry(path: Path) -> dict | None:
    """ffprobe 로 잰 프록시의 실제 기하 → `{height, fps, duration_sec}` · 실패면 None.

    ⚠ 실패를 예외로 올리지 않는 것이 의도다 — 이 함수의 유일한 소비자는 '재사용해도
    되는가'이고, 못 읽는 파일은 **재사용하지 않는다**(= 다시 만든다)가 안전한 답이다.
    중단된 인코딩이 남긴 반쪽 파일이 그런 경우다."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        ffprobe = find_ffmpeg_command("ffprobe")
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height,avg_frame_rate,r_frame_rate",
             "-show_entries", "format=duration",
             "-of", "json", str(path)],
            check=True, capture_output=True).stdout
        doc = json.loads(out.decode("utf-8", "replace"))
        stream = (doc.get("streams") or [{}])[0]
        height = int(stream.get("height") or 0)
        fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(
            stream.get("r_frame_rate"))
        if height <= 0 or not fps:
            return None
        dur = float((doc.get("format") or {}).get("duration") or 0.0)
        return {"height": height, "fps": round(fps, 4), "duration_sec": round(dur, 3)}
    except Exception:  # noqa: BLE001 — 위 주석: 못 읽으면 '재사용 불가'로 답한다
        return None


def _parse_rate(rate: Any) -> float | None:
    """`"30/1"`·`"30000/1001"`·`"30"` → float. 0/0 같은 미상 표기는 None."""
    if not rate:
        return None
    s = str(rate)
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            d = float(den)
            return float(num) / d if d else None
        return float(s)
    except ValueError:
        return None


def _geometry_ok(geo: dict | None, *, height: int, file_fps: float) -> bool:
    if not geo:
        return False
    return (int(geo["height"]) == int(height)
            and abs(float(geo["fps"]) - float(file_fps)) <= FPS_TOLERANCE)


def build_proxy(video_path: Path, out_path: Path, *, height: int = PROXY_HEIGHT,
                file_fps: float = PROXY_FILE_FPS, crf: int = PROXY_CRF,
                log=print) -> tuple[Path, dict]:
    """전체 훑기용 프록시 인코딩 → `(경로, {height, file_fps, crf, bytes, elapsed_sec,
    reused, mtime, geometry})`.

    v3 `seq_analyze.build_scan_proxy` 의 ffmpeg 인자를 **기하만 바꿔** 쓴다.
    v3 와 의도적으로 다른 것 셋:

    ① **재사용 판정에 ffprobe 를 한 번 더 건다.** 이름이 기하를 들고 있어도(위 ①)
       그 이름의 파일 *내용*이 다를 수 있다 — 중단된 인코딩, 손으로 옮긴 파일.
       기하가 다르면 조용히 쓰지 않고 **사유를 찍고 다시 만든다**.
    ② **임시 파일에 쓰고 os.replace 로 바꾼다.** 인코딩 중에 죽으면 v3 는 반쪽
       파일을 남기고, 다음 실행이 그것을 '존재하므로 재사용'한다. 원자 교체면
       그 경로에는 완성본만 존재한다(`app/modules/job.py` 의 run_log 와 같은 규율).
    ③ **ffmpeg stderr 를 삼키지 않는다.** `capture_output=True` 만 있고 예외에서
       stderr 를 안 실으면 원인 추적이 불가(v3 stage4 리뷰 지적과 같은 형태).

    비용 실측치(bytes·elapsed_sec)를 반환·로그에 남기는 것은 **계약이다** —
    720p/30fps 의 크기·시간은 아직 추정이고(기획서 §12) M2 실측 라운드가 이 숫자로
    `PROXY_CRF` 를 갈아낀다.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    geo = probe_geometry(out_path)
    if out_path.exists():
        if _geometry_ok(geo, height=height, file_fps=file_fps):
            st = out_path.stat()
            log(f"  [v4/proxy] 재사용: {out_path.name} "
                f"({st.st_size / 1e6:.1f}MB · {geo['height']}p/{geo['fps']:g}fps)")
            return out_path, {"height": int(geo["height"]),
                              "file_fps": float(geo["fps"]), "crf": int(crf),
                              "bytes": st.st_size, "elapsed_sec": 0.0,
                              "reused": True, "mtime": round(st.st_mtime, 3),
                              "geometry": geo}
        # 조용한 재사용 금지 — 무엇이 어긋나서 다시 만드는지 남긴다
        log(f"  [v4/proxy] ⚠ 기하 불일치 — 다시 만든다: {out_path.name} "
            f"(파일={geo or '판독 불가'} · 요구={height}p/{file_fps:g}fps)")

    ffmpeg = find_ffmpeg_command("ffmpeg")
    # 같은 디렉토리 임시 파일 → os.replace (위 ②). 다른 디렉토리면 rename 이 원자적이지
    # 않을 수 있어(파일시스템 경계) 반드시 옆에 만든다.
    tmp_path = out_path.with_name(f".{out_path.name}.part.mp4")
    cmd = [ffmpeg, "-y", "-i", str(Path(video_path).resolve()),
           "-vf", f"scale=-2:{int(height)},fps={file_fps:g}",
           "-fps_mode", "cfr",
           "-c:v", "libx264", "-preset", PROXY_PRESET, "-crf", str(int(crf)),
           "-c:a", "aac", "-ac", str(PROXY_AUDIO_CHANNELS),
           "-ar", str(PROXY_AUDIO_RATE),
           "-threads", str(PROXY_THREADS), str(tmp_path)]
    t0 = time.time()
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            "프록시 인코딩 실패 — ffmpeg stderr 꼬리: "
            f"{(e.stderr or b'')[-400:].decode('utf-8', 'replace')}") from e
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, out_path)
    elapsed = round(time.time() - t0, 2)

    st = out_path.stat()
    made = probe_geometry(out_path)
    if not _geometry_ok(made, height=height, file_fps=file_fps):
        # 방금 만든 것이 요구 기하가 아니면 크게 실패한다 — 이 파일로 계속 가면
        # 표본 fps 예산(파일 fps 가 상한)과 실제가 갈린 채 6단계가 돈다.
        raise RuntimeError(
            f"프록시 기하가 요구와 다르다: 만든 것={made or '판독 불가'} · "
            f"요구={height}p/{file_fps:g}fps ({out_path})")
    mb = st.st_size / 1e6
    rate = (mb / elapsed * 60.0) if elapsed > 0 else 0.0
    log(f"  [v4/proxy] {out_path.name} — {mb:.1f}MB · {elapsed:.1f}s "
        f"({rate:.0f}MB/분) · crf {int(crf)} "
        f"[⚠ crf 는 480p/10fps 에서 물려받은 추정값 — M2 실측 대상]")
    return out_path, {"height": int(made["height"]), "file_fps": float(made["fps"]),
                      "crf": int(crf), "bytes": st.st_size,
                      "elapsed_sec": elapsed, "reused": False,
                      "mtime": round(st.st_mtime, 3), "geometry": made}


def proxy_fingerprint(video_path: Path, *, height: int = PROXY_HEIGHT,
                      file_fps: float = PROXY_FILE_FPS,
                      crf: int = PROXY_CRF) -> str:
    """`checkpoint_upload.json` 의 지문 — 소재 신원 + 인코딩 파라미터. 순수·결정적.

    ⚠ **시각은 재료가 아니다**(계약 §3 각주 — 결정성). 소재 신원은 파일 이름과 크기로
    본다: 3시간 원본을 sha256 으로 해싱하면 프록시를 만드는 것만큼 오래 걸리고, mtime 은
    같은 파일을 복사·재다운로드만 해도 바뀌어 멀쩡한 캐시를 버린다.
    ⚠ 지문 재료는 **부르는 쪽이 전량 명시**한다는 규약(`job.fingerprint` 독스트링)대로,
    인코딩 인자를 여기서 빠짐없이 적는다 — 하나라도 빠지면 그 값을 바꿔도 옛 프록시가
    재사용된다(v3 지문 4종이 서로 다른 재료를 봐서 각각 다른 변경을 놓쳤다 — gotcha 9).
    """
    from app.modules.job import fingerprint as _fingerprint

    src = Path(video_path)
    return _fingerprint({
        "schema": SCHEMA_UPLOAD,
        "source_name": src.name,
        "source_bytes": src.stat().st_size if src.exists() else None,
        "height": int(height),
        "file_fps": float(file_fps),
        "crf": int(crf),
        "preset": PROXY_PRESET,
        "audio": [PROXY_AUDIO_CHANNELS, PROXY_AUDIO_RATE],
    })


# ── Files API 핸들 ──────────────────────────────────────────────────────────

def handle_name_of(handle_or_id: Any) -> str:
    """핸들 객체·`files/abc`·업로드 uri 중 무엇을 받아도 `files/abc` 로 정규화. 순수.

    `files.get`/`files.delete` 는 **name** 을 받는데 체크포인트에는 uri 도 함께 남는다
    (재개는 JSON 만 들고 온다). 정규화를 한 곳에 두지 않으면 재개 경로만 조용히
    404 를 받고 '만료'로 오해한다."""
    name = getattr(handle_or_id, "name", None) or str(handle_or_id or "")
    name = name.strip()
    if not name:
        raise ValueError("Files API 핸들 이름이 비어 있다")
    if "/files/" in name:                      # https://…/v1beta/files/abc → files/abc
        name = "files/" + name.rsplit("/files/", 1)[1]
    elif not name.startswith("files/"):
        name = f"files/{name}"
    return name.split("?", 1)[0].rstrip("/")


def upload_handle(gemini, proxy: Path, *, log=print) -> tuple[Any, dict]:
    """Files API 업로드 + PROCESSING 폴링 → `(핸들, {uri, name, bytes, elapsed_sec})`.

    v3 `seq_analyze._upload_video` 를 **부른다** — 그 함수는 업로드·폴링만 하고
    삭제는 하지 않는다(삭제는 전부 호출부에 있었다. 모듈 독스트링 ②).
    지연 import 인 이유: `app.v3.seq_analyze` 는 v4 가 폐기하는 Stage 1 프롬프트·검증기를
    함께 들고 있어, 프록시만 쓰는 자리에서 그 표면을 import 시점에 끌어오지 않는다.

    ⚠ **`media_processing=STATIC` 은 여기가 아니다**(기획서 §1 행 5·§8 이 5단계 칸에
    적어 두었지만, 실제 표면은 업로드가 아니라 **요청**이다 — 파일을 첨부하는 쪽,
    즉 6·6b·8·10a 의 `generate_content` 인자다). 이 모듈은 업로드만 하므로 그 자리를
    여기서 조용히 삼키지 않고 **어디에 있어야 하는지**만 적어 둔다. SDK 핀도 같다
    (requirements 의 `google-genai` — 이 모듈의 표면이 아니다).

    ⚠ **폴링 상한은 아직 없다**(기획서 §8 '업로드 폴링 상한 — 파일 크기에 비례'는 잔여).
    상한을 걸려면 v3 함수의 while 루프를 고쳐야 하는데 v3 는 동결 표면이다. 대신 여기서
    `elapsed_sec` 를 남긴다 — 720p/30fps 업로드 시간이 아직 추정(2~3분)이라, 상한을 정할
    근거가 이 숫자다(기획서 §12).
    """
    from app.v3.seq_analyze import _upload_video

    proxy = Path(proxy)
    t0 = time.time()
    uploaded = _upload_video(gemini, proxy, log=log)
    elapsed = round(time.time() - t0, 2)
    meta = {
        "uri": getattr(uploaded, "uri", None),
        "name": handle_name_of(uploaded),
        # 서버가 알려 주는 크기가 있으면 그것을, 없으면 올린 파일의 크기를 적는다
        "bytes": int(getattr(uploaded, "size_bytes", None) or proxy.stat().st_size),
        "elapsed_sec": elapsed,
    }
    log(f"  [v4/upload] {meta['name']} — {meta['bytes'] / 1e6:.1f}MB · {elapsed:.1f}s")
    return uploaded, meta


def release_handle(gemini, handle, *, log=print) -> None:
    """서버 파일 삭제. **최종 렌더 성공 뒤** 또는 실패 시 배선의 finally 에서 부른다.

    🛑 단계 안에서 부르면 안 된다 — 6·6b·8·10a 가 같은 핸들을 공유한다(모듈 독스트링 ②).
    삭제 실패는 WARN 으로 남기고 넘어간다(v3 네 곳과 같은 규율): 이미 지워졌거나 만료된
    핸들이 흔하고, 정리 실패가 완성된 편의 발행을 막을 이유가 없다. **다만 조용히
    넘어가지는 않는다** — 지워지지 않은 파일은 다음 실행의 할당량을 먹는다.
    """
    if handle is None:
        log("  [v4/upload] 삭제 생략 — 핸들 없음(업로드 전에 죽었다)")
        return
    try:
        name = handle_name_of(handle)
    except ValueError as e:
        log(f"  [v4/upload] WARN 삭제 불가 — 핸들 이름 판독 실패: {e}")
        return
    try:
        gemini.client.files.delete(name=name)
        log(f"  [v4/upload] 서버 파일 삭제: {name}")
    except Exception as e:  # noqa: BLE001 — 정리 실패가 본편을 막지 않는다
        log(f"  [v4/upload] WARN 서버 파일 삭제 실패: {name} — {e}")


def handle_alive(gemini, uri_or_name: str) -> bool:
    """`files.get` 으로 핸들이 아직 살아 있는지 확인. 48h 만료·재개에서 쓴다.

    실패(404·권한·네트워크)는 전부 False 다 — 부르는 쪽은 **재업로드**라는 같은 조치를
    하고, 그때 `[cache] ⚠` 로 사유를 남긴다(계약 §3). 여기서 예외를 올리면 만료라는
    정상 사건에 배선이 죽는다.
    ⚠ 반대로 '살아 있다'는 판정은 서버가 ACTIVE 라고 답한 경우로 좁힌다 — PROCESSING·
    FAILED 상태의 핸들을 살아 있다고 보면 뒷단계가 쓸 수 없는 파일을 참조한다.
    """
    try:
        got = gemini.client.files.get(name=handle_name_of(uri_or_name))
    except Exception:  # noqa: BLE001 — 위 주석: 만료·부재는 정상 사건이다
        return False
    state = getattr(getattr(got, "state", None), "name", None) or getattr(
        got, "state", None)
    if state is None:
        return True         # 상태를 안 주는 구현(가짜·구 SDK)은 존재를 답으로 본다
    return str(state) == "ACTIVE"


# ── 체크포인트 ──────────────────────────────────────────────────────────────

def upload_checkpoint_doc(*, fingerprint: str, proxy_path: Path, proxy_meta: dict,
                          handle_meta: dict) -> dict:
    """`checkpoint_upload.json` 문서 — 계약 §3 의 모양 그대로. 순수.

    ⚠ 넘겨받은 dict 를 제자리에서 고치지 않는다(사본만).
    ⚠ **시각은 `Date.now()` 류를 쓰지 않는다**(계약 §3 각주 — 지문 결정성). 언제 올렸는지는
    프록시 파일의 mtime 과 각 단계의 elapsed 로만 되짚는다. 48h 만료 판정도 시계 산술이
    아니라 `handle_alive`(서버에 물어본다)가 한다 — 노드 시계가 틀어져도 옳다.
    """
    return {
        "schema": SCHEMA_UPLOAD,
        "fingerprint": fingerprint,
        "proxy": {
            "file": Path(proxy_path).name,
            "height": proxy_meta.get("height"),
            "file_fps": proxy_meta.get("file_fps"),
            "crf": proxy_meta.get("crf"),
            "bytes": proxy_meta.get("bytes"),
            "elapsed_sec": proxy_meta.get("elapsed_sec"),
            "reused": proxy_meta.get("reused"),
            "mtime": proxy_meta.get("mtime"),
        },
        "handle": {
            "uri": handle_meta.get("uri"),
            "name": handle_meta.get("name"),
            "bytes": handle_meta.get("bytes"),
            "elapsed_sec": handle_meta.get("elapsed_sec"),
        },
        "uploaded_at_note": (
            "업로드 시각은 남기지 않는다 — 지문 결정성(계약 §3). 시점은 proxy.mtime, "
            "소요는 elapsed_sec, 생존 여부는 handle_alive(files.get)로 본다."
        ),
    }
