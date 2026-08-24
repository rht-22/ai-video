"""L4 — 같은 노브로 재렌더하고 텔롭을 번인한다.

원본: `localize_run.render_flags` · `_provision_fonts` · `l4_render`.

⚠ **컷 재현이 이 단계의 전부다.** 원 생성과 같은 A/B 노브로 돌지 않으면 컷이 달라져
자막 싱크가 통째로 깨진다(SPIKE §설계수정-2 실증: 49.7s → 53.3s). 그래서 렌더 뒤에
원본과 길이를 대조해 0.05초라도 어긋나면 실패시킨다.

⚠ 재렌더는 **자기 자신을 subprocess 로** 부른다(`app.cli create_shorts --from-step render`).
같은 프로세스에서 파이프라인을 직접 부르면 모듈 전역 상태·환경이 섞여 산출이 달라질 수
있다 — 회귀 0 이 조건이라 vlp 가 쓰던 프로세스 경계를 그대로 둔다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from app.localize.spec import BRAIN, FONTS_DIR, REPO_ROOT
from app.modules.ffmpeg_utils import find_ffmpeg_command

CUT_TOLERANCE_SEC = 0.05      # 이보다 어긋나면 gen_flags 재현 실패로 본다
_ASS_FILTER_CACHE: dict[str, bool] = {}


def render_flags(run_log: dict) -> list:
    """재렌더 A/B 노브 복원. 순수(테스트 대상).

    컷을 프레임 단위로 재현하려면 원 생성과 **같은 노브**여야 한다. 정본은 그 런의
    run_log.provenance.config.app — **실제로 쓰인 값**이다. brain loop_policy.gen_flags_base 는
    '현재 정책'이라 런 이후 바뀌었을 수 있고, ves-orchestrator 경로에서는 work_order.knob_config
    가 정책을 덮으므로 애초에 일치를 보장하지 못한다. provenance 가 없는 옛 런만 정책으로 폴백."""
    app = ((run_log or {}).get("provenance") or {}).get("config", {}).get("app") or {}
    flags = []
    prof = app.get("silence_cut_profile")
    if prof in ("aggressive", "conservative"):
        flags += ["--silence-profile", prof]
    # length=tight 의 지문 — cli._apply_ab_env 가 세팅하는 세 값 그대로(45/50/1.1)
    try:
        tight = (int(app.get("target_duration_sec")) == 45
                 and int(app.get("max_duration_sec")) == 50
                 and abs(float(app.get("max_duration_tolerance")) - 1.1) < 1e-6)
    except (TypeError, ValueError):
        tight = False
    if tight:
        flags += ["--length-profile", "tight"]
    if not flags:
        try:
            flags = list(json.loads((BRAIN / "config" / "loop_policy.json")
                                    .read_text(encoding="utf-8"))["gen_flags_base"])
        except (OSError, ValueError, KeyError):
            flags = []
    if "--loudness-lufs" not in flags:
        flags += ["--loudness-lufs", "-14"]   # 쇼츠 표준. 컷에는 영향 없다(오디오 전용)
    return flags


def has_ass_filter(ffmpeg: str) -> bool:
    """이 ffmpeg 빌드에 ass(libass) 필터가 있는가 — 자막 번인 가능 여부.

    libass 없이 빌드된 ffmpeg 는 `-h filter=ass` 에 "Unknown filter" 를 내면서도
    종료코드 0 을 반환하므로 출력 문자열로 판별한다."""
    if ffmpeg not in _ASS_FILTER_CACHE:
        try:
            r = subprocess.run([ffmpeg, "-hide_banner", "-h", "filter=ass"],
                               capture_output=True, text=True)
            _ASS_FILTER_CACHE[ffmpeg] = "Filter ass" in (r.stdout + r.stderr)
        except OSError:
            _ASS_FILTER_CACHE[ffmpeg] = False
    return _ASS_FILTER_CACHE[ffmpeg]


def ass_filter_hint(ffmpeg: str) -> str:
    """ass 필터 부재 시 사용자 안내 문구."""
    return (f"ffmpeg('{ffmpeg}') 빌드에 ass 필터(libass) 없음 → 자막 번인 불가. "
            "libass 포함 빌드를 FFMPEG_BIN 환경변수(.env 가능)로 지정하세요 "
            "(예: FFMPEG_BIN=/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg).")


def render_argv(python: str, job: Path, work_display: str, video_path: str,
                locale_cfg: dict, gen_flags: list) -> list:
    """재렌더 argv. 순수(테스트 대상) — `--job-id` 지정이라 제목이 달라도 디렉토리가
    새로 생기지 않는다."""
    return [python, "-m", "app.cli", "create_shorts",
            "--title", work_display,
            "--video", video_path,
            "--outdir", str(job.parent),
            "--from-step", "render", "--job-id", job.name,
            "--design-title-font", locale_cfg["title_font"],
            "--design-subtitle-font", locale_cfg["subtitle_font"],
            "--max-shorts", "1", *gen_flags]


SYSTEM_JP_FONT = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")


def _provision_fonts(locale_cfg: dict):
    """일본어 폰트 자동 프로비저닝 — ArialUnicode 는 macOS 시스템 폰트(재배포 라이선스
    문제로 레포에 못 넣는다)라, 없으면 시스템 사본을 assets 로 복사한다.
    전 워커 노드가 맥이라는 전제(ves-orchestrator MACHINE_SETUP).

    ⚠ **`copy2` 가 아니라 `copyfile` 이다.** copy2 는 메타데이터까지 복사하는데,
    macOS 시스템 폰트에는 SIP 플래그가 붙어 있어 `chflags` 가
    `PermissionError: Operation not permitted` 로 죽는다(노드 실측). 우리가 원하는 건
    글리프뿐이고 SIP 플래그는 오히려 안 따라와야 한다.
    운영 노드는 폰트가 이미 있어(untracked) 이 경로를 안 타므로 여태 안 드러났다 —
    **새 노드·새 체크아웃에서 처음 도는 순간 터진다.**"""
    for key in ("title_font", "subtitle_font", "telop_font"):
        if locale_cfg.get(key) != "ArialUnicode":
            continue
        dst = FONTS_DIR / "ArialUnicode.ttf"
        if not dst.exists():
            if not SYSTEM_JP_FONT.exists():
                raise SystemExit(
                    f"일본어 폰트 없음: {dst} — macOS 시스템 폰트({SYSTEM_JP_FONT})도 없다")
            FONTS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SYSTEM_JP_FONT, dst)     # 내용만 — 메타데이터는 복사하지 않는다
            print(f"[L4] 폰트 프로비저닝: {SYSTEM_JP_FONT.name} → {dst}")
        break


def _duration(path: Path) -> float:
    return float(subprocess.run(
        [find_ffmpeg_command("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout.strip())


def l4_render(job: Path, wcfg: dict, locale_cfg: dict, out_dir: Path):
    _provision_fonts(locale_cfg)
    run_log = json.loads((job / "run_log.json").read_text(encoding="utf-8"))
    video_path = run_log["input"]["video_path"]
    if not Path(video_path).exists():
        raise SystemExit(f"소스 영상이 없다: {video_path}")
    gen_flags = render_flags(run_log)
    print(f"[L4] 재현 플래그: {' '.join(gen_flags)}")

    cmd = render_argv(sys.executable, job, wcfg["display"], video_path, locale_cfg, gen_flags)
    print(f"[L4] 재렌더: {' '.join(cmd[3:])}")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=1800)
    (out_dir / "rerender.log").write_text(
        r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"재렌더 실패 rc={r.returncode} — {out_dir/'rerender.log'} 확인")
    rendered = job / "shorts.mp4"
    # 컷 재현 검증 — 원본과 길이가 다르면 자막 싱크가 깨진 것 (SPIKE §설계수정-2)
    d_ko, d_ja = _duration(job / "shorts_ko.mp4"), _duration(rendered)
    if abs(d_ko - d_ja) > CUT_TOLERANCE_SEC:
        raise RuntimeError(
            f"컷 길이 불일치: ko {d_ko:.3f}s vs ja {d_ja:.3f}s — gen_flags 재현 실패 의심")
    print(f"[L4] 재렌더 완료 {time.time()-t0:.0f}s (길이 {d_ja:.3f}s = 원본 일치)")

    # 텔롭 병기 번인 (오디오 무손실 copy)
    telop_ass = out_dir / "telops.ass"
    notelop = job / "shorts_ja_notelop.mp4"
    shutil.move(rendered, notelop)
    ass_arg = str(telop_ass).replace(":", "\\:")
    fonts_arg = str(FONTS_DIR).replace(":", "\\:")
    ffmpeg = find_ffmpeg_command("ffmpeg")
    r2 = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(notelop),
         "-vf", f"ass='{ass_arg}':fontsdir='{fonts_arg}'",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "copy",
         str(rendered)], capture_output=True, text=True, timeout=600)
    if r2.returncode != 0:
        shutil.move(notelop, rendered)          # 원복
        if not has_ass_filter(ffmpeg):
            raise RuntimeError(ass_filter_hint(ffmpeg))
        raise RuntimeError(f"텔롭 번인 실패: {r2.stderr[-500:]}")
    print("[L4] 텔롭 번인 완료 → shorts.mp4 (중간본 shorts_ja_notelop.mp4 보존)")
