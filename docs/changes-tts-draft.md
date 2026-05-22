# 변경 요약 — tts_draft / visual_essential / thinking_level + 프롬프트 v2

날짜: 2026-05-22
브랜치: `feat/rollback`

## 한 줄 요약

candidate에 `tts_draft`(내레이션 초안)·`visual_essential`(시각 비트 보호) 필드 추가, Gemini 호출을 temperature → `thinking_level`로 교체, 프롬프트 두 개 구조 정리.

## 큰 변경 4가지

| 항목 | 무엇 | 왜 |
|------|------|----|
| `tts_draft` | candidate에 12~25자 내레이션 한 줄. TTS 디렉터가 정본으로 참조 | description에서 추출하던 라인이 영상과 멀어지는 회귀 → 영상을 직접 본 단계에서 미리 생성 |
| `visual_essential` | true면 silence_cutter가 무음 컷 스킵 | 펜 각인·표정·소품 같은 시각 핵심 비트가 잘려나가는 문제 |
| `thinking_level` | temperature 6개 제거, thinking_level 6개로 교체 | Gemini 3.x 공식 가이드(sampling 끄고 thinking 제어) |
| 프롬프트 v2 | GEMINI / STORY 두 프롬프트 구조 재정리 (P1·P2 통합, 라운드 주석 제거, STORY는 4원칙·4단계·clip_selection_plan 도입) | 라운드 누적으로 룰이 산재 → 가독성·일관성 |

## 파일별

- `gemini_client.py` — 프롬프트 v2 + thinking_level + 새 필드 스키마 + schema 검증 강화
- `pipeline.py` — tts_draft / visual_essential 전파 + Gemini 절대시간 sanity check + `run_log_gemini.json` 원본 보존
- `silence_cutter.py` — visual_essential 무음 보호 + flatten 시 새 필드 보존
- `story_builder.py` — `StoryClip`에 `tts_draft`, `visual_essential` 필드
- `work_researcher.py` — temperature → thinking_level

## 운영 주의

- **env var 이름 변경**: `GEMINI_*_TEMPERATURE` → `GEMINI_*_THINKING_LEVEL` (구 변수는 무시됨)
- **STORY 프롬프트 v2는 라이브 테스트 중** — `_validate_story_response` 코드 검사는 미연결 상태(프롬프트만의 행동 변화). 회귀 보이면 `STORY_COMPOSITION_PROMPT` 본문만 직전 커밋(e7a2874)으로 되돌리면 됨
- **새 파일** `run_log_gemini.json` 자동 생성 (gemini 단계 원본 응답 보존, 진단용)
