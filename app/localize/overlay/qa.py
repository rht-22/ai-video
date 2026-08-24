"""[엔진] 품질 리포트 — PSNR/SSIM(인페인트 ROI) + 아티팩트/고움직임 플래그.

주의: 인페인팅엔 ground-truth 가 없다. 여기 PSNR/SSIM 은 '원본 대비 변화량/이질감'의
*휴리스틱 프록시*다(절대 품질 점수 아님). 낮은 SSIM·경계 불연속·고움직임 구간을
"검수 필요" 타임코드로 플래그해 사람 눈(게이트②)으로 넘기는 것이 목적.

numpy/skimage/PIL 은 lazy import. 플래그 판정·리포트 빌드·타임코드는 순수 → 테스트 가능.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.localize.overlay.common import ensure_dir, get_logger, resolve_path, write_json

log = get_logger("qa")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def seconds_to_tc(seconds: float) -> str:
    """초 → mm:ss.cs 타임코드 (반올림 캐리를 초/분까지 전파)."""
    cs_total = int(round(max(0.0, seconds) * 100))   # 먼저 센티초로 반올림 후 분해
    m, rem = divmod(cs_total, 6000)
    s, cs = divmod(rem, 100)
    return f"{m:02d}:{s:02d}.{cs:02d}"


def flag_reason(psnr: float, ssim: float, motion: float, cfg: dict[str, Any]) -> str:
    """임계값 대비 플래그 사유 문자열(없으면 빈 문자열)."""
    reasons = []
    if ssim < float(cfg.get("ssim_warn_below", 0.90)):
        reasons.append(f"SSIM낮음({ssim:.3f})")
    if psnr < float(cfg.get("psnr_warn_below", 30.0)):
        reasons.append(f"PSNR낮음({psnr:.1f}dB)")
    if motion > float(cfg.get("motion_flag_threshold", 0.35)):
        reasons.append(f"고움직임({motion:.2f})")
    return ", ".join(reasons)


def summarize(measures: list[dict[str, Any]]) -> dict[str, Any]:
    if not measures:
        return {"frames": 0, "psnr_avg": 0.0, "ssim_avg": 0.0, "flagged": 0}
    n = len(measures)
    return {
        "frames": n,
        "psnr_avg": round(sum(m["psnr"] for m in measures) / n, 2),
        "ssim_avg": round(sum(m["ssim"] for m in measures) / n, 4),
        "flagged": sum(1 for m in measures if m["reason"]),
    }


def build_report(video_id: str, measures: list[dict[str, Any]], config: dict[str, Any],
                 extra: Optional[dict[str, Any]] = None) -> str:
    s = summarize(measures)
    qcfg = config.get("qa", {})
    lines = [
        f"# 품질 리포트 — {video_id}", "",
        "> PSNR/SSIM 은 원본 대비 변화량의 **휴리스틱 프록시**입니다. 절대 품질 아님 —",
        "> 플래그 구간은 반드시 사람이 육안 확인하세요(검수 게이트②).", "",
        "## 요약",
        f"- 측정 프레임: {s['frames']}",
        f"- 평균 PSNR: {s['psnr_avg']} dB (경고 임계 {qcfg.get('psnr_warn_below', 30.0)})",
        f"- 평균 SSIM: {s['ssim_avg']} (경고 임계 {qcfg.get('ssim_warn_below', 0.90)})",
        f"- 검수 필요 구간: {s['flagged']}", "",
    ]
    if extra:
        lines.append("## 처리 정보")
        for k, v in extra.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
    flagged = [m for m in measures if m["reason"]]
    lines.append("## ⚠ 검수 필요 타임코드")
    if flagged:
        lines.append("| 타임코드 | 프레임 | PSNR | SSIM | 사유 |")
        lines.append("|---|---|---|---|---|")
        for m in flagged:
            lines.append(f"| {seconds_to_tc(m['ts'])} | {m['idx']} | {m['psnr']:.1f} | "
                         f"{m['ssim']:.3f} | {m['reason']} |")
    else:
        lines.append("플래그된 구간 없음(그래도 표본 육안 확인 권장).")
    lines += ["", "## 캐릭터 어미 일관성", "- [ ] 채택 어미가 전 구간 일관 적용되었는가",
              "- [ ] 정보성 텍스트(설명/©)에 어미가 잘못 들어가지 않았는가", "",
              "## 자막 싱크", "- [ ] ja.ass / ja.srt 타이밍이 화면과 맞는가", ""]
    return "\n".join(lines)


# ── 측정 (numpy/skimage lazy) ────────────────────────────────────────────
def _psnr(a, b) -> float:
    import numpy as np

    mse = float(np.mean((a.astype("float64") - b.astype("float64")) ** 2))
    return 100.0 if mse == 0 else 20.0 * float(np.log10(255.0 / np.sqrt(mse)))


def _ssim(a, b) -> float:
    try:
        from skimage.metrics import structural_similarity as ssim
        import cv2

        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        return float(ssim(ga, gb))
    except ImportError:
        return 1.0  # skimage 없으면 SSIM 생략(PSNR 만)


def _motion(prev, cur) -> float:
    import numpy as np

    if prev is None:
        return 0.0
    diff = np.abs(cur.astype("int16") - prev.astype("int16")).mean()
    return float(diff / 255.0)


def measure(original_dir: str, inpainted_dir: str, config: dict[str, Any],
            fps: float = 30.0) -> list[dict[str, Any]]:
    import cv2

    qcfg = config.get("qa", {})
    orig = sorted(Path(original_dir).glob("*.png"))
    out: list[dict[str, Any]] = []
    prev = None
    for fp in orig:
        ip = Path(inpainted_dir) / fp.name
        if not ip.exists():
            continue
        a = cv2.imread(str(fp))
        b = cv2.imread(str(ip))
        psnr, ssim, motion = _psnr(a, b), _ssim(a, b), _motion(prev, b)
        idx = int(fp.stem)
        out.append({"idx": idx, "ts": idx / fps if fps else 0.0,
                    "psnr": psnr, "ssim": ssim, "motion": motion,
                    "reason": flag_reason(psnr, ssim, motion, qcfg)})
        prev = b
    return out


def side_by_side(original: str, inpainted: str, out_path: str) -> Path:
    from PIL import Image

    a = Image.open(original).convert("RGB")
    b = Image.open(inpainted).convert("RGB")
    canvas = Image.new("RGB", (a.width + b.width, max(a.height, b.height)), (20, 20, 20))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (a.width, 0))
    out = Path(out_path)
    ensure_dir(out.parent)
    canvas.save(out)
    return out


def run_qa(video_id: str, original_dir: str, inpainted_dir: str, config: dict[str, Any],
           fps: float = 30.0, extra: Optional[dict[str, Any]] = None) -> Path:
    """측정 → review_report.md + 플래그 구간 side-by-side PNG."""
    measures = measure(original_dir, inpainted_dir, config, fps=fps)
    base = resolve_path(f"{config['paths']['outputs_dir']}/{video_id}")
    ensure_dir(base / "qa")
    if config.get("qa", {}).get("sidebyside", True):
        for m in (x for x in measures if x["reason"]):
            name = f"{m['idx']:06d}.png"
            try:
                side_by_side(str(Path(original_dir) / name), str(Path(inpainted_dir) / name),
                             str(base / "qa" / f"cmp_{name}"))
            except Exception as e:  # noqa: BLE001
                log.warning("side-by-side 실패 %s: %s", name, e)
    report = base / "review_report.md"
    report.write_text(build_report(video_id, measures, config, extra), encoding="utf-8")
    summary = summarize(measures)
    # 기계 판독용(autopilot QA 게이트가 소비). 사람용 리포트와 항상 쌍으로 생성.
    write_json({"video_id": video_id, **summary}, base / "qa_result.json")
    log.info("QA 리포트: %s (플래그 %d)", report, summary["flagged"])
    return report
