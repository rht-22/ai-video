"""L-P0 — 의존성 실측 도구의 순수 로직 고정.

이 도구의 숫자가 requirements 이관 판단의 근거가 된다. 틀린 숫자는 없느니만 못하다
(노드 첫 실행에서 venv 크기가 82.5 MiB 로 나왔다 — 심볼릭 링크를 따라가 파이썬
설치를 재고 있었다).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.deps_probe import (  # noqa: E402
    _older, conflict_severity, cv2_winner, delta_report, find_conflicts, human,
    normalize, summarize_resolution, venv_root,
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


# ── cv2 승자 — delta_report 가 못 보는 어긋남 ───────────────────────────
def test_cv2_winner_detects_contrib_shadowing_base():
    """노드 실측: 해석표는 opencv-python 4.14 인데 런타임 cv2 는 4.10(contrib)이었다."""
    resolved = {"opencv-python": "4.14.0.94", "opencv-contrib-python": "4.10.0.84"}
    w = cv2_winner(resolved, "4.10.0")
    assert w["winner"] == "opencv-contrib-python"
    assert w["shadowed"] == "opencv-python"


def test_cv2_winner_detects_base_winning():
    resolved = {"opencv-python": "4.14.0.94", "opencv-contrib-python": "4.10.0.84"}
    assert cv2_winner(resolved, "4.14.0")["winner"] == "opencv-python"


def test_cv2_winner_none_when_only_one_installed():
    assert cv2_winner({"opencv-python": "4.14.0.94"}, "4.14.0")["winner"] == "opencv-python"


def test_cv2_winner_none_when_ambiguous_or_missing():
    assert cv2_winner({}, "4.10.0") is None
    assert cv2_winner({"opencv-python": "4.10.0.84",
                       "opencv-contrib-python": "4.10.0.84"}, "4.10.0") is None
    assert cv2_winner({"opencv-python": "4.14.0.94"}, "") is None


# ── 부트스트랩은 신호가 아니다 ──────────────────────────────────────────
def test_absent_ignores_bootstrap_packages():
    d = delta_report({"pip": "25.0", "setuptools": "80.0", "real-thing": "1.0"}, {})
    assert d["absent"] == ["real-thing"]


# ── opencv 두 배포판은 같은 버전으로 묶여 있어야 한다 (L-P4, 2026-08-25) ──────
# 둘은 같은 `cv2` 디렉토리를 덮어써서 *설치 순서*가 런타임 버전을 정한다. 한쪽만
# 적으면 다른 쪽이 전이로(deepface·retina-face·rapidocr) 상한 없이 들어와 5.x 가
# 되고, 5.x 는 번들 haarcascade 가 없어 얼굴검출이 죽는다. 버전을 맞춰 두면 누가
# 이기든 결과가 같다 — `cv2_winner` 가 같은 버전을 '승자 없음'으로 보는 이유다.
def _requirements_text() -> str:
    return (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")


def _pinned(name: str) -> str:
    for line in _requirements_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith(name + "=="):
            return line.split("==", 1)[1].strip()
    raise AssertionError(f"requirements.txt 에 `{name}==` 핀이 없다")


def test_both_opencv_distributions_are_pinned_to_the_same_version():
    assert _pinned("opencv-contrib-python") == _pinned("opencv-python")


def test_pinned_opencv_is_below_5_because_5x_drops_bundled_haarcascades():
    assert int(_pinned("opencv-python").split(".")[0]) < 5


# ── 공존 경고는 버전이 갈릴 때만 경고다 ──────────────────────────────────
def test_conflict_severity_same_version_is_not_a_warning():
    g = ["opencv-contrib-python", "opencv-python"]
    assert conflict_severity(g, {"opencv-contrib-python": "4.10.0.84",
                                 "opencv-python": "4.10.0.84"}) == "same"


def test_conflict_severity_mixed_versions_is_the_real_warning():
    g = ["opencv-contrib-python", "opencv-python"]
    assert conflict_severity(g, {"opencv-contrib-python": "4.10.0.84",
                                 "opencv-python": "5.0.0.93"}) == "mixed"


def test_conflict_severity_unknown_keeps_the_warning():
    """버전을 모르면 판정하지 않는다 — 가드가 조용히 사라지는 것이 오판보다 나쁘다."""
    g = ["opencv-contrib-python", "opencv-python"]
    assert conflict_severity(g, {"opencv-contrib-python": "4.10.0.84"}) == "unknown"
    assert conflict_severity(g, {}) == "unknown"
