*기획서 · ai-video · 2026-09-03 · rev.9 (통합판 · 운영자 결정 9건 반영)*
> **상세 부록.** 읽기용 정본은 `v4-plan.md`(정리판)다. 이 문서는 흡수 지도·계약·캐시 표·결정 이력의 근거 판이다.
# v4 통합 파이프라인 기획서 — v4 골격 · v3 흡수 · v5 합류

운영자 결정(2026-09-03): **골격은 v4**(rev.7)를 기본으로 두고, **v3 의 기능**(M1~M15)을 흡수하고,
**v5 기획서가 찾은 함정·정보**를 합친다. 같은 날 운영자가 rev.8 에 **결정 9건(O1~O9, §10)** 을 더했다 —
프록시 720p/30fps · 훑기 fps 계단 4/3/2 · 후보 5~16 · 6단계 제목 가안 · **6c 구간 검증 신설** · 6b ±60s ·
**결함 게이트 통과분 전부 렌더** · 살붙이기 병렬 · 초벌 720p/30fps + 스타일 HIGH.
이 문서는 `v4-pipeline-plan.md`(rev.7)의 후속이며 실측 절(§2 A~H)은 그 문서를 인용한다. `docs/v5-pipeline-plan.md` 는
이 판으로 흡수됐다. 비교 근거: `docs/v4-v5-comparison.md`.

| | |
|---|---|
| **골격** | 11단계(+6b·6c) · 통합 1콜 후보 편성 · 결정적 깔때기 · offset 시각 플래그 · **결함 게이트 통과분 전부 렌더**(rev.7 의 "1편 + top-3 보관"을 O7 이 뒤집었다) · 격자 *단계* 없음 · 프록시 720p/30fps(O1) |
| **rev.7 에서 바뀐 것** | ① 구축 방식: v3 포크 + v1 모놀리스 추출 ② 바깥 계약 표 승계 ③ v3 격자 모듈을 재료로 흡수 ④ 11단계 하위 id 5개 + v3 M12~M15·v1 E-항목 흡수 ⑤ 예산 판정 산식 둘(상한=count · 과금=usage) ⑥ 운영 규약 합류 ⑦ 마일스톤 표 ⑧ 선택 단계 10a ⑨ **O1~O9** |
| **편당 입력(67분 · usage)** | 승인 1편 ≈0.96~1.14M · 승인 k 편이면 **+(k−1)×0.11M**(살붙이기+HIGH 스타일) · 60분 소재는 fps 3 이라 +0.17M · 첫 30편 fps 2 shadow +0.63M |
| **남은 결정** | §10 — 승계 8건 + v5 처분 7건 + O1~O9 확정 + 신규 N1·N2·N4·N5·**N6~N8** |

## §0 세 문서의 자리

| | 어디서 왔나 | 이 판에서 |
|---|---|---|
| 골격·깔때기·offset·실측 A~H·검수 판정 | v4 rev.7 | **그대로**(§1 "v4" 열) — Q1(top-3)만 O7 로 대체 |
| 격자 재료·refine·textcheck·assemble 슬롯·stage4 라벨·finalize 검증·M9~M15 | v3 코드 | **흡수** — 자리는 §4 표 |
| 바깥 계약·v1 진입점 결합·모놀리스 추출·AST 가드·계약 대조·3시간 실측·count 상한·STATIC·SDK 핀·타임아웃·롤백·자막 정정 자리·TTS 라벨 캐시·어절 통일·유튜브 설명·발행 메타·연출 두 벌 통합 | v5 기획서 | **합류** — §1 "v5" 열과 §5~§8 |
| O1~O9 | 운영자(2026-09-03) | **확정** — §10 표에 각 항의 비용·조건을 적었다 |

**v4 골격을 지키는 선.** v5 가 다르게 냈지만 채택하지 않는 넷. 각각 되돌릴 조건을 적어 둔다.
- 10단계 + proxy 4켜 → **11단계 유지**. v5 의 "재개 손잡이는 켜 단위"는 11단계 하위 id 로 받는다.
- LLM 시각 무출력(span id) → **절대초 + 하드 게이트 + 관용 안 스냅 유지**. 대신 **6c 가 전사로 구간을 검증**하고(O5) validate 가 격자 정합률을 기록한다. 승격 조건은 N2.
- 청크 상세 켜(3fps 전량) → **없음 유지**. "④ 주도 지연 상세"는 **10a(승자 근방·선택)** 로만.
- 훑기 fps 1.0 고정 → **계단 4/3/2**(O2). 90분 넘으면 예산 역산 `fps_cap`, fps 2 는 shadow 로 30편 관측.

## §1 11단계 — 무엇을 어디서 가져오나

산출의 **[기존]** 은 이름·모양을 그대로 쓰는 것, **[신설]** 은 새 파일이다. 기존 이름을 다른 모양으로 쓰는 것은 금지 — v3 가 현지화를 깨뜨린 방식이다.

