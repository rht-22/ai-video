"""V4-M1 §7 추출 회귀 가드 — v1 모놀리스의 함수를 app/modules 로 옮긴 것을 고정한다.

**왜 옮겼나**: v3 는 `app/pipeline.py` 를 한 줄도 import 하지 못했다(`_resolve_cue_anchors`
는 비공개였고 나머지는 7,035줄 모놀리스 안에 있었다). 그래서 E10·E12·E14·E18-2·E19·E20·E21
대응을 하나도 승계하지 못했다. v4 는 **같은 함수를 부른다** — 그러려면 함수가 밖에서
부를 수 있는 자리에 있어야 하고, 모놀리스는 자기 호출 지점을 위해 재수출해야 한다.

이 파일이 못박는 것은 그 이동이 **회귀 0** 이라는 것이다:
  ① 새 경로에서 부를 수 있다
  ② 구 경로(`app.pipeline.X`)가 **같은 객체**다 — 별칭이지 복사본이 아니다.
     복사본이면 한쪽만 고쳐지고 그 순간 v1 과 v4 의 판정이 갈린다(옮긴 이유 그 자체).
  ③ 옛 비공개 이름 `_resolve_cue_anchors` 로도 계속 부를 수 있다
     (`tests/test_cue_anchor_resolve.py` 25건이 이 이름으로 부른다)
  ④ 함께 옮긴 상수의 값이 안 바뀌었다 — 값이 판정이다
  ⑤ 새 모듈을 import 해도 `app.pipeline`(모놀리스·무거운 의존)이 끌려오지 않는다.
     끌려오면 "v4 는 모놀리스를 import 하지 않는다"는 계약이 이름만 남는다.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ── ① 새 경로에서 부를 수 있다 ──────────────────────────────────────────────

def test_new_paths_import():
    """v4 가 부를 이름들이 새 모듈에 공개로 있다."""
    from app.modules.clip_guard import CLIP_LOST_TOLERANCE_SEC, clips_beyond_source
    from app.modules.cues import (
        CUE_OVERLAP_TOLERANCE_SEC,
        resolve_cue_anchors,
        snap_cues_to_dialogue_gaps,
    )

    assert callable(clips_beyond_source)
    assert callable(resolve_cue_anchors)
    assert callable(snap_cues_to_dialogue_gaps)
    assert isinstance(CLIP_LOST_TOLERANCE_SEC, float)
    assert isinstance(CUE_OVERLAP_TOLERANCE_SEC, float)


# ── ② 구 경로가 같은 객체 ───────────────────────────────────────────────────

def test_monolith_reexports_the_same_object():
    """`app.pipeline.X is app.modules.X` — 복사본이면 언젠가 한쪽만 고쳐진다."""
    import app.pipeline as P
    from app.modules import clip_guard, cues

    assert P.clips_beyond_source is clip_guard.clips_beyond_source
    assert P.resolve_cue_anchors is cues.resolve_cue_anchors
    assert P.snap_cues_to_dialogue_gaps is cues.snap_cues_to_dialogue_gaps


# ── ③ 옛 비공개 이름 별칭 ───────────────────────────────────────────────────

def test_legacy_private_alias_is_the_same_function():
    """`_resolve_cue_anchors` 는 옛 이름이지 다른 함수가 아니다.

    모놀리스 안의 호출 지점(`[tts cues]` 블록)과 `tests/test_cue_anchor_resolve.py`
    가 이 이름으로 부른다 — 감싼 함수를 새로 만들면 그 순간 두 벌이 된다."""
    import app.pipeline as P
    from app.modules import cues

    assert cues._resolve_cue_anchors is cues.resolve_cue_anchors
    assert P._resolve_cue_anchors is cues.resolve_cue_anchors


# ── ④ 함께 옮긴 상수의 값 ───────────────────────────────────────────────────

def test_moved_constants_keep_their_values():
    """값이 곧 판정이다 — 이동이 값을 흔들면 회귀 0 이 아니다.

    · CLIP_LOST_TOLERANCE_SEC 1.0 — 끝 경계 소수점 어긋남(실측 0~0.2s)은 통과시키고
      클립이 통째로 증발하는 것만 잡는 자(2026-08-24 실측 5건).
    · CUE_OVERLAP_TOLERANCE_SEC 0.2 — 릴레이 문법의 맞닿는 경계 관용치(E19-3).
    · _MIN_CUE_TAIL 0.5 — 앵커 소재가 잘려나갔을 때 클립 끝에서 확보할 여유."""
    from app.modules.clip_guard import CLIP_LOST_TOLERANCE_SEC
    from app.modules.cues import CUE_OVERLAP_TOLERANCE_SEC, _MIN_CUE_TAIL

    assert CLIP_LOST_TOLERANCE_SEC == 1.0
    assert CUE_OVERLAP_TOLERANCE_SEC == 0.2
    assert _MIN_CUE_TAIL == 0.5


def test_monolith_constants_are_the_moved_ones():
    """모놀리스가 자기 사본을 따로 들고 있지 않다."""
    import app.pipeline as P
    from app.modules import clip_guard, cues

    assert P.CLIP_LOST_TOLERANCE_SEC == clip_guard.CLIP_LOST_TOLERANCE_SEC
    assert P.CUE_OVERLAP_TOLERANCE_SEC == cues.CUE_OVERLAP_TOLERANCE_SEC
    assert P._MIN_CUE_TAIL == cues._MIN_CUE_TAIL


def test_render_safety_margin_stays_in_the_monolith():
    """`RENDER_SAFETY_MARGIN_SEC` 은 따라가지 않았다 — cue 판정이 아니라 길이 클램프다.

    E19-3 블록 옆에 적혀 있어 같이 옮기기 쉬운 자리인데, 실제로 이 값을 읽는 곳은
    `_fit_storyline_to_duration` 계열(길이 클램프·narrative-ext 예산)이고 cue 두 함수는
    한 번도 안 본다. 쓰지 않는 모듈로 상수를 옮기면 다음 사람이 그 값을 cue 관용치로
    읽는다."""
    import app.pipeline as P
    from app.modules import cues

    assert P.RENDER_SAFETY_MARGIN_SEC == 0.3
    assert not hasattr(cues, "RENDER_SAFETY_MARGIN_SEC")


# ── ⑤ 새 모듈은 모놀리스를 끌고 오지 않는다 ─────────────────────────────────

def test_extracted_modules_do_not_import_the_monolith_source():
    """AST — 새 모듈 어디에도 `app.pipeline` 임포트가 없다(함수 안 임포트 포함).

    ⚠ 파일 단위 이동은 **함수 안 임포트**를 놓친다(P4 이식 실측: 지연 임포트 3건이
    문법 검사·모듈 임포트를 전부 통과하고 런타임에 죽었다). 그래서 모듈 top-level 이
    아니라 트리 전체를 훑는다."""
    for rel in ("app/modules/clip_guard.py", "app/modules/cues.py"):
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("app.pipeline"), f"{rel}: {a.name}"
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("app.pipeline"), \
                    f"{rel}: {node.module}"


def test_importing_extracted_modules_does_not_load_the_monolith():
    """AST 만으로는 부족하다 — 간접 임포트로도 모놀리스가 딸려오면 계약이 깨진다.

    서브프로세스에서 새 모듈만 import 하고 `sys.modules` 를 본다(같은 프로세스에서는
    다른 테스트가 이미 app.pipeline 을 올려 놓아 판정이 안 된다)."""
    code = (
        "import sys; import app.modules.clip_guard, app.modules.cues; "
        "assert 'app.pipeline' not in sys.modules, sorted(m for m in sys.modules "
        "if m.startswith('app.')); print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok" in r.stdout


# ── 이동 후에도 판정이 같다(대표 실측 1건씩) ────────────────────────────────

class _Clip:
    def __init__(self, start: float, end: float, chunk: int = 0, cand: int = 0):
        self.start_sec = start
        self.end_sec = end
        self.chunk_index = chunk
        self.candidate_index = cand


def test_behaviour_survived_the_move_clip_guard():
    """혜미리예채파 2화 실측 — 소스 189.99s 인데 payoff 가 200.0~226.0s(통째 증발)."""
    from app.modules.clip_guard import clips_beyond_source

    bad = clips_beyond_source([_Clip(31.5, 56.5), _Clip(200.0, 226.0)], 189.9898)
    assert [b["index"] for b in bad] == [1]
    assert bad[0]["rendered"] == 0.0
    # 소스 길이를 모르면 판정하지 않는다(오판 금지).
    assert clips_beyond_source([_Clip(200.0, 226.0)], 0.0) == []


def test_behaviour_survived_the_move_cue_anchor():
    """앵커 212.0s 는 두 번째 조각(200~226 · base 25.0) 안 → 편집 37.0s.

    렌더 late-cue 안전망(`tests/test_render_late_cue_guard.py`)이 기준으로 삼는 그 수식."""
    from app.modules.cues import resolve_cue_anchors

    clips = [_Clip(31.5, 56.5), _Clip(200.0, 226.0)]
    cue = {"text": "내레이션", "source_time_sec": 212.0, "duration_sec": 3.5,
           "chunk_index": 0, "candidate_index": 0}
    out = resolve_cue_anchors([cue], clips)
    assert len(out) == 1
    assert out[0]["start_sec"] == pytest.approx(37.0)
    assert out[0]["end_sec"] == pytest.approx(40.5)
    # 순수 — 넘겨받은 cue 를 제자리에서 고치지 않는다.
    assert "start_sec" not in cue


def test_behaviour_survived_the_move_gap_snap():
    """대사와 겹친 cue 는 gap 으로 스냅되고, 들어갈 gap 이 없으면 경고만 남는다."""
    from app.modules.cues import snap_cues_to_dialogue_gaps

    cues = [{"text": "겹침", "start_sec": 2.0, "end_sec": 5.0}]
    out, rep = snap_cues_to_dialogue_gaps(cues, [_seg(0.0, 4.0), _seg(8.0, 12.0)], 20.0)
    assert rep["cue_snapped"] == 1
    assert out[0]["start_sec"] == pytest.approx(4.0)
    # 원본 cue 는 그대로(순수)
    assert cues[0]["start_sec"] == 2.0

    # 대사가 편 전체를 덮으면 옮길 자리가 없다 — 지우지 않고 경고만 센다.
    out2, rep2 = snap_cues_to_dialogue_gaps(
        [{"text": "겹침", "start_sec": 2.0, "end_sec": 5.0}], [_seg(0.0, 20.0)], 20.0)
    assert (rep2["cue_snapped"], rep2["warned"]) == (0, 1)
    assert out2[0]["start_sec"] == 2.0


class _Seg:
    def __init__(self, s: float, e: float):
        self.start_sec = s
        self.end_sec = e


def _seg(s: float, e: float) -> _Seg:
    return _Seg(s, e)
