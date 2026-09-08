# v3 갭 작업 인수인계 — 2단계 이후 (다른 세션용, 2026-09-07)

> **진행 상태(2026-09-08)**: §2(2단계)·§3(3단계)·§4-1·§4-3·§5 구현 완료, EP01 실런 2회 통과 — 기록은
> `docs/v3_gaps_from_manual_shorts.md` 「2·3·4단계 실행 기록」「별건 실행 기록」. **남은 것**: §4-2 편별 잡
> 레이아웃(§4-4 결정 1 대기), §4-4 결정 2·3, 갭 12 실렌더 A/B(코드는 design 키로 게이트됨).

이 문서는 **처음 보는 세션이 그대로 착수할 수 있게** 쓴 실행 명세다. 배경·근거는
`docs/v3_gaps_from_manual_shorts.md`(갭 1~12 · 코드 실측 · 고정/루프 · 1단계 실행 기록)에 있고,
여기서는 그 문서를 **읽었다고 가정하지 않는다** — 필요한 사실은 다시 적었다. 다만 판단의 근거가
궁금하면 그 문서의 해당 절을 본다(절 이름을 괄호로 표시).

## 0. 시작 전에 (환경·재료·규율)

- **Python 은 `.venv/bin/python`** (시스템 python3 은 3.9 라 `import app` 자체가 실패한다).
  테스트: `.venv/bin/python -m pytest tests -q -x` — 2026-09-07 기준 **2093 통과**가 기준선이다.
- 브랜치 `v3-human-flow`. 1단계 커밋은 `0790e5f`(rebase 후). CLAUDE.md 의 규율이 전부 적용된다:
  순수 함수 · fail-loud(조용한 폴백 금지) · additive(기존 키·동작 불변, 미지정 = 종전과 바이트 동일)
  · run_log 기록 · 회귀 가드 테스트(LLM·ffmpeg 없이 도는 것 우선) · 커밋은 사용자가 시킬 때.
- **실측 재료**(레포 밖 — 다음 세션이 못 찾는 것들):
  - 소스 `~/Downloads/drama_example/EP01.mp4` — 「지금 불륜이 문제가 아닙니다」 1화, 1920×1080,
    23.976fps, 3146.325s, **레터박스 위아래 59px**(그림 1920×962).
  - 로컬 v3 잡 `outputs/지금불륜이문제가아닙니다_*` 11개. Stage 2 완료본은 `cb337459`·`3c4717c4`
    (8/8 청크). `run_log.json`·`stage1.json`·`stage2.json`·`grid.json`·`checkpoint_*.json` 이 있다.
  - 수작업 산출 `~/premiere_claude/` — `shorts_ep01_v9/v10/v11/v12/`(대조 기준 완성본),
    `builders/build12.py`, `autoframe.py`(YuNet 화자 검출), `models/face_detection_yunet_2023mar.onnx`.
  - Stage 2 기록을 시각 범위로 뽑아 보는 스크립트 패턴(이 세션이 쓴 것 — 재료 확인용):
    ```bash
    .venv/bin/python - <<'EOF'
    import json; from app.v3 import schemas
    doc=json.load(open("outputs/지금불륜이문제가아닙니다_cb337459/stage2.json"))
    lo,hi=2290,2345
    for sq in doc["sequences"]:
      for ch in sq.get("chunks",[]):
        for m in ch.get("meanings",[]):
          for s in m["spans"]:
            t0,t1=schemas.parse_ts(s["time"]["start"]),schemas.parse_ts(s["time"]["end"])
            if t1>=lo and t0<=hi: print(s["span_id"],round(t0,1),round(t1,1),"유" if s["is_audio"] else "무",s["importance"],s["scene_script"][:80])
    EOF
    ```
- **비용 감각**(EP01 실측): grid 1157s(whisper CPU) · Stage 1 191s(Pro) · Stage 2 8청크 528~664s(Pro)
  · story 15~66s(Flash) · refine 4콜 78s(Flash). **Stage 2 재실행은 편당 최대 비용**이라 프롬프트·
  스키마를 바꾸는 작업은 한 번에 묶어 한 번만 치른다.
