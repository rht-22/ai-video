"""영상 호출 한 곳 — 6·6b·8·10a·11:style 이 **같은 함수**를 부른다.

계약 정본 `docs/v4/M3-interfaces.md` §1 · 기획 `docs/v4/v4-plan.md` §4·§7·§8.

이 모듈이 있는 이유는 v3 의 실동작이다. v3 는 영상을 보는 호출이 **네 곳**인데 네 곳이
전부 조금씩 다르다(직접 확인):

    호출부                        모델    media_res   max_out   thinking          삭제
    seq_analyze._call_model       Pro     LOW 명시    65536     analysis 레벨     finally
    chunk_analyze._call_stage2_…  Pro     미지정      65536     analysis 레벨     finally
    refine._call_probe            Flash   미지정       1024     없음              finally
    stage4._call_style_model      Flash   미지정       8192     없음              finally

파싱 폴백도 셋만 같고 refine 만 다른 예외 문구를 낸다. 이런 것은 언젠가 한쪽만 고쳐진다
(이 레포가 여러 번 다친 방식 — E13 '베낀 수식', gotcha 9 '지문 재료가 단계마다 다름').
v4 는 **fps·media_resolution·재시도·usage 기록·파싱**을 여기 한 곳에 모은다.

🛑 **핸들을 삭제하지 않는다.** v3 네 곳은 전부 `finally: files.delete` 를 한다. v4 는
6·6b·8·10a 가 **같은 핸들을 공유**하므로(업로드 1회 — 기획서 §1 행 5) 단계 안에서 지우면
뒷단계가 죽은 핸들을 쓴다. 수명은 `app/v4/proxy.py`(`upload_handle`/`release_handle`)가
쥐고, 삭제는 최종 렌더 뒤 한 번이다.

## 이 워크트리에서 실제로 확인한 것 / 확인 못 한 것

**확인함**(google-genai **2.22.0** · `types` 실측):

    types.VideoMetadata.model_fields
      start_offset : Optional[str]   ← 문자열이다. float·int 를 주면 ValidationError
      end_offset   : Optional[str]
      fps          : Optional[float] ← "valid range is (0.0, 24.0]" (SDK 필드 설명)
    types.Part.media_processing : Optional[MediaProcessing]   ← STATIC/AGENTIC 은
      **Part 의 필드**다. `GenerateContentConfig` 에는 그런 필드가 없다(model_fields 확인).
      기획서 §4 의 "media_processing=STATIC 명시"가 어느 표면인지 여기서 정해진다.
    types.HttpOptions.timeout : Optional[int] — "Timeout for the request in **milliseconds**"
    usage_metadata 필드명: prompt_token_count · thoughts_token_count ·
      candidates_token_count · cached_content_token_count · total_token_count

**확인 못 함**(이 워크트리에 `GEMINI_API_KEY` 가 없다 — 실호출 0):

  · 같은 file_uri 를 offset 이 다른 파트로 여러 번 붙였을 때 **서버가 그 순서대로 이어
    붙여 보는가**. 근거는 기획서 §2-B 의 REST 실측(`docs/v4/probes/seam_equiv.py`·
    `mrcheck3.py`)이고 **SDK 경로의 실호출은 처음**이다. 파트 조립·순서 보존까지는
    이 파일의 테스트가 직렬화로 확인한다.
  · `"120"`(접미 없는 초)을 서버가 어떻게 읽는지. SDK 는 통과시킨다 — 그래서 `_offset`
    **한 함수**에서만 포맷하고 항상 `s` 접미를 붙인다(계약 §1 의 지시).
  · `media_processing=STATIC` 의 실제 효과. 기획서 §12 가 '미확인'으로 두고 있고,
    requirements 의 핀이 `google-genai>=0.8.0` 으로 느슨해 **필드가 없는 SDK 판이 노드에
    있을 수 있다.** 그래서 기본값은 None(안 보냄)이고, 켤 때 SDK 가 못 받으면 조용히
    빠지지 않고 **크게 실패한다**(`_media_processing_value`).
  · 토큰 산식. 예산 판정은 `app/v4/fps.py` 몫이고 이 파일은 **실측된 usage 를 그대로**
    기록만 한다 — 멀티파트 과금은 `countTokens` 로 3.8배 과소 계산되므로(기획서 §2-C)
    `usageMetadata` 가 유일한 정본이다.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Sequence

from app.modules.gemini_client import (
    _extract_json_from_markdown,
    _finish_reason,
    _loads_first_json,
    _max_tokens_usage,
    _usage_counts,
)

# ── 상수 ────────────────────────────────────────────────────────────────────

# E11 규약: 첫 시도를 뺀 재시도 횟수. 429·5xx·네트워크에만 쓴다.
MAX_RETRIES = 2
# 백오프는 `stt_elevenlabs._post_stt` 와 같은 자다(2s · 4s) — E11 이 정한 형태.
RETRY_BACKOFF_SEC = 2.0

# v3 세 곳(seq_analyze·chunk_analyze)의 값. 짧은 출력을 받는 단계(6b 1024 · 11:style
# 8192)는 부르는 쪽이 줄인다 — 여기서 줄이면 6단계 후보 16개가 조용히 잘린다.
DEFAULT_MAX_OUTPUT_TOKENS = 65536

# SDK 필드 설명 "The valid range is (0.0, 24.0]" + 프로브 실측
# (`docs/v4/probes/fps_cap_check.py` ② fps 하드캡). 넘겨도 서버가 거절하므로 여기서 먼저 죽는다.
FPS_HARD_CAP = 24.0

# 조각이 이보다 짧게 남으면 보내지 않는다. **판단이 아니라 물리다** — 길이 0 짜리 구간은
# 프레임이 한 장도 없다. 여기서 크게 잡으면(예: 1/표본fps) 정당하게 짧은 조각을 우리가
# 버리게 된다 — 짧다는 판정은 6c·7 의 몫이고 이 벨트는 '보낼 수 없는 것'만 자른다.
MIN_CLIP_SEC = 0.05

# v3 네 곳이 전부 같은 값으로 쓰는 것들. 바꾸면 산출이 흔들리므로 상수로 세운다.
VIDEO_MIME_TYPE = "video/mp4"
RESPONSE_MIME_TYPE = "application/json"
# 기획서 §7 "temperature 0, 모델 버전 기록". CLAUDE.md 는 Gemini 3.x 에서 샘플링
# 매개변수를 건드리지 말라고 하지만, **결정성이 합격 조항**이라 v3 네 곳이 전부 이미
# 명시적으로 예외를 두고 있다(v3-M1 절: "GeminiConfig 에 온도 필드가 없는 3.x 규약의
# 의도적 예외"). 같은 예외를 이어받는다.
TEMPERATURE = 0.0

# `gemini.config` 의 thinking_level 어휘(`GeminiConfig.analysis_thinking_level` 주석).
# 🛑 모르는 값을 그대로 보내면 400 INVALID_ARGUMENT 다 — P4 실런 결함 1 이 정확히 이것
# (2.5 시대 인자 `thinking_budget=0` 을 3.x 에 보냈다). 보내기 전에 죽는다.
THINKING_LEVELS = ("minimal", "low", "medium", "high")

# media_resolution 은 요청 단위다. 실측(기획서 §2-B): **미지정 = LOW = MEDIUM** 이고
# HIGH 만 약 4배다. 즉 "LOW 를 떼서 화질을 올린다"는 것은 존재하지 않는다.
MEDIA_RESOLUTIONS = ("LOW", "MEDIUM", "HIGH")

# Part.media_processing. 기획서 §4 가 STATIC 명시를 지시하지만 §12 는 '미확인'으로 둔다
# — 기본은 안 보내고(None), 켜는 것은 부르는 쪽 결정이다(모듈 독스트링 참조).
MEDIA_PROCESSINGS = ("STATIC", "AGENTIC")

__all__ = [
    "MAX_RETRIES", "RETRY_BACKOFF_SEC", "DEFAULT_MAX_OUTPUT_TOKENS",
    "FPS_HARD_CAP", "MIN_CLIP_SEC", "VIDEO_MIME_TYPE", "RESPONSE_MIME_TYPE",
    "TEMPERATURE", "THINKING_LEVELS", "MEDIA_RESOLUTIONS", "MEDIA_PROCESSINGS",
    "Clip", "VideoCallError", "VideoParseError",
    "classify_error", "clips_within_source", "build_video_parts",
    "usage_note", "call_video",
]


# ── 예외 ────────────────────────────────────────────────────────────────────

class VideoCallError(RuntimeError):
    """호출 자체가 실패했다(재시도 소진 · permanent 4xx). `.usage` 는 없을 수 있다."""

    def __init__(self, message: str, *, usage: dict | None = None,
                 kind: str | None = None) -> None:
        super().__init__(message)
        self.usage = usage
        self.kind = kind        # "transient" | "permanent" | None


class VideoParseError(ValueError):
    """응답은 왔는데 JSON 이 아니다.

    🛑 `ValueError` 인 것이 계약이다 — v3 `seq_analyze._call_model` 이 그렇게 올리고
    재질의 루프가 그것을 **반려 재료**로 받는다(리뷰 재현 수정: 루프 밖으로 새면 Pro
    비용을 치른 뒤 재질의 0회로 즉사한다). v4 6단계 루프도 같은 어휘를 본다.

    ⚠ v3 와 달리 `usage` 를 들고 올라간다 — 계약 §2 가 "audit 에 시도별
    {attempt, problems, **usage**} 를 전량 남긴다"고 했는데, 파싱이 실패한 시도야말로
    토큰을 제일 많이 먹은 시도다(MAX_TOKENS 절단). 그 숫자를 버리면 안 된다."""

    def __init__(self, message: str, *, usage: dict | None = None,
                 raw_text: str = "") -> None:
        super().__init__(message)
        self.usage = usage
        self.raw_text = raw_text


# ── 조각 ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Clip:
    """한 요청에 붙일 영상 조각. `start_sec`/`end_sec` 가 없으면 전체다.

    ⚠ 값 객체다 — 여기서 범위를 검사하지 않는다. 검사는 두 곳이고 역할이 다르다:
    `clips_within_source` 는 **기록하며 자르고**(소스 경계), `build_video_parts` 는
    보낼 수 없는 것에 **크게 실패한다**(start ≥ end 같은 계약 위반)."""

    start_sec: float | None = None
    end_sec: float | None = None

    @property
    def whole(self) -> bool:
        return self.start_sec is None and self.end_sec is None


def _finite(value: Any) -> float | None:
    """숫자로 읽히고 유한하면 float, 아니면 None. bool 은 숫자로 치지 않는다."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _offset(sec: float) -> str:
    """초 → 프로토버프 Duration 문자열. **포맷하는 자리는 여기 하나다**(계약 §1).

    SDK 실측: `start_offset`/`end_offset` 은 `Optional[str]` 이고 float 을 주면
    ValidationError 다. `"120"`(접미 없음)도 SDK 는 받지만 프로토버프 Duration 은 `s`
    접미가 필요하고 **서버가 어떻게 읽는지는 미검증**이라 항상 붙인다.

    소수 3자리는 프로브(`seam_equiv.offset_parts` 의 `f"{s:.3f}s"`)와 같은 자다 —
    ms 단위면 30fps 프레임(33.3ms) 경계보다 촘촘하다."""
    f = _finite(sec)
    if f is None:
        raise ValueError(f"offset 이 유한한 숫자가 아니다: {sec!r}")
    if f < 0:
        raise ValueError(f"offset 이 음수다: {f}")
    return f"{f:.3f}s"


