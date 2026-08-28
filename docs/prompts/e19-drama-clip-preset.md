# E19 — 드라마 클립 프리셋 갭 묶음 (발주서)

**상태: E19-1~5 구현 완료(2026-08-28) · 나머지(E19-6~8) 기획.** 구현된 항목의
계약 정본은 이제 코드다: ai-video `CLAUDE.md`(채널 톤 프로파일 절 + E19-2~5 하위 절) ·
`app/modules/style_tone.py` · `renderer.strip_title_markup`/`extract_title_highlights` ·
`pipeline.snap_cues_to_dialogue_gaps` · `style_compose.avoid_faces_for_texts`/
`load_sfx_manifest` · `reframe.CropKeyframe(face_*)` · `renderer.sfx_audio` ·
`tests/test_e19_*.py`. ⚠ SFX 는 번들이 비어 있으면 닫힌 채로다 — 라이선스 확인된
효과음 파일을 `app/assets/sfx/` 에 manifest 와 함께 넣어야 실전에서 열린다.
⚠ E19-2 의 **AI 마크업 생성**(§2 두 경로 중 AI 쪽)은 구현하지 않았다 — story 산출
제목의 마크업이 edit_plan·발행 메타(대시보드·유튜브 제목)로 그대로 흘러가는 표면의
세척이 선행돼야 한다. 사람 경로(편집실·채널 제목 문자열)만 개통. 후속 별건.
⚠ 발주서 정본 위치는 ves-orchestrator `docs/prompts/` 인데,
그 저장소 메인 체크아웃이 지금 다른 세션의 브랜치에 올라가 있어 **임시로 이 레포에
둔다** — ves 체크아웃이 정리되면 그쪽으로 옮기고 여기는 포인터만 남긴다.

근거 자료(전부 2026-08-28 실측):
- 벤치마크 분석 보고서 아티팩트 — 신병4(일병, 51.9s)·쿠팡플레이(꿀벌무비, 46s)
  0.3s 프레임·오디오·전사 정밀 판독
- [`docs/design_presets/drama_clip_kr.preset.json`](../design_presets/drama_clip_kr.preset.json) —
  이 발주서가 열어야 하는 옵션이 `status: "prompt"` / `"e19"` 로 전부 표시돼 있다
- 소재 대조: 신병4_EP1_EPK.mp4(33:14) — 쇼츠 내레이션 3줄이 EPK 에 없음을 확인
  (= 내레이션은 합성해 얹는 레이어라는 증거)

사용자 결정(2026-08-28):
- 상황 라벨·효과 텍스트는 **얼굴 위/아래, 영상에 방해되지 않는 자리**에 놓는다.
- 효과음(「쾅」류)을 쓴다 — 청취로 확인된 벤치마크 문법.
- TTS 와 원음 오디오가 이어지며 **호흡이 끊기지 않아야** 한다.
- 이 전부를 **프리셋 = 옵션값 파일**로 채널마다 적용할 수 있어야 한다.

## 0. 요약 — 무엇을 만드나

| # | 항목 | 크기 | 자리 |
|---|------|------|------|
| E19-1 | 스타일 **톤 프로파일** `--style-tone <name>` — prompt 노브 묶음의 운반체 | 중 | cli · story · style_compose |
| E19-2 | 제목 **단어 단위** 색 강조 (`{{...}}` 인라인 마크업) | 소 | renderer(제목 drawtext) |
| E19-3 | 내레이션 **cue–대사 겹침 검사기** (gap 스냅) | 소 | pipeline(앵커 해석 뒤) |
| E19-4 | 라벨 배치 고도화 — 얼굴 회피·컷 경계 재배치·괄호형 글로우 | 중 | style_compose · edit_overrides 배치 |
| E19-5 | **SFX 레이어** — manifest·id-only·편당 캡·amix 입력 | 중 | style_compose · renderer |
| E19-6 | 무음 컷 **잔여 정적 하한** (연출 정적 보존) | 소 | silence_cut |
| E19-7 | 자막 **욕설 마스킹** (음성 원음 유지, 자막만 `XX`) | 소 | subtitle 후처리 |
| E19-8 | 렌더 후 **QA 지표** 기록 (LUFS·무음 개수) | 소 | pipeline(render 뒤) · run_log |

범위 밖(별건): 화자별 대사 자막 색(E11 의 `diarize=false` 계약 변경 동반 — 2단계),
CC(SRT) 업로드·배우 해시태그(ves publish 몫), BGM 레이어(권리 문제).

## 1. E19-1 스타일 톤 프로파일 — 이 발주의 척추

