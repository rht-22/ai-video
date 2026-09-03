*설계 · V4-M1 · 2026-09-03*
# V4-M1 인터페이스 계약

기획 정본 `v4-plan.md`. 이 문서는 **M1 에서 실제로 짓는 모듈의 시그니처**를 못박는다.
병렬 구현자가 이 문서만 보고 서로 맞는 코드를 쓸 수 있어야 한다. 여기 없는 것은 M1 범위 밖이다.

## 0. 구축 방식 — 포크가 아니라 승격 + 임포트

기획서 §5 의 의도는 "v3 가 v1 의 교훈을 승계하지 못한 사고를 반복하지 않는 것"이다.
v3 는 v1 모놀리스를 **부를 수 없었다**(비공개 함수). v4 는 다르다 — v3 의 판단 코드는
이미 공개 함수이므로 **7,035줄을 베끼는 것보다 부르는 것이 안전하다**(베낀 코드는 언젠가
한쪽만 고쳐진다).

- **승격**: v3 안의 격자 재료 6종을 `app/modules/grid/` 로 **물리 이동**하고 `app/v3/<name>.py`
  는 재수출 껍데기로 남긴다 → v3 은퇴 시 v4 가 함께 끊기지 않는다. v3 테스트가 회귀 0 을 증명한다.
- **추출**: v1 모놀리스의 함수를 `app/modules/` 로 옮기고 모놀리스는 재수출.
- **임포트**: 그 밖의 v3 모듈(`story`·`stage4`·`finalize`·`refine`·`chunk_analyze`·`assemble`·
  `textcheck`)은 M1 에서 **부른다**. 진짜 흡수(프롬프트 분해·통합 스키마)는 M3·M7 의 일이다.
- **AST 가드**가 이 규약을 집행한다(§7).

## 1. 단계 표 — `app/v4/steps.py`

```python
V4_STEPS: tuple[str, ...] = (
    "init", "research", "transcribe", "probe", "upload",
    "candidates", "boundary", "verify", "funnel", "flags", "approve",
    "flesh", "detail",
    "11:resources", "11:draft", "11:style", "11:render", "11:validate",
)
STEP_ALIASES: dict[str, str] = {"11": "11:resources", "6": "candidates",
                                "6b": "boundary", "6c": "verify", "7": "funnel",
                                "8": "flags", "9": "approve", "10": "flesh",
                                "10a": "detail"}
STEP_ORDER: dict[str, int]      # 이름 → 순번(0-based)

def parse_from_step(value: str | None) -> str | None:
    """별칭을 정본 id 로 정규화. 모르는 값은 ValueError(허용 목록 전량을 메시지에)."""

def should_run(step: str, from_step: str | None) -> bool:
    """`from_step` 이후(포함)의 단계인가. from_step=None 이면 전부 True.

    🛑 v3 는 이 판정을 손으로 적은 멤버십 검사 **5종**으로 했고 각각 다른 상류 집합을
    봤다(조사 gotcha 4). 규칙이 하나여야 단계를 더할 때 아무도 안 놓친다."""
```

- **순수**·부작용 없음. 모르는 단계 이름은 `KeyError` 가 아니라 `ValueError`.
- `should_run` 은 "캐시 무효화" 판정이다. 실제 캐시 재사용은 각 단계가 지문으로 다시 본다.

## 2. job 규약 — `app/modules/job.py` (신설 · v1·v3·v4 공용 지향)

v3 는 job 디렉토리·run_log·provenance 를 `pipeline.py` 안에 인라인으로 들고 있다.
M1 은 **v4 가 쓸 형태로 추출**한다(v3 를 이 함수로 바꾸는 것은 M1 잔여 — 1773 테스트 위험).