def clips_within_source(
    clips: Sequence[Clip] | None,
    source_duration_sec: float,
) -> tuple[list[Clip], list[dict]]:
    """보내기 전 소스 길이와 대조 → (살아남은 파트, 손댄 기록). 순수·결정적.

    🛑 `endOffset` 은 소스를 넘어도 **오류 없이 조용히 클램프된다**(기획서 §7 경계 벨트).
    보내고 나면 모델이 무엇을 봤는지 알 수 없다 — 그래서 **보내기 전에** 자른다.
    v1 `pipeline.clips_beyond_source` 와 같은 취지인데 조치가 다르다: 저쪽은 렌더가
    조각을 통째로 잃는 사고라 **크게 실패**했고, 이쪽은 프롬프트 첨부라 자르고 기록한다.

    ⚠ **두 번째 값은 '버린 것'만이 아니다.** 각 기록에 `action` 이 있고
    `"dropped"`(보내지 않음) · `"clamped"`(끝을 소스 길이로 당김) 둘이다 — 클램프도
    모델이 보는 것을 바꾸므로 조용히 넘기지 않는다(규율 3). 드롭 수를 세려면
    `action == "dropped"` 로 걸러라. `len(records)` 는 드롭 수가 아니다.

    ⚠ 소스 길이를 모르면(0·음수·비유한) **판정하지 않고 크게 실패**한다. v1 의
    `clips_beyond_source` 는 '못 읽으면 판정하지 않는다'였지만, 그쪽은 ffprobe 실패라는
    정상 사건을 다뤘고 여기 오는 길이는 **격자(`grid["source"]["duration_sec"]`)** 다 —
    격자는 길이를 재지 못하면 애초에 만들어지지 않는다. 여기서 0 이 온다는 것은 배선
    오류이고, 그때 전량 통과시키면 벨트가 조용히 사라진다."""
    dur = _finite(source_duration_sec)
    if dur is None or dur <= 0:
        raise ValueError(
            f"소스 길이가 유효하지 않다: {source_duration_sec!r} — 경계 벨트는 격자의 "
            f"duration_sec 을 받는다(배선 오류일 때 전량 통과시키지 않는다)")

    kept: list[Clip] = []
    records: list[dict] = []
    for i, clip in enumerate(clips or []):
        if not isinstance(clip, Clip):
            raise TypeError(f"clips[{i}] 가 Clip 이 아니다: {type(clip).__name__}")
        if clip.whole:
            kept.append(clip)                     # 전체 첨부는 자를 것이 없다
            continue

        raw_start, raw_end = clip.start_sec, clip.end_sec
        start = 0.0 if raw_start is None else _finite(raw_start)
        end = dur if raw_end is None else _finite(raw_end)
        if start is None or end is None:
            records.append({"index": i, "action": "dropped",
                            "reason": "숫자가 아니다",
                            "start_sec": raw_start, "end_sec": raw_end})
            continue

        if start < 0:
            records.append({"index": i, "action": "clamped", "reason": "시작이 음수",
                            "start_sec": start, "end_sec": end,
                            "new_start_sec": 0.0, "new_end_sec": end})
            start = 0.0
        if start >= dur:
            # 시작 자체가 소스 밖이면 모델은 **아무것도 못 본다**(클램프해도 빈 구간).
            records.append({"index": i, "action": "dropped",
                            "reason": f"시작이 소스 밖({start} ≥ {dur})",
                            "start_sec": start, "end_sec": end})
            continue
        if end > dur:
            records.append({"index": i, "action": "clamped",
                            "reason": f"끝이 소스 밖({end} > {dur})",
                            "start_sec": start, "end_sec": end,
                            "new_start_sec": start, "new_end_sec": dur})
            end = dur
        if end - start < MIN_CLIP_SEC:
            records.append({"index": i, "action": "dropped",
                            "reason": f"길이가 {MIN_CLIP_SEC}s 미만({end - start:.3f}s)",
                            "start_sec": start, "end_sec": end})
            continue
        kept.append(Clip(start_sec=start, end_sec=end))
    return kept, records


