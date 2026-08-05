# TTS cue 를 절대시간에서 **클립 앵커**로 바꾸기 (설계·작업 지시서)

작성 2026-08-04 (맥5, macmini-luna5) · 대상 레포 **ai-video** · 작업 예상 반나절
관련 실측 산출물: `outputs/scene_loop/커리어데이 숏츠/ep01/` (아래 §7 재현 절차)

> ✅ **구현 완료 (2026-08-04, 맥5).** §4~5 에서 두 가지를 일반화해 구현했다:
> ① §4-3 의 "clip_index 누적 길이" 공식 대신 **원본시간 → 편집시간 조각 매핑**
> (`_resolve_cue_anchors`) — 정규화가 앵커를 `source_time_sec`(원본 절대시간)로 변환해 두면,
> 해석이 최종 클립의 원본 구간으로 위치를 찾는다. 이유: silence_cut 의 클립 **분할**
> (`flatten_to_clips`)과 story 단계 beat trim 때문에 story 클립 배열과 최종 클립 배열이
> 1:1 이 아니고, 첫 클립 *앞 확장* 시 클립-상대 offset 은 여전히 내용과 어긋나기 때문.
> ② 해석 위치는 §5-3 의 "resources 직전"이 아니라 **[tts cues] 블록** —
> 멀티쇼츠 렌더 루프가 `tts_cues_per_variant` 를 직접 소비하므로 variant 전체를 한 곳에서 해석.
> `_shift_cues_by_silence_cut`·`_clamp_cues_to_variants` 삭제. 테스트: ai-video 249 passed
> (`tests/test_cue_anchor_resolve.py` 신규 — §8 의 7.28s 회귀 포함).
> §7 실측 재현: hook 이 −6.98s 확장 + build 클립이 길이 보정으로 통째 제거된 storyline 에서
> cue1 = 7.47s(= 원본 116.2 − 확장된 hook 시작 108.72), 앵커 클립이 제거된 cue 는 드롭 로그,
> ASS·adelay·덕킹 전부 일치 확인 (`outputs/verify_cue_anchor/커리어데이_50/`).

---

## 1. 한 줄 요약

LLM 이 TTS cue 를 **편집 타임라인 절대시간**으로 적는데, 그 뒤 파이프라인이 클립 경계를
세 번 더 바꾼다. 보정은 그중 한 가지(무음 컷)만 반영하므로 cue 가 화면과 어긋난다.
**cue 를 `(클립 인덱스, 클립 내부 오프셋)` 으로 표현하고, 타임라인이 확정된 뒤 한 번에
절대시간으로 변환**하도록 바꿔 드리프트를 구조적으로 제거한다.

---

## 2. 증상 (실측)

커리어데이 숏츠 EP1, run `커리어데이_50` (2026-08-04 생성):

- 10.0초 — 내레이션 "이제는 핵심 인재를 빌려 쓰는 시대."
- 그 시점 화면 — 화자는 아직 **앞 이야기**(갑을 관계)를 하는 중
- 16.4초 — 화자가 그제서야 "요즘 얘기하는 borrow 개념은 …" 이라며 **내레이션이 이미 말한 내용**을 말함

즉 내레이션이 원본 화자보다 먼저 결론을 말해버려 완성도가 떨어진다.
같은 드리프트가 cue2 에도 있다. **오차는 세 cue 모두 정확히 7.28초로 일정하다.**

---

## 3. 근본 원인

### 3-1. cue 가 작성된 타임라인 ≠ 렌더된 타임라인

| 클립 | story 단계 (cue 작성 시점) | 최종 `edit_plan.json` | 차이 |
|---|---|---|---|
| hook | 173.2 – 182.3 (9.1초) | **165.92** – 182.3 (16.38초) | 앞으로 **+7.28초** |
| build | 274.8 – 287.1 (12.3초) | 274.8 – 286.8 (12.0초) | −0.3초 |
| payoff | 298.5 – 319.5 (21.0초) | 298.5 – 319.82 (21.32초) | +0.32초 |

LLM 이 적은 cue: `0.5–5.0`, `10.0–15.0`, `22.0–27.5`
최종 렌더에 그대로 사용됨 (ASS 자막 · `adelay` · 덕킹 구간 전부 동일).

cue1 `10.0` 은 story 타임라인에서 build 시작(9.1초) 직후를 노린 값이다.
그런데 실제 build 는 **16.38초**에 시작한다 → 7.28초 일찍 터진다.
cue2 `22.0` 도 payoff 시작(story 21.4초 → 실제 28.38초) 대비 같은 7.28초.

