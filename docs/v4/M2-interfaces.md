*설계 · V4-M1 배선 + V4-M2 앞단 · 2026-09-03*
# V4-M1/M2 인터페이스 계약 — 배선과 앞단(1~5단계)

`M1-interfaces.md`(바닥 모듈)의 후속. 이 문서는 **파이프라인 배선**과 **1~5단계 실구현**,
그리고 M1 합격선인 **계약 대조 도구·AST 가드**를 못박는다. 6~11단계(영상을 부르는 단계)는
자리만 만들고 M3+ 로 남긴다.

## 0. 절대초와 span 의 다리 — 이 판의 가장 중요한 결정

기획서 §2 는 **"LLM 은 절대초를 낸다"**(v4 골격 · v5 의 span-id 강제는 채택하지 않음)이고,
**6c 가 전사로 검증**한다. 그런데 v3 의 하류 기계(`assemble.assemble_edit_plan` ·
`word_subtitles` · `plan_narration_slots` · `verify_edit_plan`)는 전부 **span_id** 로 말한다.

> **결정: 다리는 10단계 입구 한 곳이다.**
> 6·6b·6c·7·8·9 는 **절대초**로 말한다(후보 = `segments[{start_sec, end_sec, quote}]`).
> 10단계 입구에서 승인 후보를 **한 번** span 으로 옮긴다:
> ① 비대칭 스냅(기획서 §2 — start 앞 ≤2.0/뒤 ≤0.5 · end 뒤 ≤2.0, 눈금 = 전사 단어 경계
>   ∪ scene_cuts ∪ 무음 경계) → ② `app/v3/overrides.spans_in_window(grid, t0, t1)` 의
>   **중점 규칙**으로 span_ids 산출 → ③ 그 뒤로는 v3 기계를 그대로 쓴다.
>
> 그래서 시각 정본은 두 겹이다 — 후보 단계는 **게이트**(6c·소스범위·경계 벨트)가 지키고,
> 조립 단계는 **격자**가 지킨다(`verify_edit_plan` 벨트 100%는 그대로 유지).
> 이 다리를 두 곳에 두면 언젠가 어긋난다. **`app/v4/bridge.py` 한 파일에만 둔다.**

M1 은 이 다리를 **아직 짓지 않는다**(10단계가 M5 다). 다만 위 결정을 여기 적어 두어
6~9 단계를 절대초로 설계하는 근거를 남긴다.

## 1. 배선 — `app/v4/pipeline.py`

```python
def run_v4(*, video_path: Path, work_title: str, outdir: Path,
           srt_path: Path | None = None, episode: int | None = None,
           job_id: str | None = None, from_step: str | None = None,
           stop_after: str | None = None,
           skip_research: bool = False,
           max_shorts: int | None = None,
           scene_threshold: float = SCENE_THRESHOLD,
           edit_overrides_path: Path | None = None,
           log=print) -> Path:
    """v4 전 단계 배선. 반환은 job 디렉토리."""
```

- **단계 판정은 `steps.should_run` 하나만** 쓴다. 손으로 적은 멤버십 검사 금지(v3 gotcha 4).
- **`stop_after`**: 그 단계까지만 돌고 정상 종료. v3 의 `--skip-stage2/3/4` 다섯 플래그를
  하나로 대체한다(v3 는 스킵이 run_log 에 남는 방식이 제각각이었다 — gotcha 15).
  **스킵도 반드시 `step(name, skipped=…)` 로 남긴다.**
- **run_log 는 `app/modules/job.py` 의 것을 쓴다** — step 마다 즉시 원자적 기록.
- **dotenv 를 진입점에서 먼저 로드한다**(v3 gotcha 21 — 안 하면 렌더가 PATH 의 ffmpeg 8 로
  떨어져 죽는다). v3 `pipeline.py:124~128` 과 같은 순서.
- 각 단계는 `steps.should_run` 이 True 여도 **자기 지문으로 캐시를 다시 본다**. 캐시 히트도
  `step(name, cached=True)` 로 남긴다(v3 는 캐시 히트가 무기록이라 '단계 부재'가 '안 돌았다'
  인지 '캐시였다'인지 구분되지 않았다 — gotcha 5).