| # | 단계 · 호출 | v4 (rev.7) + O1~O9 | v3 에서 흡수 | v5 에서 합류 | 산출 |
|---|---|---|---|---|---|
| 1 | init · 0 | job 디렉토리·run_log | — | `app/modules/job.py` 추출 · 마커 `pipeline:"v4"` · `provenance.config.app` · `design_cli` · 켜별 deadline·편당 벽시계 상한 | `run_log.json` [기존] |
| 2 | research · 1(+1) | `work_researcher` | — | 인물 <3명이면 보완 검색 · 인명 사전은 3·6c·10·11 이 함께 쓴다 | `checkpoint_research.json` [기존] |
| 3 | 자막 전사 · 0 | 단어 타임스탬프 = 눈금 · **6c 의 정본** | `transcribe_words` · `retranscribe_gaps`(공백 ≥6s) · 인명 프롬프트 | SRT 있으면 전사 없음(명시) · 전사 지문과 격자 지문 분리 · 전사 텍스트 토큰을 countTokens 로 재 둔다(§3 예산선 재료) | `checkpoint_grid_words.json` [기존] |
| 4 | probe + 장면전환·무음 · 0 | ffmpeg 곁다리 · **표본 fps 계단 결정**(§3) | `media_probe` · `scenecut` · `audio` · `timegrid` → `grid.json` 재료 사이드카 | 사전검사를 프록시 인코딩 앞으로 · 하한 미달 fail-loud | `checkpoint_probe.json` · `grid.json` [기존] |
| 5 | proxy 업로드 · 0 | **720p / 30fps 파일**(O1) · Files API 1회 · 핸들 공유(6·6b·8·10a) | `_upload_video` 공개 헬퍼 · `SCAN_PROXY_HEIGHT` 480→720 · `build_scan_proxy` 10→30fps | 폴링 상한 · `media_processing=STATIC` · SDK 핀 · 삭제는 최종 렌더 뒤 · 재개 시 재업로드 + `[cache] ⚠` | `checkpoint_upload.json` [신설] |
| 6 | 후보 편성 · 1(+≤2) | 통합 1콜 · 표본 fps 계단 · **후보 5~16**(O3) `{id, template, segments[{start,end,quote}], reason, title_draft≤20자}`(O4 — 제목 가안만) · `exception_sector` 자가 신고 | `STORY_TEMPLATE_SPECS` 4종을 다양성 축으로(템플릿당 1~4개) · M10-B 재료 신뢰 표기 · 인명 초성 사전 · `character_index` 슬롯 | temperature 0 · `model_version` · implicit cache · usage 전 항목 · 출력 상한 65,536 · 반려 소진 = **편 실패** | `checkpoint_candidates.json` [신설] |
| 6b | exception 경계 프로브 · ≤3~8 | **±60s 창**(O6) · 창 밖이면 이어서 한 창 재프로브(예산 안) · exception 전무 편 tail 180s 의무 · offset fps 6 · verify | `refine.py` 순수 함수화(`boundary_probe_windows` 창 폭 인자화) | 별도 캐시·재개 id `6b` · 실패 = 원판정 유지 · `FLASH_BUDGET` 감사 | `candidates.exception_sectors` |
| **6c** | **구간 검증 · 0** [신설 · O5] | 후보 조각마다 **전사로 시각 환각을 검사**: ① 소스범위(`bounds_problem`) ② 인용 대사 실재(`find_quote_times` — 정규화 편집거리 ≤0.35, 그 구간 안이면 ok · **다른 시각에만 있으면 그 시각으로 재배치** · 어디에도 없으면 환각 → 조각 드롭) ③ 발화 커버리지(`plausible_speech_intervals` — 대사 없는 조각에 quote 가 있으면 환각) ④ 경계 눈금 관용(±2.0s 안에 눈금 없으면 경고) · 조각 드롭 후 길이 하한(40s) 미달이면 후보 드롭 · 전량 드롭이면 6단계 재질의 사유로 회송(≤2 예산 안) | v1 `timestamp_check`(quotes_of·candidate_problem·filter_candidates — 933 방어) · v3 `chunk_analyze` 각색 임계 0.35 · `schemas.SNAP_TOLERANCE_SEC` | v5 §2 "시간 격자" 문제의식을 게이트로 — 순수 함수 · 판정 전량 기록 | `candidates.verify` |
| 7 | 결정적 깔때기 · 0 | 6c 통과분 → IoU 0.5·발화커버리지·길이·결속·정지화면·다양성 → **≤8** · exception 꼬리 하드 | 눈금 어휘(관용 안 소프트 스냅) · `score_story` 루브릭 6항 · `check_repetition` | 순수 함수 · 같은 입력 = 같은 순위 | `candidates.funnel` |
| 8 | 시각 사고 플래그 · ≤8 병렬 | offset 멀티파트 fps 5 · 이진 플래그만(seam_jump · hook_weak) | — | 동시 4→8 · Lock 카운터 · 증분 저장 · 미채점≠0점 3갈래 | `candidates.flags` |
| 9 | 승인 + 순위 · 0 | **결함 게이트 통과분 전부 승인**(O7): 7단계 하드 통과 ∧ 6c 통과 ∧ seam_jump=false ∧ hook_weak=false ∧ 채점됨. 순위(소프트 신호)는 **파일 번호·발행 순서**로만 쓴다. 0편이면 1위를 경고와 함께(`candidate_rank_fallback`) · 상한 `--max-shorts`(v4 진입점 1~8, 기본 8 — v1 CLI 의 1~3 클램프 해제) | `fallback_highlight` 폴백 재료 | 동점 = 소스 시각순 · `steps[candidate_rank]` 감사(승인·탈락 사유 전량) | `candidates.rank` · `approved[]` |
| 10 | 살붙이기 · **k 콜 병렬**(+설명 k) | **승인 k 편 각각**(O8) 제목 두 줄·내레이션·라벨·효과 문구 · `title_draft` 를 씨앗으로 | M11 어휘 · 제목 20자(E21 `title_fixed`) · `--hook-variants`(선택) · 톤 프로파일 덧붙임 | 설명·해시태그 별도 호출 · `--candidate-id` 단독 재진입 · 8단계와 같은 병렬 규약 | `checkpoint_story.json` [기존 — `variants[k]` v1 모양] |
| 10a | 승자 근방 정밀 청취 · ≤2/편 **(선택 · 기본 꺼짐)** | 승인 편마다 창 합집합(≤180s)·3fps·offset | `chunk_analyze` 축소 → `heard` → M9-C 전사 판정 | 게이트 `--winner-detail` · A/B 는 V4-M10 | `checkpoint_winner_detail.json` [신설] |
| 11 | 후반부 × k (하위 id 5) | 편집재료 → **초벌 720p/30fps** → 스타일 **HIGH** → 최종 1080p → 검증 — **승인 편마다** | §1-1 | §1-1 | 1위 `edit_plan.json`·`checkpoint_style.json`·`shorts.mp4` [기존] · 2위↓ `edit_plan_{n}.json`·`checkpoint_style_{n}.json`·`shorts_{n}.mp4`(v1 규약) |

