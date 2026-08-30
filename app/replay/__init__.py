"""V3-M0 리플레이 하네스 — 저장 아카이브에서 구간 분포 지표를 재계산하는 저울.

LLM 재실행 없이(비용 0 · 결정적 · 반복 가능) 저장 데이터만으로 동작한다.
v3 파이프라인(4단계 재구성)의 모든 마일스톤 합격 판정이 이 저울을 지난다.

아카이브 실물 위치 (2026-08-30 실측 — 추측이 아니라 DB 확인):
  - **정본은 오케스트레이터 DB `clip_metadata` 표**다. 편(clip_id)당 한 행에
    `checkpoint_story`·`edit_plan`·`run_log`(요약본)가 jsonb 로 통째로 들어 있고,
    2026-07-15 부터 전 기간이 있다. 문제 정의 §3의 "저장된 614편"이 바로 이 표다
    (측정 시점 8/24 오후 이후 같은 창에 5편이 더 들어와 지금은 619편).
  - Supabase Storage `ves-runs/<sha256(run_id)[:16]>/bundle/` 에는 8/13 이후 런의
    **전체 텍스트 번들**(subtitle_segments·checkpoint_gemini·checkpoint_silence_cut …)이
    있다 — 문장 절단·맥락 확장 상세 같은 2차 지표는 이 번들이 있어야 나온다.
  - 사람 기준선: `editor_baselines`(AI 원안 타임라인 스냅샷) + `job_queue` 의
    generate 잡 `params.edit_overrides`(사람이 고친 clips).

지표 정의 동결 (문제 정의 §3 재현으로 검증됨 — report.REFERENCE_S3 대조):
  - **원안 구간** = `checkpoint_story.clips` — 스토리 조립이 끝난(통삭제·스냅·클램프 **뒤**)
    · 무음 컷 **앞** 클립. raw_response 의 storyline 구조(hook/build/payoff)가 아니다 —
    그걸로 세면 §3 분포(1개 31% · 2개 34% · 3개 32%)가 재현되지 않는다(실측).
  - **최종 구간** = `edit_plan.timeline` — 실제 렌더에 들어간 클립(무음 컷 뒤).
    ⚠ 편집실 재렌더가 있었던 편은 edit_plan 이 **사람 손을 탄 값**으로 덮여 있다
    (clip_metadata 는 재렌더 시 갱신된다 — human_edited 플래그로 구분해 집계).
  - 무음컷이 쪼갬 = 최종 > 원안 · 줄어듦(통삭제 흔적) = 최종 < 원안.

모듈 구성 — CLI 는 `python -m scripts.replay_harness` 하나:
  loader   아카이브 로드(스냅샷 레이아웃 + job 디렉토리 레이아웃 자동 판별)
  metrics  편 단위 지표 추출 + 집계(순수 — 테스트 대상)
  report   §3 기준선 대조 + JSON/Markdown 리포트(순수)
  golden   골든 케이스 채점 인터페이스(M3 에서 실소재 채점 — 여기서는 인터페이스까지)
  fetch    DB/Storage → 로컬 아카이브 스냅샷(표준 라이브러리 urllib 만 사용 — 의존 0)
"""
