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
from app.localize.style_texts import apply_editor_text_translation, load_json_or_none
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


VISUAL_OVERRIDE_KEYS = ("images", "texts")


def visual_only_overrides(ov):
    """편집실 오버라이드에서 **화면 겹치기(images·texts)만** 남긴다. 순수(테스트 대상).

    제목·자막·구간·내레이션은 L3 가 백업 + 번역으로 이미 일본어로 써 놓았다 — 그것들까지
    넘기면 사람이 고친 **한국어**가 일본어판 위에 그대로 다시 덮인다. 반대로 images·texts 는
    edit_overrides.json 에만 있어서(체크포인트에 안 남는다) 안 넘기면 사람이 올린 이미지·
    문구가 일본어판에서 조용히 사라진다. 그래서 이 두 키만 승계한다.

    스키마는 v3 로 찍는다 — images·texts 는 v3 전용 계약이다."""
    keep = {k: (ov or {}).get(k) for k in VISUAL_OVERRIDE_KEYS if (ov or {}).get(k)}
    return {"schema": "edit_overrides/v3", **keep} if keep else None


def design_restore_flags(run_log: dict, locale_cfg: dict, fallback=None) -> list:
    """재렌더에 쓸 --design-* 토큰. 순수(테스트 대상).

    '그 런이 실제로 쓴 디자인'을 그대로 복원한 뒤 현지화 폰트를 **뒤에** 얹는다
    (argparse 는 뒤가 이긴다). 종전엔 폰트 두 개만 넘겨서 채널·편집실이 정한 화면비·
    영상 위치·제목 스타일이 전부 엔진 기본값으로 떨어졌다
    (2026-08-23 SHOTCONE: aspect_ratio 13:9 → 완성본 1:1).

    출처 둘 — 정본은 엔진이 남기는 run_log.design_cli 이고, 없으면 오케스트레이터가
    run_dir 에 남긴 design_cli.json(fallback)을 쓴다. 두 배포가 서로를 기다리지 않게
    한 쪽만 있어도 복원이 성립한다. 둘 다 없는 옛 런은 종전과 동일하게 폰트만 — 회귀 0."""
    source = (run_log or {}).get("design_cli") or fallback or []
    restored = [str(t) for t in source]
    return restored + ["--design-title-font", locale_cfg["title_font"],
                       "--design-subtitle-font", locale_cfg["subtitle_font"]]


def read_design_cli_file(job: Path) -> list:
    """오케스트레이터가 남긴 run_dir/design_cli.json → 토큰 목록(없거나 깨지면 빈 목록).

    깨진 파일에 잡을 걸지 않는다 — 복원이 안 되면 아래에서 경고를 남기고 종전처럼 그린다."""
    p = job / "design_cli.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"[L4] ⚠️ design_cli.json 파싱 실패 — 디자인 복원 없이 진행: {p}")
        return []
    return [str(t) for t in data] if isinstance(data, list) else []


def render_argv(python: str, job: Path, work_display: str, video_path: str,
                locale_cfg: dict, gen_flags: list,
                design_flags=None, ov_flags=None) -> list:
    """재렌더 argv. 순수(테스트 대상) — `--job-id` 지정이라 제목이 달라도 디렉토리가
    새로 생기지 않는다.

    design_flags 미지정이면 종전처럼 폰트 두 개만 얹는다(옛 런 · 회귀 0)."""
    design = list(design_flags) if design_flags else [
        "--design-title-font", locale_cfg["title_font"],
        "--design-subtitle-font", locale_cfg["subtitle_font"]]
    return [python, "-m", "app.cli", "create_shorts",
            "--title", work_display,
            "--video", video_path,
            "--outdir", str(job.parent),
            "--from-step", "render", "--job-id", job.name,
            *design,
            "--max-shorts", "1", *gen_flags, *(ov_flags or [])]


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


