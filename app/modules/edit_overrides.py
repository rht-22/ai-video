"""편집실 오버라이드 — 사람이 고친 구간·제목을 파이프라인에 먹인다 (2026-08-16).

왜 필요한가: edit_plan.json 은 **기록물이지 입력이 아니다.** 렌더는
checkpoint_silence_cut → checkpoint_story 순으로 클립·제목을 읽고, edit_plan 은
둘 다 없을 때만 보는 폴백이라 사실상 도달하지 않는다. 그래서 사람이 edit_plan 을
고쳐도 영상은 그대로였다(2026-08-16 조사 실측). 관제 편집실이 성립하려면
**체크포인트와 무관한 별도 입력 통로**가 있어야 한다 — 이 모듈이 그 통로다.

계약(v1): `--edit-overrides <json>` 로 받는다.

    {
      "schema": "edit_overrides/v1",
      "title":  { "top_title": "1줄\\n2줄" },
      "clips":  [ {"start_sec": 742.5, "end_sec": 771.0, "role": "hook",
                   "use_original_audio": true}, ... ],
      "subtitles": [ {"start_sec": 0.2, "end_sec": 2.3, "text": "고친 자막"}, ... ]
    }

규약 넷 — 다 이유가 있다:
  · **clips 는 전량 교체**다(부분 패치 아님). 추가·삭제·순서변경이 섞이면 인덱스
    패치는 반드시 어긋난다. 편집실은 항상 전체 목록을 보내고 여기서 그대로 받는다.
  · **clips 를 지정하면 그 편은 '고정(pinned)'** 이다 — 대사 경계 스냅(±5초)·서사
    확장(편측 8초)·갭 메우기 같은 자동 보정을 건너뛴다. 사람이 12.5초라고 했는데
    8초 밀리면 편집기가 아니다. 단 길이 클램프와 중복 제거는 유지한다(렌더 안전장치).
  · **shorts #1(첫 variant)만 대상**이다. 편집은 특정 영상 하나를 고치는 일이고,
    variant #2·#3 은 자동 후보라 사람 손이 닿지 않은 채로 두는 편이 낫다.
  · **subtitles 는 clips 와 좌표계가 다르다** — clips 는 원본 절대초,
    subtitles 는 **편집본 시간축**(쇼츠 0초 시작)이다. subtitle_segments.json 과
    같은 좌표계이며, 그 파일이 자막의 유일한 정본이기 때문이다(clips[].subtitle 은
    use_original_audio=false 인 컷에서만 쓰이는 다른 값이라 편집 통로가 못 된다).
    역시 전량 교체다 — 한 줄을 지우려면 그 줄을 뺀 전체 목록을 보낸다.

clips 와 subtitles 를 함께 보내도 자막이 이긴다: 구간이 바뀌면 파이프라인이 자막을
전사에서 다시 매핑하는데(_subtitle_invalidate), 그 **뒤에** 이 오버라이드를 덮는다.
순서가 반대면 사람이 고친 문장이 조용히 기계 전사로 되돌아간다.

순수 함수만 둔다 — 파일 입출력은 load_edit_overrides 하나, 나머지는 테스트 대상.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.modules.story_builder import StoryClip

SCHEMA_V1 = "edit_overrides/v1"
VALID_ROLES = ("hook", "build", "payoff")


class EditOverrideError(ValueError):
    """오버라이드가 계약을 위반 — 조용히 무시하지 않고 즉시 실패한다.

    사람이 고친 값이 반영 안 된 채 영상이 나가는 것이 최악이다. 관제가 보낸 값이
    이상하면 잡을 실패시켜 검수함에 남기는 편이 낫다(registry 원칙과 같은 이유)."""


def load_edit_overrides(path: str | Path | None) -> dict[str, Any] | None:
    """오버라이드 JSON 로드 + 스키마 검증. 경로가 없으면 None(종전 동작)."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise EditOverrideError(f"편집 오버라이드 파일 없음: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise EditOverrideError(f"편집 오버라이드 파싱 실패: {e}") from e
    return validate_overrides(data)