- **재실행 방식**: 기존 잡을 APFS clone(`cp -Rc outputs/A outputs/B`)한 뒤 `--job-id B --from-step <단계>`
  (CLAUDE.md 「가왕쇼 7화 재실행」 절). 캐시 지문 규율: Stage 2 캐시(`checkpoint_chunk_analyze.json`)
  는 `chunk 계획 + span 경계` 지문, story 캐시(`checkpoint_story.json`)는 `span 경계·importance +
  길이 노브 (+ templates/tone/flow/exclude 지정 시)` 지문. **프롬프트 문구는 지문에 없다** — 문구만
  바꾸면 옛 캐시가 조용히 재사용된다. 스키마를 바꾸면 지문에 버전을 넣어야 한다(2-4).
- **실모델 국소 검증 패턴**(파이프라인 전체를 안 돌리고 한 단계만): 1단계에서 갭 7 을 이렇게 확인했다.
  ```python
  import dataclasses, json, os; from pathlib import Path
  from dotenv import load_dotenv; load_dotenv(".env")
  from app.modules.gemini_client import load_gemini_client
  from app.v3.pipeline import V3_MODEL_DEFAULT
  g = load_gemini_client(); m = os.environ.get("GEMINI_V3_MODEL", V3_MODEL_DEFAULT)
  g.config = dataclasses.replace(g.config, model_name=m, flash_model_name=m)
  # 이제 g 를 refine_exception / run_chunk_analyze / run_story_flow 등에 그대로 넘긴다
  ```
- **하지 말 것**: OCR 엔진 도입(Gemini 가 프레임을 이미 본다) · `~/premiere_claude/tools/check_names.py`
  이식(textcheck 의 열화판) · 모델 규칙 위반(3.7-flash 외 금지) · requirements 추가 · v1 렌더러
  (`app/modules/renderer.py`) 수정 · Stage 2 `scene_script` "보이는 것만" 규율 완화 · 예고(teaser)
  경계의 "이른 쪽 보수" 편향 변경(가왕쇼 6화 사고의 방어선).

## 1. 지금까지 된 것 (1단계 — 커밋 0790e5f)

| 항목 | 위치 | 확인 방법 |
| :-- | :-- | :-- |
| 갭 6 인명 내부 표기 폴백 | `textcheck.check_internal_variants`/`fix_internal_variants` · `pipeline._run_m3` 배선 · run_log `name_variants` | `tests/test_v3_textcheck.py` |
| 레터박스 | `app/v3/letterbox.py` · probe 체크포인트 `picture` · `finalize.subject_crop_map(picture=)` | `tests/test_v3_letterbox.py` · EP01 y=60·h=960 |
| 갭 3 무대사 편 | `select.validate_beats(require_dialogue=)` · `silent_runs`/`silent_block` · TOPIC 예외 문구 · `narration.silent_note`·밀도 하한·`include_silent_beats` · `cover.designated_window(allow_ids=)` | `tests/test_v3_story_flow.py` 무대사 절 |
| 갭 8 첫 삽 | `cover.PROBE_PROMPT` `text_matches`/`seen` · `run_story_flow` 걸음 4·5 되돌림 1회(`COVER_REASK_MAX`) · 합성 캐시 | 같은 파일 |
| 갭 7 credit 경계 | `scenecut.detect_black_runs` · `refine.anchor_candidates`·`edge_note`·`DELTA_PROMPT`·머리 앵커 검사·`TEASER_CHECK_PROMPT` · EP01 레이블 `tests/data/v3_exception_labels/bulryun_ep01.json` · 채점기 `max_overreach` 옵트인 | `tests/test_v3_refine.py` 갭 7 절 · 실모델: 2930.75 → 2943.5 회복 |

⚠ 1단계가 바꾼 프롬프트: `select.TOPIC_PROMPT`(예외 문구 + `{silent_block}`) · `SCENES_PROMPT`
(`{silent_block}`) · `narration.PROMPT`(`{silent_note}`) · `cover.PROBE_PROMPT`(대조 칸) ·
`refine.PROBE_PROMPT`(`{edge_note}`). 이 플레이스홀더는 **모든 `.format` 호출부에 인자를 줘야 한다**
— 새 호출부를 만들면 KeyError 가 난다.

