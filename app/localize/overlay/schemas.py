"""파이프라인 단계 간 데이터 계약(dataclass + JSON 직렬화).

흐름:  detect → detections.json (DetectionDoc)
       translate → translations.json (TranslationDoc, source 텍스트로 dedup)
       render 가 둘을 합쳐 일본어를 합성/자막화.

순수 stdlib(dataclasses) 만 사용 → 의존성 없이 테스트 가능.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

BBox = tuple[int, int, int, int]  # (x1, y1, x2, y2)


@dataclass
class Style:
    """탐지된 텍스트의 대략 스타일(재렌더 시 원본 느낌 복원에 사용)."""
    color: tuple[int, int, int] = (255, 255, 255)
    font_size: int = 32
    position: str = "bottom-center"     # top/center/bottom × left/center/right
    bold: bool = True
    serif: bool = False
    stroke_color: tuple[int, int, int] = (0, 0, 0)
    stroke_width: int = 3

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Style":
        return Style(
            color=tuple(d.get("color", (255, 255, 255))),                # type: ignore[arg-type]
            font_size=int(d.get("font_size", 32)),
            position=d.get("position", "bottom-center"),
            bold=bool(d.get("bold", True)),
            serif=bool(d.get("serif", False)),
            stroke_color=tuple(d.get("stroke_color", (0, 0, 0))),        # type: ignore[arg-type]
            stroke_width=int(d.get("stroke_width", 3)),
        )


@dataclass
class Region:
    """한 프레임 내 텍스트 영역."""
    bbox: BBox
    text: str
    confidence: float = 1.0
    style: Style = field(default_factory=Style)
    flagged: bool = False               # confidence 낮음 등 사람 검수 필요

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Region":
        return Region(
            bbox=tuple(d["bbox"]),                                       # type: ignore[arg-type]
            text=d.get("text", ""),
            confidence=float(d.get("confidence", 1.0)),
            style=Style.from_dict(d.get("style", {})),
            flagged=bool(d.get("flagged", False)),
        )


@dataclass
class FrameDetections:
    frame_idx: int
    timestamp: float
    regions: list[Region] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "FrameDetections":
        return FrameDetections(
            frame_idx=int(d["frame_idx"]),
            timestamp=float(d.get("timestamp", 0.0)),
            regions=[Region.from_dict(r) for r in d.get("regions", [])],
        )


@dataclass
class DetectionDoc:
    """detect.py 산출물 (outputs/{video_id}/detections.json)."""
    video_id: str
    fps: float
    width: int
    height: int
    sample_every: int
    ocr_backend: str
    roi: Optional[BBox] = None
    frames: list[FrameDetections] = field(default_factory=list)

    def unique_texts(self) -> list[str]:
        """등장 순서를 보존한 중복 제거 텍스트 목록(번역 입력용)."""
        seen: dict[str, None] = {}
        for fr in self.frames:
            for r in fr.regions:
                t = r.text.strip()
                if t:
                    seen.setdefault(t, None)
        return list(seen.keys())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "DetectionDoc":
        return DetectionDoc(
            video_id=d["video_id"],
            fps=float(d["fps"]),
            width=int(d["width"]),
            height=int(d["height"]),
            sample_every=int(d.get("sample_every", 1)),
            ocr_backend=d.get("ocr_backend", "unknown"),
            roi=tuple(d["roi"]) if d.get("roi") else None,              # type: ignore[arg-type]
            frames=[FrameDetections.from_dict(f) for f in d.get("frames", [])],
        )

    def save(self, path) -> None:
        from app.localize.overlay.common import write_json
        write_json(self.to_dict(), path)

    @staticmethod
    def load(path) -> "DetectionDoc":
        from app.localize.overlay.common import read_json
        return DetectionDoc.from_dict(read_json(path))


@dataclass
class TranslationEntry:
    source: str
    target: str                          # 채택 어미 미정 시 표준어+placeholder 가능
    notes: str = ""
    flagged: bool = False                # 네이티브 검수 필요 표시
    # 줄 단위 오버라이드(검수 반려 수정, docs/subtitle-style-overrides.md) — 렌더가
    # 이벤트에 전사한다(render.attach_entry_overrides). None = 오버라이드 없음.
    style: Optional[dict[str, Any]] = None       # {size, y, color, rotate}
    start_sec: Optional[float] = None            # 편집본 시간축 초
    end_sec: Optional[float] = None
    # 소프트 삭제(E6-0): False = 이 줄을 렌더(번인·ass/srt)에서 뺀다 — 검수함 자막 ✕.
    use: bool = True

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TranslationEntry":
        return TranslationEntry(
            source=d["source"],
            target=d.get("target", ""),
            notes=d.get("notes", ""),
            flagged=bool(d.get("flagged", False)),
            style=d.get("style") or None,
            start_sec=d.get("start_sec"),
            end_sec=d.get("end_sec"),
            use=d.get("use") is not False,       # 명시적 false 만 삭제(없음/None = 사용)
        )


@dataclass
class TranslationDoc:
    """translate.py 산출물 (outputs/{video_id}/translations.json). source 로 dedup."""
    video_id: str
    model: str
    draft: bool = True                   # 초벌 = 네이티브 검수 전
    entries: list[TranslationEntry] = field(default_factory=list)

    def as_map(self) -> dict[str, str]:
        # use=False(소프트 삭제, E6-0)는 tmap 에서 빠진다 — detections_to_events 가
        # tmap 에 없는 원문을 건너뛰므로 번인(replace)·ass/srt·ja_events 전부 그 줄이 빠진다.
        return {e.source: e.target for e in self.entries if e.use}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TranslationDoc":
        return TranslationDoc(
            video_id=d["video_id"],
            model=d.get("model", "unknown"),
            draft=bool(d.get("draft", True)),
            entries=[TranslationEntry.from_dict(e) for e in d.get("entries", [])],
        )

    def save(self, path) -> None:
        from app.localize.overlay.common import write_json
        write_json(self.to_dict(), path)

    @staticmethod
    def load(path) -> "TranslationDoc":
        from app.localize.overlay.common import read_json
        return TranslationDoc.from_dict(read_json(path))
