*설계 · V4-M3(6·6b) + V4-M4(8) · 2026-09-03*
# V4-M3/M4 인터페이스 계약 — 영상을 부르는 단계

`M1-interfaces.md`(바닥) · `M2-interfaces.md`(배선)의 후속. 여기서 짓는 것은 **후보 깔때기의
LLM 쪽 절반**이다: 6(후보 편성) · 6b(경계 정밀) · 8(시각 플래그). 6c·7·9 는 이미 있다.

> 🛑 **이 판은 실호출로 검증할 수 없다** — 이 워크트리에 `GEMINI_API_KEY` 가 없다.
> 코드와 가짜 클라이언트 테스트까지가 범위이고, **프롬프트 품질·offset 멀티파트의 실제
> 동작·토큰 실측은 키가 있는 노드의 몫**이다(기획서 §12 '추정' 목록에 더한다).
> 특히 offset 멀티파트는 **이 저장소에 선례가 0건**이다(grep 확인) — 기획서 §2-B 의
> 실측은 `docs/v4/probes/mrcheck3.py` 가 REST 로 한 것이고, SDK 배선은 여기서 처음이다.

## 0. v3 프롬프트를 그대로 못 쓰는 이유

v3 `story.PROMPT_TEMPLATE` 은 **span id 로만 말하고** 재료가 `stage2_doc`(청크 상세 분석)이다.
v4 는 그 단계를 없앴고(기획서 §4) 모델이 **절대초 + 인용**을 낸다. 그래서 재료가 다르다:

| | v3 story | v4 후보 편성(6) |
|---|---|---|
| 보는 것 | 기록만(영상 안 봄) | **영상**(720p/30fps · 표본 4/3/2) + 텍스트 |
| 재료 | `stage2_doc` 의 meaning·span 표 | 전사(시각) · 격자 요약 · 리서치 |
| 출력 좌표 | span id | **절대초 + quote** |
| 산출 | 완제품 3안(제목·내레이션·라벨) | **얇은 후보 5~16개**(제목 가안만) |

⇒ 프롬프트는 새로 쓴다. 다만 **문구 자산은 v3 에서 가져온다**(실측이 밴 문장들):
규칙 7(서론 금지) · 규칙 8(대사 신뢰 어휘 `[대사없음]`·`[저확신]`·`[청취]`) ·
템플릿 4종 설명(`story.STORY_TEMPLATE_SPECS`). 규칙 4·5·6(내레이션 자수·라벨·제목)은
**10단계 몫**이라 여기 싣지 않는다 — 실으면 모델이 후보마다 내레이션을 지어 오고 토큰을 먹는다.

## 1. 영상 호출 한 곳 — `app/v4/video.py` (신설 · 먼저 짓는다)

6·6b·8·10a·11:style 이 **같은 함수**를 부른다. 각자 만들면 fps·media_resolution·재시도·
usage 기록이 갈린다(v3 는 네 곳이 각각 조금씩 다르다 — 조사 확인).

