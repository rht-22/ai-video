"""V4-M2 §3 프록시·업로드 회귀 가드 — `app/v4/proxy.py`.

이 파일이 고정하는 것은 넷이다(전부 계약 `docs/v4/M2-interfaces.md` §3 의 문장이다):

① **파일명에 기하가 박힌다** — 기하가 다르면 다른 경로가 나온다. v3 는 out_path
   존재만으로 재사용해서, 이름에 기하가 없으면 480p 잔재를 720p 실행이 조용히
   재사용한다. 그 사고를 이름 규칙 + ffprobe 실측 재확인 둘로 막는다.
② **720p/30fps 로 실제로 나온다**(운영자 결정 O1) — 합성 소재를 실제 ffmpeg 으로
   인코딩해 ffprobe 로 잰다. 인자 문자열만 보는 가드는 "scale 은 맞는데 결과가
   다르다"를 못 잡는다.
③ **핸들 수명을 이 모듈이 쥔다** — 업로드는 v3 `_upload_video`(삭제를 하지 않는
   함수)를 부르고, 삭제는 `release_handle` 한 곳이다. 네트워크는 쓰지 않는다(가짜
   클라이언트).
④ **지문에 시각이 없다** — 같은 재료면 늘 같은 지문(결정성).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from app.modules.ffmpeg_utils import find_ffmpeg_command
from app.v4 import fps as v4fps
from app.v4 import proxy


# ── 합성 소재 ───────────────────────────────────────────────────────────────

def _synth_source(tmp_path: Path, *, seconds: int = 2) -> Path:
    """testsrc2 + sine 짧은 소재. 다른 v3/v4 테스트와 같은 방식."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        [find_ffmpeg_command("ffmpeg"), "-y", "-f", "lavfi",
         "-i", f"testsrc2=size=320x240:rate=12:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    return src


# ── ① 파일명에 기하 ─────────────────────────────────────────────────────────

def test_proxy_path_carries_geometry(tmp_path):
    p = proxy.proxy_path_for(tmp_path)
    assert p.name == "scan_720p30.mp4", "계약 §3 의 이름 그대로여야 한다"
    assert p.parent == tmp_path


def test_proxy_path_differs_when_geometry_differs(tmp_path):
    """480p 잔재를 720p 실행이 재사용하지 못하게 하는 것이 이 함수의 전부다."""
    v3_like = proxy.proxy_path_for(tmp_path, height=480, file_fps=10.0)
    v4_now = proxy.proxy_path_for(tmp_path, height=720, file_fps=30.0)
    assert v3_like.name == "scan_480p10.mp4"
    assert v3_like != v4_now
    # 높이만 같고 fps 가 달라도 다른 경로다(표본 fps 의 상한이 파일 fps 이므로)
    assert proxy.proxy_path_for(tmp_path, height=720, file_fps=10.0) != v4_now


def test_proxy_path_fractional_fps_label(tmp_path):
    """29.97 같은 값도 확장자를 헷갈리게 하지 않는다."""
    assert proxy.proxy_path_for(tmp_path, file_fps=29.97).name == "scan_720p29_97.mp4"


def test_proxy_file_fps_is_not_a_second_copy():
    """파일 fps 의 정본은 `app/v4/fps.py` 하나다 — 여기서는 재수출뿐.

    두 곳에 숫자를 적으면 한쪽만 고쳐질 때 예산 계산(표본 fps ≤ 파일 fps)과 실제
    파일이 갈린다(E13 '베낀 수식' 교훈)."""
    assert proxy.PROXY_FILE_FPS is v4fps.PROXY_FILE_FPS
    assert proxy.PROXY_FILE_FPS == 30.0
    assert proxy.PROXY_HEIGHT == 720          # 운영자 결정 O1
    assert proxy.PROXY_CRF == 30              # v3 값 그대로 시작(M2 실측 대상)


# ── ② 실제로 720p/30fps 로 나온다 ───────────────────────────────────────────