# ── 파트 조립 ───────────────────────────────────────────────────────────────

def _file_uri(handle: Any) -> str:
    """핸들 객체·업로드 uri 문자열 → `file_uri`.

    `proxy.upload_handle` 은 객체를 돌려주지만 재개 경로는 체크포인트의 문자열만 든다
    (`checkpoint_upload.json` 의 `handle.uri`). 두 모양을 여기서 하나로 받는다.
    ⚠ `proxy.handle_name_of` 는 **name**(`files/abc`)을 만든다 — 그쪽은 files.get/delete
    용이고 첨부는 uri 다. 두 개를 섞지 않으려고 정규화를 각자 자리에 둔다."""
    uri = getattr(handle, "uri", None) or (handle if isinstance(handle, str) else None)
    uri = (uri or "").strip()
    if not uri:
        raise ValueError(
            f"영상 핸들에서 file_uri 를 못 얻었다: {handle!r} — "
            f"`proxy.upload_handle` 이 돌려준 핸들이나 그 uri 문자열을 넘겨라")
    return uri


def _media_processing_value(types: Any, media_processing: str | None) -> Any:
    """`Part.media_processing` 에 넣을 값. None 이면 안 보낸다.

    🛑 SDK 판이 이 필드를 모르면 **크게 실패한다**. requirements 핀이
    `google-genai>=0.8.0` 으로 느슨해 노드마다 판이 다를 수 있는데, 조용히 빠지면
    '명시했다고 믿는 상태'로 도는 것이 제일 나쁘다(기획서 §12 미확인 항목)."""
    if media_processing is None:
        return None
    value = str(media_processing).upper()
    if value not in MEDIA_PROCESSINGS:
        raise ValueError(f"모르는 media_processing: {media_processing!r} "
                         f"(허용 {MEDIA_PROCESSINGS})")
    if "media_processing" not in getattr(getattr(types, "Part", None),
                                         "model_fields", {}):
        raise ValueError(
            "이 SDK 판의 types.Part 에는 media_processing 필드가 없다 — "
            "google-genai 를 올리거나 media_processing 을 넘기지 마라 "
            "(조용히 빼면 '명시했다'고 믿은 채 돈다)")
    enum = getattr(types, "MediaProcessing", None)
    return getattr(enum, value) if enum is not None else value


