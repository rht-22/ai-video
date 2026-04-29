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

    def build_appearance_index(
        self,
        video_path: Path,
        sample_interval_sec: float = 2.0,
        similarity_threshold: float = 0.4,
    ) -> list[dict[str, Any]]:
        """영상을 일정 간격으로 샘플링해 캐릭터 등장 구간 인덱스를 만든다.

        proxy(저해상·저fps) 영상을 입력으로 가정. 샘플 간격마다 한 프레임을 뽑아
        identify_in_frame()으로 등장 캐릭터를 추정하고, 인접 샘플에서 동일 캐릭터가
        연속하면 하나의 구간으로 병합한다.

        Returns:
            [{"character": str, "start_sec": float, "end_sec": float,
              "samples": [{"t": float, "x_norm": float, "y_norm": float, "similarity": float}, ...]}, ...]
            (start_sec 오름차순 정렬)

            x_norm/y_norm은 입력 프레임 기준의 정규화 좌표(0~1).
            reframe 단계에서 원본 해상도로 스케일 업해 좌표 lookup에 재사용한다.
        """
        if not self.references:
            return []

        import cv2

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return []
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 1.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_frames / fps if fps > 0 else 0.0

            char_intervals: dict[str, list[list[float]]] = {}
            char_samples: dict[str, list[dict[str, Any]]] = {}
            last_seen: dict[str, float] = {}
            merge_gap = sample_interval_sec * 1.6  # 1샘플 누락은 같은 구간으로 간주

            t = 0.0
            while t < duration:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ret, frame = cap.read()
                if not ret:
                    break
                fh, fw = frame.shape[:2]
                try:
                    identified = self.identify_in_frame(frame, similarity_threshold)
                except Exception:
                    identified = []

                seen_now: set[str] = set()
                for fd in identified:
                    name = fd.get("character", "unknown")
                    if name == "unknown":
                        continue
                    x1, y1, x2, y2 = fd["bbox"]
                    cx = (x1 + x2) / 2.0
                    cy = (y1 + y2) / 2.0
                    char_samples.setdefault(name, []).append({
                        "t": float(t),
                        "x_norm": float(cx / fw) if fw else 0.0,
                        "y_norm": float(cy / fh) if fh else 0.0,
                        "similarity": float(fd.get("similarity", 0.0)),
                    })
                    seen_now.add(name)

                end_t = t + sample_interval_sec
                for name in seen_now:
                    if name in last_seen and (t - last_seen[name]) <= merge_gap:
                        char_intervals[name][-1][1] = end_t
                    else:
                        char_intervals.setdefault(name, []).append([t, end_t])
                    last_seen[name] = t
                t += sample_interval_sec
        finally:
            cap.release()

        appearances: list[dict[str, Any]] = []
        for name, intervals in char_intervals.items():
            samples_all = char_samples.get(name, [])
            for s, e in intervals:
                seg_samples = [smp for smp in samples_all if s <= smp["t"] < e]
                appearances.append({
                    "character": name,
                    "start_sec": float(s),
                    "end_sec": float(e),
                    "samples": seg_samples,
                })
        appearances.sort(key=lambda x: x["start_sec"])
        return appearances


def find_target_in_index(
    appearances: list[dict[str, Any]],
    target_character: str,
    t_sec: float,
    frame_width: int,
    frame_height: int,
    max_dt_sec: float = 3.0,
) -> tuple[float, float] | None:
    """character_index에서 target_character의 t_sec 시점 좌표를 lookup한다.

    가장 가까운 sample을 사용 (선형 보간 미적용 — reframe의 EMA 스무딩이 부드럽게 만든다).
    sample이 없거나 가장 가까운 sample이 max_dt_sec를 넘으면 None.
    좌표는 정규화된 sample을 frame_width/height로 스케일 업한 픽셀 값.
    """
    best_sample: dict[str, Any] | None = None
    best_dt = float("inf")
    for ap in appearances:
        if ap.get("character") != target_character:
            continue
        if ap["start_sec"] - 1.0 <= t_sec <= ap["end_sec"] + 1.0:
            for smp in ap.get("samples") or []:
                dt = abs(smp["t"] - t_sec)
                if dt < best_dt:
                    best_dt = dt
                    best_sample = smp
    if best_sample is None or best_dt > max_dt_sec:
        return None
    return (
        best_sample["x_norm"] * frame_width,
        best_sample["y_norm"] * frame_height,
    )

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