### §1-1 11단계 하위 id — 승인 편마다 돈다

`--from-step 11` = `11:resources` 부터. 편집실 재렌더는 `11:render`(연출 재배치만) 또는 `11:resources`(구간 수정). 변수 `n` 은 승인 순위(1 = `shorts.mp4`).

| 하위 id | 하는 일 | v3 흡수 | v1 추출·호출 | v5 합류 · O9 |
|---|---|---|---|---|
| `11:resources` | 승인 조각 → 편집 재료 | **맨 앞 자막 정정**(`check_names/fix_names` 거리1 ∪ 초성일치∧거리2 · `drop_repetition`) · `word_subtitles` · `plan_narration_slots`(M11) · `narration_windows`·`split_by_windows`(M15 뮤트 창) · `speaker_colors`(M13) | `clips_beyond_source` · `_resolve_cue_anchors` · `snap_cues_to_dialogue_gaps` · `synthesize_cue_cached` · 무음 컷·발화 갭 페이싱·길이 클램프(채널 게이트대로) · 마스킹 → 단일 줄 → 오버라이드 | 자막 정정 자리 = 여기 · TTS 라벨 목소리도 유료 백엔드 실사용 시 캐시 · 어절 규칙 = v1 읽기 속도 정본 · 편집실 키별 순수 함수 + `origin:"editor"` + 스냅 관용 0.5s · **TTS 캐시 키는 편 간 공유**(같은 문구·목소리면 승인 편끼리 재사용) |
| `11:draft` | 초벌 **720p / 30fps** 중립 캔버스(O9) | `stage4.render_draft` | — | 입력 seek 로 수정 · `--draft-height`(기본 720 · 480 은 속도 옵션) · 뮤트 규칙을 최종과 같은 함수로 |
| `11:style` | 연출 결정 · 1(+2)/편 · **`media_resolution=HIGH`**(O9) | 프리셋 · `STYLE_ALLOWED` · M12 라벨 위치·rotate·color·fx(보정+노트) · `label_x_range` · 확정 design 밴드 클램프 · M14 pop→자막 fx · `fit_title_sizes` · `resolve_work_logo` · `plan_labels` | `style_compose` 어휘 전부 · `title_fixed` · 굵게·회전 AI 금지 · `fill_title_gaps` · `clamp_texts_to_band` · 얼굴 회피 · sfx · `title_windows_owner` | 연출 시스템 통합 스키마 · 입력 = 720p 초벌 + 자막·TTS 띠 y 프롬프트 + validate 벨트 · HIGH 는 §2-G 대로 **720p 와 한 세트**(480p+HIGH 는 최악 조합) — 화면 속 방송 텔롭·잔글씨를 읽어 라벨을 피한다. 단 §2-H: 읽은 글자는 **배치 근거로만**, 사실 판정에 안 쓴다 · 편집실 라운드 무재호출 · `checkpoint_style` E15 키 모양 |
| `11:render` | 최종 1080×1920/30fps | `render_final` 어댑터 부분 · `place_above_burned`+`runs_in_window` · `muted_windows` · 로고 · 제목 폭 | `renderer.render_short` · 번인 회피(E17-2·E18-6) · 밴드 앵커(E10) · `cues_within_video`·`sfx_within_video` · `audio_qa` | `render_adapter.py` 추출(v1·v3 병합) · **E15 variant 구멍 메우기 필수**(variant #2·#3 이 image_overlays·title_segments·text_subtitle_path 를 못 받던 것 — O7 로 2위 이하도 연출을 다 받는다) · 폰트 이원화 · 산출 `shorts.mp4`·`shorts_{n}.mp4` |
| `11:validate` | 발행 판정 · 1/편 | `run_validate` 차단 3종 · 경고(진행감·루프·`frame_vision_qc` 4×5·반복·audio_qa) · `snap_belt.pct` 기록+경고 | `validator.validate_output` | 발행 메타 형식 [신설] · 입력 어댑터(`stage1_doc·stage2_doc·grid` → `checkpoint_candidates`·`grid.json`) · **편별** `validation_{n}.json` |

## §2 시간 정본 — 격자는 재료, 6c 가 게이트

rev.7 §5 문장 그대로다. 달라진 것은 재료를 **v3 모듈로 만들고**, 후보 시각을 **6c 가 전사로 검증**한다는 것이다(O5).

- **눈금** = `grid.json` 의 span 경계 ∪ scene_cuts ∪ 무음 경계 ∪ {0, 러닝타임}. v3 `transcribe·timegrid·scenecut·audio` 를 `app/modules/grid/` 로 승격. `arousal` 은 소비 0.
- **LLM 은 절대초를 낸다.** 조각마다 **`quote`(그 구간 대표 대사 1줄)** 를 함께 내게 한다 — 6c 가 그 대사를 전사에서 찾아 시각이 맞는지 확인한다. 대사 없는 구간은 `quote:null` 이고, 그 조각은 ③ 발화 커버리지 검사만 받는다.
- 6c 판정 셋: **ok**(quote 가 그 구간 전사에 있음) · **relocated**(quote 가 다른 시각에만 있음 → 그 시각으로 조각 이동, 길이 유지) · **dropped**(quote 가 어디에도 없음 = 환각, 또는 소스 밖). 이 셋은 `timestamp_check.candidate_problem` 이 이미 v1 에서 하던 판정이다(2026-08-24 상류 차단) — v4 는 그것을 **단계로 승격**하고 전량 기록한다.
- **스냅은 관용 안에서만, 비대칭**(rev.7 M2): start 앞 ≤2.0/뒤 ≤0.5 · end 뒤 ≤2.0. 편집실 스냅도 관용 0.5s.
- **정합률 기록** — validate `snap_belt.pct`. 30편 쌓이면 N2 로 차단 승격 여부.

## §3 시각 호출 예산 — 산식 둘, 계단 하나

rev.7 §2 A~H 실측은 그대로다. v5 의 3시간 실측이 **상한 판정 단위**를 바꿨고, O2 가 기본 fps 를 계단으로 정했다.

```
상한 판정(400 초과)  : count = D × (fps × 71 + 32)      ← 실제 400 은 이 값으로 난다(v5 3시간 실측)
과금·예산 집계      : usage = D × (fps × 66 + 25)       ← usageMetadata. 멀티파트는 countTokens 3.8배 과소 — 반드시 usageMetadata

예산선 = 1,048,576 − 텍스트 토큰(3단계 전사 + 프롬프트·리서치, countTokens 실측) − 여유 30,000
fps_cap = floor_0.05( (예산선 − 32·D) / (71·D) )
step(D) = 4 (D ≤ 40분) · 3 (≤ 60분) · 2 (≤ 90분) · fps_cap (그 이상)         ← O2
fps     = min( step(D), 파일fps, 24, fps_cap )
fps_cap < 0.5 (= 1/SNAP_TOLERANCE_SEC) → 프록시 인코딩 앞에서 크게 실패
```

텍스트 토큰은 고정 유보(rev.7 85%)가 아니라 **실측**이다 — 전사 실측 54k/80분 ≈ 0.68k/분 + 프롬프트·리서치 ≈15k. 그래야 90분 fps 2 가 성립하는지가 소재마다 정확히 판정된다.

| 소재 | 텍스트(추정) | step | fps_cap | **요청 fps** | count | 상한 대비 | 비고 |
|---|---|---|---|---|---|---|---|
| 10분 | 22k | 4 | 20.45 | **4** | 189,600 | 18.1% | 파일 30fps 라 파일 상한은 없다 |
| 30분 | 35k | 4 | 6.50 | **4** | 568,800 | 54.2% | |
| 40분 | 42k | 4 | 4.60 | **4** | 758,400 | 72.3% | 계단 경계 |
| 60분 | 55k | 3 | 3.30 | **3** | 882,000 | 84.1% | 계단 경계 |
| 67분 | 60k | 2 | 2.90 | **2** | 699,480 | 66.7% | 비용 표 기준 |
| 90분 | 76k | 2 | **2.00** | **2** | 939,600 | 89.6% | ⚠ 텍스트 ≤79k 일 때만 2. 넘으면 cap 이 1.95 로 내리고 `sample_fps_note` 에 사유 |
| 120분 | 96k | cap | 1.35 | 1.35 | 920,520 | 87.8% | |
| 150분 | 116k | cap | 0.95 | 0.95 | 895,050 | 85.4% | |
| 180분 | 137k | cap | 0.65 | 0.65 | 843,930 | 80.5% | v5 실측 0.85 는 5만 유보 선 |
| ≈212분 | 158k | cap | 0.50 | 0.50 | — | — | **하한** — 그 이상은 fail-loud(텍스트 밀도에 따라 ±10분) |

- 계단 0.05 내림은 결정성. 하한 0.5 는 표본 간격이 스냅 관용 ±2s 와 같아지는 지점.
- ⚠ **파일 30fps 는 표본의 상한일 뿐이다**(O1). 요청 fps 최대는 6b 의 6 이라 30fps 파일의 추가 정보는 지금 소비처가 없다 — 비용은 업로드 크기다(§12). 결정대로 가되 `SCAN_PROXY_FPS` 상수 하나로 되돌릴 수 있게 둔다.
- fps 2 shadow(rev.7 §9-E 의 방향 교체): 첫 30편, ≤60분 소재에서 같은 핸들로 fps 2 후보 편성 1콜 더 → `candidates.shadow`. 지표는 결정적(dedup 후 후보 수·6c 통과율·7단계 생존 수·소스 커버리지). "계단 4/3 이 후보를 실제로 넓히는가"의 답.
- `media_processing=STATIC` 명시 + `google-genai` 핀 + 첫 실호출에서 산식 재검증.

**편당 입력(67분 · usage 단위 · 승인 k 편)**

| 단계 | 콜 | usage | 비고 |
|---|---|---|---|
| 6 후보 편성 fps 2 | 1 | 631,140 | 60분 소재는 fps 3 → 802,800 · 40분 fps 4 → 693,600 |
| 6b exception 프로브 | ≤3~8 | +50,000~230,000 | 창 120s = 50,520/창(±60s) · tail 180s 75,780 |
| 6c 구간 검증 | 0 | 0 | 순수 코드 |
| 8 시각 플래그 | ≤8 | ≤170,400 | 후보 수 비례 |
| 10 살붙이기 + 설명 | 2k | k × ≈10,000 | 텍스트 · 병렬 |
| 10a 승자 근방 정밀(선택) | ≤2k | k × 40,140 | 180s·3fps offset |
| 11:style **HIGH** 720p | k | **k × 96,540** | 60s·6fps·HIGH(프레임 264) — rev.8 의 25,260 대비 3.8배(O9) |
| **합계** | | **k=1: ≈0.96~1.14M** · k=3: ≈1.17~1.35M · k=8: ≈1.71~1.89M | v3 현행 1.29M. 출력·thinking 단가는 입력의 5배 — 20편 usage 뒤 달러 병기. **TTS 요금·렌더 시간도 k 배** |

## §4 v3 흡수 지도 — 모듈별 처분

| v3 모듈(줄) | 처분 | v4 자리 | 남는 것 / 버리는 것 |
|---|---|---|---|
| `transcribe`(218) | 승격 `modules/grid/` | 3 | 단어 정본 · 공백 재전사 · 인명 프롬프트 |
| `timegrid`(161) · `scenecut`(46) · `audio`(67) | 승격 `modules/grid/` | 4 | span 재단·눈금. `arousal` 은 소비 0 |
| `schemas`(228) | 승격 | 6c·7·11:validate | 시각 표기·`SNAP_TOLERANCE_SEC`·커버리지 검증 |
| `seq_analyze`(548) | 분해 | 4·5·6 | `_upload_video`·`VideoMetadata` 헬퍼·`resolve_scan_fps`(§3 규칙으로 일반화)·`build_scan_proxy`(720p/30fps) 남김. Stage 1 프롬프트·검증기 **폐기** — `exception_sector` 계약만 6단계로 |
| `chunk_split`(124) | **폐기** | — | offset 멀티파트가 대체 |
| `chunk_analyze`(660) | 축소 흡수 | 6c(각색 임계 0.35)·10a(선택) | `verify_time_alignment`·전사 diff 복원·`heard` |
| `refine`(594) | 흡수 | 6b | 순수 함수화 · 창 폭 ±60 인자 · 창 밖 재프로브 유지 · `retile_sequences` 폐기 |
| `story`(843) | 분해 | 6·7·9·10 | 템플릿 4종→6 · `score_story`→7 · `fallback_highlight`→9 · 제목 20자·M11 어휘→10. 3안 다안 심사는 후보 5~16 이 대체 |
| `assemble`(422) | 흡수 | 11:resources | `word_subtitles`·`plan_narration_slots`·`narration_windows`·`split_by_windows`·`speaker_colors`·`finalize_cues`·`verify_edit_plan`(→정합률) |
| `textcheck`(173) | 흡수 | 11:resources·11:validate | M9-A/B·M10-A · `--fix-names` 게이트 |
| `stage4`(558) | 흡수 | 11:draft·11:style | `render_draft`(seek·720p/30fps)·프리셋·`STYLE_ALLOWED`·`_call_style_model`(HIGH 인자)·M12·M14 |
| `finalize`(722) | 분해 | 11:style·render·validate | `plan_labels`·`label_x_range`·`fit_title_sizes`·`resolve_work_logo`·`subtitle_fx_windows`→style · 어댑터·`place_above_burned`→`render_adapter` · `run_validate`·`frame_vision_qc`→validate |
| `overrides`(135) | 재작성 | 11:resources | 두 결함 수정 → 키별 순수 함수 · 관용 0.5s 스냅 |
| `variants`(181) | 흡수 | 10 | `--hook-variants N`(선택) |
| `feedback`(143) | 흡수 | 사후 | `steps[candidate_rank]` 감사 표 → within-prompt 상관 · **승인 k 편의 성과 비교**가 처음으로 가능해진다(같은 소재·다른 후보) |
| `face_id` | 유지 | 6 병렬 슬롯 | `deps_absent` 규약 · 소비는 10a |
| `pipeline`·`cli`·`__main__` | 재작성 | `app/v4` | 11단계 재편 · `--max-shorts` 1~8 |
| replay `loader` | 확장 | — | v4 후보/승인/변형 인식 |
| v1 `timestamp_check` | 승격 | **6c** | `quotes_of`·`find_quote_times`·`candidate_problem`·`bounds_problem`·`filter_candidates` 그대로 — 후보 조각 형태(다중 구간)만 넓힌다 |

**M9~M15 승계 체크리스트(V4-M1 합격선)** — rev.8 표 그대로(11항). 자리 변경 없음.

## §5 v1 에서 추출하는 것 — rev.8 §5 그대로

`clips_beyond_source`(688) · `_resolve_cue_anchors`·`snap_cues_to_dialogue_gaps`(723·842) · `synthesize_cue_cached`(1195) ·
`_video_band_bottom`·`_compute_subtitle_margin_v`(1571·1616) · 번인 회피 3종(1710~1817) · 인라인 `[style]` 우선순위·오버라이드
적용 순서·자막 후처리 순서 · job/run_log 규약 → `app/modules/` 로, 모놀리스는 re-export. **AST 가드**: `app/v4` 가 `app.pipeline` 을
import 하지 않되 위 함수가 호출되는지. 추가: v1 **variant 렌더 루프**(`shorts_{n}.mp4` · `max_shorts` 클램프)는 v4 진입점이 1~8 로 넓혀 쓴다.

## §6 바깥 계약 표 — 승인 k 편이 되면서 바뀐 것

| 파일 | 읽는 쪽 | 읽는 것 | v4 정책 |
|---|---|---|---|
| `shorts.mp4` · `shorts_{n}.mp4` | 현지화 L0/L4 · 오케스트레이터 | 최종본 | v1 규약 그대로(1위 = `shorts.mp4`). ⚠ **현지화 `RENDER_OUTPUT` 은 상수 하나** — 2위 이하 현지화는 N8(별건) |
| `checkpoint_story.json` | 현지화 L3 · v1 render 재개 · 로더 | `title_text · variants[].title_text · clips` | **v1 모양 그대로 — `variants[k]` 가 곧 승인 k 편**(1위가 `clips`·`title_text`) + `candidate_id` |
| `checkpoint_candidates.json` [신설] | 편집실 · feedback · 로더 | 후보·verify·funnel·flags·rank·approved·exception·shadow | 지문 `sha1(후보 timeline + fps·프롬프트 판·루브릭 가중치)` |
| `edit_plan.json` · `edit_plan_{n}.json` | 오케스트레이터 ingest · 편집실 · 현지화 | `layout.top_title · timeline` + `description · hashtags` | 1위는 이름 유지 · 2위↓ 가산 파일(**ves ingest 확장 필요** — 검수 카드 다중 영상) |
| `checkpoint_style.json` · `_{n}` | 현지화 E16 | `texts[] · title_segments[] · title_fixed` | E15 키 모양 · 편별 |
| `checkpoint_resources.json` | 편집실 미리듣기 · 현지화 L3t | `tts_cue_files[].cue` · `tts_backend` | 모양 유지 · cue 에 `variant` 키 가산 |
| `subtitle_segments.json` · `_{n}` | 편집실 · 현지화 L3 | 줄·시각·style | 모양 유지 + `origin` |
| `validation.json` · `_{n}` | 오케스트레이터 | hard_fail · 경고 | 편별 · 발행은 편별 판정 |
| `run_log.json` | 현지화 · 오케스트레이터 · 로더 | 마커 · provenance · steps | `pipeline:"v4"` · `steps[].usage/model_version` · `approved_count` |
| `grid.json` · `checkpoint_grid_words.json` | 편집실 스냅 · 6c · validate · 10a | 눈금·전사 | 재료 사이드카 |
| `design_cli.json · edit_overrides.json` | 입력 | 디자인 키 · 편집실 수정 | 어댑터 `CHANNEL_DESIGN_FLAGS` 전체 · 모르는 플래그 거절 · `edit_overrides` 에 `variant` 키(없으면 1위) |

**현지화 진입점**: (a) v4 가 v1 모양을 함께 낸다 — V4-M1 범위. 2위 이하 현지화(b′)는 `RENDER_OUTPUT` 을 variant 인자로 넓히는 별건(N8).

## §7 캐시·지문·실패 정책

| 단계 | 파일 | 지문 재료 | 버리는 조건 | 실패 정책 |
|---|---|---|---|---|
| 3 | `checkpoint_grid_words` | 오디오 sha · whisper 설정 · SRT sha | 소재·SRT 바뀜 | 창 실패는 scene 폴백 + 명시 |
| 4 | `grid.json` · `checkpoint_probe` | 단어 sha · 임계 · 재단 상수 · **fps 계단 값** | 재료·상수 바뀜 | ffmpeg 실패 = 편 실패 |
| 5 | `checkpoint_upload` | 프록시 sha(720p/30fps 인코딩 파라미터 포함) | 48h 만료·`files.get` 실패 → 재업로드 | — |
| 6 | `checkpoint_candidates` | 격자 지문 + fps + 프롬프트 sha + 모델명 + 템플릿 키 + **후보 수 범위** | 어느 하나 바뀜 | 반려 소진 = **편 실패** |
| 6b | `exception_sectors` | 6 지문 + 창 폭(±60) | 6 바뀜 | 예산 소진·검증 실패 = 원판정 유지 |
| 6c | `verify` | 6 지문 + 전사 sha + 임계(0.35·40s) | 전사 바뀜 | 순수 · 전량 드롭 = 6단계 회송(예산 안) → 소진 시 편 실패 |
| 7·9 | `funnel`·`rank`·`approved` | 6·6b·6c + 루브릭 가중치 + `max_shorts` | 가중치·상한 바뀜 | 순수 · 승인 0 = 1위 경고 채택 |
| 8 | `flags`(후보 단위 증분) | 후보 timeline + fps 5 + 프롬프트 sha | 후보 바뀜 | 성공→플래그 / 실패→**미채점**(승인 불가) / 전량→`scoring_unavailable`(1위만 경고) |
| 10 | `checkpoint_story.variants[k]` | `candidate_id` + timeline + 톤 sha | 승인 목록 바뀜 — **편별 증분**(한 편 실패가 다른 편을 되돌리지 않는다) | 반려 소진 = 그 편만 탈락 + 기록 |
| 10a | `checkpoint_winner_detail` | timeline + fps 3 | 승인 목록 바뀜 | 실패 = 전사 채택 |
| 11:resources | `checkpoint_resources` · TTS 캐시(편 간 공유) | 편 + 오버라이드 + 정정 사전 | — | E11·E12·E17-3 규약 |
| 11:style | `checkpoint_style(_n)` | `sha1(timeline + label_plan + 프리셋)` | 구간 수정 시 재배치만 | 위반·실패 = 연출 없이 진행 + 기록 |
| 11:render | `render_fingerprint(_n)` · `checkpoint_burned_subtitle` | 확정 style_doc + 클립 지문 | 클립 구성 바뀜 | 회피 실패 = (None, []) 삼킴 · 한 편 렌더 실패는 그 편만 |

`--edit-overrides` 가 있으면 6~9 는 통째 건너뛴다(대상은 `variant` 키, 기본 1위). `--candidate-id` + clips/subtitles/tts 동시 지정은 즉시 실패.

## §8 운영 규약 — 합류

- **타임아웃**: 켜별 deadline(6단계 ≥450s) · 업로드 폴링 상한(**720p/30fps 는 업로드가 길다** — 상한을 파일 크기에 비례) · 편당 벽시계 상한(k 편 렌더 포함).
- **병렬**: 8단계 ≤8 · **10단계 k** · 11단계 렌더는 직렬(libx264 가 전 코어 — rev.7 §4 실측 speedup 0.99). Lock 안 check-and-increment.
- **결정성**: 6·8·10 temperature 0 · `model_version` · 후보 목록 고정.
- **API 표면**: `media_processing=STATIC` · SDK 핀 · HIGH 는 요청 단위(파트별 무시) — 스타일 호출은 파트 하나라 무관.
- **usage 전 항목** · 캡은 20편 p99×2 · implicit cache(6단계 재질의).
- **롤아웃**: 어댑터 게이트(`pipeline:"v4"`) · 전 노드 SHA 뒤 채널 키 · 롤백 = 키 제거 + 리허설 1회 · ves 는 **다중 영상 ingest·검수 카드**를 먼저 받는다(V4-M5·M9).

## §9 마일스톤

| | 내용 | 규모 | 발주처 | 합격선 |
|---|---|---|---|---|
| **V4-M0** | 선행 — v3 `overrides` design 결함 · replay 로더 v4 인식 · CLAUDE.md 토큰 절 보강 · 프로브 통합 · v1 `max_shorts` 클램프 위치 확인 | S | 엔진 | 회귀 가드 |
| **V4-M1** | 뼈대 — 포크 · 11단계+6b·6c+하위 id · `job.py`·`render_adapter`·모놀리스 추출 · `grid/` 승격 · `timestamp_check` 승격 · 타임아웃·usage · 어댑터 게이트 · 계약 대조 도구 · AST 가드 · M9~M15 체크리스트 · 테스트 처분표 | L | 엔진 | v3 실잡 픽스처 되쓰기 필드 동일 · v1 잡 키 diff · AST · 체크리스트 |
| **V4-M2** | 앞단 1~5 — 전사(텍스트 토큰 실측) · 격자 · fps 계단(§3 표 값 고정) · **720p/30fps 프록시**(업로드 시간 실측) · 핸들 수명 · STATIC | M | 엔진 | §3 표 값 테스트 · 3시간 fail-loud · 업로드 시간 기록 |
| **V4-M3** | 후보 편성 + 6b + **6c** + 깔때기 — 후보 5~16 · 제목 가안 · quote 계약 · ±60 창 · 전사 검증 · 루브릭 | M | 엔진 | 7편 레이블 miss 0 · **6c 픽스처: 환각 quote 드롭·재배치·소스 밖 클램프** · 골든 채점 · 결정성 |
| **V4-M4** | 시각 플래그 + **승인 게이트**(8·9) — 병렬 · 미채점 3갈래 · 승인 규칙 · `--max-shorts` 1~8 · 감사 | M | 엔진 | 동시 4 실측 · 승인 0 폴백 · 감사 표 · κ 재검증 |
| **V4-M5** | 살붙이기 **k 병렬**(10·10a) — M11 어휘 · 설명 호출 · `--candidate-id` · `variants[k]` 증분 | M | 엔진 / ves: `edit_plan_{n}` ingest · 검수 카드 다중 영상 | 편별 증분 실패 격리 · 발행 메타가 ves 까지 |
| **V4-M6** | 재료·초벌(11:resources·11:draft) — 자막 정정 · TTS 캐시 편 간 공유 · 어절 통일 · 오버라이드 키별(`variant`) · **초벌 720p/30fps seek** | M | 엔진 | 키별 회귀 · 캐시 적중 · 초벌 시간 실측(720p/30fps) |
| **V4-M7** | 스타일·렌더(11:style·11:render) — 통합 스키마 · **HIGH 스타일** · M12~M15 · 회피 · **E15 variant 구멍 메우기** · design 키 · 효과음 | L | 엔진 / 자산 | 라운드 후 지문 불변 · 화이트리스트 밖 0 · 프레임 QC 0 · **2위 이하도 연출 전부 수신** |
| **V4-M8** | 검증 + 실측 라운드(실소재 3편 · 승인 k 분포) | M | 엔진 | 위반 픽스처 차단 · usage 로 §3 확정 · 사람 검수 · 승인율·정합률 첫 기록 |
| **V4-M9** | 채널 컷오버 — 첫 채널 · 병행 대조 · 롤백 리허설 | M | ves | 기준선 30편 · 롤백 트리거 · 리허설 |
| **V4-M10** | A/B — fps 2 shadow 판정 · 10a on/off · HIGH 스타일 on/off(연출 품질 사람 판정) · 캡 p99×2 · κ · N2 | M | 엔진 | 비열등 + 비용 |

순서 대안: M1 → M2 → M6 → M7 → M3 → M4 → M5. **V4-M9 전에는 실채널 산출물이 바뀌지 않는다.**

## §10 결정 목록

**승계(확정)** — rev.7 §9 의 Q2~Q4·A~E 여덟 건. **Q1(1편 렌더 + top-3 보관)은 O7 이 대체했다.**

**운영자 결정 O1~O9 (2026-09-03) — 확정. 각 항의 대가와 조건**

| # | 결정 | 반영 | 대가 · 조건 |
|---|---|---|---|
| O1 | 5단계 프록시 **720p / 30fps** | §1 행 5 · `SCAN_PROXY_HEIGHT`·`SCAN_PROXY_FPS` | 토큰 불변(해상도·파일 fps 는 토큰에 없다). 파일 ≈2.5~3.3배(추정 200~260MB/60분) · 업로드 ≈2~3분. **표본 fps ≤6 이라 30fps 의 정보는 지금 소비처가 없다** — 상수 하나로 되돌릴 수 있다. 720p 는 나중에 6b·8 을 HIGH 로 올릴 때 필수 조합(§2-G) |
| O2 | 훑기 fps 계단 **≤40분 4 · ≤60분 3 · ≤90분 2 · 이후 fps_cap** | §3 규칙·표 | 60분 fps 3 = 84.1% · **90분 fps 2 는 텍스트 ≤79k 일 때만**(넘으면 1.95 자동 하향 + 기록). 계단 경계에서 불연속(60분 3 → 61분 2) |
| O3 | 후보 **최소 5 · 최대 16** | §1 행 6 · 프롬프트 · 지문 | 5 미만이면 반려 사유. 템플릿당 1~4개 |
| O4 | 6단계에서 살붙이기 동시 추출 — **부담이면 10 유지** | **10 유지 + 6단계에 `title_draft`(≤20자)만 추가** | 부담 근거: 출력 65,536 이 thinking 과 공유(v3 M2 절단 실측) · 후보당 2,500~4,000 × 16 = 확실히 초과 · 출력 단가 5배를 탈락 후보에 지불 · M11 문장 배열 어휘가 길다. 제목 가안은 후보당 ~15 토큰 |
| O5 | **전사로 LLM 구간 환각 검증 단계** | **6c 신설** | LLM 0콜 · `timestamp_check` 승격 · 6단계 출력에 `quote` 계약 추가 · 판정 ok/relocated/dropped 전량 기록 |
| O6 | 6b 경계 창 **±60s** | §1 행 6b | 창당 75,780 → 50,520 usage. 가왕쇼 지각 49.5s 는 창 안 · 포핸즈2 (91s) 는 **창 밖 재프로브 경로**로(M8 실측 경로 유지) |
| O7 | 9단계 **나가도 되는 영상 전부** | 승인 규칙(§1 행 9) · `--max-shorts` 1~8 | "나가도 된다" = **결함 게이트 전부 통과**(절대 점수 임계 아님 — M9·rev.7 §4 유지). 승인 k 편마다 10·11 전부 → +(k−1)×0.11M usage + TTS 요금 + 렌더 시간. ves 다중 ingest 선행 |
| O8 | 10단계를 승인 전부에 | §1 행 10 · 병렬 k · `variants[k]` | 편별 증분·실패 격리 |
| O9 | 11단계 **720p/30fps 초벌 + 스타일 HIGH** | §1-1 draft·style | 스타일 25,260 → **96,540/편** · 초벌 렌더 시간 ↑(실측 V4-M6) · HIGH 는 720p 와 한 세트(480p+HIGH 는 최악) · 읽은 글자는 배치 근거로만 |

**v5 열린 결정 7건의 처분** — rev.8 그대로(포크 · 11:resources 맨 앞 · v1 모양 병기 · 은퇴 일정 · E15 키 · 첫 채널/효과음은 운영자).

**신규(운영자 결정 대기)**

| # | 항목 | 권고 |
|---|---|---|
| N1 | 10a 기본 on/off | off · V4-M10 뒤 |
| N2 | 격자 정합률 차단 승격 조건 | 30편 `snap_belt.pct` p10 ≥ 98% |
| N4 | 문서 이름 | 이 판이 정본 · v5 문서에 "rev.9 로 흡수" 표기 |
| N5 | 실측 자산 | `scripts/gemini_tokens_probe/` → `docs/v4/probes/` |
| **N6** | 90분 fps 2 가 텍스트 초과로 안 들어갈 때 | **자동 1.95 하향 + 기록**(권고) vs 편 실패 |
| **N7** | 승인 상한 `--max-shorts` 기본값 | **8**(깔때기 상한 = "전부") — 채널이 줄인다. 비용은 O7 행 |
| **N8** | 2위 이하 현지화 | 별건(현지화 `RENDER_OUTPUT` 을 variant 인자로) — V4-M9 전에 결정 |

## §11 리스크

- **교훈 미승계** — 완화 넷(포크·추출·AST 가드·체크리스트)이 V4-M1 합격선.
- **계약이 조용히 어긋나는 것** — 필드 단위 대조 도구. **다중 출력이 새 어긋남 지점**이다(`_{n}` 파일·ves ingest·현지화 1위 한정).
- **비용의 k 배** — 승인이 늘수록 스타일 HIGH·TTS·렌더가 곱해진다. `--max-shorts` 와 V4-M8 승인율 실측이 벨트.
- **6c 오탐** — quote 편집거리 임계 0.35 는 v3 각색 판정값이다. 노래·감탄사 구간에서 오드롭 가능 → V4-M3 픽스처 + relocated 우선(드롭은 마지막).
- **90분 예산 경계** — O2 의 fps 2 가 텍스트에 종속. N6.
- **E15 variant 구멍** — O7 로 2위 이하가 연출을 받아야 하므로 V4-M7 필수. 안 메우면 2위 이하가 라벨·자막 강조 없이 나간다.
- **API 표면** — STATIC·SDK 핀·상수 회귀 가드.

## §12 확정 아닌 것 — 인용 시 주의

rev.7 §10 전항 + 아래.

| 항목 | 상태 |
|---|---|
| 720p/30fps 프록시 파일 크기·업로드 시간 | **추정**(480p/10fps 80MB·50초 실측에서 외삽) — V4-M2 실측 |
| HIGH 스타일이 연출 품질을 올리는가 | 미검증 — §2-G 는 글자 회수율만 잼. V4-M10 사람 판정 |
| 승인 게이트 통과율(편당 k 분포) | 미지 — V4-M8 첫 표본. 비용 표의 k 는 가정 |
| 6c 임계 0.35·40s 의 오탐률 | 미검증 — v3 각색 판정 조건에서 가져온 값 |
| 90분 소재 텍스트 토큰 76k | 추정(54k/80분 외삽) — 소재마다 실측이 규칙에 들어간다 |
| `media_processing` STATIC/AGENTIC · fps 필드 사용 중단 | 미확인(v5) |
| 10a 자막 정확도 · 템플릿 다양성 · 격자 정합률 분포 | 미검증 — rev.8 그대로 |

rev.9 (2026-09-03): 운영자 결정 O1~O9 반영 — 프록시 720p/30fps · fps 계단 4/3/2 + 실측 텍스트 예산선 · 후보 5~16 · 제목 가안 · **6c 구간 검증 신설** · 6b ±60 · 승인 = 결함 게이트 통과 전부(Q1 대체) · 살붙이기 k 병렬 · 초벌 720p/30fps + 스타일 HIGH · 다중 출력 계약(`_{n}`) · E15 variant 구멍 필수화 · N6~N8. / rev.8: 통합판 초판.