def test_build_proxy_is_really_720p30(tmp_path, monkeypatch):
    src = _synth_source(tmp_path)
    out = proxy.proxy_path_for(tmp_path)

    seen: list[list[str]] = []
    real_run = subprocess.run

    def _spy(cmd, *a, **k):
        seen.append(list(cmd))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(proxy.subprocess, "run", _spy)
    path, meta = proxy.build_proxy(src, out, log=lambda *a: None)

    assert path == out and out.exists()
    geo = proxy.probe_geometry(out)
    assert geo["height"] == 720                       # O1 — v3 는 480
    assert abs(geo["fps"] - 30.0) <= proxy.FPS_TOLERANCE   # O1 — v3 는 10
    assert meta["reused"] is False
    assert meta["height"] == 720 and meta["file_fps"] == pytest.approx(30.0, abs=0.05)
    # 실측 재료(기획서 §12 '추정' 항목을 갈아낄 근거) — 값이 실제로 실린다
    assert meta["bytes"] == out.stat().st_size > 0
    assert meta["elapsed_sec"] > 0
    assert meta["mtime"] == pytest.approx(out.stat().st_mtime, abs=0.01)

    # v3 `build_scan_proxy` 와 같은 인코딩 인자 — 기하만 다르다
    ff = [c for c in seen if "-fps_mode" in c]
    assert ff, "ffmpeg 인코딩 호출이 있어야 한다"
    cmd = ff[0]
    assert "scale=-2:720,fps=30" in cmd
    for token in ("-fps_mode", "cfr", "libx264", "ultrafast", "30", "aac",
                  "-ac", "1", "-ar", "22050", "-threads", "4"):
        assert token in cmd, f"v3 와 같아야 할 인자가 없다: {token}"


