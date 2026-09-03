"""`python -m app.v4` — pipeline v4 엔트리포인트(계약 §6).

v3 와 같은 모양이다(`app/v3/__main__.py`) — 기존 `app.cli` 는 **한 줄도 고치지 않는다**.
v4 를 단일 진입점에 합류시키는 것은 채널 전환 마일스톤의 별건이다.
"""
from app.v4.cli import main

raise SystemExit(main())
