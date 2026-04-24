"""인물 인식 모듈 — deepface 기반 얼굴 임베딩 매칭.

배우 프로필 사진에서 레퍼런스 임베딩을 만들고,
영상 프레임에서 감지된 얼굴과 매칭해 인물을 식별합니다.

deepface가 없으면 모든 함수가 graceful하게 빈 결과를 반환합니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class FaceReference:
    character_name: str
    actor_name: str
    embedding: np.ndarray   # ArcFace 임베딩 벡터
    image_path: Path | None


def _has_deepface() -> bool:
    import importlib.util
    return importlib.util.find_spec("deepface") is not None


class FaceIdentifier:
    """배우 사진에서 레퍼런스 임베딩을 만들고, 프레임에서 얼굴을 매칭."""

    def __init__(self, model_name: str = "ArcFace", detector_backend: str = "opencv"):
        if not _has_deepface():
            raise ImportError("deepface가 설치되지 않았습니다. pip install deepface")
        self.model_name = model_name
        self.detector_backend = detector_backend
        self.references: list[FaceReference] = []
        # 모델 사전 로드 (첫 호출 시 자동 다운로드)
        self._initialized = False

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        from deepface import DeepFace
        # 더미 호출로 모델 로드
        try:
            DeepFace.build_model(self.model_name)
        except Exception:
            pass
        self._initialized = True

    def build_references(self, cast_images: list) -> None:
        """배우 프로필 사진에서 임베딩을 추출해 레퍼런스 DB 구축.

        Args:
            cast_images: CharacterInfo 리스트 (image_path가 있는 항목만 처리)
        """
        from deepface import DeepFace
        self._ensure_init()

        for char in cast_images:
            img_path = getattr(char, "image_path", None)
            if not img_path or not Path(img_path).exists():
                continue

            try:
                import cv2
                img_array = cv2.imdecode(
                    np.frombuffer(Path(img_path).read_bytes(), dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                result = DeepFace.represent(
                    img_path=img_array,
                    model_name=self.model_name,
                    detector_backend=self.detector_backend,
                    enforce_detection=False,
                )
                if result and len(result) > 0:
                    embedding = np.array(result[0]["embedding"], dtype=np.float32)
                    self.references.append(FaceReference(
                        character_name=getattr(char, "character_name", ""),
                        actor_name=getattr(char, "actor_name", ""),
                        embedding=embedding,
                        image_path=Path(img_path),
                    ))
                    print(f"    [FaceID] 레퍼런스 등록: {char.actor_name} ({char.character_name})")
            except Exception as e:
                print(f"    [FaceID] 레퍼런스 추출 실패 ({getattr(char, 'actor_name', '?')}): {e}")

    def identify_in_frame(
        self,
        frame: np.ndarray,
        similarity_threshold: float = 0.4,
    ) -> list[dict[str, Any]]:
        """프레임에서 얼굴을 감지하고 레퍼런스와 매칭.

        Returns:
            [{"bbox": (x1,y1,x2,y2), "character": str, "similarity": float}, ...]
        """
        if not self.references:
            return []

        from deepface import DeepFace

        try:
            faces = DeepFace.represent(
                img_path=frame,
                model_name=self.model_name,
                detector_backend=self.detector_backend,
                enforce_detection=False,
            )
        except Exception:
            return []

        results = []
        for face_data in faces:
            face_emb = np.array(face_data["embedding"], dtype=np.float32)
            facial_area = face_data.get("facial_area", {})

            best_name = "unknown"
            best_sim = -1.0

            for ref in self.references:
                # 코사인 유사도
                dot = float(np.dot(face_emb, ref.embedding))
                norm = float(np.linalg.norm(face_emb) * np.linalg.norm(ref.embedding))
                sim = dot / norm if norm > 0 else 0.0

                if sim > best_sim:
                    best_sim = sim
                    best_name = ref.character_name

            if best_sim < similarity_threshold:
                best_name = "unknown"

            x = facial_area.get("x", 0)
            y = facial_area.get("y", 0)
            w = facial_area.get("w", 0)
            h = facial_area.get("h", 0)

            results.append({
                "bbox": (x, y, x + w, y + h),
                "character": best_name,
                "similarity": best_sim,
            })

        return results

    def find_target_in_frame(
        self,
        frame: np.ndarray,
        target_character: str,
        similarity_threshold: float = 0.4,
    ) -> tuple[float, float] | None:
        """프레임에서 특정 캐릭터의 얼굴 중심 좌표를 반환.

        Returns:
            (center_x, center_y) 또는 None (타겟 인물 미발견)
        """
        identified = self.identify_in_frame(frame, similarity_threshold)
        target_faces = [f for f in identified if f["character"] == target_character]

        if not target_faces:
            return None

        best = max(target_faces, key=lambda f: f["similarity"])
        x1, y1, x2, y2 = best["bbox"]
        return ((x1 + x2) / 2, (y1 + y2) / 2)