def test_build_proxy_writes_atomically(tmp_path, monkeypatch):
    """인코딩 중 죽어도 그 경로에 반쪽 파일이 남지 않는다.

    v3 는 out_path 로 바로 인코딩해서, 중단되면 다음 실행이 반쪽을 '존재하므로
    재사용'했다."""
    src = _synth_source(tmp_path)
    out = proxy.proxy_path_for(tmp_path)

    def _boom(cmd, *a, **k):
        Path(cmd[-1]).write_bytes(b"\x00" * 64)       # 반쯤 쓰인 파일 흉내
        raise subprocess.CalledProcessError(1, cmd, stderr=b"synthetic failure")

    monkeypatch.setattr(proxy.subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        proxy.build_proxy(src, out, log=lambda *a: None)
    assert not out.exists(), "실패한 인코딩이 프록시 경로에 남으면 안 된다"
    assert not list(tmp_path.glob(".*.part.mp4")), "임시 파일도 안 남는다"


# ── ③ 재사용 판정 ───────────────────────────────────────────────────────────

def test_build_proxy_reuses_matching_file(tmp_path):
    src = _synth_source(tmp_path)
    out = proxy.proxy_path_for(tmp_path)
    _p, first = proxy.build_proxy(src, out, log=lambda *a: None)
    stat_before = out.stat()
    time.sleep(0.01)
    _p2, second = proxy.build_proxy(src, out, log=lambda *a: None)
    assert second["reused"] is True
    assert second["elapsed_sec"] == 0.0
    assert second["bytes"] == first["bytes"]
    assert out.stat().st_mtime == stat_before.st_mtime, "재사용이면 다시 안 쓴다"


def test_build_proxy_rebuilds_on_geometry_mismatch(tmp_path):
    """🛑 이름은 720p 인데 내용이 480p/10fps 인 파일 — v3 라면 그대로 썼다."""
    src = _synth_source(tmp_path)
    out = proxy.proxy_path_for(tmp_path)          # scan_720p30.mp4
    subprocess.run(                                # v3 기하로 그 이름에 심어 둔다
        [find_ffmpeg_command("ffmpeg"), "-y", "-i", str(src),
         "-vf", "scale=-2:480,fps=10", "-fps_mode", "cfr",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
         "-c:a", "aac", "-ac", "1", "-ar", "22050", str(out)],
        check=True, capture_output=True)
    assert proxy.probe_geometry(out)["height"] == 480

    logs: list[str] = []
    _p, meta = proxy.build_proxy(src, out, log=logs.append)
    assert meta["reused"] is False, "기하가 다르면 다시 만든다"
    assert meta["height"] == 720
    assert proxy.probe_geometry(out)["height"] == 720
    assert any("기하 불일치" in m for m in logs), "조용히 다시 만들지 않는다(사유 기록)"


def test_build_proxy_rebuilds_on_unreadable_file(tmp_path):
    """중단된 인코딩이 남긴 판독 불가 파일도 재사용하지 않는다."""
    src = _synth_source(tmp_path)
    out = proxy.proxy_path_for(tmp_path)
    out.write_bytes(b"not a video")
    assert proxy.probe_geometry(out) is None
    _p, meta = proxy.build_proxy(src, out, log=lambda *a: None)
    assert meta["reused"] is False and meta["height"] == 720


def test_probe_geometry_missing_file_is_none(tmp_path):
    assert proxy.probe_geometry(tmp_path / "none.mp4") is None
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert proxy.probe_geometry(empty) is None


@pytest.mark.parametrize("rate,expect", [
    ("30/1", 30.0), ("30000/1001", pytest.approx(29.97, abs=0.01)),
    ("25", 25.0), ("0/0", None), ("", None), (None, None), ("bad", None),
])
def test_parse_rate(rate, expect):
    got = proxy._parse_rate(rate)
    if expect is None:
        assert got is None
    else:
        assert got == expect


# ── ④ 지문 — 시각이 재료가 아니다 ──────────────────────────────────────────

def test_fingerprint_is_deterministic_and_time_free(tmp_path):
    src = _synth_source(tmp_path)
    a = proxy.proxy_fingerprint(src)
    time.sleep(0.01)
    Path(src).touch()                              # mtime 만 바뀐다
    assert proxy.proxy_fingerprint(src) == a, "시각이 지문에 들어가면 안 된다(결정성)"


def test_fingerprint_changes_with_encoding_params(tmp_path):
    src = _synth_source(tmp_path)
    base = proxy.proxy_fingerprint(src)
    assert proxy.proxy_fingerprint(src, height=480) != base
    assert proxy.proxy_fingerprint(src, file_fps=10.0) != base
    assert proxy.proxy_fingerprint(src, crf=28) != base


def test_fingerprint_changes_with_source(tmp_path):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    s1 = _synth_source(d1, seconds=2)
    s2 = _synth_source(d2, seconds=3)              # 길이가 다르면 크기가 다르다
    assert proxy.proxy_fingerprint(s1) != proxy.proxy_fingerprint(s2)


# ── 가짜 Gemini ─────────────────────────────────────────────────────────────

class _State:
    def __init__(self, name):
        self.name = name


class _File:
    def __init__(self, name="files/abc123", uri=None, state="ACTIVE", size=None):
        self.name = name
        self.uri = uri or f"https://generativelanguage.googleapis.com/v1beta/{name}"
        self.state = _State(state)
        if size is not None:
            self.size_bytes = size


class _Files:
    def __init__(self, *, upload_result=None, get_result=None, get_raises=None,
                 delete_raises=None):
        self.upload_result = upload_result or _File()
        self.get_result = get_result
        self.get_raises = get_raises
        self.delete_raises = delete_raises
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.got: list[str] = []

    def upload(self, *, file):
        self.uploaded.append(file)
        return self.upload_result

    def get(self, *, name):
        self.got.append(name)
        if self.get_raises:
            raise self.get_raises
        return self.get_result or self.upload_result

    def delete(self, *, name):
        self.deleted.append(name)
        if self.delete_raises:
            raise self.delete_raises


class _Client:
    def __init__(self, files):
        self.files = files


class _Config:
    max_retries = 2


class _Gemini:
    def __init__(self, files):
        self.client = _Client(files)
        self.config = _Config()


# ── ③ 핸들 수명 ────────────────────────────────────────────────────────────

def test_v3_upload_helper_does_not_delete():
    """계약이 기대는 사실을 못박는다 — `_upload_video` 는 삭제하지 않는다.

    삭제는 v3 의 **호출부** 네 곳에 있다(seq_analyze:546 · chunk_analyze:658 ·
    refine:338 · stage4:488). v4 가 그 함수를 그대로 부를 수 있는 근거이므로,
    누가 그 함수 안에 정리 코드를 넣으면 여기서 잡힌다."""
    import inspect

    from app.v3.seq_analyze import _upload_video
    assert "files.delete" not in inspect.getsource(_upload_video)


def test_upload_handle_returns_meta(tmp_path):
    proxy_file = tmp_path / "scan_720p30.mp4"
    proxy_file.write_bytes(b"x" * 4096)
    files = _Files(upload_result=_File(name="files/zz", state="ACTIVE"))
    handle, meta = proxy.upload_handle(_Gemini(files), proxy_file, log=lambda *a: None)

    assert handle.name == "files/zz"
    assert files.uploaded == [str(proxy_file)]
    assert files.deleted == [], "🛑 업로드 단계는 절대 삭제하지 않는다(핸들 공유)"
    assert meta["name"] == "files/zz"
    assert meta["uri"].endswith("files/zz")
    assert meta["bytes"] == 4096                   # 서버가 안 알려주면 파일 크기
    assert meta["elapsed_sec"] >= 0


def test_upload_handle_prefers_server_size(tmp_path):
    proxy_file = tmp_path / "scan_720p30.mp4"
    proxy_file.write_bytes(b"x" * 10)
    files = _Files(upload_result=_File(size=999))
    _h, meta = proxy.upload_handle(_Gemini(files), proxy_file, log=lambda *a: None)
    assert meta["bytes"] == 999


@pytest.mark.parametrize("given,expect", [
    ("files/abc", "files/abc"),
    ("abc", "files/abc"),
    ("https://generativelanguage.googleapis.com/v1beta/files/abc", "files/abc"),
    ("https://host/v1beta/files/abc?alt=media", "files/abc"),
])
def test_handle_name_normalization(given, expect):
    assert proxy.handle_name_of(given) == expect


def test_handle_name_of_object_and_empty():
    assert proxy.handle_name_of(_File(name="files/qq")) == "files/qq"
    with pytest.raises(ValueError):
        proxy.handle_name_of("")


def test_release_handle_deletes_once():
    files = _Files()
    logs: list[str] = []
    proxy.release_handle(_Gemini(files), _File(name="files/abc"), log=logs.append)
    assert files.deleted == ["files/abc"]
    assert any("삭제" in m for m in logs)


def test_release_handle_survives_failure_but_records_it():
    """정리 실패가 완성된 편의 발행을 막지 않는다 — 다만 조용하지도 않다."""
    files = _Files(delete_raises=RuntimeError("boom"))
    logs: list[str] = []
    proxy.release_handle(_Gemini(files), "files/abc", log=logs.append)
    assert any("WARN" in m and "boom" in m for m in logs)


def test_release_handle_with_no_handle_is_recorded():
    logs: list[str] = []
    proxy.release_handle(_Gemini(_Files()), None, log=logs.append)
    assert logs and "핸들 없음" in logs[0]


def test_handle_alive_true_only_for_active():
    files = _Files(get_result=_File(state="ACTIVE"))
    assert proxy.handle_alive(_Gemini(files), "files/abc") is True
    assert files.got == ["files/abc"], "uri 든 name 이든 name 으로 정규화해 묻는다"

    processing = _Files(get_result=_File(state="PROCESSING"))
    assert proxy.handle_alive(_Gemini(processing), "files/abc") is False


def test_handle_alive_false_on_lookup_failure():
    """48h 만료·404 는 정상 사건이다 — 예외가 아니라 False 로 답한다(재업로드 신호)."""
    files = _Files(get_raises=RuntimeError("404 not found"))
    assert proxy.handle_alive(_Gemini(files), "files/gone") is False
    # uri 로 물어도 같은 결과 · 정규화된 이름으로 조회한다
    files2 = _Files(get_result=_File(state="ACTIVE"))
    assert proxy.handle_alive(
        _Gemini(files2), "https://host/v1beta/files/abc") is True
    assert files2.got == ["files/abc"]


# ── 체크포인트 ──────────────────────────────────────────────────────────────

def test_upload_checkpoint_doc_shape(tmp_path):
    proxy_meta = {"height": 720, "file_fps": 30.0, "crf": 30, "bytes": 123,
                  "elapsed_sec": 4.2, "reused": False, "mtime": 1_700_000_000.5,
                  "geometry": {"height": 720, "fps": 30.0, "duration_sec": 2.0}}
    handle_meta = {"uri": "https://host/v1beta/files/abc", "name": "files/abc",
                   "bytes": 123, "elapsed_sec": 9.9}
    doc = proxy.upload_checkpoint_doc(
        fingerprint="deadbeefdeadbeef",
        proxy_path=tmp_path / "scan_720p30.mp4",
        proxy_meta=proxy_meta, handle_meta=handle_meta)

    assert doc["schema"] == "v4_upload/v1"
    assert doc["fingerprint"] == "deadbeefdeadbeef"
    assert doc["proxy"]["file"] == "scan_720p30.mp4"
    assert doc["proxy"]["height"] == 720 and doc["proxy"]["file_fps"] == 30.0
    assert doc["proxy"]["mtime"] == 1_700_000_000.5
    assert doc["handle"] == {"uri": handle_meta["uri"], "name": "files/abc",
                             "bytes": 123, "elapsed_sec": 9.9}
    assert "uploaded_at_note" in doc
    # 순수 — 넘겨받은 dict 를 제자리에서 고치지 않는다
    assert proxy_meta["geometry"]["height"] == 720 and "file" not in proxy_meta
    # 직렬화 가능해야 체크포인트로 쓸 수 있다
    json.loads(json.dumps(doc))


def test_checkpoint_name_is_the_contract_name():
    assert proxy.CHECKPOINT_UPLOAD_NAME == "checkpoint_upload.json"