```python
@dataclass(frozen=True)
class Clip:
    """한 요청에 붙일 영상 조각. `start_sec`/`end_sec` 가 없으면 전체다."""
    start_sec: float | None = None
    end_sec: float | None = None

def call_video(gemini, handle, prompt: str, *, sample_fps: float,
               clips: Sequence[Clip] | None = None,
               media_resolution: str | None = None,      # None | "LOW" | "HIGH"
               max_output_tokens: int = 65536,
               thinking_level: str | None = None,
               model: str | None = None,                 # None = Flash 슬롯
               log=print) -> tuple[Any, dict]:
    """영상 1회 호출 → (파싱된 JSON, usage 기록).

    · `clips` 가 여럿이면 **첨부 순서가 곧 편집 순서**다(기획서 §2-B 실측).
      각 파트는 같은 핸들에 `VideoMetadata(start_offset=…, end_offset=…, fps=…)` 로 붙는다.
      ⚠ SDK 의 offset 필드 이름·단위는 이 저장소에 선례가 없다 — 배선을 한 곳에 모아
      두는 이유가 이것이다(틀렸을 때 고칠 자리가 하나).
    · 응답 파싱은 v3 가 쓰는 것을 그대로 부른다:
      `gemini_client._extract_json_from_markdown` → `json.loads` → 실패 시 `_loads_first_json`.
      (2026-08-03 실측: 분석 22회 중 12회 파싱 실패를 그 폴백이 구제했다.)
    · **재시도 분류는 E11 규약**: 429·5xx·네트워크만 재시도(≤2, 백오프), 그 밖의 4xx 는
      즉시 실패. 조용히 다른 모델·다른 설정으로 떨어지지 않는다.
    · usage 기록(모든 호출이 남긴다 — 기획서 §8):
      `{prompt, thoughts, candidates, cached, total, finish_reason, model_version,
        elapsed_sec, sample_fps, media_resolution, parts}`
      `finish_reason` 이 MAX_TOKENS 면 **크게 남긴다**(절단은 조용하면 안 된다).

    ⚠ 핸들을 **삭제하지 않는다** — 수명은 `app/v4/proxy.py` 가 관리한다(6·6b·8·10a 공유).
    """

### ⚠ SDK offset 실측 (2026-09-03 · google-genai **2.22.0** · 이 워크트리에서 직접 확인)

```python
types.VideoMetadata.model_fields
#   start_offset : Optional[str]     ← **문자열이다. float 을 주면 ValidationError**
#   end_offset   : Optional[str]
#   fps          : Optional[float]
```

| 넘긴 값 | 결과 |
|---|---|
| `120.5` (float) | ✗ `ValidationError` |
| `120` (int) | ✗ `ValidationError` |
| `"120s"` | ✓ |
| `"120.25s"` | ✓ — **소수점 초가 된다**(조각 경계는 소수다) |
| `"120"` (접미 없음) | ⚠ SDK 는 받는다. 프로토버프 Duration 은 `s` 접미가 필요하므로 **서버에서 어떻게 읽히는지는 미검증**이다 — 반드시 접미를 붙여라 |

⇒ 초 → offset 변환은 **한 함수**로 두고(`_offset(sec) -> str`) 거기서만 포맷한다.
두 곳에서 포맷하면 한쪽이 접미를 빠뜨리는 날 조용히 다른 구간을 본다.

한 요청에 같은 `file_uri` 로 파트 여럿을 붙이는 것은 SDK 에서 정상 조립되고 **순서가 보존**된다
(직렬화 확인). 서버가 그 순서대로 이어 붙여 보는지는 기획서 §2-B 의 REST 실측이 근거이고,
**SDK 경로의 실호출은 이 워크트리에서 확인할 수 없다**(키 없음).

def clips_within_source(clips, source_duration_sec) -> tuple[list[Clip], list[dict]]:
    """보내기 전 소스 길이와 대조 → (살아남은 파트, 버린 기록). 순수.

    🛑 `endOffset` 은 소스를 넘어도 **오류 없이 조용히 클램프된다**(기획서 §7 경계 벨트).
    보내고 나면 모델이 무엇을 봤는지 알 수 없으므로 **보내기 전에** 자른다."""

def usage_note(response, *, elapsed_sec, sample_fps, media_resolution, parts) -> dict
    """응답 → usage 기록 dict. 순수(테스트 대상)."""
```

## 2. 후보 편성 — `app/v4/candidates.py` (6단계)