def build_video_parts(gemini: Any, handle: Any, clips: Sequence[Clip] | None, *,
                      sample_fps: float, media_processing: str | None = None) -> list:
    """같은 핸들에 조각 수만큼 `Part` 를 붙인다 → 파트 목록.

    🛑 **첨부 순서가 곧 편집 순서다**(기획서 §2-B 실측: 역순으로 넣으면 답도 역순).
    그래서 이 함수는 넘겨받은 순서를 절대 바꾸지 않는다 — 정렬·중복 제거 없음.

    `clips` 가 None·빈 목록이면 파트 하나로 **전체**를 붙인다(v3 네 곳과 같은 모양).
    범위 위반(start ≥ end, 음수)은 여기서 **크게 실패한다** — `clips_within_source` 를
    지나온 조각은 그럴 수 없으므로, 그렇다면 배선이 그 벨트를 건너뛴 것이다."""
    fps = _finite(sample_fps)
    if fps is None or fps <= 0 or fps > FPS_HARD_CAP:
        raise ValueError(
            f"표본 fps 가 (0, {FPS_HARD_CAP}] 밖이다: {sample_fps!r} — "
            f"SDK 필드 설명과 프로브 실측(docs/v4/probes/fps_cap_check.py)이 같은 상한이다")

    types = gemini.types
    uri = _file_uri(handle)
    mp = _media_processing_value(types, media_processing)
    parts = []
    for i, clip in enumerate(list(clips) if clips else [Clip()]):
        if not isinstance(clip, Clip):
            raise TypeError(f"clips[{i}] 가 Clip 이 아니다: {type(clip).__name__}")
        meta: dict[str, Any] = {"fps": fps}
        if not clip.whole:
            start = 0.0 if clip.start_sec is None else float(clip.start_sec)
            end = clip.end_sec
            if end is None:
                raise ValueError(
                    f"clips[{i}] 에 end_sec 이 없다 — 끝을 안 주면 서버가 소스 끝까지 "
                    f"읽고, 그 길이는 우리가 모른다(조용한 클램프의 입구다). "
                    f"전체를 붙이려면 Clip() 을 써라")
            if float(end) - start < MIN_CLIP_SEC:
                raise ValueError(
                    f"clips[{i}] 의 길이가 {MIN_CLIP_SEC}s 미만이다: "
                    f"{start}~{end} — clips_within_source 를 먼저 지나야 한다")
            meta["start_offset"] = _offset(start)
            meta["end_offset"] = _offset(end)
        kwargs: dict[str, Any] = {
            "file_data": types.FileData(file_uri=uri, mime_type=VIDEO_MIME_TYPE),
            "video_metadata": types.VideoMetadata(**meta),
        }
        if mp is not None:
            kwargs["media_processing"] = mp
        parts.append(types.Part(**kwargs))
    return parts