- 미구현 단계(6~11)는 `step(name, not_implemented="M3")` 를 남기고 **거기서 정상 종료**한다.
  조용히 건너뛰지 말고 stdout 에 무엇이 남았는지 적는다.

### 단계별 M1/M2 구현 내용

| 단계 | 무엇을 하나 | 재사용 | 산출 |
|---|---|---|---|
| `init` | job 디렉토리·run_log | `app/modules/job.py` | `run_log.json` |
| `research` | 작품·인물 검색 | `app/modules/work_researcher.py` 그대로 | `checkpoint_research.json` |
| `transcribe` | 단어 타임스탬프 + 공백 재전사 | `app/modules/grid/transcribe.py` | `checkpoint_grid_words.json` |
| `probe` | 소재 계측 → 눈금 → **표본 fps 계단** | `media_probe` · `grid.{scenecut,audio,arousal,timegrid}` · `app/v4/fps.py` | `checkpoint_probe.json` · `grid.json` |
| `upload` | **720p/30fps** 프록시 + Files API 1회 | `app/v4/proxy.py`(신설) | `checkpoint_upload.json` |
| `candidates`~`11:validate` | 자리만 — `not_implemented` | — | — |

⚠ **`research` 는 `should_run` 을 타야 한다.** v3 는 research 에 무효화 경로가 아예 없어
`checkpoint_research.json` 이 한 번 생기면 어떤 `--from-step` 으로도 재실행되지 않았다(gotcha 5).

⚠ **전사 캐시와 격자 캐시는 단위가 다르다.** 전사(가장 비싸다)는 오디오·SRT 로만 무효화되고,
격자는 장면 임계·재단 상수로도 무효화된다. v3 는 `from_step=='grid'` 로 격자를 폐기해도
words 캐시는 존재만으로 재사용했다(gotcha 6 — 의도된 동작). v4 는 **지문을 각각** 두고
그 사실을 `step` 에 남긴다.

## 2. 표본 fps 사전검사의 자리

기획서 §3: **프록시 인코딩·업로드 앞**에서 죽는다. 720p/30fps 는 인코딩·업로드가 더 비싸다.

- `probe` 단계 끝에서 `fps.resolve_sample_fps(duration, text_tokens=…)` 를 부른다.
- `text_tokens` 는 **실측**이다: 전사 텍스트 + 프롬프트·리서치. M2 에서는
  `len(전사 문자) // 2 + 15_000` 같은 추정 대신 **`grid.json` 의 전사 문자 수에서
  보수적으로 환산**하고(한국어 대략 1자 ≈ 1토큰이 아니라 실측이 필요하다) 그 근거를
  `checkpoint_probe.json` 에 남긴다. ⚠ 실측 전이므로 **추정임을 값에 명시**하라
  (`text_tokens_estimated: true`) — M8 실측 라운드에서 갈아낀다.
- 실패는 `ValueError` 로 크게. `checkpoint_probe.json` 은 쓰고 죽는다(왜 죽었는지 남는다).
- 기록: `step("probe", sample_fps=…, sample_fps_note={…})`.

## 3. 프록시·업로드 — `app/v4/proxy.py` (신설)