```python
CANDIDATES_MIN = 5            # 운영자 결정 O3
CANDIDATES_MAX = 16
TITLE_DRAFT_MAX_CHARS = 20    # v3 `_enforce_title_line_limit` · 렌더러 split_text_smart 와 같은 자
SEGMENTS_MAX = 8              # 후보 하나의 조각 상한(8단계 offset 파트 수와 한 몸)
MAX_REASKS = 2                # v3 seq_analyze.MAX_REASKS 를 import 한다(재선언 금지)

def transcript_block(grid: dict, *, max_chars: int | None = None) -> str:
    """전사를 프롬프트 모양으로 → `[120.0] 이건 정말 대단한 순간이었습니다` 줄 목록. 순수.

    ⚠ 이 블록이 곧 `fps.resolve_sample_fps(text_tokens=…)` 가 재려던 그 텍스트다.
    4단계는 추정치를 쓰므로(계약 M2 §2) 6단계는 **실제 블록 길이**를 기록해 둔다 —
    M8 이 그 둘을 맞대어 환산 상수를 갈아낀다."""

def build_prompt(*, work_title: str, transcript: str, grid_summary: str,
                 research_context: str = "", hints: dict | None = None,
                 templates: tuple[str, ...], target_sec: float, max_sec: float,
                 n_min: int = CANDIDATES_MIN, n_max: int = CANDIDATES_MAX,
                 reject_note: str = "") -> str:
    """6단계 프롬프트. 순수.

    반드시 담을 것: ① 절대초로 답하라(시:분:초 아님 — 초 실수) ② 조각마다 **그 구간에서
    실제로 발화되는 대사 한 줄**을 `quote` 로 **전사에서 그대로 옮겨라**(다듬지 마라 —
    6c 가 이걸로 시각을 검증한다. 대사가 없는 조각은 `null`) ③ 서로 **다른 아크**로
    {n_min}~{n_max}개 ④ 제목은 가안 한 줄씩 {TITLE_DRAFT_MAX_CHARS}자 ⑤ 인트로·예고·
    크레딧 구간을 함께 신고하라(`exception_sector`).

    ⚠ 규칙 4·5·6(내레이션 자수·라벨·제목 확정)은 **싣지 않는다** — 10단계 몫이다."""

def validate_response(resp, *, source_duration_sec: float,
                      templates: tuple[str, ...],
                      n_min: int = CANDIDATES_MIN, n_max: int = CANDIDATES_MAX,
                      ) -> tuple[list[dict] | None, dict | None, list[str]]:
    """→ (후보 목록 | None, exception_sector | None, 반려 사유 목록). 순수.

    검사(전부 **구조**다 — 내용 판정은 6c·7 의 몫):
      · 후보 수 n_min~n_max(모자라면 반려 사유에 몇 개 왔는지 적는다)
      · id 유일(없으면 `c%02d` 로 부여) · template 화이트리스트 · reason 비지 않음
      · segments 1~SEGMENTS_MAX · 각 조각 start < end · 숫자 · 소스 범위 안
      · quote 는 문자열 또는 null(다듬지 말라는 지시는 프롬프트 몫, 검증은 6c)
      · title_draft 는 {line1, line2} 각 TITLE_DRAFT_MAX_CHARS 이내 —
        ⚠ **자수 초과는 반려가 아니라 잘라내고 노트**(가안이다. 여기서 반려하면 제목 한 줄
        때문에 후보 16개가 통째로 날아간다). 10단계가 strict 로 다시 건다.
      · exception_sector 키는 `grid.schemas.EXCEPTION_KEYS` 안(모르는 키는 반려)

    ⚠ 하나가 걸려도 **그 후보만** 버리고 나머지는 살린다. 전량이 걸릴 때만 None."""

def run_candidates(gemini, handle, *, work_title, grid, research, sample_fps,
                   templates, target_sec, max_sec, log=print) -> tuple[dict, dict]:
    """1콜(+재질의 ≤MAX_REASKS) → (`checkpoint_candidates` 의 후보 절, audit).

    반려 소진 = **편 전체 실패**(기획서 §7 — 시각 정본의 입구라 조용히 통과시키지 않는다).
    audit 에 시도별 {attempt, problems, usage} 를 전량 남긴다."""
```

## 3. 경계 정밀 — `app/v4/boundary.py` (6b · 운영자 결정 O6)

v3 `refine.py` 를 **순수 함수로 옮겨** 쓴다. v3 는 `stage1_doc.sequences` 에 묶여 있고
(retile·커버리지 재계산) v4 에는 sequences 가 없다 — 그 부분은 **가져오지 않는다**.

```python
PROBE_WINDOW_SEC = 60.0       # O6 — v3 는 90.0
TAIL_PROBE_SEC = 180.0        # exception 신고가 없는 편의 의무 확인(기획서 §3)
PROBE_SAMPLE_FPS = 6.0        # v3 refine 과 같은 자
FLASH_BUDGET = 8              # v3 refine.FLASH_BUDGET 을 import(재선언 금지)

def probe_windows(exception_sector: dict, duration_sec: float, *,
                  window_sec: float = PROBE_WINDOW_SEC,
                  tail_sec: float = TAIL_PROBE_SEC) -> list[dict]:
    """신고된 경계마다 ±window 창 + 신고 없으면 꼬리 의무 창. 순수.

    ⚠ v3 가 ±90 인 이유는 가왕쇼 지각 49.5초가 ±30 창 **밖**이었기 때문이다.
    O6 이 ±60 으로 좁혔으므로 **창 밖 재프로브 경로가 필수**다(아래) — 좁힌 창 하나로
    끝내면 그 사고가 돌아온다."""

def apply_boundary(exception_sector: dict, probe: dict, new_t: float,
                   grid_times: list[float]) -> tuple[dict, list[str]]:
    """모델이 제안한 경계 → 격자 스냅 후 반영 → (새 sector, 노트). 순수.
    스냅은 `grid.schemas.snap_time` 을 부른다(수식 복제 금지)."""

def run_boundary_probe(gemini, handle, *, exception_sector, grid, duration_sec,
                       budget: int = FLASH_BUDGET, log=print) -> tuple[dict, dict]:
    """→ (정정된 exception_sector, audit).

    · 창 하나당 1콜(offset 멀티파트 · fps PROBE_SAMPLE_FPS · 무음 불가라 오디오 포함).
    · 제안 경계가 **창 밖**을 가리키면 그 방향으로 **한 창 더**(예산 안에서).
    · verify 단계: 정정된 경계 근처를 다시 보고 확인(v3 규약).
    · 실패·예산 소진 = **원판정 유지**(오염 방지 비대칭 — 잘못 옮기는 것이 안 옮기는 것보다
      나쁘다). audit 에 왜 멈췄는지 남긴다.
    · 예산은 **호출 수와 누적 토큰 둘 다** 강제하고 감사에 남긴다(기획서 §7)."""
```

