"""pipeline v3 — 병행 신규 파이프라인 (create_shorts_v3).

기획 정본: 아티팩트 「리캡 쇼츠 기법 이식안」(2026-08-30 v3) · 문제 정의
`docs/cut-granularity-problem.md`. 기존 14단계는 무중단 운영 — 이 패키지는
공유 모듈을 import 재사용만 하고 기존 코드를 한 줄도 바꾸지 않는다.

M1 범위(이 커밋): 정본 격자(grid) + Stage 1(seq_analyze).

⚠ **격자 재료 6종은 2026-09-03 에 `app/modules/grid/` 로 승격했다**(V4-M1 §7) —
v3 가 동결돼도 v4 가 끊기지 않게 하려는 것이다. 아래 여섯의 정본은 그쪽이고
`app/v3/<name>.py` 는 같은 모듈을 가리키는 별칭 껍데기다(회귀 0).
  schemas     시각 표기·스냅·커버리지 검증(순수) — 933초 방어의 관문   [승격]
  audio       PCM 로드·silencedetect 구간 목록                        [승격]
  arousal     정서 곡선(전 장르 공통 오디오 피처 — 소비는 M3)          [승격]
  transcribe  단어 타임스탬프 전사(speech 설정 재사용) + 실패 창 커버리지 [승격]
  scenecut    ffmpeg scdet 장면 전환(전 환경 동일 산출)                [승격]
  timegrid    span 후보 재단(cue 규칙 = stt_elevenlabs.words_to_segments 재사용) [승격]
  seq_analyze Stage 1 — Pro 1회 제안 → 스냅·커버리지·반려 루프
  pipeline    오케스트레이션 + run_log/체크포인트(기존 job 레이아웃 규약)
  cli         `python -m app.v3`

시간 정본 원칙(§1): LLM 은 경계를 제안할 뿐, 확정 시각은 grid 에 스냅되어
기록된다. 스토리·스타일 단계(M3+)는 시각을 출력하지 않고 span ID 만 다룬다.
"""
