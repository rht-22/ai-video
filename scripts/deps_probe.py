"""L-P0 — 의존성 실측 도구 (맥미니 노드에서 돌린다).

발주서: ves-orchestrator `docs/LOCALIZE_UNIFY.md` §10-1(의존성 위험).
현지화 이관은 무거운 의존(torch·paddlepaddle 계열)을 **본체 requirements 로** 들인다
(사용자 결정 1). 지금은 pip sync 가 깨져도 mm-06 하나만 멈추지만, 그 뒤로는
**6대 전부**가 멈춘다. 그래서 옮기기 전에 숫자를 본다.

    cd /opt/ves/engines/ai-video          # ⚠ -m 은 레포 루트에서만 된다
    .venv/bin/python -m scripts.deps_probe                       # ① 현재 venv + 충돌 신호
    .venv/bin/python -m scripts.deps_probe --resolve <req>       # ② 해석만(설치 안 함)
    .venv/bin/python -m scripts.deps_probe --resolve <req> --install --smoke   # ③ 실측

이 파일은 **표준 라이브러리만** 쓴다 — 레포에 없는 노드에서도 파일 하나만 내려
`python /tmp/deps_probe.py …` 로 그냥 돌릴 수 있다(그래서 `-m` 이 필수가 아니다).

`--install` 은 일회용 venv 에 진짜로 깐다. 후보 requirements 면 **디스크 3~5GB·
수십 분**이 든다(끝나면 --workdir 를 지우면 된다).

⚠ **리눅스에서 잰 값은 노드 값이 아니다.** pip 은 `--platform macosx_…` 를 줘도
환경 마커(`platform_system`)는 *실행 중인* OS 로 평가한다 — 그래서 리눅스에서
torch 를 해석하면 macOS 에 없는 CUDA 패키지를 끌어와 `ResolutionImpossible` 이 난다.
requirements 변경은 **반드시 노드(맥·arm64)에서** 이 스크립트로 확인할 것.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import venv
from pathlib import Path

# 같은 `cv2` 를 덮어쓰는 배포판들 — 한 venv 에 둘 이상 있으면 나중에 깔린 쪽이 이긴다.
# ai-video 는 `opencv-python<5` 에 얼굴검출(reframe)이 걸려 있고(requirements 주석),
# paddleocr 는 `opencv-contrib-python` 을 끌어온다. 조용히 섞이면 원인 못 찾는 고장이 된다.
CONFLICT_GROUPS = [
    {"opencv-python", "opencv-contrib-python", "opencv-python-headless",
     "opencv-contrib-python-headless"},
    {"pillow", "pillow-simd"},
]


# ─────────────────────────── 순수 (테스트 대상) ───────────────────────────

def normalize(name: str) -> str:
    """PEP 503 이름 정규화 — `Pillow`·`pillow`·`PIL_x` 표기 차이를 없앤다."""
    return "".join("-" if c in "-_." else c for c in name.strip().lower())


def find_conflicts(names) -> list[list[str]]:
    """한 venv 에 공존하면 안 되는 조합. 순수 — 테스트 대상."""
    have = {normalize(n) for n in names}
    out = []
    for group in CONFLICT_GROUPS:
        both = sorted(have & {normalize(g) for g in group})
        if len(both) > 1:
            out.append(both)
    return out


def human(nbytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(nbytes) < 1024 or unit == "GiB":
            return f"{nbytes:,.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} GiB"


def delta_report(before: dict, after: dict) -> dict:
    """{name: version} 두 벌 → 신규·변경·제거. 순수 — 테스트 대상.

    **변경(=업그레이드)이 신규보다 위험하다.** 새 패키지는 안 쓰면 그만이지만,
    이미 쓰던 패키지의 버전이 올라가면 지금 도는 채널이 조용히 달라진다."""
    b = {normalize(k): v for k, v in before.items()}
    a = {normalize(k): v for k, v in after.items()}
    return {
        "added": sorted(k for k in a if k not in b),
        "removed": sorted(k for k in b if k not in a),
        "changed": sorted(f"{k}: {b[k]} → {a[k]}" for k in a if k in b and a[k] != b[k]),
    }


def summarize_resolution(report: dict) -> dict:
    """pip `--report` JSON → {name: version} + 이름 목록. 순수 — 테스트 대상."""
    pkgs = {}
    for item in report.get("install", []):
        md = item.get("metadata") or {}
        if md.get("name"):
            pkgs[normalize(md["name"])] = md.get("version", "?")
    return pkgs


# ─────────────────────────── IO ───────────────────────────

def _pip(py: str, *args, timeout=3600):
    return subprocess.run([py, "-m", "pip", *args], capture_output=True, text=True,
                          timeout=timeout)


def installed(py: str = sys.executable) -> dict:
    r = _pip(py, "list", "--format=json", timeout=120)
    if r.returncode != 0:
        return {}
    return {normalize(p["name"]): p["version"] for p in json.loads(r.stdout)}


def venv_size(py: str = sys.executable) -> int:
    root = Path(py).resolve().parent.parent
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def resolve(req: Path, py: str = sys.executable) -> tuple[dict, Path | None]:
    """설치하지 않고 해석만 — 현재 플랫폼 기준(그래서 노드에서 돌려야 한다)."""
    out = Path("/tmp") / f"deps_report_{int(time.time())}.json"
    r = _pip(py, "install", "--dry-run", "--ignore-installed", "-q",
             "--report", str(out), "-r", str(req), timeout=1800)
    if r.returncode != 0:
        print(f"[deps_probe] 해석 실패 — 이 requirements 는 이 노드에서 설치되지 않는다:\n"
              f"{(r.stderr or r.stdout)[-1500:]}")
        return {}, None
    return summarize_resolution(json.loads(out.read_text())), out


def timed_install(req: Path, workdir: Path) -> dict:
    """일회용 venv 에 진짜 설치 — 시간·용량 실측. updater 의 타임아웃 상한을 정하는 근거."""
    workdir.mkdir(parents=True, exist_ok=True)
    vpath = workdir / "probe_venv"
    print(f"[deps_probe] 일회용 venv 생성: {vpath}")
    venv.create(vpath, with_pip=True, clear=True)
    py = str(vpath / "bin" / "python")
    t0 = time.time()
    r = _pip(py, "install", "-q", "-r", str(req), timeout=7200)
    elapsed = time.time() - t0
    ok = r.returncode == 0
    size = venv_size(py) if ok else 0
    if not ok:
        print(f"[deps_probe] 설치 실패({elapsed:.0f}s): {(r.stderr or r.stdout)[-800:]}")
    return {"ok": ok, "seconds": round(elapsed, 1), "venv_bytes": size,
            "packages": installed(py) if ok else {}}


# 설치가 끝났다고 도는 게 아니다 — arm64 에서 import 자체가 죽는 이력이 있다
# (paddleocr 초기화 실패로 2026-08-12 잔망루피 하루 처리량이 0~1편이 됐다).
# 그래서 '깔렸다'가 아니라 '불러진다'까지 본다.
SMOKE_IMPORTS = [
    ("cv2", "얼굴검출(reframe)·프레임 처리"),
    ("paddle", "인페인팅 route B 의 OCR 엔진 — arm64 이력 나쁨"),
    ("paddleocr", "OCR 백엔드"),
    ("rapidocr_onnxruntime", "OCR 경량 폴백"),
    ("torch", "더빙(demucs)·인페인팅(LaMa) 공통 전제"),
    ("torchaudio", "더빙 오디오 IO"),
    ("demucs", "더빙 보컬 분리"),
    ("faster_whisper", "전사"),
    ("skimage", "인페인팅 QA(SSIM)"),
]


def smoke(py: str) -> list[dict]:
    """import 되는가 + cv2 는 어느 배포판이 이겼는가. 실패해도 계속 — 전량을 본다."""
    out = []
    for mod, why in SMOKE_IMPORTS:
        code = ("import %s as m; print(getattr(m,'__version__','?'));"
                "print(getattr(m,'__file__','?'))" % mod)
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=300)
        ok = r.returncode == 0
        lines = (r.stdout or "").strip().splitlines()
        out.append({"module": mod, "why": why, "ok": ok,
                    "version": lines[0] if ok and lines else "",
                    "path": lines[1] if ok and len(lines) > 1 else "",
                    "error": "" if ok else ((r.stderr or "").strip().splitlines() or [""])[-1]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="의존성 실측 — 노드에서 돌린다")
    ap.add_argument("--resolve", help="후보 requirements 파일")
    ap.add_argument("--install", action="store_true",
                    help="일회용 venv 에 실제 설치해 시간·용량을 잰다(오래 걸린다)")
    ap.add_argument("--smoke", action="store_true",
                    help="설치 후 실제로 import 되는지까지 확인 (--install 과 함께)")
    ap.add_argument("--workdir", default="/tmp/deps_probe")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result: dict = {"platform": sys.platform, "python": sys.version.split()[0]}

    cur = installed()
    result["current"] = {"packages": len(cur), "venv_bytes": venv_size()}
    conflicts = find_conflicts(cur)
    result["current"]["conflicts"] = conflicts

    if not args.json:
        print(f"\n=== 현재 venv ({sys.platform} · py{result['python']}) ===")
        print(f"  패키지 {len(cur)}개 · 디스크 {human(result['current']['venv_bytes'])}")
        if conflicts:
            for g in conflicts:
                print(f"  ⚠ 같은 모듈을 덮어쓰는 배포판 공존: {' + '.join(g)}")
        else:
            print("  충돌 신호 없음")

    if args.resolve:
        req = Path(args.resolve)
        after, _ = resolve(req)
        if after:
            d = delta_report(cur, after)
            result["resolve"] = {"packages": len(after), **d}
            if not args.json:
                print(f"\n=== 해석 결과 — {req} ===")
                print(f"  총 {len(after)}개 · 신규 {len(d['added'])} · "
                      f"변경 {len(d['changed'])} · 제거 {len(d['removed'])}")
                for c in d["changed"][:20]:
                    print(f"  ⚠ 버전 변경: {c}")
                nc = find_conflicts(after)
                for g in nc:
                    print(f"  ⚠ 충돌 예상: {' + '.join(g)}")
                result["resolve"]["conflicts"] = nc
        else:
            result["resolve"] = {"error": "resolution_failed"}

        if args.install:
            m = timed_install(req, Path(args.workdir))
            result["install"] = {k: v for k, v in m.items() if k != "packages"}
            if not args.json:
                print(f"\n=== 실제 설치 ===")
                print(f"  {'성공' if m['ok'] else '실패'} · {m['seconds']:.0f}초 · "
                      f"디스크 {human(m['venv_bytes'])}")
                print(f"  → updater PIP_TIMEOUT_SEC 는 이 값의 3배 이상으로 "
                      f"(캐시 없는 첫 설치·느린 회선 여유)")
            if args.smoke and m["ok"]:
                sm = smoke(str(Path(args.workdir) / "probe_venv" / "bin" / "python"))
                result["smoke"] = sm
                if not args.json:
                    print("\n=== import 스모크 ===")
                    for r in sm:
                        mark = "OK " if r["ok"] else "!! "
                        tail = (f"{r['version']}" if r["ok"] else f"{r['error']}")
                        print(f"  {mark}{r['module']:<22} {str(tail)[:70]}")
                    cv = [r for r in sm if r["module"] == "cv2" and r["ok"]]
                    if cv:
                        print(f"  · cv2 실제 경로: {cv[0]['path']}")
                        print("    (opencv-python 과 contrib 가 같이 깔렸다면 나중에 깔린 쪽이다)")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
