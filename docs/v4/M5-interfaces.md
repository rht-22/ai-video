*설계 · V4-M5 — 다리 · 살붙이기 · 정밀 청취 · 2026-09-03*
# V4-M5 인터페이스 계약 — 절대초에서 span 으로

승인된 후보(절대초)를 v3 조립 기계(span)로 넘기고, 편마다 제목·내레이션·라벨·문구를 짓는다.

## 0. 조사로 확정한 사실 — 다리가 무엇을 채워야 하나

`assemble`·`story` 가 `span_index[sid]` 에서 실제로 읽는 열쇠를 전수 확인했다.

| 열쇠 | 읽는 곳 | v4 가 어디서 얻나 |
|---|---|---|
| `t_in` · `t_out` · `is_audio` | 전역(25·23·9회) | **격자 `span_candidates` 그대로** ✅ |
| `pos` | 순서(5회) | 격자 t_in 순서 ✅ |
| `text_source` · `heard_text` | `word_subtitles`(4·2회) | `"transcript"` · `""` (10a 가 채운다) ✅ |
| `conf` | 저확신 표기(3회) | whisper 단어 확률 평균 ✅ |
| **`importance`** | `trim_to_budget` · `plan_narration_slots` · `verify_tts_conflicts`(12회) | ⚠ **v4 에 원천이 없다** — 아래 §1 |
| **`audio_script`** | `speaker_colors` · `span_speaker`(5회) | ⚠ **v4 에 화자가 없다** — 아래 §1 |
| `meaning_content` · `mood` · `scene_script` | 프롬프트 재료(2·1·1회) | 빈 값(10a 가 채운다) |

> 🛑 **발견: 10a 를 끄면 화자별 자막색이 사라진다.**
> `speaker_colors` 는 `audio_script[].speaker` 로만 돈다. whisper 전사에는 화자가 없고,
> v4 는 청크 상세 분석을 없앴으므로 **10a(정밀 청취)만이 화자의 유일한 원천**이다.
> 화자별 자막색은 M13 승계 체크리스트 항목이고 가왕쇼 템플릿의 "가장 큰 특징"이다
> (CLAUDE.md V3-M13: "v3 는 전 줄 흰색이었다"를 고친 것).
> ⇒ **N1(10a 기본 on/off)을 다시 봐야 한다.** 이 판은 두 경로를 다 만들고, 10a 가 꺼져
> 화자를 못 얻으면 **크게 남긴다**(조용히 흰 자막으로 나가면 안 된다).

## 1. 다리 — `app/v4/bridge.py` (신설)

M2-interfaces.md §0 이 정한 대로 **다리는 10단계 입구 한 곳**이다. 두 곳에 두면 어긋난다.

```python
DEFAULT_IMPORTANCE = 3
QUOTE_IMPORTANCE = 5
# 비대칭 스냅(기획서 §2 · rev.7 M2). 대칭 nearest 는 리액션 꼬리를 자른다(E20-B1 tail_hold).
SNAP_START_BACK_SEC = 2.0     # 시작은 앞으로 넉넉히(대사 첫 글자를 자르지 않는다)
SNAP_START_FWD_SEC = 0.5      # 뒤로는 조금만
SNAP_END_FWD_SEC = 2.0        # 끝은 뒤로만(리액션 꼬리를 남긴다)

def snap_segments(segments: list[dict], *, grid: dict,
                  source_duration_sec: float) -> tuple[list[dict], list[dict]]:
    """승인 후보의 조각 경계를 눈금으로 정착 → (스냅된 조각, 기록). 순수.

    눈금 = 전사 단어 경계 ∪ `scene_cuts` ∪ 무음 경계 ∪ {0, 러닝타임}
    (`grid_snap_times` 를 부른다 — 눈금 목록을 다시 만들지 마라).
    관용 밖이면 **원값 유지 + 기록**(억지로 당기지 않는다)."""

def spans_for(segments: list[dict], grid: dict) -> tuple[list[list[str]], list[dict]]:
    """조각 → span_ids. `app/v3/overrides.spans_in_window` 의 **중점 규칙**을 그대로 쓴다
    (수식 복제 금지 — chunk 소속·편집실 스냅과 같은 자다). 순수.

    ⚠ span 이 하나도 안 잡히는 조각은 **버리지 말고 기록**한다 — 그 조각이 격자보다
    짧다는 뜻이고, 조립이 빈 클립을 만들지 않도록 호출자가 판단한다."""

def build_span_index(grid: dict, *, quoted_spans: set[str] | None = None,
                     detail: dict | None = None) -> tuple[dict[str, dict], list[str]]:
    """격자(+선택적으로 10a 산출) → `span_index`. 순수. §0 표의 열쇠를 전부 채운다.

    · `importance`: 기본 `DEFAULT_IMPORTANCE`, **인용된 대사가 든 span 은
      `QUOTE_IMPORTANCE`**. 이유는 하나다 — 그 대사가 그 후보가 존재하는 이유인데
      `plan_narration_slots` 의 ⓑ 규칙(imp≤3 유성 뮤트)이 그걸 음소거할 수 있다.
      ⚠ v3 는 이 값을 모델의 청크 분석에서 받았다. v4 에는 그 단계가 없다 —
      **여기는 원천이 다르다는 것을 run_log 에 남긴다**(`importance_source`).
    · `audio_script`: 10a 산출이 있으면 그것, 없으면 **빈 목록**(화자 없음).
    · `conf`: 그 span 에 걸친 단어 확률의 평균(없으면 None — 판정하지 않는다)."""

def to_beats(candidate: dict, *, span_ids: list[list[str]],
             roles: list[str] | None = None) -> list[dict]:
    """승인 후보 → v3 `story_doc["beats"]` 뼈대(내레이션·라벨은 비어 있다 — 10단계가 채운다).
    조각 하나 = 비트 하나. `role` 은 모델이 준 것 또는 `build`."""
```