# ── usage 기록 ──────────────────────────────────────────────────────────────

def _part_summary(parts: Any) -> tuple[int, list[list[float]] | None]:
    """`parts` 인자를 (개수, 창 목록|None) 으로. Clip 목록이면 창까지 남긴다."""
    n = _finite(parts)
    if n is not None and not isinstance(parts, (list, tuple)):
        return int(n), None
    windows: list[list[float]] = []
    seq = list(parts or [])
    for item in seq:
        if isinstance(item, Clip) and not item.whole:
            windows.append([float(item.start_sec or 0.0), float(item.end_sec or 0.0)])
    return len(seq), (windows or None)


def usage_note(response: Any, *, elapsed_sec: float, sample_fps: float,
               media_resolution: str | None, parts: Any) -> dict:
    """응답 → usage 기록 dict. 순수(테스트 대상).

    계약 §1 의 열쇠 그대로: prompt · thoughts · candidates · cached · total ·
    finish_reason · model_version · elapsed_sec · sample_fps · media_resolution · parts.

    ⚠ 토큰 숫자는 `gemini_client._usage_counts` 가 읽는다(수식·필드명 복제 금지).
    cached 만 그 함수가 안 세므로 여기서 같은 방식으로 하나 더 읽는다.
    ⚠ 없는 값은 **None 으로 남긴다** — 0 으로 채우면 '안 왔다'와 '0 이었다'가 같아지고
    집계가 조용히 틀어진다.
    ⚠ **이 숫자가 과금의 유일한 정본이다.** 멀티파트 요청을 `countTokens` 로 재면 3.8배
    과소 계산된다(기획서 §2-C · `docs/v4/probes/mrcheck2.py`)."""
    counts = _usage_counts(response) or {}
    meta = getattr(response, "usage_metadata", None)
    cached = getattr(meta, "cached_content_token_count", None) if meta is not None else None
    n_parts, windows = _part_summary(parts)
    note = {
        "prompt": counts.get("prompt_token_count"),
        "thoughts": counts.get("thoughts_token_count"),
        "candidates": counts.get("candidates_token_count"),
        "cached": cached if isinstance(cached, int) else None,
        "total": counts.get("total_token_count"),
        "finish_reason": _finish_reason(response) if response is not None else None,
        "model_version": getattr(response, "model_version", None),
        "elapsed_sec": round(float(elapsed_sec), 3),
        "sample_fps": _finite(sample_fps),
        # None 은 '미지정' 이고 실측상 LOW 와 같다 — 그 사실을 아는 것이 집계의 전제다.
        "media_resolution": media_resolution,
        "parts": n_parts,
    }
    if windows:
        note["part_windows"] = windows
    return note