### 3-2. 누가 경계를 바꿨나

`app/pipeline.py` 의 transcript 확보 직후 블록(현재 2637~2650 부근):

```python
all_storyline_variants = _snap_clip_boundaries_to_dialogue(...)   # 대사 경계로 스냅 (±5초)
all_storyline_variants = _extend_storyline_for_narrative(...)      # ← 첫 clip 시작을 뒤로 확장 (max 8초)
all_storyline_variants = _fill_intra_storyline_gaps(...)           # clip 간 간극 메우기 (max 3초)
```

이번 건은 `_extend_storyline_for_narrative` 가 목표 길이를 채우려고
**첫 clip 시작을 7.28초 앞당긴 것**이다(`max_extend_per_side=8.0` 바로 아래 값).
`_autocorrect_storyline_length` 안의 "2-2) 라운드 13 신규 — 첫 clip 시작 확장 (역방향)"
경로도 같은 성질의 변경을 한다.

### 3-3. 왜 보정이 안 됐나

보정 함수가 **한 종류뿐**이다 — `_shift_cues_by_silence_cut` (`app/pipeline.py`, 현재 454행).
이름 그대로 *무음 컷으로 줄어든 분량*만 차감한다. 게다가 두 가지 한계가 더 있다:

1. **clip 내부 cue 는 시프트하지 않는다**(docstring 명시: "clip 시작 비례 가정").
2. snap/extend/fill 로 인한 경계 변화는 **아무도 반영하지 않는다.**

그런데 현재 2718행 부근 주석은 이렇게 단언한다:

> `# tts_plan 단계 제거. cue 는 story 단계에서 storyline.tts_cues 로 미리 생성되고`
> `# silence_cut 단계가 _shift_cues_by_silence_cut 으로 시간 보정까지 완료.`

**이 주석이 사실과 다르다.** 이 문장 뒤에 경계를 또 바꾸는 단계가 세 개 더 있다.
보정 함수를 하나 더 얹는 방식(= 즉효 처방)은 네 번째 단계가 생기면 같은 함정을 반복한다.
그래서 절대시간 자체를 버린다.

---

## 4. 목표 설계

### 4-1. 원칙

> cue 는 **"어느 클립의 몇 초 지점"** 만 말한다. 절대시간은 타임라인이 확정된 뒤
> 파이프라인이 **한 곳에서** 계산한다.

클립을 앞뒤로 늘리든, 무음을 자르든, 간극을 메우든 cue 가 자동으로 따라온다.

### 4-2. 스키마 (LLM 출력)

```jsonc
"tts_cues": [
  {
    "clip_index": 1,        // storyline clips 배열의 0-based 인덱스 (필수)
    "clip_role": "build",   // 검증·가독용. clip_index 와 불일치 시 clip_index 우선
    "offset_sec": 0.9,      // 그 클립 시작으로부터의 초 (필수, >= 0)
    "duration_sec": 4.0,    // cue 길이 (필수, 2~6초 권장)
    "text": "...",
    "voice": "ko_male",
    "speed": "normal",
    "voice_rationale": "...",
    "speed_rationale": "..."
  }
]
```

`start_sec` / `end_sec` 는 **LLM 출력에서 제거**한다. 하류에서 계산해 채운다.

### 4-3. 변환 (신규 함수)

```
absolute_start = Σ(rendered_duration[0..clip_index-1]) + offset_sec
absolute_end   = absolute_start + duration_sec
```

`rendered_duration` = **최종 렌더 기준 클립 길이** = `(clip.end_sec - clip.start_sec) - removed_sec`
(`removed_sec` 은 해당 클립의 `SilenceCutResult.total_removed_sec`).

---

## 5. 변경 사항 (파일별)

### 5-1. `app/modules/gemini_client.py` — 프롬프트

`STORY_COMPOSITION_PROMPT` 의 `## TTS cue 작성` 절(현재 871행~).

- **도입 문단 교체**: "cue 는 *편집 타임라인 절대 시간* 기준" → "cue 는 **클립 앵커** 기준".
  "후처리에서 silence_cut 후 재보정된다" 문장 삭제(더 이상 사실이 아니고, 이 문장이
  LLM 에게 "대충 적어도 보정된다"는 잘못된 안심을 준다).
