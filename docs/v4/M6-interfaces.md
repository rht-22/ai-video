*설계 · V4-M6/M7 — 11단계(편집 재료 → 최종본) · 2026-09-04*
# V4-M6/M7 인터페이스 계약 — 실제 mp4 가 나오는 구간

`approve → flesh` 까지 왔다. 여기서 **승인 편마다** 재료를 만들고 렌더한다.

```
11:resources → 11:draft → 11:style → 11:render → 11:validate      (승인 편 k 번)
```

## 0. 이 판의 재사용 방침 — v3 렌더 경로를 부른다

v3 `finalize.render_final` 은 **실제로 도는 렌더 경로**다(M13~M15 의 밴드 기하·화자색·
로고·번인 자막 회피·뮤트 창이 다 그 안에 있다). v4 는 그것을 **부른다**.

> ⚠ **기획서 §1-1 은 `app/modules/render_adapter.py` 추출(v1·v3 병합)을 요구한다.**
> 이 판은 **하지 않는다** — v1 호출부에는 v3 판에 없는 것들(E10 마진·이미지 오버레이·
> 제목 창·효과음 전달)이 있고, 둘을 합치는 것은 21개 채널이 도는 v1 렌더를 건드리는
> 별건이다. 대신 **빠진 것을 가드 테스트로 못박아 보이게** 한다(§5). 합치기는 M7 잔여.

## 1. 편집 재료 — `app/v4/resources.py` (11:resources)

v3 `pipeline._run_m3` 의 뒷부분(660~755줄)이 원본이다. **읽고 같은 순서로** 옮긴다.

```python
def build_resources(story_doc, *, span_index, grid, plan, research, gemini,
                    output_dir: Path, variant: int = 1,
                    fix_names: bool = False, log=print) -> tuple[dict, list[dict], dict]:
    """→ (checkpoint_resources, subtitle_segments, audit).

    순서(v3 와 같아야 한다 — 뒤바뀌면 자막이 달라진다):
      ① `assemble.narration_windows` → 뮤트 창
      ② `assemble.word_subtitles(timeline, span_index, grid["words"], 뮤트창)` → 어절 자막
      ③ `textcheck.drop_repetition` (반복 그물 · 렌더 전 제외)
      ④ `textcheck.check_names` (경고) · `fix_names=True` 면 `textcheck.fix_names`
      ⑤ `assemble.finalize_cues` → cue (start_sec None 은 lost 로 분리 + 기록)
      ⑥ cue 마다 `tts.synthesize_tts_with_fit(shorten_fn=gemini.shorten_text)`
      ⑦ 창 초과분 물리 트림(v3 pipeline 721~732 — ffmpeg 직접 호출)

    ⚠ 인명 사전은 `research["cast_images"][].character_name` 이다(v3 와 같은 자리).
    ⚠ TTS 실패 시 v3 는 같은 이름의 옛 mp3 를 unlink 하고 path=None 으로 남긴다
      (낡은 대본이 최종 믹스에 들어가는 것을 막는 의도) — 그 동작을 그대로 지킨다."""

def resource_paths(output_dir: Path, variant: int) -> dict[str, Path]:
    """편별 산출 경로. **1위는 v1 이름 그대로**(현지화·편집실이 읽는다):
       variant 1 → `subtitle_segments.json` · `checkpoint_resources.json`
       variant n → `subtitle_segments_{n}.json` · `checkpoint_resources_{n}.json`
    ⚠ v3 훅 변형의 `*_variant_{k}.json` 과 **다른 이름**이다(그건 본편 불변의 훅 교체다)."""
```

`edit_plan.json` 조립은 `assemble.assemble_edit_plan(story_doc, span_index, …)` 를 부르고
**벨트를 반드시 건다**: `assemble.verify_edit_plan(plan, grid)["pct"] == 100.0` 아니면
`AssertionError`(v3 규약 — 구조상 100% 여야 하고 아니면 코드 결함이다).

## 2. 초벌 — 11:draft

```python
DRAFT_HEIGHT = 720        # 운영자 결정 O9 (v3 는 480)
DRAFT_FPS = 30            # O9
```
`stage4.render_draft(video_path, timeline, out_path, …)` 를 부른다.
🛑 **v3 는 `-i 원본` 에 `[0:v]trim` 을 매달아 소스 전체를 디코드한다**(3시간 소재면 3시간).
v4 는 **입력 seek**(`-ss/-to` per clip)으로 만든다 — `render_draft` 를 고칠 수 없으면
(v3 동결) v4 가 자체 함수를 갖되 **필터그래프 어휘는 그대로** 베낀다. 어느 쪽이든
"소재 길이와 무관한 시간"을 테스트가 값으로 고정한다.
산출: `draft_720.mp4` · 2위↓ `draft_720_{n}.mp4`.

## 3. 스타일 — 11:style (운영자 결정 O9 · `media_resolution=HIGH`)

`stage4.run_style(gemini, draft_path, story_doc, preset=…, windows=…, labels=…, band=…)`.

