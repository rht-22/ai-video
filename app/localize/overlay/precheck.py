"""레벨 실측 판별(pre-check) — 처리 전에 영상을 열어 무엇이 있는지 '실측'한다.

제목 추정(LLM level_guess)의 한계 보완: 번인 자막 없는 Short 에 Level B 를 돌리면
OCR 노이즈 오검출로 영상이 망가진다(2026-07-01 loopy_short 사례). 그래서:

  프레임 샘플 OCR → 번인 한국어 자막 실측  +  오디오 ASR → 대사 유무 실측
  → 라우팅: 번인 있음=B(캡션 교체) / 번인 없음+대사 있음=C(더빙 플로우)
            / 둘 다 없음=A(영상 무변환, 메타데이터만)

무거운 의존(paddleocr·faster-whisper)은 lazy import — 판정 규칙은 순수 함수로 분리.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile


from typing import Any  # noqa: E402

from app.localize.overlay.common import ffmpeg_bin, get_logger, write_json, resolve_path, ensure_dir  # noqa: E402

log = get_logger("precheck")

_HANGUL_RE = re.compile(r"[가-힣]")


# ── 순수: 판정 규칙 ───────────────────────────────────────────────────────
def hangul_chars(text: str) -> int:
    return len(_HANGUL_RE.findall(text or ""))


def solid_hit_frames(frames: list[dict[str, Any]], min_conf: float, min_hangul: int) -> int:
    """'진짜 번인 텍스트'로 보이는 검출이 있는 프레임 수.

    노이즈 필터: 신뢰도 min_conf 이상 AND 한글 min_hangul 자 이상인 region 만 인정."""
    hits = 0
    for f in frames:
        if any(float(r.get("confidence", 0)) >= min_conf
               and hangul_chars(r.get("text", "")) >= min_hangul
               for r in f.get("regions", [])):
            hits += 1
    return hits


def decide_route(burn_frames: int, dialogue_segs: int, min_persist: int) -> str:
    """실측 → 라우트. 번인+대사=BC(캡션 제거+더빙), 번인만=B, 대사만=C(더빙), 없으면 A.

    BC 는 먹방류(화면 캡션 + 나레이션) 대응 — B 만 돌리면 더빙이 없고, C 만 돌리면
    한국어 캡션 위에 일본어 자막이 겹친다(2026-07-09 데모 이중자막 사고)."""
    if burn_frames >= min_persist:
        return "BC" if dialogue_segs >= 1 else "B"
    if dialogue_segs >= 1:
        return "C"
    return "A"


def caption_margin_v(frames: list[dict[str, Any]], height: int,
                     min_conf: float, min_hangul: int, pad: int = 16,
                     default: int = 30, max_frac: float = 0.45,
                     bottom_frac: float = 0.70) -> int:
    """일본어 자막의 하단 마진(MarginV) — 한국어 하단 캡션과 겹치지 않게 그 '바로 위'.

    사용자 결정(2026-07-10): 캡션은 제거하지 않고 공존 — 자막 위치만 회피.
    하단 밴드(중심이 화면 bottom_frac 아래)의 '진짜 캡션'(신뢰도·한글 필터)만 기준 —
    중간 화면 카드에 끌려 자막이 화면 중앙까지 올라가는 것 방지(max_frac 클램프)."""
    tops: list[float] = []
    for f in frames:
        for r in f.get("regions", []):
            bbox = r.get("bbox")
            if not bbox or float(r.get("confidence", 0)) < min_conf:
                continue
            if hangul_chars(r.get("text", "")) < min_hangul:
                continue
            y1, y2 = float(bbox[1]), float(bbox[3])
            if (y1 + y2) / 2 < height * bottom_frac:   # 하단 밴드 아님(중간 카드 등)
                continue
            tops.append(y1)
    if not tops:
        return default
    # 중앙값 — 일회성 카드(한 프레임만 높이 뜨는 자막)가 전체 배치를 끌어올리지 않게
    # (실측: min 기준이면 카드 y754 에 끌려 자막이 화면 중앙까지 상승, median=972 가 적정)
    import statistics
    band_top = statistics.median(tops)
    return min(int(height - band_top) + pad, int(height * max_frac))


def foreign_audio(detected_lang: str | None, lang_prob: float,
                  expected: str = "ko", min_prob: float = 0.8) -> bool:
    """언어 자동감지 게이트 — 오디오가 확실히 비한국어면 True(대사 0 처리).

    음악만 있는 쇼츠에 Whisper ko 강제 인식을 걸면 '구독과 좋아요' 류 문구를
    지어내고 no_speech/logprob 필터도 통과한다(실측 2026-07-13 cvE0wNNrju4:
    no_speech 0.372, logprob -1.052 로 통과 — 자동감지는 ja 0.948).
    감지 언어가 expected 가 아니고 확신이 min_prob 이상일 때만 차단 —
    확신 낮은 오감지는 기존 세그먼트 필터에 맡긴다."""
    if not detected_lang or detected_lang == expected:
        return False
    return lang_prob >= min_prob


def count_dialogue_segs(segs: list[dict[str, Any]], duration: float,
                        min_hangul: int = 2, max_span_frac: float = 0.9) -> int:
    """한국어 대사 세그먼트 수 — 한글 min_hangul자 이상, 클립 전체를 덮는 세그 제외.

    클립 duration 의 max_span_frac 초과를 혼자 덮는 세그먼트는 음악 할루시네이션
    패턴(실측 cvE0wNNrju4: 1세그가 클립 전체를 덮음) — 실제 대사는 그렇게 안 나온다.
    duration 을 모르면(≤0) 스팬 필터는 건너뛴다."""
    n = 0
    for s in segs:
        if hangul_chars(s.get("text", "")) < min_hangul:
            continue
        span = float(s.get("end", 0)) - float(s.get("start", 0))
        if duration > 0 and span > duration * max_span_frac:
            continue
        n += 1
    return n


def ensure_korean_capable(actual_backend: str, requested: str) -> None:
    """OCR 폴백 가드 — 한국어 못 읽는 백엔드로 조용히 폴백되면 번인 판정이
    항상 0 이 되어 라우트가 뒤집힌다(B→C/A). 판정을 계속하느니 실패가 낫다."""
    if actual_backend == requested:
        return
    if actual_backend == "rapidocr":   # 기본 모델 한국어 인식 불가(engine/detect 경고와 동일 근거)
        raise RuntimeError(
            f"OCR 백엔드 '{requested}' 초기화 실패 → '{actual_backend}' 폴백은 한국어 인식 불가 — "
            "번인 판정 불가능이라 precheck 를 중단합니다. paddleocr 설치/모델 확인 필요.")


# ── 실측 (lazy 의존) ─────────────────────────────────────────────────────
def _sample_frames(video: str, n: int, tmp: pathlib.Path) -> list[pathlib.Path]:
    """영상에서 n 장 균등 샘플 추출(ffmpeg)."""
    from app.localize.overlay.common import probe
    dur = float(probe(video).get("duration", 0.0) or 0.0)
    outs = []
    for i in range(n):
        t = dur * (i + 0.5) / n if dur > 0 else 0
        fp = tmp / f"pc_{i:03d}.png"
        subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", video,
                        "-frames:v", "1", str(fp)], check=True)
        if fp.exists():
            outs.append(fp)
    return outs


def _ocr_probe(video: str, config: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """샘플 프레임 OCR → (프레임 dict 목록, 실사용 백엔드명).

    요청 백엔드가 한국어 불가 백엔드로 조용히 폴백되면 중단(ensure_korean_capable)."""
    import cv2
    from app.localize.overlay.detect import make_ocr, _ocr_scaled

    pc = config.get("autopilot", {}).get("precheck", {})
    n = int(pc.get("frames", 8))
    dcfg = config.get("detect", {})
    requested = dcfg.get("ocr_backend", "paddleocr")
    ocr = make_ocr(requested, dcfg.get("languages", ["korean", "en"]),
                   paddle_opts={"det_model": dcfg.get("paddle_det_model"),
                                "rec_model": dcfg.get("paddle_rec_model")})
    ensure_korean_capable(ocr.name, requested)
    down = int(dcfg.get("ocr_downscale_width", 0))
    frames = []
    with tempfile.TemporaryDirectory() as td:
        for i, fp in enumerate(_sample_frames(video, n, pathlib.Path(td))):
            img = cv2.imread(str(fp))
            if img is None:
                continue
            regions = [{"text": t, "confidence": float(c), "bbox": list(b)}
                       for (b, t, c) in _ocr_scaled(ocr, img, down)]
            frames.append({"frame_idx": i, "regions": regions})
    return frames, ocr.name


def _asr_probe(video: str, config: dict[str, Any], duration: float,
               min_hangul: int) -> dict[str, Any]:
    """한국어 대사 실측 → {dialogue_segs, asr_lang, asr_lang_prob}.

    판정과 실행의 기준 일치: 모델·필터를 실제 더빙 플로우(src/dub.transcribe)와 공유 —
    여기서 대사로 세면 더빙도 그 대사를 쓴다. 단 강제 ko 인식 전에 자동 언어감지를
    먼저 돌려 확실히 비한국어인 오디오는 대사 0 처리(foreign_audio 참고)."""
    from app.localize.overlay.dub import detect_audio_language, transcribe

    dconf = config.get("dub", {})
    lang, prob = None, 0.0
    if bool(dconf.get("asr_lang_gate", True)):
        lang, prob = detect_audio_language(video, config)
        if foreign_audio(lang, prob,
                         expected=str(dconf.get("asr_expected_lang", "ko")),
                         min_prob=float(dconf.get("asr_foreign_min_prob", 0.8))):
            log.info("ASR 언어 게이트: 자동감지 %s(%.3f) — 비한국어 오디오, 대사 0 처리",
                     lang, prob)
            return {"dialogue_segs": 0, "asr_lang": lang, "asr_lang_prob": prob}

    segs = transcribe(video, config, language="ko")
    n = count_dialogue_segs(segs, duration, min_hangul=min_hangul,
                            max_span_frac=float(dconf.get("asr_max_span_frac", 0.9)))
    if n < len(segs):
        log.info("ASR 세그 필터: %d → %d (한글 %d자 미만 또는 전체-클립 스팬 제외)",
                 len(segs), n, min_hangul)
    return {"dialogue_segs": n, "asr_lang": lang, "asr_lang_prob": prob}


def precheck(video: str, video_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """실측 판별 실행 → outputs/{id}/precheck.json + 결과 dict(route, 근거)."""
    pc = config.get("autopilot", {}).get("precheck", {})
    min_conf = float(pc.get("min_conf", 0.75))
    min_hangul = int(pc.get("min_hangul", 2))
    min_persist = int(pc.get("min_persist", 2))

    from app.localize.overlay.common import probe
    meta = probe(video)

    frames, backend = _ocr_probe(video, config)
    burn = solid_hit_frames(frames, min_conf, min_hangul)
    asr = _asr_probe(video, config, float(meta.get("duration", 0.0) or 0.0),
                     min_hangul=2)
    dialogue = asr["dialogue_segs"]
    route = decide_route(burn, dialogue, min_persist)

    result = {"video_id": video_id, "route": route,
              "burn_frames": burn, "dialogue_segs": dialogue,
              "asr_lang": asr["asr_lang"], "asr_lang_prob": asr["asr_lang_prob"],
              "width": meta.get("width"), "height": meta.get("height"),
              "sampled_frames": len(frames), "ocr_backend": backend,
              "params": {"min_conf": min_conf, "min_hangul": min_hangul,
                         "min_persist": min_persist},
              "ocr_frames": frames}
    base = ensure_dir(resolve_path(f"{config['paths']['outputs_dir']}/{video_id}"))
    write_json(result, base / "precheck.json")
    log.info("precheck(%s): route=%s (번인 %d프레임, 대사 %d세그)",
             video_id, route, burn, dialogue)
    return result
