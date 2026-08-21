"""편집실 오버라이드 — 사람이 고친 구간·제목을 파이프라인에 먹인다 (2026-08-16).

왜 필요한가: edit_plan.json 은 **기록물이지 입력이 아니다.** 렌더는
checkpoint_silence_cut → checkpoint_story 순으로 클립·제목을 읽고, edit_plan 은
둘 다 없을 때만 보는 폴백이라 사실상 도달하지 않는다. 그래서 사람이 edit_plan 을
고쳐도 영상은 그대로였다(2026-08-16 조사 실측). 관제 편집실이 성립하려면
**체크포인트와 무관한 별도 입력 통로**가 있어야 한다 — 이 모듈이 그 통로다.

계약: `--edit-overrides <json>` 로 받는다. v2 = v1 + tts(내레이션).
v3 = v2 + 자막 앵커/줄 스타일 + images (정본 문서: docs/edit_overrides_v3.md).

    {
      "schema": "edit_overrides/v3",          # v1·v2 도 계속 받는다
      "title":  { "top_title": "1줄\\n2줄",
                  "segments": [ {"text": "첫 제목\\n둘째 줄",        # (E8, v3 전용)
                                 "start_sec": 0, "end_sec": 12.5}, ... ] },
      "clips":  [ {"start_sec": 742.5, "end_sec": 771.0, "role": "hook",
                   "use_original_audio": true}, ... ],
      "subtitles": [ {"start_sec": 0.2, "end_sec": 2.3, "text": "고친 자막",
                      "source_time_sec": 743.2,            # (v3) 원본 절대초 앵커
                      "style": {"size": 64, "y": 0.8,       # (v3) 줄 단위 스타일
                                "color": "#FFDD00"}}, ... ],
      "tts": [ {"source_time_sec": 743.0, "duration_sec": 3.5,
                "voice": "ko_female", "text": "고친 내레이션"}, ... ],
      "images": [ {"file": "assets/arrow.png", "source_time_sec": 745.0,   # (v3)
                   "duration_sec": 2.0, "x": 0.1, "y": 0.2, "w": 0.3,
                   "layer": 1, "rotate": -15}, ... ]           # rotate 는 F-410
    }

스키마를 올린 이유(v2·v3 공통): 구 엔진의 validate_overrides 는 모르는 키를 조용히
무시한다 — 새 기능을 구 스키마에 얹으면 구 엔진 노드에서 사람이 고친 값이 소리 없이
사라진 채 영상이 나간다(이 모듈의 제1원칙 위반). 새 스키마는 구 엔진에서 "알 수 없는
스키마"로 즉시 실패해 검수함에 남는다(2026-08-19 실측: v2 엔진에 v3 를 넣으면
"알 수 없는 스키마: 'edit_overrides/v3'"). 같은 이유로 **이 엔진도 v3 전용 필드
(subtitles[].source_time_sec/style, images)가 v1·v2 스탬프에 실려 오면 즉시 거절**
한다 — 스탬프와 내용이 어긋난 채 받아주면 노드마다 결과가 달라진다.
관제 RPC 는 v3 필드가 있을 때만 v3 를 찍는다.

v3 추가분:
  · **subtitles[].source_time_sec (F-401)** — 원본 절대초 앵커. 있으면 대사 재매핑
    대신 tts 와 같은 규칙으로 최종 타임라인에 변환 배치한다(place_anchored_subtitles,
    포함 판정 슬롭 ±0.5s = 편집실 UI 규약). 이로써 clips 와 subtitles 를 **같이
    보내도 안전**하다 — 종전엔 구간이 바뀌면 자막 좌표(편집본 시간축)가 통째로
    어긋나서 편집실이 자막을 아예 안 보냈다(2-pass 강제). 앵커 없는 항목(신규 줄)은
    종전대로 start_sec(편집본 시간축)를 그대로 쓴다.
  · **subtitles[].style (F-407)** — 줄 단위 스타일 오버라이드 {size, y, color,
    rotate}. 채널/편 전역 프리셋 위에 그 줄만 얹는다. size = 1080×1920 캔버스 기준
    폰트 px, y = 자막 하단이 놓일 세로 위치(0=상단, 1=하단, 캔버스 비율),
    color = "#RRGGBB", rotate = 도 단위 -180~180 시계방향 양수(F-410 — ASS \\frz 는
    반시계 양수라 부호 반전은 subtitle.py 가 책임진다).
  · **images (F-408)** — 자막처럼 시간 창을 갖는 이미지(스티커·짤) 오버레이.
    x·y·w 는 1080×1920 캔버스 대비 0~1 비율(w 만 지정 — 세로는 원본비 유지,
    rotate 가 있어도 w 는 회전 **전** 원본 기준 F-410),
    file 은 run_dir 상대 경로(png/jpg, 상한 IMAGE_MAX_BYTES). 스토리지 다운로드는
    오케스트레이터 어댑터 몫이다 — 엔진은 로컬 파일만 받는다(스토리지 자격 없음).
    시간 창은 자막 앵커와 같은 원본 절대초(source_time_sec + duration_sec) —
    place_anchored_images 가 최종 타임라인으로 변환하고 고아는 tts 규칙대로 드롭한다.
    layer 로 자막 아래(≤0, 기본 0)/위(≥1)를 가른다.
  · **texts (F-411)** — 대사가 아닌 글자(의성어·의태어·강조·보조설명)를 화면 아무
    데나 얹는 자유 텍스트 오브젝트. 대사 자막(subtitles, 전사 기반·하단 정렬 프리셋)과
    **별개 레이어**다 — 편집실 '텍스트' 탭이 보내고 AI 효과 텍스트 제안도 같은
    형식으로 온다. 시간 창·앵커·배치는 images 와 동일(source_time_sec + duration_sec,
    place_anchored_texts, 고아 드롭). x·y 는 글자 **중심**의 캔버스 비율(0~1),
    size 는 1080×1920 기준 폰트 px, color "#RRGGBB", stroke(dark/none/white),
    fx(none/pop/shake — 등장 효과), rotate 는 images 와 같은 시계방향 양수,
    font 는 번들 폰트 4종 중 하나. 렌더는 별도 ASS 한 장(build_texts_ass)으로
    대사 자막·TTS 자막 **위**, images layer≥1 **아래**에 그린다(layer 키 없음).
  · **title.segments (E8)** — 시간대별 제목. 자막처럼 시간 창(편집본 시간축, 초)을
    갖는 제목 세그먼트 목록. 있으면 **그 창들만 그린다** — 창 밖 시간은 제목 없음
    (빈 화면이 유효값: '뒤에는 제목을 끄고 싶다'가 이 기능의 절반이다). top_title 은
    세그먼트가 없거나 구 대시보드가 보낼 때의 종전 경로(전체 상영) 그대로.
    title_y_fixed/title_y·title_size·title_rotate(E7)는 전 세그먼트 공통(디자인 레벨
    유지) — 세그먼트별 스타일은 후속. 검증 규칙은 validate_title_segments 참고.

규약 넷 — 다 이유가 있다:
  · **clips 는 전량 교체**다(부분 패치 아님). 추가·삭제·순서변경이 섞이면 인덱스
    패치는 반드시 어긋난다. 편집실은 항상 전체 목록을 보내고 여기서 그대로 받는다.
  · **clips 를 지정하면 그 편은 '고정(pinned)'** 이다 — 대사 경계 스냅(±5초)·서사
    확장(편측 8초)·갭 메우기 같은 자동 보정을 건너뛴다. 사람이 12.5초라고 했는데
    8초 밀리면 편집기가 아니다. 단 길이 클램프(쇼츠 성립 한계 3분 기준 — 60초는 자동
    생성 정책이라 사람에겐 적용하지 않는다, 2026-08-19)와 중복 제거는 유지한다(렌더 안전장치).
    클램프가 걸려도 구간 통째 삭제는 없다 — 역할별 부분 trim 만 한다.
  · **shorts #1(첫 variant)만 대상**이다. 편집은 특정 영상 하나를 고치는 일이고,
    variant #2·#3 은 자동 후보라 사람 손이 닿지 않은 채로 두는 편이 낫다.
  · **tts 는 원본 절대초(source_time_sec)가 좌표이자 신원이다.** cue 는 원래부터
    원본시간 앵커로 살다가 최종 타임라인 확정 후에야 편집시간으로 변환되므로
    (_resolve_cue_anchors), 사람이 구간을 함께 고쳐도 좌표가 안 흔들린다 — 자막과
    달리 구간 편집과 같이 보내도 된다. 역시 전량 교체이며, **빈 배열은 '내레이션
    전부 삭제'로 유효하다**(자막의 --no-subtitles 같은 별도 스위치가 내레이션엔
    없고, 안 어울리는 내레이션을 통째로 빼는 것이 실제 편집 수요라서다).
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
import re
from pathlib import Path
from typing import Any

from app.modules.story_builder import StoryClip

SCHEMA_V1 = "edit_overrides/v1"
SCHEMA_V2 = "edit_overrides/v2"          # v1 + tts. v1 은 계속 받는다
SCHEMA_V3 = "edit_overrides/v3"          # v2 + 자막 앵커/줄 스타일 + images
VALID_SCHEMAS = (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3)
VALID_ROLES = ("hook", "build", "payoff")
# tts 상속 매칭 반경(초) — 편집실은 받은 source_time_sec 을 그대로 되돌려 보내므로
# 사실상 정확히 일치하지만, 반올림·수기 입력의 오차를 이만큼 관용한다.
TTS_MATCH_TOLERANCE_SEC = 1.0
# 앵커 자막 포함 판정 슬롭(초) — 편집실 UI 와 동일 규약. 클립 경계 반올림(소수 3자리)
# 오차로 경계 위의 자막이 고아가 되지 않게 한다.
SUBTITLE_ANCHOR_SLOP_SEC = 0.5
# 줄 단위 스타일 계약 — 이 키들만 받는다. 모르는 키를 조용히 무시하면 사람이 고친
# 값이 반영 안 된 채 영상이 나가므로(제1원칙) 즉시 실패한다.
SUBTITLE_STYLE_KEYS = ("size", "y", "color", "rotate")
# images 파일 계약 — ffmpeg 이 그대로 읽는 정지 이미지만(알파가 필요하면 png).
# 크기 상한은 스티커·짤 용도 기준 재량값이다(캔버스 전체를 덮는 png 도 수 MB 면 충분).
IMAGE_ALLOWED_SUFFIXES = (".png", ".jpg", ".jpeg")
IMAGE_MAX_BYTES = 10 * 1024 * 1024
# images 항목 허용 키 — style 과 같은 이유로 모르는 키는 즉시 거절한다. 이 목록에
# 키를 더하는 쪽(신 엔진)이 먼저 배포돼야 구 엔진 노드가 그 키를 fail-loud 로
# 거절한다(조용한 무시 = 노드마다 결과가 갈린다).
IMAGE_KEYS = ("file", "source_time_sec", "duration_sec", "x", "y", "w", "layer",
              "rotate")
# texts(F-411) 항목 허용 키 — images 와 같은 fail-loud 규약(모르는 키 즉시 거절).
TEXT_KEYS = ("text", "source_time_sec", "duration_sec", "x", "y", "size", "color",
             "stroke", "fx", "rotate", "font")
TEXT_STROKES = ("dark", "none", "white")     # 테두리: 어둡게(기본)/없음/흰색
TEXT_FX = ("none", "pop", "shake")           # 등장 효과: 없음(기본)/튀어나옴/흔들림
TEXT_FONTS = ("Jalnan", "JalnanGothic", "mulmaru", "Griun")   # 번들 폰트 파일 stem
TEXT_SIZE_RANGE = (12, 400)                  # 캔버스(1080×1920) 기준 폰트 px
TEXT_MAX_CHARS = 60                          # 한 오브젝트의 글자 수 상한(줄바꿈 포함)
TEXT_DEFAULTS = {"size": 72, "color": "#FFFFFF", "stroke": "dark", "fx": "none",
                 "rotate": 0.0, "font": "Jalnan"}
# 회전 계약(images[].rotate · subtitles[].style.rotate 공통) — 도 단위, 시계방향
# 양수, 원점은 이미지 중심/자막 앵커, 기본 0.
ROTATE_RANGE_DEG = 180.0
# 시간대별 제목(E8) — title.segments[] 항목 허용 키. 세그먼트별 스타일(크기·색·회전)은
# 이번 판 범위 밖(후속)이라 키 자체를 거절한다 — style 과 같은 fail-loud 규약.
TITLE_SEGMENT_KEYS = ("text", "start_sec", "end_sec")
TITLE_MAX_SEGMENTS = 20
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


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
    if schema not in VALID_SCHEMAS:
        raise EditOverrideError(f"알 수 없는 스키마: {schema!r} (기대: {'/'.join(VALID_SCHEMAS)})")

    title = data.get("title") or {}
    if title and not isinstance(title, dict):
        raise EditOverrideError("title 은 객체여야 합니다")
    if title.get("top_title") is not None and not str(title["top_title"]).strip():
        raise EditOverrideError("top_title 이 비어 있습니다 — 고치지 않을 거면 키를 빼세요")
    if title.get("segments") is not None:
        if schema != SCHEMA_V3:
            raise EditOverrideError(
                "title.segments 는 edit_overrides/v3 전용입니다 — 스키마를 v3 로 찍으세요 "
                "(구 스키마에 얹으면 구 엔진 노드가 조용히 무시합니다)")
        validate_title_segments(title["segments"])

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
            _validate_subtitle_v3_fields(i, s, schema)
        # 🛑 겹침은 검사하지 않는다. 넣었다가 실측에서 걷어냈다(2026-08-17):
        #    실제 subtitle_segments.json 이 겹치는 세그먼트를 정상적으로 담고 있다 —
        #    피의_게임_X_9d2d1b85 는 20건 중 여러 쌍이 겹쳤다(0.2~3.67 과 1.7~5.3).
        #    merge_subtitle_segments 가 그렇게 만들고 ASS 도 그대로 그린다. 전량 교체
        #    규약상 편집실은 **원본 목록을 그대로 되돌려 보내는데**, 겹침을 막으면
        #    한 줄만 고쳐도 전체가 거부된다. 파이프라인이 허용하는 것을 입력 검증이
        #    막으면 안 된다 — 편집실이 만든 문제가 아니라 원래 그런 데이터다.

    tts = data.get("tts")
    if tts is not None:
        if not isinstance(tts, list):
            raise EditOverrideError("tts 는 배열이어야 합니다 "
                                    "(빈 배열 = 내레이션 전부 삭제, 전량 교체 규약)")
        for i, c in enumerate(tts):
            if not isinstance(c, dict):
                raise EditOverrideError(f"tts[{i}] 가 객체가 아닙니다")
            try:
                st = float(c["source_time_sec"])
            except (KeyError, TypeError, ValueError) as ex:
                raise EditOverrideError(
                    f"tts[{i}]: source_time_sec(원본 절대초)이 필요합니다 ({ex})") from ex
            if st < 0:
                raise EditOverrideError(f"tts[{i}]: source_time_sec 이 음수입니다 ({st})")
            if not str(c.get("text", "")).strip():
                raise EditOverrideError(
                    f"tts[{i}]: text 가 비어 있습니다 — 그 내레이션을 지우려면 배열에서 빼세요")
            if c.get("duration_sec") is not None:
                try:
                    d = float(c["duration_sec"])
                except (TypeError, ValueError) as ex:
                    raise EditOverrideError(f"tts[{i}]: duration_sec 이 숫자가 아닙니다") from ex
                if d <= 0:
                    raise EditOverrideError(f"tts[{i}]: duration_sec 은 양수여야 합니다 ({d})")

    images = data.get("images")
    if images is not None:
        if schema != SCHEMA_V3:
            raise EditOverrideError(
                "images 는 edit_overrides/v3 전용입니다 — 스키마를 v3 로 찍으세요 "
                "(구 스키마에 얹으면 구 엔진 노드가 조용히 무시합니다)")
        if not isinstance(images, list) or not images:
            raise EditOverrideError("images 는 비어 있지 않은 배열이어야 합니다 "
                                    "(이미지를 안 쓰면 키 자체를 빼세요)")
        for i, im in enumerate(images):
            _validate_image(i, im)

    texts = data.get("texts")
    if texts is not None:
        if schema != SCHEMA_V3:
            raise EditOverrideError(
                "texts 는 edit_overrides/v3 전용입니다 — 스키마를 v3 로 찍으세요 "
                "(구 스키마에 얹으면 구 엔진 노드가 조용히 무시합니다)")
        if not isinstance(texts, list) or not texts:
            raise EditOverrideError("texts 는 비어 있지 않은 배열이어야 합니다 "
                                    "(텍스트를 안 쓰면 키 자체를 빼세요)")
        for i, t in enumerate(texts):
            _validate_text(i, t)
    return data


def _validate_subtitle_v3_fields(i: int, s: dict[str, Any], schema: str) -> None:
    """subtitles[i] 의 v3 전용 필드(source_time_sec 앵커·style) 검증.

    v3 필드가 v1·v2 스탬프에 실려 오면 즉시 거절 — 구 엔진은 이 필드를 조용히
    무시하므로, 스탬프 없이 받아주면 노드마다 결과가 달라진다(모듈 머리말 참고)."""
    has_v3 = s.get("source_time_sec") is not None or s.get("style") is not None
    if not has_v3:
        return
    if schema != SCHEMA_V3:
        raise EditOverrideError(
            f"subtitles[{i}]: source_time_sec/style 은 edit_overrides/v3 전용입니다 — "
            "스키마를 v3 로 찍으세요 (구 스키마에 얹으면 구 엔진 노드가 조용히 무시합니다)")
    if s.get("source_time_sec") is not None:
        try:
            st = float(s["source_time_sec"])
        except (TypeError, ValueError) as ex:
            raise EditOverrideError(
                f"subtitles[{i}]: source_time_sec 이 숫자가 아닙니다") from ex
        if st < 0:
            raise EditOverrideError(f"subtitles[{i}]: source_time_sec 이 음수입니다 ({st})")
    style = s.get("style")
    if style is None:
        return
    if not isinstance(style, dict) or not style:
        raise EditOverrideError(
            f"subtitles[{i}]: style 은 비어 있지 않은 객체여야 합니다 — "
            "스타일을 안 고치면 키 자체를 빼세요")
    unknown = [k for k in style if k not in SUBTITLE_STYLE_KEYS]
    if unknown:
        raise EditOverrideError(
            f"subtitles[{i}]: style 에 모르는 키 {unknown} — "
            f"계약은 {'/'.join(SUBTITLE_STYLE_KEYS)} 뿐입니다")
    if style.get("size") is not None:
        try:
            size = float(style["size"])
        except (TypeError, ValueError) as ex:
            raise EditOverrideError(f"subtitles[{i}]: style.size 가 숫자가 아닙니다") from ex
        if size <= 0:
            raise EditOverrideError(f"subtitles[{i}]: style.size 는 양수여야 합니다 ({size})")
    if style.get("y") is not None:
        try:
            y = float(style["y"])
        except (TypeError, ValueError) as ex:
            raise EditOverrideError(f"subtitles[{i}]: style.y 가 숫자가 아닙니다") from ex
        if not (0.0 <= y <= 1.0):
            raise EditOverrideError(
                f"subtitles[{i}]: style.y 는 0~1 (캔버스 세로 비율)이어야 합니다 ({y})")
    if style.get("color") is not None:
        if not _COLOR_RE.match(str(style["color"])):
            raise EditOverrideError(
                f"subtitles[{i}]: style.color 는 '#RRGGBB' 형식이어야 합니다 "
                f"(받은 값: {style['color']!r})")
    if style.get("rotate") is not None:
        _validate_rotate(f"subtitles[{i}]: style.rotate", style["rotate"])


def validate_title_segments(segs: Any) -> None:
    """시간대별 제목(E8) title.segments 계약 검증 — 즉시 실패, 조용한 무시 금지. 순수.

    렌더 경계(_build_filtergraph)에서도 다시 부른다(E7 과 같은 이유 — CLI·오버라이드
    검증을 안 거친 호출이 이상한 창을 들고 오면 조용히 이상한 영상을 만드는 대신
    즉시 실패한다). 규칙:
      · 비어 있지 않은 배열, 최대 TITLE_MAX_SEGMENTS(20)개
      · text 비면 거절 · start_sec ≥ 0 · end_sec > start_sec
      · 창끼리 겹침 거절 — 제목은 한 벌 자리라 겹치면 포개진다
    좌표계는 subtitles 와 동일(**편집본 시간축**, 쇼츠 0초 시작, 초)."""
    if not isinstance(segs, list) or not segs:
        raise EditOverrideError("title.segments 는 비어 있지 않은 배열이어야 합니다 "
                                "(시간대별 제목을 안 쓰면 키 자체를 빼세요)")
    if len(segs) > TITLE_MAX_SEGMENTS:
        raise EditOverrideError(
            f"title.segments 는 최대 {TITLE_MAX_SEGMENTS}개입니다 ({len(segs)}개)")
    windows: list[tuple[float, float, int]] = []
    for i, sg in enumerate(segs):
        if not isinstance(sg, dict):
            raise EditOverrideError(f"title.segments[{i}] 가 객체가 아닙니다")
        unknown = [k for k in sg if k not in TITLE_SEGMENT_KEYS]
        if unknown:
            raise EditOverrideError(
                f"title.segments[{i}]: 모르는 키 {unknown} — 계약은 "
                f"{'/'.join(TITLE_SEGMENT_KEYS)} 뿐입니다 (세그먼트별 스타일은 후속)")
        try:
            s, e = float(sg["start_sec"]), float(sg["end_sec"])
        except (KeyError, TypeError, ValueError) as ex:
            raise EditOverrideError(
                f"title.segments[{i}]: start_sec·end_sec 이 필요합니다 ({ex})") from ex
        if s < 0:
            raise EditOverrideError(f"title.segments[{i}]: start_sec 이 음수입니다 ({s})")
        if e <= s:
            raise EditOverrideError(
                f"title.segments[{i}]: 구간이 뒤집혔거나 길이 0 입니다 ({s} → {e})")
        if not str(sg.get("text", "")).strip():
            raise EditOverrideError(
                f"title.segments[{i}]: text 가 비어 있습니다 — 그 시간대에 제목을 끄려면 "
                "그 창을 배열에서 빼세요 (창 밖 시간은 원래 제목 없음)")
        windows.append((s, e, i))
    windows.sort()
    for (s1, e1, i1), (s2, e2, i2) in zip(windows, windows[1:]):
        if s2 < e1 - 1e-9:
            raise EditOverrideError(
                f"title.segments[{i1}]·[{i2}] 창이 겹칩니다 ({s1:g}~{e1:g} vs "
                f"{s2:g}~{e2:g}) — 제목은 한 벌 자리라 겹치면 포개집니다")


def overrides_title_segments(ov: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """오버라이드 → 시간대별 제목(E8) 세그먼트 목록. 키가 없으면 None. 순수.

    {text, start_sec, end_sec} 로 정규화(숫자화)하고 시각순으로 정렬해 돌려준다 —
    겹침은 검증에서 이미 거절됐으므로 정렬은 안전하다. text 는 top_title 과 같은
    규약(줄바꿈 = 2줄 위계, title_color/title_color2 lookup)이라 그대로 통과한다.
    좌표는 편집본 시간축 그대로다 — 배속(×1/S) 변환은 제목이 setpts 뒤에 얹히는
    렌더러가 이미지 오버레이와 같은 지점에서 한다."""
    segs = ((ov or {}).get("title") or {}).get("segments")
    if not segs:
        return None
    out = [{"text": str(sg["text"]),
            "start_sec": float(sg["start_sec"]),
            "end_sec": float(sg["end_sec"])} for sg in segs]
    out.sort(key=lambda x: (x["start_sec"], x["end_sec"]))
    return out


def _validate_rotate(where: str, value: Any) -> None:
    """회전 공통 검증(images[].rotate · subtitles[].style.rotate) — 계약 동일:
    도 단위 숫자, -180~180, 시계방향 양수."""
    try:
        r = float(value)
    except (TypeError, ValueError) as ex:
        raise EditOverrideError(f"{where} 가 숫자가 아닙니다 ({value!r})") from ex
    if not (-ROTATE_RANGE_DEG <= r <= ROTATE_RANGE_DEG):
        raise EditOverrideError(
            f"{where} 는 -180~180 (도 단위, 시계방향 양수)이어야 합니다 ({r})")


def _validate_image(i: int, im: Any) -> None:
    """images[i] 스키마 검증 — 정적(파일시스템 무관) 검사만. 순수.

    파일 존재·크기 검증은 run_dir 를 아는 resolve_image_files 가 한다."""
    if not isinstance(im, dict):
        raise EditOverrideError(f"images[{i}] 가 객체가 아닙니다")
    unknown = [k for k in im if k not in IMAGE_KEYS]
    if unknown:
        raise EditOverrideError(
            f"images[{i}]: 모르는 키 {unknown} — 계약은 {'/'.join(IMAGE_KEYS)} "
            "뿐입니다 (구 엔진 노드가 조용히 무시하지 못하게 즉시 거절)")
    file = str(im.get("file") or "").strip()
    if not file:
        raise EditOverrideError(f"images[{i}]: file(run_dir 상대 경로)이 필요합니다")
    if file.startswith(("/", "~")) or ".." in Path(file).parts:
        raise EditOverrideError(
            f"images[{i}]: file 은 run_dir 상대 경로여야 합니다 (받은 값: {file!r})")
    if Path(file).suffix.lower() not in IMAGE_ALLOWED_SUFFIXES:
        raise EditOverrideError(
            f"images[{i}]: 지원 확장자는 {'/'.join(IMAGE_ALLOWED_SUFFIXES)} 뿐입니다 "
            f"(받은 값: {file!r} — 알파가 필요하면 png)")
    try:
        st = float(im["source_time_sec"])
    except (KeyError, TypeError, ValueError) as ex:
        raise EditOverrideError(
            f"images[{i}]: source_time_sec(원본 절대초)이 필요합니다 ({ex})") from ex
    if st < 0:
        raise EditOverrideError(f"images[{i}]: source_time_sec 이 음수입니다 ({st})")
    try:
        d = float(im["duration_sec"])
    except (KeyError, TypeError, ValueError) as ex:
        raise EditOverrideError(f"images[{i}]: duration_sec 이 필요합니다 ({ex})") from ex
    if d <= 0:
        raise EditOverrideError(f"images[{i}]: duration_sec 은 양수여야 합니다 ({d})")
    for k in ("x", "y", "w"):
        try:
            v = float(im[k])
        except (KeyError, TypeError, ValueError) as ex:
            raise EditOverrideError(
                f"images[{i}]: {k}(1080×1920 캔버스 대비 0~1 비율)가 필요합니다 ({ex})") from ex
        if not (0.0 <= v <= 1.0):
            raise EditOverrideError(f"images[{i}]: {k} 는 0~1 비율이어야 합니다 ({v})")
    if im.get("layer") is not None and not isinstance(im["layer"], int):
        raise EditOverrideError(f"images[{i}]: layer 는 정수여야 합니다 ({im['layer']!r})")
    if im.get("rotate") is not None:
        _validate_rotate(f"images[{i}]: rotate", im["rotate"])


def _validate_text(i: int, t: Any) -> None:
    """texts[i] 스키마 검증(F-411) — 전부 정적 검사, 파일 없음. 순수."""
    if not isinstance(t, dict):
        raise EditOverrideError(f"texts[{i}] 가 객체가 아닙니다")
    unknown = [k for k in t if k not in TEXT_KEYS]
    if unknown:
        raise EditOverrideError(
            f"texts[{i}]: 모르는 키 {unknown} — 계약은 {'/'.join(TEXT_KEYS)} "
            "뿐입니다 (구 엔진 노드가 조용히 무시하지 못하게 즉시 거절)")
    text = str(t.get("text") or "")
    if not text.strip():
        raise EditOverrideError(
            f"texts[{i}]: text 가 비어 있습니다 — 그 텍스트를 지우려면 배열에서 빼세요")
    if len(text) > TEXT_MAX_CHARS:
        raise EditOverrideError(
            f"texts[{i}]: text 가 {TEXT_MAX_CHARS}자를 넘습니다 ({len(text)}자) — "
            "긴 설명은 대사 자막으로")
    try:
        st = float(t["source_time_sec"])
    except (KeyError, TypeError, ValueError) as ex:
        raise EditOverrideError(
            f"texts[{i}]: source_time_sec(원본 절대초)이 필요합니다 ({ex})") from ex
    if st < 0:
        raise EditOverrideError(f"texts[{i}]: source_time_sec 이 음수입니다 ({st})")
    try:
        d = float(t["duration_sec"])
    except (KeyError, TypeError, ValueError) as ex:
        raise EditOverrideError(f"texts[{i}]: duration_sec 이 필요합니다 ({ex})") from ex
    if d <= 0:
        raise EditOverrideError(f"texts[{i}]: duration_sec 은 양수여야 합니다 ({d})")
    for k in ("x", "y"):
        try:
            v = float(t[k])
        except (KeyError, TypeError, ValueError) as ex:
            raise EditOverrideError(
                f"texts[{i}]: {k}(글자 중심, 1080×1920 캔버스 대비 0~1 비율)가 "
                f"필요합니다 ({ex})") from ex
        if not (0.0 <= v <= 1.0):
            raise EditOverrideError(f"texts[{i}]: {k} 는 0~1 비율이어야 합니다 ({v})")
    if t.get("size") is not None:
        try:
            sz = float(t["size"])
        except (TypeError, ValueError) as ex:
            raise EditOverrideError(f"texts[{i}]: size 가 숫자가 아닙니다 ({t['size']!r})") from ex
        if not (TEXT_SIZE_RANGE[0] <= sz <= TEXT_SIZE_RANGE[1]):
            raise EditOverrideError(
                f"texts[{i}]: size 는 {TEXT_SIZE_RANGE[0]}~{TEXT_SIZE_RANGE[1]} px "
                f"여야 합니다 ({sz})")
    if t.get("color") is not None and not _COLOR_RE.match(str(t["color"])):
        raise EditOverrideError(
            f"texts[{i}]: color 는 '#RRGGBB' 형식이어야 합니다 (받은 값: {t['color']!r})")
    if t.get("stroke") is not None and t["stroke"] not in TEXT_STROKES:
        raise EditOverrideError(
            f"texts[{i}]: stroke 는 {'/'.join(TEXT_STROKES)} 중 하나여야 합니다 "
            f"(받은 값: {t['stroke']!r})")
    if t.get("fx") is not None and t["fx"] not in TEXT_FX:
        raise EditOverrideError(
            f"texts[{i}]: fx 는 {'/'.join(TEXT_FX)} 중 하나여야 합니다 "
            f"(받은 값: {t['fx']!r})")
    if t.get("font") is not None and t["font"] not in TEXT_FONTS:
        raise EditOverrideError(
            f"texts[{i}]: font 는 번들 폰트 {'/'.join(TEXT_FONTS)} 중 하나여야 합니다 "
            f"(받은 값: {t['font']!r} — 없는 폰트명은 시스템 폰트로 조용히 대체되므로 거절)")
    if t.get("rotate") is not None:
        _validate_rotate(f"texts[{i}]: rotate", t["rotate"])


def overrides_texts(ov: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """v3 texts(F-411) → 정규화된 목록(기본값 채움, 숫자형 통일). 순수.

    반환 항목: {text, source_time_sec, duration_sec, x, y, size, color, stroke, fx,
    rotate, font}. 키가 없으면 None(= 텍스트 오버라이드 없음)."""
    if not ov or ov.get("texts") is None:
        return None
    out: list[dict[str, Any]] = []
    for t in ov["texts"]:
        item = dict(TEXT_DEFAULTS)
        item.update({
            "text": str(t["text"]),
            "source_time_sec": float(t["source_time_sec"]),
            "duration_sec": float(t["duration_sec"]),
            "x": float(t["x"]), "y": float(t["y"]),
        })
        if t.get("size") is not None:
            item["size"] = int(round(float(t["size"])))
        if t.get("color") is not None:
            item["color"] = str(t["color"]).upper()
        for k in ("stroke", "fx", "font"):
            if t.get(k) is not None:
                item[k] = str(t[k])
        if t.get("rotate") is not None:
            item["rotate"] = float(t["rotate"])
        out.append(item)
    return out


def resolve_image_files(ov: dict[str, Any] | None,
                        run_dir: str | Path) -> list[dict[str, Any]]:
    """images[].file(run_dir 상대 경로) → 절대 경로 해석 + 파일 검증(F-408).

    파이프라인 시작 직후(무거운 단계를 돌기 전)에 호출한다 — 없는 파일을 조용히 빼고
    렌더하면 사람이 올린 이미지가 소리 없이 사라진 영상이 나간다(제1원칙). 스토리지
    다운로드는 오케스트레이터 어댑터 몫이라 여기 도달한 시점엔 파일이 run_dir 에
    있어야 정상이다. 검증: run_dir 이탈 재확인(validate 의 정적 검사 + resolve 후
    방어), 존재, 크기 상한 IMAGE_MAX_BYTES. 반환 항목의 file 은 절대 경로 문자열."""
    if not ov or not ov.get("images"):
        return []
    base = Path(run_dir).resolve()
    out: list[dict[str, Any]] = []
    for i, im in enumerate(ov["images"]):
        p = (base / str(im["file"])).resolve()
        if not p.is_relative_to(base):
            raise EditOverrideError(
                f"images[{i}]: file 이 run_dir 를 벗어납니다 (받은 값: {im['file']!r})")
        if not p.is_file():
            raise EditOverrideError(
                f"images[{i}]: 파일이 없습니다: {p} — 오케스트레이터 어댑터가 스토리지에서 "
                "run_dir 로 내려받은 뒤 상대 경로로 보내는 계약입니다")
        size = p.stat().st_size
        if size == 0:
            raise EditOverrideError(f"images[{i}]: 빈 파일입니다: {p.name}")
        if size > IMAGE_MAX_BYTES:
            raise EditOverrideError(
                f"images[{i}]: 파일이 상한을 넘습니다 ({size / 1048576:.1f}MB > "
                f"{IMAGE_MAX_BYTES // 1048576}MB): {p.name}")
        item = dict(im)
        item["file"] = str(p)
        out.append(item)
    return out


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
    """오버라이드 → 자막 세그먼트 목록. 키가 없으면 None. 순수.

    subtitle_segments.json 과 **똑같은 3필드**(start_sec·end_sec·text)로 정규화해서
    돌려준다 — 그 파일이 자막의 정본이고, 호출부는 이 결과를 그대로 캐시에 다시 써서
    다음 렌더·편집실 재방문에서도 사람이 고친 문장이 보이게 한다.

    v3 추가 필드는 있을 때만 얹는다:
      · source_time_sec — 원본 절대초 앵커. **place_anchored_subtitles 를 거쳐
        편집 타임라인으로 변환된 뒤 사라진다**(캐시는 편집본 시간축 정본이라
        원본축 좌표를 섞지 않는다 — 편집실은 source_sec 을 스스로 역산한다).
      · style — {size, y, color, rotate} 정규화(숫자화). 배치 후에도 남아 캐시에
        저장된다 — 오버라이드 없는 재렌더에서도 사람이 고친 스타일이 유지돼야
        하기 때문이다."""
    if not ov or not ov.get("subtitles"):
        return None
    out: list[dict[str, Any]] = []
    for s in ov["subtitles"]:
        item: dict[str, Any] = {"start_sec": float(s["start_sec"]),
                                "end_sec": float(s["end_sec"]),
                                "text": str(s["text"]).strip()}
        if s.get("source_time_sec") is not None:
            item["source_time_sec"] = float(s["source_time_sec"])
        style = s.get("style")
        if style:
            norm: dict[str, Any] = {}
            if style.get("size") is not None:
                norm["size"] = float(style["size"])
            if style.get("y") is not None:
                norm["y"] = float(style["y"])
            if style.get("color") is not None:
                norm["color"] = str(style["color"])
            if style.get("rotate") is not None:
                norm["rotate"] = float(style["rotate"])
            item["style"] = norm
        out.append(item)
    return out


def _clip_spans(clips: list[StoryClip]) -> tuple[list[tuple[float, float, float]], float]:
    """최종 클립 목록 → (원본 start, 원본 end, 편집 base) 스팬 목록 + 총 길이. 순수."""
    spans: list[tuple[float, float, float]] = []
    total = 0.0
    for c in clips or []:
        s, e = float(c.start_sec), float(c.end_sec)
        spans.append((s, e, total))
        total += max(0.0, e - s)
    return spans, total


def _pick_span(spans: list[tuple[float, float, float]],
               t: float) -> tuple[float, float, float] | None:
    """앵커 t 를 담은 스팬 — 정확 포함(start ≤ t < end) 우선, 없으면 슬롭
    ±SUBTITLE_ANCHOR_SLOP_SEC 로 재판정해 가장 가까운 것. 없으면 None(고아). 순수."""
    pick = next((sp for sp in spans if sp[0] <= t < sp[1]), None)
    if pick is None:
        slop = [sp for sp in spans
                if sp[0] - SUBTITLE_ANCHOR_SLOP_SEC <= t <= sp[1] + SUBTITLE_ANCHOR_SLOP_SEC]
        if slop:
            pick = min(slop, key=lambda sp: min(abs(t - sp[0]), abs(t - sp[1])))
    return pick


def place_anchored_subtitles(
    subs: list[dict[str, Any]],
    clips: list[StoryClip],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """v3 앵커 자막 배치(F-401) — source_time_sec(원본 절대초) → 편집 타임라인. 순수.

    tts(_resolve_cue_anchors)와 같은 규칙: 담은 최종 클립의 편집 오프셋 + 클립 내
    상대시각. 포함 판정 슬롭 ±SUBTITLE_ANCHOR_SLOP_SEC(0.5s) — 편집실 UI 와 동일
    규약이다. 정확 포함이 우선이고, 슬롭 매칭은 경계 위 자막을 가장 가까운 클립
    안쪽으로 클램프한다. 이 변환 덕에 clips 와 subtitles 를 **같이 보내도** 구간
    변경이 자막 시각을 흔들지 않는다.

    앵커가 최종 구간 목록에 없으면 tts 고아와 같은 규칙 = **드롭**이다
    (_resolve_cue_anchors 의 "앵커 클립 소재가 최종 타임라인에 없음 → 드롭".
    tts 의 중간 단계인 같은 (chunk,candidate) 조각으로의 스냅은 자막에 그 메타데이터가
    없어 성립하지 않는다). 드롭된 항목을 둘째 반환값으로 돌려주니 호출부는 반드시
    로그로 남겨라 — 조용한 드롭은 제1원칙 위반이다.

    앵커 없는 항목은 종전대로 start_sec(편집본 시간축)를 그대로 쓴다. 반환 목록에서
    source_time_sec 은 제거된다(캐시 = 편집본 시간축 정본). style 등 나머지 필드는
    그대로 통과한다."""
    if not subs:
        return [], []
    spans, total = _clip_spans(clips)

    placed: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for sub in subs:
        t_raw = sub.get("source_time_sec")
        if t_raw is None:
            placed.append(dict(sub))
            continue
        t = float(t_raw)
        pick = _pick_span(spans, t)
        if pick is None:
            dropped.append(sub)
            continue
        s0, e0, base = pick
        rel = min(max(t - s0, 0.0), e0 - s0)       # 슬롭 매칭은 클립 안쪽으로 클램프
        dur = float(sub["end_sec"]) - float(sub["start_sec"])
        start = base + rel
        end = min(start + dur, total)
        if end - start < 0.1:                      # 변환 후 화면에 못 남는 길이 = 고아
            dropped.append(sub)
            continue
        out = {k: v for k, v in sub.items() if k != "source_time_sec"}
        out["start_sec"] = round(start, 3)
        out["end_sec"] = round(end, 3)
        placed.append(out)
    placed.sort(key=lambda x: (float(x["start_sec"]), float(x["end_sec"])))
    return placed, dropped


def place_anchored_images(
    images: list[dict[str, Any]],
    clips: list[StoryClip],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """v3 images 배치(F-408) — source_time_sec(원본 절대초) → 편집 타임라인. 순수.

    자막 앵커(place_anchored_subtitles)와 같은 규칙: 담은 최종 클립의 편집 오프셋 +
    클립 내 상대시각, 포함 판정 슬롭 ±0.5s, 클립 밖 앵커는 tts 고아와 같은 규칙 =
    **드롭**이다(변환 후 0.1s 미만 포함). 드롭 항목은 둘째 반환값 — 호출부는 반드시
    로그로 남겨라(조용한 드롭 = 제1원칙 위반).

    자막과 달리 창 길이가 duration_sec 로 오므로, 편집 창 = [start, start+duration]
    을 타임라인 끝에서 클램프한다. 반환 항목은 편집본 시간축의 start_sec/end_sec 를
    갖고 source_time_sec·duration_sec 은 제거된다(렌더러 입력 = 편집본 시간축).
    file·x·y·w·layer·rotate 는 그대로 통과하며, **배열 순서를 보존한다** — 같은
    layer 의 쌓임 순서가 배열 순서 계약이라 자막처럼 시각순 정렬하면 안 된다."""
    if not images:
        return [], []
    spans, total = _clip_spans(clips)
    placed: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for im in images:
        t = float(im["source_time_sec"])
        pick = _pick_span(spans, t)
        if pick is None:
            dropped.append(im)
            continue
        s0, e0, base = pick
        rel = min(max(t - s0, 0.0), e0 - s0)       # 슬롭 매칭은 클립 안쪽으로 클램프
        start = base + rel
        end = min(start + float(im["duration_sec"]), total)
        if end - start < 0.1:
            dropped.append(im)
            continue
        out = {k: v for k, v in im.items()
               if k not in ("source_time_sec", "duration_sec")}
        out["start_sec"] = round(start, 3)
        out["end_sec"] = round(end, 3)
        placed.append(out)
    return placed, dropped


def place_anchored_texts(
    texts: list[dict[str, Any]],
    clips: list[StoryClip],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """v3 texts 배치(F-411) — images 와 같은 규칙(담은 클립 오프셋 + 상대시각, 슬롭
    ±0.5s, 고아·0.1s 미만 드롭, 끝 클램프, 배열 순서 보존). 반환 항목은 편집본 시간축의
    start_sec/end_sec 를 갖고 source_time_sec·duration_sec 은 제거된다. 드롭 항목은
    둘째 반환값 — 호출부가 반드시 로그로 남긴다."""
    return place_anchored_images(texts, clips)


def overrides_tts(ov: dict[str, Any] | None,
                  old_cues: list[dict[str, Any]] | None = None,
                  ) -> list[dict[str, Any]] | None:
    """오버라이드 → 내레이션 cue 목록(앵커 스키마). tts 키가 없으면 None. 순수.

    호출부는 이 결과로 첫 variant 의 cue pool 을 통째로 바꾼다 — 반드시
    _resolve_cue_anchors **앞**이어야 한다(좌표가 원본 절대초라, 변환은 최종 클립
    확정 후 그쪽이 한다). 빈 배열은 '내레이션 전부 삭제'로 유효하다.

    **앵커 메타데이터는 가장 가까운 옛 cue 에서 물려받는다** — overrides_clips 와
    같은 이유다. 편집실이 보내는 것은 화면에서 만질 수 있는 값(시각·창·목소리·문구)
    뿐인데, cue 에는 화면에 없지만 해석을 좌우하는 값이 더 있다:
      · chunk_index/candidate_index — 소재가 잘렸을 때 어느 조각으로 스냅할지 판정
    문구만 고친 cue(편집실의 대다수 조작)는 source_time_sec 이 그대로라 정확히
    매칭되고, 어디에도 안 붙는 완전히 새 cue 는 -1 로 남는다 — 그 원본 시각이
    최종 타임라인에 살아 있으면 위치 매핑은 되고, 잘렸으면 드롭된다(의미상 맞다)."""
    if not ov or "tts" not in ov:
        return None
    olds = old_cues or []
    out: list[dict[str, Any]] = []
    for c in ov["tts"]:
        st = float(c["source_time_sec"])
        src = None
        best = TTS_MATCH_TOLERANCE_SEC
        for o in olds:
            if o.get("source_time_sec") is None:
                continue
            d = abs(float(o["source_time_sec"]) - st)
            if d <= best:
                src, best = o, d
        dur = (float(c["duration_sec"]) if c.get("duration_sec") is not None
               else float(src.get("duration_sec", 3.0)) if src else 3.0)
        out.append({
            "text": str(c["text"]).strip(),
            "source_time_sec": st,
            "duration_sec": dur,
            "voice": str(c.get("voice") or (src.get("voice") if src else "") or "ko_female"),
            "speed": str(c.get("speed") or (src.get("speed") if src else "") or "normal"),
            "chunk_index": int(src.get("chunk_index", -1)) if src else -1,
            "candidate_index": int(src.get("candidate_index", -1)) if src else -1,
        })
    out.sort(key=lambda x: x["source_time_sec"])
    return out


def total_duration(clips: list[StoryClip]) -> float:
    """구간 합계(초). 편집실 상한(쇼츠 3분 한계) 검증과 로그용. 순수."""
    return sum(float(c.end_sec) - float(c.start_sec) for c in clips or [])