# ── 실패 분류 (E11 규약) ────────────────────────────────────────────────────

def _status_of(exc: BaseException) -> int | None:
    """예외에서 HTTP 상태 코드를 뽑는다. 못 뽑으면 None.

    google-genai `APIError.__init__(code, response_json, response)` 가 `.code` 에 정수를
    담는다(SDK 실측). 다른 클라이언트를 쓰는 가짜·미래 판을 위해 `status_code` 도 본다.
    ⚠ **메시지 문자열에서 숫자를 긁지 않는다** — 토큰 수·시각이 섞여 있어 429 를 오인한다."""
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def classify_error(exc: BaseException) -> str:
    """→ `"transient"`(재시도) | `"permanent"`(즉시 실패). 순수.

    E11 규약 그대로: **429·5xx·네트워크만 재시도**하고 그 밖의 4xx 는 즉시 실패한다
    (`stt_elevenlabs._post_stt` 와 같은 자). 400 을 세 번 보내는 것은 요금만 세 배다.

    ⚠ **분류할 수 없는 예외는 permanent 다.** 우리 쪽 버그(ValidationError·TypeError)를
    transient 로 읽으면 비싼 호출을 세 번 태우고 같은 자리에서 죽는다. 진짜 네트워크
    오류는 대부분 `OSError`/`TimeoutError` 이거나 httpx 의 전송 예외라 아래에서 잡힌다."""
    status = _status_of(exc)
    if status is not None:
        return "transient" if (status == 429 or status >= 500) else "permanent"
    if isinstance(exc, (TimeoutError, OSError)):     # ConnectionError 는 OSError 다
        return "transient"
    try:                                             # httpx 는 genai 의 의존이라 늘 있다
        import httpx

        if isinstance(exc, httpx.TransportError):
            return "transient"
    except Exception:                                # noqa: BLE001 — 분류 보조가 죽지 않는다
        pass
    return "permanent"


