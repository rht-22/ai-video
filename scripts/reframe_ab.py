"""리프레임(얼굴검출) 회귀 A/B — 같은 job 을 두 스택으로 떠서 크롭을 맞댄다.

발주서: ves-orchestrator `docs/overlay_deps_runbook.md` §2-3.

**왜 단위 테스트로 안 되는가.** 회귀 가드(`test_e1*` 계열)는 얼굴검출 *좌표*를
고정하지만 *검출 자체*는 가짜 값으로 우회한다. 의존성이 바뀌면(L-P4: numpy
2.5→2.3 · cv2 4.14→4.10) haar cascade 가 같은 답을 내는지는 실물로만 보인다.

    python -m scripts.reframe_ab --job-dir <job> --out <dir>     # 한 판 뜬다
    python -m scripts.reframe_ab --diff <A> <B>                  # 두 판 대조

⚠ **두 판을 같은 job 으로, 두 venv 로 떠야 한다.** 저장된 `crop_*.json` 을 기준으로
삼지 않는다 — 그 파일은 실런 산출이라 이 도구의 호출과 조건이 미묘하게 다르고,
조건이 다른 둘을 맞대면 **코드 차이가 아닌 것**이 차이로 보인다(A/B 의 기본).

⚠ 운영 venv 는 노드가 갱신되는 순간 새 스택이 된다. 구 스택 판을 놓쳤으면 그때의
requirements 로 일회용 venv 를 다시 만들면 된다(`scripts/deps_probe.py --install`).

⚠ **얼굴 인식(deepface)은 2026-08-25 에 사라졌다** — 이 도구도 임베딩 대조를 함께
   걷어냈다. 남은 축은 haar 검출 하나이고, 그것이 지금 실제로 도는 유일한 경로다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CROP_FIELDS = ("time_sec", "x_center", "y_center", "crop_w", "crop_h")

# A/B 판정을 뒤집는 스택 축. 이 둘이 같으면 '두 판'이 아니라 '같은 판 두 번'이다.
STACK_KEYS = ("cv2", "numpy")


# ─────────────────────────── 순수 (테스트 대상) ───────────────────────────

def resolve_job_dir(p: Path) -> Path:
    """job 디렉토리로 정규화. 순수 — 테스트 대상.

    후보를 찾는 명령이 `grep -l … outputs/*/edit_plan.json` 이라 **파일 경로가 그대로
    붙여 넣어진다.** 그대로 두면 `…/edit_plan.json/checkpoint_probe.json` 을 열려다
    `NotADirectoryError` 로 죽는데, 그 메시지는 무엇을 잘못 줬는지 안 알려준다."""
    return p.parent if p.is_file() else p


def face_track_clips(edit_plan: dict) -> list[dict]:
    """edit_plan timeline 에서 **얼굴추적 클립만** 순서대로. 순수.

    `mode == "center"` 는 `--no-reframe` 이거나 crop_map 에 없던 클립이라 검출이
    돌지 않았다 — 그런 클립까지 새로 검출하면 A/B 가 실런과 다른 것을 잰다.
    crop 파일 이름이 `{role}_{원본 idx}` 라 center 를 건너뛰어도 **원본 idx** 를 쓴다."""
    out = []
    for idx, c in enumerate(edit_plan.get("timeline", []) or []):
        if ((c.get("reframe") or {}).get("mode")) != "face_track":
            continue
        out.append({"idx": idx, "role": c.get("role", "clip"),
                    "start_sec": float(c.get("clip_start_sec", 0.0)),
                    "end_sec": float(c.get("clip_end_sec", 0.0))})
    return out


def compare_keyframes(a: list, b: list) -> dict:
    """키프레임 두 벌 → 필드별 최대 절대차. 순수.

    길이가 다르면 그것부터 적는다 — 검출 개수가 달라지면 필드 비교는 자리부터
    어긋나 숫자가 무의미해진다."""
    res = {"n_a": len(a), "n_b": len(b), "same_len": len(a) == len(b), "max_delta": {}}
    for f in CROP_FIELDS:
        worst = 0.0
        for ka, kb in zip(a, b):
            try:
                worst = max(worst, abs(float(ka.get(f, 0)) - float(kb.get(f, 0))))
            except (TypeError, ValueError):
                worst = float("inf")
        res["max_delta"][f] = worst
    res["identical"] = res["same_len"] and all(v == 0.0 for v in res["max_delta"].values())
    return res


def stacks_differ(env_a: dict, env_b: dict) -> bool:
    """두 판이 실제로 다른 스택에서 떴는가. 순수 — 테스트 대상.

    운영 venv 는 노드가 갱신되는 순간 새 스택이 된다. 그걸 모르고 갱신 뒤에 A판을
    뜨면 **양쪽이 같은 스택**이라 무조건 `회귀 0` 이 나온다 — 가장 위험한 헛통과다."""
    return any(str(env_a.get(k)) != str(env_b.get(k)) for k in STACK_KEYS)


def verdict(crops: list[dict]) -> tuple[bool, list[str]]:
    """회귀 0 인가 + 사유. 순수 — 조용한 통과를 막으려고 사유를 늘 돌려준다."""
    reasons, ok = [], True
    bad = [c for c in crops if not c["cmp"]["identical"]]
    if not crops:
        ok = False
        reasons.append("대조한 크롭 타임라인이 0개다")
    if bad:
        ok = False
        reasons.append(f"크롭 타임라인 {len(bad)}/{len(crops)}개가 다르다")
    return ok, reasons


# ─────────────────────────── IO ───────────────────────────

def _env() -> dict:
    info = {"python": sys.version.split()[0]}
    for mod in ("cv2", "numpy"):
        try:
            m = __import__(mod)
            info[mod] = getattr(m, "__version__", "?")
            info[mod + "_path"] = getattr(m, "__file__", "?")
        except Exception as e:  # noqa: BLE001
            info[mod] = f"없음({type(e).__name__})"
    return info


def run_once(job: Path, out: Path, *, limit: int | None) -> None:
    from app.modules.reframe import build_crop_timeline

    resolved = resolve_job_dir(job)
    if resolved != job:
        print(f"[reframe_ab] 파일이 주어져 job 디렉토리로 읽는다: {resolved}")
    job = resolved
    probe_path = job / "checkpoint_probe.json"
    if not probe_path.exists():
        raise SystemExit(f"job 디렉토리가 아니다(체크포인트 없음): {job}\n"
                         f"  outputs/<작품_해시>/ 를 줄 것")
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    plan = json.loads((job / "edit_plan.json").read_text(encoding="utf-8"))
    src = Path(probe["path"])
    if not src.exists():
        raise FileNotFoundError(f"소스 영상이 없다: {src}")
    clips = face_track_clips(plan)
    if limit:
        clips = clips[:limit]
    if not clips:
        raise SystemExit("얼굴추적 클립이 없다 — 리프레임이 켜진 job 을 고를 것")

    out.mkdir(parents=True, exist_ok=True)
    print(f"[reframe_ab] {job.name} · 얼굴추적 클립 {len(clips)}개 · "
          f"{src.name} {probe['width']}x{probe['height']}")

    # 실런과 같은 anchor 승계 — 안 하면 클립 2번부터 조건이 실런과 달라진다.
    ax = ay = None
    for c in clips:
        dst = out / f"crop_{c['role']}_{c['idx']}.json"
        build_crop_timeline(
            src, dst, int(probe["width"]), int(probe["height"]), 1.0,
            start_sec=c["start_sec"], end_sec=c["end_sec"],
            enable_speaker_tracking=True,
            initial_x=ax, initial_y=ay,
        )
        kfs = json.loads(dst.read_text(encoding="utf-8"))
        if kfs:
            ax, ay = float(kfs[-1].get("x_center", 0.0)), float(kfs[-1].get("y_center", 0.0))
        print(f"  {dst.name}: 키프레임 {len(kfs)}개")

    (out / "manifest.json").write_text(json.dumps(
        {"job": str(job), "env": _env(), "clips": clips},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[reframe_ab] 완료 → {out}")


def do_diff(a: Path, b: Path, *, allow_same_stack: bool = False) -> int:
    ma = json.loads((a / "manifest.json").read_text(encoding="utf-8"))
    mb = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    print("=== 환경 ===")
    for k in sorted(set(ma["env"]) | set(mb["env"])):
        if k.endswith("_path"):
            continue
        va, vb = ma["env"].get(k, "-"), mb["env"].get(k, "-")
        print(f"  {k:10s} A {va}   B {vb}{'' if va == vb else '   ← 다름'}")

    if not stacks_differ(ma["env"], mb["env"]) and not allow_same_stack:
        print("\n🛑 두 판의 cv2·numpy 가 같다 — 이건 A/B 가 아니라 같은 판 두 번이다.")
        print("   구 스택으로 한 판을 다시 뜰 것(런북 §2-3). 그래도 돌리려면 "
              "--allow-same-stack.")
        return 2

    print("\n=== 크롭 타임라인 ===")
    crops = []
    names = sorted({p.name for p in a.glob("crop_*.json")} | {p.name for p in b.glob("crop_*.json")})
    for n in names:
        fa, fb = a / n, b / n
        if not (fa.exists() and fb.exists()):
            print(f"  🛑 {n}: 한쪽에만 있다 (A={fa.exists()} B={fb.exists()})")
            crops.append({"name": n, "cmp": {"identical": False}})
            continue
        cmp = compare_keyframes(json.loads(fa.read_text(encoding="utf-8")),
                                json.loads(fb.read_text(encoding="utf-8")))
        crops.append({"name": n, "cmp": cmp})
        mark = "동일" if cmp["identical"] else "🛑 다름"
        d = cmp["max_delta"]
        print(f"  {n}: {mark} · 키프레임 {cmp['n_a']}/{cmp['n_b']} · "
              f"x {d['x_center']:.4g} y {d['y_center']:.4g} "
              f"w {d['crop_w']:.4g} h {d['crop_h']:.4g}")

    ok, reasons = verdict(crops)
    print("\n=== 판정 ===")
    if ok:
        print("  ✅ 회귀 0")
    else:
        for r in reasons:
            print(f"  🛑 {r}")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="리프레임 얼굴검출 A/B — 노드에서 돌린다")
    ap.add_argument("--job-dir", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--clips", type=int, default=None, help="앞에서 N개만(빠른 확인용)")
    ap.add_argument("--diff", nargs=2, type=Path, metavar=("A", "B"))
    ap.add_argument("--allow-same-stack", action="store_true",
                    help="같은 스택 두 판도 대조한다(결정성 확인 등 — A/B 아님)")
    args = ap.parse_args()

    if args.diff:
        raise SystemExit(do_diff(args.diff[0], args.diff[1],
                                 allow_same_stack=args.allow_same_stack))
    if not (args.job_dir and args.out):
        ap.error("--job-dir 과 --out 을 함께 주거나 --diff A B 를 줄 것")
    run_once(args.job_dir, args.out, limit=args.clips)


if __name__ == "__main__":
    main()
