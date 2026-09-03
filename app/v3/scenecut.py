"""app.v3.scenecut — v3 호환 재수출 껍데기(ffmpeg scdet 장면 전환).

정본은 `app/modules/grid/scenecut.py` 다 — 2026-09-03 V4-M1 §7 승격으로 물리 이동했다
(왜 옮겼는지 · 왜 `import *` 가 아니라 별칭인지는 `app/modules/grid/__init__.py`).

이 파일은 자기 자신을 정본 모듈로 **치환**한다. 그래서 `app.v3.scenecut` 은
`app.modules.grid.scenecut` 과 **같은 객체**이고, 공개·비공개 이름도 monkeypatch 도
승격 전과 한 글자도 다르지 않다. 새 이름을 여기 더하지 말 것 — 정본에 더한다.
"""
import sys

from app.modules.grid import scenecut as _canonical

# ⚠ 이 대입이 이 파일의 전부다. 아래에 코드를 붙여도 실행은 되지만 아무도 못 본다
#   (다음 import 부터는 정본 모듈이 곧바로 돌아간다).
sys.modules[__name__] = _canonical
