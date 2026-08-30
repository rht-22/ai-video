"""V3 face_id 복원(레퍼런스-프리 클러스터링) 회귀 가드 — deepface 없이 돈다.

복원 계약:
  1) assign_cluster 는 순수 로직 — 같은 사람(유사 임베딩)은 같은 라벨,
     다른 사람은 새 라벨, running-mean 으로 중심이 갱신된다.
  2) 모듈 import 는 deepface 없이 성공하고, FaceIdentifier 생성만 ImportError.
  3) v3 슬롯은 deepface 부재를 deps_absent 로 구분 기록한다(module_absent 아님).
"""
from __future__ import annotations

import numpy as np
import pytest

from app.modules.face_id import assign_cluster, cosine_similarity


def _deepface_usable() -> bool:
    try:
        from deepface import DeepFace  # noqa: F401
        return True
    except Exception:  # ImportError·ValueError(tf-keras 부재) 모두 '사용 불가'
        return False


HAS_DEEPFACE = _deepface_usable()


def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def test_same_person_same_label():
    base = _unit([1.0, 0.2, 0.1, 0.0])
    near = _unit([0.98, 0.22, 0.12, 0.01])  # cos ≈ 0.999
    cents: list[dict] = []
    a = assign_cluster(base, cents)
    b = assign_cluster(near, cents)
    assert a == b == "person_1"
    assert len(cents) == 1 and cents[0]["n"] == 2


def test_different_person_new_label():
    a = assign_cluster(_unit([1, 0, 0, 0]), cents := [])
    b = assign_cluster(_unit([0, 1, 0, 0]), cents)  # cos = 0 < threshold
    assert (a, b) == ("person_1", "person_2")
    assert len(cents) == 2


def test_running_mean_updates_centroid():
    cents: list[dict] = []
    assign_cluster(_unit([1, 0.1, 0, 0]), cents)
    assign_cluster(_unit([0.9, 0.3, 0, 0]), cents)
    centroid = cents[0]["sum"] / cents[0]["n"]
    # 중심이 두 벡터 사이로 이동
    assert cosine_similarity(centroid, _unit([1, 0.1, 0, 0])) > 0.9
    assert cosine_similarity(centroid, _unit([0.9, 0.3, 0, 0])) > 0.9


def test_threshold_boundary():
    cents: list[dict] = []
    assign_cluster(_unit([1, 0, 0, 0]), cents, threshold=0.55)
    # cos(45°) ≈ 0.707 ≥ 0.55 → 합류
    joined = assign_cluster(_unit([1, 1, 0, 0]), cents, threshold=0.55)
    assert joined == "person_1"
    # cos ≈ 0.5 < 0.55 → 신설 (중심이 이동했으므로 더 먼 벡터로)
    new = assign_cluster(_unit([-0.2, 1, 0, 0]), cents, threshold=0.55)
    assert new == "person_2"


def test_module_imports_without_deepface():
    # 모듈 자체는 numpy 만 필요 — import 성공이 곧 v3 슬롯의 module_absent 탈출 조건
    import app.modules.face_id as m
    assert hasattr(m, "FaceIdentifier")


@pytest.mark.skipif(HAS_DEEPFACE, reason="deepface 설치 환경에서는 생성이 성공해야 함")
def test_identifier_raises_importerror_without_deepface():
    from app.modules.face_id import FaceIdentifier
    with pytest.raises(ImportError):
        FaceIdentifier()


def test_v3_slot_records_deps_absent(tmp_path, monkeypatch):
    if HAS_DEEPFACE:
        pytest.skip("deepface 설치 환경 — deps_absent 경로 없음")
    from app.v3.pipeline import _character_index_slot
    out: dict = {}
    _character_index_slot(tmp_path, tmp_path / "proxy.mp4", None, out, log=lambda *a: None)
    assert out.get("status") == "deps_absent"
    assert "requirements-faceid" in out.get("note", "")