def _video_duration(path: Path) -> float:
    """비디오 **스트림** 길이(컨테이너 길이가 아니라). 오디오 꼬리 판별용 — 실패하면 0.0."""
    try:
        return float(subprocess.run(
            [find_ffmpeg_command("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True).stdout.strip())
    except (OSError, ValueError):
        return 0.0


def cut_mismatch_hint(d_ko_video: float, d_ja_video: float,
                      tol: float = CUT_TOLERANCE_SEC) -> str:
    """길이 불일치의 **원인 갈래**를 문장으로. 순수(테스트 대상).

    두 원인은 조치가 완전히 다르다:
    · 오디오 꼬리 — **비디오 스트림끼리는 같은데** 컨테이너만 다르다. 렌더가
      `amix=duration=longest` 에 `-shortest` 없이 섞어서 오디오가 영상보다 길어진 것이다.
    · 컷 재현 실패 — 비디오 스트림 **자체가** 다르다. gen_flags(A/B 노브)가 원 생성과
      달라 클립 경계가 바뀐 것이다.

    ⚠ **반드시 스트림끼리 비교한다.** 첫 판(2026-08-24)은 ko 의 **컨테이너** 길이와 ja 의
    **비디오 스트림** 길이를 맞대는 단위 착오가 있었다 — 두 파일 모두 오디오 꼬리를 갖고
    있으면 늘 '컷 재현 실패'로 오판한다(실제로 그렇게 나왔다: ko 컨테이너 39.400 vs ja
    스트림 25.025 → 컷이 멀쩡해도 다르다고 읽혔다).
    두 값을 **모두 문장에 적는다** — 판정이 틀려도 사람이 원본 숫자로 되짚을 수 있어야 한다."""
    if d_ko_video <= 0 or d_ja_video <= 0:
        return (f"비디오 스트림 길이를 못 읽었다(ko {d_ko_video:.3f}s · ja {d_ja_video:.3f}s) "
                f"— 원인 판별 불가, gen_flags 재현 실패 의심")
    both = f"비디오 스트림 ko {d_ko_video:.3f}s · ja {d_ja_video:.3f}s"
    if abs(d_ko_video - d_ja_video) <= tol:
        return (f"{both} 로 **일치한다 — 컷은 재현됐고 차이는 오디오뿐**이다"
                f"(렌더가 `-shortest` 없이 amix=longest 로 섞는다). L3t 의 '창 길이로 잘랐다' "
                f"경고와 cue fit_trimmed 를 확인하라")
    return f"{both} 로 **다르다 — gen_flags 재현 실패 의심**(클립 경계가 바뀌었다)"


def l4_render(job: Path, wcfg: dict, locale_cfg: dict, out_dir: Path):
    _provision_fonts(locale_cfg)
    run_log = json.loads((job / "run_log.json").read_text(encoding="utf-8"))
    video_path = run_log["input"]["video_path"]
    if not Path(video_path).exists():
        raise SystemExit(f"소스 영상이 없다: {video_path}")
    gen_flags = render_flags(run_log)
    print(f"[L4] 재현 플래그: {' '.join(gen_flags)}")

    fallback = read_design_cli_file(job)
    design_flags = design_restore_flags(run_log, locale_cfg, fallback)
    restored = (run_log or {}).get("design_cli") or fallback
    if restored:
        src = "run_log" if run_log.get("design_cli") else "design_cli.json"
        print(f"[L4] 디자인 복원({src}): {' '.join(str(t) for t in restored)}")
    else:
        print("[L4] ⚠️ design_cli 가 없다(옛 런) — 화면비·제목 스타일이 엔진 기본값으로 "
              "그려진다. 한 번 더 생성하면 복원된다")

    # 편집실이 올린 이미지·자유 텍스트 승계 — 자막·제목은 넘기지 않는다(visual_only_overrides)
    ov_flags = []
    ov_src = job / "edit_overrides.json"
    if ov_src.exists():
        try:
            visual = visual_only_overrides(json.loads(ov_src.read_text(encoding="utf-8")))
        except ValueError as e:
            raise SystemExit(f"edit_overrides.json 파싱 실패: {e}") from e
        if visual:
            # E16: 편집실 문구도 일본어로 — 사람이 넣은 것과 AI 가 넣은 것이 한 화면에서
            # 한쪽만 일본어면 더 이상하다. 번역은 L1 이 이미 해 뒀다(translation.json).
            tr = load_json_or_none(out_dir / "translation.json") or {}
            visual = apply_editor_text_translation(visual, tr,
                                                   font=locale_cfg.get("telop_font"))
            ov_path = out_dir / "edit_overrides_visual.json"
            ov_path.write_text(json.dumps(visual, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            ov_flags = ["--edit-overrides", str(ov_path)]
            print("[L4] 편집실 겹치기 승계: "
                  + " · ".join(f"{k} {len(visual[k])}건"
                               for k in VISUAL_OVERRIDE_KEYS if k in visual))

    cmd = render_argv(sys.executable, job, wcfg["display"], video_path, locale_cfg,
                      gen_flags, design_flags, ov_flags)
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
            f"컨테이너 길이 불일치: ko {d_ko:.3f}s vs ja {d_ja:.3f}s — "
            + cut_mismatch_hint(_video_duration(job / "shorts_ko.mp4"),
                                _video_duration(rendered)))
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