## 2. 2단계 — Stage 2 스키마 묶음 (비싼 재실행을 한 번만)

목표: Stage 2 span 기록에 `screen_text`·`has_text`·`diegesis`·`is_claim` 을 더하고, 480p 에서 못 읽은
글자를 원본 프레임으로 다시 읽는 정독 패스를 붙이고, 하류(걸음 1~5·hold)까지 배선한다. 근거는
갭 문서 「갭 1」「갭 2」「갭 5」「갭별 상세 갭 1·2」와 「1단계 실행 기록」의 주의.

### 2-1 프롬프트·검증기 (`app/v3/chunk_analyze.py`)

현재 구조: `PROMPT_TEMPLATE` 과제 3 이 span 마다 `scene_script`·`characters`·`importance`·`heard`
(+ 무성 한정 `subject_pos`) 를 요구한다. `validate_stage2_response` 가 `row` dict 를 조립하고(L200~
230 부근 `row = {...}`), `assemble_chunk_meanings` 가 §4 스키마로 옮기며(`subject_pos` 는 additive
로 키가 있을 때만), `story.build_span_index` 가 하류가 보는 색인을 만든다(`subject_pos` 를 옮겨 담는
줄 참조). **세 곳을 전부 거쳐야 하류에 도달한다** — 하나라도 빠지면 조용히 사라진다(1단계 문서의
"하류에 글자가 도달할 길이 없다").

추가할 span 필드(전부 선택·additive — 키가 없으면 종전과 같다):

| 필드 | 값 | 프롬프트 지시(과제 3 에 덧붙일 문장) | 검증 |
| :-- | :-- | :-- | :-- |
| `screen_text` | 문자열 | "화면에 글자가 보이면(메시지·기사 헤드라인·게시글·댓글·검색창·문서·명패·자막) **읽어서 그대로 옮겨라**. 원문 그대로, 요약·해석 금지. 여러 줄이면 `/` 로 잇는다. 못 읽겠으면 이 칸은 비우고 `has_text: true` 만 적어라 — **지어내지 마라**." | 문자열 아니면 폐기+note · 300자 절단+note |
| `has_text` | bool | 위 문장 | bool 아니면 폐기 · `screen_text` 가 있으면 자동 true |
| `diegesis` | `actual`(생략 가능)/`imagined`/`recalled`/`unclear` | "이 장면이 **지금 실제로 일어나는 일**인지, 인물의 상상·회상(플래시백)·꿈인지 적어라. 단서: 그 장면을 바라보는 인물의 클로즈업이 앞뒤에 있는가 · 색/초점/사운드가 바뀌는가 · 같은 시각 그 인물이 다른 장소에 있는가. **확신이 없으면 `unclear`** — 확신 없이 `actual` 로 적지 마라(상상 장면을 사건으로 적으면 제목까지 거짓이 된다)." | 화이트리스트 밖은 폐기+note(반려 아님 — 다른 항목까지 날아간다) |
| `is_claim` | bool (유성 한정) | "유성 span 에서 인물이 **앞으로의 행동을 단언·맹세·약속**하거나 남을 단정하는 대사(「저 여자가 먼저 인사해도 쌩깔 거야」류)면 `is_claim: true`." | bool 아니면 폐기 · 무성 span 이면 폐기+note |

- 출력 예시 JSON 에 세 필드를 하나씩 보여 준다(모델은 예시를 따른다).
- `MOOD_MAX_CHARS` 옆에 `SCREEN_TEXT_MAX_CHARS = 300` 상수.
- `assemble_chunk_meanings`: `**({"screen_text": ...} if ...)` 식으로 additive. meaning 수준에는
  `screen_texts`(그 meaning 의 span screen_text 목록, 순서대로)를 additive 로 붙인다 — 걸음 1·2 재료
  (`meaning_table`)가 span 을 안 보고 meaning 만 보기 때문.
- `build_span_index`: `screen_text`·`has_text`·`diegesis`·`is_claim` 을 그대로 옮겨 담는다.
- `common.meaning_rows`: `screen_texts`·`diegesis`(그 meaning 의 span 중 actual 이 아닌 값이 있으면
  그 값들의 집합) 옮겨 담기.

