"""[엔진②-b] 인페인팅(배경 복원) 백엔드 팩토리.

검증된 오픈소스 모델을 *라이브러리로* 쓴다(스크래치 구현 금지).
  opencv     : cv2.inpaint Telea — 가중치 불필요, 무설치 폴백(품질 낮음, 파이프라인 검증용).
  lama       : simple-lama-inpainting (Apache-2.0) — 이미지/애니에 우수, 상업 사용 안전.
  sttn       : 실사 영상에 강함 — 저장소+가중치 연동 필요(weights/sttn/).
  propainter : 고움직임에 강함 — ⚠ S-Lab NON-COMMERCIAL 가능. 상업 사용 전 확인.
               config inpaint.propainter_commercial_ack=true 없으면 차단.

콘텐츠 타입 → 모드 매핑은 config.inpaint.mode_by_content. 원본 덮어쓰기 금지(out_dir 별도).
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import ensure_dir, get_logger, resolve_path, write_json

log = get_logger("inpaint")

PROPAINTER_LICENSE_WARNING = (
    "⚠ ProPainter 는 S-Lab 등 비상업/연구용 라이선스일 수 있습니다. "
    "상업 채널 사용 전 저장소·가중치 라이선스를 반드시 확인하고, "
    "확인 후 config inpaint.propainter_commercial_ack=true 로 명시하세요."
)


# ── 백엔드 계약 ───────────────────────────────────────────────────────────
class InpaintBackend:
    name = "base"
    needs_weights = False

    def inpaint_image(self, image_bgr, mask):  # pragma: no cover - 추상
        raise NotImplementedError

    def inpaint_sequence(self, frames_dir: Path, masks_dir: Path, out_dir: Path,
                         margin: int = 24) -> Path:
        """기본: 프레임별 inpaint_image 루프(이미지 기반 백엔드용).

        마스크의 bounding box + 여백 영역만 인페인트하고 원위치에 합성한다.
        전체 프레임 대비 모델 입력이 작아져 속도가 크게 빨라지고(특히 LaMa),
        모델이 텍스트 영역에 집중해 품질도 좋아진다. 마스크 없는 프레임은 복사.
        """
        import cv2
        import numpy as np

        out = ensure_dir(out_dir)
        frames = sorted(Path(frames_dir).glob("*.png"))
        done = 0
        for fp in frames:
            mp = Path(masks_dir) / fp.name
            img = cv2.imread(str(fp))
            if img is None:                      # 깨진/판독 불가 프레임 → 건너뜀(시퀀스 보호)
                log.warning("프레임 판독 실패 → 건너뜀: %s", fp)
                continue
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE) if mp.exists() else None
            if mask is None or not mask.any():
                cv2.imwrite(str(out / fp.name), img)  # 마스크 없으면 그대로
                continue
            ys, xs = np.where(mask > 0)
            x1, y1, x2, y2 = crop_bounds(int(xs.min()), int(ys.min()), int(xs.max()),
                                         int(ys.max()), margin, img.shape[1], img.shape[0])
            patch = self.inpaint_image(img[y1:y2, x1:x2].copy(), mask[y1:y2, x1:x2])
            img[y1:y2, x1:x2] = patch[:y2 - y1, :x2 - x1]   # 백엔드 패딩 방어(크기 보정)
            cv2.imwrite(str(out / fp.name), img)
            done += 1
        log.info("[%s] %d/%d 프레임 인페인팅(영역 크롭) → %s", self.name, done, len(frames), out)
        return out


class OpenCVBackend(InpaintBackend):
    name = "opencv"

    def __init__(self, radius: int = 3) -> None:
        self.radius = radius

    def inpaint_image(self, image_bgr, mask):
        import cv2

        return cv2.inpaint(image_bgr, mask, self.radius, cv2.INPAINT_TELEA)


class LamaBackend(InpaintBackend):
    name = "lama"
    needs_weights = True

    def __init__(self) -> None:
        try:
            from simple_lama_inpainting import SimpleLama
        except ImportError as e:
            raise ImportError(
                "lama 백엔드: pip install simple-lama-inpainting (Apache-2.0). "
                "최초 실행 시 가중치 자동 다운로드 — 라이선스 확인 권장."
            ) from e
        self._lama = SimpleLama()

    def inpaint_image(self, image_bgr, mask):
        import cv2
        import numpy as np
        from PIL import Image

        rgb = Image.fromarray(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))
        m = Image.fromarray((mask > 0).astype("uint8") * 255)
        result = self._lama(rgb, m)
        res = cv2.cvtColor(np.array(result), cv2.COLOR_RGB2BGR)
        h, w = image_bgr.shape[:2]          # LaMa 는 8 배수로 패딩 → 입력 크기로 되돌림
        return res[:h, :w]


class STTNBackend(InpaintBackend):
    name = "sttn"
    needs_weights = True

    def __init__(self, weights_dir: Path) -> None:
        self.weights_dir = weights_dir
        # TODO(라이선스): STTN 저장소/가중치 라이선스 확인 후 어댑터 연동.
        raise NotImplementedError(
            "STTN 백엔드 미연동. 검증된 STTN 구현을 vendor 하거나 PYTHONPATH 에 두고, "
            f"가중치를 {weights_dir} 에 배치한 뒤 이 어댑터에서 호출하도록 연결하세요. "
            "(실사 영상=먹방에 권장. 가중치 라이선스 확인 필수)"
        )


class ProPainterBackend(InpaintBackend):
    name = "propainter"
    needs_weights = True

    def __init__(self, weights_dir: Path) -> None:
        self.weights_dir = weights_dir
        # TODO(라이선스): ProPainter 상업 라이선스 확인. 가드는 make_inpainter 에서.
        raise NotImplementedError(
            "ProPainter 백엔드 미연동. 고움직임 컷 한정. " + PROPAINTER_LICENSE_WARNING
        )


_BACKENDS = {
    "opencv": OpenCVBackend,
    "lama": LamaBackend,
    "sttn": STTNBackend,
    "propainter": ProPainterBackend,
}


# ── 순수 디스패치 (의존성 없음 → 테스트) ─────────────────────────────────
def crop_bounds(x_min: int, y_min: int, x_max: int, y_max: int,
                margin: int, width: int, height: int) -> tuple[int, int, int, int]:
    """마스크 bbox + 여백을 프레임 경계로 클램프한 크롭 좌표 (x1,y1,x2,y2)."""
    return (max(0, x_min - margin), max(0, y_min - margin),
            min(width, x_max + margin), min(height, y_max + margin))


def select_backend(content_type: Optional[str], config: dict[str, Any]) -> str:
    """콘텐츠 타입 → 인페인트 백엔드 이름. (mukbang→sttn, anime→lama 등)"""
    icfg = config.get("inpaint", {})
    mode_map = icfg.get("mode_by_content", {})
    if content_type and content_type in mode_map:
        return mode_map[content_type]
    return mode_map.get("default") or icfg.get("default_backend", "opencv")


def _check_license(name: str, config: dict[str, Any]) -> None:
    if name == "propainter" and not config.get("inpaint", {}).get("propainter_commercial_ack", False):
        raise RuntimeError(PROPAINTER_LICENSE_WARNING)


def make_inpainter(name: str, config: dict[str, Any]) -> InpaintBackend:
    if name not in _BACKENDS:
        raise ValueError(f"알 수 없는 인페인트 백엔드: {name} (가능: {list(_BACKENDS)})")
    _check_license(name, config)
    cls = _BACKENDS[name]
    weights_dir = resolve_path(config.get("paths", {}).get("weights_dir", "weights")) / name
    if name in ("sttn", "propainter"):
        return cls(weights_dir)  # type: ignore[call-arg]
    if name == "opencv":
        return cls(radius=int(config.get("inpaint", {}).get("opencv_radius", 3)))
    return cls()


# ── 오케스트레이션 ───────────────────────────────────────────────────────
def inpaint(frames_dir: str, masks_dir: str, out_dir: str, config: dict[str, Any],
            backend_name: Optional[str] = None, content_type: Optional[str] = None) -> Path:
    """프레임+마스크 → 인페인팅된 프레임 시퀀스. inpaint_log.json 기록."""
    name = backend_name or select_backend(content_type, config)
    if name == "propainter":
        log.warning(PROPAINTER_LICENSE_WARNING)
    backend = make_inpainter(name, config)
    out = backend.inpaint_sequence(Path(frames_dir), Path(masks_dir), ensure_dir(out_dir))

    log_path = Path(out).parent / "inpaint_log.json"
    write_json({"backend": name, "content_type": content_type,
                "frames_dir": str(frames_dir), "masks_dir": str(masks_dir),
                "out_dir": str(out), "needs_weights": backend.needs_weights}, log_path)
    return out


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="텍스트 제거(인페인팅)")
    p.add_argument("--frames", required=True)
    p.add_argument("--masks", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--backend", default=None, help="opencv|lama|sttn|propainter")
    p.add_argument("--content-type", default=None, help="mukbang|anime|high_motion")
    p.add_argument("--config", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    from app.localize.overlay.common import load_config

    args = _parse_args(argv)
    config = load_config(args.config)
    inpaint(args.frames, args.masks, args.out, config,
            backend_name=args.backend, content_type=args.content_type)


if __name__ == "__main__":
    main()