⚠ **`run_style` 은 `media_resolution` 인자를 받지 않는다**(v3 는 미지정=LOW). O9 를 지키려면
둘 중 하나다: ① v4 가 `stage4._call_style_model` 대신 `video.call_video(..., media_resolution="HIGH")`
로 부르는 얇은 대체 경로를 갖는다 ② v3 를 고친다(동결이라 안 된다).
⇒ **①.** 프롬프트·검증기(`build_style_prompt`·`validate_style_response`)는 v3 것을 그대로
부르고 **호출만** v4 것으로 한다. 그 사실을 주석에 남긴다.

🛑 **HIGH 는 720p 와 한 세트다**(기획서 §2-G — 480p+HIGH 가 가장 나쁜 조합). 초벌이
720p 인 것과 HIGH 인 것이 같은 결정(O9)에서 나왔음을 주석에 적어라.

산출: `checkpoint_style.json`(**E15 키 모양** — 현지화 E16 이 읽는다) · 2위↓ `_{n}`.

## 4. 최종·검증 — 11:render · 11:validate

```python
finalize.render_final(video_path=…, plan=…, style_doc=…, segments=…, resources=…,
                      story_doc=…, output_dir=…, out_name=…)
```
- `out_name` 은 **`shorts.mp4`**(1위) · **`shorts_{n}.mp4`**(2위↓). v3 기본값
  `final_1080x1920.mp4` 를 쓰면 **현지화 `RENDER_OUTPUT` 과 어긋난다**(기획서 §6).
- 최종은 1080×1920 **30fps**(O9).

`finalize.run_validate(plan, grid, stage1_doc, stage2_doc, segments, resources, final_path, …)`
— ⚠ v4 에는 `stage1_doc`·`stage2_doc` 이 없다. **어댑터를 쓴다**:

```python
def stage_docs_for_validate(candidates_doc: dict, span_index: dict, grid: dict
                            ) -> tuple[dict, dict]:
    """v4 산출 → v3 `run_validate` 가 읽는 두 문서. 순수.

    소비처를 **코드로 확인**한 결과 쓰이는 것은 둘뿐이다:
      · `check_exception_overlap(timeline, stage1_doc)` — `stage1_doc["exception_sector"]`
        의 `{key: {"start": "MM:SS.mmm", "end": …}}`. ⚠ **시각이 문자열**이다
        (`schemas.parse_ts` 로 읽는다) — v4 는 `start_sec` 실수라 변환해야 한다.
      · `check_tts_conflicts(resources, plan, stage2_doc, grid)` — 내부에서
        `story.build_span_index(stage2_doc, grid)` 를 부른다. 그래서 v4 의 span_index 를
        **그 함수가 같은 결과를 내도록** 되싣는다: 시퀀스 1 · 청크 1 · meaning 1 에
        모든 span 을 담는다(`{"span_id", "time":{"start","end"}, "is_audio",
        "importance", "audio_script", "text_source", "heard_text", "conf", "scene_script"}`).

    ⚠ 되싣기가 맞는지는 **왕복 테스트로 고정한다** — `build_span_index(stage2_doc, grid)`
    가 v4 의 span_index 와 (소비되는 열쇠에 한해) 같아야 한다. 안 그러면 TTS 충돌 벨트가
    조용히 다른 것을 잰다."""
```

산출: `validation.json` · 2위↓ `validation_{n}.json`.
`hard_fail` 이면 **그 편만** 실패로 기록하고 다음 편으로 간다(전량 실패면 크게).

## 5. 알려진 구멍을 보이게 (M7 잔여)

`tests/test_v4_render_gaps.py` — v3 렌더 경로에 **없는 것**을 이름으로 못박는다.
조사 기록: "v3 판은 E10 마진·이미지 오버레이·제목 창·효과음 전달이 빠져 있다".
- v1 `pipeline` 의 렌더 호출부가 `RenderInputs` 에 넘기는 키 집합과, v3 `render_final` 이
  넘기는 키 집합을 **비교해 차집합을 테스트가 출력**한다. 줄어들면(= 메우면) 테스트가
  그 사실을 알리도록 한다.
- 목적은 통과/실패가 아니라 **차이가 사람 눈에 보이게** 하는 것이다. 지금 통과시키되
  차집합을 메시지에 싣고, `UNVERIFIED.md` 에 항목으로 올린다.

## 6. 배선

- 승인 편마다 `11:resources → … → 11:validate` 를 돈다. **편별 증분** — 한 편이 실패해도
  나머지 산출은 남는다.
- 지문: resources = `[story_doc, span_ids, 정정 사전]` · draft = `[timeline]` ·
  style = `sha1(timeline + label_plan + 프리셋)` · render = `[style 지문, 확정 style_doc]`.
  ⚠ **편집실 라운드에서 스타일을 재호출하지 않는다**(E15) — 지문에 사람이 편집한 필드를
  넣지 마라.
- `--stop-after` 로 각 하위 단계에서 멈춘다.
- 전량 실패면 크게(조용한 결번 금지).