# ── 본체 ────────────────────────────────────────────────────────────────────

def _media_resolution_value(types: Any, media_resolution: str | None) -> Any:
    if media_resolution is None:
        return None
    value = str(media_resolution).upper()
    if value not in MEDIA_RESOLUTIONS:
        raise ValueError(f"모르는 media_resolution: {media_resolution!r} "
                         f"(허용 {MEDIA_RESOLUTIONS} · None = 미지정 = 실측상 LOW)")
    return getattr(types.MediaResolution, f"MEDIA_RESOLUTION_{value}")


def _build_config(gemini: Any, *, media_resolution: str | None,
                  max_output_tokens: int, thinking_level: str | None,
                  timeout_sec: float | None) -> Any:
    types = gemini.types
    kwargs: dict[str, Any] = {
        "temperature": TEMPERATURE,
        "response_mime_type": RESPONSE_MIME_TYPE,
        "max_output_tokens": int(max_output_tokens),
    }
    mr = _media_resolution_value(types, media_resolution)
    if mr is not None:
        kwargs["media_resolution"] = mr
    if thinking_level is not None:
        if thinking_level not in THINKING_LEVELS:
            raise ValueError(f"모르는 thinking_level: {thinking_level!r} "
                             f"(허용 {THINKING_LEVELS})")
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
    if timeout_sec is not None:
        t = _finite(timeout_sec)
        if t is None or t <= 0:
            raise ValueError(f"timeout_sec 이 양수가 아니다: {timeout_sec!r}")
        # SDK 실측: HttpOptions.timeout 은 **밀리초**다. 초로 주면 6단계 450초 상한이
        # 0.45초가 되어 전부 타임아웃한다.
        kwargs["http_options"] = types.HttpOptions(timeout=int(round(t * 1000)))
    return types.GenerateContentConfig(**kwargs)


def _parse_response_text(text: str) -> Any:
    """v3 가 쓰는 폴백 그대로: 마크다운 펜스 제거 → json.loads → `_loads_first_json`.

    2026-08-03 실측(분석 22회 중 12회 파싱 실패)을 구제한 그 경로다. SDK 가 답변 파트를
    구분자 없이 이어 붙여 `{...}{...}` 가 되는 사고가 잦다.

    ⚠ v3 는 폴백 결과를 **dict 일 때만** 받는다. v4 는 dict·list 를 받는다 — 8단계
    플래그처럼 배열로 답하는 계약이 생길 수 있고, 여기서 좁히면 그 응답이 '파싱 실패'로
    보인다. 구조 판정은 각 단계의 validate_* 몫이다(계약 §2·§4)."""
    stripped = _extract_json_from_markdown(text or "")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as first:
        try:
            obj, _dropped = _loads_first_json(stripped)
        except json.JSONDecodeError:
            raise first from None
        if isinstance(obj, (dict, list)):
            return obj
        raise first from None