### 2-2 정독 패스 (`app/v3/screen_text.py` 신설)

`has_text: true` 인데 `screen_text` 가 비었거나 짧은(≤4자) span 만 대상. **장면 단위 1콜**:
- 클러스터링: 대상 span 을 grid 순으로 훑어 인접(시간 틈 ≤ 2.0s) span 을 한 장면으로 묶는다. EP01
  실측은 86 span → 18 장면(갭 문서 「갭별 상세 갭 1」의 목록이 합격 기준).
- 프레임: **원본 영상**(`video_path`)에서 장면 중앙 프레임 1장(1080p 그대로, `-q:v 3` JPEG) + 장면이
  4초를 넘으면 시작/끝 프레임 2장 더(스크롤 화면). ffmpeg `-ss t -i src -frames:v 1`.
  `verify_scene_binding` 의 프레임 추출·`types.Part.from_bytes` 호출 방식을 그대로 쓴다(청크
  파일이 아니라 원본을 읽는 점만 다르다).
- 프롬프트(Flash, temperature 0): "이 프레임(들)의 글자를 **보이는 그대로** 옮겨라. 화면 종류
  (메시지/기사/댓글/문서/검색/기타)와 원문. 못 읽는 글자는 `?` 로. 지어내지 마라. JSON:
  `{"kind": "...", "text": "...", "readable": true|false}`".
- 예산: `SCREEN_TEXT_BUDGET_PER_10MIN = 4` → 편당 상한 = ceil(러닝타임/600)×4 (EP01 ≈ 24). 긴
  장면·importance 높은 순으로 먼저. 소진·실패는 초벌 유지 + 기록(refine 의 `FLASH_BUDGET` 규율).
- 결과 기록: 대상 span 의 `screen_text` 를 채우고 `screen_text_source: "fullres"` 를 붙인다(초벌은
  `"stage2"`). run_log `steps[{step:"screen_text_read"}]` = `{scenes, calls, filled, budget}`.
- 사이드카 `checkpoint_screen_text.json` — Stage 2 캐시 지문에 묶는다(2-4). 재개 시 재호출 없음.
- 자리: `_run_m2` 에서 `stage2.json` 조립 **직전**(청크 캐시 로드 뒤) — stage2.json 은 정독 결과가
  반영된 문서 하나여야 한다(하류가 두 파일을 합치지 않게).

### 2-3 하류 배선

- `common.span_row`: 무성·유성 모두 `screen_text` 가 있으면 ` 📄 "…"` 를 뒤에 붙인다(60자 절단).
  `common.meaning_table`: `screen_texts` 가 있으면 행 끝에 ` 📄 …`(첫 1~2건). **걸음 1·2 재료**다 —
  찌라시·게시판·대본은 주제 선택의 근거다(갭 문서 「갭 1 카톡 하나가 아니다」).
- `narration.available_block`: 같은 `📄` 표기 → 내레이션이 문구를 인용할 수 있다.
- `narration.PROMPT`: 규칙 한 줄 추가 — "화면 글자(📄)가 있는 조각은 그 문구를 근거로 쓸 수 있다.
  글자 화면을 짚으면 `hold: true`."
- `cover.choose_cover` L394 부근 `group.get("hold")` 조건에 `or any(span_index[x].get("screen_text")
  for x in group["cover_ids"])` — 글자 화면은 자동 hold.
- `diegesis` 게이트(반려 아님·경고+검수 항목):
  - `select.validate_beats`: hook/climax 비트의 span 에 `imagined`/`unclear` 가 있으면 note
    "⚠ 비트 k 에 상상/불확실 장면 — 사람 확인". 반환 beats 항목에 `diegesis_flags` 를 붙인다.
  - `narration.beats_block`: 그 span 줄에 `⚠ 회상/상상(unclear)` 표시 + PROMPT 규칙: "⚠ 표시된 장면은
    사건으로 단정하는 문장을 쓰지 마라('~하는 상상을 한다'·'~했다고 믿는다' 식)".
  - `build_story_doc`: `review: [{kind:"diegesis", beat, span_ids, value}]` additive. 인명 불일치
    (`name_variants`)도 여기로 모아 **`<job>/review.json`** 을 처음 만든다(사람 게이트 산출물 —
    대시보드 노출은 ves 몫).