def validate_overrides(data: Any) -> dict[str, Any]:
    """계약 검증. 순수 — 테스트 대상."""
    if not isinstance(data, dict):
        raise EditOverrideError("편집 오버라이드는 JSON 객체여야 합니다")
    schema = data.get("schema")
    if schema != SCHEMA_V1:
        raise EditOverrideError(f"알 수 없는 스키마: {schema!r} (기대: {SCHEMA_V1})")

    title = data.get("title") or {}
    if title and not isinstance(title, dict):
        raise EditOverrideError("title 은 객체여야 합니다")
    if title.get("top_title") is not None and not str(title["top_title"]).strip():
        raise EditOverrideError("top_title 이 비어 있습니다 — 고치지 않을 거면 키를 빼세요")

    clips = data.get("clips")
    if clips is not None:
        if not isinstance(clips, list) or not clips:
            raise EditOverrideError("clips 는 비어 있지 않은 배열이어야 합니다 "
                                    "(전량 교체 규약 — 전체를 빼려면 키 자체를 빼세요)")
        for i, c in enumerate(clips):
            if not isinstance(c, dict):
                raise EditOverrideError(f"clips[{i}] 가 객체가 아닙니다")
            try:
                s, e = float(c["start_sec"]), float(c["end_sec"])
            except (KeyError, TypeError, ValueError) as ex:
                raise EditOverrideError(
                    f"clips[{i}]: start_sec·end_sec 이 필요합니다 ({ex})") from ex
            if not (0 <= s < e):
                raise EditOverrideError(
                    f"clips[{i}]: 구간이 뒤집혔거나 음수입니다 ({s} → {e})")
            role = c.get("role", "build")
            if role not in VALID_ROLES:
                raise EditOverrideError(
                    f"clips[{i}]: role 은 {'/'.join(VALID_ROLES)} 중 하나 (받은 값: {role!r})")

    subs = data.get("subtitles")
    if subs is not None:
        if not isinstance(subs, list) or not subs:
            raise EditOverrideError("subtitles 는 비어 있지 않은 배열이어야 합니다 "
                                    "(전량 교체 규약 — 자막을 통째로 끄려면 --no-subtitles)")
        prev_end = None
        for i, s in enumerate(subs):
            if not isinstance(s, dict):
                raise EditOverrideError(f"subtitles[{i}] 가 객체가 아닙니다")
            try:
                a, b = float(s["start_sec"]), float(s["end_sec"])
            except (KeyError, TypeError, ValueError) as ex:
                raise EditOverrideError(
                    f"subtitles[{i}]: start_sec·end_sec 이 필요합니다 ({ex})") from ex
            if not (0 <= a < b):
                raise EditOverrideError(
                    f"subtitles[{i}]: 구간이 뒤집혔거나 음수입니다 ({a} → {b})")
            if not str(s.get("text", "")).strip():
                raise EditOverrideError(
                    f"subtitles[{i}]: text 가 비어 있습니다 — 그 줄을 지우려면 배열에서 빼세요")
            # 겹치면 ASS 가 두 줄을 같은 자리에 겹쳐 그린다. 편집실이 시간을 만지게 될
            # 3단계를 대비해 지금부터 막는다 — 화면에서야 알아채면 이미 렌더가 끝난 뒤다.
            if prev_end is not None and a < prev_end - 1e-6:
                raise EditOverrideError(
                    f"subtitles[{i}]: 앞 자막과 겹칩니다 (앞 끝 {prev_end} > 이 시작 {a}) "
                    "— 시간순으로 겹치지 않게 보내세요")
            prev_end = b
    return data


def _best_overlap(start: float, end: float, olds: list[StoryClip]) -> StoryClip | None:
    """새 구간과 원본 시간축에서 가장 많이 겹치는 옛 클립. 없으면 None. 순수."""
    best, best_ov = None, 0.0
    for o in olds or []:
        ov = min(end, float(o.end_sec)) - max(start, float(o.start_sec))
        if ov > best_ov:
            best, best_ov = o, ov
    return best


