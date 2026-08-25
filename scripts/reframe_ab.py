"""L-P4 2차 관문 — 리프레임(얼굴검출) 회귀 A/B.

발주서: ves-orchestrator `docs/overlay_deps_runbook.md` §2.

**왜 단위 테스트로 안 되는가.** 회귀 가드(`test_e1*` 계열)는 얼굴검출 *좌표*를
고정하지만 *검출 자체*는 가짜 값으로 우회한다. L-P4 의존성 머지는 그 아래를 바꾼다:

    numpy  2.5.1  → 2.3.5      (deepface·opencv·faster-whisper 가 그 위에서 돈다)
    cv2    4.14.0 → 4.10.0     (opencv-contrib 로 통합 — haarcascade 도 그 배포판 것)

haar cascade 와 ArcFace 임베딩이 새 스택에서 **같은 답**을 내는지는 실물로만 보인다.

    python -m scripts.reframe_ab --job-dir <job> --out <dir>     # 한 판 뜬다
    python -m scripts.reframe_ab --diff <A> <B>                  # 두 판 대조

⚠ **두 판을 같은 job 으로, 두 venv 로 떠야 한다.** 저장된 `crop_*.json` 을 기준으로
삼지 않는 이유: 그 파일은 face_identifier·character_index 가 붙은 실런 산출이라
이 스크립트의 단순화된 호출과 조건이 다르다. 조건이 다른 둘을 맞대면 **코드 차이가
아닌 것**이 차이로 보인다(A/B 의 기본).

⚠ 운영 venv 는 노드가 갱신되는 순간 **새 스택이 된다.** A 판(구 스택)은 갱신 전에
떠야 한다 — 놓치면 구 requirements 로 venv 를 다시 만들어야 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CROP_FIELDS = ("time_sec", "x_center", "y_center", "crop_w", "crop_h")


# ─────────────────────────── 순수 (테스트 대상) ───────────────────────────

def face_track_clips(edit_plan: dict) -> list[dict]:
    """edit_plan timeline 에서 **얼굴추적 클립만** 순서대로. 순수.

    `mode == "center"` 는 `--no-reframe` 이거나 crop_map 에 없던 클립이라 검출이
    돌지 않았다 — 그런 클립까지 새로 검출하면 A/B 가 실런과 다른 것을 잰다."""
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


def compare_embeddings(a: dict, b: dict) -> dict:
    """{이름: [float,…]} 두 벌 → 최대 절대차. 순수.

    ArcFace 임베딩은 numpy 산술 위에 있어 **비트 단위로 같을 이유가 없다** —
    그래서 '같다/다르다'가 아니라 크기를 적는다(판정은 호출부 임계값)."""
    keys_a, keys_b = sorted(a), sorted(b)
    res = {"keys_a": keys_a, "keys_b": keys_b, "same_keys": keys_a == keys_b,
           "max_abs_delta": 0.0, "n": 0}
    for k in set(keys_a) & set(keys_b):
        va, vb = a[k] or [], b[k] or []
        if len(va) != len(vb):
            res["max_abs_delta"] = float("inf")
            continue
        res["n"] += 1
        for x, y in zip(va, vb):
            res["max_abs_delta"] = max(res["max_abs_delta"], abs(float(x) - float(y)))
    return res


# A/B 판정을 뒤집는 스택 축. 이 둘이 같으면 '두 판'이 아니라 '같은 판 두 번'이다.
STACK_KEYS = ("cv2", "numpy")


def stacks_differ(env_a: dict, env_b: dict) -> bool:
    """두 판이 실제로 다른 스택에서 떴는가. 순수 — 테스트 대상.

    운영 venv 는 노드가 갱신되는 순간 새 스택이 된다. 그걸 모르고 갱신 뒤에 A판을
    뜨면 **양쪽이 같은 스택**이라 무조건 `회귀 0` 이 나온다 — 가장 위험한 헛통과다."""
    return any(str(env_a.get(k)) != str(env_b.get(k)) for k in STACK_KEYS)


def verdict(crops: list[dict], emb: dict, *, emb_tol: float) -> tuple[bool, list[str]]:
    """회귀 0 인가 + 사유. 순수 — 조용한 통과를 막으려고 사유를 늘 돌려준다.

    임베딩을 **건너뛴 것은 실패가 아니다**(캐스트 사진이 없는 job 이 정상으로 있다) —
    대신 사유를 남겨 '봤는데 같았다'와 '아예 안 봤다'가 구분되게 한다."""
    reasons, ok = [], True
    bad = [c for c in crops if not c["cmp"]["identical"]]
    if not crops:
        ok = False
        reasons.append("대조한 크롭 타임라인이 0개다")
    if bad:
        ok = False
        reasons.append(f"크롭 타임라인 {len(bad)}/{len(crops)}개가 다르다")
    if emb.get("skipped"):
        reasons.append(f"임베딩 대조 없음({emb['skipped']})")
    elif not emb.get("same_keys"):
        ok = False
        reasons.append("임베딩 레퍼런스 목록이 다르다")
    elif emb.get("max_abs_delta", 0.0) > emb_tol:
        ok = False
        reasons.append(f"임베딩 최대차 {emb['max_abs_delta']:.2e} > 허용 {emb_tol:.0e}")
    return ok, reasons


# ─────────────────────────── IO ───────────────────────────

def _env() -> dict:
    info = {"python": sys.version.split()[0]}
    for mod in ("cv2", "numpy", "deepface"):
        try:
            m = __import__(mod)
            info[mod] = getattr(m, "__version__", "?")
            info[mod + "_path"] = getattr(m, "__file__", "?")
        except Exception as e:  # noqa: BLE001
            info[mod] = f"없음({type(e).__name__})"
    return info


def resolve_job_dir(p: Path) -> Path:
    """job 디렉토리로 정규화. 순수 — 테스트 대상.

    후보를 찾는 명령이 `grep -l … outputs/*/edit_plan.json` 이라 **파일 경로가 그대로
    붙여 넣어진다.** 그대로 두면 `…/edit_plan.json/checkpoint_probe.json` 을 열려다
    `NotADirectoryError` 로 죽는데, 그 메시지는 무엇을 잘못 줬는지 안 알려준다."""
    return p.parent if p.is_file() else p


def run_once(job: Path, out: Path, *, limit: int | None, frames: int = 0) -> None:
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

    # 실런과 같은 sticky anchor 승계 — 안 하면 클립 2번부터 조건이 실런과 달라진다.
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

    emb = _embeddings(job)
    if emb.get("skipped") and frames:
        print(f"  [임베딩] 캐스트 사진 경로 없음({emb['skipped']}) → 소스 프레임으로 간다")
        emb = _frame_embeddings(job, src, out, frames)
    (out / "manifest.json").write_text(json.dumps(
        {"job": str(job), "env": _env(), "clips": clips, "embeddings": emb},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[reframe_ab] 완료 → {out}")


def _represent(paths: list[tuple[str, Path]]) -> dict:
    """(이름, 이미지) → ArcFace 임베딩. 모델·검출기는 `FaceIdentifier` 것을 그대로 쓴다
    (운영과 다른 파라미터로 재면 운영을 안 재는 것이다)."""
    from deepface import DeepFace

    from app.modules.face_id import FaceIdentifier
    fi = FaceIdentifier()
    out = {}
    for name, img in paths:
        try:
            r = DeepFace.represent(img_path=str(img), model_name=fi.model_name,
                                   detector_backend=fi.detector_backend,
                                   enforce_detection=False)
            if r:
                out[name] = [float(x) for x in r[0]["embedding"]]
        except Exception as e:  # noqa: BLE001
            print(f"  [임베딩] {name} 실패: {type(e).__name__} {e}")
    return out


def _frame_embeddings(job: Path, src: Path, out: Path, n: int) -> dict:
    """소스에서 프레임을 뽑아 임베딩. 캐스트 사진이 사라진 job(대부분)의 대안이다.

    ⚠ 뽑는 시각은 **방금 만든 크롭 키프레임 시각**이라 얼굴이 있는 것이 보장된다
    (haar 가 거기서 얼굴을 찾았다). 두 판이 같은 ffmpeg 로 같은 시각을 뽑으므로
    프레임은 동일하고, 차이가 나면 그건 deepface·numpy 차이다."""
    import subprocess

    from app.modules.ffmpeg_utils import find_ffmpeg_command
    ff = find_ffmpeg_command()
    times: list[float] = []
    for f in sorted(out.glob("crop_*.json")):
        kfs = json.loads(f.read_text(encoding="utf-8"))
        if kfs:
            times.append(float(kfs[len(kfs) // 2].get("time_sec", 0.0)))
    times = times[:n]
    if not times:
        return {"skipped": "키프레임이 없어 뽑을 시각이 없다"}

    frames_dir = out / "frames"
    frames_dir.mkdir(exist_ok=True)
    pairs = []
    for t in times:
        img = frames_dir / f"f_{t:.3f}.png"
        r = subprocess.run([ff, "-nostdin", "-loglevel", "error", "-ss", f"{t:.3f}",
                            "-i", str(src), "-frames:v", "1", "-y", str(img)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and img.exists():
            pairs.append((f"frame@{t:.3f}", img))
        else:
            print(f"  [임베딩] 프레임 추출 실패 t={t:.3f}: {(r.stderr or '').strip()[:200]}")
    if not pairs:
        return {"skipped": "프레임을 하나도 못 뽑았다"}
    print(f"  [임베딩] 소스 프레임 {len(pairs)}장으로 대조")
    return {"source": "frames", "vectors": _represent(pairs)}


def _embeddings(job: Path) -> dict:
    """캐스트 사진 → ArcFace 임베딩. 없으면 사유를 담아 건너뛴다(조용한 생략 금지)."""
    rp = job / "checkpoint_research.json"
    if not rp.exists():
        return {"skipped": "checkpoint_research.json 없음"}
    cast = (json.loads(rp.read_text(encoding="utf-8")) or {}).get("cast_images") or []
    have = [c for c in cast if c.get("image_path") and Path(c["image_path"]).exists()]
    if not have:
        return {"skipped": f"쓸 수 있는 캐스트 사진 0장(항목 {len(cast)}개)"}
    try:
        from app.modules.face_id import FaceIdentifier
        fi = FaceIdentifier()
    except Exception as e:  # noqa: BLE001
        return {"skipped": f"FaceIdentifier 초기화 실패: {type(e).__name__} {e}"}

    class _C:
        def __init__(self, d):
            self.character_name = d.get("character_name", "")
            self.actor_name = d.get("actor_name", "")
            self.image_path = d.get("image_path")

    fi.build_references([_C(c) for c in have])
    return {"source": "cast",
            "vectors": {f"{r.actor_name}|{r.character_name}": [float(x) for x in r.embedding]
                        for r in fi.references}}


def do_diff(a: Path, b: Path, *, emb_tol: float, allow_same_stack: bool = False) -> int:
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
        print("   구 스택을 다시 만들어 A 판을 뜰 것(런북 §2-3). 그래도 돌리려면 "
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

    print("\n=== ArcFace 임베딩 ===")
    ea, eb = ma.get("embeddings") or {}, mb.get("embeddings") or {}
    if ea.get("skipped") or eb.get("skipped"):
        emb = {"skipped": ea.get("skipped") or eb.get("skipped")}
        print(f"  건너뜀: {emb['skipped']}")
    elif ea.get("source") != eb.get("source"):
        # 한쪽은 캐스트 사진, 다른 쪽은 프레임이면 재는 대상이 아예 다르다.
        emb = {"same_keys": False}
        print(f"  🛑 대조 대상이 다르다: A {ea.get('source')} · B {eb.get('source')}")
    else:
        emb = compare_embeddings(ea.get("vectors") or {}, eb.get("vectors") or {})
        print(f"  레퍼런스 {len(emb['keys_a'])}/{len(emb['keys_b'])}개 · "
              f"최대 절대차 {emb['max_abs_delta']:.3e} (허용 {emb_tol:.0e})")

    ok, reasons = verdict(crops, emb, emb_tol=emb_tol)
    print("\n=== 판정 ===")
    if ok and not reasons:
        print("  ✅ 회귀 0")
    elif ok:
        print("  ✅ 회귀 0 (단서: " + " · ".join(reasons) + ")")
    else:
        for r in reasons:
            print(f"  🛑 {r}")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="리프레임 얼굴검출 A/B — 노드에서 돌린다")
    ap.add_argument("--job-dir", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--clips", type=int, default=None, help="앞에서 N개만(빠른 확인용)")
    ap.add_argument("--frames", type=int, default=0, metavar="N",
                    help="캐스트 사진이 없으면 소스 프레임 N장으로 임베딩을 대조한다")
    ap.add_argument("--diff", nargs=2, type=Path, metavar=("A", "B"))
    # 임베딩은 부동소수 산술이라 비트 동일을 요구하지 않는다. 1e-4 는 ArcFace 512차
    # 벡터의 코사인 판정(임계 0.4)을 뒤집기에 한참 모자란 크기다.
    ap.add_argument("--emb-tol", type=float, default=1e-4)
    ap.add_argument("--allow-same-stack", action="store_true",
                    help="같은 스택 두 판도 대조한다(결정성 확인 등 — A/B 아님)")
    args = ap.parse_args()

    if args.diff:
        raise SystemExit(do_diff(args.diff[0], args.diff[1], emb_tol=args.emb_tol,
                                 allow_same_stack=args.allow_same_stack))
    if not (args.job_dir and args.out):
        ap.error("--job-dir 과 --out 을 함께 주거나 --diff A B 를 줄 것")
    run_once(args.job_dir, args.out, limit=args.clips, frames=args.frames)


if __name__ == "__main__":
    main()