- **작성 규칙 2번 교체**: `start_sec/end_sec` 설명 → `clip_index` / `offset_sec` / `duration_sec` 설명.
  "clips 배열은 hook → build… → payoff 순서이며 `clip_index` 는 그 배열의 0-based 인덱스"를 명시.
- **작성 규칙 3번(겹침 금지)**: 절대시간이 없으므로 "같은 clip_index 안에서 offset 이 겹치지 않게"로 수정.
  서로 다른 클립 간 겹침은 변환 후 정규화가 처리한다.
- **작성 규칙 4번 강화**: "cue 텍스트가 클립의 핵심 transcript 와 충돌하지 않도록"에
  ★**"그 클립에서 화자가 그 내용을 말하기 *전에* cue 가 먼저 말하지 않게 한다.
  요약·선언은 화자가 말한 뒤(offset 을 뒤로) 배치한다."** 를 추가.
  이번 사고의 체감 원인은 타이밍 오차 + 이 규칙의 부재가 겹친 것이다.
- **예시 JSON 3곳 수정**: 현재 1009 / 1040 / 1061행 부근의 `"tts_cues": [...]` 예시를 새 스키마로.
  (프롬프트 예시가 옛 스키마로 남으면 LLM 이 그걸 따라간다 — 반드시 전부 고칠 것)
- 프롬프트를 고치면 `prompt_set_hash` 가 바뀐다 → provenance 상 정상적인 변화다.
  `docs/prompt_backups/` 관례가 있으면 기존 판을 백업해 둘 것.

### 5-2. `app/modules/gemini_client.py` — `_normalize_storyline_tts_cues`

현재 1235행. **역할을 둘로 나눈다.**

- 이 함수는 이제 **앵커 검증**만 한다:
  - 필수 필드: `clip_index`(int ≥ 0), `offset_sec`(float ≥ 0), `duration_sec`(float > 0), `text`
  - voice/speed 라벨 검증 + fallback (기존 로직 유지)
  - `max_cues` 절단, majority voice 통일 (기존 로직 유지)
  - `clip_index >= len(clips)` 인 cue 는 **드롭**(LLM 환각 방지) → `clips_count` 인자 추가
  - 정렬은 `(clip_index, offset_sec)` 기준
  - ⚠️ 기존의 `total_duration` 클램프는 **제거**한다. 지금 호출부가 넘기는 값은
    *확장 전* 클립 합계(`_sl_total_dur`)라, 확장 후 타임라인 기준으로는 애초에 틀린 기준이었다.
    범위 검증은 변환 시점(5-3)에 클립별로 한다.
- **하위호환**: `clip_index` 가 없고 `start_sec`/`end_sec` 만 있는 응답(구 프롬프트 캐시·
  기존 `checkpoint_story.json` 재개)이면, 절대시간을 클립 경계로 역산해 앵커로 변환하는
  폴백을 둔다. 변환 불가면 드롭하고 `[TTS cue] 구 스키마 cue N개 — 앵커 역산 실패, 드롭` 로그.

### 5-3. `app/pipeline.py` — 신규 `_resolve_cue_anchors`

`_shift_cues_by_silence_cut` 을 **삭제**하고 그 자리에 넣는다.

```python
def _resolve_cue_anchors(cues, clips, silence_cut_results=None) -> list[dict]:
    """(clip_index, offset_sec, duration_sec) → 편집 타임라인 절대 start_sec/end_sec.

    clips: 최종(snap/extend/fill 적용 후) StoryClip 리스트
    silence_cut_results: 있으면 클립별 total_removed_sec 를 길이에서 차감
    """
```

동작:
1. 클립별 렌더 길이 `dur[i]` 계산 → 누적 시작점 `base[i] = Σ dur[0..i-1]`
2. `start = base[i] + min(offset_sec, max(0, dur[i] - MIN_CUE_TAIL))`
   — 클립이 짧아져 offset 이 클립 밖으로 나가면 클립 안으로 당긴다(`MIN_CUE_TAIL = 0.5` 제안)
3. `end = start + duration_sec`, 영상 전체 길이로 클램프
4. 변환 후 **cue 간 겹침 제거**(기존 정규화의 겹침 로직을 여기로 이동) + 시간순 정렬
5. 각 cue 에 `start_sec`/`end_sec` 를 채워 반환 (앵커 필드는 디버깅용으로 보존)

