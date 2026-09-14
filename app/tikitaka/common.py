"""공통 유틸 — 타임코드 서식 · JSON 체크포인트 · 잡 디렉토리 · 실행 로그."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── 지침서 상수 ────────────────────────────────────────────────────────────
SAFE_MARGIN_SEC = 0.100          # 제0-2원칙: 컷 경계 ±0.1s 안쪽만 사용
NARRATION_CHARS_PER_SEC = 4.0    # 제1원칙: 한국어 내레이션 4글자/초 (계획값 — 실측 TTS 길이가 마스터)
STACK_CUT_MIN_SEC = 1.0          # 제2원칙: 정배속 컷 분할 — 컷 하나 1.0~2.0초
STACK_CUT_MAX_SEC = 2.0
DIALOGUE_LEAD_SEC = 0.05         # S 모드: 첫 단어 시작 앞 여유
DIALOGUE_TAIL_SEC = 0.15         # S 모드: 마지막 단어 끝 뒤 여유
MAX_SHORTS_SEC = 75.0            # 쇼츠 전체 상한(리빌딩 대본 예산의 벨트)

_TC_RE = re.compile(r"^(\d{1,3}):(\d{2})\.(\d{1,3})$")


def fmt_tc(sec: float) -> str:
    """초 → `MM:SS.ms` (지침서 단위 표준). 음수는 0 으로."""
    sec = max(0.0, float(sec))
    ms_total = int(round(sec * 1000))
    m, rem = divmod(ms_total, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{m:02d}:{s:02d}.{ms:03d}"


def parse_tc(text: str) -> float:
    """`MM:SS.ms` → 초. 형식이 아니면 ValueError (근사치 엄금 — 조용히 0 으로 떨어지지 않는다)."""
    m = _TC_RE.match(str(text).strip())
    if not m:
        raise ValueError(f"타임코드 형식 위반: {text!r} (MM:SS.ms 필요)")
    mm, ss, ms = m.groups()
    ms = (ms + "000")[:3]
    return int(mm) * 60 + int(ss) + int(ms) / 1000.0


def ms3(sec: float) -> float:
    return round(float(sec) + 0.0, 3)


# ── 잡 디렉토리 / 체크포인트 ────────────────────────────────────────────────
@dataclass
class Job:
    source: Path
    out_dir: Path
    title: str
    log_lines: list[str] = field(default_factory=list)

    def path(self, name: str) -> Path:
        return self.out_dir / name

    def has(self, name: str) -> bool:
        return self.path(name).exists()

    def load(self, name: str) -> Any:
        with open(self.path(name), encoding="utf-8") as f:
            return json.load(f)

    def save(self, name: str, data: Any) -> Path:
        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return p

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.log_lines.append(line)
        try:
            with open(self.path("run.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def record_step(self, step: str, **info: Any) -> None:
        """run_log.json 에 단계 기록을 덧붙인다(가산적)."""
        name = "run_log.json"
        data = self.load(name) if self.has(name) else {"title": self.title, "source": str(self.source), "steps": []}
        info = {"step": step, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **info}
        data["steps"] = [s for s in data.get("steps", []) if s.get("step") != step] + [info]
        self.save(name, data)


def find_bin(name: str) -> str:
    """FFMPEG_BIN / FFPROBE_BIN 우선 — 레포 규약(app.modules.ffmpeg_utils)과 같은 해석."""
    from app.modules.ffmpeg_utils import find_ffmpeg_command
    return find_ffmpeg_command(name)


def load_dotenv_if_any() -> None:
    """레포 루트 .env → os.environ (있는 키는 덮지 않는다)."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    # 워크트리에서 돌면 원 레포 루트의 .env 도 본다
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / ".git").exists() or (p / ".env").exists():
            candidates.append(p / ".env")
        if p.name == "worktrees":
            candidates.append(p.parent.parent / ".env")
    # 노드 배포용 원본 env(ves.env)도 본다 — ELEVENLABS_API_KEY 등은 .env 가 아니라 여기 있다(2026-09-10 실측)
    candidates += [c.with_name("ves.env") for c in list(candidates)]
    for c in candidates:
        if c.exists():
            for line in c.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