- `is_claim` 은 2단계에서는 기록만(4단계 레지스터 재료).

### 2-4 캐시 지문·스키마 버전

- `chunk_analyze.STAGE2_SCHEMA = "v3_stage2/v2"` 상수. `_run_m2` 지문 payload 에 `"schema":
  STAGE2_SCHEMA` 추가 → 옛 캐시(지문 불일치) 자동 폐기 + 로그. `stage2.json["schema"]` 도 이 값.
- `_run_m3` story 지문: `stage2_doc.get("schema")` 를 **v2 이상일 때만** 키로 넣는다(v1 잡의 지문은
  종전 그대로 → 회귀 0).
- 테스트: 지문에 스키마가 들어가는지, v1 문서면 지문이 종전과 같은지 문자열로 고정.

### 2-5 잘린 exception 머리 기록 (갭 7 ⑤ — 선택, 작음)

`chunk_split` 이 exception 을 물리 제거하므로 그 안의 내용은 어디에도 없다. refine 끝에서 zone 마다
머리 20초를 480p 로 잘라 Flash 에 "무엇이 보이나 두 문장"을 묻고 `exception_sector[key].head_desc`
에 남긴다(편성 재료 아님 — 3단계 사실 장부 재료). 예산은 refine `FLASH_BUDGET` 안에서 zone 당 1콜.

### 2-6 테스트·합격 기준

- LLM 없이: `tests/test_v3_stage2.py` 에 validate 케이스(필드 수용·폐기·has_text 자동·무성 is_claim
  폐기·300자 절단), assemble/build_span_index 전달, 클러스터링 순수 함수, 예산·정렬, 지문 버전.
  `tests/test_v3_story_flow.py` 에 📄 노출·hold 자동·diegesis note·review 항목.
- **EP01 실런**(합격 판정): `cp -Rc outputs/지금불륜이문제가아닙니다_cb337459 outputs/…_ep01s2` →
  `.venv/bin/python -m app.v3 --video ~/Downloads/drama_example/EP01.mp4 --work-title
  지금불륜이문제가아닙니다 --job-id …_ep01s2 --from-step seq_analyze --story-flow human --fix-names
  [채널 design 플래그는 cb337459 의 run_log.design_cli 참조]`.
  확인: ① credit 2943.5(갭 7) ② 글자 장면 18곳 중 카톡(38:30~38:51)·찌라시(37:36·39:41)·게시판
  (41:47~42:32)·기사/댓글(48:54~49:04 — 이제 본편에 포함됨) 의 `screen_text` 원문 ③ 카페 장면
  (2540~2600)의 `diegesis` 가 actual 이 아님 ④ 「먼저 인사해도」(693s) `is_claim: true` ⑤ 걸음 1 이
  카톡→침실 추궁→지갑(2296~2700) 편을 고를 수 있는가(안 고르면 `--exclude-topic` 으로 위치추적기
  편을 빼고 재시도) ⑥ 완성본을 `~/premiere_claude/shorts_ep01_v10/` 와 나란히 본다.
- 갭 문서 「1단계 실행 기록」 밑에 「2단계 실행 기록」을 같은 표 형식으로 남긴다.

## 3. 3단계 — 회차 지도 (`app/v3/episode_map.py` 신설, 텍스트 온리)

목표: Stage 2 기록 **위를 두 번 읽어**(정방향 상태 갱신 → 역방향 수정) 사실/믿음 장부·설정/회수
레지스터·열린 질문·diegesis 확정·사람 확인 목록을 만든다. **Stage 2 를 덮어쓰지 않는 additive 주석
층**이다. 근거: 갭 문서 「갭 너머 — 필요한 것 7가지」「무엇을 고정하고 무엇을 루프로 열지」.

### 3-1 입력·출력

- 입력: `stage2.json`(2단계 스키마) + `grid.json`(시각·무대사 구간) + 리서치(`checkpoint_research.json`
  의 `work_context`·`cast_images`) + (있으면) 작품 단위 이전 회차 지도(7번 — 회차 간 상태).
