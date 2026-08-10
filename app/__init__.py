"""ai-video 패키지.

파이썬 하한을 여기서 못 박는다. requirements.txt 의 `fastapi==0.135.1` 이 사실상 3.10+ 를
요구하는데 그 사실이 어디에도 적혀 있지 않아, 새 맥(시스템 python 3.9.6)에서 venv 설치가
통째로 실패한 적이 있다(2026-07-23 실측, SETUP_NEW_MACHINE.md §11-1). 설치가 아니라
실행 단계에서 3.9 로 들어오는 경우엔 더 알아보기 어려운 에러가 나므로, 여기서 먼저 멈춘다.

※ brain 레포(ai-improvement-edit-video)는 3.9 를 쓴다 — 두 레포의 하한이 다르다.
"""
import sys

if sys.version_info < (3, 10):
    raise RuntimeError(
        f"ai-video 는 Python 3.10 이상이 필요합니다 (현재 {sys.version.split()[0]}). "
        "venv 를 3.10+ 로 다시 만드세요 — 예: brew install python@3.12 && "
        "python3.12 -m venv .venv"
    )