```python
def job_dir_for(outdir: Path, work_title: str, job_id: str | None) -> Path:
    """신규는 f"{work_title 공백→_}_{uuid4().hex[:8]}" · mkdir(parents=True, exist_ok=False).
    job_id 를 주면 그 디렉토리가 **이미 있어야** 한다(없으면 FileNotFoundError).
    ⚠ '특정 job_id 로 신규 생성'은 v3 와 같이 불가다 — 오케스트레이터 규약."""

def new_run_log(*, pipeline: str, job_id: str, config: Any | None = None) -> dict:
    """{"schema": "run_log/v1", "pipeline": pipeline, "job_id": …,
        "provenance": {...build_provenance(config) 또는 최소 git_sha...}, "steps": []}"""

def resume_run_log(path: Path, *, pipeline: str, job_id: str,
                   from_step: str | None, config: Any | None = None) -> dict:
    """기존 run_log 를 **이어 쓴다**(steps 에 {"step":"resume", "from_step":…} append).
    파일이 깨져 있으면 그대로 터뜨린다 — 조용한 초기화 금지가 의도다(v3 규약 승계).
    ⚠ provenance 는 최초 생성분을 유지한다."""

def append_step(run_log: dict, name: str, **fields) -> dict:
    """steps 에 {"step": name, **fields} 를 append 하고 그 dict 를 돌려준다. 순수(제자리 수정)."""

def write_run_log(path: Path, run_log: dict) -> None:
    """**원자적 기록** — 같은 디렉토리 임시 파일에 쓰고 os.replace.

    🛑 v3 는 run_log 를 `finally` 한 곳에서만 썼다 — SIGKILL 되면 그 실행의 감사 기록이
    통째로 사라진다(조사 gotcha 1). v4 는 step 마다 즉시 쓴다: usage·elapsed 기록이
    O7 승인 편수만큼 늘어나는데 그게 죽을 때 다 날아가면 비용을 되짚을 수 없다."""

def fingerprint(*parts: Any) -> str:
    """sha1(json.dumps(parts, sort_keys=True, ensure_ascii=False))[:16]. 순수.
    ⚠ 지문 재료는 부르는 쪽이 **전량 명시**한다 — v3 는 지문 4종의 재료가 서로 달라
    각각 다른 변경을 놓쳤다(조사 gotcha 9)."""

def read_json(path: Path) -> Any        # 없으면 FileNotFoundError(조용한 기본값 금지)
def write_json(path: Path, doc: Any) -> None    # 원자적
```

## 3. 표본 fps 계단 — `app/v4/fps.py`

기획서 §4. **상한 판정은 count 산식**(프레임 71 · 오디오 32)이고 이 파일이 그 정본이다.

```python
FPS_LADDER: tuple[tuple[float, float], ...] = (
    (40 * 60, 4.0), (60 * 60, 3.0), (90 * 60, 2.0),
)   # (길이 상한 초, 그 이하일 때 쓰는 표본 fps) — 운영자 결정 O2
FPS_QUANTUM = 0.05          # 90분 초과 구간의 내림 계단(결정성)
FPS_FLOOR = 0.5             # = 1 / SNAP_TOLERANCE_SEC(2.0) — 유래를 주석에 남긴다
INPUT_LIMIT = 1_048_576
TEXT_RESERVE_MIN = 30_000   # 텍스트 실측 위에 더 얹는 여유

TOKENS_PER_FRAME = 71       # count_tokens 단위 — 과금(usage 66)과 **다른 산식**이다
TOKENS_PER_SEC_AUDIO = 32
USAGE_TOKENS_PER_FRAME = 66 # 과금·예산 집계 전용(usageMetadata)
USAGE_TOKENS_PER_SEC_AUDIO = 25
HIGH_FRAME_MULTIPLIER = 4.0 # media_resolution HIGH — 프레임당 ×4 (71→284 · 66→264)

def count_tokens(duration_sec: float, fps: float, *, high: bool = False) -> int
def usage_tokens(duration_sec: float, fps: float, *, high: bool = False) -> int
def max_duration_sec(fps: float, *, budget: int) -> float

def resolve_sample_fps(duration_sec: float, *, text_tokens: int = 0,
                       ladder: tuple = FPS_LADDER) -> tuple[float, dict]:
    """소재 길이 → (표본 fps, 기록 dict). 순수·결정적.

    ① 예산 = INPUT_LIMIT − max(text_tokens, 0) − TEXT_RESERVE_MIN
    ② 계단: 길이가 ladder 의 상한 이하면 그 fps, 아니면 마지막 계단 아래로 내려간다
    ③ 예산 상한 fps_cap = floor_quantum((예산 − 32·D) / (71·D))
    ④ fps = min(계단, fps_cap)  — 계단이 예산에 안 들면 조용히 내린다(사유 기록)
    ⑤ fps < FPS_FLOOR 면 **크게 실패**(ValueError) — 메시지에 필요 fps · 하한의 유래 ·
      하한에서의 최대 길이를 함께 싣는다(비싼 인코딩·업로드 앞에서 죽는 것이 계약)

    반환 dict 키(run_log·테스트가 읽는다):
      {duration_sec, text_tokens, budget_tokens, ladder_fps, fps_cap, fps,
       est_count_tokens, est_usage_tokens, reason}
    reason 어휘: "ladder" | "budget_capped" | "floor_failed"

    ⚠ 길이를 모르면(≤0) 판정하지 않고 계단 첫 값을 그대로 준다(오판 금지)."""
```