- 출력 `episode_map.json`:
  ```json
  {"schema": "v3_map/v1", "fingerprint": "...",
   "state_by_sequence": [{"seq": 0, "characters": {"박경희": {"knows": [...], "believes": [...], "wants": [...]}},
                          "open_questions": [...], "facts": [...], "beliefs": [...]}],
   "facts_ledger": [{"id": "f001", "text": "...", "source": "dialogue|screen_text|action", "meanings": ["m012"], "t": 2040.0}],
   "beliefs_ledger": [{"id": "b001", "holder": "박경희", "text": "...", "meanings": [...], "confirmed": null}],
   "register": [{"id": "r001", "kind": "condition|phrase|object|claim|symmetry",
                 "setup": {"meaning": "m020", "t": 2040.0, "quote": "..."},
                 "payoff": {"meaning": "m037", "t": 2344.0, "quote": "..."} | null,
                 "what_flipped": "..."}],
   "diegesis_final": {"sp1175": {"value": "imagined", "evidence": ["m043: 2431 일터"], "changed_from": "recalled"}},
   "review": [{"kind": "diegesis|name|cross_episode", "meanings": [...], "why": "..."}]}
  ```
- 캐시 지문 = sha1(stage2.json 의 schema + meaning/span 내용) — 2단계 재실행이면 자동 무효.

### 3-2 호출 구조 (Flash · 온도 0 · 시퀀스당 1콜 + 역방향 시퀀스당 1콜)

- 정방향: 시퀀스 i 의 meaning 표(+ span 의 📄·is_claim·diegesis)와 **직전까지의 상태 문서**(압축 JSON,
  4k자 상한 — 넘으면 열린 질문·레지스터만 남기고 요약)를 주고 `(이 시퀀스의 사실/믿음 갱신, 레지스터
  등록·연결, 열린 질문, 이 시퀀스 span 의 diegesis 판정)` 을 받는다. 프롬프트의 핵심 질문은 "이 장면을
  보고 나서 **이야기에 대한 이해가 어떻게 바뀌었나**"(묘사가 아니다).
- 역방향: 마지막 시퀀스부터 거꾸로, 최종 상태를 들고 "앞 시퀀스의 판정 중 뒤 증거와 충돌하는 것"만
  고친다 — 출력은 `diegesis_final` 변경분 + `beliefs.confirmed` 갱신 + 레지스터 payoff 연결. 카페
  장면(2540~2600)이 2431 일터 근거로 `imagined/unclear` 로 확정되는 것이 이 패스의 존재 이유.
- 검증기(순수): 모든 id 가 존재하는 meaning/span 인가 · register kind 화이트리스트 · setup.t < payoff.t
  · 반려·재질의 ≤2(`story_flow._loop` 재사용 — `initial_reject` 인자 있음).
- 예산: 시퀀스 수×2 + 재질의. EP01(7 시퀀스) ≈ 14~20콜.

### 3-3 배선

- CLI `--episode-map`(store_true) — **미지정 = 단계 없음(회귀 0)**. `V3_STEPS` 에 `"episode_map"` 을
  `chunk_analyze` 와 `story` 사이에 넣고 `--from-step episode_map` 허용.
