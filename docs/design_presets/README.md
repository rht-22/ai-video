# 디자인 프리셋 (Design Presets)

채널 하나의 화면·소리·호흡 문법을 **옵션값 파일 하나**로 묶는다 — 프리미어 프로젝트
파일처럼, 모든 결정이 각각의 키로 드러나고 채널마다 값만 바꿔 재사용한다.

첫 프리셋: [`drama_clip_kr.preset.json`](drama_clip_kr.preset.json) —
신병4(일병)·쿠팡플레이(꿀벌무비) 클립 쇼츠 2편의 프레임(0.3s)·오디오·전사 실측에서
추출했다. 분석 전문: 벤치마킹 보고서 아티팩트(2026-08-28).

## 파일 구조

| 파일 | 역할 |
|------|------|
| `<preset>.preset.json` | **마스터** — 전 옵션값 + 적용 경로(status) + 근거 노트. 사람이 읽고 고치는 정본 |
| `<preset>.channel_design.json` | 마스터에서 **오늘 적용 가능한 부분집합**만 추린 것 — ves 채널 design 에 그대로 붙는다 |

## status 어휘 — "이 옵션은 어디에 꽂히는가"

마스터의 모든 옵션에는 `status` 가 있다. 옵션값이 실제로 작동하는 경로가 다 다르기
때문에, 이것이 없으면 "설정했는데 왜 안 먹지"가 된다.

| status | 적용 경로 | 적용 방법 |
|--------|-----------|-----------|
| `native` | 채널 design 키 → `--design-*` CLI | `channel_design.json` 을 ves 채널 design 에 병합 |
| `auto` | 엔진 계약이 자동 계산 | **값을 주지 않는 것이 곧 설정** (예: 자막 margin 은 E10 밴드 앵커) |
| `env` | 실행 노드 환경변수 | 노드 `ves.env` (예: `SILENCE_CUT_PROFILE`) |
| `work_asset` | 작품 층 (권리물) | 작품별 로고 등 — 채널 design 아님 |
| `prompt` | 스토리/E15 프롬프트의 채널 톤 | 엔진 기능은 있음 — 프롬프트에 노브를 여는 소형 작업 |
| `e19` | 신규 엔진 기능 | E19 발주 대상 (아래 표) |
| `ves` | 오케스트레이터(발행) | publish 잡 확장 |
| `qa` | 산출물 검사 지표 | 렌더를 바꾸지 않는다 — 결과를 잰다 |

## 적용 절차 (native 부분)

1. `channel_design.json` 내용을 대상 채널의 design 에 병합한다.
   ⚠ **통째 교체 금지** — 대시보드 저장이 기존 키를 증발시킨 사고가 있다(8/24 HANIPJUMAK).
2. `style_compose`·`narration`·`title_size2` 는 신 CLI 플래그다 — 엔진 전 노드 배포
   전이면 이 키들을 빼고 적용한다(구 엔진 argparse 즉사). `style_compose` 는
   ops_config `channel_style` 게이트도 켜야 실린다.
3. `subtitle_y_margin`·`tts_y_margin` 은 **비워 둔다** — `video_width: 1080` 명시가
   밴드 앵커(E10)를 켜는 조건이고, 명시값은 앵커를 이긴다.
4. 실렌더 1편으로 픽셀 확인 후 size·색 초기값을 확정한다(한 입 주막 절차).

## E19 대상 옵션 (이 프리셋이 요구하는 신규 기능)

| 옵션 | 내용 | 크기 |
|------|------|------|
| `title.keyword_highlight` | 제목 한 줄 안 단어 단위 색 강조 (`{{...}}` 인라인 마크업) | 소 |
| `subtitle.speaker_colors` | 화자별 대사 자막 색 (diarize 계약 변경 동반) | 중 |
| `subtitle.profanity_mask` | 자막 자체검열 (음성 원음 유지, 자막만 `XX`) | 소 |
| `narration.placement` | cue–대사 겹침 검사기 (gap 스냅/경고) | 소 |
| `labels_fx.placement` | 얼굴 위/아래 빈 공간 배치 + 컷 추적 재배치 (face_tracking 얼굴 검출 재사용 후보) | 중 |
| `labels_fx.glow_paren` | 괄호형 라벨 글로우 테두리 | 소 |
| `sfx.*` | 효과음 레이어 (라이선스 manifest, id만 AI 노출, 편당 캡, amix 입력 추가) | 중 |
| `audio_pacing.preserve_beat_pauses` | 무음 컷의 연출 정적 보존 창 | 소 |

`prompt` 항목(내레이션 톤·라벨 어휘/밀도/동기·러닝 개그·엔딩 컷 등)은 엔진 코드가
아니라 스토리/E15 프롬프트에 채널 톤 노브를 여는 작업으로 한 번에 묶인다.

## 새 프리셋을 만들 때

1. 벤치마크 영상을 받아 실측한다 — 컷(중앙값), 무음(개수·위치), LUFS, 라벨(창·색·계열),
   자막(색 코딩·회전 리듬), 내레이션(밀도·톤), 구조. 이번 절차는 보고서 아티팩트 §03~§13.
2. 마스터에 **모든** 옵션을 적는다 — 기본값과 같아도 명시한다(프리셋의 요점은 재현성).
3. `native` 만 추려 `channel_design.json` 을 만들고, 어댑터 허용 키 목록과 대조한다
   (모르는 키는 어댑터가 즉시 실패시킨다 — 오타 방지 규율).