## 2. 살붙이기 — `app/v4/flesh.py` (10단계 · 운영자 결정 O8)

승인 편 **각각**에 1콜(+설명 1콜), **병렬**. 편별 증분 — 한 편이 실패해도 나머지는 남는다.

```python
FLESH_MAX_OUTPUT_TOKENS = 16384
TITLE_MAX_CHARS = 20          # v3 `_enforce_title_line_limit` 와 같은 자
NARRATION_SENT_CHARS = (12, 16)   # v3 규칙 4 실측 인용 — 그 문구를 그대로 쓴다
LABELS_PER_EPISODE = (2, 4)       # v3 규칙 5

def build_flesh_prompt(*, work_title, candidate, span_index, span_ids, research_context,
                       template, target_sec, max_sec, reject_note="") -> str:
    """10단계 프롬프트. 순수.

    **v3 `story.PROMPT_TEMPLATE` 의 규칙 4·5·6 을 여기서 쓴다**(6단계가 안 실은 그것들):
    내레이션 문장당 12~16자(실측 인용 2건 포함) · 라벨 2~4개 괄호형 + span 앵커 ·
    제목 2줄 각 20자. 재료는 **채택된 후보의 span 만** 싣는다 — 전량 실으면 67분 소재에서
    입력이 두 배가 된다(v3 `build_material_block` 에 `only` 인자가 없어 생긴 문제)."""

def validate_flesh_response(resp, *, span_ids: set[str],
                            title_max: int = TITLE_MAX_CHARS) -> tuple[dict | None, list[str]]:
    """→ (story 문서 조각 | None, 반려 사유). 순수.
    검사: 제목 2줄·자수(**여기서는 strict** — 가안이 아니라 확정이다) · 내레이션 문장 배열 ·
    라벨 앵커가 그 편의 span 안 · 모르는 열쇠 반려."""

def run_flesh(gemini, handle, approved: list[dict], *, grid, span_index, research,
              work_title, templates, target_sec, max_sec,
              concurrency: int = 4, log=print) -> tuple[dict, dict]:
    """승인 편마다 살붙이기(병렬) → (`{cand_id: story_doc}`, audit).

    · 실패는 **그 편만** 탈락 + 기록(다른 편을 되돌리지 않는다).
    · 전량 실패면 크게 실패한다(승인이 있었는데 낼 것이 없다 = 조용한 결번).
    · 슬롯 배치·충돌 벨트는 v3 것을 그대로 부른다:
      `story.plan_narration_slots` · `story.verify_tts_conflicts` · `story.trim_to_budget`
      (⚠ `trim_to_budget` 은 'climax' 를 하드코딩으로 보호한다 — v4 역할 이름이 다르면
      보호가 안 걸린다. 확인하고 맞추거나 기록하라.)"""

def build_description_prompt(story_doc, *, work_title, research_context) -> str
def run_description(gemini, story_doc, **kw) -> tuple[dict, dict]
    """유튜브 설명·해시태그 — **별도 텍스트 호출**(기획서 §3). story 검증기를 안 건드리려는
    분리다. 목적지는 `edit_plan.layout.description / hashtags` 가산 키."""
```

## 3. 정밀 청취 — `app/v4/detail.py` (10a · 선택 · 기본 꺼짐)

```python
DETAIL_SAMPLE_FPS = 3.0
DETAIL_WINDOW_MAX_SEC = 180.0

def detail_windows(approved_segments, *, max_sec=DETAIL_WINDOW_MAX_SEC) -> list[Clip]
    """승인 편 조각의 합집합(인접 병합) → offset 파트. 순수."""

def run_detail(gemini, handle, *, windows, grid, log=print) -> tuple[dict, dict]:
    """승자 구간만 3fps 로 다시 듣는다 → (`{span_id: {audio_script, importance, ...}}`, audit).

    · `app/v3/chunk_analyze.py` 의 응답 검증·전사 diff(각색 임계 0.35)·`heard` 산출을
      **부른다**(베끼지 마라). 전사 판정은 `textcheck.adjudicate_transcript`(M9-C).
    · 실패는 **원판정 유지**(전사 채택) — 안전장치가 본편을 막지 않는다.
    · 🛑 이 단계가 **화자의 유일한 원천**이다(§0). 꺼져 있으면 화자별 자막색이 없다."""
```

## 4. 배선

```
approve → [detail(선택)] → flesh → (M6: 11:resources 가 조립·자막·TTS)
```
- `flesh` 입구에서 다리를 건넌다(스냅 → span → span_index). **여기 한 곳이다.**
- 산출 `checkpoint_story.json` 은 **v1 모양**(기획서 §6 계약 표):
  `{title_text, clips, variants[{clips, title_text, score, tts_cues}], ...}` +
  v4 가산 `candidate_id` · `beats`(v3 모양 — 11단계가 읽는다).
  ⚠ 현지화 L3 가 `variants[*].title_text` 를 읽는다 — 빠뜨리면 JP 판 제목이 안 바뀐다.
- 10a 는 `--winner-detail` 로만 켠다. 꺼져 있고 화자를 못 얻으면 **stdout·run_log 에 남긴다**.
- `--max-shorts` 로 잘린 편은 살붙이기도 안 한다(비용).

## 5. 이번 판에서 짓지 않는 것

11단계 후반부(재료·초벌·스타일·최종·검증) 전량 — M6·M7 이다.
`assemble_edit_plan`·`word_subtitles`·TTS 합성은 11:resources 의 일이고, M5 는
**story 문서까지**만 만든다.