**호출 위치가 이 작업의 핵심이다.** 반드시 다음 셋 **모두**보다 뒤여야 한다:
`_snap_clip_boundaries_to_dialogue` → `_extend_storyline_for_narrative` → `_fill_intra_storyline_gaps`
그리고 무음 컷 결과가 확정된 뒤여야 한다.
현재 구조에서는 **resources 단계에서 TTS 합성 직전**(현재 2850행 `synthesize_tts_with_fit` 호출 루프 바로 앞)이
가장 안전하다 — 그 시점엔 클립도 무음 컷도 전부 확정돼 있다.

기존 `_shift_cues_by_silence_cut` 호출부(현재 2534행 부근)는 제거한다.
2718행 부근의 "silence_cut 단계가 … 시간 보정까지 완료" 주석도 **새 사실로 교체**할 것.

### 5-4. 하류 소비자 — 변경 불필요 (확인만)

`_resolve_cue_anchors` 가 `start_sec`/`end_sec` 를 채워 주므로 아래는 그대로 동작한다.
다만 앵커 해석이 **이들보다 먼저** 실행되는지 배선을 확인할 것:

| 소비자 | 위치 | 쓰는 값 |
|---|---|---|
| TTS 합성 | `pipeline.py` `synthesize_tts_with_fit` 루프 | `target_sec = end - start` |
| 원본 오디오 덕킹 | `app/modules/renderer.py` (현재 1148행~) | `cue.start_sec ~ cue.end_sec` |
| TTS 오디오 배치 | 같은 곳, `adelay=start*1000` | `start_sec` |
| TTS 자막 | `pipeline.py` (현재 2947행 부근) | `start_sec` + **mp3 실제 길이** |

### 5-5. 체크포인트 호환

- `checkpoint_story.json` 에 저장되는 `storylines[].tts_cues` 가 새 스키마가 된다.
- **기존 잡을 `--from-step render` 등으로 재개하면 옛 스키마 cue 를 만난다** → 5-2 의 폴백이
  받아 준다. 폴백이 없으면 밤 배치 재개가 조용히 무자막(무내레이션)으로 나갈 수 있다.
- `checkpoint_resources.json` 의 `tts_cue_files[].cue` 는 해석 *후* 값이 저장되므로
  기존과 같은 모양(`start_sec`/`end_sec` 포함)이 유지된다 — 렌더 재개 경로는 영향 없음.

---

## 6. 엣지 케이스 체크리스트

- [ ] `clip_index` 가 클립 수보다 큼 → 드롭 (환각)
- [ ] `offset_sec` 이 해당 클립 길이보다 큼 → 클립 안으로 클램프
- [ ] 클립이 무음 컷으로 크게 줄어 cue 가 안 들어감 → 클램프 후에도 `duration` 이 남으면 다음 클립을 침범
      하지 않도록 `end` 만 잘라 낸다(오디오는 잘리지 않으니 **덕킹 구간만 짧아지는 것**에 유의)
- [ ] cue 2개가 같은 클립 같은 지점 → 겹침 제거 후 뒤 cue 가 클립 밖으로 밀리면 드롭
- [ ] `tts_cues` 가 빈 배열 → 지금도 정상 경로(내레이션 없는 쇼츠)
- [ ] variant 가 여러 개일 때 pool 인덱스와 클립 리스트가 어긋나지 않게 (현재 `storyline_tts_cues_pool` 은
      variant 와 1:1 — 해석도 variant 별로 각각)
- [ ] Gemini 폴백 경로(`used_gemini_fallback=True`)는 snap/extend/fill 을 건너뛴다 → 앵커 해석은
      그래도 실행돼야 한다(클립 길이만으로 계산되므로 문제 없음, 테스트로 고정)

---

## 7. 검증 레시피 (이 드리프트를 실제로 재현)

기존 잡을 **story 단계부터** 다시 돌리면 새 프롬프트가 탄다:

```bash
cd ~/ves/ai-video
.venv/bin/python -m app.cli create_shorts --title 커리어데이 \
  --video ~/ves/sources/커리어데이/ep001/source.mp4 --episode 1 --max-shorts 1 \
  --no-subtitles --silence-profile aggressive --length-profile tight --loudness-lufs -14 \
  --outdir outputs/verify_cue_anchor --from-step story --job-id 커리어데이_50
```

기대값 (수정 전 → 수정 후):

