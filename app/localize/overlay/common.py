"""공용 유틸 — 설정/시크릿 로딩, 경로, 로깅, ffmpeg 헬퍼.

설계 원칙:
- 무거운 의존성(numpy/cv2/torch/yaml/anthropic ...)은 **함수 안에서 lazy import**.
  → 의존성 미설치 환경에서도 모듈 import 와 --help, 순수 로직 테스트가 동작한다.
- ffmpeg 는 시스템 CLI(subprocess)로 호출(가장 견고). 설치 필요: `ffmpeg`, `ffprobe`.
  바이너리는 `FFMPEG_BIN`/`FFPROBE_BIN` 환경변수(.env 가능)로 교체 가능 — 자막 번인은
  libass 포함 빌드가 필요하다(`ffmpeg_bin`/`has_ass_filter` 참고).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

# ── 경로 ───────────────────────────────────────────────────────────────────
# ⚠ vlp 는 자기 레포 루트였다. ai-video 에서는 **레포 루트**다(app/localize/overlay → ../../..).
# config 의 상대경로(outputs·weights·fonts·persona…)가 전부 이 기준으로 풀린다 —
# 값은 data/pipeline.config.yaml 에서 ai-video 위치로 다시 적었고, 이 수식만 바꾼다.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def resolve_path(p: str | os.PathLike, base: Path = PROJECT_ROOT) -> Path:
    """상대경로면 base(기본=프로젝트 루트) 기준으로 절대화."""
    path = Path(p)
    return path if path.is_absolute() else (base / path)


def ensure_dir(p: str | os.PathLike) -> Path:
    d = Path(p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_text(p: str | os.PathLike) -> str:
    return Path(p).read_text(encoding="utf-8")


# ── 로깅 ───────────────────────────────────────────────────────────────────
def get_logger(name: str = "loopyjp") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(h)
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger


# ── 시크릿 / .env ──────────────────────────────────────────────────────────
def load_env(env_path: Optional[str | os.PathLike] = None) -> None:
    """.env 를 환경에 로드. python-dotenv 있으면 사용, 없으면 KEY=VALUE 수동 파싱."""
    path = Path(env_path) if env_path else (PROJECT_ROOT / ".env")
    try:  # pragma: no cover - 의존성 분기
        from dotenv import load_dotenv

        load_dotenv(path if path.exists() else None)
        return
    except Exception:
        pass
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def get_secret(name: str, *fallbacks: str, required: bool = False) -> Optional[str]:
    """환경변수에서 시크릿 조회. 여러 이름 폴백 지원."""
    load_env()
    for key in (name, *fallbacks):
        val = os.environ.get(key)
        if val:
            return val
    if required:
        raise RuntimeError(
            f"시크릿 '{name}' 미설정. .env 에 추가하세요 (.env.example 참고)."
        )
    return None


# ── 설정 ───────────────────────────────────────────────────────────────────
def load_config(path: Optional[str | os.PathLike] = None) -> dict[str, Any]:
    """pipeline.config.yaml 로드 → dict. (pyyaml 필요)"""
    cfg_path = resolve_path(path) if path else (Path(__file__).resolve().parent / "data" / "pipeline.config.yaml")
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise ImportError("pyyaml 필요: pip install pyyaml") from e
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_yaml(path: str | os.PathLike) -> Any:
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise ImportError("pyyaml 필요: pip install pyyaml") from e
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_persona(config: dict[str, Any]) -> str:
    """persona.md 본문 반환(LLM system 주입용)."""
    p = resolve_path(config.get("paths", {}).get("persona", "config/persona.md"))
    return read_text(p) if p.exists() else ""


def load_glossary(config: dict[str, Any]) -> dict[str, str]:
    """glossary.yaml 의 terms 맵 반환(없으면 빈 dict)."""
    rel = config.get("paths", {}).get("glossary")
    if not rel:
        return {}
    p = resolve_path(rel)
    if not p.exists():
        return {}
    data = load_yaml(p) or {}
    return data.get("terms", {}) or {}


# ── JSON IO ────────────────────────────────────────────────────────────────
def write_json(obj: Any, path: str | os.PathLike) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_json(path: str | os.PathLike) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── ffmpeg / ffprobe (subprocess) ───────────────────────────────────────────
def ffmpeg_bin() -> str:
    """ffmpeg 실행 파일 — FFMPEG_BIN(환경변수/.env)으로 빌드 지정, 없으면 PATH 의 ffmpeg.

    PATH 의 ffmpeg 가 libass 없이 빌드된 머신(예: homebrew ffmpeg 8)은 ass 필터가
    없어 자막 번인이 실패한다 → FFMPEG_BIN 으로 libass 포함 빌드를 가리킨다.
    """
    load_env()
    return os.environ.get("FFMPEG_BIN") or "ffmpeg"


def ffprobe_bin() -> str:
    """ffprobe — FFPROBE_BIN 우선, 없으면 FFMPEG_BIN 옆의 ffprobe, 최후 PATH."""
    load_env()
    explicit = os.environ.get("FFPROBE_BIN")
    if explicit:
        return explicit
    fb = os.environ.get("FFMPEG_BIN")
    if fb:
        sibling = Path(fb).parent / "ffprobe"
        if sibling.is_file():
            return str(sibling)
    return "ffprobe"


def has_ffmpeg() -> bool:
    from shutil import which

    return which(ffmpeg_bin()) is not None and which(ffprobe_bin()) is not None


_ASS_FILTER_CACHE: dict[str, bool] = {}


def has_ass_filter(ffmpeg: Optional[str] = None) -> bool:
    """해당 ffmpeg 빌드에 ass(libass) 필터가 있는가 — 자막 번인 가능 여부.

    libass 없이 빌드된 ffmpeg 는 `-h filter=ass` 에 "Unknown filter" 를 내면서도
    종료코드 0 을 반환하므로 출력 문자열로 판별한다.
    """
    b = ffmpeg or ffmpeg_bin()
    if b not in _ASS_FILTER_CACHE:
        try:
            r = subprocess.run([b, "-hide_banner", "-h", "filter=ass"],
                               capture_output=True, text=True)
            _ASS_FILTER_CACHE[b] = "Filter ass" in (r.stdout + r.stderr)
        except OSError:
            _ASS_FILTER_CACHE[b] = False
    return _ASS_FILTER_CACHE[b]


def ass_filter_hint(ffmpeg: str) -> str:
    """ass 필터 부재 시 사용자 안내 문구(에러 메시지 공용)."""
    return (f"ffmpeg('{ffmpeg}') 빌드에 ass 필터(libass) 없음 → 자막 번인 불가. "
            "libass 포함 빌드를 FFMPEG_BIN 환경변수(.env 가능)로 지정하세요 "
            "(예: FFMPEG_BIN=/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg).")


def _run(cmd: list[str], quiet: bool = True) -> None:
    get_logger().debug("run: %s", " ".join(cmd))
    subprocess.run(
        cmd,
        check=True,
        stdout=(subprocess.DEVNULL if quiet else None),
        stderr=(subprocess.DEVNULL if quiet else None),
    )


def probe(video: str | os.PathLike) -> dict[str, Any]:
    """ffprobe 로 영상 메타 추출 → {width,height,fps,nb_frames,duration}."""
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(video)],
        check=True, capture_output=True, text=True,
    ).stdout
    info = json.loads(out)
    vstream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})

    def _fps(s: dict) -> float:
        rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
        num, _, den = rate.partition("/")
        den_f = float(den) if den else 1.0
        return (float(num) / den_f) if den_f else 0.0

    nb = vstream.get("nb_frames")
    try:   # ffprobe 가 duration 을 "N/A" 로 줄 수 있어 falsy 가드만으론 부족
        duration = float(info.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "width": int(vstream.get("width", 0)),
        "height": int(vstream.get("height", 0)),
        "fps": round(_fps(vstream), 6),
        "nb_frames": int(nb) if nb and str(nb).isdigit() else None,
        "duration": duration,
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
    }


def extract_frames(video: str | os.PathLike, out_dir: str | os.PathLike,
                   pattern: str = "%06d.png", start_number: int = 0) -> Path:
    """모든 프레임을 무손실 PNG 시퀀스로 추출. 반환: out_dir."""
    out = ensure_dir(out_dir)
    _run([ffmpeg_bin(), "-y", "-i", str(video), "-start_number", str(start_number),
          "-vsync", "0", str(out / pattern)])
    return out


def extract_audio(video: str | os.PathLike, out_wav: str | os.PathLike) -> Optional[Path]:
    """오디오 트랙 분리(없으면 None)."""
    if not probe(video).get("has_audio"):
        return None
    out = Path(out_wav)
    ensure_dir(out.parent)
    _run([ffmpeg_bin(), "-y", "-i", str(video), "-vn", "-acodec", "pcm_s16le", str(out)])
    return out


def frames_to_video(frames_dir: str | os.PathLike, out: str | os.PathLike, fps: float,
                    codec: str = "ffv1", pattern: str = "%06d.png",
                    pix_fmt: str = "yuv420p", crf: Optional[int] = None,
                    start_number: int = 0) -> Path:
    """프레임 시퀀스 → 영상. ffv1=무손실 중간본, libx264/265/av1=최종."""
    out = Path(out)
    ensure_dir(out.parent)
    cmd = [ffmpeg_bin(), "-y", "-framerate", str(fps), "-start_number", str(start_number),
           "-i", str(Path(frames_dir) / pattern), "-c:v", codec]
    if crf is not None and codec != "ffv1":
        cmd += ["-crf", str(crf)]
    if codec != "ffv1":
        cmd += ["-pix_fmt", pix_fmt, "-movflags", "+faststart"]  # 플레이어 호환(moov 앞으로)
    cmd += [str(out)]
    _run(cmd)
    return out


def mux_audio(video: str | os.PathLike, audio: Optional[str | os.PathLike],
              out: str | os.PathLike) -> Path:
    """영상에 원본 오디오 merge(오디오 None 이면 영상만 복사)."""
    out = Path(out)
    ensure_dir(out.parent)
    if audio is None:
        _run([ffmpeg_bin(), "-y", "-i", str(video), "-c", "copy",
              "-movflags", "+faststart", str(out)])
        return out
    _run([ffmpeg_bin(), "-y", "-i", str(video), "-i", str(audio),
          "-c:v", "copy", "-c:a", "aac", "-shortest", "-map", "0:v:0", "-map", "1:a:0",
          "-movflags", "+faststart", str(out)])
    return out


def mux_dub(video: str | os.PathLike, dub_audio: str | os.PathLike,
            out: str | os.PathLike, bg_volume: float = 0.3,
            voice_volume: float = 1.8,
            bg_audio: Optional[str | os.PathLike] = None,
            loudnorm: bool = True, limiter: bool = False,
            limit: float = 0.95) -> Path:
    """배경 오디오(낮춤=더킹) + 더빙 보이스 믹스 → 영상에 입힘(faststart).

    bg_audio=None: 영상의 원본 오디오를 배경으로 사용.
    bg_audio 지정: 원본 대신 그 트랙(예: 보컬 제거 스템)을 배경으로 → 원본 목소리 제거.
    bg_volume=0 이면 배경 완전 제거(더빙만). ASMR 등은 bg_volume 을 높여 원음 보존.
    loudnorm=True: 최종 라우드니스 정규화(EBU R128, -16 LUFS — 플랫폼 표준에 근접).
    limiter=True(loudnorm=False 일 때): 브릭월 리미터(alimiter)로 피크만 제한 →
        ASMR 다이내믹은 보존하면서 합산 클리핑(째짐) 방지. limit=최대 진폭(0~1).
    """
    out = Path(out)
    ensure_dir(out.parent)
    if bg_audio is not None:
        cmd = [ffmpeg_bin(), "-y", "-i", str(video), "-i", str(bg_audio), "-i", str(dub_audio)]
        pre = (f"[1:a]volume={bg_volume}[bg];[2:a]volume={voice_volume}[voc];"
               f"[bg][voc]amix=inputs=2:duration=first:normalize=0")
    else:
        cmd = [ffmpeg_bin(), "-y", "-i", str(video), "-i", str(dub_audio)]
        pre = (f"[0:a]volume={bg_volume}[bg];[1:a]volume={voice_volume}[voc];"
               f"[bg][voc]amix=inputs=2:duration=first:normalize=0")
    if loudnorm:
        post = "[mix];[mix]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    elif limiter:
        post = f"[mix];[mix]alimiter=limit={limit}[a]"   # 피크만 제한, 다이내믹 보존
    else:
        post = "[a]"
    filt = pre + post
    cmd += ["-filter_complex", filt, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(out)]
    _run(cmd)
    return out


def cut_video(video: str | os.PathLike, out: str | os.PathLike,
              cuts: list[dict[str, float]], crf: int = 18,
              pix_fmt: str = "yuv420p") -> Path:
    """cuts 구간을 영상에서 들어낸다(앞뒤가 이어 붙음) — E9 구간 잘라내기.

    비디오+오디오 함께 trim → concat. 프레임 정확도가 필요해 재인코딩 경로(발주 허용).
    cuts 는 engine.cuts.validate_cuts 를 통과한 값(정렬·비겹침) 전제.
    """
    from app.localize.overlay.cuts import keep_segments

    out = Path(out)
    ensure_dir(out.parent)
    meta = probe(video)
    keeps = keep_segments(cuts, float(meta["duration"]))
    if not keeps:
        raise ValueError("cuts 적용 결과 남는 구간이 없습니다")
    has_audio = bool(meta.get("has_audio"))
    parts: list[str] = []
    for i, (s, e) in enumerate(keeps):
        span = f"start={s:.6f}" + (f":end={e:.6f}" if e is not None else "")
        parts.append(f"[0:v]trim={span},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            parts.append(f"[0:a]atrim={span},asetpts=PTS-STARTPTS[a{i}]")
    pads = "".join(f"[v{i}][a{i}]" if has_audio else f"[v{i}]" for i in range(len(keeps)))
    parts.append(f"{pads}concat=n={len(keeps)}:v=1:a={1 if has_audio else 0}"
                 + ("[v][a]" if has_audio else "[v]"))
    cmd = [ffmpeg_bin(), "-y", "-i", str(video), "-filter_complex", ";".join(parts),
           "-map", "[v]", "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", pix_fmt]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac"]
    cmd += ["-movflags", "+faststart", str(out)]
    _run(cmd)
    return out


def burn_subtitles(video: str | os.PathLike, ass_path: str | os.PathLike,
                   out: str | os.PathLike, fonts_dir: Optional[str | os.PathLike] = None,
                   crf: int = 18, pix_fmt: str = "yuv420p") -> Path:
    """ASS 자막을 영상에 번인(원본 영상 위에 덧입힘). 오디오는 그대로 복사, faststart.

    원본 한국어 자막은 화면에 남아 있고, ASS 의 일본어가 그 위/아래에 추가로 렌더된다.
    """
    out = Path(out)
    ensure_dir(out.parent)
    af = f"ass={ass_path}"
    if fonts_dir:
        af += f":fontsdir={fonts_dir}"
    ffmpeg = ffmpeg_bin()
    try:
        _run([ffmpeg, "-y", "-i", str(video), "-vf", af,
              "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", pix_fmt,
              "-c:a", "copy", "-movflags", "+faststart", str(out)])
    except subprocess.CalledProcessError:
        if not has_ass_filter(ffmpeg):
            raise RuntimeError(ass_filter_hint(ffmpeg)) from None
        raise
    return out


# ── 텍스트 비교(백체크 공용) ─────────────────────────────────────────────
def levenshtein(a: str, b: str) -> int:
    """편집거리(순수, 2행 DP) — CER 용."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    """문자 오류율 = 편집거리/len(ref). ref 가 비면 hyp 유무로 0/1."""
    if not ref:
        return 0.0 if not hyp else 1.0
    return levenshtein(ref, hyp) / len(ref)


def norm_text(text: str) -> str:
    """표기 비교용 경량 정규화 — NFKC + 기호/공백 제거 + 소문자화(발음 변환 없음)."""
    import re
    import unicodedata
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text or "")).lower()