```python
PROXY_HEIGHT = 720          # 운영자 결정 O1 — v3 는 480
PROXY_FILE_FPS = 30.0       # 운영자 결정 O1 — v3 는 10
PROXY_CRF = 30              # ⚠ v3 값 그대로 시작. 720p/30fps 에서 파일이 크게 늘어난다 —
                            #   M2 실측 뒤 조정 대상(기획서 §12 '추정' 항목)

def proxy_path_for(output_dir: Path, *, height: int, file_fps: float) -> Path:
    """파일명에 기하를 박는다 — `scan_720p30.mp4`.

    🛑 v3 `build_scan_proxy` 는 out_path 존재만으로 재사용한다. 이름에 기하가 없으면
    480p 잔재를 720p 실행이 조용히 재사용한다(조사 지적)."""

def build_proxy(video_path: Path, out_path: Path, *, height: int = PROXY_HEIGHT,
                file_fps: float = PROXY_FILE_FPS, crf: int = PROXY_CRF,
                log=print) -> tuple[Path, dict]:
    """→ (경로, {height, file_fps, crf, bytes, elapsed_sec, reused}).
    v3 `seq_analyze.build_scan_proxy` 의 ffmpeg 인자를 기하만 바꿔 쓴다."""

def upload_handle(gemini, proxy: Path, *, log=print) -> tuple[Any, dict]:
    """Files API 업로드 + 폴링 → (핸들, {uri, name, bytes, elapsed_sec}).
    v3 `seq_analyze._upload_video` 를 부른다(그 함수는 삭제하지 않는다)."""

def release_handle(gemini, handle, *, log=print) -> None:
    """삭제. **최종 렌더 성공 뒤** 또는 실패 시 finally 에서 부른다.

    🛑 v3 는 호출 직후 `finally: files.delete` 를 **네 곳**에서 한다
    (seq_analyze:546 · chunk_analyze:658 · refine:338 · stage4:488). v4 는 6·6b·8·10a 가
    **같은 핸들을 공유**하므로 단계 안에서 삭제하면 뒷단계가 죽은 핸들을 쓴다.
    v4 는 v3 의 그 함수들을 부르지 않고 이 모듈이 수명을 관리한다."""

def handle_alive(gemini, uri_or_name: str) -> bool:
    """`files.get` 으로 확인. 48h 만료·재개 시 쓴다(실패면 재업로드 + `[cache] ⚠` 기록)."""
```

`checkpoint_upload.json` = `{"schema": "v4_upload/v1", "fingerprint": …, "proxy": {…},
"handle": {"uri", "name", "bytes"}, "uploaded_at_note": "…"}`.
⚠ 시각은 `Date.now()` 류가 아니라 파일 mtime·elapsed 로만 남긴다(결정성 — 지문에 넣지 말 것).

## 4. 계약 대조 도구 — `scripts/v4_contract_diff.py` (M1 합격선)

```
python -m scripts.v4_contract_diff --job <job 디렉토리> [--against <다른 job>] [--json]
```

기획서 §6 의 **파일·필드 표**를 코드로 옮긴 것이다. v3 가 현지화를 깨뜨린 방식이
"기존 이름을 다른 모양으로 쓴 것"이었으므로, 이 도구가 M1 의 합격선이다.

- 계약 표를 **모듈 상수**로 둔다: `CONTRACTS: dict[파일명, tuple[필수 키 경로, ...]]`
  (예: `"checkpoint_story.json": ("title_text", "variants[].title_text", "clips")`).
- job 디렉토리 하나를 받아 **있는 파일마다** 필수 키가 있는지 본다. 없는 파일은 `skipped`
  (그 단계를 안 돈 잡일 수 있다) — **없는 것과 모양이 틀린 것을 구분**한다.
- `--against` 를 주면 두 잡의 **키 집합 diff** 를 낸다(v1 잡 ↔ v4 잡 대조 — 합격선).
- 종료 코드: 위반 있으면 1. `--json` 은 기계 판독용.
- 실제 소비자를 주석에 적어라(누가 그 키를 읽는지) — 근거 없는 필수 키는 넣지 마라.

## 5. AST 가드 + 승계 체크리스트 — `tests/test_v4_guards.py` (M1 합격선)