| 확인 대상 | 수정 전 (버그) | 수정 후 |
|---|---|---|
| `edit_plan.json` hook 시작 | 165.92 (story 는 173.2) | 동일 (렌더 로직은 안 건드림) |
| cue1 절대 start | **10.0** | **≈17.3** (= build 시작 16.38 + offset) |
| cue2 절대 start | **22.0** | **≈29.0** (= payoff 시작 28.38 + offset) |
| 체감 | 내레이션이 화자보다 7.28초 먼저 말함 | 해당 클립 안에서 발화와 맞물림 |

빠른 확인 명령:

```bash
grep -m3 "^Dialogue" outputs/verify_cue_anchor/커리어데이_50/tts_subtitles.ass
grep -o "adelay=[0-9|]*" outputs/verify_cue_anchor/커리어데이_50/shorts.filter.txt
```

**중요**: 이 검증은 story 단계를 다시 태우므로 Gemini 호출이 발생한다(비용·쿼터).
밤 배치와 같은 `GEMINI_API_KEY` 를 쓰는 시간대는 피할 것 —
짝 머신과 키를 공유하면 배치 생성이 쿼터로 실패한다(`config/assignments.json` 의 `gemini_key`).

---

## 8. 테스트

- `tests/test_pr4_story_tts_cues.py` — 기존 22건이 옛 스키마(`start_sec`/`end_sec`)를 전제한다.
  앵커 스키마로 갱신하고, **하위호환 폴백 테스트**를 추가한다(구 스키마 입력 → 앵커 역산).
- 신규 `tests/test_cue_anchor_resolve.py` (제안):
  1. 확장 없음 → cue 절대시간이 offset 그대로
  2. **첫 클립이 앞으로 7.28초 확장 → 모든 cue 가 +7.28초** ★이번 버그의 회귀 테스트
  3. 무음 컷으로 클립이 줄어듦 → 뒤 클립 cue 가 그만큼 당겨짐
  4. offset 이 클립 길이 초과 → 클램프
  5. `clip_index` 환각 → 드롭
  6. cue 겹침 → 뒤 cue 시프트, 클립 밖이면 드롭
- `_shift_cues_by_silence_cut` 을 지우면 `tests/test_pr5_silence_cut.py` 의 관련 테스트가
  깨질 수 있다 — 새 함수 기준으로 이관할 것.
- 전체: `.venv/bin/python -m pytest tests/ -q` (수정 전 기준 210 passed).
  ⚠️ **맥5 venv 에는 pytest 가 없다.** 작업 머신에서 `pip install -r requirements-dev.txt` 먼저.

---

## 9. 이번 작업 범위 밖 (별건, 같이 하면 체감 큼)

같은 영상에서 함께 발견됐지만 원인이 다르므로 분리한다.

1. **TTS mp3 앞뒤 무음** — 세 파일 모두 앞 0.34~0.39초 · 뒤 1.11~1.23초가 무음.
   3.7초 노출 중 실제 발화는 2.1초(57%)뿐이라 자막이 목소리보다 먼저 뜨고 늦게 사라진다.
   → 합성 후 `silencedetect` 로 트림하거나, 자막 세그먼트를 발화 구간에 맞추면 해결.
2. **화자와 목소리 겹침** — 큐1·2 가 원본 화자 발화와 각각 1.6초·2.2초 겹친다
   (원본은 −6dB 덕킹되지만 한국어 남성 음성 둘이 동시에 난다).
   → 최종 transcript 를 이미 갖고 있으니 cue 를 발화 공백에 스냅하는 후처리가 확실하다.
   앵커 도입 후에는 "offset 을 발화 공백으로 스냅"으로 자연스럽게 얹을 수 있다.

---

## 10. 리스크 · 롤백

- **영향 범위는 전 채널·전 작품이다.** 길이 보정은 공통 경로라 짧은 장면을 뽑은 회차마다
  같은 드리프트가 있었다. 6대 전부 해당한다. 반대로 말하면 회귀 시 피해도 전면적이다.
- 프롬프트 변경은 LLM 응답 형태를 바꾼다 — 첫 실행에서 `tts_cues=0` 이 나오면 정규화가
  전부 드롭한 것이다. `_normalize_storyline_tts_cues` 드롭 사유를 **로그로 남기고** 배포할 것.
- 롤백은 커밋 되돌리기로 충분하다(체크포인트는 5-5 폴백으로 양방향 호환).
- 배포 후 첫 밤 배치는 `results/scene_loop.log` 와 생성물의 `tts_subtitles.ass` 시각을
  눈으로 한 번 확인할 것.