**실측 상한(조사 확인)**: fps 4 → 52.7분 · 3 → 67.9분 · 2 → 95.6분 · 1 → 161.6분 ·
0.5 → 246.6분(예산 998,576 기준). 계단 4/3/2 는 각자 상한 안에 있고, 90분 초과는 ④가 잇는다.

## 4. 구간 검증 — `app/v4/verify.py` (6c · 운영자 결정 O5)

v1 `app/modules/timestamp_check.py` 의 판정을 **다중 구간 후보**로 넓힌 것이다.
그 모듈은 옮기지 않고 **부른다**(v1 이 계속 쓴다).

```python
QUOTE_MATCH_TOLERANCE_SEC = 5.0   # timestamp_check.TOLERANCE_SEC 와 같은 자
MIN_CANDIDATE_SEC = 40.0          # 조각을 잃고 이보다 짧아지면 후보 드롭(길이 정책 하한)
MIN_SEGMENT_SEC = 1.0

def verify_candidate(cand: dict, *, segments: list[dict], source_duration_sec: float,
                     grid_times: list[float] | None = None) -> dict:
    """후보 하나 → 판정. 순수. 넘겨받은 dict 를 건드리지 않는다.

    조각(`segments[]`)마다 넷을 본다:
      ① 소스 범위 — timestamp_check.bounds_problem 재사용(drop | clamp)
      ② 인용 대사 실재 — quote 가 있으면 timestamp_check.find_quote_times 로 찾는다
         · 그 조각 안(±TOLERANCE)에서 발견 → ok
         · 다른 시각에서만 발견 → **relocated**: 조각을 그 시각으로 옮긴다(길이 유지)
         · 어디에도 없음 → dropped(환각)
      ③ 발화 커버리지 — quote 가 있는데 그 조각에 전사 단어가 하나도 없으면 dropped
      ④ 경계 눈금 — grid_times 가 있고 ±SNAP_TOLERANCE 안에 눈금이 없으면 경고만(드롭 아님)

    반환:
      {"id": str, "verdict": "ok"|"relocated"|"dropped",
       "segments": [...],            # 살아남은 조각(재배치·클램프 반영)
       "total_sec": float,
       "notes": [{"segment": int, "action": "ok|relocated|clamped|dropped|unsnapped",
                  "why": str, ...}]}

    ⚠ 전사가 비어 있으면 ②③ 을 **판정하지 않는다**(모르는 것을 틀렸다고 하지 않는다 —
      timestamp_check 의 규율). ①④ 는 전사와 무관하므로 그대로 돈다."""

def verify_candidates(cands: list[dict], *, segments, source_duration_sec,
                      grid_times=None) -> tuple[list[dict], dict]:
    """전량 판정 → (살아남은 후보 목록, 기록).
    기록 = {"results": [...verify_candidate 반환...], "kept": [id...],
            "dropped": [{"id", "why"}...], "relocated": int, "clamped": int}
    ⚠ 전량 드롭이면 kept=[] 를 그대로 돌려준다 — 재질의 판단은 부르는 쪽(6단계)의 일이다."""
```

## 5. 결정적 깔때기 — `app/v4/funnel.py` (7)