def overrides_clips(ov: dict[str, Any] | None,
                    olds: list[StoryClip] | None = None) -> list[StoryClip] | None:
    """오버라이드 → StoryClip 목록. clips 키가 없으면 None. 순수 — 테스트 대상.

    **메타데이터는 가장 많이 겹치는 옛 클립에서 물려받는다.** 편집실이 보내는 것은
    사람이 화면에서 만질 수 있는 값(구간·역할·오디오)뿐인데, StoryClip 에는 화면에
    없지만 품질을 좌우하는 값들이 더 있기 때문이다:
      · character_focus — 얼굴 추적·멀티 크롭의 타겟. 잃으면 리프레이밍이 눈에 띄게 나빠진다
      · chunk_index/candidate_index — TTS cue 앵커의 동점 판정, 길이 클램프 lookup
      · subtitle — use_original_audio=false 일 때 그 구간 자막이 되는 값
    경계를 몇 초 옮긴 것(편집실의 대다수 조작)은 같은 소재를 그대로 쓰는 것이므로
    물려받는 게 맞다. 원본 어디에서도 겹치지 않는 완전히 새 구간이면 -1/빈 값으로
    남는데, 그건 '그 자리에 계획된 나레이션이 없다'는 뜻이라 의미상으로도 맞다."""
    if not ov or not ov.get("clips"):
        return None
    out: list[StoryClip] = []
    for c in ov["clips"]:
        s, e = float(c["start_sec"]), float(c["end_sec"])
        src = _best_overlap(s, e, olds or [])
        out.append(StoryClip(
            role=c.get("role", "build"),
            start_sec=s,
            end_sec=e,
            subtitle=str(c["subtitle"]) if c.get("subtitle") is not None
                     else (src.subtitle if src else ""),
            use_original_audio=bool(c["use_original_audio"])
                               if c.get("use_original_audio") is not None
                               else (src.use_original_audio if src else True),
            pacing_note=str(c.get("pacing_note", "")) or (src.pacing_note if src else ""),
            chunk_index=src.chunk_index if src else -1,
            candidate_index=src.candidate_index if src else -1,
            character_focus=src.character_focus if src else (),
            visual_essential=src.visual_essential if src else False,
            tts_draft=src.tts_draft if src else "",
        ))
    return out


def apply_overrides(variants: list[tuple[list[StoryClip], str, float]],
                    ov: dict[str, Any] | None,
                    ) -> tuple[list[tuple[list[StoryClip], str, float]], bool]:
    """첫 variant 에 오버라이드 적용 → (새 variants, pinned).

    pinned=True 면 호출부가 snap/extend/fill 자동 보정을 건너뛴다.
    제목만 고친 경우 pinned=False — 구간은 종전 자동 보정을 그대로 받는다
    (제목 수정 때문에 구간 품질이 달라지면 사람이 놀란다). 순수 — 테스트 대상."""
    if not ov or not variants:
        return variants, False
    clips, title, score = variants[0]
    new_clips = overrides_clips(ov, clips)
    new_title = ((ov.get("title") or {}).get("top_title") or "").strip() or title
    head = (new_clips if new_clips is not None else clips, new_title, score)
    return [head, *variants[1:]], new_clips is not None


def overrides_subtitles(ov: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """오버라이드 → 자막 세그먼트 목록(편집본 시간축). 키가 없으면 None. 순수.

    subtitle_segments.json 과 **똑같은 3필드**로 정규화해서 돌려준다 — 그 파일이
    자막의 정본이고, 호출부는 이 결과를 그대로 캐시에 다시 써서 다음 렌더·편집실
    재방문에서도 사람이 고친 문장이 보이게 한다."""
    if not ov or not ov.get("subtitles"):
        return None
    return [{"start_sec": float(s["start_sec"]),
             "end_sec": float(s["end_sec"]),
             "text": str(s["text"]).strip()} for s in ov["subtitles"]]


def total_duration(clips: list[StoryClip]) -> float:
    """구간 합계(초). 편집실 60초 상한 검증과 로그용. 순수."""
    return sum(float(c.end_sec) - float(c.start_sec) for c in clips or [])
