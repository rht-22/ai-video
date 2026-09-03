"""정본 격자(grid) 재료 — v3·v4 공용.

**왜 여기 있나**(V4-M1 §7 승격 · 2026-09-03): 이 여섯은 원래 `app/v3/` 안에 있었다.
그런데 v3 는 곧 동결되고(v4 기획 결정 4) v4 는 이 여섯을 그대로 쓴다 — v3 안에
두면 v3 이 은퇴하는 날 v4 가 함께 끊긴다. 베끼는 것은 더 나쁘다(베낀 코드는 언젠가
한쪽만 고쳐진다 — 이 레포가 L-P1·P4 이식에서 반복해 배운 것). 그래서 **물리 이동**
하고 `app/v3/<name>.py` 는 재수출 껍데기로 남겼다. 승격의 합격 조건은 v3 회귀 0 이고
`tests/test_v4_grid_promote.py` 가 그것을 고정한다.

⚠ 여기 있는 것은 **격자 재료**뿐이다 — 판단(스토리·연출·스테이지 프롬프트)은
파이프라인 몫이고 승격 대상이 아니다. 이 여섯은 전부 leaf 이고 서로에 대한 의존은
`arousal → audio`(SAMPLE_RATE) 하나뿐이다. 그 성질을 깨는 import 를 새로 들이지 말 것 —
승격의 값어치가 '파이프라인과 무관하게 부를 수 있다'에 있다.

  schemas     시각 표기·스냅·커버리지 검증(순수) — 933초 사고 방어의 관문
  timegrid    span 후보 재단(cue 규칙 = stt_elevenlabs.words_to_segments 재사용)
  scenecut    ffmpeg scdet 장면 전환(의존 추가 0 · 전 환경 동일 산출)
  audio       16kHz PCM 로드 · silencedetect 무음 구간 목록
  arousal     정서 곡선(전 장르 공통 오디오 피처 4종)
  transcribe  단어 타임스탬프 전사 + 실패 창 커버리지 + 공백 재전사

**시간 정본 원칙**(v3 기획서 §1 · v4 가 그대로 승계): LLM 은 경계를 제안할 뿐,
확정 시각은 이 격자의 눈금에 스냅되어 기록된다.

## 껍데기가 `import *` 가 아니라 **모듈 별칭**인 이유 (실측으로 정한 것)

`app/v3/<name>.py` 여섯은 `sys.modules[__name__] = <정본 모듈>` 한 줄이다 —
즉 `app.v3.audio is app.modules.grid.audio` 다. 계약 초안은 `from … import *` +
비공개 이름 명시 재수출이었는데, 실제로 해 보니 v3 회귀 0 이 안 된다:

① `import *` 는 `_` 접두를 안 가져오는데 이 레포는 실제로 비공개 이름을 모듈 밖에서
   부른다(`app/v3/pipeline.py:294` 의 `_build_whisper_prompt` · 테스트의
   `_transcribe_range`). 목록으로 좇으면 다음 사람이 하나를 빠뜨린다.
② 더 결정적으로 `import *` 는 **monkeypatch 를 끊는다**. 테스트가
   `setattr(app.v3.transcribe, "_get_whisper_model", 가짜)` 를 해도 정본 함수는 자기
   모듈 globals 를 보므로 가짜가 안 먹는다. 실측: 이 방식으로 test_v3_refine ·
   test_v3_stage1 · test_v3_stage2 **5건**이 깨졌다.

별칭이면 이름도 패치도 전부 그대로다. 승격의 합격 조건이 회귀 0 이므로 이쪽이 맞다.
"""
from __future__ import annotations

from app.modules.grid import (  # noqa: F401  — 서브모듈 재수출(`grid.audio` 등)
    arousal,
    audio,
    scenecut,
    schemas,
    timegrid,
    transcribe,
)

# 계약(V4-M1 §7)이 패키지 수준에 올리라고 못박은 이름들 — 부르는 쪽이
# `from app.modules.grid import build_grid_doc` 한 줄로 끝낼 수 있어야 한다.
from app.modules.grid.arousal import compute_arousal
from app.modules.grid.audio import detect_silence_intervals, load_pcm
from app.modules.grid.scenecut import detect_scene_cuts
from app.modules.grid.schemas import SNAP_TOLERANCE_SEC, parse_ts
from app.modules.grid.timegrid import build_grid_doc, carve_spans
from app.modules.grid.transcribe import retranscribe_gaps, transcribe_words

__all__ = [
    # 서브모듈
    "arousal", "audio", "scenecut", "schemas", "timegrid", "transcribe",
    # 계약 지정 이름
    "SNAP_TOLERANCE_SEC",
    "build_grid_doc",
    "carve_spans",
    "compute_arousal",
    "detect_scene_cuts",
    "detect_silence_intervals",
    "load_pcm",
    "parse_ts",
    "retranscribe_gaps",
    "transcribe_words",
]
