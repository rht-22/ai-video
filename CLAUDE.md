# AI Video Shorts Creator

## Gemini 모델 사용 규칙

이 프로젝트에서 Gemini API 모델은 반드시 아래 두 가지만 사용한다:

| 역할 | 모델명 |
|------|--------|
| 정밀 분석 (Pro) | `gemini-3.1-pro-preview` |
| 스크리닝/스토리 구성 (Flash) | `gemini-3-flash-preview` |

`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-3.1-flash-preview` 등 다른 모델은 사용 금지.  
환경변수(`GEMINI_MODEL_NAME`, `GEMINI_FLASH_MODEL_NAME`)와 코드 기본값 모두 위 두 모델로 고정.

## 아키텍처 개요

- **Pro 모델** (`gemini-3.1-pro-preview`): 청크별 영상 분석 (Agent 2, `analyze_chunk()`)
- **Flash 모델** (`gemini-3-flash-preview`): 스크리닝 및 스토리 구성 (Agent 1/3, `screen_candidates()`, `compose_story()`)
- **청크 분할**: 프록시(480p)를 청크별로 물리적 분할 후 각각 독립 업로드 → 10fps 유지 가능