def call_video(gemini: Any, handle: Any, prompt: str, *, sample_fps: float,
               clips: Sequence[Clip] | None = None,
               media_resolution: str | None = None,
               max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
               thinking_level: str | None = None,
               model: str | None = None,
               media_processing: str | None = None,
               timeout_sec: float | None = None,
               log=print) -> tuple[Any, dict]:
    """영상 1회 호출 → (파싱된 JSON, usage 기록).

    · `clips` 가 여럿이면 **첨부 순서가 곧 편집 순서**다(기획서 §2-B). 각 파트는 같은
      핸들에 `VideoMetadata(start_offset, end_offset, fps)` 로 붙는다 — 렌더 없이
      짜집기 후보를 그대로 보여줄 수 있다는 것이 이 배선의 전부다.
      ⚠ 경계 벨트(`clips_within_source`)를 **먼저** 지나야 한다. 여기서는 위반이
      크게 실패한다(조용한 클램프의 반대편).
    · `model=None` 이면 **Flash 슬롯**(`gemini.config.flash_model_name`)이다. Pro 슬롯을
      쓰려면 부르는 쪽이 `gemini.config.model_name` 을 명시한다 — 어느 호출이 어느
      슬롯을 썼는지는 CLAUDE.md 의 역할 표가 정본이고, 이 함수는 조용히 고르지 않는다.
    · 재시도는 E11 규약(`classify_error`): 429·5xx·네트워크만 ≤{MAX_RETRIES}회, 백오프
      2s·4s. 그 밖의 4xx 는 즉시 실패한다. **조용히 다른 모델·설정으로 떨어지지 않는다.**
    · `finish_reason` 이 MAX_TOKENS 면 크게 남긴다 — v3-M2 실측에서 thinking 이 출력
      예산을 먹어 JSON 이 절단됐고, 그 잘린 조각은 엉뚱한 JSONDecodeError 로 보였다.

    🛑 **핸들을 삭제하지 않는다** — 수명은 `app/v4/proxy.py` 가 관리한다(모듈 독스트링).
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 가 비었다")

    # 파트·설정 조립은 **재시도 루프 밖**이다. 여기서 나는 오류(잘못된 offset·모르는
    # thinking_level)는 우리 계약 위반이라 재시도로 나아지지 않는다 — 세 번 태우지 않는다.
    parts = build_video_parts(gemini, handle, clips, sample_fps=sample_fps,
                              media_processing=media_processing)
    config = _build_config(gemini, media_resolution=media_resolution,
                           max_output_tokens=max_output_tokens,
                           thinking_level=thinking_level, timeout_sec=timeout_sec)
    model_name = model or gemini.config.flash_model_name

    response = None
    elapsed = 0.0
    attempts = 0
    last_err: BaseException | None = None
    for attempt in range(MAX_RETRIES + 1):
        attempts = attempt + 1
        t0 = time.time()
        try:
            response = gemini.client.models.generate_content(
                model=model_name, contents=[*parts, prompt], config=config)
            elapsed = round(time.time() - t0, 3)
            break
        except Exception as e:                       # noqa: BLE001 — 아래에서 분류한다
            elapsed = round(time.time() - t0, 3)
            last_err = e
            kind = classify_error(e)
            status = _status_of(e)
            where = f"{type(e).__name__}" + (f"/{status}" if status else "")
            if kind == "permanent" or attempt == MAX_RETRIES:
                raise VideoCallError(
                    f"영상 호출 실패({kind}, 시도 {attempts}/{MAX_RETRIES + 1}) — "
                    f"{where}: {e}", kind=kind) from e
            wait = RETRY_BACKOFF_SEC * (attempt + 1)
            log(f"  [v4/video] 재시도 {attempt + 1}/{MAX_RETRIES} — "
                f"{where}: {e} ({wait:.0f}s 뒤)")
            time.sleep(wait)

    if response is None:                             # 도달 불가 — 위 루프가 반드시 끝낸다
        raise VideoCallError(f"영상 호출이 응답도 예외도 남기지 않았다: {last_err}")

    usage = usage_note(response, elapsed_sec=elapsed, sample_fps=sample_fps,
                       media_resolution=media_resolution,
                       parts=list(clips) if clips else parts)
    usage["retries"] = attempts - 1                  # 계약 열쇠에 더한 가산 기록
    usage["model"] = model_name

    truncated = _max_tokens_usage(response)
    if truncated:
        # 조용하면 안 된다 — v3-M2 는 이 절단을 'JSON 파싱 실패'로만 보고 원인을 찾는 데
        # 로그를 거슬러 올라가야 했다.
        log(f"  [v4/video] 🛑 MAX_TOKENS 절단 — 응답이 끝나지 않았다 ({truncated}). "
            f"max_output_tokens={max_output_tokens} · thinking={thinking_level or '미지정'}")
    log(f"  [v4/video] {model_name} · 파트 {usage['parts']} · fps {sample_fps:g} · "
        f"{elapsed:.1f}s · 토큰 {usage['prompt']}/{usage['thoughts']}/"
        f"{usage['candidates']} (prompt/thoughts/out) · {usage['finish_reason']}")

    text = getattr(response, "text", None) or ""
    try:
        return _parse_response_text(text), usage
    except json.JSONDecodeError as e:
        raise VideoParseError(
            "응답 JSON 파싱 실패"
            + (f" (MAX_TOKENS 절단: {truncated})" if truncated else "")
            + f" [finish_reason={usage['finish_reason']}]: {e} — "
            f"앞 200자: {text[:200]!r}",
            usage=usage, raw_text=text) from e