prompt 노브가 10개를 넘는다(내레이션 톤·캡·relay, 라벨 어휘·색·밀도·동기, 러닝 개그,
페이오프 클린, 엔딩 컷, 제목 밈 톤). **각각을 design 키로 열면 어댑터 미러+구 엔진
argparse 롤아웃을 10번 반복한다.** 대신 한 키로 묶는다:

```
--style-tone drama_clip_kr        # 채널 design 키 style_tone 이 그대로 나른다
→ app/data/style_tones/drama_clip_kr.json   # 엔진 번들 — 어휘는 엔진이 든다
```

- 프로파일 파일 내용 = preset 의 `status:"prompt"` 항목 전부:
  `narration`(tone·max_cues·cue_len_chars·relay_rule) ·
  `labels`(categories·color_map·density_max·duration_sec·sync_to_speech·
  fill_gaps·payoff_clean·running_gag) · `story`(structure·title_tone·ending·
  payoff_longtake). 값 정본은 `drama_clip_kr.preset.json` — 옮겨 적을 때 갈리면
  가드가 잡게 **테스트가 두 파일을 대조한다**.
- 주입 지점은 둘: story 계열 프롬프트(내레이션 cue 규칙·엔딩·구조·제목 톤)와
  `STYLE_COMPOSITION_PROMPT`(라벨 어휘·밀도·동기). **프롬프트 문구는 톤 미지정이면
  한 글자도 안 바뀐다**(E13 의 프롬프트 동결 규율 — 문구가 바뀌면 전 채널 산출이
  흔들린다). 톤 지정 시에만 추가 절을 덧붙인다.
- `density_max` 는 E15 하드캡(텍스트 8)을 **프로파일 값으로 대체**한다(상한 20).
  미지정 = 8 그대로.
- 없는 프로파일 이름은 **즉시 실패**(argparse choices 가 아니라 파일 존재 검사 —
  프로파일은 늘어나는 어휘다). 조용히 기본 톤으로 떨어지면 '프리셋 적용했는데 왜
  그대로지'가 된다(E11 transcribe-backend 와 같은 규율).
- run_log `provenance` 에 `style_tone` 이름 + 프로파일 sha 를 남긴다 — 산출물이
  어느 톤으로 나갔는지 재추적.
- ⚠ 신 CLI 플래그다 — 엔진 전 노드 배포 뒤에만 어댑터 `CHANNEL_DESIGN_FLAGS` 에
  `style_tone` 미러(style_compose 와 같은 롤아웃, ops 게이트는 channel_style 재사용).

## 2. E19-2 제목 단어 단위 색 강조

벤치마크: "가장 **X같은** 질문" — 2줄 안에서 훅 단어만 노랑. 지금은 줄 단위 색뿐.

- 문법: 제목 문자열 안 `{{강조}}` → 그 어절만 `title_highlight_color`(신규 design 키,
  기본 `#FFE24A`)로. 마크업 없으면 **종전과 바이트 동일**(회귀 0).
- 해석 자리는 제목 drawtext 조립 한 곳 — 줄별 색·박스·bold 와 직교해야 한다
  (2줄 각각에 강조가 있을 수 있다).
- 마크업 **생성**은 두 경로: 사람(채널 title 템플릿·편집실)과 AI(story 제목 생성 —
  톤 프로파일 `title_tone` 이 켠 채널만). ⚠ AI 생성은 톤 게이트 뒤에만 —
  톤 없는 채널의 story 산출에 마크업이 섞이면 구 엔진 노드가 문자 그대로 그린다.
- 렌더가 못 알아보는 마크업 잔여물(`{{` 홀짝 불일치)은 **떼고 그리되 건별 로그**
  (제목이 깨진 채 발행되는 것이 최악).

## 3. E19-3 내레이션 cue–대사 겹침 검사기

벤치마크의 "끊김 없는 호흡"의 반은 **내레이션이 대사와 절대 겹치지 않는 것**이다
(릴레이: 「어디다 버리지?」→「누구나 갈 수 있는 곳」). 지금은 앵커가 어긋나면 TTS 가
원음 대사 위에 그대로 겹친다.

- 자리: `_resolve_cue_anchors` 로 cue 창이 확정된 **직후, resources(합성) 앞** —
  비싼 합성 전에 고친다(E17 자격증명 교훈: 비싼 단계 앞에서 검사).
- 판정: cue 창 vs `subtitle_segments`(대사 시간). 겹침이 `CUE_OVERLAP_TOLERANCE`
  (0.2s) 초과면 **가장 가까운 대사 gap 으로 스냅**(cue 길이가 gap 에 들어갈 때만).
  들어갈 gap 이 없으면 **옮기지 않고 건별 경고**(`[cue-overlap]`) — 멀쩡한 내레이션을
  지우거나 엉뚱한 자리로 보내는 것이 겹침보다 나쁘다(영상 밖 cue 안전망의 규율).
