# v3 기록 → Tikitaka 소비 경로 (2026-09-15)

모든 필드가 같은 방식으로 사용되는 것은 아니다. 원본 Stage 2는 index의 `v3_stage2`와 잡/편별 `stage2.json`에 그대로 보존한다. 저장된 사실을 전부 새 편성 규칙으로 해석하지 않는다.

| 기록 | 실제 소비 | 보존만/제약 |
|---|---|---|
| Stage 1 sequences/content/chunks/time | v3 청크 계획·원본 재단, Tikitaka windows의 회차 요약 | 의미 경계가 대본 입력에 전달됨 |
| Stage 1 exception_sector | 청크에서 제거; 대본 줄·순간·덮개·렌더 제외 범위에 반영 | 원본 전사 파일의 줄은 삭제하지 않음 |
| Stage 1 shorts_candidates 등 편성 제안 | 원본 문서에 보존 | Tikitaka 14전략 편성을 대체하지 않음 |
| grid span ID/t_in/t_out/is_audio/time_authority | 관찰 귀속·화자 겹침·순간 ID·컷·자막 좌표 | 기존 Tikitaka STT 단어로 v3 grid 생성. Whisper로 전사를 바꾸지 않음 |
| meaning content/characters/time | Tikitaka SC 장면·대본 문맥·같은 장면 덮개 후보 | 의미 단위를 임의로 단일 span으로 해체하지 않음 |
| meaning mood/importance | 대본/작품 이해의 보충 기록 | 직접 효과를 켜는 규칙은 아님 |
| span scene_script/characters | 순간 설명, 덮개 선택·프로브, Stage 4 인물 근거 | 화면 인물을 화자로 자동 대체하지 않음 |
| span importance | 훅 첫 화면 하한·덮개 후보, 재관찰 트림 보호 | v3 값을 유지 |
| span subject_pos | 덮개 컷 → 편집 타임라인 → 최종 크롭 | v3에서 값이 없는 경우 중앙 기본 동작 |
| screen_text/has_text/screen_text_kind | v3 원본 정독, 대본 인용 근거, 자료화면 hold 제한, Stage 4 화면 근거 | 대사 구간의 화면 글자도 대본 보충 기록으로 전달 |
| diegesis | 대본의 회상/상상 구분, 덮개 후보 프롬프트, 리뷰 기록 | 전용 회상 효과를 자동으로 추가하는 기능은 아님 |
| is_claim | 대본 보충 기록으로 전달 | v3 register/irony 판단 체인은 미연결; 라벨용 register는 빈 목록 |
| audio_script.speaker | STT 줄과 시간 겹침으로 화자 연결 | 여러 이름이 겹치면 임의 다수결하지 않고 미상·감사 목록 |
| audio_script.line/heard_text | 공유 v3 Stage 2 내부 전사 대조 및 원문 보존; 덮개 사실 데이터에도 포함 | Tikitaka 전사 문구를 덮어쓰지 않음 |
| conf/text_source | v3 Stage 2 내부 대조·원문 감사 | Tikitaka 자막 교정/저확신 UI에는 새로 연결하지 않음 |
| validation/coverage/chunk audits | 실패 청크 제외·재시도·실행 기록 | 모델 원문/감사 세부를 대본에 모두 덤프하지 않음 |
| face/character index | 기존 문서가 분석 작업 디렉토리에 있을 경우 공유 v3 M2가 읽음 | 이 연결에서 별도 얼굴 인덱싱 단계는 실행하지 않음 |

## 연결 중 발견해 고친 누락

- `finish.run`이 원본 Stage 2를 단일 span meaning 문서로 재구성하며 `audio_script`·`heard_text`를 비우던 경로: v3 backend는 원본 문서를 저장하도록 변경.
- 대본 입력의 화면 글자·diegesis가 무성 moments에만 한정되던 경로: v3 backend는 대사 span의 화면 글자·diegesis·is_claim도 보충 기록으로 전달.
- 실패 청크와 Stage 1 제외 구간: 영상에서만 빠지고 대사 재료에는 남는 일이 없도록 대본·조립 제외 범위까지 전달.

## 기존 성공 산출 재생 검증

EP01 성공 v3 `ep01x01/stage2.json` + 현재 Tikitaka 전사를 오프라인 변환: SC 장면 73개, 무성 순간 727개, grid 관찰 1,366개. 전사 1,000줄의 문구·시각·ID 변경 없음. 원본 Stage 2 문서 동일. 단일 화자 연결 845줄, 다중 화자 근거 45줄은 미상(서로 다른 전사 분할을 맞댄 검사이므로 새 생성 정확도 수치가 아님). 크레딧 2943.5~3146.325초는 대본·덮개 제외 범위에 반영.

검사 출력: `outputs_tikitaka_grid/jigeum_ep01_20260915_run1/v3_adapter_check/`.
