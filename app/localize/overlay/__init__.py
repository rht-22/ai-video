"""overlay 현지화 — 완성본 mp4 한 개를 다른 언어판으로 다시 그린다 (L-P4).

발주서: ves-orchestrator `docs/LOCALIZE_UNIFY.md` §3-2. video-localization-project
`engine/*` + `src/process_video.py` 를 **충실히 이식**한 것이다.

⚠ **rerender(`app/localize/`)와 입력이 다르다.**

| | rerender | overlay |
|---|---|---|
| 입력 | ai-video job 디렉토리(체크포인트) | **외부 완성본 mp4 한 개** |
| 화면 속 한글 | 애초에 안 그린다 | 인페인팅으로 지우거나 병기하거나 그대로 |
| 컷 재현 | gen_flags 복원 → 프레임 단위 | 해당 없음(원본 시간축) |
| 쓰는 채널 | 혜미리예채파 · 잔망루피 롱폼 | **잔망루피 쇼츠** |

route (구 '등급' — `mode` 와 섞여 혼선을 낳던 이름을 강등했다):

    A   inpaint ✗ · subtitle   — 무변환, 자막 트랙만
    B   inpaint ✓ · replace    — 화면 한글 지우고 일본어 재합성
    BJ  inpaint ✗ · bilingual  — 한글 두고 일본어 병기
    C   inpaint ✓ · replace  + dub  — 더빙 (잔망루피 실운영)
    BC  inpaint ✓ · clean    + dub  — 더빙자막이 자막 담당

route 정의는 `data/pipeline.config.yaml` 의 `levels` 가 정본이다(이름만 route 로 읽는다).

의존성: OCR(rapidocr·paddleocr·easyocr)과 인페인트(opencv·lama·sttn·propainter)는
**전부 지연 임포트 + 폴백**이다. 무거운 것을 requirements 에 넣지 않았고, 백엔드 선택은
config 가 한다 — 없는 백엔드는 다음 후보로 넘어간다(`detect._FALLBACK_ORDER`).

🛑 `propainter` 는 S-Lab **비상업** 라이선스라 `inpaint.propainter_commercial_ack=true`
없으면 차단된다(회귀 0 계약 §8-7). 이식하면서 이 게이트를 그대로 옮겼다.
"""
from __future__ import annotations

# route → config.levels 키. 구 '등급' 이름을 그대로 쓴다(설정 파일이 정본이라 바꾸면 어긋난다).
ROUTES = ("A", "B", "BJ", "C", "BC")
DUB_ROUTES = ("C", "BC")          # 더빙이 뒤따르는 route (어댑터 needs_dub 과 같은 값)

__all__ = ["ROUTES", "DUB_ROUTES"]