- 게이트: 톤 프로파일 `narration.placement == "dialogue_gaps_only"` 일 때만.
  미지정 채널은 검사 자체가 없다(회귀 0).
- 스냅 결과는 run_log style/story 단계에 `{cue_snapped, of, warned}` 로 남긴다.

## 4. E19-4 라벨 배치 고도화 — 얼굴 회피

사용자 지시: "사람 얼굴 위나 아래, 영상에 방해되지 않는 부분". 실측: 라벨은 인물
얼굴 **옆 빈 공간**에 붙고, 컷이 바뀌면 새 구도에 맞춰 재배치된다(「난가???」 3위치).
지금 E15 는 E18-2 밴드 클램프(제목·자막 회피)뿐 — 얼굴은 모른다.

- **얼굴 좌표는 새로 만들지 않는다** — 리프레임(face_tracking)이 이미 클립별 얼굴
  검출을 한다. 그 크롭 타임라인의 얼굴 박스를 스타일 단계에 노출해 재사용한다
  (검출 비용 0 — E18-4 의 "L2 position 재사용" 과 같은 규율).
  ⚠ `face_tracking:false` 채널이나 검출 실패 클립은 **얼굴 회피 없이 종전 배치** —
  안전장치가 연출을 막으면 안 된다(E17-2 실패 규율).
- 배치 규칙: 라벨 텍스트 박스가 (a) 얼굴 박스와 비겹침, (b) E18-2 밴드 클램프 안,
  (c) 얼굴 **위 → 아래 → 옆** 순서로 첫 성립 후보. 성립 후보가 없으면 클램프만
  적용하고 건별 기록(드롭 금지 — E18-2 와 동일하게 "살리고 당긴다").
- **컷 경계 재배치**: 라벨 창이 클립 경계를 넘으면 창을 클립 경계에서 쪼개
  각 조각을 그 클립의 얼굴 기준으로 다시 배치한다(v3 texts 는 조각당 한 항목으로
  표현 가능 — 렌더 계약 변경 없음).
- **괄호형 글로우**: v3 `texts[].fx` 에 `glow` 추가(ASS blur+색 외곽선 조합).
  fx 어휘 확장이라 렌더러 한 곳 + 검증기 한 줄. 미사용 시 종전 동일.

## 5. E19-5 SFX 레이어

벤치마크: 편당 1~3건, 큰 비트에만(분노 스팅·액션 폴리·「쾅」). 라벨 등장에는 소리를
붙이지 않는다. 지금 엔진은 화면(텍스트·스티커)만 얹고 소리는 못 얹는다.

- **스티커 규율 그대로**(E15): `assets/sfx/manifest.json` 의 **id 만** AI 에 노출,
  **빈 목록이 정상 상태**(라이선스 확인된 파일만 번들), 없는 id 는 그 항목만
  드롭+로그.
- 플랜 어휘: `sfx: [{id, source_time_sec, gain_db}]` — 앵커 좌표계는 tts cue 와
  같은 원본 절대초(변환도 같은 함수. 수식 복제 금지 — E13 교훈).
- 하드캡 `max_per_episode`(톤 프로파일, 기본 3). 넘으면 앞에서부터 자르고 기록.
- 렌더: `_build_input_args`/`_build_audio_filter` 에 cue 와 같은 방식으로 입력 추가
  (adelay + amix). ⚠ cue 입력 인덱스 규율(E18 렌더 절): **같은 목록을 두 곳이 봐야**
  인덱스가 안 어긋난다 — sfx 도 같은 지점에서 한 번만 거른다.
  `gain_db` 기본 −6(대사 아래). 영상 밖 시작 sfx 는 cue 와 같은 안전망으로 드롭.
- 게이트: style_compose + 톤 프로파일에 sfx 절이 있을 때만. 미지정 = 입력도 필터도
  종전과 동일(회귀 0 — 필터그래프 문자열 대조 가드).
- 검수: `checkpoint_style.json` 에 플랜과 함께 저장(재렌더 재적용 — E15 재개 계약),
  run_log style 단계에 `{sfx_used, dropped}`.

## 6. E19-6 무음 컷 잔여 정적 하한

벤치마크는 무음이 0~2건이지만 **반전 직후 0.3~0.5s 정적은 남긴다**(20.95s 의 0.53s —
편 최대 호흡이자 웃음 비트). aggressive 프로파일이 이런 정적까지 밀면 호흡이 죽는다.