## 4. 시각 사고 플래그 — `app/v4/flags.py` (8단계)

```python
FLAG_SAMPLE_FPS = 5.0         # 기획서 §3 — 후보는 60초라 예산이 넉넉하다
FLAG_MAX_OUTPUT_TOKENS = 4096 # 검수 권고 초기값(20편 p99×2 로 재설정 — 기획서 §9-A)
FLAG_CONCURRENCY = 4          # 실측 순차 78~80s → 동시 4 22~25s. TPM 확인 후 8

FLAG_KEYS = ("seam_jump", "hook_weak")

def build_flags_prompt(cand: dict, *, seam_times: list[float]) -> str:
    """8단계 프롬프트. 순수. **`finalize.py` 의 QC 프롬프트와 같은 계약**을 쓴다:

        화면 사고만 찾아라 — 취향 평가 금지. 점수를 매기지 마라.
        아래 항목에 true/false 와 근거 시각(초)만 답하라.
          · seam_jump : 조각 이음새에서 인물·장소가 설명 없이 바뀌는가
          · hook_weak : 첫 2초 안에 사건(대사·동작·리액션)이 없는가

    이음새 시각은 **편집본 좌표**로 준다(조각 길이의 누적합) — 모델이 보는 것이 이어 붙인
    영상이기 때문이다."""

def validate_flags_response(resp) -> tuple[dict | None, list[str]]:
    """→ ({seam_jump: bool, hook_weak: bool, evidence_sec: [...]} | None, 사유). 순수.
    ⚠ 모델이 점수·정도를 답하면 **반려**한다(불리언이 계약이다 — M9 원칙)."""

def run_flags(gemini, handle, cands: list[dict], *, source_duration_sec: float,
              concurrency: int = FLAG_CONCURRENCY, budget_tokens: int | None = None,
              log=print) -> tuple[dict, dict]:
    """후보별 1콜(병렬) → (`{cand_id: 플래그}`, audit).

    · 실패는 **미채점**이다 — `{status: "failed", reason: …}` 로 남기고 0점으로 읽지 않는다
      (9단계 `approve` 가 그 어휘를 본다: `FLAGS_STATUS_OK`).
    · 예산 카운터는 **Lock 안 check-and-increment**(기획서 §8 — v3 식 int 는 샌다).
    · 후보 id 정렬 순서로 결과를 담는다(결정성).
    · offset 파트가 `SEGMENTS_MAX` 를 넘거나 파트 상한에 걸리면 **인접 조각을 병합**하고,
      그래도 안 되면 그 후보는 미채점 + 사유(기획서 §5 M12)."""
```

## 5. 배선 (`app/v4/pipeline.py` 확장)

```
candidates → boundary → verify → funnel → flags → approve
```
- 6·6b 는 `checkpoint_candidates.json` 의 자기 절만 쓴다(증분 · M1 §8).
- 지문: 6 = `[격자 지문, sample_fps, 프롬프트 sha, 모델명, 템플릿 키, (n_min,n_max)]` ·
  6b = `[6 지문, window_sec]` · 8 = `[후보 timeline, FLAG_SAMPLE_FPS, 프롬프트 sha]`(후보 단위 증분).
- 8단계는 **후보 단위 증분 저장** — 한 후보가 실패해도 나머지 결과가 남는다.
- `--stop-after` 로 각 단계에서 멈출 수 있어야 한다(스모크·비용 통제).
- 6c·7·9 는 이미 있는 순수 함수를 부르기만 한다.

## 6. 이번 판에서 짓지 않는 것

10(살붙이기) · 10a(정밀 청취) · `bridge.py`(절대초→span 다리 — M2 §0) · 11단계 후반부.
전부 M5~M7 이다.