```python
def test_v4_does_not_import_the_monoliths():
    """`app/v4/*` 는 `app.pipeline`·`app.v3.pipeline` 을 import 하지 않는다.
    배선은 v4 것이고, v1 모놀리스의 함수는 `app/modules/` 로 추출된 것을 부른다."""

def test_v4_calls_the_extracted_v1_functions():
    """추출했다고 끝이 아니다 — v3 는 '재사용'을 선언하고 한 줄도 부르지 않았다.
    M1 시점에는 아직 부르는 단계가 없으므로 **부를 대상이 존재하고 import 가능한지**를
    고정하고, 실제 호출은 그 단계를 짓는 마일스톤이 켠다(아래 표의 `milestone`)."""

ABSORB_TABLE: tuple[tuple[str, str, str], ...] = (
    # (모듈 경로, 이름, 이걸 실제로 부르기 시작하는 마일스톤)
    ("app.modules.clip_guard", "clips_beyond_source", "M3"),
    ("app.modules.cues", "resolve_cue_anchors", "M6"),
    ("app.modules.cues", "snap_cues_to_dialogue_gaps", "M6"),
    ("app.modules.grid.timegrid", "carve_spans", "M2"),
    ("app.modules.grid.transcribe", "transcribe_words", "M2"),
    ("app.modules.grid.transcribe", "retranscribe_gaps", "M2"),
    ("app.modules.timestamp_check", "find_quote_times", "M3"),
    ("app.v3.textcheck", "check_names", "M6"),
    ("app.v3.textcheck", "drop_repetition", "M6"),
    ("app.v3.assemble", "word_subtitles", "M6"),
    ("app.v3.assemble", "narration_windows", "M6"),
    ("app.v3.assemble", "split_by_windows", "M6"),
    ("app.v3.assemble", "speaker_colors", "M6"),
    ("app.v3.story", "plan_narration_slots", "M5"),
    ("app.v3.story", "verify_tts_conflicts", "M5"),
    ("app.v3.story", "build_span_index", "M3"),
    ("app.v3.refine", "boundary_probe_windows", "M3"),
    ("app.v3.stage4", "run_style", "M7"),
    ("app.v3.finalize", "plan_labels", "M7"),
    ("app.v3.finalize", "place_above_burned", "M7"),
    ("app.v3.finalize", "resolve_work_logo", "M7"),
    ("app.v3.finalize", "fit_title_sizes", "M7"),
    ("app.v3.finalize", "subtitle_fx_windows", "M7"),
    ("app.v3.finalize", "run_validate", "M7"),
    ("app.modules.subtitle_region", "runs_in_window", "M7"),
    ("app.modules.style_compose", "title_windows_owner", "M7"),
)

def test_absorb_targets_all_exist():
    """M9~M15 승계 체크리스트 — 이 이름들이 v4 가 흡수할 동작의 **주소**다.
    v3 동결 중에 누가 지우거나 이름을 바꾸면 여기서 잡힌다."""
```

## 6. CLI — `app/v4/cli.py` · `app/v4/__main__.py`

```
python -m app.v4 --video <원본.mp4> --work-title <작품명>
   [--srt 자막.srt] [--episode N] [--outdir outputs] [--job-id <재개>]
   [--from-step <단계>] [--stop-after <단계>]
   [--max-shorts 1..8]            # 기본 8 — 운영자 결정 O7·N7
   [--skip-research]
   [--scene-threshold 0.3]
   [--edit-overrides <json>]
```

- **모르는 플래그는 argparse 가 거절**한다(기획서 §6 — 받는 키 집합을 못박는다).
- `--from-step`·`--stop-after` 는 `steps.parse_from_step` 로 정규화. 허용 목록을 help 에.
- `--max-shorts` 는 `approve.clamp_max_shorts` 로 검사 — 범위 밖은 즉시 실패.
- 비싼 단계 앞 자격 검사: `GEMINI_API_KEY` 는 6단계 이상을 돌 때만 필수(E11 규율).
  `--stop-after probe` 처럼 LLM 을 안 쓰는 실행은 키 없이 돈다.
- `ensure_ffmpeg_supported()` 를 v3 CLI 와 같은 자리에서.
- ⚠ v3 CLI 의 `--skip-stage2/3/4` 다섯 플래그는 **만들지 않는다** — `--stop-after` 하나다.

## 7. 이번 판에서 짓지 않는 것

`candidates.py`(6) · `boundary.py`(6b) · `flags.py`(8) · `flesh.py`(10) · `detail.py`(10a) ·
`bridge.py`(§0 다리) · 11단계 어댑터. 전부 M3~M7 이고, 배선은 그 자리에
`not_implemented` 를 남긴다.