- `SILENCE_CUT_PROFILE=aggressive` 에 **잔여 정적 하한** 파라미터를 더한다:
  무음 구간을 잘라낼 때 `min_residual_pause_sec`(기본 = 현행 동작과 동일한 값으로
  캘리브레이션 — **기본값에서 산출 불변이 회귀 0 조건**)만큼은 남긴다.
- conservative 프로파일은 손대지 않는다.
- A/B 근거: 실측 45편 대조 도구(E14 방식 — 저장된 타임라인에 파라미터만 다시 태워
  수정 전후 대조)를 함께 만든다.

## 7. E19-7 자막 욕설 마스킹

벤치마크: 음성은 원음(「새끼야」), 자막만 「XX끼」. 제목의 "X같은" 과 같은 규율.

- 사전은 코드가 아니라 `app/data/profanity_mask_ko.json` 한 곳(E13
  `transcribe_normalize_ko.json` 과 같은 규율). 규칙도 같다: **공백 토큰 완전 일치**
  — 부분 문자열 치환 금지(멀쩡한 단어를 깨뜨린다).
- 적용 자리: cue 조립이 끝난 **자막 텍스트에만** — 전사 원문·TTS 합성 입력은 그대로
  (소리는 원음 유지가 계약이다).
- 게이트: design 키 `subtitle_profanity_mask`(`off` 기본 = 종전 그대로). 바꾼 내역
  건별 stdout·run_log.

## 8. E19-8 렌더 후 QA 지표

렌더를 바꾸지 않는다 — **재기만 한다**. 벤치마크 대비 산출물이 어디쯤인지 편마다
남아야 프리셋 튜닝이 데이터로 돈다.

- render 뒤 ffmpeg 두 번: `ebur128`(통합 LUFS·LRA) + `silencedetect`(-30dB/0.3s
  구간 수). run_log `steps[{step:"render"}].audio_qa` 에 기록.
- 실패해도(측정 불가) 편을 막지 않는다 — `audio_qa_error` 로 사유만.
- 목표값(예: −13.5 LUFS · 무음 ≤2)은 엔진이 판정하지 않는다 — ves evaluate 가
  읽고 대시보드에 띄우는 것은 별건.

## 9. 회귀 0 총칙

E11·E13·E15 와 같은 게이트 규율이다. 이 발주 전체에서:

- `--style-tone` 미지정 + 마크업 없음 + sfx 절 없음 + mask off 면 **프롬프트 문자열·
  필터그래프·subtitle_segments.json·cue 전부 종전과 동일**해야 한다. 가드는 문자열
  대조로 고정한다(E15 회귀 0 실측과 같은 방식).
- 새 CLI 플래그는 `--style-tone` 하나뿐이어야 한다(나머지는 전부 프로파일·마크업·
  design 키 재사용) — 롤아웃 표면적 최소화.
- 모델 정책: LLM 호출 추가 없음. 기존 compose_style(Flash)·story(Flash) 프롬프트
  확장만. Pro 는 여전히 analyze_chunk 하나다.

## 10. 검증

1. 회귀 가드 테스트: `tests/test_e19_style_tone.py`(프로파일 로드·프롬프트 불변·
   preset↔프로파일 값 대조), `test_e19_title_highlight.py`, `test_e19_cue_overlap.py`,
   `test_e19_label_face_avoid.py`, `test_e19_sfx_layer.py`(필터그래프 회귀 0 포함),
   `test_e19_silence_residual.py`, `test_e19_profanity_mask.py`, `test_e19_audio_qa.py`.
2. 실렌더 A/B: 합성 소스 + 손 플랜(F-408/E15 방식) 1회 + **신병4 EPK 실소재 1편**
   — 톤 on/off 를 같은 입력으로 렌더해 프레임·오디오 대조. EPK 파일은 사용자
   드라이브에 있다(신병4_EP1_EPK.mp4).
3. 벤치마크 수치 대조: 완성본을 이 발주의 근거 실측과 같은 도구(0.3s 타일·
   silencedetect·ebur128)로 재서 §0 표의 각 항목이 실제로 벤치마크 문법에
   가까워졌는지 본다 — QA 지표(E19-8)가 그 자를 상시화한다.

## 11. 롤아웃 순서

1. 엔진 구현 + 테스트 → main 머지(맥미니 6대 auto_update).
2. 전 노드 갱신 확인 후 ves 어댑터에 `style_tone` 키 미러(+ 대시보드 v_allowed).
3. 파일럿 채널 1개에 `drama_clip_kr.channel_design.json` 병합 + `style_tone` 지정.
4. 실렌더 검수(픽셀·청취) → size·색 초기값 확정 → preset 파일 갱신(버전 올림).
