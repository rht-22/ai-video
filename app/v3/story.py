"""Stage 3 — story. v3 의 심장부: 여기서부터 **시각 비접촉 구간**이다.

입력은 Stage 1+2 기록과 grid 뿐 — 영상은 다시 보지 않는다. Flash 가 span id 로만
비트(beat)를 편성하고, 확정 시각은 전부 grid lookup 이다(Stage 2 와 같은 구조 —
모델은 시각을 아예 출력하지 않는다).

역할 분담:
  모델(Flash · 텍스트 온리): 템플릿 선택 · 비트별 아이템 열(대사 span 연속
    범위 + 내레이션 다리 — 2026-09-01 재설계) · 카피
    (제목 2줄 · 내레이션 대본 · 괄호 라벨).
  코드(결정성): id 검증·반려(≤MAX_REASKS) · 길이 예산 다듬기(통삭제 금지 ·
    보호 목록 · arousal 은 동점 타이브레이커 ±0.5 상한 — §9-B 계약) ·
    TTS 슬롯 배치(ⓐ무성 → ⓑimportance≤3 뮤트 → ⓒ불가 시 드랍+기록) ·
    최소 1개 보장(재질의 소진 시 highlight 코드 폴백).

내레이션 견적은 0.6초 + 7.0자/초(공백 제외 — 실측 회귀). 실제 오디오는 resources
단계에서 합성·fit 되므로 여기 견적은 슬롯 크기 판정에만 쓴다.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.modules.gemini_client import (
    _extract_json_from_markdown,
    _loads_first_json,
)
from app.v3 import schemas
from app.v3.seq_analyze import MAX_REASKS

SCHEMA_STORY = "v3_story/v1"
TEMPLATES = ("recap_dialogue", "highlight")   # 기본 제공(종전 그대로 — 회귀 0)

# ── 스토리 템플릿 레지스트리 (2026-08-31, laeebly 벤치마크 6편 실측 후속) ──────
# 기본 2종의 프롬프트 문구는 PROMPT_TEMPLATE 본문에 그대로 박혀 있다(바이트 불변) —
# 레지스트리의 desc 는 **추가(extra) 템플릿의 것만** 프롬프트에 덧붙는다. 채널이
# --story-templates 로 열지 않으면 검증기도 프롬프트도 종전과 완전히 같다.
# required_roles 는 검증기가 강제한다(recap 의 climax 필수를 일반화한 것 — 반려
# 문구는 종전과 같은 형식).
#
# 신규 2종의 근거(2026-08-31 laeebly 상위작 프레임 해부 — 뜨비·빡빡이횽·누나스픽 포함):
# · conflict_payoff — 쇼츠몽 40.8만(에스컬레이션→반격→충격 프리즈)·옥상평상 36.9만
#   (펀치라인 4.9s 감속)·뜨비 리텐션 105.6%(끝=시작 같은 상태 행동 루프)의 공통 문법.
# · chemi_observe — 누나스픽 도깨비 10주년 여행 22.9만·자매 채널 리텐션 110~120%:
#   갈등 없이 '관전 과제 + 인물 역할 별명 + 전원 피크 + 리캡 루프'가 반복 시청을 만든다.
STORY_TEMPLATE_SPECS: dict[str, dict] = {
    "recap_dialogue": {"required_roles": ("climax",), "extra": False, "desc": ""},
    "highlight": {"required_roles": (), "extra": False, "desc": ""},
    "conflict_payoff": {
        "required_roles": ("turn", "payoff"), "extra": True,
        "desc": (
            "- conflict_payoff(갈등형 — 드라마·콩트): 비트 역할 = hook(갈등 정점의 "
            "대사 한복판에서 시작 — 상황 설명 span 금지) → escalate(같은 갈등이 한 "
            "단계씩 세지는 대사 인용 2~3비트) → turn(반격·재맥락화 선언 — **핵심 대사 "
            "span 은 자르지 말고 통째로**, 여기서만 호흡을 늦춘다) → payoff(최대 "
            "펀치라인 한 방) → loop_ending(당한 쪽 리액션 직후 **즉시 컷** — 해소·"
            "정리·화해 대사 금지. 마지막 상태가 hook 의 갈등과 같은 상태면 루프 재생이 "
            "이어진다). 내레이션은 전환부에만 최소로."),
    },
    "chemi_observe": {
        "required_roles": ("ensemble",), "extra": True,
        "desc": (
            "- chemi_observe(케미 관찰형 — 출연진 조합 예능): 비트 역할 = "
            "observe_hook(내레이션이 관전 과제를 선언 — 예: \"짧은 순간에 성격 다 "
            "나옴\") → member_moment(인물 한 명의 순간, 인물 수만큼 반복 — 라벨은 "
            "그 인물의 역할 **별명**(예: \"(츤데레 작은오빠)\")으로 붙이고 같은 인물이 "
            "다시 나오면 같은 별명을 재사용한다) → ensemble(전원이 한 화면에서 웃음이 "
            "동시에 터지는 피크) → recap_loop(제목을 되받는 내레이션 한 줄을 얹고 최대 "
            "웃음 직후 **즉시 컷**). 갈등이 없어도 된다 — 관계·성격 대비가 긴장을 "
            "대신한다."),
    },
}
STORY_TARGET_SEC = 53.0      # recap 템플릿 기준(레퍼런스 53s) — 채널 노브
STORY_MAX_SEC = 60.0         # 쇼츠 상한 아래 여유 — 초과분은 예산 다듬기가 던다
PIECES_MIN, PIECES_MAX = 6, 8   # 편성 조각 수 지향(합격 기준 분포 — soft)
# 내레이션 길이 견적 = 고정 오버헤드 + 자수/속도. **두 항이다** — 합성물 앞뒤에
# 붙는 무음이 짧은 문장에서 지배적이라 자수/속도만으로는 창이 늘 모자란다.
# 실측(2026-09-01 · ElevenLabs eleven_multilingual_v2 · ko_female/normal · 9문장
# 선형회귀): 길이(초) ≈ 0.588 + 0.1428 × 공백제외자수 → 오버헤드 0.59s · 7.0자/초.
# 종전 식(자수/7.5, 오버헤드 0)은 6자에서 실제의 69%만 잡아 cue 가 **전량 잘렸다**
# (실측: 창 1.0s vs 실제 1.44s · 4/4 트림).
# ⚠ 백엔드 비대칭 — edge-tts 는 같은 문장이 2.25s(≈3.6자/초)라 이 값으로도 잘린다.
# 운영이 ElevenLabs 이므로 그쪽 기준으로 잡되, 폴백 실행은 트림 경고가 남는다.
NARRATION_CPS = 7.0          # 순수 발화 자수/초(공백 제외)
NARRATION_LEAD_SEC = 0.6     # 합성 앞뒤 고정 무음
NARRATION_MIN_SEC = 1.0      # 이보다 작은 슬롯은 슬롯이 아니다
AROUSAL_TIEBREAK_MAX = 0.5   # §9-B: arousal 은 보조지표 — importance 동점 근처에서만
# 비트 머리 데드에어 컷(2026-09-02, 사용자 지시 "배치하고도 남으면 잘라야") —
# 머리 무성은 내레이션 창 재료라 페이싱이 보호하는데, 창 확정 **후** 남는 잔여를
# 정리하는 규칙이 없어 도입 2.3초 데드에어가 발행됐다(e748690c 실사고).
# 판정은 전부 기계 실측 산술(창 시작 = 실측 mp3 길이 역산)이라 LLM 무관.
HEAD_LEAD_NAR_SEC = 0.3      # 내레이션 창 앞에 남기는 리드
HEAD_LEAD_DIALOGUE_SEC = 1.0  # 창 없는 머리의 대사 앞 호흡(E20-B1 head_lead_in 과 동값)
# 내레이션 배속 사다리(2026-09-02, 사용자 지시 "너무 길면 배속, 드랍 절대 금지" +
# "기본적으로 배속 상태가 쇼츠 호흡에 맞는다") — **기본이 fast(1.1)** 이고 창이
# 그보다 작으면 very_fast(1.2, ElevenLabs 허용 최대)로 흡수, 축약(전보문 위험)은
# 최후다. 값은 E11 EL_SPEED 계약에서 온다. 기본 배속은 창 견적도 줄여 자리
# 확보·축약 발동을 함께 줄인다(실사고: 조사 '건'이 축약에 지워짐).
NARRATION_BASE_SPEED = "fast"
NARRATION_BASE_RATE = 1.1
NARRATION_SPEED_LADDER = (("fast", 1.1), ("very_fast", 1.2))
TAIL_PAD_MAX_SEC = 0.25      # 유성 꼬리 클리핑 보정(Whisper 단어 끝이 빡빡함) 상한
MUTE_MAX_IMPORTANCE = 3      # ⓑ: 이 이하 유성 span 만 뮤트 후보(ⓒ: ≥4 는 절대 불가)
TITLE_MAX_CHARS = 16         # 상단 밴드 2줄 각각의 실측 상한(템플릿 폭 990px)
# 대화 페이싱(2026-09-01, 사용자 편집 지침) — "같은 장면이어도 대사만 바로바로
# 이어서 보여주느라 컷을 한다. 한 장면을 길게 보여주면 루즈하다." 사람 편집 실측
# (리플레이 하네스 614편)의 클립 중앙 4.8s 가 그 리듬이다. 비트 안 대사 사이의
# 무성 run 이 이 값을 넘으면 긴 무성 span 부터 덜어 점프컷을 만든다(잔여가 호흡).
DIALOGUE_MAX_GAP_SEC = 1.5   # 대사 사이 무성 허용 총량 — 넘는 만큼 잘라 붙인다
SILENT_BEAT_CAP_SEC = 2.5    # 무성 전용 비트(silent_break) 총량 상한 — 8.7s 실사고
LOW_CONF = 0.5               # 이 아래 확신도는 재료 목록에 [저확신] 표기(M10-B)


# ── 재료 색인(순수) ─────────────────────────────────────────────────────────

def build_span_index(stage2_doc: dict, grid: dict) -> tuple[dict[str, dict], list[str]]:
    """분석된 chunk 의 span 들 → id 색인 + grid 순서 목록.

    Stage 3 의 재료는 **분석된 chunk 뿐**이다(커버리지 밖 span 을 편성하면 근거 없는
    컷이 된다). grid 의 t_in 순서가 곧 span 의 전역 순서다."""
    _gspans = sorted(grid.get("span_candidates") or [],
                     key=lambda s: (float(s["t_in"]), s["id"]))
    grid_order = [sp["id"] for sp in _gspans]
    grid_pos = {sid: i for i, sid in enumerate(grid_order)}
    # 문장 이어짐(2026-09-02, "받아야 / 사업도 확장하고" 반토막 실사고) — 길이
    # 규칙(44자/6초)이 문장을 중간에서 가른 자리는 단어가 **바로 붙어**(갭<0.12s)
    # 이어진다. 자연스러운 문장 경계는 0.5초 침묵 규칙으로 갈리므로 이 갭이 안
    # 나온다. 종결부호가 있으면(Whisper 는 드물게 찍는다) 문장 끝으로 믿는다.
    # 재료 표에 ↪ 로 표시하고, 검증이 "한쪽만 고른 편성"을 반려한다.
    _cont: dict[str, str] = {}
    _pause_pair: dict[str, str] = {}   # 쉼-조각: b ← a ("큰일 … 날 뻔했네요" 실사고)
    for a, b in zip(_gspans, _gspans[1:]):
        if not (a.get("is_audio") and b.get("is_audio")):
            continue
        gap = float(b["t_in"]) - float(a["t_out"])
        txt = str(a.get("text") or "").strip()
        no_end = bool(txt) and txt[-1] not in ".?!…\"'」』)]"
        if gap < 0.12 and no_end:
            _cont[a["id"]] = b["id"]
        elif gap < 2.0 and no_end and len(txt.split()) <= 2:
            # 짧은 조각(1~2어절·무종결)이 쉼 뒤 조각으로 이어지는 꼴 — 한 문장이
            # 쉼으로 쪼개졌을 수 있다. 확정 불가라 반려가 아니라 재료 표 마커+
            # 프롬프트 지시로만 민다(2026-09-02 "큰일" 절단 실사고).
            _pause_pair[b["id"]] = a["id"]

    index: dict[str, dict] = {}
    mi = -1                       # 전역 meaning 번호 — 다리(내레이션) 점프 판정의 단위
    for si, sq in enumerate(stage2_doc.get("sequences") or []):
        for ch in sq.get("chunks") or []:
            for m in ch.get("meanings") or []:
                mi += 1
                for s in m.get("spans") or []:
                    sid = s.get("span_id")
                    if not isinstance(sid, str) or sid not in grid_pos:
                        continue
                    index[sid] = {
                        "t_in": schemas.parse_ts(s["time"]["start"]),
                        "t_out": schemas.parse_ts(s["time"]["end"]),
                        "is_audio": bool(s.get("is_audio")),
                        "importance": int(s.get("importance") or 3),
                        "audio_script": s.get("audio_script") or [],
                        # M9-C 판정 결과 — 자막 생성이 이걸 봐야 한다(리뷰 확정
                        # critical: 판정이 화면에 전파되지 않던 결함)
                        "text_source": s.get("text_source"),
                        "heard_text": s.get("heard_text") or "",
                        "conf": s.get("conf"),
                        "scene_script": s.get("scene_script") or "",
                        # 인물(2026-09-03): 내레이션 걸음이 '지금 누구를 보는지'를 알아야
                        # 전환을 말로 잇는다 — Stage 2 가 이미 적어 둔 것을 옮겨 담는다
                        "characters": [str(x) for x in (s.get("characters") or []) if x],
                        "meaning_characters": [str(x) for x in (m.get("characters") or []) if x],
                        "meaning_content": m.get("content") or "",
                        "mood": m.get("mood") or "",
                        "meaning_idx": mi,
                        "sequence_idx": si,
                        "pos": grid_pos[sid],
                        # 주 피사체 가로 위치(2026-09-02, 무성 인서트 선택 필드) —
                        # 조립이 클립 단위로 접어 크롭 앵커 재료로 쓴다
                        "subject_pos": s.get("subject_pos"),
                        # 같은 문장이 다음 span 으로 이어짐(위 _cont) — 분석 밖
                        # 다음 span 은 요구할 수 없으니 색인에 있을 때만 산다
                        "continues_to": _cont.get(sid),
                        "continues_from": next(
                            (a for a, t in _cont.items() if t == sid), None),
                        "pause_cont_from": _pause_pair.get(sid),
                    }
    # 미분석 무성 계층(2026-09-02, 사용자 결정 ⓑ) — 내레이션 창이 모자랄 때
    # 분석 밖 **무성** span 까지 자리로 끌어온다. 화면 검증이 안 된 재료라는
    # 위험을 사용자가 수용했다(대사 없는 구간 한정 — 유성은 여전히 분석 필수).
    # 모델 재료 목록(order)·재료 표에는 안 나간다 — 자리 보강 코드만 쓴다.
    analyzed = set(index)
    for sp in _gspans:
        sid = sp["id"]
        if sid in index or sp.get("is_audio"):
            continue
        nxt = next((index[s] for s in grid_order[grid_pos[sid] + 1:]
                    if s in analyzed), None)
        prv = next((index[s] for s in reversed(grid_order[:grid_pos[sid]])
                    if s in analyzed), None)
        near = nxt or prv
        if near is None:
            continue
        index[sid] = {
            "t_in": float(sp["t_in"]), "t_out": float(sp["t_out"]),
            "is_audio": False, "importance": 2, "audio_script": [],
            "text_source": None, "heard_text": "", "conf": None,
            "scene_script": "", "meaning_content": "", "mood": "",
            "meaning_idx": near["meaning_idx"],
            "sequence_idx": near["sequence_idx"],
            "pos": grid_pos[sid], "subject_pos": None, "continues_to": None,
            "unanalyzed": True,
        }
    order = sorted((sid for sid in index if not index[sid].get("unanalyzed")),
                   key=lambda sid: index[sid]["pos"])
    return index, order


def arousal_adjust(arousal: list[dict], t0: float, t1: float) -> float:
    """구간 평균 arousal score → 타이브레이커 보정치(±AROUSAL_TIEBREAK_MAX 클램프).

    §9-B 계약: 전 장르 공통 피처의 z-합이라 크기를 믿지 않는다 — 방향만 쓰고
    상한으로 자른다. 포인트가 없으면 0(무보정)."""
    vals = [float(p["score"]) for p in arousal or []
            if t0 <= float(p.get("t", -1)) < t1 and isinstance(p.get("score"), (int, float))]
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    return max(-AROUSAL_TIEBREAK_MAX, min(AROUSAL_TIEBREAK_MAX, m * AROUSAL_TIEBREAK_MAX))


# ── 모델 응답 검증(순수) ────────────────────────────────────────────────────

def validate_story_response(resp: Any, span_index: dict[str, dict],
                            span_order: list[str],
                            allowed_templates: tuple[str, ...] = TEMPLATES,
                            max_sec: float | None = None,
                            require_proto: bool = False,
                            ) -> tuple[dict | None, list[str], list[str]]:
    """모델 응답 → (정규화 스토리 | None, 반려 사유, 보정 노트).

    비트의 span_ids 는 **분석된 span 의 grid 연속 범위**여야 한다(부분 발췌·원거리
    결합은 비트를 나눠서 — 비트 하나 = 소스에서 이어지는 한 덩어리). 비트 간
    span 재사용 금지. 편성 순서는 자유다(원거리 결합).

    require_proto=True 는 **모델 응답 검증 전용 백도어 폐쇄**다(2026-09-01) — 구
    스키마(span_ids+narration, lines 없음)로 온 비트를 반려한다. 구 스키마 승자는
    why·topic 규율을 건너뛰고, items 가 없어 페이싱의 내레이션 창 보호·앵커식 슬롯
    배치가 전부 무장해제된다(실사고 3건: why 0/6 ×2 · 내레이션 3개 전멸 발행).
    기본 False 는 코드 폴백·기존 테스트의 구 스키마 경로 유지(회귀 0)."""
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(resp, dict):
        return None, ["응답이 객체가 아니다"], []

    template = resp.get("template")
    _model_allowed = tuple(t for t in allowed_templates if t != "highlight")
    if template not in _model_allowed:
        # highlight 는 재질의 소진 시 **코드 폴백 전용**이다 — 모델이 고르면 topic·
        # why·내레이션 규율을 전부 건너뛴 나열이 된다(실사고: cohesion 0.0 무혈입성).
        problems.append(f"template 은 {_model_allowed} 중 하나: {template!r}"
                        + (" — highlight 는 코드 폴백 전용이다, 서사를 짜라"
                           if template == "highlight" else ""))

    title = resp.get("title")
    if not isinstance(title, dict) or not str(title.get("line1") or "").strip() \
            or not str(title.get("line2") or "").strip():
        problems.append("title 은 {line1, line2} 두 줄 모두 필요")
        title = {"line1": "", "line2": ""}
    line1 = str(title.get("line1") or "").strip()
    line2 = str(title.get("line2") or "").strip()
    for name, line in (("line1", line1), ("line2", line2)):
        if len(line) > TITLE_MAX_CHARS:
            problems.append(f"title.{name} 이 {len(line)}자 — {TITLE_MAX_CHARS}자 이내로")

    beats_in = resp.get("beats")
    if not isinstance(beats_in, list) or not beats_in:
        return None, problems + ["beats 배열이 없다"], []

    # 모델이 고를 수 있는 재료는 **분석된** span 뿐 — 미분석 무성(ⓑ 계층)은 자리
    # 보강 코드 전용이다(재료 표에도 없는 id 를 모델이 부르면 종전대로 무시+노트).
    pos_of = {sid: span_index[sid]["pos"] for sid in span_index
              if not span_index[sid].get("unanalyzed")}
    used: set[str] = set()
    beats: list[dict] = []
    for k, b in enumerate(beats_in):
        if not isinstance(b, dict):
            problems.append(f"beats[{k}] 가 객체가 아님")
            continue
        role = str(b.get("role") or "").strip() or "build"
        # 프로토 계약(2026-09-01 교체 — interval-proto 8/25 실증 이식) —
        # 비트 = 의미 단위: {action, role, lines(비연속 span id), visual, why,
        # link, narration}. lines 는 **연속일 필요가 없다** — 필요 없는 대사를 뺀
        # 자리가 컷이 된다(클립 = pos 인접 런, 조립이 나눈다 — 시각 정본 유지).
        # why = topic 전개에서 이 비트가 하는 일, link = 장소·인물·시간 도약이
        # 어떻게 성립하는지 — 모델이 구성 시점에 도약을 자기 증명한다.
        if isinstance(b.get("lines"), list):
            lines_in = [str(x) for x in (b.get("lines") or [])]
            vis_in = [str(x) for x in (b.get("visual") or [])
                      if isinstance(x, str)]
            ids = lines_in + [v for v in vis_in if v not in lines_in]
            if not ids:
                problems.append(f"beats[{k}] lines 가 비었다")
                continue
            unknown = [x for x in ids if x not in pos_of]
            if unknown:
                notes.append(f"beats[{k}] 없는 조각 id {len(unknown)}개 무시: "
                             f"{unknown[:3]}")
                ids = [x for x in ids if x in pos_of]
                if not ids:
                    problems.append(f"beats[{k}] 쓸 조각이 없다")
                    continue
            ids.sort(key=lambda x: pos_of[x])
            # 비트 내부 구멍(2026-09-02 실사고: reaction 비트 안 25.6초 점프 —
            # 화재 소동에서 소송 대화로 설명 없이 낙하): 처음엔 반려였으나
            # **반려 폭풍 실사고**(같은 날 EP02 — 3회×3안 전량 탈락 → highlight
            # 폴백·내레이션 0)로 자동 수리로 강등. 반려 문구가 시키던 일("구멍
            # 앞뒤를 별도 비트로 나누라")을 run_story 의 split_beats_at_holes 가
            # 기계적으로 한다 — 재료 불변·경계만 승격이라 내용 훼손이 없다.
            _hole = next(
                ((a2, b2, span_index[b2]["t_in"] - span_index[a2]["t_out"])
                 for a2, b2 in zip(ids, ids[1:])
                 if span_index[b2]["t_in"] - span_index[a2]["t_out"] > 5.0),
                None)
            if _hole:
                notes.append(
                    f"beats[{k}] 안 {_hole[2]:.0f}초 구멍({_hole[0]}→{_hole[1]}) — "
                    "채택 시 비트 분할로 수리")
            reused = [x for x in ids if x in used]
            if reused:
                problems.append(f"beats[{k}] span 재사용: {reused[:5]} — "
                                "한 조각은 한 비트에만")
                continue
            # 문장 반토막 금지(2026-09-02) — ↪이어짐 조각을 골랐으면 짝도 함께.
            # 짝이 분석 밖이면 요구할 수 없어 통과(색인에 있는 짝만 검사).
            half = [x for x in ids
                    if span_index[x].get("continues_to")
                    and span_index[x]["continues_to"] in pos_of
                    and span_index[x]["continues_to"] not in ids]
            front = [x for x in ids
                     if span_index[x].get("continues_from")
                     and span_index[x]["continues_from"] in pos_of
                     and span_index[x]["continues_from"] not in ids]
            if half or front:
                problems.append(
                    f"beats[{k}] 문장 반토막: "
                    + (f"{half[:3]} 는 다음 조각으로 이어지고 " if half else "")
                    + (f"{front[:3]} 는 앞 조각에서 이어진다 " if front else "")
                    + "(재료 표의 ↪) — 짝을 함께 넣거나 둘 다 빼라")
                continue
            used.update(ids)
            raw_nar = b.get("narration")
            if isinstance(raw_nar, dict):
                nar_text = str(raw_nar.get("text") or "").strip()
            elif isinstance(raw_nar, str):
                nar_text = raw_nar.strip()
            else:
                nar_text = ""
            narration = [nar_text] if nar_text else []
            items = None
            if narration:
                # 앵커식 배치 재사용 — nar 를 비트 머리 리드인으로(프로토 방식)
                items = [{"kind": "nar", "text": nar_text, "span_ids": [],
                          "label": None},
                         {"kind": "mat", "text": None, "span_ids": list(ids),
                          "label": None}]
            # 내레이션 자리 보강(2026-09-02, "드랍 절대 금지"의 상류) — 비트 머리가
            # 유성인데 다리를 썼으면 **코드가 인접 무성 span 을 끼워 자리를 만든다**
            # (재료는 grid 에 있고 모델이 빼먹었을 뿐이다). 처음엔 반려로 했다가
            # EP02 실측에서 9안 전멸→폴백을 만들어 강등했다(반려 폭풍이 폴백보다
            # 나쁘다). 못 끼우면 노트만 — 배치가 드랍하고 루브릭이 감점한다.
            if narration \
                    and not (isinstance(raw_nar, dict) and raw_nar.get("span_ids")):
                # 필요량 = 기본 배속 기준 견적. 머리의 무성 런이 그에 못 미치면
                # 인접 무성 span(분석·미분석 불문 — ⓑ)을 끼워 창을 채운다.
                need = NARRATION_LEAD_SEC \
                    + len("".join(nar_text.split())) / NARRATION_CPS \
                    / NARRATION_BASE_RATE
                have = 0.0
                for sid2 in ids:
                    sp2 = span_index[sid2]
                    if sp2["is_audio"]:
                        break
                    have += sp2["t_out"] - sp2["t_in"]
                by_pos = {sp["pos"]: sid2 for sid2, sp in span_index.items()}
                added = []
                cur = span_index[ids[0]]
                while have < need - 0.05 and len(added) < 4:
                    cand = by_pos.get(cur["pos"] - 1)
                    if cand is None or cand in used or cand in ids \
                            or span_index[cand]["is_audio"] \
                            or abs(span_index[cand]["t_out"] - cur["t_in"]) > 0.05:
                        break
                    ids.insert(0, cand)
                    added.insert(0, cand)
                    have += span_index[cand]["t_out"] - span_index[cand]["t_in"]
                    cur = span_index[cand]
                if added:
                    ext = sum(1 for x in added
                              if span_index[x].get("unanalyzed"))
                    notes.append(f"beats[{k}] 내레이션 자리 자동 보강 — 무성 리드인 "
                                 f"{added} 삽입(미분석 {ext}건)")
                elif have < need - 0.05:
                    notes.append(f"beats[{k}] 내레이션 자리 부족({have:.1f}s/"
                                 f"{need:.1f}s) — 배속·축약으로 흡수 시도")
            why = str(b.get("why") or "").strip()[:120]
            if not why:
                # why 없는 비트는 topic 기여를 증명 못 한 비트다 — 규율 우회 차단
                problems.append(f"beats[{k}] 에 why(이 비트가 topic 전개에서 하는 일 "
                                "한 줄)가 없다")
                continue
            beats.append({"role": role, "span_ids": ids,
                          "narration": narration, "labels": [], "items": items,
                          "action": str(b.get("action") or "").strip()[:80],
                          "why": why,
                          "link": (str(b.get("link")).strip()[:120]
                                   if b.get("link") else None)})
            continue
        if require_proto:
            # 백도어 폐쇄 — 모델이 구 스키마로 답하면 반려해 재질의로 돌린다.
            # 사유를 구체적으로 적어야 다음 시도가 형태를 고친다(반려 문구 규율).
            problems.append(
                f"beats[{k}] 가 구 스키마다(lines 없음) — 비트는 반드시 "
                "{action, lines, visual, why, link, narration} 프로토 계약으로. "
                "span_ids 키는 받지 않는다")
            continue
        # 아이템 열 스키마(2026-09-01 재설계) — 비트 = 소리 순서대로의 아이템 열.
        # {"nar": …, "span_ids"?} = 내레이션(선택 장면), {"span_ids": …, "label"?} =
        # 대사·장면. items 가 없으면 구 스키마(span_ids+narration+labels) 그대로 —
        # 코드 폴백·옛 테스트가 이 길로 온다(require_proto=False 한정 · 회귀 0).
        items: list[dict] | None = None
        raw_items = b.get("items")
        if isinstance(raw_items, list) and raw_items:
            items = []
            bad_items = []
            for j, it in enumerate(raw_items):
                if not isinstance(it, dict):
                    bad_items.append(f"beats[{k}] items[{j}] 가 객체가 아님")
                    continue
                nar = it.get("nar")
                iids = it.get("span_ids") or []
                if not isinstance(iids, list) \
                        or not all(isinstance(x, str) for x in iids):
                    bad_items.append(f"beats[{k}] items[{j}] span_ids 는 문자열 배열")
                    continue
                if isinstance(nar, str) and nar.strip():
                    items.append({"kind": "nar", "text": nar.strip(),
                                  "span_ids": list(iids), "label": None})
                elif iids:
                    lab = it.get("label")
                    items.append({"kind": "mat", "text": None,
                                  "span_ids": list(iids),
                                  "label": str(lab).strip() if lab else None})
                else:
                    bad_items.append(
                        f"beats[{k}] items[{j}] 는 nar 또는 span_ids 가 필요")
            if bad_items:
                problems.extend(bad_items)
                continue
            if not any(it["kind"] == "mat" for it in items):
                # nar 전용 비트 — 마무리 내레이션을 제 비트로 내는 자연스러운 실수다
                # (시도2 실측 2건). 반려 대신 **직전 비트 꼬리로 접합**한다 — 꼬리
                # nar 는 앵커식 배치가 여운 컷 위에 정확히 얹는다. 첫 비트면 붙일
                # 곳이 없어 반려.
                if beats:
                    prev = beats[-1]
                    prev_items = prev.get("items")
                    if not prev_items:
                        prev_items = [{"kind": "mat", "text": None,
                                       "span_ids": list(prev["span_ids"]),
                                       "label": None}]
                    prev_items.extend(items)
                    prev["items"] = prev_items
                    prev["narration"] = (prev.get("narration") or []) + \
                        [it["text"] for it in items]
                    notes.append(f"beats[{k}] nar 전용 비트 → 직전 비트 꼬리로 접합")
                    continue
                problems.append(f"beats[{k}] 에 대사·장면 아이템이 하나는 필요 — "
                                "nar 만으로는 비트가 안 된다(화면 재료가 없다)")
                continue
            ids = [x for it in items for x in it["span_ids"]]
        else:
            ids = b.get("span_ids")
        if not isinstance(ids, list) or not ids \
                or not all(isinstance(s, str) for s in ids):
            problems.append(f"beats[{k}] span_ids 는 문자열 id 배열이어야 한다")
            continue
        unknown = [s for s in ids if s not in pos_of]
        if unknown:
            problems.append(f"beats[{k}] 모르는/분석 밖 span id: {unknown[:5]} — "
                            "재료 목록의 id 로만 골라라")
            continue
        positions = [pos_of[s] for s in ids]
        if positions != sorted(positions) \
                or any(b2 - a2 != 1 for a2, b2 in zip(positions, positions[1:])):
            problems.append(f"beats[{k}] span_ids 가 grid 연속 범위가 아니다: "
                            f"{ids[0]}~{ids[-1]} — 떨어진 구간은 비트를 나눠라")
            continue
        reused = [s for s in ids if s in used]
        if reused:
            problems.append(f"beats[{k}] span 재사용: {reused[:5]} — 한 span 은 한 비트에만")
            continue
        used.update(ids)
        if items is not None:
            # 아이템 경로 — 내레이션·라벨은 아이템에서 파생된다.
            narration = [it["text"] for it in items if it["kind"] == "nar"]
            labels = []
            for it in items:
                if it["kind"] == "nar" and it["span_ids"]:
                    # nar 장면은 무성·imp≤3 만 — imp≥4 유성 위에 얹으면 대사를 죽인다
                    bad = [x for x in it["span_ids"]
                           if span_index[x]["is_audio"]
                           and span_index[x]["importance"] > MUTE_MAX_IMPORTANCE]
                    if bad:
                        notes.append(f"beats[{k}] nar 장면 {bad[:3]} 는 importance≥4 "
                                     "유성 — 자동 배치로 대체")
                        it["span_ids"] = []
                if it["kind"] == "mat" and it.get("label"):
                    text = it["label"]
                    if not (text.startswith("(") and text.endswith(")")):
                        notes.append(f"beats[{k}] 라벨 괄호 보정: {text!r}")
                        text = f"({text.strip('()')})"
                        it["label"] = text
                    labels.append({"text": text, "span_id": it["span_ids"][0]})
            beats.append({"role": role, "span_ids": list(ids),
                          "narration": narration, "labels": labels,
                          "items": items})
            continue
        # M11-A: 내레이션은 **짧은 문장 배열** — 레퍼런스 실측(1.4~2.2s ×2)의 리듬.
        # 단일 문자열도 계속 받는다(하위호환).
        raw_nar = b.get("narration")
        if isinstance(raw_nar, str):
            narration = [raw_nar.strip()] if raw_nar.strip() else []
        elif isinstance(raw_nar, list):
            narration = [str(x).strip() for x in raw_nar if str(x).strip()]
        else:
            narration = []

        # M11-B: 라벨은 **복수 + span 앵커** — 레퍼런스는 대사 순간에 붙는다.
        raw_labels = b.get("labels")
        if raw_labels is None and b.get("label") is not None:
            raw_labels = [b["label"]]            # 하위호환: 단일 라벨 → 첫 span 앵커
        labels: list[dict] = []
        for li, item in enumerate(raw_labels or []):
            if isinstance(item, str):
                text, anchor = item.strip(), ids[0]
            elif isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                anchor = item.get("span_id") or ids[0]
            else:
                problems.append(f"beats[{k}] labels[{li}] 형식 오류")
                continue
            if not text:
                continue
            if anchor not in ids:
                problems.append(f"beats[{k}] labels[{li}] 앵커 {anchor!r} 가 이 비트의 "
                                f"span 이 아니다 — 비트 안 span 을 골라라")
                continue
            if not (text.startswith("(") and text.endswith(")")):
                notes.append(f"beats[{k}] 라벨 괄호 보정: {text!r}")
                text = f"({text.strip('()')})"
            labels.append({"text": text, "span_id": anchor})
        beats.append({"role": role, "span_ids": list(ids),
                      "narration": narration, "labels": labels, "items": None})

    # topic 필수(프로토 계약 — 척추 없는 편성은 나열이다). lines 스키마를 쓴
    # 응답에만 요구한다 — 구 스키마·폴백은 topic 개념이 없다(하위호환).
    if beats and any(bt.get("why") for bt in beats) \
            and not str(resp.get("topic") or "").strip():
        problems.append("topic(이 쇼츠가 결국 무엇에 관한 이야기인지 한 문장)이 없다 — "
                        "topic 이 척추다")

    # 비트는 원본 시간 순서(프로토 계약) — 어긋나면 pos 순으로 정렬 + 노트
    if beats and any(isinstance(bt.get("span_ids"), list) and bt["span_ids"]
                     for bt in beats):
        firsts = [span_index[bt["span_ids"][0]]["pos"] for bt in beats
                  if bt.get("span_ids") and bt["span_ids"][0] in span_index]
        if firsts != sorted(firsts):
            notes.append("비트가 시간순이 아니다 — 원본 순서로 정렬")
            beats.sort(key=lambda bt: span_index[bt["span_ids"][0]]["pos"]
                       if bt.get("span_ids") and bt["span_ids"][0] in span_index
                       else 10 ** 9)
    # 시퀀스를 넘는 도약은 link(또는 내레이션) 필수 — "작품을 모르는 사람이 한 번
    # 보고 따라갈 수 있는가"의 최소 담보. 프로토 계약의 link 를 코드가 존재 검증.
    for i in range(1, len(beats)):
        pb, cb = beats[i - 1], beats[i]
        if not pb.get("span_ids") or not cb.get("span_ids"):
            continue
        a, z = pb["span_ids"][-1], cb["span_ids"][0]
        if a not in span_index or z not in span_index:
            continue
        sa = span_index[a].get("sequence_idx")
        sz = span_index[z].get("sequence_idx")
        if sa is not None and sz is not None and sa != sz \
                and not cb.get("link") and not cb.get("narration"):
            problems.append(
                f"beats[{i}] 는 다른 시퀀스로 넘어가는데 link 도 내레이션도 없다 — "
                "이 도약이 어떻게 성립하는지 link 에 밝히거나 다리 내레이션을 놓아라")
        # 실험 정책(2026-09-02, 사용자 지시 "장면 전환마다 내레이션") — 소스에서
        # 5초 넘게 건너뛰는 전환 비트는 다리 내레이션을 요구한다. ⚠ 처음엔 반려로
        # 했다가 EP02 에서 2연속 반려 폭풍(16건)→폴백을 만들어 **노트+심사 감점**
        # 으로 강등했다(score_story 의 gap-bridge 계수가 강제한다 — 다리 없는 안은
        # 심사에서 진다). link 문구만으로는 부족하다(화면에 안 보인다).
        gap = span_index[z]["t_in"] - span_index[a]["t_out"]
        if gap > 5.0 and not cb.get("narration") and cb.get("why"):
            notes.append(
                f"beats[{i}] 장면 전환(소스 {gap:.0f}초 건너뜀)에 다리 내레이션 "
                "없음 — 심사 감점")

    # 길이 상한 — 프롬프트 권고가 아니라 **반려**다(2026-09-01). 실측 3판 연속
    # 상한의 1.5~2배 계획(115s·122s) → 코드 트림이 절반을 들어내 아크가 무너졌다
    # (엔딩 떡밥·다리가 트림에 증발). 1.2배까지는 트림이 다듬는다 — 그 이상은
    # 모델이 다시 고르는 것이 낫다(코드는 자를 줄만 알지 고를 줄 모른다).
    if max_sec is not None and beats:
        _total = story_duration(beats, span_index)
        if _total > max_sec * 1.2:
            _per = " · ".join(
                f"{bt['role']} {sum(span_index[x]['t_out'] - span_index[x]['t_in'] for x in bt['span_ids']):.0f}s"
                for bt in beats if bt.get("span_ids"))
            problems.append(
                f"편성 합계 {_total:.0f}초 — 상한 {max_sec:.0f}초를 크게 넘는다. "
                f"비트별 합: {_per}. 코드가 잘라주지 않는다(자르면 아크가 무너진다) — "
                f"재료 표의 길이 열을 더해 가며 상한 안으로 **다시 골라서** 내라. "
                f"긴 비트에서 덜 중요한 줄부터 빼라.")

    # 템플릿별 필수 역할 — recap 의 climax 필수를 레지스트리로 일반화(반려 문구 동일)
    _spec = STORY_TEMPLATE_SPECS.get(template) or {}
    if beats:
        for _need in _spec.get("required_roles") or ():
            if not any(b["role"] == _need for b in beats):
                problems.append(f"{template} 는 {_need} 비트가 하나 필요하다")
    if problems:
        return None, problems, notes
    return {"template": template, "reason": str(resp.get("reason") or "").strip(),
            "topic": str(resp.get("topic") or "").strip()[:120],
            "title": {"line1": line1, "line2": line2}, "beats": beats}, [], notes


def story_duration(beats: list[dict], span_index: dict[str, dict]) -> float:
    return sum(span_index[s]["t_out"] - span_index[s]["t_in"]
               for b in beats for s in b["span_ids"])


# ── 대화 페이싱(순수) ───────────────────────────────────────────────────────

def apply_dialogue_pacing(beats: list[dict], span_index: dict[str, dict],
                          *, max_gap_sec: float = DIALOGUE_MAX_GAP_SEC,
                          silent_beat_cap: float = SILENT_BEAT_CAP_SEC) -> list[dict]:
    """비트 안 대사 사이의 무성 시간을 덜어 점프컷을 만든다. 반환: 제거 로그.

    쇼츠의 리듬은 컷이다 — 같은 장면이어도 대사끼리 바로 이어 붙인다(사용자 편집
    지침 · 사람 편집 실측 클립 중앙 4.8s). 규칙:
    · **대사 사이** 무성 run 만 본다 — 비트 머리(리드인)·꼬리(여운)는 안 건드린다
      (내레이션 창·여운 컷의 재료다).
    · run 총량이 max_gap_sec 를 넘으면 **긴 span 부터** 덜어낸다(동점은 이른 쪽) —
      남는 짧은 span 이 호흡이 된다. span 단위로만 던다(클립 경계 = span 경계 벨트).
    · nar 아이템이 지정한 장면(span_ids)·nar 앵커 직전 리드인은 보호한다 — 페이싱이
      내레이션 자리를 먹으면 안 된다(E19-3 과 같은 규율).
    · 무성 전용 비트(silent_break 류)는 총량을 silent_beat_cap 으로 자른다(앞부터
      보존) — 8.7s 정적 실사고.
    비트가 중간에서 비연속이 되면 조립이 클립을 나눈다 — 그게 점프컷이다."""
    removed: list[dict] = []
    for bi, b in enumerate(beats):
        ids = b.get("span_ids") or []
        if len(ids) < 2:
            continue
        # 보호 집합 — nar 지정 장면 + nar 앵커 직전 리드인(견적 길이만큼)
        protected: set[str] = set()
        items = b.get("items") or []
        for j, it in enumerate(items):
            if it.get("kind") != "nar":
                continue
            protected.update(it.get("span_ids") or [])
            if not it.get("span_ids"):
                nxt = next((x for x in items[j + 1:]
                            if x.get("kind") == "mat" and x.get("span_ids")), None)
                if nxt:
                    voiced = [x for x in nxt["span_ids"] if x in span_index
                              and span_index[x]["is_audio"]]
                    if voiced:
                        est = max(NARRATION_MIN_SEC, NARRATION_LEAD_SEC
                                  + len("".join(it["text"].split())) / NARRATION_CPS)
                        anchor_t = span_index[voiced[0]]["t_in"]
                        for sid in ids:
                            sp = span_index.get(sid)
                            # 창에 **걸치는** span 전부 보호 — 시작만 보면 창을
                            # 덮는 긴 span 이 잘려 리드인이 사라진다(테스트 실측)
                            if sp and not sp["is_audio"] \
                                    and sp["t_out"] > anchor_t - est - 0.2 \
                                    and sp["t_in"] < anchor_t:
                                protected.add(sid)

        voiced_pos = [k for k, sid in enumerate(ids)
                      if sid in span_index and span_index[sid]["is_audio"]]
        drop: list[str] = []
        if not voiced_pos:
            # 무성 전용 비트 — 총량 캡(앞부터 보존). ⚠ **마지막 비트·리빌 역할은
            # 예외다**: 무성 엔딩(발견·리빌)은 호흡이 아니라 결말이다 — 캡이 발견
            # 장면을 앞 2.5초만 남기고 잘라 제목이 약속한 화면이 증발한 실사고
            # (2026-09-01 "마지막 장면이 안 나왔는데?"). 캡은 중간 호흡 전용.
            if bi == len(beats) - 1 or b.get("role") in ("ending", "payoff"):
                continue
            acc = 0.0
            for sid in ids:
                sp = span_index.get(sid)
                if sp is None:
                    continue
                d = sp["t_out"] - sp["t_in"]
                if acc + d > silent_beat_cap and sid not in protected:
                    drop.append(sid)
                else:
                    acc += d
        else:
            lo, hi = voiced_pos[0], voiced_pos[-1]
            run: list[str] = []

            def flush(run: list[str]) -> None:
                total = sum(span_index[x]["t_out"] - span_index[x]["t_in"]
                            for x in run)
                cands = sorted((x for x in run if x not in protected),
                               key=lambda x: (-(span_index[x]["t_out"]
                                               - span_index[x]["t_in"]),
                                              span_index[x]["t_in"]))
                for x in cands:
                    if total <= max_gap_sec:
                        break
                    total -= span_index[x]["t_out"] - span_index[x]["t_in"]
                    drop.append(x)

            for k in range(lo + 1, hi):
                sid = ids[k]
                sp = span_index.get(sid)
                if sp is not None and not sp["is_audio"]:
                    run.append(sid)
                else:
                    if run:
                        flush(run)
                    run = []
            if run:
                flush(run)
        for sid in drop:
            b["span_ids"].remove(sid)
            sp = span_index[sid]
            removed.append({"beat": bi, "span_id": sid,
                            "sec": round(sp["t_out"] - sp["t_in"], 3)})
        if drop and items:
            keep = set(b["span_ids"])
            for it in items:
                it["span_ids"] = [x for x in it["span_ids"] if x in keep]
            b["items"] = [it for it in items
                          if it.get("kind") == "nar" or it["span_ids"]]
            b["labels"] = [{"text": it["label"], "span_id": it["span_ids"][0]}
                           for it in b["items"]
                           if it.get("kind") == "mat" and it.get("label")]
    return removed


# ── 길이 예산(순수) ─────────────────────────────────────────────────────────

def split_beats_at_holes(beats: list[dict], span_index: dict[str, dict],
                         gap_sec: float = 5.0) -> list[dict]:
    """비트 내부 5초+ 구멍을 비트 **경계로 승격**시킨다(자동 수리). 반환: 분할 기록.

    2026-09-02: 구멍 반려가 반려 폭풍(3회×3안 전량 탈락 → highlight 폴백)을 일으켜
    강등 — 반려 문구가 시키던 "구멍 앞뒤를 별도 비트로 나누라"를 코드가 한다.
    재료(span)는 그대로고 경계만 생기므로 내용 훼손이 없다. 아이템 배분:
    mat 은 조각별 소속 span 으로 갈라지고(라벨은 원 첫 span 을 담은 조각에),
    nar 는 **다음 mat 이 속한 파트의 머리**로 간다(다리 의도 유지 — 구멍 위
    nar 였다면 자연히 전환 비트의 다리가 된다). 뒤에 mat 이 없으면 마지막 파트."""
    recs: list[dict] = []
    out: list[dict] = []
    for bi, b in enumerate(beats):
        ids = [x for x in b["span_ids"] if x in span_index]
        parts: list[list[str]] = [[ids[0]]] if ids else []
        holes: list[float] = []
        for prev, cur in zip(ids, ids[1:]):
            g = span_index[cur]["t_in"] - span_index[prev]["t_out"]
            if g > gap_sec:
                parts.append([cur])
                holes.append(round(g, 1))
            else:
                parts[-1].append(cur)
        if len(parts) <= 1:
            out.append(b)
            continue
        part_of = {sid: pi for pi, part in enumerate(parts) for sid in part}
        its = b.get("items") or []
        part_items: list[list[dict]] = [[] for _ in parts]
        for j, it in enumerate(its):
            if it["kind"] == "nar":
                nxt = next((x for x in its[j + 1:]
                            if x["kind"] == "mat" and x["span_ids"]), None)
                pi = part_of.get(nxt["span_ids"][0], len(parts) - 1) \
                    if nxt else len(parts) - 1
                part_items[pi].append(dict(it))
                continue
            first = it["span_ids"][0] if it.get("span_ids") else None
            for pi in range(len(parts)):
                kept = [x for x in (it.get("span_ids") or [])
                        if part_of.get(x) == pi]
                if not kept:
                    continue
                piece = dict(it)
                piece["span_ids"] = kept
                if it.get("label") and part_of.get(first) != pi:
                    piece["label"] = None      # 라벨은 원 앵커 조각에만
                part_items[pi].append(piece)
        for pi, part in enumerate(parts):
            nb = dict(b)
            nb["span_ids"] = part
            if its:
                nb["items"] = part_items[pi]
                nb["labels"] = [{"text": x["label"], "span_id": x["span_ids"][0]}
                                for x in part_items[pi]
                                if x["kind"] == "mat" and x.get("label")]
            pset = set(part)
            nb["muted_span_ids"] = [m for m in (b.get("muted_span_ids") or [])
                                    if m in pset]
            out.append(nb)
        recs.append({"beat": bi, "role": b.get("role"),
                     "parts": len(parts), "holes_sec": holes})
    if recs:
        beats[:] = out
    return recs


def trim_to_budget(beats: list[dict], span_index: dict[str, dict],
                   arousal: list[dict], max_sec: float) -> list[dict]:
    """초과분을 비트 **가장자리에서만** 덜어낸다. 반환: 제거 로그.

    ⚠ 2026-09-02부터 run_story 는 부르지 않는다 — 초과분은 watch-trim(초안 재분석)
    이 1차로 덜고, 산술 벨트는 watch_trim.budget_fallback_cuts 가 **같은 우선순위**
    로 편집본 좌표에서 잇는다. 이 함수는 그 우선순위 규칙의 정본으로 남긴다.

    보호: climax 비트 전체 · importance 5 span · 비트의 마지막 남은 span(통삭제
    금지). 제거 순서 = importance + arousal 보정(±0.5)이 낮은 것부터 — 동점은
    긴 것부터(예산 회수 효율), 그다음 이른 시각(결정성)."""
    removed: list[dict] = []
    while story_duration(beats, span_index) > max_sec:
        cands: list[tuple[float, float, float, int, str]] = []
        for bi, b in enumerate(beats):
            if b["role"] == "climax" or len(b["span_ids"]) <= 1:
                continue
            for sid in (b["span_ids"][0], b["span_ids"][-1]):
                sp = span_index[sid]
                if sp["importance"] >= 5:
                    continue
                dur = sp["t_out"] - sp["t_in"]
                score = sp["importance"] + arousal_adjust(arousal, sp["t_in"], sp["t_out"])
                cands.append((score, -dur, sp["t_in"], bi, sid))
        if not cands:
            break   # 던 게 없다 — budget_unmet 은 호출자가 기록
        cands.sort()
        _score, _ndur, _t, bi, sid = cands[0]
        beats[bi]["span_ids"].remove(sid)
        sp = span_index[sid]
        removed.append({"beat": bi, "span_id": sid,
                        "sec": round(sp["t_out"] - sp["t_in"], 3),
                        "importance": sp["importance"]})
    # 아이템 열 비트 동기화 — 트림이 뺀 span 을 아이템에서도 지운다. 빈 mat 아이템은
    # 라벨과 함께 소멸(고아 라벨이 화면에 남으면 안 된다). nar 아이템은 장면이
    # 사라져도 남는다 — 자동 배치로 폴백한다.
    for b in beats:
        its = b.get("items")
        if not its:
            continue
        keep = set(b["span_ids"])
        for it in its:
            it["span_ids"] = [x for x in it["span_ids"] if x in keep]
        b["items"] = [it for it in its if it["kind"] == "nar" or it["span_ids"]]
        b["labels"] = [{"text": it["label"], "span_id": it["span_ids"][0]}
                       for it in b["items"]
                       if it["kind"] == "mat" and it.get("label")]
    return removed


# ── TTS 슬롯 배치(순수) ─────────────────────────────────────────────────────

def _eligible_span(sp: dict, allow_mute: bool) -> bool:
    return (not sp["is_audio"]) or (allow_mute and sp["importance"] <= MUTE_MAX_IMPORTANCE)


def _beat_items_view(b: dict) -> list[dict]:
    """items 가 없는 비트(구 스키마·폴백)를 아이템 열로 본다 — nar 들을 재료 앞에."""
    its = b.get("items")
    if isinstance(its, list) and its:
        return its
    view = [{"kind": "nar", "text": str(t), "span_ids": [], "label": None}
            for t in (b.get("narration") or []) if str(t).strip()]
    view.append({"kind": "mat", "text": None,
                 "span_ids": list(b.get("span_ids") or []), "label": None})
    return view


def _clip_to_used(w0: float, w1: float,
                  used: list[tuple[float, float]]) -> tuple[float, float]:
    """이미 배치된 창과 겹치면 시작을 당긴다 — 앵커(끝)에 붙는 쪽이 생명이라 끝 보존."""
    for a, z in used:
        if a < w1 and z > w0:
            w0 = max(w0, z)
    return w0, w1


def _place_before(anchor_t: float, host: list[tuple[str, dict]], est: float,
                  used: list[tuple[float, float]], *,
                  allow_mute_fallback: bool = True):
    """anchor_t(다음 대사 시작)에 **끝이 닿는** 창을 host 재료 위에서 찾는다.

    166편 해부 A조항의 기계화 — "다음에 올 것을 미리 말한다"(실측: 내레이션의
    절반이 다음 대사에 0.3초 이내로 붙는다). 무성 우선, allow_mute_fallback 이면
    부족할 때 imp≤3 유성 뮤트. 작은 창도 수락한다(2026-09-02 사용자 지시 "드랍
    절대 금지" — 부족분은 배속 사다리 → fit 축약이 순서대로 흡수한다).
    반환 (w0, w1, mode, muted) | None."""
    floor = NARRATION_MIN_SEC
    best_start = None
    for allow_mute in ((False, True) if allow_mute_fallback else (False,)):
        start = anchor_t
        for _sid, sp in reversed(host):
            if abs(sp["t_out"] - start) > 0.005:
                break                              # 소스 구멍 — 런 끊김
            if not _eligible_span(sp, allow_mute):
                break
            start = sp["t_in"]
        avail = anchor_t - start
        if avail >= est - 1e-6:
            best_start = start
            break
        if avail >= floor - 1e-6 \
                and (best_start is None or start < best_start):
            best_start = start
    if best_start is None:
        return None
    w0, w1 = _clip_to_used(max(best_start, anchor_t - est), anchor_t, used)
    if w1 - w0 < floor - 1e-6:
        return None
    muted = [sid for sid, sp in host if sp["is_audio"]
             and sp["t_in"] < w1 - 0.01 and sp["t_out"] > w0 + 0.01]
    mode = "fit" if (w1 - w0) < est - 0.01 else ("muted" if muted else "silent")
    return w0, w1, mode, muted


def _place_tail(bspans: list[tuple[str, dict]], est: float,
                used: list[tuple[float, float]], *,
                allow_mute_fallback: bool = True):
    """비트 끝의 여운 컷 위 — 마지막 대사가 끝난 직후부터 말한다(엔딩 리캡 루프)."""
    if not bspans:
        return None
    end = bspans[-1][1]["t_out"]
    best = None
    for allow_mute in ((False, True) if allow_mute_fallback else (False,)):
        start = end
        for _sid, sp in reversed(bspans):
            if abs(sp["t_out"] - start) > 0.005:
                break
            if not _eligible_span(sp, allow_mute):
                break
            start = sp["t_in"]
        avail = end - start
        if avail >= NARRATION_MIN_SEC - 1e-6:
            best = start
            if avail >= est - 1e-6:
                break
    if best is None:
        return None
    w0, w1 = _clip_to_used(best, min(end, best + est), used)
    if w1 - w0 < NARRATION_MIN_SEC - 1e-6:
        return None
    muted = [sid for sid, sp in bspans if sp["is_audio"]
             and sp["t_in"] < w1 - 0.01 and sp["t_out"] > w0 + 0.01]
    mode = "fit" if (w1 - w0) < est - 0.01 else ("muted" if muted else "silent")
    return w0, w1, mode, muted


def trim_beat_heads(beats: list[dict], span_index: dict[str, dict],
                    cues: list[dict]) -> list[dict]:
    """창 확정 후 비트 머리의 데드에어 컷 — 반환: 트림 기록(beats 는 제자리 수정).

    필요 시작점 = min(첫 유성 시작, 머리 창 시작) − 리드. 그보다 통째로 앞인 무성
    span 은 제거, 걸치는 무성 span 은 b["head_trim_sec"] 로 클립 시작점만 당긴다
    (span 내부라 span 단위 컷 불가 — 조립·스냅 벨트가 이 키를 정식 어휘로 받는다).
    무성 전용 비트(창 없음)는 산술로 무용을 판정할 수 없어 안 건드린다(시청 트림 몫).
    라벨 앵커 span 은 제거하지 않는다."""
    recs: list[dict] = []
    anchors = {lb.get("span_id") for b in beats for lb in (b.get("labels") or [])}
    for bi, b in enumerate(beats):
        ids = b.get("span_ids") or []
        if not ids:
            continue
        t0 = span_index[ids[0]]["t_in"]
        first_voiced = next((span_index[s]["t_in"] for s in ids
                             if span_index[s]["is_audio"]), None)
        horizon = first_voiced if first_voiced is not None \
            else span_index[ids[-1]]["t_out"]
        wins = [float(c["source_time_sec"]) for c in cues
                if t0 - 1e-6 <= float(c["source_time_sec"]) < horizon - 1e-6]
        if wins:
            need = min(wins) - HEAD_LEAD_NAR_SEC
        elif first_voiced is not None:
            need = first_voiced - HEAD_LEAD_DIALOGUE_SEC
        else:
            continue
        if need <= t0 + 0.05:
            continue
        removed: list[str] = []
        while b["span_ids"]:
            sid = b["span_ids"][0]
            sp = span_index[sid]
            if sp["is_audio"] or sid in anchors or sp["t_out"] > need + 1e-6:
                break
            b["span_ids"].pop(0)
            removed.append(sid)
        partial = None
        if b["span_ids"]:
            sid0 = b["span_ids"][0]
            sp0 = span_index[sid0]
            if not sp0["is_audio"] and sid0 not in anchors \
                    and sp0["t_in"] < need - 0.05:
                b["head_trim_sec"] = round(need, 3)
                partial = round(need - sp0["t_in"], 3)
        if removed or partial:
            recs.append({"beat": bi, "removed_spans": removed,
                         "partial_sec": partial,
                         "sec": round(sum(span_index[s]["t_out"]
                                          - span_index[s]["t_in"]
                                          for s in removed) + (partial or 0.0), 3)})
    return recs


NARRATION_OVERSHOOT_SEC = 6.0   # 자리 생성이 예산을 넘겨도 되는 한도 — 초과분은
                                # watch-trim 예산 컷(⑦)이 다른 데서 회수한다


def _grow_before(b: dict, span_index: dict[str, dict], beats: list[dict],
                 before_sid: str, need_sec: float, budget: dict) -> float:
    """before_sid 앞에 인접 무성(미분석 포함) span 을 이어붙여 창 재료를 만든다.

    2026-09-02 사용자 원칙("내레이션이 먼저고 자리가 따른다")의 기계 — 창을
    찾다 실패하면 줄이는 게 아니라 **만든다**. 인접·미사용·무성·연속(≤0.05s)
    조건을 전부 만족하는 동안만 자란다(다른 장면·대사 위로는 절대 안 넘어간다).
    budget["left"] 소비를 기록하고 확보한 초를 돌려준다."""
    if before_sid not in b["span_ids"]:
        return 0.0
    all_used = {x for bb in beats for x in bb["span_ids"]}
    by_pos = {sp["pos"]: sid for sid, sp in span_index.items()}
    cur = span_index[before_sid]
    idx = b["span_ids"].index(before_sid)
    got = 0.0
    grown = 0
    while got < need_sec - 1e-6 and budget["left"] > 0.1 and grown < 8:
        cand = by_pos.get(cur["pos"] - 1)
        if cand is None or cand in all_used \
                or span_index[cand]["is_audio"] \
                or abs(span_index[cand]["t_out"] - cur["t_in"]) > 0.05:
            break
        d = span_index[cand]["t_out"] - span_index[cand]["t_in"]
        b["span_ids"].insert(idx, cand)
        all_used.add(cand)
        got += d
        budget["left"] -= d
        grown += 1
        cur = span_index[cand]
    return got


def _grow_after(b: dict, span_index: dict[str, dict], beats: list[dict],
                after_sid: str, need_sec: float, budget: dict) -> float:
    """after_sid 뒤에 인접 무성 span 을 이어붙인다 — _grow_before 의 꼬리판.

    엔딩 여운이 견적보다 짧을 때 마지막 장면 **뒤**의 무성(같은 장면의 잔상)으로
    여운을 늘린다. 조건은 머리판과 같다(인접·미사용·무성·연속 ≤0.05s)."""
    if after_sid not in b["span_ids"]:
        return 0.0
    all_used = {x for bb in beats for x in bb["span_ids"]}
    by_pos = {sp["pos"]: sid for sid, sp in span_index.items()}
    cur = span_index[after_sid]
    idx = b["span_ids"].index(after_sid)
    got = 0.0
    grown = 0
    while got < need_sec - 1e-6 and budget["left"] > 0.1 and grown < 8:
        cand = by_pos.get(cur["pos"] + 1)
        if cand is None or cand in all_used \
                or span_index[cand]["is_audio"] \
                or abs(span_index[cand]["t_in"] - cur["t_out"]) > 0.05:
            break
        d = span_index[cand]["t_out"] - span_index[cand]["t_in"]
        idx += 1
        b["span_ids"].insert(idx, cand)
        all_used.add(cand)
        got += d
        budget["left"] -= d
        grown += 1
        cur = span_index[cand]
    return got


def _best_internal_run(b: dict, span_index: dict[str, dict], beats: list[dict],
                       est: float, used_list: list[tuple[float, float]],
                       budget: dict) -> tuple[float, float] | None:
    """비트 **안**의 무성 run 중 내레이션 창이 될 자리를 찾는다(①b, 2026-09-02).

    머리 리드인이 좁을 때의 마지막 자유도 — 내레이션은 여전히 자기 비트 화면
    위다(원칙 불변). run 이 견적보다 짧으면 꼬리 방향 인접 무성으로 늘려 본다
    (_grow_after). 견적을 다 담는 **가장 이른** run 을 우선하고, 없으면 70% 이상
    담는 첫 run. 이미 배정된 창과 겹치는 run 은 건너뛴다."""
    ids = [x for x in b["span_ids"] if x in span_index]
    runs: list[list[str]] = []
    cur: list[str] | None = None
    for sid in ids:
        sp = span_index[sid]
        if sp["is_audio"]:
            cur = None
            continue
        if cur is not None \
                and abs(sp["t_in"] - span_index[cur[-1]]["t_out"]) <= 0.05:
            cur.append(sid)
        else:
            cur = [sid]
            runs.append(cur)
    best: tuple[float, float] | None = None
    for run in runs:
        w0 = span_index[run[0]]["t_in"]
        ext = span_index[run[-1]]["t_out"] - w0
        if ext < est + 0.2 - 1e-6:
            _grow_after(b, span_index, beats, run[-1], est + 0.2 - ext, budget)
            ids2 = [x for x in b["span_ids"] if x in span_index]
            k = ids2.index(run[0])
            end = span_index[ids2[k]]["t_out"]
            for x in ids2[k + 1:]:
                sp2 = span_index[x]
                if sp2["is_audio"] or abs(sp2["t_in"] - end) > 0.05:
                    break
                end = sp2["t_out"]
            ext = end - w0
        w1 = min(w0 + est, w0 + ext)
        if any(u0 < w1 and u1 > w0 for u0, u1 in used_list):
            continue
        if w1 - w0 >= est - 1e-6:
            return (w0, w1)                    # 견적을 다 담는 가장 이른 run
        if best is None \
                and w1 - w0 >= max(NARRATION_MIN_SEC, est * 0.7) - 1e-6:
            best = (w0, w1)
    return best


def _plan_narration_slots_items(beats: list[dict], span_index: dict[str, dict],
                                measured: dict[str, float] | None = None,
                                budget_slack: float = 0.0) \
        -> tuple[list[dict], list[dict]]:
    """아이템 열 편성의 슬롯 배치 — 창을 찾는 게 아니라 **자리가 이미 정해져 있다**.

    nar 아이템의 창 = 다음 대사에 끝이 닿는 리드인(내레이션-장면 교대 구조).
    배치 정책(2026-09-02 사용자 지시로 교체 — 종전 ②직전 비트 꼬리·③뮤트 폐지):
      ⓪ 모델이 지정한 무성 장면(nar.span_ids) → ① **이 비트 머리** 리드인(무성만)
      → ② 자리 생성(_grow_before — 창 머리 앞 인접 무성을 견적까지 이어붙임,
      ⓪·① 공통 · 예산 초과는 OVERSHOOT 한도까지 watch-trim 이 회수)
      → ④ 드랍+기록(인접 무성이 전혀 없을 때만 — 2026-09-02 "내레이션이 먼저,
      자리는 만들어서라도"로 드랍은 사실상 소멸).
    폐지 이유(실사고 둘): ② "집에 오자마자…" 다리가 직전 장면(대화) 위에서 들려
    화면과 어긋났고, ③ 뮤트는 이어지는 대사 중간의 소리를 2초 죽였다 끼워 넣어
    "대사가 잘렸다 다시 나오는" 청감 파손을 만들었다. 내레이션은 **자기 비트의
    새 장면 위**에서만 들린다 — 자리는 모델이 머리 무성 조각(visual)으로 만든다
    (프롬프트 규칙 4와 한 벌). 엔딩 꼬리 nar(뒤 재료 없음)는 자기 비트 여운 유지."""
    cues: list[dict] = []
    dropped: list[dict] = []
    used: dict[int, list[tuple[float, float]]] = {}
    # 자리 생성 공동 예산 — 슬랙을 다 써도 OVERSHOOT 까지는 자리를 만든다
    _budget = {"left": max(0.0, budget_slack) + NARRATION_OVERSHOOT_SEC}
    for b in beats:
        b.setdefault("muted_span_ids", [])
    for bi, b in enumerate(beats):
        items = _beat_items_view(b)
        bspans = [(x, span_index[x]) for x in b["span_ids"] if x in span_index]
        placed_texts: list[str] = []
        line = 0
        for j, it in enumerate(items):
            if it["kind"] != "nar":
                continue
            text = it["text"]
            # 실측이 정본(프로토 규약 — '문구가 고정이고 자리가 양보한다').
            # 실측이 없으면 견적(0.6s + 자수/7.0 — 상수 주석의 회귀식) 폴백.
            # 견적은 기본 배속(fast) 기준 — 발화부만 ÷rate (리드 0.6s 는 고정)
            if measured and text in measured:
                _speech = max(0.0, measured[text] + 0.3 - NARRATION_LEAD_SEC)
            else:
                _speech = len("".join(text.split())) / NARRATION_CPS
            est = max(NARRATION_MIN_SEC,
                      NARRATION_LEAD_SEC + _speech / NARRATION_BASE_RATE)
            placed = None                      # (host_bi, w0, w1, mode, muted)
            _floor = NARRATION_MIN_SEC
            # 자리 생성(2026-09-02 사용자 원칙 — "내레이션을 만들고 그 내레이션을
            # 위한 자리를 어떻게든 만들어야지, 좁은 자리에 맞추면 안 된다"):
            # 창 재료가 견적에 못 미치면 창 머리 앞에 인접 무성을 이어붙인다.
            # ⓪ 모델 지정·① 비트 머리 **공통** — A판 1.03s 창 축약("옥상 불길
            # 맞선")은 ⓪ 이 확장에서 빠져 재발했다. 예산이 없어도
            # NARRATION_OVERSHOOT_SEC 까지는 자리를 만든다(초과분은 watch-trim
            # 예산 컷이 다른 데서 회수 — 축약·드랍보다 그쪽이 싸다).
            if it.get("span_ids"):
                _sps0 = [span_index[x] for x in it["span_ids"] if x in span_index]
                have = sum(sp["t_out"] - sp["t_in"] for sp in _sps0)
                _head = next((x for x in it["span_ids"] if x in span_index), None)
            else:
                have = 0.0
                for sid2 in b["span_ids"]:
                    sp2 = span_index[sid2]
                    if sp2["is_audio"]:
                        break
                    have += sp2["t_out"] - sp2["t_in"]
                _head = b["span_ids"][0] if b["span_ids"] else None
            grown_sec = 0.0
            if _head is not None and have < est + 0.2 - 1e-6:
                grown_sec = _grow_before(b, span_index, beats, _head,
                                         est + 0.2 - have, _budget)
                if grown_sec:
                    bspans = [(x, span_index[x]) for x in b["span_ids"]
                              if x in span_index]
            if it.get("span_ids"):             # ⓪ 모델 지정 장면
                ids0 = [x for x in it["span_ids"] if x in span_index]
                if grown_sec and ids0 and ids0[0] in b["span_ids"]:
                    # 방금 이어붙인 무성은 지정 장면 바로 앞에 있다 — 연속 무성
                    # 리드인을 창 재료에 합친다(내레이션이 그 위에서 흐른다).
                    k0 = b["span_ids"].index(ids0[0])
                    pre: list[str] = []
                    for x in reversed(b["span_ids"][:k0]):
                        if span_index.get(x, {}).get("is_audio", True):
                            break
                        pre.insert(0, x)
                    ids0 = pre + ids0
                sps = [(x, span_index[x]) for x in ids0]
                if sps:
                    w0 = sps[0][1]["t_in"]
                    w1 = min(sps[-1][1]["t_out"], w0 + est)
                    if w1 - w0 >= _floor - 1e-6:
                        muted = [x for x, sp in sps if sp["is_audio"]
                                 and sp["t_in"] < w1 - 0.01 and sp["t_out"] > w0 + 0.01]
                        placed = (bi, w0, w1, "muted" if muted else "silent", muted)
            if placed is None:
                nxt = next((x for x in items[j + 1:]
                            if x["kind"] == "mat" and x["span_ids"]), None)
                if nxt is not None:
                    # 앵커 = 다음 아이템의 첫 **유성** span — 무성 머리는 리드인
                    # 재료다(내레이션이 그 위에서 흐른다). 전부 무성이면 그 장면
                    # 자체가 화면이다 — 장면 시작에 얹는다(⓪과 같은 처리).
                    voiced = [x for x in nxt["span_ids"]
                              if x in span_index and span_index[x]["is_audio"]]
                    if voiced:
                        anchor_sid = voiced[0]
                        anchor_t = span_index[anchor_sid]["t_in"]
                        k = b["span_ids"].index(anchor_sid)
                        host = bspans[:k]
                        # 직전 비트가 **무성 전용**(silent_break 류)이면 그 꼬리는
                        # 새 장면의 무성 리드인과 한 덩어리다 — 창 재료로 허용
                        # (2026-09-02: 1.66s 창 갇힘 → 전보문 재발 수정. 대사 장면
                        # 꼬리에 얹던 옛 ② 와 달리 화면도 소리도 안전하다).
                        if bi > 0:
                            pb_ids = beats[bi - 1].get("span_ids") or []
                            if pb_ids and all(not span_index[x]["is_audio"]
                                              for x in pb_ids
                                              if x in span_index):
                                host = [(x, span_index[x]) for x in pb_ids
                                        if x in span_index] + host
                        got = _place_before(anchor_t, host, est,
                                            used.get(bi, []),
                                            allow_mute_fallback=False)
                        if got is not None:
                            placed = (bi, *got)
                    else:
                        sps = [(x, span_index[x]) for x in nxt["span_ids"]
                               if x in span_index]
                        if sps:
                            w0 = sps[0][1]["t_in"]
                            w1 = min(sps[-1][1]["t_out"], w0 + est)
                            if w1 - w0 >= _floor - 1e-6:
                                placed = (bi, w0, w1, "silent", [])
                else:
                    got = _place_tail(bspans, est, used.get(bi, []),
                                      allow_mute_fallback=False)
                    if got is not None:
                        placed = (bi, *got)
            _head_intent = bool(it.get("span_ids")) or any(
                x["kind"] == "mat" and x["span_ids"] for x in items[j + 1:])
            if placed is None and b["span_ids"] and _head_intent:
                # 마지막 구제(드랍 대체) — 비트 머리가 유성이라도 그 **앞** 무성을
                # 끌어와 리드인을 새로 만든다. 자리가 없으면 만드는 것이 원칙이고,
                # 끌어온 재료는 같은 장면의 직전 무성뿐이라 화면·소리 다 안전하다.
                # (꼬리 의도 nar — mat 뒤 엔딩 회고 — 는 여기 안 온다: 장면보다
                # 먼저 들리면 어긋난다. 아래 꼬리 여운 구제가 받는다.)
                got2 = _grow_before(b, span_index, beats, b["span_ids"][0],
                                    est + 0.2, _budget)
                if got2 >= max(_floor, est * 0.7) - 1e-6:
                    bspans = [(x, span_index[x]) for x in b["span_ids"]
                              if x in span_index]
                    w0 = bspans[0][1]["t_in"]
                    w1 = min(w0 + est, w0 + got2)
                    placed = (bi, w0, w1, "silent", [])
            if _head_intent and (placed is None
                                 or placed[3] != "muted"
                                 and placed[2] - placed[1] < est * 0.7 - 1e-6):
                # ①b 비트 내부 무성 run 재배치(2026-09-02): 머리 리드인이 좁고
                # 앞이 대사라 못 넓힐 때(EP02 훅 1.03s 창 축약 재발 사례), 자기
                # 비트 **안** 다른 무성 run 에 창을 놓는다 — 화면은 여전히 자기
                # 비트다. 좁은 창에 문구를 구겨 넣는 것보다 위치 이동이 낫다.
                alt = _best_internal_run(b, span_index, beats, est,
                                         used.get(bi, []), _budget)
                if alt is not None and (placed is None
                                        or alt[1] - alt[0]
                                        > placed[2] - placed[1] + 1e-6):
                    placed = (bi, alt[0], alt[1], "silent", [])
                    bspans = [(x, span_index[x]) for x in b["span_ids"]
                              if x in span_index]
            # 엔딩 계열 역할만 — climax 등 중간 비트의 예고형 다리가 장면 **뒤**에
            # 놓이면 어긋난다(예고를 사후에 듣는 꼴). 템플릿이 엔딩 역할을 필수로
            # 강제하므로(required_roles) 역할명 스코프로 충분하다.
            if placed is None and b["span_ids"] \
                    and b.get("role") in ("ending", "loop_ending", "recap_loop"):
                # 엔딩 꼬리 여운 배치(2026-09-02 사용자 지시) — 머리에 재료가
                # 없으면 마지막 장면의 **여운 위**에서 말한다(엔딩 다리는 회고라
                # 꼬리가 오히려 제자리다). 여운이 견적보다 짧으면 마지막 span 뒤
                # 인접 무성을 이어붙여 늘린다(자리 생성의 꼬리판).
                tail_have = 0.0
                for sid2 in reversed(b["span_ids"]):
                    sp2 = span_index.get(sid2)
                    if sp2 is None or sp2["is_audio"]:
                        break
                    tail_have += sp2["t_out"] - sp2["t_in"]
                if tail_have < est + 0.2 - 1e-6:
                    _grow_after(b, span_index, beats, b["span_ids"][-1],
                                est + 0.2 - tail_have, _budget)
                    bspans = [(x, span_index[x]) for x in b["span_ids"]
                              if x in span_index]
                got3 = _place_tail(bspans, est, used.get(bi, []),
                                   allow_mute_fallback=False)
                if got3 is not None:
                    placed = (bi, *got3)
            if placed is None:
                dropped.append({"beat": bi, "text": text,
                                "reason": "인접 무성 재료가 전혀 없음 — 창을 만들 "
                                          "수도 없는 자리(비트 머리 앞이 다른 "
                                          "장면의 대사, 엔딩이면 꼬리 여운도 없음)"})
                continue
            host_bi, w0, w1, mode, muted = placed
            used.setdefault(host_bi, []).append((w0, w1))
            for m in muted:
                if m not in beats[host_bi]["muted_span_ids"]:
                    beats[host_bi]["muted_span_ids"].append(m)
            placed_texts.append(text)
            # 배속 사다리 — 기본 fast, 창이 견적(fast 기준)보다 작으면 very_fast 로
            # 흡수한다. 사다리로도 모자라면 mode 는 fit 그대로 남아 합성단의 Flash
            # 축약이 최후로 돈다(실사고: 축약이 조사를 지움 — 최대한 안 가게).
            speed = NARRATION_BASE_SPEED
            win = w1 - w0
            if win < est - 1e-6:
                base_speech = max(0.0, (est - NARRATION_LEAD_SEC)
                                  * NARRATION_BASE_RATE)
                for lbl, rate in NARRATION_SPEED_LADDER[1:]:
                    speed = lbl
                    if NARRATION_LEAD_SEC + base_speech / rate <= win + 1e-6:
                        break
            cues.append({"beat": bi, "line": line, "text": text, "mode": mode,
                         "speed": speed,
                         "source_time_sec": round(w0, 3),
                         "source_end_sec": round(w1, 3),
                         "muted_span_ids": muted})
            line += 1
        b["narration"] = placed_texts or None
    return cues, dropped


def plan_narration_slots(beats: list[dict], span_index: dict[str, dict],
                         measured: dict[str, float] | None = None,
                         budget_slack: float = 0.0) \
        -> tuple[list[dict], list[dict]]:
    """비트별 내레이션 → (cue 계획, 드랍 기록).

    규칙(발주서 §A-4): ⓐ비트 내 무성 span 런 위(기본) → ⓑ없으면 importance≤3
    유성 포함 런(해당 유성 span 뮤트) → ⓒimportance≥4 유성과는 절대 겹지 않는다 —
    창이 안 나오면 내레이션 드랍 + 기록(조용한 누락 금지).

    cue 의 source_time_sec = 창 시작(원본 절대초 — C2 신원 규약). 창이 견적보다
    작아도 NARRATION_MIN_SEC 이상이면 배치한다 — resources 의 fit 이 줄인다.

    아이템 열 편성(items)이 하나라도 있으면 앵커식 배치로 분기한다 —
    _plan_narration_slots_items(실측 길이 measured 가 있으면 그것이 창 크기 정본).
    이 본문은 구 스키마·폴백 전용(회귀 0)."""
    if any(isinstance(b.get("items"), list) and b["items"] for b in beats):
        return _plan_narration_slots_items(beats, span_index, measured,
                                           budget_slack=budget_slack)
    cues: list[dict] = []
    dropped: list[dict] = []
    for bi, b in enumerate(beats):
        raw = b.get("narration")
        texts = ([raw] if isinstance(raw, str) and raw.strip()
                 else [str(x) for x in (raw or []) if str(x).strip()])
        if not texts:
            b["muted_span_ids"] = []
            continue
        # M11-A: 문장들을 비트 안 창에 **순서대로** 배치한다(레퍼런스는 짧은 문장
        # 둘이 이어 붙는다). 커서(cursor)가 이미 쓴 시각을 기억해 겹치지 않는다.
        placed_muted: list[str] = []
        cursor = 0.0
        for ti, text in enumerate(texts):
            est = max(NARRATION_MIN_SEC,
                      NARRATION_LEAD_SEC
                      + len("".join(text.split())) / NARRATION_CPS)

            def runs(allow_mute: bool) -> list[list[str]]:
                """덮을 수 있는 span 의 **소스 연속** 런 — grid 인덱스 인접이어도 0.5s
                미만 전사 구멍으로 소스가 끊길 수 있다(적대 리뷰 확정: 창 끝이 구멍에
                떨어져 cue 소실+뮤트만 남는 재현). 구멍에서도 런을 끊는다."""
                out: list[list[str]] = []
                cur: list[str] = []
                for sid in b["span_ids"]:
                    sp = span_index[sid]
                    ok = (not sp["is_audio"]) or \
                        (allow_mute and sp["importance"] <= MUTE_MAX_IMPORTANCE)
                    broken = bool(cur) and \
                        abs(sp["t_in"] - span_index[cur[-1]]["t_out"]) > 0.005
                    if ok and not broken:
                        cur.append(sid)
                    else:
                        if cur:
                            out.append(cur)
                        cur = [sid] if ok else []
                if cur:
                    out.append(cur)
                return out

            def free(run: list[str]) -> tuple[float, float]:
                """이 런에서 **아직 안 쓴** 구간(커서 이후) → (시작, 남은 길이)."""
                a = max(span_index[run[0]]["t_in"], cursor)
                b_end = span_index[run[-1]]["t_out"]
                return a, max(0.0, b_end - a)

            chosen: list[str] | None = None
            mode = None
            for allow_mute in (False, True):                  # ⓐ 먼저, 그다음 ⓑ
                fits = [r for r in runs(allow_mute) if free(r)[1] >= est]
                if fits:
                    chosen, mode = fits[0], ("silent" if not allow_mute else "muted")
                    break
            if chosen is None:                                # 견적 미달 — 최장 런에 fit
                all_runs = [r for r in runs(True) if free(r)[1] >= NARRATION_MIN_SEC]
                if all_runs:
                    chosen = max(all_runs, key=lambda r: (free(r)[1],
                                                          -span_index[r[0]]["pos"]))
                    mode = "fit"
            if chosen is None:                                # ⓒ — 드랍 + 기록
                # 뒤 문장부터 버린다(앞 문장이 살아남는 쪽이 서사에 낫다)
                for rest in texts[ti:]:
                    dropped.append({"beat": bi, "text": rest,
                                    "reason": "남은 창 없음(importance≥4 유성뿐)"})
                break
            w0, avail = free(chosen)
            w1 = min(span_index[chosen[-1]]["t_out"],
                     w0 + max(min(est, avail), NARRATION_MIN_SEC))
            # 뮤트는 **창과 겹치는** 유성 span 만 — 런 전체 뮤트는 창 밖 대사까지
            # 무음으로 만들었다(적대 리뷰 확정: 내레이션도 대사도 없는 구간 재현)
            muted = [s for s in chosen if span_index[s]["is_audio"]
                     and span_index[s]["t_in"] < w1 - 0.01
                     and span_index[s]["t_out"] > w0 + 0.01]
            placed_muted.extend(m for m in muted if m not in placed_muted)
            cursor = w1
            cues.append({"beat": bi, "line": ti, "text": text, "mode": mode,
                         "source_time_sec": round(w0, 3),
                         "source_end_sec": round(w1, 3),
                         "muted_span_ids": muted})
        b["muted_span_ids"] = placed_muted
        placed = [c["text"] for c in cues if c["beat"] == bi]
        b["narration"] = placed or None
    return cues, dropped


def verify_tts_conflicts(cues: list[dict], beats: list[dict],
                         span_index: dict[str, dict]) -> list[str]:
    """벨트: cue 창이 뮤트 안 된 importance≥4 유성 span 과 겹치면 위반(0 이어야 한다)."""
    violations: list[str] = []
    for cue in cues:
        c0, c1 = cue["source_time_sec"], cue["source_end_sec"]
        muted = set(cue.get("muted_span_ids") or [])
        for b in beats:
            for sid in b["span_ids"]:
                sp = span_index[sid]
                if not sp["is_audio"] or sid in muted \
                        or sp["importance"] <= MUTE_MAX_IMPORTANCE:
                    continue
                if min(c1, sp["t_out"]) - max(c0, sp["t_in"]) > 0.01:
                    violations.append(f"cue(beat {cue['beat']}) ↔ {sid} "
                                      f"(importance {sp['importance']})")
    return violations


# ── 폴백 편성(순수) — 최소 1개 보장 ─────────────────────────────────────────

def fallback_highlight(span_index: dict[str, dict], span_order: list[str],
                       arousal: list[dict], target_sec: float,
                       work_title: str) -> dict:
    """재질의 소진 시 코드가 짓는 highlight 편성 — 카피 없이도 편집 가능한 최소.

    meaning importance 상위부터 그 meaning 의 span 연속 덩어리를 시각순으로 담는다.
    동점은 arousal 보정(±0.5) — 여기가 §9-B 의 '동점 타이브레이커' 소비처다."""
    # 그룹 = meaning content 가 같고 **grid 연속**인 런 — content 문자열만으로
    # 묶으면 동일 문구의 떨어진 meaning 이 병합돼 비연속 비트가 나온다(적대 리뷰
    # 확정: validate 였다면 반려될 편성을 폴백이 직접 생성).
    group_list: list[list[str]] = []
    for sid in span_order:
        if group_list and \
                span_index[group_list[-1][-1]]["meaning_content"] == span_index[sid]["meaning_content"] \
                and span_index[sid]["pos"] - span_index[group_list[-1][-1]]["pos"] == 1:
            group_list[-1].append(sid)
        else:
            group_list.append([sid])
    groups = {f"{span_index[ids[0]]['meaning_content']}#{i}": ids
              for i, ids in enumerate(group_list)}

    slot_sec = max(NARRATION_MIN_SEC, target_sec / PIECES_MIN)

    def core_run(ids: list[str]) -> list[str]:
        """meaning 의 span 런에서 최고 importance span 중심의 ~slot_sec 코어만.

        meaning 통째 편성은 드라이런에서 87s 한 덩어리를 낳았고, 트림도 importance 5
        보호에 막혀 줄이지 못했다 — 폴백은 애초에 코어만 담는다. 확장은 양옆 중
        (importance 높은 쪽, 동률이면 이른 쪽) — 결정성."""
        anchor = max(range(len(ids)),
                     key=lambda i: (span_index[ids[i]]["importance"], -i))
        lo = hi = anchor
        def dur(a: int, b: int) -> float:
            return sum(span_index[ids[i]]["t_out"] - span_index[ids[i]]["t_in"]
                       for i in range(a, b + 1))
        while dur(lo, hi) < slot_sec and (lo > 0 or hi < len(ids) - 1):
            left = span_index[ids[lo - 1]]["importance"] if lo > 0 else -1
            right = span_index[ids[hi + 1]]["importance"] if hi < len(ids) - 1 else -1
            if left >= right:
                lo -= 1
            else:
                hi += 1
        return ids[lo:hi + 1]

    scored = []
    for content, ids in groups.items():
        core = core_run(ids)
        t0, t1 = span_index[core[0]]["t_in"], span_index[core[-1]]["t_out"]
        imp = max(span_index[s]["importance"] for s in core)
        scored.append((-(imp + arousal_adjust(arousal, t0, t1)), t0, content, core))
    scored.sort()
    beats: list[dict] = []
    total = 0.0
    for _neg, t0, _content, ids in scored:
        # 조각 수 지향(PIECES_MIN)까지는 예산이 차도 계속 담는다 — 초과분은
        # trim_to_budget 이 가장자리에서 던다(한 덩어리 87s 편성이 나오던 드라이런 수정)
        if len(beats) >= PIECES_MAX or (total >= target_sec and len(beats) >= PIECES_MIN):
            break
        beats.append({"role": "build", "span_ids": list(ids),
                      "narration": None, "labels": []})
        total += sum(span_index[s]["t_out"] - span_index[s]["t_in"] for s in ids)
    beats.sort(key=lambda b: span_index[b["span_ids"][0]]["t_in"])   # 시각순 편성
    return {"template": "highlight", "reason": "재질의 소진 — 코드 폴백(최소 1개 보장)",
            "title": {"line1": work_title, "line2": "하이라이트"}, "beats": beats}


# ── M10-C: 다안 심사 — 판단은 모델, 승자 선택은 코드(결정적) ────────────────

STORY_CANDIDATES = 3         # 한 호출에서 받는 안 개수(추가 LLM 호출 0)
RUBRIC_WEIGHTS = {           # 품질 사고 > 취향 — 실측이 만든 가중치
    "narration": 3.0,        # 실현율 × 다리 배치 품질(실측 0/3 드랍 · 819초 점프 사고)
    "material": 3.0,         # 재료 신뢰도(저확신 자막이 화면에 나간 사고)
    "coverage": 2.0,         # 오디오 커버리지(실측 62% 판 — 14.7초 연속 정적 사고)
    "cohesion": 1.5,         # 아크 응집도(원거리 짜집기 억제 — 사용자 지적)
    "progression": 1.0,      # 진행감(§9-D)
    "budget": 1.0,           # 예산 적합
    "intro": 1.0,            # 서론 금지(§9-D)
    "ending": 1.0,           # 엔딩 연장선(2026-09-02 — 다음 국면 해소 장면 감점)
}
_GREETING = ("안녕", "반갑", "처음 뵙", "소개할게", "인사드리")


def _meaning_jumps(beats: list[dict], span_index: dict[str, dict]) -> tuple[int, int]:
    """비트 경계 점프(meaning 통째 건너뜀 ∨ 시퀀스 경계 넘음) 수와 다리 수.

    절대 초 임계를 쓰지 않는다 — 작품·회차마다 호흡이 달라 초는 기준이 못 된다
    (사용자 지적 2026-09-01). meaning 은 Stage 2 가 그 소재의 호흡대로 이미 잘라 둔
    단위라, '건너뛴 meaning 이 있는가'는 소재에 자동으로 맞춰진다. 같은 meaning
    안에서 span 을 쳐낸 것은 점프가 아니다(같은 장면을 다듬은 것 — 다리 불필요).
    다리 = 뒤 비트 머리 또는 앞 비트 꼬리의 nar 아이템(구 스키마는 내레이션
    보유 = 다리로 관용 판정)."""
    pos2sid = {sp["pos"]: sid for sid, sp in span_index.items()}

    def _head_nar(b: dict) -> bool:
        its = b.get("items")
        if isinstance(its, list) and its:
            return its[0]["kind"] == "nar"
        return bool(b.get("narration"))

    def _tail_nar(b: dict) -> bool:
        its = b.get("items")
        return bool(isinstance(its, list) and its and its[-1]["kind"] == "nar")

    jumps = bridged = 0
    for i in range(1, len(beats)):
        pb, cb = beats[i - 1], beats[i]
        if not pb.get("span_ids") or not cb.get("span_ids"):
            continue
        a, z = pb["span_ids"][-1], cb["span_ids"][0]
        if a not in span_index or z not in span_index:
            continue
        pa, pz = span_index[a]["pos"], span_index[z]["pos"]
        if pz - pa <= 1:
            continue
        ma = span_index[a].get("meaning_idx")
        mz = span_index[z].get("meaning_idx")
        sa = span_index[a].get("sequence_idx")
        sz = span_index[z].get("sequence_idx")
        between = set()
        for pp in range(pa + 1, pz):
            sid = pos2sid.get(pp)
            if sid is not None:
                between.add(span_index[sid].get("meaning_idx"))
        between -= {None, ma, mz}
        seq_cross = sa is not None and sz is not None and sa != sz
        # 점프 = meaning 통째 건너뜀 ∨ 시퀀스 경계 넘음 — 둘 다 Stage 1·2 가 그
        # 소재의 호흡대로 잘라 둔 구조 단위라 절대 초 없이 작품에 자동 적응한다.
        # (meaning 이 긴 작품에서 같은 meaning 안 20초 점프가 안 잡히던 구멍을
        # 시퀀스 축이 보완 — 규칙 2 를 '한 시퀀스 금지'에서 '다리 조건부'로 완화한
        # 것의 벨트가 이 지표다.)
        if not between and not seq_cross:
            continue
        jumps += 1
        if _head_nar(cb) or _tail_nar(pb):
            bridged += 1
    return jumps, bridged


def score_story(story: dict, span_index: dict[str, dict], *,
                target_sec: float) -> dict:
    """안 하나 → 항목별 0~1 점수 + 총점. 순수·결정적.

    LLM 심사를 쓰지 않는다 — 검증자와 피검증자가 편향을 공유하면 안 된다(M9 원칙).
    항목은 전부 실측 사고에서 유래했다(주석의 사고 이름 참조)."""
    beats = story.get("beats") or []
    ids = [s for b in beats for s in b.get("span_ids") or []]
    voiced = [span_index[s] for s in ids
              if s in span_index and span_index[s]["is_audio"]]

    # ① 내레이션 실현율 — 계획한 내레이션 중 실제 슬롯을 얻는 비율
    # M11: 분모는 **문장 수**다 — 비트 수로 세면 복수 문장에서 비율이 1 을 넘어
    # (실측 1.667) 가중치 3.0 이 다른 항목을 압도한다(응집도 0.43 인 안이 이겼다).
    def _lines(b: dict) -> int:
        n = b.get("narration")
        if isinstance(n, str):
            return 1 if n.strip() else 0
        return len([x for x in (n or []) if str(x).strip()])

    planned = sum(_lines(b) for b in beats)
    probe = [dict(b, span_ids=list(b["span_ids"])) for b in beats]
    cues, dropped = plan_narration_slots(probe, span_index)
    # 실현율 × 다리 배치 품질. **개수를 처벌하지 않는다**(사용자 결정 2026-09-01 —
    # 재료가 촘촘해 0줄이면 정당하다, 종전 recap 0줄=0.0 처벌 제거). 대신 meaning
    # 을 통째로 건너뛰고도 다리를 안 놓은 점프가 감점이다 — 819초 점프 실사고.
    # fit(창 < 견적 = 축약 예정) cue 는 반 실현(2026-09-02 "내레이션이 먼저,
    # 자리가 따른다") — 좁은 자리에 문구를 구겨 넣을 안은 심사에서 밀리고,
    # 같은 재료라도 내레이션에 진짜 자리를 준 안이 뽑히게 하는 선택 압력이다.
    # (EP02 훅 1.03s '옥상 둘, 불길' 실사고 — 배치 사다리가 다 실패한 자리는
    # 심사가 피하는 수밖에 없다.)
    realized = sum(0.5 if c.get("mode") == "fit" else 1.0 for c in cues)
    realization = min(1.0, realized / planned) if planned else 1.0
    jumps, bridged_n = _meaning_jumps(beats, span_index)
    bridge = (bridged_n / jumps) if jumps else 1.0
    # gap-bridge 계수(2026-09-02 실험 정책 "장면 전환마다 내레이션") — 소스 5초+
    # 건너뛰는 전환 중 다리 내레이션이 붙은 비율. 반려 대신 여기서 강제한다.
    gap_jumps = gap_bridged = 0
    for _i in range(1, len(beats)):
        _p, _c = beats[_i - 1], beats[_i]
        if not _p.get("span_ids") or not _c.get("span_ids"):
            continue
        _a, _z = _p["span_ids"][-1], _c["span_ids"][0]
        if _a not in span_index or _z not in span_index:
            continue
        if span_index[_z]["t_in"] - span_index[_a]["t_out"] > 5.0:
            gap_jumps += 1
            if _c.get("narration"):
                gap_bridged += 1
    gap_factor = (gap_bridged / gap_jumps) if gap_jumps else 1.0
    narration = realization * bridge * gap_factor

    # ② 재료 신뢰도 — 채택 유성 span 의 확신도·판정 상태
    if voiced:
        ok = 0.0
        for sp in voiced:
            src, conf = sp.get("text_source"), sp.get("conf")
            if src == "none":
                continue                       # 대사 없음 = 0점
            if src == "heard":
                ok += 0.9                      # 확정 대사(청취) — 거의 만점
            elif conf is None:
                ok += 0.7                      # 미측정(구 문서) — 중립
            else:
                ok += min(1.0, max(0.0, (conf - 0.3) / 0.5))
        material = ok / len(voiced)
    else:
        material = 0.5                         # 대사 없는 편성 — 중립

    # ③ 아크 응집도 — 소스 시간 점프 횟수(비트 사이 불연속)
    starts = [span_index[b["span_ids"][0]]["t_in"] for b in beats
              if b.get("span_ids") and b["span_ids"][0] in span_index]
    ends = [span_index[b["span_ids"][-1]]["t_out"] for b in beats
            if b.get("span_ids") and b["span_ids"][-1] in span_index]
    jumps = sum(1 for a, b in zip(ends, starts[1:]) if abs(b - a) > 5.0)
    cohesion = max(0.0, 1.0 - jumps / max(1, len(beats) - 1))

    # ④ 진행감 — 긴 통짜 비트 비율. 임계 8s = 사람 편집 실측 비트 중앙 7.5s
    # (리플레이 하네스 614편 — 단일 작품 아닌 전 채널 분포)의 반올림. 종전 12s 는
    # 13~16s 통 클립을 통과시켰다(루즈한 호흡 실사고 — 사용자 지적).
    long_beats = sum(1 for b in beats
                     if b.get("span_ids") and b["span_ids"][0] in span_index
                     and (span_index[b["span_ids"][-1]]["t_out"]
                          - span_index[b["span_ids"][0]]["t_in"]) > 8.0)
    progression = max(0.0, 1.0 - long_beats / max(1, len(beats)))

    # ⑤ 예산 적합
    total = story_duration(beats, span_index)
    budget = max(0.0, 1.0 - abs(total - target_sec) / max(1.0, target_sec))

    # ⑦ 오디오 커버리지 — 소리(대사 span ∪ 내레이션 창)가 러닝타임을 얼마나 채우나.
    # 실사고(2026-09-01): 62% 판 — 14.7초 연속 정적. 0.8 이상 = 만점. 반려선이
    # 아니라 채점 항목이다(실측은 게이트가 아니라 계기판).
    iv = [(span_index[x]["t_in"], span_index[x]["t_out"]) for x in ids
          if x in span_index and span_index[x]["is_audio"]]
    iv += [(c["source_time_sec"], c["source_end_sec"]) for c in cues]
    iv.sort()
    covered, cur_end = 0.0, float("-inf")
    for a, z in iv:
        a = max(a, cur_end)
        if z > a:
            covered += z - a
            cur_end = z
    coverage = min(1.0, (covered / total) / 0.8) if total > 0 else 0.0

    # ⑥ 서론 금지 — hook 첫 대사가 인사말인가. + 도입 맥락(2026-09-02, 사용자
    # 지시 "그냥 보면 왜 이 얘기가 나오는지 맥락이 필요") — 이 쇼츠만 보는 사람은
    # 전후 사정을 모르므로 훅에 도입 내레이션이 없으면 감점한다(반려 아님 — 대사가
    # 자명한 편성도 있어 코드가 단정할 수 없다. 프롬프트 규칙 4-ⓐ와 한 벌).
    intro = 1.0
    hook = next((b for b in beats if b.get("role") == "hook"), None)
    if hook is None:
        intro = 0.5                      # hook 부재 — 도입 없는 편성(2026-09-02)
    if hook:
        for sid in hook.get("span_ids") or []:
            sp = span_index.get(sid)
            if not sp or not sp["is_audio"]:
                continue
            line = " ".join(str(a.get("line") or "")
                            for a in sp.get("audio_script") or [])
            if any(g in line for g in _GREETING):
                intro = 0.0
            break
        if intro > 0.0 and not (hook.get("narration") or []):
            intro = 0.5

    # ⑧ 엔딩 연장선(2026-09-02, 사용자 지적 "벌레 소동 → 평온한 마무리가 부자연" —
    # 사건이 끝난 다음 국면은 해소 장면이다) — 마지막 비트가 직전 비트와 같은
    # meaning 이거나 소스에서 5초 안에 붙으면 연장선(1.0). 떨어져 있으면 내레이션
    # 다리가 있어야 떡밥형 엔딩으로 절반, link 주장만으로는 0.4(화면에 연결이
    # 없다), 무근거 0.0.
    ending = 1.0
    if len(beats) >= 2:
        last, prev = beats[-1], beats[-2]
        l_ids = [x for x in last.get("span_ids") or [] if x in span_index]
        p_ids = [x for x in prev.get("span_ids") or [] if x in span_index]
        if l_ids and p_ids \
                and all(span_index[x].get("meaning_idx") is not None
                        for x in l_ids + p_ids):   # 구 색인엔 없다 — 판정 포기(오판 금지)
            l_mi = {span_index[x]["meaning_idx"] for x in l_ids}
            p_mi = {span_index[x]["meaning_idx"] for x in p_ids}
            gap = span_index[l_ids[0]]["t_in"] - span_index[p_ids[-1]]["t_out"]
            if l_mi & p_mi or gap < 5.0:
                ending = 1.0
            elif last.get("narration"):
                ending = 0.6
            elif last.get("link"):
                ending = 0.4
            else:
                ending = 0.0

    parts = {"narration": narration, "material": material, "coverage": coverage,
             "cohesion": cohesion, "progression": progression, "budget": budget,
             "intro": intro, "ending": ending}
    total_score = sum(parts[k] * w for k, w in RUBRIC_WEIGHTS.items())
    return {"parts": {k: round(v, 3) for k, v in parts.items()},
            "score": round(total_score, 3),
            "total_sec": round(total, 2), "narration_dropped": len(dropped)}


def pick_best(cands: list[dict], span_index: dict[str, dict], *,
              target_sec: float) -> tuple[int, list[dict]]:
    """안 목록 → (승자 인덱스, 점수표). 동점은 낮은 인덱스(결정성)."""
    table = [{"index": i, **score_story(c, span_index, target_sec=target_sec)}
             for i, c in enumerate(cands)]
    best = max(range(len(table)), key=lambda i: (table[i]["score"], -i))
    return best, table


# ── 프롬프트·호출 ───────────────────────────────────────────────────────────

# 편성 규칙 9~11 과 규칙 4·5 의 개정은 「지금 불륜이 문제가 아닙니다」 쇼츠 **166편 해부**
# (2026-08-31, 내레이션 358줄·전 행 서사 비트 라벨)에서 왔다. 정리본:
# work/apn/v3_story_prompt_final.md · 아티팩트 「불륜 쇼츠 내레이션 대본집」.
# ⚠ 실측 수치는 **반려 게이트가 아니라 참고값**이다 — 검증기에 새 숫자 조건을 넣지
# 않는다(강제는 물리적으로 깨지는 것만: 슬롯 길이·span 연속성 등 기존 구조 계약).
# 우리 완성본을 같은 해부기(scripts/shorts_anatomy.py)에 넣어 대조하고, 어긋나면
# ⚠ **예시 문구에 참고 쇼츠의 실제 문구를 쓰지 않는다**(2026-09-01 사용자 지적).
#    실측 근거로 쓴 쇼츠의 라벨·내레이션을 예시로 넣으면 모델이 그대로 베낀다 —
#    같은 작품을 재료로 줄 때는 특히. 실제로 예시 "(팩폭 시전)" 하나에서 두 판이
#    각각 '팩폭'·'시전'을 가져갔다. 예시는 합성 문구이거나 우리 실측이어야 한다.
# 산출을 반려하는 게 아니라 이 프롬프트를 고친다.
# ⚠ **프롬프트에 일화·코퍼스 수치·실명을 넣지 않는다**(2026-09-01 범용화 패스 —
#    사용자: '한 상황에 과적합될까 걱정'). 근거는 여기 주석이 든다:
#    · 규칙 2(하나의 이야기·다리): 819초 점프 실사고 — 두 시퀀스가 다리 없이 붙어
#      제목이 약속한 사건이 13.6초에 끝나고 43초가 무관한 대화였다. 첫 수정은
#      '한 시퀀스 안에서만'이었으나 공간 수갑이라 폐기(highlight·교차 아이러니
#      편성을 막고 규칙 11 과 모순) — 진짜 불변량은 '따라올 수 있는가'다.
#    · 규칙 4(12~16자): TTS 물리(0.6s 오버헤드 + 7자/초 — story 상수 주석 실측).
#      17자 초과 절단 실사례: '놀랍게도 승객들은…' → '관광 접고 <인명>'.
#    · 규칙 9(뚝)·10(호흡 자리): 166편 해부 실측(뚝 62% · 중간절단 0/166 · 호흡
#      자리 3곳) — 단일 작품 코퍼스라 수치는 여기만, 프롬프트엔 지시만.
# 🛑 그 문서의 F 조항("내레이션은 안 쓰는 편이 정상 — 39%가 0줄")은 **싣지 않는다**
#    (사용자 결정 2026-09-01). 내레이션 없는 편성은 **하나의 편집 스타일**이지 전 채널에
#    강제할 규칙이 아니다 — **기본값은 내레이션 있음**이다. 그 스타일이 필요한 채널은
#    톤 프로파일 노브로 여는 것이 자리다(story.py 는 채널을 모른다). 골격 템플릿을
#    추가할 때도 이 조항을 템플릿 desc 로 되살리지 말 것.
PROMPT_TEMPLATE = """당신은 리캡 쇼츠 구성작가다. 아래 기록(전체 구조·의미 단위·span 목록)만으로 쇼츠 1편을 편성하라. 영상은 볼 수 없고, 볼 필요도 없다 — 기록이 정본이다. 시각은 절대 쓰지 않는다: **span id 로만** 말한다.

## 작품
{work_title}{research_block}

## 템플릿 (하나 선택)
- recap_dialogue(1호 — 기본): 8비트 구조. 내레이션:원본대사 ≈ 3:7. 비트 역할 = hook(내레이션 1문장, 제목과 호응) → conflict(대사 인용) → context(내레이션 배경 서술) → silent_break(자막·내레이션 없는 장면 1회 — 호흡) → climax(핵심 대사를 편집 없이 길게) → bridge(내레이션 1문장 전환) → reaction(상대 인물 대사) → ending(도전/떡밥 대사 직후 컷 — 아웃트로 없음).
{extra_templates}

## 편성 규칙
1. 목표 {target_sec:.0f}초 · **상한 {max_sec:.0f}초 — 넘기면 반려된다.** 코드가 대신 잘라주지 않는다(자르면 네가 짠 아크가 무너진다). 완성본 = 고른 조각 길이의 합 + 내레이션 자리(문장당 ~2초)의 합 — 재료 표의 **길이 열을 더해 가며** 처음부터 상한 안으로 짜라. 비트 개수는 정하지 않는다 — 총합이 전부다.
2. 비트는 **의미 단위**다 — "몇 초짜리 조각 몇 개"를 세지 말고, 대사와 행동을 읽어 하나의 사건을 잡아라. 비트의 `lines` 는 그 사건을 이루는 대사 조각 id 목록이고 **연속일 필요가 없다** — "이 줄이 없어도 의미가 서는가"를 물어 필요 없는 대사를 빼라. 빠진 자리는 실제 컷이 된다(코드가 인접 조각끼리 묶어 클립을 만든다). 화면만으로 의미가 오는 무성 조각은 `visual` 에. 편 전체가 **하나의 이야기**여야 한다: `topic` 이 척추다 — 제목·훅·엔딩이 topic 을 열고 닫으며, topic 과 무관한 비트는 빼라. 비트마다 `why`(topic 전개에서 이 비트가 하는 일 한 줄)를 적어라. 장소·인물·시간대가 앞 비트와 바뀌면 `link` 에 그 도약이 어떻게 성립하는지 밝혀라 — 시퀀스를 넘는 도약에 link 도 내레이션도 없으면 반려된다. 유일한 기준: **작품을 전혀 모르는 사람이 한 번 보고 따라갈 수 있는가.** 멀리서 가져올수록 그 연결을 네가 만들어야 한다. 비트는 원본 시간 순서로 배치하라.
3. 한 span 은 한 비트에만. importance 높은 span 을 우선하되, 대사의 호흡(문장 시작~끝)을 자르지 마라. **대화는 주고받음이 보여야 한다** — 비난·질문 대사를 넣었으면 상대의 반응 대사도 함께 넣어라(한쪽 화자의 대사만 나열하면 시청자는 무슨 얘기인지 모른다 — "괜찮지 않아"만 있고 딸의 말이 없던 실사고). 쉼으로 쪼개진 짝(재료 표의 ↪쉼)은 함께 넣거나 함께 빼라 — 뒷조각만 넣으면 "큰일 날 뻔했네요"가 "날 뻔했네요"로 나간다. 한 구도를 길게 보여주면 루즈해진다 — **컷이 잦은 것이 쇼츠의 리듬이다.** 대사 사이의 긴 침묵은 **코드가 알아서 잘라 붙인다**(점프컷) — 무성 span 을 빼려고 연속 범위를 끊지 마라, 연속 규칙이 우선이다. 너는 대사가 밀한 구간을 골라라. 비트가 8초를 넘으면 나눠라.
4. 내레이션(narration)은 **화면이 못 주는 맥락을 주는 목소리**다 — 비트에 `narration: {{"text": "…"}}` 을 적으면 코드가 그 비트 **앞에 무음 자리를 만들어** 얹는다(원래 버릴 앞 구간을 화면만 살리고 소리를 죽인다). 길이는 실제로 합성해서 재므로 네가 계산할 필요 없다. **기본값은 '있음'이고, 자리가 셋 있다**:
   ⓐ **도입(hook)** — 이 쇼츠만 보는 사람은 인물도 전후 사정도 모른다. 첫 대사가 왜 나왔는지 한 줄로 깔아라(누구의 어떤 상황인지 — 화면 밖 정보만). 도입 내레이션이 없는 훅은 감점된다.
   ⓑ **장면 전환마다** — 장소·시간·국면이 바뀌는 비트는 다리를 놓아라. 다리는 장면 이동 보고("회사에 출근하자")가 아니다 — **다음 장면을 이해하는 데 필요한 화면 밖 맥락**(무엇이 걸려 있는지·왜 중요한지, 예: "대출이 막혀 투자가 급했던 상황")을 실어라. 맥락이 크면 짧은 두 문장. 어형은 예고형(~하자/~하러 갔다)이 완료 묘사형보다 낫다 — **소스에서 5초 넘게 건너뛰는 전환 비트는 다리가 의무이고 없으면 반려된다**(같은 장면 안 점프컷은 자유). '내레이션 한 줄 → 장면'의 교대가 리캡 쇼츠의 기본 리듬이다.
   ⓒ **엔딩** — 제목 회수 또는 떡밥 한 줄(규칙 9).
   ⚠ **자리는 네가 만든다**: 내레이션은 **그 비트의 새 장면 위에서만** 들린다 — 직전 장면 꼬리나 대사 뮤트에는 절대 얹히지 않으므로, 내레이션을 쓰는 비트는 **머리에 무성 조각 1~2개를 visual 로 포함**해 자리를 확보하라(견적: 문장 길이 ÷ 7자/초 + 0.6초). 자리가 없으면 그 내레이션은 드랍된다.
   ⚠ **내레이션의 근거는 그 비트의 기록이다**: 문구가 말하는 사건·사물은 이 편에 넣은 대사·scene_script 에 실제로 있어야 한다 — 화면에 안 나오는 행동("흔적을 발견했죠")을 지어내면 시청자는 화면과 말이 어긋나는 것부터 느낀다. 기록의 **구체 명사를 그대로** 써라(기록이 '불륜 기사'면 '수상한 흔적'이 아니라 '불륜 기사'). 도구·계획이 등장했다가 나중에 작동하는 편성(설치→추적→도착 류 인과 사슬)이면 그 연결 비트들에 다리를 놓는 것이 최우선이다 — 시청자가 길을 잃는 곳이 정확히 거기다.
   금지는 둘뿐: 다음 대사의 내용을 **먼저 말해버리는 것**(상황만 깔고 장면이 답하게), 그리고 붙어 이어지는 대사 사이에 끼우는 것(소음). **문장당 공백 포함 12~20자**("형에게 돈을 빌리러 간 동생" 15자 / "돌아온 말은 뜻밖이었죠" 12자) — 너무 길면 얹을 리드인이 모자라 드랍된다. 서술체(~했어요/~했죠).
5. 라벨(괄호 강조 텍스트)은 **여기서 만들지 않는다** — 화면을 보는 다음 단계가 초안 영상을 보며 문구·시각·위치를 함께 정한다(라벨의 재료는 표정·행동·구도라 기록만으로는 못 정한다). 편성과 카피에 집중하라.
6. 제목 2줄 — **line1(위)=상황·도입**(인물 자체보다 무슨 일이 일어났는지를 압축), **line2(아래·강조 자리)=반전·핵심 후킹**. 각 {title_max}자 이내, 10~13자가 가독 최적. 두 줄을 **공백 하나로 이어 읽어 한 호흡**이어야 한다 — line1 이 연결어미(…면/…니/…는데/…자)로 끝나면 line2 는 그 결과를 완결하는 동사 종결("비 오는 날 우산 같이 쓰면 / 철벽남도 무장해제 된다"), line1 이 명사구·완결절이면 line2 도 같은 구조("일만 하는 꼰대인 줄 알았는데 / 알고보니 29살 연하남"). 이어 읽어 어색하면 둘 중 하나를 다시 써라. **사실성(절대 규칙)**: 제목의 근거는 **이 편에 넣은 대사·장면**뿐이다 — 행동 범주 바꾸기("서류 건넴"→"사표 제출") · 감정 과장("당황"→"충격 대참사") · **미래 예단**("곧 ~한다"·"~은 며칠 못 갔다") · 관계 단정, 전부 금지. 앞으로의 전개는 nar 떡밥의 몫이지 제목의 몫이 아니다 — 아이러니를 걸고 싶으면 **그 대사 자체**를 제목으로 뽑아라(화면에 있는 맹세·선언은 사실이다). line2 는 **엔딩의 실제 방향과 일치**해야 한다 — 전개 패턴을 외삽해 반대 방향을 라벨링하지 마라. **제목은 약속이다**: line2 는 편의 **뒷부분**(전환·정점·엔딩)을 가리켜야 한다 — 두 줄이 같은 순간을 말하면 3초 만에 제목이 소진되고 볼 이유가 사라진다. 훅에서 바로 나오는 대사·사건을 line2 에 쓰지 마라(제목은 스포일러가 아니라 예고다). **장면 보고체 금지**: "~라고 따졌다/물었다/말했다"처럼 화면에 그대로 나오는 행위를 서술하고 끝나는 제목은 실패다 — 그건 요약이지 낚시가 아니다. line2 는 **결과 직전에서 멈추거나**("현장을 덮쳤더니"), **구체 명사·수치·아이러니로 긴장을 만들어야** 한다("남편 지갑에서 나온 것") — 읽은 사람이 '그래서?'를 물게 되면 성공, 다 알게 되면 실패다. 이모지 금지.
7. 서론 금지 — 인사말·자기소개·상황 설명성 대사 span 은 hook 에 채택하지 않는다(후킹은 내레이션과 사건 한복판 대사의 몫이다). 설명은 대사가 아니라 **규칙 4-ⓐ의 도입 내레이션**이 맡는다 — 사건 한복판 대사 + 맥락 한 줄이 훅의 공식이다.
8. 대사 신뢰 — `[대사없음]` span 은 **대사 인용 비트로 쓰지 마라**(무성 재료·장면으로는 가능). `[저확신 …]` 은 받아쓰기가 흔들린 구간이라 화면 자막이 깨질 수 있으니 가급적 피하고, 꼭 필요하면 그 비트의 다른 span 으로 대체하라. `[청취]` 는 확정된 대사다(그대로 써도 된다).
9. 엔딩은 **대사 직후 뚝** — 문장 중간에서 끊지 마라. 아웃트로·정리 멘트·해소 대사 금지. **엔딩은 마지막 사건의 연장선이어야 한다**: 사건이 끝난 '다음 국면'(정리·건배·식사·휴식 장면)은 그게 곧 해소라 엔딩 실격이다 — 시간이 남으면 그 사건의 리액션을 더 담아라(소동은 정점에서 끊는 것이 낚시다). 장면을 건너뛰는 엔딩은 그 화면 자체가 새 펀치·떡밥일 때만 성립하고, link 문구로 연결을 주장해도 화면에 그 연결이 안 보이면 소용없다. 그리고 엔딩 대사는 **그 자체로 펀치·선언·떡밥**이어야 한다 — 밋밋한 대사로 끝날 수밖에 없으면 마지막에 마무리 nar 한 줄을 얹어 **다음에 벌어질 일을 암시**하라. 위 작품 정보와 다른 시퀀스의 `## 시퀀스` 요약에 있는 사건은 **화면 없이도 말로 예고할 수 있다** — 지금 편의 인물이 곧 겪을 반전·아이러니를 한 구절로 던지고 끊는 것이 최고의 루프 유도다. 해소는 금지, 예고는 무기다. 단 **제목·topic 이 약속한 결정적 장면(리빌·발견)이 무성이면 잘게 썰지 말고 통째로 보여줘라** — 부스러기 컷 몇 개로는 시청자가 무엇을 봤는지 모른다. 그 장면을 이루는 무성 조각을 visual 에 연속으로 담아라.
11. **작품 전체를 보고 골라라** — 위 작품 정보와 모든 `## 시퀀스` 요약을 읽고, **앞으로 벌어질 일과 인과·아이러니로 연결되는 장면**을 골라라. 인물이 한 말·맹세·판단이 뒤에 뒤집힌다면, 그 대사는 그냥 웃긴 대사보다 훨씬 값지다 — 제목·훅·엔딩이 그 연결을 지렛대로 쓰게 구성하라 — 고르는 눈은 작품 전체여야 한다.
10. 대사 없는 호흡(silent_break)은 **짧게** — 1~2초면 충분하다(길면 오디오가 비어 이탈한다). 자리는 긴장을 만들거나 여운을 주는 곳: 절정 직전(뜸들이기)·직후(여운)·훅 직후(숨 고르기). 그 자리에 무성 span 이 있을 때만 쓴다.
## 심사 기준 — 네 3안은 코드가 채점해 1안만 산다 (채점표를 보고 짜라)
가중치 순: **내레이션 약속 이행 ×3**(적은 내레이션이 전부 자리를 얻고, 5초+ 장면 전환마다 다리가 있는가 — 계획만 하고 자리를 못 얻으면 0점) · **대사 신뢰 ×3**([청취]·고확신 대사 위주인가) · **소리 커버리지 ×2**(대사∪내레이션이 러닝타임의 80%를 채우면 만점) · **아크 응집 ×1.5**(비트 사이 소스 점프가 적을수록) · 진행감(8초+ 통짜 비트 벌점) · 목표 길이 근접 · 도입 맥락(훅 내레이션 없으면 반감) · 엔딩 연장선(마지막 비트가 직전 사건에 붙어야 — 무관한 다음 국면은 감점) 각 ×1.
{shorts_block}{tone_block}{reject_block}
## 재료 — 의미 단위와 조각 (id | 유성/무성 **길이** | importance | 내용)
{material_block}

## 후보 {n_cands}안을 내라 (중요)
서로 **다른 아크·다른 소재**로 {n_cands}개를 제안하라 — 같은 구간의 어절만 바꾼 안은 안 된다. 각 안은 위 규칙을 모두 지켜야 하고, 코드가 내레이션 실현 가능성·대사 신뢰도·응집도로 채점해 하나를 고른다. 그러니 "안전한 안"과 "과감한 안"을 섞어도 좋다. 그중 **한 안은 내레이션 최소안**(0~2문장 — 도입·마무리만, 대사가 해주면 0)으로 내라 — 재료가 촘촘하면 그 안이 이긴다.

## 출력 전 자기 점검 — 하나라도 아니면 고쳐서 내라
① 길이 열 합계 ≤ {max_sec:.0f}초인가 ② ↪ 조각의 짝을 함께 넣었는가 ③ 내레이션 쓰는 비트 머리에 무성 조각(visual)이 있는가 ④ 5초+ 장면 전환마다 다리 내레이션이 있는가 ⑤ 제목 두 줄이 각 {title_max}자 이내인가

## 출력 (JSON 만)
비트 = 의미 단위: `lines`(대사 조각 id — **비연속 허용**, 필요 없는 줄은 뺀 목록) ·
`visual`(무성 조각) · `action`(무슨 일이 일어나는지 동사구 — 지어내지 마라) ·
`why`(topic 전개에서 하는 일) · `link`(앞 비트와 도약이 있으면 성립 근거, 없으면 null) ·
`narration`(다리·도입·마무리 한 문장, 없으면 null).
{{"candidates": [
 {{"template": "recap_dialogue", "reason": "선택 사유 한 문장",
   "topic": "이 쇼츠가 결국 무엇에 관한 이야기인지 한 문장",
   "title": {{"line1": "…", "line2": "…"}},
   "beats": [
    {{"role": "hook", "action": "…", "lines": ["sp0000", "sp0002"], "visual": [],
      "why": "…", "link": null, "narration": {{"text": "짧은 문장"}}}},
    {{"role": "climax", "action": "…", "lines": ["sp0102", "sp0103"], "visual": ["sp0104"],
      "why": "…", "link": "같은 저녁 자리 — 인물 연속", "narration": null}}
   ]}}
]}}"""


def build_material_block(stage2_doc: dict, span_index: dict[str, dict]) -> str:
    """분석된 meaning 들을 시각순으로 — span 행은 모델이 고를 원자 목록.

    **시퀀스 머리글을 함께 낸다**(2026-09-01). 종전에는 meaning 을 평면 나열해서
    모델이 장면 경계를 볼 방법이 없었고, 그래서 규칙 2 의 '이야기가 통해야 한다'가
    따를 수 없는 지시였다 — 실측 사고: 819초(13분 39초) 떨어진 두 시퀀스가 한 편에
    붙어 제목이 약속한 사건이 13.6초에 끝나고 나머지 43초가 무관한 대화였다.
    Stage 1 이 이미 만든 구조를 프롬프트에 흘리기만 하면 되는 일이었다."""
    lines: list[str] = []
    for sq in stage2_doc.get("sequences") or []:
        chs = sq.get("chunks") or []
        if chs:
            lines.append(
                f"\n## 시퀀스 {sq.get('number', '?')} "
                f"[{chs[0]['time']['start']}~{chs[-1]['time']['end']}] "
                f"{sq.get('content', '')}")
        for ch in chs:
            for m in ch.get("meanings") or []:
                lines.append(f"\n### [{m['time']['start']}~{m['time']['end']}] "
                             f"{m.get('content', '')} "
                             f"(importance {m.get('importance')} · {m.get('mood', '')} · "
                             f"{'/'.join(m.get('characters') or [])})")
                for s in m.get("spans") or []:
                    sid = s.get("span_id")
                    if sid not in span_index:
                        continue
                    _dur = (schemas.parse_ts(s["time"]["end"])
                            - schemas.parse_ts(s["time"]["start"]))
                    if s.get("is_audio"):
                        speech = " / ".join(
                            f"{a.get('speaker')}: {a.get('line')}"
                            for a in s.get("audio_script") or [])
                        # M10-B: 신뢰 표기 — 모델이 못 미더운 대사를 피해 고르게
                        src = s.get("text_source")
                        conf = s.get("conf")
                        tag = ""
                        if src == "none" or not speech.strip():
                            tag = " [대사없음]"
                        elif src == "heard":
                            tag = " [청취]"
                        elif conf is not None and conf < LOW_CONF:
                            tag = f" [저확신 {conf:.2f}]"
                        # ↪ = 종결부호 없이 다음 조각으로 문장이 이어짐 — 한쪽만
                        # 고르면 반려된다(문장 반토막 검증과 한 벌)
                        if span_index[sid].get("continues_to") in span_index:
                            tag += " ↪다음과 한 문장"
                        elif span_index[sid].get("pause_cont_from") in span_index:
                            tag += " ↪앞 조각에서 쉼 뒤 이어졌을 수 있음"
                        lines.append(f"{sid} | 유성 {_dur:.1f}s | "
                                     f"imp {s.get('importance')}{tag} | {speech}")
                    else:
                        lines.append(f"{sid} | 무성 {_dur:.1f}s | "
                                     f"imp {s.get('importance')} | "
                                     f"{s.get('scene_script', '')}")
    return "\n".join(lines)


def resolve_story_templates(story_templates: tuple[str, ...] | list[str] | None,
                            ) -> tuple[str, ...]:
    """채널이 추가로 연 템플릿 → 허용 목록(기본 2종 + extras). 모르는 이름은 즉시
    실패 — 조용히 무시하면 채널은 새 문법을 켰다고 믿은 채 종전 산출을 받는다."""
    extras: list[str] = []
    for name in story_templates or ():
        name = str(name).strip()
        if not name:
            continue
        spec = STORY_TEMPLATE_SPECS.get(name)
        if spec is None:
            raise ValueError(
                f"모르는 스토리 템플릿 {name!r} — 사용 가능: "
                f"{sorted(STORY_TEMPLATE_SPECS)}")
        if spec.get("extra") and name not in extras:
            extras.append(name)
    return TEMPLATES + tuple(extras)


def build_story_prompt(stage2_doc: dict, span_index: dict[str, dict], *,
                       work_title: str, research_context: str = "",
                       target_sec: float = STORY_TARGET_SEC,
                       max_sec: float = STORY_MAX_SEC,
                       reject_note: str = "",
                       story_templates: tuple[str, ...] | list[str] | None = None,
                       tone_block: str = "",
                       shorts_hints: list[dict] | None = None,
                       ) -> str:
    research_block = ""
    if research_context:
        # 단정 금지(2026-09-02, Stage 2 와 같은 규율 — "시체였어요"·"쓰러진 시신"
        # 실사고 2건): 시놉시스가 아는 사건을 화면 기록이 보여주기 전에 제목·내레
        # 이션이 단정하면 이 편의 서사(모름·오해)가 죽고 리빌이 스포일된다.
        research_block = ("\n" + research_context.strip()[:800]
                          + "\n⚠ 위 작품 정보는 인물·관계 표기와 예고(떡밥)용이다 — "
                            "**이 편의 기록(scene_script)이 보여주지 않은 사실을 제목"
                            "·내레이션·topic 에 단정해 적지 마라.** 기록이 '쓰러진 "
                            "사람'이면 너도 거기까지만 말한다 — 인물이 모르거나 "
                            "오해하는 것은 그 자체로 서사이고, 정체를 제목이 미리 "
                            "말하면 리빌이 죽는다.")
    # 본 눈의 추천(2026-09-02 생성 레버) — Stage 1 이 영상 전체를 훑으며 지명한
    # 쇼츠감 순간들. 미지정이면 빈 문자열 = 프롬프트 종전과 바이트 동일(회귀 0).
    shorts_block = ""
    if shorts_hints:
        rows = "\n".join(
            f"- {h.get('approx_time') or '?'}쯤 · {h.get('situation')}"
            + (f" ({h.get('why')})" if h.get("why") else "")
            for h in shorts_hints[:12])
        shorts_block = (
            "\n## 본 눈의 추천 — 전체 훑기에서 찍은 쇼츠감 순간들\n"
            "영상을 직접 본 눈이 회차 전체를 비교해 지명한 후보다. **시각은 대략치**"
            "이니 재료 표에서 그 상황의 조각을 찾아 확인하고 써라. 추천이지 제한이 "
            "아니다 — 재료에서 더 강한 아크가 보이면 벗어나도 된다.\n"
            + rows + "\n")
    reject_block = ""
    if reject_note:
        reject_block = f"\n## ⚠ 직전 제안 반려 사유 — 전부 고쳐서 다시 내라\n{reject_note}\n"
    # 추가 템플릿 절·채널 톤 절 — 안 열면 둘 다 빈 문자열이라 프롬프트가 종전과
    # 바이트 동일(회귀 0). tone_block 은 style_tone.v3_story_prompt_block() 이 만든다 —
    # 이 모듈은 프로파일을 모른다(파이프라인이 만들어 넘긴다).
    allowed = resolve_story_templates(story_templates)
    extra_templates = "".join(
        "\n" + STORY_TEMPLATE_SPECS[name]["desc"]
        for name in allowed if STORY_TEMPLATE_SPECS[name].get("extra"))
    return PROMPT_TEMPLATE.format(
        shorts_block=shorts_block,
        work_title=work_title, research_block=research_block,
        n_cands=STORY_CANDIDATES,
        target_sec=target_sec, max_sec=max_sec,
        pieces_min=PIECES_MIN, pieces_max=PIECES_MAX,
        title_max=TITLE_MAX_CHARS, reject_block=reject_block,
        extra_templates=extra_templates, tone_block=tone_block or "",
        material_block=build_material_block(stage2_doc, span_index))


def _call_story_model(gemini, prompt: str) -> dict:
    """Flash 텍스트 온리 — 모델 정책(Pro 는 영상 분석만) 그대로."""
    types = gemini.types
    response = gemini.client.models.generate_content(
        model=gemini.config.flash_model_name,
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=16384,
        ))
    text = _extract_json_from_markdown(response.text or "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        try:
            obj, _rest = _loads_first_json(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"응답 JSON 파싱 실패: {e} — 앞 200자: {text[:200]!r}") from e


def _beat_doc(beats: list[dict], span_index: dict[str, dict]) -> list[dict]:
    """비트 → 문서 표기(시각은 grid lookup — 원본 절대초)."""
    out = []
    for i, b in enumerate(beats):
        first, last = b["span_ids"][0], b["span_ids"][-1]
        out.append({
            "number": i, "role": b["role"], "span_ids": list(b["span_ids"]),
            "time": {"start": schemas.format_ts(span_index[first]["t_in"]),
                     "end": schemas.format_ts(span_index[last]["t_out"])},
            "narration": b.get("narration"),
            "labels": b.get("labels") or [],
            "muted_span_ids": list(b.get("muted_span_ids") or []),
            # 감사용(2026-09-01) — 이게 빠져서 "why 0/N = 구 스키마 승자" 오진이
            # 가능했다. 검증은 통과 시점에 이미 끝났고 여기는 기록만.
            "why": b.get("why") or None,
            "link": b.get("link") or None,
            # 머리 데드에어 컷(2026-09-02) — 조립이 이 값으로 첫 클립 시작을 당긴다.
            # 직렬화를 빼먹으면 체크포인트 경유 조립에서 조용히 사라진다(why 의 교훈).
            **({"head_trim_sec": b["head_trim_sec"]}
               if b.get("head_trim_sec") is not None else {}),
        })
    return out


def run_story(gemini, stage2_doc: dict, grid: dict, *, work_title: str,
              research_context: str = "",
              target_sec: float = STORY_TARGET_SEC,
              max_sec: float = STORY_MAX_SEC,
              story_templates: tuple[str, ...] | list[str] | None = None,
              tone_block: str = "",
              shorts_hints: list[dict] | None = None,
              measure_fn=None,
              log=print) -> tuple[dict, dict]:
    """Stage 3 실행 → (story 문서, 감사 기록). 실패해도 폴백으로 반드시 1개."""
    span_index, span_order = build_span_index(stage2_doc, grid)
    if not span_index:
        raise ValueError("분석된 span 이 없다 — Stage 2 가 선행돼야 한다")
    allowed_templates = resolve_story_templates(story_templates)
    arousal = grid.get("arousal") or []
    audit: dict[str, Any] = {"attempts": [], "spans_available": len(span_index),
                             "allowed_templates": list(allowed_templates)}

    story: dict | None = None
    reject_note = ""
    pool: list[dict] = []
    for attempt in range(1 + MAX_REASKS):
        prompt = build_story_prompt(
            stage2_doc, span_index, work_title=work_title,
            research_context=research_context, target_sec=target_sec,
            max_sec=max_sec, reject_note=reject_note,
            story_templates=story_templates, tone_block=tone_block,
            shorts_hints=shorts_hints)
        log(f"  [v3/story] Flash 편성 요청 (시도 {attempt + 1}/{1 + MAX_REASKS}, "
            f"{STORY_CANDIDATES}안)")
        t0 = time.time()
        problems: list[str] = []
        notes: list[str] = []
        cands: list[dict] = []
        try:
            resp = _call_story_model(gemini, prompt)
            # M10-C: N안 수집 — 하나라도 통과하면 진행(전량 반려 시에만 재질의).
            # 구 응답(단일 안)도 그대로 받는다(하위호환).
            raw = resp.get("candidates") if isinstance(resp, dict) else None
            raw = raw if isinstance(raw, list) and raw else [resp]
            for k, one in enumerate(raw[:STORY_CANDIDATES]):
                st, pr, nt = validate_story_response(
                    one, span_index, span_order,
                    allowed_templates=allowed_templates, max_sec=max_sec,
                    require_proto=True)
                notes.extend(nt)
                if st is not None:
                    cands.append(st)
                else:
                    problems.extend(f"안{k}: {x}" for x in pr[:4])
        except ValueError as e:
            problems = [f"응답 오류: {e}"]
        rec = {"attempt": attempt + 1, "elapsed": round(time.time() - t0, 1),
               "candidates": len(cands), "problems": problems, "notes": notes}
        # 약체 단독 생존 즉시 채택 금지(2026-09-02 실사고: 반토막 반려가 두 안을
        # 거르자 hook 도 없는 22초 안이 심사 없이 채택됐다) — 통과안은 **풀에
        # 누적**하고, 2안 미만이면 남은 시도에서 반려 사유를 실어 경쟁을 확보한다.
        # 마지막 시도면 풀에 있는 것으로 심사한다(폴백보다 낫다).
        pool.extend(cands)
        if len(pool) < 2 and attempt < MAX_REASKS and problems:
            audit["attempts"].append(rec)
            log(f"  [v3/story] 통과 후보 {len(pool)}개(반려 {len(problems)}건) — "
                "경쟁 확보 재질의")
            reject_note = "\n".join(f"- {p}" for p in problems[:20]) \
                + ("\n- (참고) 통과한 안이 있으나 심사 경쟁을 위해 다시 낸다"
                   if pool else "")
            continue
        if pool:
            cands = pool
            best, table = pick_best(cands, span_index, target_sec=target_sec)
            story = cands[best]
            rec["scores"] = table
            rec["winner"] = best
            audit["attempts"].append(rec)
            audit["scores"] = table
            audit["winner"] = best
            log(f"  [v3/story] {len(cands)}안 심사 → 안{best} 채택 "
                f"(점수 {table[best]['score']} · "
                + " · ".join(f"{k} {v}" for k, v in table[best]["parts"].items()) + ")")
            break
        audit["attempts"].append(rec)
        log(f"  [v3/story] 반려 — 사유 {len(problems)}건")
        reject_note = "\n".join(f"- {p}" for p in problems[:20])

    if story is None:
        log("  [v3/story] ⚠ 재질의 소진 — highlight 코드 폴백(최소 1개 보장)")
        story = fallback_highlight(span_index, span_order, arousal,
                                   target_sec, work_title)
        audit["fallback"] = True

    beats = story["beats"]
    splits = split_beats_at_holes(beats, span_index)
    if splits:
        _hs = " · ".join(str(r["holes_sec"]) for r in splits)
        log(f"  [v3/story] 비트 내부 구멍 수리 — {len(splits)}개 비트 분할({_hs}s)")
    audit["hole_splits"] = splits
    total_before = story_duration(beats, span_index)
    paced = apply_dialogue_pacing(beats, span_index)
    if paced:
        log(f"  [v3/story] 대화 페이싱 — 무성 {len(paced)}개 span · "
            f"{round(sum(r['sec'] for r in paced), 1)}s 컷(점프컷)")
    audit["pacing_removed"] = paced
    # 예산 초과를 여기서 산술로 자르지 않는다(2026-09-02 사용자 결정 — 자연스러움
    # 우선). 초과분은 watch-trim 이 초안을 **다시 보고** 덜고, 모자라면 그 단계의
    # 산술 벨트(budget_fallback_cuts — trim_to_budget 과 같은 우선순위)가 마저 던다.
    removed: list[dict] = []
    beats = [b for b in beats if b["span_ids"]]          # 방어 — 통삭제는 규칙상 없음
    total_after = story_duration(beats, span_index)

    # 프로토 규약: **먼저 합성해서 재고, 잰 만큼 자리를 만든다.** 견적은 목소리·
    # 배속이 바뀌는 순간 틀린다 — 승자 확정 후 내레이션만 실측한다(짧은 문장 몇 개).
    # measure_fn(text) -> sec | None. 실패·부재 시 견적 폴백(조용한 실패 아님 — 로그).
    measured: dict[str, float] = {}
    if measure_fn is not None:
        for b in beats:
            for t in (b.get("narration") or []):
                if t and t not in measured:
                    try:
                        sec = measure_fn(t)
                        if sec:
                            measured[t] = float(sec)
                    except Exception as e:  # noqa: BLE001
                        log(f"  [v3/story] ⚠ 내레이션 실측 실패({t[:12]}…): {e} — 견적 폴백")
        if measured:
            log(f"  [v3/story] 내레이션 실측 {len(measured)}건 — 자리를 실측 길이로 만든다")
    cues, dropped = plan_narration_slots(
        beats, span_index, measured or None,
        budget_slack=max(0.0, max_sec - total_after))
    # 창 확장이 재료를 붙였을 수 있다 — 총량을 실제 값으로 재계산(예산 기록 정합)
    total_after = story_duration(beats, span_index)
    conflicts = verify_tts_conflicts(cues, beats, span_index)
    if conflicts:
        # 배치 규칙상 나올 수 없다 — 나오면 코드 결함이므로 크게 실패(조용한 송출 금지)
        raise AssertionError(f"TTS-대사 충돌 벨트 위반: {conflicts}")
    head_trims = trim_beat_heads(beats, span_index, cues)
    if head_trims:
        cut_sec = round(sum(r["sec"] for r in head_trims), 2)
        log(f"  [v3/story] 머리 데드에어 컷 — {len(head_trims)}개 비트 · {cut_sec}s")
        total_after = round(total_after - cut_sec, 3)
    audit["head_trims"] = head_trims
    # 예산 판정은 자리 생성·머리 트림까지 끝난 **최종 총량**으로 — 초과분은 여기서
    # 산술로 자르지 않고 watch-trim(초안 재분석) 예산 컷 몫으로 넘긴다(2026-09-02).
    budget_deficit = round(max(0.0, total_after - max_sec), 3)
    budget_unmet = budget_deficit > 0
    if budget_unmet:
        log(f"  [v3/story] 예산 초과 {budget_deficit}s — 산술 트림 안 함, "
            "watch-trim(초안 재분석)이 자연 컷으로 던다")

    doc = {
        "schema": SCHEMA_STORY,
        "template": story["template"],
        "reason": story.get("reason", ""),
        "topic": story.get("topic") or None,
        "title": story["title"],
        "beats": _beat_doc(beats, span_index),
        "narration_cues": cues,
        "narration_dropped": dropped,
        "budget": {"target_sec": target_sec, "max_sec": max_sec,
                   "total_before_sec": round(total_before, 3),
                   "total_after_sec": round(total_after, 3),
                   "removed": removed, "unmet": budget_unmet,
                   "deficit_sec": budget_deficit},
    }
    audit["tts_conflicts"] = 0
    audit["pieces"] = len(beats)
    if budget_unmet:
        log(f"  [v3/story] 예산 상태 — {total_after:.1f}s > {max_sec}s "
            f"(초과 {budget_deficit}s 는 watch-trim 예산 컷 몫, run_log 기록)")
    return doc, audit