```python
FUNNEL_KEEP = 8               # 8단계로 넘길 상한
IOU_DEDUP = 0.5               # 다중 구간 **합집합** IoU (기획서 §3 — v3 의 0.7 을 조인다)
MIN_SPEECH_COVERAGE = 0.55    # E20-B4 와 같은 자
STALL_MAX_GAP_SEC = 12.0      # 장면 전환 간 최대 간격(소프트 신호)

def union_iou(a: list[dict], b: list[dict]) -> float:
    """다중 구간 두 후보의 합집합 IoU. 순수.
    ⚠ v3 `_dedup_overlapping_candidates` 는 단일 구간 IoU 라 v4 후보 형태에 맞지 않는다."""

def hard_problems(cand: dict, *, exception_sectors: dict, source_duration_sec: float,
                  speech_intervals: list[tuple[float, float]],
                  min_sec: float, max_sec: float) -> list[str]:
    """탈락 사유 목록(빈 리스트 = 통과). **사실만** — 점수 아님.
      · 예고·크레딧 구역과 겹침(꼬리 구역은 하드) · 소스 밖
      · 발화 커버리지 < MIN_SPEECH_COVERAGE · 길이 위반"""

def soft_signals(cand: dict, *, scene_cuts: list[float],
                 speech_intervals: list[tuple[float, float]],
                 words: list[dict]) -> dict:
    """감점 재료(값만 · 판정 아님):
      {"stall_max_gap_sec", "speech_coverage", "cut_mid_sentence", "segment_count",
       "cohesion_gap_sec", "lead_in_sec"}"""

def score(signals: dict, *, weights: dict | None = None) -> float
    """소프트 신호 → 순위용 점수. 낮을수록 좋다(감점 합). 순수·결정적."""

def run_funnel(cands: list[dict], *, exception_sectors, source_duration_sec,
               scene_cuts, speech_intervals, words,
               min_sec: float, max_sec: float, keep: int = FUNNEL_KEEP) -> tuple[list[dict], dict]:
    """→ (남은 후보 ≤keep, 기록). 결정적: 같은 입력이면 같은 순서.
    동점은 **소스 시각순**(첫 조각 start_sec, 그다음 id) — 6단계 나열 순서로 깨면
    문서화 안 된 위치 편향이 M9 뒷문으로 샌다(기획서 §5 M3).
    기록 = {"kept": [...], "dropped": [{"id","reasons"}], "signals": {id: {...}},
            "dedup": [{"kept","dropped","iou"}], "keep_cap": keep}"""
```

## 6. 승인 게이트 — `app/v4/approve.py` (9 · 운영자 결정 O7)

```python
MAX_SHORTS_DEFAULT = 8
MAX_SHORTS_LIMIT = 8

def is_approved(cand_id: str, *, funnel_kept: set[str], verify_ok: set[str],
                flags: dict) -> tuple[bool, list[str]]:
    """나가도 되는가 → (승인, 사유 목록). **결함 게이트 전부 통과**만이 조건이다:
      깔때기 하드 통과 ∧ 6c 통과 ∧ seam_jump=False ∧ hook_weak=False ∧ **채점됨**
    ⚠ 미채점(8단계 실패)은 승인 불가다 — 0점이 아니라 '모른다'이고, 모르는 것을 내보내지
      않는다. 그 후보가 1위였다면 아래 폴백이 경고와 함께 살린다."""

def approve(*, funnel: dict, verify: dict, flags: dict, ranking: list[dict],
            max_shorts: int = MAX_SHORTS_DEFAULT) -> dict:
    """→ {"approved": [id...], "rejected": [{"id","reasons"}],
          "fallback": bool, "capped": int, "max_shorts": int}

    · 승인 편은 **전부** 낸다(top-K 아님). 순위는 파일 번호·발행 순서에만 쓴다.
    · 승인 0 이면 1위를 `fallback=True` 와 함께 낸다 — 무인 노드의 조용한 결번이
      가장 나쁘다(레포에 4중으로 깔린 '최소 1편 보장').
    · max_shorts 로 자르면 `capped` 에 몇 편을 잘랐는지 남긴다(조용한 절단 금지).
    · 🛑 v1 은 max_shorts 를 1~3 으로 클램프한다(cli.py:632 · pipeline.py:3269 ·
      config.max_shorts_count=3). v4 는 자기 진입점에서 1~8 로 정하고 그 클램프를
      물려받지 않는다 — 물려받으면 4번째부터 조용히 사라진다."""

def clamp_max_shorts(value: int | None) -> int
    """None → MAX_SHORTS_DEFAULT · 범위 밖은 ValueError(조용한 클램프 금지)."""
```

