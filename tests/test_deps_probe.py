"""L-P0 — 의존성 실측 도구의 순수 로직 고정.

이 도구의 숫자가 requirements 이관 판단의 근거가 된다. 틀린 숫자는 없느니만 못하다
(노드 첫 실행에서 venv 크기가 82.5 MiB 로 나왔다 — 심볼릭 링크를 따라가 파이썬
설치를 재고 있었다).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.deps_probe import (  # noqa: E402
    _older, delta_report, find_conflicts, human, normalize, summarize_resolution, venv_root,
)


# ── venv 루트 — 심볼릭 링크에 속지 않는다 ───────────────────────────────
def test_venv_root_does_not_follow_symlink():
    """`.venv/bin/python` 이 시스템 파이썬 심볼릭 링크라도 venv 를 가리켜야 한다."""
    assert venv_root("/opt/ves/engines/ai-video/.venv/bin/python") == \
        Path("/opt/ves/engines/ai-video/.venv")


def test_venv_root_relative_path():
    assert venv_root(".venv/bin/python") == Path(".venv")


# ── 이름 정규화 ─────────────────────────────────────────────────────────
def test_normalize_pep503():
    assert normalize("Pillow") == "pillow"
    assert normalize("opencv_contrib.python") == "opencv-contrib-python"
    assert normalize("  PyYAML  ") == "pyyaml"


# ── 같은 모듈을 덮어쓰는 배포판 ─────────────────────────────────────────
def test_opencv_pair_is_a_conflict():
    got = find_conflicts(["opencv-python", "opencv-contrib-python", "numpy"])
    assert got == [["opencv-contrib-python", "opencv-python"]]


def test_single_opencv_is_fine():
    assert find_conflicts(["opencv-python", "numpy"]) == []


def test_conflict_detection_is_name_normalized():
    assert find_conflicts(["OpenCV_Python", "opencv-contrib-python"])


# ── 버전 비교 ───────────────────────────────────────────────────────────
def test_older_detects_downgrade():
    assert _older("2.3.5", "2.5.1") is True       # 노드 실측: numpy 2.5.1 → 2.3.5
    assert _older("6.0.2", "6.0.3") is True
    assert _older("3.3.0", "3.4.0") is True


def test_older_false_for_upgrade_and_equal():
    assert _older("2.5.1", "2.3.5") is False
    assert _older("1.0.0", "1.0.0") is False


def test_older_tolerates_suffixes():
    assert _older("1.2.3rc1", "1.3.0") is True
    assert _older("2026.8.18.122307.dev0", "2026.9.1") is True


# ── 델타 — 다운그레이드를 따로 센다 ─────────────────────────────────────
def test_delta_splits_downgrade_from_upgrade():
    d = delta_report({"numpy": "2.5.1", "idna": "3.18"},
                     {"numpy": "2.3.5", "idna": "3.19"})
    assert d["downgraded"] == ["numpy: 2.5.1 → 2.3.5"]
    assert len(d["changed"]) == 2


def test_delta_absent_is_not_removal():
    """pip install -r 은 지우지 않는다 — 이름이 'removed' 면 거짓말이 된다."""
    d = delta_report({"gone": "1.0"}, {})
    assert d["absent"] == ["gone"]
    assert "removed" not in d


def test_delta_added():
    d = delta_report({}, {"torch": "2.9.1"})
    assert d["added"] == ["torch"]
    assert d["changed"] == []


def test_delta_is_name_normalized():
    d = delta_report({"PyYAML": "6.0.3"}, {"pyyaml": "6.0.3"})
    assert d["added"] == [] and d["absent"] == [] and d["changed"] == []


# ── pip --report 파싱 ───────────────────────────────────────────────────
def test_summarize_resolution():
    rep = {"install": [
        {"metadata": {"name": "Torch", "version": "2.9.1"}},
        {"metadata": {"name": "numpy", "version": "2.3.5"}},
        {"metadata": {}},                      # 이름 없는 항목은 건너뛴다
    ]}
    assert summarize_resolution(rep) == {"torch": "2.9.1", "numpy": "2.3.5"}


def test_summarize_empty_report():
    assert summarize_resolution({}) == {}


# ── 표시 ────────────────────────────────────────────────────────────────
def test_human_units():
    assert human(512).endswith("B")
    assert "MiB" in human(5 * 1024 ** 2)
    assert "GiB" in human(3 * 1024 ** 3)