- story_flow 소비(지도가 있을 때만): 걸음 1·2 프롬프트에 `register_block`(레지스터 표 — "드라마가
  이미 만들어 둔 설정↔회수·단언↔번복") 과 `facts_block`(사실 장부 요약)을 덧붙인다. `diegesis_final`
  이 span_index 의 `diegesis` 를 **덮어쓴다**(색인 층에서만 — stage2.json 은 불변).
- 갭 8 역류: `run_story_flow` 의 되돌림(`COVER_REASK_MAX`)이 `lines/topic` 수준 모순을 보면(예:
  프로브가 "지정 화면과 다른 사건"이라 두 번 판정) `episode_map.json["corrections"]` 에 관측 스키마
  `{stage, reason, evidence:{t, span_id, frame_path}}` 로 쓰고 **편당 1회** plan/story 재실행. 무한
  루프 금지.
- 사람 교정 입력: `<job>/map_overrides.json`(있으면 지도에 병합 — diegesis·인명·레지스터 항목)을
  지도 로드 직후 얹는다. 교정의 축적(갭 문서 「솔직한 한계」)의 첫 자리.

### 3-4 합격 기준

- LLM 없이: 검증기·병합·역방향 diff 적용·지문·게이트(미지정이면 파일도 프롬프트 변화도 없음).
- EP01 실런: 레지스터에 ① 도장=가족화목(2040) ↔ 침실 추궁(2344) ② 딸 추궁(2089) ↔ 아내 추궁(2347)
  대칭 ③ 「먼저 인사해도 쌩깔 거야」(693) ↔ 그 집 식탁(1247) 이 잡히는가 · 카페 장면 diegesis_final
  ≠ actual · review 에 diegesis 항목과 유지수/류지수가 있는가. 수작업 지도(Cowork 메모리
  `project_ep01_story_arc.md`)와 대조.

## 4. 4단계 — plan(N편) + contrast + 갭 9·11

목표: 회차당 N편을 지도 위에서 계획하고, 편마다 기존 5걸음을 돌린다. 근거: 갭 문서 「갭 4」「갭 5」
「갭 9」「갭 11」「코드 대조」. **사용자 결정이 먼저 필요한 항목**(§4-4)이 있다.

### 4-1 plan 단계 (`app/v3/plan.py`)

- 입력: `episode_map.json`(필수 — 지도 없는 plan 은 실측대로 같은 사건을 N번 고른다) + meaning 표 +
  N + 이미 만든 편의 `core_meanings`(`--exclude-*` 와 같은 재료).
- 프롬프트(Flash): 레지스터·사실 장부·무대사 구간·글자 장면을 주고 편 N개를 `{no, kind:
  "event|contrast|irony", topic, core_meanings[], setup?, payoff?, purpose, why_standalone}` 로 받는다.
  자기 검증 지시: 편 사이 meaning 중복 ≤20% · 회차 전 구간 분산 · 주요 사건 커버.
- 검증기(순수): id 존재 · 중복률 계산(코드가 잰다) · contrast/irony 는 setup·payoff 필수이고
  `t(setup) < t(payoff)`, 둘 다 레지스터 항목에 대응해야 한다(레지스터 밖 쌍은 note) · 시간 거리
  규칙은 **contrast/irony 에만**(event 는 붙어 있어도 된다 — v9 가 최고작).
- 출력 `checkpoint_plan.json`. run_log `plan` 단계.

### 4-2 편별 실행 레이아웃 (하류 무변경)

- 회차 잡에서 `plan` 까지 돌린 뒤, 편마다 **새 잡 디렉토리**: `--reuse-analysis <회차잡> --plan-slot k`
  → `stage1.json`·`stage2.json`·`grid.json`·`checkpoint_probe.json`·`checkpoint_research.json`·
  `episode_map.json`·`checkpoint_plan.json` 을 복사하고(`chunks/` 는 심볼릭 링크 — 재단본 재사용),
  `story` 부터 시작. 걸음 1(topic)은 plan 항목으로 대체(호출 생략), 걸음 2 부터 종전.
- 이렇게 하면 `assemble → watch_trim → stage4 → finalize` 와 오케스트레이터의 "1 잡 = 1 쇼츠" 계약이
  그대로다. 편 사이 중복 회피는 plan 이 이미 했으므로 story 에는 `exclude` 만 넘긴다.

### 4-3 story_flow 확장 (갭 5·9·11)

- topic 종류: `select.validate_topic` 이 `kind`·`setup`·`payoff` 를 받는다(additive — 없으면 event).
  contrast/irony 면 `PURPOSES` 대신 `("선언","경과","반전")` 축을 SCENES_PROMPT 에 준다.
- `ROLES` 에 `setup`·`payoff`·`hook_return` 추가. `validate_beats`: `hook_return` 비트만 `hook`
  비트의 span 재사용 허용(편당 1회, 길이 ≤ 훅). ⚠ 재사용 span 은 **덮개 후보에서도 빼야 한다**
  (`cover.designated_window`·`placed` 가 먼저 소진시킨다) · `assemble.word_subtitles` 가 span 키로
  라인을 만들면 두 번째 등장이 빠질 수 있다 — 착수 시 확인.
- 훅 회수 계약: 두 번째 등장은 자막·cue 좌표가 편집본에서 다르므로 `to_edited_sec` 가 첫 등장을
  잡지 않게 클립 인덱스로 구분한다(같은 소스 구간이 두 클립).

### 4-4 사용자 결정 필요 (착수 전 물어볼 것)

1. 편별 잡 레이아웃(위 4-2 안) · N 의 출처(채널 design 키 `shorts_per_episode`?) · 발행 순서.
2. 무대사 편 내레이션 상한(지금 12줄·16자)을 톤 프로파일 값으로 둘지.
3. `review.json` 을 대시보드 어디에 띄우는가(ves 몫 — 여기서는 파일만).

## 5. 별건 (순서 무관 · 각각 독립)

- **갭 10 라벨 확장**(2단계 뒤): `stage4.STYLE_PROMPT` 라벨 과제에 ① 지시형(`[딸 폰에 빨간 하트]`
  — `screen_text` 가 있는 클립에서만) ② 아이러니 주석(레지스터 setup↔payoff 를 가리키는 것만)을
  열고, validator 셋 추가: 컷 경계를 넘지 않음(`finalize` 라벨 배치에 클립 경계 클램프) · 인물 지목
  라벨은 그 인물이 화면에 있는 구간만(Stage 2 `characters` 로 판정) · 판정 동사 금지(거짓말/불륜
  등 단정 — `is_claim` 규율). **6번째 호출을 만들지 않는다** — Stage 4 가 이미 초안을 본다.
- **갭 12 프레이밍**(회귀 0 아님 — 실렌더 A/B 필요): v1 `app/modules/reframe.py` 의 화자 추적
  (`_pick_speaker` 입 움직임 0.3 · 0.5s 샘플링 · y 추적)을 v3 `finalize` 에 배선(클립별
  `build_crop_timeline` → `crop_timeline_map`, 채널 `face_tracking` 게이트). 검출기만 Haar →
  YuNet(`~/premiere_claude/models/*.onnx` 를 `app/assets/models/` 로, `cv2.FaceDetectorYN`).
  `subject_pos` 와 교차 검증(모델 left 인데 검출 right 면 경고).
- **자산 나머지**: 컷별 세로 오프셋 `cy`(v1 reframe 이 이미 y 를 추적하므로 갭 12 배선에 포함) ·
  프리미어용 세그먼트 참조 XML(`build12.py` 의 FCP7 XML 생성부 — edit_plan 에서 변환).
- **EP01 intro 미검출**(1단계 곁다리): Stage 1 이 0~8.0 제공사 카드를 안 잡았다 — 레이블이 miss 8.0
  으로 잡는다. Stage 1 프롬프트 과제 1 에 "제공사·제작지원 카드도 intro" 한 줄이면 될 가능성이 크다.

## 6. 각 단계 공통 체크리스트

1. 시작 전 `git status` 가 깨끗한지, `.venv/bin/python -m pytest tests -q -x` 가 통과하는지.
2. 프롬프트를 바꾸면 그 프롬프트를 쓰는 **모든 `.format` 호출부**와 회귀 가드(문자열 고정)를 갱신.
3. 스키마를 바꾸면 캐시 지문에 버전(2-4) — 옛 캐시가 조용히 재사용되는 것이 최악이다.
4. 새 필드는 additive: 키가 없을 때 종전과 **바이트 동일**한지 테스트로 고정(1단계 letterbox 의
   "그림=컨테이너면 종전 맵과 동일" 테스트가 본보기).
5. LLM 호출은 예산 상수 + run_log 기록 + 실패 시 원판정 유지(refine 규율).
6. 실모델 검증은 §0 의 국소 패턴으로 한 단계만 먼저, 전체 재실행은 그 뒤.
7. 끝나면 `docs/v3_gaps_from_manual_shorts.md` 에 「N단계 실행 기록」 표를 붙이고, 메모리
   `~/.claude/projects/-Users-sally-ves-ai-video/memory/ep01-v3-gap-evidence.md` 에 상태 한 줄.