## 7. 승격·추출 — 물리 이동

| 무엇 | 어디서 | 어디로 | 남기는 것 |
|---|---|---|---|
| `schemas` `timegrid` `scenecut` `audio` `arousal` `transcribe` | `app/v3/` | `app/modules/grid/` | `app/v3/<name>.py` = `from app.modules.grid.<name> import *  # noqa` + 원래 이름 재수출 |
| `clips_beyond_source` | `app/pipeline.py:688` | `app/modules/clip_guard.py` | 모놀리스에서 import 후 재수출 |
| `_resolve_cue_anchors` · `snap_cues_to_dialogue_gaps` | `app/pipeline.py:723·842` | `app/modules/cues.py` | 〃 |

- 재수출 껍데기의 조건은 **v3 테스트 회귀 0**(1773건). `from x import *` 는 `_` 접두를
  안 가져오므로 비공개 이름은 명시 재수출한다.
- `app/modules/grid/__init__.py` 는 여섯 모듈을 재수출하고 `build_grid_doc`·`carve_spans`·
  `transcribe_words`·`retranscribe_gaps`·`detect_scene_cuts`·`detect_silence_intervals`·
  `load_pcm`·`compute_arousal`·`parse_ts`·`SNAP_TOLERANCE_SEC` 를 패키지 수준에 올린다.

**AST 가드**(`tests/test_v4_guards.py`):
- `app/v4/*` 는 `app.pipeline` 을 import 하지 않는다.
- 그런데 §7 추출 함수(`clips_beyond_source` 등)는 **호출된다** — 이름이 v4 소스에 있어야 한다.
- `app/v4/*` 는 `app.v3.pipeline` 도 import 하지 않는다(배선은 v4 것이다).
- `app/v3/*` 재수출 껍데기가 원래 공개 이름을 전부 다시 내놓는다.

## 8. 자료 모양 — `checkpoint_candidates.json`

6~9 가 한 파일에 **증분으로** 쌓는다. 단계마다 자기 절만 쓰고 남의 절은 건드리지 않는다.

```json
{"schema": "v4_candidates/v1",
 "fingerprint": "…",
 "source_duration_sec": 4020.0,
 "sample_fps": 3.0,
 "candidates": [{"id": "c01", "template": "recap_dialogue",
                 "segments": [{"start_sec": 120.0, "end_sec": 145.5,
                               "quote": "실제 대사 한 줄" }],
                 "reason": "한 줄 사유", "title_draft": "제목 가안"}],
 "exception_sectors": {"intro": {"start_sec": 0.0, "end_sec": 43.0},
                       "teaser": {…}, "credit": {…}},
 "verify":   {"results": […], "kept": […], "dropped": […]},
 "funnel":   {"kept": […], "dropped": […], "signals": {…}, "dedup": […]},
 "flags":    {"c01": {"seam_jump": false, "hook_weak": false, "evidence_sec": []}},
 "rank":     [{"id": "c01", "score": 1.5, "signals": {…}}],
 "approval": {"approved": ["c01"], "rejected": […], "fallback": false, "capped": 0}}
```

- `id` 는 `c%02d`(6단계 부여 순서 · 1-based). 순위·파일 번호와 **다른 것**이다.
- 지문 재료: `[grid 지문, sample_fps, 프롬프트 sha, 모델명, 템플릿 키, 후보 수 범위]`.

## 9. M1 에서 짓지 않는 것 (Wave 2 · 이후 마일스톤)

`candidates.py`(6 프롬프트·검증) · `boundary.py`(6b) · `flags.py`(8) · `flesh.py`(10) ·
`detail.py`(10a) · `pipeline.py`·`cli.py` 배선 · 11단계 어댑터(`render_adapter`) ·
계약 대조 도구 · 연출 통합 스키마. M1 은 **그것들이 올라설 바닥**만 만든다.
