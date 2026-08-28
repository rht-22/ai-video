"""E19-1 스타일 톤 프로파일 — 채널 단위 prompt 노브 묶음.

발주서: `docs/prompts/e19-drama-clip-preset.md` §1. **값 정본은 프리셋 파일**
(`docs/design_presets/<이름>.preset.json` 의 `status:"prompt"` 항목)이고, 이 모듈이
읽는 것은 그중 프롬프트에 들어갈 부분만 추린 `app/data/style_tones/<이름>.json` 이다 —
두 파일이 갈리면 `tests/test_e19_style_tone.py` 가 잡는다.

노브 10여 개(내레이션 톤·캡·relay, 라벨 어휘·색·밀도·동기, 러닝 개그, 엔딩 컷 …)를
각각 design 키로 열면 어댑터 미러 + 구 엔진 argparse 롤아웃을 10번 반복한다. 그래서
**`--style-tone <이름>` 플래그 하나**가 유일한 입구이고, 어휘는 엔진이 든다(스티커
manifest 와 같은 규율).

- **미지정이면 아무 일도 없다** — 프롬프트 블록 함수는 None 에 빈 문자열을 돌려주고
  E15 하드캡(MAX_TEXTS 8)도 그대로다. 프롬프트 문자열이 한 글자도 달라지지 않는다
  (회귀 0 — E13 의 프롬프트 동결 규율).
- 없는 프로파일 이름·깨진 파일·모르는 enum 값은 **즉시 실패**(StyleToneError) —
  조용히 기본 톤으로 떨어지면 '프리셋 적용했는데 왜 그대로지'가 된다
  (E11 transcribe-backend 와 같은 규율).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "style_tone/v1"
TONES_DIR = Path(__file__).resolve().parent.parent / "data" / "style_tones"

# 라벨 밀도 캡의 상한 — 프로파일이 E15 하드캡(8)을 키울 수 있는 한계(발주서 §1).
DENSITY_MAX_LIMIT = 20

# enum 어휘 — 프로파일은 엔진 번들이라 고정 어휘로 좁게 시작한다. 값을 늘리려면
# 여기와 아래 프롬프트 문구 표를 **같이** 늘린다(문구 없는 enum 은 로드에서 죽는다).
NARRATION_TONES = ("recall_first_person",)
NARRATION_PLACEMENTS = ("dialogue_gaps_only",)
STORY_STRUCTURES = ("single_scene_rule_of_three",)
TITLE_TONES = ("community_meme",)
ENDINGS = ("hard_cut",)
LABEL_CATEGORIES = ("state_paren", "meme_tsukkomi", "wordplay")
COLOR_MAP_KEYS = ("state", "comment", "tsukkomi", "laugh", "positive")
# E19-5 — SFX 절(선택). 절이 없는 프로파일 = SFX 닫힘(발주서 §5 게이트).
SFX_BEATS = ("rage", "surprise", "action_foley")
SFX_GAIN_RANGE = (-30.0, 0.0)
SFX_MAX_LIMIT = 10
# E19-9 — subtitle 절(선택). 절이 없는 프로파일 = 자막 분할 없음(종전 2줄 랩 그대로).
SUBTITLE_LINE_CHARS_RANGE = (4, 40)
# E20-B1 — pacing 절(선택). 절이 없는 프로파일 = 발화 갭 페이싱 없음(회귀 0).
PACING_RANGES = {
    "max_speech_gap_sec": (0.5, 5.0),
    "gap_residual_sec": (0.1, 2.0),
    "head_lead_in_sec": (0.0, 3.0),
    "tail_hold_sec": (0.0, 5.0),
}

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class StyleToneError(ValueError):
    """프로파일 로드·검증 실패 — 호출부는 조용히 넘기지 말고 죽어야 한다."""


@dataclass(frozen=True)
class StyleTone:
    name: str
    sha12: str            # 프로파일 파일 바이트의 sha256[:12] — run_log provenance 용
    narration: dict[str, Any]
    labels: dict[str, Any]
    story: dict[str, Any]
    # E19-5 — 없으면 None = 이 채널의 SFX 는 닫혀 있다(프롬프트에도 안 실리고
    # 플랜에 실려 와도 validate_plan 이 전량 드롭+기록한다).
    sfx: dict[str, Any] | None = None
    # E19-9 — 없으면 None = 자막 단일 줄 분할 없음(종전 2줄 랩 그대로, 회귀 0).
    subtitle: dict[str, Any] | None = None
    # E20-B1 — 없으면 None = 발화 갭 페이싱 없음(회귀 0).
    pacing: dict[str, Any] | None = None

    @property
    def density_max(self) -> int:
        return int(self.labels["density_max"])


def available_tones() -> list[str]:
    """번들된 프로파일 이름 목록 — 오류 메시지에 실어 오타를 한 번에 잡게 한다."""
    if not TONES_DIR.is_dir():
        return []
    return sorted(p.stem for p in TONES_DIR.glob("*.json"))


def _fail(name: str, msg: str) -> None:
    raise StyleToneError(f"스타일 톤 프로파일 '{name}': {msg}")


def _need(data: dict[str, Any], key: str, typ: type, name: str, where: str) -> Any:
    v = data.get(key)
    if not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
        _fail(name, f"{where}.{key} 는 {typ.__name__} 이어야 합니다({v!r})")
    return v


def _need_range(data: dict[str, Any], key: str, name: str, where: str,
                lo_min: float, hi_max: float) -> list[float]:
    v = data.get(key)
    if (not isinstance(v, list) or len(v) != 2
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
        _fail(name, f"{where}.{key} 는 [최소, 최대] 두 숫자여야 합니다({v!r})")
    lo, hi = float(v[0]), float(v[1])
    if not (lo_min <= lo < hi <= hi_max):
        _fail(name, f"{where}.{key} 범위가 잘못됐습니다({v!r} — {lo_min}~{hi_max} 안, 최소<최대)")
    return [lo, hi]


def _need_enum(data: dict[str, Any], key: str, allowed: tuple[str, ...],
               name: str, where: str) -> str:
    v = data.get(key)
    if v not in allowed:
        _fail(name, f"{where}.{key} 는 {'/'.join(allowed)} 중 하나여야 합니다({v!r})")
    return str(v)


def validate_tone_data(data: Any, name: str) -> dict[str, Any]:
    """프로파일 dict 검증 — 파일 없이도 테스트할 수 있게 로드와 분리해 둔다."""
    if not isinstance(data, dict):
        _fail(name, f"JSON 객체여야 합니다({type(data).__name__})")
    if data.get("schema") != SCHEMA:
        _fail(name, f"schema 는 {SCHEMA!r} 이어야 합니다({data.get('schema')!r})")
    if data.get("name") != name:
        _fail(name, f"name 필드({data.get('name')!r})가 파일 이름과 다릅니다 — "
                    f"프로파일을 복사해 만들 때 name 갱신을 잊은 전형이라 즉시 실패시킨다")

    nar = _need(data, "narration", dict, name, "")
    _need_enum(nar, "tone", NARRATION_TONES, name, "narration")
    mc = _need(nar, "max_cues", int, name, "narration")
    if not (1 <= mc <= 10):
        _fail(name, f"narration.max_cues 는 1~10 이어야 합니다({mc})")
    _need_range(nar, "cue_len_chars", name, "narration", 1, 40)
    # 김부장 실측(2026-08-28) 후속: cue 창(duration_sec)이 스토리 기본(2~6초)대로 나오면
    # 대사가 빽빽한 소재에서 들어갈 gap 이 없다(3/3 경고). 초단문 톤은 창도 짧아야 한다.
    # 선택 필드 — 없으면 창 지시를 안 얹는다(종전 프롬프트 그대로).
    if nar.get("cue_duration_sec") is not None:
        _need_range(nar, "cue_duration_sec", name, "narration", 0.5, 6.0)
    # E20-B2 — 선택: 대사 없는 틈마다 cue 를 배치하라는 지시(오디오가 비지 않게)
    if nar.get("fill_gaps") is not None:
        _need(nar, "fill_gaps", bool, name, "narration")
    _need(nar, "relay_rule", bool, name, "narration")
    _need_enum(nar, "placement", NARRATION_PLACEMENTS, name, "narration")

    lab = _need(data, "labels", dict, name, "")
    cats = _need(lab, "categories", list, name, "labels")
    if not cats or not set(cats) <= set(LABEL_CATEGORIES):
        _fail(name, f"labels.categories 는 {'/'.join(LABEL_CATEGORIES)} 의 비어있지 않은 "
                    f"부분집합이어야 합니다({cats!r})")
    cmap = _need(lab, "color_map", dict, name, "labels")
    if sorted(cmap) != sorted(COLOR_MAP_KEYS):
        _fail(name, f"labels.color_map 키는 정확히 {sorted(COLOR_MAP_KEYS)} 여야 합니다"
                    f"({sorted(cmap)})")
    for k, v in cmap.items():
        if not isinstance(v, str) or not _HEX_COLOR.match(v):
            _fail(name, f"labels.color_map.{k} 는 #RRGGBB 여야 합니다({v!r})")
    dm = _need(lab, "density_max", int, name, "labels")
    if not (1 <= dm <= DENSITY_MAX_LIMIT):
        _fail(name, f"labels.density_max 는 1~{DENSITY_MAX_LIMIT} 이어야 합니다({dm})")
    _need_range(lab, "duration_sec", name, "labels", 0.2, 6.0)
    for k in ("sync_to_speech", "fill_gaps", "payoff_clean", "running_gag"):
        _need(lab, k, bool, name, "labels")

    st = _need(data, "story", dict, name, "")
    _need_enum(st, "structure", STORY_STRUCTURES, name, "story")
    _need_enum(st, "title_tone", TITLE_TONES, name, "story")
    _need_enum(st, "ending", ENDINGS, name, "story")
    _need(st, "payoff_longtake", bool, name, "story")
    # E20-B3 — 선택: 훅 상한·제목-훅 일치(김부장 v3 실측 — 도입 설전 28초 뒤에야
    # 제목의 장면이 왔고 그마저 클램프에 잘렸다)
    if st.get("hook_max_sec") is not None:
        hm = st.get("hook_max_sec")
        if (not isinstance(hm, (int, float)) or isinstance(hm, bool)
                or not (3.0 <= float(hm) <= 20.0)):
            _fail(name, f"story.hook_max_sec 는 3~20 숫자여야 합니다({hm!r})")
    if st.get("hook_title_scene") is not None:
        _need(st, "hook_title_scene", bool, name, "story")

    # E19-5 — sfx 절은 선택이다(없음 = SFX 닫힘). 있으면 전부 검증한다.
    if data.get("sfx") is not None:
        sx = _need(data, "sfx", dict, name, "")
        mx = _need(sx, "max_per_episode", int, name, "sfx")
        if not (1 <= mx <= SFX_MAX_LIMIT):
            _fail(name, f"sfx.max_per_episode 는 1~{SFX_MAX_LIMIT} 이어야 합니다({mx})")
        g = sx.get("mix_gain_db")
        if (not isinstance(g, (int, float)) or isinstance(g, bool)
                or not (SFX_GAIN_RANGE[0] <= float(g) <= SFX_GAIN_RANGE[1])):
            _fail(name, f"sfx.mix_gain_db 는 {SFX_GAIN_RANGE[0]:g}~{SFX_GAIN_RANGE[1]:g} "
                        f"이어야 합니다({g!r})")
        beats = _need(sx, "target_beats", list, name, "sfx")
        if not beats or not set(beats) <= set(SFX_BEATS):
            _fail(name, f"sfx.target_beats 는 {'/'.join(SFX_BEATS)} 의 비어있지 않은 "
                        f"부분집합이어야 합니다({beats!r})")

    # E20-B1 — pacing 절은 선택이다(없음 = 발화 갭 페이싱 없음). 있으면 전부 검증한다.
    if data.get("pacing") is not None:
        pc = _need(data, "pacing", dict, name, "")
        vals: dict[str, float] = {}
        for key, (lo, hi) in PACING_RANGES.items():
            v = pc.get(key)
            if (not isinstance(v, (int, float)) or isinstance(v, bool)
                    or not (lo <= float(v) <= hi)):
                _fail(name, f"pacing.{key} 는 {lo:g}~{hi:g} 숫자여야 합니다({v!r})")
            vals[key] = float(v)
        if vals["gap_residual_sec"] >= vals["max_speech_gap_sec"]:
            _fail(name, "pacing.gap_residual_sec 이 max_speech_gap_sec 이상이면 "
                        f"컷이 성립하지 않습니다({vals['gap_residual_sec']:g} ≥ "
                        f"{vals['max_speech_gap_sec']:g})")
        # E20-B4 — 선택: 스토리라인 발화 커버리지 하한(0~1 비율).
        if pc.get("min_speech_coverage") is not None:
            mc = pc.get("min_speech_coverage")
            if (not isinstance(mc, (int, float)) or isinstance(mc, bool)
                    or not (0.1 <= float(mc) <= 0.95)):
                _fail(name, f"pacing.min_speech_coverage 는 0.1~0.95 비율이어야 "
                            f"합니다({mc!r})")

    # E19-9 — subtitle 절은 선택이다(없음 = 단일 줄 분할 없음). 있으면 전부 검증한다.
    if data.get("subtitle") is not None:
        sub = _need(data, "subtitle", dict, name, "")
        _need(sub, "single_line", bool, name, "subtitle")
        mlc = _need(sub, "max_line_chars", int, name, "subtitle")
        lo, hi = SUBTITLE_LINE_CHARS_RANGE
        if not (lo <= mlc <= hi):
            _fail(name, f"subtitle.max_line_chars 는 {lo}~{hi} 이어야 합니다({mlc})")
    return data


def load_style_tone(name: str) -> StyleTone:
    """`app/data/style_tones/<name>.json` → StyleTone. 실패는 전부 StyleToneError."""
    if not re.match(r"^[a-z0-9_]{1,64}$", name or ""):
        raise StyleToneError(
            f"스타일 톤 이름이 형식에 맞지 않습니다({name!r}) — 소문자/숫자/밑줄만")
    path = TONES_DIR / f"{name}.json"
    if not path.is_file():
        raise StyleToneError(
            f"스타일 톤 프로파일 '{name}' 이 없습니다({path}) — "
            f"번들된 프로파일: {', '.join(available_tones()) or '(없음)'}")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise StyleToneError(f"스타일 톤 프로파일 '{name}' 파싱 실패: {e}") from e
    validate_tone_data(data, name)
    return StyleTone(
        name=name,
        sha12=hashlib.sha256(raw).hexdigest()[:12],
        narration=dict(data["narration"]),
        labels=dict(data["labels"]),
        story=dict(data["story"]),
        sfx=dict(data["sfx"]) if data.get("sfx") is not None else None,
        subtitle=dict(data["subtitle"]) if data.get("subtitle") is not None else None,
        pacing=dict(data["pacing"]) if data.get("pacing") is not None else None,
    )


# ── 프롬프트 블록 ───────────────────────────────────────────────────────────
# enum → 문구 표. 블록은 기존 프롬프트 **뒤에 덧붙는 절**이다 — 본문 문자열은
# 건드리지 않는다(톤 미지정 실행의 프롬프트가 한 글자도 안 바뀌는 회귀 0 조건).

_STRUCTURE_TEXT = {
    "single_scene_rule_of_three":
        "- 스토리 구조: **완결된 한 장면**을 통으로 잡는 storyline 을 최우선하라. 그 안에서 "
        "같은 패턴이 2번 반복된 뒤 3번째에 비틀리는 구간(rule of three)·반복 리듬 장치"
        "(카운트다운 등)가 있으면 그 장면이 1순위다.",
}
_TITLE_TONE_TEXT = {
    "community_meme":
        "- 제목 톤: 커뮤니티 밈 화법의 한 문장 훅(예: \"군 입대하고 들은 가장 X같은 질문\"). "
        "격식체 헤드라인 금지. 필요하면 X 같은 자체검열 표기를 그대로 써라.",
}
_ENDING_TEXT = {
    "hard_cut":
        "- 엔딩: 마지막 클립은 마지막 대사(또는 감탄 리액션)가 끝나는 지점에서 **즉시** "
        "끝나야 한다. 여운·정적 꼬리를 남기지 마라(루프 재생 유도).",
}
_NARRATION_TONE_TEXT = {
    "recall_first_person":
        "- 문체: **1인칭 회상체 초단문**. 주인공 시점 과거형으로 전환·반전만 잇는 접착제형 "
        "cue 다(예: \"고민하는데\", \"물어본 선임을 찍었죠\", \"지목당했고\"). 설명형·"
        "헤드라인체 금지. 위 [텍스트 톤] 절의 권장 결(명사형 종결 등)보다 **이 문체가 이긴다**.",
}
_CATEGORY_TEXT = {
    "state_paren":
        "  ① 괄호 상태 라벨 — 인물·심리·행동을 3~6자 괄호로: \"(폐급 선임)\" \"(꿋꿋함)\" "
        "\"(놀람)\" \"(두리번 두리번)\"",
    "meme_tsukkomi":
        "  ② 커뮤니티 밈 훈수 — 시청자 댓글 같은 편집자 드립: \"웃참 2222\" \"난가???\" "
        "\"귀에서 피남\" \"축 당첨\"",
    "wordplay":
        "  ③ 의성·워드플레이 — \"똑 똑\" 같은 짧은 추임새(인물 머리 양옆 분할 배치 가능)",
}


def story_prompt_block(tone: StyleTone | None) -> str:
    """스토리 구성 프롬프트에 덧붙는 채널 톤 절. tone=None 이면 빈 문자열(회귀 0)."""
    if tone is None:
        return ""
    nar, st = tone.narration, tone.story
    lo, hi = (int(nar["cue_len_chars"][0]), int(nar["cue_len_chars"][1]))
    lines = [
        "",
        f"[채널 톤 프로파일 — {tone.name}]",
        "이 채널의 편 구성 문법이다. 위 일반 규칙과 충돌하면 **이 절이 이긴다**.",
        _STRUCTURE_TEXT[st["structure"]],
        _TITLE_TONE_TEXT[st["title_tone"]],
        _ENDING_TEXT[st["ending"]],
    ]
    if st["payoff_longtake"]:
        lines.append(
            "- 페이오프 보존: 페이오프(핵심 대답·반전) 구간은 하나의 클립으로 통째로 유지하라 "
            "— 잘게 쪼개지 마라. 그 구간의 지루함은 잦은 자막 회전이 상쇄한다.")
    # E20-B3 — 훅 규칙
    if st.get("hook_title_scene"):
        lines.append(
            "- 제목-훅 일치: 제목이 약속하는 바로 그 장면(사건의 순간)을 **첫 클립**으로 "
            "써라. 첫 2초 안에 사건·행동·강한 대사 중 하나가 화면에 있어야 한다 — "
            "배경 설명·설전 장면으로 열지 마라(그건 build 자리다).")
    if st.get("hook_max_sec") is not None:
        lines.append(
            f"- 훅 상한: hook 클립은 {st['hook_max_sec']:g}초 이내로 — 도입을 길게 "
            f"끌면 첫 이탈 곡선이 무너진다(위 클립 길이 가이드보다 이 값이 이긴다).")
    lines += [
        "",
        f"[채널 톤 — TTS cue 규칙 (위 'TTS cue 작성'의 톤·길이·개수 규칙을 다음으로 대체한다)]",
        _NARRATION_TONE_TEXT[nar["tone"]],
        f"- 길이: 한 cue {lo}~{hi}자.",
        f"- 개수: storyline 전체 {int(nar['max_cues'])}개 이하 — 대사가 서사를 끌고, cue 는 "
        f"대사 사이 틈만 잇는다.",
    ]
    if nar.get("cue_duration_sec") is not None:
        dlo, dhi = (float(nar["cue_duration_sec"][0]), float(nar["cue_duration_sec"][1]))
        lines.append(
            f"- 창: 한 cue 의 duration_sec 은 {dlo:g}~{dhi:g}초 — 초단문은 짧게 말하고 "
            f"끝난다(위 '보통 2~6초' 대신 이 값). 창이 길면 대사 틈에 들어가지 못한다.")
    if nar["placement"] == "dialogue_gaps_only":
        lines.append("- 배치: **대사가 없는 틈에만** 둔다. 대사 위에 겹치는 cue 는 만들지 마라.")
    if nar["relay_rule"]:
        lines.append(
            "- 릴레이: 직전 대사가 질문이면 cue 가 그 질문을 받아치는 문구를 우선하라"
            "(대사 \"근데 이거 어디다 버리지?\" → cue \"누구나 갈 수 있는 곳\").")
    # E20-B2 — 내레이션 갭 채움(오디오가 비지 않게)
    if nar.get("fill_gaps"):
        lines.append(
            "- 공백 채우기: 대사가 없는 1.5초 이상의 틈**마다** cue 를 배치해 **오디오가 "
            "비지 않게** 하라 — 개수 상한 안에서 틈이 남으면 cue 를 더 써라. 이 채널 "
            "문법의 정체는 내레이션이 모든 틈을 잇는 릴레이다.")
    return "\n".join(lines) + "\n"


def style_prompt_block(tone: StyleTone | None) -> str:
    """연출(E15 style_compose) 프롬프트에 덧붙는 채널 톤 절. tone=None 이면 빈 문자열."""
    if tone is None:
        return ""
    lab = tone.labels
    cmap = lab["color_map"]
    dlo, dhi = (float(lab["duration_sec"][0]), float(lab["duration_sec"][1]))
    lines = [
        "",
        f"[채널 톤 프로파일 — {tone.name} · 연출 어휘]",
        "이 채널의 효과 텍스트(라벨) 문법이다. 위 일반 규칙과 충돌하면 **이 절이 이긴다** "
        "(특히 '2~5개면 충분' 권고 — 이 채널은 아래 상한까지 촘촘히 쓰는 문법이다).",
        "- 계열 3가지:",
        *[_CATEGORY_TEXT[c] for c in lab["categories"]],
        f"- 색 문법(color 값): 상황·심리(괄호 라벨)={cmap['state']} · 논평·콜아웃={cmap['comment']} · "
        f"편집자 훈수={cmap['tsukkomi']} · 웃음(ㅋㅋ)={cmap['laugh']} · 긍정 감정={cmap['positive']}",
        f"- duration_sec 은 {dlo:g}~{dhi:g}초 — 길게 띄우지 않는다.",
        f"- 이 채널의 효과 텍스트 상한은 {tone.density_max}개다(위 '상한' 줄의 효과 텍스트 "
        f"개수 대신 이 값을 쓴다).",
    ]
    if "state_paren" in lab["categories"]:
        lines.append(
            "- 괄호형 상태 라벨에는 fx:\"glow\"(같은 색 발광 테두리)를 써라 — "
            "훈수·워드플레이는 pop/shake/none 그대로.")
    if lab["sync_to_speech"]:
        lines.append(
            "- 동기: 라벨은 해당 대사/내레이션이 **시작되는 시각과 같은 시각**에 띄워라 — "
            "발화의 시각 반주다. 발화 없는 허공 시각에 두지 마라(아래 fill_gaps 예외뿐).")
    if lab["fill_gaps"]:
        lines.append(
            "- 공백 채우기: 대사·내레이션이 모두 비는 리액션 구간(웃음·정적)에는 훈수 라벨"
            "(ㅋㅋㅋㅋㅋ 류)을 우선 배치하라 — 화면에 읽을 것이 없는 순간을 만들지 않는다.")
    if lab["running_gag"]:
        lines.append(
            "- 러닝 개그: 같은 밈을 2~3번 변주해 반복하라"
            "(\"축 당첨\" → \"또 당첨 추카추카\" → \"3번째 당첨 ㅋㅋ\").")
    if lab["payoff_clean"]:
        lines.append(
            "- ⚠ 페이오프(핵심 대답·반전·카타르시스) 구간에는 **아무 연출도 얹지 마라** — "
            "웃기는 것은 대사 자신이다. 연출은 그 앞뒤에서 비켜선다.")
    return "\n".join(lines) + "\n"
