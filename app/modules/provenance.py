"""run provenance 스탬핑 — config·모델·프롬프트·git_sha 스냅샷 (개선 파이프라인 T0-2).

⚠️ 이 파일의 거처는 **ai-video** 다: `ai-video/app/modules/provenance.py` 로 복사해 사용.
   (여기 ai-improve-edit-video 에서는 import 금지 — `app.config`/`app.modules`는 ai-video에만 존재.
    이 repo에는 적용 대기 아티팩트로만 보관. 적용법: integration/ai_video/APPLY.md)

run_log["provenance"]에 실려 디스크 run_log.json에 남고, 격리 파이프라인 DB의 clip_metadata 로
인제스트된다(ai-improve-edit-video/scripts/ingest_aivideo_run.py 의 provenance 계약과 1:1 대응).

목적(기획서 T0-2 완료기준): "두 run의 config diff 가능" — 어떤 프롬프트/파라미터/모델/커밋으로
만든 산출물인지 추적 가능하게 한다. `host`/`machine`은 여기에 더해 **어느 머신에서** 만들었는지를
남긴다 — 맥이 여러 대이고 그중 둘은 계정명까지 같아서(맥3·맥4 = 'lunaleuteumaeg4'), 산출물만 보고는
출처를 가릴 수 없었다.
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
from dataclasses import asdict
from pathlib import Path

import app.modules.gemini_client as _gc
from app.config import AppConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]   # .../ai-video


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git_sha(root: Path) -> str | None:
    """런 시점 ai-video HEAD sha. 실패 시 None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _prompt_versions() -> dict:
    """gemini_client의 모든 프롬프트 상수(*_PROMPT / *_PROMPT_TEMPLATE)를 introspection으로
    자동 수집해 sha8 매핑. 브랜치마다 프롬프트 집합이 달라도 깨지지 않는다
    (예: cut-4는 graph 제거로 RELATIONSHIP_EXTRACTION_PROMPT 없음·AV_ALIGN_PROMPT 추가)."""
    out = {}
    for name in dir(_gc):
        if name.endswith(("_PROMPT", "_PROMPT_TEMPLATE")):
            val = getattr(_gc, name)
            if isinstance(val, str):
                out[name.lower()] = _sha256(val)[:12]
    return out


def _host() -> str | None:
    """런을 돌린 머신의 raw hostname. 여러 맥에서 생성할 때 산출물 출처를 구분한다.

    ⚠️ config 스냅샷 **밖**(provenance 최상위)에 둔다 — config_hash 는 config 만 해싱하므로,
    머신이 달라도 같은 설정이면 config_hash 가 같아야 A/B 쌍 대조가 성립한다.
    """
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def _machine() -> str | None:
    """배정 정본(brain `config/assignments.json`)의 머신 id — 예: 'macmini-luna3'.

    운영 정본은 hostname 이 아니라 사람이 붙인 kebab-case id 다(hostname 은 .local 접미사·
    네트워크 변동이 있고, 계정명은 맥3·맥4 가 공유한다). 다만 ai-video 는 brain 의 배정 정본을
    읽지 않으므로, 여기서는 **환경변수로 선언된 경우만** 기록한다. 없으면 brain 인제스트가
    `host` 로 역산한다(channel_registry.detect_machine_id).
    """
    return os.environ.get("SCENE_LOOP_MACHINE") or None


def build_provenance(config: AppConfig, design=None) -> dict:
    """run_log["provenance"]에 넣을 dict. ingest_aivideo_run.py의 provenance 계약과 일치.

    주의: DesignConfig는 run_pipeline 스코프 밖(videoApi/cli에서 생성)이라 여기서 미수집.
    config diff의 핵심 레버(viral_score_min_threshold·target_duration_sec·max_storyline_candidates
    등)는 모두 AppConfig에 있으므로 1차 목표는 충족. design 스냅샷은 후속(run_pipeline에 DesignConfig
    를 전달하도록 시그니처 확장 시 추가).
    """
    pv = _prompt_versions()
    return {
        "git_sha": _git_sha(_REPO_ROOT),
        "host": _host(),
        "machine": _machine(),
        "models": {
            "pro": os.getenv("GEMINI_MODEL_NAME", "gemini-3.1-pro-preview"),
            "flash": os.getenv("GEMINI_FLASH_MODEL_NAME", "gemini-3.6-flash"),
        },
        "config": {"app": asdict(config), "design": (asdict(design) if design is not None else None)},
        "prompt_set_hash": _sha256("".join(sorted(pv.values()))),
        "prompt_versions": pv,
        "reranker_version": None,
        "notes": "design_config not captured at run_pipeline scope (follow-up)",
    }
