"""Render saved narration targets without another model call."""
from __future__ import annotations

import json
import math

from app.v3.safe_zone import SAFE_X0, SAFE_X1


def resolve(decision, *, src_size, picture, aspect_ratio, band_width=1080, canvas_width=1080):
    """A fixed crop must contain the full target inside the horizontal safe area.

    If the target cannot fit (two people far apart, wide action, uncertain
    observation), request source reselection. Never clamp blindly
    and silently cut off the selected person at the source boundary.
    """
    from app.v3.finalize import band_crop_size
    geo = band_crop_size(aspect_ratio, src_size, picture)
    if not geo:
        return {"mode": "needs_review", "reason": "크롭 기하 없음"}
    cw, ch, px, py, pw, ph = geo
    box = decision.get("target_box")
    valid = (isinstance(box, list) and len(box) == 4
             and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box)
             and box[0] < box[2] and box[1] < box[3])
    if decision.get("mode") != "focus" or not valid:
        return {"mode": "needs_review", "reason": "핵심 영역 재선택 필요"}
    sw, sh = src_size
    x0, y0, x1, y1 = box[0]*sw, box[1]*sh, box[2]*sw, box[3]*sh
    pad = (canvas_width-band_width)/2
    left = max(0., (SAFE_X0-pad)/band_width)
    right = min(1., (SAFE_X1-pad)/band_width)
    # Feasible crop-center interval: picture bounds AND full target bounds.
    lo = max(px+cw/2, x1+cw/2-right*cw)
    hi = min(px+pw-cw/2, x0+cw/2-left*cw)
    ylo = max(py+ch/2, y1-ch/2)
    yhi = min(py+ph-ch/2, y0+ch/2)
    if lo > hi or ylo > yhi:
        return {"mode": "needs_review", "reason": "대상 전체가 안전 구역에 들어가지 않음"}
    return {"mode": "focus", "x_center": min(max((x0+x1)/2, lo), hi),
            "y_center": min(max((y0+y1)/2, ylo), yhi), "crop_w": cw, "crop_h": ch}


def apply(timeline, clips, crop_map, *, output_dir, src_size, picture, design, log=print):
    """Apply after automatic zooms; explicit manual reframes always win."""
    from dataclasses import replace
    audit = []
    for i, c in enumerate(timeline):
        d = c.get("narration_framing")
        if not d or c.get("reframe"):
            continue
        r = resolve(d, src_size=src_size, picture=picture, aspect_ratio=design.aspect_ratio,
                    band_width=design.video_width or 1080)
        key = f"{c.get('role') or 'build'}_{i}"
        # A saved focus target overrides automatic information-screen fits too.
        clips[i] = replace(clips[i], fit_picture=None)
        if r["mode"] == "focus":
            p = output_dir / f"v3_crop_narration_{i}.json"
            kf = {k: r[k] for k in ("x_center", "y_center", "crop_w", "crop_h")}
            p.write_text(json.dumps([{**kf, "time_sec": c["clip_start_sec"]},
                                    {**kf, "time_sec": c["clip_end_sec"]}]), encoding="utf-8")
            crop_map[key] = p
        else:
            # Inspection previews retain the existing filled crop. Strict jobs
            # reject unresolved targets before rendering; no silent wide fallback.
            log(f"  [v3/구도] ⚠ clip{i} 재선택 필요: {r['reason']}")
        audit.append({"clip": i, "target": d.get("target"), **r})
        log(f"  [v3/덮개 구도] clip{i} {r['mode']} — {d.get('target', '')}")
    return audit
